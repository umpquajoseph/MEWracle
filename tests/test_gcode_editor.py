#!/usr/bin/env python3
"""test_gcode_editor.py -- the modern G-code page (gui_gcode_editor +
gui_gcode_changes) driven as a user would, in a real withdrawn Tk app.

Comparators (none of them written by this page):
  * the engine's own render, `mew_gcode.Document.render()`, for what the page
    shows and Save writes;
  * the shared Save seam, `_gcode_for_display_export`, for a linked text with
    no chips (byte for byte);
  * invariants: all chips off -> the designer's output; an output-option flip
    adds no chip; Undo of a chip change restores the chip set; a preset load
    puts the chips back.

Cases, each with its RED proof (the defect injected in memory by a probe script that does not ship;
the case seen failing on target; the clean run is the GREEN control):
  linked_text_is_save_text   RED: `_gce_options` returning all-off options
                             (60883 vs 60808 chars). `_gce_render_design`
                             returning raw does NOT trip it: with no chips the
                             engine render already equals the seam's.
  highlight_lights_the_line  RED: `_gce_hl_geometry` reading line n+3
                             (11 mm off; clean: 0.0000 mm).
                             RED: the pre-2026-10-26 lookup, one merged
                             time sort cut wherever the fiber changed
                             (fiber 0 lit only to x=9.7; clean: 8..10, one
                             piece each). Seen as dashes along a 4-axis ply.
  option_flip_adds_no_chip   RED: `_gce_record_edits` using
                             `_gce_model_sync()` (the 2026-10-23 defect: 12
                             bogus chips per flip).
  insert_form_anchors        RED: `anchor_at(line)` -> `anchor_at(0)`.
  form_session_one_step      RED: `end_group` not folding the session (two
                             undos needed). Cancel: `m.restore` skipped.
  edit_lives_in_anatomy      RED: `_gce_home` returning "n:file" always.
  typed_line_places_edit     RED: `Document.move_to_line` returning before
                             the move (2026-10-24; replaced the Up/Down case
                             when those buttons went).
  typing_is_never_lost       RED: `_gce_edit_finish` not recording the
                             typing (the line gone after Done); `_same_text`
                             as plain equality (the last-line typing never
                             adopted). Both seen on screen 2026-10-24.
  stitch_is_an_edit          RED: the first-to-last-move trim skipped (G28
                             and M104 come through).
  library_edit_is_the_form   RED: `_gce_edit_recipe` always adding a new copy
                             (2026-10-24; the saved row used to open its own
                             panel).
  scale_from_the_form        RED: `_apply_transform` ignoring "scale".
  typed_edit_opens_in_form   RED: `_gce_edit_finish` leaving `typed` set.
  chips_off_is_designer      RED: `_gce_toggle_chip` not calling toggle.
  undo_restores_chips        RED: `_gce_changed` not attaching the snapshots.
  generate_carries_doc       RED: `_build_args_scaffold` override returning
                             the bare argv. Its badge check was RED against
                             the first build, which also counted the Export
                             card's own option rows (5 vs 3).
  generate_carries_unrecorded_typing
                             RED: the "has changes" test run before the
                             pending typing is recorded.
  regen_has_no_doc           RED: `_gce_regen_building` never set.
  preset_round_trip          RED: `_gce_persist_entries` returning {}.
  flavor_follows_machine     RED: `_gce_follow_machine` a no-op.
  esc_closes_form            RED: the form's Esc binding without "break";
                             Esc is Cancel, so the new edit goes too.
  save_writes_page_text      RED: `save_gcode_as` writing the parse form.
  cad_page_stands_alone      RED: `_gce_full_window` a no-op (the sidebar
                             stays); `_GCE_TYPING` empty (G typed into the
                             text toggles the overlay). 2026-10-25.
  tool_speed_on_layer        RED: `_gce_node_scope` giving the whole file
                             (the one-layer checks); the short form off
                             (Type/What/Where showing).
  tool_insert_after_layer    RED: `_gce_node_anchor` giving the header;
                             `_gce_retarget` a no-op.
  text_selection_is_selection RED: `_gce_sel_anchor` going after the FIRST
                             selected line.
  delete_tool_removes_lines  RED: `_gce_record_edits` skipped (the text and
                             the document part).
  tree_x_removes_edit        RED: the release's ✕-column branch removed.
  tree_is_bars_over_picture  RED not yet shown.
  repeat_row_and_its_undo    RED: places keyed by `chip` alone (0 copies);
                             `_gce_on_step` without its redraw (the row stays
                             gone); `_gce_layout` ignoring the widest bar.

    python tests/test_gcode_editor.py

SKIPS with no Tk display. Bounded: every wait has a deadline."""
import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import os
import sys
import tempfile
import time

try:
    import tkinter as tk
except Exception:  # noqa: BLE001
    print("SKIP: tkinter unavailable")
    sys.exit(0)

_FAILS = []
_M = {}


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        _FAILS.append(name)


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
    root.withdraw()
    gui_fonts.init_fonts(root)
    app = mewracle_gui.ModernApp(root)
    root.update()
    _M.update(root=root, app=app)
    app._nav_select("gcode")
    ok = pump(60, lambda: bool(app.gce.get_text())
              and app._gce_regen is None
              and getattr(app, "_gce_model", None) is not None)
    return ok


def _show(focus=False):
    """Map the window for a case that needs real geometry or key focus,
    transparent and off to the side, so a run never flashes over the desktop.
    The foreground goes straight back to whatever had it, unless the case
    needs the keyboard (`focus`); then `_hide` gives it back."""
    root = _M["root"]
    fg = None
    if sys.platform == "win32":
        import ctypes
        fg = ctypes.windll.user32.GetForegroundWindow()
    _M["fg"] = fg
    root.attributes("-alpha", 0.0)
    root.geometry("1400x900+-12000+0")
    root.deiconify()
    root.update()
    if not focus:
        _give_back()


def _give_back():
    fg = _M.pop("fg", None)
    if fg:
        import ctypes
        ctypes.windll.user32.SetForegroundWindow(fg)


def _hide():
    _M["root"].withdraw()
    _give_back()


def _chips(app):
    return [lab for _c, lab in app._gce_model.chips()]


def linked_text_is_save_text():
    app = _M["app"]
    m = app._gce_model
    text = app.gce.get_text()
    check("linked: the page shows the document's render",
          text == m.render().text)
    saved = (app._last_gcode_is_scaffold, app._last_gcode_is_calibration)
    app._last_gcode_is_scaffold, app._last_gcode_is_calibration = True, False
    try:
        with _CommentsKept():
            seam = app._gcode_for_display_export(m.base_text)
    finally:
        app._last_gcode_is_scaffold, app._last_gcode_is_calibration = saved
    check("linked, no chips: the text is the Save seam's, byte for byte",
          text == seam, f"{len(text)} vs {len(seam)} chars")


def highlight_lights_the_line():
    """The caret's line in the picture. Comparator: the player's own parse
    (`gcode_player.parse_path`) of the text. With physics and sag off the
    lit fiber of a move IS its commanded segment; a line that does not move
    is one point."""
    import numpy as np
    app = _M["app"]
    orig = app._pv_snapshot_options
    app._pv_snapshot_options = lambda i: dict(orig(i), physics=None,
                                              sag=False)
    try:
        old = getattr(app, "_gce_hl", None)
        app._gce_pv_schedule(now=True)
        pump(90, lambda: getattr(app, "_gce_hl", None) is not old)
        hl = app._gce_hl
        check("the picture brings a line clock and point times",
              hl["clock"] is not None and hl["T"] is not None)
        c = hl["clock"]
        worst, n = 0.0, 0
        for line in range(5, len(app.gce.get_text().split("\n")), 9):
            j = np.flatnonzero(c["seg_line"] == line)
            if len(j) != 1:
                continue
            a, b = c["verts"][j[0]][:2], c["verts"][j[0] + 1][:2]
            for p in app._gce_hl_geometry(hl, [line])[0]:
                q, ab = p[:, :2], b - a
                s = np.clip(((q - a) @ ab) / max(ab @ ab, 1e-12), 0, 1)
                worst = max(worst, float(np.linalg.norm(
                    q - (a + s[:, None] * ab), axis=1).max()))
                n += 1
        check("a move lights its own commanded segment (physics, sag off)",
              n > 50 and worst < 1e-6, f"{n} lines, worst {worst:.4f} mm")
        polys, dots = app._gce_hl_geometry(hl, [2])
        check("a line that does not move is one point",
              not polys and len(dots) == 1, f"{len(polys)}, {len(dots)}")
    finally:
        del app._pv_snapshot_options


def option_flip_adds_no_chip():
    app = _M["app"]
    n0 = len(app._gce_model.changes)
    was = app.s_travel_as_g1.get()
    app.s_travel_as_g1.set(not was)
    pump(1.0)
    n1 = len(app._gce_model.changes)
    g0 = "\nG0 " in app.gce.get_text()
    app.s_travel_as_g1.set(was)
    pump(1.0)
    check("an output-option flip adds no chip", n1 == n0 and
          len(app._gce_model.changes) == n0, f"{n0} -> {n1}")
    check("the flip reaches the text (travel off shows G0)",
          g0 == was, f"G0 present={g0} with travel_as_g1 {not was}")


def _button(app, text):
    return [w for w in app._gce_walk(app._gce_form)
            if isinstance(w, tk.ttk.Button) and w["text"] == text][0]


def _field(app, label):
    """The form's entry on the row labelled `label` (showing)."""
    for w in app._gce_walk(app._gce_form):
        if isinstance(w, tk.ttk.Label) and w.winfo_manager() == "grid" \
                and str(w.cget("text")).startswith(label):
            row = int(w.grid_info()["row"])
            for e in w.master.grid_slaves(row=row):
                if isinstance(e, tk.ttk.Entry) \
                        and not isinstance(e, tk.ttk.Combobox):
                    return e
    raise LookupError(label)


def _set(entry, value):
    entry.delete(0, "end")
    entry.insert(0, value)


def _header_len(app):
    """Lines of the machine start block heading the page's text (Setup's
    header and start g-code). Fixed line numbers below count from it, so a
    longer or shorter header leaves them on the same g-code."""
    return sum(1 for o in app._gce_model.render().lines
               if o.chip == "machine:start")


class _CommentsKept(object):
    """Setup's "Strip comments" off for a case that compares Save with the
    page byte for byte. It is off by default; a printer that ticks it has
    every comment line, the machine header included, taken out of what Save
    writes while the page keeps showing them: that is the setting doing its
    job."""

    def __enter__(self):
        self.var = _M["app"].machine_strip_comments
        self.was = self.var.get()
        self.var.set(False)
        pump(0.2)

    def __exit__(self, *_exc):
        self.var.set(self.was)
        pump(0.2)


def insert_form_anchors():
    app = _M["app"]
    m = app._gce_model
    h = _header_len(app)
    app.gce.goto_line(12 + h)
    pump(0.3)
    app._gce_new_edit()
    pump(0.2)
    check("+ New edit opens the form with the edit already in the text",
          app._gce_form is not None and any(
              ln.startswith("G4") for ln in
              app.gce.get_text().split("\n")[11 + h:14 + h]))
    _set(_field(app, "Time"), "3")
    pump(0.6)
    box = app._gce_fs["vals"]["box"].get("1.0", "end-1c")
    check("the g-code box follows the field", "3" in box, repr(box))
    _button(app, "Done").invoke()
    pump(0.5)
    lines = app.gce.get_text().split("\n")
    check("Dwell at the caret: lands after the caret's line",
          lines[12 + h].startswith("G4") and "3" in lines[12 + h],
          repr(lines[11 + h:14 + h]))
    check("…as one edit, anchored in the structure",
          any("G4" in lab for lab in _chips(app)), str(_chips(app)))
    check("the form closed and the tree's panel is back",
          app._gce_form is None
          and app._gce_changes.winfo_manager() == "grid")
    _M["dwell_id"] = m.changes[-1].id


def form_session_one_step():
    app = _M["app"]
    m = app._gce_model
    t0, n0 = app.gce.get_text(), len(m.changes)
    app.gce.goto_line(20)
    app._gce_new_edit()
    pump(0.2)
    for v in ("4", "5", "6"):
        _set(_field(app, "Time"), v)
        pump(0.5)
    _button(app, "Done").invoke()
    pump(0.3)
    t1 = app.gce.get_text()
    app.gce.undo()
    pump(0.5)
    check("a whole form session is ONE undo step",
          app.gce.get_text() == t0 and len(m.changes) == n0,
          f"{len(m.changes)} edits vs {n0}")
    app.gce.redo()
    pump(0.5)
    check("…and one redo", app.gce.get_text() == t1)
    app.gce.goto_line(20)
    app._gce_new_edit()
    pump(0.2)
    _set(_field(app, "Time"), "9")
    pump(0.5)
    _button(app, "Cancel").invoke()
    pump(0.3)
    check("Cancel puts the text and the edits back",
          app.gce.get_text() == t1 and len(m.changes) == n0 + 1)


def edit_lives_in_anatomy():
    app = _M["app"]
    cid = _M["dwell_id"]
    app._gce_render_changes()
    tree = app._gce_tree
    iid = "e:" + cid
    par = tree.parent(iid) if tree.exists(iid) else ""
    check("the edit has a row in the Anatomy", tree.exists(iid))
    check("…under a part of the print, not the whole file",
          par.startswith(("n:l:", "n:g:", "n:header")), par)
    check("…and its row names the edit",
          "G4" in tree.item(iid, "text") or "Dwell" in tree.item(iid, "text")
          if tree.exists(iid) else False,
          tree.item(iid, "text") if tree.exists(iid) else "")


def typed_line_places_edit():
    app = _M["app"]
    cid = _M["dwell_id"]

    def at():
        return [i for i, rl in enumerate(app._gce_render().lines, start=1)
                if rl.chip == cid][0]
    a0 = at()
    app._gce_open_edit(cid)
    pump(0.2)
    le = app._gce_fs["vals"]["line_entry"]
    check("the Line box shows the line the edit is on",
          le.get() == str(a0), f"{le.get()!r} vs {a0}")
    _set(le, str(a0 + 40))
    # a withdrawn window takes no key events (see esc_closes_form)
    root = _M["root"]
    _show(focus=True)
    le.focus_force()
    pump(0.3, lambda: root.focus_get() is le)
    le.event_generate("<Return>")
    pump(0.3)
    _hide()
    a1 = at()
    check("Enter in the Line box moves it there, the form stays open",
          a1 == a0 + 40 and app._gce_form is not None, f"{a0} -> {a1}")
    _set(le, str(a0))
    _button(app, "Done").invoke()
    pump(0.3)
    check("a typed line is taken by Done without Enter", at() == a0,
          f"{a1} -> {at()}")


def library_edit_is_the_form():
    """A saved edit's Edit button in My library opens the My library window
    on that script (one script library, b98e37b8). The edit in this file
    opens the ONE edit form, with the library's choices in it."""
    app = _M["app"]
    m = app._gce_model
    cid = _M["dwell_id"]
    was_cache = getattr(app, "_gce_lib_cache", None)
    lib = os.path.join(tempfile.mkdtemp(), "recipes.json")
    app._gce_recipes_path = lambda: lib     # never the user's own library
    app._gce_lib_cache = None
    try:
        app._gce_save_id(cid)
        pump(0.3)
        name = m.get(cid).saved
        rows = [w for w in app._gce_walk(app._gce_saved_box)
                if isinstance(w, tk.ttk.Button) and w["text"] == "Edit"]
        check("a saved row has an Edit button", len(rows) == 1, len(rows))
        rows[0].invoke()
        pump(0.3)
        win = getattr(app, "_script_lib", None)
        check("…which opens My library on that script",
              win is not None and win.alive() and win.current == name,
              repr(getattr(win, "current", None)))
        if win is not None and win.alive():
            win.close(force=True)
            pump(0.2)
        app._gce_open_edit(cid)
        pump(0.3)
        fs = app._gce_fs
        check("the edit in this file opens the edit form",
              fs is not None and fs["cid"] == cid)
        fs["vals"]["auto"].set(True)
        app._gce_edit_lib_set(use_on_new=True)
        pump(0.3)
        rec = [r for r in app._gce_library() if r.name == name]
        check("every-new-print is set from the form",
              rec and rec[0].use_on_new)
        # A library write re-takes the design so automatic scripts show
        # (gui_scripts._scripts_changed), which closes an open form with its
        # edits kept: open it again for its next control.
        if app._gce_fs is None:
            app._gce_open_edit(cid)
            pump(0.3)
        _button(app, "Remove from library").invoke()
        _button(app, "Sure? Remove it").invoke()
        pump(0.3)
        check("Remove from library keeps the edit in this file",
              not [r for r in app._gce_library() if r.name == name]
              and m.get(cid).saved == "")
        if app._gce_fs is not None:
            _button(app, "Done").invoke()
            pump(0.2)
        # the rows' own buttons: Remove (twice) leaves the use unsaved
        app._gce_save_id(cid)
        pump(0.3)
        rm = [w for w in app._gce_walk(app._gce_saved_box)
              if isinstance(w, tk.ttk.Button) and w["text"] == "Remove"][0]
        rm.invoke()
        rm.invoke()
        pump(0.3)
        check("a saved row's Remove leaves its use here, unsaved",
              not [r for r in app._gce_library() if r.name == name]
              and m.get(cid).saved == "" and cid in app._gce_lib_rows)
    finally:
        # never leave this case's script behind: an automatic one left in
        # the scratch library would put its lines into every later case
        try:
            app._gce_write_library([r for r in app._gce_library()
                                    if r.builtin or not r.name.startswith(
                                        "Dwell")])
        except Exception:  # noqa: BLE001 -- cleanup, the checks above report
            pass
        del app._gce_recipes_path
        app._gce_lib_cache = was_cache


def typing_is_never_lost():
    """A line typed in the text is an edit: typed while an edit's form is
    open (Done re-renders the text), or typed on the empty last line (the
    render ends the file with a newline the typing lacks)."""
    app = _M["app"]
    m = app._gce_model
    t = app.gce.text
    app.gce.goto_line(20)
    app._gce_new_edit()
    pump(0.3)
    t.insert("40.0", "M118 FORM\n")
    _button(app, "Done").invoke()
    pump(0.3)
    check("typed with a form open: still there after Done, as an edit",
          "M118 FORM" in app.gce.get_text() and any(
              "M118 FORM" in (getattr(c, "lines", None) or [""])
              for c in m.changes))
    t.insert("end-1c", "M118 EOF")
    res = app._gce_pv_source()()
    adopt = res[1] if isinstance(res, tuple) else None
    check("typed on the empty last line: the preview's pass records it",
          adopt is not None and any(
              "M118 EOF" in (getattr(c, "lines", None) or [""])
              for c in adopt[2]))
    if adopt is not None:
        app._gce_pv_adopt(adopt)
    app._gce_record_edits()
    for c in list(m.changes):       # leave no typed edit for later cases
        if {"M118 FORM", "M118 EOF"} & set(getattr(c, "lines", None) or ()):
            app._gce_delete_id(c.id)
    pump(0.3)


def stitch_is_an_edit():
    """Stitch g-code > A g-code file: an edit of your own g-code at the
    caret, its first move to its last, no comments -- no homing or heating
    from the file's own frame mid-print."""
    import gui_gcode_changes
    app = _M["app"]
    m = app._gce_model
    f = os.path.join(tempfile.mkdtemp(), "part.gcode")
    with open(f, "w") as fh:
        fh.write("; a slicer\nG28\nM104 S200 ; heat\nG90\nG1 Z0.2 F600\n"
                 "G1 X1 Y1 ; first\nG1 X2 Y1\nM104 S0\nG28\nM84\n")
    was = gui_gcode_changes.filedialog.askopenfilename
    gui_gcode_changes.filedialog.askopenfilename = lambda **_k: f
    try:
        app.gce.goto_line(30)
        app._gce_stitch_edit("file")
        pump(0.3)
    finally:
        gui_gcode_changes.filedialog.askopenfilename = was
    fs = app._gce_fs
    c = m.get(fs["cid"]) if fs else None
    check("a stitched file opens in the edit form as your own g-code",
          c is not None and c.what == "custom")
    # ...then back to the height the print had at the caret (here Z0), so
    # the print after it does not carry on at the file's last height
    check("…first move to last, no comments, then back to the print's height",
          c is not None and c.lines == ["G1 Z0.2 F600", "G1 X1 Y1",
                                        "G1 X2 Y1", "G0 Z0"], c and c.lines)
    _button(app, "Cancel").invoke()
    pump(0.2)


def scale_from_the_form():
    app = _M["app"]
    m = app._gce_model
    import mew_gcode as mg
    before = [mg.parse_words(rl.text) for rl in app._gce_render().lines]
    app._gce_new_edit()
    pump(0.2)
    vals = app._gce_fs["vals"]
    vals["type"].set(app._GCE_CHANGE)
    pump(0.1)
    vals["what"].set(dict(mg.TRANSFORM_KINDS)["scale"])
    pump(0.1)
    _set(_field(app, "X"), "120")
    pump(0.6)
    _button(app, "Done").invoke()
    pump(0.3)
    c = m.changes[-1]
    check("Change existing lines, Scale: a transform edit",
          isinstance(c, mg.Transform) and c.op == "scale"
          and float(c.params.get("px")) == 120.0, repr(c))
    after = [mg.parse_words(rl.text) for rl in app._gce_render().lines]
    hit = [(a[1], b[1]) for a, b in zip(before, after)
           if mg.canon_cmd(a[0]) in ("G0", "G1") and "X" in a[1]
           and "X" in b[1]]
    ok = bool(hit) and all(
        abs(float(b["X"]) - float(a["X"]) * 1.2) < 2e-3 for a, b in hit)
    check("…every X of a move is 120 % of the original", ok,
          repr(hit[:2]))
    b = m.snapshot()
    m.remove(c.id)
    app._gce_rerender(b)
    pump(0.3)


def typed_edit_opens_in_form():
    app = _M["app"]
    m = app._gce_model
    # past the machine start block that heads the page (6ce07407)
    ln = 30 + _header_len(app)
    app.gce.goto_line(ln)
    app.gce.text.insert("%d.0 lineend" % ln, "\nM117 hello")
    pump(0.1)
    app._gce_record_edits()
    app._gce_render_changes()
    typed = [c for c in m.changes if c.typed]
    check("typing makes a typed edit", len(typed) == 1, str(typed))
    if not typed:
        return
    cid = typed[0].id
    check("…shown in the Anatomy as typed",
          app._gce_tree.exists("e:" + cid)
          and "(typed)" in app._gce_tree.item("e:" + cid, "text"))
    app._gce_open_edit(cid)
    pump(0.2)
    box = app._gce_fs["vals"]["box"].get("1.0", "end-1c")
    check("…opens in the same form, its g-code in the box",
          "M117 hello" in box, repr(box))
    vals = app._gce_fs["vals"]
    # the whole form, always: no short form, no More options
    hidden = [k for k in ("name_row", "type_row", "what_row", "where_row")
              if not _row_shown(vals[k])]
    check("…in the whole form (Name, Type, What, Where)", not hidden,
          str(hidden))
    _button(app, "Keep").invoke()
    pump(0.3)
    check("Keep: no longer a typed edit", not m.get(cid).typed)
    b = m.snapshot()
    m.remove(cid)
    app._gce_rerender(b)
    pump(0.3)


def chips_off_is_designer():
    app = _M["app"]
    m = app._gce_model
    import mew_gcode as mg
    b = m.snapshot()
    m.add(mg.Rule(text="M117 L[[layer]]", where="before_layer", name="msg"))
    app._gce_rerender(b)
    pump(0.4)
    plain = mg.Document(m.base_text, m.line_map, m.flavor, m.options,
                        source="scaffold").render().text
    for c in list(m.changes):
        var = tk.BooleanVar(value=False)
        app._gce_toggle_chip(c, var)
    pump(0.4)
    check("all chips off -> the designer's output",
          app.gce.get_text() == plain)
    for c in list(m.changes):
        app._gce_toggle_chip(c, tk.BooleanVar(value=True))
    pump(0.4)
    check("ticked again -> they are back",
          app.gce.get_text() == m.render().text
          and app.gce.get_text() != plain)


def undo_restores_chips():
    app = _M["app"]
    n0 = len(app._gce_model.changes)
    t0 = app.gce.get_text()
    import mew_gcode as mg
    b = app._gce_model.snapshot()
    app._gce_model.add(mg.Transform(op="speed_scale",
                                    params={"percent": 50.0}))
    app._gce_rerender(b)
    pump(0.4)
    n1 = len(app._gce_model.changes)
    app.gce.undo()
    pump(0.8)
    check("Undo of a chip change restores the chip set and the text",
          len(app._gce_model.changes) == n0 and app.gce.get_text() == t0,
          f"{n0} -> {n1} -> {len(app._gce_model.changes)}")
    app.gce.redo()
    pump(0.8)
    check("Redo puts it back", len(app._gce_model.changes) == n1)


def generate_carries_doc():
    """Two designs (gui_gcode_twodesigns): the page's own Generate -- the
    designer's builder run inside the page's scope -- carries the page's
    chips; the designer's Generate writes the designer's design, bare."""
    app = _M["app"]
    bare = app._build_args_scaffold()
    check("the designer's Generate carries no --gcode-doc",
          "--gcode-doc" not in bare, str(bare[-4:]))
    with app._gce_page_scope():
        argv = app._build_args_scaffold()
    ok = "--gcode-doc" in argv
    check("the page's Generate carries --gcode-doc", ok, str(argv[-4:]))
    if ok:
        import mew_gcode as mg
        d = mg.Document.load(argv[argv.index("--gcode-doc") + 1])
        check("…holding this page's chips",
              len(d.changes) == len(app._gce_model.changes))
    m = app._gce_model
    page = (sum(1 for c in m.changes if c.on)
            + sum(1 for p in m.pieces if p.on and p.source != "designer"))
    check("the Export badge counts the page's changes, not the Export "
          "card's own options", app._gce_badge_count() == page,
          f"{app._gce_badge_count()} vs {page}")


def generate_carries_unrecorded_typing():
    """A line typed with no chips yet, and Generate pressed before anything
    recorded it: the page's Generate still carries it. Reported: an added
    move never reached the locked simulation, which played the designer's
    file. The typing was unrecorded (the preview does not adopt typing the
    render re-spells), so the "has changes" test ran before the record."""
    app = _M["app"]
    m = app._gce_model
    snap = m.snapshot()
    b = app._gce_snap(record=False)
    for c in list(m.changes):
        m.remove(c.id)
    app._gce_rerender(b)
    pump(0.4)
    try:
        lines = app.gce.get_text().splitlines()
        i = next(k for k, s in enumerate(lines) if s.startswith("G1 X"))
        lines.insert(i + 1, "g1 x100")
        app.gce.set_text("\n".join(lines) + "\n")
        with app._gce_page_scope():
            argv = app._build_args_scaffold()
        ok = "--gcode-doc" in argv
        check("Generate carries typing nothing has recorded yet", ok,
              str(argv[-4:]))
        if ok:
            import mew_gcode as mg
            d = mg.Document.load(argv[argv.index("--gcode-doc") + 1])
            check("…and the document holds that edit", len(d.changes) == 1,
                  f"{len(d.changes)} changes")
    finally:
        m.restore(snap)
        app._gce_rerender(app._gce_snap(record=False))
        pump(0.4)


def regen_has_no_doc():
    app = _M["app"]
    argv, _sig, err = app._gce_design_argv()
    check("the page's own regeneration never carries the document",
          err is None and "--gcode-doc" not in argv and "--line-map" in argv,
          str(err or argv[-4:]))


def preset_round_trip():
    app = _M["app"]
    keys = app._keys_for_kind("scaffold")
    check("gce.document rides the scaffold preset", "gce.document" in keys)
    snap = app._snapshot_for_kind("scaffold")
    n = len(app._gce_model.changes)
    text = app.gce.get_text()
    app._apply_preset_for_kind("scaffold", dict(snap, **{"gce.document": ""}))
    pump(20, lambda: app._gce_regen is None
         and getattr(app, "_gce_model", None) is not None)
    pump(0.5)
    n_empty = len(app._gce_model.changes) if app._gce_model else -1
    app._apply_preset_for_kind("scaffold", snap)
    pump(20, lambda: app._gce_regen is None
         and getattr(app, "_gce_model", None) is not None
         and getattr(app, "_gce_restore", None) is None)
    pump(0.5)
    check("a preset without changes clears them", n_empty == 0,
          str(n_empty))
    # the header's "; Date:" line is the clock at render time (mew_gcode's
    # header template), so a minute can turn over between the two texts
    def _undated(t):
        return [ln for ln in t.split("\n") if not ln.startswith("; Date:")]
    was, now = _undated(text), _undated(app.gce.get_text())
    check("loading the preset puts the chips back",
          len(app._gce_model.changes) == n and now == was,
          f"{n} -> {len(app._gce_model.changes)}, "
          f"{[(a, b) for a, b in zip(was, now) if a != b][:3]}"
          f" lines {len(was)} -> {len(now)}")


def flavor_follows_machine():
    app = _M["app"]
    app._gce_flavor_reset()
    app.gcode_flavor.set("Marlin")
    pump(0.3)
    check("a document follows the Machine page's flavor",
          app.gce_flavor.get() == "Marlin"
          and app._gce_model.flavor == "marlin")
    app.gce_flavor.set("RepRapFirmware (Duet)")
    app._gce_flavor_selected()
    pump(0.3)
    app.gcode_flavor.set("GRBL")
    pump(0.3)
    check("a flavor picked on the page stays when the machine's changes",
          app._gce_model.flavor == "duet")
    check("the dwell re-spells in Duet (G4 S)",
          any(ln.startswith("G4 S") for ln in app.gce.get_text().split("\n")))
    app.gcode_flavor.set("Klipper (MEWron)")
    app._gce_flavor_reset()
    app._gce_flavor_changed()
    pump(0.3)
    check("Machine page: origin.gcode_flavor is a machine key",
          "origin.gcode_flavor" in app._keys_for_kind("machine"))


def esc_closes_form():
    app = _M["app"]
    n0 = len(app._gce_model.changes)
    app._gce_new_edit()
    pump(0.2)
    form = app._gce_form
    first = [w for w in app._gce_walk(form)
             if isinstance(w, (tk.Text, tk.ttk.Entry)) and w.winfo_manager()][0]
    # Tk redirects a generated key event to the focus widget, and a
    # withdrawn window never takes focus: deiconify for this one case
    root = _M["root"]
    _show(focus=True)
    first.focus_force()
    pump(0.3, lambda: root.focus_get() is first)
    fired = []
    root.bind_all("<Escape>", lambda e: fired.append(1), add="+")
    first.event_generate("<Escape>")
    pump(0.2)
    root.unbind_all("<Escape>")
    _hide()
    check("Esc in a form cancels it", app._gce_form is None
          and len(app._gce_model.changes) == n0,
          f"focus was {root.focus_get()!r}")
    check("…and never reaches the app-wide Esc", not fired, str(fired))


def save_writes_page_text():
    app = _M["app"]
    import gui_gcode_editor as ge
    # saved under the name the page offers: the header's "MEWracle print:"
    # line names the file it is written to
    out = os.path.join(tempfile.mkdtemp(), app._gce_page_save_name())
    real = ge.filedialog.asksaveasfilename
    ge.filedialog.asksaveasfilename = lambda **k: out
    try:
        with _CommentsKept():
            app.save_gcode_as()
    finally:
        ge.filedialog.asksaveasfilename = real
    data = open(out, encoding="utf-8").read() if os.path.isfile(out) else ""
    check("Save g-code as on the page writes the page's text exactly",
          data == app.gce.get_text() and data == app._gce_model.render().text)


def _layers(app):
    return [i for i, inf in app._gce_tree_info.items()
            if inf.get("kind") == "layer"]


def _row_shown(row):
    return any(w.winfo_manager() for w in row.widgets)


def _lines(app, iid):
    """The page's text of an Anatomy part, by its own line range."""
    info = app._gce_tree_info[iid]
    return app.gce.get_text().splitlines()[info["first"] - 1:info["last"]]


def tool_speed_on_layer():
    """Select a layer, then Speed: the change covers that layer alone, in
    the short form."""
    app = _M["app"]
    m = app._gce_model
    n0 = len(m.changes)
    lay = _layers(app)
    before4 = _lines(app, lay[3])
    app._gce_select_node(lay[4])
    pump(0.2)
    app._gce_tool_change("speed_set")
    pump(0.3)
    fs = app._gce_fs
    vals = fs["vals"]
    check("the Speed tool opens its edit as a tool's", bool(fs.get("tool")))
    check("…in its own box over the picture, not under the tree",
          str(app._gce_form).startswith(str(app._gce_formbox) + "."),
          str(app._gce_form))
    hidden = [k for k in ("name_row", "type_row", "what_row",
                          "applies_row") if not _row_shown(vals[k])]
    check("…the whole form: Name, Type, What and Applies", not hidden,
          str(hidden))
    check("…with no More options and no repeat tick box",
          not any(isinstance(w, (tk.ttk.Button, tk.ttk.Checkbutton))
                  and w.cget("text") in ("More options",
                                         "Repeat it after every")
                  for w in app._gce_walk(app._gce_form)))
    check("…and says it is on that one layer",
          vals["tool_where"].get().startswith("Applies to layer 5 of"),
          vals["tool_where"].get())
    vals["f"]["t:speed_set"]["mm_s"].set("15")
    app._gce_edit_flush()
    _button(app, "Done").invoke()
    pump(0.3)
    c = m.changes[-1] if len(m.changes) == n0 + 1 else None
    sc = getattr(c, "scope", None)
    check("…one change, on layer 5 only",
          sc is not None and (sc.kind, sc.first, sc.last) == ("layers", 5, 5),
          repr(sc))
    check("…the selected layer's moves take the speed",
          any("F900" in ln for ln in _lines(app, lay[4])))
    check("…the layer before is untouched",
          _lines(app, lay[3]) == before4)
    app.gce.undo()
    pump(0.5)
    check("…and one undo takes it off", len(m.changes) == n0)


def tool_layer_shift():
    """Whole file, then Scale/move > Layer shift: the form's X step and a
    profile chosen by its label make one change; layer n moves n steps
    (Ramp counts the first layer as 0); the form says how far the last
    layer went; one undo takes it off."""
    app = _M["app"]
    m = app._gce_model
    import mew_gcode as mg
    n0 = len(m.changes)
    root = [i for i, inf in app._gce_tree_info.items()
            if inf.get("kind") == "file"]
    lay = _layers(app)
    before = {k: _lines(app, lay[k]) for k in (0, 2)}
    app._gce_select_node(root[0])
    pump(0.2)
    check("Layer shift is offered on a flat file",
          app._gce_tool_why("layer_shift") is None,
          str(app._gce_tool_why("layer_shift")))
    app._gce_tool_change("layer_shift")
    pump(0.3)
    vals = app._gce_fs["vals"]
    f = vals["f"]["t:layer_shift"]
    labels = dict(mg.TRANSFORM_CHOICES["layer_shift"]["profile"])
    check("…its Profile is a choice showing Ramp",
          f["profile"].get() == labels["ramp"], f["profile"].get())
    f["dx"].set("20")
    app._gce_edit_flush()
    pump(0.3)
    said = vals["tool_where"].get()
    check("…the form says how far the last layer went",
          "counted from 0" in said and "The last moves X +" in said, said)
    _button(app, "Done").invoke()
    pump(0.3)
    c = m.changes[-1] if len(m.changes) == n0 + 1 else None
    check("…one change, a layer shift of 20 µm on X",
          c is not None and c.op == "layer_shift"
          and float(c.params["dx"]) == 20.0
          and c.params["profile"] == "ramp", repr(c))
    check("…the first layer is untouched", _lines(app, lay[0]) == before[0])

    def xs(lines):
        return [float(mg.parse_words(t)[1]["X"]) for t in lines
                if "X" in mg.parse_words(t)[1]]
    a, b = xs(before[2]), xs(_lines(app, lay[2]))
    check("…layer 3 moved two steps, 0.04 mm in X",
          a and len(a) == len(b)
          and all(abs(y - x - 0.04) < 2e-4 for x, y in zip(a, b)),
          repr(list(zip(a, b))[:2]))
    app.gce.undo()
    pump(0.5)
    check("…and one undo takes it off", len(m.changes) == n0)


def tool_insert_after_layer():
    """Select a layer, then Insert › Dwell: the dwell goes after it and sits
    under its row; Re-target moves it to another selected layer."""
    app = _M["app"]
    m = app._gce_model
    lay = _layers(app)
    last0 = app._gce_tree_info[lay[2]]["last"]
    t0 = app.gce.get_text().splitlines()
    app._gce_select_node(lay[2])
    pump(0.2)
    app._gce_tool_insert("dwell")
    pump(0.3)
    cid = app._gce_fs["cid"]
    _button(app, "Done").invoke()
    pump(0.3)
    c = m.get(cid)
    key = tuple(app._gce_tree_info[lay[2]]["key"])
    check("Insert after a layer anchors after that layer",
          (c.anchor.unit, c.anchor.side, tuple(c.anchor.key))
          == ("layer", "after", key), repr(c.anchor))
    app._gce_render_changes()
    tree = app._gce_tree
    check("…and sits under that layer's row",
          tree.parent("e:" + cid) == lay[2], tree.parent("e:" + cid))
    r = app._gce_render()
    at = [i for i, rl in enumerate(r.lines, start=1) if rl.chip == cid]
    t1 = app.gce.get_text().splitlines()
    # the outline's range ends on the next layer's marker line, so the
    # check is the invariant: no move of the layer after the dwell, and
    # nothing else moved
    k = at[0] - 1 if at else 0
    check("…its line right after the layer's last move",
          bool(at) and t1 == t0[:k] + [t1[k]] + t0[k:]
          and not any(ln.startswith(("G0", "G1")) for ln in t0[k:last0])
          and any(ln.startswith(("G0", "G1")) for ln in t0[k - 1:k]),
          f"{at}: {t1[k - 2:k + 2]}")
    app._gce_open_edit(cid, tool=True)
    pump(0.2)
    app._gce_select_node(lay[6])
    pump(0.2)
    app._gce_retarget()
    pump(0.3)
    ask = app._gce_fs["vals"]["rt_ask"].get()
    app._gce_render_changes()
    check("Re-target names the new place and waits for OK",
          tree.parent("e:" + cid) == lay[2]
          and app._gce_tree_info[lay[6]]["label"] in ask, ask)
    _button(app, "OK").invoke()
    pump(0.3)
    _button(app, "Done").invoke()
    pump(0.3)
    app._gce_render_changes()
    check("Re-target moves it to the newly selected layer",
          tree.parent("e:" + cid) == lay[6], tree.parent("e:" + cid))
    _M["tool_dwell"] = cid


def text_selection_is_selection():
    """Lines selected in the text: the tree shows their layer, a change
    covers exactly those lines, an insert goes right after the last one."""
    app = _M["app"]
    m = app._gce_model
    lay = _layers(app)
    info = app._gce_tree_info[lay[3]]
    a, b = info["first"] + 1, info["first"] + 3
    app._gce_select_lines(a, b)
    pump(0.2)
    check("the tree selects the layer the lines are in",
          app._gce_tree.selection() == (lay[3],),
          str(app._gce_tree.selection()))
    sc = app._gce_sel_scope()
    check("…a change covers exactly those lines",
          sc is not None and sc.kind == "lines"
          and len(sc.first or ()) == b - a + 1, repr(sc))
    n0 = len(m.changes)
    app._gce_tool_insert("dwell")
    pump(0.3)
    cid = app._gce_fs["cid"]
    _button(app, "Done").invoke()
    pump(0.3)
    r = app._gce_render()
    at = [i for i, rl in enumerate(r.lines, start=1) if rl.chip == cid]
    check("…an insert lands right after the last selected line",
          at == [b + 1], f"{at} vs {b + 1}")
    app.gce.undo()
    pump(0.5)
    check("…and undoes", len(m.changes) == n0)


def delete_tool_removes_lines():
    """Select lines, then Delete: exactly those lines go, as an edit."""
    app = _M["app"]
    m = app._gce_model
    t0 = app.gce.get_text()
    n0 = len(m.changes)
    info = app._gce_tree_info[_layers(app)[3]]
    a, b = info["first"] + 1, info["first"] + 3
    app._gce_select_lines(a, b)
    pump(0.2)
    app._gce_tool_delete()
    pump(0.5)
    lines = t0.splitlines(True)
    t1 = app.gce.get_text()
    check("Delete removes exactly the selected lines",
          t1 == "".join(lines[:a - 1] + lines[b:]))
    check("…recorded as an edit the document renders",
          len(m.changes) > n0 and m.render().text == t1,
          f"{len(m.changes)} vs {n0}")
    app.gce.undo()
    pump(0.5)
    check("…and undoes", app.gce.get_text() == t0)


def tree_x_removes_edit():
    """The ✕ at the end of an edit's bar in the tree deletes the edit."""
    app, root = _M["app"], _M["root"]
    m = app._gce_model
    cid = _M["tool_dwell"]
    app._gce_render_changes()
    tree = app._gce_tree
    iid = "e:" + cid
    _show()
    tree.see(iid)
    pump(1.0, lambda: bool(app._gce_tree_x_spot(iid)))
    x, y = app._gce_tree_x_spot(iid) or (0, 0)

    class _Ev:
        pass
    ev = _Ev()
    ev.x, ev.y = x, y
    app._gce_tree_press(ev)
    app._gce_tree_release(ev)
    pump(0.3)
    _hide()
    check("the ✕ on an edit's row removes it",
          cid not in [c.id for c in m.changes] and not tree.exists(iid),
          f"spot={x, y}")


def tree_is_bars_over_picture():
    """Fusion's browser: only each row's bar stands over the picture. The
    browser's window is clipped to bars as wide as their text, so the gap
    beside a bar and between two bars is the picture's, for the eye and the
    mouse. Comparator: the window's own region, read back from Windows."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes
    app = _M["app"]
    _show()
    pump(3.0, lambda: len(app._gce_tree_bars) > 3)
    bars = dict(app._gce_tree_bars)
    tree, box = app._gce_tree, app._gce_browser
    gdi = ctypes.WinDLL("gdi32")
    user = ctypes.WinDLL("user32")
    gdi.CreateRectRgn.restype = wintypes.HRGN
    gdi.CreateRectRgn.argtypes = (ctypes.c_int,) * 4
    gdi.PtInRegion.argtypes = (wintypes.HRGN, ctypes.c_int, ctypes.c_int)
    gdi.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
    user.GetWindowRgn.argtypes = (wintypes.HWND, wintypes.HRGN)
    rgn = gdi.CreateRectRgn(0, 0, 0, 0)
    has_rgn = user.GetWindowRgn(box.winfo_id(), rgn) > 1
    dx = tree.winfo_rootx() - box.winfo_rootx()
    dy = tree.winfo_rooty() - box.winfo_rooty()

    def shown(x, y):
        return bool(gdi.PtInRegion(rgn, dx + x, dy + y))
    check("the browser's window is clipped", has_rgn)
    check("every bar stands", all(
        shown((a + c) // 2, (b + d) // 2) for a, b, c, d in bars.values()),
        bars)
    check("beside a bar is the picture", not any(
        shown(c + 2, (b + d) // 2) for a, b, c, d in bars.values()), bars)
    check("between two bars is the picture", not any(
        shown((a + c) // 2, d) for a, b, c, d in bars.values()), bars)
    check("a bar is as wide as its text, not the tree",
          min(c - a for a, _b, c, _d in bars.values())
          < tree.winfo_width() // 2, bars)
    gdi.DeleteObject(rgn)
    _hide()


def repeat_row_and_its_undo():
    """A Speed under an edit, a Repeat under the Speed: Speed rewrites the
    copies' lines, and the Repeat still counts them. The row is not cut off,
    and Undo puts a deleted row back in the tree, not only in the text."""
    app, m = _M["app"], _M["app"]._gce_model
    eng, tree = app._gce_engine(), app._gce_tree
    _show()
    lay = next(i for i, v in app._gce_tree_info.items()
               if v.get("kind") == "layer")
    app._gce_select_node(lay)
    pump(0.2)
    before = app._gce_snap()
    c = m.add(eng.Insert(lines=["G1 X100 Y100"], flavor=m.flavor,
                         anchor=app._gce_sel_anchor()))
    sp = m.add_operation(c.id, "speed_scale", {"percent": 20})
    rp = m.add_operation(sp.id, "repeat",
                         {"where": "every_n_moves", "n": 100})
    app._gce_rerender(before)
    eid = "e:" + rp.id
    pump(3.0, lambda: eid in app._gce_tree_bars)
    wrote = {ln.uid[2] for ln in m.render().lines
             if ln.uid and ln.uid[0] == "rep"}
    words = "%d copies" % len(wrote)
    check("the Repeat counts the copies it wrote",
          len(wrote) > 1 and words in tree.item(eid, "text"),
          (words, tree.item(eid, "text")))
    pump(1.0, lambda: app._gce_browser.winfo_width()
         >= app._gce_tree_want)
    check("the browser is as wide as its widest bar",
          app._gce_browser.winfo_width() >= app._gce_tree_want,
          (app._gce_browser.winfo_width(), app._gce_tree_want))
    tree.see(eid)
    tree.focus(eid)
    app._gce_tree_key("delete")
    pump(0.3)
    check("the row deletes", not tree.exists(eid))
    app.gce.undo()
    pump(1.0, lambda: tree.exists(eid))
    check("Undo puts the row back", tree.exists(eid)
          and rp.id in [x.id for x in m.changes])
    tree.focus("e:" + c.id)
    app._gce_tree_key("delete")
    pump(0.3)
    _hide()


def picture_click_selects_layer():
    """A click on a fiber in the picture (the panel's click seam,
    `_fire_pick`) selects the ONE move that laid it: the selection is that
    line, as a text selection is, it alone is lit, the caret goes to it and
    the keyboard stays off the text (G must still open it). The tree shows
    the layer holding it. A double-click widens to that layer; empty space
    clears. The comparator is the picture itself: the clicked surface point
    lies on the lit fiber of that one line (`_gce_hl_geometry`, what the
    highlight draws). RED against the layer-wide pick: focus_lines was the
    whole layer, and the caret sat on its first line."""
    import numpy as np
    app = _M["app"]
    pv = app._gce_pv_panel
    if pv is None:
        check("picture click: no preview panel", False)
        return
    _show()

    def fresh():
        # a picture of THIS text: the seam answers nothing for a stale one
        clock = ((pv._state or {}).get("meta") or {}).get("line_clock")
        return bool((pv._state or {}).get("paths")) and clock is not None             and clock.get("text") == app.gce.get_text()
    if not fresh():
        app._gce_pv_kick_now()
    pump(30.0, fresh)
    W, H = pv._host.winfo_width(), pv._host.winfo_height()
    spot = None
    for fy in range(10, 90, 4):
        for fx in range(30, 90, 4):
            x, y = W * fx // 100, H * fy // 100
            g = pv.pick(x, y)
            if g is not None:
                hit = pv.pick_at(x, y)
                if hit and hit.get("layer") is not None:
                    spot = (x, y, hit, g)
                    break
        if spot:
            break
    check("picture click: a fiber to click on", spot is not None,
          f"{W}x{H}")
    if spot is None:
        _hide()
        return
    x, y, hit, g = spot
    line = hit["line"]
    pv._fire_pick(x, y)
    pump(0.3)
    iid = "n:l:%s:%s:%s" % tuple(hit["key"])
    info = app._gce_tree_info.get(iid, {})
    check("…the selection is that one line",
          app._gce_sel == ("text", line, line), str(app._gce_sel))
    check("…it alone is lit", app.gce.focus_lines == [line],
          str(app.gce.focus_lines[:5]))
    check("…the caret is on it", app.gce.caret()[0] == line,
          f"{app.gce.caret()} vs {line}")
    check("…the keyboard stays off the text",
          app.root.focus_get() is not app.gce.text, str(app.root.focus_get()))
    check("…the tree shows the layer holding it",
          app._gce_tree.selection() == (iid,)
          and info.get("first", 0) <= line <= info.get("last", -1),
          f"{app._gce_tree.selection()} vs {iid}")
    polys, dots = app._gce_hl_geometry(app._gce_hl, [line])
    d = float(((pv._state or {}).get("meta") or {}).get("fiber_diameter")
              or 0.02)
    P0 = np.asarray(g["point"], np.float64)
    near = float("inf")
    for pl in polys:
        pl = np.asarray(pl, np.float64).reshape(-1, 3)
        for a, b in zip(pl[:-1], pl[1:]):
            ab = b - a
            u = min(max(float(np.dot(P0 - a, ab) / max(ab @ ab, 1e-18)),
                        0.0), 1.0)
            near = min(near, float(np.linalg.norm(a + u * ab - P0)))
    check("…and the clicked point lies on that line's lit fiber",
          near <= 1.5 * d, f"{near:.4f} mm vs d={d}")
    pv._fire_pick(x, y, 2)
    pump(0.3)
    foc = app.gce.focus_lines or []
    check("a double-click selects the move's layer",
          app._gce_tree.selection() == (iid,)
          and app._gce_sel == ("node", iid) and bool(foc)
          and min(foc) == info.get("first")
          and max(foc) == info.get("last"),
          f"{app._gce_sel} {(min(foc), max(foc)) if foc else None}")
    pv._fire_pick(2, 2)
    pump(0.3)
    check("a click on empty space clears it",
          app._gce_tree.selection() == () and app._gce_sel is None,
          str(app._gce_tree.selection()))
    _hide()


def _fresh_picture(app, pv):
    """Wait for a picture of THIS text (the pick and the lit geometry
    answer nothing for a stale one)."""
    def fresh():
        clock = ((pv._state or {}).get("meta") or {}).get("line_clock")
        hl = getattr(app, "_gce_hl", None)
        return bool((pv._state or {}).get("paths")) and clock is not None \
            and clock.get("text") == app.gce.get_text() and hl is not None \
            and hl.get("clock") is clock
    if not fresh():
        app._gce_pv_kick_now()
    return pump(60.0, fresh)


def _lit_mid(app, lines, joins=True):
    """The middle (bbox centre) of the fiber `lines` lay in the picture,
    by the page's own lookup (`_gce_hl_geometry`). `joins=False` drops each
    polyline's first point: a run's first line starts where the line BEFORE
    the run ended, which a move of the run does not carry."""
    import numpy as np
    polys, dots = app._gce_hl_geometry(app._gce_hl, lines)
    pts = [np.asarray(p, np.float64).reshape(-1, 3)[0 if joins else 1:]
           for p in polys]
    pts += [np.asarray(d, np.float64).reshape(1, 3) for d in dots]
    P = np.concatenate(pts)
    return (P.min(axis=0) + P.max(axis=0)) / 2.0


def _tk_event(pv, x, y, state=0):
    import types
    h = max(pv._host.winfo_height(), 1)
    return types.SimpleNamespace(x=int(round(x)), y=int(round(h - y)),
                                 state=state)


def move_tool_moves_selection():
    """Move on two layers (selected in the tree, Ctrl-click): the gimbal
    sits at the middle of their fiber; dragging its X arrow writes the
    box's X and nothing else, and the text waits for Done; while the box
    is open the picture is the old one and the lit layers are drawn
    shifted by dx; the edit sits under BOTH layers in the tree; at Done
    the text moves exactly those layers' X words by it (the comparator:
    the text before, line by line -- every move in the two layers X + dx,
    every other line unchanged) and the picture is rebuilt, where the
    same lines now lie dx further along X (the second
    comparator: the rebuilt picture, physics and sag off so the fiber IS
    the toolpath); Undo puts the text back.
    RED: `_gce_homes` -> only the first part (one row); `_gce_mv_drag`
    writing nothing on release (box 0, text unchanged); the panel's
    `set_highlight_offset` ignored (no ghost); `_gce_mv_lines` -> the
    focused lines (no gimbal on a new move)."""
    import numpy as np
    import move_gimbal
    import mew_gcode
    app = _M["app"]
    pv = app._gce_pv_panel
    if pv is None:
        check("move: no preview panel", False)
        return
    orig = app._pv_snapshot_options
    app._pv_snapshot_options = lambda i: dict(orig(i), physics=None,
                                              sag=False)
    _show()
    try:
        app._gce_edit_finish(keep=True)
        check("move: a picture of the text", _fresh_picture(app, pv))
        layers = _layers(app)
        la, lb = layers[2], layers[6]
        ia, ib = app._gce_tree_info[la], app._gce_tree_info[lb]
        lines = list(range(ia["first"], ia["last"] + 1)) + \
            list(range(ib["first"], ib["last"] + 1))
        mid0 = _lit_mid(app, lines)
        run0 = _lit_mid(app, lines, joins=False)
        before = app.gce.get_text()
        n0 = len(app._gce_model.changes)
        app._gce_tree.selection_set([la, lb])
        app._gce_select_rows()
        pump(0.2)
        app._gce_tool_move()
        pump(0.5)
        g = pv._gimbal
        check("Move opens the box on an offset of the selection",
              app._gce_mv_vars() is not None
              and len(app._gce_model.changes) == n0 + 1)
        go = pv.gimbal_origin()
        check("…the gimbal at the middle of the two layers' fiber",
              go is not None and np.allclose(go, mid0, atol=1e-9),
              f"{go} vs {mid0}")
        if go is None:
            return
        sh = move_gimbal.handle_shapes(g.origin, g.length)
        a, b = g._project(sh["x"][0]), g._project(sh["x"][1])
        p = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        u = np.subtract(b, a) / np.linalg.norm(np.subtract(b, a))
        pv._press("orbit", _tk_event(pv, *p))
        check("…a press on the X arrow grabs it",
              pv._gdrag is not None and pv._gdrag[0] == "x",
              str(pv._gdrag and pv._gdrag[0]))
        for k in range(1, 9):
            pv._motion(_tk_event(pv, p[0] + 10 * k * u[0],
                                 p[1] + 10 * k * u[1]))
        pv._release(_tk_event(pv, p[0] + 80 * u[0], p[1] + 80 * u[1]))
        pump(0.5)
        dx, dy, dz = app._gce_mv_now()
        check("…the drag writes X alone into the box",
              dx > 0.01 and dy == 0 and dz == 0, (dx, dy, dz))
        c = app._gce_model.changes[-1]
        check("…the text waits for Done", app.gce.get_text() == before
              and isinstance(c, mew_gcode.Transform) and c.op == "offset"
              and not c.params.get("dx"), getattr(c, "params", None))
        off = pv.highlight_offset()
        check("…the old picture stays and the lit layers ride dx",
              np.allclose(off, (dx, 0, 0), atol=1e-9)
              and np.allclose(pv.gimbal_origin(), mid0 + (dx, 0, 0),
                              atol=1e-9), f"{off}")
        rows = [r for r in app._gce_tree_rows if r[0].startswith("e:" + c.id)]
        check("…the move sits under BOTH layers in the tree",
              sorted(r[1] for r in rows) == sorted([la, lb]),
              str([(r[0], r[1]) for r in rows]))
        app._gce_edit_finish(keep=True)
        c = app._gce_model.get(c.id)
        check("Done: the box's X goes into the edit",
              abs(c.params.get("dx", 0) - dx) < 1e-9, c.params)
        after = app.gce.get_text()
        bad = []
        b_lines, a_lines = before.split("\n"), after.split("\n")
        want = set(lines)
        for i, (x0, x1) in enumerate(zip(b_lines, a_lines), start=1):
            w0, w1 = mew_gcode.parse_words(x0)[1], \
                mew_gcode.parse_words(x1)[1]
            if i in want and x0.startswith(("G0 ", "G1 ", "G2 ", "G3 ")) \
                    and "X" in w0:
                ok = abs(float(w1.get("X", "nan")) - float(w0["X"]) - dx) \
                    < 1e-3 and w1.get("Y") == w0.get("Y")
            else:
                ok = x0 == x1
            if not ok:
                bad.append(i)
        check("…the text moves exactly those layers' X by it",
              len(b_lines) == len(a_lines) and not bad,
              f"{len(bad)} lines wrong, first {bad[:3]}")
        check("Done: the picture is rebuilt", _fresh_picture(app, pv))
        check("…the gimbal and the shift are gone",
              pv.gimbal_origin() is None
              and pv.highlight_offset() == (0.0, 0.0, 0.0))
        run1 = _lit_mid(app, lines, joins=False)
        check("…and in it the two layers lie dx further along X",
              np.allclose(run1 - run0, (dx, 0, 0), atol=1e-3),
              f"{run1 - run0} vs {dx}")
        app.gce.undo()
        pump(0.5)
        check("Undo puts the text back", app.gce.get_text() == before
              and len(app._gce_model.changes) == n0)
    finally:
        del app._pv_snapshot_options
        app._gce_edit_finish(keep=False)
        _hide()


def eyes_hide_and_isolate():
    """The tree's eyes (display only): a click on a layer's eye folds
    exactly its lines out of the text and its fiber out of the picture;
    Show only this keeps one layer; Show all puts the whole picture back;
    the text Save writes never changes. The comparator is the picture's own
    line clock (`preview_pick.line_window`): no drawn fiber of a hidden
    layer may lie inside its print-time window."""
    import numpy as np
    import preview_pick
    app = _M["app"]
    pv = app._gce_pv_panel
    tree = app._gce_tree
    text0 = app.gce.get_text()
    _show()
    pump(30.0, lambda: getattr(app, "_gce_pv_full", None) is not None
         and (app._gce_pv_full[1].get("line_clock") or {}).get("text")
         == app.gce.get_text())
    lay = _layers(app)
    L = lay[-1]
    info = app._gce_tree_info[L]
    tree.see(L)
    pump(0.3)
    x0, y0, w0, h0 = tree.bbox(L, "#0") or (0, 0, 0, 0)
    xs = [x for x in range(x0, x0 + w0)
          if "image" in str(tree.identify_element(x, y0 + h0 // 2))]

    class _Ev:
        pass
    ev = _Ev()
    ev.x, ev.y = (xs[0] if xs else x0), y0 + h0 // 2
    app._gce_tree_press(ev)
    app._gce_tree_release(ev)
    pump(0.5)
    txt = app.gce.text
    n = app.gce.line_count()
    gone = [i for i in range(1, n + 1) if "hidden" in txt.tag_names(f"{i}.0")]
    check("a click on a layer's eye folds exactly its lines",
          bool(xs) and gone == list(range(info["first"], info["last"] + 1)),
          f"eye at {xs[:1]}, {len(gone)} lines folded")
    clock = app._gce_pv_full[1]["line_clock"]
    t0, t1 = preview_pick.line_window(clock, info["first"], info["last"])
    meta = pv._state["meta"]
    inside = sum(int(np.sum((np.asarray(tp)[1:] > t0)
                            & (np.asarray(tp)[:-1] < t1)))
                 for tp in meta["ptimes"])
    check("…and its fiber out of the picture (none in its time window)",
          inside == 0 and len(pv._state["paths"]) > 0, f"{inside} segments")
    app._gce_isolate(lay[1])
    pump(0.5)
    i1 = app._gce_tree_info[lay[1]]
    n_shown = n - sum(1 for i in range(1, n + 1)
                      if "hidden" in txt.tag_names(f"{i}.0"))
    a, b = preview_pick.line_window(clock, i1["first"], i1["last"])
    outside = sum(int(np.sum((np.asarray(tp) < a - 1e-9)
                             | (np.asarray(tp) > b + 1e-9)))
                  for tp in pv._state["meta"]["ptimes"])
    check("Show only this: just that layer, in the text and the picture",
          n_shown == i1["last"] - i1["first"] + 1 and outside == 0,
          f"{n_shown} lines shown, {outside} points outside")
    app._gce_show_all()
    pump(0.5)
    check("Show all: the whole picture and text again",
          pv._state["paths"] is app._gce_pv_full[0]
          and not txt.tag_ranges("hidden"))
    check("…and the text Save writes never changed",
          app.gce.get_text() == text0 == app._gce_model.render().text)
    _hide()


def help_index_follows_flavor():
    """Help's command index: a search narrows it; the dwell's example is the
    engine's own line in the page's flavor, and follows the Flavor menu."""
    import mew_gcode as eng
    app = _M["app"]
    fl0 = app.gce_flavor.get()
    app._gce_help_toggle(True)
    pump(0.2)
    app.gce_help_q.set("arc")
    pump(0.1)
    cmds = [r["example"].split()[0] for r in app._gce_help_rows]
    check("Help: 'arc' finds the two arcs", cmds == ["G2", "G3"], str(cmds))
    app.gce_help_q.set("dwell")
    pump(0.1)
    want = eng.make_command("dwell", app.gce_flavor_name(), seconds=2)[0]
    check("…the dwell is the engine's line in this flavor, with its words",
          app.gce_help_ex_var.get() == want
          and eng.explain(want, app.gce_flavor_name())
          in app.gce_help_what_var.get(),
          f"{app.gce_help_ex_var.get()!r} vs {want!r}")
    app.gce_flavor.set(eng.FLAVORS["duet"].label)
    pump(0.2)
    want = eng.make_command("dwell", "duet", seconds=2)[0]
    check("…and follows the Flavor menu (Duet)",
          app.gce_help_ex_var.get() == want == "G4 S2",
          f"{app.gce_help_ex_var.get()!r} vs {want!r}")
    app.gce_flavor.set(fl0)
    app.gce_help_q.set("")
    app._gce_help_toggle(False)
    pump(0.2)


def cad_page_stands_alone():
    """The CAD layout: the status row shows on every page, this one
    included, and the page's tools ride in the top row; G toggles the
    g-code overlay unless the key is typing."""
    app, root = _M["app"], _M["root"]
    # The status row stays on every page (6146e5d2): a refusal or a run's
    # result is written there, and a row folded away on Simulation or the
    # G-code window put it where no one could see it.
    st = app._shell_status
    check("the status row shows on the G-code page",
          st.winfo_manager() == "grid")
    app._nav_select("view")
    pump(0.2)
    check("...and on Simulation", st.winfo_manager() == "grid")
    app._nav_select("machine")
    pump(0.2)
    check("...and on any other page", st.winfo_manager() == "grid")
    app._nav_select("gcode")
    pump(0.2)
    check("the page's tools are in the top row",
          app._nav_context.winfo_manager() == "pack")

    class _Ev:
        def __init__(self, widget):
            self.widget = widget
    was = app._gce_code_shown
    app._gce_key_g(_Ev(app.gce.text))
    check("G typed into the g-code text does not toggle it",
          app._gce_code_shown == was)
    app._gce_key_g(_Ev(root))
    check("G elsewhere toggles the g-code overlay",
          app._gce_code_shown != was)
    _show()
    pump(1.0, lambda: app._gce_viewport.winfo_width() > 400)
    app._gce_toggle_code(True)
    pump(0.2)
    check("the shown g-code overlay is placed over the picture",
          app._gce_code.winfo_manager() == "place"
          and app._gce_browser.winfo_manager() == "place",
          f"{app._gce_code.winfo_manager()!r} "
          f"vp={app._gce_viewport.winfo_width()}")
    app._gce_toggle_code(was)
    _hide()
    app._nav_select("scaffold")
    pump(0.3)
    check("another page brings the status row back and puts the tools away",
          not app._gce_page_shown() and st.winfo_manager() == "grid"
          and app._nav_context.winfo_manager() == "")


def page_flavor_sets_tube_feeds():
    """Aerotech picked on THIS page (the Machine page still Klipper): a 4-axis
    tube's export must carry split feeds, F linear + E rotary, because the
    page spells it for an Aerotech, which runs F as the X rate. RED before
    (2026-10-28): the feeds followed the Machine page alone and the export
    was `LINEAR ... F800` with no E -- 1.9x the surface speed on a real rig.
    Picked back to Klipper, no E may survive: Klipper would extrude it."""
    app = _M["app"]
    coll = app.s_collector.get()
    label = app.gce_flavor.get()

    def settled():
        pump(2.0)
        pump(60, lambda: app._gce_regen is None
             and getattr(app, "_gce_model", None) is not None)
        pump(0.5)

    def e_moves():
        ex = app._gce_model.render().text
        return sum(1 for ln in ex.splitlines()
                   if ln.split() and any(w[:1] == "E" for w in ln.split()[1:]))
    try:
        app._nav_select("gcode")      # a pick regenerates only a shown page
        app.s_collector.set("mandrel")
        settled()
        check("tube: a Klipper page exports no E", e_moves() == 0,
              str(e_moves()))
        app.gce_flavor.set("Aerotech (AeroBasic)")
        app._gce_flavor_selected()
        settled()
        check("tube: Aerotech picked on the page exports split feeds",
              e_moves() > 0, app.gce.get_text()[:120])
        app.gce_flavor.set(label)
        app._gce_flavor_selected()
        settled()
        check("tube: picked back, the E words are gone", e_moves() == 0,
              str(e_moves()))
    finally:
        app.gce_flavor.set(label)
        app._gce_flavor_selected()
        app.s_collector.set(coll)
        settled()


def main():
    try:
        _probe = tk.Tk()
        _probe.destroy()
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: no Tk display ({exc})")
        return 0
    import tkinter.ttk  # noqa: F401  (tk.ttk below)
    if not boot():
        print("FAIL: the page never showed the designer's g-code")
        return 1
    for case in (linked_text_is_save_text, highlight_lights_the_line,
                 option_flip_adds_no_chip,
                 insert_form_anchors, form_session_one_step,
                 edit_lives_in_anatomy, typed_line_places_edit,
                 library_edit_is_the_form, typing_is_never_lost,
                 stitch_is_an_edit,
                 scale_from_the_form, typed_edit_opens_in_form,
                 chips_off_is_designer,
                 undo_restores_chips, generate_carries_doc,
                 generate_carries_unrecorded_typing,
                 regen_has_no_doc, preset_round_trip,
                 flavor_follows_machine, esc_closes_form,
                 save_writes_page_text, tool_speed_on_layer,
                 tool_layer_shift,
                 tool_insert_after_layer, text_selection_is_selection,
                 delete_tool_removes_lines, tree_x_removes_edit,
                 tree_is_bars_over_picture, repeat_row_and_its_undo,
                 help_index_follows_flavor, picture_click_selects_layer,
                 move_tool_moves_selection,
                 eyes_hide_and_isolate,
                 cad_page_stands_alone, page_flavor_sets_tube_feeds):
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
    print("\n" + ("ALL PASS" if not _FAILS else f"{len(_FAILS)} FAILED"))
    return 1 if _FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
