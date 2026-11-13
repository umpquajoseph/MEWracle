#!/usr/bin/env python3
"""test_flavor_editor.py -- saved flavors in the GUI: the one flavor editor
(`gui_flavor_editor`) opened from the Machine page and from the G-code page's
Syntax menu, the machine preset's carried copy, and the two places the
G-code page speaks for the flavor: a word that looks like a move but isn't
read (`_gce_check_unread`), and a meaning command the flavor can't write,
which pauses Save (`_gce_gaps_notice`).

Comparators, none written by this page:
  * the files on disk in the flavor folder (read back with json);
  * the engine's parse form (`Document.render("parse")`) -- what the preview
    and the sag solve read -- for what a mapped word now means;
  * the file Save wrote, or that it wrote none.

Cases, each with its RED proof (the defect injected in memory by a probe script that does not ship;
seen failing, the
clean run is the GREEN control):
  one_editor_two_doors     RED: `_gce_edit_flavors` opening its own window
                           (a second FlavorEditor).
  duplicate_edit_save      RED: `_flavors_changed` not following a rename
                           (the Machine page keeps the old label).
  edits_are_never_lost     RED: `FlavorEditor.dirty` always False (first
                           seen in duplicate_edit_save's Not-saved-yet check).
  machine_copy_round_trip  RED: `_machine_flavor_copy_loaded` a no-op.
  unread_word_is_offered   RED: `_gce_check_unread` a no-op.
  save_pauses_on_a_gap     RED: `save_gcode_as`'s gap check removed.
  share_by_file            RED: `FlavorEditor.import_files` not telling the
                           app (`_flavors_changed` skipped).

    python tests/test_flavor_editor.py

SKIPS with no Tk display. Bounded: every wait has a deadline."""
import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import json
import os
import shutil
import sys
import tempfile
import time

try:
    import tkinter as tk
    from tkinter import ttk
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
    import flavor_presets
    import gui_fonts
    import mewracle_gui
    d = tempfile.mkdtemp(prefix="flavors_gui_")
    root = tk.Tk()
    root.withdraw()
    gui_fonts.init_fonts(root)
    app = mewracle_gui.ModernApp(root)
    # never the user's own flavor folder
    app._flavors_dir = lambda: d
    flavor_presets.load(d)
    root.update()
    _M.update(root=root, app=app, dir=d)
    app._nav_select("gcode")
    return pump(60, lambda: bool(app.gce.get_text())
                and app._gce_regen is None
                and getattr(app, "_gce_model", None) is not None)


def _walk(w):
    yield w
    for c in w.winfo_children():
        yield from _walk(c)


def _button(top, text):
    for w in _walk(top):
        if isinstance(w, ttk.Button) and w.cget("text") == text:
            return w
    return None


def _on_disk(label):
    for fn in os.listdir(_M["dir"]):
        if fn.endswith(".json"):
            with open(os.path.join(_M["dir"], fn), encoding="utf-8") as fh:
                d = json.load(fh)
            if d.get("label") == label:
                return d
    return None


def _close_editor():
    ed = getattr(_M["app"], "_flavor_editor", None)
    if ed is not None and ed.alive():
        ed.revert()
        ed.close()
    pump(0.1)


def _load(text, name):
    path = os.path.join(_M["dir"], "..", name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    _M["app"]._gce_load_file(os.path.abspath(path))
    pump(0.5)


def one_editor_two_doors():
    app = _M["app"]
    app._nav_select("machine")
    pump(0.5)
    btn = _button(app.root, "Edit flavors…")
    check("the Machine page has an Edit flavors... button", btn is not None)
    if btn is None:
        return
    btn.invoke()
    pump(0.3)
    ed = getattr(app, "_flavor_editor", None)
    check("...which opens the flavor editor", ed is not None and ed.alive())
    check("...on the machine's flavor, with Use on this printer",
          ed.flavor().label == app.gcode_flavor.get()
          and ed.use_btn.cget("text") == "Use on this printer",
          ed.use_btn.cget("text"))
    app._nav_select("gcode")
    pump(0.3)
    menu = app._gce_flavor_menu()
    labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
              if menu.type(i) != "separator"]
    check("the G-code page's Syntax menu has Edit flavors...",
          "Edit flavors…" in labels, str(labels))
    menu.invoke(labels.index("Edit flavors…") + 1)   # past the separator
    pump(0.3)
    check("...the same window, its Use now for the g-code",
          app._flavor_editor is ed
          and ed.use_btn.cget("text") == "Use for this g-code")
    built = [ed.tree.item(i, "text") for i in ed.tree.get_children()]
    check("the list shows the five built-ins, marked",
          sum(t.endswith("built in") for t in built) == 5, str(built))
    ed.select_label("Aerotech (AeroBasic)")
    pump(0.1)
    check("a built-in's rows are editable, its name and Rename are not",
          str(ed.widgets["move_word"].cget("state")) == "normal"
          and str(ed.widgets["label"].cget("state")) == "readonly"
          and str(ed.rename_btn.cget("state")) == "disabled"
          and ed.delete_btn.cget("text") == "Reset to default")
    _close_editor()


def duplicate_edit_save():
    app = _M["app"]
    ed = app._open_flavor_editor("Aerotech (AeroBasic)", app.gcode_flavor.set,
                                 "Use on this printer")
    pump(0.1)
    new = ed.duplicate()
    pump(0.1)
    check("Duplicate makes a saved copy, selected",
          new is not None and ed.flavor() is new
          and _on_disk("Aerotech (AeroBasic) copy") is not None)
    ed.use()
    pump(0.1)
    check("Use sets the Machine page's flavor",
          app.gcode_flavor.get() == "Aerotech (AeroBasic) copy")
    ed.vars["label"].set("Lab Aerotech")
    ed.vars["pressure"].set("$AO[0].X = {value}")
    check("an edit is Not saved yet until Save",
          ed.dirty() and str(ed.save_btn.cget("state")) == "normal"
          and _on_disk("Lab Aerotech") is None)
    ok = ed.save()
    pump(0.1)
    disk = _on_disk("Lab Aerotech") or {}
    check("Save writes the rows to its file, renamed in place",
          ok and disk.get("pressure") == "$AO[0].X = {value}"
          and disk.get("move_word") == "LINEAR"
          and _on_disk("Aerotech (AeroBasic) copy") is None, str(disk))
    check("the Machine page follows the rename",
          app.gcode_flavor.get() == "Lab Aerotech", app.gcode_flavor.get())
    check("...and its preset carries the saved copy",
          json.loads(app.gcode_flavor_custom.get() or "{}").get("pressure")
          == "$AO[0].X = {value}")
    check("the Machine page's list has it",
          "Lab Aerotech" in app._machine_flavor_box.cget("values"))
    ed.delete()
    check("Delete asks once on the button", _on_disk("Lab Aerotech")
          and ed.delete_btn.cget("text") == "Delete: sure?")
    ed.delete()
    pump(0.1)
    check("...and the second press deletes; the machine falls back",
          _on_disk("Lab Aerotech") is None
          and app.gcode_flavor.get() == "Klipper"
          and app.gcode_flavor_custom.get() == "")
    _close_editor()


def edits_are_never_lost():
    app = _M["app"]
    ed = app._open_flavor_editor("Marlin")
    fl = ed.duplicate()
    ed.vars["move_word"].set("MOV")
    ed.select_label("GRBL")
    check("switching flavor with unsaved edits is refused, and said",
          ed.flavor() is fl and "unsaved" in ed.status_var.get())
    ed.close()
    check("the first Close warns and stays", ed.alive()
          and "Close again" in ed.status_var.get())
    ed.revert()
    check("Revert drops the edit", not ed.dirty()
          and ed.vars["move_word"].get() == "G1")
    ed.delete()
    ed.delete()
    _close_editor()


def machine_copy_round_trip():
    import flavor_presets as fp
    app = _M["app"]
    fl = fp.duplicate(_M["dir"], "duet", "Rig 7")
    fl.pressure = "M950 P{value}"
    fp.save(_M["dir"], fl)
    snap = fp.snapshot(fl)
    fp.delete(_M["dir"], fl.name)           # as on a computer without it
    app._flavors_changed(deleted="Rig 7")
    app.gcode_flavor.set("Rig 7")            # the preset's label first...
    app.gcode_flavor_custom.set(snap)        # ...then its carried copy
    pump(0.1)
    got = fp.by_label("Rig 7")
    check("loading a machine preset recreates its flavor here",
          got is not None and got.pressure == "M950 P{value}"
          and _on_disk("Rig 7") is not None)
    check("...and the Machine page shows it",
          app.gcode_flavor.get() == "Rig 7"
          and "Rig 7" in app._machine_flavor_box.cget("values"))
    if got is not None:
        fp.delete(_M["dir"], got.name)
        app._flavors_changed(deleted="Rig 7")
    app.gcode_flavor.set("Klipper (MEWron)")


def unread_word_is_offered():
    import flavor_presets as fp
    app = _M["app"]
    _load("G21\nG90\nG0 X0 Y0 Z0.1 F600\nLIN X10 Y0\nLIN X10 Y10\n"
          "SETAO X1\n", "lin.gcode")
    check("a word that looks like a move is offered to map",
          getattr(app, "_gce_notice_kind", None) == "unread-move"
          and "LIN" in app.gce_notice_var.get(), app.gce_notice_var.get())
    before = app._gce_model.render("parse").text
    check("control: the preview doesn't read it yet",
          "G1 X10 Y0" not in before)
    btn = _button(app._gce_notice_btns, "Read as printing moves")
    btn.invoke()
    pump(0.3)
    m = app._gce_model
    fl = fp.by_label(app.gce_flavor.get())
    check("mapping a built-in's word edits that built-in, text stays in it",
          fl is not None and fl.builtin and "LIN=G1" in fl.aliases
          and fp.is_edited(fl) and m.flavor == fl.name,
          app.gce_flavor.get())
    check("the preview now reads it as a printing move",
          "G1 X10 Y0" in m.render("parse").text
          and "G1 X10 Y10" in m.render("parse").text)
    check("...and the file keeps it as written",
          "LIN X10 Y0" in app.gce.get_text()
          and "LIN X10 Y0" in m.render("export").text)
    check("the next word is offered next",
          getattr(app, "_gce_notice_kind", None) == "unread-move"
          and "SETAO" in app.gce_notice_var.get(), app.gce_notice_var.get())
    _button(app._gce_notice_btns, "Not a move").invoke()
    pump(0.2)
    fl = fp.by_label(app.gce_flavor.get())
    check("Not a move: kept in the flavor's own list, and no more notice",
          "SETAO" in fl.known
          and getattr(app, "_gce_notice_kind", None) != "unread-move")
    edit = os.path.join(_M["dir"], fp.EDITS_DIR, fl.name + ".json")
    with open(edit, encoding="utf-8") as fh:
        rows = json.load(fh)
    check("both are on disk, as the built-in's edit",
          "LIN=G1" in rows.get("aliases", [])
          and "SETAO" in rows.get("known", []), str(rows))
    fp.reset(_M["dir"], fl.name)
    app._flavors_changed(edited=fl.name)


def save_pauses_on_a_gap():
    import flavor_presets as fp
    import gui_gcode_editor as ge
    app = _M["app"]
    fl = fp.duplicate(_M["dir"], "klipper", "No home")
    fl.home = ()                # a lab flavor with no home line
    fp.save(_M["dir"], fl)
    app._flavors_changed()
    _load("G21\nG90\nG28\nG0 X0 Y0 Z0.1 F600\nG1 X10 Y0\nG1 X10 Y10\n",
          "home.gcode")
    app.gce_flavor.set("No home")
    app._gce_flavor_selected()
    pump(0.3)
    out = os.path.join(tempfile.mkdtemp(), "saved.gcode")
    real = ge.filedialog.asksaveasfilename
    ge.filedialog.asksaveasfilename = lambda **k: out
    try:
        app.save_gcode_as()
        pump(0.2)
        check("Save pauses on a command the flavor can't write",
              not os.path.exists(out)
              and getattr(app, "_gce_notice_kind", None) == "export-gap"
              and "home" in app.gce_notice_var.get().lower(),
              app.gce_notice_var.get())
        _button(app._gce_notice_btns, "Save anyway").invoke()
        pump(0.2)
        # newline="": Save keeps the opened file's line endings byte for
        # byte (a CRLF file stays CRLF), so read them back untranslated
        check("...Save anyway writes the page's text",
              os.path.exists(out) and open(out, encoding="utf-8",
                                           newline="").read()
              == app.gce.get_text())
        os.remove(out)
        app.gce_flavor.set("Klipper")
        app._gce_flavor_selected()
        pump(0.2)
        app.save_gcode_as()
        check("a flavor that can write it saves at once",
              os.path.exists(out))
    finally:
        ge.filedialog.asksaveasfilename = real
    fp.delete(_M["dir"], fl.name)
    app._flavors_changed(deleted="No home")


def share_by_file():
    import flavor_presets as fp
    app = _M["app"]
    ed = app._open_flavor_editor("GRBL")
    check("a built-in can't be exported",
          str(ed.export_btn.cget("state")) == "disabled")
    fl = ed.duplicate()
    ed.vars["label"].set("Rig 9")
    ed.vars["pressure"].set("M42 P{value}")
    ed.save()
    path = os.path.join(tempfile.mkdtemp(), "rig9.json")
    check("Export writes the flavor to a file", ed.export(path)
          and json.load(open(path, encoding="utf-8")).get("pressure")
          == "M42 P{value}")
    ed.delete()
    ed.delete()
    got = ed.import_files([path, path + ".missing"])
    pump(0.1)
    check("Import brings it back, selected, and says what it skipped",
          len(got) == 1 and ed.flavor() is got[0]
          and got[0].pressure == "M42 P{value}"
          and "Skipped" in ed.status_var.get(), ed.status_var.get())
    check("...and every flavor menu lists it",
          "Rig 9" in app._machine_flavor_box.cget("values")
          and "Rig 9" in app._gce_flavor_labels())
    fp.delete(_M["dir"], got[0].name)
    app._flavors_changed(deleted="Rig 9")
    _close_editor()


def main():
    try:
        _probe = tk.Tk()
        _probe.destroy()
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: no Tk display ({exc})")
        return 0
    if not boot():
        print("FAIL: the G-code page never showed the designer's g-code")
        return 1
    for case in (one_editor_two_doors, duplicate_edit_save,
                 edits_are_never_lost, machine_copy_round_trip,
                 unread_word_is_offered, save_pauses_on_a_gap,
                 share_by_file):
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
    shutil.rmtree(_M["dir"], ignore_errors=True)
    print("\n" + ("ALL PASS" if not _FAILS else f"{len(_FAILS)} FAILED"))
    return 1 if _FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
