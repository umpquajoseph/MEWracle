#!/usr/bin/env python3
"""gui_gcode_twodesigns.py -- MODERN-UI ONLY: the G-code page and the Scaffold
designer as TWO designs, synced only on demand.

The designer keeps its per-scaffold vars (`scaf_vars`, `s_count`, scaffold 1's
`s_*` aliases). The page owns a second set, `gce_scaf_vars` / `gce_s_count`,
built as a value copy of the designer's the first time it is asked for. The
page's tools (`gui_gcode_design`, `gui_gcode_params`) write only the page's
set, and the page's text regenerates from it. Neither follows the other on
its own. The one link is on the Scaffold designer's title: "Update from
G-code window" (page -> designer). The page has no link back.

App-wide settings (Quality / Export, the stabilization base, the collector)
stay shared: only the per-scaffold design and the scaffold count are split.

`_gce_page_scope()` is the one seam the designer's own argv builders and
validators run through for the page: it points `scaf_vars`, `s_count` and the
scaffold-1 aliases at the page's set for the duration, so the text the page
generates is exactly what the designer's Generate would write for that
design. `_gce_preset_scope()` (the page's Presets panel) instead
borrows the designer's vars: the page's design goes in, the plain preset code
runs with every widget it expects, and the result comes back to the page.

Persisted as gce.s<n>.<key> and gce.count, scaffold preset kind (beside
gce.document). An older preset without them gives the page the designer's
design it loads.

THE PAGE HAS NO SCAFFOLD LIMIT: `gce_scaf_vars` grows as `gce_s_count`
does, each new scaffold a copy of the last (`_gce_ensure_scaffolds`).

4 AXIS: EVERY PAGE SCAFFOLD IS A TUBE OF ITS OWN. Each
page scaffold dict carries its own copy of the tube settings, keyed by the
App's own m_* names (`TUBE_KEYS`, persisted gce.s<n>.m_*), plus how far out
from the rod it starts (`START_OUT`). The rod's diameter and Advanced's rig
settings (speed policy, feeds, calibrated placement) stay app-wide.
`_gce_tube_scope(i)` points the designer's Tube code (`gui_mandrel`) at
scaffold i for the duration -- its m_* vars, its Layers / speed / fiber as
`scaf_vars[0]`, and the solver state the Tube code keeps on the App
(`_TUBE_STATE`) -- so validation, resolution, the Requested / Resolved
answer and the argv are the Tube code's own, per scaffold. Several tubes
go out as scaffold 1's argv plus `--tube-scaffolds` (`scaffold_gcode.
generate_mandrel_scaffolds_gcode`). The designer keeps ONE tube: the two
"Update from…" links copy it to and from page scaffold 1, and a designer
tube of N side-by-side copies (`m_copies`, the retired Copies) comes onto
the page as N scaffolds that far apart."""
from gui_common import *
import contextlib
import json
import re

#: per-scaffold keys whose value is not part of the design (preview-only UI)
_NOT_DESIGN = {"linear_periods", "linear_editmode", "shape_editmode"}

#: a 4-axis scaffold's own tube: the App's m_* vars each page scaffold keeps
#: a copy of, under the same names
TUBE_KEYS = ("m_family", "m_length", "m_angle", "m_eta", "m_rm",
             "m_closure_override", "m_strategy", "m_design_mode",
             "m_pore_target", "m_length_tol", "m_eta_cap", "m_cell_w",
             "m_cell_h", "m_cell_fit", "m_helix_pick", "m_cell_pick",
             "m_angle_pick", "m_turn_pick", "m_rib_angle", "m_cell_amplitude",
             "m_cell_graph", "m_cell_grid", "m_layer_step",
             "m_layer_overrides", "m_place", "m_place_offset",
             "m_turn_out_on", "m_turn_out")
#: page-only: how far out from the bare rod a 4-axis scaffold starts, mm.
#: The designer's one tube always starts on the rod.
START_OUT = "m_start_out"
#: what the Tube code keeps on the App between calls (caches, ladders, pin
#: notes, the drawing's half-made edge), one set per tube
_TUBE_STATE = ("_m_pore_cache", "_m_angle_cache", "_m_turn_cache",
               "_m_pore_solver_pick", "_m_pore_alternatives",
               "_m_angle_solver_pick", "_m_angle_alternatives",
               "_m_turn_solver_pick", "_m_turn_alternatives",
               "_m_helix_dropped", "_m_angle_dropped", "_m_turn_dropped",
               "_m_cell_dropped", "_m_cell_ladder_cache",
               "_m_amplitude_backfill", "_m_cell_pending", "_m_cell_drag",
               "_m_cell_orphan")
#: scaffold 1's argv flag -> the `--tube-scaffolds` key it fills
_ENTRY_FLAGS = {"--tube-family": "tube_family", "--tube-length": "tube_length",
                "--winding-angle": "winding_angle", "--tube-eta": "tube_eta",
                "--tube-rm": "tube_rm", "--tube-pivots": "tube_pivots",
                "--tube-strategy": "tube_strategy",
                "--cell-width": "cell_width", "--cell-height": "cell_height",
                "--cell-fit": "cell_fit", "--cell-n-around": "cell_n_around",
                "--cell-n-along": "cell_n_along", "--rib-angle": "rib_angle",
                "--cell-amplitude": "cell_amplitude",
                "--cell-graph": "cell_graph", "--layers": "layers",
                "--speed": "speed", "-d": "fiber_diameter",
                "--tube-layer-step": "tube_layer_step",
                "--tube-turn-out": "tube_turn_out",
                "--tube-blocks": "tube_blocks", "--start-x": "start_x"}
#: the bare rod between a new 4-axis scaffold and the one before it, mm
#: a tube's state as `gui_mandrel._mandrel_vars_make` starts it; the rest
#: (caches, ladders) start absent
_TUBE_STATE_INIT = {"_m_amplitude_backfill": None, "_m_cell_pending": None,
                    "_m_cell_drag": None, "_m_cell_orphan": None,
                    "_m_helix_dropped": False, "_m_angle_dropped": False,
                    "_m_turn_dropped": False, "_m_cell_dropped": False}
NEW_TUBE_GAP = 5.0
_MISSING = object()


def _value_copy(var):
    """A fresh Tk variable of `var`'s type holding its value. The designer's
    rotation is a combobox WIDGET; the page gets a StringVar for it."""
    if isinstance(var, tk.BooleanVar):
        return tk.BooleanVar(value=bool(var.get()))
    if isinstance(var, tk.IntVar):
        return tk.IntVar(value=var.get())
    if isinstance(var, tk.DoubleVar):
        return tk.DoubleVar(value=var.get())
    return tk.StringVar(value=str(var.get()))


def _is_value(x):
    return isinstance(x, tk.Variable) or (hasattr(x, "get")
                                          and hasattr(x, "set")
                                          and not isinstance(x, dict))


class _GcodeTwoDesignsMixin:

    # ------------------------------------------------------------------ #
    # the page's own design
    # ------------------------------------------------------------------ #
    @property
    def gce_scaf_vars(self):
        sv = self.__dict__.get("_gce_sv")
        if sv is None:
            sv = []
            for v in self.scaf_vars:
                sv.append({k: _value_copy(x) for k, x in v.items()
                           if k == "rotation" or isinstance(x, tk.Variable)})
            for v in sv:
                v.setdefault("rotation",
                             tk.StringVar(value=ROTATION_PRESETS[0]))
                self._gce_tube_seed(v)
                import gui_speed_ratio
                gui_speed_ratio.attach(self, v)
                import gui_gcode_params
                gui_gcode_params.attach_layer_grade(v)
            self.__dict__["_gce_sv"] = sv
            # the physics diameter follows each scaffold's own speed here
            # too (gui_sim._phys_page_attach)
            attach = getattr(self, "_phys_page_attach", None)
            if attach is not None:
                for v in sv:
                    attach(v)
        return sv

    def _gce_tube_seed(self, v):
        """Give a page scaffold dict its own tube: a value copy of the
        designer's (the m_* vars), starting on the rod."""
        make = getattr(self, "_mandrel_vars_make", None)
        if make is None:
            return
        make()
        for n in TUBE_KEYS:
            if n not in v:
                v[n] = _value_copy(getattr(self, n))
        v.setdefault(START_OUT, tk.StringVar(value="0"))
        import gui_mandrel
        gui_mandrel.watch_placement(v["m_place"])

    @property
    def gce_s_count(self):
        c = self.__dict__.get("_gce_count")
        if c is None:
            src = getattr(self, "s_count", None)
            c = tk.StringVar(value=src.get() if src is not None else "1")
            self.__dict__["_gce_count"] = c
            c.trace_add("write", lambda *_a: self._gce_ensure_scaffolds(
                self._gce_scaffold_count()))
        return c

    def _gce_scaffold_count(self):
        """The page's scaffold count: any whole number from 1, no limit."""
        try:
            return max(1, int(float(self.gce_s_count.get())))
        except (ValueError, tk.TclError):
            return 1

    def _gce_ensure_scaffolds(self, n):
        """At least `n` page scaffold dicts: each new one a value copy of the
        last, in the next colour of the designer's cycle, and regenerating
        the page like the rest (`_gce_d_trace_vars`)."""
        sv = self.gce_scaf_vars
        while len(sv) < n:
            k = len(sv)
            v = {key: _value_copy(x) for key, x in sv[-1].items()
                 if key == "rotation" or isinstance(x, tk.Variable)}
            if "color" in v:
                v["color"].set(SCAFFOLD_COLORS[k % len(SCAFFOLD_COLORS)])
            if "m_place" in v:
                import gui_mandrel
                gui_mandrel.watch_placement(v["m_place"])
            sv.append(v)
            import gui_speed_ratio
            gui_speed_ratio.attach(self, v)
            import gui_gcode_params
            gui_gcode_params.attach_layer_grade(v)
            attach = getattr(self, "_phys_page_attach", None)
            if attach is not None:
                attach(v)
            if getattr(self, "_gce_d_traced", False):
                for x in v.values():
                    if isinstance(x, tk.Variable):
                        x.trace_add("write",
                                    lambda *_a: self._gce_design_regen())

    @contextlib.contextmanager
    def _gce_page_scope(self):
        """The designer's builders read the PAGE's design inside this."""
        if getattr(self, "_gce_in_page_scope", False):
            yield
            return
        page, desig = self.gce_scaf_vars, self.scaf_vars
        swaps = [("scaf_vars", page), ("s_count", self.gce_s_count)]
        for k, x in desig[0].items():
            if getattr(self, "s_" + k, None) is x and k in page[0]:
                swaps.append(("s_" + k, page[0][k]))
        saved = [(n, getattr(self, n, None)) for n, _ in swaps]
        self._gce_in_page_scope = True
        try:
            for n, val in swaps:
                setattr(self, n, val)
            yield
        finally:
            for n, val in saved:
                setattr(self, n, val)
            self._gce_in_page_scope = False

    # ------------------------------------------------------------------ #
    # 4 axis: one scaffold's tube, for the designer's Tube code
    # ------------------------------------------------------------------ #
    @contextlib.contextmanager
    def _gce_tube_scope(self, i):
        """The Tube code reads page scaffold `i`'s tube inside this: its m_*
        vars, its Layers / speed / fiber as `scaf_vars[0]` (and the s_*
        aliases), its solver state, and one copy (Copies is retired on the
        page; scaffolds replace it). Inside `_gce_page_scope`, which it
        opens if it is not open."""
        if getattr(self, "_gce_tube_in", None) == i:
            yield
            return
        with self._gce_page_scope():
            page = self.gce_scaf_vars
            self._gce_ensure_scaffolds(i + 1)
            v = page[i]
            self._gce_tube_seed(v)
            swaps = [(n, v[n]) for n in TUBE_KEYS]
            one = self.__dict__.get("_gce_one_copy")
            if one is None:
                one = self._gce_one_copy = (tk.StringVar(value="1"),
                                            tk.StringVar(value="5"))
            swaps += [("m_copies", one[0]), ("m_copy_gap", one[1])]
            first = self.scaf_vars[0]
            swaps.append(("scaf_vars", [v] + [x for j, x in enumerate(page)
                                              if j != i]))
            for k, x in first.items():
                if getattr(self, "s_" + k, None) is x and k in v:
                    swaps.append(("s_" + k, v[k]))
            saved = [(n, getattr(self, n)) for n, _x in swaps]
            d = self.__dict__
            states = d.setdefault("_gce_tube_states", {})
            mine = states.setdefault(id(v), {})
            app_state = {n: d.get(n, _MISSING) for n in _TUBE_STATE}
            prev = d.get("_gce_tube_in")
            try:
                for n, x in swaps:
                    setattr(self, n, x)
                for n in _TUBE_STATE:
                    if n in mine:
                        d[n] = mine[n]
                    elif n in _TUBE_STATE_INIT:
                        d[n] = _TUBE_STATE_INIT[n]
                    else:
                        d.pop(n, None)
                d["_gce_tube_in"] = i
                yield
            finally:
                for n in _TUBE_STATE:
                    if n in d:
                        mine[n] = d[n]
                    else:
                        mine.pop(n, None)
                    if app_state[n] is _MISSING:
                        d.pop(n, None)
                    else:
                        d[n] = app_state[n]
                for n, x in saved:
                    setattr(self, n, x)
                d["_gce_tube_in"] = prev

    def _gce_tube_count(self):
        """How many tubes the page lays: its scaffold count."""
        return self._gce_scaffold_count()

    def _gce_start_out(self, i):
        """Scaffold `i`'s Start out from rod, mm; a half-typed field reads 0
        (the validator names it)."""
        try:
            return float(self.gce_scaf_vars[i][START_OUT].get() or 0.0)
        except (ValueError, tk.TclError, KeyError, IndexError):
            return 0.0

    def _gce_tube_layouts(self):
        """Each tube's `gui_mandrel_preview.AxialLayout`, or None for one
        that will not resolve."""
        out = []
        for i in range(self._gce_tube_count()):
            with self._gce_tube_scope(i):
                try:
                    out.append(self._mandrel_axial_layout(
                        self._mandrel_resolved_design()))
                except Exception:  # noqa: BLE001 -- the validator names it
                    out.append(None)
        return out

    def _gce_tube_rod(self):
        """(u0, u1): the rod every tube is on, collector frame. A declared
        rod is the machine's, the same for all; a derived one (no rotary
        stage) covers each tube's own."""
        lays = [x for x in self._gce_tube_layouts() if x is not None]
        if not lays:
            return None
        return (min(x.rod_u0 for x in lays), max(x.rod_u1 for x in lays))

    def _mandrel_origin_u(self, design):
        """Several tubes: g-code X = 0 is on the rod they share, so every
        tube's argv and the player agree on it.

        The one tube's answer is named, not `super()`: the Calibration page
        runs this through a `gui_sweeps._RigView`, which is not an instance
        of this class, and a zero-argument super() refused it -- the lag
        tube then fell back to a rod derived from the print."""
        import gui_mandrel
        one = gui_mandrel._MandrelMixin._mandrel_origin_u
        if not getattr(self, "_gce_in_page_scope", False) \
                or self._gce_tube_count() < 2:
            return one(self, design)
        rod = self._gce_tube_rod()
        if rod is None:
            return one(self, design)
        axial, _radial = self._rotary_origin_keys()
        if axial == "far":
            return rod[1]
        if axial == "mid":
            return 0.5 * (rod[0] + rod[1])
        if axial == "custom":
            p = self._zero_custom_point("mandrel")
            return rod[0] + (p[1] if p is not None else 0.0)
        return rod[0]

    def _gce_tube_multi(self):
        """True when the page's tube text needs `--tube-scaffolds`: more than
        one tube, or one that starts out from the rod."""
        return self._gce_tube_count() > 1 or any(
            self._gce_start_out(i) for i in range(self._gce_tube_count()))

    def _validate_mandrel(self):
        """On the page: every tube, each in its own scope, named by its
        scaffold when there are several."""
        if not getattr(self, "_gce_in_page_scope", False) \
                or getattr(self, "_gce_tube_in", None) is not None:
            return super()._validate_mandrel()
        n = self._gce_tube_count()
        for i in range(n):
            with self._gce_tube_scope(i):
                err = super()._validate_mandrel()
            if err is None:
                text = str(self.gce_scaf_vars[i][START_OUT].get()).strip()
                try:
                    out = float(text or 0.0)
                    if out < 0.0 or out != out:
                        err = ("Start out from mandrel cannot be negative: it is "
                               "a distance out from the bare mandrel.")
                except ValueError:
                    err = ("Start out from mandrel must be a number (it reads "
                           "“%s”)." % text)
            if err:
                return ("Scaffold %d: %s" % (i + 1, err)) if n > 1 else err
        return None

    def _gce_tube_entry(self, argv, i):
        """Scaffold `i`'s `--tube-scaffolds` entry, read off its own argv (the
        Tube code's), so the two cannot spell a tube two ways."""
        e = {}
        try:
            k = argv.index("--progress") + 1
        except ValueError:
            k = 0
        while k < len(argv) - 1:
            key = _ENTRY_FLAGS.get(argv[k])
            if key is not None:
                e[key] = argv[k + 1]
                k += 2
            else:
                k += 1
        e["start_z"] = repr(self._gce_start_out(i))
        return e

    def _build_args_mandrel(self):
        """On the page: scaffold 1's argv, and every tube as a
        `--tube-scaffolds` entry when there is more than the one."""
        if not getattr(self, "_gce_in_page_scope", False) \
                or getattr(self, "_gce_tube_in", None) is not None:
            return super()._build_args_mandrel()
        with self._gce_tube_scope(0):
            args = super()._build_args_mandrel()
        if not self._gce_tube_multi():
            return args
        tubes = [self._gce_tube_entry(args, 0)]
        for i in range(1, self._gce_tube_count()):
            with self._gce_tube_scope(i):
                a = super()._build_args_mandrel()
            tubes.append(self._gce_tube_entry(a, i))
        return list(args) + ["--tube-scaffolds", json.dumps(tubes)]

    def _fiber_params_mandrel(self):
        """On the page: scaffold 1's, with the rod every tube is on."""
        if not getattr(self, "_gce_in_page_scope", False) \
                or getattr(self, "_gce_tube_in", None) is not None:
            return super()._fiber_params_mandrel()
        with self._gce_tube_scope(0):
            args = list(super()._fiber_params_mandrel())
            if self._gce_tube_count() > 1:
                rod = self._gce_tube_rod()
                if rod is not None:
                    origin = self._mandrel_origin_u(
                        self._mandrel_resolved_design())
                    for flag, val in (("--mandrel-start", rod[0] - origin),
                                      ("--mandrel-length", rod[1] - rod[0])):
                        if flag in args:
                            args[args.index(flag) + 1] = repr(val)
        if self._gce_tube_count() > 1:
            groups = self._gce_tube_groups()
            if groups:
                # one group per tube, as a plate's scaffolds send, so the
                # player draws each tube in ITS colour; the single
                # --fiber-color above then only colours a tube with none
                args += ["--scaffold-groups", json.dumps(groups)]
        return args

    def _gce_tube_groups(self):
        """The player's `--scaffold-groups` for a page of tubes: per tube its
        layer count (summed over its blocks, as `scaffold_gcode` groups a
        `--tube-scaffolds` run), fiber and colour. None when the page's argv
        does not carry the tubes."""
        try:
            argv = self._build_args_mandrel()
            entries = json.loads(argv[argv.index("--tube-scaffolds") + 1])
        except (ValueError, IndexError, TypeError):
            return None
        groups = []
        for i, e in enumerate(entries):
            try:
                if e.get("tube_blocks"):
                    layers = sum(int(float(b.get("layers", 0)))
                                 for b in json.loads(e["tube_blocks"]))
                else:
                    layers = int(float(e["layers"]))
                g = {"name": "scaffold%d" % (i + 1), "layers": layers,
                     "fiber_diameter": float(e["fiber_diameter"])}
            except (KeyError, ValueError, TypeError, AttributeError):
                return None
            with self._gce_tube_scope(i):
                col = self.scaf_vars[0]["color"].get().strip()
            if col:
                g["color"] = col
            groups.append(g)
        return groups

    def _gce_tube_snapshot(self):
        """The page's tube picture: scaffold 1's rod and radius, the rod
        covering every tube (the text it draws holds them all)."""
        with self._gce_tube_scope(0):
            snap = self._mandrel_snapshot()
        if self._gce_tube_count() > 1:
            rod = self._gce_tube_rod()
            if rod is not None:
                snap.update(rod_start=rod[0], rod_length=rod[1] - rod[0],
                            place="left", place_offset=0.0)
        return snap

    def _gce_tube_gap(self):
        """The first tube whose length does not close: (scaffold, gap), or
        None."""
        for i in range(self._gce_tube_count()):
            with self._gce_tube_scope(i):
                gap = self._mandrel_length_gap()
            if gap is not None:
                return i, gap
        return None

    def _gce_tube_place_beside(self, k, gap=NEW_TUBE_GAP):
        """Put scaffold `k` beside scaffold k-1 on the rod: from the rod's
        left end, `gap` mm of bare rod past where k-1's print ends."""
        v = self.gce_scaf_vars[k]
        import gui_mandrel
        with self._gce_tube_scope(k - 1):
            try:
                lay = self._mandrel_axial_layout(
                    self._mandrel_resolved_design())
                near, far = lay.clearance
                end = (lay.rod_u1 - lay.rod_u0) - far
            except Exception:  # noqa: BLE001 -- a half-typed tube
                try:
                    end = (self._mandrel_place_offset()
                           + float(self.m_length.get()))
                except (ValueError, tk.TclError):
                    end = 0.0
        v["m_place"].set("left")
        v["m_place_offset"].set("%g" % round(end + float(gap), 4))
    # ------------------------------------------------------------------ #
    @staticmethod
    def _gce_copy_vars(src, dst, keep=()):
        for s, d in zip(src, dst):
            for k, x in d.items():
                if k in _NOT_DESIGN or k in keep or k not in s \
                        or not _is_value(x):
                    continue
                try:
                    val = s[k].get()
                    if x.get() != val:
                        x.set(val)
                except (AttributeError, tk.TclError):
                    pass

    def _gce_designer_follow(self):
        """The designer page's widgets follow its vars, as a preset load
        does."""
        self._pv_suppress = True
        try:
            if hasattr(self, "_sync_scaffold_count"):
                self._sync_scaffold_count()
            for v in self.scaf_vars:
                for fn in ("_sync_shape_combo", "_toggle_shape",
                           "_toggle_array", "_toggle_linear_pattern",
                           "_toggle_angle_pattern"):
                    try:
                        getattr(self, fn)(v)
                    except (AttributeError, KeyError, tk.TclError):
                        pass
        finally:
            self._pv_suppress = False
        if hasattr(self, "_pv_all_dirty"):
            self._pv_all_dirty()

    def _gce_update_designer_from_page(self):
        """Scaffold designer link: the page's design becomes the
        designer's. The designer designs one tube: it takes scaffold 1's."""
        self._scaf_grow(self._gce_scaffold_count())
        self._gce_copy_vars(self.gce_scaf_vars, self.scaf_vars)
        self.s_count.set(self.gce_s_count.get())
        self._gce_tube_to_designer()
        self._gce_designer_follow()
        tube = getattr(self, "s_collector", None) is not None \
            and self.s_collector.get() == "mandrel"
        self.status.set(
            "The Scaffold designer now has scaffold 1's tube: it designs "
            "one tube." if tube and self._gce_scaffold_count() > 1 else
            "The Scaffold designer now has the G-code page's design.")

    def _gce_tube_from_designer(self):
        """Page scaffold 1's tube becomes the designer's one tube. A tube the
        designer lays as N copies side by side (`m_copies`, from an older
        preset) comes onto the page as N scaffolds, the copies' gap apart:
        the page has scaffolds, not copies."""
        if not hasattr(self, "m_family"):
            return
        v0 = self.gce_scaf_vars[0]
        for n in TUBE_KEYS:
            try:
                val = getattr(self, n).get()
                if v0[n].get() != val:
                    v0[n].set(val)
            except (AttributeError, KeyError, tk.TclError):
                pass
        v0[START_OUT].set("0")
        try:
            copies = int(self.m_copies.get().strip() or "1")
            gap = float(self.m_copy_gap.get().strip() or NEW_TUBE_GAP)
        except (ValueError, tk.TclError):
            copies, gap = 1, NEW_TUBE_GAP
        if copies > 1 and self.s_collector.get() == "mandrel":
            self._gce_ensure_scaffolds(copies)
            for k in range(1, copies):
                self._gce_copy_vars([self.gce_scaf_vars[k - 1]],
                                    [self.gce_scaf_vars[k]], keep=("color",))
                self._gce_tube_place_beside(k, gap)
            self.gce_s_count.set(str(copies))

    def _gce_tube_to_designer(self):
        """The designer's one tube becomes page scaffold 1's (no copies)."""
        if not hasattr(self, "m_family"):
            return
        v0 = self.gce_scaf_vars[0]
        for n in TUBE_KEYS:
            try:
                val = v0[n].get()
                if getattr(self, n).get() != val:
                    getattr(self, n).set(val)
            except (AttributeError, KeyError, tk.TclError):
                pass
        if self.m_copies.get() != "1":
            self.m_copies.set("1")

    def _gce_after_page_load(self):
        """The page's design was set wholesale: its open forms and the
        regenerate catch up."""
        if not hasattr(self, "gce"):
            return
        if getattr(self, "_gce_d_form_open", None):
            v = self.gce_scaf_vars[self._gce_d_form_open[0]] \
                if self._gce_d_form_open[0] < len(self.gce_scaf_vars) \
                else None
            if v is not None and hasattr(self, "_gce_d_follow"):
                self._gce_d_follow(v, "_sync_shape_combo", "_toggle_shape",
                                   "_toggle_array", "_toggle_linear_pattern",
                                   "_toggle_angle_pattern")
        if hasattr(self, "_gce_design_regen"):
            self._gce_design_regen()

    @contextlib.contextmanager
    def _gce_preset_scope(self):
        """The page's Presets panel: Load / Save / Update act on the
        PAGE's design. The designer's vars carry it for the duration (so the
        plain preset code and its widget syncs run unchanged), then the
        designer gets its own design back."""
        keep = [{k: x.get() for k, x in v.items()
                 if k not in _NOT_DESIGN and _is_value(x)}
                for v in self.scaf_vars]
        keep_n = self.s_count.get()
        # the tube: page scaffold 1's goes in, so a Save writes it as m.*
        names = TUBE_KEYS + ("m_copies",) if hasattr(self, "m_family") else ()
        keep_m = {n: getattr(self, n).get() for n in names}
        self._scaf_grow(self._gce_scaffold_count())
        self._gce_copy_vars(self.gce_scaf_vars, self.scaf_vars)
        self.s_count.set(self.gce_s_count.get())
        self._gce_tube_to_designer()
        self._gce_in_preset_scope = True
        self._gce_tube_take = False
        self._gce_scope_count = None
        try:
            yield
        finally:
            self._gce_in_preset_scope = False
            self._gce_ensure_scaffolds(len(self.scaf_vars))
            self._gce_copy_vars(self.scaf_vars, self.gce_scaf_vars)
            # A preset that carries the page's own count keeps it. The
            # designer's cannot stand in for it on a mandrel: applying
            # s.collector pins the designer to one tube
            # (`gui_scaffold._apply_collector_mode`), so a page of two tubes
            # loaded back as one.
            self.gce_s_count.set(self._gce_scope_count
                                 or self.s_count.get())
            self._gce_scope_count = None
            if self._gce_tube_take:
                self._gce_tube_from_designer()
            self._gce_tube_take = False
            for n, val in keep_m.items():
                try:
                    if getattr(self, n).get() != val:
                        getattr(self, n).set(val)
                except tk.TclError:
                    pass
            for v, vals in zip(self.scaf_vars, keep):
                for k, val in vals.items():
                    try:
                        if v[k].get() != val:
                            v[k].set(val)
                    except (AttributeError, tk.TclError):
                        pass
            self.s_count.set(keep_n)
            self._gce_designer_follow()
            self._gce_after_page_load()

    # ------------------------------------------------------------------ #
    # the designer's argv and Generate, for the page, read the page's design
    # ------------------------------------------------------------------ #
    def _gce_design_argv(self):
        with self._gce_page_scope():
            return super()._gce_design_argv()

    def _gce_run_designer(self):
        with self._gce_page_scope():
            return super()._gce_run_designer()

    # ------------------------------------------------------------------ #
    # persistence: the page's design rides the scaffold preset
    # ------------------------------------------------------------------ #
    def _gce_persist_entries(self):
        m = dict(super()._gce_persist_entries())
        m["gce.count"] = self.gce_s_count
        for i, v in enumerate(self.gce_scaf_vars):
            for k, x in v.items():
                if k not in _NOT_DESIGN and isinstance(x, tk.Variable):
                    m[f"gce.s{i + 1}.{k}"] = x
        return m

    def _apply_preset_for_kind(self, kind, data):
        if isinstance(data, dict):
            # the page has no scaffold limit: room for every scaffold the
            # preset names, before its keys are mapped onto the vars
            idx = [int(m.group(1)) for m in (
                re.match(r"gce\.s(\d+)\.", k) for k in data) if m]
            if idx:
                self._gce_ensure_scaffolds(max(idx))
        r = super()._apply_preset_for_kind(kind, data)
        if isinstance(data, dict) and str(data.get("gce.count") or "").strip() \
                and getattr(self, "_gce_in_preset_scope", False):
            # the scope's exit would otherwise take the designer's count
            self._gce_scope_count = str(data["gce.count"]).strip()
        if isinstance(data, dict) and "gce.count" not in data \
                and any(k.startswith("s.") for k in data):
            # an older preset: one design, so the page takes it too
            self._gce_ensure_scaffolds(len(self.scaf_vars))
            self._gce_copy_vars(self.scaf_vars, self.gce_scaf_vars)
            self.gce_s_count.set(self.s_count.get())
        if isinstance(data, dict) and "gce.s1.m_family" not in data \
                and any(k.startswith("m.") for k in data):
            # a preset from before the page had tubes of its own: scaffold
            # 1's is the designer's (and its copies, scaffolds)
            if getattr(self, "_gce_in_preset_scope", False):
                self._gce_tube_take = True      # once the scope is done
            else:
                self._gce_tube_from_designer()
        if isinstance(data, dict) and any(k.startswith("gce.s") or
                                          k == "gce.count" for k in data):
            self._gce_after_page_load()
        return r
