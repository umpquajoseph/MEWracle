#!/usr/bin/env python3
"""test_gui_calibration.py -- MODERN-UI ONLY: self-running tests for the
merged "Calibration" page (gui_calibration.py's card stack + gui_sweeps.py's
print engine and preview) on the real ModernApp.

Run:  python tests/test_gui_calibration.py   (prints PASS/FAIL per test, exit 1 on any
failure). Needs a display (opens a real Tk root, like test_ui_shell.py).

This is the successor to test_gui_sweeps.py, which guarded the page the
Calibration page replaced. Everything that suite guarded about the PRINT engine
is carried over verbatim -- the page mounts and is an input mode, the sw.* fields
ride the persist map, an edit debounces into a worker rebuild, the pager never
drops a slide, an oversize specimen is flagged rather than erased, the display
toggles restyle without a rebuild, the bottom bar dispatches here, Generate is
cancellable and never exports a stale geometry, and the exported bytes are the
engine's bytes.

What is new, and what each new case was proven RED against:

  * `every_card_mounts_and_applies` -- all seven cards exist and each one's
    Compute/Apply writes ITS calibration variable. RED with the card's Apply
    wired to the wrong cal.* key.
  * `lag_circle_card_recovers_a_planted_amplitude` -- typed rows reach
    mew_circle_fit.fit_amplitude through the widgets and recover a planted amplitude,
    and Apply writes the GAP beside it. RED before the card existed (the fit was
    CLI-only) and RED again with the gap write removed.
  * `gcode_overlay_toggles_without_touching_the_fibre` -- "G-code path" drives
    the commanded-path overlay and nothing else; the fibre actor is always on.
    RED against the shipped-before behaviour, where that box pointed at the
    fibre actor and the overlay was fed nothing, and RED again if the fibre is
    re-bound to the persisted-but-controlless `sw.show_depo`.
  * `plate_follows_the_build_plate_page` -- changing the plate on the Build-plate
    page changes what this page lays on. RED with a page-local plate_w/plate_h.
  * `no_square_widget_survives` -- no widget on the page mentions the jet-lag
    squares, and no sw.lag_* key persists. RED before the squares were hidden.

Added when a first use of the page asked for five changes. Each
of these was proven RED against the code as it stood, and the injection is
named in the case's own docstring:

  * `apply_reaches_the_physics_page` -- a card's Apply lands in the entry the
    user can SEE on the Physics page. This is the reported DEFECT; the
    existing `every_card_mounts_and_applies` stayed green through all of it,
    because it only ever asked whether a Tk var moved.
  * `one_tab_at_a_time` / `tab_picks_the_protocol_and_says_so` /
    `instructions_render_the_three_groups` -- the tab strip, what Generate
    targets from a tab that owns no print, and the three-group Instructions.
  * `fit_rows_add_and_remove` / `csv_fills_one_row_per_measured_circle` -- the
    lag-k measurement rows are an editable list with no hidden overflow.
  * `specimen_table_is_per_circle` / `blank_table_is_the_old_plate_byte_for_byte`
    -- the per-circle print table, and that leaving it alone still builds the
    plate the page built before it existed.
"""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import json
import os
import sys
import shutil
import tempfile
import time
import numpy as np
import tkinter as tk

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import gui_sweeps                                             # noqa: E402
import mew_circle_fit                                         # noqa: E402

FAILURES = []
RAN = []


def check(name, fn):
    RAN.append(name)
    try:
        fn()
        print(f"PASS  {name}")
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(name)
        print(f"FAIL  {name}: {exc!r}")


_M = {}


def _pump(cond, timeout=8.0):
    """Pump Tk until cond() or timeout; returns cond()'s final value."""
    root = _M["root"]
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    return cond()


#: every constant naming a preset FOLDER, and the shared default-POINTER file
#: that says which preset of each kind auto-loads. They are module-level in
#: gui_common and reach every mixin through `from gui_common import *`, so a
#: rebind has to be done per module -- patching gui_common alone changes
#: nothing.
_PRESET_DIR_NAMES = ("PRESETS_DIR", "MACHINE_PRESETS_DIR",
                     "CALIBRATION_PRESETS_DIR", "SWEEP_PRESETS_DIR")
_PRESET_FILE_NAMES = ("PRESETS_PATH",)


def _isolate_preset_dirs():
    """Point every preset folder and the default-pointer file at an empty
    scratch directory, BEFORE the app is built.

    Without this the suite reads whatever presets happen to be in the checkout:
    `_init_pstore` auto-loads each kind's default, so a developer who once
    saved a default machine preset gets a different plate than the field
    defaults describe, and `debounced_rebuild` fails on that machine and nobody
    else's. A test that depends on the user's saved
    files is testing the files.

    RED proof: with a `presets/machine/redproof.json` carrying a 60 x 40 mm
    plate and `presets/defaults.json` pointing `machine_default` at it, this suite's
    `debounced_rebuild` fails at `plate missing` before the isolation and
    passes after it."""
    scratch = tempfile.mkdtemp(prefix="mewcal_presets_")
    for mod in list(sys.modules.values()):
        name = getattr(mod, "__name__", "") or ""
        if not (name.startswith("gui_") or name.startswith("mewracle")):
            continue
        for const in _PRESET_DIR_NAMES + _PRESET_FILE_NAMES:
            if hasattr(mod, const):
                setattr(mod, const, os.path.join(scratch, const.lower()))
    _M["preset_scratch"] = scratch
    return scratch


def build():
    import gui_fonts
    import mewracle_gui
    _isolate_preset_dirs()
    root = tk.Tk()
    root.withdraw()
    gui_fonts.init_fonts(root)
    app = mewracle_gui.ModernApp(root)
    root.update()
    _M["root"], _M["app"] = root, app


# --------------------------------------------------------------------------- #
# the page itself
# --------------------------------------------------------------------------- #
def page_mounts():
    app, root = _M["app"], _M["root"]
    assert "calibration" not in app._page_frames, \
        "Calibrate is a mode of the CAD window, not a page"
    assert "sweeps" not in app._page_frames, \
        "the Experiment-sweeps page must be gone (the Calibration page replaced it)"
    frame = app._page_frames["gcode"]
    app._nav_select("gcode")
    root.update()
    design = app.gce.get_text()
    app._nav_select("calibration")
    root.update()
    assert str(app.nb.select()) == str(frame)
    assert app._cal_active(), "the Calibrate tab must enter the mode"
    assert _pump(lambda: app._cal_stash is not None), \
        "the slide never reached the page"
    assert not app.gce.read_only, "a slide is edited like a Design"
    assert app._gce_edit_groups.winfo_manager(), \
        "Add and Change selection are Design's tools and stay on Calibrate"
    app._nav_select("gcode")
    root.update()
    assert not app._cal_active() and app._cal_stash is None
    assert app.gce.get_text() == design, "the Design did not survive the visit"
    assert app._gce_edit_groups.winfo_manager(), "Design's ribbon must return"
    app._nav_select("calibration")
    root.update()
    assert _pump(lambda: app._cal_stash is not None)
    # a genuine INPUT mode: the shared bottom bar's Generate builds the target
    # slide of whichever print protocol a card selected
    assert frame not in app._noninput_frames, "calibration must be an input mode"
    assert app._active_mode() == "calibration", \
        "Generate must target the calibration print here"
    app._refresh_result_controls()
    assert app.run_btn.cget("text") == "Generate", app.run_btn.cget("text")


def travels_follow_the_tubes_box():
    """A travel is fiber: laid into it, one unbroken jet from move to move,
    in the design's colour, exactly as the designer draws it. Calibrate is
    Design with a calibration loaded, so no box of its own breaks the jet.

    Comparator: `gcode_to_fibers.merge_travels` is what continuous means here.
    It adds each travel to the front of the next fibre, so a fibre starts where
    the last one ended. Measured on the picture's own paths; the engine's own
    move list is the control, whose gaps the travels must close.

    RED PROOF (observed, before the page drew as Design): with the travels
    drawn as a separate tube actor over the unlaid fiber, the gap stayed."""
    import mew_sweeps
    app = _M["app"]

    def gap(paths):
        return max(float(np.linalg.norm(np.asarray(b)[0] - np.asarray(a)[-1]))
                   for a, b in zip(paths, paths[1:]))

    app._nav_select("calibration")
    assert _pump(lambda: app._cal_stash is not None)
    design = tuple(app._pv_parse_color(app.gce_scaf_vars[0]["color"]))
    try:
        app._cal_show_tab("sag")
        assert _pump(lambda: (app._sw_result is not None
                              and app._sw_result.kind == "sag"), timeout=30.0)
        full = getattr(app, "_gce_pv_full", None)
        app._gce_pv_kick()
        assert _pump(lambda: getattr(app, "_gce_pv_full", None) is not full,
                     timeout=60.0), "the build never landed"
        moves = mew_sweeps.extract_moves(app._sw_result.gcode_texts[0])[0]
        flat = gap([np.asarray(m)[:, :2] for m in moves])
        assert flat > 0.5, f"the control shows no gap: {flat}"
        tube = gap([np.asarray(p)[:, :2] for p in app._gce_pv_full[0]])
        assert tube < 0.05 * flat, f"the fiber is broken: {tube} (flat {flat})"
        assert tuple(app._gce_pv_color()) == design, "not the design's colour"
        assert tuple(app._gce_pv_state["color"]) == design
    finally:
        app._nav_select("gcode")
        _M["root"].update()


def _fill_sag_ladder(spans=(0.3, 0.6, 1.2, 2.0), coeff=0.35, exp=1.8,
                     readings=1):
    """Type a ladder into the sag card: droop = coeff * span^exp on every row.

    Four spans over 6.7x, so the refusal rule does not bite and the exponent is
    fitted. `readings` fills that many cells on each row with the SAME number, so
    the geometric mean of the row is still that number."""
    app = _M["app"]
    for i, (svar, _first) in enumerate(app._fit_sag_rows):
        dvars = app._fit_sag_depths[i]
        if i < len(spans):
            svar.set("%g" % spans[i])
            for j, dvar in enumerate(dvars):
                dvar.set("%.6g" % (coeff * spans[i] ** exp)
                         if j < readings else "")
        else:
            svar.set("")
            for dvar in dvars:
                dvar.set("")


def every_card_mounts_and_applies():
    """All seven cards exist, in the registry's order, and each one's Apply
    writes ITS calibration variable and nothing else.

    RED proof: pointing the fusion card's Apply at "cal.sag_coeff" fails here
    with `cal.fusion_overlap unchanged`, because the assertion is per-key rather
    than "some cal var moved"."""
    import calibration_procedures
    app = _M["app"]
    keys = list(calibration_procedures.PROCEDURES)
    assert list(app._cal_cards) == keys, \
        f"card order must be the registry's: {list(app._cal_cards)} != {keys}"

    # (card key, the cal.* var it must write, the inputs that make it compute)
    plan = [
        # the box is mm/MIN, as printed; Apply writes the Physics page's mm/s
        ("cts", "cal.cts_ref",
         lambda: app._fit_cts_val.set("495")),
        ("viscosity", "cal.mat_viscosity_ref",
         lambda: (app._fit_visc_press.set("3"), app._fit_visc_temp.set("100"),
                  app._fit_visc_speed.set("10"), app._fit_visc_diam.set("0.02"))),
        ("activation_energy", "cal.mat_activation_energy",
         # hotter melt, thinner viscosity, THICKER fibre: a positive Ea. The
         # rows here once ran the other way, a negative Ea the card now refuses
         lambda: ([app._fit_ea_rows[0][0].set("90"),
                   app._fit_ea_rows[0][1].set("0.020"),
                   app._fit_ea_rows[1][0].set("110"),
                   app._fit_ea_rows[1][1].set("0.030")])),
        ("sag", "cal.sag_coeff", _fill_sag_ladder),
        ("fusion", "cal.fusion_overlap",
         lambda: (app._fit_fus["junction"]["rows"][0][0].set("5"),
                  app._fit_fus["junction"]["rows"][0][1].set("0.06"),
                  app._fit_fus_diam.set("0.02"))),
    ]
    for key, calkey, fill in plan:
        before = app.cal_vars[calkey].get()
        fill()
        getattr(app, {"cts": "_apply_fit_cts", "viscosity": "_apply_fit_visc",
                      "activation_energy": "_apply_fit_ea",
                      "sag": "_apply_fit_sag",
                      "fusion": "_apply_fit_fusion_junction"}[key])()
        after = app.cal_vars[calkey].get()
        assert after != before, f"{key} card left {calkey} unchanged ({after!r})"
        assert float(after) == float(after), f"{calkey} is not a number"
    assert abs(float(app.cal_vars["cal.cts_ref"].get()) - 8.25) < 1e-9, \
        "the CTS card must write the typed 495 mm/min as 8.25 mm/s"


def _physics_entries_by_var():
    """{tcl variable name -> the widget on the Physics page bound to it}."""
    app = _M["app"]
    found = {}

    def walk(w):
        for c in w.winfo_children():
            try:
                name = str(c.cget("textvariable"))
            except tk.TclError:
                name = ""
            if name:
                # a converted field shows a display var (gui_units): key it
                # by the canonical var it stands for
                found.setdefault(gui_units.canonical_name(name), c)
            walk(c)

    import gui_units
    walk(app.tab_physics)
    return found


def apply_reaches_the_physics_page():
    """A card's Apply must land in the entry the user can SEE on the Physics
    page -- for every key any card writes, not just CTS -- and survive a page
    switch and the physics preset store.

    The defect this pins (reported 2026-09-03): "I tried putting in measured CTS
    in the calibration field and hit Apply to calibration but now the CTS entry
    in the Physics section is empty and didn't get updated." Both halves of that
    sentence are one cause. `_init_calibration_vars` rebuilt `self.cal_vars` on
    its SECOND caller, so the Physics page's entries -- built first, and holding
    only the Tcl variable's NAME -- pointed at variables tkinter had unset in
    `StringVar.__del__`. They rendered empty, and Apply wrote the replacement
    set that no widget was bound to.

    RED proof (measured, with the idempotence guard removed from
    `gui_calibration._init_calibration_vars`): this case fails at
    `no Physics-page entry is bound to cal.cts_ref` -- the live var was
    `PY_VAR418` while the Physics CTS entry was bound to `PY_VAR72`, and reading
    that entry returned ''. 36 of the Physics page's 42 bound variables were
    dead the same way. It is not enough to assert `cal_vars[key]` moved:
    `every_card_mounts_and_applies` did exactly that and stayed green through
    the whole defect, because it never looked at a widget."""
    app, root = _M["app"], _M["root"]
    # every cal.* key any of the seven cards writes
    written = ("cal.cts_ref", "cal.mat_viscosity_ref",
               "cal.mat_activation_energy", "cal.lag_amplitude",
               "cal.lag_amplitude_gap", "cal.sag_coeff", "cal.fusion_overlap")
    ents = _physics_entries_by_var()
    # the Fusion card's junction entry shows a DISPLAY copy (gui_fusion_input:
    # it can read the sink as a coefficient or a distance), refreshed on every
    # cal var write, so that copy is what Apply must reach
    shown = dict(getattr(app, "_phys_fusion_input", (None, None, {}))[2])
    for key in written:
        var = shown.get(key) or app.cal_vars[key]
        assert str(var) in ents, \
            f"no Physics-page entry is bound to {key} (its var is {var})"
    # nothing on the page may be bound to a variable Tcl no longer has
    dead = [n for n in ents
            if not root.getboolean(root.call("info", "exists", n))]
    assert not dead, \
        f"{len(dead)} Physics-page entries are bound to dead Tcl variables"

    # the real flow: type a CTS on the card, press Apply, read the WIDGET
    app._fit_cts_val.set("495")             # mm/min -> 8.25 mm/s
    app._apply_fit_cts()
    root.update()
    ent = ents[str(app.cal_vars["cal.cts_ref"])]
    import gui_units          # the entry shows the display unit, not mm/s
    want = gui_units.convert_text("8.25", "speed", "mm/s",
                                  gui_units.unit("speed"))
    assert ent.get() == want, \
        f"the Physics page's CTS entry shows {ent.get()!r} after Apply"

    # ...and it survives leaving the page and coming back
    app.nb.select(app._page_frames["scaffold"])
    root.update()
    app._nav_select("calibration")
    root.update()
    assert ent.get() == want, \
        f"the CTS entry lost its value on a page switch ({ent.get()!r})"

    # ...and reaches the physics preset store as ONE value, not two
    pmap = app._persist_map()
    assert "cal.cts_ref" in app._calibration_persist_keys()
    assert pmap["cal.cts_ref"] is app.cal_vars["cal.cts_ref"], \
        "the preset map holds a different Tk var than the cards write"
    assert pmap["cal.cts_ref"].get() == "8.25"


def lag_circle_card_recovers_a_planted_amplitude():
    """The lag3 card reaches mew_circle_fit.fit_amplitude through its OWN widgets and
    recovers a planted amplitude; Apply writes A AND the gap it was fitted at.

    The rows are built the way test_circle_sweep.py plants one: a commanded
    radius from `commanded_radius`, the deposited one from `contact_radius` at
    the truth A, so the fit has exactly one right answer.

    RED proof: this case could not be written before the card existed -- the fit
    was reachable only as `python analysis/mew_circle_fit.py <csv>`. With the
    `cal.lag_amplitude_gap` write removed from `_apply_fit_circles` it fails on
    the gap assertion, which is the defect that made the field write-only."""
    app = _M["app"]
    gap, truth = 4.5, 1.35 * mew_circle_fit.A_DEFAULT
    for i, eps in enumerate((2.0, 3.0, 4.0, 6.0)):
        R = mew_circle_fit.commanded_radius(eps, gap=gap)
        r = mew_circle_fit.contact_radius(R, eps, gap=gap, amp=truth)
        lab, e, c, d = app._fit_circ_rows[i]
        lab.set(f"eps{eps:g}")
        e.set("%g" % eps)
        c.set("%.9g" % R)
        d.set("%.9g" % r)
    for quad in app._fit_circ_rows[4:]:
        for var in quad:
            var.set("")
    app._fit_circ_gap.set("%g" % gap)
    app._compute_fit_circles()
    fit = app._fit_circ_result
    assert fit is not None, app._fit_circ_out.cget("text")
    assert abs(fit.A - truth) < 1e-6, f"recovered {fit.A}, planted {truth}"
    shown = app._fit_circ_out.cget("text")
    assert "lag amplitude A" in shown, shown

    app.cal_vars["cal.lag_amplitude"].set("1")
    app.cal_vars["cal.lag_amplitude_gap"].set("3.5")
    app._apply_fit_circles()
    assert abs(float(app.cal_vars["cal.lag_amplitude"].get()) - truth) < 1e-5, \
        "Apply did not write A"
    assert abs(float(app.cal_vars["cal.lag_amplitude_gap"].get()) - gap) < 1e-9, \
        "Apply wrote A without the gap it is conditional on"




def fit_rows_add_and_remove():
    """The lag3 card's measurement rows are a real editable list, and Compute
    reads EXACTLY the rows on screen -- no fixed count, no hidden overflow.

    The card used to offer a fixed eight rows plus a `_fit_circ_extra` list that
    a longer CSV spilled into. Those extra rows were fitted but could not be
    seen or corrected, which is the one thing a
    measurement form must not do.

    The check that matters is the last one: it plants k on the rows it KEEPS
    and a different k on the row it removes, so a removed row still reaching
    `fit_amplitude` moves the recovered value. Counting rows alone would not catch that.

    RED proof: restoring the overflow (`rows += self._fit_circ_extra` in
    `_compute_fit_circles`, and `_fill_fit_circle_rows` spilling past a fixed
    eight) fails at `Compute read a row that is not on screen: recovered
    1.0891, planted 1.35`. Making `_fit_circ_remove_row` a no-op fails earlier
    at `remove did not drop a row (10 left)`."""
    app, root = _M["app"], _M["root"]
    app._cal_show_tab("lag3")
    root.update()
    assert not hasattr(app, "_fit_circ_extra"), \
        "the hidden overflow list must be gone"

    n0 = len(app._fit_circ_rows)
    a = app._fit_circ_add_row()
    b = app._fit_circ_add_row()
    root.update()
    assert len(app._fit_circ_rows) == n0 + 2, "Add row did not add"
    assert app._fit_circ_widgets[-1][0].winfo_exists(), \
        "the added row has no widgets"
    app._fit_circ_remove_row(a)
    root.update()
    assert len(app._fit_circ_rows) == n0 + 1, \
        f"remove did not drop a row ({len(app._fit_circ_rows)} left)"
    assert a not in app._fit_circ_rows and b in app._fit_circ_rows, \
        "remove dropped the wrong row"

    # ...and the last row is kept rather than leaving nothing to type into
    while len(app._fit_circ_rows) > 1:
        app._fit_circ_remove_row(app._fit_circ_rows[-1])
    app._fit_circ_remove_row(app._fit_circ_rows[0])
    assert len(app._fit_circ_rows) == 1, "the card must keep one row"

    # the load-bearing one: a removed row must not reach the fit
    gap, truth, decoy = (4.5, 1.35 * mew_circle_fit.A_DEFAULT,
                        0.40 * mew_circle_fit.A_DEFAULT)
    while len(app._fit_circ_rows) < 5:
        app._fit_circ_add_row()
    for i, eps in enumerate((2.0, 3.0, 4.0, 6.0)):
        R = mew_circle_fit.commanded_radius(eps, gap=gap)
        r = mew_circle_fit.contact_radius(R, eps, gap=gap, amp=truth)
        lab, e, c, d = app._fit_circ_rows[i]
        lab.set(f"eps{eps:g}"); e.set("%g" % eps)
        c.set("%.9g" % R); d.set("%.9g" % r)
    # a fifth row carrying a WRONG k, then removed
    R = mew_circle_fit.commanded_radius(3.0, gap=gap)
    bad = app._fit_circ_rows[4]
    bad[0].set("decoy"); bad[1].set("3")
    bad[2].set("%.9g" % R)
    bad[3].set("%.9g" % mew_circle_fit.contact_radius(R, 3.0, gap=gap, amp=decoy))
    app._fit_circ_gap.set("%g" % gap)
    app._compute_fit_circles()
    assert app._fit_circ_result is not None
    assert abs(app._fit_circ_result.A - truth) > 1e-3, \
        "the decoy row was supposed to move the fit while it was on screen"

    app._fit_circ_remove_row(bad)
    root.update()
    app._compute_fit_circles()
    fit = app._fit_circ_result
    assert fit is not None, app._fit_circ_out.cget("text")
    assert abs(fit.A - truth) < 1e-6, \
        (f"Compute read a row that is not on screen: recovered {fit.A:.4f}, "
         f"planted {truth}")


def csv_fills_one_row_per_measured_circle():
    """Load CSV… leaves exactly as many rows as the file has -- it grows the
    list past the opening eight and shrinks it below them.

    RED proof: with `_fill_fit_circle_rows` writing into a fixed eight rows,
    the 12-row case fails at `12 rows in the file, 8 on screen`, and the
    2-row case leaves 8 (six of them stale blanks)."""
    app, root = _M["app"], _M["root"]
    app._cal_show_tab("lag3")
    gap = 3.9

    def rows_for(n):
        out = []
        for i in range(n):
            eps = 2.0 + i * 0.5
            R = mew_circle_fit.commanded_radius(eps, gap=gap)
            out.append(mew_circle_fit.CircleRow(
                eps=eps, commanded_r_mm=R, label=f"s{i}",
                deposited_r_mm=mew_circle_fit.contact_radius(
                    R, eps, gap=gap, amp=mew_circle_fit.A_DEFAULT)))
        return out

    for n in (12, 2, 8):
        app._fill_fit_circle_rows(rows_for(n), gap)
        root.update()
        assert len(app._fit_circ_rows) == n, \
            f"{n} rows in the file, {len(app._fit_circ_rows)} on screen"
        assert len(app._fit_circ_widgets) == n, "widgets and vars disagree"
        assert len(app._fit_circle_rows()) == n, \
            "a loaded row is not readable by the fit"


def specimen_table_is_per_circle():
    """The lag3 PRINT block carries a row per speed ratio -- ε, reps, and the
    radius the generator will actually command -- and each is editable on its
    own. It replaced three universal fields that could only say the same thing
    about every specimen.

    The radius column must show the ENGINE's number, so this compares it
    against `mew_sweeps.circle_table` on the same params rather than against a
    formula written here.

    RED proofs, both run: feeding `specimens=None` unconditionally in
    `_sw_snapshot_params` fails at `the edit did not build a table`; dropping
    the sticky override flag from `_sw_circ_read_rows` fails at `Add row
    dropped the radius override: '2:2, 3:5, 4:2, 6:2, 7:2'` -- which is not a
    hypothetical, it is the defect this case caught while it was being
    written."""
    import mew_sweeps
    app, root = _M["app"], _M["root"]
    app._cal_show_tab("lag3")
    app.sw_vars["circ_specimens"].set("")
    root.update()

    kind, params = app._sw_snapshot_params()
    assert kind == "circle"
    assert params.specimens is None, \
        "a blank table must pass specimens=None, never an empty list"
    table = mew_sweeps.circle_table(params)
    assert len(app._sw_circ_rows) == len(table), \
        f"{len(app._sw_circ_rows)} widget rows, engine table has {len(table)}"
    for i, (row, spec) in enumerate(zip(app._sw_circ_rows, table)):
        assert abs(float(row["eps"].get()) - spec.eps) < 1e-9
        assert int(row["reps"].get()) == int(spec.reps)
        assert abs(float(row["rad"].get()) - spec.radius_mm) < 5e-3, \
            (f"row {i} shows {row['rad'].get()}, the engine will command "
             f"{spec.radius_mm:.4g}")

    # a per-row rep count reaches the engine
    n0 = len(mew_sweeps.generate("circle", params).rows)
    app._sw_circ_rows[1]["reps"].set("5")
    root.update()
    _kind, p2 = app._sw_snapshot_params()
    assert p2.specimens is not None, "the edit did not build a table"
    n1 = len(mew_sweeps.generate("circle", p2).rows)
    assert n1 == n0 + 3, \
        f"an edited rep count did not reach the engine ({n1} rows, wanted {n0 + 3})"

    # a per-row radius override is taken verbatim, and only on that row
    app._sw_circ_rows[0]["rad"].set("12.5")
    root.update()
    _kind, p3 = app._sw_snapshot_params()
    t3 = mew_sweeps.circle_table(p3)
    assert abs(t3[0].radius_mm - 12.5) < 1e-9, \
        f"the override was not taken verbatim ({t3[0].radius_mm})"
    assert abs(t3[1].radius_mm - table[1].radius_mm) < 1e-9, \
        "overriding one ring moved another ring's radius"

    # ...and it SURVIVES an add and a remove. It did not before: circle_table
    # echoes an override back into the cell, so a text comparison alone handed
    # the ring silently back to the sizing rule one refresh later.
    app._sw_circ_add_specimen()
    root.update()
    assert ":12.5" in app.sw_vars["circ_specimens"].get(), \
        (f"Add row dropped the radius override: "
         f"{app.sw_vars['circ_specimens'].get()!r}")
    app._sw_circ_remove_specimen(app._sw_circ_rows[-1])
    root.update()
    assert ":12.5" in app.sw_vars["circ_specimens"].get(), \
        "remove dropped the radius override"
    app._sw_circ_reset_table()
    root.update()


def blank_table_is_the_old_plate_byte_for_byte():
    """With the table untouched the plate is the one the page built before the
    table existed -- byte for byte, g-code and CSV.

    This is the contract that makes the table safe to ship: `specimens=None`
    keeps `circ_eps` / `circ_reps` / `circ_reps_eps` as the truth, and those
    three keys are untouched. "Reset to law sizing" is the way back to it, so
    the case checks the reset lands there and not merely somewhere near.

    RED proof: making `_sw_circ_specs` return the resolved table instead of
    None on a blank key fails at `the reset plate differs from the untouched
    one (slide 1)` -- the resolved radii round-trip through the string at 4
    significant figures, so the commanded arcs move."""
    import mew_sweeps
    app, root = _M["app"], _M["root"]
    app._cal_show_tab("lag3")
    app.sw_vars["circ_specimens"].set("")
    root.update()
    _kind, p0 = app._sw_snapshot_params()
    base = mew_sweeps.generate("circle", p0)

    # churn the table, then reset
    app._sw_circ_rows[0]["reps"].set("4")
    root.update()
    app._sw_circ_rows[0]["rad"].set("11.0")
    root.update()
    app._sw_circ_add_specimen()
    root.update()
    assert app.sw_vars["circ_specimens"].get(), "the churn wrote no table"
    app._sw_circ_reset_table()
    root.update()

    _kind, p1 = app._sw_snapshot_params()
    assert p1.specimens is None,         f"Reset to law sizing left a table behind ({p1.specimens})"
    after = mew_sweeps.generate("circle", p1)
    assert len(after.gcode_texts) == len(base.gcode_texts)
    for i, (a, b) in enumerate(zip(after.gcode_texts, base.gcode_texts), 1):
        assert a == b, f"the reset plate differs from the untouched one (slide {i})"
    assert after.csv_text == base.csv_text, "the measurement CSV differs"
    # the three universal keys are still there, still the truth when blank
    for k in ("sw.circ_eps", "sw.circ_reps", "sw.circ_reps_eps"):
        assert k in app._persist_map(), f"{k} was dropped"
    assert "sw.circ_specimens" in app._sweep_persist_keys(), \
        "the table must persist as one sw.* key"


def no_square_widget_survives():
    """The jet-lag SQUARES are hidden from the UI. The engine (generate_lag) is
    untouched, but nothing on the page reaches it.

    RED proof: before the hide, this fails on `sw.lag_eps` in the persist map and
    on the "Jet-lag squares" radio text."""
    app = _M["app"]
    keys = [k for k in app._persist_map() if k.startswith("sw.lag_")]
    assert not keys, f"jet-lag square keys still persist: {sorted(keys)}"
    assert not [k for k in app.sw_vars if k.startswith("lag_")], \
        "jet-lag square vars still exist"

    bad = []

    def walk(w):
        try:
            txt = str(w.cget("text"))
        except Exception:  # noqa: BLE001 -- most widgets have no -text
            txt = ""
        low = txt.lower()
        if "square" in low or "jet-lag square" in low:
            bad.append(txt)
        for kid in w.winfo_children():
            walk(kid)

    walk(app._page_frames["gcode"])
    assert not bad, f"square widgets still on the page: {bad[:4]}"
    # ...and a saved preset carrying the retired kind must not brick the page
    app.sw_vars["kind"].set("lag")
    kind, _params = app._sw_snapshot_params()
    assert kind == "cts", f"a stale 'lag' kind must degrade, got {kind!r}"
    app.sw_vars["kind"].set("cts")


# --------------------------------------------------------------------------- #
# the app-owned inputs (no duplicates)
# --------------------------------------------------------------------------- #
def plate_follows_the_build_plate_page():
    """The plate the calibration print is laid on IS the Build-plate page's.

    RED proof: with the old page-local sw.plate_w/plate_h the snapshot returns
    76.2 x 50.8 whatever the Build-plate page says, so the first assert fails."""
    app = _M["app"]
    assert "plate_w" not in app.sw_vars and "plate_h" not in app.sw_vars, \
        "the duplicate plate fields must be gone"
    assert "origin_x" not in app.sw_vars and "origin_y" not in app.sw_vars, \
        "the duplicate origin fields must be gone"
    old = (app.plate_x.get(), app.plate_y.get())
    try:
        app.plate_x.set("60")
        app.plate_y.set("40")
        _kind, params = app._sw_snapshot_params()
        assert (params.slide_w, params.slide_h) == (60.0, 40.0), \
            f"plate size did not follow: {params.slide_w} x {params.slide_h}"
        # the plate is anchored at (0,0) since Plate corner X/Y went
        assert (params.origin_x, params.origin_y) == (0.0, 0.0), \
            f"plate origin: {params.origin_x}, {params.origin_y}"
        app._sw_update_cts_readout()
        assert "60" in app._sw_plate_var.get(), app._sw_plate_var.get()
    finally:
        app.plate_x.set(old[0])
        app.plate_y.set(old[1])








# --------------------------------------------------------------------------- #
# the preview
# --------------------------------------------------------------------------- #
def debounced_rebuild():
    app = _M["app"]
    app.sw_vars["kind"].set("cts")
    app._sw_state["paths"] = None
    app._sw_state["dirty"] = True
    app.sw_vars["cts_reps"].set("2")           # a real edit
    assert _pump(lambda: (app._sw_result is not None
                          and app._sw_result.kind == "cts"
                          and len(app._sw_result.rows) == 7 * 2
                          and not app._sw_state.get("dirty")
                          and app._sw_state.get("paths") is not None)), \
        f"no rebuild ({app._sw_state.get('status')!r})"
    assert app._sw_state.get("travels"), "travel polylines missing"
    plates = app._sw_state.get("plates")
    assert plates and plates[0] == (0.0, 0.0, 76.2, 50.8), "plate missing"
    assert app._sw_state.get("labels"), "specimen labels missing"
    assert app._sw_summary_var.get(), "summary readout empty"




def gcode_overlay_toggles_without_touching_the_fibre():
    """"G-code path" draws the COMMANDED path on its own actor, and it can never
    hide the deposited fibre, which is always drawn.

    RED proof (this is defect 1): before the fix `sw_show_gcode` was bound to the
    fibre actor in `sync_visibility`, so turning the overlay off hid the FIBRE --
    the first assert here fails -- and the inherited overlay was never fed
    `gcode_paths`, so `_gc_actor` had nothing and the overlay assert fails too.
    Re-binding `self._actor.SetVisibility(show_gcode)` reproduces both.

    SECOND RED PROOF, for the 2026-09-20 change that dropped the "Deposited"
    checkbox (see `_sw_build_preview_options` for why it went): re-binding
    `self._actor.SetVisibility(bool(app.sw_show_depo.get()))` passes the overlay
    asserts and fails the last one. `sw.show_depo` is still a persisted key, so a
    calibration preset saved while the checkbox existed can load False into it --
    and with no checkbox left there is no way to bring the fibre back. The fibre
    actor is unconditional, and this case is what says so."""
    app = _M["app"]
    panel = app._sw_panel
    assert _pump(lambda: app._sw_state.get("paths") is not None)
    meta = app._sw_state.get("meta") or {}
    assert meta.get("gcode_paths"), \
        "the page never handed the overlay a commanded path"

    # The scene is only built once the host is MAPPED (gui_preview:665), and the
    # suite runs on a withdrawn root. Show the window for this one case: it is
    # ABOUT the actors, so skipping when they are absent would make it
    # unfalsifiable.
    root = _M["root"]
    root.deiconify()
    root.update()
    try:
        panel._ensure_scene()
        if panel._dead or panel._actor is None:
            raise AssertionError(
                "VTK unavailable: this case cannot judge the actors")
        panel.apply_state(app._sw_state, "")
        _check_path_actors(app, panel)
    finally:
        root.withdraw()
        root.update()


def _check_path_actors(app, panel):
    """The three states, once the scene exists."""
    app.sw_show_depo.set(True)
    app.sw_show_gcode.set(False)
    _M["root"].update()
    assert panel._actor.GetVisibility(), \
        "the fibre must be visible with the overlay OFF"
    assert not panel._gc_actor.GetVisibility(), "the g-code overlay must be off"

    app.sw_show_gcode.set(True)
    _M["root"].update()
    assert panel._actor.GetVisibility(), \
        "the fibre must still be visible with the overlay ON"
    assert panel._gc_actor.GetVisibility(), \
        "'G-code path' on must show the COMMANDED overlay"

    # The stale-preset case: sw.show_depo still persists, its checkbox does not.
    # Driven the way a preset load drives it -- the var no longer carries a
    # display trace, so a bare .set() reaches nothing; what a loaded preset DOES
    # reach is a rebuild, and every rebuild ends in sync_visibility.
    app.sw_show_depo.set(False)
    panel.sync_visibility()
    _M["root"].update()
    assert panel._actor.GetVisibility(), \
        "a preset carrying sw.show_depo=False must not hide the fibre -- " \
        "there is no longer a control to bring it back"

    app.sw_show_depo.set(True)
    app.sw_show_gcode.set(False)










# --------------------------------------------------------------------------- #
# the bottom bar, presets and export
# --------------------------------------------------------------------------- #




def one_tab_at_a_time():
    """Seven tabs, one card mapped at a time, and the selection is a persisted
    sw.* key so the page reopens where it was left.

    This replaced a stack of seven cards on one scrolling page. Selecting a tab must
    MAP that card and unmap the other six --
    `winfo_ismapped` and not merely `winfo_exists`, because every card is built
    up front and only gridded/ungridded, which is what lets typed fit inputs
    survive a tab switch.

    RED proof: with `_cal_show_tab`'s `frame.grid_remove()` loop replaced by a
    bare `frame.grid()` (the old stack-everything behaviour) this fails with
    `7 cards mapped at once, wanted only viscosity`. Removing "cal_tab" from
    `_sw_vars_make` fails it at `sw.cal_tab must persist the selected tab`."""
    import calibration_procedures
    app, root = _M["app"], _M["root"]
    keys = list(calibration_procedures.PROCEDURES)
    app._nav_select("calibration")
    root.update()
    assert "sw.cal_tab" in app._persist_map(), \
        "sw.cal_tab must persist the selected tab"
    assert "sw.cal_tab" in app._sweep_persist_keys()

    def shown():
        # grid_info, not winfo_ismapped: a closed module card's body is
        # covered by the tiles, so "mapped" would not say which tab it holds.
        # Every part of a tab must agree -- one tab's settings over another's
        # fit form is the failure this is for.
        out = []
        for k, parts in app._cal_cards.items():
            on = {p for p, f in parts.items() if f.grid_info()}
            if on:
                assert on == set(parts), f"{k}: only {sorted(on)} shown"
                out.append(k)
        return out

    for key in keys:
        app._cal_show_tab(key)
        root.update()
        mapped = shown()
        assert mapped == [key], \
            f"{len(mapped)} cards mapped at once, wanted only {key}: {mapped}"
        assert app._persist_map()["sw.cal_tab"].get() == key, \
            f"selecting {key} did not persist"
        # the Type tool follows the selection, wherever it came from
        assert app._cal_type_short() in app._cal_tools["type"]._text.cget(
            "text"), f"the Type tool did not follow the selection to {key}"

    # a preset load writes the var directly; the page must follow that too
    app._cal_tab_var.set("sag")
    root.update()
    assert shown() == ["sag"]
    app._cal_show_tab("cts")
    root.update()


def tab_picks_the_protocol_and_says_so():
    """Selecting a tab that owns a print retargets the shared bottom bar;
    selecting one that does not leaves the bar alone AND says which print it
    would still build.

    That sentence is the whole point. Five of the seven quantities have no
    generator, so a tab switch cannot always retarget Generate -- and a
    Generate that silently builds the print from a tab you are no longer
    looking at is the failure this guards. The page states the target in words
    on every tab instead.

    RED proof: making `_cal_show_tab` set `sw_vars["kind"]` to "" on a
    protocol-less tab fails at `the bar's target must not change`; blanking
    `_cal_target_text`'s no-protocol branch fails at `the sag tab must name the
    print Generate would build`."""
    import calibration_procedures
    app = _M["app"]
    procs = calibration_procedures.PROCEDURES
    protos = {k: p.print_protocol for k, p in procs.items() if p.print_protocol}
    # viscosity ("visc") and activation energy ("diam") are SEPARATE protocols
    # since 2026-09-05 -- see `the_two_diameter_tabs_are_separate_designs`.
    # "tube" is the mandrel lag tab's since 2026-09-25, and "cts4" the
    # mandrel CTS tab's since the same day.
    assert set(protos.values()) == {"cts", "cts4", "circle", "diam", "visc",
                                    "sag", "tube"}, protos

    for key, proto in protos.items():
        app._cal_show_tab(key)
        kind, _params = app._sw_snapshot_params()
        assert kind == proto, f"the {key} tab did not retarget the bar ({kind})"
        assert app.sw_vars["kind"].get() == proto
        assert proto in app._cal_target_text(key) or \
            app._PROTOCOL_NAMES[proto] in app._cal_target_text(key), \
            f"the {key} tab does not say what Generate builds"

    # ...and a tab with no print leaves the target where the last one put it
    app._cal_show_tab("lag3")
    assert app.sw_vars["kind"].get() == "circle"
    for key in [k for k in procs if not procs[k].print_protocol]:
        app._cal_show_tab(key)
        assert app.sw_vars["kind"].get() == "circle", \
            f"the {key} tab (no print protocol) must not change the bar's target"
        text = app._cal_target_text(key)
        assert app._PROTOCOL_NAMES["circle"] in text, \
            f"the {key} tab must name the print Generate would build: {text!r}"
        assert "Jet lag (plate)" in text, \
            f"the {key} tab must name the tab that print came from: {text!r}"
    app._cal_show_tab("cts")


def _clear_ladder_fits(app):
    """Empty both ladder fit forms and forget what was seeded into them.

    Cases run in one long-lived app, and `every_card_mounts_and_applies` types real
    numbers into these fields earlier in the run. The seeding contract is
    deliberately "blank, or still the value we wrote" -- so without this the two
    cases below would be asserting against another case's typing and would report
    the contract as broken when it is working."""
    for tvar, dvar in getattr(app, "_fit_ea_rows", []):
        tvar.set("")
        dvar.set("")
    for dvars in getattr(app, "_fit_ea_diams", []):
        for dvar in dvars:
            dvar.set("")
    for dvar in getattr(app, "_fit_visc_diams", []):
        dvar.set("")
    for name in ("_fit_visc_press", "_fit_visc_temp", "_fit_visc_speed",
                 "_fit_visc_diam"):
        var = getattr(app, name, None)
        if var is not None:
            var.set("")
    app._cal_diam_seeded = {}
    for svar, _first in getattr(app, "_fit_sag_rows", []):
        svar.set("")
    for dvars in getattr(app, "_fit_sag_depths", []):
        for dvar in dvars:
            dvar.set("")
    app._cal_sag_seeded = {}


def the_two_diameter_tabs_are_separate_designs():
    """Viscosity and activation energy run the same GENERATOR and never the same
    DESIGN. Editing one must not move the other.

    They shared one protocol and one set of `sw.diam_*` variables until
    2026-09-05. The runbook does derive both from one ladder whose hottest rung is
    the anchor, but the two are recalibrated on different clocks -- viscosity
    weekly as the melt ages, activation energy once per polymer batch -- so the
    weekly run must not reprint or silently redesign a five-rung ladder.

    The viscosity plate is ONE slide at ONE temperature: a one-rung ladder through
    the same `generate_diameter`. Pasting several numbers into a field labelled
    "Print temperature (°C)" must still lay one slide, not quietly become a
    ladder.

    Each card's conditions line is a computed readout rather than a `sw.*`
    variable, so it is stored per MOUNT in a list. That is what the shared version
    got wrong -- a single attribute meant the second card's build orphaned the
    first card's label and the Viscosity card rendered the line BLANK -- and the
    list form is kept so remounting either protocol cannot bring it back.

    RED PROOFS (observed): pointing the visc branch of `_sw_snapshot_params` back
    at `diam_temps` fails at `AssertionError('the viscosity plate followed the Ea
    ladder: [100.0, 90.0, 80.0]')`; dropping the `[:1]` truncation fails at
    `AssertionError('a pasted ladder became 3 slides')`; restoring the
    single-attribute note form fails at `AssertionError('the viscosity card has no
    conditions line')`."""
    app = _M["app"]
    app._cal_show_tab("viscosity")
    app.sw_vars["visc_temp"].set("95")
    app.sw_vars["visc_speed"].set("600")
    app.sw_vars["diam_temps"].set("100 90 80")
    app.sw_vars["diam_speed"].set("700")
    app._sw_update_cts_readout()

    kind, params = app._sw_snapshot_params()
    assert kind == "visc", kind
    assert params.temps == [95.0], \
        "the viscosity plate followed the Ea ladder: %r" % (params.temps,)
    assert params.speed == 600.0, params.speed

    app._cal_show_tab("activation_energy")
    kind, params = app._sw_snapshot_params()
    assert kind == "diam", kind
    assert params.temps == [100.0, 90.0, 80.0], params.temps
    assert params.speed == 700.0, params.speed

    # a pasted ladder in the one-temperature field is still one slide
    app._cal_show_tab("viscosity")
    app.sw_vars["visc_temp"].set("95 85 75")
    _kind, params = app._sw_snapshot_params()
    assert len(params.temps) == 1, \
        "a pasted ladder became %d slides" % len(params.temps)
    app.sw_vars["visc_temp"].set("95")

    # every mounted card carries its own live conditions line, and they differ
    # because the two designs carry different feeds
    app._sw_update_cts_readout()
    vnotes = [v.get() for v in getattr(app, "_sw_visc_notes", [])]
    dnotes = [v.get() for v in getattr(app, "_sw_diam_notes", [])]
    assert vnotes and all(n.strip() for n in vnotes), \
        "the viscosity card has no conditions line"
    assert dnotes and all(n.strip() for n in dnotes), \
        "the Ea card has no conditions line"
    assert set(vnotes).isdisjoint(dnotes), \
        "both cards show one design's feed: %r" % (vnotes,)

    app.sw_vars["diam_temps"].set("100 92.5 85 77.5 70")
    app.sw_vars["diam_speed"].set("700")
    app.sw_vars["visc_temp"].set("100")
    app.sw_vars["visc_speed"].set("700")


def every_protocol_tab_actually_generates():
    """Every tab that owns a print can RUN its print, and gets its own result.

    The page dispatches Generate on `print_protocol`, but the engine looks the
    kind up in `mew_sweeps.GENERATORS`, and nothing tied the two together. When
    the viscosity plate was split off the ladder it got a protocol, a variable
    set, a form, a target line and a snapshot that all read "visc" -- and no
    GENERATORS entry, so `generate` raised `unknown experiment kind 'visc'`, the
    preview swallowed it, and the Viscosity tab rendered the CTS sweep's result
    while claiming to build the viscosity plate. Every other case passed: they
    all stopped at `_sw_snapshot_params`, which was correct.

    So this one goes one step further and runs the generator. Asserting the
    GENERATORS key exists would not be enough -- a key pointing at the wrong
    function still passes that.

    RED PROOF (observed): deleting the "visc" entry from `mew_sweeps.GENERATORS`
    fails at `AssertionError('the viscosity tab cannot generate: ValueError("unknown
    experiment kind \\'visc\\' -- expected one of circle, cts, diam, lag")')`."""
    import calibration_procedures
    import mew_sweeps
    app = _M["app"]
    summaries = {}
    for key, proc in calibration_procedures.PROCEDURES.items():
        if not proc.print_protocol:
            continue
        app._cal_show_tab(key)
        kind, params = app._sw_snapshot_params()
        try:
            res = mew_sweeps.generate(kind, params)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                "the %s tab cannot generate: %r" % (key, exc))
        assert res.n_slides >= 1, f"{key}: no slides"
        assert all(t.strip() for t in res.gcode_texts), f"{key}: empty g-code"
        assert ";" not in "".join(res.gcode_texts), \
            f"{key}: a ';' comment reached exported g-code"
        summaries[key] = res.summary
    # ...and no two tabs describe the same print
    assert len(set(summaries.values())) == len(summaries), \
        "two tabs build the same thing: %r" % (summaries,)
    app._cal_show_tab("cts")






def the_sag_slide_stacks_by_deposition_order():
    """Reported: with 3-D tubes on, only the first layer of the sag print
    renders. Every layer was there, on the plate: the picture drew the text as
    shown, whose layer Z is dropped and whose travels are spelled G1, so 157
    fibers came out as one flat one. Comparator: the engine's own wall height
    and move count (`generate_sag`), which this page's code did not write.

    Z is out of the picture's text whichever way the Machine page's box sits:
    the ladder's Z steps under the solver's own stacking stand the wall 0.34 mm
    tall, and 0.23 mm is 20 fibers.

    RED PROOF (observed): without `_gce_pv_source` in `gui_gcode_calibrate`, the
    wait below ends at `sag picture top 0.0, the fibre at 0.2274`."""
    import mew_sweeps
    app = _M["app"]
    app._nav_select("calibration")
    app._cal_show_tab("sag")
    state = app._gce_pv_state

    def top():
        meta = state.get("meta") or {}
        return float(meta["bbox"][1][2]) if meta else 0.0

    res = None

    def sag_up():
        nonlocal res
        res = app._sw_result
        return res is not None and res.kind == "sag" and top() > 0.1
    _pump(sag_up, timeout=30.0)
    assert res is not None and res.kind == "sag", "the sag print never built"
    row = res.rows[0]
    want = row["fibre_z_mm"]
    # 3 fibres, not 2: the picture lays the travels as Design does, and the
    # ladder's approach runs along the wall top (0.333 -> 0.366 mm measured)
    assert abs(top() - want) < 3.0 * row["fibre_diam_mm"], \
        "sag picture top %r, the fibre at %r" % (top(), want)
    moves = mew_sweeps.extract_moves(res.gcode_texts[0])[0]
    assert state["meta"]["n_paths"] == len(moves), \
        "%d fibers drawn for the engine's %d: a travel drew as fiber" % (
            state["meta"]["n_paths"], len(moves))
    was = app.no_layer_z.get()
    try:
        texts = set()
        for on in (True, False):
            app.no_layer_z.set(on)
            texts.add(app._gce_preview_text())
    finally:
        app.no_layer_z.set(was)
    assert len(texts) == 1, "the picture's stack follows the Z box"
    assert sum(" Z" in ln for ln in texts.pop().split("\n")) == 1, \
        "more than one Z word in the picture's text"
    app._cal_show_tab("cts")


def the_sag_card_recovers_a_planted_power_law():
    """A ladder whose droops are c * span^p recovers BOTH c and p through the
    widgets, and Apply writes both calibration fields.

    This is what the ladder print exists for: the card it replaces took one span
    and one droop, which can only invert the magnitude at whatever exponent the
    rig already held.

    RED PROOFS (observed): fitting ln(depth) against span instead of ln(span)
    fails at `AssertionError('p = 1.918, planted 1.8')`; having Apply write only
    the magnitude fails at `AssertionError('cal.sag_span_exp unchanged')`."""
    app = _M["app"]
    app._cal_show_tab("sag")
    _clear_ladder_fits(app)
    keep_exp = app.cal_vars["cal.sag_span_exp"].get()
    try:
        app.cal_vars["cal.sag_span_exp"].set("2.0")
        _fill_sag_ladder(coeff=0.35, exp=1.8, readings=3)
        app._compute_fit_sag()
        fit = app._fit_sag_result
        assert fit is not None and fit.exp_fitted, "the exponent was refused"
        assert abs(fit.span_exp - 1.8) < 1e-6, \
            f"p = {fit.span_exp:.4g}, planted 1.8"
        assert abs(fit.coeff - 0.35) < 1e-6, \
            f"c = {fit.coeff:.4g}, planted 0.35"
        assert fit.r2 is not None and fit.r2 > 0.999, f"R2 = {fit.r2}"
        app._apply_fit_sag()
        got_c = float(app.cal_vars["cal.sag_coeff"].get())
        got_p = float(app.cal_vars["cal.sag_span_exp"].get())
        assert abs(got_c - 0.35) < 1e-4, f"cal.sag_coeff = {got_c}"
        assert abs(got_p - 1.8) < 1e-4, "cal.sag_span_exp unchanged"

        # the refusal rule: two spans cannot carry an exponent, so the rig's own
        # is kept and only the magnitude is fitted
        app.cal_vars["cal.sag_span_exp"].set("2.0")
        _clear_ladder_fits(app)
        _fill_sag_ladder(spans=(1.0, 1.4), coeff=0.35, exp=1.8)
        app._compute_fit_sag()
        fit = app._fit_sag_result
        assert fit is not None and not fit.exp_fitted, \
            "two spans must not fit an exponent"
        assert abs(fit.span_exp - 2.0) < 1e-9, \
            f"the refused fit must keep the page's p, got {fit.span_exp}"
        before = app.cal_vars["cal.sag_span_exp"].get()
        app._apply_fit_sag()
        assert app.cal_vars["cal.sag_span_exp"].get() == before, \
            "a refused exponent must not be written back"
        assert app._fit_sag_out.cget("text").strip(), \
            "the refusal must say something"
    finally:
        app.cal_vars["cal.sag_span_exp"].set(keep_exp)
        _clear_ladder_fits(app)




def instructions_render_the_three_groups():
    """Each tab has ONE "Instructions" disclosure holding every step of its
    procedure, under the registry's three headings, numbered 1..N across the
    whole card.

    The three groups answer the three questions an operator asks: what to do in
    the software to get the g-code, how to get the measurement, and what to do
    with it. They come from `calibration_procedures`, which is the one place the
    prose lives -- this case checks the PAGE renders all of it, so a step that
    exists in the registry but never reaches a label is a failure.

    RED proof: rendering `proc.groups[:2]` instead of all three fails with
    `cts: 9 step labels on the card, the registry has 12`; renaming the
    disclosure back to "Procedure" fails at `no Instructions disclosure`;
    drawing a different title (`red_proofs_cards.py`) fails at `cts: the tab's
    title is not on the card`."""
    import calibration_procedures
    import gui_widgets
    app, root = _M["app"], _M["root"]

    def labels(w, out):
        for c in w.winfo_children():
            try:
                out.append(str(c.cget("text")))
            except tk.TclError:
                pass
            labels(c, out)
        return out

    for key, proc in calibration_procedures.PROCEDURES.items():
        app._cal_show_tab(key)
        root.update()
        texts = labels(app._cal_cards[key]["instructions"], [])
        # NB: never put raw widget text in a failure message here. This runner's
        # stdout is cp1252 on Windows, so dumping it turns a legible FAIL into a
        # UnicodeEncodeError traceback -- which is exactly what happened while
        # proving this case red.
        assert app._cal_grid.card("instructions")._title_text == "Instructions", \
            f"{key}: no Instructions card"
        # a card's section heading (gui_form's Section.TLabel), drawn as typed
        assert proc.title in texts, \
            f"{key}: the tab's title is not on the card"
        # every heading, and every step, numbered 1..N across the whole card
        for heading, _lines in proc.groups:
            assert heading in texts, f"{key}: group heading {heading!r} missing"
        rendered = [t for t in texts if t[:1].isdigit() and ".  " in t[:5]]
        assert len(rendered) == len(proc.steps), \
            (f"{key}: {len(rendered)} step labels on the card, the registry "
             f"has {len(proc.steps)}")
        for i, step in enumerate(proc.steps, 1):
            # the card shows the step in the display units (gui_units)
            import gui_units
            assert f"{i}.  {gui_units.localize(step)}" in texts, \
                f"{key}: step {i} is not rendered verbatim"
    app._cal_show_tab("cts")






















def _spelling(app, g1, conv, nlz):
    """Set the printer's three spelling options (Setup's, which Calibrate's
    rows show too); return the old values."""
    g1_var, conv_var = app._sw_spelling_vars()
    old = (g1_var.get(), conv_var.get(), app.no_layer_z.get())
    g1_var.set(g1)
    conv_var.set(conv)
    app.no_layer_z.set(nlz)
    return old


def _no_machine_frame(app):
    """Clear Setup's start and end blocks and the heat wait for a case about
    the slide's own bytes, and return what puts them back. Every saved
    slide carries the printer's frame (`_sw_user_gcode`), which would make
    a byte check test the header instead of the slide."""
    wait = getattr(app, "s_wait_for_temp", None)
    was = (app.machine_gcode_start.get(), app.machine_gcode_end.get(),
           wait.get() if wait is not None else False)
    app.machine_gcode_start.set("")
    app.machine_gcode_end.set("")
    if wait is not None:
        wait.set(False)

    def restore():
        app.machine_gcode_start.set(was[0])
        app.machine_gcode_end.set(was[1])
        if wait is not None:
            wait.set(was[2])
    return restore


def exported_bytes_are_engine_bytes():
    """Save g-code as... writes exactly what the engine returned, per slide,
    plus the measurement CSV when asked -- with every spelling option of the
    Export card OFF. With them on, `export_options_spell_the_saved_gcode`
    holds the bytes to the designer's rewrite instead."""
    app = _M["app"]
    assert _pump(lambda: app._sw_result is not None)
    res = app._sw_result
    folder = tempfile.mkdtemp()
    old = _spelling(app, False, "GX", False)
    restore = _no_machine_frame(app)
    try:
        written = app._sw_export_gcode(folder, "cal", True)
    finally:
        restore()
        _spelling(app, *old)
    assert len(written) == len(res.gcode_texts) + 1, written
    # newline="" on the way back too: the measurement CSV carries CRLF, and
    # universal-newline translation would silently "fix" a real byte difference.
    # one print keeps the name typed; several are numbered (cal_print<i>)
    for i, text in enumerate(res.gcode_texts, 1):
        with open(os.path.join(folder, written[i - 1]), newline="") as fh:
            assert fh.read() == text, f"slide {i} bytes differ from the engine"
    with open(os.path.join(folder, "cal_measure.csv"), newline="") as fh:
        assert fh.read() == res.csv_text, "the CSV differs from the engine's"
    # ...and the hard rule: no ';' comments in exported g-code
    for text in res.gcode_texts:
        assert ";" not in text, "exported g-code must carry no ';' comments"


def export_options_spell_the_saved_gcode():
    """The Export card's spelling options reach "Save g-code as…": the saved
    slide is the engine's text through the designer's own rewrite
    (`_rewrite_gcode_lines`) with THIS page's options, and the scratch file
    the converter reads stays the engine's.

    RED proof: before the card carried them, Save g-code wrote the engine
    bytes whatever was ticked, which fails the first assertion below (the
    default ticks travel-as-G1, so a saved slide still held `G0 `)."""
    app = _M["app"]
    app._cal_show_tab("cts")
    assert _pump(lambda: app._sw_result is not None
                 and app._sw_result.kind == "cts"
                 and not app._sw_state.get("dirty"), timeout=30)
    res = app._sw_result
    folder = tempfile.mkdtemp()
    old = _spelling(app, True, "G0X", True)
    restore = _no_machine_frame(app)
    try:
        first = app._sw_export_gcode(folder, "cal", False)[0]
        with open(os.path.join(folder, first), newline="") as fh:
            saved = fh.read()
        assert "\nG0 " not in saved and "\nG00 " not in saved, \
            "a travel survived travel-as-G1"
        assert "\nG01 " in saved, "G0X did not pad the motion commands"
        zs = [l for l in saved.splitlines() if " Z" in l]
        assert len(zs) == 1, f"{len(zs)} Z words with the machine's per-layer drop on"
        want = app._rewrite_gcode_lines(res.gcode_texts[0], True, True,
                                        drop_layer_z=True, boundaries=())
        assert saved == want
        # the printer's spelling is one setting: Setup's box reaches a
        # calibration file as it reaches a design's
        app.s_travel_as_g1.set(False)
        first = app._sw_export_gcode(folder, "cal", False)[0]
        with open(os.path.join(folder, first), newline="") as fh:
            assert fh.read() != want, "Setup's box did not reach a slide"
    finally:
        restore()
        app.s_travel_as_g1.set(True)
        _spelling(app, *old)
    shutil.rmtree(folder, ignore_errors=True)






def a_slide_is_edited_like_a_design():
    """Calibrate carries Design's Add and Change selection tools, and an edit
    reaches the text, the picture and Save g-code, and survives a visit to
    Design. The ribbon has no Designer group.

    RED proof: against the commit before this change the edit tools are
    unmapped on Calibrate, and the Speed tool is never offered a selection."""
    app, root = _M["app"], _M["root"]
    done = lambda: [w for w in app._gce_walk(app._gce_form)
                    if isinstance(w, tk.ttk.Button) and w["text"] == "Done"][0]
    app._nav_select("calibration")
    assert _pump(lambda: app._cal_stash is not None)
    _pump(lambda: False, 1.5)
    assert not hasattr(app, "_gce_designer_link"), "the Designer group is gone"
    tools = app._gce_tools
    assert all(tools[k].winfo_manager() for k in
               ("insert", "speed", "translate", "move", "delete")), \
        "the Add and Change tools show on Calibrate"
    assert app._jet_btn.winfo_manager() and "flat-plate" not in (
        app._jet_why() or ""), \
        "Play here is offered on a flat calibration slide"
    before = app.gce.get_text()
    node = [i for i, inf in app._gce_tree_info.items()
            if inf.get("kind") == "file"][0]
    app._gce_select_node(node)
    _pump(lambda: False, 0.4)
    assert app._gce_tool_why("speed_set") is None
    app._gce_tool_change("speed_set")
    _pump(lambda: False, 0.4)
    app._gce_fs["vals"]["f"]["t:speed_set"]["mm_s"].set("15")
    app._gce_edit_flush()
    done().invoke()
    _pump(lambda: False, 0.6)
    after = app.gce.get_text()
    assert after != before and "F900" in after and "F900" not in before, \
        "the edit reaches the slide's text"
    folder = tempfile.mkdtemp()
    written = app._sw_export_gcode(folder, "cal", False)
    with open(os.path.join(folder, written[0]), newline="") as fh:
        saved = fh.read()
    assert saved.replace("\r\n", "\n") == after.replace("\r\n", "\n"), \
        "Save g-code writes the edited slide"
    app._nav_select("gcode")
    root.update()
    app._nav_select("calibration")
    assert _pump(lambda: app._cal_stash is not None)
    _pump(lambda: False, 1.0)
    assert "F900" in app.gce.get_text(), "the edit survives a visit to Design"

    def left():
        # what the picture's worker draws: travels must stay travels
        import gcode_to_fibers
        out = app._gce_pv_source()()
        text = out[0] if isinstance(out, tuple) else out
        path = os.path.join(tempfile.mkdtemp(), "pv.gcode")
        with open(path, "w") as fh:
            fh.write(text)
        paths = gcode_to_fibers.parse_gcode(path, mode="g1")
        paths = paths[0] if isinstance(paths, tuple) else paths
        return float(min(np.min(np.asarray(p)[:, 0]) for p in paths))
    x0 = left()
    app._gce_select_node(node)
    _pump(lambda: False, 0.4)
    app._gce_tool_change("offset")
    _pump(lambda: False, 0.4)
    app._gce_fs["vals"]["f"]["t:offset"]["dx"].set("7")
    app._gce_edit_flush()
    done().invoke()
    assert _pump(lambda: abs(left() - x0 - 7.0) < 1e-6, timeout=15.0), \
        "the picture follows the edit (%s -> %s)" % (x0, left())
    app._nav_select("gcode")
    root.update()


def calibration_preset_kind_roundtrips():
    """The Calibration page's print-design presets (kind "sweep", labelled
    "Calibration presets") stay a REAL shared preset kind, disjoint from the
    scaffold / machine / physics-profile kinds."""
    app = _M["app"]
    sweep = set(app._sweep_persist_keys())
    calib = set(app._calibration_persist_keys())
    machine = set(app._machine_persist_keys())
    assert sweep and not (sweep & calib) and not (sweep & machine), \
        "the preset kinds must stay disjoint"
    assert all(k.startswith("sw.") for k in sweep)
    snap = {k: app._persist_map()[k].get() for k in sweep}
    app.sw_vars["circ_eps"].set("2 4")
    app._apply_preset_for_kind("sweep", snap)
    assert app.sw_vars["circ_eps"].get() == snap["sw.circ_eps"], \
        "a calibration preset did not restore its own field"


def generate_sets_last_gcode():
    """Generate builds the target slide and feeds the SHARED result flow, so
    Save STL / the sidebar viewers light up -- like a scaffold Generate."""
    app = _M["app"]
    app.sw_vars["kind"].set("cts")
    app.sw_vars["cts_start"].set("600")
    app.sw_vars["cts_stop"].set("540")
    app.sw_vars["cts_step"].set("60")
    app.sw_vars["cts_reps"].set("1")
    assert _pump(lambda: (app._sw_result is not None
                          and not app._sw_state.get("dirty")))
    launched = {}

    def fake_launch(args, gco, out):
        launched["args"], launched["gco"], launched["out"] = args, gco, out
        app._sw_build_busy = False

    real = app._sw_launch_build
    app._sw_launch_build = fake_launch
    try:
        app._sw_generate()
    finally:
        app._sw_launch_build = real
    assert launched, "Generate never launched a build"
    assert os.path.isfile(launched["gco"]), "the slide g-code was not staged"
    with open(launched["gco"]) as fh:
        assert fh.read() == app._sw_result.gcode_texts[0]
    app._sw_generate_done(True, launched["gco"], launched["out"])
    assert app.last_gcode == launched["gco"]








def a_mandrel_print_plays_on_the_4axis_machine():
    """A mandrel calibration print (the lag tube, the 4-axis CTS ladder) plays
    on the DEFAULT 4-AXIS MACHINE preset, whatever the Machine page holds; a
    flat print plays on the Machine page; and neither moves a Machine field.

    Reported: generating the lag tube loaded the default 3-axis printer. The
    page boots 3-axis and only the Collector toggle swaps the machine, which
    the Calibration page never touches.

    The tube's ROD is that machine's too, fixed while the ladder grows.

    RED proof (observed 2026-09-25): `_player_args` calling `_machine_args`
    directly again fails at `the tube played on the Machine page's bed`;
    placing on the live Machine page fails at `the tube's rod is not the
    4-axis default's`."""
    app = _M["app"]
    st = app._pstores["machine"]
    keys = app._keys_for_kind("machine")
    snap = lambda: {k: w.get() for k, w in app._persist_map().items()
                    if k in keys}
    page = snap()
    rig = dict(page, **{"machine.show": True, "machine.x_stl": "head4.stl",
                        "machine.z_stl": "mount4.stl", "rotary.on": True,
                        "rotary.rod_length": "40", "rotary.rod_x": "0"})
    saved_defaults = dict(st["defaults"])
    st["presets"].append({"name": "Rig 4", "data": rig})
    st["defaults"]["mandrel"] = "Rig 4"
    app.machine_show.set(True)
    app.machine_x_stl.set("head3.stl")
    app.machine_z_stl.set("bed3.stl")
    page = snap()

    def played(tab):
        app._cal_show_tab(tab)
        launched = {}
        real = app._sw_launch_build
        app._sw_launch_build = lambda a, g, o: launched.update(gco=g, out=o)
        try:
            app._sw_generate()
        finally:
            app._sw_launch_build = real
            app._sw_build_busy = False
        app._sw_generate_done(True, launched["gco"], launched["out"])
        args = app._player_args(static=True)
        with open(args[args.index("--machine") + 1]) as fh:
            return json.load(fh)

    try:
        prof = played("lag4")
        assert prof["parts"]["bed"] == "mount4.stl", \
            "the tube played on the Machine page's bed: %r" % prof["parts"]
        assert prof.get("rotary"), "the tube's printer has no rotary axis"
        assert snap() == page, "playing the tube moved a Machine field"
        rod = app._sw_plate["rod"]
        assert abs((rod[1] - rod[0]) - 40.0) < 1e-6,             "the tube's rod is not the 4-axis default's 40 mm: %r" % (rod,)
        prof = played("cts4")
        assert prof["parts"]["bed"] == "mount4.stl", prof["parts"]
        # the rod stays the machine's whatever the ladder, and the ladder
        # shrinks onto it (reported: the rod grew 26 -> 182 mm with the range)
        for start, stop in (("600", "540"), ("900", "120")):
            app.sw_vars["cts4_start"].set(start)
            app.sw_vars["cts4_stop"].set(stop)
            app._sw_ensure_fresh()
            rod = app._sw_plate["rod"]
            bbox = app._sw_result.prints[0].bbox
            assert abs((rod[1] - rod[0]) - 40.0) < 1e-6, (start, stop, rod)
            assert rod[0] - 1e-6 <= bbox[0] and bbox[2] <= rod[1] + 1e-6,                 (start, stop, bbox, rod)
        assert snap() == page, "placing the ladder moved a Machine field"
        # the shrink is said under the rod rows of its own card, never in the
        # page's red strip (reported: "i hate the red text at the bottom")
        app._sw_render_warnings(app._sw_result)
        cards = app._sw_rod_notes
        assert any("shrunk to fit" in v.get() for v in cards["cts4"]), \
            [v.get() for v in cards["cts4"]]
        assert not any(v.get() for v in cards.get("tube", [])), \
            "the ladder's note landed on the tube card"
        assert "shrunk" not in app._sw_warn_var.get(), app._sw_warn_var.get()
        prof = played("cts")
        assert prof["parts"]["bed"] == "bed3.stl", \
            "a flat print left the Machine page: %r" % prof["parts"]
        assert not prof.get("rotary")
        assert snap() == page
    finally:
        st["presets"].pop()
        st["defaults"].clear()
        st["defaults"].update(saved_defaults)
        app.sw_vars["cts4_start"].set("600")
        app.sw_vars["cts4_stop"].set("240")
        app._cal_show_tab("cts")
















class _StubProc:
    """A Popen stand-in whose `stdout` is an iterator the reader thread walks.

    `_sw_launch_build` reads the pipe line by line now (it used to
    `communicate()` and discard), so the stub has to BE a pipe. `block` keeps the
    iterator hanging until `terminate()` is called, which is how the cancel case
    holds a build open."""

    def __init__(self, lines=(), block=False):
        self._lines = list(lines)
        self._block = block
        self.killed = False
        self.stdout = self

    def __iter__(self):
        return self

    def __next__(self):
        if self._lines:
            return self._lines.pop(0)
        while self._block and not self.killed:
            time.sleep(0.01)
        raise StopIteration

    def wait(self):
        return 1 if self.killed else 0

    def terminate(self):
        self.killed = True

    def poll(self):
        return None












def teardown():
    root = _M.get("root")
    if root is not None:
        try:
            root.destroy()
        except tk.TclError:
            pass
    scratch = _M.pop("preset_scratch", None)
    if scratch:
        import shutil
        shutil.rmtree(scratch, ignore_errors=True)


def a_fit_never_applies_garbage_or_a_stale_value():
    """What a stranger types into the fit cards never reaches a calibration
    field, and Apply always writes what is on screen NOW.

    Found by a release-audit campaign, all on the
    shipped page:
      * "nan" / "inf" typed into any card went straight into the Physics page
        field (CTS "nan", viscosity anchor "nan", Ea "nan", sag c "nan",
        lag A "nan"), and the workers then ran on NaN;
      * the CTS / viscosity / Ea cards kept their last good result, so Check 8,
        type 10, Apply wrote 8 -- and so did Apply after a red Check;
      * the viscosity and Ea cards reported their errors in GREEN, the
        viscosity one as a bare "check the inputs";
      * pressure 0 wrote a viscosity anchor of 0, which then broke every later
        fit with a division by zero, and a temperature at absolute zero raised
        out of the Ea card instead of saying so;
      * an Ea fitted from rows where the fibre got THINNER as the melt got
        hotter came out negative and was applied.
    RED proof: every assertion below failed on cc329c7 (the campaign's output
    is the record); each is the operator-visible symptom, not an internal."""
    import mew_calibration as mc
    app = _M["app"]
    defaults = mc.default_profile()
    saved = {k: app.cal_vars[k].get() for k in defaults if k in app.cal_vars}
    RED, GREEN = "#a33", "#1f6b2e"
    bad = []            # every symptom, not just the first -- the RED run lists them

    def expect(ok, msg):
        if not ok:
            bad.append(msg)

    def fg(lbl):
        return str(lbl.cget("foreground"))

    def refused(key, apply_, out, why):
        app.cal_vars[key].set("SENTINEL")
        try:
            apply_()
        except Exception as exc:  # noqa: BLE001 -- Tk would swallow it
            expect(False, f"{why}: Apply raised {exc!r}")
        got = app.cal_vars[key].get()
        expect(got == "SENTINEL", f"{why}: Apply wrote {key} = {got!r}")
        expect(fg(out) == RED, f"{why}: error shown in {fg(out)}, not red "
                               f"({out.cget('text')!r})")

    def visc(p, t, v, d):
        app._fit_visc_press.set(p)
        app._fit_visc_temp.set(t)
        app._fit_visc_speed.set(v)
        for i, var in enumerate(app._fit_visc_diams):
            var.set(d if i == 0 else "")

    def ea(rows):
        for i, (tv, _d) in enumerate(app._fit_ea_rows):
            tv.set(rows[i][0] if i < len(rows) else "")
            for j, dv in enumerate(app._fit_ea_diams[i]):
                dv.set(rows[i][1] if (i < len(rows) and j == 0) else "")

    def reset():
        for k in saved:
            app.cal_vars[k].set(defaults[k])
        app._fit_results.clear()

    try:
        reset()
        # -- non-finite numbers, card by card
        for v in ("nan", "inf"):
            reset()
            app._fit_cts_val.set(v)
            refused("cal.cts_ref", app._apply_fit_cts, app._fit_cts_out,
                    "CTS %s" % v)
        for args in (("nan", "100", "10", "0.02"), ("3", "nan", "10", "0.02"),
                     ("3", "100", "nan", "0.02"), ("3", "100", "inf", "0.02"),
                     ("0", "100", "10", "0.02"), ("-3", "100", "10", "0.02"),
                     ("3", "-300", "10", "0.02"), ("3", "100", "0", "0.02")):
            reset()
            visc(*args)
            refused("cal.mat_viscosity_ref", app._apply_fit_visc,
                    app._fit_visc_out, "viscosity %s" % (args,))
        expect("check the inputs" != app._fit_visc_out.cget("text"),
               "the viscosity card must say WHAT is wrong")
        for rows in ((("nan", "0.03"), ("110", "0.02")),
                     (("90", "inf"), ("110", "0.02")),
                     (("-273.15", "0.03"), ("110", "0.02")),
                     (("-400", "0.03"), ("110", "0.02")),
                     (("90", "0.03"), ("110", "0.02")),      # negative Ea
                     (("90", "0.03"),)):
            reset()
            ea(rows)
            refused("cal.mat_activation_energy", app._apply_fit_ea,
                    app._fit_ea_out, "Ea %s" % (rows,))
        reset()
        _fill_sag_ladder()
        app._fit_sag_rows[0][0].set("nan")
        app._fit_sag_rows[1][0].set("inf")
        app._compute_fit_sag()
        coeff = app._fit_sag_out.cget("text")
        expect("nan" not in coeff.lower(), f"sag fitted through NaN: {coeff!r}")
        reset()
        app._fit_fus_diam.set("nan")
        app._fit_fus["junction"]["rows"][0][0].set("5")
        app._fit_fus["junction"]["rows"][0][1].set("0.06")
        refused("cal.fusion_overlap", app._apply_fit_fusion_junction,
                app._fit_fus_out, "fusion d = nan")
        app._fit_fus_diam.set("0.02")
        app._fit_fus["junction"]["rows"][0][0].set("2.7")
        refused("cal.fusion_overlap", app._apply_fit_fusion_junction,
                app._fit_fus_out, "fusion N = 2.7 layers")
        app._fit_fus["junction"]["rows"][0][0].set("5")
        app._fit_fus["junction"]["rows"][0][1].set("nan")
        refused("cal.fusion_overlap", app._apply_fit_fusion_junction,
                app._fit_fus_out, "fusion h = nan")
        reset()
        for q in app._fit_circ_rows:
            for var in q:
                var.set("")
        for var, val in zip(app._fit_circ_rows[0], ("a", "nan", "2", "1.9")):
            var.set(val)
        app._fit_circ_gap.set("4")
        refused("cal.lag_amplitude",
                lambda: (app._compute_fit_circles(), app._apply_fit_circles()),
                app._fit_circ_out, "lag circle eps = nan")
        reset()
        for trio, vals in zip(app._fit_lag_rows, (("30", "nan", "0.4"),
                                                  ("45", "0.62", "0.38"),
                                                  ("60", "0.65", "0.35"))):
            for var, val in zip(trio, vals):
                var.set(val)
        app._compute_fit_mandrel_lag()
        expect("NoneType" not in app._fit_lag_out.cget("text"),
               "the mandrel card must name the bad entry, not a Python error: "
               + app._fit_lag_out.cget("text"))

        # -- Apply writes what is on screen now, never a remembered result
        reset()
        app._fit_cts_val.set("480")
        app._compute_fit_cts()
        app._fit_cts_val.set("600")
        app._apply_fit_cts()
        expect(app.cal_vars["cal.cts_ref"].get() == "10",
               "CTS Apply wrote %r, not the typed 600 mm/min as 10 mm/s"
               % app.cal_vars["cal.cts_ref"].get())
        app._fit_cts_val.set("-1")
        refused("cal.cts_ref", app._apply_fit_cts, app._fit_cts_out,
                "CTS Apply after a red Check")
        reset()
        visc("3", "100", "10", "0.02")
        app._compute_fit_visc()
        first = app._fit_visc_out.cget("text")
        expect(fg(app._fit_visc_out) == GREEN, first)
        visc("3", "100", "10", "0.04")
        app._apply_fit_visc()
        v02 = mc.fit_viscosity_ref(app._calibration_model(), 0.02,
                                   3 * 6894.757, 100.0, 10.0)
        got = float(app.cal_vars["cal.mat_viscosity_ref"].get())
        expect(abs(got - v02) > 1e-3 * v02,
               "viscosity Apply wrote the result of the diameter no longer typed")
        reset()
        _fill_sag_ladder(coeff=0.35)
        app._compute_fit_sag()
        _fill_sag_ladder(coeff=0.70)
        app._apply_fit_sag()
        c = float(app.cal_vars["cal.sag_coeff"].get())
        expect(abs(c - 0.70) < 1e-3, f"sag Apply wrote a stale c = {c}")
        assert not bad, "\n    " + "\n    ".join(bad)
    finally:
        for k, v in saved.items():
            app.cal_vars[k].set(v)




def a_nan_field_never_raises_out_of_a_readout():
    """Typing "nan" into a field the page's live readouts read raises nothing
    out of a Tk callback. Reported by the GUI slice of the 2026-09-24
    release audit (`gen_garbage.py`): `sim.standoff` or
    `sw.circ_amp_prior` = nan raised mew_circle_fit's "circle radius and gap
    must both be > 0 mm" out of the lag-circle Conditions line, and
    `sw.sag_layers` = nan raised "cannot convert float NaN to integer" out of
    the sag readout. Tk swallows both to the console, and the readout they
    were drawing is left stale. RED on cc329c7: both tracebacks, captured
    here through `report_callback_exception`."""
    app, root = _M["app"], _M["root"]
    caught = []
    old = root.report_callback_exception
    root.report_callback_exception = \
        lambda e, v, tb: caught.append("%s: %s" % (e.__name__, v))
    saved = {("sim", "standoff"): app.sim_standoff.get()}
    for k in ("circ_amp_prior", "sag_layers"):
        saved[("sw", k)] = app.sw_vars[k].get()
    try:
        for key, var in ((("sim", "standoff"), app.sim_standoff),
                         (("sw", "circ_amp_prior"), app.sw_vars["circ_amp_prior"]),
                         (("sw", "sag_layers"), app.sw_vars["sag_layers"])):
            var.set("nan")
            _pump(lambda: False, timeout=1.2)     # let every debounce fire
            var.set(saved[key])
            _pump(lambda: False, timeout=0.3)
        # the readouts also run directly on a tab switch
        for key in ("lag3", "sag"):
            app._cal_show_tab(key)
            root.update()
    finally:
        root.report_callback_exception = old
    assert not caught, "; ".join(caught)


def the_fusion_card_fits_junction_and_wall_separately():
    """The Fusion card fits the operator's own scaffold: a fiber diameter, then
    junction rows and optional wall rows of (layers, height). Each table gives
    its own phi, and each phi has its own Apply button that writes it and
    nothing else.

    Comparator: heights planted from the height law at known phis, read back
    through the widgets and then through `models_from_profile` -- the model the
    workers actually build, not just the Tk var.

    RED proofs (measured): with the gate tick removed from `_apply_fit_fusion`
    this fails at `wall phi never reached the model` (the unticked field reads
    as blank); with `_apply_fit_fusion` writing the junction phi whichever
    button called it, it fails at `Apply wall overlap left the wall phi
    unset`."""
    import mew_calibration as mc
    app, root = _M["app"], _M["root"]
    fs = next(f for f in mc.CALIBRATION_FIELDS
              if f.attr == "fusion_wall_overlap")
    keys = ("cal.fusion_overlap", fs.key, fs.gate_key)
    saved = {k: app.cal_vars[k].get() for k in keys}
    d = 0.02

    def h(n, phi):
        return "%.9g" % (d * (1.0 + (n - 1) * (1.0 - phi)))

    def press(text):
        """Invoke the fit card's button by its on-screen label."""
        found = []

        def walk(w):
            for c in w.winfo_children():
                if c.winfo_class() == "TButton" and \
                        str(c.cget("text")) == text:
                    found.append(c)
                walk(c)
        walk(app._cal_cards["fusion"]["fit"])
        assert len(found) == 1, f"{len(found)} {text!r} buttons on the card"
        found[0].invoke()
        root.update()

    try:
        # the table starts with two rows; add/remove works and keeps one row
        J, W = app._fit_fus["junction"], app._fit_fus["wall"]
        for which, g in (("junction", J), ("wall", W)):
            while len(g["rows"]) > 1:
                app._fit_fus_remove_row(which, g["rows"][-1])
            for var in g["rows"][0]:
                var.set("")
        app._fit_fus_add_row("junction")
        assert len(J["rows"]) == 2
        W["rows"][0][0].set("3")
        app._fit_fus_remove_row("wall", W["rows"][0])
        assert len(W["rows"]) == 1 and W["rows"][0][0].get() == "", \
            "removing the last wall row must clear it, not delete it"

        app.cal_vars["cal.fusion_overlap"].set("0.5")
        app.cal_vars[fs.gate_key].set(False)
        app.cal_vars[fs.key].set("")
        app._fit_fus_diam.set(str(d))
        J["rows"][0][0].set("5"), J["rows"][0][1].set(h(5, 0.35))
        J["rows"][1][0].set("9"), J["rows"][1][1].set(h(9, 0.35))
        W["rows"][0][0].set("4"), W["rows"][0][1].set(h(4, 0.2))
        app._fit_fus_add_row("wall", ("7", h(7, 0.2)))
        press("Compute")
        text = app._fit_fus_out.cget("text")
        assert "Junction" in text and "Wall" in text, text

        # the junction button writes the junction phi and leaves the wall alone
        press("Apply junction sink")
        got = float(app.cal_vars["cal.fusion_overlap"].get())
        assert abs(got - 0.35) < 1e-6, f"junction phi {got} != 0.35"
        assert not app.cal_vars[fs.gate_key].get(), \
            "Apply junction overlap ticked the straight-span switch"
        assert app.cal_vars[fs.key].get() == "", \
            "Apply junction overlap wrote the wall phi"

        # the wall button writes the wall phi, ticked, and leaves the junction
        app.cal_vars["cal.fusion_overlap"].set("0.5")
        press("Apply straight-span sink")
        assert app.cal_vars[fs.key].get() != "", \
            "Apply wall overlap left the wall phi unset"
        assert app.cal_vars["cal.fusion_overlap"].get() == "0.5", \
            "Apply wall overlap moved the junction phi"
        _m, _n, cal = mc.models_from_profile(
            app._calibration_profile_snapshot())
        assert cal.fusion_wall_overlap is not None, \
            "wall phi never reached the model"
        assert abs(cal.fusion_wall_overlap - 0.2) < 1e-6, \
            f"wall phi {cal.fusion_wall_overlap} != 0.2"

        # no wall readings: the wall button says so and writes nothing
        app.cal_vars[fs.key].set("")
        for pair in list(W["rows"]):
            app._fit_fus_remove_row("wall", pair)
        press("Compute")
        press("Apply straight-span sink")
        assert app.cal_vars[fs.key].get() == "", \
            "an empty wall table wrote a wall phi"
        assert "No wall readings" in app._fit_fus_out.cget("text")
    finally:
        for k, v in saved.items():
            app.cal_vars[k].set(v)
        app._sync_cal_gates()




def main():
    build()
    for name, fn in (
        # first: a regression here hangs every case after it, so it has to
        # report before any of them run
        ("page_mounts", page_mounts),
        ("every_card_mounts_and_applies", every_card_mounts_and_applies),
        ("the_fusion_card_fits_junction_and_wall_separately",
         the_fusion_card_fits_junction_and_wall_separately),
        ("a_fit_never_applies_garbage_or_a_stale_value",
         a_fit_never_applies_garbage_or_a_stale_value),
        ("a_nan_field_never_raises_out_of_a_readout",
         a_nan_field_never_raises_out_of_a_readout),
        ("apply_reaches_the_physics_page", apply_reaches_the_physics_page),
        ("lag_circle_card_recovers_a_planted_amplitude",
         lag_circle_card_recovers_a_planted_amplitude),
        ("every_protocol_tab_actually_generates",
         every_protocol_tab_actually_generates),
        ("the_sag_slide_stacks_by_deposition_order",
         the_sag_slide_stacks_by_deposition_order),
        ("the_sag_card_recovers_a_planted_power_law",
         the_sag_card_recovers_a_planted_power_law),
        ("exported_bytes_are_engine_bytes", exported_bytes_are_engine_bytes),
        ("a_slide_is_edited_like_a_design", a_slide_is_edited_like_a_design),
        ("calibration_preset_kind_roundtrips", calibration_preset_kind_roundtrips),
        ("generate_sets_last_gcode", generate_sets_last_gcode),
        ("a_mandrel_print_plays_on_the_4axis_machine",
         a_mandrel_print_plays_on_the_4axis_machine),
        ("travels_follow_the_tubes_box", travels_follow_the_tubes_box),
    ):
        check(name, fn)
    teardown()
    print(f"\n{len(RAN) - len(FAILURES)}/{len(RAN)} passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
