#!/usr/bin/env python3
"""gui_theme.py -- Theme mixin: the Dark-mode toggle and the whole light/dark
theming engine, applied *by widget class* so it maintains itself.

Part of the gcode_to_fibers_gui split; see gui_common.py.

Design: theme by CLASS, never per instance. Adding a new tab/field/widget later
must NOT require touching any dark-mode code, and two mechanisms guarantee it:

* Every form widget in the GUI is a ttk widget, and `ttk.Style` is configured per
  widget CLASS, so any ttk widget created now or later inherits the theme for
  free. `_apply_theme` switches the engine to "clam" -- the one themeable base
  theme -- when dark, and configures every class from a palette.

* The only classic (non-ttk) tk widgets are handled categorically, not by name.
  The two log/g-code `tk.Text` consoles are intentionally dark in BOTH modes and
  are left alone; the VScrollFrame page-background canvases are recoloured
  (existing ones by `_recolor_classic`, future ones by the `option_add` defaults);
  the linear-pattern preview canvas is a chart and stays white in both modes.
  `option_add` keys are class-scoped, so they pre-colour any classic widget
  created after startup and never bleed into ttk widgets.

The only thing that cannot auto-adapt is a DELIBERATE hardcoded per-widget
colour; for a new accent, read from the palette instead.

Light mode keeps the OS-native engine on Windows ("vista"), so the default look is
unchanged; only dark mode forces "clam". The preference persists per-user in
gui_prefs.json, its own tiny file, so this feature never touches the preset store.
"""
from gui_common import *

# Per-user dark-mode flag, written by the toggle. Its own file (not the preset
# store) so theming stays fully isolated from gui_presets. Gitignored.
import gui_prefs     # gui_prefs.json: one reader, one merging writer

# The dark palette. Light values are derived at runtime from the OS default
# background (captured once in _apply_theme) so light mode restores today's look
# exactly; see _light_palette.
DARK = {
    "bg":        "#2b2b2b",   # window / frames / notebook page / structural canvas
    "field":     "#3c3f41",   # entry / spinbox / combobox / treeview field
    "fg":        "#e0e0e0",   # primary text
    "disabled":  "#6f6f6f",   # disabled text / arrows
    "select_bg": "#4b6eaf",   # selection / active highlight
    "select_fg": "#ffffff",
    "border":    "#555555",   # troughs, borders, separators
    "active":    "#4a4d4f",   # button / heading background, hover
    "accent":    "#3a4a63",   # preset group-header rows (replaces #e8eef7)
    "tab_bg":    "#3c3f41",   # unselected notebook tabs
}


class _ThemeMixin:
    # -- the toggle widget (built into the shared bottom strip by gui_core) ---
    def _build_theme_toggle(self, parent):
        """Create the Dark-mode checkbox in `parent` (the always-visible bottom
        button strip). The boot value comes from the saved per-user preference, so
        the window opens in whichever mode was last used. Returns the widget."""
        self.dark_mode = tk.BooleanVar(value=self._load_theme_pref())
        chk = ttk.Checkbutton(parent, text="\U0001F319 Dark mode",
                              variable=self.dark_mode, command=self._toggle_theme)
        chk.pack(side="right", padx=8)
        return chk

    def _toggle_theme(self):
        """Apply the newly chosen mode and remember it for next launch."""
        self._apply_theme()
        self._save_theme_pref(self.dark_mode.get())

    # -- the engine ---------------------------------------------------------- #
    def _apply_theme(self):
        """(Re)paint the whole GUI for the current `dark_mode` value. Idempotent and
        cheap, so it is safe at startup and on every toggle. Themes by class
        (ttk.Style) + class-scoped option defaults + a walk for the existing
        structural canvases, so it needs no per-widget bookkeeping."""
        dark = bool(self.dark_mode.get())
        root = self.root
        # capture the OS-native background ONCE, before we ever recolour, so the
        # light palette can restore it verbatim (matches vista/clam frames).
        if getattr(self, "_light_bg", None) is None:
            try:
                self._light_bg = root.cget("background")
            except tk.TclError:
                self._light_bg = "#f0f0f0"

        pal = DARK if dark else self._light_palette()
        # clam is the only base theme that honours colour config; use it for dark
        # always and for light off-Windows (so a dark->light switch fully resets).
        # Windows light keeps native "vista" -- zero change from today's look.
        use_clam = dark or not IS_WINDOWS
        style = ttk.Style()
        try:
            style.theme_use("clam" if use_clam else "vista")
        except tk.TclError:
            style.theme_use("clam")
            use_clam = True
        if use_clam:
            self._style_clam(style, pal)
        # both themes: ttk's row is a fixed 20 px, which clips the text once
        # the display scales it past 100%
        import gui_fonts
        style.configure("Treeview",
                        rowheight=gui_fonts.row_height(style.master, 20))

        try:
            root.configure(background=pal["bg"])
        except tk.TclError:
            pass
        self._set_classic_options(pal)      # future classic widgets
        self._recolor_classic(root, pal)    # existing structural canvases
        self._theme_preset_table(pal)       # the one hardcoded tag we own

    def _light_palette(self):
        """Light-mode colours. The background mirrors the OS default so the
        structural canvases/root sit flush with native (or clam) frames; the
        preset-row accent is the original #e8eef7."""
        bg = getattr(self, "_light_bg", None) or "#f0f0f0"
        return {
            "bg": bg, "field": "white", "fg": "black",
            "disabled": "#a3a3a3", "select_bg": "#0a64ad", "select_fg": "white",
            "border": "#c0c0c0", "active": "#e5e5e5", "accent": "#e8eef7",
            "tab_bg": bg,
        }

    @staticmethod
    def _style_clam(style, p):
        """Configure every ttk class used in the GUI from palette `p`. Driving the
        clam theme by class is what makes new widgets theme themselves -- nothing
        here references a specific widget instance."""
        bg, field, fg = p["bg"], p["field"], p["fg"]
        disabled, sel_bg, sel_fg = p["disabled"], p["select_bg"], p["select_fg"]
        border, active, tab_bg = p["border"], p["active"], p["tab_bg"]

        # base: every class inherits from "."
        style.configure(".", background=bg, foreground=fg, fieldbackground=field,
                        troughcolor=bg, bordercolor=border, lightcolor=bg,
                        darkcolor=bg, focuscolor=sel_bg, insertcolor=fg)
        style.map(".", foreground=[("disabled", disabled)],
                  background=[("disabled", bg)])

        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TLabelframe", background=bg, bordercolor=border)
        style.configure("TLabelframe.Label", background=bg, foreground=fg)
        style.configure("TSeparator", background=border)
        style.configure("TProgressbar", background=sel_bg, troughcolor=field,
                        bordercolor=border)

        style.configure("TButton", background=active, foreground=fg,
                        bordercolor=border, focuscolor=sel_bg)
        style.map("TButton",
                  background=[("pressed", sel_bg), ("active", sel_bg),
                              ("disabled", bg)],
                  foreground=[("disabled", disabled)])

        for cls in ("TEntry", "TSpinbox", "TCombobox"):
            style.configure(cls, fieldbackground=field, foreground=fg,
                            background=field, bordercolor=border, arrowcolor=fg,
                            insertcolor=fg)
            style.map(cls,
                      fieldbackground=[("readonly", field), ("disabled", bg)],
                      foreground=[("disabled", disabled)],
                      arrowcolor=[("disabled", disabled)])

        for cls in ("TCheckbutton", "TRadiobutton"):
            style.configure(cls, background=bg, foreground=fg)
            style.map(cls, background=[("active", bg)],
                      foreground=[("disabled", disabled)],
                      indicatorcolor=[("selected", sel_bg), ("!selected", field)])

        style.configure("TNotebook", background=bg, bordercolor=border)
        style.configure("TNotebook.Tab", background=tab_bg, foreground=fg,
                        bordercolor=border, padding=(8, 3))
        style.map("TNotebook.Tab",
                  background=[("selected", bg), ("active", active)],
                  foreground=[("selected", fg), ("disabled", disabled)])

        style.configure("Treeview", background=field, fieldbackground=field,
                        foreground=fg, bordercolor=border)
        style.map("Treeview", background=[("selected", sel_bg)],
                  foreground=[("selected", sel_fg)])
        style.configure("Treeview.Heading", background=active, foreground=fg,
                        bordercolor=border)
        style.map("Treeview.Heading", background=[("active", sel_bg)])

        style.configure("TScrollbar", background=active, troughcolor=bg,
                        bordercolor=border, arrowcolor=fg)
        style.map("TScrollbar", background=[("active", sel_bg)],
                  arrowcolor=[("disabled", disabled)])

    def _set_classic_options(self, p):
        """Seed the Tk option database so classic (non-ttk) widgets created AFTER
        now are born with the right colours. Keys are class-scoped, so ttk widgets
        are untouched and the explicit-white chart canvas keeps its own bg
        (constructor args beat the option db)."""
        bg, field, fg = p["bg"], p["field"], p["fg"]
        sel_bg, sel_fg = p["select_bg"], p["select_fg"]
        for key, val in (
            ("*Canvas.background", bg),
            ("*Canvas.highlightBackground", bg),
            ("*Menu.background", field), ("*Menu.foreground", fg),
            ("*Menu.activeBackground", sel_bg),
            ("*Menu.activeForeground", sel_fg),
            ("*Listbox.background", field), ("*Listbox.foreground", fg),
            ("*Listbox.selectBackground", sel_bg),
            ("*Listbox.selectForeground", sel_fg),
            ("*TCombobox*Listbox.background", field),
            ("*TCombobox*Listbox.foreground", fg),
            ("*TCombobox*Listbox.selectBackground", sel_bg),
            ("*TCombobox*Listbox.selectForeground", sel_fg),
        ):
            try:
                self.root.option_add(key, val)
            except tk.TclError:
                pass

    def _recolor_classic(self, widget, p):
        """Recurse the widget tree and recolour the EXISTING structural canvases, the
        VScrollFrame page backgrounds. Identified by RELATIONSHIP (a canvas whose
        master is a VScrollFrame), never by name, so new VScrollFrames are handled the
        same way. The console tk.Text widgets and the white chart canvas are skipped."""
        for w in widget.winfo_children():
            if isinstance(w, tk.Canvas) and isinstance(w.master, VScrollFrame):
                try:
                    w.configure(background=p["bg"], highlightbackground=p["bg"])
                except tk.TclError:
                    pass
            self._recolor_classic(w, p)

    def _theme_preset_table(self, p):
        """Re-tag every preset grid's accent rows from the palette -- the lone
        hardcoded accent this feature owns. A no-op until a Presets tab has been
        built, and it iterates the per-kind stores, so any future kind is themed too.
        It also reaches every grid built since (`_preset_trees`): the G-code
        page's Presets panel builds its own grid, which is in no store, and was
        left with the light accent under dark mode's light text."""
        self._preset_accent = p["accent"]
        live = [t for t in getattr(self, "_preset_trees", ())
                if _exists(t)]
        self._preset_trees = live
        trees = [st.get("table") for st in getattr(self, "_pstores", {}).values()]
        for tree in trees + [t for t in live if t not in trees]:
            if tree is None:
                continue
            try:
                tree.tag_configure("preset", background=p["accent"])
            except tk.TclError:
                pass

    # -- per-user preference (its own tiny file, like the default-preset
    #    pointer in gui_presets) --------------------------------------------- #
    @staticmethod
    def _load_theme_pref():
        """Read the saved dark-mode flag from gui_prefs.json; False on a missing
        or corrupt file (tolerant, like _load_default_pointer)."""
        return bool(gui_prefs.get("dark_mode", False))

    @staticmethod
    def _save_theme_pref(value):
        """Persist the dark-mode flag to gui_prefs.json (best-effort). MERGED
        into the file: it used to rewrite the whole file as this one key."""
        gui_prefs.update(dark_mode=bool(value))


def _exists(widget):
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False
