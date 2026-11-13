"""Accel-limited motion planning for the toolpath -- the velocity profile v(t).

The g-code commands a feedrate per move, but a machine cannot change speed
instantly. The naive ``length / feed`` timing TELEPORTS between values at each
vertex; this module replaces it with the trajectory the machine actually runs, so
the print-time estimate, the animated head, the contact lag and the deposited
diameter all read the true speed. Pure module: stdlib only, no numpy, no VTK, no
I/O, no GUI.

THE TIMING AUTHORITY, AND OFF BY DEFAULT AT THE FUNCTION LEVEL. ``gcode_player``
re-times its playback clock and HUD total onto this trajectory and remaps the .depo
cache onto it; ``mew_deposition`` / ``mew_live`` size the deposited lag and diameter
at the head's REAL per-interval speed. ``max_accel`` None or <= 0 gives the naive
length/feed timeline, byte-identical. The player's clock/HUD/head re-time whenever
its Max-accel field is set -- pure visualisation, never touching the written STL.

IT CANNOT MOVE THE CALIBRATED STEADY STATE. On the constant-feed, stiff-gantry
process MEW runs, the ramp transients live on a ~1-10 ms / few-um scale while the
jet's flight lag is tens of ms, so the jet low-pass-filters them and the dominant
start/corner artefact is still the JET LAG. What re-timing visibly changes is the
print-TIME estimate, the animated head, and the deposited DIAMETER (more sensitive
to v than the centreline). A constant-feed cruise interior plans entry == exit, so
its duration is exactly length/feed, and ``max_accel -> infinity`` collapses the
trajectory to naive -- both pinned by the tests.

The model is the standard look-ahead trapezoidal planner 3D-printer firmware runs
(GRBL, Klipper): corner speed by JUNCTION DEVIATION (Sonny Jeon's GRBL cornering
algorithm, 2011), then a forward and a backward LOOK-AHEAD pass over the caps each
enforcing
v^2 <= v_prev^2 + 2 a L, then a PER-MOVE TRAPEZOID from the entry/cruise/exit
speeds. The user-facing knob is Klipper's SQUARE_CORNER_VELOCITY, what a
Voron/Klipper user reads off their ``printer.cfg``, converted by Klipper's own
junction_deviation = scv^2 / (max_accel * (sqrt(2) + 1)) so a 90 deg corner runs at
exactly ``scv``. ``plan_trajectory`` also applies a centripetal (curvature) cap,
so a turnaround ARC pre-expanded into small chords slows to sqrt(a*R): junction
deviation alone barely throttles a near-straight chord. ``MACHINE`` below is the
under-the-hood default (verified Voron 0.2 stock values); both limits are machine
properties, not jet knobs. Units: mm | mm/s | mm/s^2 | s, with feeds arriving in
mm/min and converted here, once.

A SEPARATE ROTARY LIMIT (4-axis mandrel). One ``max_accel`` caps the whole move
along the path. A rotary axis has its own limit, in deg/s^2, so ``plan_trajectory``
also takes ``seg_accel``: one along-path acceleration per segment, which
``axis_seg_accels`` / ``chart_seg_accels`` work out from how much of the move is
linear travel and how much is rotation. A move whose path length is ``L``, with
linear travel ``lin`` (mm) and rotation ``rot`` (deg), accelerating at ``a`` along
the path loads the linear axes with ``a * lin / L`` and the rotary with
``a * rot / L``, so the largest ``a`` both axes allow is
``min(max_accel * L / lin, rotary_accel * L / rot)``. With a rotary limit set, the
linear limit therefore caps the LINEAR axes only, not the whole surface path; with
none (0 or None, the default) ``seg_accel`` is None and the plan is the one-limit
plan, byte-identical. A corner takes the lower of its two moves' accelerations.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

MM_PER_MIN_TO_MM_PER_S = 1.0 / 60.0
_SQRT2_PLUS_1 = math.sqrt(2.0) + 1.0


# --------------------------------------------------------------------------- #
# Default machine config -- the fallback when a caller passes no limits. The
# PRIMARY source is now the GUI's Printhead-sim Max-accel / Corner-speed fields,
# threaded to the player + export; edit here only to change the default.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MachineConfig:
    """The machine's motion limits -- the only two numbers the planner needs.
    Both are properties of the printer (read them off your Klipper ``printer.cfg``),
    not of the jet or the material.

    max_accel               -- acceleration limit, mm/s^2.
    square_corner_velocity  -- speed held through a 90 deg corner, mm/s. Higher ->
                               faster corners / less slowdown; this is the Klipper
                               knob, converted internally to a junction deviation.

    Defaults are the VERIFIED Voron 0.2 stock values (LDO v02r1 printer.cfg:
    max_velocity 200, max_accel 2000, square_corner_velocity 6.0, Z 15/45). The
    planner uses only accel + SCV; max_velocity/Z are listed for reference and are
    already enforced upstream by the g-code feedrates."""
    max_accel: float = 2000.0
    square_corner_velocity: float = 6.0


# The active config. Swap the values above, or assign a new MachineConfig here,
# to retarget the planner to a different machine.
MACHINE = MachineConfig()


def junction_deviation_from_scv(square_corner_velocity: float,
                                max_accel: float) -> float:
    """Klipper's conversion: the junction deviation whose 90 deg corner speed is
    exactly ``square_corner_velocity``. Returns 0 for a non-positive accel (no
    cornering allowance), which the planner reads as "stop at every junction"."""
    if max_accel <= 0.0:
        return 0.0
    scv = max(square_corner_velocity, 0.0)
    return scv * scv / (max_accel * _SQRT2_PLUS_1)


def _centripetal_cap(p0, p1, p2, max_accel: float) -> float:
    """Speed cap (mm/s) from the centripetal limit ``v^2 / R <= max_accel`` at a
    polyline vertex, ``R`` the circumradius of the three consecutive points.

    Junction deviation throttles a corner by the angle between TWO moves, so on a
    smooth turnaround ARC pre-expanded into many ~2 deg chords each junction looks
    almost straight and is barely slowed -- yet the chords together trace a tight
    radius whose centripetal load (``v^2 / R``) is real and is what actually limits
    arc speed on the machine. This recovers that limit from the local curvature, so a
    tight arc slows to ``sqrt(a * R)`` while a sharp corner between long moves (large
    circumradius) is left to junction deviation. The two caps are taken together
    (min) in ``plan_trajectory``; both spend the same ``max_accel`` budget. Colinear
    / degenerate triples have ``R -> infinity`` -> returns +inf (no cap), so a
    straight run is untouched (and the planner still collapses to naive as a->inf)."""
    ax, ay, az = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
    bx, by, bz = p2[0] - p1[0], p2[1] - p1[1], p2[2] - p1[2]
    d01 = math.sqrt(ax * ax + ay * ay + az * az)
    d12 = math.sqrt(bx * bx + by * by + bz * bz)
    if d01 <= 1e-12 or d12 <= 1e-12:
        return math.inf                            # degenerate neighbour: no cap
    cx, cy, cz = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
    d02 = math.sqrt(cx * cx + cy * cy + cz * cz)
    # twice the triangle area = |AB x BC|
    crx, cry, crz = (ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx)
    area2 = math.sqrt(crx * crx + cry * cry + crz * crz)
    if area2 <= 1e-18:
        return math.inf                            # straight: no cap
    r_circ = (d01 * d12 * d02) / (2.0 * area2)     # circumradius = abc / (4*Area)
    return math.sqrt(max(max_accel, 0.0) * r_circ)


@dataclass(frozen=True)
class Move:
    """One planned segment points[i] -> points[i+1]. The trapezoid is stored as
    three phase durations plus the speeds, which is all `position`/`speed` need."""
    length: float           # segment length, mm
    cruise_v: float         # commanded feed cap, mm/s
    entry_v: float          # speed entering the move, mm/s
    exit_v: float           # speed leaving the move, mm/s
    peak_v: float           # speed reached on the plateau (<= cruise_v), mm/s
    t_acc: float            # accelerate-phase duration, s
    t_cruise: float         # cruise-phase duration, s
    t_dec: float            # decelerate-phase duration, s
    start: tuple            # (x, y, z) start position, mm
    unit: tuple             # (ux, uy, uz) unit direction (zero for a null move)
    accel: float = 0.0      # this move's ramp, mm/s^2; 0 -> the Trajectory's

    @property
    def duration(self) -> float:
        return self.t_acc + self.t_cruise + self.t_dec


@dataclass(frozen=True)
class Trajectory:
    """The planned motion. `times[k]` is the cumulative time the head reaches
    vertex k; `moves[i]` is the trapezoid for segment i. `speed_at`/`resample`
    read the velocity profile that the jet physics consumes."""
    times: list             # (n_pts,) cumulative arrival time per vertex, s
    moves: list             # (n_pts-1,) Move
    accel: float            # the max_accel used, mm/s^2
    total_time: float       # times[-1], s

    # -- velocity profile sampling ----------------------------------------- #
    def _accel_of(self, m: Move) -> float:
        """The ramp move `m` was planned at: its own under a rotary limit, else
        the one limit every move shares."""
        return m.accel if m.accel > 0.0 else self.accel

    def _dist_in_move(self, m: Move, lt: float) -> float:
        """Distance travelled into move `m` at local time `lt` (0..duration)."""
        a = self._accel_of(m)
        if lt <= m.t_acc:
            return m.entry_v * lt + 0.5 * a * lt * lt
        d = m.entry_v * m.t_acc + 0.5 * a * m.t_acc * m.t_acc      # end of accel
        lt -= m.t_acc
        if lt <= m.t_cruise:
            return d + m.peak_v * lt
        d += m.peak_v * m.t_cruise                                  # end of cruise
        lt -= m.t_cruise
        return d + m.peak_v * lt - 0.5 * a * lt * lt

    def _speed_in_move(self, m: Move, lt: float) -> float:
        """Instantaneous speed inside move `m` at local time `lt`."""
        a = self._accel_of(m)
        if lt <= m.t_acc:
            return m.entry_v + a * lt
        lt -= m.t_acc
        if lt <= m.t_cruise:
            return m.peak_v
        lt -= m.t_cruise
        return max(m.peak_v - a * lt, 0.0)

    def speed_at(self, t: float) -> float:
        """Instantaneous head speed (mm/s) at print-time `t` (clamped to the run).
        This is the hook the transient-diameter step (mass conservation responds
        to the real speed) and any later consumer reads."""
        if not self.moves:
            return 0.0
        if t <= self.times[0]:
            return self.moves[0].entry_v
        if t >= self.times[-1]:
            return self.moves[-1].exit_v
        i = bisect.bisect_right(self.times, t) - 1
        i = min(max(i, 0), len(self.moves) - 1)
        return self._speed_in_move(self.moves[i], t - self.times[i])

    def resample(self, ramp_steps: int = 4):
        """Sample the trajectory finely enough to resolve its ramps, adaptively:
        cruise phases stay one interval (position is exactly linear there, so the
        downstream linear-interpolation is exact), while each accel/decel phase is
        split into `ramp_steps` so its quadratic position is well approximated.

        Returns five parallel lists (one entry per sample):
            times   -- print-time of the sample, s (ascending)
            points  -- (x, y, z) head position, mm
            speeds  -- instantaneous speed, mm/s
            arcs    -- cumulative arc length along the path, mm
            seg     -- index of the move the sample belongs to

        A straight constant-feed run resamples to essentially its own vertices
        (each cruising move contributes a single end sample), so this is cheap;
        cost grows only where the path actually ramps (starts, stops, corners)."""
        if not self.moves:
            t0 = self.times[0] if self.times else 0.0
            return [t0], [(0.0, 0.0, 0.0)], [0.0], [0.0], [0]
        steps = max(int(ramp_steps), 1)
        m0 = self.moves[0]
        times = [self.times[0]]
        points = [m0.start]
        speeds = [m0.entry_v]
        arcs = [0.0]
        seg = [0]
        s_base = 0.0
        for i, m in enumerate(self.moves):
            t0 = self.times[i]
            if m.length <= 1e-12:                # null move: no motion, no time
                continue
            local = []                           # local sample times, ascending
            if m.t_acc > 1e-12:
                local += [m.t_acc * j / steps for j in range(1, steps + 1)]
            if m.t_cruise > 1e-12:
                local.append(m.t_acc + m.t_cruise)
            if m.t_dec > 1e-12:
                base = m.t_acc + m.t_cruise
                local += [base + m.t_dec * j / steps for j in range(1, steps + 1)]
            if not local:                        # degenerate trapezoid -> one step
                local = [m.duration]
            prev = 0.0
            for lt in local:
                if lt <= prev + 1e-15:
                    continue
                d = self._dist_in_move(m, lt)
                points.append((m.start[0] + m.unit[0] * d,
                               m.start[1] + m.unit[1] * d,
                               m.start[2] + m.unit[2] * d))
                times.append(t0 + lt)
                speeds.append(self._speed_in_move(m, lt))
                arcs.append(s_base + d)
                seg.append(i)
                prev = lt
            s_base += m.length
        return times, points, speeds, arcs, seg


def plan_trajectory(points, seg_feeds, max_accel: float = None,
                    square_corner_velocity: float = None,
                    curvature_limit: bool = True,
                    entry_v: float = 0.0, exit_v: float = 0.0,
                    seg_accel=None) -> Trajectory:
    """Plan the accel-limited trajectory for a polyline.

    Args:
        points:     sequence of N head positions, each indexable as (x, y, z).
        seg_feeds:  sequence of N-1 commanded feeds in mm/min (one per segment).
        max_accel:  machine acceleration limit, mm/s^2. None -> MACHINE.max_accel.
        square_corner_velocity: speed to hold through a 90 deg corner, mm/s
                    (Klipper's knob). None -> MACHINE.square_corner_velocity.
        curvature_limit: also cap each vertex by the centripetal limit of the LOCAL
                    curvature, so a turnaround ARC given as many small chords slows
                    to ``sqrt(a*R)``. On by default; a straight run or a sharp single
                    corner is unaffected (large circumradius -> no cap), so it can
                    only ADD a slowdown on genuinely curved stretches.
        entry_v:    speed (mm/s) the head ALREADY carries into the first vertex, and
        exit_v:     the speed it must still carry out of the last. Both default 0.0
                    -- the path starts and ends AT REST. A caller that knows the path
                    continues smoothly into a neighbour (a layer-change LINKING LOOP,
                    whose lead-out keeps the previous fibre's heading) passes the
                    cruise speed so the plan does not insert a spurious stop, and
                    downstream a v->0 diameter bead, at that boundary. Each is capped
                    to the adjoining segment's cruise feed.
        seg_accel:  None (default) -> every segment ramps at ``max_accel``. Else N-1
                    along-path accelerations, mm/s^2, one per segment -- what
                    ``axis_seg_accels`` returns under a separate rotary limit. The
                    junction-deviation radius stays the one ``max_accel`` and
                    ``square_corner_velocity`` set; each corner then spends the
                    lower of its two moves' accelerations. Ignored when
                    ``max_accel`` <= 0 (planning off).

    Returns a `Trajectory`. With N < 2 (nothing to move) it is empty-but-valid.
    """
    if max_accel is None:
        max_accel = MACHINE.max_accel
    if square_corner_velocity is None:
        square_corner_velocity = MACHINE.square_corner_velocity
    n = len(points)
    if n < 2:
        return Trajectory(times=[0.0] * n, moves=[], accel=max(float(max_accel),
                          1e-9), total_time=0.0)

    P = [(float(p[0]), float(p[1]), float(p[2])) for p in points]
    nseg = n - 1

    if max_accel <= 0.0:
        # Explicitly OFF: honour the documented contract (module docstring,
        # "max_accel None/<=0 -> the naive length/feed timeline") instead of
        # running the trapezoidal planner with a clamped-near-zero accel (which
        # would inflate the total time by orders of magnitude). Every move is
        # pure cruise at the commanded feed -- instantaneous accel, no ramps --
        # so duration == length/feed exactly, matching the pre-kinematics timing
        # every caller already assumes when it skips calling this function.
        length = [0.0] * nseg
        unit = [(0.0, 0.0, 0.0)] * nseg
        for i in range(nseg):
            dx = P[i + 1][0] - P[i][0]
            dy = P[i + 1][1] - P[i][1]
            dz = P[i + 1][2] - P[i][2]
            L = math.sqrt(dx * dx + dy * dy + dz * dz)
            length[i] = L
            if L > 1e-12:
                unit[i] = (dx / L, dy / L, dz / L)
        cruise = [max(float(seg_feeds[i]) * MM_PER_MIN_TO_MM_PER_S, 1e-9)
                  for i in range(nseg)]
        moves = []
        times = [0.0] * n
        t_cum = 0.0
        for i in range(nseg):
            vc, L = cruise[i], length[i]
            if L <= 1e-12:                          # null move: passes no time
                moves.append(Move(length=0.0, cruise_v=vc, entry_v=vc, exit_v=vc,
                                  peak_v=vc, t_acc=0.0, t_cruise=0.0, t_dec=0.0,
                                  start=P[i], unit=unit[i]))
                times[i + 1] = t_cum
                continue
            t_cru = L / vc
            t_cum += t_cru
            moves.append(Move(length=L, cruise_v=vc, entry_v=vc, exit_v=vc,
                              peak_v=vc, t_acc=0.0, t_cruise=t_cru, t_dec=0.0,
                              start=P[i], unit=unit[i]))
            times[i + 1] = t_cum
        return Trajectory(times=times, moves=moves, accel=float(max_accel),
                          total_time=t_cum)

    a = max(float(max_accel), 1e-9)
    jd = junction_deviation_from_scv(float(square_corner_velocity), a)

    # geometry: length and unit direction per segment
    length = [0.0] * nseg
    unit = [(0.0, 0.0, 0.0)] * nseg
    for i in range(nseg):
        dx = P[i + 1][0] - P[i][0]
        dy = P[i + 1][1] - P[i][1]
        dz = P[i + 1][2] - P[i][2]
        L = math.sqrt(dx * dx + dy * dy + dz * dz)
        length[i] = L
        if L > 1e-12:
            unit[i] = (dx / L, dy / L, dz / L)

    cruise = [max(float(seg_feeds[i]) * MM_PER_MIN_TO_MM_PER_S, 1e-9)
              for i in range(nseg)]
    # the ramp per segment: `a` everywhere unless a rotary limit made it vary
    if seg_accel is None:
        acc = [a] * nseg
    else:
        if len(seg_accel) != nseg:
            raise ValueError("seg_accel needs one value per segment: %d vs %d"
                             % (len(seg_accel), nseg))
        acc = [max(float(x), 1e-9) for x in seg_accel]

    # vertex speed caps c[0..nseg]. The path starts/ends at rest by default, but a
    # caller continuing smoothly into a neighbour (a linking loop's straight junction)
    # seeds the boundary with the carried speed, capped to the adjoining cruise -- so
    # the forward/backward passes never force a stop (or its v->0 diameter bead) there.
    c = [0.0] * (nseg + 1)
    c[0] = min(max(float(entry_v), 0.0), cruise[0])
    c[nseg] = min(max(float(exit_v), 0.0), cruise[nseg - 1])
    for k in range(1, nseg):
        u0, u1 = unit[k - 1], unit[k]
        aj = min(acc[k - 1], acc[k])               # the corner's ramp: the lower
        if length[k - 1] <= 1e-12 or length[k] <= 1e-12:
            vj = min(cruise[k - 1], cruise[k])     # null neighbour: no real corner
        else:
            cos_t = -(u0[0] * u1[0] + u0[1] * u1[1] + u0[2] * u1[2])
            cos_t = max(-1.0, min(1.0, cos_t))
            # sin_h = cos(phi/2): -> 1 as the junction STRAIGHTENS (and the point the
            # 1/(1-sin_h) cornering radius blows up -> no limit), -> 0 at a full
            # REVERSAL (where the formula gives vj -> 0 on its own, a momentary stop).
            # So the only special case is the near-straight singularity; everything
            # between runs the junction-deviation formula.
            sin_h = math.sqrt(0.5 * (1.0 - cos_t))
            if sin_h >= 0.999999:                  # ~straight: junction imposes no limit
                vj = min(cruise[k - 1], cruise[k])
            else:
                vj = math.sqrt(aj * jd * (sin_h / (1.0 - sin_h)))
        c[k] = min(vj, cruise[k - 1], cruise[k])
        # also cap by the LOCAL curvature's centripetal limit, so a turnaround arc
        # (many small chords junction deviation barely throttles) slows like the real
        # machine; a straight run / sharp corner has a huge circumradius -> no effect
        if curvature_limit:
            vcp = _centripetal_cap(P[k - 1], P[k], P[k + 1], aj)
            if vcp < c[k]:
                c[k] = vcp

    # forward pass (acceleration limit) then backward pass (deceleration limit)
    for k in range(1, nseg + 1):
        reach = math.sqrt(c[k - 1] * c[k - 1] + 2.0 * acc[k - 1] * length[k - 1])
        if c[k] > reach:
            c[k] = reach
    for k in range(nseg - 1, -1, -1):
        reach = math.sqrt(c[k + 1] * c[k + 1] + 2.0 * acc[k] * length[k])
        if c[k] > reach:
            c[k] = reach

    # build the per-move trapezoids and cumulative arrival times
    moves = []
    times = [0.0] * n
    t_cum = 0.0
    for i in range(nseg):
        ve, vx, vc, L, ai = c[i], c[i + 1], cruise[i], length[i], acc[i]
        if L <= 1e-12:                             # null move: passes no time
            moves.append(Move(length=0.0, cruise_v=vc, entry_v=ve, exit_v=vx,
                              peak_v=max(ve, vx), t_acc=0.0, t_cruise=0.0,
                              t_dec=0.0, start=P[i], unit=unit[i], accel=ai))
            times[i + 1] = t_cum
            continue
        # peak the move could reach accelerating in then decelerating out, capped
        vp = min(math.sqrt(max((2.0 * ai * L + ve * ve + vx * vx) * 0.5, 0.0)), vc)
        vp = max(vp, ve, vx)                        # guard tiny numerical dips
        d_acc = max((vp * vp - ve * ve) / (2.0 * ai), 0.0)
        d_dec = max((vp * vp - vx * vx) / (2.0 * ai), 0.0)
        d_cru = max(L - d_acc - d_dec, 0.0)
        t_acc = (vp - ve) / ai
        t_dec = (vp - vx) / ai
        t_cru = d_cru / vp if vp > 1e-12 else 0.0
        t_cum += t_acc + t_cru + t_dec
        moves.append(Move(length=L, cruise_v=vc, entry_v=ve, exit_v=vx, peak_v=vp,
                          t_acc=t_acc, t_cruise=t_cru, t_dec=t_dec,
                          start=P[i], unit=unit[i], accel=ai))
        times[i + 1] = t_cum

    return Trajectory(times=times, moves=moves, accel=a, total_time=t_cum)


# --------------------------------------------------------------------------- #
# Separate linear and rotary acceleration limits -- the 4-axis mandrel
# --------------------------------------------------------------------------- #
def rotary_limit_on(max_accel, rotary_accel) -> bool:
    """Whether a separate rotary limit applies: planning on AND a positive
    rotary limit. Off, every caller plans the one-limit path, byte-identical."""
    return (max_accel is not None and max_accel > 0.0
            and rotary_accel is not None and rotary_accel > 0.0)


def axis_seg_accels(lengths, linear_mm, rotary_deg, max_accel, rotary_accel):
    """Per-segment along-path acceleration (mm/s^2) under separate limits, or
    None when there is no rotary limit (`rotary_limit_on`).

    `lengths` is each segment's path length as the planner measures it (mm),
    `linear_mm` its linear-axis travel (mm, the X/Y/Z vector), `rotary_deg` its
    rotation (degrees, the largest joint). Accelerating at `a` along the path
    loads the linear axes with `a * lin / L` and the rotary with
    `a * rot / L`, so the segment gets `min(max_accel * L / lin,
    rotary_accel * L / rot)`. Both ratios are the same for every piece of a
    move cut into equal steps, so a helix drawn as many chords gets one value.
    A segment with neither (a null move) keeps `max_accel`."""
    if not rotary_limit_on(max_accel, rotary_accel):
        return None
    lin_a, rot_a = float(max_accel), float(rotary_accel)
    out = []
    for L, lin, rot in zip(lengths, linear_mm, rotary_deg):
        L, lin, rot = float(L), abs(float(lin)), abs(float(rot))
        a = math.inf
        if L > 1e-12:
            if lin > 1e-12:
                a = lin_a * L / lin
            if rot > 1e-12:
                a = min(a, rot_a * L / rot)
        out.append(a if math.isfinite(a) else lin_a)
    return out


def chart_seg_accels(chart_pts, metric_pts, frame, max_accel, rotary_accel):
    """`axis_seg_accels` for a fibre given in a mandrel's (u, v, h) chart, or
    None when there is no rotary limit or the frame is not a mandrel's.

    Only a `CylinderFrame` (the 4-axis mandrel, `chart_currency`) has the chart
    this reads: `u` is machine X, `h` is Z above the surface, and
    `v = R0 * radians(A)` (`CylinderFrame.unroll`), so the linear travel is
    `hypot(du, dh)` and the rotation `degrees(|dv| / R0)` -- the same split
    `gcode_player` reads off the file's X/Z and A words. A five-axis collector
    has no such chart here and plans on the one limit, in the player too.
    `metric_pts` are the planner's own points (`_metric_path`), so the lengths
    match the path it plans."""
    if not rotary_limit_on(max_accel, rotary_accel):
        return None
    if not (getattr(frame, "rotary", False)
            and getattr(frame, "chart_currency", False)):
        return None
    r0 = float(frame.radius)
    lengths, lin, rot = [], [], []
    for k in range(len(chart_pts) - 1):
        p0, p1 = chart_pts[k], chart_pts[k + 1]
        q0, q1 = metric_pts[k], metric_pts[k + 1]
        lengths.append(math.sqrt(sum((float(q1[i]) - float(q0[i])) ** 2
                                     for i in range(3))))
        lin.append(math.hypot(float(p1[0]) - float(p0[0]),
                              float(p1[2]) - float(p0[2])))
        rot.append(math.degrees(abs(float(p1[1]) - float(p0[1])) / r0))
    return axis_seg_accels(lengths, lin, rot, max_accel, rotary_accel)


# --------------------------------------------------------------------------- #
# Five-axis feed compensation -- the F word on a machine whose collector moves
# --------------------------------------------------------------------------- #
# WHAT THE F WORD DIVIDES, AND WHY IT IS NOT A PHYSICAL LAW. On a three-axis
# machine `F` is the tool speed, because the only axes are millimetres and the
# move vector's norm IS the distance the nozzle travels. Add a rotary axis and
# firmware keeps the same arithmetic: it divides the norm of the FULL axis
# vector by F, with the rotary components counted in DEGREES, added in
# quadrature with millimetres. That norm is not a length and the quotient is not
# a speed; it is a machine convention, and it is the one Terranova 2024's own
# files use. Measured on all six of their published
# files: `|(dX,dY,dZ,dU,dV)| / F` is constant per file to within 1% on 83-99.6%
# of moves (99.999 ms on Fig3D_Wavy.g, 60.000 ms on Fig3B, 499 ms on Fig4H),
# where the same quotient taken over dXYZ alone scatters with cv 0.78.
#
# The consequence is the reason this function exists. Their toolpath is sampled
# uniformly in TIME, so their F is the arithmetic shadow of that sampling rather
# than a speed anyone chose; the angular step is a fixed increment giving a
# 0.1 mm arc at ONE nominal radius per collector, not at the varying local
# radius. So their surface speed is NOT held constant, and their file is a
# comparator for the CONVENTION and not for the physics.
#
# Ours holds the surface speed, because in our own model CTS is what sets fibre
# diameter and jet lag, and neither cares what a controller divided by. The
# compensation is therefore: pick the segment time from the SURFACE arc length,
# then emit whatever F word makes the firmware take that long.


def machine_norm(delta_mm, delta_deg=()):
    """The vector norm firmware divides by F: mm and DEGREES in quadrature.

    Dimensionally incoherent on purpose -- see the note above. `delta_mm` is the
    linear axis deltas, `delta_deg` the rotary ones in degrees.
    """
    total = 0.0
    for d in delta_mm:
        total += float(d) * float(d)
    for d in delta_deg:
        total += float(d) * float(d)
    return math.sqrt(total)


def surface_feeds(norms, surface_lengths, speed):
    """Per-segment F words holding SURFACE speed at `speed` (mm/min).

    `norms` are `machine_norm` per segment, `surface_lengths` the true arc the
    deposition point walks ON THE COLLECTOR over the same segment. The segment
    must take `arc / speed`, so the word to emit is `speed * norm / arc`.

    A segment with no surface motion has no surface speed to hold and keeps
    `speed`; that is the pure-reorientation move, where the collector turns
    under a stationary contact point. A flat three-axis path has `norm == arc`
    on every segment and every word comes back exactly `speed`, which is what
    makes switching this on over flat output a no-op rather than a rounding
    difference.
    """
    if not speed > 0:
        raise ValueError("speed must be positive, got %r" % (speed,))
    if len(norms) != len(surface_lengths):
        raise ValueError("norms and surface_lengths must agree: %d vs %d"
                         % (len(norms), len(surface_lengths)))
    out = []
    for norm, arc in zip(norms, surface_lengths):
        if arc <= 1e-12 or norm <= 1e-12:
            out.append(float(speed))
        else:
            out.append(float(speed) * float(norm) / float(arc))
    return out


__all__ = [
    "MachineConfig", "MACHINE", "Move", "Trajectory", "plan_trajectory",
    "junction_deviation_from_scv", "MM_PER_MIN_TO_MM_PER_S",
    "machine_norm", "surface_feeds",
    "rotary_limit_on", "axis_seg_accels", "chart_seg_accels",
]
