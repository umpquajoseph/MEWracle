#!/usr/bin/env python3
"""app_paths.py -- frozen-aware resource and user-data locations.

Two lookups the whole app funnels through so a PyInstaller build behaves:

  resource_path(*parts)  read-only BUNDLED assets (the taskbar icon in Logo/,
                         the printhead STL, gui_defaults.json). Frozen they live
                         under ``sys._MEIPASS`` (the one-folder extraction root);
                         from source they sit in the repo root.

  user_data_dir()        user-WRITABLE data: the named-preset stores, the scratch
                         "outputs" session folder, per-user prefs. Frozen the
                         bundle dir is read-only (it can land in Program Files, and
                         a one-file build's _MEIPASS is a throwaway temp), so this
                         is ``%LOCALAPPDATA%\\MEWracle``. From source it is the repo
                         dir -- so a normal (unfrozen) run writes EXACTLY where it
                         always did; every user-data path stays byte-identical.

Deliberately free of tkinter/vtk/numpy: gui_viewer_shell imports
it inside VTK subprocesses, so it must cost nothing and drag nothing in.
"""

import os
import sys

# The program's folders. Their modules import one another by bare name
# (`import gcode_to_fibers`, whichever folder it is in), so importing this module
# puts the repo root and every folder on sys.path: see add_program_folders.
PROGRAM_FOLDERS = ("engine", "physics", "machine", "viewers",
                   "analysis", "gui", "tools")

# The repo root when run from source: this file sits in its engine/ folder. A
# one-folder bundle (PyInstaller sets a module's __file__ under sys._MEIPASS) or
# a flattened copy (MEWracle Lite's container image) holds every module in ONE
# directory, which is then the root. resource_path prefers sys._MEIPASS
# explicitly so it is correct for a one-file build too.
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_DIR = os.path.dirname(_HERE) if os.path.basename(_HERE) == "engine" else _HERE


def resource_path(*parts):
    """Absolute path to a bundled, read-only asset (icon, printhead STL, ...).

    Frozen: rooted at ``sys._MEIPASS``. From source: the repo root, one
    folder up -- identical to the old ``os.path.join(HERE, *parts)`` the call sites
    used, so nothing changes when run normally. With no parts, returns the base."""
    base = getattr(sys, "_MEIPASS", None) or _SRC_DIR
    return os.path.join(base, *parts) if parts else base


def beside_app(*parts):
    """Absolute path to an optional download the user puts NEXT TO the app.

    Frozen: the folder holding MEWracle.exe -- NOT ``sys._MEIPASS``, which a
    one-folder build puts in ``_internal`` and a one-file build in a temp dir.
    From source: the repo dir, where a git clone already has the file. This is
    how `machine_models/` (kept out of the zip) is found after a download."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = _SRC_DIR
    return os.path.join(base, *parts) if parts else base


def user_data_dir():
    """Directory for user-writable app data (presets, scratch outputs, prefs).

    Frozen: ``%LOCALAPPDATA%\\MEWracle`` (created on first use), because the
    bundle/extraction dir is read-only. From source: the repo root, one
    folder up -- so PRESETS_DIR / OUTPUTS_DIR / gui_prefs.json land exactly where they
    always have and a normal run is byte-identical."""
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "MEWracle")
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass
        return path
    return _SRC_DIR


#: From source, the folder in the checkout that holds the researcher's own
#: presets. Ignored by git (.gitignore), so `git pull` never touches it.
SOURCE_USER_PRESETS = "my_presets"


def shipped_presets_dir():
    """The presets that come WITH the program: read-only, replaced by every
    update. Frozen: the bundle's `presets/`. From source: the tracked
    `presets/` folder of the checkout. Nothing the app saves goes here."""
    return resource_path("presets")


def user_presets_dir():
    """The researcher's OWN presets, their edits of shipped ones and the
    default pointers they set (`preset_store` reads both folders, theirs
    winning by name).

    Frozen: ``%LOCALAPPDATA%\\MEWracle\\presets``, where they always were.
    From source: ``<checkout>/my_presets``, beside the tracked ``presets/``
    and ignored by git, so a pull can neither refuse over them nor replace
    them, and each checkout (a worktree, a test run) keeps its own."""
    if getattr(sys, "frozen", False):
        return os.path.join(user_data_dir(), "presets")
    return os.path.join(_SRC_DIR, SOURCE_USER_PRESETS)


def add_program_folders():
    """Put the repo root and every PROGRAM_FOLDERS folder on sys.path.

    Runs once, on this module's first import, so a source run can import any
    module from any folder. An entry point outside engine/ reaches it with
    ``sys.path.insert(0, <root>/engine)`` and ``import app_paths``. A folder
    already on the path keeps its place; the rest go in after sys.path[0], the
    running script's own folder. Frozen, every module is in the bundle and this
    does nothing; in a flattened copy the folders do not exist and are skipped."""
    if getattr(sys, "frozen", False):
        return
    have = {os.path.normcase(os.path.abspath(p)) for p in sys.path if p}
    new = []
    for p in [_SRC_DIR] + [os.path.join(_SRC_DIR, f) for f in PROGRAM_FOLDERS]:
        if os.path.isdir(p) and os.path.normcase(p) not in have:
            new.append(p)
    sys.path[1:1] = new


add_program_folders()
