#!/usr/bin/env python3
"""test_gcode_deselect.py -- click a fiber on the G-code page, click off it.

Self-running (no pytest): boots the modern app on the G-code page, clicks a
fiber through the panel's own pick seam (`_fire_pick`, what a left click
calls), then clicks empty space. Comparator: what the picture draws, read off
the panel's highlight layer (`_hl["data"]`: the polylines it paints yellow).

    python tests/test_gcode_deselect.py

Cases: a fiber click lights polylines; a click on empty space clears the
selection AND the yellow; the same fiber clicked again lights again.

RED proof: before the fix the empty click cleared `focus_lines` but the
highlight fell back to the caret's line (`_gce_hl_refresh`), which the pick
had just moved onto the fiber, so the polylines stayed."""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import os
import sys
import time
import tkinter as tk

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

_M = {}
FAILS = []


def check(name, ok, detail=""):
    print(("PASS" if ok else "FAIL"), name, ("" if ok else detail))
    if not ok:
        FAILS.append(name)


def pump(sec, until=None):
    root = _M["root"]
    t = time.time()
    while time.time() - t < sec:
        root.update()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until is not None else True


def boot():
    import gui_fonts
    import mewracle_gui
    root = tk.Tk()
    root.geometry("1400x900+60+40")
    gui_fonts.init_fonts(root)
    app = mewracle_gui.ModernApp(root)
    root.update()
    _M.update(root=root, app=app)
    app._nav_select("gcode")
    return pump(60, lambda: getattr(app, "_gce_model", None) is not None
                and getattr(app, "_gce_regen", 1) is None)


def lit(panel):
    hl = panel._hl
    return len(hl["data"][0]) if hl else 0


def main():
    check("boot", boot())
    app = _M["app"]
    pump(3)
    panel = app._gce_pv_panel
    w, h = panel._host.winfo_width(), panel._host.winfo_height()
    ren = panel._ren
    fiber = empty = None
    for x in range(8, w, 12):
        for y in range(8, h, 12):
            z = ren.GetZ(x, h - y)
            if fiber is None and 0 < z < 1 and panel.pick(x, y):
                fiber = (x, y)
            if empty is None and z >= 1 and panel.pick(x, y) is None \
                    and panel.pick_at(x, y) is None:
                empty = (x, y)
        if fiber and empty:
            break
    check("found a fiber and empty space", bool(fiber and empty),
          "%r %r" % (fiber, empty))
    panel._fire_pick(*fiber)
    pump(0.5)
    check("fiber click lights it", lit(panel) > 0 and bool(app.gce.focus_lines),
          "lit=%d focus=%r" % (lit(panel), app.gce.focus_lines))
    panel._fire_pick(*empty)
    pump(0.5)
    check("empty click clears the selection", app._gce_sel is None
          and not app.gce.focus_lines)
    check("empty click clears the yellow", lit(panel) == 0,
          "lit=%d" % lit(panel))
    panel._fire_pick(*fiber)
    pump(0.5)
    check("a fiber clicked again lights again", lit(panel) > 0,
          "lit=%d" % lit(panel))
    _M["root"].destroy()
    print("FAILED: %s" % FAILS if FAILS else "ALL PASS")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
