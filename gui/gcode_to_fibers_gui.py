#!/usr/bin/env python3
"""
gcode_to_fibers_gui.py -- the CLASSIC (developer) UI, with two input modes:
  * "G-code -> STL"  : convert an existing G-code file into a sagged fibre STL
                       (runs gcode_to_fibers.py).
  * "Scaffold -> STL": generate MEW scaffold G-code from print parameters and
                       render it into a sagged fibre STL in one step
                       (runs scaffold_gcode.py, which chains into the converter).

The single App class is assembled from mixin modules; see gui_common.py.

The workers run as SUBPROCESSES rather than imports because the conversion is
resource heavy: a big mesh can exhaust RAM, and in-process a MemoryError would
take the window down with it. As a separate process the worker dies alone and the
GUI reports the failure. Closing the GUI kills any running worker too.
"""

if __name__ == "__main__":  # run as a script: put the program's folders on sys.path
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "engine"))
    import app_paths  # noqa: E402,F401

from gui_common import *

from gui_core import _CoreMixin
from gui_scaffold import _ScaffoldMixin
from gui_buildplate import _BuildPlateMixin
from gui_args import _ArgsMixin
from gui_presets import _PresetsMixin
from gui_viewers import _ViewerMixin
from gui_run import _RunMixin
from gui_sim import _SimMixin
from gui_micrograph import _MicrographMixin
from gui_calibration import _CalibrationMixin
from gui_deviation import _DeviationMixin
from gui_history import _HistoryMixin
from gui_mandrel import _MandrelMixin
from gui_theme import _ThemeMixin


# MRO note: the physics mixins (_SimMixin, _CalibrationMixin, _DeviationMixin,
# _HistoryMixin, _MicrographMixin) lead so their hooks win over _CoreMixin's no-ops,
# with _CoreMixin last as the base. _MicrographMixin / _DeviationMixin /
# _HistoryMixin only ADD methods, so their position is not load-bearing.
class App(_MandrelMixin, _SimMixin, _CalibrationMixin, _DeviationMixin, _HistoryMixin,
          _MicrographMixin, _ThemeMixin, _PresetsMixin, _RunMixin, _ScaffoldMixin,
          _BuildPlateMixin, _ArgsMixin, _ViewerMixin, _CoreMixin):
    WINDOW_TITLE = "MEWracle"


def _fit_to_screen(root, margin=80):
    """Clamp the freshly built window so it never opens taller than the screen's
    usable height. The shared bottom strip (Generate/Cancel) and the log box live
    below the notebook, so on a short or display-scaled screen the natural height
    runs past the bottom edge and hides them with no way to scroll the window.

    The log's row (outer row 3) is the only one with grid weight, so capping the
    toplevel height makes Tk take the shortfall out of the log: it shrinks but stays
    visible. The window is left resizable."""
    root.update_idletasks()
    w = root.winfo_reqwidth()
    h = root.winfo_reqheight()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    h = min(h, sh - margin)
    x = max(0, (sw - w) // 2)
    root.geometry(f"{w}x{h}+{x}+0")


def main():
    # One UI in this build: the modern one, whichever file is run.
    import mewracle_gui
    return mewracle_gui.main([])


if __name__ == "__main__":
    main()
