#!/usr/bin/env python3
"""Saved g-code flavors -- the flavor preset store (`flavor_presets`).

COMPARATORS, none written by the store:

  1. **The files on disk.** Everything the store claims is read back from
     the folder by a second, fresh `load` after the registry is cleared --
     the state another session (or another computer) would see.
  2. **The engine's own export.** A duplicated Aerotech exports a document
     byte for byte as Aerotech does; a renamed flavor keeps its file.
  3. **A machine preset from before the store.** Its
     `origin.gcode_flavor_custom` held the single Custom slot's rows (the
     JSON `gui_buildplate._custom_flavor_edited` wrote); `adopt` must turn it
     into a saved flavor called "Custom" whose words are those rows.

Self-running: prints PASS/FAIL per case, exits non-zero on any failure."""

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

import flavor_presets as fp
import mew_gcode as g

_PASS, _FAIL = [], []


def check(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       ("   " + detail) if detail else ""))


def _forget_saved():
    for f in fp.saved_flavors():
        g.FLAVORS.pop(f.name, None)


def test_duplicate_save_reload(d):
    fl = fp.duplicate(d, "aerotech")
    check("duplicating a built-in gives a saved copy",
          fl.label == "Aerotech (AeroBasic) copy" and not fl.builtin
          and fl.move_word == "LINEAR" and fl.aliases == ("RAPID=G0",),
          fl.label)
    doc = "G21\nG90\nG0 X0 A0 Z0.5 F600\nG1 X10 A360\nG4 P500\n"
    check("...which exports exactly as Aerotech does",
          g.Document(doc, flavor=fl.name).render().text
          == g.Document(doc, flavor="aerotech").render().text)
    again = fp.duplicate(d, "aerotech")
    check("a second copy gets its own name and id",
          again.label == "Aerotech (AeroBasic) copy (2)"
          and again.name != fl.name, again.label)
    fl.move_word = "MOVE"
    fp.save(d, fl)
    _forget_saved()
    back = {f.label: f for f in fp.load(d)}
    check("a fresh load reads back both, with the edit",
          set(back) == {fl.label, again.label}
          and back[fl.label].move_word == "MOVE", str(sorted(back)))
    check("...and lists them after the built-ins",
          fp.labels()[-2:] == sorted(back, key=str.lower)
          and fp.labels()[0] == "Klipper", str(fp.labels()))


def test_rename_and_delete(d):
    fl = fp.duplicate(d, "duet", "Lab Duet")
    name = fl.name
    fp.rename(d, name, "Lab Duet v2")
    check("rename keeps the id and the file",
          os.path.exists(os.path.join(d, name + ".json"))
          and g.get_flavor(name).label == "Lab Duet v2")
    for bad, why in ((lambda: fp.rename(d, name, "Marlin"), "a built-in's"),
                     (lambda: fp.rename(d, "klipper", "K2"), "built-in"),
                     (lambda: fp.delete(d, "grbl"), "built-in delete")):
        try:
            bad()
            ok = False
        except ValueError:
            ok = True
        check("refused: %s" % why, ok)
    check("the old name still finds it (a preset saved before the rename)",
          fp.by_label("Lab Duet") is g.get_flavor(name))
    k = g.get_flavor("klipper")
    k.pause = "M601"
    fp.save(d, k)
    edit = os.path.join(d, fp.EDITS_DIR, "klipper.json")
    with open(edit, encoding="utf-8") as fh:
        rows = json.load(fh)
    check("an edited built-in keeps only its changed rows, apart",
          rows == {"pause": "M601"} and fp.is_edited("klipper"), str(rows))
    fp.load(d)
    check("...and is still edited after a restart",
          g.get_flavor("klipper").pause == "M601"
          and g.get_flavor("klipper").builtin)
    fp.reset(d, "klipper")
    check("Reset to default brings back the shipped flavor",
          not fp.is_edited("klipper") and not os.path.exists(edit)
          and g.get_flavor("klipper").pause == "PAUSE")
    fp.delete(d, name)
    _forget_saved()
    check("delete removes it from the folder and the menus",
          not os.path.exists(os.path.join(d, name + ".json"))
          and "Lab Duet v2" not in fp.labels()
          and "Lab Duet v2" not in [f.label for f in fp.load(d)])


def test_a_machine_preset_carries_its_flavor(d, other):
    fl = fp.duplicate(d, "klipper", "Rig 2")
    fl.pressure = "SET_PRESSURE P={value}"
    fp.save(d, fl)
    snap = fp.snapshot(fl)
    check("a built-in carries no copy", fp.snapshot("marlin") == "")
    _forget_saved()
    fp.load(other)                     # another computer: never saw Rig 2
    got = fp.adopt(other, snap)
    check("on a computer without it, the copy is recreated and saved",
          got is not None and got.label == "Rig 2"
          and got.pressure == "SET_PRESSURE P={value}"
          and os.path.exists(os.path.join(other, got.name + ".json")))
    got.pressure = "MINE"
    fp.save(other, got)
    again = fp.adopt(other, snap)
    check("...and once it has one, its own wins",
          again.pressure == "MINE")
    fp.rename(other, again.name, "Rig 2 (renamed here)")
    check("...even after it was renamed here: the id finds it",
          fp.adopt(other, snap) is again
          and "Rig 2" not in fp.labels(), str(fp.labels()))
    fp.rename(other, again.name, "Rig 2")
    _forget_saved()
    fp.load(other)
    clash = json.loads(snap)
    clash["name"] = "user-someone-else"
    check("a different flavor carrying the same name: this computer's wins",
          fp.adopt(other, json.dumps(clash)).pressure == "MINE")
    bi = json.loads(snap)
    bi.update(name="user-x", label="Marlin")
    got = fp.adopt(other, json.dumps(bi))
    check("a copy named like a built-in is saved under its own name",
          got.label == "Marlin (2)" and not got.builtin, got.label)
    check("an empty or broken copy adopts nothing",
          fp.adopt(other, "") is None and fp.adopt(other, "{") is None)


def test_the_old_custom_slot(d):
    """The JSON `_custom_flavor_edited` wrote before this store: the rows
    of FLAVOR_FIELDS as text, no name."""
    old = {"dwell_word": "P", "dwell_unit": "ms", "move_word": "MOVE",
           "rapid_word": "", "home": "HOME_ALL", "max_line": 0,
           "line_numbers": False, "pressure": "M260 S{value}",
           "rotary_feed": "combined", "label": "Custom"}
    _forget_saved()
    fp.load(d)
    got = fp.adopt(d, json.dumps(old, sort_keys=True))
    check("an old machine preset's Custom rows become a saved 'Custom'",
          got is not None and got.label == "Custom" and not got.builtin
          and got.move_word == "MOVE" and got.home == ("HOME_ALL",)
          and got.pressure == "M260 S{value}", repr(got and got.home))
    check("...whose label the old preset's combobox value still names",
          fp.by_label("Custom") is got)
    check("...and loading it twice makes one flavor, not two",
          fp.adopt(d, json.dumps(old)) is got
          and [f.label for f in fp.saved_flavors()].count("Custom") == 1)


def test_a_bad_file_is_skipped(d):
    with open(os.path.join(d, "user-broken.json"), "w") as fh:
        fh.write("{not json")
    with open(os.path.join(d, "notes.txt"), "w") as fh:
        fh.write("x")
    _forget_saved()
    try:
        fp.load(d)
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    check("a broken file is skipped, never fatal", ok)
    fl = fp.duplicate(d, "grbl", "Gone soon")
    os.remove(os.path.join(d, fl.name + ".json"))
    fp.load(d)
    check("a flavor whose file went is gone from the menus after a load",
          "Gone soon" not in fp.labels())


def test_share_a_flavor(d, other):
    """Export on one computer, import on another (two folders)."""
    _forget_saved()
    fp.load(d)
    fl = fp.duplicate(d, "aerotech", "Lab A3200")
    fl.pressure = "$AO[0].X = {value}"
    fp.save(d, fl)
    path = os.path.join(os.path.dirname(d), "shared.json")
    fp.export(fl, path)
    try:
        fp.export("klipper", path + "2")
        ok = False
    except ValueError:
        ok = True
    check("a built-in is not exported", ok and not os.path.exists(path + "2"))
    _forget_saved()
    fp.load(other)
    got = fp.import_file(other, path)
    check("an exported flavor imports elsewhere with every word",
          got.label == "Lab A3200" and got.pressure == fl.pressure
          and got.move_word == "LINEAR" and got.aliases == ("RAPID=G0",)
          and os.path.exists(os.path.join(other, got.name + ".json")))
    again = fp.import_file(other, path)
    check("importing it twice keeps both, never replaces",
          again.label == "Lab A3200 (2)" and again.name != got.name
          and fp.by_label("Lab A3200") is got, again.label)
    _forget_saved()
    back = {f.label for f in fp.load(other)}
    check("...both read back from the folder",
          {"Lab A3200", "Lab A3200 (2)"} <= back, str(back))
    bad = os.path.join(os.path.dirname(d), "bad.json")
    for text in ("{not json", json.dumps({"label": "No moves"}), "[1]"):
        with open(bad, "w") as fh:
            fh.write(text)
        try:
            fp.import_file(other, bad)
            ok = False
        except ValueError:
            ok = True
        check("refused: not a flavor (%s)" % text[:12], ok)


def main():
    root = tempfile.mkdtemp(prefix="flavors_")
    try:
        a, b = os.path.join(root, "a"), os.path.join(root, "b")
        for d in (a, b):
            os.makedirs(d)
        test_duplicate_save_reload(a)
        test_rename_and_delete(a)
        test_a_machine_preset_carries_its_flavor(a, b)
        c = os.path.join(root, "c")
        os.makedirs(c)
        test_the_old_custom_slot(c)
        test_a_bad_file_is_skipped(c)
        e, f = os.path.join(root, "e"), os.path.join(root, "f")
        for x in (e, f):
            os.makedirs(x)
        test_share_a_flavor(e, f)
    finally:
        _forget_saved()
        shutil.rmtree(root, ignore_errors=True)
    print("\n%d passed, %d failed" % (len(_PASS), len(_FAIL)))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
