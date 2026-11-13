#!/usr/bin/env python3
"""gui_common.py -- shared imports, constants and small helpers for the GUI.

Every GUI mixin module (gui_core, gui_scaffold, gui_presets, gui_viewers, gui_run,
gui_sim) opens with `from gui_common import *`, so this file is the single shared
namespace: the stdlib + tk imports, the module-level path/preset constants and
the small widget/parse helpers. `__all__` is explicit
so the underscore helpers export too.

Import-only at module load -- nothing here creates a Tk object.
"""

import glob
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading

# One BLAS thread, set before anything imports numpy, and inherited by every
# worker and viewer the app starts. OpenBLAS reserves a buffer of committed
# memory per thread at import: about 490 MB per process on a 16-thread machine,
# 17 MB with one. The engines' heavy work is elementwise numpy and einsum, which
# never call BLAS, so the output is bit-identical (the graft and flat .depo,
# STL and g-code) and the time is unchanged within noise.
# A value already in the environment is kept.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

# Frozen-aware locations (PyInstaller). resource_path -> bundled read-only assets;
# user_data_dir -> where user-writable data (presets, scratch) must live. From source
# BOTH resolve to this repo dir, so every path below is byte-identical to before;
# frozen, USER_DATA_DIR becomes %LOCALAPPDATA%\MEWracle so presets and scratch never
# try to write the read-only bundle.
from app_paths import (resource_path, shipped_presets_dir, user_data_dir,
                       user_presets_dir)

HERE = resource_path()   # the repo root from source, the bundle root frozen
USER_DATA_DIR = user_data_dir()
WORKER = os.path.join(HERE, "engine", "gcode_to_fibers.py")
SCAFFOLD_WORKER = os.path.join(HERE, "engine", "scaffold_gcode.py")
VIEWER = os.path.join(HERE, "viewers", "stl_viewer.py")
PLAYER = os.path.join(HERE, "viewers", "gcode_player.py")

# --- frozen-aware worker dispatch ----------------------------------------
# The GUI runs its heavy work as SEPARATE processes. From source each is
# `python -u <script.py> ...`. Frozen there is no python and no loose .py: the ONE
# exe is a multi-call binary re-launched as `MEWracle.exe worker:xxx ...`, and
# mewracle_gui._maybe_dispatch_worker routes argv[1] to that worker's main().
# worker_cmd() is the single place that builds the right argv for either mode.
# Keys MUST match mewracle_gui._WORKERS.
_WORKER_PY = {
    "worker:convert":    WORKER,
    "worker:scaffold":   SCAFFOLD_WORKER,
    "worker:export":     os.path.join(HERE, "physics", "mew_export.py"),
    "worker:viewer":     VIEWER,
    "worker:player":     PLAYER,
    "worker:bedcal":     os.path.join(HERE, "machine", "bed_calibrator.py"),
    "worker:micrograph": os.path.join(HERE, "viewers", "micrograph_viewer.py"),
}


def worker_cmd(name, *args, unbuffered=True):
    """argv to launch a bundled worker, identical from source and frozen.

    `name` is a "worker:xxx" key (see _WORKER_PY). From source this returns
    ``[sys.executable, "-u", <script.py>, *args]``, byte-identical to the argv the
    launch sites hand-built before. Frozen (``sys.frozen``) it returns
    ``[sys.executable, name, *args]`` and the exe re-invokes itself.

    `unbuffered` adds Python's -u. The streaming workers (converter / scaffold /
    export) want it; the WINDOW launches (stl_viewer / gcode_player /
    bed_calibrator) never passed -u and so call ``unbuffered=False`` to stay
    byte-identical. -u is a source-interpreter flag only."""
    if getattr(sys, "frozen", False):
        return [sys.executable, name, *args]
    script = _WORKER_PY[name]
    flags = ["-u"] if unbuffered else []
    return [sys.executable, *flags, script, *args]


# Popen kwargs for a worker whose stdout the GUI reads. A Windows pipe otherwise
# takes the ANSI code page on BOTH ends: the worker died at its first print of
# a non-Latin path (a CJK file name; frozen, a Windows user name, since scratch
# lives under %LOCALAPPDATA%) and the GUI mis-decoded what did get through.
WORKER_PIPE_UTF8 = {
    "env": dict(os.environ, PYTHONIOENCODING="utf-8"),
    "encoding": "utf-8", "errors": "replace",
}


# Shareable named-preset store. Each preset is its OWN JSON file inside a folder
# ({"name": <display name>, "data": {persist-key: value}}), so a configuration
# can be shared by handing over one file -- drop it in and it appears on the next
# launch. Every store is a subfolder of ONE folder beside the pointer file
# `defaults.json`, and there are TWO such folders (engine/preset_store.py):
# SHIPPED_PRESETS_ROOT, the presets that come with the program (read-only, an
# update replaces them), and PRESETS_ROOT, the researcher's own (what they
# save, their edits of shipped presets, the defaults they set). Every constant
# below names the USER folder, which is the only one the app writes;
# gui_presets._init_pstore finds a store's shipped twin by its path relative to
# PRESETS_ROOT. From source PRESETS_ROOT is the git-ignored `my_presets/`, so a
# `git pull` never meets a file the app wrote. Until 2026-11 the stores were
# five siblings at the top of the data folder; migrate_preset_layout moves an
# older install into this layout once.
PRESETS_ROOT = user_presets_dir()
SHIPPED_PRESETS_ROOT = shipped_presets_dir()
SHIPPED_PRESETS_PATH = os.path.join(SHIPPED_PRESETS_ROOT, "defaults.json")
PRESETS_DIR = os.path.join(PRESETS_ROOT, "scaffold")
# Machine-and-Collector presets live in their OWN folder, so a machine/collector
# configuration can be saved and reused across scaffold recipes and vice versa.
MACHINE_PRESETS_DIR = os.path.join(PRESETS_ROOT, "machine")
# Saved g-code FLAVORS (flavor_presets): one JSON per flavor, shared by the
# Machine page and the G-code page. Machine presets name one and carry a copy.
FLAVOR_PRESETS_DIR = os.path.join(PRESETS_ROOT, "flavor")
# PHYSICS presets -- the rig/material profile (cal.* anchors + hooks and the sim.*
# process knobs) the Physics page saves, in its own folder so a per-rig profile can
# be shared independently of the machine geometry and the scaffold recipe.
#
# The CONSTANT and the kind id still read "calibration" while the FOLDER reads
# "physics": the id is load-bearing across gui_calibration, gui_core,
# mew_history and saved pointer files, so only the folder and the label moved. The
# id -> folder -> label table at the top of gui_presets.py is the authority.
CALIBRATION_PRESETS_DIR = os.path.join(PRESETS_ROOT, "physics")
# CALIBRATION presets -- the Calibration page's print DESIGNS (the CTS / jet-lag
# specimen forms) saved as a named profile. Kind id "sweep", inherited from the
# Experiment Sweep Designer this page grew out of. Each Calibration TAB keeps its
# own: kind "sweep:<tab>" in the subfolder `presets/calibration/<tab>/`, with its
# own pointer "sweep_default:<tab>". A .json directly in this folder is a
# whole-page preset from before the split; gui_presets._migrate_legacy_sweep_presets
# splits it into the tab subfolders once and keeps the original in `_legacy/`.
SWEEP_PRESETS_DIR = os.path.join(PRESETS_ROOT, "calibration")
SWEEP_KIND_PREFIX = "sweep:"
# Per-user pointer to the preset auto-loaded on startup -- a LOCAL preference, not
# shared. Holds one key per preset KIND (ids, not folder names -- see the table atop
# gui_presets.py): "default" (scaffold), "machine_default", "calibration_default"
# (the physics profile) and "sweep_default:<tab>" (one per Calibration tab; the
# bare "sweep_default" is the pre-split whole-page one), each a name or null. The scaffold and
# machine kinds keep ONE DEFAULT PER COLLECTOR (a 3-axis machine cannot print a
# 4-axis design), so those two also have "default_4axis" and
# "machine_default_4axis"; the unsuffixed key is the 3-axis one, which is why an
# install written before that split still boots off exactly the key it wrote.
# gui_presets.AXIS_MODES is the authority for the suffixes. This file is also
# the legacy store older builds wrote every preset into; on first run the loader
# migrates any inline presets it still holds into PRESETS_DIR, then rewrites it to
# hold only the pointers.
PRESETS_PATH = os.path.join(PRESETS_ROOT, "defaults.json")


# --- one-time layout move: the top-level stores -> presets/<kind>/ ---------
# An install written before 2026-11 keeps each store at the top of the data
# folder: presets/ (scaffold), presets_machine/, presets_physics/,
# presets_calibration/, presets_flavor/ and the pointer gui_presets.json. From
# source a `git pull` moves the TRACKED files and leaves a user's own presets
# behind in the old folders; frozen, %LOCALAPPDATA%\MEWracle holds the whole
# old layout. This moves both into presets/<kind>/ once, on the first store
# load. It never deletes a file and never overwrites one: an entry whose name
# the new folder already holds stays where it is, and an old folder is removed
# only when it is left empty.
_OLD_STORES = (("presets_machine", "machine"), ("presets_physics", "physics"),
               ("presets_calibration", "calibration"),
               ("presets_flavor", "flavor"))
_LAYOUT_NAMES = ("scaffold", "machine", "physics", "calibration", "flavor",
                 "defaults.json")


def _move_entries(src, dst, skip=(), files_only=False):
    """Move each entry of `src` (not named in `skip`; with `files_only`, only
    its .json files) into `dst` unless `dst` already holds that name.
    Returns (moved, left)."""
    try:
        names = [n for n in sorted(os.listdir(src)) if n not in skip
                 and (not files_only or (n.lower().endswith(".json")
                      and os.path.isfile(os.path.join(src, n))))]
    except OSError:
        return 0, 0
    if not names:
        return 0, 0
    try:
        os.makedirs(dst, exist_ok=True)
    except OSError:
        return 0, len(names)
    moved = left = 0
    for n in names:
        target = os.path.join(dst, n)
        if os.path.exists(target):
            left += 1
            continue
        try:
            shutil.move(os.path.join(src, n), target)
        except OSError:
            left += 1
            continue
        moved += 1
    return moved, left


def migrate_preset_layout(root, dest=None):
    """Move an old-layout install under `root` into `dest`/<kind>/ (`dest`
    defaults to `root`/presets, the frozen user folder; from source it is
    my_presets/, so the checkout's tracked presets/ is never moved into).
    Returns a one-line summary, or "" when there was nothing to do.
    Idempotent. Only loose .json FILES move out of `root`/presets: a folder
    there is a store, the program's own from source."""
    old_root = os.path.join(root, "presets")
    new_root = dest or old_root
    old_ptr = os.path.join(root, "gui_presets.json")
    new_ptr = os.path.join(new_root, "defaults.json")
    # In the OLD layout presets/ held only scaffold presets, so a
    # presets/defaults.json beside a gui_presets.json is a preset of that
    # name, not the pointer, and moves with the rest -- in the frozen user
    # folder. From source presets/defaults.json is the shipped pointer.
    skip = _LAYOUT_NAMES if (not os.path.isfile(old_ptr)
                             or new_root != old_root) else tuple(
        n for n in _LAYOUT_NAMES if n != "defaults.json")
    moved, left = _move_entries(old_root, os.path.join(new_root, "scaffold"),
                                skip, files_only=True)
    for old, kind in _OLD_STORES:
        src = os.path.join(root, old)
        m, l = _move_entries(src, os.path.join(new_root, kind))
        moved, left = moved + m, left + l
        try:
            os.rmdir(src)          # only succeeds when it is now empty
        except OSError:
            pass
    if os.path.isfile(old_ptr):
        if os.path.exists(new_ptr):
            left += 1
        else:
            try:
                os.makedirs(new_root, exist_ok=True)
                shutil.move(old_ptr, new_ptr)
                moved += 1
            except OSError:
                left += 1
    if not moved and not left:
        return ""
    tail = (" %d left where they were: presets/ already holds that name."
            % left) if left else ""
    return ("Moved %d preset file%s into the presets/ folder.%s"
            % (moved, "" if moved == 1 else "s", tail))


_preset_layout_migrated = False


def migrate_preset_layout_once():
    """migrate_preset_layout over the real user-data root, at most once per
    process. Called before anything reads a store: gui_presets._init_pstore
    and the flavor store's accessor."""
    global _preset_layout_migrated
    if _preset_layout_migrated:
        return ""
    _preset_layout_migrated = True
    msg = migrate_preset_layout(USER_DATA_DIR, PRESETS_ROOT)
    if msg:
        print(msg)
    return msg


# --- one-time folder migration: presets/calibration -> presets/physics ------
# Older installs keep PHYSICS profiles in `presets_calibration/`, the folder the
# Calibration page's print designs use. This moves them once, on the first store load, and
# never deletes anything: a file it cannot classify stays put.
#
# A preset file is a PHYSICS profile if any key of its "data" object starts with
# "cal." or "sim." -- exactly what _calibration_persist_keys claims. A
# Calibration-page design's keys all start with "sw.".
_PHYSICS_KEY_PREFIXES = ("cal.", "sim.")


def _is_physics_preset(path):
    """True if `path` is a preset file whose data belongs to the physics profile.
    Unreadable, non-JSON or ill-shaped files return False, so they are left where
    they are rather than moved on a guess."""
    try:
        with open(path, "r") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return False
    data = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(data, dict) or not data:
        return False
    return any(str(k).startswith(_PHYSICS_KEY_PREFIXES) for k in data)


def migrate_preset_folders(root):
    """Move pre-migration physics profiles out of `<root>/calibration/`
    and into `<root>/physics/`, where `root` is the USER presets folder (from
    source never the tracked presets/) (migrate_preset_layout has already
    brought an old install's `presets_calibration/` there). Returns a one-line summary, or "" when there was
    nothing to do.

    Idempotent, and deliberately conservative. The presence of `presets/physics/`
    means the install is already migrated, whatever the old folder holds. A folder
    with no physics profile is left as it is. If every readable file is a physics
    profile the FOLDER is renamed, carrying any sidecars with it; a MIXED folder
    moves only the physics files, and a destination name already taken is skipped.
    Nothing is ever deleted."""
    old = os.path.join(root, "calibration")
    new = os.path.join(root, "physics")
    if os.path.exists(new) or not os.path.isdir(old):
        return ""
    try:
        names = sorted(os.listdir(old))
    except OSError:
        return ""
    jsons = [n for n in names if n.lower().endswith(".json")]
    physics = [n for n in jsons if _is_physics_preset(os.path.join(old, n))]
    if not physics:
        return ""
    if len(physics) == len(jsons):
        try:
            os.rename(old, new)
        except OSError as exc:
            return (f"Preset folder migration skipped: could not rename "
                    f"{old} -> {new} ({exc}).")
        return (f"Migrated {len(physics)} physics preset"
                f"{'s' if len(physics) != 1 else ''}: renamed "
                f"presets/calibration/ -> presets/physics/.")
    try:
        os.makedirs(new, exist_ok=True)
    except OSError as exc:
        return f"Preset folder migration skipped: could not create {new} ({exc})."
    moved, skipped = 0, 0
    for n in physics:
        dest = os.path.join(new, n)
        if os.path.exists(dest):
            skipped += 1
            continue
        try:
            shutil.move(os.path.join(old, n), dest)
        except OSError:
            skipped += 1
            continue
        moved += 1
    tail = f" ({skipped} left in place)" if skipped else ""
    return (f"Migrated {moved} physics preset{'s' if moved != 1 else ''} from "
            f"presets/calibration/ to presets/physics/; "
            f"{len(jsons) - len(physics)} calibration preset"
            f"{'s' if len(jsons) - len(physics) != 1 else ''} stayed{tail}.")


# --- the move of presets an older app saved into the program's folder ------
# engine/preset_store.migrate: from a git checkout it moves what an older app
# saved into the tracked `presets/` over to `my_presets/`; in any install it
# sets aside unchanged copies of shipped presets (the frozen first launch used
# to copy them all into %LOCALAPPDATA%), so they stop hiding newer versions.
# It replaces that first-launch copy: shipped presets are now read where they
# ship, so a preset a later version adds reaches an existing install.
_user_presets_migrated = False


def migrate_user_presets_once():
    """preset_store.migrate over the real folders, at most once per process.
    Called from gui_presets._init_pstore after the layout and folder moves."""
    global _user_presets_migrated
    if _user_presets_migrated:
        return ""
    _user_presets_migrated = True
    import preset_store
    repo = None if getattr(sys, "frozen", False) else resource_path()
    try:
        msg = preset_store.migrate(SHIPPED_PRESETS_ROOT, PRESETS_ROOT, repo)
    except Exception as exc:  # noqa: BLE001 -- the app still starts
        msg = "Presets were not moved (%s); nothing was changed." % exc
    if msg:
        print(msg)
    return msg


_preset_folders_migrated = False


def migrate_preset_folders_once():
    """Run migrate_preset_folders over the real user-data root, at most once per
    process, printing its one line if it did anything. Called from
    gui_presets._init_pstore, i.e. on the first preset store any UI builds."""
    global _preset_folders_migrated
    if _preset_folders_migrated:
        return ""
    _preset_folders_migrated = True
    msg = migrate_preset_folders(PRESETS_ROOT)
    if msg:
        print(msg)
    return msg


# sidecar the worker drops next to the STL so the player skips the sag solve;
# a viewing aid, cleaned up on close like the slide (mirrors gcode_to_fibers'
# DEPO_CACHE_EXT, kept literal here to avoid importing the worker module)
DEPO_CACHE_EXT = ".depo.npz"

IS_WINDOWS = os.name == "nt"

# --- scratch output area --------------------------------------------------
# Generation never asks for an output path up front: g-code / STL / preview files
# are written into a throwaway per-session folder under OUTPUTS_DIR, and the user
# only names a file when they "Save ... as". Each GUI process owns
# OUTPUTS_DIR/session_<pid>/, so two windows can run at once; a clean exit removes
# the folder, and a crash leaves a stale session_<pid> that the NEXT launch sweeps
# once it sees that pid is gone. That sweep is the whole orphan-cleanup story.
OUTPUTS_DIR = os.path.join(USER_DATA_DIR, "outputs")
SESSION_DIR = os.path.join(OUTPUTS_DIR, "session_%d" % os.getpid())


def _pid_alive(pid):
    """True if a process with this id is currently running -- used to tell a
    live GUI's scratch folder (keep) from a crashed one's leftovers (sweep)."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True  # exists but unqueryable -> assume alive, keep its files
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    return True


def prepare_session_dir():
    """Create this process's scratch folder, sweeping any dead session's leftovers
    first. Returns SESSION_DIR. Call once at startup. A session_<pid> folder whose
    owning process is gone (a prior crash/force-quit) -- or our own pid recycled
    from one -- is removed now, so scratch never accumulates across runs."""
    try:
        os.makedirs(OUTPUTS_DIR, exist_ok=True)
        for name in os.listdir(OUTPUTS_DIR):
            if not name.startswith("session_"):
                continue
            path = os.path.join(OUTPUTS_DIR, name)
            if not os.path.isdir(path):
                continue
            try:
                pid = int(name[len("session_"):])
            except ValueError:
                continue
            if pid == os.getpid() or not _pid_alive(pid):
                shutil.rmtree(path, ignore_errors=True)
        os.makedirs(SESSION_DIR, exist_ok=True)
    except OSError:
        pass
    return SESSION_DIR


# rotation presets shown in the scaffold tab's dropdown. Each names the angle
# cycle it lays, since the step (60) is not what a researcher asks for
# (0/60/120). The number leads, as `_rotation_value` reads the first word.
ROTATION_PRESETS = ["90 (0/90)", "60 (0/60/120)", "45 (0/45/90/135)",
                    "36 (decagon)"]

# Default per-scaffold STL-viewer colours: a rainbow by scaffold index, mirroring
# stl_viewer._RAINBOW / _STAB_COLOR so the field shows what the viewer would draw.
# Colour is viewer-only (--colors) and never affects the g-code or STL.
SCAFFOLD_COLORS = ["#ff2626", "#ff8c1a", "#f2d91a", "#33cc40", "#4073ff"]
#: the scaffold-count spinners' top: a design has no limit, a Spinbox needs one
SCAFFOLD_SPIN_MAX = 999
STAB_COLOR_HEX = "#9e9ea8"
# A LONE scaffold defaults to this calm blue instead of the rainbow's leading red:
# with no siblings to distinguish it from, a neutral brand blue reads better. Adding
# a second scaffold rejoins scaffold 1 to the rainbow -- see
# gui_scaffold._apply_scaffold1_default_color.
SINGLE_SCAFFOLD_COLOR = "#2f6fd6"

# Render quality (combo label -> internal value), shared by the Generate dropdown and
# both viewer dropdowns so one vocabulary drives all three. "Lines" draws the sagged
# fiber CENTRELINES from the .depo cache; "Fibers" draws them as round swept tubes.
# Both generate WITHOUT building the STL mesh (the sag + .depo are written, the mesh
# skipped). "Full mesh" builds the exact STL, and Export always produces the exact
# mesh, on demand if needed.
RENDER_CHOICES = [
    ("Lines (instant)", "wireframe"),
    ("Fibers (fast)", "tube:6"),
    ("Fibers (smooth)", "tube:12"),
    ("Full mesh (slow)", "exact"),
]
RENDER_LBL2VAL = {lbl: val for lbl, val in RENDER_CHOICES}
RENDER_VAL2LBL = {val: lbl for lbl, val in RENDER_CHOICES}

# Sag solver (combo label -> internal value), shared by the Export panel and the
# G-code-import panel. "exact" -> the full contact solve (gcode_to_fibers.apply_sag);
# "approximate" -> the same physics with analytic pair contacts, ~5x faster
# (--sag-approx -> apply_sag_approx -> sag_approx.drape_sag); "off" -> --no-sag.
# NOT the scaffold-designer PREVIEW's sagging, which is a separate, far faster engine
# on its own toggle (preview_sag.py) and is unaffected by this.
SAG_SOLVER_CHOICES = [
    ("Exact", "exact"),
    ("Approximate (fast)", "approximate"),
    ("Off (straight bars)", "off"),
]
SAG_SOLVER_LBL2VAL = {lbl: val for lbl, val in SAG_SOLVER_CHOICES}
SAG_SOLVER_VAL2LBL = {val: lbl for lbl, val in SAG_SOLVER_CHOICES}

# Mesh detail (combo label -> --mesh-tolerance value, as a string so the persist map
# stores exactly what goes on the argv). A FILE-SIZE control, not a physics one: it
# decides how many of the sag solver's samples become tube rings in the STL, and
# nothing else reads it -- the .depo cache, and so the player, the deviation view and
# the Analyze engine, always carry the full sampling. "Full" is byte-for-byte the
# pre-mesh-detail behaviour.
MESH_DETAIL_CHOICES = [
    ("Standard", "1.0"),
    ("Smaller file", "4.0"),
    ("Full (largest file)", "0"),
]
MESH_DETAIL_LBL2VAL = {lbl: val for lbl, val in MESH_DETAIL_CHOICES}
MESH_DETAIL_VAL2LBL = {val: lbl for lbl, val in MESH_DETAIL_CHOICES}

# Sag resolution (combo label -> --sag-resolution factor, a string for the same
# reason as Mesh detail). How densely the SOLVE samples each drooping fibre -- the
# droop, contacts and rest heights are unchanged, only the drawn drape is smoother.
# Unlike Mesh detail it IS a .depo cache key, so it goes to the player too.
# "Standard" sends nothing: byte-for-byte the pre-control behaviour.
SAG_RESOLUTION_CHOICES = [
    ("Standard", "1"),
    ("Fine (about 3x slower)", "3"),
    ("Publication (about 10x slower)", "10"),
]
SAG_RESOLUTION_LBL2VAL = {lbl: val for lbl, val in SAG_RESOLUTION_CHOICES}
SAG_RESOLUTION_VAL2LBL = {val: lbl for lbl, val in SAG_RESOLUTION_CHOICES}
SAG_RESOLUTION_TIP = (
    "How finely the sag solver samples each sagging fiber. Finer draws a "
    "smoother drape, with no visible facets or small notches on a span's flank. "
    "The sag depth, where fibers touch and the height of each support are the "
    "same at every setting.\n\n"
    "Standard: the default, and what every earlier export used. Right beside a "
    "wall or crossing the fiber can sit up to about one fiber diameter lower "
    "than the finer settings draw it.\n\n"
    "Fine and Publication: 3x and 10x the samples, for close-up views and "
    "figures. The sag solve takes about that much longer, and the simulation "
    "re-solves it at the same setting.\n\n"
    "It does not round the corner where a fiber lands on the collector or on "
    "a fiber lying under it. That corner is in the sag model, not the sampling.")



def _field(parent, label, var, row, col, pad, width=12):
    ttk.Label(parent, text=label).grid(row=row, column=col, sticky="e", **pad)
    e = ttk.Entry(parent, textvariable=var, width=width)
    e.grid(row=row, column=col + 1, sticky="w", **pad)
    return e


def _parse_length_mm(text):
    """Parse a length into millimetres, accepting inches or mm.

    Recognised: '3in', '3 in', '3inch', '3"' (inches -> x25.4) and bare numbers
    or '76.2mm' (millimetres). Returns (value_mm, error) where exactly one is
    non-None."""
    s = text.strip().lower()
    if not s:
        return None, "is required"
    inch = False
    for suf in ('"', "inches", "inch", "in"):
        if s.endswith(suf):
            s, inch = s[:-len(suf)].strip(), True
            break
    else:
        if s.endswith("mm"):
            s = s[:-2].strip()
    try:
        v = float(s)
    except ValueError:
        return None, "must be a number (optionally with 'in' or 'mm')"
    if v <= 0:
        return None, "must be positive"
    return (v * 25.4 if inch else v), None


def enable_dpi_awareness():
    """Opt this process into the monitor's real DPI on Windows so Tk renders at the
    display's scaling instead of letting Windows bitmap-stretch a blurry, mis-sized
    96-DPI window. Without it a >100% display gets a stretched window AND breaks the
    relationship between point-sized fonts, which Windows scales, and any pixel-sized
    geometry the layout assumes.

    Must run BEFORE the Tk root is created. Guarded, and a no-op off Windows. Uses
    PROCESS_SYSTEM_DPI_AWARE (level 1) because Tk 8.6 does not handle the per-monitor
    (level 2) DPI-change events and would leave widgets mis-sized; falls back to the
    legacy user32 call on older Windows."""
    if not IS_WINDOWS:
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001  (no shcore on older Windows, etc.)
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


class VScrollFrame(ttk.Frame):
    """A vertically scrollable container so a tab's content can exceed the window
    height without being clipped. Build content into `.body`; it lays out like a
    normal frame (pass `padding=` for inner margins).

    The scrollbar appears only when the content overflows and hides itself when
    everything fits. The body's width tracks the viewport so children fill
    horizontally (no horizontal scrollbar). The mouse wheel scrolls only while the
    pointer is inside this frame, and only when there is overflow."""

    def __init__(self, parent, padding=0, **kw):
        super().__init__(parent, **kw)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self._canvas = tk.Canvas(self, highlightthickness=0, takefocus=0)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._vsb = ttk.Scrollbar(self, orient="vertical",
                                  command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._on_yscroll)
        # the frame callers build into; padding lives here so content isn't flush
        self.body = ttk.Frame(self._canvas, padding=padding)
        self._win = self._canvas.create_window((0, 0), window=self.body,
                                               anchor="nw")
        # content resized -> refresh the scrollable region
        self.body.bind("<Configure>", self._on_body_configure)
        # viewport resized -> keep the body exactly as wide as the canvas
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        # pointer-scoped wheel: grab it while inside, release on a real exit
        self._canvas.bind("<Enter>", self._bind_wheel)
        self._canvas.bind("<Leave>", self._unbind_wheel)

    def _on_body_configure(self, _event=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self._canvas.itemconfigure(self._win, width=event.width)

    def _on_yscroll(self, lo, hi):
        # auto-hide: show the bar only while the content overflows the viewport
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self._vsb.grid_remove()
        else:
            self._vsb.grid(row=0, column=1, sticky="ns")
        self._vsb.set(lo, hi)

    def _bind_wheel(self, _event=None):
        self._canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self, _event=None):
        # A <Leave> also fires when the pointer merely crosses onto a child of the
        # canvas -- we are still inside, so the wheel must keep working over the
        # fields. Tkinter does not expose the crossing detail, so decide by where the
        # pointer actually is.
        try:
            under = self.winfo_containing(*self.winfo_pointerxy())
        except (KeyError, tk.TclError):
            under = None
        if under is not None:
            here = str(self)
            if str(under) == here or str(under).startswith(here + "."):
                return
        self._canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event):
        lo, hi = self._canvas.yview()
        if lo <= 0.0 and hi >= 1.0:
            return  # everything fits; nothing to scroll
        self._canvas.yview_scroll(int(-event.delta / 120), "units")


# Explicit so `from gui_common import *` also pulls the stdlib/tk aliases and the
# underscore helpers the mixins reference unqualified.
__all__ = [
    "glob", "json", "os", "queue", "re", "subprocess", "sys", "threading",
    "tk", "filedialog", "messagebox", "simpledialog", "ttk", "shutil",
    "HERE", "USER_DATA_DIR", "resource_path", "user_data_dir",
    "WORKER", "SCAFFOLD_WORKER", "VIEWER", "PLAYER", "worker_cmd",
    "WORKER_PIPE_UTF8",
    "PRESETS_ROOT", "SHIPPED_PRESETS_ROOT", "SHIPPED_PRESETS_PATH",
    "migrate_user_presets_once", "PRESETS_DIR", "MACHINE_PRESETS_DIR",
    "CALIBRATION_PRESETS_DIR",
    "FLAVOR_PRESETS_DIR",
    "SWEEP_PRESETS_DIR", "SWEEP_KIND_PREFIX",
    "migrate_preset_layout", "migrate_preset_layout_once",
    "migrate_preset_folders", "migrate_preset_folders_once",
    "PRESETS_PATH", "DEPO_CACHE_EXT",
    "IS_WINDOWS",
    "OUTPUTS_DIR", "SESSION_DIR", "prepare_session_dir",
    "ROTATION_PRESETS", "SCAFFOLD_COLORS", "SCAFFOLD_SPIN_MAX",
    "SINGLE_SCAFFOLD_COLOR",
    "STAB_COLOR_HEX",
    "RENDER_CHOICES", "RENDER_LBL2VAL", "RENDER_VAL2LBL",
    "SAG_SOLVER_CHOICES", "SAG_SOLVER_LBL2VAL", "SAG_SOLVER_VAL2LBL",
    "MESH_DETAIL_CHOICES", "MESH_DETAIL_LBL2VAL", "MESH_DETAIL_VAL2LBL",
    "SAG_RESOLUTION_CHOICES", "SAG_RESOLUTION_LBL2VAL", "SAG_RESOLUTION_VAL2LBL",
    "SAG_RESOLUTION_TIP",
    "_field", "_parse_length_mm",
    "enable_dpi_awareness", "VScrollFrame",
]
