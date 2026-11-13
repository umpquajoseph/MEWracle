#!/usr/bin/env python3
"""test_gcode_angles.py -- the G-code page's Angle cycle form.

Self-running (no pytest): boots the modern app on the G-code page, adds the
Angle cycle to scaffold 1 through its tool, and edits the cycle the way a user
does. Comparator: the designer's own per-layer angle table
(`_render_angle_table` / `_sync_angle_pattern`) and its parser
(`_parse_angle_pattern`), which the form is that table's body laid over the
page's widget-less vars.

    python tests/test_gcode_angles.py [--shot out.png]

Cases: opening the form raises nothing (Tk's swallowed callback errors are
caught); the table shows one row per angle of the seeded cycle; an edited
Angle writes `angle_pattern`; "+ Add layer" and the row's X change the cycle
and the row label; a Stepover cell writes `angle_stepover_pattern`; the tree
row's label follows the cycle.

RED proof: with `_toggle_angle_pattern` calling `.configure` on the page's
`rotation` StringVar (the defect), the form opens as the checkbox alone and
"form raised nothing" fails with AttributeError."""

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
ERRS = []


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
    cls = mewracle_gui.ModernApp
    root = tk.Tk()
    root.geometry("1400x900+60+40")
    gui_fonts.init_fonts(root)
    root.report_callback_exception = lambda *e: ERRS.append(e[1])
    app = cls(root)
    root.update()
    _M.update(root=root, app=app)
    app._nav_select("gcode")
    return pump(60, lambda: getattr(app, "_gce_model", None) is not None
                and getattr(app, "_gce_regen", 1) is None)


def rows(app):
    """The open table's (Angle, Stepover) entry texts, top to bottom."""
    view = app._gce_d_form_open[2]
    return ([r.get() for r in view["angle_rows"]],
            [r.get() for r in view["angle_step_rows"]])


def main():
    shot = None
    if "--shot" in sys.argv:
        shot = sys.argv[sys.argv.index("--shot") + 1]
    check("boot", boot())
    app = _M["app"]
    app._gce_design_regen = lambda: None
    v = app.gce_scaf_vars[0]
    v["angle_pattern_on"].set(False)
    v["angle_pattern"].set("")

    app._gce_d_tool("angles")
    pump(1)
    check("form raised nothing", not ERRS, repr(ERRS))
    check("angle cycle switched on", bool(v["angle_pattern_on"].get()))
    form = getattr(app, "_gce_d_form_open", None)
    check("form open on angles", bool(form) and form[1] == "angles")
    view = form[2] if form else {}
    table = view.get("angle_table") if form else None
    check("table shown", table is not None and table.winfo_ismapped())
    seed = [x.strip() for x in v["angle_pattern"].get().split(",") if x.strip()]
    a, s = rows(app) if form else ([], [])
    check("one row per seeded angle", len(seed) > 0 and len(a) == len(seed),
          "seed=%r rows=%r" % (seed, a))
    if shot:
        import capture_ui
        pump(1)
        capture_ui.grab(_M["root"]).save(shot)

    # edit an angle
    view["angle_rows"][0].set("15")
    check("edited angle written", v["angle_pattern"].get().split(",")[0]
          .strip() == "15", v["angle_pattern"].get())

    # add and remove a layer
    n = len(seed)
    app._add_angle_row(view)
    pump(0.2)
    a, s = rows(app)
    check("+ Add layer adds a row", len(a) == n + 1 and a[0] == "15", repr(a))
    check("added angle in the cycle",
          len(v["angle_pattern"].get().split(",")) == n + 1,
          v["angle_pattern"].get())
    app._remove_angle_row(view, n)
    pump(0.2)
    a, s = rows(app)
    check("X removes the row", len(a) == n and a[0] == "15", repr(a))

    # a stepover cell
    view["angle_step_rows"][1].set("0.4")
    check("stepover cell written",
          v["angle_stepover_pattern"].get().split(",")[1].strip() == "0.4",
          v["angle_stepover_pattern"].get())

    # the tree row follows the cycle
    check("tree label follows",
          app._gce_d_label(v, "angles").startswith("Layer angles · 15"),
          app._gce_d_label(v, "angles"))
    check("nothing raised while editing", not ERRS, repr(ERRS))

    _M["root"].destroy()
    print("FAILED: %s" % FAILS if FAILS else "ALL PASS")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
