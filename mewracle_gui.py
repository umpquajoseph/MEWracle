#!/usr/bin/env python3
"""mewracle_gui.py -- the MODERN UI entry point.

Builds ModernApp: the SAME App as gcode_to_fibers_gui.py (same mixins, same Tk
variables, validators, workers, presets) wrapped in the modern shell -- one top
row that reads as the job (gui_shell), the Home page (gui_home), the Helvetica type ramp
(gui_fonts) and the modern light/dark theme (gui_modern_theme).

Launch: `python mewracle_gui.py`, or double-click MEWracle.bat.

gcode_to_fibers_gui.py holds the App this builds on; run directly, it opens this
same UI."""

import os as _os, sys as _sys
# The program lives in the folders beside this file (engine/, gui/, ...); its
# modules import one another by bare name, and app_paths puts every folder on
# sys.path. Frozen, every module is in the bundle and this adds nothing.
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                  "engine"))
import app_paths  # noqa: E402,F401

import inspect
import sys

from gui_common import *


# --- frozen-aware worker dispatch (the multi-call binary) ------------------
# From source the GUI launches every worker as `python -u <script.py> ...`. Frozen
# there is no python and no loose .py: `sys.executable` becomes MEWracle.exe, so the
# exe must be able to RUN a worker itself. gui_common.worker_cmd builds
# `[MEWracle.exe, "worker:xxx", ...]` when frozen; this front door catches that on
# startup, imports the worker's module and runs its main(). Keys MUST match
# gui_common._WORKER_PY.
_WORKERS = {
    "worker:convert":    ("gcode_to_fibers",   "main"),
    "worker:scaffold":   ("scaffold_gcode",    "main"),
    "worker:export":     ("mew_export",        "main"),
    "worker:viewer":     ("stl_viewer",        "main"),
    "worker:player":     ("gcode_player",      "main"),
    "worker:bedcal":     ("bed_calibrator",    "main"),
    "worker:micrograph": ("micrograph_viewer", "main"),
}


def _maybe_dispatch_worker(argv):
    """If argv is a worker relaunch (`<exe> worker:xxx ...`), run that worker and exit;
    otherwise return None so main() boots the GUI.

    Sets sys.argv to the worker's OWN argv (the "worker:xxx" token dropped, a proper
    program name at [0]) and calls its main(). The workers split across two main()
    conventions -- `main(argv=None)` reading sys.argv[1:] itself, and `main(argv)`
    slicing argv[1:] internally -- so which one is detected from whether the first
    parameter has a default, and BOTH parse exactly the same argv[2:]. Raises
    SystemExit with the worker's return code on dispatch."""
    if len(argv) >= 2 and argv[1] in _WORKERS:
        mod_name, fn_name = _WORKERS[argv[1]]
        mod = __import__(mod_name, fromlist=[fn_name])
        fn = getattr(mod, fn_name)
        sys.argv = [mod_name] + list(argv[2:])
        params = list(inspect.signature(fn).parameters.values())
        needs_argv = bool(params) and params[0].default is inspect.Parameter.empty
        raise SystemExit(fn(sys.argv) if needs_argv else fn())
    return None


# A distinct Windows taskbar identity for MEWracle. Without an explicit
# AppUserModelID, Windows derives one from the process image (pythonw.exe) and folds
# this window under whatever taskbar button it already associates with that identity.
# Changing the shortcut icon cannot fix it: the button shown is chosen by AUMID, not
# by which .lnk was clicked. Must run before tk.Tk().
_APP_USER_MODEL_ID = "MEWracle.PhysicsBuild"


def _set_taskbar_identity():
    """Claim an explicit AppUserModelID so this build owns its taskbar button.
    Fully guarded and a no-op off Windows."""
    if not IS_WINDOWS:
        return
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            _APP_USER_MODEL_ID)
    except Exception:  # noqa: BLE001  (no shell32 / older Windows, etc.)
        pass


def _set_window_icon(root):
    """Point the window + taskbar button at the MEWracle scaffold icon.

    The artwork ships in this package's own ``Logo\\`` folder; older checkouts kept it
    one level up, so try both, resolved relative to THIS FILE and never the working
    directory. On Windows a multi-size ``.ico`` via ``iconbitmap(default=...)`` drives
    both the title bar and the taskbar; elsewhere fall back to the PNG through
    ``iconphoto``. Guarded -- a missing Logo folder just keeps Tk's default feather."""
    from pathlib import Path
    here = Path(__file__).resolve().parent
    # resource_path("Logo") is the repo's Logo/ from source and the bundled copy
    # (sys._MEIPASS/Logo) when frozen; the parent-dir entry keeps the old
    # "artwork one level up" fallback for loose source checkouts.
    try:
        for logo in (Path(resource_path("Logo")), here / "Logo", here.parent / "Logo"):
            ico = logo / "mewracle_scaffold_icon_v4.ico"
            if IS_WINDOWS and ico.exists():
                root.iconbitmap(default=str(ico))
                return
            png = logo / "mewracle_scaffold_icon_v4.png"
            if png.exists():
                img = tk.PhotoImage(file=str(png))
                root.iconphoto(True, img)
                root._mewracle_icon_ref = img   # keep a ref so Tk doesn't GC it
                return
    except Exception:  # noqa: BLE001  (icon is cosmetic -- never block launch)
        pass


def _maximize_window(root):
    """Open the window maximized (title bar + taskbar still shown) rather than at its
    fitted size. Clicking a shortcut runs pythonw with no console, so the window state
    must be set here in code, not by the launcher. _fit_to_screen has already set the
    geometry that becomes the restore size.

    'zoomed' is the Windows/macOS Tk state; on X11 fall back to the -zoomed attribute,
    and a platform with neither just keeps the fitted window."""
    try:
        root.state("zoomed")
    except tk.TclError:
        try:
            root.attributes("-zoomed", True)
        except tk.TclError:
            pass


def _open_boot_splash():
    """Put the boot splash (gui_splash) on screen and return it, or None.

    Runs at IMPORT time, from the line below, so the card is up before the
    heavy imports after it (VTK, matplotlib, the whole mixin stack); on a cold
    Windows boot those imports are most of the wait. Only for a real app
    launch: an import of this module (the tests, capture_ui) or a frozen
    worker relaunch gets None and no window. main() builds the app on the
    splash's root, so the taskbar identity and DPI opt-in move here with it."""
    if __name__ != "__main__" or (len(sys.argv) >= 2 and sys.argv[1] in _WORKERS):
        return None
    _set_taskbar_identity()  # both must precede tk.Tk()
    enable_dpi_awareness()
    try:
        import gui_splash
        return gui_splash.BootSplash()
    except Exception:  # noqa: BLE001  (cosmetic: boot without it)
        return None


_SPLASH = _open_boot_splash()

import gui_fonts
from gcode_to_fibers_gui import App, _fit_to_screen
from gui_shell import _ShellMixin
from gui_gcode_editor import _GcodeEditorMixin
from gui_gcode_params import _GcodeParamsMixin
from gui_gcode_changes import _GcodeChangesMixin
from gui_scripts import _ScriptsMixin
from gui_gcode_work import _GcodeWorkMixin
from gui_gcode_move import _GcodeMoveMixin
from gui_gcode_design import _GcodeDesignMixin
from gui_gcode_subsection import _GcodeSubsectionMixin
from gui_gcode_twodesigns import _GcodeTwoDesignsMixin
from gui_gcode_panels import _GcodePanelsMixin
from gui_gcode_collector import _GcodeCollectorMixin
from gui_gcode_jet import _GcodeJetMixin
from gui_gcode_view import _GcodeViewMixin
from gui_gcode_tutorial import _GcodeTutorialMixin
from gui_gcode_calibrate import _GcodeCalibrateMixin
from gui_embed import _EmbedMixin
from gui_preview import _PreviewMixin
from gui_pages import _ModernPagesMixin
from gui_home import _HomeMixin
from gui_modern_theme import _ModernThemeMixin
from gui_sweeps import _SweepsMixin
from gui_machine_link import _MachineLinkMixin
from gui_finder import _FinderMixin
from gui_summary import _SummaryMixin
from gui_tour import _TourMixin
from gui_settings import _SettingsMixin
from gui_legacy import _LegacyMixin
from gui_fiber_target import _FiberTargetMixin
import gui_units


# MRO: the shell/pages/home/theme layers lead so their overrides win
# (_build_widgets, _build_sim_tab, _build_scaffold_container, the
# _scaffold_selector_* pair, _apply_all_toggles, _active_mode,
# _refresh_result_controls, _apply_theme); App brings the classic mixin stack
# unchanged. _PreviewMixin sits before _ModernPagesMixin so its
# _show_scaffold_frame override chains via super(). _EmbedMixin precedes
# everything that launches or finishes work so its _spawn_player /
# _spawn_stl_viewer / on_close / _on_finished overrides beat gui_viewers and
# gui_run. _SweepsMixin / _MachineLinkMixin only ADD page builders and no
# persist entries, so their slots are order-insensitive.
# _FinderMixin adds only the Ctrl+F control palette (gui_finder): no builder, no
# persist entry and no override of anything the other layers define, so its slot
# is order-insensitive too. It leads so that `open_finder` / `reveal_control`
# cannot be shadowed by a later layer. _SummaryMixin (the top row's design
# line) and _TourMixin (Home's guided tour) only add methods the shell and
# Home call, so their slots are free too. _SettingsMixin (the top row's gear
# panel) likewise only adds methods. _GcodeTutorialMixin wraps the
# G-code page's builder last (after every other layer's) to put Tutorial
# beside Help. _GcodeCalibrateMixin leads the G-code layers: it is a mode of
# that page, so its answers come before every layer's. _LegacyMixin (More's
# Legacy UI) wraps the shell's top row, so it comes before _ShellMixin.
class ModernApp(_FinderMixin, _LegacyMixin, _EmbedMixin, _GcodeCalibrateMixin,
                _GcodeTutorialMixin, _GcodeSubsectionMixin, _GcodeTwoDesignsMixin,
                _GcodeViewMixin, _GcodeJetMixin, _GcodeCollectorMixin,
                _GcodePanelsMixin,
                _GcodeDesignMixin,
                _GcodeParamsMixin, _GcodeWorkMixin, _GcodeMoveMixin,
                _ScriptsMixin, _GcodeChangesMixin,
                _GcodeEditorMixin, _ShellMixin,
                _PreviewMixin,
                _SweepsMixin, _MachineLinkMixin,
                _ModernPagesMixin, _HomeMixin, _SummaryMixin, _TourMixin,
                _SettingsMixin, _ModernThemeMixin, _FiberTargetMixin,
                App):
    WINDOW_TITLE = "MEWracle"

    def __init__(self, root, *args, **kwargs):
        # display units (the Settings panel): the modern shell only, and ON
        # before any field is built, so every gui_form entry is bound once
        gui_units.enable()
        super().__init__(root, *args, **kwargs)


def main(argv=None):
    # Multi-call binary: when frozen the exe is re-launched as a worker; run it
    # and exit (raises SystemExit) before any Tk/GUI setup. No-op for a normal
    # launch (returns None) and from source (worker_cmd never emits this form).
    _maybe_dispatch_worker(sys.argv)
    argv = sys.argv[1:] if argv is None else list(argv)
    if _SPLASH is not None:
        # the splash already made the root (identity + DPI first), withdrawn
        root = _SPLASH.root
        _SPLASH.step("Building the workspace…", 0.65)
    else:
        _set_taskbar_identity()  # own taskbar button; must precede tk.Tk()
        enable_dpi_awareness()   # must precede tk.Tk(); no-op off Windows
        root = tk.Tk()
    # Hide the window while it is built, sized and maximized. Building a visible
    # window shows it at Tk's default size, again after _fit_to_screen resizes it,
    # and again after _maximize_window zooms it -- a multi-second sequence of flashes
    # as the heavy page tree realizes. Withdrawn, that happens off-screen and the
    # window appears once, already full-size. (update_idletasks in _fit_to_screen
    # still computes requested sizes on a withdrawn window.)
    root.withdraw()
    _set_window_icon(root)   # window + taskbar icon; guarded, cosmetic
    gui_fonts.init_fonts(root)      # the named-font ramp, before widgets
    # the real app, and only it, asks before losing work and keeps an
    # autosave (gui_gcode_work): a rig building ModernApp(root) never
    # meets a modal prompt
    ModernApp.GW_INTERACTIVE = True
    ModernApp(root)
    _fit_to_screen(root)     # sets the restore-size geometry...
    _maximize_window(root)   # ...then open maximized
    if _SPLASH is not None:
        _SPLASH.close()
    root.deiconify()         # ...and reveal it, once, fully built
    root.mainloop()


if __name__ == "__main__":
    main()
