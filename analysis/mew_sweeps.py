#!/usr/bin/env python3
"""mew_sweeps.py -- the calibration PRINT engine, headless.

Generates the rig-calibration experiment prints the paper repo's data-folder scripts
(`cts_sweep_generator.py`, `lag_square_generator.py`) produced by hand, as an
importable engine the Calibration page (gui_sweeps.py) drives live:

  * `generate_cts` -- the straight-line CTS sweep, one column of parallel test lines
    per slide, feed stepping DOWN, boustrophedon so connectors never cross the
    measurement field. Protocol: Hrynevich, Bas & Dalton, *Small* 2018 (CTS = the
    LOWEST speed at which ALL replicates are straight). One column PER SLIDE, because
    a cross-column travel would lay a stray fibre over the test lines.
  * `generate_diameter` -- the melt TEMPERATURE LADDER, which is one print design
    serving TWO calibrated quantities. Each rung of the ladder is its own slide of
    parallel straight lines at ONE fixed feed; the operator sets the nozzle to that
    rung's temperature, prints the slide, and measures the fibre diameter. The whole
    ladder fits `activation_energy` (ln d vs 1/T), and its HOTTEST rung is on its own
    the `viscosity` anchor -- the runbook is explicit that the anchor is not a
    separate print but the reference-temperature rung of this one (the
    calibration runbook written with the accompanying paper).
    NO `M104`/`M109` is emitted: the realised trial reprinted ONE file per rung with
    the operator changing temperature between prints, and that file carries no
    temperature word at all (`data/Trials/Fiber diameter 1/master gcode all temps
    F700.gcode` -- grep finds no M104/M109 in its 398 lines).
  * `generate_lag` -- the open-square jet-lag specimens. NOT REACHABLE FROM THE UI,
    though the engine path and its guards are untouched.

Every print file begins with the program's own serpentine STABILIZATION BASE
(`scaffold_gcode.emit_scaffold_layers`, the tested public hook) and returns a
`SweepResult`. The engine NEVER touches disk except inside `extract_moves`. Every mode
auto-splits onto more slides and flags oversize (never-erase) rather than dropping a
specimen.

BYTE-IDENTITY CONTRACT, and its TWO deliberate exceptions. This module is a
byte-identical port of the paper repo's generators; every knob added since is
byte-identical at its default, and anything else that differs is a defect. Both
departures are shared by all three generators, because all three open with the base:

  * the `M30` footer is NOT emitted. Klipper wires M28/M29/M30 to an error handler, so
    it raises at the end of every print and the job reports `cancelled` instead of
    `complete`.
  * the base pad's FIRST TRAVEL carries the print feed: the `F` word
    rides the opening `G0` instead of the first `G1`, so that travel runs at the print
    speed rather than at whatever feed the machine last held (F is modal, and the
    lead-in carried none). The paper's copies put it on the first `G1`.

THE TWO EXPORT OPTIONS (`g2g3`, `exit_margin` on the four plate prints) are knobs of
that kind: off by default and byte-identical there. `g2g3` writes the base pad's turnarounds and each
circle's lap as true arcs and puts the density their consumers must re-expand them
at in `SweepResult.arc_segments`; `exit_margin` appends the end-of-print exit
(`_with_exit`) after every bbox and CSV row is computed, so it moves neither.

HARD RULE honoured here: NO `;` comments ever go in the exported g-code -- all metadata
lives in the CSV / SweepResult. Never imports Tk/VTK, so it can be tested headless.
"""
from __future__ import annotations

import csv
import dataclasses
import io
import math
import os
import re
import tempfile

import scaffold_gcode
import array_travel
import gcode_exit
import mew_circle_fit

# Hrynevich Eq.4 at the 3.5 mm reference gap: lag = A*(1 - eps^-B) mm, scaled
# linearly to other gaps (lag ~ H). Sizing/annotation ONLY -- never geometry
# the fit depends on.
_A, _B, _REF_GAP = 10.02, 0.304, 3.5
# mew_physics cts_ref = 7.5 mm/s upper bound, as mm/min (the CSV eps_ref
# annotation default for the CTS sweep).
CTS_REF_MM_MIN = 450.0
DEFAULT_EPS = [1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 6.0]     # the fit grid
DIAG_EPS = [1.125, 5.0, 9.0, 17.0]     # fit-EXCLUDED diagnostic tail
# the circle ladder. Bottom rung 2 because below it the believed CTS's
# error dominates k (5% of CTS -> 11.6% of k at 1.5, 6.5% at 2); top rung 6
# because R grows with the lag and an eps-8 circle costs a whole plate.
DEFAULT_CIRCLE_EPS = [2.0, 3.0, 4.0, 6.0]

#: segments per half-turn the converter re-expands an arc at when nothing finer
#: is asked for -- `gcode_to_fibers._DEFAULT_ARC_SEGMENTS`, and the pad's own
#: chord count, so a file with no circles parses as it always has.
DEFAULT_ARC_SEGMENTS = 8

# jet-lag square LAYOUTS + emit ORDERs (the operator's placement controls).
# "shelf"+"size" is the plain auto-fit.
LAYOUTS = ("shelf", "columns", "rows", "grid")
ORDERS = ("size", "slowest", "fastest", "position")


def lag_mm(eps, gap):
    """Hrynevich Eq.4 lag estimate at `gap` (mm), for specimen sizing."""
    return (gap / _REF_GAP) * _A * (1.0 - eps ** (-_B))


def _fmt(v):
    return f"{v:.4f}".rstrip("0").rstrip(".")


def parse_float_list(text):
    """Comma/space/semicolon separated numbers -> [float]; bad tokens skipped.
    "nan" and "inf" are bad tokens too: `float` parses both, and an inf span or
    ratio went on to raise OverflowError deep inside a generator."""
    if isinstance(text, (list, tuple)):
        toks = text
    else:
        toks = re.split(r"[,;\s]+", str(text).strip())
    vals = []
    for t in toks:
        if not t:
            continue
        try:
            val = float(t)
        except (TypeError, ValueError):
            continue
        if math.isfinite(val):
            vals.append(val)
    return vals


def parse_reps_overrides(text):
    """'6:4 2.5:3' -> {6.0: 4, 2.5: 3}. Raises ValueError on a malformed pair
    (a half-typed override should surface, not silently drop)."""
    out = {}
    for tok in re.split(r"[,;\s]+", str(text or "").strip()):
        if not tok:
            continue
        try:
            e_str, n_str = tok.split(":")
            out[round(float(e_str), 4)] = int(n_str)
        except ValueError:
            raise ValueError(f"per-eps override {tok!r} must be EPS:N, e.g. 6:4")
    return out


# --------------------------------------------------------------------------- #
# parameter records (defaults = the data-folder scripts' CLI defaults)
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class StabParams:
    """The serpentine stabilization base (the program's own defaults)."""
    enabled: bool = True
    width: float = 10.0
    height: float = 10.0
    layers: int = 5
    stepover: float = 1.7
    arc_mult: float = 1.5          # arc radius = arc_mult * stepover


@dataclasses.dataclass
class CtsParams:
    start: float = 600.0           # fastest feed, mm/min (above expected CTS)
    stop: float = 240.0            # slowest feed, mm/min (below expected CTS)
    step: float = 60.0             # decrement, mm/min (60 coarse / 10 fine)
    reps: int = 3                  # replicate lines per speed
    line_len: float | None = None  # mm; None = auto-fill to the far edge
    pitch: float = 1.5             # vertical spacing between lines, mm
    diam: float = 0.015            # fibre diameter, mm (= base layer height)
    col_gap: float = 5.0           # base -> line-column gap, mm
    cts_ref: float = CTS_REF_MM_MIN  # eps_ref annotation only
    slide_w: float = 76.2
    slide_h: float = 50.8
    margin: float = 4.0
    origin_x: float = 0.0          # plate lower-left on the collector
    origin_y: float = 0.0
    bottom_up: bool = False        # first line at the bottom, ascend
    slowest_first: bool = True     # print order: slowest speed first; False = fastest
    base_speed: float | None = None  # mm/min; None = start feed
    max_feed: float | None = None  # machine max translation feed, mm/min (warn)
    # LINKING LOOPS: the hop from one line's end to the next line's start
    # becomes the Scaffold designer's turnaround loop (`_line_move`), sized and
    # fed as the designer's "Turnaround radius" and "Loop speed". 0 is the
    # straight G0 hop this print always made, byte-identical.
    loop_arc: float = 0.0          # loop radius, x the distance to the next line
    loop_segments: int = 8         # G1 chords per half-turn of a loop
    loop_speed: float | None = None  # mm/min; None = the feed of the line before
    # manual placement offsets (mm), set by no UI; a script may pass them. They shift the whole
    # line-block and the stabilization base off their auto positions INDEPENDENTLY
    # of each other. (0,0) each -> byte-identical to the auto layout. Never-erase:
    # a block pushed off the slide is flagged (on_slide), not silently clamped.
    lines_offset: tuple = (0.0, 0.0)  # (dx, dy) added to every test line
    base_offset: tuple = (0.0, 0.0)   # (dx, dy) added to the stabilization base
    # EXPORT options, the Scaffold designer's two that change the FILE. Both off
    # by default, which is byte-identical: `g2g3` writes the base pad's turnaround
    # loops (and a circle's lap) as true arcs; `exit_margin` (mm) appends the
    # end-of-print exit off the slide (`_with_exit`).
    g2g3: bool = False
    exit_margin: float | None = None
    stab: StabParams = dataclasses.field(default_factory=StabParams)


@dataclasses.dataclass
class DiamParams:
    """The melt temperature ladder -- one slide per nozzle temperature.

    Geometry is the CTS sweep's, with the roles of the two axes swapped: there the
    FEED varies down a single slide, here the feed is fixed and the TEMPERATURE varies
    from slide to slide, because a rung is a thing the operator sets on the machine
    between prints rather than a thing g-code can say (no M104/M109 -- see the module
    docstring). Every rung's slide is therefore geometrically identical, which is
    exactly what the realised trial printed: five plates off one file.

    Defaults reproduce that trial. `temps` is the runbook's descending ladder
    (100 / 92.5 / 85 / 77.5 / 70 degC, descending so the
    100 degC anchor is measured on the freshest melt); `speed` is 700 mm/min, the feed
    the trial actually ran (parameters.txt and results.txt both say 700 mm/min, and
    Analysis/REPORT.md:138 fits at v = 11.667 mm/s = 700 mm/min -- the F500 in the
    per-plate copies is a superseded draft); `reps` is 5, the five parallel lines the
    trial laid per plate. The runbook itself asks only for one line >=20 mm read at
    n >= 3 stations (:444), so reps is replication above that floor, not the floor.
    """
    temps: list = dataclasses.field(
        default_factory=lambda: [100.0, 92.5, 85.0, 77.5, 70.0])
    speed: float = 700.0           # the ONE fixed feed, mm/min, held for every rung
    reps: int = 5                  # parallel lines per rung (the trial printed 5)
    line_len: float | None = None  # mm; None = auto-fill to the far edge
    pitch: float = 1.5             # vertical spacing between lines, mm
    diam: float = 0.015            # fibre diameter, mm (= base layer height)
    col_gap: float = 5.0           # base -> line-column gap, mm
    slide_w: float = 76.2
    slide_h: float = 50.8
    margin: float = 4.0
    origin_x: float = 0.0
    origin_y: float = 0.0
    bottom_up: bool = False        # first line at the bottom, ascend
    base_speed: float | None = None  # mm/min; None = the print feed
    max_feed: float | None = None  # machine max translation feed, mm/min (warn)
    pressure_psi: float = 0.0      # annotation only (CSV); 0 = not recorded
    # EXPORT options, the Scaffold designer's two that change the FILE. Both off
    # by default, which is byte-identical: `g2g3` writes the base pad's turnaround
    # loops (and a circle's lap) as true arcs; `exit_margin` (mm) appends the
    # end-of-print exit off the slide (`_with_exit`).
    g2g3: bool = False
    exit_margin: float | None = None
    stab: StabParams = dataclasses.field(default_factory=StabParams)


@dataclasses.dataclass
class SagParams:
    """The sag ladder -- ONE slide: a row of parallel walls with the gap between
    them stepping wider, crossed by two bridging fibres that span those gaps.

    The walls run in Y and the gaps step along X; the two bridging fibres run in X
    over the whole row, one layer each, and droop into every gap at once. Measuring
    a bridging fibre's midspan droop at each gap gives droop against span from a
    single print, which is what fitting BOTH the span exponent and the magnitude
    coefficient needs -- a single scaffold pore gives one point and can only fit the
    magnitude.

    `spans` is the commanded wall CENTRELINE PITCH, not the clear gap: the clear gap
    is `span - diam`. `mirror` lays the list forwards and then backwards, so every
    span is printed twice on one slide, once approached from the narrow side and once
    from the wide side. With the default seven spans that is 14 gaps and 15 walls.

    Z convention is the scaffold's: wall layer k sits at z = k*diam for k in
    0..wall_layers-1, so the top wall layer is at (wall_layers-1)*diam and the
    bridging fibres land one layer above it, at wall_layers*diam.

    THE TRAVEL LANE IS THE WHOLE POINT, because on a continuous-flow rig a travel
    move deposits fibre exactly like a print move: every gap-crossing hop between
    two walls leaves a strand suspended in that gap, and a strand in front of a
    bridging fibre is a strand you have to look THROUGH to measure it side-on. So
    every hop is confined to one Y band -- the lane -- and `travel_lane` says where
    that band sits:

      * "middle" (default): the lane runs down the wall's midline. Each X carries
        two half-walls of `wall_len`, one either side of the lane, and a bridging
        fibre sits `fibre_inset` inboard of each outer extreme. BOTH fibres are then
        a full `wall_len` clear of every strand, so both can be imaged -- the second
        one by turning the plate around. Costs twice the wall length in Y and time.
      * "edge": the lane runs along one end of the walls and there is ONE bridging
        fibre, `fibre_inset` inboard of the far end. Half the wall time; half the
        readings.

    Keeping the hops in the lane needs the head to come BACK to the lane before it
    crosses a gap, so a wall is visited in even strokes: `layers_per_visit` passes go
    on (out, back, out, back...) before the hop. They are laid at ONE commanded Z --
    see the emission comment for why the converter allows no other shape -- so the
    wall gains that many fibres while the nozzle stands still and the standoff ends
    the visit short by (`layers_per_visit` - 1) diameters. KEEP IT SMALL: 2 costs one
    diameter against a 3.5 mm standoff. It must be even and must divide
    `wall_layers`, which counts passes per wall, not commanded Z steps.

    Defaults are the sag calibration procedure drafted for the accompanying
    paper: spans 0.25..2.0 mm mirrored, 3.0 mm walls, 20 layers, 5 mm of
    lead-in and lead-out. The inset is 0.75 mm rather than that section's 0.5: jet
    lag pulls a wall's turnaround short of its commanded end, so a fibre laid at the
    commanded inset lands over wall that is not there. THE PROCEDURE IS
    TENTATIVE -- it has not been printed yet, and these numbers are what the geometry
    is to be judged on, not a calibrated design. The lane and the visit stroke are
    NOT in that section: it specifies a serpentine, which put ~145 strands at each
    wall end and made the side-on view it also specifies impossible.

    `sag_coeff` / `sag_span_exp` are the live droop law (mew_physics Calibration c
    and p), carried here for ONE purpose: deciding which spans will bottom out on
    the collector before they are printed, which is `c * span**p` against the wall
    height and not the span itself. The GUI passes the rig's calibrated pair; the
    defaults mirror mew_physics' shipped values.
    """
    spans: list = dataclasses.field(
        default_factory=lambda: [0.25, 0.35, 0.5, 0.7, 1.0, 1.4, 2.0])
    mirror: bool = True            # lay the list forwards then backwards
    wall_len: float = 3.0          # mm, wall length in Y per bridging fibre
    wall_layers: int = 20          # stacked layers per wall
    travel_lane: str = "middle"    # "middle" (2 fibres) | "edge" (1 fibre)
    layers_per_visit: int = 2      # even, divides wall_layers; layers per wall visit
    fibre_inset: float = 0.75      # mm, each bridging fibre inboard of a wall end
    lead: float = 5.0              # mm of bridging fibre either side of the row
    speed: float = 700.0           # print feed, mm/min
    sag_coeff: float = 0.341588    # mm^(1-p), the droop law's c -- warnings only
    sag_span_exp: float = 2.0      # the droop law's p -- warnings only
    diam: float = 0.015            # fibre diameter, mm (= layer height, wall width)
    col_gap: float = 5.0           # base -> ladder gap, mm
    slide_w: float = 76.2
    slide_h: float = 50.8
    margin: float = 4.0
    origin_x: float = 0.0
    origin_y: float = 0.0
    base_speed: float | None = None  # mm/min; None = the print feed
    max_feed: float | None = None  # machine max translation feed, mm/min (warn)
    # EXPORT options, the Scaffold designer's two that change the FILE. Both off
    # by default, which is byte-identical: `g2g3` writes the base pad's turnaround
    # loops (and a circle's lap) as true arcs; `exit_margin` (mm) appends the
    # end-of-print exit off the slide (`_with_exit`).
    g2g3: bool = False
    exit_margin: float | None = None
    stab: StabParams = dataclasses.field(default_factory=StabParams)


@dataclasses.dataclass
class LagParams:
    cts: float = 6.0               # measured CTS, mm/s
    gap: float = 3.5               # gap H, mm
    eps: list = dataclasses.field(default_factory=lambda: list(DEFAULT_EPS))
    reps: int = 2                  # squares per eps
    reps_eps: dict = dataclasses.field(default_factory=dict)  # per-eps override
    safety: float = 4.0            # side = safety * lag (auto-size mode)
    floor: float = 8.0             # minimum side, mm (auto-size mode)
    # gap-INDEPENDENT sizing (defaults reproduce the auto-size byte-for-byte):
    # fixed_side > 0 switches sizing to side = fixed_side + side_per_eps*eps, used
    # verbatim, so a plate of squares keeps a chosen size regardless of gap (gap
    # then only drives the physics lag/rounding). side_per_eps (mm per unit eps)
    # optionally enlarges the faster ratios that lag more. Set fixed_side to match
    # a printed plate; leave it 0 for the lag-derived auto sizing.
    fixed_side: float = 0.0
    side_per_eps: float = 0.0
    diam: float = 0.015            # fibre diameter, mm (= layer height)
    spacing: float = 3.0           # gap between specimens, mm
    travel_offset: float = 1.0     # routing clearance, mm (0 = straight hops)
    layers: int = 1                # stacked loops per square
    speed_order: bool = False      # slowest->fastest fail-safe ordering
    # placement controls (defaults reproduce the original auto-fit byte-for-byte):
    layout: str = "shelf"          # "shelf" | "columns" | "rows" | "grid"
    order: str = "size"            # "size" | "slowest" | "fastest" | "position"
    grid_cols: int = 0             # grid columns; 0 = auto (near-square)
    # manual placement: label -> (slide_idx0, x0_abs, y0_abs). A listed specimen is
    # pinned there and the rest still auto-lay. An orphan label is ignored, so an
    # override survives a parameter change. {} = pure auto (byte-identical).
    placements: dict = dataclasses.field(default_factory=dict)
    # manual per-slide stabilization-base placement offsets (mm), set by no UI
    # (a script may pass them): EACH slide's base moves independently of every other
    # slide's base and of the squares. {slide_idx0: (dx, dy)}; a slide with no
    # entry keeps its auto local corner (margin, margin). {} = byte-identical to
    # today's layout (every base at the margin).
    base_offsets: dict = dataclasses.field(default_factory=dict)
    stab_speed_eps: float = 1.5    # base speed as multiple of CTS
    max_feed: float | None = None  # machine max translation feed, mm/min (warn)
    slide_w: float = 76.2
    slide_h: float = 50.8
    margin: float = 4.0
    origin_x: float = 0.0
    origin_y: float = 0.0
    stab: StabParams = dataclasses.field(default_factory=StabParams)


@dataclasses.dataclass
class CircleSpec:
    """ONE row of the circle table: a speed ratio, how many circles to lay at it, and
    optionally the radius to command instead of the law's.

    `radius_mm is None` means SIZE ME: R = lag(eps, gap, amp_prior) / lr_target, floored
    by `CircleParams.floor` -- the rule the whole amplitude error budget is written against.
    A number is taken VERBATIM,
    clamped up to 0.1 mm exactly as `fixed_radius` is, and participates in nesting,
    packing and the CSV the same way. It also moves that ring off the design dial, so
    it is an override rather than a preference, and the flatness check's floor no
    longer follows `lr_target` for that row.

    `circle_table(p)` returns these with `radius_mm` FILLED IN -- what the generator
    will actually lay. `specs_to_str` / `specs_from_str` are the one-string form.
    """
    eps: float                      # speed ratio v/CTS, > 1
    reps: int = 2                   # circles at this ratio
    radius_mm: float | None = None  # None = size from the law


@dataclasses.dataclass
class CircleParams:
    """The circle set that calibrates the FLAT lag amplitude `A`.

    One circle per (speed ratio, rep). The lag turns into a measurable
    cross-track INSET on a curve, so `A` follows from the commanded radius and
    the deposited one -- see `mew_circle_fit`, which holds both the closed form
    and its inversion, and which this dataclass sizes against so the two cannot
    carry different laws.

    As with the lag squares, CTS and the gap are NOT fields on the sweeps page:
    each is owned by its canonical tab.
    """
    cts: float = 6.0               # measured CTS, mm/s
    gap: float = 3.5               # gap H, mm -- A is CONDITIONAL on this
    eps: list = dataclasses.field(
        default_factory=lambda: list(DEFAULT_CIRCLE_EPS))
    reps: int = 2                  # circles per ratio
    reps_eps: dict = dataclasses.field(default_factory=dict)  # per-eps override
    # THE TABLE. None (the default) keeps the eps/reps/reps_eps path above, byte for
    # byte. A list of `CircleSpec` is instead the WHOLE truth: its rows carry the
    # ratios, the rep counts and any hand-set radii, and `eps`, `reps`, `reps_eps` and
    # `fixed_radius` are all IGNORED. One row per ratio -- a repeated ratio raises.
    specimens: list | None = None   # [CircleSpec] | None
    # SIZING. R = lag(eps, gap, amp_prior) / lr_target. lr_target is the design dial and
    # every error term is a function of it alone (R scales with the gap because the lag
    # does, so the dimensionless problem is gap-invariant). 0.35 is the smallest ratio
    # at which two reps of {2,3,4,6} still fit two 76.2 x 50.8 mm plates; it buys a
    # 3.6% ray-versus-filter ambiguity in the recovered amplitude.
    lr_target: float = mew_circle_fit.DEFAULT_LR
    # The amplitude the SIZING assumes, not a result. It is Eq.4's A on the same
    # scale as the fit's answer -- it was a multiplier `k_prior` defaulting to 1
    # while the amplitude lived elsewhere, and the two are now one number.
    amp_prior: float = mew_circle_fit.A_DEFAULT
    floor: float = 0.0             # minimum radius, mm (0 = none)
    fixed_radius: float = 0.0      # > 0 -> every circle this radius, verbatim
                                   # (reproduce a printed plate; 0 keeps the
                                   # lag-derived sizing)
    # Arc sampling sagitta, mm. Not cosmetic: the lap is emitted as a POLYLINE, so a
    # coarse one is a polygon whose mean radius sits BELOW the commanded circle, and
    # the fit -- which inverts an ideal circle -- reads that as amplitude. Measured end
    # to end through parse_gcode + mew_deposition with a planted k = 1: 0.02 mm returns
    # k = 1.015-1.037, 0.005 returns 1.006-1.015, 0.001 returns 1.002-1.007, for 752
    # g-code lines against 474. 1 um is also safely under a fibre radius.
    chord_tol: float = 0.001
    # "nested" | "chain". NESTED puts the whole ladder on one centre as
    # concentric rings, one stack per replicate: 2-3x the plate density,
    # because the radii already differ per ratio. What that buys is the
    # eps-flatness check -- its floor is the placement-rule ambiguity, which
    # falls with lr_target, and nesting is what makes a small lr_target
    # affordable (2 plates at 0.25, where the chain needs 6). CHAIN is the
    # original row of separate circles on a shared tangent baseline; it is what
    # a single-ratio design falls back to, and nesting degrades to it
    # automatically when there is only one ring to lay.
    layout: str = "nested"
    exclude_deg: float = mew_circle_fit.EXCLUDE_DEG   # CSV annotation: the
                                   # entry sector the radius fit must skip
    # The straight tangential run-up, as a multiple of the lag (floored
    # in mm). It buys almost nothing and costs plate: with the entry
    # sector excluded from the fit, 0.5xL already leaves under 5 um of
    # bias and 1.0xL under 3 um (measured). NESTED it is expensive --
    # the outermost ring's run-up is the only thing reaching outside the
    # stack, so it sets the footprint: 2.0 costs a whole slide at
    # lr_target 0.45, 1.0 does not.
    lead_mult: float = 1.0
    lead_min: float = 2.0          # ... but never shorter than this, mm
    diam: float = 0.015            # fibre diameter, mm (= layer height)
    spacing: float = 3.0           # gap between specimens, mm
    travel_offset: float = 1.0     # routing clearance, mm (0 = straight hops)
    layers: int = 1                # stacked laps per circle
    stab_speed_eps: float = 1.5    # base speed as multiple of CTS
    max_feed: float | None = None  # machine max translation feed, mm/min (warn)
    slide_w: float = 76.2
    slide_h: float = 50.8
    margin: float = 4.0
    origin_x: float = 0.0
    origin_y: float = 0.0
    # EXPORT options, the Scaffold designer's two that change the FILE. Both off
    # by default, which is byte-identical: `g2g3` writes the base pad's turnaround
    # loops (and a circle's lap) as true arcs; `exit_margin` (mm) appends the
    # end-of-print exit off the slide (`_with_exit`).
    g2g3: bool = False
    exit_margin: float | None = None
    stab: StabParams = dataclasses.field(default_factory=StabParams)


@dataclasses.dataclass
class Specimen:
    """One printed specimen, for the preview's floating label + tooltips. All
    coords collector-absolute mm."""
    label: str                     # the CSV label (unique per specimen)
    value_text: str                # short badge shown in the preview ("ε 6.0")
    center: tuple                   # (x, y) label anchor
    size: float                    # square side / line length (mm)
    feed_mm_min: float             # commanded feed (for the feed-ceiling check)


@dataclasses.dataclass
class PrintMeta:
    """One print file's layout summary (all coords collector-absolute)."""
    index: int                     # 1-based print number
    n_items: int                   # test lines / squares in this file
    labels: list                   # per-item labels, emit order
    bbox: tuple                    # (xmin, ymin, xmax, ymax) of ALL moves
    on_slide: bool                 # bbox within the plate rectangle
    specimens: list = dataclasses.field(default_factory=list)  # [Specimen]
    base_bbox: tuple = None        # (xmin,ymin,xmax,ymax) absolute, this slide's
                                    # stabilization base (offset already applied);
                                    # None if there is no base on this slide


@dataclasses.dataclass
class SweepResult:
    kind: str                      # "cts" | "lag" | "circle" | "diam"
    gcode_texts: list              # one string per print file / slide
    csv_text: str                  # the measurement template
    rows: list                     # per-specimen dicts (the CSV's source)
    prints: list                   # [PrintMeta]
    summary: str                   # one-paragraph human summary
    warnings: list                 # user-facing strings (e.g. OFF SLIDE)
    # The never-erase contract: a specimen too large for the usable plate is NOT
    # silently dropped and does NOT raise -- it is recorded here and the rest of
    # the sweep is still laid out, so the GUI shows what fits plus an explicit
    # "won't fit" note instead of reverting to stale geometry. None = all placed.
    oversize: list = dataclasses.field(default_factory=list)   # [str] messages
    # ADVISORIES are not warnings: nothing is wrong with the print, the design
    # merely will not answer the question as well as it could -- a span the droop
    # LAW (whose c is the very thing being measured) says will flatten, or a span
    # range too narrow to carry an exponent. They ride their own channel because
    # the page must not put them in the red strip under whichever card is open:
    # the Calibration settings card already states the measurable band in its own
    # conditions line, and a second copy in red is the same sentence twice.
    advisories: list = dataclasses.field(default_factory=list)  # [str] messages
    # Segments per half-turn every consumer must re-expand this result's G2/G3
    # arcs at (`--arc-segments` to the converter and the player, `arc_segments=`
    # to `extract_moves`). The default is the converter's own; an arc-mode
    # circle plate asks for more (`circle_arc_segments`).
    arc_segments: int = DEFAULT_ARC_SEGMENTS

    @property
    def all_on_slide(self):
        return all(p.on_slide for p in self.prints)

    @property
    def n_slides(self):
        return len(self.gcode_texts)


# --------------------------------------------------------------------------- #
# shared emit helpers
# --------------------------------------------------------------------------- #
def _emit_base(lines, bx, by, stab, diam, speed_mmmin, g2g3=False):
    """Append the serpentine stabilization base via the tested public hook.

    `current_f` goes in as None, which since 787b2c8 is what tells
    `emit_scaffold_layers` to seed the modal feed onto the pad's opening `G0`
    rather than the first `G1` -- the second of the two deliberate departures
    from the paper repo's generators (see the module docstring).

    `g2g3` writes the pad's turnaround loops as true arcs, the Scaffold
    designer's "Write curves as G2/G3 arcs". The pad's footprint is the same
    either way (`_measure_base_bbox` measures the chord form on purpose)."""
    scaffold_gcode.emit_scaffold_layers(
        lines, True, None, _fmt,
        width=stab.width, height=stab.height, stepover=stab.stepover,
        fiber_diameter=diam, layers=stab.layers, speed=speed_mmmin,
        arc_radius=stab.arc_mult * stab.stepover,
        start_x=bx, start_y=by, start_z=0.0, g2g3=g2g3)
    return speed_mmmin             # the base leaves F at the base speed


_WORD = re.compile(r"([XYIJ])(-?[\d.]+)")


def _arc_extent(sx, sy, ex, ey, i, j, cw):
    """The points that bound one G2/G3 arc: its two ends plus every axis
    extreme (0, 90, 180, 270 deg about the centre) its sweep passes. The sweep
    rule is `gcode_to_fibers.arc_xy_points`'s, so a start == end arc is the
    full circle there and here."""
    cx, cy = sx + i, sy + j
    r = math.hypot(i, j)
    pts = [(sx, sy), (ex, ey)]
    if r < 1e-12:
        return pts
    a0 = math.atan2(sy - cy, sx - cx)
    sweep = math.atan2(ey - cy, ex - cx) - a0
    if cw:
        while sweep >= 0.0:
            sweep -= 2.0 * math.pi
    else:
        while sweep <= 0.0:
            sweep += 2.0 * math.pi
    for k in range(4):
        ang = k * 0.5 * math.pi
        d = (ang - a0) % (2.0 * math.pi) if not cw else \
            (a0 - ang) % (2.0 * math.pi)
        if d <= abs(sweep) + 1e-12:
            pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


def _motion_points(lines):
    """[(cmd, [(x, y), ...])] per motion line: the XY points that bound the
    move, an arc's bulge included. Modal: an axis the line does not name keeps
    its last value. Lines with no X/Y word contribute no points."""
    out = []
    x = y = None
    for l in lines if isinstance(lines, list) else lines.splitlines():
        words = dict((a, float(v)) for a, v in _WORD.findall(l))
        if "X" not in words and "Y" not in words:
            continue
        cmd = l.split(None, 1)[0].upper() if l.strip() else ""
        nx = words.get("X", x)
        ny = words.get("Y", y)
        if (cmd in ("G2", "G3", "G02", "G03") and x is not None
                and y is not None and nx is not None and ny is not None):
            pts = _arc_extent(x, y, nx, ny, words.get("I", 0.0),
                              words.get("J", 0.0), cmd in ("G2", "G02"))
        else:
            pts = [(nx if nx is not None else words.get("X"),
                    ny if ny is not None else words.get("Y"))]
        out.append((cmd, pts))
        x, y = nx, ny
    return out


def _xy_bbox(lines):
    """(xmin, ymin, xmax, ymax) over every X/Y word in the g-code lines, and over
    the bulge of every G2/G3 arc -- an arc's ends alone miss most of it."""
    xs, ys = [], []
    for _cmd, pts in _motion_points(lines):
        for px, py in pts:
            if px is not None:
                xs.append(px)
            if py is not None:
                ys.append(py)
    if not xs or not ys:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _measure_base_bbox(stab, diam, speed_mmmin):
    """The base's TRUE g-code extent at (0,0) -- turnaround balloons included.

    Always the CHORD form, also when the print writes arcs: the layout is then
    the same in both modes, and the arc reaches past its chords by one sagitta
    (about 2% of the loop radius at the pad's 8 segments per half-turn), which
    `col_gap` absorbs many times over."""
    tmp = []
    _emit_base(tmp, 0.0, 0.0, stab, diam, speed_mmmin)
    return _xy_bbox(tmp)


# --------------------------------------------------------------------------- #
# the two export options: G2/G3 arcs and the end-of-print exit
# --------------------------------------------------------------------------- #
#: clearance (mm) the exit route keeps from every deposited run, on the prints
#: with no `travel_offset` of their own -- the circles' default routing
#: clearance, so every plate print leaves the same way.
EXIT_CLEARANCE = 1.0


def circle_arc_segments(chord_tol, radii):
    """Segments per half-turn at which an arc-mode lap re-expands no coarser
    than `_circle_points` would have written it: the `chord_tol` sagitta at the
    LARGEST radius (the sagitta of a fixed angular step grows with R), and never
    under the polyline's floor of 24 points per lap. This is what the converter,
    the player and the preview must be told (`--arc-segments`), or they draw a
    16-gon and the fit reads its polygon as amplitude."""
    n = DEFAULT_ARC_SEGMENTS
    for R in radii:
        if R <= 0:
            continue
        tol = max(1e-6, min(float(chord_tol), 0.5 * R))
        dtheta = 2.0 * math.acos(1.0 - tol / R)
        n = max(n, 12, int(math.ceil(math.pi / dtheta)))
    return n


def _emit_lap(lines, cx, cy, R, start_angle):
    """One CCW lap from the tangent point at `start_angle` back to it, as FOUR
    G3 quarter arcs. The caller has already laid the straight G1 run to that
    point (it carries the feed), so the arcs need no F word.

    Quarters rather than one full-circle arc or two halves: a full circle's
    start == end is read differently by different controllers, and four
    endpoints at 90 deg keep every point of the lap within a quarter-turn of a
    point the file names."""
    x0 = cx + R * math.cos(start_angle)
    y0 = cy + R * math.sin(start_angle)
    for k in range(1, 5):
        th = start_angle + 0.5 * math.pi * k
        x1, y1 = cx + R * math.cos(th), cy + R * math.sin(th)
        if k == 4:
            x1, y1 = x0, y0
        sx, sy = _cur_xy(lines)
        lines.append(f"G3 X{_fmt(x1)} Y{_fmt(y1)} "
                     f"I{_fmt(cx - sx)} J{_fmt(cy - sy)}")


def _cur_xy(lines):
    """The head's (x, y) after `lines`, as the FILE names it (4 dp), so an
    arc's I/J is measured from where the controller really is."""
    x = y = None
    for l in reversed(lines):
        words = dict((a, float(v)) for a, v in _WORD.findall(l))
        if x is None and "X" in words:
            x = words["X"]
        if y is None and "Y" in words:
            y = words["Y"]
        if x is not None and y is not None:
            break
    return x, y


def _runs(text):
    """Bounding boxes (x0, y0, x1, y1) of each maximal run of deposition moves
    (G1/G2/G3), arcs at their real extent, and the head's last (x, y)."""
    boxes, run, last = [], None, None
    for cmd, pts in _motion_points(text):
        if cmd in ("G1", "G01", "G2", "G02", "G3", "G03"):
            if run is None:
                run = [last] if last is not None else []
            run.extend(pts)
        elif run is not None:
            boxes.append(run)
            run = None
        last = pts[-1] if cmd not in ("G2", "G3", "G02", "G03") else pts[1]
    if run is not None:
        boxes.append(run)
    out = []
    for r in boxes:
        r = [q for q in r if q is not None and None not in q]
        if r:
            out.append((min(q[0] for q in r), min(q[1] for q in r),
                        max(q[0] for q in r), max(q[1] for q in r)))
    return out, last


def _with_exit(text, p, margin, clearance):
    """`text` plus the end-of-print exit: the Scaffold designer's move
    (scaffold_gcode's end-of-print exit), with the slide as the plate and every
    deposited run as a footprint. Step clear of the run the head finished on,
    then route around every run to `gcode_exit.plate_exit_point`, `margin` mm
    past the slide edge the finish faces. Travel only (G0), Z modal.

    The polymer keeps flowing after the last move; left over the last
    specimen it lands ON the specimen. Only appends, so every bbox, on-slide
    verdict and CSV row is the print's own -- the callers compute them first."""
    boxes, finish = _runs(text)
    if not boxes or finish is None:
        return text
    part = (min(b[0] for b in boxes), max(b[2] for b in boxes),
            min(b[1] for b in boxes), max(b[3] for b in boxes))
    plate = (p.origin_x, p.origin_x + p.slide_w,
             p.origin_y, p.origin_y + p.slide_h)
    out = []
    start = finish
    eps = 1e-6
    for b in boxes:
        if (b[0] - eps <= finish[0] <= b[2] + eps
                and b[1] - eps <= finish[1] <= b[3] + eps):
            sx, sy, _edge = gcode_exit.outward_edge_exit(
                finish[0], finish[1], (b[0] + b[2]) / 2.0,
                (b[1] + b[3]) / 2.0, b[0], b[2], b[1], b[3], margin=clearance)
            out.append(f"G0 X{_fmt(sx)} Y{_fmt(sy)}")
            start = (sx, sy)
            break
    goal = gcode_exit.plate_exit_point(start[0], start[1], part, plate,
                                       bounds=None, margin=margin)
    for wx, wy in array_travel.plan_exit_travel(start, goal, boxes, clearance):
        out.append(f"G0 X{_fmt(wx)} Y{_fmt(wy)}")
    return text + "\n".join(out) + "\n"


def _finish_texts(texts, p):
    """The flat generators' last step: the end-of-print exit on every slide
    when `p.exit_margin` asks for one. None (the default) is byte-identical."""
    margin = getattr(p, "exit_margin", None)
    if margin is None:
        return texts
    clearance = getattr(p, "travel_offset", None) or EXIT_CLEARANCE
    return [_with_exit(t, p, float(margin), float(clearance)) for t in texts]


def _print_meta(idx, gcode_text, labels, p, specimens=None, base_bbox=None):
    """Absolute bbox + on-slide verdict for one finished print file."""
    bbox = _xy_bbox(gcode_text)
    ok = (bbox[0] >= p.origin_x - 1e-9 and bbox[1] >= p.origin_y - 1e-9
          and bbox[2] <= p.origin_x + p.slide_w + 1e-9
          and bbox[3] <= p.origin_y + p.slide_h + 1e-9)
    return PrintMeta(index=idx, n_items=len(labels), labels=labels,
                     bbox=bbox, on_slide=ok, specimens=specimens or [],
                     base_bbox=base_bbox)


def _feed_ceiling_warnings(specimens, max_feed):
    """One warning naming the specimens whose commanded feed exceeds the machine's
    max translation feed (the runbook's repeated feed-ceiling caveat -- e.g. eps=9
    or 17 need 9-17x CTS, which usually overruns the MEWron). Empty when max_feed
    is None/<=0 or nothing exceeds it."""
    if not max_feed or max_feed <= 0:
        return []
    over = [s for s in specimens if s.feed_mm_min > max_feed + 1e-6]
    if not over:
        return []
    names = ", ".join(f"{s.label} ({s.feed_mm_min:.0f} mm/min)" for s in over[:6])
    tail = "" if len(over) <= 6 else f" +{len(over) - 6} more"
    return [f"{len(over)} specimen(s) exceed the machine max feed "
            f"{max_feed:.0f} mm/min: {names}{tail} -- cap the ladder or the "
            f"machine will clamp them"]


def _validate_plate(p, what):
    if p.slide_w <= 2 * p.margin or p.slide_h <= 2 * p.margin:
        raise ValueError(f"plate {p.slide_w}x{p.slide_h} mm leaves no room "
                         f"inside a {p.margin} mm margin")
    if p.diam <= 0:
        raise ValueError("fibre diameter must be > 0")
    if p.stab.enabled and (p.stab.width <= 0 or p.stab.height <= 0
                           or p.stab.layers < 1 or p.stab.stepover <= 0):
        raise ValueError(f"stabilization base dimensions invalid for the {what}")


# --------------------------------------------------------------------------- #
# the CTS straight-line sweep
# --------------------------------------------------------------------------- #
def _validate_loops(p):
    """The linking-loop fields of a line print, in the operator's words."""
    arc = getattr(p, "loop_arc", 0.0) or 0.0
    if not math.isfinite(arc) or arc < 0:
        raise ValueError("turnaround radius must be 0 or more (x spacing)")
    if arc and int(getattr(p, "loop_segments", 8)) < 2:
        raise ValueError("arc segments must be 2 or more")
    ls = getattr(p, "loop_speed", None)
    if ls is not None and (not math.isfinite(ls) or ls <= 0):
        raise ValueError("loop speed must be > 0")


def _line_move(lines, prev, a, b, feed, p, z=0.0):
    """Append one test line from `a` to `b` at `feed`, and return what the
    next call needs as `prev`.

    `prev` is the line printed just before, `(a, b, feed)`, or None for the
    first line of a run. With `p.loop_arc` 0 every line opens with a `G0` hop
    to its start -- the file these prints always wrote. With it set, the hop
    from the end of the line before is replaced by the Scaffold designer's
    turnaround loop (`scaffold_gcode._turn_points`, the circle the designer
    joins its passes with): radius `loop_arc` x the distance between the two
    lines, ballooning out past the line end, `loop_segments` chords per
    half-turn (one true G2/G3 under `p.g2g3`), at `p.loop_speed` or the feed
    of the line before -- the designer's blank Loop speed. Continuous flow
    lays that hop as fibre anyway; the loop decides where."""
    arc = getattr(p, "loop_arc", 0.0) or 0.0
    looped = False
    if prev is not None and arc > 0:
        pa, pb, pfeed = prev
        chord = math.hypot(a[0] - pb[0], a[1] - pb[1])
        outward = (pb[0] - pa[0], pb[1] - pa[1])
        if chord > 1e-9 and math.hypot(*outward) > 1e-9:
            radius = arc * chord
            lf = getattr(p, "loop_speed", None) or pfeed
            if getattr(p, "g2g3", False):
                sol = scaffold_gcode._turn_circle(pb, a, outward, radius)
                if sol is not None:
                    cx, cy, _r, _ab, sweep = sol
                    hx, hy = _cur_xy(lines)          # where the file says we are
                    lines.append(
                        f"{'G3' if sweep > 0 else 'G2'} X{_fmt(a[0])} "
                        f"Y{_fmt(a[1])} I{_fmt(cx - hx)} J{_fmt(cy - hy)} "
                        f"F{_fmt(lf)}")
                    looped = True
            else:
                pts = scaffold_gcode._turn_points(
                    pb, a, outward, radius, int(p.loop_segments)) + [a]
                for j, (x, y) in enumerate(pts):
                    lines.append(f"G1 X{_fmt(x)} Y{_fmt(y)}"
                                 + (f" F{_fmt(lf)}" if j == 0 else ""))
                looped = True
    if not looped:
        lines.append(f"G0 X{_fmt(a[0])} Y{_fmt(a[1])} Z{_fmt(z)}")
    lines.append(f"G1 X{_fmt(b[0])} Y{_fmt(b[1])} F{_fmt(feed)}")
    return (a, b, feed)


def _loop_reach(p, chord):
    """How far past a line's end the loop between two lines `chord` mm apart
    reaches: the far side of `_turn_circle`'s circle, `R + sqrt(R^2 -
    (chord/2)^2)`. 0 with no loop. The generator pulls the line ends in by this
    much, so the loops land inside the margin and the base gap instead of off
    the plate."""
    m = getattr(p, "loop_arc", 0.0) or 0.0
    if m <= 0 or chord <= 0:
        return 0.0
    R = max(m * chord, chord / 2.0)
    return R + math.sqrt(max(0.0, R * R - (chord / 2.0) ** 2))


def _loop_feed_warnings(p):
    """The loop speed over the machine ceiling, which the line check misses."""
    ls, mf = getattr(p, "loop_speed", None), getattr(p, "max_feed", None)
    if (getattr(p, "loop_arc", 0.0) or 0.0) > 0 and ls and mf and mf > 0 \
            and ls > mf + 1e-6:
        return [f"the loop speed {ls:.0f} mm/min exceeds the machine max feed "
                f"{mf:.0f} mm/min -- the machine will clamp it"]
    return []


def speed_ladder(start, stop, step):
    """Descending feeds start -> stop (inclusive-ish) in `step` mm/min."""
    start, stop, step = float(start), float(stop), float(step)
    if not all(math.isfinite(v) for v in (start, stop, step)):
        raise ValueError("the feeds and the step must be finite numbers")
    if step <= 0:
        raise ValueError("feed step must be positive")
    if start < stop:
        raise ValueError("start feed must be >= stop feed (the ladder descends)")
    n = int(math.floor((start - stop) / step + 1e-9)) + 1
    return [round(start - i * step, 4) for i in range(max(1, n))]


def _place_lines(n, pitch, slide_h, margin, x0, bottom_up):
    """Single column per slide (never a second column -- a cross-column travel
    would lay a stray fibre over the finished lines); overflow spills to a new
    print. Returns [[(x0, y_centreline)]] per print, in speed order."""
    if pitch <= 0:
        raise ValueError("line pitch must be > 0")
    y_top = slide_h - margin
    per_col = max(1, int((y_top - margin) / pitch + 1e-9) + 1)
    prints = []
    for i in range(0, n, per_col):
        cnt = min(per_col, n - i)
        if bottom_up:
            prints.append([(x0, round(margin + j * pitch, 4))
                           for j in range(cnt)])
        else:
            prints.append([(x0, round(y_top - j * pitch, 4))
                           for j in range(cnt)])
    return prints


def generate_cts(p: CtsParams) -> SweepResult:
    """The straight-line CTS sweep -> SweepResult. Raises ValueError with a
    user-facing message on an impossible spec (the preview shows it)."""
    _validate_plate(p, "CTS sweep")
    _validate_loops(p)
    if p.reps < 1:
        raise ValueError("reps must be >= 1")
    feeds = speed_ladder(p.start, p.stop, p.step)
    if p.slowest_first:                              # the ladder itself descends
        feeds = feeds[::-1]
    conds = [(f, r) for f in feeds for r in range(p.reps)]
    base_speed = p.base_speed if p.base_speed is not None else p.start
    warnings = []

    ox, oy = p.origin_x, p.origin_y
    ldx, ldy = p.lines_offset                       # manual line-block placement
    bdx, bdy = p.base_offset                         # manual base placement
    if p.stab.enabled:
        bmnx, bmny, bmxx, bmxy = _measure_base_bbox(p.stab, p.diam, base_speed)
        x0 = p.margin + (bmxx - bmnx) + p.col_gap    # lines right of the base bbox
    else:
        bmnx = bmny = 0.0
        x0 = p.margin
    # the loops at both line ends need room of their own (0 without loops)
    inset = _loop_reach(p, p.pitch)
    x0 += inset

    line_len = p.line_len
    if line_len is None:                             # auto-fill to the far edge
        line_len = (p.slide_w - p.margin - inset) - x0
    line_len = round(line_len, 4)
    if line_len <= 0:
        raise ValueError("no room for test lines -- shrink the base / margin "
                         "or widen the plate")

    prints_xy = _place_lines(len(conds), p.pitch, p.slide_h, p.margin, x0,
                             p.bottom_up)

    texts, rows, metas = [], [], []
    all_specimens = []
    idx = 0
    for pi, pts in enumerate(prints_xy, 1):
        lines = ["G21", "G90"]
        labels, specimens = [], []
        if p.stab.enabled:
            _emit_base(lines, ox + p.margin - bmnx + bdx, oy + p.margin - bmny + bdy,
                       p.stab, p.diam, base_speed, g2g3=p.g2g3)
        prev = None                                 # one loop run per slide
        for k, (xc, y) in enumerate(pts):
            feed, r = conds[idx]
            idx += 1
            left, right = ox + xc + ldx, ox + xc + line_len + ldx
            xa, xb = (left, right) if (k % 2 == 0) else (right, left)
            ya = oy + y + ldy
            prev = _line_move(lines, prev, (xa, ya), (xb, ya), feed, p)
            label = f"cts_{_fmt(feed)}mmmin" + (f"_r{r + 1}" if p.reps > 1 else "")
            labels.append(label)
            specimens.append(Specimen(
                label=label, value_text=f"{feed:.0f}",
                center=((left + right) / 2.0, ya), size=line_len,
                feed_mm_min=feed))
            rows.append(dict(
                print=pi, label=label,
                v_mm_min=feed, v_mm_s=round(feed / 60.0, 4),
                eps_ref=round(feed / p.cts_ref, 3) if p.cts_ref else "",
                rep=r + 1, x_start=round(xa, 3), x_end=round(xb, 3),
                y_mm=round(ya, 3), len_mm=line_len))
        text = "\n".join(lines) + "\n"
        texts.append(text)
        metas.append(_print_meta(pi, text, labels, p, specimens))
        all_specimens.extend(specimens)

    for m in metas:
        if not m.on_slide:
            warnings.append(
                f"slide {m.index} exceeds the plate -- shorten the line length, "
                f"lower the pitch or split the feed range")
    warnings += _feed_ceiling_warnings(all_specimens, p.max_feed)
    warnings += _loop_feed_warnings(p)

    summary = (f"CTS sweep {p.start:.0f} -> {p.stop:.0f} mm/min, step "
               f"{p.step:.0f} ({p.step / 60:.3f} mm/s) x {p.reps} reps: "
               f"{len(feeds)} speeds ("
               f"{'slowest' if p.slowest_first else 'fastest'} first), "
               f"{len(conds)} lines @ {line_len:.1f} mm, "
               f"{len(texts)} slide(s), base "
               f"{'ON' if p.stab.enabled else 'OFF'} @ {base_speed:.0f} mm/min")
    return SweepResult("cts", _finish_texts(texts, p), _cts_csv(rows), rows,
                       metas, summary, warnings)


def _cts_csv(rows):
    cols = ["print", "label", "v_mm_min", "v_mm_s", "eps_ref", "rep",
            "x_start", "x_end", "y_mm", "len_mm",
            "straight_yes_no", "coil_wavelength_mm", "notes"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\r\n")
    w.writeheader()
    for r in rows:
        out = {k: r.get(k, "") for k in cols}
        out["straight_yes_no"] = out["coil_wavelength_mm"] = out["notes"] = ""
        w.writerow(out)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# the melt temperature ladder (activation energy, and the viscosity anchor)
# --------------------------------------------------------------------------- #
def temperature_ladder(temps):
    """Clean a user-typed ladder: floats, duplicates dropped, order preserved.

    Order is preserved rather than sorted because DESCENDING is the runbook's
    protocol and slide 1 is therefore the anchor
    rung -- re-sorting would silently move the anchor to the last slide printed, on
    the most degraded melt, which is the one thing the descending order exists to
    prevent."""
    out = []
    for t in temps:
        t = round(float(t), 4)
        if t not in out:
            out.append(t)
    return out


def generate_diameter(p: DiamParams) -> SweepResult:
    """The melt temperature ladder -> SweepResult, ONE SLIDE PER RUNG.

    Each slide is `reps` parallel straight lines at the single fixed feed `speed`,
    laid right of the stabilization base exactly as the CTS sweep lays its column --
    the same `_place_lines` geometry, the same boustrophedon so a connector never
    crosses a finished line. The slides are geometrically identical to one another on
    purpose: the temperature is not in the engine's text (no M104/M109), it is
    what the operator sets between prints. The app's export may add the rung's
    own setpoint (Setup's heat-and-wait, the start block's
    [[temperature]]), never another rung's.

    Never-erase: a rung whose replicate count will not fit one column is laid to the
    column's capacity and the shortfall is NAMED in `oversize`, never dropped
    silently and never spilled onto a second slide -- a second slide would be a
    second rung, and the slide index IS the rung."""
    _validate_plate(p, "temperature ladder")
    if p.reps < 1:
        raise ValueError("lines per temperature must be >= 1")
    temps = temperature_ladder(p.temps)
    if not temps:
        raise ValueError("give at least one nozzle temperature (degC)")
    if p.speed <= 0:
        raise ValueError("print feed must be > 0")
    base_speed = p.base_speed if p.base_speed is not None else p.speed
    warnings, oversize = [], []

    ox, oy = p.origin_x, p.origin_y
    if p.stab.enabled:
        bmnx, bmny, bmxx, bmxy = _measure_base_bbox(p.stab, p.diam, base_speed)
        x0 = p.margin + (bmxx - bmnx) + p.col_gap    # lines right of the base bbox
    else:
        bmnx = bmny = 0.0
        x0 = p.margin

    line_len = p.line_len
    if line_len is None:                             # auto-fill to the far edge
        line_len = (p.slide_w - p.margin) - x0
    line_len = round(line_len, 4)
    if line_len <= 0:
        raise ValueError("no room for test lines -- shrink the base / margin "
                         "or widen the plate")
    if line_len < 20.0:
        warnings.append(
            f"the lines are {line_len:.1f} mm; the diameter route asks for at "
            f"least 20 mm so three measurement stations fit along one fibre")

    # one column, capacity-checked ONCE: every rung's slide is identical, so a
    # shortfall is the same shortfall on all of them.
    per_col = len(_place_lines(p.reps, p.pitch, p.slide_h, p.margin, x0,
                               p.bottom_up)[0])
    laid = min(p.reps, per_col)
    if laid < p.reps:
        oversize.append(
            f"{p.reps} lines per temperature do not fit one column: "
            f"{laid} laid, {p.reps - laid} dropped -- lower the line pitch "
            f"({p.pitch:g} mm), raise the plate height or ask for fewer lines")
    pts = _place_lines(laid, p.pitch, p.slide_h, p.margin, x0, p.bottom_up)[0]

    texts, rows, metas = [], [], []
    all_specimens = []
    for pi, temp in enumerate(temps, 1):
        lines = ["G21", "G90"]
        labels, specimens = [], []
        base_bbox = None
        if p.stab.enabled:
            _emit_base(lines, ox + p.margin - bmnx, oy + p.margin - bmny,
                       p.stab, p.diam, base_speed, g2g3=p.g2g3)
            base_bbox = _xy_bbox(lines)
        for k, (xc, y) in enumerate(pts):
            left, right = ox + xc, ox + xc + line_len
            xa, xb = (left, right) if (k % 2 == 0) else (right, left)
            ya = oy + y
            lines.append(f"G0 X{_fmt(xa)} Y{_fmt(ya)} Z0")
            lines.append(f"G1 X{_fmt(xb)} Y{_fmt(ya)} F{_fmt(p.speed)}")
            label = f"d_{_fmt(temp)}C" + (f"_r{k + 1}" if laid > 1 else "")
            labels.append(label)
            specimens.append(Specimen(
                label=label, value_text=f"{temp:g}\u00b0C",
                center=((left + right) / 2.0, ya), size=line_len,
                feed_mm_min=p.speed))
            rows.append(dict(
                print=pi, label=label, T_C=temp,
                anchor="yes" if pi == 1 else "",
                v_mm_min=p.speed, v_mm_s=round(p.speed / 60.0, 4),
                pressure_psi=(p.pressure_psi if p.pressure_psi else ""),
                rep=k + 1, x_start=round(xa, 3), x_end=round(xb, 3),
                y_mm=round(ya, 3), len_mm=line_len))
        text = "\n".join(lines) + "\n"
        texts.append(text)
        metas.append(_print_meta(pi, text, labels, p, specimens, base_bbox))
        all_specimens.extend(specimens)

    for m, temp in zip(metas, temps):
        if not m.on_slide:
            warnings.append(
                f"slide {m.index} ({temp:g} degC) exceeds the plate -- shorten "
                f"the line length or lower the line pitch")
    warnings += _feed_ceiling_warnings(all_specimens[:laid], p.max_feed)

    # One rung is the VISCOSITY plate, not a ladder of length one, and it has no
    # "anchor rung" to name -- there is only the one slide. Calling it a ladder
    # on the Viscosity tab is the shared-design wording the split removed
    # everywhere else.
    if len(temps) == 1:
        summary = (f"diameter plate at {temps[0]:g} degC, {p.speed:.0f} mm/min "
                   f"({p.speed / 60:.3f} mm/s): 1 slide x {laid} line(s) @ "
                   f"{line_len:.1f} mm, base "
                   f"{'ON' if p.stab.enabled else 'OFF'} @ {base_speed:.0f} mm/min")
    else:
        summary = (f"temperature ladder {' -> '.join(f'{t:g}' for t in temps)} degC "
                   f"at a fixed {p.speed:.0f} mm/min ({p.speed / 60:.3f} mm/s): "
                   f"{len(temps)} slide(s) x {laid} line(s) @ {line_len:.1f} mm, "
                   f"anchor rung {temps[0]:g} degC on slide 1, base "
                   f"{'ON' if p.stab.enabled else 'OFF'} @ {base_speed:.0f} mm/min")
    return SweepResult("diam", _finish_texts(texts, p), _diam_csv(rows), rows,
                       metas, summary, warnings, oversize)


def _diam_csv(rows):
    cols = ["print", "label", "T_C", "anchor", "v_mm_min", "v_mm_s",
            "pressure_psi", "rep", "x_start", "x_end", "y_mm", "len_mm",
            "d1_mm", "d2_mm", "d3_mm", "d_mean_mm", "soak_min", "notes"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\r\n")
    w.writeheader()
    for r in rows:
        out = {k: r.get(k, "") for k in cols}
        for blank in ("d1_mm", "d2_mm", "d3_mm", "d_mean_mm", "soak_min",
                      "notes"):
            out[blank] = ""
        w.writerow(out)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# the sag ladder (the fibre sag coefficient, and its span exponent)
# --------------------------------------------------------------------------- #
def sag_gaps(spans, mirror=True):
    """Clean a user-typed span list into the gap sequence the row is built from.

    Non-positive entries are dropped. Duplicates are KEPT, unlike the temperature
    ladder's: two equal gaps side by side are two independent readings of the same
    span, which is a replicate an operator may legitimately ask for, whereas two
    equal rungs of a ladder would be the same slide printed twice. `mirror` appends
    the list reversed, so every span is laid once approached from the narrow side
    and once from the wide side.
    """
    out = [round(float(x), 4) for x in parse_float_list(spans)
           if float(x) > 0]
    if mirror:
        out = out + out[::-1]
    return out


def sag_band(spans, coeff, exp, diam, wall_top_z):
    """Split `spans` (mm) into (flat, thin, good) by what the droop law says each
    one will do on a ladder whose wall tops stand `wall_top_z` mm up.

      * flat -- `coeff * span**exp` reaches the collector: no droop to measure.
      * thin -- droop under three fibre diameters: the sub-diameter BENDING regime,
        not the stretching-dominated span^2 catenary the fit is recovering, so these
        points pull the exponent toward a different law.
      * good -- what a fit can actually use.

    Shared by the generator's warnings and the card's readout so the two cannot
    disagree about which spans are measurable. `coeff`/`exp` are the live c and p.
    """
    if diam <= 0:
        return [], [], list(spans)
    flat = [s for s in spans if coeff * s ** exp >= wall_top_z]
    thin = [s for s in spans
            if s not in flat and coeff * s ** exp < 3.0 * diam]
    good = [s for s in spans if s not in flat and s not in thin]
    return flat, thin, good


def sag_span_warnings(p: SagParams, spans, wall_top_z):
    """Which spans of a sag ladder will not give a measurable droop, and why.

    Three failures, all decided by the droop LAW rather than by the span in mm --
    which is the whole correction here. The check this replaces compared the wall
    height to `max(span)` directly; those are both millimetres, so it ran, but the
    droop is `c * span**p` and the two only coincide near c = 1/span^(p-1). At the
    shipped c = 0.3416 mm^-1 it let three of the seven default spans through as
    measurable when all three flatten onto the collector.

    The band it names is bounded BELOW as well as above, and the lower bound is not
    a measurement limit but a regime one: a droop of order the fibre diameter is the
    sub-diameter bending transient, where the exponent is not the stretching-dominated
    span^2 the fit is trying to recover (`bridge_sag_depth`'s own docstring). Fitting
    p across that boundary mixes two laws.

    Clearance is taken as the wall-top height, one layer under where the fibre
    actually sits, so the bottoming-out call is conservative by one layer.

    The law's own c is what the print exists to measure, so every note here is
    advisory and NOTHING is rescaled: a wrong c by 3x moves the band, and the
    operator's `bottomed_out` column is the authority in the end. They ride
    `SweepResult.advisories`, never `warnings`: the Calibration page's red strip
    is for a print that will not come out, and these say the print is fine but
    the question it answers is narrower than the design implies.
    """
    out = []
    if not spans or p.diam <= 0:
        return out

    def droop(s):
        return p.sag_coeff * s ** p.sag_span_exp

    flat, thin, good = sag_band(spans, p.sag_coeff, p.sag_span_exp, p.diam,
                                wall_top_z)
    if flat:
        need = int(droop(max(flat)) / p.diam) + 2
        out.append(
            "the walls are %.3f mm tall and the droop law (c = %g, p = %g) puts "
            "%s mm on the collector -- those spans measure nothing; %d layers "
            "would clear the widest of them"
            % (wall_top_z, p.sag_coeff, p.sag_span_exp,
               " / ".join("%g" % s for s in flat), need))
    if thin:
        out.append(
            "%s mm droop less than three fibre diameters (%.3f mm), which is the "
            "sub-diameter bending regime rather than the span^2 catenary the fit "
            "recovers -- read them if you like, but expect them to pull p"
            % (" / ".join("%g" % s for s in thin), 3.0 * p.diam))
    if len(good) < 3:
        out.append(
            "only %d span(s) land in the measurable band, and fitting the span "
            "exponent needs at least three -- the fit will fall back to the fixed "
            "exponent from the Physics page" % (len(good),))
    elif max(good) / min(good) < 3.0:
        out.append(
            "the measurable spans run %g to %g mm, a %.1fx range; the exponent fit "
            "wants 3x or more -- raise the wall height to keep the wide end, or "
            "the fit is really a magnitude fit"
            % (min(good), max(good), max(good) / min(good)))
    return out


def generate_sag(p: SagParams) -> SweepResult:
    """The sag ladder -> SweepResult, ONE SLIDE.

    Emission order: every wall, sweep by sweep, then the bridging fibre(s). EVERY
    gap-crossing move sits in the travel lane, because a travel move on a
    continuous-flow rig deposits fibre and a strand suspended in a gap is a strand
    the operator has to look through. A wall visit is `layers_per_visit` strokes out
    and back from the lane, so the head is always in the lane when it hops; with
    `travel_lane="middle"` the visit does that on each side of the lane in turn, so
    the two half-walls rise together. Sweeps always run left to right and return
    along the lane, which is what carries the Z step -- the emission comment below
    says why that is not a free choice.

    The bridging fibres run the full row plus `lead` either side, one layer above the
    wall tops, and are REACHED THROUGH THE LANE: the head runs along the lane to the
    lead-in X first and only then moves out in Y, beyond the last wall, where no gap
    is crossed. A direct diagonal to the lead-in would lay a strand across every gap
    at exactly the height being measured.

    NEVER-ERASE: a ladder too wide or too tall for the usable plate is laid anyway
    and named in `oversize`, never shrunk silently -- a quietly rescaled span list
    would make every measured droop a droop at a span the operator did not ask for.
    The same holds for a span the droop law says will bottom out: it is printed and
    named in `advisories`, since the law's own c is what this print exists to
    measure -- which is also why it is an advisory and not a warning.
    """
    _validate_plate(p, "sag ladder")
    gaps = sag_gaps(p.spans, p.mirror)
    if not gaps:
        raise ValueError("give at least one span in mm")
    if p.wall_layers < 1:
        raise ValueError("wall height must be at least 1 layer")
    if p.wall_len <= 0:
        raise ValueError("wall length must be > 0")
    if p.travel_lane not in ("middle", "edge"):
        raise ValueError("the travel lane is \"middle\" or \"edge\", not %r"
                         % (p.travel_lane,))
    if p.layers_per_visit < 2 or p.layers_per_visit % 2:
        raise ValueError(
            "layers per wall visit must be an EVEN number of at least 2 -- an odd "
            "count leaves the head at the far end of the wall, and the hop to the "
            "next wall then crosses the gap on the side being measured")
    if p.wall_layers % p.layers_per_visit:
        raise ValueError(
            "%d layers per wall visit does not divide %d wall layers, so the last "
            "band would be a part-height pass -- pick a divisor of %d"
            % (p.layers_per_visit, p.wall_layers, p.wall_layers))
    if p.fibre_inset < 0:
        raise ValueError("the bridging-fibre inset cannot be negative")
    if p.fibre_inset >= p.wall_len:
        raise ValueError(
            "a bridging fibre %g mm inboard of the end of a %g mm wall is not on "
            "the wall at all -- lengthen the wall or lower the inset"
            % (p.fibre_inset, p.wall_len))
    if p.lead < 0:
        raise ValueError("the lead-in cannot be negative")
    if p.speed <= 0:
        raise ValueError("print feed must be > 0")
    if min(gaps) <= p.diam:
        raise ValueError(
            "the narrowest span %g mm is not wider than the fibre (%g mm), so "
            "its two walls would be the same wall -- span is the wall "
            "CENTRELINE pitch, not the clear gap" % (min(gaps), p.diam))
    base_speed = p.base_speed if p.base_speed is not None else p.speed
    warnings, oversize = [], []

    ox, oy = p.origin_x, p.origin_y
    if p.stab.enabled:
        bmnx, bmny, bmxx, bmxy = _measure_base_bbox(p.stab, p.diam, base_speed)
        x0 = p.margin + (bmxx - bmnx) + p.col_gap    # the ladder right of the base
    else:
        bmnx = bmny = 0.0
        x0 = p.margin

    # wall X positions: running sums of the gap list. The row starts `lead` to the
    # right of x0 so the bridging fibres' lead-in still begins inside the margin.
    wx = [round(x0 + p.lead, 4)]
    for g in gaps:
        wx.append(round(wx[-1] + g, 4))
    row_w = wx[-1] - wx[0]
    x_lead_l = round(wx[0] - p.lead, 4)
    x_lead_r = round(wx[-1] + p.lead, 4)

    # -- Y layout: the lane, the wall extremes, and the fibres ---------------- #
    # "middle": lane down the wall's midline, one `wall_len` section either side of
    # it, a fibre inboard of each outer extreme. "edge": lane at the near end, one
    # section, one fibre inboard of the far extreme.
    mid_lane = (p.travel_lane == "middle")
    y_bot = p.margin                              # against the plate's front edge
    wall_span = p.wall_len * (2.0 if mid_lane else 1.0)
    y_far = round(y_bot + wall_span, 4)
    y_lane = round(y_bot + p.wall_len, 4) if mid_lane else y_bot
    y_a = round(y_bot + p.fibre_inset, 4) if mid_lane else round(
        y_far - p.fibre_inset, 4)
    y_b = round(y_far - p.fibre_inset, 4) if mid_lane else None
    # the wall legs a visit strokes out to, from the lane
    legs = (y_far, y_bot) if mid_lane else (y_far,)
    z_top = round((p.wall_layers - 1) * p.diam, 5)
    z_fib = round(p.wall_layers * p.diam, 5)

    if x_lead_r + p.margin > p.slide_w + 1e-9:
        oversize.append(
            "the ladder is %.1f mm wide and runs past the plate's %g mm margin "
            "-- drop the widest spans, turn the mirrored pass off or shorten "
            "the lead-in (%g mm)"
            % (x_lead_r - x_lead_l, p.margin, p.lead))
    if y_far + p.margin > p.slide_h + 1e-9:
        oversize.append(
            "the walls run %g mm in Y and do not fit the %g mm plate height "
            "inside a %g mm margin" % (wall_span, p.slide_h, p.margin))

    lines = ["G21", "G90"]
    base_bbox = None
    if p.stab.enabled:
        _emit_base(lines, ox + p.margin - bmnx, oy + p.margin - bmny,
                   p.stab, p.diam, base_speed, g2g3=p.g2g3)
        base_bbox = _xy_bbox(lines)

    # -- the walls, sweep by sweep ------------------------------------------ #
    # TWO RULES, and they are the converter's, not this function's taste. Every
    # printing G1 is at a CONSTANT Z, and every Z change rides a G0 that also moves
    # in XY -- which is exactly what `scaffold_gcode.py:1366` does at a layer change,
    # and the only shape the sag solver is known to swallow. Break either one and
    # `gcode_to_fibers._split_layer_spanning` gets a deposited path whose Z rises and
    # falls again inside one layer tolerance; it cuts on distance from a band's
    # anchor, so such a band survives the cut with twice the spread it allows, the
    # split reaches a fixed point, and `apply_sag` recurses until Python gives up.
    # An earlier draft of this loop sloped the G1s and sawtoothed Z per wall, and did
    # precisely that -- 773 bands, unchanged, forever.
    #
    # So: a SWEEP lays `layers_per_visit` passes on every wall at ONE commanded Z,
    # left to right, hopping along the lane between walls. Z rises once per sweep, on
    # the lane traverse that returns to the left-hand wall -- a move that crosses the
    # row in X, in the lane, where a strand is harmless. Z is therefore monotonic
    # across the whole print and no printing stroke changes it at all.
    #
    # The passes are an out-and-back at one nozzle height, so the wall gains
    # `layers_per_visit` fibres per sweep while the nozzle stands still, and the Z
    # step per sweep is `layers_per_visit` layers to match. The standoff is short by
    # up to (layers_per_visit - 1) fibre diameters at the end of a sweep, which is
    # why this knob stays SMALL -- 2 costs one diameter against a 3.5 mm standoff.
    lpv = p.layers_per_visit
    lines.append("G0 X%s Y%s Z0" % (_fmt(ox + wx[0]), _fmt(oy + y_lane)))
    n_sweeps = p.wall_layers // lpv
    for sweep in range(n_sweeps):
        z = round(sweep * lpv * p.diam, 5)
        for n, i in enumerate(range(len(wx))):
            x = ox + wx[i]
            for y_end in legs:
                for j in range(lpv):
                    y_to = y_end if j % 2 == 0 else y_lane
                    lines.append("G1 X%s Y%s F%s"
                                 % (_fmt(x), _fmt(oy + y_to), _fmt(p.speed)))
            if n + 1 < len(wx):                  # the hop, along the lane
                lines.append("G0 X%s Y%s"
                             % (_fmt(ox + wx[n + 1]), _fmt(oy + y_lane)))
        if sweep + 1 < n_sweeps:                 # back to the left, one Z step up
            lines.append("G0 X%s Y%s Z%s"
                         % (_fmt(ox + wx[0]), _fmt(oy + y_lane),
                            _fmt(round((sweep + 1) * lpv * p.diam, 5))))

    # -- the bridging fibre(s) ----------------------------------------------- #
    # The approach runs ALONG THE LANE to the lead-in X and only then out in Y, past
    # the end of the row where there is no gap to cross. Fibre B (mid-lane only)
    # starts where A finished, so its approach is a pure Y move at the lead-out X.
    fibres = [("A", y_a, x_lead_l, x_lead_r)]
    if mid_lane:
        fibres.append(("B", y_b, x_lead_r, x_lead_l))
    lines.append("G0 X%s Y%s Z%s"
                 % (_fmt(ox + x_lead_l), _fmt(oy + y_lane), _fmt(z_fib)))
    for _fib, y, xa, xb in fibres:
        lines.append("G0 X%s Y%s Z%s"
                     % (_fmt(ox + xa), _fmt(oy + y), _fmt(z_fib)))
        lines.append("G1 X%s Y%s F%s"
                     % (_fmt(ox + xb), _fmt(oy + y), _fmt(p.speed)))

    text = "\n".join(lines) + "\n"

    # -- rows, one per gap per fibre; preview badges, one per gap ----------- #
    n_fwd = len(gaps) // 2 if p.mirror else len(gaps)
    rows, specimens, labels = [], [], []
    for i, g in enumerate(gaps):
        left, right = wx[i], wx[i + 1]
        mid = round((left + right) / 2.0, 4)
        block_no = 1 if (not p.mirror or i < n_fwd) else 2
        specimens.append(Specimen(
            label="sag_g%02d" % (i + 1), value_text="%g mm" % g,
            center=(ox + mid, oy + (y_lane if mid_lane else y_a)),
            size=g, feed_mm_min=p.speed))
        for fib, y, _xa, _xb in fibres:
            label = "sag_g%02d_%s_%s" % (i + 1, _fmt(g), fib)
            labels.append(label)
            rows.append(dict(
                label=label, block=block_no, gap=i + 1, span_mm=g,
                clear_gap_mm=round(g - p.diam, 4), fibre=fib,
                x_left_mm=round(ox + left, 3), x_mid_mm=round(ox + mid, 3),
                x_right_mm=round(ox + right, 3), y_mm=round(oy + y, 3),
                wall_top_z_mm=z_top, fibre_z_mm=z_fib,
                wall_layers=p.wall_layers, fibre_diam_mm=p.diam,
                v_mm_min=p.speed, v_mm_s=round(p.speed / 60.0, 4)))

    meta = _print_meta(1, text, labels, p, specimens, base_bbox)
    if not meta.on_slide:
        warnings.append(
            "the ladder runs off the plate -- drop the widest spans, shorten "
            "the lead-in or turn the stabilization base off")
    warnings += _feed_ceiling_warnings(specimens, p.max_feed)
    advisories = sag_span_warnings(p, sorted(set(gaps)), z_top)
    if p.layers_per_visit > 2:
        warnings.append(
            "%d passes per wall visit are laid at one nozzle height, so the "
            "standoff ends each visit %.3f mm short -- lower it to 2 unless you "
            "want that" % (p.layers_per_visit,
                           (p.layers_per_visit - 1) * p.diam))

    summary = (
        "sag ladder %s mm%s: 1 slide x %d span(s), %d walls %g mm in Y x "
        "%d layers (%.3f mm tall), %s lane, %d bridging fibre(s) at %.3f mm, "
        "%.1f x %g mm, base %s @ %.0f mm/min"
        % (" / ".join("%g" % g for g in gaps[:n_fwd]),
           " mirrored" if p.mirror else "", len(gaps), len(wx), wall_span,
           p.wall_layers, z_top, p.travel_lane, len(fibres), z_fib,
           row_w + 2 * p.lead, wall_span,
           "ON" if p.stab.enabled else "OFF", base_speed))
    return SweepResult("sag", _finish_texts([text], p), _sag_csv(rows), rows,
                       [meta], summary, warnings, oversize, advisories)


def _sag_csv(rows):
    cols = ["label", "block", "gap", "span_mm", "clear_gap_mm", "fibre",
            "x_left_mm", "x_mid_mm", "x_right_mm", "y_mm", "wall_top_z_mm",
            "fibre_z_mm", "wall_layers", "fibre_diam_mm", "v_mm_min", "v_mm_s",
            "left_mm", "mid_mm", "right_mm", "depth_mm", "bottomed_out",
            "notes"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\r\n")
    w.writeheader()
    for r in rows:
        out = {k: r.get(k, "") for k in cols}
        for blank in ("left_mm", "mid_mm", "right_mm", "depth_mm",
                      "bottomed_out", "notes"):
            out[blank] = ""
        w.writerow(out)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# the jet-lag open squares
# --------------------------------------------------------------------------- #
def square_side(eps, gap, safety, floor_mm, fixed_side=0.0, side_per_eps=0.0):
    """The specimen side (mm). Two modes:

      * fixed_side <= 0 (default): AUTO-size from the predicted lag -- side =
        safety*lag rounded UP to an even mm, floored at `floor_mm`. Because
        lag ~ gap, the square GROWS with the gap (a bigger lag needs a longer
        steady run-up before each corner -- the Hrynevich protocol's reason).
      * fixed_side > 0: gap-INDEPENDENT sizing -- side = fixed_side +
        side_per_eps*eps, used verbatim (NOT rounded, NOT floored), so a plate
        keeps a chosen size no matter the gap (set it to reproduce a printed
        plate). side_per_eps (mm per unit eps, default 0 -> constant) optionally
        enlarges the faster ratios that lag more. Clamped just above 0."""
    if fixed_side and fixed_side > 0.0:
        return max(0.1, fixed_side + side_per_eps * eps)
    return max(floor_mm, 2.0 * math.ceil(safety * lag_mm(eps, gap) / 2.0))


def lag_label(eps, rep, reps):
    """The CSV / manual-placement label for one square (unique per specimen).
    The `_r<n>` suffix appears only when there is more than one rep, matching the
    emitted g-code + CSV labels so a placement override keys to the right square."""
    return f"lag_eps{_fmt(eps)}" + (f"_r{rep + 1}" if reps > 1 else "")


def _square_moves(x0, y0, s):
    """Vertices of one open square, start/end at bottom-mid (S). The corners
    with a full-side steady approach -- TR, TL, BL -- are the measured ones."""
    S = (x0 + s / 2.0, y0)
    return [S, (x0 + s, y0), (x0 + s, y0 + s), (x0, y0 + s), (x0, y0), S]


def _pack_into_prints(sizes, slide_w, slide_h, margin, spacing,
                      base_w, base_h, use_base):
    """First-fit-DECREASING shelf pack onto slides; each slide reserves the
    base's real bbox at bottom-left. Returns (prints, oversize) where
    prints = [{'base_xy', 'squares': [(cond_idx, x0, y0, size)]}] in plate-local
    coords and oversize = [cond_idx] for any specimen too large for the usable
    plate.

    Never-erase contract: a normal excess of specimens auto-splits onto more
    slides; a single specimen larger than the usable plate is NOT dropped
    silently and does NOT raise -- it is returned in `oversize` so the caller can
    lay out everything that DOES fit and surface an explicit "won't fit" note,
    rather than reverting the whole layout to stale geometry."""
    prints = []
    oversize = []

    def new_print():
        pr = {"base_xy": (margin, margin) if use_base else None, "squares": []}
        cx = margin + (base_w + spacing if use_base else 0.0)
        return pr, cx, margin, (base_h if use_base else 0.0)

    # the widest a square could ever be and still start at the left margin, and
    # the tallest that could sit above the base row -- the usable plate window
    usable_w = slide_w - 2 * margin
    usable_h = slide_h - 2 * margin

    pr, cx, cy, row_h = new_print()
    order = sorted(range(len(sizes)), key=lambda i: -sizes[i])
    for i in order:
        s = sizes[i]
        if s > usable_w + 1e-9 or s > usable_h + 1e-9:
            oversize.append(i)                       # too big for ANY slide
            continue
        if cx + s > slide_w - margin:                # wrap to the next row
            cy += row_h + spacing
            cx, row_h = margin, 0.0
        if cy + s > slide_h - margin:                # slide full -> new print
            prints.append(pr)
            pr, cx, cy, row_h = new_print()
            if cx + s > slide_w - margin:            # (paranoia) wrap again
                cy += row_h + spacing
                cx, row_h = margin, 0.0
        pr["squares"].append((i, cx, cy, s))
        cx += s + spacing
        row_h = max(row_h, s)
    if pr["squares"] or not prints:
        prints.append(pr)
    return prints, oversize


def _dense_pack(order, sizes, slide_w, slide_h, margin, spacing,
                base_w, base_h, use_base, vertical):
    """Dense 2-D shelf pack of the squares in `order` (the caller passes size-DESCENDING
    first-fit-decreasing order, so the pack is as tight as the FFD shelf and equal-eps
    squares stay adjacent, each eps reading as one contiguous block). That is what keeps
    columns/rows as TIGHT as the shelf: grouping each eps onto its own single line
    wasted the whole PERPENDICULAR dimension and pushed each eps onto its own slide.

      * horizontal (vertical=False -> ROWS): fill a row left->right, wrap up to the
        next band when the width runs out, new slide when the height runs out;
      * vertical   (vertical=True  -> COLUMNS): fill a column bottom->up, wrap right
        to the next column when the height runs out, new slide when width runs out.

    The base sits bottom-left; a row/column overlapping its footprint starts just past
    it. Never-erase: only a square whose side exceeds the usable plate is flagged in
    `oversize`. Returns (prints, oversize_idx)."""
    usable_w = slide_w - 2 * margin
    usable_h = slide_h - 2 * margin
    right, top = slide_w - margin, slide_h - margin
    base_r, base_t = margin + base_w, margin + base_h
    oversize = []
    prints = []

    def fresh():
        return {"base_xy": (margin, margin) if use_base else None, "squares": []}

    def row_x0(y):                     # a row at height y clears the base to its right
        return base_r + spacing if use_base and y < base_t - 1e-9 else margin

    def col_y0(x):                     # a column at x clears the base above it
        return base_t + spacing if use_base and x < base_r - 1e-9 else margin

    pr = fresh()
    if not vertical:
        cy = margin
        cx = row_x0(cy)
        # seed the first band with the base's HEIGHT so the first band-advance
        # clears the base row (mirrors the shelf packer's row_h=base_h); without
        # it the first overflow bumps only `spacing`, stranding the next row
        # inside the base band and pushing a wide square off the plate.
        band = base_h if use_base else 0.0
        for i in order:
            s = sizes[i]
            if s > usable_w + 1e-9 or s > usable_h + 1e-9:
                oversize.append(i)
                continue
            if cx + s > right + 1e-9:              # row full -> next band up
                cy += band + spacing
                cx, band = row_x0(cy), 0.0
            if cy + s > top + 1e-9:                # slide full -> new slide
                if pr["squares"]:
                    prints.append(pr)
                    pr = fresh()
                cy = margin
                cx, band = row_x0(cy), 0.0
            pr["squares"].append((i, cx, cy, s))
            cx += s + spacing
            band = max(band, s)
    else:
        cx = margin
        cy = col_y0(cx)
        # seed the first band with the base's WIDTH so the first band-advance
        # clears the base column (the vertical mirror of the shelf/row seeding).
        band = base_w if use_base else 0.0
        for i in order:
            s = sizes[i]
            if s > usable_w + 1e-9 or s > usable_h + 1e-9:
                oversize.append(i)
                continue
            if cy + s > top + 1e-9:                # column full -> next column right
                cx += band + spacing
                cy, band = col_y0(cx), 0.0
            if cx + s > right + 1e-9:              # slide full -> new slide
                if pr["squares"]:
                    prints.append(pr)
                    pr = fresh()
                cx = margin
                cy, band = col_y0(cx), 0.0
            pr["squares"].append((i, cx, cy, s))
            cy += s + spacing
            band = max(band, s)
    if pr["squares"] or not prints:
        prints.append(pr)
    return prints, oversize


def _pack_grid(conds, sizes, slide_w, slide_h, margin, spacing,
               base_w, base_h, use_base, grid_cols):
    """MATRIX: equal cells filled ascending-eps, row-major bottom->top, each square
    centred in its cell -> a clean, microscope-findable (row, col) grid. Two things
    keep it from wasting plate: the cells the stabilization base covers are skipped
    (no reserved band), and a slide's cell size is the largest square ON THAT SLIDE,
    not the global max -- so a slide of small eps packs densely instead of stranding
    every 8 mm square in an 18 mm (eps=6) cell. Because side is monotonic in eps, a
    slide holds a contiguous run whose cell = its last (largest) square. `grid_cols`
    sets the width (0 = near-square), clamped to what fits; overflow spills to more
    rows / slides (never-erase). Returns (prints, oversize_idx)."""
    usable_w = slide_w - 2 * margin
    usable_h = slide_h - 2 * margin
    oversize = sorted(i for i in range(len(sizes))
                      if sizes[i] > usable_w + 1e-9 or sizes[i] > usable_h + 1e-9)
    skip = set(oversize)
    order_idx = sorted((i for i in range(len(sizes)) if i not in skip),
                       key=lambda i: (conds[i][0], conds[i][1]))

    def fresh():
        return {"base_xy": (margin, margin) if use_base else None, "squares": []}

    if not order_idx:
        return [fresh()], oversize

    want = grid_cols if grid_cols and grid_cols > 0 else 0

    def slots_for(cell):
        """(pitch, usable [(c, r), ...]) for a cell size -- base cells removed."""
        pitch = cell + spacing
        fit_cols = max(1, int((usable_w + spacing) / pitch + 1e-9))
        fit_rows = max(1, int((usable_h + spacing) / pitch + 1e-9))
        cols = (min(want, fit_cols) if want
                else min(max(1, int(math.ceil(math.sqrt(len(order_idx))))),
                         fit_cols))
        cols = max(1, cols)

        def hits_base(c, r):
            if not use_base:
                return False
            x0, y0 = margin + c * pitch, margin + r * pitch
            return x0 < margin + base_w - 1e-9 and y0 < margin + base_h - 1e-9

        slots = [(c, r) for r in range(fit_rows) for c in range(cols)
                 if not hits_base(c, r)]
        return pitch, (slots or [(0, 0)])

    prints = []
    k, n = 0, len(order_idx)
    while k < n:
        # grow the run while it still fits at its own (non-decreasing) cell size
        m, fit = k, None
        while m < n:
            pitch, slots = slots_for(sizes[order_idx[m]])
            if (m - k + 1) > len(slots):
                break
            fit = (sizes[order_idx[m]], pitch, slots)
            m += 1
        if fit is None:                                  # one square, no slot: force
            pitch, slots = slots_for(sizes[order_idx[k]])
            fit = (sizes[order_idx[k]], pitch, slots)
            m = k + 1
        cell, pitch, slots = fit
        pr = fresh()
        for j, i in enumerate(order_idx[k:m]):
            c, r = slots[j]
            off = (cell - sizes[i]) / 2.0                # centre in the cell
            pr["squares"].append(
                (i, margin + c * pitch + off, margin + r * pitch + off, sizes[i]))
        prints.append(pr)
        k = m
    return prints, oversize


def _sort_within(pr, conds, policy):
    """Sort ONE print's emit/visiting SEQUENCE in place for `policy` (positions
    stay fixed -- only the order the squares are visited changes):
      "slowest" -- eps ascending; "fastest" -- eps descending;
      "position" -- reading order, top row first then left->right;
      "size"/unknown -- leave as packed (the default, largest-first from FFD)."""
    if policy == "position":
        pr["squares"].sort(key=lambda sq: (-sq[2], sq[1]))
    elif policy in ("slowest", "fastest"):
        pr["squares"].sort(key=lambda sq: conds[sq[0]][0],
                           reverse=(policy == "fastest"))


def _apply_order(prints, conds, policy):
    """Reorder the emit / visiting SEQUENCE within and across slides WITHOUT
    moving any square (positions are fixed by the packer). Policies:
      "size"     -- leave as packed (the default; largest-first from the FFD pack);
      "slowest"  -- ascending eps within a file AND lowest-eps slide first (the old
                    `speed_order`: a fast-end jet break then loses the least data);
      "fastest"  -- descending eps, mirror of "slowest";
      "position" -- reading order, top row first then left->right.
    Empty prints (all-oversize) sort to the end and never raise."""
    for pr in prints:
        _sort_within(pr, conds, policy)
    if policy not in ("slowest", "fastest"):
        return                                           # no cross-slide re-order
    rev = policy == "fastest"

    def _key(pr):
        eps = [conds[sq[0]][0] for sq in pr["squares"]]
        if not eps:
            return float("-inf") if rev else float("inf")
        return max(eps) if rev else min(eps)

    prints.sort(key=_key, reverse=rev)


def generate_lag(p: LagParams) -> SweepResult:
    """The open-square jet-lag specimen set -> SweepResult."""
    _validate_plate(p, "lag squares")
    if p.cts <= 0:
        raise ValueError("CTS must be > 0 mm/s")
    if p.gap <= 0:
        raise ValueError("gap must be > 0 mm")
    # the script never blind-refuses an operator's eps: only a non-positive one
    # is impossible (F <= 0); at/below 1 the jet coils instead of lagging, so
    # keep it but say so
    eps_list = [e for e in p.eps if e > 0.0]
    if not eps_list:
        raise ValueError("need at least one eps ratio (e.g. 1.25 1.5 2 3 4 6)")
    warnings = []
    if len(eps_list) != len(p.eps):
        warnings.append("non-positive eps values were dropped (feed would be 0)")
    if any(e <= 1.0 for e in eps_list):
        warnings.append("eps <= 1 prints at/below CTS -- the jet coils there, "
                        "no steady lag to measure")
    if p.reps < 1:
        raise ValueError("reps must be >= 1")

    reps_eps = {round(k, 4): int(v) for k, v in (p.reps_eps or {}).items()}

    def reps_for(e):
        return reps_eps.get(round(e, 4), p.reps)

    travel_offset = p.travel_offset
    if travel_offset >= p.spacing / 2.0 and travel_offset > 0:
        clamped = round(max(0.0, p.spacing / 2.0 - 0.25), 3)
        warnings.append(f"travel clearance {travel_offset} >= spacing/2 "
                        f"({p.spacing / 2.0}); clamped to {clamped} mm so the "
                        f"between-square lanes stay open")
        travel_offset = clamped

    conds = [(e, r) for e in eps_list for r in range(reps_for(e))]
    sizes = [square_side(e, p.gap, p.safety, p.floor, p.fixed_side,
                         p.side_per_eps) for (e, _) in conds]

    stab_speed = p.stab_speed_eps * p.cts * 60.0
    if p.stab.enabled:
        bmnx, bmny, bmxx, bmxy = _measure_base_bbox(p.stab, p.diam, stab_speed)
        base_w, base_h = bmxx - bmnx, bmxy - bmny
    else:
        bmnx = bmny = base_w = base_h = 0.0
    layout = p.layout if p.layout in LAYOUTS else "shelf"
    if layout in ("columns", "rows"):
        # size-DESCENDING (first-fit-decreasing, ties broken eps-ascending) so the
        # dense 2-D pack is as tight as the FFD shelf -- a size-ASCENDING order
        # leaves a ragged frontier and costs a slide. Equal-size (= equal-eps)
        # squares stay adjacent, so each eps still reads as one contiguous block.
        order = sorted(range(len(sizes)),
                       key=lambda i: (-sizes[i], conds[i][0], conds[i][1]))
        prints, oversize_idx = _dense_pack(
            order, sizes, p.slide_w, p.slide_h, p.margin, p.spacing,
            base_w, base_h, p.stab.enabled, vertical=(layout == "columns"))
    elif layout == "grid":
        prints, oversize_idx = _pack_grid(
            conds, sizes, p.slide_w, p.slide_h, p.margin, p.spacing,
            base_w, base_h, p.stab.enabled, p.grid_cols)
    else:
        prints, oversize_idx = _pack_into_prints(
            sizes, p.slide_w, p.slide_h, p.margin, p.spacing, base_w, base_h,
            p.stab.enabled)

    # emit/visiting order determined BEFORE the pins: `order` supersedes;
    # `speed_order` stays as the "slowest" shortcut when order is left at its
    # default (back-compat with the presets + the existing test). Positions are
    # fixed -- only the sequence moves. Pins key to this ORDERED slide list (a
    # caller pins against the final displayed order), so ordering must
    # happen first and must NOT be re-run across slides after pins land.
    policy = p.order if p.order in ORDERS else "size"
    if policy == "size" and p.speed_order:
        policy = "slowest"
    _apply_order(prints, conds, policy)

    # manual placement overrides: pin the listed specimens to their slide +
    # collector-absolute corner (rescuing them from the auto-pack AND from
    # oversize); the rest still auto-lay per `layout`. An orphan label (its eps
    # was removed) is ignored, so an override survives a parameter change. The
    # pin's slide index is post-order, so only the WITHIN-slide emit sequence of
    # a slide that received a pin is re-sorted (per policy) -- never the whole
    # prints list, which would shift the index the pin refers to.
    if p.placements:
        label_cond = {lag_label(conds[i][0], conds[i][1],
                                 reps_for(conds[i][0])): i
                      for i in range(len(conds))}
        pinned = {}
        for lbl, pos in p.placements.items():
            i = label_cond.get(lbl)
            if i is None or not pos:
                continue                             # orphan / empty -> ignore
            try:
                pinned[i] = (int(pos[0]), float(pos[1]), float(pos[2]))
            except (TypeError, ValueError, IndexError):
                continue
        if pinned:
            oversize_idx = [i for i in oversize_idx if i not in pinned]
            for pr in prints:
                pr["squares"] = [sq for sq in pr["squares"]
                                 if sq[0] not in pinned]
            cap = len(prints) - 1 + len(pinned)      # bound a typo'd slide index
            pinned = {i: (max(0, min(sl, cap)), x, y)
                      for i, (sl, x, y) in pinned.items()}
            top = max(sl for sl, _x, _y in pinned.values())
            while len(prints) <= top:
                prints.append({"base_xy": (p.margin, p.margin)
                               if p.stab.enabled else None, "squares": []})
            touched = set()
            for i, (sl, x0a, y0a) in pinned.items():
                # placements are collector-ABSOLUTE; the packer/emit frame is
                # plate-local (emit adds the origin back), so subtract the origin
                prints[sl]["squares"].append(
                    (i, x0a - p.origin_x, y0a - p.origin_y, sizes[i]))
                touched.add(sl)
            # re-run ONLY the within-slide emit sequence for slides that gained a
            # pin (slowest/fastest sort the pin in by eps, position by reading
            # order; size leaves it appended). The cross-slide order + unpinned
            # slides stay exactly as _apply_order left them.
            for sl in touched:
                _sort_within(prints[sl], conds, policy)

    # never-erase: name the specimens too big for the usable plate (they are NOT
    # dropped from the design silently -- the rest still lays out, and the GUI
    # shows this explicit note instead of reverting to the last good geometry).
    # Quote the axis actually binding for each square (a landscape/portrait plate
    # can overrun width OR height first) and collapse the identical strings a
    # multi-rep oversize eps would otherwise repeat into one "(xN)" note.
    usable_w = p.slide_w - 2 * p.margin
    usable_h = p.slide_h - 2 * p.margin
    counts, order = {}, []
    for i in sorted(set(oversize_idx), key=lambda j: -sizes[j]):
        e, s = conds[i][0], sizes[i]
        over_w = s > usable_w + 1e-9
        over_h = s > usable_h + 1e-9
        if over_w and over_h:                # both overrun -> quote the tighter
            axis, cap = (("width", usable_w) if usable_w <= usable_h
                         else ("height", usable_h))
        elif over_w:
            axis, cap = "width", usable_w
        else:
            axis, cap = "height", usable_h
        msg = (f"eps {_fmt(e)} square ({s:.0f} mm) exceeds the usable plate "
               f"{axis} ({cap:.0f} mm) -- enlarge the plate, lower the safety "
               f"factor or drop this eps")
        if msg not in counts:
            order.append(msg)
        counts[msg] = counts.get(msg, 0) + 1
    oversize = [m + (f" (×{counts[m]})" if counts[m] > 1 else "")
                for m in order]

    # per-slide base placement offsets (0-based slide index -> (dx, dy)); a slide
    # missing an entry keeps its auto local corner (margin, margin)
    base_offsets = {}
    for k, v in (p.base_offsets or {}).items():
        try:
            base_offsets[int(k)] = (float(v[0]), float(v[1]))
        except (TypeError, ValueError, IndexError):
            continue

    ox, oy = p.origin_x, p.origin_y
    texts, rows, metas = [], [], []
    all_specimens = []
    for pi, pr in enumerate(prints, 1):
        lines = ["G21", "G90"]
        labels, specimens = [], []
        f_emitted = None
        base_end = None
        blx = bly = None
        base_bbox = None
        if p.stab.enabled and pr["base_xy"] is not None:
            blx, bly = pr["base_xy"]
            odx, ody = base_offsets.get(pi - 1, (0.0, 0.0))
            blx, bly = blx + odx, bly + ody
            base_bbox = (ox + blx, oy + bly, ox + blx + base_w, oy + bly + base_h)
            f_emitted = _emit_base(lines, ox + blx - bmnx, oy + bly - bmny,
                                   p.stab, p.diam, stab_speed)
            bx = by = None                       # where the base leaves the head
            for l in lines:
                mx = re.search(r"X(-?[\d.]+)", l)
                my = re.search(r"Y(-?[\d.]+)", l)
                if mx:
                    bx = float(mx.group(1))
                if my:
                    by = float(my.group(1))
            base_end = (bx, by)

        # exclusion zones the inter-square travel routes AROUND (absolute)
        obstacles = [(ox + sx, oy + sy, ox + sx + ss, oy + sy + ss)
                     for (_, sx, sy, ss) in pr["squares"]]
        if base_bbox is not None:
            obstacles.append(base_bbox)
        route = travel_offset > 0

        cur = base_end                           # head XY after the base
        from_square = False
        for (i, x0l, y0l, s) in pr["squares"]:
            e, r = conds[i]
            feed = e * p.cts * 60.0
            x0, y0 = ox + x0l, oy + y0l
            sx_mid = x0 + s / 2.0
            if cur is not None and route:
                cx_, cy_ = cur
                clear = ((cx_, cy_ - travel_offset) if from_square
                         else (ox + blx + base_w + travel_offset, cy_))
                lines.append(f"G0 X{_fmt(clear[0])} Y{_fmt(clear[1])}")
                pre = (sx_mid, y0 - travel_offset)
                for wx, wy in array_travel.plan_exit_travel(
                        clear, pre, obstacles, travel_offset):
                    lines.append(f"G0 X{_fmt(wx)} Y{_fmt(wy)}")
            for L in range(max(1, p.layers)):
                z = L * p.diam
                pts = _square_moves(x0, y0, s)
                lines.append(f"G0 X{_fmt(pts[0][0])} Y{_fmt(pts[0][1])} "
                             f"Z{_fmt(z)}")
                for (x, y) in pts[1:]:
                    w = f"G1 X{_fmt(x)} Y{_fmt(y)}"
                    if f_emitted != feed:
                        w += f" F{_fmt(feed)}"
                        f_emitted = feed
                    lines.append(w)
            cur = (sx_mid, y0)                   # squares end at bottom-mid S
            from_square = True
            label = lag_label(e, r, reps_for(e))
            labels.append(label)
            specimens.append(Specimen(
                label=label, value_text=f"ε {_fmt(e)}",
                center=(x0 + s / 2.0, y0 + s / 2.0), size=s,
                feed_mm_min=feed))
            rows.append(dict(
                print=pi, label=label, eps=e, v_mm_s=round(e * p.cts, 4),
                side_mm=s, gap_mm=p.gap, cts_mm_s=p.cts,
                lag_pred_mm=round(lag_mm(e, p.gap), 3),
                x0_mm=round(x0, 3), y0_mm=round(y0, 3),
                measure_corners="TR;TL;BL"))
        text = "\n".join(lines) + "\n"
        texts.append(text)
        metas.append(_print_meta(pi, text, labels, p, specimens, base_bbox))
        all_specimens.extend(specimens)

    for m in metas:
        if not m.on_slide:
            warnings.append(f"slide {m.index} exceeds the plate -- lower the "
                            f"safety factor or split the eps list")
    warnings += _feed_ceiling_warnings(all_specimens, p.max_feed)

    n_placed = sum(len(m.specimens) for m in metas)
    if p.fixed_side and p.fixed_side > 0.0:
        size_note = (f"fixed side {p.fixed_side:g} mm"
                     + (f" +{p.side_per_eps:g}/eps" if p.side_per_eps else "")
                     + " (gap-independent)")
    else:
        size_note = f"auto side (safety {p.safety:g}x lag @ gap {p.gap} mm)"
    summary = (f"Jet-lag squares: CTS {p.cts} mm/s, gap {p.gap} mm, "
               f"{size_note}, "
               f"{n_placed} squares ({len(eps_list)} eps x reps) across "
               f"{len(texts)} slide(s), base "
               f"{'ON' if p.stab.enabled else 'OFF'} @ "
               f"{p.stab_speed_eps:g}xCTS"
               + (", slowest-first" if p.speed_order else ""))
    return SweepResult("lag", texts, _lag_csv(rows), rows, metas, summary,
                       warnings, oversize=oversize)


def _lag_csv(rows):
    cols = ["print", "label", "eps", "v_mm_s", "side_mm", "gap_mm", "cts_mm_s",
            "lag_pred_mm", "x0_mm", "y0_mm", "corner", "measured_lag_mm",
            "notes"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\r\n")
    w.writeheader()
    for r in rows:
        for c in r["measure_corners"].split(";"):
            out = {k: r.get(k, "") for k in cols}
            out["corner"], out["measured_lag_mm"], out["notes"] = c, "", ""
            w.writerow(out)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# the jet-lag circles (the flat lag-AMPLITUDE calibration)
# --------------------------------------------------------------------------- #
def circle_radius(eps, gap, lr_target, amp_prior=mew_circle_fit.A_DEFAULT,
                  floor_mm=0.0):
    """The commanded radius for one speed ratio (mm), floored.

    Straight from `mew_circle_fit.commanded_radius`, WITH ITS CONSTANTS -- the
    sizing and the fit must not carry two copies of the lag law. The squares'
    `lag_mm` above keeps this module's own `_A`/`_B` because their sizing is a
    byte-identical port and must not move; those two constants agree with
    `mew_circle_fit`'s to ten digits but not to the last bit, and the circle
    inversion is where that would quietly become a wrong amplitude.

    R grows with the gap because the lag does, which is what makes the
    recovered amplitude's error budget a function of `lr_target` alone."""
    return max(floor_mm, mew_circle_fit.commanded_radius(
        eps, gap=gap, amp_prior=amp_prior, lr_target=lr_target))


def circle_lag_mm(eps, gap, amp_prior=mew_circle_fit.A_DEFAULT):
    """The straight-run lag L the radius rule sizes against (mm). It is the
    lr_target = 1 case of the radius rule, by definition, so it comes from the
    same call rather than from a second copy of the law."""
    return mew_circle_fit.commanded_radius(eps, gap=gap, amp_prior=amp_prior,
                                           lr_target=1.0)


def _circle_rows(p, warnings=None):
    """The resolved circle table: one `CircleSpec` per ratio in LAID order, with
    `radius_mm` filled in. The single place `specimens` and the
    `eps`/`reps`/`reps_eps`/`fixed_radius` path become the same thing, so the
    two layouts (chain and nested) cannot size a circle two ways.

    Raises exactly what `generate_circles` raised before it existed, in the same
    order, and appends the same ratio warnings to `warnings`."""
    if warnings is None:
        warnings = []
    if p.cts <= 0:
        raise ValueError("CTS must be > 0 mm/s")
    if p.gap <= 0:
        raise ValueError("gap must be > 0 mm")
    if p.specimens is None and p.reps < 1:
        raise ValueError("reps must be >= 1")
    if p.lr_target <= 0:
        raise ValueError("the lag/radius ratio must be > 0")

    if p.specimens is None:
        reps_eps = {round(k, 4): int(v) for k, v in (p.reps_eps or {}).items()}
        raw = [CircleSpec(eps=float(e),
                          reps=reps_eps.get(round(float(e), 4), p.reps))
               for e in p.eps]
    else:
        raw = list(p.specimens)
        if not raw:
            raise ValueError("the circle table is empty -- add a row")
        for s in raw:
            if int(s.reps) < 1:
                raise ValueError(f"row eps {_fmt(s.eps)}: reps must be >= 1")
            if s.radius_mm is not None and float(s.radius_mm) <= 0:
                raise ValueError(f"row eps {_fmt(s.eps)}: a hand-set radius "
                                 f"must be > 0 mm")
        keys = [round(float(s.eps), 4) for s in raw]
        if len(set(keys)) != len(keys):
            raise ValueError("two table rows carry the same speed ratio -- "
                             "they would share a specimen label. Use one row "
                             "with more reps")
        raw = [CircleSpec(eps=float(s.eps), reps=int(s.reps),
                          radius_mm=(None if s.radius_mm is None
                                     else float(s.radius_mm)))
               for s in raw]

    rows = [s for s in raw if s.eps > 1.0]
    if len(rows) != len(raw):
        warnings.append("speed ratios at or below 1 were dropped -- below CTS "
                        "the jet coils and lays no steady circle to measure")
    if not rows:
        raise ValueError("need at least one speed ratio above 1 (e.g. 2 3 4 6)")
    if any(s.eps < 2.0 for s in rows):
        warnings.append(
            "a ratio below 2 carries most of the believed CTS error into k "
            "(a 5% CTS error moves k by 11.6% at eps 1.5, 6.5% at 2, 3.8% at "
            "3) -- the low rungs cost precision rather than buying it")

    fixed = (p.specimens is None and p.fixed_radius and p.fixed_radius > 0.0)
    out = []
    for s in rows:
        if s.radius_mm is not None:
            R = max(0.1, float(s.radius_mm))
        elif fixed:
            R = max(0.1, p.fixed_radius)
        else:
            R = circle_radius(s.eps, p.gap, p.lr_target, p.amp_prior, p.floor)
        out.append(CircleSpec(eps=s.eps, reps=int(s.reps), radius_mm=R))
    return out


def circle_table(p: CircleParams) -> list:
    """The rows `generate_circles(p)` will lay -- one per speed ratio, in emit
    order, with `radius_mm` the radius it will actually command (mm).

    This is the GUI's table: show it, let the operator edit a rep count or a
    radius, hand the edited list back as `CircleParams.specimens`. Feeding
    `circle_table(p)` straight back in is therefore a fixed point -- the same
    plate, now with every radius pinned."""
    return _circle_rows(p)


def _spec_num(v):
    """A number that reads short and parses back EXACTLY (`repr` for the
    fractional ones, so a radius cannot lose a digit on the round trip)."""
    f = float(v)
    return str(int(f)) if f == int(f) else repr(f)


def specs_to_str(specs) -> str:
    """[CircleSpec] -> "2:2, 3:2, 4:2:12.5, 6:2" -- the one string the GUI
    persists. `eps:reps`, plus `:radius_mm` on a row whose radius is hand-set."""
    out = []
    for s in specs:
        t = f"{_spec_num(s.eps)}:{int(s.reps)}"
        if s.radius_mm is not None:
            t += f":{_spec_num(s.radius_mm)}"
        out.append(t)
    return ", ".join(out)


def specs_from_str(text) -> list:
    """"2:2, 3:2, 4:2:12.5, 6:2" -> [CircleSpec]. The inverse of `specs_to_str`.

    Rows are comma (or semicolon) separated; each is `eps`, `eps:reps` or
    `eps:reps:radius_mm`. Blank input is an empty table, which is the GUI's "no
    table" state.

    This RAISES on anything it cannot read -- a bad number, an empty row, a
    fourth field, eps <= 1, reps < 1, radius <= 0. It deliberately does NOT skip
    a bad token the way `parse_float_list` does: a silently dropped row is a
    specimen that quietly stops being printed, and the operator would find that
    out from the plate."""
    if isinstance(text, (list, tuple)):
        return specs_from_str(", ".join(str(t) for t in text))
    s = str(text).strip()
    if not s:
        return []
    out = []
    for raw in re.split(r"[,;]", s):
        tok = raw.strip()
        if not tok:
            raise ValueError(f"empty row in the circle table {s!r}")
        fields = [f.strip() for f in tok.split(":")]
        if len(fields) > 3:
            raise ValueError(f"row {tok!r} has {len(fields)} fields -- expected "
                             f"eps, eps:reps or eps:reps:radius")
        try:
            eps = float(fields[0])
            reps = int(float(fields[1])) if len(fields) > 1 and fields[1] else 2
            rad = (float(fields[2])
                   if len(fields) > 2 and fields[2] not in ("", "-") else None)
        except ValueError:
            raise ValueError(f"row {tok!r} is not eps[:reps[:radius]] in "
                             f"numbers") from None
        if eps <= 1.0:
            raise ValueError(f"row {tok!r}: the speed ratio must be > 1 (below "
                             f"CTS the jet coils and lays no circle)")
        if reps < 1:
            raise ValueError(f"row {tok!r}: reps must be >= 1")
        if rad is not None and rad <= 0:
            raise ValueError(f"row {tok!r}: the radius must be > 0 mm")
        out.append(CircleSpec(eps=eps, reps=reps, radius_mm=rad))
    return out


def _circle_size_note(p, radii):
    """The summary's one-phrase account of how the radii were chosen."""
    if p.specimens is not None:
        n_set = sum(1 for s in p.specimens if s.radius_mm is not None)
        return (f"per-row table, {n_set} of {len(p.specimens)} radii set by "
                f"hand ({_fmt(min(radii))}-{_fmt(max(radii))} mm)"
                if radii else "per-row table, no specimens")
    if p.fixed_radius and p.fixed_radius > 0.0:
        return f"fixed radius {p.fixed_radius:g} mm"
    if radii:
        return (f"R = lag/{p.lr_target:g} at A={p.amp_prior:g} "
                f"({_fmt(min(radii))}-{_fmt(max(radii))} mm)")
    return "no specimens"


def circle_label(eps, rep, reps):
    """The CSV / placement label for one circle (unique per specimen)."""
    return f"circ_eps{_fmt(eps)}" + (f"_r{rep + 1}" if reps > 1 else "")


def _circle_points(cx, cy, R, chord_tol, start_angle=-0.5 * math.pi):
    """One CCW lap as a polyline, starting AND ending at the tangent point at
    `start_angle` (the bottom of the circle by default), sampled to a
    `chord_tol` sagitta.

    A polyline rather than G2/G3 BY DEFAULT: what the file says is then exactly
    what is printed and what is measured, with no dependence on the consumer's
    arc discretisation -- the machine's, the parser's or the preview's. The
    `g2g3` option (`_emit_lap`) takes that dependence on for machines that
    interpolate arcs, and pays it back with `circle_arc_segments`: every
    consumer is told a density at least as fine as this polyline."""
    tol = max(1e-6, min(float(chord_tol), 0.5 * R))
    dtheta = 2.0 * math.acos(1.0 - tol / R)
    n = max(24, int(math.ceil(2.0 * math.pi / dtheta)))
    out = []
    for i in range(n + 1):
        th = start_angle + 2.0 * math.pi * i / n
        out.append((cx + R * math.cos(th), cy + R * math.sin(th)))
    return out


def _nested_rings(radii, leads):
    """One concentric STACK, in emit order: outermost ring first, working in.

    Returns [(index, R, phi, lead)] where `phi` is the tangent point's angle and `lead`
    the straight tangential run-up into it, both chosen so the whole stack can be laid
    WITHOUT ever crossing a ring:

      * A ring is entered along its own tangent line, backwards from the tangent point
        by `lead`. That line touches the ring once and otherwise lies OUTSIDE it, and
        every smaller ring is inside that one.
      * The run-up starts at radius sqrt(R^2 + lead^2). Clamping `lead` to
        0.95*sqrt(R_outer^2 - R^2) keeps that start INSIDE the ring just finished.
      * The connector between rings is then exactly RADIAL: `phi` advances by
        atan(lead/R) per ring, and a radial segment straddling no ring crosses nothing.

    """
    order = sorted(range(len(radii)), key=lambda i: -radii[i])
    out, phi, prev_R = [], -0.5 * math.pi, None
    for n, i in enumerate(order):
        R = radii[i]
        lead = leads[i]
        if prev_R is not None:
            cap = 0.95 * math.sqrt(max(0.0, prev_R * prev_R - R * R))
            lead = min(lead, cap)
            phi += math.atan2(lead, R)
        out.append((i, R, phi, lead))
        prev_R = R
    return out


def _stack_footprint(rings):
    """Radius of the disc one stack occupies -- the outermost ring plus its own
    run-up, which is the only part that reaches outside it."""
    if not rings:
        return 0.0
    _i, R, _phi, lead = rings[0]
    return math.hypot(R, lead)


def _circle_record(pi, e, rep, R, cx, cy, phi, p, reps, lag, warnings):
    """The label / preview badge / CSV row for one circle. Shared by both
    layouts so a column can never exist in one and not the other."""
    feed = e * p.cts * 60.0
    r_cp = mew_circle_fit.contact_radius(R, e, gap=p.gap, amp=p.amp_prior)
    if r_cp is None:
        warnings.append(
            f"eps {_fmt(e)} at R {R:.2f} mm has NO steady contact circle at "
            f"the prior amplitude -- the lag outruns the arc. Lower the "
            f"lag/radius ratio")
    label = circle_label(e, rep, reps)
    spec = Specimen(label=label, value_text=f"ε {_fmt(e)}", center=(cx, cy),
                    size=2 * R, feed_mm_min=feed)
    row = dict(print=pi, label=label, eps=e, v_mm_s=round(e * p.cts, 4),
               feed_mm_min=round(feed, 3), commanded_r_mm=round(R, 4),
               gap_mm=p.gap, cts_mm_s=p.cts, amp_prior=p.amp_prior,
               lr_target=round(p.lr_target, 4), lag_pred_mm=round(lag, 4),
               r_cp_pred_mm=("" if r_cp is None else round(r_cp, 4)),
               inset_pred_mm=("" if r_cp is None else round(R - r_cp, 4)),
               exclude_deg=p.exclude_deg,
               entry_deg=round(math.degrees(phi) % 360.0, 2),
               cx_mm=round(cx, 3), cy_mm=round(cy, 3))
    return label, spec, row

def _pack_rows(todo, radii, slide_w, slide_h, margin, spacing, leads, y0,
               first_left=None, first_min_h=0.0):
    """First-fit-decreasing rows from `y0` upward. Does NOT consume `todo`;
    returns [{'cells','y','h','right','band'}] where `band` is the vertical
    space the row occupies (above `h` only for a first row shortened to sit
    beside a taller stabilization base)."""
    left_todo = list(todo)
    rows, y = [], y0
    while left_todo:
        left = first_left if (not rows and first_left is not None) else margin
        row, x = [], left
        for i in list(left_todo):
            R = radii[i]
            nx = x + (spacing if row else leads[i]) + 2 * R
            if nx > slide_w - margin + 1e-9:
                continue                           # try the next size down
            row.append((i, nx - R, y + R, R))
            x = nx
        if not row:
            break
        row_h = 2 * max(c[3] for c in row)
        band = max(row_h, first_min_h) if not rows else row_h
        if y + band > slide_h - margin + 1e-9:
            break                                  # no vertical room left
        rows.append({"cells": row, "y": y, "h": row_h, "right": x,
                     "band": band})
        for c in row:
            left_todo.remove(c[0])
        y += band + spacing
    return rows


def _slide_candidates(todo, radii, slide_w, slide_h, margin, spacing, leads,
                      base_w, base_h, use_base):
    """Every way one slide could be laid out, as (rows, base_xy). The caller
    keeps whichever places the most circles.

    Four strategies, because no one of them wins everywhere and the base is big:
    its REAL bbox includes the serpentine turnaround balloons, so a nominal
    10 x 10 base is 20 x 20 on the plate -- a third of the usable width and half
    the usable height. Reserving the bottom-left corner unconditionally (what
    the lag squares do) costs whole slides here; so does refusing to."""
    rows = _pack_rows(todo, radii, slide_w, slide_h, margin, spacing, leads,
                      margin)
    if not use_base:
        return [(rows, None)]
    out = []
    # (a) the leftover width of a row already tall enough to hold it
    for rw in rows:
        if (rw["h"] >= base_h - 1e-9
                and rw["right"] + spacing + base_w <= slide_w - margin + 1e-9):
            out.append((rows, (rw["right"] + spacing, rw["y"])))
            break
    # (b) its own band above the rows
    if rows:
        top = rows[-1]["y"] + rows[-1]["band"] + spacing
        if top + base_h <= slide_h - margin + 1e-9:
            out.append((rows, (margin, top)))
    # (c) its own band BELOW them -- the one that lets two mid-size rows stack
    below = _pack_rows(todo, radii, slide_w, slide_h, margin, spacing, leads,
                       margin + base_h + spacing)
    if below:
        out.append((below, (margin, margin)))
    # (d) the bottom-left corner, first row shortened to make space
    beside = _pack_rows(todo, radii, slide_w, slide_h, margin, spacing, leads,
                        margin, first_left=margin + base_w + spacing,
                        first_min_h=base_h)
    if beside:
        out.append((beside, (margin, margin)))
    # never-erase: if the base cannot go anywhere, keep the specimens and let
    # the caller say the base did not fit rather than dropping the slide
    return out or [(rows, None)]


def _pack_circles(radii, slide_w, slide_h, margin, spacing, leads,
                  base_w, base_h, use_base):
    """Row pack onto slides. Every circle in a row is tangent to that row's
    BASELINE, which is what lets one straight line be the lead-in, the chain
    connector and the next lap's lead-in all at once: it touches each circle at
    one point and crosses none of them.

    Each slide is its own print file and so carries its own stabilization base;
    `_slide_candidates` decides where. Returns (prints, oversize) with prints =
    [{'base_xy', 'rows': [[(i, cx, cy, R)]]}] in plate-local coords; `base_xy`
    is None on a slide with no room for a base. Never-erase: a circle too big
    for the usable plate is returned in `oversize`, not dropped and not raised.
    """
    usable_w = slide_w - 2 * margin
    usable_h = slide_h - 2 * margin
    order = sorted(range(len(radii)), key=lambda i: -radii[i])
    oversize = [i for i in order
                if 2 * radii[i] > usable_w + 1e-9
                or 2 * radii[i] > usable_h + 1e-9]
    todo = [i for i in order if i not in oversize]

    prints = []
    while todo:
        best = None
        for rows, base_xy in _slide_candidates(
                todo, radii, slide_w, slide_h, margin, spacing, leads,
                base_w, base_h, use_base):
            n = sum(len(rw["cells"]) for rw in rows)
            if best is None or n > best[0]:
                best = (n, rows, base_xy)
        if best is None or not best[1]:
            break                         # nothing fits any more: stop, do not
        _n, rows, base_xy = best          # spin forever emitting empty slides
        for rw in rows:
            for c in rw["cells"]:
                todo.remove(c[0])
        prints.append({"base_xy": base_xy,
                       "rows": [rw["cells"] for rw in rows]})
    return prints, oversize


def _stack_approach(cx, cy, R, phi, tangent, margin):
    """A point on the ring's own tangent line, far enough back that it is
    outside the square of half-width `margin` about the stack centre. Scanned
    rather than solved: the closed form is four cases and this runs once per
    stack."""
    step = max(0.05, 0.01 * margin)
    s = 0.0
    while s < 4.0 * margin:
        px = cx + R * math.cos(phi) - s * tangent[0]
        py = cy + R * math.sin(phi) - s * tangent[1]
        if max(abs(px - cx), abs(py - cy)) >= margin:
            return (px, py)
        s += step
    return (cx + R * math.cos(phi) - 4.0 * margin * tangent[0],
            cy + R * math.sin(phi) - 4.0 * margin * tangent[1])


def _generate_circles_nested(p, eps_list, reps_for, radius_for,
                             warnings) -> SweepResult:
    """The circle ladder as concentric STACKS -- a target per replicate.

    The radii already differ per speed ratio, so the whole ladder nests inside a disc
    of the largest one instead of taking a row of its own. Two to three times the plate
    density, and what that buys is the eps-flatness check: its floor is the
    placement-rule ambiguity, which falls with the lag/radius ratio, and nesting is what
    makes a small ratio affordable. Replicates cannot nest with each other -- same
    ratio, same radius -- so it is one stack per rep.

    The crossing-free construction is `_nested_rings`; this function is the packing and
    the emission. A hand-set radius nests exactly the way a law-sized one does."""
    max_reps = max(reps_for(e) for e in eps_list)
    stacks = []                       # [[(eps, R, lag)]] ascending in R
    for rep in range(max_reps):
        rings = [(e, radius_for(e), circle_lag_mm(e, p.gap, p.amp_prior))
                 for e in eps_list if rep < reps_for(e)]
        if rings:
            stacks.append(sorted(rings, key=lambda t: t[1]))
    if not stacks:
        raise ValueError("no circles to lay")
    if any(len({round(R, 6) for _e, R, _L in st}) != len(st) for st in stacks):
        warnings.append(
            "two speed ratios have the SAME radius, so they cannot nest -- "
            "either give them different radii or use the chain layout")

    # never-erase, nested flavour: a stack too wide for the plate loses its
    # OUTERMOST rings (the big ones) until it fits, and each drop is reported.
    # Nesting couples the ladder -- one oversize ring would otherwise take the
    # whole stack with it, which is exactly the erasure the contract forbids.
    usable = min(p.slide_w, p.slide_h) - 2 * p.margin
    dropped = []
    geom, foot = [], []
    for st in stacks:
        st = list(st)
        while st:
            radii = [R for _e, R, _L in st]
            leads = [max(p.lead_min, p.lead_mult * L) for _e, _R, L in st]
            rings = _nested_rings(radii, leads)
            f = _stack_footprint(rings)
            if 2 * f <= usable + 1e-9 or len(st) == 1:
                break
            dropped.append((st[-1][0], 2 * f))
            st = st[:-1]
        radii = [R for _e, R, _L in st]
        leads = [max(p.lead_min, p.lead_mult * L) for _e, _R, L in st]
        rings = _nested_rings(radii, leads)
        geom.append([(st[i][0], R, phi, lead, st[i][2])
                     for (i, R, phi, lead) in rings])
        foot.append(_stack_footprint(rings))

    stab_speed = p.stab_speed_eps * p.cts * 60.0
    if p.stab.enabled:
        bmnx, bmny, bmxx, bmxy = _measure_base_bbox(p.stab, p.diam, stab_speed)
        base_w, base_h = bmxx - bmnx, bmxy - bmny
    else:
        bmnx = bmny = base_w = base_h = 0.0

    # A stack is a disc, so the row packer places it with no lead-in of its own
    # -- the run-up is already inside the footprint radius.
    prints, oversize_idx = _pack_circles(
        foot, p.slide_w, p.slide_h, p.margin, p.spacing, [0.0] * len(foot),
        base_w, base_h, p.stab.enabled)

    usable_w = p.slide_w - 2 * p.margin
    usable_h = p.slide_h - 2 * p.margin
    oversize = []
    seen = {}
    for e, d in dropped:
        seen[(round(e, 4), round(d))] = seen.get((round(e, 4), round(d)), 0) + 1
    for (e, d), n in seen.items():
        oversize.append(
            f"eps {_fmt(e)} was dropped from its nested stack -- with it the "
            f"stack is {d:.0f} mm across against a {usable:.0f} mm usable "
            f"plate. The rings inside it still print"
            + (f" (x{n})" if n > 1 else ""))
    for i in sorted(set(oversize_idx)):
        axis, cap = (("width", usable_w) if usable_w <= usable_h
                     else ("height", usable_h))
        oversize.append(
            f"a nested stack ({2 * foot[i]:.0f} mm across) exceeds the usable "
            f"plate {axis} ({cap:.0f} mm) -- raise the lag/radius ratio, "
            f"enlarge the plate or drop the top ratio")
    n_left = len(foot) - len(oversize_idx) - sum(
        len(row) for pr in prints for row in pr["rows"])
    if n_left > 0:
        oversize.append(f"{n_left} nested stack(s) did not fit any slide")

    travel_offset = p.travel_offset
    ox, oy = p.origin_x, p.origin_y
    texts, rows, metas, all_specimens = [], [], [], []
    for pi, pr in enumerate(prints, 1):
        lines = ["G21", "G90"]
        labels, specimens = [], []
        f_emitted = None
        base_bbox = None
        cur = None
        if p.stab.enabled and pr["base_xy"] is not None:
            blx, bly = pr["base_xy"]
            base_bbox = (ox + blx, oy + bly, ox + blx + base_w,
                         oy + bly + base_h)
            f_emitted = _emit_base(lines, ox + blx - bmnx, oy + bly - bmny,
                                   p.stab, p.diam, stab_speed, g2g3=p.g2g3)
            bx = by = None
            for l in lines:
                mx = re.search(r"X(-?[\d.]+)", l)
                my = re.search(r"Y(-?[\d.]+)", l)
                if mx:
                    bx = float(mx.group(1))
                if my:
                    by = float(my.group(1))
            cur = (bx, by)
        stacks_only = [(ox + cx - R, oy + cy - R, ox + cx + R, oy + cy + R)
                       for row in pr["rows"] for (_i, cx, cy, R) in row]
        obstacles = list(stacks_only)
        if base_bbox is not None:
            obstacles.append(base_bbox)
        from_base = base_bbox is not None
        from_stack = False

        for row in pr["rows"]:
            for (si, sxl, syl, sfoot) in row:
                cx, cy = ox + sxl, oy + syl
                rings = geom[si]
                _e0, R0, phi0, lead0, _L0 = rings[0]
                t0 = (-math.sin(phi0), math.cos(phi0))
                start = (cx + R0 * math.cos(phi0) - lead0 * t0[0],
                         cy + R0 * math.sin(phi0) - lead0 * t0[1])
                # AIM at a point outside the stack's obstacle BOX, not at the run-up
                # start: that start sits on the footprint radius, i.e. inside the box
                # the router is asked to avoid, and a router handed a target inside an
                # obstacle gives up and returns the direct line -- a fibre straight
                # across the rings. (The box, not the disc: a point outside the disc
                # can still be inside the square drawn round it.) Extending backwards
                # along the run-up's own tangent line gets there, and the run in from
                # there stays outside every ring by construction. It also stays clear
                # of the NEIGHBOURING stack, whose centre is footprint+spacing+
                # footprint away with clearance below spacing.
                aim = _stack_approach(cx, cy, R0, phi0, t0,
                                      sfoot + travel_offset)
                if cur is not None and travel_offset > 0:
                    # step clear of whatever the head is standing on, exactly as
                    # the chain layout does and for the same reason
                    if from_base:
                        clear = (base_bbox[2] + travel_offset, cur[1])
                        route_obs = stacks_only
                        lines.append(f"G0 X{_fmt(clear[0])} "
                                     f"Y{_fmt(clear[1])}")
                    elif from_stack:
                        clear = cur          # the retrace already left it clear
                        route_obs = obstacles
                    else:
                        clear = (cur[0], cur[1] - travel_offset)
                        route_obs = obstacles
                        lines.append(f"G0 X{_fmt(clear[0])} "
                                     f"Y{_fmt(clear[1])}")
                    for wx, wy in array_travel.plan_exit_travel(
                            clear, aim, route_obs, travel_offset):
                        lines.append(f"G0 X{_fmt(wx)} Y{_fmt(wy)}")
                lines.append(f"G0 X{_fmt(start[0])} Y{_fmt(start[1])} Z0")

                prev_exit = None
                trail = [aim, start]      # every point the head reached on its
                for (e, R, phi, lead, lag) in rings:   # way IN to this stack
                    feed = e * p.cts * 60.0
                    tx = (-math.sin(phi), math.cos(phi))
                    T = (cx + R * math.cos(phi), cy + R * math.sin(phi))
                    S = (T[0] - lead * tx[0], T[1] - lead * tx[1])

                    def g1(pt):
                        nonlocal f_emitted
                        w = f"G1 X{_fmt(pt[0])} Y{_fmt(pt[1])}"
                        if f_emitted != feed:
                            w += f" F{_fmt(feed)}"
                            f_emitted = feed
                        lines.append(w)

                    if prev_exit is not None:
                        trail.append(S)
                    for lay in range(max(1, p.layers)):
                        if lay:
                            lines.append(f"G0 X{_fmt(S[0])} Y{_fmt(S[1])} "
                                         f"Z{_fmt(lay * p.diam)}")
                        elif prev_exit is not None:
                            # the RADIAL connector into this ring's run-up, at
                            # THIS ring's feed: the lag arriving at the arc has
                            # to be the one the arc will hold
                            g1(S)
                        if p.g2g3:
                            g1(T)          # the run-up; the lap is four arcs
                            _emit_lap(lines, cx, cy, R, phi)
                        else:
                            for pt in _circle_points(cx, cy, R, p.chord_tol,
                                                     start_angle=phi):
                                g1(pt)
                    trail.append(T)
                    prev_exit = T
                    label, spec, rec = _circle_record(
                        pi, e, si, R, cx, cy, phi, p, len(stacks), lag,
                        warnings)
                    labels.append(label)
                    specimens.append(spec)
                    rows.append(rec)
                # Leave the way we came in. A nested stack has no clear
                # step-off: the innermost ring's exit is INSIDE every ring
                # outside it, and any straight line out crosses them -- the
                # first version stepped 1 mm down and routed from there, which
                # laid a fibre across three rings. Retracing the entry trail
                # cannot cross anything (each of its segments was already
                # established as clear on the way in) and only re-covers the
                # run-ups and radial connectors, which are not measured.
                for wx, wy in reversed(trail[:-1]):
                    lines.append(f"G0 X{_fmt(wx)} Y{_fmt(wy)}")
                cur = aim                     # outside the stack's own box
                from_base = False
                from_stack = True
        text = "\n".join(lines) + "\n"
        texts.append(text)
        metas.append(_print_meta(pi, text, labels, p, specimens, base_bbox))
        all_specimens.extend(specimens)

    # nesting is only safe while the DEPOSITED rings stay apart, and they are
    # closer than the commanded ones: the inset grows with the radius, so the
    # outer gaps shrink most. Measured on the prediction, not on the command.
    tight = 0.0
    for st in geom:
        dep = []
        for (e, R, _phi, _lead, _lag) in st:
            r_cp = mew_circle_fit.contact_radius(R, e, gap=p.gap,
                                                 amp=p.amp_prior)
            dep.append(R if r_cp is None else r_cp)
        dep.sort()
        for a, b in zip(dep, dep[1:]):
            tight = min(tight or (b - a), b - a)
    if tight and tight < max(20.0 * p.diam, 0.5):
        warnings.append(
            f"the closest pair of DEPOSITED rings is {tight:.2f} mm apart "
            f"({tight / p.diam:.0f} fibre diameters) -- the inset grows with "
            f"the radius, so nested rings crowd from the outside in. Lower the "
            f"lag/radius ratio, drop a rung, or use the chain layout")

    for m in metas:
        if not m.on_slide:
            warnings.append(f"slide {m.index} exceeds the plate -- raise the "
                            f"lag/radius ratio or split the ratio list")
    warnings += _feed_ceiling_warnings(all_specimens, p.max_feed)

    n_placed = sum(len(m.specimens) for m in metas)
    allR = [R for st in geom for (_e, R, _p, _l, _L) in st]
    size_note = _circle_size_note(p, allR)
    summary = (f"Lag circles, NESTED: CTS {p.cts} mm/s, gap {p.gap} mm, "
               f"{size_note}, {n_placed} circles in "
               f"{sum(len(m.specimens) and 1 or 0 for m in metas) and len(stacks)}"
               f" stack(s) across {len(texts)} slide(s), fit each arc past "
               f"{p.exclude_deg:g} deg from its own entry, base "
               f"{'ON' if p.stab.enabled else 'OFF'} @ "
               f"{p.stab_speed_eps:g}xCTS")
    return SweepResult("circle", _finish_texts(texts, p), _circle_csv(rows),
                       rows, metas, summary, warnings, oversize=oversize,
                       arc_segments=(circle_arc_segments(p.chord_tol, allR)
                                     if p.g2g3 else DEFAULT_ARC_SEGMENTS))


def generate_circles(p: CircleParams) -> SweepResult:
    """The circle specimens that calibrate the FLAT lag amplitude -> SweepResult.

    The protocol is the accompanying paper's.

    TWO WAYS TO SAY WHAT TO PRINT, and they do not mix. `p.specimens = None` (the
    default) is the ladder: `eps` x `reps`, `reps_eps` per ratio, `fixed_radius` over
    all of them. A `p.specimens` list is instead the whole truth, and `eps`, `reps`,
    `reps_eps` and `fixed_radius` are IGNORED -- no merge, deliberately, because a
    table laid out by hand must not be re-sized by a global dial nobody can see.
    `circle_table(p)` resolves either form to the same rows."""
    _validate_plate(p, "lag circles")
    warnings = []
    # ONE resolution for both layouts: the ratio list, the rep counts and the
    # radii, whether they came from `eps`/`reps`/`reps_eps`/`fixed_radius` or
    # from a hand-edited `specimens` table. It carries the raises and the ratio
    # warnings, so they are not written out here.
    table = _circle_rows(p, warnings)
    if p.lr_target > 0.45:
        warnings.append(
            f"lag/radius {p.lr_target:g} is above 0.45: the recovered k becomes "
            f"conditional on which placement rule is inverted (about "
            f"{100 * 0.036 * (p.lr_target / 0.35) ** 2:.0f}% between the "
            f"shipped ray rule and the retired filter). Smaller circles are "
            f"cheaper on the plate and worse on the model")

    eps_list = [s.eps for s in table]
    by_eps = {round(s.eps, 4): s for s in table}

    def reps_for(e):
        return by_eps[round(e, 4)].reps

    def radius_for(e):
        return by_eps[round(e, 4)].radius_mm

    if p.layout == "nested" and len(eps_list) > 1:
        return _generate_circles_nested(p, eps_list, reps_for, radius_for,
                                        warnings)

    conds = [(e, r) for e in eps_list for r in range(reps_for(e))]
    radii = [radius_for(e) for (e, _) in conds]
    lags = [circle_lag_mm(e, p.gap, p.amp_prior) for (e, _) in conds]
    leads = [max(p.lead_min, p.lead_mult * L) for L in lags]

    stab_speed = p.stab_speed_eps * p.cts * 60.0
    if p.stab.enabled:
        bmnx, bmny, bmxx, bmxy = _measure_base_bbox(p.stab, p.diam, stab_speed)
        base_w, base_h = bmxx - bmnx, bmxy - bmny
    else:
        bmnx = bmny = base_w = base_h = 0.0

    travel_offset = p.travel_offset
    if travel_offset >= p.spacing / 2.0 and travel_offset > 0:
        clamped = round(max(0.0, p.spacing / 2.0 - 0.25), 3)
        warnings.append(f"travel clearance {travel_offset} >= spacing/2 "
                        f"({p.spacing / 2.0}); clamped to {clamped} mm so the "
                        f"between-circle lanes stay open")
        travel_offset = clamped

    prints, oversize_idx = _pack_circles(
        radii, p.slide_w, p.slide_h, p.margin, p.spacing, leads,
        base_w, base_h, p.stab.enabled)

    usable_w = p.slide_w - 2 * p.margin
    usable_h = p.slide_h - 2 * p.margin
    counts, order = {}, []
    for i in sorted(set(oversize_idx), key=lambda j: -radii[j]):
        e, d = conds[i][0], 2 * radii[i]
        axis, cap = (("width", usable_w) if usable_w <= usable_h
                     else ("height", usable_h))
        msg = (f"eps {_fmt(e)} circle ({d:.0f} mm across) exceeds the usable "
               f"plate {axis} ({cap:.0f} mm) -- raise the lag/radius ratio, "
               f"enlarge the plate or drop this ratio")
        if msg not in counts:
            order.append(msg)
        counts[msg] = counts.get(msg, 0) + 1
    oversize = [m + (f" (x{counts[m]})" if counts[m] > 1 else "")
                for m in order]
    n_left = len(conds) - len(oversize_idx) - sum(
        len(row) for pr in prints for row in pr["rows"])
    if n_left > 0:
        oversize.append(
            f"{n_left} circle(s) did not fit any slide even after splitting -- "
            f"the lead-in plus the diameter overruns the usable plate width")

    ox, oy = p.origin_x, p.origin_y
    texts, rows, metas = [], [], []
    all_specimens = []
    for pi, pr in enumerate(prints, 1):
        lines = ["G21", "G90"]
        labels, specimens = [], []
        f_emitted = None
        base_bbox = None
        cur = None
        if p.stab.enabled and pr["base_xy"] is not None:
            blx, bly = pr["base_xy"]
            base_bbox = (ox + blx, oy + bly, ox + blx + base_w,
                         oy + bly + base_h)
            f_emitted = _emit_base(lines, ox + blx - bmnx, oy + bly - bmny,
                                   p.stab, p.diam, stab_speed, g2g3=p.g2g3)
            bx = by = None
            for l in lines:
                mx = re.search(r"X(-?[\d.]+)", l)
                my = re.search(r"Y(-?[\d.]+)", l)
                if mx:
                    bx = float(mx.group(1))
                if my:
                    by = float(my.group(1))
            cur = (bx, by)

        circles_only = [(ox + cx - R, oy + cy - R, ox + cx + R, oy + cy + R)
                        for row in pr["rows"] for (_i, cx, cy, R) in row]
        obstacles = list(circles_only)
        if base_bbox is not None:
            obstacles.append(base_bbox)
        from_base = base_bbox is not None

        for row in pr["rows"]:
            i0, cx0, cy0, R0 = row[0]
            y0 = oy + cy0 - R0                     # the row's shared baseline
            x_lead = ox + cx0 - R0 - leads[i0]
            # get onto the baseline clear of everything already laid
            if cur is not None and travel_offset > 0:
                # Step CLEAR of whatever the head is standing on before asking
                # for a route: a lap ends on its own circle's boundary and the
                # base ends inside the base, and `plan_exit_travel` plans
                # between free points -- handed a start on an obstacle it
                # returns the direct line, which is a fibre laid across the
                # specimens. The lag squares do the same thing for the same
                # reason. Below the baseline is always free: every circle in a
                # row is tangent to it from ABOVE.
                if from_base:
                    clear = (base_bbox[2] + travel_offset, cur[1])
                    route_obs = circles_only     # crossing the sacrificial base
                else:                            # is fine; crossing a specimen
                    clear = (cur[0], cur[1] - travel_offset)
                    route_obs = obstacles
                lines.append(f"G0 X{_fmt(clear[0])} Y{_fmt(clear[1])}")
                way = list(array_travel.plan_exit_travel(
                    clear, (x_lead, y0), route_obs, travel_offset))
                if way and abs(way[-1][0] - x_lead) < 1e-9 \
                        and abs(way[-1][1] - y0) < 1e-9:
                    way.pop()                    # the Z0 line below IS that move
                for wx, wy in way:
                    lines.append(f"G0 X{_fmt(wx)} Y{_fmt(wy)}")
            lines.append(f"G0 X{_fmt(x_lead)} Y{_fmt(y0)} Z0")
            for (i, cxl, cyl, R) in row:
                e, r = conds[i]
                feed = e * p.cts * 60.0
                cx, cy = ox + cxl, oy + cyl
                for lay in range(max(1, p.layers)):
                    z = lay * p.diam
                    if lay:                        # re-enter for the next lap
                        lines.append(f"G0 X{_fmt(cx - R - leads[i])} "
                                     f"Y{_fmt(y0)} Z{_fmt(z)}")
                    # The straight run up to the tangent point IS the lead-in
                    # (and, between circles, the previous lap's exit), so it is
                    # emitted at THIS circle's feed: the lag arriving at the arc
                    # has to be the one the arc will hold.
                    pts = _circle_points(cx, cy, R, p.chord_tol)
                    for (x, y) in (pts[:1] if p.g2g3 else pts):
                        w = f"G1 X{_fmt(x)} Y{_fmt(y)}"
                        if f_emitted != feed:
                            w += f" F{_fmt(feed)}"
                            f_emitted = feed
                        lines.append(w)
                    if p.g2g3:                     # the lap itself, as arcs
                        _emit_lap(lines, cx, cy, R, -0.5 * math.pi)
                cur = (cx, y0)
                from_base = False
                label, spec, rec = _circle_record(
                    pi, e, r, R, cx, cy, -0.5 * math.pi, p, reps_for(e),
                    lags[i], warnings)
                labels.append(label)
                specimens.append(spec)
                rows.append(rec)
        text = "\n".join(lines) + "\n"
        texts.append(text)
        metas.append(_print_meta(pi, text, labels, p, specimens, base_bbox))
        all_specimens.extend(specimens)

    for m in metas:
        if not m.on_slide:
            warnings.append(f"slide {m.index} exceeds the plate -- raise the "
                            f"lag/radius ratio or split the ratio list")
    warnings += _feed_ceiling_warnings(all_specimens, p.max_feed)

    n_placed = sum(len(m.specimens) for m in metas)
    size_note = _circle_size_note(p, radii)
    summary = (f"Lag circles: CTS {p.cts} mm/s, gap {p.gap} mm, {size_note}, "
               f"{n_placed} circles ({len(eps_list)} ratios x reps) across "
               f"{len(texts)} slide(s), fit the arc past "
               f"{p.exclude_deg:g} deg, base "
               f"{'ON' if p.stab.enabled else 'OFF'} @ "
               f"{p.stab_speed_eps:g}xCTS")
    return SweepResult("circle", _finish_texts(texts, p), _circle_csv(rows),
                       rows, metas, summary, warnings, oversize=oversize,
                       arc_segments=(circle_arc_segments(p.chord_tol, radii)
                                     if p.g2g3 else DEFAULT_ARC_SEGMENTS))


# --------------------------------------------------------------------------- #
# the mandrel lag calibration TUBE (4-axis)
# --------------------------------------------------------------------------- #
#: Winding angles, degrees FROM THE MANDREL AXIS (0 = axial lines, 90 = rings),
#: that the tube prints by default: the paper's section 2.4.6 prescribes 20, 45 and
#: 70 plus 0 and 90 as checks, which is exactly `mew_lag_fit`'s split of fit rows
#: from null rows. The 0 deg check is left out of the DEFAULT, with the bands at
#: 13 mm and 1 mm apart (`LagTubeParams.band_len` / `band_gap`), so the default
#: tube is 48 mm long (`lag_tube_length`: a 1 mm lead-in, three 13 mm bands,
#: the 5 mm ring check, three 1 mm gaps) and fits the shipped 4-axis machine's
#: 50 mm rod; the paper's full 0-90 set at 20 mm needs about 80 mm. The check
#: is optional (the fit never needs it), so a user with a longer rod types 0
#: back into "Winding angles".
DEFAULT_TUBE_ANGLES = [20.0, 45.0, 70.0, 90.0]


@dataclasses.dataclass
class LagTubeParams:
    """The mandrel jet-lag calibration TUBE -- ONE 4-axis print, two readings.

    WHAT IT MEASURES (the paper's section 2.4.6). On a mandrel every fibre lands
    slightly to ONE SIDE of its programmed path, and the side flips with the
    direction of rotation [Neuhaus 2025]. So parallel lines printed back and
    forth come out with gaps alternating wide / narrow, and `D = (wide -
    narrow) / 4` needs no image scale. Bands at several winding angles give
    D(alpha), which `mew_lag_fit.fit_radii` inverts for R_ax and R_cir. The 0
    and 90 deg bands are CHECKS: the model expects no deviation there.

    NO CTS LADDER ON THE TUBE. The mandrel's CTS is the CTS (4-axis) tab's.

    Layout, along the axis from the tube's near end:

      [lead-in] [band a0] gap [band a1] gap ...

    * The LEAD-IN is one helix at the print speed, `settle_turns` turns over
      `band_gap` mm, so the jet is steady before the first band begins.
    * A BAND at 0 <= alpha < 90 is a meander of N parallel lines, each running
      the whole `band_len` axially and so turning `band_len * tan(alpha)` of
      arc. Neighbouring lines run in opposite directions and are joined by a
      short connector along the band's EDGE. `lines` None (the default) takes
      the EVEN count that closes once around the circumference, widening the
      perpendicular spacing from `spacing` to `2*pi*r*cos(alpha) / N` so the
      lines are evenly spread (`_tube_band_count`); every gap around the tube
      then alternates. A given `lines` lays exactly that many at exactly
      `spacing`, and warns when they need more than one turn (they overlap).
    * A band that ends at its NEAR edge leaves at its far edge without laying
      a stray fibre across itself by RETRACING line 0 in its own direction:
      the retrace lands on line 0 with the same deviation, so one line per
      band is two fibres tall and no gap changes.
    * A 90 deg band is RINGS of alternating rotation, stepped axially by
      exactly `spacing`: as many as fit its length (three at least), or
      `lines`.
    * Between bands, a pure axial move across `band_gap` of bare rod.

    BAND LENGTH IS A PHYSICS REQUIREMENT, not a layout choice. After every
    turn at a band's edge the jet needs distance to settle back onto its
    steady deviation, and the middle of a short band reads LOW. Measured
    through the shipped touchdown relay on a 3 mm mandrel at 2x CTS, the
    mid-band deviation at 20 deg reached 0.81 of its steady value at 3.3 lag
    lengths of band, 0.96 at 6.2, 0.98 at 8 and 0.995 at 10.5; 45 deg was
    within 2% by 3.3 and 70 deg at every length tried. So `SETTLE_LAG_LENGTHS`
    = 8 is the rule for fit angles below `SETTLE_BELOW_DEG`, measured against
    `lag_mm` (the caller's predicted R_ax). `band_len` is the FITTED bands'
    length; the 0 and 90 deg CHECK bands have no deviation to settle to, so
    they are a short fixed `CHECK_BAND_LEN`, with no knob.

    THE ROD IS NOT SHRUNK TO FIT. A tube longer than `rod` is laid as designed
    and warned about, with the length it needs; the user shortens the bands or
    fits a longer rod.

    CONVENTIONS are the mandrel designer's (`scaffold_gcode.
    generate_mandrel_gcode`): G21/G90, X + the rotary A in UNWRAPPED degrees,
    Z the one layer's height with the bare-surface datum `start_z` already in
    it, and F the RESULTANT SURFACE SPEED in mm/min, emitted on change. One
    layer only. `start_x` is the g-code X of the print's near end, already on
    the user's datum; placing it on the rod is the caller's job.

    `deviation` is an optional callable `angle_deg -> D (mm)`: the model's
    PREDICTION at this tube's conditions, used only to annotate the CSV and to
    warn when neighbours would touch. It never moves a coordinate.
    """
    mandrel_diameter: float = 3.0
    angles: list = dataclasses.field(
        default_factory=lambda: list(DEFAULT_TUBE_ANGLES))
    spacing: float = 1.0           # mm, perpendicular to the lines, requested
    lines: int = None              # lines per band; None = once around
    band_len: float = 13.0         # mm, axial length of each FITTED band
    band_gap: float = 1.0          # mm of bare rod between sections
    speed: float = 900.0           # mm/min, the surface print speed
    settle_turns: float = 2.0      # lead-in turns at the print speed
    diam: float = 0.015            # mm, fibre diameter (bookkeeping only)
    start_x: float = 0.0           # g-code X of the print's near end
    start_z: float = 0.0           # g-code Z of the (one) layer
    start_angle: float = 0.0       # g-code A the print starts at, degrees
    rod: tuple = None              # (x0, x1) g-code X of the rod ends, or None
    #: a RECORD, not an input: the axial datum re-base the caller has already
    #: folded into `start_x` and `rod`, which a player must undo. The emitter
    #: never reads it.
    datum_dx: float = 0.0
    exit_margin: float = 5.0       # mm past the nearer end; None/0 = no exit
    max_feed: float = None         # mm/min machine ceiling (warn), or None
    deviation: object = None       # angle_deg -> predicted D (mm), or None
    lag_mm: float = None           # predicted R_ax (mm), for the length check


#: How many predicted lag lengths (R_ax) of band a fit angle needs for the
#: middle of the band to read within ~2% of its steady deviation. Measured, not
#: chosen: see "BAND LENGTH" in `LagTubeParams`.
SETTLE_LAG_LENGTHS = 8.0

#: ...and only the SHALLOW fit angles need it. At 45 deg the middle was within
#: 2% at 3.3 lag lengths, the shortest band measured, and 70 deg at every
#: length; the slow settling is a property of lines that run mostly along the
#: axis, which is where the jet trails furthest.
SETTLE_BELOW_DEG = 45.0

#: A band this close to 0 or 90 deg is a CHECK band -- the same split
#: `mew_lag_fit.NULL_TOL_DEG` makes between fit rows and null rows.
_NULL_TOL_DEG = 2.0


#: mm, the axial length of a 0 or 90 deg CHECK band. Fixed, not a knob: a check
#: carries no fit and has no deviation to settle to, so it only has to show a
#: few clean lines -- six rings at the 1 mm default spacing, and its middle
#: well clear of the turns at its ends.
CHECK_BAND_LEN = 5.0


def _is_null_angle(alpha):
    return alpha < _NULL_TOL_DEG or alpha > 90.0 - _NULL_TOL_DEG


def _band_len(p, alpha):
    """The axial length a band at `alpha` is laid over: `CHECK_BAND_LEN` for a
    check band, the design's `band_len` for a fitted one."""
    return CHECK_BAND_LEN if _is_null_angle(alpha) else float(p.band_len)


def _tube_band_count(radius, alpha_deg, spacing):
    """(N, resolved perpendicular spacing) for a band at 0 <= alpha < 90 that
    closes once around the mandrel.

    N is the largest EVEN line count, at least 2, that closes exactly around
    the circumference WITHOUT bringing the lines closer than `spacing`. Rounded
    towards fewer lines, never to the nearest: the spacing is there so that
    neighbours never meet (paper section 2.4.6). The one exception is a
    circumference too short for two lines at `spacing`, where two is the floor
    alternation needs."""
    perim = 2.0 * math.pi * radius * math.cos(math.radians(alpha_deg))
    n = max(2, 2 * int(math.floor(perim / (2.0 * spacing) + 1e-9)))
    return n, perim / n


def _fit_to_rod(p, natural_len, fields, what):
    """`(p', note)`: `p` with `fields` scaled by ONE factor so the print fits
    `p.fit_len`, the room the rod leaves between its margins -- the largest
    factor that fits, found by bisection because a ring band's line count
    floors. `note` says what was shortened, or is None when nothing was.

    THE ROD IS FIXED; THE PRINT GIVES WAY. A ladder with more rungs, or a
    tube with more bands, does not grow the collector with it. Every length
    shrinks together, so every rung and every band keeps its share; nothing
    is dropped. `fit_len` None (no rod declared) lays the print as designed.
    Raises ValueError when the rod leaves no room at all."""
    room = getattr(p, "fit_len", None)
    bare = dataclasses.replace(p, fit_len=None)
    if room is None:
        return bare, None
    natural = natural_len(bare)
    if natural <= room + 1e-9:
        return bare, None
    if not room > 0.0:
        raise ValueError("the rod leaves no room for the %s: shorten the "
                         "end offset or use a longer rod" % what)

    def scaled(k):
        return dataclasses.replace(bare, **{f: getattr(bare, f) * k
                                            for f in fields})

    lo, hi = 0.0, 1.0
    for _ in range(60):
        k = 0.5 * (lo + hi)
        if natural_len(scaled(k)) <= room + 1e-9:
            lo = k
        else:
            hi = k
    if lo <= 1e-6:
        raise ValueError(
            "the rod leaves %.1f mm for the %s, too little even with its lines "
            "shortened to nothing: shorten the end offset, use a longer rod, "
            "or lay fewer rungs or bands" % (room, what))
    q = scaled(lo)
    # ONE sentence a user can act on: how much rod there is, and what the
    # print gave up to fit it. The designed length is in the fields already.
    names = {"band_len": "bands", "band_gap": "gaps",
             "line_len": "lines", "rung_gap": "rung gaps"}
    parts = ", ".join("%s %.1f mm instead of %g" % (
        names.get(f, f), getattr(q, f), getattr(bare, f)) for f in fields)
    note = ("The rod has %.0f mm free, so the %s was shrunk to fit: %s."
            % (room, what, parts))
    return q, note



def tube_band_layout(radius, alpha_deg, spacing, lines=None, band_len=None):
    """`(n, spacing_mm, closed)` for one band: how many lines it lays, at what
    perpendicular spacing, and whether they close once around the mandrel.

    THE one definition, read by the generator, the page's fit card and its
    band summary alike. `lines` None is the default: once around for an angled
    band (`_tube_band_count`), as many rings as fit `band_len` for 90 deg --
    three at least, so there is a wide and a narrow gap. A given `lines` is
    laid at exactly `spacing` and closes only by accident."""
    if alpha_deg >= 90.0:
        if lines:
            return int(lines), float(spacing), False
        n = max(3, int(math.floor(float(band_len) / spacing + 1e-9)) + 1)
        return n, float(spacing), False
    if not lines:
        n, s_res = _tube_band_count(radius, alpha_deg, spacing)
        return n, s_res, True
    return int(lines), float(spacing), False


def _ring_len(n, spacing):
    return (n - 1) * float(spacing)


def lag_tube_length(p: LagTubeParams) -> float:
    """The axial length `generate_lag_tube` will lay, in mm, WITHOUT laying it.

    The GUI has to place the tube on the rod before it generates it (the rod
    and the datum decide `start_x`), so it needs the length first. Never
    raises: an input the generator would refuse counts as zero here, and the
    generator then says what is wrong in its own words."""
    total = 0.0
    if p.settle_turns > 0.0:
        total += float(p.band_gap)
    angles = [float(a) for a in p.angles]
    for i, alpha in enumerate(angles):
        if i:
            total += float(p.band_gap)
        if alpha >= 90.0:
            if p.spacing > 0.0:
                n = tube_band_layout(0.0, alpha, p.spacing, p.lines,
                                     _band_len(p, alpha))[0]
                total += _ring_len(n, p.spacing)
        else:
            total += _band_len(p, alpha)
    return total


def generate_lag_tube(p: LagTubeParams) -> SweepResult:
    """The mandrel lag calibration tube -> SweepResult (ONE print file).

    Raises ValueError with a user-facing message on an impossible spec, as
    every generator here does; see `LagTubeParams` for the layout."""
    r = 0.5 * float(p.mandrel_diameter)
    if not r > 0.0:
        raise ValueError("the mandrel diameter must be positive")
    for name, val in (("line spacing", p.spacing), ("band length", p.band_len),
                      ("print speed", p.speed)):
        if not (math.isfinite(val) and val > 0.0):
            raise ValueError(f"the {name} must be a positive number")
    if not (math.isfinite(p.band_gap) and p.band_gap >= 0.0):
        raise ValueError("the gap between bands cannot be negative")
    if p.lines is not None and int(p.lines) < 3:
        raise ValueError("lines per band must be at least 3, so each band has "
                         "one wide and one narrow gap; leave it blank for as "
                         "many as fit once around")
    angles = [float(a) for a in p.angles]
    if not angles:
        raise ValueError("give at least one winding angle")
    bad = [a for a in angles if not 0.0 <= a <= 90.0]
    if bad:
        raise ValueError("winding angles run from 0 (along the axis) to 90 "
                         "(a ring); got %s" % ", ".join("%g" % a for a in bad))
    if len(set(angles)) != len(angles):
        raise ValueError("each winding angle may appear only once")
    if not p.settle_turns >= 0.0:
        raise ValueError("the lead-in turns cannot be negative")

    def fmt(v):
        return scaffold_gcode.format_coord(v, 4)

    def a_of(v):
        return p.start_angle + math.degrees(v / r)

    circ = 2.0 * math.pi * r
    lines = ["G21", "G90"]
    f_emitted = p.speed
    # the approach carries F, as the designer's does: F is modal, and an
    # approach without it runs at whatever feed the machine last held
    lines.append("G0 X%s A%s Z%s F%s" % (fmt(p.start_x), fmt(a_of(0.0)),
                                         fmt(p.start_z), fmt(f_emitted)))
    u, v = 0.0, 0.0                      # unrolled, relative to the start

    def move(du, dv, feed):
        nonlocal u, v, f_emitted
        u, v = u + du, v + dv
        word = "G1 X%s A%s" % (fmt(p.start_x + u), fmt(a_of(v)))
        if feed != f_emitted:
            word += " F%s" % fmt(feed)
            f_emitted = feed
        lines.append(word)

    rows, specimens, labels, warnings, advisories = [], [], [], [], []
    short = []          # (angle, length) of fit bands too short to settle

    # -- lead-in, then the bands ----------------------------------------- #
    if p.settle_turns > 0.0:
        move(p.band_gap, p.settle_turns * circ, p.speed)
    for bi, alpha in enumerate(angles):
        if bi:
            move(p.band_gap, 0.0, p.speed)
        u0, v0 = u, v
        n, s_res, closed = tube_band_layout(r, alpha, p.spacing, p.lines,
                                            _band_len(p, alpha))
        if alpha >= 90.0:
            for k in range(n):
                if k:
                    move(p.spacing, 0.0, p.speed)
                move(0.0, circ if k % 2 == 0 else -circ, p.speed)
            length = _ring_len(n, p.spacing)
        else:
            length = _band_len(p, alpha)
            pitch_v = s_res / math.cos(math.radians(alpha))  # arc between lines
            rise = length * math.tan(math.radians(alpha))
            for k in range(n):
                if k:
                    move(0.0, pitch_v, p.speed)   # the edge connector
                sign = 1.0 if k % 2 == 0 else -1.0
                move(sign * length, sign * rise, p.speed)
            if n % 2 == 0:
                # back at the NEAR edge: along it to line 0's start (one turn
                # on), then line 0 again, in its own direction, out at the far
                # edge. A closed band is one pitch short of that turn.
                to_turn = math.ceil(((v - v0) + 1e-9) / circ) * circ
                move(0.0, v0 + to_turn - v, p.speed)
                move(length, rise, p.speed)
            if closed and abs(s_res / p.spacing - 1.0) > 0.25:
                advisories.append(
                    "%g°: %d lines close around this mandrel at %.2f mm "
                    "spacing, not the %.2f mm asked for"
                    % (alpha, n, s_res, p.spacing))
            if not closed and n * pitch_v > circ + 1e-9:
                warnings.append(
                    "%g°: %d lines at %.2f mm need %.1f mm around the "
                    "mandrel, more than its %.1f mm circumference, so lines "
                    "will overlap; lower the lines per band"
                    % (alpha, n, s_res, n * pitch_v, circ))
        null = _is_null_angle(alpha)
        if (not null and alpha < SETTLE_BELOW_DEG and p.lag_mm
                and length < SETTLE_LAG_LENGTHS * float(p.lag_mm) - 1e-9):
            short.append((alpha, length))
        label = "tube_a%s" % _fmt(alpha)
        labels.append(label)
        specimens.append(Specimen(
            label=label, value_text=("%g° check" if null else "%g°") % alpha,
            center=(p.start_x + 0.5 * (u0 + u), 0.0), size=u - u0,
            feed_mm_min=p.speed))
        pred = None
        if callable(p.deviation) and not null:
            try:
                pred = float(p.deviation(alpha))
            except Exception:  # noqa: BLE001 -- an annotation, never fatal
                pred = None
        rec = dict(label=label, section="meander", angle_deg=alpha,
                   check_band="yes" if null else "no", lines=n,
                   closed=closed, spacing_req_mm=p.spacing,
                   spacing_mm=round(s_res, 4), circ_mm=round(circ, 6),
                   v_start_mm=round(v0 + math.radians(p.start_angle) * r, 6),
                   feed_mm_min=p.speed,
                   x_start_mm=round(p.start_x + u0, 4),
                   x_end_mm=round(p.start_x + u0 + length, 4))
        if pred is not None:
            rec.update(pred_dev_um=round(pred * 1000.0, 1),
                       pred_wide_mm=round(s_res + 2.0 * pred, 4),
                       pred_narrow_mm=round(s_res - 2.0 * pred, 4))
            # Neuhaus's fibres merged once D reached half the spacing; warn
            # well before, while the narrow gap is still half its programmed
            # width, so there is room for the model to be wrong
            if pred >= 0.25 * s_res:
                warnings.append(
                    "%g°: at this speed the model expects the narrow gap to "
                    "be %.2f mm; below about %.2f mm (half the spacing) the "
                    "lines may touch, so widen the spacing"
                    % (alpha, max(0.0, s_res - 2.0 * pred), 0.5 * s_res))
        rows.append(rec)

    x_lo, x_hi = p.start_x, p.start_x + u
    scaffold_gcode._append_mandrel_exit(lines, x_lo, x_hi, p.exit_margin, fmt)
    text = "\n".join(lines) + "\n"

    on_rod = True
    if p.rod is not None:
        # 1 um, not 1e-9: a print placed flush on the rod end lands on it to
        # within rounding, and must not then be told it runs off it
        on_rod = (x_lo >= p.rod[0] - 1e-6 and x_hi <= p.rod[1] + 1e-6)
        if not on_rod:
            # how short the fitted bands may go before the shallow ones read
            # low, when the caller predicted a lag to measure it against
            floor = ""
            if p.lag_mm and any(not _is_null_angle(a) and a < SETTLE_BELOW_DEG
                                for a in angles):
                floor = (" Bands shorter than about %.0f mm make the shallow "
                         "angles read low."
                         % math.ceil(SETTLE_LAG_LENGTHS * float(p.lag_mm)))
            warnings.append(
                "This tube needs %.0f mm of rod, and the rod is %.0f mm long "
                "(the Machine page's 4-axis rod): shorten “Band length” or "
                "drop an angle.%s"
                % (x_hi - x_lo, p.rod[1] - p.rod[0], floor))
    meta = PrintMeta(index=1, n_items=len(labels), labels=labels,
                     bbox=(x_lo, 0.0, x_hi, 0.0), on_slide=on_rod,
                     specimens=specimens)
    warnings += _feed_ceiling_warnings(specimens, p.max_feed)
    # the shallow bands too short to settle, as ONE sentence however many
    if short:
        need = math.ceil(SETTLE_LAG_LENGTHS * float(p.lag_mm))
        names = " and ".join("%g°" % a for a, _l in short)
        warnings.insert(0, (
            "The %s band%s %s too short (%.1f mm) to show the full deviation, "
            "which needs about %.0f mm, so %s will measure low."
            % (names, "s" if len(short) > 1 else "",
               "are" if len(short) > 1 else "is", short[0][1], need,
               "they" if len(short) > 1 else "it")))
    fit_angles = [a for a in angles if not _is_null_angle(a)]
    if len(fit_angles) < 3:
        advisories.append(
            "only %d winding angle(s) away from 0° and 90°; the fit needs "
            "three" % len(fit_angles))

    summary = ("lag tube on a %g mm mandrel: %d band(s) at %s° @ %g mm/min, "
               "%.1f mm long"
               % (p.mandrel_diameter, len(angles),
                  " / ".join("%g" % a for a in angles), p.speed, x_hi - x_lo))
    return SweepResult("tube", [text], _tube_csv(rows), rows, [meta], summary,
                       warnings, [], advisories)


def tube_mid_gaps(paths_uv, row, dedupe=1e-6):
    """Where to measure one band, read off a laid (or relayed) tube: the lines'
    crossings of the band's MIDDLE and the gaps between neighbours there,
    perpendicular to the lines -- the operator's micrograph reading.

    `paths_uv` are the tube's UNROLLED paths, `(u, v[, h])` with u the g-code X
    and v the arc length, as `extract_moves(frame=)` returns them; `row` is one
    of `generate_lag_tube`'s band rows. An angled band is cut at its middle u,
    and its lines' v positions are the crossings, in print order from line 0
    (the retraced line 0 of an even band is dropped); a ring band is cut half
    a turn from where its rings start, and their u positions are. Crossings
    closer than `dedupe` mm are one line: the same point met twice, where a
    path is split or a vertex sits on the cut. It is a micron and not more on
    purpose. Where the model expects neighbours to overlap, the relay lays
    them a few microns apart, never across each other, and those are two
    lines that touch; a 10 um tolerance merged them and read an 8-line band
    as 4.

    Returns `dict(ring, at, cross, gaps, cos)`: `cross` the sorted crossing
    positions (v for an angled band, within [0, 2 * circ); u for rings),
    `gaps` a list of `(i, j,
    perpendicular_gap_mm)` between neighbouring crossings -- including the gap
    across v = 0 when the band closes around the mandrel -- and `cos` the
    factor from a crossing-coordinate gap to a perpendicular one."""
    alpha = float(row["angle_deg"])
    circ = float(row["circ_mm"])
    u0, u1 = float(row["x_start_mm"]), float(row["x_end_mm"])
    ring = alpha >= 90.0
    hits = []
    if ring:
        # half a turn from where the rings start, so no axial step between
        # two rings lies on the cut; half a spacing of slack in u, because a
        # relayed ring lands a little off its commanded u
        at = float(row.get("v_start_mm", 0.0)) + 0.5 * circ
        slack = 0.5 * float(row["spacing_mm"])
        for q in paths_uv:
            for a, b in zip(q[:-1], q[1:]):
                if not (min(a[0], b[0]) >= u0 - slack
                        and max(a[0], b[0]) <= u1 + slack):
                    continue
                lo, hi = sorted((a[1], b[1]))
                k = math.ceil((lo - at) / circ)
                while at + k * circ <= hi:
                    vv = at + k * circ
                    t = 0.0 if hi == lo else (vv - a[1]) / (b[1] - a[1])
                    if 0.0 <= t <= 1.0:
                        hits.append(float(a[0] + t * (b[0] - a[0])))
                    k += 1
        cosf = 1.0
        hits.sort()
    else:
        at = 0.5 * (u0 + u1)
        cosf = math.cos(math.radians(alpha))
        for q in paths_uv:
            for a, b in zip(q[:-1], q[1:]):
                if (a[0] - at) * (b[0] - at) > 0.0 or a[0] == b[0]:
                    continue
                t = (at - a[0]) / (b[0] - a[0])
                hits.append(float(a[1] + t * (b[1] - a[1])))
        # an even band's LAST crossing, in print order, is line 0 retraced:
        # it lands within a few microns of line 0, not on it, so it is
        # dropped by identity rather than by distance
        n = int(row.get("lines") or 0)
        if n and n % 2 == 0 and len(hits) == n + 1:
            hits = hits[:-1]
        # ordered from line 0, half a pitch before it, so the lines come in
        # print order around the mandrel and a band that does not close has
        # its one empty stretch LAST, where no gap is read across it
        pitch = float(row["spacing_mm"]) / cosf
        ref = (float(row.get("v_start_mm", 0.0))
               + (at - u0) * math.tan(math.radians(alpha)) - 0.5 * pitch)
        base = ref % circ
        hits = sorted(base + (h - ref) % circ for h in hits)
    cross = []
    for h in hits:
        if not cross or h - cross[-1] > dedupe:
            cross.append(h)
    if (not ring and len(cross) > 1
            and cross[0] + circ - cross[-1] <= dedupe):
        cross.pop()                          # the same line, across v = 0
    gaps = [(i, i + 1, (cross[i + 1] - cross[i]) * cosf)
            for i in range(len(cross) - 1)]
    if not ring and row.get("closed") and len(cross) > 1:
        gaps.append((len(cross) - 1, 0,
                     (cross[0] + circ - cross[-1]) * cosf))
    return dict(ring=ring, at=at, cross=cross, gaps=gaps, cos=cosf)


def _tube_csv(rows):
    cols = ["label", "angle_deg", "check_band", "lines", "spacing_mm",
            "print_speed_mm_min", "x_start_mm", "x_end_mm", "pred_wide_mm",
            "pred_narrow_mm", "wide_mm", "narrow_mm", "notes"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\r\n")
    w.writeheader()
    for rec in rows:
        out = {k: rec.get(k, "") for k in cols}
        out["print_speed_mm_min"] = rec.get("feed_mm_min", "")
        w.writerow(out)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# the CTS sweep on a MANDREL (4-axis) -- the CTS tab's other collector
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class CtsMandrelParams:
    """The CTS speed sweep laid on a rotating mandrel -- `generate_cts` with only
    the collector changed.

    SAME PROTOCOL AS THE PLATE (Hrynevich, Bas & Dalton, Small 2018): a ladder
    of feeds stepping DOWN from `start` to `stop`, `reps` replicate lines per
    feed, and CTS read as the slowest feed at which every replicate is
    straight. Each RUNG is a short meander of `reps` AXIAL lines along the top
    of the mandrel, `pitch` mm of arc apart, joined by pure rotations: the
    lines being judged are plain linear X moves at a known speed, and the
    turns between them are not read, so their feed does not matter.
    Every move of a rung carries its feed,
    which on a mandrel is the SURFACE speed, as everywhere in 4 axes.

    Rungs step along the axis, `rung_gap` apart, fastest at the near end. The
    run from one rung to the next never crosses a rung: after an odd number of
    lines the head is already at the far end and goes on through the gap; after
    an even number it turns half a revolution first and runs back along the
    UNDERSIDE, well away from the lines on top. Each rung starts at the top
    again (A a whole number of turns). Before rung 1 a settle helix at the
    start feed stands in for the plate's stabilization pad.

    Conventions are the lag tube's (`LagTubeParams`): unwrapped A, F = surface
    speed on change, one layer at `start_z`, `start_x` / `rod` / `datum_dx`
    already on the user's datum.
    """
    mandrel_diameter: float = 3.0
    start: float = 600.0           # fastest feed, mm/min
    stop: float = 240.0            # slowest feed, mm/min
    step: float = 60.0             # decrement, mm/min
    reps: int = 3                  # replicate lines per feed
    line_len: float = 10.0         # mm, axial length of each line
    pitch: float = 1.5             # mm of ARC between neighbouring lines
    rung_gap: float = 3.0          # mm axial between rungs
    settle_turns: float = 2.0      # turns at the start feed before rung 1
    diam: float = 0.015            # mm, fibre diameter (bookkeeping only)
    start_x: float = 0.0
    start_z: float = 0.0
    start_angle: float = 0.0
    rod: tuple = None
    datum_dx: float = 0.0          # a RECORD; see LagTubeParams
    exit_margin: float = 5.0
    max_feed: float = None
    fit_len: float = None          # mm of rod free for the print; None = as designed


#: the lengths a mandrel CTS ladder shrinks by to fit its rod (`_fit_to_rod`)
_CTS4_FIT_FIELDS = ("line_len", "rung_gap")


def cts_mandrel_length(p: CtsMandrelParams) -> float:
    """The axial length `generate_cts_mandrel` lays, without laying it -- after
    it has shrunk to `p.fit_len`, when one is set. Never raises; an impossible
    ladder counts as zero rungs."""
    try:
        p = _fit_to_rod(p, _cts_mandrel_natural, _CTS4_FIT_FIELDS,
                        "ladder")[0]
    except ValueError:
        p = dataclasses.replace(p, fit_len=None)
    return _cts_mandrel_natural(p)


def _cts_mandrel_natural(p):
    """`cts_mandrel_length` as designed, before any fit to the rod."""
    try:
        n = len(speed_ladder(p.start, p.stop, p.step))
    except ValueError:
        n = 0
    total = float(p.rung_gap) if p.settle_turns > 0.0 else 0.0
    if n:
        total += n * float(p.line_len) + (n - 1) * float(p.rung_gap)
    return total


def generate_cts_mandrel(p: CtsMandrelParams) -> SweepResult:
    """The mandrel CTS sweep -> SweepResult (ONE print file). See
    `CtsMandrelParams`; raises ValueError with a user-facing message."""
    p, fit_note = _fit_to_rod(p, _cts_mandrel_natural, _CTS4_FIT_FIELDS,
                              "ladder")
    r = 0.5 * float(p.mandrel_diameter)
    if not r > 0.0:
        raise ValueError("the mandrel diameter must be positive")
    feeds = speed_ladder(p.start, p.stop, p.step)
    reps = int(p.reps)
    if reps < 1:
        raise ValueError("each feed needs at least one line")
    for name, val in (("line length", p.line_len), ("line pitch", p.pitch)):
        if not (math.isfinite(val) and val > 0.0):
            raise ValueError(f"the {name} must be a positive number")
    if not (p.rung_gap >= 0.0 and p.settle_turns >= 0.0):
        raise ValueError("the rung gap and the settle turns cannot be negative")
    circ = 2.0 * math.pi * r
    warnings = []
    if (reps - 1) * p.pitch > 0.5 * circ:
        warnings.append(
            "the %d lines of a rung spread %.1f mm round a %.1f mm "
            "circumference, past the top half of the mandrel: lower the pitch "
            "or the replicates" % (reps, (reps - 1) * p.pitch, circ))

    def fmt(v):
        return scaffold_gcode.format_coord(v, 4)

    def a_of(v):
        return p.start_angle + math.degrees(v / r)

    lines = ["G21", "G90"]
    f_emitted = feeds[0]
    lines.append("G0 X%s A%s Z%s F%s" % (fmt(p.start_x), fmt(a_of(0.0)),
                                         fmt(p.start_z), fmt(f_emitted)))
    u, v = 0.0, 0.0

    def move(du, dv, feed):
        nonlocal u, v, f_emitted
        u, v = u + du, v + dv
        word = "G1 X%s A%s" % (fmt(p.start_x + u), fmt(a_of(v)))
        if feed != f_emitted:
            word += " F%s" % fmt(feed)
            f_emitted = feed
        lines.append(word)

    def next_top():
        """v of the next top (a whole turn), strictly ahead of where we are."""
        return circ * (math.floor(v / circ + 1e-9) + 1.0)

    if p.settle_turns > 0.0:
        move(p.rung_gap, p.settle_turns * circ, feeds[0])
    rows, specimens, labels = [], [], []
    for i, f in enumerate(feeds):
        u0 = u
        for j in range(reps):
            if j:
                move(0.0, p.pitch, f)                 # the turn: not read
            move(p.line_len if j % 2 == 0 else -p.line_len, 0.0, f)
        label = "cts4_%02d_%s" % (i + 1, _fmt(f))
        labels.append(label)
        specimens.append(Specimen(label=label, value_text="%g" % f,
                                  center=(p.start_x + u0 + 0.5 * p.line_len,
                                          0.0),
                                  size=p.line_len, feed_mm_min=f))
        rows.append(dict(label=label, rung=i + 1, feed_mm_min=f,
                         v_mm_s=round(f / 60.0, 4), reps=reps,
                         x_start_mm=round(p.start_x + u0, 4),
                         x_end_mm=round(p.start_x + u0 + p.line_len, 4)))
        if i + 1 == len(feeds):
            break
        nf = feeds[i + 1]
        if reps % 2 == 0:
            # back at the near end: turn to the underside, run past the rung
            move(0.0, 0.5 * circ, nf)
            move(p.line_len + p.rung_gap, 0.0, nf)
            move(0.0, next_top() - v, nf)
        else:
            move(p.rung_gap, next_top() - v, nf)
    x_lo = p.start_x
    x_hi = p.start_x + max(u, max((rw["x_end_mm"] - p.start_x
                                   for rw in rows), default=u))
    scaffold_gcode._append_mandrel_exit(lines, x_lo, x_hi, p.exit_margin, fmt)
    text = "\n".join(lines) + "\n"
    on_rod = True
    if p.rod is not None:
        # 1 um, not 1e-9: a print shrunk to fit lands on the rod end to
        # within rounding, and must not then be told it runs off it
        on_rod = (x_lo >= p.rod[0] - 1e-6 and x_hi <= p.rod[1] + 1e-6)
        if not on_rod:
            warnings.append(
                "the sweep runs %.1f mm (X %.1f to %.1f) and does not fit the "
                "rod (X %.1f to %.1f): shorten the lines or the ladder"
                % (x_hi - x_lo, x_lo, x_hi, p.rod[0], p.rod[1]))
    meta = PrintMeta(index=1, n_items=len(labels), labels=labels,
                     bbox=(x_lo, 0.0, x_hi, 0.0), on_slide=on_rod,
                     specimens=specimens)
    warnings += _feed_ceiling_warnings(specimens, p.max_feed)
    if fit_note:
        warnings.insert(0, fit_note)
    summary = ("CTS sweep on a %g mm mandrel: %g → %g mm/min, step %g, %d "
               "axial line(s) of %g mm per feed, %.1f mm long"
               % (p.mandrel_diameter, feeds[0], feeds[-1], p.step, reps,
                  p.line_len, x_hi - x_lo))
    return SweepResult("cts4", [text], _cts4_csv(rows), rows, [meta], summary,
                       warnings)


def _cts4_csv(rows):
    cols = ["label", "rung", "feed_mm_min", "v_mm_s", "reps", "x_start_mm",
            "x_end_mm", "all_straight", "notes"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\r\n")
    w.writeheader()
    for rec in rows:
        w.writerow({k: rec.get(k, "") for k in cols})
    return buf.getvalue()


#: kind -> the NAME of its generator. The ONE dispatch: the GUI drives this from
#: two places (the debounced preview worker and the synchronous Generate path),
#: and a two-way `if kind == "lag" else cts` at either would let a third kind
#: fall through to the CTS generator and raise on a missing field.
#:
#: It maps to a NAME, not to the function object, and `generate` looks the name
#: up at CALL time. Holding the object would freeze it at import: the sweeps GUI
#: suite drives the worker's coalescing by swapping `mew_sweeps.generate_cts`
#: for a blocking stand-in, and a frozen table silently ignores that -- the
#: worker keeps calling the real generator and the test can no longer see the
#: pipeline it is asserting about. It went red exactly that way.
#: "visc" and "diam" deliberately share one generator: the viscosity plate IS a
#: one-rung temperature ladder, and which rungs it carries is decided GUI-side by
#: the tab's own variables (gui_sweeps.py, the `kind in ("diam", "visc")` branch).
#: They are two kinds rather than one because the two tabs are recalibrated on
#: different clocks and must not share a design.
GENERATORS = {"cts": "generate_cts", "lag": "generate_lag",
              "sag": "generate_sag",
              "tube": "generate_lag_tube",
              "cts4": "generate_cts_mandrel",
              "circle": "generate_circles",
              "diam": "generate_diameter",
              "visc": "generate_diameter"}


def generate(kind, params) -> SweepResult:
    """Run the generator for `kind` ("cts" | "lag" | "circle" | "diam" | "visc")."""
    try:
        name = GENERATORS[kind]
    except KeyError:
        raise ValueError(f"unknown experiment kind {kind!r} -- expected one of "
                         f"{', '.join(sorted(GENERATORS))}") from None
    return globals()[name](params)


def _circle_csv(rows):
    cols = ["print", "label", "eps", "v_mm_s", "feed_mm_min",
            "commanded_r_mm", "gap_mm", "cts_mm_s", "amp_prior", "lr_target",
            "lag_pred_mm", "r_cp_pred_mm", "inset_pred_mm", "exclude_deg",
            "entry_deg", "cx_mm", "cy_mm", "measured_r_mm", "notes"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\r\n")
    w.writeheader()
    for r in rows:
        out = {k: r.get(k, "") for k in cols}
        out["measured_r_mm"] = out["notes"] = ""
        w.writerow(out)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# preview support: deposition + travel polylines from a generated file
# --------------------------------------------------------------------------- #
def extract_moves(gcode_text, arc_segments=8, with_times=False, frame=None):
    """One generated print file -> (depo_paths, travel_paths), each a list of float
    [N,3] point lists. With `with_times=True` returns (depo_paths, travel_paths,
    depo_times), the per-vertex deposition times the physics jet-lag relay
    (`mew_deposition.make_transform`) needs to reshape the commanded path into the
    predicted touchdown path.

    Deposition comes from the REAL converter parser (`gcode_to_fibers.parse_gcode`,
    mode "g1"), so arcs and modal state are exactly the export's. Travels come from a
    small modal tracker: each maximal run of G0 moves becomes one polyline -- the fibre
    a continuous-flow MEW rig REALLY lays on a repositioning move. The file's very
    first G0 starts a travel at its own endpoint, so no segment is invented.

    `frame` is a ROTARY `mew_frame.CylinderFrame` for a 4-axis file (the lag
    tube), None for a plate. Given one, every path comes back UNROLLED as
    `(u, v, h)` -- the parser's own reading of the A word -- and the travels are
    the parser's too, since the tracker below knows no A. Wrapping them onto the
    mandrel is the caller's job, after any relay, because the relay needs the
    frame and works on the unrolled path."""
    import gcode_to_fibers

    fd, tmp = tempfile.mkstemp(suffix=".gcode", prefix="mew_sweep_")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(gcode_text)
        if frame is not None:
            depo, depo_times, chains = gcode_to_fibers.parse_gcode(
                tmp, mode="g1", with_times=True, with_travels=True,
                frame=frame)
            travels = [c[0] for c in chains
                       if c is not None and len(c[0]) >= 2]
            if with_times:
                return depo, travels, depo_times
            return depo, travels
        parsed = gcode_to_fibers.parse_gcode(
            tmp, mode="g1", arc_segments=arc_segments, with_times=with_times)
        depo, depo_times = parsed if with_times else (parsed, None)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    # -- travel tracker ----------------------------------------------------- #
    travels = []
    cur = None                     # last known head position [x, y, z]
    run = None                     # the open travel polyline
    word = re.compile(r"([XYZ])(-?[\d.]+)")

    def _close():
        nonlocal run
        if run is not None and len(run) >= 2:
            travels.append(run)
        run = None

    for raw in gcode_text.splitlines():
        line = raw.strip().upper()
        if not line:
            continue
        cmd = line.split(None, 1)[0]
        if cmd not in ("G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03"):
            continue
        nxt = list(cur) if cur is not None else [0.0, 0.0, 0.0]
        seen = False
        for axis, val in word.findall(line):
            seen = True
            nxt["XYZ".index(axis)] = float(val)
        if not seen:
            continue
        if cmd in ("G0", "G00"):
            if run is None:
                run = [list(cur)] if cur is not None else []
            run.append(list(nxt))
        else:
            _close()
        cur = nxt
    _close()
    if with_times:
        return depo, travels, depo_times
    return depo, travels
