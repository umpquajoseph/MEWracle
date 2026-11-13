#!/usr/bin/env python3
"""test_gcode_params.py -- the G-code page's Parameters table, main path.

Self-running (no pytest): boots the modern app on the G-code page with three
scaffolds, opens the table on scaffold 2 and drives it the way a user does.
Comparator: the Scaffold designer's own Tk variables and its own validator
(`_validate_one_scaffold`), which the table must write through and agree with.

    python tests/test_gcode_params.py [--shot out.png]

Cases: the table opens with one column per scaffold and scaffold 2 lit; a
valid Stepover in column 2 lands in `scaf_vars[1]` and regenerates once; a bad
value is refused with the designer's wording and writes nothing; Layer
rotation goes through the designer's combobox; Linking loops toggles the
designer's checkbox; copy-to-all fills the row; Undo changes restores all;
a change on the designer side shows in the table."""

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
    from gui_gcode_params import _GcodeParamsMixin
    cls = mewracle_gui.ModernApp
    if not issubclass(cls, _GcodeParamsMixin):
        cls = type("ParamsApp", (_GcodeParamsMixin, cls), {})
    root = tk.Tk()
    root.geometry("1400x900+60+40")
    gui_fonts.init_fonts(root)
    app = cls(root)
    root.update()
    _M.update(root=root, app=app)
    app._nav_select("gcode")
    return pump(60, lambda: getattr(app, "_gce_model", None) is not None
                and getattr(app, "_gce_regen", 1) is None)


def cell(app, key, c):
    # the rows the table drew: the grade rows come and go with the grade
    rows = app._gcp_rows(app._gcp)
    gi = next(i for i, r in enumerate(rows) if r[2] == key)
    return app._gcp["cells"][(gi, c)]


def lcell(app, key, c):
    from gui_gcode_params import LAYER_ROWS
    gi = next(i for i, r in enumerate(LAYER_ROWS) if r[2] == key)
    return app._gcl["cells"][(gi, c)]


def grab(path):
    pump(2)
    try:
        import capture_ui
        capture_ui.grab(_M["root"]).save(path)
        print("shot", path)
    except Exception as e:  # noqa: BLE001
        print("shot failed", e)


def main():
    shot = None
    if "--shot" in sys.argv:
        shot = sys.argv[sys.argv.index("--shot") + 1]
    check("boot", boot())
    app = _M["app"]
    regens = []
    # the table's own regenerate calls; the page's own regen (which also
    # follows every designer var) is held off for the run
    app._gcp_regen = lambda: regens.append(1)
    app._gce_design_regen = lambda: None
    app.gce_s_count.set("3")
    app._sync_scaffold_count() if hasattr(app, "_sync_scaffold_count") \
        else None
    pump(1)
    before = {(i, k): app.gce_scaf_vars[i][k].get() for i in range(3)
              for k in ("stepover", "speed", "link_loops_on")}
    app._gce_params_open(col=1)
    pump(1)
    st = app._gcp
    check("one column per scaffold", st["n"] == 3 and len(st["heads"]) == 3,
          f"n={st['n']}")
    check("scaffold 2 highlighted", st["hi"] == 1)
    check("table mapped", st["box"].winfo_ismapped())

    # a valid change in column 2
    c = cell(app, "stepover", 1)
    c["var"].set("0.35")
    app._gcp_commit(c)
    check("stepover written to scaffold 2",
          app.gce_scaf_vars[1]["stepover"].get() == "0.35",
          app.gce_scaf_vars[1]["stepover"].get())
    check("scaffold 1 untouched",
          app.gce_scaf_vars[0]["stepover"].get() == before[(0, "stepover")])
    check("one regen per change", len(regens) == 1, str(regens))
    app._gcp_commit(c)
    check("no regen without a change", len(regens) == 1, str(regens))
    check("designer validator agrees",
          app._validate_one_scaffold(app.gce_scaf_vars[1]) is None)

    # a bad value: refused, nothing written, designer's words
    c = cell(app, "layers", 1)
    c["var"].set("2.5")
    app._gcp_commit(c)
    check("bad layers not written", app.gce_scaf_vars[1]["layers"].get() == "10")
    check("bad layers flagged", c["bad"]
          and "whole number" in st["err"].get(), st["err"].get())
    check("no regen on a bad value", len(regens) == 1)
    app._gcp_cell_esc(c)
    check("Esc puts it back", c["var"].get() == "10" and not st["err"].get())

    # rotation through the combobox
    c = cell(app, "rotation", 2)
    c["var"].set("60")
    app._gcp_commit(c)
    check("rotation via the designer combo",
          app._rotation_value(app.gce_scaf_vars[2]["rotation"]) == 60.0)

    # linking loops checkbox
    c = cell(app, "link_loops_on", 0)
    c["var"].set(False)
    app._gcp_commit(c)
    check("linking loops off on scaffold 1",
          app.gce_scaf_vars[0]["link_loops_on"].get() is False)

    # copy to all
    c = cell(app, "speed", 1)
    c["var"].set("450")
    app._gcp_copy_row(c)
    check("copy speed to all", [app.gce_scaf_vars[i]["speed"].get()
                                for i in range(3)] == ["450"] * 3)

    # the designer side shows in the table
    app.gce_scaf_vars[2]["start_x"].set("12")
    pump(0.2)
    check("designer change shows", cell(app, "start_x", 2)["var"].get()
          == "12")

    # the view toggle: per layer shows the lit scaffold's layers
    before_lo = app.gce_scaf_vars[1]["layer_overrides"].get()
    st["view"].set("layer")
    app._gcp_switch(st)
    pump(0.5)
    check("toggle shows scaffold 2's layers", app._gcl["shown"]
          and app._gcl["s"] == 1 and not st["box"].winfo_ismapped())
    cb = app._gcl["pick_cb"]
    cb.current(2)
    cb.event_generate("<<ComboboxSelected>>")
    pump(0.5)
    check("scaffold picker switches the layers shown", app._gcl["s"] == 2)
    # a layer's right-click: scaffold 2, layer 3 lit
    app._gce_layer_params_open(1, 2)
    pump(1)
    lt = app._gcl
    check("layer table: one column per layer",
          lt["n"] == int(app.gce_scaf_vars[1]["layers"].get()) and lt["hi"] == 2)
    check("scaffold table hidden", not st["box"].winfo_ismapped())
    c = lcell(app, "stepover", 2)
    check("layer cell shows the scaffold's value",
          c["var"].get() == app.gce_scaf_vars[1]["stepover"].get())
    c["var"].set("0.25")
    app._gcp_commit(c)
    check("layer override saved", app._gce_layer_overrides(1)
          == {2: {"stepover": "0.25"}}, app.gce_scaf_vars[1]["layer_overrides"]
          .get())
    check("layer change regenerates", len(regens) >= 3, str(regens))
    c = lcell(app, "speed", 4)
    c["var"].set("-5")
    app._gcp_commit(c)
    check("bad layer value refused", c["bad"]
          and 4 not in app._gce_layer_overrides(1))
    app._gcp_cell_esc(c)
    c = lcell(app, "link_loops", 3)
    c["var"].set(not app.gce_scaf_vars[1]["link_loops_on"].get())
    app._gcp_commit(c)
    check("linking loops differ on layer 4",
          "link_loops" in app._gce_layer_overrides(1).get(3, {}))
    app._gcl_clear(c)
    check("Use the scaffold's value clears it",
          3 not in app._gce_layer_overrides(1))
    with app._gce_page_scope():           # the page's scaffolds, not the designer's
        spec = app._scaffold_spec(1)
    check("spec carries the layer overrides",
          spec.get("layer_overrides") == {"3": {"stepover": "0.25"}},
          str(spec.get("layer_overrides")))
    check("designer validator accepts it",
          app._validate_one_scaffold(app.gce_scaf_vars[1]) is None)
    import scaffold_gcode
    g0 = dict(spec)
    g0.pop("layer_overrides")
    a = scaffold_gcode.generate_multi_scaffold_gcode(
        [scaffold_gcode._scaffold_from_spec(g0)])
    b = scaffold_gcode.generate_multi_scaffold_gcode(
        [scaffold_gcode._scaffold_from_spec(spec)])
    check("the g-code changes", a != b)
    if shot:
        grab(shot.replace(".png", "_layers.png"))
    # the view toggle back to every scaffold
    app._gcl["view"].set("scaffold")
    app._gcp_switch(app._gcl)
    pump(0.5)
    check("toggle shows the scaffold view", st["shown"]
          and not app._gcl["shown"] and st["hi"] == 1)
    # the stepover row, found by key: the grade rows shift row numbers
    gi_step = next(i for i, r in enumerate(app._gcp_rows(st))
                   if r[2] == "stepover")
    badge = st["badges"].get((gi_step, 1))
    check("scaffold table counts the layer",
          badge is not None and badge.cget("text") == "·1",
          badge.cget("text") if badge is not None else sorted(st["badges"]))
    check("per-layer changes listed",
          len(st["list"].winfo_children()) == 2)

    if shot:
        grab(shot)

    # undo everything
    app._gcp_undo_all(app._gcp)
    after = {(i, k): app.gce_scaf_vars[i][k].get() for i, k in before}
    check("undo clears the layer change",
          app.gce_scaf_vars[1]["layer_overrides"].get() == before_lo)
    check("undo restores", after == before,
          str({k: (before[k], after[k]) for k in before
               if before[k] != after[k]}))

    # count change rebuilds the columns
    app.gce_s_count.set("2")
    pump(0.5)
    check("count change rebuilds", st["n"] == 2)

    app._gce_params_close()
    check("closes", not st["box"].winfo_ismapped())

    print("\n%d failure(s)" % len(FAILS))
    try:
        _M["root"].destroy()
    except tk.TclError:
        pass
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
