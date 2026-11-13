#!/usr/bin/env python3
"""preset_store.py -- where presets live, and the move of an older install.

Two folders, the same layout in each (`<kind>/*.json` plus `defaults.json`):

  shipped   `app_paths.shipped_presets_dir()` -- the presets that come with the
            program. Read-only to the app: an update replaces them, and a
            preset a later version adds simply appears.
  user      `app_paths.user_presets_dir()` -- what the researcher saved, their
            edits of shipped presets, the defaults they set and the shipped
            presets they deleted (`defaults.json`'s "hidden"). Never tracked
            by git (from source it is the ignored `my_presets/`), never inside
            the bundle (frozen it is under %LOCALAPPDATA%).

A user preset with the NAME of a shipped one is that shipped preset as the
researcher edited it: the app lists one entry, theirs, and "Reset to shipped"
deletes their file so the shipped one shows again. The merge itself is in
gui_presets._load_presets; this module is the headless half: the move of an
older install into the user folder, run once per process before the first
store is read, and by update.bat before it pulls.

The move never deletes a file and never overwrites one:

  * From a git checkout, under the tracked `presets/`:
      - an UNTRACKED .json (a preset the old app saved there) MOVES to the same
        place in the user folder, under a fresh file name if that one is taken;
      - a tracked preset the old app saved OVER (modified, unstaged) is COPIED
        as the user's edit of it, unless the user folder already has a preset
        of that name. The tracked file is left as it is; update.bat sets such
        leftovers aside in a git stash, so a pull never refuses over them;
      - a tracked preset the old app DELETED (unstaged) is recorded as hidden,
        so it stays deleted for them;
      - a modified `presets/defaults.json` gives each pointer it changed to the
        user's `defaults.json`, unless the user already set that pointer.
    Anything STAGED is left alone: someone using git on purpose is moving it.
  * Frozen only, in the user folder, a file that is an unchanged copy of the shipped preset
    of the same name (the frozen build's first launch used to copy the whole
    shipped set into %LOCALAPPDATA%) moves into `_copies_of_shipped/`, so it no
    longer hides the newer shipped version an update brings.

Stdlib only, no Tk: update.bat runs `python engine/preset_store.py` before it
pulls."""

import json
import os
import shutil
import subprocess
import sys

POINTER = "defaults.json"
#: the user `defaults.json` key listing deleted shipped presets:
#: {folder relative to the store root ("scaffold", "calibration/cts"): [names]}
HIDDEN_KEY = "hidden"
COPIES_DIR = "_copies_of_shipped"


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def preset_name(raw, filename):
    """The display name a preset file carries (its "name", else its stem)."""
    name = raw.get("name") if isinstance(raw, dict) else None
    if isinstance(name, str) and name.strip():
        return name
    return os.path.splitext(os.path.basename(filename))[0]


def folder_presets(directory):
    """{lower-case display name: (file name, raw dict)} for a store folder."""
    out = {}
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return out
    for fn in names:
        if not fn.lower().endswith(".json"):
            continue
        raw = _read_json(os.path.join(directory, fn))
        if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict):
            continue
        out.setdefault(preset_name(raw, fn).lower(), (fn, raw))
    return out


def free_path(directory, filename):
    """`directory/filename`, or `stem_2.json`, `_3`... -- the first free one."""
    stem, ext = os.path.splitext(filename)
    path, i = os.path.join(directory, filename), 2
    while os.path.exists(path):
        path = os.path.join(directory, "%s_%d%s" % (stem, i, ext))
        i += 1
    return path


def read_pointer(path):
    raw = _read_json(path)
    return raw if isinstance(raw, dict) else {}


def write_pointer(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def add_hidden(pointer, folder, name):
    """Record shipped preset `name` of store `folder` as deleted (in place)."""
    hidden = pointer.get(HIDDEN_KEY)
    if not isinstance(hidden, dict):
        hidden = pointer[HIDDEN_KEY] = {}
    names = hidden.setdefault(folder, [])
    if name.lower() in {n.lower() for n in names}:
        return False
    names.append(name)
    return True


# --- the git checkout ----------------------------------------------------- #
def _git(repo, *args):
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                           timeout=20, **({"creationflags": 0x08000000}
                                          if os.name == "nt" else {}))
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _git_changes(repo, rel_root):
    """[(XY, path relative to repo)] under `rel_root`, or [] without git."""
    if not os.path.exists(os.path.join(repo, ".git")):
        return []
    out = _git(repo, "status", "--porcelain=v1", "-z", "--no-renames",
               "--untracked-files=all", "--", rel_root)
    if not out:
        return []
    rows = []
    for item in out.decode("utf-8", "replace").split("\0"):
        if len(item) > 3:
            rows.append((item[:2], item[3:]))
    return rows


def _head_json(repo, relpath):
    out = _git(repo, "show", "HEAD:" + relpath.replace(os.sep, "/"))
    try:
        return json.loads(out.decode("utf-8")) if out else None
    except ValueError:
        return None


def migrate_checkout(repo, shipped_root, user_root):
    """Move what an older app saved into a checkout's tracked `presets/` into
    the user folder (see the module docstring). Returns [lines said]."""
    try:
        rel_root = os.path.relpath(shipped_root, repo)
    except ValueError:
        return []
    if rel_root.startswith(".."):
        return []
    moved = copied = hid = pointers = 0
    user_ptr_path = os.path.join(user_root, POINTER)
    user_ptr = read_pointer(user_ptr_path)
    ptr_dirty = False
    for xy, path in _git_changes(repo, rel_root):
        x, y = xy[0], xy[1]
        full = os.path.join(repo, path)
        rel = os.path.relpath(full, shipped_root)
        folder = os.path.dirname(rel).replace(os.sep, "/")
        if not rel.lower().endswith(".json") or rel.startswith(".."):
            continue
        if rel == POINTER:
            if xy == "??" or y != "M" or x != " ":
                continue
            now, head = read_pointer(full), _head_json(repo, path) or {}
            for k, v in now.items():
                if k != HIDDEN_KEY and head.get(k) != v and k not in user_ptr:
                    user_ptr[k] = v
                    ptr_dirty = True
                    pointers += 1
            continue
        dest_dir = os.path.join(user_root, os.path.dirname(rel))
        if xy == "??":
            try:
                os.makedirs(dest_dir, exist_ok=True)
                shutil.move(full, free_path(dest_dir, os.path.basename(rel)))
                moved += 1
            except OSError:
                pass
        elif x == " " and y == "M":
            raw = _read_json(full)
            if not isinstance(raw, dict) or not isinstance(raw.get("data"),
                                                            dict):
                continue
            if preset_name(raw, rel).lower() in folder_presets(dest_dir):
                continue
            try:
                os.makedirs(dest_dir, exist_ok=True)
                shutil.copyfile(full,
                                free_path(dest_dir, os.path.basename(rel)))
                copied += 1
            except OSError:
                pass
        elif x == " " and y == "D":
            raw = _head_json(repo, path)
            if isinstance(raw, dict) and                     add_hidden(user_ptr, folder, preset_name(raw, rel)):
                ptr_dirty = True
                hid += 1
    if ptr_dirty:
        try:
            write_pointer(user_ptr_path, user_ptr)
        except OSError:
            pass
    said = []
    if moved or copied:
        said.append("Moved %d preset%s you saved into %s, which git leaves "
                    "alone%s." % (moved + copied,
                                  "" if moved + copied == 1 else "s",
                                  os.path.basename(user_root),
                                  " (%d of them your edits of presets that "
                                  "came with MEWracle)" % copied
                                  if copied else ""))
    if pointers:
        said.append("Kept %d default%s you set." % (
            pointers, "" if pointers == 1 else "s"))
    if hid:
        said.append("Kept %d shipped preset%s you deleted out of the list."
                    % (hid, "" if hid == 1 else "s"))
    return said


# --- copies of shipped presets in the user folder ------------------------- #
def set_aside_copies(shipped_root, user_root):
    """Move every user preset that is an unchanged copy of the shipped preset
    of the same name into `_copies_of_shipped/`. Returns [lines said]."""
    n = 0
    for dirpath, dirnames, _files in os.walk(user_root):
        dirnames[:] = [d for d in dirnames
                       if d != COPIES_DIR and not d.startswith("_")]
        rel = os.path.relpath(dirpath, user_root)
        if rel == ".":
            continue
        shipped = folder_presets(os.path.join(shipped_root, rel))
        if not shipped:
            continue
        for low, (fn, raw) in folder_presets(dirpath).items():
            hit = shipped.get(low)
            if hit is None or hit[1].get("data") != raw.get("data"):
                continue
            dest = os.path.join(user_root, COPIES_DIR, rel)
            try:
                os.makedirs(dest, exist_ok=True)
                shutil.move(os.path.join(dirpath, fn), free_path(dest, fn))
                n += 1
            except OSError:
                pass
    if not n:
        return []
    return ["Set aside %d unchanged cop%s of presets that came with MEWracle "
            "(in %s), so updates to them reach you." % (
                n, "y" if n == 1 else "ies", COPIES_DIR)]


def migrate(shipped_root, user_root, repo=None):
    """The whole move, idempotent. `repo` is the checkout when run from
    source (None frozen). Returns a one-line summary or ""."""
    if os.path.normcase(os.path.abspath(shipped_root)) == \
            os.path.normcase(os.path.abspath(user_root)):
        return ""
    # The set-aside is for the frozen install only: its first launch made
    # the copies, and from a checkout the "shipped" folder is the working
    # tree, where a preset saved over still reads as shipped until update.bat
    # stashes it -- its copy in my_presets would look unchanged.
    if repo:
        said = migrate_checkout(repo, shipped_root, user_root)
    elif os.path.isdir(user_root):
        said = set_aside_copies(shipped_root, user_root)
    else:
        said = []
    return " ".join(said)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import app_paths
    repo = None if getattr(sys, "frozen", False) else app_paths.resource_path()
    msg = migrate(app_paths.shipped_presets_dir(),
                  app_paths.user_presets_dir(), repo)
    if msg:
        print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
