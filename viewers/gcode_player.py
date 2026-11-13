#!/usr/bin/env python3
"""
gcode_player.py

Animate a mock printhead following a G-code toolpath, with VCR-style playback
controls (play / pause, faster / slower, skip back / forward, restart, scrub).

    python viewers/gcode_player.py path/to/file.gcode [overlay.stl]

Why a separate script (and a separate process)?
    The GUI (gcode_to_fibers_gui.py) launches this with subprocess, exactly the
    way it launches the mesh workers and stl_viewer.py. VTK runs its own event
    loop, which would fight Tkinter's mainloop if embedded in the same process,
    and the player must never touch -- or slow -- the core converter. Keeping it
    in its own process means it can't freeze or crash the GUI, and the heavy STL
    overlays (often 100MB+) are loaded ONLY if and when the user asks to see them
    (see _toggle_overlay), so the player always starts instantly.

The printhead moves at the G-code's COMMANDED feedrates (modal F, mm/min): fast
on travels, slower on deposition, scaled by an adjustable wall-clock multiplier.
Per frame only a marker is repositioned and two line cell-arrays are rebuilt from
NumPy, so playback stays cheap regardless of path length.

Requires: vtk and numpy (both already dependencies of this project).
"""

if __name__ == "__main__":  # run as a script: put the program's folders on sys.path
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "engine"))
    import app_paths  # noqa: E402,F401

import contextlib
import json
import math
import os
import sys
import time

# --- startup phase timing (diagnostic, off unless MEWPERF is set) ---------- #
# The window the GUI labels "Starting the viewer…" is this whole process: spawn,
# imports, parse, machine model, .depo load, mesh build, first frame. None of it
# is reachable from the parent, and a probe that re-runs the phases by hand
# measures a different program (that is how the previous sweep ended up timing
# stl_viewer). MEWPERF=<file> appends "elapsed\tphase" lines instead. This block
# MUST stay above the numpy/vtk imports -- they are one of the phases.
_PERF_PATH = os.environ.get("MEWPERF")
_PERF_T0 = time.perf_counter()


def _perf_rss_mb():
    """This process's (working set, peak working set) in MB, or (0, 0). Asked from
    INSIDE the child: reading it from the launcher needs OpenProcess rights the
    launcher may not have, and a silent 0 there reads as "no memory climb"."""
    try:
        import ctypes
        import ctypes.wintypes as wt

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        # argtypes are NOT optional here: without them ctypes passes the
        # pseudo-handle (HANDLE)-1 as a 32-bit int and the call fails, returning
        # a silent 0 that reads exactly like "no memory was used".
        k32.GetCurrentProcess.restype = wt.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE,
                                               ctypes.POINTER(_PMC), wt.DWORD]
        psapi.GetProcessMemoryInfo.restype = wt.BOOL
        p = _PMC()
        p.cb = ctypes.sizeof(_PMC)
        if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(),
                                      ctypes.byref(p), p.cb):
            return p.WorkingSetSize / 1e6, p.PeakWorkingSetSize / 1e6
    except Exception:  # noqa: BLE001 -- a diagnostic must never take the sim down
        pass
    return 0.0, 0.0


def _gl_renderer(window):
    """The OpenGL implementation this window actually got, one line. Worth having
    in the phase log: an offscreen window that quietly lands on a software
    rasteriser turns every timing above it into a measurement of the CPU."""
    try:
        for line in window.ReportCapabilities().splitlines():
            if "OpenGL renderer string" in line:
                return line.split(":", 1)[1].strip()
    except Exception:  # noqa: BLE001
        pass
    return "?"


def _perf(phase):
    """Record seconds since interpreter start, and this process's working set,
    against `phase`. No-op when off."""
    if not _PERF_PATH:
        return
    try:
        rss, peak = _perf_rss_mb()
        with open(_PERF_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{time.perf_counter() - _PERF_T0:.3f}\t{phase}"
                     f"\t{rss:.0f}\t{peak:.0f}\n")
    except OSError:
        pass


import numpy as np
import vtk
from vtk.util import numpy_support

from gcode_to_fibers import (DEFAULT_FEED, DEPO_CACHE_EXT, apply_sag,
                             arc_xy_points, build_deposition_mesh,
                             decimate_keep,
                             file_skips_self_crossings, load_depo_cache,
                             decimate_idx_for, load_depo_decimation,
                             write_depo_decimation,
                             load_depo_diam_by_time, load_depo_pdia, parse_gcode,
                             path_colors_from_groups, path_diameters_from_groups,
                             rotary_split_seconds, _hex_to_rgb255,
                             set_fiber_transform, set_sag_law, set_fusion_law,
                             _DEFAULT_SAG_RESET_LAYERS,
                             _sag_applicable, _tube_fibers, _dedupe_idx,
                             _resample_by_time)
from vtk_helpers import (apply_preferred_projection,
                         fit_clipping_range, free_memory_bytes,
                         install_cad_camera, origin_marker,
                         projection_toggle, set_fibre_material,
                         studio_light_kit)
from app_paths import resource_path  # bundled-asset (printhead STL) locator, frozen-safe

# The upper-right ViewCube. Guarded like the modern-shell import below: a
# hand-launched viewer without the shell modules must still open, just without
# the gizmo.
try:
    import view_cube as _view_cube
except Exception:  # noqa: BLE001
    _view_cube = None

# A GUI launched from its desktop shortcut runs under pythonw.exe (no console), and
# the sim subprocess it spawns inherits that: pythonw leaves sys.stdout/sys.stderr ==
# None, so an unguarded write (e.g. the machine-model read-out in _report_machine)
# raises and kills the player before its window can open -- which is why the sim
# opened from cmd but not the shortcut. Route None streams to a null sink so a
# diagnostic can never take the sim down; launched from a real console (cmd / the
# .bat) they stay the console.
for _std in ("stdout", "stderr"):
    if getattr(sys, _std, None) is None:
        try:
            setattr(sys, _std, open(os.devnull, "w"))
        except OSError:
            pass

# Optional multi-body printer model (printhead/gantry/bed + Frame-A kinematics).
# Guarded so the player still runs if the module is absent (e.g. launched by hand
# from an older tree); a missing module simply disables the machine view.
try:
    import machine_model
except ImportError:  # pragma: no cover - degrade to the single-nozzle marker
    machine_model = None

# Reduced-order MEW physics: the jet's diameter and lag come from this model
# (mew_physics.py) instead of fixed knobs. Kept optional so a hand-launched or
# partially-installed player still runs -- a missing module just falls back to
# the legacy fixed --lag / --diameter behaviour. mew_live (imported alongside --
# it needs mew_physics) powers the real-time adjustment mode's forward-
# accumulating re-lay; without it, --live-adjust degrades to the locked sim.
try:
    from mew_physics import ProcessParams
    import mew_calibration
    import mew_live
    import mew_incremental
    from mew_deposition import make_transform as _make_depo_transform
    _HAVE_PHYSICS = True
except Exception:  # noqa: BLE001 -- physics is an enhancement, never a hard dep
    _HAVE_PHYSICS = False

# Accel-limited motion planning (mew_kinematics) re-times the toolpath onto the
# machine's real trapezoidal trajectory -- Klipper accel + square-corner-velocity
# cornering -- so the print-time estimate, the playback clock, and the animated
# head reflect ramps and corner slowdowns instead of teleporting at full feed.
# Pure stdlib and INDEPENDENT of the jet model, so it is imported on its own:
# accel-limited timing works even with --no-physics. A missing module (or
# --max-accel <= 0) falls back to the naive length/feed timeline.
try:
    from mew_kinematics import plan_trajectory, axis_seg_accels
except Exception:  # noqa: BLE001
    plan_trajectory = axis_seg_accels = None

_perf("imports")

# DEFAULT_FEED (mm/min, used until the first F word) is shared with
# gcode_to_fibers so the line clock here and the deposition mesh built there
# agree on timing. In MEW the head holds ONE feedrate for the whole print, so
# travels (G0) move at this same modal speed as deposition.

# Deposition view tuning. The animated scaffold renders its tubes at the SAME facet
# fidelity as the export STL, NOT coarser. A polygonal tube's flat faces sit inside
# the true (circular) radius the sag rests fibres at -- by ~13% of the radius at 6
# sides, ~3% at 12 -- so a too-coarse preview makes touching fibres visibly float, or
# a thin fibre's vertex poke through a thick one's flat (the coarse-preview artefact).
# `DEPO_FACES` is the FLOOR (the export default); the deposition follows a higher
# export `--faces` so the preview is never coarser than the STL the user trusts. The
# higher triangle count is fine: revealed triangles are drawn in fixed-size time-
# chunks (`DEPO_CHUNK_TRIS`), so per-frame cost stays flat across the print regardless
# of the total -- only the chunk straddling the playhead is rebuilt each frame.
DEPO_FACES = 12
DEPO_CHUNK_TRIS = 100_000
# The jet is a varying-radius tube: fat at the bore (the Taylor cone) tapering to the
# fibre (mew_physics cone-jet profile, written per point by _jet_radii). The cone end
# is at the nozzle bore scale, so it reads rounder than a bare fibre -- 16 sides.
JET_SIDES = 16
# The jet centreline is a field-shaped arc (mew_field), and its radius profile necks
# fast near the nozzle (the cone), so it needs enough points up top to read smoothly.
# Still a tiny polyline -- cheap to re-sweep + re-radius each frame. 60 (with the
# height-clustered sampling in _jet_arc_points) puts ~20 points inside the fat cone,
# enough that the fastest-thinning first two segments span <10% of the radius drop.
JET_ARC_SEGMENTS = 60
# THE SHAPE HAS NO TUNING KNOB, deliberately. It is a catenary pinned to the nozzle
# and the touchdown, and its one parameter is solved from that geometry -- see
# _jet_arc_points. The retired knobs (JET_TURN_EXP / JET_BOW_GAIN / JET_TURN_MIN, and
# the _JET_FINE sub-stepping the tangent-angle integration needed) drew a curve whose
# NORMALISED shape was the same at every lag: 2% / 8% / 21% of the sideways travel
# used up at three-quarter / half / quarter height, whether the jet trailed 0.2 mm or
# 2 mm. A catenary spends 5-10% / 13-23% / 26-43% over that same range, which is the
# whole point -- more lag, more taut, the turn spread further up the gap.
# How strongly a HORIZONTAL field component pushes the arc sideways. Zero for the
# default uniform field (no lateral E); nonzero only once a source (tip
# enhancement, or a future deposited-fibre perturbation) breaks the symmetry --
# this is what makes those effects visible in the jet.
JET_NUDGE_GAIN = 0.4

# Real-time adjustment mode (mew_incremental): the deposition is
# time-marched as the clock advances, so a live parameter edit only appends a
# schedule breakpoint (O(1), the frozen-past / lazy-future model). The schedule is
# updated immediately on every edit; only the heavier VISUAL refresh (re-tube the
# in-progress layer + re-install the mesh) is DEBOUNCED -- a rapid knob-mash
# collapses to one tip re-tube this long (s) after the last change. A genuine new
# committed layer rebuilds promptly (it is not an edit).
RELAY_DEBOUNCE = 0.35
# Layers laid per advance_to call: bounds the per-frame work so a big scrub (or
# jump to the end) deposits PROGRESSIVELY over frames rather than freezing on one
# batch-sized solve. One layer's sag is the unit of work the march can't split.
PHASE4_LAYER_CAP = 1

# The player draws the head's ACTUAL path, so G2/G3 arcs are rendered as the true
# smooth curve the machine follows -- a fine fixed step, NOT the coarser STL
# preview fidelity (arc_segments). This is just a polyline + head animation, so a
# fine step is cheap; it only affects how round the drawn path looks, never the
# (lazily built, arc_segments-driven) deposition mesh.
_SMOOTH_ARC_STEP_DEG = 2.0

DEPOSIT_COLOR = (0.96, 0.76, 0.20)   # gold: material laid down so far
TRAVEL_COLOR = (0.42, 0.46, 0.56)    # dim grey: travel hops already executed
HEAD_COLOR = (1.0, 0.40, 0.12)       # warm nozzle marking the printhead (fallback)
PRINTHEAD_COLOR = (0.72, 0.74, 0.78) # brushed-metal real printhead STL
#: The printhead's opacity when the sidebar's "See-through printhead" switch is
#: on. The head is a 70 x 95 x 110 mm block whose tip is already 20 mm wide,
#: parked 4.5 mm over a print a few tens of mm across, so from many views it
#: covers the winding and the point where the fibre lands. A CAM simulator's
#: "transparent tool" does the same. Back faces are culled so the shell reads as
#: one surface rather than its overlapping walls. The switch is OFF (a solid
#: head) on every launch, like the other viewer switches.
PRINTHEAD_OPACITY = 0.25
JET_COLOR = (0.98, 0.85, 0.45)       # the molten polymer jet (needle -> fiber)
SCAFFOLD_COLOR = (0.80, 0.80, 0.86)  # solid scaffold-fiber STL overlay
# same colour as a 0..255 RGB triple -- the per-scaffold fallback for an
# uncoloured group, and the solid colour when no group carries a colour
SCAFFOLD_RGB255 = tuple(int(round(c * 255)) for c in SCAFFOLD_COLOR)
PLATE_COLOR = (0.38, 0.45, 0.56)     # matte neutral steel-blue build-plate overlay
                                     # (rendered flat/angle-stable in _toggle_overlay
                                     # so it never sweeps a hotspot as the camera orbits)

# Machine-model part colours (used only when a machine profile is loaded).
# PRINTHEAD_COLOR is shared with the real-printhead STL above; the gantry/bed/
# static body are progressively darker greys so the moving parts read apart.
GANTRY_COLOR = (0.46, 0.49, 0.55)
BED_COLOR = (0.38, 0.41, 0.47)
FRAME_COLOR = (0.30, 0.32, 0.37)


def format_duration(seconds):
    """A duration in seconds as a compact 'Xh Ym Zs' string, dropping units above
    the largest non-zero one ('2h 5m 30s', '5m 30s', '45s'). Matches the friendly
    print-time the scaffold generator and STL viewer show."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def format_rotary(deg, unit="°"):
    """A rotary reading as whole turns plus the angle within the turn.

    A mandrel's A word is a running total, so the raw number after a long
    print (162000) says nothing about where on the rod the nozzle is. This
    splits it the way a duration is split into hours and minutes: '450 turns
    + 12.3°'. Under one turn it is just the angle ('12.3°'), which is also
    every five-axis tilt. The sign is the whole reading's ('-1 turn - 10.0°').
    Rounded to a tenth of a degree first, so 359.96° reads '1 turn + 0.0°'
    and never '360.0°'."""
    v = float(deg)
    tenths = int(round(abs(v) * 10.0))
    turns, rest = divmod(tenths, 3600)
    sign = "-" if v < 0 and tenths else ""
    angle = "%.1f%s" % (rest / 10.0, unit)
    if not turns:
        return sign + angle
    return "%s%d turn%s %s %s" % (sign, turns, "" if turns == 1 else "s",
                                  "-" if sign else "+", angle)


def _render_sides(render):
    """Facet count from a 'tube:N' deposition render mode (0 = wireframe / not a
    tube). Mirrors stl_viewer._render_sides so the two views agree."""
    if render and render.startswith("tube:"):
        try:
            return max(3, int(render.split(":", 1)[1]))
        except ValueError:
            return 6
    return 0


def _line_cells(paths):
    """Line segments (M, 2) joining consecutive points WITHIN each path, indexed
    into the concatenated point array -- the wireframe deposition's cells."""
    cells, base = [], 0
    for p in paths:
        n = len(p)
        if n >= 2:
            idx = np.arange(base, base + n - 1)
            cells.append(np.column_stack([idx, idx + 1]))
        base += n
    return (np.concatenate(cells) if cells
            else np.zeros((0, 2), dtype=np.int64))


# --- locked-preview centreline decimation -------------------------------- #
# The sag solver samples each fibre at STL fidelity, which the locked preview
# cannot resolve and cannot afford to tube every frame. Thinning each fibre's
# centreline before tubing cuts the vertex/triangle count, and touches ONLY the
# preview: the .depo cache, the sag solve and the exported STL are untouched.
#
# The tolerance bounds how far the drawn TUBE WALL may move, not the centreline --
# `_decimate_paths` has the distinction and the measured curve.
#
# 0.1 d = 2 um on a 20 um fibre, at 1.36x the vertex count of the retired 0.2 d
# rule, which allowed 15.71 um. Raise it for FPS on very large scaffolds, lower it
# toward the exported mesh's 0.34 um for screenshots; the constant is the only knob.
_DEPO_DECIMATE_D = 0.1       # tube-WALL tolerance in fibre diameters (sub-fibre)
_REVEAL_DT = 0.03            # s: cap the PRINT-TIME gap between kept points so the
                             # deposition reveal stays smooth at 1x. Thinning collapses
                             # flat runs to a few points; the time-reveal would then pop
                             # a whole run in at once. Capping the gap in TIME (not arc
                             # length) makes the reveal advance at a steady ~1/dt points
                             # per print-second EVERYWHERE -- arc spacing looked smooth on
                             # fast straights but choppy wherever the head ran slow.
                             # 0.03s -> ~33 reveals/s at 1x playback.


def _thin_keep(P, tol):
    """Indices of P (N,3) kept by iterative vertex decimation at perpendicular
    tolerance `tol`: drop every interior vertex lying within `tol` of the chord between
    its current neighbours, re-evaluate, repeat until none remain -- the cheap
    vectorised cousin of Ramer-Douglas-Peucker (a few whole-array numpy passes instead
    of RDP's per-vertex Python recursion: ~20x faster on a 100k-point fibre, at a
    looser but still sub-fibre bound). Endpoints are always kept; a maximal NON-adjacent
    set is dropped each pass -- every other removable vertex, and since the removable
    positions are strictly increasing, taking alternates guarantees the two are never
    neighbours, so a chord is never collapsed onto a vertex removed in the same pass."""
    n = len(P)
    if n < 3:
        return np.arange(n, dtype=np.int64)
    alive = np.ones(n, dtype=bool)
    while True:
        idx = np.flatnonzero(alive)
        if len(idx) < 3:
            break
        a = P[idx[:-2]]
        ac = P[idx[2:]] - a
        ab = P[idx[1:-1]] - a               # interior vertices (idx[1:-1])
        L2 = np.einsum("ij,ij->i", ac, ac)
        t = np.where(L2 > 0.0,
                     np.einsum("ij,ij->i", ab, ac) / np.where(L2 > 0.0, L2, 1.0),
                     0.0)
        r = ab - t[:, None] * ac            # interior vertex -> foot on its chord
        dev = np.sqrt(np.einsum("ij,ij->i", r, r))
        rem = np.flatnonzero(dev < tol)[::2]        # non-adjacent removable interiors
        if len(rem) == 0:
            break
        alive[idx[1:-1][rem]] = False
    return np.flatnonzero(alive)


def _cadence_fill(idx, q, max_gap):
    """Add the fewest original indices to the sorted keep-set `idx` so no two
    consecutive kept points sit more than `max_gap` apart in the monotonic quantity `q`
    (here the per-point print TIME). Thinning collapses a flat run to a few points, but
    the time-reveal needs intermediate samples so the tube grows WITH the head instead
    of snapping a whole run in at once. Capping the gap in TIME (not arc length) makes
    the reveal advance at a steady rate everywhere, including where the head runs slow."""
    if max_gap <= 0.0 or len(idx) < 2:
        return idx
    out = [int(idx[0])]
    for a, b in zip(idx[:-1], idx[1:]):
        a, b = int(a), int(b)
        gap = float(q[b] - q[a])
        if gap > max_gap:
            k = int(gap // max_gap)                      # interior points needed
            targets = q[a] + (gap / (k + 1)) * np.arange(1, k + 1)
            extra = np.clip(np.searchsorted(q, targets), a + 1, b - 1)
            out.extend(int(e) for e in np.unique(extra))
        out.append(b)
    return np.array(sorted(set(out)), dtype=np.int64)


def _decimate_paths(paths, ptimes, diameter, pdia=None):
    """Thin each fibre's centreline (carrying its per-point reveal times) for the
    locked preview, the tolerance scaled per fibre by that fibre's diameter so a thick
    scaffold thins harder than a thin one, both staying sub-fibre. Same fibre count and
    order out, far fewer points each. Preview-only -- see _DEPO_DECIMATE_D.

    Uses the EXPORT's decimator (`gcode_to_fibers.decimate_keep`), not a plain
    centreline RDP, and the reason is the crossover crests. A tube swept along a
    path whose tangent turns by theta has its WALL displaced by about r*theta on
    top of whatever the centreline moved -- so a rule that only bounds the
    centreline lets the drawn surface move much further than it promises, and it
    does so worst exactly at tight bends. A crest is a tight bend. Measured on a
    10x10x10 at 20 um fibre, worst tube-surface displacement:

        _thin_keep @ 0.2 d (the old rule)    80 613 pts    15.71 um
        decimate_keep @ 0.2 d                95 469 pts     4.00 um
        decimate_keep @ 0.1 d (shipped)     109 487 pts     2.00 um
        the exported mesh, for reference    198 406 pts     0.34 um

    15.7 um of wall movement on a 20 um fibre is most of the fibre, and it was
    the reported gap between "Full mesh" and "Fibres (smooth)" on identical
    geometry -- the same sagged centreline, thinned two ways."""
    out_p, out_t = [], []
    for i, p in enumerate(paths):
        p = np.asarray(p, dtype=np.float64)
        t = np.asarray(ptimes[i], dtype=np.float64)
        if len(p) < 3:
            kp, kt = _resample_by_time(p, t, _REVEAL_DT)
            keep = _dedupe_idx(kp)
            out_p.append(kp[keep])
            out_t.append(kt[keep])
            continue
        d = float(pdia[i]) if (pdia is not None and i < len(pdia)) else diameter
        idx = decimate_keep(p, _DEPO_DECIMATE_D * max(d, 1e-6),
                            radius=0.5 * max(d, 1e-6))
        idx = _cadence_fill(idx, t, _REVEAL_DT)     # keep the reveal smooth in TIME
        # ...and where the source has no sample to re-add, ADD one. `_cadence_fill`
        # can only SELECT existing indices, which is enough only while the source is
        # densely sampled in time. The bottom layer is not: it crosses nothing, so
        # the sag solver never subdivides it and a straight pass survives as its two
        # raw g-code vertices -- one ring interval for the whole pass, so the fibre
        # froze and the turnaround then flooded in at the corner.
        #
        # `_resample_by_time` is the EXPORT's own answer to this and `_tube_fibers`
        # has always called it, which is why only this preview branch had the fault.
        # Reused rather than re-derived; the dedupe after it is the pairing that
        # caller uses.
        kp, kt = _resample_by_time(p[idx], t[idx], _REVEAL_DT)
        keep = _dedupe_idx(kp)
        out_p.append(kp[keep])
        out_t.append(kt[keep])
    return out_p, out_t


def _conn_dtype(n_verts):
    """The index type the reveal keeps its connectivity in: int32 while the
    vertex count fits (every scaffold so far, by three orders of magnitude),
    int64 past it. `vtkCellArray.SetData` takes either and keeps the array it
    is given, so an int32 view of the sorted connectivity is drawn with no
    copy (see `_reveal_deposition`)."""
    return np.int32 if n_verts < 2**31 - 1 else np.int64


def _tube_via_filter(paths, ptimes, diameter, sides, pdia=None, path_color=None):
    """Tube the sagged centrelines with vtkTubeFilter (fast C++) at `sides` facets,
    carrying each point's print time through so the time-reveal still works. Returns
    (verts, tris (T,3), vtimes per vertex, normals, colours) -- the same shape
    _tube_fibers returns, so the deposition build treats both uniformly. `colours`
    is an (V,3) uint8 per-vertex RGB when `path_color` is given (one RGB per fibre),
    else None -- so the tube/wireframe preview colours scaffolds exactly like the
    exact mesh and the STL viewer, instead of collapsing to one solid grey.

    `pdia` (one diameter per fibre) makes the tube radius VARY per scaffold: each
    fibre's points carry their own radius and vtkTubeFilter sizes the tube to it, so
    a multi-diameter stack renders each scaffold at its true diameter. When the
    diameters are all equal (or pdia is absent) it's one constant radius, as before."""
    import gcode_to_fibers as _g2f
    mult = _g2f.TUBE_RADIUS_MULT      # --diameter-multiplier: drawing only
    allpts = np.concatenate(paths).astype(np.float32)
    alltime = np.concatenate([np.asarray(t, dtype=np.float64) for t in ptimes])
    # per-point radius from per-fibre diameters -- only when they actually differ
    radii = None
    if pdia is not None and len(pdia) == len(paths) \
            and len({round(float(d), 9) for d in pdia}) > 1:
        radii = np.concatenate([np.full(len(p), float(pdia[i]) / 2.0)
                                for i, p in enumerate(paths)]).astype(np.float64) * mult
    # per-point RGB from the per-fibre scaffold colours: every point of a fibre
    # carries that scaffold's colour, so vtkTubeFilter propagates it to each ring
    # vertex (exactly how it carries the "t" reveal array). Colour is constant
    # within a fibre and each fibre is a separate line, so no cross-fibre bleeding.
    want_color = (path_color is not None and len(path_color) == len(paths))
    pts = vtk.vtkPoints()
    pts.SetData(numpy_support.numpy_to_vtk(np.ascontiguousarray(allpts), deep=1))
    lens = np.array([len(p) for p in paths], dtype=np.int64)
    offs = np.concatenate([[0], np.cumsum(lens)]).astype(np.int64)
    conn = np.arange(int(lens.sum()), dtype=np.int64)
    cells = vtk.vtkCellArray()
    cells.SetData(numpy_support.numpy_to_vtkIdTypeArray(offs, deep=1),
                  numpy_support.numpy_to_vtkIdTypeArray(conn, deep=1))
    poly = vtk.vtkPolyData()
    poly.SetPoints(pts)
    poly.SetLines(cells)
    # print time rides as a named array (propagated through the filter for the
    # reveal); when radius varies it must NOT be the active scalar -- that's radius
    ts = numpy_support.numpy_to_vtk(np.ascontiguousarray(alltime), deep=1)
    ts.SetName("t")
    poly.GetPointData().AddArray(ts)
    if want_color:
        rgb = np.concatenate([np.tile(np.asarray(path_color[i], dtype=np.uint8),
                                      (len(p), 1)) for i, p in enumerate(paths)])
        rgba = numpy_support.numpy_to_vtk(np.ascontiguousarray(rgb, dtype=np.uint8),
                                          deep=1)
        rgba.SetName("scaffold_rgb")
        poly.GetPointData().AddArray(rgba)
    tube = vtk.vtkTubeFilter()
    tube.SetInputData(poly)
    tube.SetNumberOfSides(int(sides))
    tube.CappingOn()
    if radii is not None:
        rad = numpy_support.numpy_to_vtk(np.ascontiguousarray(radii), deep=1)
        rad.SetName("radius")
        poly.GetPointData().SetScalars(rad)         # active scalar drives the radius
        tube.SetVaryRadiusToVaryRadiusByAbsoluteScalar()
    else:
        poly.GetPointData().SetActiveScalars("t")   # harmless; radius stays constant
        tube.SetRadius(max(diameter, 1e-6) / 2.0 * mult)
    trifilter = vtk.vtkTriangleFilter()      # tubes -> pure triangles for the reveal
    trifilter.SetInputConnection(tube.GetOutputPort())
    trifilter.Update()
    out = trifilter.GetOutput()
    # No widening copies. The filter's points and normals are already float32
    # (the input points are), and `_install_depo_mesh` uploads float32, so a
    # float64 copy here was a third of the transient peak and bought nothing.
    # The connectivity goes out as int32 (6 M vertices against 2.1 G), which
    # is what `_install_depo_mesh` keeps; the filter's int64 array dies with
    # `out`. The times stay float64 because the accel-limited clock map and
    # the deviation bake run on them before the install narrows them.
    verts = numpy_support.vtk_to_numpy(out.GetPoints().GetData())
    vtimes = np.asarray(numpy_support.vtk_to_numpy(
        out.GetPointData().GetArray("t")), dtype=np.float64)
    tris = numpy_support.vtk_to_numpy(
        out.GetPolys().GetConnectivityArray()).astype(
            _conn_dtype(len(verts))).reshape(-1, 3)
    nrm = out.GetPointData().GetNormals()
    normals = (numpy_support.vtk_to_numpy(nrm)
               if nrm is not None else np.zeros((len(verts), 3), np.float32))
    colors = None
    if want_color:
        cvtk = out.GetPointData().GetArray("scaffold_rgb")
        if cvtk is not None:
            colors = numpy_support.vtk_to_numpy(cvtk).astype(np.uint8)
    return verts, tris, vtimes, normals, colors


# --------------------------------------------------------------------------- #
# G-code parsing -> an ordered move table
# --------------------------------------------------------------------------- #
def parse_path(path, frame=None, five=None):
    """Parse the file into the toolpath the head actually follows.

    Every move is kept in execution order, including the lead-in travel from the
    origin, unlike `gcode_to_fibers.parse_gcode`. G90 (absolute) is assumed.

    Returns (verts, times, is_dep, seg_feeds, seg_line, dep_feed, rotary):
      verts    (P,3) f8 -- vertices in command order; verts[0] is the machine
               origin (0,0,0) the first move departs from.
      times    (P,) f8 -- cumulative seconds to REACH each vertex at the commanded
               feedrate; times[0] == 0.
      is_dep   (P-1,) bool -- segment j is a G1 deposition move, else a G0 travel.
      seg_feeds (P-1,) f8 -- the feedrate (mm/min) on each segment, so the jet
               physics runs off the speed AT THE PLAYHEAD, not one number. The
               modal F word on three and four axes; on FIVE it is the surface
               speed the deposition point actually holds, `F * length / span`,
               because there F is not a deposition speed -- see `five_span`.
      seg_line (P-1,) i8 -- the 1-based source line the segment came from; an arc
               expands to sub-segments all carrying its one line.
      dep_feed float -- the most-deposited feedrate, used for the initial solve.
      rotary   None on a flat collector, else (mach, rot): the commanded MACHINE
               point per vertex, and the rotary reading in degrees -- a SCALAR per
               vertex on a mandrel's one joint, a (P, J) array on a five-axis
               pair, collector-first with `machine.rotaries`.

    `frame` is a `mew_frame.DepositionFrame`, else None for the flat collector. A
    ROTARY frame changes three things: the modal state carries `A`; `verts` come back
    in the collector's co-rotating frame, densified across the curvature because a
    segment straight in (u, v) is a helix in space; and segment length -- so the
    clock -- is McColl Eq 1.3's effective velocity at the mean deposition radius,
    matching `gcode_to_fibers.parse_gcode` so the two clocks cannot drift.

    `five` is a `gcode_to_fibers.FiveAxisReader` and makes this the FIVE-AXIS
    parser. It replaces the CHART, not merely the word count, and that is the
    whole of the difference: a four-axis file is unrolled to (u, v, h) because a
    cylinder is developable, while a doubly-curved collector has no global chart
    to unroll to (spec section 5 rule 2). So the reader answers in the
    COLLECTOR'S OWN XYZ and there is no `unroll` call on this path at all --
    `RevolutionFrame.unroll` raises precisely to stop the three-axis one being
    reused here. Segment length is then the plain Euclidean distance the
    deposition point travels in that frame, and needs no metric to say it --
    but the CLOCK does not divide that by F. It divides the firmware's own
    mixed mm-and-degrees norm, `five_span`; on a five-axis file those two are
    different numbers, and near a pole they differ by more than an order of
    magnitude. The WORDS COME FROM THE JOINTS, never from a letter written
    here: Terranova's U/V are their rig's convention, not a standard."""
    rotary = frame is not None and getattr(frame, "rotary", False)
    if five is not None and not rotary:
        raise ValueError("a five-axis read-back needs a rotary frame")
    pos = [0.0, 0.0, 0.0]
    ang = 0.0                    # the modal rotary word, degrees
    feed = DEFAULT_FEED
    # SPLIT FEEDS on a mandrel (Aerotech): F is the linear axes' rate, E the
    # rotary's in deg/min, the clock is `rotary_split_seconds`, and `seg_feeds`
    # carries the SURFACE speed the move works out to -- the number F carries
    # on a combined file. Twin of `parse_gcode`'s; the two must not drift.
    split_rot = rotary and five is None
    split = False
    rot_feed = None
    per = 1.0                    # 60 after G76

    # THE FIVE-AXIS MODAL STATE is a list of readings, one per joint, because the
    # words that set them belong to the rig and not to this parser.
    five_words = tuple(five.words) if five is not None else ()
    angs = [0.0] * len(five_words)

    def surface(p, a):
        return frame.unroll(p[0], p[1], p[2], a)

    def world(p, a):
        return list(frame.to_world(*surface(p, a)))

    def five_world(p, aa):
        """Machine state -> the deposition point in the collector's own frame."""
        st = {"X": p[0], "Y": p[1], "Z": p[2]}
        st.update(zip(five_words, aa))
        return five.collector_point(st)

    def five_len(p0, a0, p1, a1):
        """How far the DEPOSITION POINT travels, mm. In the collector's own
        frame that is plain Euclidean: the chart that would need a metric is the
        one this parser deliberately does not build.

        This is the PHYSICS weight -- how much fibre this move lays -- and no
        longer the clock's divisor; see `five_span`."""
        return math.dist(five_world(p0, a0), five_world(p1, a1))

    def five_span(p0, a0, p1, a1):
        """What the CLOCK divides by the feed: `mew_kinematics.machine_norm`,
        the firmware's mixed mm-and-degrees norm over the COMMANDED deltas.

        Twin of `gcode_to_fibers.parse_gcode`'s `feed_span`, which carries the
        reasoning; the two must not drift. In short: F belongs to the
        controller, and the controller does not know where the deposition point
        is. Timing a five-axis move by the deposition point's own travel made
        the player 16.9x fast on an apex-centred dome (1309.3 s on the
        controller, 77.6 s here), because near a pole the
        rotary words carry almost all of the norm.

        `rot_len` is deliberately NOT given the same treatment: the four-axis
        clock is McColl Eq 1.3's effective velocity and is its own decision,
        validated against its own prints."""
        import mew_kinematics

        return mew_kinematics.machine_norm(
            [b - a for a, b in zip(p0, p1)], [b - a for a, b in zip(a0, a1)])

    def five_steps(p0, a0, p1, a1):
        """Sub-vertices per move, from how far the COLLECTOR turns -- the same
        rule and the same constant as the mandrel's `rot_steps`, taken over the
        joint that swings furthest, because either one bends the drawn curve."""
        swing = max((abs(b - a) for a, b in zip(a0, a1)), default=0.0)
        return max(1, int(math.ceil(swing / _SMOOTH_ARC_STEP_DEG)))

    def rot_len(p0, a0, p1, a1):
        """McColl Eq 1.3's effective velocity path -- the distance the DEPOSITION
        POINT travels, which is what the clock divides by the feed. Kept
        expression-for-expression identical to `parse_gcode`'s `path_len`."""
        u0, v0, h0 = surface(p0, a0)
        u1, v1, h1 = surface(p1, a1)
        a, b, c = frame.metric(0.5 * (u0 + u1), 0.5 * (v0 + v1),
                               0.5 * (h0 + h1))
        arc = ((v1 - v0) - c * (u1 - u0)) * b
        return math.sqrt(((u1 - u0) * a) ** 2 + arc ** 2 + (h1 - h0) ** 2)

    def rot_steps(p0, a0, p1, a1):
        """How many sub-vertices to draw this move as. One per `_SMOOTH_ARC_STEP_DEG`
        of mandrel rotation, the same rule the G2/G3 expansion uses for a planar
        arc -- and for the same reason: the drawn line has to follow the curve the
        machine runs, not the chord between its endpoints."""
        return max(1, int(math.ceil(abs(a1 - a0) / _SMOOTH_ARC_STEP_DEG)))

    if five is not None:
        verts = [five_world(pos, angs)]
    elif rotary:
        verts = [world(pos, ang)]
    else:
        verts = [list(pos)]
    mach = [list(pos)]
    rots = [list(angs) if five is not None else ang]
    times = [0.0]
    is_dep = []
    seg_feeds = []          # modal feed (mm/min) per segment, parallel to is_dep
    seg_line = []           # 1-based source g-code line per segment, parallel too
    t_cum = 0.0
    # deposited length at each feedrate, so the physics model can be fed the
    # print's representative deposition speed (the most-deposited feed wins)
    dep_len_by_feed = {}
    # five-axis only: [total deposited mm, sum(mm * surface feed)] -- see the
    # `surf_feed` comment in the five-axis branch for why the modal bucket is
    # not the right representative there.
    five_dep_len = [0.0, 0.0]

    with open(path, "r", errors="ignore") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.split(";", 1)[0].strip()
            if not line:
                continue
            tokens = line.split()
            code = tokens[0].upper()

            # arcs (G2 CW / G3 CCW): expand into a FINE smooth polyline so the
            # drawn head path follows the true curve the machine runs (not the
            # coarser arc_segments STL preview). Each sub-segment is a deposition
            # move. Absolute coords (G90, always emitted here); I/J are the centre
            # offset from the arc start, Z is modal.
            if code in ("G2", "G3", "G02", "G03"):
                ex, ey, ez = pos
                ci = cj = 0.0
                for tok in tokens[1:]:
                    axis = tok[0].upper()
                    try:
                        val = float(tok[1:])
                    except ValueError:
                        continue
                    if axis == "X":
                        ex = val
                    elif axis == "Y":
                        ey = val
                    elif axis == "Z":
                        ez = val
                    elif axis == "I":
                        ci = val
                    elif axis == "J":
                        cj = val
                    elif axis == "F" and val > 0:
                        feed = val
                prevx, prevy = pos[0], pos[1]
                for (px_, py_) in arc_xy_points(pos[0], pos[1], ex, ey, ci, cj,
                                                code in ("G2", "G02"),
                                                step_deg=_SMOOTH_ARC_STEP_DEG):
                    length = math.hypot(px_ - prevx, py_ - prevy)
                    if length > 1e-12:
                        t_cum += length / (feed / 60.0)
                        verts.append([px_, py_, ez])
                        mach.append([px_, py_, ez])
                        rots.append(list(angs) if five is not None else ang)
                        times.append(t_cum)
                        is_dep.append(True)
                        seg_feeds.append(feed)
                        seg_line.append(lineno)
                        dep_len_by_feed[feed] = dep_len_by_feed.get(feed, 0.0) + length
                        prevx, prevy = px_, py_
                pos = [ex, ey, ez]
                continue

            if split_rot and code in ("G75", "G76"):
                split = True
                per = 60.0 if code == "G76" else 1.0
                continue
            if code not in ("G0", "G1", "G00", "G01"):
                continue

            new_pos = list(pos)
            new_ang = ang
            new_angs = list(angs)
            for tok in tokens[1:]:
                axis = tok[0].upper()
                try:
                    val = float(tok[1:])
                except ValueError:
                    continue
                if axis == "X":
                    new_pos[0] = val
                elif axis == "Y":
                    new_pos[1] = val
                elif axis == "Z":
                    new_pos[2] = val
                elif five is not None and axis in five_words:
                    new_angs[five_words.index(axis)] = val
                elif axis == "A" and rotary and five is None:
                    new_ang = val
                elif axis == "F" and val > 0:
                    feed = val * per
                elif axis == "E" and split_rot:
                    split = True
                    if val > 0:
                        rot_feed = val * per

            if five is not None:
                # the whole move, drawn as the arc the collector actually swings
                # through, timed on the FIRMWARE's norm and weighted for the
                # physics by the deposition point's own travel
                length = five_len(pos, angs, new_pos, new_angs)
                span = five_span(pos, angs, new_pos, new_angs)
                # GATED ON THE MACHINE, NOT ON THE SURFACE. `span` is zero only
                # when no axis moved at all, and a move that turns the collector
                # under a stationary contact point -- which `surface_feeds`
                # names and keeps at `speed` -- takes real time on the machine.
                # `length > 0` implies `span > 0`, so this admits every move a
                # length gate would, and more.
                if span > 1e-12:
                    deposit = code in ("G1", "G01")
                    # THE SURFACE FEED, NOT THE F WORD -- `seg_feeds` exists to
                    # tell the jet physics how fast the DEPOSITION POINT is
                    # moving (see the return contract above), and on this rig
                    # the F word is not that. `F * length / span` is
                    # `mew_kinematics.surface_feeds` read backwards: the
                    # emitter writes `speed * norm / arc` to hold the surface
                    # at `speed`, so this returns exactly `speed` on a
                    # --surface-feed-comp file and the machine's true, varying
                    # surface speed on an uncompensated one. Left as F, the two
                    # disagree and the jet foot -- placed at where the nozzle
                    # was `lag` SECONDS ago -- collapses onto the nozzle: the
                    # dome's median foot-behind-nozzle goes -0.696 -> -0.037 mm.
                    surf_feed = feed * (length / span)
                    if deposit:
                        dep_len_by_feed[surf_feed] = (
                            dep_len_by_feed.get(surf_feed, 0.0) + length)
                        # ...and the length-weighted mean, because on an
                        # uncompensated five-axis file every segment has its
                        # own surface speed, so "the most-deposited feed" picks
                        # an arbitrary singleton bucket. Only the INITIAL solve
                        # reads it; `_update_deposit_speed` corrects at the
                        # playhead from the first frame.
                        five_dep_len[0] += length
                        five_dep_len[1] += length * surf_feed
                    n = five_steps(pos, angs, new_pos, new_angs)
                    for i in range(1, n + 1):
                        f = float(i) / n
                        p_i = [pos[k] + (new_pos[k] - pos[k]) * f
                               for k in range(3)]
                        a_i = [angs[k] + (new_angs[k] - angs[k]) * f
                               for k in range(len(angs))]
                        # `machine_norm` is a norm of deltas that are LINEAR in
                        # `f`, so the sub-step's own norm is exactly `span / n`.
                        t_cum += (span / n) / (feed / 60.0)
                        verts.append(five_world(p_i, a_i))
                        mach.append(list(p_i))
                        rots.append(list(a_i))
                        times.append(t_cum)
                        is_dep.append(deposit)
                        seg_feeds.append(surf_feed)
                        seg_line.append(lineno)
                pos, angs = new_pos, new_angs
                continue

            if rotary:
                # the whole move, drawn as a helix and timed on the surface path
                length = rot_len(pos, ang, new_pos, new_ang)
                if length > 1e-12:
                    deposit = code in ("G1", "G01")
                    secs = (rotary_split_seconds(math.dist(pos, new_pos),
                                                 abs(new_ang - ang), feed,
                                                 rot_feed)
                            if split else None)
                    if secs is None or not secs > 0.0:
                        # the combined clock, expression-for-expression as it
                        # always was, so a combined file times bit-identically
                        secs = None
                        surf_feed = feed
                    else:
                        surf_feed = 60.0 * length / secs
                    if deposit:
                        dep_len_by_feed[surf_feed] = (
                            dep_len_by_feed.get(surf_feed, 0.0) + length)
                    n = rot_steps(pos, ang, new_pos, new_ang)
                    for i in range(1, n + 1):
                        f = float(i) / n
                        p_i = [pos[k] + (new_pos[k] - pos[k]) * f for k in range(3)]
                        a_i = ang + (new_ang - ang) * f
                        t_cum += (secs / n if secs is not None
                                  else (length / n) / (feed / 60.0))
                        verts.append(world(p_i, a_i))
                        mach.append(list(p_i))
                        rots.append(a_i)
                        times.append(t_cum)
                        is_dep.append(deposit)
                        seg_feeds.append(surf_feed)
                        seg_line.append(lineno)
                pos, ang = new_pos, new_ang
                continue

            length = math.dist(pos, new_pos)
            if length <= 1e-12:
                # zero-length move (e.g. a MEWron G0 marker): no head motion,
                # nothing to time or draw, but it can still carry an F update.
                pos = new_pos
                continue

            deposit = code in ("G1", "G01")
            # uniform speed: travels move at the same modal feed as deposition
            # (MEW holds one F the whole print)
            t_cum += length / (feed / 60.0)
            if deposit:
                dep_len_by_feed[feed] = dep_len_by_feed.get(feed, 0.0) + length

            verts.append(list(new_pos))
            mach.append(list(new_pos))
            rots.append(list(angs) if five is not None else ang)
            times.append(t_cum)
            is_dep.append(deposit)
            seg_feeds.append(feed)
            seg_line.append(lineno)
            pos = new_pos

    if five is not None and five_dep_len[0] > 0.0:
        dep_feed = five_dep_len[1] / five_dep_len[0]
    else:
        dep_feed = (max(dep_len_by_feed, key=dep_len_by_feed.get)
                    if dep_len_by_feed else feed)
    # `mach`/`rots` are grown on every path so the arcs and the flat moves stay
    # parallel to `verts` without a second walk; they are only HANDED BACK on a
    # rotary frame, where a caller needs them, and are the same arrays as `verts`
    # otherwise.
    rot_out = None
    if rotary:
        # `rots` is (P,) on one joint and (P, J) on a pair; every consumer below
        # is written to take either, so the mandrel's array shape is untouched.
        rot_out = (np.asarray(mach, dtype=np.float64),
                   np.asarray(rots, dtype=np.float64))
    return (np.asarray(verts, dtype=np.float64),
            np.asarray(times, dtype=np.float64),
            np.asarray(is_dep, dtype=bool),
            np.asarray(seg_feeds, dtype=np.float64),
            np.asarray(seg_line, dtype=np.int64),
            dep_feed, rot_out)


def line_to_time(seg_line, times, line_no):
    """Playback time for a given 1-based g-code line number ("find in simulation").

    `seg_line` (P-1,) is the source line of each segment (from `parse_path`, and it
    is non-decreasing -- segments are in file order). `times` (P,) is the segment
    boundary clock. Returns the START time of the first segment whose source line is
    >= `line_no`: exactly that line when it is a motion line, else the next move
    after a comment / blank / non-motion line. Past the last motion line -> the end
    (`times[-1]`); empty path -> 0.0."""
    seg_line = np.asarray(seg_line)
    if seg_line.size == 0 or len(times) == 0:
        return 0.0
    j = int(np.searchsorted(seg_line, line_no, side="left"))
    if j >= seg_line.size:
        return float(times[-1])
    return float(times[j])


def line_byte_starts(data):
    """Byte offset of the START of each 1-based g-code line, as an int64 array.

    `data` is the file's RAW BYTES. The split is on b"\\n" and each line costs
    `len(line) + 1` bytes, which is exactly how Klipper's virtual_sdcard accounts for
    what it has read (`next_file_position = self.file_position + len(line.encode())
    + 1`, klippy/extras/virtual_sdcard.py) -- so this is the inverse of the printer's
    own arithmetic, not an approximation of it. A CRLF file needs no special case:
    the "\\r" stays inside the line and is counted.

    The result is ascending, so `byte_to_line` is a `searchsorted`. Element i is the
    start of line i+1; there is always at least one line."""
    parts = data.split(b"\n")
    starts = np.empty(len(parts), dtype=np.int64)
    off = 0
    for i, p in enumerate(parts):
        starts[i] = off
        off += len(p) + 1
    return starts


# ---------------------------------------------------------------------------
# Opt-in live-reveal trace. Off unless MEW_REVEAL_TRACE names a file.
#
# It exists because the offline replay harness is NOT the running viewer: the
# replay has reported the drawn edge within 0.019 s of the nozzle (p99) on the same
# recorded feed where the real window visibly froze for a whole pass. No harness
# that reconstructs the tick can settle that -- only the tick itself. So this writes
# one row per `_tick_machine_live` from inside the drawing process, and it is what
# any claim about live smoothness must be checked against.
#
#   set MEW_REVEAL_TRACE=C:\path\trace.csv   (then launch the GUI)
# ---------------------------------------------------------------------------
_TRACE_PATH = os.environ.get("MEW_REVEAL_TRACE") or ""
_TRACE_FH = None
_TRACE_COLS = ("wall,t,t_read,reveal_t,lines_t,raw_n,cell_hi,depo_n,seg_hi,hx,hy,state,lead_max\n")


def _reveal_trace(p):
    """One row per live tick: wall clock, playhead, file cursor, nozzle, and what
    the deposition reveal has actually drawn. No-op unless MEW_REVEAL_TRACE is set."""
    global _TRACE_FH
    if not _TRACE_PATH:
        return
    try:
        if _TRACE_FH is None:
            _TRACE_FH = open(_TRACE_PATH, "w", buffering=1)
            _TRACE_FH.write(_TRACE_COLS)
        line = -1
        if p._byte_starts is not None and p._live_filepos is not None:
            line = byte_to_line(p._byte_starts, p._live_filepos)
        t_read = (line_to_time(p.seg_line, p.times, line) if line > 0 else -1.0)
        h = p._live_head
        # The three quantities that can pin the deposition reveal while the
        # playhead runs on: the time the MESH is cut at, the raw searchsorted
        # answer, and the ratchet that may be overriding it. `lines_t` is beside
        # them because the commanded path is drawn from that one and was reported
        # smooth in the same window the fibre froze -- so logging only one of the
        # two cut times cannot tell them apart.
        rt = float(p._reveal_time())
        tt = p._depo_sorted_tt
        raw_n = int(np.searchsorted(tt, rt, side="right")) if tt is not None else -1
        _TRACE_FH.write(
            "%.4f,%.4f,%.4f,%.4f,%.4f,%d,%d,%d,%d,%.3f,%.3f,%s,%.3f\n" % (
                time.time(), float(p.t), float(t_read), rt,
                float(p._lines_reveal_time()), raw_n, int(p._live_cell_hi),
                int(p._depo_last_n), int(p._live_seg_hi),
                float(h[0]), float(h[1]), p._live_state or "",
                float(p._live_lead_max)))
    except Exception:                                   # never break the viewer
        pass


def byte_to_line(starts, pos):
    """The 1-based g-code line a byte offset falls in (`starts` from
    `line_byte_starts`).

    The printer reports `virtual_sdcard.file_position` as the offset it has read TO,
    which after dispatching line N is the START of line N+1. Reading that back with
    `side="right"` therefore names the line the machine is about to execute -- the
    read cursor. Clamped into [1, n_lines]; a negative offset reads as line 1."""
    if starts.size == 0:
        return 1
    n = int(np.searchsorted(starts, int(pos), side="right"))
    return min(max(n, 1), int(starts.size))


def time_to_line(seg_line, times, t):
    """Inverse of `line_to_time`: the source g-code line the playhead is executing
    at time `t`. Returns the segment's source line, or 0 for an empty path."""
    seg_line = np.asarray(seg_line)
    if seg_line.size == 0 or len(times) == 0:
        return 0
    seg = int(np.searchsorted(times, t, side="right")) - 1
    seg = min(max(seg, 0), seg_line.size - 1)
    return int(seg_line[seg])


def jet_source_keep_mask(ptimes):
    """Concatenated keep-mask for the jet's anchor source, dropping each fibre's
    points at/after the NEXT fibre's first deposition time.

    The jet's bottom is found by interpolating a SINGLE time-sorted array of every
    laid point. But a finished fibre's touchdown LINGERS past its nozzle: the jet
    lags, so the trailing material lands after the head has left (and a held settle
    tail extends it further). Where the next fibre begins before that lingering tail
    ends -- a layer change into a LINKING LOOP is the worst case, the loop hovering at
    the shared corner the previous layer just finished on -- the time sort INTERLEAVES
    the two fibres' points, and the jet bottom oscillates between the current
    touchdown and the previous fibre's end (the linking-loop "jet stutter"). Ending
    each fibre's contribution when the next starts depositing keeps the jet on the
    fibre actually being laid. `ptimes` is the per-fibre list of point times (the
    cache's naive clock, or the live re-lay's, in deposition order); the last fibre
    keeps every point (nothing deposits after it). At least the first point of every
    fibre is always kept, so a fibre never vanishes from the anchor."""
    masks = []
    n = len(ptimes)
    for i in range(n):
        t = np.asarray(ptimes[i], dtype=np.float64)
        if i + 1 < n and len(ptimes[i + 1]):
            m = t < float(np.asarray(ptimes[i + 1])[0])
            if not m.any():
                m[:1] = True
        else:
            m = np.ones(len(t), dtype=bool)
        masks.append(m)
    return (np.concatenate(masks) if masks
            else np.zeros(0, dtype=bool))


def _catenary_a(lag, drop):
    """The catenary parameter a > 0 satisfying a*(cosh(lag/a) - 1) == drop.

    Both arguments in mm and strictly positive. This is the ONE number the drawn
    jet shape has, and it is solved from the geometry rather than tuned -- see
    _jet_arc_points.

    Solved in the SCALE-FREE variable t = lag/a, where the equation becomes
        (cosh(t) - 1) / t == drop / lag
    whose left side rises monotonically 0 -> inf. That change of variable is not
    cosmetic: bracketing `a` directly has to span lag**2 / (2*drop), which blows up
    whenever the lag dwarfs the drop, and a bracket that is merely generous
    (max(lag, drop) * 10) silently returns its own upper bound instead of a root --
    the curve then misses the touchdown entirely. Bracketing t needs no such guess.

    EVALUATED AS 2*sinh(t/2)**2 / t, NOT as the literal (cosh(t) - 1) / t. Those are
    the same function and they do not compute the same way: cosh(t) - 1 is a
    subtraction of two nearly equal numbers, so it is already 2% low at t = 1e-7 and
    returns EXACTLY ZERO below t ~ 1.5e-8. A left side pinned at zero is flat, so the
    bisection stops finding the root and lands on the cancellation threshold instead.
    The half-angle identity has no subtraction in it and is exact to any small t.
    """
    ratio = drop / lag
    def g(t):
        return 2.0 * math.sinh(0.5 * t) ** 2 / t
    lo, hi = 1e-9, 1.0
    for _ in range(200):                      # grow until the root is bracketed
        if g(hi) >= ratio:
            break
        lo, hi = hi, hi * 2.0
    for _ in range(200):                      # bisect: ~1e-60 on the initial width
        mid = 0.5 * (lo + hi)
        if g(mid) < ratio:
            lo = mid
        else:
            hi = mid
    return lag / (0.5 * (lo + hi))


# --------------------------------------------------------------------------- #
# VTK helpers
# --------------------------------------------------------------------------- #
def _travel_spans(times, is_dep):
    """[(t0, t1)] for every contiguous run of TRAVEL segments in a toolpath.

    `times` is per-vertex (P,), `is_dep` per-segment (P-1,), as `parse_path`
    returns them. A run of travel segments i..j becomes (times[i], times[j+1]),
    so the interval is exactly the wall-clock the head spent travelling.

    This is how the deposition mesh is split back into travel fibre and scaffold
    fibre. The alternative -- having the converter record where each travel sits
    in each cached fibre -- cannot work: `merge_travels` splices a travel into
    the front of a fibre and the sag solve then resamples it (and
    `_split_layer_spanning` can dice one fibre into several), so no index written
    at merge time still means anything by the time the player reads the cache. A
    print time survives all of it, and it is the same quantity the converter cuts
    the STL on (`gcode_to_fibers._drop_travel_spans`).
    """
    is_dep = np.asarray(is_dep, dtype=bool)
    times = np.asarray(times, dtype=np.float64)
    if is_dep.size == 0 or times.size < 2:
        return []
    trv = ~is_dep
    if not trv.any():
        return []
    edge = np.flatnonzero(np.diff(
        np.concatenate(([False], trv, [False])).astype(np.int8)))
    return [(float(times[a]), float(times[min(b, times.size - 1)]))
            for a, b in zip(edge[0::2], edge[1::2])]      # segments [a, b)


def _line_actor(points, color, width, lighting):
    """A polyline actor that SHARES `points` (a vtkPoints holding every vertex);
    its segments are set later, per frame, by _set_segments. Empty to start."""
    poly = vtk.vtkPolyData()
    poly.SetPoints(points)
    poly.SetLines(vtk.vtkCellArray())
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(poly)
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    prop = actor.GetProperty()
    prop.SetColor(*color)
    prop.SetLineWidth(width)
    prop.SetLighting(lighting)
    return poly, actor


def _set_segments(poly, seg_starts):
    """Point `poly`'s line cells at the segments whose START vertex indices are
    in `seg_starts` (each segment j connects vertex j -> j+1). Connectivity is
    built with NumPy and handed to VTK in one shot, so this is cheap even when
    thousands of segments are revealed, and it works the same whether the time
    cursor moved forward or was scrubbed backward."""
    m = int(seg_starts.size)
    ca = vtk.vtkCellArray()
    if m:
        conn = np.empty(2 * m, dtype=np.int64)
        conn[0::2] = seg_starts
        conn[1::2] = seg_starts + 1
        offsets = np.arange(0, 2 * m + 1, 2, dtype=np.int64)
        ca.SetData(numpy_support.numpy_to_vtkIdTypeArray(offsets, deep=1),
                   numpy_support.numpy_to_vtkIdTypeArray(conn, deep=1))
    poly.SetLines(ca)
    poly.Modified()


# --------------------------------------------------------------------------- #
# Player
# --------------------------------------------------------------------------- #
def _interp_buf(buf, rt, extrap_cap, max_seg=None):
    """Linear interpolation of a machine-live sample buffer at render-time `rt`.

    `buf` is a list of (timestamp, xyz) in ascending time (the POLLER's sample
    timestamps, NOT arrival times -- arrival is bursty under the Tk pump and would
    corrupt the timeline). Returns the position at `rt`, interpolated between the two
    bracketing samples; before the first sample -> the first. Past the last sample
    (the feed briefly stalled) it extrapolates from the last pair for at most
    `extrap_cap` SECONDS, then HOLDS -- a small cap on purpose: the render delay is
    sized so playback normally sits BETWEEN two real samples, so extrapolation is only
    a stall stopgap, and an unbounded one would fling the toolhead far ahead on a fast
    travel and then snap back (the 'teleport'). Bounded, a stall reads as a tiny pause,
    not a jump. None if empty. Pure + side-effect-free so smoothness is unit-tested.

    `max_seg` (seconds, None = off) refuses to interpolate ACROSS a bracketing pair
    further apart than that, HOLDING at the older sample instead. A gap in the feed is
    missing data, not a straight-line move: sweeping it renders travel that never
    happened, and the sweep is compressed into real time, so it is fast. Measured on
    the real 4 Hz feed with the head moving at 40 mm/s, a 0.5 s gap draws a 7.9 mm
    single-frame jump (472 mm/s on screen) and a 2 s gap draws 67.9 mm -- the
    'first jog after the machine has been idle' yank. `extrap_cap` cannot catch this:
    it only guards the region PAST the newest sample, whereas this is interpolation
    BETWEEN two real ones. In normal operation the caller re-seats before the renderer
    ever reaches such a pair; this is the backstop."""
    if not buf:
        return None
    if len(buf) == 1:
        return buf[-1][1]
    if rt <= buf[0][0]:
        return buf[0][1]
    for i in range(len(buf) - 1):
        t0, p0 = buf[i]
        t1, p1 = buf[i + 1]
        if t0 <= rt <= t1:
            if max_seg is not None and (t1 - t0) > max_seg:
                return p0
            a = 0.0 if t1 <= t0 else (rt - t0) / (t1 - t0)
            return p0 + (p1 - p0) * a
    t0, p0 = buf[-2]
    t1, p1 = buf[-1]
    if t1 > t0 and (max_seg is None or (t1 - t0) <= max_seg):
        v = (p1 - p0) / (t1 - t0)
        return p1 + v * min(max(rt - t1, 0.0), extrap_cap)
    return p1


class _LiveFeed:
    """The TRUST layer over the machine-live position stream (--machine-live).

    Takes `livepos` payloads exactly as the Machine-link page forwards them and
    answers one question: what position should be DRAWN right now? Pure -- no VTK,
    no Tk, injectable clock -- so it unit-tests headlessly
    (`test_machine_link_yank.py`); `Player` only delegates to it.

    It exists because `motion_report.live_position` is NOT a continuous physical
    quantity but a coordinate in a frame Klipper may redefine at any instant: a
    `G28` calls `set_position` before each endstop-seeking move and again at the
    trigger, so the reported position steps to places the machine cannot be while
    nothing teleports. Drawing those steps as travel is the printhead yank.

    Three ways the feed stops being trustworthy, one response:

      * HOLD -- the page says these coordinates are not a position (it sent the G28,
        or the sample left the travel volume). Stop moving; do not guess.
      * DROPOUT -- samples more than `MAX_SEG` apart. The gap is missing data, not a
        straight-line move; sweeping it draws travel that never happened.
      * CATCH-UP -- coming back from either, close the gap at a BOUNDED SPEED
        (`CATCHUP_SPEED`). Freezing and then snapping is two yanks, not zero.

    Catch-up is rate-limited, not timed: a timed blend renders `distance /
    duration` -- the same yank, triggered differently -- and cannot converge.
    """

    RENDER_DELAY = 0.32
    EXTRAP_CAP = 0.12
    MAX_SEG = 0.75
    # Ceiling on how fast the VIEW may move while re-seating, mm/s. 1.33 mm per frame
    # at 60 fps: quick enough to close the largest real re-seat (the 175 mm Z leg of a
    # homing sequence) in ~2 s, slow enough to read as catching up, not a teleport.
    # The two rejected designs, both measured: freeze-then-snap teleported 108.7 mm
    # in one frame, and a timed 0.35 s blend over a 160 mm re-seat drew 11.8 mm.
    CATCHUP_SPEED = 80.0
    CATCHUP_EPS = 0.05               # below this, lock back onto the feed exactly
    MAX_SAMPLES = 8

    def __init__(self, clock=time.time):
        self.clock = clock
        self.buf = []                    # [(sample_time, xyz)], ascending
        self.head = np.zeros(3)          # last position actually rendered
        self.hold = False
        self.catching_up = False
        self._last_t = None              # previous sample() time, for the rate limit
        # Accuracy audit of RENDER_DELAY. Every frame drawn from a bracketing pair of
        # REAL samples is interpolated (measured); a frame drawn past the newest sample
        # is EXTRAPOLATED (guessed forward along the last velocity). The delay is
        # budgeted so the second never happens, and these counters are how a rig
        # session CHECKS that instead of trusting the budget -- the player publishes
        # them in its embed state, so a live session can read `extrap` and see 0.
        self.n_drawn = 0
        self.n_extrap = 0

    def push(self, msg):
        """Fold one livepos payload in. Bad/partial payloads are ignored."""
        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0
        if "x" not in msg or "y" not in msg or "z" not in msg:
            return
        p = np.array([_f(msg.get("x")), _f(msg.get("y")), _f(msg.get("z"))], float)
        ts = msg.get("ts")
        stamp = float(ts) if ts is not None else self.clock()
        if msg.get("hold"):
            self.hold = True             # drop it: this is not a position
            return
        if self.hold:                    # release -- everything buffered is stale
            self.hold = False
            self._restart(stamp, p)
            return
        if self.buf and stamp - self.buf[-1][0] > self.MAX_SEG:
            self._restart(stamp, p)      # dropout
            return
        # Timestamp by the POLLER's sample time (carried in "ts", a wall-clock
        # time.time() from the same machine) -- NOT arrival time. The Tk pump batches
        # the ~4 Hz feed, so arrival is bursty and using it corrupts the interpolation
        # timeline (the residual stutter); sample time is (near) evenly spaced
        # regardless of delivery. A stale/duplicate ts (feed paused, same wall time)
        # replaces rather than appends, so the timeline stays strictly increasing.
        if self.buf and stamp <= self.buf[-1][0]:
            self.buf[-1] = (stamp, p)
        else:
            self.buf.append((stamp, p))
        if len(self.buf) > self.MAX_SAMPLES:
            self.buf.pop(0)

    def _restart(self, stamp, p):
        """Throw the stale buffer away, seat on the new sample, and start catching
        up to it at a bounded speed."""
        self.buf = [(stamp, p)]
        target = self._raw()
        self.catching_up = (
            target is not None
            and float(np.linalg.norm(target - np.asarray(self.head, float)))
            > self.CATCHUP_EPS)

    def _raw(self):
        """The interpolated feed position for now, before hold/re-seat are applied."""
        if not self.buf:
            return None
        rt = self.clock() - self.RENDER_DELAY
        self.n_drawn += 1
        if rt > self.buf[-1][0]:         # past the newest real sample -> guessing
            self.n_extrap += 1
        return _interp_buf(self.buf, rt, self.EXTRAP_CAP, self.MAX_SEG)

    def sample(self):
        """The position to RENDER now (None until the first sample arrives).

        Updates `head` itself: catch-up is a rate limiter, so it needs the previously
        rendered position and the frame interval. Callers may still assign the result
        to `head` (the player does) -- that is a no-op."""
        if not self.buf:
            return None
        now = self.clock()
        dt = 0.0 if self._last_t is None else max(0.0, now - self._last_t)
        self._last_t = now
        if self.hold:
            return np.array(self.head, float)
        live = self._raw()
        if live is None:
            return None
        if self.catching_up:
            delta = live - np.asarray(self.head, float)
            dist = float(np.linalg.norm(delta))
            step = self.CATCHUP_SPEED * dt
            if dist > self.CATCHUP_EPS and dist > step:
                self.head = np.asarray(self.head, float) + delta * (step / dist)
                return np.array(self.head, float)
            self.catching_up = False     # arrived: back on the feed exactly
        self.head = np.array(live, float)
        return np.array(self.head, float)


class Player:
    # Machine-live render delay, s: play the streamed toolhead back this far BEHIND
    # real time and interpolate between the two bracketing samples. It MUST exceed
    # one sample spacing + delivery latency, so the render sits between two REAL
    # samples and never past the newest -- extrapolating is GUESSED motion, so this
    # protects the drawn position's ACCURACY, not just its smoothness.
    # Budgeted from rig measurements, not estimated:
    #   sample spacing 249.8 ms mean, p95 251.6, n=101 -- Klipper's own status
    #   cadence, a blocking wait that ends on that tick, so each response carries a
    #   FRESH sample; Tk pump <=16 ms; child tick <=16 ms; IPC ~1 ms
    # => floor 283 ms; 320 ms keeps ~37 ms of margin for Windows scheduling jitter.
    # A faster transport does not help (a WebSocket subscribe measured a 251.2 ms
    # median push against the same rig); only a faster FEED goes below this.
    _LIVE_RENDER_DELAY = 0.32
    # On a brief feed stall the interpolator may run past the newest sample; it then
    # extrapolates for at most this long before HOLDING. Kept small so a stall is a
    # tiny pause, never a fling-and-snap across the plate on a fast travel move.
    _LIVE_EXTRAP_CAP = 0.12
    # Longest sample-to-sample gap still treated as a move to interpolate across.
    # 3x the measured 0.25 s spacing: jitter stays smooth, a real dropout does not
    # get drawn as a straight-line sweep (see _interp_buf). A gap longer than this
    # RE-SEATS the feed instead.
    _LIVE_MAX_SEG = 0.75
    # A re-seat means "the position you were drawing is no longer trustworthy":
    # discard the buffer and close the gap to the live feed at a BOUNDED SPEED
    # (mm/s), so the worst rendered frame is the same whether the feed ran away by
    # 1 mm or 175 mm. Freezing and then snapping is two yanks, not zero.
    _LIVE_CATCHUP_SPEED = 80.0
    # Below this the view locks back onto the feed exactly (nothing left to close).
    _LIVE_CATCHUP_EPS = 0.05

    def __init__(self, gcode_path, stl_path=None, fiber_opts=None,
                 standoff=4.5, lag=0.3, head_stl=None, head_scale=1.0,
                 physics=True, pressure=2.0e4, voltage=4.0, temperature=90.0,
                 live_adjust=False, coil=False,
                 coil_wave_amp=0.0, coil_wave_len=0.3, cone=True,
                 max_accel=2000.0, square_corner_velocity=6.0,
                 rotary_accel=0.0,
                 physics_deposit=False, dynamic_diameter=False,
                 fill_per_tau=6.0, corner_chord=None,
                 physical_sag=False, physical_sag_viscous=False,
                 physical_fusion=False, calibration=None,
                 machine=None, origin=None,
                 hide_printhead=False, static=False, embed=None, depo=None,
                 plate=None, goto_line=None, machine_live=False,
                 reveal_scaffold=False, reveal_gcode=False,
                 travel_gcode_line=False,
                 mandrel_diameter=None, mandrel_length=None,
                 mandrel_start=None, mandrel_surface_z=0.0,
                 collector=None):
        # Machine-live mode (--machine-live): drive the machine bodies from a real
        # printer's STREAMED toolhead position (Moonraker, over the embed channel as
        # {"t":"livepos",...}). With NO g-code file that is the whole scene -- bodies
        # + plate, no toolpath / deposition / jet. Given the g-code the printer is
        # ACTUALLY running, the same mode also lays the scaffold under the live
        # nozzle: an ordinary player scene with exactly two things swapped out --
        # the bodies are placed from the live feed, not the toolpath, and the
        # playhead comes from the printer's file cursor, not a clock. Everything
        # else is the locked sim's code, unmodified. Do not build a parallel reveal
        # engine on the bodies-only scene; `_advance_live_cursor` says why the file
        # cursor is the one signal that works.

        # --- the collector ------------------------------------------------- #
        # A rotating MANDREL, or None (the default) for the flat collector. This is
        # the only switch: every mandrel branch below tests `self._frame is None`.
        # The diameter has to be TOLD, not detected -- the rotary word is in the file
        # but the mandrel it turns is not, and a 4-axis file read without it parses
        # as a flat zigzag with no error, `A` not being in the axis whitelist
        # (`parse_path`). The GUI passes it from the scaffold parameters.
        self._frame = None
        self._rotary = None            # (mach, rot) from parse_path, or None
        # The five-axis read-back, built once the machine profile is loaded (it
        # needs the joints). None on every three- and four-axis scene, and every
        # `five` branch below tests exactly this.
        self._five = None
        self._five_rot = ()            # the joints, kept past a dropped machine view
        self._mandrel_len = None
        self._mandrel_u0 = None        # rod near end (g-code X), from the designer
        self._rot_deg = 0.0            # the mandrel's reading at the playhead
        self.mandrel_actor = None
        self.holder_actor = None       # the mandrel HOLDER STL, when a profile names one
        # ONE actor per rotary joint, collector-first, `None` where a joint
        # names no body. A mandrel has at most one and it is the holder, so
        # `holder_actor` is this tuple's last entry and nothing four-axis
        # changed; a five-axis table has two REAL bodies -- the spinning table
        # and the cradle under it -- and each turns about its own picked line.
        self.rotary_actors = ()
        if mandrel_diameter and float(mandrel_diameter) > 0.0:
            import mew_frame
            # `surface_z` is the RADIAL half of a 4-axis zero: which g-code Z
            # means the bare rod. Zeroed on the rod's surface (the default) it is 0;
            # zeroed on its AXIS the file
            # carries the radius, and read with 0 the tube would animate one
            # radius clear of the rod it is wound on. The converter is handed
            # the same number by `scaffold_gcode`, so the .depo and the
            # animation cannot disagree.
            self._frame = mew_frame.CylinderFrame(
                0.5 * float(mandrel_diameter),
                surface_z=float(mandrel_surface_z or 0.0))
            self._mandrel_len = (float(mandrel_length)
                                 if mandrel_length else None)
            # Where the ROD begins, from the designer's collector interval. The
            # printed extent cannot supply it once the print is PLACED on the rod
            # bare rod BEYOND the print, so a rod centred on the print would show
            # none of it. None centres the rod on the printed extent.
            self._mandrel_u0 = (float(mandrel_start)
                                if mandrel_start is not None else None)
        if collector is not None:
            # A CURVED COLLECTOR, already built by `scaffold_gcode.surface_frame`
            # -- THE ONE BUILDER, the same call the designer's preview and the
            # generator make. Taking a built frame rather than shape words is
            # what stops this module owning a second definition of a dome.
            if self._frame is not None:
                raise SystemExit(
                    "gcode_player: --mandrel and --surface are two different "
                    "collectors; pass one.")
            self._frame = collector
        self.machine_live = bool(machine_live)
        self._live_src = bool(machine_live and gcode_path
                              and os.path.isfile(gcode_path))
        # Draw the commanded travel moves as G-CODE LINES on top of the scene.
        # Off by default: travel fibre is the default rendering now, and the
        # fibre is what is continuous with the print. This is the opt-in
        # overlay for seeing where the travels actually are.
        self.travel_gcode_line = bool(travel_gcode_line)
        self._reveal_scaffold = bool(reveal_scaffold) and self._live_src
        self._reveal_gcode = bool(reveal_gcode) and self._live_src
        # Work-origin offset H (see moonraker_link.work_offset), streamed with the
        # position. The reveal geometry is in FILE coordinates; the nozzle is drawn at
        # raw machine coordinates; H is what carries one into the other. Zero outside
        # machine-live, so every placement below is byte-identical in the normal sim.
        self._live_H = np.zeros(3)
        self._live_filepos = None      # printer's virtual_sdcard.file_position
        self._live_state = None        # printer's print_stats.state
        # Is the playhead currently being driven by a RUNNING print? False covers
        # idle / complete / cancelled / not connected yet, where the view shows the
        # whole path instead (see _advance_live_cursor). The ratchet below only
        # applies while this is true -- a static "show everything" pass must never be
        # mistaken for "the printer has laid all of this".
        self._live_tracking = False
        # High-water marks for what has been DRAWN (see _reveal_high_water): the
        # furthest commanded segment completed, and the furthest deposition-mesh cell
        # revealed. Cleared whenever tracking starts or stops (see _reset_reveal).
        self._live_seg_hi = 0
        self._live_cell_hi = 0
        # Tracker state for the playhead filter (see _track_nozzle): the previous
        # playhead, the wall clock it was taken at, and the previous drawn head XY
        # (its heading). All None = no anchor, so the next frame seeds straight from
        # the projection instead of clocking forward from a stale value.
        self._live_t_prev = None
        self._live_t_wall = None
        self._live_head_xy = None
        self._byte_starts = None       # line_byte_starts of the source file
        # How far the reader may lead, for THIS file (see _derive_live_bracket).
        # Seeded with the floor so the cursor is well defined before the path is
        # parsed; recomputed from the parsed path below.
        self._live_lead_max = float(self._LIVE_LEAD_FLOOR)
        if self.machine_live:
            live_adjust = False        # the printer drives the playhead, not a march
            if not self._live_src:
                physics = False        # bodies-only: no jet / deposition engines
        # streamed toolhead samples (--machine-live): a small (recv_time, xyz) buffer
        # the tick interpolates for smooth playback; _live_head is the last rendered
        # position (exposed to the GUI/tests). Empty until the first livepos arrives.
        self._live = _LiveFeed()
        self._live.RENDER_DELAY = self._LIVE_RENDER_DELAY
        self._live.EXTRAP_CAP = self._LIVE_EXTRAP_CAP
        self._live.MAX_SEG = self._LIVE_MAX_SEG
        self._live.CATCHUP_SPEED = self._LIVE_CATCHUP_SPEED
        self._live.CATCHUP_EPS = self._LIVE_CATCHUP_EPS
        self.title = (os.path.basename(gcode_path) if gcode_path
                      else "Printer (live)")
        self.gcode_path = gcode_path
        # "Find in simulation" target: open paused at the frame that executes this
        # 1-based g-code line, with the head/jet/deposition revealed up to it (a
        # mid-print freeze). None -> the normal launch. Applied in run() after the
        # scene + deposition mesh exist. See _goto_frame / line_to_time.
        self.goto_line = None if goto_line is None else int(goto_line)
        # Explicit build-plate sibling path (--plate-stl, from the GUI's
        # last_plate). Same resolution problem as --depo below: the implicit search
        # (next to the STL / the g-code) misses for a g-code IMPORT at a preview
        # quality (no STL, external g-code), so a plate the converter DID write
        # (next to -o in the session folder) never surfaced -- no show/hide-plate
        # toggle. The GUI knows where it landed, so it passes it here. None -> the
        # implicit search, byte-identical.
        self._plate_arg = plate
        # Explicit .depo sidecar path (--depo, from the GUI's last_depo). The
        # implicit search (_find_depo_cache) only looks next to the STL and next to
        # the G-CODE -- which both miss for a g-code IMPORT generated at a preview
        # quality: the import lives outside the session folder and no main STL was
        # built, so the converter's cache (written next to -o in the session
        # folder) was never found and the locked sim silently fell back to the
        # COMMANDED (square) path while the live sim's in-process re-lay rounded.
        # The GUI knows exactly where the cache landed, so it passes it here;
        # None (a hand-launched player) keeps the old search, byte-identical.
        self.depo_path = depo
        # Embedded-viewer mode (viewer_embed.EmbedHost, from --embed): render
        # offscreen inside the GUI's Locked/Live-simulation page -- frames
        # stream over shared memory, the sidebar chrome exports to Tk, and the
        # GUI's forwarded input drives a generic interactor. None -> the
        # classic windowed player, byte-identical behaviour.
        self.embed = embed
        # Real-printhead + jet physics knobs. standoff (mm) is the fixed needle
        # height above the CURRENT commanded layer (the g-code Z encodes only
        # layer height, not the 4-5mm gap), so the needle tip rides at
        # commanded_Z + standoff. It doubles as the physics working distance
        # (tip-to-collector gap) the jet spans.
        self.standoff = float(standoff)
        # lag (s) is the jet's flight time: the fiber contact point trails the
        # head by this much print-time, which gives the jet its deflection angle
        # (lag_distance = feed * lag). With physics on it is recomputed from the
        # model each time a parameter changes; otherwise it stays this fixed knob.
        self.lag = float(lag)
        # physics jet: the diameter and lag come from mew_physics. The four knobs
        # are live-editable in the player; pressure/voltage/temperature via _bump,
        # the print speed via _bump_print_speed. pressure in Pa, voltage in kV,
        # temperature in degC. Speed STARTS from the g-code deposition feed.
        self.physics_on = bool(physics) and _HAVE_PHYSICS
        self.pressure = float(pressure)
        self.voltage = float(voltage)
        self.temperature = float(temperature)
        # Real-time parameter adjustment mode (the "Open simulation window
        # (real-time adjustment)" button). When OFF (the default / the plain
        # "Open simulation window") the live knob keybinds are inert and the
        # deposited fibre is exactly the exported scaffold -- so a stray keypress
        # can't quietly mutate the run. When ON, the v/f/h/[ ] keys edit the
        # knobs AND the deposited fibre is re-laid (diameter + whip) to match,
        # forward-accumulating from a per-time schedule (see mew_live / _relay).
        # Requires physics; degrades to the locked sim without it.
        self.live_adjust = bool(live_adjust) and self.physics_on
        # Opt-in below-CTS coiling (the fluid-mechanical sewing machine). Forwarded
        # to the live re-lay (mew_live.relay_fibers) so the real-time-adjustment
        # window reshapes a below-CTS fibre into the coil instead of leaving it
        # straight -- the same effect the locked sim shows via the .depo cache when
        # the export was run with --coil. Off -> below-CTS fibre is left as commanded.
        self.coil = bool(coil)
        # Below-CTS coiling-WAVE indicator (mew_coil_wave): a simple fixed lateral
        # sine marking that CTS was reached, distinct from the physical --coil model
        # above. Forwarded to the live re-lay (IncrementalDeposition -> mew_live /
        # laid_fibers) so the real-time window waves a below-CTS fibre the same way
        # the locked sim shows it via the .depo cache (export run with --coil-wave-*).
        # amp <= 0 -> off. Set from --coil-wave-amp / --coil-wave-len.
        self.coil_wave_amp = float(coil_wave_amp)
        self.coil_wave_len = float(coil_wave_len)
        # Draw the molten Taylor CONE at the nozzle (the cone-jet radius profile: fat
        # at the bore, necking to the fibre). On by default; off -> the legacy
        # constant-radius thin jet at the fibre diameter (clearer for reading the
        # fibre thickness, and the pre-cone look). Purely a jet-render choice -- it
        # never touches the deposited mesh or the export. Set from --no-cone.
        self.cone_on = bool(cone)
        # Whether the EXPORT that wrote the .depo sidecar used the physics fibre
        # transform (the GUI's "Physics-deposited scaffold" toggle). The player can't
        # itself reshape geometry, but it passes this as load_depo_cache's `transformed`
        # so a cache built in the OTHER mode (physics toggled since, same sag params) is
        # rejected instead of shown -- the deposition view tracks the toggle the way it
        # already tracks diameter/sag. Set from --physics-deposit (off by default).
        self.physics_deposit = bool(physics_deposit)
        # The deposition refine grid the LIVE re-lay must use. The locked `.depo`
        # was baked by mew_export at this same value (the GUI passes one field to
        # both), so an edited layer is sampled like the untouched ones beside it.
        self.fill_per_tau = float(fill_per_tau)
        # (tol_mm, radius_mm, steps_per_tau) or None -- the Curve-quality tier the
        # live march draws a TUBE's corners at, matching the exported STL. The
        # march ignores it without a rotary frame (see `mew_incremental`).
        self.corner_chord = corner_chord
        # Use the grounded physical sag law (--physical-sag, a sub-option of
        # physics-deposit). Passed to load_depo_cache as `phys_sag` so the locked sim
        # accepts the physical .depo (and rejects a geometric one); when on it is also
        # installed into gcode_to_fibers' sag-law seam below, so the live re-lay
        # (IncrementalDeposition) re-solves with the SAME law the export used.
        self.physical_sag = bool(physical_sag)
        # Sub-option of --physical-sag: select the grounded VISCOUS-CREEP sag law
        # (temperature predicted via eta(T)) over the static tensioned-thread parabola.
        self.physical_sag_viscous = bool(physical_sag_viscous)
        # Path to the rig calibration profile JSON (mew_calibration): the Material /
        # Nozzle / Calibration anchors + hooks the jet model is built from. None ->
        # the factory defaults (byte-identical to the pre-profile build). Set from
        # --calibration; the gap stays the separate --standoff working distance.
        self.calibration_path = calibration
        # Junction fusion (--physical-fusion, a sub-option of physics-deposit like
        # physical-sag). Passed to load_depo_cache as `phys_fuse` so the locked sim
        # accepts the fused .depo (and rejects an unfused one); when on it is also
        # installed into gcode_to_fibers' fusion seam below, so the live re-lay
        # (IncrementalDeposition) sinks crossovers with the SAME law the export used.
        self.physical_fusion = bool(physical_fusion)
        # Whether the live re-lay should recompute each fibre's DIAMETER from the
        # physics (mass conservation + the live pressure/temperature knobs) or keep
        # the commanded diameter. Set from --dynamic-diameter, which the GUI forwards
        # only when the diameter is physics-driven ("Physics-deposited scaffold" on
        # and "Override fibre diameter" off) -- the same condition that gives the
        # export its --dynamic-diameter. Off -> the live fibre is drawn at the hand-
        # typed diameter, so a manual override shows the value the user set (the
        # centreline still re-lays for jet-lag/coiling). Only consulted in live-adjust.
        self.dynamic_diameter = bool(dynamic_diameter)
        # Machine motion limits for the accel-limited re-timing (mew_kinematics):
        # max_accel (mm/s^2) and the Klipper square-corner-velocity (mm/s, the speed
        # held through a 90deg corner). Properties of the printer, NOT the jet -- set
        # from the GUI's Printhead-sim fields. max_accel <= 0 disables the re-timing
        # (the naive length/feed timeline, byte-identical to the pre-kinematics build).
        self.max_accel = float(max_accel)
        self.scv = float(square_corner_velocity)
        # the mandrel's separate rotary limit, deg/s^2; 0 = none, and max_accel
        # then caps the whole move along the surface (see `_retime_kinematic`)
        self.rotary_accel = float(rotary_accel or 0.0)
        # the as-launched knobs, kept so Reset (live mode) can restore them
        self._launch_pressure = self.pressure
        self._launch_voltage = self.voltage
        self._launch_temperature = self.temperature
        # live PRINT-speed scale: multiplies the g-code's commanded feed before it
        # enters the jet model, so the physics (diameter, lag/deflection, CTS
        # ratio, regime) respond to a what-if speed. In the locked sim it stays 1.0
        # and never touches the g-code or the deposited-mesh geometry; in real-time
        # mode it ALSO (a) retimes the playback clock so the nozzle visibly speeds
        # up / slows to the what-if speed (see _on_tick) and (b) feeds the re-lay so
        # the laid fibre tracks it. The g-code's own timeline (the times array) is
        # never rewritten -- only the wall-clock RATE self.t advances at. Adjusted
        # live with [ and ] (see _bump_print_speed).
        self.print_speed_scale = 1.0
        # real-time INCREMENTAL deposition state (populated by
        # _init_live_relay when live_adjust). The march -- relay + sag + tube of each
        # layer as the clock reaches it, frozen once laid -- lives in
        # mew_incremental.IncrementalDeposition; a live edit only appends a schedule
        # breakpoint (O(1)), so there is no per-edit batch re-solve. The player keeps
        # the commanded fibres only as re-parse context / a degrade check.
        self._cmd_paths = self._cmd_ptimes = None
        self._orch = None               # mew_incremental.IncrementalDeposition
        self._depo_sig = None           # (n_committed, tip_version) the mesh was built for
        self._depo_tube_cache = {}      # cmd-index -> (sagged geom, diam, tube block)
        self._relay_dirty = False       # a live edit is pending a (debounced) refresh
        self._relay_req_wall = 0.0
        self.jet_state = None
        self.jet_field = None               # gap E-field (mew_field); set on solve
        self.model = None
        self.head_scale = float(head_scale)
        # default printhead asset is a bundled read-only resource (resource_path:
        # the repo from source, under sys._MEIPASS when frozen); a missing file
        # draws no tool (just the jet), so this never hard-fails.
        self.head_stl = head_stl or resource_path(
            "machine_models", "MEWron_0.2", "Printhead STL.stl")
        # hide the STANDALONE printhead (the non-machine-model view): draw no tool,
        # only the jet and the fibre. Set by the GUI when the printer
        # model is toggled off or the printhead is ticked Hidden, so those config
        # toggles govern the standalone printhead too (with a full machine model loaded
        # the head is hidden via the profile's hidden_parts instead).
        self._hide_printhead = bool(hide_printhead)
        # Unified-viewer default: open on the finished scaffold, paused, with
        # the machine / printhead / jet / travels hidden -- visually the plain STL
        # view. Un-pausing turns it into the live sim (the machine reveals
        # itself). Applied in run(); harmless in a plain windowed launch.
        self._static = bool(static)
        self._has_printhead = False
        self._head_tip = np.zeros(3)        # needle-tip offset in the STL's frame
        # Print-time before which no jet is drawn at all -- the lead-in that flies
        # in from beside the collector. Set in _setup; see _lead_in_jet_gate.
        self._jet_on = float("-inf")
        self._jet_t = None                  # (M,) deposited-point times, ascending
        self._jet_xyz = None                # (M,3) matching centreline points
        self._jet_diam = None               # (M,) fiber diameter at each point
        # Whether the loaded .depo fibre is physics-reshaped (jet-lag baked into the
        # geometry) vs the plain commanded path. Set by _set_jet_source from the
        # `reshaped=` its caller states as fact -- never measured off the geometry
        # (see there); decides whether the reveal cut already includes the lag
        # (see _reveal_time).
        self._reshaped = False
        # fiber params (diameter/faces/sag) used to rebuild the sagged mesh for
        # the deposition view, so it matches the scaffold the converter wrote;
        # empty -> gcode_to_fibers defaults (e.g. player launched by hand)
        self.fiber_opts = dict(fiber_opts or {})
        # The exported scaffold STL is not DRAWN -- it has no overlay or switch
        # (see _build_buttons) -- but the path is still how the .depo
        # cache and the grey build-plate sibling <stem>_plate.stl are found. The
        # plate IS drawn, lazily on the first Collector toggle, because those
        # meshes are often 100MB+ and must not be read at startup.
        self.stl_path = stl_path if stl_path and os.path.isfile(stl_path) else None
        self.plate_path = self._resolve_plate_path(plate, stl_path, gcode_path or "")
        self.plate_actor = None
        # lighting state, declared here so any path that builds the sidebar
        # before/without _build_scene still finds it (the kit itself needs a
        # renderer, so it is created there)
        self._light_kit = None
        self._studio_light = True
        # g-code origin re-base offset baked into the exported g-code/plate by the
        # converter; the machine-model view UNDOES it so the rendered printer is
        # invariant to the origin choice (only the (0,0,0) triad moves). See
        # _place_machine. None -> no re-base.
        self.origin = (np.zeros(3) if origin is None
                       else np.asarray(origin, float))
        # in-plane build registration (dx, dy) seating the collector on the bed STL;
        # computed in _build_machine from the bed top-face corner + slide_offset, fed
        # to machine_model.place_scene. Zero until then (and the no-bed fallback makes
        # it the slide_offset directly -- a world-frame slide position).
        self._registration = np.zeros(2)
        # PRINT SURFACE datums for the machine-live reveal (see _reveal_surface_z):
        # the bed top face in the bed STL's frame, and the collector plate's top in
        # its own frame, both filled in when the part loads.
        self._bed_top_stl = None
        self._plate_top_local = None
        # Optional multi-body printer model (machine_model.MachineProfile). When
        # active the printhead/jet view is replaced by the real machine split into
        # printhead (X) / gantry (Y) / bed (Z), animated in Frame A; the jet still
        # runs, from the machine nozzle tip to the bed-borne contact. None -> the
        # single printhead+jet view, byte-for-byte unchanged.
        self.machine = None
        if machine is not None and machine_model is not None:
            try:
                prof = (machine if isinstance(machine, machine_model.MachineProfile)
                        else machine_model.MachineProfile.load(machine))
                # `is_active` asks whether the profile names GEOMETRY to draw. A
                # five-axis pair is kept even when it names none, because its
                # joints are KINEMATICS: they carry the rotary words the file is
                # written in and the chain the deposition point is read back
                # through, and without them a curved-collector file cannot be
                # parsed at all. Dropping such a profile is how a rig described
                # for playback but not for rendering became "no --machine given".
                if prof.is_active() or prof.rotary_count >= 2:
                    self.machine = prof
            except Exception as exc:  # noqa: BLE001 -- never block playback
                sys.stderr.write(
                    f"gcode_player: machine profile load failed: {exc}\n")
        # The five-axis read-back needs the JOINTS, so it is built here and not
        # beside the frame: it is what turns a machine XYZ plus two rotary words
        # back into the collector's own coordinates, and it refuses a profile
        # that does not carry exactly two joints. Without a machine there is no
        # five-axis scene at all -- the words themselves live on the joints.
        # Decided by CAPABILITY, not shape: a frame whose read-back is world
        # (`chart_currency` False) is a five-axis collector, library or
        # imported alike. The mandrel is the one frame holding chart columns.
        if (self._frame is not None
                and not getattr(self._frame, "chart_currency", False)):
            if self.machine is None:
                raise SystemExit(
                    "gcode_player: a curved collector needs --machine -- the "
                    "rotary WORDS are a property of the rig, so there is no "
                    "default pair of letters to fall back on.")
            import gcode_to_fibers as _g2f
            self._five = _g2f.FiveAxisReader(self.machine, frame=self._frame)
            # ...and hold the joints separately. `_build_machine` sets
            # `self.machine = None` and falls back to the nozzle marker when the
            # printhead STL is missing, which must not stop the collector
            # TURNING: the joints are the kinematic chain, and the scene is still
            # a five-axis scene with no printer drawn in it. Reading them off
            # `self.machine` there left the pair frozen at its zero pose and the
            # fibre standing still while the file swung both rotaries.
            self._five_rot = tuple(self.machine.rotaries)
        self.gantry_actor = None
        self.bed_actor = None
        self.static_actor = None
        self._machine_on = True         # machine model currently shown (toggle)
        self._mach_tip = None           # nozzle tip offset in the head STL's frame
        # PHYSICAL props resting on the bed in Frame A: the origin triad and the
        # lazily-loaded scaffold / collector-plate STL overlays. They are objects
        # sitting on the bed, so they take the plain bed delta (bed_group) and NEVER
        # the machine-live work offset H -- shifting the collector plate by H is what
        # made it fly around and phase through the bed on the rig.
        self._bed_actors = []
        # REVEAL geometry in the FILE frame: the commanded toolpath lines (gold
        # deposit / grey travel / active tip). The deposition mesh chunks
        # (self.depo_actors) are the same class. These ride bed_group + H, which is
        # what puts the exported file's coordinates under the real nozzle. H is zero
        # outside machine-live, so the two lists are placed identically there.
        self._reveal_actors = []
        # world-shift the reveal/depo actors were last placed at (_place_machine's
        # `overlay`), for _frame_camera to shift view_bounds by -- the camera has
        # no actor of its own to read this back from
        self._last_reveal_shift = np.zeros(3)

        # deposition mesh: the sagged scaffold drawn as it is laid down. Built
        # lazily on first toggle (it re-runs sagging, which can take a moment),
        # then revealed each frame up to the playhead -- see _build_deposition /
        # _reveal_deposition.
        self._depo_built = False        # mesh built yet (lazy, on first toggle)
        self._depo_visible = False      # deposition view currently shown
        self.depo_actors = []           # one actor per time-chunk
        self._depo_chunks = []          # per-chunk dicts: lo, hi, poly, count
        self._depo_conn = None          # (K*C,) connectivity, sorted by time
        self._depo_cell_k = 3           # verts per cell: 3 tris (tube/exact), 2 lines
        self._depo_is_lines = False     # wireframe render -> line cells, not polys
        self._depo_sorted_tt = None     # (C,) cell reveal times, ascending
        self._depo_last_n = -1          # cells shown last frame (skip no-ops)
        # Deposition render quality (locked sim): wireframe / tube:N / exact, set
        # before launch from the GUI dropdown (like the STL viewer). The live-adjust
        # real-time march keeps its own incremental tubes and ignores this.
        self._render = self.fiber_opts.get("render", "exact")
        # scaffold-colour toggle: offered when groups carry colours (a plate) or
        # the run names one --fiber-color (a tube or a curved collector, which
        # send no groups); OFF draws the default off-white in both
        _grp = self.fiber_opts.get("scaffold_groups")
        self._depo_can_color = (bool(_grp) and any(
            isinstance(g, dict) and g.get("color") for g in _grp)) or (
            _hex_to_rgb255(self.fiber_opts.get("fiber_color")) is not None)
        self._depo_colored = True       # show per-scaffold colours when present
        self._depo_has_colors = False   # set true once the mesh is built coloured

        if self.machine_live and not self._live_src:
            # No toolpath in machine-live: a degenerate 2-point path at the origin
            # keeps the n_pts>=2 guards happy while the machine bodies + plate carry
            # the scene. The tick places them from the live feed, never this path.
            verts = np.zeros((2, 3))
            times = np.array([0.0, 1.0])
            is_dep = np.array([False])
            seg_feeds = np.array([0.0])
            seg_line = np.zeros(1, dtype=np.int64)
            dep_feed = 60.0
            # no g-code file, so there is no source text and no line<->time map.
            self.gcode_lines = []
        else:
            (verts, times, is_dep, seg_feeds, seg_line, dep_feed,
             self._rotary) = parse_path(gcode_path, self._frame, self._five)
            # Raw source lines (1-based), so the HUD can show the command text of the
            # line the playhead is executing (task: sim line/command readout). Cheap --
            # a scaffold g-code is small. errors="ignore" mirrors parse_path.
            try:
                with open(gcode_path, "r", errors="ignore") as _fh:
                    self.gcode_lines = _fh.read().splitlines()
            except OSError:
                self.gcode_lines = []
            # Byte offsets of every line, so the printer's `file_position` can be read
            # back as a line number in machine-live (see `_advance_live_cursor`). Read
            # as BYTES -- the offsets are Klipper's, and a text read would mangle them
            # on any non-ASCII or CRLF content.
            if self._live_src:
                try:
                    with open(gcode_path, "rb") as _bfh:
                        self._byte_starts = line_byte_starts(_bfh.read())
                except OSError:
                    self._byte_starts = None
            # Re-time the toolpath onto the accel-limited trajectory (Klipper accel +
            # SCV) so the clock, the print-time total and the animated head ramp and
            # corner-slow like the real machine, instead of teleporting at full feed.
            # Also stashes the naive->kinematic time map so the .depo cache (written on
            # the naive clock) can be remapped onto this clock -- see _retime_kinematic
            # / _to_clock. A no-op (naive timeline) when kinematics is off/unavailable.
            # seg_line rides along (reindexed by the resample map) so a source line
            # still maps to a playback time after re-timing.
            verts, times, is_dep, seg_feeds, seg_line = self._retime_kinematic(
                verts, times, is_dep, seg_feeds, seg_line)
            _perf("parse+retime")
        # 1-based source g-code line per segment (parallel to is_dep); the readout
        # reads it at the cursor and "find in simulation" searches it for a line.
        self.seg_line = seg_line
        # Times at which a NEW source g-code line begins, so the arrow keys scrub
        # line by line (not a fixed 2% jump). A new line starts at segment j when
        # its source line differs from the previous segment's; its start time is
        # times[j] (times[:-1] is each segment's start). See _skip.
        if seg_line.size and (not self.machine_live or self._live_src):
            change = np.ones(seg_line.size, dtype=bool)
            change[1:] = seg_line[1:] != seg_line[:-1]
            self._line_step_times = np.ascontiguousarray(
                times[:-1][change], dtype=np.float64)
        else:
            self._line_step_times = np.zeros(0, dtype=np.float64)
        # modal feed (mm/min) per segment, so the jet physics tracks the speed at
        # the playhead -- per-scaffold and per-loop feeds differ, so the deflection
        # must re-solve when the head crosses into a faster (or slower) region.
        # (After re-timing each resampled segment carries its COMMANDED move's feed,
        # so the jet still reads the commanded speed, not the momentary ramp speed.)
        self.seg_feeds = seg_feeds
        self.verts = verts
        self.times = times
        self.is_dep = is_dep
        # (t0, t1) of every travel run, on THIS clock (post-retime), so the
        # deposition mesh can be split into travel fibre and scaffold fibre
        # without the cache having to say which is which. The converter merges
        # a travel into the FRONT of the fibre it leads into and the sag solve
        # then resamples it, so no index survives -- but the print TIME does,
        # and it is the same quantity `_drop_travel_spans` cuts the STL on.
        self._travel_spans = _travel_spans(times, is_dep)
        self.total = float(times[-1]) if len(times) else 0.0
        self.n_pts = len(verts)
        # The machine-live file bracket, sized from THIS file's longest commanded
        # move and its layer duration rather than from a global constant. Computed
        # unconditionally (it is O(n) over the path and machine_live is decided by
        # the caller, not here) so the value is always available to inspect.
        self._live_lead_max = self._derive_live_bracket()
        # the print's representative deposition speed (mm/s), fed to the model as
        # the translation speed; clamped >0 so the model never divides by zero
        self.deposit_speed = max(dep_feed / 60.0, 1e-6)

        # stand up the physics model (working distance == needle standoff) and
        # compute the initial jet state, which sets self.lag when physics is on
        if self.physics_on:
            # Build the jet model from the rig calibration profile (Material /
            # Nozzle / Calibration anchors + hooks), or the factory defaults when
            # no profile is set (byte-identical to the pre-profile build). The gap
            # stays the needle standoff. The viscous flag overlays the dormant
            # viscous-creep sag toggle (sub-option of --physical-sag) so
            # make_sag_law builds the viscous closure.
            self.model = mew_calibration.build_model(
                self.calibration_path, self.standoff,
                viscous=self.physical_sag_viscous)
            # Install the grounded sag law into the shared seam so the live re-lay
            # re-solves exactly like the physical export. Dedicated player process,
            # so no uninstall needed; off (default) -> the geometric sag, untouched.
            if self.physical_sag:
                set_sag_law(self.model.make_sag_law(temperature=self.temperature))
            if self.physical_fusion:
                set_fusion_law(
                    self.model.make_fusion_law(temperature=self.temperature),
                    self.model.fusion_wall_band())
        if not self.machine_live or self._live_src:
            self._recompute_physics()

        # Pre-split segment indices by kind so each frame just slices by the
        # current cursor instead of re-testing every segment.
        seg = np.arange(max(0, self.n_pts - 1))
        self.dep_idx = seg[is_dep] if seg.size else seg
        self.trv_idx = seg[~is_dep] if seg.size else seg
        # File-frame Z of the FIRST DEPOSITED layer -- the height the print rests at.
        # Measured over deposition endpoints only, so a lead-in travel at a safe Z (or
        # a Z-hop) cannot drag it down. It is what the machine-live reveal seats on the
        # collector; see _overlay_delta / _reveal_surface_z.
        if self.dep_idx.size:
            _dep_v = np.zeros(self.n_pts, dtype=bool)
            _dep_v[self.dep_idx] = True
            _dep_v[self.dep_idx + 1] = True
            self._reveal_base_z = float(verts[_dep_v][:, 2].min())
        else:
            self._reveal_base_z = float(verts[:, 2].min()) if self.n_pts else 0.0

        # view_bounds (the print plus the origin) sizes the printhead stand-in.
        # The CAMERA frames the print alone: with the origin folded in, the
        # default 10 mm scaffold 35 mm from it filled a quarter of the view, and
        # what a researcher opens Simulation for is how the fiber lands. The
        # origin marker and the lead-in stay in the scene, a zoom-out away.
        self.view_bounds = self._framing_bounds()
        self.frame_bounds = self._framing_bounds(with_origin=False)

        self.t = 0.0
        self.speed = 1.0                   # default 1x (real-time); - / + adjust
        self.playing = True
        self._last_wall = None
        self._sync_slider = False          # guard against slider<->clock feedback

        if not self.machine_live or self._live_src:
            self._build_jet_source()
            if self.live_adjust:
                self._init_live_relay()
        # after the anchor, because the gate IS the anchor's own opening
        self._jet_on = self._lead_in_jet_gate(verts, is_dep)
        self._build_scene()
        _perf("build_scene")

    def _framing_bounds(self, with_origin=True):
        """(xmin,xmax,ymin,ymax,zmin,zmax) of the deposited geometry, plus the
        origin (0,0,0) when with_origin. Falls back to all vertices, then to a
        unit box."""
        if self.dep_idx.size:
            mask = np.zeros(self.n_pts, dtype=bool)
            mask[self.dep_idx] = True
            mask[self.dep_idx + 1] = True
            pts = self.verts[mask]
        elif self.n_pts:
            pts = self.verts
        else:
            return (-1.0, 1.0, -1.0, 1.0, -1.0, 1.0)
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        if with_origin:
            lo = np.minimum(lo, 0.0)             # fold in the origin
            hi = np.maximum(hi, 0.0)
        # pad any zero-thickness axis (e.g. a single-layer scaffold in Z) so the
        # camera has a non-degenerate box to fit
        span = hi - lo
        pad = np.where(span > 1e-9, 0.0, 1.0)
        lo = lo - pad
        hi = hi + pad
        return (lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])

    def _frame_camera(self):
        """Fit the camera on the printed scaffold at startup, CENTRED -- identical
        whether or not a machine model is loaded and regardless of which parts are
        hidden, so opening the sim always lands on the print (the machine body, when
        shown, is reachable by zooming out).

        With a machine model the scaffold rides the collector's Frame-A offset (the
        bed-group shift _place_machine applies to the reveal actors, read back here
        from self._last_reveal_shift -- NOT from an actor, since the reveal actors'
        own transforms are the shift being asked about), so its WORLD position
        differs from its g-code
        view_bounds; shift the bounds by that offset to centre on where the scaffold
        actually renders -- else the camera frames the un-shifted g-code box (or, if
        we unioned the machine bodies, the whole printer) and the scaffold sits off
        to one side. Without a model the offset is zero, so this is byte-for-byte the
        marker-only framing. Framing the scaffold (not the visible props) is also what
        keeps a hidden bed / all-hidden from collapsing the camera onto the origin.

        Framing the small scaffold leaves the camera sitting near the MACHINE's centre,
        which would let the near clipping plane slice the printer in half as you orbit
        (see fit_clipping_range). So after the scaffold framing fixes the zoom (parallel
        scale) and view centre, fit_clipping_range slides the camera back along its view
        axis until the whole machine clears the near plane -- a no-op to the image under
        parallel projection, but it stops the orbit-clipping for good."""
        if self.machine_live:
            # No scaffold to frame -- fit the whole VISIBLE machine (bodies + plate +
            # origin) directly in world coords. ComputeVisiblePropBounds already
            # includes each actor's placed position, so unlike the scaffold path this
            # must NOT add the bed-group offset back on. Drives run()'s framing, the
            # ViewCube snaps and "Snap top-down" alike.
            b = self.renderer.ComputeVisiblePropBounds()
            if b[0] <= b[1]:
                self.renderer.ResetCamera(b)
                fit_clipping_range(self.renderer)
            return
        ox, oy, oz = self._last_reveal_shift
        x0, x1, y0, y1, z0, z1 = getattr(self, "frame_bounds", self.view_bounds)
        if os.environ.get("MEW_DEBUG_CAMERA"):
            sys.stderr.write(
                "DEBUG_CAMERA bed_group offset=(%r,%r,%r) view_bounds=%r "
                "shifted=(%r,%r,%r,%r,%r,%r)\n"
                % (ox, oy, oz, self.view_bounds,
                   x0 + ox, x1 + ox, y0 + oy, y1 + oy, z0 + oz, z1 + oz))
        self.renderer.ResetCamera(x0 + ox, x1 + ox, y0 + oy, y1 + oy,
                                  z0 + oz, z1 + oz)
        fit_clipping_range(self.renderer)
        if os.environ.get("MEW_DEBUG_CAMERA"):
            cam = self.renderer.GetActiveCamera()
            sys.stderr.write(
                "DEBUG_CAMERA pos=%r focal=%r parallel_scale=%r "
                "clip=%r\n"
                % (cam.GetPosition(), cam.GetFocalPoint(),
                   cam.GetParallelScale(), cam.GetClippingRange()))

    def _snap_top(self):
        """Snap the camera to a level, straight-down (top-down) view of the print
        -- the micrograph viewer's "Snap top-down" ported into the unified sim
        viewer. Only the projection DIRECTION and view-up need setting here: point
        the camera down -Z with +Y up, then let _frame_camera's ResetCamera keep
        that orientation while it re-fits the zoom + clipping on the scaffold
        footprint, so the print lands centred and filling the view (the same
        framing the sim opens with, just looking straight down)."""
        cam = self.renderer.GetActiveCamera()
        fx, fy, fz = cam.GetFocalPoint()
        cam.SetPosition(fx, fy, fz + 1.0)   # above the focal point: view dir -Z
        cam.SetViewUp(0.0, 1.0, 0.0)        # +Y up, matching the native viewer
        self._frame_camera()
        self.window.Render()

    def _toggle_view_cube(self):
        """Show/hide the upper-right ViewCube. _after_control (the wrapper every
        sidebar callback runs through) refreshes the label and renders."""
        if self._cube is not None:
            self._cube.toggle()

    def _toggle_origin(self):
        """Show/hide the XYZ origin triad (same wrapper as the cube's)."""
        self.origin_actor.SetVisibility(not self.origin_actor.GetVisibility())

    def _on_origin_msg(self, m):
        """The Settings window's "Show G-code zero", flipped while open."""
        self.origin_actor.SetVisibility(bool(m.get("on")))
        self.window.Render()

    # -- scene -------------------------------------------------------------- #
    def _build_scene(self):
        self.renderer = vtk.vtkRenderer()
        self.renderer.SetBackground(0.11, 0.11, 0.13)
        # Studio lighting, ON by default (see vtk_helpers.studio_light_kit for
        # why the stock single headlight flattens a fibre). The kit is built
        # here but installed by _apply_studio_light, which is also what the
        # sidebar toggle calls -- one path, so the A/B cannot drift.
        self._light_kit = studio_light_kit()
        self._apply_studio_light()

        # one shared point set for both the deposit and travel polylines
        self.points = vtk.vtkPoints()
        if self.n_pts:
            self.points.SetData(numpy_support.numpy_to_vtk(
                np.ascontiguousarray(self.verts), deep=1))

        self.travel_poly, self.travel_actor = _line_actor(
            self.points, TRAVEL_COLOR, 1.5, lighting=False)
        self.deposit_poly, self.deposit_actor = _line_actor(
            self.points, DEPOSIT_COLOR, 2.5, lighting=False)
        self.renderer.AddActor(self.travel_actor)
        self.renderer.AddActor(self.deposit_actor)

        # the segment currently being traversed, drawn partially up to the head
        self.active_pts = vtk.vtkPoints()
        self.active_pts.SetNumberOfPoints(2)
        self.active_pts.SetPoint(0, 0.0, 0.0, 0.0)
        self.active_pts.SetPoint(1, 0.0, 0.0, 0.0)
        active_poly = vtk.vtkPolyData()
        active_poly.SetPoints(self.active_pts)
        ca = vtk.vtkCellArray()
        ca.InsertNextCell(2)
        ca.InsertCellPoint(0)
        ca.InsertCellPoint(1)
        active_poly.SetLines(ca)
        amap = vtk.vtkPolyDataMapper()
        amap.SetInputData(active_poly)
        self.active_actor = vtk.vtkActor()
        self.active_actor.SetMapper(amap)
        self.active_actor.GetProperty().SetLineWidth(2.5)
        self.active_actor.GetProperty().SetLighting(False)
        self.renderer.AddActor(self.active_actor)

        bx0, bx1, by0, by1, bz0, bz1 = self.view_bounds
        diag = float(math.dist((bx0, by0, bz0), (bx1, by1, bz1))) or 1.0

        # printhead: the multi-body machine model when a profile is loaded, else the
        # real printhead STL. There is no truncated-cone "frustum" stand-in:
        # hiding the machine/printhead simply hides it (no cone replacement), and a
        # build with no printhead asset shows just the jet.
        if self.machine is not None and self._build_machine():
            self._apply_machine_visibility()
        else:
            self._build_printhead(diag)

        # the polymer jet: a multi-point centreline swept into a real 3D tube by a
        # vtkTubeFilter, so it reads as a fiber, not a line. The points trace a
        # field-shaped arc (see _jet_arc_points) and are moved per frame (radius
        # reset to the active fiber's) -- re-sweeping a ~15-point polyline is
        # negligible -- so the jet bows and leans to track the deposition while the
        # printhead above it stays fixed.
        # +2, not +1: the drawn arc's TIP is not the top of the tube. One extra
        # lead point sits above the nozzle-exit plane, and everything above that
        # plane is cut away again (see _set_jet_geometry / the exit-plane clip
        # below), so the jet ends on a flat horizontal face at the bore.
        n_jet = JET_ARC_SEGMENTS + 2
        self.jet_pts = vtk.vtkPoints()
        self.jet_pts.SetNumberOfPoints(n_jet)
        for i in range(n_jet):
            self.jet_pts.SetPoint(i, 0.0, 0.0, 0.0)
        jet_poly = vtk.vtkPolyData()
        jet_poly.SetPoints(self.jet_pts)
        jca = vtk.vtkCellArray()
        jca.InsertNextCell(n_jet)
        for i in range(n_jet):
            jca.InsertCellPoint(i)
        jet_poly.SetLines(jca)
        # Per-point radius scalar: the jet is NOT a constant-radius tube -- it is the
        # cone-jet PROFILE r(s)=sqrt(Q/(pi v(s))) (mew_physics), fat at the bore (the
        # Taylor cone) and necking smoothly to the fibre as the field accelerates it.
        # VaryRadiusByAbsoluteScalar makes the tube radius at each point EQUAL its
        # scalar, so _jet_radii just writes the profile in per frame (no separate cone
        # actor -- the cone is the thick top of this one tube; see _place_head_and_jet).
        self.jet_radii = vtk.vtkDoubleArray()
        self.jet_radii.SetName("radii")
        self.jet_radii.SetNumberOfTuples(n_jet)
        for i in range(n_jet):
            self.jet_radii.SetValue(i, 0.01)
        jet_poly.GetPointData().SetScalars(self.jet_radii)
        self.jet_tube = vtk.vtkTubeFilter()
        self.jet_tube.SetInputData(jet_poly)
        self.jet_tube.SetNumberOfSides(JET_SIDES)
        self.jet_tube.CappingOn()
        self.jet_tube.SetRadius(0.01)              # base; overridden by the abs-scalar
        self.jet_tube.SetVaryRadiusToVaryRadiusByAbsoluteScalar()
        # THE NOZZLE-EXIT FACE. vtkTubeFilter caps PERPENDICULAR TO THE FIRST CHORD
        # and the drawn jet leaves the nozzle leaning (`_jet_arc_points` does not pin
        # that end), so the top of the Taylor cone tilted with the lag -- 9.7 deg at
        # a 2 mm lag. The melt leaves a vertical bore: that face is horizontal at
        # every lag.
        # A RENDER fix and nothing else -- the centreline is untouched, so no shape
        # claim moves: the tube is swept a little PAST the tip and cut by the exit
        # plane, and a cylinder cut by a plane its axis is not perpendicular to gives
        # a flat rim and a smooth wall. Do not instead pin the arc's second node
        # vertical or splice a straight lead-in: both turn the centreline through the
        # whole lean in ONE step at the BORE radius, where r*dtheta is ~27 um against
        # a 5 um sample step, so the rings cross and the surface folds.
        self._jet_exit_plane = vtk.vtkPlane()
        self._jet_exit_plane.SetNormal(0.0, 0.0, -1.0)      # keep what is BELOW
        planes = vtk.vtkPlaneCollection()
        planes.AddItem(self._jet_exit_plane)
        self.jet_clip = vtk.vtkClipClosedSurface()
        # fed the tube's triangle STRIPS directly -- it takes them, and a
        # vtkTriangleFilter in front costs another 0.05 ms/frame for nothing
        self.jet_clip.SetInputConnection(self.jet_tube.GetOutputPort())
        self.jet_clip.SetClippingPlanes(planes)
        self.jet_clip.SetScalarModeToNone()
        # the clip does not carry the tube's point normals through, and at
        # JET_SIDES=16 flat shading reads as a faceted cone. Splitting on a
        # feature angle keeps the new rim a crisp edge while the swept wall stays
        # smooth; without it the averaged normals smear a dark streak along the
        # rim. Whole chain measures +0.99 ms/frame over the bare tube, timed in
        # place on a scrubbing Player rather than on a standalone rig. That is a
        # recorded number and NOT a judged one: nothing here holds the jet to a
        # per-frame budget, so it is 3% of a 33 ms frame and unbanded.
        self.jet_normals = vtk.vtkPolyDataNormals()
        self.jet_normals.SetInputConnection(self.jet_clip.GetOutputPort())
        self.jet_normals.SetFeatureAngle(50.0)
        self.jet_normals.SplittingOn()
        self.jet_normals.ConsistencyOff()
        self.jet_normals.ComputeCellNormalsOff()
        jmap = vtk.vtkPolyDataMapper()
        jmap.SetInputConnection(self.jet_normals.GetOutputPort())
        jmap.ScalarVisibilityOff()                 # scalars size the tube, not colour it
        self.jet_actor = vtk.vtkActor()
        self.jet_actor.SetMapper(jmap)
        prop = self.jet_actor.GetProperty()
        prop.SetColor(*JET_COLOR)
        prop.SetSpecular(0.4)
        prop.SetSpecularPower(30)
        self.jet_actor.SetVisibility(False)        # shown once placed (see _apply_time)
        self.renderer.AddActor(self.jet_actor)
        # user toggle (Hide/Show jet); the jet still auto-extinguishes at t >= total
        self._jet_visible = True

        # origin (0,0,0) marker -- a small axis triad sized to the part
        self.origin_actor = origin_marker(0.08 * diag)
        self.renderer.AddActor(self.origin_actor)
        import vtk_helpers                  # the Settings "Show G-code zero"
        self.origin_actor.SetVisibility(vtk_helpers.prefer_origin())

        # the mandrel collector, when there is one. It rides the same seat and
        # spin as the print resting on it, so it is placed in `_place_collector`
        # rather than parked once here.
        self.mandrel_actor = self._mandrel_body()
        if self.mandrel_actor is not None:
            self.renderer.AddActor(self.mandrel_actor)

        # in Frame A the toolpath lines + origin marker rest on the bed, so they ride
        # its Z motion with the deposited print (deposition chunks join as they're
        # built; overlays as they're lazily loaded).
        # A MANDREL needs these lists for the same reason a machine model does:
        # everything on the collector moves together. With no printer drawn the
        # motion is just the seat and the spin, but it is the same two groups and
        # the same one transform -- see `_place_collector`.
        if self.machine is not None or self._frame is not None:
            self._reveal_actors.extend([self.travel_actor, self.deposit_actor,
                                        self.active_actor])
            self._bed_actors.append(self.origin_actor)
        if self._live_src:
            self._lift_reveal_over_surfaces()

        # Modern chrome (themed left sidebar) -- guarded so a hand-launched
        # player without the shell module still runs. `self._themed` gates the
        # new layout; `self._chrome` (built below) owns the sidebar + confined
        # 3D viewport so nothing floats over the scene.
        try:
            import gui_viewer_shell as _shell
            self._shell_mod = _shell
            self._themed = True
        except Exception:  # noqa: BLE001
            self._shell_mod = None
            self._themed = False
        self._chrome = None

        # window + interactor
        self.window = vtk.vtkRenderWindow()
        self.window.AddRenderer(self.renderer)
        # a taller default gives the sidebar room for the readout + buttons +
        # key legend without crowding (all live in the strip now)
        self.window.SetSize(1240, 820)
        self.window.SetWindowName(f"Simulation — {self.title}")
        # embedded mode replays the GUI's forwarded input from an external
        # loop; the host picks the interactor (the platform one on Windows,
        # so native fullscreen can pump real OS input into the same observers)
        if self.embed is not None:
            self.interactor = self.embed.make_interactor()
        else:
            self.interactor = vtk.vtkRenderWindowInteractor()
        self.interactor.SetRenderWindow(self.window)
        self.interactor.SetInteractorStyle(
            vtk.vtkInteractorStyleTrackballCamera())
        # CAD-style orbit/zoom about the point under the cursor; its observers sit
        # at a lower priority than the buttons and the timeline slider, so clicking
        # those is never mistaken for the start of an orbit
        install_cad_camera(self.interactor, self.renderer)

        # Build the sidebar chrome BEFORE the HUD/buttons so they can mount into
        # it (it confines the scene viewport and paints the sidebar panel).
        # Embedded: the SAME chrome calls export the rows to the GUI's control
        # rail (viewer_embed.EmbedChrome mirrors ViewerChrome), and the host
        # takes the render pipeline offscreen before the first Render().
        if self.embed is not None:
            title = os.path.basename(self.title) if isinstance(self.title, str) \
                else ""
            chrome_title = ("Printer (live)" if self.machine_live
                            else "Live simulation" if self.live_adjust
                            else "Simulation")
            self._chrome = self.embed.make_chrome(chrome_title, subtitle=title)
            self._themed = True
            self.embed.attach(self.window, self.interactor, self.renderer)
        elif self._themed:
            title = os.path.basename(self.title) if isinstance(self.title, str) \
                else ""
            self._chrome = self._shell_mod.ViewerChrome(
                self.window, self.interactor, self.renderer,
                title="Simulation", subtitle=title)

        # Show the build plate by DEFAULT when the export wrote one (the grey
        # <stem>_plate.stl sibling) -- it grounds the scaffold instead of leaving
        # it floating in space. Done here, BEFORE _build_buttons, so the plate
        # COLLECTOR toggle is created reading "Hide collector" (its visible state)
        # from the very first hello -- no one-frame "Show collector" flicker. A
        # small flat mesh, so
        # the lazy-load cost is negligible; its visibility is independent of the
        # machine toggle (_apply_machine_visibility leaves _bed_actors alone), so
        # it stays shown in the static view even with the machine hidden. No-op
        # when no plate sibling exists.
        if self.plate_path and not self._overlay_on("plate"):
            self._toggle_overlay("plate")

        # The upper-right ViewCube. Built AFTER the chrome (so the window's
        # layer count is already settled) but BEFORE _build_buttons, so its
        # toggle reads its real visibility in the first hello -- the same
        # backwards-label rule the plate above follows. Scene-side, not chrome-
        # side, which is what makes it survive into the embedded panel where
        # there is no ViewerChrome at all (see view_cube's docstring).
        self._cube = None
        if _view_cube is not None:
            try:
                self._cube = _view_cube.ViewCube(
                    self.window, self.interactor, self.renderer,
                    reframe=self._frame_camera)
            except Exception:  # noqa: BLE001 -- a gizmo must never sink a launch
                self._cube = None

        self._build_hud()
        self._build_buttons()
        self._build_slider()

        self.interactor.AddObserver("KeyPressEvent", self._on_key)
        # Swallow VTK's built-in single-key shortcuts (CharEvent): 'f' fly-to-point
        # (which warns "no current renderer" until the first mouse move), 'w'/'s'
        # wireframe/surface, 'e'/'q' exit, 'r' reset, '3' stereo... The player owns
        # its keys via KeyPressEvent (fired first), so aborting CharEvent at high
        # priority kills those defaults and their surprises -- including that 'f'
        # warning -- without touching our handling. Explicit q/Escape quit is
        # restored in _on_key so closing with the keyboard still works.
        self._char_tag = self.interactor.AddObserver(
            "CharEvent", self._swallow_char, 10.0)
        self.interactor.AddObserver("TimerEvent", self._on_tick)
        # Also advance the clock on camera interaction. VTK's event loop is
        # single-threaded, so while the mouse is dragged the playback TimerEvent
        # is starved and the animation would otherwise stall until the orbit
        # ends; InteractionEvent fires continuously during the drag, so playback
        # keeps moving as you pan/rotate. _on_tick advances by real elapsed
        # wall-time, so being called from both sources never double-counts.
        self.interactor.AddObserver("InteractionEvent", self._on_tick)

    def _build_hud(self):
        if self.machine_live:
            # machine-live wants a CLEAN 3-D view: the Machine-link page already
            # shows state / X / Y / Z / progress in its form beside the panel, so a
            # sidebar readout + button rail here is pure redundant clutter. No HUD, no
            # footers -> the rail is just the
            # wordmark; orientation is the clickable scene-side ViewCube + mouse.
            self.hud = None
            self.help = None
            return
        # The live readout. Themed: a multi-line block near the top of the
        # sidebar (managed by the chrome). Legacy: a two-line overlay pinned to
        # the top-left of the render window.
        self.hud = vtk.vtkTextActor()
        tp = self.hud.GetTextProperty()
        if self._themed and self._chrome is not None:
            # Reserve EXACTLY the number of readout lines the refresh will emit,
            # so no empty rows pad the gap to the buttons below. Machine-live
            # shows state + blank + X/Y/Z/v (6). Otherwise the base is 6 (state,
            # t, speed, line, command, xyz) and the jet model adds a blank + 6.
            if self.machine_live:
                hud_lines = 6
            else:
                hud_lines = 13 if self.physics_on else 6
            self._chrome.set_hud(self.hud, max_lines=hud_lines)
            # the embedded rail draws the readout as a labelled table from these
            # rows (refreshed with the HUD text in _refresh_hud); the fullscreen
            # sidebar keeps the text block
            self._readout_rows, self._readout_text = [], None
            if hasattr(self._chrome, "set_readout"):
                self._chrome.set_readout(
                    lambda: (self._readout_rows, self._readout_text))
            if hasattr(self._chrome, "set_listing"):
                self._chrome.set_listing(self._listing_payload)
        else:
            tp.SetFontSize(15)
            tp.SetColor(0.92, 0.92, 0.96)
            tp.SetBackgroundColor(0.16, 0.18, 0.22)
            tp.SetBackgroundOpacity(0.7)
            self.renderer.AddActor2D(self.hud)

        # Keyboard legend. Only the shortcuts WITHOUT an on-screen button are
        # worth listing (every toggle/playback control is a labelled sidebar
        # button already); showing those keeps the legend short enough to fit
        # the sidebar. Skip arrows + speed always apply; the live knobs only in
        # real-time mode.
        keys = ["Space  play / pause",
                "‹ ›   step line   − +  speed",
                "R      restart"]
        if self.machine_live:
            # no playback clock in machine-live -- the scene rides the printer feed
            keys = ["Live from the printer"]
        if self.physics_on and self.live_adjust:
            keys.append("v/V    voltage")
            keys.append("f/F    flow")
            keys.append("h/H    temperature")
            keys.append("[ ]    print speed")
            keys.append("0      reset knobs")
        self._help_keys = list(keys)
        keys_text = "\n".join(keys)
        if self._themed and self._chrome is not None:
            self._chrome.footer_section("Keys")
            self._chrome.footer_label(keys_text, lines=len(keys))
            self._chrome.footer_section("Mouse")
            self._chrome.footer_label(
                "L-drag  orbit\nShift+L-drag  pan\nR-drag  turn (Z)\nwheel   zoom", lines=4)
            self.help = None                        # chrome owns the legend
        else:
            self.help = vtk.vtkTextActor()
            hp = self.help.GetTextProperty()
            hp.SetFontSize(12)
            hp.SetColor(0.7, 0.7, 0.78)
            # legacy one-liner keeps the full toggle-key hints
            full = ["Space play/pause", "< > step line", "- + speed", "R restart"]
            if self.plate_path:
                full.append("P plate")
            if self.machine is not None:
                full.append("M machine")
            full += ["T travels", "N printhead", "G gcode lines",
                     "D deposition"]
            if self._depo_can_color:
                full.append("C color")
            if self.physics_on and self.live_adjust:
                full += ["v/V volts", "f/F flow", "h/H temp",
                         "[ ] print speed", "0 reset"]
            self.help.SetInput("   ".join(full))
            self.renderer.AddActor2D(self.help)

    def _build_buttons(self):
        if self.machine_live:
            return self._build_buttons_machine_live()
        # Clickable on-screen buttons (the same vtkTextActor + click-hit-test
        # pattern stl_viewer uses for its toggles). Each entry is (actor, label
        # provider, callback); a single observer hit-tests them all. The scaffold
        # and print-slide overlays each get their OWN toggle, and only when that
        # STL actually exists.
        # Sim-tool visibility flags. In the unified viewer's --static default the
        # machine / printhead / jet / travels open HIDDEN (a clean "look at the
        # scaffold" view); the first play/scrub reveals them (see _reveal_sim_tool).
        # Setting the flags HERE (not only in run()) keeps the button LABELS honest
        # in the hello the embedded rail is built from -- otherwise a static launch
        # would show "Hide X" while X is already hidden (the backwards-toggle bug).
        show_tool = not self._static
        # Travels default ON even in the --static view: the
        # inter-part / inter-layer hops read better shown, and _apply_time draws them
        # from this flag on the very first frame. The printhead + jet still open
        # HIDDEN in static and reveal on the first play/scrub (see _reveal_sim_tool).
        self._travels_on = True            # the travel FIBRE (see _toggle_travels)
        # the commanded travel POLYLINE, a separate opt-in overlay since travel
        # fibre became the default rendering (--travel-gcode-line)
        self._travel_lines_on = bool(self.travel_gcode_line)
        # INSTEAD OF, not on top of: the point of the overlay is to see the
        # commanded G0 path, and a tube drawn along the same path hides it.
        # Either can still be flipped back on from the sidebar afterwards.
        if self._travel_lines_on:
            self._travels_on = False
        self._head_visible = show_tool     # printhead actor (the real STL)
        self._jet_visible = show_tool      # molten thread; also auto-off at t>=total
        self._static_revealed = False      # first play/scrub reveals the sim tool
        self._deviation_on = False         # deviation colour scheme on the depo mesh
        self._dev_bar = None               # the deviation colour-key overlay (2D)
        self._dev_sidecar = None           # last sidecar the GUI pushed down
        self._dev_bake_sidecar = None      # sidecar to bake in at the next mesh build
        self._dev_baked_sidecar = None     # sidecar whose deviation the mesh carries
        self._depo_has_dev = False         # the built mesh carries a deviation scalar
        self._dev_computing = False        # awaiting the GUI's analysis (HUD status)
        self._dev_error = ""               # why the last Show deviation failed (HUD)
        self._micro_on = False           # micrograph overlay layer (embed only)
        self._micro_actor = self._micro_state = None
        self._micro_ctrls_added = False    # the Micrograph sidebar section exists
        if self._static:
            self._machine_on = False       # machine model hidden until the first play
        self._listing_on = False           # the g-code listing (Simulation): off
        self._deposit_on = False           # gold g-code lines off by default; the
        self.deposit_actor.SetVisibility(False)   # deposition mesh is the default
        self.buttons = []

        # -- Playback: the transport actions ------------------------------- #
        # A third element is the embedded rail's layout hint (EmbedChrome.button):
        # {"role": ...} places a button on the transport / beside the readout,
        # {"toggle": (name, state_fn)} draws it as a named on/off switch. The
        # label still streams, so the fullscreen sidebar is unchanged.
        playback = [
            (lambda: "Pause" if self.playing else "Play", self._toggle_play,
             {"role": "play"}),
            (lambda: "Slower", self._slower, {"role": "slower"}),
            (lambda: "Faster", self._faster, {"role": "faster"}),
            (lambda: "Restart", self._restart, {"role": "restart"}),
        ]
        # -- Live edit: nudge the physics knobs from the sidebar ----------- #
        # In real-time mode the v/f/h/[ ] keybinds edit the jet; these buttons
        # are the on-screen equivalent, one increase/decrease pair per knob so
        # the sim is fully drivable without the keyboard. Each calls the SAME
        # handler the keybind does with the SAME delta (see _on_key): voltage
        # +/-0.5 kV, pressure +/-1 psi, temperature +/-5 C; print speed
        # is multiplicative (x1.25 / /1.25) like the [ / ] keys. Reset params
        # (restore the launch values + re-lay the original) rounds out the set.
        live_edit = []
        if self.live_adjust:
            live_edit = [
                (lambda: "Voltage +", lambda: self._bump("voltage", 0.5),
                 {"role": "knob:voltage:+"}),
                (lambda: "Voltage −", lambda: self._bump("voltage", -0.5),
                 {"role": "knob:voltage:-"}),
                (lambda: "Pressure +", lambda: self._bump("pressure", 6894.76),
                 {"role": "knob:pressure:+"}),
                (lambda: "Pressure −", lambda: self._bump("pressure", -6894.76),
                 {"role": "knob:pressure:-"}),
                (lambda: "Temperature +", lambda: self._bump("temperature", 5.0),
                 {"role": "knob:temperature:+"}),
                (lambda: "Temperature −",
                 lambda: self._bump("temperature", -5.0),
                 {"role": "knob:temperature:-"}),
                (lambda: "Print speed +",
                 lambda: self._bump_print_speed(1.25), {"role": "knob:speed:+"}),
                (lambda: "Print speed −",
                 lambda: self._bump_print_speed(1.0 / 1.25),
                 {"role": "knob:speed:-"}),
                (lambda: "Reset params", self._reset_params,
                 {"role": "knob:reset"}),
            ]

        # -- Scaffold: how the deposited part is drawn --------------------- #
        #
        # There is no "Scaffold" switch here any more. It showed the EXPORTED STL
        # as a second, solid copy of the part -- and `run()` builds and shows the
        # deposition mesh at startup unconditionally, so the copy landed on top of
        # geometry already in the scene at the same coordinates (measured
        # coincident to the millimetre on a calibration slide). Its
        # only difference was that the STL omits the travel fibre
        # without --travel-in-stl, which the Travel-fibre switch already answers.
        # A switch whose entire effect is to redraw what is drawn is a switch that
        # reads as broken, and it was reported as one. `stl_path` still matters --
        # it is how the .depo cache and the plate sibling are found.
        scaffold = []
        # ONE button for whichever collector this run has -- the flat plate in
        # 3-axis, the mandrel rod in 4-axis. Offered only when a plate STL
        # sibling exists, it would leave a tube print with no way to get the rod
        # out of the way at all.
        if self._collector_available():
            scaffold.append((lambda: "Hide collector" if self._collector_on()
                             else "Show collector", self._toggle_collector,
                             {"toggle": ("Collector", self._collector_on)}))
        # deposition works straight from the g-code (rebuilds the sagged mesh
        # itself), so it's offered even when no STL was handed in
        scaffold.append((lambda: "Hide deposition" if self._depo_on()
                         else "Show deposition", self._toggle_deposition,
                         {"toggle": ("Deposition", self._depo_on)}))
        # the commanded g-code lines, gold deposit + grey travel: hide to see the
        # trailing mesh unobstructed. The state is the OR of the two, so the switch
        # cannot read OFF while either set is drawn (see _toggle_deposit_lines).
        scaffold.append((lambda: "Hide g-code path" if self._gcode_lines_on()
                         else "Show g-code path", self._toggle_deposit_lines,
                         {"toggle": ("G-code path", self._gcode_lines_on)}))
        # per-scaffold colour, only when the run carries scaffold colours
        if self._depo_can_color:
            scaffold.append((lambda: "Solid color" if self._depo_colored
                             else "Scaffold colors", self._toggle_depo_color,
                             {"toggle": ("Scaffold colors",
                                         lambda: self._depo_colored)}))

        # -- Printer: the machine, printhead and jet. They are things IN the
        #    scene, so they sit on the Scene tab beside the scaffold (see
        #    viewer_sidebar_spec.TAB_OF_SECTION). ------------------------ #
        printer = []
        if self.machine is not None:
            printer.append((lambda: "Hide printer" if self._machine_on
                               else "Show printer", self._toggle_machine,
                               {"toggle": ("Machine", lambda: self._machine_on)}))
        # the printhead can be hidden to see the jet + contact point unobstructed;
        # only offered when there IS one (a real head STL or a machine head part) --
        # with neither, the scene is just the jet, so the toggle would be a no-op
        if self.head_actor is not None or self.machine is not None:
            printer.append((lambda: "Hide printhead" if self._head_visible
                               else "Show printhead", self._toggle_printhead,
                               {"toggle": ("Printhead",
                                           lambda: self._head_visible)}))
            # see-through or solid; off (solid) by default. Not in machine-live,
            # which always draws the head solid
            if not self.machine_live:
                printer.append((lambda: "Solid printhead"
                                   if self._see_through_on()
                                   else "See-through printhead",
                                   self._toggle_see_through_head,
                                   {"toggle": ("See-through printhead",
                                               self._see_through_on)}))
        # the jet (molten thread), independent of the printhead; auto-off at t>=total
        printer.append((lambda: "Hide jet" if self._jet_visible
                           else "Show jet", self._toggle_jet,
                           {"toggle": ("Jet", lambda: self._jet_visible)}))
        # -- Simulation: what the animation adds over time ------------- #
        simulation = []
        simulation.append((lambda: "Hide travels" if self._travels_on
                           else "Show travels", self._toggle_travels,
                           {"toggle": ("Travels",
                                       lambda: self._travels_on)}))
        # the g-code listing: a scrolling window of the file, the current
        # line lit, in place of the readout's single Line / Move rows. Only the
        # sidebars that draw it (EmbedChrome.set_listing), only with a file.
        if (self._themed and self._chrome is not None
                and hasattr(self._chrome, "set_listing")
                and self.gcode_lines and not self.machine_live):
            simulation.append((lambda: "Hide g-code listing" if self._listing_on
                               else "Show g-code listing",
                               self._toggle_listing,
                               {"toggle": ("G-code listing",
                                           lambda: self._listing_on)}))
        # live-edit toggle (embedded viewer): relaunch in real-time-adjust mode
        # (v/f/h/[ ] edit the jet live, from t=0) or back to the static view. The
        # relaunch is unavoidable -- the live re-lay can't jump to the finished part.
        if self.embed is not None:
            simulation.append((lambda: "Exit live edit" if self.live_adjust
                               else "Live edit", self._request_live_edit,
                               {"toggle": ("Live edit",
                                           lambda: self.live_adjust)}))

        # -- Deviation: recolour the scaffold by predicted-vs-commanded departure.
        #    Embedded viewer only -- the button asks the GUI to run the deviation
        #    analysis (gui_deviation), which pushes the sidecar back down (apply-in-place). NOT in
        #    the live-edit sim: that re-lays geometry incrementally with no matching
        #    per-print sidecar, so deviation is a locked-sim-only view. ---------- #
        deviation = []
        if self.embed is not None and not self.live_adjust:
            deviation.append((lambda: "Hide deviation" if self._deviation_on
                              else "Show deviation", self._toggle_deviation,
                              {"toggle": ("Deviation",
                                          lambda: self._deviation_on)}))

        # -- View: camera presets + the orientation gizmo ------------------ #
        view = [(lambda: "Snap top-down", self._snap_top)]
        # Lighting A/B. Lives under View because it changes how the scene is lit,
        # not what is in it -- and it is the one control that makes a fibre read
        # as round rather than as a flat pale bar.
        view.append((lambda: "Flat lighting" if self._studio_light
                     else "Studio lighting", self._toggle_studio_light,
                     {"toggle": ("Studio lighting",
                                 lambda: self._studio_light)}))
        if self._cube is not None:
            # one chrome.button is all the fan-out the cube needs: the ttk rail,
            # the VTK sidebar and the native-fullscreen chrome all draw it from
            # this same row (the cube itself is scene-side, so it is already in
            # every one of those states).
            view.append((self._cube.label, self._toggle_view_cube,
                         {"toggle": ("View cube", self._cube.visible)}))
        # Orthographic (the default) vs perspective. Orthographic is what keeps
        # parallel fibres parallel and what gives the unlimited zoom-in, so it
        # stays the default; perspective is here for the depth cue when reading a
        # tall stack or the machine body. Label + switch state come straight off
        # the camera, so nothing caches a mode that a re-frame could contradict.
        _proj_lbl, _proj_cb, _proj_on = projection_toggle(self.renderer)
        view.append((_proj_lbl, _proj_cb, {"toggle": ("Perspective", _proj_on)}))
        # the XYZ origin triad, on by default; same row on every collector
        view.append((lambda: "Hide origin" if self.origin_actor.GetVisibility()
                     else "Show origin", self._toggle_origin,
                     {"toggle": ("Origin",
                                 lambda: bool(self.origin_actor.GetVisibility()))}))

        groups = [("Playback", playback), ("Live edit", live_edit),
                  ("Scaffold", scaffold), ("Printer", printer),
                  ("Simulation", simulation),
                  ("Deviation", deviation), ("View", view)]
        specs = [s for _sec, g in groups for s in g]   # flat list for the fallback

        if self._themed and self._chrome is not None:
            # Modern sidebar: mount each non-empty section under its header. Every
            # callback runs the same `_after_control()` housekeeping (apply time ->
            # refresh labels -> render) the old floating buttons did; the chrome's
            # own click handler additionally refreshes labels + renders + aborts orbit.
            def _wrap(cb):
                def _w():
                    cb()
                    self._after_control()
                return _w

            for section, section_specs in groups:
                if not section_specs:
                    continue
                self._chrome.section(section)
                for label_fn, cb, *hint in section_specs:
                    self._chrome.button(label_fn, _wrap(cb),
                                        **(hint[0] if hint else {}))

            # keep self.buttons pointing at the same list the chrome owns
            # (each entry: (actor, label_fn, callback)) so _refresh_buttons and
            # the rest of the class see no shape change
            self.buttons = self._chrome.buttons
            self._chrome.layout()
            # the chrome's own ConfigureEvent hook re-lays the sidebar; add
            # ours to keep the HUD line in step (it lives in the chrome too)
            self._click_tag = self._chrome.install_click_handler(self.interactor)
        else:
            # legacy floating-button fallback (unchanged behaviour for a
            # hand-launched player that cannot import gui_viewer_shell)
            for label_fn, cb, *_hint in specs:
                b = vtk.vtkTextActor()
                tp = b.GetTextProperty()
                tp.SetFontSize(15)
                tp.SetColor(0.92, 0.92, 0.96)
                tp.SetBackgroundColor(0.20, 0.22, 0.28)
                tp.SetBackgroundOpacity(0.85)
                tp.SetFrame(1)
                tp.SetFrameColor(0.45, 0.48, 0.55)
                self.renderer.AddActor2D(b)
                self.buttons.append([b, label_fn, cb])
            self._refresh_buttons()
            self._place_overlays()
            self._click_tag = self.interactor.AddObserver(
                "LeftButtonPressEvent", self._on_click, 10.0)
            self.interactor.AddObserver("ConfigureEvent",
                                        self._place_overlays)

    def _build_slider(self):
        # Embedded: the transport scrubber is a real ttk.Scale in the GUI (fed
        # by the host's "state" messages, driving the "scrub" command) -- no
        # in-scene VTK slider at all, in the panel OR in native fullscreen
        # (the GUI pins a slim ttk transport strip over the native window; the
        # vtkSliderWidget's pick geometry does not survive the offscreen->
        # native window flip, so the reliable ttk scale drives both).
        # _apply_time / _on_scrub guard on None.
        self._slider_cfg_tag = None
        if self.embed is not None:
            self.slider = None
            return
        self._create_slider()

    def _create_slider(self, pal=None):
        rep = vtk.vtkSliderRepresentation2D()
        rep.SetMinimumValue(0.0)
        rep.SetMaximumValue(max(self.total, 1e-6))
        rep.SetValue(min(max(self.t, 0.0), self.total))
        rep.SetTitleText("")
        rep.ShowSliderLabelOff()
        rep.GetPoint1Coordinate().SetCoordinateSystemToNormalizedDisplay()
        rep.GetPoint2Coordinate().SetCoordinateSystemToNormalizedDisplay()
        rep.SetSliderLength(0.02)
        rep.SetSliderWidth(0.025)
        rep.SetTubeWidth(0.008)
        # Palette-coloured (modern) or the original gold-on-slate (legacy).
        # The scrubber is the one horizontal control -- it stays a thin transport
        # bar along the BOTTOM of the 3D view (right of the sidebar), the
        # universal media-player convention; it never overlaps the sidebar.
        # `pal` is passed explicitly by the native-fullscreen hook (the embed
        # chrome exports rows and has no palette of its own); the windowed
        # themed path reads it off its ViewerChrome as before.
        if pal is None and self._themed and self._chrome is not None:
            pal = getattr(self._chrome, "pal", None)
        if pal is not None:
            from gui_viewer_shell import _hex_to_rgb, SIDEBAR_WIDTH_PX
            handle_rgb = _hex_to_rgb(pal["select_bg"])
            tube_rgb   = _hex_to_rgb(pal["active"])
            cap_rgb    = _hex_to_rgb(pal["border"])

            # The rep's NormalizedDisplay coords are relative to the widget's
            # CurrentRenderer, so the p1/p2 below are window-relative ONLY if the
            # widget draws into a FULL-WINDOW renderer. The themed chrome confines
            # the SCENE renderer to a right-hand box (240-px sidebar); parenting
            # the slider to that box shifted every 2D actor right by the sidebar
            # width (handle drew at 75% when t=50%, last ~22% off-screen). The
            # chrome's own overlay renderer (`_chrome.chrome`) spans the whole
            # window, so we bind the widget to it below -- then these fractions
            # ARE window-relative, and the widget's hit-testing (which also reads
            # CurrentRenderer) draws and picks in the same frame.
            def _place_slider(*_):
                w, _h = self.window.GetSize()
                sb_frac = SIDEBAR_WIDTH_PX / max(w, 1)
                rep.GetPoint1Coordinate().SetValue(sb_frac + 0.03, 0.05)
                rep.GetPoint2Coordinate().SetValue(0.98, 0.05)
            _place_slider()
            self._slider_cfg_tag = self.interactor.AddObserver(
                "ConfigureEvent", _place_slider)
        else:
            handle_rgb = (0.96, 0.76, 0.20)
            tube_rgb   = (0.4, 0.42, 0.5)
            cap_rgb    = (0.4, 0.42, 0.5)
            rep.GetPoint1Coordinate().SetValue(0.28, 0.06)
            rep.GetPoint2Coordinate().SetValue(0.97, 0.06)
        rep.GetSliderProperty().SetColor(*handle_rgb)
        rep.GetSelectedProperty().SetColor(*handle_rgb)
        rep.GetTubeProperty().SetColor(*tube_rgb)
        rep.GetCapProperty().SetColor(*cap_rgb)
        self.slider = vtk.vtkSliderWidget()
        self.slider.SetInteractor(self.interactor)
        self.slider.SetRepresentation(rep)
        self.slider.SetAnimationModeToOff()
        # Draw the scrubber into a FULL-WINDOW renderer, not the sidebar-confined
        # scene box. vtkSliderRepresentation2D lays its NormalizedDisplay actors
        # out relative to the widget's CurrentRenderer AND hit-tests against the
        # same one, so binding both to a full-window overlay keeps the drawn
        # handle and the pick geometry in lockstep, window-relative. Without this
        # the widget defaults to picking the scene renderer -- confined to
        # (sidebar_frac, 0, 1, 1) -- shifting the whole track +240 px right (the
        # handle hit ~75% at t=50 %, and the last ~22 % drew off-window). The
        # chrome's layer-1 overlay renderer (`.chrome`) spans the whole window;
        # the legacy non-themed path has no chrome, so it keeps VTK's default
        # (its scene renderer IS the full window) and stays pixel-identical.
        _ovl = getattr(self._chrome, "chrome", None) if self._chrome else None
        if _ovl is not None:
            self.slider.SetCurrentRenderer(_ovl)
            self.slider.SetDefaultRenderer(_ovl)
        self.slider.EnabledOn()
        # dragging the slider scrubs the clock (and pauses, like a real scrubber)
        self.slider.AddObserver("InteractionEvent", self._on_scrub)
        self.slider.AddObserver("StartInteractionEvent", self._on_scrub_start)

    # -- printhead + jet ---------------------------------------------------- #
    def _build_printhead(self, diag):
        """Load the real printhead STL and remember its needle-tip offset so the
        tip can be parked on (commanded point + standoff) each frame. The tip is
        found geometrically -- the centroid of the lowest ring of points -- so it
        works for any printhead model, not just this one. A missing asset draws
        no tool, so the player never hard-depends on the STL being present.

        With `_hide_printhead` (the printer model toggled off, or the printhead ticked
        Hidden in the config), skip the STL -- so those toggles hide the standalone
        printhead just as hidden_parts hides the machine model's head."""
        if (not self._hide_printhead
                and self.head_stl and os.path.isfile(self.head_stl)):
            try:
                reader = vtk.vtkSTLReader()
                reader.SetFileName(self.head_stl)
                reader.Update()
                pd = reader.GetOutput()
                pts = numpy_support.vtk_to_numpy(pd.GetPoints().GetData())
                zmin = float(pts[:, 2].min())
                # needle tip = centroid of the bottom-most points (the syringe
                # nozzle), in the STL's own coordinate frame
                self._head_tip = pts[pts[:, 2] <= zmin + 1e-3].mean(axis=0)
                mapper = vtk.vtkPolyDataMapper()
                mapper.SetInputConnection(reader.GetOutputPort())
                self.head_actor = vtk.vtkActor()
                self.head_actor.SetMapper(mapper)
                self.head_actor.SetScale(self.head_scale)
                prop = self.head_actor.GetProperty()
                prop.SetColor(*PRINTHEAD_COLOR)
                prop.SetSpecular(0.3)
                prop.SetSpecularPower(20)
                self._see_through_head()
                self.renderer.AddActor(self.head_actor)
                self._has_printhead = True
                return
            except Exception as exc:  # noqa: BLE001 -- fall back, never crash
                sys.stderr.write(f"gcode_player: printhead STL load failed: {exc}\n")

        # No printhead asset (and no machine model): show NO tool -- just the jet and
        # the deposited fibre. There is no nozzle frustum.
        # head_actor stays None; every consumer guards on it (see _place_head_and_jet,
        # _toggle_printhead, _reveal_sim_tool, the --static setup).
        self.head_actor = None
        self._has_printhead = False

    def _see_through_on(self):
        """The "See-through printhead" switch: off (a solid head) until the
        user turns it on in this launch."""
        return bool(getattr(self, "_head_see_through", False))

    def _see_through_head(self):
        """Draw the printhead body see-through (PRINTHEAD_OPACITY) when the
        switch is on, solid when it is off, on both paths that build it: the
        standalone STL and the machine model's head. Always solid in
        machine-live, where there is no print under it to watch."""
        if self.head_actor is None:
            return
        see = self._see_through_on() and not self.machine_live
        prop = self.head_actor.GetProperty()
        prop.SetOpacity(PRINTHEAD_OPACITY if see else 1.0)
        prop.SetBackfaceCulling(bool(see))

    def _toggle_see_through_head(self):
        """Sidebar switch: a see-through or a solid printhead. It applies at
        once and lasts for this launch only."""
        self._head_see_through = not self._see_through_on()
        self._see_through_head()

    # -- accel-limited re-timing (mew_kinematics) --------------------------- #
    def _retime_kinematic(self, verts, times_naive, is_dep, seg_feeds, seg_line):
        """Re-time the parsed toolpath onto the machine's accel-limited trajectory.

        `parse_path` times every move as length/feed: infinite acceleration, full
        feed through every corner, a hard stop only at the path ends. This replaces
        that with the trapezoidal look-ahead trajectory `mew_kinematics` plans
        (Klipper accel + square-corner-velocity cornering), then RESAMPLES it so each
        accel/decel ramp is split finely enough for the head to visibly ease in and
        out. Returns (verts, times, is_dep, seg_feeds) on the kinematic clock.

        The resampled grid is a superset of the commanded vertices (corners stay
        exact); each resampled segment inherits its COMMANDED move's kind, feed and
        source line, so per-frame slicing, the jet's per-segment speed and the line
        readout are unchanged -- only the time labels and the in-ramp sampling
        differ, and the cost grows only where the path actually ramps.

        Also records the monotonic naive->kinematic vertex time map (`_naive_t` /
        `_kin_t`) consumed by `_to_clock`: the .depo cache is written on the NAIVE
        clock, so its point times are remapped to stay on the kinematic one --
        keeping the jet anchored to the fibre it lays.

        A no-op returning the inputs unchanged (identity time map) when the planner
        is unavailable, disabled (`max_accel <= 0`), or there is nothing to plan; so
        with kinematics off the player is byte-identical to the pre-kinematics build,
        and as `max_accel -> infinity` the kinematic clock collapses back to naive."""
        self._naive_t = self._kin_t = None        # identity remap until proven otherwise
        self._traj = None                         # the planned trajectory (HUD speed_at)
        if plan_trajectory is None or self.max_accel <= 0.0 or len(verts) < 2:
            return verts, times_naive, is_dep, seg_feeds, seg_line
        traj = plan_trajectory(verts, seg_feeds, max_accel=self.max_accel,
                               square_corner_velocity=self.scv,
                               seg_accel=self._rotary_seg_accel(verts))
        if not traj.moves:
            return verts, times_naive, is_dep, seg_feeds, seg_line
        rt, rp, _rspeed, _rarc, rseg = traj.resample()
        rt = np.asarray(rt, dtype=np.float64)
        rp = np.asarray(rp, dtype=np.float64)
        rseg = np.asarray(rseg, dtype=np.int64)
        # naive<->kinematic map at the commanded vertices (both strictly increasing),
        # used to carry the naive-clock cache onto this clock; keep the trajectory so
        # the HUD can read the ACTUAL accel-limited speed at the playhead (speed_at)
        self._naive_t = np.asarray(times_naive, dtype=np.float64)
        self._kin_t = np.asarray(traj.times, dtype=np.float64)
        self._traj = traj
        # resampled segment k->k+1 belongs to commanded move rseg[k+1] (samples carry
        # the index of the move they end; the boundary sample opens the next move),
        # so it inherits that move's kind + feed
        move_of_seg = rseg[1:]
        is_dep_rs = np.asarray(is_dep, dtype=bool)[move_of_seg]
        seg_feeds_rs = np.asarray(seg_feeds, dtype=np.float64)[move_of_seg]
        seg_line_rs = np.asarray(seg_line, dtype=np.int64)[move_of_seg]
        self._retime_rotary(verts, rp, rseg)
        return rp, rt, is_dep_rs, seg_feeds_rs, seg_line_rs

    def _rotary_seg_accel(self, verts):
        """Per-segment accelerations for the planner under the mandrel's own
        rotary limit, or None (one limit for the whole move, byte-identical).

        The split comes from the FILE: `self._rotary` holds the commanded
        machine point and the A reading per vertex, so a segment's linear travel
        is its X/Y/Z step and its rotation its A step, while its length is the
        one the planner plans on (`verts`). The export reads the same split off
        the mandrel's chart (`mew_kinematics.chart_seg_accels`), so the clock
        and the deposited fibre ramp alike. A five-axis pair (`rot` 2-D) plans
        on the one limit, as its export does: that path has no chart to read."""
        if axis_seg_accels is None or self._rotary is None:
            return None
        mach = np.asarray(self._rotary[0], dtype=np.float64)
        rot = np.asarray(self._rotary[1], dtype=np.float64)
        if rot.ndim != 1 or len(rot) != len(verts):
            return None
        pts = np.asarray(verts, dtype=np.float64)
        return axis_seg_accels(
            np.linalg.norm(np.diff(pts, axis=0), axis=1),
            np.linalg.norm(np.diff(mach, axis=0), axis=1),
            np.abs(np.diff(rot)), self.max_accel, self.rotary_accel)

    def _retime_rotary(self, verts, rp, rseg):
        """Carry `self._rotary` onto the resampled grid, in place.

        `(mach, rot)` is the one PER-VERTEX array `parse_path` returns besides
        `verts`/`times` -- everything else the playhead indexes (`is_dep`,
        `seg_feeds`, `seg_line`, and the `dep_idx`/`trv_idx`/`_line_step_times`
        derived after this call) is already resampled. It rides the playhead's
        segment index in `_rotary_at`, so leaving it on the COMMANDED grid means
        the mandrel spin and the machine nozzle point are read out of a different
        move than the one the head is on: measured on a reentrant tube, up to
        1280 deg of rotary word and 12.6 mm of nozzle X (the whole circumference),
        which drove the jet 12.7 mm sideways on a zero-lag control.

        Every resampled point lies exactly on its commanded move (`resample` places
        it at `start + unit*d`), so the fraction along that move is a projection --
        exact inside an accel ramp, where interpolating the naive<->kinematic vertex
        clock would not be (position is quadratic in time there, not linear).
        No-op on a flat collector, where `_rotary` is None."""
        if self._rotary is None:
            return
        mach = np.asarray(self._rotary[0], dtype=np.float64)
        rot = np.asarray(self._rotary[1], dtype=np.float64)
        cmd = np.asarray(verts, dtype=np.float64)
        i0 = np.asarray(rseg, dtype=np.int64)      # commanded move of each sample
        a = cmd[i0]
        d = cmd[i0 + 1] - a
        L2 = np.einsum("ij,ij->i", d, d)
        f = np.zeros(i0.size, dtype=np.float64)
        ok = L2 > 1e-24                            # a null move stays at its start
        f[ok] = np.einsum("ij,ij->i", rp[ok] - a[ok], d[ok]) / L2[ok]
        np.clip(f, 0.0, 1.0, out=f)
        # `rot` is (P,) on a mandrel's one joint and (P, J) on a five-axis pair.
        # The broadcast shape is taken from the array rather than written as
        # `f` or `f[:, None]`, so the one-joint arithmetic is the expression it
        # always was -- `fr` IS `f` when rot is 1-D -- and the pair is not a
        # second code path that could drift from it.
        fr = f.reshape((-1,) + (1,) * (rot.ndim - 1))
        self._rotary = (mach[i0] + f[:, None] * (mach[i0 + 1] - mach[i0]),
                        rot[i0] + fr * (rot[i0 + 1] - rot[i0]))

    def _to_clock(self, t_naive):
        """Map a time (or array) from the .depo cache's naive length/feed clock onto
        the player's accel-limited clock, via the monotonic vertex map stashed by
        `_retime_kinematic`. Identity when kinematics is off (no map stored), so the
        cache flows through untouched. Cheap (one np.interp); applied wherever a
        cache time meets the playback clock (jet anchor, deposition mesh, live re-lay)
        so the jet keeps meeting the fibre it lays."""
        if self._naive_t is None or self._kin_t is None:
            return t_naive
        return np.interp(t_naive, self._naive_t, self._kin_t)

    def _from_clock(self, t_kin):
        """Inverse of `_to_clock`: map a playback (accel-limited) time back onto the
        naive length/feed clock the incremental march (mew_incremental) keys its
        layer-completion times on. Identity when kinematics is off. Lets the player
        tell the march how far the playhead has reached in the march's own clock."""
        if self._naive_t is None or self._kin_t is None:
            return t_kin
        return np.interp(t_kin, self._kin_t, self._naive_t)

    def _build_jet_source(self):
        """Build the time-ordered deposited centreline (x,y,z paired with print
        time) the jet's bottom is anchored to. Prefers the sagged centrelines from
        the converter's .depo sidecar -- true LANDED Z, and instant to load since
        only TUBING them is costly -- so the jet meets the fiber where it actually
        rests. Falls back to the commanded deposition vertices (no sag data) when
        no matching cache exists, so the jet still works on a hand-launched or
        string-art run; it just anchors at the commanded Z."""
        opts = self.fiber_opts
        diameter = float(opts.get("diameter", 0.02))
        groups = opts.get("scaffold_groups")
        xyz = ttimes = diam = None
        from_cache = False
        try:
            cached = load_depo_cache(
                self._find_depo_cache(),
                diameter=diameter,
                no_sag=bool(opts.get("no_sag", False)),
                sag_approx=bool(opts.get("sag_approx", False)),
                sag_multiplier=float(opts.get("sag_multiplier", 1.0)),
                sag_reset_layers=int(opts.get("sag_reset_layers", 0) or 0),
                render_travel=bool(opts.get("render_travel", False)),
                self_crossings=not file_skips_self_crossings(self.gcode_path),
                gcode_path=self._depo_staleness_ref(),
                transformed=self.physics_deposit,
                phys_sag=self.physical_sag, phys_fuse=self.physical_fusion)
        except Exception:  # noqa: BLE001 -- a bad cache just means fall back
            cached = None
        if cached is not None:
            paths, ptimes = cached
            if paths:
                # `load_depo_cache` only returns a cache whose meta `transformed`
                # equals the `transformed=` asked for above, so this geometry was
                # laid in exactly this player's deposition mode -- which is the
                # fact `_set_jet_source` needs and cannot measure.
                from_cache = True
                xyz = np.concatenate([np.asarray(p, np.float64) for p in paths])
                ttimes = np.concatenate(
                    [np.asarray(t, np.float64) for t in ptimes])
                # per-point fiber diameter so the jet's bottom thickens/thins to
                # match the fibre it's laying. Prefer the physics dynamic-diameter
                # lookup the export stored (diameter varies with the head's speed --
                # the slower linking loops are thicker); else each scaffold's own
                # per-path diameter (multi-scaffold runs); else the single diameter.
                lookup = load_depo_diam_by_time(self._find_depo_cache())
                if lookup is not None:
                    diam = np.interp(ttimes, lookup[0], lookup[1])
                else:
                    path_diam = path_diameters_from_groups(groups, len(paths),
                                                           diameter)
                    diam = np.concatenate([
                        np.full(len(p),
                                diameter if path_diam is None
                                else float(path_diam[i]),
                                dtype=np.float64)
                        for i, p in enumerate(paths)])
                # Drop each fibre's lingering touchdown once the next fibre starts
                # depositing, so the time-sorted anchor never interleaves a finished
                # fibre's tail with the next (the linking-loop "jet stutter" -- see
                # jet_source_keep_mask). Computed on the cache's own naive clock.
                keep = jet_source_keep_mask(ptimes)
                xyz, ttimes, diam = xyz[keep], ttimes[keep], diam[keep]
                # the cache is written on the naive length/feed clock; carry its point
                # times onto the player's accel-limited clock so the jet anchors to the
                # fibre at the right instant. (diam was interpolated just above on the
                # cache's own naive clock, so it is correct and left untouched.)
                ttimes = self._to_clock(ttimes)
        if xyz is None and self.dep_idx.size:
            # fallback: every deposition vertex at its commanded position/time,
            # all at the single diameter (no per-scaffold split without the cache)
            idx = np.unique(np.concatenate([self.dep_idx, self.dep_idx + 1]))
            xyz = self.verts[idx]
            ttimes = self.times[idx]
            diam = np.full(len(idx), diameter, dtype=np.float64)
        # Reshaped iff the cache supplied the geometry AND this player is in the
        # physics-deposition mode the cache was accepted under; the fallback
        # above is the commanded path itself, which never carries the lag.
        self._set_jet_source(xyz, ttimes, diam,
                             reshaped=from_cache and self.physics_deposit)

    def _adopt_laid_jet_source(self, laid, diameter, *, reshaped):
        """Anchor the jet on the LAID centreline a from-g-code mesh was built from.

        `laid` is `build_deposition_mesh(..., return_paths=True)`'s second value:
        (paths, ptimes, path_diam, diam_by_time), already transformed and sagged --
        the same curve the tubes follow. Applies `jet_source_keep_mask` so a finished
        fibre's lingering touchdown stops competing with the next fibre's start (the
        linking-loop jet stutter), and carries the naive point times onto the player's
        clock. Best-effort: a failure leaves the existing anchor alone rather than
        losing the jet.

        `reshaped` is whether the deposition transform was actually installed for
        that build -- `_physics_fiber_transform` yields exactly that, and it is
        False whenever the physics was off or unavailable, so the caller passes
        the yielded value straight through (see `_set_jet_source`)."""
        try:
            paths, ptimes, path_diam, diam_by_time = laid
            if not paths:
                return
            xyz = np.concatenate([np.asarray(p, np.float64) for p in paths])
            tt = np.concatenate([np.asarray(t, np.float64) for t in ptimes])
            if diam_by_time is not None:
                diam = np.interp(tt, diam_by_time[0], diam_by_time[1])
            else:
                diam = np.concatenate([
                    np.full(len(p), diameter if path_diam is None
                            else float(path_diam[i]), dtype=np.float64)
                    for i, p in enumerate(paths)])
            keep = jet_source_keep_mask(ptimes)
            xyz, tt, diam = xyz[keep], tt[keep], diam[keep]
            self._set_jet_source(xyz, self._to_clock(tt), diam,
                                 reshaped=reshaped)
        except Exception as exc:  # noqa: BLE001 -- keep the old anchor, never crash
            sys.stderr.write("gcode_player: laid jet anchor unavailable (%s)\n" % exc)

    def _set_jet_source(self, xyz, ttimes, diam, *, reshaped):
        """Adopt a deposited centreline (concatenated points + times + per-point
        diameter) as the jet's anchor. Split out from _build_jet_source so the
        real-time re-lay can feed its freshly computed geometry through the SAME
        path (sort by time, set the anchor arrays, record whether the geometry is
        physics-reshaped) -- keeping the jet meeting the fibre it lays.

        `reshaped` -- whether the jet-lag is ALREADY BAKED into these points --
        is a required keyword because every caller knows it as a fact and it
        cannot be measured reliably from the geometry. The `.depo` path reads
        `transformed` out of the cache meta and `load_depo_cache` rejects a
        mismatch; the from-g-code rebuild is told by
        `_physics_fiber_transform`; the live re-lay always lays through the
        contact tracker; the commanded-vertex fallback never reshapes.

        Do not put back a displacement threshold. The retired guess assumed a
        reshaped fibre trails the command by ~the lag distance everywhere, which
        is false wherever the path reverses sooner than the touchdown settles --
        on the reentrant unit cell it read as commanded and `_reveal_time` then cut
        at `t - lag` on geometry that already carried it, taking the jet's mean
        horizontal reach from 0.143 mm to 0.568, past its own physical ceiling."""
        if xyz is None or len(xyz) == 0:
            self._jet_t = self._jet_xyz = self._jet_diam = None
            self._reshaped = False
            return
        order = np.argsort(ttimes, kind="stable")
        self._jet_t = np.ascontiguousarray(ttimes[order])
        self._jet_xyz = np.ascontiguousarray(xyz[order])
        self._jet_diam = np.ascontiguousarray(diam[order])
        # The reveal reads this to know whether the lag is already in the
        # geometry -- see _reveal_time.
        self._reshaped = bool(reshaped)

    # -- real-time parameter adjustment (live re-lay) ----------------------- #
    def _init_live_relay(self):
        """Stand up the real-time INCREMENTAL deposition: parse the
        commanded fibres and build the mew_incremental.IncrementalDeposition that
        time-marches the relay + sag + tube as the clock advances (frozen past, lazy
        future). Falls back to the locked sim if the g-code can't be parsed or
        physics is unavailable. Called once at construction when live_adjust is on.

        A ROTARY COLLECTOR MARCHES TOO, and it needs two things. A parse with no
        `frame=` collapses a tube onto its own axis -- on the default re-entrant
        cell tube, 265 points instead of 529, 138.230 mm of path instead of
        446.767 mm, zero circumferential extent, and a 17.8 s clock instead of
        48.8 s -- and `mew_deposition.laid_fibers` with neither `frame` nor
        `placement` lays even a correct parse flat. Handing the frame to the parse
        ALONE would make the failure LOOK right.

        Both halves are supplied from here. The frame is the one the scene
        already built (`self._frame` -- the single rotary switch this class has), and
        it unrolls the commanded fibres into (u, v, h) exactly as the batch converter
        does. The contact `placement` is built the SAME way the export's transform
        builds it (`mew_deposition.make_transform`): a `CylinderFrame` carrying THIS
        run's nozzle gap, because the cylindrical contact model needs the gap and the
        converter's light frame carries only the radius. Built here rather than
        imported from the export's closure on purpose -- the two must agree about the
        rig, not share a call stack."""
        opts = self.fiber_opts
        placement = None
        # `self.model is None` (physics unavailable) is handled by the existing
        # degrade below, after the parse -- don't reach through it for the gap here.
        if (self.model is not None and self._frame is not None
                and getattr(self._frame, "rotary", False)):
            try:
                import mew_mandrel as _mm
                placement = _mm.placement_for_frame(
                    self._frame.with_nozzle_gap(self.model.working_distance),
                    self.model.calibration, model=self.model)
            except Exception as exc:  # noqa: BLE001 -- degrade rather than lay flat
                sys.stderr.write(
                    "gcode_player: could not build the mandrel contact placement "
                    f"({exc}); playing the exported deposition locked instead.\n")
                self.live_adjust = False
                self._orch = None
                return
        try:
            self._cmd_paths, self._cmd_ptimes = parse_gcode(
                self.gcode_path, mode=opts.get("mode", "auto"), with_times=True,
                arc_segments=opts.get("arc_segments"), frame=self._frame)
        except Exception:  # noqa: BLE001
            self._cmd_paths = self._cmd_ptimes = None
        if not self._cmd_paths or self.model is None:
            # SAY SO (ledger B9). This is the degrade a five-axis file takes
            # every time -- `parse_gcode(frame=RevolutionFrame)` raises, so
            # `--live-adjust` on a curved collector has always quietly played
            # the exported deposition instead, and the window looked identical
            # to a live one. stderr, like the mandrel degrade above it, which
            # is what the GUI's Log page shows; a persistent HUD banner needs a
            # flag and a branch in `_refresh_hud` and is not this one line.
            sys.stderr.write(
                "gcode_player: nothing to march live (%s); playing the "
                "exported deposition locked instead.\n"
                % ("no physics model" if self.model is None
                   else "the g-code did not parse for this collector"))
            self.live_adjust = False        # nothing to march; degrade to locked
            return
        groups = opts.get("scaffold_groups")
        diameter = float(opts.get("diameter", 0.02))
        no_sag = bool(opts.get("no_sag", False))
        sag_multiplier = float(opts.get("sag_multiplier", 1.0))
        sag_reset_layers = int(opts.get("sag_reset_layers", 0) or 0)
        self_crossings = not file_skips_self_crossings(self.gcode_path)
        # one diameter per commanded fibre (per-scaffold groups), so each scaffold
        # marches at its own thickness -- the same seam the static export uses.
        base_pd = path_diameters_from_groups(groups, len(self._cmd_paths), diameter)
        # LOCKED-cache seed: the SAME `.depo` the locked sim rides. While UNEDITED the
        # march commits these cached fibres verbatim instead of re-deriving via
        # mew_live -- so the live sim is byte-for-byte the locked sim (mew_live and
        # mew_deposition reshape the centreline slightly differently, which otherwise
        # makes the live crossovers drift from the locked ones, inconsistently). Only
        # fibre laid AFTER a live knob edit re-derives. None when the cache is missing
        # or its params differ (load_depo_cache rejects a mismatch) -> the march
        # re-derives everything, exactly as before.
        locked = self._load_live_locked(diameter, no_sag, sag_multiplier,
                                        self_crossings, sag_reset_layers)
        # the rotary march runs on the mandrel's own amplitude and CTS, exactly
        # as the export's transform does (`mew_deposition.make_transform`)
        march_model = self.model
        if placement is not None:
            try:
                import mew_lag_fit as _mlf
                march_model = _mlf.mandrel_model(self.model)
            except Exception:  # noqa: BLE001 -- the flat model is the old behaviour
                march_model = self.model
        try:
            self._orch = mew_incremental.IncrementalDeposition(
                self._cmd_paths, self._cmd_ptimes, march_model,
                launch_pressure=self._launch_pressure,
                launch_voltage=self._launch_voltage,
                launch_temperature=self._launch_temperature,
                diameter=diameter,
                sag_multiplier=sag_multiplier,
                sag_reset_layers=sag_reset_layers,
                no_sag=no_sag,
                self_crossings=self_crossings,
                base_path_diam=base_pd,
                zeta=self.model.calibration.contact_damping_zeta,
                coil=self.coil, coil_wave_amp=self.coil_wave_amp,
                coil_wave_len=self.coil_wave_len, max_accel=self.max_accel,
                square_corner_velocity=self.scv,
                rotary_accel=self.rotary_accel,
                dynamic_diameter=self.dynamic_diameter,
                fill_per_tau=self.fill_per_tau,
                locked=locked, frame=self._frame, placement=placement,
                corner_chord=self.corner_chord)
        except Exception as exc:  # noqa: BLE001 -- degrade rather than fail to open
            sys.stderr.write(f"gcode_player: incremental init failed: {exc}\n")
            self.live_adjust = False
            self._orch = None

    def _load_live_locked(self, diameter, no_sag, sag_multiplier, self_crossings,
                          sag_reset_layers=0):
        """Load the exported `.depo` cache as the march's locked-geometry seed,
        aligned to commanded-fibre index: (paths, times, per-fibre diam, per-point
        ppd). Returns None (the march re-derives everything) when there is no cache,
        it mismatches the current sag/physics params (load_depo_cache rejects it), or
        its fibre count differs from the parsed commanded set. Best-effort: any
        failure degrades to None rather than breaking launch.

        `render_travel=False` and `sag_approx=False` are passed UNCONDITIONALLY,
        whatever the export did: this march parses the commanded fibres without
        travels (`_init_live_relay`) and sags with the EXACT `_SagState` stepper
        (mew_incremental), so either cache is geometry it cannot reproduce.
        Seeding from one would show cached fibre verbatim until the first live
        knob edit and different fibre after it, with a visible step at the
        boundary. The fibre-count guard below cannot catch a travel-rendered
        cache, because `merge_travels` preserves the count on purpose. Rejecting
        costs only the locked seed: the live sim re-derives from scratch, exactly
        as it does with no cache.

        A MANDREL RUN IS REFUSED OUTRIGHT, and that rejection is structural rather
        than a param mismatch. The converter writes the `.depo` AFTER the wrap
        (`gcode_to_fibers`, "the wrap"), so a tube's cache is in the collector's
        world space, while the march -- like the batch solver before that wrap --
        works in the unrolled (u, v, h) surface frame. Seeding one with the other
        would feed world coordinates to a surface-frame sag index, and the march
        would then wrap already-wrapped fibre on the way back out. Unwrapping the
        cache is not the fix either: the wrap DENSIFIED it, and `v` is unwrapped arc
        length whose winding number `to_world` has already discarded, so the inverse
        cannot return the geometry the export actually sagged. A tube is therefore
        re-derived from the commanded g-code, and `mew_incremental._use_export_relay`
        routes its constant-param layers through `mew_deposition.laid_fibers` -- the
        same engine that built that cache -- so what is given up is the byte
        identity, not the physics."""
        if self._frame is not None and getattr(self._frame, "rotary", False):
            return None
        try:
            cache = load_depo_cache(
                self._find_depo_cache(), diameter=diameter, no_sag=no_sag,
                sag_approx=False,
                sag_multiplier=sag_multiplier, self_crossings=self_crossings,
                sag_reset_layers=sag_reset_layers, render_travel=False,
                gcode_path=self._depo_staleness_ref(),
                transformed=self.physics_deposit,
                phys_sag=self.physical_sag, phys_fuse=self.physical_fusion)
            if cache is None:
                return None
            paths, ptimes = cache
            n = len(self._cmd_paths)
            if len(paths) != n:                 # reshape changed the fibre count
                return None
            pdia = load_depo_pdia(self._find_depo_cache())
            diam = [(float(pdia[i]) if pdia and i < len(pdia) and pdia[i] else
                     diameter) for i in range(n)]
            dbt = load_depo_diam_by_time(self._find_depo_cache())
            ppd = None
            if dbt is not None:
                ppd = [np.interp(np.asarray(ptimes[i], dtype=np.float64),
                                 dbt[0], dbt[1]) for i in range(n)]
            return (paths, ptimes, diam, ppd)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"gcode_player: live locked-seed load failed: {exc}\n")
            return None

    def _note_param_change(self):
        """Record the current knobs as taking effect at the deposition-time being
        laid right NOW (the jet's contact time). O(1): the march freezes everything
        already laid and only LATER layers pick up the new knobs (forward
        accumulation) -- there is no per-edit batch re-solve. The schedule updates
        immediately so the breakpoint lands at the true contact time; the heavier
        VISUAL refresh (tip re-tube) is debounced via the flag below. No-op when
        locked."""
        if not self.live_adjust or self._orch is None:
            return
        self._orch.set_params(
            self._from_clock(self._reveal_time()),
            mew_live.Params(self.pressure, self.voltage, self.temperature,
                            self.print_speed_scale))
        self._relay_dirty = True
        self._relay_req_wall = time.perf_counter()

    def _advance_deposition(self, force=False):
        """March the incremental deposition up to the playhead and, when the laid
        geometry changed, rebuild the jet anchor + mesh. Called every tick in live
        mode -- cheap when nothing crossed a layer boundary (the orchestrator's gate
        is a couple of comparisons and the tip is cached). A newly COMMITTED layer
        rebuilds promptly; a tip-only change from a live edit is debounced so a rapid
        knob-mash collapses to one tip re-tube. `PHASE4_LAYER_CAP` bounds the per-call
        sag so a big scrub deposits over frames instead of one long freeze. Returns
        True iff it rebuilt the geometry (so the caller renders)."""
        if not self.live_adjust or self._orch is None:
            return False
        # a due layer means the next advance runs a (possibly heavy) sag synchronously
        # -- flash a notice first so the brief freeze reads as work, not a hang
        if self._orch.pending(self._from_clock(self.t)):
            self.hud.SetInput("  Depositing fiber (physics)…  ")
            self.window.Render()
        self._orch.advance_to(self._from_clock(self.t), max_layers=PHASE4_LAYER_CAP)
        sig = (self._orch.n_committed, self._orch.tip_version())
        if not force and sig == self._depo_sig:
            return False
        # a committed-count change is a genuine new layer -> rebuild now; a tip-only
        # change is an unsettled edit -> wait out the debounce (coalesce key-mashes)
        committed_changed = (self._depo_sig is None or sig[0] != self._depo_sig[0])
        settled = (not self._relay_dirty
                   or time.perf_counter() - self._relay_req_wall >= RELAY_DEBOUNCE)
        if not force and not committed_changed and not settled:
            return False
        if settled:
            self._relay_dirty = False
        self._depo_sig = sig
        self._rebuild_live_geometry()
        return True

    def _rebuild_live_geometry(self):
        """Rebuild the jet anchor (always) and the deposition mesh (when shown) from
        the orchestrator's laid geometry: every COMMITTED (sagged, frozen) fibre plus
        the in-progress TIP layer (also SAGGED -- against the frozen structure below
        -- so the jet meets the fibre at its resting Z and the trail never floats at
        the commanded layer height; the playhead time-cut reveals only the part laid
        so far). Naive deposition times are remapped onto the player's kinematic
        clock. Tubes reuse a per-fibre cache keyed by commanded index, so a frozen
        committed fibre is tubed once and never again -- only the freshly committed
        layer + the tip are re-tubed. Robust: any failure leaves the prior geometry
        in place."""
        try:
            cpaths, ctimes, cdiam = self._orch.committed_fibers()
            cppd = self._orch.committed_ppd()
            tmembers, tpaths, ttimes, tdiam = self._orch.tip_fibers()
            tppd = self._orch.tip_ppd()
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"gcode_player: incremental gather failed: {exc}\n")
            return
        # the laid set in (committed-by-index, then tip) order, each tagged with its
        # commanded index (for per-scaffold colour and the tube cache). `pf_diam` is
        # the per-fibre representative scalar (cache key + fallback); `pp_diam` is the
        # per-POINT drawn diameter the jet + tube actually size from, so an already-
        # laid stretch keeps its diameter when a later edit retimes the schedule.
        idx, paths, ptimes_n, pf_diam, pp_diam = [], [], [], [], []
        for i in range(len(cpaths)):
            if cpaths[i] is not None:
                idx.append(i)
                paths.append(cpaths[i])
                ptimes_n.append(ctimes[i])
                pf_diam.append(cdiam[i])
                pp_diam.append(cppd[i])
        for k, i in enumerate(tmembers):
            idx.append(i)
            paths.append(tpaths[k])
            ptimes_n.append(ttimes[k])
            pf_diam.append(tdiam[k])
            pp_diam.append(tppd[k])
        if not paths:
            return
        # naive -> kinematic clock for the jet + reveal (geometry/diameter unchanged)
        ptimes = [self._to_clock(np.asarray(t, np.float64)) for t in ptimes_n]
        # per-point drawn diameter, defaulting to the representative scalar where a
        # fibre carries no per-point profile (sag N/A / passthrough)
        pp_diam = [np.asarray(d, np.float64) if d is not None and len(np.atleast_1d(d))
                   else np.full(len(paths[k]), pf_diam[k], np.float64)
                   for k, d in enumerate(pp_diam)]

        # jet anchor (always): each laid point at ITS OWN drawn diameter. Drop each
        # fibre's lingering touchdown once the next starts depositing (computed on the
        # naive clock, in deposition order) so the anchor never interleaves a finished
        # fibre's tail with the next -- the linking-loop "jet stutter", same fix as the
        # cached path (see jet_source_keep_mask).
        keep = jet_source_keep_mask(ptimes_n)
        xyz = np.concatenate([np.asarray(p, np.float64) for p in paths])[keep]
        tt = np.concatenate(ptimes)[keep]
        diam = np.concatenate(pp_diam)[keep]
        # `mew_incremental` lays every fibre it RE-DERIVES through the contact
        # tracker, so the march's own output carries the lag. The exception is the
        # locked `.depo` seed committed verbatim while unedited
        # (`_load_live_locked`), which carries it only iff `physics_deposit` -- the
        # `transformed=` the seed was accepted under. So: reshaped unless a
        # commanded seed is in play, which keeps the unedited live view reading the
        # same as the locked view it is built to match.
        # `has_locked` is a PROPERTY, so getattr hands back the bool itself;
        # calling it raises TypeError and kills the live player at startup.
        seeded = bool(getattr(self._orch, "has_locked", False))
        self._set_jet_source(xyz, tt, diam,
                             reshaped=self.physics_deposit or not seeded)

        if not self._depo_visible:
            return
        opts = self.fiber_opts
        groups = opts.get("scaffold_groups")
        diameter = float(opts.get("diameter", 0.02))
        try:
            colors = path_colors_from_groups(groups, len(self._cmd_paths),
                                             self._fiber_rgb255())
            path_color = [colors[i] for i in idx] if colors is not None else None
            # reuse the cached tube block for any fibre whose sagged geometry +
            # per-point diameter are unchanged (every committed fibre; the tip only
            # between edits) -- keyed by commanded index so list order is irrelevant
            cache = self._depo_tube_cache
            reuse = []
            for k, i in enumerate(idx):
                hit = cache.get(i)
                if (hit is not None
                        and np.array_equal(hit[0], np.asarray(paths[k], np.float64))
                        and np.array_equal(hit[1], pp_diam[k])):
                    reuse.append(hit[2])
                else:
                    reuse.append(None)
            # per-fibre time->diameter lookup (each fibre's own kinematic point times
            # vs its per-point drawn diameter): each vertex's tube radius is sized from
            # its OWN deposition time, so the drawn fibre forward-accumulates exactly
            # like the centreline -- the already-laid stretch keeps its diameter.
            dbt = [(ptimes[k], pp_diam[k]) for k in range(len(paths))]
            # tube facets = the export fidelity, floored at DEPO_FACES so the live
            # preview is never coarser than the STL (else fat crossovers float / poke)
            depo_faces = max(int(opts.get("faces", DEPO_FACES)), DEPO_FACES)
            v, tr, vt, nm, col, blocks = _tube_fibers(
                paths, ptimes, diameter, depo_faces,
                float(opts.get("max_ring_dt", 0.03)), path_diam=pf_diam,
                path_color=path_color, diam_by_time=dbt, reuse_blocks=reuse,
                return_blocks=True)
            self._depo_tube_cache = {
                i: (np.asarray(paths[k], np.float64), pp_diam[k], blocks[k])
                for k, i in enumerate(idx) if blocks[k] is not None}
            if len(tr):
                self._install_depo_mesh(v, tr, vt, nm, col)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"gcode_player: incremental tube failed: {exc}\n")

    def _reset_params(self):
        """Restore the as-launched knobs, collapse the schedule, and re-march from
        the start -- the live-mode 'undo' for a parameter sweep gone wrong. No-op
        when locked."""
        if not self.live_adjust or self._orch is None:
            return
        self.pressure = self._launch_pressure
        self.voltage = self._launch_voltage
        self.temperature = self._launch_temperature
        self.print_speed_scale = 1.0
        self._orch.reset(mew_live.Params(
            self._launch_pressure, self._launch_voltage,
            self._launch_temperature, 1.0))
        self._depo_tube_cache = {}
        self._depo_sig = None
        self._recompute_physics()
        self._relay_dirty = False
        self.t = 0.0
        self._last_wall = time.perf_counter()
        self._advance_deposition(force=True)
        self._after_control()

    def _contact_point(self, t_query):
        """The deposited centreline point at print-time `t_query` (linearly
        interpolated by time, clamped to the deposited span) and the fiber
        diameter there, as ((x,y,z), diameter). This is where the jet meets the
        fiber: both the jet's bottom and the reveal query it at _reveal_time(), so
        the jet bends to the trailing landing point and the mesh's leading edge ends
        on that same point -- and the diameter sizes the jet tube to match."""
        tt = self._jet_t
        t_query = min(max(t_query, float(tt[0])), float(tt[-1]))
        j = int(np.searchsorted(tt, t_query, side="right"))
        if j <= 0:
            return self._jet_xyz[0], float(self._jet_diam[0])
        if j >= len(tt):
            return self._jet_xyz[-1], float(self._jet_diam[-1])
        t0, t1 = tt[j - 1], tt[j]
        p0, p1 = self._jet_xyz[j - 1], self._jet_xyz[j]
        f = 0.0 if t1 <= t0 else (t_query - t0) / (t1 - t0)
        return p0 + f * (p1 - p0), float(self._jet_diam[j])

    def _jet_contact(self):
        """Where the jet's bottom currently sits, as ((x,y,z), fibre diameter).

        In MEW the jet never stops: a travel is real deposited material laid on
        its commanded (or physics-reshaped) line, exactly like a deposition move
        -- see `merge_travels` and `--render-travel`, unconditional on every GUI
        launch. So whenever the anchor (`_jet_xyz`/`_jet_t`, built from that same
        merged geometry) actually COVERS the current reveal time, the jet's foot
        is the laid fibre's point there, the same .depo point the mesh reveals to
        (see _reveal_time) -- whether the current segment is nominally a G1
        deposit or a G0 travel makes no difference to what physically sits under
        the nozzle. Querying the anchor when it does NOT cover the reveal time
        (no cache, or render-travel off) would clamp the jet to the nearest deposited point and PIN it there
        for the whole move; there, the jet instead trails the moving nozzle along
        the travel -- the COMMANDED position a lag-time back -- exactly as the
        head's own trailing thread would."""
        seg, _ = self._cursor()
        dep = bool(self.is_dep.size
                    and self.is_dep[min(seg, self.is_dep.size - 1)])
        t_reveal = self._reveal_time()
        jt = self._jet_t
        covered = (jt is not None and jt.size
                   and jt[0] - 1e-9 <= t_reveal <= jt[-1] + 1e-9)
        if dep or covered:
            return self._contact_point(t_reveal)
        # travelling, with no physically-deposited fibre there yet: trail the
        # nozzle along the commanded path (lag-time back)
        dseg, dfrac = self._cursor(max(0.0, self.t - self._reveal_lag()))
        contact = (self.verts[dseg]
                   + dfrac * (self.verts[dseg + 1] - self.verts[dseg]))
        diam = self.jet_state.fiber_diameter if self.jet_state is not None else 0.02
        return contact, diam

    def _lead_in_jet_gate(self, verts, is_dep):
        """Print-time before which NO jet is drawn -- the lead-in approach that
        flies in from beside the collector.

        On a collector the head can arrive from OUTSIDE (`covers` can answer
        False -- a surface of revolution, i.e. five axis), there is nothing
        under the nozzle for most of the approach, and `_jet_contact`'s fallback
        trailed the jet along the commanded path there: a jet hanging in the air
        beside the collector, which then SNAPPED onto the laid fibre the instant
        the anchor opened. The rule: no jet or deposition is
        drawn outside the collector, and both turn on as soon as the nozzle is
        above any portion of it.

        So the gate is the ANCHOR'S OWN FIRST POINT: `gcode_to_fibers.take_travel`
        clips the travel fibre to where the nozzle is genuinely over the
        collector (`DepositionFrame.under_nozzle`, the nozzle's axis cast at the
        collector), and the jet lights on exactly that vertex. One rule, decided
        once, in the parser -- not a second copy of it here that could disagree.

        Returns -inf -- no gate, byte-identical -- when there is no frame (the
        flat plate), when the lead-in's first vertex is already over the
        collector (the mandrel: `covers` is True on it everywhere), or when
        there is no anchor to read. Those are the comparator.
        """
        is_dep = np.asarray(is_dep, dtype=bool)
        if self._frame is None or not is_dep.size or not is_dep.any():
            return float("-inf")
        if int(np.argmax(is_dep)) <= 0:
            return float("-inf")
        jt = self._jet_t
        if jt is None or not len(jt):
            return float("-inf")
        try:
            v0 = np.asarray(verts, float)[0]
            if self._frame.covers(*v0):
                # `covers` answers a LATERAL question only (its own docstring:
                # "the standoff's business... folding it in here would make a
                # lateral question depend on a vertical tolerance nobody
                # set"). This gate is a PROXIMITY question -- is the lead-in's
                # first vertex actually near the surface, not just within its
                # meridian span -- so the height bound belongs here, off the
                # rig's own standoff, not inside `covers`.
                _u, _v, h = self._frame.from_world(*v0)
                if abs(float(h)) <= self.standoff:
                    return float("-inf")
        except Exception:  # noqa: BLE001 -- a frame that cannot answer gets no gate
            return float("-inf")
        return float(jt[0])

    def _recompute_physics(self):
        """Solve the jet model for the current knobs and adopt its lag. Called at
        startup and after any live parameter change. A no-op (and clears the
        state) when physics is off, so the legacy fixed lag/diameter stay."""
        if not self.physics_on or self.model is None:
            self.jet_state = None
            self.jet_field = None
            return
        self.jet_state = self.model.solve(ProcessParams(
            pressure=self.pressure, voltage=self.voltage,
            speed=self.deposit_speed, temperature=self.temperature))
        self.lag = self.jet_state.lag_time
        # the gap's electric field for the current voltage; built once here (not per
        # frame) and sampled by _jet_arc_points (centreline shape) AND _jet_radii (the
        # cone-jet radius profile, via the potential drop along the jet)
        self.jet_field = self.model.build_field(self.voltage)

    def _reveal_time(self):
        """Print-time the deposition MESH and the jet's bottom are cut at, so the
        mesh's leading edge and the jet end on the same laid-fibre point -- the jet
        never floats ahead of the deposited scaffold. (The commanded toolpath LINES
        use _lines_reveal_time instead -- they are a different geometry; see there.)

        When the loaded fibre is physics-RESHAPED (mew_deposition has already baked
        the jet-lag into the .depo geometry -- the 'Physics-deposited scaffold'
        case), the lag is in the GEOMETRY, so the cut is simply self.t: the laid
        touchdown at time t already trails the nozzle by the lag distance, giving the
        jet its lean, and revealing up to t puts the mesh edge exactly there.
        Revealing at self.t - lag instead would DOUBLE-count the lag and was the
        whole-lag-distance gap behind the jet. When the fibre is the plain commanded
        path (physics-deposition off, or a hand-launched run), the lag is not in the
        geometry, so the (t - lag) cut supplies the lean -- and 0 with the fallback
        frustum (no jet, reveal runs right to the nozzle). At the end the in-flight
        tail has landed, so everything is revealed."""
        if self.t >= self.total:
            return self.t
        if self._reshaped:
            return self.t
        return max(0.0, self.t - self._reveal_lag())

    def _lines_reveal_time(self):
        """Print-time the COMMANDED toolpath lines (gold deposit + grey travel + the
        active segment) are revealed up to. These are the raw commanded path, NOT the
        reshaped fibre, so -- regardless of whether the deposited cache is reshaped --
        they must be cut a lag-time behind the nozzle to end at the jet's bottom (the
        trailing touchdown while printing, the trailing thread while travelling)
        rather than running ahead to the nozzle centre. For a reshaped cache the MESH
        cuts at self.t (its geometry carries the lag) while these lines cut at
        self.t - lag; the two end at the same place because the touchdown at self.t
        IS the commanded point at self.t - lag. 0 with the fallback frustum (no jet);
        self.t at the end, where the in-flight tail has landed."""
        if self.t >= self.total:
            return self.t
        return max(0.0, self.t - self._reveal_lag())

    def _update_deposit_speed(self, seg):
        """Adopt the feedrate of toolpath segment `seg` (scaled by the live
        print-speed knob) as the deposition speed, re-solving the jet model when
        it changes. This is what makes the jet deflection track per-scaffold and
        per-loop speed changes: as the playhead crosses into a region printed at a
        different F, the lag, diameter, and field-shaped arc all update instead of
        staying pinned to the print-wide feed solved at startup. The scale rides
        on top of that segment feed; a what-if speed entered live enters the
        physics HERE, the playback clock (to retime the nozzle -- see _on_tick) and
        the re-lay -- but never the g-code, its timeline (the times array), or the
        deposited-mesh geometry. A no-op when physics is off, and re-solves
        only on an actual change (one comparison per frame otherwise)."""
        if not self.physics_on or self.seg_feeds is None or self.seg_feeds.size == 0:
            return
        idx = min(max(int(seg), 0), self.seg_feeds.size - 1)
        speed = max(float(self.seg_feeds[idx]) / 60.0 * self.print_speed_scale, 1e-6)
        if abs(speed - self.deposit_speed) > 1e-9:
            self.deposit_speed = speed
            self._recompute_physics()

    # physical floors for the live-editable knobs, so a key-mash can't drive a
    # parameter to a value the model can't take (no negative pressure/voltage; the
    # melt stays above a temperature where the Arrhenius viscosity is meaningful)
    _KNOB_FLOORS = {"voltage": 0.0, "pressure": 0.0, "temperature": 40.0}

    def _bump(self, name, delta):
        """Nudge one live physics knob (see _KNOB_FLOORS), re-solve the jet, and (in
        real-time mode) schedule the deposited fibre to re-lay to match."""
        if not self.physics_on:
            return
        floor = self._KNOB_FLOORS[name]
        setattr(self, name, max(floor, getattr(self, name) + delta))
        self._recompute_physics()
        self._note_param_change()

    # print-speed scale bounds: a wide what-if range around the g-code feed, kept
    # off zero so the model never sees a non-positive speed
    _SPEED_SCALE_MIN = 0.1
    _SPEED_SCALE_MAX = 10.0

    def _bump_print_speed(self, factor):
        """Scale the PRINT speed fed to the jet model (live, multiplicative). The
        jet physics (diameter ~v^-0.5, lag/deflection, the v/CTS ratio and regime)
        always respond; in the LOCKED sim that is all it touches (never the g-code,
        its timeline, or the deposited mesh). In real-time mode it ALSO retimes the
        nozzle -- the playback clock advances by this scale, so the head visibly
        speeds up / slows down with the knob (see _on_tick) -- AND schedules the
        deposited fibre to re-lay at the what-if speed (thinner + more whip ahead).
        Re-reads the CURRENT segment's feed so the scale rides on top of whatever
        speed this part of the path is printed at, then re-solves through the one
        speed choke point (_update_deposit_speed)."""
        if not self.physics_on:
            return
        self.print_speed_scale = min(
            max(self.print_speed_scale * factor, self._SPEED_SCALE_MIN),
            self._SPEED_SCALE_MAX)
        seg, _ = self._cursor()
        self._update_deposit_speed(seg)
        self._note_param_change()

    def _jet_radius(self, cache_diameter):
        """Radius (mm) to draw the jet tube at: the physics-predicted fiber
        diameter when the model is on, else the deposited-mesh diameter so the
        jet still meets the fiber it's laying."""
        d = (self.jet_state.fiber_diameter
             if self.jet_state is not None else cache_diameter)
        return max(0.5 * d, 1e-4)

    def _jet_radii(self, arc, tip_world, contact, cache_diameter):
        """Per-point tube radius down the jet -- the cone-jet PROFILE
        r(s) = sqrt(Q / (pi v(s))) (mew_physics): fat at the bore (the Taylor cone),
        necking SMOOTHLY to the fibre as the field accelerates the melt to the landing
        speed. The base is PINNED to the bore (v starts at the bore-exit speed) so it
        never widens with pressure; the transition is gradual, not a step. The jet
        speed at each point follows the fraction of the nozzle->collector POTENTIAL it
        has dropped through (self.jet_field), so the cone is field-coupled. Falls back
        to the constant fibre radius (the legacy thin jet) when physics/field is off,
        and on ANY error -- the cone must never break the jet."""
        n = len(arc)
        r_fiber = self._jet_radius(cache_diameter)
        js, fld = self.jet_state, self.jet_field
        # cone disabled (GUI checkbox) or physics/field unavailable -> the legacy
        # constant-radius thin jet at the fibre diameter
        if not self.cone_on or n == 0 or js is None or self.model is None or fld is None:
            return [r_fiber] * n
        try:
            q = js.flow_rate
            v_land = max(self.deposit_speed, 1e-6)
            r_base = js.cone_base_radius                       # the pinned bore radius
            con_z = float(contact[2])
            tx, ty = float(tip_world[0]), float(tip_world[1])

            def drop_potential(p):                             # phi in the field frame
                return fld.potential((float(p[0]) - tx, float(p[1]) - ty,
                                      float(p[2]) - con_z))
            phi_top = drop_potential(arc[0])                   # at the needle tip
            span = phi_top - drop_potential(arc[-1])           # tip -> touchdown drop
            radii = []
            for i, p in enumerate(arc):
                frac = ((phi_top - drop_potential(p)) / span if abs(span) > 1e-12
                        else (i / (n - 1) if n > 1 else 1.0))
                r = self.model.jet_radius(q, self.model.jet_speed_at(q, v_land, frac))
                radii.append(min(max(r, 1e-4), r_base))        # never exceed the bore
            return radii
        except Exception:  # noqa: BLE001 -- never let the cone profile break the jet
            return [r_fiber] * n

    def _set_jet_geometry(self, arc, tip_world, contact, cache_diameter):
        """Write the drawn arc AND its cone-jet radius profile into the tube, and
        park the nozzle-exit plane that cuts the top face flat. Shared by the
        single-head and machine jet paths.

        The tube carries ONE point more than the arc. That lead point is placed
        BACKWARDS along the arc's first chord, so the first two tube segments are
        collinear and everything from the tip down is swept exactly as it was
        before this existed; then the exit-plane clip (see _build_scene) removes
        the part standing above the bore. The result is a flat horizontal face at
        the nozzle at every lag, with no change to the centreline and no crease.

        The lead has to be long enough that the tube's OWN end cap -- still
        perpendicular to that first chord -- clears the plane, or the clip leaves
        a sliver of it behind: that cap rises r*tan(theta) above its centre, so
        the lead is that plus one more radius of margin. Both scale with the tube,
        so neither is a tuning knob.

        THE CLIP IS SKIPPED when the touchdown is NOT below the tip. A flat
        collector cannot produce that, but a mandrel can, since the contact rides
        a cylinder -- and there the jet climbs, so a plane at the tip keeping what
        is below it would delete the whole jet. Parking the plane above everything
        drawn is the no-op."""
        radii = self._jet_radii(arc, tip_world, contact, cache_diameter)
        n = len(arc)
        p0 = np.asarray(arc[0], float)
        d = np.asarray(arc[1], float) - p0 if n > 1 else np.array([0.0, 0.0, -1.0])
        dn = float(np.linalg.norm(d))
        d = d / dn if dn > 1e-12 else np.array([0.0, 0.0, -1.0])
        r0 = float(radii[0]) if radii else 0.01
        # tan of the first chord's lean off vertical; clamped because a chord that
        # is more horizontal than vertical is outside what this drawing models,
        # and the lead length would otherwise run away
        vz = abs(float(d[2]))
        tan_t = min(float(math.hypot(d[0], d[1])) / vz, 8.0) if vz > 1e-12 else 8.0
        lead = p0 - d * (r0 * (1.0 + tan_t))

        self.jet_pts.SetPoint(0, lead[0], lead[1], lead[2])
        for i, q in enumerate(arc):
            self.jet_pts.SetPoint(i + 1, q[0], q[1], q[2])
        self.jet_pts.Modified()
        self.jet_radii.SetValue(0, r0)              # the lead is a straight bore stub
        for i, r in enumerate(radii):
            self.jet_radii.SetValue(i + 1, r)
        self.jet_radii.Modified()

        exit_z = float(tip_world[2])
        if float(arc[-1][2]) >= exit_z:             # touchdown at or above the tip
            exit_z = max(float(q[2]) for q in arc) + r0 + 1.0   # clip nothing
        self._jet_exit_plane.SetOrigin(0.0, 0.0, exit_z)

    def _jet_arc_points(self, tip_world, contact):
        """The jet centreline from needle tip to fibre contact, returned as
        JET_ARC_SEGMENTS+1 points (tip first, contact last).

        A catenary with its vertex at the TOUCHDOWN: with x measured back toward
        the nozzle, z(x) = a*(cosh(x/a) - 1) over x in [0, lag]. `a` is not a
        knob -- `_catenary_a` solves it from a*(cosh(lag/a) - 1) = drop -- so the
        shape has no tuning constant, the landing tangent is horizontal
        identically (a vertex is horizontal), and the nozzle end falls out at
        atan(sinh(lag/a)) off horizontal -- 89 deg at a 0.2 mm lag over a 3.5 mm
        gap, 78 deg at 2 mm -- rather than being pinned vertical.

        A VISUAL SHAPE MODEL: `a` comes from the drawn geometry, not a jet current
        or a field integral -- never call the curve predicted.
        The gap field's horizontal component (self.jet_field, mew_field) nudges
        the arc sideways through a weight s^2 (1-s)^2, zero WITH zero slope at both
        ends, so the endpoints and the landing tangent survive any field. A uniform
        field (the default) contributes exactly zero; with physics off the jet
        degrades to a straight line. The curve is discrete, so a source parked
        pathologically close to the tip can still lean the first chord.

        Two shapes it cannot draw: the backward-tilting heel at and below CTS (a
        catenary is monotone in x), and a landing tangent to a MANDREL (the vertex
        gives tangency to the horizontal PLANE only)."""
        n = JET_ARC_SEGMENTS
        tip = np.asarray(tip_world, float)
        con = np.asarray(contact, float)
        fld = self.jet_field
        if fld is None:                                  # legacy straight jet
            return [tip + (i / n) * (con - tip) for i in range(n + 1)]

        # field frame: collector (z=0) at the contact plane, tip on +z above it
        def to_field(w):
            return (float(w[0] - tip[0]), float(w[1] - tip[1]), float(w[2] - con[2]))

        drop = float(tip[2] - con[2])                    # vertical gap, mm
        lat = float(math.hypot(con[0] - tip[0], con[1] - tip[1]))   # the lag, mm
        e_ref = fld.voltage / fld.gap if fld.gap > 0.0 else 1.0
        e_ref = e_ref if e_ref > 1e-12 else 1.0

        # -- resample at drop fractions CLUSTERED toward both ends -----------------
        # The radius profile (jet_radius vs height, _jet_radii) is fattest and changes
        # FASTEST at the top (the Taylor cone), and the landing tangent lives at the
        # bottom, while the long middle span barely changes. A cosine cluster puts the
        # sample density where the drawing needs it -- dense cone, dense landing, sparse
        # middle -- so the smooth horn renders smooth instead of collapsing across one
        # fat segment. (The endpoints 0.5*(1-/+1) land exactly on tip and contact.)
        # NB clustering the DRAW POINTS is not a physics choice: the catenary below is
        # the same curve; only where we sample it changes.
        vt = 0.5 * (1.0 - np.cos(math.pi * np.linspace(0.0, 1.0, n + 1)))

        # -- the catenary, as a lateral fraction at each drawn level ---------------
        if drop <= 1e-9 or lat < 1e-9:
            # Nothing to hang. Either the jet is vertical (no lag), or the touchdown
            # is not below the tip -- which the flat collector cannot produce but a
            # MANDREL can, since the contact rides a cylinder. Fall back to the chord.
            # That matches the retired form for the NO-LAG case only: there its eased
            # lateral fraction multiplied a zero lateral delta, so it drew the same
            # vertical line. For a touchdown ABOVE the tip it did something else --
            # it floored the drop at 1e-6 and then affine-mapped its full eased curve
            # into the tip->contact box, so it drew a bow where this draws a chord.
            ut = vt.copy() if drop <= 1e-9 else np.zeros_like(vt)
        else:
            a = _catenary_a(lat, drop)
            z = drop * (1.0 - vt)                         # height above the touchdown
            ut = 1.0 - a * np.arccosh(np.clip(z / a + 1.0, 1.0, None)) / lat
            ut[0], ut[-1] = 0.0, 1.0                      # pin both endpoints exactly

        pts = []
        for i in range(n + 1):
            p = np.array([
                tip[0] + ut[i] * (con[0] - tip[0]),
                tip[1] + ut[i] * (con[1] - tip[1]),
                tip[2] + vt[i] * (con[2] - tip[2]),
            ])
            # lateral nudge along the horizontal field; vt^2 (1-vt)^2 is zero with zero
            # SLOPE at both ends, so it pins the endpoints and leaves both tangent
            # BCs intact (its peak is 1/16, vs 1/4 for a plain vt(1-vt), so this gain
            # reads weaker than it did -- it is a free visual knob, anchored to nothing)
            ex, ey, _ = fld.field(to_field(p))
            w = vt[i] * vt[i] * (1.0 - vt[i]) * (1.0 - vt[i])
            # abs(drop): this is a LENGTH SCALE for the nudge, not a direction. The
            # retired form could write `drop` because it floored it at 1e-6; this one
            # does not floor it, and a MANDREL can put the touchdown above the tip, so
            # a signed drop would push the arc against the field instead of along it.
            bump = JET_NUDGE_GAIN * abs(drop) * w / e_ref
            p[0] += bump * ex
            p[1] += bump * ey
            pts.append(p)
        return pts

    def _place_head_and_jet(self, head):
        """Park the printhead so its needle tip sits at the commanded point lifted
        by the standoff, then stretch the jet from that tip down to the trailing
        fiber contact. The nozzle tip floats one standoff above the plane WHETHER OR
        NOT a printhead body is drawn: with the real printhead the tip leads at a
        fixed height and the jet does the leaning; with NO printhead asset (the
        default no-machine-model view -- the printer model toggled off, or the head
        ticked Hidden) the tip is invisible but the bare jet + deposited fibre are
        still shown."""
        if self.machine is not None:
            self._place_machine(head)        # multi-body machine + bed-borne jet
            return
        # Legacy frustum marker: it rides ON the deposition path, so there is no
        # floating tip and the (zero-length) jet is not drawn. The current build
        # removed that marker (head_actor is None with no printhead asset), so this
        # guard only fires if a path-riding marker is ever reintroduced.
        if self.head_actor is not None and not self._has_printhead:
            self.head_actor.SetPosition(*head)
            self.jet_actor.SetVisibility(False)
            return
        # The nozzle tip floats one standoff above the commanded point. Position the
        # real printhead so its tip sits there; with no printhead asset the tip is
        # just the (invisible) jet anchor -- the jet still leans from it to the fibre,
        # so the sim is visible even when no machine/printhead is shown.
        # a mandrel with no printer drawn: the collector still has to be seated
        # and spun, and the jet still has to meet the fibre where it is DRAWN
        if self._frame is not None:
            seat = self._collector_seat()
            self._place_collector(seat, seat, self._rot_deg, True)
        tip_world = np.array([head[0], head[1], head[2] + self.standoff])
        if self._has_printhead:
            # world = scale*local + position (no rotation), so position places
            # the scaled tip exactly on tip_world
            pos = tip_world - self.head_scale * self._head_tip
            self.head_actor.SetPosition(pos[0], pos[1], pos[2])
        # the jet runs while printing; once parked at the end the print is
        # done and the jet is extinguished (no dangling thread)
        if self._jet_t is not None and self.t < self.total and self.t >= self._jet_on:
            # The jet's bottom is the deposited fibre's growing tip while
            # printing, and trails the nozzle while travelling -- see
            # _jet_contact. Anchoring it to the SAME laid centreline (the .depo
            # cache) the mesh reveals to means the jet meets the fibre instead of
            # floating off a separately-integrated path (which trailed the mesh
            # by a whole lag distance). With physics-deposition on, the cache
            # already carries the jet-lag, so _reveal_time cuts at self.t and the
            # jet leans because the laid touchdown trails the nozzle; without it
            # the cache is the commanded path and the (t - lag) cut supplies the
            # lean. z comes from the cache too (the LANDED height), so the jet
            # meets the fibre where it actually rests.
            contact, cdiam = self._jet_contact()
            # the contact is a point ON the drawn fibre, so it takes the same seat
            # and spin the fibre's actor does -- measured off the geometry, it
            # would otherwise anchor the jet to an unspun point
            if self._frame is not None:
                contact = self._to_scene(contact, self._collector_seat(),
                                         self._rot_deg)
            arc = self._jet_arc_points(tip_world, contact)
            # points + jet thickness (the cone-jet PROFILE, fat cone at the bore ->
            # fibre) + the nozzle-exit plane that cuts the top face flat
            self._set_jet_geometry(arc, tip_world, contact, cdiam)
            self.jet_actor.SetVisibility(self._jet_visible)
        else:
            self.jet_actor.SetVisibility(False)

    # -- overlay placement -------------------------------------------------- #
    # -- the mandrel collector ---------------------------------------------- #
    def _mandrel_axial_bounds(self):
        """Axial extent of the DEPOSITED tube, excluding approach/exit travels.

        ``parse_path`` deliberately keeps the lead-in from machine origin to the
        first print move. That travel belongs in the animation, but not in the
        collector's length/centre calculation: a tube beginning at X=40 would
        otherwise centre its mandrel over the whole 0..X execution span and draw
        the rod 20 mm away from the fibre. ``dep_idx`` identifies deposition
        segments, so their two endpoint sets are the actual printed extent.
        """
        dep_idx = getattr(self, "dep_idx", None)
        if dep_idx is not None and len(dep_idx):
            idx = np.unique(np.concatenate([dep_idx, dep_idx + 1]))
            x = self.verts[idx, 0]
        else:
            x = self.verts[:, 0]
        return float(x.min()), float(x.max())

    def _mandrel_body(self):
        """A cylinder actor for the collector, in the BUILD GROUP's own frame.

        Drawn rather than loaded: a mandrel is a turned rod, so a machine.json
        would be carrying an STL of a cylinder. It is still PLACED by the model
        (`machine_model.RotaryAxis.body`) rather than decorated onto the scene --
        change the radius and the body, the seat and the geometry all move
        together.

        The axis alignment is BAKED INTO THE POLYDATA, not left on the actor.
        vtkCylinderSource builds along +Y, and an actor whose orientation is
        already spent aligning the body has none left for the mandrel's own
        rotation -- which is the whole point of the actor.

        `self._mandrel_len` is the ROD, from `gui_mandrel_preview.axial_layout`
        by way of `--mandrel-length` -- the same number the designer's own rod
        actor is built from, and the machine page's declared rod when there is
        one. It is NOT the design's `length`, which is the
        cell tiling: a wavy family runs an amplitude past each end, and a rod
        cut to `length` left the tube hanging off the collector it is wound on
        (0.5 mm each end at the designer's chevron defaults). Because the span matches the print, the
        centring below and `machine_model.RotaryAxis.body`'s anchoring at the
        print start land in the same place."""
        if self._frame is None:
            return None
        # The frame says what solid it is, rather than this code assuming a
        # radius attribute exists: a collector that is not a cylinder gets no
        # cylinder actor instead of a plausible wrong one.
        body = self._frame.body_spec()
        if body is None:
            return None
        if body.get("kind") == "revolution":
            # A CURVED COLLECTOR, drawn by the frame itself. `stl_viewer` owns the
            # builder because it owns every other shared actor, and it queries
            # `to_world` for each vertex -- so the surface under the fibre and the
            # surface the fibre was laid on are one surface, not two descriptions
            # of one. It is in the collector's own frame, exactly as the rod is.
            import stl_viewer
            return stl_viewer.revolution_actor(self._frame)
        if body.get("kind") != "cylinder":
            return None
        start, length = self._mandrel_rod()
        r = body["radius"]
        src = vtk.vtkCylinderSource()
        src.SetRadius(r)
        src.SetHeight(length)
        src.SetResolution(64)
        src.CappingOn()
        xf = vtk.vtkTransform()
        xf.Translate(start + 0.5 * length, 0.0, 0.0)
        xf.RotateZ(-90.0)                      # +Y -> +X, baked into the points
        tf = vtk.vtkTransformPolyDataFilter()
        tf.SetInputConnection(src.GetOutputPort())
        tf.SetTransform(xf)
        tf.Update()
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(tf.GetOutput())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        p = actor.GetProperty()
        p.SetColor(*PLATE_COLOR)
        # matte, exactly as the build plate is: a specular band sweeping a
        # cylinder as the camera orbits reads as the mandrel changing colour
        p.SetSpecular(0.0)
        p.SetAmbient(0.55)
        p.SetDiffuse(0.45)
        return actor

    def _mandrel_rod(self):
        """`(start, length)` of the drawn collector in g-code X -- the ONE
        definition, so the body actor and the machine-model seat cannot disagree.

        `--mandrel-start` / `--mandrel-length` are the designer's own ROD
        (`gui_mandrel_preview.axial_layout`), which is what carries its
        PLACEMENT: the print sits somewhere ON the rod, and a rod derived from
        the printed extent would show none of the bare rod either side. Without the flags, fall back to the
        plain rule -- centre the given length on the printed extent, or span
        the print with 15 % over -- so a hand-launched player is unchanged."""
        x0, x1 = self._mandrel_axial_bounds()
        span = x1 - x0
        length = self._mandrel_len
        if not length:
            # no length given: span the print, with a little either side so the
            # tube does not appear to run off the end of its own collector
            length = max(1.0, span * 1.15)
        # getattr, like `_mandrel_axial_bounds` above: the collector-length suite
        # builds a Player through __new__ to compare the drawn rod with the
        # designer's, so the attribute may legitimately not be set.
        u0 = getattr(self, "_mandrel_u0", None)
        if u0 is not None:
            return float(u0), length
        return x0 - 0.5 * (length - span), length

    def _mandrel_start(self):
        """Where the drawn collector's near end sits along the mandrel axis, mm --
        the same number `_mandrel_body` builds the rod from."""
        if self._frame is None:
            return 0.0
        return self._mandrel_rod()[0]

    def _collector_seat(self):
        """The base translation of everything on the collector, with NO printer.

        `-R0` along the nozzle normal. The wrapped tube measures from the mandrel
        AXIS; the standoff, the origin triad and every clearance check in this
        program measure from a SURFACE. With a machine model the identical shift
        arrives inside `bed_group` (machine_model.RotaryAxis.seat), so the two
        paths agree by construction rather than by two copies of a constant."""
        if self._frame is None:
            return np.zeros(3)
        return self._frame.seat_offset()

    def _collector_surface_height(self):
        """How far the collector's deposition surface stands above its own AXIS
        datum, mm -- `R0` on a mandrel, `rho(u_mid)` on a surface of revolution.

        The FRAME answers this, never the joint. A `RotaryAxis.radius` is a rod's
        radius and a curved collector has none; asking the joint would either
        return 0 (the surface sinks onto the axis and the print runs through the
        collector) or a number the machine page invented. It is spelled as the
        negated `seat_offset` z rather than as a radius so the bed-contact term
        and the build-group seat cannot carry two copies of the same constant --
        the same rule `_collector_seat` follows, and on a `CylinderFrame` it is
        `R0` reached through the identical float.

        ZERO ON A PAIR, and that is the same statement `place_scene` makes by
        not seating one. A pair's collector datum is the point its axis of
        revolution is bolted at -- the pick on the spinning body -- and the
        exported file already measures from the collector's own origin, which is
        ON that axis (`machine_model.five_axis_command` takes the seat term only
        when there is one joint). So there is no radius standing between the
        datum and the deposition surface to add here; how far the collector's
        own geometry reaches above its mount is the profile's business and comes
        out of `to_world`, not out of this term."""
        if self._frame is None:
            return 0.0
        if self.machine is not None and self.machine.rotary_count > 1:
            return 0.0
        return -float(self._frame.seat_offset()[2])

    def _rotary_body_matrices(self, sc):
        """`(actor, vtkMatrix4x4)` for every rotary body this frame.

        `place_scene` reports one chain of (angle, axis, pivot) triples per
        joint, in that body's own STL frame scaled by the profile's `scale`, and
        a body is turned by the joints UNDER it -- so the base joint's chain is
        empty and its body never moves, which is byte-for-byte what a mandrel's
        holder always did. The translation is the part the body is bolted to,
        and it is the BASE joint's `role` that says which, because that is the
        one actually bolted to a body.

        A chain is composed through `machine_model.compose_rotations` rather
        than turned into an actor orientation: two stacked joints with two
        different pivots do not reduce to three Euler angles about one origin,
        which is the same reason `_collector_pose` drives a user matrix."""
        chains = sc.get("rotary_bodies") or ()
        if not chains or not self.rotary_actors:
            return ()
        rot = self.machine.rotaries
        base = sc["static" if (rot and rot[-1].role == "frame") else "bed"]
        out = []
        for actor, triples in zip(self.rotary_actors, chains):
            if actor is None:
                continue
            out.append((actor,
                        self._user_matrix(machine_model.compose_rotations(triples),
                                          base)))
        return tuple(out)

    def _mount_axial(self):
        """The COLLECTOR's own axial coordinate at its bottom edge, or None.

        The frame owns the number (`mew_frame.DepositionFrame.mount_axial`); this
        is only the place the player asks for it, so the pose chain, the seating
        block and the read-out cannot each ask a different way. None with no
        frame, and None on a frame that does not stand on a mount -- a plane or a
        cylinder -- which is what keeps every three- and four-axis scene on the
        path it was on."""
        if self._frame is None:
            return None
        return self._frame.mount_axial()

    def _collector_pose(self, rot_deg):
        """`(R, t)` carrying a point in the COLLECTOR's frame into the build
        group's, for a five-axis pair -- or None on a mandrel or a flat scene.

        `machine_model.rotary_transform` is the one chain: the generator wrote
        the file through it, `gcode_to_fibers.FiveAxisReader` inverted it to get
        these vertices back, and this turns it forward again to draw them. A
        second rotation composed here would be a third copy of the kinematics
        free to disagree with both.

        A PAIR cannot be an actor orientation. `SetOrientation` is three Euler
        angles about the actor's own origin, and two joints with two different
        pivots do not reduce to that -- which is exactly why `place_scene`
        reports `collector_rotations` as a list of (angle, axis, pivot) triples
        rather than one triple. So the pair path drives a 4x4 user matrix and
        leaves position and orientation alone; the mandrel keeps its single
        `SetOrientation` about +x, untouched."""
        if self._five is None or not self._five_rot:
            return None
        angles = machine_model._rotary_angles(self._five_rot, rot_deg)
        return machine_model.rotary_transform(self._five_rot, angles,
                                              self._mount_axial())

    @staticmethod
    def _user_matrix(pose, base):
        """A `vtkMatrix4x4` for `p -> R @ p + t + base`."""
        r, t = pose
        m = vtk.vtkMatrix4x4()
        off = np.asarray(t, dtype=np.float64) + np.asarray(base, dtype=np.float64)
        for i in range(3):
            for j in range(3):
                m.SetElement(i, j, float(r[i, j]))
            m.SetElement(i, 3, float(off[i]))
        return m

    def _collector_spin(self, rot_deg):
        """The actor orientation for a mandrel reading `rot_deg`, degrees.

        `mew_frame.to_world` puts a point laid at reading A at body azimuth A, and
        a right-hand rotation about +x maps azimuth psi -> psi - theta -- so
        spinning the group by +A brings the freshly laid fibre back under the
        nozzle, which is where the jet just put it. Derived in
        `machine_model.RotaryAxis`, and asserted there against an independent
        rotation."""
        return (float(rot_deg), 0.0, 0.0)

    def _place_collector(self, prop_base, reveal_base, rot_deg, positions):
        """Seat and spin every actor that rides the collector.

        The deposited fibre, the toolpath and the mandrel are all STATIC in the
        collector's own frame; what moves is the collector. So this is ONE
        SetOrientation per actor instead of rewriting point arrays every tick --
        which is why the `.depo` cache needs no rotary format.

        Two bases, because the two groups have always had two: PHYSICAL props on
        the collector take the plain delta, while FILE-frame reveal geometry also
        takes the machine-live work offset. They share one ROTATION.

        `positions` is False under a machine model, which has already set every
        actor's position from its own kinematics -- this then adds the spin and
        nothing else. It is True with no printer drawn, where nothing else places
        these actors at all: leaving it out is what left the tube centred on the
        g-code origin while the mandrel sat a radius below it, so the fibre ran
        through the middle of its own collector."""
        pose = self._collector_pose(rot_deg)
        if pose is not None:
            # A FIVE-AXIS PAIR: the whole placement rides one user matrix, and
            # position/orientation are cleared so the two cannot both apply. See
            # `_collector_pose` for why a pair is not an actor orientation.
            props = self._user_matrix(pose, prop_base)
            reveal = self._user_matrix(pose, reveal_base)
            groups = ((self._bed_actors, props), (self._reveal_actors, reveal),
                      (self.depo_actors, reveal))
            if self.mandrel_actor is not None:
                groups = groups + (((self.mandrel_actor,), props),)
            for actors, m in groups:
                for a in actors:
                    a.SetPosition(0.0, 0.0, 0.0)
                    a.SetOrientation(0.0, 0.0, 0.0)
                    a.SetUserMatrix(m)
            return
        ori = self._collector_spin(rot_deg)
        for a in self._bed_actors:
            if positions:
                a.SetPosition(*prop_base)
            a.SetOrientation(*ori)
        if self.mandrel_actor is not None:
            self.mandrel_actor.SetPosition(*prop_base)
            self.mandrel_actor.SetOrientation(*ori)
        for a in self._reveal_actors:
            if positions:
                a.SetPosition(*reveal_base)
            a.SetOrientation(*ori)
        for a in self.depo_actors:
            if positions:
                a.SetPosition(*reveal_base)
            a.SetOrientation(*ori)

    def _to_scene(self, pts, base, rot_deg):
        """Collector-frame point(s) -> where the actors actually draw them.

        Rotate about the mandrel axis, then translate -- the same order a VTK
        actor composes SetOrientation and SetPosition, so anything MEASURED off
        the geometry (the jet's contact, a HUD readout, the radial clearance
        check) lands exactly where the fibre is drawn. Without this the jet
        anchors to an unspun point and trails the tube by up to a diameter."""
        p = np.atleast_2d(np.asarray(pts, dtype=np.float64))
        pose = self._collector_pose(rot_deg)
        if pose is not None:
            # the pair, through the SAME chain the actors are drawn by, so a
            # measured point and a drawn one cannot land in different places
            r, t = pose
            out = p @ np.asarray(r, dtype=np.float64).T \
                + np.asarray(t, dtype=np.float64) \
                + np.asarray(base, dtype=np.float64)
            return out if np.ndim(pts) > 1 else out[0]
        if self._frame is not None and rot_deg:
            th = math.radians(rot_deg)
            c, s = math.cos(th), math.sin(th)
            y = p[:, 1] * c - p[:, 2] * s
            z = p[:, 1] * s + p[:, 2] * c
            p = np.column_stack([p[:, 0], y, z])
        out = p + np.asarray(base, dtype=np.float64)
        return out if np.ndim(pts) > 1 else out[0]

    def _rotary_at(self, seg, frac):
        """(machine point, rotary reading) at the playhead. (None, 0.0) when flat.

        The reading is a FLOAT on a mandrel's one joint, exactly as it always
        was, and an (J,) array on a five-axis pair -- collector-first, the shape
        `machine_model.place_scene` takes as `rotary_deg` and `_rotary_angles`
        already accepts either."""
        if self._rotary is None:
            return None, 0.0
        mach, rot = self._rotary
        j = min(seg, len(mach) - 2)
        m = mach[j] + frac * (mach[j + 1] - mach[j])
        a = rot[j] + frac * (rot[j + 1] - rot[j])
        return m, (np.asarray(a, dtype=np.float64) if rot.ndim > 1 else float(a))

    def _rotary_text(self, rot):
        """The rotary readout: one word per joint, named as the RIG names it.

        `A` was hardcoded here, which is right on every shipped mandrel and wrong
        the moment a rig declares anything else -- and a five-axis pair has two
        readings, so the old format string raised on the array outright rather
        than printing the wrong letter. The words come from the joints, the same
        rule the emitter and the parser follow."""
        a = np.atleast_1d(np.asarray(rot, dtype=np.float64))
        joints = self._five_rot or (self.machine.rotaries if self.machine else ())
        words = [j.word.upper() for j in joints][:len(a)]
        while len(words) < len(a):
            words.append("A")
        return "  ".join("%s %s" % (w, format_rotary(v, " deg"))
                         for w, v in zip(words, a))

    #: Peak bytes per tube triangle from `_tube_via_filter` through
    #: `_install_depo_mesh`, `_reveal_deposition` and the first `Render()`.
    #: The peak is the END state, not a moment inside the build: the int32
    #: sorted connectivity (12 B), float32 points + normals (~12 B), float32
    #: times (4 B), and the GPU upload, which the driver also charges to the
    #: process's RAM (~50 B on an NVIDIA laptop; on an integrated GPU it IS
    #: RAM). Measured 85 B on the 40-layer standard scaffold at 12 sides
    #: (6.0 M triangles) through the real methods, 137 B before the int32 /
    #: float32 / shared-view change. 100 = that measurement plus ~15 % for a
    #: driver that keeps more than this one does; the half-of-free rule in
    #: _tube_sides_that_fit is the headroom for the rest of the app.
    TUBE_TRI_BYTES = 100

    def _tube_sides_that_fit(self, dpaths, sides):
        """The finest tube that fits in half the free memory: the sides asked
        for, then 6 ("Fibers (fast)"), then 0 (centrelines, "Lines"). A tube
        of n points and s sides is ~2*n*s triangles. A 150-layer 10 mm print
        at Fibers (smooth) is 23 M triangles, more than 4 GB, and VTK aborts
        the viewer rather than raise when an allocation fails. A step down
        says so in the status line; the design is never thinned."""
        free = free_memory_bytes()
        if free is None:
            return sides
        n = sum(len(p) for p in dpaths)

        def name(s):
            # The Display quality box's own words. Smooth is the design's facet
            # count (often 12, any count above Fast's 6), so it is named by
            # range rather than by one number.
            if s == 0:
                return "Lines (instant)"
            if s > 6:
                return "Fibers (smooth)"
            if s == 6:
                return "Fibers (fast)"
            return "simplified fibers (%d-sided)" % s
        # 3 sides before Lines: still a lit, solid fiber at a quarter of
        # Smooth's memory. Lines are unlit one-pixel wires, and on a many-layer
        # mandrel they merge into one flat blue tube with no fiber to see.
        for s in [sides] + [x for x in (6, 3) if x < sides] + [0]:
            if s == 0 or 2 * n * s * self.TUBE_TRI_BYTES <= free // 2:
                break
        if s != sides:
            asked = name(sides)
            self._note_host(
                "Large design: showing %s. %s needs more free memory than "
                "this computer has now." % (name(s), asked))
            # ...and what is drawn, for the sidebar: its Display quality box
            # still reads the quality asked for, and the status line above is
            # overwritten by the next thing any page says
            embed = getattr(self, "embed", None)
            if embed is not None:
                try:
                    embed.send({"t": "render_used", "asked": asked,
                                "shown": name(s)})
                except Exception:  # noqa: BLE001
                    pass
        return s

    def _note_host(self, text):
        """One line for the app's status line (embedded), or stderr."""
        embed = getattr(self, "embed", None)
        if embed is not None:
            try:
                embed.send({"t": "note", "text": text})
                return
            except Exception:  # noqa: BLE001
                pass
        sys.stderr.write("gcode_player: %s\n" % text)

    def _rotary_short(self, rot):
        """The rotary words for the rail's Position row: degrees with a ° sign,
        as whole turns plus the angle within the turn (`format_rotary`). A
        mandrel's A is a running total, and the raw 162000° it reached by the
        end of a print told a reader nothing about where on the rod the
        nozzle was. The Move row above keeps the g-code word as written."""
        a = np.atleast_1d(np.asarray(rot, dtype=np.float64))
        joints = self._five_rot or (self.machine.rotaries if self.machine else ())
        words = [j.word.upper() for j in joints][:len(a)]
        while len(words) < len(a):
            words.append("A")
        return " ".join("%s %s" % (w, format_rotary(v)) for w, v in zip(words, a))

    def _place_overlays(self, *_):
        # Themed: the chrome owns ALL 2D placement (HUD, buttons, keys) via its
        # own layout()/ConfigureEvent hook -- nothing to do here. Legacy: the
        # floating HUD/help/buttons are hand-placed over the render window.
        if self._themed and self._chrome is not None:
            return
        _w, h = self.window.GetSize()
        self.hud.SetDisplayPosition(14, h - 28)
        if self.help is not None:
            self.help.SetDisplayPosition(14, 10)
        for idx, (b, _fn, _cb) in enumerate(self.buttons):
            b.SetDisplayPosition(14, h - 70 - idx * 30)

    def _refresh_buttons(self):
        for b, label_fn, _cb in self.buttons:
            b.SetInput(f" {label_fn()} ")

    # -- HUD/state ---------------------------------------------------------- #
    def _diam_is_dynamic(self):
        """Whether the fibre's diameter is physics-driven (mass conservation, the
        jet's own) rather than the hand-typed uniform one. Live-adjust is told by
        `--dynamic-diameter`; a locked run by the diameter-vs-time lookup the
        physics export wrote into the .depo -- the export carries it exactly when
        the GUI's diameter is physics-driven, so a manual override or a plain
        geometric export leaves it out. Looked up once."""
        if self.dynamic_diameter:
            return True
        if getattr(self, "_depo_dynamic", None) is None:
            try:
                self._depo_dynamic = load_depo_diam_by_time(
                    self._find_depo_cache()) is not None
            except Exception:  # noqa: BLE001 -- a readout never raises
                self._depo_dynamic = False
        return self._depo_dynamic

    def _readout_q(self, quantity, value, canonical):
        """`value` (in `canonical`) as the readout prints it: in the unit the
        user chose on the Settings panel (gui_prefs, the same store gui_units
        reads), three significant figures, with its unit. The Design fields
        show speed in mm/min by default, so a readout in the player's own mm/s
        read as a different number from the one the user typed. Read once per
        player: a unit change applies the next time the Simulation opens."""
        units = getattr(self, "_disp_units", None)
        if units is None:
            units = {}
            try:
                import gui_units
                for q in ("speed", "pressure", "fiber_d"):
                    units[q] = (gui_units.unit(q), gui_units.QUANTITIES[q])
            except Exception:  # noqa: BLE001 -- no prefs: keep the canonical unit
                units = {}
            self._disp_units = units
        u, table = units.get(quantity, (canonical, None))
        x = float(value)
        if table and canonical in table and u in table:
            x *= table[canonical] / table[u]
        else:
            u = canonical
        if abs(x) >= 100:     # ungrouped, as the g-code writes F1200
            s = "%.0f" % x
        else:
            s = "%.3g" % x
            if "e" in s:
                s = ("%.6f" % x).rstrip("0").rstrip(".")
        return s, u

    def _fiber_diameter_um(self, js):
        """The diameter readout (um): the fibre's CURRENT diameter -- the jet's
        live value when physics sets it, the typed diameter when it does not."""
        if self._diam_is_dynamic():
            return js.fiber_diameter * 1000.0
        return float(self.fiber_opts.get("diameter", 0.02)) * 1000.0

    def _refresh_hud(self):
        # machine-live has no HUD at all (the Machine-link page's form owns the
        # readout), so there is nothing to refresh -- and self.hud is None
        if self.hud is None:
            return
        # While the GUI is computing the deviation sidecar (seconds), keep a status
        # in the HUD -- otherwise the per-tick readout below overwrites it in 16 ms
        # and the user sees no feedback while "Show deviation" churns.
        if self._dev_computing:
            self.hud.SetInput("  Computing deviation — running the analysis…  ")
            return
        # Interpolated playhead Z -- the head's ACTUAL height at self.t, matching
        # what _apply_time places the nozzle at. Reading the segment END vertex
        # (verts[seg+1, 2]) jumped the readout to the target height the instant a
        # lead-in / inter-layer Z move began, so the HUD showed the next layer's Z
        # while the head was still climbing to it. Interpolate along the segment.
        rot_txt = None
        if self.n_pts > 1:
            seg, frac = self._cursor()
            head = self.verts[seg] + frac * (self.verts[seg + 1]
                                             - self.verts[seg])
            # On a mandrel `self.verts` are the DRAWN fibre, in the collector's own
            # frame -- they spin. The readout must say where the MACHINE is, which
            # is a different point: X and Z, plus the rotary word, and no Y at all.
            # Showing the collector-frame point instead reads as the nozzle
            # wandering in Y while the printer holds it dead still.
            mhead, rot = self._rotary_at(seg, frac)
            if mhead is not None:
                head = mhead
                rot_txt = self._rotary_text(rot)
                rot_short = self._rotary_short(rot)
            x, y, z = float(head[0]), float(head[1]), float(head[2])
            line_no, cmd = self._cursor_line(seg)
        else:
            x = y = z = 0.0
            line_no, cmd = 0, ""
        state = "PLAY" if self.playing else "PAUSE"
        js = self.jet_state
        scale = ""
        vtxt = None
        if js is not None:
            d_um = self._fiber_diameter_um(js)
            # flag the print-speed scale only when the user has moved it off 1.0,
            # so the displayed v is recognisably a what-if rather than the feed
            scale = (f" x{self.print_speed_scale:.2f}"
                     if abs(self.print_speed_scale - 1.0) > 1e-9 else "")
            # ACTUAL accel-limited speed at the playhead (speed_at) vs the SET
            # commanded feed: the actual dips below the set value at ramps / corners /
            # tight turnaround arcs, and HOW far/often it dips is max_accel at work --
            # more at low accel, ~never at high accel. BOTH ride the live print-speed
            # what-if: speed_at is planned in the COMMANDED frame (it never sees the
            # [ / ] knob), so scale it by print_speed_scale to read the genuinely-
            # retimed head speed -- the same scale the playback clock (_on_tick) and
            # the SET feed (deposit_speed) already carry. With kinematics off there
            # is no accel limit, so just show the (already-scaled) commanded feed.
            traj = getattr(self, "_traj", None)
            if traj is not None:
                v_act = traj.speed_at(self.t) * self.print_speed_scale
                vtxt = f"v(act/set) {v_act:.1f}/{self.deposit_speed:.1f} mm/s{scale}"
            else:
                vtxt = f"v {self.deposit_speed:.1f} mm/s{scale}"

        if self._themed and self._chrome is not None:
            # Vertical, sidebar-width readout: one field per line so it fits the
            # ~240 px strip (the horizontal one-liner would run off the edge). The
            # g-code line the frame is executing + its command text + the live
            # nozzle position (the commanded toolpath point at self.t).
            lines = [f"{'▶' if self.playing else '❙❙'}  {state}",
                     f"t   {self.t:.1f}s / {format_duration(self.total)}",
                     f"speed  x{self.speed:g}"]
            if line_no:
                lines.append(f"line {line_no}")
                if cmd:
                    lines.append(f"  {cmd[:26]}")
            if rot_txt is not None:
                lines.append(f"x {x:.2f}  z {z:.3f}  {rot_txt}")
            else:
                lines.append(f"xyz {x:.2f} {y:.2f} {z:.2f}")
            if js is not None:
                lines += [
                    "",
                    f"V {self.voltage:.1f} kV   P {self.pressure / 6894.76:.1f} psi",
                    f"T {self.temperature:.0f} C",
                    vtxt,
                    f"d {d_um:.0f} um",
                    f"CTS {js.critical_speed:.1f}   v/CTS {js.speed_ratio:.2f}",
                    f"lag {js.lag_angle_deg:.0f} deg   [{js.regime.value}]"]
            text = "\n".join(lines)
            self.hud.SetInput(text)
            # the same fields as labelled rows, for the embedded rail's table
            rows = [{"g": "Print", "k": "Time",
                     "v": f"{format_duration(self.t)} / "
                          f"{format_duration(self.total)}"}]
            if line_no:
                rows.append({"g": "Print", "k": "Line", "v": str(line_no)})
                if cmd:
                    rows.append({"g": "Print", "k": "Move", "v": cmd[:22]})
            rows.append({"g": "Print", "k": "Position",
                         "v": (f"X {x:.1f} Z {z:.2f} {rot_short}"
                               if rot_txt is not None
                               else f"{x:.1f}, {y:.1f}, {z:.2f}")})
            if js is not None:
                traj = getattr(self, "_traj", None)
                q = self._readout_q
                set_s, su = q("speed", self.deposit_speed, "mm/s")
                spd = (f"{q('speed', traj.speed_at(self.t) * self.print_speed_scale, 'mm/s')[0]}"
                       f" / {set_s} {su}" if traj is not None
                       else f"{set_s} {su}")
                cts_s, _cu = q("speed", js.critical_speed, "mm/s")
                rows += [
                    {"g": "Process", "k": "Voltage",
                     "v": f"{self.voltage:.1f} kV", "knob": "voltage"},
                    {"g": "Process", "k": "Pressure",
                     "v": "%s %s" % q("pressure", self.pressure / 6894.76, "psi"),
                     "knob": "pressure"},
                    {"g": "Process", "k": "Temperature",
                     "v": f"{self.temperature:.0f} °C", "knob": "temperature"},
                    {"g": "Process", "k": "Speed", "v": spd, "knob": "speed"},
                    {"g": "", "k": "Diameter",
                     "v": "%s %s" % q("fiber_d", d_um / 1000.0, "mm")},
                    {"g": "", "k": "CTS",
                     "v": f"{cts_s} {_cu} · v/CTS {js.speed_ratio:.2f}"},
                    {"g": "", "k": "Jet lag", "v": f"{js.lag_angle_deg:.0f}°"}]
            self._readout_rows, self._readout_text = rows, text
            if self._dev_error:
                # a HUD that differs from the readout text is the rail's status
                # line (EmbedChrome.readout), so the rows keep updating under it
                self.hud.SetInput(f"  {self._dev_error}  ")
            return

        # legacy: the compact overlay pinned to the render window
        linestr = (f"   line {line_no}: {cmd[:40]}" if line_no else "")
        txt = (f" {state}   t={self.t:6.2f}s / {format_duration(self.total)}   "
               f"speed x{self.speed:g}   xyz=({x:.2f},{y:.2f},{z:.2f}){linestr} ")
        if js is not None:
            txt += ("\n"
                    f" V={self.voltage:.1f}kV  P={self.pressure / 6894.76:.1f}psi  "
                    f"T={self.temperature:.0f}C  {vtxt} "
                    f"|  d={d_um:.0f}um  "
                    f"CTS={js.critical_speed:.1f}  v/CTS={js.speed_ratio:.2f}  "
                    f"lag={js.lag_angle_deg:.0f}deg  [{js.regime.value}] ")
        if self._dev_error:
            txt = f" {self._dev_error}\n{txt}"
        self.hud.SetInput(txt)

    # -- controls ----------------------------------------------------------- #
    def _reveal_sim_tool(self):
        """First time the user animates out of the --static default (play / scrub),
        reveal the printhead + jet (+ machine if a profile is loaded, else the nozzle
        marker) + travels so the running simulation is actually visible. Runs ONCE;
        after that the user's own Hide/Show toggles are respected. No-op for a plain
        (non-static) launch or after the first reveal."""
        if not self._static or getattr(self, "_static_revealed", False):
            return
        self._static_revealed = True
        self._head_visible = True
        self._jet_visible = True
        self._travels_on = True
        # `or self._deposit_on`: the G-code-lines switch owns both line kinds now,
        # and in --static it can be flipped BEFORE the first play. Re-reading the
        # launch flag alone would silently drop the travel half of a switch the
        # user has already turned on.
        self._travel_lines_on = bool(self.travel_gcode_line) or self._deposit_on
        # INSTEAD OF, not on top of: the point of the overlay is to see the
        # commanded G0 path, and a tube drawn along the same path hides it.
        # Either can still be flipped back on from the sidebar afterwards.
        if self.travel_gcode_line:
            self._travels_on = False
        if self.machine is not None:
            self._machine_on = True
            self._apply_machine_visibility()
        else:
            if self.head_actor is not None:
                self.head_actor.SetVisibility(True)
        # The opening framing fitted the clip range to what was VISIBLE then,
        # the print alone. The head now shown is ~110 mm tall over a print a
        # few tens of mm across, so its top sat behind the camera and the near
        # plane cut it open: Play "put the camera inside the printhead".
        fit_clipping_range(self.renderer)
        self._refresh_buttons()

    def _toggle_play(self):
        self.playing = not self.playing
        if self.playing:
            self._reveal_sim_tool()    # first play out of --static reveals the tool
            if self.t >= self.total:
                self.t = 0.0           # play from the start if parked at the end
        self._last_wall = time.perf_counter()

    def _slower(self):
        self.speed = max(0.1, round(self.speed / 2.0, 4))

    def _faster(self):
        self.speed = min(64.0, round(self.speed * 2.0, 4))

    def _restart(self):
        self._reveal_sim_tool()
        self.t = 0.0
        self._last_wall = time.perf_counter()

    #: the lines the listing shows either side of the current one
    LISTING_AHEAD = 5

    def _toggle_listing(self):
        self._listing_on = not self._listing_on

    def _listing_payload(self):
        """The listing the sidebars draw: {"cur": the 1-based line the playhead
        is executing, "lines": [[n, text], ...]} -- a window of the file with
        the current line inside it -- or None while the listing is off."""
        if not self._listing_on or not self.gcode_lines:
            return None
        line, _cmd = self._cursor_line(self._cursor()[0])
        if not line:
            return None
        n = len(self.gcode_lines)
        span = 2 * self.LISTING_AHEAD + 1
        lo = max(1, min(line - self.LISTING_AHEAD, n - span + 1))
        hi = min(n, lo + span - 1)
        return {"cur": line,
                "lines": [[i, self.gcode_lines[i - 1].rstrip()[:80]]
                          for i in range(lo, hi + 1)]}

    def _toggle_travels(self):
        """Show/hide the TRAVEL FIBRE -- the material the jet lays while the head
        moves between parts and between layers, which the converter solves with
        the rest of the print (`merge_travels`).

        It does not mean the commanded travel POLYLINE; that has its own
        switch (`--travel-gcode-line`, the Scaffold designer's checkbox, off by
        default), since travel fibre is the default rendering. The fibre is
        the thing that is continuous with the print, so it is the one the
        primary toggle drives; the g-code line is the opt-in overlay.

        Every chunk has to refill, not just the one straddling the head: the
        mask changed under all of them, and the reveal skips a chunk whose
        prefix count is unchanged."""
        self._travels_on = not self._travels_on
        for ch in self._depo_chunks:
            ch["count"] = -1                   # force a refill under the new mask
        self._depo_last_n = -1
        self._apply_time()

    def _toggle_printhead(self):
        """Show/hide the printhead actor (real STL or fallback nozzle). The jet
        is a separate actor, so it stays visible -- you can watch the bare jet
        and contact point without the head in the way."""
        self._head_visible = not self._head_visible
        if self.head_actor is not None:
            self.head_actor.SetVisibility(self._head_visible)

    def _toggle_jet(self):
        """Show/hide the jet (the molten thread from nozzle to fibre contact),
        independently of the printhead. The jet still auto-extinguishes once the
        print finishes (t >= total), so this only matters mid-print."""
        self._jet_visible = not self._jet_visible
        self._apply_time()

    def _gcode_lines_on(self):
        """Whether ANY commanded g-code line is drawn -- the switch's state. The OR,
        not `_deposit_on`, because a `--travel-gcode-line` launch draws the grey
        travels with the gold half off, and a switch that read OFF there would add
        lines on a click labelled "Hide"."""
        return bool(self._deposit_on or self._travel_lines_on)

    def _toggle_deposit_lines(self):
        """Show/hide the COMMANDED G-CODE LINES -- gold G1 deposit AND grey G0
        travel -- so the trailing scaffold mesh can be viewed without the lines on
        top of it. The active segment up to the head follows suit in _apply_time.

        It owns BOTH kinds because that is what the switch is labelled. Flipping
        `_deposit_on` alone would leave the travel polyline reachable only from
        `--travel-gcode-line` at launch: on a print whose G0s are half the moves --
        a sag ladder hops between every pair of walls -- "G-code path" would draw
        the walls and silently drop 2759 of 6293 commanded segments. Machine-live
        already flips the two together off its one
        checkbox (`_on_embed_msg`); this is the same shape for the sidebar.

        The state is the OR, so a `--travel-gcode-line` launch (travel lines on,
        deposit lines off) reads as ON -- lines ARE showing -- and one click clears
        both rather than adding the gold half to a switch that already looks on.
        The travel FIBRE tube is a separate switch and is not touched here: the
        launch flag still trades the tube for the line, but a click on this switch
        only ever adds or removes lines."""
        want = not (self._deposit_on or self._travel_lines_on)
        self._deposit_on = want
        self._travel_lines_on = want
        self.deposit_actor.SetVisibility(want)
        self._apply_time()

    # -- lighting ----------------------------------------------------------- #
    def _lit_actors(self):
        """The actors whose MATERIAL the lighting toggle owns: the deposition
        chunks, and nothing else.

        Everything else in the scene already carries a hand-tuned material with
        a reason attached -- the plate is matte so it doesn't sweep a hotspot as
        you orbit, the STL overlay is forced flat in _toggle_overlay so a reader
        swap can't restyle it, the jet and printhead are lit to read as molten
        polymer and brushed metal. The light KIT still falls on all of them
        (lighting is per-renderer); only the material stays theirs."""
        return list(getattr(self, "depo_actors", ()) or ())

    def _apply_studio_light(self):
        """Install (or lift) the light kit and the matching fibre material.

        Both halves move together: the material's specular term is invisible
        under a headlight, and the kit alone leaves the mesh matte, so splitting
        them into two controls would give the user two settings that only mean
        something in one combination."""
        kit = getattr(self, "_light_kit", None)
        if kit is None or self.renderer is None:
            return
        # Clear FIRST, always. vtkRenderer creates its automatic headlight during
        # any Render that finds the light list empty, and that light is a normal
        # member of the list afterwards -- so going studio -> flat -> studio with
        # only Add/RemoveLightsFromRenderer left the headlight in place and the
        # kit stacked on top of it: 6 lights instead of 5, visibly brighter than
        # the first studio pass (reported zoomed in on a junction, reproduced at
        # 6 vs 5). Clearing makes the toggle idempotent in both directions.
        self.renderer.RemoveAllLights()
        if self._studio_light:
            kit.AddLightsToRenderer(self.renderer)
        # else: leave the list empty -- the next Render re-creates exactly one
        # automatic headlight, which IS the stock look.
        for a in self._lit_actors():
            set_fibre_material(a.GetProperty(), self._studio_light)

    def _toggle_studio_light(self):
        """Sidebar toggle: studio lighting vs the stock single headlight."""
        self._studio_light = not self._studio_light
        self._apply_studio_light()

    # -- machine model (multi-body printer) --------------------------------- #
    def _apply_machine_visibility(self):
        """Set each machine body's visibility from the master toggle (`_machine_on`)
        and the profile's per-part hide set; a hidden body is simply not drawn.
        No-op without a loaded machine model. Visibility only: placement and
        the export are untouched."""
        if self.machine is None:
            return
        on = self._machine_on
        hidden = self.machine.hidden_parts
        for actor, role in ((self.head_actor, "head"),
                            (self.gantry_actor, "gantry"),
                            (self.bed_actor, "bed"),
                            (self.static_actor, "frame")):
            if actor is not None:
                # the head body is the Printhead switch's, which the Machine
                # switch sets (_toggle_machine) -- so the head alone can be shown
                # with the rest of the machine hidden. At scene build the
                # switches do not exist yet, so the head follows the machine.
                shown = (getattr(self, "_head_visible", on) if role == "head"
                         else on)
                actor.SetVisibility(shown and role not in hidden)
        # the rotary bodies are machine parts like any other -- a five-axis table
        # is two big bodies and leaving them lit with the machine hidden reads as
        # the toggle being broken. They have no `hidden_parts` role of their own:
        # the set names the four kinematic slots, and a joint is not one of them.
        for actor in self.rotary_actors:
            if actor is not None:
                actor.SetVisibility(on)

    def _toggle_machine(self):
        """Show/hide the machine model. When hidden, the nozzle marker stands in for
        the printhead so there's still a tool indicator; the print (and the jet) stay
        in Frame A either way -- the toggle is purely the machine geometry. The
        profile's per-part hide set is re-applied so a body hidden in the config
        stays hidden when the model is toggled back on.

        The printhead is part of the machine, so its switch follows: Machine off
        turns Printhead off (one click then shows the head alone), Machine on
        turns it back on."""
        if self.machine is None:
            return
        self._machine_on = not self._machine_on
        self._head_visible = self._machine_on
        self._apply_machine_visibility()
        self._apply_time()

    def _load_part(self, path, color):
        """Load one machine-part STL into a renderer actor + its reader (returned so
        the pipeline stays alive). Returns (actor, reader) or None on a missing/bad
        file -- a skipped part is never fatal, the rest of the machine still draws."""
        if not path or not os.path.isfile(path):
            return None
        try:
            reader = vtk.vtkSTLReader()
            reader.SetFileName(path)
            reader.Update()
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputConnection(reader.GetOutputPort())
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.SetScale(self.machine.scale)
            prop = actor.GetProperty()
            prop.SetColor(*color)
            prop.SetSpecular(0.2)
            prop.SetSpecularPower(20)
            self.renderer.AddActor(actor)
            return actor, reader
        except Exception as exc:  # noqa: BLE001 -- skip a bad part, never crash
            sys.stderr.write(f"gcode_player: machine part load failed "
                             f"({os.path.basename(path)}): {exc}\n")
            return None

    @staticmethod
    def _part_points(reader):
        """The (N, 3) vertex array of a loaded part, or an empty (0, 3) array when the
        STL has no geometry. vtkSTLReader yields a polydata with GetPoints() == None on
        a file it cannot parse (a non-STL/empty file), so guard that before numpy_support
        dereferences it -- otherwise auto tip / bed-top detection crashes the player at
        startup (and under pythonw, where stderr is a null sink, the window just never
        opens). Returns a real array so callers test `.size`, never None."""
        poly = reader.GetOutput()
        vtk_pts = poly.GetPoints() if poly is not None else None
        if vtk_pts is None or vtk_pts.GetNumberOfPoints() == 0:
            return np.empty((0, 3), float)
        return numpy_support.vtk_to_numpy(vtk_pts.GetData())

    def _build_machine(self):
        """Build the machine-model actors from the loaded profile. The printhead is
        required and carries the nozzle tip; gantry/bed/static are optional.
        The bed is auto-raised so its top meets the deposition plane -- the export
        parks it low (its top sits well below the nozzle), so without this the
        collector + print would float above it. Returns True once the printhead is
        built (so the caller skips the marker); on a printhead miss it disables the
        machine view and returns False, falling back to the marker."""
        m = self.machine
        # A mandrel is a CONSUMABLE: the same mount takes a 1 mm and a 6 mm rod,
        # so the size comes from the print, not from the machine.json. Installing
        # it here rather than seating the group by hand is what keeps ONE seat in
        # the program: `place_scene` folds `-R0` into `bed_group`, and the
        # no-machine path uses the same expression through `_collector_seat`.
        if self._frame is not None:
            body = self._frame.body_spec()
            kind = None if body is None else body.get("kind")
            if kind == "cylinder":
                # ...so install the rod ONTO whatever mount the profile declares,
                # never over it: replacing the joint here threw away the machine
                # page's word, mount role, axis height and holder every time a tube
                # was played. With no `rotary` block at all (a machine.json written
                # before the page had one) fall back to the historical A-on-the-bed
                # joint, so an old profile still animates a tube exactly as it did.
                if not m.rotaries:
                    m.rotary = machine_model.RotaryAxis(word="A", role="bed")
                m.rotary.radius = body["radius"]
                m.rotary.length = self._mandrel_len or 30.0
            elif m.rotary_count < 2:
                # A CURVED collector on a rig that cannot hold one. There is no
                # honest fallback: a surface of revolution is not a rod, so
                # fabricating a joint here would draw a mandrel that is not on the
                # machine -- exactly what `place_scene` withholds `collector_body`
                # from a pair to prevent. Fail visibly rather than plausibly.
                raise SystemExit(
                    "gcode_player: a %s collector needs a machine profile with "
                    "TWO rotary joints (\"rotaries\": [collector, base]); this "
                    "one declares %d. Nothing is drawn rather than drawing a rod "
                    "that is not on the rig."
                    % (kind, m.rotary_count))
            # A five-axis PAIR carries no rod, no radius and no length: the
            # collector's geometry is the FRAME's, and the joints only turn it.
            # Leaving them untouched is the whole of the install here.
        else:
            # A ROTARY RIG printing a FLAT scaffold is a 3-axis scene: the stage is
            # fitted but nothing is wound on it, so the joints must not seat the
            # build group by -R0 or lift the bed by a rod that is not there.
            # Dropping them is what keeps a plate print on a rotary-capable machine
            # byte-identical -- and it drops BOTH joints of a five-axis pair, for
            # the same reason it drops a mandrel's one.
            m.rotaries = ()
        head = self._load_part(m.head_path, PRINTHEAD_COLOR)
        if head is None:
            sys.stderr.write("gcode_player: machine model needs an X/printhead STL; "
                             "using the nozzle marker instead.\n")
            self.machine = None
            return False
        self.head_actor, head_reader = head
        self._see_through_head()
        head_pts = self._part_points(head_reader)
        # nozzle tip: explicit from the profile, else auto-detected (lowest ring). A
        # printhead STL VTK could not parse (no points) has no detectable tip and would
        # render invisibly -- treat it like a missing printhead (marker fallback) rather
        # than crash on the empty cloud.
        if m.tip is not None:
            self._mach_tip = np.asarray(m.tip, float)
        elif head_pts.size:
            self._mach_tip = machine_model.find_tip(head_pts)
        else:
            sys.stderr.write("gcode_player: printhead STL has no readable geometry; "
                             "using the nozzle marker instead.\n")
            self.renderer.RemoveActor(self.head_actor)
            self.head_actor = None
            self.machine = None
            return False

        g = self._load_part(m.gantry_path, GANTRY_COLOR)
        self.gantry_actor = g[0] if g else None
        b = self._load_part(m.bed_path, BED_COLOR)
        self.bed_actor = b[0] if b else None
        bed_corner_stl = None                     # (x, y) of the bed top-face corner
        bed_top = bed_zrange = None
        bed_pts = self._part_points(b[1]) if b is not None else np.empty((0, 3), float)
        if bed_pts.size:
            # the bed's PRINT SURFACE (robust to raised nubs -- find_bed_top_face
            # rejects them): its Z and bottom-left corner, in the bed STL's own frame.
            # An unreadable bed STL (no points) simply yields no datum -- the slide
            # falls back to the world-frame offset (see _compute_registration).
            bed_zrange = (float(bed_pts[:, 2].min()), float(bed_pts[:, 2].max()))
            # a MANUAL datum (set in the machine config / picked in the calibration
            # window) overrides auto-detect -- the robust path for an assembly STL
            # where find_bed_top_face can't isolate the print bed.
            manual_datum = (m.bed_top_z is not None and m.bed_corner is not None)
            if manual_datum:
                bed_top = m.bed_top_z
                bed_corner_stl = (float(m.bed_corner[0]), float(m.bed_corner[1]))
            else:
                bed_top, _bcx, _bcy = machine_model.find_bed_top_face(bed_pts)
                bed_corner_stl = (_bcx, _bcy)
            # keep the PRINT SURFACE's height in the bed STL's own frame: the
            # machine-live reveal seats the scaffold on it directly rather than
            # trusting the frame arithmetic to land there (see _reveal_surface_z)
            self._bed_top_stl = bed_top
        # THE SEAT DATUM: where the thing the print is laid on actually sits, in
        # the frame of the STL it was picked on. On every rig this program had
        # until now that is the bed's top face (or the manual datum on the
        # holder, which points at the mandrel AXIS). A five-axis TABLE has a
        # collector that is not on the bed at all -- it is bolted to the top of
        # the spinning body, two joints up -- so its datum is the pick on THAT
        # body, `rotaries[0].pivot`, and it feeds the same two consumers the bed
        # datum feeds: the contact gap below and the in-plane registration.
        #
        # The bed's own numbers are left exactly as they were: `_bed_top_stl` is
        # the drawn bed's face and the machine-live reveal still seats on it.
        # This is a second SOURCE for one datum, not a second datum.
        seat_top, seat_corner_stl, seat_src = bed_top, bed_corner_stl, "bed"
        collector_datum = (m.collector_origin(self._mount_axial())
                           if m.rotary_count > 1 else None)
        if collector_datum is not None:
            seat_top = float(collector_datum[2])
            seat_corner_stl = (float(collector_datum[0]), float(collector_datum[1]))
            seat_src = ("collector seat" if m.rotaries[0].seat_set
                        else "collector pick")
        # A PICKED HOME OUTRANKS THE DERIVATION, and replaces it rather than
        # adding to it. Where the machine homes could not be calibrated, so
        # for now the home is a point picked on the C axis. That pick says where the nozzle
        # tip IS at g-code (0, 0, 0) -- an observation. Everything the block
        # below computes is a MODEL of the same number: the collector's mount
        # face, plus a standoff, plus how far the collector's own geometry
        # stands off that face. Running an observation through a model puts a
        # measured number and a modelled one in series, so when the home is
        # picked the standoff and the surface term are dropped and the gap is
        # `scale * (tip_z - home_z)` exactly.
        #
        # ALL THREE AXES COME FROM IT, not Z alone. The home is not a point on
        # the spin axis: a real pick sits 42.93 mm off that axis in X and
        # 11.79 mm in Y, because the zero is often at the edge of the collector
        # or off it entirely, so the travel fibre does not cross an important
        # part of the print. Dropping its XY would discard the whole point of
        # the pick and start the nozzle inside the dome.
        #
        # IT SEATS THE FILE'S ZERO, not the collector. `rotary_transform` now
        # subtracts this pick from every exported coordinate, so g-code (0, 0, 0)
        # IS this point; the collector's own placement is the seat above, and the
        # two are independent -- moving one does not move the other.
        collector_home = m.collector_home if m.rotary_count > 1 else None
        if collector_home is not None:
            seat_top = float(collector_home[2])
            seat_corner_stl = (float(collector_home[0]),
                               float(collector_home[1]))
            seat_src = "picked home"
        if seat_top is not None:
            # bring the nozzle tip and the bed top into one-standoff contact (the
            # deposition plane). `gap` is how far the bed would rise to it. WHO moves
            # to close it is the rig's call: a Z-driven bed (MEWron) rises to the
            # fixed nozzle, a Z-driven nozzle assembly drops the tool onto a FIXED
            # bed. Constant nudges, so `place` stays generic. A rotary collector
            # RIDING THE BED stands its deposition surface one RADIUS off the datum;
            # without that term the bed rises until its own face meets the plane and
            # drives the rod up through the printhead. A stage on the static frame is
            # not on this chain at all. THE AXIS IS THE DATUM, with no second source:
            # a manual datum points at the mandrel axis in the holder's own frame, so
            # the axis IS `bed_top`. With NO datum the rod is drawn on the
            # auto-detected top face -- REPORTED by `_log_placement`, not papered over.
            # THE MOUNT IS THE BASE JOINT -- the last one, the one actually
            # bolted to a body -- and on every shipped single-joint rig it is the
            # same object it always was. The HEIGHT is the frame's, not the
            # joint's: `seat_offset` is `-R0` on a cylinder and `-rho(u_mid)` on a
            # surface of revolution, so one expression seats both collectors and
            # the player never asks a joint for a radius a curved collector does
            # not have. `machine_model` takes a pair's `collector_seat` as an
            # ARGUMENT for exactly this reason -- its height datum belongs to the
            # surface, so the caller that owns the surface supplies it.
            surface_above_bed = 0.0
            mount = m.rotaries[-1] if m.rotaries else None
            if mount is not None and mount.role == "bed":
                surface_above_bed = self._collector_surface_height()
            if collector_home is not None:
                # THE STANDOFF IS NOT DROPPED HERE. The argument for dropping
                # it is that the picked home IS the nozzle position, so adding
                # a modelled standoff to a measured point puts the two in
                # series. That argument is about the TIP; this expression is
                # about the COLLECTOR, and dropping the term does not move the
                # tip -- it moves the collector, by exactly one standoff,
                # straight into its own picked seat. Measured with the seat and
                # the home both picked: the dome's rim sat -3.5000 mm through
                # the seat plane at standoff 3.5 with the term dropped, and
                # 0.0000 with it kept (read off the actors' own VTK matrices).
                #
                # `surface_above_bed` is 0 on a pair whatever happens here (the
                # file measures from the collector's own origin), so the two
                # branches now differ in one thing only: WHERE g-code Z = 0
                # lands -- the picked home, or the collector's own datum.
                gap = (m.scale * (self._mach_tip[2] - seat_top)
                       - m.standoff)
            else:
                gap = (m.scale * (self._mach_tip[2] - seat_top)
                       - m.standoff - surface_above_bed)
            parts, depo = m.contact_alignment(gap)
            for role, off in parts.items():
                m.part_offsets[role] = m.part_offsets.get(role, np.zeros(3)) + off
            m.deposition_offset = m.deposition_offset + depo
        s = self._load_part(m.static_path, FRAME_COLOR)
        self.static_actor = s[0] if s else None
        # The mandrel HOLDER (optional). The rod itself is drawn from the job
        # (`_mandrel_body`); this is the chuck/stage that carries it, and it is a
        # machine part like any other -- it rides the body its `role` names, so a
        # bed-mounted stage sinks with Z and a frame-mounted one never moves.
        # ...and it is the BASE joint that names it, because that is the one
        # bolted to a body. On a single-joint rig it is the same object.
        # ...and on a five-axis table there are TWO of them, one per joint:
        # `rotary_paths` resolves the lot, collector-first. Resolving only the
        # first draws half a machine.
        self.rotary_actors = tuple(
            (self._load_part(path, BED_COLOR) or (None,))[0]
            for path in m.rotary_paths)
        self.holder_actor = self.rotary_actors[-1] if self.rotary_actors else None
        # the slide offset is the collector's position ON THE BED, so it is a
        # number a pair does not have: the pick already says where the collector
        # is, and adding a slide to it would move the print off the table it was
        # just registered to. The Machine page hides the row for a pair; this
        # makes a stale value in an old profile inert as well.
        self._compute_registration(seat_corner_stl,
                                   slide=None if collector_datum is None
                                   else np.zeros(2))
        self._report_machine(bed_top, bed_corner_stl, bed_zrange,
                             seat_top, seat_corner_stl, seat_src)
        return True

    def _report_machine(self, bed_top, bed_corner_stl, bed_zrange,
                        seat_top=None, seat_corner_stl=None, seat_src="bed"):
        """Print what the machine model actually computed to stderr (the GUI shows it
        in the run log). This is the calibration read-out: if the slide phases through
        the bed or a part sits off the frame, these numbers say why -- a `bed top-face`
        near the LOW end of `bed Z-range` means the detector picked the underside; a
        large `registration` means the collector seats far from the parts' home (set
        the slide offset / motion limits to bring it back)."""
        m = self.machine

        def _v(a):
            return None if a is None else [round(float(x), 3) for x in np.asarray(a)]

        src = "manual" if m.bed_top_z is not None and m.bed_corner is not None \
            else "auto"
        lines = ["gcode_player machine model:",
                 f"  nozzle tip (head frame): {_v(self._mach_tip)}",
                 f"  bed top-face Z ({src}): "
                 f"{None if bed_top is None else round(bed_top, 3)}"
                 f"   corner: {_v(bed_corner_stl)}"
                 f"   (bed Z-range: {_v(bed_zrange)})",
                 f"  slide offset: {_v(m.slide_offset)}",
                 f"  build registration (collector seat): {_v(self._registration)}",
                 f"  motion limits: {m.limits or 'none set'}"]
        # WHICH DATUM SEATED THE PRINT. On a five-axis table this is NOT the bed
        # line above it: the collector is bolted to the spinning body two joints
        # up, so its datum is the pick on that body. Printing both, named, is
        # what makes a rig seated off the wrong face readable rather than just
        # visibly odd -- and `slide offset` above is inert once the pick is the
        # source, which is why it is said here instead of looking ignored.
        if seat_src != "bed":
            lines.append(
                f"  collector seat datum ({seat_src}): "
                f"{None if seat_top is None else round(seat_top, 3)}"
                f"   corner: {_v(seat_corner_stl)}"
                f"   (the bed line above seats nothing on this rig, and the "
                f"slide offset is ignored)")
        # THE MOUNT, when there is one -- what decides whether the rod is drawn
        # where the mandrel actually sits or somewhere merely plausible. The
        # UNCALIBRATED case is the one that has to be loud: with no datum the
        # software has never been told where the axis is, so it falls back to the
        # holder's auto-detected top face and the rod is almost certainly in the
        # wrong place. Saying so is the whole reason the `axis_height` field was
        # removed instead of being defaulted -- a believable number in that slot
        # is what let a STALE datum from a previously-loaded bed STL go unnoticed.
        # ONE LINE PER JOINT, collector-first, because a five-axis rig has two and
        # reporting only the first is how a silently-ignored mount reads as fine.
        # The ROD line belongs to the mandrel alone: a curved collector has no rod,
        # and printing `r0.000 x 0.000` for one would be a plausible wrong number
        # in exactly the slot where an axis-height guess would mislead. The datum note
        # rides the BASE joint, the one bolted to a body.
        paths = m.rotary_paths
        for i, r in enumerate(m.rotaries):
            base = (i == len(m.rotaries) - 1)
            if m.rotary_count > 1:
                # A PAIR: each joint has its own picked line and its own body, so
                # the note is per joint rather than one datum note on the base.
                # An unpicked joint turns about its STL ORIGIN, which is almost
                # never where a bearing is -- said, not papered over, exactly as
                # the mandrel's no-datum case is.
                if r.pivot_set:
                    note = ("   (axis through %s of its own STL, picked)"
                            % (np.round(r.pivot, 4).tolist(),))
                else:
                    note = ("   *** NO ROTATION CENTRE PICKED: this joint DOES "
                            "NOT TURN, and neither does anything mounted on "
                            "it. There is no line to turn about, and using the "
                            "STL's origin instead would swing the body across "
                            "the scene at a plausible angle. Calibrate it on "
                            "the Machine page.")
                body = (os.path.basename(paths[i]) if paths[i]
                        else "no body STL (the joint turns, nothing is drawn)")
                lines.append(f"  rotary joint {i}: word {r.word} about {r.axis}, "
                             f"rides {r.role}, body {body}{note}")
                continue
            if not base:
                note = "   (carried by the joint below it)"
            elif m.bed_top_z is not None and m.bed_corner is not None:
                note = "   (axis from the manual datum on the holder)"
            elif r.role != "bed":
                note = ("   (frame-mounted stage: the collector never moves, so "
                        "there is no bed contact to seat it through)")
            else:
                note = ("   *** NO COLLECTOR-AXIS DATUM SET: the collector is "
                        "drawn on the holder's auto-detected top face and is "
                        "probably not where it actually is. Calibrate the axis "
                        "on the Machine page (Bed STL -> Calibrate).")
            rod = ""
            if self._frame is not None and m.rotary_count == 1:
                rod = f"mandrel r{r.radius:.3f} x {r.length:.3f} mm"
            elif self._frame is not None:
                rod = "curved collector (geometry from the frame, not the joint)"
            else:
                rod = "no collector mounted"
            lines.append(f"  rotary joint {i}: word {r.word}, rides {r.role}, "
                         f"{rod}{note}")
        sys.stderr.write("\n".join(lines) + "\n")

    def _compute_registration(self, bed_corner_stl, slide=None):
        """Seat the collector/print on the printer: compute the in-plane build
        registration (`self._registration`) so the collector's g-code-origin corner
        lands at the bed STL's top-face bottom-left corner + the profile slide_offset.

        With NO bed STL the slide offset is taken in the world frame directly (the
        marker/preview fallback). Otherwise: the bed top-face corner in world is the
        STL corner (scaled) plus the bed actor's home position; the collector corner's
        canonical world position (registration 0) comes from one place_scene at the
        g-code origin; the registration is the difference. Evaluated once at the origin
        command, so it is a constant seat the print then animates from. Pure layering
        over machine_model -- contact_alignment must already be applied (it shifts the
        bed/deposition the canonical position depends on)."""
        m = self.machine
        slide = (np.asarray(m.slide_offset, float)[:2] if slide is None
                 else np.asarray(slide, float)[:2])
        if bed_corner_stl is None:
            self._registration = slide.copy()
            return
        sc0 = machine_model.place_scene(m, self._mach_tip, np.zeros(3),
                                        origin=self.origin, registration=(0.0, 0.0))
        bed_corner_world = m.scale * np.asarray(bed_corner_stl, float) + sc0["bed"][:2]
        # the collector's g-code-origin corner sits at exported XY = self.origin[:2]
        # (the footprint is anchored at (0,0), re-based by -datum = origin), and rides
        # the build group, so its canonical world XY is that + bed_group.
        collector_corner_world = self.origin[:2] + sc0["bed_group"][:2]
        self._registration = bed_corner_world + slide - collector_corner_world

    # Z lift (mm) for the machine-live reveal overlay. The exported file's first layer
    # sits at Z = 0, exactly coplanar with the drawn bed's top face, so without a lift
    # the gold path Z-FIGHTS the bed (fading in and out as you zoom -- worst zoomed in,
    # where the depth range spans the whole machine) and the micron fibre tubes are
    # half-buried. 0.05 mm is invisible at machine scale and kills both.
    _LIVE_OVERLAY_LIFT = 0.05

    def _lift_reveal_over_surfaces(self):
        """Make the machine-live reveal lines win the depth test against the surface
        they lie on, at any zoom.

        The exported first layer is at Z = 0, coplanar with the drawn collector/bed
        top face, so the path Z-FIGHTS it: the line renders as a dim stipple of itself
        and vanishes as you zoom, because the depth range VTK fits around the visible
        scene changes with the view and flips which fragment wins. The world-space
        `_LIVE_OVERLAY_LIFT` alone cannot fix that -- a fixed millimetre offset is a
        different number of depth units at every zoom, which is precisely why the
        symptom is zoom-dependent.

        Polygon-offset coincident-topology resolution is the scale-free version of the
        same idea: the offset is applied in DEPTH units at raster time, so a coplanar
        line stays in front whatever the camera is doing. Applied only to the reveal
        actors, so the ordinary sim's rendering is untouched."""
        for actor in self._reveal_actors:
            mapper = actor.GetMapper()
            if mapper is None:
                continue
            try:
                mapper.SetResolveCoincidentTopologyToPolygonOffset()
                # negative units = toward the viewer; -4 clears a coplanar surface
                # with room to spare without lifting the line off short walls
                mapper.SetRelativeCoincidentTopologyLineOffsetParameters(0.0, -4.0)
                mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(0.0, -4.0)
            except AttributeError:      # older VTK: the world-space lift still applies
                pass

    def _overlay_delta(self, delta):
        """Placement offset for FILE-frame reveal geometry: the bed delta plus the
        machine-live work offset H **in X and Y only**, plus the anti-Z-fighting lift.
        Outside machine-live-with-a-source this returns `delta` unchanged, so the
        normal sim's placement is byte-identical.

        H is flattened to XY on purpose. In X/Y it is a real displacement -- the
        work origin decides where on the plate the print lands, so the overlay
        moves with it. In Z it is not a displacement at all: zeroing Z is done ON
        the collector, so file Z = 0 already MEANS the collector surface, and
        adding Hz shifts the print off that surface by exactly Hz (measured: at
        Hz = -2 mm the first layer sat 1.95 mm below the bed top face -- buried by
        a constant, so no depth-buffer trick could have fixed it).

        In Z it does not use `delta` either. `delta` is the build group's offset,
        and the print lands on the collector through it only if every term in the
        frame chain happens to agree; on the rig they did not. So the Z is
        MEASURED: `_reveal_surface_z` reads where the print surface is actually
        drawn this frame and seats the base layer on it, and because the reveal
        and the plate then ride the same bed delta their separation stays exactly
        `_LIVE_OVERLAY_LIFT` at any machine height."""
        if not self._live_src:
            return delta
        surface = self._reveal_surface_z()
        if surface is None:                     # no bed / plate drawn -- keep the frame
            return np.asarray(delta, float) + np.array(
                [self._live_H[0], self._live_H[1], self._LIVE_OVERLAY_LIFT])
        return np.array([delta[0] + self._live_H[0], delta[1] + self._live_H[1],
                         surface + self._LIVE_OVERLAY_LIFT - self._reveal_base_z])

    def _reveal_surface_z(self):
        """WORLD Z of the top of the drawn print surface, this frame, or None.

        Whichever surface is drawn highest wins -- the collector plate when one is
        loaded and visible, otherwise the bed's print face. Taking the highest is the
        point: the scaffold must sit ON the surface it is printed on, and being buried
        in either of them is the failure this replaces.

        The bed's face is `find_bed_top_face`'s datum (NOT the STL's max Z, which a
        raised nub or a screw boss would win), carried through the actor's own
        scale + position so it tracks the bed as it moves in Frame A."""
        tops = []
        if (self.bed_actor is not None and self._bed_top_stl is not None
                and self.bed_actor.GetVisibility()):
            scale = self.bed_actor.GetScale()[2]
            tops.append(self._bed_top_stl * scale + self.bed_actor.GetPosition()[2])
        if self.plate_actor is not None and self.plate_actor.GetVisibility():
            if self._plate_top_local is None:
                self._plate_top_local = float(self.plate_actor.GetBounds()[5]) \
                    - self.plate_actor.GetPosition()[2]
            tops.append(self._plate_top_local + self.plate_actor.GetPosition()[2])
        return max(tops) if tops else None

    def _place_machine(self, head):
        """Frame A placement for the commanded point `head` (g-code XYZ): position the
        machine parts, slide the build group (toolpath, deposited print, collector,
        triad) with the bed, and run the jet from the machine nozzle tip to the
        bed-borne contact. The frame math is machine_model.place_scene (it undoes the
        g-code origin re-base and applies the bed-anchored registration that seats the
        collector on the bed; the printhead tracks the build, only the static frame and
        the bed STL stay fixed)."""
        sc = machine_model.place_scene(self.machine, self._mach_tip, head,
                                       self.origin, self._registration,
                                       rotary_deg=self._rot_deg,
                                       rotary_start=self._mandrel_start(),
                                       mount_axial=self._mount_axial())
        self.head_actor.SetPosition(*sc["head"])
        if self.gantry_actor is not None:
            self.gantry_actor.SetPosition(*sc["gantry"])
        if self.bed_actor is not None:
            self.bed_actor.SetPosition(*sc["bed"])
        if self.static_actor is not None:
            self.static_actor.SetPosition(*sc["static"])
        # Every rotary BODY rides the part it is bolted to, so it stays registered
        # to the collector through Z motion instead of drifting off it -- and it is
        # the BASE joint that is bolted, which on a pair is not `rotary`. On a pair
        # the body above the base additionally TURNS, about its own picked line, by
        # the joints under it; on a mandrel the one body's chain is empty and this
        # is the SetPosition it always was, reached through an identity matrix.
        for actor, mat in self._rotary_body_matrices(sc):
            actor.SetPosition(0.0, 0.0, 0.0)
            actor.SetOrientation(0.0, 0.0, 0.0)
            actor.SetUserMatrix(mat)
        # The build group rides the bed motion + registration as one shift. Two
        # groups, one rule: PHYSICAL props on the bed (collector, scaffold STL, origin
        # triad) take the plain delta; FILE-frame reveal geometry (toolpath lines,
        # deposition mesh) additionally takes the machine-live work offset H, which is
        # what lands the exported coordinates under the real nozzle. Registration
        # cancels -- it is applied equally to the overlay (via bed_group) and to the
        # nozzle (via tip_world) -- so only H moves the reveal. H is zero outside
        # machine-live, making both groups identical in the normal sim.
        delta = sc["bed_group"]
        # the PHYSICAL props go first: _overlay_delta measures where the collector is
        # actually drawn, so it has to run after the collector has been placed
        for a in self._bed_actors:
            a.SetPosition(*delta)
        overlay = self._overlay_delta(delta)
        self._last_reveal_shift = overlay
        for a in self._reveal_actors:
            a.SetPosition(*overlay)
        for a in self.depo_actors:
            a.SetPosition(*overlay)
        # ...and, on a mandrel, the spin. Positions above, orientation here, so
        # the two groups keep their separate translations (the reveal takes the
        # machine-live work offset; the props do not) and share one rotation.
        if self._frame is not None:
            self._place_collector(delta, overlay, self._rot_deg, False)
        # the jet: from the machine nozzle tip down to the (bed-shifted) contact, so
        # it meets the fibre where it actually rests in Frame A. Same jet machinery
        # as the single-head path, only the endpoints are machine-frame aware.
        tip_world = np.asarray(sc["tip_world"], float)
        if self._jet_t is not None and self.t < self.total and self.t >= self._jet_on:
            contact, cdiam = self._jet_contact()
            # the contact is a point ON the revealed fibre, so it takes the reveal
            # group's shift, not the bed's -- and its spin, on a mandrel
            contact = self._to_scene(contact, overlay, self._rot_deg)
            arc = self._jet_arc_points(tip_world, contact)
            # points + cone-jet radius profile + the flat nozzle-exit face
            self._set_jet_geometry(arc, tip_world, contact, cdiam)
            self.jet_actor.SetVisibility(self._jet_visible)
        else:
            self.jet_actor.SetVisibility(False)

    def _overlay(self, which):
        """(actor-or-None, file path, colour) for the named STL overlay. The build
        plate is the only one left; the scaffold-STL overlay went with its switch
        (see _build_buttons), and `which` stays so the plate's call site still
        names what it is asking for."""
        return self.plate_actor, self.plate_path, PLATE_COLOR

    def _overlay_on(self, which):
        actor, _path, _col = self._overlay(which)
        return actor is not None and bool(actor.GetVisibility())

    # -- the collector: the plate in 3-axis, the mandrel rod in 4-axis ------ #
    #
    # One control for both, because a run has one collector or the other and the
    # user is asking the same question of either: get the thing the fibres are
    # sitting on out of the way. The plate is an STL sibling loaded lazily
    # through `_toggle_overlay`; the rod is drawn (`_mandrel_body`) and already
    # in the scene from the first frame. The list shape below costs nothing and
    # means the button's LABEL cannot disagree with the scene if a run ever
    # carries both.
    def _collector_actors(self):
        out = []
        if self.plate_actor is not None:
            out.append(self.plate_actor)
        if self.mandrel_actor is not None:
            out.append(self.mandrel_actor)
        return out

    def _collector_available(self):
        """Whether this run has a collector to show at all. `plate_path` rather
        than `plate_actor`: the plate is not read until the first toggle, so
        asking for the actor would hide the button on exactly the run that has a
        plate and has never been asked for it."""
        return bool(self.plate_path) or self.mandrel_actor is not None

    def _collector_on(self):
        return any(bool(a.GetVisibility()) for a in self._collector_actors())

    def _toggle_collector(self):
        want = not self._collector_on()
        if want and self.plate_actor is None and self.plate_path:
            # first ask for the plate: this loads it AND leaves it visible
            self._toggle_overlay("plate")
        for actor in self._collector_actors():
            actor.SetVisibility(want)

    def _toggle_overlay(self, which):
        """Show/hide the build-plate STL, loading it the FIRST time it's asked for
        (the meshes are often 100MB+, so we never read them at startup). Drawn
        SOLID -- it sits behind the toolhead as the print runs.

        The read goes through stl_viewer, not a bare vtkSTLReader, because this is
        the SAME fibre mesh the viewer opens and a plain reader pays twice for it:
        it welds coincident points through an incremental locator (O(V log V), and
        pointless for a flat-shaded facet mesh) and parses the 50-byte records one
        at a time. Measured on the 409 MB default export -- stl_viewer's numpy
        path 0.42 s, vtkSTLReader with SetMerging(0) 0.93 s, vtkSTLReader as it
        was here 2.62 s -- on a toggle the user hits mid-simulation."""
        actor, path, color = self._overlay(which)
        if actor is None:
            if not path or not os.path.isfile(path):
                return
            import stl_viewer
            actor = stl_viewer._stl_actor(path, color)
            if actor is None:
                return
            prop = actor.GetProperty()
            prop.SetColor(*color)
            # _stl_actor gives the STANDALONE viewer's slight gloss; this overlay
            # has always been drawn flat, and a reader swap must not restyle it.
            prop.SetSpecular(0.0)
            prop.SetSpecularPower(1.0)
            # the build plate reads as a MATTE surface: high ambient + no
            # specular so its shade stays constant as the camera orbits (a
            # flat plate under the headlight otherwise sweeps a bright band
            # across itself and looks like it "changes colour with angle").
            prop.SetAmbient(0.55)
            prop.SetDiffuse(0.45)
            actor.SetVisibility(False)      # the click below flips it visible
            self.renderer.AddActor(actor)
            self.plate_actor = actor
            # the collector plate rests on the bed, so it rides its
            # Frame-A Z motion; the next _apply_time positions it.
            if self.machine is not None:
                self._bed_actors.append(actor)
        actor.SetVisibility(not actor.GetVisibility())

    # -- deposition (growing sagged scaffold) ------------------------------- #
    def _depo_on(self):
        return self._depo_visible

    def _toggle_deposition(self):
        """Show/hide the deposition mesh, building it the FIRST time it's asked
        for. The build re-runs the sagging solve, so it can take a few seconds
        on a big scaffold -- which is why it's lazy and never touched at
        startup, and why a notice is drawn first (see _build_deposition)."""
        if not self._depo_built:
            self._build_deposition()
            if not self._depo_built:               # nothing to deposit / failed
                return
        self._depo_visible = not self._depo_visible
        # live mode builds the mesh incrementally and skips it while hidden, so
        # turning it ON must build it now (for the layers laid so far) -- the march
        # extends it from there. No-op in the locked sim (the cache mesh exists).
        if self.live_adjust and self._depo_visible and self._orch is not None:
            self._advance_deposition(force=True)
        for a in self.depo_actors:
            a.SetVisibility(self._depo_visible)
        self._depo_last_n = -1                      # force a reveal pass

    def _request_live_edit(self):
        """Ask the GUI (via the request channel) to relaunch this viewer in
        live-edit mode -- real-time parameter editing from t=0 -- or back to the
        static locked view. Embedded viewer only (the GUI owns the relaunch)."""
        if self.embed is not None:
            self.embed.send({"t": "request", "action": "live_edit",
                             "on": not self.live_adjust})

    # -- in-scene deviation recolour (embedded viewer) ----------------- #
    def _toggle_deviation(self):
        """Show/hide the deviation view: the scaffold recoloured by how far the
        physics predicts each fibre lands from the commanded path. The sidecar is
        computed GUI-side (gui_deviation) -- ask for it via the request
        channel; the GUI runs it and pushes it back down (see _on_deviation_msg).
        A cached sidecar re-applies instantly. Hiding restores the plain scaffold."""
        if self._deviation_on:
            self._clear_deviation()
            self.window.Render()
            return
        self._dev_error = ""                      # a fresh attempt retires the last one
        if self._dev_sidecar and os.path.isfile(self._dev_sidecar):
            self._apply_deviation(self._dev_sidecar)     # already computed
            self.window.Render()
        elif self.embed is not None:
            self._dev_computing = True            # persistent HUD status (_refresh_hud)
            self._refresh_hud()
            self.window.Render()
            self.embed.send({"t": "request", "action": "deviation"})

    def _on_deviation_msg(self, msg):
        """The GUI pushed a computed deviation sidecar DOWN (apply-in-place). An
        empty path means the analysis was unavailable/failed -- report it, with the
        GUI's reason, until the next attempt. A one-shot hud.SetInput here was
        overwritten by the next tick's readout, so a failure looked like nothing."""
        self._dev_computing = False
        side = msg.get("sidecar") or ""
        if not (side and os.path.isfile(side)):
            self._dev_error = ("Deviation unavailable: "
                               + (msg.get("error") or "no analysis"))
            self._refresh_hud()
            self.window.Render()
            return
        self._dev_sidecar = side
        self._apply_deviation(side)
        self.window.Render()

    def _deviation_vertex_scalar(self, sidecar, vtimes):
        """A per-mesh-vertex deviation scalar aligned to the deposition mesh, so the
        SAME time-sorted reveal grows the deviation view instead of a separate actor.

        The sidecar carries one deviation per cached CENTRELINE point (in the order
        `stl_viewer._load_centerlines` concatenates the kept >=2-point fibres). Every
        centreline point also has a print time in the .depo, and every mesh vertex the
        tube/wireframe/exact build emits carries the SAME (naive) print time in
        `vtimes` -- so interpolating deviation-vs-time and sampling at each vertex's
        time colours the mesh faithfully, independent of the tessellation, the preview
        decimation, OR whether the mesh came from the cache or a fresh g-code sag (a
        tube ring's facets share their centreline point's time, so they get exactly
        that point's deviation). Returns (dev_per_vertex, lut, scalar_top, unit) or
        None on a mismatch (-> plain mesh).

        `vtimes` must be the UN-remapped naive times (the .depo time base), matched
        before _to_clock carries them onto the accel-limited clock."""
        import stl_viewer
        depo = self._find_depo_cache()
        dv = stl_viewer._load_deviation(depo, sidecar) if depo else None
        if dv is None:
            return None
        dev, vcap = dv
        # per-point print times of the KEPT (>=2-point) fibres, concatenated in the
        # SAME order the sidecar's `dev` runs -- read straight from the .depo so the
        # (time -> deviation) table is independent of how the mesh was built.
        try:
            with np.load(depo, allow_pickle=False) as d:
                t = np.asarray(d["t"], dtype=np.float64)
                splits = np.asarray(d["splits"], dtype=np.int64)
                depo_xyz = np.asarray(d["xyz"])
        except Exception:  # noqa: BLE001
            return None
        t_kept = [tf for tf in np.split(t, splits[:-1]) if len(tf) >= 2]
        if not t_kept:
            return None
        t_all = np.concatenate(t_kept)
        if len(t_all) != len(dev):
            return None                       # fibre-length mismatch -> plain mesh
        # Defense-in-depth colour-cap floor at the float32-storage noise level, so a
        # physics-OFF scaffold (whose only "deviation" is |c|*2^-24 storage dust) paints
        # deep blue rather than the p99 washing it red. _load_deviation already floors
        # vcap; this re-derives it from the .depo xyz in hand in case a stale/legacy vcap
        # slips through. Formula mirrors mew_analysis._DEV_NOISE_ULP_K (K=8); NOT imported
        # (keeps this path independent of the physics engine).
        max_abs_coord = float(np.abs(depo_xyz).max()) if depo_xyz.size else 0.0
        vcap = max(float(vcap), 8.0 * 2.0 ** -24 * max_abs_coord)
        # micron display when the colour cap is sub-0.01 mm (mirror _deviation_actor's
        # scaling: the scalars AND the LUT range scale together, colours unchanged).
        unit, uscale = ("mm", 1.0)
        if 0.0 < float(vcap) < 0.01:
            unit, uscale = ("um", 1000.0)
        order = np.argsort(t_all, kind="stable")    # np.interp needs ascending x
        ts = np.ascontiguousarray(t_all[order])
        ds = np.ascontiguousarray(np.asarray(dev, dtype=np.float64)[order] * uscale)
        dev_vert = np.interp(np.asarray(vtimes, dtype=np.float64), ts, ds)
        lut = stl_viewer._deviation_lut(float(vcap) * uscale)
        return dev_vert, lut, max(float(vcap) * uscale, 1e-9), unit

    def _apply_deviation(self, sidecar):
        """Turn on the deviation view. In the LOCKED sim it is a colour scheme baked
        onto the deposition mesh: (re)build the mesh carrying a per-vertex deviation
        scalar, switch the chunk mappers to the deviation LUT, and add a colour key.
        The clock is NOT parked and the deposition is NOT hidden -- the deviation
        colouring rides the SAME progressive reveal as the plain mesh, so play / scrub
        / restart show the deviation at each print stage. Deviation is a LOCKED-SIM
        view only: the live-edit sim re-lays geometry incrementally with no matching
        per-print sidecar (its button is hidden -- see _build_buttons -- and a stray
        GUI push is ignored here). Fails gracefully (a mismatched sidecar leaves the
        plain scaffold)."""
        import stl_viewer
        if self.live_adjust:
            return                              # deviation is disabled in the live sim
        if not self._ensure_depo_deviation(sidecar):
            self._dev_error = ("Deviation did not match this scaffold — "
                               "generate it again")
            self._refresh_hud()
            return
        self._dev_error = ""
        if self._dev_bar is not None:
            self.renderer.RemoveActor2D(self._dev_bar)
        # the key's text in the viewer palette's foreground: a fixed light
        # grey vanished against the light theme's backdrop (stl_viewer's own
        # deviation window themes it the same way)
        text = (0.90, 0.90, 0.94)
        shell = getattr(self, "_shell_mod", None)
        if shell is not None:
            try:
                text = shell._hex_to_rgb(shell.palette()["fg"])
            except Exception:  # noqa: BLE001
                pass
        self._dev_bar = stl_viewer._deviation_scalar_bar(
            self._depo_dev_lut, text, self._depo_dev_unit)
        self.renderer.AddActor2D(self._dev_bar)
        self._deviation_on = True
        self._apply_depo_color()        # switch active scalar -> the deviation LUT
        # place the freshly rebuilt chunk actors in the machine frame (bed shift +
        # registration) AND reveal up to the clock -- _apply_time does both. Without
        # this the new actors sit at raw .depo coords (off the plate under a machine)
        # until the first scrub snapped them back; _reveal_deposition alone doesn't
        # position them.
        self._apply_time()
        self._refresh_buttons()

    def _ensure_depo_deviation(self, sidecar):
        """Make sure the deposition mesh exists AND carries this sidecar's deviation
        scalar, (re)building it once if needed, and show it. Returns True when the
        scalar is present (False -> a mismatch, so the caller keeps the plain view)."""
        need = (not self._depo_built or not getattr(self, "_depo_has_dev", False)
                or getattr(self, "_dev_baked_sidecar", None) != sidecar)
        if need:
            self._dev_bake_sidecar = sidecar
            self._depo_built = False        # force a fresh build carrying the scalar
            try:
                self._build_deposition()
            finally:
                self._dev_bake_sidecar = None
        self._depo_visible = True
        for a in self.depo_actors:
            a.SetVisibility(True)
        return getattr(self, "_depo_has_dev", False)

    def _clear_deviation(self, restore=True):
        """Turn the deviation view off: revert the deposition mesh to its plain (per-
        scaffold / solid) colours. The mesh itself stays -- deviation is just a colour
        scheme on it now, not a separate actor -- so hiding it leaves the scaffold
        exactly as the reveal has it. `restore=False` skips the recolour (a fresh
        deviation about to replace it)."""
        if self._dev_bar is not None:
            self.renderer.RemoveActor2D(self._dev_bar)
            self._dev_bar = None
        was_on = self._deviation_on
        self._deviation_on = False
        if restore and was_on:
            self._apply_depo_color()        # back to per-scaffold / solid colour
            self._refresh_buttons()

    # -- in-scene micrograph overlay layer (embedded viewer) ----------- #
    def _micro_reference_bounds(self):
        """World-space bounds to seat an imported micrograph under: the deposited
        SCAFFOLD footprint at the collector's g-code origin, NOT the whole visible
        scene. With a machine loaded the scene is dominated by the printer body, so
        `ComputeVisiblePropBounds` centred + sized the image on the machine -- it
        landed huge, off under the whole machine. The scaffold's toolpath (complete,
        reveal-independent) shifted into world by the build-group delta the deposition
        actors carry gives the print footprint on the collector, exactly what the
        native viewer auto-fits to. Falls back to the whole scene if there's no path."""
        dep_idx = getattr(self, "dep_idx", None)
        if self.n_pts >= 2 and dep_idx is not None and len(dep_idx):
            # deposition vertices ONLY (the scaffold) -- excludes the origin vertex,
            # travel moves and the exit-lift, so the footprint is the print itself,
            # not the origin->part span or the Z=5 retract.
            vidx = np.unique(np.concatenate([dep_idx, dep_idx + 1]))
            dep = self.verts[vidx]
            delta = (np.asarray(self.depo_actors[0].GetPosition(), dtype=float)
                     if self.depo_actors else np.zeros(3))
            lo = dep.min(axis=0) + delta
            hi = dep.max(axis=0) + delta
            if hi[0] > lo[0] or hi[1] > lo[1]:
                return (lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])
        return self.renderer.ComputeVisiblePropBounds()

    def _on_micrograph_msg(self, msg):
        """The GUI pushed a micrograph image path DOWN (apply-in-place): load it,
        place it under the scaffold at auto-fit (or the pinned mm/px scale), and --
        on first use -- add a Micrograph sidebar section (opacity / move / scale /
        rotate) via a rail rebuild. An empty path removes the overlay."""
        import micrograph_viewer as MV
        path = msg.get("image") or ""
        if not path or not os.path.isfile(path):
            self._remove_micrograph()
            self.window.Render()
            return
        rgb = MV.load_image_rgb(path)
        if rgb is None:
            self.hud.SetInput("  Could not load the micrograph image  ")
            self.window.Render()
            return
        h_px, w_px = int(rgb.shape[0]), int(rgb.shape[1])
        bb = self._micro_reference_bounds()             # scaffold footprint (world)
        diag = max(math.dist((bb[0], bb[2], bb[4]), (bb[1], bb[3], bb[5])), 1e-6)
        sw = max(bb[1] - bb[0], 1e-6)
        sh = max(bb[3] - bb[2], 1e-6)
        fit_s = min(sw / w_px, sh / h_px) if (w_px and h_px) else 0.001
        cx, cy = (bb[0] + bb[1]) / 2.0, (bb[2] + bb[3]) / 2.0
        fit_z = bb[4] - 0.002 * diag
        # ...but never under the plate's top face: there the opaque collector
        # hid the whole image, so an import looked like it did nothing
        plate = getattr(self, "plate_actor", None)
        if plate is not None:
            top = plate.GetBounds()[5]
            if math.isfinite(top):
                fit_z = max(fit_z, top + 0.0025 * diag)
        try:
            scale_mmpx = float(msg.get("scale_mmpx"))
        except (TypeError, ValueError):
            scale_mmpx = 0.0
        sx = sy = scale_mmpx if scale_mmpx > 0 else fit_s
        self._remove_micrograph()               # replace any prior overlay
        tex = MV.image_texture(rgb)
        actor = MV.image_plane_actor(w_px, h_px, tex)
        self.renderer.AddActor(actor)
        self._micro_actor = actor
        self._micro_state = MV._ImageState(actor, self.window, sx, sy, 0.0, cx, cy,
                                           fit_z, opacity=0.85, pw=w_px, ph=h_px)
        self._micro_span = diag                 # slider ranges (origin/scale)
        self._micro_on = True
        self._add_micrograph_controls(bb, diag)
        self.window.Render()

    def _micro_set(self, field, v):
        """Apply a Micrograph slider to the live image state (no-op if removed)."""
        st = self._micro_state
        if st is None:
            return
        try:
            setattr(st, field, float(v))
        except (TypeError, ValueError):
            return
        st.apply()

    def _add_micrograph_controls(self, bb, diag):
        """Add the Micrograph sidebar section (once) -- opacity / origin X / origin
        Y / scale / rotation sliders + a Remove button -- then re-send the hello so
        the embedded rail rebuilds with them. The slider closures read
        self._micro_state dynamically, so a re-imported image reuses them."""
        c = getattr(self, "_chrome", None)
        if (c is None or not self._themed or self.embed is None
                or self._micro_ctrls_added):
            return
        span = diag
        before = len(c.controls)
        c.section("Micrograph")
        c.slider("Image opacity", lambda: self._micro_val("opacity", 0.85),
                 0.0, 1.0, lambda v: self._micro_set("opacity", v))
        c.slider("Origin X (mm)", lambda: self._micro_val("ox", 0.0),
                 bb[0] - span, bb[1] + span, lambda v: self._micro_set("ox", v))
        c.slider("Origin Y (mm)", lambda: self._micro_val("oy", 0.0),
                 bb[2] - span, bb[3] + span, lambda v: self._micro_set("oy", v))
        c.slider("Origin Z (mm)", lambda: self._micro_val("z", 0.0),
                 bb[4] - span, bb[5] + span, lambda v: self._micro_set("z", v))
        cur_s = max(getattr(self._micro_state, "sx", 1e-3), 1e-6)
        c.slider("Scale (mm/px)", lambda: self._micro_val("sx", cur_s),
                 cur_s * 0.2, cur_s * 5.0, lambda v: self._micro_set_scale(v))
        c.slider("Rotation (deg)", lambda: self._micro_val("rot", 0.0),
                 -180.0, 180.0, lambda v: self._micro_set("rot", v))
        c.button(lambda: "Exit micrograph", self._exit_micrograph)
        c.layout()
        # the control ids just added, so "Exit micrograph" can remove the whole
        # section from the sidebar (not just the image actor)
        self._micro_ctrl_ids = [c.controls[i]["id"]
                                for i in range(before, len(c.controls))]
        self._micro_ctrls_added = True
        self.embed.resend_hello()

    def _exit_micrograph(self):
        """Fully leave the micrograph layer: remove the image actor AND take the
        Micrograph sidebar section away (bug5 -- "Remove image" left the controls).
        Re-importing later re-adds the section."""
        self._remove_micrograph()
        c = getattr(self, "_chrome", None)
        ids = getattr(self, "_micro_ctrl_ids", None)
        if c is not None and ids and hasattr(c, "remove_controls"):
            c.remove_controls(ids)
            self.buttons = c.buttons            # keep the class's ref in step
            self._micro_ctrl_ids = []
            self._micro_ctrls_added = False
            if self.embed is not None:
                self.embed.resend_hello()
        self.window.Render()

    def _micro_val(self, field, default):
        st = self._micro_state
        return float(getattr(st, field, default)) if st is not None else default

    def _micro_set_scale(self, v):
        """Scale slider drives BOTH sx and sy (uniform), so the micrograph never
        distorts from the sidebar."""
        st = self._micro_state
        if st is None:
            return
        try:
            st.sx = st.sy = float(v)
        except (TypeError, ValueError):
            return
        st.apply()

    def _remove_micrograph(self):
        """Remove the micrograph overlay actor (the sidebar section stays; the
        sliders go inert until another image is imported)."""
        if self._micro_actor is not None:
            self.renderer.RemoveActor(self._micro_actor)
            self._micro_actor = None
        self._micro_state = None
        self._micro_on = False

    def _toggle_depo_color(self):
        """Flip the deposition mesh between per-scaffold colours and one solid
        colour. The preference is remembered even before the (lazy) mesh is
        built, then honoured by _build_deposition / _apply_depo_color."""
        if not self._depo_can_color:
            return
        self._depo_colored = not self._depo_colored
        if self._depo_built:
            self._apply_depo_color()

    def _apply_depo_color(self):
        """Colour every deposition chunk by the active scheme, in precedence order:
        deviation (the LUT-mapped cross-track scalar) while "Show deviation" is on,
        else per-scaffold colours (when the mesh carries them and the toggle is on),
        else the single solid colour. Touches only colour + the mapper's active
        scalar, never the per-frame reveal or actor visibility -- so the deviation
        view rides the SAME progressive reveal as the plain deposition mesh."""
        dev_on = self._deviation_on and getattr(self, "_depo_has_dev", False)
        col_on = self._depo_colored and self._depo_has_colors
        for a in self.depo_actors:
            m = a.GetMapper()
            poly = m.GetInput()
            if dev_on:
                poly.GetPointData().SetActiveScalars("deviation")
                m.SetScalarModeToUsePointData()
                # the deviation array is already LUT-mapped RGBA (see
                # _install_depo_mesh), so the mappers pass it through instead of
                # each running the LUT over the whole mesh
                m.SetColorModeToDirectScalars()
                m.ScalarVisibilityOn()
            elif col_on:
                poly.GetPointData().SetActiveScalars("scaffold_rgb")
                m.SetScalarModeToUsePointData()
                m.SetColorModeToDirectScalars()         # uint8 RGB as literal colour
                m.ScalarVisibilityOn()
            else:
                m.ScalarVisibilityOff()
                a.GetProperty().SetColor(*self._solid_rgb())

    def _fiber_rgb255(self):
        """The deposited fibre's base colour as a 0..255 RGB triple.

        `--fiber-color` when it parses, else `SCAFFOLD_RGB255`. This is the
        colour a fibre gets when no `--scaffold-groups` entry claims it, which
        on a mandrel or a curved collector is EVERY fibre -- those two pages
        send no group list, so before this hook the designer's colour reached
        their preview and stopped there while the player drew the whole part in
        the solid off-white. It is also the per-group fallback, so a plate run
        whose scaffold left the colour field blank picks this up too."""
        rgb = _hex_to_rgb255(self.fiber_opts.get("fiber_color"))
        return rgb if rgb is not None else SCAFFOLD_RGB255

    def _fiber_rgb(self):
        """`_fiber_rgb255` as VTK's 0..1 floats -- the solid actor colour."""
        return tuple(c / 255.0 for c in self._fiber_rgb255())

    def _solid_rgb(self):
        """The solid actor colour under the "Scaffold colors" switch: the run's
        colour while it is on, the default off-white once it is turned off --
        what a plate run's "Solid color" has always drawn, since a plate sends
        no --fiber-color."""
        rgb = self._fiber_rgb255() if self._depo_colored else SCAFFOLD_RGB255
        return tuple(c / 255.0 for c in rgb)

    @staticmethod
    def _resolve_plate_path(plate, stl_path, gcode_path):
        """Locate the grey build-plate sibling (<stem>_plate.stl): the explicit
        --plate-stl path first (the GUI's last_plate -- the ONLY finder for a
        g-code import at a preview quality, where the plate sits next to the -o
        target in the session folder while the g-code is elsewhere and no STL was
        built), then next to the STL, then next to the g-code (a run with no STL,
        e.g. string art, can still show one). None -> no plate sibling (no toggle).
        Mirror of _find_depo_cache; kept static so it is unit-testable without the
        VTK Player."""
        if plate and os.path.isfile(plate):
            return plate
        if stl_path:
            cand = os.path.splitext(stl_path)[0] + "_plate.stl"
            if os.path.isfile(cand):
                return cand
        cand = os.path.splitext(gcode_path)[0] + "_plate.stl"
        return cand if os.path.isfile(cand) else None

    def _find_depo_cache(self):
        """Path of the deposition sidecar the converter may have written: the
        explicit --depo path first (the GUI's last_depo -- the ONLY finder for a
        g-code import at a preview quality, where the cache sits next to the -o
        target in the session folder while the g-code lives elsewhere and no STL
        exists; see __init__), then next to the STL, then next to the g-code.
        None -> no cache (the locked sim rebuilds from the commanded g-code)."""
        cands = []
        if getattr(self, "depo_path", None):
            cands.append(self.depo_path)
        if self.stl_path:
            cands.append(os.path.splitext(self.stl_path)[0] + DEPO_CACHE_EXT)
        cands.append(os.path.splitext(self.gcode_path)[0] + DEPO_CACHE_EXT)
        for c in cands:
            if os.path.isfile(c):
                return c
        return None

    def _depo_staleness_ref(self):
        """The g-code whose mtime may invalidate the cache -- or None when `--depo`
        named the cache explicitly.

        `load_depo_cache` drops a sidecar older than its g-code, on the reasoning
        that the g-code must have been re-edited since. That is right for the
        IMPLICIT search above, which pairs the two by naming convention and could
        easily find a stale neighbour. It is wrong for an explicit `--depo`, where
        the caller is asserting the pairing -- and in machine-live it is wrong every
        single time, because the source file is a FRESH DOWNLOAD of the printer's
        copy. Same bytes, new mtime, so the cache always looked stale and the viewer
        always re-solved the sag: measured on a 10x10 mm 20-layer scaffold, 7.62 s to
        open against 2.55 s, with nothing differing but the two timestamps.

        The pairing is not taken on trust: the GUI only passes `--depo` after
        comparing the files BYTE FOR BYTE (`_ml_depo_for_source`), which is strictly
        stronger than an mtime, and `load_depo_cache` still checks every solve
        parameter in the cache's own meta."""
        return None if getattr(self, "depo_path", None) else self.gcode_path

    @contextlib.contextmanager
    def _physics_fiber_transform(self):
        """Install the physics deposition transform into the converter's fibre seam
        for the duration of a from-g-code mesh rebuild, then remove it.

        This is the same `mew_deposition.make_transform` install `mew_export` does
        before it writes an STL, driven from the knobs this player already carries --
        so a mesh rebuilt here matches the exported one instead of being the raw
        commanded path. A no-op (and byte-identical to the old behaviour) unless
        `--physics-deposit` is on and the physics modules are present; any failure
        degrades to the commanded rebuild rather than losing the mesh.

        `fill_per_tau` is passed for the same reason every other knob is: 93dc508
        named four consumers of the Corner-resolution field and wired three, so
        this rebuild refined at the hardcoded 6 while `mew_export` (:169) baked
        the `.depo` at the field and the live march (`_init_live_relay`) marched
        at it -- the one path where the rebuilt mesh did NOT match the exported
        one, at any setting but 6.

        Yields whether the transform was actually installed. `_build_deposition`
        passes that straight to `_adopt_laid_jet_source`, because it is the same
        question as "does this laid geometry carry the lag?" -- see
        `_set_jet_source`, which would otherwise have to guess it."""
        installed = False
        try:
            if self.physics_deposit and _HAVE_PHYSICS and self.model is not None:
                try:
                    transform = _make_depo_transform(
                        self.model, pressure=self.pressure, voltage=self.voltage,
                        temperature=self.temperature,
                        zeta=self.model.calibration.contact_damping_zeta,
                        dynamic_diameter=self.dynamic_diameter, coil=self.coil,
                        coil_wave_amp=self.coil_wave_amp,
                        coil_wave_len=self.coil_wave_len,
                        max_accel=self.max_accel,
                        square_corner_velocity=self.scv,
                        rotary_accel=self.rotary_accel,
                        fill_per_tau=self.fill_per_tau,
                        corner_chord=self.corner_chord,
                        simulate_travel=bool(
                            self.fiber_opts.get("render_travel", False)))
                    set_fiber_transform(
                        transform,
                        with_travels=bool(
                            self.fiber_opts.get("render_travel", False)),
                        with_frame=True)
                    installed = True
                except Exception as exc:  # noqa: BLE001 -- fall back, never fail
                    sys.stderr.write("gcode_player: physics deposition transform "
                                     "unavailable, drawing the commanded fibre "
                                     "(%s)\n" % exc)
            yield installed
        finally:
            if installed:
                set_fiber_transform(None)

    def _build_deposition(self):
        """Rebuild the sagged fiber mesh from the g-code with per-vertex print
        times, then pre-sort its triangles by the time their LAST vertex is laid
        down so revealing up to the playhead is a contiguous prefix. The sorted
        triangles are split into fixed-size time-chunks, each its own actor:
        fully-deposited chunks are uploaded once and never touched again, so the
        per-frame reveal only rebuilds the single chunk straddling the head
        (bounded cost) instead of the whole revealed part (which grew without
        bound and made later layers lag)."""
        import mew_frame as _g2f_frame     # NonDevelopableFrame / ChartUnusable, below
        self._depo_built = True       # one attempt; don't retry on a failure
        # The build is synchronous and GIL-bound (the sag solve is Python-heavy)
        # so it briefly blocks the UI; draw a notice first so the freeze is
        # explained rather than mysterious.
        # machine-live has no HUD (the Machine-link page's form owns the readout),
        # and it builds this SAME mesh for the live reveal -- so guard it
        if self.hud is not None:
            self.hud.SetInput("  Building deposition view (one-time)...  ")
        self.window.Render()
        # A CURVED COLLECTOR is not refused here. `build_deposition_mesh` takes
        # `frame=` / `five_axis=` (the same pair `main` takes) and the CACHE
        # path below needs no frame at all -- the `.depo` is written in world
        # space, AFTER the wrap, so a cone or torus `.depo` from Generate tubes
        # here exactly like a flat or mandrel one. What can still be refused,
        # further down, is a NO-CACHE rebuild on a collector whose chart guard
        # raises (a dome/bowl) -- a per-attempt exception, not a standing
        # refusal on every curved collector.
        opts = self.fiber_opts
        diameter = float(opts.get("diameter", 0.02))
        # the export's own facet count, and the tube's. They differ: the tube is
        # floored at DEPO_FACES so the locked preview is never coarser than the
        # STL (else fat crossovers float / poke), while the DECIMATION has to keep
        # the export's count, because that is the tolerance the STL on disk was
        # thinned to (facet_error falls as facets rise, so thinning at the floored
        # count would keep rings the STL dropped -- and would miss the companion
        # the export left, whose key records that same count).
        stl_faces = max(3, int(opts.get("faces", DEPO_FACES)))
        faces = max(stl_faces, DEPO_FACES)
        max_ring_dt = float(opts.get("max_ring_dt", 0.03))
        no_sag = bool(opts.get("no_sag", False))
        # the Export panel's "Mesh detail", so the exact mesh is thinned to the
        # SAME tolerance the STL was (see build_deposition_mesh's docstring). Only
        # the exact path uses it -- the wireframe/tube previews have their own,
        # much coarser, preview thinning in _decimate_paths.
        # --no-sag zeroes it, because the exporter skips the thinning in that mode
        # too (gcode_to_fibers.main: `and not args.no_sag`): it renders exactly
        # what was commanded, as the byte-identical reference a solver diff needs.
        mesh_tolerance = (0.0 if no_sag
                          else float(opts.get("mesh_tolerance", 0.0) or 0.0))
        sag_approx = bool(opts.get("sag_approx", False))
        sag_multiplier = float(opts.get("sag_multiplier", 1.0))
        sag_reset_layers = int(opts.get("sag_reset_layers", 0) or 0)
        arc_segments = opts.get("arc_segments")     # STL arc fidelity; None = default
        # Deposition render quality (set before launch from the GUI dropdown):
        # "wireframe" draws the sagged centrelines as lines (instant), "tube:N"
        # grows them into N-sided tubes with vtkTubeFilter (fast), "exact" tubes
        # every sagged point (the full mesh, with physics dynamic diameter).
        render = self._render
        sides = _render_sides(render)
        # Honor the user's chosen tube fidelity: "Tubes - fast" (tube:6) renders 6
        # facets, "Tubes - smooth" (tube:12) renders 12. The old floor at DEPO_FACES
        # silently forced tube:6 up to 12 -- it hid a sub-fibre polygonal-inset gap at
        # fat crossovers, but in doing so disabled the fast tube entirely. That gap is a
        # cosmetic detail the user opts out of by choosing the fast tube, so let the
        # chosen count stand (_render_sides already floors at 3, a valid tube). The
        # exact-mesh path still uses the DEPO_FACES-floored `faces`. 0 = wireframe.
        is_lines = (render == "wireframe")
        # Same-layer self-contact is auto-disabled for string-art g-code (a
        # coplanar path with thousands of self-crossings hangs the build); the
        # marker lives in the file, so detect it the same way the converter did
        # -- this also keeps the cache key in sync with the sidecar it wrote.
        self_crossings = not file_skips_self_crossings(self.gcode_path)
        try:
            # Fast path: the converter wrote the sagged fibers to a sidecar when
            # it made the STL, so we skip the (multi-second) sag solve and just
            # tube them. Falls back to computing from the g-code when there's no
            # matching cache (e.g. a hand-launched player or string art).
            cached = load_depo_cache(
                self._find_depo_cache(), diameter=diameter, no_sag=no_sag,
                sag_approx=sag_approx,
                sag_multiplier=sag_multiplier, self_crossings=self_crossings,
                sag_reset_layers=sag_reset_layers,
                render_travel=bool(opts.get("render_travel", False)),
                gcode_path=self._depo_staleness_ref(),
                transformed=self.physics_deposit,
                phys_sag=self.physical_sag, phys_fuse=self.physical_fusion)
            _perf("depo_load")
            # per-scaffold fiber diameters / colours (multi-scaffold runs);
            # None -> the single diameter / a solid colour, exactly as before
            groups = opts.get("scaffold_groups")
            cell_k, is_lines_used = 3, is_lines
            if cached is not None:
                paths, ptimes = cached
                # per-fiber diameter from the cache, so every mode tubes each
                # scaffold at ITS diameter (multi-diameter stacks render right)
                pdia = load_depo_pdia(self._find_depo_cache())
                # per-scaffold colours for the preview meshes too (multi-scaffold
                # runs whose groups carry a colour) -- so the wireframe/tube results
                # viewer colours each scaffold like the exact mesh and the STL
                # viewer, instead of one solid grey. None -> stays solid, as before.
                path_color = path_colors_from_groups(
                    groups, len(paths), self._fiber_rgb255())
                dec = None
                if render.startswith("tube:") and sides >= 3:
                    # the quality chosen is a wish, not a promise: a mesh that
                    # does not fit aborts the viewer (VTK has no failed-alloc
                    # exception), so step down to the finest one that does
                    dec = _decimate_paths(paths, ptimes, diameter, pdia)
                    _perf("decimate")
                    sides = self._tube_sides_that_fit(dec[0], sides)
                    if sides == 0:
                        render, is_lines = "wireframe", True
                        is_lines_used = True
                if render == "wireframe":
                    # preview-only centreline thinning (see _decimate_paths): the
                    # screen can't resolve the sag solver's sub-fibre sampling
                    dpaths, dptimes = dec or _decimate_paths(
                        paths, ptimes, diameter, pdia)
                    verts = np.concatenate(dpaths).astype(np.float64)
                    cells = _line_cells(dpaths)
                    vtimes = np.concatenate([np.asarray(t, dtype=np.float64)
                                             for t in dptimes])
                    normals = None
                    # one RGB per fibre, repeated over that fibre's kept points
                    # (decimation preserves fibre count/order, so path_color aligns)
                    colors = (np.concatenate(
                        [np.tile(np.asarray(path_color[i], dtype=np.uint8),
                                 (len(p), 1)) for i, p in enumerate(dpaths)])
                        if path_color is not None else None)
                    cell_k = 2
                elif sides >= 3:
                    # preview-only centreline thinning before tubing (see
                    # _decimate_paths): ~15-30x fewer tube vertices/triangles,
                    # visually exact; the .depo cache and exported STL are untouched
                    dpaths, dptimes = dec or _decimate_paths(
                        paths, ptimes, diameter, pdia)
                    _perf("decimate")
                    verts, cells, vtimes, normals, colors = _tube_via_filter(
                        dpaths, dptimes, diameter, sides, pdia=pdia,
                        path_color=path_color)
                    _perf("tube_filter")
                else:                                   # exact: full sagged mesh
                    # physics dynamic diameter: if the export stored a diameter-vs-
                    # time lookup, tube each fibre with a speed-dependent (varying)
                    # radius so the mesh thickens on slow loops -- matching the jet
                    diam_by_time = load_depo_diam_by_time(self._find_depo_cache())
                    # the decimation the EXPORT already solved for the STL, if it
                    # left one beside the cache and it was solved at the settings
                    # in force now; None -> _tube_fibers computes it (see
                    # load_depo_decimation)
                    _dp = self._find_depo_cache()
                    dec_idx = load_depo_decimation(
                        _dp, mult=mesh_tolerance, faces=stl_faces, paths=paths)
                    _perf(f"decim_cache hit={dec_idx is not None}")
                    if dec_idx is None and mesh_tolerance > 0.0 and _dp:
                        # Nothing to inherit -- a PREVIEW run (--no-mesh) writes
                        # the .depo but never builds an STL, so the export had no
                        # reason to decimate and left no companion. Solve it here
                        # and leave it behind, so this is paid once per scaffold
                        # instead of once per viewer open.
                        dec_idx = decimate_idx_for(
                            paths, ptimes, diameter, stl_faces, mesh_tolerance,
                            path_diam=pdia, diam_by_time=diam_by_time)
                        write_depo_decimation(
                            _dp, dec_idx, mult=mesh_tolerance, faces=stl_faces,
                            n_in=sum(len(p) for p in paths))
                        _perf("decim_solved")
                    verts, cells, vtimes, normals, colors = _tube_fibers(
                        paths, ptimes, diameter, faces, max_ring_dt,
                        path_diam=pdia, path_color=path_color,
                        diam_by_time=diam_by_time,
                        mesh_tolerance=mesh_tolerance, decimate_idx=dec_idx,
                        mesh_faces=stl_faces)
                    _perf("tube_exact")
            else:
                # No sidecar: rebuild from the g-code. With physics deposition on,
                # install the SAME centreline transform the exporter installs first --
                # otherwise this path silently yields the COMMANDED fibre, which looks
                # like a working scaffold but carries no jet-lag or corner rounding.
                # That is what the machine-link live view hits whenever the printer is
                # running a file this session did not export (so there is no matching
                # `.depo`), and the difference is invisible until you look for it.
                with self._physics_fiber_transform() as laid_reshaped:
                    mesh, laid = build_deposition_mesh(
                        self.gcode_path, diameter=diameter, faces=faces,
                        mode=opts.get("mode", "auto"), no_sag=no_sag,
                        sag_multiplier=sag_multiplier, max_ring_dt=max_ring_dt,
                        self_crossings=self_crossings, arc_segments=arc_segments,
                        sag_reset_layers=sag_reset_layers,
                        sag_approx=sag_approx,
                        mesh_tolerance=mesh_tolerance, mesh_faces=stl_faces,
                        scaffold_groups=groups,
                        scaffold_default_rgb=self._fiber_rgb255(),
                        # THE COLLECTOR (see build_deposition_mesh's docstring):
                        # both None on a flat/no-frame scene, byte-identical to
                        # before. A mandrel or curved surface with no `.depo` to
                        # ride now rebuilds into the SAME chart the export
                        # solved in, instead of the commanded path re-parsed
                        # flat -- the seam `build_deposition_mesh(frame=,
                        # five_axis=)` had no caller for.
                        frame=self._frame, five_axis=self._five,
                        return_paths=True)
                verts, cells, vtimes, normals, colors = mesh
                # Re-anchor the jet to the fibre this mesh was actually tubed from.
                # Until now the no-cache path left the jet on the COMMANDED vertices
                # while the mesh followed the laid centreline, so the jet bottom sat
                # on a curve the fibre no longer occupied -- it hung on the commanded
                # pass endpoints and stretched across the laid path until the next
                # linking loop yanked it back.
                self._adopt_laid_jet_source(laid, diameter,
                                            reshaped=laid_reshaped)
                is_lines_used = False
        except (_g2f_frame.NonDevelopableFrame, _g2f_frame.ChartUnusable) as exc:
            # A collector the sag solver still refuses (a dome/bowl until slice
            # 25's local chart lands) and no cached `.depo` to ride instead. The
            # commanded TOOLPATH is already on screen; only the sag solve is
            # missing, and saying so beats a bare traceback in stderr.
            self._depo_visible = False
            sys.stderr.write(
                "gcode_player: no sagged deposition mesh for this collector "
                f"yet ({exc}); the toolpath shown is the commanded path, "
                "without the sag solve.\n")
            if self.hud is not None:
                self.hud.SetInput("  Curved collector: toolpath shown, no sag "
                                  "mesh yet  ")
                self.window.Render()
            return
        except Exception as exc:  # noqa: BLE001 -- never let it kill playback
            sys.stderr.write(f"gcode_player: deposition build failed: {exc}\n")
            return
        if len(cells) == 0:
            sys.stderr.write("gcode_player: no deposited fibers to animate\n")
            return
        # Optional deviation baking (the locked sim's "Show deviation"): a per-vertex
        # scalar colouring every mesh vertex by how far the predicted fibre lands from
        # the commanded path, so the SAME reveal grows the deviation view instead of a
        # separate whole-scaffold actor with a parked clock. Interpolated over PRINT
        # TIME (the .depo centreline time base) using the un-remapped vtimes below, so
        # it is agnostic to the tube/wireframe/exact tessellation and preview thinning.
        dev_bundle = None
        side = getattr(self, "_dev_bake_sidecar", None)
        if side:
            dev_bundle = self._deviation_vertex_scalar(side, vtimes)
        # the mesh's per-vertex reveal times (from the cache or a fresh sag) are on
        # the naive length/feed clock; carry them onto the player's accel-limited
        # clock so the growing mesh reveals in lockstep with the kinematic playhead.
        vtimes = self._to_clock(vtimes)
        self._install_depo_mesh(verts, cells, vtimes, normals, colors,
                                cell_k=cell_k, is_lines=is_lines_used,
                                dev=dev_bundle)
        _perf(f"install_depo cells={len(cells)}")
        self._dev_baked_sidecar = side if dev_bundle is not None else None

    def _install_depo_mesh(self, verts, cells, vtimes, normals, colors,
                           cell_k=3, is_lines=False, dev=None):
        """Upload a freshly built deposition mesh as time-chunked actors. Tears down
        any existing chunks first, pre-sorts the cells by the time their LAST vertex
        lands (so the revealed prefix stays contiguous), then splits them into
        fixed-size time-chunks, each its own actor -- fully-deposited chunks upload
        once and are never touched again, so the per-frame reveal only rebuilds the
        chunk straddling the head.

        `cell_k` / `is_lines` make it geometry-agnostic: 3-vertex TRIANGLES (tube /
        exact -- the default, and what the live-adjust re-lay passes) or 2-vertex
        LINES (the wireframe render quality). The live-adjust caller passes neither,
        so its triangle path is unchanged.

        Shared by the one-time first build and the real-time re-lay; the teardown is
        what lets the re-lay rebuild the whole mesh each settled edit (the already-
        laid part is reproduced identically, so the revealed scaffold doesn't jump).
        New actors take the CURRENT _depo_visible, so a re-lay of a shown mesh stays
        shown while the first build (visible flipped on by the toggle afterwards)
        stays hidden until then."""
        for a in self.depo_actors:               # re-lay rebuilds from scratch
            self.renderer.RemoveActor(a)
        self.depo_actors = []
        self._depo_chunks = []
        self._depo_last_n = -1                   # force a reveal pass on the new mesh
        if len(cells) == 0:
            self._depo_conn = self._depo_sorted_tt = None
            return

        # a cell appears only once ALL its vertices are down, so it never pokes
        # ahead of the head; sorting by that time makes "revealed so far" a
        # contiguous prefix. K = 3 (triangles) or 2 (wireframe lines).
        #
        # Kept compact, because this is most of what the viewer holds per drawn
        # triangle: the sorted connectivity as int32 (12 B, not 24), the times
        # as float32 (4 B, not 8), and ONE copy of each -- the chunks draw views
        # of `_depo_conn` rather than copies (see _reveal_deposition). float32
        # steps 0.24 ms at an hour of print, against a 30 ms reveal cadence
        # (_REVEAL_DT); the travel mask below is computed in float64 first, so
        # its span boundaries are exact. The max over the K columns is taken
        # column by column so no (C, K) float64 temporary is built.
        cells = np.asarray(cells)
        cell_t = vtimes[cells[:, 0]]
        for j in range(1, cells.shape[1]):
            np.maximum(cell_t, vtimes[cells[:, j]], out=cell_t)
        order = np.argsort(cell_t, kind="stable")
        scells = np.ascontiguousarray(cells[order],
                                      dtype=_conn_dtype(len(verts)))
        del cells                                # this frame's reference
        self._depo_sorted_tt = np.ascontiguousarray(cell_t[order], dtype=np.float64)
        del cell_t, order
        # Which of those cells are TRAVEL fibre, in the same sorted order, so
        # "Hide travels" is a mask over the revealed prefix rather than a
        # second mesh. `(t0, t1]` and not `[t0, t1)`: a cell is timed by its LAST
        # vertex, so the travel's closing ring lands exactly on t1 while the
        # preceding fibre's closing ring lands exactly on t0.
        self._depo_cell_trv = None
        if self._travel_spans:
            # t is in some span (lo, hi] exactly when the largest hi among the
            # spans with lo < t is >= t: spans sorted by lo, a running max of
            # hi, one searchsorted. The cells x spans matrix this replaces was
            # 3.3 GB for a 150-layer 10 mm scaffold (23.4 M cells x 151
            # travels) and stopped the viewer at Fibers quality.
            _st = self._depo_sorted_tt
            _sp = np.asarray(self._travel_spans, dtype=np.float64).reshape(-1, 2)
            _sp = _sp[np.argsort(_sp[:, 0], kind="stable")]
            _reach = np.maximum.accumulate(_sp[:, 1])
            _i = np.searchsorted(_sp[:, 0], _st, side="left") - 1
            self._depo_cell_trv = (_i >= 0) & (
                _st <= _reach[np.maximum(_i, 0)])
            del _st, _i
        # ...and only now narrowed, once the exact float64 answer is in the mask.
        # The reveal compares in float32 too (see _reveal_deposition).
        self._depo_sorted_tt = self._depo_sorted_tt.astype(np.float32)
        self._depo_cell_k = int(cell_k)
        self._depo_is_lines = bool(is_lines)
        self._depo_conn = scells.reshape(-1)           # sorted connectivity
        n_cells = len(scells)
        del scells
        # One offsets array serves every chunk: each chunk's cells start at its
        # own slice of `_depo_conn`, so its offsets are always 0, K, 2K, ...
        # and a chunk never holds more than DEPO_CHUNK_TRIS cells.
        _k = int(cell_k)
        self._depo_offsets = np.arange(
            0, _k * (min(n_cells, DEPO_CHUNK_TRIS) + 1), _k,
            dtype=self._depo_conn.dtype)

        # shared points (float32 -> half the GPU upload/footprint vs float64); tube
        # / exact carry analytic normals, wireframe needs none (lighting off).
        pts = vtk.vtkPoints()
        pts.SetData(numpy_support.numpy_to_vtk(
            np.ascontiguousarray(verts, dtype=np.float32), deep=1))
        vnorm = None
        if normals is not None and len(normals):
            vnorm = numpy_support.numpy_to_vtk(
                np.ascontiguousarray(normals, dtype=np.float32), deep=1)
        # per-scaffold colours (exact mode, multi-scaffold): one shared per-vertex
        # colour array. None for wireframe/tube (single colour) -> a solid fallback.
        #
        # Stored RGBA, not RGB, and that ALPHA CHANNEL IS THE WHOLE POINT. Sharing
        # the array between the chunk mappers shares the object, not the work:
        # vtkMapper::MapScalars runs per mapper, and with direct scalars anything
        # that is not already 4-component uint8 has to be converted -- so every one
        # of the ~200 chunks built its own full-length RGBA copy of the WHOLE mesh's
        # colours and uploaded its own VBO. Measured on a 12M-triangle scaffold
        # (.perf/render_cost.py, and the same A/B through the real player): 3-comp
        # 2.96 s and 7,292 MB, 4-comp 0.45 s and 1,828 MB, framebuffers identical to
        # the pixel. Pre-padding here is what makes the shared array actually shared.
        vcol = None
        if colors is not None:
            rgba = np.empty((len(colors), 4), dtype=np.uint8)
            rgba[:, :3] = colors
            rgba[:, 3] = 255
            vcol = numpy_support.numpy_to_vtk(rgba, deep=1)
            vcol.SetName("scaffold_rgb")
        self._depo_has_colors = vcol is not None
        # optional deviation scalar (one value per vertex, in verts order): a named
        # point-data array carried alongside the RGB/normals so "Show deviation" can
        # switch the mapper's active scalar to it (see _apply_depo_color). Not the
        # active scalar by default, so the plain scaffold is unchanged until toggled.
        vdev, self._depo_has_dev = None, False
        if dev is not None:
            dvals, self._depo_dev_lut, dtop, self._depo_dev_unit = dev
            self._depo_dev_range = (0.0, float(dtop))
            # The LUT is applied ONCE, here, rather than left to the mappers. Same
            # reason the scaffold colours are padded to RGBA above: with
            # SetColorModeToMapScalars each chunk mapper runs the LUT over the whole
            # mesh's scalars and keeps its own RGBA copy, so the cost is paid ~200
            # times. Baking it gives every chunk one 4-component uint8 array to pass
            # straight through. Measured on 12M triangles: 3.64 s / 7,396 MB ->
            # 0.45 s / 1,947 MB, framebuffer identical to the pixel
            # (.perf/render_cost.py --colors lut vs --colors lutrgba).
            # The LUT itself is kept: the colour-key bar still draws from it.
            self._depo_dev_lut.SetTableRange(*self._depo_dev_range)
            _scal = numpy_support.numpy_to_vtk(
                np.ascontiguousarray(dvals, dtype=np.float64), deep=1)
            _mapped = self._depo_dev_lut.MapScalars(
                _scal, vtk.VTK_COLOR_MODE_MAP_SCALARS, -1)
            vdev = numpy_support.numpy_to_vtk(
                numpy_support.vtk_to_numpy(_mapped).copy(), deep=1)
            vdev.SetName("deviation")
            self._depo_has_dev = True

        for lo in range(0, n_cells, DEPO_CHUNK_TRIS):
            hi = min(lo + DEPO_CHUNK_TRIS, n_cells)
            poly = vtk.vtkPolyData()
            poly.SetPoints(pts)                         # shared across chunks
            if vnorm is not None:
                poly.GetPointData().SetNormals(vnorm)
            if is_lines:
                poly.SetLines(vtk.vtkCellArray())       # filled per frame
            else:
                poly.SetPolys(vtk.vtkCellArray())
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(poly)
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(*self._solid_rgb())  # solid fallback
            if is_lines:
                actor.GetProperty().SetLighting(False)  # flat bright wires
                actor.GetProperty().SetLineWidth(1.0)
            if vcol is not None:
                poly.GetPointData().SetScalars(vcol)    # shared across chunks
                mapper.SetScalarModeToUsePointData()
                mapper.SetColorModeToDirectScalars()    # uint8 RGB as literal colour
            if vdev is not None:
                poly.GetPointData().AddArray(vdev)      # not active until toggled
            actor.SetVisibility(self._depo_visible)     # re-lay keeps a shown mesh shown
            # the chunks are rebuilt on every re-lay, so the material has to be
            # (re)applied here, not only at toggle time
            if not is_lines:
                set_fibre_material(actor.GetProperty(),
                                   getattr(self, "_studio_light", False))
            self.renderer.AddActor(actor)
            self.depo_actors.append(actor)
            self._depo_chunks.append(
                {"lo": lo, "hi": hi, "poly": poly, "count": 0})
        # honour the colour toggle's current state (per-scaffold vs solid)
        self._apply_depo_color()

    def _reveal_deposition(self):
        """Show every fiber facet laid down by time self.t. Each chunk is filled
        to its share of the revealed prefix; chunks that are already complete (or
        still empty) are skipped, so only the chunk straddling the head is
        rebuilt -- bounded by DEPO_CHUNK_TRIS regardless of how far the print has
        progressed."""
        tt = self._depo_sorted_tt
        if tt is None:                 # no mesh installed (empty/degenerate build)
            return
        # reveal only up to the jet's CONTACT point (see _reveal_time), not the head
        # position: fiber exists where the jet has already touched down, so the
        # mesh's leading edge meets the jet instead of running out ahead of it -- or
        # trailing behind it through corners. At the very end the in-flight tail has
        # landed, so reveal the whole mesh.
        reveal_t = self._reveal_time()
        # in the array's own float32: a float64 key would make searchsorted
        # widen the whole (C,) array every frame
        n = int(np.searchsorted(tt, tt.dtype.type(reveal_t), side="right"))
        if self._live_tracking:
            # laid fibre is a fact too -- ratchet it, for the same reason the
            # commanded lines ratchet (see _reveal_high_water). Only while a print is
            # RUNNING: the idle view's whole-path pass is not a record of anything.
            n = self._live_cell_hi = max(self._live_cell_hi, n)
        if n == self._depo_last_n:
            return
        self._depo_last_n = n
        conn = self._depo_conn
        k = self._depo_cell_k                          # 2 (lines) or 3 (triangles)
        # None -> nothing to mask (no travel fibre in this mesh, or it is shown)
        trv = (None if self._travels_on else getattr(self, "_depo_cell_trv", None))
        for ch in self._depo_chunks:
            lo, hi = ch["lo"], ch["hi"]
            want = 0 if n <= lo else (hi - lo if n >= hi else n - lo)
            if want == ch["count"]:
                continue                               # unchanged (common case)
            ch["count"] = want
            ca = vtk.vtkCellArray()
            if want:
                if trv is None:
                    # a VIEW of the sorted connectivity, not a copy: SetData
                    # keeps the int32/int64 array it is handed, and
                    # numpy_to_vtk(deep=0) pins the numpy memory to it
                    sub = conn[k * lo: k * (lo + want)]
                    shown = want
                else:
                    # travel fibre hidden: drop those cells from this chunk's
                    # share of the prefix. `want` still counts the full prefix,
                    # so the skip-if-unchanged test above stays valid and only
                    # the chunk straddling the head is rebuilt, exactly as when
                    # nothing is masked. A toggle forces every chunk to refill
                    # once (see _toggle_travels).
                    keep = ~trv[lo: lo + want]
                    shown = int(keep.sum())
                    sub = np.ascontiguousarray(
                        conn.reshape(-1, k)[lo: lo + want][keep].reshape(-1))
                ca.SetData(
                    numpy_support.numpy_to_vtk(
                        self._depo_offsets[:shown + 1], deep=0),
                    numpy_support.numpy_to_vtk(sub, deep=0))
            if self._depo_is_lines:
                ch["poly"].SetLines(ca)
            else:
                ch["poly"].SetPolys(ca)
            ch["poly"].Modified()

    # -- event handlers ----------------------------------------------------- #
    def _on_key(self, obj, _evt):
        key = obj.GetKeySym()
        actions = {
            "space": self._toggle_play, "Left": lambda: self._skip(-1),
            "Right": lambda: self._skip(1), "minus": self._slower,
            "plus": self._faster, "equal": self._faster,
            "r": self._restart, "R": self._restart,
            # no 's': the scaffold-STL overlay and its switch are gone (it drew a
            # second solid copy of the deposition mesh -- see _build_buttons)
            # 'p' still, for the muscle memory -- it is the COLLECTOR now, so on
            # a tube it takes the mandrel rod the same way it takes the plate
            "p": self._toggle_collector,
            "P": self._toggle_collector,
            "m": self._toggle_machine, "M": self._toggle_machine,
            "t": self._toggle_travels, "T": self._toggle_travels,
            "n": self._toggle_printhead, "N": self._toggle_printhead,
            "g": self._toggle_deposit_lines, "G": self._toggle_deposit_lines,
            "d": self._toggle_deposition, "D": self._toggle_deposition,
            "c": self._toggle_depo_color, "C": self._toggle_depo_color,
            # quit (restored after swallowing VTK's default 'e'/'q' exit shortcuts)
            "q": self._quit, "Q": self._quit, "Escape": self._quit,
        }
        # Live parameter editing is ONLY bound in real-time-adjustment mode, so in
        # the plain sim these keys do nothing -- a stray press can't mutate the run.
        # Each bumps a knob (lower = up, UPPER = down), re-solves the jet, and
        # schedules the deposited fibre to re-lay; 0 resets to the launch params.
        if self.live_adjust:
            actions.update({
                "v": lambda: self._bump("voltage", 0.5),
                "V": lambda: self._bump("voltage", -0.5),
                "f": lambda: self._bump("pressure", 6894.76),   # +1 psi
                "F": lambda: self._bump("pressure", -6894.76),  # -1 psi
                "h": lambda: self._bump("temperature", 5.0),
                "H": lambda: self._bump("temperature", -5.0),
                # print speed: [ slower, ] faster (multiplicative what-if)
                "bracketleft": lambda: self._bump_print_speed(1.0 / 1.25),
                "bracketright": lambda: self._bump_print_speed(1.25),
                "0": self._reset_params,
            })
        action = actions.get(key)
        if action:
            action()
            self._after_control()

    def _swallow_char(self, obj, _evt):
        """Abort every CharEvent so VTK's built-in single-key shortcuts never fire
        (see the observer setup in _build_scene). The player handles its keys on
        KeyPressEvent, which fires first, so nothing of ours is lost."""
        obj.GetCommand(self._char_tag).SetAbortFlag(1)

    def _quit(self):
        """Exit the player cleanly (the keyboard equivalent of closing the
        window)."""
        self.interactor.TerminateApp()

    def _skip(self, direction):
        # Arrow keys scrub the playhead g-code line by line: jump to the previous /
        # next source-line boundary. Pausing first (a scrub is an inspection) so the
        # clock doesn't immediately roll the frame off the line the user stepped to.
        lt = getattr(self, "_line_step_times", None)
        self.playing = False
        self._reveal_sim_tool()          # stepping is a sim interaction (--static)
        if lt is None or lt.size == 0:
            # no motion lines to step (e.g. a comment-only file): fall back to the
            # old ~2%-of-print nudge so the keys still do something
            step = max(1.0, 0.02 * self.total)
            self.t = min(max(self.t + direction * step, 0.0), self.total)
            return
        if direction > 0:
            i = int(np.searchsorted(lt, self.t, side="right"))
            self.t = float(lt[i]) if i < lt.size else self.total
        else:
            i = int(np.searchsorted(lt, self.t, side="left")) - 1
            self.t = float(lt[i]) if i >= 0 else 0.0

    def _on_click(self, obj, _evt):
        cx, cy = obj.GetEventPosition()
        for b, _label_fn, cb in self.buttons:
            bx, by = b.GetPosition()
            size = [0.0, 0.0]
            b.GetSize(self.renderer, size)
            if bx <= cx <= bx + size[0] and by <= cy <= by + size[1]:
                cb()
                self._after_control()
                obj.GetCommand(self._click_tag).SetAbortFlag(1)  # don't orbit
                break

    def _on_scrub_start(self, *_):
        self.playing = False

    def _on_scrub(self, obj, _evt):
        if self._sync_slider:
            return
        self._reveal_sim_tool()          # scrubbing is a sim interaction
        self.t = float(obj.GetRepresentation().GetValue())
        self._apply_time()
        self._refresh_buttons()
        self.window.Render()

    def _mount_fs_transport(self, fs_chrome):
        """Native fullscreen (embedded child): mount the timeline INSIDE the
        fullscreen window's own chrome. It must NOT be a GUI-side overlay: a
        cross-process topmost strip is invisible whenever this monitor-covering
        window holds the foreground -- DWM hands such a window the display
        directly (independent flip), bypassing composition, so no external
        window can reliably draw over it (diagnosed live: the strip
        kept z-order and WindowFromPoint ownership yet never reached the
        screen). Drawn by this window, the timeline can never be decoupled."""
        def _fmt(t):
            m, s = divmod(int(round(max(0.0, t))), 60)
            h, m = divmod(m, 60)
            return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        fs_chrome.transport(
            lambda: self.t, 0.0, max(self.total, 1e-6), self._fs_scrub,
            text_fn=lambda: f"{_fmt(self.t)} / {_fmt(self.total)}",
            play_label_fn=lambda: "Pause" if self.playing else "Play",
            on_play=self._toggle_play)
        fs_chrome.layout()      # place the new bar (the hook runs post-layout)

    def _fs_scrub(self, v):
        """The fullscreen transport track (press-jump / drag-scrub): the same
        contract as the panel scrubber -- pause and jump the clock. The chrome
        click handler renders after us."""
        self._reveal_sim_tool()
        self.playing = False
        self.t = min(max(float(v), 0.0), self.total)
        self._apply_time()
        self._refresh_buttons()

    def _embed_scrub(self, msg):
        """The embedded page's transport scrubber ("scrub" command): jump the
        clock, paused -- exactly what dragging the in-scene slider does."""
        try:
            t = float(msg.get("time", 0.0))
        except (TypeError, ValueError):
            return
        self._reveal_sim_tool()
        self.playing = False
        self.t = min(max(t, 0.0), self.total)
        self._apply_time()
        self._refresh_buttons()
        self.window.Render()

    def _after_control(self):
        self._apply_time()
        self._refresh_buttons()
        # a switch can show a body the clip range was never fitted to (the
        # printhead, the machine, the collector); only ever pushes the camera
        # back, which under parallel projection leaves the picture unchanged
        fit_clipping_range(self.renderer)
        self.window.Render()

    # -- machine-live mode (--machine-live): drive the bodies from a real -----
    #    printer's streamed toolhead position, no g-code / clock / deposition.
    # the streamed toolhead state lives in the pure `_LiveFeed`; these keep the
    # long-standing attribute names working for the rest of the player + the tests
    @property
    def _live_head(self):
        return self._live.head

    @_live_head.setter
    def _live_head(self, v):
        self._live.head = v

    def _on_livepos(self, msg):
        """Handle a {"t":"livepos", x,y,z, ts, hold} message from the GUI (fed from
        Moonraker). All the decision-making lives in the pure `_LiveFeed` -- see its
        docstring for why a reported position is not always a position."""
        self._live.push(msg)

    def _on_filepos(self, msg):
        """Handle a {"t":"filepos", pos, state, hx,hy,hz} message: where the printer
        is IN THE FILE, its print state, and the work-origin offset H.

        Deliberately a separate message from `livepos`: position and file cursor are
        independent facts about the machine, and keeping them apart is what lets the
        reveal be wrong without the nozzle ever moving. Ignored unless a reveal source
        is loaded."""
        if not self._live_src:
            return
        if "pos" in msg:
            try:
                pos = int(msg["pos"])
            except (TypeError, ValueError):
                pos = None
            if pos is not None:
                # A fresh print rewinds the file: drop the monotonic latch so the
                # reveal grows from zero again instead of staying at the last print's
                # high-water mark. (Resuming from a pause does not rewind, so it is
                # left alone.)
                if self._live_filepos is not None and pos < self._live_filepos:
                    self._reset_reveal()
                self._live_filepos = pos
        if "state" in msg:
            self._live_state = msg.get("state")
        hx, hy, hz = msg.get("hx"), msg.get("hy"), msg.get("hz")
        if hx is not None and hy is not None and hz is not None:
            try:
                self._live_H = np.array([float(hx), float(hy), float(hz)], float)
            except (TypeError, ValueError):
                pass

    # A print is "live" (progressive reveal) only in these states. Anything else --
    # idle, complete, cancelled, an unknown state, no print at all -- shows the FULL
    # path instead of nothing, so the overlay is always there to check placement
    # against. An earlier attempt revealed only while printing and otherwise drew
    # nothing, which read on the rig as "I can't see the g-code at all".
    _LIVE_PRINT_STATES = ("printing", "paused")

    def _advance_live_cursor(self):
        """Set the playhead `self.t` from the printer's own file cursor.

        `virtual_sdcard.file_position` is a BYTE OFFSET into the file being printed,
        advanced by Klipper once per dispatched g-code line. `line_byte_starts`
        inverts that arithmetic exactly, and `line_to_time` maps the resulting line
        number onto this player's timeline. The line number is a JOIN KEY, not a
        clock: `t` lands in the player's own time base, so the printer's clock and
        the simulator's never have to be reconciled.

        Three earlier cursors each derived `t` from a quantity that cannot produce
        it, and each failed on the rig while passing offline: nearest-XY projection
        (ambiguous, because a scaffold stacks rotated near-identical layers, so a
        forward-only cursor ratchets to end-of-file); `print_stats.print_duration`
        (a different clock with a different origin and rate, so the error is
        systematic and unbounded); `virtual_sdcard.progress` (a byte fraction, not
        a time fraction -- a fast travel line and a slow deposition line weigh the
        same).

        The one residual is that `file_position` is the READER, running ahead of the
        toolhead by Klipper's lookahead. `_live_lead_max` bounds it, sized from THIS
        file (`_derive_live_bracket`): the rendered nozzle is projected onto the path
        within a strictly BACKWARD window from the file anchor, which cannot jump a
        layer and cannot ratchet.
        """
        if self.n_pts < 2 or self.total <= 0.0:
            return
        if (self._live_state not in self._LIVE_PRINT_STATES
                or self._live_filepos is None or self._byte_starts is None):
            self.t = self.total          # static overlay: the whole path, placed
            # ...and that pass is NOT a record of anything printed. Leaving the
            # ratchet holding it was the "whole scaffold appears at once" bug: the
            # view opens idle, draws the entire path once at t = total, the marks
            # latch at the last segment/cell, and every later frame of the real print
            # is then max()'d straight back up to the end. Measured on the looped
            # replay: the high-water mark sat at segment 1047 of 1048 from the first
            # frame of the print onwards. The static view needs no marks (nothing is
            # being cut), so drop them the moment it takes over.
            if self._live_tracking:
                self._live_tracking = False
                self._reset_reveal()
            return
        if not self._live_tracking:
            # a print has just started (or we connected mid-print): the marks and the
            # tracker anchor belong to whatever was on screen before, not to this
            self._live_tracking = True
            self._reset_reveal()
        line = byte_to_line(self._byte_starts, self._live_filepos)
        t_read = min(max(line_to_time(self.seg_line, self.times, line), 0.0),
                     self.total)
        # The playhead FOLLOWS THE NOZZLE, with no latch. Monotonicity is a property
        # of what is DRAWN, not of the clock, and it is enforced there instead (see
        # `_reveal_high_water`). Latching the clock looked equivalent and is not: on a
        # real scaffold one long pass is a SINGLE g-code line, so `file_position` --
        # and with it t_read -- does not move for the pass's whole duration, and the
        # nozzle projection is the only thing advancing the reveal. A latch turns any
        # momentary forward excursion (the reader leads by up to the lookahead) into a
        # freeze lasting until reality catches up, so the pass reveals all at once
        # when its move ends and the jet hangs at the last linking loop meanwhile --
        # then the loop's many short lines fly past and it jumps. Both symptoms, one
        # cause.
        self.t = self._track_nozzle(t_read)
        self._live_t_prev = self.t

    def _reset_reveal(self):
        """Forget what has been drawn: the ratchet marks and the tracker anchor.

        Called at every transition into or out of a live print, on a rewind, and by
        the page's "Clear scaffold". All of it describes ONE print's progress, so none
        of it may survive into another view -- and `_depo_last_n` is the memo that
        skips redundant mesh work, so it has to be invalidated alongside or the next
        reveal pass would conclude it had nothing to do."""
        self._live_seg_hi = 0
        self._live_cell_hi = 0
        self._live_t_prev = self._live_t_wall = self._live_head_xy = None
        self._depo_last_n = -1

    # The live playhead: a complementary filter, not a per-frame search. Wall time
    # supplies the high frequencies (the only thing that animates a long pass, where
    # the file cursor does not move); the projection of the drawn nozzle onto the
    # toolpath supplies the low ones, entering as a CORRECTION of fraction
    # `_LIVE_TRIM_GAIN`, so a wrong match nudges instead of teleporting. Two
    # properties are load-bearing: the projection is taken over the WHOLE file
    # bracket, with no window around the previous playhead, so nothing it computed
    # feeds back into the evidence meant to correct it; and against a STATIONARY
    # nozzle the clock settles `dt/gain` (67 ms at 60 Hz) ahead of the projection
    # rather than running away to the file cursor, so a pause or a mid-print G28
    # cannot reveal unlaid fibre. The lead itself is never MODELLED -- the reader
    # leads by 1.09-3.38 s within one run, by path shape.

    # Bound on how far the file reader may lead the toolhead, in print-time seconds.
    # `file_position` points at the END of the last line Klipper dispatched, so the
    # real lead is its lookahead buffer PLUS one whole commanded move -- on a MEW
    # scaffold, a full pass. The bracket must exceed that while staying FAR below a
    # LAYER time: the gap stops the projection reaching another layer. Swept on
    # `ambiguous_scaffold(layers=8, feed=1800)`, widening reproduces the failure:
    #     3 s   worst error 0.136 s      6 s   0.798 s      12 s   8.207 s
    # A flat 6 s then measured insufficient (virtual MEWron reader lead p99 6.36 s,
    # max 7.05 s) and the floor dragging the playhead up jumped it 82x real time, so
    # it is DERIVED PER FILE (`_derive_live_bracket`). `_LIVE_LEAD_FLOOR` is the old
    # flat value and the derivation may only WIDEN past it
    # (`the_reveal_never_leads_up_to_the_bracket`).
    _LIVE_LEAD_FLOOR = 6.0
    # Klipper keeps dispatching until ~`buffer_time_high` of motion is queued: 1.0 s
    # in current master, 2.0 s in <= v0.12.0. The larger is assumed because assuming
    # the smaller under-sizes the bracket, which is the failure above.
    _LIVE_LOOKAHEAD_S = 2.0
    # Headroom on (buffer + longest move). 1.25 x that puts the preset's bracket at
    # 10.0 s against a measured worst lead of 7.05 s -- 42% clear.
    _LIVE_LEAD_MARGIN = 1.25
    # Ceiling as a fraction of ONE LAYER. The projection must not be able to reach
    # the geometrically identical layer two back, so this has to stay well under 1.
    _LIVE_LEAD_CAP_FRAC = 0.8

    def _derive_live_bracket(self):
        """How far the file reader is allowed to lead the toolhead, for THIS file.

        Two requirements, neither of which a global constant can meet on every file:

          * WIDER than the reader's real lead, or the bracket floor overtakes the
            nozzle and the clamp in `_track_nozzle` drags the reveal ahead of it.
            The lead is the lookahead buffer plus the whole of the last line the
            reader dispatched -- and on a MEW scaffold one line is one full pass, so
            the longest COMMANDED MOVE is the term that matters, not the mean.
          * NARROWER than a layer, so the projection cannot reach the near-identical
            geometry of an earlier layer and resolve to the wrong one.

        Returns seconds. Never smaller than `_LIVE_LEAD_FLOOR`: a file whose layers
        are too short to afford the first requirement keeps the old flat bracket
        rather than getting a worse one, so this can only ever widen.
        """
        floor = float(self._LIVE_LEAD_FLOOR)
        if self.n_pts < 2 or self.total <= 0.0:
            return floor
        dt = np.diff(np.asarray(self.times, float))
        if not dt.size:
            return floor
        # The longest single commanded MOVE, not the longest segment: a turnaround
        # arc is one g-code line per facet but a pass is one line for all of it, and
        # it is the line the reader dispatches whole. `seg_line` survives re-timing
        # (it is reindexed by the resample map), so it is the right key here.
        sl = np.asarray(self.seg_line)
        if sl.size == dt.size and sl.size:
            per_line = np.bincount(sl - int(sl.min()), weights=dt)
            longest_move = float(per_line.max())
        else:
            longest_move = float(dt.max())
        need = self._LIVE_LEAD_MARGIN * (self._LIVE_LOOKAHEAD_S + longest_move)
        cap = self._LIVE_LEAD_CAP_FRAC * self._layer_duration()
        return float(min(max(need, floor), max(cap, floor)))

    def _layer_duration(self):
        """Print-time seconds the file spends at one commanded Z, or the whole path
        when it never changes Z.

        Grouping by COMMANDED Z is what makes this survive the file the old comment
        worried about: with no Z moves there is one group, the answer is the whole
        path, and the cap stops binding -- which is right, because a single-layer
        file has no repeated geometry for the projection to confuse.

        THE STATISTIC IS A DURATION-WEIGHTED MEDIAN, and a plain median is wrong
        here rather than merely worse. The layer change is not a step: the generator
        ramps Z, and re-timing resamples that ramp, so a 6-layer scaffold parses as
        51 Z groups -- 6 real layers of 272-296 s and 45 slivers of under 2 ms.
        Counting groups, the slivers are the majority and the median is 0.000 s,
        which collapses the cap and silently returns the whole feature to the floor
        (measured: `40x20 F400` reported a 6.00 s bracket with the bug in place).
        Weighting by duration asks instead "how long is the layer the head is in
        right now", which is the quantity the bracket has to stay under, and it
        needs no threshold to separate a sliver from a layer."""
        z = np.asarray(self.verts[:, 2], float)
        if z.size < 2:
            return float(self.total)
        zs = np.round(0.5 * (z[:-1] + z[1:]), 6)          # one Z per segment
        dt = np.diff(np.asarray(self.times, float))
        change = np.empty(zs.size, dtype=bool)
        change[0] = True
        change[1:] = zs[1:] != zs[:-1]
        starts = np.nonzero(change)[0]
        if starts.size < 2:                               # one Z: one "layer"
            return float(self.total)
        # reduceat, not a Python loop over the groups: a file that ramps Z
        # continuously (a spiral) has one group per SEGMENT, and this runs at
        # Player construction on every launch, not just machine-live.
        dur = np.add.reduceat(dt, starts)
        dur = dur[dur > 0.0]
        if not dur.size:
            return float(self.total)
        dur.sort()
        cum = np.cumsum(dur)
        k = int(np.searchsorted(cum, 0.5 * cum[-1]))
        return float(dur[min(k, dur.size - 1)])

    # Fraction of the projection's disagreement folded into the clock each frame.
    # This is the filter's crossover, not a threshold: high frequencies come from wall
    # time, low ones from the projection. Its inverse is the settling behaviour --
    # against a stationary nozzle the clock parks `dt/gain` ahead of the projection
    # (67 ms at 60 Hz), which is the bound on how much unprinted fibre a pause can
    # show.
    _LIVE_TRIM_GAIN = 0.25

    # Rate limits on the playhead, as multiples of real time. Forward is generous
    # enough to close a real gap in about a second (3x); backward is deliberately
    # slower (0.5x) because a retreat is a correction to something already drawn, and
    # `_reveal_high_water` is holding the drawing still while it happens.
    #
    # THESE, NOT THE GAIN, ARE THE DAMPING. Swept against the true nozzle on the
    # 8-layer ambiguous scaffold: a 20x gain range (0.05-1.00) moves the worst error
    # only 0.796-0.860 s, while loosening the slew to 20/20 gives 3.834 s -- the
    # undamped free projection -- and tightening to 1.05/0.05 gives 3.394 s, too
    # tight to follow the machine. They sit in a broad flat middle (2.0/0.25 through
    # 6.0/1.0 land within 0.130-0.230 s looped and 0.759-0.996 s ambiguous);
    # changing them needs the sweep re-run.
    _LIVE_SLEW_FWD = 3.0
    _LIVE_SLEW_BACK = 0.5

    def _live_dt(self):
        """Wall seconds since the last cursor update, or None with no previous one.

        Read from `_live.clock` (injectable, so a replay drives it), which is what
        makes the tracker's reach scale with the tick rate rather than encode it."""
        now = self._live.clock()
        prev, self._live_t_wall = self._live_t_wall, now
        if prev is None:
            return None
        return max(0.0, float(now) - float(prev))

    def _live_head_dir(self):
        """Unit XY direction the drawn nozzle is moving in, or None before there are
        two samples / while it is stationary.

        A DIFFERENCE of head positions, so the work offset H cancels: this is already
        in the file's frame and needs no registration."""
        h = np.asarray(self._live_head, float)[:2]
        prev, self._live_head_xy = self._live_head_xy, h.copy()
        if prev is None:
            return None
        v = h - prev
        n = float(np.hypot(v[0], v[1]))
        return None if n < 1e-9 else v / n

    def _project_head(self, lo_t, hi_t):
        """Where on the toolpath the RENDERED nozzle is, searched over the whole
        [lo_t, hi_t] bracket. A path time, or None when there is nothing to match.

        This is a MEASUREMENT of the machine, deliberately independent of the
        playhead: nothing the playhead computed narrows the search, which is what
        makes a slip recoverable. See the block above for what the previous
        playhead-centred window did instead.

        Matched IN XY ONLY, for the same reason `_overlay_delta` applies H in XY
        only: Z is not a shared frame here. The reveal is seated on the MEASURED
        collector surface while the head is a raw machine coordinate, so their Z
        origins differ by the work offset plus whatever the frame chain contributes.
        Including Z would let that mismatch dominate the distance and select a
        segment from the wrong LAYER. In XY the match is well posed anyway: the
        bracket spans a fraction of a layer, so the candidates are one pass and its
        neighbours."""
        lo = max(0, int(np.searchsorted(self.times, lo_t)) - 1)
        hi = min(self.n_pts - 1, int(np.searchsorted(self.times, hi_t)) + 1)
        if hi <= lo:
            return None
        g = np.asarray(self._live_head, float)[:2] - self._live_H[:2]
        a = self.verts[lo:hi, :2]
        ab = self.verts[lo + 1:hi + 1, :2] - a
        if not len(ab):
            return None
        L2 = np.einsum("ij,ij->i", ab, ab)
        safe = np.where(L2 > 1e-12, L2, 1.0)
        u = np.clip(np.einsum("ij,ij->i", g[None, :] - a, ab) / safe, 0.0, 1.0)
        u = np.where(L2 > 1e-12, u, 0.0)
        rel = (a + u[:, None] * ab) - g[None, :]
        d2 = np.einsum("ij,ij->i", rel, rel)
        # Reject candidates the nozzle is not travelling ALONG. On a serpentine
        # scaffold consecutive passes are ANTI-parallel and one `pitch` apart, so at
        # a turnaround the nozzle is as close to the pass it just finished as to the
        # one it is now on and distance cannot choose between them. Direction can.
        # Without this gate the search locks onto the previous pass and mirrors the
        # machine backwards along it -- the jet "stuck midway through layer one".
        # A plain sign test, no tuned threshold. Skipped when EVERY candidate
        # disagrees (inside a turnaround arc, where the heading is rotating) so the
        # match never comes back empty. The gate must be consulted every frame
        # regardless, because `_live_head_dir` advances the state it differences
        # against.
        v = self._live_head_dir()
        if v is not None:
            wrong = ab @ v < 0.0
            if wrong.any() and not wrong.all():
                d2 = np.where(wrong, np.inf, d2)
        k = int(np.argmin(d2))
        seg = lo + k
        span = self.times[seg + 1] - self.times[seg]
        return float(self.times[seg] + float(u[k]) * span)

    def _track_nozzle(self, t_read):
        """The playhead: a clock at 1x wall time, trimmed toward where the rendered
        nozzle actually is, inside the file's bracket [t_read - _live_lead_max,
        t_read]. Returns a time inside that bracket, always -- so this can shave the
        lookahead lead but can never run away past the reader.

        Three terms, and the block above says why each is there:

          * `prev + dt` -- the machine executes the file at 1x by definition, so it
            is the whole answer inside a long pass, where `file_position` does not
            move and nothing else advances the reveal;
          * `+ gain * (measured - clock)` -- the projection enters as a correction,
            never as a position, so a wrong match nudges instead of teleporting;
          * the bracket clamp then the slew clamp, bounding the transient.

        THE ORDER OF THE LAST TWO IS LOAD-BEARING. With the bracket applied last, a
        rising floor moves the playhead by whatever the floor moved, unchecked by a
        rate limit already spent. Clamping first and rate-limiting last means a
        bracket violation is WALKED to at 3x real time, so a turnaround the nozzle
        has not reached can never be drawn. The ceiling is then re-applied, because
        the playhead must never pass the reader; the FLOOR is not, since sitting
        below it is what "trust the nozzle over the reader" means.

        The anchor is dropped (`_live_t_prev = None`) whenever the print starts,
        stops or rewinds, so a mid-print connect or a resume SEEDS from the
        projection rather than crawling to it from a stale clock."""
        lo_t = max(0.0, t_read - self._live_lead_max)
        hi_t = t_read
        prev, dt = self._live_t_prev, self._live_dt()
        t_at = self._project_head(lo_t, hi_t) if self._live.buf else None
        if prev is None or dt is None:            # first frame, or just reseeded
            return min(max(t_read if t_at is None else t_at, lo_t), hi_t)
        t = prev + dt
        if t_at is not None:
            t += self._LIVE_TRIM_GAIN * (t_at - t)
        t = min(max(t, lo_t), hi_t)                       # the bracket FIRST
        t = min(max(t, prev - self._LIVE_SLEW_BACK * dt),
                prev + self._LIVE_SLEW_FWD * dt)          # the rate limit LAST
        return min(t, hi_t)

    def _interp_head(self):
        """The toolhead position to render NOW (None until the first sample)."""
        return self._live.sample()

    def _place_machine_live(self):
        """Place the machine bodies at the current interpolated head (or the origin
        before any sample / if the profile failed to build)."""
        if self.machine is None:
            return
        head = self._interp_head()
        self._live_head = np.zeros(3) if head is None else head
        self._place_machine(self._live_head)

    def _tick_machine_live(self, *_):
        """The machine-live heartbeat (replaces the clock-driven tick): re-place the
        bodies from the interpolated feed and, with a reveal source, advance the
        playhead from the printer's file cursor and run the normal reveal. Runs at the
        16 ms embed tick."""
        if not self._live_src:
            self._place_machine_live()
            self.window.Render()
            return
        head = self._interp_head()
        self._live_head = np.zeros(3) if head is None else head
        self._advance_live_cursor()
        _reveal_trace(self)
        # _apply_time is the LOCKED SIM's reveal, unmodified: gold/travel lines, the
        # active tip, the deposition mesh prefix and the jet, all cut at self.t. Only
        # the head it places the bodies with is swapped for the live one (see there).
        self._apply_time()
        # Re-fit the clipping planes each tick: they are otherwise set once in
        # _frame_camera, so zooming in lets VTK's default range clip the thin overlay
        # lines and micron fibre under parallel projection (the "path fades when I
        # zoom in"). The clock-driven sim re-fits on every orbit; this is the
        # equivalent hook. Cheap bounds math.
        fit_clipping_range(self.renderer)
        self.window.Render()

    def _clear_scaffold(self):
        """Throw the deposited scaffold away: mesh actors removed, mesh discarded,
        jet hidden, reveal marks forgotten.

        The page's "Clear scaffold". Distinct from unticking the checkbox, which only
        HIDES a mesh it keeps built for an instant re-show: this frees it, so the next
        enable pays the sag solve again. That is the point -- it is the way out of a
        view cluttered by a finished (or wrongly seated) print without relaunching
        the viewer."""
        for a in self.depo_actors:
            self.renderer.RemoveActor(a)
        self.depo_actors = []
        self._depo_chunks = []
        self._depo_conn = self._depo_sorted_tt = None
        self._depo_built = False
        self._depo_visible = False
        self._reveal_scaffold = False
        self._jet_visible = False
        if self.jet_actor is not None:
            self.jet_actor.SetVisibility(False)
        self._reset_reveal()

    def _on_reveal_msg(self, msg):
        """Live toggle from the Machine-link page ({"t":"reveal", scaffold?, gcode?,
        clear?}): show/hide the deposited scaffold (+jet) and the commanded path
        without a relaunch, building the mesh on first enable -- or throw the
        scaffold away entirely (`clear`). Ignored with no source, so it can never
        disturb the bodies-only view."""
        if not self._live_src:
            return
        if msg.get("clear"):
            # ahead of the visibility keys below, and consuming `scaffold` on the way
            # out, so the button's single {clear, scaffold: False} message cannot
            # toggle the mesh back on after clearing it
            self._clear_scaffold()
            msg = {k: v for k, v in msg.items() if k != "scaffold"}
        if "scaffold" in msg:
            want = bool(msg.get("scaffold"))
            self._reveal_scaffold = want
            if want != self._depo_visible:
                self._toggle_deposition()      # builds on first enable
            self._jet_visible = want
        if "gcode" in msg:
            want = bool(msg.get("gcode"))
            self._reveal_gcode = want
            self._deposit_on = want
            self._travels_on = want
            self._travel_lines_on = want
            for a in (self.deposit_actor, self.travel_actor, self.active_actor):
                a.SetVisibility(want)
        self._tick_machine_live()

    def _build_buttons_machine_live(self):
        """No sidebar controls for machine-live -- the Machine-link page's form owns
        the state/position readout, so a button rail here would only duplicate it.
        Just set the minimal visibility flags the shared
        placement code reads and hide the degenerate origin toolpath actors. The
        clickable scene-side ViewCube + mouse handle orientation; the panel keeps its
        own fullscreen / close buttons."""
        self.buttons = []
        # Bodies-only keeps every reveal OFF (byte-identical to before). With a reveal
        # source the commanded lines + jet ride their checkbox; the scaffold MESH is
        # built and shown in _run_machine_live, since building it is not free.
        self._travels_on = self._reveal_gcode
        self._travel_lines_on = self._reveal_gcode
        self._machine_on = True
        self._jet_visible = self._reveal_scaffold
        self._deviation_on = False
        self._micro_on = False
        self._depo_visible = False
        self._deposit_on = self._reveal_gcode
        self._head_visible = True
        self._static_revealed = True
        self._click_tag = None
        for a in (self.deposit_actor, self.travel_actor, self.active_actor):
            a.SetVisibility(self._reveal_gcode)
        if self._themed and self._chrome is not None:
            self._chrome.layout()               # a bare rail (wordmark only)

    def _run_machine_live(self):
        """run() for machine-live: seat the bodies, frame the machine, then hand off
        to the embed loop (has_transport=False) with the livepos handler, or a plain
        windowed timer loop. No clock; with a reveal source the scaffold / path / jet
        are laid under the live nozzle, driven by the printer's file cursor."""
        if self._reveal_scaffold:
            # one-time lazy build of the sagged mesh (as run() does for the locked
            # sim); the file-cursor tick then reveals a growing prefix of it
            self._toggle_deposition()
        self._place_machine_live()             # seat the bodies before framing
        if self._live_src:
            self._advance_live_cursor()        # seat the reveal (static: whole path)
            self._apply_time()
        cam = self.renderer.GetActiveCamera()
        cam.ParallelProjectionOn()
        cam.SetFocalPoint(0.0, 0.0, 0.0)
        cam.SetPosition(1.0, -1.0, 0.8)        # direction only; distance set below
        cam.SetViewUp(0.0, 0.0, 1.0)           # Z up, like CAD
        self._frame_camera()                   # machine-live branch frames the bodies
        apply_preferred_projection(self.renderer)   # the Settings switch
        self.window.Render()
        if self.embed is not None:
            self.embed.tick_hook = self._on_tick        # -> _tick_machine_live
            self.embed.on("livepos", self._on_livepos)  # the GUI's position stream
            self.embed.on("origin", self._on_origin_msg)  # Settings switch
            self.embed.on("filepos", self._on_filepos)  # the printer's file cursor
            self.embed.on("reveal", self._on_reveal_msg)  # the page's checkboxes
            # expose the rendered head so the GUI/tests can observe the feed is live
            self.embed.state_provider = lambda: {
                # a reveal timeline only when there IS one: the bodies-only view's
                # path is a degenerate 2-point stub, whose `total` means nothing
                "time": round(float(self.t), 3) if self._live_src else 0.0,
                "total": round(float(self.total), 3) if self._live_src else 0.0,
                "playing": False, "speed": 1.0,
                "head": [round(float(v), 3) for v in self._live_head],
                # reveal diagnostics, so a rig session reads NUMBERS rather than
                # guessing: the frame shift being applied, the file cursor consumed,
                # and the resulting playhead. The regression test asserts on these.
                "H": [round(float(v), 3) for v in self._live_H],
                "filepos": self._live_filepos,
                "reveal": bool(self._live_src),
                # Seating diagnostics. Whether the overlay sits ON the collector or
                # sunk into it is a question about three world-Z numbers, and after
                # several wrong guesses from the outside the only reliable way to
                # answer it is to have the CHILD report them from the real scene.
                # base = the first deposited layer as drawn; surf = the print surface
                # it was seated on; bedtop = the highest bed geometry anywhere.
                "base": (None if not self._live_src else
                         round(float(self._reveal_base_z
                                     + self.deposit_actor.GetPosition()[2]), 3)),
                "surf": (None if not self._live_src or self._reveal_surface_z() is None
                         else round(float(self._reveal_surface_z()), 3)),
                "bedtop": (None if self.bed_actor is None else
                           round(float(self.bed_actor.GetBounds()[5]), 3)),
                # accuracy audit of _LIVE_RENDER_DELAY: frames drawn, and how many of
                # them were EXTRAPOLATED past the newest real sample (should stay 0 --
                # see _LiveFeed.n_extrap)
                "drawn": self._live.n_drawn,
                "extrap": self._live.n_extrap,
                "focal": [round(v, 4)
                          for v in self.renderer.GetActiveCamera().GetFocalPoint()]}
            self._last_wall = time.perf_counter()
            self.embed.loop(tick_ms=16, has_transport=False, total=0.0)
            return
        self.interactor.Initialize()
        self._last_wall = time.perf_counter()
        self.interactor.CreateRepeatingTimer(16)
        self.window.Render()
        self.interactor.Start()

    # -- the animation tick ------------------------------------------------- #
    def _on_tick(self, *_):
        if self.machine_live:
            return self._tick_machine_live()
        if self.playing:
            now = time.perf_counter()
            if self._last_wall is None:
                self._last_wall = now
            # Advance the print-time clock by the wall delta times BOTH the viewing
            # multiplier (self.speed, the - / + keys) and the live print-speed
            # what-if (print_speed_scale, the [ / ] keys). The latter retimes the
            # nozzle: at scale 0.5 the head crawls (a half-speed print), at 2.0 it
            # races -- so the head visibly tracks the knob, matching the thinner/
            # thicker fibre and the lag the same scale already drives. Only the RATE
            # changes; self.t stays on the g-code's original timeline, so the head
            # lookup, reveal, jet and mesh (all keyed off self.t) stay in lockstep
            # and a mid-playback knob change takes effect on the very next tick.
            # A no-op outside real-time mode, where the scale is pinned at 1.0.
            self.t += ((now - self._last_wall) * self.speed
                       * self.print_speed_scale)
            self._last_wall = now
            if self.t >= self.total:
                self.t = self.total
                self.playing = False
                self._refresh_buttons()
        # March the incremental deposition up to the (now-current) playhead
        # and refresh the jet/mesh if the laid geometry changed. Runs whether playing
        # (lay new layers as the clock advances) or paused (a debounced edit refreshes
        # the in-progress layer). Cheap when nothing crossed a layer boundary.
        rebuilt = self._advance_deposition() if self.live_adjust else False
        if self.playing or rebuilt:
            self._apply_time()
            self.window.Render()

    def _nozzle_floats(self):
        """True when the nozzle is drawn floating one standoff above the deposition
        plane with a leaning jet bridging the gap to the trailing contact -- a loaded
        machine model (`self.machine`, which is reset to None in _build_machine on any
        fallback, so it is non-None only when the multi-body model was actually built),
        the real standalone printhead (`_has_printhead`), OR the no-tool standalone view
        (no printhead asset -- head_actor is None -- but the bare jet still leans from
        the floating tip). All three run the jet from the lifted tip down to the
        touchdown (see _place_head_and_jet / _place_machine); ONLY the legacy
        path-riding frustum marker (head_actor set but not `_has_printhead`) has no jet.
        This must MATCH _place_head_and_jet's jet-drawing condition, because it is what
        makes the reveal trail the nozzle by the lag so the gold lines end at the jet's
        touchdown rather than running ahead to the nozzle centre."""
        if self.machine is not None:
            return True
        # standalone: the jet floats unless the legacy frustum marker rides the path
        return self._has_printhead or self.head_actor is None

    def _reveal_lag(self):
        """STATIC reveal lag, used only as the fallback in _reveal_time (physics off
        or a hand-launched run with no contact curve); the physics jet uses the
        dynamical reveal curve instead. How far (in print-time) the DEPOSITED reveal
        (gold lines + deposition mesh) trails the nozzle so its leading edge meets
        the jet's contact point: the contact's geometric time-lag lag_distance /
        v_anim, which is self.lag * print_speed_scale -- self.lag is the steady lag
        TIME at the (scaled) what-if speed, and the scale converts it back to the
        actual animation timeline, so the reveal keeps pace with the jet as the
        print speed is scaled with [ / ] (without it, scaling sped the jet's lean
        but not the reveal, leaving the reveal running ahead). 0 with the fallback
        frustum: no jet, so the reveal runs right up to the nozzle, as before.

        Gated on _nozzle_floats (NOT _has_printhead alone): a loaded machine model
        draws the same leaning jet but leaves _has_printhead False, so gating on the
        printhead flag let the gold lines run to the nozzle while the deposited mesh
        trailed -- the "lines reveal under the syringe" divergence."""
        return self.lag * self.print_speed_scale if self._nozzle_floats() else 0.0

    def _cursor_line(self, seg):
        """(1-based source g-code line, its command text) for segment `seg`. The
        command is stripped of any comment. (0, "") when there is no line map."""
        sl = getattr(self, "seg_line", None)
        if sl is None or sl.size == 0:
            return 0, ""
        line = int(sl[min(max(int(seg), 0), sl.size - 1)])
        cmd = ""
        if 0 < line <= len(self.gcode_lines):
            cmd = self.gcode_lines[line - 1].split(";", 1)[0].strip()
        return line, cmd

    def _cursor(self, t=None):
        """(segment index, fraction along it) for time `t` (default self.t)."""
        if t is None:
            t = self.t
        if self.n_pts < 2:
            return 0, 0.0
        seg = int(np.searchsorted(self.times, t, side="right")) - 1
        seg = min(max(seg, 0), self.n_pts - 2)
        span = self.times[seg + 1] - self.times[seg]
        frac = 0.0 if span <= 0 else (t - self.times[seg]) / span
        return seg, min(max(frac, 0.0), 1.0)

    def _apply_time(self):
        if self.machine_live and not self._live_src:
            # no clock and no toolpath -- place the bodies from the live feed
            # (callers Render)
            self._place_machine_live()
            return
        if self.n_pts < 2:
            return
        # the head/printhead follow the nozzle at the current time
        seg, frac = self._cursor()
        head = self.verts[seg] + frac * (self.verts[seg + 1] - self.verts[seg])
        # On a mandrel `self.verts` are in the COLLECTOR's frame -- they are the
        # drawn fibre, and they spin. The printer's own bodies move in MACHINE
        # coordinates, which is a different point entirely: the nozzle sits still
        # in Y and Z while the collector turns beneath it.
        mhead, self._rot_deg = self._rotary_at(seg, frac)
        if mhead is not None:
            head = mhead
        if self.machine_live:
            # ... except in machine-live, where the bodies belong to the REAL machine.
            # The playhead still drives every reveal below; only the thing that says
            # where the printer physically is changes hands. Keeping these two
            # independent is what stops a reveal bug from ever moving the nozzle
            # again (the failure mode of the first overlay attempt, which fed the
            # g-code frame to the bodies and made G92 teleport them to the corner).
            head = self._live_head

        # drive the jet physics from the SPEED AT THE PLAYHEAD: if this segment is
        # printed at a different feed (a second scaffold, or a loop with its own
        # speed), re-solve so the lag/deflection and arc update before the reveal
        # and jet are placed below (which both consume the fresh self.lag).
        self._update_deposit_speed(seg)

        # the commanded toolpath lines (gold deposit + grey travel + active tip) end
        # at the jet's bottom -- a lag-time behind the nozzle (see _lines_reveal_time)
        # -- so the travel and gold lines meet the jet instead of running ahead to the
        # nozzle centre. (The deposition MESH uses _reveal_time: the reshaped fibre
        # carries the lag in its geometry, so it cuts at self.t and still ends at the
        # same jet point.) At the end the in-flight tail has landed, reveal to nozzle.
        reveal_t = self._lines_reveal_time()
        dseg, dfrac = self._cursor(reveal_t)
        dhead = self.verts[dseg] + dfrac * (self.verts[dseg + 1]
                                            - self.verts[dseg])

        # completed segments are those strictly before the (trailing) active one --
        # or, in machine-live, before the furthest one ever reached, so a printed pass
        # can never un-draw (see _reveal_high_water)
        done = self._reveal_high_water(dseg)
        _set_segments(self.deposit_poly, self.dep_idx[self.dep_idx < done])
        if self._travel_lines_on:
            _set_segments(self.travel_poly, self.trv_idx[self.trv_idx < done])
        else:
            _set_segments(self.travel_poly, np.empty(0, dtype=np.int64))

        # the active segment, drawn partially up to the contact point, by kind. Once
        # the high-water mark has moved past it the whole segment is already drawn,
        # so the partial stub would only double it.
        self.active_pts.SetPoint(0, *self.verts[dseg])
        self.active_pts.SetPoint(1, *dhead)
        self.active_pts.Modified()
        is_dep_seg = bool(self.is_dep[dseg])
        show_active = ((is_dep_seg and self._deposit_on)
                       or (not is_dep_seg and self._travel_lines_on)) and dseg >= done
        self.active_actor.SetVisibility(show_active)
        self.active_actor.GetProperty().SetColor(
            *(DEPOSIT_COLOR if is_dep_seg else TRAVEL_COLOR))

        self._place_head_and_jet(head)

        if self.slider is not None and not self._sync_slider:
            self._sync_slider = True
            self.slider.GetRepresentation().SetValue(self.t)
            self._sync_slider = False

        if self._depo_visible:
            self._reveal_deposition()

        self._refresh_hud()

    def _reveal_high_water(self, dseg):
        """The commanded segment index the reveal is complete up to.

        Normally just `dseg` -- the clock-driven sim scrubs both ways, so what is
        drawn must follow the playhead exactly. In machine-live it is the FURTHEST
        segment ever reached, because a printed pass is a fact: the machine cannot
        un-print it, and the reveal should not un-draw it.

        That is where monotonicity belongs. Enforcing it on the playhead instead
        (a clock latch) freezes the position within the current pass too, which
        stalls the jet and makes each pass appear only once its move has finished --
        the whole point is that the reveal EDGE ratchets while the position inside
        the active pass stays free to track the nozzle.

        Gated on `_live_tracking`, not on `_live_src`: with no print running the view
        places the WHOLE path at t = total, and a mark taken from that pass would pin
        the reveal at the end of the file for the rest of the session."""
        if not self._live_tracking:
            return dseg
        self._live_seg_hi = max(self._live_seg_hi, int(dseg))
        return self._live_seg_hi

    def _goto_frame(self, line_no):
        """Freeze the playhead at the frame that executes 1-based g-code line
        `line_no` -- the head/jet/deposition revealed up to it (a mid-print freeze
        for "find in simulation"). No-op without a line map. Reveals the sim tools
        so a --static launch shows the head at the line instead of the bare finished
        scaffold; a plain launch already has them visible."""
        if self.n_pts < 2:
            return
        self.t = line_to_time(self.seg_line, self.times, int(line_no))
        self.playing = False
        self._reveal_sim_tool()
        self._apply_time()

    #: The opening camera direction on a flat collector: 60 degrees above the
    #: plate. A MEW scaffold is a lattice whose point is its pores, and from the
    #: CAD isometric (30 degrees) a stack of layers hides the pore behind it
    #: once the stack is taller than ~0.6 of the pore: a 20-layer 22 um stack
    #: (0.28 mm) over 0.30 mm pores drew as one solid blue sheet, at every
    #: display quality. At 60 degrees the same stack covers 0.16 mm of the
    #: 0.30 mm pore, and the scaffold still reads as 3-D. A mandrel keeps the
    #: isometric: its fibres wrap a tube, and looking down hides the tube.
    OPENING_DIR_FLAT = (1.0, -1.0, 2.45)
    OPENING_DIR_ISO = (1.0, -1.0, 0.8)

    def _opening_view_dir(self):
        """The direction the camera opens along (distance is set by framing)."""
        if self._frame is None and self._five is None:
            return self.OPENING_DIR_FLAT
        return self.OPENING_DIR_ISO

    # -- run ---------------------------------------------------------------- #
    def run(self):
        if self.machine_live:
            return self._run_machine_live()
        if self._static:
            # Unified-viewer default: open on the FINISHED scaffold, paused, with
            # the machine / printhead / jet / travels hidden -- visually the plain
            # STL view. _build_buttons already set the sim-tool flags OFF (so the
            # labels read "Show X"); here we open at the end and apply the actor
            # visibility the reveal passes don't (the head). The first
            # play/scrub reveals them (_reveal_sim_tool). total is known from setup.
            self.t = self.total
            self.playing = False
            if self.head_actor is not None:
                self.head_actor.SetVisibility(False)
            self._apply_machine_visibility()   # honours _machine_on == False
        self._apply_time()                     # place the head before framing
        # CAD-style camera: parallel (orthographic) projection so parallel
        # fibers stay parallel instead of converging to a vanishing point, with
        # Z up and an isometric default direction -- the view Fusion / ncviewer
        # users expect. Set the orientation BEFORE ResetCamera, which keeps the
        # view direction and view-up and only slides the camera to fit.
        cam = self.renderer.GetActiveCamera()
        cam.ParallelProjectionOn()
        cam.SetFocalPoint(0.0, 0.0, 0.0)
        cam.SetPosition(*self._opening_view_dir())  # direction only
        cam.SetViewUp(0.0, 0.0, 1.0)           # Z up, like CAD
        # frame the scaffold explicitly (not ResetCamera() with no args, which
        # would include the origin-anchored markers and push the part off-centre).
        # With a machine model loaded, frame the WHOLE machine too -- the point of
        # that view is to watch the printer -- but never let hidden parts collapse
        # the camera onto the origin frustum (see _frame_camera).
        self._frame_camera()                   # frames + slides the camera clear of the
                                               # near plane (fit_clipping_range): no clip
        apply_preferred_projection(self.renderer)   # the Settings switch
        self.window.Render()
        _perf("first_render")
        # GUARDED, because the ARGUMENT is what costs. `_perf` is a no-op with
        # MEWPERF unset, but the f-string is built first, so `ReportCapabilities`
        # -- a live query of the GL context -- ran on every launch and the result
        # was thrown away. That is charged to the "Starting the viewer…" window
        # this instrumentation exists to shorten.
        if _PERF_PATH:
            _perf(f"gl {_gl_renderer(self.window)}")
        # Default view: show the deposited (sagged) scaffold mesh, with the raw
        # gold g-code lines hidden (see _build_buttons).
        if self.live_adjust:
            # real-time mode: the deposition is time-marched from the launch
            # parameters as the clock advances -- NO whole-scaffold precompute up
            # front (the launch freeze the old re-lay paid is gone). The march owns
            # the mesh + jet anchor from here, not the cache; the initial build seeds
            # the in-progress tip so the jet is anchored before playback.
            self._depo_visible = True
            self._depo_built = True
            self._advance_deposition(force=True)
        elif not self._depo_visible:
            # the same lazy cache build the toggle triggers; run it once at startup
            self._toggle_deposition()
            self._refresh_buttons()
            self._apply_time()
            _perf("depo_reveal")
            self.window.Render()
            _perf("depo_upload")
        # The deposition mesh was added AFTER the first _frame_camera above, so the
        # near/far clip planes were fitted WITHOUT it -- the scaffold clips at load
        # until the first orbit re-fits (fit_clipping_range). Re-frame now that the
        # full mesh exists so the very first frame is clean. This matters most in
        # --static, where the whole scaffold is revealed immediately (a plain sim
        # launch reveals nothing at t=0, so there was nothing to clip before).
        self._frame_camera()
        _perf("depo_frame_cam")
        self.window.Render()
        _perf("depo_render")
        if os.environ.get("MEW_DEBUG_CAMERA"):
            w2i = vtk.vtkWindowToImageFilter()
            w2i.SetInput(self.window)
            w2i.Update()
            writer = vtk.vtkPNGWriter()
            writer.SetFileName(os.environ["MEW_DEBUG_CAMERA"])
            writer.SetInputConnection(w2i.GetOutputPort())
            writer.Write()
            sys.stderr.write("DEBUG_CAMERA wrote %s\n"
                             % os.environ["MEW_DEBUG_CAMERA"])
        # "Find in simulation": jump the playhead to the requested g-code line and
        # freeze there, once the scene + deposition mesh exist so the reveal is
        # correct. Done after framing so the camera still fits the whole scaffold.
        if self.goto_line is not None:
            self._goto_frame(self.goto_line)
            self.window.Render()
        if self.embed is not None:
            # embedded: the host loop replaces Start() -- it drives the 16 ms
            # clock tick, replays the GUI's input, and streams frames/HUD/labels/
            # state back to the embedded page. The clock is driven THROUGH
            # tick_hook (a direct _on_tick call), NOT the interactor's TimerEvent:
            # invoking TimerEvent also runs vtkInteractorStyle::OnTimer, which
            # re-applies an in-progress middle-button PAN off the last event
            # positions -- the embedded viewer's camera teleport on the first
            # pan and its drift while held (the windowed path uses a real repeating
            # timer, where fresh OS mouse positions make that re-pan a no-op).
            self.embed.tick_hook = self._on_tick
            self.embed.on("scrub", self._embed_scrub)
            self.embed.on("origin", self._on_origin_msg)  # Settings switch
            # apply-in-place: the GUI pushes a computed deviation sidecar down
            self.embed.on("deviation", self._on_deviation_msg)
            # apply-in-place: the GUI pushes a micrograph image path down
            self.embed.on("micrograph", self._on_micrograph_msg)
            self.embed.state_provider = lambda: {
                "time": self.t, "total": self.total,
                "playing": self.playing, "speed": self.speed,
                # deviation view state -- ignored by the GUI transport, read by
                # test_viewer_embed's reveal guard: `dev_on` is the deviation colour
                # scheme, `dev_cells` the count of deposition cells the clock has
                # revealed (it must SHRINK when scrubbing back, proving the deviation
                # view rides the same progressive reveal as the plain mesh)
                "dev_on": bool(self._deviation_on),
                "dev_cells": int(self._depo_last_n),
                # camera focal point -- ignored by the GUI transport, read by
                # test_viewer_embed's pan-teleport guard (a held pan must not
                # drift on idle clock ticks)
                "focal": [round(v, 4)
                          for v in self.renderer.GetActiveCamera().GetFocalPoint()]}
            self.embed.fs_enter_hook = self._mount_fs_transport
            self._last_wall = time.perf_counter()
            _perf("embed_loop")     # the GUI's "Starting the viewer…" ends here
            self.embed.loop(tick_ms=16, has_transport=True, total=self.total)
            return
        self.interactor.Initialize()
        self._last_wall = time.perf_counter()
        self.interactor.CreateRepeatingTimer(16)   # ~60 fps
        self.window.Render()
        self.interactor.Start()


def _corner_chord_arg(args):
    """`--auto-corner-grid` / `--curve-quality` -> the `(tol_mm, radius_mm,
    steps_per_tau)` triple `laid_fibers` takes, or None when the flag is off.

    `mew_deposition.auto_corner_grid` is THE definition of a tier, so the player,
    the preview and `mew_export` cannot disagree about what "standard" means -- the
    reason this resolves a NAME here rather than accepting three numbers. Imported
    locally because `mew_deposition` is a physics import the player otherwise pulls
    in lazily, and a bad tier degrades to the fixed grid rather than refusing to
    open a viewer."""
    if not getattr(args, "auto_corner_grid", 0.0) > 0.0:
        return None
    try:
        import mew_deposition
        return mew_deposition.auto_corner_grid(args.auto_corner_grid,
                                               args.curve_quality)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"gcode_player: ignoring --auto-corner-grid ({exc})\n")
        return None


def build_parser():
    """The CLI, as a parser, so callers can be tested against the REAL flag set.

    A caller can drive the argv the unified Viewer's live-edit button relaunches
    with and ask whether it reaches the march with a rotary frame; built by
    keyword instead, that check would pass whether or not the GUI's `--mandrel`
    ever arrived. `main` parses through this, so there is one flag set and not
    two."""
    import argparse
    ap = argparse.ArgumentParser(
        description="Animate a printhead following a G-code toolpath.")
    ap.add_argument("gcode", nargs="?", default=None,
                    help="input .gcode file (omit only with --machine-live)")
    ap.add_argument("stl", nargs="?", default=None,
                    help="optional scaffold-fiber STL overlay")
    # fiber params for the deposition view -- pass the SAME values the converter
    # used so the deposited mesh matches the written STL. Omitted -> the
    # gcode_to_fibers defaults (fine for a hand-launched player).
    ap.add_argument("--diameter", type=float, default=0.02)
    ap.add_argument("--faces", type=int, default=12)
    # THE COLLECTOR. A mandrel print's rotary word is in the file; the rod it
    # turns is not -- and read without this the file animates as a flat zigzag
    # with no error, because `A` is not in the axis whitelist (see `parse_path`).
    ap.add_argument("--mandrel", type=float, default=None, metavar="MM",
                    help="the g-code drives a ROTATING MANDREL of this diameter: "
                         "read the 4th axis, draw the collector, and spin it on "
                         "the toolpath's own clock")
    ap.add_argument("--mandrel-length", type=float, default=None, metavar="MM",
                    help="[--mandrel] length of the drawn collector -- the "
                         "design's PRINTED SPAN, not its tiling length (a wavy "
                         "family runs an amplitude past each end); omitted, it "
                         "spans the print with a margin either side")
    ap.add_argument("--mandrel-start", type=float, default=None, metavar="MM",
                    help="[--mandrel] where the rod's NEAR END sits along the "
                         "axis, in g-code X. The designer's collector interval, "
                         "so the bare rod either side of the print shows; omitted, it is "
                         "centred on the printed extent (the historical rule)")
    ap.add_argument("--mandrel-surface-z", type=float, default=0.0, metavar="MM",
                    help="[--mandrel] the g-code Z that means the BARE MANDREL "
                         "SURFACE -- what the machine was zeroed on radially. 0 "
                         "(the default) is a Z word that is wall height; a "
                         "program zeroed on the rod's AXIS carries the rod "
                         "radius here")
    ap.add_argument("--mode", choices=["auto", "e", "g1"], default="auto")
    ap.add_argument("--no-sag", action="store_true")
    # "Sag solver: approximate" -- a .depo CACHE KEY here, like --render-travel,
    # not a solver this player runs. The two solvers take the same diameter /
    # multiplier / reset period, so nothing else in the cache meta distinguishes
    # their output: without this the player would silently reject the very cache
    # the run just wrote and re-derive the scaffold with the OTHER solver.
    ap.add_argument("--sag-approx", action="store_true")
    ap.add_argument("--sag-multiplier", type=float, default=1.0)
    # Periodic sag reset, forwarded so the sim resets on the same layers the STL
    # does. MUST default to the converter's default, not 0: the value is part of the
    # .depo cache key, so a hand-launched player that defaulted to "never" would
    # reject a default-built cache and silently fall back to re-deriving the march
    # via mew_live -- the live-vs-locked drift the locked seed exists to prevent.
    ap.add_argument("--sag-reset-layers", type=int,
                    default=_DEFAULT_SAG_RESET_LAYERS)
    # The sag output-sampling resolution, forwarded for the same reason as the
    # reset period: it is part of the .depo cache key (gcode_to_fibers
    # `set_sag_resolution`), so a player left at 1 would reject a finer cache and
    # re-derive the march at the coarse density, and the live march must sample
    # at the export's density to stay bit-identical to it.
    ap.add_argument("--sag-resolution", type=float, default=1.0)
    # The Export panel's "Mesh detail", forwarded so the FULL-MESH view is
    # sampled exactly like the STL that run wrote. NOT a .depo cache key: the
    # cache always stores the full sampling and the thinning happens on the way
    # into the tube, so changing this cannot make the cache disagree with the
    # geometry on disk. 0 = no thinning (the "Full (largest file)" choice).
    ap.add_argument("--mesh-tolerance", type=float, default=0.0, metavar="MULT")
    # Travel (G0) deposition -- a .depo CACHE KEY here, not a mode this player
    # implements. The export can render travels into the cached fibres; because
    # `merge_travels` preserves the fibre COUNT, such a cache is indistinguishable
    # from a plain one by any count guard, so without this key the locked sim would
    # happily ride a cache whose travel mode is not the one the user asked for.
    ap.add_argument("--travel-gcode-line", action="store_true",
                    help="draw the commanded travel (G0) moves as g-code "
                         "lines. Off by default: the travel FIBRE is the "
                         "default rendering and the sidebar's Show/Hide "
                         "travel fibre drives it; this is the overlay for "
                         "seeing where the travels are.")
    ap.add_argument("--render-travel", action="store_true",
                    help=argparse.SUPPRESS)
    # STL arc-approximation fidelity, forwarded so the (lazily built) deposition
    # overlay matches the STL when no sidecar cache exists. The drawn path itself
    # is always smooth (see _SMOOTH_ARC_STEP_DEG), independent of this.
    ap.add_argument("--arc-segments", type=int, default=None)
    # Deposition render quality for the LOCKED sim (matches the STL viewer):
    # "exact" tubes every sagged point; "tube:N" grows the centrelines into
    # N-sided tubes with vtkTubeFilter (fast); "wireframe" draws them as lines.
    # The live-adjust real-time march keeps its own incremental tubes regardless.
    ap.add_argument("--render", default="exact",
                    help="locked-sim deposition view: exact | tube:N | wireframe")
    # Explicit .depo sidecar path (the GUI's last_depo). Without it the cache is
    # only searched next to the STL / the g-code -- which both miss for a g-code
    # IMPORT at a preview quality (no STL; cache next to -o in the session folder,
    # g-code elsewhere), silently degrading the locked sim to the commanded
    # (square) path. Omitted -> the old search, byte-identical.
    ap.add_argument("--depo", default=None,
                    help="deposition sidecar (.depo.npz) the export wrote; "
                         "checked before the STL-/g-code-adjacent search")
    # Explicit build-plate sibling path (the GUI's last_plate). Like --depo, the
    # implicit <stem>_plate.stl search misses for a g-code import at a preview
    # quality, so the plate the converter wrote in the session folder never showed.
    ap.add_argument("--plate-stl", default=None,
                    help="build-plate sibling (<stem>_plate.stl) the converter "
                         "wrote; checked before the STL-/g-code-adjacent search")
    # per-scaffold parts (name/layers/fiber_diameter) for a multi-scaffold run,
    # so the deposition view tubes each scaffold at its own diameter; same JSON
    # gcode_to_fibers takes. Omitted -> every fiber at --diameter, as before.
    ap.add_argument("--scaffold-groups", default=None)
    # ONE colour for every deposited fibre, '#rrggbb'. The per-scaffold
    # `--scaffold-groups` colours above win wherever they apply; this is the
    # fallback underneath them, which is what a single-part run has. A flat
    # plate has always coloured its fibres through the groups list (one group
    # per scaffold, each carrying the designer's "Viewer colour"), but a
    # mandrel or a curved collector is ONE part with no group list at all, so
    # its fibres fell back to the solid SCAFFOLD_COLOR and the designer's
    # colour stopped at the preview. Malformed or omitted -> SCAFFOLD_COLOR,
    # exactly as before.
    ap.add_argument("--diameter-multiplier", type=float, default=1.0,
                    metavar="X",
                    help="visualization only: scale every drawn fiber's "
                         "diameter by X (default 1); the tubes are built with "
                         "gcode_to_fibers.TUBE_RADIUS_MULT")
    ap.add_argument("--fiber-color", default=None,
                    help="colour for deposited fibres, '#rrggbb'; "
                         "--scaffold-groups colours override it per scaffold")
    # real-printhead + jet controls (see Player.__init__)
    ap.add_argument("--standoff", type=float, default=4.5,
                    help="needle height above the commanded layer, mm")
    ap.add_argument("--lag", type=float, default=0.3,
                    help="jet flight time, s -- fixed deflection used only with "
                         "--no-physics")
    # physics jet model (mew_physics): the four MEW knobs. Speed is read from the
    # g-code deposition feed. On by default; --no-physics restores the fixed jet.
    ap.add_argument("--pressure", type=float, default=2.0e4,
                    help="melt pressure, Pa (physics jet); ~3 psi default")
    ap.add_argument("--voltage", type=float, default=4.0,
                    help="applied voltage, kV (physics jet)")
    ap.add_argument("--temperature", type=float, default=90.0,
                    help="melt temperature, degC (physics jet)")
    ap.add_argument("--calibration", default=None,
                    help="path to a rig calibration profile JSON (mew_calibration; "
                         "the Material / Nozzle / Calibration anchors + hooks). "
                         "Omitted -> the factory defaults, byte-identical.")
    ap.add_argument("--no-physics", action="store_true",
                    help="disable the physics jet; use fixed --lag and --diameter")
    ap.add_argument("--live-adjust", action="store_true",
                    help="enable real-time parameter editing: the v/f/h/[ ] keys "
                         "edit voltage/pressure/temperature/print-speed live AND "
                         "the deposited fibre re-lays (diameter + whip) to match, "
                         "forward-accumulating; 0 resets. Off -> the locked sim "
                         "(those keys do nothing). Needs the physics jet.")
    ap.add_argument("--coil", action="store_true",
                    help="enable the below-CTS sewing-machine coiling model in the "
                         "real-time re-lay (matches mew_export --coil); off -> a "
                         "below-CTS fibre is left as its commanded path. No effect "
                         "in the locked sim, which rides the exported .depo cache.")
    ap.add_argument("--coil-wave-amp", type=float, default=0.0,
                    help="below-CTS coiling-WAVE indicator amplitude, mm (a simple "
                         "fixed lateral sine marking CTS was reached, matching "
                         "mew_export --coil-wave-amp); forwarded to the real-time "
                         "re-lay. <= 0 -> off. The locked sim rides the .depo cache.")
    ap.add_argument("--coil-wave-len", type=float, default=0.3,
                    help="below-CTS coiling-wave indicator wavelength, mm "
                         "(only meaningful with --coil-wave-amp > 0)")
    ap.add_argument("--no-cone", action="store_true",
                    help="do NOT draw the molten Taylor cone at the nozzle; render the "
                         "legacy constant-radius thin jet at the fibre diameter instead. "
                         "Jet-render only -- never affects the deposited mesh or export.")
    ap.add_argument("--auto-corner-grid", type=float, default=0.0, metavar="MM",
                    help="the adaptive corner sampler for the LIVE march and the "
                         "mesh rebuild, sized on the deposited fibre diameter "
                         "given here (mew_deposition.auto_corner_grid). Same flag "
                         "and same meaning as mew_export's, so the live sim draws "
                         "corners at the tier the exported STL uses. 0 (default) "
                         "is the fixed --fill-per-tau grid. On a flat print, pass "
                         "it only when the .depo was exported with it too: the "
                         "march re-lays layers beside the cached ones.")
    ap.add_argument("--curve-quality", default=None, metavar="TIER",
                    help="Curve-quality tier for --auto-corner-grid (draft / "
                         "standard / fine); mew_deposition.CURVE_QUALITY is the "
                         "one definition, so the player, the preview and the "
                         "export cannot disagree about what a tier means.")
    ap.add_argument("--fill-per-tau", type=float, default=6.0,
                    help="refine grid for the LIVE re-lay: deposited samples per "
                         "relaxation time inside a corner/start transient "
                         "(default: 6). Must match the value mew_export baked the "
                         ".depo with, or a live edit re-lays on a different grid "
                         "than the locked geometry it started from. The GUI passes "
                         "one value to both.")
    ap.add_argument("--physics-deposit", action="store_true",
                    help="the .depo cache to load was written by the physics export "
                         "(mew_export, the 'Physics-deposited scaffold' toggle); used "
                         "to reject a cache built in the OTHER mode (physics toggled "
                         "since). Off -> expect a plain commanded-path cache.")
    ap.add_argument("--physical-sag", action="store_true",
                    help="the scaffold was built with the grounded physical sag law "
                         "(mew_export --physical-sag): accept that .depo cache (reject "
                         "a geometric one) and re-solve the live re-lay with the same "
                         "law. Off -> the geometric droop.")
    ap.add_argument("--physical-sag-viscous", action="store_true",
                    help="DORMANT future-work (not exposed in the GUI): sub-option of "
                         "--physical-sag that uses the VISCOUS-CREEP sag law (temperature "
                         "predicted via eta(T)) for the live re-lay, matching mew_export "
                         "--physical-sag-viscous. Default off (reduced-order law).")
    ap.add_argument("--physical-fusion", action="store_true",
                    help="the scaffold was built with junction fusion "
                         "(mew_export --physical-fusion): accept that .depo cache "
                         "(reject an unfused one) and sink crossovers in the live "
                         "re-lay with the same law. Off -> the full-diameter stack.")
    ap.add_argument("--dynamic-diameter", action="store_true",
                    help="in the live-adjust re-lay, recompute each fibre's diameter "
                         "from the physics (mass conservation; the live pressure/"
                         "temperature knobs change it). Matches mew_export's "
                         "--dynamic-diameter; the GUI sets it when the diameter is "
                         "physics-driven. Off -> the live fibre keeps the commanded "
                         "diameter (a manual override shows the typed value).")
    ap.add_argument("--head-stl", default=None,
                    help="printhead STL (default: the bundled MEWron head)")
    ap.add_argument("--head-scale", type=float, default=1.0,
                    help="uniform scale for the printhead STL")
    ap.add_argument("--hide-printhead", action="store_true",
                    help="draw no printhead, only the jet and the fibre, "
                         "in the non-machine-model view (the GUI sets it when the "
                         "printer model is off or the printhead is ticked Hidden)")
    ap.add_argument("--origin", default=None,
                    help="g-code origin re-base offset 'dx,dy,dz' (undone in the "
                         "machine view so the scene is origin-invariant)")
    ap.add_argument("--machine", default=None,
                    help="machine-model profile JSON (printhead/gantry/bed STLs); "
                         "omitted -> the single printhead + jet view")
    # machine motion limits for the accel-limited re-timing (mew_kinematics). Both
    # are printer properties (Klipper printer.cfg), defaulting to verified Voron 0.2
    # stock values; the GUI's Machine-and-Collector fields override them.
    ap.add_argument("--max-accel", type=float, default=2000.0,
                    help="acceleration limit, mm/s^2 (Klipper max_accel). The clock, "
                         "print-time total and animated head ramp at this accel; "
                         "<= 0 disables the re-timing -> the naive length/feed clock.")
    ap.add_argument("--square-corner-velocity", type=float, default=6.0,
                    help="speed held through a 90deg corner, mm/s (Klipper SCV); "
                         "higher -> faster corners / less slowdown")
    ap.add_argument("--rotary-accel", type=float, default=0.0,
                    help="the mandrel's rotary acceleration limit, deg/s^2; <= 0 "
                         "-> none, and --max-accel caps the whole move along the "
                         "surface. Set, --max-accel caps the linear axes only")
    ap.add_argument("--static", action="store_true",
                    help="unified-viewer default: open paused on the finished "
                         "scaffold with the machine / printhead / jet / travels "
                         "hidden (visually the plain STL view); un-pause to animate.")
    ap.add_argument("--goto-line", type=int, default=None,
                    help="'find in simulation': open PAUSED at the frame that "
                         "executes this 1-based g-code line, head/jet/deposition "
                         "revealed up to it. A non-motion line snaps to the next "
                         "move. Composes with --static (the embedded viewer).")
    ap.add_argument("--embed", default=None, metavar="NAME:WxH",
                    help="GUI-internal: run as the embedded-viewer child "
                         "(offscreen render, frames over the named shared "
                         "memory, controls exported to the GUI). Omitted -> "
                         "the classic windowed player.")
    # Machine-link: render the machine bodies moving in
    # lockstep with a real printer's toolhead position streamed from the GUI over the
    # embed channel ({"t":"livepos",...}). With no g-code that is the whole scene --
    # no deposition, no jet, no clock. Given the file the printer is running, the
    # scaffold is ALSO laid under the live nozzle, the playhead driven by the
    # printer's file cursor ({"t":"filepos",...}).
    ap.add_argument("--machine-live", action="store_true",
                    help="drive the machine bodies from a live streamed toolhead "
                         "position (Moonraker) instead of a g-code toolpath; requires "
                         "--machine. Pass the g-code the printer is running (+ --depo) "
                         "to also reveal the scaffold under the live nozzle.")
    ap.add_argument("--reveal-scaffold", action="store_true",
                    help="machine-live: lay the deposited scaffold mesh + jet under "
                         "the live nozzle (needs the printing g-code, and its .depo "
                         "sidecar for the exported geometry)")
    ap.add_argument("--reveal-gcode", action="store_true",
                    help="machine-live: draw the commanded g-code toolpath under the "
                         "live nozzle")
    return ap


def main(argv):
    ap = build_parser()
    args = ap.parse_args(argv[1:])
    if not 0.0 < args.diameter_multiplier < float("inf"):
        ap.error("--diameter-multiplier must be a positive number")
    import gcode_to_fibers
    gcode_to_fibers.TUBE_RADIUS_MULT = args.diameter_multiplier
    if not 1.0 <= args.sag_resolution < float("inf"):
        ap.error("--sag-resolution must be at least 1 (1 = shipped density)")
    # before any .depo is read or any march solved: both take it from the module
    gcode_to_fibers.set_sag_resolution(args.sag_resolution)

    if args.machine_live:
        if not args.machine:
            sys.stderr.write("gcode_player: --machine-live requires --machine\n")
            return 1
    elif not args.gcode or not os.path.isfile(args.gcode):
        sys.stderr.write(f"gcode_player: file not found: {args.gcode}\n")
        return 1
    groups = None
    if args.scaffold_groups:
        try:
            groups = json.loads(args.scaffold_groups)
        except ValueError:
            groups = None
    fiber_opts = dict(diameter=args.diameter, faces=args.faces, mode=args.mode,
                      no_sag=args.no_sag, sag_approx=args.sag_approx,
                      sag_multiplier=args.sag_multiplier,
                      sag_reset_layers=args.sag_reset_layers,
                      render_travel=args.render_travel,
                      travel_gcode_line=args.travel_gcode_line,
                      arc_segments=args.arc_segments, scaffold_groups=groups,
                      fiber_color=args.fiber_color,
                      render=args.render, mesh_tolerance=args.mesh_tolerance)
    origin = None
    if args.origin:
        try:
            vals = [float(v) for v in args.origin.split(",")]
            origin = vals if len(vals) == 3 else None
        except ValueError:
            origin = None
    embed = None
    if args.embed:
        from viewer_embed import EmbedHost
        embed = EmbedHost(args.embed)
    Player(args.gcode, args.stl, fiber_opts,
           standoff=args.standoff, lag=args.lag,
           head_stl=args.head_stl, head_scale=args.head_scale,
           physics=not args.no_physics, pressure=args.pressure,
           voltage=args.voltage, temperature=args.temperature,
           live_adjust=args.live_adjust, coil=args.coil,
           coil_wave_amp=args.coil_wave_amp, coil_wave_len=args.coil_wave_len,
           cone=not args.no_cone,
           max_accel=args.max_accel,
           square_corner_velocity=args.square_corner_velocity,
           rotary_accel=args.rotary_accel,
           physics_deposit=args.physics_deposit,
           dynamic_diameter=args.dynamic_diameter,
           fill_per_tau=args.fill_per_tau,
           corner_chord=_corner_chord_arg(args),
           physical_sag=args.physical_sag,
           physical_sag_viscous=args.physical_sag_viscous,
           physical_fusion=args.physical_fusion,
           calibration=args.calibration,
           machine=args.machine, origin=origin,
           hide_printhead=args.hide_printhead, static=args.static,
           embed=embed, depo=args.depo, plate=args.plate_stl,
           goto_line=args.goto_line,
           machine_live=args.machine_live,
           reveal_scaffold=args.reveal_scaffold,
           reveal_gcode=args.reveal_gcode,
           mandrel_diameter=args.mandrel,
           mandrel_length=args.mandrel_length,
           mandrel_start=args.mandrel_start,
           mandrel_surface_z=args.mandrel_surface_z).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
