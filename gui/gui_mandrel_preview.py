#!/usr/bin/env python3
"""Collector-aware, modern-shell preview for the Mandrel tube designer.

This is deliberately smaller than :mod:`gui_preview`: a tube has one real
generator path, no planar array or approximate-preview alternative, and needs a
collector in the picture.  It nevertheless uses the same offscreen VTK/Tk
``PreviewPanel`` so its camera, rendering, and failure posture remain identical
to the Scaffold designer.  ``build_mandrel_preview`` is pure apart from its
caller-provided scratch g-code path, making the geometry seam independently
testable.
"""

import collections
import math

import numpy as np

#: Bytes of memory one drape sample costs (`preview_sag.drape_sag`'s own
#: estimate). Measured in the app on a 6 mm, 30 degree, 88-pivot tube: the
#: drape costs 1.0-1.4 KB per drawn point and its estimate runs 1.2-1.45x the
#: drawn count, so 1.4 KB a sample is the dear end. The default mandrel is
#: ~450k samples; a 20-layer graft of that tube is ~7.2M.
_DRAPE_BYTES_PER_SAMPLE = 1400
#: (the budget itself is `gui_preview.drape_budget_samples`, shared with the
#: flat scaffold preview, whose drape costs ~100 B a sample.)


def cell_design(*, family, diameter, target_length, cell_w, cell_h,
                fit="scale", rib_angle=30.0, amplitude=0.0,
                n_around=None, n_axial=None, graph=None):
    """Resolve one `mew_unit_cell.TubeCellDesign` from the page's raw fields.

    ONE place decides which family knob is passed, so the preview, the resolved
    readout, the validator and the worker argv cannot disagree about it --
    `mew_unit_cell._resolve_params` REJECTS an unknown knob, so handing
    `amplitude` to the re-entrant family is an error rather than a no-op.
    `UnitCellError` is left to propagate: the page shows its message verbatim.

    `n_around` / `n_axial` are the designer page's count-ladder pin, passed
    straight through: `None` (both, the default) is the engine's own rounding.
    """
    import mew_unit_cell

    if family == "reentrant":
        kw = {"rib_angle_deg": float(rib_angle)}
    elif family == "custom":
        # the drawing is passed as the string the editor holds; the engine
        # parses it, so the page and the worker cannot read it two ways.
        kw = {"graph": graph}
    else:
        kw = {"amplitude": float(amplitude)}
    return mew_unit_cell.design_tube_cells(
        family, float(diameter), float(target_length),
        float(cell_w), float(cell_h), fit=fit,
        n_around=n_around, n_axial=n_axial, **kw)


def collector_bounds(design):
    """The PRINTED axial interval ``(u0, u1)``, in the design's frame.

    THE ONE DEFINITION of how long the mandrel is and where it starts, for
    every consumer downstream of the designer: the preview's rod actor, the
    resolved readout, and the ``--mandrel-length`` the player is launched with.
    Before this existed each of those three spelled it out for itself and the
    player did not spell it out at all -- it was handed ``design.length`` --
    so a chevron's rod came out 2A short in the Viewer and the tube hung
    A off each end of the collector it is supposedly wound on (measured: 0.5 mm
    each end at the page's own defaults, print span 13.5664 mm on a 12.5664 mm
    rod).

    ``length`` is the cell TILING, not the extent.  A wavy ring waves like
    every other ring, so the print runs from ``-A`` to ``length + A``; ``A``
    is read off ``params["axial_overhang"]``, which the engine puts there for
    exactly this, and is never re-derived from the amplitude field (it is zero
    for a straight brick and for every helix, which is why one function covers
    both families).

    THE END MARGIN IS NOT IN HERE.  As an optional second argument it would
    make this function answer two different questions with one name: a margin is bare
    ROD, so adding it here reported the print as
    longer than it is.  `axial_layout` below is where the rod and the margin
    live; this stays the print.
    """
    over = float((getattr(design, "params", None) or {})
                 .get("axial_overhang", 0.0) or 0.0)
    return -over, float(design.length) + over


#: Where the print sits on the rod, as (key, what it means). The offset is a
#: DISTANCE FROM THAT END, so "left" and "right" are the same control read from
#: opposite ends and "centre" ignores it. This replaced an "end margin" that was
#: really "offset from the left, and also make the rod longer": once the rod has
#: its own length the two halves of that are different questions, and only one
#: of them is the print's.
PLACEMENTS = ("left", "centre", "right")

#: What `axial_layout` answers.  `rod_u0` / `rod_u1` bound the ROD and
#: `print_start_x` is the generator's `start_x` -- the axial coordinate of the
#: tube's near end -- all three in the COLLECTOR frame (before any g-code-origin
#: re-base).  `clearance` is (near, far): the bare rod left at each end, which
#: is what a caller reports and what a validator checks for negatives.
AxialLayout = collections.namedtuple(
    "AxialLayout", "rod_u0 rod_u1 print_start_x clearance")


def axial_layout(design, *, place="left", offset=0.0,
                 rod_length=None, rod_start=0.0, copies=1, copy_gap=0.0,
                 overhang=None):
    """Where the ROD is and where the PRINT sits on it -- the one definition.

    THE ROD IS A PLACE, NOT A CONSEQUENCE OF THE PRINT.  A mandrel is a real
    object in a real holder: `rod_length` (the Machine page's "Rod length") and
    `rod_start` (its near end along the axis) are its two facts, and the print
    goes on wherever there is room.  `rod_length=None` means no rotary stage has
    been declared, and the rod is then derived from the print -- printed span
    plus `offset` at each end -- so a page that never opens the machine tab is
    unchanged.

    `place` is WHICH END THE OFFSET IS MEASURED FROM: "left" (the rod's near, -X
    end), "right" (its far end) or "centre" (equal bare rod at both ends, offset
    ignored).  On a DERIVED rod all three land in the same place, because with no
    declared rod there is only one placement.  An "end margin" instead,
    which grew the rod at both ends and moved no emitted
    coordinate: zero stayed at the print's own near end, so zeroing the machine
    on the rod end would put the nozzle a margin from where the program thought it was.

    The wave overhang is folded in, because `start_x` is the tube's near end and
    a wavy family dips `A` BEFORE it (`collector_bounds`): offset the near end
    alone and the trough hangs off the rod, offset the trough and an offset of 0
    puts the extreme fiber exactly at the rod's edge, which is what 0 means.
    `clearance` may be NEGATIVE, deliberately -- a print that overhangs the rod
    is a real state the page reports as a refusal, so the arithmetic must be able
    to express it rather than clamping it out of sight.

    `copies` side-by-side tubes `copy_gap` apart (`scaffold_gcode.
    generate_mandrel_blocks_gcode`) are ONE print here: the span runs from
    the first copy's trough to the last one's crest, and `print_start_x` is
    still the first copy's near end. `overhang`, when given, is the widest
    wave of a tube of several blocks (`scaffold_gcode.tube_overhang`), in
    place of `design`'s own."""
    p0, p1 = collector_bounds(design)
    if overhang is not None:
        p0, p1 = -float(overhang), float(design.length) + float(overhang)
    if int(copies) > 1:
        p1 += (int(copies) - 1) * (p1 - p0 + float(copy_gap))
    d = float(offset or 0.0)
    span = p1 - p0
    rod_len = span + 2.0 * d if rod_length is None else float(rod_length)
    u0 = float(rod_start)
    if rod_length is None or place == "left":
        near = d
    elif place == "right":
        near = rod_len - span - d
    else:                                   # "centre"
        near = 0.5 * (rod_len - span)
    return AxialLayout(u0, u0 + rod_len, u0 + near - p0,
                       (near, rod_len - span - near))


def a_span_deg(gcode):
    """The largest rotary A word in `gcode`, i.e. where the mandrel ends up.

    Read off the emitted text rather than re-derived from the route, for the
    same reason `scaffold_gcode.main` reads it that way: the unwrapped A of a
    multi-layer program is the emitter's business, and a second derivation here
    would be a second opinion to keep in sync.
    """
    best = 0.0
    for line in gcode.splitlines():
        for word in line.split():
            if word.startswith("A"):
                try:
                    best = max(best, float(word[1:]))
                except ValueError:
                    pass
    return best


def build_mandrel_preview(*, gcode_path, diameter, target_length, angle,
                          fiber_diameter, layers, speed, eta=None, rm=0,
                          pivots=None, speed_mode="surface", sag_multiplier=1.0,
                          family="zigzag", cell_w=2.0, cell_h=4.18,
                          cell_fit="scale", rib_angle=30.0, amplitude=0.0,
                          graph=None,
                          cell_n_around=None, cell_n_axial=None,
                          physics=None, place="left", place_offset=0.0,
                          rod_length=None, rod_start=0.0,
                          fuse_law=None, fuse_band=None, gcode_text=None,
                          text_origin_u=0.0, blocks=None, layer_step=None, copies=1,
                          copy_gap=None, turn_out=0.0, on_toolpath=None,
                          should_stop=None):
    """Build the real wrapped paths for one closing tube preview.

    ``on_toolpath(paths, meta)``, when given, is called once the program is
    parsed, BEFORE the relay and the drape, with the commanded toolpath as the
    picture and ``meta["provisional"]`` True: the plate preview's toolpath-first
    picture (``gui_preview.build_preview_paths``), so a slow tube shows its new
    shape at once. It changes nothing about the paths this call returns.

    ``should_stop`` (None: never) is asked before the relay and handed to the
    drape; True raises ``preview_sag.BuildSuperseded``, because a newer edit
    has replaced this build and its picture would be discarded.

    ``text_origin_u`` is where on the rod that text's X 0 sits: a text
    re-based onto g-code zero (the G-code page's) is drawn back on the rod by
    it, so the tube stays where it is whichever end is zero. 0 for the
    generator's own program, which is already in rod coordinates.

    ``gcode_text``, when given, is drawn in place of the generator's program:
    the G-code page's picture of its own (possibly hand-edited) tube text. The
    design is still resolved from the other arguments, because the rod's radius
    and span are not in the text.

    The sequence is intentionally the export's own seam: X/A/Z g-code, the rotary
    ``CylinderFrame`` parse to unrolled surface paths, the physics touchdown relay
    (opt-in, ``physics``), the preview-only approximate drape, ``wrap_polyline``.
    ``meta["gcode_paths"]`` is the COMMANDED toolpath, always built, so the
    "Show the g-code path" box is a pure actor toggle.  It comes off the parse
    before relay and drape, wrapped the way the fiber is, so the on-screen gap
    between the two is exactly what those two stages moved.

    ``pivots`` (an even N, with ``eta`` None) is the end-turn closure of
    ``mew_tube.TurnedTubeDesign``, as ``--tube-pivots`` builds it.

    ``blocks`` (a list of ``scaffold_gcode.TubeBlock``, already resolved on
    the caller's thread) replaces the one-family tube with its layer blocks;
    the other arguments still resolve the base design, whose length every
    block shares. ``layer_step`` / ``copies`` / ``copy_gap`` are the
    emitter's own (`generate_mandrel_blocks_gcode`), and the rod covers every
    copy.

    ``family`` selects the emitter and ONLY the emitter -- a unit-cell tube and a
    McColl helix are one X/A/Z program to the parser.  ``place`` /
    ``place_offset`` / ``rod_length`` / ``rod_start`` go to ``axial_layout`` and
    reach the generator as ``start_x``, so the preview shows the export's offset.
    ``physics`` is None or the SAME options dict ``build_preview_paths`` takes;
    given one, the surface paths go through ``mew_deposition.make_transform``
    **with the rotary frame** -- the export's own call, and the frame is the whole
    of the 4-axis part, since without it the transform silently takes its flat
    branch.  With ``physics=None`` the output is byte-identical to before.

    COST on the desktop, this being a Tk-thread freeze: the relay is cheap
    (1.15 s, 10-ply re-entrant tube) but turns 2 890 commanded points into 88 210
    and ``preview_sag.drape_sag`` then takes 79.8 s against 2.25 s -- its periodic
    self-crossing pass, NOT density (77.9 s at 21 678 points).  DEFERRED;
    zigzag 0.25 s, brick 2.6 s, chevron 3.2 s."""
    import gcode_to_fibers
    import mew_frame
    import scaffold_gcode
    import time

    t0 = time.perf_counter()
    if family == "zigzag":
        d = scaffold_gcode.mandrel_design(diameter, angle, target_length,
                                          eta=eta, rm=rm, pivots=pivots)
    else:
        d = cell_design(family=family, diameter=diameter,
                        target_length=target_length, cell_w=cell_w,
                        cell_h=cell_h, fit=cell_fit, rib_angle=rib_angle,
                        amplitude=amplitude, n_around=cell_n_around,
                        n_axial=cell_n_axial, graph=graph)
    laid = blocks or [scaffold_gcode.TubeBlock(d, layers, turn_out=turn_out)]
    if copy_gap is None:
        copy_gap = scaffold_gcode.TUBE_COPY_GAP
    lay = axial_layout(d, place=place, offset=place_offset,
                       rod_length=rod_length, rod_start=rod_start,
                       copies=copies, copy_gap=copy_gap,
                       overhang=scaffold_gcode.tube_overhang(laid))
    if gcode_text is not None:
        gcode = gcode_text
    elif blocks is None and copies == 1 and layer_step is None:
        # the one-family tube, through the call it has always made
        if family == "zigzag":
            gcode = scaffold_gcode.generate_mandrel_gcode(
                diameter, target_length, angle, fiber_diameter, layers, speed,
                eta=eta, rm=rm, design=d, speed_mode=speed_mode,
                start_x=lay.print_start_x, turn_out=turn_out)
        else:
            gcode = scaffold_gcode.generate_mandrel_cell_gcode(
                diameter, d, fiber_diameter, layers, speed,
                speed_mode=speed_mode, start_x=lay.print_start_x)
    else:
        gcode = scaffold_gcode.generate_mandrel_blocks_gcode(
            diameter, laid, fiber_diameter, speed, layer_step=layer_step,
            speed_mode=speed_mode, start_x=lay.print_start_x, copies=copies,
            copy_gap=copy_gap)
    # A wavy ring bulges `amplitude` past the first and last cell edge, so the
    # drawn tube is longer than `d.length`; the camera envelope, the rod actor
    # and the player's collector all have to cover it or the end rings hang off
    # the collector they are wound on.  `collector_bounds` is the ONE place that
    # says so, for every family.
    # Two intervals, deliberately: `overhang` is the WAVE bulge and belongs to the
    # design, while the ROD may be longer still by the user's end margin. Keeping
    # them apart is what stops a margin being reported as axial overhang.
    overhang = scaffold_gcode.tube_overhang(laid)
    u0, u1 = lay.rod_u0, lay.rod_u1
    with open(gcode_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(gcode)
    frame = mew_frame.CylinderFrame(d.radius)
    # The clock and the rapids are parsed unconditionally: the relay needs the
    # clock, the overlay needs both.  Neither changes `unrolled` (the parser
    # reports travels alongside), so the drawn tube is what it always was.
    import gui_preview
    unrolled, ptimes, travels = gcode_to_fibers.parse_gcode(
        gcode_path, with_times=True, with_travels=True, frame=frame)
    if text_origin_u:
        # u is the axial coordinate in the unrolled frame (`to_world`'s
        # first), so one add puts the program back on the rod
        shift = np.array([float(text_origin_u), 0.0, 0.0])
        unrolled = [np.asarray(q, dtype=np.float64) + shift for q in unrolled]
        if travels is not None:
            travels = [None if tr is None else
                       (np.asarray(tr[0], dtype=np.float64) + shift,) + tuple(tr[1:])
                       for tr in travels]

    # The overlay, snapshotted HERE: after the parse, before the relay and the
    # drape.  That ordering is the feature -- what separates the two lines on
    # screen is exactly what those two stages move.
    commanded, commanded_t = (
        gcode_to_fibers.merge_travels(unrolled, ptimes, travels)
        if travels is not None else (unrolled, ptimes))
    wrapped_command = [mew_frame.wrap_path(frame, p, times=t)
                       for p, t in zip(commanded, commanded_t)]
    gcode_paths = [p for p, _t in wrapped_command]
    gcode_ptimes = [t for _p, t in wrapped_command]
    # The Design summary's numbers. The time is the parse's own clock -- the
    # modal-feed clock the player runs on -- because `estimate_print_time`
    # walks X/Y/Z only and a tube's moves are mostly A. The length is the
    # commanded path WRAPPED, so it is the fiber's length on the rod.
    print_time_s = 0.0
    for t in list(ptimes) + [tr[1] for tr in (travels or []) if tr is not None]:
        if len(t):
            print_time_s = max(print_time_s, float(t[-1]))
    fiber_length_mm = gui_preview.polyline_length(gcode_paths)
    a_max_deg = a_span_deg(gcode)

    def _meta(paths, point_times, provisional, n_raw=None, thin_bound=None,
              sag_refused=None):
        n_pts = sum(len(p) for p in paths)
        pts = np.concatenate(paths, axis=0)
        # Include the rod as well as the fiber in the camera envelope.  The
        # axial span is the resolved (not requested) closing length.
        lo = np.minimum(pts.min(axis=0),
                        np.array([u0, -d.radius, -d.radius]))
        hi = np.maximum(pts.max(axis=0),
                        np.array([u1, d.radius, d.radius]))
        return {
            "bbox": (tuple(lo), tuple(hi)),
            "fiber_diameter": float(fiber_diameter),
            "layers": sum(int(b.layers) for b in laid),
            "n_pts": n_pts,
            "n_pts_raw": int(n_pts if n_raw is None else n_raw),
            "decimated": thin_bound is not None,
            "thin_bound": thin_bound,
            "radius": d.radius,
            "length": d.length,
            # The collector's DRAWN span is `length` plus this at each end.
            # Carried here once, from `d.params["axial_overhang"]`, so the rod
            # actor reads the same number the camera envelope above does and
            # neither has to re-derive it from the amplitude.
            "axial_overhang": float(overhang),
            # ... and the ROD interval, straight from `axial_layout`, so the
            # rod actor does no arithmetic of its own. Named for the actor that
            # reads it, not for the function it now comes from.
            "collector_bounds": (float(u0), float(u1)),
            # Where the PRINT starts on that rod -- the end margin, made
            # visible. The rod interval alone cannot show it: a derived rod is
            # exactly the print plus two margins, so the two numbers differ
            # only here.
            "print_start_x": float(lay.print_start_x),
            "family": family,
            "a_max_deg": a_max_deg,
            "design": d,
            "physics": physics is not None and not provisional,
            "preview_sagged": (bool(sag_multiplier > 0.0) and not provisional
                               and sag_refused is None),
            "sag_refused": sag_refused,
            "gcode_paths": gcode_paths,
            "gcode_ptimes": gcode_ptimes,
            "print_time_s": print_time_s,
            "fiber_length_mm": fiber_length_mm,
            "ptimes": (point_times if all(t is not None for t in point_times)
                       else None),
            "elapsed": time.perf_counter() - t0,
            "provisional": provisional,
        }

    # -- the toolpath-first picture: what the program commands, wrapped, with
    # no relay and no drape; the build's final picture replaces it
    if on_toolpath is not None and gcode_paths:
        on_toolpath(gcode_paths, _meta(gcode_paths, gcode_ptimes, True))

    import preview_sag
    if should_stop is not None and should_stop():
        raise preview_sag.BuildSuperseded("a newer edit replaced this build")
    sag_grid = None                  # set by the relay below; None = no split
    if physics is not None:
        import mew_deposition
        transform = mew_deposition.make_transform(
            physics["model"], pressure=physics["pressure"],
            voltage=physics["voltage"], temperature=physics["temperature"],
            zeta=physics.get("zeta", 1.0), coil=physics.get("coil", False),
            coil_wave_amp=physics.get("coil_wave_amp", 0.0),
            coil_wave_len=physics.get("coil_wave_len", 0.3),
            max_accel=physics.get("max_accel") or None,
            square_corner_velocity=physics.get("square_corner_velocity", 6.0),
            rotary_accel=physics.get("rotary_accel", 0.0),
            fill_per_tau=physics.get("fill_per_tau", 6.0),
            # THE ADAPTIVE CORNER GRID, on unconditionally here because this
            # call is only ever reached with a cylinder.  `fill_per_tau` samples
            # uniformly in TIME inside each corner window, a fair proxy on a
            # plate but not on a tube, where corners sit closer together than
            # the lag so every window clamps and the fill collapses into one
            # grid that knows nothing about how far a corner turns.  Measured at
            # the shipped `fill_per_tau` of 6 against the same march at 96: the
            # drawn chevron cut 31.7 um (1.6 fiber diameters) off the curve while
            # its marched VERTICES sat 1.7 um from it -- a sampling defect, not
            # an integration one.  With the grid on at Standard, 3.2 um.  The
            # TIER is `physics["curve_quality"]`; `auto_corner_grid` is the one
            # place saying what a tier means, and Corner-resolution seeds it.
            corner_chord=mew_deposition.auto_corner_grid(
                fiber_diameter, physics.get("curve_quality")),
            simulate_travel=True)
        # `frame=` is load-bearing, not decoration: it is what makes the
        # transform build the mandrel ContactPlacement instead of reshaping the
        # unrolled path as if it were a flat plate.
        # the jet never stops, so the travels (the lead-in included) are laid
        # fibre: the plate preview's `rtravel`, and the export's --render-travel
        unrolled, ptimes = transform(unrolled, ptimes, travels, frame=frame)
        sag_grid = transform.sag_grid
    else:
        unrolled, ptimes = gcode_to_fibers.merge_travels(
            unrolled, ptimes, travels)
    import preview_sag
    # THE SAG / DRAWN-CURVE SPLIT, exactly as the export does it
    # (`gcode_to_fibers.main`): the corner grid's extra samples exist to smooth
    # what is DRAWN, and the drape resamples the centreline on its own step
    # either way, so it runs on the SEED grid and its Z is then carried back onto
    # the refined fiber.  `sag_grid` is None with the corner grid off, and this is
    # then the single call it has always been.
    drawn, drawn_t = unrolled, ptimes
    if sag_grid:
        unrolled, ptimes = gcode_to_fibers.split_for_sag(unrolled, ptimes,
                                                         sag_grid)
    # FUSION: `drape_sag`'s
    # `fuse_sink` defaults to 0, so a tube drew every crossing at the bare
    # one-diameter rest while the 3-axis and 5-axis previews (through
    # `gui_preview.preview_sag`) and every export compacted theirs. The tube's
    # stack therefore stood taller than the STL it previews, by one sink per
    # ply. The law arrives already built, from the same `_pv_fusion_law` the
    # other two previews read, and is evaluated here exactly as
    # `gui_preview.preview_sag` evaluates it -- two lines rather than an import
    # of a Tk mixin on the worker thread.
    _fuse = float(fuse_law(fiber_diameter)) if callable(fuse_law) else 0.0
    _fuse_w = float(fuse_law(fiber_diameter, True)) if callable(fuse_law) else None
    # The drape runs in the APP's process, so it gets the app's budget
    # (`gui_preview.drape_budget_samples`). Over it the tube is drawn as
    # commanded, with the reason on the status line, and the export solver --
    # its own process -- is the way to see it sagged.
    sag_refused = None
    budget = gui_preview.drape_budget_samples(_DRAPE_BYTES_PER_SAMPLE)
    try:
        unrolled, sagged_times = preview_sag.drape_sag(
            unrolled, ptimes, fiber_diameter, sag_multiplier=sag_multiplier,
            fuse_sink=_fuse, fuse_wall=_fuse_w, band=fuse_band,
            frame=frame, max_samples=budget, should_stop=should_stop)
    except preview_sag.DrapeBudgetExceeded as exc:
        unrolled, sagged_times = drawn, drawn_t
        sag_grid = None
        sag_refused = (gui_preview.drape_refusal(exc, budget)
                       + ". Press Preview at export quality, or Generate, to "
                       "see it sagged")
    if sag_grid:
        unrolled, sagged_times = gcode_to_fibers.carry_sag_onto_drawn(
            unrolled, sagged_times, drawn, drawn_t)
    # `wrap_path` is `wrap_polyline` carrying each point's print time across
    # the densification: the G-code page's click-to-line reads it
    if sagged_times is None or len(sagged_times) != len(unrolled):
        sagged_times = [None] * len(unrolled)
    wrapped = [mew_frame.wrap_path(frame, p, times=t)
               for p, t in zip(unrolled, sagged_times)]
    paths = [w for w, _t in wrapped]
    point_times = [t for _w, t in wrapped]
    if not paths:
        raise ValueError("the mandrel generator produced no deposited paths")
    # Only the DRAWING is thinned past the display budget, within a stated
    # sub-fiber bound, its times with it -- the plate builder's rule
    # (`gui_preview.thin_for_display`). Drawing costs ~2 KB per point.
    n_raw = sum(len(p) for p in paths)
    times_in = (point_times if all(t is not None for t in point_times)
                else None)
    paths, times_out, thin_bound = gui_preview.thin_for_display(
        paths, times_in, max_bound=max(gui_preview._DISPLAY_STEP_MM,
                                       2.0 * float(fiber_diameter)))
    if times_in is not None:
        point_times = times_out
    elif thin_bound is not None:
        point_times = [None] * len(paths)
    meta = _meta(paths, point_times, False, n_raw=n_raw,
                 thin_bound=thin_bound, sag_refused=sag_refused)
    return paths, meta


class MandrelPreviewPanel:
    """The Scaffold designer's ONE preview panel, plus a solid collector.

    Composition avoids changing Scaffold preview internals.  The wrapped panel
    retains its established mouse camera and offscreen rendering behaviour.

    Since the tab merge this is the panel BOTH collector modes
    draw into, so the rod is not always wanted: it is an additive actor whose
    visibility follows the collector toggle (``set_collector_visible``).  Built
    standalone it defaults to visible, since
    nothing but a mandrel would construct one directly.
    """

    def __init__(self, parent, app, direct=False):
        import gui_preview

        owner = self

        class _Panel(gui_preview.PreviewPanel):
            def _on_map(panel_self, _e=None):
                panel_self.retheme()
                # One panel, two owners: hand the map to whichever half the
                # collector toggle has selected.
                if owner._is_mandrel(app):
                    app._mandrel_preview_on_mapped()
                else:
                    app._pv_on_panel_mapped()

            def _ensure_scene(panel_self):
                ready = super(_Panel, panel_self)._ensure_scene()
                if ready and not hasattr(panel_self, "_mandrel_actor"):
                    vtk = gui_preview._VTK
                    src = vtk.vtkCylinderSource()
                    src.SetResolution(64)
                    src.CappingOn()
                    xf = vtk.vtkTransform()
                    xf.RotateZ(-90.0)       # VTK cylinder +Y -> mandrel +X
                    filt = vtk.vtkTransformPolyDataFilter()
                    filt.SetInputConnection(src.GetOutputPort())
                    filt.SetTransform(xf)
                    mapper = vtk.vtkPolyDataMapper()
                    mapper.SetInputConnection(filt.GetOutputPort())
                    actor = vtk.vtkActor()
                    actor.SetMapper(mapper)
                    prop = actor.GetProperty()
                    # SOLID: the rod is a Preview-
                    # options checkbox, so "see through it" is served by
                    # unticking the box rather than by a permanent half-tint
                    # that made the fibers behind it muddy in both states.
                    #
                    # The MATERIAL is ported from the Viewer's collector, not
                    # re-picked -- gcode_player.py:269 (PLATE_COLOR) and its
                    # _mandrel_body, gcode_player.py:3508-3513. All four values
                    # matter: a rod a hair lighter in RGB can read far
                    # darker, if it takes VTK's default ambient 0 / diffuse
                    # 1 and so has unlit sides. The lift is the ambient term.
                    prop.SetColor(0.38, 0.45, 0.56)   # == PLATE_COLOR
                    prop.SetAmbient(0.55)
                    prop.SetDiffuse(0.45)
                    prop.SetSpecular(0.0)
                    actor.SetVisibility(1 if owner._rod_visible else 0)
                    panel_self._ren.AddActor(actor)
                    panel_self._mandrel_src = src
                    panel_self._mandrel_actor = actor
                if ready and not hasattr(panel_self, "_plate_actor"):
                    # THE FLAT COLLECTOR, the 3-axis member of the set the rod
                    # above and the revolution body below already had. Same
                    # material as the rod, ported from the Viewer's plate for
                    # the same reason (gcode_player.PLATE_COLOR, and the
                    # ambient lift without which the sides read unlit).
                    #
                    # A CUBE for both plate shapes. The Machine page offers a
                    # circular plate as well, and this draws it as the square
                    # its bounding box describes rather than growing a second
                    # source: the square is the plate's own footprint at its own
                    # position (see `_size_plate`); only the corners are not.
                    vtk = gui_preview._VTK
                    psrc = vtk.vtkCubeSource()
                    pmap = vtk.vtkPolyDataMapper()
                    pmap.SetInputConnection(psrc.GetOutputPort())
                    pact = vtk.vtkActor()
                    pact.SetMapper(pmap)
                    pprop = pact.GetProperty()
                    pprop.SetColor(0.38, 0.45, 0.56)    # == PLATE_COLOR
                    pprop.SetAmbient(0.55)
                    pprop.SetDiffuse(0.45)
                    pprop.SetSpecular(0.0)
                    pact.SetVisibility(1 if owner._plate_visible else 0)
                    panel_self._ren.AddActor(pact)
                    panel_self._plate_src = psrc
                    panel_self._plate_actor = pact
                if ready:
                    owner._apply_look(panel_self)
                return ready

            def apply_state(panel_self, state, subtitle):
                ok = super(_Panel, panel_self).apply_state(state, subtitle)
                meta = state.get("meta") or {}
                if ok:
                    owner._size_plate(panel_self, state)
                src = getattr(panel_self, "_mandrel_src", None)
                if ok and src is not None and "radius" in meta and "length" in meta:
                    # The rod is sized to the printed SPAN, not to `length`.
                    # `length` is the cell tiling; a wavy ring runs from
                    # u = -A to u = length + A, so a rod cut to `length` left
                    # the end rings hanging off the collector they are wound
                    # on -- 0.5 mm off each end at the page's own chevron
                    # defaults.  The camera envelope already covered the
                    # overhang; the rod was the only thing short.
                    #
                    # Read, never recomputed: `collector_bounds` is the one
                    # definition and `build_mandrel_preview` has already run
                    # it.  The fallback is that same function's arithmetic and
                    # exists only for a meta built before it did.
                    u0, u1 = meta.get("collector_bounds") or (
                        -float(meta.get("axial_overhang", 0.0) or 0.0),
                        float(meta["length"])
                        + float(meta.get("axial_overhang", 0.0) or 0.0))
                    src.SetRadius(float(meta["radius"]))
                    src.SetHeight(u1 - u0)
                    # vtkCylinderSource is centred/aligned on +Y.  The fixed
                    # transform turns +Y into the collector's +X, so its centre
                    # must be supplied on Y *before* that transform.  Supplying
                    # it on X made the tube appear to float beside the rod.
                    src.SetCenter(0.0, 0.5 * (u0 + u1), 0.0)
                    src.Modified()
                    panel_self._fit_clipping()
                    panel_self._render_soon()
                return ok

        self._rod_visible = self._is_mandrel(app)
        # Off unless the app says otherwise -- the plate box is the one
        # collector toggle that ships unticked (gui_preview._pv_ensure_option_vars).
        self._plate_visible = False
        self._app = app
        self._look = (self.COLLECTOR_RGB, 1.0)
        self.widget = _Panel(parent, app, direct=direct)

    @staticmethod
    def _is_mandrel(app):
        """True when the app's collector toggle is on the mandrel.

        Defaults to True for an app that has no toggle at all -- a bare test
        harness constructing this panel directly means a mandrel."""
        fn = getattr(app, "_mandrel_is_active", None)
        return bool(fn()) if callable(fn) else True

    def clear(self):
        """Blank the fibers on a collector switch (`PreviewPanel.clear`)."""
        self.widget.clear()

    #: the drawn collectors' colour, == gcode_player.PLATE_COLOR
    COLLECTOR_RGB = (0.38, 0.45, 0.56)

    def set_look(self, rgb=None, opacity=1.0):
        """The collector's colour (None: the default) and opacity (1 is
        solid), for whichever of the two bodies is drawn now or later."""
        self._look = (tuple(rgb) if rgb else self.COLLECTOR_RGB, float(opacity))
        self._apply_look(self.widget)
        try:
            self.widget._render_soon()
        except Exception:  # noqa: BLE001
            pass

    def _apply_look(self, panel):
        rgb, opacity = self._look
        for name in ("_mandrel_actor", "_plate_actor"):
            actor = getattr(panel, name, None)
            if actor is not None:
                actor.GetProperty().SetColor(*rgb)
                actor.GetProperty().SetOpacity(opacity)

    def set_collector_visible(self, visible):
        """Show or hide the collector rod (the 3-axis / 4-axis switch)."""
        self._rod_visible = bool(visible)
        actor = getattr(self.widget, "_mandrel_actor", None)
        if actor is not None:
            actor.SetVisibility(1 if self._rod_visible else 0)
            try:
                self.widget._render_soon()
            except Exception:  # noqa: BLE001
                pass

    def set_plate_visible(self, visible):
        """Show or hide the build plate (the 3-axis switch).

        The flat twin of `set_collector_visible`. The actor exists from the
        first `_ensure_scene`, but it is a unit cube until `_size_plate` has a
        scaffold to sit under, so this is only a visibility flip."""
        self._plate_visible = bool(visible)
        actor = getattr(self.widget, "_plate_actor", None)
        if actor is None:
            return
        actor.SetVisibility(1 if self._plate_visible else 0)
        # The CAMERA STAYS PUT, in both directions, like the rod's and the
        # dome's toggles. It used to reframe to fit the whole plate, which
        # threw away the user's zoom on every tick (reported: toggling the
        # box zooms out). The framing follows the design, never the plate;
        # only the clipping range has to grow to take the slab in.
        try:
            self.widget._fit_clipping()
            self.widget._render_soon()
        except Exception:  # noqa: BLE001
            pass

    def _size_plate(self, panel, state):
        """Put the plate under the scaffold that was just applied.

        SIZE comes from the Machine page's own fields -- the same vars
        `gui_buildplate._plate_args` sends to the workers -- so the slab is the
        user's real plate at real scale. A circular plate is drawn as its
        bounding square; see the actor's own comment.

        PLACE does too: the preview draws each scaffold at its real Start X/Y
        (plus the origin datum), in the g-code frame the workers use, so the
        plate goes at `gui_buildplate._plate_center_xy` -- the centre
        `_plate_args` sends them. Without that method (a partial build) it
        falls back to centring under the drawn scaffold.

        TOP is the floor of a first layer commanded at Z 0: the preview draws
        fiber centrelines at the commanded Z (the plate-top lift is the
        workers', not the preview's), so the plate top is half a fiber below
        it. A design lifted by Start Z then floats above the slab, as it will
        print."""
        src = getattr(panel, "_plate_src", None)
        app = self._app
        if src is None or app is None:
            return
        meta = state.get("meta") or {}
        bbox = meta.get("bbox")
        if not bbox:
            return
        try:
            shape = str(app.plate_shape.get()).strip()
            thick = float(app.plate_thickness.get())
            if shape == "circular":
                sx = sy = float(app.plate_diameter.get())
            else:
                sx = float(app.plate_x.get())
                sy = float(app.plate_y.get())
        except (AttributeError, ValueError, TypeError):
            return                          # a half-typed field draws nothing new
        if not (sx > 0.0 and sy > 0.0 and thick > 0.0):
            return
        (lox, loy, _loz), (hix, hiy, _hiz) = bbox
        top = -0.5 * float(meta.get("fiber_diameter", 0.0) or 0.0)
        centre = None
        fn = getattr(app, "_plate_center_xy", None)
        if callable(fn):
            try:
                centre = fn()
            except (AttributeError, ValueError, TypeError):
                centre = None
        cx, cy = centre or (0.5 * (lox + hix), 0.5 * (loy + hiy))
        key = (shape, sx, sy, thick, cx, cy, top)
        if getattr(panel, "_plate_key", None) == key:
            return
        panel._plate_key = key
        src.SetXLength(sx)
        src.SetYLength(sy)
        src.SetZLength(thick)
        src.SetCenter(cx, cy, top - 0.5 * thick)
        src.Modified()
        panel._fit_clipping()
        panel._render_soon()

    def __getattr__(self, name):
        return getattr(self.widget, name)
