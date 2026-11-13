"""gui_physics_diagrams.py -- the picture on each Physics-page card.

Each law card (and Process, and Export/Visualization) shows what its values
control: the fiber, the lengths the symbols name, and what changing each one
does. The pictures are 3D renders of fibers with dimension lines and symbols on
them, made at build time by `assets/physics/make_physics_figures.py`; this
module only shows them. One PNG per card and theme, `<card>_<dark|light>.png`,
authored at 2x a 96-dpi screen.

Placement and size. `mount(body, key, first_row, last_row, under_row)` puts
the picture in column 1 of a card body, beside the fields in column 0, spanning
the given rows plus `under_row`. That last row is empty and the only one with
weight, so when the picture is taller than the fields the slack collects under
them instead of being spread between them (Tk shares a spanning widget's extra
height evenly over unweighted rows).

Every card looks the same. The fields column is at least FIELDS_WIDTH wide, so
every picture starts at the same x. Every picture is a row of identical panels,
and every card is drawn at ONE scale: the display's own (the Tk `1i` length
over 96), times what fits the widest picture (FULL_WIDTH, three panels) into the
room. So a panel is the same size on a one-panel card as on a three-panel one.
When that room holds less than MIN_SHARE of FULL_WIDTH, the picture moves under
the fields and takes the body's full width.

No size feedback. The picture reads the BODY's width and column 0's width,
neither of which depends on the picture: column 0 carries no weight and holds
only the fields, so a resized picture cannot resize what it was measured from.

A missing PNG (a stripped checkout) leaves the card without a picture rather
than failing to build it.
"""

import os
import tkinter as tk

import gui_widgets

try:
    from app_paths import resource_path as _resource_path
except ImportError:                     # a stripped-down import path (tests)
    def _resource_path(*parts):
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), *parts)

FIGURE_DIR = _resource_path("assets", "physics")
AUTHORED_SCALE = 2          # the PNGs are 2x their logical size
GAP = 32                    # logical px between the fields and the picture
# Every picture is a row of identical panels (make_physics_figures.Panels):
# FULL_WIDTH is the widest, MAX_PANELS (3) panels of 360 with 20 between.
# Every card is drawn at the scale that fits FULL_WIDTH into its room, so a
# one-panel card's panel is exactly the size of a three-panel card's.
FULL_WIDTH = 3 * 360 + 2 * 20
# The fields column is this wide on every card (wider only where a card's own
# fields need it), so every picture starts at the same x.
FIELDS_WIDTH = 470
# Narrower than this share of FULL_WIDTH beside the fields, the picture goes
# underneath them instead, where it has the card's whole width.
MIN_SHARE = 0.62

try:
    from PIL import Image, ImageTk
except ImportError:         # the picture is decoration; never block the card
    Image = ImageTk = None


def _is_dark(pal):
    h = pal.get("bg", "#ffffff").lstrip("#")
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return False
    return (0.299 * r + 0.587 * g + 0.114 * b) < 128


def figure_path(key, dark):
    return os.path.join(FIGURE_DIR, "%s_%s.png" % (key, "dark" if dark
                                                     else "light"))


def available(key):
    """True when both theme variants of `key`'s picture are on disk."""
    return all(os.path.isfile(figure_path(key, d)) for d in (True, False))


class PhysicsFigure(tk.Label):
    """One card's picture. Rethemes with the app and resizes with the card."""

    _sources = {}           # (path) -> PIL image, shared by every card

    def __init__(self, body, key, first_row, last_row, under_row):
        super().__init__(body, bd=0, highlightthickness=0, takefocus=False)
        self.key = key
        self._body = body
        self._rows = (first_row, last_row)
        self._under_row = under_row
        self._beside = None
        self._width = None
        self._dark = None
        self._photo = None
        self._pending = None
        body.columnconfigure(0, weight=0, minsize=self._px(FIELDS_WIDTH))
        body.columnconfigure(1, weight=1)
        self._place(True)
        body.bind("<Configure>", self._schedule, add="+")
        gui_widgets.register(self)
        self.retheme()

    # -- layout -------------------------------------------------------------- #
    def _place(self, beside):
        if beside == self._beside:
            return
        self._beside = beside
        first, _last = self._rows
        if beside:
            self._body.rowconfigure(self._under_row, weight=1)
            self.grid(row=first, column=1,
                      rowspan=self._under_row - first + 1,
                      sticky="nw", padx=(self._px(GAP), 0))
        else:
            self._body.rowconfigure(self._under_row, weight=0)
            self.grid(row=self._under_row, column=0, columnspan=2,
                      sticky="nw", padx=0, pady=(self._px(16), 0))

    def _px(self, logical):
        return int(round(logical * self._scale()))

    def _scale(self):
        try:
            return max(1.0, float(self.winfo_fpixels("1i")) / 96.0)
        except tk.TclError:
            return 1.0

    def _schedule(self, _event=None):
        if self._pending is None:
            self._pending = self.after_idle(self._fit)

    def _fit(self):
        self._pending = None
        src = self._source()
        if src is None:
            return
        try:
            body_w = self._body.winfo_width()
            col0 = self._body.grid_bbox(0, self._rows[0], 0,
                                        self._rows[1])[2]
        except tk.TclError:
            return
        if body_w <= 1:
            return
        full = self._px(FULL_WIDTH)
        room = body_w - col0 - self._px(GAP)
        beside = room >= full * MIN_SHARE
        self._place(beside)
        # one scale for every card: what fits the WIDEST picture in the room
        fit = min(1.0, (room if beside else body_w) / full)
        width = int(round(src.width / AUTHORED_SCALE * self._scale() * fit))
        if width < 40:
            return
        if self._width is not None and abs(width - self._width) < 3 \
                and self._photo is not None:
            return
        self._width = width
        self._draw()

    # -- drawing ------------------------------------------------------------- #
    def _source(self):
        if Image is None:
            return None
        path = figure_path(self.key, bool(self._dark))
        if path not in self._sources:
            try:
                self._sources[path] = Image.open(path).convert("RGBA")
            except OSError:
                self._sources[path] = None
        return self._sources[path]

    def _draw(self):
        src = self._source()
        if src is None or not self._width:
            self.configure(image="")
            self._photo = None
            return
        w = self._width
        h = max(1, int(round(src.height * w / src.width)))
        # premultiplied, so antialiased edges keep their own colour
        img = src.convert("RGBa").resize((w, h), Image.LANCZOS).convert("RGBA")
        self._photo = ImageTk.PhotoImage(img, master=self)
        self.configure(image=self._photo)

    def retheme(self):
        pal = gui_widgets.palette()
        self.configure(bg=pal["bg"])
        dark = _is_dark(pal)
        if dark != self._dark:
            self._dark = dark
            if self._width:
                self._draw()
        self._schedule()


def mount(body, key, first_row, last_row, under_row):
    """The picture for card `key` in `body`: beside rows first..last of the
    fields (column 1), or at `under_row` across both columns when the card is
    too narrow. None when the card has no picture."""
    if Image is None or not available(key):
        return None
    return PhysicsFigure(body, key, first_row, last_row, under_row)
