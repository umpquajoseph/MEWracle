#!/usr/bin/env python3
"""gui_gcode_work.py -- MODERN-UI ONLY: keeping the G-code page's WORK safe.

What a scientist with a real print at stake needs from the page, beyond
editing: to know what is saved and what is not, to get the work back after a
crash, to undo from anywhere, to see what the print costs, to copy a part,
and to send the file they are LOOKING AT to the printer. One mixin
(`_GcodeWorkMixin`, before `_GcodeMoveMixin` in `ModernApp`'s MRO) so none of
it spreads through the editor and changes halves.

  * UNSAVED WORK. `_gw_key` is a fingerprint of the page's work (its design
    vars, its document, its text; an opened file's text). `_gw_base` is that
    fingerprint at the last save, load or open. Unsaved means they differ, so
    opening the page or a regeneration that changes nothing never counts. The
    tree's title bar says it (`gce_work_var`: the preset the page is tied to, and
    whether there are unsaved changes).
  * CTRL+S saves the page's DESIGN preset (kind "scaffold": it carries
    `gce.document`, the edits, and a copy of the Physics page it prints
    with, `_preset_data_for_kind`), inside the page's own design scope
    (`_gce_preset_scope`) -- going around that scope would save the DESIGNER's
    design. First save asks for a name; once the page is tied to a preset (saved
    or loaded) it offers Overwrite / Save as new / Cancel, and never offers to
    overwrite a read-only preset. An opened file has no design preset: Ctrl+S
    there is Save g-code. CTRL+O is Open file.
  * THE PROMPT (Save / Don't save / Cancel) comes before anything that would
    REPLACE the work: quitting, Open file, loading a preset. Cancel cancels.
    Quitting also offers to keep Physics page values the next start would
    not load (`_gw_keep_physics`).
  * AUTOSAVE keeps the unsaved work in `autosave/` (atomic, off the Tk thread)
    while it is unsaved; the next launch OFFERS to restore it, saying what it is
    and when it was saved. A clean exit, a save and an explicit Don't save
    leave nothing behind.
  * THE TRAP: a modal prompt on close would hang a test rig or capture_ui.
    Prompts, autosave and the recovery offer exist only when the app is
    INTERACTIVE -- `GW_INTERACTIVE`, set by `mewracle_gui.main()` and never by
    a rig that builds `ModernApp(root)` itself -- and never on a withdrawn
    window. The real launch always has them.
  * UNDO / REDO: Ctrl+Z, Ctrl+Y, Ctrl+Shift+Z anywhere on the page (after a
    click on the picture, a ribbon tool, empty space), never on another page,
    never inside an entry box or the text (which keep their own).
  * JOB STATS (`gce_stats_var`): time, fiber length and volume of the print
    THE PAGE would send, edits included; each number is shown only while it
    belongs to the text on screen.
  * COPY / PASTE / DUPLICATE (Ctrl+C, Ctrl+V, Ctrl+K; the
    tree's right-click menu; no ribbon buttons): a copied scaffold pastes as an ordinary EDITABLE
    scaffold, anything else as a frozen piece (Move's Make a copy). Move
    opens on the result.
  * SEND: `_gce_send` uploads `_gce_save_ready(text)` -- the bytes Save
    writes, after the flavor gap check -- then offers to start.
  * OUT OF DATE (for the Machine link): `_gw_stale_reason`. A Generate is
    stamped when it lands (`_gw_result`); the printer path is stale when the
    page's text (a Generate on the page) or the designer's design (a Generate
    there) is no longer the one it was made from. A result nobody stamped
    (a calibration print, a file the app was handed) is never called stale.
"""

from gui_common import *

import hashlib
import json
import math
import os
import queue
import re
import threading
import time

import moonraker_link as ml

import gui_fonts
import gui_units
from gui_tips import add_tip
from gui_gcode_twodesigns import _is_value


_TYPING = ("Text", "Entry", "TEntry", "TCombobox", "TSpinbox", "Spinbox",
           "Listbox")
_AUTOSAVE_FILE = "gcode_page_autosave.json"
_AUTOSAVE_EVERY_S = 4.0
_TICK_MS = 1000


class _Val:
    """A value with `.get()`, so `_gce_copy_vars` can read a snapshot."""

    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v


def _sha(text):
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()


class _GcodeWorkMixin:
    GW_INTERACTIVE = False

    # ------------------------------------------------------------------ #
    # setup: keys, the close request, the tick
    # ------------------------------------------------------------------ #
    def _bind_shell_keys(self):
        super()._bind_shell_keys()
        self._gw_install()

    def _gw_state_init(self):
        if getattr(self, "_gw_ready", False):
            return
        self._gw_ready = True
        self._gw_base = None            # the key at the last save/load/open
        self._gw_tie = None             # {"name": ...} the preset tied to
        self._gw_rebase_wait = False
        self._gw_clip = None
        self._gw_post = None
        self._gw_pending = None         # a Generate pressed, not landed
        self._gw_result = None          # the stamp of the last Generate
        self._gw_offer_start = None
        self._gw_auto_key = None
        self._gw_auto_t = 0.0
        self._gw_auto_busy = False
        self._gw_recovery_open = False
        self._gw_recovery_done = not self.GW_INTERACTIVE
        self._gw_stats = None
        self._gw_stats_q = queue.Queue()
        self._gw_stats_want = None
        self._gw_tick_id = None
        self._gw_stale_seen = None

    def _gw_install(self):
        self._gw_state_init()
        self._gw_track_collector()
        r = self.root
        for seq, redo in (("<Control-z>", False), ("<Control-y>", True),
                          ("<Control-Z>", True)):
            r.bind_all(seq, lambda e, redo=redo: self._gw_key_undo(e, redo),
                       add="+")
        r.bind_all("<Control-s>", self._gw_key_save, add="+")
        r.bind_all("<Control-o>", self._gw_key_open, add="+")
        r.bind_all("<Control-c>", lambda e: self._gw_key_clip(e, "copy"),
                   add="+")
        r.bind_all("<Control-v>", lambda e: self._gw_key_clip(e, "paste"),
                   add="+")
        r.bind_all("<Control-k>", lambda e: self._gw_key_clip(e, "dup"),
                   add="+")
        text = getattr(getattr(self, "gce", None), "text", None)
        if text is not None:
            # Tk's Text opens a line on Ctrl+O; here it is Open file
            text.bind("<Control-o>", self._gw_key_open)
        r.protocol("WM_DELETE_WINDOW", self._gw_close_request)
        if self._gw_tick_id is None:
            self._gw_tick_id = r.after(_TICK_MS, self._gw_tick)
        if self.GW_INTERACTIVE:
            r.after(2500, self._gw_offer_recovery)

    def _gw_interactive(self):
        """Prompts, autosave and the recovery offer: the real app only."""
        try:
            return bool(self.GW_INTERACTIVE) and \
                self.root.state() != "withdrawn"
        except tk.TclError:
            return False

    def _gw_settled(self):
        return (getattr(self, "_gce_regen", None) is None
                and getattr(self, "_gce_d_after", None) is None
                and getattr(self, "_gce_model", None) is not None
                and getattr(self, "_gce_restore", None) is None
                and not getattr(self, "_gce_pending_design", None))

    # ------------------------------------------------------------------ #
    # work state and job stats, which the tree's title bar shows
    # ------------------------------------------------------------------ #
    def _gw_build_state(self):
        self._gw_state_init()
        self.gce_work_var = tk.StringVar(value="")
        self.gce_stats_var = tk.StringVar(value="")

    # ------------------------------------------------------------------ #
    # unsaved work
    # ------------------------------------------------------------------ #
    def _gw_is_file(self):
        return self._gce_doc["source"] == self._GCE_FILE

    def _gw_key(self):
        """The fingerprint of the page's work (see the module docstring).
        Inside one `_gw_tick` it is worked out once (`_gw_key_memo`)."""
        memo = getattr(self, "_gw_key_memo", None)
        if memo is not None:
            if not memo:
                memo.append(self._gw_key_now())
            return memo[0]
        return self._gw_key_now()

    def _gw_key_now(self):
        doc = self._gce_doc
        text = self.gce.get_text()
        if doc["source"] == self._GCE_FILE:
            return _sha("file\0%s\0%s" % (doc.get("path") or "", text))
        try:
            st = self._gce_d_state()
            design = json.dumps({"top": st.get("top"),
                                 "scaf": st.get("scaf")},
                                sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            design = ""
        try:
            ds = self._gce_doc_state()
        except Exception:  # noqa: BLE001
            ds = ""
        flavor = self.gce_flavor.get() if getattr(
            self, "_gce_flavor_picked", False) else ""
        return _sha("\0".join((doc["source"] or "", design, ds, flavor, text)))

    def _gw_dirty(self, fresh=False):
        """Real changes against the last save, load or open. `fresh`
        records typing nobody has recorded yet first."""
        if getattr(self, "_gw_base", None) is None or not hasattr(
                self, "gce"):
            return False
        self._gw_fresh_landed()
        if self._gw_rebase_wait and self._gw_settled():
            # a preset load has landed: its result is the saved state
            self._gw_rebase_wait = False
            self._gw_rebase()
        if fresh:
            try:
                self._gce_record_edits()
            except Exception:  # noqa: BLE001
                pass
        return self._gw_key() != self._gw_base

    def _gw_rebase(self):
        self._gw_base = self._gw_key()
        self._gw_auto_key = None
        self._gw_label_refresh()

    def _gw_label_refresh(self):
        var = getattr(self, "gce_work_var", None)
        if var is None:
            return
        tie = getattr(self, "_gw_tie", None)
        if tie and self._gw_store() is not None                 and self._gw_preset_index(tie["name"]) is None:
            self._gw_tie = None     # deleted or renamed away: no wrong tie
        dirty = self._gw_dirty()
        if self._gw_is_file():
            text = "● File edited, unsaved (Ctrl+S)" if dirty else ""
        else:
            name = (self._gw_tie or {}).get("name")
            if dirty:
                text = ("● “%s” · unsaved (Ctrl+S)" % name) if name                     else "● Unsaved (Ctrl+S)"
            else:
                text = ("“%s” · saved" % name) if name else ""
        # the design a launch opens on is the startup preset, not the
        # user's work: say so until the first change, a save or an open
        if dirty or tie or self._gw_is_file():
            self._gw_fresh = False
        boot = getattr(self, "_boot_design_preset", None)
        if not text and boot and getattr(self, "_gw_fresh", True):
            text = "“%s” · starting design" % boot.get("name", "")
        if var.get() != text:
            var.set(text)

    # ------------------------------------------------------------------ #
    # dialogs (replaceable: a test hands in the answer)
    # ------------------------------------------------------------------ #
    def _gw_dialog(self, title, text, buttons, default=0, cancel="cancel",
                   entry=None, dont_ask=None, remember=()):
        """A modal choice. `buttons`: [(key, label)]. Returns the key, or
        ``(key, typed)`` with `entry`. Esc and the window's close are
        `cancel`. Never raised on a non-interactive app.
        `dont_ask`: a gui_prefs key. The dialog then has a "Don't ask again"
        box, and ticking it before one of the `remember` buttons (the
        go-ahead answers) stores the key, so the caller skips the question
        from then on (`gui_prefs.is_hidden`). Any other answer stores
        nothing: a hidden question never silently says no."""
        if not self._gw_interactive():
            return (cancel, None) if entry is not None else cancel
        top = tk.Toplevel(self.root)
        top.title(title)
        top.transient(self.root)
        top.resizable(False, False)
        out = {"key": cancel}
        body = ttk.Frame(top, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=text, wraplength=420, justify="left").pack(
            anchor="w")
        var = tk.StringVar(value=entry or "")
        ent = None
        if entry is not None:
            ent = ttk.Entry(body, textvariable=var, width=44)
            ent.pack(anchor="w", pady=(10, 0))
        never = tk.BooleanVar(value=False)
        if dont_ask:
            ttk.Checkbutton(body, text="Don't ask again", variable=never
                            ).pack(anchor="w", pady=(10, 0))
        row = ttk.Frame(body)
        row.pack(anchor="e", pady=(14, 0))

        def done(k):
            out["key"] = k
            if dont_ask and never.get() and k in remember:
                import gui_prefs
                gui_prefs.hide(dont_ask)
            top.destroy()
        btns = []
        for i, (k, lab) in enumerate(buttons):
            b = ttk.Button(row, text=lab, command=lambda k=k: done(k))
            b.pack(side="left", padx=(6, 0))
            btns.append(b)
        top.bind("<Escape>", lambda _e: done(cancel))
        top.protocol("WM_DELETE_WINDOW", lambda: done(cancel))
        top.bind("<Return>", lambda _e: done(buttons[default][0]))
        top.update_idletasks()
        try:
            x = self.root.winfo_rootx() + max(
                0, (self.root.winfo_width() - top.winfo_reqwidth()) // 2)
            y = self.root.winfo_rooty() + max(
                0, (self.root.winfo_height() - top.winfo_reqheight()) // 3)
            top.geometry("+%d+%d" % (x, y))
        except tk.TclError:
            pass
        top.grab_set()
        (ent or btns[default]).focus_set()
        top.wait_window()
        if entry is not None:
            return out["key"], var.get()
        return out["key"]

    def _gw_ask_unsaved(self, what, physics=None):
        """Save / Don't save / Cancel before `what` replaces the work.
        `physics`: the Physics page rows the next start would lose
        (`_gw_unkept_rows`), when quitting; Save then keeps them too, so
        quitting asks one question, not two."""
        also = ""
        if physics:
            also = ("\n\nThe Physics page also has values the next start "
                    "will not load:\n\n%s\n\nSave keeps them as the "
                    "physics preset “%s”, which then loads at startup."
                    % (self._cal_bullets(physics), self._gw_kept_name()))
        if self._gw_is_file():
            line = ("The file you opened has edits that are not saved. "
                    "Save them as g-code first?%s" % also)
        else:
            name = (self._gw_tie or {}).get("name")
            line = ("This page's design and g-code changes are not saved%s."
                    " Save them as a preset first?%s"
                    % ((" to “%s”" % name) if name else "", also))
        return self._gw_dialog(
            "Unsaved work",
            # the quit dialog needs no closing line; the others say what the
            # answer is about to replace
            line if what == "quit" else "%s\n\nYou are about to %s." % (
                line, what),
            [("save", "Save"), ("discard", "Don't save"),
             ("cancel", "Cancel")], default=0, cancel="cancel")

    def _gw_confirm_replace(self, what):
        """True to go ahead with something that replaces the page's work:
        nothing unsaved, or the answer to the prompt allows it."""
        if not self._gw_interactive() or not self._gw_dirty(fresh=True):
            return True
        ans = self._gw_ask_unsaved(what)
        if ans == "save":
            return bool(self._gw_save())
        if ans == "discard":
            self._gw_clear_autosave()
            return True
        return False

    # ------------------------------------------------------------------ #
    # Ctrl+S, Ctrl+O and the presets
    # ------------------------------------------------------------------ #
    def _gw_in_root(self, e):
        try:
            return e.widget.winfo_toplevel() is self.root
        except (tk.TclError, AttributeError, KeyError):
            return False

    def _gw_typing(self, e):
        try:
            return e.widget.winfo_class() in _TYPING
        except (tk.TclError, AttributeError):
            return True

    def _gw_key_ok(self, e):
        return self._gce_page_shown() and self._gw_in_root(e)

    def _gw_key_save(self, e):
        if not self._gw_key_ok(e):
            return None
        self._gw_save()
        return "break"

    def _gw_key_open(self, e):
        if not self._gw_key_ok(e):
            return None
        self._gce_open_file()
        return "break"

    def _gw_store(self):
        return (getattr(self, "_pstores", None) or {}).get("scaffold")

    def _gw_readonly(self, preset):
        fn = preset.get("_file")
        st = self._gw_store()
        if preset.get("builtin") or preset.get("readonly"):
            return True
        if fn and st is not None:
            path = os.path.join(st["dir"], fn)
            return os.path.isfile(path) and not os.access(path, os.W_OK)
        return False

    def _gw_snapshot(self):
        """The page's design as a preset stores it: under the page's own
        design scope, the document first brought up to date."""
        self._gce_record_edits()
        self._gce_sync_persist()
        scope = getattr(self, "_gce_preset_scope", None)
        if scope is None:
            return self._preset_data_for_kind("scaffold")
        with scope():
            return self._preset_data_for_kind("scaffold")

    def _gw_say(self, text, buttons=(), kind="work", ms=0, hide_key=None):
        if hasattr(self, "_gce_notice_show"):
            shown = self._gce_notice_show(text, list(buttons), kind=kind,
                                          hide_key=hide_key)
            if ms and shown:
                self.root.after(ms, lambda: self._gce_notice_hide_if(kind))

    def _gw_save(self):
        """Ctrl+S. True when the work was saved, False when it was not
        (cancelled, or it could not be written)."""
        if not hasattr(self, "gce"):
            return False
        if self._gw_is_file():
            before = getattr(self, "_gw_saved_marker", 0)
            self.save_gcode_as()
            if getattr(self, "_gw_saved_marker", 0) != before:
                self._gw_rebase()
                return True
            return False
        st = self._gw_store()
        if st is None:
            self._gw_say("Presets are not available in this build.")
            return False
        tie = self._gw_tie
        idx = self._gw_preset_index(tie["name"]) if tie else None
        if idx is not None:
            name = st["presets"][idx]["name"]
            ro = self._gw_readonly(st["presets"][idx])
            text = ("Save this page's design as “%s” again, or as a new "
                    "preset?" % name)
            buttons = [("overwrite", "Overwrite “%s”" % name),
                       ("new", "Save as new…"), ("cancel", "Cancel")]
            if ro:
                text = ("“%s” is read-only, so it cannot be overwritten. "
                        "Save this page's design as a new preset?" % name)
                buttons = [("new", "Save as new…"), ("cancel", "Cancel")]
            ans = self._gw_dialog("Save design", text, buttons, default=0,
                                  cancel="cancel")
            if ans == "overwrite":
                return self._gw_write_preset(idx)
            if ans != "new":
                return False
        return self._gw_save_new(
            initial="" if idx is None else self._unique_preset_name(
                "scaffold", st["presets"][idx]["name"] + " copy"))

    def _gw_preset_index(self, name):
        st = self._gw_store()
        if st is None or not name:
            return None
        return self._preset_name_index("scaffold", name)

    def _gw_save_new(self, initial=""):
        st = self._gw_store()
        while True:
            ans = self._gw_dialog(
                "Save design as a preset",
                "A name for this design. It is saved with its g-code "
                "changes, and Ctrl+S will offer to update it from then on.",
                [("ok", "Save"), ("cancel", "Cancel")], default=0,
                cancel="cancel", entry=initial)
            key, name = ans if isinstance(ans, tuple) else (ans, "")
            name = (name or "").strip()
            if key != "ok":
                return False
            if not name:
                self._gw_say("A preset needs a name.", kind="work", ms=5000)
                initial = ""
                continue
            clash = self._preset_name_index("scaffold", name)
            if clash is None:
                st["presets"].append({"name": name, "data": {}})
                return self._gw_write_preset(len(st["presets"]) - 1,
                                             new=True)
            if self._gw_readonly(st["presets"][clash]):
                self._gw_say("“%s” is a read-only preset: pick another "
                             "name." % name, kind="work", ms=6000)
                initial = name + " copy"
                continue
            ok = self._gw_dialog(
                "Overwrite preset",
                "A preset named “%s” already exists. Replace it with this "
                "design?" % name,
                [("yes", "Overwrite"), ("no", "Pick another name")],
                default=1, cancel="no")
            if ok == "yes":
                return self._gw_write_preset(clash)
            initial = name

    def _gw_write_preset(self, idx, new=False):
        st = self._gw_store()
        preset = st["presets"][idx]
        data = self._gw_snapshot()
        old = preset.get("data")
        preset["data"] = data
        if not self._write_preset_file("scaffold", preset):
            preset["data"] = old
            if new:
                st["presets"].pop(idx)
            self._gw_say("The preset could not be written to %s. Nothing "
                         "was saved." % st["dir"], kind="work")
            return False
        self._revert_rebaseline(self._keys_for_kind("scaffold"))
        kept = self._preset_kept_physics(preset)
        self._commit_presets("scaffold", idx,
                             "Saved design preset “%s”.%s"
                             % (preset["name"], kept))
        self._gw_tie = {"name": preset["name"]}
        self._gw_rebase()
        self._gw_clear_autosave()
        self._gw_say("Saved the design as preset “%s”.%s"
                     % (preset["name"], kept),
                     [("OK", self._gce_notice_hide)], kind="work-saved",
                     ms=5000, hide_key="preset-saved")
        return True

    def _gw_saved_gcode(self):
        """Called by Save g-code once it has written the file."""
        self._gw_saved_marker = getattr(self, "_gw_saved_marker", 0) + 1

    # presets: the page's work is replaced by a load --------------------- #
    def _preset_load(self, kind):
        if kind == "scaffold" and hasattr(self, "gce") \
                and self._selected_preset_index(kind) is not None:
            if not self._gw_confirm_replace("load another preset"):
                return None
            idx = self._selected_preset_index(kind)
            name = self._pstores[kind]["presets"][idx]["name"]
            r = super()._preset_load(kind)
            if r is False:      # cancelled at the physics question
                return r
            self._gw_tie = {"name": name}
            self._gw_rebase_wait = True
            self._gw_clear_autosave()
            # a loaded preset is a new design, not a tweak: its first
            # picture frames it (`PreviewPanel` keeps the camera unless the
            # size changes by a fifth, so a design placed elsewhere on the
            # plate opened half out of view). Taken by `_gce_pv_apply`.
            self._gce_pv_reframe = True
            return r
        return super()._preset_load(kind)

    # the collector: a click loads that mode's design ----------------------- #
    def _gw_track_collector(self):
        """Remember the collector before the last change, so a click whose
        load is cancelled can put the radio back (the radio writes the var
        before its command runs)."""
        coll = getattr(self, "s_collector", None)
        if coll is None or getattr(self, "_gw_coll_traced", False):
            return
        self._gw_coll_traced = True
        self._gw_coll_now, self._gw_coll_prev = coll.get(), None

        def seen(*_a):
            v = coll.get()
            if v != self._gw_coll_now:
                self._gw_coll_prev, self._gw_coll_now = self._gw_coll_now, v
        coll.trace_add("write", seen)

    def _collector_mode_chosen(self):
        """A collector click loads that mode's default printer and design
        (`gui_presets._collector_mode_chosen`), so it replaces the page's work
        the way loading a preset does: it asks first, and the page is then
        that design's (its name in the title bar, framed, nothing unsaved).
        The plate's "Demo: Two tubes" read "unsaved" over the mandrel's
        default design, so Ctrl+S offered to save it under the demo's name."""
        if not hasattr(self, "gce"):
            return super()._collector_mode_chosen()
        mode = self.s_collector.get()
        st = getattr(self, "_pstores", {}).get("scaffold")
        name = None
        if st and self._is_per_axis(st):
            name = st["defaults"].get(mode)
            if name and self._gw_preset_index(name) is None:
                name = None
        if name is None:
            return super()._collector_mode_chosen()     # the design stays
        prev = getattr(self, "_gw_coll_prev", None)
        if prev not in (None, mode) and \
                not self._gw_confirm_replace("switch the collector"):
            self.s_collector.set(prev)      # the click is taken back
            # the switch blanked the picture for the other collector
            self._gce_pv_schedule(now=True)
            return None
        r = super()._collector_mode_chosen()
        idx = self._gw_preset_index(name)
        if idx is not None:
            self._gw_tie = {"name": st["presets"][idx]["name"]}
            self._gw_rebase_wait = True
            self._gw_clear_autosave()
            self._gce_pv_reframe = True
        # Undo does not cross the switch: its steps restore one collector's
        # design vars under the other's (after plate -> mandrel -> plate,
        # Undo laid the Mandrel default's numbers on the plate)
        self.gce._undo.clear()
        self.gce._redo.clear()
        self._gw_fresh_text = self.gce.get_text()
        return r

    def _gw_fresh_landed(self):
        """A collector switch's design is in: Undo starts from it (the step
        that brought it in would put the other collector's text back under
        this one's design)."""
        before = getattr(self, "_gw_fresh_text", None)
        if before is None or not self._gw_settled()                 or self.gce.get_text() == before:
            return
        self._gw_fresh_text = None
        self.gce._undo.clear()
        self.gce._redo.clear()

    # the Presets panel's own Save / Update / Rename / Delete -------------- #
    def _gw_panel_ready(self, kind):
        return kind == "scaffold" and hasattr(self, "gce")             and self._gce_page_shown()

    def _preset_save_new(self, kind):
        if self._gw_panel_ready(kind):
            self._gce_record_edits()
            self._gce_sync_persist()
        return super()._preset_save_new(kind)

    def _preset_update(self, kind):
        if self._gw_panel_ready(kind):
            self._gce_record_edits()
            self._gce_sync_persist()
        return super()._preset_update(kind)

    def _commit_presets(self, kind, idx, msg=None, *a, **k):
        r = super()._commit_presets(kind, idx, msg, *a, **k)
        if self._gw_panel_ready(kind) and isinstance(msg, str)                 and msg.startswith(("Saved ", "Updated ")):
            try:
                name = self._pstores[kind]["presets"][idx]["name"]
            except (IndexError, KeyError):
                return r
            # the page's work is now this preset: tied, and nothing unsaved
            self._gw_tie = {"name": name}
            self._gw_rebase()
            self._gw_clear_autosave()
        return r

    def _preset_rename(self, kind):
        tie = (self._gw_tie or {}).get("name") if hasattr(
            self, "_gw_tie") else None
        idx = self._selected_preset_index(kind) if tie else None
        r = super()._preset_rename(kind)
        if tie and idx is not None and kind == "scaffold":
            try:
                now = self._pstores[kind]["presets"][idx]["name"]
            except (IndexError, KeyError):
                return r
            if now != tie and tie.lower() != now.lower()                     and self._gw_preset_index(tie) is None:
                self._gw_tie = {"name": now}    # the tie follows the rename
        return r

    # Open file ----------------------------------------------------------- #
    def _gce_open_file(self):
        path = filedialog.askopenfilename(
            title="Open a g-code file",
            filetypes=[("G-code", "*.gcode *.gco *.g *.nc *.txt"),
                       ("All files", "*.*")])
        if not path or not self._gw_confirm_replace("open another file"):
            return
        self.in_var.set(path)
        if self._gw_open_prepared(path):
            return          # it loads when the worker has prepared it
        self._gw_confirmed = True
        try:
            self._gce_load_file(path)
        finally:
            self._gw_confirmed = False

    def _gw_open_prepared(self, path):
        """A large file's read, document and render run on a worker
        (`_gce_prepare_file`), the window answering meanwhile under the
        "Opening..." notice; the load then takes them. A 120,000-line file
        held the window about 3 s for that. True when the worker was started;
        a small file, or a build without the engine, loads at once."""
        if self._gce_engine() is None \
                or not hasattr(self, "_gce_prepare_file") \
                or not self._gw_busy_open(path):
            return False
        token = object()
        self._gw_opening = token
        unit = (self._feed_time_unit() if hasattr(self, "_feed_time_unit")
                else "min")
        box = {}

        def work():
            try:
                box["pre"] = self._gce_prepare_file(path, unit)
            except Exception:  # noqa: BLE001 -- the load reads it again,
                box["pre"] = None   # and says what is wrong in its words
        th = threading.Thread(target=work, daemon=True)
        th.start()

        def poll():
            if th.is_alive():
                self.root.after(50, poll)
                return
            if getattr(self, "_gw_opening", None) is not token:
                return              # another file was opened meanwhile
            self._gw_opening = None
            self._gce_prepared = box.get("pre")
            self._gw_confirmed = self._gw_busy_shown = True
            try:
                self._gce_load_file(path)
            finally:
                self._gw_confirmed = self._gw_busy_shown = False
                self._gce_prepared = None
                self._gw_busy_done()
        self.root.after(50, poll)
        return True

    def _gce_load_file(self, path):
        if not getattr(self, "_gw_confirmed", False) \
                and not self._gw_confirm_replace("open another file"):
            return
        self._gw_confirmed = False
        # File > Open has had its notice up since the worker started
        busy = (not getattr(self, "_gw_busy_shown", False)
                and self._gw_busy_open(path))
        try:
            super()._gce_load_file(path)
        finally:
            if busy:
                self._gw_busy_done()
        if self._gce_doc["source"] == self._GCE_FILE:
            self._gw_tie = None
            self._gw_clear_autosave()
            self._gw_rebase()

    _GW_BUSY_BYTES = 500_000

    def _gw_busy_open(self, path):
        """A large file holds the window while it is read and parsed (a
        120,000-line program takes seconds). Say so before that starts --
        the notice and a busy cursor are painted now, and stay on screen
        until the file is in -- so the wait never reads as a hang. True when
        it was said."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        if size < self._GW_BUSY_BYTES or not hasattr(self, "gce"):
            return False
        try:
            if not self._gce_page_shown():
                self._nav_select("gcode")
            self._gce_notice_show(
                "Opening “%s” (%.1f MB). A large file takes a few seconds…"
                % (os.path.basename(path), size / 1e6), [], kind="opening")
            self.root.configure(cursor="watch")
            self.root.update_idletasks()
        except tk.TclError:
            return False
        return True

    def _gw_busy_done(self):
        try:
            self.root.configure(cursor="")
        except tk.TclError:
            pass
        self._gce_notice_hide_if("opening")

    # quit ----------------------------------------------------------------- #
    def _gw_close_request(self):
        """The window's close button: the unsaved prompt first, then the
        ordinary close (which may ask about a running job)."""
        if self._gw_interactive() and self._gw_dirty(fresh=True):
            rows = self._gw_unkept_rows()
            ans = self._gw_ask_unsaved("quit", physics=rows)
            if ans == "save":
                if not self._gw_save():
                    return
                if rows and not self._gw_keep_physics_now():
                    return
            elif ans != "discard":
                return
        elif self._gw_interactive() and not self._gw_keep_physics():
            return
        self.on_close()
        try:
            alive = bool(self.root.winfo_exists())
        except tk.TclError:
            alive = False
        if alive:
            # the close itself was declined (a running job): keep working,
            # and keep the autosave current
            self._gw_auto_key = None
        else:
            self._gw_clear_autosave()

    def _gw_kept_name(self):
        return getattr(self, "KEPT_PHYSICS_PRESET", "My calibration")

    def _gw_unkept_rows(self):
        """The Physics page values the next start would not load
        (`_cal_unkept`), one readable row each; [] when there are none."""
        unkept = getattr(self, "_cal_unkept", None)
        diffs = unkept() if callable(unkept) else []
        return [self._cal_diff_line(k, page, nxt, "now", "at the next start")
                for k, nxt, page in diffs]

    def _gw_keep_physics_now(self):
        """Keep the Physics page for the next start. False, said, when it
        could not be written."""
        if self._cal_keep_applied():
            return True
        self._gw_say("The Physics page could not be saved as a preset, "
                     "so nothing was closed.", kind="work")
        return False

    def _gw_keep_physics(self):
        """Before quitting with no unsaved design: offer to keep the Physics
        page values the next start would not load. Keep writes them to the
        physics preset Apply keeps itself in, which then loads at startup.
        False when the close is cancelled."""
        rows = self._gw_unkept_rows()
        if not rows:
            return True
        ans = self._gw_dialog(
            "Physics page not kept",
            "The Physics page has values the next start will not load:\n\n"
            "%s\n\nKeep them? They are saved as the physics preset “%s”, "
            "which then loads at startup."
            % (self._cal_bullets(rows), self._gw_kept_name()),
            [("keep", "Keep"), ("discard", "Don't keep"),
             ("cancel", "Cancel")], default=0, cancel="cancel")
        if ans == "keep":
            return self._gw_keep_physics_now()
        return ans == "discard"

    def _gw_boot_physics(self):
        """At launch, after the recovery offer: the startup design (the
        scaffold default) was put on the page with the default physics. When
        it was saved with others, ask which the print uses, as loading it by
        hand does. Skipped when recovered work replaced it."""
        preset = getattr(self, "_boot_design_preset", None)
        self._boot_design_preset = None
        choose = getattr(self, "_cal_design_choice", None)
        if preset is None or not callable(choose) \
                or not self._gw_interactive():
            return
        choice = choose(preset, startup=True)
        if choice == "design":
            self._apply_preset_for_kind(
                "calibration", preset["data"][self.DESIGN_PHYSICS_KEY])
            self.status.set("Startup design “%s”:%s %s" % (
                preset["name"], getattr(self, "_boot_zero_note", ""),
                self._cal_design_note(preset["data"], choice)))

    # ------------------------------------------------------------------ #
    # the tick: state, autosave, stats
    # ------------------------------------------------------------------ #
    def _gw_tick(self):
        """Once a second: the unsaved mark, autosave, the job stats, the
        Machine link's stale note.

        The fingerprint reads every design var and hashes the whole text,
        and while the preview worker is computing, every Tk call here waits
        its turn at the GIL (`gui_preview.PREVIEW_SWITCH_INTERVAL`): the
        tick held the window up to a second mid-build. So a
        tick works the fingerprint out once, and a tick that lands while a
        picture is being built leaves it to the next one (five at most, so
        a build that never lands cannot freeze the mark)."""
        self._gw_tick_id = None
        building = getattr(self, "_gce_pv_active", False)
        self._gw_waited = getattr(self, "_gw_waited", 0) + 1 if building else 0
        try:
            if hasattr(self, "gce") and self._gw_waited in (0, 6):
                self._gw_waited = 0
                if (self._gw_base is None or self._gw_rebase_wait) \
                        and self._gw_settled() and self._gce_page_shown():
                    self._gw_rebase_wait = False
                    if self._gw_base != "restored":
                        self._gw_rebase()
                self._gw_key_memo = []
                try:
                    self._gw_label_refresh()
                    self._gw_autosave_step()
                    self._gw_stats_refresh()
                    self._gw_stale_note()
                finally:
                    self._gw_key_memo = None
        finally:
            try:
                self._gw_tick_id = self.root.after(_TICK_MS, self._gw_tick)
            except tk.TclError:
                self._gw_tick_id = None

    # -- autosave ----------------------------------------------------------- #
    def _gw_auto_dir(self):
        return os.environ.get("MEWRACLE_AUTOSAVE_DIR") or \
            os.path.join(USER_DATA_DIR, "autosave")

    def _gw_auto_path(self):
        return os.path.join(self._gw_auto_dir(), _AUTOSAVE_FILE)

    def _gw_auto_data(self):
        doc = self._gce_doc
        data = {"v": 1, "saved_at": time.time(),
                "tie": (self._gw_tie or {}).get("name"),
                "source": doc["source"]}
        if doc["source"] == self._GCE_FILE:
            data.update(path=doc.get("path"), text=self.gce.get_text())
        else:
            design = {}
            for k, w in self._gce_persist_entries().items():
                try:
                    design[k] = w.get()
                except tk.TclError:
                    pass
            design["gce.document"] = self._gce_doc_state()
            try:
                top = self._gce_d_state().get("top") or {}
            except Exception:  # noqa: BLE001
                top = {}
            data.update(design=design, top=top,
                        flavor=(self.gce_flavor.get() if getattr(
                            self, "_gce_flavor_picked", False) else ""),
                        n=self._gce_scaffold_count())
        return data

    def _gw_auto_physics(self):
        """The Physics page, when the next start would not load all of it,
        else None. Reported: a crash lost Physics edits even when Restore
        brought the design back, because the autosave held the design only."""
        unkept = getattr(self, "_cal_unkept", None)
        if not callable(unkept) or not unkept():
            return None
        return self._cal_design_physics()

    def _gw_autosave_step(self):
        if not self._gw_interactive() or self._gw_auto_busy \
                or not self._gw_recovery_done:
            return
        settled = self._gw_settled()
        if not settled and getattr(self, "_gce_model", None) is not None:
            return          # the page is mid-change: leave the autosave be
        # a page never opened has no design work, but the Physics page may
        dirty = settled and self._gw_dirty()
        physics = self._gw_auto_physics()
        if not dirty and physics is None:
            if self._gw_auto_key is not None:
                self._gw_clear_autosave()
            return
        key = "%s\0%s" % (self._gw_key() if dirty else "",
                           json.dumps(physics, sort_keys=True, default=str))
        if key == self._gw_auto_key \
                or time.time() - self._gw_auto_t < _AUTOSAVE_EVERY_S:
            return
        self._gw_auto_key = key
        self._gw_auto_t = time.time()
        if dirty:
            data = self._gw_auto_data()
        else:
            data = {"v": 1, "saved_at": time.time(), "tie": None,
                    "source": None}
        if physics is not None:
            data["physics"] = physics
        self._gw_auto_busy = True
        path = self._gw_auto_path()

        def work():
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
                os.replace(tmp, path)
            except OSError:
                pass
            finally:
                self._gw_auto_busy = False
        threading.Thread(target=work, daemon=True, name="GWautosave").start()

    def _gw_clear_autosave(self):
        self._gw_auto_key = None
        for p in (self._gw_auto_path(), self._gw_auto_path() + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    def _gw_read_autosave(self):
        try:
            with open(self._gw_auto_path(), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("v") == 1 \
            else None

    def _gw_describe(self, data):
        when = time.strftime("%A %d %B at %H:%M",
                             time.localtime(data.get("saved_at", 0)))
        if data.get("source") == self._GCE_FILE:
            what = "your edits to the file “%s”" % os.path.basename(
                data.get("path") or "an opened file")
        elif data.get("source") is None:
            what = ""
        else:
            n = data.get("n") or 1
            what = "this page's design (%d scaffold%s) and its g-code " \
                   "changes" % (n, "" if n == 1 else "s")
            if data.get("tie"):
                what += ", from preset “%s”" % data["tie"]
        phys = data.get("physics")
        if isinstance(phys, dict) and hasattr(self, "_cal_cts_phrase"):
            values = "the values on the Physics page (%s)" \
                % self._cal_cts_phrase(phys)
            what = ("%s, and %s" % (what, values)) if what else values
        return what or "your unsaved work", when

    def _gw_offer_recovery(self):
        """At launch: unsaved work from a session that did not close
        cleanly. Restore or discard, never silently. The autosave stays
        untouched until the answer is in (a tick must not clear the very
        file being offered)."""
        try:
            data = self._gw_read_autosave()
            if data is None or not self._gw_interactive():
                return
            what, when = self._gw_describe(data)
            ans = self._gw_dialog(
                "Recover unsaved work",
                "MEWracle did not close cleanly last time. It kept %s, "
                "saved on %s.\n\nRestore it?" % (what, when),
                [("restore", "Restore"), ("discard", "Discard"),
                 ("later", "Ask me next time")], default=0, cancel="later")
            if ans == "restore":
                self._boot_design_preset = None   # replaced by the work
                self._gw_restore(data)
            elif ans == "discard":
                self._gw_clear_autosave()
        finally:
            self._gw_recovery_done = True
            if getattr(self, "_boot_design_preset", None) is not None:
                self.root.after_idle(self._gw_boot_physics)

    def _gw_restore(self, data, _tries=0):
        """Put recovered work back. The page has to exist first: from Home
        it has never been shown, so go there and apply the work once it has
        built its document. Physics values go back at once; work that is
        only those does not open the page."""
        phys = data.pop("physics", None)
        if isinstance(phys, dict):
            self._apply_preset_for_kind("calibration", phys)
            self.status.set("Restored the Physics page (%s)."
                            % self._cal_cts_phrase())
        if data.get("source") is None and "design" not in data:
            self._gw_auto_key = None
            return
        if not self._gce_page_shown() or self._gce_doc.get("source") is None                 or (_tries < 200 and not self._gw_settled()):
            if not self._gce_page_shown():
                self._nav_select("gcode")
            if _tries < 200:
                self.root.after(300, lambda: self._gw_restore(data, _tries + 1))
            return
        self._gw_restore_now(data)

    def _gw_restore_now(self, data):
        self._gw_tie = {"name": data["tie"]} if data.get("tie") else None
        if data.get("source") == self._GCE_FILE:
            path = data.get("path")
            self._gw_confirmed = True
            try:
                if path and os.path.isfile(path):
                    self._gce_load_file(path)
                self.gce.set_text(data.get("text") or "", undoable=True)
            finally:
                self._gw_confirmed = False
        else:
            design = dict(data.get("design") or {})
            idx = [int(m.group(1)) for m in (
                re.match(r"gce\.s(\d+)\.", k) for k in design) if m]
            if idx:
                self._gce_ensure_scaffolds(max(idx))
            entries = self._gce_persist_entries()
            doc = design.pop("gce.document", "")
            for k, v in design.items():
                w = entries.get(k)
                if w is not None:
                    try:
                        w.set(v)
                    except tk.TclError:
                        pass
            for n, v in (data.get("top") or {}).items():
                w = getattr(self, n, None)
                if isinstance(w, tk.Variable):
                    try:
                        w.set(v)
                    except tk.TclError:
                        pass
            if data.get("flavor"):
                self.gce_flavor.set(data["flavor"])
                self._gce_flavor_picked = True
            if "gce.document" in entries:
                entries["gce.document"].set(doc)
            self._gce_after_page_load()
        # restored work has not been saved: it stays unsaved until it is
        self._gw_base = "restored"
        self._gw_rebase_wait = False
        self._gw_auto_key = None
        self._gw_label_refresh()
        # Say what came back. The page's arrival line was written when Restore
        # opened it, BEFORE the work was put back, so it still called the
        # print "the starting design" over the user's own restored scaffold.
        if data.get("source") == self._GCE_FILE:
            what = "your edits to “%s”" % os.path.basename(
                data.get("path") or "the opened file")
        else:
            n = self._gce_scaffold_count()
            what = "your design (%d scaffold%s)" % (n, "" if n == 1 else "s")
        try:
            self.status.set(
                "Restored %s. It is not saved yet: press Generate to make "
                "its g-code, then Save g-code as." % what)
        except (AttributeError, tk.TclError):
            pass

    # ------------------------------------------------------------------ #
    # undo and redo, anywhere on the page
    # ------------------------------------------------------------------ #
    def _gw_key_undo(self, e, redo):
        """Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z on the page. An entry box and the
        text keep their own."""
        if not self._gw_key_ok(e) or self._gw_typing(e):
            return None
        (self._gce_redo if redo else self._gce_undo)()
        return "break"

    def _gce_undo(self):
        """Undo, from the keys, the buttons and the tree. A design change
        that is waiting on typed edits (the notice 'the design changed')
        is the newest thing the user did and has no step of its own: Undo
        takes it back first, instead of silently undoing something older."""
        self._gw_fresh_landed()
        if getattr(self, "_gce_pending_design", None)                 and getattr(self, "_gce_d_base_state", None) is not None:
            self._gce_pending_design = None
            self._gce_d_restore(self._gce_d_base_state)
            self._gce_notice_hide_if("design-moved")
            self._gce_set_link_label()
            return
        super()._gce_undo()

    # ------------------------------------------------------------------ #
    # job stats
    # ------------------------------------------------------------------ #
    def _gce_pv_apply(self, paths, meta, err):
        super()._gce_pv_apply(paths, meta, err)
        if err or not meta or meta.get("provisional"):
            return      # a toolpath-first tube has no clock yet; its build does
        clock = meta.get("line_clock")
        text = clock.get("text") if clock else None
        if text is None:
            self._gw_stats = dict(text=None, why="This picture has no "
                                  "clock to read the time from.")
            return
        if "radius" in meta:
            # a tube: the designer's own tube numbers (its wrapped length and
            # the parse's clock), not the linear-axes walk
            self._gw_stats = dict(
                text=text, time=meta.get("print_time_s"),
                length=meta.get("fiber_length_mm"), tube=True)
        # the flat print's numbers come from `_gw_stats_refresh`'s walk of
        # the text itself

    def _gw_diameters(self):
        out = set()
        try:
            n = self._gce_scaffold_count()
            for v in self.gce_scaf_vars[:n]:
                # the fibre's diameter, on a tube too (m_diameter is the
                # mandrel's own)
                x = v.get("diameter")
                if x is not None:
                    out.add(round(float(str(x.get()).strip()), 9))
        except (ValueError, tk.TclError, AttributeError):
            return None
        return out or None

    def _gw_stats_refresh(self):
        var = getattr(self, "gce_stats_var", None)
        if var is None or not self._gce_page_shown():
            return
        text = self.gce.get_text()
        if not text.strip():
            var.set("")
            return
        # a finished worker
        try:
            while True:
                key, res = self._gw_stats_q.get_nowait()
                if key == _sha(text) and not (self._gw_stats or {}).get(
                        "tube"):
                    self._gw_stats = dict(res, text=text, tube=False)
        except queue.Empty:
            pass
        tube = self._gce_pv_is_tube() if hasattr(
            self, "_gce_pv_is_tube") else False
        st = self._gw_stats
        if not tube:
            if st is None or st.get("text") != text or st.get("tube"):
                key = _sha(text)
                if self._gw_stats_want != key:
                    self._gw_stats_want = key
                    self._gw_stats_start(text, key)
                var.set("Print: working it out…")
                return
        elif st is None or st.get("text") != text or not st.get("tube"):
            busy = getattr(self, "_gce_pv_waiting", lambda: False)()
            var.set("Print: updating…" if busy or self._gce_regen
                    is not None else
                    "Print: not known (the picture is not drawn; Update "
                    "preview)")
            return
        var.set(self._gw_stats_text(st))

    def _gw_stats_start(self, text, key):
        def work():
            import scaffold_gcode
            try:
                t, mm = scaffold_gcode.print_path_stats(text)
                res = dict(time=t, length=mm)
            except Exception as exc:  # noqa: BLE001
                res = dict(time=None, length=None, error=str(exc))
            self._gw_stats_q.put((key, res))
        threading.Thread(target=work, daemon=True, name="GWstats").start()

    def _gw_stats_text(self, st):
        U = gui_units
        t, mm = st.get("time"), st.get("length")
        if t is None or mm is None:
            return "Print: not known (%s)" % (st.get("error")
                                              or "no moves to measure")
        dia = self._gw_diameters() if not self._gw_is_file() else None
        if self._gw_is_file():
            try:
                dia = {float(str(self.diameter.get()).strip())}
            except (ValueError, tk.TclError):
                dia = None
        bits = [U.readout("print_time", t, "s"),
                U.readout("fiber_len", mm, "mm") + " of fiber"]
        if dia and len(dia) == 1:
            d = next(iter(dia))
            bits.append(U.readout("volume", mm * math.pi * d * d / 4.0,
                                  "mm³"))
        else:
            bits.append("volume varies with each scaffold's fiber "
                        "diameter")
        return "Print: " + " · ".join(bits)

    # ------------------------------------------------------------------ #
    # out of date: for the Machine link
    # ------------------------------------------------------------------ #
    def run(self):
        if hasattr(self, "gce") and not getattr(self, "_gce_dispatch", None) \
                and getattr(self, "_gce_run_text", None) is None:
            self._gw_note_run()
        return super().run()

    def _gw_run_kind(self):
        try:
            mode = self._active_mode()
        except Exception:  # noqa: BLE001
            return None
        # the page builds from any page now, Setup and Home included (run in
        # gui_gcode_editor), so its result is stamped wherever it was pressed
        if mode == "gcode":
            return "page"
        if mode in ("scaffold", "mandrel") \
                and not self._gce_page_shown():
            return "designer"
        return None

    def _gw_page_key(self):
        """What the page shows, for staleness: the document source, an
        opened file's path and text, the page's own design and its g-code
        changes. A linked page's TEXT is left out: it regenerates lazily
        when first shown, with no change by the user, so its text would
        read as a change that was never made."""
        doc = self._gce_doc
        if doc["source"] == self._GCE_FILE:
            return _sha("file\0%s\0%s" % (doc.get("path") or "",
                                           self.gce.get_text()))
        try:
            st = self._gce_d_state()
            design = json.dumps({"top": st.get("top"),
                                 "scaf": st.get("scaf")},
                                sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            design = ""
        try:
            ds = self._gce_doc_state()
        except Exception:  # noqa: BLE001
            ds = ""
        extra = "" if doc["source"] == self._GCE_LINKED             else self.gce.get_text()
        # a never-opened page (no source yet) is the linked page it becomes
        # when first shown or built: the same key, so that is not a change
        source = doc["source"] or self._GCE_LINKED
        return _sha("\0".join((source, design, ds, extra)))

    def _gw_design_key(self):
        sig = self._design_signature("scaffold")
        if sig is None:
            return None
        return _sha(json.dumps({k: v for k, v in sig.items()
                                if not k.startswith("gce.")},
                               sort_keys=True, default=str))

    def _gw_stamp_key(self, kind):
        """Both halves, taken together: the design a Generate came from AND
        the page's state at that moment."""
        return dict(design=self._gw_design_key(), page=self._gw_page_key())

    def _gw_note_run(self):
        kind = self._gw_run_kind()
        path = getattr(self, "last_gcode", None)
        try:
            pre = os.path.getmtime(path) if path else None
        except OSError:
            pre = None
        self._gw_pending = None
        if kind is None:
            return
        try:
            key = self._gw_stamp_key(kind)
        except Exception:  # noqa: BLE001
            key = None
        if key is not None:
            self._gw_pending = dict(kind=kind, key=key, path=path, pre=pre,
                                    doc=self._gw_doc_path())

    def _gw_doc_path(self):
        """The opened file the page shows, or None (a design, or no page)."""
        doc = getattr(self, "_gce_doc", None) or {}
        return doc.get("path") if self._gw_is_file() else None

    def _on_finished(self, code):
        pend, self._gw_pending = getattr(self, "_gw_pending", None), None
        super()._on_finished(code)
        self._gw_adopt_result(pend)

    def _gw_adopt_result(self, pend):
        """A Generate ended: when it left a new file, that file is stamped
        with what the Generate was pressed on."""
        if not pend:
            return
        path = getattr(self, "last_gcode", None)
        try:
            mt = os.path.getmtime(path) if path else None
        except OSError:
            mt = None
        if mt is not None and (path != pend["path"] or mt != pend["pre"]):
            self._gw_result = dict(path=path, mtime=mt, kind=pend["kind"],
                                   key=pend["key"], doc=pend.get("doc"))

    def _gw_stale_reason(self):
        """None, or {"text", "kind"}: why the g-code the printer path would
        send is not what the user now sees."""
        res = getattr(self, "_gw_result", None)
        path = getattr(self, "last_gcode", None)
        if not res or not path or getattr(self, "proc", None) is not None:
            return None
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return None
        if res["path"] != path or abs(res["mtime"] - mt) > 1e-6:
            return None             # a result nobody stamped: not ours to judge
        try:
            now = self._gw_stamp_key(res["kind"])
        except Exception:  # noqa: BLE001
            return None
        waiting = (getattr(self, "_gce_d_after", None) is not None
                   or getattr(self, "_gce_regen", None) is not None)
        if now["page"] != res["key"]["page"] or (
                waiting and res["kind"] == "page"):
            # "an opened file" only when it is ANOTHER file: an edit of the
            # very file Generate built was told it was a different one
            other = self._gw_is_file() and self._gw_doc_path() != res.get("doc")
            return dict(kind="page", text=(
                "The G-code page now shows an opened file, not what you "
                "pressed Generate on." if other else
                "The G-code page has changed since you pressed "
                "Generate, so this file is older than what the page "
                "shows."))
        if now["design"] is not None and res["key"]["design"] is not None                 and now["design"] != res["key"]["design"]:
            return dict(kind="designer", text=(
                "The Scaffold designer has changed since you pressed "
                "Generate, so this file is older than the design."))
        return None

    def _gw_stale_note(self):
        """The Machine link's Will-send line follows the state."""
        why = self._gw_stale_reason()
        key = why["text"] if why else None
        if key != self._gw_stale_seen:
            self._gw_stale_seen = key
            fn = getattr(self, "_ml_refresh_print_name", None)
            if fn is not None:
                fn()

    # ------------------------------------------------------------------ #
    # Send: the page's text, as Save writes it
    # ------------------------------------------------------------------ #
    def _gce_send(self, report=None):
        """Upload THIS text (the bytes Save writes) to the connected
        printer, then offer to start it. `report(text, buttons)` says where
        it speaks (the page's notice by default)."""
        say = report or (lambda t, b=(): self._gw_say(t, b, kind="send"))
        text = self.gce.get_text()
        if not text.strip():
            say("Nothing to send yet: the page has no g-code.")
            return False
        if getattr(self, "_gce_regen", None) is not None \
                or getattr(self, "_gce_d_after", None) is not None:
            say("The page is still updating its g-code. Press Send again "
                "in a moment.")
            return False
        if not getattr(self, "_gce_gaps_ok", False):
            gaps = self._gce_export_gaps()
            if gaps:
                if not self._gce_page_shown():
                    self._nav_select("gcode")
                self._gce_gaps_for = "send"
                try:
                    self._gce_gaps_notice(gaps)
                finally:
                    self._gce_gaps_for = None
                return False
        if not getattr(self, "_ml_connected", False) \
                or getattr(self, "_ml_client", None) is None:
            say("Not sent: the Printer link is not connected to a printer. "
                "Connect it there, then press Send again.",
                [("Open Printer link",
                  lambda: self._nav_select("machine_link"))])
            return False
        return self._gce_send_upload(text, say)

    def _gce_send_upload(self, text, say):
        """Send's upload, once every check has passed: the page's text as
        Save writes it, stored under `_gw_send_name`, then the offer to
        start it."""
        out = self._gce_save_ready(text)
        # what Save would ask about these bytes, Send asks too
        if not self._preflight_ok(out, flavor=self.gce_flavor_name(),
                                  designed=self._gce_from_designer(),
                                  send=True):
            say("Not sent.")
            return False
        path = self._gce_scratch("send.gcode")
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(out)
        except OSError as exc:
            say("Not sent: the file could not be prepared (%s)." % exc)
            return False
        name = self._gw_send_name()
        self._gw_offer_start = name
        self._gw_sending = name
        self._gw_send_text = out          # the start offer reads its heaters
        self._ml_do_upload(path, False, name)
        say("Sending %s to %s…" % (name, self.ml_host.get().strip()))
        return True

    def _gw_send_name(self):
        typed = self.ml_upload_name.get().strip()
        if typed:
            return self._ml_sanitize_filename(typed, "scaffold.gcode")
        doc = self._gce_doc
        if doc["source"] == self._GCE_FILE and doc.get("path"):
            stem = os.path.splitext(os.path.basename(doc["path"]))[0]
            return self._ml_sanitize_filename(stem + "_edited",
                                              "scaffold.gcode")
        return "scaffold.gcode"

    def _gce_send_anyway(self):
        self._gce_gaps_ok = True
        try:
            self._gce_send()
        finally:
            self._gce_gaps_ok = False

    def _ml_handle_event(self, item):
        """A send pressed on this page hears back here, in the page's
        notice: success, the offer to start, and every failure. Before, a
        refused or unanswered upload left the notice reading "Sending…" for
        good, and the reason went to the Printer link page's own line, which
        clears itself after a few seconds."""
        super()._ml_handle_event(item)
        if not (isinstance(item, tuple) and item):
            return
        kind = item[0]
        to_link = [("Open Printer link",
                    lambda: self._nav_select("machine_link"))]
        if kind in ("started", "start_failed") \
                and getattr(self, "_gw_started_name", None) == item[1]:
            self._gw_started_name = None
            if kind == "started":
                self._gw_say("Started %s on the printer." % item[1],
                             kind="send", ms=8000,
                             hide_key="printer-started")
            else:
                self._gw_say(self.ml_conn_status.get(), to_link, kind="send")
            return
        if kind == "upload_failed" and getattr(self, "_gw_sending", None):
            self._gw_sending = None
            self._gw_offer_start = None
            self._gw_say(item[2], to_link, kind="send")
            return
        if kind == "uploaded_all" and getattr(self, "_gw_sending", None):
            self._gw_sending = None
            self._gw_say(self.ml_conn_status.get(), kind="send")
            return
        if kind == "uploaded" and not item[2] \
                and item[1] == self._gw_offer_start:
            self._gw_sending = None
            self._gw_offer_start = None
            name = item[1]
            heat = self._ml_heat_note(getattr(self, "_gw_send_text", None))
            if messagebox.askyesno(
                    "Send and start",
                    "Sent %s to %s. Start printing it now?%s"
                    % (name, self.ml_host.get().strip(),
                       ("\n\n" + heat) if heat else "")):
                self._gw_started_name = name
                client = self._ml_client
                if client is None:
                    return
                self._gw_say("Starting %s…" % name, kind="send")

                def work():
                    try:
                        client.print_start(name)
                        self._ml_q.put(("started", name))
                    except ml.MoonrakerError as exc:
                        self._ml_q.put(("start_failed", name, exc))
                threading.Thread(target=work, daemon=True,
                                 name="MLstart").start()
            else:
                self._gw_say("Sent %s to the printer. It has not started: "
                             "start it under G-code files on the Printer "
                             "link page." % name, to_link, kind="send",
                             ms=10000,
                             hide_key="printer-sent-not-started")

    # ------------------------------------------------------------------ #
    # copy, paste, duplicate
    # ------------------------------------------------------------------ #
    def _gw_key_clip(self, e, op):
        """Ctrl+C / Ctrl+V / Ctrl+K on the page. Anything typing keeps its
        own: the text, an entry box, the Parameters table's cells."""
        if not self._gw_key_ok(e) or self._gw_typing(e):
            return None
        self._gw_clip_op(op)
        return "break"

    def _gw_scaffold_why(self):
        mode = self._gce_designer_mode()
        if mode == "mandrel":
            return self._gce_m_why()
        return self._gce_d_why()

    def _gw_pick(self):
        """What copy would take: ("scaffold", i, label, snapshot) |
        ("lines", lines, label) | (None, why)."""
        ts = self._gce_targets()
        if not ts:
            return (None, "Nothing to copy: select a scaffold, a layer or "
                          "some lines first.")
        if len(ts) == 1 and ts[0][0] == "node":
            iid = ts[0][1]
            info = self._gce_tree_info.get(iid) or {}
            if info.get("kind") == "group":
                i = self._gce_d_index_of(iid)
                if isinstance(i, int) and 0 <= i < len(self.gce_scaf_vars) \
                        and not self._gw_scaffold_why():
                    snap = {}
                    for k, x in self.gce_scaf_vars[i].items():
                        if _is_value(x):
                            try:
                                snap[k] = x.get()
                            except tk.TclError:
                                pass
                    return ("scaffold", i, info.get("label") or
                            "scaffold %d" % (i + 1), snap)
        lines = set()
        for t in ts:
            if t[0] == "text":
                lines.update(range(int(t[1]), int(t[2]) + 1))
            else:
                lines.update(self._gce_target_lines(t))
        lines = sorted(x for x in lines if x >= 1)
        if not lines:
            return (None, "Nothing to copy: the selection has no g-code of "
                          "its own (the whole file, or a design row).")
        label = ((self._gce_tree_info.get(ts[0][1]) or {}).get("label")
                 if len(ts) == 1 and ts[0][0] == "node" else None) \
            or "%d lines" % len(lines)
        return ("lines", lines, label)

    def _gw_lines_clip(self, lines, label):
        eng = self._gce_engine()
        self._gce_record_edits()
        text = self.gce.get_text().split("\n")
        body, starts = self._gce_mv_restack(text, lines, eng)
        if not any(t.strip() for t in body):
            return None
        return dict(kind="lines", body=body, starts=starts, label=label)

    def _gw_clip_op(self, op):
        if op == "paste":
            clip = self._gw_clip
            if clip is None:
                self._gw_say("Nothing to paste yet: select a scaffold or a "
                             "layer and press Ctrl+C first.", kind="clip",
                             ms=6000)
                return False
            return self._gw_paste(clip)
        pick = self._gw_pick()
        if pick[0] is None:
            self._gw_say(pick[1], kind="clip", ms=6000)
            return False
        if pick[0] == "scaffold":
            clip = dict(kind="scaffold", snap=pick[3], label=pick[2])
        else:
            clip = self._gw_lines_clip(pick[1], pick[2])
            if clip is None:
                self._gw_say("Nothing to copy: the selected lines hold no "
                             "moves.", kind="clip", ms=6000)
                return False
        if op == "copy":
            self._gw_clip = clip
            self._gw_say("Copied %s. Ctrl+V pastes it." % clip["label"],
                         kind="clip", ms=4000, hide_key="copied")
            return True
        return self._gw_paste(clip)         # duplicate: the clipboard stays

    def _gw_paste(self, clip):
        if clip["kind"] == "scaffold":
            why = self._gw_scaffold_why()
            if why:
                self._gw_say("Cannot paste a scaffold here: %s" % why,
                             kind="clip", ms=8000)
                return False
            n = self._gce_scaffold_count()
            self._gce_ensure_scaffolds(n + 1)
            self._gce_copy_vars([{k: _Val(v) for k, v in
                                  clip["snap"].items()}],
                                [self.gce_scaf_vars[n]], keep=("color",))
            if self._gce_designer_mode() == "mandrel":
                self._gce_tube_place_beside(n)
            else:
                self._gw_place_beside_flat(n, clip["snap"])
            self.gce_s_count.set(str(n + 1))
            self._gw_post = dict(kind="scaffold", n=n,
                                 until=time.time() + 90)
            self._gw_say("Pasted a copy of %s as scaffold %d. Move opens "
                         "when it is drawn." % (clip["label"], n + 1),
                         kind="clip", ms=6000, hide_key="pasted-scaffold")
            return True
        return self._gw_paste_lines(clip)

    GW_PASTE_GAP = 5.0          # mm of bare plate between a copy and its neighbour

    def _gw_place_beside_flat(self, k, snap):
        """A pasted flat scaffold would land exactly on its original and look
        like nothing happened. Its position is its Start X / Start Y (the
        lower-left corner of its footprint): put it GW_PASTE_GAP mm to the
        right of the right-most scaffold already on the page, same Y as the
        copy's source. A rotated footprint is measured by its bounding box."""
        def num(v, key, default=0.0):
            try:
                return float(str(v[key].get() if hasattr(v[key], "get")
                                 else v[key]).strip())
            except (KeyError, ValueError, tk.TclError):
                return default

        def span(v):
            w, h = num(v, "width"), num(v, "height")
            a = math.radians(num(v, "footprint_angle"))
            return w, abs(w * math.cos(a)) + abs(h * math.sin(a))
        right = None
        for v in self.gce_scaf_vars[:k]:
            w, w_eff = span(v)
            edge = num(v, "start_x") + w / 2.0 + w_eff / 2.0
            right = edge if right is None else max(right, edge)
        if right is None:
            return
        new = self.gce_scaf_vars[k]
        w, w_eff = span(new)
        new["start_x"].set("%g" % round(
            right + self.GW_PASTE_GAP - w / 2.0 + w_eff / 2.0, 4))
        new["start_y"].set("%g" % num(snap, "start_y"))

    def _gw_paste_lines(self, clip):
        m = self._gce_model_now()
        eng = self._gce_engine()
        if m is None or eng is None:
            self._gw_say("Nothing to paste into: there is no g-code yet.",
                         kind="clip", ms=6000)
            return False
        snippet, _n = eng.normalise_piece_text("\n".join(clip["body"]),
                                               m.flavor)
        starts = clip["starts"]
        if len(snippet.splitlines()) != len(clip["body"]):
            starts = None
        n = sum(1 for nf in self._gce_tree_info.values()
                if nf.get("kind") == "group")
        piece = eng.Piece(source="snippet", name="scaffold %d · copy"
                          % (n + 1), text=snippet, flavor=m.flavor,
                          layer_starts=starts or None)
        before = self._gce_snap()
        m.add_piece(piece)
        r = self._gce_render()
        self._gce_doc["base"] = r.text
        if self.gce.get_text() != r.text:
            self.gce.set_text(r.text, undoable=True)
        self._gce_changed(before)
        self._gw_post = dict(kind="piece", until=time.time() + 30)
        self.root.after_idle(self._gw_post_run)
        self._gw_say("Pasted %s as a frozen copy. Move opens on it." %
                     clip["label"], kind="clip", ms=6000,
                     hide_key="pasted-lines")
        return True

    def _gce_take_design(self, raw, sig):
        super()._gce_take_design(raw, sig)
        if getattr(self, "_gw_post", None):
            self.root.after(150, self._gw_post_run)

    def _gw_post_run(self):
        """After a paste, once the picture's tree has the new part: select
        it and open Move on it."""
        post = getattr(self, "_gw_post", None)
        if not post:
            return
        if time.time() > post["until"]:
            self._gw_post = None
            return
        groups = [(nf.get("first") or 0, iid, nf.get("label"))
                  for iid, nf in self._gce_tree_info.items()
                  if nf.get("kind") == "group"]
        if not groups:
            return
        groups.sort()
        iid = None
        if post["kind"] == "scaffold":
            want = "Scaffold %d" % (post["n"] + 1)
            if len(groups) <= post["n"]:
                return              # not drawn yet: the next design lands
            iid = next((g[1] for g in groups if g[2] == want), None) \
                or groups[-1][1]
        else:
            iid = groups[-1][1]
        self._gw_post = None
        self._gce_select_node(iid, see=True, focus=False)
        why = self._gce_tool_why("offset")
        if why:
            self._gw_say(why, kind="clip", ms=6000)
            return
        self._gce_tool_move()

    # the File group's tools, the tree's menu ---------------------------- #
    def _build_gce_work_tools(self, fil, tool):
        """The File tool: Save design, Open file and Send, on one menu."""
        self._gw_state_init()
        self._gce_file_btn = tool(
            fil, "file", "save", "File",
            "Save this page's design and its g-code changes as a preset "
            "(Ctrl+S; a file you opened saves as g-code), open a g-code file "
            "from anywhere (Ctrl+O), or send this g-code to the printer the "
            "Printer link is connected to.", menu=self._gce_file_menu)

    def _gce_file_menu(self):
        m = tk.Menu(self._gce_file_btn, tearoff=0)
        m.add_command(label="Save g-code" if self._gw_is_file()
                      else "Save design", accelerator="Ctrl+S",
                      command=self._gw_save)
        m.add_command(label="Open file…", accelerator="Ctrl+O",
                      command=self._gce_open_file)
        m.add_command(label="Send to printer", command=self._gce_send)
        return m

    def _gce_tree_menu_post(self, menu, iid, e):
        tree = self._gce_tree
        if iid not in tree.selection():
            tree.selection_set(iid)
            try:
                self._gce_select_rows()
            except Exception:  # noqa: BLE001
                pass
        menu.add_separator()
        for label, op, key in (("Copy", "copy", "Ctrl+C"),
                               ("Paste", "paste", "Ctrl+V"),
                               ("Duplicate", "dup", "Ctrl+K")):
            menu.add_command(label=label, accelerator=key,
                             command=lambda op=op: self._gw_clip_op(op))
        super()._gce_tree_menu_post(menu, iid, e)


def gui_form_mark(w):
    """Mark `w` as live text (the revert scan skips it)."""
    try:
        import gui_form
        gui_form.mark(w, "live")
    except Exception:  # noqa: BLE001
        pass
