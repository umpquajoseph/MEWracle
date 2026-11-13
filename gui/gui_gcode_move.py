#!/usr/bin/env python3
"""gui_gcode_move.py -- MODERN-UI ONLY: the G-code page's Move tool.

Fusion's Move, on the parts of a print. Select scaffolds and layers -- in the
tree, or in the picture (click a fiber: its layer; double-click: its
scaffold; Ctrl- or Shift-click adds or takes away) -- then Move (the ribbon's
Change selection group, or M). Two things appear:

  * the BOX -- the page's one edit form, in its floating box, opened on an
    offset edit (`mew_gcode` op "offset": X / Y / Z in mm, Rotate about Z in
    degrees, the turn's Pivot X / Y): the exact distances, typed;
  * the GIMBAL -- `move_gimbal.MoveGimbal` in the picture, at the pivot:
    drag an arrow along X, Y or Z, a square in the XY, YZ or ZX plane, the
    ball in the screen's plane, or the blue ring to turn about Z. The box's
    numbers follow the drag.

THE PIVOT is the middle of what is moving (`mew_gcode.motion_xy_centre`, the
engine's own default), written into the box when the move opens or is
retargeted while it has not turned yet, so the turn the engine makes and the
one the picture shows are about the same point. The gimbal sits at the pivot
as moved: pivot + (X, Y).

THE MOVE IS AN EDIT like any other: one `Transform(op="offset")` whose scope
is exactly the selection (`_gce_sel_scope`, a "set" for several parts), made
by `_gce_tool_change`, so Done keeps it as one undo step, Cancel puts it all
back, and Generate / Save / the STL see it as they see every edit. A move of
several parts sits in the tree under EACH part it moves
(`_gce_anatomy_rows`), the way a hand edit sits under its layer.

THE PICTURE WAITS FOR DONE (`_gce_pv_waiting`: an open form never rebuilds
it), so while the box is open what moves on screen is the HIGHLIGHT: the
selected parts lit in yellow, drawn shifted by how far the move has gone
since the picture was built (`PreviewPanel.set_highlight_offset`). Done
rebuilds the picture once, with the parts in their new place.

MAKE A COPY (a check in the box): Done leaves the selection where it was and
prints the moved selection AGAIN, as a new scaffold after everything else --
a stitched piece (`mew_gcode.Piece`, source "snippet") holding the moved
lines as they stood at Done, its own from then on (a later design change
does not reach it). Copied layers are re-stacked from the collector up: the
first sits where its scaffold's first layer sits, each next one a layer step
above (its own step in its own scaffold); the box's Z adds on top. While the
box is open the picture already looks like a copy -- the originals stay
drawn, the ghost is where the copy goes.

The gimbal's axes are the picture's. On a flat print and on a 4-axis tube
they are the g-code's X, Y and Z: on a tube, X runs along the mandrel. A
tube's moves carry no Y and its Z is a ply's height above the rod, so there
Y and Z write those words as the box says, and the picture's ghost cannot
show them on the wrapped tube; X along the rod is exact. A tube has no turn
about Z, so there the ring is not drawn.
"""

import hashlib
import math

import numpy as np

import tkinter as tk


def _key(text):
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()


def _fmt_mm(v):
    s = f"{float(v):.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


class _GcodeMoveMixin:
    """Sits before `_GcodeChangesMixin` in `ModernApp`'s MRO: it overrides
    the form's after-apply / finish hooks and the highlight, each chaining
    to super()."""

    _GCE_MOVE_FIELDS = ("dx", "dy", "dz")

    # -- the tool -------------------------------------------------------- #
    def _gce_tool_move(self):
        """Move: an offset edit on exactly the selection, its box open and
        the gimbal on the picture."""
        if self._gce_mv_vars() is not None:
            fs = self._gce_fs
            self._gce_focus_edit(fs["cid"], see=True)
            return
        self._gce_tool_change("offset")

    def _gce_mv_field(self, name):
        """The open move's var for `name` (an offset field), or None."""
        if self._gce_mv_vars() is None:
            return None
        return self._gce_fs["vals"]["f"]["t:offset"].get(name)

    @staticmethod
    def _gce_mv_num(v, blank=0.0):
        try:
            raw = str(v.get()).strip() if v is not None else ""
            return float(raw) if raw else blank
        except (ValueError, tk.TclError):
            return blank

    def _gce_mv_turn(self):
        """The box's turn about Z, degrees (0 mid-typing)."""
        return self._gce_mv_num(self._gce_mv_field("rz"))

    def _gce_mv_pivot(self):
        """The box's pivot (x, y) in mm, or None while either is blank."""
        x = self._gce_mv_num(self._gce_mv_field("cx"), None)
        y = self._gce_mv_num(self._gce_mv_field("cy"), None)
        return None if x is None or y is None else (x, y)

    def _gce_mv_quiet(self, pairs):
        """Write {var: text} without the form applying (it applies with the
        next change, or Done)."""
        fs = self._gce_fs
        was, fs["loading"] = fs["loading"], True
        try:
            for v, t in pairs:
                if v is not None and v.get() != t:
                    v.set(t)
        finally:
            fs["loading"] = was

    def _gce_mv_centre(self):
        """Fill a blank pivot with the middle of what is moving, as the
        engine would take it: the text's moves, less the move's own shift
        (the text already carries it; with no turn yet that is all)."""
        if self._gce_mv_pivot() is not None or self._gce_mv_turn() % 360.0:
            return
        if self._gce_fs.get("after") is not None:
            return          # an apply is pending: the scope is not yet it
        lines = self._gce_mv_lines()
        if not lines:
            return
        c = self._gce_engine().motion_xy_centre(
            self.gce.get_text().split("\n"), {i - 1 for i in lines})
        if c is None:
            return
        # the shift the text carries is the edit's as last applied; the
        # box's may be ahead of it (a held move waits for Done)
        try:
            p = self._gce_model_now().get(self._gce_fs["cid"]).params or {}
            dx, dy = float(p.get("dx") or 0.0), float(p.get("dy") or 0.0)
        except (KeyError, AttributeError, TypeError, ValueError):
            dx, dy, _dz = self._gce_mv_now()
        self._gce_mv_quiet(((self._gce_mv_field("cx"), _fmt_mm(c[0] - dx)),
                            (self._gce_mv_field("cy"), _fmt_mm(c[1] - dy))))

    def _gce_mv_vars(self):
        """The open box's X / Y / Z vars when it is a move (an offset
        change), else None."""
        fs = getattr(self, "_gce_fs", None)
        if fs is None or fs.get("line"):
            return None
        try:
            if self._gce_edit_key() != "t:offset":
                return None
            f = fs["vals"]["f"]["t:offset"]
            return tuple(f[k] for k in self._GCE_MOVE_FIELDS)
        except (KeyError, AttributeError, tk.TclError):
            return None

    def _gce_mv_now(self):
        """The box's (dx, dy, dz) in mm; a field mid-typing reads as 0."""
        vs = self._gce_mv_vars()
        out = []
        for v in vs or ():
            try:
                out.append(float(str(v.get()).strip() or 0.0))
            except (ValueError, tk.TclError):
                out.append(0.0)
        return tuple(out) if len(out) == 3 else (0.0, 0.0, 0.0)

    # -- the numbers wait for Done ---------------------------------------- #
    def _gce_mv_holds(self):
        """True when the open box is a move and what it now says differs
        from its edit in the numbers alone (X / Y / Z, the turn, its
        pivot): an apply that would only move the same lines."""
        fs = getattr(self, "_gce_fs", None)
        if fs is None or fs.get("mv_force") or self._gce_mv_vars() is None:
            return False
        m = self._gce_model_now()
        try:
            c = m.get(fs["cid"])
            new = self._gce_edit_build(c)
        except (KeyError, AttributeError, ValueError):
            return False            # the apply says what is wrong
        a, b = new.to_dict(), c.to_dict()
        pa, pb = a.pop("params", None), b.pop("params", None)
        if a != b:
            return False
        fs["mv_held"] = pa != pb
        return True

    def _gce_edit_apply(self):
        """While a move is open its numbers move the yellow ghost and the
        gimbal only (`_gce_hl_refresh`): the text, the marks and the tree
        take them once, at Done, or whenever something reads the document
        (`_gce_record_edits`, `_gce_edit_flush`). An apply per gimbal
        release re-rendered the whole file: 16 s on a 20x20 mm waved
        scaffold, 27 s at 40x40. The text the apply writes is the same
        whenever it runs."""
        fs = getattr(self, "_gce_fs", None)
        if fs is not None and self._gce_mv_holds():
            fs["after"] = None
            ba = fs["vals"].get("ba")
            if fs.get("mv_held") and ba is not None:
                ba.set("The g-code takes the move at Done.")
            self._gce_hl_schedule()
            return
        super()._gce_edit_apply()

    def _gce_mv_apply_held(self):
        """Apply a held move now (the form's own apply, unheld)."""
        fs = self._gce_fs
        if fs["after"] is not None:
            try:
                self.root.after_cancel(fs["after"])
            except (ValueError, tk.TclError):
                pass
            fs["after"] = None
        fs["mv_held"] = False
        fs["mv_force"] = True
        try:
            super()._gce_edit_apply()
        finally:
            fs["mv_force"] = False

    def _gce_edit_flush(self):
        """Apply now, a held move too (Save to library, Make a copy)."""
        super()._gce_edit_flush()
        fs = getattr(self, "_gce_fs", None)
        if fs is not None and fs.get("mv_held"):
            self._gce_mv_apply_held()

    def _gce_record_edits(self):
        """Everything that reads the document (Done, Save, Generate, a
        snapshot, a copy) brings it up to date here first: a held move
        is applied with it."""
        fs = getattr(self, "_gce_fs", None)
        if fs is not None and fs.get("mv_held") and not fs.get("mv_force"):
            self._gce_mv_apply_held()       # records the typing first
            return
        super()._gce_record_edits()

    # -- the form's hooks -------------------------------------------------- #
    def _gce_edit_after(self):
        if (getattr(self, "_gce_fs", None) or {}).get("mv_closing"):
            # Done's own apply: the box is closing, so its lines, labels and
            # gimbal are not drawn again (the label alone rendered the whole
            # file with the move off: 1.3 s at 20x20 mm)
            return
        super()._gce_edit_after()
        self._gce_mv_sync(centre=True)

    def _gce_render_changes(self):
        if (getattr(self, "_gce_fs", None) or {}).get("mv_closing"):
            return      # Done's apply: the close below refreshes them once
        super()._gce_render_changes()

    def _gce_edit_vis(self):
        super()._gce_edit_vis()
        # the box switched to or from Offset (its Type / What combos);
        # a retarget calls this BEFORE it applies the new scope, so the
        # pivot waits for the apply
        if getattr(self, "_gce_fs", None) is not None \
                and not self._gce_fs.get("loading"):
            self._gce_mv_sync()

    def _gce_edit_finish(self, keep=True, remove=False):
        made = None
        if keep and not remove and self._gce_mv_copy_on():
            made = self._gce_mv_copy()
        if made is None:
            fs = getattr(self, "_gce_fs", None)
            if keep and not remove and fs is not None \
                    and self._gce_mv_vars() is not None:
                fs["mv_closing"] = True
            super()._gce_edit_finish(keep=keep, remove=remove)
        else:
            # the move edit goes; the copy stays -- one undo step, the form's
            super()._gce_edit_finish(keep=True, remove=True)
            self._gce_status(f"{made.name.capitalize()}: a copy, printed "
                             "after everything else.")
        self._gce_mv_end()

    # -- make a copy ------------------------------------------------------- #
    def _gce_kind_rows(self, F, vals, key, fields, pre):
        super()._gce_kind_rows(F, vals, key, fields, pre)
        if key != "t:offset":
            return
        v = tk.BooleanVar(value=False)
        vals["mv_copy"] = v
        vals["rows"][key].append(F.check(
            "Make a copy", v,
            "Leave the selection where it is and print a moved copy of it "
            "as a new scaffold, after everything else. Copied layers stack "
            "from the collector up. The copy is its own: later design "
            "changes do not reach it."))

    def _gce_mv_copy_on(self):
        if self._gce_mv_vars() is None:
            return False
        v = self._gce_fs["vals"].get("mv_copy")
        try:
            return bool(v is not None and v.get())
        except tk.TclError:
            return False

    def _gce_mv_copy(self):
        """Done with Make a copy: the moved lines, re-stacked, become a new
        piece; the text then shows it (the move edit is still in, the
        caller's finish takes it out). The piece, or None when nothing
        moves (then Done is a plain Done)."""
        self._gce_record_edits()
        self._gce_edit_flush()
        m = self._gce_model_now()
        lines = self._gce_mv_lines()
        if m is None or not lines:
            return None
        eng = self._gce_engine()
        text = self.gce.get_text().split("\n")
        body, starts = self._gce_mv_restack(text, lines, eng)
        if not any(t.strip() for t in body):
            return None
        snippet, _notes = eng.normalise_piece_text("\n".join(body),
                                                   m.flavor)
        if len(snippet.splitlines()) != len(body):
            starts = None           # lines dropped: guess the layers
        n = sum(1 for nf in self._gce_tree_info.values()
                if nf.get("kind") == "group")
        piece = eng.Piece(source="snippet", name="scaffold %d · copy"
                          % (n + 1), text=snippet, flavor=m.flavor,
                          layer_starts=starts or None)
        m.add_piece(piece)
        r = self._gce_render()
        self._gce_doc["base"] = r.text
        if self.gce.get_text() != r.text:
            self.gce.set_text(r.text, undoable=True)
        return piece

    def _gce_mv_restack(self, text, lines, eng):
        """The moved `lines` (1-based, of `text`), each copied layer's Z
        words shifted so the copies stack from the collector up, and where
        in them each copied layer starts (0-based)."""
        dz = self._gce_mv_now()[2]
        info = self._gce_tree_info
        rows = sorted((nf for nf in info.values()
                       if nf.get("kind") == "layer" and nf.get("first")),
                      key=lambda nf: nf["first"])
        chosen = set(lines)
        copied = [nf for nf in rows if nf["first"] in chosen]

        def z_of(nf):
            for i in range(nf["first"], nf["last"] + 1):
                _c, w = eng.parse_words(text[i - 1])
                if "Z" in w:
                    try:
                        z = float(w["Z"])
                    except ValueError:
                        continue
                    # a moved layer's Z already carries the move's dz
                    return z - dz if nf["first"] in chosen else z
            return None

        shift = {}
        top = None
        for nf in copied:
            sib = [r for r in rows if r["key"][:2] == nf["key"][:2]]
            i = sib.index(nf)
            z = z_of(nf)
            if z is None:
                continue
            if top is None:
                new = z_of(sib[0])
            else:
                below = z_of(sib[i - 1]) if i else None
                nxt = z_of(sib[1]) if len(sib) > 1 else None
                step = (z - below if below is not None else
                        nxt - z_of(sib[0]) if nxt is not None else 0.0)
                new = top + step
            new = z if new is None else new
            shift[nf["first"]] = new - z
            top = new
        # each line takes its layer's shift; a line outside every copied
        # layer (a scaffold's lead-in) the next layer's, else the last
        spans = [(nf["first"], nf["last"], shift.get(nf["first"], 0.0))
                 for nf in copied]
        out, starts = [], []
        heads = {a for a, _b, _s in spans}
        for ln in sorted(chosen):
            if not 1 <= ln <= len(text):
                continue
            t = text[ln - 1]
            if ln in heads:
                starts.append(len(out))
            s = next((sh for a, b, sh in spans if a <= ln <= b), None)
            if s is None:
                s = next((sh for a, _b, sh in spans if a > ln),
                         spans[-1][2] if spans else 0.0)
            _c, w = eng.parse_words(t)
            if s and "Z" in w:
                try:
                    t = eng._set_words(t, {"Z": float(w["Z"]) + s})
                except ValueError:
                    pass
            out.append(t)
        return out, starts

    def _gce_mv_end(self):
        panel = getattr(self, "_gce_pv_panel", None)
        self._gce_mv_seen = {}
        self._gce_mv_geom = None
        self._gce_mv_drag0 = None
        if panel is not None:
            panel.show_gimbal(None)
            panel.set_highlight_offset((0.0, 0.0, 0.0))
            panel.on_gimbal = None

    def _gce_mv_sync(self, centre=False):
        """After any change of the open box: remember which offset the text
        now carries, then light and place (or hide) the gimbal."""
        panel = getattr(self, "_gce_pv_panel", None)
        if panel is None:
            return
        if self._gce_mv_vars() is None:
            if panel.on_gimbal is not None:
                self._gce_mv_end()
            return
        if centre:
            self._gce_mv_centre()
        seen = self.__dict__.setdefault("_gce_mv_seen", {})
        if len(seen) > 64:
            seen.clear()
        # what the text now carries: the shift, the turn and its pivot
        seen[_key(self.gce.get_text())] = (self._gce_mv_now(),
                                           self._gce_mv_turn(),
                                           self._gce_mv_pivot())
        panel.on_gimbal = self._gce_mv_drag
        self._gce_hl_schedule()

    # -- the highlight, shifted ------------------------------------------- #
    def _gce_hl_refresh(self):
        """While a move is open: light the moving parts from the picture as
        it was built, shifted by how far they have moved since, and put the
        gimbal at their middle. The picture's own lines are the moved
        lines' (an offset never adds or removes a line)."""
        if self._gce_mv_vars() is None:
            return super()._gce_hl_refresh()
        self._gce_hl_after = None
        panel = getattr(self, "_gce_pv_panel", None)
        hl = getattr(self, "_gce_hl", None)
        if panel is None:
            return
        if getattr(panel, "_gdrag", None) is not None:
            return                  # the drag owns the ghost
        clock = (hl or {}).get("clock")
        text = self.gce.get_text()
        if clock is None or clock["text"].count("\n") != text.count("\n"):
            panel.set_highlight()
            panel.show_gimbal(None)
            return
        lines = self._gce_mv_lines()
        if not lines:
            panel.set_highlight()
            panel.show_gimbal(None)
            return
        # a move just opened has not applied yet: its pivot is filled here
        self._gce_mv_centre()
        # the same lines of the same picture light the same fiber: a release
        # only moves it (0.5 s a lookup for a whole 20x20 mm scaffold)
        gkey = (hl.get("window"), tuple(lines))
        memo = getattr(self, "_gce_mv_geom", None)
        if memo is not None and memo[0] is hl and memo[1] is clock \
                and memo[2] == gkey:
            polys, dots = memo[3]
        else:
            polys, dots = self._gce_hl_geometry(hl, lines)
            self._gce_mv_geom = (hl, clock, gkey, (polys, dots))
        pts = [np.asarray(p, np.float64).reshape(-1, 3) for p in polys]
        pts += [np.asarray(d, np.float64).reshape(1, 3) for d in dots]
        pts = [p for p in pts if len(p)]
        if not pts:
            panel.set_highlight()
            panel.show_gimbal(None)
            return
        d1, r1, c1 = (self._gce_mv_now(), self._gce_mv_turn(),
                      self._gce_mv_pivot())
        d0, r0, c0 = getattr(self, "_gce_mv_seen", {}).get(
            _key(clock["text"]), (d1, r1, c1))
        # the picture drew p0 = R0 (p - c0) + c0 + d0; the box now asks for
        # R1 (p - c1) + c1 + d1. Drawn turned R1 - R0 about q = c0 + d0
        # and shifted t, it is: t = R1 (c0 - c1) + c1 + d1 - c0 - d0
        if c1 is None:
            c1 = c0
        if c0 is None or not r0 % 360.0:
            c0 = c1
        if c0 is None:
            c0 = c1 = (0.0, 0.0)
        a = math.radians(r1)
        co, si = math.cos(a), math.sin(a)
        ux, uy = c0[0] - c1[0], c0[1] - c1[1]
        t = (co * ux - si * uy + c1[0] + d1[0] - c0[0] - d0[0],
             si * ux + co * uy + c1[1] + d1[1] - c0[1] - d0[1],
             d1[2] - d0[2])
        q = (c0[0] + d0[0], c0[1] + d0[1], 0.0)
        allp = np.concatenate(pts)
        mid = (allp.min(axis=0) + allp.max(axis=0)) / 2.0
        piv = self._gce_mv_pivot()
        at = (mid[0] + t[0], mid[1] + t[1], mid[2] + t[2]) if piv is None \
            else (piv[0] + d1[0], piv[1] + d1[1], mid[2] + t[2])
        panel.set_highlight_offset(t, r1 - r0, q)
        panel.set_highlight(polys, dots)
        panel.show_gimbal(at, ring=not self._gce_pv_is_tube())

    def _gce_mv_lines(self):
        """The lines the open move moves, 1-based: every line of the parts
        its scope names, whether or not it has changed them yet (a move of
        0 changes none, and the gimbal still belongs on them)."""
        fs = getattr(self, "_gce_fs", None)
        m = self._gce_model_now()
        if fs is None or m is None:
            return []
        try:
            c = m.get(fs["cid"])
        except KeyError:
            return []
        eng = self._gce_engine()
        sc = getattr(c, "scope", None) or eng.Scope()
        subs = [eng.Scope.from_dict(d) for d in (sc.first or ())] \
            if sc.kind == "set" else [sc]
        info = self._gce_tree_info
        out = set()
        for sub in subs:
            if sub.kind == "all":
                return list(range(1, self.gce.line_count() + 1))
            if sub.kind in ("layers", "group"):
                kind = "layer" if sub.kind == "layers" else "group"
                try:
                    lo = int(sub.first or 1)
                    hi = int(sub.last if sub.last not in (None, "") else lo)
                except (TypeError, ValueError):
                    continue
                for nf in info.values():
                    if nf.get("kind") == kind and nf.get("first") \
                            and lo <= (nf.get("ord") or 0) <= hi:
                        out.update(range(nf["first"], nf["last"] + 1))
            elif sub.kind == "lines":
                want = {tuple(k) for k in (sub.first or ())}
                r = self._gce_render()
                out.update(i for i, rl in enumerate(r.lines, start=1)
                           if rl.key and tuple(rl.key) in want)
            elif sub.kind == "edit":
                out.update(getattr(self, "_gce_chip_lines", {}).get(
                    str(sub.first), ()))
        if not out:
            out.update(self.gce.focus_lines or ())
        return sorted(out)

    # -- the drag ---------------------------------------------------------- #
    def _gce_mv_drag(self, delta, done):
        """The gimbal moved by `delta` (world mm, then degrees about Z)
        since its press: the box's numbers follow, and the ghost; the
        text takes them at Done (`_gce_edit_apply`). A turn is about the gimbal, which sits at the moved pivot,
        so it adds to the box's turn and leaves its X / Y as they are."""
        vs = self._gce_mv_vars()
        fs = getattr(self, "_gce_fs", None)
        if vs is None or fs is None:
            return
        vs = vs + (self._gce_mv_field("rz"),)
        if getattr(self, "_gce_mv_drag0", None) is None:
            self._gce_mv_drag0 = self._gce_mv_now() + (self._gce_mv_turn(),)
            if self._gce_mv_pivot() is None:
                # a turn needs its pivot written down: the gimbal's
                g = self._gce_pv_panel.gimbal_origin()
                b = self._gce_mv_drag0
                if g is not None:
                    self._gce_mv_quiet((
                        (self._gce_mv_field("cx"), _fmt_mm(g[0] - b[0])),
                        (self._gce_mv_field("cy"), _fmt_mm(g[1] - b[1]))))
        base = self._gce_mv_drag0
        delta = tuple(delta) + (0.0,) * (4 - len(delta))
        want = [_fmt_mm(b + d) for b, d in zip(base, delta)]
        if not done:
            # the fields show the numbers; the form's own trace waits
            self._gce_mv_quiet(zip(vs, want))
            return
        self._gce_mv_drag0 = None
        for v, s in zip(vs, want):
            v.set("")               # a write the trace sees, always
            v.set(s)
        # the form's own apply, now rather than after its pause: the move
        # waits for Done in it (`_gce_edit_apply`), so this moves the ghost
        if fs["after"] is not None:
            try:
                self.root.after_cancel(fs["after"])
            except (ValueError, tk.TclError):
                pass
            fs["after"] = None
        self._gce_edit_apply()

    # -- the selection in the picture -------------------------------------- #
    def _gce_on_pick(self, hit):
        """While a move is open a click picks what a move moves: a fiber's
        layer, a double-click its scaffold; Ctrl- or Shift-click adds or
        takes away. Empty space keeps the selection. Without a move open,
        Ctrl/Shift-click does the same, and a plain click is the page's own
        (the move under the fiber; double-click: its layer)."""
        moving = self._gce_mv_vars() is not None
        mods = int((hit or {}).get("state", 0)) & 0x5
        if hit is None:
            if moving:
                return
            return super()._gce_on_pick(hit)
        if not moving and not mods:
            return super()._gce_on_pick(hit)
        key = hit.get("key")
        if not key or len(key) != 3:
            self._gce_status("Move takes layers and scaffolds: click a "
                             "scaffold's fiber.")
            return
        layer = "n:l:%s:%s:%s" % tuple(key)
        group = "n:g:%s:%s" % tuple(key[:2])
        info = self._gce_tree_info
        double = int(hit.get("clicks", 1)) >= 2
        if layer not in info:
            layer = group           # a part with no layer rows is picked whole
        iid = group if double else layer
        if iid not in info:
            return
        tree = self._gce_tree
        if mods:
            sel = [i for i in tree.selection()
                   if (info.get(i) or {}).get("kind") in ("layer", "group",
                                                          "file", "header",
                                                          "end")]
            if double:
                # the double-click's first press toggled the layer: the
                # scaffold takes its place
                sel = [i for i in sel if i != layer]
                if group not in sel:
                    sel.append(group)
            elif iid in sel:
                sel.remove(iid)
            else:
                sel.append(iid)
            tree.selection_set(sel)
            if sel:
                tree.see(sel[-1])
            self._gce_select_rows()
        else:
            self._gce_select_node(iid, see=True, focus=False)

    def _gce_sel_show(self):
        """A new selection while a move is open is what it moves: the move
        takes it at once, as Fusion's Move takes what is selected."""
        super()._gce_sel_show()
        if self._gce_mv_vars() is None \
                or getattr(self, "_gce_mv_retargeting", False):
            return
        fs = self._gce_fs
        sel = getattr(self, "_gce_sel", None)
        if not sel or sel == ("edit", fs["cid"]) or sel[0] not in (
                "node", "multi"):
            return
        if self._gce_sel_scope() is None:
            return
        if not self._gce_mv_turn() % 360.0:
            # not turned yet: the pivot follows to the new parts' middle
            self._gce_mv_quiet(((self._gce_mv_field("cx"), ""),
                                (self._gce_mv_field("cy"), "")))
        self._gce_mv_retargeting = True
        try:
            self._gce_retarget_move()
        finally:
            self._gce_mv_retargeting = False

    # -- the key ------------------------------------------------------------ #
    def _gce_key_m(self, e):
        """M: Move, as in Fusion -- unless the key is typing into
        something."""
        if not self._gce_page_shown():
            return None
        try:
            if e.widget.winfo_class() in self._GCE_TYPING:
                return None
        except (tk.TclError, AttributeError):
            return None
        why = self._gce_tool_why("offset")
        if why:
            self._gce_status(why)
            return "break"
        self._gce_tool_move()
        return "break"
