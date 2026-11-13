#!/usr/bin/env python3
"""gui_fonts.py -- MODERN-UI ONLY: the Helvetica type ramp (named fonts) and the
spacing tokens the modern shell feeds the mounted tab builders via `self.pad`.

All type flows through NAMED Tk fonts, so one module controls the whole app. The
intended look is Helvetica, and the ramp takes the first of these that really
renders (checked with `Font.actual()`, which reports the face the OS realized):

1. **Helvetica Neue**, when installed.
2. **Helvetica**, when installed AND Tk draws it. On Windows Tk realizes Arial
   for "Helvetica" even with a Helvetica installed and registered (measured:
   `Font.actual()` said Arial, and the line metrics were Arial's 13/3/16 px at
   10 pt, not the installed Helvetica.ttf's 12/3/15), so there this step never
   succeeds.
3. **Liberation Sans**, bundled in `fonts/liberation-fonts-ttf-2.1.5/` and
   registered for this process only (GDI AddFontResourceExW, FR_PRIVATE). It
   is Red Hat's Arial/Helvetica-metric face under the SIL Open Font License
   1.1, whose text ships beside it. The WHOLE upstream release ships, byte for
   byte (Serif and Mono too): the OFL counts deleting a component of the
   Original Version as a modification, so do not trim the folder.
4. **Arial**, the OS's own Helvetica stand-in, if even the bundled files are gone.

The bundled face must be a HINTED TrueType (.ttf with fpgm/prep/cvt tables).
GDI, which draws every Tk widget on Windows, renders a CFF-outline .otf with no
TrueType hints faint and soft: the TeX Gyre Heros .otf this replaced laid down
13% less ink than Helvetica in 10 pt body text and 25% less in the 9 pt grey
hint text (same strings, same GDI path, 96 dpi). Liberation Sans is within 3%
of Arial at both sizes.

Every .ttf/.otf/.ttc under the app's "fonts" folder (from source, also the
sibling "Font" folder) is registered the same way, so a user-supplied face is
available by name. Liberation Sans has Arial's advance widths but a line 1 px
shorter at 10 pt, so any fixed pixel height beside text must come from
`row_height()` or the font's own metrics. `font_report()` states which case is
live.

The classic (developer) UI never imports this module."""

import os
import sys

import tkinter as tk
from tkinter import font as tkfont

# -- spacing tokens (the modern shell's self.pad; classic keeps padx=6/pady=4) --
# Everything sits on a 4-px grid: tight rhythm INSIDE a group (PAD), generous air
# BETWEEN groups (GAP_SECTION/PAGE_MARGIN). SP is the raw scale for the
# gui_widgets primitives and any one-off layout math.
SP = {"xs": 4, "s": 8, "m": 12, "l": 16, "xl": 24, "xxl": 32}
PAD = {"padx": 8, "pady": 4}
GAP_SECTION = 24          # vertical air between major page sections
GAP_HEADER = 8            # section header -> its first row
PAGE_MARGIN = 24          # page body inset
INDENT = 16               # disclosure bodies / checkbox children

# request order: a family is used directly only when the OS resolves it to
# itself (a real install); otherwise plain "Helvetica" is requested regardless
# and the OS substitute (Arial on Windows) renders it.
_PREFERRED = ("Helvetica Neue", "Helvetica")
#: the bundled face (fonts/liberation-fonts-ttf-2.1.5), once registered
_BUNDLED = "Liberation Sans"
#: what the OS draws when nothing above loads
_LAST_RESORT = "Arial" if os.name == "nt" else "Helvetica"

_requested = None           # family the ramp asks for ("Helvetica"…)
_actual = None              # family the OS actually renders (from Font.actual)
# live references to the created named fonts. REQUIRED: tkinter's Font.__del__
# DELETES the underlying named Tk font when the Python wrapper is garbage
# collected, so an unreferenced create would silently un-define the ramp.
_FONTS = {}


def dpi_px(widget, px96):
    """`px96` pixels as drawn at 96 dpi, scaled to `widget`'s screen. Fonts
    are sized in points, which Windows scales with the display; any pixel
    size that sits beside text has to scale the same way or the text
    outgrows it (a 24 px row holds 10 pt text at 100%, not at 225%)."""
    try:
        s = float(widget.winfo_fpixels("1i")) / 96.0
    except tk.TclError:
        s = 1.0
    return max(1, round(px96 * s))


def size_dialog(top, w96, h96, min_w96=None, min_h96=None):
    """Size a dialog given in 96-dpi pixels the way its text is sized: scaled
    with the screen (`dpi_px`), and never past the screen, less room for the
    taskbar and the title bar. A fixed 920x640 held 150% text in two thirds
    of the room it needed and cut its Close button off."""
    sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
    w = min(dpi_px(top, w96), sw - dpi_px(top, 40))
    h = min(dpi_px(top, h96), sh - dpi_px(top, 100))
    top.geometry("%dx%d" % (w, h))
    if min_w96 and min_h96:
        top.minsize(min(dpi_px(top, min_w96), w), min(dpi_px(top, min_h96), h))


def row_height(widget, px96, font="TkDefaultFont"):
    """A Treeview row height for text in `font`: `px96` scaled with the
    screen, and never less than the font's line plus a margin, so a larger
    text setting or a taller substitute face cannot clip the rows."""
    try:
        line = tkfont.nametofont(font, root=widget).metrics("linespace")
    except tk.TclError:
        line = tkfont.Font(root=widget, font=font).metrics("linespace")
    return max(dpi_px(widget, px96), line + dpi_px(widget, 6))


def resolved_family():
    """The family actually rendering ('' until init_fonts has run)."""
    return _actual or ""


def requested_family():
    """The family the ramp requests (Helvetica unless a Neue is installed)."""
    return _requested or ""


def font_report():
    """One line saying which face the UI is drawn in, and why."""
    if not _requested:
        return ""
    req, act = requested_family(), resolved_family()
    if _actual and _actual.casefold() == _BUNDLED.casefold():
        return (f"UI font: {req} (bundled Helvetica-metric face; an "
                "installed Helvetica Neue is used instead when present)")
    if _requested == _LAST_RESORT:
        return (f"UI font: {req} (the OS substitute "
                f"{act or '?'}: the bundled Liberation Sans in the fonts "
                "folder was not found)")
    if act and act.casefold() == req.casefold():
        return f"UI font: {req}"
    return (f"UI font: {req} (rendering the OS substitute "
            f"{act or '?'})")


def _font_dirs():
    """Folders whose font files are registered: the bundled `fonts` folder
    (beside the frozen exe's data, or the repo from source) and, from source
    only, a sibling `Font` folder a developer may keep licensed faces in."""
    import app_paths
    dirs = [app_paths.resource_path("fonts")]
    if not getattr(sys, "frozen", False):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dirs.append(os.path.abspath(os.path.join(here, os.pardir, "Font")))
    return dirs


def _load_private_fonts():
    """Windows-only: register every .ttf/.otf/.ttc under the `_font_dirs()`
    (recursive) for THIS process (GDI AddFontResourceExW with FR_PRIVATE), so
    the bundled Liberation Sans, or a user-supplied face, works without a
    system-wide install. Returns how many files registered. Safe no-op off
    Windows / when no folder exists."""
    if os.name != "nt":
        return 0
    import ctypes
    FR_PRIVATE = 0x10
    count = 0
    for base in _font_dirs():
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                if fn.lower().endswith((".ttf", ".otf", ".ttc")):
                    path = os.path.join(dirpath, fn)
                    try:
                        if ctypes.windll.gdi32.AddFontResourceExW(
                                path, FR_PRIVATE, 0):
                            count += 1
                    except Exception:  # noqa: BLE001
                        pass
    return count


def init_fonts(root):
    """Create (or reconfigure) the named fonts of the type ramp. Must run after
    the Tk root exists and before the ModernApp builds. Also retunes the core Tk
    named fonts (TkDefaultFont & co.) so classic tk widgets inherit the family.
    Idempotent. Returns the family actually rendering."""
    global _requested, _actual
    _load_private_fonts()      # the bundled face (+ any user face), per-process
    fam = None
    for want in _PREFERRED + (_BUNDLED,):
        try:
            probe = tkfont.Font(root=root, family=want, size=10)
            if probe.actual("family").casefold() == want.casefold():
                fam = want                   # genuinely installed
                break
        except tk.TclError:
            continue
    if fam is None:
        fam = _LAST_RESORT                   # the bundled files are missing
    _requested = fam

    specs = {
        "MewBody":       dict(family=fam, size=10),
        "MewTitle":      dict(family=fam, size=15, weight="bold"),
        "MewSection":    dict(family=fam, size=11, weight="bold"),
        "MewHint":       dict(family=fam, size=9),
        "MewNav":        dict(family=fam, size=10),
        "MewNavSection": dict(family=fam, size=8, weight="bold"),
        "MewOverline":   dict(family=fam, size=8, weight="bold"),
        "MewMono":       dict(family="Consolas", size=9),
    }
    for name, spec in specs.items():
        try:
            f = tkfont.nametofont(name, root=root)        # already exists
            f.configure(**spec)
        except tk.TclError:
            f = tkfont.Font(root=root, name=name, **spec)
        _FONTS[name] = f          # keep the wrapper alive (see _FONTS above)
    # the core Tk families, so any widget not styled explicitly follows the ramp
    for core in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
        try:
            tkfont.nametofont(core, root=root).configure(family=fam, size=10)
        except tk.TclError:
            pass
    body = _FONTS.get("MewBody")
    _actual = body.actual("family") if body is not None else fam
    return _actual
