#!/usr/bin/env python3
"""4-axis (rotating-collector) half of the Scaffold designer, both desktop UIs.

Since the tab merge the mandrel is NOT a page of its own: it is the
``mandrel`` value of the Scaffold designer's ``s_collector`` toggle, and this
mixin contributes only the sections a rotating collector needs.  Everything a
tube and a planar scaffold express the same way is now ONE variable owned by
``gui_scaffold`` -- layers, speed and colour on ``scaf_vars[0]``, sag on
``s_sag_mode``/``s_sag_mult``, facets on ``s_faces``, fiber diameter on
``scaf_vars[0]["diameter"]`` (which ``gui_sim`` has already baked the prediction
into) with the 3-axis ``sim_diam_override``, live only under physics-deposit --
so the old ``m_*`` twins and their own prediction readout are gone.

What stays here is genuinely collector-specific: mandrel diameter, tube length,
the outcome the pattern is designed to, the closure ladder (η, Rm), the speed
policy, and the calibrated placement radii.  Those last three (S, R_ax, R_cir)
are ON THIS PAGE ON PURPOSE -- ``mew_calibration:314`` excludes them from the
Physics field registry so they sit beside diameter and winding angle.

**Three design modes, and the page is OUTCOME-FIRST**: the form asks
what the user wants and the table underneath says what the software made of it.
``m_design_mode`` is an engine token.  ``turn`` (the DEFAULT,
the only mode the Tube card shows rows for by default) takes omega, length AND
a pore side, and builds all three: ``mew_tube.solve_turned_tube`` picks the
even pivot count nearest the pore, and the mandrel turns at each end of a pass
onto the next pivot instead of a length having to close the helix (argv
``--tube-pivots N``; ladder pinned in ``m_turn_pick``).  The other two close by
LENGTH (McColl Eq 3.1) and are picked under Advanced's "Closure".  ``pore`` takes a pore SIDE
in micrometres and lets ``mew_tube.solve_tube_for_pore`` return the closing tube
nearest it, so omega / eta / Rm become OUTPUTS: greyed in Advanced, written by
``_mandrel_write_solved``, sent to the worker off the design object.  ``angle``
takes omega as typed and STILL resolves eta / Rm:
``mew_tube.solve_tube_options`` gives the closing length nearest the request
(the CLI's own answer at the default 5 %) plus a ladder of the other pore sizes
that angle closes at, pinned in ``m_angle_pick``.  Advanced's "Set η and Rm
myself" (``m_closure_override``) is the page this file used to be -- both typed,
argv byte-identical to ``_ZIGZAG_ARGV``'s -- and a pre-redesign preset that
typed an eta loads in it.  Pre-redesign presets load in ``angle``.

The cell families have no mode -- they are tiled, so there is no helix to
resolve.  They get BOTH of Devlin's fits at once, side by side, and the two
``Use`` buttons under the table are the only writers of ``m_cell_fit``.  ONE
table skeleton serves both: rows are what the user asked for, columns are
Requested plus one resolved column (a helix) or two, Scaled and Fitted (a cell
family).  Only when the rings wave past the ends (``params["axial_overhang"]``
non-zero) does a line name the span the print really occupies,
``length + 2 * overhang`` read off the design object rather than re-derived.
``m_cell_readout``'s sentence form is still maintained for both, because that is
where an engine refusal reaches the user verbatim; it is only DISPLAYED when the
table is not.  No generator change and no new CLI flag, and the mode owns no
Generate/Save button, so the action bar's worker keeps result tracking, scratch
output, Save-as and the player intact."""
from gui_common import *

import dataclasses
import math


_MANDREL_PREVIEW_MS = 350


class _LengthGap(ValueError):
    """Winding-angle mode's refusal when no tube closes near the length.

    The engine's own message names r, omega, eta and Rm and explains closure;
    this one says what was asked and what is nearest, in one line, and keeps
    the two nearest designs so the Tube box can offer them as `Use` rows.
    `headline` is the sentence above that table."""

    def __init__(self, headline, shorter, longer):
        near = " or ".join("%.3f" % d.length for d in (shorter, longer) if d)
        super().__init__(headline + (" Nearest: %s mm." % near if near else ""))
        self.headline = headline
        self.shorter = shorter
        self.longer = longer

    @classmethod
    def from_request(cls, diameter, omega, length, tol):
        """The gap for this request, or None when the engine refused for
        another reason (a winding angle it cannot wind at all)."""
        import mew_tube
        try:
            shorter, longer = mew_tube.nearest_closing_lengths(
                0.5 * diameter, omega, length)
        except mew_tube.TubeDesignError:
            return None
        return cls("No tube closes within %g %% of %g mm at %g°."
                   % (100.0 * tol, length, omega), shorter, longer)

#: The preview's status line while a `_LengthGap` stands. The Tube box carries
#: the detail and the `Use` rows; this only says where to look.
_MANDREL_GAP_STATUS = ("Not redrawn: no tube closes at this length. "
                       "Pick one under Tube.")
#: ...and the G-code page's, where the Parameters table offers them
_MANDREL_GAP_STATUS_PAGE = ("Not redrawn: no tube closes at this length. "
                            "Pick one in Parameters.")

#: The shared preview panel's subtitle while the collector is the mandrel.
#: Spelled once because three call sites now show the tube: the submit (which
#: has no geometry yet), the result apply, and the collector-toggle handover.
_MANDREL_SUBTITLE = "Collector-aware preview · X/A/Z path"

#: Shown in RED, in place of "updating…", when the 4-axis preview refuses to
#: build because the commanded speed is below CTS. See `_mandrel_below_cts`.
#: The flat plate refuses with the same words, so they are spelled once.
from gui_preview import BELOW_CTS_STATUS as _MANDREL_BELOW_CTS  # noqa: E402

#: Pattern families, in the order the combobox offers them: the engine token
#: that reaches ``--tube-family``, and the human label the page shows.  The
#: token is what is PERSISTED (``m.family``) -- unlike the sag-solver combobox,
#: which persists its label, because a family name is an engine identifier that
#: also has to survive being re-worded here.
MANDREL_FAMILY_CHOICES = (
    ("zigzag", "Diamond helix (McColl zigzag)"),
    ("reentrant", "Re-entrant honeycomb (auxetic)"),
    ("brick-closed", "Closed brick (rings + longitudinals)"),
    ("brick-open", "Open brick (staggered longitudinals)"),
    ("chevron", "Chevron (triangle-wave rings)"),
    ("custom", "Custom cell (drawn)"),
)
MANDREL_FAMILY_VAL2LBL = {v: l for v, l in MANDREL_FAMILY_CHOICES}
MANDREL_FAMILY_LBL2VAL = {l: v for v, l in MANDREL_FAMILY_CHOICES}
#: every family except the legacy helix -- the ones `mew_unit_cell` tiles.
MANDREL_CELL_FAMILIES = tuple(v for v, _ in MANDREL_FAMILY_CHOICES
                              if v != "zigzag")
#: the families whose circumferential fibers can be given a wave amplitude.
MANDREL_WAVE_FAMILIES = ("brick-closed", "brick-open", "chevron")
#: the diamond helix's design modes, the default first (see `m_design_mode`)
MANDREL_DESIGN_MODES = ("turn", "pore", "angle")
#: the vars Advanced (`_build_mandrel_closure_fields`) binds, the winding
#: angle aside (the Tube card's). The first ones decide whether Advanced
#: changes the tube at all (`MANDREL_ADVANCED_SWITCHES`); eta / Rm and the
#: fixed feeds are read only once one of those says so.
MANDREL_ADVANCED_VARS = ("m_design_mode", "m_closure_override", "m_eta_cap",
                         "m_length_tol", "m_speed_mode", "m_feed_mode",
                         "m_support", "m_lag_axial", "m_lag_circ",
                         "m_eta", "m_rm", "m_fixed_f", "m_fixed_e")
MANDREL_ADVANCED_SWITCHES = MANDREL_ADVANCED_VARS[:9]
#: the ONE family whose geometry is drawn rather than named. It takes neither
#: the rib angle nor the wave amplitude -- `mew_unit_cell._resolve_params`
#: refuses a knob a family does not take -- so it owns its own editor section.
MANDREL_DRAWN_FAMILY = "custom"

#: What a fresh Custom selection starts from, so the page is never a blank
#: canvas whose first Generate refuses. A honeycomb: nodes 3/4 sit on u = 0 and
#: u = 1 and weld cell to cell along the axis, nodes 5/6 sit on v = 1 and weld
#: ring to ring around the circumference.
MANDREL_DEFAULT_CELL = ("0.25,0; 0.75,0; 0,0.5; 1,0.5; 0.25,1; 0.75,1 | "
                        "1-2:free; 3-1; 2-4; 3-5; 4-6")

_FAMILY_INFO = (
    "Which pattern the tube is built from.\n\n"
    "\u2018Diamond helix\u2019 is the McColl winding: one continuous helix that reverses at each end, closing after a "
    "whole number of revolutions. It is driven by the winding angle \u03c9.\n\n"
    "Every other family is a literature UNIT CELL tiled on the unwrapped "
    "(length \u00d7 circumference) plane and routed as one continuous path: it "
    "is driven by a cell size instead, and the winding angle does not apply.\n\n"
    "Sources, read as PDFs: McCosker et al. 2022, Adv. Mater. Technol. 7, "
    "2200259, Figure 1 (unit cell, printing path and full pattern for "
    "six designs) and Table 1; Devlin et al. 2024, Adv. Mater. Technol. 9, "
    "2400419, Table 1 (the tubular library T01\u2013T07), Figure 2B and "
    "\u00a72.3.")

_FIT_INFO = (
    "How a requested cell is reconciled with the fact that a WHOLE number of "
    "cells has to close the circumference (Devlin 2024 \u00a72.3).\n\n"
    "\u2018scale\u2019 keeps the cell's aspect ratio and scales both sides, "
    "so the tube length moves away from what you asked for.\n\n"
    "\u2018fit\u2019 keeps the requested length exactly and distorts the cell "
    "around the circumference instead.\n\n"
    "Devlin's third option, \u2018no change\u2019, cannot close a "
    "circumference and is not offered.")


# Where the print sits on the rod: THE ONE TABLE of placement values, as
# (key, label, short label). An m_place var, Calibrate's `rod_place`, a
# layer's override and a saved preset all hold the KEY, which is also
# `gui_mandrel_preview.axial_layout`'s `place`; the label is what the Scaffold
# designer's menu shows and the short label the Parameters table's cell. The
# offset beside it is a DISTANCE FROM THAT END, so left and right are one
# control read from opposite ends; centre ignores it and splits the bare rod
# evenly. Ordered the way a rod reads left to right.
PLACEMENTS = (
    ("left", "Offset from the left end", "Left end"),
    ("centre", "Centered on the mandrel", "Centered"),
    ("right", "Offset from the right end", "Right end"),
)
PLACEMENT_KEYS = tuple(k for k, _l, _s in PLACEMENTS)
_PLACEMENT_LABELS = [label for _k, label, _s in PLACEMENTS]
#: What a placement was saved as before it was saved by key: its menu
#: label, in every wording the menu has had. Read ONLY by
#: `placement_from_saved`, the load-time migration. Until 2026-11-13 the var
#: held the label, so the 2026-11-11 rewording ("Centred on the rod" ->
#: "Centred on the mandrel") left saved values that matched nothing and were
#: read as "left": the row said centred while the picture and the g-code put
#: the tube at the left end.
_SAVED_PLACEMENT_LABELS = {
    "offset from the left end": "left",
    "centred on the rod": "centre",
    "centred on the mandrel": "centre",
    "centered on the mandrel": "centre",
    "offset from the right end": "right",
}


#: a tube layer table key -> the `scaffold_gcode.TUBE_LAYER_KEYS` key it
#: goes out as (the rest go out under their own name)
_LAYER_ENTRY_KEY = {"amplitude": "cell_amplitude"}


def placement_from_saved(value):
    """A saved placement as its key: a key stays a key, an old menu label
    becomes its key, anything else is returned UNCHANGED so the validator
    names it (`placement_problem`) rather than it reading as "left"."""
    text = str(value if value is not None else "").strip()
    if text in PLACEMENT_KEYS:
        return text
    return _SAVED_PLACEMENT_LABELS.get(text.lower(), value)


def placement_label(key, short=False):
    """The menu's label (or the Parameters cell's, `short`) for a key, or
    None for a value that is not one."""
    for k, label, s in PLACEMENTS:
        if k == str(key).strip():
            return s if short else label
    return None


def placement_problem(value, what="Position on mandrel"):
    """A sentence naming a placement that is not one of the choices, or
    None. A placement the software cannot read is refused by name, never
    read as the left end."""
    if str(value).strip() in PLACEMENT_KEYS:
        return None
    return ("%s reads \u201c%s\u201d, which is not one of its choices. Pick "
            "%s." % (what, value, ", ".join(
                "\u201c%s\u201d" % label for label in _PLACEMENT_LABELS)))


def watch_placement(var):
    """Migrate on write: an m_place var given an old saved label (a path
    that set it without the preset migration) holds the key instead.
    Anything unreadable is left as it is, for the validator to name.
    Idempotent per var."""
    if getattr(var, "_placement_watched", False):
        return var

    def fix(*_a):
        try:
            text = var.get()
        except tk.TclError:
            return
        key = placement_from_saved(text)
        if key != text and key in PLACEMENT_KEYS:
            var.set(key)
    var.trace_add("write", fix)
    try:
        var._placement_watched = True
    except AttributeError:
        pass
    fix()
    return var


def placement_menu_var(var):
    """A StringVar of `var`'s LABEL for a menu that shows labels, kept in
    step both ways: picking a label writes its key to `var`, and a key
    written to `var` shows as its label. An unreadable value shows as it
    is, so the menu does not pretend it is a choice."""
    shown = tk.StringVar()
    busy = []

    def to_menu(*_a):
        if busy:
            return
        busy.append(1)
        try:
            v = var.get()
            shown.set(placement_label(v) or v)
        except tk.TclError:
            pass
        finally:
            busy.pop()

    def to_var(*_a):
        if busy:
            return
        busy.append(1)
        try:
            key = next((k for k, label, _s in PLACEMENTS
                        if label == shown.get()), None)
            if key is not None and var.get() != key:
                var.set(key)
        except tk.TclError:
            pass
        finally:
            busy.pop()
    var.trace_add("write", to_menu)
    shown.trace_add("write", to_var)
    to_menu()
    return shown


class _MandrelMixin:
    """Desktop plumbing for ``scaffold_gcode --mandrel``."""

    def _mandrel_length_label(self):
        return "Scaffold length" if hasattr(self, "_gce_page_scope") else "Tube length"

    # ------------------------------------------------------------------ #
    # variables
    # ------------------------------------------------------------------ #
    def _mandrel_vars_make(self):
        if hasattr(self, "m_diameter"):
            return
        self.m_diameter = tk.StringVar(value="3.0")
        # WHERE ON THE ROD the print goes. Which end the offset is measured
        # from, and how far -- the rod's length is the machine's (Machine &
        # collector -> Rotary collector), so this is the only question left.
        # "left" + 0 is flush with the rod's near end, which on a derived rod
        # (no rotary stage declared) is byte-identical to the page's old
        # behaviour.
        self.m_place = watch_placement(tk.StringVar(value="left"))
        self.m_place_offset = tk.StringVar(value="0")
        self.m_length = tk.StringVar(value="12.0")
        # 40 deg, not 45. Kept in step with the CLI's own
        # --winding-angle default so a headless run and a fresh designer page
        # produce the same tube.
        self.m_angle = tk.StringVar(value="40.0")
        self.m_eta = tk.StringVar(value="")
        self.m_rm = tk.StringVar(value="0")
        # Advanced's "Set η and Rm myself". Off, the page resolves the closure
        # in BOTH design modes and the two fields are its greyed readout; on,
        # winding-angle mode builds the typed pair as it always did. Pore mode
        # ignores it -- the pore solver owns all three numbers there.
        self.m_closure_override = tk.BooleanVar(value=False)
        self.m_strategy = tk.StringVar(value="zigzag")
        self.m_speed_mode = tk.StringVar(value="surface")
        # The controller's FEEDS when its flavor splits them (Aerotech: F
        # linear, E rotary). WHETHER it splits is the Machine page's flavor
        # (`mew_gcode.Flavor.rotary_feed`) and the time unit its feed time
        # unit, both rig facts. "auto" works F and E out per move from the
        # surface speed, so a reentrant cell's struts all draw at it; "fixed"
        # writes the user's own F and E, in the controller's units, as typed.
        self.m_feed_mode = tk.StringVar(value="auto")
        self.m_fixed_f = tk.StringVar(value="")
        self.m_fixed_e = tk.StringVar(value="")
        # -- how the diamond helix is DRIVEN (the outcome-first redesign) ---- #
        # "pore": the user states the opening they want and the page resolves
        #   omega / eta / Rm through `mew_tube.solve_tube_for_pore`.
        # "angle": the page this file has always been -- omega is typed and the
        #   pore is whatever falls out.
        # "turn" (the DEFAULT): omega, pore AND length are typed
        #   and all three are built -- `mew_tube.solve_turned_tube` picks the
        #   pivot count nearest the pore and the mandrel turns at each end of a
        #   pass onto the next pivot, so no length has to close the helix.
        #   The other two close by LENGTH (McColl Eq 3.1) and live in Advanced.
        # An ENGINE token, never a label, for the same reason `m_family` holds
        # one.
        self.m_design_mode = tk.StringVar(value="turn")
        self.m_design_mode.trace_add("write", self._apply_mandrel_mode)
        self.m_closure_override.trace_add("write", self._apply_mandrel_mode)
        #: requested pore stated as the SIDE OF A SQUARE OF EQUAL AREA, in um.
        #: One number, because the diamond's two spans are both functions of
        #: omega and N and a second input would be over-determined.  The page
        #: squares it exactly once, in `_mandrel_pore_target_area`.
        self.m_pore_target = tk.StringVar(value="400")
        #: how far the closing length may fall from the request, in PERCENT.
        self.m_length_tol = tk.StringVar(value="5")
        #: blank -> the solver's own eta ceiling.  `mew_tube.DEFAULT_MAX_ETA`
        #: (36) is the LENGTH ladder's cap and is too low for a fine pore, so
        #: the pore solver carries a higher default of its own.
        self.m_eta_cap = tk.StringVar(value="")
        # -- pattern family, and the cell knobs the non-helix families use --- #
        # `m_family` holds the ENGINE token; `_m_family_label` is the combobox's
        # display mirror and is deliberately NOT persisted.
        self.m_family = tk.StringVar(value="zigzag")
        self._m_family_label = tk.StringVar(
            value=MANDREL_FAMILY_VAL2LBL["zigzag"])
        self._m_family_syncing = False
        self.m_family.trace_add("write", self._mandrel_family_from_value)
        self._m_family_label.trace_add("write", self._mandrel_family_from_label)
        # McCosker Sec. 4's longitudinal / radial spacing.  Their published
        # homogeneous print is 2 mm x 4.18 mm, which is 6 cells around an 8 mm
        # mandrel; this page defaults to a 3 mm mandrel, so 1.5 mm of arc is
        # 6 cells around here as well.
        self.m_cell_w = tk.StringVar(value="2.0")
        self.m_cell_h = tk.StringVar(value="1.5")
        self.m_cell_fit = tk.StringVar(value="scale")
        #: WHICH RUNG OF A LADDER THE PAGE IS USING -- the two pins.
        #: Empty is "Auto", i.e. whatever the solver or the rounding picked;
        #: otherwise the identity of one ladder row, so a reload comes back to
        #: the design the user chose rather than to the default answer.
        #: `m_helix_pick` is "eta,rm,omega" (omega to 3 dp, the precision
        #: `mew_tube.solve_tube_for_pore` itself deduplicates the ladder on)
        #: and `m_cell_pick` is "n_around,n_axial,fit".  Neither is a source of
        #: geometry: both are looked up in the ladder as it stands NOW, and a
        #: key that is no longer offered is dropped with a note rather than
        #: silently resurrecting a tube nothing on the page describes.
        self.m_helix_pick = tk.StringVar(value="")
        self.m_cell_pick = tk.StringVar(value="")
        #: ...and the winding-angle ladder's pin, same "eta,rm,omega" key. Its
        #: own var so switching the design mode does not drop the other mode's
        #: choice as "no longer on the ladder".
        self.m_angle_pick = tk.StringVar(value="")
        #: ...and the end-turn ladder's, for the same reason.
        self.m_turn_pick = tk.StringVar(value="")
        self.m_rib_angle = tk.StringVar(value="30")
        # `mew_unit_cell._resolve_params` defaults the brick families to 0 --
        # straight rings -- so 0 is the value that reproduces the engine.  The
        # chevron family REFUSES 0 (a zero-amplitude chevron is the straight
        # closed brick), which `_apply_mandrel_family` backfills on selection
        # and takes back when the chevron is left.  `_m_amplitude_backfill`
        # holds the string that backfill wrote, and the trace below drops it
        # the moment anything ELSE writes the field -- the user typing, a
        # preset loading, a persisted value being restored.  So "was this
        # value ours or theirs" is answered by provenance, not by comparing
        # numbers, and a user who types exactly the backfilled value keeps it.
        self._m_amplitude_backfill = None
        self._m_amplitude_writing = False
        self.m_cell_amplitude = tk.StringVar(value="0.0")
        self.m_cell_amplitude.trace_add("write", self._mandrel_amplitude_touched)
        # -- the DRAWN cell (the "custom" family) --------------------------- #
        #: The canonical drawing, in `mew_unit_cell.parse_cell_graph`'s form.
        #: ONE string is the source of truth, exactly as `linear_pattern` is
        #: for the 3-axis wave editor: the canvas is a view over it, so a
        #: preset load or a typed edit and a click cannot disagree.
        self.m_cell_graph = tk.StringVar(value=MANDREL_DEFAULT_CELL)
        #: snap step in CELL FRACTIONS, not mm -- the drawing is normalised, so
        #: it resolves onto any mandrel and any cell size without redrawing.
        self.m_cell_grid = tk.StringVar(value="0.25")
        #: "node" adds/drags, "edge" joins two nodes, "remove" deletes. A UI
        #: mode, so it is deliberately NOT persisted.
        self.m_cell_editmode = tk.StringVar(value="node")
        #: "1" draws the authored cell alone; "9" zooms out to a 3x3 block of
        #: REAL neighbours, which is the only way to SEE whether the drawing
        #: welds to its own copies. A UI mode, so not persisted either.
        self.m_cell_view = tk.StringVar(value="1")
        #: live validity line under the canvas; written only by `_cell_redraw`.
        self.m_cell_status = tk.StringVar(value="")
        #: the canvas's MILLIMETRE caption -- what one square is worth on the
        #: real mandrel. Display-only, rewritten by `_cell_redraw`.
        self.m_cell_size = tk.StringVar(value="")
        #: the first node of an edge being drawn, or None.
        self._m_cell_pending = None
        #: index of the node under the cursor mid-drag, or None.
        self._m_cell_drag = None
        #: read-only resolved readout, refreshed with the preview.
        self.m_cell_readout = tk.StringVar(value="")
        # These are OPTIONAL calibrated placement inputs.  Empty is the grounded
        # off case; they are folded into the existing calibration profile only for
        # a physics-deposited mandrel export.
        self.m_support = tk.StringVar(value="")
        self.m_lag_axial = tk.StringVar(value="")
        self.m_lag_circ = tk.StringVar(value="")
        # ...and the two that make the pair MANDREL-ONLY: the
        # surface speed it was measured at (mm/s) and the CTS measured on the
        # mandrel (mm/s). Blank is the old behaviour -- the pair a ratio only,
        # the flat amplitude and CTS. Written by the Calibration page's two
        # mandrel Applies, shown and edited on the Physics page's Jet lag card.
        self.m_lag_speed = tk.StringVar(value="")
        self.m_cts = tk.StringVar(value="")
        # -- the G-code page's tube tables ----------------------- #
        #: how far the nozzle steps OUT between layers, mm. Blank is the fiber
        #: diameter, the engine's own default (`--tube-layer-step`).
        self.m_layer_step = tk.StringVar(value="")
        #: PERIMETER TRAVEL on a tube: an end-turn helix runs `m_turn_out` mm
        #: past the tube's end, turns there, and comes back
        #: (`--tube-turn-out`, `scaffold_gcode.TubeBlock.turn_out`).
        self.m_turn_out_on = tk.BooleanVar(value=False)
        self.m_turn_out = tk.StringVar(value="1")
        #: per-LAYER tube settings, JSON {"<layer, 1-based>": {key: value}} with
        #: keys from `scaffold_gcode.TUBE_LAYER_KEYS` -- the 3-axis
        #: `layer_overrides` shape. Consecutive layers that ask for the same
        #: thing are one block (`scaffold_gcode.tube_layer_blocks`), so "4
        #: winding / 4 re-entrant / 4 winding" is layers 5-8 set to re-entrant.
        self.m_layer_overrides = tk.StringVar(value="")
        #: copies of the tube SIDE BY SIDE on the rod (`--tube-copies`), and
        #: the bare rod between two of them, mm (`--tube-copy-gap`).
        self.m_copies = tk.StringVar(value="1")
        self.m_copy_gap = tk.StringVar(value="5")
        #: Advanced's values as made, before any preset: what the G-code
        #: page's Advanced row compares against and resets to
        self._m_advanced_defaults = {n: getattr(self, n).get()
                                     for n in MANDREL_ADVANCED_VARS}

    def _mandrel_persist_entries(self):
        """Only the 4-axis-ONLY keys survive the merge.

        ``m.layers`` / ``m.speed`` / ``m.fiber_diameter`` / ``m.diam_override`` /
        ``m.sag_mode`` / ``m.sag_mult`` / ``m.faces`` were duplicates of the
        scaffold designer's own fields and are gone; ``gui_presets`` migrates a
        preset that still carries them onto the shared ``s.*`` keys."""
        self._mandrel_vars_make()
        return {
            "m.diameter": self.m_diameter, "m.length": self.m_length,
            "m.place": self.m_place,
            "m.place_offset": self.m_place_offset,
            "m.angle": self.m_angle,
            "m.eta": self.m_eta, "m.rm": self.m_rm,
            "m.strategy": self.m_strategy,
            "m.speed_mode": self.m_speed_mode,
            "m.feed_mode": self.m_feed_mode,
            "m.fixed_f": self.m_fixed_f, "m.fixed_e": self.m_fixed_e,
            "m.family": self.m_family,
            "m.cell_w": self.m_cell_w, "m.cell_h": self.m_cell_h,
            "m.cell_fit": self.m_cell_fit, "m.rib_angle": self.m_rib_angle,
            "m.cell_amplitude": self.m_cell_amplitude,
            "m.cell_graph": self.m_cell_graph,
            "m.cell_grid": self.m_cell_grid,
            "m.support": self.m_support, "m.lag_axial": self.m_lag_axial,
            "m.lag_circ": self.m_lag_circ,
            "m.lag_speed": self.m_lag_speed, "m.cts": self.m_cts,
            # the outcome-first redesign.  `gui_presets._migrate_legacy_mandrel_
            # mode` gives a preset that predates `m.design_mode` the value it
            # always meant ("angle"), because the FACTORY default is "pore".
            "m.design_mode": self.m_design_mode,
            "m.pore_target": self.m_pore_target,
            "m.length_tol": self.m_length_tol,
            "m.eta_cap": self.m_eta_cap,
            # the two ladder pins.  Persisted because a pinned rung is a
            # DESIGN DECISION -- the whole point of the ladder is that the
            # solver's own pick is not always the one you want -- and a preset
            # that reloaded the request without it would quietly hand back a
            # different tube.  Absent (an older preset) means Auto, which is
            # the pre-ladder behaviour, so no migration is needed.
            "m.helix_pick": self.m_helix_pick,
            "m.cell_pick": self.m_cell_pick,
            "m.angle_pick": self.m_angle_pick,
            "m.turn_pick": self.m_turn_pick,
            # Absent means the page resolves the closure. A preset from before
            # the outcome-first page that typed an eta gets True from
            # `_migrate_legacy_mandrel_mode`; a later one carried the solver's
            # echo in `m.eta`, which never meant a choice.
            "m.closure_override": self.m_closure_override,
            # the G-code page's tube tables. Absent (an older preset) is the
            # one-block tube: no per-layer change, the fiber-diameter step,
            # one copy -- what such a preset always built.
            "m.layer_step": self.m_layer_step,
            "m.turn_out_on": self.m_turn_out_on,
            "m.turn_out": self.m_turn_out,
            "m.layer_overrides": self.m_layer_overrides,
            "m.copies": self.m_copies, "m.copy_gap": self.m_copy_gap,
        }

    # ------------------------------------------------------------------ #
    # the shared fields, read through one name each
    # ------------------------------------------------------------------ #
    def _mandrel_layers(self):
        return int(self.scaf_vars[0]["layers"].get())

    def _mandrel_speed(self):
        return float(self.scaf_vars[0]["speed"].get())

    def _mandrel_rotary_feed(self):
        """"split" when the Machine page's flavor splits the feed (Aerotech:
        F linear, E rotary), else "combined" -- a rig fact, not a design one.

        The G-code page's OWN regeneration (`_gce_regen_building`) reads the
        page's flavor instead: the page spells its export in that flavor, so
        its feeds must be that flavor's too. Picked on the page alone, Aerotech
        exported a COMBINED tube in LINEAR spelling, whose surface-speed F an
        Aerotech runs as the X rate -- 1.9x the commanded surface speed on the
        4-axis default. Generate keeps the Machine page's: its
        file is spelled in that flavor (`_gcode_export_flavor`), and split
        feeds spelled for Klipper are E words Klipper extrudes."""
        import mew_gcode
        if getattr(self, "_gce_regen_building", False) \
                and hasattr(self, "gce_flavor_name"):
            name = self.gce_flavor_name()
        else:
            name = (self._machine_flavor_name()
                    if hasattr(self, "_machine_flavor_name") else "klipper")
        return mew_gcode.get_flavor(name).rotary_feed

    def _mandrel_fixed_feed(self):
        """The user's own (F, E) when Feeds is "fixed", else None. Read
        strictly: `_validate_mandrel` names a bad value before a run."""
        if self.m_feed_mode.get() != "fixed":
            return None
        return (float(self.m_fixed_f.get()), float(self.m_fixed_e.get()))

    def _mandrel_fiber_diameter(self):
        """The ONE deposited diameter, exactly as the 3-axis page resolves it.

        With physics-deposit on and the override off, ``gui_sim`` has already
        written the predetermined diameter into this var (``_refresh_scaffold_
        phys_diam``), so there is nothing to predict here."""
        return float(self.scaf_vars[0]["diameter"].get())

    def _mandrel_standoff(self):
        return self._sim_float(self.sim_standoff, 4.5)

    # ------------------------------------------------------------------ #
    # form sections (container-agnostic: the shells choose LabelFrame vs
    # Disclosure and hand the body in, exactly like the scaffold builders)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _mandrel_form(box):
        """The gui_form Form that lays out `box`'s rows -- one per box, made on
        first use, so every row of a mandrel grid shares its label and field
        edges with every other card in the shell."""
        F = getattr(box, "_gui_form", None)
        if F is None:
            import gui_form
            F = box._gui_form = gui_form.Form(box)
        return F

    def _mandrel_register_row(self, rw, families, modes):
        if families is not None:
            self._mandrel_family_register(families, *rw.widgets)
        if modes is not None:
            self._mandrel_mode_register(modes, *rw.widgets)

    def _mandrel_field(self, box, row, label, var, info=None, tips=None,
                       families=None, modes=None, indent=0):
        """One parameter row of a mandrel grid, laid out by gui_form: the unit
        in the label, `info` as the hover tip on label and field. `tips` is
        accepted for the call sites and not read.

        `families` marks the row as belonging to a subset of the pattern
        families and `modes` to a subset of the design modes; the row is then
        shown or hidden by `_apply_mandrel_visibility` with the same
        `grid_remove()` idiom `_collector_register` uses for the 3-axis /
        4-axis split, so all three switches compose instead of fighting."""
        rw = self._mandrel_form(box).entry(label, var, info, row=row,
                                           indent=indent)
        self._mandrel_register_row(rw, families, modes)
        return rw.field

    def _mandrel_combo(self, box, row, label, var, values, info=None,
                       tips=None, families=None, modes=None):
        """Same row, with a readonly combobox instead of an entry. Wide
        enough for the longest pattern family name."""
        rw = self._mandrel_form(box).combo(label, var, values, info, row=row,
                                           width=36)
        self._mandrel_register_row(rw, families, modes)
        return rw.field

    # ------------------------------------------------------------------ #
    # pattern family: which rows a family owns, and the show/hide switch
    # ------------------------------------------------------------------ #
    def _mandrel_family_register(self, families, *widgets):
        """Record widgets that belong to a SUBSET of the pattern families."""
        self._mandrel_family_widgets = getattr(
            self, "_mandrel_family_widgets", [])
        fams = frozenset(families)
        for w in widgets:
            if w is not None:
                self._mandrel_family_widgets.append((w, fams))

    def _mandrel_mode_register(self, modes, *widgets):
        """The same, for the DESIGN MODE (`pore` / `angle`) axis.

        A second list rather than a wider tuple, because the two axes are
        independent -- the pore field belongs to the diamond helix AND to pore
        mode, and either one hiding it must win.  `_apply_mandrel_visibility`
        ANDs the two, so the order the traces fire in cannot matter; a widget
        absent from a list is simply unconstrained on that axis."""
        self._mandrel_mode_widgets = getattr(self, "_mandrel_mode_widgets", [])
        ms = frozenset(modes)
        for w in widgets:
            if w is not None:
                self._mandrel_mode_widgets.append((w, ms))

    def _mandrel_solver_register(self, *widgets):
        """Entries the PORE solver writes: live in `angle` mode, greyed in `pore`.

        Greyed rather than hidden: the resolved winding angle is the number a
        user checks the ladder against, so it has to stay readable."""
        self._mandrel_solver_widgets = getattr(
            self, "_mandrel_solver_widgets", [])
        for w in widgets:
            if w is not None:
                self._mandrel_solver_widgets.append(w)

    def _mandrel_family_value(self):
        """The engine token the CLI wants, never the combobox label."""
        val = self.m_family.get().strip()
        return val if val in MANDREL_FAMILY_VAL2LBL else "zigzag"

    def _mandrel_family_from_value(self, *_):
        """m_family (the persisted token) -> the combobox's display label."""
        if self._m_family_syncing:
            return
        self._m_family_syncing = True
        try:
            self._m_family_label.set(
                MANDREL_FAMILY_VAL2LBL[self._mandrel_family_value()])
        finally:
            self._m_family_syncing = False
        self._apply_mandrel_family()

    def _mandrel_family_from_label(self, *_):
        """The combobox's display label -> m_family."""
        if self._m_family_syncing:
            return
        val = MANDREL_FAMILY_LBL2VAL.get(self._m_family_label.get())
        if val is None:
            return
        self._m_family_syncing = True
        try:
            self.m_family.set(val)
        finally:
            self._m_family_syncing = False
        self._apply_mandrel_family()

    def _mandrel_amplitude_touched(self, *_):
        """Any write NOT made by the chevron backfill makes the value theirs."""
        if not getattr(self, "_m_amplitude_writing", False):
            self._m_amplitude_backfill = None

    def _set_mandrel_amplitude_backfill(self, text):
        """Write the amplitude AS a backfill, i.e. without claiming it typed."""
        self._m_amplitude_writing = True
        try:
            self.m_cell_amplitude.set(text)
        finally:
            self._m_amplitude_writing = False
        self._m_amplitude_backfill = text

    def _apply_mandrel_family(self, *_):
        """Show the rows this family uses, hide the rest, redraw the preview."""
        self._mandrel_family_backfill()
        self._apply_mandrel_visibility()
        self._schedule_mandrel_preview()

    def _mandrel_family_backfill(self):
        """The values a family switch writes, and nothing on screen: the
        G-code page runs this for one of its own tubes
        (`gui_gcode_twodesigns._gce_tube_scope`), where the designer's
        widgets are not that tube's."""
        fam = self._mandrel_family_value()
        if fam == "chevron":
            # The engine refuses a zero-amplitude chevron by name ("ask for
            # 'brick-closed' instead"), so selecting it with the brick default
            # still in the box would put the page in a permanently invalid
            # state.  Backfill a quarter of the requested cell width, which is
            # half of what `design_tube_cells` allows before neighbouring rings
            # would intersect.
            try:
                if float(self.m_cell_amplitude.get() or 0.0) <= 0.0:
                    self._set_mandrel_amplitude_backfill(
                        "%.4g" % (0.25 * float(self.m_cell_w.get()),))
            except (ValueError, tk.TclError):
                pass
        elif getattr(self, "_m_amplitude_backfill", None) is not None:
            # Leaving the chevron: take the backfill back.  It was never asked
            # for, and a brick that inherits it prints sinusoidal rings AND
            # the end overhang that comes with them, where a straight brick
            # was requested.  Only a value this page wrote is withdrawn -- the
            # trace above has already forgotten the backfill if anything else
            # touched the field, so a typed amplitude survives the switch.
            try:
                self._set_mandrel_amplitude_backfill("0.0")
            except tk.TclError:
                pass
            self._m_amplitude_backfill = None
        if fam == MANDREL_DRAWN_FAMILY and not self.m_cell_graph.get().strip():
            # The engine refuses an empty drawing by name, so selecting the
            # drawn family with a cleared canvas would put the page in a
            # permanently invalid state -- the same trap the chevron's
            # amplitude has above. Open on the shipped honeycomb instead;
            # anything the user drew is already in the field and is kept.
            self.m_cell_graph.set(MANDREL_DEFAULT_CELL)

    def _mandrel_design_mode(self):
        """`turn`, `pore` or `angle`, never anything else."""
        val = (self.m_design_mode.get() or "").strip()
        return val if val in MANDREL_DESIGN_MODES else "turn"

    def _apply_mandrel_mode(self, *_):
        """The design-mode twin of `_apply_mandrel_family`."""
        self._apply_mandrel_visibility()
        self._schedule_mandrel_preview()

    def _apply_mandrel_visibility(self):
        """Show a row only if BOTH axes want it, and grey what the solver owns.

        One pass over both registries, because a row constrained on the family
        AND on the design mode (the pore field is) must not flicker between two
        handlers that each know half the answer."""
        fam = self._mandrel_family_value()
        mode = self._mandrel_design_mode()
        want = {}
        for widget, fams in getattr(self, "_mandrel_family_widgets", []):
            want[id(widget)] = (widget, want.get(id(widget), (widget, True))[1]
                                and fam in fams)
        for widget, modes in getattr(self, "_mandrel_mode_widgets", []):
            want[id(widget)] = (widget, want.get(id(widget), (widget, True))[1]
                                and mode in modes)
        for widget, show in want.values():
            try:
                widget.grid() if show else widget.grid_remove()
            except tk.TclError:          # a widget destroyed by a page rebuild
                continue
        # The page writes omega / eta / Rm (pore mode) or eta / Rm (winding-
        # angle mode), so they are readable but not typeable -- unless the
        # user has ticked "Set eta and Rm myself", which is the only state in
        # which they are typed. The length tolerance is the auto closure's
        # bound, so it greys in exactly that state instead.
        # "disabled", not "readonly", so they grey the same way the physics-
        # driven fiber diameter greys on this very grid (`_apply_diam_mode`) --
        # readonly is not visually distinct in this theme, which is the whole
        # point of showing them at all.
        typed = self._mandrel_closure_typed()
        for registry, live in (("_mandrel_solver_widgets", typed),
                               ("_mandrel_auto_widgets", not typed)):
            for widget in getattr(self, registry, []):
                try:
                    widget.configure(state="normal" if live else "disabled")
                except tk.TclError:
                    continue

    def _mandrel_closure_typed(self):
        """True when the helix's eta / Rm are the USER's, not the page's:
        diamond helix, winding-angle mode, and Advanced's override ticked."""
        try:
            override = bool(self.m_closure_override.get())
        except (tk.TclError, AttributeError):
            override = False
        return (override and self._mandrel_family_value() == "zigzag"
                and self._mandrel_design_mode() == "angle")

    def _build_mandrel_collector_fields(self, box, tips=None):
        """The collector itself: its diameter, and how much bare rod to leave."""
        self._mandrel_vars_make()
        box.columnconfigure(3, weight=1)
        # Column 0 holds the LABELS (sticky="e"); the slack belongs to the
        # spacer column above. Pin column 0 to no weight so a parent that
        # weights it -- ModuleGrid.add does, on every card body -- cannot split
        # the slack with the label column and shove the form right.
        box.columnconfigure(0, weight=0)
        self._mandrel_field(
            box, 0, "Mandrel diameter (mm):", self.m_diameter,
            "Bare collector diameter. The fiber is wrapped around this radius, "
            "and each layer sits one fiber diameter farther out.", tips)
        # WHERE ON THE ROD, in two rows: which end the offset is measured from,
        # and how far. Not an "end margin": that word only worked while the rod
        # was exactly as long as the print plus two margins, and since the rod
        # became the machine's own number a longer rod has three answers.
        self._mandrel_combo(
            box, 1, "Position on mandrel:", placement_menu_var(self.m_place),
            _PLACEMENT_LABELS,
            "Where the tube sits along the mandrel. The mandrel's LENGTH is the "
            "printer's (Printer & collector -> Rotary collector), so this is "
            "what decides which part of it gets printed on: a distance in from "
            "the left (-X) end, the same in from the right, or centered with "
            "equal bare mandrel at both ends. Leaving bare mandrel matters because the "
            "first fiber has to land on collector rather than off the tip of "
            "it. With no rotary stage declared the mandrel is derived as the print "
            "plus this offset at each end, and all three choices coincide.",
            tips)
        self._m_place_offset_entry = self._mandrel_field(
            box, 2, "Offset (mm):", self.m_place_offset,
            "How far in from the end named above. Ignored when the tube is "
            "centered, because centering is what decides the distance.", tips)
        self.m_place.trace_add("write", lambda *_a: self._sync_place_offset())
        self._sync_place_offset()

    def _sync_place_offset(self):
        """Grey the offset when the tube is CENTRED -- centring computes the
        distance, so an editable field there would silently do nothing."""
        entry = getattr(self, "_m_place_offset_entry", None)
        if entry is None:
            return
        try:
            entry.configure(
                state="disabled" if self._mandrel_place() == "centre"
                else "normal")
        except tk.TclError:                  # destroyed by a page rebuild
            pass

    def _build_mandrel_geometry_fields(self, box, tips=None):
        """The 4-axis form: what you WANT, then what the software resolved.

        Row 0 is the PATTERN, because it decides which of the rows below it
        apply, and row 1 is the DESIGN MODE ("Design by", diamond helix only),
        which decides the rest: in `pore` mode the user states the opening
        they want and the winding angle is an output; in `angle` mode the
        winding angle is typed on row 2 and the pore is whatever falls out.  Rows are registered on both axes (`_mandrel_field`'s `families`
        and `modes` arguments) and swapped by `_apply_mandrel_visibility`; an
        empty grid row has zero height in Tk, so the visible rows close up.

        Only the outcome lives here, and in angle mode the winding angle IS
        the outcome asked for.  The solver's resolved omega, the closure
        ladder, the two search bounds, the speed policy and the calibrated
        placement are all in ADVANCED (`_build_mandrel_closure_fields`),
        collapsed by default.  Two shape knobs deliberately stay in this column: the wave
        amplitude and the rib angle are what their own family is FOR, and the
        chevron cannot validate without an amplitude at all
        (`mew_unit_cell._resolve_params`), so a required field is never hidden.

        Tube length is tube-only; Layers, Print speed and Fiber diameter are the
        SHARED scaffold vars shown again here, so the 4-axis form reads as one
        grid instead of sending the user to a section that is hidden in this
        mode.  Two widgets, one variable -- which is why the diameter entry is
        registered for ``_apply_diam_mode`` to grey."""
        self._mandrel_vars_make()
        v = self.scaf_vars[0]
        cells = MANDREL_CELL_FAMILIES
        box.columnconfigure(4, weight=1)
        # Column 0 holds the LABELS (sticky="e"); the slack belongs to the
        # spacer column above. Pin column 0 to no weight so a parent that
        # weights it -- ModuleGrid.add does, on every card body -- cannot split
        # the slack with the label column and shove the form right.
        box.columnconfigure(0, weight=0)
        fam_combo = self._mandrel_combo(
            box, 0, "Pattern:", self._m_family_label,
            [lbl for _, lbl in MANDREL_FAMILY_CHOICES], _FAMILY_INFO, tips)
        # hovering a pattern in the list shows its unit cell
        import gui_cell_hover
        gui_cell_hover.attach_combobox(
            fam_combo, MANDREL_FAMILY_LBL2VAL,
            graph_of=lambda: self.m_cell_graph.get())
        # -- diamond helix: WHAT the user designs to ------------------------- #
        # Here, not in Advanced: angle mode worked all along, but
        # its switch and its omega lived only in the collapsed Advanced card,
        # so a user saw pore-size design and nothing else. A cell family is
        # tiled and has no helix to resolve, so the choice is the helix's.
        F = self._mandrel_form(box)
        # No mode switch here since the end-turn closure: the
        # default `turn` mode takes the angle, the length AND the pore, and
        # builds all three. The two length-closing modes are an Advanced
        # choice (`_build_mandrel_closure_fields`), and a preset in one of
        # them shows its own rows here.
        self._mandrel_field(
            box, 2, "Winding angle \u03c9 (deg):", self.m_angle,
            "The fiber's angle from the mandrel axis, 0 along it and 90 a "
            "ring. Built exactly as typed.",
            tips, families=("zigzag",), modes=("turn", "angle"))
        self._mandrel_field(
            box, 3, self._mandrel_length_label() + " (mm):", self.m_length,
            "How long a tube you want. Built exactly as typed: the mandrel "
            "turns at each end onto the next pivot, so no length has to "
            "close the helix. (In Advanced's length-closing modes, the "
            "nearest length that closes is built instead.)", tips)
        # -- the ONE pore number --------------------------------------------- #
        self._mandrel_field(
            box, 4, "Pore size (\u00b5m):", self.m_pore_target,
            "The opening you want, stated as the side of a square of the same "
            "area. The software builds the nearest one this angle and mandrel "
            "can make (the pore is set by the number of pivot points around "
            "the tube).", tips, families=("zigzag",), modes=("turn", "pore"))
        # -- cell families only --------------------------------------------- #
        self._mandrel_field(
            box, 5, "Cell along the tube (mm):", self.m_cell_w,
            "The repeating cell's size along the mandrel axis.",
            tips, families=cells)
        self._mandrel_field(
            box, 6, "Cell around the tube (mm):", self.m_cell_h,
            "The repeating cell's size around the circumference, as arc "
            "length. A whole number of them has to close, so it is a request.",
            tips, families=cells)
        self._mandrel_field(
            box, 7, "Rib angle \u03b8 (deg):", self.m_rib_angle,
            "How far the re-entrant bow-tie is folded, measured from the "
            "mandrel axis. Strictly between 0 and 90.",
            tips, families=("reentrant",))
        self._mandrel_field(
            box, 8, "Wave amplitude (mm):", self.m_cell_amplitude,
            "How far the circumferential fibers wave along the axis. 0 leaves "
            "the rings straight; the chevron needs a positive value.",
            tips, families=MANDREL_WAVE_FAMILIES)
        # -- shared with the 3-axis designer -------------------------------- #
        self._mandrel_field(
            box, 9, "Layers:", v["layers"],
            "Layers of wall, the same field as the 3-axis designer's Layers. "
            "Any whole number from 1. A layer is one whole sheet of the "
            "pattern: a winding circuit out and back over every pivot, or one "
            "pass over every cell. The nozzle steps out one fiber diameter "
            "between layers.", tips)
        import gui_units
        if gui_units.enabled():
            import gui_speed_ratio
            gui_speed_ratio.compact_row(self, self._mandrel_form(box), v, 10)
        else:
            self._mandrel_field(
                box, 10, "Print speed (mm/min):", v["speed"],
                "Resultant deposition-surface speed, not an X-axis speed. The same "
                "field as the 3-axis designer's Print speed.", tips)
        # The fiber diameter is greyed and physics-driven under exactly the same
        # rule as the 3-axis field (the Fiber diameter law on), so it must be
        # greyed by the same code -- hence the second registered entry.
        v["diameter_entry_tube"] = self._mandrel_field(
            box, 11, "Fiber diameter (mm):", v["diameter"],
            "The deposited fiber diameter. Predicted from the Physics page "
            "while its Fiber diameter card's switch is on, and not editable "
            "here.", tips)
        # the "From Physics" jump (gui_sim); absent on a stub or the classic UI
        link = getattr(self, "_diam_link", None)
        if link is not None:
            link(v["diameter_entry_tube"])
        # -- what it resolved: one table, the span note, the ladder ---------- #
        # Every number in it comes off the design object the generator will
        # actually use; nothing here re-derives a resolved value.
        # A computed readout, not a parameter: one slot on the form's label
        # edge, exempt inside (its cells are drawn at run time).
        import gui_form
        self._m_resolved = gui_form.exempt(
            ttk.Frame(box), "resolved-design table: values the generator "
                            "computed, drawn at run time")
        F.slot(None, self._m_resolved, row=12, sticky="ew")
        self._m_resolved.columnconfigure(0, weight=1)
        # ... and the one-line sentence form, which is ALSO where a refusal is
        # printed verbatim.  Shown only when there is no table to show, so the
        # page never states the same number twice.
        self._m_readout_label = F.live(
            None, textvariable=self.m_cell_readout, row=13, wrap=440).field
        self._install_mandrel_readout_traces()
        self._apply_mandrel_family()

    def _build_mandrel_closure_fields(self, box, tips=None):
        """ADVANCED: the overrides the design mode owns, and placement. The
        mode switch itself is the Tube card's "Design by" row.

        Both shells put this behind a collapsed group (a `gui_widgets.
        Disclosure` in the modern shell, a notebook tab in the classic developer
        UI), so nothing in here is needed to design a tube -- it is the escape
        hatch from the outcome-first page back to the parameter-first one, plus
        the two calibrated-placement radii that have always lived here
        (``mew_calibration.py:314`` keeps them off the Physics page on purpose).

        `m_strategy` is still not shown: `MANDREL_STRATEGIES` has one value, so
        a combobox for it would be visible-and-inert, which is the arrangement
        the tab merge settled against. It stays persisted
        and still reaches the argv.

        The name is unchanged so both shells keep calling it; what it BUILDS is
        the Advanced group."""
        self._mandrel_vars_make()
        box.columnconfigure(3, weight=1)
        # Column 0 holds the LABELS (sticky="e"); the slack belongs to the
        # spacer column above. Pin column 0 to no weight so a parent that
        # weights it -- ModuleGrid.add does, on every card body -- cannot split
        # the slack with the label column and shove the form right.
        box.columnconfigure(0, weight=0)
        F = self._mandrel_form(box)
        # HOW THE HELIX CLOSES. The default turns the mandrel at each end, so
        # the Tube card's angle, pore and length are all built as typed. The
        # other two are McColl's Eq 3.1, closing by LENGTH with no end turn:
        # only a ladder of lengths closes, so one of the Tube card's numbers
        # has to give.
        close_tip = (
            "Turn at the ends: the angle, pore and length you type are all "
            "built; after each pass the mandrel turns, without moving along "
            "the axis, onto the next pivot, which lays a short ring of fiber "
            "at each end. The two others close the helix by LENGTH alone "
            "(McColl 2018), so the length you get is the nearest that closes: "
            "Length closes, pore picked solves the angle for your pore; "
            "Length closes, angle picked keeps your angle and the pore is "
            "whatever that length allows.")
        close = F.group("Closure", close_tip)
        for text, token in (("Turn at the ends", "turn"),
                            ("Length closes, pore picked", "pore"),
                            ("Length closes, angle picked", "angle")):
            close.add_radio(text, self.m_design_mode, token, close_tip)
        self._mandrel_family_register(("zigzag",), *close.widgets)
        # The typed omega is in the Tube card (`_build_mandrel_geometry_fields`).
        # What stays is the
        # solver's own omega, shown greyed in pore mode so the ladder can be
        # checked against it; in angle mode the Tube card's row is the one.
        omega = self._mandrel_field(
            box, None, "Winding angle \u03c9 (deg):", self.m_angle,
            "Angle from the mandrel axis, as the pore solver resolved it. To "
            "type it yourself, pick Turn at the ends or Length closes, angle "
            "picked above.", tips, families=("zigzag",), modes=("pore",))
        # eta and Rm are worked out for you in both modes; this is the one way
        # to type them, and only in winding-angle mode (pore mode resolves the
        # angle too, so a typed pair there would be a third design mode).
        own = F.check(
            "Set \u03b7 and Rm myself", self.m_closure_override,
            "Off: the software picks \u03b7 and Rm, the closing length "
            "nearest yours, and the ladder offers the other pore sizes. "
            "On: the two numbers below are built as typed, whatever "
            "length they give.")
        self._mandrel_register_row(own, ("zigzag",), ("angle",))
        eta = self._mandrel_field(
            box, None, "\u03b7 divisor:", self.m_eta,
            "McColl's closure divisor: the pattern goes around \u03b7 times "
            "per pass, which sets the diamonds around the tube. Blank takes "
            "the nearest closing length. Diamond helix only.", tips,
            families=("zigzag",), modes=("pore", "angle"))
        rm = self._mandrel_field(
            box, None, "Extra revolutions Rm:", self.m_rm,
            "Whole extra revolutions per axial iteration, with closure "
            "preserved. Diamond helix only.", tips, families=("zigzag",),
            modes=("pore", "angle"))
        self._mandrel_solver_register(omega, eta, rm)
        tol = self._mandrel_field(
            box, None, "Length may miss by (%):", self.m_length_tol,
            "How far the closing length may fall from your request. Raise it "
            "to buy a finer pore, or more pore sizes to choose from.", tips,
            families=("zigzag",), modes=("pore", "angle"))
        # appended IN PLACE, like the other registries: the G-code page's
        # `_View` re-runs this builder and shares the App's list only so
        self._mandrel_auto_widgets = getattr(self, "_mandrel_auto_widgets", [])
        self._mandrel_auto_widgets.append(tol)
        self._mandrel_field(
            box, None, "\u03b7 search cap:", self.m_eta_cap,
            "The largest \u03b7 the pore search will try. Blank uses the "
            "solver's own ceiling.", tips, families=("zigzag",),
            modes=("pore",))
        self._build_mandrel_rig_fields(box, tips, F)
        self._apply_mandrel_family()

    def _build_mandrel_rig_fields(self, box, tips=None, F=None):
        """The half of Advanced that is the RIG's, not one tube's: the speed
        policy, the feeds and the calibrated placement. The G-code page's
        Advanced panel is this alone; a tube's closure is a row of its
        Parameters table there."""
        if F is None:
            self._mandrel_vars_make()
            box.columnconfigure(3, weight=1)
            box.columnconfigure(0, weight=0)
            F = self._mandrel_form(box)
        F.combo("Speed policy", self.m_speed_mode, ("surface", "omega"),
                "How the feed is held as the wall grows. surface: the "
                "deposition-surface speed stays constant, so every layer is "
                "drawn at the same rate. omega: the mandrel's angular rate "
                "stays constant, so the surface speed drifts up by "
                "(R0 + h)/R0 as the wall builds.")
        feed_tip = (
            "Only for a printer whose flavor splits the feed (Aerotech, set "
            "on the Printer & collector page): F is the linear (X) rate and E the rotary "
            "rate in degrees.\n\n"
            "auto: both are worked out for every move from Speed, so each "
            "move, every strut of a cell included, is drawn at the surface "
            "speed you set.\n\n"
            "fixed: your own F and E, written on every move as you type "
            "them, in the Printer & collector page's feed time unit. Speed then sets no "
            "move's speed. Exact for a constant-angle helix; a cell's struts "
            "will run at different surface speeds.")
        F.combo("Feeds", self.m_feed_mode, ("auto", "fixed"), feed_tip)
        fg = F.group("Fixed F and E", feed_tip, indent=1)
        fg.add_entry("F", self.m_fixed_f, feed_tip, width="small")
        fg.add_entry("E", self.m_fixed_e, feed_tip, width="small")
        self._m_fixed_row = fg

        def _sync_fixed(*_a):
            on = self.m_feed_mode.get() == "fixed"
            for w in fg.widgets:
                w.grid() if on else w.grid_remove()
        self.m_feed_mode.trace_add("write", _sync_fixed)
        _sync_fixed()
        F.section("Calibrated placement (optional)")
        self._mandrel_field(
            box, None, "Support S (mm):", self.m_support,
            "S in Neuhaus Eq 4, measured from the mandrel AXIS. Blank -> the "
            "geometric r + 0.75*gap.", tips)
        self._mandrel_field(
            box, None, "Measured R_ax (mm):", self.m_lag_axial,
            "The measured lag radii at YOUR calibration speed and gap, "
            "written by Calibrate's “Jet lag (mandrel)” type. With the speed "
            "they were measured at (Physics page, Jet lag card) they set the "
            "4-axis lag: R_ax at that speed, carried to others by the lag "
            "law, and R_cir as a fraction of it. Without the speed only their "
            "RATIO is used, in place of the geometric r/(r+H). Flat prints "
            "never read them.", tips)
        self._mandrel_field(
            box, None, "Measured R_cir (mm):", self.m_lag_circ,
            "See R_ax: supplied as a pair, read as a ratio. Leave both blank "
            "to use the parameter-free r/(r+H).", tips)

    # ------------------------------------------------------------------ #
    # preview (drives the SHARED scaffold preview panel; see gui_preview)
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # the drawn cell -- a graph editor over ONE canonical string
    # ------------------------------------------------------------------ #
    # THE CANVAS IS A VIEW, NEVER THE TRUTH.  ``m_cell_graph`` is the single
    # source, exactly as ``linear_pattern`` is for the 3-axis wave editor
    # (``gui_scaffold._build_scaffold_linear_fields``): every click rewrites the
    # string and the string's own trace redraws the canvas, so a preset load, a
    # typed edit and a drag cannot disagree about what is drawn.
    #
    # AND THE VALIDATOR IS THE ENGINE.  ``_cell_validate`` routes the drawing
    # through ``mew_unit_cell.preview_route`` -- the same tiler, graph and Euler
    # walk the worker runs -- rather than re-implementing a connectivity test
    # here.  That is the rule the footprint editor already follows by calling
    # ``scaffold_gcode._polygon_is_convex`` (``gui_scaffold._shape_is_convex``),
    # and it is what makes it impossible for the page to accept a cell the
    # generator would refuse, or to refuse one it would accept.
    #
    # THE VIEW IS FIXED, so there is no zoom/pan/fit state to persist and none
    # of ``_resolve_view``'s machinery is needed: the authoring domain IS the
    # unit square, and the margin around it shows the four neighbouring tiles as
    # ghosts, because the weld across u = 1 and v = 1 is the one thing about
    # this format that is not visible from the numbers.
    _CELL_CANVAS = 340       # px, square
    _CELL_MARGIN = 50        # px of ghost margin on each side
    _CELL_HIT = 8            # px, how near a click must be to grab something

    #: px of margin around the 3x3 block in the 9-cell view. No ghost band is
    #: needed there -- the neighbours ARE the drawing -- but the two axis
    #: captions still need somewhere to sit that is not on top of a tile.
    _CELL_MARGIN9 = 26

    @property
    def _cell_side(self):
        """ONE cell, in px, at the current zoom."""
        if self._cell_zoom == 9:
            return (self._CELL_CANVAS - 2 * self._CELL_MARGIN9) / 3.0
        return self._CELL_CANVAS - 2 * self._CELL_MARGIN

    @property
    def _cell_zoom(self):
        """9 for the 3x3 block, 1 for the single cell. Anything else reads 1."""
        try:
            return 9 if self.m_cell_view.get().strip() == "9" else 1
        except (AttributeError, tk.TclError):   # pragma: no cover -- destroyed
            return 1

    @property
    def _cell_origin(self):
        """Canvas px of the AUTHORED cell's (u = 0, v = 1) corner.

        The 9-view puts the authored cell in the middle, so it starts one cell
        in from the block; the 1-view keeps the ghost margin it always had.
        Every hit test goes through `_cell_to_px`, so both views pick the same
        way for free."""
        if self._cell_zoom == 9:
            return (self._CELL_MARGIN9 + self._cell_side,
                    self._CELL_MARGIN9 + self._cell_side)
        return self._CELL_MARGIN, self._CELL_MARGIN

    def _cell_to_px(self, u, v):
        """Unit-cell (u, v) -> canvas pixels. v runs UP the screen, so the cell
        reads the way the unwrapped plane is drawn everywhere else."""
        (x0, y0), s = self._cell_origin, self._cell_side
        return x0 + u * s, y0 + (1.0 - v) * s

    def _cell_from_px(self, px, py):
        """Canvas pixels -> snapped unit-cell (u, v), unclamped.

        Unclamped and in the AUTHORED cell's frame at both zooms, so a click on
        a neighbour in the 9-view lands outside 0..1 and `_cell_press`'s own
        bounds guard rejects it: the block is there to be read, and only the
        middle tile is edited."""
        (x0, y0), s = self._cell_origin, self._cell_side
        g = self._cell_grid_step()
        u, v = (px - x0) / s, 1.0 - (py - y0) / s
        return round(u / g) * g, round(v / g) * g

    def _cell_grid_step(self):
        """Snap step in CELL FRACTIONS; a garbled entry falls back to 0.25."""
        try:
            g = float(self.m_cell_grid.get())
        except (ValueError, tk.TclError):
            return 0.25
        return g if 0.0 < g <= 1.0 else 0.25

    def _cell_graph_pair(self):
        """The current drawing as mutable ``(nodes, edges)``, or ``([], [])``.

        A drawing that does not parse yields empty lists rather than raising:
        the canvas has to keep drawing while the user is mid-edit, and the
        status line is where the refusal is said."""
        import mew_unit_cell
        try:
            nodes, edges = mew_unit_cell.parse_cell_graph(
                self.m_cell_graph.get())
        except mew_unit_cell.UnitCellError:
            return [], []
        return list(nodes or []), list(edges or [])

    def _cell_write(self, nodes, edges):
        """Write a drawing back to the canonical string; its trace redraws."""
        import mew_unit_cell
        self._m_cell_orphan = None
        if not nodes or not edges:
            self.m_cell_graph.set("")
            return
        self.m_cell_graph.set(mew_unit_cell.format_cell_graph(nodes, edges))

    def _cell_hit_node(self, nodes, px, py):
        """Index of the node within ``_CELL_HIT`` px of the click, or None."""
        best, bestd = None, float(self._CELL_HIT) ** 2
        for i, (u, v) in enumerate(nodes):
            sx, sy = self._cell_to_px(u, v)
            d = (sx - px) ** 2 + (sy - py) ** 2
            if d <= bestd:
                best, bestd = i, d
        return best

    def _cell_hit_edge(self, nodes, edges, px, py):
        """Index of the edge whose SEGMENT passes within ``_CELL_HIT`` px."""
        best, bestd = None, float(self._CELL_HIT)
        for i, (a, b, _role) in enumerate(edges):
            ax, ay = self._cell_to_px(*nodes[a])
            bx, by = self._cell_to_px(*nodes[b])
            dx, dy = bx - ax, by - ay
            den = dx * dx + dy * dy
            if den <= 0.0:
                continue
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / den))
            d = math.hypot(ax + t * dx - px, ay + t * dy - py)
            if d <= bestd:
                best, bestd = i, d
        return best

    def _cell_press(self, event):
        """Canvas mouse-down, by mode.

        node   -- grab an existing node to drag, or drop a new snapped one.
        edge   -- click two nodes in turn to join them.
        remove -- delete the node under the cursor (and every edge touching it,
                  with the remaining edges REINDEXED), or the edge under it.
        """
        nodes, edges = self._cell_graph_pair()
        if not nodes:
            nodes = list(getattr(self, "_m_cell_orphan", None) or [])
        mode = self.m_cell_editmode.get()
        hit = self._cell_hit_node(nodes, event.x, event.y)

        if mode == "remove":
            if hit is not None:
                edges = [(a - (a > hit), b - (b > hit), r)
                         for a, b, r in edges if hit not in (a, b)]
                del nodes[hit]
                self._cell_write(nodes, edges)
            else:
                ei = self._cell_hit_edge(nodes, edges, event.x, event.y)
                if ei is not None:
                    del edges[ei]
                    self._cell_write(nodes, edges)
            self._m_cell_pending = None
            self._cell_redraw()
            return

        if mode == "edge":
            if hit is None:
                self._m_cell_pending = None
            elif self._m_cell_pending is None:
                self._m_cell_pending = hit
            elif self._m_cell_pending != hit:
                a, b = self._m_cell_pending, hit
                key = (min(a, b), max(a, b))
                if not any((min(x, y), max(x, y)) == key for x, y, _r in edges):
                    edges.append((a, b, "long"))
                    self._cell_write(nodes, edges)
                self._m_cell_pending = None
            else:
                self._m_cell_pending = None
            self._cell_redraw()
            return

        # "node"
        if hit is not None:
            self._m_cell_drag = hit
            return
        u, v = self._cell_from_px(event.x, event.y)
        if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
            return
        if any(abs(u - nu) < 1e-9 and abs(v - nv) < 1e-9 for nu, nv in nodes):
            return                              # a node is already there
        nodes.append((u, v))
        if edges:
            self._cell_write(nodes, edges)
        else:
            # A drawing with no edge yet cannot be written to the canonical
            # string (the format has none), so the nodes live here until the
            # first Join gives them one. Held on the instance rather than in
            # the string precisely so the string never holds an unprintable
            # half-state a preset could save.
            self._m_cell_orphan = nodes
            self._cell_redraw()

    def _cell_drag(self, event):
        if self._m_cell_drag is None:
            return
        nodes, edges = self._cell_graph_pair()
        if not nodes:
            nodes = list(getattr(self, "_m_cell_orphan", None) or [])
        if not 0 <= self._m_cell_drag < len(nodes):
            self._m_cell_drag = None
            return
        u, v = self._cell_from_px(event.x, event.y)
        u, v = min(1.0, max(0.0, u)), min(1.0, max(0.0, v))
        if any(i != self._m_cell_drag and abs(u - nu) < 1e-9
               and abs(v - nv) < 1e-9 for i, (nu, nv) in enumerate(nodes)):
            return                              # would land on another node
        nodes[self._m_cell_drag] = (u, v)
        if edges:
            self._cell_write(nodes, edges)
        else:
            self._m_cell_orphan = nodes
            self._cell_redraw()

    def _cell_release(self, _event=None):
        self._m_cell_drag = None

    def _cell_reset(self):
        """Back to the shipped honeycomb -- the drawing a fresh page opens on."""
        self._m_cell_pending = None
        self._m_cell_orphan = None
        self.m_cell_graph.set(MANDREL_DEFAULT_CELL)

    def _cell_clear(self):
        self._m_cell_pending = None
        self._m_cell_orphan = None
        self.m_cell_graph.set("")

    def _cell_toggle_free(self):
        """Flip every edge between load-bearing and free-to-retrace.

        Roles are the RETRACE PRICE, not decoration: a free edge costs 1.0 to
        lay twice and a load-bearing one 1000, so this changes which members
        the Euler walk chooses to double, and the retraced figure in the status
        line moves with it.
        """
        nodes, edges = self._cell_graph_pair()
        if not edges:
            return
        flip = {"ring": "long", "long": "ring"}
        self._cell_write(nodes, [(a, b, flip.get(r, "long"))
                                 for a, b, r in edges])

    def _cell_n_around(self):
        """How many cells the circumference takes, the way the engine rounds it.

        Spelled from the same two fields `mew_unit_cell.design_tube_cells` reads,
        so the validator tiles the cell the generator will tile.
        """
        return max(2, int(round(math.pi * float(self.m_diameter.get())
                                / float(self.m_cell_h.get()))))

    def _cell_preview_route(self):
        """The three-cell tile, routed -- ``(points, diagnostics)``.

        Measured on this branch: the full tube is superlinear (599 ms at 320
        cells, 2.1 s at 600) while three cells is 1.7 ms, and every per-length
        figure has converged by three -- the retraced fraction exactly, the
        under-CTS score to 0.2 points. `reversals` is the one that does NOT
        transfer, so callers report it per cell.
        """
        import mew_unit_cell
        nodes, edges = mew_unit_cell.parse_cell_graph(self.m_cell_graph.get())
        if not nodes or not edges:
            return None, None
        return mew_unit_cell.preview_route(
            nodes, edges, float(self.m_diameter.get()),
            float(self.m_cell_w.get()), self._cell_n_around(), n_axial=3)

    def _cell_spliced(self):
        """The drawing with its separate pieces joined at their crossings.

        ``(nodes, edges, changed)``, or ``(None, None, False)`` when there is
        nothing to read. `changed` is compared on the CANONICAL STRING, not on
        the node count: the repair that costs zero new nodes -- re-routing a
        member through an apex already sitting on it -- is the best outcome
        there is, and counting nodes would report it as having done nothing.
        """
        import mew_unit_cell
        try:
            nodes, edges = mew_unit_cell.parse_cell_graph(
                self.m_cell_graph.get())
        except mew_unit_cell.UnitCellError:
            return None, None, False
        if not nodes or not edges:
            return None, None, False
        n2, e2, _added = mew_unit_cell.splice_cell_crossings(nodes, edges)
        changed = (mew_unit_cell.format_cell_graph(n2, e2)
                   != mew_unit_cell.format_cell_graph(nodes, edges))
        return n2, e2, changed

    def _cell_splice_would_fix(self):
        """True if pressing Join crossings turns a refusal into a print.

        Asked by ROUTING the spliced drawing, not by counting its pieces: the
        page never claims printability on its own, and there is a case
        (`mew_unit_cell.cell_connect_fault`'s winding-parity note) where the
        pieces look joined and the engine still refuses. Only ever reached on
        a drawing that has already failed, so it costs nothing on the path
        that builds a preview.
        """
        import mew_unit_cell
        n2, e2, changed = self._cell_spliced()
        if not changed:
            return False
        try:
            mew_unit_cell.preview_route(
                n2, e2, float(self.m_diameter.get()),
                float(self.m_cell_w.get()), self._cell_n_around(), n_axial=3)
        except (mew_unit_cell.UnitCellError, ValueError, ZeroDivisionError,
                tk.TclError):
            return False
        return True

    def _cell_join_crossings(self):
        """Splice a node in wherever two separate pieces of the drawing cross.

        The button behind the status line's own advice. It rewrites the
        canonical string, so the canvas, the validator and the worker all see
        the repair at once -- and it is undoable by the same route anything
        else on this canvas is, by editing the string.

        Silent when there is nothing to do: `_cell_redraw` restates the
        verdict either way, and a drawing whose pieces never cross has no
        junction to add, which the status line says in words.
        """
        n2, e2, changed = self._cell_spliced()
        if not changed:
            self._cell_redraw()
            return
        self._m_cell_pending = None
        self._cell_write(n2, e2)

    def _cell_why_refused(self, engine_msg):
        """The engine's refusal, plus what to DO about it.

        The engine reports the size of the piece its Euler walk landed in --
        "covered 18 of 108 traversals" -- which is true and says nothing about
        the cause. `cell_connect_fault` names the cause; this adds the repair
        when there is one. The engine's own text stays FIRST and verbatim: it
        is the verdict, and the rest is commentary on it.
        """
        import mew_unit_cell
        if "not connected" not in engine_msg:
            return engine_msg
        try:
            nodes, edges = mew_unit_cell.parse_cell_graph(
                self.m_cell_graph.get())
        except mew_unit_cell.UnitCellError:
            return engine_msg
        if not nodes or not edges:
            return engine_msg
        _code, why = mew_unit_cell.cell_connect_fault(nodes, edges)
        parts = [engine_msg]
        if why:
            parts.append(why)
        if self._cell_splice_would_fix():
            parts.append("Press “Join crossings” to put a node where they "
                         "cross. The lines do not move.")
        elif why:
            parts.append("Nothing crosses, so there is no node to add: give a "
                         "boundary node its twin on the opposite edge, or join "
                         "the pieces yourself.")
        return "\n".join(parts)

    def _cell_validate(self):
        """``(ok, message)`` for the drawing as it stands -- THE ENGINE'S ANSWER.

        `ok` is True, False, or None for "nothing drawn yet", which the status
        label paints grey rather than red: an empty page is not a mistake.
        """
        import mew_unit_cell
        _EMPTY = "draw a cell: click to place nodes, then join them"
        if not self.m_cell_graph.get().strip():
            return None, _EMPTY
        try:
            pts, diag = self._cell_preview_route()
        except (mew_unit_cell.UnitCellError, ValueError, ZeroDivisionError,
                tk.TclError) as exc:
            return False, self._cell_why_refused(str(exc))
        if pts is None:
            return None, _EMPTY
        retr = 100.0 * diag["retraced_length"] / diag["total_length"]
        return True, ("prints as one fiber · %.1f %% of it laid twice "
                      "· %.1f mandrel reversals per cell along the axis"
                      % (retr, diag["reversals"] / 3.0))

    def _cell_speed_note(self):
        """The bend-tightness line, or "" when its inputs are not readable.

        THE MACHINE NUMBER IS THE X GANTRY'S. ``machine_model.RotaryAxis``
        carries no motion limits at all, so the acceleration here is the Machine
        page's ``sim_max_accel`` applied to the SURFACE path -- an optimistic
        bound, because a mandrel is a rotating inertia and nobody has measured
        its acceleration (the rotary axis is an opt-in ``manual_stepper`` the
        real rig does not have; ``pi_sim/config/mandrel_probe.cfg``). The line
        names the numbers it used for exactly that reason. When the Machine
        page's Rotary accel is set, the plan splits the two limits the way the
        player and export do (`mew_kinematics.axis_seg_accels`), and the line
        says so.

        It is read whether or not the accel-planner checkbox is on, because that
        checkbox governs whether the EXPORT is re-timed; this is an advisory
        about the machine, and a cell is no less cornered for the box being off.
        """
        import mew_kinematics
        import mew_unit_cell
        try:
            pts, _diag = self._cell_preview_route()
            if pts is None:
                return ""
            accel = float(self.sim_max_accel.get())
            scv = float(self.sim_scv.get())
            rot_var = getattr(self, "sim_rotary_accel", None)
            rot_accel = float(rot_var.get() or 0.0) if rot_var is not None else 0.0
            radius = 0.5 * float(self.m_diameter.get())
            # `_mandrel_speed` is the page's Speed field, which is MM/MIN --
            # `scaffold_gcode --speed` is mm/min and is emitted as F verbatim.
            # `plan_trajectory` wants mm/min too, so it is passed straight
            # through; only the comparison against CTS is in mm/s.
            feed_mm_min = self._mandrel_speed()
            feed = feed_mm_min / 60.0
            cts_var = getattr(self, "cal_vars", {}).get("cal.cts_ref")
            cts = float(cts_var.get()) if cts_var is not None else 7.5
        except (mew_unit_cell.UnitCellError, AttributeError, ValueError,
                ZeroDivisionError, tk.TclError):
            return ""
        if not (accel > 0.0 and feed > 0.0 and cts > 0.0):
            return ""
        # (u, v) on the bare surface: u is the X travel, v / R the rotation
        seg_accel = None
        if rot_accel > 0.0 and radius > 0.0:
            du = [abs(b[0] - a[0]) for a, b in zip(pts, pts[1:])]
            dv = [abs(b[1] - a[1]) for a, b in zip(pts, pts[1:])]
            seg_accel = mew_kinematics.axis_seg_accels(
                [math.hypot(x, y) for x, y in zip(du, dv)], du,
                [math.degrees(y / radius) for y in dv], accel, rot_accel)
        traj = mew_kinematics.plan_trajectory(
            [(u, v, 0.0) for u, v in pts], [feed_mm_min] * (len(pts) - 1),
            max_accel=accel, square_corner_velocity=scv, seg_accel=seg_accel)
        if traj.total_time <= 0.0:
            return ""
        slow = 0.0
        for mv in traj.moves:
            for v0, v1, dur in ((mv.entry_v, mv.peak_v, mv.t_acc),
                                (mv.peak_v, mv.peak_v, mv.t_cruise),
                                (mv.peak_v, mv.exit_v, mv.t_dec)):
                if dur <= 0.0:
                    continue
                if v0 < cts and v1 < cts:
                    slow += dur
                elif v0 < cts or v1 < cts:
                    slow += dur * abs(cts - min(v0, v1)) / abs(v1 - v0)
        pct = 100.0 * slow / traj.total_time
        # Below CTS the jet coils instead of laying a straight fiber, so the
        # number is only a warning when there IS one; at zero the same sentence
        # would describe a defect the cell does not have.
        head = ("the whole layer runs at or above CTS" if pct < 0.5 else
                "%.0f %% of the layer runs below CTS, where the jet coils "
                "rather than laying straight" % (pct,))
        if seg_accel is not None:
            return ("%s (CTS %.3g mm/s, feed %.4g mm/min = %.3g mm/s, accel "
                    "%.0f mm/s² linear and %.0f deg/s² rotary, from the "
                    "Printer & collector page)"
                    % (head, cts, feed_mm_min, feed, accel, rot_accel))
        return ("%s (CTS %.3g mm/s, feed %.4g mm/min = %.3g mm/s, accel "
                "%.0f mm/s² from the Printer & collector page's X axis; the rotary "
                "axis has no acceleration limit set)"
                % (head, cts, feed_mm_min, feed, accel))

    def _cell_size_note(self):
        """What the square on the canvas is WORTH, in mm -- or "" if unreadable.

        The canvas is drawn in cell FRACTIONS, so on its own it says nothing
        about size, and the two fields that do set the size ("Cell along/around
        the tube") sit in another section entirely. That gap is what makes the
        Snap box look like a size control. This line closes it.

        It calls `mew_unit_cell.resolve_cell_size`, which is the arithmetic
        `design_tube_cells` itself runs -- NOT a re-derivation of it -- and
        which does no tiling and no routing, so it is free enough to run on
        every keystroke where `_mandrel_cell_design` (a whole Euler circuit)
        would not be.

        The pin is read RAW rather than through `_mandrel_cell_counts`, because
        settling the ladder means building six routed designs and this runs
        inside an undebounced trace. A stale pin is settled by the panel on its
        own refresh; the worst case here is a caption one rung out for one
        repaint, and the caption is advisory.
        """
        import mew_unit_cell
        pick = (self.m_cell_pick.get() or "").strip().split(",")
        n_around = n_axial = None
        fit = (self.m_cell_fit.get() or "").strip() or "scale"
        if len(pick) == 3:
            try:
                n_around, n_axial, fit = int(pick[0]), int(pick[1]), pick[2]
            except ValueError:                  # pragma: no cover -- hand-edited
                n_around = n_axial = None
        try:
            req_w = float(self.m_cell_w.get())
            req_h = float(self.m_cell_h.get())
            size = mew_unit_cell.resolve_cell_size(
                float(self.m_diameter.get()), float(self.m_length.get()),
                req_w, req_h, fit=fit, n_around=n_around, n_axial=n_axial)
        except (mew_unit_cell.UnitCellError, ValueError, ZeroDivisionError,
                tk.TclError):
            return ""
        head = ("One square = %.3g mm along × %.3g mm around  "
                "(%d cells around the tube, %d along, tube %.3g mm)"
                % (size.cell_w, size.cell_h, size.n_around, size.n_axial,
                   size.length))
        if self._cell_zoom == 9:
            head += "\nThe block shown = %.3g × %.3g mm" % (
                3.0 * size.cell_w, 3.0 * size.cell_h)
        # The fit is allowed to move BOTH numbers, and "I asked for 2 and the
        # tube says 1.97" is exactly the confusion this caption exists to end.
        moved = []
        if abs(size.cell_w - req_w) > 5e-4:
            moved.append("along %.3g → %.3g" % (req_w, size.cell_w))
        if abs(size.cell_h - req_h) > 5e-4:
            moved.append("around %.3g → %.3g" % (req_h, size.cell_h))
        if moved:
            head += ("\nThe '%s' fit moved your request so a whole number of "
                     "cells closes: %s mm." % (fit, "; ".join(moved)))
        return head

    def _cell_redraw(self, *_a):
        """Repaint the canvas from the canonical string, and restate validity."""
        canvas = getattr(self, "_m_cell_canvas", None)
        if canvas is None:
            return
        try:
            canvas.delete("all")
        except tk.TclError:                     # pragma: no cover -- destroyed
            return
        nodes, edges = self._cell_graph_pair()
        if not nodes:
            nodes = list(getattr(self, "_m_cell_orphan", None) or [])
        (x0, y0), s = self._cell_origin, self._cell_side
        zoom = self._cell_zoom

        # The snap grid is drawn in the AUTHORED cell only. At 9 cells it would
        # be 9 grids at a third the pitch, which reads as hatching rather than
        # as a snap.
        g = self._cell_grid_step()
        for i in range(int(round(1.0 / g)) + 1):
            t = i * s * g
            canvas.create_line(x0 + t, y0, x0 + t, y0 + s, fill="#eeeeee")
            canvas.create_line(x0, y0 + t, x0 + s, y0 + t, fill="#eeeeee")

        # THE NEIGHBOURS. At zoom 1 they are the four ghosts that spill into the
        # margin -- a hint that the cell tiles. At zoom 9 they are the whole
        # point: eight real copies drawn in a tint, so the weld across u = 1 and
        # v = 1 stops being a claim about coordinates and becomes something the
        # eye checks. A drawing that does not stitch shows its gaps here.
        if zoom == 9:
            around = [(du, dv) for du in (-1, 0, 1) for dv in (-1, 0, 1)
                      if (du, dv) != (0, 0)]
            tint = {"ring": "#a8d5a8", "long": "#a9c4e0"}
            width = 2
        else:
            around = [(-1, 0), (1, 0), (0, -1), (0, 1)]
            tint = {"ring": "#dddddd", "long": "#dddddd"}
            width = 1
        for du, dv in around:
            for a, b, role in edges:
                ax, ay = self._cell_to_px(nodes[a][0] + du, nodes[a][1] + dv)
                bx, by = self._cell_to_px(nodes[b][0] + du, nodes[b][1] + dv)
                canvas.create_line(ax, ay, bx, by, fill=tint.get(role,
                                                                "#dddddd"),
                                   width=width)
            if zoom == 9:                       # each copy's own cell outline
                cx, cy = self._cell_to_px(du, dv + 1.0)
                canvas.create_rectangle(cx, cy, cx + s, cy + s,
                                        outline="#f0f0f0")

        canvas.create_rectangle(x0, y0, x0 + s, y0 + s, outline="#999999")
        # The captions ride the MARGIN, which at zoom 9 is the canvas edge
        # rather than the band around a lone cell -- anchored to the authored
        # cell they landed on top of the neighbouring tiles.
        if zoom == 9:
            ux, uy = 6, self._CELL_CANVAS - 7
            vx, vy = 6, 9
        else:
            ux, uy = x0 + s + 6, y0 + s + 14
            vx, vy = x0 - 6, y0 - 14
        canvas.create_text(ux, uy, anchor="w",
                           text="u → (along the axis)", fill="#999999",
                           font=("TkDefaultFont", 7))
        canvas.create_text(vx, vy, anchor="w",
                           text="v ↑ (around)", fill="#999999",
                           font=("TkDefaultFont", 7))

        for a, b, role in edges:
            ax, ay = self._cell_to_px(*nodes[a])
            bx, by = self._cell_to_px(*nodes[b])
            canvas.create_line(ax, ay, bx, by, width=2,
                               fill="#2ca02c" if role == "ring" else "#1f77b4")
        for i, (u, v) in enumerate(nodes):
            px, py = self._cell_to_px(u, v)
            pending = (i == self._m_cell_pending)
            r = (5.0 if pending else 4.0) * (0.75 if zoom == 9 else 1.0)
            canvas.create_oval(px - r, py - r, px + r, py + r,
                               fill="#ff7f0e" if pending else "#333333",
                               outline="white", width=1)
            canvas.create_text(px + 8, py - 8, anchor="w", text=str(i + 1),
                               fill="#777777", font=("TkDefaultFont", 7))

        size_var = getattr(self, "m_cell_size", None)
        if size_var is not None:
            size_var.set(self._cell_size_note())

        ok, msg = self._cell_validate()
        note = self._cell_speed_note() if ok else ""
        var = getattr(self, "m_cell_status", None)
        if var is not None:
            var.set(msg + (("\n" + note) if note else ""))
        lbl = getattr(self, "_m_cell_status_label", None)
        if lbl is not None:
            try:
                lbl.configure(foreground={True: "#2ca02c", False: "#d62728",
                                          None: "#888888"}[ok])
            except tk.TclError:                 # pragma: no cover -- destroyed
                pass

    def _build_mandrel_cell_editor_fields(self, box, tips=None):
        """The Custom-cell section: draw one unit cell, see whether it prints.

        Gridded by BOTH shells (``gui_scaffold``'s mandrel notebook and
        ``gui_pages``' disclosure list) and shown only for the drawn family, via
        ``_mandrel_family_register``. It owns no Generate/Save button -- the
        shared bottom action bar keeps that, as everywhere else on this page.
        """
        import gui_form
        import gui_tips
        self._mandrel_vars_make()
        # the card body's column 0 arrives weighted; it is the label column
        box.columnconfigure(0, weight=0)
        # No intro of its own: the card's blurb already says what this is,
        # and the tiling explanation is the canvas's tooltip.
        F = gui_form.Form(box, stretch=True)

        # THE EDITOR: three toolbars over the drawing canvas. One slot on the
        # form's label edge, exempt inside -- a drawing tool, not a column of
        # parameter rows. Its toolbar dots stay: each explains a drawing tool.
        editor = gui_form.exempt(
            ttk.Frame(box), "unit-cell drawing editor: a canvas and its "
                            "three toolbars")
        F.slot(None, editor, sticky="nw")
        tile_tip = (
                "The cell is a TILE, so its edges join its neighbours: a node "
                "on the right edge (u = 1) is the same point as the next "
                "cell's left edge, and one on the top edge (v = 1) is the next "
                "ring's bottom. The four grey ghosts around the square are "
                "those neighbours. An edge reaching a boundary is what "
                "stitches the pattern together, and a cell whose edges reach "
                "no boundary tiles into loose rings that cannot be printed as "
                "one fiber.\n\n"
                "u runs ALONG the mandrel axis and v runs AROUND it. Both are "
                "fractions of the cell, so one drawing works on any mandrel "
                "diameter and any cell size.\n\n"
                "A green edge is one the router may lay TWICE if doubling it "
                "is what closes the path (McCosker's doubled struts, "
                "“non-load-bearing in the tensile regime”). A blue "
                "edge is load-bearing and is doubled only as a last resort. "
                "New edges are blue.")

        bar = ttk.Frame(editor)
        bar.grid(row=1, column=0, sticky="w", pady=(0, 4))
        ttk.Label(bar, text="Click to:").grid(row=0, column=0, sticky="w")
        for i, (label, value) in enumerate((("Add / move node", "node"),
                                            ("Join nodes", "edge"),
                                            ("Remove", "remove"))):
            ttk.Radiobutton(bar, text=label, value=value,
                            variable=self.m_cell_editmode).grid(
                row=0, column=1 + i, sticky="w", padx=(6, 0))
        # "Snap", not "Grid": it is where a click LANDS, in cell fractions, and
        # calling it Grid is what made it read as the cell's size control.
        ttk.Label(bar, text="Snap:").grid(row=0, column=4, sticky="e",
                                          padx=(12, 2))
        ttk.Entry(bar, width=5, textvariable=self.m_cell_grid).grid(
            row=0, column=5, sticky="w")
        if tips is not None:
            tips.info_dot(bar, (
                "How finely a click snaps, as a FRACTION of the cell. 0.25 "
                "puts four steps across the square. It is a drawing aid and "
                "changes nothing about the print.\n\n"
                "The cell's REAL size is set by “Cell along the tube (mm)” "
                "and “Cell around the tube (mm)” in the section above, and "
                "the caption under the canvas always says what one square is "
                "currently worth in millimeters."),
                title="Snap step").grid(row=0, column=6, padx=(4, 0))

        bar2 = ttk.Frame(editor)
        bar2.grid(row=2, column=0, sticky="w", pady=(0, 4))
        ttk.Button(bar2, text="Toggle free", width=12,
                   command=self._cell_toggle_free).grid(row=0, column=0)
        ttk.Button(bar2, text="Clear", width=8,
                   command=self._cell_clear).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(bar2, text="Honeycomb", width=12,
                   command=self._cell_reset).grid(row=0, column=2, padx=(6, 0))

        # Its OWN row: on bar2 the three buttons plus this group asked for
        # 571 px against a 342 px canvas, which widens the whole section.
        bar3 = ttk.Frame(editor)
        bar3.grid(row=3, column=0, sticky="w", pady=(0, 4))
        # Here rather than beside Clear/Honeycomb: those three EDIT the
        # drawing, and this row is the pair that explains one. It also keeps
        # the widest row at the edit bar -- four buttons plus this one asked
        # for 536 px against a 342 px canvas.
        ttk.Button(bar3, text="Join crossings", width=14,
                   command=self._cell_join_crossings).grid(row=0, column=0)
        ttk.Label(bar3, text="Show:").grid(row=0, column=1, sticky="w",
                                          padx=(16, 2))
        for i, (label, value) in enumerate((("1 cell", "1"),
                                            ("9 cells", "9"))):
            ttk.Radiobutton(bar3, text=label, value=value,
                            variable=self.m_cell_view).grid(
                row=0, column=2 + i, sticky="w", padx=(4, 0))
        if tips is not None:
            tips.info_dot(bar3, (
                "“9 cells” zooms out to a 3 × 3 block of the SAME drawing, "
                "with the eight neighbours shown in a tint and the cell you "
                "are editing in the middle at full strength.\n\n"
                "This is the view that shows whether the pattern actually "
                "stitches. Where the tinted copies meet the middle one, the "
                "lines should JOIN. A member that stops dead at the edge of "
                "the square is a member with no twin on the far side, and it "
                "is why a drawing gets refused as “not connected”.\n\n"
                "Only the middle cell is editable. Clicks on a neighbour are "
                "ignored."),
                title="Seeing the tiling").grid(row=0, column=4, padx=(6, 0))

        canvas = tk.Canvas(editor, width=self._CELL_CANVAS,
                           height=self._CELL_CANVAS, background="white",
                           highlightthickness=1, highlightbackground="#cccccc",
                           takefocus=0)
        canvas.grid(row=4, column=0, sticky="nw")
        gui_tips.add_tip(canvas, tile_tip)
        self._m_cell_canvas = canvas
        canvas.bind("<Button-1>", self._cell_press)
        canvas.bind("<B1-Motion>", self._cell_drag)
        canvas.bind("<ButtonRelease-1>", self._cell_release)

        # what one square is worth, and whether the drawing prints: computed
        # while you draw, so grey `live` text rather than tooltips
        F.live(None, textvariable=self.m_cell_size,
               wrap=self._CELL_CANVAS + 60)
        self._m_cell_status_label = F.live(
            None, textvariable=self.m_cell_status,
            wrap=self._CELL_CANVAS + 60).field

        F.entry("Drawing", self.m_cell_graph,
                "The drawing as text: nodes | edges. Typed and clicked are "
                "the same field, so either edits the other.", width="path")

        for var in (self.m_cell_graph, self.m_cell_grid, self.m_diameter,
                    self.m_cell_w, self.m_cell_h, self.m_cell_editmode,
                    # the zoom repaints the canvas; the length, the fit and the
                    # count pin all move the mm caption under it.
                    self.m_cell_view, self.m_length, self.m_cell_fit,
                    self.m_cell_pick):
            var.trace_add("write", self._cell_redraw)
        self._cell_redraw()

    def _mandrel_new_preview_state(self):
        """The 4-axis twin of `_pv_new_state`, generation counters included.

        `gen_sub`/`gen_done` are what make a slow build discardable: the tube
        rides the shared worker, so a result that lands after a newer edit was
        submitted must be dropped rather than drawn (`_mandrel_apply_result`)."""
        return dict(gen_sub=0, gen_done=0, paths=None, meta=None,
                    lines_pd=None, tubes_pd=None, gcode_pd=None,
                    display_mode=None, camera=None, framed_diag=None, diag=0.0,
                    color=self._pv_parse_color(self.scaf_vars[0]["color"]),
                    status="waiting for valid parameters…", status_error=False)

    def _install_mandrel_readout_traces(self):
        """Dirty the preview AND the resolved readout on any design input.

        Installed by the FORM builder, not by the preview builder, because the
        classic developer UI has no preview panel at all and its readout still
        has to follow the fields.  `_install_mandrel_preview_traces` adds the
        modern shell's display-only vars on top of this; the two are idempotent
        and share one debounce, so the order they run in does not matter."""
        if getattr(self, "_mandrel_readout_traced", False):
            return
        self._mandrel_readout_traced = True
        for var in (self.m_diameter, self.m_length, self.m_angle, self.m_eta,
                    self.m_rm, self.m_speed_mode, self.m_family, self.m_cell_w,
                    self.m_cell_h, self.m_cell_fit, self.m_rib_angle,
                    self.m_cell_amplitude,
                    # the DRAWN cell -- a click on the canvas rewrites this
                    # string, so it is a design input exactly as a typed
                    # number is, and it has to dirty the preview the same way.
                    self.m_cell_graph,
                    # the pore search's own inputs: each one moves the design
                    # the solver returns, so each one dirties the preview.
                    self.m_pore_target, self.m_length_tol, self.m_eta_cap,
                    # the two ladder pins: a pinned rung IS the design, so it
                    # dirties the preview exactly as a typed field does.
                    self.m_helix_pick, self.m_cell_pick, self.m_angle_pick,
                    self.m_turn_pick,
                    # WHERE ON THE ROD. These move the emitted path (`start_x`),
                    # so they are design inputs like any other -- and the "end
                    # margin" they replaced was in NEITHER trace list from the
                    # day it was added, so editing the collector row moved the
                    # rod under a tube that stayed where it was until some
                    # unrelated field was touched.
                    self.m_place, self.m_place_offset,
                    # the tube tables: the blocks, the step, the copies
                    self.m_layer_step, self.m_layer_overrides,
                    self.m_turn_out_on, self.m_turn_out,
                    self.m_copies, self.m_copy_gap):
            var.trace_add("write", self._schedule_mandrel_preview)
        # ...and the MACHINE page's half of the same layout. A rod declared
        # over there decides where this preview's tube sits, so an edit there
        # has to dirty this preview as much as an edit here does. `getattr`
        # rather than a hard reference because the classic developer UI hosts
        # this mixin without that page; `_rotary_vars_make` is what makes them
        # exist whichever page was built first.
        try:
            self._rotary_vars_make()
        except AttributeError:                 # no Build-plate mixin (classic)
            pass
        for name in ("rotary_on", "rotary_rod_length", "rotary_rod_x",
                     "rotary_origin_axial", "rotary_origin_radial",
                     "rotary_origin_custom"):
            var = getattr(self, name, None)
            if var is not None:
                var.trace_add("write", self._schedule_mandrel_preview)
        try:
            self.scaf_vars[0]["layers"].trace_add(
                "write", self._schedule_mandrel_preview)
        except (AttributeError, IndexError, KeyError):   # pragma: no cover
            pass

    def _install_mandrel_preview_traces(self):
        self._install_mandrel_readout_traces()
        # The shared fields dirty the 3-axis preview through _install_preview_
        # traces; in 4-axis they must dirty THIS one instead.  Layers is already
        # traced by the readout set above, which both UIs install.
        for var in (self.scaf_vars[0]["speed"],
                    self.scaf_vars[0]["diameter"], self.s_sag_mode,
                    self.s_sag_mult):
            var.trace_add("write", self._schedule_mandrel_preview)
        # The Physics page's knobs re-lay the touchdown path, so in 4-axis they
        # are geometry inputs, not decoration -- the mirror of the 3-axis list in
        # `gui_preview._install_preview_traces`.  A knob that reaches the physics
        # dict but not this list is picked up only on the NEXT unrelated edit,
        # which reads as the preview ignoring it (which is exactly how the whole
        # relay read before it existed).  `pv_show_sag` rides here rather than
        # with the readout vars because `_refresh_mandrel_preview` reads it to
        # decide the drape multiplier.
        for name in ("sim_physics_deposit", "pv_show_lag", "sim_sag_law",
                     "sim_physical_fusion", "pv_show_fusion",
                     "sim_standoff", "sim_voltage",
                     "sim_pressure", "sim_temperature", "sim_kinematics",
                     "sim_max_accel", "sim_scv", "sim_rotary_accel",
                     "sim_coil_wave",
                     "sim_coil_wave_amp", "sim_coil_wave_len",
                     "pv_fill_per_tau", "pv_curve_quality", "pv_show_sag",
                     "sim_mandrel_per_layer",
                     "m_support", "m_lag_axial", "m_lag_circ"):
            var = getattr(self, name, None)
            if var is not None:
                var.trace_add("write", self._schedule_mandrel_preview)
        # ...and the rig profile behind them: the model the relay runs is built
        # from the Calibration fields, and on a cylinder the placement reads
        # them directly (lag_amplitude, the support knobs).  Debounced like any
        # other edit, so a guided-fit Apply that writes several coalesces into
        # one redraw.
        for var in (getattr(self, "cal_vars", None) or {}).values():
            try:
                var.trace_add("write", self._schedule_mandrel_preview)
            except AttributeError:              # pragma: no cover -- not a Variable
                pass
        self.scaf_vars[0]["color"].trace_add(
            "write", self._mandrel_on_color)

    def _mandrel_on_color(self, *_):
        """A colour edit is a pure restyle, exactly as _pv_on_color is."""
        state = getattr(self, "_mandrel_preview_state", None)
        if state is None:
            return
        state["color"] = self._pv_parse_color(self.scaf_vars[0]["color"])
        panel = getattr(self, "_mandrel_preview", None)
        if panel is not None and state.get("paths") is not None:
            try:
                panel.widget._actor.GetProperty().SetColor(*state["color"])
                panel.widget._render_soon()
            except Exception:  # noqa: BLE001
                pass

    def _schedule_mandrel_preview(self, *_):
        """Debounce a redraw of whichever 4-axis output this UI actually has.

        No longer gated on the preview panel existing: the classic developer UI
        has no panel but does have the resolved readout, and the readout is the
        only place either UI shows what the generator resolved the request to."""
        if not self._mandrel_is_active():
            return
        if getattr(self, "_m_solver_writing", False):
            return          # the solver echoing its own answer into Advanced
        root = getattr(self, "root", None)
        if root is None:
            return
        after = getattr(self, "_mandrel_preview_after", None)
        if after is not None:
            try:
                root.after_cancel(after)
            except tk.TclError:
                pass
        self._mandrel_preview_after = root.after(
            _MANDREL_PREVIEW_MS, self._refresh_mandrel_view)

    def _refresh_mandrel_view(self):
        if getattr(self, "_mandrel_preview", None) is not None:
            self._refresh_mandrel_preview()
        else:
            self._mandrel_preview_after = None
            self._refresh_mandrel_readout()

    def _mandrel_designer_hidden(self):
        """The Scaffold designer's picture is on a page under another one.

        The modern shell's PageStack never unmaps a page, so `<Map>` (which
        `_mandrel_preview_on_mapped` answers) does not fire on arrival and
        `winfo_ismapped` is true for every page. Always False in the classic
        UI, whose notebook really unmaps."""
        panel = getattr(self, "_pv_panel", None)
        hidden = getattr(panel, "_page_hidden", None)
        return bool(hidden is not None and hidden())

    def _mandrel_page_arrived(self):
        """A page came up: if it is the designer and builds were held back
        while it was hidden, build now (the work `<Map>` does in the classic
        UI)."""
        if not getattr(self, "_mandrel_stale", False) \
                or self._mandrel_designer_hidden():
            return
        self._mandrel_stale = False
        if not self._mandrel_is_active():
            return
        self._mandrel_show_last()
        self.root.after_idle(self._refresh_mandrel_view)

    def _mandrel_show_last(self):
        """Re-show the last good tube on the shared panel, WITHOUT rebuilding.

        The rebuild is asynchronous, so every handover of the shared panel back
        to the 4-axis half has to redraw what it already has; otherwise the
        planar scaffold stays on screen for the length of the tube build."""
        panel = getattr(self, "_mandrel_preview", None)
        state = getattr(self, "_mandrel_preview_state", None)
        if panel is not None and state is not None \
                and state.get("paths") is not None:
            panel.apply_state(state, _MANDREL_SUBTITLE)

    def _mandrel_preview_on_mapped(self):
        # A newly selected shell page needs a visible first frame, not the edit
        # debounce.  Subsequent field edits still coalesce through the 350 ms path.
        if getattr(self, "_mandrel_preview_state", {}).get("paths") is None:
            self.root.after_idle(self._refresh_mandrel_view)
        else:
            # The scene may have been rebuilt by the map; put the geometry back
            # now rather than after the debounce plus a build.
            self._mandrel_show_last()
            self._schedule_mandrel_preview()

    def _mandrel_snapshot(self):
        """Every Tk var this build reads, as plain values, on the Tk thread.

        Exactly the keyword arguments of `build_mandrel_preview` and nothing
        else, because the worker thread must not touch a `tk.Variable`.  The
        physics dict is safe to hand over: `_pv_snapshot_options` constructs a
        fresh `MEWModel` per call, so the object the worker reshapes with is
        never the one a later edit replaces.  Raises on a half-typed field --
        the caller turns that into the waiting posture, as it always did."""
        eta = self.m_eta.get().strip()
        # Whenever the page resolves the closure (pore mode, and winding-angle
        # mode without Advanced's override) the design is RESOLVED here, on the
        # Tk thread, before the snapshot leaves it -- the worker then rebuilds
        # exactly this design because `mandrel_design` with eta pinned returns
        # it directly (`scaffold_gcode.mandrel_design`).  Every number comes off the design
        # object rather than back out of the field the solver just wrote, so a
        # formatting round trip cannot move the tube.
        solved = self._mandrel_solved_design()
        return dict(
            gcode_path=os.path.join(self.session_dir, "mandrel_preview.gcode"),
            diameter=float(self.m_diameter.get()),
            target_length=(solved.length if solved is not None
                           else float(self.m_length.get())),
            angle=(solved.omega_deg if solved is not None
                   else float(self.m_angle.get())),
            fiber_diameter=self._mandrel_fiber_diameter(),
            layers=self._mandrel_layers(), speed=self._mandrel_speed(),
            eta=(None if self._mandrel_is_turned(solved) else
                 solved.eta if solved is not None
                 else (int(eta) if eta else None)),
            pivots=(solved.n_pivots if self._mandrel_is_turned(solved)
                    else None),
            rm=(solved.rm if solved is not None else int(self.m_rm.get() or 0)),
            speed_mode=self.m_speed_mode.get(),
            sag_multiplier=(0.0 if self._sag_solver_value(self.s_sag_mode) == "off"
                            or not self._pv_sag_shown()
                            else float(self.s_sag_mult.get())),
            physics=self._mandrel_physics_options(),
            # Fusion, from the ONE builder the 3-axis and 5-axis previews read,
            # so the three collectors cannot band a crossing three ways. It is
            # independent of `physics` on purpose -- see `_pv_fusion_law`.
            **dict(zip(("fuse_law", "fuse_band"), self._pv_fusion_law())),
            place=self._mandrel_place(),
            place_offset=self._mandrel_place_offset(),
            # the tube tables: resolved HERE, on the Tk thread, like `solved`
            blocks=self._mandrel_tube_blocks(),
            layer_step=self._mandrel_layer_step(),
            turn_out=self._mandrel_turn_out(),
            copies=self._mandrel_copies(), copy_gap=self._mandrel_copy_gap(),
            # the declared rod, so the preview draws the mandrel the machine
            # page describes and the margin shows as bare rod rather than being
            # inferred from a rod that grew to fit
            **dict(zip(("rod_length", "rod_start"), self._rotary_rod())),
            **self._mandrel_cell_kwargs())

    def _mandrel_below_cts(self, spec):
        """`(v_mm_s, cts_mm_s)` when this tube would be relayed BELOW CTS, else None.

        Read off the SPEC, not the Tk vars, so the number tested is the number the
        build would have used. Two units meet here and the field names do not say
        so: the Print-speed field is **mm/min** (gui_scaffold.py:1165) while
        `critical_speed` returns **mm/s**, so the field is divided by 60 before the
        comparison. Getting that wrong is a factor of 60 in either direction and
        both directions look plausible on screen.

        PHYSICS-GATED on purpose. Below CTS the relay coils the centreline, and the
        coil wavelength is what the drape then has to resolve: on the shipped
        *Mandrel default* at eps = 0.833 the build measured 24.55 s / 1 382 377
        points against 3.54 s / 392 628 above CTS -- but with physics OFF the same
        below-CTS spec measured 2.01 s / 288 353 points, because nothing coils a
        commanded path. So the commanded preview is not refused: there is nothing
        wrong with it, and there is no model to ask for a CTS anyway.

        The CTS comes from `MEWModel.cts_at`, which derives flow and viscosity
        from the same knobs `mew_deposition` does, so a rig with fitted `cts_flow_exp` /
        `cts_visc_exp` is judged on the same CTS the relay would use rather than
        on a two-argument approximation of it. It can raise on a half-fitted
        profile (a set exponent with no reference), and a non-positive CTS is a
        missing anchor rather than a slow speed -- `mew_deposition` handles that
        case itself, without the blow-up. Both FAIL OPEN: this guard exists to
        stop one measured pathology, not to become a second validator."""
        if spec.get("physics") is None:          # no model, no CTS to ask
            return None
        import gui_preview
        res = gui_preview.cts_check(spec["physics"], spec["speed"])
        return res if res is not None and res[0] < res[1] else None

    def _refresh_mandrel_preview(self):
        """SUBMIT a tube build to the shared preview worker -- never build here.

        Calling `build_mandrel_preview` inline would put a
        generator + rotary parse + touchdown relay + drape on the Tk thread, so
        a heartbeat `after` counter stopped ticking for the whole build: 1.31 s
        of a 1.67 s re-entrant rebuild at 10 plies, and 7.96 s of 8.41 s with
        physics on.  It now takes the SAME path the 3-axis preview has always
        taken -- snapshot on the Tk thread, one daemon worker, a latest-only
        mailbox slot keyed `gui_preview.PV_MANDREL`, a generation id, and the
        `_pv_poll` result pump -- so a slow family delays the picture instead of
        the window.  Nothing cancels a running drape: an obsolete result is
        discarded on arrival by `_mandrel_apply_result`, which is what the
        generation id is for."""
        self._mandrel_preview_after = None
        panel = getattr(self, "_mandrel_preview", None)
        state = getattr(self, "_mandrel_preview_state", None)
        if panel is None or state is None or not self._mandrel_is_active():
            return
        import gui_preview
        if self._pv_preview_disabled():
            self._pv_drop_queued(gui_preview.PV_MANDREL)
            state["gen_sub"] += 1
            state["status"] = gui_preview.PREVIEW_DISABLED_STATUS
            state["status_error"] = False
            self._refresh_mandrel_readout()
            panel.set_subtitle(_MANDREL_SUBTITLE)
            panel.set_status(state["status"])
            # the Design summary's CTS check does not need the preview
            try:
                state["cts_now"] = gui_preview.cts_check(
                    self._mandrel_physics_options(), self._mandrel_speed())
            except Exception:  # noqa: BLE001 -- mid-typing; no verdict
                state["cts_now"] = None
            self._pv_summary_changed()
            return
        # (the disabled branch above builds nothing, so it runs hidden too)
        if self._mandrel_designer_hidden():
            # Nobody can see this picture: the CAD page edits the same tube
            # and builds its own, and a second build here ran beside it and
            # halved it (GIL), then cost the Tk thread ~2.5 s to apply. The
            # designer builds when it is next shown (`_mandrel_page_arrived`).
            self._mandrel_stale = True
            return
        try:
            spec = self._mandrel_snapshot()
        except Exception as exc:  # keep the last valid preview while typing
            gap = isinstance(exc, _LengthGap)
            if gap:
                # as for below-CTS: no build already in flight may land and
                # paint over the refusal with a status that says all is well
                state["gen_sub"] += 1
            state["status"] = (_MANDREL_GAP_STATUS if gap else
                               "waiting for valid parameters: %s" % exc)
            state["status_error"] = gap
            self._refresh_mandrel_readout(error=exc)
            panel.set_subtitle(_MANDREL_SUBTITLE)
            panel.set_status(state["status"], error=gap)
            return
        import gui_preview
        below = self._mandrel_below_cts(spec)
        # the Design summary's CTS line, at edit time rather than build time
        state["below_cts"] = below
        state["cts_now"] = (gui_preview.cts_check(spec["physics"],
                                                  spec["speed"])
                            if spec.get("physics") is not None else None)
        if below is not None:
            # `gen_sub` is bumped even though nothing is submitted, so a build
            # that went to the worker BEFORE the speed dropped below CTS cannot
            # land and paint a picture over this refusal: `_mandrel_apply_result`
            # discards any result older than `gen_sub`. The last good tube stays
            # on screen exactly as it does in the waiting posture -- the line
            # underneath is what says it is stale.
            state["gen_sub"] += 1
            state["status"] = _MANDREL_BELOW_CTS
            state["status_error"] = True
            self._refresh_mandrel_readout()
            panel.set_subtitle(_MANDREL_SUBTITLE)
            panel.set_status(state["status"], error=True)
            self._pv_summary_changed()
            return
        state["gen_sub"] += 1
        state["color"] = self._pv_parse_color(self.scaf_vars[0]["color"])
        state["status"] = "updating…"
        state["status_error"] = False
        panel.set_subtitle(_MANDREL_SUBTITLE)
        panel.set_status(state["status"])
        # the plate's rule (`_pv_kick`): a build that took a second or more
        # last time shows the commanded tube first, then its sagged one
        last = state.get("meta") or {}
        first = (last.get("provisional")
                 or float(last.get("elapsed", math.inf))
                 >= gui_preview.PREVIEW_TOOLPATH_FIRST_S)
        with self._pv_cv:
            self._pv_mailbox[gui_preview.PV_MANDREL] = (
                state["gen_sub"], spec, {"toolpath_first": first})
            self._pv_cv.notify()
        self._pv_summary_changed()
        self._pv_pending += 1
        self._pv_ensure_worker()
        self._pv_arm_poll()

    def _mandrel_apply_result(self, gen, paths, meta, err):
        """A worker result for the tube, on the Tk thread (from `_pv_poll`).

        The twin of `_pv_apply_result`, with two 4-axis-only jobs: the resolved
        readout is refreshed from the design object the build actually used
        (never a second resolution that could disagree), and the shared panel is
        left alone when the collector toggle has since flipped -- a tube landing
        on top of a plate scaffold is the same defect that guard prevents on the
        planar side."""
        state = getattr(self, "_mandrel_preview_state", None)
        if state is None or gen < state["gen_sub"]:
            return                                  # a newer edit is in flight
        if (meta or {}).get("provisional"):
            # The toolpath-first picture, as the plate draws it: shown and
            # labelled, NOT the build's result -- `gen_done` stays put and the
            # status keeps "updating", so whatever waits for the build waits.
            if gen <= state.get("gen_done", -1):
                return                              # the full build beat it
            state.update(paths=paths, meta=meta, lines_pd=None, tubes_pd=None,
                         gcode_pd=None, display_mode=None,
                         diag=math.dist(*meta["bbox"]),
                         color=self._pv_parse_color(self.scaf_vars[0]["color"]),
                         status_error=False,
                         status=(f"toolpath only · {meta['layers']} layers · "
                                 "updating…"))
            self._refresh_mandrel_readout(design=meta.get("design"),
                                          a_max=meta.get("a_max_deg"))
            # unlike the plate's, everything the tube's summary reads (print
            # time, fiber length, the design's pore) is final here already
            self._pv_summary_changed()
            panel = getattr(self, "_mandrel_preview", None)
            if panel is not None and self._mandrel_is_active():
                panel.apply_state(state, _MANDREL_SUBTITLE)
                if getattr(self, "_mandrel_rod_pending", False):
                    self._mandrel_rod_pending = False
                    self._pv_on_show_collector()
            return
        if err is not None:
            state["status"] = "waiting for valid parameters: %s" % err
            state["status_error"] = False
            self._refresh_mandrel_readout(error=err)
        else:
            state["status_error"] = False
            state.update(gen_done=gen, paths=paths, meta=meta, lines_pd=None,
                         tubes_pd=None, gcode_pd=None, display_mode=None,
                         diag=math.dist(*meta["bbox"]),
                         color=self._pv_parse_color(self.scaf_vars[0]["color"]),
                         status=(f"mandrel Ø{2 * meta['radius']:.3f} mm · "
                                 f"{meta['layers']} layers · {meta['n_pts']:,} points · "
                                 + ("physics on · " if meta.get("physics") else "")
                                 + ("approximate sag"
                                    if meta["preview_sagged"] else "sag off")))
            self._refresh_mandrel_readout(design=meta.get("design"),
                                          a_max=meta.get("a_max_deg"))
        self._pv_summary_changed()
        panel = getattr(self, "_mandrel_preview", None)
        if panel is None or not self._mandrel_is_active():
            return
        if err is not None:
            panel.set_status(state["status"])
            return
        panel.apply_state(state, _MANDREL_SUBTITLE)
        # The tube is now on screen, so the rod the collector switch withheld
        # can join it (gui_preview._pv_rod_wanted). Deliberately NOT done on the
        # error branch: a build that never produced a tube must not put a rod
        # around whatever the panel still happens to be showing.
        if getattr(self, "_mandrel_rod_pending", False):
            self._mandrel_rod_pending = False
            self._pv_on_show_collector()

    #: The mandrel-only calibration values, as (Calibration attr, var name).
    #: THE ONE LIST: the preview (`_mandrel_physics_options`), the export
    #: (`_calibration_args`), the Calibration page's lag tube and the Physics
    #: page's Jet lag card all read it, so no reader can miss a value.
    _MANDREL_CAL_VARS = (("mandrel_support_mm", "m_support"),
                         ("mandrel_lag_axial", "m_lag_axial"),
                         ("mandrel_lag_circ", "m_lag_circ"),
                         ("mandrel_lag_speed", "m_lag_speed"),
                         ("mandrel_cts", "m_cts"))

    def _mandrel_cal_extras(self):
        """{Calibration attr: float} for every mandrel-only value that is set.
        A field that does not read as a number is left out, as a blank is --
        the validator names it before any run gets this far."""
        self._mandrel_vars_make()
        out = {}
        for attr, name in self._MANDREL_CAL_VARS:
            text = getattr(self, name).get().strip()
            if not text:
                continue
            try:
                out[attr] = float(text)
            except ValueError:
                continue
        return out

    def _mandrel_physics_options(self):
        """The touchdown-relay options for the tube, or None when physics is off.

        There is ONE builder for them -- `gui_preview._pv_snapshot_options`, the
        same one the 3-axis preview uses -- so the two collectors cannot drift
        apart on which fields the model is read from; only the mandrel-only
        placement radii are added here, because the Physics page's field registry
        deliberately excludes them (`mew_calibration.py:314`) and this page is
        their only writer.  Those three keys are the same three
        `_calibration_args` folds into the profile it hands the export worker, so
        preview and export solve the same cylinder.  A garbage sim field raises,
        exactly as it does on the 3-axis side: physics ON but unusable must not
        silently fall back to a commanded preview."""
        opts = self._pv_snapshot_options(0)
        phys = opts.get("physics")
        if phys is None:
            return None
        extra = self._mandrel_cal_extras()
        if extra:
            model = phys["model"]
            model.calibration = dataclasses.replace(model.calibration, **extra)
        return phys

    # ------------------------------------------------------------------ #
    # the resolved readout -- what the generator made of the request
    # ------------------------------------------------------------------ #
    def _mandrel_cell_kwargs(self):
        """The family knobs, spelled once, for the preview and the readout.

        Carries the PINNED counts, so the preview, the readout, the validator
        and the argv cannot disagree about which rung of the count ladder is
        being built.  `(None, None)` -- Auto -- is the rounding
        `mew_unit_cell.design_tube_cells` does on its own, byte for byte."""
        n_around, n_axial = self._mandrel_cell_counts()
        return dict(family=self._mandrel_family_value(),
                    cell_w=float(self.m_cell_w.get()),
                    cell_h=float(self.m_cell_h.get()),
                    cell_fit=self.m_cell_fit.get().strip() or "scale",
                    rib_angle=float(self.m_rib_angle.get()),
                    amplitude=float(self.m_cell_amplitude.get() or 0.0),
                    graph=self.m_cell_graph.get().strip(),
                    cell_n_around=n_around, cell_n_axial=n_axial)

    def _mandrel_cell_design(self):
        """Resolve the cell design for the fields as they stand.

        `mew_unit_cell.UnitCellError` propagates: its messages name the offending
        number and what to do about it, and the page shows them verbatim."""
        import gui_mandrel_preview
        kw = self._mandrel_cell_kwargs()
        return gui_mandrel_preview.cell_design(
            family=kw["family"], diameter=float(self.m_diameter.get()),
            target_length=float(self.m_length.get()), cell_w=kw["cell_w"],
            cell_h=kw["cell_h"], fit=kw["cell_fit"], rib_angle=kw["rib_angle"],
            amplitude=kw["amplitude"], n_around=kw["cell_n_around"],
            n_axial=kw["cell_n_axial"], graph=kw["graph"])

    def _mandrel_resolved_design(self):
        """The design object of whichever family is selected.

        Both carry `.length`, which is the ONE number the player and the
        converter need; nothing else about them is interchangeable."""
        if self._mandrel_family_value() == "zigzag":
            return self._mandrel_design()
        return self._mandrel_cell_design()

    # ------------------------------------------------------------------ #
    # layer blocks and side-by-side copies (the G-code page's tube tables)
    # ------------------------------------------------------------------ #
    def _mandrel_layer_overrides(self):
        """{layer, 0-based: {key: value}} as `m_layer_overrides` holds it; a
        garbled field reads as none, as the plate's does."""
        try:
            raw = self.m_layer_overrides.get().strip()
            data = json.loads(raw) if raw else {}
            return {int(k) - 1: dict(v) for k, v in data.items() if v}
        except (ValueError, TypeError, AttributeError, tk.TclError):
            return {}

    def _mandrel_layer_step(self):
        """The Layer step, mm, or None: blank is the fiber diameter."""
        text = self.m_layer_step.get().strip()
        return float(text) if text else None

    def _mandrel_turn_out(self):
        """How far an end turn runs past the tube's end, mm: 0 when the
        perimeter travel is off (or the offset is blank or unreadable)."""
        if not self.m_turn_out_on.get():
            return 0.0
        try:
            return max(0.0, float(self.m_turn_out.get().strip() or 0.0))
        except ValueError:
            return 0.0

    def _mandrel_copies(self):
        text = self.m_copies.get().strip()
        return int(text) if text else 1

    def _mandrel_copy_gap(self):
        import scaffold_gcode
        text = self.m_copy_gap.get().strip()
        return float(text) if text else scaffold_gcode.TUBE_COPY_GAP

    def _mandrel_tube_entries(self):
        """The `--tube-blocks` entries the tube's layers make, or None when
        every layer is the tube's own (one block: nothing to send).

        `scaffold_gcode.tube_layer_blocks` groups the layers. An entry that
        changes the PATTERN is then spelled out whole (`_mandrel_entry_
        design`), so the worker, which lays it over its own flags, and the
        page, which resolves it here, build one design."""
        import scaffold_gcode
        # the table writes what was typed; every key but the family and the
        # placement is a number. A layer's placement goes out as how far
        # along the rod it sits from the tube's own start (`shift`)
        ov = {}
        for k, row in self._mandrel_layer_overrides().items():
            r = {}
            for kk, vv in row.items():
                if kk == "family":
                    r[kk] = vv
                elif kk not in ("place", "place_offset"):
                    r[_LAYER_ENTRY_KEY.get(kk, kk)] = float(vv)
            if "place" in row or "place_offset" in row:
                dx = self._mandrel_layer_shift(row.get("place"),
                                               row.get("place_offset"))
                if abs(dx) > 1e-9:
                    r["shift"] = dx
            if r:
                ov[k] = r
        if not ov:
            return None
        entries = scaffold_gcode.tube_layer_blocks(self._mandrel_layers(), ov)
        if len(entries) == 1 and set(entries[0]) <= {"layers", "step"}:
            if "step" not in entries[0]:
                return None
        base = self._mandrel_family_value()
        return [self._mandrel_entry_design(e, e.get("family", base))
                if set(e) & {"family", "winding_angle", "pore",
                             "cell_width", "cell_height", "rib_angle",
                             "cell_amplitude"} else e
                for e in entries]

    def _mandrel_layer_shift(self, place, offset):
        """How far along the rod a layer placed by (`place`, `offset`) sits
        from the tube's own start, mm; None for either is the tube's own.
        Both starts are the tube's placement rule
        (`gui_mandrel_preview.axial_layout`) on the same rod and length, so
        a layer "Offset from the left end, 5" on a tube placed at 3 sits
        2 mm on. The wave overhang moves both starts alike, and cancels."""
        import gui_mandrel_preview
        try:
            design = self._mandrel_resolved_design()
        except Exception:  # noqa: BLE001 -- the validator names it
            return 0.0
        rod_length, rod_x = self._rotary_rod()

        def start(p, o):
            return gui_mandrel_preview.axial_layout(
                design, place=p, offset=o, rod_length=rod_length,
                rod_start=rod_x, overhang=0.0).print_start_x

        own_place, own_off = self._mandrel_place(), self._mandrel_place_offset()
        p = place if place in PLACEMENT_KEYS else own_place
        try:
            o = float(offset) if offset is not None else own_off
        except (TypeError, ValueError):
            o = own_off
        return start(p or "left", o) - start(own_place, own_off)

    def _mandrel_entry_design(self, entry, fam):
        """One block whose pattern is not the tube's, every knob its family
        reads written in.

          * A cell: the tube's cell knobs with the layer's size, fit 'fit'
            -- the length is the tube's, so the cell bends round to fit it
            rather than moving it -- and no count pin, which is the tube's
            own cells'. A chevron with no amplitude gets the designer's
            backfill rule (`_apply_mandrel_family`): a quarter of the cell
            width, since the engine refuses a flat one.
          * The winding: the layer's angle, closed by an END TURN at the
            tube's length with the pivot count nearest the layer's pore
            (or, unset, the page's)
            (`mew_tube.solve_turned_tube`) -- the one closure that holds a
            length while the angle moves."""
        e = dict(entry, family=fam)
        pore = e.pop("pore", None)          # a side, um; the page's own key
        if fam == "zigzag":
            import mew_tube
            for k in ("cell_width", "cell_height", "rib_angle",
                      "cell_amplitude"):
                e.pop(k, None)
            omega = float(e.get("winding_angle", self.m_angle.get()))
            best, _alts = mew_tube.solve_turned_tube(
                0.5 * float(self.m_diameter.get()), omega,
                self._mandrel_resolved_design().length,
                ((float(pore) / 1000.0) ** 2 if pore is not None
                 else self._mandrel_pore_target_area()),
                fiber_diameter=self._mandrel_fiber_diameter())
            e.update(winding_angle=omega, tube_pivots=int(best.n_pivots),
                     tube_eta=None, tube_rm=0)
            return e
        e.pop("winding_angle", None)
        w = float(e.get("cell_width", self.m_cell_w.get()))
        e.update(cell_width=w,
                 cell_height=float(e.get("cell_height", self.m_cell_h.get())),
                 cell_fit="fit", cell_n_around=None, cell_n_along=None)
        if fam == "reentrant":
            e.pop("cell_amplitude", None)
            e["rib_angle"] = float(e.get("rib_angle", self.m_rib_angle.get()))
        elif fam == MANDREL_DRAWN_FAMILY:
            e.pop("rib_angle", None)
            e.pop("cell_amplitude", None)
            e["cell_graph"] = self.m_cell_graph.get().strip()
        else:
            e.pop("rib_angle", None)
            amp = float(e.get("cell_amplitude",
                              self.m_cell_amplitude.get() or 0.0))
            if fam == "chevron" and amp <= 0.0:
                amp = float("%.4g" % (0.25 * w,))
            e["cell_amplitude"] = amp
        return e

    def _mandrel_tube_blocks(self):
        """The tube as resolved `scaffold_gcode.TubeBlock`s, or None for the
        one-block tube. Raises with the engine's own message when a block
        will not resolve -- or, at its length, would not share the tube's."""
        import argparse
        import scaffold_gcode
        entries = self._mandrel_tube_entries()
        if entries is None:
            return None
        base = self._mandrel_resolved_design()
        kw = self._mandrel_cell_kwargs()
        ns = argparse.Namespace(
            mandrel=float(self.m_diameter.get()), tube_length=base.length,
            tube_family=kw["family"], winding_angle=float(self.m_angle.get()),
            tube_eta=None, tube_rm=0, tube_pivots=None,
            cell_width=kw["cell_w"], cell_height=kw["cell_h"],
            cell_fit=kw["cell_fit"], cell_n_around=kw["cell_n_around"],
            cell_n_along=kw["cell_n_axial"], rib_angle=kw["rib_angle"],
            cell_amplitude=kw["amplitude"], cell_graph=kw["graph"],
            layers=self._mandrel_layers(),
            tube_turn_out=self._mandrel_turn_out())
        blocks, _lines = scaffold_gcode.tube_blocks_from_entries(
            entries, ns, base_design=base)
        for i, b in enumerate(blocks):
            if abs(b.design.length - base.length) > 1e-6:
                raise ValueError(
                    "block %d (layers from %d) comes out %.4f mm long but the "
                    "tube is %.4f mm: every block shares the tube's length. "
                    "Pick the end-turn closure under Advanced, or a cell "
                    "family, for a layer whose winding angle differs"
                    % (i + 1, 1 + sum(int(x.layers) for x in blocks[:i]),
                       b.design.length, base.length))
        return blocks

    def _mandrel_tube_overhang(self, design):
        """The widest wave of the tube's blocks, or `design`'s own."""
        import scaffold_gcode
        try:
            blocks = self._mandrel_tube_blocks()
        except Exception:  # noqa: BLE001 -- the validator names it
            blocks = None
        return scaffold_gcode.tube_overhang(
            blocks or [scaffold_gcode.TubeBlock(
                design, 1, turn_out=self._mandrel_turn_out())])

    def _mandrel_cell_fits(self):
        """BOTH of Devlin's fits, resolved at once -> {fit: design or Exception}.

        The choice used to hide behind a combobox, so the compromise the fit
        makes was invisible until after it had been made.  Both are shown side
        by side instead and the two `Use` buttons write `m_cell_fit`; a fit that
        refuses carries its `UnitCellError` into the table's own column rather
        than taking the whole panel down with it.

        BOTH columns are resolved at the PINNED count, so the table always
        describes the tube the page is about to build.  Only `n_around` is
        pinned across the two: `n_axial` follows the fit ('scale' rounds the
        length against the scaled cell width, 'fit' against the requested one),
        so pinning it here would make one of the two columns fiction."""
        import gui_mandrel_preview
        kw = self._mandrel_cell_kwargs()
        out = {}
        for fit in ("scale", "fit"):
            try:
                out[fit] = gui_mandrel_preview.cell_design(
                    family=kw["family"], diameter=float(self.m_diameter.get()),
                    target_length=float(self.m_length.get()),
                    cell_w=kw["cell_w"], cell_h=kw["cell_h"], fit=fit,
                    rib_angle=kw["rib_angle"], amplitude=kw["amplitude"],
                    graph=kw["graph"], n_around=kw["cell_n_around"])
            except Exception as exc:  # noqa: BLE001 -- shown, not swallowed
                out[fit] = exc
        return out

    def _mandrel_use_fit(self, fit):
        """A `Use scaled` / `Use fitted` click: choose the FIT, keep the count.

        The fit and the cell count are two independent choices about the same
        cell, so this one does not clear a pinned count -- it rewrites the fit
        component of the pin, which is what keeps `m_cell_fit` and the ticked
        ladder row describing the same design."""
        self.m_cell_fit.set(fit)
        pick = (self.m_cell_pick.get() or "").strip().split(",")
        if len(pick) == 3:
            self.m_cell_pick.set("%s,%s,%s" % (pick[0], pick[1], fit))
        self._mandrel_redraw_panel()

    def _mandrel_use_alternative(self, design):
        """A helix ladder `Use` click: PIN this rung, and stay in pore mode.

        It used to write the triple into Advanced and flip `m_design_mode` to
        `angle`.  That took the pore field, the two search bounds and the
        ladder itself off the grid (they are pore-mode rows), so the page
        appeared to collapse the moment you used it.

        A pin instead: the mode does not move, the page keeps every row it had,
        and the rung becomes the design every consumer reads -- the Resolved
        column, the ladder's tick, the preview and the argv all take it off
        the same object, because `_mandrel_pore_design` returns the PINNED
        design rather than the solver's own pick.  The winding-angle ladder
        pins into its own var the same way."""
        self._mandrel_pick_var().set(self._helix_pick_key(design))
        self._mandrel_redraw_panel()

    def _mandrel_pick_var(self):
        """The helix pin the CURRENT design mode's ladder writes."""
        return {"pore": self.m_helix_pick, "angle": self.m_angle_pick,
                "turn": self.m_turn_pick}[self._mandrel_design_mode()]

    def _mandrel_use_cell_alternative(self, key):
        """A cell ladder `Use` click: pin the count AND agree about the fit.

        A ladder row is a (count, fit) pair, so picking one has to write
        `m_cell_fit` too -- otherwise the ticked row and the `Use scaled` /
        `Use fitted` buttons above it would name two different designs."""
        parts = key.split(",")
        if len(parts) == 3:
            self.m_cell_fit.set(parts[2])
        self.m_cell_pick.set(key)
        self._mandrel_redraw_panel()

    def _mandrel_use_auto(self, var):
        """The `Auto` control: drop the pin, back to the solver / the rounding.

        Also takes down a stale drop note -- choosing Auto deliberately is not
        the same event as having a pin taken away, and the page must not go on
        explaining a decision the user has since made themselves."""
        var.set("")
        self._m_helix_dropped = False
        self._m_angle_dropped = False
        self._m_turn_dropped = False
        self._m_cell_dropped = False
        self._mandrel_redraw_panel()

    def _mandrel_redraw_panel(self):
        """Repaint the resolved panel after a click, without the 350 ms wait.

        `after_idle`, not a direct call: the click arrives inside the Use
        button's own command, and the redraw destroys that button.  The
        variable write has already scheduled the 3-D preview through the
        ordinary debounce; this only moves the tick."""
        root = getattr(self, "root", None)
        if root is None:
            return
        try:
            root.after_idle(self._refresh_mandrel_readout)
        except tk.TclError:                     # a window being torn down
            pass

    # ------------------------------------------------------------------ #
    # the two pins: one key format each, one drop rule each
    # ------------------------------------------------------------------ #
    @staticmethod
    def _helix_pick_key(design):
        """`eta,rm,omega` -- a helix rung's identity, to 3 dp of omega.

        The same precision `mew_tube.solve_tube_for_pore` deduplicates its own
        ladder on, so two keys collide exactly when the solver considers the
        two designs the same rung."""
        return "%d,%d,%.3f" % (design.eta, design.rm, design.omega_deg)

    @staticmethod
    def _cell_pick_key(n_around, n_axial, fit):
        """`n_around,n_axial,fit` -- a cell rung's identity."""
        return "%d,%d,%s" % (n_around, n_axial, fit)

    def _mandrel_set_pin(self, var, text):
        """Write a pin WITHOUT re-arming the preview debounce.

        `_schedule_mandrel_preview` reads `_m_solver_writing`; a pin the page
        settles or drops during a refresh is a readout of that refresh, not a
        new edit, so it must not schedule another one."""
        self._m_solver_writing = True
        try:
            if var.get() != text:
                var.set(text)
        finally:
            self._m_solver_writing = False

    def _mandrel_apply_helix_pick(self, best, alts, var=None,
                                  dropped="_m_helix_dropped"):
        """Return the rung the page is USING, and settle the pin around it.

        THE DROP RULE, and it is the same shape for both ladders: a pin is
        kept while a row that still matches it is on the ladder as the ladder
        stands NOW, and dropped otherwise.  For the helix the row IS its
        `(eta, Rm, omega)` -- there is nothing derived to refresh -- so the
        match is the whole key against `[the solver's pick] + the
        alternatives`.  A small edit (a 1 um nudge of the requested pore)
        leaves the same designs on the ladder and keeps the pin; an edit that
        rebuilds the search around a different rod does not, and the panel then
        says so rather than silently reverting.

        THE NOTE IS STICKY, and that is the point of it. Dropping the pin
        empties the variable, so the very next resolve of the same ladder finds
        nothing to drop -- a flag cleared per call would be gone before it was
        ever drawn (measured: the panel redraw after the drop showed no note).
        It is cleared instead by the two things that make it stale: a fresh
        search, and a new pin.

        `var` / `dropped` name the pin and its note: the pore ladder's by
        default, the winding-angle ladder's (`m_angle_pick`) from
        `_mandrel_angle_design`."""
        var = self.m_helix_pick if var is None else var
        pick = (var.get() or "").strip()
        chosen = best
        if pick:
            for design in [best] + list(alts):
                if self._helix_pick_key(design) == pick:
                    chosen = design
                    setattr(self, dropped, False)
                    break
            else:
                setattr(self, dropped, True)
                self._mandrel_set_pin(var, "")
        self._mandrel_write_solved(chosen)
        return chosen

    @staticmethod
    def _mandrel_collector_bounds(design):
        """`(u0, u1)` of the collector for `design` -- the ONE definition.

        Delegates to `gui_mandrel_preview.collector_bounds`, which the preview's
        rod actor and the camera envelope also read, so the page, the panel and
        the player cannot disagree about how long the mandrel is."""
        import gui_mandrel_preview
        return gui_mandrel_preview.collector_bounds(design)

    def _mandrel_place(self):
        """The tube's placement key. A value that is not one is refused by
        `_validate_mandrel` (`placement_problem`), so no program is written
        from it; only the picture of the moment falls back to the left
        end."""
        v = self.m_place.get().strip()
        return v if v in PLACEMENT_KEYS else "left"

    def _mandrel_place_offset(self):
        """The Offset field as a number; a half-typed field reads 0. NOT clamped
        at 0 -- a negative offset hangs the print off the rod, which the
        validator refuses by name rather than the page silently correcting."""
        try:
            return float(self.m_place_offset.get())
        except (ValueError, tk.TclError):
            return 0.0

    def _mandrel_axial_layout(self, design):
        """Where the rod is, where the print starts on it -- collector frame.

        `gui_mandrel_preview.axial_layout` plus the machine page's rod, so
        `layout.print_start_x` is what the generator is told and
        `layout.rod_u0` is where the drawn rod begins, both BEFORE any
        g-code-origin re-base (`_mandrel_origin_u` is that re-base).

        Distinct from `_mandrel_collector_bounds`, which stays the PRINTED
        interval the resolved readout quotes: the margin is bare ROD, so a
        print interval that carried it would report the print as longer than it
        is."""
        import gui_mandrel_preview
        rod_length, rod_x = self._rotary_rod()
        # every copy, and every block's wave, is on the rod; a half-typed
        # copies field reads one copy, and the validator names it
        try:
            copies, gap = self._mandrel_copies(), self._mandrel_copy_gap()
        except (ValueError, tk.TclError):
            copies, gap = 1, 0.0
        return gui_mandrel_preview.axial_layout(
            design, place=self._mandrel_place(),
            offset=self._mandrel_place_offset(),
            rod_length=rod_length, rod_start=rod_x,
            copies=max(1, copies), copy_gap=gap,
            overhang=self._mandrel_tube_overhang(design))

    def _mandrel_origin_u(self, design):
        """The axial coordinate that g-code X = 0 lands on, collector frame.

        The datum is a point on the ROD -- an end, or its mid-length -- and
        never on the print: the rod is what a nozzle can be touched to. So this
        is the ONE number separating the layout above (physical, rod-based)
        from the emitted program (whatever the user zeroed on)."""
        # Always the COLLECTOR origin.
        lay = self._mandrel_axial_layout(design)
        axial, _radial = self._rotary_origin_keys()
        if axial == "far":
            return lay.rod_u1
        if axial == "mid":
            return 0.5 * (lay.rod_u0 + lay.rod_u1)
        if axial == "custom":
            # picked on the mandrel, X from its near end (`_calibrate_zero`)
            p = self._zero_custom_point("mandrel")
            return lay.rod_u0 + (p[1] if p is not None else 0.0)
        return lay.rod_u0

    def _mandrel_surface_z(self, design):
        """The g-code Z of the bare mandrel surface -- the radial datum.

        Delegates to the machine page, which owns the radial ladder; the radius
        is the job's, which is why the call happens here."""
        return self._rotary_surface_z(design.radius)

    def _mandrel_origin_offset(self):
        """The tube's own g-code-origin re-base, asked for by name.

        Separate from the `_origin_offset` override below because the two have
        different preconditions: this one is simply "the tube's offset" and the
        argv builder wants it outright, while the override answers for whatever
        collector is dispatching and therefore depends on a run being in
        flight. A half-typed design has no re-base -- the validator has already
        stopped the run by then, and a traceback out of an arg builder is not
        how the page reports a bad field."""
        try:
            design = self._mandrel_resolved_design()
        except Exception:  # noqa: BLE001
            return (0.0, 0.0, 0.0)
        # `+ 0.0` normalises negative zero: negating a datum of 0.0 gives -0.0,
        # which reaches the worker as the string "-0.0" and makes an otherwise
        # unchanged default argv look changed.
        return (-self._mandrel_origin_u(design) + 0.0, 0.0, 0.0)

    def _origin_offset(self):
        """The g-code-origin re-base, 4-axis flavour while a tube dispatches.

        AXIAL ONLY, by design rather than by omission. `dy` is 0 because a tube
        program emits no Y word at all -- laterally the datum is the mandrel
        axis by construction. `dz` is 0 because the radial datum travels as
        `surface_z` on the cylindrical FRAME instead (see `_rotary_surface_z`):
        re-based here it would be undone twice, once by the frame unrolling the
        file and once by the player putting the scene back into machine
        coordinates, and the tube would sit a radius off its own rod.

        Off a mandrel dispatch this is the plate's own offset, untouched --
        which is what keeps `gui_run`'s frozen origin arg (and therefore the
        player's undo) right for both collectors from one call site."""
        if not getattr(self, "_mandrel_dispatch", False):
            return super()._origin_offset()
        return self._mandrel_origin_offset()

    @classmethod
    def _mandrel_printed_span(cls, design):
        """The length the print OCCUPIES on the rod, not the tiling length.

        A wavy ring waves like every other ring, so the print runs from -A to
        length + A.  A helix and a straight family have no overhang, so for
        them the span IS the length -- which is why the row can be the same
        row."""
        u0, u1 = cls._mandrel_collector_bounds(design)
        return u1 - u0

    # ------------------------------------------------------------------ #
    # the resolved panel -- ONE table skeleton, both families
    # ------------------------------------------------------------------ #
    @staticmethod
    def _mandrel_miss(value, ref):
        """`  (+4.7 %)`, or nothing when there is no request to miss."""
        try:
            ref = float(ref)
        except (TypeError, ValueError):
            return ""
        if not ref:
            return ""
        return "  (%+.1f %%)" % (100.0 * (float(value) - ref) / ref,)

    @staticmethod
    def _mandrel_miss_c(value, ref):
        """`(+4.7%)` -- `_mandrel_miss` without the gutter, for a ladder cell.

        The tables above have three columns to spend width on and the ladder
        has seven; the same string twice over is what pushed it out of the
        form column."""
        try:
            ref = float(ref)
        except (TypeError, ValueError):
            return ""
        if not ref:
            return ""
        return " (%+.1f%%)" % (100.0 * (float(value) - ref) / ref,)

    def _mandrel_panel(self):
        """The resolved-panel frame, emptied. None in a UI that has no form."""
        frame = getattr(self, "_m_resolved", None)
        if frame is None:
            return None
        for child in frame.winfo_children():
            child.destroy()
        return frame

    def _mandrel_table(self, parent, row, headers, rows):
        """The ONE resolution table.

        Rows are what the user ASKED FOR; columns are Requested and then one
        resolved column per answer -- one for the diamond helix, two (Scaled and
        Fitted) for a cell family.  Deliberately the same widget and the same
        skeleton for both, because the question "what did my request turn into"
        is the same question whichever pattern answers it."""
        tbl = ttk.Frame(parent)
        tbl.grid(row=row, column=0, sticky="w")
        for col, text in enumerate(headers):
            ttk.Label(tbl, text=text).grid(
                row=0, column=col, sticky="w" if col == 0 else "e",
                padx=(0, 16), pady=(0, 2))
        ttk.Separator(tbl, orient="horizontal").grid(
            row=1, column=0, columnspan=len(headers), sticky="ew", pady=(0, 4))
        for i, (label, cells) in enumerate(rows):
            ttk.Label(tbl, text=label).grid(row=2 + i, column=0, sticky="w",
                                            padx=(0, 16), pady=1)
            for col, text in enumerate(cells):
                if isinstance(text, tuple):         # (text, command): a choice
                    cell = ttk.Frame(tbl)
                    cell.grid(row=2 + i, column=1 + col, sticky="e",
                              padx=(0, 16), pady=1)
                    ttk.Label(cell, text=text[0]).grid(row=0, column=0,
                                                       padx=(0, 6))
                    ttk.Button(cell, text="Use", width=6,
                               command=text[1]).grid(row=0, column=1)
                    continue
                ttk.Label(tbl, text=text).grid(row=2 + i, column=1 + col,
                                               sticky="e", padx=(0, 16), pady=1)
        return tbl

    def _mandrel_span_note(self, parent, row, design):
        """The one line under the table, drawn ONLY when there is a wave.

        The span used to be a table ROW, which meant a "\u2014" in the Requested
        column and the resolved length restated verbatim on the five families
        that have no overhang: noise wherever it was not news.  Here it is a
        sentence and it exists only when it says something -- so a straight
        family and the diamond draw nothing at all.

        Same fact and same source as the readout sentence (1ef19e2):
        `params["axial_overhang"]` off the design object, never re-derived from
        the amplitude field.  Returns the label, or None when there is no wave.
        """
        over = float((getattr(design, "params", None) or {})
                     .get("axial_overhang", 0.0) or 0.0)
        if not over > 0.0:
            return None
        note = ttk.Label(
            parent, wraplength=460, justify="left",
            text="Rings wave %.3f mm past each end. Printed span %.3f mm."
                 % (over, self._mandrel_printed_span(design)))
        note.grid(row=row, column=0, sticky="w", pady=(6, 0))
        return note

    def _mandrel_ladder(self, parent, row, title, help_text, headers, rows,
                        picked, on_use, on_auto, note=""):
        """THE ONE LADDER WIDGET -- both families, so both LOOK the same.

        The diamond trades length error for pore and the cell families trade
        cell size for count, but the question is identical ("the software
        picked one of these; here are its neighbours"), so it is one widget
        and only the column headers differ.

        `rows` is a sequence of `(key, cells, refusal)`. `cells[0]` is the row
        label and the rest line up under `headers[1:]`; `key` is what a `Use`
        click pins; `refusal` is the engine's own message for a rung it will
        not build, drawn IN THE ROW instead of the cells and with no button --
        an alternative that cannot be made is information, not a traceback and
        not a gap in the ladder.  Exactly one row carries the tick, `picked`.
        """
        box = ttk.Frame(parent)
        box.grid(row=row, column=0, sticky="w", pady=(12, 0))
        head = ttk.Frame(box)
        head.grid(row=0, column=0, sticky="w", pady=(0, 2))
        ttk.Label(head, text=title).grid(row=0, column=0, sticky="w",
                                         padx=(0, 10))
        ttk.Button(head, text="Auto", width=6, command=on_auto).grid(
            row=0, column=1, sticky="w")
        ttk.Label(box, text=help_text, wraplength=460, justify="left").grid(
            row=1, column=0, sticky="w", pady=(0, 6))
        if note:
            ttk.Label(box, text=note, wraplength=460, justify="left").grid(
                row=2, column=0, sticky="w", pady=(0, 6))
        tbl = ttk.Frame(box)
        tbl.grid(row=3, column=0, sticky="w")
        ncol = len(headers) + 1                      # + the Use column
        # 6 px of gutter, not the 16 the tables above use: this widget carries
        # one more column than they do and the cell family's numbers are the
        # widest on the page. MEASURED -- at 12 px the cell ladder wanted
        # 592 px inside a 493 px form column and its whole Use column was
        # clipped off the right edge.
        for col, text in enumerate(headers):
            ttk.Label(tbl, text=text).grid(
                row=0, column=col, sticky="w" if col == 0 else "e",
                padx=(0, 5), pady=(0, 2))
        ttk.Separator(tbl, orient="horizontal").grid(
            row=1, column=0, columnspan=ncol, sticky="ew", pady=(0, 4))
        for i, (key, cells, refusal) in enumerate(rows):
            ttk.Label(tbl, text=cells[0]).grid(row=2 + i, column=0, sticky="w",
                                               padx=(0, 5), pady=1)
            if refusal is not None:
                ttk.Label(tbl, text=str(refusal), wraplength=380,
                          justify="left").grid(
                              row=2 + i, column=1, columnspan=ncol - 1,
                              sticky="w", padx=(0, 5), pady=1)
                continue
            for col, text in enumerate(cells[1:]):
                ttk.Label(tbl, text=text).grid(row=2 + i, column=1 + col,
                                               sticky="e", padx=(0, 5), pady=1)
            ttk.Button(tbl, text="Use" + (" ✓" if key == picked else ""),
                       width=6, command=lambda k=key: on_use(k)).grid(
                           row=2 + i, column=ncol - 1, sticky="e",
                           padx=(0, 2), pady=1)
        return box

    # ------------------------------------------------------------------ #
    # the cell families' ladder: the counts either side of the rounding
    # ------------------------------------------------------------------ #
    def _mandrel_build_cell_ladder(self):
        """`n_around` in {n-1, n, n+1} x the two fits, as ladder rows.

        Reads the RAW fields rather than `_mandrel_cell_kwargs`, because that
        one now carries the pinned counts and asking it here would be circular:
        the ladder is what decides whether the pin is still valid.

        A rung the engine refuses (n_around = 1 cannot close a circumference)
        keeps its row and carries the message; it is not dropped, because the
        gap would then have to be explained by its absence."""
        import gui_mandrel_preview
        fam = self._mandrel_family_value()
        diameter = float(self.m_diameter.get())
        target = float(self.m_length.get())
        req_w = float(self.m_cell_w.get())
        req_h = float(self.m_cell_h.get())
        rib = float(self.m_rib_angle.get())
        amp = float(self.m_cell_amplitude.get() or 0.0)
        graph = self.m_cell_graph.get().strip()
        try:
            base = gui_mandrel_preview.cell_design(
                family=fam, diameter=diameter, target_length=target,
                cell_w=req_w, cell_h=req_h, fit="scale", rib_angle=rib,
                amplitude=amp, graph=graph)
        except Exception:  # noqa: BLE001 -- the panel prints it above the ladder
            return []
        rows = []
        for n in (base.n_around - 1, base.n_around, base.n_around + 1):
            if n < 1:
                continue
            for fit in ("scale", "fit"):
                try:
                    d = gui_mandrel_preview.cell_design(
                        family=fam, diameter=diameter, target_length=target,
                        cell_w=req_w, cell_h=req_h, fit=fit, rib_angle=rib,
                        amplitude=amp, graph=graph, n_around=n)
                except Exception as exc:  # noqa: BLE001 -- shown in the row
                    rows.append(("%d,,%s" % (n, fit), (fit,), exc))
                    continue
                cells = (fit, str(d.n_around), str(d.n_axial),
                         "%.2f × %.2f%s"
                         % (d.cell_w, d.cell_h,
                            self._mandrel_miss_c(d.cell_w, req_w)),
                         "%.3f%s" % (d.length,
                                     self._mandrel_miss_c(d.length, target)),
                         "%.3f" % self._mandrel_printed_span(d))
                rows.append((self._cell_pick_key(d.n_around, d.n_axial, fit),
                             cells, None))
        return rows

    def _mandrel_cell_ladder(self):
        """The cell ladder for the fields as they stand, pin settled.

        Cached on every field it reads, for the reason `_mandrel_pore_design`
        is: six routed designs are six Euler circuits, and the validator, the
        preview snapshot, the panel and the argv builder all want the same six
        inside one refresh.  MEASURED on the desktop, at the page defaults,
        for one CACHE MISS on the Tk thread: re-entrant 122.9 ms, brick-open
        32.2 ms, chevron 4.8 ms.  That is inside the 350 ms debounce and small
        beside the tube rebuild it shares a refresh with (seconds), but it is
        Tk-thread time and it is why the cache is not optional."""
        # EVERY field `_mandrel_build_cell_ladder` reads is in the key --
        # including the DRAWING, which is a family knob exactly as the rib angle
        # and the amplitude are. Leaving it out kept the previous cell's six
        # rows after an edit, and the page has no other source for them.
        key = (self._mandrel_family_value(), self.m_diameter.get(),
               self.m_length.get(), self.m_cell_w.get(), self.m_cell_h.get(),
               self.m_rib_angle.get(), self.m_cell_amplitude.get(),
               self.m_cell_graph.get())
        cached = getattr(self, "_m_cell_ladder_cache", None)
        if cached is None or cached[0] != key:
            cached = (key, self._mandrel_build_cell_ladder())
            self._m_cell_ladder_cache = cached
            self._m_cell_dropped = False        # a new ladder, a new note
        self._mandrel_settle_cell_pin(cached[1])
        return cached[1]

    def _mandrel_settle_cell_pin(self, rows):
        """THE CELL DROP RULE -- the twin of `_mandrel_apply_helix_pick`.

        Matched on `(n_around, fit)`, not on the whole key: `n_axial` is
        DERIVED from the requested length and the cell width, so an edit that
        moves it has changed what the row SAYS, not which row it is.  The
        stored key is rewritten with the new count instead, which is what lets
        a pinned count survive an edit to the tube length.  The pin is dropped
        only when no row offers that count and fit at all -- the rounding has
        moved by more than one, or that rung now refuses -- and then the panel
        carries a note."""
        pick = (self.m_cell_pick.get() or "").strip()
        if not pick:
            return
        want = pick.split(",")
        if len(want) != 3:
            self._mandrel_set_pin(self.m_cell_pick, "")
            return
        for key, _cells, refusal in rows:
            if refusal is not None:
                continue
            got = key.split(",")
            if got[0] == want[0] and got[2] == want[2]:
                self._mandrel_set_pin(self.m_cell_pick, key)
                self._m_cell_dropped = False
                return
        self._m_cell_dropped = True
        self._mandrel_set_pin(self.m_cell_pick, "")

    def _mandrel_cell_counts(self):
        """The pinned `(n_around, n_axial)`, or `(None, None)` for the rounding.

        Settling the pin is part of reading it, so every consumer of the counts
        sees the same drop decision."""
        self._mandrel_cell_ladder()
        pick = (self.m_cell_pick.get() or "").strip().split(",")
        if len(pick) != 3:
            return (None, None)
        try:
            return (int(pick[0]), int(pick[1]))
        except ValueError:                      # pragma: no cover -- a hand-
            return (None, None)                 # edited preset

    def _mandrel_render_resolved(self, fam, design, error=None):
        """Draw the panel for whatever was resolved, or nothing on a refusal.

        `design` is None when the request was refused: the panel is cleared and
        the sentence label above carries the engine's own message verbatim, the
        posture `_refresh_mandrel_readout` has always taken -- except for a
        `_LengthGap`, where the table itself offers the nearest lengths that
        close (`_mandrel_render_gap`)."""
        panel = self._mandrel_panel()
        if panel is None:
            return
        label = getattr(self, "_m_readout_label", None)
        try:
            if design is None and isinstance(error, _LengthGap) \
                    and (error.shorter or error.longer):
                if label is not None:
                    label.grid_remove()
                self._mandrel_render_gap(panel, error)
                return
            if design is None:
                if label is not None:
                    label.grid()
                return
            # The sentence form duplicates the table when the table is drawn,
            # so it steps aside and comes back for the refusal case above.
            if label is not None:
                label.grid_remove()
            if fam == "zigzag":
                self._mandrel_render_helix(panel, design)
            else:
                self._mandrel_render_cells(panel, design)
        except tk.TclError:                      # a page rebuild mid-refresh
            pass

    def _mandrel_length_gap(self):
        """The standing `_LengthGap`, or None -- for the G-code page, which
        leaves this refusal to the Tube box instead of its banner."""
        try:
            self._mandrel_resolved_design()
        except _LengthGap as gap:
            return gap
        except Exception:  # noqa: BLE001 -- any other refusal is not a gap
            return None
        return None

    def _mandrel_render_gap(self, panel, gap):
        """The refused length, and the nearest that close, as `Use` rows of
        the same What-you-asked-for table. `Use` writes the tube length; the
        nearest closing tube to that length is then that design exactly."""
        ttk.Label(panel, text=gap.headline + " Use a length that does:",
                  wraplength=460, justify="left").grid(
                      row=0, column=0, sticky="w", pady=(0, 6))
        want = self.m_length.get()
        opts = []
        for d in (gap.shorter, gap.longer):
            if d is not None:
                opts.append(("%.3f%s, pore %.0f \u00b5m"
                             % (d.length, self._mandrel_miss(d.length, want),
                                1000.0 * math.sqrt(d.pore_area)),
                             lambda L=d.length: self._mandrel_use_length(L)))
        rows = [(self._mandrel_length_label() + " (mm)", ["%.3f" % float(want), opts[0]])]
        rows += [("", ["", o]) for o in opts[1:]]
        self._mandrel_table(panel, 1, ("What you asked for", "Requested",
                                       "Resolved"), rows)

    def _mandrel_use_length(self, length):
        """A gap row's `Use`: the tube length becomes that closing length."""
        self.m_length.set("%.3f" % (length,))
        self._mandrel_redraw_panel()

    def _mandrel_render_helix(self, panel, design):
        """Requested vs Resolved, then the trade ladder."""
        mode = self._mandrel_design_mode()
        side_um = 1000.0 * math.sqrt(design.pore_area)
        want_um = None
        if mode in ("pore", "turn"):
            try:
                want_um = float(self.m_pore_target.get())
            except (ValueError, tk.TclError):
                want_um = None
        rows = []
        if mode == "turn":
            rows.append(("Winding angle (\u00b0)",
                         ["%.2f" % float(self.m_angle.get()),
                          "%.2f%s" % (design.omega_deg,
                                      self._mandrel_miss(design.omega_deg,
                                                         self.m_angle.get()))]))
        rows += [
            ("Pore size (\u00b5m)",
             ["%.0f" % want_um if want_um else "\u2014",
              "%.0f%s" % (side_um, self._mandrel_miss(side_um, want_um))]),
            (self._mandrel_length_label() + " (mm)",
             ["%.3f" % float(self.m_length.get()),
              "%.3f%s" % (design.length,
                          self._mandrel_miss(design.length,
                                             self.m_length.get()))]),
        ]
        turned = self._mandrel_is_turned(design)
        if turned:
            # what the exact length costs: the arc the end turn lays, in the
            # unit a user can picture on the rod
            rows.append(("Turn at each end",
                         ["\u2014", "%.1f\u00b0 (%.2f mm of fiber)"
                          % (design.end_turn_deg,
                             math.radians(design.end_turn_deg)
                             * design.radius)]))
        self._mandrel_table(panel, 0, ("What you asked for", "Requested",
                                       "Resolved"), rows)
        # A helix has no overhang, so this draws nothing -- but it is the SAME
        # rule as the cell families', asked of the design object rather than
        # assumed from the family, so a helix that ever grew one would say so.
        self._mandrel_span_note(panel, 1, design)
        if self._mandrel_closure_typed():
            return                  # the user typed eta / Rm: nothing to offer
        pore = (mode == "pore")
        tag = {"pore": "_m_pore", "angle": "_m_angle", "turn": "_m_turn"}[mode]
        alts = getattr(self, tag + "_alternatives", None) or []
        solver_pick = getattr(self, tag + "_solver_pick", None)
        if solver_pick is None or not alts:
            return
        # ROW 1 IS THE SOLVER'S OWN PICK, so the ladder holds every design the
        # page can be in and exactly one row always carries the tick. `Auto`
        # returns to that row; it is not a seventh choice.
        # No eta / Rm / N columns: they are how a row
        # closes, not what it gives you, and the page picks them for you.
        # Length and pore are the trade; omega too in pore mode, where it moves.
        by_key = {}
        rows = []
        for i, alt in enumerate([solver_pick] + list(alts)):
            key = self._helix_pick_key(alt)
            if key in by_key:                   # the pick repeated in the list
                continue
            by_key[key] = alt
            s_um = 1000.0 * math.sqrt(alt.pore_area)
            cells = ["Solver's pick" if i == 0 else "#%d" % (i + 1,)]
            if pore:
                cells.append("%.1f\u00b0" % alt.omega_deg)
            if mode == "turn":
                # angle and length are the user's on every row; what moves is
                # the pore and the end turn that closes it
                cells += ["%.0f%s" % (s_um, self._mandrel_miss_c(s_um,
                                                                 want_um)),
                          "%.1f\u00b0" % alt.end_turn_deg]
                rows.append((key, tuple(cells), None))
                continue
            cells += ["%.3f%s" % (alt.length,
                                  self._mandrel_miss_c(alt.length,
                                                       self.m_length.get())),
                      "%.0f%s" % (s_um, self._mandrel_miss_c(s_um, want_um))]
            rows.append((key, tuple(cells), None))
        var = self._mandrel_pick_var()
        if mode == "turn":
            title, help_, cols = (
                "Other pore sizes at this angle and length",
                "The software's pick is the pore nearest yours. Use takes a "
                "coarser or finer one, and Auto goes back; pore in \u00b5m, "
                "and the turn each one needs at the tube's ends.",
                ("", "Pore", "End turn"))
        elif pore:
            title, help_, cols = (
                "Trade length error for pore",
                "Use pins a rung and Auto goes back to the software's own "
                "pick; length in mm, pore in \u00b5m.",
                ("", "\u03c9", "Length", "Pore"))
        else:
            title, help_, cols = (
                "Other pore sizes at this angle",
                "The software's pick is the closing length nearest yours. Use "
                "takes a coarser or finer pore instead, and Auto goes back; "
                "length in mm, pore in \u00b5m.",
                ("", "Length", "Pore"))
        self._mandrel_ladder(
            panel, 2, title, help_, cols,
            rows, self._helix_pick_key(design),
            lambda key: self._mandrel_use_alternative(by_key[key]),
            lambda: self._mandrel_use_auto(var),
            note=("The alternative you had pinned is no longer on the ladder, "
                  "so this is the software's pick again."
                  if getattr(self, {"pore": "_m_helix_dropped",
                                    "angle": "_m_angle_dropped",
                                    "turn": "_m_turn_dropped"}[mode], False)
                  else ""))

    def _mandrel_render_cells(self, panel, design):
        """Requested vs Scaled vs Fitted, the span note, then the two fits."""
        fits = self._mandrel_cell_fits()
        chosen = self.m_cell_fit.get().strip() or "scale"

        def cell(fit, get, ref=None, fmt="%.3f"):
            d = fits.get(fit)
            if not hasattr(d, "length"):
                return "\u2014"
            val = get(d)
            return (fmt % val) + (self._mandrel_miss(val, ref)
                                  if ref is not None else "")

        req_w, req_h = self.m_cell_w.get(), self.m_cell_h.get()
        req_len = self.m_length.get()
        rows = [
            ("Cell along the tube (mm)",
             ["%.3f" % float(req_w),
              cell("scale", lambda d: d.cell_w, req_w),
              cell("fit", lambda d: d.cell_w, req_w)]),
            ("Cell around the tube (mm)",
             ["%.3f" % float(req_h),
              cell("scale", lambda d: d.cell_h, req_h),
              cell("fit", lambda d: d.cell_h, req_h)]),
            (self._mandrel_length_label() + " (mm)",
             ["%.3f" % float(req_len),
              cell("scale", lambda d: d.length, req_len),
              cell("fit", lambda d: d.length, req_len)]),
        ]
        self._mandrel_table(panel, 0, ("What you asked for", "Requested",
                                       "Scaled", "Fitted"), rows)
        self._mandrel_span_note(panel, 1, design)
        btns = ttk.Frame(panel)
        btns.grid(row=2, column=0, sticky="w", pady=(8, 0))
        for col, (fit, text) in enumerate((("scale", "Use scaled"),
                                           ("fit", "Use fitted"))):
            ttk.Button(btns, text=text + ("  \u2713" if fit == chosen else ""),
                       command=lambda f=fit: self._mandrel_use_fit(f)).grid(
                           row=0, column=col, sticky="w", padx=(0, 8))
        # Devlin's third option, "no change", cannot close a circumference
        # (`mew_unit_cell.py:851-855`) and is not offered.
        # ... and the count ladder, the cell families' answer to the diamond's
        # trade: the rounding has neighbours, and they are the same widget.
        rows = self._mandrel_cell_ladder()
        if not rows:
            return
        self._mandrel_ladder(
            panel, 3, "Other cell counts around the tube",
            "Use pins a count and Auto goes back to the rounding a whole "
            "number of cells closing the circumference forces; cell, "
            "length and span in mm.",
            ("Fit", "Around", "Along", "Cell", "Length", "Span"),
            rows, (self.m_cell_pick.get() or "").strip()
            or self._cell_pick_key(design.n_around, design.n_axial,
                                   design.fit),
            self._mandrel_use_cell_alternative,
            lambda: self._mandrel_use_auto(self.m_cell_pick),
            note=("The cell count you had pinned is no longer on the ladder, "
                  "so this is the rounded count again."
                  if getattr(self, "_m_cell_dropped", False) else ""))

    def _refresh_mandrel_readout(self, design=None, a_max=None, error=None):
        """Restate the resolved design, as a table and as one sentence.

        Every number is read off the design object; none is recomputed here.
        The A end angle likewise comes off the emitted g-code (the same way
        `scaffold_gcode.main` reports it), never from a second derivation.

        The sentence in `m_cell_readout` is kept for BOTH outcomes, because it
        is where a refusal reaches the user verbatim; it is only DISPLAYED when
        the table is not, so the page never states the same number twice."""
        var = getattr(self, "m_cell_readout", None)
        if var is None:
            return
        fam = self._mandrel_family_value()
        try:
            if error is not None:
                raise error if isinstance(error, BaseException) else \
                    ValueError(str(error))
            if design is None:
                design = self._mandrel_resolved_design()
            if fam == "zigzag":
                var.set(
                    "Resolved: closing length %.4f mm (requested %.4f mm) \u00b7 "
                    "%d pivots \u00b7 pivot angle %.2f\u00b0 \u00b7 pore area %.4f mm\u00b2%s"
                    % (design.length, float(self.m_length.get()),
                       design.n_pivots, design.pivot_angle, design.pore_area,
                       " \u00b7 turns %.2f\u00b0 at each end"
                       % design.end_turn_deg
                       if self._mandrel_is_turned(design) else ""))
                self._mandrel_render_resolved(fam, design)
                return
            if a_max is None:
                a_max = self._mandrel_cell_a_max(design)
            req = design.requested
            # `length` is the cell TILING, not the extent: a wavy ring waves
            # like every other ring, so the print runs from -A to length + A
            # and is 2A longer than the number beside it.  Read off
            # `params["axial_overhang"]`, which the engine puts there for
            # exactly this, never re-derived from the amplitude field.  Said
            # only when it is non-zero, so a straight family's readout is
            # unchanged to the character.
            u0, u1 = self._mandrel_collector_bounds(design)
            over = -u0
            span = ("" if over <= 0.0 else
                    ", printed span %.4f mm (rings wave %.4f mm past each end)"
                    % (u1 - u0, over))
            lines = [
                "Resolved: %d \u00d7 %d cells of %.4f \u00d7 %.4f mm \u2192 length %.4f mm%s "
                "(requested %.4f mm of %.4f \u00d7 %.4f mm, fit \u2018%s\u2019)"
                % (design.n_axial, design.n_around, design.cell_w,
                   design.cell_h, design.length, span, req["target_length"],
                   req["cell_w"], req["cell_h"], design.fit),
                "%.1f %% of the path retraced (%.4f of %.4f mm, %d of %d "
                "members) \u00b7 %d rotation reversals \u00b7 A ends at %.1f\u00b0 "
                "(%.4g turns)"
                % (100.0 * design.retraced_length / design.total_length,
                   design.retraced_length, design.total_length,
                   design.n_retraced, design.n_members, design.reversals,
                   a_max, a_max / 360.0)]
            if design.unfold_strain is not None:
                lines.append(
                    "Unfolds at %.1f %% longitudinal / %.1f %% radial strain "
                    "(derived kinematics with rigid ribs and hinged nodes, not a "
                    "mechanics prediction)"
                    % (100.0 * design.unfold_strain["longitudinal"],
                       100.0 * design.unfold_strain["radial"]))
            var.set("\n".join(lines))
            self._mandrel_render_resolved(fam, design)
        except Exception as exc:  # noqa: BLE001  -- a half-typed field
            var.set("Resolved: \u2014 (%s)" % (exc,))
            self._mandrel_render_resolved(fam, None, exc)

    def _mandrel_cell_a_max(self, design):
        """Where the mandrel ends up, read off the emitted program."""
        import gui_mandrel_preview
        import scaffold_gcode
        gcode = scaffold_gcode.generate_mandrel_cell_gcode(
            float(self.m_diameter.get()), design,
            self._mandrel_fiber_diameter(), self._mandrel_layers(),
            self._mandrel_speed(), speed_mode=self.m_speed_mode.get())
        return gui_mandrel_preview.a_span_deg(gcode)

    # ------------------------------------------------------------------ #
    # mode, validation and the worker
    # ------------------------------------------------------------------ #
    def _mandrel_is_active(self):
        """True when the Scaffold designer's collector toggle is on the mandrel.

        Deliberately independent of `_mandrel_dispatch`, which inverts
        `_active_mode()` for the duration of a run so the shared scaffold run
        path is taken."""
        var = getattr(self, "s_collector", None)
        return var is not None and var.get() == "mandrel"

    # ------------------------------------------------------------------ #
    # the pore solver seam -- the page resolves, the generator never does
    # ------------------------------------------------------------------ #
    def _mandrel_pore_target_area(self):
        """The requested pore as an AREA in mm^2.

        The page collects a SIDE in micrometres, because that is how a MEW user
        states a pore and because area hides the quadratic (halving a pore looks
        like a quarter).  This is the ONE place it is squared."""
        side_um = float(self.m_pore_target.get())
        if not side_um > 0.0:
            raise ValueError(
                "pore size must be positive, got %r um" % (side_um,))
        return (side_um / 1000.0) ** 2

    def _mandrel_pore_design(self):
        """Resolve omega / eta / Rm from the requested pore; keep the ladder.

        Cached on every number the search reads, because `_validate_mandrel`,
        the preview snapshot, the resolved panel and the argv builder all want
        the same answer inside one refresh and a default call is a 17.6 ms
        search.  A stale entry cannot survive an edit: the key IS the inputs.

        `mew_tube.TubeDesignError` propagates -- its message names the number it
        refused and the way out, and the panel prints it verbatim rather than
        clamping the field, which is the defect class this redesign removes."""
        import mew_tube
        key = (float(self.m_diameter.get()), float(self.m_length.get()),
               self._mandrel_pore_target_area(),
               self.m_length_tol.get().strip(), self.m_eta_cap.get().strip(),
               self._mandrel_fiber_diameter())
        cached = getattr(self, "_m_pore_cache", None)
        if cached is None or cached[0] != key:
            kw = dict(length_tol=float(self.m_length_tol.get()) / 100.0,
                      fiber_diameter=key[5])
            if key[4]:                   # blank -> the solver's own ceiling
                kw["max_eta"] = int(key[4])
            best, alts = mew_tube.solve_tube_for_pore(
                0.5 * key[0], key[1], key[2], **kw)
            cached = (key, best, list(alts))
            self._m_pore_cache = cached
            self._m_helix_dropped = False       # a new search, a new note
        # The SOLVER's own answer is kept beside the ladder: it is row 1, it is
        # what `Auto` returns to, and it is what a dropped pin falls back on.
        self._m_pore_solver_pick = cached[1]
        self._m_pore_alternatives = cached[2]
        return self._mandrel_apply_helix_pick(cached[1], cached[2])

    def _mandrel_angle_design(self):
        """Winding-angle mode's closure: eta / Rm resolved, the ladder kept.

        The pick is `mew_tube.solve_tube_options`, which is `solve_tube` -- so
        at the default 5 % tolerance it is the tube the CLI builds when no eta
        is given. The ladder is the other pore sizes this angle closes at
        inside the tolerance. Cached on its inputs like the pore search, so the
        validator, the snapshot, the panel and the argv share one answer."""
        import mew_tube
        key = (float(self.m_diameter.get()), float(self.m_angle.get()),
               float(self.m_length.get()),
               float(self.m_length_tol.get()) / 100.0,
               self._mandrel_fiber_diameter())
        cached = getattr(self, "_m_angle_cache", None)
        if cached is None or cached[0] != key:
            try:
                best, alts = mew_tube.solve_tube_options(
                    0.5 * key[0], key[1], key[2], length_tol=key[3],
                    fiber_diameter=key[4])
            except mew_tube.TubeDesignError as exc:
                raise _LengthGap.from_request(*key[:4]) or exc
            cached = (key, best, list(alts))
            self._m_angle_cache = cached
            self._m_angle_dropped = False       # a new ladder, a new note
        self._m_angle_solver_pick = cached[1]
        self._m_angle_alternatives = cached[2]
        return self._mandrel_apply_helix_pick(
            cached[1], cached[2], self.m_angle_pick, "_m_angle_dropped")

    def _mandrel_turn_design(self):
        """End-turn mode: omega, length and pore in, all three built.

        `mew_tube.solve_turned_tube` keeps the angle and the length exactly
        and takes the even pivot count nearest the pore; the ladder is the
        neighbouring counts, pinned in `m_turn_pick`. Cached on its inputs
        like the other two searches."""
        import mew_tube
        key = (float(self.m_diameter.get()), float(self.m_angle.get()),
               float(self.m_length.get()), self._mandrel_pore_target_area(),
               self._mandrel_fiber_diameter())
        cached = getattr(self, "_m_turn_cache", None)
        if cached is None or cached[0] != key:
            best, alts = mew_tube.solve_turned_tube(
                0.5 * key[0], key[1], key[2], key[3], fiber_diameter=key[4])
            cached = (key, best, list(alts))
            self._m_turn_cache = cached
            self._m_turn_dropped = False        # a new ladder, a new note
        self._m_turn_solver_pick = cached[1]
        self._m_turn_alternatives = cached[2]
        return self._mandrel_apply_helix_pick(
            cached[1], cached[2], self.m_turn_pick, "_m_turn_dropped")

    def _mandrel_is_turned(self, design):
        """Does this design close by turning at its ends (`--tube-pivots`)?"""
        import mew_tube
        return isinstance(design, mew_tube.TurnedTubeDesign)

    def _mandrel_solved_design(self):
        """The design the PAGE resolved, or None when the user typed it (a
        cell family, or winding-angle mode with the override ticked)."""
        if self._mandrel_family_value() != "zigzag" \
                or self._mandrel_closure_typed():
            return None
        mode = self._mandrel_design_mode()
        if mode == "turn":
            return self._mandrel_turn_design()
        if mode == "pore":
            return self._mandrel_pore_design()
        return self._mandrel_angle_design()

    def _mandrel_write_solved(self, design):
        """Show the resolved closure in the Advanced fields, without re-arming.

        Those vars are traced onto the preview debounce, so a solver write
        that scheduled another refresh would resolve again, write again, and
        never settle.  `_m_solver_writing` is read by
        `_schedule_mandrel_preview`; the values themselves still come off the
        design object at every consumer, so what is written here is a readout,
        not a source.  Omega is written only in pore mode: in winding-angle
        mode it is the user's own field, and a "%g" echo would rewrite "40.0"
        as "40" under their cursor."""
        if self._mandrel_design_mode() == "turn":
            return                  # no eta / Rm rows in this mode to fill
        pairs = [(self.m_eta, str(design.eta)), (self.m_rm, str(design.rm))]
        if self._mandrel_design_mode() == "pore":
            pairs.insert(0, (self.m_angle, "%g" % (design.omega_deg,)))
        self._m_solver_writing = True
        try:
            for var, text in pairs:
                if var.get() != text:
                    var.set(text)
        finally:
            self._m_solver_writing = False

    def _mandrel_design(self):
        solved = self._mandrel_solved_design()
        if solved is not None:
            return solved
        import scaffold_gcode
        eta = self.m_eta.get().strip()
        return scaffold_gcode.mandrel_design(
            float(self.m_diameter.get()), float(self.m_angle.get()),
            float(self.m_length.get()), eta=(int(eta) if eta else None),
            rm=int(self.m_rm.get() or 0))

    def _validate_mandrel(self):
        check = getattr(self, "_zero_custom_error", None)
        e = check("mandrel") if callable(check) else None
        if e:
            return e
        fam = self._mandrel_family_value()
        try:
            # Resolving the design IS the validation for both families: the
            # engine raises with a message that names the offending number and
            # what to do about it, and that message is what the user sees.
            self._mandrel_resolved_design()
            if float(self._mandrel_standoff()) <= 0 or self._mandrel_fiber_diameter() <= 0:
                return "Physics standoff and fiber diameter must be positive."
            if self._mandrel_speed() <= 0:
                return "Surface speed must be positive."
            if (self._mandrel_rotary_feed() == "split"
                    and self.m_feed_mode.get() == "fixed"):
                try:
                    f, e = self._mandrel_fixed_feed()
                except ValueError:
                    return "Fixed F and E must both be numbers."
                if f <= 0 or e <= 0:
                    return "Fixed F and E must both be positive."
            n = self._mandrel_layers()
            if n < 1:
                return "Layers must be at least 1 on a mandrel."
            # the tube tables
            step = self._mandrel_layer_step()
            if step is not None and not step > 0:
                return ("Layer step must be positive (blank steps out one "
                        "fiber diameter).")
            copies = self._mandrel_copies()
            if copies < 1:
                return "Copies must be at least 1."
            gap = self._mandrel_copy_gap()
            if copies > 1 and not (gap >= 0.0 and math.isfinite(gap)):
                return "The gap between copies cannot be negative."
            for layer, row in sorted(self._mandrel_layer_overrides().items()):
                for key in ("winding_angle", "cell_width", "cell_height",
                            "speed", "step"):
                    if key in row and not float(row[key]) > 0:
                        return ("Layer %d: %s must be positive."
                                % (layer + 1, key.replace("_", " ")))
            # resolving the blocks IS their check: the engine names a block
            # that will not resolve or would not share the tube's length
            self._mandrel_tube_blocks()
            if copies > 1 and self.s_exit_move.get():
                margin = float(self.s_exit_margin.get().strip() or "5")
                over = self._mandrel_tube_overhang(
                    self._mandrel_resolved_design())
                if margin > gap + over + 1e-9:
                    return ("The exit leaves the last copy into the gap "
                            "behind it: a %g mm gap is shorter than the %g mm "
                            "exit, which would park on the copy before. Widen "
                            "the gap or shorten the exit." % (gap, margin))
            if float(self.s_sag_mult.get()) < 0:
                return "Sag multiplier cannot be negative."
            e = self._faces_error(self.s_faces.get())
            if e:
                return e
            for label, v in (("S", self.m_support), ("R_ax", self.m_lag_axial),
                             ("R_cir", self.m_lag_circ)):
                if v.get().strip() and float(v.get()) <= 0:
                    return "%s must be positive when supplied." % label
        except Exception as exc:
            # A letter in a number box surfaces as Python's own parse error;
            # name the field instead. Anything else is the engine's message.
            return (self._mandrel_unparsable_field()
                    if isinstance(exc, ValueError) else None) or str(exc)
        # NaN passes every `< 0` / `<= 0` test above and would reach the argv
        # as `--start-x nan` / `--exit-margin nan`.
        bad = placement_problem(self.m_place.get())
        if bad:
            return bad
        if not math.isfinite(self._mandrel_place_offset()):
            return "Offset must be a finite number."
        if self.s_exit_move.get():
            _v, err = self._pos_float(self.s_exit_margin.get().strip() or "5",
                                      "Exit distance", allow_zero=True)
            if err:
                return err
        self._rotary_vars_make()
        if self.rotary_on.get():
            for var, label in ((self.rotary_rod_length, "Mandrel length"),
                               (self.rotary_rod_x, "Mandrel near end X")):
                if var.get().strip():
                    _v, err = self._any_float(var.get(), label)
                    if err:
                        return err + " (Printer & collector page)"
        # A print that hangs off the rod is NOT refused: the picture draws
        # it hanging off, and that is enough: no banner
        # for it. `AxialLayout.clearance` still reads negative for it.
        if self._mandrel_place_offset() < 0.0:
            return ("The offset cannot be negative: it is a distance in from "
                    "the end of the mandrel. Use 0 to start flush with it.")
        return None

    def _mandrel_unparsable_field(self):
        """The first number box that does not parse, as a sentence naming it,
        or None. Only consulted once resolving the design has already failed
        with a ValueError, so a box the current mode ignores is never blamed
        for a run that would otherwise go."""
        sv = self.scaf_vars[0]
        for var, label, kind, optional in (
                (sv["layers"], "Layers", int, False),
                (sv["speed"], "Speed", float, False),
                (sv["diameter"], "Fiber diameter", float, False),
                (self.s_faces, "Tube facets", int, False),
                (self.s_sag_mult, "Sag multiplier", float, False),
                (self.m_diameter, "Mandrel diameter", float, False),
                (self.m_length, self._mandrel_length_label(), float, False),
                (self.m_angle, "Winding angle", float, False),
                (self.m_pore_target, "Pore size", float, False),
                (self.m_length_tol, "Length may miss by (%)", float, False),
                (self.m_eta_cap, "η search cap", int, True),
                (self.m_support, "Support S", float, True),
                (self.m_lag_axial, "Measured R_ax", float, True),
                (self.m_lag_circ, "Measured R_cir", float, True),
                (self.m_layer_step, "Layer step", float, True),
                (self.m_turn_out, "Perimeter travel offset", float, True),
                (self.m_copies, "Copies", int, True),
                (self.m_copy_gap, "Gap between copies", float, True)):
            text = var.get().strip()
            if optional and not text:
                continue
            try:
                kind(text)
            except ValueError:
                what = "a whole number" if kind is int else "a number"
                return f"{label} must be {what} (it reads “{text}”)."
        return None

    def _build_args_mandrel(self):
        """The worker argv for a tube.

        The ‘zigzag’ branch is the argv this page emitted BEFORE the cell
        families existed, flag for flag and in the same order -- including the
        absence of ``--tube-family``, whose default is ‘zigzag’.  A cell family
        swaps the helix closure flags (``--winding-angle`` / ``--tube-eta`` /
        ``--tube-rm`` / ``--tube-strategy``, all of which `scaffold_gcode`
        ignores in that branch) for the cell ones; everything from
        ``--tube-speed-mode`` on is common, as is the Export panel below."""
        fam = self._mandrel_family_value()
        # A page-resolved closure (pore mode, or winding-angle mode without
        # Advanced's override) sends the RESOLVED eta / Rm and the RESOLVED
        # length. With eta pinned `mandrel_design` returns `TubeDesign(radius,
        # omega, eta, Rm)` directly and ignores the target length, so the
        # worker reproduces the page's design -- a pinned ladder rung included
        # -- instead of re-solving; and sending the resolved length is what
        # keeps the CLI's own out-of-sync note from firing on every run.
        # Omega is resolved only in pore mode; in winding-angle mode it goes
        # out as typed, which is the same number.
        d = self._mandrel_solved_design() if fam == "zigzag" else None
        solved = d is not None
        pore = solved and self._mandrel_design_mode() == "pore"
        head = ["-o", self.s_gcode_out.get().strip(),
                "--mandrel", self.m_diameter.get(),
                "--tube-length", (repr(d.length) if solved
                                  else self.m_length.get())]
        if fam == "zigzag":
            head += ["--winding-angle", (repr(d.omega_deg) if pore
                                         else self.m_angle.get()),
                     "--tube-rm", ("0" if self._mandrel_is_turned(d)
                                   else str(d.rm) if solved
                                   else self.m_rm.get()),
                     "--tube-strategy", self.m_strategy.get()]
        else:
            head += ["--tube-family", fam,
                     "--cell-width", self.m_cell_w.get().strip(),
                     "--cell-height", self.m_cell_h.get().strip(),
                     "--cell-fit", self.m_cell_fit.get().strip()]
            # ... and the pinned rung of the count ladder, when there is one.
            # Absent is the generator's own rounding, so an unpinned page emits
            # the argv it emitted before the ladder existed.
            n_around, n_axial = self._mandrel_cell_counts()
            if n_around is not None:
                head += ["--cell-n-around", str(n_around),
                         "--cell-n-along", str(n_axial)]
            # `mew_unit_cell._resolve_params` REJECTS a knob the family does
            # not take, so exactly one of these two may be sent.
            if fam == "reentrant":
                head += ["--rib-angle", self.m_rib_angle.get().strip()]
            elif fam == MANDREL_DRAWN_FAMILY:
                head += ["--cell-graph", self.m_cell_graph.get().strip()]
            else:
                head += ["--cell-amplitude",
                         self.m_cell_amplitude.get().strip() or "0"]
        # WHERE ON THE ROD, AND WHERE ZERO IS. `--start-x` is the tube's near
        # end in the COLLECTOR frame -- the rod's near end plus the end margin
        # plus the wave overhang -- and `--origin-offset` then re-bases the
        # whole program onto the datum the user zeroes on. Split that way
        # because they answer different questions: the print's place on the rod
        # is physical and the datum is bookkeeping, and the player has to undo
        # exactly the second one (`gui_run` freezes it for that). No Y and no Z
        # in the offset: see `_origin_offset`, the radial datum rides
        # `--mandrel-surface-z` instead.
        d_lay = self._mandrel_resolved_design()
        lay = self._mandrel_axial_layout(d_lay)
        ox, oy, oz = self._mandrel_origin_offset()
        # `--progress` first, exactly as the planar builder places it: a display
        # flag that makes the worker report how far through its work it is, so
        # the shared bottom bar runs 0-100 % on a tube too. Changes no output.
        args = self._export_prefix("worker:scaffold", "scaffold") + \
            ["--progress"] + head + [
            "--tube-speed-mode", self.m_speed_mode.get(),
            "-d", repr(self._mandrel_fiber_diameter()),
            "--layers", self.scaf_vars[0]["layers"].get().strip(),
            "--speed", self.scaf_vars[0]["speed"].get().strip(),
            "--start-x", repr(lay.print_start_x),
            "--mandrel-surface-z", repr(self._mandrel_surface_z(d_lay)),
            "--origin-offset", repr(ox), repr(oy), repr(oz),
        ]
        # only when the Machine page's flavor splits the feed (Aerotech), so a
        # combined export's argv is the one it always was
        if self._mandrel_rotary_feed() == "split":
            args += ["--rotary-feed", "split",
                     "--feed-time-base", self._feed_time_unit()]
            fixed = self._mandrel_fixed_feed()
            if fixed is not None:
                args += ["--fixed-feed", repr(fixed[0]), repr(fixed[1])]
        # The adaptive corner grid, 4-axis ONLY -- which is why it is spelled here
        # and not in the shared `_sim_play_args`. `--fill-per-tau` spends its
        # samples uniformly in time inside each corner window; on a tube nearly
        # every commanded vertex is a corner and the lag is anisotropic, so that
        # one global step faceted the whole part rather than one crest. Same
        # tolerance the preview holds itself to (`mew_deposition.auto_corner_grid`),
        # so the STL and the preview beside it are sampled by the same rule.
        args += self._auto_corner_grid_args(self._mandrel_fiber_diameter())
        # THE TUBE TABLES (the G-code page): the layer blocks, the step out
        # between layers, the copies side by side. Each goes out only when it
        # is not the one-block default, so a page that never touches them
        # sends the argv it always did. The blocks travel INLINE, not as a
        # file, so the G-code page's regeneration signature (the argv) sees
        # an edit to them.
        entries = self._mandrel_tube_entries()
        if entries is not None:
            args += ["--tube-blocks", json.dumps(entries)]
        step = self._mandrel_layer_step()
        if step is not None:
            args += ["--tube-layer-step", repr(step)]
        if self._mandrel_turn_out() > 0.0:
            args += ["--tube-turn-out", repr(self._mandrel_turn_out())]
        if self._mandrel_copies() > 1:
            args += ["--tube-copies", str(self._mandrel_copies()),
                     "--tube-copy-gap", repr(self._mandrel_copy_gap())]
        # The end-of-print exit, the same two switches the planar builder sends.
        # A tube has no plate edge; `scaffold_gcode`'s mandrel branch routes the
        # margin to `gcode_exit.mandrel_exit_x`, which runs the nozzle off
        # whichever END the print finishes on, along the mandrel axis.
        # Placed above the eta block so `--tube-eta` stays the LAST word the
        # zigzag argv appends.
        if not self.s_exit_move.get():
            args.append("--no-exit-move")
        else:
            args += ["--exit-margin", self.s_exit_margin.get().strip() or "5"]
        if solved and self._mandrel_is_turned(d):
            # the end-turn closure: angle and length go out as built, and the
            # pivot count is what pins the design
            args += ["--tube-pivots", str(d.n_pivots)]
        elif solved:
            # ALWAYS sent when the page resolved it: it is what pins the design.
            args += ["--tube-eta", str(d.eta)]
        elif fam == "zigzag" and self.m_eta.get().strip():
            args += ["--tube-eta", self.m_eta.get().strip()]
        # The Export panel, spelled exactly as the planar export spells it. Every
        # flag here is one `scaffold_gcode`'s mandrel branch already forwards to
        # the converter (scaffold_gcode.py:2948-2966); the flags it deliberately
        # does NOT forward -- --plate, --arc-segments, the footprint and
        # linking-loop machinery -- have no control shown in 4-axis either.
        if self.s_make_stl.get():
            args += ["--stl", self.s_stl_out.get().strip(),
                     "--faces", self.s_faces.get().strip()]
            args += self._mesh_tol_args()
            args += self._sag_solver_args(self.s_sag_mode, self.s_sag_mult,
                                          with_reset=True)
            # The jet deposits while travelling on a rod exactly as it does on a
            # plate, so this is unconditional here for the same reason it is
            # unconditional in `_build_args_scaffold`: what the user chooses is
            # only whether the solved travel reaches the exported mesh. Consumed
            # by mew_export when physics deposition is on, else forwarded by
            # `scaffold_gcode`'s mandrel branch to the converter.
            args.append("--render-travel")
            if self.s_travel_in_stl.get():
                args.append("--travel-in-stl")
            # A preview Build quality writes the sag + .depo cache and skips the
            # mesh, exactly as it does for a plate: the tube is the expensive one.
            if self._gen_is_preview():
                args.append("--no-mesh")
        return args

    def _fiber_params_mandrel(self):
        """The player's frozen render params -- same shape as the planar side.

        Mesh detail rides along even with the STL step OFF (the on-demand
        --from-depo build reads its multiplier back out of these); faces and the
        sag flags do not, so the two sides cannot disagree about the cache key."""
        d = self._mandrel_resolved_design()
        # The PRINTED SPAN, not `d.length`: `length` is the cell tiling, a wavy
        # ring runs u = -A to length + A, and the player sizes its collector to
        # whatever this says (`gcode_player._mandrel_body`, `RotaryAxis`).
        # Sending `length` drew a rod 2A short -- a 13.5664 mm print on a
        # 12.5664 mm rod at the page's chevron defaults. Start AND length,
        # because the end margin insets the print, so a player handed only a
        # length centres it on the printed extent and the margin vanishes. Both
        # come from `_mandrel_axial_layout` shifted onto the g-code datum, the
        # frame the file the player reads is written in, so the Viewer's rod is
        # the designer's rod by construction.
        lay = self._mandrel_axial_layout(d)
        origin_u = self._mandrel_origin_u(d)
        args = ["--diameter", repr(self._mandrel_fiber_diameter()),
                "--mandrel", self.m_diameter.get(),
                "--mandrel-start", repr(lay.rod_u0 - origin_u),
                "--mandrel-length", repr(lay.rod_u1 - lay.rod_u0),
                "--mandrel-surface-z", repr(self._mandrel_surface_z(d)),
                "--standoff", self._mandrel_standoff()]
        # The Curve-quality tier, so the LIVE sim draws this tube's corners at the
        # same resolution the exported STL does (the live-adjust march re-lays
        # a tube in the rotary frame). These
        # are player-only render params (`run_fiber_params` never becomes converter
        # argv), and the two other readers of this list scan for their own flags
        # only -- see `gui_viewers._fiber_diam_faces` / `_fiber_mesh_tol`.
        args += self._auto_corner_grid_args(self._mandrel_fiber_diameter())
        args += self._mesh_tol_args()
        if self.s_make_stl.get():
            args += ["--faces", self.s_faces.get().strip()]
            args += self._sag_solver_args(self.s_sag_mode, self.s_sag_mult,
                                          with_reset=True)
        # `--render-travel` is a .depo CACHE KEY, not a mode the player
        # implements, and the run above always asks for it -- so the player has
        # to expect it or the locked sim rejects the very cache the tube run
        # just wrote and silently rebuilds from the commanded path. Outside the
        # `s_make_stl` guard for the same reason `--mesh-tolerance` is: a
        # preview Build still writes a `.depo`, and that cache carries travel
        # geometry whether or not an STL was asked for. `--travel-gcode-line`
        # rides along (see `_render_travel_arg`).
        # THE DESIGNER'S COLOUR, so the player draws the part the colour the
        # preview beside it drew. One part means no `--scaffold-groups` list
        # here (that is the plate's per-scaffold seam), and without a group to
        # carry a colour every fiber fell back to the player's solid
        # `SCAFFOLD_COLOR` -- the off-white that made a blue tube play white.
        # `scaf_vars[0]["color"]` is the same var the preview restyles from
        # (`gui_mandrel._mandrel_on_color`), so the two cannot disagree. Blank -> flag omitted -> the
        # player keeps its own default, exactly as before.
        _col = self.scaf_vars[0]["color"].get().strip()
        if _col:
            args += ["--fiber-color", _col]
        args += self._render_travel_arg()
        return args

    def _calibration_args(self):
        """Fold the optional placement radii into the rig profile for a tube run.

        These three keys are excluded from the Physics page's field registry on
        purpose (``mew_calibration.py:314``) -- they describe a cylindrical
        collector -- so this is still the only writer for them."""
        args = super()._calibration_args()
        # a tube run: the designer's, or the Calibration page's lag tube
        if not (getattr(self, "_mandrel_dispatch", False)
                or getattr(self, "_sw_tube_dispatch", False)):
            return args
        path = args[1] if len(args) == 2 and args[0] == "--calibration" else None
        if path is None:
            path = os.path.join(self.session_dir, "calibration.json")
            profile = {}
        else:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    profile = json.load(fh)
            except (OSError, ValueError):
                profile = {}
        for key, val in self._mandrel_cal_extras().items():
            profile["cal." + key] = repr(val)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(profile, fh, indent=2)
        except OSError:
            return args
        return ["--calibration", path]

    def run(self):
        if self._active_mode() != "mandrel":
            return super().run()
        err = self._validate_mandrel()
        if err:
            self._refuse("Not generated: " + err)
            return
        self._mandrel_dispatch = True
        # The inversion has to cover BOTH ways `_active_mode` can answer. On the
        # Scaffold page it reads the collector toggle and `_mandrel_dispatch`
        # flips it; from any NON-INPUT page (Home -- which is where the app
        # boots -- Physics, the Viewer, History) it echoes `_last_input_mode`
        # instead, which `_apply_collector_mode` has already set to "mandrel",
        # and the flag never gets a look in. Generate then fell through
        # `_RunMixin.run`'s three-way switch to the g-code branch and the user
        # was asked to choose an input g-code file for a tube. Swap the
        # remembered mode for the same window as the flag.
        remembered = getattr(self, "_last_input_mode", None)
        if remembered == "mandrel":
            self._last_input_mode = "scaffold"
        try:
            return super().run()
        finally:
            self._mandrel_dispatch = False
            if remembered == "mandrel":
                self._last_input_mode = remembered

    def _build_args_scaffold(self):
        if getattr(self, "_mandrel_dispatch", False):
            return self._build_args_mandrel()
        return super()._build_args_scaffold()

    def _fiber_params_scaffold(self):
        if getattr(self, "_mandrel_dispatch", False):
            return self._fiber_params_mandrel()
        return super()._fiber_params_scaffold()
