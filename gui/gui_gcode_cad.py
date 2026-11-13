#!/usr/bin/env python3
"""gui_gcode_cad.py -- MODERN-UI ONLY: the G-code page's CAD chrome.

Two small widgets the CAD-layout G-code page is built from, drawn in the
app's own palette (`gui_widgets.palette`) and re-coloured with it on the
dark/light switch (`gui_widgets.register`):

  * `ToolButton` -- a ribbon tool: a line icon (`gui_ribbon_icons`) over a
    one- or two-line label, as Fusion's toolbar draws them. A command, or a
    menu posted under it (a small caret beside the icon says so).
  * `ribbon_group` -- a captioned group of tools, its caption under the
    tools, a hairline after it.
  * `RibbonFit` -- keeps a ribbon inside its window: when it is too wide
    the tools first shrink to their labels, then the rightmost groups fold,
    one by one, into a button that pops the group's tools out.
  * `OneRow` -- the preview's display toggles on one line, for the strip
    at the bottom of the picture.

The layout itself (which tools, the overlays) is the page's:
`gui_gcode_editor._GcodeEditorMixin._build_gcode_editor_page`."""

import ctypes
import sys
import contextlib
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

import gui_widgets
from gui_preview import _OptionFlow
from gui_tips import Tooltip, add_tip

#: the glyphs' font: Segoe UI itself has almost none of the arrows and
#: symbols the tools use (measured: of 23, only the plain left-right arrow),
#: Segoe UI Symbol has all of them
GLYPH_FONT = ("Segoe UI Symbol", 12, "bold")

#: which palette colour each ribbon group's glyphs take (the text-glyph
#: fallback only: a named icon is drawn in `fg` with the one `st_info` accent)
GROUP_TINT = {"add": "st_info", "change": "st_warn", "inspect": "st_ok",
              "file": "fg"}

#: a ribbon icon's size at 96 dpi; it scales with the screen's dpi
ICON_PX = 24


def _icon_px(w):
    return max(16, round(ICON_PX * w.winfo_fpixels("1i") / 96.0))


def _tool_height(w):
    """A tool's height, measured: the icon over a two-line label, as drawn,
    plus the packing."""
    t = tk.Label(w, text="A\nB", font="MewHint", pady=0)
    h = _icon_px(w) + t.winfo_reqheight() + 12
    t.destroy()
    return h


def _tool_width(text):
    """A tool's width: the same for every tool -- a short word's width plus
    the margins -- unless a line of its label needs more ("Parameters"), so
    no label is broken or clipped. Measured in the font, so it holds at any
    dpi."""
    try:
        f = tkfont.nametofont("MewHint")
    except tk.TclError:     # an app built without gui_fonts.init_fonts
        f = tkfont.nametofont("TkDefaultFont")
    base = f.measure("Footprint") + 18
    return max(base, max(f.measure(ln) for ln in text.split("\n")) + 12)


def _fit_width(w, text, menu=False):
    """A tool's compact width (`RibbonFit`): its label's longest line plus
    the margins, or its icon's (with the menu caret's two margins) when
    that is wider."""
    try:
        f = tkfont.nametofont("MewHint")
    except tk.TclError:
        f = tkfont.nametofont("TkDefaultFont")
    px = _icon_px(w)
    icon = px + (2 * max(5, px // 4) if menu else 0) + 8
    return max(icon, max(f.measure(ln) for ln in text.split("\n")) + 10)


def _mix(a, b, t):
    """#rrggbb `a` moved a fraction `t` of the way to `b`."""
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t)
                                   for x, y in zip(pa, pb))


class GlyphButton(tk.Label):
    """A bare glyph that acts as a button (Undo, Redo in the title row),
    drawn in the palette and re-coloured with it."""

    def __init__(self, parent, glyph, command, size=16):
        from gui_ribbon_icons import ICONS
        # `glyph` names an icon (gui_ribbon_icons.ICONS), drawn about as
        # tall as a `size`-point glyph; anything else is drawn as text
        self._icon = glyph if glyph in ICONS else None
        self._icon_px = round(size * 1.25 * parent.winfo_fpixels("1i")
                              / 96.0)
        self._icon_key = None
        self._photo = None
        if self._icon:
            super().__init__(parent, cursor="hand2", padx=4, pady=0)
        else:
            super().__init__(parent, text=glyph, cursor="hand2", padx=4,
                             pady=0, font=(GLYPH_FONT[0], size))
        self._cmd = command
        self._hover = False
        self.bind("<Enter>", lambda _e: self._set_hover(True), add="+")
        self.bind("<Leave>", lambda _e: self._set_hover(False), add="+")
        self.bind("<ButtonRelease-1>", self._click, add="+")
        gui_widgets.register(self)
        self.retheme()

    def _click(self, _e=None):
        if str(self.cget("state")) != "disabled":
            self._cmd()

    def _set_hover(self, on):
        self._hover = on
        self.retheme()

    def retheme(self):
        pal = gui_widgets.palette()
        # the title row is a Bar.TFrame, drawn in `card`
        self.configure(background=pal["hover"] if self._hover
                       else pal["card"], foreground=pal["fg"],
                       disabledforeground=pal["disabled"])
        if not self._icon:
            return
        # an icon in one colour: the text's, or grey while disabled
        ink = pal["disabled"] if str(self.cget("state")) == "disabled" \
            else _mix(pal["fg"], pal["card"], 0.12)
        key = (ink, self._icon_px)
        if key != self._icon_key:
            from PIL import ImageTk
            from gui_ribbon_icons import icon_image
            self._photo = ImageTk.PhotoImage(
                icon_image(self._icon, self._icon_px, ink, ink), master=self)
            super().configure(image=self._photo)
            self._icon_key = key

    # greying by `configure(state=...)` (the page's undo refresh) redraws
    # the icon in grey. The state is kept here, not given to Tk: a
    # disabled Label stipples its image with a checker
    def configure(self, cnf=None, **kw):
        if "state" in kw and getattr(self, "_icon", None):
            self._state = str(kw.pop("state"))
            self.retheme()
        if kw or cnf is not None:
            return super().configure(cnf, **kw)
        return None

    config = configure

    def cget(self, key):
        if key == "state" and getattr(self, "_icon", None):
            return getattr(self, "_state", "normal")
        return super().cget(key)

    __getitem__ = cget


class ToolButton(tk.Frame):
    """A ribbon tool. `command` runs on click; with `menu` (a tk.Menu, or
    a callable returning one) the menu is posted under the button instead.
    `set_on(True)` draws it pressed (a toggle such as G-code (G));
    `set_enabled(False)` greys it."""

    def __init__(self, parent, glyph, text, command=None, menu=None,
                 tint="fg", tip=None, width=None):
        from gui_ribbon_icons import ICONS
        pal = gui_widgets.palette()
        # the width it is built at is its full width (`RibbonFit` puts it
        # back when the ribbon fits)
        self._w_full = width or _tool_width(text)
        self._fit = None            # the RibbonFit that sizes it, if any
        self._fold_info = None      # its pack options while folded into More
        super().__init__(parent, width=self._w_full,
                         height=_tool_height(parent),
                         cursor="hand2", background=pal["card"],
                         takefocus=1)
        self.pack_propagate(False)
        self._cmd = command
        self._menu = menu
        self._tint = tint
        self._on = False
        self._enabled = True
        self._hover = False
        self._focus = False         # a Tab stop: drawn like a hover, label in the accent
        # `glyph` names an icon (gui_ribbon_icons.ICONS); anything else is
        # drawn as a text glyph, as before
        self._icon = glyph if glyph in ICONS else None
        self._icon_key = None
        self._photo = None
        if self._icon:
            self._glyph = tk.Label(self, padx=0, pady=0, borderwidth=0)
        else:
            self._glyph = tk.Label(self, text=glyph, width=2,
                                   font=GLYPH_FONT, padx=2, pady=0)
        self._glyph.pack(pady=(6, 2))
        self._text = tk.Label(self, text=text, font="MewHint",
                              justify="center", pady=0)
        self._text.pack()
        for w in (self, self._glyph, self._text):
            w.bind("<Enter>", self._enter, add="+")
            w.bind("<Leave>", self._leave, add="+")
            w.bind("<ButtonRelease-1>", self._click, add="+")
        # the keyboard reaches every tool: Tab to it, Return or Space presses
        # it (a menu tool posts its menu, which the arrow keys then walk)
        self.bind("<FocusIn>", lambda _e: self._focused(True), add="+")
        self.bind("<FocusOut>", lambda _e: self._focused(False), add="+")
        self.bind("<Return>", lambda _e: self.invoke(), add="+")
        self.bind("<space>", lambda _e: self.invoke(), add="+")
        self._tip = tip or ""
        self._tips = [Tooltip(w, self._tip)
                      for w in (self, self._glyph, self._text)]
        gui_widgets.register(self)
        self.retheme()

    def set_why(self, why):
        """Grey the tool when `why` (a sentence: what to select first) is
        given, and say it in the tooltip; None: the tool can act."""
        self.set_enabled(not why)
        self._why = why
        self._tips_text()

    def set_note(self, note):
        """A line the tooltip adds while the tool still acts (what part of
        its menu is greyed, and why); None for none."""
        self._note = note
        self._tips_text()

    def _tips_text(self):
        extra = [x for x in (getattr(self, "_why", None),
                             getattr(self, "_note", None)) if x]
        text = "\n\n".join([self._tip] + extra)
        for t in self._tips:
            t.text = text

    def retheme(self):
        pal = gui_widgets.palette()
        # pressed (a toggle that is on): a wash of the accent, not a fill --
        # the label stays readable and the icon keeps its colours
        bg = _mix(pal["card"], pal["select_bg"], 0.22) if self._on else (
            pal["hover"] if (self._hover or self._focus) else pal["card"])
        fg = pal["fg"] if self._enabled else pal["disabled"]
        if self._focus and self._enabled:
            fg = pal["select_bg"]
        self.configure(background=bg)
        self._text.configure(background=bg, foreground=fg)
        if self._icon:
            self._glyph.configure(background=bg)
            self._draw_icon(pal)
            return
        glyph = pal.get(self._tint, pal["fg"]) if self._enabled             else pal["disabled"]
        self._glyph.configure(background=bg, foreground=glyph)

    def _draw_icon(self, pal):
        """The icon in the palette's ink and accent (both grey when the
        tool is off), and a caret at its lower right when the tool opens a
        menu. Redrawn only when a colour changes (the ground is clear, so a
        hover needs none)."""
        if self._enabled:
            ink = _mix(pal["fg"], pal["card"], 0.12)
            acc = pal["st_info"]
        else:
            ink = acc = pal["disabled"]
        caret = pal["hint"] if self._enabled else pal["disabled"]
        size = _icon_px(self)
        key = (ink, acc, caret if self._menu is not None else None, size)
        if key == self._icon_key:
            return
        from PIL import Image, ImageDraw, ImageTk
        from gui_ribbon_icons import icon_image
        im = icon_image(self._icon, size, ink, acc)
        if self._menu is not None:
            # the caret sits in a margin on each side, so the icon stays
            # centred over the label
            c = max(5, size // 4)
            out = Image.new("RGBA", (size + 2 * c, size), (0, 0, 0, 0))
            out.alpha_composite(im, (c, 0))
            k = 4
            big = Image.new("RGBA", (c * k, size * k), (0, 0, 0, 0))
            d = ImageDraw.Draw(big)
            h = (c - 1) * k
            y = (size - 2) * k
            d.polygon([(k, y - h // 2), (k + h, y - h // 2),
                       (k + h // 2, y)], fill=caret)
            out.alpha_composite(big.resize((c, size), Image.LANCZOS),
                                (size + c, 0))
            im = out
        self._photo = ImageTk.PhotoImage(im, master=self)
        self._glyph.configure(image=self._photo)
        self._icon_key = key

    # `state` and `text` read and write like a ttk.Button's, so the shared
    # code that greys or relabels a button by name keeps working on a tool
    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw, cnf = dict(cnf, **kw), None
        if "state" in kw:
            self.set_enabled(str(kw.pop("state")) != "disabled")
        if "text" in kw:
            text = kw.pop("text")
            # a relabel to the same words leaves every width as it is: no
            # refit (the page's refresh relabels its tools on each design
            # change, and each refit measured the whole ribbon)
            if str(text) != str(self._text.cget("text")):
                self._text.configure(text=text)
                # a longer label widens the tool: its width was measured
                # from the label it was built with, so Collector, built as
                # "Plate (3-axis)", cut "Mandrel (4-axis)" at both ends.
                # Never narrower again, so a relabel back does not shuffle
                # the ribbon
                need = _tool_width(str(text))
                if need > self._w_full:
                    self._w_full = need
                    if self._fit is None:
                        tk.Frame.configure(self, width=need)
                if self._fit is not None:
                    self._fit.fit()
        if kw:
            return super().configure(**kw)
        if cnf is not None:
            return super().configure(cnf)
        return None

    config = configure

    def cget(self, key):
        if key == "state":
            return "normal" if self._enabled else "disabled"
        if key == "text":
            return self._text.cget("text")
        return super().cget(key)

    __getitem__ = cget

    def set_on(self, on):
        self._on = bool(on)
        self.retheme()

    def set_enabled(self, on):
        self._enabled = bool(on)
        tk.Frame.configure(self, takefocus=1 if self._enabled else 0)
        self.retheme()

    def _focused(self, on):
        self._focus = bool(on)
        self.retheme()

    def _enter(self, _e=None):
        self._hover = True
        self.retheme()

    def _leave(self, _e=None):
        self._hover = False
        self.retheme()

    def _click(self, e):
        if not self._enabled:
            # a greyed tool says why it is grey where it was clicked: the
            # tooltip says it too, but only to someone who hovers and waits
            say = getattr(self, "say", None)
            why = getattr(self, "_why", None)
            if say is not None and why:
                say(why)
            return
        # a release outside the button is a cancelled click
        x, y = e.x_root - self.winfo_rootx(), e.y_root - self.winfo_rooty()
        if not (0 <= x < self.winfo_width() and 0 <= y < self.winfo_height()):
            return
        self.invoke()

    def invoke(self, at=None):
        """What a click does: run the command, or post the menu under the
        button (under `at`, a widget, for a tool folded out of sight)."""
        if not self._enabled:
            return
        menu = self._menu() if callable(self._menu) else self._menu
        if menu is not None:
            w = at if at is not None else self
            # a posted menu is a NATIVE window on Windows, so Tk cannot say
            # where it is: remember where it was posted (gui_cell_hover)
            menu._post_xy = (w.winfo_rootx(), w.winfo_rooty() + w.winfo_height())
            menu.tk_popup(*menu._post_xy)
        elif self._cmd is not None:
            self._cmd()


def eye_images(master):
    """The tree's show / hide eyes, as Fusion's browser draws them: an open
    eye, and the same eye dimmed and struck through. Drawn at four times
    the size and scaled down, so the curves are smooth. Grey on a clear
    ground reads on the dark and the light theme alike."""
    from PIL import Image, ImageDraw, ImageTk
    from gui_fonts import dpi_px
    S = 4
    w, h = 16 * S, 12 * S

    def draw(on):
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        col = (139, 152, 168, 255) if on else (139, 152, 168, 110)
        d.ellipse((1 * S, 2 * S, 15 * S, 10 * S), outline=col, width=S + 1)
        d.ellipse((5.5 * S, 3.5 * S, 10.5 * S, 8.5 * S), fill=col)
        if not on:
            d.line((2 * S, 11 * S, 14 * S, 1 * S), fill=(139, 152, 168, 220),
                   width=S + 2)
        # 16 x 12 at 96 dpi, scaled with the screen like the text beside it
        size = (dpi_px(master, 16), dpi_px(master, 12))
        return ImageTk.PhotoImage(im.resize(size, Image.LANCZOS),
                                  master=master)
    return {"on": draw(True), "off": draw(False)}


def tree_row_style(master, name):
    """Fusion's row on the ttk Treeview style `name`: a thin chevron (right
    closed, down open, nothing on a leaf), then the eye, then the text, with
    no focus box. Returns the chevrons, which the caller keeps alive."""
    from PIL import Image, ImageDraw, ImageTk
    from gui_fonts import dpi_px
    S = 4
    n = 12 * S

    def draw(points):
        im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        if points:
            ImageDraw.Draw(im).line([(x * S, y * S) for x, y in points],
                                    fill=(139, 152, 168, 255), width=S + 2,
                                    joint="curve")
        side = dpi_px(master, 12)       # scales with the text, as the eye
        return ImageTk.PhotoImage(im.resize((side, side), Image.LANCZOS),
                                  master=master)
    shut = draw([(4.5, 2.5), (8, 6), (4.5, 9.5)])
    opened = draw([(2.5, 4.5), (6, 8), (9.5, 4.5)])
    leaf = draw(None)
    style = ttk.Style()
    part = name.split(".")[0] + ".indicator"
    try:
        style.element_create(part, "image", shut, ("user2", leaf),
                             ("user1", opened),
                             padding=(dpi_px(master, 2), 0,
                                      dpi_px(master, 4), 0))
    except tk.TclError:         # made once per theme
        pass
    style.layout(name + ".Item", [("Treeitem.padding", {
        "sticky": "nswe", "children": [
            (part, {"side": "left", "sticky": ""}),
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.text", {"side": "left", "sticky": ""})]})])
    return shut, opened, leaf


def clip_window(widget, rects):
    """Show only `rects` (left, top, right, bottom, in `widget`'s pixels) of
    its window; the rest is gone from the screen and from the mouse, and
    whatever is behind it shows. Windows only: elsewhere the widget stays
    the plain rectangle it was. False when nothing was clipped."""
    if sys.platform != "win32":
        return False
    from ctypes import wintypes
    gdi = ctypes.WinDLL("gdi32")
    user = ctypes.WinDLL("user32")
    gdi.CreateRectRgn.restype = wintypes.HRGN
    gdi.CreateRectRgn.argtypes = (ctypes.c_int,) * 4
    gdi.CombineRgn.argtypes = (wintypes.HRGN,) * 3 + (ctypes.c_int,)
    gdi.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
    user.SetWindowRgn.argtypes = (wintypes.HWND, wintypes.HRGN, wintypes.BOOL)
    rgn = gdi.CreateRectRgn(0, 0, 0, 0)
    for rect in rects:
        part = gdi.CreateRectRgn(*rect)
        gdi.CombineRgn(rgn, rgn, part, 2)       # RGN_OR
        gdi.DeleteObject(part)
    # the window owns the region from here on
    return bool(user.SetWindowRgn(widget.winfo_id(), rgn, True))


def cut_hidden(paths, ptimes, windows):
    """The drawn fiber with the print-time `windows` [(t0, t1), ...] cut
    out: ``(paths, ptimes)``, each polyline split where a hidden stretch
    was. A segment goes when its time span overlaps a window at all."""
    import numpy as np
    w = sorted((a, b) for a, b in windows if b > a)
    merged = []
    for a, b in w:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    if not merged:
        return paths, ptimes
    ws = np.array([m[0] for m in merged])
    we = np.array([m[1] for m in merged])
    out_p, out_t = [], []
    for p, tp in zip(paths, ptimes):
        tp = np.asarray(tp, np.float64)
        if len(tp) < 2:
            continue
        a, b = tp[:-1], tp[1:]
        k = np.searchsorted(ws, b, side="left") - 1
        gone = (k >= 0) & (we[np.clip(k, 0, None)] > a)
        keep = ~gone
        # runs of kept segments -> polylines of their points
        edges = np.flatnonzero(np.diff(np.r_[0, keep.astype(np.int8), 0]))
        for s, e in zip(edges[::2], edges[1::2]):
            out_p.append(np.asarray(p)[s:e + 1])
            out_t.append(tp[s:e + 1])
    return out_p, out_t


class OneRow(_OptionFlow):
    """The preview's display toggles on ONE line, each at its own width:
    the designer's builder fills it (`add_toggle`, the same gate handles),
    and the strip it sits in is placed as wide as it asks for.

    FOLDING (`fit`), the row keeps the first `keep` shown items and puts
    the rest behind a "More…" link whose menu lists them on the same
    variables: the editor fits it to what the action bar leaves
    (`_gce_disp_fit`), the way the ribbon folds groups into More."""

    #: shown items kept on the row; None keeps all of them, no More
    keep = None
    #: px to spare before a folded item comes back onto the row
    HYST = 8
    _fold_btn = None

    def _shown(self):
        return [it for it in self._items if it.shown]

    def _more_btn(self):
        if self._fold_btn is None:
            self._fold_btn = ttk.Button(self, text="More…",
                                        style="BarLink.TButton",
                                        command=self._fold_popup)
            add_tip(self._fold_btn, "The display options that do not fit "
                    "on this bar at the window's width.")
        return self._fold_btn

    def width_at(self, keep):
        """What grid asks for with the first `keep` shown items on the row
        (None: all, no More link). The last one has no gap after it."""
        vis = self._shown()
        ws = [it.widget.winfo_reqwidth() for it in vis]
        if keep is not None and keep < len(vis):
            ws = ws[:keep] + [self._more_btn().winfo_reqwidth()]
        return sum(ws) + self._hgap * max(len(ws) - 1, 0)

    def fit(self, avail):
        """Keep as many items as `avail` px holds; one comes back only
        with `HYST` px to spare, so a window at the edge does not flicker."""
        n = len(self._shown())
        cur = n if self.keep is None else min(self.keep, n)
        lv = 0
        for k in range(n, -1, -1):
            if self.width_at(k) <= avail:
                lv = k
                break
        if lv > cur:
            lv = max([k for k in range(cur + 1, lv + 1)
                      if self.width_at(k) + self.HYST <= avail] or [cur])
        keep = None if lv >= n else lv
        if keep != self.keep:
            self.keep = keep
            self.reflow()

    def folded_items(self):
        vis = self._shown()
        return [] if self.keep is None else vis[self.keep:]

    def reflow(self):
        vis = self._shown()
        kept = vis if self.keep is None else vis[:self.keep]
        for it in self._items:
            if it not in kept:
                try:
                    it.widget.grid_forget()
                except tk.TclError:
                    pass
        row = [it.widget for it in kept]
        if len(kept) < len(vis):
            row.append(self._more_btn())
        elif self._fold_btn is not None:
            self._fold_btn.grid_forget()
        for n, w in enumerate(row):
            try:
                w.grid(row=0, column=n, sticky="w",
                       padx=(0, self._hgap if n < len(row) - 1 else 0))
            except tk.TclError:
                pass
        self.grid_propagate(True)

    def fold_menu(self):
        """More's menu, built from the folded items as they are now: a
        toggle an entry on its own variable, a button a command; greyed
        with them."""
        m = tk.Menu(self, tearoff=0)
        for it in self.folded_items():
            w = it.widget
            kw = {"label": str(w.cget("text")),
                  "state": ("disabled" if w.instate(["disabled"])
                            else "normal")}
            if isinstance(w, ttk.Checkbutton):
                kw.update(variable=str(w.cget("variable")),
                          onvalue=w.cget("onvalue"),
                          offvalue=w.cget("offvalue"))
                cmd = str(w.cget("command"))
                if cmd:
                    kw["command"] = cmd
                m.add_checkbutton(**kw)
            else:
                m.add_command(command=w.invoke, **kw)
        return m

    def _fold_popup(self):
        b = self._fold_btn
        m = self.fold_menu()
        m.update_idletasks()
        try:
            m.tk_popup(b.winfo_rootx(), b.winfo_rooty() - m.winfo_reqheight())
        finally:
            m.grab_release()

    def drop(self, text):
        """Take the toggle labelled `text` out of this row (its var and the
        designer's own box stay)."""
        for it in list(self._items):
            try:
                lab = str(it.widget.cget("text"))
            except tk.TclError:
                continue
            if lab == text:
                self._items.remove(it)
                it.widget.destroy()
        self.reflow()


#: Help's index of g-code commands: (topic, a fixed example line OR an Insert
#: form kind with its fields, a note on the words). What each example does
#: is the engine's own words (`mew_gcode.explain`), and a form's example is
#: the engine's own spelling in the page's flavor (`make_command`), so the
#: index follows the Syntax menu.
GCODE_INDEX = (
    ("Straight move", "G1 X10 Y5 F600", None,
     "X, Y, Z: where to go, in mm. F: the speed, in mm per minute."),
    ("Travel move", "G0 X0 Y0", None, "The same words as G1."),
    ("Arc, clockwise", "G2 X10 Y0 I5 J0", None,
     "X, Y: where the arc ends. I, J: the arc's center, measured from "
     "where the arc starts."),
    ("Arc, counter-clockwise", "G3 X10 Y0 I5 J0", None,
     "X, Y: where the arc ends. I, J: the arc's center, measured from "
     "where the arc starts."),
    ("Dwell (wait in place)", None, ("dwell", {"seconds": 2}), ""),
    ("Set speed", None, ("set_speed", {"mm_s": 10}),
     "The example sets 10 mm/s."),
    ("Move to a place", None, ("move_to", {"x": 0, "y": 0, "z": 5,
                                           "mm_s": 10}), ""),
    ("Wait for temperature", None, ("wait_temp", {"celsius": 90}), ""),
    ("Set nozzle temperature", "M104 S90", None, "S: the temperature, °C."),
    ("Set bed temperature", "M140 S60", None, "S: the temperature, °C."),
    ("Message on the printer screen", None,
     ("message", {"text": "Layer 2"}), ""),
    ("Pause for the operator", None, ("pause", {}), ""),
    ("Set pressure", None, ("set_pressure", {}), ""),
    ("High voltage on / off", None, ("hv_on", {}), ""),
    ("Home the axes", "G28", None, ""),
    ("Absolute positions", "G90", None, ""),
    ("Relative positions", "G91", None, ""),
    ("Set the current position", "G92 X0 Y0", None, ""),
    ("Millimeters", "G21", None, ""),
)


# where a lab sets the commands no standard flavor has (pressure, high
# voltage): said once, in Help, so the menus stay short
_CUSTOM_HOWTO = ("Your lab's own command goes in your own flavor: Syntax > "
                 "Edit flavors…, Duplicate the flavor you use, type the "
                 "lines for pressure and high voltage, Save, then Use for "
                 "this g-code. Insert offers them from then on.")


def gcode_index(eng, flavor):
    """The help index in `flavor`: one dict per GCODE_INDEX row -- topic,
    example ("" when the flavor has none), what (the engine's words), note
    and kind (the Insert form, or None)."""
    lacks = getattr(eng, "_LACKS", {}).get(flavor, {})
    rows = []
    for topic, line, form, note in GCODE_INDEX:
        kind = None
        if form is not None:
            kind, fields = form
            try:
                line = "\n".join(eng.make_command(kind, flavor, **fields))
            except Exception as exc:  # noqa: BLE001 -- CommandUnavailable
                rows.append(dict(topic=topic, example="", what=str(exc),
                                 note=_CUSTOM_HOWTO if "your own flavor"
                                 in str(exc) else "", kind=None))
                continue
        first = line.split("\n")[0]
        what = eng.explain(first, flavor=flavor)
        if kind == "dwell":
            # the time's word and unit differ by flavor: read them back
            # from the engine's own 2-second line
            word = first.split()[-1] if len(first.split()) > 1 else ""
            if word[:1] in ("P", "S"):
                unit = "milliseconds" if word[1:] == "2000" else "seconds"
                note = f"{word[0]}: the time, in {unit}."
        why = lacks.get(first.split()[0].upper()) if first.split() else None
        if why:
            note = (note + " " if note else "") + why + "."
        rows.append(dict(topic=topic, example=line, what=what, note=note,
                         kind=kind))
    return rows


#: a ribbon group's gap on each side: as built, and compact (`RibbonFit`)
GROUP_PADX = 4
GROUP_PADX_COMPACT = 1


def ribbon_group(bar, title, last=False):
    """A captioned group in `bar` (packed left). Returns the frame its
    ToolButtons pack into. The group's box carries `_rg` (its title, row,
    caption and hairline) for `RibbonFit`."""
    box = ttk.Frame(bar, style="Bar.TFrame")
    box.pack(side="left", fill="y", padx=(GROUP_PADX, GROUP_PADX))
    row = ttk.Frame(box, style="Bar.TFrame")
    row.pack(side="top")
    cap = ttk.Label(box, text=title.upper(), style="BarHint.TLabel")
    cap.pack(side="bottom", pady=(0, 2))
    sep = None
    if not last:
        sep = ttk.Separator(bar, orient="vertical")
        sep.pack(side="left", fill="y", pady=6)
    box._rg = {"title": title, "row": row, "cap": cap, "sep": sep,
               "folded": False}
    return row


class RibbonFit:
    """Keeps the ribbon `bar` inside the width it is given, in steps, so it
    looks as built wherever it already fits:

      level 0   every tool at its built width (`_tool_width`), the groups'
                gaps as built;
      level 1   tight: every tool at its built width, only the groups'
                gaps tightened -- so a ribbon a few pixels too wide does
                not drop a whole step to the compact look;
      level 2   compact: each tool as wide as its own label (`_fit_width`),
                the groups' gaps tightened;
      level 2+k compact, and the k rightmost tools taken off the ribbon
                into one More button at its right end, whose menu lists
                them, each group's under its caption (`_popup`).

    It folds tool by tool, so a group wider than the room left stays on the
    ribbon as far as it fits. A group with no tool left goes with its hairline.

    The groups are the `ribbon_group` boxes PACKED in `bar` (nested frames
    are walked, so a set of groups packed out of the way -- the 3-axis set
    while the collector is a mandrel -- is not counted), in their packing
    order. A folded tool or group is pack-forgotten and packed back where it
    was (before the next sibling, in creation order, that is packed). The
    level is chosen from widths worked out from the tools'
    widths and the packing, never by trying a layout on screen, so a fit
    draws nothing it does not keep and needs no settled geometry: call
    `fit()` straight after a change that adds or swaps groups. It runs on
    every resize of `bar`; it widens back only with `HYST` pixels to spare,
    so the groups do not flicker at the edge while the window is dragged.

    Something that points at a tool or a group (the tutorial) asks
    `anchor(widget)`: the widget, or More while its group is folded."""

    HYST = 8

    def __init__(self, bar):
        self.bar = bar
        self.level = 0
        self._menu = None
        # More: a box like a group's, its button saying the name, the
        # caption's line kept empty so the ribbon is as tall as before
        self.more_box = ttk.Frame(bar, style="Bar.TFrame")
        self.more = ToolButton(
            self.more_box, "more", "More", menu=self._popup,
            tip="The tools the window is too narrow to show. Widen it to "
                "bring them back.")
        tk.Frame.configure(self.more, width=_fit_width(bar, "More", True))
        self.more.pack(side="top", padx=1, pady=(3, 0))
        cap = ttk.Label(self.more_box, text=" ", style="BarHint.TLabel")
        cap.pack(side="bottom", pady=(0, 2))
        bar.bind("<Configure>", lambda _e: self.fit(), add="+")

    # -- what is on the ribbon ---------------------------------------------- #
    def groups(self):
        """The groups' boxes in the packed containers, left to right,
        folded or not."""
        out = []

        def walk(w):
            for c in self._in_pack_order(w):
                if hasattr(c, "_rg"):
                    out.append(c)
                elif c.winfo_class() in ("TFrame", "Frame") and \
                        c is not self.more_box and \
                        not isinstance(c, ToolButton) and \
                        c.winfo_manager():
                    walk(c)
        walk(self.bar)
        return out

    def _in_pack_order(self, w):
        """`w`'s children left to right as packed, which is not the order
        they were made in: Calibrate's groups are made last and packed
        first (`_cal_ribbon`), and walking creation order folded them
        before the tools to their right. A child that is not packed (a
        folded group) sits where `_fold` would pack it back: before its
        next packed sibling in creation order."""
        kids = w.winfo_children()
        pos = {str(c): i for i, c in enumerate(w.pack_slaves())}

        def key(c):
            if str(c) in pos:
                return (pos[str(c)], 0, 0)
            nxt = self._next_packed(c)
            at = pos.get(str(nxt), len(pos)) if nxt is not None else len(pos)
            return (at, -1, kids.index(c))
        return sorted(kids, key=key)

    @staticmethod
    def tools(box):
        """`box`'s tools, left to right, those in More among them."""
        return [t for t in box._rg["row"].winfo_children()
                if isinstance(t, ToolButton)
                and (t.winfo_manager() or t._fold_info is not None)]

    def folded(self):
        """The groups with a tool in More."""
        return [b for b in self.groups()
                if any(t._fold_info is not None for t in self.tools(b))]

    def anchor(self, w):
        """Where `w` (a tool, or a group's box) is on screen: itself, or
        More while its group is folded."""
        x = w
        while x is not None and x is not self.bar:
            if getattr(x, "_fold_info", None) is not None:
                return self.more
            rg = getattr(x, "_rg", None)
            if rg is not None:
                return self.more if rg["folded"] else w
            x = x.master
        return w

    # -- widths ------------------------------------------------------------- #
    @staticmethod
    def _ints(w, v):
        """A Tk pad or padding value (an int, a tuple, a Tcl list) as ints."""
        if isinstance(v, (int, float)):
            return [int(v)]
        if not isinstance(v, (tuple, list)):
            v = w.tk.splitlist(str(v))
        return [int(float(str(x))) for x in v]

    def _padx(self, w):
        memo = self._memo
        if memo is not None:
            hit = memo.get(("padx", w))
            if hit is None:
                hit = memo[("padx", w)] = self._padx_now(w)
            return hit
        return self._padx_now(w)

    def _padx_now(self, w):
        try:
            info = getattr(w, "_fold_info", None) or w.pack_info()
            p = self._ints(w, info.get("padx", 0)) or [0]
        except tk.TclError:
            return 0
        return p[0] + p[-1] if len(p) > 1 else 2 * p[0]

    def _tool_w(self, t, compact):
        if not compact:
            return t._w_full
        memo = self._memo
        if memo is not None:
            hit = memo.get(("tool", t))
            if hit is None:
                hit = memo[("tool", t)] = _fit_width(
                    t, t.cget("text"), t._menu is not None)
            return hit
        return _fit_width(t, t.cget("text"), t._menu is not None)

    def _box_w(self, box, compact, shown=None, tight=False):
        """`box`'s packed width with its gaps and its hairline, holding
        the tools `shown` (all of them when None). `tight`: the compact
        gaps with the tools at their built width."""
        rg = box._rg
        inner = max(sum(self._tool_w(t, compact) + self._padx(t)
                        for t in (self.tools(box) if shown is None else shown)),
                    rg["cap"].winfo_reqwidth())
        sep = rg["sep"].winfo_reqwidth() if rg["sep"] is not None else 0
        return inner + sep + 2 * (GROUP_PADX_COMPACT if compact or tight
                                  else GROUP_PADX)

    def _more_w(self):
        return (self.more.winfo_reqwidth() + 2
                + 2 * GROUP_PADX_COMPACT)

    def _rest(self, groups):
        """Everything on the ribbon that is not a group, a group's hairline
        or More: its padding, any other widget, packed left to right."""
        def pad_lr(w):
            try:
                p = self._ints(w, w.cget("padding"))
            except tk.TclError:
                return 0
            if not p:
                return 0
            return p[0] + (p[2] if len(p) > 2 else p[0])

        skip = {str(self.more_box)}
        for b in groups:
            skip.add(str(b))
            if b._rg["sep"] is not None:
                skip.add(str(b._rg["sep"]))

        def walk(w):
            n = pad_lr(w)
            for c in w.pack_slaves():
                if str(c) in skip:
                    continue
                if c.winfo_class() in ("TFrame", "Frame") and \
                        not isinstance(c, ToolButton):
                    n += walk(c) + self._padx(c)
                else:
                    n += c.winfo_reqwidth() + self._padx(c)
            return n
        return walk(self.bar)

    #: set for the length of one `fit()` or `widths()`: each tool's measured width and
    #: each widget's packed gaps, which the levels ask for again and again
    #: (one `font.measure` per tool per level). Every Tk call on this thread
    #: waits for the GIL behind the preview worker, so the repeats held the
    #: window 0.4 s on every design edit. Nothing it reads
    #: changes inside the call.
    _memo = None

    def widths(self):
        """The ribbon's width at each level: [level 0, level 1, ...]."""
        with self._measuring():
            return self._widths()

    @contextlib.contextmanager
    def _measuring(self):
        """`_memo` held for the length of the block (the outermost one)."""
        if self._memo is not None:
            yield
            return
        self._memo = {}
        try:
            yield
        finally:
            self._memo = None

    def _widths(self):
        g = self.groups()
        rest = self._rest(g)
        shown = {b: self.tools(b) for b in g}
        comp = {b: self._box_w(b, True) for b in g}
        out = [rest + sum(self._box_w(b, False) for b in g),
               rest + sum(self._box_w(b, False, tight=True) for b in g),
               rest + sum(comp.values())]
        gone = set()
        for box in reversed(g):
            for _t in tuple(shown[box])[::-1]:
                shown[box].pop()
                comp[box] = self._box_w(box, True, shown[box]) \
                    if shown[box] else 0
                if not shown[box]:
                    gone.add(box)
                out.append(rest + sum(comp.values()) + self._more_w()
                           + self._emptied(g, gone))
        return out

    def _emptied(self, g, fold):
        """The containers (not `bar`) every packed child of which is a
        group in `fold`: each is left 1 px wide (`_release`)."""
        n = 0
        for c in {b.master for b in g if b.master is not self.bar}:
            mine = {str(b) for b in g if b.master is c}
            mine |= {str(b._rg["sep"]) for b in g
                     if b.master is c and b._rg["sep"] is not None}
            others = [w for w in c.pack_slaves() if str(w) not in mine]
            if not others and all(b in fold for b in g if b.master is c):
                n += 1
        return n

    def _release(self, g):
        """A frame whose last packed child leaves keeps the size it had
        (Tk's packer does not shrink an empty container; width=0 does not
        release it, width=1 does): set an emptied container to 1 px."""
        for c in {b.master for b in g if b.master is not self.bar}:
            if not c.pack_slaves():
                c.configure(width=1)

    # -- choosing and applying a level -------------------------------------- #
    def fit(self):
        try:
            avail = self.bar.winfo_width()
        except tk.TclError:
            return
        if avail <= 1:              # not laid out yet: <Configure> comes
            return
        with self._measuring():
            self._fit_to(avail)

    def _fit_to(self, avail):
        w = self.widths()
        cur = min(self.level, len(w) - 1)
        if w[cur] > avail:          # too wide: the first level that fits
            lv = next((i for i, x in enumerate(w) if x <= avail), len(w) - 1)
        else:                       # fits: widen back only with room
            lv = next((i for i, x in enumerate(w[:cur])
                       if x + self.HYST <= avail), cur)
        self.apply(lv)

    def apply(self, level):
        """Lay the ribbon out at `level` (see the class docstring)."""
        g = self.groups()
        tools = [t for b in g for t in self.tools(b)]
        level = max(0, min(level, len(tools) + 2))
        self.level = level
        compact = level >= 2
        fold = set(tools[len(tools) - (level - 2):]) if level >= 3 else set()
        gap = GROUP_PADX_COMPACT if level >= 1 else GROUP_PADX
        for box in g:
            mine = self.tools(box)
            for t in mine:
                t._fit = self
                want = self._tool_w(t, compact)
                if int(t.cget("width")) != want:
                    tk.Frame.configure(t, width=want)
                self._fold_tool(t, t in fold)
            self._fold(box, bool(mine) and all(t in fold for t in mine))
            if not box._rg["folded"] and self._padx(box) != 2 * gap:
                box.pack_configure(padx=(gap, gap))
        self._release(g)
        if fold and not self.more_box.winfo_manager():
            self.more_box.pack(side="left", fill="y",
                               padx=(GROUP_PADX_COMPACT, GROUP_PADX_COMPACT))
        elif not fold and self.more_box.winfo_manager():
            self.more_box.pack_forget()

    def _fold_tool(self, t, on):
        if (t._fold_info is not None) == on:
            return
        if on:
            t._fold_info = t.pack_info()
            t.pack_forget()
            return
        info, t._fold_info = t._fold_info, None
        info.pop("in", None)
        nxt = self._next_packed(t)
        if nxt is not None:
            info["before"] = nxt
        t.pack(**info)

    def _fold(self, box, on):
        rg = box._rg
        if rg["folded"] == on:
            return
        rg["folded"] = on
        parts = [box] + ([rg["sep"]] if rg["sep"] is not None else [])
        if on:
            rg["pack"] = [w.pack_info() for w in parts]
            for w in parts:
                w.pack_forget()
            return
        for w, info in zip(parts, rg.pop("pack")):
            info.pop("in", None)
            nxt = self._next_packed(w)
            if nxt is not None:
                info["before"] = nxt
            w.pack(**info)

    def _next_packed(self, w):
        """The first sibling created after `w` that is packed in its
        parent: where `w` goes back in."""
        sib = w.master.winfo_children()
        for c in sib[sib.index(w) + 1:]:
            try:
                if c.winfo_manager() == "pack" and c.master is w.master:
                    return c
            except tk.TclError:
                pass
        return None

    def _popup(self):
        """The folded groups' tools, each group under its caption, as a
        menu built at the click, so each entry reads the tool's state now:
        greyed when the tool is, ticked when it is pressed. A tool with a
        menu of its own posts it under More."""
        if self._menu is not None:
            try:
                self._menu.destroy()
            except tk.TclError:
                pass
        m = self._menu = tk.Menu(self.bar, tearoff=False)
        m._keep = []
        for n, box in enumerate(self.folded()):
            if n:
                m.add_separator()
            m.add_command(label=box._rg["title"].upper(), state="disabled")
            for t in self.tools(box):
                if t._fold_info is None:
                    continue
                label = " ".join(t.cget("text").split())
                state = "normal" if t._enabled else "disabled"
                img = dict(image=t._photo, compound="left") \
                    if t._photo is not None else {}
                if t._menu is not None:
                    # its icon carries the menu's caret, as on the ribbon
                    m.add_command(label=label, state=state,
                                  command=lambda t=t: self.bar.after_idle(
                                      lambda: t.invoke(at=self.more)),
                                  **img)
                elif t._on:
                    v = tk.BooleanVar(master=m, value=True)
                    m._keep.append(v)
                    m.add_checkbutton(label=label, variable=v, state=state,
                                      command=t.invoke, **img)
                else:
                    m.add_command(label=label, state=state,
                                  command=t.invoke, **img)
        return m
