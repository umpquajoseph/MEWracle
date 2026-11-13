#!/usr/bin/env python3
"""mew_analysis.py -- the headless engine behind the Viewer's "Show deviation".

Only `deviation_sidecar` (via gui_deviation) is wired into the app. The measure /
predict / parity / fit functions below are kept for scripts and
`analysis_plots`, and are reached by no GUI control.

MEWracle GENERATES a scaffold and RENDERS its predicted physics; this module is the
half that QUANTIFIES that prediction. It answers two strictly-separated questions with
ONE shared set of primitives:

  1. PREDICTED vs COMMANDED (purely computational, always available): geometry measured
     on the commanded centreline (raw g-code + nominal diameter) and on the
     physics-deposited centreline (the `.depo` cache), reported as the DEVIATION --
     placement error, corner rounding, pore-size shift, sag depth, stacked-peak
     reduction, diameter variation.
  2. PREDICTED vs MEASURED (needs real data; the paper-validation axis): run the
     calibrated `MEWModel` at the SAME process parameters a real print used and compare
     its prediction to the measurement -- parity, residuals, R^2 / RMSE.

Both axes reduce to ONE primitive, a residual between a model prediction and a target
value, and the Calibration guided-fit minimises exactly that residual.

HONESTY / scope, kept in code so it never drifts from the laws:
  * Every scalar metric is flagged `calibrated_anchor` when its model value is
    something we FIT (CTS, sag magnitude, fusion overlap) rather than a free prediction
    (diameter, jet-lag). A calibrated anchor is NOT independent evidence against the
    data it was fit to -- its claim is GENERALISATION, hence the
    calibrate-on-subset / validate-on-held-out split. The flag drives UI labelling
    only; it changes no number.
  * Geometry metrics run on the FULL sagged `.depo` centreline, with no lossy
    metric-only decimation. To stay fast without an O(P*S) freeze, the placement /
    corner-cut deviation uses a convergence-gated cap: computed at the cap and at half
    of it, and reported with a `converged` flag from their agreement. The number is
    always proven-stable at the sampling used.
  * Geometry measures are robust on the REGULAR serpentine grids the tool generates. On
    arbitrary imported g-code the pore/corner extraction degrades and the measures
    return None ("n/a") rather than a wrong number.
  * Pure Python + numpy, never Tk/VTK, so the engine runs headless.

Reuse, not reinvention: commanded geometry comes from `gcode_to_fibers.parse_gcode`,
predicted geometry from the existing `.depo` loaders, and the model from
`mew_calibration.models_from_profile` / `build_model` -- identical to what the sim, the
export and the player run, so analysis reads the SAME rig profile.
"""
from __future__ import annotations

import csv
import dataclasses
import io
import math
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import gcode_to_fibers as gtf
import mew_calibration
from mew_physics import MEWModel, ProcessParams, Regime

# The MEWron regulator reads psi; the flow law wants Pa (matches gui_sim /
# gui_calibration). Process-parameter dicts in THIS module hold pressure in Pa --
# the GUI / CSV layer converts psi -> Pa once, at the edge.
PSI_TO_PA = 6894.76

# The process knobs a sweep / sensitivity run can step. `gap` is NOT a
# `ProcessParams` field -- it is the model's working distance -- so it is handled
# by rebuilding the model (see `_model_with_gap`).
PARAM_AXES = ("speed", "pressure", "temperature", "voltage", "gap")

AXIS_LABELS = {
    "speed": ("Print speed", "mm/s"),
    "pressure": ("Pressure", "Pa"),
    "temperature": ("Temperature", "°C"),
    "voltage": ("Voltage", "kV"),
    "gap": ("Gap / standoff", "mm"),
}

# A defensible default operating point (neat-PCL-ish), used only to seed UIs / tests
# when the caller gives no base. Pressure in Pa.
DEFAULT_PARAMS = {
    "speed": 600.0 / 60.0,          # 600 mm/min -> mm/s (the scaffold default feed)
    "pressure": 100.0 * PSI_TO_PA,
    "temperature": 85.0,
    "voltage": 7.0,
    "gap": 4.0,
}


class DepoMismatch(Exception):
    """Raised by `bundle_from_depo` when a `.depo` cache is unreadable/corrupt, or
    when its stamped physics mode contradicts what the caller `expect`ed (so a
    consumer never silently analyses geometry from the wrong deposition mode)."""


# --------------------------------------------------------------------------- #
# Process parameters & scalar prediction
# --------------------------------------------------------------------------- #
def make_process_params(params: dict) -> ProcessParams:
    """A param dict (speed mm/s, pressure Pa, voltage kV, temperature degC) ->
    `ProcessParams`. Missing keys fall back to `DEFAULT_PARAMS`. `gap` is ignored
    here (it lives on the model, not in `ProcessParams`)."""
    g = lambda k: float(params.get(k, DEFAULT_PARAMS[k]))  # noqa: E731
    return ProcessParams(pressure=g("pressure"), voltage=g("voltage"),
                         speed=g("speed"), temperature=g("temperature"))


def _model_with_gap(model: MEWModel, gap) -> MEWModel:
    """Return `model`, or a copy at a different working distance. The gap is the only
    process knob that is not a `ProcessParams` field, so a sweep / sensitivity step
    over `gap` rebuilds the model from its own material / nozzle / calibration."""
    if gap is None:
        return model
    gap = float(gap)
    if gap <= 0.0 or abs(gap - model.working_distance) < 1e-12:
        return model
    return MEWModel(material=model.material, nozzle=model.nozzle,
                    working_distance=gap, calibration=model.calibration)


def solve_point(model: MEWModel, params: dict):
    """`MEWModel.solve` for a param dict, honouring a `gap` override. The one entry
    every scalar metric reads through."""
    m = _model_with_gap(model, params.get("gap"))
    return m.solve(make_process_params(params))


# --------------------------------------------------------------------------- #
# Geometry container
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Fiber:
    """One deposited (or commanded) fibre: an (N,3) XYZ centreline in mm, a
    per-vertex tube RADIUS (N,) in mm (constant for a commanded fibre), the optional
    per-vertex print TIME (N,) in s (predicted fibres carry it; commanded may),
    the inferred `layer` index (0 = first laid) and the scaffold `group` index."""
    xyz: np.ndarray
    radius: np.ndarray
    times: np.ndarray = None
    layer: int = 0
    group: int = 0

    @property
    def xy(self) -> np.ndarray:
        return self.xyz[:, :2]

    @property
    def z(self) -> np.ndarray:
        return self.xyz[:, 2]


@dataclasses.dataclass
class GeometryBundle:
    """A normalised set of fibres + provenance. BOTH the commanded geometry (parsed
    g-code) and the predicted geometry (the `.depo` cache) reduce to this, so the
    geometry metrics never care which side they measure."""
    fibers: list
    source: str = ""
    nominal_diameter: float = 0.0
    physics: dict = dataclasses.field(default_factory=dict)
    group_names: list = dataclasses.field(default_factory=list)

    @property
    def n_layers(self) -> int:
        return (1 + max(f.layer for f in self.fibers)) if self.fibers else 0

    def layer_fibers(self, layer):
        return [f for f in self.fibers if f.layer == layer]


def _assign_layers(fibers, tol):
    """Cluster fibres into layers by representative (median) Z. MEWron lays one
    continuous fibre per layer, so a per-fibre median Z, sorted and split wherever
    the gap exceeds `tol` (~half a layer height), recovers the layer index without
    it being stored. Mutates `f.layer`; returns the layer count."""
    if not fibers:
        return 0
    reps = np.array([float(np.median(f.z)) for f in fibers])
    order = np.argsort(reps)
    layer = 0
    prev = reps[order[0]]
    for rank, idx in enumerate(order):
        if rank and reps[idx] - prev > tol:
            layer += 1
        fibers[idx].layer = layer
        prev = reps[idx]
    return layer + 1


def bundle_from_gcode(gcode_path, nominal_diameter, mode="auto",
                      arc_segments=None, with_travels=False) -> GeometryBundle:
    """Build the COMMANDED bundle: raw deposition centrelines exactly as the g-code
    commands them (sharp corners, flat layers, commanded Z), tubed at the nominal
    diameter, with per-vertex print times. Reuses `gcode_to_fibers.parse_gcode`.

    `with_travels` splices each G0 travel into the front of the fibre it leads into
    (`gcode_to_fibers.merge_travels`, the converter's own splice), matching a .depo
    stamped `render_travel` -- which every travel-aware physics export is, asked or
    not. Fibre count and order are unchanged, so index pairing still holds."""
    physics = {}
    if with_travels:
        paths, ptimes, travels = gtf.parse_gcode(
            gcode_path, mode=mode, with_times=True, arc_segments=arc_segments,
            with_travels=True)
        paths, ptimes = gtf.merge_travels(paths, ptimes, travels)
        physics["render_travel"] = 1.0
    else:
        paths, ptimes = gtf.parse_gcode(gcode_path, mode=mode, with_times=True,
                                        arc_segments=arc_segments)
    r = 0.5 * float(nominal_diameter)
    fibers = []
    for p, t in zip(paths, ptimes):
        p = np.asarray(p, dtype=np.float64)
        if len(p) < 2:
            continue
        fibers.append(Fiber(xyz=p, radius=np.full(len(p), r, dtype=np.float64),
                            times=np.asarray(t, dtype=np.float64)))
    _assign_layers(fibers, tol=max(0.5 * float(nominal_diameter), 1e-4))
    return GeometryBundle(fibers=fibers, source="commanded",
                          nominal_diameter=float(nominal_diameter), physics=physics)


# Order MUST match gcode_to_fibers._depo_cache_meta (the writer). Extra trailing
# keys are safe for old caches: `_read_depo_meta` only reads indices i < len(m), so a
# cache written before a flag existed simply omits it (7 fields originally, 10 since
# the sag-solver choice). NB the WRITER's own load path is stricter -- it rejects a
# short meta outright -- so this leniency is only about READING mode flags back.
_META_KEYS = ("diameter", "no_sag", "sag_multiplier", "self_crossings",
              "transformed", "phys_sag", "phys_fuse", "sag_reset_layers",
              "render_travel", "sag_approx", "depo_generation")


def _read_depo_meta(path) -> dict:
    """Read the `.depo` meta -> a dict of mode flags, so a consumer can say WHICH
    physics produced the cache. Empty dict if unreadable."""
    try:
        with np.load(path, allow_pickle=False) as d:
            m = d["meta"]
    except Exception:  # noqa: BLE001
        return {}
    return {k: float(m[i]) for i, k in enumerate(_META_KEYS) if i < len(m)}


def bundle_from_depo(depo_path, fallback_diameter=0.0, expect=None) -> GeometryBundle:
    """Build the PREDICTED bundle from an existing `.depo.npz`: the physics-deposited
    centrelines (jet-lag, corner rounding, sag, fusion already baked into XYZ), with
    a per-vertex radius from whichever diameter record the cache carries. Reuses the
    converter's own loaders so there is zero recompute.

    Raises `DepoMismatch` when the cache is unreadable/corrupt, or when `expect`
    (a dict of required meta flags, e.g. {"transformed": 1.0}) is given and any flag
    disagrees -- so a caller demanding a PHYSICS deposition never silently gets a
    plain geometric-sag cache (or vice-versa).

    Radius source, priority order: dynamic diameter-by-time -> per-fibre base
    diameter (`load_depo_pdia`) -> `fallback_diameter` (the nominal)."""
    raw = gtf._load_depo_raw(depo_path)
    if raw is None:
        raise DepoMismatch(f"could not read a .depo cache at {depo_path!r}")
    meta = _read_depo_meta(depo_path)
    if expect:
        for k, v in expect.items():
            got = meta.get(k)
            if got is None or abs(float(got) - float(v)) > 1e-9:
                raise DepoMismatch(
                    f"cache mode {k}={got} but expected {v} -- re-run with the "
                    "matching physics mode")
    paths, ptimes = raw
    diam_t = gtf.load_depo_diam_by_time(depo_path)
    pdia = gtf.load_depo_pdia(depo_path)
    groups = gtf.load_depo_groups(depo_path)
    nominal = meta.get("diameter", fallback_diameter) or fallback_diameter

    pgroup, gnames = (groups if groups else (None, []))
    fibers = []
    for i, (p, t) in enumerate(zip(paths, ptimes)):
        p = np.asarray(p, dtype=np.float64)
        t = np.asarray(t, dtype=np.float64)
        if len(p) < 2:
            continue
        if diam_t is not None:
            tk, dk = diam_t
            rad = 0.5 * np.interp(t, tk, dk)
        elif pdia is not None and i < len(pdia):
            rad = np.full(len(p), 0.5 * float(pdia[i]), dtype=np.float64)
        else:
            rad = np.full(len(p), 0.5 * float(nominal), dtype=np.float64)
        grp = int(pgroup[i]) if (pgroup is not None and i < len(pgroup)) else 0
        fibers.append(Fiber(xyz=p, radius=rad, times=t, group=grp))
    _assign_layers(fibers, tol=max(0.5 * float(nominal or 0.05), 1e-4))
    return GeometryBundle(fibers=fibers, source="predicted",
                          nominal_diameter=float(nominal), physics=meta,
                          group_names=list(gnames))


def _half_sampled_idx(bundle: GeometryBundle):
    """`_half_sampled` plus the per-fibre index array it used to stride each fibre.
    The indices let a caller subset a per-vertex quantity already computed on the FULL
    bundle (e.g. the deviation floor, which is per-point and therefore sampling-
    invariant) instead of recomputing it on the half bundle. Returns (bundle, idxs)."""
    fibers = []
    idxs = []
    for f in bundle.fibers:
        n = len(f.xyz)
        if n <= 3:
            fibers.append(f)
            idxs.append(np.arange(n))
            continue
        idx = np.arange(0, n, 2)
        if idx[-1] != n - 1:
            idx = np.append(idx, n - 1)
        fibers.append(Fiber(xyz=f.xyz[idx], radius=f.radius[idx],
                            times=(f.times[idx] if f.times is not None else None),
                            layer=f.layer, group=f.group))
        idxs.append(idx)
    half = GeometryBundle(fibers=fibers, source=bundle.source,
                          nominal_diameter=bundle.nominal_diameter,
                          physics=bundle.physics, group_names=bundle.group_names)
    return half, idxs


def _half_sampled(bundle: GeometryBundle) -> GeometryBundle:
    """A copy of `bundle` with every fibre's vertices strided by 2 (endpoints kept).
    Feeding this to the same metric and comparing to the full result is the
    convergence probe: if the number barely moves, the sampling was fine enough."""
    return _half_sampled_idx(bundle)[0]


# --------------------------------------------------------------------------- #
# Geometry helpers (small, dependency-free, unit-tested)
# --------------------------------------------------------------------------- #
def _seg_point_dist2(px, py, ax, ay, bx, by):
    """Squared distance from points (px,py) to segment a->b, fully broadcast."""
    abx, aby = bx - ax, by - ay
    apx, apy = px - ax, py - ay
    denom = abx * abx + aby * aby
    t = np.where(denom > 0.0,
                 (apx * abx + apy * aby) / np.where(denom > 0.0, denom, 1.0), 0.0)
    t = np.clip(t, 0.0, 1.0)
    cx, cy = ax + t * abx, ay + t * aby
    dx, dy = px - cx, py - cy
    return dx * dx + dy * dy


# The point-to-polyline distance runs the FULL sagged centreline against its matched
# commanded polyline. A small P*S is one vectorised broadcast of the P query points
# against all S segments. A large one (a 30 mm layer: ~62k points x ~1,150 segments)
# first prunes, per block of points, every segment that provably cannot be nearest
# (`_point_to_polyline_pruned`). The per-point uniform-grid index that used to serve
# the large case ran a Python loop per point, ~9 s per 30 mm layer, and a diagonal
# (60 deg) pass's bounding box filled so many cells that it scanned most segments
# anyway; with 8 threads holding the GIL it froze the GUI for minutes.
_PRUNE_CHUNK = 128           # consecutive query points per pruned block
_PRUNE_EPS = 1e-9            # mm, slack on the prune bound for rounding in d(c,seg)
_BRUTE_CHUNK = 400_000       # cap on P_chunk*S per broadcast (memory bound)


def _point_to_polyline_brute(pts, ax, ay, bx, by):
    S = len(ax)
    out = np.empty(len(pts), dtype=np.float64)
    step = max(1, _BRUTE_CHUNK // max(1, S))
    for s in range(0, len(pts), step):
        px = pts[s:s + step, 0][:, None]
        py = pts[s:s + step, 1][:, None]
        d2 = _seg_point_dist2(px, py, ax[None, :], ay[None, :], bx[None, :], by[None, :])
        out[s:s + step] = np.sqrt(d2.min(axis=1))
    return out


def _point_to_polyline_pruned(pts, ax, ay, bx, by):
    """Exact min distance from each of `pts` to the segments, for a large P*S. Points
    go in blocks of `_PRUNE_CHUNK` consecutive vertices (path order, so a block is
    spatially compact). For a block with bbox centre c and half-diagonal R, every
    point p satisfies |d(p,seg) - d(c,seg)| <= R, so a segment with
    d(c,seg) > min d(c,.) + 2R can never be any point's nearest and is dropped before
    the broadcast. The kept set always holds every point's true nearest, and each
    distance is the same elementwise float as `_point_to_polyline_brute`, so the
    result is BYTE-IDENTICAL to it at a fraction of the cost."""
    out = np.empty(len(pts), dtype=np.float64)
    for s in range(0, len(pts), _PRUNE_CHUNK):
        blk = pts[s:s + _PRUNE_CHUNK]
        lo = blk.min(axis=0); hi = blk.max(axis=0)
        c = 0.5 * (lo + hi)
        R = 0.5 * math.hypot(hi[0] - lo[0], hi[1] - lo[1])
        dc = np.sqrt(_seg_point_dist2(c[0], c[1], ax, ay, bx, by))
        keep = np.flatnonzero(dc <= dc.min() + 2.0 * R + _PRUNE_EPS)
        out[s:s + len(blk)] = _point_to_polyline_brute(
            blk, ax[keep], ay[keep], bx[keep], by[keep])
    return out


def _point_to_polyline(points_xy, poly_xy):
    """Min XY distance from each of `points_xy` (P,2) to the polyline `poly_xy`
    (S+1,2). One vectorised broadcast when P*S is small; the pruned blocks otherwise
    (same values either way)."""
    poly = np.asarray(poly_xy, dtype=np.float64)
    pts = np.asarray(points_xy, dtype=np.float64)
    if len(poly) < 2 or len(pts) == 0:
        return np.full(len(pts), np.inf)
    S = len(poly) - 1
    kernel = (_point_to_polyline_brute if len(pts) * S <= _BRUTE_CHUNK
              else _point_to_polyline_pruned)
    return kernel(pts, poly[:-1, 0], poly[:-1, 1], poly[1:, 0], poly[1:, 1])


# THE DEVIATION METRIC -- the design note. `_deviation_crosstrack` states the contract
# and `harness_deviation.py` (cases B2/B3/F) is the ground truth.
# A predicted fibre preserves parametric ORDER, so a predicted vertex is compared to
# the commanded path around its OWN parametric position, corresponded by print TIME --
# never to the globally nearest commanded segment, which on a fold-back matches a
# parametrically distant loop and reports a wrong, small deviation.

# Same-time alone is not enough on a curved path: a purely TRAILING deposit is offset
# from its same-time commanded point by a chord with a real perpendicular component.
# The harness measured that phantom at up to the full lag (0.499 mm of a 0.5 mm lag on
# a sharp wave, 0.232 mm at a 180-deg turnaround), inflating 82% of vertices on a real
# scaffold, so a per-fibre transport delay is fitted and removed before projecting.

# The window terms are sized off SAMPLE SPACING and the sag solver's native density
# varies ~7x between a flat and a sagging layer, so two byte-identical-geometry layers
# read differently -- hence the canonical uniform arc-length grid at one fibre diameter
# (features below a diameter are physically unresolvable), read back at the native
# vertices so the returned array stays 1:1 with them. `_deviation_arclen` below is the
# LEGACY arc-length-windowed fallback for a bundle with no usable per-vertex times.
_DEV_WIN_FRAC = 0.6          # window half-width as a fraction of the longest cmd seg
_DEV_WIN_FLOOR = 0.3         # mm, minimum window (tiny scaffolds)
_DEV_COLOR_PCT = 99.0        # percentile the deviation-view LUT caps its red end at
_DEV_NOISE_ULP_K = 8         # colour-cap noise floor multiplier. Half-ulp of float32
                             # storage is |c|*2^-24 per axis; K=8 covers the xy
                             # combination + the interp arithmetic headroom. The cap is
                             # floored at K*2^-24*max|coord| so a physics-OFF scaffold --
                             # whose only deviation is float32 storage dust the p99 would
                             # otherwise stretch to full red -- stays deep blue (the LUT's
                             # white band opens at vcap/2, so dust <= floor/K is well below
                             # it). Only the COLOUR cap is floored; dev/vmax (grading +
                             # readout) stay honest.
_TAU_FIT_MAX = 800           # cap on vertices used for the transport-delay fit objective
_DEV_GRID_CAP = 40000        # max points on the canonical arc-length grid: the diameter-
                             # scale step is floored at arc_length/_DEV_GRID_CAP so a
                             # pathologically long fibre can't explode the grid (the grid
                             # is normally FEWER points than the fine native sampling).
_DEV_TANGENT_DIAMS = 4       # tangent window half-width on the canonical grid, in fibre
                             # diameters -- a FIXED PHYSICAL length, decoupled from the
                             # grid step. A deposited MEW path cannot bend at diameter
                             # scale (its minimum radius is the jet's R_min, ~1.7 mm at
                             # a 4.5 mm gap) while the .depo stores chords up to ~30 um
                             # whose DIRECTION errs by ~chord/2R. A window above the
                             # worst chord scale and below the real feature scale
                             # (~46 um at d=11.4 um, against ~28 um native spacing on a
                             # coarse layer) averages chord noise without blurring it.
                             # A {2..10}-diameter sweep showed the metric is INSENSITIVE
                             # here, so 4 is the grounded mid-value: 2 sits below the
                             # worst chord and >=6 starts blurring the tightest loops.


def _cum_arc(xy):
    """Per-vertex cumulative arc length along a polyline (0..L). Returns (cum, seglen)."""
    seg = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))
    return np.concatenate([[0.0], np.cumsum(seg)]), seg


def _deviation_crosstrack(cf_xy, cf_t, pf_xy, pf_t, floor=None, step=None):
    """Per-vertex CROSS-TRACK deviation of the deposited fibre `pf_xy` from its matched
    commanded fibre `cf_xy`, corresponded by print TIME (`cf_t`, `pf_t`, same clock).

    For each deposited vertex: interpolate the commanded position at that vertex's print
    time, then take the offset PERPENDICULAR to the deposited fibre's local tangent.
    Time is the rounding-INVARIANT parameter -- unlike arc length it does not drift when
    corner rounding shortens the deposited path -- and a parametrically distant fold is
    far away in time, so it can never steal the match. The cross-track component
    discards the purely tangential jet-lag while still registering a flattened
    wave/corner that hugs the commanded legs but misses the apex.

    A per-fibre transport delay `tau_hat` is FITTED and removed first, so the commanded
    reference is the point the jet was ACTUALLY trailing and only true lateral error
    remains. The search prefers no shift and is capped by the TANGENTIAL component of
    the offset, so a purely perpendicular shape error cannot manufacture a delay.

    Returns a (P,) array, or None when the commanded times are unusable so the caller
    can fall back to `_deviation_arclen`. `floor` is the ONE expensive term; pass it to
    skip recomputing it -- it is sampling-invariant, so a convergence probe on a strided
    subset reuses the full-bundle floor indexed. `step` (mm), when given, resamples the
    projection/despike terms onto a canonical uniform arc-length grid before reading
    them back at the native vertices, so the metric is a property of the GEOMETRY rather
    than of how densely the sag solver sampled it."""
    cf_xy = np.asarray(cf_xy, dtype=np.float64)
    pf_xy = np.asarray(pf_xy, dtype=np.float64)
    ct = np.asarray(cf_t, dtype=np.float64)
    pt = np.asarray(pf_t, dtype=np.float64)
    P = len(pf_xy)
    if P < 1:
        return np.zeros(0)
    if len(cf_xy) < 2 or len(ct) != len(cf_xy) or len(pt) != P:
        return None
    # np.interp needs a strictly-increasing sample axis; drop zero-length (equal-time)
    # commanded vertices -- their geometry is redundant. Need >= 2 distinct knots.
    keep = np.concatenate([[True], np.diff(ct) > 0.0])
    ct = ct[keep]
    cxy = cf_xy[keep]
    if len(ct) < 2:
        return None
    cx0 = cxy[:, 0]
    cy0 = cxy[:, 1]
    t0, t1 = ct[0], ct[-1]
    span = t1 - t0

    # The projection/despike pipeline, run on WHATEVER (xy, t) it is handed -- native
    # vertices or the canonical grid below. Everything here is sized off the SAMPLE
    # SPACING (the p90-segment tangent window, the 5-vertex despike), which is exactly
    # why it must run on a geometry-scaled grid rather than the sag solver's variable
    # native density (see the design note above `_deviation_crosstrack`).
    def _core(wxy, wt, win=None):
        n = len(wxy)
        # Deposited local tangent, unit-normalised: serves the transport-delay fit gate
        # and the final cross-track projection. A naive per-index tangent is noisy (the
        # sampling is very non-uniform and the tangential jet-lag leaks into the normal
        # projection -> per-vertex banding), so it is a central difference over a WINDOW:
        # `win` when given (the grid path passes the FIXED physical _DEV_TANGENT_DIAMS
        # window -- see the constant's grounding note; the resampled positions still
        # carry the source trace's chord-direction noise, which a window at the grid
        # step would follow facet by facet), else ~2x the coarsest typical sample
        # spacing (p90 segment length -- the legacy native-path behaviour).
        seg = np.hypot(np.diff(wxy[:, 0]), np.diff(wxy[:, 1]))
        pos = seg[seg > 0.0]
        if n >= 3 and len(pos):
            s2 = np.concatenate([[0.0], np.cumsum(seg)])
            w = float(win) if win else 2.0 * float(np.percentile(pos, 90))
            idx = np.arange(n)
            lo = np.clip(np.searchsorted(s2, s2 - w, "left"), 0, n - 1)
            hi = np.clip(np.searchsorted(s2, s2 + w, "right") - 1, 0, n - 1)
            bad = lo >= hi                       # window collapsed (tiny w) -> neighbours
            lo = np.where(bad, np.clip(idx - 1, 0, n - 1), lo)
            hi = np.where(bad, np.clip(idx + 1, 0, n - 1), hi)
            tx = wxy[hi, 0] - wxy[lo, 0]
            ty = wxy[hi, 1] - wxy[lo, 1]
        else:                                    # degenerate (all coincident / < 3 pts)
            tx = np.ones(n)
            ty = np.zeros(n)
        tl = np.hypot(tx, ty)
        tl[tl < 1e-12] = 1.0
        tx = tx / tl
        ty = ty / tl
        # --- fit + remove the per-fibre transport delay tau_hat (see the design note) ---
        # A purely trailing deposit on a CURVED path shows a phantom cross-track = lag*(1-
        # cos(heading change over the lag)); fit the best-fit pure time shift explaining the
        # deposited positions and compare against the commanded point tau_hat EARLIER. The
        # search window is bounded by the TANGENTIAL offset (a delay is along-track), so a
        # purely PERPENDICULAR shape error (a flattened wave) keeps tau_hat ~ 0 and still
        # colours; without that gate the total-distance search aliases a shift onto a
        # coincidental commanded zero-crossing and cancels real cross-track error.
        tau_hat = 0.0
        if n >= 3 and span > 0.0:
            q0 = np.clip(wt, t0, t1)
            ox = wxy[:, 0] - np.interp(q0, ct, cx0)
            oy = wxy[:, 1] - np.interp(q0, ct, cy0)
            o_along = np.abs(ox * tx + oy * ty)          # tangential component (the lag)
            o99 = float(np.percentile(o_along, 99))
            cseg = np.hypot(np.diff(cx0), np.diff(cy0))
            cdt = np.diff(ct)                             # all > 0 (ct strictly increasing)
            v_med = float(np.median(cseg / cdt)) if len(cdt) else 0.0
            tau_cap = min(2.0 * o99 / max(v_med, 1e-9), 0.45 * span)
            if np.isfinite(tau_cap) and tau_cap > 0.0 and o99 > 0.0:
                taus = np.linspace(0.0, tau_cap, 65)      # includes tau = 0 (no shift)
                if taus[1] - taus[0] > 0.0:
                    # J(tau) = median sq distance over UNCLAMPED vertices, vectorised over
                    # the 65 shifts; the objective rides at most _TAU_FIT_MAX strided
                    # vertices (the fit is one robust scalar), the final deviation uses all.
                    stepn = max(1, -(-n // _TAU_FIT_MAX))  # ceil(n / cap) -> F <= cap
                    fpt = wt[::stepn]
                    fpx = wxy[::stepn, 0]
                    fpy = wxy[::stepn, 1]
                    F = len(fpt)
                    sh = fpt[None, :] - taus[:, None]      # (65, F)
                    within = (sh >= t0) & (sh <= t1)
                    cxq = np.interp(sh.ravel(), ct, cx0).reshape(65, F)
                    cyq = np.interp(sh.ravel(), ct, cy0).reshape(65, F)
                    d2 = (fpx[None, :] - cxq) ** 2 + (fpy[None, :] - cyq) ** 2
                    d2 = np.where(within, d2, np.nan)      # drop clamped vertices
                    cnt = within.sum(axis=1)
                    with np.errstate(all="ignore"):
                        Js = np.where(cnt >= 8, np.nanmedian(d2, axis=1), np.inf)
                    if np.isfinite(Js).any():
                        jmin = float(np.nanmin(Js[np.isfinite(Js)]))
                        thresh = 1.05 * jmin + 1e-12
                        # SMALLEST tau within 5% of the best -> prefer no shift (design intent)
                        cand = np.nonzero(Js <= thresh)[0]
                        if len(cand):
                            tau_hat = float(taus[cand[0]])
        q = np.clip(wt - tau_hat, t0, t1)
        cx = np.interp(q, ct, cx0)
        cy = np.interp(q, ct, cy0)
        dx = wxy[:, 0] - cx
        dy = wxy[:, 1] - cy
        cd = np.abs(dx * (-ty) + dy * tx)        # component along the deposited normal
        # De-noise the PROJECTION term (only): a short median rejects isolated tangent-
        # noise spikes while preserving a sustained excess (a genuinely flattened wave/
        # corner). On the canonical grid this is a fixed ~5-diameter world-scale despike.
        if n >= 5:
            _pad = np.pad(cd, 2, mode="edge")
            cd = np.median(np.stack([_pad[k:k + n] for k in range(5)]), axis=0)
        return cd

    # FLOOR (native): a point's deviation can never read less than its distance to the
    # ENTIRE commanded polyline -- "it's just jet-lag" requires actually lying ON the
    # commanded path. Computed on and re-applied to the NATIVE vertices so the invariant
    # (and the sidecar's 1:1-with-native contract) holds exactly.
    if floor is None:
        floor = _point_to_polyline(pf_xy, cf_xy)
    # native cumulative arc length -- drives the resample decision + the read-back.
    nseg = np.hypot(np.diff(pf_xy[:, 0]), np.diff(pf_xy[:, 1]))
    s = np.concatenate([[0.0], np.cumsum(nseg)])
    L = float(s[-1])
    h = float(step) if (step is not None and step > 0.0) else 0.0
    if h <= 0.0 or L < 3.0 * h:
        # no step given, or a tiny fibre (< 3 grid steps): run on the native vertices as
        # before (tiny fibres were never the sampling-density problem -- the fix is a
        # no-op for them, and the native path stays byte-identical to the pre-grid code).
        return np.maximum(_core(pf_xy, pt), floor)
    # Canonical uniform arc-length grid. Dedupe zero-length runs before interpolating
    # (np.interp needs a strictly-increasing sample axis) -- same keep-first pattern as
    # the commanded-time dedupe above; pt is non-decreasing along the fibre.
    keep_s = np.concatenate([[True], np.diff(s) > 0.0])
    sd, pxd, pyd, ptd = s[keep_s], pf_xy[keep_s, 0], pf_xy[keep_s, 1], pt[keep_s]
    sg = np.arange(0.0, L, h)
    if len(sg) == 0 or sg[-1] < L:
        sg = np.append(sg, L)                    # always carry the endpoint
    if len(sg) < 8 or len(sd) < 2:               # too few grid points -> native
        return np.maximum(_core(pf_xy, pt), floor)
    gxy = np.column_stack([np.interp(sg, sd, pxd), np.interp(sg, sd, pyd)])
    gt = np.interp(sg, sd, ptd)
    # grid cross-track (fixed physical tangent window), read back at the native vertices
    cd_native = np.interp(s, sg, _core(gxy, gt, win=_DEV_TANGENT_DIAMS * h))
    return np.maximum(cd_native, floor)


def _deviation_arclen(cf_xy, pf_xy):
    """LEGACY fallback (see the note above `_deviation_crosstrack`): per-vertex lateral
    distance of `pf_xy` from the PARAMETRICALLY-corresponding portion of `cf_xy` (an
    arc-length window), so a fold-back in the commanded path can't steal the match.
    Vectorised in blocks of predicted vertices (their correspondence arc positions are
    monotonic, so each block reads a contiguous slice of commanded segments)."""
    cf_xy = np.asarray(cf_xy, dtype=np.float64)
    pf_xy = np.asarray(pf_xy, dtype=np.float64)
    S = len(cf_xy) - 1
    P = len(pf_xy)
    if S < 1 or P < 1:
        return np.zeros(P)
    c_cum, c_seg = _cum_arc(cf_xy)
    Lc = float(c_cum[-1])
    if Lc <= 0.0:
        return np.zeros(P)
    c_center = 0.5 * (c_cum[:-1] + c_cum[1:])            # (S,) monotonic
    ax, ay = cf_xy[:-1, 0], cf_xy[:-1, 1]
    bx, by = cf_xy[1:, 0], cf_xy[1:, 1]
    W = max(_DEV_WIN_FLOOR, _DEV_WIN_FRAC * float(c_seg.max()))
    W = min(W, 0.25 * Lc)                               # never window a degenerate path
    p_cum, _ = _cum_arc(pf_xy)
    Lp = float(p_cum[-1]) or 1.0
    sc = (p_cum / Lp) * Lc                              # predicted -> commanded arc (P,)
    out = np.empty(P, dtype=np.float64)
    # Chunk predicted vertices by ARC SPAN (<= 0.5*W), not a fixed count: then the
    # commanded slice a chunk reads ([min(sc)-W, max(sc)+W]) is a TIGHT superset of
    # every point's own +/-W window (span <= 2.5*W), so no point ever sees a
    # parametrically-distant fold (which sits >= a couple of passes = several*W away).
    # A fixed vertex block would span the whole path on short fibres and re-admit the
    # fold -- the bug this replaces.
    step = 0.5 * W
    i = 0
    while i < P:
        j = int(np.searchsorted(sc, sc[i] + step, "right"))
        j = max(j, i + 1)
        lo = int(np.searchsorted(c_center, sc[i] - W, "left")) - 1
        hi = int(np.searchsorted(c_center, sc[j - 1] + W, "right")) + 1
        lo = max(0, lo); hi = min(S, max(lo + 1, hi))
        px = pf_xy[i:j, 0][:, None]
        py = pf_xy[i:j, 1][:, None]
        d2 = _seg_point_dist2(px, py, ax[None, lo:hi], ay[None, lo:hi],
                              bx[None, lo:hi], by[None, lo:hi])
        out[i:j] = np.sqrt(d2.min(axis=1))
        i = j
    return out


# The deviation floor (`_point_to_polyline`) is the ONE heavy term -- ~99% of the
# analysis wall-clock on a dense scaffold -- and it is per-fibre-independent, so the
# floors compute concurrently. The kernel is a big vectorised numpy broadcast that
# releases the GIL, so a thread pool gives a real (~2.6x measured) speedup with
# BYTE-IDENTICAL output; threads (not processes) avoid pickling the fibre arrays.
_DEV_MAX_THREADS = 8            # measured plateau (memory-bandwidth bound past ~8)


def _dev_floor_one(pair):
    cf, pf = pair
    if len(pf.xyz) < 2 or len(cf.xyz) < 2:
        return None              # too short for cross-track -> no floor needed
    return _point_to_polyline(pf.xy, cf.xy)


def _dev_floors(pairs):
    """Per-fibre point-to-commanded-polyline floor, one entry per pair (None where the
    pair is too short for cross-track). Threaded across fibres (see the note above)."""
    if not pairs:
        return []
    workers = min(len(pairs), (os.cpu_count() or 1), _DEV_MAX_THREADS)
    if workers <= 1:
        return [_dev_floor_one(p) for p in pairs]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(_dev_floor_one, pairs))


def deviation_floors(commanded: GeometryBundle, predicted: GeometryBundle):
    """The per-fibre deviation floors (`_dev_floors`) for a bundle pair, or None when
    the fibre counts differ. A caller computes this once and threads it into BOTH
    `_deviation_by_fiber(floors=)` and `compare_geometry(floors=)` so the heavy floor
    (and its convergence-probe recompute) is paid exactly once."""
    pairs = _match_fibers(commanded, predicted)
    return None if pairs is None else _dev_floors(pairs)


def _deviation_by_fiber(commanded: GeometryBundle, predicted: GeometryBundle,
                        floors=None):
    """The per-vertex lateral deviation of each PREDICTED fibre from the parametrically
    corresponding part of its matched commanded polyline -- a list of (Ni,) arrays, one
    per predicted fibre, in fibre order. THE single expensive geometry computation:
    placement RMS/max, the per-layer strip, and the deviation sidecar all derive from
    it, so the GUI computes it once and threads it through (dev_by_fiber=) instead of
    paying for three passes. Uses time-frame cross-track correspondence
    (`_deviation_crosstrack`) so rounding never drifts the match and a fold-back never
    steals it; falls back to the arc-length window (`_deviation_arclen`) only when a
    fibre carries no usable per-vertex times. None when the fibre counts differ.

    The dominant cost -- the per-fibre floor -- is computed in parallel (`_dev_floors`)
    and can be supplied via `floors` (aligned to the fibre pairs) so a caller that
    already holds the full-bundle floors reuses them; the convergence probe passes the
    full floors indexed to its strided subset, since the floor is sampling-invariant."""
    # A travel-rendered cache (render_travel) merges each G0 travel into the FRONT of
    # its fibre. Against a commanded bundle parsed WITHOUT travels every fibre lead-in
    # would mis-score (polluting placement RMS/max and the sidecar cap), so that pairing
    # is refused LOUDLY; build the commanded side with `bundle_from_gcode(
    # with_travels=True)` -- `deviation_sidecar` does -- and the two sides match.
    if (predicted.physics.get("render_travel")
            and not commanded.physics.get("render_travel")):
        raise DepoMismatch(
            "this .depo carries travel (G0) moves as deposited fibre but the "
            "commanded bundle was parsed without them -- build it with "
            "bundle_from_gcode(with_travels=True)")
    pairs = _match_fibers(commanded, predicted)
    if pairs is None:
        return None
    if floors is None:
        floors = _dev_floors(pairs)
    out = []
    for (cf, pf), fl in zip(pairs, floors):
        n = len(pf.xyz)
        if n < 1:
            out.append(np.zeros(0))
            continue
        if n < 2 or len(cf.xyz) < 2:
            out.append(np.zeros(n))
            continue
        d = None
        if cf.times is not None and pf.times is not None:
            # Canonical grid step = one fibre diameter (features below a diameter are
            # physically unresolvable), floored at arc_length/_DEV_GRID_CAP so a
            # pathological fibre can't explode the grid. See _deviation_crosstrack.
            rmed = float(np.median(pf.radius)) if len(pf.radius) else 0.0
            hstep = max(2.0 * rmed, 1e-4)
            arclen = float(np.hypot(np.diff(pf.xy[:, 0]), np.diff(pf.xy[:, 1])).sum())
            hstep = max(hstep, arclen / _DEV_GRID_CAP)
            d = _deviation_crosstrack(cf.xy, cf.times, pf.xy, pf.times, floor=fl,
                                      step=hstep)
        if d is None:                                    # no usable times -> legacy path
            d = _deviation_arclen(cf.xy, pf.xy)
        d = np.asarray(d, dtype=np.float64)
        d[~np.isfinite(d)] = 0.0
        out.append(d)
    return out


def _match_fibers(commanded: GeometryBundle, predicted: GeometryBundle):
    """Pair commanded fibre i with predicted fibre i. The physics transform reshapes
    each fibre in place (resampling vertices) but preserves fibre count and order, so
    index matching is correct when the counts agree. Returns the pair list, or None
    if the counts differ (the caller then reports the deviation as unavailable)."""
    if len(commanded.fibers) != len(predicted.fibers):
        return None
    return list(zip(commanded.fibers, predicted.fibers))


def _label_4(mask):
    """4-connectivity connected-component labels of a boolean grid via union-find.
    Returns an int label array (-1 where False). Dependency-free (no scipy)."""
    h, w = mask.shape
    n = h * w
    parent = np.arange(n, dtype=np.int64)

    def find(a):
        root = a
        while parent[root] != root:
            root = parent[root]
        while parent[a] != root:
            parent[a], a = root, parent[a]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    ids = np.arange(n).reshape(h, w)
    hsel = mask[:, :-1] & mask[:, 1:]
    for a, b in zip(ids[:, :-1][hsel].tolist(), ids[:, 1:][hsel].tolist()):
        union(a, b)
    vsel = mask[:-1, :] & mask[1:, :]
    for a, b in zip(ids[:-1, :][vsel].tolist(), ids[1:, :][vsel].tolist()):
        union(a, b)

    labels = np.full(n, -1, dtype=np.int64)
    flat = mask.ravel()
    for a in np.nonzero(flat)[0].tolist():
        labels[a] = find(a)
    return labels.reshape(h, w)


def _rasterize_open(bundle: GeometryBundle, max_px=320):
    """Rasterise the top-down (XY) projection of all fibres into a boolean COVERED
    grid, then return (open_mask, covered_mask, pixel_mm). A pixel is covered if it
    is within a fibre's radius of any centreline; the open mask is its complement.
    Resolution capped at `max_px` on the long axis so the cost is bounded regardless
    of fibre count. Returns None if empty/degenerate. Fully vectorised across all
    segments of all fibres (the per-segment Python loop took ~30s on a dense grid)."""
    fibers = [f for f in bundle.fibers if len(f.xyz) >= 2]
    if not fibers:
        return None
    allxy = np.concatenate([f.xy for f in fibers])
    lo = allxy.min(axis=0)
    hi = allxy.max(axis=0)
    span = hi - lo
    long = float(max(span[0], span[1]))
    if long <= 0.0:
        return None
    pixel = long / float(max_px)
    rmax = max((float(np.max(f.radius)) for f in fibers), default=0.0)
    pad = rmax + 2.0 * pixel
    lo = lo - pad
    hi = hi + pad
    w = min(int(math.ceil((hi[0] - lo[0]) / pixel)) + 1, 4 * max_px)
    h = min(int(math.ceil((hi[1] - lo[1]) / pixel)) + 1, 4 * max_px)
    covered = np.zeros((h, w), dtype=bool)
    a_list, b_list = [], []
    for f in fibers:
        xy = f.xy
        if len(xy) >= 2:
            a_list.append(xy[:-1])
            b_list.append(xy[1:])
    if a_list:
        a = np.concatenate(a_list)
        b = np.concatenate(b_list)
        seglen = np.hypot(b[:, 0] - a[:, 0], b[:, 1] - a[:, 1])
        nsub = np.maximum(1, np.ceil(seglen / (0.5 * pixel)).astype(np.int64))
        cnt = nsub + 1
        seg = np.repeat(np.arange(a.shape[0], dtype=np.int64), cnt)
        t = ((np.arange(seg.size, dtype=np.float64)
              - np.repeat(np.cumsum(cnt) - cnt, cnt)) / np.repeat(nsub, cnt))
        sx = a[seg, 0] + (b[seg, 0] - a[seg, 0]) * t
        sy = a[seg, 1] + (b[seg, 1] - a[seg, 1]) * t
        gx = np.clip(((sx - lo[0]) / pixel).astype(np.int64), 0, w - 1)
        gy = np.clip(((sy - lo[1]) / pixel).astype(np.int64), 0, h - 1)
        covered[gy, gx] = True
    rad_px = int(math.ceil(rmax / pixel)) if rmax > 0 else 1
    for _ in range(max(1, rad_px)):
        d = covered.copy()
        d[:, 1:] |= covered[:, :-1]
        d[:, :-1] |= covered[:, 1:]
        d[1:, :] |= covered[:-1, :]
        d[:-1, :] |= covered[1:, :]
        covered = d
    return (~covered), covered, pixel


# --------------------------------------------------------------------------- #
# Geometry measures -- on one bundle
# --------------------------------------------------------------------------- #
def measure_diameter_mean(bundle: GeometryBundle):
    rs = [f.radius for f in bundle.fibers if len(f.radius)]
    if not rs:
        return None
    return float(2.0 * np.mean(np.concatenate(rs)))


def measure_diameter_cv(bundle: GeometryBundle):
    rs = [f.radius for f in bundle.fibers if len(f.radius)]
    if not rs:
        return None
    d = 2.0 * np.concatenate(rs)
    m = float(np.mean(d))
    return float(np.std(d) / m) if m > 0 else 0.0


def measure_sag_depth(bundle: GeometryBundle, reducer="mean"):
    """Per-fibre downward excursion below its own resting plane (the fibre's median
    Z), averaged (or max'd). A flat commanded fibre gives ~0; a sagged predicted
    fibre gives its midspan droop. Stacking RAISES Z, so the per-fibre median datum
    isolates the downward (sag) component."""
    depths = []
    for f in bundle.fibers:
        if len(f.z) < 2:
            continue
        if len(f.z) < 3:
            depths.append(0.0)
            continue
        plane = float(np.median(f.z))
        depths.append(plane - float(np.min(f.z)))
    if not depths:
        return None
    depths = np.array(depths)
    return float(depths.max() if reducer == "max" else depths.mean())


def measure_stack_height(bundle: GeometryBundle):
    """Total built height: top of the highest fibre minus bottom of the lowest
    (radius included). Fusion compaction lowers this below N*d."""
    tops, bots = [], []
    for f in bundle.fibers:
        if not len(f.z):
            continue
        tops.append(float(np.max(f.z + f.radius)))
        bots.append(float(np.min(f.z - f.radius)))
    if not tops:
        return None
    return float(max(tops) - min(bots))


def measure_stack_ratio(bundle: GeometryBundle):
    """Built height / (N_layers * nominal_d). ~1 for an un-fused stack; < 1 when
    junction fusion squishes layers together."""
    h = measure_stack_height(bundle)
    n = bundle.n_layers
    d = bundle.nominal_diameter
    if h is None or n <= 0 or d <= 0:
        return None
    return float(h / (n * d))


def _interior_pores(bundle: GeometryBundle):
    """Shared pore extraction: equivalent-diameter (mm) of every INTERIOR open cell
    (open components not touching the border -> the true windows), plus the footprint
    pixel count and pixel size. Returns (pore_diams_mm, footprint_px, interior_px) or
    None."""
    ras = _rasterize_open(bundle)
    if ras is None:
        return None
    open_mask, _covered, pixel = ras
    labels = _label_4(open_mask)
    border = set(labels[0, :].tolist()) | set(labels[-1, :].tolist()) \
        | set(labels[:, 0].tolist()) | set(labels[:, -1].tolist())
    border.discard(-1)
    ids, counts = np.unique(labels[labels >= 0], return_counts=True)
    pore_diams, interior_px = [], 0
    h, w = open_mask.shape
    for lid, cnt in zip(ids.tolist(), counts.tolist()):
        if lid in border:
            continue
        interior_px += cnt
        area = cnt * pixel * pixel
        pore_diams.append(2.0 * math.sqrt(area / math.pi))
    footprint_px = int(h * w - sum(
        c for lid, c in zip(ids.tolist(), counts.tolist()) if lid in border))
    return np.array(pore_diams), footprint_px, interior_px


def measure_pore_size(bundle: GeometryBundle, _pores=None):
    res = _pores if _pores is not None else _interior_pores(bundle)
    if res is None or len(res[0]) == 0:
        return None
    return float(np.mean(res[0]))


def measure_pore_cv(bundle: GeometryBundle, _pores=None):
    res = _pores if _pores is not None else _interior_pores(bundle)
    if res is None or len(res[0]) < 2:
        return None
    m = float(np.mean(res[0]))
    return float(np.std(res[0]) / m) if m > 0 else 0.0


def measure_porosity(bundle: GeometryBundle, _pores=None):
    """Open-area fraction within the scaffold footprint (the top-down projected
    porosity a micrograph reads). 0..1."""
    res = _pores if _pores is not None else _interior_pores(bundle)
    if res is None:
        return None
    _, footprint_px, interior_px = res
    if footprint_px <= 0:
        return None
    return float(interior_px / footprint_px)


# --------------------------------------------------------------------------- #
# Geometry measures -- comparing the two bundles (deviation metrics)
# --------------------------------------------------------------------------- #
def _decimate_xy(xy, cap):
    """Down-sample a polyline to at most `cap` vertices (uniform stride, endpoints
    kept). DISPLAY-ONLY -- never used inside a metric (the original tab's trust bug
    was decimating the metric path). Figures / the deviation sidecar use it to keep
    rendered vertex counts sane."""
    if len(xy) <= cap:
        return xy
    idx = np.linspace(0, len(xy) - 1, cap).astype(np.int64)
    return xy[idx]


# (Deviation metrics run on the FULL centreline -- the vectorised kernel above makes
# that fast, so there is no per-fibre cap. `_decimate_xy` remains for DISPLAY only.)


def _placement_distances(commanded: GeometryBundle, predicted: GeometryBundle,
                         dev_by_fiber=None):
    """Concatenated lateral distances of every predicted vertex to its matched
    commanded polyline -- the shared work behind placement RMS/max. Pass a
    pre-computed `dev_by_fiber` (from `_deviation_by_fiber`) to avoid recomputing.
    None when the fibre counts differ or empty."""
    dev = dev_by_fiber if dev_by_fiber is not None \
        else _deviation_by_fiber(commanded, predicted)
    if dev is None:
        return None
    parts = [a for a in dev if len(a)]
    if not parts:
        return None
    d = np.concatenate(parts)
    d = d[np.isfinite(d)]
    return d if len(d) else None


def measure_placement(commanded, predicted, reducer="rms", _dists=None):
    """Lateral (XY) deviation of the predicted centreline from the commanded one --
    the combined jet-lag trail + corner rounding, reduced as RMS or max."""
    d = _dists if _dists is not None else _placement_distances(commanded, predicted)
    if d is None or not len(d):
        return None
    return float(np.sqrt(np.mean(d * d)) if reducer == "rms" else d.max())


def measure_corner_rounding(commanded, predicted, angle_thresh_deg=30.0):
    """Mean corner-cut depth: at each sharp turn in the commanded path, how far the
    predicted path falls short of the commanded vertex (the rounding). None if no
    corners or counts differ."""
    pairs = _match_fibers(commanded, predicted)
    if pairs is None:
        return None
    cuts = []
    thr = math.radians(angle_thresh_deg)
    for cf, pf in pairs:
        c = cf.xy
        if len(c) < 3 or len(pf.xy) < 1:
            continue
        v1 = c[1:-1] - c[:-2]
        v2 = c[2:] - c[1:-1]
        n1 = np.hypot(v1[:, 0], v1[:, 1])
        n2 = np.hypot(v2[:, 0], v2[:, 1])
        ok = (n1 > 1e-9) & (n2 > 1e-9)
        if not np.any(ok):
            continue
        cosang = np.ones(len(v1))
        cosang[ok] = np.clip((v1[ok, 0] * v2[ok, 0] + v1[ok, 1] * v2[ok, 1])
                             / (n1[ok] * n2[ok]), -1.0, 1.0)
        turn = np.arccos(cosang)
        corner_idx = np.nonzero(turn > thr)[0] + 1
        if not len(corner_idx):
            continue
        cverts = c[corner_idx]
        d = _point_to_polyline(cverts, pf.xy)
        cuts.append(d[np.isfinite(d)])
    if not cuts:
        return None
    allc = np.concatenate(cuts)
    return float(np.mean(allc)) if len(allc) else None


# --------------------------------------------------------------------------- #
# The metric registry
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class AnalysisMetric:
    """One analysable quantity. `kind`:
      * "scalar"      -- from `MEWModel.solve` (reads `jet_attr` off JetState),
                         optionally needing measured context (span / N) via `predict`;
      * "geom_bundle" -- measured on a single bundle (`measure_bundle`);
      * "geom_diff"   -- a deviation needing BOTH bundles (`measure_pair`).
    `calibrated_anchor` flags a value we FIT (not a free prediction); `parity_eligible`
    marks it usable on the predicted-vs-measured parity axis; `csv_col` is its column
    in an imported measurement table (None -> not importable)."""
    key: str
    label: str
    unit: str
    kind: str
    jet_attr: str = None
    measure_bundle: object = None
    measure_pair: object = None
    predict: object = None
    calibrated_anchor: bool = False
    parity_eligible: bool = False
    straight_only: bool = False
    csv_col: str = None
    desc: str = ""


def _scalar_predict(attr, straight_only):
    """A scalar metric's predictor: solve and read one JetState attribute. None when
    the quantity is invalid in the regime (e.g. straight-fibre diameter below CTS)."""
    def fn(model, params, ctx=None):
        js = solve_point(model, params)
        if straight_only and not js.straight_fibre:
            return None
        return float(getattr(js, attr))
    return fn


def _predict_sag(model, params, ctx):
    """Predict suspended-span sag depth at a point -- needs a span (mm) in ctx. Uses
    the model's grounded `bridge_sag_depth` with the diameter the model predicts (or a
    measured diameter if ctx supplies one)."""
    if not ctx or ctx.get("span") in (None, ""):
        return None
    span = float(ctx["span"])
    d = ctx.get("diameter")
    if d in (None, ""):
        d = solve_point(model, params).fiber_diameter
    try:
        return float(model.bridge_sag_depth(span, float(d)))
    except Exception:  # noqa: BLE001
        return None


def _predict_stack(model, params, ctx):
    """Predict an N-layer stacked-crossover height -- needs n_layers in ctx. Uses the
    calibrated fusion overlap: h = d*[1 + (N-1)*(1-phi)]."""
    if not ctx or ctx.get("n_layers") in (None, ""):
        return None
    n = int(float(ctx["n_layers"]))
    d = ctx.get("diameter")
    if d in (None, ""):
        d = solve_point(model, params).fiber_diameter
    phi = float(getattr(model.calibration, "fusion_overlap", 0.0))
    return float(float(d) * (1.0 + (n - 1) * (1.0 - phi)))


_METRIC_LIST = [
    # -- scalar: free predictions (the paper's spine) ----------------------- #
    AnalysisMetric("diameter", "Fibre diameter", "mm", "scalar",
                   jet_attr="fiber_diameter", parity_eligible=True,
                   straight_only=True, csv_col="diameter_mm",
                   predict=_scalar_predict("fiber_diameter", True),
                   desc="d = sqrt(4Q/pi v); mass conservation. The headline free prediction."),
    AnalysisMetric("lag_distance", "Jet-lag distance", "mm", "scalar",
                   jet_attr="lag_distance", parity_eligible=True, csv_col="lag_mm",
                   predict=_scalar_predict("lag_distance", False),
                   desc="Horizontal trail of the touchdown behind the nozzle (Hrynevich-anchored)."),
    AnalysisMetric("lag_angle", "Jet-lag angle", "deg", "scalar",
                   jet_attr="lag_angle_deg", parity_eligible=True, csv_col="lag_deg",
                   predict=_scalar_predict("lag_angle_deg", False),
                   desc="Chord angle of the nozzle->touchdown lag."),
    AnalysisMetric("speed_ratio", "Speed ratio v/CTS", "", "scalar",
                   jet_attr="speed_ratio",
                   predict=_scalar_predict("speed_ratio", False),
                   desc="Translation speed in units of CTS (regime indicator)."),
    AnalysisMetric("flow_rate", "Flow rate", "mm³/s", "scalar",
                   jet_attr="flow_rate", parity_eligible=True, csv_col="flow_mm3_s",
                   predict=_scalar_predict("flow_rate", False),
                   desc="Hagen-Poiseuille volumetric flow through the needle."),
    AnalysisMetric("viscosity", "Melt viscosity", "Pa.s", "scalar",
                   jet_attr="viscosity",
                   predict=_scalar_predict("viscosity", False),
                   desc="Arrhenius eta(T); the temperature axis of the model."),
    AnalysisMetric("nozzle_exit_speed", "Nozzle exit speed", "mm/s", "scalar",
                   jet_attr="nozzle_exit_speed",
                   predict=_scalar_predict("nozzle_exit_speed", False),
                   desc="Melt speed at the bore exit (jet start)."),
    # -- scalar: calibrated anchors (validate held-out, never circularly) --- #
    AnalysisMetric("cts", "Critical translation speed", "mm/s", "scalar",
                   jet_attr="critical_speed", parity_eligible=True,
                   calibrated_anchor=True, csv_col="cts_mm_s",
                   predict=_scalar_predict("critical_speed", False),
                   desc="Coiling-onset speed. A FIT anchor -- validate on held-out conditions."),
    # -- scalar-with-context: geometry consequences predictable at a point -- #
    AnalysisMetric("sag_depth", "Sag depth (span)", "mm", "scalar",
                   parity_eligible=True, calibrated_anchor=True, csv_col="sag_mm",
                   predict=_predict_sag,
                   desc="Suspended-span midspan droop (needs a span; uses bridge_sag_depth). FIT anchor."),
    AnalysisMetric("stack_height_pt", "Stacked-peak height (N)", "mm", "scalar",
                   parity_eligible=True, calibrated_anchor=True, csv_col="stack_mm",
                   predict=_predict_stack,
                   desc="N-layer crossover-peak height (needs N; uses fusion overlap). FIT anchor."),
    # -- geometry: measured on each bundle ---------------------------------- #
    AnalysisMetric("g_diameter_mean", "Diameter (mean)", "mm", "geom_bundle",
                   measure_bundle=measure_diameter_mean,
                   desc="Mean deposited fibre diameter over all vertices."),
    AnalysisMetric("g_diameter_cv", "Diameter variation (CV)", "", "geom_bundle",
                   measure_bundle=measure_diameter_cv,
                   desc="Coefficient of variation of the deposited diameter (speed-driven)."),
    AnalysisMetric("g_pore_size", "Pore size (mean)", "mm", "geom_bundle",
                   measure_bundle=measure_pore_size,
                   desc="Mean equivalent diameter of the open windows (top-down projection)."),
    AnalysisMetric("g_pore_cv", "Pore-size variation (CV)", "", "geom_bundle",
                   measure_bundle=measure_pore_cv,
                   desc="Coefficient of variation of the pore sizes."),
    AnalysisMetric("g_porosity", "Porosity (projected)", "frac", "geom_bundle",
                   measure_bundle=measure_porosity,
                   desc="Open-area fraction within the scaffold footprint."),
    AnalysisMetric("g_sag_depth", "Sag depth (mean)", "mm", "geom_bundle",
                   measure_bundle=measure_sag_depth,
                   desc="Mean per-fibre downward excursion below its resting plane."),
    AnalysisMetric("g_sag_depth_max", "Sag depth (max)", "mm", "geom_bundle",
                   measure_bundle=lambda b: measure_sag_depth(b, "max"),
                   desc="Worst-case per-fibre sag depth."),
    AnalysisMetric("g_stack_height", "Built height", "mm", "geom_bundle",
                   measure_bundle=measure_stack_height,
                   desc="Top-of-stack minus bottom (radius included)."),
    AnalysisMetric("g_stack_ratio", "Stack ratio h/(N.d)", "", "geom_bundle",
                   measure_bundle=measure_stack_ratio,
                   desc="Built height vs N*d; < 1 = fusion compaction."),
    # -- geometry: deviation between the two bundles ------------------------ #
    AnalysisMetric("d_placement_rms", "Placement error (RMS)", "mm", "geom_diff",
                   measure_pair=lambda c, p: measure_placement(c, p, "rms"),
                   desc="RMS lateral deviation of the predicted centreline from commanded."),
    AnalysisMetric("d_placement_max", "Placement error (max)", "mm", "geom_diff",
                   measure_pair=lambda c, p: measure_placement(c, p, "max"),
                   desc="Worst-case lateral deviation (jet-lag trail + corner rounding)."),
    AnalysisMetric("d_corner_round", "Corner rounding", "mm", "geom_diff",
                   measure_pair=measure_corner_rounding,
                   desc="Mean corner-cut depth at sharp commanded turns."),
]

METRICS = {m.key: m for m in _METRIC_LIST}
SCALAR_METRICS = [m for m in _METRIC_LIST if m.kind == "scalar"]
GEOM_BUNDLE_METRICS = [m for m in _METRIC_LIST if m.kind == "geom_bundle"]
GEOM_DIFF_METRICS = [m for m in _METRIC_LIST if m.kind == "geom_diff"]
PARITY_METRICS = [m for m in _METRIC_LIST if m.parity_eligible]
CSV_COL_TO_METRIC = {m.csv_col: m.key for m in _METRIC_LIST if m.csv_col}

# Absolute convergence floor per geometry metric (in the metric's own unit): a value
# whose full-vs-half sampling delta is within max(floor, 2% of value) is "converged".
_CONV_FLOOR = {
    "g_diameter_mean": 5e-4, "g_diameter_cv": 0.01,
    "g_pore_size": 5e-3, "g_pore_cv": 0.01, "g_porosity": 5e-3,
    "g_sag_depth": 1e-3, "g_sag_depth_max": 1e-3,
    "g_stack_height": 5e-3, "g_stack_ratio": 0.02,
    "d_placement_rms": 1e-3, "d_placement_max": 1e-3, "d_corner_round": 2e-3,
}


def predict_metric(model, params, metric_key, ctx=None):
    """Predict one metric at a parameter point (scalar metrics only). None if not
    predictable at a point or invalid in the regime."""
    m = METRICS.get(metric_key)
    if m is None or m.predict is None:
        return None
    try:
        return m.predict(model, params, ctx)
    except Exception:  # noqa: BLE001 -- one bad metric never breaks a batch
        return None


# --------------------------------------------------------------------------- #
# Predicted vs commanded -- geometry diff -> a typed comparison
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class MetricRow:
    """One row of the fidelity comparison. `kind` is "free" | "anchor" (a calibrated
    anchor is not independent evidence about the data it was fit to). `converged` is
    the sampling-stability flag (False -> the UI greys the row); `note` carries a
    short human reason when a value is missing / not-a-grid."""
    key: str
    label: str
    unit: str
    commanded: float
    predicted: float
    delta: float
    delta_pct: float
    kind: str
    converged: bool = True
    note: str = ""


@dataclasses.dataclass
class ComparisonResult:
    rows: list

    def row(self, key):
        for r in self.rows:
            if r.key == key:
                return r
        return None


def _measure_side(key, m, commanded, predicted):
    """Return (commanded_value, predicted_value) for one metric, using pre-computed
    shared intermediates where possible (filled by `compare_geometry`)."""
    if m.kind == "geom_bundle":
        return m.measure_bundle(commanded), m.measure_bundle(predicted)
    if m.kind == "geom_diff":
        return 0.0, m.measure_pair(commanded, predicted)
    return None, None


def compare_geometry(commanded: GeometryBundle, predicted: GeometryBundle,
                     metric_keys=None, dev_by_fiber=None,
                     floors=None) -> ComparisonResult:
    """Measure every requested geometry metric on both bundles (or as a deviation)
    and return a `ComparisonResult` of `MetricRow`s. Each metric is independent and
    defensive -- a None from any one never aborts the rest. Every geometry row also
    carries a `converged` flag from a full-vs-half sampling probe on the PREDICTED
    (dense) side, so the user always knows whether the number is sampling-stable.
    `metric_keys` None -> all geometry metrics. Pass `dev_by_fiber`
    (`_deviation_by_fiber`) to reuse the deviation the caller already computed for the
    sidecar / per-layer strip instead of paying for it again. Pass `floors`
    (`_dev_floors`, aligned to the fibre pairs) so the placement convergence probe
    reuses the full-bundle deviation floor on its strided subset -- the floor is the
    heavy term and is sampling-invariant, so this avoids a second full recompute."""
    if metric_keys is None:
        metric_keys = [m.key for m in GEOM_BUNDLE_METRICS + GEOM_DIFF_METRICS]
    keys = list(metric_keys)
    want = set(keys)

    # expensive shared intermediates, computed at most once (full sampling)
    _PORE = {"g_pore_size": measure_pore_size, "g_pore_cv": measure_pore_cv,
             "g_porosity": measure_porosity}
    _PLACE = {"d_placement_rms": "rms", "d_placement_max": "max"}
    cpores = ppores = None
    if want & set(_PORE):
        cpores = _interior_pores(commanded)
        ppores = _interior_pores(predicted)
    pdist = None
    if want & set(_PLACE):
        pdist = _placement_distances(commanded, predicted, dev_by_fiber=dev_by_fiber)

    # half-sampled predicted side (for the convergence probe) -- computed lazily once
    half_pred = None
    half_idx = None
    half_pores = None
    half_pdist = None

    rows = []
    for key in keys:
        m = METRICS.get(key)
        if m is None:
            continue
        note = ""
        try:
            if key in _PORE:
                cval = _PORE[key](commanded, _pores=cpores)
                pval = _PORE[key](predicted, _pores=ppores)
            elif key in _PLACE:
                cval = 0.0
                pval = measure_placement(commanded, predicted, _PLACE[key], _dists=pdist)
            else:
                cval, pval = _measure_side(key, m, commanded, predicted)
        except Exception:  # noqa: BLE001
            cval = pval = None

        # convergence probe on the predicted (dense) side
        converged = True
        if pval is not None:
            if half_pred is None:
                half_pred, half_idx = _half_sampled_idx(predicted)
            try:
                if key in _PORE:
                    if half_pores is None:
                        half_pores = _interior_pores(half_pred)
                    pval_h = _PORE[key](half_pred, _pores=half_pores)
                elif key in _PLACE:
                    if half_pdist is None:
                        # Reuse the full-bundle floor on the strided subset when the
                        # caller supplied it (the floor is per-point, so subsetting is
                        # byte-identical) -- else recompute the deviation on the half.
                        half_dev = None
                        if floors is not None:
                            hfl = [(f[ix] if f is not None else None)
                                   for f, ix in zip(floors, half_idx)]
                            half_dev = _deviation_by_fiber(commanded, half_pred,
                                                           floors=hfl)
                        half_pdist = _placement_distances(commanded, half_pred,
                                                          dev_by_fiber=half_dev)
                    pval_h = measure_placement(commanded, half_pred, _PLACE[key],
                                               _dists=half_pdist)
                elif m.kind == "geom_bundle":
                    pval_h = m.measure_bundle(half_pred)
                else:
                    pval_h = m.measure_pair(commanded, half_pred)
            except Exception:  # noqa: BLE001
                pval_h = None
            if pval_h is not None:
                floor = _CONV_FLOOR.get(key, 1e-6)
                converged = abs(pval - pval_h) <= max(floor, 0.02 * abs(pval))

        if pval is None:
            note = "n/a (not a regular grid)" if m.kind == "geom_bundle" \
                else "n/a (fibre counts differ)"
        delta = delta_pct = None
        if cval is not None and pval is not None:
            delta = pval - cval
            delta_pct = (100.0 * delta / cval) if abs(cval) > 1e-12 else None
        rows.append(MetricRow(
            key=key, label=m.label, unit=m.unit, commanded=cval, predicted=pval,
            delta=delta, delta_pct=delta_pct,
            kind=("anchor" if m.calibrated_anchor else "free"),
            converged=converged, note=note))
    return ComparisonResult(rows=rows)


# --------------------------------------------------------------------------- #
# Deviation sidecar (for stl_viewer --deviation) + per-layer metrics
# --------------------------------------------------------------------------- #
def _depo_fingerprint(depo_path):
    """A cheap content hash of a .depo cache's vertex block -- identifies WHICH
    scaffold a deviation sidecar was computed against. Both the writer (here) and
    stl_viewer's loader read the same on-disk `xyz` array, so hashing its raw bytes
    gives a stable match: a different scaffold with the same vertex COUNT (which the
    count check alone can't tell apart) has different coordinates -> different hash.
    None if the depo can't be read (the sidecar then carries no fingerprint and the
    loader keeps its legacy count-only check)."""
    if not depo_path:
        return None
    try:
        import hashlib
        with np.load(depo_path, allow_pickle=False) as d:
            return hashlib.sha1(
                np.ascontiguousarray(d["xyz"]).tobytes()).hexdigest()
    except Exception:  # noqa: BLE001
        return None


def write_deviation_sidecar(path, predicted: GeometryBundle,
                            commanded: GeometryBundle, dev_by_fiber=None,
                            depo_path=None) -> float:
    """Write an npz sidecar aligned 1:1 with the PREDICTED bundle's vertices: `dev`
    (float32, per predicted vertex = its cross-track deviation from the matched
    commanded path), `splits` (int64 cumulative fibre lengths), `vmax` (float64, the
    true worst-case) and `vcap` (float64, the colour cap). Returns the TRUE max
    deviation (for an honest 'worst-case' readout). The order + splits match how
    `bundle_from_depo` reads the `.depo`, so stl_viewer can colour the cached
    centreline tubes directly -- it ranges its blue->red LUT over [0, vcap] so a lone
    freak vertex can't wash the whole scaffold blue, while `dev`/`vmax` stay in honest
    mm. `vcap = max(p99, noise_floor)`: the p99 governs a real deviation field, but a
    physics-OFF scaffold whose only "deviation" is float32 STORAGE dust (the .depo xyz
    is float32; the half-ulp |c|*2^-24 per axis vanishes in float64 yet the p99 of the
    dust would still stretch the LUT to full red on the denser-sampled loops/corners) is
    floored to `_DEV_NOISE_ULP_K * 2^-24 * max|coord|`, orders of magnitude above the
    dust, so the whole scaffold stays deep blue. The floor never bites a real field (p99
    dominates) and keeps `vcap` positive even when `vmax` is exactly 0. Pass
    `dev_by_fiber` (`_deviation_by_fiber`) to reuse an already-computed deviation. Best
    effort: returns 0.0 (writes nothing) if it cannot pair the two bundles."""
    dev = dev_by_fiber if dev_by_fiber is not None \
        else _deviation_by_fiber(commanded, predicted)
    if dev is None:
        return 0.0
    dev_parts, lengths = [], []
    max_abs_coord = 0.0                        # biggest |xyz| over the kept fibres
    for pf, d in zip(predicted.fibers, dev):
        n = len(pf.xyz)
        if n < 1:
            continue
        if len(d) != n:                       # a fibre with no commanded pair -> zeros
            d = np.zeros(n)
        dev_parts.append(np.asarray(d, dtype=np.float32))
        lengths.append(n)
        max_abs_coord = max(max_abs_coord, float(np.abs(pf.xyz).max()))
    if not dev_parts:
        return 0.0
    dev = np.concatenate(dev_parts)
    splits = np.cumsum(lengths).astype(np.int64)
    vmax = float(dev.max()) if len(dev) else 0.0
    # Colour cap = a high percentile, so the LUT's red end is the "typical worst", not
    # a single outlier vertex that would otherwise compress everything else to blue.
    vcap = float(np.percentile(dev, _DEV_COLOR_PCT)) if len(dev) else 0.0
    if vcap <= 0.0:                           # degenerate (mass at 0 + a lone spike)
        vcap = vmax
    # Floor the COLOUR cap (not dev/vmax) at the float32-storage noise level, so a
    # physics-OFF scaffold -- whose only "deviation" is |c|*2^-24 storage dust -- keeps
    # a sane range and paints deep blue instead of the p99 stretching dust to full red.
    # Grades/readout stay honest; on a real field the p99 dominates and this never bites.
    noise_floor = _DEV_NOISE_ULP_K * 2.0 ** -24 * max_abs_coord
    vcap = max(vcap, noise_floor)
    extra = {}
    fp = _depo_fingerprint(depo_path)
    if fp is not None:
        extra["depo_fp"] = np.asarray(fp)      # ties this sidecar to its .depo
    try:
        np.savez(path, dev=dev, splits=splits,
                 vmax=np.float64(vmax), vcap=np.float64(vcap), **extra)
    except OSError:
        return 0.0
    return vmax


def deviation_sidecar(gcode_path, depo_path, out_path, fallback_diameter=0.02):
    """The Viewer's "Show deviation" in one call: pair the g-code's commanded fibres
    with the .depo's deposited ones and write the sidecar at `out_path`. Returns the
    true max deviation (mm). The commanded side takes the travels exactly when the
    .depo carries them. Raises `DepoMismatch` with a short reason the Viewer can show
    when the cache is unreadable or the two sides do not pair."""
    pred = bundle_from_depo(depo_path, fallback_diameter=fallback_diameter)
    cmd = bundle_from_gcode(gcode_path, pred.nominal_diameter or fallback_diameter,
                            with_travels=bool(pred.physics.get("render_travel")))
    dev = _deviation_by_fiber(cmd, pred)
    if dev is None:
        raise DepoMismatch(
            f"the g-code has {len(cmd.fibers)} fibres but the .depo has "
            f"{len(pred.fibers)} -- regenerate this print")
    vmax = write_deviation_sidecar(out_path, pred, cmd, dev_by_fiber=dev,
                                   depo_path=depo_path)
    if not os.path.isfile(out_path):
        raise DepoMismatch(f"could not write the deviation sidecar at {out_path!r}")
    return vmax


def load_deviation_sidecar(path):
    """Read a deviation sidecar -> (dev (V,) float32, splits (F,) int64, vmax float).
    None if unreadable."""
    try:
        with np.load(path, allow_pickle=False) as d:
            return (np.asarray(d["dev"], dtype=np.float32),
                    np.asarray(d["splits"], dtype=np.int64),
                    float(d["vmax"]))
    except Exception:  # noqa: BLE001
        return None


def per_layer_metrics(commanded: GeometryBundle, predicted: GeometryBundle,
                      keys=("d_placement_rms", "g_pore_size"), dev_by_fiber=None):
    """Per-layer fidelity: one dict per PRINT layer with `layer`, `t_start`, `t_end`
    (print-time bounds, s; None if the bundle carries no times) and one entry per
    requested key. Layers come from the COMMANDED bundle's clean per-fibre Z (one
    fibre per layer in MEWron), NOT the predicted median Z -- physics fusion/sag
    compresses the deposited Z and would merge print layers together (the bug that
    showed 5 rows for a 10-layer print). Predicted fibres are matched to commanded by
    index (the transform preserves fibre count/order). Deviation is reused from
    `dev_by_fiber` when given. Robust: an unmeasurable metric stores None."""
    pairs = _match_fibers(commanded, predicted)
    if pairs is None:
        return []
    if dev_by_fiber is None:
        dev_by_fiber = _deviation_by_fiber(commanded, predicted)
    # group fibre indices by the COMMANDED fibre's layer
    by_layer = {}
    for i, (cf, _pf) in enumerate(pairs):
        by_layer.setdefault(cf.layer, []).append(i)
    out = []
    for li in sorted(by_layer):
        idxs = by_layer[li]
        pfs = [predicted.fibers[i] for i in idxs]
        cfs = [commanded.fibers[i] for i in idxs]
        ts = [f.times for f in pfs if f.times is not None and len(f.times)]
        t0 = float(min(float(t.min()) for t in ts)) if ts else None
        t1 = float(max(float(t.max()) for t in ts)) if ts else None
        row = {"layer": li, "t_start": t0, "t_end": t1}
        pbund = GeometryBundle(fibers=pfs, source="predicted",
                               nominal_diameter=predicted.nominal_diameter)
        for k in keys:
            m = METRICS.get(k)
            val = None
            try:
                if m is None:
                    pass
                elif m.kind == "geom_bundle":
                    val = m.measure_bundle(pbund)
                elif m.kind in ("d_placement_rms",) or k in ("d_placement_rms",
                                                             "d_placement_max"):
                    # reuse the shared deviation: RMS/max over this layer's fibres
                    parts = [dev_by_fiber[i] for i in idxs if len(dev_by_fiber[i])] \
                        if dev_by_fiber is not None else []
                    if parts:
                        d = np.concatenate(parts)
                        val = float(np.sqrt(np.mean(d * d)) if k.endswith("rms")
                                    else d.max())
                elif m.kind == "geom_diff":
                    cbund = GeometryBundle(fibers=cfs, source="commanded",
                                           nominal_diameter=commanded.nominal_diameter)
                    val = m.measure_pair(cbund, pbund)
            except Exception:  # noqa: BLE001
                val = None
            row[k] = val
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# Sweep / grid
# --------------------------------------------------------------------------- #
def evaluate_sweep(model, base_params, axis, values, metric_keys):
    """Step one process `axis` through `values`, holding the rest at `base_params`,
    and evaluate each scalar metric. Returns
        {axis, axis_label, unit, values, regimes, metrics:{key:[...]}, labels,
         metric_units}. Geometry metric keys are ignored."""
    if axis not in PARAM_AXES:
        raise ValueError(f"unknown sweep axis {axis!r}")
    keys = [k for k in metric_keys if METRICS.get(k) and METRICS[k].predict]
    cols = {k: [] for k in keys}
    regimes = []
    xs = [float(v) for v in values]
    for v in xs:
        p = dict(base_params)
        p[axis] = v
        js = solve_point(model, p)
        regimes.append(js.regime.value if isinstance(js.regime, Regime) else str(js.regime))
        for k in keys:
            cols[k].append(predict_metric(model, p, k))
    lbl, unit = AXIS_LABELS.get(axis, (axis, ""))
    return {"axis": axis, "axis_label": lbl, "unit": unit, "values": xs,
            "regimes": regimes, "metrics": cols,
            "labels": {k: METRICS[k].label for k in keys},
            "metric_units": {k: METRICS[k].unit for k in keys}}


def evaluate_grid(model, base_params, axis_x, xs, axis_y, ys, metric_key):
    """Evaluate ONE scalar metric over a 2D grid of two process axes (a process map).
    Returns {axis_x, axis_y, x_values, y_values, x_label, y_label, x_unit, y_unit,
    metric_label, metric_unit, metric_key, z (rows=y), regimes}. `z[iy][ix]` is None
    where the metric is invalid in that cell's regime (heatmap renders a gap)."""
    m = METRICS.get(metric_key)
    if m is None or m.predict is None:
        raise ValueError(f"{metric_key!r} is not a predictable scalar metric")
    for ax in (axis_x, axis_y):
        if ax not in PARAM_AXES:
            raise ValueError(f"unknown grid axis {ax!r}")
    xs = [float(v) for v in xs]
    ys = [float(v) for v in ys]
    z = [[None] * len(xs) for _ in ys]
    regimes = [[None] * len(xs) for _ in ys]
    for iy, yv in enumerate(ys):
        for ix, xv in enumerate(xs):
            p = dict(base_params)
            p[axis_x] = xv
            p[axis_y] = yv
            js = solve_point(model, p)
            regimes[iy][ix] = (js.regime.value if isinstance(js.regime, Regime)
                               else str(js.regime))
            z[iy][ix] = predict_metric(model, p, metric_key)
    lx, ux = AXIS_LABELS.get(axis_x, (axis_x, ""))
    ly, uy = AXIS_LABELS.get(axis_y, (axis_y, ""))
    return {"axis_x": axis_x, "axis_y": axis_y, "x_values": xs, "y_values": ys,
            "x_label": lx, "y_label": ly, "x_unit": ux, "y_unit": uy,
            "metric_label": m.label, "metric_unit": m.unit,
            "metric_key": metric_key, "z": z, "regimes": regimes}


# --------------------------------------------------------------------------- #
# Predicted vs measured -- parity + residual + sensitivity
# --------------------------------------------------------------------------- #
def parity_stats(predicted, measured):
    """Compare predicted vs measured arrays. Returns
        {n, rmse, mae, bias, r2, slope, intercept}
    where r2 is about the 1:1 line (the honest validation R^2 -- can go negative),
    and slope/intercept come from OLS predicted~measured. NaNs dropped."""
    p = np.asarray(predicted, dtype=np.float64)
    m = np.asarray(measured, dtype=np.float64)
    good = np.isfinite(p) & np.isfinite(m)
    p, m = p[good], m[good]
    n = int(len(p))
    out = {"n": n, "rmse": None, "mae": None, "bias": None, "r2": None,
           "slope": None, "intercept": None}
    if n == 0:
        return out
    resid = p - m
    out["rmse"] = float(np.sqrt(np.mean(resid * resid)))
    out["mae"] = float(np.mean(np.abs(resid)))
    out["bias"] = float(np.mean(resid))
    ss_tot = float(np.sum((m - np.mean(m)) ** 2))
    ss_res = float(np.sum(resid * resid))
    out["r2"] = float(1.0 - ss_res / ss_tot) if ss_tot > 0 else None
    if n >= 2 and np.std(m) > 0:
        b, a = np.polyfit(m, p, 1)
        out["slope"] = float(b)
        out["intercept"] = float(a)
    return out


def residual(model, params, measured_value, metric_key, ctx=None):
    """The shared primitive both validation and the Calibration guided-fit minimise:
    predicted(metric, params) - measured_value. None if not predictable."""
    pred = predict_metric(model, params, metric_key, ctx)
    if pred is None:
        return None
    return float(pred - float(measured_value))


def parity_for_measurements(model, measurements, metric_key):
    """Run the model at each measurement's parameters and pair its prediction with
    that measurement's value for `metric_key`. Returns
        {pairs:[(measured, predicted, role, label)], stats, stats_validation}.
    `stats` over ALL rows; `stats_validation` over rows tagged role 'validation'
    (None if none) -- the held-out read that makes a calibrated anchor a real test."""
    pairs = []
    for meas in measurements:
        if metric_key not in meas.measured:
            continue
        ctx = dict(meas.context)
        if "diameter" not in ctx and "diameter_mm" in meas.measured:
            ctx["diameter"] = meas.measured["diameter_mm"]
        pred = predict_metric(model, meas.params, metric_key, ctx)
        if pred is None:
            continue
        pairs.append((float(meas.measured[metric_key]), float(pred),
                      meas.role, meas.label))
    meas_v = [pr[0] for pr in pairs]
    pred_v = [pr[1] for pr in pairs]
    val = [(pr[0], pr[1]) for pr in pairs if pr[2] == "validation"]
    stats_v = parity_stats([b for _, b in val], [a for a, _ in val]) if val else None
    return {"pairs": pairs, "stats": parity_stats(pred_v, meas_v),
            "stats_validation": stats_v}


def sensitivity(model, base_params, metric_keys, axes=PARAM_AXES, frac=0.1):
    """For each input `axis`, perturb it by +/-frac (multiplicatively) and report the
    LOG-LOG sensitivity (elasticity) d ln(metric)/d ln(axis) of each scalar metric.
    Where a metric is <= 0 it falls back to a normalised linear sensitivity. Returns
        {metrics:{key:{axis:value}}, base:{key:metric0}, labels, axes, frac}."""
    keys = [k for k in metric_keys if METRICS.get(k) and METRICS[k].predict]
    base_vals = {k: predict_metric(model, base_params, k) for k in keys}
    table = {k: {} for k in keys}
    for axis in axes:
        if axis not in base_params and axis not in DEFAULT_PARAMS:
            continue
        x0 = float(base_params.get(axis, DEFAULT_PARAMS[axis]))
        if x0 == 0.0:
            for k in keys:
                table[k][axis] = None
            continue
        p_hi = dict(base_params); p_hi[axis] = x0 * (1.0 + frac)
        p_lo = dict(base_params); p_lo[axis] = x0 * (1.0 - frac)
        for k in keys:
            m_hi = predict_metric(model, p_hi, k)
            m_lo = predict_metric(model, p_lo, k)
            m0 = base_vals[k]
            if m_hi is None or m_lo is None or m0 is None:
                table[k][axis] = None
            elif m_hi > 0 and m_lo > 0:
                table[k][axis] = float(
                    (math.log(m_hi) - math.log(m_lo))
                    / (math.log(x0 * (1 + frac)) - math.log(x0 * (1 - frac))))
            elif abs(m0) > 1e-12:
                table[k][axis] = float((m_hi - m_lo) / (2.0 * frac) / m0)
            else:
                table[k][axis] = None
    return {"metrics": table, "base": base_vals,
            "labels": {k: METRICS[k].label for k in keys},
            "axes": list(axes), "frac": frac}


# --------------------------------------------------------------------------- #
# Fit loop -- calibrate a single anchor from measured data (validation workbench)
# --------------------------------------------------------------------------- #
# Which calibration profile key each fittable metric anchors, and the estimator
# that inverts it. The GUI applies the fitted value through gui_calibration's
# `_cal_apply(cal_key, value)`, so these keys MUST match the calibration registry.
FIT_TARGETS = {
    "diameter": "cal.mat_viscosity_ref",
    "sag_depth": "cal.sag_coeff",
    "stack_height_pt": "cal.fusion_overlap",
    "cts": "cal.cts_ref",
}


def _fit_one(mcal, model, metric_key, meas):
    """One measurement -> a fitted constant for `metric_key`, or None if the row
    lacks what the estimator needs. Reuses the mew_calibration estimators so the
    fitted value is consistent with what the model then predicts."""
    p = meas.params
    val = meas.measured.get(metric_key)
    if val is None:
        return None
    pressure = float(p.get("pressure", DEFAULT_PARAMS["pressure"]))
    temperature = float(p.get("temperature", DEFAULT_PARAMS["temperature"]))
    speed = float(p.get("speed", DEFAULT_PARAMS["speed"]))
    ctx = meas.context
    if metric_key == "diameter":
        return mcal.fit_viscosity_ref(model, float(val), pressure, temperature, speed)
    if metric_key == "cts":
        return float(val)                    # cts_ref IS the measured onset speed
    if metric_key == "sag_depth":
        span = ctx.get("span")
        if span in (None, ""):
            return None
        d = meas.measured.get("diameter")
        if d in (None, ""):
            d = solve_point(model, p).fiber_diameter
        return mcal.fit_sag_coeff(model, float(val), float(span), float(d),
                                  temperature)
    if metric_key == "stack_height_pt":
        n = ctx.get("n_layers")
        if n in (None, ""):
            return None
        d = meas.measured.get("diameter")
        if d in (None, ""):
            d = solve_point(model, p).fiber_diameter
        return mcal.fit_fusion_overlap(float(val), int(float(n)), float(d))
    return None


def fit_calibration_constant(model, measurements, metric_key, role="calibration"):
    """Fit the single calibration constant that `metric_key` anchors, from the
    `role`-tagged measurements (default the calibration subset), by averaging the
    per-row estimator fits. Returns {ok, cal_key, value, n, metric_key} on success,
    else {ok: False, error}. The GUI applies `value` to `cal_key` via the Calibration
    tab's apply path, then re-validates on the held-out rows."""
    cal_key = FIT_TARGETS.get(metric_key)
    if cal_key is None:
        return {"ok": False, "error": f"{metric_key} has no single-constant fit"}
    rows = [m for m in measurements
            if (not role or m.role == role) and metric_key in m.measured]
    if not rows:
        return {"ok": False,
                "error": f"no {role or 'any'}-role rows measure {metric_key}"}
    fits = []
    for m in rows:
        try:
            v = _fit_one(mew_calibration, model, metric_key, m)
        except Exception:  # noqa: BLE001 -- one bad row never aborts the fit
            v = None
        if v is not None and math.isfinite(v):
            fits.append(float(v))
    if not fits:
        return {"ok": False, "error": f"could not fit {metric_key} from these rows"}
    return {"ok": True, "cal_key": cal_key, "value": float(np.mean(fits)),
            "n": len(fits), "metric_key": metric_key}


# --------------------------------------------------------------------------- #
# Measurement CSV I/O
# --------------------------------------------------------------------------- #
_PARAM_COLUMNS = [
    ("speed_mm_s", "speed", 1.0),
    ("pressure_psi", "pressure", PSI_TO_PA),
    ("temperature_c", "temperature", 1.0),
    ("voltage_kv", "voltage", 1.0),
    ("gap_mm", "gap", 1.0),
]
_CONTEXT_COLUMNS = [("span_mm", "span"), ("n_layers", "n_layers")]

# Real rig / lag-runbook CSVs (and older templates) name some columns differently
# from the current template. Map each to the canonical column the reader knows, so
# a hand-kept measurement sheet imports without being silently reshaped into blanks.
# (dst is only filled when absent, so an explicit canonical column always wins.)
_COLUMN_ALIASES = {
    "v_mm_s": "speed_mm_s",           # translation speed
    "measured_lag_mm": "lag_mm",      # the MEASURED lag (vs lag_pred_mm, a design echo)
}


@dataclasses.dataclass
class Measurement:
    """One imported observation: `params` (internal units, pressure Pa), `measured`
    metric values keyed by METRIC key, optional `context` (span / n_layers), a `role`
    ('calibration' | 'validation' | '') and a free `label`."""
    params: dict
    measured: dict
    context: dict = dataclasses.field(default_factory=dict)
    role: str = ""
    label: str = ""


def measurement_template() -> str:
    """A documented blank measurement CSV (header + two example rows + a legend) the
    GUI offers as a download. Pressure psi; blanks mean 'not measured'; role =
    calibration|validation."""
    param_cols = [c for c, _, _ in _PARAM_COLUMNS]
    ctx_cols = [c for c, _ in _CONTEXT_COLUMNS]
    metric_cols = [m.csv_col for m in _METRIC_LIST if m.csv_col]
    header = ["label", "role"] + param_cols + ctx_cols + metric_cols
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    ex1 = {"label": "rig_v05", "role": "calibration", "speed_mm_s": "8",
           "pressure_psi": "100", "temperature_c": "85", "voltage_kv": "7",
           "gap_mm": "4", "diameter_mm": "0.012"}
    ex2 = {"label": "rig_v20", "role": "validation", "speed_mm_s": "20",
           "pressure_psi": "100", "temperature_c": "85", "voltage_kv": "7",
           "gap_mm": "4", "diameter_mm": "0.008", "lag_mm": "0.6"}
    for ex in (ex1, ex2):
        w.writerow([ex.get(c, "") for c in header])
    legend = (
        "# MEWracle measurement table. One row per measured print.\n"
        "# Fill the parameter columns (pressure in psi) and any metric columns you\n"
        "# measured; leave the rest blank. role = calibration (fit on these) or\n"
        "# validation (held-out test). span_mm is needed to validate sag_mm;\n"
        "# n_layers is needed to validate stack_mm.\n")
    return legend + buf.getvalue()


def _num(s):
    s = (s or "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def read_measurements_csv(path) -> list:
    """Parse a measurement CSV (the `measurement_template` shape) into `Measurement`s.
    Robust: unknown columns ignored, unparseable cells skipped, a bad row dropped.
    Pressure psi -> Pa. Comment ('#') and blank lines ignored."""
    out = []
    try:
        with open(path, "r", newline="", errors="ignore") as fh:
            lines = [ln for ln in fh
                     if ln.strip() and not ln.lstrip().startswith("#")]
        reader = csv.DictReader(lines)
        rows = []
        for row in reader:
            row = {(k or "").strip(): v for k, v in row.items()}
            for src, dst in _COLUMN_ALIASES.items():          # normalise headers
                if row.get(src) not in (None, "") and not row.get(dst):
                    row[dst] = row[src]
            rows.append(row)
        # A metric column that carries the SAME value in every row it appears in is a
        # design-input echo (e.g. a runbook that repeats its target `cts_mm_s=6.0` on
        # every line), NOT a per-print measurement -- ingesting it as measured data
        # would feed a constant into the fit/validation. A genuine measurement varies.
        # So drop any metric column that is present in >=2 rows and never differs.
        echo_cols = set()
        for col in CSV_COL_TO_METRIC:
            vals = [_num(r.get(col)) for r in rows]
            vals = [v for v in vals if v is not None]
            if len(vals) >= 2 and max(vals) - min(vals) < 1e-12:
                echo_cols.add(col)
        for row in rows:
            params = {}
            for col, key, scale in _PARAM_COLUMNS:
                v = _num(row.get(col))
                if v is not None:
                    params[key] = v * scale
            measured = {}
            for col, mkey in CSV_COL_TO_METRIC.items():
                if col in echo_cols:
                    continue
                v = _num(row.get(col))
                if v is not None:
                    measured[mkey] = v
                    measured.setdefault(col, v)
            ctx = {}
            for col, ckey in _CONTEXT_COLUMNS:
                v = _num(row.get(col))
                if v is not None:
                    ctx[ckey] = v
            if not params and not measured:
                continue
            out.append(Measurement(
                params=params, measured=measured, context=ctx,
                role=(row.get("role") or "").strip().lower(),
                label=(row.get("label") or "").strip()))
    except (OSError, csv.Error):
        return out
    return out


def _write_csv(path, header, rows):
    try:
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)
        return True
    except OSError:
        return False


def write_sweep_csv(path, sweep) -> bool:
    keys = list(sweep["metrics"].keys())
    header = [f"{sweep['axis']}_{sweep['unit']}".strip("_"), "regime"] + [
        f"{sweep['labels'][k]} ({sweep['metric_units'][k]})".strip() for k in keys]
    rows = []
    for i, x in enumerate(sweep["values"]):
        rows.append([x, sweep["regimes"][i]] + [sweep["metrics"][k][i] for k in keys])
    return _write_csv(path, header, rows)


def write_parity_csv(path, parity, metric_key) -> bool:
    header = ["label", "role", "measured", "predicted", "residual"]
    rows = [[lbl, role, mv, pv, pv - mv] for (mv, pv, role, lbl) in parity["pairs"]]
    return _write_csv(path, header, rows)


def write_sensitivity_csv(path, sens) -> bool:
    axes = sens["axes"]
    header = ["metric"] + axes
    rows = [[sens["labels"][k]] + [sens["metrics"][k].get(a) for a in axes]
            for k in sens["metrics"]]
    return _write_csv(path, header, rows)


def write_grid_csv(path, grid) -> bool:
    rows = []
    for iy, yv in enumerate(grid["y_values"]):
        for ix, xv in enumerate(grid["x_values"]):
            rows.append([xv, yv, grid["regimes"][iy][ix], grid["z"][iy][ix]])
    header = [f"{grid['axis_x']}_{grid['x_unit']}".strip("_"),
              f"{grid['axis_y']}_{grid['y_unit']}".strip("_"), "regime",
              f"{grid['metric_label']} ({grid['metric_unit']})".strip()]
    return _write_csv(path, header, rows)


# --------------------------------------------------------------------------- #
# Convenience: a model from a calibration profile (mirrors the workers' entry)
# --------------------------------------------------------------------------- #
def model_from_profile(profile, gap=4.0) -> MEWModel:
    """A `MEWModel` from a flat calibration profile dict (the GUI's `cal.*` fields) at
    working distance `gap`. Identical construction to the sim / export / player, so
    analysis reads the SAME rig calibration."""
    mat, noz, cal = mew_calibration.models_from_profile(profile or {})
    g = float(gap) if gap and float(gap) > 0 else 4.0
    return MEWModel(material=mat, nozzle=noz, working_distance=g, calibration=cal)


__all__ = [
    "PSI_TO_PA", "PARAM_AXES", "AXIS_LABELS", "DEFAULT_PARAMS", "DepoMismatch",
    "make_process_params", "solve_point",
    "Fiber", "GeometryBundle", "bundle_from_gcode", "bundle_from_depo",
    "AnalysisMetric", "METRICS", "SCALAR_METRICS", "GEOM_BUNDLE_METRICS",
    "GEOM_DIFF_METRICS", "PARITY_METRICS", "CSV_COL_TO_METRIC",
    "MetricRow", "ComparisonResult", "compare_geometry",
    "write_deviation_sidecar", "deviation_sidecar", "load_deviation_sidecar",
    "per_layer_metrics",
    "predict_metric", "evaluate_sweep", "evaluate_grid", "write_grid_csv",
    "parity_stats", "residual", "parity_for_measurements", "sensitivity",
    "FIT_TARGETS", "fit_calibration_constant",
    "Measurement", "measurement_template", "read_measurements_csv",
    "write_sweep_csv", "write_parity_csv", "write_sensitivity_csv",
    "model_from_profile",
    "measure_diameter_mean", "measure_diameter_cv", "measure_pore_size",
    "measure_pore_cv", "measure_porosity", "measure_sag_depth",
    "measure_stack_height", "measure_stack_ratio", "measure_placement",
    "measure_corner_rounding",
]
