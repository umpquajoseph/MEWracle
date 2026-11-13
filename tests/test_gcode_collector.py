#!/usr/bin/env python3
"""test_gcode_collector.py -- the collector and the 4-axis design tools on the
CAD-style G-code page (gui_gcode_collector), driven as a user would, in a real
withdrawn Tk app.

Comparators (none of them written by the page):
  * the Scaffold designer's own seams: `_apply_axis_defaults` (what its 3/4/5
    bar does on a click), `_build_args_scaffold` (the argv the worker gets),
    and the App's own vars (`plate_x`, `m_angle`...) a panel row must bind;
  * invariants: a panel closed gives the designer back every handle it
    lent; the Collector row's eye is the picture's own collector toggle.

Cases, each with its RED proof (the defect injected in memory by a probe script that does not ship,
the case seen failing; the
clean run is the GREEN control):
  ribbon_follows_collector   RED: `_gce_c_mode_sync` a no-op (the 3-axis
                             groups stay packed in 4 axis).
  collector_click_loads_defaults
                             RED: `_gce_c_chosen` a no-op (no axis defaults
                             loaded).
  plate_dims_are_machine     RED: the panel's plate rows built over the bare
                             App's `_build_plate_fields` replaced by one
                             binding a fresh var (the row edits nothing).
  cell_panel_is_the_scaffolds
                             RED: `_TubeView.__getattr__` returning the
                             App's methods unscoped (Clear empties the
                             designer's drawing, not scaffold 2's).
  winding_angle_reaches_argv RED: `_gce_tube_scope` not swapping m_angle
                             (the designer's angle reaches the worker).
  new_scaffold_goes_beside   RED: `_gce_tube_place_beside` a no-op (the
                             new tube on top of the first; offset 0).
  collector_switch_regenerates
                             RED: this layer's regen traces dropped (the
                             plate's text stays). Three of them regenerate
                             it, each alone: `s_collector`, `rotary_on`
                             (the Machine page sets it on a collector
                             change) and the solved m_* vars (the mandrel
                             preview's snapshot writes them).
  tube_edit_regenerates_text RED: the same injection (60808 -> 60808
                             chars: the text stays the plate's).
  handles_go_back            RED: `_MandrelView.release` not restoring
                             (the designer's `_m_cell_canvas` lost).
  collector_row_in_tree      RED: `_gce_top_rows` returning () (no row);
                             `_gce_design_eye` falling through to super
                             (the eye leaves pv_show_collector alone).
  presets_machine_kind       RED: `_gce_panel_presets` ignoring the kind
                             (the Design blurb under Machine).
  tube_tables                the Parameters tables on tubes, one column per
                             scaffold. Comparators: the page vars a cell must
                             write, the argv the worker gets,
                             `scaffold_gcode.tube_layer_blocks`. RED:
                             `_gcp_tube` always False (the plate's table on
                             tubes: no Pattern row).
  tube_tree_rows             each tube's feature rows. RED: `_gce_m_rows`
                             returning [] (no rows).

    xvfb-run -a -s "-screen 0 1920x1200x24" python tests/test_gcode_collector.py

SKIPS with no Tk display. Bounded: every wait has a deadline."""
import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import sys

try:
    import tkinter as tk
except Exception:  # noqa: BLE001
    print("SKIP: tkinter unavailable")
    sys.exit(0)

import test_gcode_editor as E

check, pump, _M = E.check, E.pump, E._M


def _walk(w):
    yield w
    for c in w.winfo_children():
        yield from _walk(c)


def _form():
    app = _M["app"]
    f = getattr(app, "_gce_form", None)
    return f


def _open(key):
    app = _M["app"]
    if getattr(app, "_gce_panel_key", None) == key and _form() is not None:
        app._gce_host_close()
        pump(0.2)
    app._gce_panel_open(key)
    pump(0.3)
    return _form()


def _close():
    app = _M["app"]
    if _form() is not None:
        app._gce_host_close()
    pump(0.3)


def _bound(root, var):
    """The widgets under `root` whose textvariable/variable is `var`."""
    name = str(var)
    out = []
    for w in _walk(root):
        for opt in ("textvariable", "variable"):
            try:
                if str(w.cget(opt)) == name:
                    out.append(w)
            except tk.TclError:
                pass
    return out


def _bar(root):
    import gui_widgets
    return next((w for w in _walk(root)
                 if isinstance(w, gui_widgets.SegmentedBar)), None)


def _set_mode(mode):
    """The collector set as the designer's bar would (no preset load)."""
    app = _M["app"]
    app.s_collector.set(mode)
    pump(0.4)


def _packed(fr):
    try:
        return bool(fr.winfo_manager())
    except tk.TclError:
        return False


# ---------------------------------------------------------------------- #
def ribbon_follows_collector():
    app = _M["app"]
    _set_mode("plate")
    sets = app._gce_c_sets
    b = app._gce_ctools["collector"]
    check("3 axis: plate groups shown", _packed(sets["plate"]))
    check("3 axis: tube groups hidden", not _packed(sets["mandrel"]))
    check("3 axis: tool says so", "Plate (3-axis)" in str(b.cget("text")),
          str(b.cget("text")))
    _set_mode("mandrel")
    check("4 axis: tube groups shown", _packed(sets["mandrel"]))
    check("4 axis: plate groups hidden", not _packed(sets["plate"]))
    check("4 axis: tool says so", "Mandrel (4-axis)" in str(b.cget("text")),
          str(b.cget("text")))
    _set_mode("plate")


def collector_click_loads_defaults():
    app = _M["app"]
    _set_mode("plate")
    calls = []
    real = app._apply_axis_defaults
    app._apply_axis_defaults = lambda mode: (calls.append(mode), real(mode))[1]
    try:
        form = _open("collector")
        bar = _bar(form) if form is not None else None
        check("panel has the 3/4/5 bar", bar is not None)
        if bar is not None:
            bar.invoke("mandrel")
            pump(0.5)
    finally:
        del app._apply_axis_defaults
    check("click loads the 4-axis defaults", calls == ["mandrel"], calls)
    check("collector is 4 axis", app.s_collector.get() == "mandrel",
          app.s_collector.get())
    _close()
    _set_mode("plate")


def plate_dims_are_machine():
    app = _M["app"]
    _set_mode("plate")
    form = _open("collector")
    check("plate X row edits the Machine's plate_x",
          bool(_bound(form, app.plate_x)))
    check("plate thickness row edits plate_thickness",
          bool(_bound(form, app.plate_thickness)))
    _close()


def _page_reset(n=1):
    """The page back to n default tubes, the table closed."""
    import gui_mandrel
    app = _M["app"]
    app._gce_params_close()
    _close()
    app.gce_s_count.set(str(n))
    for i in range(n):
        v = app.gce_scaf_vars[i]
        v["m_layer_overrides"].set("")
        v["m_family"].set(gui_mandrel.MANDREL_FAMILY_CHOICES[0][0])
        v["m_design_mode"].set("turn")
        v["m_angle"].set("40.0")
        v["m_place"].set("left")
        v["m_place_offset"].set("0")
        v["m_start_out"].set("0")
        v["layers"].set("12")
    pump(0.2)


def cell_panel_is_the_scaffolds():
    """Custom cell edits the SELECTED scaffold's drawing: built over it, and
    a later click on the panel still writes it. RED: `_TubeView` returning
    the App's methods unscoped (Clear empties the designer's drawing)."""
    import gui_mandrel
    app = _M["app"]
    _set_mode("mandrel")
    _page_reset(2)
    v = app.gce_scaf_vars[1]
    v["m_family"].set(gui_mandrel.MANDREL_DRAWN_FAMILY)
    with app._gce_tube_scope(1):
        app._mandrel_family_backfill()
    mine, theirs = v["m_cell_graph"], app.m_cell_graph
    was = theirs.get()
    app._gce_m_cell_open(1)
    pump(0.3)
    form = _form()
    check("the cell panel binds scaffold 2's drawing",
          form is not None and bool(_bound(form, mine))
          and not _bound(form, theirs))
    clear = next((w for w in _walk(form) if w.winfo_class() == "TButton"
                  and str(w.cget("text")) == "Clear"), None)
    check("... has Clear", clear is not None)
    if clear is not None:
        clear.invoke()
        pump(0.2)
    check("... and Clear empties scaffold 2's, not the designer's",
          not mine.get().strip() and theirs.get() == was,
          (mine.get()[:30], theirs.get()[:30]))
    _close()
    _page_reset(1)
    _set_mode("plate")


def winding_angle_reaches_argv():
    """Each scaffold's own angle reaches the worker; the designer's is not
    touched."""
    app = _M["app"]
    _set_mode("mandrel")
    _page_reset(1)
    theirs = app.m_angle.get()
    app.gce_scaf_vars[0]["m_angle"].set("33")
    pump(0.2)
    argv, err = _argv()
    check("page can build the tube", not err and argv, err)
    got = argv[argv.index("--winding-angle") + 1] \
        if "--winding-angle" in argv else None
    check("--winding-angle follows scaffold 1's angle", got is not None
          and abs(float(got) - 33.0) < 1e-9, got)
    check("... and the end-turn closure pins the pivots",
          "--tube-pivots" in argv and "--tube-eta" not in argv, argv[-6:])
    check("... and the designer's angle is its own",
          app.m_angle.get() == theirs, app.m_angle.get())
    _page_reset(1)
    _set_mode("plate")


def new_scaffold_goes_beside():
    """New scaffold: a copy of the last, 5 mm beyond its end on the rod,
    and both tubes reach the worker. Comparator: the worker's own run."""
    import json as _json
    import subprocess
    app = _M["app"]
    _set_mode("mandrel")
    _page_reset(1)
    app._gce_m_new_scaffold()
    pump(0.3)
    v = app.gce_scaf_vars
    check("two scaffolds", app._gce_scaffold_count() == 2)
    lays = app._gce_tube_layouts()
    gap = None
    if len(lays) == 2 and None not in lays:
        a, b = lays
        gap = b.clearance[0] - ((a.rod_u1 - a.rod_u0) - a.clearance[1])
    check("... the new one 5 mm of bare rod past the first's print",
          gap is not None and abs(gap - 5.0) < 1e-6, gap)
    argv, err = _argv()
    spec = (_json.loads(argv[argv.index("--tube-scaffolds") + 1])
            if "--tube-scaffolds" in argv else [])
    check("both tubes go to the worker", not err and len(spec) == 2,
          err or spec)
    if not err and argv:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=240)
        check("... which lays them", r.returncode == 0
              and "2 tube(s)" in r.stdout, (r.stdout + r.stderr)[-300:])
    _page_reset(1)
    _set_mode("plate")


def _settled():
    app = _M["app"]
    return app._gce_regen is None and getattr(app, "_gce_d_after", None) \
        is None and bool(app.gce.get_text())


def collector_switch_regenerates():
    """The collector alone (no preset to load) makes the page a tube."""
    app = _M["app"]
    _set_mode("plate")
    pump(90, _settled)
    t0 = app.gce.get_text()
    _set_mode("mandrel")
    pump(1.0)
    pump(90, lambda: _settled() and app.gce.get_text() != t0)
    t1 = app.gce.get_text()
    check("3 -> 4 axis regenerates the page's text", t1 != t0,
          f"{len(t0)} -> {len(t1)} chars")
    check("... into a tube (rotary words)",
          any(" A" in ln for ln in t1.splitlines()[:400]))
    _set_mode("plate")
    pump(1.0)
    pump(90, lambda: _settled() and app.gce.get_text() == t0)
    check("4 -> 3 axis puts the plate's text back",
          app.gce.get_text() == t0)


def tube_edit_regenerates_text():
    """A tube edit on the page writes a new tube into the page's text."""
    app = _M["app"]
    _set_mode("mandrel")
    _page_reset(1)
    v = app.gce_scaf_vars[0]
    pump(90, _settled)
    t0 = app.gce.get_text()
    v["m_angle"].set("55")
    pump(1.0)
    pump(90, lambda: _settled() and app.gce.get_text() != t0)
    t1 = app.gce.get_text()
    check("ω edit regenerates the page's text", t1 != t0 and bool(t1),
          f"{len(t0)} -> {len(t1)} chars")
    _page_reset(1)
    _set_mode("plate")
    pump(90, _settled)


def handles_go_back():
    import gui_mandrel
    app = _M["app"]
    _set_mode("mandrel")
    _page_reset(1)
    app.gce_scaf_vars[0]["m_family"].set(gui_mandrel.MANDREL_DRAWN_FAMILY)
    before = app.__dict__.get("_m_cell_canvas")
    app._gce_m_cell_open(0)
    pump(0.3)
    form = _form()
    lent = app.__dict__.get("_m_cell_canvas")
    check("open: the page draws the cell canvas",
          lent is not None and lent is not before
          and str(lent).startswith(str(form)))
    _close()
    pump(0.3)
    check("closed: the designer's canvas is back",
          app.__dict__.get("_m_cell_canvas") is before)
    check("closed: no panel view left", app._gce_m_view is None)
    dead = [e for e in app._mandrel_family_widgets
            if not (e[0] if isinstance(e, tuple) else e).winfo_exists()]
    check("closed: no dead widgets registered", not dead, len(dead))
    _page_reset(1)
    _set_mode("plate")


def collector_row_in_tree():
    app = _M["app"]
    _set_mode("mandrel")
    app._gce_refresh_anatomy()
    pump(0.3)
    tree = app._gce_tree
    top = list(tree.get_children(""))
    kids = list(tree.get_children(top[0])) if top else []
    rows = top + kids
    check("Collector row under Whole file", "d:collector" in rows, rows[:4])
    if "d:collector" in rows:
        txt = str(tree.item("d:collector", "text")) + " ".join(
            str(v) for v in tree.item("d:collector", "values") or ())
        check("row names the mandrel", "mandrel" in txt, txt)
        app._gce_tree_open("d:collector")
        pump(0.3)
        check("double-click opens Collector",
              app._gce_panel_key == "collector" and _form() is not None)
        _close()
        v0 = bool(app.pv_show_collector.get())
        app._gce_eye_toggle("d:collector")
        pump(0.3)
        check("eye flips the picture's collector",
              bool(app.pv_show_collector.get()) != v0)
        app._gce_eye_toggle("d:collector")
        pump(0.3)
        check("eye back", bool(app.pv_show_collector.get()) == v0)
    _set_mode("plate")


def _argv():
    argv, _sig, err = _M["app"]._gce_design_argv()
    return [str(a) for a in argv or ()], err


def _cell(st, key):
    rows = st["app_rows"]
    gi = next(i for i, r in enumerate(rows) if r[2] == key)
    return {c: st["cells"][(gi, c)] for c in range(st["n"])}


def _type(cell, text):
    app = _M["app"]
    cell["var"].set(text)
    app._gcp_commit(cell)
    pump(0.1)


def tube_tables():
    """The 4-axis Parameters table: one column per scaffold, every Tube
    field a row. Comparators: the page vars a cell must write, the argv the
    worker gets, `scaffold_gcode.tube_layer_blocks`. RED: `_gcp_tube`
    always False (the plate's table on tubes: no Pattern row)."""
    import gui_mandrel
    import scaffold_gcode
    app = _M["app"]
    _set_mode("mandrel")
    _page_reset(2)
    app._gce_tube_place_beside(1)
    app._gcp_view = "scaffold"
    app._gce_params_open()
    pump(0.3)
    st = app._gcp
    st["app_rows"] = app._gcp_rows(st)
    keys = [r[2] for r in st["app_rows"]]
    check("one column per scaffold, of the tube's rows",
          st["tube"] and st["n"] == 2 and "family" in keys
          and "pore" in keys and "r_pore" in keys and "helix_pick" in keys
          and "place_offset" in keys and "copies" not in keys
          and "width" not in keys, (st.get("tube"), st["n"], keys))
    check("... headed by scaffold",
          [str(h.cget("text")) for h in st["heads"]]
          == ["Scaffold 1", "Scaffold 2"])
    fam = _cell(st, "family")
    _type(fam[1], gui_mandrel.MANDREL_FAMILY_VAL2LBL["reentrant"])
    v = app.gce_scaf_vars
    check("picking a pattern writes that scaffold's engine token",
          v[1]["m_family"].get() == "reentrant"
          and v[0]["m_family"].get() == "zigzag",
          (v[0]["m_family"].get(), v[1]["m_family"].get()))
    pump(0.3)
    st["app_rows"] = app._gcp_rows(st)
    check("... its winding angle greys, its cell size frees; the other "
          "column's do not",
          not app._gcp_editable(_cell(st, "winding_angle")[1])
          and app._gcp_editable(_cell(st, "cell_width")[1])
          and app._gcp_editable(_cell(st, "winding_angle")[0])
          and not app._gcp_editable(_cell(st, "cell_width")[0]))
    out = _cell(st, "r_pore")[0]["var"].get()
    check("Resolved pore reads off scaffold 1's design", "(" in out
          and "  " not in out, out)
    pick = _cell(st, "helix_pick")[0]
    rungs = list(pick["w"].cget("values"))
    if len(rungs) > 1:
        _type(pick, rungs[1])
    check("a pore-size rung pins scaffold 1's closure",
          len(rungs) > 1 and bool(v[0]["m_turn_pick"].get().strip()),
          rungs)
    _type(pick, rungs[0] if rungs else "")
    check("... and Auto unpins it", not v[0]["m_turn_pick"].get().strip())
    _type(_cell(st, "rib_angle")[1], "95")
    check("a rib angle of 95 is refused and not written",
          v[1]["m_rib_angle"].get() != "95" and st["err"].get(),
          st["err"].get())
    app._gcp_cell_esc(_cell(st, "rib_angle")[1])
    _type(fam[1], gui_mandrel.MANDREL_FAMILY_VAL2LBL["zigzag"])
    # per layer: layers 5-8 of scaffold 2 re-entrant is three blocks
    app._gce_layer_params_open(1, 4)
    pump(0.3)
    lt = app._gcl
    lt["app_rows"] = app._gcp_rows(lt)
    check("the layer table is scaffold 2's, a column a layer",
          lt["tube"] and lt["s"] == 1 and lt["n"] == 12 and lt["hi"] == 4
          and [r[2] for r in lt["app_rows"]]
          == list(scaffold_gcode.TUBE_LAYER_KEYS), (lt["n"], lt["hi"]))
    check("... layer 1 has no step below it to set",
          not app._gcp_editable(_cell(lt, "step")[0])
          and app._gcp_editable(_cell(lt, "step")[1]))
    fams = _cell(lt, "family")
    for k in range(4, 8):
        _type(fams[k], gui_mandrel.MANDREL_FAMILY_VAL2LBL["reentrant"])
    ov = app._gce_layer_overrides(1)
    check("each layer set writes scaffold 2's per-layer field",
          sorted(ov) == [4, 5, 6, 7]
          and all(r == {"family": "reentrant"} for r in ov.values())
          and not app._gce_layer_overrides(0), ov)
    import json as _json
    argv, err = _argv()
    spec = (_json.loads(argv[argv.index("--tube-scaffolds") + 1])
            if "--tube-scaffolds" in argv else [])
    blocks = (_json.loads(spec[1]["tube_blocks"])
              if len(spec) == 2 and spec[1].get("tube_blocks") else [])
    check("the worker gets scaffold 2's three blocks",
          not err and [e.get("family", "zigzag") for e in blocks]
          == ["zigzag", "reentrant", "zigzag"], err or spec)
    pore = _cell(lt, "pore")
    _type(pore[1], "300")
    with app._gce_tube_scope(1):
        ents = app._mandrel_tube_entries() or []
    check("a pore on one winding layer reaches the worker as pivots",
          any("tube_pivots" in e and "pore" not in e for e in ents), ents)
    _page_reset(1)
    _set_mode("plate")


def tube_tree_rows():
    """Each tube's feature rows sit under its own scaffold row, and a layer
    that differs is said ONCE, inside its own branch, as on the plate.

    Reported: a tube's layer blocks were rows under the
    scaffold too ("Layer 2 · Re-entrant hone"), so every layer was listed
    twice. RED against 1862f24: `no layer is listed twice` fails on
    `d:m1:blk4_5`."""
    app = _M["app"]
    _set_mode("mandrel")
    _page_reset(2)
    app._gce_tube_place_beside(1)
    app.gce_scaf_vars[1]["m_layer_overrides"].set(
        '{"5": {"family": "reentrant"}, "6": {"family": "reentrant"}}')
    pump(1.0)
    pump(120, _settled)
    app._gce_refresh_anatomy()
    pump(0.3)
    tree = app._gce_tree

    def rows():
        out = {}

        def walk(x):
            for c in tree.get_children(x):
                out[c] = str(tree.item(c, "text"))
                walk(c)
        walk("")
        return out
    r = rows()
    groups = [g for g in r if g.startswith("n:g:")]
    check("two scaffolds are two groups", len(groups) == 2, groups)
    check("... named Scaffold 1 and Scaffold 2", len(groups) == 2
          and "Scaffold 2" in r[groups[1]] and "copy" not in r[groups[1]],
          [r[g] for g in groups])
    f2 = {k: v for k, v in r.items() if k.startswith("d:m1:")}
    top = [k for k in f2 if len(groups) == 2 and tree.parent(k) == groups[1]]
    check("scaffold 2's own rows: its pattern only", top == ["d:m1:pattern"],
          {k: f2[k] for k in top})
    check("no layer is listed twice",
          not any(":blk" in k for k in r), [k for k in r if ":blk" in k])
    lrows = {k: v for k, v in f2.items() if ":L" in k}
    check("layers 5 and 6 each carry one Layer parameters row, in their "
          "own branch", sorted(lrows) == ["d:m1:L4", "d:m1:L5"]
          and all(v == "Layer parameters" for v in lrows.values())
          and all(tree.parent(k).startswith("n:l:")
                  and tree.parent(k).split(":")[4] == k.split("L")[-1]
                  for k in lrows), {k: (v, tree.parent(k))
                                    for k, v in lrows.items()})
    check("scaffold 1 has its pattern and nothing more",
          "d:m0:pattern" in r and not any(
              k.startswith("d:m0:") and k != "d:m0:pattern" for k in r))
    check("no Advanced or Copies row",
          not any(k.endswith((":advanced", ":copies")) for k in r))
    app._gcp_view = "scaffold"             # the table opens in its last view
    app._gce_tree_open(groups[1])
    pump(0.3)
    check("double-click scaffold 2 opens the table on its column",
          app._gce_params_shown() and app._gcp["hi"] == 1)
    app._gce_params_close()
    lay = next((x for x in tree.get_children(groups[1])
                if x.startswith("n:l:") and x.split(":")[4] == "5"), None)
    if lay is not None:
        app._gce_tree_open(lay)
        pump(0.3)
    check("double-click its layer 6 opens that layer",
          lay is not None and app._gce_params_shown()
          and app._gcl["s"] == 1 and app._gcl["hi"] == 5)
    app._gce_params_close()
    _page_reset(1)
    _set_mode("plate")


def presets_machine_kind():
    app = _M["app"]
    blurbs = {k: b for k, _l, b in app._GCE_PRESET_KINDS}
    app._gce_preset_kind().set("machine")
    form = _open("presets")
    texts = [str(w.cget("text")) for w in _walk(form)
             if isinstance(w, (tk.Label,)) or w.winfo_class() == "TLabel"]
    check("Machine kind shows the machine presets",
          any(blurbs["machine"][:30] in t for t in texts))
    check("... not the design's",
          not any(blurbs["scaffold"][:30] in t for t in texts))
    _close()
    app._gce_preset_kind().set("scaffold")


def main():
    try:
        root = tk.Tk()
        root.destroy()
    except tk.TclError:
        print("SKIP: no display")
        return 0
    if not E.boot():
        print("FAIL  boot")
        return 1
    for case in (ribbon_follows_collector, collector_click_loads_defaults,
                 plate_dims_are_machine,
                 cell_panel_is_the_scaffolds, winding_angle_reaches_argv,
                 new_scaffold_goes_beside,
                 collector_switch_regenerates,
                 tube_edit_regenerates_text, handles_go_back, collector_row_in_tree,
                 presets_machine_kind, tube_tables, tube_tree_rows):
        print(f"[{case.__name__}]")
        try:
            case()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(f"{case.__name__} raised", False, repr(exc))
    app, root = _M["app"], _M["root"]
    for aid in root.tk.call("after", "info"):
        try:
            root.tk.call("after", "cancel", aid)
        except tk.TclError:
            pass
    app._gce_stop_regen()
    root.destroy()
    fails = E._FAILS
    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
