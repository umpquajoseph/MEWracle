#!/usr/bin/env python3
"""gui_prefs.py -- the per-user preferences file, `gui_prefs.json`: one reader
and one merging writer for everything the Settings panel keeps.

What lives here is about the PERSON, not the design: dark mode, the display
units, the default Save-as folder. A design's own numbers stay in its presets,
in their canonical units, whatever this file says.

Every write is read-modify-write. Before this module the dark-mode toggle
rewrote the whole file as ``{"dark_mode": ...}``, which would have wiped every
other setting the first time the theme was flipped.

No Tk: the viewer child processes read it too (`gui_viewer_shell`), and they
must find it where the App writes it. From source that is the repo dir; frozen
it is ``%LOCALAPPDATA%\\MEWracle`` (`app_paths.user_data_dir`).

`PATH` is a module attribute on purpose: a test points it at a scratch file so
flipping a setting never changes the next real launch.
"""
import json
import os

import app_paths

PATH = os.path.join(app_paths.user_data_dir(), "gui_prefs.json")


def load():
    """The whole file as a dict; {} on a missing or corrupt file."""
    try:
        with open(PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def get(key, default=None):
    return load().get(key, default)


def update(**values):
    """Merge `values` into the file. Best-effort: returns False if it could
    not be written, and never raises."""
    data = load()
    data.update(values)
    try:
        with open(PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        return False
    return True


def save_dir_kw():
    """``{"initialdir": folder}`` for a save dialog when the Settings panel
    names a folder that exists, else {} so the dialog keeps opening where the
    user last saved. Spread into every asksaveasfilename call."""
    d = get("save_dir", "") or ""
    return {"initialdir": d} if d and os.path.isdir(d) else {}


# -- "Don't show again" ------------------------------------------------------ #
# A popup that only informs or advises can be hidden for good from its own
# "Don't show again" choice. Each popup has its own key under HIDDEN_KEY, so
# one can be hidden without the others; Settings -> General -> Messages
# brings every one back (`reset_hidden`). Popups that guard work, a real
# printer, or report an error never get a key: hiding those would decide
# for the person later without asking.
HIDDEN_KEY = "hidden_popups"

# The other once-only flags the reset also clears: the Generate banner shows
# on the first Generate only (`gui_shell._generate_next_step`).
ONCE_KEYS = ("generate_banner_seen",)


def is_hidden(key):
    """True when the person chose "Don't show again" on popup `key`."""
    hidden = get(HIDDEN_KEY, {})
    return bool(key) and isinstance(hidden, dict) and bool(hidden.get(key))


def hide(key):
    """Remember "Don't show again" for popup `key`."""
    hidden = get(HIDDEN_KEY, {})
    hidden = dict(hidden) if isinstance(hidden, dict) else {}
    hidden[key] = True
    return update(**{HIDDEN_KEY: hidden})


def hidden_count():
    """How many popups are hidden now, the once-only flags included."""
    data = load()
    hidden = data.get(HIDDEN_KEY, {})
    n = sum(1 for v in hidden.values() if v) if isinstance(hidden, dict) else 0
    return n + sum(1 for k in ONCE_KEYS if data.get(k))


def reset_hidden():
    """Every hidden popup shows again: clears HIDDEN_KEY and ONCE_KEYS."""
    values = {HIDDEN_KEY: {}}
    values.update({k: False for k in ONCE_KEYS})
    return update(**values)
