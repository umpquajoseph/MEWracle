#!/usr/bin/env python3
"""test_ui_shell.py -- MODERN-UI ONLY: self-running smoke + parity tests for the
top-row shell (mewracle_gui.ModernApp).

Run:  python tests/test_ui_shell.py     (prints PASS/FAIL per test, exit 1 on any
failure -- same convention as the other suites). Needs a display (opens and
withdraws real Tk roots, like the gui skill's smoke snippet).

The classic developer UI is retired: nothing here builds it."""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import os
import shutil
import sys
import tkinter as tk

import gui_common
from gui_common import SAG_SOLVER_VAL2LBL

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

FAILURES = []


def _snapshot(app):
    """Snapshot every persisted var (the old _current_settings_snapshot name in
    the gui skill's smoke snippet is stale -- the presets split superseded it)."""
    return {k: w.get() for k, w in app._persist_map().items()}


def _destroy(root):
    """Destroy a test root, first cancelling pending after-callbacks (the run
    poller re-arms itself every 120 ms) so Tk doesn't gripe on teardown."""
    try:
        for aid in root.tk.call("after", "info"):
            try:
                root.tk.call("after", "cancel", aid)
            except tk.TclError:
                pass
    except tk.TclError:
        pass
    root.destroy()


RAN = []


def check(name, fn):
    RAN.append(name)
    try:
        fn()
        print(f"PASS  {name}")
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(name)
        print(f"FAIL  {name}: {exc!r}")


# ---------------------------------------------------------------------------
# modern App: one shared build for the remaining tests
# ---------------------------------------------------------------------------
_M = {}


def modern_build():
    from tkinter import font as tkfont
    import gui_fonts
    import mewracle_gui
    root = tk.Tk()
    root.withdraw()
    fam = gui_fonts.init_fonts(root)
    assert fam, "font ramp did not resolve a family"
    app = mewracle_gui.ModernApp(root)
    root.update()
    # the named ramp fonts must EXIST with their ramp sizes (guards the
    # tkinter Font.__del__ GC pitfall: an unreferenced create silently
    # un-defines the named font)
    for name, size in (("MewBody", 10), ("MewTitle", 15), ("MewSection", 11),
                       ("MewHint", 9), ("MewNav", 10), ("MewMono", 9)):
        f = tkfont.nametofont(name, root=root)   # raises if it was GC-deleted
        assert abs(int(f.actual("size"))) == size, f"{name} size wrong"
    assert tkfont.nametofont("MewTitle", root=root).actual("weight") == "bold"
    # the report never claims the requested family unless it truly renders
    rep = gui_fonts.font_report()
    assert rep.startswith("UI font: " + gui_fonts.requested_family())
    if gui_fonts.resolved_family().casefold() != \
            gui_fonts.requested_family().casefold():
        assert "substitute" in rep, "substituted font not disclosed"
    _M["root"], _M["app"] = root, app


def modern_page_sweep():
    app, root = _M["app"], _M["root"]
    assert len(app._page_order) >= 8, f"only {len(app._page_order)} pages"
    for pid, frame in app._page_order:
        app.nb.select(frame)
        root.update()
        cur = app.nb.nametowidget(app.nb.select())
        assert cur is frame or str(cur) == str(frame), f"select failed: {pid}"


def scaffold_vars_are_persisted():
    """Every per-scaffold var the generator reads must actually be SAVED.

    `persist_parity` below cannot catch a missing one: it only checks that the
    sweeps and positioning keys exist, so a control absent from `_persist_map`
    goes unnoticed. That is exactly how `link_loop_travel` shipped unsaved -- it was added
    to `_prefill_scaffold` (which seeds scaffold i from i-1) instead, which reads
    like the right list and is not. Anchor on a DIFFERENT source of truth: the
    keys `gui_args._build_scaffold_spec` feeds to the engine.
    """
    app = _M["app"]
    keys = set(app._persist_map())
    v = app.scaf_vars[0]
    # the linking-loop group, whose members are read by `_scaffold_from_spec`
    for name in ("link_loops_on", "link_loops_mode", "link_loop_angle",
                 "link_loop_dist", "link_loop_travel"):
        assert name in v, f"scaffold var {name} does not exist"
        assert f"s.{name}" in keys, \
            f"scaffold var {name} is read by the generator but never persisted"
        for i in range(2, 6):
            assert f"s{i}.{name}" in keys, \
                f"scaffold {i} var {name} is not persisted (the s2..s5 list)"


def persist_parity():
    """The modern UI saves the sweeps and positioning pages' controls. (The
    classic developer UI is retired; this no longer diffs against it.)"""
    modern_keys = set(_M["app"]._persist_map())
    assert any(k.startswith("sw.") for k in modern_keys),         "sweeps page persist keys missing"
    assert not any(k.startswith("pos.") for k in modern_keys), \
        "the Positioning page is gone: its keys must be too"








def mandrel_mode_wires_the_shared_worker():
    """The 4-axis half owns no private Generate path and no planar argv.

    Since the tab merge the collector TOGGLE is the input mode, so this also
    pins the duplicate-field deletion: layers / speed / fibre diameter / sag
    solver / facets are the scaffold designer's own vars, and the mandrel's old
    m.* copies must be gone from the persist map, not merely unused."""
    app, root = _M["app"], _M["root"]
    pf = app._page_frames
    assert "mandrel" not in pf, "the Mandrel page must not exist after the merge"
    app.nb.select(pf["scaffold"])
    app.s_collector.set("mandrel")
    root.update()
    assert app._active_mode() == "mandrel"
    pm = app._persist_map()
    for key in ("m.diameter", "m.length", "m.angle", "m.eta", "m.rm",
                "m.strategy", "m.speed_mode", "m.support", "m.lag_axial",
                "m.lag_circ", "s.collector",
                # the unit-cell families: the family token itself plus the
                # knobs `mew_unit_cell.design_tube_cells` resolves a cell from
                "m.family", "m.cell_w", "m.cell_h", "m.cell_fit",
                "m.rib_angle", "m.cell_amplitude",
                # the DRAWN family: the canonical cell drawing and the canvas
                # snap step. The edit MODE is not here on purpose -- it is a
                # click posture, not a design input, so it must not travel in
                # a preset. Neither does `m.cell_status`, which is derived.
                "m.cell_graph", "m.cell_grid",
                # the outcome-first redesign: which way the page is driven, the
                # pore it is driven TO, and the two search bounds. `m.cell_fit`
                # survives above even though the combobox is gone -- the two
                # "Use" buttons under the resolved table write it now.
                "m.design_mode", "m.pore_target", "m.length_tol",
                "m.eta_cap",
                # the two ALTERNATIVES-LADDER pins: which rung of the helix
                # trade, and which rung of the cell-count ladder, the page is
                # using. Empty is Auto (the solver's pick / the rounding), so
                # an older preset that carries neither still means what it
                # meant and needs no migration.
                "m.helix_pick", "m.cell_pick",
                # winding-angle mode's own ladder pin, and Advanced's "Set
                # eta and Rm myself" (off = the page resolves them)
                "m.angle_pick", "m.closure_override", "m.turn_pick",
                # the G-code page's tube tables: the layer step, the per-layer
                # settings that make layer blocks, and side-by-side copies
                "m.layer_step", "m.layer_overrides", "m.copies",
                "m.copy_gap"):
        assert key in pm, f"mandrel field {key} is not persisted"
    # the combobox's display mirror is NOT persisted -- `m.family` holds the
    # engine token, so re-wording a label cannot invalidate a saved preset.
    assert "m.family_label" not in pm and "_m_family_label" not in pm
    for key in ("m.cell_editmode", "m.cell_status"):
        assert key not in pm, f"{key} is a UI posture and must not persist"
    for key in ("m.layers", "m.speed", "m.fiber_diameter", "m.diam_override",
                "m.sag_mode", "m.sag_mult", "m.faces"):
        assert key not in pm, (
            f"{key} still duplicates a scaffold-designer field after the merge")
    assert "m.gap" not in pm, "mandrel must inherit Physics standoff"
    assert not hasattr(app, "m_resolved"), "obsolete Geometry and motion readout remains"
    app.s_collector.set("plate")
    root.update()


def a_legacy_preset_still_means_the_angle_it_pinned():
    """A preset that pins `m.angle` and names no design mode meant the ANGLE.

    The factory default of the new key is `pore`, so without a migration the
    loader falls through to that default and a saved preset silently changes
    meaning -- the exact failure `gui_presets`' own `s.collector` comment warns
    about. Same shape as `_migrate_legacy_slide` / `_migrate_legacy_no_sag`.

    RED CONTROL INCLUDED, in the same case: a preset that already names a mode
    must come back unchanged, so this cannot pass by writing `angle` always.
    """
    import gui_presets
    mig = gui_presets._PresetsMixin._migrate_legacy_mandrel_mode
    legacy = mig({"m.angle": "40.0", "m.diameter": "3.0"})
    assert legacy.get("m.design_mode") == "angle", (
        "a preset pinning m.angle loaded in %r mode, not 'angle': %r"
        % (legacy.get("m.design_mode"), legacy))
    # the control: an explicit mode wins, in both directions
    for mode in ("pore", "angle"):
        kept = mig({"m.angle": "40.0", "m.design_mode": mode})
        assert kept.get("m.design_mode") == mode, kept
    # ... and a preset with no mandrel keys at all is untouched
    assert "m.design_mode" not in mig({"s.width": "10"})
    # Such a preset's eta was TYPED (nothing wrote it back then), so it keeps
    # the page from resolving it over the top. Control: a blank eta, and a
    # preset that already names a mode, do not get the override.
    typed = mig({"m.angle": "40.0", "m.eta": "12"})
    assert typed.get("m.closure_override") is True, typed
    assert "m.closure_override" not in mig({"m.angle": "40.0", "m.eta": ""})
    assert "m.closure_override" not in mig(
        {"m.angle": "40.0", "m.eta": "12", "m.design_mode": "angle"})


def a_legacy_end_margin_becomes_an_offset_from_the_left():
    """`m.end_margin` was the 4-axis placement until 2026-09-03 and meant "this
    far in from the rod's LEFT end" -- the left rung of the ladder that replaced
    it.

    Dropped rather than migrated, an old tube preset loads with the offset at
    its factory 0 and prints flush with the rod tip, silently losing the
    clearance it was saved with. Same shape as the mode migration above.

    RED CONTROL INCLUDED: a preset that already carries the new keys must come
    back untouched, so this cannot pass by overwriting them always.
    """
    import gui_presets
    mig = gui_presets._PresetsMixin._migrate_legacy_end_margin
    old = mig({"m.end_margin": "2.5", "m.diameter": "3.0"})
    assert old.get("m.place_offset") == "2.5", old
    assert old.get("m.place") == "left", old
    # the control: a preset that already places itself wins
    kept = mig({"m.end_margin": "2.5", "m.place_offset": "4",
                "m.place": "Centred on the rod"})
    assert kept["m.place_offset"] == "4" and kept["m.place"] == "Centred on the rod", kept
    # ... and a preset with no mandrel keys at all is untouched
    assert "m.place_offset" not in mig({"s.width": "10"})


def a_legacy_holder_stl_moves_into_the_bed_slot():
    """`rotary.stl` was a second STL slot beside the bed one until 2026-09-03.

    A 4-axis rig has no bed, so the holder now goes IN the bed slot and that key
    is gone. Dropped rather than moved, a preset naming a holder would load with
    no collector body at all and the user's holder would simply vanish.

    RED CONTROL INCLUDED: a preset that names BOTH was written by a rig that had
    both, so the bed must win -- which is what stops this from being "always
    overwrite machine.z_stl".
    """
    import gui_presets
    mig = gui_presets._PresetsMixin._migrate_legacy_holder_stl
    moved = mig({"rotary.stl": "holder.stl", "rotary.on": True})
    assert moved.get("machine.z_stl") == "holder.stl", moved
    assert "rotary.stl" not in moved, moved
    kept = mig({"rotary.stl": "holder.stl", "machine.z_stl": "bed.stl"})
    assert kept["machine.z_stl"] == "bed.stl", kept
    # ... and a preset with no holder at all is untouched
    assert "machine.z_stl" not in mig({"s.width": "10"})
    # "that key is gone": no widget writes it, and left in the persist map it
    # belongs to no machine key list, so every SCAFFOLD preset saved a blank
    # copy of it (found 2026-09-24 by a close/reopen round-trip of every field)
    assert "rotary.stl" not in _M["app"]._persist_map()


def a_legacy_preset_is_not_hidden_by_a_namesake():
    """The OLD single-file format (an inline "presets" list in gui_presets.json)
    is migrated into one file per preset. A legacy preset whose name matched
    one already in the folder -- a shipped one, say -- was written to
    `<slug>_2.json` and then hidden by the loader's first-file-wins rule on a
    name clash: on disk, never in the list. Found by a
    legacy-preset probe.

    It now arrives under the "(2)" name an Import gives a clash.
    RED CONTROL INCLUDED: a legacy preset with a fresh name keeps it exactly.
    """
    import json
    import tempfile
    import gui_presets

    saved = (gui_presets.PRESETS_PATH, gui_presets.PRESETS_DIR)
    with tempfile.TemporaryDirectory() as td:
        pdir = os.path.join(td, "presets")
        os.makedirs(pdir)
        with open(os.path.join(pdir, "default_square.json"), "w") as fh:
            json.dump({"name": "default square", "data": {"s.width": "20"}}, fh)
        ptr = os.path.join(td, "gui_presets.json")
        with open(ptr, "w") as fh:
            json.dump({"default": "default square", "presets": [
                {"name": "default square", "data": {"s.width": "99"}},
                {"name": "Old one", "data": {"s.width": "12"}}]}, fh)
        gui_presets.PRESETS_PATH, gui_presets.PRESETS_DIR = ptr, pdir
        try:
            gui_presets._PresetsMixin()._migrate_legacy_presets()
        finally:
            gui_presets.PRESETS_PATH, gui_presets.PRESETS_DIR = saved
        got = {}
        for fn in os.listdir(pdir):
            with open(os.path.join(pdir, fn)) as fh:
                raw = json.load(fh)
            got[raw["name"]] = raw["data"]["s.width"]
        with open(ptr) as fh:
            default = json.load(fh).get("default")
    # the legacy default meant the legacy preset, not its namesake
    assert default == "default square (2)", default

    # ... and a preset whose file cannot be written is NOT dropped from the
    # inline list: the list is the only copy until its file exists
    import builtins
    with tempfile.TemporaryDirectory() as td:
        pdir = os.path.join(td, "presets")
        os.makedirs(pdir)
        ptr = os.path.join(td, "gui_presets.json")
        with open(ptr, "w") as fh:
            json.dump({"presets": [{"name": "Only copy",
                                    "data": {"s.width": "7"}}]}, fh)

        def refuse(path, mode="r", *a, **k):
            if "w" in mode and os.path.dirname(os.path.abspath(path)) == \
                    os.path.abspath(pdir):
                raise OSError("read-only folder")
            return builtins.open(path, mode, *a, **k)
        gui_presets.PRESETS_PATH, gui_presets.PRESETS_DIR = ptr, pdir
        gui_presets.open = refuse
        try:
            gui_presets._PresetsMixin()._migrate_legacy_presets()
        finally:
            del gui_presets.open
            gui_presets.PRESETS_PATH, gui_presets.PRESETS_DIR = saved
        with open(ptr) as fh:
            kept = json.load(fh).get("presets")
    assert kept and kept[0]["name"] == "Only copy", \
        f"a preset that could not be written was dropped: {kept}"
    assert got.get("default square") == "20", got
    assert got.get("Old one") == "12", got
    assert got.get("default square (2)") == "99", \
        f"the legacy namesake is not reachable by name: {got}"












def collector_toggle_drives_the_worker_argv():
    """The toggle IS the input mode: it picks which argv shared Generate builds.

    This drives the REAL chain -- `run()`, not a hand-set `_mandrel_dispatch` --
    because that flag is set by `_MandrelMixin.run()` as a CONSEQUENCE of
    `_active_mode()`. Setting it by hand skips the very link under test: an
    earlier draft of this case did exactly that and stayed green against a
    `_active_mode()` that could not see the toggle at all.

    The worker launch is the seam: `subprocess.Popen` is stubbed to raise, which
    lands in run()'s own error branch after argv and the fibre params are built.

    RED CONTROL: with `gui_core._active_mode()` reverted to the pre-merge
    frame-identity dispatch, the mandrel half fails -- `--mandrel` never reaches
    argv and `--width` does. Verified 2026-09-01."""
    import gui_run
    app, root = _M["app"], _M["root"]
    app.nb.select(app._page_frames["scaffold"])
    root.update()
    v = app.scaf_vars[0]

    seen = {}
    real_popen, real_box = gui_run.subprocess.Popen, gui_run.messagebox

    class _Silent:
        @staticmethod
        def showerror(*_a, **_k):
            seen["error"] = _a

        @staticmethod
        def showinfo(*_a, **_k):
            seen["error"] = _a

    def _capture(args, **_kw):
        seen["argv"] = list(args)
        raise RuntimeError("worker launch stubbed by the test")

    def generate():
        seen.clear()
        gui_run.subprocess.Popen = _capture
        gui_run.messagebox = _Silent
        try:
            app.run()
        finally:
            gui_run.subprocess.Popen = real_popen
            gui_run.messagebox = real_box
        assert "argv" in seen, f"Generate never reached the worker: {seen}"
        return seen["argv"], list(app.run_fiber_params)

    app.s_collector.set("plate")
    root.update()
    plate_argv, _ = generate()
    assert "--mandrel" not in plate_argv and "--width" in plate_argv, plate_argv

    app.s_collector.set("mandrel")
    root.update()
    assert app._active_mode() == "mandrel"
    tube_argv, fiber_argv = generate()
    assert ("--mandrel" in tube_argv and "--tube-length" in tube_argv
            and "--winding-angle" in tube_argv), tube_argv
    assert ("--width" not in tube_argv and "--shape" not in tube_argv
            and "--plate" not in tube_argv), tube_argv
    # the SHARED fields -- not a private mandrel copy -- are what it sends
    assert tube_argv[tube_argv.index("--layers") + 1] == v["layers"].get().strip()
    assert tube_argv[tube_argv.index("--speed") + 1] == v["speed"].get().strip()
    assert tube_argv[tube_argv.index("--faces") + 1] == app.s_faces.get().strip()
    assert fiber_argv[fiber_argv.index("--standoff") + 1] == app.sim_standoff.get()
    # ... and the flag that routes them is cleared again, whatever happened
    assert not app._mandrel_dispatch, "_mandrel_dispatch leaked past the run"

    app.s_collector.set("plate")
    root.update()


def export_panel_reaches_the_tube_worker():
    """Every Export control still SHOWN in 4-axis must reach the tube worker.

    The rule the merge settled: a knob is either wired or hidden, never
    visible-and-inert. So this asserts both halves --

      wired : mesh detail, sag solver + sag reset, tube facets, "Also render STL",
              the Build-quality preview skip, and the travel fibre.
              `scaffold_gcode`'s --mandrel branch forwards all of them.
      hidden: exit move, G2/G3 arcs, deposition order -- the generator does not
              forward them under --mandrel, so their controls are
              collector-registered "plate" and unmapped in 4-axis.

    The travel fibre MOVED between those two lists when the mandrel branch started
    forwarding --render-travel / --travel-in-stl. Not a rebaseline: the flag swings a
    measured tube -- on a 4 mm rod, a 5.98 mm zigzag inset 2 mm from the datum,
    --travel-in-stl builds 30264 triangles against 30216 without it, off a `.depo` that
    is byte-identical either way and carries the approach at rho 2.000 mm (the rod).

    RED CONTROL: drop `_mesh_tol_args()` and `with_reset=True` from
    `gui_mandrel._build_args_mandrel` and the two flag assertions below fail. The
    travel assertions carry their own two-way control in-line: the tube argv must GAIN
    --travel-in-stl when the box is ticked and lose it when it is not."""
    import gui_run
    app, root = _M["app"], _M["root"]
    app.nb.select(app._page_frames["scaffold"])
    app.s_collector.set("mandrel")
    root.update()
    app._assign_output_paths("scaffold")

    def tube_argv():
        app._mandrel_dispatch = True
        try:
            return app._build_args_mandrel()
        finally:
            app._mandrel_dispatch = False

    was_stl, was_render = app.s_make_stl.get(), app.gen_render.get()
    was_reset, was_reset_n = app.s_sag_reset.get(), app.s_sag_reset_layers.get()
    was_tis, was_tgl = app.s_travel_in_stl.get(), app.s_travel_gcode_line.get()
    try:
        app.s_make_stl.set(True)
        app.s_sag_reset.set(True)
        app.s_sag_reset_layers.set("7")
        root.update()
        argv = tube_argv()
        assert "--stl" in argv, argv
        assert "--mesh-tolerance" in argv, "Mesh detail never reaches the tube"
        assert argv[argv.index("--sag-reset-layers") + 1] == "7", argv
        assert argv[argv.index("--faces") + 1] == app.s_faces.get().strip()
        # Build quality: a preview skips the mesh on a tube exactly as on a plate
        assert ("--no-mesh" in argv) == app._gen_is_preview(), (
            app.gen_render.get(), argv)

        # "Also render STL" off -> no STL step at all, and no STL registered
        app.s_make_stl.set(False)
        root.update()
        off = tube_argv()
        assert "--stl" not in off and "--faces" not in off, off
        assert app._stl_path_for_run(True) is None, (
            "a tube still forces an STL with 'Also render STL' unticked")

        # THE TRAVEL FIBRE. Unconditional on the worker, exactly as on a plate:
        # the jet does not stop on a rod either, so the travel is always solved
        # and only the STL question is the user's. Its own two-way control.
        app.s_make_stl.set(True)
        app.s_travel_in_stl.set(False)
        app.s_travel_gcode_line.set(False)
        root.update()
        plain = tube_argv()
        assert "--render-travel" in plain, (
            "the tube worker is never asked for travel fibre", plain)
        assert "--travel-in-stl" not in plain, plain
        app.s_travel_in_stl.set(True)
        root.update()
        ticked = tube_argv()
        assert "--travel-in-stl" in ticked, (
            "the STL box does not reach the tube worker", ticked)
        # ... and the PLAYER is told, because --render-travel is a .depo cache
        # key: without it the locked sim rejects the cache the run just wrote.
        fp = app._fiber_params_mandrel()
        assert "--render-travel" in fp, (
            "the tube player would reject its own .depo", fp)
        assert "--travel-gcode-line" not in fp, fp
        app.s_travel_gcode_line.set(True)
        root.update()
        assert "--travel-gcode-line" in app._fiber_params_mandrel(), \
            app._fiber_params_mandrel()
    finally:
        app.s_make_stl.set(was_stl)
        app.gen_render.set(was_render)
        app.s_sag_reset.set(was_reset)
        app.s_sag_reset_layers.set(was_reset_n)
        app.s_travel_in_stl.set(was_tis)
        app.s_travel_gcode_line.set(was_tgl)
        root.update()

    # ... and the three the generator does NOT forward are hidden, not inert
    root.update_idletasks()
    root.update()
    plate_only = [w for w, m in app._collector_widgets if m == "plate"]
    labels = []
    every = []

    def walk(w, into):
        try:
            into.append(str(w.cget("text")))
        except tk.TclError:
            pass
        for child in w.winfo_children():
            walk(child, into)

    for w in plate_only:
        walk(w, every)
        if w.winfo_ismapped():
            continue
        walk(w, labels)
    blob = " ".join(labels)
    for phrase in ("G2/G3 arcs", "Deposition order"):
        assert phrase in blob, (
            f"{phrase!r} is not among the sections hidden in 4-axis -- either it "
            f"is shown while inert, or it moved")
    # The other half of the same rule, and the guard against this change being
    # The exit move joined that half on 2026-09-03: a tube has no plate edge but
    # it has two ends, so `scaffold_gcode`'s mandrel branch now routes
    # --exit-margin to `gcode_exit.mandrel_exit_x`. It must be SHOWN in 4-axis,
    # and it must not still call itself a plate.
    # quietly reverted: render travel is WIRED on a tube now (the argv above),
    # so it must not be collector-registered plate-only any more.
    assert "Render travel" not in " ".join(every), (
        "the travel-fibre box is hidden in 4-axis again, but the tube worker "
        "argv above still carries --render-travel -- wired and hidden at once")
    # The field and the toggle row it rides (packed inside it since the two
    # share one line), not the card body: a closed card's body is itself
    # ungridded.
    exit_row = app.s_exit_margin_entry
    assert exit_row.winfo_manager()         and exit_row.master.winfo_manager() == "grid", (
        "the exit-move row is grid_remove()d in 4-axis, but the tube worker argv "
        "above still carries --exit-margin -- wired and hidden at once. (Checked "
        "by grid manager, not by winfo_ismapped: the Export panel sits in a "
        "disclosure, so nothing in it is mapped in this harness either way. Not "
        "by label either -- the label is swapped per collector.)")
    seen = []
    walk(app.s_exit_margin_entry.master, seen)
    assert any("off the end of the tube" in s for s in seen), (
        "in 4-axis the exit row still says 'build plate' -- a tube has no plate, "
        "and a user reading that concludes the option is not for them: %r" % seen)
    app.s_collector.set("plate")
    root.update()




def a_planar_preview_result_cannot_land_on_the_tube():
    """One panel, two owners -- so a planar job still in flight when the toggle
    flips must NOT draw its plate scaffold over the tube.

    FOUND BY LOOKING, not by a test: a screenshot showed the
    plate mesh drawn on top of the collector rod while the toggle read 4-axis.
    The two unguarded paths were `_pv_apply_result` (a worker result arriving
    late) and `_show_scaffold_frame` (reached because `_apply_collector_mode`
    pins `s_count` to 1).

    RED CONTROL: drop `and not self._mandrel_is_active()` from
    `gui_preview._pv_apply_result` and the mandrel-mode assertion below fails.
    Verified 2026-09-01."""
    app, root = _M["app"], _M["root"]
    app.nb.select(app._page_frames["scaffold"])
    root.update()
    panel = app._pv_panel
    calls = []
    real = panel.apply_state
    panel.apply_state = lambda state, subtitle: calls.append(subtitle)

    # a plausible finished planar job for scaffold 1
    state = app._pv_states[0]
    meta = dict(bbox=((0.0, 0.0, 0.0), (30.0, 30.0, 0.2)), physics=False,
                sag=True, array=1, layers=10, n_pts=1234, elapsed=1.0,
                decimated=False)
    paths = [__import__("numpy").zeros((2, 3))]

    try:
        app.s_collector.set("mandrel")
        root.update()
        calls.clear()
        app._pv_apply_result(0, state["gen_sub"], paths, meta, None)
        assert not calls, (
            f"a planar preview result drew over the tube: {calls}")
        # the result is still STORED, so switching back does not re-solve
        assert state["paths"] is paths, "the planar result was dropped, not held"

        app.s_collector.set("plate")
        root.update()
        calls.clear()
        app._pv_apply_result(0, state["gen_sub"], paths, meta, None)
        assert calls, "the planar result never reached the panel in 3-axis"
    finally:
        panel.apply_state = real
        app.s_collector.set("plate")
        app._pv_all_dirty()
        root.update()
























def generate_never_navigates_and_the_viewer_loads_on_entry():
    """One rule, both halves, and they pull in opposite directions: Generate
    does not force a page switch, and switching to the Viewer page yourself
    loads the newly generated result.

    So a finished run must leave the page alone, and ENTERING the Viewer page
    must load the newest result with no "Open viewer" press. It shipped the
    other way round once (08d4c13 made Generate navigate), which is the whole
    reason this case exists.

    Three RED proofs, one per assert:
      * restore `intended = self._viewer_result_ready() and view_page` in
        `gui_embed._EmbedMixin._on_finished` -> the first assert fails.
      * delete the `_on_shell_tab_changed` override -> the second fails.
      * stamp the panel from `(last_gcode, last_depo)` instead of `_result_gen`
        -> the third block fails ("re-entry with nothing new respawned the
        viewer child"). The stamp has to be a counter: every scaffold Generate
        writes the SAME `scaffold.gcode` / `scaffold.depo.npz` into the session
        folder, so a path comparison cannot tell a new result from the one on
        screen, and the type it stamps no longer matches what a launch records.

    `play_gcode` is stubbed: this is about WHEN the viewer is told to load, and
    a real VTK child would make the case about whether VTK is installed."""
    import tempfile
    app, root = _M["app"], _M["root"]
    pf = app._page_frames
    panel = app._embed_panels["unified"]

    tmp = tempfile.mkdtemp(prefix="mew_autoload_")
    gco = os.path.join(tmp, "scaffold.gcode")
    depo = os.path.join(tmp, "scaffold.depo.npz")
    for p in (gco, depo):
        with open(p, "w") as fh:
            fh.write("x")

    saved = {k: getattr(app, k, None)
             for k in ("last_gcode", "last_stl", "last_depo", "expected_gcode",
                       "expected_stl", "proc", "cancelled",
                       "_result_gen", "play_gcode")}
    calls = []
    try:
        app.play_gcode = lambda **kw: calls.append(kw)
        app.proc = None
        app.cancelled = False
        # A PREVIEW run: the -o stem names an STL that was never built, and the
        # .depo cache beside it is the 3-D result. `_on_finished` derives
        # last_depo from that stem, so the stem has to be set -- with it None
        # the run is g-code-only and `gui_run._on_finished` rightly sends the
        # user to the G-code text page, which is not what this case is about.
        app.expected_gcode = gco
        app.expected_stl = os.path.join(tmp, "scaffold.stl")   # never created
        app.last_gcode, app.last_stl, app.last_depo = gco, None, depo

        # ---- 1. a finished run does not navigate ------------------------- #
        app.nb.select(pf["scaffold"])
        root.update()
        before = app.nb.select()
        app._on_finished(0)
        root.update()
        assert app.nb.select() == before, \
            "a finished Generate navigated away from the page the user was on"
        assert not calls, f"a finished Generate opened the viewer: {calls!r}"

        # ---- 2. ...and entering the Viewer loads it ---------------------- #
        app.last_gcode, app.last_depo = gco, depo   # _on_finished reconciled
        app.nb.select(pf["view"])
        root.update()
        assert calls == [{"static": True}], \
            f"entering the Viewer did not load the result: {calls!r}"

        # ---- 3. the stamp is a COUNTER, not the paths -------------------- #
        # pretend the launch took: stamp the panel at the current generation,
        # and give it a live-looking session.
        panel._result_gen = app._result_gen
        panel.session = _FakeAliveSession()
        calls.clear()
        app.nb.select(pf["scaffold"])
        root.update()
        app.nb.select(pf["view"])
        root.update()
        assert not calls, \
            f"re-entry with nothing new respawned the viewer child: {calls!r}"
        # now a NEW result at the SAME paths
        app._mark_result_fresh()
        app.nb.select(pf["scaffold"])
        root.update()
        app.nb.select(pf["view"])
        root.update()
        assert calls == [{"static": True}], \
            ("a new result at the same file paths did not reload the viewer: "
             f"{calls!r}")
    finally:
        panel.session = None
        for k, v in saved.items():
            if v is None and k == "play_gcode":
                try:
                    del app.play_gcode          # unshadow the real method
                except AttributeError:
                    pass
            else:
                setattr(app, k, v)
        app.nb.select(pf["home"])
        root.update()
        shutil.rmtree(tmp, ignore_errors=True)


class _FakeAliveSession:
    """Stands in for a running embedded-viewer child: the page-entry check only
    asks whether the session is alive, and spawning a real VTK process would
    make the case about VTK."""

    def alive(self):
        return True


def viewer_request_channel():
    """U2b: a child->parent 'request' event routes through ViewerPanel._on_event
    to the app's _handle_viewer_request, which dispatches by action to a
    registered handler; unknown actions are ignored and send_child no-ops while
    the panel is idle (no live session)."""
    app = _M["app"]
    panel = app._embed_panels["unified"]
    got = {}
    orig = app._viewer_request_handlers
    app._viewer_request_handlers = lambda: {
        "__probe__": lambda p, m: got.update(panel=p, v=m.get("v"))}
    try:
        panel._on_event("request", {"action": "__probe__", "v": 7})
        assert got.get("panel") is panel and got.get("v") == 7, got
        panel._on_event("request", {"action": "nope"})     # ignored, no raise
        assert panel.send_child(t="noop") is False          # idle -> no-op
    finally:
        app._viewer_request_handlers = orig






def _rail_texts(w):
    out = []
    for ch in w.winfo_children():
        try:
            out.append(str(ch.cget("text")))
        except Exception:  # noqa: BLE001 -- frames have no text
            pass
        out += _rail_texts(ch)
    return out


















def the_mandrel_rcir_law_reaches_the_placement():
    """The Jet lag card's R_cir panel, end to end: the mandrel strip typesets
    Eq. 7 in r_m (not the retired r/(r+H)), Apply puts the typed law in the
    profile the workers read, the 4-axis placement built from that profile
    runs it, and the in-use line and the strip both say so. The card's reset
    hands it back.

    Comparator: the law's own arithmetic -- `0.5 * R_ax` halves the lag
    exactly -- through `models_from_profile` and `placement_for_frame`, the
    path an export takes.

    RED proof: have `_phys_mandrel_recompose` ignore the applied law (always
    the shipped rows): FAILS on the strip assertion. Drop "rcir" from the
    Jet lag card's reset laws: FAILS on the last block. Control: passes.
    Withholding `on_applied` alone does NOT redden it, because the law var's
    trace recomposes the strip too -- two routes, on purpose."""
    app, root = _M["app"], _M["root"]
    import mew_calibration as mc
    import mew_frame
    import mew_mandrel
    import mew_physics
    eds = app._phys_law_editors
    ed = eds["rcir"]
    strip = app._phys_mandrel_strip
    shipped = strip.label._src
    assert "arcsin" in shipped and "r_{m}" in shipped, shipped
    assert "r_{m} + H" in shipped, shipped
    before = app.cal_vars[mc.LAW_SOURCE_KEY].get()
    try:
        ed._law_text.delete("1.0", "end")
        ed._law_text.insert("1.0", "0.5 * R_ax")
        ed._law_apply()
        root.update()
        mat, noz, cal = mc.models_from_profile(
            app._calibration_profile_snapshot())
        assert cal.law_source.get("rcir") == "0.5 * R_ax", cal.law_source
        model = mew_physics.MEWModel(material=mat, nozzle=noz,
                                     working_distance=1.0, calibration=cal)
        pl = mew_mandrel.placement_for_frame(
            mew_frame.CylinderFrame(3.0, nozzle_gap=1.0), cal, model=model)
        assert abs(pl.circ_radius(0.8) - 0.4) < 1e-12, pl.circ_radius(0.8)
        root.update()
        note = app._phys_mandrel_note.get()
        # a calibrated pair outranks the law, so only claim it when none is set
        if not (app.m_lag_axial.get().strip() and app.m_lag_circ.get().strip()):
            assert "your own R_cir law" in note, note
        assert "your own R_cir law" in app._summary_phys_lag()
        src = strip.label._src
        assert src != shipped, "the strip still shows Equation 7"
        assert "0.5" in src and "R_{ax}" in src and "varepsilon" in src, src

        # the Jet lag CARD's reset hands the law back, not only the panel's
        card = app._physics_grid.card("lag")
        found = []

        def walk(w):
            for c in w.winfo_children():
                if c.winfo_class() in ("TButton", "Button"):
                    try:
                        if str(c.cget("text")) == "Reset to defaults":
                            found.append(c)
                    except tk.TclError:
                        pass
                walk(c)
        walk(card.body)
        assert len(found) == 1, found
        found[0].invoke()
        root.update()
        mat, noz, cal = mc.models_from_profile(
            app._calibration_profile_snapshot())
        assert not (cal.law_source or {}).get("rcir"), cal.law_source
        assert strip.label._src == shipped, strip.label._src
    finally:
        app.cal_vars[mc.LAW_SOURCE_KEY].set(before)
        app._phys_mandrel_recompose()
        root.update()


def physics_law_editor_reaches_the_worker():
    """The "Try a different form" panel, end to end: type a law -> Apply -> the persist
    map -> the profile snapshot the workers read -> the geometry.

    Asserting the CHAIN and not the widget is the point. Every earlier version of
    this feature's guard could pass with the editor writing to a var nothing
    reads: `_calibration_persist_keys` is `list(cal_vars)`, so a var that exists
    is automatically "persisted" whether or not `models_from_profile` decodes it.
    The only honest check is the depth the sag solver would actually use.

    RED proofs (each run against an injected defect, control passing):
      * `_store` writing to a var that is not in `cal_vars` -- the snapshot
        carries no law and the depth stays at the shipped value: FAILS.
      * `models_from_profile` not decoding LAW_SOURCE_KEY (the pre-feature
        state): same failure, one step later in the chain.
      * `apply()` storing without calling `check()` first -- the deliberately
        broken expression is then persisted: FAILS on the last block.
    """
    app, root = _M["app"], _M["root"]
    import mew_calibration as mc
    import mew_lawlang
    import mew_physics
    eds = getattr(app, "_phys_law_editors", None)
    assert eds, "no law editors were built on the Physics page"
    assert set(eds) == set(mew_lawlang.LAWS), \
        f"an editable law has no editor: {sorted(set(mew_lawlang.LAWS) - set(eds))}"
    assert "lag" not in eds, (
        "the Jet lag card must NOT get an expression editor -- the tracker "
        "integrates the law's closed-form inverse, so a typed form would leave "
        "the STL following one law and the card showing another")
    # both law keys are persisted, and they are cal.* so they ride the PHYSICS
    # preset rather than the scaffold one
    pm = app._persist_map()
    for k in (mc.LAW_SOURCE_KEY, mc.LAW_CONSTS_KEY):
        assert k in pm, f"{k} is not in the persist map"
        assert k in set(app._calibration_persist_keys()), \
            f"{k} does not ride the Physics preset"

    ed = eds["sag"]
    before = app.cal_vars[mc.LAW_SOURCE_KEY].get()
    try:
        ed._law_text.delete("1.0", "end")
        ed._law_text.insert("1.0", "c0*span**2 + c1*span")
        ed._law_add_const("c0", "0.011")
        ed._law_add_const("c1", "0.002")
        ed._law_apply()
        root.update()
        # ... the whole way to the number the sag solver would use
        prof = app._calibration_profile_snapshot()
        mat, noz, cal = mc.models_from_profile(prof)
        assert cal.law_source.get("sag") == "c0*span**2 + c1*span", \
            f"the law did not reach the profile: {cal.law_source}"
        model = mew_physics.MEWModel(material=mat, nozzle=noz, calibration=cal)
        got = model.bridge_sag_depth(2.0, 0.02, 1.0, 1.0)
        want = 0.011 * 4 + 0.002 * 2
        assert abs(got - want) < 1e-12, \
            f"the applied law did not reach the depth: {got} != {want}"
        assert "your own law" in app._summary_phys_sag(), \
            "the module tile does not say the law was replaced"

        # a law that will not compile is REFUSED, not stored: a stored broken law
        # would raise in the worker, mid-export, instead of here where it can be
        # read and fixed
        ed._law_text.delete("1.0", "end")
        ed._law_text.insert("1.0", "span **")
        ed._law_apply()
        root.update()
        still = mc.models_from_profile(
            app._calibration_profile_snapshot())[2].law_source.get("sag")
        assert still == "c0*span**2 + c1*span", \
            f"a broken expression was stored: {still!r}"
    finally:
        ed._law_reset()
        app.cal_vars[mc.LAW_SOURCE_KEY].set(before)
        root.update()
    assert mc.models_from_profile(
        app._calibration_profile_snapshot())[2].law_source == {}, \
        "Reset did not put the shipped law back"










def preview_can_be_disabled():
    """"Disable preview" is persisted on the scaffold preset, OFF by default,
    and while ticked no preview build reaches the worker for ANY collector.

    Asked for after a 12 x 12 mm design on an imported collector previewed for
    ~110 s on a thread inside the GUI process and made the whole window lag
    (2026-09-16). The worker is stubbed so nothing actually builds; the
    MAILBOX is what is observed, because a queued job is the thing the switch
    exists to stop.

    RED PROOF: with the `_pv_preview_disabled()` gate removed from `_pv_kick`,
    the planar half queues a job while ticked and this fails."""
    import gui_preview
    app = _M["app"]
    if not hasattr(app, "pv_disabled") or getattr(app, "_pv_panel", None) is None:
        return
    pm = app._persist_map()
    assert "pv.disabled" in pm, "pv.disabled dropped from the persist map"
    assert "pv.disabled" in set(app._keys_for_kind("scaffold")),         "pv.disabled must ride the scaffold preset"
    assert app.pv_disabled.get() is False, "the preview must be ON by default"
    real_worker = app._pv_ensure_worker
    app._pv_ensure_worker = lambda: None
    i = app._active_scaffold_index()
    state = app._pv_states[i]
    try:
        with app._pv_cv:
            app._pv_mailbox.clear()
        # a job already queued is dropped when the box is ticked
        with app._pv_cv:
            app._pv_mailbox[i] = (0, None, None)
        app._pv_pending += 1
        app.pv_disabled.set(True)
        state["dirty"] = True
        app._pv_kick()
        assert i not in app._pv_mailbox,             "a planar preview build was queued while the preview is disabled"
        assert state["status"] == gui_preview.PREVIEW_DISABLED_STATUS,             "planar status does not say the preview is disabled"
        assert state["dirty"], "disabled must leave the preview stale"
        # the four-axis half, forced active for the call
        app._mandrel_is_active = lambda: True
        try:
            if getattr(app, "_mandrel_preview", None) is not None:
                app._refresh_mandrel_preview()
                assert gui_preview.PV_MANDREL not in app._pv_mailbox,                     "a four-axis preview build was queued while disabled"
                assert app._mandrel_preview_state["status"] ==                     gui_preview.PREVIEW_DISABLED_STATUS
        finally:
            del app._mandrel_is_active
        # CONTROL: unticked, the same call queues the planar build
        app.pv_disabled.set(False)
        state["dirty"] = True
        app._pv_kick()
        assert i in app._pv_mailbox,             "unticked, the planar preview did not queue a build (the check "             "above would pass for a preview that never builds)"
    finally:
        app.pv_disabled.set(False)
        with app._pv_cv:
            app._pv_mailbox.clear()
        app._pv_pending = 0
        app._pv_ensure_worker = real_worker








def hidden_designer_builds_on_arrival():
    """The Scaffold designer's 4-axis preview is not built while another page
    is up, and is built when its page comes up. The G-code page edits the same
    tube and builds its own picture; a second, invisible build beside it cost
    ~7 s of Tk-thread freezing per 3 -> 4 axis switch (measured), and the
    PageStack never unmaps a page, so `<Map>` never refreshed it on arrival.
    What is observed is every POST to the mailbox, recorded as it is made:
    the preview worker is one thread that lives for the app's life, so once
    an earlier case has started it, it pops a post within milliseconds and a
    look at the mailbox afterwards sees nothing. Stubbing `_pv_ensure_worker`
    cannot stop a worker that is already running.

    CONTROL: the same edit with the designer page up queues the build, and
    arriving on it after a held-back one queues it. RED PROOF: with the
    `_mandrel_designer_hidden()` gate removed, the build is queued while the
    G-code page is up and the first assertion fails."""
    import gui_preview
    app, root = _M["app"], _M["root"]
    if getattr(app, "_mandrel_preview", None) is None:
        return
    real_worker = app._pv_ensure_worker
    app._pv_ensure_worker = lambda: None

    def settle(sec):
        import time as _t
        end = _t.time() + sec
        while _t.time() < end:
            root.update()
            _t.sleep(0.02)

    class _Recording(dict):
        posted = set()

        def __setitem__(self, k, v):
            self.posted.add(k)
            super().__setitem__(k, v)

    with app._pv_cv:
        real_mailbox = app._pv_mailbox
        app._pv_mailbox = _Recording(real_mailbox)

    def queued():
        return gui_preview.PV_MANDREL in _Recording.posted

    try:
        app._nav_select("scaffold")
        settle(0.3)
        app.s_collector.set("mandrel")
        app._collector_mode_chosen()
        settle(1.2)
        assert queued(), "control: the designer page up queued no build"
        _Recording.posted.clear()
        with app._pv_cv:
            app._pv_mailbox.clear()
        app._mandrel_stale = False
        app._nav_select("gcode")
        settle(0.3)
        assert app._mandrel_designer_hidden(), "the designer is not hidden"
        app.m_angle.set(str(float(app.m_angle.get()) + 1.0))
        settle(1.2)
        assert not queued(), \
            "a 4-axis build was queued for the designer while it was hidden"
        assert app._mandrel_stale, "the held-back build was not remembered"
        app._nav_select("scaffold")
        settle(1.2)
        assert queued(), "arriving on the designer queued no build"
        assert not app._mandrel_stale
    finally:
        app._pv_ensure_worker = real_worker
        with app._pv_cv:
            real_mailbox.clear()
            app._pv_mailbox = real_mailbox
        app._pv_pending = 0
        app.s_collector.set("plate")
        app._collector_mode_chosen()
        settle(0.5)
        app._nav_select("home")


def viewer_plays_the_generated_machine():
    """The viewers play a result on the machine and collector it was
    GENERATED on, not the ones the Collector toggle has loaded onto the
    Machine page since. The Hide ticks stay live; a result this session did
    not generate still reads the page.

    Reported: a 4-axis scaffold generated, the toggle switched back to 3
    axis, the Viewer opened -- the 3-axis machine. Stood in for by writing
    the fields the toggle's machine load writes (the rotary, the bed).

    RED proof: `gui_sweeps._sw_rig_machine_args` (the modern
    shell's route) falling back to the live `_machine_args()` again fails at
    `played on the page's machine after Generate`."""
    import json
    import tempfile
    app = _M["app"]
    fd, gco = tempfile.mkstemp(suffix=".gcode")
    os.close(fd)
    names = ("machine_show", "machine_x_stl", "machine_z_stl", "rotary_on",
             "machine_hide_bed")
    saved = {n: getattr(app, n).get() for n in names}
    was_last = app.last_gcode
    was_scene = getattr(app, "_viewer_scene", None)

    def played():
        args = app._player_args(static=True)
        with open(args[args.index("--machine") + 1]) as fh:
            return json.load(fh), args

    try:
        app.machine_show.set(True)
        app.machine_x_stl.set("head.stl")
        app.machine_z_stl.set("mount4.stl")
        app.rotary_on.set(True)
        app.machine_hide_bed.set(False)
        app._viewer_scene_note()                 # Generate, 4-axis page
        app._viewer_scene_commit(gco)
        app.last_gcode = gco
        app.machine_z_stl.set("bed3.stl")        # the toggle, after Generate
        app.rotary_on.set(False)
        prof, args = played()
        assert prof["parts"]["bed"] == "mount4.stl" and (
            prof.get("rotary") or prof.get("rotaries")), \
            f"played on the page's machine after Generate: {prof}"
        app.machine_hide_bed.set(True)           # display ticks stay live
        prof, _ = played()
        assert "bed" in prof.get("hidden_parts", []), prof
        app.last_gcode = gco + ".other"          # not generated here: live
        with open(app.last_gcode, "w"):
            pass
        prof, args = played()
        assert prof["parts"]["bed"] == "bed3.stl", prof
        os.remove(app.last_gcode)
    finally:
        for n, v in saved.items():
            getattr(app, n).set(v)
        app.last_gcode = was_last
        app._viewer_scene = was_scene
        os.remove(gco)










def sag_solver_argv_matrix():
    """The Sag-solver combo drives the WORKER argv and the PLAYER argv the same
    way on both panels, and "Exact" is byte-identical to the pre-dropdown state.

    This is the whole point of the control: `--sag-approx` is a `.depo` cache key
    as well as a solver switch, so a spelling that reached the worker but not the
    player would make the sim ride geometry the STL does not have."""
    app = _M["app"]
    app.in_var.set(__file__)                # any existing file; argv only

    def scaffold_args():
        return app._build_args_scaffold()

    cases = {
        "exact":       (False, False),      # (--no-sag, --sag-approx)
        "approximate": (False, True),
        "off":         (True, False),
    }
    for val, (want_no_sag, want_approx) in cases.items():
        for mode_var, worker, player in (
                (app.s_sag_mode, scaffold_args, app._fiber_params_scaffold),
                (app.sag_mode, app._build_args_gcode, app._fiber_params_gcode)):
            mode_var.set(SAG_SOLVER_VAL2LBL[val])
            for label, argv in (("worker", worker()), ("player", player())):
                assert ("--no-sag" in argv) is want_no_sag, \
                    f"{val}/{label}: --no-sag {'missing' if want_no_sag else 'leaked'}"
                assert ("--sag-approx" in argv) is want_approx, \
                    f"{val}/{label}: --sag-approx {'missing' if want_approx else 'leaked'}"
                if val != "off":
                    assert "--sag-multiplier" in argv, \
                        f"{val}/{label}: sag multiplier dropped"
    # back to the default and prove it emits neither flag (the old unticked box)
    app.s_sag_mode.set(SAG_SOLVER_VAL2LBL["exact"])
    app.sag_mode.set(SAG_SOLVER_VAL2LBL["exact"])
    for argv in (scaffold_args(), app._build_args_gcode(),
                 app._fiber_params_scaffold(), app._fiber_params_gcode()):
        assert "--no-sag" not in argv and "--sag-approx" not in argv, \
            "the Exact default must add no sag flag at all"




def corner_resolution_reaches_export_and_player():
    """The Export panel's Corner-resolution field must reach the WORKER argv and
    the PLAYER argv as one value, and vanish from both when physics deposition is
    off.

    One field, both argvs, is the whole contract. `mew_export` bakes the `.depo`
    at this grid and the live-adjust march re-lays touched layers at it; a value
    that reached only one of them would leave an edited layer sampled differently
    from the untouched layers beside it in the same scene -- which is what
    test_mew_incremental's bit-identity guard exists to forbid.

    RED PROOF: with `args += self._fill_per_tau_args()` removed from
    `gui_sim._sim_play_args`, this fails on the first assert ("--fill-per-tau
    missing from the physics-on argv"). With the `_fill_per_tau_active` guard
    dropped so the flag is always emitted, it fails on the physics-off assert
    ("--fill-per-tau leaked with physics off"), which is the case that would hand
    the PLAIN worker a flag it does not accept.
    """
    app = _M["app"]
    was = app.sim_physics_deposit.get()
    try:
        app.sim_physics_deposit.set(True)
        app.s_fill_per_tau.set("12")
        argv = app._sim_play_args()
        assert "--fill-per-tau" in argv,             "--fill-per-tau missing from the physics-on argv"
        assert argv[argv.index("--fill-per-tau") + 1] == "12",             f"wrong value forwarded: {argv[argv.index('--fill-per-tau') + 1]!r}"
        # the export prefix and the player both ride _sim_play_args, so one
        # presence check covers both consumers -- assert that is still true
        pre = app._export_prefix("worker:convert", "converter")
        assert "--fill-per-tau" in pre and "mew_export.py" in " ".join(pre),             "the physics export prefix dropped the grid"

        app.sim_physics_deposit.set(False)
        assert "--fill-per-tau" not in app._sim_play_args(),             "--fill-per-tau leaked with physics off"
    finally:
        app.sim_physics_deposit.set(was)
        app.s_fill_per_tau.set("6")




def auto_corner_resolution_reaches_all_three():
    """"Auto corner resolution" (Export panel, plate scaffolds) must put ONE
    adaptive corner grid on the export argv, the player argv and the live
    preview, and nothing on any of them when unticked.

    All three, because the live-adjust march re-lays layers beside the ones the
    export cached in the `.depo`, so a grid that reached the export but not the
    player would sample an edited layer differently from its neighbours; and the
    preview is where a faceted crest is noticed. Unticked must be EMPTY on all
    three, which is what keeps an unticked plate export byte-identical. The box
    is ON by default. The export and the player take the Curve-quality tier,
    set to "fine" below so a hard-coded default cannot pass; the preview takes
    ITS OWN Curve quality (`pv_curve_quality`, Draft by default for speed; see
    `_pv_snapshot_options`), whatever that says.

    RED PROOF: with `args += self._flat_auto_corner_args()` removed from
    `_fiber_params_scaffold`, this fails on "player argv lacks the grid"; with
    `corner_chord=corner_chord` dropped from the preview's physics dict, it fails
    on "preview corner grid"."""
    import mew_deposition
    app = _M["app"]
    saved = (app.sim_physics_deposit.get(), app.pv_show_lag.get(),
             app.s_auto_corner_grid.get(), app.s_curve_quality.get(),
             app.pv_auto_corner_grid.get())

    def grid_of(argv):
        if "--auto-corner-grid" not in argv:
            return None
        i = argv.index("--auto-corner-grid")
        return float(argv[i + 1]), argv[argv.index("--curve-quality") + 1]

    try:
        app.sim_physics_deposit.set(True)
        app.pv_show_lag.set(True)
        app.s_curve_quality.set("fine")
        d = float(app.scaf_vars[0]["diameter"].get())

        app.s_auto_corner_grid.set(False)
        assert grid_of(app._build_args_scaffold()) is None, \
            "export argv carries the grid while unticked"
        assert grid_of(app._fiber_params_scaffold()) is None, \
            "player argv carries the grid while unticked"
        # the preview has its OWN box (Settings > Quality > Live preview): the
        # export's does not reach it, and its own does
        assert app._pv_snapshot_options(0)["physics"]["corner_chord"] \
            is not None, "the export's box switched the preview's grid off"
        app.pv_auto_corner_grid.set(False)
        assert app._pv_snapshot_options(0)["physics"]["corner_chord"] is None, \
            "preview refines while unticked"
        app.pv_auto_corner_grid.set(True)

        app.s_auto_corner_grid.set(True)
        assert grid_of(app._build_args_scaffold()) == (d, "fine"), \
            f"export argv lacks the grid: {grid_of(app._build_args_scaffold())}"
        assert grid_of(app._fiber_params_scaffold()) == (d, "fine"), \
            f"player argv lacks the grid: {grid_of(app._fiber_params_scaffold())}"
        got = app._pv_snapshot_options(0)["physics"]["corner_chord"]
        assert got == mew_deposition.auto_corner_grid(d, "draft"), \
            f"preview corner grid {got!r}"

        # it is a persisted scaffold-preset field
        assert "s.auto_corner_grid" in app._keys_for_kind("scaffold")
        snap = app._snapshot_for_kind("scaffold")
        app.s_auto_corner_grid.set(False)
        app._apply_preset_for_kind("scaffold", snap)
        assert app.s_auto_corner_grid.get() is True, \
            "preset load dropped Auto corner resolution"
    finally:
        app.sim_physics_deposit.set(saved[0])
        app.pv_show_lag.set(saved[1])
        app.s_auto_corner_grid.set(saved[2])
        app.s_curve_quality.set(saved[3])
        app.pv_auto_corner_grid.set(saved[4])


def physics_laws_switch_independently():
    """Each Physics-page law switch reaches generation/export on its own, and the
    previews' own boxes never reach it.

    Comparator: the argv the export and the player are launched with -- what
    actually decides the written geometry -- plus the preview worker's options.
    The four law switches are Jet lag (sim_physics_deposit), Fiber sagging
    (sim_sag_law), Fiber fusion (sim_physical_fusion) and Fiber diameter
    (sim_diam_law, the inverse of sim_diam_override).

    RED CONTROLS (2026-10-20, each a one-line revert run against this case):
    dropping the sim_sag_law gate from `gui_args._sag_solver_args` fails the
    "--no-sag" assert; dropping `--no-jet-lag` from `gui_sim._export_prefix`
    fails the lag-off assert."""
    app, root = _M["app"], _M["root"]
    names = ("sim_physics_deposit", "sim_sag_law", "sim_physical_fusion",
             "sim_diam_override", "pv_show_lag", "pv_show_sag",
             "pv_show_fusion")
    was = {n: getattr(app, n).get() for n in names}
    plain = gui_common.worker_cmd("worker:convert")

    def setv(**kw):
        for n, v in kw.items():
            getattr(app, n).set(v)
        root.update()

    def prefix():
        return app._export_prefix("worker:convert", "converter")

    try:
        setv(sim_physics_deposit=True, sim_sag_law=True,
             sim_physical_fusion=True, sim_diam_override=False,
             pv_show_lag=True, pv_show_sag=True, pv_show_fusion=True)
        pre = prefix()
        for flag in ("--physics-deposit", "--physical-sag",
                     "--physical-fusion", "--dynamic-diameter"):
            assert flag in pre, f"all laws on: {flag} missing from {pre}"
        assert "--no-jet-lag" not in pre

        # Jet lag off: the wrapper still runs (sag + fusion), centreline kept,
        # and the planner / coiling wave that ride the lag forward 0
        setv(sim_physics_deposit=False)
        pre = prefix()
        assert "--no-jet-lag" in pre, f"lag off: no --no-jet-lag in {pre}"
        assert "--physics-deposit" not in pre
        assert "--dynamic-diameter" not in pre
        assert "--physical-sag" in pre and "--physical-fusion" in pre
        assert pre[pre.index("--max-accel") + 1] == "0"
        assert pre[pre.index("--coil-wave-amp") + 1] == "0"
        assert app._diam_is_physics(), "Jet lag must not switch the diameter"

        # Fiber sagging off: --no-sag for every worker AND the player
        setv(sim_physics_deposit=True, sim_sag_law=False)
        assert app._sag_solver_args(app.s_sag_mode, app.s_sag_mult) == \
            ["--no-sag"], "sagging off must send --no-sag"
        assert "--physical-sag" not in app._sim_play_args()

        # all three wrapper laws off -> the plain worker, byte-identical
        setv(sim_physics_deposit=False, sim_physical_fusion=False)
        assert prefix() == plain, f"all off should be the plain worker: {prefix()}"

        # the diameter switch and the override are one setting
        setv(sim_diam_override=True)
        assert not app.sim_diam_law.get()
        app.sim_diam_law.set(True)
        root.update()
        assert not app.sim_diam_override.get()

        # previews: their own box hides a law, the Physics switch removes it
        setv(sim_physics_deposit=True, sim_sag_law=True,
             sim_physical_fusion=True, pv_show_lag=False)
        assert app._pv_snapshot_options(0)["physics"] is None
        assert prefix() != plain and "--physics-deposit" in prefix(), \
            "the preview's Jet lag box must not reach the export"
        setv(pv_show_lag=True, sim_physics_deposit=False)
        assert app._pv_snapshot_options(0)["physics"] is None
        setv(sim_physics_deposit=True)
        assert app._pv_snapshot_options(0)["physics"] is not None
        setv(sim_sag_law=False)
        assert app._pv_snapshot_options(0)["sag"] is None
        setv(sim_sag_law=True, pv_show_sag=False)
        assert app._pv_snapshot_options(0)["sag"] is None
        assert "--no-sag" not in app._sag_solver_args(app.s_sag_mode,
                                                      app.s_sag_mult)
        setv(pv_show_sag=True, sim_physical_fusion=False)
        assert app._pv_fusion_law() == (None, None)

        # a pre-split preset saved with the old master switch OFF meant no physics
        mig = app._migrate_legacy_physics_master({"sim.physics_deposit": False})
        assert mig["sim.sag_law"] is False and mig["sim.physical_fusion"] is False
        assert mig["sim.diam_override"] is True
        kept = {"sim.physics_deposit": False, "sim.sag_law": True}
        assert app._migrate_legacy_physics_master(kept) == kept
    finally:
        for n, v in was.items():
            getattr(app, n).set(v)
        root.update()








# ---------------------------------------------------------------------------
# the Ctrl+F control finder (gui_finder) + the one-vocabulary rule
# ---------------------------------------------------------------------------
































def simulation_page_is_the_viewer():
    """The Simulation page is the viewer, edge to edge, with no row of its own:
    its tools and window buttons sit in a card of their own, and the rail is
    only as tall as its content, so the picture shows between the two. The card
    outlives the rail's rebuild on every viewer's hello.

    Comparator: Tk's own geometry. The page frame's box is the box the shell
    gives a page, and the viewport has to be exactly that. The top row's tallest
    child has to be a tab, since a tab is what the row exists to hold.

    RED PROOF (observed): `padding=margin` back on the page frame fails at the
    viewport box, and the rail placed at the viewport's full height fails at
    `the rail still fills the column`."""
    import gui_embed
    app, root = _M["app"], _M["root"]
    panel = app._embed_panels["unified"]
    app._nav_open("simulation")
    root.update()
    # read from `place_info`, the geometry asked for: place() arranges nothing
    # under a withdrawn root, and showing this one would take the screen
    page, vp = app._unified_view_frame, panel.viewport
    assert page.winfo_children() == [panel], "the page holds a row"
    box = (panel.winfo_x(), panel.winfo_y(), vp.winfo_width(), vp.winfo_height())
    want = (0, 0, page.winfo_width(), page.winfo_height())
    assert box == want, f"viewport {box} is not the page {want}"
    assert page.winfo_width() == app.nb.winfo_width(), "the page is inset"
    panel._build_rail(panel.rail, gui_embed._skeleton_hello())
    root.update()
    panel._place_overlays()
    assert panel._foot.winfo_exists(), "the rail's rebuild took the foot"
    assert panel._cards[0] == panel._foot_cards[0], "the rebuild lost its card"

    def spot(w):
        info = w.place_info()
        return {k: int(float(info[k])) for k in ("x", "y", "width", "height")}
    rail, foot = spot(panel.rail), spot(panel._foot)
    assert rail["y"] + rail["height"] < foot["y"],         "the rail still fills the column"
    assert foot["y"] + foot["height"] < vp.winfo_height(),         "the card left the picture"
    assert panel._foot.winfo_reqwidth() <= foot["width"], "the card is clipped"
    row = app._nav_row
    tab = max(b.winfo_reqheight() for b in app._nav_btns.values())
    tallest = max(c.winfo_reqheight() for c in row.winfo_children())
    assert tallest <= tab + 2, f"the top row is {tallest}, its tabs {tab}"


def legacy_ui_round_trip():
    """More's Legacy UI trades the top row for the sidebar on every page, and
    its G-code text is the CAD page's own text, alone and editable. Exit legacy
    UI puts everything back.

    Comparator: the CAD page itself, measured before the switch, is what the
    way out has to restore. `place_info` and `winfo_manager` are the geometry
    asked for, because place() arranges nothing under a withdrawn root.

    RED PROOF (observed): `_gce_sync_text_only` left a no-op fails at `the
    ribbon is still up`, and the sidebar packed without `_nav_wrap.grid_remove`
    fails at `the top row is still up`."""
    app, root = _M["app"], _M["root"]
    menu = app._nav_more_menu
    labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
              if menu.type(i) == "command"]
    assert labels[-1] == "Legacy UI", labels

    app._nav_select("gcode")
    root.update()
    app._gce_layout()
    assert app._gce_ribbon.winfo_manager() == "grid"
    assert app._gce_browser.winfo_manager() == "place"
    code_shown = app._gce_code_shown
    try:
        menu.invoke(menu.index("Legacy UI"))
        root.update()
        assert app._nav_wrap.winfo_manager() == "", "the top row is still up"
        assert app._legacy_sidebar.winfo_manager() == "pack"
        for pid in app._legacy_btns:
            app._legacy_btns[pid].invoke()
            root.update()
            assert app._legacy_sidebar.winfo_manager() == "pack", pid
            assert app._legacy_btns[pid].cget("style") == "NavSel.TButton", pid
            # Exit is the sidebar's, not a page's
            assert str(app._legacy_back).startswith(str(app._legacy_sidebar))

        # calibration is a mode of the CAD window: its chrome comes back
        app._legacy_btns["calibration"].invoke()
        root.update()
        assert app._gce_ribbon.winfo_manager() == "grid", "no ribbon to calibrate"

        app._legacy_btns["gcode"].invoke()
        root.update()
        app._gce_layout()
        root.update()
        assert app._gce_ribbon.winfo_manager() == "", "the ribbon is still up"
        assert app._gce_browser.winfo_manager() == "", "the tree is still up"
        vp = app._gce_viewport
        info = app._gce_code.place_info()
        assert (int(info["width"]), int(info["height"])) == (
            vp.winfo_width(), vp.winfo_height()), info
        was = app.gce.text.get("1.0", "end-1c")
        app.gce.text.insert("1.0", "G4 P1\n")
        root.update()
        assert app.gce.text.get("1.0", "end-1c") == "G4 P1\n" + was, \
            "the text does not take typing"
        app.gce.text.delete("1.0", "2.0")
    finally:
        app._legacy_leave()
        root.update()
    assert app._nav_wrap.winfo_manager() == "grid", "the top row stayed away"
    assert app._legacy_sidebar.winfo_manager() == "", "the sidebar stayed"
    app._nav_select("gcode")
    root.update()
    app._gce_layout()
    assert app._gce_ribbon.winfo_manager() == "grid", "the ribbon stayed away"
    assert app._gce_browser.winfo_manager() == "place", "the tree stayed away"
    assert app._gce_code_shown == code_shown, "the text's own switch moved"
    app._nav_select("home")
    root.update()


def teardown():
    root = _M.get("root")
    if root is not None:
        _destroy(root)




def a_summary_never_takes_the_page_down():
    """A half-typed entry is the normal state of a form -- validation runs on
    commit, so every formatter sees garbage regularly. A cosmetic line must never
    raise through the trace that computes it.

    THE HOOK IS THE WHOLE TEST. Summaries are recomputed from an after_idle
    callback, so an exception there goes to Tk's report_callback_exception, gets
    printed to stderr, and never reaches root.update(). A first version of this
    case asserted on the tile's TEXT instead and stayed GREEN with both guards
    ripped out of gui_pages -- it could not see the thing it is named after.
    Installing the hook is the only vantage point the property is visible from."""
    app, root = _M["app"], _M["root"]
    v = app.scaf_vars[0]
    geo = app._scaf_grid.card("geometry")
    orig = v["width"].get()
    caught = []
    prev = root.report_callback_exception
    root.report_callback_exception = (
        lambda exc, val, tb: caught.append("%s: %s" % (exc.__name__, val)))
    try:
        for junk in ("", "-", ".", "1e", "abc", "  "):
            v["width"].set(junk)
            root.update()
            assert isinstance(geo._summary_text, str)
        v["width"].set("17")
        root.update()
        assert "17" in geo._summary_text, \
            "the summary stopped updating after junk input: %r" % geo._summary_text
    finally:
        root.report_callback_exception = prev
        v["width"].set(orig)
        root.update()
    assert not caught, \
        "a summary formatter raised on half-typed input: %s" % caught[:3]


def main():
    check("modern build (fonts + shell + pages)", modern_build)
    if "app" in _M:
        check("modern page sweep", modern_page_sweep)
        check("persist-map parity (no dropped controls)", persist_parity)
        check("every scaffold var the generator reads is persisted",
              scaffold_vars_are_persisted)
        check("Mandrel mode + shared worker", mandrel_mode_wires_the_shared_worker)
        check("a legacy mandrel preset still means its pinned angle",
              a_legacy_preset_still_means_the_angle_it_pinned)
        check("a legacy end margin becomes an offset from the left",
              a_legacy_end_margin_becomes_an_offset_from_the_left)
        check("a legacy holder STL moves into the bed slot",
              a_legacy_holder_stl_moves_into_the_bed_slot)
        check("a legacy preset is not hidden by a namesake",
              a_legacy_preset_is_not_hidden_by_a_namesake)
        check("collector toggle drives the worker argv",
              collector_toggle_drives_the_worker_argv)
        check("Export panel reaches the tube worker",
              export_panel_reaches_the_tube_worker)
        check("a planar preview result cannot land on the tube",
              a_planar_preview_result_cannot_land_on_the_tube)
        # ("Mandrel form matches scaffold margin" retired 2026-09-01: after the
        #  tab merge there is ONE form and one inset, so the assertion could no
        #  longer fail. Its replacement is the colour-inheritance guard above,
        #  which is the invariant the merge actually had to establish.)
        check("a summary never takes the page down",
              a_summary_never_takes_the_page_down)
        check("law editor reaches the worker profile",
              physics_law_editor_reaches_the_worker)
        check("the mandrel R_cir law reaches the placement",
              the_mandrel_rcir_law_reaches_the_placement)
        check("Generate never navigates; the Viewer loads on entry",
              generate_never_navigates_and_the_viewer_loads_on_entry)
        check("viewer child->parent request channel (U2b)",
              viewer_request_channel)
        check("Disable preview stops every collector's build",
              preview_can_be_disabled)
        check("hidden Scaffold designer builds on arrival, not before",
              hidden_designer_builds_on_arrival)
        check("the viewer plays the machine a result was generated on",
              viewer_plays_the_generated_machine)
        check("Sag solver drives worker AND player argv on both panels",
              sag_solver_argv_matrix)
        check("each physics law switches export alone; previews never do",
              physics_laws_switch_independently)
        check("Corner resolution reaches the export AND player argv",
              corner_resolution_reaches_export_and_player)
        check("Auto corner resolution reaches export, player and preview",
              auto_corner_resolution_reaches_all_three)
        check("the Simulation page is the viewer, edge to edge",
              simulation_page_is_the_viewer)
        check("More's Legacy UI goes there and back", legacy_ui_round_trip)
        teardown()
        # LAST, after the shared app is gone: it builds two apps of its own at
        # real window sizes, and run earlier it knocked the two DPI-fit cases
        # that follow it over
    n_fail = len(FAILURES)
    # Counted, not hardcoded. The literal that used to sit here had drifted to 32
    # against 42 cases actually run, so a case that never executed -- a teardown
    # that swallowed the tail of the list -- would still have printed a full
    # green total. (Found 2026-09-01 while adding the collector-toggle cases.)
    total = len(RAN)
    print(f"\n{'FAIL' if n_fail else 'PASS'}: {total - n_fail}/{total} tests passed")
    if n_fail:
        print("failed:", ", ".join(FAILURES))
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
