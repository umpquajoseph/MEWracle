#!/usr/bin/env python3
"""gui_viewer_shell.py -- MODERN-UI ONLY: a themed left-sidebar chrome for the
two VTK subprocesses the shell launches (stl_viewer.py, gcode_player.py).

ALL 2D chrome -- wordmark, HUD readout, toggle buttons, keyboard legend,
print-time -- lives in a solid left sidebar; the only thing on the right is the
3D view, and nothing floats over the scene. The `vtk` pip wheel ships no
vtkRenderingTk DLL, so the render window cannot be embedded in a real Tk
sidebar; the pure-VTK way to guarantee it is a THREE-renderer stack in one
window:

    layer 0  bg_ren     full window            -> painted the sidebar colour
    layer 0  scene_ren  [sidebar_frac, 0, 1,1] -> the 3D scene (viewport bg)
    layer 1  chrome_ren full window, transparent -> every 2D actor (text/buttons)

Because the SCENE renderer's viewport starts at the sidebar's right edge, the 3D
content is physically clipped to the right region and can never bleed under the
sidebar. The transparent overlay then draws text and buttons in a controlled
order, so nothing hides behind a backdrop rectangle -- the bug the earlier
single-renderer version had.

Fonts: this text is VTK text, not Tk, so it does not pick up the GUI's Helvetica
automatically. The bundled `<app>/../Font/**/Helvetica.ttf` is loaded into every
text property via SetFontFile; a Helvetica INSTALLED in Windows' per-user or
system font folder is found too, so a checkout with no sibling Font folder (a
worktree, a release copy) still draws it. With no Helvetica anywhere, the
bundled Liberation Sans in `fonts/liberation-fonts-ttf-2.1.5/` (the face the
Tk ramp falls back to, see gui_fonts) is used, and VTK's built-in Arial only if
that is gone.

Palette + dark/light follow the SAME gui_prefs.json the main App writes, so a
viewer opened after toggling dark mode lands in the matching palette.
"""

import json
import os

import vtk

# The modern-shell palettes are pure dicts -- import without dragging tkinter
# (this module never opens a Tk root; it is imported inside VTK subprocesses).
from gui_modern_theme import LIGHT, DARK

# The width + row height shared with the ttk rail (gui_embed), so this sidebar
# and its minimized twin stay the same size. Import-free module -> safe here and
# in the vtk-free GUI process alike.
from viewer_sidebar_spec import SIDEBAR_W, BUTTON_ROW_H

from app_paths import resource_path as _resource_path  # noqa: E402
_HERE = _resource_path()   # the repo root from source, the bundle root frozen


# -- preferences / palette ------------------------------------------------ #

def read_dark_pref():
    """The saved dark-mode flag from gui_prefs.json (the file the App writes
    via gui_theme._save_theme_pref). Best-effort: missing/corrupt -> False.

    Read through gui_prefs, so it is the file the App WRITES -- not this
    module's own folder, which in the packaged build is the read-only bundle, not the %LOCALAPPDATA% folder the App writes to."""
    import gui_prefs
    return bool(gui_prefs.get("dark_mode", False))


def palette(dark=None):
    """The modern-shell palette (LIGHT/DARK from gui_modern_theme). dark=None
    reads the persisted flag; True/False forces a palette."""
    if dark is None:
        dark = read_dark_pref()
    return DARK if dark else LIGHT


def _hex_to_rgb(h):
    """'#rrggbb' -> (r, g, b) float triple in [0, 1]."""
    h = h.lstrip("#")
    return (int(h[0:2], 16) / 255.0,
            int(h[2:4], 16) / 255.0,
            int(h[4:6], 16) / 255.0)


# viewport (3D) background. Dark mode keeps the CAD/DCC dark backdrop; light
# mode is near-white -- a slightly grey bottom +
# white top gradient, so even pale fibers keep a silhouette (VTK's default
# shading does the rest; genuinely white fibers read through their shaded
# flanks and the per-scaffold colour toggle).
def viewport_bg(dark):
    """The flat/bottom colour (fallback paths use it un-gradiented)."""
    return (0.10, 0.11, 0.12) if dark else (0.906, 0.918, 0.933)


def apply_viewport_bg(renderer, dark):
    """Set the themed 3D backdrop on `renderer`: flat dark, or the light
    near-white vertical gradient."""
    renderer.SetBackground(*viewport_bg(dark))
    if dark:
        renderer.GradientBackgroundOff()
    else:
        renderer.SetBackground2(0.984, 0.988, 0.992)   # top: just off white
        renderer.GradientBackgroundOn()


# -- Helvetica (VTK text) ------------------------------------------------- #

_FONT_CACHE = {}


def _find_helvetica():
    """Locate the bundled Helvetica .ttf files. Returns (regular, bold), each
    an absolute path or None. Searches <app>/../Font and <app>/fonts
    recursively (the same places gui_fonts registers), then the Windows
    per-user and system font folders -- Tk finds an installed Helvetica by
    name, but VTK needs the file, and from a worktree <app>/../Font does not
    exist. Matches 'Helvetica' in the filename; prefers an exact
    'Helvetica.ttf' / 'Helvetica-Bold.ttf', taking the first one found in
    root order, so the bundled copy beats an installed one."""
    if "paths" in _FONT_CACHE:
        return _FONT_CACHE["paths"]
    reg = bold = None
    reg_exact = bold_exact = None
    roots = [os.path.abspath(os.path.join(_HERE, os.pardir, "Font")),
             os.path.join(_HERE, "fonts")]
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
        roots.append(os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                                  "Fonts"))
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                low = fn.lower()
                if not low.endswith(".ttf") or "helvetica" not in low:
                    continue
                full = os.path.join(dirpath, fn)
                is_bold = "bold" in low
                is_oblique = "oblique" in low or "italic" in low
                if is_oblique:
                    continue                       # upright faces only
                if is_bold:
                    bold = bold or full
                    if low == "helvetica-bold.ttf":
                        bold_exact = bold_exact or full
                else:
                    reg = reg or full
                    if low == "helvetica.ttf":
                        reg_exact = reg_exact or full
    paths = (reg_exact or reg, bold_exact or bold)
    if not paths[0]:
        lib = os.path.join(_HERE, "fonts", "liberation-fonts-ttf-2.1.5")
        pair = tuple(os.path.join(lib, f) for f in (
            "LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf"))
        if os.path.isfile(pair[0]):
            paths = tuple(f if os.path.isfile(f) else None for f in pair)
    _FONT_CACHE["paths"] = paths
    return paths


def apply_font(text_prop, bold=False):
    """Point a vtkTextProperty at the bundled Helvetica .ttf (upright or bold),
    or fall back to VTK's built-in Arial (metric-compatible) when it's absent.
    Central so every actor in every viewer gets the same face in one call."""
    reg, bold_file = _find_helvetica()
    path = (bold_file if bold else reg) or reg
    if path:
        try:
            text_prop.SetFontFamily(vtk.VTK_FONT_FILE)
            text_prop.SetFontFile(path)
        except Exception:  # noqa: BLE001 -- fall back if the build lacks it
            text_prop.SetFontFamilyToArial()
    else:
        text_prop.SetFontFamilyToArial()
    if bold:
        text_prop.SetBold(1)


# -- layout constants ----------------------------------------------------- #

# Shared with the minimized ttk rail (viewer_sidebar_spec.SIDEBAR_W) so the two
# states are the SAME width. 240 px clears the player's widest readout line --
# "v(act/set) NN.N/NN.N mm/s xN.NN", scale suffix and all -- which the old 160 px
# clipped once playback was sped up (the reported spillover).
SIDEBAR_WIDTH_PX = SIDEBAR_W
_PAD_X          = 12          # left inset for all sidebar content
_TITLE_SIZE     = 15
_SUBTITLE_SIZE  = 10
_SECTION_SIZE   = 9
_BUTTON_SIZE    = 12
_HUD_SIZE       = 12
_KEYS_SIZE      = 11

# One consistent vertical rhythm on the modern shell's 4-px grid (all rows are
# TOP-anchored and flow downward, so the layout is deterministic and gap-free
# regardless of window height).
_TITLE_H        = 20          # wordmark line
_SUBTITLE_H     = 18          # build-badge line
_BLOCK_GAP      = 16          # gap between major blocks (HUD / sections)
_SECTION_GAP    = 12          # extra gap ABOVE a section header
_SECTION_H      = 20          # section-header line
_BUTTON_H       = 28          # per toggle row (24-px surface + 4-px gap)
_ROW_H          = BUTTON_ROW_H   # visible button surface (shared with the rail)
_ROW_X          = 8           # button-row inset from the sidebar edges
_CHIP           = 8           # per-scaffold colour chip (square), px
_LINE_H         = 15          # per text line in a multi-line block (HUD / keys)
# slider rows: a value line, then a track with a draggable knob
_SLIDER_H       = 16          # the track+knob band height
_SLIDER_TRACK_H = 4           # the thin track bar
_KNOB_W         = 10          # draggable knob (px)
_KNOB_H         = 14
_SLIDER_ROW_H   = _LINE_H + 2 + _SLIDER_H + 6   # value line + band + gap
# the bottom transport bar (players' fullscreen timeline): margins inside the
# 3D area, the play/pause card width, and the right-aligned time reservation
_TRANSPORT_MARGIN = 24
_TRANSPORT_BTN_W  = 84
_TRANSPORT_TIME_W = 150       # fits "1:02:03 / 1:02:03"
_TRANSPORT_BOT    = 16        # gap under the bar

# Autofit knobs. The sidebar sizes itself to its content instead of
# living at a fixed 240x-tall block: it grows WIDER to fit the longest label and,
# when the stacked rows would run off the bottom of a short window, COMPRESSES its
# vertical rhythm + type so everything stays on screen.
_FONT_MIN        = 6          # never shrink VTK text below this on compression
_SCALE_MIN       = 0.5        # tightest vertical compression before a clip is allowed
_WIDTH_CAP_FRAC  = 0.42       # the sidebar never eats more than this frac of the window


class ViewerChrome:
    """A themed left sidebar drawn as a VTK overlay, plus a confined 3D scene
    viewport on the right. Owns ALL 2D actors so the caller never manages
    z-order or pixel math.

    Construct it only AFTER the caller has a window, an interactor and a scene
    renderer already added to that window; then `section` / `button` / `set_hud`
    / `footer_section` / `footer_label`, then `layout` and
    `install_click_handler`. Sections and buttons flow TOP-DOWN under the
    wordmark and HUD; footer_* rows flow BOTTOM-UP from the window's bottom edge,
    so a long key legend or a print-time readout never pushes the buttons
    off-screen.
    """

    def __init__(self, window, interactor, scene_renderer,
                 title="MEWracle", subtitle="", pal=None, dark=None):
        if dark is None:
            dark = read_dark_pref()
        self.window = window
        self.iren = interactor
        self.scene = scene_renderer
        self.pal = pal or palette(dark)

        self._sidebar_rgb = _hex_to_rgb(self.pal["sidebar"])
        self._fg_rgb      = _hex_to_rgb(self.pal["fg"])
        self._hint_rgb    = _hex_to_rgb(self.pal["hint"])
        self._card_rgb    = _hex_to_rgb(self.pal["card"])
        self._border_rgb  = _hex_to_rgb(self.pal["border"])
        # older prefs snapshots may predate the hairline key
        self._hairline_rgb = _hex_to_rgb(
            self.pal.get("hairline", self.pal["border"]))

        # base renderer: paints the WHOLE window the sidebar colour. Must
        # render BEFORE the scene (which paints its right-hand box over it),
        # so we re-order: drop the scene, add bg, re-add scene, add chrome.
        self.bg = vtk.vtkRenderer()
        self.bg.SetLayer(0)
        self.bg.InteractiveOff()
        self.bg.SetBackground(*self._sidebar_rgb)

        self.scene.SetLayer(0)
        apply_viewport_bg(self.scene, dark)

        self.chrome = vtk.vtkRenderer()
        self.chrome.SetLayer(1)
        self.chrome.InteractiveOff()
        # A layer-1 renderer CLEARS the depth buffer before drawing by
        # default, wiping the scene depth that install_cad_camera's pivot
        # reads (renderer.GetZ) -- orbit/zoom would silently fall back to the
        # focal plane instead of the point under the cursor. The chrome is
        # pure 2D overlay (depth test off), so preserving depth changes
        # nothing visually and keeps the pivot pick working.
        self.chrome.PreserveDepthBufferOn()

        window.SetNumberOfLayers(2)
        try:
            window.RemoveRenderer(self.scene)
        except Exception:  # noqa: BLE001
            pass
        window.AddRenderer(self.bg)        # 1st: base panel fill
        window.AddRenderer(self.scene)     # 2nd: 3D box over the base
        window.AddRenderer(self.chrome)    # 3rd (layer 1): 2D chrome on top

        # a 1-px divider between sidebar and scene (reads as a real panel edge)
        # + a hairline under the wordmark block, like the main GUI's sections
        self._divider = self._rect_actor(self._border_rgb)
        self._head_rule = self._rect_actor(self._hairline_rgb)

        self.buttons = []          # (actor, label_fn, callback)
        self._sliders = []         # slider rows (also present in _top_rows)
        self._transport = None     # the bottom timeline row (in _sliders too)
        self._transport_btn = None # its play/pause card (in buttons too)
        self._grab = None          # the slider row currently being dragged
        self._top_rows = []        # ordered: {"actor","kind","lines"}
        self._footer_rows = []     # ordered top->bottom visually; placed bottom-up
        self._hud_actor = None
        self._hud_header = None     # "READOUT" section header above the readout
        self._hud_lines = 0
        self._sidebar_w = SIDEBAR_WIDTH_PX   # autofit width, recomputed in layout()

        # wordmark + subtitle (top of the sidebar)
        self._title = self._text_actor(f"{title}", _TITLE_SIZE,
                                       self._fg_rgb, bold=True)
        self._subtitle = self._text_actor(f"{subtitle}", _SUBTITLE_SIZE,
                                          self._hint_rgb)

    # -- actor factories -------------------------------------------------- #
    def _text_actor(self, text, size, rgb, bold=False):
        a = vtk.vtkTextActor()
        a.SetInput(text)
        tp = a.GetTextProperty()
        tp.SetFontSize(size)
        tp.SetColor(*rgb)
        # TOP + LEFT anchoring: SetDisplayPosition then marks the top-left of
        # the text, so every row flows downward from a known top edge -- the
        # single rule that keeps the vertical rhythm consistent.
        tp.SetVerticalJustificationToTop()
        tp.SetJustificationToLeft()
        apply_font(tp, bold=bold)
        self.chrome.AddActor2D(a)
        return a

    def _rect_actor(self, rgb, opacity=1.0):
        pd = vtk.vtkPolyData()
        mapper = vtk.vtkPolyDataMapper2D()
        mapper.SetInputData(pd)
        a = vtk.vtkActor2D()
        a.SetMapper(mapper)
        a.GetProperty().SetColor(*rgb)
        a.GetProperty().SetOpacity(opacity)
        self.chrome.AddActor2D(a)
        a._pd = pd                              # keep a handle for resize
        return a

    @staticmethod
    def _set_rect(actor, x, y, w, h):
        pts = vtk.vtkPoints()
        pts.InsertNextPoint(x, y, 0.0)
        pts.InsertNextPoint(x + w, y, 0.0)
        pts.InsertNextPoint(x + w, y + h, 0.0)
        pts.InsertNextPoint(x, y + h, 0.0)
        cells = vtk.vtkCellArray()
        cells.InsertNextCell(4)
        for i in range(4):
            cells.InsertCellPoint(i)
        actor._pd.SetPoints(pts)
        actor._pd.SetPolys(cells)

    # -- sidebar content adders ------------------------------------------- #
    def section(self, text):
        a = self._text_actor(text.upper(), _SECTION_SIZE, self._hint_rgb,
                             bold=True)
        self._top_rows.append({"actor": a, "kind": "section", "lines": 1})
        return a

    def button(self, label_fn, callback, swatch=None, toggle=None, role=None):
        """A themed toggle row, styled like the main GUI's flat controls: a
        full-width card surface with a 1-px hairline border, an optional
        colour CHIP at the left (the per-scaffold cue -- chips replaced the
        old noisy full-row colour frames), and the label. `label_fn()`
        returns the current label; `callback()` runs on click. The whole row
        is the hit target (rect stored by layout()). `toggle` / `role` are the
        embedded rail's layout hints (viewer_embed.EmbedChrome.button); this
        sidebar still draws every button as a labelled row, so it ignores them."""
        outer = self._rect_actor(self._hairline_rgb)   # 1-px border...
        inner = self._rect_actor(self._card_rgb)       # ...around the surface
        chip = self._rect_actor(swatch) if swatch is not None else None
        a = vtk.vtkTextActor()
        a.SetInput(label_fn().strip())
        tp = a.GetTextProperty()
        tp.SetFontSize(_BUTTON_SIZE)
        tp.SetColor(*self._fg_rgb)
        # centred in the card, both axes: SetDisplayPosition then marks the
        # text's CENTRE, so layout() anchors it at the row's mid-point (the ttk
        # rail's ViewerBtn.TButton centres the same way -- keep them in step)
        tp.SetVerticalJustificationToCentered()
        tp.SetJustificationToCentered()
        apply_font(tp)
        tp.SetBackgroundOpacity(0.0)                   # the rects are the row
        self.chrome.AddActor2D(a)                      # text above the rects
        entry = (a, label_fn, callback)
        self.buttons.append(entry)
        self._top_rows.append({"actor": a, "kind": "button", "lines": 1,
                               "outer": outer, "inner": inner, "chip": chip,
                               "entry": entry, "hit": None})
        return a

    def slider(self, label, value_fn, lo, hi, on_set, step=None, fmt="%.3g"):
        """A themed continuous control, drawn in the SAME overlay as the buttons:
        a "label  value" line over a track with a draggable knob. `value_fn()` ->
        current value; `on_set(v)` applies a new one (press-on-track jumps, drag
        scrubs -- see install_click_handler). Matches the flat-card look."""
        track = self._rect_actor(self._hairline_rgb)
        fill = self._rect_actor(self._fg_rgb)
        knob = self._rect_actor(self._fg_rgb)
        a = self._text_actor("", _BUTTON_SIZE, self._fg_rgb)
        row = {"kind": "slider", "actor": a, "lines": 2, "track": track,
               "fill": fill, "knob": knob, "value_fn": value_fn,
               "on_set": on_set, "lo": float(lo), "hi": float(hi),
               "fmt": fmt, "label": label, "hit": None, "geom": None}
        self._sliders.append(row)
        self._top_rows.append(row)
        return a

    def transport(self, value_fn, lo, hi, on_set, text_fn=None,
                  play_label_fn=None, on_play=None):
        """A full-width bottom timeline over the 3D area (the players'
        fullscreen transport): [play/pause card] [track + knob] [time]. Drawn
        with the sidebar's exact slider/card language and hit-tested the same
        manual way (press-on-track jumps, drag scrubs) -- no VTK picking, so
        it works on the flipped native-fullscreen window where vtkSliderWidget
        does not. `text_fn()` -> the right-aligned time readout; the knob and
        text follow value_fn() on every refresh_labels(). One per chrome;
        placed by layout()."""
        accent = _hex_to_rgb(self.pal["select_bg"])
        track = self._rect_actor(self._border_rgb)
        fill = self._rect_actor(accent)
        knob = self._rect_actor(accent)
        a = self._text_actor("", _BUTTON_SIZE, self._fg_rgb)
        tp = a.GetTextProperty()
        tp.SetJustificationToRight()               # anchored at the right edge
        tp.SetVerticalJustificationToCentered()
        row = {"kind": "transport", "actor": a, "lines": 2, "track": track,
               "fill": fill, "knob": knob, "value_fn": value_fn,
               "on_set": on_set, "lo": float(lo), "hi": float(hi),
               "fmt": "%.3g", "label": "", "text_fn": text_fn,
               "hit": None, "geom": None}
        self._sliders.append(row)                  # hit-test + refresh reuse
        self._transport = row
        if on_play is not None:
            outer = self._rect_actor(self._hairline_rgb)
            inner = self._rect_actor(self._card_rgb)
            b = vtk.vtkTextActor()
            b.SetInput((play_label_fn or (lambda: "Play"))().strip())
            btp = b.GetTextProperty()
            btp.SetFontSize(_BUTTON_SIZE)
            btp.SetColor(*self._fg_rgb)
            btp.SetVerticalJustificationToCentered()
            btp.SetJustificationToCentered()
            apply_font(btp)
            self.chrome.AddActor2D(b)
            entry = (b, play_label_fn or (lambda: "Play"), on_play)
            self.buttons.append(entry)             # label refreshes with the rest
            self._transport_btn = {"kind": "button", "actor": b, "outer": outer,
                                   "inner": inner, "chip": None, "entry": entry,
                                   "hit": None}
        return a

    def _slider_text(self, row):
        fn = row.get("text_fn")
        if fn is not None:
            try:
                return fn()
            except Exception:  # noqa: BLE001
                return ""
        try:
            return f"{row['label']}  {row['fmt'] % float(row['value_fn']())}"
        except Exception:  # noqa: BLE001
            return row["label"]

    def _slider_frac(self, row):
        lo, hi = row["lo"], row["hi"]
        try:
            v = float(row["value_fn"]())
        except Exception:  # noqa: BLE001
            v = lo
        return 0.0 if hi <= lo else max(0.0, min(1.0, (v - lo) / (hi - lo)))

    def _place_slider_knob(self, row):
        """Position the fill + knob from the current value (called by layout and
        after every drag/programmatic change, so no full relayout is needed). The
        band height is read from the row (layout stashes the possibly-compressed
        value there), so a refresh centres the knob on the same band layout drew."""
        geom = row.get("geom")
        if not geom:
            return
        x0, row_w, track_y = geom
        slider_h = row.get("slh", _SLIDER_H)
        frac = self._slider_frac(row)
        knob_x = x0 + frac * (row_w - _KNOB_W)
        ty = track_y + (slider_h - _SLIDER_TRACK_H) // 2
        self._set_rect(row["fill"], x0, ty,
                       max(1.0, (knob_x + _KNOB_W / 2) - x0), _SLIDER_TRACK_H)
        self._set_rect(row["knob"], knob_x, track_y + (slider_h - _KNOB_H) // 2,
                       _KNOB_W, _KNOB_H)

    def _slider_set_from_x(self, row, cx):
        geom = row.get("geom")
        if not geom:
            return
        x0, row_w, _ = geom
        frac = max(0.0, min(1.0, (cx - x0) / max(1.0, row_w - _KNOB_W)))
        v = row["lo"] + frac * (row["hi"] - row["lo"])
        try:
            row["on_set"](v)
        except Exception:  # noqa: BLE001
            pass

    def set_hud(self, actor, max_lines=12):
        """Register the caller's live readout actor (updated every frame by the
        viewer) for a fixed slot under the wordmark. The viewer keeps ownership
        of the TEXT; the chrome owns only its POSITION + styling."""
        tp = actor.GetTextProperty()
        tp.SetFontSize(_HUD_SIZE)
        tp.SetColor(*self._fg_rgb)
        tp.SetVerticalJustificationToTop()
        tp.SetJustificationToLeft()
        apply_font(tp)
        tp.SetBackgroundOpacity(0.0)            # sits on the sidebar panel
        if actor not in self.chrome.GetActors2D():
            self.chrome.AddActor2D(actor)
        self._hud_actor = actor
        self._hud_lines = max_lines
        # a "READOUT" section header above the readout, matching the ttk rail
        # (gui_embed._build_rail) so both states carry the same headers
        if self._hud_header is None:
            self._hud_header = self._text_actor("READOUT", _SECTION_SIZE,
                                                self._hint_rgb, bold=True)

    # -- footer (bottom-anchored) ----------------------------------------- #
    def footer_section(self, text):
        a = self._text_actor(text.upper(), _SECTION_SIZE, self._hint_rgb,
                             bold=True)
        self._footer_rows.append({"actor": a, "kind": "section", "lines": 1})
        return a

    def footer_label(self, text, lines=None, hint=True):
        a = self._text_actor(text, _KEYS_SIZE,
                             self._hint_rgb if hint else self._fg_rgb)
        # left-align the (multi-line) block so key columns line up
        a.GetTextProperty().SetJustificationToLeft()
        n = lines if lines is not None else max(1, text.count("\n") + 1)
        self._footer_rows.append({"actor": a, "kind": "label", "lines": n})
        return a

    # -- autofit helpers -------------------------------------------------- #
    def _content_height1(self):
        """Total vertical pixels the sidebar content needs at scale 1.0 -- mirrors
        the EXACT advances _place() makes, so the compression factor below is
        derived from the same rhythm it then scales (drift here = wrong scale)."""
        h = _TITLE_H + _SUBTITLE_H + 8            # title, subtitle, air above rule
        if self._hud_actor is not None:
            h += _SECTION_GAP
            if self._hud_header is not None:
                h += _SECTION_H
            h += self._hud_lines * _LINE_H
        for row in list(self._top_rows) + list(self._footer_rows):
            k = row["kind"]
            if k == "section":
                h += _SECTION_GAP + _SECTION_H
            elif k == "label":
                h += row["lines"] * _LINE_H
            elif k == "slider":
                h += _LINE_H + 2 + _SLIDER_H + 6
            else:                                 # button card
                h += _BUTTON_H
        return h

    def _apply_font_scale(self, s):
        """Re-set every sidebar text actor's font size to base * s, so vertical
        compression shrinks the TYPE along with the rhythm and labels still fit
        their (now shorter) rows. s == 1.0 restores the design sizes."""
        def setf(actor, base):
            if actor is not None:
                actor.GetTextProperty().SetFontSize(
                    max(_FONT_MIN, int(round(base * s))))
        setf(self._title, _TITLE_SIZE)
        setf(self._subtitle, _SUBTITLE_SIZE)
        setf(self._hud_actor, _HUD_SIZE)
        setf(self._hud_header, _SECTION_SIZE)
        base_of = {"section": _SECTION_SIZE, "button": _BUTTON_SIZE,
                   "slider": _BUTTON_SIZE, "label": _KEYS_SIZE}
        for row in list(self._top_rows) + list(self._footer_rows):
            setf(row["actor"], base_of.get(row["kind"], _BUTTON_SIZE))
        if self._transport is not None:
            setf(self._transport["actor"], _BUTTON_SIZE)
        if self._transport_btn is not None:
            setf(self._transport_btn["actor"], _BUTTON_SIZE)

    def _fit_sidebar_width(self, w):
        """Autofit the sidebar WIDTH to the widest STATIC label (measured in the
        live font, after any compression), floored at SIDEBAR_W and capped so it
        never eats the scene. The live HUD is excluded on purpose: its text jitters
        every frame and the floor already clears its widest bounded line, so the
        width can never shake with the readout."""
        widest = 0.0
        actors = ([self._title, self._subtitle, self._hud_header]
                  + [r["actor"] for r in self._top_rows]
                  + [r["actor"] for r in self._footer_rows])
        for a in actors:
            if a is None:
                continue
            try:
                sz = [0, 0]
                a.GetSize(self.chrome, sz)
                widest = max(widest, sz[0])
            except Exception:  # noqa: BLE001 -- measurement not ready -> floor
                pass
        need = int(widest) + 2 * _PAD_X + 8
        cap = max(SIDEBAR_WIDTH_PX, int(_WIDTH_CAP_FRAC * w))
        return max(SIDEBAR_WIDTH_PX, min(need, cap))

    # -- layout ----------------------------------------------------------- #
    def layout(self, *_):
        """Place every sidebar element top-down with one consistent rhythm.
        All actors are TOP-anchored, so `y` is always the top edge of the next
        row; each block subtracts its own (possibly compressed) height + gap.

        AUTOFIT: the sidebar sizes itself to its content. It grows
        WIDER to fit the longest label (`_fit_sidebar_width`), and when the stacked
        rows would overflow a short window it COMPRESSES the whole vertical rhythm
        + type by a single factor `s` so nothing runs off the bottom -- fullscreen
        is usually tall, a short window (or many controls) triggers it. Also
        confines the 3D scene to the right of the strip."""
        w, h = self.window.GetSize()
        if w <= 0 or h <= 0:
            return

        # vertical compression: everything (top margin + content + bottom margin)
        # scales by one factor, so total = s * (16 + content + 8) must fit h.
        total1 = 16 + self._content_height1() + 8
        s = 1.0 if total1 <= h else max(_SCALE_MIN, h / float(total1))
        self._apply_font_scale(s)

        def sc(v):                                # scaled vertical measure
            return int(round(v * s))

        sw = self._fit_sidebar_width(w)           # autofit width (post font-scale)
        self._sidebar_w = sw
        sbf = min(0.9, sw / float(w))
        self.scene.SetViewport(sbf, 0.0, 1.0, 1.0)
        self._set_rect(self._divider, sw - 1, 0, 1, h)

        row_w = sw - 2 * _ROW_X
        row_h = sc(_ROW_H)
        button_h = sc(_BUTTON_H)
        section_gap = sc(_SECTION_GAP)
        section_h = sc(_SECTION_H)
        line_h = sc(_LINE_H)
        slider_h = sc(_SLIDER_H)

        x = _PAD_X
        y = h - sc(16)                            # top margin
        self._title.SetDisplayPosition(x, y)
        y -= sc(_TITLE_H)
        self._subtitle.SetDisplayPosition(x, y)
        y -= sc(_SUBTITLE_H)
        y -= sc(8)                                # air above the header rule
        self._set_rect(self._head_rule, _ROW_X, y, sw - 2 * _ROW_X, 1)

        if self._hud_actor is not None:
            y -= section_gap
            if self._hud_header is not None:
                self._hud_header.SetDisplayPosition(x, y)   # "READOUT" header
                y -= section_h
            self._hud_actor.SetDisplayPosition(x, y)   # top of the readout
            y -= sc(self._hud_lines * _LINE_H)

        def _place(rows):
            nonlocal y
            for row in rows:
                if row["kind"] == "section":
                    y -= section_gap
                    row["actor"].SetDisplayPosition(x, y)
                    y -= section_h
                elif row["kind"] == "label":       # multi-line block (keys)
                    row["actor"].SetDisplayPosition(x, y)
                    y -= row["lines"] * line_h
                elif row["kind"] == "slider":      # value line + track + knob
                    row["actor"].SetInput(self._slider_text(row))
                    row["actor"].SetDisplayPosition(x, y)
                    y -= line_h + 2
                    track_y = y - slider_h
                    self._set_rect(row["track"], _ROW_X,
                                   track_y + (slider_h - _SLIDER_TRACK_H) // 2,
                                   row_w, _SLIDER_TRACK_H)
                    row["geom"] = (_ROW_X, row_w, track_y)
                    row["slh"] = slider_h          # band height for _place_slider_knob
                    self._place_slider_knob(row)
                    row["hit"] = (_ROW_X, track_y, row_w, slider_h)
                    y -= slider_h + 6
                else:                              # button: bordered card row
                    bot = y - row_h                # rects are bottom-anchored
                    self._set_rect(row["outer"], _ROW_X, bot, row_w, row_h)
                    self._set_rect(row["inner"], _ROW_X + 1, bot + 1,
                                   row_w - 2, row_h - 2)
                    # label is centre-justified (both axes): SetDisplayPosition
                    # marks its centre, so anchor it at the row's mid-point. A
                    # colour chip (STL only) sits at the far left; centre the
                    # label in the space to its RIGHT so they never overlap.
                    text_left = _ROW_X
                    if row["chip"] is not None:
                        chip_x = _ROW_X + 8
                        self._set_rect(row["chip"], chip_x,
                                       bot + (row_h - _CHIP) // 2,
                                       _CHIP, _CHIP)
                        text_left = chip_x + _CHIP + 7
                    cx = (text_left + _ROW_X + row_w) // 2
                    cy = bot + row_h // 2
                    row["actor"].SetDisplayPosition(cx, cy)
                    row["hit"] = (_ROW_X, bot, row_w, row_h)
                    y -= button_h

        _place(self._top_rows)
        _place(self._footer_rows)                  # keys/print-time, in flow
        self._layout_transport(w)

    def _layout_transport(self, w):
        """Place the bottom timeline across the 3D area: play/pause card at
        the left, the track filling the middle, the time readout right-aligned.
        All bottom-anchored, so it survives any window size / ConfigureEvent."""
        row = self._transport
        if row is None:
            return
        x = self._sidebar_w + _TRANSPORT_MARGIN
        right = w - _TRANSPORT_MARGIN
        mid = _TRANSPORT_BOT + _ROW_H // 2         # the bar's midline (px up)
        btn = self._transport_btn
        if btn is not None:
            by = mid - _ROW_H // 2
            self._set_rect(btn["outer"], x, by, _TRANSPORT_BTN_W, _ROW_H)
            self._set_rect(btn["inner"], x + 1, by + 1,
                           _TRANSPORT_BTN_W - 2, _ROW_H - 2)
            btn["actor"].SetDisplayPosition(x + _TRANSPORT_BTN_W // 2, mid)
            btn["hit"] = (x, by, _TRANSPORT_BTN_W, _ROW_H)
            x += _TRANSPORT_BTN_W + 16
        tx1 = right
        if row.get("text_fn") is not None:
            row["actor"].SetInput(self._slider_text(row))
            row["actor"].SetDisplayPosition(right, mid)   # right-justified
            tx1 = right - _TRANSPORT_TIME_W - 14
        track_y = mid - _SLIDER_H // 2
        tw = max(40, tx1 - x)
        self._set_rect(row["track"], x,
                       track_y + (_SLIDER_H - _SLIDER_TRACK_H) // 2,
                       tw, _SLIDER_TRACK_H)
        row["geom"] = (x, tw, track_y)
        self._place_slider_knob(row)
        # a taller grab band than the sidebar rows -- it's a fullscreen target
        row["hit"] = (x, track_y - 6, tw, _SLIDER_H + 12)

    # -- labels + clicks -------------------------------------------------- #
    def refresh_labels(self):
        for a, label_fn, _cb in self.buttons:
            try:
                a.SetInput(label_fn().strip())
            except Exception:  # noqa: BLE001
                pass
        for row in self._sliders:
            try:
                row["actor"].SetInput(self._slider_text(row))
                self._place_slider_knob(row)
            except Exception:  # noqa: BLE001
                pass

    def hit_test_slider(self, cx, cy):
        for row in self._sliders:
            hit = row.get("hit")
            if hit is None:
                continue
            bx, by, bw, bh = hit
            # a generous vertical pad so the thin track is easy to grab
            if bx <= cx <= bx + bw and by - 4 <= cy <= by + bh + 4:
                return row
        return None

    def hit_test(self, cx, cy):
        # The hit region IS the row rect layout() drew (stored per row), so the
        # click target always matches the visible control exactly -- the rule
        # that fixed the old top-vs-bottom anchor mismatch stays honoured by
        # construction now.
        rows = self._top_rows
        if self._transport_btn is not None:
            rows = list(rows) + [self._transport_btn]
        for row in rows:
            if row["kind"] != "button" or row.get("hit") is None:
                continue
            bx, by, bw, bh = row["hit"]
            if bx <= cx <= bx + bw and by <= cy <= by + bh:
                return row["entry"]
        return None

    def install_click_handler(self, interactor):
        """High-priority observers: on LeftButtonPress hit-test the sidebar --
        a slider grabs (press-on-track jumps the value, then MouseMove scrubs
        until LeftButtonRelease), else a button fires its callback. Every
        handled event aborts so the orbit style never sees it. Returns the
        press observer tag."""
        holder = {}

        def _abort(obj, key):
            tag = holder.get(key)
            if tag is not None:
                try:
                    obj.GetCommand(tag).SetAbortFlag(1)
                except Exception:  # noqa: BLE001
                    pass

        def _render(self):
            try:
                self.window.Render()
            except Exception:  # noqa: BLE001
                pass

        def _on_press(obj, _evt):
            cx, cy = obj.GetEventPosition()
            # No sidebar-only shortcut here: the bottom TRANSPORT lives in the
            # 3D area, so every press goes through the hit rects (they confine
            # themselves; a miss still falls through to the orbit style).
            srow = self.hit_test_slider(cx, cy)
            if srow is not None:
                self._grab = srow
                self._slider_set_from_x(srow, cx)
                self.refresh_labels()
                _render(self)
                _abort(obj, "press")
                return
            hit = self.hit_test(cx, cy)
            if hit is None:
                return
            _a, _fn, cb = hit
            cb()
            self.refresh_labels()
            _render(self)
            _abort(obj, "press")

        def _on_move(obj, _evt):
            if self._grab is None:
                return
            cx, _cy = obj.GetEventPosition()
            self._slider_set_from_x(self._grab, cx)
            self.refresh_labels()
            _render(self)
            _abort(obj, "move")

        def _on_release(obj, _evt):
            if self._grab is None:
                return
            self._grab = None
            _abort(obj, "release")

        holder["press"] = interactor.AddObserver(
            "LeftButtonPressEvent", _on_press, 10.0)
        holder["move"] = interactor.AddObserver(
            "MouseMoveEvent", _on_move, 10.0)
        holder["release"] = interactor.AddObserver(
            "LeftButtonReleaseEvent", _on_release, 10.0)
        # tags kept so a TRANSIENT chrome (the embed child's native-fullscreen
        # sidebar) can remove its observers on teardown; the windowed viewers
        # never need them
        self._click_tag = holder["press"]
        self._move_tag = holder["move"]
        self._release_tag = holder["release"]
        self._configure_tag = interactor.AddObserver("ConfigureEvent",
                                                     self.layout)
        return holder["press"]


# -- the fullscreen RAIL: the embedded ttk rail's layout, drawn in VTK ------ #
#
# Pixel measures PORTED from the ttk rail (gui_embed.ViewerPanel._build_rail and
# the widgets it builds), read off a capture_ui render of the Viewer page at
# standard DPI. VTK draws text at 72 dpi, so a VTK size in px is the Tk point
# size x 96/72: MewBody 10 pt -> 13, MewTitle 15 pt -> 20, MewNavSection 8 pt
# bold -> 11, MewHint 9 pt -> 12.
_R_PAD_L, _R_PAD_R, _R_PAD_T = 10, 8, 8   # VScrollFrame(padding=(10, 8, 8, 8))
_R_GAP        = 8                       # gui_fonts.SP["s"] between blocks
_R_CARD_PX    = 10                      # _card: inner padx=10, pady=8
_R_CARD_PY    = 8
_R_TITLE_SIZE = 20
_R_BODY_SIZE  = 13
_R_GROUP_SIZE = 11
_R_CHIP_SIZE  = 12
_R_TITLE_H    = 26                      # the title row
_R_CHIP_W, _R_CHIP_H = 64, 18           # Paused/Playing: fixed size, both words
_R_HEADER_H   = 19                      # a readout group header (PRINT)
_R_ROW_H      = 22                      # a readout row
_R_LIST_H     = 16                      # a line of the g-code listing
_R_RULE       = 11                      # 5 px + hairline + 5 px between groups
_R_STEP_W, _R_STEP_H = 28, 20           # the readout's -/+ steppers
_R_TAB_H      = 24                      # SegmentedBar (MewNav + pady 4)
_R_TAB_PADX   = 14                      # SegmentedBar.PAD_X
_R_TAB_GAP    = 4                       # pady=(0, 4) under the tab bar
_R_TOGGLE_H   = 24                      # a switch row (pady 2)
_R_BTN_H      = 26                      # a ViewerBtn card
_R_BTN_ROW    = 4 + _R_BTN_H + 2        # pady=(4, 2)
_R_SLIDER_ROW = 2 + 20 + 18 + 1         # value line + scale, pady=(2, 1)
_R_SW_W, _R_SW_H = 30, 16               # gui_widgets.ToggleSwitch.W/H
_R_LEGEND_ROW = 20                      # the Controls popup's key rows
# the transport bar: the embedded panel's buttons, same order and widths
_R_TBTN_W, _R_TPLAY_W, _R_TSPEED_W, _R_TBTN_H = 46, 90, 46, 30


def _circle(cx, cy, r, n=20):
    import math
    return [(cx + r * math.cos(2 * math.pi * i / n),
             cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def _stadium(x, y, w, h, n=10):
    """A horizontal pill (the switch track), bottom-left at (x, y)."""
    import math
    r = h / 2.0
    pts = []
    for i in range(n + 1):                      # right cap, bottom -> top
        a = -math.pi / 2 + math.pi * i / n
        pts.append((x + w - r + r * math.cos(a), y + r + r * math.sin(a)))
    for i in range(n + 1):                      # left cap, top -> bottom
        a = math.pi / 2 + math.pi * i / n
        pts.append((x + r + r * math.cos(a), y + r + r * math.sin(a)))
    return pts


def _glyph(kind, cx, cy, s=10):
    """Transport/stepper icons as filled polygons centred on (cx, cy): drawn,
    not typed, because the bundled Helvetica has no media-control glyphs."""
    h = s / 2.0
    if kind == "minus":
        return [[(cx - h, cy - 1), (cx + h, cy - 1), (cx + h, cy + 1),
                 (cx - h, cy + 1)]]
    if kind == "plus":
        return _glyph("minus", cx, cy, s) + [
            [(cx - 1, cy - h), (cx + 1, cy - h), (cx + 1, cy + h),
             (cx - 1, cy + h)]]
    if kind == "play":
        return [[(cx - h * 0.8, cy - h), (cx + h, cy), (cx - h * 0.8, cy + h)]]
    if kind == "pause":
        return [[(cx - h, cy - h), (cx - h + 3, cy - h), (cx - h + 3, cy + h),
                 (cx - h, cy + h)],
                [(cx + h - 3, cy - h), (cx + h, cy - h), (cx + h, cy + h),
                 (cx + h - 3, cy + h)]]
    if kind == "restart":
        return [[(cx - h, cy - h), (cx - h + 2, cy - h), (cx - h + 2, cy + h),
                 (cx - h, cy + h)],
                [(cx + h, cy - h), (cx + h, cy + h), (cx - h + 3, cy)]]
    return []


class RailChrome(ViewerChrome):
    """The NATIVE-FULLSCREEN sidebar, laid out like the embedded ttk rail
    (gui_embed.ViewerPanel._build_rail) so the two states of one viewer look
    the same: a title row with the Paused/Playing chip, the readout as a
    labelled table card (with the live-edit -/+ steppers), the controls as tabs
    over one fixed-height card -- on/off controls as switches -- and the key
    legend in a card at the bottom (the embedded page keeps it behind its
    toolbar's Controls button; fullscreen has no toolbar). The transport bar
    carries the panel's restart / play / slower / speed / faster.

    Built from the SAME captured spec the ttk rail is (`source` is the child's
    viewer_embed.EmbedChrome), grouped by the SAME functions
    (viewer_sidebar_spec.rail_tabs / readout_knobs / legend_rows). The classic
    WINDOWED viewers keep the plain ViewerChrome sidebar."""

    def __init__(self, window, interactor, scene_renderer, source,
                 has_transport=False, state_fn=None, tab=None,
                 pal=None, dark=None):
        import viewer_sidebar_spec as spec
        super().__init__(window, interactor, scene_renderer,
                         title=(source.title or "").strip(), subtitle="",
                         pal=pal, dark=dark)
        self._src = source
        self._state_fn = state_fn
        self._accent = _hex_to_rgb(self.pal["select_bg"])
        self._accent_fg = _hex_to_rgb(self.pal["select_fg"])
        self._ok_rgb = _hex_to_rgb(self.pal.get("st_ok", self.pal["fg"]))
        self._hits = []                  # (x, y, w, h, entry), VTK coords
        self._text_cache = {}            # actor -> last text set
        self._h = 0

        # the base wordmark block becomes the rail's title row
        self._subtitle.VisibilityOff()
        self._head_rule.VisibilityOff()
        tp = self._title.GetTextProperty()
        tp.SetFontSize(_R_TITLE_SIZE)
        tp.SetVerticalJustificationToCentered()
        self._chip = None
        if has_transport:
            self._chip = {"outer": self._rect_actor(self._ok_rgb),
                          "inner": self._rect_actor(self._card_rgb),
                          "text": self._rtext("Paused", _R_CHIP_SIZE,
                                              self._ok_rgb, just="center")}

        controls = list(source.controls)
        self._role = {c["role"]: c["id"] for c in controls if c.get("role")}
        self._has_rows = source._readout_fn is not None

        # -- the readout ---------------------------------------------------- #
        self._table = None
        if self._has_rows:
            self._table = self._build_table(spec, controls)
        elif source._hud_actor is not None:
            self.set_hud(source._hud_actor, max_lines=source._hud_lines)
            self._hud_actor.GetTextProperty().SetFontSize(_R_BODY_SIZE)
            htp = self._hud_header.GetTextProperty()
            htp.SetFontSize(_R_GROUP_SIZE)
            htp.SetVerticalJustificationToCentered()

        # -- the tabs + the controls card ------------------------------------ #
        tabs = spec.rail_tabs(controls, self._has_rows)
        self._tab_keys = list(tabs)
        self._tab = tab if tab in tabs else (self._tab_keys[0]
                                             if self._tab_keys else None)
        self._segs = {}
        if len(self._tab_keys) > 1:
            self._seg_frame = self._rect_actor(self._hairline_rgb)
            for key in self._tab_keys:
                self._segs[key] = {
                    "rect": self._rect_actor(self._card_rgb),
                    "text": self._rtext(key, _R_BODY_SIZE, self._fg_rgb,
                                        just="center")}
        # a card's rects are added BEFORE its contents: the overlay draws in
        # the order actors were added, so a later card would paint over them
        self._ctl_card = self._card_pair() if self._tab_keys else None
        self._items = {}
        for key, specs in tabs.items():
            built = [self._build_item(s) for s in specs]
            self._items[key] = [it for it in built if it is not None]

        # -- the key legend --------------------------------------------------- #
        self._legend = None
        rows = spec.legend_rows(source.footers)
        if rows:
            card = self._card_pair()
            secs = []
            for section, pairs in rows:
                hdr = (self._rtext(section.upper(), _R_GROUP_SIZE,
                                   self._hint_rgb, bold=True)
                       if section else None)
                secs.append((hdr, [
                    (self._rtext(k, _R_BODY_SIZE, self._fg_rgb),
                     self._rtext(a, _R_BODY_SIZE, self._hint_rgb)
                     if a else None)
                    for k, a in pairs]))
            self._legend = {"card": card, "secs": secs}

        self._transport_extra = []       # the panel's other transport buttons
        self._play_icon = None
        self._play_x = None
        self._speed_text = None

    # -- factories ---------------------------------------------------------- #
    def _rtext(self, text, size, rgb, bold=False, just="left"):
        """A rail text actor, vertically CENTRED: placed at its row's mid-line."""
        a = self._text_actor(text, size, rgb, bold=bold)
        tp = a.GetTextProperty()
        tp.SetVerticalJustificationToCentered()
        if just == "center":
            tp.SetJustificationToCentered()
        elif just == "right":
            tp.SetJustificationToRight()
        return a

    def _card_pair(self):
        """(border, surface) rects: a hairline-ringed card in the card tone."""
        return (self._rect_actor(self._hairline_rgb),
                self._rect_actor(self._card_rgb))

    @staticmethod
    def _set_polys(actor, polys):
        """Fill a _rect_actor with arbitrary convex polygons (icons, the
        switch's pill and knob)."""
        pts = vtk.vtkPoints()
        cells = vtk.vtkCellArray()
        for poly in polys:
            cells.InsertNextCell(len(poly))
            for (x, y) in poly:
                cells.InsertCellPoint(pts.InsertNextPoint(x, y, 0.0))
        actor._pd.SetPoints(pts)
        actor._pd.SetPolys(cells)
        actor._pd.Modified()

    def _entry(self, cid):
        return self._src._buttons_by_id.get(cid) if cid else None

    def _build_table(self, spec, controls):
        knobs = spec.readout_knobs(controls)
        rows = list(spec.READOUT_ROWS)
        reset_id = self._role.get("knob:reset")
        t = {"card": self._card_pair(), "rows": [], "headers": {},
             "rules": [], "status": self._rtext("", _R_BODY_SIZE,
                                                self._hint_rgb),
             "showing_status": None,
             # the g-code listing, drawn in place of the Line and Move rows
             # while the viewer's "G-code listing" switch is on
             "listing": {"on": False, "ix": 0, "iw": 0, "top": 0,
                         "cur": self._rect_actor(self._accent),
                         "lines": [self._rtext("", _R_BODY_SIZE, self._fg_rgb)
                                   for _ in range(spec.LISTING_ROWS)]}}
        for a in [t["listing"]["cur"]] + t["listing"]["lines"]:
            a.SetVisibility(0)
        group = None
        for i, row in enumerate(rows):
            g = row.get("g", "")
            if g != group:
                if group is not None:
                    t["rules"].append(self._rect_actor(self._hairline_rgb))
                if g:
                    t["headers"][g] = self._rtext(g.upper(), _R_GROUP_SIZE,
                                                  self._hint_rgb, bold=True)
                group = g
            r = {"g": g, "k": row["k"], "sample": row["sample"],
                 "key": self._rtext(row["k"], _R_BODY_SIZE, self._hint_rgb),
                 "knob": None, "reset": None}
            knob = row.get("knob") if row.get("knob") in knobs else None
            if knob:
                r["val"] = self._rtext(spec.EMPTY_VALUE, _R_BODY_SIZE,
                                       self._fg_rgb, just="center")
                r["knob"] = [self._stepper(f"knob:{knob}:-", "minus"),
                             self._stepper(f"knob:{knob}:+", "plus")]
                last = not any(x.get("knob") in knobs and x.get("g") == g
                               for x in rows[i + 1:])
                if last and reset_id is not None:
                    r["reset"] = self._rail_button(reset_id, label="Reset")
            else:
                r["val"] = self._rtext(spec.EMPTY_VALUE, _R_BODY_SIZE,
                                       self._fg_rgb)
            t["rows"].append(r)
        return t

    def _stepper(self, role, glyph):
        outer, inner = self._card_pair()
        return {"outer": outer, "inner": inner, "glyph": glyph,
                "icon": self._rect_actor(self._fg_rgb),
                "entry": self._entry(self._role.get(role))}

    def _rail_button(self, cid, label=None):
        """A ViewerBtn card: bordered, centred label (live via label_fn)."""
        entry = self._entry(cid)
        if entry is None:
            return None
        _carrier, label_fn, cb = entry
        outer, inner = self._card_pair()
        fn = (lambda: label) if label is not None else label_fn
        a = self._rtext(fn().strip(), _R_BODY_SIZE, self._fg_rgb,
                        just="center")
        e = (a, fn, cb)
        self.buttons.append(e)               # base refresh_labels keeps it live
        return {"kind": "button", "outer": outer, "inner": inner, "text": a,
                "entry": e}

    def _build_item(self, spec_row):
        ctype = spec_row.get("type", "button")
        cid = spec_row["id"]
        if ctype == "button":
            if spec_row.get("toggle"):
                entry = self._entry(cid)
                if entry is None:
                    return None
                return {"kind": "toggle", "entry": entry,
                        "state_fn": self._src._toggle_fns.get(cid),
                        "name": self._rtext(spec_row["toggle"], _R_BODY_SIZE,
                                            self._fg_rgb),
                        "track": self._rect_actor(self._hairline_rgb),
                        "knob": self._rect_actor(self._hint_rgb),
                        "geom": None, "on": None}
            return self._rail_button(cid)
        # slider / number: a label + value line over a track (a number has no
        # text entry in a VTK overlay, so it is a slider over its range here)
        vc = self._src._value_ctrls.get(cid)
        if vc is None:
            return None
        lo, hi = spec_row.get("min"), spec_row.get("max")
        if lo is None or hi is None:
            cur = float(vc["value_fn"]())
            span = max(abs(cur), 1.0)
            lo = cur - span if lo is None else lo
            hi = cur + span if hi is None else hi
        fmt = vc.get("fmt", "%.3g")
        row = {"kind": "slider", "lines": 2,
               "actor": self._rtext("", _R_BODY_SIZE, self._fg_rgb,
                                    just="right"),
               "track": self._rect_actor(self._hairline_rgb),
               "fill": self._rect_actor(self._accent),
               "knob": self._rect_actor(self._accent),
               "value_fn": vc["value_fn"], "on_set": vc["on_set"],
               "lo": float(lo), "hi": float(hi), "fmt": fmt, "label": "",
               "text_fn": (lambda f=fmt, fn=vc["value_fn"]: f % float(fn())),
               "hit": None, "geom": None,
               "name": self._rtext(spec_row.get("label", ""), _R_BODY_SIZE,
                                   self._hint_rgb)}
        self._sliders.append(row)
        return row

    # -- the transport bar ---------------------------------------------------- #
    def transport(self, value_fn, lo, hi, on_set, text_fn=None,
                  play_label_fn=None, on_play=None):
        a = super().transport(value_fn, lo, hi, on_set, text_fn=text_fn,
                              play_label_fn=play_label_fn, on_play=on_play)
        if self._transport_btn is not None:
            self._play_icon = self._rect_actor(self._fg_rgb)
            ptp = self._transport_btn["actor"].GetTextProperty()
            ptp.SetFontSize(_R_BODY_SIZE)
            ptp.SetJustificationToLeft()
        for role, glyph in (("restart", "restart"), ("slower", "minus"),
                            ("faster", "plus")):
            entry = self._entry(self._role.get(role))
            if entry is None:
                continue
            outer, inner = self._card_pair()
            self._transport_extra.append(
                {"role": role, "outer": outer, "inner": inner, "glyph": glyph,
                 "icon": self._rect_actor(self._fg_rgb), "entry": entry,
                 "hit": None})
        if any(b["role"] in ("slower", "faster") for b in self._transport_extra):
            self._speed_text = self._rtext("", _R_BODY_SIZE, self._hint_rgb,
                                           just="center")
        ttp = self._transport["actor"].GetTextProperty()
        ttp.SetColor(*self._hint_rgb)
        ttp.SetFontSize(_R_BODY_SIZE)
        return a

    def _layout_transport(self, w):
        """The panel's transport order: restart, play, slower, speed, faster,
        the track, the time -- bottom-anchored across the 3D area."""
        row = self._transport
        if row is None:
            return
        x = self._sidebar_w + _TRANSPORT_MARGIN
        right = w - _TRANSPORT_MARGIN
        by = _TRANSPORT_BOT
        mid = by + _R_TBTN_H // 2
        extra = {b["role"]: b for b in self._transport_extra}

        def card(b, bw):
            self._set_rect(b["outer"], x, by, bw, _R_TBTN_H)
            self._set_rect(b["inner"], x + 1, by + 1, bw - 2, _R_TBTN_H - 2)
            self._set_polys(b["icon"], _glyph(b["glyph"], x + bw / 2.0, mid))
            b["hit"] = (x, by, bw, _R_TBTN_H)

        if "restart" in extra:
            card(extra["restart"], _R_TBTN_W)
            x += _R_TBTN_W + 4
        btn = self._transport_btn
        if btn is not None:
            self._set_rect(btn["outer"], x, by, _R_TPLAY_W, _R_TBTN_H)
            self._set_rect(btn["inner"], x + 1, by + 1, _R_TPLAY_W - 2,
                           _R_TBTN_H - 2)
            btn["hit"] = (x, by, _R_TPLAY_W, _R_TBTN_H)
            self._play_x = x
            self._place_play_label()
            x += _R_TPLAY_W + 10
        if "slower" in extra:
            card(extra["slower"], _R_TBTN_W)
            x += _R_TBTN_W
        if self._speed_text is not None:
            self._speed_text.SetDisplayPosition(int(x + _R_TSPEED_W / 2), mid)
            x += _R_TSPEED_W
        if "faster" in extra:
            card(extra["faster"], _R_TBTN_W)
            x += _R_TBTN_W
        if extra or btn is not None:
            x += 10
        tx1 = right
        if row.get("text_fn") is not None:
            row["actor"].SetInput(self._slider_text(row))
            row["actor"].SetDisplayPosition(right, mid)
            tx1 = right - _TRANSPORT_TIME_W - 8
        track_y = mid - _SLIDER_H // 2
        tw = max(40, tx1 - x)
        self._set_rect(row["track"], x,
                       track_y + (_SLIDER_H - _SLIDER_TRACK_H) // 2,
                       tw, _SLIDER_TRACK_H)
        row["geom"] = (x, tw, track_y)
        self._place_slider_knob(row)
        row["hit"] = (x, track_y - 6, tw, _SLIDER_H + 12)

    def _place_play_label(self):
        """Icon + word, centred together in the play card (the panel's
        "▶  Play" / "❚❚  Pause")."""
        btn = self._transport_btn
        x = self._play_x
        if btn is None or x is None:
            return
        a = btn["actor"]
        playing = (a.GetInput() or "").strip().lower().startswith("pause")
        tw = self._w(a) or 36
        icon_w, gap = 10, 8
        x0 = x + (_R_TPLAY_W - (icon_w + gap + tw)) / 2.0
        mid = _TRANSPORT_BOT + _R_TBTN_H // 2
        if self._play_icon is not None:
            self._set_polys(self._play_icon,
                            _glyph("pause" if playing else "play",
                                   x0 + icon_w / 2.0, mid))
        a.SetDisplayPosition(int(x0 + icon_w + gap), mid)

    # -- width -------------------------------------------------------------- #
    def _w(self, actor, text=None):
        """An actor's rendered width in px (optionally with `text` in it)."""
        if actor is None:
            return 0
        old = None
        if text is not None:
            old = actor.GetInput()
            actor.SetInput(text)
        sz = [0, 0]
        try:
            actor.GetSize(self.chrome, sz)
        except Exception:  # noqa: BLE001
            sz = [0, 0]
        if old is not None:
            actor.SetInput(old)
        return sz[0]

    def _keycol(self):
        return max([self._w(r["key"]) for r in self._table["rows"]]
                   or [0]) + 10

    def _fit_rail_width(self, w):
        """The ttk rail's rule (_fit_rail_width): its content's width in the
        live font, + 44, floored at SIDEBAR_W and capped at twice that -- and
        here also at a fraction of the window, so it never eats the scene."""
        card = 2 + 2 * _R_CARD_PX
        need = self._w(self._title) + 8 + (_R_CHIP_W if self._chip else 0)
        if self._table is not None:
            kc = self._keycol()
            for r in self._table["rows"]:
                vw = self._w(r["val"], r["sample"])
                if r["knob"]:
                    vw += 2 * _R_STEP_W + 8
                need = max(need, card + kc + vw)
        if self._segs:
            need = max(need, 2 + len(self._segs) - 1 + sum(
                self._w(s["text"]) + 2 * _R_TAB_PADX
                for s in self._segs.values()))
        for items in self._items.values():
            for it in items:
                if it["kind"] == "toggle":
                    need = max(need, card + self._w(it["name"]) + 8 + 32)
                elif it["kind"] == "button":
                    need = max(need, card + self._w(it["text"]) + 2 * 10 + 2)
                else:
                    need = max(need, card + self._w(it["name"]) + 8 + 60)
        want = max(SIDEBAR_WIDTH_PX, min(need + 44, 2 * SIDEBAR_WIDTH_PX))
        cap = max(SIDEBAR_WIDTH_PX, int(_WIDTH_CAP_FRAC * w))
        return min(want, cap)

    # -- layout ------------------------------------------------------------- #
    # The rail lays out TOP-DOWN in "yt" (px down from the window top), the
    # way Tk does; these helpers turn that into VTK's bottom-up coordinates.
    def _box(self, actor, x, yt, bw, bh):
        self._set_rect(actor, x, self._h - yt - bh, bw, bh)

    def _card_box(self, pair, x, yt, bw, bh):
        self._box(pair[0], x, yt, bw, bh)
        self._box(pair[1], x + 1, yt + 1, bw - 2, bh - 2)

    def _at(self, actor, x, yc):
        actor.SetDisplayPosition(int(round(x)), int(round(self._h - yc)))

    def _hit(self, x, yt, bw, bh, entry):
        if entry is not None:
            self._hits.append((x, self._h - yt - bh, bw, bh, entry))

    @staticmethod
    def _show(actors, on):
        for a in actors:
            if a is not None:
                a.SetVisibility(1 if on else 0)

    def layout(self, *_):
        w, h = self.window.GetSize()
        if w <= 0 or h <= 0:
            return
        self._h = h
        self._hits = []
        sw = self._fit_rail_width(w)
        self._sidebar_w = sw
        self.scene.SetViewport(min(0.9, sw / float(w)), 0.0, 1.0, 1.0)
        self._set_rect(self._divider, sw - 1, 0, 1, h)
        x0 = _R_PAD_L
        cw = sw - _R_PAD_L - _R_PAD_R
        yt = _R_PAD_T

        # title row: the name left, the chip right
        self._at(self._title, x0, yt + _R_TITLE_H / 2.0)
        if self._chip is not None:
            cx = x0 + cw - _R_CHIP_W
            cy = yt + (_R_TITLE_H - _R_CHIP_H) // 2
            self._box(self._chip["outer"], cx, cy, _R_CHIP_W, _R_CHIP_H)
            self._box(self._chip["inner"], cx + 1, cy + 1, _R_CHIP_W - 2,
                      _R_CHIP_H - 2)
            self._at(self._chip["text"], cx + _R_CHIP_W / 2.0,
                     cy + _R_CHIP_H / 2.0)
        yt += _R_TITLE_H + _R_GAP

        if self._table is not None:
            yt = self._layout_table(x0, yt, cw) + _R_GAP
        elif self._hud_actor is not None:
            self._at(self._hud_header, x0, yt + _R_HEADER_H / 2.0)
            yt += _R_HEADER_H
            self._hud_actor.SetDisplayPosition(x0, h - yt)
            yt += self._hud_lines * 17 + _R_GAP

        if self._segs:
            yt = self._layout_tabs(x0, yt, cw) + _R_TAB_GAP
        if self._ctl_card is not None:
            yt = self._layout_controls(x0, yt, cw)
        if self._legend is not None:
            self._layout_legend(x0, yt + _R_GAP, cw)
        self._layout_transport(w)
        self.refresh_labels()

    def _layout_table(self, x0, yt, cw):
        t = self._table
        ix = x0 + 1 + _R_CARD_PX
        iw = cw - 2 - 2 * _R_CARD_PX
        kc = self._keycol()
        top = yt
        iy = yt + 1 + _R_CARD_PY
        group = None
        rules = iter(t["rules"])
        lst = t["listing"]
        for r in t["rows"]:
            g = r["g"]
            if g != group:
                if group is not None:
                    self._box(next(rules), ix, iy + 5, iw, 1)
                    iy += _R_RULE
                if g:
                    self._at(t["headers"][g], ix, iy + _R_HEADER_H / 2.0)
                    iy += _R_HEADER_H
                group = g
            if lst["on"] and (g, r["k"]) in self._listing_rows():
                if (g, r["k"]) == self._listing_rows()[0]:
                    # the listing stands in for the Line and Move rows
                    lst.update(ix=ix, iw=iw, top=iy)
                    iy += len(lst["lines"]) * _R_LIST_H
                continue
            yc = iy + _R_ROW_H / 2.0
            self._at(r["key"], ix, yc)
            if r["knob"]:
                vx = ix + kc
                vw = iw - kc
                bm, bp = r["knob"]
                sy = iy + (_R_ROW_H - _R_STEP_H) // 2
                self._step_box(bm, vx, sy)
                self._step_box(bp, vx + vw - _R_STEP_W, sy)
                self._at(r["val"], vx + vw / 2.0, yc)
            else:
                self._at(r["val"], ix + kc, yc)
            iy += _R_ROW_H
            if r["reset"] is not None:
                rb = r["reset"]
                bw = self._w(rb["text"]) + 2 * 10 + 2
                bx = ix + iw - bw
                self._card_box((rb["outer"], rb["inner"]), bx, iy + 3, bw,
                               _R_BTN_H)
                self._at(rb["text"], bx + bw / 2.0, iy + 3 + _R_BTN_H / 2.0)
                self._hit(bx, iy + 3, bw, _R_BTN_H, rb["entry"])
                iy += 3 + _R_BTN_H
        bottom = iy + _R_CARD_PY + 1
        self._card_box(t["card"], x0, top, cw, bottom - top)
        # a transient status ("Computing deviation…") sits where the table is
        self._at(t["status"], ix, top + 1 + _R_CARD_PY + _R_ROW_H / 2.0)
        return bottom

    def _step_box(self, b, x, yt):
        self._card_box((b["outer"], b["inner"]), x, yt, _R_STEP_W, _R_STEP_H)
        self._set_polys(b["icon"], _glyph(
            b["glyph"], x + _R_STEP_W / 2.0, self._h - yt - _R_STEP_H / 2.0,
            s=8))
        self._hit(x, yt, _R_STEP_W, _R_STEP_H, b["entry"])

    def _layout_tabs(self, x0, yt, cw):
        """The SegmentedBar: one hairline-ringed bar, segments split by 1-px
        hairlines, each its natural width plus an equal share of the slack
        (Tk's pack expand=True), the live one filled in the accent."""
        self._box(self._seg_frame, x0, yt, cw, _R_TAB_H)
        nat = {k: self._w(s["text"]) + 2 * _R_TAB_PADX
               for k, s in self._segs.items()}
        n = len(self._segs)
        extra = (cw - 2 - (n - 1) - sum(nat.values())) / float(n)
        x = x0 + 1.0
        for i, (key, s) in enumerate(self._segs.items()):
            sw = nat[key] + extra
            if i == n - 1:
                sw = x0 + cw - 1 - x              # absorb rounding
            sel = key == self._tab
            s["rect"].GetProperty().SetColor(
                *(self._accent if sel else self._card_rgb))
            s["text"].GetTextProperty().SetColor(
                *(self._accent_fg if sel else self._fg_rgb))
            sx, sww = int(round(x)), int(round(sw))
            self._box(s["rect"], sx, yt + 1, sww, _R_TAB_H - 2)
            self._at(s["text"], x + sw / 2.0, yt + _R_TAB_H / 2.0)
            self._hit(sx, yt, sww, _R_TAB_H,
                      (s["text"], (lambda k=key: k),
                       (lambda k=key: self._select_tab(k))))
            x += sw + 1
        return yt + _R_TAB_H

    def _select_tab(self, key):
        if key != self._tab:
            self._tab = key
            self.layout()

    @staticmethod
    def _item_h(it):
        return {"toggle": _R_TOGGLE_H, "button": _R_BTN_ROW,
                "slider": _R_SLIDER_ROW}[it["kind"]]

    @staticmethod
    def _item_actors(it):
        k = it["kind"]
        if k == "toggle":
            return [it["name"], it["track"], it["knob"]]
        if k == "button":
            return [it["outer"], it["inner"], it["text"]]
        return [it["name"], it["actor"], it["track"], it["fill"], it["knob"]]

    def _layout_controls(self, x0, yt, cw):
        """Every tab's items in ONE card as tall as the tallest tab, so
        switching tabs never moves anything (the ttk rail stacks its panes in
        one grid cell for the same reason)."""
        ix = x0 + 1 + _R_CARD_PX
        iw = cw - 2 - 2 * _R_CARD_PX
        tallest = max([sum(self._item_h(it) for it in items)
                       for items in self._items.values()] or [0])
        card_h = tallest + 2 + 2 * _R_CARD_PY
        self._card_box(self._ctl_card, x0, yt, cw, card_h)
        for key, items in self._items.items():
            shown = key == self._tab
            iy = yt + 1 + _R_CARD_PY
            for it in items:
                self._show(self._item_actors(it), shown)
                if it["kind"] == "slider":
                    it["hit"] = None
                if not shown:
                    continue
                k = it["kind"]
                if k == "toggle":
                    yc = iy + _R_TOGGLE_H / 2.0
                    self._at(it["name"], ix, yc)
                    sx = ix + iw - _R_SW_W - 1
                    it["geom"] = (sx, self._h - yc - _R_SW_H / 2.0)
                    it["on"] = None                   # repaint at the new spot
                    self._paint_switch(it)
                    self._hit(ix, iy, iw, _R_TOGGLE_H, it["entry"])
                elif k == "button":
                    by = iy + 4
                    self._card_box((it["outer"], it["inner"]), ix, by, iw,
                                   _R_BTN_H)
                    self._at(it["text"], ix + iw / 2.0, by + _R_BTN_H / 2.0)
                    self._hit(ix, by, iw, _R_BTN_H, it["entry"])
                else:
                    yc = iy + 2 + 10
                    self._at(it["name"], ix, yc)
                    self._at(it["actor"], ix + iw, yc)
                    track_y = self._h - (iy + 2 + 20 + 18)
                    self._set_rect(it["track"], ix,
                                   track_y + (_SLIDER_H - _SLIDER_TRACK_H) // 2,
                                   iw, _SLIDER_TRACK_H)
                    it["geom"] = (ix, iw, track_y)
                    it["slh"] = _SLIDER_H
                    self._place_slider_knob(it)
                    it["hit"] = (ix, track_y, iw, _SLIDER_H)
                iy += self._item_h(it)
        return yt + card_h

    def _paint_switch(self, it):
        """gui_widgets.ToggleSwitch, drawn: a pill track with a round knob,
        accent + knob right while on, hairline + knob left while off."""
        fn = it.get("state_fn")
        try:
            on = bool(fn()) if fn is not None else False
        except Exception:  # noqa: BLE001
            on = False
        if on == it["on"] or it["geom"] is None:
            return
        it["on"] = on
        sx, sy = it["geom"]
        it["track"].GetProperty().SetColor(
            *(self._accent if on else self._hairline_rgb))
        it["knob"].GetProperty().SetColor(
            *(self._accent_fg if on else self._hint_rgb))
        self._set_polys(it["track"], [_stadium(sx, sy + 1, _R_SW_W - 2,
                                               _R_SW_H - 2)])
        d = _R_SW_H - 6
        kx = sx + (_R_SW_W - _R_SW_H + 3 if on else 3) + d / 2.0
        self._set_polys(it["knob"], [_circle(kx, sy + _R_SW_H / 2.0, d / 2.0)])

    def _layout_legend(self, x0, flow_top, cw):
        """The key legend (the panel's Controls popup) as a card pinned to the
        sidebar's bottom -- or straight under the controls when the window is
        too short for both."""
        lg = self._legend
        ix = x0 + 12
        keyw = max([self._w(k) for _hdr, pairs in lg["secs"]
                    for k, a in pairs if a is not None] or [0]) + 14
        hgt = 0
        for i, (hdr, pairs) in enumerate(lg["secs"]):
            if hdr is not None:
                hgt += (10 if i == 0 else 8) + _R_HEADER_H - 2
            hgt += len(pairs) * _R_LEGEND_ROW
        hgt += 8 + 2
        top = max(flow_top, self._h - _R_PAD_T - hgt)
        self._card_box(lg["card"], x0, top, cw, hgt)
        iy = top + 1
        for i, (hdr, pairs) in enumerate(lg["secs"]):
            if hdr is not None:
                iy += 10 if i == 0 else 8
                self._at(hdr, ix, iy + (_R_HEADER_H - 2) / 2.0)
                iy += _R_HEADER_H - 2
            for k, a in pairs:
                yc = iy + _R_LEGEND_ROW / 2.0
                self._at(k, ix, yc)
                if a is not None:
                    self._at(a, ix + keyw, yc)
                iy += _R_LEGEND_ROW

    # -- live state --------------------------------------------------------- #
    def _set_text(self, actor, text):
        if self._text_cache.get(actor) != text:
            self._text_cache[actor] = text
            actor.SetInput(text)

    def refresh_labels(self):
        """Per tick while native: labels, slider values, the chip, the speed,
        the switches and the readout values (text and colour only -- nothing
        here moves a row, so no relayout)."""
        super().refresh_labels()
        state = {}
        if self._state_fn is not None:
            try:
                state = self._state_fn() or {}
            except Exception:  # noqa: BLE001
                state = {}
        playing = bool(state.get("playing"))
        if self._chip is not None:
            ch = self._chip
            ch["outer"].GetProperty().SetColor(
                *(self._accent if playing else self._ok_rgb))
            ch["inner"].GetProperty().SetColor(
                *(self._accent if playing else self._card_rgb))
            ch["text"].GetTextProperty().SetColor(
                *(self._accent_fg if playing else self._ok_rgb))
            self._set_text(ch["text"], "Playing" if playing else "Paused")
        if self._speed_text is not None and "speed" in state:
            try:
                self._set_text(self._speed_text, f"x{float(state['speed']):g}")
            except (TypeError, ValueError):
                pass
        self._place_play_label()
        for items in self._items.values():
            for it in items:
                if it["kind"] == "toggle":
                    self._paint_switch(it)
        if self._table is not None:
            self._refresh_table()

    def _refresh_table(self):
        import viewer_sidebar_spec as spec
        t = self._table
        ro = self._src.readout()
        rows, status = ro if ro is not None else ([], "")
        got = {(r.get("g", ""), r.get("k", "")): r for r in rows}
        for r in t["rows"]:
            v = got.get((r["g"], r["k"]))
            self._set_text(r["val"], v.get("v", "") if v is not None
                           else spec.EMPTY_VALUE)
        show_status = bool(status)
        self._set_text(t["status"], status or "")
        if t["showing_status"] != show_status:
            t["showing_status"] = show_status
            table = list(t["headers"].values()) + list(t["rules"])
            for r in t["rows"]:
                table += [r["key"], r["val"]]
                for b in r["knob"] or []:
                    table += [b["outer"], b["inner"], b["icon"]]
                if r["reset"]:
                    table += [r["reset"]["outer"], r["reset"]["inner"],
                              r["reset"]["text"]]
            self._show(table, not show_status)
            self._show([t["status"]], show_status)
        self._refresh_listing(spec, t, show_status)

    def _listing_rows(self):
        import viewer_sidebar_spec as spec
        return spec.LISTING_REPLACES

    def _refresh_listing(self, spec, t, show_status):
        """The g-code listing: switching it on or off moves every row under
        it, so that is a relayout; a tick only rewrites its lines."""
        lst = t["listing"]
        data = self._src.listing()
        on = bool(data and data.get("lines"))
        if on != lst["on"]:
            lst["on"] = on
            self.layout()
            return
        replaced = [r for r in t["rows"]
                    if (r["g"], r["k"]) in spec.LISTING_REPLACES]
        for r in replaced:
            self._show([r["key"], r["val"]], not show_status and not on)
        shown = on and not show_status
        self._show([lst["cur"]] + lst["lines"], shown)
        if not on:
            return
        lines = data["lines"]
        wide = len(str(lines[-1][0]))
        row_y = lst["top"]
        for i, actor in enumerate(lst["lines"]):
            if i >= len(lines):
                self._set_text(actor, "")
                continue
            n, body = lines[i]
            room = max(spec.LISTING_COLS - wide - 2, 4)
            if len(body) > room:
                body = body[:room - 1] + "\u2026"
            now = n == data["cur"]
            self._set_text(actor, f"{str(n).rjust(wide)}  {body}")
            actor.GetTextProperty().SetColor(
                *(self._accent_fg if now else self._fg_rgb))
            yc = row_y + (i + 0.5) * _R_LIST_H
            self._at(actor, lst["ix"] + 4, yc)
            if now:
                self._box(lst["cur"], lst["ix"], row_y + i * _R_LIST_H,
                          lst["iw"], _R_LIST_H)

    # -- clicks ------------------------------------------------------------- #
    def hit_test(self, cx, cy):
        def inside(rect):
            bx, by, bw, bh = rect
            return bx <= cx <= bx + bw and by <= cy <= by + bh

        for bx, by, bw, bh, entry in self._hits:
            if inside((bx, by, bw, bh)):
                return entry
        for b in self._transport_extra:
            if b["hit"] is not None and inside(b["hit"]):
                return b["entry"]
        btn = self._transport_btn
        if btn is not None and btn.get("hit") is not None and inside(btn["hit"]):
            return btn["entry"]
        return None
