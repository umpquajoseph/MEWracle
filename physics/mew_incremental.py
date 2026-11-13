"""Real-time INCREMENTAL (time-marched) deposition for the player.

Phase 4: "precompute the whole sagged scaffold, then reveal it" gives way to a
TIME-MARCH. The jet lays fibre as the clock advances and each layer is sagged onto
the structure already beneath it the instant it BEGINS being laid -- the in-progress
layer is the "tip", sagged against the frozen structure below and revealed up to the
playhead, so it shows at its resting Z rather than floating at the commanded height
-- then frozen into the lower-layer index the instant it completes, committing the
very polylines the tip already solved, so there is no second solve and no snap.

WHY THIS KILLS THE LIVE-EDIT LATENCY, on two properties:
* SAGGING IS CAUSAL IN DEPOSITION ORDER. A layer rests only on layers laid before
  it, never after (`gcode_to_fibers._SagState` and the bit-identity warm-start test
  pin this), so the accumulated result is byte-for-byte the batch `apply_sag` for
  the single-scaffold case, where deposition order is Z order.
* FROZEN PAST, LAZY FUTURE. Editing a knob only appends a schedule breakpoint at the
  contact time, so already-laid layers are untouched (an O(1) edit). The freeze is
  at the PLAYHEAD, not the layer boundary: an edit also snapshots the part of the
  IN-PROGRESS layer already laid (`_freeze_tip_up_to`) and splices it verbatim in
  front of every later re-solve, or a mid-layer diameter edit floats the fibre
  already laid.

LIVE == LOCKED WHILE UNEDITED. `mew_live` and `mew_deposition` reshape the
centreline slightly differently, and that sub-diameter difference, fed through the
layer-sequential sag, drifts the live crossovers from the locked ones. So when the
player hands this class the same `.depo` (`locked=`), a layer untouched by any edit
commits the cached fibres VERBATIM (`_layer_locked_clean`).

AND THE EDITED REGION USES THE EXPORT RELAY TOO. `mew_live` samples the touchdown
~5x denser, and the sag's crest passes are sample-density dependent, so a `mew_live`
re-derive clamps the two sides of a crest by different arc distances. A
CONSTANT-param edited layer therefore re-derives through
`mew_deposition.laid_fibers` + `diameter_by_time`, the path that built the `.depo`;
`mew_live` is kept ONLY for a layer STRADDLING a mid-fibre edit, and for the
no-`.depo` path (`_use_export_relay`).

Pure module -- numpy, the converter and `mew_live`, no VTK, no GUI, no I/O. Scope
is the `--live-adjust` sim only: the locked sim rides the `.depo` and the exported
STL is still batch-built. Units follow `mew_physics`.
"""

from __future__ import annotations

import numpy as np

import mew_deposition
import mew_live
from gcode_to_fibers import (_SagState, _layer_ids, _hash_cell, _sag_applicable,
                             loop_entry_flags, support_radii)
from mew_deposition import junction_speeds

__all__ = ["IncrementalDeposition"]

# Diameter multiple the persistent lower-layer index is PRE-SIZED for. NOT a clamp on
# the deposited fibre: the sag support, the tube and the jet all track the true
# physics diameter with no ceiling. The pre-size only sets where `rest_heights`
# switches from the cheap 3x3 cell query to a wider one -- a fatter fibre widens its
# own query neighbourhood, and the persistent index is never re-sized or re-built.
# 1.9x keeps the common moderate-over-extrusion what-if on the fast path while
# staying byte-identical to the batch `apply_sag` in the normal range (query width 1).
_INDEX_PRESIZE_MULT = 1.9

# Contact depth, in layer heights, kept in the `all_lower` rest-on-top index. A
# draping fibre contacts the first structure beneath it -- the immediate layer at a
# crossing, or in a pore the nearest parallel layer -- never the buried interior, so
# each cell keeps only the structure within this depth of its own top. That holds
# the per-query candidate count FLAT with print height while staying byte-identical
# to the batch solve, the capped-away segments being unable to win a rest-height
# maximum. It is a per-CELL cap, NOT a global layer-count window: a perimeter
# turnaround loop ballooning out over open space is the TOP of its otherwise-empty
# cells, so it is kept however many layers down it is. A global window dropped it
# and the loop free-drooped to the bed; `test_lower_cap_keeps_exposed_deep_support`
# guards that, and the keystone incremental==batch test guards that the depth is
# enough for the dense interior.
_LOWER_WINDOW_LAYERS = 3


class IncrementalDeposition:
    """Accumulating, time-marched deposition state for the live sim.

    Construct once per launch from the commanded fibres + the live sag options;
    then drive it with `advance_to(t)` (lay everything complete by print-time `t`)
    and `set_params(t, params)` (a live knob edit, forward-accumulating). Read the
    laid geometry back with `committed_fibers()` (sagged, frozen) and `tip_fibers()`
    (the layer currently being laid -- SAGGED against the frozen structure below, so
    the jet meets the fibre at its resting Z and the trail never floats at the
    commanded height; revealed up to the playhead by the caller's time cut). All
    times are on the NAIVE commanded clock.

    The lower-layer spatial index lives in a persistent `_SagState`, sized once to
    the thickest fibre a diameter what-if can reach, so a frozen layer is never
    re-indexed and the march never rebuilds it from scratch (the per-edit cost the
    batch solve paid)."""

    def __init__(self, cmd_paths, cmd_ptimes, model, *, launch_pressure,
                 launch_voltage, launch_temperature, diameter,
                 sag_multiplier=1.0, sag_reset_layers=0,
                 no_sag=False, self_crossings=True,
                 base_path_diam=None, zeta=1.0, coil=False,
                 coil_wave_amp=0.0, coil_wave_len=0.3, max_accel=None,
                 square_corner_velocity=6.0, dynamic_diameter=True,
                 fill_per_tau=6.0, locked=None, frame=None, placement=None,
                 corner_chord=None, rotary_accel=0.0):
        # THE ROTARY SEAM. `frame` is a `mew_frame.DepositionFrame` (None = flat
        # collector) and `placement` a `mew_mandrel.ContactPlacement` (None = flat).
        # The commanded paths arriving here are ALREADY UNROLLED into the surface
        # frame (u, v, h) by `parse_gcode(frame=...)`, exactly as the batch
        # converter's are, so every span, sag and relay below is the flat solve on a
        # straight helix -- the same trick `gcode_to_fibers` uses. The two objects
        # are consumed in three places and nowhere else: the junction speeds and the
        # relay need `frame` for the METRIC (an arc in `v` is longer than its
        # unrolled coordinate says), the sag state needs it for the `v` SEAM and the
        # curvature terms, and the relay needs `placement` for the contact geometry
        # (Q10's along/across split). Both None takes no branch at all, so the flat
        # march stays byte-identical -- the property `test_mew_incremental`'s
        # keystone pins and this file must not cost.
        self._frame = frame
        self._placement = placement
        # THE ADAPTIVE CORNER GRID (`mew_deposition.auto_corner_grid`'s
        # (tol_mm, radius_mm, steps_per_tau) triple), the Curve-quality tier the
        # export renders at. A tube has no seed (see `_use_export_relay`), so it
        # simply matches the export. A FLAT run seeds from the `.depo`, so the
        # grid must be the one that `.depo` was exported on, or a re-derived layer
        # disagrees with the cached ones beside it. That is the CALLER's contract:
        # the GUI sends `--auto-corner-grid` to the export and the player together
        # ("Auto corner resolution", `gui_args._flat_auto_corner_args`), and
        # neither otherwise, so a flat march with None is the march it always was.
        #
        # NOTE the batch converter ALSO splits the grid -- solving the sag on the
        # unrefined seed times and carrying Z back onto the drawn fibre -- and this
        # deliberately does not. That split is a SPEED optimisation, not a
        # correctness one: "the sag solver resamples the centreline on its own
        # contact-sweep step either way, so feeding it the refined grid buys
        # nothing and costs a share of the run's dominant stage"
        # (`gcode_to_fibers`, "THE SAG / DRAWN-CURVE SPLIT"), which also measures
        # what taking it costs -- up to 13.5 um of Z. The march sags the refined
        # fibre directly, so it pays the time and keeps the Z.
        self._corner_chord = corner_chord
        self._cmd_paths = [np.asarray(p, dtype=np.float64) for p in cmd_paths]
        self._cmd_ptimes = [np.asarray(t, dtype=np.float64) for t in cmd_ptimes]
        # Per-fibre linking-loop flag, detected on the COMMANDED centrelines (the
        # loop closes on its exact start vertex here, before the re-lay rounds it),
        # so the live sag keeps a loop's entry continuous with the layer below --
        # the byte-for-byte twin of the batch converter's loop_entry (see
        # gcode_to_fibers.apply_sag), keeping live == locked at a layer change.
        self._loop_entry = loop_entry_flags(self._cmd_paths)
        # Boundary speeds (mm/s) so the head flows THROUGH a linking-loop junction
        # instead of stopping there -- removing the spurious v->0 diameter bead at the
        # loop entry. Computed once over the whole commanded set (the bead spans two
        # adjacent layers' fibres), then sliced per layer in `_relay_layer`. 0 off a
        # loop junction, so a non-loop print re-lays exactly as before.
        self._entry_vs, self._exit_vs = junction_speeds(
            self._cmd_paths, self._cmd_ptimes, self._loop_entry, frame=frame)
        self.model = model
        self._launch = mew_live.Params(launch_pressure, launch_voltage,
                                       launch_temperature, 1.0)
        self._nominal = float(diameter)
        # Whether the deposited DIAMETER tracks the physics (mass conservation, the
        # head's speed + the live pressure/temperature knobs) or stays the fixed
        # commanded `diameter`. Mirrors the GUI's "Physics-deposited scaffold" minus
        # "Override fibre diameter": on -> the live diameter is recomputed from the
        # schedule per deposition-time (what mew_export's --dynamic-diameter bakes
        # into the .depo cache); off -> the hand-typed nominal everywhere, so a manual
        # override shows the value the user set instead of a re-derived physics one
        # (the centreline still re-lays for jet-lag/coiling -- only the diameter is
        # pinned). The CTS regime gate (coiling) is unaffected; it reads the schedule.
        self._dynamic_diameter = bool(dynamic_diameter)
        self._sag_multiplier = float(sag_multiplier)
        # Periodic sag reset -- the march must reset on the SAME layers the batch
        # export does, or the live sim and the STL diverge above the first reset
        # (see _SagState.maybe_reset and `_sag_layer`). 0 = never.
        self._sag_reset_layers = int(sag_reset_layers or 0)
        self._no_sag = bool(no_sag)
        self._self_crossings = bool(self_crossings)
        self._base_pd = (list(base_path_diam) if base_path_diam is not None
                         else None)
        self.zeta = float(zeta)
        self.coil = bool(coil)
        self.coil_wave_amp = float(coil_wave_amp)
        self.coil_wave_len = float(coil_wave_len)
        self.max_accel = max_accel
        self.scv = float(square_corner_velocity)
        # the mandrel's separate rotary limit, deg/s^2 (0 = none): forwarded to
        # BOTH relay branches beside max_accel, as mew_export bakes the .depo
        self.rotary_accel = float(rotary_accel or 0.0)
        # The refine grid, forwarded to BOTH relay branches. It must be the value
        # mew_export baked the locked `.depo` with: a live edit re-lays only the
        # touched layers, so a mismatch would leave the edited layers sampled
        # differently from the untouched ones sitting next to them in the same scene.
        self.fill_per_tau = float(fill_per_tau)

        n = len(self._cmd_paths)
        self._sched = mew_live.ParamSchedule(self._launch)

        # LOCKED geometry (the exported `.depo` cache the locked sim rides), aligned
        # to commanded-fibre index: (paths, times, per-fibre diam, per-point ppd). When
        # present AND a layer is UNTOUCHED by any live edit, the march commits this
        # verbatim instead of re-deriving via mew_live -- so the unedited live sim is
        # byte-for-byte the locked sim (mew_live and mew_deposition produce slightly
        # different centrelines, which otherwise drift the live crossovers vs locked).
        # Only fibre laid AFTER an edit re-derives. None -> always re-derive (the
        # pre-`.depo` behaviour; also the fallback when no cache exists or it mismatches
        # the current params). See `_layer_locked_clean` / `_sag_layer`.
        self._locked_path = self._locked_time = None
        self._locked_diam = self._locked_ppd = None
        if locked is not None:
            lp, lt, ld, lpp = locked
            if (lp is not None and len(lp) == n and lt is not None
                    and all(p is not None and len(p) >= 1 for p in lp)):
                self._locked_path = [np.asarray(p, dtype=np.float64) for p in lp]
                self._locked_time = [np.asarray(t, dtype=np.float64) for t in lt]
                self._locked_diam = [float(d) for d in ld]
                self._locked_ppd = ([None if d is None else
                                     np.asarray(d, dtype=np.float64) for d in lpp]
                                    if lpp is not None else [None] * n)

        # --- stable globals (functions of the commanded set; fixed across edits)
        # _layer_ids clusters by median Z, and the re-lay preserves Z, so the layer
        # assignment is identical whether taken on the commanded or relaid paths and
        # never moves when a knob changes. Compute it once.
        z_cluster, _ = _layer_ids(
            self._cmd_paths, layer_tol=0.5 * self._min_nominal())
        # Split each Z-cluster into DEPOSITION-CONTIGUOUS march layers: the march
        # commits + sags strictly in clock order, so a "layer" here must be a single
        # contiguous run of fibres laid back-to-back at one height, sharing ONE
        # completion time. A pure Z-cluster does not: a stabilization base and the
        # main scaffold both start at Z0 (the base shares the scaffold's start_z), so
        # the base's bottom fibre and the scaffold's bottom fibre land in one cluster
        # whose completion time then sits LATE, commit-time ordering buries it under
        # the base's own top layers, and the jet sticks at the end of the first
        # layer. Starting a new march layer whenever the Z-cluster changes between
        # consecutive commanded fibres keeps every layer causal. For a single
        # bottom-up scaffold this reduces to the plain Z-clustering and the geometry
        # stays byte-for-byte the batch apply_sag (the incremental==batch keystone).
        self._members = []
        for i in range(n):
            if i == 0 or z_cluster[i] != z_cluster[i - 1]:
                self._members.append([])
            self._members[-1].append(i)
        self._n_layers = len(self._members)
        self._bed_center = (min(float(np.median(p[:, 2]))
                                for p in self._cmd_paths) if n else 0.0)
        # The index cell is PRE-SIZED to a moderate over-extrusion (the pre-size
        # multiple x the largest nominal) so the common what-if stays on the cheap 3x3
        # query path. A fibre fatter than this is NOT under-reached: `rest_heights`
        # widens its own query neighbourhood to the true contact distance (see
        # _SegIndex), so the persistent index is never re-sized. A wider cell only
        # costs query time, not a result (candidate sets are max-reduced / exactly
        # tested), so this stays byte-identical to the batch solve in the normal range.
        self._shared_cell = _hash_cell(_INDEX_PRESIZE_MULT * self._max_nominal())
        # per-layer deposition-completion time = the last commanded point time over
        # its members; a layer is laid (and sag-able) once the clock passes it.
        self._completion = [max((float(self._cmd_ptimes[i][-1])
                                 for i in self._members[li]
                                 if len(self._cmd_ptimes[i])), default=0.0)
                            for li in range(self._n_layers)]
        # commit order = completion order (Z order for a bottom-up print, the
        # bit-identical case; for a multi-scaffold run whose Z interleaves it is
        # deposition-causal -- each layer rests on what was actually laid first).
        self._order = sorted(range(self._n_layers),
                             key=lambda li: (self._completion[li], li))
        # per-layer commanded Z (the layer's median height) -- the depth axis the
        # lower-index cap measures along, and the layer-spacing source for it.
        self._layer_z = [
            float(np.median([float(np.median(self._cmd_paths[i][:, 2]))
                             for i in self._members[li]]))
            if self._members[li] else 0.0
            for li in range(self._n_layers)]
        # The per-cell height cap is sound only when the print stacks monotonically
        # upward in commit order -- then a cell's highest segment is its most recent,
        # so capping below the top keeps exactly the reachable surface. A
        # multi-scaffold run whose Z interleaves could put older-but-higher structure
        # in a cell, so there we keep the full history (cap off); these runs are
        # small, not the slow case.
        order_z = [self._layer_z[li] for li in self._order]
        self._prune_lower = all(order_z[k] <= order_z[k + 1] + 1e-9
                                for k in range(len(order_z) - 1))
        # Per-cell height cap for the rest-height index: keep, per spatial cell, the
        # structure within ~_LOWER_WINDOW_LAYERS layer heights of that cell's top.
        # This bounds the per-query candidate count on a tall stack LOCALLY -- the
        # robust replacement for the old global layer-count eviction, which dropped a
        # perimeter loop's deep support when the layers above ballooned to other
        # cells (the trapezoid intersections). Sized from the real layer Z spacing so
        # it tracks the deepest local rest (~2 layers down in a pore) with margin.
        gaps = np.diff(np.sort(order_z))
        gaps = gaps[gaps > 1e-9]
        spacing = float(np.median(gaps)) if len(gaps) else self._max_nominal()
        self._lower_cap_depth = ((_LOWER_WINDOW_LAYERS + 1) * spacing
                                 if self._prune_lower else None)

        # whether sag applies at all (>=2 layers, or a self-crossing single layer);
        # when not, every layer passes through as its relaid centreline.
        self._applicable = (not self._no_sag) and _sag_applicable(
            self._cmd_paths, self._min_nominal(),
            self_crossings=self._self_crossings)[0]

        self._reset_accumulator()

    # -- nominal-diameter helpers ------------------------------------------ #
    def _min_nominal(self):
        if self._base_pd:
            vals = [float(d) for d in self._base_pd if d and float(d) > 0]
            return min(vals) if vals else self._nominal
        return self._nominal

    def _max_nominal(self):
        if self._base_pd:
            vals = [float(d) for d in self._base_pd if d and float(d) > 0]
            return max(vals) if vals else self._nominal
        return self._nominal

    def _nominal_for(self, i):
        if self._base_pd and self._base_pd[i] and float(self._base_pd[i]) > 0:
            return float(self._base_pd[i])
        return self._nominal

    # -- locked-`.depo` reuse ---------------------------------------------- #
    @property
    def has_locked(self):
        """Whether a usable locked `.depo` was supplied (the unedited march will
        ride it instead of re-deriving)."""
        return self._locked_path is not None

    def _layer_locked_clean(self, li):
        """True if layer `li` can commit the LOCKED `.depo` geometry verbatim: the
        cache is present for every member AND no live edit touches this layer's
        deposition (it finished before the earliest edit), so it is byte-for-byte
        what the locked sim shows. False -> re-derive via mew_live (an edit reached
        it, or no cache). The whole print is clean until the first edit, so an
        unedited march rides the cache end-to-end (live == locked)."""
        if self._locked_path is None:
            return False
        members = self._members[li]
        if any(self._locked_path[i] is None or len(self._locked_path[i]) < 1
               for i in members):
            return False
        comp = self._completion[li]
        # any edit breakpoint at/before this layer's completion = edit-affected.
        # breakpoint_times_in excludes the t=0 base, so a no-edit schedule -> [].
        edits = self._sched.breakpoint_times_in(0.0, 1e18)
        return not any(te <= comp + 1e-9 for te in edits)

    def _locked_layer_rec(self, li):
        """The `_sag_layer` 7-tuple built from the LOCKED `.depo` geometry for layer
        `li` (no relay, no sag -- the cached sagged fibres ARE the answer). Diameter
        is the cache's per-fibre value; the per-point drawn profile is the cache's
        diam-by-time sample (None -> falls back to the scalar). Shaped exactly like
        the re-derived path so the commit / tip / freeze machinery is unchanged."""
        members = self._members[li]
        out_polys = [self._locked_path[i] for i in members]
        out_times = [self._locked_time[i] for i in members]
        diams = [self._locked_diam[i] for i in members]
        ppd = [(self._locked_ppd[i] if self._locked_ppd[i] is not None
                else np.full(len(self._locked_path[i]), self._locked_diam[i],
                             dtype=np.float64)) for i in members]
        layer_polys = [p for p in out_polys if len(p) >= 2]
        radius = (min(diams) / 2.0) if diams else (self._max_nominal() / 2.0)
        return members, out_polys, out_times, diams, layer_polys, radius, ppd

    # -- accumulator state ------------------------------------------------- #
    def _reset_accumulator(self):
        self._state = (_SagState(
            shared_cell=self._shared_cell, bed_center=self._bed_center,
            max_diam=_INDEX_PRESIZE_MULT * self._max_nominal(),
            sag_multiplier=self._sag_multiplier,
            sag_reset_layers=self._sag_reset_layers,
            self_crossings=self._self_crossings, with_times=True,
            lower_cap_depth=self._lower_cap_depth, frame=self._frame)
            if self._applicable else None)
        self._committed = []      # records in commit order (see _lay_one_layer)
        self._cursor = 0          # index into _order of the next layer to lay
        self._tip = None          # (paths, times, diams) for the in-progress layer
        self._tip_key = None      # (layer, sched_version) the tip was built for
        self._sched_version = 0
        # single-entry preview-sag cache: a layer is sagged ONCE per (layer,
        # schedule version) -- the tip shows it, then `_lay_one_layer` commits the
        # very same solved polylines (no second solve). See `_sag_layer`.
        self._sag_key = None
        self._sag_rec = None
        # Freeze-front for the in-progress layer: deposited geometry must be immutable,
        # so a knob edit snapshots the part of the current layer already laid (up to
        # the playhead) and splices it, verbatim, in front of every later re-solve --
        # only fibre still AHEAD of the edit responds. `_frozen_tip` maps a commanded
        # fibre index to its frozen (poly, times, ppd) prefix; `_frozen_front` is the
        # deposition time everything up to which is frozen; `_frozen_layer` is the
        # march layer the snapshot belongs to. Empty until the first edit -> a no-edit
        # march never splices and stays byte-identical to the batch solve. See
        # `_freeze_tip_up_to` / `_splice_frozen`.
        self._frozen_tip = {}
        self._frozen_front = None
        self._frozen_layer = None

    @property
    def applicable(self):
        return self._applicable

    @property
    def n_committed(self):
        return len(self._committed)

    def frontier_time(self):
        """Completion time of the last committed (fully-laid) layer, or 0."""
        return self._committed[-1]["completion"] if self._committed else 0.0

    def pending(self, t):
        """True if a layer is due to be laid by naive time `t` but hasn't yet -- so
        the next `advance_to(t)` will run a (possibly heavy) sag. The player reads
        this to show a 'depositing' notice before the synchronous solve."""
        return (self._cursor < self._n_layers
                and self._completion[self._order[self._cursor]] <= t)

    # -- the march --------------------------------------------------------- #
    def set_params(self, t, params):
        """Record a live knob change taking effect at deposition-time `t` (the
        jet's contact time). Forward-accumulating: geometry already laid before `t`
        is untouched; geometry laid after picks up `params`. O(1) for an edit at the
        playhead (the common case); an edit in the already-laid past truncates the
        accumulator back to `t` so those layers re-lay on the next advance.

        Before the schedule changes, the part of the IN-PROGRESS layer already laid
        (up to `t`) is frozen (`_freeze_tip_up_to`), so a mid-layer edit cannot move
        deposited fibre -- the frozen prefix is spliced verbatim in front of every
        later re-solve and only fibre still ahead of `t` responds."""
        past = self.frontier_time() > t
        if not past:                          # at the playhead -> freeze deposited fibre
            self._freeze_tip_up_to(t)
        self._sched.set_at(t, params)
        self._sched_version += 1
        self._tip = self._tip_key = None
        if past:                              # editing the laid past -> rewind
            self._truncate_to(t)

    def reset(self, params):
        """Collapse the schedule back to a single segment at `params` and clear the
        accumulator (the live-mode Reset)."""
        self._sched.reset(params)
        self._sched_version += 1
        self._reset_accumulator()

    def advance_to(self, t, max_layers=None):
        """Lay every layer complete by naive print-time `t`, sagging each onto the
        accumulated structure below, and refresh the in-progress tip. A backward `t`
        (scrub-back) truncates first.

        `max_layers` caps how many layers a single call lays, so a big forward jump
        (scrub to the end) deposits PROGRESSIVELY across frames instead of freezing
        on one batch-sized solve -- the player keeps calling each frame until the
        march catches up to `t`. Returns True while there is still un-laid layer
        below `t` (the caller should keep advancing). None = no cap (lay all due)."""
        if self.frontier_time() > t:
            self._truncate_to(t)
        laid = 0
        while (self._cursor < self._n_layers
               and self._completion[self._order[self._cursor]] <= t):
            if max_layers is not None and laid >= max_layers:
                self._refresh_tip(t)
                return True                    # more layers due -> keep advancing
            self._lay_one_layer(self._order[self._cursor])
            self._cursor += 1
            laid += 1
        self._refresh_tip(t)
        return False

    def _truncate_to(self, t):
        """Drop committed layers that complete after `t` and rebuild the lower-layer
        index from the kept prefix (no re-solve -- the kept layers are reused
        verbatim). Bounded by the kept layer count."""
        keep = [rec for rec in self._committed if rec["completion"] <= t]
        self._committed = keep
        self._cursor = len(keep)
        self._tip = self._tip_key = None
        self._sag_key = self._sag_rec = None    # lower index rebuilt -> re-preview
        # the in-progress layer is being re-laid from scratch -> drop its freeze snapshot
        self._frozen_tip = {}
        self._frozen_front = None
        self._frozen_layer = None
        if self._state is not None:
            self._state = _SagState(
                shared_cell=self._shared_cell, bed_center=self._bed_center,
                max_diam=_INDEX_PRESIZE_MULT * self._max_nominal(),
                sag_multiplier=self._sag_multiplier,
                sag_reset_layers=self._sag_reset_layers,
                self_crossings=self._self_crossings, with_times=True,
                lower_cap_depth=self._lower_cap_depth, frame=self._frame)
            # Replay every kept layer; the index's per-cell height cap (set above)
            # bounds the query cost, so unlike the old layer-count window there is no
            # need to drop deep layers here -- the cap drops only genuinely buried
            # structure, locally, as each layer is re-added (see `_SegIndex._build`).
            for rec in keep:
                self._state.reuse_layer(rec["polys"], rec["diam"],
                                        layer_radii=rec["support_radii"])

    def _use_export_relay(self, li):
        """Whether layer `li`'s re-derive should go through the EXPORT relay
        (`mew_deposition`) rather than `mew_live` -- the crossover-crest-symmetry fix.

        `mew_live` samples the touchdown ~5x denser (per-event local-tau windows to
        the fibre END) than `mew_deposition` (windows to the next CORNER, fibre-wide
        tau_min). The sag's crest passes are sample-density-dependent -- the rest-clamp
        keys off sample positions -- so fed mew_live's non-uniform grid they clamp the
        two sides of a crossover crest by different arc distances -> one side a steep
        clamped plateau, the other shallow (the live-sim asymmetry). `_round_crests`
        reaches by distance, not a count of samples, so it rounds both sides to one
        radius; the rest-clamp half remains. The export relay built the
        locked `.depo`, whose crests are symmetric, so routing a re-derived layer
        through it reproduces that geometry.

        True when no schedule breakpoint lands strictly inside the layer's deposition
        window (its params are constant across it) AND either a locked `.depo` seed is
        present or this is a MANDREL run. A layer STRADDLING a mid-fibre edit returns
        False either way and keeps `mew_live`, where the freeze/splice transient
        machinery lives.

        THE MANDREL CLAUSE, and why it is not the seed rule relaxed by accident. On a
        flat run the seed is what licenses the claim of EXACTNESS -- the cache was
        built by this relay at these params, so routing through it reproduces the
        cache byte-for-byte, and with no cache there is nothing to reproduce, so the
        old behaviour (`mew_live` everywhere) costs nothing it can name. A tube has no
        such choice available: the `.depo` is written AFTER the wrap, in world
        coordinates, while everything here works in the unrolled surface frame, so
        that cache cannot be a seed for this march at all (`_load_live_locked` refuses
        it, and says so). What does NOT go away with the seed is the reason the export
        relay exists -- `mew_live`'s ~5x denser sampling clamps a crossover crest
        asymmetrically, which is a defect in the geometry whether or not a cache
        exists to compare against. So a tube takes the export relay on its merits:
        `mew_deposition.laid_fibers` is the same engine that builds the STL, run at
        the same params, and it is the accurate answer rather than the reproducible
        one."""
        if self._locked_path is None and self._placement is None:
            return False
        members = self._members[li]
        t0 = min((float(self._cmd_ptimes[i][0]) for i in members
                  if len(self._cmd_ptimes[i])), default=0.0)
        return not self._sched.breakpoint_times_in(t0, self._completion[li])

    def _repr_diams(self, members, times, dlk):
        """Per-fibre REPRESENTATIVE diameter (one scalar per fibre) -- the scalar the
        layer's SAG solve uses, kept like the batch `apply_sag`. The true physics mean
        of `dlk` (the time->diameter lookup) sampled on each fibre's sagged times, NOT
        clamped: a fat fibre must rest one TRUE diameter above the one below or its
        drawn tube clips in (`rest_heights` widens its own query for the larger contact
        reach -- see `_INDEX_PRESIZE_MULT`). Falls back to the commanded nominal when
        the diameter is not physics-driven (`dlk` None). Shared by both relay branches."""
        diams = []
        for k, i in enumerate(members):
            nom = self._nominal_for(i)
            tk = np.asarray(times[k], dtype=np.float64)
            if dlk is not None and len(tk):
                d = float(np.mean(np.interp(tk, dlk[0], dlk[1])))
            else:
                d = nom
            diams.append(d)
        return diams

    def _relay_layer(self, li):
        """Re-lay one layer's commanded fibres into their touchdown centrelines under
        the current schedule, and size each fibre's REPRESENTATIVE diameter from the
        same schedule (`_repr_diams`). The per-POINT diameter the tube/jet draw is
        computed separately in `_sag_layer` (on the sagged times), so an already-laid
        stretch keeps its diameter when a later edit retimes the schedule. Returns
        (members, paths, times, diams, dlk) -- `dlk` the time->diameter lookup, reused
        for the per-point pass.

        A constant-param layer with a locked `.depo` seed takes the EXPORT relay
        (`_relay_layer_export`) for symmetric crossover crests; everything else (no
        seed, or a layer straddling a mid-fibre edit) takes the `mew_live` relay. See
        `_use_export_relay`."""
        members = self._members[li]
        mp = [self._cmd_paths[i] for i in members]
        mt = [self._cmd_ptimes[i] for i in members]
        mev = [self._entry_vs[i] for i in members]
        mxv = [self._exit_vs[i] for i in members]
        if self._use_export_relay(li):
            return self._relay_layer_export(li, members, mp, mt, mev, mxv)
        paths, times = mew_live.relay_fibers(
            mp, mt, self.model, self._sched, zeta=self.zeta, coil=self.coil,
            coil_wave_amp=self.coil_wave_amp, coil_wave_len=self.coil_wave_len,
            max_accel=self.max_accel, square_corner_velocity=self.scv,
            fill_per_tau=self.fill_per_tau, entry_vs=mev, exit_vs=mxv,
            frame=self._frame, placement=self._placement,
            rotary_accel=self.rotary_accel)
        # The physics diameter-vs-time lookup, only when the diameter is physics-
        # driven. Off (a manual override / a non-physics diameter run) -> None, so
        # every per-fibre / per-point diameter below falls back to the commanded
        # nominal -- the value the user typed -- while the centreline above still
        # re-lays for jet-lag and coiling.
        dlk = (mew_live.diameter_schedule(
                   mp, mt, self.model, self._sched, max_accel=self.max_accel,
                   square_corner_velocity=self.scv, entry_vs=mev, exit_vs=mxv,
                   frame=self._frame, laid_paths=paths, laid_ptimes=times,
                   rotary_accel=self.rotary_accel)
               if self._dynamic_diameter else None)
        diams = self._repr_diams(members, times, dlk)
        return members, paths, times, diams, dlk

    def _relay_layer_export(self, li, members, mp, mt, mev, mxv):
        """Re-derive a CONSTANT-param layer through the EXPORT relay
        (`mew_deposition.laid_fibers` + `diameter_by_time` -- the same path that built
        the locked `.depo`) instead of `mew_live`, so its crossover crests stay
        symmetric (the locked geometry) rather than the asymmetric clamped-plateau the
        denser mew_live sampling drives through the sag (see `_use_export_relay`).

        The layer's params are constant (the gate guarantees no breakpoint strictly
        inside it), read once at the deposition-window midpoint -- robust to a
        breakpoint sitting exactly on either boundary. `speed_scale` is folded into the
        lag / diameter by `laid_fibers` / `diameter_by_time` the way `mew_live._tau_at`
        does, so the `]`/`[` speed what-if reshapes the same as a live sweep would.
        Boundary speeds (`mev`/`mxv`, computed once over the WHOLE commanded set) and
        the loop-entry flags are threaded in, so relaying this one layer in isolation
        still matches the whole-set export across a linking-loop junction. Same
        (members, paths, times, diams, dlk) contract as the `mew_live` branch."""
        t0 = min((float(t[0]) for t in mt if len(t)), default=0.0)
        pp = self._sched.params_at(0.5 * (t0 + self._completion[li]))
        le = [self._loop_entry[i] for i in members]
        paths, times, _info = mew_deposition.laid_fibers(
            mp, mt, self.model, pressure=pp.pressure, voltage=pp.voltage,
            temperature=pp.temperature, speed_scale=pp.speed_scale, zeta=self.zeta,
            coil=self.coil, coil_wave_amp=self.coil_wave_amp,
            coil_wave_len=self.coil_wave_len, max_accel=self.max_accel,
            square_corner_velocity=self.scv, fill_per_tau=self.fill_per_tau,
            loop_entry=le, entry_vs=mev, exit_vs=mxv,
            placement=self._placement, frame=self._frame,
            corner_chord=self._corner_chord, rotary_accel=self.rotary_accel)
        dlk = (mew_deposition.diameter_by_time(
                   mp, mt, self.model, pressure=pp.pressure, voltage=pp.voltage,
                   temperature=pp.temperature, speed_scale=pp.speed_scale,
                   max_accel=self.max_accel, square_corner_velocity=self.scv,
                   loop_entry=le, entry_vs=mev, exit_vs=mxv, frame=self._frame,
                   laid_paths=paths, laid_ptimes=times,
                   rotary_accel=self.rotary_accel)
               if self._dynamic_diameter else None)
        diams = self._repr_diams(members, times, dlk)
        return members, paths, times, diams, dlk

    def _per_point_diam(self, members, point_times, dlk):
        """Per-point diameter for each member fibre: the time-varying physics diameter
        (`dlk`) sampled at each SAGGED point's deposition time. Evaluating on the
        point's OWN time is what makes the diameter forward-accumulate -- a point laid
        before a knob edit keeps its old diameter, only points laid after thicken or
        thin. Returned parallel to `point_times` (one (Ni,) array per member), so it
        aligns vertex-for-vertex with the geometry that consumes it -- BOTH the drawn
        tube/jet AND the lower-layer support index.

        No clamp: the deposited fibre tracks the true physics diameter, and a later
        layer rests on it at that true diameter (`rest_heights` widens its own query to
        find the fatter contact -- see _INDEX_PRESIZE_MULT), so a fat crossover sits
        cleanly on top of the fibre below instead of clipping through it. (The earlier
        split between a clamped support diameter and an unclamped drawn one was what
        froze the crossover height ~2x nominal while the tube kept growing.)"""
        out = []
        for k, i in enumerate(members):
            nom = self._nominal_for(i)
            tk = np.asarray(point_times[k], dtype=np.float64)
            if dlk is not None and len(tk):
                d = np.interp(tk, dlk[0], dlk[1])
            else:
                d = np.full(len(tk), nom, dtype=np.float64)
            out.append(d)
        return out

    # THE SAME RULE THE LOCKED EXPORT USES, and deliberately not a second copy of
    # it: `gcode_to_fibers.support_radii` is now called by `_SagState.add_layer`
    # too, so the march and the batch solve cannot drift apart in what they seed
    # the lower index with. Kept as a name on this class because the docstring
    # above, `_lay_one_layer`, the scrub-back replay and `test_mew_incremental`
    # all reach for it here.
    _support_radii = staticmethod(support_radii)

    def _freeze_tip_up_to(self, t):
        """Snapshot the in-progress layer's geometry up to deposition-time `t`, so a
        schedule edit at `t` cannot move what has already been laid. Captures, per
        member fibre, the (poly, times, ppd) prefix with time <= `t` from the CURRENT
        (pre-edit) solve -- which already carries any earlier freeze -- and records it
        as the frozen prefix. `_sag_layer` then splices this verbatim in front of every
        later re-solve (see `_splice_frozen`), and `_lay_one_layer` commits the spliced
        result, so deposited fibre is immutable while fibre ahead of `t` still responds.
        No-op once the print is fully laid (no in-progress layer).

        The freeze front tracks the EDIT time `t` (== the playhead: an edit is made
        at the jet's current contact time). A forward edit grows it (more of the tip
        is now laid + shown); a SCRUB-BACK edit RETRACTS it -- the operator moved the
        playhead earlier and is re-deciding from there, so the once-frozen span ahead
        of the new `t` is no longer laid and must re-lay with the new params. Clamping
        the front to only grow (`max`) left that span frozen with stale params while
        `ParamSchedule.set_at` had already discarded the superseded breakpoint, so the
        committed geometry disagreed with the schedule (a scrub-back edit inside the
        tip produced different fibre than the same final schedule laid without the
        scrub -- and it propagated into the layers resting on top)."""
        if self._cursor >= self._n_layers:
            return
        li = self._order[self._cursor]
        members, out_polys, out_times, _diams, _lp, _r, ppd = self._sag_layer(li)
        if li != self._frozen_layer:            # tip moved to a new layer -> fresh snapshot
            self._frozen_tip = {}
        self._frozen_layer = li
        self._frozen_front = float(t)           # track the playhead (grow OR retract)
        fr = self._frozen_front
        for k, i in enumerate(members):
            tk = np.asarray(out_times[k], dtype=np.float64)
            mask = tk <= fr
            self._frozen_tip[i] = (np.asarray(out_polys[k])[mask], tk[mask],
                                   np.asarray(ppd[k])[mask])

    def _splice_frozen(self, members, out_polys, out_times, ppd):
        """Splice the frozen prefix in front of a fresh re-solve: per member fibre,
        keep the frozen (already-laid) points and append only the fresh points laid
        AFTER the freeze front. The frozen X/Y match the fresh solve's there (the
        schedule edit only moved fibre past the front, so the relay is unchanged
        before it), so the join is continuous; the Z/diameter may step at the front,
        which is exactly where the jet changed. Returns spliced (polys, times, ppd),
        each aligned per member and time-monotonic."""
        fr = self._frozen_front
        sp_polys, sp_times, sp_ppd = [], [], []
        for k, i in enumerate(members):
            fz = self._frozen_tip.get(i)
            ft = np.asarray(out_times[k], dtype=np.float64)
            if fz is None:
                sp_polys.append(out_polys[k])
                sp_times.append(out_times[k])
                sp_ppd.append(ppd[k])
                continue
            fz_poly, fz_times, fz_ppd = fz
            suf = ft > fr                       # only fibre ahead of the front re-lays
            sp_polys.append(np.concatenate([fz_poly, np.asarray(out_polys[k])[suf]]))
            sp_times.append(np.concatenate([fz_times, ft[suf]]))
            sp_ppd.append(np.concatenate([fz_ppd, np.asarray(ppd[k])[suf]]))
        return sp_polys, sp_times, sp_ppd

    def _sag_layer(self, li):
        """Relay + sag one layer against the accumulated structure below WITHOUT
        committing it -- the in-progress preview. Memoised by (layer, schedule
        version), so a layer is sagged exactly ONCE per (layer, edit): the tip shows
        this solve, and `_lay_one_layer` commits the very same polylines when the
        layer completes (no second solve, no snap). Returns
        (members, out_polys, out_times, diams, layer_polys, radius, ppd) -- `diams`
        the per-fibre scalar the sag used, `ppd` the per-POINT diameter (the true
        physics value, unclamped) that BOTH the drawn tube/jet AND the lower-layer
        support index size from; for a single flat layer (sag N/A) the sag is a
        passthrough of the relaid centreline."""
        key = (li, self._sched_version)
        if self._sag_key == key:
            return self._sag_rec
        # Unedited layer with a usable cache -> commit the LOCKED `.depo` geometry
        # verbatim (live == locked), bypassing the mew_live relay + sag entirely. The
        # freeze/splice below is skipped (a frozen tip means an edit reached this
        # layer, so it is no longer clean). Only fibre laid after an edit falls
        # through to the re-derive path. See `_layer_locked_clean`.
        if self._layer_locked_clean(li):
            rec = self._locked_layer_rec(li)
            # The cached geometry is already flat on a reset layer, but the
            # ACCUMULATOR still has to forget the structure below -- otherwise a
            # later EDITED layer would solve against pre-reset fibre the export had
            # cleared, and the live sim would drift from the STL above the first
            # reset. rec[5] is this layer's radius. (No-op unless a reset is due.)
            if self._state is not None:
                self._state.maybe_reset(rec[5])
            self._sag_key, self._sag_rec = key, rec
            return rec
        members, paths, times, diams, dlk = self._relay_layer(li)
        if self._state is not None:
            member_loop = [self._loop_entry[i] for i in members]
            # Reset BEFORE the solve so this layer sees the cleared indices and comes
            # out straight, exactly as the batch `add_layer` does. Idempotent: this
            # layer is not committed yet, so a re-solve after an edit re-derives the
            # same datum rather than stacking another reset.
            self._state.maybe_reset(
                min(diams, default=self._max_nominal()) / 2.0)
            # `dlk` (time->diameter, the physics dynamic diameter) makes the BED rest
            # lift each bottom-layer point's centreline by its own radius, so the
            # fibre UNDERSIDE lands on the plate however the diameter varies along it
            # -- the fix for the live diameter-edit "float" (a thinner already-laid
            # stretch no longer sits above a thicker one). None (a manual/non-physics
            # diameter) -> the plain bed plane, byte-identical. Matches the export
            # .depo, which bakes the same lift (apply_sag bed_diam_by_time), so the
            # unedited live sim (which rides the cache) and the export stay equal.
            out_polys, out_times, layer_polys, radius = self._state._solve_layer(
                paths, diams, times, member_loop, bed_diam_by_time=dlk)
        else:                                   # sag N/A -> passthrough centreline
            out_polys, out_times, layer_polys, radius = paths, times, [], 0.0
        # per-point diameter on the SAGGED times (aligns with out_polys), so the
        # diameter forward-accumulates per-point and an edit never repaints the
        # already-laid stretch of the in-progress fibre. One unclamped value drives the
        # tube/jet AND seeds the support, so a fat fibre rests at its true diameter
        # (rest_heights widens its query to find it). See `_per_point_diam`.
        ppd = self._per_point_diam(members, out_times, dlk)
        # Splice the frozen prefix (deposited fibre from before an edit) in front of
        # this re-solve, so already-laid geometry is immutable and only fibre ahead of
        # the edit moves. Empty without an edit on this layer -> a no-edit march never
        # splices and stays byte-identical to the batch solve. See `_freeze_tip_up_to`.
        if self._frozen_tip and li == self._frozen_layer:
            out_polys, out_times, ppd = self._splice_frozen(
                members, out_polys, out_times, ppd)
            layer_polys = [p for p in out_polys if len(p) >= 2]
        rec = (members, out_polys, out_times, diams, layer_polys, radius, ppd)
        self._sag_key, self._sag_rec = key, rec
        return rec

    def _lay_one_layer(self, li):
        """Commit one layer: take its (preview-)solved sag from `_sag_layer` -- the
        same polylines the tip has been showing -- seed the lower-layer index with
        them, and append the committed record. Reuses the tip's solve, so a layer is
        never sagged twice; byte-identical to feeding it to a batch `apply_sag` at
        the same per-fibre diameters."""
        members, out_polys, out_times, diams, layer_polys, radius, ppd = \
            self._sag_layer(li)
        # per-fibre support radius (uniform fibre -> exact scalar == batch; a fibre
        # carrying a mid-layer diameter edit -> per-point) -- so a later layer rests
        # on this one at its LOCAL diameter and a mid-layer edit no longer lifts the
        # whole layer. Built from the true per-point diameter (the SAME one the tube
        # draws), so a fat fibre supports the next layer at its true radius and the
        # crossover sits on top without clipping. Stored so a scrub-back replay
        # re-seeds the same radii.
        support_radii = self._support_radii(ppd, radius)
        if self._state is not None:
            # out_polys carries the degenerate members too; add_fiber skips them, so
            # the indexed set is identical to layer_polys.
            self._state.commit_solved(out_polys, radius, layer_radii=support_radii)
        self._committed.append({
            "li": li, "completion": self._completion[li], "members": members,
            "polys": out_polys, "times": out_times, "diam": diams, "ppd": ppd,
            "support_radii": support_radii})
        if li == self._frozen_layer:        # this layer is now committed -> frozen for good
            self._frozen_tip = {}
            self._frozen_front = None
            self._frozen_layer = None

    def _refresh_tip(self, t):
        """Refresh the in-progress tip: the layer currently being laid (the next to
        commit), SAGGED against the frozen structure below -- so the jet and the
        mesh's leading edge meet the fibre at its RESTING Z, never floating at the
        commanded layer height (the bug this fixes). Cached by (layer, schedule
        version) via `_sag_layer` so a scrub within one layer doesn't re-solve and
        the committed layer reuses it; the reveal cut at `t` (done by the caller)
        hides the part past the playhead."""
        if self._cursor >= self._n_layers:
            self._tip = self._tip_key = None
            return
        li = self._order[self._cursor]
        key = (li, self._sched_version)
        if self._tip_key == key:
            return
        _members, out_polys, out_times, diams, _lp, _r, ppd = self._sag_layer(li)
        # Wrapped HERE, not in the accessors: `_tip` is read only by `tip_fibers` /
        # `tip_ppd`, and this runs once per (layer, schedule version) -- so the tip
        # pays the wrap once per edit rather than once per rendered frame, which is
        # the same bargain `_present_rec` strikes for a committed layer. Everything
        # that CONSUMES the tip internally (`_freeze_tip_up_to`, `_lay_one_layer`)
        # calls `_sag_layer` itself and keeps working in the surface frame.
        wp, wt, wd = self._present(out_polys, out_times, ppd)
        self._tip = (wp, wt, diams, wd)
        self._tip_key = key

    # -- the wrap: surface frame -> world ---------------------------------- #
    def _present(self, polys, times, ppd):
        """Wrap one layer's fibre out of the surface frame and into the collector's
        own co-rotating space -- THE seam between the two coordinate systems, and the
        march's twin of the single one the batch converter has
        (`gcode_to_fibers`, "the wrap").

        Everything upstream -- the relay, the placement, every span and every sag --
        works in the unrolled (u, v, h) surface frame, where a helix is a STRAIGHT
        LINE and the flat solver's geometry is exact. Everything downstream draws in
        space. So the fibre is wrapped HERE, once, on the way OUT to the player, and
        the accumulator below never sees a curved coordinate.

        It DENSIFIES: a segment straight in (u, v) is a helix in space, and its two
        endpoints alone would cut a chord through the collector. That is why the
        per-point diameter is carried through `wrap_path` rather than returned
        unchanged -- the tube and the jet index it positionally against the geometry,
        so a wrapped path beside an unwrapped `ppd` would misalign silently, exactly
        the failure `times` is already carried to avoid.

        Flat (`frame is None`) returns the inputs UNTOUCHED, not copies, so the
        3-axis march pays nothing and stays byte-identical."""
        if self._frame is None:
            return polys, times, ppd
        import mew_frame
        out_p, out_t, out_d = [], [], []
        for p, t, d in zip(polys, times, ppd):
            p = np.asarray(p, dtype=np.float64)
            t = np.asarray(t, dtype=np.float64)
            if len(p) < 2:                      # degenerate fibre: nothing to wrap
                out_p.append(p)
                out_t.append(t)
                out_d.append(d)
                continue
            da = np.asarray(d, dtype=np.float64) if d is not None else None
            if da is not None and da.ndim == 1 and len(da) == len(p):
                wp, wt, (wd,) = mew_frame.wrap_path(self._frame, p, t,
                                                    parallel=[da])
            else:                               # scalar / absent -> rides through
                wp, wt = mew_frame.wrap_path(self._frame, p, t)
                wd = d
            out_p.append(wp)
            out_t.append(wt)
            out_d.append(wd)
        return out_p, out_t, out_d

    def _present_rec(self, rec):
        """`_present` for a COMMITTED layer, memoised on the record. A committed
        layer is frozen for good (a later edit never reaches it), so the wrap is
        computed once per layer and never again -- the same frozen-past property the
        player's per-fibre tube cache relies on. `_truncate_to` drops whole records
        rather than mutating them, so a scrub-back cannot leave a stale memo."""
        w = rec.get("world")
        if w is None:
            w = self._present(rec["polys"], rec["times"], rec["ppd"])
            rec["world"] = w
        return w

    # -- accessors for the player ------------------------------------------ #
    def committed_fibers(self):
        """(paths, times, diams) of every fully-laid SAGGED fibre, ALIGNED TO
        COMMANDED FIBRE INDEX (length == number of commanded fibres; a not-yet-laid
        fibre is None). Index alignment lets the player colour/group each tube by
        its scaffold and compares directly against `apply_sag`'s per-index output.
        Frozen once laid -- a later edit never changes a committed fibre. `diams` is
        one scalar per fibre (the representative diameter its SAG used; the tube/jet
        draw the per-point profile from `committed_ppd`)."""
        n = len(self._cmd_paths)
        paths = [None] * n
        times = [None] * n
        diams = [None] * n
        for rec in self._committed:
            wp, wt, _wd = self._present_rec(rec)
            for k, i in enumerate(rec["members"]):
                paths[i] = wp[k]
                times[i] = wt[k]
                diams[i] = rec["diam"][k]      # a scalar; the wrap cannot move it
        return paths, times, diams

    def committed_ppd(self):
        """Per-POINT drawn diameter of every committed fibre, aligned to commanded
        fibre index (None where not yet laid; one (Ni,) array per laid fibre, parallel
        to `committed_fibers`'s paths/times). The tube + jet size each vertex from this
        so a fibre keeps the diameter it was laid at, even after a later edit."""
        n = len(self._cmd_paths)
        ppd = [None] * n
        for rec in self._committed:
            _wp, _wt, wd = self._present_rec(rec)
            for k, i in enumerate(rec["members"]):
                ppd[i] = wd[k]
        return ppd

    def tip_fibers(self):
        """(members, paths, times, diams) of the in-progress layer's SAGGED
        centreline (rest heights against the frozen structure below -- shown at its
        sagged Z as it is laid, never floating at the commanded layer height). It is
        the SAME geometry this layer commits with, so it settles seamlessly (no
        snap) when complete; the caller's reveal cut hides the part past the
        playhead. `members` are the commanded fibre indices, parallel to the other
        lists; all four are empty at end of print. The per-point drawn diameter is
        `tip_ppd` (parallel to paths)."""
        if self._tip is None:
            return [], [], [], []
        paths, times, diams, _ppd = self._tip
        return list(self._members[self._order[self._cursor]]), \
            list(paths), list(times), list(diams)

    def tip_ppd(self):
        """Per-POINT drawn diameter of the in-progress tip fibres (one (Ni,) array
        per member, parallel to `tip_fibers`'s paths); empty at end of print. Sampled
        on each point's deposition time, so the already-laid stretch keeps its
        diameter across a live edit -- only fibre past the playhead re-sizes."""
        if self._tip is None:
            return []
        return list(self._tip[3])

    def tip_version(self):
        """A token that changes whenever the tip geometry changes (new layer or a
        schedule edit), so the player re-tubes the tip only when it must."""
        return self._tip_key
