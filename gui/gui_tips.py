#!/usr/bin/env python3
"""gui_tips.py -- MODERN-UI ONLY: hover tooltips, the info dot (click to open a
small popup), and the one shared glossary of MEW terms, so wording stays
consistent across pages.

Two mechanisms, three entry points:
  * add_tip(widget, text)   -- a HOVER tooltip on any control, for a short hint.
  * info_dot(parent, text)  -- a clickable dot; CLICK opens a dismissable popup
                               (click-away / Esc / click the dot again). This is
                               the home for long explanations that would
                               crowd a form inline. Pass title= for a bold heading.
  * glossary_dot(parent, term) -- an info_dot pre-filled from GLOSSARY.

A tip or dot text can carry a PICTURE: `pic(name, text)` returns the text as a
`TipText`, a str that also names `assets/tips/<name>.png`. Every path above
shows that picture above the words, and every caller that only passes the text
along (gui_form, Row.set_tip) keeps working because it is still a str. The
pictures are drawn from the engine's own output by assets/tips/make_tip_images.py;
a missing file just leaves the tip as text.

Palette-aware: gui_modern_theme calls set_tip_palette() on every theme apply. The
classic (developer) UI never imports this module."""

import os
import gui_units
import tkinter as tk
from tkinter import ttk
import tkinter.font as _tkfont

try:
    from app_paths import resource_path as _resource_path
except ImportError:                     # a stripped-down import path (tests)
    def _resource_path(*parts):
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), *parts)

# colours the next-shown tip/popup uses; retinted by gui_modern_theme on apply
_TIP = {"bg": "#ffffff", "fg": "#1c1e21", "border": "#b8bcc2"}

#: where `pic` names resolve; bundled by MEWracle.spec
TIP_IMAGE_DIR = _resource_path("assets", "tips")


class TipText(str):
    """A tip text that also names a picture in TIP_IMAGE_DIR (`.image`)."""
    image = None


def pic(name, text):
    """`text` as a TipText showing assets/tips/<name>.png above the words."""
    t = TipText(text)
    t.image = name
    return t


# One PhotoImage per (interpreter, name), kept here: Tk drops an image the
# moment Python drops its last reference, and a tip is rebuilt on every hover.
_IMAGES = {}


def tip_image(widget, name):
    """The PhotoImage for picture `name`, or None when there is no such file or
    Tk cannot read it. Loaded once per interpreter."""
    if not name:
        return None
    try:
        interp = widget.tk
    except AttributeError:
        return None
    key = (id(interp), name)
    if key not in _IMAGES:
        path = os.path.join(TIP_IMAGE_DIR, name + ".png")
        img = None
        if os.path.isfile(path):
            try:
                img = tk.PhotoImage(master=widget, file=path)
            except tk.TclError:
                img = None
        _IMAGES[key] = img
    return _IMAGES[key]


def _image_label(parent, widget, text):
    """A label holding `text`'s picture, or None when it has none."""
    img = tip_image(widget, getattr(text, "image", None))
    if img is None:
        return None
    return tk.Label(parent, image=img, background=_TIP["bg"], borderwidth=0)


def _font(widget, name, fallback):
    """The named modern-UI font if it exists, else a plain fallback. gui_tips is also
    reached by the physics sim panel, which renders in the classic developer UI where
    the Mew* named fonts are never installed."""
    try:
        if name in _tkfont.names(widget):
            return name
    except Exception:  # noqa: BLE001
        pass
    return fallback


def set_tip_palette(bg, fg, border):
    _TIP.update(bg=bg, fg=fg, border=border)


class Tooltip:
    """A hover tooltip: appears after `delay` ms near the pointer, hides on
    leave/click/destroy. One instance per widget; safe on any widget."""

    def __init__(self, widget, text, delay=500, wrap=360):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.wrap = wrap
        self._after = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        try:
            self._after = self.widget.after(self.delay, self._show)
        except tk.TclError:
            pass

    def _cancel(self):
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self):
        if self._tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_pointerx() + 14
            y = self.widget.winfo_pointery() + 18
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            try:
                tip.attributes("-topmost", True)
            except tk.TclError:
                pass
            frame = tk.Frame(tip, background=_TIP["border"], padx=1, pady=1)
            frame.pack()
            picture = _image_label(frame, self.widget, self.text)
            if picture is not None:
                picture.configure(padx=8, pady=6)
                picture.pack(fill="x")
            # display units (gui_units): a speed or pressure in the text reads
            # in the unit the fields show; the picture keys on the raw text
            tk.Label(frame, text=gui_units.localize(self.text), justify="left",
                     wraplength=self.wrap, background=_TIP["bg"],
                     foreground=_TIP["fg"],
                     font=_font(self.widget, "MewHint", ("Segoe UI", 9)),
                     padx=8, pady=6).pack()
            # keep the tip on-screen horizontally
            tip.update_idletasks()
            sw = tip.winfo_screenwidth()
            if x + tip.winfo_reqwidth() > sw - 8:
                x = max(8, sw - tip.winfo_reqwidth() - 8)
            tip.wm_geometry(f"+{x}+{y}")
            self._tip = tip
        except tk.TclError:
            self._tip = None

    def _hide(self, _e=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


def add_tip(widget, text, wrap=360):
    """Attach a hover tooltip to any widget; returns the widget for chaining."""
    Tooltip(widget, text, wrap=wrap)
    return widget


# --------------------------------------------------------------------------- #
# the ⓘ info button: click to open a dismissable popup
# --------------------------------------------------------------------------- #
# One popup at a time, tracked module-wide. The global click/Esc dismiss is armed
# once per root and left in place (Tk's unbind is unreliable for a single funcid),
# guarded on whether a popup is open -- so it is a no-op the rest of the time.
_POPUP = {"win": None, "anchor": None}
_ARMED = set()          # id(root) already carrying the dismiss binding


def _close_popup(_e=None):
    win = _POPUP["win"]
    _POPUP["win"] = None
    _POPUP["anchor"] = None
    if win is not None:
        try:
            win.destroy()
        except tk.TclError:
            pass


def _global_click(e):
    """Close the open popup when the click lands outside it (and outside the dot
    that owns it -- that dot's own handler toggles)."""
    win = _POPUP["win"]
    if win is None:
        return
    wpath = str(win)
    ew = str(e.widget)
    if ew == wpath or ew.startswith(wpath + "."):
        return                                   # click inside the popup
    if e.widget is _POPUP["anchor"]:
        return                                   # the dot toggles itself
    _close_popup()


def _ensure_armed(root):
    if id(root) in _ARMED:
        return
    try:
        root.bind_all("<Button-1>", _global_click, add="+")
        root.bind_all("<Escape>", _close_popup, add="+")
        _ARMED.add(id(root))
    except tk.TclError:
        pass


def _open_popup(anchor, text, title, wrap):
    text = gui_units.localize(text)     # display units (gui_units)
    _close_popup()
    try:
        win = tk.Toplevel(anchor)
    except tk.TclError:
        return
    win.wm_overrideredirect(True)
    try:
        win.attributes("-topmost", True)
    except tk.TclError:
        pass
    border = tk.Frame(win, background=_TIP["border"], padx=1, pady=1)
    border.pack()
    body = tk.Frame(border, background=_TIP["bg"])
    body.pack()
    if title:
        # NB: tk.Label's own -pady is a single screen distance, NOT a
        # (top, bottom) tuple -- put asymmetric vertical spacing on pack() instead.
        tk.Label(body, text=title, justify="left", background=_TIP["bg"],
                 foreground=_TIP["fg"],
                 font=_font(anchor, "MewSection", ("Segoe UI", 10, "bold")),
                 padx=12).pack(anchor="w", fill="x", pady=(9, 1))
    picture = _image_label(body, anchor, text)
    if picture is not None:
        picture.configure(padx=12)
        picture.pack(anchor="w", pady=(6 if title else 9, 0))
    tk.Label(body, text=text, justify="left", wraplength=wrap,
             background=_TIP["bg"], foreground=_TIP["fg"],
             font=_font(anchor, "MewHint", ("Segoe UI", 9)),
             padx=12).pack(anchor="w", pady=(2 if title else 9, 10))

    # place under the dot, flipped/clamped to stay on-screen
    win.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    ax, ay = anchor.winfo_rootx(), anchor.winfo_rooty()
    x = ax
    y = ay + anchor.winfo_height() + 4
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    if x + w > sw - 8:
        x = max(8, sw - w - 8)
    if y + h > sh - 8:
        y = max(8, ay - h - 4)                    # flip above the dot
    win.wm_geometry(f"+{x}+{y}")

    _POPUP["win"] = win
    _POPUP["anchor"] = anchor
    _ensure_armed(anchor.winfo_toplevel())


def show_popup(anchor, text, title=None, wrap=380):
    """Open the click-to-dismiss popup `info_dot` uses, anchored under any
    widget. For a control whose explanation must not be hover-only."""
    _open_popup(anchor, text, title, wrap)


def info_dot(parent, text, wrap=380, title=None):
    """A small dot that opens `text` in a click-to-dismiss popup -- the modern shell's
    replacement for long inline explainer paragraphs. Click to open, click away / Esc /
    click again to close. `title` adds a bold heading. Returns the dot label.

    The text is RE-READ at open time, not captured, and the label carries
    `set_info_text()` / `info_text()` so a caller can change it later. The Calibration
    page needs that: four of its Process-settings sub-sections are ONE set of widgets
    shared by both print tabs, so the Instructions step number a dot quotes has to
    follow the selected tab rather than be frozen at build time."""
    lbl = ttk.Label(parent, text="ⓘ", cursor="hand2", style="Hint.TLabel")
    cell = {"text": text, "title": title}

    def _set(new_text, new_title=None):
        cell["text"] = new_text
        if new_title is not None:
            cell["title"] = new_title

    lbl.set_info_text = _set
    lbl.info_text = lambda: cell["text"]

    def _toggle(_e=None):
        if _POPUP["win"] is not None and _POPUP["anchor"] is lbl:
            _close_popup()
        else:
            _open_popup(lbl, cell["text"], cell["title"], wrap)
        return "break"

    lbl.bind("<Button-1>", _toggle)
    lbl.bind("<Destroy>", lambda _e: (_close_popup()
                                      if _POPUP["anchor"] is lbl else None))
    return lbl


def glossary_dot(parent, term, wrap=380, title=None):
    """An info_dot pre-filled from GLOSSARY[term] (case-insensitive), titled with
    the term so it reads the same everywhere it appears. Falls back to showing
    `term` verbatim if it is not in the glossary."""
    entry = GLOSSARY.get(term)
    key = term
    if entry is None:
        for k, v in GLOSSARY.items():
            if k.lower() == term.lower():
                entry, key = v, k
                break
    if entry is None:
        return info_dot(parent, term, wrap=wrap, title=title)
    head = title or (key if key.isupper() else key[:1].upper() + key[1:])
    return info_dot(parent, entry, wrap=wrap, title=head)


# One shared glossary so terminology reads the same on every page. Keep the
# definitions plain-English and short enough for a popup; wire them in with
# glossary_dot(parent, "<key>").
GLOSSARY = {
    "CTS": pic("cts",
        "Critical translation speed: the print speed above which the jet is "
        "pulled straight. Below it the jet buckles and coils like thread from "
        "a sewing machine, so fibers wobble instead of landing straight."),
    "jet lag": pic("jet_lag",
        "The molten jet trails behind the moving nozzle, so fiber lands a "
        "short distance BEHIND where the nozzle is, and sharp corners come "
        "out rounded."),
    "Taylor cone": ("The cone of molten polymer at the nozzle that necks down "
                    "into the jet. It is drawn in the simulation only and never "
                    "changes the deposited fiber."),
    "SCV": ("Square-corner velocity: a printer setting for how fast the head may "
            "round a 90° corner without slowing to a stop. Read it from your "
            "printer's config."),
    "stepover": pic("stepover",
        "Fiber spacing: how far apart neighbouring fibers are laid in one "
        "layer, centre to centre. The open pore between them is this less "
        "the fiber diameter: 0.5 mm with a 25 µm fiber leaves a 475 µm "
        "pore."),
    "stabilization base": pic("stabilization",
        "A small dense pad printed FIRST, in the same continuous fiber, so "
        "the jet is already steady before the real part starts."),
    "standoff": ("The gap from the nozzle down to the collector: the fiber's "
                 "working height, and the distance the electric field spans."),
    "collector": ("The grounded surface the fiber lands on: a flat plate (or a "
                  "glass slide on it), or a rotating mandrel."),
    "jet": ("The thin stream of molten polymer drawn from the nozzle to the "
            "collector. In the viewer it is the moving thread under the nozzle; it "
            "is a visual aid and does not change the saved fiber."),
    "travels": ("Non-printing repositioning moves (G0) between printed features. "
                "In continuous-flow MEW the jet keeps flowing, so a little fiber is "
                "laid on travels too."),
    "footprint": ("The 2-D outline the scaffold fills, its plan-view shape "
                  "(rectangle, circle, ellipse, polygon, or a drawn outline)."),
    "sag": pic("sag",
        "Deposited fibers sag under their own weight where they bridge a "
        "gap, and rest on whatever is already printed below. The renderer "
        "reproduces this sag so the STL looks like the real print."),
    "deposition cache": ("A .depo file holding the deposited fiber paths worked "
                         "out during Generate, saved so the viewers and deviation "
                         "can reuse them without rebuilding the heavy STL mesh."),
    "deviation": ("How far the predicted deposited fiber lands from the path the "
                  "g-code commanded, measured sideways (across-track) at each point "
                  "along the print."),
    "render quality": ("Lines (instant) draws each fiber as a thin line. "
                       "Fibers (fast) and Fibers (smooth) draw round fibers, "
                       "with 6 or 12 sides. Full mesh (slow) draws the exact "
                       "fiber surface."),
    "speed ratio": ("ε (epsilon) = print speed ÷ CTS. It sets how far above the "
                    "coiling threshold each test square prints; bigger ε means "
                    "more jet lag and rounder corners."),
    "fusion": ("Where fibers touch, the upper one settles into the one below "
               "instead of stacking full-height, so the structure is shorter than "
               "diameter × number-of-layers. Two kinds, calibrated separately on "
               "the Physics page: a JUNCTION where fibers cross briefly at a point, "
               "and a STRAIGHT SPAN where they stack collinearly for a long run "
               "(a wall, or a fiber sagging into a valley onto the parallel layer "
               "below). Suspended spans never fuse: there is nothing under them."),
    "array": ("Tile one scaffold into a grid of identical copies. When on, the copy "
              "count replaces the multi-scaffold stack."),
    "linking loop": pic("linking_loops",
        "A small lead-in loop added where one layer ends and the next begins "
        "at the same square corner, so the jet rounds the turn just off the "
        "part instead of on it. It is not the turnaround at the end of each "
        "pass."),
    "datum": ("A chosen reference point on a part (a corner or face) that other "
              "positions are measured from."),
    "kinematics": ("Which axis each part of the printer moves on, e.g. the head "
                   "moves in X/Y and the bed in Z. Pick the option that matches "
                   "your printer so the simulation moves correctly."),
    "pore size": ("The open gap between neighbouring fibers in a layer: the "
                  "fiber spacing less the fiber diameter. It is the grid's hole "
                  "size."),
}
