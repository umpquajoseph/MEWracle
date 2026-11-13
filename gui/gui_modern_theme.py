#!/usr/bin/env python3
"""gui_modern_theme.py -- MODERN-UI ONLY: the modern shell's light + dark palettes
and ttk styles, applied BY WIDGET CLASS exactly like the gui_theme engine, which
this reuses and does not modify.

_ModernThemeMixin sits ahead of _ThemeMixin in ModernApp's MRO and overrides only
`_apply_theme`; the toggle widget, the dark_mode var and the gui_prefs.json
persistence are inherited unchanged, so the preference file is shared between the
modern and classic UIs.

Both modes use the clam base theme so the palette is fully honoured; the classic
UI keeps native vista in light mode.
"""

from gui_common import *

import gui_fonts
import gui_tips
import gui_widgets
from gui_theme import DARK as _CLASSIC_DARK

# -- palettes (gui_theme key set + the shell's extra tokens) ------------------ #
LIGHT = {
    "bg":        "#f6f7f9",   # window / page background
    "field":     "#ffffff",
    "fg":        "#1c1e21",
    "disabled":  "#9aa0a6",
    "select_bg": "#2f6fd6",   # the accent (selection, focus, selected nav row)
    "select_fg": "#ffffff",
    "border":    "#d9dce1",
    "active":    "#e9edf2",
    "accent":    "#e8eef7",   # preset-grid group rows (same as classic light)
    "tab_bg":    "#e9edf2",
    # shell extras
    "card":      "#ffffff",
    "sidebar":   "#eef0f4",
    "hint":      "#646a71",   # 4.6:1 or better on every light ground (WCAG AA)
    "hover":     "#eef1f5",   # flat-control hover wash (rows, disclosures)
    "hairline":  "#d9dce1",   # 1-px typographic dividers (== border today;
                              # kept separate so hairlines can go lighter later)
    # setup statuses (machine_setup): always drawn WITH a symbol and a word, so
    # colour is never the only carrier
    "st_ok":     "#2e7d32",
    "st_warn":   "#9a5b00",
    "st_err":    "#c0392b",
    "st_info":   "#2f6fd6",
    # the Ctrl+F finder's marker: a highlighter yellow, deliberately NOT
    # select_bg -- the accent is also the focus colour, so a ring in it would
    # make the control the finder landed on look merely focused.
    "find_hl":   "#f2c200",
}

DARK = dict(_CLASSIC_DARK)
DARK.update({
    "bg":        "#232527",   # main display: the old sidebar tone (darker)
    "select_bg": "#3d6fc4",
    "card":      "#333639",
    "sidebar":   "#1b1d1f",   # sidebar: a step darker than the new main display
    "hint":      "#9a9fa4",
    "hover":     "#2b2e31",
    "hairline":  "#3c4045",
    "st_ok":     "#7cc47f",
    "st_warn":   "#e8b04e",
    "st_err":    "#f07a70",
    "st_info":   "#7aa9f5",
    "find_hl":   "#f5c84b",
})


def _shade(hex_color, factor):
    """Darken (<1) or lighten (>1) a #rrggbb colour; used for the pressed state
    of the filled accent button so no second accent enters the palette."""
    r = max(0, min(255, int(int(hex_color[1:3], 16) * factor)))
    g = max(0, min(255, int(int(hex_color[3:5], 16) * factor)))
    b = max(0, min(255, int(int(hex_color[5:7], 16) * factor)))
    return f"#{r:02x}{g:02x}{b:02x}"


#: the check box and radio images, made once and repainted in place on a theme
#: change (a ttk image element cannot be re-created inside one theme)
_INDICATORS = {}

# (state spec, key) in match order: ttk takes the first spec that holds
_CHECK_STATES = (("disabled selected", "on_dis"), ("disabled", "off_dis"),
                 ("pressed selected", "on_hot"), ("active selected", "on_hot"),
                 ("selected", "on"), ("pressed", "off_hot"),
                 ("active", "off_hot"))


def _indicator_pixels(kind, key, pal, side, gap=0):
    """One indicator as a PIL image: drawn at four times its size and scaled
    down, as the tree's eyes are (gui_gcode_cad.eye_images), so the corners
    and the tick are smooth at every display scale."""
    from PIL import Image, ImageDraw

    def rgb(h):
        return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5)) + (255,)
    accent, field = pal["select_bg"], pal["field"]
    light = int(pal["bg"][1:3], 16) > 128
    edge = _shade(pal["border"], 0.80 if light else 1.35)
    on = key.startswith("on")
    if key.endswith("_dis"):
        fill = rgb(pal["disabled"]) if on else rgb(pal["bg"])
        ring = rgb(pal["disabled"]) if on else rgb(pal["border"])
    elif on:
        shade = _shade(accent, 0.90) if key == "on_hot" else accent
        fill = ring = rgb(shade)
    else:
        fill, ring = rgb(field), rgb(accent if key == "off_hot" else edge)
    S = 4
    n = side * S
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    w = max(S, round(n / 12))                       # a 1-px ring at 96 dpi
    if kind == "check":
        r = round(n * 0.18)
        d.rounded_rectangle((0, 0, n - 1, n - 1), radius=r, fill=ring)
        d.rounded_rectangle((w, w, n - 1 - w, n - 1 - w),
                            radius=max(1, r - w), fill=fill)
        if on:
            mark = rgb(pal["select_fg"])
            d.line([(n * 0.24, n * 0.52), (n * 0.42, n * 0.70),
                    (n * 0.76, n * 0.32)], fill=mark,
                   width=max(S, round(n * 0.13)), joint="curve")
    else:
        d.ellipse((0, 0, n - 1, n - 1), fill=ring)
        d.ellipse((w, w, n - 1 - w, n - 1 - w),
                  fill=fill if not on else rgb(field))
        if on:
            c = n * 0.27
            d.ellipse((c, c, n - 1 - c, n - 1 - c), fill=ring)
    im = im.resize((side, side), Image.LANCZOS)
    if gap:                     # clear pixels to the right: the label's gap
        wide = Image.new("RGBA", (side + gap, side), (0, 0, 0, 0))
        wide.paste(im, (0, 0))
        im = wide
    return im


def _style_indicators(style, root, pal):
    """Check boxes and radio buttons in the app's own drawing: an accent box
    with a white tick, an accent ring with a dot. Clam's own indicator is a
    black cross in a grey square, which read as a default nobody styled."""
    try:
        from PIL import ImageTk
    except ImportError:                    # keep clam's indicator
        return
    side = gui_fonts.dpi_px(root, 14)
    # the space between box and label rides in the image: an image
    # element's own padding is not honoured inside this layout
    gap = gui_fonts.dpi_px(root, 6)
    keys = ("off", "off_hot", "off_dis", "on", "on_hot", "on_dis")
    for kind in ("check", "radio"):
        for key in keys:
            px = _indicator_pixels(kind, key, pal, side, gap)
            img = _INDICATORS.get((kind, key))
            if img is None or img.width() != side + gap:
                _INDICATORS[(kind, key)] = ImageTk.PhotoImage(px, master=root)
            else:
                img.paste(px)
    for kind, cls in (("check", "Checkbutton"), ("radio", "Radiobutton")):
        name = "Mew%s.indicator" % cls
        try:
            style.element_create(
                name, "image", _INDICATORS[(kind, "off")],
                *[(spec, _INDICATORS[(kind, k)]) for spec, k in _CHECK_STATES],
                sticky="")
        except tk.TclError:
            pass                           # made once per theme
        style.layout("T" + cls, [
            (cls + ".padding", {"sticky": "nswe", "children": [
                (name, {"side": "left", "sticky": ""}),
                (cls + ".focus", {"side": "left", "sticky": "w", "children": [
                    (cls + ".label", {"sticky": "nswe"})]})]})])


def _menu_options(root, pal):
    """The look every tk.Menu takes: rows padded to a comfortable height
    (Tk's own rows were the font's line and nothing more, so a menu read as
    a cramped list), and a quiet hover wash instead of the full accent."""
    return {"background": pal["field"], "foreground": pal["fg"],
            "activebackground": pal["active"], "activeforeground": pal["fg"],
            "disabledforeground": pal["disabled"],
            "selectcolor": pal["select_bg"],
            "activeborderwidth": gui_fonts.dpi_px(root, 5),
            "borderwidth": 0, "relief": "flat", "font": "MewBody"}


def _style_menus(root, style, pal):
    """Menus and the dropdown lists of comboboxes. The option database covers
    the ones made later; the ones that exist already are walked and set."""
    opts = _menu_options(root, pal)
    names = {"background": "background", "foreground": "foreground",
             "activebackground": "activeBackground",
             "activeforeground": "activeForeground",
             "disabledforeground": "disabledForeground",
             "selectcolor": "selectColor",
             "activeborderwidth": "activeBorderWidth",
             "borderwidth": "borderWidth", "relief": "relief", "font": "font"}
    for key, val in opts.items():
        try:
            root.option_add("*Menu." + names[key], val)
        except tk.TclError:
            pass
    lists = {"font": "MewBody", "background": pal["field"],
             "foreground": pal["fg"], "selectbackground": pal["select_bg"],
             "selectforeground": pal["select_fg"]}
    # walked in Tcl, so the menus and dropdown lists Tcl made itself (a
    # combobox's list, a menu's clone) are reached as well
    todo = [str(root)]
    while todo:
        path = todo.pop()
        try:
            kids = root.tk.splitlist(root.tk.call("winfo", "children", path))
        except tk.TclError:
            continue
        for k in kids:
            try:
                cls = root.tk.call("winfo", "class", k)
                if cls == "Menu":
                    root.tk.call(k, "configure",
                                 *[x for kv in opts.items()
                                   for x in ("-" + kv[0], kv[1])])
                elif cls == "Listbox" and ".popdown." in str(k):
                    root.tk.call(k, "configure",
                                 *[x for kv in lists.items()
                                   for x in ("-" + kv[0], kv[1])])
            except tk.TclError:
                pass
            todo.append(k)

    # a combobox's open list: the hairline edge of a field, not a black frame,
    # no inner bevel, and the body font
    style.configure("ComboboxPopdownFrame", relief="solid", borderwidth=1,
                    bordercolor=pal["border"], background=pal["field"])
    for key, val in (("*TCombobox*Listbox.font", "MewBody"),
                     ("*TCombobox*Listbox.borderWidth", 0),
                     ("*TCombobox*Listbox.highlightThickness", 0),
                     ("*TCombobox*Listbox.selectBorderWidth", 0),
                     ("*TCombobox*Listbox.activeStyle", "none"),
                     ("*TCombobox*Listbox.background", pal["field"]),
                     ("*TCombobox*Listbox.foreground", pal["fg"]),
                     ("*TCombobox*Listbox.selectBackground", pal["select_bg"]),
                     ("*TCombobox*Listbox.selectForeground", pal["select_fg"])):
        try:
            root.option_add(key, val)
        except tk.TclError:
            pass


def _chevron_pixels(direction, colour, w, h):
    """A thin chevron centred in a clear w x h image (drawn at 4x)."""
    from PIL import Image, ImageDraw
    S = 4
    im = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    rgb = tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5)) + (255,)
    cx, cy = w * S / 2, h * S / 2
    hw = min(w, h * 2) * S * 0.20          # half-width of the chevron
    hh = hw * 0.55
    if direction == "down":
        pts = [(cx - hw, cy - hh), (cx, cy + hh), (cx + hw, cy - hh)]
    else:
        pts = [(cx - hw, cy + hh), (cx, cy - hh), (cx + hw, cy + hh)]
    ImageDraw.Draw(im).line(pts, fill=rgb, width=max(S, round(S * 1.6)),
                            joint="curve")
    return im.resize((w, h), Image.LANCZOS)


def _style_arrows(style, root, pal):
    """Dropdown and spinbox arrows as thin chevrons on the field's own ground.
    Clam drew a heavy black triangle in a bordered grey button, the one part
    of a field that still looked like a default."""
    try:
        from PIL import ImageTk
    except ImportError:
        return
    shapes = {"combo": ("down", gui_fonts.dpi_px(root, 22),
                        gui_fonts.dpi_px(root, 14)),
              "up": ("up", gui_fonts.dpi_px(root, 18), gui_fonts.dpi_px(root, 9)),
              "down": ("down", gui_fonts.dpi_px(root, 18),
                       gui_fonts.dpi_px(root, 9))}
    tones = {"": pal["hint"], "hot": pal["fg"], "dis": pal["disabled"]}
    for shape, (direction, w, h) in shapes.items():
        for tone, colour in tones.items():
            px = _chevron_pixels(direction, colour, w, h)
            img = _INDICATORS.get((shape, tone))
            if img is None or img.width() != w or img.height() != h:
                _INDICATORS[(shape, tone)] = ImageTk.PhotoImage(px, master=root)
            else:
                img.paste(px)

    def element(name, shape):
        try:
            style.element_create(
                name, "image", _INDICATORS[(shape, "")],
                ("disabled", _INDICATORS[(shape, "dis")]),
                ("pressed", _INDICATORS[(shape, "hot")]),
                ("active", _INDICATORS[(shape, "hot")]), sticky="")
        except tk.TclError:
            pass                           # made once per theme
    element("MewCombobox.downarrow", "combo")
    element("MewSpinbox.uparrow", "up")
    element("MewSpinbox.downarrow", "down")
    style.layout("TCombobox", [
        ("Combobox.field", {"sticky": "nswe", "children": [
            ("MewCombobox.downarrow", {"side": "right", "sticky": "ns"}),
            ("Combobox.padding", {"expand": "1", "sticky": "nswe", "children": [
                ("Combobox.textarea", {"sticky": "nswe"})]})]})])
    style.layout("TSpinbox", [
        ("Spinbox.field", {"side": "top", "sticky": "we", "children": [
            ("null", {"side": "right", "sticky": "", "children": [
                ("MewSpinbox.uparrow", {"side": "top", "sticky": "e"}),
                ("MewSpinbox.downarrow", {"side": "bottom", "sticky": "e"})]}),
            ("Spinbox.padding", {"sticky": "nswe", "children": [
                ("Spinbox.textarea", {"sticky": "nswe"})]})]})])


def _style_text_boxes(root, pal):
    """Plain tk.Text boxes (the printer's start and end g-code, the header
    preview, My library's script, notes) in the look of a field: a hairline
    edge that takes the accent on focus, the palette's colours, and the app's
    mono face instead of Tk's Courier. Tk's own box was a sunken black frame
    around Courier, the one input on a page that matched nothing beside it.

    A box its author coloured or framed on purpose (the Log, the G-code
    editor, a dark console) is left as it is: only a box still on Tk's
    defaults, or on a colour this theme set, is touched."""
    ours = {"systemwindow", "white", "#ffffff", LIGHT["field"].lower(),
            DARK["field"].lower()}
    edge, focus = pal["border"], pal["select_bg"]
    # the boxes made later (a dialog's notes, a panel built on first open)
    # are born with it; constructor arguments still win over these
    for key, val in (("*Text.background", pal["field"]),
                     ("*Text.foreground", pal["fg"]),
                     ("*Text.insertBackground", pal["fg"]),
                     ("*Text.selectBackground", pal["select_bg"]),
                     ("*Text.selectForeground", pal["select_fg"]),
                     ("*Text.relief", "flat"), ("*Text.borderWidth", 0),
                     ("*Text.highlightThickness", 1),
                     ("*Text.highlightBackground", edge),
                     ("*Text.highlightColor", focus),
                     ("*Text.font", "MewMono")):
        try:
            root.option_add(key, val)
        except tk.TclError:
            pass
    todo = [root]
    while todo:
        w = todo.pop()
        try:
            kids = w.winfo_children()
        except tk.TclError:
            continue
        todo.extend(kids)
        if not isinstance(w, tk.Text) or getattr(w, "_mew_text_skip", False):
            continue
        try:
            if str(w.cget("font")) in ("TkFixedFont", ""):
                w.configure(font="MewMono")
            if str(w.cget("background")).lower() not in ours:
                continue
            w.configure(background=pal["field"], foreground=pal["fg"],
                        insertbackground=pal["fg"],
                        selectbackground=pal["select_bg"],
                        selectforeground=pal["select_fg"])
            # the frame only where it is still Tk's sunken one (or ours
            # from an earlier pass): a box drawn flat on purpose stays flat
            if getattr(w, "_mew_text_framed", False) or                     str(w.cget("relief")) == "sunken":
                w.configure(relief="flat", borderwidth=0,
                            highlightthickness=1, highlightbackground=edge,
                            highlightcolor=focus,
                            padx=gui_fonts.dpi_px(root, 4),
                            pady=gui_fonts.dpi_px(root, 3))
                w._mew_text_framed = True
        except tk.TclError:
            pass


#: whether new windows open with a dark title bar (the live theme)
_TITLE_DARK = [False]


def _title_bar(win, dark):
    """Ask Windows for a dark or light title bar on `win`. Windows draws the
    bar, not Tk, so in dark mode the app sat under a white strip. A no-op off
    Windows and on a Windows too old to know the attribute."""
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            hwnd = int(win.wm_frame(), 16)
        except (tk.TclError, ValueError):
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        on = ctypes.c_int(1 if dark else 0)
        # DWMWA_USE_IMMERSIVE_DARK_MODE is 20; Windows 10 before 20H1 used 19
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
    except Exception:  # noqa: BLE001 -- a title bar is never worth a crash
        pass


def _style_title_bars(root, dark):
    """Every open window's title bar follows the theme, and every window
    opened later (a dialog, Settings' own window) is born matching it."""
    _TITLE_DARK[0] = dark
    if not getattr(root, "_mew_title_hook", False):
        root.bind_class("Toplevel", "<Map>", lambda e: _title_bar(
            e.widget, _TITLE_DARK[0]), add="+")
        root._mew_title_hook = True
    todo = [root]
    while todo:
        w = todo.pop()
        try:
            kids = w.winfo_children()
        except tk.TclError:
            continue
        todo.extend(k for k in kids if isinstance(k, tk.Toplevel))
        _title_bar(w, dark)


class _ModernThemeMixin:
    def _modern_palette(self):
        dark = bool(getattr(self, "dark_mode", None) and self.dark_mode.get())
        return DARK if dark else LIGHT

    def _apply_theme(self):
        """Override _ThemeMixin._apply_theme: clam in BOTH modes, the modern
        palettes, the Helvetica ramp on every ttk class, plus the shell's own
        styles (sidebar / action bar / cards / hidden top-level tab bar).
        Reuses the inherited by-class engine pieces (_style_clam,
        _set_classic_options, _recolor_classic, _theme_preset_table)."""
        pal = self._modern_palette()
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self._style_clam(style, pal)       # inherited from _ThemeMixin (static)
        self._style_modern(style, pal)
        _style_indicators(style, self.root, pal)
        _style_arrows(style, self.root, pal)
        try:
            self.root.configure(background=pal["bg"])
        except tk.TclError:
            pass
        self._set_classic_options(pal)     # future classic widgets
        self._recolor_classic(self.root, pal)
        _style_menus(self.root, style, pal)
        _style_text_boxes(self.root, pal)
        _style_title_bars(self.root, pal is DARK)
        # the G-code window's text pane follows the theme like the Log
        gce = getattr(self, "gce", None)
        if gce is not None and hasattr(gce, "retheme"):
            gce.retheme()
        # the Log page's text: the theme's field, not a black terminal slab
        # that outweighed every other surface of a light window
        log = getattr(self, "log", None)
        if log is not None:
            try:
                log.configure(background=pal["field"], foreground=pal["fg"],
                              insertbackground=pal["fg"],
                              selectbackground=pal["select_bg"],
                              selectforeground=pal["select_fg"],
                              borderwidth=0, highlightthickness=1,
                              highlightbackground=pal["border"],
                              highlightcolor=pal["border"],
                              padx=gui_fonts.dpi_px(self.root, 8),
                              pady=gui_fonts.dpi_px(self.root, 6))
                log.tag_configure("empty", foreground=pal["hint"])
            except tk.TclError:
                pass
        self._theme_preset_table(pal)
        gui_tips.set_tip_palette(pal["card"], pal["fg"], pal["border"])
        gui_widgets.set_palette(pal)       # retheme the composite primitives

    @staticmethod
    def _style_modern(style, p):
        """The shell-specific styles, on top of _style_clam's class-wide pass.
        All type references are NAMED fonts (gui_fonts), so a font change never
        touches this file."""
        bg, fg, hint = p["bg"], p["fg"], p["hint"]
        card, sidebar = p["card"], p["sidebar"]
        sel_bg, sel_fg = p["select_bg"], p["select_fg"]
        border, active = p["border"], p["active"]
        hover = p.get("hover", active)
        hairline = p.get("hairline", border)
        field = p["field"]
        disabled = p["disabled"]

        # the whole ttk world takes the ramp's body font
        style.configure(".", font="MewBody")
        style.configure("TNotebook.Tab", font="MewBody", padding=(10, 4))

        # -- class-wide FLATTENING (on top of _style_clam): every reused
        #    builder inherits the typographic look with zero builder edits ---- #

        # fields: white surface, 1-px hairline, accent ring on focus
        for cls in ("TEntry", "TSpinbox", "TCombobox"):
            style.configure(cls, fieldbackground=field, background=field,
                            bordercolor=hairline, lightcolor=field,
                            darkcolor=field, padding=(6, 3))
            style.map(cls,
                      bordercolor=[("focus", sel_bg)],
                      lightcolor=[("focus", field)],
                      darkcolor=[("focus", field)])

        # secondary buttons: quiet card surface, hairline edge, hover wash
        # (re-maps _style_clam's accent-on-hover, which is too loud here)
        style.configure("TButton", font="MewBody", padding=(10, 4),
                        background=card, foreground=fg, borderwidth=1,
                        bordercolor=hairline, relief="flat", focuscolor=card)
        style.map("TButton",
                  background=[("disabled", bg), ("pressed", active),
                              ("active", hover)],
                  foreground=[("disabled", disabled)],
                  bordercolor=[("pressed", border), ("active", border)])

        # LabelFrames: boxes -> typographic groups (title keeps MewSection)
        style.configure("TLabelframe", borderwidth=0, relief="flat",
                        background=bg)
        style.configure("TLabelframe.Label", font="MewSection", background=bg,
                        foreground=fg)

        # tables: taller rows, quiet flat headings. Row heights scale with the
        # screen and never fall below the text (gui_fonts.row_height): fixed
        # pixels clipped every row once the text grew past them
        style.configure("Treeview",
                        rowheight=gui_fonts.row_height(style.master, 26,
                                                       "MewBody"),
                        background=field,
                        fieldbackground=field, borderwidth=0)
        style.configure("Treeview.Heading", font="MewBody", background=bg,
                        foreground=hint, relief="flat", borderwidth=0,
                        padding=(6, 4))
        style.map("Treeview.Heading", background=[("active", hover)],
                  foreground=[("active", fg)])
        # the G-code page's tree: bars over the picture, a step off its ground
        style.configure("GceTree.Treeview",
                        rowheight=gui_fonts.row_height(style.master, 24,
                                                       "MewBody"),
                        background=active,
                        fieldbackground=active)
        style.configure("GceBar.TFrame", background=active)
        style.configure("GceBar.Link.TButton", background=active)
        style.configure("GceBar.SectionHeader.TLabel", background=active)
        style.configure("GceBar.Hint.TLabel", background=active)

        # kill the hover box on check/radio rows (keep the accent indicator)
        for cls in ("TCheckbutton", "TRadiobutton"):
            style.map(cls, background=[("active", bg)])

        # the top-level notebook: pages only, tab bar hidden (the shell's top
        # row IS the tab strip). Emptying the Tab layout collapses the bar entirely; the
        # fallback shrinks it to nothing if a Tk build rejects an empty layout.
        style.configure("Shell.TNotebook", background=bg, borderwidth=0)
        try:
            style.layout("Shell.TNotebook.Tab", [])
        except tk.TclError:
            style.configure("Shell.TNotebook.Tab", padding=0)

        # text roles
        style.configure("Title.TLabel", font="MewTitle", background=bg,
                        foreground=fg)
        style.configure("Section.TLabel", font="MewSection", background=bg,
                        foreground=fg)
        style.configure("Hint.TLabel", font="MewHint", background=bg,
                        foreground=hint)
        # section overline (gui_widgets.section_header adds the letterspacing)
        style.configure("SectionHeader.TLabel", font="MewOverline",
                        background=bg, foreground=hint)
        # 1-px typographic divider (a height=1 frame)
        style.configure("Hairline.TFrame", background=hairline)

        # scaffold pill rail (a selector, not tabs)
        style.configure("Pill.TButton", font="MewNav", padding=(10, 3),
                        background=card, foreground=fg, borderwidth=1,
                        bordercolor=hairline, relief="flat", focuscolor=card)
        style.map("Pill.TButton",
                  background=[("pressed", active), ("active", hover)],
                  foreground=[("disabled", disabled)])
        style.configure("PillSel.TButton", font="MewNav", padding=(10, 3),
                        background=sel_bg, foreground=sel_fg, borderwidth=1,
                        bordercolor=sel_bg, relief="flat", focuscolor=sel_bg)
        style.map("PillSel.TButton",
                  background=[("pressed", sel_bg), ("active", sel_bg)],
                  foreground=[("pressed", sel_fg), ("active", sel_fg)])

        # a button that reads as text: flat on the page ground, accent-coloured,
        # no border and no focus box. The preview panel's "Options ▸" drawer
        # toggle (gui_preview.py) and Home's "Take the tour" link (gui_home.py)
        # use it -- the ModuleGrid's back
        # control was one until it was made an ordinary button on purpose, so
        # do not re-point it here without a reason.
        style.configure("Link.TButton", font="MewBody", padding=(0, 2),
                        background=bg, foreground=sel_bg, borderwidth=0,
                        relief="flat", focuscolor=bg, anchor="w")
        style.map("Link.TButton",
                  background=[("pressed", bg), ("active", bg)],
                  foreground=[("disabled", disabled)])

        # sidebar
        style.configure("Sidebar.TFrame", background=sidebar)
        style.configure("Wordmark.TLabel", font="MewTitle", background=sidebar,
                        foreground=fg)
        style.configure("SidebarSection.TLabel", font="MewNavSection",
                        background=sidebar, foreground=hint)
        style.configure("SidebarHint.TLabel", font="MewHint",
                        background=sidebar, foreground=hint)
        style.configure("Nav.TButton", font="MewNav", anchor="w",
                        padding=(12, 6), background=sidebar, foreground=fg,
                        borderwidth=0, relief="flat", focuscolor=sidebar)
        style.map("Nav.TButton",
                  background=[("pressed", active), ("active", active)],
                  foreground=[("disabled", p["disabled"])])
        style.configure("NavSel.TButton", font="MewNav", anchor="w",
                        padding=(12, 6), background=sel_bg, foreground=sel_fg,
                        borderwidth=0, relief="flat", focuscolor=sel_bg)
        style.map("NavSel.TButton",
                  background=[("pressed", sel_bg), ("active", sel_bg)],
                  foreground=[("pressed", sel_fg), ("active", sel_fg)])
        style.configure("Sidebar.TCheckbutton", font="MewNav",
                        background=sidebar, foreground=fg)
        style.map("Sidebar.TCheckbutton", background=[("active", sidebar)])
        style.configure("Warn.TLabel", font="MewHint",
                        foreground=p["st_warn"])

        # the top row (gui_shell): on the bar's ground, the page's tab lit as
        # the sidebar's row was
        style.configure("TopWordmark.TLabel", font="MewSection", background=card,
                        foreground=fg)
        style.configure("TopTab.TButton", font="MewNav", anchor="center",
                        padding=(12, 4), background=card, foreground=fg,
                        borderwidth=0, relief="flat", focuscolor=card)
        style.map("TopTab.TButton",
                  background=[("pressed", active), ("active", hover)],
                  foreground=[("disabled", p["disabled"])])
        style.configure("TopTabSel.TButton", font="MewNav", anchor="center",
                        padding=(12, 4), background=sel_bg, foreground=sel_fg,
                        borderwidth=0, relief="flat", focuscolor=sel_bg)
        style.map("TopTabSel.TButton",
                  background=[("pressed", sel_bg), ("active", sel_bg)],
                  foreground=[("pressed", sel_fg), ("active", sel_fg)])
        # its design line (gui_summary)
        style.configure("BarOk.TLabel", font="MewHint", background=card,
                        foreground=p["st_ok"])
        style.configure("BarWarn.TLabel", font="MewHint", background=card,
                        foreground=p["st_warn"])

        # viewer-sidebar control button: a bordered CARD with CENTRED text, the ttk
        # twin of gui_viewer_shell.ViewerChrome's VTK card button, so the embedded
        # rail and the fullscreen sidebar read as one control. Keep in step with
        # ViewerChrome.button/layout.
        style.configure("ViewerBtn.TButton", font="MewNav", anchor="center",
                        padding=(10, 4), background=card, foreground=fg,
                        borderwidth=1, relief="solid", bordercolor=hairline,
                        lightcolor=card, darkcolor=card, focuscolor=card)
        style.map("ViewerBtn.TButton",
                  background=[("pressed", active), ("active", hover)],
                  bordercolor=[("focus", sel_bg), ("active", hairline)],
                  foreground=[("disabled", disabled)])

        # action bar
        style.configure("Bar.TFrame", background=card)
        style.configure("Bar.TLabel", background=card, foreground=fg)
        style.configure("BarHint.TLabel", font="MewHint", background=card,
                        foreground=hint)
        # the Settings panel sits on the card colour too (gui_settings)
        style.configure("Bar.TCheckbutton", background=card, foreground=fg)
        style.map("Bar.TCheckbutton", background=[("active", card)])
        # Link.TButton on a bar (the G-code page's Back, Stop following,
        # Reset view): the same text-like button on the card colour
        style.configure("BarLink.TButton", font="MewBody", padding=(0, 2),
                        background=card, foreground=sel_bg, borderwidth=0,
                        relief="flat", focuscolor=card, anchor="w")
        style.map("BarLink.TButton",
                  background=[("pressed", card), ("active", card)],
                  foreground=[("disabled", disabled)])
        # the one filled-accent control in the app (primary action only)
        style.configure("Primary.TButton", font="MewSection", padding=(18, 8),
                        background=sel_bg, foreground=sel_fg, borderwidth=0,
                        relief="flat", focuscolor=sel_bg)
        style.map("Primary.TButton",
                  background=[("disabled", active),
                              ("pressed", _shade(sel_bg, 0.82)),
                              ("active", _shade(sel_bg, 0.92))],
                  foreground=[("disabled", disabled)])

        # progress: a thin flat track with an accent fill. Clam's bevelled,
        # bordered box read as an empty text field while idle
        style.configure("Horizontal.TProgressbar", troughcolor=active,
                        background=sel_bg, bordercolor=active,
                        lightcolor=sel_bg, darkcolor=sel_bg, borderwidth=0,
                        # clam sizes the bar by arrowsize; it has no thickness option
                        arrowsize=gui_fonts.dpi_px(style.master, 6))

        # (Home uses the flat gui_widgets.ActionRow / Pill.TButton language like
        # every other page, so there are no boxed home-card styles here.)