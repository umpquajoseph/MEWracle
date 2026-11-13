#!/usr/bin/env python3
"""gui_gcode_tutorial.py -- MODERN-UI ONLY: the G-code page's two tours.

  * **Tutorial** (beside Help, for everyone): ONE print, start to finish,
    the way a newcomer needs it -- the picture and the tree, sizing a
    scaffold in Parameters, grading its pores per layer, Generate, playing
    it, Simulation, keeping it, then one step on the rest of the ribbon and
    Undo. Reported: the old tutorial toured every tool in ribbon order and
    taught no task ("stale and not useful"). A "Do it" changes the design
    as the user's own clicks would (Ctrl+Z takes it back); a "Show me"
    only shows, and what it opens is put back when the tour moves on.
  * **Showcase** (hidden: shown only with ``MEWRACLE_SHOWCASE=1``): builds,
    one "Do it" at a time, a sheet that needs the page's tools together --
    a 2x2 array of round patches with triangular pores, graded porosity by
    per-layer stepover, a speed ramp on the anchoring layers, a message
    every 4 layers, a crimped (waved) crown stacked on top, a frame ring
    of Line to + Arc moves printed first, and a tweezer tab stitched in as
    pasted g-code and saved to My library. It ends with Play. Every
    Do it changes the design and the edits as the user's own clicks would:
    Ctrl+Z takes any step back.

Both run on Home's tour engine (`gui_tour`): its ring, callout and keys,
with this module's steps and a fifth item per step, the action. A Do it
that changes the DESIGN waits for the page's regeneration before the next
part of it selects a tree row (`_gt_after_idle`), since the rows are
rebuilt from the new text.

Hook: `_build_gcode_editor_page` (after every other layer's) packs the
buttons left of Help (`_gce_help_btn`)."""

import math
import os
import re

import tkinter as tk
from tkinter import ttk

import gui_fonts
from gui_tips import add_tip

#: the showcase's button is hidden unless this is set to 1
SHOWCASE_ENV = "MEWRACLE_SHOWCASE"

_NUM = re.compile(r"([XYZ])(-?\d+(?:\.\d*)?|-?\.\d+)")


class _GcodeTutorialMixin:

    def _build_gcode_editor_page(self, page):
        super()._build_gcode_editor_page(page)
        helpb = getattr(self, "_gce_help_btn", None)
        if helpb is None:
            return
        gap = gui_fonts.SP["s"]
        tut = ttk.Button(helpb.master, text="Tutorial",
                         style="BarLink.TButton",
                         command=self.start_gcode_tutorial)
        tut.pack(side="right", padx=(0, 2 * gap), after=helpb)
        add_tip(tut, "Build a first scaffold with it: size it, grade its "
                     "pores, Generate, then check it and keep it. Do it "
                     "does a step for you, and Ctrl+Z takes it back.")
        self._gt_tutorial_btn = tut
        self._gt_showcase_btn = None
        if os.environ.get(SHOWCASE_ENV) == "1":
            show = ttk.Button(helpb.master, text="Showcase",
                              command=self.start_gcode_showcase)
            show.pack(side="right", padx=(0, gap), after=tut)
            add_tip(show, "Build a graded, crimped patch sheet step by "
                          "step, with every tool on this page.")
            self._gt_showcase_btn = show

    # ------------------------------------------------------------------ #
    # starting, and what a step leaves behind
    # ------------------------------------------------------------------ #
    def start_gcode_tutorial(self):
        self._gt_undo = []
        self.start_tour(steps=self._gt_tutorial_steps(),
                        on_step=self._gt_on_step)

    def start_gcode_showcase(self):
        self._gt_undo = []
        self.start_tour(steps=self._gt_showcase_steps(),
                        on_step=self._gt_on_step)

    def _gt_on_step(self, _i):
        """Before each step (and on Close): put back what the last Show me
        opened."""
        for fn in reversed(getattr(self, "_gt_undo", None) or ()):
            try:
                fn()
            except (tk.TclError, AttributeError, KeyError):
                pass
        self._gt_undo = []

    def _gt_later(self, fn):
        """`fn` runs before the next step is shown."""
        self._gt_undo.append(fn)

    # ------------------------------------------------------------------ #
    # helpers: rows, boxes, waiting
    # ------------------------------------------------------------------ #
    def _gt_row(self, *path):
        """The tree row under the labels `path` (each the start of a row's
        text: "Scaffold 1", "Layer 3"), or None."""
        tree = getattr(self, "_gce_tree", None)
        if tree is None:
            return None
        parent = ""
        for label in path:
            hit = None
            for iid in tree.get_children(parent):
                text = str(tree.item(iid, "text")).strip()
                if text == label or text.startswith(label + " "):
                    hit = iid
                    break
            if hit is None:
                return None
            parent = hit
        return parent or None

    def _gt_rows(self, prefix):
        """Every top-level row whose text starts with `prefix`."""
        tree = getattr(self, "_gce_tree", None)
        if tree is None:
            return []
        return [i for i in tree.get_children("")
                if str(tree.item(i, "text")).startswith(prefix)]

    def _gt_select(self, *path):
        iid = self._gt_row(*path)
        if iid is not None:
            self._gce_select_node(iid, see=True)
        return iid

    def _gt_close_boxes(self):
        """Close whatever box is open over the picture (an edit, a design
        form, My library, the Parameters table), keeping its changes:
        Done, never Cancel."""
        if getattr(self, "_gce_fs", None) is not None:
            self._gce_edit_finish(keep=True)
        if getattr(self, "_gce_form", None) is not None:
            self._gce_host_close()
        if hasattr(self, "_gce_params_close"):
            self._gce_params_close()

    def _gt_idle(self):
        return (getattr(self, "_gce_regen", None) is None
                and getattr(self, "_gce_d_after", None) is None
                and getattr(self, "_gce_layout_after", None) is None
                and self._gce_model_now() is not None)

    def _gt_after_idle(self, fn, done, timeout_ms=240000):
        """Run `fn` once the page has finished regenerating (idle on two
        polls in a row: the take is followed by a tree rebuild). On a
        timeout, `done(False)`."""
        state = {"left": timeout_ms // 150, "calm": 0}

        def poll():
            try:
                calm = self._gt_idle()
            except tk.TclError:
                return
            state["calm"] = state["calm"] + 1 if calm else 0
            state["left"] -= 1
            if state["calm"] >= 3:
                fn()
            elif state["left"] <= 0:
                self._gce_status("The page is still regenerating; try "
                                 "Do it again in a moment.")
                done(False)
            else:
                self.root.after(150, poll)
        self.root.after(150, poll)

    def _gt_chain(self, parts, done):
        """Run `parts` one after another, each after the page is idle;
        then `done(True)`. A part returning False stops the chain."""
        parts = list(parts)

        def step():
            if not parts:
                done(True)
                return
            part = parts.pop(0)
            try:
                ok = part()
            except Exception as exc:  # noqa: BLE001 -- say it, don't hang
                self._gce_status(f"That step could not finish: {exc}")
                done(False)
                return
            if ok is False:
                done(False)
                return
            self._gt_after_idle(step, done)
        self._gt_after_idle(step, done)

    def _gt_field(self, kind, name, value):
        """Set a field of the open edit form (`kind` "i:dwell", "t:speed_set"
        ...): the form applies it live, as typing would."""
        fs = getattr(self, "_gce_fs", None)
        if fs is None:
            return
        var = fs["vals"]["f"].get(kind, {}).get(name)
        if var is not None:
            var.set(value)

    def _gt_flush(self):
        fs = getattr(self, "_gce_fs", None)
        if fs is not None and hasattr(self, "_gce_edit_flush"):
            self._gce_edit_flush()

    def _gt_widget(self, root, cls, text):
        """The first `cls` widget under `root` whose text is `text`."""
        stack = [root] if root is not None else []
        while stack:
            w = stack.pop(0)
            try:
                if w.winfo_class() == cls and str(w.cget("text")) == text:
                    return w
            except tk.TclError:
                pass
            stack.extend(w.winfo_children())
        return None

    # -- anchors ------------------------------------------------------------ #
    def _gt_group(self, tool):
        """A ribbon group's frame, from one of its tools."""
        try:
            return tool.master.master
        except AttributeError:
            return None

    def _gt_dtool(self, key):
        return (getattr(self, "_gce_dtools", None) or {}).get(key)

    def _gt_tool(self, key):
        return (getattr(self, "_gce_tools", None) or {}).get(key)

    def _gt_mapped(self, w):
        try:
            return w if w is not None and w.winfo_ismapped() else None
        except tk.TclError:
            return None

    def _gt_formbox(self):
        return self._gt_mapped(getattr(self, "_gce_formbox", None))

    def _gt_params_box(self):
        for n in ("_gcl", "_gcp"):
            st = getattr(self, n, None)
            if st and st.get("shown"):
                return st.get("box")
        return None

    def _gt_jet_anchor(self):
        bar = self._gt_mapped(getattr(self, "_jet_bar", None))
        return bar or getattr(self, "_jet_btn", None)

    # ------------------------------------------------------------------ #
    # the tutorial
    # ------------------------------------------------------------------ #
    def _gt_tutorial_steps(self):
        # a resolver; a tool or group the narrow window folded into the
        # ribbon's More points at More (gui_gcode_cad.RibbonFit.anchor)
        fit = getattr(self, "_gce_ribfit", None)
        A = (lambda f: f) if fit is None else (  # noqa: E731
            lambda f: (lambda: fit.anchor(f())))
        steps = [
            (None, A(lambda: self._gce_viewport),
             "Your first scaffold",
             "This walks through one print: size a scaffold, grade its "
             "pores, generate it and watch it print. Each Do it changes "
             "your design as your own clicks would, and Ctrl+Z takes it "
             "back. Drag the picture to turn it, scroll to zoom."),
            (None, A(lambda: self._gce_browser),
             "The tree is the print",
             "The header, each scaffold and its layers, the end. Click a "
             "row, or a layer in the picture, and it lights up in both.",
             ("Show me", self._gt_show_select)),
            (None, A(lambda: self._gt_params_box()
                     or self._gt_group(self._gt_dtool("params"))),
             "Size it in Parameters",
             "Parameters holds every scaffold's settings in one table: "
             "width, length, layers and fiber spacing, the distance from "
             "one fiber to the next. Do it makes a 20 × 20 mm scaffold of 12 "
             "layers. Done closes the table.",
             ("Do it", self._gt_do_size)),
            (None, A(lambda: self._gt_params_box()
                     or self._gt_group(self._gt_dtool("params"))),
             "Grade the pores",
             "Tick Grade the spacing in Parameters and pick a direction: "
             "across the layer, or up through the layers. Do it opens the "
             "fiber spacing from 0.3 mm on the bottom layer to 0.6 mm on "
             "the top: small pores below, large ones above.",
             ("Do it", self._gt_do_grade)),
            (None, A(lambda: self._gt_group(self._gt_dtool("scaffold"))),
             "More scaffolds, if you want them",
             "New scaffold adds another, beside the last one on the plate "
             "or on top of it. Stabilization scaffold "
             "prints a small dense pad first, beside the scaffold, so the "
             "jet is steady before the first real fiber."),
            (None, A(lambda: getattr(self, "run_btn", None)),
             "Generate",
             "Generate, bottom left, writes the g-code and the deposited "
             "fiber. The top right then says Up to date.",
             ("Do it", self._gt_do_generate)),
            (None, A(self._gt_jet_anchor),
             "Watch it print here",
             "The bar along the foot plays the jet over this design, "
             "carrying on from the layer the slider is on (layer 0 is the "
             "empty collector). Space pauses, Esc goes back.",
             ("Show me", self._gt_show_play)),
            (None, A(lambda: self._nav_btns.get("simulation")),
             "Check it in Simulation",
             "Simulation replays the generated print in 3-D with the jet "
             "lag, sagging and fusion. Look there before you print."),
            (None, A(lambda: (getattr(self, "save_gcode_btn", None)
                              and self.save_gcode_btn.master)),
             "Keep it",
             "Ctrl+S saves this design, so it is here next time. Save "
             "g-code as… keeps the file, and Send to printer uploads it "
             "through Printer link."),
            (None, A(lambda: self._gce_ribbon),
             "The rest of the ribbon",
             "Pattern tiles a scaffold (Array), bends its fibers (Wave) or "
             "turns its layers (Layer angles). Add inserts a command or "
             "another file. Change selection edits what you picked. "
             "Inspect shows the g-code, Find and Compare."),
            (None, A(lambda: self._gce_undo_btn),
             "Undo anything",
             "Ctrl+Z takes back any step, Ctrl+Y redoes it. Help has the "
             "index of g-code commands."),
        ]
        if getattr(self, "_jet_btn", None) is None:
            steps = [s for s in steps if s[2] != "Watch it print here"]
        return steps

    # -- Do it / Show me ---------------------------------------------------- #
    #: the tutorial's scaffold: (key, value) on scaffold 1, in mm
    _GT_SIZE = (("width", "20"), ("height", "20"), ("layers", "12"),
                ("stepover", "0.4"))
    _GT_GRADE = (0.3, 0.6)

    def _gt_do_size(self, done):
        if not self._sc_ready():
            done(False)
            return
        self._gt_close_boxes()
        v = self._sc_v()
        for key, val in self._GT_SIZE:
            if key in v:
                v[key].set(val)
        self._gt_chain([lambda: self._gce_open_scaffold_view(0)], done)

    def _gt_do_grade(self, done):
        if not self._sc_ready():
            done(False)
            return
        self._gt_close_boxes()
        try:
            n = int(float(self._sc_v()["layers"].get()))
        except (ValueError, tk.TclError):
            done(False)
            return
        self._gt_grade_layers(*self._GT_GRADE)
        self._gt_chain([lambda: self._gce_open_scaffold_view(0)], done)

    def _gt_grade_layers(self, lo, hi):
        """Grade the spacing, Up through the layers, as the Parameters
        table's rows set it: Fiber spacing is the bottom layer's and
        `stepover_top` the top's; every layer between follows
        (`gui_gcode_params.sync_layer_grade`)."""
        v = self._sc_v()
        v["stepover_end"].set("")
        v["stepover"].set("%g" % lo)
        v["stepover_top"].set("%g" % hi)

    def _gt_do_generate(self, done, timeout_ms=600000):
        """Press Generate and report back when the run ends: done(True)
        once there is a result, done(False) when it was refused or failed
        (the status line already says why).

        It presses only once the page is idle: pressed while the page was
        still rebuilding from the step before (the graded pores), the run
        built the old text and the top right read "Changed since Generate"
        the moment it finished."""
        self._gt_close_boxes()
        left = {"n": timeout_ms // 300}

        def poll():
            if getattr(self, "proc", None) is not None and left["n"] > 0:
                left["n"] -= 1
                self.root.after(300, poll)
                return
            g = getattr(self, "last_gcode", None)
            done(bool(g and os.path.isfile(g)))

        def press():
            try:
                self.run()
            except Exception as exc:  # noqa: BLE001 -- say it, don't hang
                self._gce_status(f"Generate could not start: {exc}")
                done(False)
                return
            if getattr(self, "proc", None) is None:
                done(False)
                return
            self.root.after(300, poll)
        self._gt_after_idle(press, done)

    def _gt_first_layers(self):
        """The first scaffold's row and its layer rows."""
        g = (self._gt_rows("Scaffold") or [None])[0]
        if g is None:
            return None, []
        return g, list(self._gce_tree.get_children(g))

    def _gt_show_select(self, done):
        _g, layers = self._gt_first_layers()
        if not layers:
            done(False)
            return
        self._gce_select_node(layers[min(1, len(layers) - 1)], see=True)
        done(True)

    def _gt_show_play(self, done):
        if getattr(self, "_jet", None) is None:
            self._jet_toggle()
        self._gt_later(lambda: self._jet_stop()
                       if getattr(self, "_jet", None) is not None else None)
        self.root.after(400, lambda: done(getattr(self, "_jet", None)
                                          is not None))

    # ------------------------------------------------------------------ #
    # the showcase
    # ------------------------------------------------------------------ #
    #: the patch: (key, value) on scaffold 1, in the designer's units
    _SC_PATCH = (("width", "12"), ("height", "12"), ("stepover", "0.4"),
                 ("layers", "12"), ("rotation", "90 (0/90)"),
                 ("start_x", "40"), ("start_y", "10"))
    _SC_PITCH = "16"            # array spacing, mm: 12 mm patches, 4 mm gaps
    _SC_RAMP = (0.7, 0.8, 0.9)  # layers 1-3's speed, of the scaffold's
    _SC_EVERY = 4

    def _gt_showcase_steps(self):
        A = lambda f: f                         # noqa: E731
        return [
            (None, A(lambda: self._gt_params_box() or self._gce_browser),
             "A graded, crimped patch sheet",
             "Four round patches with triangular pores, open at the top "
             "and dense at the bottom, crowned with crimped fibers and "
             "framed by a ring. Do it starts a fresh design: Ctrl+Z gets "
             "yours back.",
             ("Do it", self._sc_start)),
            (None, A(self._gt_formbox),
             "Round footprint",
             "The patch fills a circle instead of a square.",
             ("Do it", self._sc_footprint)),
            (None, A(self._gt_formbox),
             "Triangular pores",
             "Layer angles of 0, 60 and 120°: every three layers make "
             "triangles, stiff in every direction.",
             ("Do it", self._sc_angles)),
            (None, A(lambda: self._gt_params_box() or self._gce_browser),
             "Graded porosity",
             "Grade the spacing, up through the layers: the fiber spacing "
             "opens from 0.3 mm at the bottom to 0.8 mm at the top. Small "
             "pores to hold cells, big ones to let them in.",
             ("Do it", self._sc_graded)),
            (None, A(self._gt_formbox),
             "A steady start",
             "A small dense pad prints first, beside the patch, so the jet "
             "has settled before the first real fiber.",
             ("Do it", self._sc_base)),
            (None, A(lambda: self._gce_browser),
             "Speed ramp",
             "Layers 1 to 3 print at 70, 80 and 90 % speed. A slower "
             "nozzle lays a thicker fiber, so the patch anchors firmly.",
             ("Do it", self._sc_ramp)),
            (None, A(lambda: self._gce_browser),
             "A message every 4 layers",
             "The printer's screen counts the layers. One edit, repeated "
             "after every fourth layer.",
             ("Do it", self._sc_every)),
            (None, A(lambda: self._gce_viewport),
             "Array 2 × 2",
             "Four patches, 16 mm apart. Watch the ramp and the messages "
             "follow every copy.",
             ("Do it", self._sc_array)),
            (None, A(self._gt_formbox),
             "A crimped crown",
             "A second scaffold stacks on top and waves its fibers, like "
             "the crimp in tendon collagen: it stretches easily first, "
             "then stiffens.",
             ("Do it", self._sc_crown)),
            (None, A(lambda: self._gce_browser),
             "A frame ring",
             "Printed first, right after the pad: a Line to and two Arcs "
             "draw one ring round the whole sheet, so it lifts off in one "
             "piece.",
             ("Do it", self._sc_ring)),
            (None, A(lambda: self._gt_mapped(self._gce_formbox)
                     or self._gce_browser),
             "A tweezer tab",
             "A small zig-zag tab stitched onto the ring as pasted g-code, "
             "then saved to My library for the next print.",
             ("Do it", self._sc_tab)),
            (None, A(self._gt_jet_anchor),
             "Watch it print",
             "Play: the jet lays down the pad, the ring, the patches "
             "and their crowns, in order.",
             ("Do it", self._sc_play)),
        ]

    # -- the steps' work ---------------------------------------------------- #
    def _sc_v(self, i=0):
        # the page's own design (gui_gcode_twodesigns), not the designer's
        return self.gce_scaf_vars[i]

    def _sc_ready(self):
        why = self._gce_d_why()
        if why:
            self._gce_status(why)
            return False
        return True

    def _sc_start(self, done):
        if not self._sc_ready():
            done(False)
            return
        self._gt_close_boxes()
        self.gce_s_count.set("1")
        if self.stab_on.get():
            self.stab_on.set(False)
            self._toggle_stabilization()
        for feat in ("array", "wave", "angles", "footprint"):
            if self._gce_d_on(self._sc_v(), feat):
                self._gce_d_set(0, feat, False)
        self._gce_d_suppressed().clear()
        v = self._sc_v()
        v["layer_overrides"].set("")
        v["stepover_end"].set("")          # an in-layer grade left from before
        v["stepover_top"].set("")          # and a layer by layer one
        for key, val in self._SC_PATCH:
            v[key].set(val)
        self._gt_chain([lambda: self._gce_params_open(col=0)], done)

    def _sc_footprint(self, done):
        self._gt_close_boxes()

        def go():
            self._gt_select("Scaffold 1")
            self._gce_d_set(0, "footprint", True, "circle")
            self._gce_d_form(0, "footprint")
        self._gt_chain([go], done)

    def _sc_angles(self, done):
        self._gt_close_boxes()

        def go():
            self._gt_select("Scaffold 1")
            self._gce_d_tool("angles")
        self._gt_chain([go], done)

    def _sc_graded(self, done):
        self._gt_close_boxes()
        self._gt_grade_layers(0.3, 0.8)
        self._gt_chain([lambda: self._gce_open_scaffold_view(0)], done)

    def _sc_base(self, done):
        self._gt_close_boxes()
        self._gce_d_base()
        self._gt_chain([], done)

    def _sc_ramp(self, done):
        self._gt_close_boxes()
        parts = []
        for k, pct in enumerate(self._SC_RAMP, start=1):
            def one(k=k, pct=pct):
                if self._gt_select("Scaffold 1", "Layer %d" % k) is None:
                    self._gce_status("Layer %d is not in the tree." % k)
                    return False
                self._gce_tool_change("speed_set")
                fs = getattr(self, "_gce_fs", None)
                var = fs and fs["vals"]["f"].get("t:speed_set", {}).get(
                    "mm_s")
                if var is None:
                    return False
                var.set("%.3g" % (float(var.get()) * pct))
                self._gt_flush()
                self._gce_edit_finish(keep=True)
                return True
            parts.append(one)
        self._gt_chain(parts, done)

    def _sc_every(self, done):
        self._gt_close_boxes()

        def go():
            row = self._gt_select("Scaffold 1", "Layer %d" % self._SC_EVERY)
            if row is None:
                return False
            self._gce_tool_insert("message")
            fs = getattr(self, "_gce_fs", None)
            if fs is None:
                return False
            self._gt_field("i:message", "text", "MEW layer [[layer]]")
            self._gt_flush()
            self._sc_msg = fs["cid"]
            self._gce_edit_finish(keep=True)
            return True

        def repeat():
            # Change selection › Repeat on the edit: after every 4th layer
            self._gce_select_edit(self._sc_msg, see=True)
            self._gce_tool_change("repeat")
            fs = getattr(self, "_gce_fs", None)
            if fs is None:
                return False
            fs["vals"]["rwhere"].set(self._GCE_REP_LAYERS)
            fs["vals"]["rep_n"].set(str(self._SC_EVERY))
            self._gt_flush()
            self._gce_edit_finish(keep=True)
            return True
        self._gt_chain([go, repeat], done)

    def _sc_array(self, done):
        self._gt_close_boxes()
        v = self._sc_v()
        v["array_nx"].set("2")
        v["array_ny"].set("2")
        v["array_dx"].set(self._SC_PITCH)
        v["array_dy"].set(self._SC_PITCH)

        def go():
            self._gt_select("Scaffold 1")
            self._gce_d_tool("array")
        self._gt_chain([go], done)

    def _sc_crown(self, done):
        self._gt_close_boxes()

        # every var is set before the debounced regenerate fires, so the
        # crown generates once (the copy of the patch it starts as -- twelve
        # graded layers, four times -- is never built on its own)
        self._gce_d_new_scaffold()
        self._gce_params_close()
        i = self._gce_scaffold_count() - 1
        v = self._sc_v(i)
        v["layer_overrides"].set("")
        v["stepover_end"].set("")
        v["stepover_top"].set("")
        if self._gce_d_on(v, "angles"):
            self._gce_d_set(i, "angles", False)
        v["layers"].set("4")
        v["stepover"].set("1.0")
        v["linear_wavelength"].set("2")
        self._gce_d_set(i, "wave", True)

        def form():
            # the crown's first row: an array names its copies over the
            # expanded list, so find the scaffold whose index is i
            for iid in self._gt_rows("Scaffold"):
                if self._gce_d_index_of(iid) == i:
                    self._gce_select_node(iid, see=True)
                    break
            self._gce_d_form(i, "wave")
            return True
        self._gt_chain([form], done)

    def _sc_extent(self):
        """The scaffolds' fiber in the text: (cx, cy, radius, z0) -- the
        centre and reach of every scaffold row's X/Y, and the lowest Z any
        printing move has (the base pad's first layer, when there is one)."""
        tree = self._gce_tree
        text = self.gce.get_text().splitlines()
        xs, ys, zs = [], [], []
        for iid in self._gt_rows("Scaffold"):
            info = self._gce_tree_info.get(iid) or {}
            a, b = info.get("first"), info.get("last")
            if not a or not b:
                continue
            x = y = None
            for ln in text[a - 1:b]:
                if not ln.lstrip().upper().startswith(("G1", "G01")):
                    continue
                for ax, num in _NUM.findall(ln.upper()):
                    val = float(num)
                    if ax == "X":
                        x = val
                    elif ax == "Y":
                        y = val
                    else:
                        zs.append(val)
                if x is not None and y is not None:
                    xs.append(x)
                    ys.append(y)
        del tree
        # the ring goes on the plate: the lowest Z the whole text prints at
        zs += [float(z) for ln in text
               if ln.lstrip().upper().startswith(("G1 ", "G01 "))
               for z in re.findall(r"Z(-?\d+(?:\.\d*)?)", ln.upper())]
        if not xs:
            return None
        cx = (min(xs) + max(xs)) / 2.0
        cy = (min(ys) + max(ys)) / 2.0
        r = max(math.hypot(x - cx, y - cy) for x, y in zip(xs, ys))
        return cx, cy, r + 2.0, (min(zs) if zs else None)

    def _sc_ring(self, done):
        self._gt_close_boxes()
        held = {}

        def insert(kind, fields):
            sel = held.get("cid")
            if sel is not None:
                self._gce_select_edit(sel, see=True)
            self._gce_tool_insert(kind)
            fs = getattr(self, "_gce_fs", None)
            if fs is None:
                return False
            for name, val in fields.items():
                self._gt_field("i:" + kind, name, "%.4g" % val)
            held["cid"] = fs["cid"]
            self._gt_flush()
            self._gce_edit_finish(keep=True)
            return True

        def first():
            ext = self._sc_extent()
            if ext is None:
                self._gce_status("There is no scaffold to frame yet.")
                return False
            held["ext"] = ext
            base = self._gt_rows("Base") or self._gt_rows("Stabil")
            row = base[0] if base else self._gt_row("Header")
            if row is None:
                return False
            self._gce_select_node(row, see=True)
            cx, cy, r, z0 = ext
            f = dict(x=cx + r, y=cy)
            if z0 is not None:
                f["z"] = z0
            return insert("line_to", f)

        def half(sign):
            cx, cy, r, _z = held["ext"]
            return insert("arc_cw", dict(x=cx - sign * r, y=cy,
                                         i=-sign * r, j=0.0))
        self._gt_chain([first, lambda: half(1), lambda: half(-1)],
                       lambda ok: (setattr(self, "_sc_ring_end",
                                           held.get("cid")), done(ok)))

    def _sc_tab_lines(self, cx, cy, r):
        """Four passes of a 6 mm zig-zag, out from the ring's east point
        and back to it, so the print carries on from where it was."""
        x0, out = cx + r, []
        pts = [(x0 + 0.5 * k, cy + (1.5 if k % 2 else -1.5))
               for k in range(13)]
        for p in range(4):
            seq = pts if p % 2 == 0 else pts[::-1]
            out += ["G1 X%.3f Y%.3f" % xy for xy in seq]
        out.append("G1 X%.3f Y%.3f" % (x0, cy))
        return out

    def _sc_tab(self, done):
        self._gt_close_boxes()
        cid = getattr(self, "_sc_ring_end", None)
        ext = self._sc_extent()
        if cid is None or ext is None:
            self._gce_status("Do the frame ring first.")
            done(False)
            return

        def go():
            self._gce_select_edit(cid, see=True)
            self._gce_stitch_edit("snippet")
            fs = getattr(self, "_gce_fs", None)
            if fs is None:
                return False
            vals = fs["vals"]
            vals["name"].set("Tweezer tab")
            box = vals["box"]
            box.delete("1.0", "end")
            box.insert("1.0", "\n".join(self._sc_tab_lines(*ext[:3])))
            box.event_generate("<KeyRelease>")
            self._gt_flush()
            tab = fs["cid"]
            self._gce_edit_finish(keep=True)
            self._gce_save_id(tab)
            self._gce_library_toggle(True)
            return True
        self._gt_chain([go], done)

    def _sc_play(self, done):
        self._gt_close_boxes()

        def go():
            if getattr(self, "_jet", None) is None:
                self._jet_toggle()
            return True
        self._gt_chain([go], done)
