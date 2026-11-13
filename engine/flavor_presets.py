#!/usr/bin/env python3
"""flavor_presets.py -- saved g-code flavors: the flavor PRESET store.

A flavor (`mew_gcode.Flavor`) says how a lab's controller spells the commands
MEWracle reads. A lab's own flavors are saved here, one JSON file per flavor
(`Flavor.to_dict`), in the folder the GUI passes (`presets/flavor/` under the
user data folder). Headless: no Tk, so the store is tested on its own and both
editors (the Machine page and the G-code page's Syntax menu,
`gui_flavor_editor`) share it.

Built-ins. The five shipped flavors can be edited too. An edit is kept as a
DIFF from the shipped flavor in `edited_defaults/<name>.json`, beside the
lab's own: only the rows the lab changed, so an update that improves a shipped
row the lab never touched still reaches it, and the lab's rows survive every
update. `reset` deletes the diff, so the shipped flavor
(`mew_gcode.SHIPPED_FLAVORS`) is never lost. A built-in keeps its name and
label: presets and files on every computer know it by them. Older builds read
only the top folder, so they never see the subfolder.

Identity. A saved flavor's `name` is its id: chosen once from the label it
was first saved under and never changed, so Rename changes only the label.
The label is what every menu shows, and it is unique across all flavors,
built-in or saved, case-insensitive (the GUI maps a label back to a flavor).
A rename keeps the old label in the flavor's file (`former_labels`), so a
printer preset, a session or a design saved under the old name still finds it
(`by_label`).

The machine preset. `origin.gcode_flavor` keeps the LABEL (the Machine
page's box) and `origin.gcode_flavor_custom` carries a COPY of a saved flavor
(`snapshot`), so a preset copied to another computer brings its flavor
along. `adopt` reads that copy back: a flavor this computer already has
(same id, or else same label) wins, otherwise the copy is saved here. A
built-in carries no copy: every computer has it, and its own version wins
there, as a saved flavor's does. The key keeps its old name because users'
machine presets already carry it; in presets from before this store it holds
the old single Custom slot's rows, which `adopt` saves as a flavor labelled
"Custom"."""

import copy
import json
import os
import re

import mew_gcode

#: The machine preset's label for the old single Custom slot.
LEGACY_LABEL = "Custom"

#: The subfolder holding the lab's edits to built-ins, one diff per flavor.
EDITS_DIR = "edited_defaults"

#: Rows of a built-in that are not the lab's to change.
_FIXED = ("name", "label", "builtin", "sources")

_FILE = re.compile(r"^[a-z0-9_-]+\.json$")

#: saved flavor id -> the labels it showed before a rename, oldest first
_FORMER = {}


def is_saved(flavor):
    """True for a lab's own flavor (anything not built in)."""
    return not mew_gcode.get_flavor(flavor).builtin


def saved_flavors():
    """Every registered saved flavor, by label."""
    return sorted((f for f in mew_gcode.FLAVORS.values() if not f.builtin),
                  key=lambda f: f.label.lower())


def builtin_flavors():
    """The built-in flavors, in the engine's order."""
    return [f for f in mew_gcode.FLAVORS.values() if f.builtin]


def labels():
    """What a flavor menu lists: the built-ins, then the saved flavors."""
    return [f.label for f in builtin_flavors()] + [
        f.label for f in saved_flavors()]


def _shown_as(label):
    """The flavor whose CURRENT label is `label` (case-insensitive), or
    None."""
    low = str(label or "").strip().lower()
    for f in mew_gcode.FLAVORS.values():
        if f.label.lower() == low:
            return f
    return None


def by_label(label):
    """The flavor shown as `label` -- or as an old built-in label
    (`mew_gcode.LEGACY_FLAVOR_LABELS`), or as a label a saved flavor showed
    before it was renamed -- or None. A current label wins over a former
    one."""
    label = str(label or "").strip()
    label = mew_gcode.LEGACY_FLAVOR_LABELS.get(label, label)
    f = _shown_as(label)
    if f is not None:
        return f
    low = label.lower()
    for name, olds in _FORMER.items():
        if any(o.lower() == low for o in olds):
            f = mew_gcode.FLAVORS.get(name)
            if f is not None and not f.builtin:
                return f
    return None


def label_taken(label, but=None):
    """True when another flavor (not the one named `but`) shows `label`."""
    f = _shown_as(label)
    return f is not None and f.name != but


def is_edited(flavor):
    """True for a built-in this computer has changed from the shipped one."""
    return mew_gcode.flavor_is_edited(flavor)


def shipped(name):
    """The built-in `name` exactly as MEWracle ships it, or None."""
    return mew_gcode.SHIPPED_FLAVORS.get(str(name or "").lower())


def unique_label(label):
    """`label`, or `label (2)`, `(3)`... -- the first no flavor shows."""
    label = str(label or "").strip() or "My flavor"
    if not label_taken(label):
        return label
    stem = re.sub(r"\s*\(\d+\)$", "", label)
    k = 2
    while label_taken("%s (%d)" % (stem, k)):
        k += 1
    return "%s (%d)" % (stem, k)


def new_name(label):
    """A fresh id for a flavor first saved as `label`."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(label).lower()).strip("-")[:40]
    base = "user-" + (slug or "flavor")
    name, k = base, 2
    while name in mew_gcode.FLAVORS:
        name = "%s-%d" % (base, k)
        k += 1
    return name


def _path(directory, name):
    return os.path.join(directory, name + ".json")


def _edit_path(directory, name):
    return os.path.join(directory, EDITS_DIR, name + ".json")


def _write_json(path, d):
    """Replace `path` with `d` in one step, so a crash keeps the old file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _diff(fl):
    """The rows of built-in `fl` that differ from the shipped flavor."""
    ship = shipped(fl.name).to_dict()
    return {k: v for k, v in fl.to_dict().items()
            if k not in _FIXED and ship.get(k) != v}


def _builtin_with(name, rows):
    """Built-in `name` as shipped, with the lab's `rows` applied."""
    d = shipped(name).to_dict()
    d.update({k: v for k, v in rows.items() if k in d and k not in _FIXED})
    return mew_gcode.Flavor.from_dict(d)


def _load_edits(directory):
    """Every built-in back to shipped, then the lab's edits applied. A file
    that cannot be read leaves its built-in as shipped."""
    for name, ship in mew_gcode.SHIPPED_FLAVORS.items():
        mew_gcode.FLAVORS[name] = copy.deepcopy(ship)
    sub = os.path.join(directory, EDITS_DIR)
    try:
        files = sorted(os.listdir(sub))
    except OSError:
        return
    for fn in files:
        name = fn[:-5]
        if not _FILE.match(fn) or shipped(name) is None:
            continue
        try:
            with open(os.path.join(sub, fn), encoding="utf-8") as fh:
                rows = json.load(fh)
            if isinstance(rows, dict):
                mew_gcode.FLAVORS[name] = _builtin_with(name, rows)
        except (OSError, ValueError, TypeError):
            continue


def load(directory):
    """Register every flavor saved in `directory` and the lab's edits to the
    built-ins; returns the saved flavors. A file that cannot be read is
    skipped, never fatal, and a flavor that has gone from the folder is
    unregistered."""
    mew_gcode.FLAVOR_STORE_LOADED = True
    for f in saved_flavors():
        mew_gcode.FLAVORS.pop(f.name.lower(), None)
    _FORMER.clear()
    _load_edits(directory)
    out = []
    try:
        files = sorted(os.listdir(directory))
    except OSError:
        return out
    for fn in files:
        if not _FILE.match(fn) or shipped(fn[:-5]) is not None:
            continue
        try:
            with open(os.path.join(directory, fn), encoding="utf-8") as fh:
                d = json.load(fh)
            d["name"] = fn[:-5]
            d["builtin"] = False
            former = d.pop("former_labels", None)
            fl = mew_gcode.Flavor.from_dict(d)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if not fl.label or label_taken(fl.label, but=fl.name):
            fl.label = unique_label(fl.label or fl.name)
        if isinstance(former, list):
            _FORMER[fl.name] = [str(x) for x in former
                                if str(x).strip() and str(x) != fl.label]
        out.append(mew_gcode.register_flavor(fl))
    return out


def _note_rename(name, old, new):
    """Saved flavor `name` went from label `old` to `new`: remember `old`."""
    olds = [o for o in _FORMER.get(name, ()) if o.lower() != new.lower()]
    if old and old.lower() != new.lower() and old not in olds:
        olds.append(old)
    _FORMER[name] = olds


def save(directory, flavor):
    """Write `flavor` to `directory` and register it. The file is replaced in
    one step, so a crash mid-save keeps the old one.

    A flavor carrying a built-in's name is the lab's edit of that built-in:
    only its changed rows are written (`edited_defaults/`), its label and
    sources stay the shipped ones, and an edit that matches the shipped
    flavor again removes the file. Raises ValueError for a label another
    flavor shows."""
    fl = mew_gcode.get_flavor(flavor)
    ship = shipped(fl.name)
    if ship is not None and fl.name == ship.name:
        fl.builtin, fl.label = True, ship.label
        fl.sources = dict(ship.sources)
        rows = _diff(fl)
        path = _edit_path(directory, fl.name)
        if rows:
            _write_json(path, rows)
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        return mew_gcode.register_flavor(fl)
    if not fl.label.strip():
        raise ValueError("A flavor needs a name.")
    if label_taken(fl.label, but=fl.name):
        raise ValueError("Another flavor is already called %s." % fl.label)
    fl.builtin = False
    had = mew_gcode.FLAVORS.get(fl.name.lower())
    if had is not None and had is not fl and had.label != fl.label:
        _note_rename(fl.name, had.label, fl.label)
    d = fl.to_dict()
    d.pop("builtin", None)
    d["sources"] = {}
    if _FORMER.get(fl.name):
        d["former_labels"] = list(_FORMER[fl.name])
    _write_json(_path(directory, fl.name), d)
    return mew_gcode.register_flavor(fl)


def reset(directory, name):
    """Built-in `name` back to exactly what MEWracle ships: the lab's edit
    file is deleted. Returns the shipped flavor, now registered."""
    ship = shipped(name)
    if ship is None:
        raise ValueError("Only a built-in flavor has a default to reset to.")
    try:
        os.remove(_edit_path(directory, ship.name))
    except FileNotFoundError:
        pass
    return mew_gcode.register_flavor(copy.deepcopy(ship))


def new(directory, label=None):
    """A new flavor of the lab's own, from scratch: plain G-code (G1/G0 moves,
    G2/G3 arcs, G90/G91, G28, G21, G4 P in ms, ";" comments -- `Flavor`'s own
    defaults) and NO process commands, so it asserts nothing about a
    controller that the lab has not typed. Its label is `label` or "New
    flavor", made unique."""
    label = unique_label(label or "New flavor")
    return save(directory, mew_gcode.Flavor(new_name(label), label,
                                            builtin=False))


def duplicate(directory, source, label=None):
    """A saved copy of `source` (any flavor, built-in or saved): "Duplicate
    Aerotech and edit". Its label is `label` or "<source> copy", made
    unique."""
    src = mew_gcode.get_flavor(source)
    label = unique_label(label or "%s copy" % src.label)
    fl = mew_gcode.custom_flavor(src.name, new_name(label), label)
    return save(directory, fl)


def rename(directory, name, label):
    """Show saved flavor `name` as `label`; its id, and so its file, stay.
    The old label is kept as a former one (`by_label` still finds it)."""
    fl = mew_gcode.get_flavor(name)
    if fl.builtin or fl.name != name:
        raise ValueError("A built-in keeps its name: printer presets and "
                         "files on every computer know it by it.")
    label = str(label or "").strip()
    if not label:
        raise ValueError("A flavor needs a name.")
    if label_taken(label, but=name):
        raise ValueError("Another flavor is already called %s." % label)
    old, olds = fl.label, list(_FORMER.get(name, ()))
    _note_rename(name, old, label)
    fl.label = label
    try:
        return save(directory, fl)
    except Exception:
        fl.label = old
        _FORMER[name] = olds
        raise


def delete(directory, name):
    """Remove saved flavor `name` from `directory` and the menus."""
    fl = mew_gcode.FLAVORS.get(str(name).lower())
    if fl is None or fl.builtin:
        raise ValueError("Only your own flavors can be deleted.")
    try:
        os.remove(_path(directory, fl.name))
    except FileNotFoundError:
        pass
    mew_gcode.FLAVORS.pop(fl.name.lower(), None)
    _FORMER.pop(fl.name, None)


def export(flavor, path):
    """Write `flavor` to `path` to share: every row, the JSON a saved
    flavor's own file holds. A built-in as shipped is refused -- every
    MEWracle has it; a built-in this computer edited is shared, and imports
    elsewhere as a flavor of its own ("Klipper (2)")."""
    fl = mew_gcode.get_flavor(flavor)
    if fl.builtin and not is_edited(fl):
        raise ValueError("%s is built in and unchanged, so everyone already "
                         "has it." % fl.label)
    d = fl.to_dict()
    d.pop("builtin", None)
    d["sources"] = {}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(d, indent=1, sort_keys=True))
    os.replace(tmp, path)


def import_file(directory, path):
    """Save the flavor in the file at `path` (an `export`, or a flavor's own
    file) as a new flavor here, and return it. It always gets a fresh id, and
    a label already shown becomes "Label (2)": an import never replaces a
    flavor you have. Raises ValueError for a file that is not a flavor."""
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ValueError("Couldn't read %s: %s"
                         % (os.path.basename(path), exc))
    if not isinstance(d, dict) or not str(d.get("move_word") or "").strip():
        raise ValueError("%s is not a g-code flavor."
                         % os.path.basename(path))
    label = str(d.get("label") or "").strip() or os.path.splitext(
        os.path.basename(path))[0]
    d.pop("former_labels", None)
    d.update(builtin=False, sources={}, label=unique_label(label))
    d["name"] = new_name(d["label"])
    try:
        fl = mew_gcode.Flavor.from_dict(d)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s is not a g-code flavor (%s)."
                         % (os.path.basename(path), exc))
    return save(directory, fl)


def snapshot(flavor):
    """The copy a machine preset carries: a saved flavor as JSON, "" for a
    built-in (its name is enough everywhere)."""
    fl = mew_gcode.get_flavor(flavor)
    if fl.builtin:
        return ""
    d = fl.to_dict()
    d.pop("builtin", None)
    d["sources"] = {}
    return json.dumps(d, sort_keys=True)


def adopt(directory, text):
    """The flavor a machine preset's carried copy names, made available
    here: this computer's own flavor when it has one with the same id (or
    else the same label) -- it wins -- otherwise the copy, saved. `text` is
    `snapshot` output, or the old Custom slot's rows (no "name"), adopted as
    the flavor "Custom". None for an empty or unreadable copy."""
    try:
        d = json.loads(text or "")
    except ValueError:
        return None
    if not isinstance(d, dict) or not d:
        return None
    if "name" not in d:
        known = {f for f, _l, _e in mew_gcode.FLAVOR_FIELDS}
        rows = {k: v for k, v in d.items() if k in known and k != "label"}
        mine = by_label(LEGACY_LABEL)
        if mine is not None and not mine.builtin:
            return mine
        return save(directory, mew_gcode.custom_flavor(
            "klipper", new_name(LEGACY_LABEL), LEGACY_LABEL, **rows))
    name = str(d.get("name", "")).lower()
    have = mew_gcode.FLAVORS.get(name)
    if have is not None and not have.builtin:
        return have
    mine = by_label(d.get("label"))
    if mine is not None:
        if mine.builtin:
            d["label"] = unique_label(d.get("label"))
        else:
            return mine
    d["builtin"] = False
    d.pop("former_labels", None)
    try:
        fl = mew_gcode.Flavor.from_dict(d)
    except TypeError:
        return None
    if not _FILE.match(fl.name + ".json") or fl.name in mew_gcode.FLAVORS \
            or shipped(fl.name) is not None:
        fl.name = new_name(fl.label)
    return save(directory, fl)
