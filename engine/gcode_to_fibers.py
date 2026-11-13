#!/usr/bin/env python3
"""
gcode_to_fibers.py

Convert 3D-printing G-code into an STL of solid fibers.

Each extruding move in the G-code becomes a tube (a swept polygonal cross
section) of a user-specified diameter. Travel (non-extruding) moves are
ignored, so the result is just the deposited material rendered as fibers.

Usage:
    python engine/gcode_to_fibers.py input.gcode -o fibers.stl --diameter 0.2 --faces 12

Mesh size: the sag solver samples each drooping span for the SOLVE, finer than a
`--faces`-gon tube can express, so the centreline is thinned on the way into the
tube sweep -- to within the tube's OWN faceting error r*(1-cos(pi/faces)).
`--mesh-tolerance 0` turns it off and restores the previous byte-identical STL.

Requires: numpy
"""

if __name__ == "__main__":  # run as a script: put the program's folders on sys.path
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "engine"))
    import app_paths  # noqa: E402,F401

import argparse
import glob
import json
import math
import os
import struct
import sys
import time

import numpy as np


# Modal feedrate (mm/min) assumed until the first F word, matching
# gcode_player.parse_path. In MEW the head holds ONE feed for the whole print,
# so travels move at this same speed as deposition. Only used when parse_gcode
# is asked to also report per-point print times (with_times=True).
DEFAULT_FEED = 600.0


# --------------------------------------------------------------------------- #
# Run progress (the GUI's determinate bar)
# --------------------------------------------------------------------------- #
# One line per update on stdout ("[PROGRESS] 0.4732 Solving the sag"), read by
# the GUI's output pump exactly as GCODE_READY_MARKER is (gui_run.py), and only
# under --progress. The number is a FRACTION OF THE WORK, never an estimate of
# the time left; the stage NAME rides along because the bar moves at different
# rates per stage.
PROGRESS_MARKER = "[PROGRESS]"

# What SHARE of a run each stage is. MEASURED off the timing recap main prints at
# the end of every run, not guessed:
#   20x20 / 0.4 / 10 layers   parse 0.3 %  sag 74.6 %  tube 9.1 %  write 16.0 %
#    8x8  / 1.0 / 20 layers   parse 0.9 %  sag 89.5 %  tube 3.9 %  write  5.7 %
# "mesh" also covers decimate_for_mesh, which the recap does not break out. The
# sag solve dominates, which is why it reports per LAYER rather than at its
# edges. A stage that will not run (--no-sag, --no-mesh, --from-depo) drops out
# and the rest renormalise, so the bar always ends at exactly 1.0.
PROGRESS_WEIGHTS = {
    "generate": 0.03,       # scaffold_gcode, before the chain reaches here
    "parse":    0.02,
    "sag":      0.72,
    "mesh":     0.13,       # decimate + the tube sweep
    "write":    0.10,
}

# The slice of the whole bar this program owns. (0.0, 1.0) run on its own;
# scaffold_gcode.py narrows it before its in-process `main(...)` chain so its own
# generation stage keeps the front. A module global for the same reason
# `_FIBER_TRANSFORM` is one: the chain is a function call, not a subprocess.
_PROGRESS_SPAN = (0.0, 1.0)


def set_progress_span(base, span):
    """Reserve `base .. base+span` of the progress bar for this program's run."""
    global _PROGRESS_SPAN
    _PROGRESS_SPAN = (float(base), float(span))


class _Progress:
    """Emits the progress lines for one run. Inert unless --progress is given.

    `stages` is the ordered list of stage keys that WILL run; the weights above
    are renormalised over exactly those, so a --no-mesh run still finishes at
    1.0 instead of stopping at 0.77.
    """

    def __init__(self, stages, enabled=True, out=None):
        self.enabled = bool(enabled)
        self._out = out if out is not None else sys.stdout
        base, span = _PROGRESS_SPAN
        total = sum(PROGRESS_WEIGHTS[k] for k in stages) or 1.0
        self._span = {}
        at = 0.0
        for k in stages:
            w = PROGRESS_WEIGHTS[k] / total
            self._span[k] = (base + span * at, base + span * (at + w))
            at += w
        self._end = base + span
        self._cur = None
        self._label = ""
        self._last = -1.0
        self._last_t = 0.0

    def stage(self, key, label):
        """Enter a stage; the bar jumps to the front of that stage's slice."""
        if key not in self._span:       # a stage this run turned out to skip
            return
        self._cur = key
        self._label = label
        self._emit(self._span[key][0], force=True)

    def tick(self, done, total):
        """Report progress WITHIN the current stage (done of total work items)."""
        if self._cur is None:
            return
        lo, hi = self._span[self._cur]
        self._emit(lo + (hi - lo) * (done / float(max(1, total))))

    def finish(self):
        self._emit(self._end, force=True)

    def _emit(self, frac, force=False):
        if not self.enabled:
            return
        frac = min(1.0, max(0.0, float(frac)))
        now = time.perf_counter()
        # Throttled to half a percent or 200 ms, whichever comes first: a
        # 400-layer solve would otherwise put 400 lines through a pipe the GUI
        # reads on a 120 ms poll.
        if not force and (frac - self._last < 0.005
                          and now - self._last_t < 0.2):
            return
        self._last = frac
        self._last_t = now
        print("%s %.4f %s" % (PROGRESS_MARKER, frac, self._label),
              file=self._out, flush=True)


# --------------------------------------------------------------------------- #
# Fibre-transform seam (off by default)
# --------------------------------------------------------------------------- #
# An optional hook applied inside main() to the freshly parsed fibres, BEFORE
# grouping/sag/tube: a `(paths, ptimes) -> (paths, ptimes)` callable that may
# reshape the centrelines. It must preserve the fibre count and order, or a
# caller's per-scaffold grouping stops lining up. None = identity, so the plain
# CLI behaves exactly as before. A module global, so it also threads through
# scaffold_gcode.py's in-process `gcode_to_fibers.main(...)` chain and BOTH
# export paths pick it up. Nothing here imports the physics modules -- the seam
# stays generic (`mew_export.py` installs the jet-touchdown transform).
_FIBER_TRANSFORM = None
# When the installed transform asks for travels, it is called as
# `(paths, ptimes, travels) -> (paths, ptimes[, diam_by_time])` and main() parses
# with_travels. It still must preserve the fibre count -- the physics transform
# MERGES each laid travel into the fibre it leads into rather than adding paths.
_FIBER_TRANSFORM_TRAVELS = False
# A frame-aware transform is passed the deposition frame as a keyword argument,
# so the physics export can tell a flat collector from a rotary one.
_FIBER_TRANSFORM_FRAME = False


def set_fiber_transform(transform, with_travels=False, with_frame=False):
    """Install (or clear, with None) the fibre-transform hook applied in main().
    See the `_FIBER_TRANSFORM` note above.

    with_travels: the hook also wants the travel (G0) geometry, so main() parses
    with_travels=True and passes `travels` as a third argument (the jet deposits
    through travels).

    with_frame: pass the active deposition frame as ``frame=``. Opt-in, because
    two/three-argument transforms are part of the old seam.
    """
    global _FIBER_TRANSFORM, _FIBER_TRANSFORM_TRAVELS, _FIBER_TRANSFORM_FRAME
    _FIBER_TRANSFORM = transform
    _FIBER_TRANSFORM_TRAVELS = bool(with_travels) and transform is not None
    _FIBER_TRANSFORM_FRAME = bool(with_frame) and transform is not None


def merge_travels(paths, ptimes, travels):
    """Splice each travel into the FRONT of the fibre it leads into.

    Returns (paths, ptimes) with the SAME fibre count and order -- a travel is
    never a new path, because callers index fibres positionally and the
    converter's per-scaffold grouping asserts sum(layers) == len(paths).

    In MEW the jet never stops, so a travel and the fibre after it are ONE
    continuous filament and the travel is real deposited material. The travel
    shares its endpoints exactly with the fibres either side, so the splice drops
    the duplicated junction vertex. travels[0] (the LEADING travel -- origin to
    the first part) is deposited like the rest; its start is the parser's ASSUMED
    origin, which for generator output IS the g-code frame.

    Layer clustering is unaffected: `_layer_ids` keys off each fibre's MEDIAN Z,
    which a short travel cannot move off the fibre's own layer."""
    out_p, out_t = [], []
    for i, (p, t) in enumerate(zip(paths, ptimes)):
        tv = travels[i] if (travels and i < len(travels)) else None
        if tv is None or tv[0] is None or len(tv[0]) < 2:
            out_p.append(np.asarray(p, dtype=np.float64))
            out_t.append(np.asarray(t, dtype=np.float64))
            continue
        out_p.append(np.concatenate([np.asarray(tv[0], dtype=np.float64),
                                     np.asarray(p, dtype=np.float64)[1:]],
                                    axis=0))
        out_t.append(np.concatenate([np.asarray(tv[1], dtype=np.float64),
                                     np.asarray(t, dtype=np.float64)[1:]]))
    # travels[len(paths)], when there is one, is the TRAILING travel (the
    # end-of-print exit; see parse_gcode): it leads OUT of the last fiber, so
    # it is spliced onto that fiber's END, sharing its first vertex.
    tv = trailing_travel(paths, travels)
    if tv is not None and out_p:
        out_p[-1] = np.concatenate([out_p[-1],
                                    np.asarray(tv[0], dtype=np.float64)[1:]],
                                   axis=0)
        out_t[-1] = np.concatenate([out_t[-1],
                                    np.asarray(tv[1], dtype=np.float64)[1:]])
    return out_p, out_t


def trailing_travel(paths, travels):
    """The trailing travel (the end-of-print exit) `parse_gcode` reported
    as travels[len(paths)], as (xyz, times), or None. Its first vertex is the
    last fiber's last."""
    if not travels or len(travels) <= len(paths):
        return None
    tv = travels[len(paths)]
    if tv is None or tv[0] is None or len(tv[0]) < 2 or tv[1] is None:
        return None
    return tv


def _trailing_travel(tv):
    """The trailing chain `take_travel` closed after the last fiber, less any
    points at its end that move in Z only (a lift lays nothing new across the
    collector), or None when nothing that moves across is left."""
    if tv is None:
        return None
    pts = np.asarray(tv[0], dtype=np.float64)
    k = len(pts)
    while k >= 2 and np.allclose(pts[k - 1, :2], pts[k - 2, :2],
                                 rtol=0.0, atol=1e-9):
        k -= 1
    if k < 2:
        return None
    return (pts[:k], None if tv[1] is None else np.asarray(tv[1])[:k])


def _drop_travel_spans(paths, ptimes, spans, keep_xy=0.0):
    """(paths, ptimes) with every point laid DURING a travel removed.

    The inverse of `merge_travels`, applied after the sag solve rather than
    before it, and applied by print TIME because indices do not survive the
    solve (see `travel_spans` in main). `spans` is a list of (t0, t1).

    The interval is half-open, `t0 <= t < t1`, and the two ends are not
    symmetric because the travel does not own both of them:

      * t1 is the JUNCTION with the fibre the travel leads into. `merge_travels`
        spliced the travel in as `travel + fibre[1:]`, so this vertex stands in
        for the fibre's own dropped first one -- cut it and the kept fibre starts
        a sample late, with a visible notch.
      * t0 is where the travel BEGAN, i.e. the end of the fibre before it. That
        preceding fibre's own last vertex carries the same time in its own array,
        so a plain `t >= t0` would truncate it. Hence the last-point exemption
        below: a fibre never loses its final vertex.

    The TRAILING travel (the end-of-print exit, spliced onto the END of the
    last fibre) is the mirror case, and it is told apart by the fibre ENDING
    inside it (`t0 < t_end <= t1`). There the fibre owns t0, its own last
    commanded vertex, and the travel owns everything after it, the final
    vertex included: the cut is `t > t0`.

    Fibre count and order are preserved (`path_group` and the per-fibre radius are
    parallel arrays); a fibre left with fewer than two points comes back empty,
    which the tube loop skips.

    `keep_xy` (mm, a scalar or one per fibre) exempts a run whose XY extent, from
    the vertex before it to the junction after it, is no longer than that. Such a
    run is not a travel across the collector: it is the LAYER STEP, the jet
    rising one fibre height at the corner where one layer ends and the next
    begins, and cutting it severs the filament -- the tubes meet as flat caps and
    the part is two bodies. A pure-Z hop is the limit case (zero XY). 0.0 (the
    default) keeps the old cut exactly.
    """
    lo = np.asarray([a for a, _b in spans], dtype=np.float64)
    hi = np.asarray([b for _a, b in spans], dtype=np.float64)
    per_fibre = np.ndim(keep_xy) != 0
    out_p, out_t = [], []
    for fi, (p, t) in enumerate(zip(paths, ptimes)):
        t = np.asarray(t, dtype=np.float64)
        p = np.asarray(p, dtype=np.float64)
        if t.size == 0 or t.size != len(p):
            out_p.append(p)
            out_t.append(t)
            continue
        t_end = float(t[-1])
        tail = (lo < t_end - 1e-9) & (t_end <= hi + 1e-9)
        inside = ((t[:, None] >= lo[~tail][None, :])
                  & (t[:, None] < hi[~tail][None, :])).any(axis=1)
        if tail.any():
            inside |= t > float(lo[tail].min())   # the exit: see the docstring
        else:
            inside[-1] = False                 # never drop a fibre's last vertex
        tol = float(keep_xy[fi] if per_fibre else keep_xy)
        if tol > 0.0 and inside.any():
            # un-drop each short run (see `keep_xy`)
            idx = np.flatnonzero(inside)
            brk = np.flatnonzero(np.diff(idx) > 1) + 1
            for run in np.split(idx, brk):
                lo_i, hi_i = max(int(run[0]) - 1, 0), min(int(run[-1]) + 1,
                                                          len(p) - 1)
                xy = p[lo_i:hi_i + 1, :2]
                if float(np.hypot(*np.diff(xy, axis=0).T).sum()) <= tol:
                    inside[run] = False
        keep = ~inside
        if keep.sum() < 2:
            out_p.append(np.zeros((0, 3), dtype=np.float64))
            out_t.append(np.zeros(0, dtype=np.float64))
        else:
            out_p.append(p[keep])
            out_t.append(t[keep])
    return out_p, out_t


# --------------------------------------------------------------------------- #
# G-code parsing
# --------------------------------------------------------------------------- #
# A G2/G3 true arc carries no segment count, so the re-expansion density matches
# the scaffold generator's G1-chord fidelity: `arc_segments` segments per
# half-turn. 8 -> 22.5 deg/segment, the SAME point count the equivalent G1 file
# would have, so an arc-mode STL costs no more to sag than a chord-mode one. The
# STL is only a preview (the machine interpolates the true arc), so this controls
# preview smoothness vs build time, nothing physical. Forwarded via the converter
# CLI (--arc-segments); the g-code stays comment-free.
_DEFAULT_ARC_SEGMENTS = 8

# Degrees of ROTARY swing per sub-vertex when a five-axis TRAVEL is drawn as the
# arc the collector actually turns through. The same 2.0 deg
# `gcode_player._SMOOTH_ARC_STEP_DEG` uses, and for the same reason: a move that
# swings the collector is a curve in the collector's own frame, and its two
# endpoints are a chord across that curve. Deposition moves need no such rule --
# the surface generator already emits fine chords (`--surface-sample-step`) --
# but a lead-in is ONE `G0` across the whole approach, and on the development rig its
# chord cuts 53 mm through the part it is approaching.
_TRAVEL_ARC_STEP_DEG = 2.0


def _arc_step_deg(arc_segments):
    """Degrees per chord for `arc_segments` segments per half-turn (180 deg).
    arc_segments=8 -> 22.5 deg. Higher = smoother arc preview, denser sag input."""
    return 180.0 / max(1, int(arc_segments))


_ARC_STEP_DEG = _arc_step_deg(_DEFAULT_ARC_SEGMENTS)   # 22.5 deg fallback


def arc_xy_points(sx, sy, ex, ey, i, j, cw, step_deg=_ARC_STEP_DEG):
    """Discretise a G2/G3 circular arc into XY points, EXCLUDING the start and
    INCLUDING the exact endpoint.

    The arc runs from (sx, sy) to (ex, ey) about centre (sx+i, sy+j) -- the I/J
    word convention (centre as an offset from the start). `cw` True is a G2
    (clockwise) arc, False a G3 (counter-clockwise) one. The centre offset sets
    the radius; the endpoint is snapped back onto it so tiny I/J rounding can't
    leave a gap. Returns [(ex, ey)] for a degenerate (zero-radius) arc.

    Shared by parse_gcode here and gcode_player.parse_path so the STL and the
    animation expand arcs identically."""
    cx, cy = sx + i, sy + j
    r = math.hypot(i, j)
    if r < 1e-12:
        return [(ex, ey)]
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    sweep = a1 - a0
    if cw:                              # G2: sweep must be negative (clockwise)
        while sweep >= 0.0:
            sweep -= 2.0 * math.pi
    else:                              # G3: sweep must be positive
        while sweep <= 0.0:
            sweep += 2.0 * math.pi
    n = max(1, int(math.ceil(abs(sweep) / math.radians(step_deg))))
    out = []
    for k in range(1, n + 1):
        ang = a0 + sweep * (k / n)
        out.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    out[-1] = (ex, ey)                  # exact endpoint, immune to I/J rounding
    return out


def _first_commanded_z(path):
    """The first absolute Z a motion command in `path` asks for, or None.

    The height the head must already have been at for the program to make sense --
    the ASSUMED pre-program Z when travel geometry is reported (see
    `parse_gcode`'s `travel_step`). Returns None when the first Z word cannot be
    read as a height: relative positioning (G91), or a work-offset (G92) touching
    X/Y/Z, makes the raw word a delta or a re-based coordinate. `travel_step` then
    falls back to clamping the chain's opening point, so None DEGRADES the
    re-datum rather than switching it off.

    An E-only `G92 E0` is NOT a reason to give up: it cannot move Z, and it sits
    in the header of nearly every slicer file (test_leading_travel_survives_a_
    g92_e0_header).
    """
    try:
        with open(path, "r", errors="ignore") as fh:
            for raw in fh:
                up = raw.split(";", 1)[0].upper()
                if not up.strip():
                    continue
                toks = up.replace("\t", " ").split()
                for i, tok in enumerate(toks):
                    if tok in ("G91", "G091"):
                        return None
                    if tok in ("G92", "G092") and any(
                            t[:1] in ("X", "Y", "Z") for t in toks[i + 1:]):
                        return None
                if toks[0] not in ("G0", "G00", "G1", "G01",
                                   "G2", "G02", "G3", "G03"):
                    continue
                for tok in toks[1:]:
                    if tok.startswith("Z") and len(tok) > 1:
                        try:
                            return float(tok[1:])
                        except ValueError:
                            return None
    except OSError:
        return None
    return None


#: Words that mark a file as driving a rotary collector. In such a file an E word
#: is not extrusion: on an Aerotech it is the feedrate of the rotary (dependent)
#: axes, and MEW never meters material by E anyway.
_ROTARY_FILE_WORDS = ("A", "B", "C", "U", "V")


def file_uses_extrusion(path):
    """True if any G0/G1 move carries an E word (slicer-style flow control) AND
    no move carries a rotary word -- see `_ROTARY_FILE_WORDS`. A split-feed
    mandrel file (`scaffold_gcode`'s rotary_feed="split") has an E on most of
    its moves, and reading those as extrusion parsed it as an empty print."""
    has_e = False
    with open(path, "r", errors="ignore") as fh:
        for raw in fh:
            line = raw.split(";", 1)[0]
            up = line.upper()
            if "G1" not in up and "G0" not in up:
                continue
            for tok in up.split():
                if len(tok) > 1 and (tok[1].isdigit() or tok[1] in "+-."):
                    if tok[0] in _ROTARY_FILE_WORDS:
                        return False
                    if tok[0] == "E":
                        has_e = True
    return has_e


def rotary_split_seconds(lin_mm, rot_deg, feed, rot_feed):
    """Seconds an Aerotech takes over one move under split feeds.

    F (mm/min) is the rate of the DOMINANT (linear) axes and E (deg/min) of the
    DEPENDENT (rotary) ones. Aerotech's Automation1 reference ("G-Code
    Commands"): a move with any dominant axis in it runs at F, the rotary axis
    following; only a move with no linear axis runs at E. Both rates must
    already be per MINUTE (a G76 file is scaled by its reader). A rotation-only
    move before any E is seen returns None, and the caller falls back on its
    own clock."""
    if lin_mm > 1e-12:
        return lin_mm / (feed / 60.0) if feed > 0 else 0.0
    if rot_deg > 1e-12:
        if not rot_feed:
            return None
        return rot_deg / (rot_feed / 60.0)
    return 0.0


# A g-code-header marker the string-art writer drops so the fiber converter skips
# the same-layer self-contact pass. A dense string-art path crosses itself
# thousands of times on ONE Z; resolving every crossing is both pointless (the
# strands are coplanar) and slow enough to hang the deposition viewer. Interlayer
# sag is unaffected.
NO_SELF_CROSS_MARKER = "mew:no-self-crossings"


def file_skips_self_crossings(path):
    """True if `path`'s g-code header carries NO_SELF_CROSS_MARKER. It lives in
    the header, so only the first few lines are scanned."""
    try:
        with open(path, "r", errors="ignore") as fh:
            for _ in range(40):
                line = fh.readline()
                if not line:
                    break
                if line.lstrip().startswith(";") and NO_SELF_CROSS_MARKER in line:
                    return True
    except OSError:
        pass
    return False


#: Rotary words a controller might carry that this parser does NOT read by
#: default. A word in here that reaches a move without a `five_axis` reader is
#: an error rather than a silent drop: Terranova 2024's six published files all
#: carry `U` and `V`, and parsing one of them before this guard existed returned
#: a path of 10 697 points from 14 140 moves -- the 3 443 moves that turn the
#: collector and nothing else vanished without a word, and the remainder drew a
#: shape that is not the print. That is the same failure mode as an unregistered
#: rotary axis in Klipper, which the mandrel emitter's docstring already warns
#: about, and the cure is the same: say so instead of answering `ok`.
_UNREAD_ROTARY_WORDS = ("U", "V", "B", "C")

#: The words a move may carry on a three- or four-axis file. A five-axis reader
#: extends it with ITS OWN joints' words; the tuple is not widened globally,
#: because a parse with no reader must stay byte-identical.
_MOVE_AXES = ("X", "Y", "Z", "E", "A")


class FiveAxisReader(object):
    """Reads a five-axis file's two rotary words back into COLLECTOR coordinates.

    The five-axis machine holds the nozzle still and vertical and moves the
    COLLECTOR under it, so a machine coordinate is not where the fibre went: the
    deposition point is the machine point taken back into the collector's own
    frame, which is the exact inverse of what `scaffold_gcode.generate_surface_gcode`
    did to write the file. Both sides use `machine_model.rotary_transform`, so
    there is one kinematic chain and not two.

    WHAT THIS RETURNS, AND WHY IT IS NOT `(u, v, h)`. The four-axis rotary parse
    unrolls to the surface chart in one step, because on a mandrel the machine
    word IS the chart coordinate. Here the two are separate questions with
    separate authorities: the WORDS belong to the machine and the CHART to the
    collector, and the arbitrary-STL collector section 5 rule 2 is protecting
    has no chart at all. So this answers in the collector's own XYZ, which is
    the question a triangle mesh can also answer, and a caller that wants the
    chart takes the answer on through `mew_frame.unwrap_path` -- which is what
    `main` does for a surface of revolution, and what a mesh frame would have
    nothing to offer.
    """

    def __init__(self, machine, frame=None):
        import machine_model

        rotaries = tuple(machine.rotaries)
        if len(rotaries) != 2:
            raise ValueError(
                "a five-axis read-back needs exactly two rotary joints, got "
                "%d. One joint is a mandrel: pass `frame=CylinderFrame(...)` "
                "instead." % (len(rotaries),))
        self.machine = machine
        self.rotaries = rotaries
        #: The words to READ, taken from the joints. A rig's words are a
        #: property of the rig -- Terranova's U/V are their machine's
        #: convention, not a standard -- so nothing here hardcodes a letter.
        self.words = tuple(j.word.upper() for j in rotaries)
        #: The COLLECTOR's own axial coordinate at its bottom edge, from the
        #: frame the file was projected onto -- the same number the emitter was
        #: given. It is what turns the Machine page's seat pick into a collector
        #: origin, and it is here rather than derived because the frame owns it.
        #: `None` (no frame) reads a file the way an uncalibrated profile writes
        #: one, which is byte-identical to every file written before the picks
        #: existed.
        self.mount_axial = None if frame is None else frame.mount_axial()
        self._mm = machine_model

    def collector_point(self, p):
        """Machine state -> the deposition point in the collector's frame."""
        import numpy as np

        angles = [j.angle_from_word(p.get(w, 0.0))
                  for j, w in zip(self.rotaries, self.words)]
        r, t = self._mm.rotary_transform(self.rotaries, angles,
                                         self.mount_axial)
        seat = (self.rotaries[0].seat() if len(self.rotaries) == 1
                else np.zeros(3))
        xyz = np.array([p["X"], p["Y"], p["Z"]], dtype=float)
        return [float(c) for c in (r.T @ (xyz - seat - t))]

    def collector_dir(self, p):
        """Machine state -> the NOZZLE'S OWN AXIS in the collector's frame.

        The nozzle is held still and vertical and the collector turns under it,
        so in the collector's frame the axis swings -- and it is what decides
        whether there is collector under the head at all
        (`mew_frame.DepositionFrame.under_nozzle`). Same rotation as
        `collector_point`, applied to the direction rather than the position,
        so the two cannot describe different poses.
        """
        import numpy as np

        angles = [j.angle_from_word(p.get(w, 0.0))
                  for j, w in zip(self.rotaries, self.words)]
        r, _t = self._mm.rotary_transform(self.rotaries, angles,
                                          self.mount_axial)
        return [float(c) for c in (r.T @ np.array([0.0, 0.0, -1.0]))]


def _rim_crossing_samples(frame, p0, p1, t0, t1, d0, d1, n=12):
    """Points spanning the exact instant the nozzle comes over the collector.

    A travel chain is sampled at a fixed rotary step, so the first sample that
    HAS collector under it is already past the crossing -- and near tangency
    that matters far more than the step size suggests. The nozzle's axis meets
    a curved collector first at its TANGENT point, and an advance of `eps` past
    that moves the touchdown by ~sqrt(2*R*eps): on the dome preset one 2 deg
    step put the first laid point 2.15 mm of meridian inside the rim, which is
    the bare band seen between the rim and the start of the travel fibre.
    A real travel fibre cannot begin partway up the collector.

    So the crossing is BISECTED along the segment -- position, nozzle axis and
    time all interpolated -- and the samples returned are spaced as the square
    of their distance past it, which is the inverse of the square root the
    touchdown moves by. The first one IS the crossing, so the fibre starts on
    the collector's own edge.
    """
    p0 = np.asarray(p0, dtype=np.float64)
    p1 = np.asarray(p1, dtype=np.float64)
    d0 = np.asarray(d0, dtype=np.float64)
    d1 = np.asarray(d1, dtype=np.float64)

    def at(lam):
        d = d0 + (d1 - d0) * lam
        nn = float(np.linalg.norm(d))
        return p0 + (p1 - p0) * lam, (d / nn if nn else d)

    lo, hi = 0.0, 1.0
    for _ in range(20):
        mid = 0.5 * (lo + hi)
        q, e = at(mid)
        if frame.under_nozzle(q, e):
            hi = mid
        else:
            lo = mid
    pts, ts, ds = [], [], []
    for j in range(n):
        lam = hi + (1.0 - hi) * (float(j) / n) ** 2
        q, e = at(lam)
        pts.append([float(c) for c in q])
        ts.append(t0 + (t1 - t0) * lam)
        ds.append([float(c) for c in e])
    return pts, ts, ds


def _finite_word(value, raw):
    """Refuse a g-code word that float() read as NaN or infinity."""
    if not math.isfinite(value):
        raise ValueError("a g-code word is not a finite number: %r"
                         % raw.strip())


def parse_gcode(path, mode="auto", with_times=False, arc_segments=None,
                with_travels=False, frame=None, five_axis=None):
    """
    Parse a G-code file into a list of polylines (continuous extruding paths).

    Returns a list of (N, 3) XYZ paths drawn while extruding; a new path starts
    whenever extrusion stops, so separate fibers are not joined. `mode` is "e" (a
    move extrudes only when E increases), "g1" (every G1 extrudes, G0 is travel --
    MEWron continuous flow) or "auto" (E words present -> "e"). G2/G3 arcs are
    re-expanded at `arc_segments` per half-turn (None -> _DEFAULT_ARC_SEGMENTS).

    with_times also returns `ptimes`, GLOBAL print seconds per point on the same
    modal-feed clock `gcode_player.parse_path` uses. with_travels ALSO returns
    `travels` ALIGNED TO `paths`: travels[i] is the (xyz, times) chain leading INTO
    paths[i], or None -- travels ride ALONGSIDE, never interleaved, because callers
    index fibers positionally. In MEW the jet NEVER STOPS: consecutive G0s coalesce
    into one chain sharing its endpoints exactly with the fibers either side.
    A chain AFTER the last fiber (the end-of-print exit) is one extra entry,
    travels[len(paths)] (`trailing_travel`); there is none when the file ends
    on a fiber.

    `frame` is a `mew_frame.DepositionFrame`, or None for a flat collector, and None
    is not a `PlanarFrame`: None takes the flat code path untouched, which is
    what makes "mandrel mode off is byte-identical" a property of the code. Under a
    ROTARY frame the 4th axis word `A` (DEGREES, right-hand rule about the mandrel
    axis) is read modally and points come back UNROLLED as `(u, v, h)`, so a pure
    rotation moves and a helix is a straight line; the clock advances on the TRUE
    surface length at the LOCAL radius r = R0 + h (McColl 2018 Eq 1.3). G2/G3 is
    REFUSED there rather than silently mis-drawn.

    `five_axis` is a `FiveAxisReader` and turns this into the five-axis parser:
    the file's TWO rotary words are read modally and every point comes back in
    the COLLECTOR's own XYZ, because the collector is what moves. It is separate
    from `frame` on purpose -- the words and the joints belong to the MACHINE,
    the surface only to the frame -- and with it None nothing below changes, so
    a three- or four-axis parse is byte-identical.
    """
    if mode == "auto":
        mode = "e" if file_uses_extrusion(path) else "g1"
    if arc_segments is None:
        arc_segments = _DEFAULT_ARC_SEGMENTS
    arc_step = _arc_step_deg(arc_segments)
    # Is there a 4th axis to read? `rotary` is the ONLY flag consulted below, so
    # frame=None and a PlanarFrame both fall through to the flat path.
    rotary = frame is not None and getattr(frame, "rotary", False)
    # Five axes is its own flag, not a frame test: `RevolutionFrame.rotary` is
    # True but its `unroll` raises, precisely because two rotary words have no
    # meaning without the joints that drive them.
    five = five_axis
    if five is not None:
        rotary = True
    # Machine state. "A" is carried unconditionally -- it costs one dict slot and
    # nothing reads it unless `rotary`, which keeps the flat path free of a branch.
    pos = {"X": 0.0, "Y": 0.0, "Z": 0.0, "E": 0.0, "A": 0.0}
    move_axes = _MOVE_AXES
    unread_rotary = _UNREAD_ROTARY_WORDS
    if five is not None:
        for w in five.words:
            pos.setdefault(w, 0.0)
        move_axes = _MOVE_AXES + tuple(w for w in five.words
                                       if w not in _MOVE_AXES)
        unread_rotary = tuple(w for w in _UNREAD_ROTARY_WORDS
                              if w not in five.words)

    def point(p):
        """The machine state as ONE deposited point, in the ROTARY frame.

        The deposition appends below inline the flat form rather than calling
        this: it runs once per emitted point, and routing the flat path through a
        call measured 6% slower (32.9 -> 34.9 ms) for a feature that is off.
        """
        if five is not None:
            return five.collector_point(p)
        return list(frame.unroll(p["X"], p["Y"], p["Z"], p["A"]))

    def travel_point(p, zval):
        """As `point`, but with `travel_step`'s substituted height (see `_z`)."""
        if five is not None:
            q = dict(p)
            q["Z"] = zval
            return five.collector_point(q)
        if rotary:
            return list(frame.unroll(p["X"], p["Y"], zval, p["A"]))
        return [p["X"], p["Y"], zval]

    def feed_span(p_from, p_to):
        """What the CLOCK divides by the feed -- not always a distance.

        Flat: the XYZ Euclidean length, exactly as before. Four-axis: McColl Eq
        1.3's effective velocity path, taken at the mean deposition radius of
        the move. Five-axis: `mew_kinematics.machine_norm`, the firmware's own
        mixed mm-and-degrees norm -- see the branch.
        """
        if not rotary:
            return math.sqrt(sum((p_to[a] - p_from[a]) ** 2
                                 for a in ("X", "Y", "Z")))
        if five is not None:
            # THE FIRMWARE'S NORM, NOT THE DEPOSITION POINT'S TRAVEL. F is a
            # property of the controller, and a controller has no idea where
            # the deposition point is: it divides F by the norm of the
            # COMMANDED axis deltas, mm and degrees in quadrature, which is
            # `mew_kinematics.machine_norm` and is the convention this repo
            # measured on Terranova 2024's six published files (see the note
            # above that function).
            #
            # This branch does NOT return the collector-frame arc the
            # deposition point walks. That IS the right weight for the
            # physics, and `mew_deposition` still gets it -- but it is the
            # wrong divisor for the clock, and the two are only equal when the
            # rotary words contribute nothing. Near a pole they contribute
            # almost everything: an apex-centred dome R10 at a flat F800
            # measured 1309.3 s on the controller against 77.6 s here,
            # 16.9x fast.
            #
            # It is also exactly what `--surface-feed-comp` is defined against:
            # `mew_kinematics.surface_feeds` emits `speed * norm / arc` so that
            # `norm / F` comes out as `arc / speed`. So on a COMPENSATED file
            # this returns the same segment times the arc would, and on an
            # uncompensated one it returns the machine's.
            #
            # The words are the joints' own (`five.words`) and the deltas are
            # taken on the commanded READINGS, which is what the firmware sees;
            # a joint's `zero_deg` is a pure offset and cancels in a delta.
            import mew_kinematics

            return mew_kinematics.machine_norm(
                [p_to[a] - p_from[a] for a in ("X", "Y", "Z")],
                [p_to[w] - p_from[w] for w in five.words])
        u0, v0, h0 = frame.unroll(p_from["X"], p_from["Y"], p_from["Z"], p_from["A"])
        u1, v1, h1 = frame.unroll(p_to["X"], p_to["Y"], p_to["Z"], p_to["A"])
        a, b, c = frame.metric(0.5 * (u0 + u1), 0.5 * (v0 + v1),
                               0.5 * (h0 + h1))
        arc = ((v1 - v0) - c * (u1 - u0)) * b
        return math.sqrt(((u1 - u0) * a) ** 2 + arc ** 2 + (h1 - h0) ** 2)
    # Until the first Z word, `pos["Z"]` above is an ASSUMPTION about where the head
    # was before the file, not a height the program set. `pre_z` is the better
    # assumption; only `travel_step` consults either -- see the Z note there.
    pre_z = _first_commanded_z(path) if with_travels else None
    z_seen = False
    absolute_xyz = True      # G90 / G91
    absolute_e = True        # M82 / M83
    feed = DEFAULT_FEED      # modal feed (mm/min); only consulted when with_times
    # SPLIT FEEDS (Aerotech) on a mandrel: E is the rotary rate in deg/min and
    # the clock is `rotary_split_seconds`. Switched on by the G75/G76 header the
    # emitter writes, or by the first E word; the flat and five-axis paths
    # never read either, so they are untouched.
    split_rot = rotary and five is None
    split = False
    rot_feed = None          # deg/min, modal
    per = 1.0                # 60 after G76: F and E are per second
    t_cum = 0.0              # cumulative print seconds, travels included

    paths = []
    ptimes = []              # per-fiber arrays of global time at each point
    current = []
    current_t = []

    # --- travel capture (with_travels; see the docstring) ------------------ #
    # `cur_tv` accumulates the running G0 chain; when the next fiber OPENS it is
    # taken as that fiber's inbound travel and parked in `pending_tv` until
    # flush() commits the fiber, so `travels` stays index-aligned to `paths`; the
    # chain left open after the last fiber (the exit) is appended once at the end.
    travels = []
    cur_tv = []
    cur_tv_t = []
    # ...and the NOZZLE'S AXIS at each of those points, in collector
    # coordinates. `None` on every three- and four-axis parse, which is what
    # makes `under_nozzle` the plain lateral answer there -- see take_travel.
    cur_tv_d = []
    pending_tv = None

    def take_travel():
        """Close the running G0 chain and return it as (xyz, times) or None.

        THE CHAIN IS CLIPPED TO THE COLLECTOR IT LANDS ON. In MEW the jet never
        stops, so every travel is deposited -- but that is only true where
        there is collector underneath to deposit ONTO. On a flat plate and on a
        mandrel there always is (`frame.covers` is True everywhere on both), so
        those two are untouched and stay the comparator. A five-axis rig zeroes
        OFF its collector on purpose, so its lead-in flies in from beside the
        part: measured on the development rig, 35.3 mm outside the dome's rim at the
        start, crossing it only 85% of the way in. Depositing the whole chain
        is what `2d920fb` did, and `merge_travels` then handed a 34 mm-high
        point to `mew_frame.unwrap_path`, which took the wrong azimuth branch
        and re-based the whole first fibre until both sag solvers refused the
        chart.

        The clip takes the LAST run of covered points, never a middle one: the
        chain has to keep its final vertex, because `merge_travels` splices it
        in sharing that vertex exactly with the fibre it leads into. A chain
        with fewer than two covered points at its end comes back None, which is
        the same answer this returned for a chain too short to draw.
        """
        nonlocal cur_tv, cur_tv_t, cur_tv_d
        pts, ts, ds = cur_tv, cur_tv_t, cur_tv_d
        if len(pts) >= 2 and frame is not None:
            # THE CLIP IS THE NOZZLE'S OWN QUESTION, not the point's. `covers`
            # projects RADIALLY from the collector's axis, so on a curved
            # collector it opens a cone -- wider than the collector at every
            # height above it. Measured on the dome preset: it answered yes
            # while the nozzle was 4.83 mm outside the collector in XY and
            # 0.49 s from being over it, and that is where the travel fibre
            # started, with the jet snapping onto it. The travel
            # fibre must start the moment the nozzle is over any part of the
            # collector, and not before. `under_nozzle` casts the nozzle axis instead, and
            # is the plain `covers` answer with no axis to cast -- so the flat
            # plate and the mandrel are untouched and stay the comparator.
            k = len(pts)
            while k > 0 and frame.under_nozzle(
                    pts[k - 1], ds[k - 1] if ds else None):
                k -= 1
            if k and k < len(pts):      # k == 0 -> the whole chain is covered
                # the clipped chain opens at the first SAMPLE with collector
                # under it, which is already past the crossing -- refine it
                if ds and ds[k - 1] is not None and ds[k] is not None:
                    ip, it, idd = _rim_crossing_samples(
                        frame, pts[k - 1], pts[k], ts[k - 1], ts[k],
                        ds[k - 1], ds[k])
                    pts, ts, ds = ip + pts[k:], it + ts[k:], idd + ds[k:]
                else:
                    pts, ts, ds = pts[k:], ts[k:], ds[k:] if ds else ds
            elif k:
                pts, ts, ds = pts[k:], ts[k:], ds[k:] if ds else ds
            # THE LEADING CHAIN IS THE ONLY ONE LAID DOWN, and "leading" is
            # exact rather than a threshold: no fibre has been committed yet,
            # so this is the one chain in the file that starts at the parser's
            # ASSUMED pre-program position instead of at a commanded point.
            # That is precisely the chain whose points are wherever the axes
            # happen to be rather than on the surface. Clipping alone does not
            # cover it -- an approach can be over the collector for its whole
            # length and still run above it (measured on `test_five_axis_gcode`'s
            # own fixture: nothing clipped, and 2.366 mm of float).
            #
            # WHETHER it is laid down is the FRAME's answer, not this parser's,
            # and a plane and a mandrel both say no -- their lead-in is the
            # comparator and stays byte-identical. Deciding it here instead
            # lengthened a 4-axis tube's approach fibre from 40 mm to 40.0281.
            if not paths:
                frame.lay_lead_in(pts, ds if ds else None)
        tv = None
        if len(pts) >= 2:
            tv = (np.array(pts, dtype=np.float64),
                  np.array(ts, dtype=np.float64) if with_times else None)
        cur_tv = []
        cur_tv_t = []
        cur_tv_d = []
        return tv

    def travel_step(p_from, p_to, t_from, t_to):
        """Extend the running G0 chain by one moving travel segment.

        The PROGRAM'S FIRST chain opens at an assumed position, and with
        `--render-travel` that assumption is deposited as real fibre. X and Y are
        kept: the origin datum is a reachable place and the approach from it is
        the one the rig really makes. Z is NOT, because an assumed Z of 0 is the
        g-code floor while every print raised onto a build plate has its first
        commanded Z at the plate top -- opening the chain at 0 drew a fibre
        climbing THROUGH the plate, whose diced sub-fibre medians then pulled
        `apply_sag`'s bed plane down and seated the scaffold one plate thickness
        low ("phases into the plate").

        So until the first Z word the chain is written at `pre_z`, the first
        height the program commands (`_first_commanded_z`). A homing lift ABOVE
        the print is left alone: it is a real position, and the bed is the LOWEST
        per-fibre median anyway.

        When that height cannot be read (`pre_z is None`) the re-datum DEGRADES
        rather than switching off: the opening point still takes its own
        destination's Z. Reverting to 0.0 there put the sink back once already
        (test_unreadable_first_z_still_falls_back_to_the_opening_clamp).

        Only `travels` is affected; deposition always starts at a commanded point,
        so "paths are UNCHANGED by with_travels" still holds.
        """
        def _z(p, fallback=None):
            if z_seen or five is not None:
                # FIVE AXIS TAKES ITS OWN Z, and the re-datum above does not
                # apply to it. `pre_z` exists because a flat print's assumed
                # pre-program Z of 0 is the g-code floor while the first
                # commanded Z is the plate top, so opening the chain at 0 drew
                # a fibre climbing through the plate. On a five-axis file
                # g-code (0, 0, 0) is not an unknown: `rotary_transform`
                # subtracts `collector_home_offset`, so it IS the picked home,
                # a real place with a real height. Substituting the first
                # commanded Z there moves the whole approach -- measured, it
                # put the lead-in's rim crossing at 51.7% of the move instead
                # of 68.3% and left the clipped fibre starting 13.895 mm off
                # the dome instead of 5.834 mm.
                return p["Z"]
            if pre_z is not None:
                return pre_z
            return p["Z"] if fallback is None else fallback
        if not cur_tv:
            # the opening point's own destination is the fallback floor
            cur_tv.append(travel_point(p_from, _z(p_from, p_to["Z"])))
            cur_tv_t.append(t_from)
            cur_tv_d.append(None if five is None else five.collector_dir(p_from))
        # A FIVE-AXIS TRAVEL IS DRAWN AS THE ARC THE COLLECTOR TURNS THROUGH,
        # not as the chord between its endpoints. Deposition needs no such rule
        # here -- the surface generator emits fine chords -- but a lead-in is
        # ONE `G0` carrying the whole approach, and its two endpoints say
        # nothing about where the path runs in between. That matters twice:
        # `take_travel` has to find where the chain crosses the collector's rim,
        # which a two-point chain cannot express, and the fibre that survives
        # the clip has to lie on the collector rather than cut across it.
        #
        # FIVE-AXIS ONLY, and that is the contract rather than an oversight: a
        # mandrel's travels stay two-point, so `mew_deposition`'s flat and
        # 4-axis outputs (digests once held by `test_mandrel_placement`) are byte-identical through
        # this change. A rod is covered everywhere anyway, so nothing there is
        # waiting on a rim crossing.
        n = 1
        if five is not None:
            swing = max((abs(p_to[w] - p_from[w]) for w in five.words),
                        default=0.0)
            n = max(1, int(math.ceil(swing / _TRAVEL_ARC_STEP_DEG)))
        for i in range(1, n + 1):
            f = float(i) / n
            # THE LAST SAMPLE IS THE TARGET ITSELF, not the target computed.
            # `merge_travels` splices a chain in sharing its final vertex with
            # the fibre it leads into, and `a + (b - a) * 1.0` is not `b`: it
            # was exact only while every travel started from the assumed
            # pre-program position of all zeros, and a lead-in routed through a
            # waypoint (`scaffold_gcode._leadin_approach`) does not
            # (test_five_axis_gcode, 4.4e-16 mm of Y).
            q = (dict(p_to) if i == n else
                 {k: p_from[k] + (p_to[k] - p_from[k]) * f for k in p_from})
            cur_tv.append(travel_point(q, _z(q)))
            cur_tv_t.append(t_from + (t_to - t_from) * f)
            cur_tv_d.append(None if five is None else five.collector_dir(q))

    def flush():
        nonlocal current, current_t, pending_tv
        if len(current) >= 2:
            paths.append(np.array(current, dtype=np.float64))
            if with_times:
                ptimes.append(np.array(current_t, dtype=np.float64))
            if with_travels:
                travels.append(pending_tv)
        current = []
        current_t = []
        pending_tv = None

    with open(path, "r", errors="ignore") as fh:
        for raw in fh:
            line = raw.split(";", 1)[0].strip()   # strip inline comments
            if not line:
                continue

            tokens = line.split()
            code = tokens[0].upper()

            # --- modal state commands ---------------------------------- #
            if code == "G90":
                absolute_xyz = True
                absolute_e = True
                continue
            if code == "G91":
                absolute_xyz = False
                absolute_e = False
                continue
            if code == "M82":
                absolute_e = True
                continue
            if code == "M83":
                absolute_e = False
                continue
            if split_rot and code in ("G75", "G76"):
                split = True
                per = 60.0 if code == "G76" else 1.0
                continue
            if code in ("G20", "G020"):
                # every length below is read as mm; an inch file drawn that
                # way is a part 25.4x too small, with nothing to say so
                raise ValueError(
                    "G20 (inch units) is not supported: every length is read "
                    "in millimetres (G21). Re-export the file in mm: %r"
                    % raw.strip())

            # --- G92: set position without moving ---------------------- #
            if code == "G92":
                # `pos` already carries the reader's words, so a five-axis
                # file's `G92 ... U90 V0` datum lands here and is not dropped.
                for tok in tokens[1:]:
                    axis = tok[0].upper()
                    if axis in pos:
                        try:
                            pos[axis] = float(tok[1:])
                        except ValueError:
                            pass
                        else:
                            _finite_word(pos[axis], raw)
                continue

            # --- moves ------------------------------------------------- #
            if code in ("G0", "G1", "G00", "G01"):
                words = {}
                for tok in tokens[1:]:
                    axis = tok[0].upper()
                    if axis in move_axes:
                        try:
                            words[axis] = float(tok[1:])
                        except ValueError:
                            pass
                    elif axis in unread_rotary:
                        raise ValueError(
                            "this move carries the rotary word %r, which the "
                            "parse was not given a reader for: %r\nA "
                            "five-axis file needs `parse_gcode(..., "
                            "five_axis=FiveAxisReader(machine))` with the "
                            "machine whose joints use that word. Dropping it "
                            "keeps only the moves that ALSO move XYZ, and "
                            "draws a shape that is not the print."
                            % (axis, raw.strip()))
                    elif axis == "F" and with_times:
                        try:
                            f = float(tok[1:])
                        except ValueError:
                            pass
                        else:
                            _finite_word(f, raw)
                            if f > 0:
                                feed = f * per
                if split_rot and "E" in words:
                    split = True
                    if words["E"] > 0:
                        rot_feed = words["E"] * per
                    del words["E"]      # a rate, never an extrusion position
                # float() reads 'nan' and 'inf'; one sum catches either in
                # any word (a sum of finite words is finite). Both spellings
                # hold an 'n', so a plain move line skips the sum (~1 % of a
                # 900k-move parse either way)
                if (("n" in line or "N" in line)
                        and not math.isfinite(sum(words.values()))):
                    _finite_word(float("nan"), raw)

                # Compute the new XYZ(A) position
                new_pos = dict(pos)
                for axis in move_axes:
                    if axis == "E":
                        continue
                    if axis in words:
                        new_pos[axis] = words[axis] if absolute_xyz else pos[axis] + words[axis]

                # Compute extrusion delta
                if "E" in words:
                    if absolute_e:
                        e_delta = words["E"] - pos["E"]
                        new_pos["E"] = words["E"]
                    else:
                        e_delta = words["E"]
                        new_pos["E"] = pos["E"] + words["E"]
                else:
                    e_delta = 0.0

                if rotary:
                    # the 4th axis MOVES the deposition point, so the move test
                    # has to be taken in the surface frame -- taking it in XYZ is
                    # exactly what discards a pure rotation today (§4.2).
                    _a, _b = point(pos), point(new_pos)
                    moved_xyz = any(abs(_b[i] - _a[i]) > 1e-12 for i in (0, 1, 2))
                else:
                    moved_xyz = any(
                        abs(new_pos[a] - pos[a]) > 1e-12 for a in ("X", "Y", "Z")
                    )
                if mode == "g1":
                    # continuous flow: every G1 deposits, G0 is travel
                    is_g1 = code in ("G1", "G01")
                    extruding = is_g1 and moved_xyz
                else:
                    extruding = e_delta > 1e-9 and moved_xyz

                # advance the global clock for any real motion (travels
                # included, exactly like gcode_player.parse_path) so the
                # deposition times line up with the playhead
                if with_times:
                    advance = None
                    if split:
                        advance = rotary_split_seconds(
                            math.sqrt(sum((new_pos[a] - pos[a]) ** 2
                                          for a in ("X", "Y", "Z"))),
                            abs(new_pos["A"] - pos["A"]), feed, rot_feed)
                    if advance is None:
                        seg_len = feed_span(pos, new_pos)
                        advance = seg_len / (feed / 60.0) if (
                            seg_len > 1e-12 and feed > 0) else 0.0
                else:
                    advance = 0.0

                if extruding:
                    if not current:
                        # start the path at the previous position, reached at the
                        # current clock value (after any preceding travel) -- which
                        # is exactly where the inbound G0 chain ended, so taking it
                        # here makes fiber+travel+fiber one continuous polyline.
                        if with_travels:
                            pending_tv = take_travel()
                        current.append(point(pos) if rotary
                                       else [pos["X"], pos["Y"], pos["Z"]])
                        if with_times:
                            current_t.append(t_cum)
                    t_cum += advance
                    current.append(point(new_pos) if rotary
                                   else [new_pos["X"], new_pos["Y"], new_pos["Z"]])
                    if with_times:
                        current_t.append(t_cum)
                elif mode == "g1":
                    # continuous flow: only a G0 that actually moves is a real
                    # travel. Zero-length G0 markers are stitched through so the
                    # fiber stays continuous.
                    t0 = t_cum
                    t_cum += advance
                    if moved_xyz:
                        flush()
                        if with_travels:
                            travel_step(pos, new_pos, t0, t_cum)
                else:
                    # e-mode: travel / retraction / Z-hop ends the fiber
                    t0 = t_cum
                    t_cum += advance
                    flush()
                    # only a chain that MOVES is travel geometry; a bare `G1 F900`
                    # or a pure retraction ends the fiber without going anywhere.
                    if with_travels and moved_xyz:
                        travel_step(pos, new_pos, t0, t_cum)

                pos = new_pos
                if "Z" in words:
                    z_seen = True           # pos["Z"] is a height the program set
                continue

            # --- arcs: G2 (CW) / G3 (CCW) ------------------------------ #
            # A curved deposition move on machines that interpolate arcs. We
            # re-expand it into the same dense polyline the tube sweep / sag and
            # the animation already consume, so an arc file renders exactly like
            # the equivalent G1-chord file. Endpoint X/Y obey G90/G91; I/J are
            # the centre offset from the arc start (the standard convention the
            # scaffold generator emits); Z is modal (planar arcs).
            if code in ("G2", "G3", "G02", "G03"):
                if rotary:
                    # I/J describe an arc in the XY PLANE. Under a rotary frame
                    # that plane is not the deposition surface, so re-expanding
                    # it would draw a curve that is nowhere near the fibre. Fail
                    # loudly rather than return plausible wrong geometry; nothing
                    # emits arcs in mandrel mode.
                    raise ValueError(
                        "G2/G3 arcs are not supported under a rotary deposition "
                        "frame (%s): I/J are XY-plane offsets and the mandrel "
                        "surface is not that plane. Emit G1 chords instead."
                        % type(frame).__name__)
                words = {}
                for tok in tokens[1:]:
                    axis = tok[0].upper()
                    if axis in ("X", "Y", "Z", "I", "J", "E"):
                        try:
                            words[axis] = float(tok[1:])
                        except ValueError:
                            pass
                    elif axis == "R":
                        # the radius form names no centre; read as I=J=0 it
                        # was drawn as a straight chord
                        raise ValueError(
                            "a G2/G3 arc in the radius (R) form is not "
                            "supported: give the centre as I/J offsets: %r"
                            % raw.strip())
                    elif axis == "F" and with_times:
                        try:
                            f = float(tok[1:])
                        except ValueError:
                            pass
                        else:
                            _finite_word(f, raw)
                            if f > 0:
                                feed = f
                if not math.isfinite(sum(words.values())):
                    _finite_word(float("nan"), raw)

                new_pos = dict(pos)
                for axis in ("X", "Y", "Z"):
                    if axis in words:
                        new_pos[axis] = (words[axis] if absolute_xyz
                                         else pos[axis] + words[axis])
                if "E" in words:
                    if absolute_e:
                        e_delta = words["E"] - pos["E"]
                        new_pos["E"] = words["E"]
                    else:
                        e_delta = words["E"]
                        new_pos["E"] = pos["E"] + words["E"]
                else:
                    e_delta = 0.0

                cw = code in ("G2", "G02")
                arc_xy = arc_xy_points(pos["X"], pos["Y"],
                                       new_pos["X"], new_pos["Y"],
                                       words.get("I", 0.0), words.get("J", 0.0),
                                       cw, step_deg=arc_step)
                # an arc deposits in g1/continuous-flow mode, or (e-mode) when its
                # E advances; otherwise it is a rare non-extruding repositioning
                extruding = True if mode == "g1" else (e_delta > 1e-9)

                if extruding:
                    if not current:
                        if with_travels:
                            pending_tv = take_travel()
                        current.append([pos["X"], pos["Y"], pos["Z"]])
                        if with_times:
                            current_t.append(t_cum)
                    prevx, prevy = pos["X"], pos["Y"]
                    for (ax_, ay_) in arc_xy:
                        if with_times:
                            seg_len = math.hypot(ax_ - prevx, ay_ - prevy)
                            t_cum += (seg_len / (feed / 60.0)
                                      if (seg_len > 1e-12 and feed > 0) else 0.0)
                        current.append([ax_, ay_, new_pos["Z"]])
                        if with_times:
                            current_t.append(t_cum)
                        prevx, prevy = ax_, ay_
                else:
                    if with_times:
                        prevx, prevy = pos["X"], pos["Y"]
                        for (ax_, ay_) in arc_xy:
                            seg_len = math.hypot(ax_ - prevx, ay_ - prevy)
                            t_cum += (seg_len / (feed / 60.0)
                                      if (seg_len > 1e-12 and feed > 0) else 0.0)
                            prevx, prevy = ax_, ay_
                    flush()

                pos = new_pos
                if "Z" in words:
                    z_seen = True           # planar arc, but the word still sets Z
                continue

            # any other command (M-codes, etc.) is ignored

    flush()
    if with_travels:
        # The chain still open here is the TRAILING travel: the end-of-print
        # exit, after the last fiber. The jet never stops, so it is laid fiber
        # like every other travel -- the one the exit move routes off the part
        # -- and it rides as ONE EXTRA entry, travels[len(paths)]. Index-aligned
        # callers read travels[i] for i < len(paths) and never see it;
        # `merge_travels` splices it onto the END of the last fiber. A pure lift
        # at the end lays nothing new across the collector and is trimmed.
        tail = _trailing_travel(take_travel()) if paths else None
        if tail is not None:
            travels.append(tail)
        return (paths, ptimes, travels) if with_times else (paths, travels)
    return (paths, ptimes) if with_times else paths


# --------------------------------------------------------------------------- #
# Geometry: sweep a polygon cross-section along a polyline
# --------------------------------------------------------------------------- #
def _dedupe(points, tol=1e-9):
    """Remove consecutive duplicate points (those coincident with the previous
    KEPT point). Defined via _dedupe_idx so the keep-rule lives in one place and
    can't drift between the two; also returns empty for empty input."""
    pts = np.asarray(points, dtype=np.float64)
    return pts[_dedupe_idx(pts, tol)]


def _dedupe_idx(points, tol=1e-9):
    """Indices of the points _dedupe() keeps (it drops points coincident with
    the previous KEPT point), so a parallel per-point array -- e.g. the print
    times from parse_gcode(with_times=True) -- can be deduped in lockstep with
    the geometry and stay aligned.

    Most input has no coincident points at all, so that case is answered with one
    vectorised consecutive-distance check; only input that actually contains a
    duplicate falls back to the sequential keep-rule (which must compare against
    the last KEPT point, an inherently serial chain).

    That fallback is a hot path, not a rarity -- the APPROXIMATE sag solver emits
    8.2% coincident-consecutive points against the exact solver's ~0% -- so the
    loop runs on PLAIN PYTHON FLOATS from one .tolist(), the trick _rmf_frames
    uses. Same keep-rule, so the geometry stays bit-identical."""
    pts = np.asarray(points, dtype=np.float64)
    n = len(pts)
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    d = np.linalg.norm(pts[1:] - pts[:-1], axis=1)
    if bool((d > tol).all()):
        return np.arange(n, dtype=np.int64)
    pl = pts.tolist()
    t2 = float(tol) * float(tol)
    kx, ky, kz = pl[0]
    keep = [0]
    add = keep.append
    for i in range(1, n):
        x, y, z = pl[i]
        dx = x - kx
        dy = y - ky
        dz = z - kz
        if dx * dx + dy * dy + dz * dz > t2:
            add(i)
            kx, ky, kz = x, y, z
    return np.asarray(keep, dtype=np.int64)


def _tangents(points):
    """Per-vertex unit tangents (averaged at interior vertices)."""
    n = len(points)
    seg = points[1:] - points[:-1]
    seg_len = np.linalg.norm(seg, axis=1, keepdims=True)
    seg_len[seg_len == 0] = 1.0
    seg_dir = seg / seg_len

    tang = np.zeros((n, 3))
    tang[0] = seg_dir[0]
    tang[-1] = seg_dir[-1]
    if n > 2:
        tang[1:-1] = seg_dir[:-1] + seg_dir[1:]
    norms = np.linalg.norm(tang, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return tang / norms


def _rmf_frames(points, tangents):
    """
    Rotation-minimizing frames via the double-reflection method
    (Wang et al. 2008). Returns (normals, binormals) per vertex.

    The recurrence is inherently sequential (each frame is reflected from the
    previous one), so the propagation runs as a Python loop, and the per-step
    vector maths is written out in scalars read from PLAIN PYTHON LISTS
    (`.tolist()` up front). For length-3 vectors the numpy call and
    item-assignment overhead dwarfs the arithmetic. float64 -> Python float is
    exact both directions, so the result is bit-identical.
    """
    n = len(points)

    t0 = tangents[0]
    # pick an arbitrary reference axis not parallel to t0
    ref = np.array([0.0, 0.0, 1.0]) if abs(t0[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    r = np.cross(ref, t0)
    r /= np.linalg.norm(r)

    pl = points.tolist()
    tl = tangents.tolist()
    # carry the current normal (== normals[i]) in scalars across the loop
    nx, ny, nz = float(r[0]), float(r[1]), float(r[2])
    out = [(nx, ny, nz)]
    emit = out.append
    px, py, pz = pl[0]
    for i in range(n - 1):
        qx, qy, qz = pl[i + 1]
        v1x = qx - px
        v1y = qy - py
        v1z = qz - pz
        px, py, pz = qx, qy, qz
        c1 = v1x * v1x + v1y * v1y + v1z * v1z
        if c1 < 1e-18:
            emit((nx, ny, nz))
            continue
        f1 = 2.0 / c1
        # reflect the normal and the tangent across the segment's bisecting plane
        d_rn = v1x * nx + v1y * ny + v1z * nz
        rlx = nx - f1 * d_rn * v1x
        rly = ny - f1 * d_rn * v1y
        rlz = nz - f1 * d_rn * v1z
        tix, tiy, tiz = tl[i]
        d_tn = v1x * tix + v1y * tiy + v1z * tiz
        tlx = tix - f1 * d_tn * v1x
        tly = tiy - f1 * d_tn * v1y
        tlz = tiz - f1 * d_tn * v1z
        # second reflection aligns the reflected tangent with the next one
        t2x, t2y, t2z = tl[i + 1]
        v2x = t2x - tlx
        v2y = t2y - tly
        v2z = t2z - tlz
        c2 = v2x * v2x + v2y * v2y + v2z * v2z
        if c2 < 1e-18:
            rnx, rny, rnz = rlx, rly, rlz
        else:
            f2 = 2.0 / c2
            d_rl = v2x * rlx + v2y * rly + v2z * rlz
            rnx = rlx - f2 * d_rl * v2x
            rny = rly - f2 * d_rl * v2y
            rnz = rlz - f2 * d_rl * v2z
        nrm = math.sqrt(rnx * rnx + rny * rny + rnz * rnz)
        if nrm > 0.0:                       # else keep the previous normal
            nx, ny, nz = rnx / nrm, rny / nrm, rnz / nrm
        emit((nx, ny, nz))

    normals = np.array(out, dtype=np.float64)
    binormals = np.cross(tangents, normals)
    return normals, binormals


# Visualization-only scale on every tube's radius (`--diameter-multiplier`). It
# is applied here, where the tube is swept, so it is downstream of the sag solve,
# the contact radii and the .depo cache and cannot change any of them.
TUBE_RADIUS_MULT = 1.0


def tube_mesh(points, radius, faces, vert_offset, with_normals=False):
    """
    Build a tube (with end caps) along a polyline.

    Returns (vertices, faces_idx) where vertices is an (V, 3) float array and
    faces_idx is an (T, 3) int array of triangles whose indices are global
    (offset by vert_offset). Returns two empty arrays for degenerate paths.

    with_normals: also return an (V, 3) array of unit outward vertex normals.
    They come straight from the sweep frame (a ring vertex's normal IS its radial
    offset direction; the cap centres' is +/- the tangent), so they are exact and
    free -- the deposition viewer uses them for lit shading instead of running
    vtkPolyDataNormals over millions of triangles.
    """
    pts = np.asarray(points, dtype=np.float64)
    keep = _dedupe_idx(pts)
    points = pts[keep]
    if len(points) < 2:
        empty = (np.zeros((0, 3), dtype=np.float64),
                 np.zeros((0, 3), dtype=np.int64))
        return empty + (np.zeros((0, 3), dtype=np.float64),) if with_normals \
            else empty

    # `radius` is a scalar (uniform tube, the default) OR a per-vertex array -- a
    # fibre whose diameter VARIES along its length (the physics dynamic-diameter
    # feature). A per-vertex array is deduped in lockstep with the points and
    # floored to a tiny positive value so the offs/radius normal stays finite.
    rad = np.asarray(radius, dtype=np.float64) * TUBE_RADIUS_MULT
    if rad.ndim:
        rad_b = np.maximum(rad[keep], 1e-9)[:, None, None]   # (n_pts,1,1) per ring
    else:
        rad_b = rad                                          # scalar broadcast

    tang = _tangents(points)
    normals, binormals = _rmf_frames(points, tang)

    n_pts = len(points)
    # angles for the cross-section polygon
    angles = np.linspace(0.0, 2.0 * math.pi, faces, endpoint=False)
    cos_a = np.cos(angles)
    sin_a = np.sin(angles)

    # ring vertices: ring r, vertex k -> local index r*faces + k
    # offset[r,k] = radius * (cos_a[k]*normal[r] + sin_a[k]*binormal[r])
    offs = rad_b * (cos_a[None, :, None] * normals[:, None, :]
                    + sin_a[None, :, None] * binormals[:, None, :])
    ring = points[:, None, :] + offs                 # (n_pts, faces, 3)
    ring_verts = ring.reshape(-1, 3)                 # row r*faces + k

    base = vert_offset

    # --- side quads (two triangles each) ----------------------------------- #
    r = np.arange(n_pts - 1)
    k = np.arange(faces)
    R, K = np.meshgrid(r, k, indexing="ij")          # (n_pts-1, faces)
    a = R * faces + K
    b = R * faces + (K + 1) % faces
    c = (R + 1) * faces + (K + 1) % faces
    d = (R + 1) * faces + K
    side1 = np.stack([a, b, c], axis=-1).reshape(-1, 3)
    side2 = np.stack([a, c, d], axis=-1).reshape(-1, 3)

    # --- end caps (fan triangulation around a center vertex) --------------- #
    start_center = n_pts * faces                     # local index of points[0]
    end_center = n_pts * faces + 1                   # local index of points[-1]
    ks = np.arange(faces)
    k0a = ks                                         # ring_idx(0, k)
    k0b = (ks + 1) % faces                           # ring_idx(0, k+1)
    cap_start = np.stack(
        [np.full(faces, start_center), k0b, k0a], axis=-1)  # wind outward
    last = n_pts - 1
    kla = last * faces + ks
    klb = last * faces + (ks + 1) % faces
    cap_end = np.stack([np.full(faces, end_center), kla, klb], axis=-1)

    verts = np.concatenate([ring_verts, points[0:1], points[-1:]], axis=0)
    tris = np.concatenate([side1, side2, cap_start, cap_end], axis=0) + base
    if with_normals:
        # a ring vertex's outward normal is its (unit) radial offset direction;
        # normal/binormal are orthonormal so offs/radius is already unit length
        ring_n = (offs / rad_b).reshape(-1, 3)
        cap_n = np.stack([-tang[0], tang[-1]])      # start/end cap centres
        normals = np.concatenate([ring_n, cap_n], axis=0)
        return verts, tris, normals
    return verts, tris


# --------------------------------------------------------------------------- #
# Mesh-sampling decimation (centreline -> tube rings)
# --------------------------------------------------------------------------- #
# The sag solver's OUTPUT sampling is chosen for the SOLVE, far finer than the MESH
# needs: on a 30x30 scaffold it turns 47k commanded points into ~690k sagged ones,
# and tube_mesh spends a whole `faces`-gon ring on each -- 16.5M triangles, an
# 824 MB STL. The tolerance below is not a taste call: the tube is already a
# `faces`-gon, so its surface departs from the true round fibre by the polygon's
# sagitta, facet_error = r * (1 - cos(pi / faces)) (0.27 um at r=8um, 12 faces),
# and a centreline sample droppable while staying INSIDE that error adds no error
# the mesh did not already carry.

# WHAT IS AND IS NOT PROVEN. The guarantee is on the CENTRELINE: every dropped
# point lies within `tol` of the kept polyline, bend penalty included (see
# decimate_keep). That is measured, not assumed. The SURFACE is a different number,
# measured separately at mean 0.095 um and a MAX of 1.738 um on one 10x10 physics
# export. That max is NOT the tolerance's doing -- it is identical
# at an 8x tighter tolerance -- and it is unattributed: the three candidate causes
# measured (a tangent turn of at most 1.53 deg, the cross-section's rotational
# phase, a metric artifact) are all too small. Do not restate 1.738 um as a bound.

# This runs on the way into tube_mesh ONLY. The .depo cache is written before it
# (the player, deviation view and analysis engine keep the full sagged centreline),
# the sag solve is untouched, and `--mesh-tolerance 0` restores the previous
# byte-identical STL.
_DECIMATE_MAX_PASSES = 64   # bounds the worst case; convergence is ~log2(n)


def facet_error(radius, faces):
    """Half-width of the gap between a `faces`-gon tube of the given radius and
    the true cylinder it approximates (mm). The mesh's own existing error."""
    n = max(3, int(faces))
    return float(radius) * (1.0 - math.cos(math.pi / n))


def _span_max_dist(pts, lo, hi):
    """For each (lo[k], hi[k]) index pair, the largest distance from the ORIGINAL
    points strictly between them to the chord pts[lo[k]] -> pts[hi[k]] (mm).

    Fully vectorised: every interior point of every span is flattened into one
    array, the perpendicular distances are computed in one shot, and the per-span
    maxima come back via np.maximum.reduceat (the group ids are sorted by
    construction, which is what makes reduceat legal and O(m))."""
    lo = np.asarray(lo, dtype=np.int64)
    hi = np.asarray(hi, dtype=np.int64)
    out = np.zeros(len(lo), dtype=np.float64)
    counts = hi - lo - 1
    live = counts > 0
    if not live.any():
        return out
    lo_l, hi_l, cnt = lo[live], hi[live], counts[live]
    ends = np.cumsum(cnt)
    starts = ends - cnt
    m = int(ends[-1])
    # interior original indices, span by span
    ii = np.repeat(lo_l + 1, cnt) + (np.arange(m, dtype=np.int64)
                                     - np.repeat(starts, cnt))
    a = pts[np.repeat(lo_l, cnt)]
    ab = pts[np.repeat(hi_l, cnt)] - a
    seg = pts[ii] - a
    l2 = np.einsum("ij,ij->i", ab, ab)
    safe = l2 > 1e-24
    t = np.zeros(m, dtype=np.float64)
    np.divide(np.einsum("ij,ij->i", seg, ab), l2, out=t, where=safe)
    np.clip(t, 0.0, 1.0, out=t)
    perp = seg - t[:, None] * ab
    d = np.sqrt(np.einsum("ij,ij->i", perp, perp))
    out[live] = np.maximum.reduceat(d, starts)
    return out


def _span_max_turn(seg_unit, pts, lo, hi):
    """For each (lo[k], hi[k]) pair, the largest angle (radians) between the
    chord pts[lo]->pts[hi] and any ORIGINAL segment direction inside it.

    `seg_unit` is the whole path's unit segment directions, computed once.
    Vectorised the same way as _span_max_dist: the largest angle is the smallest
    cosine, so np.minimum.reduceat over sorted groups gives it in one pass."""
    lo = np.asarray(lo, dtype=np.int64)
    hi = np.asarray(hi, dtype=np.int64)
    out = np.zeros(len(lo), dtype=np.float64)
    counts = hi - lo                       # segments lo .. hi-1
    live = counts > 0
    if not live.any():
        return out
    lo_l, hi_l, cnt = lo[live], hi[live], counts[live]
    ends = np.cumsum(cnt)
    starts = ends - cnt
    m = int(ends[-1])
    ii = np.repeat(lo_l, cnt) + (np.arange(m, dtype=np.int64)
                                 - np.repeat(starts, cnt))
    chord = pts[hi_l] - pts[lo_l]
    cl = np.sqrt(np.einsum("ij,ij->i", chord, chord))
    cl[cl <= 1e-24] = 1.0
    chord_u = np.repeat(chord / cl[:, None], cnt, axis=0)
    cos = np.einsum("ij,ij->i", seg_unit[ii], chord_u)
    out[live] = np.arccos(np.clip(np.minimum.reduceat(cos, starts), -1.0, 1.0))
    return out


def _span_max_rdev(rad, cum, lo, hi):
    """For each (lo[k], hi[k]) pair, the largest gap between the ORIGINAL per-point
    radius strictly inside it and the radius the tube would draw there once the
    span is merged -- linear in arc length between rad[lo] and rad[hi], which is
    how a tube lofted between two rings sizes its wall.

    `cum` is the original path's cumulative arc length. Vectorised the same way
    as _span_max_dist."""
    lo = np.asarray(lo, dtype=np.int64)
    hi = np.asarray(hi, dtype=np.int64)
    out = np.zeros(len(lo), dtype=np.float64)
    counts = hi - lo - 1
    live = counts > 0
    if not live.any():
        return out
    lo_l, hi_l, cnt = lo[live], hi[live], counts[live]
    ends = np.cumsum(cnt)
    starts = ends - cnt
    m = int(ends[-1])
    ii = np.repeat(lo_l + 1, cnt) + (np.arange(m, dtype=np.int64)
                                     - np.repeat(starts, cnt))
    a = np.repeat(lo_l, cnt)
    b = np.repeat(hi_l, cnt)
    span = cum[b] - cum[a]
    f = np.zeros(m, dtype=np.float64)
    np.divide(cum[ii] - cum[a], span, out=f, where=span > 1e-24)
    dev = np.abs(rad[ii] - (rad[a] + f * (rad[b] - rad[a])))
    out[live] = np.maximum.reduceat(dev, starts)
    return out


def decimate_keep(points, tol, radius=0.0):
    """Indices to keep so that every dropped point lies within `tol` mm of the
    kept polyline AND the tube swept along it moves by at most `tol`. Returns
    them in increasing order; endpoints always survive.

    Two tests, not one, and the second was added because the first is not enough:
    the centreline bound alone was measured to let the SURFACE move up to 2.07 um,
    7.6x the 0.2726 um it promised, with every worst vertex on a turnaround loop. A
    tube of radius r swept along a path whose tangent turns by theta away from the
    chord has its wall displaced by about r*theta on top of whatever the centreline
    moved, so a merge is accepted only when

        centreline deviation  +  r * theta  <=  tol.

    On a straight run theta is 0 and nothing changes -- which is where nearly all
    the reduction comes from -- while a tight bend (a turnaround loop, a crossover
    crest) keeps its rings. Turnaround geometry is a modelled physics output here.

    A third term joins them when `radius` is a per-point array that varies (a
    dynamic diameter): the tube is re-sized at the KEPT rings only, so a merge
    must not move the radius the wall is drawn at by more than `tol` either.
    Without it a straight fibre whose diameter halves along it kept its two ends
    and came out a uniform-looking taper between them. A constant radius skips
    the term, so every uniform-diameter print decimates as before.

    Not Douglas-Peucker: RDP is top-down and recursive, and on a 70k-point fibre
    costs more than the mesh it saves. This is bottom-up and vectorised -- each pass
    proposes dropping a pairwise non-adjacent set of currently-kept points, checks
    each proposal against the ORIGINAL points in the span it would merge, and drops
    the ones inside `tol`; one pass is O(n) numpy work and there are ~log2 passes.
    Validating against the ORIGINAL points, not the previous pass's survivors, is
    what stops error accumulating across passes."""
    pts = np.asarray(points, dtype=np.float64)
    n = len(pts)
    if n < 3 or tol <= 0.0:
        return np.arange(n, dtype=np.int64)

    # unit segment directions of the ORIGINAL path, once (the turn test measures
    # against these, never against the surviving polyline, so the bend budget
    # cannot be spent twice)
    seg = pts[1:] - pts[:-1]
    seg_len = np.sqrt(np.einsum("ij,ij->i", seg, seg))
    seg_len[seg_len <= 1e-24] = 1.0
    seg_unit = seg / seg_len[:, None]
    r = float(radius) if np.ndim(radius) == 0 else float(np.max(radius))
    rad = cum = None
    if np.ndim(radius) and len(radius) == n:
        rad = np.asarray(radius, dtype=np.float64)
        if np.ptp(rad) > 0.0:
            # true lengths: `seg_len` had its zero-length segments set to 1
            cum = np.concatenate(
                [[0.0], np.cumsum(np.sqrt(np.einsum("ij,ij->i", seg, seg)))])
        else:
            rad = None

    keep = np.arange(n, dtype=np.int64)
    for p in range(_DECIMATE_MAX_PASSES):
        if len(keep) < 3:
            break
        d = _span_max_dist(pts, keep[:-2], keep[2:])
        if r > 0.0:
            d = d + r * _span_max_turn(seg_unit, pts, keep[:-2], keep[2:])
        if rad is not None:
            d = d + _span_max_rdev(rad, cum, keep[:-2], keep[2:])
        cand = d <= tol                                  # interior t -> keep[t+1]
        if not cand.any():
            break
        # A pairwise non-adjacent subset, so no two merges share an endpoint and
        # each proposal's validation stays true after the pass. Alternating the
        # parity keeps a long candidate run from being thinned from one end only.
        sel = cand.copy()
        sel[(p % 2)::2] = False
        if not sel.any():                                # run of length 1 at the
            sel = cand.copy()                            # wrong parity -- take it
            sel[1 - (p % 2)::2] = False
            if not sel.any():
                break
        drop = np.zeros(len(keep), dtype=bool)
        drop[1:-1] = sel
        keep = keep[~drop]
    return keep


def max_deviation(original, kept_idx):
    """Largest distance (mm) from any dropped point to the polyline the kept
    points form. THIS is the fidelity number -- the decimator's tolerance is a
    promise, this is the measurement of whether it was kept."""
    pts = np.asarray(original, dtype=np.float64)
    idx = np.asarray(kept_idx, dtype=np.int64)
    if len(idx) >= len(pts) or len(idx) < 2:
        return 0.0
    return float(_span_max_dist(pts, idx[:-1], idx[1:]).max())


def decimate_for_mesh(paths, radii, faces, mult=1.0, times=None,
                      return_keep=False):
    """Thin each fibre centreline down to what a `faces`-gon tube can express.

    radii: per-path scalar radius, or a per-point array (dynamic diameter). The
    tolerance uses the SMALLEST radius on the path, so a fibre that thins where
    the head ran fast is decimated to the tolerance its thinnest section earns.

    Returns (paths, times, stats) with times decimated in lockstep (None in,
    None out). stats is (pts_before, pts_after, worst_deviation_mm).

    return_keep appends the per-fibre KEPT-INDEX arrays to the return. They are
    what write_depo_decimation stores so the player can reuse this solve instead
    of repeating it (4.87 s on a 30x30x10)."""
    if mult <= 0.0 or not paths:
        n = sum(len(p) for p in paths)
        empty = ([np.arange(len(p), dtype=np.int64) for p in paths]
                 if return_keep else None)
        out = (paths, times, (n, n, 0.0))
        return out + (empty,) if return_keep else out
    out_p, out_t = [], ([] if times is not None else None)
    out_k = [] if return_keep else None
    before = after = 0
    worst = 0.0
    for i, p in enumerate(paths):
        pts = np.asarray(p, dtype=np.float64)
        before += len(pts)
        r = radii[i] if radii is not None else 0.0
        r_min = float(np.min(r)) if np.ndim(r) else float(r)
        tol = mult * facet_error(r_min, faces)
        # the bend penalty uses the LARGEST radius on the fibre (the thickest
        # section swings furthest on a turn), while the tolerance uses the
        # smallest -- both conservative, in opposite directions
        idx = decimate_keep(pts, tol, radius=r)
        worst = max(worst, max_deviation(pts, idx))
        after += len(idx)
        out_p.append(pts[idx])
        if out_t is not None:
            out_t.append(np.asarray(times[i], dtype=np.float64)[idx])
        if out_k is not None:
            out_k.append(np.asarray(idx, dtype=np.int64))
    out = (out_p, out_t, (before, after, worst))
    return out + (out_k,) if return_keep else out


def box_mesh(xmin, xmax, ymin, ymax, zmin, zmax, vert_offset):
    """Axis-aligned rectangular box (8 vertices, 12 triangles).

    Used to render a rectangular build plate the scaffold sits on (and any other
    slab). Triangles are wound counter-clockwise as seen from OUTSIDE, so the
    facet normals that write_binary_stl() derives from the winding point outward."""
    verts = np.array([
        [xmin, ymin, zmin],   # 0
        [xmax, ymin, zmin],   # 1
        [xmax, ymax, zmin],   # 2
        [xmin, ymax, zmin],   # 3
        [xmin, ymin, zmax],   # 4
        [xmax, ymin, zmax],   # 5
        [xmax, ymax, zmax],   # 6
        [xmin, ymax, zmax],   # 7
    ], dtype=np.float64)
    tris = np.array([
        [0, 2, 1], [0, 3, 2],   # bottom (-Z)
        [4, 5, 6], [4, 6, 7],   # top    (+Z)
        [0, 1, 5], [0, 5, 4],   # front  (-Y)
        [2, 3, 7], [2, 7, 6],   # back   (+Y)
        [1, 2, 6], [1, 6, 5],   # right  (+X)
        [3, 0, 4], [3, 4, 7],   # left   (-X)
    ], dtype=np.int64) + vert_offset
    return verts, tris


def disk_prism_mesh(cx, cy, radius, zmin, zmax, vert_offset, segments=64):
    """Regular-prism approximation of a flat cylinder -- a round build plate /
    wafer: a `segments`-gon disk swept from zmin to zmax, centred at (cx, cy).
    Triangles wind counter-clockwise as seen from OUTSIDE, so the facet normals
    write_binary_stl() derives from the winding point outward (matches box_mesh)."""
    n = max(3, int(segments))
    ang = np.arange(n) * (2.0 * np.pi / n)
    rim_x = cx + radius * np.cos(ang)
    rim_y = cy + radius * np.sin(ang)
    bottom = np.column_stack([rim_x, rim_y, np.full(n, zmin)])   # 0 .. n-1
    top = np.column_stack([rim_x, rim_y, np.full(n, zmax)])      # n .. 2n-1
    centres = np.array([[cx, cy, zmin], [cx, cy, zmax]])         # 2n, 2n+1
    verts = np.concatenate([bottom, top, centres], axis=0)
    ci_b, ci_t = 2 * n, 2 * n + 1
    tris = []
    for i in range(n):
        j = (i + 1) % n
        tris.append([i, j, n + j])          # side quad, outward (+radial)
        tris.append([i, n + j, n + i])
        tris.append([ci_b, j, i])           # bottom cap (-Z, outward)
        tris.append([ci_t, n + i, n + j])   # top cap (+Z, outward)
    tris = np.array(tris, dtype=np.int64) + vert_offset
    return verts, tris


# --------------------------------------------------------------------------- #
# Fiber sagging
# --------------------------------------------------------------------------- #
# Purely geometric, no FEA. Layers are ranked by commanded Z and processed
# bottom-up. A fibre is held up where it CROSSES OVER the layer below (exact 2D
# segment intersection, resolution independent) and wherever its own droop would
# sink onto a fibre it never crosses in projection, or onto one more than a layer
# down -- those extra contacts come from an obstacle-relaxation pass that pins a
# support on any obstacle the droop hits and re-solves until the fibre clears
# everything below it. A fibre also rests on ITS OWN earlier passes wherever the
# path self-crosses in XY at the same height, resolved in PRINT ORDER, which is
# what lets a single-layer print sag.

# Between two supports the centreline sags as a downward parabola whose midspan
# depth grows with the SQUARE of the span (self-weight bridging:
# depth = SAG_COEFF * span**2 / diameter, times the user's sag_multiplier), so the
# sag scales with pore size on its own. The droop is NOT artificially capped: it is
# bounded only by the fibre coming to rest on what is below it, or settling onto
# the bed where there is nothing under the span. The three constants below are the
# calibration; the only user control is the dimensionless `sag_multiplier`.

#   SAG_COEFF      base droop strength; /diameter keeps the sag-to-span ratio
#                  scale-free. 0.01 recalibrates the droop so multiplier 1.0
#                  matches what the old 0.02 coefficient gave at 0.5.
#   SAG_STEP_D     droop sampling step, in diameters, at multiplier 1. The sag
#                  parabola has span-INDEPENDENT curvature (d2z/ds2 = 8*SAG_COEFF
#                  *mult/diameter), so a fixed arc-length step resolves it equally
#                  at any pore size. Scaled finer as the multiplier grows.
#   CREST_RADIUS_D crest-rounding radius, in diameters, for the bend a stiff
#                  fibre makes as it drapes over the one below (_round_crests).
SAG_COEFF = 0.01
SAG_STEP_D = 3.0
CREST_RADIUS_D = 1.0

# Exponents of the droop law. depth = SAG_COEFF * mult * span**SAG_SPAN_EXP
# / diameter**SAG_DIAM_EXP. Named constants so the law can be re-fitted to real
# sag measurements by changing these four numbers alone -- nothing else in the
# solver assumes a particular power. SAG_STEP_D's constant-curvature argument
# holds only while SAG_SPAN_EXP == 2; if a fit changes it, revisit the sampling
# step in apply_sag.
SAG_SPAN_EXP = 2.0
SAG_DIAM_EXP = 1.0

# Periodic sag reset (see `_SagState.maybe_reset`). Real prints show the deposited
# fibres lying straight again every so many layers, with droop re-accumulating from
# there; the MECHANISM is not known and none is claimed -- this is a
# phenomenological PERIOD, the same class of knob as `sag_multiplier`. Droop
# SATURATES rather than growing without bound, so a reset reads as a repeating band
# (flat, ramp, saturate), not a step.

# 0 = never reset, and that is the default on EVERY surface -- this keyword, the
# converter/scaffold/player CLIs and the GUI tickbox -- so an un-asked-for run is
# byte-for-byte the old solve. The shared default matters: the period is part of
# the `.depo` cache key, so a surface defaulting differently would reject a
# default-built cache and the player would silently re-derive the march
# (live-vs-locked drift). The GUI prefills 20 when it IS ticked.
_DEFAULT_SAG_RESET_LAYERS = 0

# Output-sampling guards on each span's droop. A span is sampled at the fixed
# SAG_STEP_D arc step ONLY when it actually droops: below SAG_TOL_D diameters of
# effective (clamped) sagitta it lies flat to within tolerance and gets no
# interior points. That skips the catastrophic oversampling of flat spans (a
# bottom-layer pass on the bed would otherwise get tens of thousands of collinear points)
# while leaving sagging spans at their tested density. SAG_MAX_SPAN_SAMPLES caps
# the per-span count as a safety net.
SAG_TOL_D = 0.05
SAG_MAX_SPAN_SAMPLES = 64

# Crest sampling -- how far either side of a support the drape is sampled, and how
# finely. Over a support the centreline is `_round_crests`' parabola of radius
# CREST_RADIUS_D * diameter, running from the support height down to wherever the
# free droop or the bed takes over. (The round cross-section it rests on has the
# SAME radius at its apex but falls away faster, so `maximum` picks the parabola
# everywhere but the apex: the crest you see is the rounding, not the cap.) It is
# the sharpest feature on the whole centreline -- the free droop between supports
# is two orders of magnitude flatter -- which is why it needs its own rule.

# EQUAL ARC, not equal angle: a parabola has constant curvature in arc, so a
# uniform step gives every chord the same sagitta, step**2 / (8 * round_r). And
# emitted per SUPPORT, not per drooping SPAN -- a span skipped as flat used to
# leave its supports with a crest sampled on ONE SIDE ONLY (the lopsided
# crossover).

# 0.6 diameters is FREE, which is why it is the default rather than something
# tighter. Measured on a 30x30x10 at 0.5 mm stepover, against the three
# fixed-offset samples it replaced:
#   step   crest error   solve    centreline pts   pts after mesh decimation
#   old      8.6-10.8 um  10.9 s     561 887              376 579
#   0.4         0.31 um   12.6 s     633 053              408 729
#   0.6         0.55 um   11.4 s     561 875              376 206
#   0.8         1.25 um   10.4 s     490 703              308 570
# At 0.6 the whole fix costs TWELVE fewer points than the rule it replaces: the old
# offsets crowded the apex, where the drape is flattest, and left the steep part on
# a chord. 0.4 buys parity with the 12-gon tube's own 0.27 um faceting at +13 %
# points; 0.8 is 13 % cheaper.
CREST_STEP_D = 0.6
CREST_MAX_SAMPLES = 10

# How far the crest sampling may reach from a support, in diameters. A RESOLUTION
# bound, not a shape one -- the centreline is still `maximum(rounded parabola,
# floor)` at every point emitted, and nothing is clipped, flattened or held. It
# only decides where the crest's dense samples stop and the span's take over.
#
# pi/2 * CREST_RADIUS_D because the rounding is an ARC of radius round_r: it turns
# from horizontal at the apex to vertical and no further, so the stretch carrying
# curvature 1/round_r is at most a quarter circle. Past that the fibre is in free
# droop, twelve times gentler at the defaults, which the SAG_STEP_D grid resolves.
# Its cost was measured against a reference with this cap LIFTED, so the cap's
# own cost is inside the measurement rather than cancelling out of it.
CREST_MAX_REACH_D = 0.5 * math.pi * CREST_RADIUS_D

# Ceiling on `_round_crests`' derived shift count -- a runaway guard for a
# stretch sampled far finer than anything here produces, not a shape knob. The
# derived count is 6-12 on ordinary scaffolds.
_ROUND_CRESTS_MAX_SHIFTS = 96


def crest_extent(span, free, eff, diameter, dh=0.0):
    """How far the crest at a support reaches into an adjacent span (mm).

    Three bounds, whichever is TIGHTEST, because any of them can end the crest:

      * the rounding parabola meets the free droop. Leaving the support the droop
        falls at about (4*free + |dh|) / span; a parabola of radius R matches that
        slope at arc 2*R*slope, and past there the droop is the lower curve.
      * the rounding parabola meets the FLOOR. It falls `drop` over
        sqrt(2*R*drop), and `eff` is exactly that drop -- the span's droop already
        clamped to what the structure below leaves available.
      * CREST_MAX_REACH_D -- the quarter circle. The parabola bound above is the
        arc over which the parabola is the UPPER curve, which runs away as
        2*R*slope whenever the neighbouring span droops hard. Uncapped, the crest
        sampling ran 4+ diameters out into free fall -- the span sampling's
        territory, not the crest's.

    NONE of this clamps the fibre; these bounds only decide WHERE the crest's
    dense samples stop. Floored at one diameter so a span too flat to droop still
    gets its round cross-section sampled, and capped at half the span by the
    caller so two neighbouring crests meet instead of overrunning each other.

    Scalar in, float out; arrays in, array out -- `sag_approx` has the whole span
    table in hand at once and must not have to loop to use this."""
    R = CREST_RADIUS_D * float(diameter)
    span = np.asarray(span, dtype=np.float64)
    live = span > 0.0
    slope = ((4.0 * np.asarray(free, dtype=np.float64)
              + np.abs(np.asarray(dh, dtype=np.float64)))
             / np.where(live, span, 1.0))
    w = np.minimum(np.minimum(2.0 * R * slope,
                              CREST_MAX_REACH_D * float(diameter)),
                   np.sqrt(2.0 * R * np.maximum(
                       0.0, np.asarray(eff, dtype=np.float64))))
    w = np.maximum(float(diameter), np.where(live, w, float(diameter)))
    return w if w.ndim else float(w)


def sag_depth(span, diameter, sag_multiplier=1.0):
    """Free midspan droop of a fiber bridging an unsupported `span` (mm).

    The self-weight bridging law, isolated in one place so a calibration from
    real sag data can replace it without touching the solver:

        depth = SAG_COEFF * sag_multiplier
                * span**SAG_SPAN_EXP / diameter**SAG_DIAM_EXP

    This is the FREE droop (before the fiber comes to rest on whatever is
    beneath it); the solver clamps it onto the structure below afterwards.
    `span` may be a scalar or a numpy array; returns the same shape."""
    return (SAG_COEFF * sag_multiplier
            * np.power(span, SAG_SPAN_EXP) / diameter ** SAG_DIAM_EXP)


# Optional droop-law seam, identity-by-default like `_FIBER_TRANSFORM`. None ->
# the built-in GEOMETRIC `sag_depth` (byte-identical). The physics layer installs
# a grounded tensioned-thread law via `set_sag_law`; the batch solver AND the live
# `_SagState` march both read it through `_sag_law`, so a SINGLE install reshapes
# the exported scaffold and the real-time re-lay alike. No physics import here --
# the seam is a plain callable.
_SAG_LAW = None


def set_sag_law(law):
    """Install the droop law the sag solver uses (None -> geometric `sag_depth`).
    Mirror of `set_fiber_transform`: a module global so it threads through `main`,
    `apply_sag`, and the live `_SagState` march without per-call plumbing.
    The physics callers (`mew_export`, `gcode_player`) set it; the plain CLI does not."""
    global _SAG_LAW
    _SAG_LAW = law


def _sag_law(span, diameter, sag_multiplier=1.0):
    """The droop law currently in force: the installed physical law if any, else
    the geometric `sag_depth`. Same (span, diameter, sag_multiplier) contract;
    `span` may be scalar or array. Every droop in the solver goes through here so
    a single install switches the whole pipeline (batch export + live march)."""
    if _SAG_LAW is None:
        return sag_depth(span, diameter, sag_multiplier)
    return _SAG_LAW(span, diameter, sag_multiplier)


# Sag OUTPUT-sampling resolution. It never changes the droop law, the contacts or
# the support heights, only how densely the solved centreline is sampled. 1.0 is
# the shipped density (byte-identical); a factor q divides the span step
# (SAG_STEP_D) and the crest step (CREST_STEP_D) by q and lifts the matching
# per-span / per-crest / crest-rounding caps by q, so the caps never become what
# stops the refinement.
# It is NOT cosmetic at 1.0: `_round_crests` rounds each crest over the samples,
# so at the shipped density the shoulder beside a support sits up to ~0.9 d low
# and a flank can carry a visible notch. Measured on the default sag ladder
# (fibre B, against the 10x solve): 1x 0.905 d max / 0.078 d mean,
# 3x 0.109 d / 0.015 d. Cost is about q x the solve time.
# Mirror of `set_sag_law`: a module global, so ONE call at a CLI's start reaches
# `apply_sag`, the live `_SagState` march, `sag_approx` and the `.depo` cache key
# without per-call plumbing -- and no call site can forget to pass it.
_SAG_RESOLUTION = 1.0


def set_sag_resolution(factor):
    """Set the sag output-sampling resolution factor (>= 1; 1 = shipped density,
    byte-identical). The CLIs (`main`, `gcode_player`) set it from
    `--sag-resolution`; nothing else should."""
    global _SAG_RESOLUTION
    f = float(factor)
    if not (math.isfinite(f) and f >= 1.0):
        raise ValueError("sag resolution must be a number >= 1, got %r" % factor)
    _SAG_RESOLUTION = f


def sag_resolution():
    """The sag output-sampling resolution factor in force (see `set_sag_resolution`)."""
    return _SAG_RESOLUTION


# Optional fibre-fusion seam, identity-by-default like `_SAG_LAW`. None -> NO
# fusion: a fibre rests a FULL diameter ON TOP of the structure below (the
# geometric stack, byte-identical). With a law installed (--physical-fusion) the
# solver sinks the rest height by `_fusion_law(diameter)` at fibre-on-fibre
# contacts ONLY, never on the suspended spans -- so stacked crossover peaks come
# down without touching the pore sag. Read by BOTH the batch `apply_sag` and the
# live `_SagState` march. No physics import here.

# The law takes a `wall` flag because a contact is not one thing: two fibres
# CROSSING touch at a point, while a COLLINEAR pair (a stacked wall, or layer N+2
# drooping onto the parallel layer N) touch along a line, and a rig may calibrate
# the two differently. `_FUSION_BAND` carries the (sin_lo, sin_hi) crossing-angle
# band to blend between them; None -> one magnitude, and the solver then computes
# no per-contact angle at all (byte-identical).
_FUSION_LAW = None
_FUSION_BAND = None


def set_fusion_law(law, wall_band=None):
    """Install the fibre-fusion law the sag solver uses -- None is no fusion, the
    geometric full-diameter stack. Mirror of `set_sag_law`: a module global, so it
    threads through `main`, `apply_sag` and the live `_SagState` march. `law` maps
    (fibre diameter (mm), wall=False) -> a centre-sink delta (mm); `wall=True`
    asks for the COLLINEAR-contact magnitude.

    `wall_band` is the (sin_lo, sin_hi) crossing-angle band over which the solver
    blends wall into junction. None (the default) means ONE magnitude for every
    contact, and the solver then never computes a contact angle at all:
    byte-identical."""
    global _FUSION_LAW, _FUSION_BAND
    _FUSION_LAW = law
    _FUSION_BAND = wall_band


def _fusion_law(diameter, wall=False):
    """The fusion centre-sink (mm) at fibre-on-fibre contacts: the installed
    compaction law if any, else 0.0. `wall=False` (the default) is the CROSSING
    (junction) magnitude, `wall=True` the COLLINEAR one. Every contact-height
    lowering in the solver goes through here, so a single install switches the
    whole pipeline."""
    if _FUSION_LAW is None:
        return 0.0
    return float(_FUSION_LAW(diameter, wall))


def _wall_weight(sin_theta, band):
    """Blend weight in [0, 1] for the WALL sink at contacts whose crossing angle has
    |sin| = `sin_theta` (0 = collinear, 1 = square crossing): 1 at/below the band's
    sin_lo (pure wall), 0 at/above sin_hi (pure junction), smoothstep between.

    A BLEND, not a threshold, and that is load-bearing: sin_theta varies
    continuously along a curved fibre, so a hard switch would flip classification
    mid-fibre and leave a (junction - wall) sink STEP in a continuous centreline.
    Smoothstep has zero derivative at both ends, so the sink is C1 across the band.
    The band spans the empty gap between the measured populations (collinear
    contacts spike at 0-2 deg; the smallest crossing a standard pore shape makes
    is 36 deg)."""
    lo, hi = band
    s = np.asarray(sin_theta, dtype=np.float64)
    if hi <= lo:                        # degenerate band -> hard switch at lo
        return (s <= lo).astype(np.float64)
    t = np.clip((s - lo) / (hi - lo), 0.0, 1.0)
    return 1.0 - t * t * (3.0 - 2.0 * t)


def _sink_fused_contacts(heights, bed_center, sink, eps, protect=None):
    """Lower fibre-on-fibre rest heights by the fusion `sink` (mm), IN PLACE,
    leaving BED contacts (height at/near `bed_center`) and any `protect`-masked
    indices (the already-merged linking-loop entry) untouched. A contact counts as
    fibre-on-fibre when its rest height sits above the bed by more than `eps`.
    No-op when `sink <= 0`. Returns `heights`.

    Used at BOTH twin sites -- the support heights in resolve_supports and the
    rest-on-top clamp's floor -- so the sink the supports hang from is the one the
    clamp allows; lowering only one would be fought by the other.

    `sink` may be a SCALAR or a PER-POINT array parallel to `heights` (the
    angle-blended wall/junction sink -- see `_wall_weight`). `bed_center` likewise
    may be per-point, the bed-underside rest level when the bottom layer is laid
    at a per-point diameter, so a bed contact at `bed_center + r_point` is still
    recognised as the bed."""
    if heights is None or len(heights) == 0:
        return heights
    scalar_sink = np.ndim(sink) == 0
    if scalar_sink and sink <= 0.0:
        return heights
    bed = np.asarray(bed_center, dtype=np.float64)
    on_fiber = heights > bed + eps
    if protect is not None:
        on_fiber &= ~protect
    if not scalar_sink:
        on_fiber &= np.asarray(sink) > 0.0
    if on_fiber.any():
        floor_here = bed[on_fiber] if bed.ndim else bed
        sink_here = sink if scalar_sink else np.asarray(sink)[on_fiber]
        heights[on_fiber] = np.maximum(floor_here, heights[on_fiber] - sink_here)
    return heights


def _arc_lengths(pts, local=None):
    """Cumulative arc length along a chart polyline, and its total.

    `local` (None -> the raw chart norm, byte-for-byte) is
    `_local_chart`'s factory: with it each segment's length is measured through the
    metric at the segment's own MIDPOINT, so `cum` is a PHYSICAL arc length rather
    than a chart one.

    WHY THAT MATTERS, and it is not a refinement. This parametrisation is the
    solver's spine: support positions, the parabola's `t`, the resample grid and
    the crossing merge tolerance are all distances along it. A rigid turn about
    the collector's axis maps `(du, dv) -> (du, dv + rho'*du*dphi)`, which changes
    nothing physical and changes the chart length of every DIAGONAL segment --
    measured on the shipped five-axis dome, worst segment 60.5% and the whole
    fibre +21.5%, against 0.006% and -0.0000% for the physical length.
    With the contact index
    already measuring locally, this was the whole of what was left: the dome still
    moved 85.0 um under that turn.

    The h column is taken as-is. It is the collector-NORMAL offset and is already
    physical, so only the two chart columns need the map.
    """
    seg = pts[1:] - pts[:-1]
    if local is None:
        cum = np.concatenate([[0.0], np.cumsum(np.linalg.norm(seg, axis=1))])
        return cum, float(cum[-1])
    import mew_frame
    mid = 0.5 * (pts[1:] + pts[:-1])
    at_mid = local(mid[:, 2])
    ex, ey = mew_frame.local_offsets(at_mid(mid[:, 0], mid[:, 1]),
                                     seg[:, 0], seg[:, 1])
    cum = np.concatenate([[0.0], np.cumsum(
        np.sqrt(ex ** 2 + ey ** 2 + seg[:, 2] ** 2))])
    return cum, float(cum[-1])


def _tangent_at(base, cum, arcs, window=0.0):
    """Unit XY tangent (tx, ty) of polyline `base` at arc positions `arcs` -- the
    direction the fibre is running there.

    `window` (mm, 0 -> off) measures the direction as the CHORD across +/- window/2
    of arc instead of reading the one segment the position falls in. The fusion
    wall/junction split passes one fibre DIAMETER, and that is a physical
    statement: two fibres in contact share melt over a region about a fibre wide,
    so "the angle this contact crosses at" is only meaningful averaged over that
    width. Read off a raw segment, the angle -- and with it the sink -- can swing
    the full wall->junction range across a sub-micron step at a resampled
    turnaround corner, dropping a (phi_j - phi_w)*d CLIFF into a continuous
    centreline (measured: a 23deg->0deg tangent swing over 2.4um of arc left a
    7.6um step on a 20um fibre).

    Zero-length segments / a degenerate window give (0, 0). Only the fusion split
    needs this, so it is off every default path."""
    total = float(cum[-1]) if len(cum) else 0.0
    a = np.asarray(arcs, dtype=np.float64)
    if window > 0.0 and total > 0.0:
        half = 0.5 * min(window, total)
        lo = np.clip(a - half, 0.0, total)
        hi = np.clip(a + half, 0.0, total)
        dx = np.interp(hi, cum, base[:, 0]) - np.interp(lo, cum, base[:, 0])
        dy = np.interp(hi, cum, base[:, 1]) - np.interp(lo, cum, base[:, 1])
        L = np.hypot(dx, dy)
        good = L > 0.0
        tx = np.zeros(a.shape)
        ty = np.zeros(a.shape)
        tx[good] = dx[good] / L[good]
        ty[good] = dy[good] / L[good]
        return tx, ty
    seg = base[1:, :2] - base[:-1, :2]
    L = np.hypot(seg[:, 0], seg[:, 1])
    good = L > 0.0
    tx = np.zeros(len(seg))
    ty = np.zeros(len(seg))
    tx[good] = seg[good, 0] / L[good]
    ty[good] = seg[good, 1] / L[good]
    k = np.clip(np.searchsorted(cum, a, side="right") - 1, 0, len(seg) - 1)
    return tx[k], ty[k]


def _layer_ids(paths, layer_tol):
    """Cluster fibers into layers by their representative (median) Z."""
    reps = [float(np.median(p[:, 2])) for p in paths]
    centers = []
    for z in sorted(reps):
        if not centers or z - centers[-1] > layer_tol:
            centers.append(z)

    def id_of(z):
        return min(range(len(centers)), key=lambda i: abs(z - centers[i]))

    return [id_of(r) for r in reps], len(centers)


def _layer_ids_deposition(paths, layer_tol):
    """Cluster fibers into layers in DEPOSITION (g-code) order rather than by
    global height.

    `_layer_ids` sorts every fiber by Z, so a scaffold printed entirely AFTER
    another can still be interleaved with it whenever their commanded Z ranges
    overlap (the "sandwich"). Here a new layer instead begins whenever a fiber's
    median Z departs from the current layer's anchor Z by more than `layer_tol`,
    walking the fibers in the order they are laid -- so a later-deposited group at
    a lower Z becomes its OWN later layer and rests ON TOP of everything already
    laid, the physical continuous-jet behaviour.

    For any g-code emitted in ascending Z (every ordinary print) this is identical
    to `_layer_ids`: same members, same order."""
    ids = []
    n_layers = 0
    anchor = None
    for p in paths:
        z = float(np.median(p[:, 2]))
        if anchor is None or abs(z - anchor) > layer_tol:
            if anchor is not None:
                n_layers += 1
            anchor = z
        ids.append(n_layers)
    return ids, (n_layers + 1 if ids else 0)


# --------------------------------------------------------------------------- #
# Layer-spanning fibres
#
# Both `_layer_ids` and `_layer_ids_deposition` take ONE representative Z per fibre
# (its median), so a fibre walking through several layers is assigned whole to the
# layer nearest its middle. The whole stack then collapses into one plane: nothing
# has a layer below to rest on, every crossing becomes an in-layer
# self-intersection, and the solve blows up (measured on the 30x30/0.5/x10 default:
# 1 layer instead of 10, and the drape 3.2 s -> 47.9 s). The fix is granularity,
# not ordering: cut a fibre into Z-bands BEFORE clustering, solve the bands as the
# separate layers they physically are, then re-join each fibre's bands on output.

# WHEN THIS BITES. No scaffold this program generates has a layer-spanning fibre --
# across ten configurations every fibre's Z span is exactly 0.000 and
# `_split_layer_spanning` returns None, leaving the plain code path untouched. It
# bites on an IMPORTED file whose travels are spelled G1 (which our own "Emit
# travel (G0) moves as G1" export produces), on `--render-travel`, whose laid
# travels give each fibre a one-layer-height tail, and on genuinely continuous
# g-code -- a vase/spiral whose Z rises without discrete layers. The anchor test
# below is cumulative rather than per-step so a slow ramp bands correctly.
def _split_layer_spanning(paths, layer_tol, point_times=None, band_key=None):
    """Cut each polyline into Z-bands, each within `layer_tol` of its first point.

    Returns `(subs, owner, sub_times)`, or `(None, None, None)` when no path
    spans more than `layer_tol` -- the caller then runs its ORIGINAL code path
    verbatim, which is what keeps every existing output byte-identical.

    A new band starts when a point's Z departs from the band's anchor (its first
    point's Z) by more than `layer_tol`. Cumulative, not per-step, or a spiral
    climbing 1 um per point would never trip it. Every point of a band is
    within `layer_tol` of the band's first point, so a band can span up to
    2 * `layer_tol` and splitting the result again is NOT always a no-op;
    `apply_sag` stops its recursion with `_banded` instead.

    THE BOUNDARY IS A CUT, AND A CUT LOSES THE SEGMENT ACROSS IT. That is right
    for the thing the cut was written for -- the jet CLIMBING between layers,
    which is not a deposited span and must not be drooped as one -- and wrong for
    a strand that is laid across the print while it rises. The rule is therefore
    stated in XY: a boundary may only fall where the step across it moves less
    than one fibre width in XY (`2 * layer_tol`). Where the step is longer the
    band SWALLOWS it and the search walks on to the next candidate, so every
    point still belongs to exactly one band and nothing is duplicated; the band
    is then taller than `layer_tol`, which costs only layer granularity. What
    survives a boundary that IS taken is the implicit join when `_rejoin_split`
    concatenates the bands.

    MEASURED, the reported sag ladder: the
    once-per-layer lane traverse rises a layer while crossing the whole row, so
    its band boundary landed on a 3.68 mm step spanning four wall stacks. That
    step was in no band, so it was never solved, and the lane ran dead flat
    through all four walls at a vertical gap of exactly 0.000 d while every wall
    outside it read the 0.650 d of a fused rest.

    Termination: the "nothing to split" answer is now the CUT COUNT as well as
    the Z range -- a second pass over bands this one produced finds one band per
    path and returns None. A band the rule declines to cut would otherwise be
    offered back for ever; `_banded` already stops the recursion at one level,
    and this makes the function itself terminate on its own output.

    `band_key`, optional: a list parallel to `paths` whose Z column decides
    where a band starts, while `paths` itself is still what gets sliced and
    returned. None (default, every existing caller) decides from `paths`' own
    Z -- unchanged. A caller whose `paths` currency differs from its natural
    layer-height currency (`apply_sag_approx`'s world conversion for a
    non-developable `frame`, `gcode_to_fibers.py:5028-5031` -- WORLD Z is not
    deposition-layer height on a curved collector, so keying on it there cut
    one dome layer into hundreds of one-point bands) passes the
    pre-conversion, chart-space paths here instead. The XY step above is read
    off `paths`, the thing being sliced, because the question it asks is how far
    the jet travelled across the collector while it rose. On the curved caller
    that is a WORLD xy step, which understates travel across a steeply tilted
    patch; the residual is a boundary taken where a long deposited move happens
    to project short, and it is the same chart-versus-world question `band_key`
    itself exists to answer rather than anything this rule introduces."""
    keys = paths if band_key is None else band_key
    # Necessary condition for any cut at all, and the cheap one: a path whose Z
    # never leaves a `layer_tol` window cannot produce a boundary. Kept as an
    # early return so the per-point walk below is never paid for by the ordinary
    # scaffold, where no fibre spans (every Z range is exactly 0).
    spans = [bool(len(p)) and float(np.ptp(np.asarray(k)[:, 2])) > layer_tol
             for p, k in zip(paths, keys)]
    if not any(spans):
        return None, None, None
    # One fibre width. `layer_tol` is half a diameter at every caller, so this
    # is the diameter, in the only currency this function is given.
    step_tol = 2.0 * layer_tol
    all_cuts = []
    for p, k, spans_z in zip(paths, keys, spans):
        a = np.asarray(p, dtype=np.float64)
        ka = np.asarray(k, dtype=np.float64)
        if not spans_z:                      # one band, no walk needed
            all_cuts.append([0, len(a)])
            continue
        cuts, anchor = [0], None
        for idx in range(len(ka)):
            z = ka[idx, 2]
            if anchor is None or abs(z - anchor) > layer_tol:
                if anchor is None:
                    anchor = z
                    continue
                # The step that would straddle the boundary. A near-vertical one
                # is the climb the cut exists for; a long one is deposited fibre
                # and stays whole, so leave the anchor where it is and let the
                # next point be tested against it.
                if math.hypot(a[idx, 0] - a[idx - 1, 0],
                              a[idx, 1] - a[idx - 1, 1]) > step_tol:
                    continue
                cuts.append(idx)
                anchor = z
        cuts.append(len(a))
        all_cuts.append(cuts)
    if all(len(c) <= 2 for c in all_cuts):
        return None, None, None
    subs, owner, sub_times = [], [], ([] if point_times is not None else None)
    for i, (p, cuts) in enumerate(zip(paths, all_cuts)):
        a = np.asarray(p, dtype=np.float64)
        for s, e in zip(cuts[:-1], cuts[1:]):
            if e <= s:
                continue
            subs.append(a[s:e])
            owner.append(i)
            if sub_times is not None:
                sub_times.append(np.asarray(point_times[i],
                                            dtype=np.float64)[s:e])
    return subs, owner, sub_times


def _seat_lone_bands(subs, sub_out, owner, frame=None):
    """`sub_out` with each band the solver could not sag seated on the strand.

    A one-point band has no span, so neither solver can sag it: it comes back at
    its commanded height. `--render-travel` makes one at every layer change of a
    generated scaffold -- the Z-only corner G0 is spliced onto the next fibre, so
    that fibre's first vertex is one layer below the rest of it. Left alone, the
    vertex stands at its commanded height while the stack under it compresses: a
    vertical spike at each seam, one diameter taller every two layers.

    The jet never stops, so that vertex is the strand climbing onto the new
    layer. It is seated on the nearest solved material either side and keeps its
    own place along the collector.

    A RUN of one-point bands is the same defect, not a different one, and is
    not declined as "a climb steeper than one band per vertex".
    A routed five-axis travel climbs off the stack, crosses, and comes back
    down; that climb is steeper than `2 * layer_tol` per vertex, so EVERY vertex
    on the ramp is its own one-point band with one-point neighbours. Measured on
    a r10 dome, 6 x 4 mm, 10 layers at 60 deg layer rotation: eleven
    free-standing vertices per travel at up to 18.7 diameters above the
    collector, while the 51-vertex cruise band BETWEEN them drooped correctly to
    0.6-3.1. That is the thin triangle reported at the rim.

    SEATING IS ALONG THE COLLECTOR'S NORMAL, NOT DOWN Z. Rewriting the Z word is
    right on a plate, where Z IS height above the collector, and it is what this
    does there. On a dome it is close to useless: at the
    rim the outward normal is (0.974, -0.121, 0.321), so lowering a vertex a
    full diameter in Z sheds 0.32 of a diameter of height, and shedding the
    measured 14 would take 43. Both earlier attempts at this bug moved Z and
    both failed for that reason -- the worst spike went 12.83 -> 12.11 diameters
    and stayed. With a `frame` the vertex is placed at `Q + h * N` from
    `surface.height`, which is exact on any collector; without one the Z rule is
    kept verbatim, so every flat-plate output is byte-identical.

    THE ANCHOR BEFORE A LEADING RUN MAY BE THE PREVIOUS FIBRE. `merge_travels`
    splices the travel onto the FRONT of the fibre it leads into, so a ramp at
    the head of a fibre has no earlier band of its own -- but the jet never
    stops, and the band before it is the strand it is continuous with.

    A run of ONE band keeps the original preference exactly (the next band's
    first point, else the previous band's last). A longer run is spread between
    the two anchors by XY arclength, because a ramp's vertices are not evenly
    spaced -- 5 to 290 um apart on the measured dome -- and spacing by band
    index would bunch the descent.

    A seated height is an approximation, and this rule has always been one: it
    puts the vertex on the strand rather than on whatever lies directly beneath
    it. Re-solving these vertices against the support index would be exact, and
    a far larger change."""
    out = list(sub_out)
    n = len(subs)
    surface = None
    if frame is not None:
        try:
            surface = frame.surface()
        except Exception:  # noqa: BLE001 -- a frame with no surface (a plate)
            surface = None

    def lone(j):
        return len(subs[j]) == 1 and len(out[j]) == 1

    def anchor(j, step, end):
        """The solved endpoint of the nearest band from `j` in direction `step`
        that has a span. Crosses into the neighbouring fibre, because the jet
        does; None only when there is no such band that way at all."""
        k = j + step
        while 0 <= k < n:
            if len(subs[k]) >= 2 and len(out[k]):
                return np.asarray(out[k], dtype=np.float64)[end]
            k += step
        return None

    def height_of(P):
        return float(np.asarray(surface.height(
            np.asarray(P, dtype=np.float64).reshape(1, 3))[0])[0])

    j = 0
    while j < n:
        if not lone(j):
            j += 1
            continue
        e = j
        while e + 1 < n and owner[e + 1] == owner[j] and lone(e + 1):
            e += 1
        a, b = anchor(j, -1, -1), anchor(e, +1, 0)
        if a is None and b is None:
            j = e + 1
            continue
        pts = np.array([np.asarray(out[k], dtype=np.float64)[0]
                        for k in range(j, e + 1)])
        # how far along the run each vertex sits, by XY arclength through the
        # two anchors -- 0 at `a`, 1 at `b`
        chain = np.vstack([p[None, :] for p in (a, b) if p is not None])
        if a is not None and b is not None:
            chain = np.vstack([a[None, :], pts, b[None, :]])
            d = np.hypot(np.diff(chain[:, 0]), np.diff(chain[:, 1]))
            cum = np.concatenate([[0.0], np.cumsum(d)])
            t = (cum / cum[-1] if cum[-1] > 1e-12 else np.zeros(len(cum)))[1:-1]
        else:
            t = None
        if e == j:
            # UNCHANGED PREFERENCE: one lone band takes the NEXT band's first
            # point, falling back to the previous band's last.
            pick = b if b is not None else a
            if surface is None:
                q = np.array(out[j], dtype=np.float64)
                q[:, 2] = float(pick[2])
                out[j] = q
            else:
                ht = np.full(1, height_of(pick))
                out[j] = _place_at_height(surface, out[j], ht)
            j = e + 1
            continue
        if surface is None:
            if t is None:
                z = np.full(len(pts), float((a if b is None else b)[2]))
            else:
                z = float(a[2]) + (float(b[2]) - float(a[2])) * t
            for i, k in enumerate(range(j, e + 1)):
                q = np.array(out[k], dtype=np.float64)
                q[:, 2] = z[i]
                out[k] = q
        else:
            if t is None:
                ht = np.full(len(pts), height_of(a if b is None else b))
            else:
                ha, hb = height_of(a), height_of(b)
                ht = ha + (hb - ha) * t
            seated = _place_at_height(surface, pts, ht)
            for i, k in enumerate(range(j, e + 1)):
                q = np.array(out[k], dtype=np.float64)
                q[:] = seated[i]
                out[k] = q
        j = e + 1
    return out


def _place_at_height(surface, P, heights):
    """`P` moved along the collector's own normal to `heights` above it.

    `surface.height` answers `(h, Q, N, face)` -- the foot `Q` and the outward
    normal `N` at it -- so the seat is `Q + h * N` exactly, on a dome, a bowl,
    a cone or an imported solid alike. The XY of the result is NOT the input's:
    that is the point, since on a curved collector height is measured along the
    normal and not down Z."""
    A = np.asarray(P, dtype=np.float64).reshape(-1, 3)
    _h, Q, N, _face = surface.height(A)
    Q = np.asarray(Q, dtype=np.float64).reshape(-1, 3)
    N = np.asarray(N, dtype=np.float64).reshape(-1, 3)
    return Q + np.asarray(heights, dtype=np.float64).reshape(-1, 1) * N


def _rejoin_split(sub_out, owner, n_paths):
    """Concatenate each original path's bands back into one polyline, restoring
    the one-entry-per-input-path contract every caller (the .depo splits array,
    per-fibre viewer colouring, deposition times) depends on."""
    out = [None] * n_paths
    for j, o in enumerate(owner):
        piece = np.asarray(sub_out[j], dtype=np.float64)
        out[o] = piece if out[o] is None else np.concatenate([out[o], piece])
    return out


def _seg_hits_xy(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1):
    """Vectorised XY segment-segment intersection over parallel arrays of
    segment pairs (A and B given component-wise). Returns (hit, t, u): a bool
    mask of pairs that actually cross, and the clamped [0,1] intersection
    parameters along A and B. t/u are only meaningful where hit is True."""
    rx = ax1 - ax0
    ry = ay1 - ay0
    sx = bx1 - bx0
    sy = by1 - by0
    denom = rx * sy - ry * sx
    ok = np.abs(denom) >= 1e-15
    den = np.where(ok, denom, 1.0)
    qpx = bx0 - ax0
    qpy = by0 - ay0
    t = (qpx * sy - qpy * sx) / den
    u = (qpx * ry - qpy * rx) / den
    hit = (ok & (t >= -1e-9) & (t <= 1.0 + 1e-9)
           & (u >= -1e-9) & (u <= 1.0 + 1e-9))
    return hit, np.clip(t, 0.0, 1.0), np.clip(u, 0.0, 1.0)


_EMPTY_IDX = np.zeros(0, dtype=np.int64)   # shared empty index array

# the 3x3 cell-neighbourhood offsets every grid query expands to
_NEIGH_X = np.array([-1, -1, -1, 0, 0, 0, 1, 1, 1], dtype=np.int64)
_NEIGH_Y = np.array([-1, 0, 1, -1, 0, 1, -1, 0, 1], dtype=np.int64)


def _neigh_offsets(k):
    """(dx, dy) cell offsets of the (2k+1)x(2k+1) block centred on a query cell,
    row-major -- exactly `_NEIGH_X`/`_NEIGH_Y` for k == 1. A rest-height query whose
    contact reach exceeds what a 3x3 block guarantees for the index's cell widens k so
    no in-contact segment is missed (see `_SegIndex.rest_heights`)."""
    o = np.arange(-k, k + 1, dtype=np.int64)
    return np.repeat(o, o.size), np.tile(o, o.size)


def _pack_cells(gx, gy):
    """Pack integer cell coordinates into one int64 key (unique for any
    |coordinate| below 2^31 -- far beyond any physical print volume)."""
    return (gx << np.int64(32)) + (gy & np.int64(0xFFFFFFFF))


# The 3D siblings of the two helpers above, reached only when `_SegIndex.frame`
# is set (see `_footprint_frame`): a doubly-curved collector bins by WORLD
# footprint, which needs a third axis the chart-plane grid never carried.
_NEIGH3 = np.stack(np.meshgrid([-1, 0, 1], [-1, 0, 1], [-1, 0, 1],
                                indexing="ij")).reshape(3, -1)
_NEIGH3X, _NEIGH3Y, _NEIGH3Z = (_NEIGH3[0].astype(np.int64),
                                _NEIGH3[1].astype(np.int64),
                                _NEIGH3[2].astype(np.int64))


def _neigh_offsets3(k):
    """(dx, dy, dz) cell offsets of the (2k+1)^3 block, the 3D sibling of
    `_neigh_offsets` -- exactly `_NEIGH3X/Y/Z` for k == 1."""
    o = np.arange(-k, k + 1, dtype=np.int64)
    n = o.size
    gx = np.repeat(o, n * n)
    gy = np.tile(np.repeat(o, n), n)
    gz = np.tile(o, n * n)
    return gx, gy, gz


def _pack_cells3(gx, gy, gz):
    """Pack three integer cell coordinates into one int64 key -- 21 bits per
    axis, unique for any |coordinate| below 2^20 (a grid spanning +-31 m at a
    30 um cell, thousands of times any physical collector), the 3D sibling of
    `_pack_cells`."""
    m = np.int64(0x1FFFFF)
    return (((gx & m) << np.int64(42))
            + ((gy & m) << np.int64(21))
            + (gz & m))


def _footprint_frame(frame):
    """`frame`, or None -- whether `_SegIndex` should stop binning by chart
    plane at all, for THIS frame.

    The same gate `_local_chart` uses (`frame is not None and not
    frame.developable`), answering a different question: not what ruler to
    MEASURE a distance with, but what to bin CANDIDATES by in the first place.
    A doubly-curved collector's `(u, v)` is not a safe grid key even before any
    distance is measured: near the axis of revolution `v = rho*phi` puts two
    points a fibre apart on the surface an arbitrary distance apart in the
    chart (`mew_frame.ChartPoleExclusion`), and a design wide enough to wrap
    the collector has no single period to ghost by -- `RevolutionFrame.period_v`
    is None off a cylinder, because the wrap length is a function of `u`
    (`period_v_at`), not a scalar.

    `_SegIndex` bins by the point's own WORLD footprint instead --
    `to_world(u, v, 0)`, the base-surface point with the wall height dropped,
    exactly the "ignore the depth axis" trick a flat collector's index already
    relies on for Z. That needs no period at all: the two sides of a seam are
    the same world neighbourhood, not a cut, and the apex is one point in
    space, not a division by a vanishing radius (`RevolutionFrame.to_world`
    already answers there, isotropically, as a limit). None for a plane, a
    cylinder and a cone -- unchanged, which is what keeps their digests
    byte-identical."""
    if frame is None or getattr(frame, "developable", True):
        return None
    return frame


def _unique_int(a):
    """`np.unique(a)` for a 1-D int64 array, by sort and neighbour compare.

    The same sorted, deduplicated array. numpy 2.4's plain `np.unique` takes a
    hash path for integers and was measured at 7.3 s on 20 M keys with 5x
    repeats, against 0.71 s here. A 1 mm zig-zag linear pattern's crossing
    search hands `seg_pairs` exactly that."""
    s = np.sort(a)
    if s.size < 2:
        return s
    keep = np.empty(s.size, dtype=bool)
    keep[0] = True
    np.not_equal(s[1:], s[:-1], out=keep[1:])
    return s[keep]


# Candidate pairs a crossing search expands at once before deduplicating.
# The candidates are (pairs x cells the two share), so long legs retraced
# along one corridor ask for far more than the pairs: a hand-edited
# `G1 X100 Y100` after every move of a 15 mm scaffold asked 290 M (2.2 GB in
# one array) for 408 k distinct pairs, and 300 retraces of one line 810 M.
# A 1 mm zig-zag linear pattern, the largest design case known, asks ~20 M.
_PAIR_CHUNK = 16_000_000


def _unique_run_pairs(q, lo, cnt, ids, n):
    """Sorted unique `q*n + ids[j]` over every run `ids[lo:lo+cnt]` of each
    `q`, expanding at most `_PAIR_CHUNK` candidates at a time.

    The same array `_unique_int` gives on the whole expansion: the union of
    each chunk's unique keys has the same distinct members, sorted. Memory is
    the chunk plus the distinct pairs, whatever the duplication."""
    live = cnt > 0
    q, lo, cnt = q[live], lo[live], cnt[live]
    ends = np.cumsum(cnt)
    out = []
    a = 0
    while a < cnt.size:
        base = int(ends[a - 1]) if a else 0
        b = max(a + 1, int(np.searchsorted(ends, base + _PAIR_CHUNK,
                                           side="right")))
        c = cnt[a:b]
        tot = int(c.sum())
        pos = (np.arange(tot, dtype=np.int64)
               - np.repeat(np.cumsum(c) - c, c) + np.repeat(lo[a:b], c))
        out.append(_unique_int(np.repeat(q[a:b], c) * np.int64(n) + ids[pos]))
        a = b
    if not out:
        return _EMPTY_IDX
    return out[0] if len(out) == 1 else _unique_int(np.concatenate(out))


def _self_pair_load(p, cell):
    """How many candidates a self-crossing search of the polyline `p` at
    `cell` expands, counted without expanding them: every `_SegIndex` entry
    against every entry in its cell's 3x3 neighbourhood. Its own memory is the
    entry count, about `length / (cell/2)`."""
    p = np.asarray(p, dtype=np.float64)
    if len(p) < 2:
        return 0
    _sid, gx, gy = _SegIndex(cell)._cells_of(p[:-1, 0], p[:-1, 1],
                                             p[1:, 0], p[1:, 1])
    if gx.size == 0:
        return 0
    ukey, first, ucnt = np.unique(_pack_cells(gx, gy), return_index=True,
                                  return_counts=True)
    cx, cy = gx[first], gy[first]
    total = 0
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            nk = _pack_cells(cx + dx, cy + dy)
            j = np.minimum(np.searchsorted(ukey, nk), ukey.size - 1)
            hit = ukey[j] == nk
            total += int((ucnt[hit] * ucnt[j[hit]]).sum())
    return total


# What the STL export's sag will attempt. Past these it refuses with a reason
# instead of filling the machine's memory (measured under a 3 GB cap):
# - one fibre's length in contact-sweep samples: a hand-edited layer of
#   450 legs to a far point is 118 m in one fibre (7.9 M samples) and filled
#   the cap in both solvers; a 50 mm / 0.25 design layer is about 10 m;
# - the exact solver's self-contact load (`_self_pair_load`): 60 retraces of
#   one line (21.7 M) exported at 1.0 GB, 88 (46 M) hit the cap, in
#   `rest_heights`, whose candidates are real neighbours, not duplicates.
_SAG_MAX_FIBRE_SAMPLES = 5_000_000
_SAG_MAX_SELF_LOAD = 25_000_000


def sag_budget_problem(paths, diameter, exact=True):
    """Why the sag solve of `paths` would exceed the export's budgets above,
    in words for the run log, or None when it is within them."""
    cell = _hash_cell(diameter)
    for i, p in enumerate(paths):
        p = np.asarray(p, dtype=np.float64)
        if len(p) < 2:
            continue
        length = float(np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1).sum())
        if length / (0.5 * cell) > _SAG_MAX_FIBRE_SAMPLES:
            return (f"fibre {i + 1} is {length / 1000.0:,.1f} m long, too long "
                    f"to sag")
        if exact and len(p) >= 4 and \
                _self_pair_load(p, cell) > _SAG_MAX_SELF_LOAD:
            return (f"fibre {i + 1} retraces its own path too many times for "
                    f"the exact sag (the Approximate solver can take it)")
    return None


class _SegIndex:
    """XY-binned set of 3D line segments for fast 'what is near here' queries.

    Segments live in one growing (M, 6) array; the grid is a flat list of (packed
    cell key, segment id) entries kept sorted by key, so a query is a batched
    binary search + gather. Each segment is sampled into every cell it passes
    through at steps <= cell, so a 3x3 cell neighbourhood around any query point
    is guaranteed to contain every segment within one fiber DIAMETER of it as long
    as cell >= 2 * diameter -- see _hash_cell. Candidate sets may contain duplicate
    or out-of-reach segments; every consumer either max-reduces over them
    (rest_heights) or tests them exactly (the crossing finders), so extras can
    never change a result, only cost time."""

    def __init__(self, cell, period=None, frame=None):
        self.cell = cell
        # The COLLECTOR, or None -- see `_footprint_frame`. Set only for a
        # doubly-curved surface (a dome, and anything else `_local_chart`
        # already treats as non-developable): `add_fiber` and every query then
        # bin by the point's own WORLD footprint (`to_world(u, v, 0)`) instead
        # of by the raw chart plane, and `rest_heights` measures the exact
        # world distance to each candidate instead of a chart offset. A plane
        # and a cylinder pass None here always, so their code path -- and
        # their pinned digests -- are untouched.
        self.frame = frame
        # Period of the `y` column in mm, or None for a flat collector.
        #
        # On a mandrel `y` is the CIRCUMFERENTIAL coordinate and it wraps: a fibre
        # at v = eps and one at v = period - eps are a hair apart on the real
        # collector, so binning by raw coordinate puts them at opposite ends of the
        # grid and a later layer sags THROUGH the one below along one seam line
        # running the length of the tube. Our generator makes this the common case:
        # it emits v UNWRAPPED. `add_fiber` therefore SPLITS a fibre at every period
        # boundary and shifts each piece back into [0, period), then GHOSTS the
        # pieces within reach of a seam by +/- one period. None (the default) takes
        # the flat code path untouched, byte for byte, exactly as
        # `parse_gcode(frame=None)` does.
        #
        # A `frame` makes this meaningless rather than merely unused: the WORLD
        # footprint already puts the two sides of a seam in the same neighbourhood
        # with no cut to ghost across, and a dome's period varies with `u`
        # (`RevolutionFrame.period_v_at`) so there is no single scalar to ghost by
        # in the first place (`RevolutionFrame.period_v` is None off a cylinder).
        # Forced to None rather than left to the caller, so the two mechanisms can
        # never both be armed on one index.
        self.period = None if (period is None or frame is not None) else float(period)
        # Per-cell height cap (mm): a cell keeps only the entries within
        # `cap_depth` of its HIGHEST segment; deeper (buried) entries can never win
        # a rest_heights maximum, since the winner is within one contact reach of
        # the top. Bounding the candidates per CELL is what keeps the deposition
        # lower-index cheap on a tall stack WITHOUT the global layer-count eviction
        # that wrongly dropped a perimeter loop's deep support where the layers
        # above ballooned in different cells. Only the rest-height index
        # (`all_lower`) sets it; the crossing finders leave it None.
        self.cap_depth = None
        self.n = 0                  # segments stored so far
        self._blocks = []           # (k, 6) arrays, concatenated lazily
        self._rad_blocks = []       # (k,) per-segment radius, one block per add
        # Provenance, one block per add, ONLY maintained under a period (a flat
        # index synthesises the identity in `seg_provenance`, so nothing is
        # allocated and no flat code path changes). See `_wrap_segments`.
        self._oid_blocks = []       # (k,) caller's segment index per stored piece
        self._pt_blocks = []        # (k, 2) parametric extent within that segment
        self._prov = None           # cached (oid, t0, t1); None = stale
        self._max_seg_radius = 0.0  # largest stored segment radius (rest_heights k)
        self._radarr = None         # cached concatenation of _rad_blocks
        self._radarr_blocks = 0     # how many blocks _radarr already covers
        self._arr = None            # cached concatenation of _blocks
        self._arr_blocks = 0        # how many blocks _arr already covers
        self._aug = None            # cached query form (see _aug_array)
        self._world_aug = None      # cached WORLD form (see _world_aug_array)
        self._key_chunks = []       # packed cell keys, one chunk per add
        self._sid_chunks = []       # matching segment ids
        self._skey = None           # entry keys sorted (None = stale)
        self._ssid = None           # entry segment ids, sorted by key
        self._ukey = None           # the DISTINCT cell keys (see _build)
        self._ustart = None         # where each cell's run starts in _ssid
        self._ucnt = None           # how many entries each cell has

    def _cells_of(self, x0, y0, x1, y1):
        """(sid, gx, gy) grid samples for an array of segments, stepping <= cell/2
        along each one (endpoints included). The HALF-cell step is what lets the
        cell itself stay small: a contact query is complete when diameter + step/2
        <= cell, so half-cell stepping admits any cell >= (4/3) * diameter, where
        full-cell stepping forces cell >= 2 * diameter and with it a candidate
        neighbourhood over twice as wide (see _hash_cell)."""
        c = self.cell
        n = (np.hypot(x1 - x0, y1 - y0) / (0.5 * c)).astype(np.int64) + 1
        cnt = n + 1
        sid = np.repeat(np.arange(x0.size, dtype=np.int64), cnt)
        k = (np.arange(sid.size, dtype=np.int64)
             - np.repeat(np.cumsum(cnt) - cnt, cnt))
        t = k / n[sid]
        gx = np.floor((x0[sid] + (x1[sid] - x0[sid]) * t) / c).astype(np.int64)
        gy = np.floor((y0[sid] + (y1[sid] - y0[sid]) * t) / c).astype(np.int64)
        # Drop consecutive samples that land in the SAME cell (the old _walk
        # loop's "same as last" guard): at half-cell steps each segment hits
        # most of its cells two or three times running, and every repeat would
        # become a duplicate index entry -> a duplicate candidate in every
        # query that sees it. Same cells, ~1.9x fewer entries, identical result.
        if sid.size:
            keep = np.empty(sid.size, dtype=bool)
            keep[0] = True
            keep[1:] = ((sid[1:] != sid[:-1]) | (gx[1:] != gx[:-1])
                        | (gy[1:] != gy[:-1]))
            sid, gx, gy = sid[keep], gx[keep], gy[keep]
        return sid, gx, gy

    def _footprint(self, x, y):
        """World XYZ of the collector's BASE surface at `(x, y)` == `(u, v)`
        -- `to_world(u, v, 0)`, wall height dropped. `_SegIndex`'s binning key
        on a doubly-curved collector (`self.frame` set, see `_footprint_frame`);
        never called otherwise."""
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        wx, wy, wz = self.frame.to_world(x, y, np.zeros_like(x))
        return (np.asarray(wx, dtype=np.float64).reshape(x.shape),
                np.asarray(wy, dtype=np.float64).reshape(x.shape),
                np.asarray(wz, dtype=np.float64).reshape(x.shape))

    def _cells_of3(self, x0, y0, x1, y1):
        """(sid, gx, gy, gz) WORLD-footprint grid samples for an array of
        segments -- the 3D sibling of `_cells_of`, reached only when
        `self.frame` is set.

        SAMPLES IN WORLD, AT ANCHORS THE SURFACE ITSELF CHOOSES. What the
        binning needs is that consecutive grid samples sit no more than half a
        cell apart IN WORLD, since that plus a 3x3 neighbourhood is what
        `_hash_cell` guarantees a contact query against. A skipped cell is a
        missed candidate, not merely a slower query, so this side must never
        under-sample; extras are free by `_SegIndex`'s own contract.

        Stepping by CHART distance cannot deliver that, because a chart step
        is not a world step: an orthographic chart understates by `1/cos` of
        however far the surface has turned, which on an R10 dome is 1.005 near
        the design centre and 7.09 at the silhouette. What stood here was a
        fixed sixteenth-of-a-cell chart step -- an 8x blind margin that is
        neither a bound where the chart stretches hardest nor necessary where
        it barely stretches, and that on an imported collector cost an exact
        kernel ray cast at EVERY sample: 134 of them per 0.25 mm segment,
        120,000 per solve, 69 of the run's 87 seconds.

        So: evaluate the segment ENDS exactly, then walk the world chord
        between them -- and use the surface to decide whether that chord is
        honest. `_flatten_anchors` bisects each segment until its own measured
        midpoint sits within `_CHORD_TOL_CELLS` of the chord, which is
        adaptive rather than assumed: a flat run costs one probe, a fillet
        costs as many as it earns. The samples between anchors are pure
        arithmetic, no kernel. On the parity design that is ~2 exact rays per
        segment instead of 134, and the sagged output is unchanged bit for
        bit -- on the analytic dome as well as the imported one.

        `test_gcode_to_fibers.py`'s exhaustive segment-against-segment search
        is still the comparator that would show the sampling wrong, and the
        bit-identity of both domes' `.depo` is the evidence that it is not."""
        c = self.cell
        if not x0.size:
            e = np.zeros(0, dtype=np.int64)
            return e, e, e, e
        pieces = self._flatten_anchors(x0, y0, x1, y1)
        # every anchor-to-anchor piece is now a straight world segment; sample
        # each at half a cell of WORLD distance, the rule `_cells_of` applies
        # in a chart where chart and world are the same thing
        psid, pa, pb = pieces
        n = (np.linalg.norm(pb - pa, axis=1) / (0.5 * c)).astype(np.int64) + 1
        cnt = n + 1
        row = np.repeat(np.arange(len(psid), dtype=np.int64), cnt)
        k = (np.arange(row.size, dtype=np.int64)
             - np.repeat(np.cumsum(cnt) - cnt, cnt))
        t = (k / n[row])[:, np.newaxis]
        f = pa[row] + (pb[row] - pa[row]) * t
        sid = psid[row]
        gx = np.floor(f[:, 0] / c).astype(np.int64)
        gy = np.floor(f[:, 1] / c).astype(np.int64)
        gz = np.floor(f[:, 2] / c).astype(np.int64)
        if sid.size:
            keep = np.empty(sid.size, dtype=bool)
            keep[0] = True
            keep[1:] = ((sid[1:] != sid[:-1]) | (gx[1:] != gx[:-1])
                        | (gy[1:] != gy[:-1]) | (gz[1:] != gz[:-1]))
            sid, gx, gy, gz = sid[keep], gx[keep], gy[keep], gz[keep]
        return sid, gx, gy, gz

    def _flatten_anchors(self, x0, y0, x1, y1):
        """Chart segments -> `(sid, world_a, world_b)` pieces straight enough
        to walk as chords.

        ADAPTIVE FLATTENING, the standard curve-to-polyline construction, run
        in LEVELS so the surface is queried for a whole generation of midpoints
        at once rather than segment by segment. Each round measures the real
        sag -- the distance from a piece's own exact midpoint to the midpoint
        of its chord -- and splits only the pieces that fail. A piece over a
        flat face passes on the first round; one over a fillet splits until it
        does not.

        MEASURED, NOT BOUNDED, and that is the same standing this replaces:
        the sixteenth-of-a-cell step was documented as "a measured safety
        factor, not a proven bound". The difference is that this measurement is
        taken on the surface actually being printed on, at the place actually
        being sampled, instead of once for every surface in advance.

        `_MAX_FLATTEN_LEVELS` stops the recursion whatever the geometry, so a
        crease -- where the surface has no single tangent plane and the sag
        never converges -- costs a bounded number of probes and then falls back
        to dense chart sampling, which is what stood here before.
        """
        tol = _CHORD_TOL_CELLS * self.cell
        sid = np.arange(x0.size, dtype=np.int64)
        ua = np.column_stack([x0, y0])
        ub = np.column_stack([x1, y1])
        wa = np.column_stack(self._footprint(ua[:, 0], ua[:, 1]))
        wb = np.column_stack(self._footprint(ub[:, 0], ub[:, 1]))
        done_sid, done_a, done_b = [], [], []
        for _level in range(_MAX_FLATTEN_LEVELS):
            if not sid.size:
                break
            um = 0.5 * (ua + ub)
            wm = np.column_stack(self._footprint(um[:, 0], um[:, 1]))
            sag = np.linalg.norm(wm - 0.5 * (wa + wb), axis=1)
            flat = sag <= tol
            if flat.any():
                # a flat piece is kept WHOLE rather than as its two halves:
                # the chord and the two half-chords mark the same cells to
                # within the tolerance just measured, and one piece is one
                # walk
                done_sid.append(sid[flat])
                done_a.append(wa[flat])
                done_b.append(wb[flat])
            bent = ~flat
            if not bent.any():
                sid = sid[:0]
                break
            # both halves of every bent piece go round again, and the midpoint
            # already evaluated is re-used as the new end -- no probe is thrown
            # away
            sid = np.concatenate([sid[bent], sid[bent]])
            ua, ub, wa, wb = (np.concatenate([ua[bent], um[bent]]),
                              np.concatenate([um[bent], ub[bent]]),
                              np.concatenate([wa[bent], wm[bent]]),
                              np.concatenate([wm[bent], wb[bent]]))
        if sid.size:
            # out of levels: these pieces never flattened, so take them as
            # chords anyway -- they are now 2**_MAX_FLATTEN_LEVELS shorter than
            # the segment they came from, and the exact test downstream is what
            # decides a contact either way
            done_sid.append(sid)
            done_a.append(wa)
            done_b.append(wb)
        return (np.concatenate(done_sid),
                np.concatenate(done_a),
                np.concatenate(done_b))

    def _wrap_segments(self, seg, rad):
        """(M,6) segments -> segments wrapped into one period of `y`, plus the
        seam ghosts. Only reached when `self.period` is set.

        1. **Split at every period boundary.** A mandrel pass climbs several
           revolutions in v, so one segment can cross the seam repeatedly; the
           piece either side is a physically SEPARATE strand that happens to share
           a place on the collector.
        2. **Shift each piece into [0, period)** by whole periods, choosing the
           shift from the piece MIDPOINT so a piece ending exactly on a boundary is
           not pushed a period away from its own neighbours.
        3. **Ghost the pieces near a seam** by +/- one period. This is what joins
           the seam: the far side of the wrap is not adjacent to a query's cell
           block however the cells are numbered, but a COPY of that geometry at the
           arithmetically adjacent place is. Duplicate candidates are safe by
           construction (extras cost time, never a result).

        `reach` is 2 cells: a query needs every segment within (r_query + r_seg) and
        `_hash_cell` keeps cell >= (4/3)*diameter, so two cells is at least 2.6
        diameters. (`rest_heights` widens its own query block for a fibre fatter
        than the index was sized for; a periodic index would need its ghosts widened
        to match, which no batch solve reaches.)

        Returns (seg, rad, oid, t0, t1): the pieces, their radii, and their
        PROVENANCE -- which caller segment each piece came from and the parametric
        sub-range it covers."""
        per = self.period
        if not len(seg):
            return (seg, rad, _EMPTY_IDX,
                    np.zeros(0, dtype=np.float64), np.zeros(0, dtype=np.float64))
        b0 = np.floor(seg[:, 1] / per)
        b1 = np.floor(seg[:, 4] / per)
        hard = np.flatnonzero(b0 != b1)
        if hard.size == 0:
            out_seg, out_rad = seg.copy(), rad
            out_oid = np.arange(len(seg), dtype=np.int64)
            out_t0 = np.zeros(len(seg), dtype=np.float64)
            out_t1 = np.ones(len(seg), dtype=np.float64)
        else:
            # The segments that cross no boundary are the overwhelming majority
            # on a sampled fibre (the sample step is far below a circumference),
            # so they are taken in one slice and only the crossers are walked.
            easy = np.flatnonzero(b0 == b1)
            pieces = [seg[easy]]
            prad = [rad[easy]]
            poid = [easy.astype(np.int64)]
            pt0 = [np.zeros(easy.size, dtype=np.float64)]
            pt1 = [np.ones(easy.size, dtype=np.float64)]
            for i in hard.tolist():
                a, b = seg[i, :3], seg[i, 3:]
                lo, hi = (b0[i], b1[i]) if b0[i] < b1[i] else (b1[i], b0[i])
                bounds = np.arange(lo + 1.0, hi + 1.0) * per
                t = (bounds - a[1]) / (b[1] - a[1])
                t = np.sort(t[(t > 0.0) & (t < 1.0)])
                ts = np.concatenate([[0.0], t, [1.0]])
                pts = a + np.outer(ts, b - a)
                pieces.append(np.concatenate([pts[:-1], pts[1:]], axis=1))
                prad.append(np.full(len(ts) - 1, rad[i]))
                poid.append(np.full(len(ts) - 1, i, dtype=np.int64))
                pt0.append(ts[:-1])
                pt1.append(ts[1:])
            out_seg = np.concatenate(pieces)
            out_rad = np.concatenate(prad)
            out_oid = np.concatenate(poid)
            out_t0 = np.concatenate(pt0)
            out_t1 = np.concatenate(pt1)
        mid = 0.5 * (out_seg[:, 1] + out_seg[:, 4])
        shift = np.floor(mid / per) * per
        out_seg[:, 1] -= shift
        out_seg[:, 4] -= shift
        reach = 2.0 * self.cell
        ylo = np.minimum(out_seg[:, 1], out_seg[:, 4])
        yhi = np.maximum(out_seg[:, 1], out_seg[:, 4])
        ghosts, grads, goid, gt0, gt1 = [], [], [], [], []
        for mask, delta in ((ylo < reach, per), (yhi > per - reach, -per)):
            if mask.any():
                g = out_seg[mask].copy()
                g[:, 1] += delta
                g[:, 4] += delta
                ghosts.append(g)
                grads.append(out_rad[mask])
                goid.append(out_oid[mask])
                gt0.append(out_t0[mask])
                gt1.append(out_t1[mask])
        if ghosts:
            out_seg = np.concatenate([out_seg] + ghosts)
            out_rad = np.concatenate([out_rad] + grads)
            out_oid = np.concatenate([out_oid] + goid)
            out_t0 = np.concatenate([out_t0] + gt0)
            out_t1 = np.concatenate([out_t1] + gt1)
        return out_seg, out_rad, out_oid, out_t0, out_t1

    def _wrap_q(self, ys):
        """Query `y` values folded into [0, period). A no-op without a period,
        and IDEMPOTENT, so a consumer that wraps before calling one of the query
        methods (which wrap again) is not penalised.

        The second fold is not paranoia. `np.mod(-1e-18, P)` returns exactly `P`
        -- because `-1e-18 + P` rounds to `P` -- one past the top of a half-open
        range. `rest_heights` then measures its contact distances against `P`
        while `point_pairs`, folding again, looks the candidates up at
        `np.mod(P, P) == 0`: the two ends of the seam, so the query finds the
        fibre a whole circumference away and reports no contact at all. The span
        that lost that support drooped through the mandrel onto the bed. Guarded
        by `test_mandrel_sag.py`."""
        if self.period is None:
            return ys
        y = np.mod(np.asarray(ys, dtype=np.float64), self.period)
        return np.where(y >= self.period, 0.0, y)

    def add_fiber(self, poly, seg_radius=0.0):
        """Index one fiber's segments. `seg_radius` is this fiber's tube radius,
        stored per segment so rest_heights can compute contact against fibers of a
        DIFFERENT radius (stacked scaffolds with unequal diameters). It is
        irrelevant for indices used only by the crossing/self-contact finders,
        which test geometry rather than contact distance, so it defaults to 0.

        It may be a scalar OR a PER-POINT array parallel to `poly` -- a fibre whose
        diameter varies along its length. A per-point radius is reduced to
        per-segment by averaging the two endpoints, so a later layer sees the LOCAL
        support diameter at each crossing."""
        p = np.asarray(poly, dtype=np.float64)
        if len(p) < 2:
            return
        seg = np.concatenate([p[:-1], p[1:]], axis=1)        # (k, 6)
        if np.ndim(seg_radius) == 0:
            rad = np.full(len(seg), float(seg_radius))
        else:
            r = np.asarray(seg_radius, dtype=np.float64)
            rad = (0.5 * (r[:-1] + r[1:]) if len(r) == len(p)
                   else np.full(len(seg), float(np.mean(r)) if len(r) else 0.0))
        self.add_segments(seg, rad)

    def add_segments(self, seg, rad):
        """`add_fiber`'s back half: index `(k, 6)` segments `[x0 y0 z0 x1 y1
        z1]` with a per-segment radius `rad`. For a caller holding many
        DISJOINT runs of one fibre -- `preview_sag`'s self-lifted runs -- which
        would otherwise pay one `add_fiber` per run: the same segments in the
        same order, so the same index."""
        if self.period is not None:
            seg, rad, oid, t0, t1 = self._wrap_segments(seg, rad)
            if not len(seg):
                return
            self._oid_blocks.append(oid)
            self._pt_blocks.append(np.column_stack([t0, t1]))
            self._prov = None
        if self.frame is not None:
            sid, gx, gy, gz = self._cells_of3(seg[:, 0], seg[:, 1],
                                              seg[:, 3], seg[:, 4])
            keys = _pack_cells3(gx, gy, gz)
        else:
            sid, gx, gy = self._cells_of(seg[:, 0], seg[:, 1],
                                         seg[:, 3], seg[:, 4])
            keys = _pack_cells(gx, gy)
        self._blocks.append(seg)
        self._rad_blocks.append(rad)
        if len(rad):
            self._max_seg_radius = max(self._max_seg_radius, float(rad.max()))
        self._key_chunks.append(keys)
        self._sid_chunks.append(sid + self.n)
        self.n += len(seg)
        self._skey = None                                    # sorted view stale

    def seg_provenance(self):
        """(oid, t0, t1) per STORED segment, parallel to `_seg_array()`.

        `oid` is the index of the caller's segment the piece came from -- only
        meaningful for an index holding ONE fibre (the self-contact pass), which
        is the only consumer that needs it. `t0`/`t1` are the piece's parametric
        extent within that segment, so a hit found on a piece can be reported at
        its position along the ORIGINAL.

        Flat (`period is None`): nothing is stored and the identity is
        synthesised here -- one segment per caller segment, spanning [0, 1] --
        so the consumers below run one code path and the flat numbers come out
        bit-for-bit unchanged (`t0 + t*(t1 - t0)` is exactly `t`)."""
        if self.period is None:
            n = self.n
            return (np.arange(n, dtype=np.int64),
                    np.zeros(n, dtype=np.float64), np.ones(n, dtype=np.float64))
        if self._prov is None or len(self._prov[0]) != self.n:
            oid = (np.concatenate(self._oid_blocks) if self._oid_blocks
                   else _EMPTY_IDX)
            pt = (np.concatenate(self._pt_blocks) if self._pt_blocks
                  else np.zeros((0, 2), dtype=np.float64))
            self._prov = (oid, pt[:, 0].copy(), pt[:, 1].copy())
        return self._prov

    def query_segments(self, x0, y0, x1, y1):
        """The caller's query segments expressed in THIS index's frame.

        Returns (qx0, qy0, qx1, qy1, oid, t0, t1). Flat: the inputs verbatim,
        `oid = arange`, `t0 = 0`, `t1 = 1` -- the identity, so a flat caller is
        byte-for-byte unchanged. Periodic: each query segment is SPLIT at every
        period boundary and each piece SHIFTED into [0, period), exactly as
        `add_fiber` treats a stored fibre, because a candidate lookup can only
        find what shares its frame -- our own generator emits `v` unwrapped and
        climbing, so an unwrapped query segment lands in cells the index never
        fills and finds nothing at all.

        Pieces, not one nearest copy: a mandrel pass spans several revolutions and
        genuinely crosses another pass at MANY lattice points, one per revolution.

        NOT ghosted (unlike the stored side). The stored ghosts already carry the
        far side of the seam into the cells a query near it looks at, so ghosting
        the query too would find every seam crossing twice, and a duplicated
        crossing is a duplicated support."""
        x0 = np.asarray(x0, dtype=np.float64)
        y0 = np.asarray(y0, dtype=np.float64)
        x1 = np.asarray(x1, dtype=np.float64)
        y1 = np.asarray(y1, dtype=np.float64)
        n = x0.size
        if self.period is None or n == 0:
            return (x0, y0, x1, y1, np.arange(n, dtype=np.int64),
                    np.zeros(n, dtype=np.float64), np.ones(n, dtype=np.float64))
        per = self.period
        b0 = np.floor(y0 / per)
        b1 = np.floor(y1 / per)
        hard = np.flatnonzero(b0 != b1)
        if hard.size == 0:
            px0, py0, px1, py1 = x0.copy(), y0.copy(), x1.copy(), y1.copy()
            oid = np.arange(n, dtype=np.int64)
            t0 = np.zeros(n, dtype=np.float64)
            t1 = np.ones(n, dtype=np.float64)
        else:
            easy = np.flatnonzero(b0 == b1)
            cx0, cy0, cx1, cy1 = [x0[easy]], [y0[easy]], [x1[easy]], [y1[easy]]
            coid = [easy.astype(np.int64)]
            ct0 = [np.zeros(easy.size, dtype=np.float64)]
            ct1 = [np.ones(easy.size, dtype=np.float64)]
            for i in hard.tolist():
                lo, hi = (b0[i], b1[i]) if b0[i] < b1[i] else (b1[i], b0[i])
                t = (np.arange(lo + 1.0, hi + 1.0) * per - y0[i]) / (y1[i] - y0[i])
                t = np.sort(t[(t > 0.0) & (t < 1.0)])
                ts = np.concatenate([[0.0], t, [1.0]])
                cx0.append(x0[i] + (x1[i] - x0[i]) * ts[:-1])
                cx1.append(x0[i] + (x1[i] - x0[i]) * ts[1:])
                cy0.append(y0[i] + (y1[i] - y0[i]) * ts[:-1])
                cy1.append(y0[i] + (y1[i] - y0[i]) * ts[1:])
                coid.append(np.full(len(ts) - 1, i, dtype=np.int64))
                ct0.append(ts[:-1])
                ct1.append(ts[1:])
            px0 = np.concatenate(cx0)
            py0 = np.concatenate(cy0)
            px1 = np.concatenate(cx1)
            py1 = np.concatenate(cy1)
            oid = np.concatenate(coid)
            t0 = np.concatenate(ct0)
            t1 = np.concatenate(ct1)
        shift = np.floor(0.5 * (py0 + py1) / per) * per
        return px0, py0 - shift, px1, py1 - shift, oid, t0, t1

    def _seg_array(self):
        """(M, 6) float64 view of all segments, grown incrementally (only the
        newly added blocks are concatenated onto the cached array)."""
        nb = len(self._blocks)
        if self._arr is None or self._arr_blocks != nb:
            parts = ([] if self._arr is None or self._arr_blocks == 0
                     else [self._arr]) + self._blocks[self._arr_blocks:]
            self._arr = (np.concatenate(parts) if parts
                         else np.zeros((0, 6), dtype=np.float64))
            self._arr_blocks = nb
            self._aug = None
        return self._arr

    def _rad_array(self):
        """(M,) per-segment radius, grown incrementally to stay aligned with
        _seg_array (only the newly added blocks are concatenated on)."""
        nb = len(self._rad_blocks)
        if self._radarr is None or self._radarr_blocks != nb:
            parts = ([] if self._radarr is None or self._radarr_blocks == 0
                     else [self._radarr]) + self._rad_blocks[self._radarr_blocks:]
            self._radarr = (np.concatenate(parts) if parts
                            else np.zeros(0, dtype=np.float64))
            self._radarr_blocks = nb
        return self._radarr

    def _aug_array(self):
        """(7, M) point-distance query form of the segments: rows x0, y0, z0,
        dx, dy, dz, L2s. Degenerate (zero-XY-length) segments get dx = dy = dz = 0
        and L2s = 1, which makes the projection parameter t come out 0 and the
        closest point collapse to (x0, y0, z0) -- the degenerate case baked into
        the data so the per-pair expressions are branch-free.

        COLUMN-major (7, M) rather than the natural row-major, because the only
        consumer -- `rest_heights` -- gathers millions of candidate segments by id.
        Row-major makes that a fancy-index over 56-byte rows and leaves every field
        a stride-7 view; per ROW each gather touches one cache-resident M*8-byte
        column and hands the arithmetic contiguous arrays."""
        if self._aug is None or self._aug.shape[1] != self.n:
            seg = self._seg_array()
            aug = np.empty((7, len(seg)), dtype=np.float64)
            aug[0:3] = seg[:, 0:3].T
            d = seg[:, 3:6] - seg[:, 0:3]
            L2 = d[:, 0] ** 2 + d[:, 1] ** 2
            deg = L2 < 1e-18
            d[deg] = 0.0
            aug[3:6] = d.T
            aug[6] = np.where(deg, 1.0, L2)
            self._aug = aug
        return self._aug

    def _world_aug_array(self):
        """(6, M) TRUE-position WORLD form of every stored segment: rows
        Ax, Ay, Az, Dx, Dy, Dz -- `A` is the segment's start at its OWN
        deposited height (`to_world(u, v, h)`, not the `_footprint`'s h = 0),
        `D = B - A`. Reached only when `self.frame` is set (see
        `_footprint_frame`); `rest_heights`' WORLD-EXACT branch uses it in
        place of `_aug_array`'s chart-plane form.

        `to_world(u, v, h)` is EXACTLY AFFINE in `h` -- every term of
        `RevolutionFrame.to_world` is linear in it, because `h` is a literal
        offset along the fixed normal at `(u, v)`, not a small-h approximation
        -- so `A` and `D` computed this way are the segment's real endpoints
        in the collector's own XYZ, with no densification needed to keep the
        straight-in-(u,v,h) segment straight enough in world: it already is,
        to the same precision `to_world` itself carries."""
        if self._world_aug is None or self._world_aug.shape[1] != self.n:
            seg = self._seg_array()
            ax, ay, az = self.frame.to_world(seg[:, 0], seg[:, 1], seg[:, 2])
            bx, by, bz = self.frame.to_world(seg[:, 3], seg[:, 4], seg[:, 5])
            out = np.empty((6, len(seg)), dtype=np.float64)
            out[0] = np.asarray(ax, dtype=np.float64)
            out[1] = np.asarray(ay, dtype=np.float64)
            out[2] = np.asarray(az, dtype=np.float64)
            out[3] = np.asarray(bx, dtype=np.float64) - out[0]
            out[4] = np.asarray(by, dtype=np.float64) - out[1]
            out[5] = np.asarray(bz, dtype=np.float64) - out[2]
            self._world_aug = out
        return self._world_aug

    def _build(self):
        """Sort the (cell key, segment id) entries so each cell's members form
        one contiguous run, found by binary search. Re-sorts only when fibers
        were added since the last build; the concatenated form is kept so the
        next rebuild doesn't redo the concatenation."""
        if self._skey is not None:
            return
        if self._key_chunks:
            keys = (self._key_chunks[0] if len(self._key_chunks) == 1
                    else np.concatenate(self._key_chunks))
            sids = (self._sid_chunks[0] if len(self._sid_chunks) == 1
                    else np.concatenate(self._sid_chunks))
            order = np.argsort(keys, kind="stable")
            skey = keys[order]
            ssid = sids[order]
            if self.cap_depth is not None and skey.size:
                # drop, per cell, the entries more than `cap_depth` below that cell's
                # highest segment (buried -> can't win a rest_heights max). Each
                # cell's entries are one contiguous run in the sorted keys.
                seg = self._seg_array()
                # the segment's HIGHEST point (not midpoint): a steeply-drooping
                # segment is kept whenever EITHER end reaches within cap_depth of the
                # cell top, so the cap never drops a segment that rises into reach.
                ez = np.maximum(seg[ssid, 2], seg[ssid, 5])
                starts = np.flatnonzero(np.r_[True, skey[1:] != skey[:-1]])
                counts = np.diff(np.r_[starts, skey.size])
                cell_top = np.repeat(np.maximum.reduceat(ez, starts), counts)
                keep = ez >= cell_top - self.cap_depth
                skey, ssid = skey[keep], ssid[keep]
                # COMPACT: a segment dropped from every cell it touched is buried
                # everywhere -> drop it from the segment/aug/radius arrays too and
                # renumber. Keeps the WHOLE index bounded, not just the query keys,
                # so a tall stack does not grow the per-build sort.
                uniq, ssid = np.unique(ssid, return_inverse=True)
                if len(uniq) != self.n:
                    cseg = seg[uniq]                  # `seg` is the full array above
                    rad = self._rad_array()[uniq]
                    self._blocks = [cseg]
                    self._arr = cseg
                    self._arr_blocks = 1
                    self._rad_blocks = [rad]
                    self._radarr = rad
                    self._radarr_blocks = 1
                    self._aug = None
                    # keep the provenance aligned with the compacted segments,
                    # or a later `seg_provenance()` would index the OLD ids
                    if self._oid_blocks:
                        oid, t0, t1 = self.seg_provenance()
                        self._oid_blocks = [oid[uniq]]
                        self._pt_blocks = [np.column_stack([t0[uniq], t1[uniq]])]
                        self._prov = None
                    self.n = len(uniq)
                ssid = ssid.astype(np.int64)
            self._skey = skey
            self._ssid = ssid
            self._key_chunks = [self._skey]
            self._sid_chunks = [self._ssid]
        else:
            self._skey = _EMPTY_IDX
            self._ssid = _EMPTY_IDX
        # Per-CELL run table: the distinct cell keys, plus where each cell's run of
        # entries starts and how long it is, so a query costs ONE binary search
        # over the (much shorter) distinct-key array rather than two over every
        # entry. Same runs, same candidates; the sag solve issues tens of millions
        # of these lookups and they were the index's largest single cost.
        if self._skey.size:
            self._ukey, ustart, self._ucnt = np.unique(
                self._skey, return_index=True, return_counts=True)
            self._ustart = ustart.astype(np.int64)
        else:
            self._ukey = _EMPTY_IDX
            self._ustart = _EMPTY_IDX
            self._ucnt = _EMPTY_IDX

    def _pairs_for_cells(self, item, gx, gy, gz=None, k=1):
        """(item id, segment id) pairs for every entry in the (2k+1)x(2k+1) [or,
        with `gz`, (2k+1)^3] cell neighbourhood of each given cell (k=1 -> the
        3x3 [3x3x3] default, byte-identical to before when `gz` is None). Item
        ids are emitted in input order, so the output is grouped by item
        (non-decreasing when the input is)."""
        runs = self._cell_runs(item, gx, gy, gz, k)
        if runs is None:
            return _EMPTY_IDX, _EMPTY_IDX
        itn, lo, cnt = runs
        tot = int(cnt.sum())
        if tot == 0:
            return _EMPTY_IDX, _EMPTY_IDX
        qid = np.repeat(itn, cnt)
        pos = (np.arange(tot, dtype=np.int64)
               - np.repeat(np.cumsum(cnt) - cnt, cnt)
               + np.repeat(lo, cnt))
        return qid, self._ssid[pos]

    def _cell_runs(self, item, gx, gy, gz=None, k=1):
        """`_pairs_for_cells` before it expands: (item per neighbour cell,
        that cell's run start in `_ssid`, its run length), or None when
        nothing is indexed or queried."""
        self._build()
        if self._skey.size == 0 or item.size == 0:
            return None
        if gz is None:
            nx, ny = (_NEIGH_X, _NEIGH_Y) if k == 1 else _neigh_offsets(k)
            itn = np.repeat(item, nx.size)
            ngx = (gx[:, None] + nx).ravel()
            ngy = (gy[:, None] + ny).ravel()
            keys = _pack_cells(ngx, ngy)
        else:
            nx, ny, nz = (_NEIGH3X, _NEIGH3Y, _NEIGH3Z) if k == 1 \
                else _neigh_offsets3(k)
            itn = np.repeat(item, nx.size)
            ngx = (gx[:, None] + nx).ravel()
            ngy = (gy[:, None] + ny).ravel()
            ngz = (gz[:, None] + nz).ravel()
            keys = _pack_cells3(ngx, ngy, ngz)
        # one search over the DISTINCT cell keys -> that cell's run (start, count);
        # a key with no entries at all lands on a mismatch and contributes nothing,
        # exactly as the old empty lo == hi bracket did (see _build)
        j = np.searchsorted(self._ukey, keys, side="left")
        np.minimum(j, self._ukey.size - 1, out=j)
        present = self._ukey[j] == keys
        lo = np.where(present, self._ustart[j], 0)
        cnt = np.where(present, self._ucnt[j], 0)
        return itn, lo, cnt

    def point_pairs(self, xs, ys, k=1):
        """(point id, segment id) candidate pairs: every indexed segment with a
        grid entry in the (2k+1)x(2k+1) neighbourhood of each query point's cell
        (k=1 -> the 3x3 default). May contain duplicates. Each point's pairs are
        contiguous in the output, but the points themselves are ordered by CELL, not
        by id -- order-sensitive consumers must sort by point id afterwards.

        Query points are far denser than grid cells (sag samples are spaced
        well below one cell), so the binary searches are run once per UNIQUE
        cell and the resulting per-cell candidate runs are then fanned out to
        that cell's points -- typically several times fewer searches."""
        c = self.cell
        if self.frame is not None:
            fx, fy, fz = self._footprint(xs, ys)
            gx = np.floor(fx / c).astype(np.int64)
            gy = np.floor(fy / c).astype(np.int64)
            gz = np.floor(fz / c).astype(np.int64)
            keys = _pack_cells3(gx, gy, gz)
            ukeys, first, inv = np.unique(keys, return_index=True,
                                          return_inverse=True)
            cqid, csid = self._pairs_for_cells(
                np.arange(ukeys.size, dtype=np.int64),
                gx[first], gy[first], gz[first], k=k)
        else:
            ys = self._wrap_q(ys)          # no-op on a flat collector
            gx = np.floor(xs / c).astype(np.int64)
            gy = np.floor(ys / c).astype(np.int64)
            keys = _pack_cells(gx, gy)
            ukeys, first, inv = np.unique(keys, return_index=True,
                                          return_inverse=True)
            cqid, csid = self._pairs_for_cells(
                np.arange(ukeys.size, dtype=np.int64),
                gx[first], gy[first], k=k)
        if cqid.size == 0:
            return _EMPTY_IDX, _EMPTY_IDX
        # pairs per unique cell (cqid is grouped and non-decreasing), and the
        # start of each cell's pair-run
        ccnt = np.bincount(cqid, minlength=ukeys.size)
        cstart = np.concatenate([[0], np.cumsum(ccnt)])[:-1]
        # gather points cell by cell so each point's pairs stay contiguous
        order = np.argsort(inv, kind="stable")
        oinv = inv[order]
        pcnt = ccnt[oinv]
        tot = int(pcnt.sum())
        if tot == 0:
            return _EMPTY_IDX, _EMPTY_IDX
        qid = np.repeat(order, pcnt)
        pos = (np.arange(tot, dtype=np.int64)
               - np.repeat(np.cumsum(pcnt) - pcnt, pcnt)
               + np.repeat(cstart[oinv], pcnt))
        return qid, csid[pos]

    def seg_pairs(self, x0, y0, x1, y1):
        """(query segment id, indexed segment id) candidate pairs: every
        indexed segment whose grid entries fall within the 3x3 neighbourhood of
        any cell the query segment passes through. Deduplicated and sorted."""
        x0 = np.asarray(x0, dtype=np.float64)
        y0 = np.asarray(y0, dtype=np.float64)
        x1 = np.asarray(x1, dtype=np.float64)
        y1 = np.asarray(y1, dtype=np.float64)
        if self.frame is not None:
            sid, gx, gy, gz = self._cells_of3(x0, y0, x1, y1)
            runs = self._cell_runs(sid, gx, gy, gz)
        else:
            sid, gx, gy = self._cells_of(x0, y0, x1, y1)
            runs = self._cell_runs(sid, gx, gy)
        if runs is None:
            return _EMPTY_IDX, _EMPTY_IDX
        # deduplicated as it expands (`_unique_run_pairs`): a corridor of
        # retraced legs repeats each pair once per shared cell
        pair = _unique_run_pairs(*runs, self._ssid, self.n)
        if pair.size == 0:
            return _EMPTY_IDX, _EMPTY_IDX
        return pair // self.n, pair % self.n

    def rest_heights(self, xs, ys, q_radius, floor, _chunk=4_000_000,
                     pair_sink=None, local=None):
        """Highest centre-height a fiber of radius `q_radius` would rest at over
        the segments near each query point (xs[i], ys[i]); never below `floor`.

        `q_radius` is a SCALAR, or a PER-POINT array parallel to `xs`/`ys` when
        the querying fibre's own diameter varies along its length -- the
        deposited diameter is mass conservation at the landing speed, so a
        reversal tip is genuinely fatter than the run into it and has to reach
        further for its support. The 3x3 block is widened off the MAXIMUM of it,
        so a fat stretch cannot lose a candidate a thin one would have found.
        Two tubes touch when their centres are (r_query + r_segment) apart -- an
        XY offset h then gives a vertical centre gap of sqrt(s^2 - h^2), where
        s = r_query + r_segment. Each segment carries its own radius (set in
        add_fiber), so a thin fiber rests correctly on a thicker one and vice-versa.

        `floor` is the CENTRE height each query rests at with nothing beneath it --
        the bare surface plus a radius, so the fibre's underside lands on it (see
        `_SagState._solve_layer`'s `bed_at`). A SCALAR when the layer has one
        diameter, or a PER-POINT array parallel to `xs`/`ys` when it is laid at a
        per-point diameter. With nothing below the query returns `floor` verbatim;
        with a contact, the higher of the two.

        `pair_sink` (fusion's wall/junction split; None -> off, the plain max
        verbatim) is a callable (query ids, seg dx, seg dy) -> a per-CANDIDATE sink
        (mm) subtracted from each candidate's contact height BEFORE the max, so the
        query rests on the highest POST-fusion contact. That ordering is the point:
        sinking the winner of a GEOMETRIC max instead lets two candidates tie at
        equal height, get different sinks, and hand the floor a (junction - wall)
        STEP the moment the argmax flips. Because max() of continuous candidates is
        continuous, folding the sink in removes that class of cliff entirely. A
        candidate sunk below `floor` loses to it, so "nothing sinks below the bed"
        falls out for free.

        `local` (None -> off, and off is byte-for-byte the flat solve) is a
        callable `(xs, ys) -> (a, b, c)`: the chart's metric at each query point,
        normally `frame.metric(xs, ys, h)` with `h` the layer's own wall height.
        It makes this query measure in a LOCALLY FLATTENED PATCH instead of in
        the global chart, and widens the candidate block to match -- see the two
        paragraphs above `abc` below for why both halves are needed. It is the
        one thing a doubly-curved collector needs from this method: `(u, v)` on a
        dome is sheared by `c = rho'*phi`, which grows with ABSOLUTE azimuth and
        so is not made smaller by looking at a smaller patch, and a raw chart
        distance there overstates by 66.9% at the shipped five-axis default
        against the 12.5% this index can absorb.

        NOT applied to `pair_sink`. The fusion wall/junction split hands it the
        segment's raw chart direction and compares that to the fibre's chart
        tangent, so its CROSSING ANGLE is still a chart angle. Left alone
        deliberately: it is off unless the calibration splits the two sinks, and
        moving it would change every flat scaffold's fusion."""
        xs = np.ascontiguousarray(xs, dtype=np.float64).ravel()
        ys = np.ascontiguousarray(ys, dtype=np.float64).ravel()
        # Wrap HERE, not only inside point_pairs: the contact geometry below
        # measures each candidate against `ys` itself, so the coordinate the
        # distance is taken from has to be the same one the candidate was found
        # from. Wrapping only the lookup would find the seam neighbour and then
        # compute its offset as most of a circumference. No-op when flat.
        ys = np.ascontiguousarray(self._wrap_q(ys), dtype=np.float64)
        floor_a = np.asarray(floor, dtype=np.float64)
        out = (np.full(xs.size, float(floor_a)) if floor_a.ndim == 0
               else np.ascontiguousarray(floor_a, dtype=np.float64).ravel().copy())
        if xs.size == 0 or self.n == 0:
            return out
        # Widen the query neighbourhood to the ACTUAL contact reach. A 3x3 block is
        # guaranteed to hold every segment within contact distance s = q_radius +
        # seg_radius only while cell >= (4/3)*s (see _hash_cell); a fibre fatter
        # than the cell was sized for reaches further, so a fixed 3x3 would miss
        # its supports and it would droop through them. k = ceil(s/cell + 1/4)
        # cells keeps every contact however fat a fibre grows, so the persistent
        # index never needs re-sizing and the deposited diameter never needs
        # clamping. k == 1 (the byte-identical default) whenever the cell already
        # covers the reach, which is every batch apply_sag.
        q_rad = np.asarray(q_radius, dtype=np.float64)
        s_max = float(q_rad.max()) + self._max_seg_radius
        # THE LOCAL CHART, and it changes both halves of this query. `local`
        # (None -> every line below is byte-for-byte the flat solve, including
        # the absence of an import) returns `mew_frame.metric` at each query
        # point, which makes the chart's own Jacobian available per point:
        #
        #   the RULER. A raw `(dx, dy)` is not a distance wherever the chart is
        #     sheared, and this query turns a distance straight into a deposited
        #     height through `sqrt(s^2 - h^2)`. `mew_frame.local_offsets` maps
        #     both the offset to the segment and the segment's own direction
        #     into the patch flattened AT THE QUERY POINT, so the clamp and the
        #     distance below are physical. That patch is the `normal`
        #     chart: 0.0003% at contact scale against the exact great circle.
        #   the REACH. A segment physically within `s` can be as far as
        #     `s*(1 + overstatement)` away in the chart, so a block sized for
        #     `s` would MISS it -- the one failure mode this index cannot
        #     absorb, since its contract only permits extras. `k` therefore
        #     widens by the same overstatement the gate was checked with
        #     (`mew_frame.overstatement_from_metric`, one spelling for both).
        #
        # A 3x3 block is guaranteed to hold every segment within contact
        # distance s = q_radius + seg_radius only while cell >= (4/3)*s (see
        # _hash_cell); a fibre fatter than the cell was sized for reaches
        # further, so a fixed 3x3 would miss its supports and it would droop
        # through them. k = ceil(s*inflate/cell + 1/4) cells keeps every contact
        # however fat a fibre grows or however sheared the chart is, so the
        # persistent index never needs re-sizing and the deposited diameter
        # never needs clamping. k == 1 (the byte-identical default) whenever the
        # cell already covers the reach, which is every flat batch apply_sag.
        abc = None
        inflate = 1.0
        # A doubly-curved collector (`self.frame` set) skips `local` entirely:
        # candidates are already found by WORLD proximity (see
        # `_footprint_frame`), so no chart-distance overstatement can hide a
        # contact and no widening factor is needed -- k stays the plain
        # `_hash_cell` guarantee, exactly as a flat collector's. Below, the
        # WORLD-EXACT branch replaces `local`'s chart-plane correction with the
        # real geometry instead of an even-better approximation of it.
        if local is not None and self.frame is None:
            import mew_frame
            abc = tuple(np.broadcast_to(
                np.atleast_1d(np.asarray(m, dtype=np.float64)), xs.shape)
                for m in local(xs, ys))
            inflate = 1.0 + float(np.max(
                mew_frame.overstatement_from_metric(*abc)))
        k = max(1, int(math.ceil(s_max * inflate / self.cell + 0.25)))
        qid, sid = self.point_pairs(xs, ys, k=k)
        if qid.size == 0:
            return out
        rad = self._rad_array()
        if self.frame is not None:
            # WORLD-EXACT: `to_world(u, v, h)` is exactly AFFINE in `h` (every
            # term of `RevolutionFrame.to_world` is linear in it -- `h` is a
            # literal offset along the fixed normal at `(u, v)`), so the
            # query's own rest-height search is a genuine straight RAY in
            # world, `P0 + t*N`, and `_world_aug_array` already holds each
            # candidate's TRUE deposited position. The touching condition is
            # the SAME sphere-vs-line quadratic this method has always solved,
            # with the query's own local normal standing in for the flat
            # collector's global +Z -- decompose into components parallel and
            # perpendicular to `N` exactly as the chart branch decomposes into
            # (x, y) and z.
            #
            # THE HEIGHT IS THE CHART'S `h`, NOT THE WORLD CHORD'S. Only the
            # IN-SURFACE offset is asked of world geometry here; how high the
            # candidate sits above the collector is its own `h` column, read
            # exactly as the chart branch below reads it. A segment straight in
            # `(u, v)` is a CURVE in world -- that is what `wrap_polyline`
            # exists for -- so `_world_aug_array`'s straight chord sits its own
            # sagitta inside the collector, and taking the height from that
            # chord lowered every fibre by it. Measured on the shipped 10 mm
            # dome at the 0.5 mm sample step the generator writes:
            # `0.5^2/(8*10)` = 3.13 um of it, which is the whole of the 5.05 um
            # the 6 mm design had drifted. The PERPENDICULAR offset is
            # left on the world chord: the sagitta is a displacement along the
            # normal, so it lands in the parallel component alone, and the
            # world ruler is what makes this branch pole-safe.
            p0x, p0y, p0z = self.frame.to_world(xs, ys, np.zeros_like(xs))
            n1x, n1y, n1z = self.frame.to_world(xs, ys, np.ones_like(xs))
            p0x = np.asarray(p0x, dtype=np.float64)
            p0y = np.asarray(p0y, dtype=np.float64)
            p0z = np.asarray(p0z, dtype=np.float64)
            nvx = np.asarray(n1x, dtype=np.float64) - p0x
            nvy = np.asarray(n1y, dtype=np.float64) - p0y
            nvz = np.asarray(n1z, dtype=np.float64) - p0z
            wa = self._world_aug_array()
            aug = self._aug_array()      # pair_sink's dx, dy, and the h column
            for a in range(0, qid.size, _chunk):
                q = qid[a:a + _chunk]
                sidc = sid[a:a + _chunk]
                ax_, ay_, az_ = wa[0][sidc], wa[1][sidc], wa[2][sidc]
                dx_, dy_, dz_ = wa[3][sidc], wa[4][sidc], wa[5][sidc]
                qpx, qpy, qpz = p0x[q], p0y[q], p0z[q]
                qnx, qny, qnz = nvx[q], nvy[q], nvz[q]
                aqx, aqy, aqz = ax_ - qpx, ay_ - qpy, az_ - qpz
                aq_par = aqx * qnx + aqy * qny + aqz * qnz
                d_par = dx_ * qnx + dy_ * qny + dz_ * qnz
                aqpx = aqx - aq_par * qnx
                aqpy = aqy - aq_par * qny
                aqpz = aqz - aq_par * qnz
                dpx = dx_ - d_par * qnx
                dpy = dy_ - d_par * qny
                dpz = dz_ - d_par * qnz
                L2 = dpx * dpx + dpy * dpy + dpz * dpz
                deg = L2 < 1e-18
                t = np.clip(-(aqpx * dpx + aqpy * dpy + aqpz * dpz)
                            / np.where(deg, 1.0, L2), 0.0, 1.0)
                # THE CANDIDATE AT `t` IS ON THE COLLECTOR, NOT ON THE CHORD.
                # `t` above is the closest approach to the straight world
                # chord, which is all that is needed to LOCATE the contact; but
                # a segment straight in `(u, v, h)` is a CURVE in world, so
                # reading the contact's position off the chord puts it its own
                # sagitta inside the collector and every fibre resting on it
                # rests that much low. The fibre IS straight between its
                # vertices in the chart, so the true point is one `to_world` of
                # the chart interpolation -- exact, and it needs no
                # densification of the index. Measured on the shipped 10 mm
                # dome at the generator's 0.5 mm sample step, a fibre laid
                # directly on another (`probe_floor2.py`): 2.79 um low from the
                # chord, 0.004 um from this.
                cu_ = aug[0][sidc] + aug[3][sidc] * t
                cv_ = aug[1][sidc] + aug[4][sidc] * t
                ch_ = aug[2][sidc] + aug[5][sidc] * t
                cwx, cwy, cwz = self.frame.to_world(cu_, cv_, ch_)
                cax = np.asarray(cwx, dtype=np.float64) - qpx
                cay = np.asarray(cwy, dtype=np.float64) - qpy
                caz = np.asarray(cwz, dtype=np.float64) - qpz
                par = cax * qnx + cay * qny + caz * qnz
                perp2 = ((cax - par * qnx) ** 2 + (cay - par * qny) ** 2
                         + (caz - par * qnz) ** 2)
                s = (q_rad[q] if q_rad.ndim else q_rad) + rad[sidc]
                s2 = s * s
                height = np.where(perp2 < s2,
                                  par + np.sqrt(np.maximum(s2 - perp2, 0.0)),
                                  -np.inf)
                if pair_sink is not None:
                    height = height - pair_sink(q, aug[3][sidc], aug[4][sidc])
                starts = np.flatnonzero(np.r_[True, q[1:] != q[:-1]])
                red = np.maximum.reduceat(height, starts)
                tq = q[starts]
                out[tq] = np.maximum(out[tq], red)
            return out
        aug = self._aug_array()
        for a in range(0, qid.size, _chunk):
            q = qid[a:a + _chunk]
            sidc = sid[a:a + _chunk]
            x0 = aug[0][sidc]
            y0 = aug[1][sidc]
            dx = aug[3][sidc]
            dy = aug[4][sidc]
            px = xs[q]
            py = ys[q]
            if abc is None:
                t = np.clip(((px - x0) * dx + (py - y0) * dy) / aug[6][sidc],
                            0.0, 1.0)
                h2 = (px - (x0 + dx * t)) ** 2 + (py - (y0 + dy * t)) ** 2
            else:
                # the patch flattened at the QUERY point: the segment's offset
                # and its direction go through the same map, so the clamp finds
                # the closest approach ON THE SURFACE rather than in the chart
                m = tuple(x[q] for x in abc)
                ox, oy = mew_frame.local_offsets(m, px - x0, py - y0)
                sx, sy = mew_frame.local_offsets(m, dx, dy)
                L2 = sx * sx + sy * sy
                deg = L2 < 1e-18
                t = np.clip((ox * sx + oy * sy)
                            / np.where(deg, 1.0, L2), 0.0, 1.0)
                h2 = (ox - sx * t) ** 2 + (oy - sy * t) ** 2
            zc = aug[2][sidc] + aug[5][sidc] * t
            # centre distance at contact: the SUM OF THE TWO LOCAL RADII
            s = (q_rad[q] if q_rad.ndim else q_rad) + rad[sidc]
            s2 = s * s
            height = np.where(h2 < s2,
                              zc + np.sqrt(np.maximum(s2 - h2, 0.0)), -np.inf)
            if pair_sink is not None:
                # fuse each candidate INTO the fibre it would rest on before ranking
                # them; -inf (out of reach) stays -inf, so a sink cannot conjure a
                # contact where there is none
                height = height - pair_sink(q, dx, dy)
            # per-point max: each point's pairs form one contiguous run (a
            # chunk boundary can split a run, but the two partial maxima merge
            # correctly through the running np.maximum into `out`)
            starts = np.flatnonzero(np.r_[True, q[1:] != q[:-1]])
            red = np.maximum.reduceat(height, starts)
            tq = q[starts]
            out[tq] = np.maximum(out[tq], red)
        return out


#: How far a chart segment's world image may bow off its own chord before
#: `_SegIndex._flatten_anchors` splits it, as a fraction of a hash cell. An
#: eighth: the samples walked along the chord are half a cell apart, so a piece
#: accepted at this tolerance strays less than a quarter of the way to the next
#: sample, and the 3x3 query neighbourhood `_hash_cell` guarantees absorbs it
#: with the margin that constant already carries (1.5 diameters against the 4/3
#: the guarantee needs).
_CHORD_TOL_CELLS = 0.125

#: Where `_flatten_anchors` stops bisecting whatever the sag says. Eight levels
#: is a 256th of the original segment -- far below a cell on any real design --
#: and it is what stops a CREASE, which has no tangent plane and whose sag
#: therefore never converges, from recursing without end.
_MAX_FLATTEN_LEVELS = 8


def _hash_cell(diameter):
    """Spatial-hash cell size shared by the sag solve and the self-cross tests.

    Contact queries ask 'which segments sit within one fiber DIAMETER of this
    point', and a segment is sampled into the grid at steps <= cell/2, so a 3x3
    neighbourhood is guaranteed to contain every such segment whenever cell >=
    (4/3) * diameter. 1.5 diameters keeps that guarantee with margin while keeping
    every query's candidate set tight: only segments within ~2 diameters are
    considered at all."""
    return 1.5 * diameter


def _surface_span_dir(frame, ua, va, ub, vb, umid, vmid, hmid):
    """A span's direction `(eu, ev)` in the surface's own principal frame, read
    off the WORLD endpoints instead of off a chart step.

    What `mew_frame.chord_drop_at` wants is the direction the fibre runs in on
    the deposition surface, resolved on (meridian, parallel) in physical units.
    The chart route to it -- `eu = a*du`, `ev = b*(dv - c*du)` -- is exact in
    real arithmetic, but its shear `c = rho'*v/rho` DIVERGES as the meridian
    approaches the axis, so on an apex-centred design `ev` is a difference of
    two large numbers that should cancel. This is the same two numbers with no
    chart in them: the world chord between the span's ends, the normal removed,
    projected on the two principal directions AT THE SPAN'S MIDPOINT.

    The parallel direction is the azimuthal one, `axis x radial`, which needs
    neither `rho` nor `phi`; the meridian direction is `n x e_v`, which is a
    unit vector because the two are already perpendicular. On the axis itself
    (`r = 0`) there is no azimuth -- and none is needed, because a pole a
    `RevolutionFrame` answers at is UMBILIC (`k1 == k2`), so Euler's theorem
    returns the same drop whatever direction it is handed.

    Only the doubly-curved branch calls this. `u` is the axis of revolution in
    the collector's own XYZ for every frame in this module
    (`RevolutionFrame.to_world` writes `(x, r sin phi, r cos phi)`), which is
    what lets the parallel direction be written down rather than differenced.
    """
    ax, ay, az = frame.to_world(ua, va, hmid)
    bx, by, bz = frame.to_world(ub, vb, hmid)
    tx = np.asarray(bx, dtype=np.float64) - np.asarray(ax, dtype=np.float64)
    ty = np.asarray(by, dtype=np.float64) - np.asarray(ay, dtype=np.float64)
    tz = np.asarray(bz, dtype=np.float64) - np.asarray(az, dtype=np.float64)
    nx, ny, nz = frame.normal(umid, vmid, hmid)
    nx = np.asarray(nx, dtype=np.float64)
    ny = np.asarray(ny, dtype=np.float64)
    nz = np.asarray(nz, dtype=np.float64)
    dot = tx * nx + ty * ny + tz * nz
    tx = tx - dot * nx
    ty = ty - dot * ny
    tz = tz - dot * nz
    _px, py, pz = frame.to_world(umid, vmid, hmid)
    py = np.asarray(py, dtype=np.float64)
    pz = np.asarray(pz, dtype=np.float64)
    r = np.hypot(py, pz)
    ok = r > 0.0
    rs = np.where(ok, r, 1.0)
    evy = -pz / rs
    evz = py / rs
    ev = ty * evy + tz * evz
    # meridian = n x e_v, with e_v = (0, evy, evz)
    eux = ny * evz - nz * evy
    euy = -nx * evz
    euz = nx * evy
    eu = tx * eux + ty * euy + tz * euz
    # On the axis the frame is umbilic, so any direction gives the same drop;
    # hand it the span's own length in the meridian slot and nothing in the
    # parallel one rather than a 0/0.
    flat = np.sqrt(tx * tx + ty * ty + tz * tz)
    return np.where(ok, eu, flat), np.where(ok, ev, 0.0)


def _local_chart(frame):
    """The contact index's ruler for this frame: `None`, or `h -> local(xs, ys)`.

    THE ONE PLACE THIS CONDITION LIVES. Two things have to agree about which
    ruler is in use -- `_SegIndex.rest_heights`' arithmetic, and which band
    `mew_frame.require_chart` applies -- and a second spelling of the test is how
    a gate comes to pass a solve the solve then cannot run. `apply_sag` reads it
    once and hands both halves the answer.

    NONE FOR EVERY DEVELOPABLE FRAME, and that is a decision rather than an
    oversight. A plane, a cylinder and a CONE keep the raw chart distance they
    have always used, so their solves stay byte-identical -- which is what pins
    the flat digests, the cylinder's, and the cone's. A cone's chart is not actually an isometry
    (`c = rho'*phi = sin(alpha)*phi`), so it keeps a residual chart dependence;
    measured, that is 0.5212 um under a 1 rad rigid turn against a 20 um fibre,
    and its 12.5% gate still refuses anything wider.

    A doubly-curved frame gets the local chart, because there the raw distance is
    not an approximation but a different number: 66.9% of overstatement at the
    shipped five-axis dome default, against the 12.5% the index can absorb.

    The returned factory takes the wall height `h` -- the metric's `a` and `b` ARE
    the offset stretch `1 + k*h`, so a layer ten plies up is measured on its own
    parallel surface and not on the bare collector.
    """
    if frame is None or getattr(frame, "developable", True):
        return None

    def at_height(h):
        return lambda xs, ys: frame.metric(xs, ys, h)

    return at_height


def _local_period_v(frame, u):
    """Vectorised `RevolutionFrame.period_v_at`: `|2*pi*rho(u)|` for a whole
    array of `u` at once. That method is scalar-only (`float(rho)` in its own
    body), and `_fold_seam_branch` needs the local period for a whole batch of
    candidate pairs together.

    ZERO WHERE `v` DOES NOT WRAP, which `_fold_seam_branch` reads as "no fold"
    -- the same answer `mew_frame._v_period` gives as None. Only a surface of
    revolution has a seam to fold across: its chart's `v` is an azimuth and
    running round the collector returns to the same world point. An imported
    solid's chart is a tangent plane and `v` is a distance across it, so there
    is no branch to pick and the raw pair is already in the only one there is.
    The test is `period_v_at`, the capability actually being asked for, not the
    `profile` underneath it."""
    if getattr(frame, "period_v_at", None) is None:
        return np.zeros(np.shape(np.asarray(u, dtype=np.float64)),
                        dtype=np.float64)
    _x, rho = frame.profile.point(np.asarray(u, dtype=np.float64))
    return np.abs(2.0 * math.pi * np.asarray(rho, dtype=np.float64))


def _fold_seam_branch(frame, qseg, sseg):
    """Shift each pair's `sseg` by whole periods of `v` -- evaluated at the
    QUERY piece's own `u` -- so the two pieces of a pair sit in the SAME local
    branch before the exact 2D crossing test runs.

    The seam fold `_SegIndex._wrap_segments` performs by GHOSTING at a
    constant period, generalised to the per-`u` period a dome actually has
    (`RevolutionFrame.period_v` is None off a cylinder; the wrap length is a
    function of `u`, `period_v_at`). Candidates here were found by WORLD
    proximity (`_footprint_frame`), so a pair straddling the un-ghosted seam is
    exactly the pair that needs this: left raw, the two sides read as most of
    a revolution apart in `v`, and the planar test misses -- or worse,
    mis-locates -- a crossing that is really one fibre's width away."""
    qv_mid = 0.5 * (qseg[:, 1] + qseg[:, 3])
    sv_mid = 0.5 * (sseg[:, 1] + sseg[:, 3])
    qu_mid = 0.5 * (qseg[:, 0] + qseg[:, 2])
    per = _local_period_v(frame, qu_mid)
    ok = per > 1e-12
    shift = np.where(ok, np.round((sv_mid - qv_mid)
                                  / np.where(ok, per, 1.0))
                     * np.where(ok, per, 0.0), 0.0)
    out = sseg.copy()
    out[:, 1] -= shift
    out[:, 3] -= shift
    return out


# How far back along a filament a contact must be before it counts as one strand
# resting on another rather than as the strand meeting itself. Used by
# `_self_contact_pairs` (and its world twin `_WorldFibre._self_pairs`) WITHIN a
# member and by `_solve_layer` across the seam between two members of one layer,
# which must agree or a contact admitted by one is refused by the other at the
# same place.
_SELF_CONTACT_ARC = 2.0          # in fibre diameters

# THE LAYER-CHANGE SEAM, across layers. In MEW the jet never stops, so a layer's
# first vertex is the previous layer's last one (`merge_travels` splices the
# layer-change hop onto the front of the next fibre; a hop of zero or sub-
# diameter length is the same point in XY). That point is one blob of polymer at
# ONE height. Every layer is solved against the structure below, and the previous
# layer's own tail is part of that structure, so left to the ordinary rules the
# new layer's head rests a full diameter ON its own tail: the centreline then
# jumps a diameter at every layer change, the two tubes meet as flat end caps one
# diameter apart, and the part is severed -- a break an FEA mesh cannot cross.
#
# The head therefore leaves from the tail's height and climbs onto what it rests
# on over `_SEAM_RAMP_ARC` diameters of arc (a smoothstep: no kink at either
# end). The same-layer twin is `_solve_layer`'s `seam_prev`; this one reaches
# across layers, which that one cannot (it lives inside one layer's member loop).
# A head matches a tail within a WHOLE diameter, not the quarter the same-layer
# seam uses: after the layer-change hop the fibre's own first vertex is the hop's
# far end, up to one fibre width from the tail (`_split_layer_spanning` only cuts
# a hop that short, and cuts the tail's own coincident vertex off as a lone band).
_SEAM_MATCH = 1.0                # head within this many diameters of a tail's XY
_SEAM_RAMP_ARC = 4.0             # in fibre diameters


def seam_tail_z(tails, x, y, diameter):
    """The height of the previous layer's tail that the fibre starting at (x, y)
    carries on from, or None. `tails` is `((x, y, z), ...)`; the nearest one
    within `_SEAM_MATCH` diameters wins."""
    best, best_d = None, _SEAM_MATCH * diameter
    for tx, ty, tz in tails or ():
        d = math.hypot(x - tx, y - ty)
        if d < best_d:
            best, best_d = tz, d
    return best


def seam_ramp(zo, s, tail_z, diameter):
    """`zo` blended toward `tail_z` over `_SEAM_RAMP_ARC` diameters of arc `s` (mm
    from the fibre's first vertex): all tail at the head, all solved height past
    the window. A BLEND, not a shift of the whole window by the head's offset: a
    shift drags every point by the same amount, so a fibre that drops off its
    perch right after the seam is carried below the floor it just came to rest
    on. A blend of two heights that were both valid stays between them."""
    zo = np.asarray(zo, dtype=np.float64)
    if not len(zo):
        return zo
    t = np.clip(np.asarray(s, dtype=np.float64) / (_SEAM_RAMP_ARC * diameter),
                0.0, 1.0)
    w = 1.0 - t * t * (3.0 - 2.0 * t)
    return zo + (float(tail_z) - zo) * w


def _arc_trim_tail(poly, arc):
    """`poly` with its last `arc` mm of arc length removed, cut exactly (the last
    kept vertex is interpolated onto the cut, not snapped to a sample).

    Returns a view-like copy; fewer than two vertices survive when the whole
    polyline is shorter than `arc`, and the caller drops it."""
    p = np.asarray(poly, dtype=np.float64)
    if len(p) < 2 or arc <= 0.0:
        return p
    seg = np.linalg.norm(np.diff(p[:, :3], axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    keep_to = cum[-1] - arc
    if keep_to <= 0.0:
        return p[:0]
    k = int(np.searchsorted(cum, keep_to))
    if k >= len(p):
        return p
    # interpolate the cut vertex between k-1 and k
    span = cum[k] - cum[k - 1]
    t = 0.0 if span <= 0.0 else (keep_to - cum[k - 1]) / span
    cut = p[k - 1] + t * (p[k] - p[k - 1])
    return np.vstack([p[:k], cut[None, :]])


def _self_pairs_xy(base, cell, period=None, frame=None):
    """Candidate NON-ADJACENT self-pairs of one polyline, WITH the geometry to
    test them in the index's own frame.

    Returns None when there is nothing to test, else
    (q, s, qseg, sseg, qt, st): `q`/`s` are the two ORIGINAL segment indices of
    each candidate pair (s < q - 1, exactly the pairs the old incremental walk
    tested); `qseg`/`sseg` are (M, 4) [x0, y0, x1, y1] of the two pieces IN ONE
    SHARED FRAME, which is what the exact intersection test must run on; and
    `qt`/`st` are (M, 2) parametric extents of those pieces within their
    original segments, so a hit at piece-parameter `t` is reported along the
    original at `qt[:, 0] + t*(qt[:, 1] - qt[:, 0])`.

    Flat (`period=None`, `frame=None`) this is the identity: the pieces ARE
    the segments and the extents are [0, 1], so every number downstream is
    bit-for-bit unchanged.

    Periodic (a mandrel) it is the whole point. Two turns of one helical pass a
    revolution apart never meet in the unwrapped `v` the solver works in, yet they
    cross on the collector -- and on a tube that crossing IS the lattice, because
    McColl's layers repeat identically so every diamond comes from a pass crossing
    ITSELF, not from the layer below.

    `frame`, for a doubly-curved collector (see `_footprint_frame`): candidates
    are found by WORLD footprint instead of by chart plane, and `sseg` is then
    re-branched onto `qseg`'s own local `v` (`_fold_seam_branch`) before the
    caller's planar test runs, so a candidate found across the un-ghosted seam
    is tested in a chart where the two pieces are actually close together."""
    n = len(base) - 1
    if n < 3:
        return None
    fp = _footprint_frame(frame)
    idx = _SegIndex(cell, period=period, frame=fp)
    idx.add_fiber(base)
    qx0, qy0, qx1, qy1, qoid, qt0, qt1 = idx.query_segments(
        base[:-1, 0], base[:-1, 1], base[1:, 0], base[1:, 1])
    qi, si = idx.seg_pairs(qx0, qy0, qx1, qy1)
    if qi.size == 0:
        return None
    soid, st0, st1 = idx.seg_provenance()
    q = qoid[qi]
    s = soid[si]
    keep = s < q - 1
    if not keep.any():
        return None
    qi, si, q, s = qi[keep], si[keep], q[keep], s[keep]
    sg = idx._seg_array()[si]
    qseg = np.column_stack([qx0[qi], qy0[qi], qx1[qi], qy1[qi]])
    sseg = sg[:, [0, 1, 3, 4]]
    if fp is not None:
        sseg = _fold_seam_branch(fp, qseg, sseg)
    return (q, s, qseg, sseg,
            np.column_stack([qt0[qi], qt1[qi]]),
            np.column_stack([st0[si], st1[si]]))


def _self_crosses(base, cell, period=None, frame=None):
    """True if the polyline `base` crosses ITSELF in XY (some pair of non-
    adjacent segments actually intersect). Used to decide whether a fiber needs
    the extra self-contact pass -- ordinary scaffold layers (rasters, spirals,
    serpentines) never self-intersect and so skip it entirely. `period` (mm, or
    None) makes the `y` axis periodic -- on a mandrel a helical pass crosses its
    own earlier turns, and a flat reading says it never touches anything.
    `frame`, for a doubly-curved collector -- see `_self_pairs_xy`."""
    got = _self_pairs_xy(np.asarray(base, dtype=np.float64), cell, period,
                        frame=frame)
    if got is None:
        return False
    _q, _s, qseg, sseg, _qt, _st = got
    hit, _t, _u = _seg_hits_xy(qseg[:, 0], qseg[:, 1], qseg[:, 2], qseg[:, 3],
                               sseg[:, 0], sseg[:, 1], sseg[:, 2], sseg[:, 3])
    return bool(hit.any())


def _fold_bridge(arcs, xo, yo, zo, arc_gap, turn_deg=150.0, pos=None):
    """Carry the filament across a 180 degree FOLD, where it can have no
    self-contact by construction.

    `_self_contact_pairs` admits a pair only once it is `_SELF_CONTACT_ARC` of arc
    back along the filament, which is what stops a strand resting on itself. At a
    fold the filament doubles back, so the returning limb is 2s of arc from the
    outgoing limb at distance s past the turn, and for s below half the arc gap
    there is nothing admissible at all. Measured on the reported ladder: past the
    fold the returning limb picks up ordinary pairs from s = 1.62 d and is lifted
    a healthy 1.25 d, but the one point that lands inside the window gets no
    support and drops onto whatever the index offers underneath -- 0.18132 against
    its own outgoing limb's 0.19018, i.e. the jet passing through polymer it laid
    a moment earlier. Those points are the residual `wall x wall` events, every
    one of them a path against ITSELF at a fold.

    The filament is CONTINUOUS, so a window it cannot find support in is spanned
    by the heights either side of it, not dropped to the floor: inside each fold
    window the height is raised to the straight line between the last supported
    point before the window and the first one after. That is the same principle as
    the visit seam (`seam_prev`) -- one blob of polymer is at one height, and the
    jet does not step down into its own last stroke -- applied WITHIN a member,
    which is the one place the seam rule cannot see.

    A floor, never a ceiling: `np.maximum`, so nothing already resting higher is
    pulled down, and a fibre with no reversal in it is returned untouched. The
    window is the solver's own `arc_gap`, so this carries exactly as far as the
    exclusion that created it and not one sample further.

    The fold is read off `(xo, yo)`, the chart plane every contact in this engine
    is found in. `pos`, optional and (N, k): read it off these positions instead
    -- the WORLD points on a collector, where the self-contacts are found and so
    where the unsupportable window is. A fold is a tangent flip either way, and
    the turn has to be measured where the filament actually runs."""
    z = np.asarray(zo, dtype=np.float64)
    n = len(z)
    if n < 5 or float(arc_gap) <= 0.0:
        return z
    p = (np.column_stack([np.asarray(xo, dtype=np.float64),
                          np.asarray(yo, dtype=np.float64)])
         if pos is None else np.atleast_2d(np.asarray(pos, dtype=np.float64)))
    if p.shape[0] != n:
        return z
    d = np.diff(p, axis=0)
    seg = np.linalg.norm(d, axis=1)
    ok = seg > 1e-12
    u = d / np.where(ok, seg, 1.0)[:, None]
    u[~ok] = 0.0
    cosang = np.einsum("ij,ij->i", u[:-1], u[1:])
    # a reversal: the tangent flips. Only ever true at a real fold -- an arc
    # turnaround, the shape a scaffold uses, sweeps through its direction change
    # over many samples and never reaches this angle between ADJACENT ones.
    rev = np.flatnonzero(ok[:-1] & ok[1:]
                         & (cosang < math.cos(math.radians(turn_deg)))) + 1
    if not rev.size:
        return z
    a = np.asarray(arcs, dtype=np.float64)
    out = z.copy()
    # a cusp can register on two adjacent samples; one window serves both
    groups = np.split(rev, np.flatnonzero(np.diff(rev) > 1) + 1)
    for g in groups:
        lo_a, hi_a = a[g[0]] - arc_gap, a[g[-1]] + arc_gap
        i0 = int(np.searchsorted(a, lo_a, "left")) - 1
        i1 = int(np.searchsorted(a, hi_a, "right"))
        if i0 < 0 or i1 >= n:
            continue                    # the window runs off the fibre: nothing
        span = a[i1] - a[i0]            # to carry from, so leave it alone
        if span <= 0.0:
            continue
        k = slice(i0 + 1, i1)
        w = (a[k] - a[i0]) / span
        out[k] = np.maximum(out[k], z[i0] + (z[i1] - z[i0]) * w)
    return out


def _self_near(base, cum, cell, diameter, period=None, frame=None):
    """True if the polyline `base` can rest on its OWN earlier self -- the gate
    that arms the same-layer self-contact pass.

    The RETRACE sibling of `_self_crosses`, and the reason it has to exist:
    `_seg_hits_xy` decides a crossing from `rx*sy - ry*sx`, so two COLLINEAR
    strands -- a stroke laid straight back over the one before it -- have a zero
    determinant and never "cross" however exactly they overlap. Every same-layer
    support in `_solve_layer` was gated on that test, so an out-and-back wall (the
    calibration sag ladder's, `mew_sweeps.generate_sag`) gained ONE fibre of height
    per visit however many passes it laid, and a 20-layer wall stood 6.5 diameters
    tall. The lift machinery was never the problem: hand `_self_contact_heights`
    the same retrace directly and it stacks the return pass exactly one fused
    diameter up. It was simply never called.

    So this asks `_self_contact_pairs` itself, on the COMMANDED vertices rather
    than on the resampled grid the real pass runs over. Asking the pass its own
    question is the whole point: the gate and the pass then share one definition
    of an admissible contact -- within one diameter, at least `2 * diameter` of
    arc back -- and cannot drift apart. A hand-rolled segment-to-segment version
    of this test read a 9-diameter serpentine turnaround as a retrace, because
    measuring the arc gap to the END of a long segment admits a pair that touches
    at its START; the pass measures at the contact, and so now does the gate.

    It is cheap for what it saves: on a 7 471-point raster layer, where it runs on
    every fibre and correctly reads False, it is 4.8% of that layer's solve.
    `period` / `frame` mean what they do in `_self_contact_pairs`.

    FLAT/CHART geometry only. A fibre laid on a doubly-curved collector asks the
    same question through `_WorldFibre.self_near`, which runs the world pair
    finder for the same reason -- the gate has to be the pass's own question in
    whichever geometry the pass will run in.
    """
    base = np.asarray(base, dtype=np.float64)
    if len(base) < 3:
        return False
    return _self_contact_pairs(cum, base[:, 0], base[:, 1], diameter, cell,
                               period=period, frame=frame) is not None


def _self_intersections(base, cum, cell, period=None, frame=None):
    """Locate every place the polyline `base` crosses ITSELF in XY.

    Returns a list of (arc_lo, arc_hi, sin_theta): the crossing's two arc-length
    positions along the fiber (arc_lo printed first) and |sin| of the angle
    between the two crossing segments (1 = square, -> 0 = grazing). Used to drop a
    fine sample window exactly where a crossing is, instead of resampling the
    whole fiber. `frame`, for a doubly-curved collector -- see `_self_pairs_xy`."""
    base = np.asarray(base, dtype=np.float64)
    got = _self_pairs_xy(base, cell, period, frame=frame)
    if got is None:
        return []
    q, s, qseg, sseg, qt, st = got
    hit, t, u = _seg_hits_xy(qseg[:, 0], qseg[:, 1], qseg[:, 2], qseg[:, 3],
                             sseg[:, 0], sseg[:, 1], sseg[:, 2], sseg[:, 3])
    if not hit.any():
        return []
    qh = q[hit]
    sh = s[hit]
    # the hit is parameterised along the PIECE; report it along the ORIGINAL
    # segment (the identity when flat -- t0 = 0, t1 = 1)
    ta = qt[hit, 0] + t[hit] * (qt[hit, 1] - qt[hit, 0])
    ub = st[hit, 0] + u[hit] * (st[hit, 1] - st[hit, 0])
    arc_a = cum[qh] + ta * (cum[qh + 1] - cum[qh])
    arc_b = cum[sh] + ub * (cum[sh + 1] - cum[sh])
    dax = qseg[hit, 2] - qseg[hit, 0]
    day = qseg[hit, 3] - qseg[hit, 1]
    dbx = sseg[hit, 2] - sseg[hit, 0]
    dby = sseg[hit, 3] - sseg[hit, 1]
    la = np.hypot(dax, day)
    lb = np.hypot(dbx, dby)
    okl = (la > 1e-12) & (lb > 1e-12)
    sin_t = np.where(okl, np.abs(dax * dby - day * dbx)
                     / np.where(okl, la * lb, 1.0), 1.0)
    lo = np.minimum(arc_a, arc_b)
    hi = np.maximum(arc_a, arc_b)
    return list(zip(lo.tolist(), hi.tolist(), sin_t.tolist()))


# How many grids deep a run of overlapping self-crossing windows may stack
# before `_crossing_window_arcs` samples it once (see there). Above the 3.6 the
# Demo designs reach, below the 5 a single-Z file reaches at its median.
_WINDOW_STACK_MAX = 4.0


def _crossing_window_arcs(crossings, diameter, merge_tol, total):
    """The fine sample window each self-crossing earns, on BOTH strands, as one
    array of arc positions in [0, total].

    A crossing's window is centred on its arc `ac` and runs `ac + q*merge_tol`
    for q in -steps..steps, its half-width `~3d/sin(theta)` because the lifted
    region reaches ~diameter/sin(theta) along the strand. Each window is emitted
    exactly so, sample for sample, unless its run of overlapping windows stacks
    more than `_WINDOW_STACK_MAX` grids deep.

    WHERE WINDOWS STACK DEEPER, their union is sampled ONCE at `merge_tol`, on
    the grid of the window that starts first. Laid side by side, each window kept
    its own grid, offset from its neighbours by whatever the crossings' spacing
    happened to be, so k overlapping windows put k interleaved grids on one
    stretch of fibre: k times the samples the window asks for, with no finer
    shape to show for it. On a scaffold that is nothing, because grid crossings
    are layer-to-layer and a layer crosses itself at a few turnarounds. It is
    everything on a file whose layers share ONE commanded Z (a saved MEWracle
    file with "Don't adjust height per layer"): the whole print is one
    self-crossing layer, same-direction strands of different layers lie on one
    another and cross at grazing angles, and the graded-pore demo's 100 425
    crossings (69 037 under sin 0.15, each a +-20 d window) stacked 3.45 M
    samples over 4.2 m of fibre that one grid covers with 0.42 M. The
    self-contact pair search then expands every sample against every other in
    its 3x3 cells -- 2 billion candidates -- and the export ran past 300 s.
    Sampled once, every point of every window is still within `merge_tol` of a
    sample, which is the resolution the window was asking for.

    A shallow stack keeps its windows' own samples, so a scaffold's sag output
    is unchanged to the byte. Depth is a run's window samples over the samples
    of one grid across it: on the eight Demo designs as Design generates them
    it is 1 where windows never meet and at most 3.6 (the mandrel tubes, 8
    windows in a run); on the reopened graded pore it is 5 at the median and 36
    at worst."""
    if not crossings:
        return np.empty(0, dtype=np.float64)
    c = np.asarray(crossings, dtype=np.float64).reshape(-1, 3)
    half = np.minimum(50.0 * diameter,
                      np.maximum(3.0 * diameter,
                                 3.0 * diameter / np.maximum(c[:, 2], 0.15)))
    steps = np.ceil(half / merge_tol).astype(np.int64)
    ac = np.concatenate([c[:, 0], c[:, 1]])
    steps = np.concatenate([steps, steps])
    start = ac + (-steps) * merge_tol
    end = ac + steps * merge_tol
    order = np.argsort(start, kind="stable")
    start, end, ac, steps = start[order], end[order], ac[order], steps[order]
    # a new cluster wherever a window starts at or past every earlier one's end
    reach = np.maximum.accumulate(end)
    first = np.flatnonzero(np.r_[True, start[1:] >= reach[:-1]])
    count = np.diff(np.r_[first, ac.size])
    a0 = ac[first]
    s0 = steps[first]
    c_end = np.maximum.reduceat(end, first)
    q_hi = np.where(count == 1, s0,
                    np.maximum(s0, np.ceil((c_end - a0) / merge_tol)
                               .astype(np.int64)))
    n_per = q_hi + s0 + 1
    # how many grids deep each run stacks: its windows' samples over one grid's
    n_win = 2 * steps + 1
    deep = np.add.reduceat(n_win, first) > _WINDOW_STACK_MAX * n_per
    # shallow runs: every window on its own grid, as it always was
    own = ~np.repeat(deep, count)
    w_ac, w_s, w_n = ac[own], steps[own], n_win[own]
    w_off = np.repeat(np.cumsum(w_n) - w_n, w_n)
    w_q = np.arange(int(w_n.sum()), dtype=np.int64) - w_off - np.repeat(w_s, w_n)
    # deep runs: one grid across the union
    a0, s0, n_per = a0[deep], s0[deep], n_per[deep]
    off = np.repeat(np.cumsum(n_per) - n_per, n_per)
    q = np.arange(int(n_per.sum()), dtype=np.int64) - off - np.repeat(s0, n_per)
    arcs = np.concatenate([np.repeat(w_ac, w_n) + w_q * merge_tol,
                           np.repeat(a0, n_per) + q * merge_tol])
    return arcs[(arcs >= 0.0) & (arcs <= total)]


# How far both ends of a lower segment may sit from this fibre's own path for the
# pair to count as two copies of ONE path rather than a crossing (`_same_path`),
# mm. Measured on the default sag ladder: one sweep's path repeats the last to
# 1.4e-8 mm; a fibre is microns across. 1 nm sits orders of magnitude from both.
_SAME_PATH_TOL = 1e-6


def _same_path(p0, p1, path, cum, arc, reach, tol=_SAME_PATH_TOL):
    """Per candidate crossing: do BOTH ends of the lower segment, `p0[i]` and
    `p1[i]`, lie on this fibre's own polyline `path` (to `tol`)?

    Then the lower segment is a chord of the same path, not another fibre. A
    fibre laid along the path the layer below already took -- every sweep of
    the sag ladder's travel lane, any wall stacked on itself -- rests on that
    layer along its LENGTH, which is the floor's business (rest heights, then
    the relaxation). But the layer below is stored as the solver's own output,
    chords between its samples, and each chord cuts across the path wherever
    the path bends (at the bend's own angle, 10-20 degrees on the lane's hops)
    or wherever float noise tips two collinear segments. Each such cut read as
    an exact crossing, and so as a guaranteed support at a position the
    SAMPLING chose: 9521 of 11116 "crossings" on the ladder's lane, pinning
    some strands down to the one below and leaving their neighbours hanging.

    The test needs no angle: a real crossing's lower segment leaves this path
    by its own length times sin(theta), however shallow the angle. Searched
    over the path's segments within `reach[i]` of arc either side of
    `arc[i]`, which the caller makes long enough to hold both ends."""
    n = len(arc)
    if not n or len(path) < 2:
        return np.zeros(n, dtype=bool)
    path = np.asarray(path, dtype=np.float64)
    lo = np.maximum(np.searchsorted(cum, arc - reach, side="left") - 1, 0)
    hi = np.minimum(np.searchsorted(cum, arc + reach, side="right"),
                    len(path) - 1)
    cnt = np.maximum(hi - lo, 1)
    p0 = np.asarray(p0, dtype=np.float64)
    p1 = np.asarray(p1, dtype=np.float64)
    # The expanded (candidate, path segment) rows are measured
    # _SAME_PATH_BLOCK at a time and folded into each candidate's running
    # minimum. A long lower chord reaches many path segments: one candidate of
    # a 20-layer mandrel tube's drape spans 5.6M rows, ~7M in all, which took
    # the drape past 3.5 GB when expanded at once. The minimum of the same
    # distances is the same number however the rows are grouped.
    ends = np.cumsum(cnt)
    start = ends - cnt
    d0 = np.full(n, np.inf)
    d1 = np.full(n, np.inf)
    total = int(ends[-1])
    for r0 in range(0, total, _SAME_PATH_BLOCK):
        rows = np.arange(r0, min(r0 + _SAME_PATH_BLOCK, total), dtype=np.int64)
        hh = np.searchsorted(ends, rows, side="right")
        seg = np.minimum(lo[hh] + (rows - start[hh]), len(path) - 2)
        A = path[seg]
        v = path[seg + 1] - A
        L2 = np.einsum("ij,ij->i", v, v)
        L2 = np.where(L2 > 0.0, L2, 1.0)
        for P, dm in ((p0, d0), (p1, d1)):
            w = P[hh] - A
            t = np.clip(np.einsum("ij,ij->i", w, v) / L2, 0.0, 1.0)
            np.minimum.at(dm, hh, np.linalg.norm(w - t[:, None] * v, axis=1))
    return (d0 <= tol) & (d1 <= tol)


#: expanded rows one `_same_path` run holds (see the comment there)
_SAME_PATH_BLOCK = 1_000_000


def _crossing_contacts(base, cum, prev_layer, with_sin=False):
    """Arc positions along `base` of every exact XY crossing over the previous
    layer's fibers -- the guaranteed grid contacts, located to full precision,
    found by testing all candidate segment pairs in one vectorised pass. A lower
    segment lying on this fibre's own path is not a crossing (`_same_path`).
    `with_sin`: also |sin| of each crossing angle, as (arcs, sin)."""
    empty = np.zeros(0, dtype=np.float64)
    if prev_layer.n == 0 or len(base) < 2:
        return (empty, empty) if with_sin else empty
    # Express the query in the index's frame first: the identical arrays when
    # flat, and per-revolution pieces on a mandrel, where an unwrapped `v` would
    # otherwise land in cells the index never fills (see `query_segments`).
    qx0, qy0, qx1, qy1, qoid, qt0, qt1 = prev_layer.query_segments(
        base[:-1, 0], base[:-1, 1], base[1:, 0], base[1:, 1])
    # Found _CROSS_BLOCK_PIECES query pieces at a time. A 20-layer tube on a
    # 6 mm mandrel has ~7M candidate pairs, and the per-pair arrays below
    # (the six-column segment copy, the seam fold, the hit test, `_same_path`)
    # took a preview drape past 3.5 GB when built at once. `seg_pairs` returns
    # its pairs sorted by query piece and every step after it is per pair, so
    # the concatenated blocks are the same arrays in the same order.
    parts = [_crossing_block(base, cum, prev_layer, qx0, qy0, qx1, qy1, qoid,
                             qt0, qt1, lo, min(lo + _CROSS_BLOCK_PIECES,
                                               len(qx0)), with_sin)
             for lo in range(0, len(qx0), _CROSS_BLOCK_PIECES)]
    parts = [p for p in parts if p is not None]
    if not parts:
        return (empty, empty) if with_sin else empty
    if not with_sin:
        return parts[0] if len(parts) == 1 else np.concatenate(parts)
    if len(parts) == 1:
        return parts[0]
    return (np.concatenate([p[0] for p in parts]),
            np.concatenate([p[1] for p in parts]))


#: query pieces one `_crossing_contacts` block tests (see the comment there)
_CROSS_BLOCK_PIECES = 8192


def _crossing_block(base, cum, prev_layer, qx0, qy0, qx1, qy1, qoid, qt0, qt1,
                    lo, hi, with_sin):
    """`_crossing_contacts` for query pieces lo..hi-1; None when they have no
    candidate pair."""
    qi, si = prev_layer.seg_pairs(qx0[lo:hi], qy0[lo:hi], qx1[lo:hi],
                                  qy1[lo:hi])
    if qi.size == 0:
        return None
    qi = qi + lo
    sg = prev_layer._seg_array()[si]
    sx0, sy0, sx1, sy1 = sg[:, 0], sg[:, 1], sg[:, 3], sg[:, 4]
    if prev_layer.frame is not None:
        # candidates were found by WORLD footprint (see _footprint_frame), so a
        # pair straddling the un-ghosted seam needs the same re-branch the
        # between-fibre and self-crossing finders apply.
        qseg = np.column_stack([qx0[qi], qy0[qi], qx1[qi], qy1[qi]])
        sseg = _fold_seam_branch(prev_layer.frame,
                                 qseg, np.column_stack([sx0, sy0, sx1, sy1]))
        sx0, sy0, sx1, sy1 = sseg[:, 0], sseg[:, 1], sseg[:, 2], sseg[:, 3]
    hit, t, _u = _seg_hits_xy(qx0[qi], qy0[qi], qx1[qi], qy1[qi],
                              sx0, sy0, sx1, sy1)
    qk = qi[hit]
    qh = qoid[qk]
    th = qt0[qk] + t[hit] * (qt1[qk] - qt0[qk])
    arcs = cum[qh] + th * (cum[qh + 1] - cum[qh])
    # Drop the chords of this fibre's own path (`_same_path`). The lower
    # segment is in the query PIECE's frame (shifted by whole periods on a
    # mandrel), so it is moved back by that piece's shift first; flat, the
    # shift is exactly zero.
    sx0, sy0, sx1, sy1 = sx0[hit], sy0[hit], sx1[hit], sy1[hit]
    b0 = base[qh, :2] + qt0[qk, None] * (base[qh + 1, :2] - base[qh, :2])
    sh = np.column_stack([qx0[qk], qy0[qk]]) - b0
    p0 = np.column_stack([sx0, sy0]) - sh
    p1 = np.column_stack([sx1, sy1]) - sh
    reach = 2.0 * np.hypot(sx1 - sx0, sy1 - sy0) + _SAME_PATH_TOL
    real = ~_same_path(p0, p1, base[:, :2], cum, arcs, reach)
    arcs, qk = arcs[real], qk[real]
    sx0, sy0, sx1, sy1 = sx0[real], sy0[real], sx1[real], sy1[real]
    if not with_sin:
        return arcs
    ax, ay = qx1[qk] - qx0[qk], qy1[qk] - qy0[qk]
    bx, by = sx1 - sx0, sy1 - sy0
    la, lb = np.hypot(ax, ay), np.hypot(bx, by)
    ok = (la > 1e-12) & (lb > 1e-12)
    sin_t = np.where(ok, np.abs(ax * by - ay * bx)
                     / np.where(ok, la * lb, 1.0), 1.0)
    return arcs, sin_t


def _self_contact_heights(arcs, xo, yo, base_z, diameter, cell, fuse_sink=0.0,
                          fuse_wall=None, band=None, period=None, local=None,
                          frame=None):
    """Lift each point of a fiber so it rests on top of the fiber's OWN earlier
    segments wherever the path crosses itself in XY.

    The fiber is swept in arc order, which IS print order, so the later strand
    always ends on top of the earlier one and there is no circular "who is on top"
    to resolve. Only earlier segments at least `arc_gap` behind the current point
    are considered, so a smoothly continuing path never rests on itself. Two
    equal-diameter tubes touch when their centres are one diameter apart, so a point
    an XY offset h from a lower segment rests sqrt(d^2 - h^2) above it. `diameter`
    may instead be a PER-POINT array parallel to `arcs` -- the deposited diameter
    varies along a fibre -- and each contact then uses the SUM OF THE TWO LOCAL
    RADII in place of the one d. `base_z` is
    the height BEFORE self-contact (the parabola already clamped onto the
    lower-layer floor); the self-contacted heights are returned.

    `fuse_sink` (mm; 0 = off, byte-identical) lowers each self-rest by that much --
    the later strand coalescing INTO its own earlier strand exactly as it does over
    the layer below -- clamped so it never sinks below the earlier strand's centre.
    `fuse_wall` + `band` (both None -> the scalar path verbatim) split that sink by
    each self-contact's CROSSING ANGLE, blended by `_wall_weight`, so a same-layer
    contact and a between-layer one at the same angle fuse identically.

    `period` (mm, or None) makes the `y` axis periodic: on a mandrel a later turn of
    one helical pass rests on an earlier turn a whole revolution away in the
    unwrapped coordinate, which on a tube IS the lattice. The geometry is read from
    the index's stored pieces, so query and candidate are measured in one frame. A
    point can only be lifted by an earlier segment within one diameter in XY, and
    lifting changes Z alone, so every pair that can matter is found up front."""
    return _self_contact_apply(
        _self_contact_pairs(arcs, xo, yo, diameter, cell, fuse_sink=fuse_sink,
                            fuse_wall=fuse_wall, band=band, period=period,
                            local=local, frame=frame),
        base_z, fuse_sink=fuse_sink)


class _SelfPairs(tuple):
    """`_self_contact_pairs`' five print-ordered pair arrays -- (query point,
    earlier segment, closest-approach parameter, lift, per-pair sink or None) --
    carrying the LEVEL PLAN `_self_contact_apply` sweeps them with.

    A `tuple` subclass, so `qs, ks, ts, ls, fs = pairs` unpacks as the five plain
    lists it replaced did. What it adds is a place to CACHE the plan: one fibre's
    pairs are swept dozens of times (`preview_sag`'s support convergence loop
    re-solves the same fibre once per pass) and the plan is a function of the pair
    geometry alone, so it must not be rebuilt per sweep.
    """

    def __new__(cls, q, k, t, lift, sink):
        return tuple.__new__(cls, (np.ascontiguousarray(q, dtype=np.int64),
                                   np.ascontiguousarray(k, dtype=np.int64),
                                   np.ascontiguousarray(t, dtype=np.float64),
                                   np.ascontiguousarray(lift, dtype=np.float64),
                                   None if sink is None else
                                   np.ascontiguousarray(sink,
                                                        dtype=np.float64)))

    def subset(self, keep):
        """The same pairs restricted to `keep` (an index array or a boolean
        mask), print order preserved -- and with a plan of its own, because a
        sub-set of a DAG's edges is a different, generally shallower, DAG."""
        q, k, t, lift, sink = self
        return _SelfPairs(q[keep], k[keep], t[keep], lift[keep],
                          None if sink is None else sink[keep])

    def plan(self, max_rounds=64):
        p = self.__dict__.get("_plan")
        if p is None:
            p = self._plan = _self_pair_plan(self, max_rounds=max_rounds)
        return p


def _self_pair_plan(pairs, max_rounds=64):
    """Group `pairs` into DEPENDENCY LEVELS so the print-order sweep can run as
    one vectorised pass per level instead of one Python step per pair.

    A pair lifts query point `q` onto the strand height of an earlier segment
    `(k, k+1)`, and the admissibility test that built it (`A[k+1] <= A[q] -
    arc_gap`, `arc_gap > 0`) makes `k + 1 < q` STRICTLY -- so the pairs are a DAG,
    not a general recurrence. Give every point the length of the longest chain of
    lifts ending at it and a pair the level `1 + max(level[k], level[k+1])`: every
    point a level-L pair reads then has its own level below L, so once levels
    1..L-1 are applied those heights are FINAL and the whole of level L evaluates
    at once. `max` is associative and exact in floating point, so the per-level
    `np.maximum.reduceat` returns the identical bits the scalar sweep accumulated.

    The total work is one pass over the pairs however deep the DAG is; only the
    round count follows the depth, and real tubes measure a depth of 2-3 -- a lift
    chain is a crossing resting on a crossing, and a lattice stacks few of those.

    This reads only the dependency structure of whatever pairs it is handed, so it
    survives a change to how the contacts are FOUND. Returns None if the level
    relaxation has not settled within `max_rounds`, and `_self_contact_apply` then
    takes the scalar path.
    """
    q, k, _t, _lift, _sink = pairs
    if q.size == 0:
        return []
    k1 = k + 1
    n = int(max(int(q[-1]), int(k1.max()))) + 1
    lv = np.zeros(n, dtype=np.int64)
    # `q` is print-ordered, so one point's pairs are contiguous
    starts = np.flatnonzero(np.concatenate(([True], q[1:] != q[:-1])))
    gq = q[starts]
    for _ in range(max_rounds):
        per_q = np.maximum.reduceat(np.maximum(lv[k], lv[k1]), starts) + 1
        cur = lv[gq]
        if not np.any(per_q > cur):
            break
        lv[gq] = np.maximum(cur, per_q)
    else:
        return None
    level = np.maximum(lv[k], lv[k1])          # + 1 is a constant shift
    order = np.argsort(level, kind="stable")   # stable -> print order survives
    lo = level[order]
    qo = q[order]
    ko = k[order]
    to = _t[order]
    li = _lift[order]
    si = None if _sink is None else _sink[order]
    bounds = np.flatnonzero(np.concatenate(([True], lo[1:] != lo[:-1])))
    plan = []
    for i, a in enumerate(bounds):
        b = int(bounds[i + 1]) if i + 1 < bounds.size else order.size
        a = int(a)
        seg_q = qo[a:b]
        rel = np.flatnonzero(np.concatenate(([True], seg_q[1:] != seg_q[:-1])))
        plan.append((np.ascontiguousarray(ko[a:b]),
                     np.ascontiguousarray(ko[a:b] + 1),
                     np.ascontiguousarray(to[a:b]),
                     np.ascontiguousarray(li[a:b]),
                     None if si is None else np.ascontiguousarray(si[a:b]),
                     rel, np.ascontiguousarray(seg_q[rel])))
    return plan


#: Query points per block in `_self_contact_pairs`. A block's candidate pairs
#: and their working arrays are all that is alive at once; 4096 points of a
#: mandrel fibre are a few hundred thousand pairs, tens of MB.
_SELF_PAIR_BLOCK_POINTS = 4096


def _self_contact_pairs(arcs, xo, yo, diameter, cell, fuse_sink=0.0,
                        fuse_wall=None, band=None, period=None, local=None,
                        frame=None):
    """The XY half of `_self_contact_heights`, split out so a caller that solves
    one fibre REPEATEDLY can pay for it once.

    Everything up to the print-order sweep -- the index, the candidate pairs, the
    arc-gap admissibility, each pair's closest-approach parameter, its lift and
    (when the calibration splits wall from junction) its sink -- is a function of
    the fibre's XY geometry alone; `base_z` enters only in the sweep. The crossing
    angle looks Z-dependent but is not: `_tangent_at` reads columns 0 and 1 of the
    polyline and never the third.

    `local` (None -> off, byte-for-byte the flat path) is `_SegIndex.rest_heights`'
    own parameter and means the same thing here: a callable `(xs, ys) -> (a, b, c)`
    giving the chart's metric per query point, so the closest approach and the lift
    are measured in a locally flattened patch rather than in a sheared global chart.
    This function needs it for exactly the reason that one does -- `lift` is
    `sqrt(d^2 - h^2)` over a chart `h` -- and it also widens its own candidate block
    by the same overstatement, or a self-contact physically within one diameter
    would never become a candidate.

    Returns None when no pair can lift anything (the caller then passes `base_z`
    straight through), else the sweep's five print-ordered lists."""
    n = len(arcs)
    if n < 3:
        return None
    A = np.asarray(arcs, dtype=np.float64)
    X = np.asarray(xo, dtype=np.float64)
    Y = np.asarray(yo, dtype=np.float64)
    # `diameter` is a SCALAR, or a PER-POINT array parallel to `arcs` when the
    # deposited diameter varies along the fibre (mass conservation at the
    # landing speed: a 180 degree reversal tip is ~29% fatter than the stroke
    # running into it on the reported ladder). Two roles come out of it and they
    # are not the same number:
    #   * the CONTACT DISTANCE, which is the sum of the two local radii and is
    #     therefore per PAIR -- built below, once the candidates are known;
    #   * the REACH and the WINDOWS (the candidate block `kq`, the arc-gap
    #     admissibility, the tangent window), which must not shrink anywhere or
    #     a fat stretch loses a contact a thin one would have found. Those take
    #     the maximum.
    # A scalar leaves every line here byte-for-byte what it was.
    dloc = None if np.ndim(diameter) == 0 else np.asarray(diameter,
                                                          dtype=np.float64)
    d_nom = float(diameter) if dloc is None else float(dloc.max())
    arc_gap = _SELF_CONTACT_ARC * d_nom

    fp = _footprint_frame(frame)
    idx = _SegIndex(cell, period=period, frame=fp)
    idx.add_fiber(np.column_stack([X, Y, np.zeros(n)]),
                  seg_radius=0.0 if dloc is None else 0.5 * dloc)
    # the query has to be read in the index's frame too, or a candidate found
    # across the seam would have its offset measured as most of a circumference
    Yq = idx._wrap_q(Y)                     # a no-op on a flat collector
    abc = None
    kq = 1
    if local is not None and fp is None:
        import mew_frame
        abc = tuple(np.broadcast_to(
            np.atleast_1d(np.asarray(m, dtype=np.float64)), X.shape)
            for m in local(X, Yq))
        inflate = 1.0 + float(np.max(
            mew_frame.overstatement_from_metric(*abc)))
        # the block this function has always used is 3x3 at a cell of 1.5*d,
        # which covers a reach of one diameter; the chart can put a segment
        # `inflate` times further away than it physically is
        kq = max(1, int(math.ceil(d_nom * inflate / cell + 0.25)))
    # A doubly-curved collector (`fp` set): candidates are already found by
    # WORLD footprint (see `_footprint_frame`), so no CHART-distance
    # overstatement can hide a contact and no widening factor is needed here
    # either -- kq stays the plain 3x3x3 `_hash_cell` guarantee, exactly as
    # `_SegIndex.rest_heights` already treats this case. Before this fix `local`
    # (the raw chart metric) was consulted regardless of `fp`, and the chart's
    # shear is UNBOUNDED near the pole -- not merely large -- so `inflate` and
    # the `kq` it fed could reach the thousands and size a (2*kq+1)**3
    # neighbourhood array in the hundreds of gigabytes to terabytes.
    # THE PAIRS ARE FOUND AND FILTERED IN BLOCKS OF QUERY POINTS. A mandrel
    # fibre wraps its own collector many times over, so one fibre of the
    # default 4-axis design (330 000 sagged points) has 14 million candidate
    # point-segment pairs, and the dozen pair-length arrays built below (the
    # candidate ids, the segment ends, the closest-approach parameter and
    # distance) held over 2 GB at once: the default mandrel Generate failed
    # in a 3.5 GB budget. Each point's candidates come from its OWN cell's
    # run, in that run's order, whatever other points share the query, so a
    # block's survivors are exactly the pairs the whole query would keep for
    # those points, and the stable sort by point below restores the one
    # order the sweep reads. Nothing here changes a number, only how many
    # pairs are alive at once.
    soid, st0, st1 = idx.seg_provenance()
    sga = idx._seg_array()
    if fp is not None:
        p0x, p0y, p0z = fp.to_world(X, Yq, np.zeros_like(X))
        n1x, n1y, n1z = fp.to_world(X, Yq, np.ones_like(X))
        p0x = np.asarray(p0x, dtype=np.float64)
        p0y = np.asarray(p0y, dtype=np.float64)
        p0z = np.asarray(p0z, dtype=np.float64)
        qnx = np.asarray(n1x, dtype=np.float64) - p0x
        qny = np.asarray(n1y, dtype=np.float64) - p0y
        qnz = np.asarray(n1z, dtype=np.float64) - p0z
        wa = idx._world_aug_array()

    def _near_pairs(lo, hi):
        """The admissible pairs of query points lo..hi-1 within contact
        distance, or None."""
        qid, sid = idx.point_pairs(X[lo:hi], Yq[lo:hi], k=kq)
        qid = qid + lo                     # point j vs stored piece
        if qid.size == 0:
            return None
        sog = soid[sid]
        # a segment becomes a candidate only once the sweep is arc_gap past its end
        adm = A[sog + 1] <= A[qid] - arc_gap
        qid = qid[adm]
        sid = sid[adm]
        sog = sog[adm]
        if qid.size == 0:
            return None
        # static XY proximity: closest-approach parameter t and distance, exactly
        # as the per-pair lift computes them (XY never changes, so this is final)
        x0 = sga[sid, 0]
        y0 = sga[sid, 1]
        x1 = sga[sid, 3]
        y1 = sga[sid, 4]
        if fp is not None:
            # candidates were found by WORLD footprint (see _footprint_frame), so a
            # pair straddling the un-ghosted seam needs the SAME re-branch
            # `_fold_seam_branch` gives the between-fibre crossing finders --
            # folded here onto the QUERY point's own `v`, since this function's
            # candidates are points, not segment pairs. This chart bookkeeping only
            # feeds `dx, dy` below (the fusion angular blend's crossing-angle
            # input); the near-test and the lift just below are WORLD-EXACT and
            # never read x0/y0/x1/y1 again.
            per = _local_period_v(fp, X[qid])
            okp = per > 1e-12
            shift = np.where(okp, np.round((0.5 * (y0 + y1) - Yq[qid])
                                           / np.where(okp, per, 1.0))
                             * np.where(okp, per, 0.0), 0.0)
            y0 = y0 - shift
            y1 = y1 - shift
        dx = x1 - x0
        dy = y1 - y0
        if fp is not None:
            # WORLD-EXACT: the self-contact sibling of `_SegIndex.rest_heights`'
            # WORLD-EXACT branch. `to_world(u, v, h)` is exactly affine in `h`, so
            # the closest approach to each candidate (stored at its own footprint,
            # h = 0) is measured in the plane perpendicular to the QUERY point's
            # own local normal -- the same parallel/perpendicular decomposition
            # `rest_heights` uses, with the query's normal standing in for the
            # flat collector's global +Z. This replaces the chart-patch measurement
            # (`mew_frame.local_offsets`) the doubly-curved case used before: that
            # ruler's shear is unbounded at the pole, which is what let `kq` above
            # explode. No seam handling is needed here (unlike the chart branch
            # above) -- two points a period apart in `v` map to the SAME world
            # point, so `to_world` resolves the seam for free.
            ax_, ay_, az_ = wa[0][sid], wa[1][sid], wa[2][sid]
            ddx, ddy, ddz = wa[3][sid], wa[4][sid], wa[5][sid]
            qpx, qpy, qpz = p0x[qid], p0y[qid], p0z[qid]
            qnnx, qnny, qnnz = qnx[qid], qny[qid], qnz[qid]
            aqx, aqy, aqz = ax_ - qpx, ay_ - qpy, az_ - qpz
            aq_par = aqx * qnnx + aqy * qnny + aqz * qnnz
            d_par = ddx * qnnx + ddy * qnny + ddz * qnnz
            aqpx = aqx - aq_par * qnnx
            aqpy = aqy - aq_par * qnny
            aqpz = aqz - aq_par * qnnz
            dpx = ddx - d_par * qnnx
            dpy = ddy - d_par * qnny
            dpz = ddz - d_par * qnnz
            L2 = dpx * dpx + dpy * dpy + dpz * dpz
            deg = L2 < 1e-18
            t = np.clip(-(aqpx * dpx + aqpy * dpy + aqpz * dpz)
                        / np.where(deg, 1.0, L2), 0.0, 1.0)
            h2 = ((aqpx + dpx * t) ** 2 + (aqpy + dpy * t) ** 2
                  + (aqpz + dpz * t) ** 2)
        elif abc is None:
            L2 = dx * dx + dy * dy
            deg = L2 < 1e-18
            t = np.where(deg, 0.0,
                         np.clip(((X[qid] - x0) * dx + (Yq[qid] - y0) * dy)
                                 / np.where(deg, 1.0, L2), 0.0, 1.0))
            hx = X[qid] - (x0 + dx * t)
            hy = Yq[qid] - (y0 + dy * t)
            h2 = hx * hx + hy * hy
        else:
            # the same patch-flattened measurement `rest_heights` makes, at the same
            # place in the arithmetic: map the offset and the direction through the
            # query point's own Jacobian, then clamp and measure there
            m = tuple(x[qid] for x in abc)
            ox, oy = mew_frame.local_offsets(m, X[qid] - x0, Yq[qid] - y0)
            sx, sy = mew_frame.local_offsets(m, dx, dy)
            L2 = sx * sx + sy * sy
            deg = L2 < 1e-18
            t = np.where(deg, 0.0,
                         np.clip((ox * sx + oy * sy)
                                 / np.where(deg, 1.0, L2), 0.0, 1.0))
            hx = ox - sx * t
            hy = oy - sy * t
            h2 = hx * hx + hy * hy
        # THE CONTACT DISTANCE, per pair: two tubes touch when their centres are
        # (r_query + r_segment) apart, and on a varying fibre those are different
        # numbers. `idx` carries the earlier strand's own per-segment radius (it was
        # indexed with one), so this is the same sum `_SegIndex.rest_heights` forms
        # for a contact between two different fibres.
        if dloc is None:
            d2 = d_nom * d_nom
        else:
            s_pair = 0.5 * dloc[qid] + idx._rad_array()[sid]
            d2 = s_pair * s_pair
        near = h2 < d2
        if not near.any():
            return None
        d2n = d2 if np.ndim(d2) == 0 else d2[near]
        return (qid[near], sid[near], sog[near], dx[near], dy[near],
                t[near], h2[near], d2n)

    blocks = []
    step = max(1, int(_SELF_PAIR_BLOCK_POINTS))
    for lo in range(0, n, step):
        got = _near_pairs(lo, min(n, lo + step))
        if got is not None:
            blocks.append(got)
    if not blocks:
        return None
    if len(blocks) == 1:
        qid, sid, sog, dx, dy, tpc, h2n, d2n = blocks[0]
    else:
        qid, sid, sog, dx, dy, tpc, h2n = (
            np.concatenate([b[i] for b in blocks]) for i in range(7))
        d2n = (blocks[0][7] if np.ndim(blocks[0][7]) == 0
               else np.concatenate([b[7] for b in blocks]))
    del blocks
    # the closest approach is parameterised along the PIECE; the strand height
    # below is interpolated along the ORIGINAL segment (identity when flat)
    t = st0[sid] + tpc * (st1[sid] - st0[sid])
    lift = np.sqrt(d2n - h2n)
    # Per-pair sink: one magnitude unless the calibration splits wall from junction,
    # in which case each contact takes the blend for ITS crossing angle -- the earlier
    # segment's direction against the fibre's tangent at the resting point.
    if fuse_wall is not None and band is not None:
        tx, ty = _tangent_at(np.column_stack([X, Y, np.zeros(n)]), A, A[qid],
                             window=d_nom)
        sx = dx
        sy = dy
        sl = np.hypot(sx, sy)
        okd = sl > 0.0
        sxn = np.where(okd, sx / np.where(okd, sl, 1.0), 0.0)
        syn = np.where(okd, sy / np.where(okd, sl, 1.0), 0.0)
        sin_t = np.abs(tx * syn - ty * sxn)
        w = _wall_weight(sin_t, band)
        pair_sink = np.where(okd, fuse_wall * w + fuse_sink * (1.0 - w),
                             fuse_sink)
    else:
        pair_sink = None
    # point_pairs groups by CELL; the sweep must run in print order, so sort the
    # surviving pairs by point id.  Print order is also what makes the pair set
    # a DAG (see `_SelfPairs`), so the sweep reads its groups straight off it.
    order = np.argsort(qid, kind="stable")
    return _SelfPairs(qid[order], sog[order], t[order], lift[order],
                      None if pair_sink is None else pair_sink[order])


def _self_contact_sweep(pairs, Z, fuse_sink):
    """The scalar print-order sweep, one Python step per pair.  Kept as the
    fallback for a pair set whose dependency DAG `_self_pair_plan` declined to
    level (and as the definition the levelled sweep is checked against --
    both agree over the same pairs)."""
    qs, ks, ts, ls, fs = pairs
    qs = qs.tolist() if hasattr(qs, "tolist") else qs
    ks = ks.tolist() if hasattr(ks, "tolist") else ks
    ts = ts.tolist() if hasattr(ts, "tolist") else ts
    ls = ls.tolist() if hasattr(ls, "tolist") else ls
    fs = fs.tolist() if hasattr(fs, "tolist") else fs
    Zl = Z.tolist()
    i = 0
    m = len(qs)
    while i < m:
        j = qs[i]
        best = Zl[j]
        while i < m and qs[i] == j:
            k = ks[i]
            z0 = Zl[k]
            strand_z = z0 + (Zl[k + 1] - z0) * ts[i]
            h = strand_z + ls[i]
            sink = fuse_sink if fs is None else fs[i]
            if sink:                            # fuse into the earlier strand,
                h = max(strand_z, h - sink)        # never below its centre
            if h > best:
                best = h
            i += 1
        Zl[j] = best
    return np.asarray(Zl, dtype=np.float64)


def _self_contact_apply(pairs, base_z, fuse_sink=0.0):
    """The print-order sweep of `_self_contact_heights` over a prebuilt `pairs`
    (from `_self_contact_pairs`, or None for "nothing can lift"). Split out so a
    caller re-solving ONE fibre's heights re-runs only this half.

    Swept by DEPENDENCY LEVEL (`_self_pair_plan`) rather than one pair at a time:
    the lift a pair applies is a function of two EARLIER points' final heights, so
    the pairs form a DAG and every level evaluates in one vectorised step. The
    arithmetic is written out in the same order the scalar sweep used it --
    `z0 + (z1 - z0) * t`, then `+ lift`, then the sink clamp -- and `max` is
    exact, so the result is bit-identical; that is what `_self_contact_sweep` is
    still here to state. It matters because `preview_sag` sweeps one tube's pairs
    40-odd times while its supports converge, and the scalar version is linear in
    pairs times passes."""
    Z = np.asarray(base_z, dtype=np.float64).copy()
    if pairs is None:
        return Z
    plan = (pairs.plan() if isinstance(pairs, _SelfPairs)
            else _self_pair_plan(_SelfPairs(*pairs)))
    if plan is None:
        return _self_contact_sweep(pairs, Z, fuse_sink)
    for kk, kk1, tt, ll, ss, rel, gq in plan:
        z0 = Z[kk]
        strand_z = z0 + (Z[kk1] - z0) * tt
        h = strand_z + ll
        if ss is not None:
            h = np.where(ss != 0.0, np.maximum(strand_z, h - ss), h)
        elif fuse_sink:
            h = np.maximum(strand_z, h - fuse_sink)
        Z[gq] = np.maximum(Z[gq], np.maximum.reduceat(h, rel))
    return Z


def _round_crests(arcs, zo, round_r, k_max=None):
    """Round every convex-up crest of a centreline to a minimum bend radius.

    A real fibre has bending stiffness, so where it drapes over the fibre below it
    forms a smooth crest rather than a tight arc or a sharp point. ONE uniform
    operation, so a grid crossover, an interlayer loop crossover and a same-layer
    self crossover all get the identical shape however the contact was found.

    It is a grayscale dilation of `zo` by a downward parabola of curvature
    1/round_r: each point is lifted to the highest such parabola resting on a nearby
    point. Convex-up crests therefore round to radius `round_r`; sags are untouched
    (a downward parabola cannot fill a valley); flat stretches stay flat. It only
    ever RAISES the fibre, and `zo` already rests on everything below, so nothing it
    rounds can sink through what is beneath.

    Vectorised as neighbour shifts, with the SHIFT COUNT derived from the arc
    positions rather than fixed. A point p can lift a point q only while
    z[p] - (da)**2 / (2*round_r) > z[q], so no lift is possible beyond
    da = sqrt(2 * round_r * (z.max() - z.min())): count the samples inside that
    reach and the dilation is EXACT at whatever spacing the caller used. A fixed
    count instead makes the reach a number of SAMPLES, so the crest's bend radius
    tracks the sampling density -- the lopsided crossovers, the live-vs-locked crest
    mismatch, and a solver whose output cannot be its own convergence reference.

    At the shipped crest density the derived count comes out at or below 12, so
    ordinary scaffolds are unchanged; `k_max` still overrides it."""
    a = np.asarray(arcs, dtype=np.float64)
    z = np.asarray(zo, dtype=np.float64)
    n = len(z)
    if n < 3 or round_r <= 0.0:
        return z
    if k_max is None:
        reach = math.sqrt(2.0 * round_r * max(0.0, float(z.max() - z.min())))
        idx = np.arange(n)
        k_max = int(max(
            (np.searchsorted(a, a + reach, side="right") - idx).max(),
            (idx - np.searchsorted(a, a - reach, side="left")).max(), 1))
        k_max = min(k_max, int(_ROUND_CRESTS_MAX_SHIFTS * _SAG_RESOLUTION))
    out = z.copy()
    inv = 1.0 / (2.0 * round_r)
    for k in range(1, min(k_max, n - 1) + 1):
        drop = (a[k:] - a[:-k]) ** 2 * inv          # parabola fall over k samples
        out[k:] = np.maximum(out[k:], z[:-k] - drop)   # lifted by left neighbour
        out[:-k] = np.maximum(out[:-k], z[k:] - drop)  # lifted by right neighbour
    return out


# The converged relaxation (see `_relax_pins`). Always on in the product; this
# module constant exists only so a script can A/B it in-process (off = the free
# droop clamped onto the floor, the corner the relaxation replaces). It is NOT a
# .depo key -- the shipped behaviour is covered by `_DEPO_CACHE_GENERATION` --
# so never flip it in a process that also reads or writes caches.
_TANGENT_TOUCHDOWN = True

# Bound on the relaxation's rounds. Under the span**2 law each round adds the
# deepest point of every sub-span still in the floor, which is quickhull: it
# ends after about log2(samples on the longest resting run) rounds, 15 on the
# default sag ladder. Under a fitted exponent, or on a curved collector, a
# round can also release a pin;
# should that ever cycle, the bound stops it and the rest clamp still holds the
# fibre out of the floor.
_RELAX_MAX_ROUNDS = 200

# Floor on |sin| of a crossing angle when sizing a support's cap (see
# `_support_caps`): the floor the self-crossing sample window already uses, so
# a grazing crossing's cap stops at R / 0.15 instead of running the whole span.
_CAP_MIN_SIN = 0.15


def _support_caps(sups, cap_arcs, cap_half, merge_tol):
    """Half-width, along this fibre, of the round top each support rests on.

    A crossing support rests on the crossed fibre's top. That fibre's cross-
    section reaches R = r_q + r_s either side of it, which along a fibre crossing
    at angle theta is R / sin(theta) of arc. Inside that the floor is the
    support's OWN obstacle: the crest rounding draws the drape over it, and the
    span the sag law hangs (and the sag ladder calibrates) runs from the support
    itself. So the relaxation never pins on it (see `_tangent_touchdowns`).

    `cap_arcs` / `cap_half`: one entry per crossing contact, before the merge in
    `resolve_supports`. A support takes the widest cap among the contacts merged
    into it, which all lie within `merge_tol` of it. A support no crossing made
    (a fibre end, a floor peak) has none: a floor peak is an obstacle the
    relaxation pinned, so the floor round it is the relaxation's business too."""
    sa = np.asarray(sups, dtype=np.float64)
    cap = np.zeros(len(sa))
    ca = np.asarray(cap_arcs, dtype=np.float64)
    if not len(ca) or not len(sa):
        return cap
    hi = np.clip(np.searchsorted(sa, ca), 0, len(sa) - 1)
    lo = np.maximum(hi - 1, 0)
    k = np.where(np.abs(ca - sa[lo]) <= np.abs(ca - sa[hi]), lo, hi)
    near = np.abs(ca - sa[k]) <= merge_tol
    np.maximum.at(cap, k[near], np.asarray(cap_half, dtype=np.float64)[near])
    return cap


def _relax_pins(arcs, floor, sups, sup_h, span_parab, obs, tol=1e-9,
                max_rounds=None):
    """The paper's section 2.3.4 relaxation, run to convergence: pin a support on
    each obstacle the free droop meets, re-solve, until no new support appears.

    Between two supports the fibre hangs by the sag law (eq. 5, drawn by
    `span_parab`, the solver's own parabola for a span). Within each span of the
    fixed supports `sups`, every round pins one support at the sample where each
    sub-span's droop sinks deepest into `floor`, at the floor's height there, and
    redraws both new sub-spans by the same law. A pin may only hold the fibre
    UP: one the merged droop of its two neighbours clears is released.
    Converged, every stretch between neighbouring supports is a span of eq. 5,
    nothing is in the floor, and every pin pushes.

    Under the span**2 law the free curvature is the same on every sub-span
    (z'' = 8c), and the deepest point of a sub-span below its chord is a vertex
    of the upper hull of floor - 4c s**2. So every pin is one the converged
    fibre keeps, none is ever released, and the result is the least curve on or
    above the floor with z'' <= 8c: where the floor curves up harder than the
    law allows the fibre bridges it, elsewhere it lies on it, and it lands
    tangentially to within a sample. Under a fitted exponent the curvature
    depends on the sub-span, and on a curved collector the chord term
    (`curved_extra`) varies along the fibre, so a release can happen and is
    checked for: 95 releases in the first 3 layers of the five-axis matrix's
    imported block.

    Pins only ever go on samples flagged `obs`. Returns (pin, chain, hit): the
    pin mask, the converged droop at every sample, and per span whether the
    droop reached the floor there. An untouched span's chain is its free droop,
    bit for bit.

    A sub-span's depth depends on the sub-span alone, never on where along it,
    so it is read ONCE per sub-span (`span_parab(None, ...)`) and every sample
    drawn from it, and a sub-span whose ends did not move keeps the depth it had
    last round (`depths`; the release check's merged spans likewise). On a curved collector each read places a point on the surface; read
    per sample, every round, the floor-peak pass alone took 64 s of a 76 s sag
    solve on an imported block (14x `main`). Same inputs, same depth: the result
    is bit-identical either way."""
    a = np.asarray(arcs, dtype=np.float64)
    fl = np.asarray(floor, dtype=np.float64)
    sa = np.asarray(sups, dtype=np.float64)
    sh = np.asarray(sup_h, dtype=np.float64)
    if max_rounds is None:
        max_rounds = _RELAX_MAX_ROUNDS
    pin = np.zeros(len(a), dtype=bool)

    def depths(L, R, hL, hR, memo):
        # Depth of each span (L, R, hL, hR), `L` ascending. One the previous
        # call saw with the same four ends keeps its depth; `memo` holds that
        # call's spans.
        d = np.full(len(L), np.nan)
        if memo:
            mL, mR, mhL, mhR, md = memo
            i = np.minimum(np.searchsorted(mL, L), len(mL) - 1)
            same = ((mL[i] == L) & (mR[i] == R) & (mhL[i] == hL)
                    & (mhR[i] == hR))
            d[same] = md[i[same]]
        new = np.isnan(d)
        if new.any():
            d[new] = span_parab(None, L[new], R[new], hL[new], hR[new])
        memo[:] = [L, R, hL, hR, d]
        return d

    sub_memo, rel_memo = [], []

    def chain(q, pa, ph, j):
        # the droop at samples `q`, sample i on sub-span j[i]
        u = np.flatnonzero(np.bincount(j, minlength=len(pa) - 1))
        d = np.zeros(len(pa) - 1)
        d[u] = depths(pa[u], pa[u + 1], ph[u], ph[u + 1], sub_memo)
        return span_parab(q, pa[j], pa[j + 1], ph[j], ph[j + 1], depth=d[j])

    k = np.clip(np.searchsorted(sa, a, side="right") - 1, 0, len(sa) - 2)
    ch = chain(a, sa, sh, k)
    ob = np.asarray(obs, dtype=bool)
    hit = np.zeros(len(sa) - 1, dtype=bool)
    hit[k[ob & (fl > ch + tol)]] = True
    if not hit.any():
        return pin, ch, hit
    # Only these samples can take a pin, so the rounds draw only them; the
    # whole chain is drawn once, from the last round's supports.
    li = np.flatnonzero(ob & hit[k])
    al, fll = a[li], fl[li]
    pa, ph = sa, sh
    # samples in arc order (every caller's are) make each sub-span's run
    # contiguous, so its deepest sample is a segment max, not a sort
    ordered = bool(np.all(al[1:] >= al[:-1]))
    idx = np.arange(len(al))
    for _ in range(max_rounds):
        pa = np.concatenate([sa, a[pin]])
        ph = np.concatenate([sh, fl[pin]])
        o = np.argsort(pa, kind="stable")
        pa, ph = pa[o], ph[o]
        jl = np.clip(np.searchsorted(pa, al, side="right") - 1, 0, len(pa) - 2)
        pen = fll - chain(al, pa, ph, jl)
        # the deepest sample of every sub-span still in the floor (the first,
        # on a tie)
        if ordered:
            st = np.flatnonzero(np.r_[True, jl[1:] != jl[:-1]])
            top = np.repeat(np.maximum.reduceat(pen, st), np.diff(np.r_[st, len(jl)]))
            best = np.minimum.reduceat(np.where(pen == top, idx, len(jl)), st)
        else:
            order = np.lexsort((-pen, jl))
            best = order[np.r_[True, jl[order][1:] != jl[order][:-1]]]
        add = li[best[pen[best] > tol]]
        if len(add):
            pin[add] = True
            continue
        # Converged: release any pin that only pulls the fibre down -- every
        # other one at most, so two neighbours are never released together on
        # tests that each assumed the other stays.
        pi = np.flatnonzero(pin)
        if not len(pi):
            break
        q = np.searchsorted(pa, a[pi])
        L, R, hL, hR = pa[q - 1], pa[q + 1], ph[q - 1], ph[q + 1]
        m = span_parab(a[pi], L, R, hL, hR,
                       depth=depths(L, R, hL, hR, rel_memo))
        rel = np.flatnonzero(m > fl[pi] + tol)
        if not len(rel):
            break
        rel = rel[np.r_[True, np.diff(rel) > 1]]
        pin[pi[rel]] = False
    j = np.clip(np.searchsorted(pa, a, side="right") - 1, 0, len(pa) - 2)
    return pin, chain(a, pa, ph, j), hit


def _tangent_touchdowns(arcs, z_par, floor, sups, sup_h, span_parab,
                        keep=None, cap=None, tol=1e-9):
    """Rest a drooping fibre on what is below it the way the sag law does,
    instead of at the corner `maximum(droop, floor)` leaves.

    Where a span's droop reaches the floor -- the bed, a fibre lying along it,
    the flank of anything below -- `_relax_pins` pins supports on it until
    nothing is left in the floor, so every stretch is a span of eq. 5 and the
    fibre lands tangentially and lies along the floor. The clamp instead cut the
    full-span parabola off where it met the floor: a slope jump of 30-77
    degrees on the default sag ladder, the same at every sampling, and on a
    stack of fibres along one path each corner was laid on by the next.

    Run on the free droop, before the crest rounding and the clamp, which then
    treat it exactly as they treat any droop. Never pinned on: `cap` (per
    support, from `_support_caps`), the support's own round top; `keep` (bool
    per sample), windows that step down on purpose (loop entry, visit seam),
    whose floor is lowered there for the clamp. The relaxed droop is drawn
    through both and the clamp still applies. A span whose droop clears every
    pinnable sample is returned bit for bit."""
    a = np.asarray(arcs, dtype=np.float64)
    zp = np.asarray(z_par, dtype=np.float64)
    sa = np.asarray(sups, dtype=np.float64)
    if not _TANGENT_TOUCHDOWN or len(sa) < 2 or len(a) < 3:
        return zp
    fl = np.asarray(floor, dtype=np.float64)
    if not (fl > zp + tol).any():
        return zp
    k = np.clip(np.searchsorted(sa, a, side="right") - 1, 0, len(sa) - 2)
    obs = (a > sa[k]) & (a < sa[k + 1])
    if cap is not None:
        cp = np.asarray(cap, dtype=np.float64)
        obs &= (a - sa[k] >= cp[k]) & (sa[k + 1] - a >= cp[k + 1])
    if keep is not None:
        obs &= ~np.asarray(keep, dtype=bool)
    _pin, ch, hit = _relax_pins(a, fl, sa, sup_h, span_parab, obs, tol)
    if not hit.any():
        return zp
    return np.where(hit[k], ch, zp)


def _sag_applicable(paths, diameter, self_crossings=True, period=None,
                    frame=None, world=False):
    """Return (applicable, n_layers): whether sagging would change anything.

    True when there are >= 2 layers (a fiber can sag onto the one below) OR when
    some single fiber crosses itself in XY (its later strands must rest on its
    earlier ones). The second case lets single-Z self-crossing prints -- string
    art, intersecting loops -- sag even though they form only one layer.

    self_crossings=False suppresses that second case: a single-Z print whose
    only contacts are self-crossings then reports "not applicable" so the whole
    sag solve is skipped (string art -- see NO_SELF_CROSS_MARKER).

    `frame`, for a doubly-curved collector -- see `_self_pairs_xy`. A caller
    that omits it on a single-layer apex-crossing design risks a false
    "not applicable"; every caller in this module today passes `period=None`
    here regardless of frame, which is the documented residual this slice
    leaves for whoever wires a single-layer curved self-cross case in.

    `world`: `paths` are world points on `frame`'s collector (see `apply_sag`);
      layers are counted on the height along the normal and a self-crossing is
      found on the collector."""
    if world:
        surface = frame.surface()
        _, n_layers = _layer_ids(surface_heights(paths, surface),
                                 layer_tol=0.5 * diameter)
        if n_layers >= 2:
            return True, n_layers
        if not self_crossings:
            return False, n_layers
        lg = _WorldLayer(surface, 0.5 * diameter, diameter, 1.0, None)
        cell = _hash_cell(diameter)
        for p in paths:
            base = _dedupe(np.asarray(p, dtype=np.float64))
            if len(base) < 3:
                continue
            fib = lg.fibre(base)
            if fib.self_intersections(cell):
                return True, n_layers
            # ... and a single-layer RETRACE, which rests on itself without ever
            # crossing itself -- the same blind spot `_solve_layer` has
            # (`_self_near`).
            if fib.self_near(cell):
                return True, n_layers
        return False, n_layers
    _, n_layers = _layer_ids(paths, layer_tol=0.5 * diameter)
    if n_layers >= 2:
        return True, n_layers
    if not self_crossings:
        return False, n_layers
    cell = _hash_cell(diameter)
    for p in paths:
        base = _dedupe(np.asarray(p, dtype=np.float64))
        if len(base) < 3:
            continue
        if _self_crosses(base, cell, period, frame=frame):
            return True, n_layers
        # ... and a single-layer RETRACE, which rests on itself without ever
        # crossing itself -- the same blind spot `_solve_layer` has (`_self_near`).
        if _self_near(base, _arc_lengths(base)[0], cell, diameter, period,
                      frame=frame):
            return True, n_layers
    return False, n_layers


def lift_to_underside(paths, path_diam=None, diameter=None, surface=None,
                      point_diam=None):
    """Raise commanded centrelines so each fibre's UNDERSIDE sits on its commanded Z.

    The commanded Z of a layer is the height of the surface that layer lands on --
    `machine_model.Cylinder.seat` states the same datum for the nozzle -- so a tube
    laid there has its CENTRE one radius above it. The sag solver applies that lift
    at its bed datum (`_SagState._solve_layer`'s `bed_at`), because it decides every
    fibre's height anyway. THIS is for the runs that skip the solver -- `--no-sag`,
    and a single layer with nothing to sag onto -- which would otherwise keep the
    commanded Z verbatim and bury the bottom half of every fibre in the plate.

    `path_diam` is the per-path diameter (`None` -> `diameter` for all of them), so
    a multi-scaffold file lifts each part by its own radius. Column 2 is the height
    above the collector in every frame this module works in -- world Z on a plate,
    the wall height `h` on a mandrel -- so one lift serves both. Returns fresh
    arrays; the input is not modified.

    `surface`: `paths` are WORLD points on a five-axis collector (`apply_sag`'s
    `world`), where column 2 is not a height; each point moves one radius along
    the collector's normal at its foot instead.

    `point_diam`: per-fibre PER-POINT diameters parallel to `paths` (a dynamic
    diameter's lookup evaluated at each point's print time). It wins over
    `path_diam`, so every point rests at its OWN radius, as `bed_at` seats it
    when the solver runs -- see `point_diameters`."""
    out = []
    for i, p in enumerate(paths):
        a = np.asarray(p, dtype=np.float64)
        d = (point_diam[i] if point_diam is not None
             else diameter if path_diam is None else path_diam[i])
        if a.size == 0 or d is None or not np.any(d):
            out.append(a)
            continue
        half = 0.5 * np.asarray(d, dtype=np.float64)
        if surface is not None:
            _h, _q, n, _f = surface.height(a)
            out.append(a + (half[:, None] if half.ndim else half) * n)
            continue
        a = a.copy()
        a[:, 2] += half
        out.append(a)
    return out


def point_diameters(ptimes, diam_by_time):
    """Each point's diameter off a (t_knots, d_knots) lookup -- the one the tube
    mesh sizes itself from -- or None without a lookup or times."""
    if diam_by_time is None or ptimes is None:
        return None
    return [np.interp(np.asarray(t, dtype=np.float64),
                      diam_by_time[0], diam_by_time[1]) for t in ptimes]


# --------------------------------------------------------------------------- #
# The sag solve's GEOMETRY, behind one seam
#
# `_SagState._solve_layer` is mostly 1-D along a fibre: supports, the parabola,
# crest rounding, fusion, linking loops, the reset. Only a handful of questions
# need to know WHERE the fibre is -- its arc length, where it crosses the layer
# below or itself, what it rests on, how the collector curves under a span, and
# the polyline it finally emits. Those questions are answered by a per-layer
# geometry object, and there are two:
#
#   `_ChartLayer`   the (u, v, h) engine every flat, mandrel and chart-era
#                   five-axis solve has always used. Its methods are the code
#                   that stood inline in `_solve_layer`, moved and not changed,
#                   so those solves stay byte-identical.
#   `_WorldLayer`   WORLD-NATIVE, for a five-axis collector. Paths are world points
#                   on the collector; a point's height is its distance along the
#                   surface normal; crossings are found in world and tested in
#                   the local tangent plane; the solved height is emitted along
#                   the normal. It asks the collector only through
#                   `frame.surface()` (`mew_surface`), never what shape it is,
#                   so a library dome, an imported dome and a scaffold wrapping
#                   the edge of a block run the same code.
# --------------------------------------------------------------------------- #


class _ChartLayer:
    """One layer's geometry in the (u, v, h) chart: the flat and mandrel engine.

    `frame`/`period`/`fp` are `_SagState`'s; `fusion` is the layer's
    `(angular, fuse_any, fuse_sink, fuse_wall, band, eps_z)`."""

    def __init__(self, frame, period, fp, radius, diameter, sag_multiplier,
                 fusion):
        self.frame = frame
        self.period = period
        self.fp = fp
        self.radius = radius
        self.diameter = diameter
        self.sag_multiplier = sag_multiplier
        self.fusion = fusion
        # Curved collector? Only then do the two geometric terms exist at all --
        # on a plane both metric stretches are 1 and every principal curvature is 0, so the
        # arithmetic would be a no-op, and skipping it outright is what makes
        # the flat solve byte-identical rather than merely equal.
        self.curved = frame is not None and frame.is_curved()
        #: the tie tolerance (floor-peak rule, sample-arc merge): a chart
        #: plateau and a chart arc are bit-exact
        self.tie = 0.0
        # THE CONTACT INDEX'S RULER, and `_local_chart` is the ONE place the
        # condition lives -- `apply_sag` tells `require_chart` which band to
        # apply from the same call, so the gate and the arithmetic cannot
        # disagree about which ruler is in use. None on a plane, a cylinder and
        # a CONE, all of which keep the raw chart distance and with it their
        # digests; a factory per layer because the metric's `a` and `b` are the
        # offset stretch `1 + k*h` and this layer has its own `h`.
        local_of = _local_chart(frame)
        if local_of is not None:
            # Every caller hands this the COMMANDED height, which is the surface
            # the fibre lands on -- not the fibre. Its centreline stands one
            # radius off that surface (`bed_at`), and the offset stretch `1 + k*h`
            # belongs at the fibre, so add the radius here rather than at each of
            # the two call sites (`_arc_lengths` and `local_at`). Term B's own
            # `hmid` needs no such correction: it is read off the SOLVED support
            # heights, which already carry the lift.
            def local_of(h, _f=local_of, _r=radius):
                return _f(np.asarray(h, dtype=np.float64) + _r)
        self.local_of = local_of

    def degenerate(self, base):
        # A fibre with no segment has no droop to solve, but it is still
        # a fibre laid on a surface: `base`'s Z is the height of that
        # surface, so its centreline stands one radius above it, exactly
        # as `bed_at` puts every solved point. Skipping the lift here
        # left a single stray point on the plate per run -- invisible
        # while the whole bottom layer sat there too, and the last thing
        # `underside_clearance_mm` reported once it did not.
        return base + np.array([0.0, 0.0, self.radius])

    def degenerate_height(self, base):
        """`degenerate`'s answer in the SOLVE's currency -- chart Z here, height
        along the normal on a collector. What the visit seam carries forward."""
        return float(base[0, 2]) + self.radius

    def fibre(self, base):
        return _ChartFibre(self, base)


class _ChartFibre:
    """One fibre's geometry in the chart. `cum`/`total` are its arc lengths."""

    def __init__(self, lg, base):
        self.lg = lg
        self.base = base
        self.cum, self.total = _arc_lengths(base, local=lg.local_of)

    def _local_at(self, qa):
        """The chart's metric at these arc positions, or None.

        The wall height comes from the COMMANDED centreline, interpolated
        at the query's own arc position rather than taken as one number
        for the layer: on a curved collector `h` is what the offset
        stretch `1 + k*h` is read at, and a layer that has already
        drooped is still supported by the surface it was commanded onto.
        None on every developable frame, which is what keeps those
        solves byte-identical.
        """
        if self.lg.local_of is None:
            return None
        return self.lg.local_of(np.interp(
            np.asarray(qa, dtype=np.float64), self.cum, self.base[:, 2]))

    def _xy(self, arcs):
        return (np.interp(arcs, self.cum, self.base[:, 0]),
                np.interp(arcs, self.cum, self.base[:, 1]))

    def rest(self, index, arcs, floor, q_radius=None):
        """Rest heights at `arcs` over `index`, no fusion (the floor-peak pass).

        `q_radius`, optional: this fibre's OWN radius at `arcs`, per point, in
        place of the layer's scalar. A contact is `sqrt(s^2 - h^2)` about
        `s = r_query + r_support`, so both radii have to be the local ones or a
        thick stretch rests as if it were thin. None -> the layer scalar, and
        every line below is byte-for-byte the pre-existing solve."""
        fx, fy = self._xy(arcs)
        return index.rest_heights(
            fx, fy, self.lg.radius if q_radius is None else q_radius, floor,
            local=self._local_at(arcs))

    def fused_rest(self, index, arcs, floor, protect=None, q_radius=None):
        """Rest heights against the structure below with fusion ALREADY
        applied -- the one place the fibre's resting floor is decided, so
        the supports and the rest-clamp can never disagree about it.

        One magnitude: the plain max then the scalar sink, byte-for-byte
        the pre-split code. Split: each CANDIDATE is sunk by its own
        crossing-angle blend before the max, so the fibre lands on the
        highest post-fusion contact rather than the geometric winner
        over-sunk.

        `protect` masks query points that must not fuse at all -- the
        linking-loop entry, which merges a full diameter into the layer
        below straight after, so fusing it too would compound the two.

        `q_radius` means what it does in `rest`: this fibre's own per-point
        radius, or None for the layer's scalar."""
        angular, fuse_any, fuse_sink, fuse_wall, band, eps_z = self.lg.fusion
        radius = self.lg.radius if q_radius is None else q_radius
        qx, qy = self._xy(arcs)
        if not angular:
            h = index.rest_heights(qx, qy, radius, floor,
                                   local=self._local_at(arcs))
            if fuse_any and len(h):
                _sink_fused_contacts(h, floor, fuse_sink, eps_z, protect)
            return h
        tx, ty = _tangent_at(self.base, self.cum, arcs,
                             window=self.lg.diameter)

        def pair_sink(q, sdx, sdy):
            L = np.hypot(sdx, sdy)
            ok = L > 0.0
            sin_t = np.where(
                ok, np.abs(tx[q] * sdy - ty[q] * sdx)
                / np.where(ok, L, 1.0), 1.0)
            w = _wall_weight(np.minimum(sin_t, 1.0), band)
            s = fuse_wall * w + fuse_sink * (1.0 - w)
            return s if protect is None else np.where(protect[q], 0.0, s)

        return index.rest_heights(qx, qy, radius, floor,
                                  pair_sink=pair_sink,
                                  local=self._local_at(arcs))

    def crossings(self, prev_layer, with_sin=False):
        # Exact 2D crossings over the layer immediately below: the
        # guaranteed grid contacts, located to full precision.
        return _crossing_contacts(self.base, self.cum, prev_layer,
                                  with_sin=with_sin)

    def self_intersections(self, cell):
        return _self_intersections(self.base, self.cum, cell, self.lg.period,
                                   frame=self.lg.fp)

    def self_near(self, cell):
        """Can this fibre rest on its own earlier self WITHOUT crossing it?

        The retrace gate -- see `_self_near`. Asked on the commanded vertices,
        which is where `_self_contact_pairs` can see an out-and-back: the
        resampled grid the real pass runs over says the same thing, but this
        runs once per fibre and that one does not."""
        return _self_near(self.base, self.cum, cell, self.lg.diameter,
                          self.lg.period, frame=self.lg.fp)

    def near_start(self, arcs):
        """Within a fibre width of the fibre's first vertex (the loop entry)."""
        x, y = self._xy(arcs)
        return (np.hypot(x - self.base[0, 0], y - self.base[0, 1])
                < self.lg.diameter)

    def curved_extra(self, sa, sh, k, a, b, span, hL, hR):
        """The curved-collector terms B and C, as ONE extra midspan
        depth added to the droop the flat law already returned. Both are
        geometry, not physics, and both are span^2, which is why they ride
        the parabola the solver already draws.

        TERM C (the metric, 3.5). The unroll is not isometric: the fibre
        sits on the parallel surface h out along the normal, which is
        stretched by `1 + k_i*h` in each principal direction
        (`mew_frame.metric`). On a CYLINDER k1 = 0, so only the `v`
        component scales and the factor is the old scalar `metric_v`; on
        a dome or a torus BOTH scale, by different amounts. Either way
        the physical span is `span * hypot(m1*cos psi, m2*sin psi)` and
        the correction is the law re-evaluated at that longer span.

        TERM B (normal curvature, 3.4). A span hangs from its supports as a
        CHORD, and on a curved collector a chord already sits below the
        design surface before any droop: `k_n*s^2/8`, with `k_n` Euler's
        theorem in the span's own direction (`mew_frame.chord_drop_at`).
        Maximum along the more curved principal direction, zero along the
        straight one -- on a cylinder that is circumferential and axial,
        the opposite angular signature to the placement deviation, which
        is what lets the two be validated apart.

        THE DIRECTION AND THE PLACE ARE THE SURFACE'S, NOT THE CHART'S.
        Euler wants the angle the fibre runs at ON THE SURFACE and the
        curvature AT THE POINT it is laid, and the chart supplies neither
        for free: `(du, dv)` is sheared wherever the meridian's radius
        varies and stretched differently along the two axes, and the
        frame's `principal_curvatures` is one representative number for
        a whole collector. So the direction handed to term B is `(eu,
        ev)` -- the same vector whose LENGTH gives the physical span
        just above -- and the position `umid` goes with it."""
        frame = self.lg.frame
        cum = self.cum
        base = self.base
        diameter = self.lg.diameter
        sag_multiplier = self.lg.sag_multiplier
        ua = np.interp(a, cum, base[:, 0])
        ub = np.interp(b, cum, base[:, 0])
        va = np.interp(a, cum, base[:, 1])
        vb = np.interp(b, cum, base[:, 1])
        du = ub - ua
        dv = vb - va
        hmid = 0.5 * (hL + hR)
        L = np.hypot(du, dv)
        ok = L > 0.0
        Ls = np.where(ok, L, 1.0)
        # SIGNED cosines: with a shear term BOTH the physical length and
        # the physical direction depend on whether du and dv agree in
        # sign. There is no absolute pair beside these, folded into
        # 0..90 to serve as a helix ANGLE for term B,
        # because a chart angle is not a surface angle -- see
        # `mew_frame.chord_drop_at`, which now takes the direction.
        cu = np.where(ok, du / Ls, 1.0)
        cv = np.where(ok, dv / Ls, 0.0)
        umid = 0.5 * (ua + ub)
        vmid = 0.5 * (va + vb)
        if self.lg.local_of is not None:
            # `cum` is ALREADY a physical arc length here (see
            # `_arc_lengths`), so `span` is the physical span and term C
            # has nothing left to convert -- re-scaling it would apply
            # the metric twice. This is not term C being dropped: it has
            # moved UPSTREAM and is now measured per SEGMENT rather than
            # once across a whole span, which also stops it assuming the
            # metric is constant between two supports. The direction is
            # still needed, because Euler's theorem in term B wants the
            # heading the fibre runs at on the SURFACE -- and it is read
            # off the world endpoints (`_surface_span_dir`), not off the
            # chart step, because the chart's shear `rho'*v/rho`
            # diverges as the meridian reaches the axis and an
            # apex-centred design is exactly the case that walks there.
            eu, ev = _surface_span_dir(frame, ua, va, ub, vb,
                                       umid, vmid, hmid)
            return frame.chord_drop_at(span, eu, ev, umid, hmid,
                                       v=vmid)
        a, b, c = frame.metric(umid, vmid, hmid)
        # ONE direction, used twice. `(eu, ev)` is the step as the
        # surface sees it; its LENGTH scales the span and its ANGLE
        # feeds Euler, so the two cannot disagree about which way the
        # fibre runs. They did, and on a 5 mm mandrel at a 15-layer
        # wall that cost 5.5% of term B at a 45 degree heading.
        eu = a * cu
        ev = b * (cv - c * cu)
        span_p = span * np.hypot(eu, ev)
        return ((_sag_law(span_p, diameter, sag_multiplier)
                 - _sag_law(span, diameter, sag_multiplier))
                + frame.chord_drop_at(span_p, eu, ev, umid, hmid,
                                      v=vmid))

    def extra_arcs(self):
        """Arc positions the emitted polyline needs for the collector's own
        sake. None: a chart polyline is wrapped afterwards (`wrap_path`)."""
        return None

    def self_contact(self, arcs, zo, cell, diameter=None):
        # The same rest-on-the-round-cross-section step as the rest clamp,
        # but against the fiber's OWN earlier strand, which cannot be in
        # `all_lower` (it is this same fiber, mid-solve) and so takes one
        # print-ordered sweep rather than a batch query. Both give the
        # identical z_below + sqrt(s^2 - h^2) drape.
        # `diameter`, optional: this fibre's own PER-POINT deposited diameter at
        # `arcs`, in place of the layer's scalar -- each contact then rests at
        # the sum of the two local radii.
        angular, _fuse_any, fuse_sink, fuse_wall, band, _eps = self.lg.fusion
        xo, yo = self._xy(arcs)
        return _self_contact_heights(
            arcs, xo, yo, zo,
            self.lg.diameter if diameter is None else diameter,
            cell, fuse_sink=fuse_sink,
            fuse_wall=fuse_wall if angular else None,
            band=band if angular else None, period=self.lg.period,
            local=self._local_at(arcs), frame=self.lg.fp)

    def fold_bridge(self, arcs, zo, arc_gap):
        """`_fold_bridge` in the chart: the fold is a flip of the (x, y)
        tangent, which is the plane every contact in this engine is found in."""
        xo, yo = self._xy(arcs)
        return _fold_bridge(arcs, xo, yo, zo, arc_gap)

    def emit(self, arcs, zo):
        xo, yo = self._xy(arcs)
        return np.column_stack([xo, yo, zo])


class _WorldIndex(_SegIndex):
    """`_SegIndex` over WORLD fibre segments on a collector `surface`.

    Segments are stored as they were emitted: world centreline points, which
    ARE the drawn fibre, so a rest height read off them is read off the tube
    that is actually drawn. Each segment is sampled at half a cell of world
    distance and binned by its FOOT on the collector (`surface.settle`), the
    same "drop the depth axis" key a flat index uses for Z: a fibre ten
    layers up bins beside the bare surface point under it.

    WHY THE FOOT AND NOT THE POINT. A rest query is a ray along the normal
    from the foot; the fibre it lands on can be any height above that foot.
    Binning by the point itself would put a high fibre in cells no query from
    the surface looks at.

    REACH. `_hash_cell` guarantees a 3x3x3 block holds every segment within
    `s <= 0.75 * cell` of the query. Feet of points `h` above a surface of
    curvature `k` sit `1/(1 + k*h)` as far apart as the points, so the
    guarantee holds while that factor is inside the 12.5% slack `_hash_cell`
    carries (`|k*h| <= 0.11`: a 1.1 mm wall on an R10 bowl). A CONCAVE CREASE
    between two faces pulls feet apart without bound and is not covered.

    The chart-only machinery of the base class -- period, chart footprint,
    the per-cell height cap -- is never armed here.
    """

    def __init__(self, cell, surface):
        _SegIndex.__init__(self, cell)
        self.surface = surface

    def _keys_along(self, a, b, settled=False):
        """(sid, gx, gy, gz): half-cell world samples of each segment a -> b,
        binned by their foot (or by the sample itself when `settled`)."""
        c = self.cell
        if not len(a):
            e = _EMPTY_IDX
            return e, e, e, e
        n = (np.linalg.norm(b - a, axis=1) / (0.5 * c)).astype(np.int64) + 1
        cnt = n + 1
        sid = np.repeat(np.arange(len(a), dtype=np.int64), cnt)
        k = (np.arange(sid.size, dtype=np.int64)
             - np.repeat(np.cumsum(cnt) - cnt, cnt))
        t = (k / n[sid])[:, np.newaxis]
        f = a[sid] + (b[sid] - a[sid]) * t
        if not settled:
            f = self.surface.settle(f)[0]
        g = np.floor(f / c).astype(np.int64)
        gx, gy, gz = g[:, 0], g[:, 1], g[:, 2]
        keep = np.empty(sid.size, dtype=bool)
        keep[0] = True
        keep[1:] = ((sid[1:] != sid[:-1]) | (gx[1:] != gx[:-1])
                    | (gy[1:] != gy[:-1]) | (gz[1:] != gz[:-1]))
        return sid[keep], gx[keep], gy[keep], gz[keep]

    def add_fiber(self, poly, seg_radius=0.0, settled=False):
        p = np.asarray(poly, dtype=np.float64)
        if len(p) < 2:
            return
        seg = np.concatenate([p[:-1], p[1:]], axis=1)
        if np.ndim(seg_radius) == 0:
            rad = np.full(len(seg), float(seg_radius))
        else:
            r = np.asarray(seg_radius, dtype=np.float64)
            rad = (0.5 * (r[:-1] + r[1:]) if len(r) == len(p)
                   else np.full(len(seg), float(np.mean(r)) if len(r) else 0.0))
        sid, gx, gy, gz = self._keys_along(p[:-1], p[1:], settled=settled)
        self._blocks.append(seg)
        self._rad_blocks.append(rad)
        if len(rad):
            self._max_seg_radius = max(self._max_seg_radius, float(rad.max()))
        self._key_chunks.append(_pack_cells3(gx, gy, gz))
        self._sid_chunks.append(sid + self.n)
        self.n += len(seg)
        self._skey = None

    def _build(self):
        # the per-cell height cap reads a Z column, which world segments do not
        # have; a cap is an optimisation, so it is simply not applied
        cap, self.cap_depth = self.cap_depth, None
        try:
            _SegIndex._build(self)
        finally:
            self.cap_depth = cap

    def foot_pairs(self, Q, k=1):
        """(point id, segment id) candidates for feet `Q` (N, 3), grouped by
        point. The world twin of `_SegIndex.point_pairs`."""
        c = self.cell
        g = np.floor(np.asarray(Q, dtype=np.float64) / c).astype(np.int64)
        keys = _pack_cells3(g[:, 0], g[:, 1], g[:, 2])
        ukeys, first, inv = np.unique(keys, return_index=True,
                                      return_inverse=True)
        cqid, csid = self._pairs_for_cells(
            np.arange(ukeys.size, dtype=np.int64),
            g[first, 0], g[first, 1], g[first, 2], k=k)
        if cqid.size == 0:
            return _EMPTY_IDX, _EMPTY_IDX
        ccnt = np.bincount(cqid, minlength=ukeys.size)
        cstart = np.concatenate([[0], np.cumsum(ccnt)])[:-1]
        order = np.argsort(inv, kind="stable")
        oinv = inv[order]
        pcnt = ccnt[oinv]
        tot = int(pcnt.sum())
        if tot == 0:
            return _EMPTY_IDX, _EMPTY_IDX
        qid = np.repeat(order, pcnt)
        pos = (np.arange(tot, dtype=np.int64)
               - np.repeat(np.cumsum(pcnt) - pcnt, pcnt)
               + np.repeat(cstart[oinv], pcnt))
        return qid, csid[pos]

    def world_seg_pairs(self, A, B):
        """(query segment id, stored segment id) candidates, deduplicated."""
        sid, gx, gy, gz = self._keys_along(np.asarray(A, dtype=np.float64),
                                           np.asarray(B, dtype=np.float64))
        qid, tid = self._pairs_for_cells(sid, gx, gy, gz)
        if qid.size == 0:
            return qid, tid
        pair = _unique_int(qid * np.int64(self.n) + tid)
        return pair // self.n, pair % self.n

    def rest_heights_world(self, Q, N, q_radius, floor, pair_sink=None,
                           _chunk=2_000_000):
        """Highest height ALONG THE NORMAL a fibre of radius `q_radius` rests
        at over the stored segments, for feet `Q` with unit normals `N`;
        never below `floor`. `q_radius` may be a PER-POINT array parallel to
        `Q`, for the same reason and with the same widening rule as the flat
        `rest_heights`.

        The flat query with the local normal standing in for +Z: each
        candidate's closest approach to the ray `Q + t*N` is found in the
        plane perpendicular to `N`, its perpendicular offset `h` gives the
        vertical gap `sqrt(s^2 - h^2)` of two touching tubes, and its
        component along `N` is its own height. `pair_sink(q, D)` is fusion's
        per-candidate sink from the stored segment's world direction `D`."""
        Q = np.asarray(Q, dtype=np.float64)
        N = np.asarray(N, dtype=np.float64)
        floor_a = np.asarray(floor, dtype=np.float64)
        out = (np.full(len(Q), float(floor_a)) if floor_a.ndim == 0
               else np.ascontiguousarray(floor_a, dtype=np.float64).ravel().copy())
        if len(Q) == 0 or self.n == 0:
            return out
        q_rad = np.asarray(q_radius, dtype=np.float64)
        s_max = float(q_rad.max()) + self._max_seg_radius
        k = max(1, int(math.ceil(s_max / self.cell + 0.25)))
        qid, sid = self.foot_pairs(Q, k=k)
        if qid.size == 0:
            return out
        seg = self._seg_array()
        rad = self._rad_array()
        for a in range(0, qid.size, _chunk):
            q = qid[a:a + _chunk]
            sidc = sid[a:a + _chunk]
            A = seg[sidc, 0:3]
            D = seg[sidc, 3:6] - A
            n = N[q]
            aq = A - Q[q]
            aq_par = np.einsum("ij,ij->i", aq, n)
            d_par = np.einsum("ij,ij->i", D, n)
            aqp = aq - aq_par[:, None] * n
            dp = D - d_par[:, None] * n
            L2 = np.einsum("ij,ij->i", dp, dp)
            deg = L2 < 1e-18
            t = np.clip(-np.einsum("ij,ij->i", aqp, dp)
                        / np.where(deg, 1.0, L2), 0.0, 1.0)
            C = aq + D * t[:, None]
            par = np.einsum("ij,ij->i", C, n)
            perp = C - par[:, None] * n
            perp2 = np.einsum("ij,ij->i", perp, perp)
            s = (q_rad[q] if q_rad.ndim else q_rad) + rad[sidc]
            s2 = s * s
            height = np.where(perp2 < s2,
                              par + np.sqrt(np.maximum(s2 - perp2, 0.0)),
                              -np.inf)
            if pair_sink is not None:
                height = height - pair_sink(q, D)
            starts = np.flatnonzero(np.r_[True, q[1:] != q[:-1]])
            red = np.maximum.reduceat(height, starts)
            tq = q[starts]
            out[tq] = np.maximum(out[tq], red)
        return out


def _tangent_basis(n, d):
    """Unit `e1` along `d` with its `n` component removed, and `e2 = n x e1`.
    Where `d` is along `n` any perpendicular is used."""
    dp = d - np.einsum("ij,ij->i", d, n)[:, None] * n
    e1, L = _unit_rows(dp)
    bad = L <= 1e-15
    if np.any(bad):
        ref = np.where(np.abs(n[bad, 0:1]) < 0.9, [[1.0, 0.0, 0.0]],
                       [[0.0, 1.0, 0.0]])
        e1[bad] = _unit_rows(np.cross(n[bad], ref))[0]
    return e1, np.cross(n, e1)


def _unit_rows(v):
    L = np.sqrt(np.einsum("ij,ij->i", v, v))
    return v / np.where(L > 0.0, L, 1.0)[:, None], L


def _plane_hits(a0, a1, b0, b1, n):
    """Do segments a0->a1 and b0->b1 cross when both are projected onto the
    plane perpendicular to `n` (one normal per pair)? `(hit, t, u, sin)` as
    `_seg_hits_xy` plus |sin| of the crossing angle in that plane."""
    e1, e2 = _tangent_basis(n, a1 - a0)
    ra = a1 - a0
    rb0 = b0 - a0
    rb1 = b1 - a0
    ax1 = np.einsum("ij,ij->i", ra, e1)
    ay1 = np.einsum("ij,ij->i", ra, e2)
    bx0 = np.einsum("ij,ij->i", rb0, e1)
    by0 = np.einsum("ij,ij->i", rb0, e2)
    bx1 = np.einsum("ij,ij->i", rb1, e1)
    by1 = np.einsum("ij,ij->i", rb1, e2)
    z = np.zeros_like(ax1)
    hit, t, u = _seg_hits_xy(z, z, ax1, ay1, bx0, by0, bx1, by1)
    dbx = bx1 - bx0
    dby = by1 - by0
    la = np.hypot(ax1, ay1)
    lb = np.hypot(dbx, dby)
    okl = (la > 1e-12) & (lb > 1e-12)
    sin_t = np.where(okl, np.abs(ax1 * dby - ay1 * dbx)
                     / np.where(okl, la * lb, 1.0), 1.0)
    return hit, t, u, sin_t


class _WorldLayer:
    """One layer's geometry on a collector `surface`, in world XYZ.

    A path is a world polyline at its commanded height; `h` is that height
    along the outward normal (`surface.height`). Every solved height is
    `h`-space, exactly as it is `Z` on a plate, and is emitted as
    `foot + h * normal`."""

    #: A span between two supports is a straight chord in space, and on a
    #: curved collector it sits below the surface-following curve the solve
    #: draws. Term B adds that; on a plane its curvature is 0.
    curved = True

    #: The tie tolerance, mm: the floor-peak rule's, and the distance below
    #: which two output sample arcs are one sample. A plateau of the floor read
    #: off the collector carries rounding a chart plateau does not (1e-15 on a
    #: tilted plane); a nanometre is far above that and far below any contact.
    tie = 1e-9

    def __init__(self, surface, radius, diameter, sag_multiplier, fusion):
        self.surface = surface
        self.radius = radius
        self.diameter = diameter
        self.sag_multiplier = sag_multiplier
        self.fusion = fusion

    def degenerate(self, base):
        h, Q, N, _f = self.surface.height(base)
        return Q + (h + self.radius)[:, None] * N

    def degenerate_height(self, base):
        """`_ChartLayer.degenerate_height` on the collector: the height along
        the normal, which is the currency the solve's `zo` is in."""
        h, _Q, _N, _f = self.surface.height(base[:1])
        return float(h[0]) + self.radius

    def fibre(self, base):
        return _WorldFibre(self, base)


class _WorldFibre:
    """One fibre on the collector. `cum` is arc length along its CENTRELINE
    (the commanded path lifted one radius), matching the chart engine's
    `_arc_lengths` at `h + radius`."""

    def __init__(self, lg, base):
        self.lg = lg
        self.base = base
        h, Q, N, F = lg.surface.height(base)
        self.h = h
        self._foot = (Q, N, F)
        c = Q + (h + lg.radius)[:, None] * N
        seg = np.sqrt(np.einsum("ij,ij->i", np.diff(c, axis=0),
                                np.diff(c, axis=0)))
        self.cum = np.concatenate([[0.0], np.cumsum(seg)])
        self.total = float(self.cum[-1])
        self._memo = None

    def _at(self, arcs):
        """World point on the COMMANDED path at each arc position."""
        a = np.atleast_1d(np.asarray(arcs, dtype=np.float64))
        return np.column_stack([np.interp(a, self.cum, self.base[:, i])
                                for i in range(3)])

    def place(self, arcs, memo=True):
        """`(Q, N, F, P)`: foot, outward normal and face under each arc
        position, and the commanded point `P` they were settled from."""
        a = np.atleast_1d(np.asarray(arcs, dtype=np.float64))
        if (memo and self._memo is not None and self._memo[0].shape == a.shape
                and np.array_equal(self._memo[0], a)):
            return self._memo[1]
        P = self._at(a)
        Q, N, F = self.lg.surface.settle(P)
        got = (Q, N, F, P)
        if memo:
            self._memo = (a.copy(), got)
        return got

    def tangent(self, arcs, N):
        """Unit tangent at `arcs`, the chord across one diameter of arc, in the
        tangent plane (the world twin of `_tangent_at(window=diameter)`)."""
        a = np.atleast_1d(np.asarray(arcs, dtype=np.float64))
        half = 0.5 * min(self.lg.diameter, self.total)
        d = (self._at(np.clip(a + half, 0.0, self.total))
             - self._at(np.clip(a - half, 0.0, self.total)))
        d = d - np.einsum("ij,ij->i", d, N)[:, None] * N
        return _unit_rows(d)[0]

    def rest(self, index, arcs, floor, q_radius=None):
        Q, N, _F, _P = self.place(arcs)
        return index.rest_heights_world(
            Q, N, self.lg.radius if q_radius is None else q_radius, floor)

    def fused_rest(self, index, arcs, floor, protect=None, q_radius=None):
        """`_ChartFibre.fused_rest` on the collector; the crossing angle of a
        fusion blend is measured in the tangent plane at the query. `q_radius`
        means what it does there."""
        angular, fuse_any, fuse_sink, fuse_wall, band, eps_z = self.lg.fusion
        radius = self.lg.radius if q_radius is None else q_radius
        Q, N, _F, _P = self.place(arcs)
        if not angular:
            h = index.rest_heights_world(Q, N, radius, floor)
            if fuse_any and len(h):
                _sink_fused_contacts(h, floor, fuse_sink, eps_z, protect)
            return h
        T = self.tangent(arcs, N)

        def pair_sink(q, D):
            n = N[q]
            Dp = D - np.einsum("ij,ij->i", D, n)[:, None] * n
            Dn, L = _unit_rows(Dp)
            ok = L > 0.0
            sin_t = np.where(ok, np.linalg.norm(np.cross(T[q], Dn), axis=1),
                             1.0)
            w = _wall_weight(np.minimum(sin_t, 1.0), band)
            s = fuse_wall * w + fuse_sink * (1.0 - w)
            return s if protect is None else np.where(protect[q], 0.0, s)

        return index.rest_heights_world(Q, N, radius, floor,
                                        pair_sink=pair_sink)

    def crossings(self, prev_layer, with_sin=False):
        """Arc positions where this fibre crosses the layer below, each pair of
        segments tested in the tangent plane at the query segment's middle.
        `with_sin`: also |sin| of each crossing angle, in that plane."""
        base = self.base
        empty = np.zeros(0, dtype=np.float64)
        if prev_layer.n == 0 or len(base) < 2:
            return (empty, empty) if with_sin else empty
        A, B = base[:-1], base[1:]
        qi, si = prev_layer.world_seg_pairs(A, B)
        if qi.size == 0:
            return (empty, empty) if with_sin else empty
        Nm = self.lg.surface.settle(0.5 * (A + B))[1]
        seg = prev_layer._seg_array()[si]
        hit, t, _u, s = _plane_hits(A[qi], B[qi], seg[:, 0:3], seg[:, 3:6],
                                    Nm[qi])
        qh = qi[hit]
        arcs = self.cum[qh] + t[hit] * (self.cum[qh + 1] - self.cum[qh])
        s = s[hit]
        if len(arcs):
            # Chords of this fibre's own path are not crossings
            # (`_same_path`). The layer below stands a height off the
            # collector, so both sides are compared at their feet.
            settle = self.lg.surface.settle
            lo, hi = seg[hit, 0:3], seg[hit, 3:6]
            reach = 2.0 * np.linalg.norm(hi - lo, axis=1) + _SAME_PATH_TOL
            real = ~_same_path(settle(lo)[0], settle(hi)[0],
                               settle(base)[0], self.cum, arcs, reach)
            arcs, s = arcs[real], s[real]
        return (arcs, s) if with_sin else arcs

    def self_intersections(self, cell):
        """`_self_intersections` on the collector: `(arc_lo, arc_hi, sin)`."""
        base = self.base
        if len(base) - 1 < 3:
            return []
        idx = _WorldIndex(cell, self.lg.surface)
        idx.add_fiber(base)
        A, B = base[:-1], base[1:]
        q, s = idx.world_seg_pairs(A, B)
        keep = s < q - 1
        if not keep.any():
            return []
        q, s = q[keep], s[keep]
        Nm = self.lg.surface.settle(0.5 * (A + B))[1]
        hit, t, u, sin_t = _plane_hits(A[q], B[q], A[s], B[s], Nm[q])
        if not hit.any():
            return []
        qh, sh = q[hit], s[hit]
        cum = self.cum
        arc_a = cum[qh] + t[hit] * (cum[qh + 1] - cum[qh])
        arc_b = cum[sh] + u[hit] * (cum[sh + 1] - cum[sh])
        lo = np.minimum(arc_a, arc_b)
        hi = np.maximum(arc_a, arc_b)
        return list(zip(lo.tolist(), hi.tolist(), sin_t[hit].tolist()))

    def self_near(self, cell):
        """The retrace gate (`_self_near`) on the collector.

        Same rule, same geometry as the pass it arms: `_self_pairs` IS this
        class's `_self_contact_pairs`, so asking it on the commanded vertices
        gives the gate and the pass one definition of an admissible contact,
        which is the whole point of the flat version. The fusion arguments are
        the neutral ones -- a sink changes a contact's HEIGHT, never whether
        there is one, and the answer here is only whether there is one."""
        if len(self.base) < 3:
            return False
        return self._self_pairs(self.cum, cell, 0.0, None, None) is not None

    def near_start(self, arcs):
        return (np.linalg.norm(self._at(arcs) - self.base[0], axis=1)
                < self.lg.diameter)

    def curved_extra(self, sa, sh, k, a, b, span, hL, hR):
        """Term B on the collector: the chord between two supports sits
        `k_n * s^2 / 8` below the surface-following curve, with `k_n` the
        normal curvature at the span's middle along the span's own world
        direction (`surface.normal_curvature`), taken on the offset surface
        at the supports' mean height, `k / (1 + k*h)`, as
        `mew_frame.principal_curvatures` does. Term C has nothing to do:
        `cum` is already a physical length."""
        a = np.atleast_1d(np.asarray(a, dtype=np.float64))
        b = np.atleast_1d(np.asarray(b, dtype=np.float64))
        Q, N, F, _P = self.place(0.5 * (a + b), memo=False)
        kn = self.lg.surface.normal_curvature(Q, N, F, self._at(b) - self._at(a))
        hmid = 0.5 * (np.asarray(hL, dtype=np.float64)
                      + np.asarray(hR, dtype=np.float64))
        kn = kn / (1.0 + kn * hmid)
        return 0.125 * kn * np.asarray(span, dtype=np.float64) ** 2

    def extra_arcs(self):
        """Samples that keep the EMITTED polyline on the collector.

        A span too flat to droop gets no interior samples, and in space its
        chord cuts inside a curved collector by `k s^2 / 8`. Sampling at
        `sqrt(8 * tol / k_max)` holds that to `SAG_TOL_D` diameters, the
        tolerance the span sampling already uses; `k_max` is the largest
        principal curvature under the fibre's vertices. None on a plane."""
        Q, N, F = self._foot
        k1, k2, _d1, _d2 = self.lg.surface.curvature(Q, N, F)
        if not len(k1):
            return None
        kmax = float(np.max(np.maximum(np.abs(k1), np.abs(k2))))
        if not kmax > 0.0:
            return None
        step = (math.sqrt(8.0 * SAG_TOL_D * self.lg.diameter / kmax)
                / _SAG_RESOLUTION)
        if step >= self.total:
            return None
        return np.arange(step, self.total, step).tolist()

    def self_contact(self, arcs, zo, cell, diameter=None):
        """`_self_contact_heights` on the collector: each point rests on the
        fibre's own earlier strand, the offset measured in the tangent plane
        at the point, then the shared print-order sweep.

        `diameter`, optional: this fibre's own PER-POINT deposited diameter at
        `arcs`, in place of the layer's scalar -- see `_self_pairs`."""
        angular, _fuse_any, fuse_sink, fuse_wall, band, _eps = self.lg.fusion
        pairs = self._self_pairs(arcs, cell, fuse_sink,
                                 fuse_wall if angular else None,
                                 band if angular else None,
                                 diameter=diameter)
        return _self_contact_apply(pairs, zo, fuse_sink=fuse_sink)

    def _self_pairs(self, arcs, cell, fuse_sink, fuse_wall, band,
                    diameter=None):
        n = len(arcs)
        if n < 3:
            return None
        A = np.asarray(arcs, dtype=np.float64)
        # The layer's scalar, or this fibre's own per-point deposited diameter,
        # split into its two roles exactly as `_self_contact_pairs` splits it:
        # the REACH and the arc-gap window take the maximum (a fat stretch must
        # not lose a candidate a thin one would find), the CONTACT DISTANCE is
        # the sum of the two local radii and so is per pair. A scalar leaves
        # every line below byte-for-byte what it was.
        if diameter is None:
            diameter = self.lg.diameter
        dloc = None if np.ndim(diameter) == 0 else np.asarray(
            diameter, dtype=np.float64)
        d_nom = float(diameter) if dloc is None else float(dloc.max())
        arc_gap = _SELF_CONTACT_ARC * d_nom
        d2 = d_nom * d_nom
        Q, N, _F, _P = self.place(A)
        idx = _WorldIndex(cell, self.lg.surface)
        idx.add_fiber(Q, settled=True,
                      seg_radius=0.0 if dloc is None else 0.5 * dloc)
        qid, sid = idx.foot_pairs(Q, k=1)
        if qid.size == 0:
            return None
        adm = A[sid + 1] <= A[qid] - arc_gap
        qid, sid = qid[adm], sid[adm]
        if qid.size == 0:
            return None
        n_ = N[qid]
        aq = Q[sid] - Q[qid]
        D = Q[sid + 1] - Q[sid]
        aqp = aq - np.einsum("ij,ij->i", aq, n_)[:, None] * n_
        dp = D - np.einsum("ij,ij->i", D, n_)[:, None] * n_
        L2 = np.einsum("ij,ij->i", dp, dp)
        deg = L2 < 1e-18
        t = np.clip(-np.einsum("ij,ij->i", aqp, dp) / np.where(deg, 1.0, L2),
                    0.0, 1.0)
        off = aqp + dp * t[:, None]
        h2 = np.einsum("ij,ij->i", off, off)
        # the contact distance, per pair: the sum of the two local radii (the
        # index carries the earlier strand's, it was built with it)
        if dloc is not None:
            s_pair = 0.5 * dloc[qid] + idx._rad_array()[sid]
            d2 = s_pair * s_pair
        near = h2 < d2
        if not near.any():
            return None
        if np.ndim(d2):
            d2 = d2[near]
        qid, sid, t, h2, dp = qid[near], sid[near], t[near], h2[near], dp[near]
        lift = np.sqrt(np.maximum(d2 - h2, 0.0))
        if fuse_wall is not None and band is not None:
            T = self.tangent(A[qid], N[qid])
            Dn, L = _unit_rows(dp)
            okd = L > 0.0
            sin_t = np.linalg.norm(np.cross(T, Dn), axis=1)
            w = _wall_weight(sin_t, band)
            pair_sink = np.where(okd, fuse_wall * w + fuse_sink * (1.0 - w),
                                 fuse_sink)
        else:
            pair_sink = None
        order = np.argsort(qid, kind="stable")
        return _SelfPairs(qid[order], sid[order], t[order], lift[order],
                          None if pair_sink is None else pair_sink[order])

    def fold_bridge(self, arcs, zo, arc_gap):
        """`_fold_bridge` on the collector: the fold is read off the WORLD
        points the fibre runs through, because that is where its self-contacts
        are found (`_self_pairs`) and so where the unsupportable window is."""
        W = self._at(arcs)
        return _fold_bridge(arcs, W[:, 0], W[:, 1], zo, arc_gap, pos=W)

    def emit(self, arcs, zo):
        Q, N, _F, _P = self.place(arcs)
        return Q + np.asarray(zo, dtype=np.float64)[:, None] * N


def surface_heights(paths, surface):
    """Each world path with its Z column replaced by the height along the
    collector's normal -- the key layer clustering, band splitting and the
    bed datum read, which is world Z only on a plate."""
    out = []
    for p in paths:
        a = np.asarray(p, dtype=np.float64)
        if not len(a):
            out.append(a)
            continue
        out.append(np.column_stack([a[:, 0], a[:, 1],
                                    surface.height(a)[0]]))
    return out


def support_radii(ppd, scalar_radius):
    """Per-fibre support radius a solved layer seeds the lower index with, from
    its per-point deposited diameter `ppd`.

    THE ONE RULE, so the locked export and the live march cannot drift apart:
    `mew_incremental._support_radii` is this function. A fibre whose deposited
    diameter is UNIFORM commits at the single `scalar_radius`, which is an exact
    scalar and keeps every uniform-diameter digest byte-identical; a fibre whose
    diameter genuinely varies along its length commits the per-point profile
    (ppd/2), so a later layer rests on the thin stretch lower than on the thick
    one.

    It is a real distinction, not one without a difference: the deposited diameter is
    mass conservation at the
    LANDING speed (`mew_deposition.diameter_by_time`), so it varies along every
    fibre, and seeding one scalar per layer instead cost 27.0 um -- 1.24
    diameters -- of stack height by layer 9 of the 13-layer sag ladder."""
    out = []
    for d in ppd:
        da = np.asarray(d, dtype=np.float64)
        if da.size and np.all(da == da.flat[0]):
            out.append(scalar_radius)            # uniform: exact scalar
        else:
            out.append(0.5 * da)                 # varies: per-point profile
    return out


class _SagState:
    """Persistent state for the layer-sequential sag solve, factored out of
    `apply_sag` so layers can be fed ONE AT A TIME while the lower-layer spatial
    index stays alive between calls.

    `apply_sag` drives it as a thin batch loop, byte-for-byte its old behaviour.
    `mew_incremental` drives it a layer at a time as the jet lays them: the
    time-marched twin of the batch solve, identical because sagging is causal in
    deposition order (a layer rests only on layers laid before it).

    Pure geometry -- no physics, no VTK, no I/O. A LAYER is the set of fibres at
    one Z, fed in deposition / Z order. `add_layer` solves and commits a fresh
    layer; `reuse_layer` commits an already-solved one verbatim (the warm-start).
    The held `all_lower` cell is sized by the caller to the thickest possible
    fibre, so a later thicker layer never under-reaches a frozen one -- a wider
    cell only costs time, never a result (see `_SegIndex`)."""

    def __init__(self, *, shared_cell, bed_center, max_diam, sag_multiplier,
                 self_crossings, with_times, lower_cap_depth=None,
                 bed_diam_by_time=None, sag_reset_layers=0, frame=None,
                 world=False):
        # The DEPOSITION FRAME (`mew_frame.DepositionFrame`), or None for a flat
        # collector. None takes the flat path untouched, which keeps
        # "mandrel mode off is byte-identical" a property of the code rather than
        # of a test. A CURVED frame changes three things and nothing else: the
        # contact index gets the frame's `v` period (the seam), the droop gets
        # term C's metric so a span measured in `v` is used at its true arc
        # length, and the chord gets term B's normal curvature so it starts below
        # the design surface. See `_solve_layer`'s `parab`.
        self.frame = frame
        # WORLD-NATIVE (`_WorldLayer`): paths are world points on the collector
        # and every geometric question goes to `frame.surface()`. The chart
        # machinery below -- period, footprint frame -- is then never armed.
        self.world = bool(world)
        if self.world and frame is None:
            raise ValueError("a world-native sag solve needs the collector's "
                             "frame")
        self.surface = frame.surface() if self.world else None
        self.period = (None if frame is None or self.world
                       else frame.period_v)
        # A doubly-curved collector (see `_footprint_frame`): every `_SegIndex`
        # this state owns bins by WORLD footprint instead of chart plane, and
        # `self.period` above is already None for it (`RevolutionFrame.period_v`
        # is None off a cylinder), so the two mechanisms never collide.
        self._fp = None if self.world else _footprint_frame(frame)
        self.shared_cell = shared_cell
        self.bed_center = bed_center
        self.max_diam = max_diam
        self.sag_multiplier = sag_multiplier
        self.self_crossings = self_crossings
        self.with_times = with_times
        # Periodic sag reset (see `_maybe_reset`): every Nth committed layer is laid
        # STRAIGHT and becomes the datum the layers above it sag onto, so droop
        # re-accumulates from flat instead of staying at its saturated depth. 0 (the
        # library default) = never reset -> byte-for-byte the old solve.
        self.sag_reset_layers = int(sag_reset_layers or 0)
        self.layers_committed = 0
        # Top of the last committed layer + the radius it supports at, so a reset can
        # re-datum onto the structure below without re-querying the index (see
        # `_maybe_reset`). None until the first commit.
        self.last_top = None
        self.last_radius = None
        # Optional (t_knots, d_knots) diameter-vs-print-time lookup: the BED rest
        # then lifts each bottom-layer point's centreline by its own radius so the
        # fibre UNDERSIDE lands on the plate, keeping undersides co-planar however
        # the diameter varies along a fibre. None -> the plain bed plane,
        # byte-identical. A `_solve_layer` call may override it per layer.
        self.bed_diam_by_time = bed_diam_by_time
        # `lower_cap_depth` (mm): the live march sets this so `all_lower` keeps only
        # the locally-top structure per cell (within this depth of each cell's
        # highest segment) -- bounding the per-query candidate count on a tall stack
        # without dropping an exposed perimeter loop's deep support (see _SegIndex).
        # None (the batch `apply_sag`) keeps every segment -- byte-identical.
        self.lower_cap_depth = lower_cap_depth
        self.all_lower = self._new_index()
        self.all_lower.cap_depth = lower_cap_depth
        self.prev_layer = self._new_index()
        # The previous committed layer's tails, `((x, y, z), ...)`, so the next
        # layer's head can leave from where its filament ended (see
        # `seam_tail_z`). FLAT BED ONLY: on a curved frame (mandrel, collector) the
        # height currency differs (along the normal, with lag and curvature terms)
        # and carrying a tail's height across a ply sank the head into the bare
        # mandrel (`test_mandrel_sag`), so a framed solve carries none.
        self.prev_tails = ()

    @property
    def _flat_bed(self):
        """No frame, or the flat `PlanarFrame` (the same collector spelled as an
        object, which every output must treat byte-identically to None)."""
        return (self.frame is None
                or type(self.frame).__name__ == "PlanarFrame")

    def _new_index(self):
        """An empty contact index in this state's currency."""
        if self.world:
            return _WorldIndex(self.shared_cell, self.surface)
        return _SegIndex(self.shared_cell, period=self.period, frame=self._fp)

    def _layer_geometry(self, radius, diameter, fusion):
        """The per-layer geometry `_solve_layer` asks every WHERE question of."""
        if self.world:
            return _WorldLayer(self.surface, radius, diameter,
                               self.sag_multiplier, fusion)
        return _ChartLayer(self.frame, self.period, self._fp, radius, diameter,
                           self.sag_multiplier, fusion)

    def _top(self, poly):
        """How high a committed fibre stands: its Z, or on the collector its
        height along the normal."""
        if self.world:
            return float(np.max(self.surface.height(poly)[0]))
        return float(np.max(poly[:, 2]))

    def _on_surface(self, poly, rad):
        """The fibre as the WORLD index must see it, on a curved collector.

        A segment straight in `(u, v, h)` is a CURVE in world -- that is
        what `mew_frame.wrap_polyline` exists for -- so the straight world
        chord `_world_aug_array` forms between two stored vertices sits its
        own sagitta INSIDE the collector, and `rest_heights` then measures
        a fibre resting on it as that much lower. It is not a rounding:
        measured on the shipped 10 mm dome at the 0.5 mm sample step the
        five-axis generator writes, a fibre laid directly on another rested
        2.79 um low, which is 0.14 of a 20 um diameter. `densify` at its
        shipped 2 deg takes that to 0.004 um for 3.75x the segments.

        `prev_layer` is deliberately NOT densified: it answers whether two
        chart polylines CROSS, and cutting a segment into collinear pieces
        cannot change that. Only the height query reads world positions.

        None frame (a plate, a mandrel, a cone) returns the input verbatim,
        so every pinned digest is untouched. A WORLD-NATIVE solve returns it
        verbatim too: its index stores world points already and settles each
        one on the collector itself (`_WorldIndex`), so there is no chart
        chord to cut.

        A method rather than `_commit_layer`'s closure because `_solve_layer`'s
        same-layer index needs the identical treatment -- it answers the same
        height query, so it must see the fibre on the same surface.
        """
        if self._fp is None:
            return poly, rad
        import mew_frame
        dense, par = mew_frame.densify(self._fp, poly)
        if np.ndim(rad) != 0:
            rad = np.interp(par, np.arange(len(poly), dtype=np.float64),
                            np.asarray(rad, dtype=np.float64))
        return dense, rad

    def _commit_layer(self, layer_polys, radius, layer_radii=None):
        """Add a finished layer's fibres to the lower-layer indices so the next
        layer rests on them: into `all_lower` (queried by every later layer) and a
        fresh `prev_layer` (the immediate-below grid the crossover finder uses).
        Each fibre is tagged with THIS layer's radius so a later, thinner or
        thicker layer rests on it at the right height. `layer_radii`, when given, is a
        per-fibre PER-POINT radius (parallel to `layer_polys`) so a fibre laid with a
        live diameter sweep supports the next layer at its LOCAL diameter along its
        length; None -> the single scalar `radius`, byte-identical to before."""
        def _rad(j):
            return radius if layer_radii is None else layer_radii[j]

        _on_surface = self._on_surface

        for j, poly in enumerate(layer_polys):
            self.all_lower.add_fiber(*_on_surface(poly, _rad(j)))
        self.prev_layer = self._new_index()
        for j, poly in enumerate(layer_polys):
            self.prev_layer.add_fiber(poly, _rad(j))
        # Book-keeping for the periodic sag reset: the surface a reset re-datums
        # onto -- the highest centreline this layer left behind and the radius it
        # offers contact at (the max over a per-point sweep, so the reset clears
        # the thickest stretch). Kept here, the one place every path funnels
        # through, so the batch driver and the live march count layers identically.
        self.layers_committed += 1
        self.prev_tails = (() if not self._flat_bed else tuple(
            (float(p[-1, 0]), float(p[-1, 1]), float(p[-1, 2]))
            for p in layer_polys if len(p) >= 2))
        if layer_polys:
            top = max(self._top(p) for p in layer_polys)
            self.last_top = top if self.last_top is None else max(self.last_top, top)
            rad = (float(radius) if layer_radii is None
                   else max(float(np.max(r)) for r in layer_radii))
            self.last_radius = rad

    def maybe_reset(self, radius=None):
        """Periodic sag reset: once `sag_reset_layers` layers have been committed
        since the last reset, forget the structure below and re-datum onto its top,
        so the layer about to be solved is laid STRAIGHT and the layers above it sag
        onto it as if it were the plate.

        Real prints show the fibres lying straight again every so many layers, with
        sagging re-accumulating from there. The MECHANISM is not known and nothing
        here claims one: this is a phenomenological period, the same kind of knob
        as `sag_multiplier`. 0 = never reset, byte-for-byte the old solve.

        Geometrically a reset is exactly "make this layer layer 0 again": with both
        indices empty `_solve_layer` finds no crossings, skips the floor-peak pass,
        and `rest_heights` returns the floor verbatim, so the layer comes out flat
        at `bed_center` with no special-casing of the droop. The new datum is the
        top SURFACE of the structure below -- its highest centreline plus the
        radius it offers contact at -- so `bed_center` keeps meaning exactly what
        it means on layer 0: the bare surface a fibre's underside lands on.
        `bed_at` adds this layer's own r_this on top of it, which is why r_this is
        not added here.

        Called by `add_layer` AND by `reuse_layer` -- a REPLAYED layer must reset
        the state too, or the first layer solved after a warm-start prefix would
        rest on pre-reset fibre a cold solve had cleared. Idempotent while nothing
        new is committed. Returns True if it reset.

        `radius` is accepted and ignored: `bed_at` adds r_this on every path, so a reset that
        added it here as well would double it. Kept in the signature because four
        call sites pass it and none of them should have to care."""
        n = self.sag_reset_layers
        if n < 1 or self.layers_committed == 0 or self.layers_committed % n:
            return False
        if self.last_top is None:                # nothing below yet -> already flat
            return False
        self.bed_center = self.last_top + float(self.last_radius)
        self.all_lower = self._new_index()
        self.all_lower.cap_depth = self.lower_cap_depth
        self.prev_layer = self._new_index()
        # "Make this layer layer 0 again" includes the layer-change seam: the
        # layer lies straight on the new datum, not ramped from the old tail.
        self.prev_tails = ()
        return True

    def reuse_layer(self, reused_polys, member_diam, layer_radii=None):
        """Commit a previously-solved (frozen / already-laid) layer verbatim -- no
        re-solve, just seed the indices at this layer's radius. `layer_radii`, when
        given, is a per-fibre support radius (scalar, or PER-POINT for a fibre laid
        with a live diameter sweep) replayed verbatim so the re-seeded support
        matches what the forward march committed; None -> min(member_diam)/2 for
        the whole layer, the byte-identical batch warm-start path.

        Replaying also re-runs the periodic sag reset, so the rebuilt state matches
        a cold solve's at the same point (see `maybe_reset`)."""
        polys = [np.asarray(p, dtype=np.float64) for p in reused_polys]
        self.maybe_reset(min(member_diam, default=self.max_diam) / 2.0)
        if layer_radii is None:
            diameter = min(member_diam, default=self.max_diam)
            self._commit_layer(polys, diameter / 2.0)
        else:
            self._commit_layer(polys, self.max_diam / 2.0, layer_radii=layer_radii)

    def _solve_layer(self, member_paths, member_diam, member_btimes=None,
                     member_loop=None, bed_diam_by_time=None):
        """Solve one layer's fibres against the accumulated structure below WITHOUT
        committing it, and return (out_polys, out_times, layer_polys, radius): the
        per-member sagged (Mi, 3) polylines, the per-member (Mi,) times when this
        state carries times (else None), the non-degenerate subset a commit would
        seed the lower index with, and this layer's fibre radius. The per-fibre
        body is `apply_sag`'s solve verbatim -- supports from the layer-below
        crossings + droop-reachable floor peaks + same-layer self-crossings, the
        parabolic droop, crest rounding, and rest-on-what's-below.

        Split out of `add_layer` so the live time-march can PREVIEW the in-progress
        layer's sag against the frozen structure below without committing it, then
        commit those very polylines when the layer completes (no re-solve).

        A fibre's UNDERSIDE lands on the bed on every path (`bed_at`): the bed
        rest is `bed_center` plus a radius, because `bed_center` is the bare
        SURFACE and a tube resting on a surface has its centre one radius up.

        `bed_diam_by_time` (optional (t_knots, d_knots) diameter-vs-print-time
        lookup) only changes WHICH radius: when given AND this state carries
        times, each point is lifted by its OWN radius at its own print time
        instead of by the one scale this layer was solved at. That keeps a bottom
        layer laid at a per-point diameter co-planar on the plate; without it a
        thicker stretch's underside dips below and a thinner one floats above.
        Only the BED floor is at issue either way -- fibre-on-fibre rests already
        offset by the contact radius."""
        # this layer's true fiber diameter (all its fibers share one scaffold);
        # drives every per-fiber scale below (droop, merge, sampling, contact)
        diameter = min(member_diam, default=self.max_diam)
        radius = diameter / 2.0
        merge_tol = radius                  # merge near-coincident crossings
        # fusion centre-sink for THIS layer's diameter (0 unless the
        # physical-fusion law is installed), applied at the two rest-height twin
        # sites below. `fuse_wall` is the COLLINEAR magnitude; when the calibration
        # splits the two, every contact takes a blend keyed on its own crossing
        # angle. `angular` gates that entire path: with one magnitude nothing here
        # computes an angle and the solve is byte-identical to the pre-split code.
        fuse_sink = _fusion_law(diameter)
        fuse_wall = _fusion_law(diameter, wall=True)
        band = _FUSION_BAND
        angular = band is not None and fuse_wall != fuse_sink
        fuse_any = fuse_sink > 0.0 or fuse_wall > 0.0
        cell = _hash_cell(diameter)         # local (self-contact) query scale
        all_lower = self.all_lower
        # THE LAYER'S OWN EARLIER MEMBERS. In MEW the jet never stops, so the
        # members of one layer are not independent fibres -- they are consecutive
        # stretches of ONE filament (`merge_travels` splices each travel onto the
        # front of the fibre it leads into, so member j+1 starts exactly where
        # member j ended). A later stretch laid over an earlier one is held up by
        # it, which is the same physics `fib.self_contact` already applies WITHIN
        # a member; until this index existed it simply stopped at the member
        # boundary, so two members of one layer could occupy the same space.
        #
        # It costs nothing where members do not overlap: `rest_heights` hands back
        # the floor it was given when no segment is near, and a scaffold layer is
        # ONE serpentine member while a multi-scaffold layer's members are
        # spatially disjoint -- both re-solve bit-identically. It bites on a print
        # whose members really do cross, which is the physics-reshaped calibration
        # ladder: the jet lag bends each wall stroke off its commanded line until
        # consecutive wall visits in one sweep genuinely overlap.
        #
        # `_new_index` gives it this state's own currency, so a world-native solve
        # gets a `_WorldIndex` and the contact is found on the collector.
        same_layer = self._new_index()
        prev_layer = self.prev_layer
        bed_center = self.bed_center
        # per-call lookup wins; else the one bound at construction (the batch driver)
        bed_lut = (bed_diam_by_time if bed_diam_by_time is not None
                   else self.bed_diam_by_time)
        sag_multiplier = self.sag_multiplier
        self_crossings = self.self_crossings
        with_times = self.with_times
        eps_z = 0.02 * diameter
        # THE GEOMETRY. Every question below about WHERE a fibre is -- its arc
        # length, a crossing, what it rests on, the curvature under a span, the
        # polyline emitted -- is put to `lg` / `fib`; everything else here is
        # 1-D along the fibre and shared by both engines (see `_ChartLayer`).
        lg = self._layer_geometry(
            radius, diameter,
            (angular, fuse_any, fuse_sink, fuse_wall, band, eps_z))
        curved = lg.curved
        layer_polys = []
        out_polys = []
        out_times = [] if with_times else None
        # THE VISIT SEAM. In MEW the jet never stops, so member j+1 does not
        # begin a new fibre: `merge_travels` splices the travel onto its front
        # and its first vertex IS member j's last one. That point is one blob of
        # polymer and it is at ONE height, so the new member has to leave from
        # where the old one ended -- neither resting a diameter on top of it
        # (which is why the tail is trimmed out of `same_layer` below) nor
        # dropping through it onto what the previous stroke laid earlier.
        #
        # Left to the ordinary rules it does the second. MEASURED on the
        # reported ladder, at the wall visit x = 38.1084: path 86 ends at
        # z 0.20416 and path 87 opened at 0.18999 -- one fused pitch (0.650 d)
        # lower, which is inside the stroke its own predecessor laid on the way
        # down, and it ran there for ~0.2 mm. 140 of the 220 remaining
        # interpenetration events on that print were this one seam repeated.
        #
        # Carried as the previous member's (input end XY, solved end height):
        # the match is made on the INPUT vertex, because that is where the two
        # members are exactly coincident, while the height comes from the SOLVE
        # and is `zo`, which is the currency the clamp and the supports below
        # are in -- chart Z on a plate, height along the normal on a collector.
        seam_prev = None

        for j, raw in enumerate(member_paths):
            base = _dedupe(np.asarray(raw, dtype=np.float64))
            btimes = None
            if with_times:
                keep = _dedupe_idx(raw)
                btimes = np.asarray(member_btimes[j], dtype=np.float64)[keep]
            if len(base) < 2:
                out_polys.append(lg.degenerate(base))
                if with_times:
                    out_times.append(btimes)
                # a one-point member is still the filament passing through, so
                # it carries the seam forward rather than leaving the previous
                # member's end to match something it does not adjoin
                seam_prev = ((float(base[0, 0]), float(base[0, 1]),
                              lg.degenerate_height(base)) if len(base)
                             else None)
                continue
            is_loop = bool(member_loop[j]) if member_loop is not None else False
            # Continuing the same filament? (see `seam_prev`). A quarter of a
            # diameter is far tighter than any real spacing in a print and far
            # looser than the exact coincidence `merge_travels` produces.
            seam_z = None
            if seam_prev is not None and math.hypot(
                    base[0, 0] - seam_prev[0],
                    base[0, 1] - seam_prev[1]) < 0.25 * diameter:
                seam_z = seam_prev[2]
            # ...or the PREVIOUS LAYER's filament (`seam_tail_z`): a layer change
            # is the same continuous jet, and the earlier layer's tail is below
            # this head, so it would otherwise rest a diameter on it.
            tail_z = None
            if seam_z is None and self._flat_bed:
                tail_z = seam_tail_z(self.prev_tails, base[0, 0], base[0, 1],
                                     diameter)
            fib = lg.fibre(base)
            cum, total = fib.cum, fib.total

            def rad_at(arcs, _bt=btimes, _cum=cum):
                """THIS FIBRE'S OWN RADIUS at these arc positions, in the one
                currency every contact in this solve is measured in.

                A contact is `sqrt(s^2 - h^2)` about `s = r_query + r_support`,
                so both radii have to be the LOCAL ones or a thick stretch rests
                as if it were thin. The deposited diameter is mass conservation
                at the landing speed (`mew_deposition.diameter_by_time`), which
                varies along every fibre -- ~21.8 um on a cruising run against
                ~28.2 um at a 180 degree reversal tip on the reported ladder --
                so "this layer's diameter" is not a radius any contact actually
                has.

                Read off the SAME (print time -> diameter) lookup the tube mesh
                sizes itself from, so the contact and the drawn fibre cannot
                disagree. Without the lookup (or without times) it returns this
                layer's scalar and every line downstream is byte-for-byte the
                pre-existing solve -- which is what keeps a uniform-diameter
                print, and every pinned digest, untouched."""
                if not _bed_active:
                    return radius
                a = np.asarray(arcs, dtype=np.float64)
                t_at = np.interp(a, _cum, _bt)
                return 0.5 * np.interp(t_at, bed_lut[0], bed_lut[1])

            def fused_rest(arcs, floor, protect=None, _f=fib):
                """Rest height against everything already laid: the layers BELOW,
                then this layer's own earlier members.

                Chained rather than merged, because `rest_heights` max-reduces
                against the floor it is handed -- so feeding the lower-layer
                answer in as the same-layer query's floor returns the higher of
                the two contacts, which is the rest height. Empty same-layer index
                -> the first answer verbatim, so a layer of one member (every
                scaffold layer) and a layer of disjoint members (multi-scaffold)
                are both untouched.

                Fusion is applied to the second query exactly as to the first: a
                contact on a same-layer strand sinks into it as one on the layer
                below does, since it is the same polymer meeting at the same
                angle."""
                r = rad_at(arcs)
                h = _f.fused_rest(all_lower, arcs, floor, protect, q_radius=r)
                if not same_layer.n:
                    return h
                return _f.fused_rest(same_layer, arcs, h, protect, q_radius=r)

            def plain_rest(arcs, floor, _f=fib):
                """`fused_rest`'s unfused sibling -- the floor-peak scan, which
                wants WHERE the floor rises, not what a contact settles to.

                A same-layer strand makes a floor peak exactly as a lower-layer
                fibre does, so the droop has to come to rest on it too; its half
                takes the SCALAR sink, not the crossing-angle blend, because
                this scan feeds peak POSITIONS and a blend that varies along the
                fibre moves peaks about rather than only lowering them."""
                r = rad_at(arcs)
                h = _f.rest(all_lower, arcs, floor, q_radius=r)
                if not same_layer.n:
                    return h
                g = _f.rest(same_layer, arcs, h, q_radius=r)
                if fuse_any and len(g):
                    _sink_fused_contacts(g, h, fuse_sink, eps_z, None)
                return g

            # Bed rest level by arc position. `bed_center` is the bare SURFACE the
            # next fibre lands on -- the plate, or a collector at h = 0 -- because
            # that is what the commanded Z of the lowest layer means
            # (`machine_model.Cylinder.seat`: "g-code Z keeps meaning WALL HEIGHT
            # ABOVE THE BARE SURFACE"). A fibre is a tube, so a point resting on
            # that surface has its CENTRELINE one radius above it, and the lift
            # here is what puts the fibre's UNDERSIDE on the bed instead of
            # burying half of it. With a per-point diameter lookup the radius is
            # that point's own, computed on its print time exactly as the tube
            # sizes it; otherwise it is this layer's.
            _bed_active = (bed_lut is not None and with_times
                           and btimes is not None and len(btimes) == len(base))

            def bed_at(arcs, _bt=btimes, _cum=cum):
                if not _bed_active:
                    return bed_center + radius
                a = np.asarray(arcs, dtype=np.float64)
                t_at = np.interp(a, _cum, _bt)          # print time at these arcs
                return bed_center + 0.5 * np.interp(t_at, bed_lut[0], bed_lut[1])

            # --- crossover points (supports) ------------------------------- #
            # (1) Exact crossings over the layer immediately below: the
            #     guaranteed grid contacts, located to full precision.
            cross_arcs, cross_sin = fib.crossings(prev_layer, with_sin=True)
            contacts = cross_arcs.tolist()
            # Each crossing's round top, as a half-width along this fibre (see
            # `_support_caps`); the self-crossings add theirs further down.
            cap_arcs = list(contacts)
            cap_half = (diameter
                        / np.maximum(cross_sin, _CAP_MIN_SIN)).tolist()

            # Resolve contact arc-positions into supports: merge near-coincident
            # ones, anchor the two fiber ends, and read each support's rest height
            # off whatever is actually below it (the possibly-sagged fiber there,
            # or the bed). Commanded Z only orders the layers; using it here would
            # make a fiber float when the one beneath it has sagged. Factored out
            # because it runs twice: first on the guaranteed grid crossings, then
            # again after the droop-reachable floor peaks below.
            def resolve_supports(arcs_in, forced=()):
                sups = [0.0]
                for a in sorted(arcs_in) + [total]:
                    if a - sups[-1] > merge_tol:
                        sups.append(a)
                sups[-1] = total            # snap last support to the fiber end
                sa = np.asarray(sups, dtype=np.float64)
                bed_sa = bed_at(sa)
                # Fusion (opt-in): sink fibre-on-fibre supports so stacked
                # crossover peaks compact, leaving bed contacts -- and, for a loop,
                # the entry (index 0, merged a full diameter below just after this)
                # -- alone so the two lowerings never compound.
                protect = None
                if is_loop and len(sa):
                    protect = np.zeros(len(sa), dtype=bool)
                    protect[0] = True
                sh = fused_rest(sa, bed_sa, protect)
                # Linking-loop entry continuity: a looped fibre's FIRST support is
                # the shared layer-change corner. The loop is the inter-layer
                # transition -- one continuous dragged fibre -- so its entry MERGES
                # with the layer below at that corner instead of resting a full
                # diameter ON TOP of it. Resting on top pins the lead-in one
                # diameter up while the unsupported excursion sags to the plate
                # around it, so the fibre reads as SEVERED at the entry. Dropping
                # the entry support by one diameter makes it continuous with layer
                # n; where the loop RETURNS it then rests on this lowered strand at
                # +1 diameter = layer n+1's height, so deposition rejoins cleanly.
                if is_loop and len(sh):
                    bed0 = bed_sa[0] if np.ndim(bed_sa) else bed_sa
                    sh[0] = max(bed0, sh[0] - diameter)
                # Visit-seam continuity (see `seam_prev`): this member's first
                # support is not resting on anything -- it is the filament still
                # being where it already was -- so it takes the previous
                # member's own solved end height verbatim.
                if seam_z is not None and len(sh):
                    sh[0] = seam_z
                # `forced`: (arc, height) pairs for supports that rest on
                # something NOT in `all_lower` -- a same-layer self-crossing,
                # where the later strand rides on the fiber's own earlier strand.
                # Pin each onto the nearest retained support (the merge may have
                # snapped it a hair) and only ever raise it: the crossing holds
                # the fibre up, never pulls it down.
                for a, h in forced:
                    k = int(np.argmin(np.abs(sa - a)))
                    if h > sh[k]:
                        sh[k] = h
                return sups, sh

            # Supports from the GUARANTEED crossover crossings alone (section 1
            # above), and the free droop they produce. The extra sagging-contacts
            # pass (section 2, after parab) folds in any droop-reachable floor
            # peaks and re-resolves.
            sups, sup_h = resolve_supports(contacts)

            def span_parab(qa, a, b, hL, hR, k=None, depth=None):
                """Parabolic-droop centreline height at arc positions `qa` of
                ONE span from support (a, hL) to (b, hR). `parab` is this over
                the support table; `_tangent_touchdowns` calls it on the
                sub-span it pins, so a sub-span is drawn exactly as a span is.
                `qa=None` returns the span's midspan DEPTH alone; `depth`
                passes one in instead of computing it (see `_relax_pins`)."""
                span = b - a
                if depth is None:
                    depth = span_depth(a, b, hL, hR, span, k)
                if qa is None:
                    return depth
                t = np.where(span > 0.0, (qa - a) / np.where(span > 0.0, span, 1.0),
                             0.0)
                chord = hL * (1.0 - t) + hR * t
                return chord - depth * 4.0 * t * (1.0 - t)

            def span_depth(a, b, hL, hR, span, k=None):
                # Self-weight droop of a bridge grows with the SQUARE of its
                # span (a uniformly loaded strand hangs as a parabola), so the
                # midspan deflection is SAG_COEFF * span**2 / diameter, scaled by
                # the user's sag_multiplier. The droop is NOT clamped: where it is
                # deep enough to reach the structure below, the rest-on-top step
                # after this (maximum(z_par, floor_arc)) stops it there -- a
                # physical contact, not an artificial cap -- and where there is
                # nothing under the span it simply settles onto the bed (the
                # floor). The /diameter keeps the sag-to-span ratio scale-free.
                depth = _sag_law(span, diameter, sag_multiplier)
                if curved:
                    depth = depth + fib.curved_extra(
                        np.asarray(sups, dtype=np.float64),
                        np.asarray(sup_h, dtype=np.float64), k, a, b, span,
                        hL, hR)
                return depth

            def parab(qa):
                """Parabolic-droop centreline height at arc positions `qa`,
                given the supports (`sups` / `sup_h`). Vectorised."""
                sa = np.asarray(sups, dtype=np.float64)
                sh = np.asarray(sup_h, dtype=np.float64)
                k = np.clip(np.searchsorted(sa, qa, side="right") - 1,
                            0, len(sups) - 2)
                return span_parab(qa, sa[k], sa[k + 1], sh[k], sh[k + 1], k)

            # (2) Extra contacts that sagging itself creates: as the fiber droops
            # it can rest on a lower fiber it never crosses in projection, or on
            # one more than a layer down where the immediate layer has a gap.
            # Sample the floor on a fine arc grid (spacing below one fiber width,
            # so no obstacle is stepped over) and take its local PEAKS -- but a
            # peak only becomes a support where the droop actually rests on it.
            # A peak the fiber CLEARS must not be pinned: pinning every peak
            # dragged the bridge down onto whatever sat below it even when it
            # hung clear, and since peak picking is sampling-phase dependent it
            # did so for only a fraction of otherwise-identical pores -- the
            # "second fiber touches the one underneath" banding.
            #
            # "Rests on it" is judged on the CONVERGED droop (`_relax_pins`),
            # not the free one. The free droop of the whole span sinks through
            # every peak under it, and a low peak beside a higher one is then
            # pinned although the fibre, held up by the higher one, never comes
            # down to it: on the sag ladder that pin dragged a fibre 25 um down
            # inside 0.03 mm of its wall.
            if all_lower.n or same_layer.n:
                n_fine = max(2, int(math.ceil(total / merge_tol)))
                fine = np.unique(np.concatenate(
                    [np.linspace(0.0, total, n_fine + 1), cum]))
                bed_fine = bed_at(fine)
                floor_fine = plain_rest(fine, bed_fine)
                # local maxima of the floor (last index of any plateau), above
                # the bed, that the converged droop rests on (pinned, or within
                # `eps_z` of it, as the free-droop test always allowed). Never
                # on a crossing's own round top. `lg.tie` is how
                # far apart two floor samples must be to count as a rise or a
                # fall: 0.0 in the chart, where a plateau is bit-exact, and a
                # nanometre on the collector, where a plateau read off a tilted
                # or curved surface carries 1e-15 of rounding that made every
                # other sample a "peak".
                tie = lg.tie
                rises = np.r_[False, floor_fine[1:] >= floor_fine[:-1] - tie]
                falls = np.r_[floor_fine[:-1] > floor_fine[1:] + tie, False]
                peaks = rises & falls & (floor_fine > bed_fine + eps_z)
                if peaks.any():
                    sa = np.asarray(sups, dtype=np.float64)
                    cp = _support_caps(sa, cap_arcs, cap_half, merge_tol)
                    k = np.clip(np.searchsorted(sa, fine, side="right") - 1,
                                0, len(sa) - 2)
                    obs = ((fine - sa[k] >= cp[k])
                           & (sa[k + 1] - fine >= cp[k + 1]))
                    pinned, chain, _hit = _relax_pins(
                        fine, floor_fine, sa, sup_h, span_parab, obs)
                    peaks &= obs & (pinned | (floor_fine >= chain - eps_z))
                if peaks.any():
                    contacts.extend(float(a) for a in fine[peaks])
                    sups, sup_h = resolve_supports(contacts)

            # --- same-layer self-crossings as supports --------------------- #
            # Where the fibre crosses ITSELF within this layer (turnaround
            # balloons, intersecting linking loops) the later strand is genuinely
            # HELD UP, so that crossing is a support identical to a grid crossing
            # over the layer below and the strand sags to its neighbours through
            # the SAME parabola. Only the support HEIGHT differs: the later strand
            # rides one diameter over the earlier one, so it is pinned to the
            # earlier strand's already-resolved height plus a diameter rather than
            # to the floor. The rest of the path is byte-for-byte the between-layer
            # one: parab -> _round_crests -> rest on the round cross-section below.
            crossings = fib.self_intersections(cell) if self_crossings else []
            # A RETRACE holds the later strand up exactly as a crossing does, and
            # `self_intersections` is blind to one -- collinear segments have a
            # zero determinant and never "cross" (`_self_near`). It earns no entry
            # here, though: a retrace supports the strand along its whole LENGTH,
            # not at a point, so there is no support arc for `resolve_supports` to
            # span between. It arms the rest-clamp further down and nothing else.
            self_supported = bool(crossings) or (
                self_crossings and fib.self_near(cell))
            if crossings:
                lo_arcs = np.array([c[0] for c in crossings], dtype=np.float64)
                hi_arcs = np.array([c[1] for c in crossings], dtype=np.float64)
                # Where the EARLIER (lower) strand rests on the structure below.
                bed_lo = bed_at(lo_arcs)
                # Loop-entry points: the lower strand there MERGES into the layer
                # below (dropped a full diameter just below), so junction fusion must
                # skip them -- the two lowerings must not compound (mirror floor_arc's
                # near_entry protect). None when not a loop.
                near = fib.near_start(lo_arcs) if is_loop else None
                # Fusion (opt-in): the lower strand coalesces INTO the layer
                # beneath it exactly as a span does, so sink its fibre-on-fibre
                # rest BEFORE the self-crossing stacks on it -- the self-crossing
                # twin of the floor_arc / resolve_supports sinks. Without it the
                # inter-layer junction UNDER a self-crossing tower never fused, so
                # the tower compacted at half the grid's rate and the tip peaks
                # stuck up above the fused grid. Bed + loop-entry contacts are left
                # alone; spans are untouched.
                rh_lo = fused_rest(lo_arcs, bed_lo, near)
                z_lo = np.maximum(parab(lo_arcs), rh_lo)
                if is_loop:
                    # A linking loop's lead-in MERGES with the layer below, so the
                    # loop's own RETURN must rest one diameter above THAT merged
                    # height. But all_lower.rest_heights still reports the entry as
                    # resting ON the layer below, a diameter too high, and every
                    # crossover a higher layer makes over this corner inherits the
                    # error. Drop z_lo by a diameter where the lower strand is the
                    # entry corner, mirroring the entry-continuity lowering.
                    bed_lo_near = bed_lo[near] if np.ndim(bed_lo) else bed_lo
                    z_lo[near] = np.maximum(bed_lo_near, z_lo[near] - diameter)
                contacts.extend(float(a) for a in hi_arcs)
                cap_arcs.extend(hi_arcs.tolist())
                cap_half.extend(diameter / max(c[2], _CAP_MIN_SIN)
                                for c in crossings)
                # Fusion: the later strand coalesces into its OWN earlier strand at
                # a self-crossing too, so sink the self-support by the same delta,
                # never below the lower strand. Else the turnaround crossings stay
                # a full diameter up while every grid crossover compacts (raised
                # scaffold edges). `_self_intersections` already reports each
                # crossing's |sin theta|, so the blend needs no direction query.
                # The later strand rides over the earlier one at the SUM OF THE
                # TWO LOCAL RADII, not at one layer diameter. On the reported
                # ladder the two are the same number on a cruising run and 29%
                # apart where a 180 degree reversal tip meets a thin outbound
                # stroke, which is every wall visit.
                self_rest = z_lo + rad_at(lo_arcs) + rad_at(hi_arcs)
                if fuse_any:
                    if angular:
                        sin_x = np.array([c[2] for c in crossings],
                                         dtype=np.float64)
                        w_x = _wall_weight(sin_x, band)
                        sink_x = fuse_wall * w_x + fuse_sink * (1.0 - w_x)
                    else:
                        sink_x = fuse_sink
                    self_rest = np.maximum(z_lo, self_rest - sink_x)
                sups, sup_h = resolve_supports(
                    contacts, zip(hi_arcs.tolist(), self_rest.tolist()))

            # --- output samples: backbone + per-span parabola points --------- #
            # Each span is sampled at a fixed arc step (the well-tested density),
            # BUT a span that does not actually droop is skipped entirely. The free
            # parabola dives SAG_COEFF*mult*span^2/d below the chord, yet the fibre
            # then rests on whatever is beneath, so the EFFECTIVE sagitta is
            # bounded by the drop from the support chord to the bed. Below the
            # sampling tolerance the span lies flat and its linear chord is already
            # exact, so it gets no interior points -- the catastrophic-oversampling
            # fix. Spans that really do sag keep their original density.
            sag_step = (SAG_STEP_D * diameter / math.sqrt(max(1.0, sag_multiplier))
                        / _SAG_RESOLUTION)
            tol = SAG_TOL_D * diameter
            arcset = set(float(c) for c in cum)
            arcset.update(float(a) for a in sups)
            crest_w = np.zeros(max(len(sups), 1), dtype=np.float64)
            for k in range(len(sups) - 1):
                a, b = sups[k], sups[k + 1]
                span = b - a
                # effective droop = min(free droop, drop available down to the
                # bed). Supports rest at sup_h; the parabola can only sink to the
                # structure below, so its visible sagitta never exceeds the
                # support chord's height above the bed. Below `tol` the span is
                # flat to within tolerance -> no interior samples needed.
                free = float(_sag_law(span, diameter, sag_multiplier))
                midchord = 0.5 * (float(sup_h[k]) + float(sup_h[k + 1]))
                bed_mid = bed_at(0.5 * (a + b))
                bed_mid = float(bed_mid.flat[0]) if np.ndim(bed_mid) else float(bed_mid)
                eff = min(free, max(0.0, midchord - bed_mid))
                # How far the CREST reaches into this span (see crest_extent). Both
                # of its supports own that reach, so each keeps the wider of its two
                # sides' spans -- the sampling is then set by the drape actually
                # there, not by whichever neighbour happened to be looked at last.
                w = crest_extent(span, free, eff, diameter,
                                 abs(float(sup_h[k + 1]) - float(sup_h[k])))
                crest_w[k] = max(crest_w[k], w)
                crest_w[k + 1] = max(crest_w[k + 1], w)
                # WARN: skipping a near-flat span can shift a self-contact bump
                # over it by up to ~1 fiber diameter vs sampling it densely.
                if eff <= tol:
                    continue
                # Uniform interior samples across the WHOLE span at sag_step (so
                # the sag valley is always captured), capped for safety.
                n = min(int(SAG_MAX_SPAN_SAMPLES * _SAG_RESOLUTION),
                        max(2, int(round(span / sag_step))))
                for tt in np.linspace(a, b, n + 1)[1:-1]:
                    if a < tt < b:
                        arcset.add(float(tt))

            # Crest detail hugging EVERY support, on BOTH sides (see crest_extent /
            # CREST_STEP_D). The drape belongs to the SUPPORT -- the fibre bends
            # over the one below whether or not the span beyond it droops -- so it
            # is emitted per support, symmetric by construction. Emitted per SPAN
            # instead, a span skipped as flat contributed nothing and its two
            # supports got a crest sampled on ONE SIDE ONLY: the lopsided crest.
            #
            # Reaches at most half way to the neighbouring support, so two adjacent
            # crests meet rather than each sampling past the other's peak.
            if len(sups) >= 2:
                sa = np.asarray(sups, dtype=np.float64)
                step = CREST_STEP_D * diameter / _SAG_RESOLUTION
                # Equal ARC steps: the crest is a parabola of constant curvature
                # 1/round_r, so a uniform step gives every chord the same sagitta.
                # Equal-angle would suit the round cross-section alone, but
                # _round_crests' parabola sits ABOVE that circle everywhere but the
                # apex, so the parabola is the curve actually drawn.
                #
                # The count is PER SIDE, from that side's own reach. One count for
                # the whole fibre doubled the solver's output: every ordinary
                # 3-sample crossover was handed the 10 samples the longest crest
                # had earned. Grouping by count keeps the emission vectorised.
                for w in (np.minimum(crest_w, 0.5 * (sa - np.r_[0.0, sa[:-1]])),
                          -np.minimum(crest_w, 0.5 * (np.r_[sa[1:], total] - sa))):
                    ns = np.clip(np.ceil(np.abs(w) / step), 1,
                                 int(CREST_MAX_SAMPLES * _SAG_RESOLUTION)
                                 ).astype(np.int64)
                    for n in np.unique(ns):
                        sel = ns == n
                        frac = np.arange(1, n + 1, dtype=np.float64) / n
                        cr = (sa[sel, None] - w[sel, None] * frac).ravel()
                        arcset.update(float(t)
                                      for t in cr[(cr > 0.0) & (cr < total)])

            # Where the path crosses ITSELF, drop a fine sample window at each
            # crossing -- on BOTH the earlier and later strand -- so the
            # self-contact sweep can shape the bump over it. Only the crossing
            # neighbourhoods are densified, so a layer that crosses itself at a few
            # turnarounds stays cheap. The lifted region reaches
            # ~diameter/sin(theta) along the strand, so the window scales with
            # 1/sin(theta). Overlapping windows share one grid
            # (`_crossing_window_arcs`).
            if crossings:
                arcset.update(_crossing_window_arcs(
                    crossings, diameter, merge_tol, total).tolist())

            # The collector's own samples (None for the chart engine, whose
            # polyline is wrapped onto the collector afterwards).
            extra = fib.extra_arcs()
            if extra is not None:
                arcset.update(extra)

            arcs = np.array(sorted(a for a in arcset if 0.0 <= a <= total))
            if lg.tie > 0.0:
                # Two sample sources can land on one arc a rounding apart on the
                # collector (1.8e-15 on a tilted plane). Both survive the set, and
                # the emitted fibre then carries a zero-length segment whose
                # direction the NEXT layer's angular fusion reads as a
                # perpendicular crossing: a wall sink instead of a junction sink,
                # 0.2 d too high. Keep the ends, merge the interior within
                # the tie. The chart's tie is 0.0, so its samples are untouched.
                inner = arcs[(arcs > lg.tie) & (arcs < total - lg.tie)]
                if len(inner):
                    inner = inner[np.r_[True, np.diff(inner) > lg.tie]]
                arcs = np.concatenate([[0.0], inner, [total]])

            # Rest the fibre on whatever it crosses over: it follows the lower
            # fibre's circular cross-section (centre offset h -> rest height
            # z_below + sqrt(d^2 - h^2)), so the crossover takes its true shape
            # from the geometry -- a gentle bump at tight stepover, a steep peak at
            # wide stepover -- with no fixed cap flattening it out.
            bed_arcs = bed_at(arcs)
            # Linking-loop entry continuity (the rest-clamp twin of the lowered
            # entry support in resolve_supports): the lead-in sits right over the
            # previous layer's exit CORNER, so the rest-on-top clamp would pin that
            # corner cluster one diameter up and sever it from the layer below.
            # Drop the clamp by a diameter within a fibre width of the entry
            # vertex. The mask is local to that corner, so the outboard excursion
            # and the loop's RETURN clamp normally. Computed FIRST so junction
            # fusion can skip it -- the two lowerings must not compound.
            near_entry = None
            if is_loop and len(arcs):
                near_entry = fib.near_start(arcs)
            # The rest-clamp twin of the seam support above. Within a fibre
            # width of the seam vertex the only thing under this member is its
            # own predecessor's tail, and the strand is already at that height,
            # so the clamp must not push it up onto the stroke below either.
            near_seam = None
            if seam_z is not None and len(arcs):
                near_seam = fib.near_start(arcs)
            # Fusion (opt-in): the rest-clamp twin of the resolve_supports
            # sink -- lower fibre-on-fibre contacts so the crossover peaks settle in,
            # never the bed or the (already-merged) loop entry.
            floor_arc = fused_rest(arcs, bed_arcs, near_entry)
            if near_entry is not None:
                bed_ne = bed_arcs[near_entry] if np.ndim(bed_arcs) else bed_arcs
                floor_arc[near_entry] = np.maximum(
                    bed_ne, floor_arc[near_entry] - diameter)
            if near_seam is not None:
                floor_arc[near_seam] = np.minimum(floor_arc[near_seam], seam_z)
            z_par = parab(arcs)
            # Where the droop reaches a floor with no peak to pin (the bed, or a
            # fibre lying along the span), relax it onto that floor: as many
            # pinned contacts as it takes, each stretch between them a span of
            # the sag law, so it lands tangentially and lies along the floor
            # (`_tangent_touchdowns`). Done on the FREE droop, before the crest
            # rounding and the clamp, which then treat it exactly as they treat
            # any droop. The entry / seam windows step down on purpose and are
            # never pinned on, nor is a crossing's own round top.
            keep = None
            if near_entry is not None or near_seam is not None:
                keep = np.zeros(len(arcs), dtype=bool)
                for msk in (near_entry, near_seam):
                    if msk is not None:
                        keep |= msk
            z_par = _tangent_touchdowns(
                arcs, z_par, floor_arc, sups, sup_h, span_parab, keep,
                _support_caps(sups, cap_arcs, cap_half, merge_tol))

            # Round the FREE (pre-contact) centreline at each support corner into a
            # smooth crest of one fiber-diameter radius. BEFORE resting the fibre
            # on what's below, and that order matters: a dilation can only RAISE
            # points, so rounding after the rest-clamp would re-inflate the contact
            # region and lift the side segments into a wide, shallow cap. Rounding
            # the free curve only smooths the apex; the clamp below then drapes the
            # shoulders down onto the actual fibre.
            z_par = _round_crests(arcs, z_par, CREST_RADIUS_D * diameter)

            zo = np.maximum(z_par, floor_arc)    # rest on it, never sink through

            if self_supported:
                # This is also the ONLY thing a retrace gets (see
                # `self_supported`), and it is enough: every point of the return
                # pass is lifted onto the strand under it, which is what a
                # continuously supported strand does.
                dia_at = rad_at(arcs) * 2.0
                zo = fib.self_contact(arcs, zo, cell, diameter=dia_at)
                # ...and carry it across any 180 degree FOLD, the one window the
                # sweep above cannot support by construction (see `_fold_bridge`).
                # Same arc gap the pair admissibility uses, so this reaches
                # exactly as far as the exclusion that created the hole.
                zo = fib.fold_bridge(
                    arcs, zo, _SELF_CONTACT_ARC * float(np.max(dia_at)))

            if tail_z is not None and len(arcs):
                zo = seam_ramp(zo, np.asarray(arcs) - arcs[0], tail_z, diameter)

            poly = fib.emit(arcs, zo)
            out_polys.append(poly)
            if with_times:
                # `arcs` are positions along the original centreline (the same
                # space `cum`/`btimes` live in), so a straight interp carries
                # the print times onto the resampled, drooped points.
                out_times.append(np.interp(arcs, cum, btimes))
            layer_polys.append(poly)
            # Offer this member to the ones printed after it in the SAME layer,
            # minus its last `_SELF_CONTACT_ARC` of arc. That tail is the junction:
            # member j+1 begins on member j's final vertex, one continuous filament,
            # so a contact there is the strand meeting ITSELF and resting on it
            # would step the fibre a diameter up at the seam. It is the same
            # exclusion `_self_contact_pairs` already applies within a member --
            # a contact must be at least that much arc back along the filament --
            # applied across the member boundary, which is the only place the
            # within-member rule cannot see.
            # This member's own end becomes the next one's seam (see
            # `seam_prev`): the INPUT vertex two members share, and the height
            # the solve just put it at -- `zo`, not the emitted polyline's third
            # column, because on a collector those are different quantities.
            seam_prev = (float(base[-1, 0]), float(base[-1, 1]),
                         float(zo[-1]))
            if len(poly) >= 2:
                # The member and its per-point radius are trimmed TOGETHER, as
                # one (N, 4) array: `_arc_trim_tail` measures on columns 0-2 and
                # interpolates the whole row, so the cut vertex carries the
                # radius the fibre actually has there. A scalar radius rides
                # through unchanged and the trim is byte-for-byte the old one.
                r_poly = rad_at(arcs)
                head = _arc_trim_tail(
                    np.column_stack([poly, np.broadcast_to(
                        np.atleast_1d(r_poly), (len(poly),))]),
                    _SELF_CONTACT_ARC * diameter)
                if len(head) >= 2:
                    same_layer.add_fiber(
                        *self._on_surface(head[:, :3], head[:, 3]))

        return out_polys, out_times, layer_polys, radius

    def add_layer(self, member_paths, member_diam, member_btimes=None,
                  member_loop=None):
        """Solve one layer's fibres against the accumulated structure below, commit
        the layer to the lower-layer indices, and return (out_polys, out_times).
        The whole layer is committed at once so same-layer fibres do not support /
        stack on one another. `member_loop` (optional, parallel to `member_paths`)
        flags fibres that begin with a linking loop, so their entry stays
        continuous with the layer below (see `_solve_layer`); None is byte-for-byte
        the pre-split behaviour."""
        # Periodic sag reset BEFORE the solve, so this layer sees the cleared
        # indices and comes out straight (no-op unless `sag_reset_layers` is set).
        # The radius it re-datums by is this layer's own, derived exactly as
        # `_solve_layer` derives it.
        self.maybe_reset(min(member_diam, default=self.max_diam) / 2.0)
        out_polys, out_times, layer_polys, radius = self._solve_layer(
            member_paths, member_diam, member_btimes, member_loop)
        # Seed the lower index at the radius each point ACTUALLY has, which is
        # what the live march has always done (`mew_incremental._lay_one_layer`)
        # and what this path did not: it committed one scalar per layer, so a
        # later layer crossing a fat reversal tip rested as if the tip were the
        # cruise diameter. The lookup is the same (print time -> diameter) one
        # `bed_at` and the tube mesh read, so the support, the bed rest and the
        # drawn fibre are all the same number. `support_radii` hands back the
        # exact scalar wherever a fibre's diameter does not vary, so a
        # uniform-diameter print re-solves bit-identically.
        layer_radii = None
        lut = self.bed_diam_by_time
        if lut is not None and out_times is not None:
            where = {id(p): k for k, p in enumerate(out_polys)}
            ppd = [np.interp(np.asarray(out_times[where[id(p)]],
                                        dtype=np.float64), lut[0], lut[1])
                   for p in layer_polys]
            layer_radii = support_radii(ppd, radius)
        self._commit_layer(layer_polys, radius, layer_radii=layer_radii)
        return out_polys, out_times

    def commit_solved(self, layer_polys, radius, layer_radii=None):
        """Commit an already-solved layer (the `layer_polys` / `radius` from a prior
        `_solve_layer`) to the lower-layer indices, so the next layer rests on it.
        Lets the live march preview-sag the in-progress layer and then commit those
        very polylines verbatim when it completes -- no second solve. `layer_radii`,
        when given, is a per-fibre PER-POINT radius (parallel to `layer_polys`) so a
        fibre laid with a live diameter sweep supports the next layer at its LOCAL
        diameter; None -> the single scalar `radius`, byte-identical to before."""
        self._commit_layer(layer_polys, radius, layer_radii=layer_radii)


def loop_entry_flags(paths, tol=1e-6):
    """Per-fibre bool: does this fibre START with a layer-change LINKING LOOP?

    A linking loop is a lasso lead-in (`scaffold_gcode._link_loop_points`): the
    fibre leaves the shared layer-change corner, makes a short outboard excursion
    and returns to that SAME corner before deposition begins. So a looped fibre is
    exactly one whose FIRST vertex is revisited early in the path; an ordinary
    serpentine layer never returns to its start. The coordinates are byte-identical
    in the g-code, so an exact (1e-6 mm) match is unambiguous.

    Must be computed on the COMMANDED paths -- the physics deposition transform
    rounds the loop's closure, so after it the fibre no longer returns to its exact
    start. The flag is carried into `apply_sag` to keep the loop's entry continuous
    with the layer below (see the entry-continuity note in `_solve_layer`)."""
    flags = []
    for p in paths:
        p = np.asarray(p, dtype=np.float64)
        flag = False
        if len(p) >= 4:
            sx, sy = float(p[0, 0]), float(p[0, 1])
            # The FIRST exact revisit of the start vertex is the lasso closure; it
            # must leave fibre AFTER it (the deposition pass), so the final vertex is
            # excluded -- that rules out a genuinely closed perimeter (start == end).
            for k in range(2, len(p) - 1):
                if abs(p[k, 0] - sx) < tol and abs(p[k, 1] - sy) < tol:
                    flag = True
                    break
        flags.append(flag)
    return flags


def apply_sag(paths, diameter, *, sag_multiplier=1.0,
              point_times=None, self_crossings=True, path_diam=None,
              reuse_sagged=None, reuse_times=None, frozen=None,
              loop_entry=None, bed_diam_by_time=None, sag_reset_layers=0,
              layer_by_deposition=False, frame=None, progress=None,
              world=False, _banded=False):
    """Return sagged polylines for each input fiber (see the module section above).
    The droop comes from geometry alone -- span between crossover supports and fibre
    diameter, via SAG_COEFF -- with no per-run tuning and no cap: it is bounded by
    the fibre coming to rest on what is below.

    sag_multiplier: the one user control, a dimensionless scale on the droop (the
      output sampling step auto-tightens with it). point_times: parallel (N,) print
      times, resampled onto the sagged points by arc length -> (sagged,
      sagged_times). progress: `(layers_done, layers_total)` once per solved layer,
      HERE because this loop is 75-90 % of a run. self_crossings=False skips the
      same-layer self-contact pass (string art; NO_SELF_CROSS_MARKER).
    reuse_sagged / reuse_times / frozen: incremental warm-start -- the bottom prefix
      of Z-layers whose every member is `frozen` is reused, then indexed so the
      suffix rests on it. BIT-IDENTICAL to a full solve; all None IS a full solve.
    loop_entry: per-path flags from `loop_entry_flags` on the COMMANDED paths; True
      keeps a linking loop's entry support continuous with the layer below.
    bed_diam_by_time: with point_times, lifts each bottom-layer point by its OWN
      radius rather than by this layer's single scale. The underside-on-the-plate
      rest itself is unconditional -- the commanded Z of a layer is the height of
      the surface that layer lands on, so a fibre's centreline comes back one
      radius above it. sag_reset_layers: every Nth
      layer laid STRAIGHT and re-datumed onto -- a period, not a mechanism; 0 =
      never, byte-for-byte the old solve.
    frame: the deposition surface, None for flat; `paths` must then be in its UNROLLED
      (u, v, h). It gives the contact index the `v` PERIOD (without which a tube has
      no contacts and every layer falls through the mandrel), uses each span at its
      TRUE arc length (`mew_frame.metric`, term C) and starts it a chord below the design
      surface (term B). layer_by_deposition clusters layers in g-code order.
    world: `paths` are WORLD points on `frame`'s collector and the solve is
      world-native (`_WorldLayer`): heights along the collector's normal, the
      collector asked only through `frame.surface()`, the result in world. No
      unwrap before and no wrap after. False is the chart engine, unchanged.
    _banded: private -- set only by this function's own layer-spanning
      recursion, whose `paths` are already bands (see below)."""
    # Guard against bad input up front so a caller gets a clear failure instead
    # of an opaque crash deep in the spatial hash (a ~0 cell -> nan -> int64
    # garbage in np.repeat) or an IndexError from a mismatched loop_entry list.
    if not paths:
        return ([], []) if point_times is not None else []
    # A diameter this tiny collapses the spatial-hash cell (_hash_cell) to ~0,
    # which becomes NaN/garbage int64 sizes deep in np.repeat rather than a clean
    # error -- so the floor is not just "<= 0" but "too small to be a physically
    # meaningful fibre diameter".
    if not (diameter > 1e-9):
        raise ValueError("diameter must be positive (and not vanishingly small)")
    if loop_entry is not None and len(loop_entry) != len(paths):
        raise ValueError("loop_entry must have one flag per path")
    # Per-path fiber diameter. Each scaffold may stack at its OWN diameter, and
    # the solve is layer-sequential with every layer belonging to a single
    # scaffold, so droop, contact and sampling scales are taken per layer from the
    # true diameter there. None -> the scalar `diameter` for every fiber.
    if path_diam is None:
        path_diam = [diameter] * len(paths)
    else:
        path_diam = [(float(d) if d and float(d) > 0 else diameter)
                     for d in path_diam]
    min_diam = min(path_diam) if path_diam else diameter
    max_diam = max(path_diam) if path_diam else diameter

    # THE CHART HAS TO BE A RULER WHERE THE PATTERN GOES. Every length this
    # solver forms goes through `mew_frame.metric` per point and is exact in
    # any chart; the raw chart distances were `_SegIndex`'s binning and the
    # contact measurement it feeds, and a chart that overstates THOSE drops a
    # contact silently. Slice 25 split those two apart, so what this gate asks
    # now depends on which ruler the frame gets (`_local_chart`):
    #
    #   a DEVELOPABLE frame -- a plane, a cylinder, a cone -- measures raw chart
    #     deltas, exactly as before, so it is still held to the index's own
    #     12.5% slack (`_hash_cell` is 1.5*d and the 3x3 guarantee needs
    #     d <= 0.75*cell) at the index's own scale, which is the coarsest fibre
    #     present -- the same `max_diam` the shared cell is sized to just below.
    #     Slice 7 measured the global chart inside that band by 8x everywhere
    #     but the axis of revolution.
    #   a DOUBLY-CURVED frame measures through the local chart, which is exact
    #     to 0.0003% at contact scale however sheared the global one is, and
    #     widens its candidate block to match. For it the shear is a COST and
    #     the band is `CHART_COST_BAND`. That is what lets a dome and a bowl
    #     through: the shipped default reads 66.9%, refused as an accuracy
    #     failure and affordable as a cost.
    #
    # The POLE exclusion is unchanged either way -- there the local chart's own
    # Jacobian is singular. It refuses the disc, not the whole surface.
    # AND ONLY WHEN `paths` REALLY CARRIES CHART COLUMNS. The question this
    # gate asks is whether the chart is a good enough RULER for the deltas the
    # index will measure in it -- which is a question about `(u, v, h)`. A
    # frame whose read-back is WORLD hands this three columns of collector XYZ,
    # and reading those as `(u, v, h)` is not a loose gate, it is a different
    # surface: on the imported flat-topped block it asked the chart for
    # `u = 9.699`, a world X on a design whose chart spans -4.5 to 4.38, and
    # the ray from there misses the collector entirely -- exit 2, with no STL.
    # `sag_approx.holds_chart` is the ONE place that answers the currency
    # question, and `mew_frame.DepositionFrame.chart_currency` carries both the
    # reasoning and the identical defect on the cone. A doubly-curved import
    # never reached here (`_footprint_frame` is armed for it), which is why
    # this hid behind the dome for two sessions.
    #
    # Imported HERE and not at module scope for `apply_sag_approx`'s reason:
    # `sag_approx` imports this module for its laws, so a top-level import
    # would be a cycle.
    import sag_approx
    # World-native keys layers, bands and the bed on the height ALONG THE
    # NORMAL, which is world Z only on a plate.
    keyed = paths
    if world:
        if frame is None:
            raise ValueError("world=True needs the collector's frame")
        keyed = surface_heights(paths, frame.surface())
    if (not world and frame is not None and _footprint_frame(frame) is None
            and sag_approx.holds_chart(frame)):
        # A doubly-curved frame is skipped here ENTIRELY, not merely widened
        # to a different band: `_footprint_frame` being armed means `_SegIndex`
        # is no longer measuring through the `(u, v)` chart at all -- binning by
        # WORLD footprint and, in `rest_heights`, the exact WORLD distance
        # (`_world_aug_array`) -- so there is no chart accuracy question left
        # to gate, and a POLE EXCLUSION raised here for an
        # apex-centred design would refuse a design the index now handles
        # correctly. A developable frame -- a plane, a cylinder, a cone --
        # still measures raw chart deltas exactly as before, so it keeps this
        # gate unchanged.
        _pts = np.concatenate([np.asarray(p, dtype=np.float64) for p in paths])
        frame.require_chart("the exact sag solver", _pts[:, 0], max_diam,
                            v=_pts[:, 1], h=_pts[:, 2], ruler="raw")

    # A fibre that walks through several layers would be assigned WHOLE to one of
    # them (median Z), collapsing the stack -- see `_split_layer_spanning`. Cut it
    # into Z-bands, solve those as the separate layers they are, re-join on the way
    # out. Returns None when nothing spans, which is every scaffold this program
    # generates.
    #
    # NOT attempted under the incremental warm-start: `frozen` / `reuse_sagged` are
    # indexed per INPUT fibre and the frozen set must stay a deposition-time
    # prefix, neither of which survives re-indexing onto bands. A live-mode re-lay
    # of a layer-spanning import therefore still collapses.
    if frozen is None and reuse_sagged is None and not _banded:
        _subs, _owner, _stimes = _split_layer_spanning(
            paths, 0.5 * min_diam, point_times,
            band_key=keyed if world else None)
        if _subs is not None:
            # `_banded` stops the recursion at one level. It must not rest on
            # the split being a no-op the second time: a band holds every point
            # within layer_tol of its FIRST point, so it can span 2 * layer_tol
            # (a layer hop that dips below its start, then climbs), the span
            # test fires again, cuts identically, and recursed until
            # RecursionError (the r10 library dome at its apex, 12 x 12 mm).
            # loop_entry marks a fibre's ENTRY, so only its first band inherits it.
            _first = set()
            _le = None
            if loop_entry is not None:
                _le = []
                for j, o in enumerate(_owner):
                    _le.append(bool(loop_entry[o]) and o not in _first)
                    _first.add(o)
            _res = apply_sag(
                _subs, diameter, sag_multiplier=sag_multiplier,
                point_times=_stimes, self_crossings=self_crossings,
                path_diam=[path_diam[o] for o in _owner],
                loop_entry=_le, bed_diam_by_time=bed_diam_by_time,
                sag_reset_layers=sag_reset_layers,
                layer_by_deposition=layer_by_deposition, frame=frame,
                progress=progress, world=world, _banded=True)
            if point_times is not None:
                _sp, _st = _res
                _sp = _seat_lone_bands(_subs, _sp, _owner, frame=frame)
                return (_rejoin_split(_sp, _owner, len(paths)),
                        _rejoin_split(_st, _owner, len(paths)))
            return _rejoin_split(_seat_lone_bands(_subs, _res, _owner, frame=frame),
                                 _owner, len(paths))

    # Shared lower-layer indices serve queries from EVERY layer, so their cell must
    # reach the largest possible contact (the thickest scaffold); a too-small cell
    # could miss a far one. Sized to max_diam this only widens cells when the
    # diameters differ.
    shared_cell = _hash_cell(max_diam)

    # Cluster into layers at the FINEST scale so a thin scaffold's closely spaced
    # layers are never swallowed by a coarser scaffold's tolerance.
    _layers_fn = _layer_ids_deposition if layer_by_deposition else _layer_ids
    fiber_layer, n_layers = _layers_fn(keyed, layer_tol=0.5 * min_diam)
    bed_center = min(float(np.median(p[:, 2])) for p in keyed)

    state = _SagState(shared_cell=shared_cell, bed_center=bed_center,
                      max_diam=max_diam, sag_multiplier=sag_multiplier,
                      self_crossings=self_crossings,
                      with_times=point_times is not None,
                      bed_diam_by_time=bed_diam_by_time,
                      sag_reset_layers=sag_reset_layers, frame=frame,
                      world=world)

    sagged = [None] * len(paths)
    sagged_times = [None] * len(paths) if point_times is not None else None

    # Incremental warm-start (see the docstring). Needs the cached geometry, and
    # times when point_times is on; without them reuse_until stays 0 = full solve.
    layers_members = [[i for i in range(len(paths)) if fiber_layer[i] == li]
                      for li in range(n_layers)]
    reuse_until = 0
    if (frozen is not None and reuse_sagged is not None
            and (point_times is None or reuse_times is not None)):
        for li in range(n_layers):
            if layers_members[li] and all(frozen[i] for i in layers_members[li]) \
                    and all(reuse_sagged[i] is not None for i in layers_members[li]):
                reuse_until = li + 1
            else:
                break

    # Drive the per-layer solve through `_SagState` (the persistent stepper), in Z
    # order. The warm-start reuses the frozen bottom prefix verbatim; every other
    # layer is solved against the accumulated structure below.
    for li in range(n_layers):
        members = layers_members[li]
        member_diam = [path_diam[i] for i in members]
        if li < reuse_until:                 # warm-start: take the cached solve
            reused = [np.asarray(reuse_sagged[i], dtype=np.float64)
                      for i in members]
            state.reuse_layer(reused, member_diam)
            for k, i in enumerate(members):
                sagged[i] = reused[k]
                if sagged_times is not None:
                    sagged_times[i] = np.asarray(reuse_times[i], dtype=np.float64)
            continue
        member_paths = [paths[i] for i in members]
        member_btimes = ([point_times[i] for i in members]
                         if point_times is not None else None)
        member_loop = ([bool(loop_entry[i]) for i in members]
                       if loop_entry is not None else None)
        out_polys, out_times = state.add_layer(member_paths, member_diam,
                                               member_btimes, member_loop)
        for k, i in enumerate(members):
            sagged[i] = out_polys[k]
            if sagged_times is not None:
                sagged_times[i] = out_times[k]
        if progress is not None:
            progress(li + 1, n_layers)

    return (sagged, sagged_times) if point_times is not None else sagged



# --- the sag / drawn-curve split -------------------------------------------- #

def split_for_sag(paths, ptimes, sag_grid):
    """`(solve_paths, solve_times)` -- the subset of each fibre the sag solve runs on.

    `sag_grid` is `mew_deposition`'s per-fibre SEED time grid (published as the
    installed transform's `sag_grid`). Refining is add-only, so the seed grid is a
    strict subset of the laid grid and this is a pure selection: no interpolation,
    no new geometry. `None`, a length mismatch, or a fibre whose grid is not a
    subset falls back to the fibre itself, so a caller can always call this.
    """
    if not sag_grid or len(sag_grid) != len(paths):
        return list(paths), list(ptimes)
    out_p, out_t = [], []
    for p_, t_, g in zip(paths, ptimes, sag_grid):
        t_ = np.asarray(t_, dtype=np.float64)
        if g is None or len(g) >= len(t_):
            out_p.append(p_)
            out_t.append(t_)
            continue
        idx = np.searchsorted(t_, np.asarray(g, dtype=np.float64))
        idx = np.clip(idx, 0, len(t_) - 1)
        if not np.array_equal(t_[idx], np.asarray(g, dtype=np.float64)):
            out_p.append(p_)                 # not a subset -- do not guess
            out_t.append(t_)
            continue
        out_p.append(np.asarray(p_, dtype=np.float64)[idx])
        out_t.append(t_[idx])
    return out_p, out_t


def carry_sag_onto_drawn(sagged, sagged_times, drawn, drawn_times, surface=None):
    """Merge a sag solved on the SEED grid onto the fibre the corner grid DREW.

    Sagging "only moves points in Z and adds samples along the existing centreline"
    (see `apply_sag`), so the two sides are two samplings of one curve on one
    clock: take the union of their times, read XY off the drawn fibre and Z off the
    sag.

    The union is the rule that works, measured. Keeping the SAG solver's own
    sampling and only re-assigning its XY leaves the drawn chord at 0.395 fibre
    diameters, barely better than the 0.382 of not refining at all, because that
    sampling is span-driven and puts no sample at a corner vertex; the union reads
    0.109, the same as sagging the refined fibre directly.

    `surface`: both sides are WORLD points on a five-axis collector, where the
    sag moves a point along the collector's normal rather than in Z. The drawn
    fibre gives the foot and the normal, the sagged fibre gives the height.
    """
    out_p, out_t = [], []
    for sp, st, dp, dt in zip(sagged, sagged_times, drawn, drawn_times):
        sp = np.asarray(sp, dtype=np.float64)
        dp = np.asarray(dp, dtype=np.float64)
        st = np.asarray(st, dtype=np.float64)
        dt = np.asarray(dt, dtype=np.float64)
        if len(sp) < 2 or len(dp) < 2:
            out_p.append(sp)
            out_t.append(st)
            continue
        grid = np.union1d(st, dt)
        if surface is not None:
            dg = np.column_stack([np.interp(grid, dt, dp[:, k])
                                  for k in range(3)])
            _hd, q, n, _f = surface.height(dg)
            hs = np.interp(grid, st, surface.height(sp)[0])
            out_p.append(q + hs[:, None] * n)
            out_t.append(grid)
            continue
        out_p.append(np.column_stack([
            np.interp(grid, dt, dp[:, 0]),   # XY from the fibre that was drawn
            np.interp(grid, dt, dp[:, 1]),
            np.interp(grid, st, sp[:, 2])]))  # ... Z from the fibre that was sagged
        out_t.append(grid)
    return out_p, out_t

def apply_sag_approx(paths, diameter, *, sag_multiplier=1.0, point_times=None,
                     path_diam=None, sag_reset_layers=0, frame=None,
                     progress=None):
    """`apply_sag`'s signature over the APPROXIMATE engine (`sag_approx.drape_sag`).

    The "Sag solver: approximate" choice. Same physics, same droop law, same fusion
    law -- contacts found analytically between commanded-centreline segment pairs
    instead of by sampling the floor every half diameter, which is where the ~5x
    comes from. `sag_approx`'s module docstring lists the bounded departures;
    `sag_bench.py compare sag_approx` measures them against `apply_sag`, the
    authority. Returns the same shape `apply_sag` does.

    This is the seam that keeps `sag_approx` free of the physics: it takes the fusion
    sinks as NUMBERS, from the same `_fusion_law` / `_FUSION_BAND` globals `_SagState`
    reads, at the same diameter. The droop law needs no argument (`sag_law=None` falls
    back to `_sag_law`) and there is deliberately no way to pass one in: different
    laws on the two sides are their largest source of disagreement.

    `path_diam` is reduced to its MINIMUM, matching the single scale `_SagState` picks
    per layer, so a multi-scaffold file whose parts differ in diameter is drooped at
    the finest one. Both solvers rest the bottom layer's UNDERSIDE on the plate, so
    they put the stack at one height. `frame` carries a curved surface in:
    on a mandrel it uses the exact solver's periodic seam index for candidate lookup.

    THE DYNAMIC-DIAMETER CASE IS THE WEAK ONE, measured: mean Z agrees to 0.35 um
    with a uniform diameter and to ~0.25 d once `--dynamic-diameter` varies it,
    because the per-point pitch and droop cannot be expressed as the single scale
    `drape_sag` takes. `main` prints a note; Exact remains the final-export answer."""
    # Imported HERE, not at module scope: `sag_approx` imports this module for its
    # laws, so a top-level import would be a cycle -- and this way the exact path
    # (the default) never pays for a module it does not use.
    import sag_approx

    # THE CURRENCY RULE. `frame` says what `paths`' three columns mean, and the
    # discriminator is `sag_approx.holds_chart` -- does the READ-BACK hand
    # chart columns? None (a flat bed) and a four-axis CYLINDER do, because
    # `parse_gcode` unrolled them; everything else is world XYZ in the
    # collector's own frame and is converted here.
    #
    # The test is not `developable`, which is a fact about the
    # SURFACE and agreed with this one only because a cylinder happens to be
    # both. A CONE is developable and does NOT unroll, so its chart-space
    # columns were handed to `drape_sag` unconverted while `GlobalChart`'s
    # from_world was the identity -- two wrongs that cancelled here and did
    # not cancel in `mew_deposition`.
    #
    # Converted HERE rather than at the caller, so the EXACT solver's currency
    # is untouched and the two seams stay one line each. Both directions are
    # exact: `to_world` is the chart's own forward map, and `unwrap_path`'s
    # round trip is 3.85e-15 mm on an apex-centred design since the period
    # bookkeeping was fixed.
    # The result comes back in the chart because `main` and
    # `build_deposition_mesh` still hand it to `mew_frame.wrap_path`.
    _world = frame is not None and not sag_approx.holds_chart(frame)
    _band_key = None
    if _world:
        import mew_frame
        # Keep the PRE-conversion, chart-space paths as the layer-height
        # currency `_split_layer_spanning` wants: WORLD Z is not
        # deposition-layer height on a curved collector, so keying the band
        # split on it (rather than on chart `h`) cut one true layer into
        # hundreds of one-point bands that `drape_sag` then skipped unsolved.
        _band_key = paths
        paths = [np.column_stack(frame.to_world(p[:, 0], p[:, 1], p[:, 2]))
                 for p in paths]

    d = min(path_diam) if path_diam else diameter
    # Same layer-spanning cut the exact solver makes, for the same reason:
    # `drape_sag` clusters through `_layer_ids_deposition`, so a welded fibre
    # collapses the stack here too. Applied at THIS seam rather than inside
    # `sag_approx`, so the two solvers keep taking identical geometry.
    # `band_key` keeps the split decision in the ORIGINAL (chart-space)
    # currency even though `paths` itself is now world -- see its docstring.
    _subs, _owner, _stimes = _split_layer_spanning(paths, 0.5 * d, point_times,
                                                    band_key=_band_key)
    if _subs is not None:
        _sp, _st = sag_approx.drape_sag(
            _subs, _stimes, d, sag_multiplier=sag_multiplier,
            sag_reset_layers=sag_reset_layers,
            fuse_sink=_fusion_law(d), fuse_wall=_fusion_law(d, wall=True),
            band=_FUSION_BAND, frame=frame, progress=progress)
        sagged = _rejoin_split(_seat_lone_bands(_subs, _sp, _owner, frame=frame), _owner,
                               len(paths))
        sagged_times = (_rejoin_split(_st, _owner, len(paths))
                        if point_times is not None else _st)
        if _world:
            sagged = [mew_frame.unwrap_path(frame, p) for p in sagged]
        return (sagged, sagged_times) if point_times is not None else sagged
    sagged, sagged_times = sag_approx.drape_sag(
        paths, point_times, d,
        sag_multiplier=sag_multiplier,
        sag_reset_layers=sag_reset_layers,
        fuse_sink=_fusion_law(d), fuse_wall=_fusion_law(d, wall=True),
        band=_FUSION_BAND, frame=frame, progress=progress)
    if _world:
        sagged = [mew_frame.unwrap_path(frame, p) for p in sagged]
    return (sagged, sagged_times) if point_times is not None else sagged


# --------------------------------------------------------------------------- #
# Deposition mesh (sagged fibers tagged with print time, for the animator)
# --------------------------------------------------------------------------- #
def _resample_by_time(points, times, max_dt):
    """Insert points along a fiber so no two consecutive points are more than
    `max_dt` print-seconds apart, interpolating BOTH position and time.

    The deposition animation reveals the mesh one ring (= one fiber point) at a
    time, so the trail's update period is the time gap between rings. Sagging
    subdivides fibers densely wherever they cross what is below, but the bottom
    layer crosses nothing, so a long straight pass there becomes a single ring and
    the trail jumps a whole pass at a time. Resampling by TIME gives every fiber a
    uniform update period regardless of layer, feedrate or pass length. Original
    points are kept, so this only ADDS detail."""
    pts = np.asarray(points, dtype=np.float64)
    t = np.asarray(times, dtype=np.float64)
    if len(pts) < 2 or max_dt <= 0 or t[-1] <= t[0]:
        return pts, t
    grid = np.arange(t[0], t[-1], max_dt)
    nt = np.unique(np.concatenate([t, grid]))   # keep originals, fill the gaps
    # position is interpolated against time (monotonic within a fiber), so the
    # added points land exactly where the head is at each instant
    nx = np.interp(nt, t, pts[:, 0])
    ny = np.interp(nt, t, pts[:, 1])
    nz = np.interp(nt, t, pts[:, 2])
    return np.column_stack([nx, ny, nz]), nt


def _hex_to_rgb255(s):
    """'#rrggbb' or 'rrggbb' -> (r, g, b) ints in 0..255, or None if malformed."""
    if not isinstance(s, str):
        return None
    s = s.strip().lstrip("#")
    if len(s) != 6:
        return None
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return None


def path_colors_from_groups(groups, n_paths, default_rgb):
    """Map each of `n_paths` g-code paths (one per layer, in g-code order) to its
    scaffold's RGB colour from a --scaffold-groups list (each entry may carry a
    hex "color"). Returns a list of (r,g,b) uint8 tuples of length n_paths, or None
    when colourisation does not apply -- `groups`/`default_rgb` falsy, layer counts
    that do not sum to n_paths, or no group carrying a usable colour -- so the
    caller keeps its single solid colour. A group without a valid colour uses
    `default_rgb`."""
    if not groups or default_rgb is None:
        return None
    try:
        if sum(int(g["layers"]) for g in groups) != n_paths:
            return None
    except (KeyError, ValueError, TypeError):
        return None
    grp_rgb = [_hex_to_rgb255(g.get("color")) for g in groups]
    if not any(c is not None for c in grp_rgb):
        return None                         # nothing to colour -> stay solid
    out, gi, left = [], 0, int(groups[0]["layers"])
    for _ in range(n_paths):
        while left <= 0 and gi + 1 < len(groups):
            gi += 1
            left = int(groups[gi]["layers"])
        out.append(grp_rgb[gi] if grp_rgb[gi] is not None else tuple(default_rgb))
        left -= 1
    return out


def path_diameters_from_groups(groups, n_paths, default_diam):
    """Map each of `n_paths` g-code paths (one per layer, in g-code order) to its
    scaffold's fiber_diameter from a --scaffold-groups list (each entry a dict
    with "layers" and optional positive "fiber_diameter"). Returns a list of
    length n_paths, or None when `groups` is falsy OR its layer counts don't sum
    to n_paths -- so callers fall back to the uniform `default_diam` exactly as
    before. A group without a positive fiber_diameter also falls back per path."""
    if not groups:
        return None
    try:
        if sum(int(g["layers"]) for g in groups) != n_paths:
            return None
        out = []
        gi, left = 0, int(groups[0]["layers"])
        for _ in range(n_paths):
            while left <= 0 and gi + 1 < len(groups):
                gi += 1
                left = int(groups[gi]["layers"])
            try:
                gd = float(groups[gi].get("fiber_diameter", 0.0))
            except (TypeError, ValueError):
                gd = 0.0
            out.append(gd if gd > 0.0 else default_diam)
            left -= 1
        return out
    except (KeyError, ValueError, TypeError):
        return None


def build_deposition_mesh(gcode_path, diameter=0.02, faces=12, mode="auto",
                          no_sag=False, sag_multiplier=1.0, max_ring_dt=0.03,
                          self_crossings=None, arc_segments=None,
                          scaffold_groups=None, scaffold_default_rgb=None,
                          sag_reset_layers=0, sag_approx=False,
                          mesh_tolerance=0.0, mesh_faces=None,
                          return_paths=False, frame=None, five_axis=None):
    """Build the same sagged-fiber tube mesh write_binary_stl would, but tag every
    vertex with the GLOBAL print time (s) at which the head reaches it.

    Returns (vertices (V,3) float64, triangles (T,3) int64, vert_times (V,) float64,
    normals (V,3) float64, colors (V,3) uint8 or None) -- empty arrays if the file
    has no extruding moves. The timeline matches parse_path (modal feed,
    DEFAULT_FEED until the first F word, travels included), so gcode_player can
    reveal the mesh in lockstep with the playhead.

    Parameters mirror the CLI so the animated mesh is identical to the STL the
    converter wrote for the same settings, EXCEPT each fiber is resampled to at most
    `max_ring_dt` print-seconds between rings so the trail advances smoothly
    (0 disables that densification).

    `mesh_tolerance` is the OTHER half of that promise and must be passed the value
    the export used, or the promise is false: main() thins every centreline through
    decimate_for_mesh on the way into the STL (default 1.0), so a mesh built here at
    0 is DENSER than the STL it claims to match. `mesh_faces` is the facet count the
    TOLERANCE is derived from when that is not the count the tube is built at (the
    player floors `faces` at DEPO_FACES, but the STL was thinned at
    facet_error(r, ITS faces)). `no_sag` suppresses the thinning entirely whatever
    `mesh_tolerance` says, because main() does the same. self_crossings None
    auto-detects from the header (NO_SELF_CROSS_MARKER). sag_approx solves with the
    APPROXIMATE engine -- the no-cache fallback, so it must be told which solver the
    export used or it rebuilds geometry the STL on disk does not have.

    `frame` / `five_axis` are the COLLECTOR, and they are the same pair `main`
    takes: the frame is the surface, `five_axis` a `FiveAxisReader` for the rig
    whose two rotary words a five-axis file carries. Given them, this parses into
    the surface chart, solves the sag there, and wraps the result out to the
    collector's own XYZ -- the same three seams `main` uses, so a no-cache
    rebuild produces the geometry the export produced instead of a flat scaffold
    with the same name. Both None is byte-identical to before."""
    if self_crossings is None:
        self_crossings = not file_skips_self_crossings(gcode_path)
    paths, ptimes = parse_gcode(gcode_path, mode=mode, with_times=True,
                                arc_segments=arc_segments, frame=frame,
                                five_axis=five_axis)
    # `main`'s currency rule: a five-axis read-back on the exact solver stays
    # world to the end; the approximate solver is unwrapped into the chart,
    # AFTER the transform (which takes world), exactly as `main` does.
    world_native = (five_axis is not None and frame is not None
                    and not getattr(frame, "chart_currency", False)
                    and not sag_approx)
    world_surface = frame.surface() if world_native else None
    if not paths:
        return (np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64),
                np.zeros((0,)), np.zeros((0, 3)), None)

    # Per-fibre linking-loop flag for the sag solve's entry-continuity, computed
    # on the COMMANDED paths (mirrors main(), which flags before any fibre-
    # transform seam rounds the loop's closure) so the player's no-cache
    # fallback sags loop entries identically to the exported STL.
    loop_entry = loop_entry_flags(paths)

    # Fibre-transform seam, exactly as main() applies it: with a transform
    # installed the mesh is built on the LAID centreline, not the commanded one.
    # Without this a caller that rebuilds from g-code instead of reading the
    # `.depo` sidecar silently gets the COMMANDED fibre -- no jet-lag, no corner
    # rounding -- which still looks like a working scaffold. That is the
    # machine-link live view whenever the printer runs a file this session did not
    # export. No transform installed -> byte-identical.
    diam_by_time = None
    if _FIBER_TRANSFORM is not None and paths:
        if _FIBER_TRANSFORM_FRAME and five_axis is not None:
            res = _FIBER_TRANSFORM(paths, ptimes, frame=frame)
        else:
            res = _FIBER_TRANSFORM(paths, ptimes)
        if isinstance(res, tuple) and len(res) == 3:
            paths, ptimes, diam_by_time = res
        else:
            paths, ptimes = res
    if five_axis is not None and frame is not None and not world_native:
        import mew_frame
        paths = [mew_frame.unwrap_path(frame, p) for p in paths]

    # per-scaffold fiber diameters / colours (multi-scaffold files); None -> the
    # uniform single diameter / a single solid colour, exactly as before
    path_diam = path_diameters_from_groups(scaffold_groups, len(paths), diameter)
    path_color = path_colors_from_groups(scaffold_groups, len(paths),
                                         scaffold_default_rgb)
    sag_diam = min(path_diam) if path_diam else diameter

    applicable, _n_layers = _sag_applicable(paths, sag_diam,
                                            self_crossings=self_crossings,
                                            frame=frame if world_native else None,
                                            world=world_native)
    if no_sag or not applicable:
        # The solver is what normally rests a fibre's underside on the bed; a run
        # that skips it has to do the same lift itself or its bottom layer is half
        # inside the plate (see `lift_to_underside`).
        paths = lift_to_underside(paths, path_diam, diameter,
                                  surface=world_surface,
                                  point_diam=point_diameters(ptimes,
                                                             diam_by_time))
    else:
        # `sag_approx` picks the same solver the export used, so this no-cache
        # fallback rebuilds the geometry that is actually on disk rather than a
        # second, differently-solved copy of it.
        if sag_approx:
            paths, ptimes = apply_sag_approx(
                paths, diameter, sag_multiplier=sag_multiplier,
                point_times=ptimes, path_diam=path_diam,
                sag_reset_layers=sag_reset_layers, frame=frame)
        else:
            paths, ptimes = apply_sag(
                paths, diameter, sag_multiplier=sag_multiplier,
                point_times=ptimes, self_crossings=self_crossings,
                path_diam=path_diam, loop_entry=loop_entry,
                sag_reset_layers=sag_reset_layers, frame=frame,
                world=world_native)

    # The wrap, and it is `main`'s seam moved here rather than a second copy of
    # it: everything above works in the surface chart and everything below draws
    # in space. `wrap_path` DENSIFIES, and the times ride along, or the tube and
    # its per-vertex clock would be different lengths. A world-native run has
    # nothing to wrap.
    if frame is not None and paths and not world_native:
        import mew_frame
        _w = [mew_frame.wrap_path(frame, p, t) for p, t in zip(paths, ptimes)]
        paths = [w[0] for w in _w]
        ptimes = [w[1] for w in _w]

    mesh = _tube_fibers(paths, ptimes, diameter, faces, max_ring_dt,
                        path_diam=path_diam, path_color=path_color,
                        diam_by_time=diam_by_time,
                        mesh_tolerance=(0.0 if no_sag else mesh_tolerance),
                        mesh_faces=mesh_faces)
    if return_paths:
        # The LAID centreline the mesh was tubed from -- transformed and sagged. A
        # caller anchoring something to the deposited fibre (the player's jet) must
        # use THIS, not the commanded path: with a transform installed they are
        # different curves, and the jet would meet empty space.
        return mesh, (paths, ptimes, path_diam, diam_by_time)
    return mesh


def _tube_fibers(paths, ptimes, diameter, faces, max_ring_dt, path_diam=None,
                 path_color=None, diam_by_time=None, reuse_blocks=None,
                 return_blocks=False, mesh_tolerance=0.0, decimate_idx=None,
                 mesh_faces=None):
    """Tube each ALREADY-SAGGED fiber polyline (with its per-point print times) into
    the deposition mesh: densify by time for a smooth trail, sweep a tube, and tag
    every vertex with its print time and exact outward normal. Returns (verts, tris,
    vert_times, normals, colors); `colors` is per-vertex RGB (V,3) uint8 when
    `path_color` is given, else None. Split out so the player can feed it cached
    sagged fibers and get a byte-for-byte equivalent mesh.

    reuse_blocks / return_blocks: an OPTIONAL per-fibre tube cache for the real-time
    sim. Each block is built at vertex-offset 0 and the offset added only on assembly,
    so a block is position-INDEPENDENT and reusable verbatim for a fibre whose sagged
    geometry did not change. Both off (the default) is byte-for-byte the old behaviour.

    mesh_tolerance is applied to each centreline BEFORE the time resample, the same
    thinning main() applies into the exported STL; 0 (default) is off, and the PLAYER
    passes the run's setting so its mesh and the STL sample alike. decimate_idx: the
    kept-index arrays the EXPORT already computed -- given, the thinning is skipped
    and these applied, and the ORDER matters: decimating first and resampling second
    lets _resample_by_time put back any span that went too coarse in TIME. mesh_faces
    is the facet count the tolerance comes from when the tube is built at a different
    count, since a caller flooring `faces` upward would thin tighter than the STL.

    path_diam / path_color: optional per-path diameter and (r,g,b). diam_by_time:
    (t_knots, d_knots), making each vertex's radius 0.5*interp(its print time) and
    overriding path_diam -- or a PER-FIBRE list of such tuples, so the live sim can
    forward-accumulate per fibre without one global schedule clobbering nominals."""
    empty = (np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64),
             np.zeros((0,)), np.zeros((0, 3)), None)
    radius = diameter / 2.0
    radii = (None if path_diam is None else
             [(float(d) / 2.0 if d and float(d) > 0 else radius)
              for d in path_diam])
    # the facet count the DECIMATION's tolerance comes from (see mesh_faces);
    # `faces` still builds the tube
    dec_faces = faces if mesh_faces is None else max(3, int(mesh_faces))
    vert_blocks, tri_blocks, time_blocks, norm_blocks = [], [], [], []
    color_blocks = [] if path_color is not None else None
    out_blocks = [None] * len(paths) if return_blocks else None
    voff = 0
    for idx, (p, pt) in enumerate(zip(paths, ptimes)):
        cached = (reuse_blocks[idx] if reuse_blocks is not None
                  and idx < len(reuse_blocks) else None)
        if cached is not None:               # warm-start: reuse the cached tube
            v, t_local, times_v, nrm, cblk = cached
        else:
            p = np.asarray(p, dtype=np.float64)
            pt = np.asarray(pt, dtype=np.float64)
            dbt0 = (diam_by_time[idx] if isinstance(diam_by_time, list)
                    else diam_by_time)
            if mesh_tolerance > 0.0 and len(p) >= 2:
                # Thin to what a `faces`-gon tube can express, exactly as main()
                # does for the STL: tolerance from the SMALLEST radius on the
                # fibre, bend penalty from the full radius profile.
                #
                # BEFORE the dedupe, because that is where main() does it.
                # Deduping first would hand the decimator a different input on any
                # fibre with a coincident point and the two samplings would quietly
                # diverge, which also invalidates a cached index array. And before
                # the resample, so the time fill can restore any span this made too
                # coarse to reveal smoothly.
                kidx = (decimate_idx[idx]
                        if decimate_idx is not None and idx < len(decimate_idx)
                        else None)
                if kidx is None:
                    # one implementation of the tolerance rule, shared with the
                    # player's cache-filling call (see decimate_idx_for)
                    kidx = decimate_idx_for(
                        [p], [pt], diameter, dec_faces, mesh_tolerance,
                        path_diam=(None if path_diam is None
                                   else [path_diam[idx]]),
                        diam_by_time=dbt0)[0]
                p, pt = p[kidx], pt[kidx]
            keep = _dedupe_idx(p)            # match tube_mesh's own _dedupe so the
            p = p[keep]                      # per-vertex times stay
            pt = pt[keep]                    # aligned to the geometry
            if len(p) < 2:
                continue
            # densify sparse fibers (the bottom layer especially) so the trail
            # update period is uniform, then dedupe again to keep geometry/times in
            # lockstep with tube_mesh's vertex layout
            p, pt = _resample_by_time(p, pt, max_ring_dt)
            keep = _dedupe_idx(p)
            p = p[keep]
            pt = pt[keep]
            if len(p) < 2:
                continue
            dbt = (diam_by_time[idx] if isinstance(diam_by_time, list)
                   else diam_by_time)
            if dbt is not None:
                r = 0.5 * np.interp(pt, dbt[0], dbt[1])
            else:
                r = radius if radii is None else radii[idx]
            # build at offset 0 so the block is position-independent (reusable);
            # the running voff is added on assembly below -- identical result.
            v, t_local, nrm = tube_mesh(p, r, faces, 0, with_normals=True)
            if len(v) == 0:
                continue
            # tube_mesh's vertex layout: len(p) rings of `faces` vertices in point
            # order, then the two cap centres (first point, last point).
            times_v = np.concatenate([np.repeat(pt, faces), pt[:1], pt[-1:]])
            if len(times_v) != len(v):       # alignment guard; should never trip
                continue
            cblk = (np.broadcast_to(np.array(path_color[idx], dtype=np.uint8),
                                    (len(v), 3))
                    if color_blocks is not None else None)
        vert_blocks.append(v)
        tri_blocks.append(t_local + voff)
        time_blocks.append(times_v)
        norm_blocks.append(nrm)
        if color_blocks is not None:
            color_blocks.append(cblk)
        if out_blocks is not None:
            out_blocks[idx] = (v, t_local, times_v, nrm, cblk)
        voff += len(v)

    if not vert_blocks:
        return (empty + (out_blocks,)) if return_blocks else empty
    colors = (np.concatenate(color_blocks) if color_blocks else None)
    mesh = (np.concatenate(vert_blocks),
            np.concatenate(tri_blocks),
            np.concatenate(time_blocks),
            np.concatenate(norm_blocks),
            colors)
    return (mesh + (out_blocks,)) if return_blocks else mesh


# --------------------------------------------------------------------------- #
# Deposition cache (sidecar so the player skips the sag solve)
# --------------------------------------------------------------------------- #
# The sag solve dominates building the deposition view (>10s on a default
# scaffold) and the converter ALREADY runs it for the STL, so it writes the sagged
# fibers + per-point print times to a small <stem>.depo.npz sidecar that
# gcode_player tubes on the fly. The cache is faces/resolution-independent (it
# stores centrelines, not the mesh), so the player can tube it at preview
# fidelity. With the _FIBER_TRANSFORM seam active the cached centrelines are the
# fibre as actually LAID, so the player's deposition view and jet anchor reflect
# the physics with no player-side change.
DEPO_CACHE_EXT = ".depo.npz"
# Companion to the .depo, holding the kept-index arrays decimate_for_mesh produced
# for the STL. A SEPARATE file because the .depo is written before the decimation
# runs, and is written even by a --no-mesh preview that never decimates. Absent ->
# the consumer recomputes, which is why a preview DROPS it rather than leaving one
# describing the solve it just replaced.
DEPO_DECIM_EXT = ".depo.dec.npz"
# The companion's own key for the RULE that chose its indices. 2 = `decimate_keep`
# also bounds the radius profile (a dynamic diameter); a 4-field companion from
# rule 1 kept a straight fibre's two ends only, and is rejected rather than
# reused against the same point count.
_DEPO_DECIM_RULE = 2


# Bump when a change alters the GEOMETRY the solver writes into a `.depo` without
# altering any of the parameters below it. Every other entry in the cache key is a
# setting the user chose, so toggling one rejects the cache by itself; a solver fix
# has no such entry, so the params match, the file is still newer than the g-code,
# and the old geometry is served. Generation 2 = the crest resampling;
# generation 3 = frame-aware cylindrical deposition placement; generation 4 =
# the sag solve reaching a DOUBLY-CURVED collector.
#
# Generation 4 is the one that had to be bumped for a reason no user setting
# expresses. The `--surface` flags are new, so a cache written before them
# cannot carry them and cannot be distinguished by them -- and the same
# scaffold name in one session can now hold a flat solve and a curved one. A
# stale `.depo` there is not a slightly-wrong drape: it is a scaffold on a
# different surface, which the player and the Viewer would draw as a working
# print.
#
# Generation 5 = the FIVE-AXIS CLOCK. A `.depo` stores a per-point
# time array `t`, and on a five-axis file those times were the deposition
# point's own travel divided by F. They are now `mew_kinematics.machine_norm`
# divided by F -- see `feed_span` -- which on an apex-centred dome is 16.9x
# larger. Nothing in the key above expresses that: same diameter, same sag
# settings, same flags, so a cache written yesterday passes every test and is
# then read against a clock that disagrees with it by more than an order of
# magnitude. The player reveals by time and places the jet foot at where the
# nozzle was `lag` SECONDS ago, so a stale file there is not a slightly-early
# reveal -- it is a foot drawn ahead of the nozzle.
#
# A cache KEY, not a file-format version: an old `.depo` is REJECTED and
# recomputed, never misread. Bumping costs one re-solve per cached scaffold.
#
# 5 -> 6: the bed datum. Every centreline in a generation-5 cache is one radius
# low -- the solver rested a bottom-layer fibre's CENTRE on the plate instead of
# its underside, burying half of it (`_SagState._solve_layer`'s `bed_at`). The
# plate sibling is placed to match, so a stale cache read with the new plate
# would show a sink that is not there.
#
# 6 -> 7: THE SAG-LADDER CONTACT FIXES, ported from `module-grid` as one bump
# because they land on this branch together. Each is invisible to the key above
# -- same g-code, same diameter, same flags -- so without a bump the viewer
# reloads a generation-6 `.depo` and redraws the geometry every one of them
# fixes.
#   * the RETRACE contact (`_self_near`). A stroke laid straight back over the
#     one before it now rests on it, so any print whose fibres retrace -- every
#     calibration sag ladder, whose wall is `layers_per_visit` passes out and
#     back at one commanded Z -- stands taller than a generation-6 cache has it.
#   * SAME-LAYER member contact. A layer's later member now rests on its earlier
#     ones (`_solve_layer`'s `same_layer` index), so any print that lays more
#     than one member per layer -- every calibration sweep, whose layer is
#     fourteen wall visits and two lane traverses at one commanded Z -- stands
#     taller and stops interpenetrating.
#   * the BAND BOUNDARY (`_split_layer_spanning`). A deposited step that rises a
#     layer while it travels is no longer cut out of the solve, so any print with
#     a laid travel that climbs -- the sag ladder's once-per-layer lane, which
#     crossed four wall stacks inside one 3.68 mm step -- now rests on what it
#     crosses instead of running flat through it.
#   * the LOCAL CONTACT RADII (`rad_at`, `support_radii`). A contact rests at the
#     sum of the two LOCAL radii, not at one layer diameter, which is 27 um of
#     stack height by layer 9 of the 13-layer ladder.
#   * the VISIT SEAM (`seam_prev`). A new member leaves from where the last one
#     ended instead of dropping through it into the stroke below.
#   * the FOLD BRIDGE (`_fold_bridge`). Inside one arc gap of a 180 degree
#     reversal the filament can have no self-contact by construction, and the
#     point that lands there would otherwise drop through the limb the same
#     stroke had just laid.
#   * the LANDING-SPEED DIAMETER (`mew_deposition.diameter_by_time`). Wherever
#     the touchdown is in transient -- every start, corner, speed change and
#     above all every 180 degree reversal -- the fibre thickens toward the CTS
#     diameter instead of staying at the cruise value, so a sag ladder's wall
#     tips are fatter and every layer above them rests higher. Same g-code, same
#     NOMINAL diameter, same flags, so the key cannot express it either.
#
# 7 -> 8: TANGENT TOUCHDOWN (`_tangent_touchdowns`). Where a span's droop
# reaches the bed, or a fibre lying along it, the landing is now a pinned
# support and the fibre arrives tangentially, instead of at the rest clamp's
# corner. Same g-code, same flags, so a generation-7 `.depo` would redraw every
# landing with the corner -- and the layers above them on the old floor.
#
# 8 -> 9: THE CONVERGED RELAXATION (`_relax_pins`) and SAME-PATH CHORDS
# (`_same_path`). A droop that reaches a floor is relaxed onto it with as many
# contacts as it takes, not one per side; a floor peak is pinned only where the
# converged droop rests on it; and a lower segment lying on this fibre's own
# path is no longer an exact crossing. Same g-code, same flags, so a
# generation-8 `.depo` would keep every stacked strand pinned where its
# sampling put it.
#
# 9 -> 10: THE LOCAL-RADIUS BED LIFT without the solver. A run that skips the
# sag solve (one layer, or --no-sag) is seated by `lift_to_underside`, which
# lifted a dynamic-diameter fibre by the nominal radius; it now lifts each point
# by its own, as `bed_at` does. Same g-code, same flags, so a generation-9
# `.depo` would keep thick sections sunk into the bed and thin ones floating.
_DEPO_CACHE_GENERATION = 10


def _depo_cache_meta(diameter, no_sag, sag_multiplier, self_crossings=True,
                     transformed=False, phys_sag=False, phys_fuse=False,
                     sag_reset_layers=0, render_travel=False,
                     sag_approx=False):
    meta = [float(diameter), float(bool(no_sag)),
            float(sag_multiplier), float(bool(self_crossings)),
            float(bool(transformed)), float(bool(phys_sag)),
            float(bool(phys_fuse)), float(int(sag_reset_layers or 0)),
            float(bool(render_travel)), float(bool(sag_approx)),
            float(_DEPO_CACHE_GENERATION)]
    # The sag resolution in force (`set_sag_resolution`), read here rather than
    # passed so no writer or loader can leave it out. Appended ONLY off the
    # default: a 1.0 cache keeps the 11-long key every existing .depo carries, and
    # a finer one differs in shape, which load_depo_cache rejects either way round.
    if _SAG_RESOLUTION != 1.0:
        meta.append(float(_SAG_RESOLUTION))
    return np.array(meta, dtype=np.float64)


def write_depo_cache(path, paths, ptimes, *, diameter, no_sag, sag_multiplier,
                     self_crossings=True, diam_by_time=None, transformed=False,
                     path_diam=None, phys_sag=False, phys_fuse=False,
                     path_group=None, group_names=None, sag_reset_layers=0,
                     render_travel=False, sag_approx=False, travel_spans=None):
    """Write the sagged fibers + times to `path` (a .npz). `paths`/`ptimes` are
    the per-fiber arrays AFTER sagging (or the raw parse when sag is off). Best
    effort: any failure is swallowed so it never breaks STL generation.

    diam_by_time: optional (t_knots, d_knots) diameter-vs-print-time lookup, stored
    alongside so the player can tube the cached fibres with a radius that VARIES
    along each one (load_depo_diam_by_time). Absent -> a uniform-diameter cache.

    transformed / phys_sag: whether the fibre-transform seam and the grounded
    physical sag law were active when these fibres were built. Stamped into the
    meta so a consumer that now wants a DIFFERENT mode rejects this cache rather
    than reusing geometry from the wrong one (see load_depo_cache).

    path_group / group_names: the per-fibre scaffold-group index and the ordered
    group names from --scaffold-groups, so the fast centreline viewers can split
    the cached fibres back into per-scaffold actors matching the sibling STLs.
    Absent -> a single merged view (load_depo_groups).

    sag_reset_layers: the periodic-sag-reset period these fibres were solved with,
    stamped like the other sag params.

    travel_spans: [(t0, t1)] print times of the travel material merged into these
    fibres, stored because `--from-depo` skips the parse and so has no `travels`
    of its own -- without them a rebuild keeps travel material the same settings
    cut out on a fresh run. TIME, not an index, for the reason
    `_drop_travel_spans` states. Absent -> the rebuild leaves whatever the cache
    holds in the mesh.

    sag_approx: whether the APPROXIMATE solver produced these fibres. It has to be
    stamped: the two solvers take the same diameter / multiplier / reset period, so
    no other key can tell their output apart, and the player would otherwise ride
    an exact-sagged cache while the STL on disk is approximate."""
    if not paths:
        return
    try:
        xyz = np.concatenate([np.asarray(p, dtype=np.float32) for p in paths])
        t = np.concatenate([np.asarray(s, dtype=np.float64) for s in ptimes])
        splits = np.cumsum([len(p) for p in paths]).astype(np.int64)
        extra = {}
        if diam_by_time is not None:
            extra["diam_t"] = np.asarray(diam_by_time[0], dtype=np.float64)
            extra["diam_d"] = np.asarray(diam_by_time[1], dtype=np.float64)
        # per-fiber base diameter, so the viewers and the on-demand exact build
        # tube each scaffold at ITS diameter (load_depo_pdia)
        if path_diam is not None and len(path_diam) == len(paths):
            extra["pdia"] = np.asarray(path_diam, dtype=np.float64)
        # per-fibre scaffold-group index + the group names, so the centreline
        # viewers can rebuild per-scaffold actors (load_depo_groups). Only stored
        # when both are present and consistent; an ungrouped run omits them.
        if (path_group is not None and group_names is not None
                and len(path_group) == len(paths)):
            extra["pgroup"] = np.asarray(path_group, dtype=np.int64)
            extra["gname"] = np.asarray([str(n) for n in group_names])
        # travel spans (M, 2), print times -- what a --from-depo rebuild trims on
        if travel_spans:
            extra["tspan"] = np.asarray(travel_spans, dtype=np.float64
                                        ).reshape(-1, 2)
        np.savez(path, xyz=xyz, t=t, splits=splits,
                 meta=_depo_cache_meta(diameter, no_sag, sag_multiplier,
                                       self_crossings, transformed, phys_sag,
                                       phys_fuse, sag_reset_layers,
                                       render_travel, sag_approx),
                 **extra)
    except Exception:  # noqa: BLE001 -- a missing cache just means recompute
        pass


def load_depo_cache(path, *, diameter, no_sag, sag_multiplier,
                    self_crossings=True, gcode_path=None, transformed=False,
                    phys_sag=False, phys_fuse=False, sag_reset_layers=0,
                    render_travel=False, sag_approx=False):
    """Return (paths, ptimes) from a .depo.npz if it exists, was built with the
    SAME sag params AND the same `transformed`, `phys_sag`, `phys_fuse` and
    `render_travel` modes, and is newer than the g-code; else None (-> recompute).
    Toggling any one mode alone therefore no longer reuses stale geometry. A cache
    written before a key existed has a shorter meta and is rejected by the shape
    test below.

    `render_travel` matters even though `merge_travels` PRESERVES the fibre count:
    a travel-rendered cache has the same number of fibres as a plain parse, so a
    consumer's count guard cannot catch the difference and only this key can. That
    is the trap the live march would otherwise fall into -- it parses without
    travels, so it must pass render_travel=False and let a travel cache be rejected
    rather than seed itself with geometry it cannot reproduce."""
    if not path or not os.path.isfile(path):
        return None
    if (gcode_path and os.path.isfile(gcode_path)
            and os.path.getmtime(path) < os.path.getmtime(gcode_path)):
        return None                                  # stale: g-code re-edited
    try:
        with np.load(path, allow_pickle=False) as d:
            want = _depo_cache_meta(diameter, no_sag, sag_multiplier,
                                    self_crossings, transformed, phys_sag,
                                    phys_fuse, sag_reset_layers, render_travel,
                                    sag_approx)
            meta = d["meta"]
            if meta.shape != want.shape or not np.allclose(meta, want):
                return None                          # built with other params
            xyz = d["xyz"].astype(np.float64)
            t = d["t"].astype(np.float64)
            splits = d["splits"]
    except Exception:  # noqa: BLE001
        return None
    cut = splits[:-1]
    return list(np.split(xyz, cut)), list(np.split(t, cut))


def decimate_idx_for(paths, ptimes, diameter, faces, mult,
                     path_diam=None, diam_by_time=None):
    """The per-fibre kept-index arrays _tube_fibers thins with, computed the way
    main() computes them for the STL: tolerance from the SMALLEST radius on the
    fibre, bend penalty from the whole radius profile.

    Factored out so the PLAYER can compute them once, cache them beside the `.depo`
    and pass them back in on the next open -- and so there is exactly ONE
    implementation of the tolerance rule. Fibres shorter than 2 points get an
    identity index, so the result lines up one-to-one with `paths`."""
    radius = diameter / 2.0
    radii = (None if path_diam is None else
             [(float(d) / 2.0 if d and float(d) > 0 else radius)
              for d in path_diam])
    out = []
    for i, p in enumerate(paths):
        pts = np.asarray(p, dtype=np.float64)
        if len(pts) < 2 or mult <= 0.0:
            out.append(np.arange(len(pts), dtype=np.int64))
            continue
        dbt = (diam_by_time[i] if isinstance(diam_by_time, list)
               else diam_by_time)
        if dbt is not None:
            r = 0.5 * np.interp(np.asarray(ptimes[i], dtype=np.float64),
                                dbt[0], dbt[1])
        else:
            r = radius if radii is None else radii[i]
        r_min = float(np.min(r)) if np.ndim(r) else float(r)
        out.append(np.asarray(
            decimate_keep(pts, mult * facet_error(r_min, faces), radius=r),
            dtype=np.int64))
    return out


def decim_path_for(depo_path):
    """The decimation companion beside a .depo path (or None if it isn't one)."""
    if not depo_path or not depo_path.endswith(DEPO_CACHE_EXT):
        return None
    return depo_path[:-len(DEPO_CACHE_EXT)] + DEPO_DECIM_EXT


def write_depo_decimation(depo_path, keep, *, mult, faces, n_in):
    """Store the kept-index arrays the STL's decimation produced, beside the
    .depo. Best effort: any failure is swallowed, because this is a CACHE -- the
    consumer recomputes when it is missing, and a half-written one is rejected by
    the key below rather than trusted.

    `n_in` is the total point count the indices were computed against; it is what
    catches a companion left over from a different solve of the same scaffold."""
    p = decim_path_for(depo_path)
    if not p or not keep:
        return
    try:
        lens = np.array([len(k) for k in keep], dtype=np.int64)
        np.savez_compressed(
            p,
            idx=(np.concatenate([np.asarray(k, dtype=np.int64) for k in keep])
                 if len(keep) else np.zeros(0, dtype=np.int64)),
            splits=np.cumsum(lens),
            meta=np.array([float(mult), float(int(faces)), float(len(keep)),
                           float(int(n_in)), float(_DEPO_DECIM_RULE)],
                          dtype=np.float64))
    except Exception:  # noqa: BLE001
        pass


def drop_depo_decimation(depo_path):
    """Remove the decimation companion beside `depo_path`, if there is one.

    Called from BOTH places a companion can be left behind stale, because the key
    cannot catch the second one: a run that does not decimate (Mesh detail "Full",
    or --no-sag), and a run that REWRITES the .depo without decimating -- a
    --no-mesh preview -- where a geometry change that leaves the fibre count and
    total point count alone (a different sag multiplier or reset period does
    exactly that) is invisible to every field in the key.

    Best effort, like the write: this is a cache, and the consumer recomputes."""
    p = decim_path_for(depo_path)
    if p and os.path.isfile(p):
        try:
            os.remove(p)
        except OSError:
            pass


def load_depo_decimation(depo_path, *, mult, faces, paths):
    """The kept-index arrays for `paths`, or None to recompute.

    Returns None unless the companion was written at the SAME `mult` and `faces`
    and against the same fibre count and total point count, so changing "Mesh
    detail" (deliberately NOT a .depo cache key) falls back to computing it rather
    than reusing the previous setting's sampling. Every index is bounds-checked
    before it is handed back, because an out-of-range one would raise deep inside
    the tube builder."""
    p = decim_path_for(depo_path)
    if not p or not os.path.isfile(p) or mult <= 0.0:
        return None
    try:
        with np.load(p, allow_pickle=False) as d:
            meta, idx, splits = d["meta"], d["idx"], d["splits"]
        if len(meta) != 5 or int(meta[4]) != _DEPO_DECIM_RULE:
            return None                       # thinned by an older rule
        n_in = sum(len(x) for x in paths)
        if (abs(float(meta[0]) - float(mult)) > 1e-12
                or int(meta[1]) != int(faces)
                or int(meta[2]) != len(paths)
                or int(meta[3]) != int(n_in)):
            return None                       # a different solve or setting
        out = np.split(np.asarray(idx, dtype=np.int64),
                       np.asarray(splits, dtype=np.int64)[:-1])
        if len(out) != len(paths):
            return None
        for k, src in zip(out, paths):
            if len(k) < 2 or k[0] < 0 or k[-1] >= len(src):
                return None                   # truncated / mismatched
        return out
    except Exception:  # noqa: BLE001
        return None


def load_depo_diam_by_time(path):
    """Return the (t_knots, d_knots) diameter-vs-print-time lookup a physics
    dynamic-diameter export stored in a .depo.npz, or None. Kept separate from
    load_depo_cache so that function's (paths, ptimes) return is unchanged. The
    player feeds it to _tube_fibers / its jet anchor so the cached fibres render
    with a speed-dependent thickness."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with np.load(path, allow_pickle=False) as d:
            if "diam_t" in d and "diam_d" in d:
                return (d["diam_t"].astype(np.float64),
                        d["diam_d"].astype(np.float64))
    except Exception:  # noqa: BLE001
        return None
    return None


def load_depo_pdia(path):
    """Return the per-fiber base diameters a multi-scaffold run stored in a
    .depo.npz (`pdia`), or None. The viewers and the on-demand exact build use it
    to tube each scaffold at its OWN diameter; a dynamic diam_by_time, when
    present, still drives the finer speed-dependent radius."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with np.load(path, allow_pickle=False) as d:
            if "pdia" in d:
                return [float(v) for v in d["pdia"]]
    except Exception:  # noqa: BLE001
        return None
    return None


def load_depo_travel_spans(path):
    """Return the travel intervals `write_depo_cache` stored in a .depo.npz
    (`tspan`) as [(t0, t1)], or None. The ONLY route by which a `--from-depo`
    rebuild can find the travel material: that path skips the parse, so
    `travels` never exist. Without it the rebuild kept travel fibre a fresh run
    with the same settings cuts out -- and before that raised UnboundLocalError
    on the trim itself."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with np.load(path, allow_pickle=False) as d:
            if "tspan" in d:
                a = np.asarray(d["tspan"], dtype=np.float64).reshape(-1, 2)
                return [(float(t0), float(t1)) for t0, t1 in a]
    except Exception:  # noqa: BLE001
        return None
    return None


def load_depo_groups(path):
    """Return (path_group, group_names) a multi-scaffold run stored in a .depo.npz:
    `pgroup` (one group index per fibre) and `gname` (the ordered names), or None.
    Lets the fast centreline viewers split the cached fibres back into per-scaffold
    actors, so a tube/wireframe view keeps the hide/show + colour-by-scaffold
    controls the exact mesh path gets from the sibling STLs."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with np.load(path, allow_pickle=False) as d:
            if "pgroup" in d and "gname" in d:
                return ([int(v) for v in d["pgroup"]],
                        [str(n) for n in d["gname"]])
    except Exception:  # noqa: BLE001
        return None
    return None


def _load_depo_raw(path):
    """Load sagged fibres + times from a .depo cache WITHOUT the param/freshness
    checks load_depo_cache makes -- the caller (--from-depo) points at this exact
    file on purpose, to tube already-solved geometry without re-running the sag
    solve. Returns (paths, ptimes) or None. (Per-fiber diameters and the dynamic
    diam-by-time lookup are read separately via load_depo_pdia / _diam_by_time.)"""
    try:
        with np.load(path, allow_pickle=False) as d:
            xyz = d["xyz"].astype(np.float64)
            t = d["t"].astype(np.float64)
            splits = d["splits"]
    except Exception:  # noqa: BLE001
        return None
    cut = splits[:-1]
    return list(np.split(xyz, cut)), list(np.split(t, cut))


# --------------------------------------------------------------------------- #
# STL output (binary)
# --------------------------------------------------------------------------- #
# Binary-STL triangle record: facet normal + 3 vertices (float32) + 2-byte
# attribute. align=False so it packs to exactly 50 bytes, matching the spec.
_STL_REC = np.dtype([
    ("n", "<f4", 3),
    ("v0", "<f4", 3),
    ("v1", "<f4", 3),
    ("v2", "<f4", 3),
    ("attr", "<u2"),
])


def _write_stl_records(fh, verts, tris, chunk=1_000_000, progress=None):
    """Serialise triangles `tris` (indices into `verts`) as binary-STL records
    onto `fh`, `chunk` at a time. Returns the number written.

    Facet normals are computed with numpy and whole blocks of triangles serialised
    straight into the 50-byte STL record layout, in chunks to keep peak memory
    bounded. The normals dominate the time, not the file I/O, which is why np.cross
    (extra temporaries), np.linalg.norm (sqrt(einsum) is the same value) and
    np.divide's `where=` are all avoided, and why the record array goes straight to
    fh.write rather than through another .tobytes() copy.

    Every record depends on its own three vertices alone, so a mesh written in
    any number of pieces, in order, is byte-identical to the mesh written whole
    (`_StlStream` relies on this).

    The zero-fill of degenerate facets looks dead -- |n| == 0 happens exactly when
    n is already (0,0,0) -- but removing it CHANGES THE BYTES: a cross product can
    produce NEGATIVE zero and the assignment normalises -0.0 to +0.0. Two
    coincident vertices and a third off-axis is enough:
        v = [[0,0,0], [0,0,0], [0,1,-1]]  ->  normal x becomes 0x80000000.
    Real fibre exports contain no zero-area facets, and the `.any()` guard means
    the scatter only runs on a mesh that has one."""
    verts = np.asarray(verts, dtype=np.float64)
    tris = np.asarray(tris)
    n_tri = len(tris)
    for s in range(0, n_tri, chunk):
        if progress is not None:
            progress(s, n_tri)
        tb = tris[s:s + chunk]
        p0 = verts[tb[:, 0]]
        p1 = verts[tb[:, 1]]
        p2 = verts[tb[:, 2]]
        u = p1 - p0
        v = p2 - p0
        n = np.empty_like(u)
        n[:, 0] = u[:, 1] * v[:, 2] - u[:, 2] * v[:, 1]
        n[:, 1] = u[:, 2] * v[:, 0] - u[:, 0] * v[:, 2]
        n[:, 2] = u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0]
        ln = np.sqrt(np.einsum("ij,ij->i", n, n))[:, None]
        # normalise where the triangle has area
        np.divide(n, ln, out=n, where=ln > 0)
        # ... and force POSITIVE zero on the rest (see the docstring: this is
        # a sign fix, not a value fix, and it is what keeps the bytes identical)
        degenerate = ln[:, 0] == 0.0
        if degenerate.any():
            n[degenerate] = 0.0

        rec = np.empty(len(tb), dtype=_STL_REC)
        rec["n"] = n
        rec["v0"] = p0
        rec["v1"] = p1
        rec["v2"] = p2
        rec["attr"] = 0
        fh.write(rec)
    return n_tri


def write_binary_stl(path, vertices, triangles, chunk=1_000_000,
                     progress=None):
    """Write a binary STL of a whole mesh at once (`_write_stl_records`)."""
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)                       # header
        fh.write(struct.pack("<I", len(triangles)))  # triangle count
        _write_stl_records(fh, vertices, triangles, chunk=chunk,
                           progress=progress)


#: the converter's exit code when --max-stl-bytes refused to write the STL
STL_TOO_BIG_EXIT = 3


def stl_size_estimate(paths, faces, path_group=None):
    """``(bytes, triangles)`` the fibre STL for centrelines `paths` will take,
    counted as `tube_mesh` builds each tube: ``2 * faces`` side triangles per
    segment plus a ``faces``-triangle fan cap at each end. A binary STL record
    is 50 bytes after an 84-byte header. With `path_group` (one group index
    per path) the per-scaffold part STLs are counted too, since every fibre is
    written again into its part. Points are de-duplicated as `tube_mesh` does
    them (the approximate sag solver repeats about 8% of its samples), so the
    count is the one written."""
    n_tri = 0
    groups = set()
    for i, p in enumerate(paths):
        n = len(_dedupe_idx(np.asarray(p, dtype=np.float64))) if len(p) else 0
        if n < 2:
            continue
        n_tri += 2 * faces * (n - 1) + 2 * faces
        if path_group is not None:
            groups.add(path_group[i])
    n_bytes = 84 + 50 * n_tri
    if path_group is not None and groups:
        n_bytes += 84 * len(groups) + 50 * n_tri
    return n_bytes, n_tri


class _StlStream:
    """A binary STL written one piece at a time, so a mesh never has to be held
    whole: the fibre export tubes and writes each fibre in turn. Byte-identical
    to `write_binary_stl` over the same pieces concatenated in order (each
    record depends on its own triangle alone; the count in the header is
    patched on `close`).

    Written to `<path>.part` and renamed over `path` on `close`, so nothing is
    left at `path` by a run that writes no triangles and calls `discard`, or by
    one that dies mid-write. The file is opened on the first piece, so a stream
    that never receives one leaves no file at all."""

    def __init__(self, path):
        self.path = path
        self.tmp = path + ".part"
        self.fh = None
        self.n_tri = 0
        self.n_vert = 0

    def add(self, verts, tris, progress=None):
        """Append one piece: `tris` index into this piece's own `verts`. A
        piece with no vertices is no piece (the whole-mesh writer's callers
        skipped those too)."""
        if not len(verts):
            return
        self._open()
        self.n_tri += _write_stl_records(self.fh, verts, tris,
                                         progress=progress)
        self.n_vert += len(verts)

    def _open(self):
        if self.fh is None:
            self.fh = open(self.tmp, "wb")
            self.fh.write(b"\0" * 80)
            self.fh.write(struct.pack("<I", 0))

    def close(self, always=False):
        """Patch the triangle count and move the file into place. Returns
        whether a file was written. `always` writes one even with no piece
        (an empty, 84-byte STL), as the whole-mesh writer did."""
        if self.fh is None and always:
            self._open()
        if self.fh is None:
            return False
        self.fh.seek(80)
        self.fh.write(struct.pack("<I", self.n_tri))
        self.fh.close()
        self.fh = None
        os.replace(self.tmp, self.path)
        return True

    def discard(self):
        """Drop whatever was written; nothing is left at `path`."""
        if self.fh is not None:
            self.fh.close()
            self.fh = None
            try:
                os.remove(self.tmp)
            except OSError:
                pass


def _write_build_plate(args, out_path):
    """Write the optional build-plate sibling STL (<out>_plate.stl), or sweep a
    stale one when this run draws no plate. The plate is a VIEWING AID derived
    purely from the --plate* args and never touches the fibre mesh, so it must be
    produced on EVERY run that asks for it, including --no-mesh preview runs: the
    viewers offer their show/hide-plate toggle only when this sibling exists. Also
    sweeps the legacy <out>_slide.stl an older toolchain wrote.

    Plate TOP placement, so the fibres rest ON it: at the deposition datum,
    full stop. A bottom-layer fibre rests its UNDERSIDE there (`bed_at`), so the
    datum IS the contact plane and the plate top belongs on it, with no
    one-diameter drop: a bottom layer's centreline does not sit on the datum
    with half the tube under it, so there is nothing to compensate. It descends one
    plate-thickness below the top, and is shifted by the same --origin-offset
    re-base as the fibres."""
    plate_path = os.path.splitext(out_path)[0] + "_plate.stl"
    if args.plate:
        # shift the plate by the same origin re-base applied to the fibres, so it
        # stays under the (already-translated) print
        ox, oy, oz = getattr(args, "origin_offset", (0.0, 0.0, 0.0))
        top_z = args.plate_top_z + oz
        bottom_z = top_z - args.plate_thickness
        cx, cy = args.plate_center_x + ox, args.plate_center_y + oy
        if args.plate_shape == "circular":
            sv, st = disk_prism_mesh(cx, cy, args.plate_diameter / 2.0,
                                     bottom_z, top_z, 0)
            shape_note = f"circular d{args.plate_diameter:g}mm"
        else:
            hx, hy = args.plate_size_x / 2.0, args.plate_size_y / 2.0
            sv, st = box_mesh(cx - hx, cx + hx, cy - hy, cy + hy,
                              bottom_z, top_z, 0)
            shape_note = f"rectangular {args.plate_size_x:g}x{args.plate_size_y:g}mm"
        write_binary_stl(plate_path, sv, st)
        print(f"Wrote {plate_path} (build plate {shape_note}, "
              f"{args.plate_thickness:g}mm thick, top at z={top_z:g})")
    elif os.path.isfile(plate_path):
        # a previous run at this path wrote a plate and this one didn't -> sweep
        # it, so the viewer's optional plate toggle can't surface a stale slab
        try:
            os.remove(plate_path)
            print(f"Removed stale {plate_path}")
        except OSError:
            pass
    # the slide became the build plate; sweep any legacy sibling so the viewers
    # (which no longer read it) cannot surface a stale slab
    legacy_slide = os.path.splitext(out_path)[0] + "_slide.stl"
    if os.path.isfile(legacy_slide):
        try:
            os.remove(legacy_slide)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
#: The most facets a fiber tube may have. The STL grows linearly with it, and
#: 100000 hung the converter; 64 is past any visible difference at MEW sizes.
MAX_FACES = 64


def _no_fibers_reason(path, mode):
    """Why a parse of `path` drew no fiber, in the user's words. The readers
    split words on spaces and read UTF-8, so a file from another tool can
    hold real moves and still parse to nothing; that case is named, with the
    way through (the G-code window tidies such a file when it opens it)."""
    name = os.path.basename(path)
    try:
        import mew_gcode
        with open(path, "rb") as fh:
            data = fh.read()
        text, enc = mew_gcode.decode_gcode_bytes(data)
        _tidy, notes = mew_gcode.tidy_foreign_text(text)
    except Exception:  # noqa: BLE001 -- the plain reason still stands
        enc, notes, text = None, [], ""
    fixes = ([enc] if enc else []) + notes
    if fixes:
        return ("%s holds moves this converter cannot read as written (it "
                "needs: %s). Open the file in the G-code window, which "
                "tidies it as it opens, and generate from there"
                % (name, "; ".join(fixes)))
    if not text.strip():
        return "%s is empty: there is nothing to draw" % name
    if mode == "e":
        return ("%s has no move whose E value increases, so nothing extrudes "
                "in extrusion mode. A MEW file with no E words is read in g1 "
                "mode, where every G1 draws fiber" % name)
    return ("%s has no printing move to draw a fiber along: no G1 (or G2/G3) "
            "move, or none with a word that moves the nozzle" % name)


def main(argv=None):
    """`_main`, with a collector-chart refusal REPORTED rather than raised.

    A `mew_frame.ChartUnusable` is not a crash: it is the converter saying this
    collector's `(u, v)` is not a good enough ruler where this pattern sits,
    and the message it carries names the number and the fix. Reaching a user as
    a traceback -- in the GUI's run log, which is where a five-axis export ends
    up -- it reads as a bug in MEWracle rather than as an answer, which is
    exactly the failure `gui_run`'s "blames memory" dialog already is.

    `mew_frame` is imported INSIDE the handler, not at the top. That keeps the
    flat path from importing it at all, which is the seam the module docstring
    sets up and which `--mandrel-diameter` also honours.

    A path that cannot be opened or written is reported the same way, for the
    same reason: it is the user's answer to fix, not the converter's crash.

    Stdout is a PIPE when the GUI runs this, and a pipe on Windows takes the
    ANSI code page, which cannot encode a Cyrillic or CJK path: the first
    `print` of one killed the worker. Unencodable characters are escaped
    instead; a real console already writes Unicode.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):     # not a TextIOWrapper
            pass
    try:
        return _main(argv)
    except OSError as exc:
        print("error: %s" % (exc,), file=sys.stderr)
        return 2
    except ValueError as exc:
        import mew_frame
        if not isinstance(exc, mew_frame.ChartUnusable):
            raise
        print("error: %s" % (exc,), file=sys.stderr)
        return 2


def _main(argv=None):
    ap = argparse.ArgumentParser(
        description="Convert 3D-printing G-code into an STL of solid fibers."
    )
    ap.add_argument("input", help="input .gcode file")
    ap.add_argument("-o", "--output", default=None,
                    help="output .stl file (default: <input>.stl)")
    ap.add_argument("-d", "--diameter", type=float, default=0.02,
                    help="fiber diameter in mm (default: 0.02)")
    ap.add_argument("-f", "--faces", type=int, default=12,
                    help='number of faces on the fiber "circle" (default: 12)')
    ap.add_argument("--diameter-multiplier", type=float, default=1.0,
                    metavar="X",
                    help="visualization only: scale every drawn fiber's "
                         "diameter by X in the STL tubes (default 1). The sag "
                         "solve and the .depo cache are unaffected")
    ap.add_argument("--no-mesh", action="store_true",
                    help="run the sag solve and write the .depo cache, but DON'T "
                         "build/write the STL mesh (or its per-scaffold/plate "
                         "siblings). Lets the viewers render the sagged centrelines "
                         "as wireframe/tubes without paying the mesh-build time; the "
                         "exact STL is built later on demand (see --from-depo)")
    ap.add_argument("--from-depo", default=None, metavar="CACHE.npz",
                    help="skip parse + sag and read the already-sagged fibres from "
                         "this .depo cache, then tube + write the STL. Builds the "
                         "exact mesh on demand (e.g. for export) WITHOUT re-running "
                         "the sag solve. The cache itself is not rewritten")
    ap.add_argument("--max-stl-bytes", type=float, default=None, metavar="BYTES",
                    help="count the STL's triangles before writing any, and when "
                         "the files would pass BYTES write nothing: print "
                         "'STL_TOO_BIG <bytes> <triangles>' and exit with code "
                         "%d, so the caller can say how big it would be and ask "
                         "first. Counted after the mesh thinning, part STLs "
                         "included" % STL_TOO_BIG_EXIT)
    ap.add_argument("--subsection", default=None, metavar="SECTION.json",
                    help="clip the solved fibre mesh to this subsection; requires "
                         "--from-depo")
    ap.add_argument("--progress", action="store_true",
                    help="emit machine-readable progress lines on stdout "
                         f"('{PROGRESS_MARKER} <fraction 0..1> <stage>'), one "
                         "per update, for a GUI to drive a determinate bar "
                         "with. Off by default so a terminal run stays "
                         "readable. The fraction is a share of the WORK, not an "
                         "estimate of the time left")
    ap.add_argument("-m", "--mode", choices=["auto", "e", "g1"], default="auto",
                    help='extrusion detection: "e" = E-based (slicer), '
                         '"g1" = every G1 is a fiber (MEWron/continuous flow), '
                         '"auto" = detect from file (default)')
    ap.add_argument("--arc-segments", type=int, default=_DEFAULT_ARC_SEGMENTS,
                    help="segments per half-turn when re-expanding G2/G3 arcs for "
                         f"the mesh (default: {_DEFAULT_ARC_SEGMENTS}; more = "
                         "smoother but a slower sag solve). Ignored for files "
                         "with no arcs")
    ap.add_argument("--mesh-tolerance", type=float, default=1.0, metavar="MULT",
                    help="thin the sagged centreline before tubing it, keeping "
                         "every dropped point within MULT x the tube's OWN "
                         "faceting error r*(1-cos(pi/faces)) of the kept "
                         "polyline (default: 1.0 -> the STL is no less faithful "
                         "than its own facets already are, at a fraction of the "
                         "triangles). 0 disables it and restores the previous "
                         "byte-identical mesh. Affects the STL only -- the .depo "
                         "cache keeps the full sag sampling")

    # --- sagging ----------------------------------------------------------- #
    ap.add_argument("--no-sag", action="store_true",
                    help="disable sagging (render straight bars)")
    ap.add_argument("--sag-approx", action="store_true",
                    help="solve the sag with the APPROXIMATE engine (sag_approx."
                         "drape_sag) instead of the exact contact solve: the same "
                         "physics with analytic segment-pair contacts instead of "
                         "half-diameter floor sampling, ~5x faster and within a "
                         "fraction of a fibre diameter of apply_sag. Ignored when "
                         "--no-sag is given. Off (default) -> the exact solve, "
                         "byte-identical. Stamps the .depo cache as approximate.")
    ap.add_argument("--sag-multiplier", type=float, default=1.0,
                    help="scale the droop the algorithm computes from the "
                         "geometry. The sag is determined by pore size (the span "
                         "between crossovers) and fiber diameter -- it grows with "
                         "the square of the pore, so tight pores barely sag and "
                         "wide pores sag onto whatever is below, with no "
                         "artificial cap. This is the only sag knob: 1.0 = the "
                         "algorithm's own droop (default), >1 sags more, <1 less, "
                         "0 = none")
    ap.add_argument("--travel-in-stl", action="store_true",
                    help="with --render-travel: tube the travel fibre into "
                         "the EXPORTED STL as well. Off by default -- the "
                         "travel is solved with the rest (it is real material "
                         "that later layers rest on, and the .depo the player "
                         "rides carries it), but the mesh written to disk is "
                         "the scaffold alone unless you ask for it.")
    ap.add_argument("--render-travel", action="store_true",
                    help="render travel (G0) moves as deposited fibre. In MEW the "
                         "jet never stops, so the head really does lay material "
                         "while it travels -- including the entry/exit tails at "
                         "every part and the linking fibre at a layer change. Off "
                         "(default) a travel deposits nothing, byte-identical. Each "
                         "travel is SPLICED into the fibre it leads into (never a "
                         "new path), so the per-scaffold grouping still lines up. "
                         "With the physics build's deposition transform installed "
                         "the travel is also laid at the jet's trailing touchdown "
                         "and the fibre after it INHERITS the lag state.")
    ap.add_argument("--sag-reset-layers", type=int,
                    default=_DEFAULT_SAG_RESET_LAYERS,
                    help="lay every Nth layer straight and let sagging re-"
                         "accumulate from it, the observed 'the fibres go straight "
                         "again every so many layers' behaviour (default: "
                         f"{_DEFAULT_SAG_RESET_LAYERS} = never reset, so droop "
                         "stays at its saturated depth once it gets there; 20 is a "
                         "typical period). A period, not a mechanism -- see "
                         "apply_sag")
    ap.add_argument("--sag-resolution", type=float, default=1.0,
                    metavar="FACTOR",
                    help="sample the sagged centreline FACTOR times more densely "
                         "(>= 1; default 1 = the shipped density). The droop law, "
                         "contacts and support heights are unchanged; the drape "
                         "converges, smoothing flank facets and the crest "
                         "shoulders (up to ~0.9 d low at 1), at roughly FACTOR x "
                         "the solve time. Part of the .depo cache key, so the "
                         "player must be given the same value")
    ap.add_argument("--no-self-crossings", action="store_true",
                    help="skip the same-layer self-contact pass (a fiber still "
                         "sags onto lower layers but not onto its own earlier "
                         "passes). Auto-enabled for string-art g-code; pass it "
                         "explicitly to force it on any file (default: auto)")
    ap.add_argument("--layer-by-deposition", action="store_true",
                    help="sag layers in g-code (deposition) order instead of by "
                         "global height, so a multi-scaffold stack printed by "
                         "scaffold number rests each scaffold on top of the last "
                         "rather than interleaving them by height. Byte-identical "
                         "for ascending-Z g-code (default: off = by height)")

    # --- build plate (the support the fibers are written on) --------------- #
    # A circular or rectangular plate positioned by its centre, drawn as a grey
    # sibling STL (<output>_plate.stl). --plate-top-z is the plate's top surface Z
    # (the deposition datum); the slab descends a thickness below it.
    ap.add_argument("--mandrel-diameter", type=float, default=None, metavar="MM",
                    help="the g-code was written for a ROTATING MANDREL of this "
                         "diameter: read the 4th axis A, solve on the cylinder, "
                         "and write the mesh wrapped around it. Without this a "
                         "4-axis file parses as a flat zigzag -- the rotation is "
                         "not in the axis whitelist, so it vanishes silently")
    ap.add_argument("--mandrel-surface-z", type=float, default=0.0, metavar="MM",
                    help="[--mandrel-diameter] the g-code Z that means the BARE "
                         "MANDREL SURFACE, i.e. what the machine was zeroed on "
                         "radially. 0 (the default) is a Z word that is wall "
                         "height above the surface; a program zeroed on the "
                         "rod's AXIS carries the rod radius here. Read without "
                         "it such a file solves as a tube one radius thick")

    # `scaffold_gcode` refuses a non-finite number below the same way, and
    # its check is the one used. A converter must import on its own, but a
    # CLI may need its sibling, and `scaffold_gcode` costs nothing here.
    import scaffold_gcode

    ap.add_argument("--plate", action="store_true",
                    help="add a build-plate slab beneath the fibers")
    ap.add_argument("--plate-shape", choices=("rectangular", "circular"),
                    default="rectangular", help="[--plate] plate shape")
    ap.add_argument("--plate-size-x", type=float, default=76.2,
                    help="[--plate rectangular] size X in mm (default: 76.2)")
    ap.add_argument("--plate-size-y", type=float, default=50.8,
                    help="[--plate rectangular] size Y in mm (default: 50.8)")
    ap.add_argument("--plate-diameter", type=float, default=50.0,
                    help="[--plate circular] diameter in mm (default: 50)")
    ap.add_argument("--plate-thickness", type=float, default=1.0,
                    help="[--plate] thickness (Z) in mm (default: 1.0)")
    ap.add_argument("--plate-center-x", type=float, default=38.1,
                    help="[--plate] centre X in mm (default: 38.1)")
    ap.add_argument("--plate-center-y", type=float, default=25.4,
                    help="[--plate] centre Y in mm (default: 25.4)")
    ap.add_argument("--plate-top-z", type=float, default=0.0,
                    help="[--plate] Z of the plate top surface (default: 0)")

    ap.add_argument("--origin-offset", type=float, nargs=3,
                    default=(0.0, 0.0, 0.0), metavar=("DX", "DY", "DZ"),
                    help="origin re-base offset already applied to the input "
                         "g-code; shifts the drawn plate to match (the fibres are "
                         "read from the g-code as-is)")

    # --- per-scaffold parts (for the viewer's per-scaffold toggle/colour) --- #
    # The fibers STL is always written whole (one merged mesh -- the export).
    # When given, --scaffold-groups ALSO writes a sibling STL per group so the
    # viewer can show/hide/colour each scaffold independently. The g-code carries
    # no markers (it must stay print-clean); instead the caller passes the path
    # (= layer) count of each group here, IN G-CODE ORDER, e.g. a stabilization
    # base then each scaffold. Format: a JSON list of {"name","layers"}.
    ap.add_argument("--scaffold-groups", default=None,
                    help="JSON list of {name, layers} naming consecutive runs of "
                         "fiber paths; writes a sibling <output>_<name>.stl per "
                         "group as a viewing aid (the whole STL is still written)")
    args = ap.parse_args(argv)
    if not 0.0 < args.diameter_multiplier < float("inf"):
        ap.error("--diameter-multiplier must be a positive number")
    global TUBE_RADIUS_MULT
    TUBE_RADIUS_MULT = args.diameter_multiplier

    # argparse's float() takes 'nan' and 'inf' (scaffold_gcode.main refuses
    # them the same way): -d nan died in the mesher, --sag-multiplier inf
    # divided by zero, --origin-offset nan exited 0
    for act in ap._actions:
        val = getattr(args, act.dest, None)
        if scaffold_gcode._nonfinite_numbers(val):
            ap.error(f"{'/'.join(act.option_strings) or act.dest} must be a "
                     f"finite number, got {val!r}")

    if args.faces < 3:
        ap.error("--faces must be at least 3")
    if args.faces > MAX_FACES:
        ap.error("tube facets is %d; the most is %d. At 20 um a tube of %d "
                 "facets is already round to the eye, and every facet more "
                 "adds triangles to every fiber, so %d would mesh for minutes "
                 "into a file nothing can open" % (args.faces, MAX_FACES,
                                                   MAX_FACES, args.faces))
    if args.mesh_tolerance < 0:
        ap.error("--mesh-tolerance must be 0 (off) or more")
    if args.diameter <= 0:
        ap.error("--diameter must be positive")
    if args.sag_multiplier < 0:
        ap.error("--sag-multiplier must be non-negative")
    if args.sag_reset_layers < 0:
        ap.error("--sag-reset-layers must be non-negative (0 = never reset)")
    if not args.sag_resolution >= 1.0:
        ap.error("--sag-resolution must be at least 1 (1 = shipped density)")
    # Before anything reads the .depo cache or solves: the key and the solve both
    # take it from the module (set_sag_resolution).
    set_sag_resolution(args.sag_resolution)
    if args.plate:
        if args.plate_shape == "circular":
            if args.plate_diameter <= 0:
                ap.error("--plate-diameter must be positive")
        elif args.plate_size_x <= 0 or args.plate_size_y <= 0:
            ap.error("--plate-size-x and --plate-size-y must be positive")
        if args.plate_thickness <= 0:
            ap.error("--plate-thickness must be positive")

    # The DEPOSITION FRAME. None (the default) is a flat collector and takes the
    # flat path untouched at every seam it reaches -- parse, sag, and the
    # wrap below, each branching on `is not None` rather than on a mode flag.
    # `mew_frame` is imported here, not at module scope: the flat path never uses
    # it.
    frame = None
    if args.mandrel_diameter is not None:
        if args.mandrel_diameter <= 0:
            ap.error("--mandrel-diameter must be positive")
        if args.plate:
            # A build plate is a flat collector. Drawing one under a tube would put
            # a plane through the mandrel and read as the datum the print rests on,
            # which is the mandrel SURFACE. Refused rather than ignored.
            ap.error("--plate is a flat-collector datum and has no meaning with "
                     "--mandrel-diameter; the collector IS the mandrel")
        import mew_frame
        frame = mew_frame.CylinderFrame(0.5 * args.mandrel_diameter,
                                        surface_z=args.mandrel_surface_z)
        print("Mandrel mode: d%.4f mm collector, circumference %.4f mm, "
              "bare surface at Z%.4f"
              % (args.mandrel_diameter, frame.period_v, frame.surface_z))

    five_reader = None

    # A five-axis run on the exact solver stays in the collector's world XYZ
    # from the read-back to the `.depo` (see the read-back note below).
    world_native = (frame is not None
                    and not getattr(frame, "chart_currency", False)
                    and not args.sag_approx)
    world_surface = (frame.surface() if world_native and not args.from_depo
                     else None)

    out_path = args.output or (args.input.rsplit(".", 1)[0] + ".stl")
    radius = args.diameter / 2.0
    section = None
    section_clipper = None
    if args.subsection:
        if not args.from_depo or args.no_mesh:
            ap.error("--subsection requires --from-depo and a mesh output")
        try:
            import mew_subsection
            with open(args.subsection, encoding="utf-8") as fh:
                section = json.load(fh)
            if mew_subsection.is_snapped(section):
                # a section wrapped on the rod is cut by the fibre's clock
                # (`snapped_spans`, below), not by planes; this checks it
                mew_subsection.clipping_planes(section)
            else:
                section_clipper = mew_subsection.ClosedClipper(section)
            if section.get("z_mode", "height") == "layers":
                layer_windows = section["layer_windows"]
                if not isinstance(layer_windows, list) or not layer_windows:
                    raise ValueError("missing print-order layer windows")
        except (OSError, ValueError, KeyError, ImportError) as exc:
            ap.error("cannot read subsection: %s" % exc)

    # --- the progress bar's stage plan ------------------------------------- #
    # Decided HERE, before any work, because the weights renormalise over the
    # stages that will actually run -- a --no-mesh run has to finish at 1.0.
    # Deferring the plan past the parse would mean emitting nothing during it.
    _stages = []
    if not args.from_depo:
        _stages.append("parse")
        if not args.no_sag:
            _stages.append("sag")
    if not args.no_mesh:
        _stages += ["mesh", "write"]
    prog = _Progress(_stages, enabled=args.progress)

    # Tell the GUI the input g-code is ready to play / simulate, BEFORE the slow
    # sag + mesh build, so the simulation buttons enable immediately. The GUI
    # output reader watches for this exact prefix (GCODE_READY_MARKER in
    # gui_run.py). Chained from scaffold_gcode the file is announced twice, which
    # is harmless -- the handler is idempotent.
    if os.path.isfile(args.input):
        print(f"[GCODE-READY] {args.input}", flush=True)

    detected = args.mode
    if detected == "auto":
        detected = "e" if file_uses_extrusion(args.input) else "g1"
    # Per-fibre linking-loop flag for the sag solve's entry-continuity (set in the
    # parse branch on the COMMANDED paths, before any transform rounds the closure).
    # None in the --from-depo branch (the cache is already sagged -- no solve).
    loop_entry = None
    if args.from_depo:
        # On-demand exact build: load the already-sagged (already fibre-transformed)
        # fibres from the cache; skip parse + transform + sag (and don't rewrite the
        # cache). The cache's per-fibre diameters (pdia) and the dynamic diam-by-time
        # drive the tube radius below, so a physics-deposited / multi-diameter part
        # rebuilds exactly without re-solving.
        print(f"Re-tubing from {args.from_depo} (skipping parse + sag)")
        cached = _load_depo_raw(args.from_depo)
        if cached is None:
            print(f"Could not read --from-depo cache: {args.from_depo}",
                  file=sys.stderr)
            return 1
        paths, ptimes = cached
        if section is not None and section.get("z_mode", "height") == "layers":
            try:
                layer_times = ptimes
                try:
                    sub_frame = None
                    if section.get("mandrel_radius"):
                        import mew_frame
                        sub_frame = mew_frame.CylinderFrame(
                            float(section["mandrel_radius"]),
                            surface_z=float(section.get("mandrel_surface_z", 0.0)))
                    _commanded, layer_times = parse_gcode(
                        args.input, mode=detected, with_times=True,
                        arc_segments=args.arc_segments, frame=sub_frame)
                    if len(layer_times) != len(paths):
                        layer_times = ptimes
                except (OSError, ValueError):
                    layer_times = ptimes
                section["path_spans"] = mew_subsection.selected_time_spans(
                    layer_times, section["layer_windows"],
                    int(section["layer_start"]), int(section["layer_end"]))
            except (KeyError, TypeError, ValueError) as exc:
                ap.error("cannot read subsection layer range: %s" % exc)
        depo_pdia = load_depo_pdia(args.from_depo)
        fiber_radius = [d / 2.0 for d in depo_pdia] if depo_pdia else None
        diam_by_time = load_depo_diam_by_time(args.from_depo)
        # The travel intervals the run that WROTE this cache recorded: no second
        # source exists here, since the parse that produces `travels` is exactly
        # what this branch skips. A cache written before the sidecar returns
        # None -> [], which leaves the travel in the mesh.
        travel_spans = load_depo_travel_spans(args.from_depo) or []
        t_parse = 0.0
    else:
        prog.stage("parse", "Reading g-code")
        print(f"Parsing {args.input} ... (mode: {detected})")
        # parse with times too: negligible cost, and it lets us write the deposition
        # cache so gcode_player doesn't have to redo the sag solve. Pass the resolved
        # `detected` mode so parse_gcode doesn't re-scan the file for E-words.
        _t0 = time.perf_counter()
        # Travels are parsed when the user asks to render them, OR when the
        # installed transform wants them (the physics continuous-jet export).
        want_travels = bool(args.render_travel) or _FIBER_TRANSFORM_TRAVELS
        travels = None
        # (t_start, t_end) of every merged travel: how BOTH consumers find the
        # travel material again after the solve -- the STL trim below, and
        # gcode_player's show/hide. TIME and not a point index, because apply_sag
        # resamples and `_split_layer_spanning` can dice one fibre into several,
        # so an index recorded at merge time means nothing afterwards.
        travel_spans = []
        # a word the parser refuses (G20, an R-form arc, a NaN, an unread
        # rotary word) is the FILE's problem: one line, as `main` reports a
        # chart refusal, not a traceback that reads as a crash
        try:
            if want_travels:
                paths, ptimes, travels = parse_gcode(
                    args.input, mode=detected, with_times=True,
                    arc_segments=args.arc_segments, with_travels=True,
                    frame=frame, five_axis=five_reader)
            else:
                paths, ptimes = parse_gcode(args.input, mode=detected,
                                            with_times=True,
                                            arc_segments=args.arc_segments,
                                            frame=frame,
                                            five_axis=five_reader)
        except ValueError as exc:
            import mew_frame
            if isinstance(exc, mew_frame.ChartUnusable):
                raise
            # the reason quotes the offending line; the path is a scratch
            # file's when the G-code window runs this, so it is not named
            ap.error("this g-code cannot be read: %s" % (exc,))
        t_parse = time.perf_counter() - _t0
        fiber_radius = None     # grouped/global/dynamic radius resolved below

        # Flag linking-loop fibres NOW, on the commanded centrelines: the loop
        # closes on its exact start vertex here, but the physics transform below
        # rounds that closure away. Carried into apply_sag so a loop's entry stays
        # continuous with the layer beneath instead of severing one diameter up.
        loop_entry = loop_entry_flags(paths)

        # Optional fibre-transform seam (None on the plain CLI): the
        # physics layer reshapes the commanded centrelines into the jet's trailing
        # touchdown path here, so everything downstream -- grouping, sag, the tube
        # mesh, the deposition-cache sidecar -- is the fibre as actually laid. Fibre
        # count is preserved, so the grouping below still matches. See the seam note
        # above set_fiber_transform.
        # The transform may also return a per-print diameter-vs-time lookup (the
        # physics dynamic-diameter feature) as an optional 3rd element; when present
        # it makes the tube radius vary along each fibre with the head's speed. A
        # plain 2-tuple (an unset seam / a non-dynamic transform) leaves it None.
        diam_by_time = None
        # THE COMMANDED Z LEVELS, snapshotted HERE -- before the transform reshapes
        # the centrelines and before any travel is spliced in. The layer-collapse
        # warning below is about the FILE ("is --diameter larger than your layer
        # height? Check units"), so it has to be measured on the file's own
        # geometry. Measured after the transform it counted physics-reshaped
        # medians instead: a jet-lag of ~1.1 mm moves a fibre's median off the
        # commanded grid, so a sag ladder whose 21 layers are perfectly regular
        # reported "22 commanded-Z levels collapsed into 21" and sent the reader
        # hunting a units bug that was not there. The layer
        # COUNT was right in every case; only the warning was wrong.
        commanded_z = {round(float(np.median(np.asarray(p)[:, 2])), 6)
                       for p in paths if len(p)}
        if _FIBER_TRANSFORM is not None and paths:
            # A travel-aware transform lays the travels ITSELF (the jet trails
            # through them, so their geometry is physics-shaped and the fibre it
            # feeds inherits the lag state) and merges them in; a plain transform
            # gets the commanded travels already spliced (below).
            if _FIBER_TRANSFORM_TRAVELS:
                if _FIBER_TRANSFORM_FRAME:
                    res = _FIBER_TRANSFORM(paths, ptimes, travels, frame=frame)
                else:
                    res = _FIBER_TRANSFORM(paths, ptimes, travels)
            else:
                if args.render_travel:
                    paths, ptimes = merge_travels(paths, ptimes, travels)
                if _FIBER_TRANSFORM_FRAME:
                    res = _FIBER_TRANSFORM(paths, ptimes, frame=frame)
                else:
                    res = _FIBER_TRANSFORM(paths, ptimes)
            if isinstance(res, tuple) and len(res) == 3:
                paths, ptimes, diam_by_time = res
            else:
                paths, ptimes = res
        elif args.render_travel:
            # No transform (plain converter / physics deposition off): the travel
            # is still real deposited material, just laid on its commanded line.
            paths, ptimes = merge_travels(paths, ptimes, travels)

        # THE SECOND HALF OF THE FIVE-AXIS READ-BACK. `FiveAxisReader` answers
        # in the collector's own XYZ, and on the EXACT solver the rest of the
        # run stays there: `apply_sag(world=True)` solves on the collector
        # itself, so there is no unwrap here and no wrap after it. The switch
        # is the frame's capability (`chart_currency` False: its read-back is
        # world), as `mew_deposition._world_surface` decides it, not its shape.
        #
        # The APPROXIMATE solver still works in the surface chart `(u, v, h)`,
        # so for it `mew_frame.unwrap_path` is the seam in, as `wrap_path` is
        # the seam out. A chart it cannot build (an imported solid) refuses
        # there.
        #
        # THE UNWRAP SITS AFTER THE TRANSFORM, NOT BEFORE IT, AND THAT IS THE
        # WHOLE POINT. `mew_deposition.laid_fibers` documents its own input currency
        # as WORLD XYZ in the collector's frame and converts once itself
        # (`chart = chart_of(frame); paths = chart.from_world(paths)`,
        # mew_deposition.py). Unwrapping here first handed it a path already in
        # the chart, which it then converted a SECOND time -- harmless on the
        # 4-axis mandrel, where `GlobalChart.from_world` is the identity, and
        # ruinous on a `PolarChart` (dome, bowl, torus), where it is not. Note
        # it is not "on a developable frame": a cone is developable and the
        # identity is ruinous there too.
        # Measured on the shipped 6 x 4 mm dome design: the path the physics
        # received was displaced a mean of 13.49 mm (max 23.47 mm) from the
        # commanded one, `u` collapsed to +/-0.0003 mm and `h` ran -10.000 to
        # +17.266 mm, which is the drawn "blended mess of fibres" and the
        # 4089 um reported jet lag. `gui_surface.build_surface_preview` already
        # hands the same transform `pp.world` and says why in its own docstring
        # ("feeding it `pp.uv` is what made a dome pole unusable") -- that is
        # why the sidebar preview looked correct while the player did not, and
        # the two relays now agree.
        #
        # Nothing between the old site and this one reads the chart: the loop
        # flags test a closure, `merge_travels` only needs both sides in ONE
        # currency, and `travel_spans` is times. It is placed before the
        # `--render-travel` report so that report still measures chart XY and
        # its printed figure is unchanged.
        if five_reader is not None and not world_native:
            import mew_frame
            paths = [mew_frame.unwrap_path(frame, p) for p in paths]
            if travels:
                travels = [None if tv is None
                           else (mew_frame.unwrap_path(frame, tv[0]), tv[1])
                           for tv in travels]
        if want_travels:
            travel_spans = [(float(tv[1][0]), float(tv[1][-1]))
                            for tv in (travels or [])
                            if tv is not None and tv[1] is not None
                            and len(tv[1]) >= 2]
        if args.render_travel:
            # Report what was ACTUALLY deposited, and say so when it is nothing: a
            # single scaffold starting at the origin has only in-place Z-hop
            # travels at layer changes, which deposit no visible fibre -- correct,
            # but indistinguishable from a broken toggle unless said out loud.
            n_tv = sum(1 for tv in (travels or [])
                       if tv is not None and tv[0] is not None)
            xy = 0.0
            for tv in (travels or []):
                if tv is not None and tv[0] is not None:
                    d = np.diff(np.asarray(tv[0], dtype=np.float64)[:, :2], axis=0)
                    xy += float(np.linalg.norm(d, axis=1).sum())
            if n_tv and xy > 1e-6:
                print(f"  travel (G0) rendered as deposited fibre: {n_tv} travel(s),"
                      f" {xy:.2f} mm of XY travel")
            else:
                print(f"  travel (G0) rendering ON, but nothing to draw: {n_tv} "
                      f"travel(s) with {xy:.3f} mm of XY motion (in-place layer-"
                      f"change Z-hops deposit no visible fibre; travel shows on a "
                      f"stabilization base, a multi-scaffold print, or imported "
                      f"g-code)")

    total_pts = sum(len(p) for p in paths)
    n_fibers = len(paths)
    print(f"  found {len(paths)} fiber path(s), {total_pts} extruding points "
          f"[{t_parse:.2f}s]")

    if not paths:
        # A refusal (exit 2, argparse's shape) the GUI quotes as the reason,
        # naming the likely cause when it is one a file from another tool has
        print("%s: error: %s" % (os.path.basename(sys.argv[0] or
                                                  "gcode_to_fibers.py"),
                                 _no_fibers_reason(args.input, args.mode)),
              file=sys.stderr)
        return 2

    # A flat file printing BELOW the plate's top is a nozzle driven into the
    # plate on a real printer (or a Z datum set wrong here): said, not drawn
    # silently inside the slab. A fibre's underside rests at its commanded Z
    # (`bed_at`), so the commanded Z itself is compared with the top.
    if args.plate and frame is None and five_reader is None:
        top = args.plate_top_z + args.origin_offset[2]
        low = min(float(np.min(np.asarray(p)[:, 2])) for p in paths)
        if low < top - 1e-3:
            print(f"  warning: this file prints down to Z {low:g} mm, "
                  f"{top - low:g} mm below the plate's top (Z {top:g}): "
                  f"on the printer the nozzle would run into the plate. Check "
                  f"the file's Z offset, or the plate's top Z on the Setup "
                  f"page.", file=sys.stderr)

    # --- per-scaffold grouping --------------------------------------------- #
    # Resolved BEFORE sagging because each scaffold may sag at its OWN fiber
    # diameter. A group index for each parsed path (one path per layer, in
    # g-code order); sum(layers) must match the path count, else the mapping is
    # unreliable -> fall back to no parts (one merged STL, global diameter).
    groups = None
    path_group = None
    if args.scaffold_groups:
        try:
            groups = json.loads(args.scaffold_groups)
        except ValueError:
            groups = None
        if groups:
            want = sum(int(g["layers"]) for g in groups)
            if want != len(paths):
                print(f"  note: scaffold groups cover {want} path(s) but the "
                      f"file has {len(paths)} -- skipping per-scaffold parts",
                      file=sys.stderr)
                groups = None
        if groups:
            path_group = []
            gi, left = 0, int(groups[0]["layers"])
            for _ in range(len(paths)):
                while left <= 0 and gi + 1 < len(groups):
                    gi += 1
                    left = int(groups[gi]["layers"])
                path_group.append(gi)
                left -= 1

    # A --from-depo rebuild is launched WITHOUT --scaffold-groups, but the .depo
    # stored the per-fibre grouping. Recover it so the per-scaffold sibling STLs
    # regenerate and the sweep below keeps them, instead of collapsing a
    # multi-scaffold print into one merged part and deleting its
    # <stem>_scaffoldN.stl. Names are enough: the tube radius still comes from the
    # .depo's own per-fibre diameter.
    if groups is None and args.from_depo:
        rec = load_depo_groups(args.from_depo)
        if rec is not None:
            rec_pg, rec_names = rec
            if rec_names and len(rec_pg) == len(paths):
                path_group = rec_pg
                groups = [{"name": n} for n in rec_names]

    # Per-path fiber diameter for the sag solve: each scaffold's own
    # fiber_diameter (from --scaffold-groups), falling back to the global -d for
    # any path not covered by a positive-diameter group. None when ungrouped ->
    # apply_sag uses the single global diameter exactly as before.
    path_diam = None
    if path_group is not None:
        gdia = []
        for g in groups:
            try:
                gd = float(g.get("fiber_diameter", 0.0))
            except (TypeError, ValueError):
                gd = 0.0
            gdia.append(gd if gd > 0.0 else args.diameter)
        path_diam = [gdia[gi] for gi in path_group]
    # the finest diameter present drives layer clustering / "is it saggable"
    sag_diam = min(path_diam) if path_diam else args.diameter

    # --- sagging ----------------------------------------------------------- #
    t_sag = 0.0
    if args.from_depo:
        # already sagged + transformed in the cache -- no solve, no rewrite
        sagged_pts = total_pts
    else:
        # Same-layer self-contact is skipped when forced (--no-self-crossings) or
        # when the g-code asks for it (string art -- thousands of coplanar self-
        # crossings that are meaningless to resolve and ruinous to compute).
        self_crossings = not (args.no_self_crossings
                              or file_skips_self_crossings(args.input))
        if world_native:
            applicable, n_layers = _sag_applicable(
                paths, sag_diam, self_crossings=self_crossings, frame=frame,
                world=True)
            # HEIGHT ABOVE THE COLLECTOR, not Z. World-native five-axis paths
            # have no Z grid to count: the layer coordinate is the offset from
            # the deposition surface, so the levels are read there. The
            # `commanded_z` snapshot the flat branch uses does not apply --
            # it is a set of Z medians, which on a dome are one per pass.
            distinct_z = len({round(float(np.median(p[:, 2])), 6)
                              for p in surface_heights(paths, world_surface)})
        else:
            applicable, n_layers = _sag_applicable(
                paths, sag_diam, self_crossings=self_crossings,
                period=None if frame is None else frame.period_v)
            # the FILE's levels, snapshotted before the transform (see
            # `commanded_z`) -- not the reshaped paths', which carry the jet
            # lag and never sit on a grid
            distinct_z = len(commanded_z)
        if not args.no_sag and distinct_z > max(n_layers, 1):
            print(f"  warning: {distinct_z} commanded-Z levels collapsed into "
                  f"{n_layers} layer(s) -- is --diameter ({sag_diam}) larger "
                  f"than your layer height? Check units (mm vs um).",
                  file=sys.stderr)
        if args.no_sag or not applicable:
            if args.no_sag:
                print(f"  sagging: disabled ({n_layers} layer(s))")
            else:
                print(f"  sagging: skipped ({n_layers} layer, no self-"
                      f"crossings); nothing to sag onto")
            # No solver ran, so nothing rested these fibres on the bed. Do it
            # here, or every underside sits half a fibre inside the plate.
            paths = lift_to_underside(paths, path_diam, args.diameter,
                                      surface=world_surface,
                                      point_diam=point_diameters(
                                          ptimes, diam_by_time))
        else:
            detail = (f"{n_layers} layers" if n_layers >= 2
                      else "1 layer, self-contact")
            if not self_crossings:
                detail += ", self-contact off"
            if args.sag_reset_layers >= 1:
                detail += f", reset every {args.sag_reset_layers}"
            if args.sag_resolution != 1.0:
                detail += f", resolution x{args.sag_resolution:g}"
            if args.sag_approx:
                detail += ", APPROXIMATE solver"
            print(f"  sagging: {detail}, x{args.sag_multiplier:g} "
                  f"(pore-size driven)")
            _t0 = time.perf_counter()
            prog.stage("sag", "Solving the sag")
            # THE SAG / DRAWN-CURVE SPLIT. The adaptive corner grid exists to
            # smooth what is DRAWN; the sag solver resamples the centreline on its
            # own contact-sweep step either way, so feeding it the refined grid
            # buys nothing and costs a share of the run's dominant stage. Solve on
            # the seed grid, then carry Z back onto the drawn fibre. `sag_grid` is
            # None -- a single call -- whenever the corner grid is
            # off, which is every flat export. The resulting Z is the seed grid's,
            # a smaller move than the tier choice already makes: 13.5 um at worst
            # against the 13.2 um that switching Standard -> Draft moves it by.
            _sag_grid = getattr(_FIBER_TRANSFORM, "sag_grid", None)
            _drawn, _drawn_t = paths, ptimes
            if _sag_grid:
                paths, ptimes = split_for_sag(paths, ptimes, _sag_grid)
                n_solve = sum(len(p) for p in paths)
                if n_solve < total_pts:
                    print(f"  sag solves on the seed grid: {total_pts} -> "
                          f"{n_solve} points (the drawn fibre keeps all "
                          f"{total_pts})")
            _why = sag_budget_problem(paths, args.diameter,
                                      exact=not args.sag_approx)
            if _why:
                sys.exit(f"error: {_why}. Export with No sag, or edit the "
                         f"text down.")
            if args.sag_approx:
                # The approximate engine solves in DEPOSITION order by
                # construction, so --layer-by-deposition is already its
                # behaviour rather than a flag it takes; --no-self-crossings has
                # no counterpart (its self-contact is the narrower detected-XY-
                # crossing rule either way -- see sag_approx's departures list).
                if diam_by_time is not None:
                    # MEASURED: with the diameter varying along the fibre the two
                    # solvers part company by ~0.25 d of stack height, against
                    # 0.02 d with a uniform diameter, because the approximate
                    # engine takes ONE diameter. Say so rather than let it read as
                    # the sub-micrometre agreement the uniform case earns.
                    print("  note: approximate sag + dynamic diameter -- the "
                          "approximate solver sags at one diameter, so the "
                          "stack height departs from the exact solve by ~0.25 "
                          "fibre diameters. Use Exact for a final export.")
                paths, ptimes = apply_sag_approx(
                    paths, args.diameter,
                    sag_multiplier=args.sag_multiplier,
                    point_times=ptimes, path_diam=path_diam,
                    sag_reset_layers=args.sag_reset_layers,
                    frame=frame,
                    progress=prog.tick)
            else:
                paths, ptimes = apply_sag(paths, args.diameter,
                                          sag_multiplier=args.sag_multiplier,
                                          point_times=ptimes,
                                          self_crossings=self_crossings,
                                          path_diam=path_diam,
                                          loop_entry=loop_entry,
                                          bed_diam_by_time=diam_by_time,
                                          sag_reset_layers=args.sag_reset_layers,
                                          layer_by_deposition=args.layer_by_deposition,
                                          frame=frame, progress=prog.tick,
                                          world=world_native)
            if _sag_grid:
                paths, ptimes = carry_sag_onto_drawn(paths, ptimes,
                                                     _drawn, _drawn_t,
                                                     surface=world_surface)
            t_sag = time.perf_counter() - _t0
            print(f"  sagged {total_pts} -> {sum(len(p) for p in paths)} points "
                  f"[{t_sag:.2f}s]")
        sagged_pts = sum(len(p) for p in paths)

        # --- the wrap ------------------------------------------------------ #
        # Everything above works in the surface frame (u, v, h), where a helix is a
        # STRAIGHT LINE and the flat solver's geometry is exact; everything below
        # draws or measures in space, so the fibre is wrapped around the mandrel
        # HERE, once, at the single seam between the two. It DENSIFIES: a segment
        # straight in (u, v) is a helix in space, and its two endpoints alone would
        # cut a chord through the collector. The times ride along through
        # `wrap_path`, or a caller keeping the old times would hand the reveal two
        # arrays of different lengths and misalign silently. The result is in the
        # COLLECTOR'S OWN co-rotating frame: deposited fibre is static there, which
        # lets the viewer spin one actor instead of rewriting point arrays.
        # A world-native run is already there: nothing to wrap.
        if frame is not None and paths and not world_native:
            _t0 = time.perf_counter()
            import mew_frame
            _w = [mew_frame.wrap_path(frame, p, t) for p, t in zip(paths, ptimes)]
            paths = [w[0] for w in _w]
            ptimes = [w[1] for w in _w]
            print(f"  wrapped onto the mandrel: {sagged_pts} -> "
                  f"{sum(len(p) for p in paths)} points "
                  f"[{time.perf_counter() - _t0:.2f}s]")
            sagged_pts = sum(len(p) for p in paths)

        # deposition cache (sidecar): lets the player + viewers skip this sag
        # solve. path_diam and diam_by_time ride along so the viewers tube each
        # scaffold at ITS size. These fibres are a NEW solve, so any companion
        # beside the cache describes the old one -- drop it HERE, before the
        # --no-mesh return below can leave it there. The decimation block further
        # down writes the fresh one.
        drop_depo_decimation(os.path.splitext(out_path)[0] + DEPO_CACHE_EXT)
        write_depo_cache(os.path.splitext(out_path)[0] + DEPO_CACHE_EXT,
                         paths, ptimes, diameter=args.diameter, no_sag=args.no_sag,
                         sag_multiplier=args.sag_multiplier,
                         self_crossings=self_crossings, diam_by_time=diam_by_time,
                         transformed=_FIBER_TRANSFORM is not None,
                         phys_sag=_SAG_LAW is not None,
                         phys_fuse=_FUSION_LAW is not None,
                         path_diam=path_diam, path_group=path_group,
                         sag_reset_layers=args.sag_reset_layers,
                         # so a --from-depo rebuild can cut the travel out of
                         # the STL on the same intervals this run does below
                         travel_spans=travel_spans,
                         # A travel-aware transform lays and merges the travels
                         # ITSELF, so this cache DOES contain deposited travel
                         # geometry even when args.render_travel is False.
                         # Stamped from the same disjunction `want_travels` used
                         # to PARSE them, so a consumer that asked for
                         # render-travel accepts this cache instead of falling
                         # back to the commanded path.
                         render_travel=bool(args.render_travel)
                                       or _FIBER_TRANSFORM_TRAVELS,
                         # stamped AS ASKED, not as run -- the same convention
                         # `no_sag` above follows. A consumer knows which solver
                         # it wants, not whether this file turned out to have
                         # anything to sag, so keying on the request is what
                         # lets the two sides agree.
                         sag_approx=bool(args.sag_approx),
                         group_names=([g.get("name", f"scaffold{i + 1}")
                                       for i, g in enumerate(groups)]
                                      if groups else None))

    # --no-mesh: the .depo cache (above) is all the viewers need to render the
    # scaffold as wireframe/tubes; skip the expensive tube-mesh build + STL write
    # (+ siblings/plate) entirely. The exact STL is built later on demand via
    # --from-depo (e.g. export), which never re-runs the sag solve.
    if args.no_mesh:
        # the plate is a viewing aid independent of the fibre mesh -- write it even
        # on a preview run, else the viewers lose their show/hide-plate toggle
        _write_build_plate(args, out_path)
        print(f"Skipped mesh build (--no-mesh): {sagged_pts} sagged points cached")
        prog.finish()
        return 0

    # Per-group tube radius: each scaffold may carry its own "fiber_diameter" in
    # --scaffold-groups, so a multi-scaffold file renders every scaffold at ITS
    # diameter instead of the global -d. A group without a positive
    # fiber_diameter falls back to the global radius.
    group_radius = None
    if groups:
        group_radius = []
        for g in groups:
            try:
                gd = float(g.get("fiber_diameter", 0.0))
            except (TypeError, ValueError):
                gd = 0.0
            group_radius.append(gd / 2.0 if gd > 0.0 else radius)

    def _radius_for(i, tvals):
        """Tube radius for fibre i, on whatever sampling `tvals` describes.

        Each scaffold's fibers get their own radius when grouped; with a
        diameter-vs-time lookup the radius VARIES along the fibre with the head's
        speed, evaluated at each point's print time, so a slower loop lands thicker
        than the fast straights. Taking the print times as an argument is what lets
        it be re-evaluated on the decimated sampling below rather than resampled by
        index."""
        if diam_by_time is not None and tvals is not None:
            return 0.5 * np.interp(tvals, diam_by_time[0], diam_by_time[1])
        if fiber_radius is not None:            # --from-depo: per-fiber cache diam
            return fiber_radius[i]
        if path_group is not None and group_radius is not None:
            return group_radius[path_group[i]]
        return radius

    # Thin the centreline to what a `faces`-gon tube can express before spending a
    # ring on every sag sample. The .depo cache above already holds the FULL
    # sampling, so the player, the deviation view and the analysis engine are
    # unaffected; only the STL gets the cheaper sampling. `--no-sag` is left alone:
    # it is the "render exactly what was commanded" mode used to diff solvers, and
    # keeping it byte-identical preserves a clean reference.
    prog.stage("mesh", "Building the mesh")
    mesh_paths, mesh_times = paths, ptimes
    # A run that does NOT decimate must not leave the PREVIOUS run's companion
    # lying next to the .depo. Already dropped above for a run that re-solved;
    # this is the --from-depo case, where the cache is the previous run's and only
    # the SETTINGS have moved on.
    if section is None and not (args.mesh_tolerance > 0.0 and not args.no_sag):
        drop_depo_decimation(os.path.splitext(out_path)[0] + DEPO_CACHE_EXT)
    if args.mesh_tolerance > 0.0 and not args.no_sag:
        _rad0 = [_radius_for(i, ptimes[i] if ptimes else None)
                 for i in range(len(paths))]
        _t0 = time.perf_counter()
        mesh_paths, mesh_times, _dec, _keep = decimate_for_mesh(
            paths, _rad0, args.faces, args.mesh_tolerance, times=ptimes,
            return_keep=True)
        _t_dec = time.perf_counter() - _t0
        # Hand the result to the player instead of making it repeat the solve: the
        # Full-mesh view thins the SAME centrelines at the SAME tolerance.
        if section is None:
            write_depo_decimation(
                os.path.splitext(out_path)[0] + DEPO_CACHE_EXT, _keep,
                mult=args.mesh_tolerance, faces=args.faces,
                n_in=sum(len(p) for p in paths))
        if _dec[1] < _dec[0]:
            print(f"  mesh sampling: {_dec[0]} -> {_dec[1]} points "
                  f"({_dec[0] / max(1, _dec[1]):.1f}x fewer rings, max centreline "
                  f"deviation {_dec[2] * 1000.0:.3f} um) [{_t_dec:.2f}s]")

    # The EXPORTED mesh drops the travel spans unless --travel-in-stl asked for
    # them. AFTER the decimation above, whose companion is written for the player,
    # and the player wants the travel -- so the thinning must be solved on the
    # fibres that have it. Only the tubing below is cut.
    #
    # What this does NOT do: the travel was still merged BEFORE the solve, so the
    # scaffold fibres here were sagged with it holding them up -- the truthful
    # answer, and the same geometry the player draws.
    #
    # Cut by print TIME, not by index (see `travel_spans` above). A fibre left with
    # fewer than two points comes back empty so fibre count, `path_group` and the
    # per-fibre radius stay aligned; the tube loop skips it.
    if travel_spans and not args.travel_in_stl and mesh_times:
        _n_before = sum(len(a) for a in mesh_paths)
        # A hop of up to two fibre widths is the layer step, not a travel: it
        # stays, or every layer change is a cut (see `_drop_travel_spans`).
        _keep_xy = [2.0 * (path_diam[i] if path_diam else args.diameter)
                    for i in range(len(mesh_paths))]
        mesh_paths, mesh_times = _drop_travel_spans(
            mesh_paths, mesh_times, travel_spans, keep_xy=_keep_xy)
        print("  travel fibre: %d points solved with the print but left out of "
              "the STL (--travel-in-stl includes them)"
              % (_n_before - sum(len(a) for a in mesh_paths)))

    # Say how big the files will be BEFORE writing any (--max-stl-bytes): a
    # 30x30 mm, 60-layer, 20 um plate wrote a 9.75 GB STL (195M triangles)
    # that nothing a researcher owns opens, with no warning. The count is
    # tube_mesh's own, on the thinned centrelines the loop below tubes.
    if args.max_stl_bytes is not None:
        n_bytes, n_tri = stl_size_estimate(mesh_paths, args.faces,
                                           path_group if groups else None)
        if n_bytes > args.max_stl_bytes:
            print("STL_TOO_BIG %d %d" % (n_bytes, n_tri))
            sys.stdout.flush()
            return STL_TOO_BIG_EXIT

    # Each fibre's tube is written as soon as it is built (`_StlStream`), so the
    # whole mesh is never held: a 30x30 mm, 60-layer, 200 um scaffold's mesh
    # did not fit in 3.5 GB held whole. Byte-identical to building it whole.
    # Each part STL streams beside it, its own mesh indexed from 0.
    stem = os.path.splitext(out_path)[0]
    stl_out = _StlStream(out_path)
    g_out = [_StlStream(f"{stem}_{g['name']}.stl") for g in (groups or ())]
    _t0 = time.perf_counter()
    for i, p in enumerate(mesh_paths):
        prog.tick(i, len(mesh_paths))
        spans = (section["path_spans"][i]
                 if section is not None and section.get("z_mode", "height") == "layers"
                 else [None])
        if not spans:
            continue
        if section is not None and mew_subsection.is_snapped(section):
            if not mesh_times or len(mesh_paths[i]) < 2:
                continue
            spans = mew_subsection.snapped_spans(section, p, mesh_times[i], spans)
            if not spans:
                continue
        r = _radius_for(i, mesh_times[i] if mesh_times else None)
        if section_clipper is not None:
            if len(p) < 2:
                continue
            margin = float(np.max(r)) * TUBE_RADIUS_MULT
            if not section_clipper.intersects_bounds(
                    np.min(p, axis=0) - margin, np.max(p, axis=0) + margin):
                continue
        full_v, full_t = tube_mesh(p, r, args.faces, 0)
        for span in spans:
            v, t = full_v, full_t
            if span is not None and len(v):
                ring_times = np.asarray(mesh_times[i], dtype=np.float64)[_dedupe_idx(p)]
                v, t = mew_subsection.cut_mesh_time(
                    v, t, ring_times, args.faces, span[0], span[1])
            if section_clipper is not None and len(v):
                v, t = section_clipper.clip(v, t)
            if len(v) == 0:
                continue
            stl_out.add(v, t)
            if path_group is not None:
                g_out[path_group[i]].add(v, t)

    if section is not None and not stl_out.n_tri:
        stl_out.discard()
        print("Subsection contains no deposited fibre.", file=sys.stderr)
        return 1
    # the tubing and the writing are one pass now; t_tube carries both
    t_tube = time.perf_counter() - _t0

    print(f"Building mesh: {stl_out.n_vert} vertices, {stl_out.n_tri} triangles "
          f"[{t_tube:.2f}s]")
    _t0 = time.perf_counter()
    prog.stage("write", "Writing the STL")
    stl_out.close(always=True)
    t_write = time.perf_counter() - _t0
    print(f"Wrote {out_path} [{t_write:.2f}s]")

    if section is not None:
        prog.finish()
        return 0

    # Per-scaffold sibling STLs (viewing aids; the merged STL above is the export).
    # The siblings beside out_path must reflect ONLY this run, so sweep stale parts
    # first. This MUST run even when this run writes no parts: the GUI reuses one
    # scratch path, so regenerating a single scaffold over a former multi-scaffold
    # print would orphan its <stem>_scaffoldN.stl -- and the viewer prefers those
    # siblings over the merged STL, so it would draw the OLD model.
    _t0 = time.perf_counter()
    # The parts this run actually writes survive; every other part-named sibling is
    # swept. glob's results and our constructed names can differ in path
    # separator/case on Windows, so compare them normalised -- otherwise a kept
    # part is deleted and rewritten, logging a misleading "Removed stale".
    def _norm(p):
        return os.path.normcase(os.path.normpath(p))
    keep = {_norm(g_out[gi].path)
            for gi, g in enumerate(groups or ()) if g_out[gi].n_vert}
    # the glob catches <stem>_scaffoldN; the stab part isn't named "scaffold*"
    stale = set(glob.glob(glob.escape(stem) + "_scaffold*.stl"))
    stale.add(stem + "_stab.stl")
    for old in stale:
        if _norm(old) in keep or not os.path.isfile(old):
            continue
        try:
            os.remove(old)
            print(f"Removed stale {old}")
        except OSError:
            pass
    if groups:
        for gi, g in enumerate(groups):
            if g_out[gi].close():
                print(f"Wrote {g_out[gi].path} ({g['name']} part: "
                      f"{g_out[gi].n_tri} triangles)")
    t_write += time.perf_counter() - _t0

    # --- optional build plate (viewing aid; see _write_build_plate) -------- #
    # A --from-depo re-tube is a derivative mesh-only rebuild carrying no --plate
    # args, so it must NOT touch the plate the original run wrote -- only a real
    # generation run owns the plate sibling.
    if not args.from_depo:
        _t0 = time.perf_counter()
        _write_build_plate(args, out_path)
        t_write += time.perf_counter() - _t0

    # --- timing recap ------------------------------------------------------ #
    # TOTAL is the sum of the four measured stages (parse, sag, tube, write);
    # untracked bits -- the cache sidecar, arg parsing -- are not broken out, so
    # the percentages are shares of that sum, not of wall time.
    _print_timing_recap(n_fibers, total_pts, sagged_pts,
                        stl_out.n_vert, stl_out.n_tri,
                        t_parse, t_sag, t_tube, t_write)
    prog.finish()
    return 0


def _fmt_count(n):
    """A compact magnitude string: 14.35M / 7.18M for big counts, commas below."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    return f"{n:,}"


def _print_timing_recap(n_fibers, raw_pts, sagged_pts, n_verts, n_tris,
                        t_parse, t_sag, t_tube, t_write):
    """Print the stage-timing table (see main)."""
    total = t_parse + t_sag + t_tube + t_write
    pct = (lambda t: 100.0 * t / total) if total > 0 else (lambda t: 0.0)
    rule = "-" * 46
    print()
    print(f"fibers={n_fibers}   raw_pts={raw_pts:,}   "
          f"sagged_pts={sagged_pts:,}")
    print(f"mesh: {_fmt_count(n_verts)} verts, {_fmt_count(n_tris)} tris")
    print(rule)
    for name, t in (("parse", t_parse), ("sag solve", t_sag),
                    ("tube mesh", t_tube), ("stl write", t_write)):
        print(f"{name:<11s} {t:7.3f}s  {pct(t):5.1f}%")
    print(rule)
    print(f"{'TOTAL':<11s} {total:7.3f}s")


if __name__ == "__main__":
    raise SystemExit(main())
