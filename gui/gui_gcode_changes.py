#!/usr/bin/env python3
"""gui_gcode_changes.py -- MODERN-UI ONLY: the G-code page's edits.

Everything that makes the page's text differ from the Scaffold designer's
plain output is ONE `mew_gcode.Document`: the designer's g-code is the
baseline and every change is an EDIT on top of it. The left column has two
quadrants:

  * My library (top): this file's edits that are not saved, the saved ones,
    and the engine's examples. "+ New edit" starts one.
  * The Anatomy (bottom): the print's parts (header, each scaffold and its
    layers, the end), with every edit under the part it is in and the
    stitched pieces under theirs. Click a part to go to it; click an edit
    to open it; drag an edit onto a part to move it there.

ONE FORM FOR EVERY EDIT, in the library's place. Typed lines and lines made
with the form are the same thing: an edit typed in the text opens in the same
form as one made from "+ New edit" -- its name, what it is (a ready-made
command, your own lines, or a change to the moves already there), its g-code
(always shown, written from the fields or by hand), and where it is (one
spot, nudged a line at a time, or a place in the structure). The form
applies live; Done keeps the whole session as one undo step, Cancel puts
it all back.

Plus the header switch (Detach / Follow the designer again, with the list of
what carries over), What changed, the Problems bar, the Export card's badge,
the saved-flavor follow-ups (`_gce_flavors_changed`; the editor itself is
`gui_flavor_editor`), the unread-move and Save-gap notices, and the
document's place in the scaffold preset (`gce.document`).

THE ENGINE OWNS THE MEANING. How a command is spelled, where an edit is
anchored, what a rule writes, how a flavor re-spells -- all of it is
`mew_gcode`; this module only shows it and hands it the user's choices. The
form fields come from the engine's own tables (`INSERT_FORMS`,
`TRANSFORM_FIELDS`, `WHERE_CHOICES`, `FLAVOR_FIELDS`), so a field added there
appears here.

Undo. Re-rendering after a chip change would read to Tk's undo as a hand
edit, so every undo step that changed the document carries the document's
snapshots (`Document.snapshot`, before and after); Undo restores the
snapshot first, then the text (`GcodeEditor.attach_meta` / `on_step`).

Generate. A linked document with changes runs the designer's own Generate
with `--gcode-doc` (the worker rebases the document on the g-code it makes
and writes the parse form), so the STL, .depo and player see the changes and
a worker run anchors exactly as the page does. A detached copy or an opened
file converts its parse form on a scratch file.

No popups anywhere (spec 12.4): forms open in place of the library's list,
confirmations are inline, and messages use the page's notice strip."""

from gui_common import *

import copy
import difflib
import json
import re

import gui_form
import gui_revert
import gui_units
import gui_widgets
from gui_tips import Tooltip, add_tip

_DETACHED = "detached"
_RECIPES_FILE = "gcode_recipes.json"
#: the tree's colours: an edit, an edit ticked off, one that lost its
#: place, a stitched piece. Each also says it in words.
_EDIT_FG = "#d9a520"
_OFF_FG = "#808080"
_LOST_FG = "#e06c5a"
_PIECE_FG = "#6fa8dc"
_AUTO_FG = "#58a06f"     # an automatic script's locked row

#: gutter marks by line origin. Never colour alone (spec 12.6): each is a
#: character, and the explain bar names the chip.
_ORIGIN_MARK = {"insert": "+", "edit": "~", "transform": "~", "rule": "R",
                "option": "•", "join": "J", "flavor": "F"}


def _same_text(typed, rendered):
    """The text as typed is the render: a missing final newline (a line
    typed after the last one) is not an edit."""
    return typed == rendered or typed + "\n" == rendered


class _GcodeChangesMixin:
    """The edits half of the G-code page. Sits before `_GcodeEditorMixin`
    in `ModernApp`'s MRO and overrides its hooks, each chaining to super()."""

    # ------------------------------------------------------------------ #
    # the document
    # ------------------------------------------------------------------ #
    def _gce_model_now(self):
        return getattr(self, "_gce_model", None)

    def _gce_from_designer(self):
        """A text from the designer (linked, or a detached copy of it): the
        output options and the designer's fiber settings apply."""
        return self._gce_doc["source"] in (self._GCE_LINKED, _DETACHED)

    def _gce_options(self):
        """The Export card's output options for a designer text, through the
        one seam Save uses; all off for an opened file (never rewritten).
        With a generator line map the scaffold boundaries are read off it
        (`AUTO`), so they stay right after a hand deletion."""
        eng = self._gce_engine()
        if not self._gce_from_designer():
            return eng.OutputOptions()
        saved = (getattr(self, "_last_gcode_is_scaffold", False),
                 getattr(self, "_last_gcode_is_calibration", False))
        self._last_gcode_is_scaffold = True
        self._last_gcode_is_calibration = False
        try:
            opts = self._gcode_output_options()
            opts.context = self._machine_gcode_context(use_gce=True)
        finally:
            (self._last_gcode_is_scaffold,
             self._last_gcode_is_calibration) = saved
        m = self._gce_model_now()
        stamp = self._gce_header_stamp(m, opts.context)
        if stamp is not None:
            opts.context["date"], opts.context["time"] = stamp
        lm = getattr(m, "line_map", None)
        if opts.drop_layer_z and lm is not None and not lm.guessed \
                and m is not None and lm.matches(m.base_text):
            opts.boundaries = eng.AUTO
        return opts

    def _gce_header_stamp(self, m, context):
        """(date, time) for the header's [[date]] and [[time]], taken when
        this base text arrived and held until the next one. The clock read
        at every render made the page's text differ from the picture's the
        moment the minute changed, and every click on a fibre was dropped as
        coming from another text.

        Held by the base text's CONTENT, for the last few texts: an Undo (or
        a zero changed and changed back) brings back a text the page already
        showed, with the stamp it was shown with. Taken afresh, a minute
        boundary between the two made the restored text differ from its own
        render, so the play bar fell to one layer and every click on the
        picture was dropped."""
        if m is None:
            return None
        base = m.base_text
        held = getattr(self, "_gce_stamp", None)
        if held is not None and held[0] is base:
            return held[1], held[2]
        seen = getattr(self, "_gce_stamps", None)
        if seen is None:
            seen = self._gce_stamps = {}
        key = (len(base or ""), hash(base))   # not the text: it can be large
        stamp = seen.pop(key, None) or (context.get("date", ""),
                                        context.get("time", ""))
        seen[key] = stamp               # newest last
        while len(seen) > 16:
            seen.pop(next(iter(seen)))
        self._gce_stamp = (base, stamp[0], stamp[1])
        return stamp

    def _gce_model_sync(self):
        """The document with the page's live options on it, and the design's
        array copies (an edit on the original lands on each copy too)."""
        m = self._gce_model_now()
        if m is not None:
            m.options = self._gce_options()
            m.copies = self._gce_copies(m)
            m.feed_unit = (self._feed_time_unit()
                           if hasattr(self, "_feed_time_unit") else "min")
        return m

    def _gce_copies(self, m):
        """{("base", copy group): ("base", original group)} for a designer
        text with arrays: each array copy is a group of its own, named
        scaffold1.. over the designer's EXPANDED list, and the expansion's
        sources say which scaffold each one copies. The first group of a
        scaffold is the original."""
        if m.source == "file" or not self._gce_from_designer() \
                or not hasattr(self, "_expanded_specs_and_sources"):
            return {}
        try:
            src = self._expanded_specs_and_sources()[1]
            groups = m._map().groups
        except Exception:  # noqa: BLE001 -- a half-typed field: no copies
            return {}
        first, out = {}, {}
        for gi, g in enumerate(groups):
            hit = re.fullmatch(r"scaffold(\d+)", (g or {}).get("name") or "")
            if not hit or int(hit.group(1)) - 1 >= len(src):
                continue
            s = src[int(hit.group(1)) - 1]
            if s in first:
                out[("base", gi)] = ("base", first[s])
            else:
                first[s] = gi
        return out

    def _gce_render(self):
        """The document's export render, cached on everything it reads: the
        base, the flavor, the options, the chips and the pieces. A large
        file renders in seconds, and the gutter, the explain bar and the
        list each ask for it."""
        m = self._gce_model_sync()
        if m is None:
            return None
        key = self._gce_doc_key(m)
        hit = getattr(self, "_gce_rcache", None)
        if hit is not None and hit[0] == key:
            return hit[1]
        r = m.render()
        self._gce_rcache = (key, r)
        self._gce_note_render(m, r)
        return r

    def _gce_render_as_is(self, m):
        """`m.render()` with the options `m` has now (never the live ones:
        `_gce_record_edits` reads typing against the render the text came
        from), out of the same cache as `_gce_render`. A redesign recorded
        the typing twice and so rendered the unchanged document twice, 0.7 s
        each on a waved scaffold."""
        key = self._gce_doc_key(m)
        hit = getattr(self, "_gce_rcache", None)
        if hit is not None and hit[0] == key:
            return hit[1]
        r = m.render()
        self._gce_rcache = (key, r)
        self._gce_note_render(m, r)
        return r

    @staticmethod
    def _gce_doc_key(m, off=None):
        """Everything a render reads, as a value. Edit `off` (an id, the
        edit switched off) stands in as its id alone: a render skips an
        edit that is off whole, and every edit under it (`_render_impl`,
        `_placements` and the at-edits all test `on` / `_shown`), so its
        numbers read nothing -- and a Move's numbers change on every
        drag."""
        return (id(m), len(m.base_text), hash(m.base_text), m.flavor,
                getattr(m, "feed_unit", "min"),
                bool(m.linked), tuple(sorted(m.copies.items())),
                json.dumps(m.options.to_dict(), sort_keys=True, default=str),
                json.dumps([{"id": c.id, "on": False}
                            if off is not None and c.id == off and not c.on
                            else c.to_dict() for c in m.changes],
                           sort_keys=True, default=str),
                json.dumps([p.to_dict() for p in m.pieces], sort_keys=True,
                           default=str))

    def _gce_snap(self, record=True):
        """The document's state before a change. An edit open in the form is
        kept first (any other change ends its session), and typing not yet
        recorded is recorded: a change rendered over unrecorded typing
        would wipe it out."""
        if record:
            if getattr(self, "_gce_fs", None) is not None:
                self._gce_edit_finish(keep=True)
            self._gce_record_edits()
        m = self._gce_model_now()
        return m.snapshot() if m is not None else None

    def _gce_changed(self, before):
        """The document changed since `before` (a snapshot): pair the text's
        newest undo step with both snapshots."""
        after = self._gce_snap(record=False)
        if before is not None and after is not None and before != after:
            self.gce.attach_meta((before, after))

    #: the page's own record of WHICH text it shows: an opened file (and
    #: where it came from) or this page's design
    _GCE_PAGE_KEYS = ("source", "raw", "base", "path", "sig", "mode",
                      "diameter")

    def _gce_snap_page(self):
        """`_gce_snap` for a step that changes which text the page shows
        (File > Open, Back to this design): the document's snapshot carries
        the page's record too (`page`; `Document.from_dict` ignores it).
        Without it Undo swapped the text back under the other document: an
        opened file came back as thousands of typed edits on the design,
        still followed by the designer, and Redo of an Open never ended."""
        m = self._gce_model_now()
        if m is None:
            return None
        snap = m.snapshot()
        doc = self._gce_doc
        page = {k: doc.get(k) for k in self._GCE_PAGE_KEYS}
        page.update(flavor=self.gce_flavor.get(),
                    flavor_picked=getattr(self, "_gce_flavor_picked", False),
                    strip=bool(self.gce_strip_comments.get()))
        snap["page"] = page
        return snap

    def _gce_page_changed(self, before):
        """`_gce_changed` for a step made with `_gce_snap_page`."""
        after = self._gce_snap_page()
        if before is not None and after is not None:
            self.gce.attach_meta((before, after))

    def _gce_page_restore(self, page):
        self._gce_doc.update({k: page.get(k) for k in self._GCE_PAGE_KEYS})
        self._gce_flavor_picked = page.get("flavor_picked", False)
        if page.get("flavor"):
            self.gce_flavor.set(page["flavor"])
        self.gce_strip_comments.set(bool(page.get("strip")))
        self._gce_pending_design = None
        self._gce_notice_hide()
        # the status line described the step just undone or redone
        if page.get("source") == getattr(self, "_GCE_FILE", None):
            self._gce_status(
                "%s is on the page again. Generate turns it into fibers for "
                "Simulation; Back to this design returns to your design."
                % os.path.basename(page.get("path") or "The file"))
        else:
            self._gce_status("This page's design is on the page again: the "
                             "text is its g-code and follows it as it "
                             "changes.")

    def _gce_zero_carry(self, dx, dy):
        """G-code zero moved on the plate (`gui_buildplate._zero_moved`),
        which rewrote every Start X/Y by (`dx`, `dy`) so the scaffold stays
        put. The edits on this page that name plate places -- a park move,
        a pasted control scaffold, a stitched copy -- move by the same, or
        they would be left at the old numbers: off the plate, or into the
        scaffold. Only a text that follows the designer: an opened file or
        a frozen copy keeps its own numbers, edits and all. Undo and Redo
        bring the document back whole (the step's snapshots), so nothing
        moves while they restore the zero."""
        m = self._gce_model_now()
        doc = getattr(self, "_gce_doc", None) or {}
        if (m is None or m.source == "file" or not m.linked
                or doc.get("source") not in (None, self._GCE_LINKED)
                or getattr(self, "_gce_d_restoring", False)
                or getattr(self, "_loading_preset", False)):
            return
        coll = getattr(self, "s_collector", None)
        try:
            if coll is not None and coll.get() != "plate":
                return          # Start X/Y place nothing on a rod
        except tk.TclError:
            return
        if not (m.changes or m.pieces):
            return
        if "_gce_zero_before" not in self.__dict__:
            # the next design text is this change's undo step
            self._gce_zero_before = self._gce_snap()
            m = self._gce_model_now()
        if m.shift_user_xy(dx, dy):
            self._gce_render_changes()
            self._gce_sync_persist()

    def _gce_on_step(self, meta, forward):
        m = self._gce_model_now()
        if m is None:
            return
        snap = meta[1] if forward else meta[0]
        m.restore(snap)
        src = {True: self._GCE_LINKED, False: _DETACHED}
        file_src = getattr(self, "_GCE_FILE", None)
        if snap.get("page") is not None:
            self._gce_page_restore(snap["page"])
            self.root.after_idle(self._gce_set_link_label)
        elif self._gce_doc["source"] in tuple(src.values()) + (file_src,):
            # a file a scaffold was added to (`adopt_file`) is a file again
            # when that step is undone, and the design again when redone
            if m.source == "file" and file_src is not None:
                self._gce_doc["source"] = file_src
                self._gce_doc["path"] = getattr(self, "_gce_splice_path",
                                                None)
            else:
                self._gce_doc["source"] = src[bool(m.linked)]
            self.root.after_idle(self._gce_set_link_label)
        # the tree and the saved session, once the text is swapped back
        self.root.after_idle(self._gce_render_changes)
        self.root.after_idle(self._gce_sync_persist)

    def _gce_line_map_path(self):
        return self._gce_scratch("design.linemap.json")

    def _gce_line_map(self):
        eng = self._gce_engine()
        path = self._gce_line_map_path()
        if eng is None or not os.path.isfile(path):
            return None
        try:
            return eng.LineMap.load(path)
        except Exception:  # noqa: BLE001
            return None

    def _gce_design_argv(self):
        """The regeneration argv, plus the generator's line map beside the
        scratch g-code (out of band: the g-code is byte-identical)."""
        self._gce_regen_building = True
        try:
            argv, sig, err = super()._gce_design_argv()
        finally:
            self._gce_regen_building = False
        if argv is not None and self._gce_engine() is not None:
            path = self._gce_line_map_path()
            try:
                os.remove(path)
            except OSError:
                pass
            argv = list(argv) + ["--line-map", path]
        return argv, sig, err

    def _gce_render_design(self, raw):
        m = self._gce_model_now()
        if m is None or m.base_text != raw:
            return super()._gce_render_design(raw)
        return self._gce_render().text

    def _gce_take_design(self, raw, sig):
        """A new designer g-code: typing still waiting is recorded first,
        then the document rebases under its chips (any that lost their place
        are listed as couldn't-carry-over) and the page takes its render.

        TYPED edits on a layer, line or scaffold are not carried: they were
        never placed by the user, so the page says so first, inline, and
        waits -- Update replaces them, Keep my text freezes it. Typed edits
        at the start, header or end carry silently (`_gce_chip_fixed`), as
        do edits made with + New edit or kept from the form."""
        doc = self._gce_doc
        if doc["source"] == self._GCE_FILE:
            # A design text for a page that shows an opened file: a regenerate
            # started before the Open. Taken, it made the page the design's
            # with no document of its own (no tree rows, a play bar of one
            # layer). Every way back to the design says so first
            # (`_gce_relink`, `_gce_persist_written`).
            return
        self._gce_edit_finish(keep=True)
        eng = self._gce_engine()
        m = self._gce_model_now()
        if (eng is not None and m is not None and m.source != "file"
                and doc["source"] in (None, self._GCE_LINKED)
                and m.base_text and m.base_text != raw
                and not getattr(self, "_gce_take_anyway", False)):
            self._gce_record_edits()
            n = len(self._gce_typed_chips())
            if n:
                self._gce_pending_design = (raw, sig)
                one = n == 1
                self._gce_notice_show(
                    f"The design changed. Updating the g-code replaces the "
                    f"{n} edit{'' if one else 's'} you typed and haven't "
                    f"kept. To keep {'it' if one else 'one'}, click "
                    f"{'it' if one else 'it'} in the tree and press "
                    f"Keep.",
                    [("Update the g-code", self._gce_take_pending_design),
                     ("Keep my text (freeze it)",
                      self._gce_detach)], kind="design-moved")
                self.gce_link_var.set("This page's design · the design "
                                      "has changed")
                return
        before = self._gce_snap(record=False)
        # a zero change moved the edits ahead of this text: the step's
        # "before" is the document as it was then (`_gce_zero_carry`)
        held = self.__dict__.pop("_gce_zero_before", None)
        if held is not None and before is not None:
            before = held
        if eng is not None and doc["source"] in (None, self._GCE_LINKED):
            # after Update the typed chips were removed on purpose: recording
            # the old text again would bring them straight back
            if not getattr(self, "_gce_take_anyway", False):
                self._gce_record_edits()
            m = self._gce_model_now()
            lm = self._gce_line_map()
            if m is None or m.source == "file":
                self._gce_library()  # import old automatic recipes before options
                self.gce_strip_comments.set(self.machine_strip_comments.get())
                self._gce_model = eng.new_document(
                    raw, lm, self.gce_flavor_name(), self._gce_options(),
                    library=())
                # New scaffold on an opened file: the file is the print's
                # first piece, its edits with it (`mew_gcode.adopt_file`)
                splice = getattr(self, "_gce_splice", None)
                self._gce_splice = None
                if splice is not None and m in (None, splice["doc"]):
                    _p, notes = eng.adopt_file(self._gce_model,
                                               splice["doc"],
                                               splice["name"])
                    self._gce_splice_notes = notes
                    self._gce_splice_path = splice["path"]
            elif m.base_text != raw or m.line_map is None:
                # `m.rebase(raw, lm)`, whose render was thrown away and done
                # again with the live options a moment later
                # (`_gce_render_design`): rendered here with those options,
                # into the cache that render reads
                m.base_text, m.line_map = raw, lm
                self._gce_render()
            self._gce_migrate_custom_line(self._gce_model_now())
        page_before = self.__dict__.pop("_gce_page_before", None)
        steps = self.gce._undo
        n0, top0 = len(steps), (steps[-1] if steps else None)
        super()._gce_take_design(raw, sig)
        if page_before is not None:
            # only onto the step this text made: a relink to the same text
            # (a frozen copy never edited) makes none, and the newest step
            # is then something older
            if len(steps) != n0 or (steps and steps[-1] is not top0):
                self._gce_page_changed(page_before)
        else:
            self._gce_changed(before)
        self._gce_restore_pending()
        self._gce_sync_persist()

    def _gce_chip_fixed(self, c):
        """True for an edit anchored to a part that never moves when the
        design changes: the start, the header, the end. It carries over a
        redesign without asking; one anchored to a layer, a line or a
        scaffold is the design's own and is asked about."""
        eng = self._gce_engine()
        if isinstance(c, eng.Insert):
            a = c.anchor
            if a.unit in ("start", "end", "header"):
                return True
            return a.unit == "line" and len(a.key) > 1 \
                and a.key[1] in ("header", "exit")
        if isinstance(c, eng.LineEdit):
            if c.at and c.at[0] in ("machine", "flavor", "opt", "origin"):
                return True
            return len(c.key) > 1 and c.key[1] in ("header", "exit")
        if isinstance(c, eng.Rule):
            return c.where in ("start", "end", "before_first_print")
        return False

    def _gce_typed_chips(self):
        """The typed edits a redesign would replace: typed and not kept,
        and anchored to something the design moves."""
        m = self._gce_model_now()
        return [c for c in (m.changes if m is not None else ())
                if getattr(c, "typed", False) and not self._gce_chip_fixed(c)]

    def _gce_take_pending_design(self):
        """Update: drop the typed chips, then take the waiting design."""
        pending = getattr(self, "_gce_pending_design", None)
        self._gce_pending_design = None
        if pending is None:
            return
        m = self._gce_model_now()
        before = self._gce_snap(record=False)
        for c in self._gce_typed_chips():
            m.remove(c.id)
        self._gce_take_anyway = True
        try:
            self._gce_take_design(*pending)
        finally:
            self._gce_take_anyway = False
        self._gce_changed(before)
        self._gce_notice_hide_if("design-moved")

    def _gce_load_file(self, path):
        self.__dict__.pop("_gce_page_before", None)
        steps = self.gce._undo
        n0, top0 = len(steps), (steps[-1] if steps else None)
        before = self._gce_snap_page()
        super()._gce_load_file(path)
        try:
            self._gce_load_file_doc(path)
        finally:
            # the Open's own step (and only it: an unreadable file made none)
            made = len(steps) != n0 or (steps and steps[-1] is not top0)
            if before is not None and made \
                    and self._gce_doc["source"] == self._GCE_FILE:
                self._gce_page_changed(before)

    def _gce_load_file_doc(self, path):
        # A file the researcher brings keeps its own comments: Save and Send
        # wrote it stripped by the printer's default, silently. Stripping
        # stays one tick away (Export > Strip comments on save); the app's
        # own generated g-code still follows the printer's setting.
        self.gce_strip_comments.set(False)
        eng = self._gce_engine()
        doc = self._gce_doc
        if eng is None or doc["source"] != self._GCE_FILE \
                or doc.get("path") != path:
            return
        pre = self._gce_prepared_for(path)
        if pre is not None and pre["text"] != doc["raw"]:
            pre = None
        detected = (pre["flavor"] if pre is not None
                    else eng.detect_flavor(doc["raw"]))
        label = eng.get_flavor(detected).label
        if label in self._gce_flavor_labels():
            self.gce_flavor.set(label)
            self._gce_flavor_picked = label != self._gce_machine_flavor()
        if pre is not None and pre["flavor"] == self.gce_flavor_name():
            # the worker's document and its render, taken when they are what
            # this thread would build: the same text in the same flavor, and
            # the render's key what the page's live options make it
            self._gce_model = pre["doc"]
            key = self._gce_doc_key(self._gce_model_sync())
            if key == pre["key"]:
                self._gce_rcache = (key, pre["render"])
                self._gce_unread_memo = (pre["render"].text,
                                         pre["unread_key"], pre["unread"])
        else:
            self._gce_model = eng.Document.from_file(doc["raw"],
                                                     self.gce_flavor_name())
        doc["base"] = self._gce_render().text
        self._gce_render_changes()
        self._gce_sync_persist()
        doc.pop("not_moves", None)      # "Not a move" was for the last file
        self._gce_check_unread()

    @classmethod
    def _gce_prepare_file(cls, path, feed_unit):
        """What opening `path` costs, worked out on a WORKER thread so a
        large file does not hold the window: the read and tidy, the flavor
        detected, the document, and its render (seconds on a 120,000-line
        file). Reads no Tk state: `feed_unit` is captured by the caller. The
        load then takes it through `_gce_prepared_for`, or does the work
        itself when anything it read has moved since."""
        import mew_gcode as eng
        st = os.stat(path)
        text, notes = eng.read_foreign_gcode(path)
        flavor = eng.detect_flavor(text)
        m = eng.Document.from_file(text, flavor)
        m.feed_unit = feed_unit
        r = m.render()
        return {"path": path, "stat": (st.st_mtime_ns, st.st_size),
                "text": text, "notes": notes, "flavor": flavor, "doc": m,
                "render": r, "key": cls._gce_doc_key(m),
                "unread_key": cls._gce_unread_key(eng, flavor),
                "unread": eng.unread_moves(r.text, flavor)}

    @staticmethod
    def _gce_unread_key(eng, flavor):
        """Everything of the flavor `unread_moves` reads: its name, its
        words (a word mapped from the notice adds one, the name unchanged)
        and its comment character."""
        return (flavor, frozenset(eng.flavor_words(flavor)),
                eng.get_flavor(flavor).comment)

    def _gce_rerender(self, before):
        """A chip, piece or flavor changed: render, show it as one undo step,
        and refresh everything that reads the document."""
        r = self._gce_render()
        if r is None:
            return
        self._gce_doc["base"] = r.text
        if self.gce.get_text() != r.text:
            self.gce.set_text(r.text, undoable=True)
        self._gce_changed(before)
        self._gce_set_link_label()
        self._gce_render_changes()
        self._gce_pv_schedule(now=True)
        self._gce_refresh_controls()
        self._gce_refresh_problems()
        self._gce_sync_persist()

    def _gce_record_edits(self):
        """The engine records each typed edit against the structure, as a
        typed chip. Called before anything that re-renders or saves -- never
        on a typing pause (the preview records into its own copy, on its
        worker). A read-only line it refuses comes back, and the strip says
        why in its words.

        Recorded against the render the text CAME from, never the live
        vars: an output option flipped a moment ago, or a Parameters value
        the header quotes (its spacing, layers, rotation, scaffold count),
        has not reached the text yet, and reading those lines as typing
        made one bogus edit per line. Text the page put there itself is no
        typing at all, and records nothing."""
        m = self._gce_model_now()
        if m is None:
            return
        typed = self.gce.get_text()
        base = self._gce_doc.get("base")
        if base is not None and _same_text(typed, base):
            return
        live = self._gce_text_render_state(m, base)
        try:
            r = self._gce_render_as_is(m)
            if _same_text(typed, r.text):
                self._gce_doc["base"] = typed
                return
            before = m.snapshot()
            try:
                m.record_edit(typed)
            except Exception as exc:  # noqa: BLE001 -- shown, never raised
                self._gce_notice_show(
                    f"That edit could not be recorded: {exc}",
                    [("OK", self._gce_notice_hide)], kind="record")
                return
        finally:
            if live is not None:
                m.options, m.copies, m.feed_unit = live
        r = self._gce_render_as_is(m)
        self._gce_doc["base"] = r.text
        if r.text != typed:
            self.gce.set_text(r.text, undoable=True)
        self._gce_changed(before)
        self._gce_notice_hide_if("record")

    def _gce_note_render(self, m, r):
        """Remember what rendered `r` (the options, the array copies, the
        time unit), so typing over `r.text` is read against it."""
        src = getattr(self, "_gce_rsrc", None)
        if src is None:
            src = self._gce_rsrc = {}
        src[hash(r.text)] = (copy.deepcopy(m.options), dict(m.copies),
                             getattr(m, "feed_unit", "min"))
        while len(src) > 6:
            src.pop(next(iter(src)))

    def _gce_text_render_state(self, m, text):
        """Put on `m` what rendered `text` when that was not what `m` has
        now, and return what it had (to put back); None when nothing
        changed or `text` is not a render this page remembers."""
        if text is None:
            return None
        src = (getattr(self, "_gce_rsrc", None) or {}).get(hash(text))
        if src is None:
            return None
        now = (m.options, m.copies, getattr(m, "feed_unit", "min"))
        if (src[0].to_dict() == now[0].to_dict() and src[1] == now[1]
                and src[2] == now[2]):
            return None
        m.options, m.copies, m.feed_unit = (copy.deepcopy(src[0]),
                                            dict(src[1]), src[2])
        return now

    def _gce_edited(self):
        """Unrecorded typing (the engine records it when typing pauses)."""
        m = self._gce_model_now()
        if m is None:
            return super()._gce_edited()
        return self.gce.get_text() != self._gce_doc.get("base", "")

    def _gce_doc_carries_edits(self):
        return self._gce_model_now() is not None

    def _gce_on_idle(self):
        super()._gce_on_idle()
        self._gce_sync_persist()

    def _gce_parse_text(self):
        if self._gce_model_now() is None:
            return super()._gce_parse_text()
        self._gce_record_edits()
        return self._gce_model_sync().parse_text()

    def _gce_pv_source(self):
        """The preview's text, worked out on the worker from a COPY of the
        document: the typing is recorded into the copy (against the render
        the text came from, as `_gce_record_edits` does), then the copy
        takes the live output options and gives its parse form. The page's
        own document is not touched there: what the copy recorded comes
        back to `_gce_pv_adopt`, which takes it if nothing moved since."""
        m = self._gce_model_now()
        if m is None:
            return super()._gce_pv_source()
        doc = copy.copy(m)
        doc.changes = copy.deepcopy(m.changes)
        doc.pieces = copy.deepcopy(m.pieces)
        doc.options = copy.deepcopy(m.options)
        live = self._gce_options()
        typed = self.gce.get_text()
        key0 = self._gce_doc_key(m)
        typing = typed != self._gce_doc.get("base", "")
        # typing is read against the render the text came from
        self._gce_text_render_state(doc, self._gce_doc.get("base"))
        # Nothing to record when the text IS the render it came from, already
        # in the page's cache under this very document: `record_edit` would
        # render the whole document once more only to find no difference.
        # That was half the worker's time on every picture of an untyped
        # text, and a 120,000-line file's open paid it at 1.9 s
        hit = getattr(self, "_gce_rcache", None)
        recorded = (hit is not None and hit[0] == key0
                    and _same_text(typed, hit[1].text))

        def run():
            if typing and not recorded:
                doc.record_edit(typed)
            doc.options = live
            parse = doc.parse_text()
            adopt = None
            if typing:
                r = doc.render()
                if _same_text(typed, r.text):
                    adopt = (typed, key0, doc.changes, doc._next_id, r, live)
            return parse, adopt
        return run

    def _gce_pv_adopt(self, adopt):
        """The worker's recording of the typing, taken when the text and the
        document are still what it worked from: the typed edits show in
        the tree and the library without a pass on this thread."""
        typed, key0, changes, next_id, r, live = adopt
        m = self._gce_model_now()
        if m is None or getattr(self, "_gce_fs", None) is not None:
            return
        if self.gce.get_text() != typed or self._gce_doc_key(m) != key0:
            return
        self.gce.commit()
        before = m.snapshot()
        m.changes = changes
        m._next_id = max(m._next_id, next_id)
        m.options = live
        self._gce_rcache = (self._gce_doc_key(m), r)
        self._gce_doc["base"] = typed
        self._gce_changed(before)
        self._gce_render_changes()
        self._gce_sync_persist()

    def _gce_parse_line(self, line):
        m = self._gce_model_now()
        if m is None:
            return line
        n = line - 1
        while n >= 0:
            p = m.export_to_parse_line(n)
            if p is not None:
                return p + 1
            n -= 1
        return 1

    def _gce_flavor_changed(self):
        """The document's flavor changed (picked here, or followed from the
        Machine page): the user's lines re-spell, one undo step."""
        m = self._gce_model_now()
        name = self.gce_flavor_name()
        if m is not None and m.flavor != name:
            before = self._gce_snap()
            m.set_flavor(name)
            self._gce_rerender(before)
        super()._gce_flavor_changed()
        self._gce_check_unread()

    def _gce_flavors_changed(self, renamed, deleted, edited):
        """A saved flavor changed (`_flavors_changed`): this text follows a
        rename, falls back to the Machine page's flavor when its own went,
        and re-renders when its flavor's words were edited."""
        if not hasattr(self, "gce"):
            return
        cur = self.gce_flavor.get()
        if cur in renamed:
            self.gce_flavor.set(renamed[cur])
        elif deleted and cur == deleted:
            self._gce_flavor_reset()
            self._gce_flavor_changed()
            return
        m = self._gce_model_now()
        if m is not None and edited and m.flavor == edited:
            self._gce_rerender(self._gce_snap())
        self._gce_check_unread()

    # ------------------------------------------------------------------ #
    # words the flavor doesn't know (`mew_gcode.unread_moves`) and
    # commands it can't write (`mew_gcode.export_gaps`)
    # ------------------------------------------------------------------ #
    def _gce_check_unread(self):
        """Lines that look like moves the preview can't read: say so once,
        with a way to tell MEWracle what the word is. Everything else a
        flavor doesn't know goes to the machine as written, unasked."""
        eng = self._gce_engine()
        m = self._gce_model_now()
        if eng is None or m is None or not hasattr(self, "gce"):
            return
        skip = self._gce_doc.setdefault("not_moves", set())
        text = self.gce.get_text()
        # the scan reads every line (0.7 s of a 120,000-line file's open), so
        # its answer for one text in one flavor is kept, and File > Open
        # hands over the worker's (`_gce_prepare_file`)
        key = self._gce_unread_key(eng, m.flavor)
        memo = getattr(self, "_gce_unread_memo", None)
        if memo is None or memo[1] != key or memo[0] != text:
            memo = (text, key, eng.unread_moves(text, m.flavor))
            self._gce_unread_memo = memo
        found = {w: ns for w, ns in memo[2].items() if w not in skip}
        if not found:
            self._gce_notice_hide_if("unread-move")
            return
        if getattr(self, "_gce_notice_kind", None) not in (None,
                                                           "unread-move"):
            return      # never over a notice that came first
        word, lines = max(found.items(), key=lambda kv: len(kv[1]))
        fl = eng.get_flavor(m.flavor)
        n = len(lines)
        more = len(found) - 1
        self._gce_notice_show(
            "%s %s with %s and %s coordinates, so %s like %s, but %s doesn't "
            "say what %s is: the preview leaves %s out.%s"
            % (n, "line starts" if n == 1 else "lines start", word,
               "carries" if n == 1 else "carry",
               "it looks" if n == 1 else "they look",
               "a move" if n == 1 else "moves", fl.label, word,
               "it" if n == 1 else "them",
               " %d more word%s like it." % (more, "" if more == 1 else "s")
               if more else ""),
            [("Read as printing moves",
              lambda: self._gce_map_word(word, "G1")),
             ("Read as travels", lambda: self._gce_map_word(word, "G0")),
             ("Not a move", lambda: self._gce_not_a_move(word)),
             ("Go to line", lambda: self.gce.goto_line(lines[0] + 1))],
            kind="unread-move", hide_key="unread-move")

    def _gce_map_word(self, word, cmd):
        """Read `word` as `cmd` (G1/G0) from now on, in this text's flavor:
        a read-only alias (`Flavor.aliases`), so the preview draws it and
        the file keeps it as written. A built-in takes the word too, as the
        lab's edit of it (Reset to default in the flavor editor undoes it)."""
        import flavor_presets as fp
        eng = self._gce_engine()
        m = self._gce_model_now()
        if eng is None or m is None:
            return
        fl = eng.get_flavor(m.flavor)
        al = [a for a in fl.aliases
              if a.partition("=")[0].strip().upper() != word.upper()]
        fl.aliases = tuple(al + ["%s=%s" % (word, cmd)])
        fp.save(self._flavors_dir(), fl)
        self._flavors_changed(edited=fl.name)
        what = "printing moves" if cmd == "G1" else "travels"
        self._gce_status(
            "%s now reads as %s in %s. Change it under Syntax > Edit "
            "flavors%s." % (word, what, fl.label,
                            ", where Reset to default brings back the "
                            "shipped one" if fl.builtin else ""))

    def _gce_not_a_move(self, word):
        """`word` is the lab's own command, not a move: never ask again, in
        this text's flavor (a built-in's as the lab's edit of it)."""
        import flavor_presets as fp
        eng = self._gce_engine()
        m = self._gce_model_now()
        if eng is None or m is None:
            return
        fl = eng.get_flavor(m.flavor)
        if word.upper() not in {k.upper() for k in fl.known}:
            fl.known = tuple(list(fl.known) + [word])
            fp.save(self._flavors_dir(), fl)
            self._flavors_changed(edited=fl.name)
        self._gce_check_unread()

    def _gce_export_gaps(self):
        """`mew_gcode.export_gaps` of the document Save would write, or []."""
        eng = self._gce_engine()
        m = self._gce_model_now()
        if eng is None or m is None:
            return []
        try:
            return eng.export_gaps(m)
        except Exception:  # noqa: BLE001 -- a check never blocks a save
            return []

    def _gce_gaps_notice(self, gaps):
        """Save paused: the commands the preview reads that the flavor
        can't write. Save anyway, or fix the flavor."""
        eng = self._gce_engine()
        g = gaps[0]
        n = len(g.lines)
        what = eng.MEANING_NAMES.get(g.cmd, g.cmd)
        where = " (%d line%s)" % (n, "" if n == 1 else "s") if n else ""
        more = (" %d more command%s like it." % (
            len(gaps) - 1, "" if len(gaps) == 2 else "s")
            if len(gaps) > 1 else "")
        send = getattr(self, "_gce_gaps_for", None) == "send"
        buttons = [("Send anyway" if send else "Save anyway",
                    self._gce_send_anyway if send
                    else self._gce_save_anyway),
                   ("Edit flavors…", self._gce_edit_flavors)]
        if g.lines:
            buttons.append(("Go to line",
                            lambda: self.gce.goto_line(g.lines[0] + 1)))
        self._gce_notice_show(
            "%s: %s%s — %s.%s The printer may not do what the "
            "preview shows." % ("Not sent yet" if send else "Not saved yet",
                                what[0].upper() + what[1:], where, g.message,
                                more),
            buttons, kind="export-gap")

    def _gce_save_anyway(self):
        self._gce_notice_hide_if("export-gap")
        self._gce_gaps_ok = True
        try:
            self.save_gcode_as()
        finally:
            self._gce_gaps_ok = False

    def _gce_explain_line(self, line):
        m = self._gce_model_now()
        if m is None:
            return super()._gce_explain_line(line)
        msg = ""
        try:
            r = self._gce_render()
            if r.text == self.gce.get_text():
                msg = m.explain_line(line - 1, r)
            else:
                msg = self._gce_engine().explain(self.gce.current_line_text(),
                                                 m.flavor)
        except Exception:  # noqa: BLE001 -- an explainer never breaks typing
            msg = ""
        self.gce_explain_var.set(f"Line {line}: {msg}" if msg else "")

    def _gce_marks(self):
        """Gutter marks and tints from where each line came from."""
        m = self._gce_model_now()
        if m is None:
            return super()._gce_marks()
        try:
            r = self._gce_render()
        except Exception:  # noqa: BLE001
            return super()._gce_marks()
        if r.text != self.gce.get_text():
            return super()._gce_marks()
        marks, tinted = {}, []
        for i, rl in enumerate(r.lines, start=1):
            mk = _ORIGIN_MARK.get(rl.origin)
            if mk:
                marks[i] = mk
                tinted.append(i)
        return marks, [], tinted

    def _gce_output_chips(self):
        if self._gce_doc["source"] == _DETACHED:
            saved = self._gce_doc["source"]
            self._gce_doc["source"] = self._GCE_LINKED
            try:
                return super()._gce_output_chips()
            finally:
                self._gce_doc["source"] = saved
        return super()._gce_output_chips()

    def _refresh_gcode_display_on_toggle(self):
        """An output option flipped: a detached copy re-renders too (its
        base is frozen, its options are still the Export card's)."""
        if self._gce_doc["source"] == _DETACHED and hasattr(self, "gce") \
                and not getattr(self, "_loading_preset", False):
            self._gce_rerender(self._gce_snap())
            return
        super()._refresh_gcode_display_on_toggle()

    # ------------------------------------------------------------------ #
    # the header: linked / detached / opened file
    # ------------------------------------------------------------------ #
    def _gce_set_link_label(self):
        super()._gce_set_link_label()
        doc = self._gce_doc
        if doc["source"] == _DETACHED:
            self.gce_link_var.set("● A frozen copy of the g-code")
            self._gce_src_tool(self._gce_relink_btn, True)

    def _gce_detach(self):
        """Stop following the designer. Nothing in the text changes."""
        doc = self._gce_doc
        m = self._gce_model_now()
        if doc["source"] != self._GCE_LINKED or m is None:
            return
        self._gce_record_edits()
        self._gce_stop_regen()
        m.detach()
        self._gce_pending_design = None
        doc["source"] = _DETACHED
        self._gce_notice_hide_if("design-moved")
        self._gce_set_link_label()
        self._gce_render_changes()
        self._gce_sync_persist()

    def _gce_relink(self):
        """Follow the designer again -- after listing, inline, what carries
        over (spec 3: "re-linking is offered, with a list of the edits it
        would carry")."""
        doc = self._gce_doc
        if not getattr(self, "_gce_splicing", False):
            self._gce_splice = None     # only New scaffold's relink adopts
        if getattr(self, "_gce_relink_armed", False):
            self._gce_relink_armed = False
            self._gce_notice_hide()
            # the design's text arrives in `_gce_take_design` (a regenerate
            # can take a moment): its undo step takes this as `before`
            self._gce_page_before = self._gce_snap_page()
            m = self._gce_model_now()
            # the status line still described the text being left ("Opened
            # <file>...") under this page's design
            back = ("Back to this page's design: the text is its g-code "
                    "again and follows it as it changes. Undo brings the "
                    "other text back.")
            if doc["source"] == _DETACHED and m is not None:
                m.relink()
                doc["source"] = self._GCE_LINKED
                doc["sig"] = None
                self._gce_set_link_label()
                self._gce_check_design()
                self._gce_status(back)
                return
            self._gce_model = None
            out = super()._gce_relink()
            self._gce_status(back)
            return out
        if doc["source"] == _DETACHED:
            m = self._gce_model_now()
            labels = dict((c.id, lab) for c, lab in m.chips()) if m else {}
            raw = doc.get("pending_raw") or doc.get("raw") or ""
            try:
                carry = m.relink_preview(raw, self._gce_line_map()) if m \
                    else []
            except Exception:  # noqa: BLE001
                carry = [(cid, True) for cid in labels]
            keep = [labels[c] for c, ok in carry if ok and c in labels]
            lost = [labels[c] for c, ok in carry if not ok and c in labels]
            text = ("Go back to this page's design? The text becomes "
                    "its g-code, with these changes carried "
                    "over: " + ("; ".join(keep) if keep else "none") + ".")
            if lost:
                text += (" These lose their place and wait in the tree "
                         "under Whole file: " + "; ".join(lost) + ".")
        else:
            text = ("Show this page's design again? Your file "
                    "stays on disk as it is; this page stops showing it "
                    "(Undo brings the text back).")
        self._gce_relink_armed = True

        def cancel():
            self._gce_relink_armed = False
            self._gce_notice_hide()
        self._gce_notice_show(text, [("Back to the design", self._gce_relink),
                                     ("Cancel", cancel)], kind="relink")

    # ------------------------------------------------------------------ #
    # the left column: My library (top) and the tree (bottom)
    # ------------------------------------------------------------------ #
    _GCE_ADD = "Add lines"
    _GCE_CHANGE = "Change existing lines"
    _GCE_CUSTOM = "Your own g-code"
    _GCE_SPOT = "One spot"
    #: Insert forms the page does not offer: comments (exported g-code is
    #: comment-free) and Set speed (the Speed tool does that, on the moves)
    _GCE_NOT_OFFERED = ("comment", "set_speed")
    #: Insert forms whose edit goes by its g-code line, not a field value
    _GCE_NAMED_BY_LINE = ("line_to", "arc_cw", "arc_ccw", "move_to")
    _GCE_SCOPES = (("all", "The whole file"), ("layers", "Layers"),
                   ("group", "Scaffolds"), ("piece", "Stitched pieces"))
    #: scopes the form holds as they are, with no Applies to row: an edit,
    #: exact lines, a multi-selection -- picked by selecting, not typed
    _GCE_FIXED = ("edit", "lines", "set")
    #: Repeat on an edit: where its copies go
    _GCE_REPEAT_HERE = "Right after it"
    #: ...its two plain patterns, first in its list: after layer N, 2N, 3N
    #: to the end; every N moving lines
    _GCE_REP_LAYERS = "Every N layers"
    _GCE_REP_LINES = "Every N lines"
    #: the places an added edit can go by itself; a pattern (after each
    #: layer, every N moves) is the Repeat tool's
    _GCE_ONE_PLACE = ("start", "end", "before_first_print")
    _GCE_REP_UNIT = {_GCE_REP_LAYERS: "layers", _GCE_REP_LINES: "lines"}
    #: the N a Repeat's pattern starts on: never 1 for lines, where a copy
    #: after every line of a big print floods the text and the picture
    _GCE_REP_N0 = {"layers": 1, "lines": 100}
    _GCE_PATTERN_N0 = {"every_n_mm": 50, "every_n_seconds": 60}

    def _build_gce_quadrants(self, top, bottom):
        """The browser's tree in `bottom`. This file's edits are the tree's
        rows -- one list, not a library beside an anatomy. The library's
        boxes are still built and kept up to date in `top` (a holder that is
        never shown), because the shared refresh writes them; what is saved
        is reached from the ribbon's Insert menu."""
        box = ttk.Frame(top)
        self._gce_unsaved_box = ttk.Frame(box)
        self._gce_unsaved_box.columnconfigure(0, weight=1)
        self._gce_saved_box = ttk.Frame(box)
        self._gce_saved_box.columnconfigure(0, weight=1)
        self._gce_examples_box = ttk.Frame(box)
        self._gce_examples_box.columnconfigure(0, weight=1)
        self._build_gce_anatomy(bottom)
        self._gce_render_library()

    def _gce_head_refresh(self):
        link = self.gce_link_var.get()
        source = link[2:] if link.startswith("● ") \
            and "page's design" not in link else ""
        work = self.gce_work_var.get()
        if source:
            work = source + " · unsaved (Ctrl+S)" if "unsaved" in work \
                else source
        self._gce_head_var.set(work)
        tip = ("Where this text comes from. This page's design: it updates "
               "when you change the design here. A file: never written back "
               "to.\n\nNow: " + (link[2:] if link.startswith("● ") else link))
        stats = self.gce_stats_var.get()
        if stats:
            tip += "\n\n" + stats + ": at the commanded speed, the length " \
                   "of fiber laid and the volume of polymer, worked out from " \
                   "this text as it is now."
        self._gce_head_tip.text = tip

    def _build_gce_anatomy(self, bottom):
        """The tree: Whole file, Header, each scaffold and its layers, End,
        every edit under the part it changes. Sized to its rows (up to a
        cap, then it scrolls)."""
        head = ttk.Frame(bottom, style="GceBar.TFrame", padding=(4, 0))
        head.grid(row=0, column=0, sticky="ew", pady=(0, 2))
        self._gce_tree_head = head
        head.columnconfigure(4, weight=1)
        # the whole panel folds to this title bar (remembered)
        self._gce_fold_btn = ttk.Button(head, text="▾", width=2,
                                        style="GceBar.Link.TButton",
                                        command=self._gce_tree_fold)
        self._gce_fold_btn.grid(row=0, column=0, sticky="w")
        add_tip(self._gce_fold_btn, "Fold the tree away, or open it again.")
        title = gui_widgets.section_header(head, "Print")
        title.configure(style="GceBar.SectionHeader.TLabel")
        title.grid(row=0, column=1, sticky="w")
        self.gce_count_var = tk.StringVar(value="")
        count = gui_form.mark(ttk.Label(head, textvariable=self.gce_count_var,
                                        style="GceBar.Hint.TLabel"), "live")
        count.grid(row=0, column=2, sticky="w", padx=(10, 0))
        # what is off the usual, on this bar so it takes no row of its own:
        # text that is not this page's design, unsaved work. What the print
        # costs is in the bar's tip
        self._gce_head_var = tk.StringVar(value="")
        note = gui_form.mark(ttk.Label(head, textvariable=self._gce_head_var,
                                       style="GceBar.Hint.TLabel"), "live")
        note.grid(row=0, column=3, sticky="w", padx=(10, 0))
        # The bar is clipped to its words (`_gce_tree_shape`), but the frame
        # spans the panel, so new words never resize it: leaving Calibrate
        # kept "Calibration print: ..."'s width as an empty grey bar. The
        # labels do resize, so they ask for a fresh clip.
        for lab in (count, note):
            lab.bind("<Configure>", self._gce_tree_shape_soon, add="+")
        self._gce_head_tip = Tooltip(title, "")
        for var in (self.gce_link_var, self.gce_work_var,
                    self.gce_stats_var):
            var.trace_add("write", lambda *_a: self._gce_head_refresh())
        self._gce_head_refresh()
        self._gce_sel = None
        frame = ttk.Frame(bottom)
        frame.grid(row=1, column=0, sticky="nsew")
        self._gce_tree_frame = frame
        bottom.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        # extended: Ctrl- and Shift-click pick several rows, and one tool
        # then acts on all of them. Each row is one bar as wide as its text,
        # as in Fusion's browser, so the columns hold their values but none
        # is drawn: `_gce_row_text` writes them into the row's text
        from gui_gcode_cad import tree_row_style
        self._gce_chevrons = tree_row_style(frame, "GceTree.Treeview")
        tree = ttk.Treeview(frame, show="tree", selectmode="extended",
                            style="GceTree.Treeview", columns=("sum", "x"),
                            displaycolumns=(), height=4)
        tree.column("#0", width=150, stretch=True)
        tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)

        def _scrolled(lo, hi):
            sb.set(lo, hi)
            self._gce_tree_shape_soon()
        tree["yscrollcommand"] = _scrolled
        self._gce_tree_sb = sb
        self._gce_tree_shape_after = None
        self._gce_tree_offs = {}
        self._gce_tree_bars = {}
        self._gce_tree_names = {}
        for w in (tree, head, getattr(self, "_gce_props", None)):
            if w is not None:
                w.bind("<Configure>", self._gce_tree_shape_soon, add="+")
        tree.tag_configure("edit", foreground=_EDIT_FG)
        tree.tag_configure("off", foreground=_OFF_FG)
        tree.tag_configure("lost", foreground=_LOST_FG)
        tree.tag_configure("piece", foreground=_PIECE_FG)
        tree.tag_configure("auto", foreground=_AUTO_FG)
        gui_widgets.scroll_guard(tree)
        tree.bind("<ButtonPress-1>", self._gce_tree_press, add="+")
        tree.bind("<B1-Motion>", self._gce_tree_motion, add="+")
        tree.bind("<ButtonRelease-1>", self._gce_tree_release, add="+")
        tree.bind("<Double-Button-1>", self._gce_tree_double, add="+")
        tree.bind("<Return>", lambda _e: self._gce_tree_open(tree.focus()),
                  add="+")
        # an edit row: Space turns it off or on, Delete takes it out
        tree.bind("<space>", lambda _e: self._gce_tree_key("toggle"),
                  add="+")
        tree.bind("<Delete>", lambda _e: self._gce_tree_key("delete"),
                  add="+")
        # the page's undo, as in the text. Ctrl+F is left to the app-wide
        # finder, which Home promises on every page and which offers the
        # text's Find as "Find in the g-code"
        for seq, act in (("<Control-z>", lambda: self._gce_undo()),
                         ("<Control-y>", lambda: self._gce_redo()),
                         ("<Control-Z>", lambda: self._gce_redo())):
            tree.bind(seq, lambda _e, act=act: (act(), "break")[1])
        # the eyes: click one to hide or show; right-click for Show only
        # this and Show all
        from gui_gcode_cad import eye_images
        self._gce_eyes = eye_images(tree)
        self._gce_hidden = set()
        self._gce_isolated = None
        tree.bind("<Button-3>", self._gce_tree_menu, add="+")
        for ev in ("<<TreeviewOpen>>", "<<TreeviewClose>>"):
            tree.bind(ev, lambda _e: (
                self.root.after_idle(self._gce_tree_fit),
                self._gce_tree_shape_soon()), add="+")
        self._gce_tree = tree
        self._gce_tree_rows = None
        self._gce_tree_info = {}
        self._gce_home_of = {}
        self._gce_press = None
        self._gce_dragging = False
        if hasattr(self, "_gce_prefs") and self._gce_prefs().get(
                "tree_folded"):
            self._gce_tree_fold(True)

    _GCE_TREE_MAX_ROWS = 22

    def _gce_tree_fit(self):
        """The tree as tall as its open rows, up to the cap; past it, a
        scrollbar."""
        tree = self._gce_tree
        n = 0
        stack = list(tree.get_children(""))
        while stack:
            x = stack.pop()
            n += 1
            if tree.item(x, "open"):
                stack.extend(tree.get_children(x))
        cap = min(self._GCE_TREE_MAX_ROWS,
                  getattr(self, "_gce_tree_cap", None)
                  or self._GCE_TREE_MAX_ROWS)
        h = max(3, min(n, cap))
        if int(tree.cget("height")) != h:
            tree.configure(height=h)
        if n > cap:
            self._gce_tree_sb.grid(row=0, column=1, sticky="ns")
        else:
            self._gce_tree_sb.grid_remove()

    def _gce_tree_rowheight(self):
        try:
            return int(ttk.Style().lookup("GceTree.Treeview", "rowheight")
                       or 20)
        except (tk.TclError, ValueError):
            return 20

    #: the picture shows between two rows' bars, this many pixels of it
    _GCE_BAR_GAP = 2
    #: the bar's room after its text, and the ✕'s share of a bar's right end
    _GCE_BAR_PAD = 10
    _GCE_BAR_X = 24
    #: an edit's row keeps its ✕ on the bar: a longer name is cut short
    _GCE_ROW_NAME_MAX = 30

    def _gce_row_text(self, text, values):
        """What a row draws: its name, then its summary and an edit's ✕ on
        the same bar. The name alone is `_gce_tree_name`."""
        summ, x = values
        if x and len(text) > self._GCE_ROW_NAME_MAX:
            text = text[:self._GCE_ROW_NAME_MAX - 1] + "…"
        return "    ".join(t for t in (text, summ, x) if t)

    def _gce_tree_name(self, iid):
        """A row's name, without what `_gce_row_text` adds to it."""
        return self._gce_tree_names.get(iid, "")

    def _gce_tree_on_x(self, iid, x):
        """Is `x` on the ✕ at the right end of this edit's bar?"""
        bar = self._gce_tree_bars.get(iid)
        return bool(bar) and iid.startswith("e:") \
            and bar[2] - self._GCE_BAR_X <= x <= bar[2]

    def _gce_tree_x_spot(self, iid):
        """The tree's (x, y) of the ✕ on this edit's bar, or None."""
        bar = self._gce_tree_bars.get(iid)
        if not bar:
            return None
        return bar[2] - self._GCE_BAR_X // 2, (bar[1] + bar[3]) // 2

    def _gce_tree_offsets(self, y):
        """Where the row at `y` starts its chevron, eye and text, read off
        the tree itself; {} until the row has drawn an eye."""
        tree = self._gce_tree
        out = {}
        for x in range(tree.winfo_width()):
            el = str(tree.identify_element(x, y))
            for part in ("indicator", "image", "text"):
                if part in el:
                    out.setdefault(part, x)
            if "text" in out:
                return out if len(out) == 3 else {}
        return {}

    def _gce_tree_shape_soon(self, _e=None):
        if self._gce_tree_shape_after is None:
            self._gce_tree_shape_after = self.root.after_idle(
                self._gce_tree_shape)

    def _gce_tree_shape(self):
        """Clip the browser's window to its title bar and one bar per row,
        each as wide as what it holds: the picture shows everywhere else,
        and a click there reaches the picture. Fusion's browser, in a
        toolkit whose widgets are opaque. Each bar goes in
        `_gce_tree_bars`: iid -> (left, top, right, bottom), in the tree's
        pixels."""
        self._gce_tree_shape_after = None
        box = getattr(self, "_gce_browser", None)
        tree = self._gce_tree
        if box is None:
            return
        try:
            bx, by = box.winfo_rootx(), box.winfo_rooty()
            tx, ty = tree.winfo_rootx() - bx, tree.winfo_rooty() - by
            font = ttk.Style().lookup("GceTree.Treeview", "font")
            bars = {}
            want = 0
            # a row half scrolled out keeps a whole bbox: its bar stops at
            # the tree's edge, or the frame behind shows as a strip below
            # the last row (light under a dark tree, dark under a light one)
            seen = tree.winfo_height()
            stack = [(x, 0) for x in reversed(tree.get_children(""))] \
                if self._gce_tree_frame.winfo_ismapped() else []
            while stack:
                iid, depth = stack.pop()
                kids = tree.get_children(iid)
                if tree.item(iid, "open"):
                    stack.extend((k, depth + 1) for k in reversed(kids))
                bb = tree.bbox(iid)
                if not bb or bb[1] > seen - 4:
                    continue
                y = bb[1] + bb[3] // 2
                off = self._gce_tree_offs.get(depth) \
                    or self._gce_tree_offsets(y)
                if not off:
                    continue
                self._gce_tree_offs[depth] = off
                wide = int(tree.tk.call("font", "measure", font,
                                        tree.item(iid, "text")))
                right = off["text"] + wide + self._GCE_BAR_PAD
                want = max(want, right)
                bars[iid] = (off["indicator" if kids else "image"], bb[1],
                             min(right, bb[0] + bb[2]),
                             min(seen, bb[1] + bb[3] - self._GCE_BAR_GAP))
            self._gce_tree_bars = bars
            rects = [(tx + a, ty + b, tx + c, ty + d)
                     for a, b, c, d in bars.values()]
            # the title bar, as wide as its words
            head = self._gce_tree_head
            hx, hy = head.winfo_rootx() - bx, head.winfo_rooty() - by
            _x, _y, used, _h = head.grid_bbox(0, 0, 3, 0)
            rects.append((hx, hy, hx + used + self._GCE_BAR_PAD,
                          hy + head.winfo_height()))
            # ...and the box at least that wide: Calibrate's title ("PRINT
            # Calibration print: Jet lag (plate)") is wider than its short
            # slide rows, and sizing to the rows alone cut it mid-word
            want = max(want, hx - tx + used + self._GCE_BAR_PAD)
            # the scrollbar past the cap, and the box of an opened file's
            # convert settings, keep their whole rectangles
            for w in (self._gce_tree_sb, getattr(self, "_gce_props", None)):
                if w is not None and w.winfo_ismapped() \
                        and w.winfo_height() > 1:
                    x, y = w.winfo_rootx() - bx, w.winfo_rooty() - by
                    rects.append((x, y, x + w.winfo_width(),
                                  y + w.winfo_height()))
        except tk.TclError:
            return
        from gui_gcode_cad import clip_window
        clip_window(box, rects)
        # the widest bar sets the browser's width, so no row is cut off: the
        # room beside the shorter bars is the picture's all the same. The
        # margin is the tree's inset, never box less tree: those two widths
        # are mid-change here, and reading them fed each layout's change
        # back as the next one's, forever (an opened file froze the app)
        sb = self._gce_tree_sb
        want += 2 * tx + (sb.winfo_reqwidth() if sb.winfo_ismapped() else 0)
        if want != getattr(self, "_gce_tree_want", 0)                 and hasattr(self, "_gce_layout_soon"):
            self._gce_tree_want = want
            self._gce_layout_soon()

    def _gce_tree_fold(self, folded=None):
        """Fold the tree panel to its title bar, or open it (remembered)."""
        frame = self._gce_tree_frame
        if folded is None:
            folded = bool(frame.winfo_manager())
        if folded:
            frame.grid_remove()
        else:
            frame.grid()
        self._gce_fold_btn.configure(text="▸" if folded else "▾")
        self._gce_tree_shape_soon()
        if hasattr(self, "_gce_prefs_set") \
                and bool(self._gce_prefs().get("tree_folded")) != folded:
            self._gce_prefs_set(tree_folded=folded)
        if hasattr(self, "_gce_layout_soon"):
            self._gce_layout_soon()

    # -- what an edit is called, and where it is ---------------------------- #
    @staticmethod
    def _gce_fmt(v):
        if v is None:
            return ""
        if isinstance(v, float) or (isinstance(v, int)
                                    and not isinstance(v, bool)):
            # twelve figures, not six: the field's text IS the stored value
            # (a Speed tool opened on 400 mm/min holds 6.666... mm/s), and
            # six figures came back from the form as F400.0002
            return gui_units.store_number(v)
        return str(v)

    def _gce_first_line(self, c):
        eng = self._gce_engine()
        m = self._gce_model_now()
        try:
            lines = eng.edit_lines(c, m.flavor)
        except Exception:  # noqa: BLE001
            return ""
        first = (lines[0] if lines else "").strip()
        if len(first) > 40:
            first = first[:39] + "…"
        return first + (f" (+{len(lines) - 1})" if len(lines) > 1 else "")

    def _gce_edit_name(self, c):
        """The name an edit goes by: the user's, else what it is ("Dwell
        2 s", "Scale X 120%", the first line of your own g-code), its speed
        in the Settings panel's display unit like every speed field."""
        if c.label:
            return c.label
        return gui_units.localize(self._gce_edit_what(c))

    def _gce_edit_what(self, c):
        eng = self._gce_engine()
        m = self._gce_model_now()
        if isinstance(c, (eng.Insert, eng.Rule)):
            forms = {k: (lab, f) for k, lab, f in eng.INSERT_FORMS}
            kind = c.what if c.what in forms else (c.command or {}).get("kind")
            if kind in forms:
                lab, fields = forms[kind]
                if c.command and fields                         and kind not in self._GCE_NAMED_BY_LINE:
                    name, _l, unit, _d = fields[0]
                    v = c.command.get(name)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        return f"{lab} {v:g} {unit}".rstrip()
                    if v not in (None, ""):
                        return f"{lab}: {v}"
                    return lab
                if c.command and kind in self._GCE_NAMED_BY_LINE:
                    return self._gce_first_line(c) or lab
                if c.command:
                    return lab
                first = self._gce_first_line(c)
                return f"{lab}: {first}" if first else lab
            return self._gce_first_line(c) or self._GCE_CUSTOM
        if isinstance(c, eng.LineEdit):
            if c.op == "delete":
                return eng.delete_phrase(m, c)
            return "Changed line: " + eng.chip_label(m, c).partition(": ")[2]
        return eng.chip_label(m, c).partition(" · ")[0]

    def _gce_layers_words(self, sc):
        """A layers scope as the tree names its layers ("layer 5 of
        scaffold 1 · copy 2", "layers 3–5 of scaffold 1"), not the file's
        running numbers ("layers 15"); None for any other scope."""
        if sc is None or sc.kind != "layers" or sc.first in (None, ""):
            return None
        try:
            lo = int(sc.first)
            hi = int(sc.last) if sc.last not in (None, "") else lo
        except (TypeError, ValueError):
            return None
        by_ord = {i.get("ord"): i for i in self._gce_tree_info.values()
                  if i.get("kind") == "layer"}
        a, b = by_ord.get(lo), by_ord.get(hi)
        if a is None or b is None:
            return None
        if lo == hi:
            return a["label"]
        na, _, ga = a["label"].partition(" of ")
        nb, _, gb = b["label"].partition(" of ")
        if ga == gb and na.startswith("layer ") and nb.startswith("layer "):
            return f"layers {na[6:]}–{nb[6:]} of {ga}"
        return f"{a['label']} to {b['label']}"

    def _gce_edit_where(self, c):
        """Where an edit is, in the tree's words."""
        eng = self._gce_engine()
        m = self._gce_model_now()
        if isinstance(c, eng.Rule) and c.where not in ("start", "end",
                                                        "before_first_print"):
            return eng.chip_label(m, c).partition(" · ")[2]
        if isinstance(c, eng.Transform):
            where = self._gce_layers_words(c.scope) or                 eng.chip_label(m, c).partition(" · ")[2]
            return where or "the whole file"
        info = self._gce_tree_info.get(self._gce_home_of.get(c.id, ""), {})
        lines = getattr(self, "_gce_chip_lines", {}).get(c.id)
        if c.id in getattr(self, "_gce_unplaced", ()):
            return "needs a place"
        bits = [info.get("label", "")] if info.get("kind") != "file" else []
        if lines and isinstance(c, eng.Insert):
            bits.insert(0, f"line {lines[0]}")
        return " · ".join(b for b in bits if b) or "the whole file"

    # -- the tree -------------------------------------------------------- #
    def _gce_refresh_anatomy(self):
        tree = getattr(self, "_gce_tree", None)
        if tree is None:
            return
        m = self._gce_model_now()
        rows, info = [], {}
        if m is not None:
            try:
                rows, info = self._gce_anatomy_rows(m)
            except Exception:  # noqa: BLE001 -- the map never breaks the page
                return
        self._gce_tree_info = info
        self._gce_tree_sync(rows)
        self._gce_vis_apply()

    # -- show / hide: the tree's eyes, Fusion's way (display only) ---------- #
    # A hidden row is hidden in the picture and folded out of the g-code
    # text; it still prints, and Save and Generate write it. Hiding a row
    # hides what is under it. "Show only this" isolates one row.
    def _gce_row_hidden(self, iid):
        tree = self._gce_tree
        x = iid
        while x:
            if x in self._gce_hidden:
                return True
            x = tree.parent(x)
        iso = self._gce_isolated
        if iso and tree.exists(iso):
            for a, b in ((iid, iso), (iso, iid)):   # on the isolated line
                x = a
                while x:
                    if x == b:
                        return False
                    x = tree.parent(x)
            return True
        return False

    def _gce_row_lines(self, iid):
        """The row's lines as 1-based, inclusive ranges."""
        info = self._gce_tree_info.get(iid) or {}
        kind = info.get("kind")
        if kind == "file":
            n = self.gce.line_count()
            return [(1, n)] if n else []
        if kind in ("edit", "auto"):
            ls = sorted(getattr(self, "_gce_chip_lines", {})
                        .get(info.get("cid"), ()))
            out = []
            for i in ls:
                if out and i == out[-1][1] + 1:
                    out[-1][1] = i
                else:
                    out.append([i, i])
            return [tuple(r) for r in out]
        if info.get("first") and info.get("last"):
            return [(info["first"], info["last"])]
        return []

    def _gce_hidden_ranges(self):
        tree = self._gce_tree
        hide = []
        for iid in self._gce_hidden:
            if tree.exists(iid):
                hide += self._gce_row_lines(iid)
        iso = self._gce_isolated
        if iso and tree.exists(iso):
            nxt = 1
            for a, b in sorted(self._gce_row_lines(iso)):
                if a > nxt:
                    hide.append((nxt, a - 1))
                nxt = max(nxt, b + 1)
            n = self.gce.line_count()
            if nxt <= n:
                hide.append((nxt, n))
        out = []
        for a, b in sorted(hide):
            if out and a <= out[-1][1] + 1:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        return [tuple(r) for r in out]

    def _gce_vis_apply(self):
        """Fold the hidden rows' lines out of the text, cut them from the
        picture, and draw each row's eye."""
        if getattr(self, "_gce_tree", None) is None:
            return
        ranges = self._gce_hidden_ranges()
        self._gce_vis_ranges = ranges
        text_ranges = list(ranges)
        if getattr(self, "_gce_hide_header", None) is not None \
                and self._gce_hide_header.get():
            rendered = self._gce_render()
            lines = [i for i, line in enumerate(rendered.lines, 1)
                     if line.uid and line.uid[:2] == ("machine", "start")]
            if lines:
                text_ranges.append((min(lines), max(lines)))
        self.gce.set_hidden(text_ranges)
        eyes = self._gce_eyes
        tree = self._gce_tree
        stack = list(tree.get_children(""))
        while stack:
            x = stack.pop()
            img = eyes["off"] if self._gce_row_hidden(x) else eyes["on"]
            if str(tree.item(x, "image") or "") != str(img):
                tree.item(x, image=img)
            stack.extend(tree.get_children(x))
        if hasattr(self, "_gce_pv_show") \
                and getattr(self, "_gce_vis_drawn", None) != ranges:
            self._gce_vis_drawn = ranges
            self._gce_pv_show()

    def _gce_eye_toggle(self, iid):
        """The eye on a row: hide it, or show it again. A row hidden by a
        hidden parent comes back on its own: the parent shows again with
        everything else under it still hidden."""
        tree = self._gce_tree
        if iid.startswith("d:") and hasattr(self, "_gce_design_eye"):
            self._gce_design_eye(iid)
            return
        if self._gce_isolated:
            self._gce_isolated = None
        if iid in self._gce_hidden:
            self._gce_hidden.discard(iid)
        elif self._gce_row_hidden(iid):
            path = []
            x = iid
            while x:
                path.append(x)
                x = tree.parent(x)
            top = next((a for a in reversed(path) if a in self._gce_hidden),
                       None)
            if top is not None:
                # `path` runs iid -> root: from `top` down to iid's parent,
                # hide each one's other children
                self._gce_hidden.discard(top)
                for x in reversed(path[1:path.index(top) + 1]):
                    for c in tree.get_children(x):
                        if c not in path:
                            self._gce_hidden.add(c)
        else:
            self._gce_hidden.add(iid)
        self._gce_vis_apply()

    def _gce_isolate(self, iid):
        """Show only this row (and what is under it)."""
        self._gce_hidden.clear()
        self._gce_isolated = iid
        self._gce_vis_apply()

    def _gce_show_all(self):
        self._gce_hidden.clear()
        self._gce_isolated = None
        self._gce_vis_apply()

    def _gce_tree_menu(self, e):
        tree = self._gce_tree
        iid = tree.identify_row(e.y)
        if not iid:
            return
        menu = getattr(self, "_gce_eye_menu", None)
        if menu is None:
            menu = self._gce_eye_menu = tk.Menu(tree, tearoff=False)
        menu.delete(0, "end")
        if iid.startswith("a:"):
            info = self._gce_tree_info.get(iid) or {}
            menu.add_command(label="Open in My library\u2026", command=lambda:
                             self.open_script_library(
                                 select=info.get("script") or None))
            menu.add_command(label="Stop running automatically",
                             command=lambda: self._automatic_set(
                                 info.get("script"), False))
            menu.add_separator()
        if iid.startswith(("e:", "p:", "d:")) or (
                iid.startswith("n:g:") and hasattr(self, "_gce_design_open")):
            # opening an edit: here, or a double-click
            menu.add_command(label="Edit",
                             command=lambda: self._gce_tree_open(iid))
            if iid.startswith(("p:", "d:")):
                self._gce_tree_menu_post(menu, iid, e)
                return
            if iid.startswith("e:"):
                cid = self._gce_row_cid(iid)
                menu.add_command(label="Rename…",
                                 command=lambda: self._gce_rename(cid))
                menu.add_command(label="Repeat…", command=lambda: (
                    self._gce_select_edit(cid),
                    self._gce_tool_change("repeat")))
                menu.add_command(label="Save to My library",
                                 command=lambda: self._gce_save_id(cid))
                menu.add_command(label="Delete", command=lambda: (
                    tree.focus(iid), self._gce_tree_key("delete")))
            menu.add_separator()
        hidden = self._gce_row_hidden(iid)
        menu.add_command(label="Show" if hidden else "Hide",
                         command=lambda: self._gce_eye_toggle(iid))
        menu.add_command(label="Show only this",
                         command=lambda: self._gce_isolate(iid))
        menu.add_separator()
        menu.add_command(label="Show all", command=self._gce_show_all,
                         state="normal" if (self._gce_hidden
                                            or self._gce_isolated)
                         else "disabled")
        self._gce_tree_menu_post(menu, iid, e)

    def _gce_tree_menu_post(self, menu, iid, e):
        """Post the tree's right-click menu, after the Scaffold designer
        (gui_gcode_design) has added its items for row `iid`."""
        getattr(self, "_gce_design_menu", lambda m, i: None)(menu, iid)
        menu.tk_popup(e.x_root, e.y_root)

    def _gce_anatomy_rows(self, m):
        """``(rows, info)``: the tree's rows in order as (iid, parent, text,
        tags), parents before children, and what each iid stands for."""
        eng = self._gce_engine()
        r = self._gce_render()
        nodes = m.outline(r)
        own = {}
        places = {}
        for i, rl in enumerate(r.lines, start=1):
            if rl.chip:
                own.setdefault(rl.chip, []).append(i)
            if rl.uid and rl.uid[0] in ("rule", "rep") and len(rl.uid) > 2:
                # uid[1] wrote the place: `chip` is whichever operation last
                # rewrote the line, and it has the place too
                for who in (rl.uid[1], rl.chip):
                    places.setdefault(who, set()).add(rl.uid[2])
            elif rl.uid and rl.uid[0] == "ins" and len(rl.uid) > 2:
                places.setdefault(rl.chip, set()).add(
                    rl.uid[3] if len(rl.uid) > 3 else 0)
            elif rl.chip and rl.key and len(rl.key) > 2 \
                    and rl.key[1] == "body":
                # a changed line: one place per scaffold (array copy) it is in
                places.setdefault(rl.chip, set()).add((rl.key[0], rl.key[2]))
        # an edit's lines are what it writes: its own and its operations'
        lines_of = {}
        for c in m.changes:
            got = sorted({i for x in m.lineage(c.id) for i in own.get(x, ())})
            if got:
                lines_of[c.id] = got
        for k, v in own.items():
            lines_of.setdefault(k, v)
        self._gce_chip_lines = lines_of
        self._gce_unplaced = set(r.unplaced or ())
        info = {"n:file": dict(kind="file", key=(), first=None,
                               label="the whole file", ord=None)}
        order = [("n:file", "", "Whole file")]
        # rows a design layer adds above the file's own (the Collector):
        # (iid, label, what the line-range column says instead)
        for did, lab, summ in getattr(self, "_gce_top_rows", lambda: ())():
            info[did] = dict(kind="design", key=(), first=None, label=lab,
                             ord=None, group=None, summ=summ)
            order.append((did, "", lab))
        layers, groups, by_key = [], [], {}
        for n in nodes:
            if n.kind == "header":
                iid, lab, kind = "n:header", "Header", "header"
            elif n.kind == "exit":
                iid, lab, kind = "n:end", "End", "end"
            else:
                iid, lab, kind = "n:g:%s:%s" % tuple(n.key), n.label, "group"
                # the designer may name it truer ("Scaffold 1 · copy 2")
                lab = getattr(self, "_gce_design_group_label",
                              lambda i, nd, lb: lb)(iid, n, lab) or lab
            if iid in info:
                continue
            if kind == "group":
                groups.append(iid)
            info[iid] = dict(kind=kind, key=tuple(n.key), first=n.first + 1,
                             last=n.last + 1,
                             label=lab if kind == "group" else lab.lower(),
                             ord=len(groups) if kind == "group" else None)
            by_key[(kind, tuple(n.key))] = iid
            if kind == "group" and len(groups) == 1:
                self._gce_design_rows_into(None, None, "", info, order)
            order.append((iid, "", lab))
            if kind == "group":
                self._gce_design_rows_into(iid, n, iid, info, order)
            for ch in n.children:
                lid = "n:l:%s:%s:%s" % tuple(ch.key)
                layers.append(lid)
                # Calibrate names a specimen's repeats "Line k"
                lab_l = getattr(self, "_gce_design_layer_label",
                                lambda i, nd, lb: lb)(lid, ch, ch.label) \
                    or ch.label
                info[lid] = dict(kind="layer", key=tuple(ch.key),
                                 first=ch.first + 1, last=ch.last + 1,
                                 label="%s of %s" % (lab_l.lower(),
                                                     lab.lower()),
                                 ord=len(layers))
                by_key[("layer", tuple(ch.key))] = lid
                order.append((lid, iid, lab_l))
                self._gce_design_rows_into(lid, ch, lid, info, order)
        kids = {}
        home_of = {}
        # an edit of several parts (a Move of a layer and a scaffold): its
        # row under each; the first is its own, the others its copies
        also = {}
        ids = {c.id for c in m.changes}
        for c in m.changes:
            pid = m.parent_id(c)
            if pid in ids:
                # an operation on an edit: one branch deeper, under it
                kids.setdefault("e:" + pid, []).append(c)
                continue
            hs = ["n:file"] if c.id in self._gce_unplaced else \
                self._gce_homes(c, by_key, layers, groups)
            hs = [h for h in hs if h in info] or ["n:file"]
            home_of[c.id] = hs[0]
            for h in hs:
                kids.setdefault(h, []).append(c)
            if len(hs) > 1:
                also[c.id] = hs[1:]
        # the automatic scripts: not edits of this file but run on every
        # print, so each is a locked row where it fires
        for c in m.automatic_rules():
            h = self._gce_home(c, by_key, layers, groups)
            kids.setdefault(h if h in info else "n:file", []).append(c)
        for c in m.changes:
            # an operation's part is its edit's
            x, n = c, 0
            while m.parent_id(x) in ids and n < 64:
                x, n = m.get(m.parent_id(x)), n + 1
            if x is not c:
                home_of[c.id] = home_of.get(x.id, "n:file")
        self._gce_home_of = home_of
        pieces = {}
        for p in m.pieces:
            if p.source == "designer":
                continue
            h = next((g for g in groups if info[g]["key"][0] == p.id),
                     "n:file")
            pieces.setdefault(h, []).append(p)
        # how many edits sit in each part, its layers' included
        count = {}
        for h, cs in kids.items():
            cs = [c for c in cs if not c.id.startswith("rig:")]
            count[h] = count.get(h, 0) + len(cs)
            par = next((p for i, p, _t in order if i == h), "")
            if par:
                count[par] = count.get(par, 0) + len(cs)
        rows = []
        gaps = {}       # a removed layer's row: eid -> (scaffold, its layer)
        for iid, par, lab in order:
            nf = info[iid]
            if iid == "n:file":
                summ = ""
            elif nf.get("summ"):
                summ = nf["summ"]
            elif nf.get("first") and nf.get("last"):
                summ = f"{nf['first']}–{nf['last']}"
            else:
                summ = ""
            rows.append((iid, par, lab, (), (summ, "")))
            for p in pieces.get(iid, ()):
                pid = "p:" + p.id
                info[pid] = dict(kind="piece", pid=p.id, first=None,
                                 label="Stitched: " + (p.name or "piece"))
                rows.append((pid, iid, "Stitched: " + (p.name or "piece")
                             + ("" if p.on else " (off)"), ("piece",),
                             ("", "")))
            todo = [(iid, c) for c in reversed(kids.get(iid, ()))]
            while todo:
                par, c = todo.pop()
                if c.id.startswith("rig:"):
                    n_at = len(places.get(c.id, ()))
                    eid = "a:" + c.id
                    nm = c.saved or c.name or "Automatic script"
                    info[eid] = dict(kind="auto", cid=c.id, script=c.saved,
                                     first=(lines_of.get(c.id) or [None])[0],
                                     label=nm)
                    rows.append((eid, par, nm + " (automatic)", ("auto",),
                                 (("%d place%s" % (n_at, "" if n_at == 1 else "s"))
                                  if n_at else "no place", "")))
                    continue
                eid = "e:" + c.id
                if par in also.get(c.id, ()):
                    eid += "@" + par
                lost = c.id in self._gce_unplaced
                name = self._gce_edit_name(c)
                text = ("Needs a place: " if lost else "") + name \
                    + ("" if c.on else " (off)") \
                    + ("" if c.saved or not c.typed
                       or getattr(c, "op", "") == "delete"
                       or self._gce_chip_fixed(c) else " (typed)")
                tags = ("lost",) if lost else ("edit",) if c.on else ("off",)
                info[eid] = dict(kind="edit", cid=c.id,
                                 first=(lines_of.get(c.id) or [None])[0],
                                 label=name, alias="@" in eid)
                n = len(places.get(c.id, ()))
                rep = isinstance(c, eng.Transform) and c.op == "repeat" \
                    and m.parent_id(c)
                summ = "" if lost or not (isinstance(c, eng.Rule) or rep
                                          or n > 1) else (
                    f"{n} cop{'ies' if n != 1 else 'y'}" if rep
                    else f"{n} place{'s' if n != 1 else ''}")
                if c.id in also and not summ:
                    summ = f"{1 + len(also[c.id])} places"
                if isinstance(c, eng.LineEdit) and c.op == "delete" \
                        and c.more and not summ and not lost:
                    summ = f"{1 + len(c.more)} lines"
                gap = self._gce_gap_after(c, iid, info)
                if gap is not None:
                    gaps[eid] = gap
                rows.append((eid, par, text, tags, (summ, "✕")))
                if "@" in eid:
                    continue        # its operations sit under its own row
                todo += [(eid, k) for k in reversed(kids.get(eid, ()))]
        if gaps:
            rows = self._gce_seat_gaps(rows, info, gaps)
        self.gce_count_var.set(
            f"{len(m.changes)} edit{'s' if len(m.changes) != 1 else ''}"
            if m.changes else "")
        return rows, info

    @staticmethod
    def _gce_gap_after(c, home, info):
        """``(scaffold key, layer index)`` for an edit that removed a whole
        layer: its home is then the scaffold, and the row belongs where the
        layer was, between its neighbours. None for any other edit."""
        k = tuple(getattr(c, "key", ()) or ())
        if getattr(c, "op", "") != "delete" or len(k) < 5 or k[3] < 0 \
                or not str(home).startswith("n:g:"):
            return None
        return (home, k[0], k[2], k[3])

    @staticmethod
    def _gce_seat_gaps(rows, info, gaps):
        """Move each removed-layer row (and the rows under it) to sit
        between the layers either side of the one it removed, as the
        timeline of a CAD tree would show it."""
        for eid, (home, pid, g, L) in gaps.items():
            at = next((i for i, r in enumerate(rows) if r[0] == eid), None)
            if at is None:
                continue
            block, sub = [rows[at]], {eid}
            j = at + 1
            while j < len(rows) and rows[j][1] in sub:
                sub.add(rows[j][0])
                block.append(rows[j])
                j += 1
            rest = rows[:at] + rows[j:]
            first = next((i for i, r in enumerate(rest)
                          if r[1] == home and info.get(r[0], {}).get("kind")
                          == "layer" and tuple(info[r[0]]["key"][:3])
                          [2:] > (L,)), None)
            if first is None:
                h = next(i for i, r in enumerate(rest) if r[0] == home)
                tree = {home}
                first = h + 1
                while first < len(rest) and rest[first][1] in tree:
                    tree.add(rest[first][0])
                    first += 1
            rows = rest[:first] + block + rest[first:]
        return rows

    def _gce_design_rows_into(self, iid, node, parent, info, order):
        """The designer's rows (gui_gcode_design): under a scaffold or a
        layer row its own, or with `iid` None the Base row before the first
        scaffold. Each is ``(child_iid, label)``, iids prefixed ``d:``."""
        if not hasattr(self, "_gce_design_rows"):
            return
        try:
            rows = self._gce_design_rows(iid, node) or ()
        except Exception:  # noqa: BLE001 -- their rows never break the tree
            rows = ()
        for did, lab in rows:
            info[did] = dict(kind="design", key=(), first=None, label=lab,
                             group=iid)
            order.append((did, parent, lab))

    def _gce_homes(self, c, by_key, layers, groups):
        """The tree rows an edit sits under: its one part, or -- a change
        of several parts at once (a "set" scope: a Move of layer 3 of
        scaffold 1 and all of scaffold 2) -- each of them, when every one
        is a part of the tree."""
        eng = self._gce_engine()
        sc = getattr(c, "scope", None)
        if isinstance(c, eng.Transform) and sc is not None \
                and sc.kind == "set":
            out = []
            for sub in sc.first or ():
                sub = eng.Scope.from_dict(sub)
                if sub.kind == "edit":
                    return ["n:file"]
                h = self._gce_home(eng.Transform(op=c.op, scope=sub),
                                   by_key, layers, groups)
                if h == "n:file":
                    return ["n:file"]
                if h not in out:
                    out.append(h)
            if out:
                return out
        return [self._gce_home(c, by_key, layers, groups)]

    def _gce_row_cid(self, iid):
        """The edit a tree row stands for (its own row or one of its
        copies under another part), or None."""
        info = self._gce_tree_info.get(iid) or {}
        return info.get("cid") if info.get("kind") == "edit" else None

    def _gce_home(self, c, by_key, layers, groups):
        """The tree row an edit sits under."""
        eng = self._gce_engine()

        def by_line(k):
            k = tuple(k)
            if len(k) < 4:
                return "n:file"
            pid, sec, g, L = k[0], k[1], k[2], k[3]
            if sec == "header":
                return "n:header"
            if sec == "exit":
                return "n:end"
            if L >= 0:
                # a layer removed whole leaves its scaffold as the home
                return by_key.get(("layer", (pid, g, L))) \
                    or by_key.get(("group", (pid, g)), "n:file")
            return by_key.get(("group", (pid, g)), "n:file")

        def nth(seq, n):
            try:
                n = int(n)
            except (TypeError, ValueError):
                return "n:file"
            return seq[n - 1] if 1 <= n <= len(seq) else "n:file"
        if isinstance(c, eng.Insert):
            u, k = c.anchor.unit, tuple(c.anchor.key)
            if u in ("start", "header"):
                return "n:header"
            if u == "end":
                return "n:end"
            if u == "group":
                return by_key.get(("group", k), "n:file")
            if u == "layer":
                return by_key.get(("layer", k), "n:file")
            if u == "pass":
                return by_key.get(("layer", k[:3]), "n:file")
            return by_line(k)
        if isinstance(c, eng.LineEdit):
            return by_line(c.key)
        if isinstance(c, eng.Rule):
            if c.where in ("start", "before_first_print"):
                return "n:header"
            if c.where == "end":
                return "n:end"
            one = c.first not in (None, "") and str(c.first) == str(c.last)
            if one and c.where in ("before_layer", "after_layer"):
                return nth(layers, c.first)
            if one and c.where in ("before_group", "after_group"):
                return nth(groups, c.first)
            return "n:file"
        if isinstance(c, eng.Transform):
            sc = c.scope
            if sc is None or sc.kind in ("all", "set", "edit"):
                return "n:file"
            if sc.kind == "lines":
                homes = {by_line(k) for k in (sc.first or ())}
                return homes.pop() if len(homes) == 1 else "n:file"
            lo = sc.first or 1
            hi = sc.last if sc.last is not None else lo
            if str(lo) == str(hi):
                if sc.kind == "layers":
                    return nth(layers, lo)
                if sc.kind == "group":
                    return nth(groups, lo)
            return "n:file"
        return "n:file"

    def _gce_tree_sync(self, rows):
        """Bring the tree to `rows`, touching only the rows that changed:
        what is open stays open, nothing redraws that did not change. An
        edit that appears is scrolled into view, its part opened."""
        tree = self._gce_tree
        if rows == self._gce_tree_rows:
            return
        first = self._gce_tree_rows is None
        at = {}
        self._gce_tree_names = {r[0]: r[2] for r in rows}
        for iid, par, name, tags, values in rows:
            i = at.get(par, 0)
            at[par] = i + 1
            text = self._gce_row_text(name, values)
            if tree.exists(iid):
                if tree.parent(iid) != par or tree.index(iid) != i:
                    tree.move(iid, par, i)
                if tree.item(iid, "text") != text:
                    tree.item(iid, text=text)
                if tuple(tree.item(iid, "tags") or ()) != tuple(tags):
                    tree.item(iid, tags=tags)
                if tuple(tree.item(iid, "values") or ()) != tuple(values):
                    tree.item(iid, values=values)
            else:
                # the scaffolds open: their layers are what gets selected
                tree.insert(par, i, iid=iid, text=text, tags=tags,
                            values=values, open=(iid == "n:file"
                                                 or iid.startswith("n:g:")))
                if not first and iid.startswith("e:"):
                    tree.see(iid)
        want = {r[0] for r in rows}
        stack = list(tree.get_children(""))
        doomed = []
        while stack:
            x = stack.pop()
            if x not in want:
                doomed.append(x)
            else:
                stack.extend(tree.get_children(x))
        for x in doomed:
            if tree.exists(x):
                tree.delete(x)
        self._gce_tree_rows = rows
        self._gce_tree_fit()
        self._gce_tree_shape_soon()

    def _gce_tree_press(self, e):
        tree = self._gce_tree
        try:
            el = tree.identify_element(e.x, e.y)
        except tk.TclError:
            el = ""
        iid = tree.identify_row(e.y)
        self._gce_press = (iid, e.y, str(el), self._gce_tree_on_x(iid, e.x))
        self._gce_dragging = False

    def _gce_tree_motion(self, e):
        p = self._gce_press
        if p and p[0].startswith("e:") and abs(e.y - p[1]) > 6:
            self._gce_dragging = True
            self._gce_tree.configure(cursor="fleur")

    def _gce_tree_release(self, e):
        tree = self._gce_tree
        tree.configure(cursor="")
        p, self._gce_press = self._gce_press, None
        if not p or not p[0]:
            return
        if self._gce_dragging:
            self._gce_dragging = False
            dst = tree.identify_row(e.y)
            if dst.startswith(("e:", "p:")) or dst == "n:file:none":
                dst = tree.parent(dst)
            if dst and dst != tree.parent(p[0]) \
                    and self._gce_row_cid(p[0]):
                self._gce_move_edit(self._gce_row_cid(p[0]), dst)
            return
        if "indicator" in p[2]:
            return
        if "image" in p[2] and tree.identify_row(e.y) == p[0]:
            self._gce_eye_toggle(p[0])
            return
        if p[3] and tree.identify_row(e.y) == p[0]:
            tree.focus(p[0])
            self._gce_tree_key("delete")
            return
        if e.state & 0x5:                   # Shift or Control held
            self._gce_select_rows()
            return
        self._gce_tree_act(p[0])

    def _gce_tree_act(self, iid):
        """A single click on a row: it becomes the selection, the "where"
        the next tool works on -- an edit as much as a layer. Opening an
        edit is a double-click (`_gce_tree_open`)."""
        info = self._gce_tree_info.get(iid) or {}
        kind = info.get("kind")
        if kind == "edit":
            self._gce_select_edit(info["cid"])
        elif kind == "auto":
            self._gce_status("Runs on every print. Double-click to open it "
                             "in My library.")
        elif kind:
            self._gce_select_node(iid)

    def _gce_tree_double(self, e):
        """A double-click on an edit (or a stitched piece) opens it. Not on
        the eye or the ✕, whose clicks have already acted."""
        tree = self._gce_tree
        try:
            el = str(tree.identify_element(e.x, e.y))
        except tk.TclError:
            el = ""
        iid = tree.identify_row(e.y)
        if "image" in el or "indicator" in el \
                or self._gce_tree_on_x(iid, e.x):
            return "break"
        if iid:
            self._gce_tree_open(iid)
        return "break"

    def _gce_tree_open(self, iid):
        """Open a row: an edit in its form, a piece in the piece form; a part
        of the print is selected, as a single click does."""
        info = self._gce_tree_info.get(iid) or {}
        kind = info.get("kind")
        # the designer's form; a falsy return leaves the row to this page
        if kind in ("design", "group", "layer") and hasattr(
                self, "_gce_design_open") and self._gce_design_open(iid):
            return
        if kind == "auto":
            self.open_script_library(select=info.get("script") or None)
        elif kind == "header":
            def build(F, vals):
                vals["notes"] = tk.StringVar(value=self.gcode_header_notes.get())
                F.entry("Print notes", vals["notes"],
                        "Notes to include in this print's header.", width="path")
            def save(vals):
                self.gcode_header_notes.set(vals["notes"].get())
                self._refresh_gcode_display_on_toggle()
            self._gce_form_open("Header", build, save, ok_text="Done")
        elif kind == "edit":
            self._gce_open_edit(info["cid"],
                                tool=self._gce_is_simple(info["cid"]))
        elif kind == "piece":
            p = next((x for x in self._gce_model.pieces
                      if x.id == info["pid"]), None)
            if p is not None:
                self._gce_open_piece(p.source, p)
        elif kind:
            # a part of the print: it becomes the selection (the "where"
            # the next tool works on); an open edit moves only on Re-target
            self._gce_select_node(iid)

    def _gce_node_anchor(self, info):
        """The one spot at the END of an Anatomy part, or None (the whole
        file is not a spot)."""
        eng = self._gce_engine()
        kind = info.get("kind")
        if kind == "header":
            return eng.Anchor("header", "after")
        if kind == "end":
            return eng.Anchor("end", "after")
        if kind in ("group", "layer"):
            return eng.Anchor(kind, "after", tuple(info["key"]))
        return None

    def _gce_node_scope(self, info):
        """What a change-lines edit covers when dropped on a part."""
        eng = self._gce_engine()
        kind = info.get("kind")
        if kind == "file":
            return eng.Scope()
        if kind == "layer":
            return eng.Scope("layers", info["ord"], info["ord"])
        if kind == "group":
            return eng.Scope("group", info["ord"], info["ord"])
        return None

    def _gce_move_edit(self, cid, node_iid):
        """An edit dragged onto a part of the tree goes there: added
        lines to the end of that part, a change to that part."""
        eng = self._gce_engine()
        m = self._gce_model_now()
        info = self._gce_tree_info.get(node_iid) or {}
        if m is None or not info:
            return
        try:
            c = m.get(cid)
        except KeyError:
            return
        if isinstance(c, eng.LineEdit):
            self._gce_status("A changed line stays on its own line.")
            return
        fs = getattr(self, "_gce_fs", None)
        if fs is not None and fs["cid"] == cid:
            self._gce_form_take_node(info)
            return
        if isinstance(c, eng.Transform):
            scope = self._gce_node_scope(info)
            if scope is None:
                self._gce_status("A change covers the whole file, a scaffold "
                                 "or a layer: drop it on one of those.")
                return
            before = self._gce_snap()
            m.get(cid).scope = scope
        else:
            anchor = self._gce_node_anchor(info)
            if anchor is None:
                self._gce_status("Drop it on a part of the print: the "
                                 "header, a scaffold, a layer or the end.")
                return
            before = self._gce_snap()
            m.place(cid, anchor)
        self._gce_rerender(before)
        self._gce_focus_edit(cid, see=True)

    def _gce_focus_edit(self, cid, see=False):
        ids = self._gce_model_now().lineage(cid)
        lines = [i for i, rl in enumerate(self._gce_render().lines, start=1)
                 if rl.chip in ids]
        self.gce.set_focus(lines, see=see)
        return lines

    # -- the selection: the "where" every tool works on --------------------- #
    def _gce_select_node(self, iid, see=False, focus=True):
        """A part of the print (a tree row: the whole file, the header, a
        scaffold, a layer, the end) becomes the selection: its row is
        selected, its lines lit in the text and the picture. `focus` False
        keeps the keyboard off the text (a picture click)."""
        info = self._gce_tree_info.get(iid) or {}
        if not info or info.get("kind") == "edit":
            return
        tree = self._gce_tree
        if tree.exists(iid):
            tree.selection_set(iid)
            tree.focus(iid)
            if see:
                tree.see(iid)
        self._gce_sel = ("node", iid)
        first, last = info.get("first"), info.get("last")
        if getattr(self, "_gce_fs", None) is None:
            lines = list(range(first, last + 1)) if first and last else []
            self.gce.set_focus(lines, see=bool(lines))
        if first:
            self.gce.goto_line(first, focus=focus)
        self._gce_sel_show()

    def _gce_select_lines(self, a, b):
        """Lines selected in the g-code text become the selection."""
        self._gce_sel = ("text", a, b)
        node = self._gce_node_of_lines(a, b)
        tree = self._gce_tree
        if node and tree.exists(node):
            tree.selection_set(node)
            tree.see(node)
        self._gce_sel_show()

    def _gce_select_edit(self, cid, see=False):
        """An edit becomes the selection, as a layer does: the next tool
        works on it -- Repeat repeats it, Scale scales it, Delete takes it
        out. Its lines light up; it is not opened (a double-click opens)."""
        tree = self._gce_tree
        iid = "e:" + cid
        if tree.exists(iid):
            tree.selection_set(iid)
            tree.focus(iid)
            if see:
                tree.see(iid)
        self._gce_sel = ("edit", cid)
        lines = getattr(self, "_gce_chip_lines", {}).get(cid) or []
        if getattr(self, "_gce_fs", None) is None:
            self.gce.set_focus(lines, see=bool(lines))
        if lines:
            self.gce.goto_line(lines[0])
        self._gce_sel_show()

    def _gce_tree_targets(self):
        """The tree's selected rows as targets: ("node", iid) for a part of
        the print, ("edit", cid) for an edit. Pieces are not targets."""
        out = []
        for iid in self._gce_tree.selection():
            info = self._gce_tree_info.get(iid) or {}
            kind = info.get("kind")
            if kind == "edit":
                out.append(("edit", info["cid"]))
            elif kind and kind != "auto":
                out.append(("node", iid))
        return out

    def _gce_select_rows(self):
        """Ctrl- or Shift-clicked rows: several targets at once, so one tool
        acts on two edits and nothing else, or on three layers."""
        targets = self._gce_tree_targets()
        if len(targets) <= 1:
            if targets and targets[0][0] == "edit":
                self._gce_select_edit(targets[0][1])
            elif targets:
                self._gce_select_node(targets[0][1])
            return
        self._gce_sel = ("multi", targets)
        lines = []
        for t in targets:
            lines += self._gce_target_lines(t)
        if getattr(self, "_gce_fs", None) is None:
            self.gce.set_focus(sorted(set(lines)), see=False)
        self._gce_sel_show()

    def _gce_target_lines(self, t):
        """One target's lines (1-based)."""
        if t[0] == "edit":
            return list(getattr(self, "_gce_chip_lines", {}).get(t[1]) or ())
        info = self._gce_tree_info.get(t[1]) or {}
        if info.get("kind") == "file":
            return []
        f, l = info.get("first"), info.get("last")
        return list(range(f, l + 1)) if f and l else []

    def _gce_targets(self):
        """The selection as a list of targets: ("node", iid), ("edit", cid)
        or ("text", a, b)."""
        sel = getattr(self, "_gce_sel", None)
        if not sel:
            return []
        if sel[0] == "multi":
            return list(sel[1])
        return [sel]

    def _gce_root_edit(self, cid):
        """The edit at the top of an operation chain: what a line an
        operation wrote belongs to."""
        m = self._gce_model_now()
        ids = {c.id for c in m.changes}
        n = 0
        while n < 64:
            try:
                pid = m.parent_id(m.get(cid))
            except KeyError:
                break
            if pid not in ids:
                break
            cid, n = pid, n + 1
        return cid

    def _gce_text_scope(self, a, b):
        """Exactly lines a..b as a Scope: their generated lines by key, and
        the edits whose lines they are. None when there is nothing in them
        to change (a header or an option line alone)."""
        eng = self._gce_engine()
        m = self._gce_model_now()
        r = self._gce_render()
        ids = {c.id for c in m.changes}
        keys, edits = [], []
        for i in range(max(1, a), min(b, len(r.lines)) + 1):
            rl = r.lines[i - 1]
            if rl.key:
                keys.append(list(rl.key))
            elif rl.chip in ids:
                e = self._gce_root_edit(rl.chip)
                if e not in edits:
                    edits.append(e)
        subs = [{"kind": "edit", "first": e, "last": None} for e in edits]
        if keys:
            subs.insert(0, {"kind": "lines", "first": keys, "last": None})
        if not subs:
            return None
        if len(subs) == 1:
            return eng.Scope.from_dict(subs[0])
        return eng.Scope("set", subs)

    def _gce_on_pick(self, hit):
        """A click on the picture (the preview's pick seam): the one move
        that laid the fiber under it becomes the selection -- a selection
        of that line, as a g-code text selection is -- lit alone in the
        picture, the text scrolled to it and marked. A double-click widens
        it to the move's layer (as the tree row does); empty space clears."""
        if hit is None:
            self._gce_sel = None
            self._gce_hl_off = True     # the caret's line is not a selection
            tree = self._gce_tree
            tree.selection_remove(tree.selection())
            if getattr(self, "_gce_fs", None) is None:
                self.gce.set_focus([])
            self._gce_sel_show()
            return
        line = hit.get("line")
        if line and hit.get("clicks", 1) < 2:
            self._gce_select_move(int(line))
            return
        key = hit.get("key")
        iid = "n:l:%s:%s:%s" % tuple(key) if key and len(key) == 3 else None
        if iid and iid not in self._gce_tree_info:
            iid = "n:g:%s:%s" % tuple(key[:2])  # a part with no layer rows
        if iid and iid in self._gce_tree_info:
            self._gce_select_node(iid, see=True, focus=False)
        elif hit.get("first") and hit.get("last"):
            self._gce_select_lines(hit["first"], hit["last"])

    def _gce_select_move(self, line):
        """One g-code line (a picked move) becomes the selection: the tree
        shows the layer holding it, the text's caret goes to it, scrolled
        into view whether or not the text is open, and it alone is lit in
        the text and the picture. The keyboard stays with the picture."""
        t = self.gce.text
        t.tag_remove("sel", "1.0", "end")       # an old text selection
        self._gce_sel = ("text", line, line)
        if getattr(self, "_gce_fs", None) is None:
            self.gce.set_focus([line], see=True)
        self.gce.goto_line(line, focus=False)
        self._gce_select_lines(line, line)

    def _gce_pick_outline(self):
        """The print's parts, with the text they number: the panel answers
        nothing for a picture drawn from another text (the seam in
        `PreviewPanel._pick_outline_now`), so a stale click never selects
        the wrong lines. The selected lines light up through the page's own
        highlight (`_gce_hl_refresh`, which lights the focused lines)."""
        m = self._gce_model_now()
        if m is None:
            return [], None
        r = self._gce_render()
        return m.outline(r), r.text

    def _gce_on_text_select(self, _e=None):
        if not self.gce.text.tag_ranges("sel"):
            return
        a, b = self.gce._selected_lines()
        if self._gce_sel != ("text", a, b):
            self._gce_select_lines(a, b)

    def _gce_node_of_lines(self, a, b):
        """The smallest part of the print holding lines a..b, or None."""
        best = None
        for iid, info in self._gce_tree_info.items():
            f, l = info.get("first"), info.get("last")
            if info.get("kind") in ("edit", "piece", "file") or not f \
                    or not l or not (f <= a and b <= l):
                continue
            if best is None or l - f < best[0]:
                best = (l - f, iid)
        return best[1] if best else None

    def _gce_sel_node_info(self):
        """The selected part's info (a text selection: the part holding
        it), or None."""
        sel = getattr(self, "_gce_sel", None)
        if not sel:
            return None
        if sel[0] == "multi":
            sel = sel[1][-1]
        if sel[0] == "node":
            return self._gce_tree_info.get(sel[1])
        if sel[0] == "edit":
            return self._gce_tree_info.get("e:" + sel[1])
        node = self._gce_node_of_lines(sel[1], sel[2])
        return self._gce_tree_info.get(node) if node else None

    def _gce_target_label(self, t):
        if t[0] == "text":
            node = self._gce_node_of_lines(t[1], t[2])
            lab = (self._gce_tree_info.get(node) or {}).get("label", "")
            span = f"line {t[1]}" if t[1] == t[2] \
                else f"lines {t[1]}–{t[2]}"
            return span + (f", in {lab}" if lab else "")
        if t[0] == "edit":
            info = self._gce_tree_info.get("e:" + t[1]) or {}
            return "the edit “%s”" % info.get("label", t[1])
        return (self._gce_tree_info.get(t[1]) or {}).get("label", "—")

    def _gce_sel_show(self):
        sel = getattr(self, "_gce_sel", None)
        var = getattr(self, "gce_sel_var", None)
        self._gce_tools_refresh()
        self._gce_retarget_ask_sync()
        if var is None:
            return
        if not sel:
            var.set("Nothing selected. Click a fiber in the picture "
                    "(double-click: its layer), a layer or an edit in the "
                    "tree, or lines in the g-code")
            return
        ts = self._gce_targets()
        if len(ts) > 1:
            n_e = sum(1 for t in ts if t[0] == "edit")
            n_p = len(ts) - n_e
            bits = ([f"{n_e} edit{'s' if n_e != 1 else ''}"] if n_e else []) \
                + ([f"{n_p} part{'s' if n_p != 1 else ''} of the print"]
                   if n_p else [])
            what = " and ".join(bits)
        else:
            what = self._gce_target_label(ts[0])
        var.set(f"Selected: {what}. Pick a tool above.")

    def _gce_target_anchor(self, t):
        """Where an added line goes after one target, or None."""
        m = self._gce_model_now()
        eng = self._gce_engine()
        if t[0] == "text":
            return m.anchor_at(t[2], self._gce_render())
        if t[0] == "edit":
            try:
                c = m.get(self._gce_root_edit(t[1]))
            except KeyError:
                return None
            if isinstance(c, eng.Insert):
                # the same place: an added line after it, in list order
                return copy.deepcopy(c.anchor)
            lines = getattr(self, "_gce_chip_lines", {}).get(t[1]) or []
            return m.anchor_at(lines[-1], self._gce_render()) if lines \
                else None
        return self._gce_node_anchor(self._gce_tree_info.get(t[1]) or {})

    def _gce_sel_anchor(self):
        """Where an added line goes for the selection: after the selected
        part or edit, or after the last selected line. None with nothing to
        go after (the whole file)."""
        ts = self._gce_targets()
        if not ts or self._gce_model_now() is None:
            return None
        return self._gce_target_anchor(ts[-1])

    def _gce_target_scope(self, t):
        """What a change covers for one target, or None (the header, the
        end: no moves to change)."""
        eng = self._gce_engine()
        if t[0] == "text":
            return self._gce_text_scope(t[1], t[2])
        if t[0] == "edit":
            return eng.Scope("edit", t[1])
        return self._gce_node_scope(self._gce_tree_info.get(t[1]) or {})

    def _gce_sel_scope(self):
        """What a change covers for the selection: exactly what is selected
        -- a part, an edit, the selected lines, or several of them. None
        when none of it has moves to change."""
        eng = self._gce_engine()
        scopes = [s for s in (self._gce_target_scope(t)
                              for t in self._gce_targets()) if s is not None]
        if not scopes:
            return None
        if len(scopes) == 1:
            return scopes[0]
        if any(s.kind == "all" for s in scopes):
            return eng.Scope()
        subs = []
        for s in scopes:
            subs += s.first if s.kind == "set" else [s.to_dict()]
        return eng.Scope("set", subs)

    # -- the tools: select first, then a tool -------------------------------- #
    def _build_gce_tool_groups(self, bar, ribbon_group):
        """ADD and CHANGE SELECTION, the ribbon's first two groups. Every
        tool works on the selection (`_gce_sel`)."""
        eng = self._gce_engine()
        if eng is None:
            return
        tool = self._gce_tool
        tools = self._gce_tools = {}
        # the Scaffold designer's groups (gui_gcode_design), left of Add
        if hasattr(self, "_build_gce_design_groups"):
            self._build_gce_design_groups(bar, ribbon_group)
        # one frame, so Calibrate can take the groups off the ribbon whole
        edit = ttk.Frame(bar, style="Bar.TFrame")
        edit.pack(side="left", fill="y")
        self._gce_edit_groups = edit
        add = ribbon_group(edit, "Add")
        tools["insert"] = tool(
            add, "add", "insert", "Insert",
            "Add a command after the selection: a line or an arc, a "
            "dwell, a temperature, a pressure or voltage change, a message, "
            "your own lines, or one you saved.", menu=self._gce_insert_menu)
        smenu = tk.Menu(self.root, tearoff=False)
        # a stitched file is an edit of your own g-code, placed like any
        # other (the piece forms -- offset, rotation, layers -- are shelved
        # until they have a place; a document that has pieces still shows
        # and opens them)
        smenu.add_command(label="A g-code file…",
                          command=lambda: self._gce_stitch_edit("file"))
        smenu.add_command(label="Paste g-code…",
                          command=lambda: self._gce_stitch_edit("snippet"))
        tools["stitch"] = tool(
            add, "add", "stitch", "Stitch\nfile",
            "Put other g-code into this one, after the selection: a file, "
            "or lines you paste.", menu=smenu)
        tools["library"] = tool(
            add, "add", "library", "My\nlibrary",
            "Your saved edits: view them, open one to change it, or use it "
            "here. Save an edit from its form to keep it.",
            command=self._gce_library_toggle)
        chg = ribbon_group(edit, "Change selection")
        tools["speed"] = tool(
            chg, "change", "speed", "Speed",
            "Set the speed (F) of exactly the selected moves: a scaffold, a "
            "layer, lines, an edit, or several of them (Ctrl-click in the "
            "tree).", command=lambda: self._gce_tool_change("speed_set"))
        self._gce_move_menu = mmenu = tk.Menu(self.root, tearoff=False)

        def post_move_menu():
            for i, op in enumerate(self._gce_move_ops):
                if op is not None:
                    mmenu.entryconfigure(i, state="disabled"
                                         if self._gce_tool_why(op)
                                         else "normal")
            return mmenu
        self._gce_move_ops = []
        for op in ("offset", "scale", "layer_shift", "repeat", "speed_scale"):
            mmenu.add_command(label=self._gce_op_label(op),
                              command=lambda op=op:
                              self._gce_tool_change(op))
            self._gce_move_ops.append(op)
        mmenu.add_separator()
        self._gce_move_ops.append(None)
        for op, text in eng.TRANSFORM_KINDS:
            # Strip comments is Save's job now (Export's "Strip comments on
            # save"); a document that already carries one still applies it
            if op not in ("offset", "scale", "layer_shift", "repeat",
                          "speed_scale", "speed_set", "strip_comments"):
                mmenu.add_command(label=text, command=lambda op=op:
                                  self._gce_tool_change(op))
                self._gce_move_ops.append(op)
        if hasattr(self, "_gce_tool_move"):
            # Fusion's Move: the gimbal on the picture, the box for exact
            # distances (gui_gcode_move)
            tools["translate"] = tool(
                chg, "change", "translate", "Move",
                "Move the selected scaffolds and layers (M): drag the arrows "
                "or squares on the picture, or type the distances in its "
                "box. Select in the tree or the picture: click a fiber "
                "for its layer, double-click for its scaffold, Ctrl-click "
                "to add.", command=self._gce_tool_move)
            self.root.bind_all("<KeyPress-m>", self._gce_key_m, add="+")
        tools["move"] = tool(
            chg, "change", "move", "Transform",
            "Offset, scale, layer-shift or repeat the selection, or change "
            "it another way. The selection can be a part of the print, an "
            "edit, lines, or several (Ctrl-click in the tree).",
            menu=post_move_menu)
        tools["delete"] = tool(
            chg, "change", "delete", "Delete",
            "Delete the selection: the selected lines, a layer's lines, or "
            "the selected edits. Undo brings them back.",
            command=self._gce_tool_delete)
        # the Delete key is this tool, wherever on the page the focus is
        # (the tree has its own binding, `_gce_tree_key`, to the same end)
        self.root.bind_all("<Delete>", self._gce_key_delete, add="+")
        self._gce_tools_refresh()

    def _gce_op_label(self, op):
        eng = self._gce_engine()
        return "Repeat" if op == "repeat" else dict(eng.TRANSFORM_KINDS)[op]

    def _gce_tool_why(self, tool):
        """Why `tool` cannot act on the selection now -- one sentence on
        what to select -- or None when it can. `tool` is "insert",
        "stitch", "delete", or a transform kind."""
        eng = self._gce_engine()
        m = self._gce_model_now()
        if eng is None or m is None:
            return "There is no g-code yet."
        ts = self._gce_targets()
        if tool in ("insert", "stitch"):
            if self._gce_sel_anchor() is None:
                return ("Select where it goes first: a layer, a scaffold, "
                        "the header, the end, an edit, or lines in the "
                        "g-code.")
            return None
        if tool == "library":
            return None
        if tool == "delete":
            if not ts:
                return "Select what to delete first: lines, a layer or edits."
            for t in ts:
                if t[0] == "node":
                    info = self._gce_tree_info.get(t[1]) or {}
                    if info.get("kind") == "piece":
                        piece = next((p for p in m.pieces
                                      if p.id == info.get("pid")), None)
                        if len(ts) == 1 and piece and piece.source != "designer":
                            continue
                        return "Select one removable stitched piece."
                    if len(ts) == 1 and getattr(
                            self, "_gce_design_can_delete", lambda _i: False)(t[1]):
                        continue
                    if info.get("kind") == "design":
                        return "Select one removable design feature."
                    if info.get("kind") == "file" or not info.get("first"):
                        return "The whole file cannot be deleted. Select " \
                               "lines, a layer or edits."
                    if len(ts) > 1:
                        return "Delete takes several edits at once, or one " \
                               "part of the print."
            return None
        scope = self._gce_sel_scope()
        if scope is None:
            return ("Select what it changes first: the whole file, a "
                    "scaffold, a layer, an edit, or lines in the g-code.")
        if tool not in eng._GEOMETRIC and scope.kind != "all":
            return "%s works on the whole file: select Whole file." % \
                dict(eng.TRANSFORM_KINDS).get(tool, tool)
        if tool == "repeat" and scope.kind == "set":
            return "Repeat one edit or one part of the print at a time."
        if tool == "layer_shift" and any(
                eng.has_rotary_words(t) for t in [m.base_text] + [
                    p.text for p in m.pieces if p.on and p.text]):
            return ("Layer shift is for flat (3-axis) prints, and this file "
                    "turns a rotary axis.")
        return None

    def _gce_tools_refresh(self):
        """Grey the tools that cannot act on the selection, each saying
        why in its tooltip."""
        tools = getattr(self, "_gce_tools", None)
        if not tools:
            return
        for key, tool in (("insert", "insert"), ("stitch", "stitch"),
                          ("speed", "speed_set"), ("delete", "delete")):
            if key in tools:
                tools[key].set_why(self._gce_tool_why(tool))
        if "insert" in tools and hasattr(tools["insert"], "set_note"):
            eng = self._gce_engine()
            fl = self.gce_flavor_name()
            off = [lab for k, lab, _f in eng.INSERT_FORMS
                   if k not in self._GCE_NOT_OFFERED
                   and not eng.form_available(k, fl)[0]]
            tools["insert"].set_note(
                "Greyed in this flavor: " + ", ".join(off).lower()
                + ". They need your own flavor; Help explains how."
                if off else None)
        if "move" in tools:
            ok = [op for op in self._gce_move_ops
                  if op is not None and not self._gce_tool_why(op)]
            tools["move"].set_why(None if ok else self._gce_tool_why("offset"))
        if "translate" in tools:
            tools["translate"].set_why(self._gce_tool_why("offset"))
        getattr(self, "_gce_design_refresh", lambda: None)()

    def _gce_insert_menu(self):
        """Insert's menu, built on every post: the commands the text's
        flavor can write, your own lines, the saved edits and the full
        form."""
        eng = self._gce_engine()
        menu = getattr(self, "_gce_ins_menu", None)
        if menu is None:
            menu = self._gce_ins_menu = tk.Menu(self.root, tearoff=False)
            self._gce_lib_menu = tk.Menu(menu, tearoff=False)
            menu.bind("<<MenuSelect>>", self._gce_insert_menu_hover)
        menu.delete(0, "end")
        fl = self.gce_flavor_name()
        # a greyed command's reason, said in the status line on hover (a
        # menu item has no tooltip); Help has the longer how-to
        self._gce_ins_why = why_of = {}
        # no comment lines offered: exported g-code stays comment-free; no
        # speed line either -- the Speed tool writes F onto the moves
        for k, lab, _f in eng.INSERT_FORMS:
            if k in self._GCE_NOT_OFFERED:
                continue
            ok, why = eng.form_available(k, fl)
            if not ok:
                lab += " (your own flavor)"
                why_of[lab] = why + " Help explains how."
            menu.add_command(label=lab, state="normal" if ok else "disabled",
                             command=lambda k=k: self._gce_tool_insert(k))
        menu.add_separator()
        menu.add_command(label="Your own lines…",
                         command=lambda: self._gce_tool_insert(None))
        lib = self._gce_library()
        mine = [r for r in lib if not r.builtin]
        sub = self._gce_lib_menu
        sub.delete(0, "end")
        for rec in mine:
            sub.add_command(label=rec.name, command=lambda rec=rec:
                            self._gce_use_recipe(rec))
        menu.add_cascade(label="My library", menu=self._gce_lib_menu,
                         state="normal" if mine else "disabled")
        menu.add_separator()
        menu.add_command(label="Full edit form…", command=self._gce_new_edit)
        return menu

    def _gce_insert_menu_hover(self, e):
        menu = e.widget
        try:
            i = menu.index("active")
            lab = menu.entrycget(i, "label") if i is not None else ""
        except tk.TclError:
            return
        why = getattr(self, "_gce_ins_why", {}).get(lab)
        if why:
            self._gce_status(why)

    def _gce_tool_delete(self):
        """Delete: the selected lines go from the text, recorded as edits
        like any typing."""
        sel = getattr(self, "_gce_sel", None)
        why = self._gce_tool_why("delete")
        if why:
            self._gce_status(why)
            return
        info = self._gce_tree_info.get(sel[1]) or {} if sel[0] == "node" else {}
        if info.get("kind") == "piece":
            pieces = self._gce_model_now().pieces
            index = next(i for i, p in enumerate(pieces) if p.id == info["pid"])
            self._gce_remove_piece(index)
            self._gce_sel = None
            self._gce_sel_show()
            self._gce_status("Deleted from this g-code. Undo brings it back.")
            return
        if sel[0] == "node" and getattr(
                self, "_gce_design_can_delete", lambda _i: False)(sel[1]):
            self._gce_design_delete(sel[1])
            self._gce_sel = None
            self._gce_sel_show()
            self._gce_status("Deleted from the design. Undo brings it back.")
            return
        edits = [t[1] for t in self._gce_targets() if t[0] == "edit"]
        fs = getattr(self, "_gce_fs", None)
        if fs is not None and edits == [fs["cid"]]:
            # the edit open in the form: one step, as its own Delete is
            self._gce_edit_finish(keep=True, remove=True)
            self._gce_sel = None
            self._gce_sel_show()
            self._gce_status("Deleted from this g-code. Undo brings it back.")
            return
        if edits:
            # the selected edits out, their operations with them: one step
            m = self._gce_model_now()
            self._gce_edit_finish(keep=True)
            before = self._gce_snap()
            for cid in edits:
                m.remove(cid)
            self._gce_sel = None
            self._gce_rerender(before)
            self._gce_sel_show()
            self._gce_status("Deleted from this g-code. Undo brings it back.")
            return
        if sel[0] == "text":
            a, b = sel[1], sel[2]
        else:
            info = self._gce_tree_info.get(sel[1]) or {}
            a, b = info.get("first"), info.get("last")
            if not a or info.get("kind") == "file":
                self._gce_need_selection("lines or a layer")
                return
        if b - a + 1 > self._GCE_DELETE_MAX:
            self._gce_status(f"That is {b - a + 1} lines; delete at most "
                             f"{self._GCE_DELETE_MAX} at a time.")
            return
        self._gce_edit_finish(keep=True)
        self.gce.replace_range(f"{a}.0", f"{b + 1}.0", "")
        self._gce_record_edits()
        self._gce_sel = None
        self._gce_sel_show()
        self._gce_render_changes()
        self._gce_status("Deleted from this g-code. Undo brings it back.")

    _GCE_DELETE_MAX = 2000

    def _gce_key_delete(self, e):
        """The Delete key anywhere on the page -- the picture, the ribbon,
        empty space -- is the ribbon's Delete on the selection, unless it is
        typing into something (the g-code text and the boxes keep their
        own). The tree takes its own key first (`_gce_tree_key`)."""
        if not self._gce_page_shown():
            return None
        try:
            w = e.widget
            if w.winfo_class() in self._GCE_TYPING + ("Treeview",)                     or w.winfo_toplevel() is not self.root:
                return None     # another list (Presets, My library) is its own
            form = getattr(self, "_gce_form", None)
            if form is not None and str(w).startswith(str(form)):
                return None     # a key in the floating box is the box's
        except (tk.TclError, AttributeError, KeyError):
            return None
        if not getattr(self, "_gce_sel", None):
            return None
        self._gce_tool_delete()
        return "break"

    def _gce_key_delete_rows(self, iid):
        """Delete on the tree: the rows selected there become the selection
        (the focus row alone when it is not one of them), then the ribbon's
        Delete acts, refusals and all."""
        tree = self._gce_tree
        info = self._gce_tree_info.get(iid) or {}
        sel = tuple(tree.selection())
        if info.get("kind") == "auto" and (not sel or sel == (iid,)):
            self._gce_status("An automatic script runs on every print. "
                             "Stop it in My library (Automatic) or on "
                             "the Printer & collector page.")
            return
        if iid and iid not in sel:
            tree.selection_set(iid)
        self._gce_select_rows()
        if not self._gce_tree_targets():
            self._gce_sel = None    # only rows nothing can delete
        self._gce_tool_delete()

    def _gce_need_selection(self, what):
        self._gce_status(f"Select {what} first: in the browser, in the "
                         "picture, or lines in the g-code (G).")

    def _gce_tool_insert(self, kind):
        """Insert › kind: the command, after the selection, opened in the
        short form. `kind` None: your own lines (the selected lines' text
        to start from, when lines are selected)."""
        eng = self._gce_engine()
        m = self._gce_model_now()
        if eng is None or m is None:
            return
        anchor = self._gce_sel_anchor()
        if anchor is None:
            self._gce_status(self._gce_tool_why("insert"))
            return
        self._gce_edit_finish(keep=True)
        before = self._gce_snap()
        self.gce.begin_group()
        if kind is None:
            sel = self._gce_sel
            lines = [""]
            if sel and sel[0] == "text":
                r = self._gce_render()
                lines = [r.lines[i - 1].text.rstrip("\r\n")
                         for i in range(sel[1], sel[2] + 1)
                         if 0 < i <= len(r.lines)] or [""]
            c = m.add(eng.Insert(lines=lines, anchor=anchor, flavor=m.flavor,
                                 what="custom"))
        else:
            fields = next(f for k, _l, f in eng.INSERT_FORMS if k == kind)
            try:
                c = m.insert_command(kind, anchor, **{
                    n: d for n, _l, _u, d in fields if d is not None})
            except Exception as exc:  # noqa: BLE001 -- the flavor has none
                self.gce.end_group(keep=False)
                self._gce_status(str(exc))
                return
            c.what = kind
        self._gce_rerender(None)
        self._gce_open_edit(c.id, new=True, before=before, tool=True)

    def _gce_tool_change(self, op):
        """Change selection › op: a change to the selected part's moves,
        opened in the short form."""
        eng = self._gce_engine()
        m = self._gce_model_now()
        if eng is None or m is None:
            return
        why = self._gce_tool_why(op)
        if why:
            self._gce_status(why)
            return
        scope = self._gce_sel_scope()
        self._gce_edit_finish(keep=True)
        before = self._gce_snap()
        self.gce.begin_group()
        if scope.kind == "edit":
            # an operation on an edit, one branch deeper under it
            c = m.add_operation(str(scope.first), op)
        else:
            params = {n: d for n, _l, _u, d in
                      eng.TRANSFORM_FIELDS.get(op, ())}
            c = m.add(eng.Transform(op=op, params=params, scope=scope,
                                    what=op))
        if op == "speed_set":
            # it opens on the speed the selection has now, not a default
            now = self._gce_speed_now(c)
            if now is not None:
                c.params["mm_s"] = now[1] / 60.0
        self._gce_rerender(None)
        self._gce_open_edit(c.id, new=True, before=before, tool=True)

    def _gce_retarget_name(self):
        """The selection, named as Re-target's question names it, or None
        with nothing selected."""
        ts = self._gce_targets() if getattr(self, "_gce_sel", None) else []
        fs = getattr(self, "_gce_fs", None)
        if not ts or (fs is not None and ts == [("edit", fs["cid"])]):
            return None             # the open edit itself: no new place
        if len(ts) > 1:
            return f"the {len(ts)} selected parts"
        return self._gce_target_label(ts[0])

    def _gce_retarget(self):
        """Re-target: name where the open edit would go, and wait for OK."""
        fs = getattr(self, "_gce_fs", None)
        if fs is None:
            return
        name = self._gce_retarget_name()
        if name is None:
            self._gce_need_selection("where it should go")
            return
        vals = fs["vals"]
        if vals["tool_where"].get().endswith(name):
            self._gce_status(f"It is already on {name}. Select another "
                             "place first.")
            return
        vals["rt_ask"].set(f"Move it to {name}?")
        vals["rt_row"].grid()

    def _gce_retarget_ask_sync(self):
        """A selection change while Re-target asks: the question names the
        new selection, so OK always moves it where the question says."""
        fs = getattr(self, "_gce_fs", None)
        if fs is None or "rt_row" not in fs["vals"] \
                or not fs["vals"]["rt_row"].winfo_manager():
            return
        name = self._gce_retarget_name()
        if name is None:
            self._gce_retarget_cancel()
        else:
            fs["vals"]["rt_ask"].set(f"Move it to {name}?")

    def _gce_retarget_cancel(self):
        fs = getattr(self, "_gce_fs", None)
        if fs is not None and "rt_row" in fs["vals"]:
            fs["vals"]["rt_row"].grid_remove()

    def _gce_retarget_ok(self):
        self._gce_retarget_cancel()
        self._gce_retarget_move()

    def _gce_retarget_move(self):
        """The open edit moves to the current selection."""
        info = self._gce_sel_node_info()
        sel = getattr(self, "_gce_sel", None)
        fs = getattr(self, "_gce_fs", None)
        if fs is None:
            return
        if fs["vals"]["type"].get() == self._GCE_CHANGE and not fs["line"]:
            scope = self._gce_sel_scope()
            if scope is None:
                self._gce_status(self._gce_tool_why("offset"))
                return
            if scope.kind in self._GCE_FIXED:
                fs["fixed"] = scope
                self._gce_edit_vis()
                self._gce_edit_flush() if fs["after"] is not None else \
                    self._gce_edit_apply()
                return
            fs["fixed"] = None
            self._gce_form_take_node(info or {"kind": "file"})
            self._gce_edit_vis()
            return
        if sel and sel[0] == "text" and not fs["vals"]["type"].get() \
                == self._GCE_CHANGE:
            m = self._gce_model_now()
            fs["anchor"] = m.anchor_at(sel[2], self._gce_render())
            fs["vals"]["where"].set(self._GCE_SPOT)
            self._gce_edit_flush() if fs["after"] is not None else \
                self._gce_edit_apply()
            return
        if info is None:
            self._gce_need_selection("where it should go")
            return
        self._gce_form_take_node(info)

    def _gce_is_simple(self, cid):
        """Whether an edit opens in the short form: one command or your own
        lines at one part of the print, or a change to one part. Anything
        else (a pattern, a changed line) opens in the full form."""
        eng = self._gce_engine()
        m = self._gce_model_now()
        try:
            c = m.get(cid)
        except (KeyError, AttributeError):
            return False
        if isinstance(c, eng.Insert):
            return c.anchor.unit in ("start", "header", "end", "group",
                                     "layer")
        if isinstance(c, eng.Rule):
            return self._gce_every_n_layers(c) is not None
        if isinstance(c, eng.Transform):
            sc = c.scope
            return sc is None or sc.kind in ("all",) + self._GCE_FIXED or (
                sc.kind in ("layers", "group")
                and (sc.last is None or str(sc.first) == str(sc.last)))
        return False

    def _gce_every_n_layers(self, c):
        """N when `c` is lines written after every Nth layer, from layer N
        to the end -- the pattern the short form's tick box writes -- else
        None."""
        eng = self._gce_engine()
        if not isinstance(c, eng.Rule) or c.where != "after_layer" \
                or c.last not in (None, ""):
            return None
        try:
            n = max(1, int(c.every or 1))
        except (TypeError, ValueError):
            return None
        first = c.first if c.first not in (None, "") else 1
        try:
            return n if int(first) == n else None
        except (TypeError, ValueError):
            return None

    def _gce_rep_switch(self):
        """The Repeat form's pattern changed: the N box shows that
        pattern's own N (what it had, else its default), never the last
        pattern's -- 1 layer is sane, 1 line is not."""
        fs = self._gce_fs
        vals = fs["vals"]
        rl = vals["rwhere"].get()
        prev = fs.get("rw_prev")
        if rl == prev or "rep_by" not in fs:
            return
        fs["rw_prev"] = rl
        unit = self._GCE_REP_UNIT
        fs["loading"] = True        # these sets are not edits of their own
        try:
            if prev in unit:
                fs["rep_by"][unit[prev]] = vals["rep_n"].get()
            if rl in unit:
                vals["rep_n"].set(self._gce_fmt(fs["rep_by"].get(
                    unit[rl], self._GCE_REP_N0[unit[rl]])))
            w = {lab: k for k, lab in self._gce_engine().WHERE_CHOICES}.get(
                rl)
            if w in self._GCE_PATTERN_N0 and not vals["n"].get().strip():
                vals["n"].set(self._gce_fmt(self._GCE_PATTERN_N0[w]))
        finally:
            fs["loading"] = False

    def _gce_repeat_plain(self, p):
        """A Repeat's params as one of its two plain patterns:
        ``(label, N)`` for after layer N, 2N, 3N to the end, or every N
        moving lines; ``(None, 1)`` for anything else (it opens on its own
        place in the list, with its own rows)."""
        try:
            if p.get("where") == "every_n_moves":
                n = float(p.get("n") or 0)
                if n >= 1 and n == int(n):
                    return self._GCE_REP_LINES, int(n)
            elif p.get("where") == "after_layer" \
                    and p.get("last") in (None, ""):
                ev = max(1, int(p.get("every") or 1))
                first = p.get("first")
                if (first in (None, "") and ev == 1) or (
                        first not in (None, "") and int(first) == ev):
                    return self._GCE_REP_LAYERS, ev
        except (TypeError, ValueError):
            pass
        return None, 1

    def _gce_rename(self, cid):
        """Rename: the edit opens in its full form with the name selected,
        ready to type over."""
        self._gce_open_edit(cid, tool=False)
        fs = getattr(self, "_gce_fs", None)
        if fs is None or fs["cid"] != cid:
            return
        e = fs["vals"]["name_row"].field
        e.focus_set()
        e.select_range(0, "end")
        e.icursor("end")

    def _gce_tree_key(self, action):
        """Space / Delete on an edit's row."""
        iid = self._gce_tree.focus()
        info = self._gce_tree_info.get(iid) or {}
        if info.get("kind") == "auto":
            if action == "delete":
                self._gce_status("An automatic script runs on every print. "
                                 "Stop it in My library (Automatic) or on "
                                 "the Printer & collector page.")
            return "break"
        if action == "delete":
            # the ribbon's Delete, on the rows selected: several edits at
            # once as the ribbon takes them, not only the row with the focus
            self._gce_key_delete_rows(iid)
            return "break"
        if info.get("kind") != "edit":
            return None
        cid = info["cid"]
        fs = getattr(self, "_gce_fs", None)
        if action == "delete":
            if fs is not None and fs["cid"] == cid:
                self._gce_edit_finish(keep=True, remove=True)
            else:
                self._gce_delete_id(cid)
        else:
            c = self._gce_model_now().get(cid)
            self._gce_toggle_id(cid, not c.on)
        return "break"

    # -- My library: the list view ------------------------------------------ #
    def _gce_recipes_path(self):
        """My library: one JSON beside the user's presets (never in the
        install folder, which a frozen build cannot write)."""
        return os.path.join(USER_DATA_DIR, _RECIPES_FILE)

    def _gce_library(self):
        """My library, read once and kept (a fresh copy each call)."""
        eng = self._gce_engine()
        if eng is None:
            return []
        lib = getattr(self, "_gce_lib_cache", None)
        if lib is None:
            lib = self._gce_lib_cache = eng.load_library(
                self._gce_recipes_path())
        return copy.deepcopy(lib)

    def _gce_migrate_custom_line(self, model):
        """Move a legacy numbered line to its nearest structural boundary."""
        spec = self._custom_gcode_spec()
        if spec is None or model is None:
            return
        if spec[1] == 0:
            self._machine_migrating = True
            try:
                self.machine_gcode_start.set("\n".join(
                    x for x in (spec[0], self.machine_gcode_start.get()) if x))
                self.custom_gcode.set("")
            finally:
                self._machine_migrating = False
            self._migrated_custom_note = "Old line 0 moved to the start block."
            return
        line_map = model._map()
        candidates = [(0, "start", None), (line_map.n_lines, "end", None)]
        counts = {"layer": 0, "group": 0}
        for mark in line_map.marks:
            kind = mark.get("kind")
            if kind in counts:
                counts[kind] += 1
                candidates.append((int(mark["line"]), "before_" + kind,
                                   counts[kind]))
        position, where, number = min(candidates, key=lambda c: abs(c[0] - spec[1]))
        eng = self._gce_engine()
        rule = eng.Rule(name="Migrated printer line", text=spec[0], where=where,
                        first=number, last=number,
                        flavor=self._machine_flavor_name())
        recipe = eng.Recipe("Migrated printer line", [rule])
        self._machine_migrating = True
        try:
            self._machine_add_recipe(recipe)
            self.custom_gcode.set("")
        finally:
            self._machine_migrating = False
        self._migrated_custom_note = ("Old line %d moved to %s%s (line %d)." %
                                      (spec[1], where, " %s" % number if number else "",
                                       position))

    def _gce_write_library(self, lib):
        eng = self._gce_engine()
        path = self._gce_recipes_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            eng.save_library(path, lib)
        except OSError as exc:
            self._gce_notice_show(f"My library could not be saved: {exc}",
                                  [("OK", self._gce_notice_hide)])
        self._gce_lib_cache = copy.deepcopy(lib)
        self._gce_render_library()
        host = getattr(self, "_gce_lib_host", None)
        if host is not None and getattr(self, "_gce_form", None) is host:
            body = next((w for w in host.winfo_children()
                         if int(w.grid_info().get("row", -1)) == 2), None)
            if body is not None:
                self._gce_library_fill(body)
        getattr(self, "_scripts_changed", lambda: None)()

    def _gce_render_library(self):
        """The list view: this file's unsaved edits (one row each, KEPT
        across refreshes and only re-labelled, so typing never makes it
        flash), the saved edits, the examples."""
        eng = self._gce_engine()
        box = getattr(self, "_gce_unsaved_box", None)
        if box is None or eng is None:
            return
        m = self._gce_model_now()
        rows = getattr(self, "_gce_lib_rows", None)
        if rows is None:
            rows = self._gce_lib_rows = {}
        edits = [c for c in (m.changes if m is not None else ())
                 if not c.saved]
        seen = set()
        for i, c in enumerate(edits):
            seen.add(c.id)
            hit = rows.get(c.id)
            if hit is None or not hit["frame"].winfo_exists():
                hit = rows[c.id] = self._gce_lib_row(box, c.id)
            name = self._gce_edit_name(c) + ("" if c.on else " (off)")
            if hit["name"].cget("text") != name:
                hit["name"].configure(text=name)
            where = ("typed, not kept yet · "
                     if c.typed and not self._gce_chip_fixed(c) else "") \
                + self._gce_edit_where(c)
            if hit["where"].cget("text") != where:
                hit["where"].configure(text=where)
            if hit["var"].get() != bool(c.on):
                hit["var"].set(bool(c.on))
            hit["save"].pack_forget() if isinstance(c, eng.LineEdit) \
                else hit["save"].pack(side="left", padx=(4, 0))
            info = hit["frame"].grid_info()
            if not info or int(info.get("row", -1)) != i:
                hit["frame"].grid(row=i, column=0, sticky="ew", pady=1)
        for cid in [k for k in rows if k not in seen]:
            if rows[cid]["frame"].winfo_exists():
                rows[cid]["frame"].destroy()
            del rows[cid]
        hint = getattr(self, "_gce_lib_empty", None)
        if hint is None:
            hint = self._gce_lib_empty = gui_form.mark(ttk.Label(
                box, style="Hint.TLabel", wraplength=240, justify="left",
                text="Nothing yet. Type in the text, or press + New edit."),
                "live")
            gui_revert.skip(hint)
        hint.grid(row=0, column=0, sticky="w") if not edits \
            else hint.grid_remove()
        self._gce_render_saved()

    def _gce_library_toggle(self, show=None):
        """The ribbon's My library: the saved edits in the floating box --
        use one here, open one to change it, take one out. Pressed again,
        or Esc, it closes."""
        shown = getattr(self, "_gce_lib_host", None) is not None \
            and getattr(self, "_gce_form", None) is self._gce_lib_host
        show = (not shown) if show is None else bool(show)
        if not show:
            if shown:
                self._gce_host_close()
            self._gce_lib_host = None
            self._gce_library_btn_on()
            return
        host, body = self._gce_host_open("My library")
        self._gce_lib_host = host
        self._gce_library_fill(body)
        btns = ttk.Frame(host)
        btns.grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Button(btns, text="Close", width=7,
                   command=lambda: self._gce_library_toggle(False)).pack(
            side="left")
        self._gce_host_keys(host, lambda: self._gce_library_toggle(False),
                            lambda: self._gce_library_toggle(False))
        self._gce_library_btn_on()

    def _gce_library_btn_on(self):
        b = (getattr(self, "_gce_tools", None) or {}).get("library")
        if b is not None:
            b.set_on(getattr(self, "_gce_lib_host", None) is not None
                     and getattr(self, "_gce_form", None)
                     is self._gce_lib_host)

    def _gce_library_fill(self, body):
        """The library panel's rows, fresh from My library."""
        for w in body.winfo_children():
            w.destroy()
        body.columnconfigure(0, weight=1)
        m = self._gce_model_now()
        lib = self._gce_library()
        used = {}
        for c in (m.changes if m is not None else ()):
            if c.saved:
                used[c.saved] = used.get(c.saved, 0) + 1
        mine = [r for r in lib if not r.builtin]
        top = ttk.Frame(body)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        new = ttk.Button(top, text="New script\u2026", width=12,
                         command=lambda: self.open_script_library(new=True))
        new.pack(side="left")
        add_tip(new, "Write a script from scratch in its own window. It is "
                     "saved to My library and is not put into this g-code "
                     "until you press Use here.")
        allb = ttk.Button(top, text="Open window", width=12,
                          command=self.open_script_library)
        allb.pack(side="left", padx=(4, 0))
        add_tip(allb, "My library in its own window, with room to read and "
                      "change the scripts.")
        link = ttk.Button(body, text="Header, end and automatic commands\u2026",
                          command=self._open_machine_gcode_card)
        link.grid(row=3, column=0, sticky="w", pady=(6, 0))
        add_tip(link, "The Printer & collector page's G-code card: what the header and "
                      "end of every print say, and the scripts that run on "
                      "every print.")
        gui_form.mark(ttk.Label(body, text="Saved", font="MewSection"),
                      "label").grid(row=1, column=0, sticky="w", pady=(0, 2))
        box = ttk.Frame(body)
        box.grid(row=2, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)
        if not mine:
            gui_form.mark(ttk.Label(
                box, text="Nothing saved yet. Open an edit (double-click it "
                          "in the tree) and press Save to library.",
                style="Hint.TLabel", wraplength=260, justify="left"),
                "live").grid(row=0, column=0, sticky="w")
        for i, rec in enumerate(mine):
            self._gce_recipe_row(box, i, rec, used.get(rec.name, 0))
        gui_revert.skip(body)

    def _gce_lib_row(self, box, cid):
        f = ttk.Frame(box)
        f.columnconfigure(1, weight=1)
        var = tk.BooleanVar(value=True)
        cb = ttk.Checkbutton(f, variable=var, command=lambda: self._gce_toggle_id(
            cid, var.get()))
        cb.grid(row=0, column=0, rowspan=2, sticky="nw")
        add_tip(cb, "Untick to take this edit out of the g-code for now; "
                    "tick it to put it back.")
        name = ttk.Label(f, text="", cursor="hand2", wraplength=150,
                         justify="left")
        name.grid(row=0, column=1, sticky="w")
        where = gui_form.mark(ttk.Label(f, text="", style="Hint.TLabel",
                                        cursor="hand2", wraplength=150,
                                        justify="left"), "live")
        where.grid(row=1, column=1, sticky="w")
        for w in (name, where):
            w.bind("<Button-1>", lambda _e: self._gce_open_edit(cid))
        add_tip(name, "Open it: rename it, change it, move it or save it.")
        acts = ttk.Frame(f)
        acts.grid(row=2, column=1, sticky="w", pady=(2, 0))
        edit = ttk.Button(acts, text="Edit", width=5,
                          command=lambda: self._gce_open_edit(cid))
        edit.pack(side="left")
        add_tip(edit, "Open it: rename it, change it, move it or save it.")
        save = ttk.Button(acts, text="Save", width=5,
                          command=lambda: self._gce_save_id(cid))
        add_tip(save, "Keep it in My library, to use on any other g-code.")
        delete = ttk.Button(acts, text="Delete", width=7,
                            command=lambda: self._gce_delete_id(cid))
        delete.pack(side="right", padx=(4, 0))
        add_tip(delete, "Take it out of this g-code (Undo brings it back).")
        gui_revert.skip(f)
        return dict(frame=f, var=var, name=name, where=where, save=save)

    def _gce_render_saved(self):
        eng = self._gce_engine()
        box = self._gce_saved_box
        m = self._gce_model_now()
        lib = self._gce_library()
        mine = [r for r in lib if not r.builtin]
        used = {}
        for c in (m.changes if m is not None else ()):
            if c.saved:
                used[c.saved] = used.get(c.saved, 0) + 1
        sig = [(r.name, bool(r.automatic), used.get(r.name, 0))
               for r in mine]
        if sig != getattr(self, "_gce_saved_sig", None):
            self._gce_saved_sig = sig
            for w in box.winfo_children():
                w.destroy()
            if not mine:
                gui_form.mark(ttk.Label(
                    box, style="Hint.TLabel", wraplength=240,
                    justify="left",
                    text="Nothing saved yet. Open an edit and press Save, "
                         "or press Save on its row."), "live").grid(
                    row=0, column=0, sticky="w")
            for i, rec in enumerate(mine):
                self._gce_recipe_row(box, i, rec, used.get(rec.name, 0))
        if not getattr(self, "_gce_examples_built", False):
            self._gce_examples_built = True
            for i, rec in enumerate(r for r in lib if r.builtin):
                self._gce_recipe_row(self._gce_examples_box, i, rec, 0)

    def _gce_recipe_row(self, box, i, rec, used):
        f = ttk.Frame(box)
        f.grid(row=i, column=0, sticky="ew", pady=1)
        f.columnconfigure(0, weight=1)
        name = ttk.Label(f, text=rec.name, wraplength=150, justify="left",
                         cursor="hand2")
        name.grid(row=0, column=0, sticky="w")
        bits = []
        if used:
            bits.append(f"used here {used}x" if used > 1 else "used here")
        if rec.automatic and not rec.builtin:
            bits.append("runs on every print")
        if bits:
            gui_form.mark(ttk.Label(f, text=" · ".join(bits),
                                    wraplength=150, justify="left",
                                    style="Hint.TLabel"), "live").grid(
                row=1, column=0, sticky="w")
        acts = ttk.Frame(f)
        acts.grid(row=2, column=0, sticky="w", pady=(2, 0))
        b = ttk.Button(acts, text="Use here", width=9,
                       command=lambda: self._gce_use_recipe(rec))
        b.pack(side="left")
        add_tip(b, "Add it to this g-code. It opens so you can choose "
                   "where it goes.")
        gui_revert.skip(f)
        if rec.builtin:
            add_tip(name, rec.description)
        else:
            e = ttk.Button(acts, text="Edit", width=5,
                           command=lambda: self.open_script_library(
                               select=rec.name))
            e.pack(side="left", padx=(4, 0))
            name.bind("<Button-1>", lambda _e: self.open_script_library(
                select=rec.name))
            auto = ttk.Button(
                acts, width=9, text="Stop auto" if rec.automatic else "Automatic",
                command=lambda: self._automatic_set(rec.name,
                                                    not rec.automatic))
            auto.pack(side="left", padx=(4, 0))
            add_tip(auto, "Stop running this script on every print."
                    if rec.automatic else
                    "Run this script on every print, at the place it says. "
                    "It shows in the tree as a locked row.")
            rm = ttk.Button(acts, text="Remove", width=7)
            rm.configure(command=lambda: self._gce_remove_recipe(rec, rm))
            rm.pack(side="left", padx=(4, 0))
            add_tip(rm, "Take it out of My library. Where this g-code uses it, "
                        "it stays, under In this file, not saved.")
            for w in (name, e):
                add_tip(w, "Open it in the My library window: change it, "
                           "rename it, or delete it.")

    def _gce_delete_id(self, cid):
        """A row's Delete: the edit out of this g-code, one undo step."""
        m = self._gce_model_now()
        if m is None:
            return
        fs = getattr(self, "_gce_fs", None)
        if fs is not None and fs["cid"] == cid:
            self._gce_edit_finish(keep=True, remove=True)
            return
        before = self._gce_snap()
        try:
            m.remove(cid)
        except KeyError:
            return
        self._gce_rerender(before)
        self._gce_status("Deleted from this g-code. Undo brings it back.")

    def _gce_remove_recipe(self, rec, btn):
        """A saved row's Remove (press twice): out of My library. Its uses
        in this g-code stay, as unsaved edits."""
        if btn.cget("text") != "Sure?":
            btn.configure(text="Sure?")
            return
        lib = [r for r in self._gce_library()
               if r.builtin or r.name != rec.name]
        m = self._gce_model_now()
        for c in (m.changes if m is not None else ()):
            if c.saved == rec.name:
                c.saved = ""
        self._gce_write_library(lib)
        self._gce_edit_lib_vis()
        self._gce_render_changes()
        self._gce_sync_persist()
        self._gce_status(f"“{rec.name}” is out of My library.")

    def _gce_toggle_id(self, cid, on):
        m = self._gce_model_now()
        if m is None:
            return
        before = self._gce_snap()
        try:
            m.toggle(cid, on)
        except KeyError:
            return
        self._gce_rerender(before)

    def _gce_toggle_chip(self, c, var):
        self._gce_toggle_id(c.id, var.get())

    def _gce_save_id(self, cid):
        """Save an edit to My library under its name (an edit already saved
        under that name is replaced: the library keeps one of each)."""
        eng = self._gce_engine()
        m = self._gce_model_now()
        if m is None:
            return
        try:
            c = m.get(cid)
        except KeyError:
            return
        if isinstance(c, eng.LineEdit):
            return
        name = self._gce_edit_name(c).strip() or "My edit"
        lib = self._gce_library()
        auto = any(r.use_on_new for r in lib
                   if not r.builtin and r.name in (name, c.saved))
        rec = eng.recipe_from_chips(m, name, [cid], use_on_new=auto)
        lib = [r for r in lib if r.builtin or r.name not in (name, c.saved)]
        lib.append(rec)
        if c.saved and c.saved != name:     # renamed: its other uses follow
            for o in m.changes:
                if o.saved == c.saved:
                    o.saved = name
        c.saved, c.typed = name, False
        if not c.label:
            c.label = name
        self._gce_write_library(lib)
        self._gce_status(f"“{name}” is in My library.")
        self._gce_render_changes()
        self._gce_sync_persist()

    def _gce_use_recipe(self, rec):
        """A library edit into this g-code, opened in the form so where it
        goes can be chosen; Cancel takes it back out."""
        eng = self._gce_engine()
        m = self._gce_model_now()
        if eng is None or m is None:
            return
        self._gce_edit_finish(keep=True)
        before = self._gce_snap()
        self.gce.begin_group()
        ids = eng.apply_recipe(m, rec)
        if not ids:
            self.gce.end_group(keep=False)
            return
        self._gce_rerender(None)
        self._gce_open_edit(ids[0], new=True, before=before)
        sel = self._gce_sel_node_info()
        if sel is not None and not isinstance(m.get(ids[0]), eng.Rule):
            self._gce_form_take_node(sel)
        c = m.get(ids[0])
        if isinstance(c, eng.Rule) and not (c.text or "").strip() \
                and not c.command:
            self._gce_fs["err"].set("Write your printer's command in the "
                                    "box; this example cannot know it.")

    def _gce_edit_recipe(self, rec):
        """A saved edit's Edit: the same form as every other edit. Used in
        this file, its first use opens; otherwise it is added here first
        (Cancel takes it back out) so it can be seen and placed."""
        m = self._gce_model_now()
        if m is None:
            return
        c = next((c for c in m.changes if c.saved == rec.name), None)
        if c is not None:
            self._gce_open_edit(c.id)
            if self._gce_fs is not None:
                self._gce_fs["library_name"] = rec.name
            return
        self._gce_use_recipe(rec)
        fs = getattr(self, "_gce_fs", None)
        if fs is not None:
            fs["library_name"] = rec.name
            fs["vals"]["lib_note"].set("Not in this file yet: Done adds it "
                                       "here, Cancel leaves the file as it "
                                       "was.")

    def _gce_edit_lib_set(self, *, use_on_new=None, delete=False):
        """The open edit's library copy: tick "every new print" (saving it
        first if it is not saved), or delete it from My library (the edit
        stays in this file)."""
        fs = getattr(self, "_gce_fs", None)
        m = self._gce_model_now()
        if fs is None or m is None:
            return
        c = m.get(fs["cid"])
        if use_on_new is not None and not c.saved:
            self._gce_edit_save()
            c = m.get(fs["cid"])
            if not c.saved:
                fs["vals"]["auto"].set(False)
                return
        lib = self._gce_library()
        if delete:
            lib = [r for r in lib if r.builtin or r.name != c.saved]
            for o in m.changes:
                if o.saved == c.saved:
                    o.saved = ""
            self._gce_status("Out of My library. The edit is still in this "
                             "g-code.")
        else:
            for r in lib:
                if not r.builtin and r.name == c.saved:
                    r.use_on_new = bool(use_on_new)
        self._gce_write_library(lib)
        self._gce_edit_lib_vis()
        self._gce_render_changes()
        self._gce_sync_persist()

    def _gce_edit_lib_vis(self):
        """The form's library row, to match the edit's library copy."""
        fs = getattr(self, "_gce_fs", None)
        m = self._gce_model_now()
        if fs is None or m is None:
            return
        try:
            c = m.get(fs["cid"])
        except KeyError:
            return
        vals = fs["vals"]
        rec = next((r for r in self._gce_library()
                    if not r.builtin and c.saved and r.name == c.saved), None)
        fs["save_btn"].configure(text="Update library copy" if rec
                                 else "Save to library")
        auto = bool(rec and rec.use_on_new)
        if vals["auto"].get() != auto:
            vals["auto"].set(auto)
        vals["del_armed"] = False
        vals["del_btn"].configure(text="Remove from library")
        vals["del_btn"].pack(side="left", padx=(6, 0)) if rec \
            else vals["del_btn"].pack_forget()

    def _gce_edit_lib_delete(self):
        vals = self._gce_fs["vals"]
        if not vals.get("del_armed"):
            vals["del_armed"] = True
            vals["del_btn"].configure(text="Sure? Remove it")
            vals["lib_note"].set("Press again to take it out of My library. "
                                 "Edits already in a g-code stay there.")
            return
        vals["lib_note"].set("")
        self._gce_edit_lib_set(delete=True)

    # -- the library's other view: a panel in its place --------------------- #
    def _gce_host_open(self, title):
        """Show a panel in place of the library list: ``(host, body)``.
        Anything open there is finished first (an edit is kept), and the
        Parameters box closes: it sits in the same place, and left open it
        showed through around the panel (`_gcp_show` does the converse)."""
        self._gce_edit_finish(keep=True)
        self._gce_host_close()
        shown = getattr(self, "_gce_params_shown", None)
        if shown is not None and shown():
            self._gce_params_close()
        box = getattr(self, "_gce_formbox", None)
        parent = box.body if box is not None else self._gce_changes.master
        host = ttk.Frame(parent)
        # a box the user made taller than the panel gives the room to the
        # panel's body (row 2): the Presets list grows, the buttons stay
        # under it (`_gce_layout`)
        host.grid(row=1, column=0, sticky="nsew")
        host.columnconfigure(0, weight=1)
        host.rowconfigure(2, weight=1)
        self._gce_form = host
        self._gce_form_title = title
        if box is not None:
            box._canvas.yview_moveto(0)
            self._gce_layout_soon()
        ttk.Label(host, text=title, font="MewSection").grid(
            row=0, column=0, sticky="w")
        body = ttk.Frame(host)
        body.grid(row=2, column=0, sticky="nsew", pady=(4, 0))
        return host, body

    def _gce_host_keys(self, host, ok, esc):
        """Return runs `ok` in a field, Esc runs `esc` -- and stops there,
        never reaching the app-wide Esc. A form edits no preset field, so
        it never gets revert arrows (`gui_revert.skip`)."""
        gui_revert.skip(host)
        def _esc(_e=None):
            esc()
            return "break"

        def _ok(_e=None):
            ok()
            return "break"
        stack = [host]
        while stack:
            w = stack.pop()
            w.bind("<Escape>", _esc, add="+")
            if isinstance(w, (ttk.Entry, ttk.Combobox)):
                w.bind("<Return>", _ok, add="+")
            stack.extend(w.winfo_children())

    def _gce_host_close(self):
        host = getattr(self, "_gce_form", None)
        if host is not None:
            try:
                host.destroy()
            except tk.TclError:
                pass
            self._gce_form = None
            self._gce_d_form_open = None
        if getattr(self, "_gce_lib_host", None) is host:
            self._gce_lib_host = None
        self._gce_library_btn_on()
        self._gce_changes.grid()
        if getattr(self, "_gce_formbox", None) is not None:
            self._gce_layout_soon()

    _gce_form_close = _gce_host_close

    @staticmethod
    def _gce_walk(w):
        out = []
        stack = [w]
        while stack:
            x = stack.pop(0)
            out.append(x)
            stack.extend(x.winfo_children())
        return out

    @staticmethod
    def _gce_num(vals, name, label, required=True, cast=float):
        """vals[name] as a number, or raise ValueError with a sentence."""
        raw = str(vals[name].get()).strip()
        if not raw:
            if required:
                raise ValueError(f"{label} is empty.")
            return None
        try:
            return cast(raw)
        except ValueError:
            raise ValueError(f"{label} must be a number, not “{raw}”.")

    def _gce_field_values(self, vals, fields):
        eng = self._gce_engine()
        labels = {}             # a choice field: its label -> its value
        for ch in eng.TRANSFORM_CHOICES.values():
            for fname, opts in ch.items():
                labels.setdefault(fname, {}).update(
                    {lab: v for v, lab in opts})
        out = {}
        for name, label, unit, default in fields:
            raw = str(vals[name].get()).strip()
            if name in labels and raw in labels[name]:
                out[name] = labels[name][raw]
            elif unit or isinstance(default, (int, float)):
                if raw == "" and default is None:
                    continue
                cast = int if isinstance(default, int) \
                    and not isinstance(default, bool) else float
                out[name] = self._gce_num(vals, name, label, cast=cast)
            else:
                out[name] = raw
        return out

    def _gce_form_open(self, title, build, on_ok, ok_text="Add", extra=()):
        """A plain form in the library's place (a stitched piece).
        `build(F, vals)` fills a `gui_form.Form`; `on_ok(vals)` returns a
        sentence to show, or None to close. `extra` is (text, command)
        buttons after Cancel."""
        host, body = self._gce_host_open(title)
        F = gui_form.Form(body)
        vals = {}
        build(F, vals)
        err = tk.StringVar(value="")
        gui_form.mark(ttk.Label(host, textvariable=err, wraplength=240,
                                justify="left", foreground="#c0392b"),
                      "live").grid(row=3, column=0, sticky="w", pady=(4, 0))
        btns = ttk.Frame(host)
        btns.grid(row=1, column=0, sticky="w", pady=(4, 0))

        def ok():
            msg = on_ok(vals)
            if msg:
                err.set(msg)
            else:
                self._gce_host_close()
        ttk.Button(btns, text=ok_text, style="Primary.TButton",
                   command=ok).pack(side="left")
        ttk.Button(btns, text="Cancel", command=self._gce_host_close).pack(
            side="left", padx=(6, 0))
        if extra:
            more = ttk.Frame(host)
            more.grid(row=4, column=0, sticky="w", pady=(4, 0))
            for i, (text, cmd) in enumerate(extra):
                ttk.Button(more, text=text, command=cmd).pack(
                    side="left", padx=(6 if i else 0, 0))
        self._gce_form_err = err
        self._gce_host_keys(host, ok, self._gce_host_close)
        first = next((w for w in self._gce_walk(body)
                      if isinstance(w, (ttk.Entry, ttk.Combobox, tk.Text))),
                     None)
        if first is not None:
            first.focus_set()
        return host

    # -- the edit form: one for every edit, typed or made here -------------- #
    def _gce_new_edit(self):
        """+ New edit: a dwell after the caret's line (or your own g-code
        from the selected lines), applied at once and opened in the form."""
        eng = self._gce_engine()
        m = self._gce_model_now()
        if eng is None or m is None:
            return
        self._gce_edit_finish(keep=True)
        before = self._gce_snap()
        self.gce.begin_group()
        r = self._gce_render()
        if self.gce.text.tag_ranges("sel"):
            a, b = self.gce._selected_lines()
            lines = [r.lines[i - 1].text.rstrip("\r\n")
                     for i in range(a, b + 1) if 0 < i <= len(r.lines)]
            c = m.add(eng.Insert(lines=lines or [""],
                                 anchor=m.anchor_at(b, r), flavor=m.flavor,
                                 what="custom"))
        else:
            line = self.gce.caret()[0] if self.gce.get_text() else 0
            c = m.insert_command("dwell", m.anchor_at(line, r), seconds=2.0)
            c.what = "dwell"
        self._gce_rerender(None)
        self._gce_open_edit(c.id, new=True, before=before)

    def _gce_open_edit(self, cid, new=False, before=None, tool=False):
        """Open an edit in the form, in the library's place. While it is
        open every change applies at once, to the text and the picture;
        Done keeps it all as ONE undo step, Cancel puts everything back."""
        eng = self._gce_engine()
        m = self._gce_model_now()
        if eng is None or m is None:
            return
        fs = getattr(self, "_gce_fs", None)
        if fs is not None and fs["cid"] == cid:
            self._gce_focus_edit(cid, see=True)
            return
        if before is None:
            self._gce_edit_finish(keep=True)
            before = self._gce_snap()
            self.gce.begin_group()
        try:
            c = m.get(cid)
        except KeyError:
            self.gce.end_group(keep=False)
            return
        self._gce_edit_form(c, before, new, tool)
        self._gce_edit_after()
        tree = getattr(self, "_gce_tree", None)
        if tree is not None and tree.exists("e:" + cid):
            tree.see("e:" + cid)
            tree.selection_set("e:" + cid)
        # the open edit is the selection, as its lit row says: the next
        # tool (Repeat, Scale, Delete) works on it, not on the part that
        # was selected before it was made
        self._gce_sel = ("edit", cid)
        self._gce_sel_show()

    def _gce_edit_form(self, c, before, new, tool=False):
        eng = self._gce_engine()
        m = self._gce_model_now()
        is_line = isinstance(c, eng.LineEdit)
        forms = list(eng.INSERT_FORMS)
        label_of = {k: lab for k, lab, _f in forms}
        kind_of = {lab: k for k, lab, _f in forms}
        # no comment lines offered: exported g-code stays comment-free
        add_labels = [self._GCE_CUSTOM] + [lab for k, lab, _f in forms
                                           if k not in self._GCE_NOT_OFFERED]
        op_of = {lab: op for op, lab in eng.TRANSFORM_KINDS}
        oplabel = dict(eng.TRANSFORM_KINDS)
        ch_labels = [lab for _op, lab in eng.TRANSFORM_KINDS]
        # one place only; an edit that already is a pattern keeps its own
        where_labels = [self._GCE_SPOT] + [
            lab for w, lab in eng.WHERE_CHOICES
            if w in self._GCE_ONE_PLACE
            or (isinstance(c, eng.Rule) and w == c.where)]
        where_key = {lab: w for w, lab in eng.WHERE_CHOICES}
        where_lab = dict(eng.WHERE_CHOICES)
        scope_key = {lab: k for k, lab in self._GCE_SCOPES}
        scope_lab = dict(self._GCE_SCOPES)

        host, body = self._gce_host_open("New edit" if new else "Edit")
        vals = {}
        fs = dict(cid=c.id, before=before, new=new, line=is_line,
                  typed=bool(c.typed), after=None, vals=vals, loading=True,
                  box_text="", anchor=None, seen=None, start="",
                  name_auto=not c.label, naming=False,
                  tool=bool(tool) and not is_line,
                  # a typed insert: its name and its g-code; its place is
                  # its spot in the tree
                  plain=bool(c.typed) and isinstance(c, eng.Insert))
        F = gui_form.Form(body)
        # the short form (a ribbon tool, or a simple edit opened from the
        # tree): where it applies, Re-target, and only the tool's fields
        tw = ttk.Frame(F.frame)
        tw.grid(row=F._next(None), column=0, columnspan=3, sticky="ew",
                pady=(0, 4))
        tw.columnconfigure(0, weight=1)
        vals["tool_where"] = tk.StringVar(value="")
        gui_form.mark(ttk.Label(tw, textvariable=vals["tool_where"],
                                wraplength=200, justify="left"),
                      "live").grid(row=0, column=0, sticky="w")
        rt = ttk.Button(tw, text="Re-target", command=self._gce_retarget)
        rt.grid(row=0, column=1, sticky="e", padx=(6, 0))
        add_tip(rt, "Move this edit to what is selected now: select "
                    "another layer (in the tree, the picture or the "
                    "g-code), then press Re-target. It names the new "
                    "place and waits for OK.")
        # Re-target's question: the new place, named, then OK or Cancel
        ask = ttk.Frame(tw)
        ask.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ask.columnconfigure(0, weight=1)
        vals["rt_ask"] = tk.StringVar(value="")
        gui_form.mark(ttk.Label(ask, textvariable=vals["rt_ask"],
                                wraplength=170, justify="left"),
                      "live").grid(row=0, column=0, sticky="w")
        ttk.Button(ask, text="OK", width=4,
                   command=self._gce_retarget_ok).grid(
            row=0, column=1, sticky="e", padx=(6, 0))
        ttk.Button(ask, text="Cancel", width=7,
                   command=self._gce_retarget_cancel).grid(
            row=0, column=2, sticky="e", padx=(4, 0))
        ask.grid_remove()
        vals["rt_row"] = ask
        vals["tool_row"] = tw
        vals["name"] = tk.StringVar(value=self._gce_edit_name(c))
        vals["name_row"] = F.entry(
            "Name", vals["name"], "What this edit is called, in the tree "
            "and in My library.", width="text")
        if is_line:
            vals["type"] = tk.StringVar(value="line")
            F.intro("A line of the design, changed by hand. It stays on "
                    "that line when the design changes.", wrap=250)
        else:
            t0 = self._GCE_CHANGE if isinstance(c, eng.Transform) \
                else self._GCE_ADD
            vals["type"] = tk.StringVar(value=t0)
            vals["type_row"] = F.combo(
                "Type", vals["type"], [self._GCE_ADD, self._GCE_CHANGE],
                    "Add lines writes new g-code into the file. Change "
                    "existing lines alters the moves already there: their "
                    "speed, position or size.", width="text")
            if isinstance(c, eng.Transform):
                w0 = oplabel.get(c.op, ch_labels[0])
            else:
                k0 = c.what if c.what in label_of else \
                    (c.command or {}).get("kind")
                w0 = label_of.get(k0, self._GCE_CUSTOM)
            vals["what"] = tk.StringVar(value=w0)
            vals["what_row"] = F.combo(
                "What", vals["what"],
                ch_labels if t0 == self._GCE_CHANGE else add_labels,
                "A ready-made command, written correctly for the printer "
                "this text is for, or your own lines. For a change: what "
                "it does to the moves.", width="text")
        # a Repeat on an edit asks first how often: every N layers, every N
        # lines, right after it, or another place of a pattern
        sc = c.scope if isinstance(c, eng.Transform) else eng.Scope()
        on_edit = isinstance(c, eng.Transform) and c.op == "repeat" \
            and sc is not None and sc.kind == "edit"
        rp = dict(eng.EDIT_REPEAT_DEFAULTS, **(c.params if on_edit else {}))
        rw_labels = [self._GCE_REP_LAYERS, self._GCE_REP_LINES,
                     self._GCE_REPEAT_HERE] + [
            lab for w, lab in eng.WHERE_CHOICES if w != "every_n_moves"]
        rw0, rep_n = self._gce_repeat_plain(rp)
        vals["rwhere"] = tk.StringVar(value=rw0 or where_lab.get(
            rp["where"], self._GCE_REPEAT_HERE))
        vals["rwhere_row"] = F.combo(
            "Repeat it", vals["rwhere"], rw_labels,
            "How often the edit is written again: after every Nth layer, "
            "every N lines, N more times right after itself, or at every "
            "place of another pattern. Its own place is never doubled.",
            width="text")
        if rw0 is None:     # a new Repeat, or another pattern: defaults
            rep_n = self._GCE_REP_N0["layers"]
        vals["rep_n"] = tk.StringVar(value=self._gce_fmt(rep_n))
        fs["rw_prev"] = vals["rwhere"].get()
        fs["rep_by"] = {self._GCE_REP_UNIT.get(fs["rw_prev"]): rep_n}
        vals["rep_row"] = F.entry(
            "Every", vals["rep_n"],
            "Layers: a copy after layer N, 2N, 3N and so on to the end. 1 "
            "is after every layer. Lines: a copy after every N lines that "
            "move the nozzle (G0 to G3); other lines are not counted.")
        # every kind's fields, built once, shown for the chosen one
        vals["rows"], vals["f"] = {}, {}
        cmd = dict(c.command or {}) if isinstance(c, (eng.Insert, eng.Rule)) \
            else {}
        for k, _lab, fields in forms:
            pre = cmd if cmd.get("kind") == k else {}
            self._gce_kind_rows(F, vals, "i:" + k, fields, pre)
        for op, fields in eng.TRANSFORM_FIELDS.items():
            pre = c.params if isinstance(c, eng.Transform) and c.op == op \
                else {}
            self._gce_kind_rows(F, vals, "t:" + op, fields, pre)
        # the g-code box: always there for added lines, and two-way with
        # the fields above it
        vals["cap"] = tk.StringVar(value="")
        cap = gui_form.mark(ttk.Label(F.frame, textvariable=vals["cap"]),
                            "label")
        cap.grid(row=F._next(None), column=0, columnspan=3, sticky="w",
                 pady=(6, 0))
        txt = tk.Text(F.frame, height=4, width=30, font="MewMono",
                      undo=True, wrap="none")
        txt.grid(row=F._next(None), column=0, columnspan=3, sticky="ew")
        ph = ", ".join(f"[[{k}]] = {d}" for k, d in eng.PLACEHOLDERS)
        add_tip(txt, "The g-code itself. Change it here, or with the fields "
                     "above. These are filled in at each place it is "
                     "written: " + ph + ".")
        vals["box_note"] = tk.StringVar(value="")
        note = gui_form.mark(ttk.Label(F.frame, textvariable=vals["box_note"],
                                       style="Hint.TLabel", wraplength=250,
                                       justify="left"), "live")
        note.grid(row=F._next(None), column=0, columnspan=3, sticky="w")
        vals["box"] = txt
        vals["box_rows"] = (cap, txt, note)
        if is_line:
            r = self._gce_render()
            was = None
            c.on = False
            try:
                for rl in m.render().lines:
                    if tuple(rl.key) == tuple(c.key):
                        was = rl.text.rstrip("\r\n")
                        break
            finally:
                c.on = True
            first = (self._gce_chip_line_texts(c.id, r) or [""])[0]
            start = "" if c.op == "delete" else (
                c.text if c.op == "replace" else first)
            vals["box_note"].set(f"Was: {was}" if was is not None else "")
            fs["box_source"] = "hand"
        else:
            start = "\n".join(eng.edit_lines(c, m.flavor)) \
                if isinstance(c, (eng.Insert, eng.Rule)) else ""
            fs["box_source"] = "fields" if getattr(c, "command", None) \
                else "hand"
            if fs["box_source"] == "fields":
                vals["box_note"].set("Written from the fields above. You can "
                                     "change it here too.")
        txt.insert("1.0", start)
        fs["box_text"] = fs["start"] = start
        # before -> after, for a change
        vals["ba"] = tk.StringVar(value="")
        ba = gui_form.mark(ttk.Label(F.frame, textvariable=vals["ba"],
                                     style="Hint.TLabel", wraplength=250,
                                     justify="left"), "live")
        ba.grid(row=F._next(None), column=0, columnspan=3, sticky="w",
                pady=(4, 0))
        vals["ba_row"] = ba
        # WHERE, for added lines
        vals["where"] = tk.StringVar(
            value=where_lab.get(c.where, self._GCE_SPOT)
            if isinstance(c, eng.Rule) else self._GCE_SPOT)
        vals["where_row"] = F.combo(
            "Where", vals["where"], where_labels,
            "One spot is one place in the text. Every other choice is part "
            "of the print's structure: it lands in every such place and "
            "stays right when the design changes.", width="text")
        spot = ttk.Frame(F.frame)
        spot.grid(row=F._next(None), column=0, columnspan=3, sticky="ew",
                  pady=(2, 2))
        spot.columnconfigure(0, weight=1)
        arrows = ttk.Frame(spot)
        arrows.grid(row=0, column=0, sticky="w")
        gui_form.mark(ttk.Label(arrows, text="Line:"), "label").pack(
            side="left", padx=(0, 4))
        vals["line"] = tk.StringVar(value="")
        le = ttk.Entry(arrows, textvariable=vals["line"], width=7)
        le.pack(side="left", padx=(0, 4))
        add_tip(le, "The line it goes on. Type a line number and press "
                    "Enter.")
        le.bind("<Return>", lambda _e: (self._gce_edit_to_line(), "break")[1])
        le.bind("<KP_Enter>", lambda _e: (self._gce_edit_to_line(),
                                          "break")[1])
        le.bind("<FocusOut>", lambda _e: self._gce_edit_to_line(), add="+")
        vals["line_entry"] = le
        vals["place"] = tk.StringVar(value="")
        gui_form.mark(ttk.Label(spot, textvariable=vals["place"],
                                wraplength=250, justify="left"),
                      "live").grid(row=1, column=0, sticky="w", pady=(2, 0))
        gui_form.mark(ttk.Label(spot, style="Hint.TLabel", wraplength=250,
                                justify="left",
                                text="Or click a part in the tree to put "
                                     "it at the end of that part."),
                      "live").grid(row=2, column=0, sticky="w", pady=(2, 0))
        vals["spot"] = spot
        vals["arrows"] = arrows
        if isinstance(c, eng.Insert):
            fs["anchor"] = c.anchor
        else:
            line = self.gce.caret()[0] if self.gce.get_text() else 0
            fs["anchor"] = m.anchor_at(line, self._gce_render_as_is(m))
        rule = c if isinstance(c, eng.Rule) else None
        for name, flabel, tip, val in (
                ("every", "Every Nth", "1 writes it at every place; 5 at "
                 "every fifth.", rule.every if rule else 1),
                ("first", "From number", "The first layer or scaffold "
                 "(empty: from the start).", rule.first if rule else ""),
                ("last", "To number", "The last one (empty: to the end).",
                 rule.last if rule else ""),
                ("n", "N", "How many moves, mm of fiber or seconds; or the "
                 "height in mm.", rule.n if rule and rule.n else "")):
            vals[name] = tk.StringVar(value=self._gce_fmt(val))
            vals["row_" + name] = F.entry(flabel, vals[name], tip)
        # WHERE, for a change
        sc = c.scope if isinstance(c, eng.Transform) else eng.Scope()
        vals["applies"] = tk.StringVar(value=scope_lab.get(
            sc.kind, self._GCE_SCOPES[0][1]))
        vals["applies_row"] = F.combo(
            "Applies to", vals["applies"], [lab for _k, lab in
                                            self._GCE_SCOPES],
            "Which part of the file it changes. Or click a part in the "
            "tree. It stays on that part when the design changes.",
            width="text")
        vals["sfrom"] = tk.StringVar(value=self._gce_fmt(sc.first))
        vals["sto"] = tk.StringVar(value=self._gce_fmt(sc.last))
        vals["sfrom_row"] = F.entry("From", vals["sfrom"], "The first layer, "
                                    "scaffold or piece, counted from 1.")
        vals["sto_row"] = F.entry("To", vals["sto"], "The last one (empty: "
                                  "just the first).")
        fs["fixed"] = sc if sc is not None and sc.kind in self._GCE_FIXED \
            else None
        # a Repeat on an edit at another pattern: its rows (built above)
        if on_edit:
            for k in ("every", "first", "last", "n"):
                v = rp.get(k)
                vals[k].set(self._gce_fmt(v if k == "every" or v not in (
                    0, 0.0) else ""))

        fs["err"] = tk.StringVar(value="")
        gui_form.mark(ttk.Label(host, textvariable=fs["err"], wraplength=250,
                                justify="left", foreground="#c0392b"),
                      "live").grid(row=3, column=0, sticky="w", pady=(4, 0))
        btns = ttk.Frame(host)
        btns.grid(row=1, column=0, sticky="w", pady=(4, 0))
        btns2 = ttk.Frame(host)
        btns2.grid(row=4, column=0, sticky="w", pady=(4, 0))
        done = ttk.Button(btns, width=6, text="Keep" if c.typed else "Done",
                          style="Primary.TButton",
                          command=lambda: self._gce_edit_finish(keep=True))
        done.pack(side="left")
        add_tip(done, "Keep this edit as it is now." + (
            " A typed edit is otherwise replaced when the design changes."
            if c.typed else ""))
        cancel = ttk.Button(btns, width=7, text="Cancel",
                            command=lambda: self._gce_edit_finish(keep=False))
        cancel.pack(side="left", padx=(6, 0))
        add_tip(cancel, "Put everything back as it was before you opened "
                        "it" + (" (the new edit goes away)." if new else "."))
        rm = ttk.Button(btns, width=7, text="Remove", command=lambda: (
            self._gce_edit_finish(keep=True, remove=True)))
        rm.pack(side="left", padx=(6, 0))
        add_tip(rm, "Take this edit out of the g-code (Undo brings it "
                    "back).")
        # My library: save, every new print, delete -- for added lines and
        # changes (a changed line belongs to its file)
        save =ttk.Button(btns2, text="Save to library",
                          command=self._gce_edit_save)
        save.pack(side="left")
        add_tip(save, "Keep it in My library under its name, to use on any "
                      "other g-code.")
        fs["save_btn"] = save
        vals["del_btn"] = ttk.Button(btns2, text="Remove from library",
                                     command=self._gce_edit_lib_delete)
        add_tip(vals["del_btn"], "Take it out of My library. It stays in "
                                 "this g-code.")
        vals["auto"] = tk.BooleanVar(value=False)
        auto = ttk.Checkbutton(
            host, text="Run automatically on every print",
            variable=vals["auto"],
            command=lambda: self._gce_edit_lib_set(
                use_on_new=vals["auto"].get()))
        auto.grid(row=5, column=0, sticky="w", pady=(4, 0))
        add_tip(auto, "Save it to My library (if it is not there yet) and "
                      "write it into every g-code the app makes, at the "
                      "place it says. This g-code takes it when you press "
                      "Done. It shows in the tree as a locked row; turn it "
                      "off here or in My library.")
        vals["lib_note"] = tk.StringVar(value="")
        gui_form.mark(ttk.Label(host, textvariable=vals["lib_note"],
                                style="Hint.TLabel", wraplength=250,
                                justify="left"), "live").grid(
            row=6, column=0, sticky="w")
        vals["auto_row"] = auto
        if is_line:
            btns2.grid_remove()
            auto.grid_remove()
        self._gce_fs = fs
        self._gce_edit_lib_vis()

        def on_type(*_a):
            if fs["loading"]:
                return
            add = vals["type"].get() == self._GCE_ADD
            vals["what_row"].field.configure(
                values=add_labels if add else ch_labels)
            if add and vals["what"].get() not in add_labels:
                vals["what"].set(label_of.get("dwell", self._GCE_CUSTOM))
            elif not add and vals["what"].get() not in ch_labels:
                vals["what"].set(ch_labels[0])
            self._gce_edit_vis()
            self._gce_edit_kick()

        def on_what(*_a):
            if fs["loading"]:
                return
            if vals["type"].get() == self._GCE_ADD:
                if vals["what"].get() in kind_of:
                    fs["box_source"] = "fields"
                    self._gce_edit_box_from_fields()
                else:
                    fs["box_source"] = "hand"
                    vals["box_note"].set("")
            self._gce_edit_vis()
            self._gce_edit_kick()

        def on_field(*_a):
            if fs["loading"]:
                return
            if vals["type"].get() == self._GCE_ADD:
                fs["box_source"] = "fields"
                self._gce_edit_box_from_fields()
            self._gce_edit_kick()

        def on_where(*_a):
            if fs["loading"]:
                return
            self._gce_rep_switch()
            self._gce_edit_vis()
            self._gce_edit_kick()

        def on_name(*_a):
            if fs["loading"] or fs["naming"]:
                return
            fs["name_auto"] = not vals["name"].get().strip()
            self._gce_edit_kick()

        def on_box(_e=None):
            if fs["loading"]:
                return
            now = txt.get("1.0", "end-1c")
            if now == fs["box_text"]:
                return
            fs["box_text"] = now
            if fs["box_source"] != "hand" and not is_line:
                fs["box_source"] = "hand"
                if vals["what"].get() in kind_of:
                    vals["box_note"].set(
                        "Written by hand. Change a field above to write it "
                        "from the fields again.")
            self._gce_edit_kick()
        vals["type"].trace_add("write", on_type)
        if not is_line:
            vals["what"].trace_add("write", on_what)
        for key, fv in vals["f"].items():
            for v in fv.values():
                v.trace_add("write", on_field)
        vals["name"].trace_add("write", on_name)
        for key in ("every", "first", "last", "n", "sfrom", "sto"):
            vals[key].trace_add("write", on_where)
        vals["where"].trace_add("write", on_where)
        vals["rwhere"].trace_add("write", on_where)
        vals["applies"].trace_add("write", on_where)

        vals["rep_n"].trace_add("write", on_where)
        txt.bind("<KeyRelease>", on_box, add="+")
        txt.bind("<<Paste>>", lambda _e: self.root.after_idle(on_box),
                 add="+")
        txt.bind("<<Cut>>", lambda _e: self.root.after_idle(on_box), add="+")
        if fs["box_source"] == "hand" and not is_line \
                and vals["what"].get() in kind_of:
            vals["box_note"].set("Written by hand. Change a field above to "
                                 "write it from the fields again.")
        self._gce_edit_vis()
        fs["loading"] = False
        self._gce_host_keys(host, lambda: self._gce_edit_finish(keep=True),
                            lambda: self._gce_edit_finish(keep=False))

    def _gce_kind_rows(self, F, vals, key, fields, pre):
        eng = self._gce_engine()
        op = key[2:] if key.startswith("t:") else None
        choices = eng.TRANSFORM_CHOICES.get(op, {})
        tips = eng.TRANSFORM_TIPS.get(op, {})
        vals["f"][key] = {}
        rows = []
        for name, flabel, unit, default in fields:
            tip = tips.get(name) or gui_units.localize(flabel + (
                f", in {unit}." if unit else "."))
            if name in choices:     # a choice: the var holds its label
                now = pre.get(name, default)
                v = tk.StringVar(value=dict(choices[name]).get(
                    now, choices[name][0][1]))
                vals["f"][key][name] = v
                rows.append(F.combo(flabel, v, [lab for _v, lab
                                                in choices[name]], tip))
                continue
            v = tk.StringVar(value=self._gce_fmt(pre.get(name, default)))
            vals["f"][key][name] = v
            rows.append(F.entry(flabel, v, tip, unit=unit or None,
                                width="num" if unit else "text"))
        vals["rows"][key] = rows

    def _gce_edit_key(self):
        """The fields key of the kind the form shows ("i:dwell",
        "t:scale"), or None (your own g-code, a changed line)."""
        eng = self._gce_engine()
        vals = self._gce_fs["vals"]
        typ = vals["type"].get()
        if typ == self._GCE_ADD:
            k = {lab: k for k, lab, _f in eng.INSERT_FORMS}.get(
                vals["what"].get())
            return "i:" + k if k else None
        if typ == self._GCE_CHANGE:
            op = {lab: op for op, lab in eng.TRANSFORM_KINDS}.get(
                vals["what"].get())
            return "t:" + op if op else None
        return None

    def _gce_edit_vis(self):
        """Show the rows the form's choices call for, hide the rest."""
        fs = self._gce_fs
        vals = fs["vals"]
        eng = self._gce_engine()
        typ = vals["type"].get()
        add, change = typ == self._GCE_ADD, typ == self._GCE_CHANGE
        key = self._gce_edit_key()
        for k, rows in vals["rows"].items():
            for row in rows:
                row.show(k == key)
        for w in vals["box_rows"]:
            w.grid() if (add or fs["line"]) else w.grid_remove()
        vals["cap"].set("This line becomes:" if fs["line"]
                        else "G-code it writes:")
        vals["ba_row"].grid() if (change or fs["line"]) \
            else vals["ba_row"].grid_remove()
        vals["where_row"].show(add)
        w = {lab: k for k, lab in eng.WHERE_CHOICES}.get(vals["where"].get())
        vals["spot"].grid() if add else vals["spot"].grid_remove()
        vals["arrows"].grid() if add and w is None \
            else vals["arrows"].grid_remove()
        show = set()
        if add and w in ("before_layer", "after_layer", "before_group",
                         "after_group"):
            show |= {"every", "first", "last"}
        if add and w and (w.startswith("every_n") or w == "at_z"):
            show.add("n")
        for k in ("every", "first", "last", "n"):
            vals["row_" + k].show(k in show)
        fixed = fs.get("fixed")
        on_edit = change and fixed is not None and fixed.kind == "edit" \
            and key == "t:repeat"
        vals["applies_row"].show(change and fixed is None)
        ranged = change and fixed is None \
            and vals["applies"].get() != self._GCE_SCOPES[0][1]
        vals["sfrom_row"].show(ranged)
        vals["sto_row"].show(ranged)
        vals["rwhere_row"].show(on_edit)
        unit = None
        if on_edit:
            rl = vals["rwhere"].get()
            unit = {self._GCE_REP_LAYERS: "layers",
                    self._GCE_REP_LINES: "lines"}.get(rl)
            rw = None if unit else {
                lab: k for k, lab in eng.WHERE_CHOICES}.get(rl)
            if rw in ("before_layer", "after_layer", "before_group",
                      "after_group"):
                show |= {"every", "first", "last"}
            if rw and (rw.startswith("every_n") or rw == "at_z"):
                show.add("n")
            for k in ("every", "first", "last", "n"):
                vals["row_" + k].show(k in show)
            vals["rows"]["t:repeat"][0].show(rl == self._GCE_REPEAT_HERE)
            if unit:
                vals["rep_row"].label.configure(
                    text=gui_form.label_text("Every", unit))
        vals["rep_row"].show(bool(unit))
        fs["save_btn"].pack_forget() if fs["line"] else None
        # always the whole form (no "More options"); a tool's edit keeps
        # its place and Re-target on top, and a Repeat on an edit drops
        # Type and What -- the tool chose them
        tool = fs.get("tool")
        vals["tool_row"].grid() if tool else vals["tool_row"].grid_remove()
        if "type_row" in vals:
            vals["type_row"].show(not on_edit)
            vals["what_row"].show(not on_edit)
        if not fs["line"] and not on_edit:
            vals["auto_row"].grid()
        else:
            vals["auto_row"].grid_remove()

    def _gce_edit_box_from_fields(self):
        """The box rewritten from the fields (a ready-made command)."""
        fs = self._gce_fs
        vals = fs["vals"]
        eng = self._gce_engine()
        key = self._gce_edit_key()
        if not key or not key.startswith("i:"):
            return
        k = key[2:]
        fields = next(f for kk, _l, f in eng.INSERT_FORMS if kk == k)
        try:
            lines = eng.make_command(k, self.gce_flavor_name(),
                                     **self._gce_field_values(
                                         vals["f"][key], fields))
        except ValueError as exc:
            vals["box_note"].set(str(exc))
            return
        text = "\n".join(lines)
        txt = vals["box"]
        if txt.get("1.0", "end-1c") != text:
            txt.delete("1.0", "end")
            txt.insert("1.0", text)
        fs["box_text"] = text
        vals["box_note"].set("Written from the fields above. You can "
                             "change it here too.")

    def _gce_edit_kick(self):
        fs = self._gce_fs
        if fs["after"] is not None:
            try:
                self.root.after_cancel(fs["after"])
            except (ValueError, tk.TclError):
                pass
        fs["after"] = self.root.after(250, self._gce_edit_apply)

    def _gce_edit_flush(self):
        fs = getattr(self, "_gce_fs", None)
        if fs is not None and fs["after"] is not None:
            try:
                self.root.after_cancel(fs["after"])
            except (ValueError, tk.TclError):
                pass
            self._gce_edit_apply()

    def _gce_edit_apply(self):
        """The form's state, applied to the document now."""
        fs = getattr(self, "_gce_fs", None)
        if fs is None:
            return
        fs["after"] = None
        # typing in the text while the form is open is kept, not drawn over
        self._gce_record_edits()
        m = self._gce_model_now()
        try:
            c = m.get(fs["cid"])
        except (KeyError, AttributeError):
            return self._gce_edit_lost()
        try:
            new = self._gce_edit_build(c)
        except ValueError as exc:
            fs["err"].set(str(exc))
            return
        fs["err"].set("")
        if new.to_dict() != c.to_dict():
            m.changes[m.changes.index(c)] = new
            self._gce_rerender(None)
        self._gce_edit_after()

    def _gce_edit_build(self, c):
        """The chip the form describes, with `c`'s id. Raises ValueError
        with a sentence for a field that does not read."""
        eng = self._gce_engine()
        m = self._gce_model_now()
        fs = self._gce_fs
        vals = fs["vals"]
        name = "" if fs["name_auto"] else vals["name"].get().strip()
        common = dict(id=c.id, on=c.on, saved=c.saved, typed=c.typed,
                      label=name)
        box = vals["box"].get("1.0", "end-1c").strip("\n")
        if fs["line"]:
            line = box.split("\n")[0] if box.strip() else ""
            if line == fs["start"].split("\n")[0]:
                new = copy.deepcopy(c)      # a words edit stays one
                new.label = name
                return new
            return eng.LineEdit(key=tuple(c.key), at=tuple(c.at),
                                stage=c.stage,
                                op="replace" if line.strip() else "delete",
                                text=line, flavor=m.flavor, what="line",
                                **common)
        key = self._gce_edit_key()
        if vals["type"].get() == self._GCE_CHANGE:
            op = key[2:]
            fields = eng.TRANSFORM_FIELDS.get(op, ())
            params = self._gce_field_values(vals["f"][key], fields)
            if op == "layer_shift":
                steps = eng.parse_shift_cycle(params.get("cycle", ""))
                if params.get("profile") == "cycle" and not steps:
                    raise ValueError("Cycle needs its list of steps, e.g. "
                                     "0, 1, 3, 6.")
                if int(params.get("period", 10)) < 1:
                    raise ValueError("Wave period must be 1 layer or more.")
            fixed = fs.get("fixed")
            if fixed is not None:
                rl = vals["rwhere"].get()
                if fixed.kind == "edit" and op == "repeat" and rl in (
                        self._GCE_REP_LAYERS, self._GCE_REP_LINES):
                    n = self._gce_num(vals, "rep_n", "Every", cast=int)
                    if n < 1:
                        raise ValueError("Every must be 1 or more.")
                    params = dict(eng.EDIT_REPEAT_DEFAULTS, **params)
                    if rl == self._GCE_REP_LAYERS:
                        # after layer N, 2N, 3N ... to the end
                        params.update(where="after_layer", every=n, first=n,
                                      last=None, n=0.0)
                    else:
                        params.update(where="every_n_moves", every=1,
                                      first=None, last=None, n=float(n))
                elif fixed.kind == "edit" and op == "repeat":
                    rw = {lab: x for x, lab in eng.WHERE_CHOICES}.get(rl)
                    params = dict(eng.EDIT_REPEAT_DEFAULTS, **params)
                    params["where"] = rw or eng.REPEAT_HERE
                    params["every"] = self._gce_num(
                        vals, "every", "Every Nth", required=False,
                        cast=int) or 1
                    params["first"] = self._gce_num(
                        vals, "first", "From number", required=False,
                        cast=int)
                    params["last"] = self._gce_num(
                        vals, "last", "To number", required=False, cast=int)
                    params["n"] = self._gce_num(vals, "n", "N", required=bool(
                        rw and (rw.startswith("every_n") or rw == "at_z"))) \
                        or 0.0
                return eng.Transform(op=op, params=params,
                                     scope=copy.deepcopy(fixed), what=op,
                                     **common)
            sk = {lab: k for k, lab in self._GCE_SCOPES}[
                vals["applies"].get()]
            first = last = None
            if sk != "all":
                first = self._gce_num(vals, "sfrom", "From", cast=int)
                last = self._gce_num(vals, "sto", "To", required=False,
                                     cast=int)
            return eng.Transform(op=op, params=params,
                                 scope=eng.Scope(sk, first, last), what=op,
                                 **common)
        k = key[2:] if key else None
        command = None
        if k and fs["box_source"] == "fields":
            fields = next(f for kk, _l, f in eng.INSERT_FORMS if kk == k)
            fv = self._gce_field_values(vals["f"][key], fields)
            lines = list(eng.make_command(k, m.flavor, **fv))
            command = dict(fv, kind=k)
        else:
            lines = box.split("\n") if box.strip() else []
        if not lines:
            raise ValueError("Write the g-code it adds in the box.")
        what = k or "custom"
        w = {lab: x for x, lab in eng.WHERE_CHOICES}.get(vals["where"].get())
        if w is None:
            return eng.Insert(lines=lines, anchor=fs["anchor"],
                              command=command, flavor=m.flavor, what=what,
                              **common)
        every = self._gce_num(vals, "every", "Every Nth", required=False,
                              cast=int) or 1
        first = self._gce_num(vals, "first", "From number", required=False,
                              cast=int)
        last = self._gce_num(vals, "last", "To number", required=False,
                             cast=int)
        n = self._gce_num(vals, "n", "N", required=bool(
            w.startswith("every_n") or w == "at_z")) or 0.0
        return eng.Rule(text="\n".join(lines), where=w, every=every,
                        first=first, last=last, n=n, name=name,
                        flavor=m.flavor, command=command, what=what,
                        **common)

    def _gce_chip_line_texts(self, cid, r=None):
        r = r or self._gce_render()
        return [rl.text.rstrip("\r\n") for rl in r.lines if rl.chip == cid]

    # -- what a change does, in numbers -------------------------------------- #
    _GCE_F = re.compile(r"(?:^|\s)F(-?\d+(?:\.\d*)?|-?\.\d+)", re.I)

    def _gce_render_without(self, c):
        """The render's lines with edit `c` switched off: the text as it
        was before this edit (every other edit still on)."""
        m = self._gce_model_now()
        was = c.on
        c.on = False
        try:
            # cached on everything the render reads, as `_gce_render` is: a
            # Move's every apply asked for it again, though only the edit's
            # own numbers had changed -- a whole render (2 s on a 20x20 mm
            # waved scaffold) per gimbal release
            key = self._gce_doc_key(m, off=c.id)
            hit = getattr(self, "_gce_rcache_without", None)
            if hit is not None and hit[0] == key:
                return hit[1]
            lines = m.render().lines
            self._gce_rcache_without = (key, lines)
            return lines
        finally:
            c.on = was

    def _gce_feeds(self, rlines):
        """The feed (mm/min) in effect on each rendered line: the last F
        written at or before it, the engine's default before the first."""
        f = self._gce_engine().DEFAULT_FEED
        out = []
        for rl in rlines:
            hit = self._GCE_F.search(rl.text.split(";", 1)[0])
            if hit:
                try:
                    f = float(hit.group(1))
                except ValueError:
                    pass
            out.append(f)
        return out

    @staticmethod
    def _gce_count_words(plain, now):
        """ "Changes 3 lines, adds 1": a walk of the text before and after
        one edit. A change only rewrites lines or adds lines after them, so
        a line that matches neither side's next one is a rewritten line."""
        i = j = changed = added = 0
        a = [rl.text for rl in plain]
        b = [rl.text for rl in now]
        if len(a) == len(b):
            # nothing added: line for line (the walk below would resync on
            # a rewritten line that happens to equal the next one -- a
            # turned layer has many)
            i = j = len(a)
            changed = sum(x != y for x, y in zip(a, b))
        while i < len(a) and j < len(b):
            if a[i] == b[j]:
                i += 1
                j += 1
            elif j + 1 < len(b) and b[j + 1] == a[i]:
                added += 1
                j += 1
            else:
                changed += 1
                i += 1
                j += 1
        added += len(b) - j
        s = lambda n: "" if n == 1 else "s"  # noqa: E731
        out = f"Changes {changed} line{s(changed)}"
        return out + (f", adds {added}" if added else "")

    def _gce_speed_now(self, c):
        """(line index, feed in mm/min) where a speed edit starts and the
        feed the text has there without it, or None when it covers no move.
        A speed no move has is set for a moment, so the edit writes F on
        the first move it covers, whatever its own value is."""
        m = self._gce_model_now()
        eng = self._gce_engine()
        keep = dict(c.params or {})
        c.params = dict(keep, mm_s=1e-4, percent=1e-4)
        try:
            ids = m.lineage(c.id)
            first = next((i for i, rl in enumerate(m.render().lines)
                          if rl.chip in ids), None)
        finally:
            c.params = keep
        if first is None:
            return None
        plain = self._gce_render_without(c)
        if first >= len(plain):
            return None
        return first, self._gce_feeds(plain[:first + 1])[-1]

    def _gce_speed_result(self, c, r, lines):
        """The Speed form's result line: the speed now and what it becomes,
        and where the text's own speed comes back -- "800 → 200 mm/min,
        restores 800 after layer 5 of scaffold 1". The numbers are read
        off the text, so a designer parameter under the edit is in them."""
        now = self._gce_speed_now(c)
        if now is None:
            return "Covers no moves here yet."
        i, was = now
        feeds = self._gce_feeds(r.lines)
        to = feeds[i] if i < len(feeds) else was
        u = gui_units.shown("speed", "mm/min")
        n = lambda f: gui_units.num("speed", "%g" % round(f, 4),  # noqa
                                    "mm/min")
        if abs(to - was) < 1e-9:
            return f"Now {n(was)} {u}. Type the new speed."
        out = f"{n(was)} → {n(to)} {u}"
        back = next((j for j in range(i + 1, len(feeds))
                     if abs(feeds[j] - to) > 1e-9), None)
        if back is None:
            out += ", to the end of the file"
        else:
            node = self._gce_node_of_lines(back + 1, back + 1)
            where = (self._gce_tree_info.get(node) or {}).get("label") \
                if node else None
            out += f", restores {n(feeds[back])}" + (
                f" after {where}" if where else f" at line {back + 1}")
        return out + ". " + self._gce_count_words(
            self._gce_render_without(c), r.lines) + "."

    def _gce_edit_after(self):
        """After an apply: light up the edit's lines, say where it is, and
        for a change show the first line before and after."""
        fs = getattr(self, "_gce_fs", None)
        if fs is None:
            return
        eng = self._gce_engine()
        m = self._gce_model_now()
        vals = fs["vals"]
        try:
            c = m.get(fs["cid"])
        except KeyError:
            return
        r = self._gce_render()
        ids = m.lineage(c.id)
        lines = [i for i, rl in enumerate(r.lines, start=1)
                 if rl.chip in ids]
        self.gce.set_focus(lines, see=lines[:1] != fs["seen"])
        fs["seen"] = lines[:1]
        if fs["name_auto"]:
            auto = self._gce_edit_name(c)
            if vals["name"].get() != auto:
                fs["naming"] = True
                try:
                    vals["name"].set(auto)
                finally:
                    fs["naming"] = False
        if isinstance(c, eng.Transform) and c.scope is not None \
                and c.scope.kind in self._GCE_FIXED:
            on = eng.chip_label(m, c).partition(" · ")[2]
            if c.scope.kind == "edit":
                on = on[3:] if on.startswith("on ") else on
                vals["tool_where"].set(("Repeats " if c.op == "repeat"
                                        else "Applies to ") + "“%s”" % on)
            else:
                vals["tool_where"].set("Applies to exactly the selection: "
                                       + on)
        elif isinstance(c, eng.Transform):
            sc = c.scope
            one = sc is not None and sc.first is not None \
                and str(sc.first) == str(sc.last)
            if sc is not None and sc.kind == "layers"                     and self._gce_layers_words(sc):
                where = self._gce_layers_words(sc)
            elif one and sc.kind == "layers":
                where = f"layer {sc.first}"
            else:
                where = eng.chip_label(m, c).partition(" · ")[2] \
                    or "the whole file"
            vals["tool_where"].set("Applies to " + where)
        elif isinstance(c, eng.Insert):
            vals["tool_where"].set("Goes " + eng.anchor_label(m, c.anchor))
        elif self._gce_every_n_layers(c) is not None:
            n = self._gce_every_n_layers(c)
            k = len({rl.uid[2] for rl in self._gce_render().lines
                     if rl.chip == c.id and len(rl.uid or ()) > 2
                     and rl.uid[0] == "rule"})
            vals["tool_where"].set(
                ("Goes after every layer" if n == 1 else
                 f"Goes after every {n} layers") + f" · {k} places")
        if isinstance(c, eng.Transform) and c.op == "layer_shift":
            # how far the last layer went, and what found nothing to move
            said = eng.layer_shift_summary(m, c)
            if said:
                vals["tool_where"].set(vals["tool_where"].get().rstrip(".")
                                       + ". " + said)
        if isinstance(c, eng.Insert):
            le = vals["line_entry"]
            if le.focus_get() is not le or fs.get("line_set"):
                vals["line"].set(str(lines[0]) if lines else "")
            fs["line_set"] = False
            if lines:
                where = eng.anchor_label(m, c.anchor)
                vals["place"].set(where[:1].upper() + where[1:] + ".")
            else:
                vals["place"].set("Its place is gone from the new design. "
                                  "Type the line it goes on, or click a "
                                  "part in the tree.")
        elif isinstance(c, eng.Rule):
            per = max(1, len(eng.edit_lines(c, m.flavor)))
            n = len(lines) // per
            vals["place"].set(
                f"Lands in {n} place{'s' if n != 1 else ''}"
                + (f", first at line {lines[0]}." if lines else "."))
        if isinstance(c, eng.Transform) and c.op in ("speed_set",
                                                    "speed_scale"):
            vals["ba"].set(self._gce_speed_result(c, r, lines))
        elif isinstance(c, eng.Transform) and lines:
            i = lines[0] - 1
            plain = self._gce_render_without(c)
            before = plain[i].text.strip() if i < len(plain) else ""
            vals["ba"].set(self._gce_count_words(plain, r.lines)
                           + f". First, line {lines[0]}: {before}  →  "
                           f"{r.lines[i].text.strip()}")
        elif isinstance(c, eng.Transform):
            vals["ba"].set("Changes no lines here yet.")
        elif fs["line"]:
            vals["ba"].set("")

    def _gce_edit_to_line(self):
        """The Line box: put the open one-spot edit on the typed line."""
        fs = getattr(self, "_gce_fs", None)
        if fs is None:
            return
        vals = fs["vals"]
        raw = vals["line"].get().strip()
        m = self._gce_model_now()
        eng = self._gce_engine()
        try:
            c = m.get(fs["cid"])
        except KeyError:
            return
        if not isinstance(c, eng.Insert) \
                or vals["where"].get() != self._GCE_SPOT:
            return
        now = next((i for i, rl in enumerate(self._gce_render().lines,
                                             start=1) if rl.chip == c.id),
                   None)
        if raw == str(now):
            return
        try:
            want = int(raw)
        except ValueError:
            fs["err"].set(f"Line must be a whole number, not “{raw}”.")
            return
        fs["err"].set("")
        self._gce_edit_flush()
        c = m.get(fs["cid"])
        if not isinstance(c, eng.Insert):
            return
        got = m.move_to_line(c.id, want)
        fs["anchor"] = m.get(c.id).anchor
        self._gce_rerender(None)
        fs["line_set"] = True
        self._gce_edit_after()
        if got is not None and got != want:
            self._gce_status(f"It went on line {got}, the nearest place to "
                             f"line {want} it can go.")

    def _gce_form_take_node(self, info):
        """A part clicked in the tree while an edit is open: the edit
        goes there."""
        fs = getattr(self, "_gce_fs", None)
        if fs is None:
            return
        vals = fs["vals"]
        if fs["line"]:
            self._gce_status("A changed line stays on its own line.")
            return
        if vals["type"].get() == self._GCE_CHANGE:
            sc = self._gce_node_scope(info)
            if sc is None:
                self._gce_status("A change covers the whole file, a scaffold "
                                 "or a layer: click one of those.")
                return
            vals["applies"].set(dict(self._GCE_SCOPES)[sc.kind])
            vals["sfrom"].set(self._gce_fmt(sc.first))
            vals["sto"].set(self._gce_fmt(sc.last))
        else:
            a = self._gce_node_anchor(info)
            if a is None:
                self._gce_status("For the whole file, pick a Where such as "
                                 "After each layer.")
                return
            fs["anchor"] = a
            vals["where"].set(self._GCE_SPOT)
        self._gce_edit_flush() if fs["after"] is not None else \
            self._gce_edit_apply()

    def _gce_edit_save(self):
        fs = getattr(self, "_gce_fs", None)
        if fs is None:
            return
        self._gce_edit_flush()
        name = fs["vals"]["name"].get().strip()
        if not name:
            fs["err"].set("Give it a name first.")
            return
        fs["name_auto"] = False
        self._gce_save_id(fs["cid"])
        self._gce_edit_lib_vis()

    def _gce_edit_finish(self, keep=True, remove=False):
        """Close the form. `keep`: the edit stays as it is now (no longer a
        typed edit), one undo step; else everything goes back to how it was
        when the form opened. `remove`: the edit is taken out."""
        fs = getattr(self, "_gce_fs", None)
        if fs is None:
            return
        if keep:
            # lines typed in the text while the form was open are edits of
            # their own; the text is re-rendered below, so record them now
            self._gce_record_edits()
        if keep and not remove:
            self._gce_edit_to_line()    # a line typed, Enter not pressed
            self._gce_edit_flush()
            old_name = fs.get("library_name")
            model = self._gce_model_now()
            if old_name and model is not None:
                try:
                    edit = model.get(fs["cid"])
                except KeyError:
                    edit = None
                if (edit is not None and edit.saved == old_name
                        and self._gce_edit_name(edit).strip() != old_name):
                    self._gce_save_id(edit.id)
        if fs["after"] is not None:
            try:
                self.root.after_cancel(fs["after"])
            except (ValueError, tk.TclError):
                pass
        self._gce_fs = None
        m = self._gce_model_now()
        if m is not None:
            if not keep:
                m.restore(fs["before"])
            elif remove:
                m.remove(fs["cid"])
            else:
                try:
                    m.get(fs["cid"]).typed = False
                except KeyError:
                    pass
            r = self._gce_render()
            self._gce_doc["base"] = r.text
            if self.gce.get_text() != r.text:
                self.gce.set_text(r.text, undoable=True)
            self.gce.end_group(meta=(fs["before"], m.snapshot()) if keep
                               else None, keep=keep)
        self.gce.set_focus([])
        self._gce_host_close()
        self._gce_set_link_label()
        self._gce_render_changes()
        # the picture waited for this: one rebuild, or none when the text
        # is back to what it shows (Cancel) and zero has not moved meanwhile
        self._gce_pv_release(self._gce_pv_stale())
        self._gce_refresh_controls()
        self._gce_sync_persist()
        getattr(self, "_automatic_respell_if_pending", lambda: None)()

    def _gce_edit_lost(self):
        """The open edit is gone (typed away, or the design dropped it)."""
        fs = getattr(self, "_gce_fs", None)
        if fs is None:
            return
        self._gce_fs = None
        m = self._gce_model_now()
        self.gce.end_group(meta=(fs["before"], m.snapshot())
                           if m is not None else None)
        self.gce.set_focus([])
        self._gce_host_close()
        self._gce_pv_release(True)
        getattr(self, "_automatic_respell_if_pending", lambda: None)()

    def _gce_before_step(self):
        """Undo or Redo pressed with an edit open: it is kept first, so the
        step undoes the whole form session."""
        self._gce_edit_finish(keep=True)

    # -- stitched pieces -------------------------------------------------- #
    def _gce_piece_set(self, p, attr, value):
        before = self._gce_snap()
        setattr(p, attr, value)
        self._gce_rerender(before)

    def _gce_move_piece(self, i, d):
        ps = self._gce_model.pieces
        j = i + d
        if 0 <= j < len(ps):
            before = self._gce_snap()
            ps[i], ps[j] = ps[j], ps[i]
            self._gce_rerender(before)

    def _gce_remove_piece(self, i):
        before = self._gce_snap()
        ps = self._gce_model.pieces
        del ps[i]
        if all(p.source == "designer" for p in ps):
            ps.clear()
        self._gce_rerender(before)

    def _gce_stitch_edit(self, source):
        """Stitch g-code: a file's moves, or lines you paste, as an edit of
        your own g-code at the caret, opened in the edit form. A file keeps
        only its first move to its last (`Piece.drop_frame`'s rule: its
        homing and heating must not run mid-print) and loses its comments,
        which exported g-code never carries."""
        eng = self._gce_engine()
        m = self._gce_model_now()
        if eng is None or m is None:
            return
        lines, name = [""], "Pasted g-code"
        if source == "file":
            path = filedialog.askopenfilename(
                title="Stitch a g-code file in",
                filetypes=[("G-code", "*.gcode *.gco *.g *.nc *.txt"),
                           ("All files", "*.*")])
            if not path:
                return
            try:
                # decoded and tidied as Open file reads it (UTF-16, packed
                # words), so a file that opens also stitches
                import mew_gcode
                raw, _tidied = mew_gcode.read_foreign_gcode(path)
            except OSError as exc:
                self._gce_notice_show(
                    f"That file could not be opened: {exc}",
                    [("OK", self._gce_notice_hide)])
                return
            flavor = eng.detect_flavor(raw)
            text, _notes = eng.normalise_piece_text(raw, flavor)
            fl = eng.get_flavor(flavor)
            lines = [ln.split(fl.comment)[0].rstrip()
                     for ln in text.splitlines()]
            lines = [ln for ln in lines if ln.strip()]

            def moves(ln):
                cmd, words = eng.parse_words(ln, fl.comment)
                return (eng.canon_cmd(cmd) if cmd else "") in (
                    "G0", "G1", "G2", "G3") and any(
                    k in words for k in ("X", "Y", "Z"))
            at = [i for i, ln in enumerate(lines) if moves(ln)]
            if at:
                lines = lines[at[0]:at[-1] + 1]
            if not lines:
                self._gce_status("That file has no g-code to stitch in.")
                return
            name = os.path.basename(path)
        self._gce_edit_finish(keep=True)
        before = self._gce_snap()
        self.gce.begin_group()
        r = self._gce_render()
        line = self.gce.caret()[0] if self.gce.get_text() else 0
        if source == "file":
            lines = lines + self._gce_stitch_return(lines, r, line)
        c = m.add(eng.Insert(lines=lines, anchor=self._gce_sel_anchor()
                             or m.anchor_at(line, r),
                             flavor=m.flavor, what="custom", label=name))
        self._gce_rerender(None)
        self._gce_open_edit(c.id, new=True, before=before)
        if source == "snippet":
            box = self._gce_fs["vals"]["box"]
            box.focus_set()
            self._gce_fs["vals"]["box_note"].set(
                "Paste or type the g-code here.")

    def _gce_stitch_return(self, lines, r, line):
        """``["G0 Z<h>"]`` when the stitched `lines` leave the nozzle at a
        height other than `h`, the one it had at text line `line` of render
        `r`; else []. The print after a stitch names no height of its own
        (with "Don't adjust height per layer" a design names Z once, at its
        top), so without this line it carries on at the stitched file's last
        height -- the way a piece's join (`Document._join`) goes back to the
        next piece's height. It is an ordinary line of the edit, shown in
        its form."""
        eng = self._gce_engine()
        w = eng._Walker()
        for rl in r.lines[:max(0, int(line))]:
            for part in rl.text.splitlines():
                w.step(part)
        before = w.z
        for ln in lines:
            w.step(ln)
        if before is None or w.z is None or abs(w.z - before) <= 1e-9:
            return []
        return ["G0 Z%s" % eng.fmt_num(before)]

    def _gce_open_piece(self, source, piece=None):
        eng = self._gce_engine()
        m = self._gce_model_now()
        if eng is None or m is None:
            return
        notes = []
        p = piece
        if p is None:
            text, name = "", "Snippet"
            if source == "file":
                path = filedialog.askopenfilename(
                    title="Add a g-code file as a piece",
                    filetypes=[("G-code", "*.gcode *.gco *.g *.nc *.txt"),
                               ("All files", "*.*")])
                if not path:
                    return
                try:
                    with open(path, encoding="utf-8",
                              errors="replace") as fh:
                        raw = fh.read()
                except OSError as exc:
                    self._gce_notice_show(
                        f"That file could not be opened: {exc}", [])
                    return
                text, notes = eng.normalise_piece_text(
                    raw, eng.detect_flavor(raw))
                name = os.path.basename(path)
            p = eng.Piece(source=source, name=name, text=text,
                          flavor=self.gce_flavor_name(),
                          drop_frame=(source == "file"))

        def build(F, vals):
            vals["name"] = tk.StringVar(value=p.name)
            F.entry("Name", vals["name"], "What the tree calls this piece.",
                    width="text")
            if notes:
                F.intro("Made ready to stitch: " + " ".join(notes))
            if p.source == "snippet":
                gui_form.mark(ttk.Label(F.frame, text="G-code:"),
                              "label").grid(row=F._next(None), column=0,
                                            sticky="w")
                txt = tk.Text(F.frame, height=5, width=30, font="MewMono",
                              undo=True, wrap="none")
                txt.grid(row=F._next(None), column=0, columnspan=3,
                         sticky="ew")
                txt.insert("1.0", p.text)
                vals["text"] = txt
            for i, ax in enumerate("XYZ"):
                vals["d" + ax] = tk.StringVar(value=f"{p.offset[i]:g}")
                F.entry(f"Offset {ax}", vals["d" + ax],
                        f"Shift the whole piece along {ax}.", unit="mm")
            vals["rot"] = tk.StringVar(value=f"{p.rotation:g}")
            F.entry("Rotation", vals["rot"], "Turn the piece about Z, around "
                    "its own center.", unit="°")
            mir = {"": "None", "x": "Flip X", "y": "Flip Y"}
            vals["mirror"] = tk.StringVar(value=mir[p.mirror])
            F.combo("Mirror", vals["mirror"], tuple(mir.values()),
                    "Flip the piece about its own center.")
            vals["scale"] = tk.StringVar(value=f"{p.scale:g}")
            F.entry("Scale X/Y", vals["scale"], "Grow or shrink the piece in "
                    "X and Y. This also changes the fiber spacing.")
            lay = p.layers or (None, None)
            vals["l1"] = tk.StringVar(value=str(lay[0] or ""))
            vals["l2"] = tk.StringVar(value=str(lay[1] or ""))
            F.entry("Layers from", vals["l1"], "The first layer to include "
                    "(empty: all of them).")
            F.entry("Layers to", vals["l2"], "The last layer to include.")
            vals["drop"] = tk.BooleanVar(value=bool(p.drop_frame))
            if p.source != "designer":
                F.check("Drop this piece's own header and footer",
                        vals["drop"], "Leave out the lines before its first "
                        "move and after its last, so they don't fight the "
                        "main job.")
            vals["lift"] = tk.StringVar(value=f"{p.lift:g}")
            F.entry("Lift for the join", vals["lift"], "How high the travel "
                    "into this piece lifts; 0 is a straight travel. In MEW the "
                    "join lays fiber.", unit="mm")

        def ok(vals):
            try:
                off = tuple(self._gce_num(vals, "d" + a, "Offset " + a)
                            for a in "XYZ")
                rot = self._gce_num(vals, "rot", "Rotation")
                scale = self._gce_num(vals, "scale", "Scale X/Y")
                l1 = self._gce_num(vals, "l1", "Layers from",
                                   required=False, cast=int)
                l2 = self._gce_num(vals, "l2", "Layers to", required=False,
                                   cast=int)
                lift = self._gce_num(vals, "lift", "Lift for the join")
            except ValueError as exc:
                return str(exc)
            if scale <= 0:
                return "Scale must be more than 0."
            if "text" in vals and not vals["text"].get(
                    "1.0", "end-1c").strip():
                return "Write or paste the snippet's g-code."
            self._gce_record_edits()
            before = m.snapshot()
            p.name = vals["name"].get().strip() or p.name
            if "text" in vals:
                p.text = vals["text"].get("1.0", "end-1c")
            p.offset, p.rotation, p.scale, p.lift = off, rot, scale, lift
            p.mirror = {"None": "", "Flip X": "x",
                        "Flip Y": "y"}[vals["mirror"].get()]
            p.layers = (l1, l2) if (l1 or l2) else None
            p.drop_frame = bool(vals["drop"].get())
            if piece is None:
                m.add_piece(p)
            self._gce_rerender(before)
            return None
        extra = ()
        if piece is not None and piece in m.pieces:
            i = m.pieces.index(piece)

            def act(fn):
                return lambda: (self._gce_host_close(), fn())
            extra = [("Earlier", act(lambda: self._gce_move_piece(i, -1))),
                     ("Later", act(lambda: self._gce_move_piece(i, 1))),
                     ("Turn off" if piece.on else "Turn on",
                      act(lambda: self._gce_piece_set(piece, "on",
                                                      not piece.on)))]
            if piece.source != "designer":
                extra.append(("Remove",
                              act(lambda: self._gce_remove_piece(i))))
        self._gce_form_open("Stitched piece" if piece is not None else
                            "Stitch g-code", build, ok,
                            ok_text="Apply" if piece is not None else "Add",
                            extra=extra)

    def _gce_status(self, text):
        st = getattr(self, "status", None)
        if st is not None:
            st.set(text)

    # ------------------------------------------------------------------ #
    # Problems, What changed
    # ------------------------------------------------------------------ #
    #: the firmware-warnings bar (`mew_gcode.problems`) is held back from
    #: this version of the page; the engine and this code stay.
    _GCE_SHOW_PROBLEMS = False

    def _gce_refresh_problems(self):
        bar = getattr(self, "_gce_problems", None)
        eng = self._gce_engine()
        m = self._gce_model_now()
        if bar is None or eng is None:
            return
        if not self._GCE_SHOW_PROBLEMS:
            bar.grid_remove()
            return
        found = []
        if m is not None and self.gce.get_text():
            try:
                found = eng.problems(m, plate=self._gce_plate_box())
            except Exception:  # noqa: BLE001
                found = []
        self._gce_found = list(found)
        self._gce_problem_i = 0
        self._gce_show_problem()

    def _gce_plate_box(self):
        """(xmin, xmax, ymin, ymax) of a rectangular plate in g-code mm,
        measured from G-code zero, whichever point of the plate that is
        (`_origin_datum`). A round plate is not judged (None). The shape
        test read "Rectangle", a value `plate_shape` never holds, so no
        plate was ever judged."""
        try:
            if self.plate_shape.get() != "rectangular":
                return None
            fp = self._plate_footprint_xy()
            if fp is None:
                return None
            px, py, _pz = self._origin_datum()
            return (fp[0] - px, fp[1] - px, fp[2] - py, fp[3] - py)
        except (AttributeError, ValueError, tk.TclError):
            return None

    def _gce_show_problem(self):
        bar = self._gce_problems
        found = getattr(self, "_gce_found", [])
        for w in getattr(self, "_gce_problem_btns", []):
            w.destroy()
        self._gce_problem_btns = []
        if not found:
            bar.grid_remove()
            self.gce_problem_var.set("")
            return
        i = self._gce_problem_i % len(found)
        p = found[i]
        where = f"Line {p.line + 1}: " if p.line is not None else ""
        more = f"   ({i + 1} of {len(found)})" if len(found) > 1 else ""
        self.gce_problem_var.set(where + p.message + more)

        def btn(text, cmd, tip):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=(8, 0))
            add_tip(b, tip)
            self._gce_problem_btns.append(b)
        if p.fix:
            btn("Fix", lambda: self._gce_apply_fix(p),
                "Apply the safe fix. It becomes a change you can untick.")
        if p.line is not None:
            btn("Go to line", lambda: self.gce.goto_line(p.line + 1),
                "Put the caret on that line.")
        if len(found) > 1:
            btn("Next", self._gce_next_problem, "The next finding.")
        bar.grid(row=4, column=0, sticky="ew")

    def _gce_apply_fix(self, p):
        m = self._gce_model_now()
        if m is None:
            return
        before = self._gce_snap()
        self._gce_engine().apply_fix(m, p)
        self._gce_rerender(before)

    def _gce_next_problem(self):
        self._gce_problem_i += 1
        self._gce_show_problem()

    def _gce_toggle_whatchanged(self):
        """What changed: the designer's plain output beside this text, in
        place of the editor (spec 7.5). Pressing again closes it."""
        pane = getattr(self, "_gce_wc", None)
        if pane is not None:
            pane.destroy()
            self._gce_wc = None
            self.gce.grid()
            self._gce_wc_btn.set_on(False)
            if getattr(self, "_gce_wc_showed_code", False):
                self._gce_wc_showed_code = False
                self._gce_toggle_code(False)
            return
        # the comparison takes the text's place in the G-code panel: with the
        # panel hidden, Compare lit up and showed nothing. Open it, and close
        # it again with the comparison.
        self._gce_wc_showed_code = False
        if hasattr(self, "_gce_code_shown") and not self._gce_code_shown:
            self._gce_toggle_code(True)
            self._gce_wc_showed_code = True
        self._gce_record_edits()
        doc = self._gce_doc
        m = self._gce_model_now()
        left = (m.base_text if m is not None else doc.get("raw")) or ""
        right = self.gce.get_text()
        mid = self.gce.master
        pane = ttk.Frame(mid)
        pane.grid(row=1, column=0, sticky="nsew")
        self.gce.grid_remove()
        self._gce_wc = pane
        self._gce_wc_btn.set_on(True)
        pane.columnconfigure(0, weight=1, uniform="wc")
        pane.columnconfigure(1, weight=1, uniform="wc")
        pane.rowconfigure(1, weight=1)
        lt = ("This design's plain output"
              if self._gce_from_designer() else "The file as opened")
        # the column titles wrap to their column: the G-code panel is narrow,
        # and the right one was cut off mid-word
        titles = [ttk.Label(pane, text=lt, style="Hint.TLabel",
                            justify="left", wraplength=120),
                  ttk.Label(pane, text="This text  (← what changed each line)",
                            style="Hint.TLabel", justify="left",
                            wraplength=120)]
        for col, lab in enumerate(titles):
            lab.grid(row=0, column=col, sticky="nw")

        def wrap(_e=None):
            try:
                w = (pane.winfo_width() - 24) // 2 - 6
            except tk.TclError:
                return
            if w > 40:
                for lab in titles:
                    lab.configure(wraplength=w)
        pane.bind("<Configure>", lambda _e: pane.after_idle(wrap), add="+")
        texts = []
        from gui_gcode_editor import code_palette
        pal = code_palette(self)        # the editor's own, for the theme
        for col in (0, 1):
            t = tk.Text(pane, wrap="none", font=self.gce._font,
                        background=pal["bg"], foreground=pal["fg"],
                        selectbackground=pal["select"],
                        selectforeground=pal["fg"],
                        borderwidth=0, highlightthickness=0)
            t.grid(row=1, column=col, sticky="nsew", padx=(0, 4))
            for tag in ("del", "add", "chg"):
                t.tag_configure(tag, background=pal[tag])
            t.tag_configure("why", foreground=pal["mark"])
            texts.append(t)
        sb = ttk.Scrollbar(pane, orient="vertical")
        sb.grid(row=1, column=2, sticky="ns")

        def yview(*a):
            for t in texts:
                t.yview(*a)
        sb.configure(command=yview)

        def follow(src):
            def f(f0, f1):
                sb.set(f0, f1)
                for o in texts:
                    if o is not src:
                        o.yview_moveto(f0)
            return f
        for t in texts:
            t.configure(yscrollcommand=follow(t))
        close = ttk.Button(pane, text="Back to the text",
                           command=self._gce_toggle_whatchanged)
        close.grid(row=2, column=0, sticky="w", pady=(4, 0))
        a, b = left.split("\n"), right.split("\n")
        if len(a) + len(b) > 240000:
            texts[0].insert("1.0", "This text is too large to compare here.")
            return
        why = self._gce_line_reasons()
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        n_changed = 0
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                texts[0].insert("end", "\n".join(a[i1:i2]) + "\n")
                texts[1].insert("end", "\n".join(b[j1:j2]) + "\n")
                continue
            n_changed += 1
            for k in range(max(i2 - i1, j2 - j1)):
                la = a[i1 + k] if i1 + k < i2 else ""
                texts[0].insert("end", la + "\n",
                                "del" if i1 + k < i2 else "")
                if j1 + k < j2:
                    texts[1].insert("end", b[j1 + k],
                                    "add" if op == "insert" else "chg")
                    if why.get(j1 + k):
                        texts[1].insert("end", "    ← " + why[j1 + k], "why")
                texts[1].insert("end", "\n")
        if n_changed == 0:
            texts[1].insert("1.0", "(No differences.)\n", "why")
            texts[0].insert("1.0", "\n")    # the two columns stay row for row
        for t in texts:
            t.configure(state="disabled")
        close.focus_set()
        pane.bind("<Escape>", lambda _e: self._gce_toggle_whatchanged())

    def _gce_line_reasons(self):
        """{0-based line of this text: what made it}, for What changed."""
        m = self._gce_model_now()
        out = {}
        if m is None:
            return out
        try:
            r = m.render()
        except Exception:  # noqa: BLE001
            return out
        labels = dict((c.id, lab) for c, lab in m.chips())
        for p in m.pieces:
            labels[p.id] = "piece: " + (p.name or p.source)
        for i, rl in enumerate(r.lines):
            if rl.origin == "base":
                continue
            if rl.origin == "option":
                out[i] = ("output option: wait for temperature"
                          if rl.chip == "opt:wait_temp"
                          else "output option: custom line")
            elif rl.chip in labels:
                out[i] = labels[rl.chip]
            else:
                out[i] = {"join": "the travel joining two pieces",
                          "flavor": "the flavor's header or footer"}.get(
                    rl.origin, "an output option")
        return out

    # ------------------------------------------------------------------ #
    # Generate and Save outside the page carry the document
    # ------------------------------------------------------------------ #
    def _gce_doc_active(self):
        """A linked document with changes that act on the g-code: the
        designer's Generate carries it."""
        m = self._gce_model_now()
        machine_active = False
        if m is not None and self._gce_doc["source"] == self._GCE_LINKED:
            eng = self._gce_engine()
            context = self._machine_gcode_context(use_gce=True)
            flavor = self.gce_flavor_name()
            blocks = "\n".join(self._machine_blocks())
            commands = eng.strip_export_comments(
                eng.render_machine_block(blocks, context, flavor), flavor)
            machine_active = bool(self._machine_recipes() or commands.strip())
        return (m is not None and self._gce_doc["source"] == self._GCE_LINKED
                and (machine_active or any(c.on for c in m.changes)
                     or any(p.on and p.source != "designer"
                            for p in m.pieces)))

    def _build_args_scaffold(self):
        """The designer's Generate argv, plus `--gcode-doc` when this page's
        document has changes: the worker rebases the document on the g-code
        it makes and writes the parse form, so the STL, .depo and player see
        the changes (never added to the page's own regeneration)."""
        argv = super()._build_args_scaffold()
        # only the page's own Generate carries its edits: the designer's
        # Generate writes the designer's design, untouched
        if getattr(self, "_gce_regen_building", False):
            return argv  # regeneration must not forget a completed Generate
        if not getattr(self, "_gce_in_page_scope", False) \
                or "-o" not in argv:
            self._gce_gen_doc = None
            return argv
        # record pending typing BEFORE asking whether there are changes: a
        # typed line nothing has recorded yet is a change too
        self._gce_record_edits()
        if not self._gce_doc_active():
            self._gce_gen_doc = None
            return argv
        self._gce_model_sync()
        path = self._gce_scratch("generate.doc.json")
        try:
            self._gce_model_sync().save(path, with_base=False)
        except OSError:
            return argv
        self._gce_gen_doc = path
        return list(argv) + ["--gcode-doc", path]

    def save_gcode_as(self):
        """Off the page, a Generate that carried the document saves the
        document's export text (the file on disk is its parse form). On the
        page, or for such a document, Save first checks the flavor can write
        every command the preview reads (`_gce_gaps_notice`)."""
        if hasattr(self, "gce") and not getattr(self, "_gce_gaps_ok", False) \
                and (self._gce_on_page() or (
                    getattr(self, "_gce_gen_doc", None)
                    and self._gce_doc_active())):
            gaps = self._gce_export_gaps()
            if gaps:
                self._gce_gaps_notice(gaps)
                if not self._gce_on_page():
                    self._gce_status(
                        "Not saved: the G-code page says why.")
                return None
        if hasattr(self, "gce") and not self._gce_on_page() \
                and getattr(self, "_gce_gen_doc", None) \
                and self._gce_doc_active() \
                and getattr(self, "_last_gcode_is_scaffold", False):
            m = self._gce_model_sync()
            try:
                with open(self.last_gcode, encoding="utf-8") as fh:
                    disk = fh.read()
            except (OSError, TypeError):
                disk = None
            if disk is not None and disk == m.parse_text():
                text = m.render().text
                if hasattr(self, "_gce_save_ready"):
                    text = self._gce_save_ready(text)
                return self._gce_save_text(text)
            if disk is not None:
                self._gce_status(
                    "This g-code was made from an older design than the "
                    "G-code page shows. Save it from the G-code page.")
                return
        return super().save_gcode_as()

    def _gcode_save_text(self, filename=None):
        """Off the page, a Generate that carried the document is saved as
        the document's export text (`save_gcode_as`): the same text here,
        for the Machine link's Upload and History's kept copy. None when the
        file on disk is from an older design (Upload's own staleness check
        says so first)."""
        if hasattr(self, "gce") and getattr(self, "_gce_gen_doc", None) \
                and self._gce_doc_active() \
                and getattr(self, "_last_gcode_is_scaffold", False):
            m = self._gce_model_sync()
            try:
                with open(self.last_gcode, encoding="utf-8") as fh:
                    disk = fh.read()
            except (OSError, TypeError):
                return None
            if disk != m.parse_text():
                return None
            m.options.context["filename"] = (
                filename or os.path.basename(self.last_gcode))
            return self._gce_save_ready(m.export_text())
        # A plain Design Generate carries no document (`_gce_gen_doc` is
        # unset), so it fell to the seam below. That seam fills the header
        # from the legacy card designer's values: Send from the Printer link
        # uploaded "Layers: 10, Speed 800" over a 7-layer print at 900 mm/min.
        # When the file on disk IS this page's linked design, write it the way
        # Save on this page does, header from the page's own design.
        if hasattr(self, "gce") and getattr(self, "_last_gcode_is_scaffold", False) \
                and not getattr(self, "_last_gcode_is_calibration", False) \
                and self._gce_from_designer():
            m = self._gce_model_now()
            if m is not None:
                try:
                    with open(self.last_gcode, encoding="utf-8") as fh:
                        disk = fh.read()
                except (OSError, TypeError):
                    disk = None
                if disk is not None and disk == m.parse_text():
                    m.options.context["filename"] = (
                        filename or os.path.basename(self.last_gcode))
                    return self._gce_save_ready(m.export_text())
        return super()._gcode_save_text(filename)

    def _gce_save_text(self, text):
        if not self._preflight_ok(text, flavor=self.gce_flavor_name()):
            return
        path = filedialog.asksaveasfilename(
            title="Save g-code as", defaultextension=".gcode",
            initialfile="scaffold.gcode",
            filetypes=[("G-code", "*.gcode"), ("All files", "*.*")])
        if not path:
            return
        m = self._gce_model_sync()
        if m is not None:
            m.options.context["filename"] = os.path.basename(path)
            text = self._gce_save_ready(m.export_text())
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        except OSError as exc:
            self._gce_status(f"The file could not be saved: {exc}")
            return
        self._gce_status(f"Saved {os.path.basename(path)}.")

    # ------------------------------------------------------------------ #
    # the Export card's badge (spec 4.4: nothing reaches an export unseen)
    # ------------------------------------------------------------------ #
    def _gce_build_badge(self, parent, row):
        """Under the Export card's rows: how many g-code changes ride this
        design's export, with the way to them. Takes space only when there
        are some."""
        self.gce_badge_var = tk.StringVar(value="")
        f = ttk.Frame(parent)
        self._gce_badge = f
        self._gce_badge_row = dict(row=row, column=0, columnspan=4,
                                   sticky="w", pady=(6, 0))
        gui_form.mark(ttk.Label(f, textvariable=self.gce_badge_var,
                                wraplength=360, justify="left"),
                      "live").pack(side="left")
        b = ttk.Button(f, text="Open the G-code page",
                       command=lambda: self._nav_select("gcode"))
        b.pack(side="left", padx=(8, 0))
        add_tip(b, "See every change, tinted in the text, and untick any.")
        return f

    def _gce_badge_count(self):
        """The G-code page's changes riding the export. Not the output
        options: they are set on the Export card the badge sits on."""
        n = 0
        m = self._gce_model_now()
        if m is not None and self._gce_doc["source"] == self._GCE_LINKED:
            n += sum(1 for c in m.changes if c.on)
            n += sum(1 for p in m.pieces if p.on and p.source != "designer")
        return n

    def _gce_refresh_badge(self):
        f = getattr(self, "_gce_badge", None)
        if f is None:
            return
        n = self._gce_badge_count()
        if not n:
            f.grid_remove()
            return
        self.gce_badge_var.set(f"{n} g-code change{'s' if n != 1 else ''} "
                               "active in this design's export.")
        f.grid(**self._gce_badge_row)

    def _gce_render_changes(self):
        """Everything that shows the edits: the tree (first: the library
        says where each edit is from it), the library, the badge. An open
        form whose edit is gone closes."""
        super()._gce_render_changes()
        fs = getattr(self, "_gce_fs", None)
        m = self._gce_model_now()
        if fs is not None and (m is None or fs["cid"] not in
                               {c.id for c in m.changes}):
            self._gce_edit_lost()
        self._gce_refresh_anatomy()
        self._gce_render_library()
        self._gce_refresh_badge()
        # the ribbon's greying: the g-code (or the edits) may be new
        self._gce_tools_refresh()

    # ------------------------------------------------------------------ #
    # persistence: the document rides the scaffold preset (gce.document)
    # ------------------------------------------------------------------ #
    def _gce_persist_entries(self):
        """{"gce.document": var}: the document as JSON, in the scaffold
        preset (it belongs to the design). Modern only, like pv.*."""
        if not hasattr(self, "gce_document"):
            self.gce_document = tk.StringVar(value="")
            self.gce_document.trace_add("write", self._gce_persist_written)
        return {"gce.document": self.gce_document}

    def _gce_doc_state(self):
        """The document as JSON ("" when there is nothing to keep). A linked
        document keeps its chips, not its base: the design regenerates the
        base. A detached copy keeps its base too."""
        doc = self._gce_doc
        m = self._gce_model_now()
        if m is None or doc["source"] not in (self._GCE_LINKED, _DETACHED):
            return ""
        flavor = self.gce_flavor.get() if self._gce_flavor_picked else ""
        if not m.changes and not m.pieces and not flavor \
                and doc["source"] == self._GCE_LINKED:
            return ""
        return json.dumps({"v": 1, "flavor": flavor,
                           "doc": m.to_dict()}, separators=(",", ":"))

    def _gce_sync_persist(self):
        if not hasattr(self, "gce_document") \
                or getattr(self, "_gce_restoring", False):
            return
        value = self._gce_doc_state()
        if value != self.gce_document.get():
            self._gce_writing = True
            try:
                self.gce_document.set(value)
            finally:
                self._gce_writing = False

    def _gce_persist_written(self, *_a):
        """A preset (or the session) set the document: it is put back on
        the design's next regeneration (a detached copy, at once)."""
        if getattr(self, "_gce_writing", False) or not hasattr(self, "gce"):
            return
        if getattr(self, "_cal_stash", None) is not None:
            # Calibrate's print is on the page and the Design's document is
            # parked (`_cal_load_doc`): this is the Design's, so it waits for
            # Design (`_cal_unload_doc`). Taken now, it threw away the
            # calibration print, and the Design kept its old text (an opened
            # file stayed open under a preset or a reproduced run). The value
            # is held, not the var: Design writes its own state into the var
            # as it comes back, before this is applied.
            self._cal_persist_later = self.gce_document.get()
            return
        raw = self.gce_document.get()
        try:
            state = json.loads(raw) if raw else {}
        except ValueError:
            state = {}
        self._gce_restore = state
        self._gce_model = None
        if self._gce_doc["source"] == _DETACHED:
            self._gce_doc["source"] = self._GCE_LINKED
        elif self._gce_doc["source"] == self._GCE_FILE:
            # A preset loaded over an opened file: the page is the preset's
            # design now (the load asked before replacing the file), so its
            # record says so before the design's text arrives, as Back to
            # this design does (`_gce_relink`). Left on the file, the text
            # arrived with no document built for it: no tree rows, a play
            # bar of one layer.
            self._gce_doc.update(source=self._GCE_LINKED, sig=None, raw="",
                                 path=None)
            self._gce_flavor_reset()
            self._gce_set_link_label()
        if state.get("doc", {}).get("linked") is False \
                and state["doc"].get("base_text"):
            self._gce_restore_pending()
        elif self._gce_page_shown():
            self._gce_doc["sig"] = None
            self.root.after_idle(self._gce_check_design)

    def _gce_restore_pending(self):
        state = getattr(self, "_gce_restore", None)
        if state is None:
            return
        self._gce_restore = None
        eng = self._gce_engine()
        self._gce_restoring = True
        try:
            if state.get("flavor"):
                self.gce_flavor.set(state["flavor"])
                self._gce_flavor_picked = True
            elif self._gce_flavor_picked:
                self._gce_flavor_reset()
            d = state.get("doc")
            m = self._gce_model_now()
            if d:
                try:
                    if d.get("linked", True) and m is not None:
                        self._gce_model = eng.Document.from_dict(
                            d, base_text=m.base_text, line_map=m.line_map)
                    elif not d.get("linked", True):
                        self._gce_model = eng.Document.from_dict(d)
                        self._gce_stop_regen()
                        self._gce_doc.update(
                            source=_DETACHED, raw=self._gce_model.base_text,
                            sig=None, path=None, mode="g1")
                except Exception as exc:  # noqa: BLE001
                    self._gce_notice_show(
                        f"This preset's g-code changes could not be read "
                        f"({exc}).", [("OK", self._gce_notice_hide)],
                        kind="restore")
            if self._gce_model_now() is not None:
                r = self._gce_render()
                self._gce_doc["base"] = r.text
                self.gce.set_text(r.text, undoable=False)
        finally:
            self._gce_restoring = False
        self._gce_set_link_label()
        self._gce_render_changes()
        self._gce_pv_schedule(now=True)
        self._gce_refresh_controls()
        self._gce_refresh_problems()
