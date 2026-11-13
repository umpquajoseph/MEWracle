"""gui_splash.py -- the boot splash: a small borderless card that is on screen
within a second of a launch and says what is loading while the heavy imports
and the page build run.

Why it exists: mewracle_gui.main() builds the main window WITHDRAWN and shows
it once, fully built. On a cold Windows boot that is tens of seconds with
nothing on screen, which reads as a launch that failed.

It lives on the SAME Tk root the app will use (a Toplevel of the withdrawn
root), so there is no second process and no second Tcl. The price is that Tk
only redraws when the app's one thread yields, so the card advances in steps:

* during the module imports, an import hook (_ImportPump) pumps the event loop
  every PUMP_S and moves the bar by how many modules have loaded. That phase is
  most of a cold boot -- disk reads and the virus scan of VTK / numpy /
  matplotlib -- so the card stays painted and responsive while it lasts;
* the hook comes OUT before the page build (step()), because a full update()
  mid-build would run the app's own queued after() callbacks against a
  half-built window. The build itself shows one fixed stage.

Stdlib + tkinter only: it has to import in milliseconds, before anything heavy.
Every call is guarded -- the splash is cosmetic and must never block a launch."""

import sys
import time
import tkinter as tk
from pathlib import Path

# Copied from gui_modern_theme.LIGHT (card / fg / hint / select_bg / border),
# not imported: that module pulls in the widget layer this card runs ahead of.
CARD = "#ffffff"
FG = "#1c1e21"
HINT = "#646a71"
ACCENT = "#2f6fd6"
BORDER = "#d9dce1"
TRACK = "#e9edf2"           # gui_modern_theme.LIGHT["active"]

PUMP_S = 0.05               # import-phase redraw cadence
# Roughly how many modules the GUI's imports add (719 in sys.modules after
# `import mewracle_gui`, minus the ~80 a bare tkinter start has).
# Only scales the bar, which is capped at the import stage's end, so drift
# costs a bar that stalls early or jumps at the end, never a wrong state.
IMPORT_MODULES = 640
IMPORT_END = 0.6            # bar fraction where the import stage ends


class _ImportPump:
    """A sys.meta_path observer: on each import lookup, at most every PUMP_S,
    advance the bar and let Tk process its events. Finds nothing itself."""

    def __init__(self, splash):
        self.splash = splash
        self.base = len(sys.modules)
        self.last = 0.0
        self.busy = False

    def find_spec(self, name, path=None, target=None):
        now = time.perf_counter()
        if self.busy or now - self.last < PUMP_S:
            return None
        self.busy, self.last = True, now
        try:
            done = (len(sys.modules) - self.base) / IMPORT_MODULES
            self.splash._set_bar(min(IMPORT_END, 0.05 + done * IMPORT_END))
            self.splash.root.update()
        except Exception:  # noqa: BLE001  (cosmetic: never break an import)
            pass
        finally:
            self.busy = False
        return None


class BootSplash:
    """Create the app's Tk root (withdrawn) and the splash card on it.

    `root` is the root main() goes on to build the app in. Call step() once the
    imports are done, then close() just before the main window is revealed."""

    def __init__(self, title="MEWracle"):
        self.root = tk.Tk()
        self.root.withdraw()
        self.win = tk.Toplevel(self.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.configure(bg=CARD, highlightthickness=1,
                           highlightbackground=BORDER)
        scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        w, h = int(420 * scale), int(280 * scale)
        family = "Segoe UI" if sys.platform.startswith("win") else "Helvetica"

        self._logo = self._load_logo(scale)
        if self._logo is not None:
            tk.Label(self.win, image=self._logo, bg=CARD).pack(
                pady=(int(28 * scale), int(6 * scale)))
        tk.Label(self.win, text=title, bg=CARD, fg=FG,
                 font=(family, 20, "bold")).pack()
        self._status = tk.StringVar(value="Loading components…")
        tk.Label(self.win, textvariable=self._status, bg=CARD, fg=HINT,
                 font=(family, 10)).pack(pady=(int(4 * scale), 0))
        self._bar_h = max(3, int(4 * scale))
        self._bar = tk.Canvas(self.win, height=self._bar_h, bg=TRACK,
                              highlightthickness=0, bd=0)
        self._bar.pack(side="bottom", fill="x")
        self._fill = self._bar.create_rectangle(0, 0, 0, self._bar_h,
                                                fill=ACCENT, width=0)
        self._frac = 0.0

        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}")
        self.win.deiconify()
        self.win.update()           # paint now: nothing else is queued yet
        self._pump = _ImportPump(self)
        sys.meta_path.insert(0, self._pump)

    @staticmethod
    def _load_logo(scale):
        """The scaffold icon (256 px), halved on a 100-125% display. Looked up
        the way mewracle_gui._set_window_icon does: this file's Logo/ folder,
        or the bundled copy when frozen."""
        here = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
        png = here / "Logo" / "mewracle_scaffold_icon_v4.png"
        try:
            img = tk.PhotoImage(file=str(png))
        except (tk.TclError, OSError):
            return None
        return img if scale >= 1.5 else img.subsample(2)

    def _set_bar(self, frac):
        self._frac = max(self._frac, min(1.0, frac))    # never moves back
        width = self._bar.winfo_width()
        self._bar.coords(self._fill, 0, 0, int(width * self._frac), self._bar_h)

    def _unhook(self):
        try:
            sys.meta_path.remove(self._pump)
        except ValueError:
            pass

    def step(self, text, frac):
        """End the import stage and show `text` at bar fraction `frac`.
        Redraws with update_idletasks(), which only paints, so it is safe to
        call with the app half-constructed."""
        self._unhook()
        try:
            self._status.set(text)
            self._set_bar(frac)
            self.win.update_idletasks()
        except tk.TclError:
            pass

    def close(self):
        self._unhook()
        try:
            self.win.destroy()
        except tk.TclError:
            pass
