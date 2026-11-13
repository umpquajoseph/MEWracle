#!/usr/bin/env python3
"""gui_gcode_design.py -- MODERN-UI ONLY: the Scaffold designer's tools on the
CAD-style G-code page (3-axis plate only, for now).

The page has a design of its own (`gce_scaf_vars` / `gce_s_count`, built by
`_GcodeTwoDesignsMixin` in `gui_gcode_twodesigns`), which its text is
generated from; the Scaffold designer keeps its own. A design tool here never
writes g-code: it sets the PAGE's Tk vars and asks for a regenerate. The page's edits ride on top of the new text exactly as
they do when the design changes.

  * Ribbon (left of Add): Create -- New scaffold (a menu: beside the
    last one on the plate, or on top of it), Base, Footprint;
    Pattern -- Array, Wave, Layer angles (the designer's cards of the same names);
    Modify -- Parameters (the Parameters table, `gui_gcode_params`).
  * Tree: under each Scaffold row, one ``d:s<i>:<feature>`` row per feature
    that is on (or suppressed), with its own form. The
    stabilization base is the existing "Base layers" group row.
  * A feature row's eye HIDES its fibres (an array's copies; for the
    others the whole scaffold), display only like every other eye. Its
    right-click has Suppress (off in the g-code, kept in the tree) and
    Delete; a scaffold row's right-click has Delete scaffold.

The forms are the designer's own card builders, run over a ``ChainMap`` of a
fresh dict and the page's scaffold var dict: the Tk vars are the page's own,
while the widget handles a builder stores (``array_fields``,
``linear_table``...) land in the fresh dict and never steal the designer
page's widgets.

Hooks into `gui_gcode_changes` (36fbc350): `_build_gce_design_groups`,
`_gce_design_rows`, `_gce_design_open`, `_gce_design_eye`,
`_gce_design_refresh`. Every one is a hasattr/getattr guard there."""
from gui_common import *
import json
import re
from collections import ChainMap

import gui_form
import gui_tips

#: feature -> (on-test var, tree label, form title)
_FEATURES = ("footprint", "array", "wave", "angles")
_TITLES = {"footprint": "Footprint", "array": "Array", "wave": "Wave",
           "angles": "Layer angles"}
#: what a tool turns on, and the seed a blank pattern gets so it generates
_WAVE_SEED = "0,1; 0.5,-1"          # the designer's own zig-zag example
_ANGLE_SEED = "0,60,120"

#: why a design tool waits on an opened file (New scaffold adds a scaffold)
_FILE_WHY = ("This changes a scaffold this page designs. An opened file has "
             "none yet: add one with New scaffold, beside the file or on "
             "top of it.")
#: why Wave and Layer angles never act on an opened file's own moves
_FILE_PATTERN_WHY = (
    "Wave and Layer angles shape the fibers of a scaffold this page "
    "designs. An opened file's moves are printed as they are: add a "
    "scaffold with New scaffold and pattern that.")
#: the clear gap New scaffold > Beside leaves between two scaffolds' fiber
#: (their turnaround loops included), mm: room for the hop between them
BESIDE_GAP_MM = 5.0
_XY = re.compile(r"([XY])\s*(-?\d+(?:\.\d*)?|-?\.\d+)")
_LAY = re.compile(r"G0?[123](?!\d)")

_REGEN_MS = 350


def _mm(x):
    """A length for a field or a sentence: at most 3 decimals, no trailing
    zeros (Start X 25.75, never 25.750000001)."""
    return "%g" % round(float(x), 3)


class _Key:
    """A stand-in node for `_gce_d_first_copy`: only `.key` is read."""
    def __init__(self, pid, g):
        self.key = (pid, g)


class _GcodeDesignMixin:

    # ------------------------------------------------------------------ #
    # ribbon
    # ------------------------------------------------------------------ #
    def _build_gce_design_groups(self, bar, ribbon_group):
        tool = self._gce_tool
        t = self._gce_dtools = {}
        cre = ribbon_group(bar, "Create")
        t["scaffold"] = tool(
            cre, "add", "scaffold", "New\nscaffold",
            "Add a scaffold to the design: beside the last one on the "
            "plate, or on top of it. Its settings start as a copy of the "
            "last one, so change only what differs in Parameters.",
            menu=self._gce_d_new_menu)
        t["base"] = tool(
            cre, "add", "base", "Stabilization\nscaffold",
            "A stabilization scaffold: a small dense pad printed first, beside "
            "the scaffold, so the jet is steady before the scaffold starts. "
            "For a sparse base under the scaffold, open Parameters, set Pore "
            "size by layer, and give the first layers a larger pore.",
            command=self._gce_d_base)
        t["footprint"] = tool(
            cre, "add", "footprint", "Footprint",
            "The outline the selected scaffold fills: rectangle, circle, "
            "ellipse, polygon or your own convex shape.",
            command=lambda: self._gce_d_tool("footprint"))
        pat = ribbon_group(bar, "Pattern")
        t["array"] = tool(
            pat, "change", "array", "Array",
            "Tile the selected scaffold into a grid of copies.",
            command=lambda: self._gce_d_tool("array"))
        t["wave"] = tool(
            pat, "change", "wave", "Wave",
            "Wave each straight fiber of the selected scaffold through a "
            "shape you draw.",
            command=lambda: self._gce_d_tool("wave"))
        t["angles"] = tool(
            pat, "change", "angles", "Layer\nangles",
            "A repeating cycle of layer angles for the selected scaffold, "
            "each with its own fiber spacing and offset if you like.",
            command=lambda: self._gce_d_tool("angles"))
        mod = ribbon_group(bar, "Modify")
        t["params"] = tool(
            mod, "change", "params", "Parameters",
            "Every scaffold's settings in one table: size, fiber, "
            "placement and turns. Double-click a scaffold in the tree to "
            "open it on that scaffold.",
            command=self._gce_d_params)
        if hasattr(self, "_ft_open_cad"):
            t["target"] = tool(
                mod, "change", "target", "Fiber\ntarget",
                "Type the fiber diameter you want, and a jet lag or a "
                "pressure, temperature or speed to hold if you like: get the "
                "settings that make it on your printer, with a map of every "
                "setting that does. Apply sets them for the selected "
                "scaffold.",
                command=self._ft_open_cad)
        self._gce_d_trace_vars()

    # ------------------------------------------------------------------ #
    # which scaffold a selection means
    # ------------------------------------------------------------------ #
    def _gce_d_group_index(self, pid, g):
        """A group row's designer index: 0..4 for scaffoldN, "base" for the
        stabilization base, None for anything else (a piece, a file)."""
        if pid != "base":
            return None
        m = self._gce_model_now()
        try:
            name = m._map().groups[int(g)]["name"]
        except Exception:  # noqa: BLE001
            name = ""
        if name == "stab":
            return "base"
        mt = re.fullmatch(r"scaffold(\d+)", name or "")
        if mt:
            k = int(mt.group(1)) - 1
            src = self._gce_d_sources()
            return src[k] if src is not None and k < len(src) else k
        return 0 if self._gce_scaffold_count() == 1 else None

    def _gce_d_sources(self):
        """Group k's designer scaffold: each array copy is a group of its
        own (`scaffold_gcode` names them scaffold1.. over the EXPANDED
        list), so map through the designer's own expansion. None when a
        half-typed field stops it."""
        try:
            return self._expanded_specs_and_sources()[1]
        except Exception:  # noqa: BLE001
            return None

    def _gce_d_first_copy(self, node):
        """True when `node` is its scaffold's first group (the one the
        feature rows hang under; an array's other copies get none)."""
        m = self._gce_model_now()
        try:
            name = m._map().groups[int(tuple(node.key)[1])]["name"]
            k = int(re.fullmatch(r"scaffold(\d+)", name).group(1)) - 1
        except Exception:  # noqa: BLE001
            return True
        src = self._gce_d_sources()
        return src is None or k >= len(src) or src.index(src[k]) == k

    def _gce_d_index_of(self, iid):
        """The designer index a tree row belongs to (walking up), or None."""
        tree = getattr(self, "_gce_tree", None)
        x = iid
        while x and tree is not None:
            if x.startswith("d:s"):
                return int(x.split(":")[1][1:])
            if x.startswith(("n:g:", "n:l:")):
                p = x.split(":")
                return self._gce_d_group_index(p[2], p[3])
            x = tree.parent(x) if tree.exists(x) else ""
        return None

    def _gce_d_target(self):
        """The scaffold (0-based) the design tools act on: the selected
        row's, or the only one there is."""
        for t in self._gce_targets():
            if t[0] == "node":
                i = self._gce_d_index_of(t[1])
                if isinstance(i, int):
                    return i
        if self._gce_scaffold_count() == 1:
            return 0
        # the Parameters table's lit column: a researcher looking at
        # scaffold 3's column means scaffold 3
        st = getattr(self, "_gcp", None)
        if st and st.get("shown") and isinstance(st.get("hi"), int) \
                and st["hi"] < self._gce_scaffold_count():
            return st["hi"]
        return None

    def _gce_d_why(self):
        """Why the flat-plate design tools cannot act at all now, or None."""
        coll = getattr(self, "s_collector", None)
        mode = coll.get() if coll is not None else "plate"
        if mode != "plate":
            return ("These tools design on the plate (3-axis); the "
                    "4-axis tools are to the right.")
        return self._gce_d_doc_why()

    def _gce_d_doc_why(self):
        """Why no design tool may write the text, whatever the collector."""
        doc = getattr(self, "_gce_doc", None) or {}
        if self._gce_d_file_open():
            return _FILE_WHY
        if doc.get("source") != self._GCE_LINKED:
            return ("This text is not this page's design: press Back to "
                    "this design (File) to use the design tools.")
        return None

    def _gce_d_file_open(self):
        """True while the page holds an opened g-code file with no design
        added to it: New scaffold adds one (`_gce_d_splice_scaffold`)."""
        doc = getattr(self, "_gce_doc", None) or {}
        m = self._gce_model_now() if hasattr(self, "_gce_model_now") \
            else None
        return (m is not None and m.source == "file"
                and doc.get("source") == getattr(self, "_GCE_FILE", None))

    def _gce_d_tool_why(self, k):
        """Why design tool `k` cannot act now, or None. On an opened file
        New scaffold adds a scaffold; the rest wait for one."""
        coll = getattr(self, "s_collector", None)
        if self._gce_d_file_open() and (coll is None
                                        or coll.get() == "plate"):
            if k == "scaffold":
                return None
            if k in ("wave", "angles"):
                return _FILE_PATTERN_WHY
            return _FILE_WHY
        return self._gce_d_why()

    def _gce_design_refresh(self):
        tools = getattr(self, "_gce_dtools", None)
        if not tools:
            return
        pick = None if self._gce_d_target() is not None else \
            ("Select a scaffold first: click it in the tree, or its column "
             "in Parameters.")
        for k, b in tools.items():
            w = self._gce_d_tool_why(k)
            if w is None and k in _FEATURES:
                w = pick
            if w is None and k == "params" and not hasattr(
                    self, "_gce_params_open"):
                w = "The Parameters table is not in this build yet."
            try:
                b.set_why(w)
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ #
    # regenerate
    # ------------------------------------------------------------------ #
    def _gce_design_regen(self):
        """A design var changed: regenerate the page's text from the
        designer, debounced, and only while the page is shown and following
        the designer. Unchanged designs are a no-op (the argv signature)."""
        if getattr(self, "_gce_d_restoring", False):
            return
        if not getattr(self, "_pv_suppress", False):
            self._gce_export_preview_cancel()
        if getattr(self, "_gcp_staging", 0):
            # the Parameters table is writing: its Done regenerates once
            self._gcp_dirty = True
            return
        aid = getattr(self, "_gce_d_after", None)
        if aid is not None:
            try:
                self.root.after_cancel(aid)
            except tk.TclError:
                pass
        self._gce_d_after = self.root.after(_REGEN_MS, self._gce_d_regen_now)

    # ------------------------------------------------------------------ #
    # undo: a design step takes the design back with the text
    # ------------------------------------------------------------------ #
    _STAB_VARS = ("gce_s_count", "stab_on", "stab_width", "stab_height",
                  "stab_stepover", "stab_layers", "stab_arc_radius_mult",
                  "stab_start_x", "stab_start_y")
    #: g-code zero on the plate. A zero change rewrites every Start X/Y
    #: (`gui_buildplate._zero_moved`), so the step it makes carries the zero
    #: too: Undo puts both back and the scaffold stays where it is.
    _ZERO_VARS = ("origin_anchor", "origin_custom", "origin_face")
    #: the print's end-of-print exit move (the Parameters table's last row,
    #: the Export card's first): one per print, not per scaffold, so a
    #: step carries it with the top-level design vars
    _EXIT_VARS = ("s_exit_move", "s_exit_margin")
    #: the process settings a physics-driven fiber diameter is worked from.
    #: With "Fiber diameter from physics" on, a change to one of them rewrites
    #: every scaffold's diameter, so the step it makes carries them too:
    #: Undo puts both back, and the design never keeps a diameter its
    #: physics no longer gives (Fiber target's Apply writes these).
    _PHYS_VARS = ("sim_pressure", "sim_temperature", "sim_voltage",
                  "sim_standoff")
    _PHYS_CAL = ("cal.noz_inner_diameter",)

    def _gce_d_phys_vars(self):
        """{key: var} of `_PHYS_VARS` and `_PHYS_CAL` that exist."""
        out = {n: getattr(self, n) for n in self._PHYS_VARS
               if isinstance(getattr(self, n, None), tk.Variable)}
        cal = getattr(self, "cal_vars", None) or {}
        out.update({k: cal[k] for k in self._PHYS_CAL
                    if isinstance(cal.get(k), tk.Variable)})
        return out

    def _gce_d_state(self):
        """Every design var's value, and the page's record of the text they
        made (raw, signature, base): what Undo puts back."""
        doc = getattr(self, "_gce_doc", None) or {}
        top = {n: getattr(self, n).get()
               for n in self._STAB_VARS + self._EXIT_VARS
               if isinstance(getattr(self, n, None), tk.Variable)}
        scaf = [{k: x.get() for k, x in v.items()
                 if isinstance(x, tk.Variable)}
                for v in getattr(self, "gce_scaf_vars", ())]
        zero = {n: getattr(self, n).get() for n in self._ZERO_VARS
                if isinstance(getattr(self, n, None), tk.Variable)}
        try:
            physics = self._diam_is_physics()
        except (AttributeError, tk.TclError):
            physics = False
        phys = ({k: x.get() for k, x in self._gce_d_phys_vars().items()}
                if physics else None)
        return dict(top=top, scaf=scaf, zero=zero, phys=phys,
                    raw=doc.get("raw"), sig=doc.get("sig"),
                    base=doc.get("base"))

    def _gce_take_design(self, raw, sig):
        """A new designer text: its undo step also carries the design vars
        before and after, so Undo takes back an Array as well as its
        copies (else the tree keeps a dead "Array · 2×2" row)."""
        before = getattr(self, "_gce_d_base_state", None)
        steps = getattr(self.gce, "_undo", None)
        n0 = len(steps) if steps is not None else 0
        top0 = steps[-1] if steps else None
        super()._gce_take_design(raw, sig)
        if getattr(self, "_gce_pending_design", None) == (raw, sig):
            return              # waiting on the typed-edits question
        now = self._gce_d_state()
        self._gce_d_base_state = now
        steps = getattr(self.gce, "_undo", None)
        if before is not None and steps is not None and (
                len(steps) == n0 and (not steps or steps[-1] is top0)):
            # the text came out the same: the edit still gets its step
            self._gce_d_step_unseen(before, now)
            return
        if before is None or not steps:
            return
        meta = steps[-1][3]
        if meta is not None and len(meta) == 2:
            steps[-1][3] = (meta[0], meta[1], before, now)

    @staticmethod
    def _gce_d_vars_of(st):
        """A design state's vars, without the text record."""
        return {k: v for k, v in st.items() if k not in ("raw", "sig", "base")}

    def _gce_d_step_unseen(self, before, now):
        """A design edit the g-code does not show (Grade the spacing ticked,
        which lays the even layer; a field the generator does not read for
        this design) made no text step, so Undo passed over it and took
        back the edit before. It gets a step of its own, which changes no
        line and puts the design back."""
        if self._gce_d_vars_of(before) == self._gce_d_vars_of(now):
            return
        model = getattr(self, "_gce_model_now", None)
        snap = None
        if model is not None and model() is not None:
            # the document is unchanged: the step restores it to itself
            snap = self._gce_snap(record=False)
            if snap is None:
                return
        self.gce.push_meta_step((snap, snap, before, now))

    def _gce_check_design(self):
        """A design edit that leaves the generator's arguments as they were
        starts no regenerate, so it has its step here
        (`_gce_d_step_unseen`)."""
        super()._gce_check_design()
        doc = getattr(self, "_gce_doc", None) or {}
        before = getattr(self, "_gce_d_base_state", None)
        if (before is None or getattr(self, "_gce_regen", None) is not None
                or getattr(self, "_gce_d_restoring", False)
                or doc.get("source") != self._GCE_LINKED
                or doc.get("error") or not doc.get("raw")
                or getattr(self, "_gce_pending_design", None)):
            return
        now = self._gce_d_state()
        self._gce_d_base_state = now
        self._gce_d_step_unseen(before, now)

    def _gce_on_step(self, meta, forward):
        super()._gce_on_step(meta, forward)
        if len(meta) >= 4 and meta[2 + bool(forward)] is not None:
            self._gce_d_restore(meta[2 + bool(forward)])

    def _gce_d_restore(self, st):
        """Put the design vars back as `st` has them, with no regenerate:
        the page's raw and signature come back too, so the design matches
        the text again."""
        self._gce_d_restoring = True
        try:
            # zero first: its own rewrite of every Start X/Y (the designer's
            # too) lands them where the step had them, and the page's are
            # then already equal below
            for n, val in (st.get("zero") or {}).items():
                x = getattr(self, n, None)
                if isinstance(x, tk.Variable) and x.get() != val:
                    x.set(val)
            # the process settings next, only while the diameter still
            # follows them (`_PHYS_VARS`): the diameters they rewrite are
            # then set back to the step's own just below
            phys = st.get("phys")
            try:
                follows = phys is not None and self._diam_is_physics()
            except (AttributeError, tk.TclError):
                follows = False
            if follows:
                vs = self._gce_d_phys_vars()
                for k, val in phys.items():
                    x = vs.get(k)
                    if x is not None and x.get() != val:
                        x.set(val)
            for n, val in st["top"].items():
                x = getattr(self, n, None)
                if isinstance(x, tk.Variable) and x.get() != val:
                    x.set(val)
            for v, vals in zip(getattr(self, "gce_scaf_vars", ()), st["scaf"]):
                for k, val in vals.items():
                    x = v.get(k)
                    if isinstance(x, tk.Variable) and x.get() != val:
                        x.set(val)
                # an open form's widgets follow, as a click would
                self._gce_d_follow(v, "_sync_shape_combo", "_toggle_shape",
                                   "_toggle_array", "_toggle_linear_pattern",
                                   "_toggle_angle_pattern")
            if hasattr(self, "_toggle_stabilization"):
                self._toggle_stabilization()
        finally:
            self._gce_d_restoring = False
        aid = getattr(self, "_gce_d_after", None)
        if aid is not None:
            try:
                self.root.after_cancel(aid)
            except tk.TclError:
                pass
            self._gce_d_after = None
        doc = self._gce_doc
        doc.update(raw=st["raw"], sig=st["sig"], base=st["base"])
        # the design as it now stands: an open form's widgets may have
        # written a var of their own while following, which a later check
        # would take for an edit (`_gce_d_step_unseen`) and so drop Redo
        self._gce_d_base_state = self._gce_d_state()
        self._gce_design_refresh()

    def _gce_set_link_label(self):
        """The header and the design tools say the same thing: following
        or not (a stale tool tip said "not the designer's" under
        "Following")."""
        super()._gce_set_link_label()
        self._gce_design_refresh()

    def _gce_d_regen_now(self):
        self._gce_d_after = None
        if not self._gce_page_shown():
            return
        if (getattr(self, "_gce_doc", None) or {}).get("source") \
                != self._GCE_LINKED:
            return
        self._gce_check_design()

    def _gce_d_trace_vars(self):
        """Every design var regenerates the page on a change -- whoever
        changes it (these forms, the Parameters table, an Update from Scaffold
        designer)."""
        if getattr(self, "_gce_d_traced", False):
            return
        self._gce_d_traced = True
        cb = lambda *_: self._gce_design_regen()   # noqa: E731
        vs = []
        for v in getattr(self, "gce_scaf_vars", ()):
            vs += [x for x in v.values() if isinstance(x, tk.Variable)]
        for name in ("gce_s_count", "stab_on", "stab_width", "stab_height",
                     "stab_stepover", "stab_layers", "stab_arc_radius_mult",
                     "stab_start_x", "stab_start_y") + self._EXIT_VARS:
            x = getattr(self, name, None)
            if isinstance(x, tk.Variable):
                vs.append(x)
        for x in vs:
            x.trace_add("write", cb)

    # ------------------------------------------------------------------ #
    # features: on/off, suppress, labels
    # ------------------------------------------------------------------ #
    def _gce_d_on(self, v, feat):
        if feat == "footprint":
            return v["shape"].get() != "rectangle"
        key = {"array": "array_on", "wave": "linear_on",
               "angles": "angle_pattern_on"}[feat]
        return bool(v[key].get())

    def _gce_d_follow(self, v, *fns):
        """Run the designer's widget-sync methods `fns` for the page's
        scaffold dict `v` -- on the open form's view, when one shows `v`.
        The page's dicts carry no widgets of their own."""
        form = getattr(self, "_gce_d_form_open", None)
        view = form[2] if form and len(form) > 2 else None
        if view is None or getattr(view, "maps", [None, None])[1] is not v:
            return
        for fn in fns:
            try:
                getattr(self, fn)(view)
            except (AttributeError, KeyError, tk.TclError):
                pass

    def _gce_d_angle_seed(self, v):
        """The cycle Layer angles starts from: the angles the scaffold already
        prints (its start angle, stepped by its rotation, until the cycle
        closes on 360 degrees), so opening the tool keeps the print and the
        user edits from what they see: 90 (square) from 0 gives 0, 90, 180,
        270. At 90 and 60 the g-code is byte-identical. At 45 every layer
        keeps its angle, but a layer past one turn (405 printed as 45)
        starts from another corner. A rotation whose cycle does not close
        within 12 layers, or one that cannot be read, starts from the fixed
        example instead."""
        try:
            rot = self._rotation_value(v["rotation"])
            a0 = float(v["start_angle"].get() or 0.0)
        except (ValueError, IndexError, KeyError, AttributeError, tk.TclError):
            return _ANGLE_SEED
        for m in range(1, 13):
            r = (m * rot) % 360.0
            if min(r, 360.0 - r) < 1e-6:
                return ", ".join("%g" % ((a0 + k * rot) % 360.0)
                                 for k in range(m))
        return _ANGLE_SEED

    def _gce_d_set(self, i, feat, on, restore=None):
        """Turn feature `feat` of scaffold `i` on or off through the
        page's vars, then let an open form's widgets follow."""
        v = self.gce_scaf_vars[i]
        if feat == "footprint":
            v["shape"].set((restore or "circle") if on else "rectangle")
            self._gce_d_follow(v, "_sync_shape_combo", "_toggle_shape")
            return
        if feat == "array":
            if on and not v["array_on"].get():
                self._seed_array_spacing(v)
            v["array_on"].set(on)
            self._gce_d_follow(v, "_toggle_array")
        elif feat == "wave":
            if on and not v["linear_pattern"].get().strip():
                seed = self._gce_d_wave_seed(v)
                if seed[1] is not None:
                    v["linear_wavelength"].set(seed[1])
                v["linear_pattern"].set(seed[0])
            v["linear_on"].set(on)
            self._gce_d_follow(v, "_toggle_linear_pattern")
        elif feat == "angles":
            if on and not v["angle_pattern"].get().strip():
                v["angle_pattern"].set(self._gce_d_angle_seed(v))
            v["angle_pattern_on"].set(on)
            self._gce_d_follow(v, "_toggle_angle_pattern")

    @staticmethod
    def _gce_d_wave_seed(v):
        """(points, wavelength or None) a new wave starts from: a zig-zag
        that swings half a stepover each way over two stepovers, so
        neighbouring fibers keep their spacing and the pattern reads. The
        fixed +/-1 mm example swung four times a 0.5 mm stepover: the
        fibers ran nearly sideways, 0.12 mm apart across their direction,
        and the edge fibers reached 1 mm past the footprint."""
        try:
            s = float(v["stepover"].get())
        except (KeyError, ValueError, tk.TclError):
            return _WAVE_SEED, None
        if not s > 0:
            return _WAVE_SEED, None
        a, w = s / 2.0, 2.0 * s
        return ("0,%g; %g,%g" % (round(a, 4), round(w / 2.0, 4),
                                 -round(a, 4)), "%g" % round(w, 4))

    def _gce_d_suppressed(self):
        s = getattr(self, "_gce_d_supp", None)
        if s is None:
            s = self._gce_d_supp = {}
        return s

    def _gce_d_label(self, v, feat):
        try:
            if feat == "footprint":
                return "Footprint · %s" % v["shape"].get()
            if feat == "array":
                nz = v["array_nz"].get().strip()
                dims = [v["array_nx"].get().strip(), v["array_ny"].get().strip()]
                if nz not in ("", "1"):
                    dims.append(nz)
                return "Array · %s" % "×".join(dims)
            if feat == "wave":
                return "Wave · %s mm" % v["linear_wavelength"].get().strip()
            if feat == "angles":
                a = [x.strip() for x in v["angle_pattern"].get().split(",")
                     if x.strip()]
                return "Layer angles · %s" % "/".join(a[:6])
        except (KeyError, tk.TclError):
            pass
        return _TITLES[feat]

    def _gce_design_rows(self, iid, node):
        """`gui_gcode_changes` hook: the feature rows under a scaffold row,
        and the Parameters table's "Layer parameters" row under a layer that has overrides."""
        if iid is None or node is None or self._gce_d_why():
            return []
        if iid.startswith("n:l:"):
            return self._gce_d_layer_rows(iid)
        i = self._gce_d_group_index(*tuple(node.key)[:2])
        if not isinstance(i, int) or i >= len(self.gce_scaf_vars) \
                or not self._gce_d_first_copy(node):
            return []
        v = self.gce_scaf_vars[i]
        supp = self._gce_d_suppressed()
        rows = []
        for feat in _FEATURES:
            did = "d:s%d:%s" % (i, feat)
            if did in supp:
                rows.append((did, "%s · suppressed" % _TITLES[feat]))
            elif self._gce_d_on(v, feat):
                rows.append((did, self._gce_d_label(v, feat)))
        return rows

    def _gce_d_layer_rows(self, lid):
        p = lid.split(":")
        i = self._gce_d_group_index(p[2], p[3])
        if not isinstance(i, int) or not hasattr(self, "_gce_layer_overrides"):
            return []
        try:
            k = int(p[4])
            ov = self._gce_layer_overrides(i) or {}
        except Exception:  # noqa: BLE001
            return []
        if ov.get(k):
            return [("d:s%d:L%d" % (i, k), "Layer parameters")]
        return []

    def _gce_design_menu(self, menu, iid):
        """`gui_gcode_changes` hook: Suppress / Delete on a feature row, Delete on a
        scaffold or the base, "Layer parameters..." on a layer row."""
        if self._gce_d_why():
            return
        i, feat = self._gce_d_parse(iid)
        if i is not None:
            supp = "d:s%d:%s" % (i, feat) in self._gce_d_suppressed()
            menu.add_separator()
            menu.add_command(
                label="Unsuppress" if supp else "Suppress",
                command=lambda: self._gce_d_suppress(i, feat))
            menu.add_command(label="Delete %s" % _TITLES[feat].lower(),
                             command=lambda: self._gce_d_delete(i, feat))
            return
        if iid.startswith("n:g:"):
            p = iid.split(":")
            g = self._gce_d_group_index(p[2], p[3])
            first = self._gce_d_first_copy(_Key(p[2], int(p[3])))
            if g == "base":
                menu.add_separator()
                menu.add_command(label="Delete stabilization scaffold",
                                 command=self._gce_d_base_off)
            elif isinstance(g, int) and first and self._gce_scaffold_count() > 1:
                menu.add_separator()
                menu.add_command(
                    label="Delete scaffold %d" % (g + 1),
                    command=lambda: self._gce_d_remove_scaffold(g))
            return
        sl = self._gce_d_layer_of(iid)
        if sl is None or not hasattr(self, "_gce_layer_params_open"):
            return
        i, k = sl
        menu.add_separator()
        menu.add_command(label="Layer parameters…",
                         command=lambda: self._gce_layer_params_open(i, k))

    def _gce_d_layer_of(self, iid):
        """(scaffold, layer), both 0-based, for a layer row or a Layer
        parameters row; None otherwise."""
        m = re.fullmatch(r"d:s(\d+):L(\d+)", iid or "")
        if m:
            return int(m.group(1)), int(m.group(2))
        if (iid or "").startswith("n:l:"):
            p = iid.split(":")
            i = self._gce_d_group_index(p[2], p[3])
            if isinstance(i, int):
                return i, int(p[4])
        return None

    def _gce_design_group_label(self, iid, node, label):
        """`gui_gcode_changes` hook: an array copy reads "Scaffold 1 · copy 2"
        rather than as a scaffold of its own. Falsy keeps the tree's own label."""
        if self._gce_d_group_index(*tuple(node.key)[:2]) == "base":
            return "Stabilization scaffold"
        m = self._gce_model_now()
        try:
            name = m._map().groups[int(tuple(node.key)[1])]["name"]
            k = int(re.fullmatch(r"scaffold(\d+)", name).group(1)) - 1
        except Exception:  # noqa: BLE001
            return None
        src = self._gce_d_sources()
        if src is None or k >= len(src) or src.count(src[k]) < 2:
            return None
        copy = src[:k + 1].count(src[k])
        if copy == 1:
            return "Scaffold %d" % (src[k] + 1)
        return "Scaffold %d · copy %d" % (src[k] + 1, copy)

    def _gce_d_feature_groups(self, i, feat):
        """The group rows a feature's fibres are: an array's copies (all but
        the first); for the others every fibre of the scaffold, since each
        one is shaped by it."""
        tree = self._gce_tree
        mine = [g for g in tree.get_children("") if g.startswith("n:g:")
                and self._gce_d_index_of(g) == i]
        return mine[1:] if feat == "array" else mine

    def _gce_design_eye(self, iid):
        """`gui_gcode_changes` hook: a feature row's eye HIDES its fibres and their
        lines, display only like every other eye. Suppress / Delete (the
        row's right-click) are what change the g-code."""
        i, feat = self._gce_d_parse(iid)
        if i is None:
            return
        hid = self._gce_hidden
        targets = self._gce_d_feature_groups(i, feat)
        if self._gce_isolated:
            self._gce_isolated = None
        if iid in hid or any(g in hid for g in targets):
            hid.discard(iid)
            hid.difference_update(targets)
        else:
            hid.add(iid)
            hid.update(targets)
        self._gce_vis_apply()

    def _gce_d_suppress(self, i, feat):
        """Fusion's Suppress: the feature is off in the g-code but kept in
        the tree, and Unsuppress brings it back as it was."""
        did = "d:s%d:%s" % (i, feat)
        supp = self._gce_d_suppressed()
        if did in supp:
            self._gce_d_set(i, feat, True, restore=supp.pop(did))
        else:
            v = self.gce_scaf_vars[i]
            supp[did] = v["shape"].get() if feat == "footprint" else True
            self._gce_d_set(i, feat, False)

    def _gce_d_delete(self, i, feat):
        self._gce_d_suppressed().pop("d:s%d:%s" % (i, feat), None)
        self._gce_d_set(i, feat, False)
        if (getattr(self, "_gce_d_form_open", None) or (None,))[:2] == (i, feat):
            self._gce_host_close()

    def _gce_design_can_delete(self, iid):
        """Whether this design row owns a removable feature or layer edit."""
        if self._gce_d_why():
            return False
        if (iid or "").startswith("n:g:"):
            p = iid.split(":")
            group = self._gce_d_group_index(p[2], p[3])
            return group == "base" or (
                isinstance(group, int) and self._gce_scaffold_count() > 1
                and self._gce_d_first_copy(_Key(p[2], int(p[3]))))
        i, feat = self._gce_d_parse(iid)
        if i is not None:
            return i < len(self.gce_scaf_vars)
        return bool(re.fullmatch(r"d:s\d+:L\d+", iid or "")
                    and self._gce_d_layer_of(iid) is not None)

    def _gce_design_delete(self, iid):
        """Remove the design edit represented by a tree row."""
        if not self._gce_design_can_delete(iid):
            return
        if iid.startswith("n:g:"):
            p = iid.split(":")
            group = self._gce_d_group_index(p[2], p[3])
            if group == "base":
                self._gce_d_base_off()
            else:
                self._gce_d_remove_scaffold(group)
            return
        i, feat = self._gce_d_parse(iid)
        if i is not None:
            self._gce_d_delete(i, feat)
            return
        i, layer = self._gce_d_layer_of(iid)
        ov = self._gce_layer_overrides(i)
        ov.pop(layer, None)
        self._gcl_ov_var(i).set(json.dumps(
            {str(k + 1): ov[k] for k in sorted(ov)}) if ov else "")

    def _gce_d_remove_scaffold(self, i):
        """Delete scaffold `i` from the design: the ones above it move down
        a place (their settings with them) and the count drops by one."""
        n = self._gce_scaffold_count()
        if n <= 1 or not 0 <= i < n:
            return
        keys = [k for k, x in self.gce_scaf_vars[0].items()
                if isinstance(x, tk.Variable)]
        for j in range(i, n - 1):
            src, dst = self.gce_scaf_vars[j + 1], self.gce_scaf_vars[j]
            for k in keys:
                if k in src and k in dst:
                    dst[k].set(src[k].get())
            try:
                dst["rotation"].set(src["rotation"].get())
            except (KeyError, AttributeError, tk.TclError):
                pass
            self._gce_d_follow(dst, "_toggle_angle_pattern",
                               "_toggle_linear_pattern", "_toggle_array",
                               "_sync_shape_combo", "_toggle_shape")
        # suppressed state rides with its scaffold too
        supp = self._gce_d_suppressed()
        moved = {}
        for did, val in supp.items():
            j, feat = self._gce_d_parse(did)
            if j is None or j == i:
                continue
            moved["d:s%d:%s" % (j - 1 if j > i else j, feat)] = val
        supp.clear()
        supp.update(moved)
        self.gce_s_count.set(str(n - 1))

    @staticmethod
    def _gce_d_parse(iid):
        m = re.fullmatch(r"d:s(\d+):(\w+)", iid or "")
        if not m or m.group(2) not in _FEATURES:
            return None, None
        return int(m.group(1)), m.group(2)

    # ------------------------------------------------------------------ #
    # opening rows and tools
    # ------------------------------------------------------------------ #
    def _gce_design_open(self, iid):
        """`gui_gcode_changes` hook: double-click / Edit. True when this module took
        the row."""
        if self._gce_d_why():
            return False
        sl = self._gce_d_layer_of(iid) if iid.startswith("d:") else None
        if sl is not None:
            if not hasattr(self, "_gce_layer_params_open"):
                return False
            self._gce_layer_params_open(*sl)
            return True
        if iid.startswith("d:"):
            i, feat = self._gce_d_parse(iid)
            if i is None:
                return False
            self._gce_d_form(i, feat)
            return True
        if iid.startswith("n:g:"):
            p = iid.split(":")
            i = self._gce_d_group_index(p[2], p[3])
            if i == "base":
                self._gce_d_base_form()
                return True
            if isinstance(i, int) and hasattr(self, "_gce_params_open"):
                self._gce_params_open(col=i)
                return True
        return False

    def _gce_d_tool(self, feat):
        i = self._gce_d_target()
        if i is None or self._gce_d_why():
            return
        self._gce_d_suppressed().pop("d:s%d:%s" % (i, feat), None)
        if feat != "footprint" and not self._gce_d_on(self.gce_scaf_vars[i], feat):
            self._gce_d_set(i, feat, True)
        self._gce_d_form(i, feat)

    def _gce_d_params(self):
        if hasattr(self, "_gce_params_open"):
            self._gce_params_open(col=self._gce_d_target())

    def _gce_d_new_menu(self):
        """New scaffold's two places: the common lab layout (several
        designs side by side on one plate) and a stack (a crown on a base)."""
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Beside the last one",
                      command=lambda: self._gce_d_new_scaffold("beside"))
        m.add_command(label="On top of the last one",
                      command=lambda: self._gce_d_new_scaffold("top"))
        return m

    def _gce_d_new_scaffold(self, place="top"):
        """Add scaffold n+1 as a copy of the last one, `place` "top" (stood
        on it) or "beside" (on the plate next to it, `_gce_d_place_beside`).
        The status line says where it went."""
        n = self._gce_scaffold_count()
        if self._gce_d_file_open():
            return self._gce_d_splice_scaffold(place)
        if self._gce_d_why():
            return
        self._gce_ensure_scaffolds(n + 1)
        # the new scaffold starts as a copy of the one before it, as the
        # designer's count spinner does (its own colour kept)
        src, dst = self.gce_scaf_vars[n - 1], self.gce_scaf_vars[n]
        for k, x in dst.items():
            if k != "color" and k in src and hasattr(x, "set"):
                x.set(src[k].get())
        # ...and stood on top of it, as the designer's prefill does
        # (gui_scaffold._prefill_scaffold): the generator stacks nothing by
        # itself (scaffold_gcode.generate_multi_scaffold_gcode), so a copied
        # Start Z printed the new scaffold through the one below
        said = None
        if place == "beside":
            said = self._gce_d_place_beside(n)
        else:
            try:
                z = (float(src["start_z"].get())
                     + int(float(src["layers"].get()))
                     * float(src["diameter"].get()))
                dst["start_z"].set(f"{z:g}")
                said = ("Scaffold %d stands on top of scaffold %d, from Z "
                        "%s mm." % (n + 1, n, _mm(z)))
            except (KeyError, ValueError, tk.TclError):
                pass
        self.gce_s_count.set(str(n + 1))
        if hasattr(self, "_gce_params_open"):
            self._gce_params_open(col=n)
        if said and hasattr(self, "status"):
            self.status.set(said)

    def _gce_d_file_box(self, m):
        """(x0, y0, x1, y1, z0, z1) of the moves an opened file makes, from
        its first move to its last, or None when it makes none."""
        import mew_gcode as eng
        w = eng._Walker()
        box = None
        for ln in m.parse_text().splitlines():
            c, words, _d, _z = w.step(ln)
            if c not in ("G0", "G1", "G2", "G3") or w.x is None \
                    or w.y is None:
                continue
            z = w.z if w.z is not None else 0.0
            box = (w.x, w.y, w.x, w.y, z, z) if box is None else (
                min(box[0], w.x), min(box[1], w.y), max(box[2], w.x),
                max(box[3], w.y), min(box[4], z), max(box[5], z))
        return box

    def _gce_d_splice_scaffold(self, place):
        """New scaffold on an opened file: one scaffold, a copy of the
        design's first, on top of the file's fiber or beside it, and the
        page becomes a design that prints the file first and the scaffold
        after it (`mew_gcode.adopt_file`), as Splice joins pieces. The
        design tools then work on that scaffold; the file keeps its hand
        edits."""
        m = self._gce_model_now()
        self._gce_snap()                    # typing waiting is recorded
        box = self._gce_d_file_box(m)
        if box is None:
            self._gce_status("This file makes no moves to put a scaffold "
                             "beside or on.")
            return
        v = self.gce_scaf_vars[0]
        try:
            w, h = float(v["width"].get()), float(v["height"].get())
            d = float(v["diameter"].get())
            s = float(v["stepover"].get())
            mult = float(v["arc_radius_mult"].get() or 1.5)
        except (KeyError, ValueError, tk.TclError):
            self._gce_status("Scaffold 1's size does not read: fix it in "
                             "Parameters first.")
            return
        x0, y0, x1, y1, z0, z1 = box
        name = os.path.basename(self._gce_doc.get("path") or "") \
            or "Opened file"
        if place == "beside":
            grow = 2.0 * mult * s
            sx, sy, sz = x1 + BESIDE_GAP_MM + grow, y0 + grow, z0
            said = ("Scaffold 1 is beside %s, %s mm clear of its fiber. "
                    % (name, _mm(BESIDE_GAP_MM)))
        else:
            sx, sy = (x0 + x1 - w) / 2.0, (y0 + y1 - h) / 2.0
            sz = z1 + d
            said = "Scaffold 1 stands on top of %s, from Z %s mm. " % (
                name, _mm(sz))
        self._gce_d_restoring = True        # one regeneration, below
        try:
            self.gce_s_count.set("1")
            v["start_x"].set(_mm(sx))
            v["start_y"].set(_mm(sy))
            v["start_z"].set(_mm(sz))
            if self.stab_on.get():
                self.stab_on.set(False)
        finally:
            self._gce_d_restoring = False
        self._gce_splice = dict(doc=m, name=name,
                                path=self._gce_doc.get("path"))
        self._gce_splice_notes = []
        # the confirmed "Back to the design", with nothing to confirm: the
        # file is kept, in the print
        self._gce_relink_armed = self._gce_splicing = True
        try:
            self._gce_relink()
        finally:
            self._gce_splicing = False
        if hasattr(self, "status"):
            self.status.set(
                said + "The file prints first, without its own start and "
                "end lines: this printer's Start and End blocks run once, "
                "around both. Change that in the file's piece (click it in "
                "the tree).")

    # ------------------------------------------------------------------ #
    # New scaffold > Beside: where the fiber already is
    # ------------------------------------------------------------------ #
    def _gce_d_extents(self):
        """{designer index, or "base": (x0, y0, x1, y1)} -- the box of the
        fiber each scaffold lays in the page's generated text, turnaround
        loops, waves and array copies included (an array's copies are one
        box). In Start X/Y coordinates: the text is written from g-code
        zero, which is what Start X/Y are measured from. Empty when there is
        no generator map to read the scaffolds from."""
        m = self._gce_model_now()
        try:
            lm = m._map()
            if lm.guessed:
                return {}
            lines = m.base_text.splitlines()
            names = [g.get("name", "") for g in lm.groups]
        except Exception:  # noqa: BLE001
            return {}
        cuts = sorted((int(k["line"]), k.get("group"))
                      for k in lm.marks if k.get("kind") in ("group", "exit"))
        src = self._gce_d_sources()
        out = {}
        for j, (a, g) in enumerate(cuts):
            if g is None or not 0 <= int(g) < len(names):
                continue
            name = names[int(g)]
            if name == "stab":
                key = "base"
            else:
                mt = re.fullmatch(r"scaffold(\d+)", name or "")
                if not mt:
                    continue
                k = int(mt.group(1)) - 1
                key = src[k] if src is not None and k < len(src) else k
            b = cuts[j + 1][0] if j + 1 < len(cuts) else len(lines)
            x = y = None
            box = out.get(key)
            for ln in lines[a:b]:
                s = ln.split(";", 1)[0].strip().upper()
                if not s:
                    continue
                was = (x, y)
                for ax, num in _XY.findall(s):
                    if ax == "X":
                        x = float(num)
                    else:
                        y = float(num)
                if not _LAY.match(s) or x is None or y is None:
                    continue
                for px, py in (was, (x, y)):
                    if px is None or py is None:
                        continue
                    box = (px, py, px, py) if box is None else (
                        min(box[0], px), min(box[1], py),
                        max(box[2], px), max(box[3], py))
            if box is not None:
                out[key] = box
        return out

    def _gce_d_box_guess(self, v):
        """A scaffold's fiber box from its fields, when its text is not
        there to read: the footprint (its circle when turned), grown by
        the turnaround loops, about two loop radii."""
        try:
            sx, sy = float(v["start_x"].get()), float(v["start_y"].get())
            w, h = float(v["width"].get()), float(v["height"].get())
            s = float(v["stepover"].get())
            mult = float(v["arc_radius_mult"].get() or 1.5)
            turn = float(v["footprint_angle"].get() or 0.0)
        except (KeyError, ValueError, tk.TclError):
            return None
        grow = 2.0 * mult * s
        cx, cy = sx + w / 2.0, sy + h / 2.0
        hx, hy = (w / 2.0, h / 2.0) if turn % 90.0 == 0.0 else \
            ((w * w + h * h) ** 0.5 / 2.0,) * 2
        if turn % 180.0 == 90.0:
            hx, hy = hy, hx
        return (cx - hx - grow, cy - hy - grow, cx + hx + grow, cy + hy + grow)

    def _gce_d_plate_box(self):
        """The plate in Start X/Y coordinates (x0, y0, x1, y1), or None."""
        try:
            fp = self._plate_footprint_xy()
            px, py = self._scaffold_origin_xy()
        except Exception:  # noqa: BLE001
            return None
        if fp is None:
            return None
        return (fp[0] - px, fp[2] - py, fp[1] - px, fp[3] - py)

    def _gce_d_place_beside(self, n):
        """Put new scaffold `n` (0-based, its fields a copy of scaffold
        n-1) on the plate beside the others: to the right of the last one,
        BESIDE_GAP_MM clear of every scaffold's fiber; failing that, to
        the right of another; failing that, a new row behind them all.
        Returns the sentence the status line shows."""
        src, dst = self.gce_scaf_vars[n - 1], self.gce_scaf_vars[n]
        boxes = self._gce_d_extents()
        for k in range(n):
            if k not in boxes:
                guess = self._gce_d_box_guess(self.gce_scaf_vars[k])
                if guess is not None:
                    boxes[k] = guess
        last = boxes.get(n - 1)
        try:
            sx, sy = float(src["start_x"].get()), float(src["start_y"].get())
        except (KeyError, ValueError, tk.TclError):
            return None
        if last is None:
            return None
        # how far the fiber reaches left of and below Start X/Y: the copy
        # reaches as far, so its box is the last one's, moved
        ox, oy = sx - last[0], sy - last[1]
        w, h = last[2] - last[0], last[3] - last[1]
        others = list(boxes.values())
        plate = self._gce_d_plate_box()
        gap = BESIDE_GAP_MM

        def on_plate(x0, y0):
            return plate is None or (
                x0 >= plate[0] - 1e-6 and y0 >= plate[1] - 1e-6
                and x0 + w <= plate[2] + 1e-6 and y0 + h <= plate[3] + 1e-6)

        def clear(x0, y0):
            near = gap - 1e-6
            return all(x0 + w + near <= o[0] or o[2] + near <= x0
                       or y0 + h + near <= o[1] or o[3] + near <= y0
                       for o in others)

        cands = [(last[2] + gap, last[1])]
        cands += [(o[2] + gap, o[1]) for o in
                  sorted(others, key=lambda o: (o[1], o[0]))]
        row = max(o[3] for o in others) + gap
        cands.append((min(o[0] for o in others), row))
        # last, any room left on the plate: each row the scaffolds stand
        # in, and the new one, walked from the plate's left edge
        if plate is not None:
            for y0 in sorted({o[1] for o in others} | {row, plate[1]}):
                x0 = plate[0]
                while x0 + w <= plate[2] + 1e-6:
                    cands.append((x0, y0))
                    x0 += 0.5
        pick = next((c for c in cands if clear(*c) and on_plate(*c)), None)
        fits = pick is not None
        if pick is None:
            pick = cands[0]
        x, y = pick[0] + ox, pick[1] + oy
        dst["start_x"].set(_mm(x))
        dst["start_y"].set(_mm(y))
        # on the plate, where the scaffolds already printing on it start
        zs = []
        for k in range(n):
            try:
                zs.append(float(self.gce_scaf_vars[k]["start_z"].get()))
            except (KeyError, ValueError, tk.TclError):
                pass
        dst["start_z"].set(_mm(min(zs)) if zs else "0")
        if fits:
            return ("Scaffold %d is beside the others, %s mm clear of their "
                    "fiber: Start X %s, Start Y %s mm. Move it in Parameters."
                    % (n + 1, _mm(gap), _mm(x), _mm(y)))
        return ("Scaffold %d does not fit on the plate beside the others, so "
                "it is to the right of scaffold %d (Start X %s mm) and runs "
                "past the plate's edge. Move it in Parameters, or make the "
                "plate larger on Setup." % (n + 1, n, _mm(x)))

    def _gce_d_base(self):
        if self._gce_d_why():
            return
        if not self.stab_on.get():
            self.stab_on.set(True)
            self._toggle_stabilization()
        self._gce_d_base_form()

    # ------------------------------------------------------------------ #
    # forms
    # ------------------------------------------------------------------ #
    def _gce_d_host(self, title, off):
        """The edit box with a title, the body for a form, and a button row:
        Done, and `off` (label, command) to switch the feature off."""
        host, body = self._gce_host_open(title)
        btns = ttk.Frame(host)
        btns.grid(row=3, column=0, sticky="w", pady=(8, 0))
        done = self._gce_host_close
        ttk.Button(btns, text="Done", command=done).pack(side="left")
        if off is not None:
            lab, cmd = off

            def _off():
                cmd()
                done()
            ttk.Button(btns, text=lab, command=_off).pack(
                side="left", padx=(6, 0))
        return host, body, done

    def _gce_d_form(self, i, feat):
        """Feature `feat` of scaffold `i` in the edit box: the designer's own
        card body over a ChainMap view of its vars (see the module doc)."""
        v = self.gce_scaf_vars[i]
        view = ChainMap({}, v)
        title = "%s · scaffold %d" % (_TITLES[feat], i + 1)
        host, body, done = self._gce_d_host(
            title, ("Delete %s" % _TITLES[feat].lower(),
                    lambda: self._gce_d_delete(i, feat)))
        body.columnconfigure(0, weight=1)
        if feat == "footprint":
            self._build_scaffold_shape_fields(body, view)
        elif feat == "array":
            self._build_scaffold_array_fields(body, view)
        elif feat == "wave":
            self._build_scaffold_linear_fields(body, view)
            # how big a wave can be, said on the form and not only in a
            # tooltip: the swing is what sets how close neighbours run
            try:
                half = "%g mm" % round(float(v["stepover"].get()) / 2.0, 4)
            except (ValueError, tk.TclError):
                half = "half the fiber spacing"
            ttk.Label(body, font="MewHint", wraplength=420, justify="left",
                      text="Points are mm: X along the fiber, Y sideways. "
                           "Keep the sideways swing within ±%s (half the "
                           "fiber spacing) so neighbouring fibers keep their "
                           "spacing; a larger swing crowds them together and "
                           "pushes the edge fibers past the footprint."
                           % half
                      ).grid(row=99, column=0, columnspan=8, sticky="w",
                             pady=(6, 0))
        elif feat == "angles":
            self._build_scaffold_angle_pattern_body(body, view)
        # the forms' own checkboxes grey the form's widgets (the view's)
        self._gce_host_keys(host, done, done)
        self._gce_d_form_open = (i, feat, view)

    def _gce_d_base_form(self):
        """The stabilization base's rows, on the designer's own stab_* vars
        (labels and tips as `_build_stabilization_tab` has them)."""
        host, body, done = self._gce_d_host(
            "Stabilization scaffold", ("Remove stabilization scaffold", self._gce_d_base_off))
        F = gui_form.Form(body)
        rows = (
            ("Width X", "stab_width", "The pad's size along X.", "mm"),
            ("Length Y", "stab_height", "The pad's size along Y.", "mm"),
            ("Fiber spacing", "stab_stepover", gui_tips.GLOSSARY["stepover"],
             "mm"),
            ("Layers", "stab_layers", "How many layers the pad stacks.", None),
            ("Turnaround radius", "stab_arc_radius_mult",
             "Corner-loop radius as a multiple of the fiber spacing. "
             "Blank = 1.5.", "× spacing"),
            ("Start X", "stab_start_x", "X of the pad's footprint corner.",
             "mm"),
            ("Start Y", "stab_start_y", "Y of the pad's footprint corner.",
             "mm"),
        )
        for lab, name, tip, unit in rows:
            # a blank turnaround radius is the generator's 1.5: say so
            F.entry(lab, getattr(self, name), tip, unit=unit,
                    placeholder="1.5" if name == "stab_arc_radius_mult"
                    else None)
        self._gce_host_keys(host, done, done)

    def _gce_d_base_off(self):
        self.stab_on.set(False)
        self._toggle_stabilization()
