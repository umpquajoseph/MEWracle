#!/usr/bin/env python3
"""gui_micrograph.py -- the micrograph image helpers for the unified
Viewer's in-scene overlay layer.

A thin, self-contained mixin -- not a page of its own. It supplies two reusable
helpers, driven by the shell's
unified-Viewer top row (`_unified_import_image` / `_unified_scale_image` in
gui_embed):

  * _an_micro_load          -- pick a top-down micrograph (PNG always; JPG/TIFF/BMP
                               with Pillow).
  * _an_micro_scale_window  -- an optional locked-2D matplotlib window: click two
                               points on a KNOWN length, type the length in mm, and
                               it pins a real mm/px that overrides the viewer's
                               auto-fit.

The loaded image (and any pinned scale) is then pushed DOWN to the open unified
viewer as a live overlay layer (`_push_micrograph_overlay`); all the placement /
scale / rotation / opacity controls live in that viewer's sidebar.

Depends on NO Analyze engine (`mew_analysis` / `analysis_plots` / `gui_analysis`)
-- only the scaffold the user already generated and, for the 2-D scale tool,
optional matplotlib. Composed into the App alongside gui_sim._SimMixin.
"""
import math

from gui_common import *

# Pillow lets the micrograph loader read JPG/TIFF/BMP; PNG works without it.
try:
    from PIL import Image
    _HAVE_PIL = True
except Exception:  # noqa: BLE001
    _HAVE_PIL = False

# The 2-D "Scale image" tool needs matplotlib's Tk backend. Absent it, the button
# explains the (optional) dependency and the viewer's auto-fit + scale sliders
# still cover the non-precise case.
try:
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    _HAVE_MPL_TK = True
except Exception:  # noqa: BLE001
    _HAVE_MPL_TK = False


class _MicrographMixin:
    """Micrograph image helpers reused by the unified Viewer's overlay layer.
    There is no separate Micrograph page/launcher:
    the shell's viewer-row "Import image…" / "Scale image…" buttons call
    `_unified_import_image` / `_unified_scale_image` (gui_embed), which reuse the
    two methods here — `_an_micro_load` (pick a top-down micrograph; PNG always,
    JPG/TIFF/BMP with Pillow) and `_an_micro_scale_window` (an optional locked-2D
    matplotlib window: click two points on a KNOWN length to pin a real mm/px) —
    then push the image DOWN to the open unified viewer as a live layer.

    Depends on NO Analyze engine (only the scaffold the user already generated and,
    for the 2-D scale tool, optional matplotlib)."""

    def _an_micro_load(self):
        self._an_micro = getattr(self, "_an_micro", {})
        types = [("PNG image", "*.png")]
        if _HAVE_PIL:
            types = [("Images", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp")] + types
        p = filedialog.askopenfilename(title="Load micrograph", filetypes=types)
        if not p:
            return
        try:
            if _HAVE_PIL:
                # the player's decoder, so a 16-bit TIFF reads the same in the
                # Scale window as in the overlay (see pil_rgb8)
                from micrograph_viewer import pil_rgb8
                img = pil_rgb8(Image.open(p))
            else:
                import matplotlib.image as mpimg
                img = mpimg.imread(p)
        except Exception as exc:  # noqa: BLE001
            why = getattr(exc, "strerror", None) or "it is not an image this "                 "app can read (PNG, JPEG, TIFF or BMP)"
            self._micro_status(f"Could not open {os.path.basename(p)}: {why}.")
            return
        self._an_micro["image"] = img
        self._an_micro["path"] = p
        self._an_micro["_fresh"] = True          # re-importing the same file
        h, w = img.shape[:2]
        self._micro_status(f"{os.path.basename(p)}  ({w}×{h} px) loaded.")

    def _micro_status(self, msg):
        """Set the micrograph status. The old Analyze micrograph-launcher label
        (an_micro_status) was retired with its page; fall back to the main
        status bar so feedback is never lost -- and, crucially, so setting it never
        raises (the exception used to abort _an_micro_load BEFORE the overlay push,
        so Import silently did nothing until Scale was opened)."""
        lbl = getattr(self, "an_micro_status", None)
        if lbl is not None:
            try:
                lbl.set(msg)
                return
            except Exception:  # noqa: BLE001
                pass
        if hasattr(self, "status"):
            try:
                self.status.set(msg)
            except Exception:  # noqa: BLE001
                pass

    def _an_micro_scale_window(self):
        """A small locked-2D window (matplotlib): click two points on a KNOWN
        length, type the length in mm, and it pins a real mm/px that overrides
        the viewer's auto-fit. Deliberately 2-D -- defining a scale is easier
        without the 3-D camera; the viewer still lets you tweak it afterward."""
        self._an_micro = getattr(self, "_an_micro", {})
        if not _HAVE_MPL_TK:
            self._refuse("The scale tool needs matplotlib (pip install "
                                "matplotlib).")
            return
        img = self._an_micro.get("image")
        if img is None:
            self._refuse("Import an image first, then scale it.")
            return
        from matplotlib.figure import Figure
        state = {"pts": [], "line": None}
        top = tk.Toplevel()
        top.title("Scale image: click two points on a known length")

        fig = Figure(figsize=(6.4, 5.2), tight_layout=True)
        ax = fig.add_subplot(111)
        ax.imshow(img, origin="upper")
        ax.set_title("Click two points spanning a known length", fontsize=10)
        canvas = FigureCanvasTkAgg(fig, master=top)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        note = tk.StringVar(value="Click the first point.")
        ttk.Label(top, textvariable=note, foreground="#555").pack(
            anchor="w", padx=8, pady=(0, 6))

        def on_click(ev):
            if ev.xdata is None or ev.ydata is None:
                return
            state["pts"].append((float(ev.xdata), float(ev.ydata)))
            if len(state["pts"]) == 1:
                note.set("Click the second point.")
                canvas.draw()
                return
            (x0, y0), (x1, y1) = state["pts"][-2:]
            dpx = math.hypot(x1 - x0, y1 - y0)
            if state["line"] is not None:
                state["line"].remove()
            state["line"], = ax.plot([x0, x1], [y0, y1], "-o", color="#00b3e6",
                                     lw=2.0, ms=5)
            canvas.draw()
            import gui_widgets
            mm = gui_widgets.ask_float(
                top, "Scale bar", "Known length between the two points (mm):",
                minvalue=1e-12)
            if mm and dpx > 0:
                mmpx = mm / dpx
                self._an_micro["scale_mmpx"] = mmpx
                msg = (f"Scale: {mmpx:.6g} mm/px ({mm:g} mm over {dpx:.1f} px). "
                       "Adjust it live from the viewer's sidebar sliders, or "
                       "click again to redo.")
                note.set(msg)
                if hasattr(self, "an_micro_scale_status"):
                    self.an_micro_scale_status.set(msg)
            state["pts"] = []

        canvas.mpl_connect("button_press_event", on_click)
        canvas.draw()
