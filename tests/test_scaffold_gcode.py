#!/usr/bin/env python3
"""Self-running tests for scaffold_gcode.py -- the linking-loop feature, plus the
guard that it is inert when off. No pytest: prints PASS/FAIL per test, exits
non-zero on any failure.

What a linking loop must do: at a near-coincident sharp layer change (one layer
exits and the next enters at the same point) insert a gentle OUTBOARD loop so
the head is already travelling along the next layer's first-fibre direction when
deposition resumes. These tests check it appears only where it should, re-enters
ALIGNED with that first fibre, bulges outboard, and that 'off' leaves the g-code
untouched."""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import math
import sys

import scaffold_gcode as sg


# --------------------------------------------------------------------------- #
# tiny g-code reader: absolute-XY move list (arcs kept by endpoint -- enough for
# the structural checks here)
# --------------------------------------------------------------------------- #
def parse_moves(gcode):
    x = y = z = 0.0
    moves = []
    for raw in gcode.splitlines():
        toks = raw.split()
        if not toks or toks[0] not in ("G0", "G1", "G2", "G3"):
            continue
        w = {}
        for t in toks[1:]:
            try:
                w[t[0]] = float(t[1:])
            except (ValueError, IndexError):
                pass
        x = w.get("X", x)
        y = w.get("Y", y)
        has_z = "Z" in w
        if has_z:
            z = w["Z"]
        moves.append({"cmd": toks[0], "x": x, "y": y, "z": z, "has_z": has_z})
    return moves


def _unit(ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-12 else (0.0, 0.0)


def find_loops(moves, tol=1e-3):
    """For every layer entry (a G0 carrying Z), detect a linking loop: the path
    leaves the entry point E and comes back to E within a few moves before the
    long deposition run. Returns one dict per detected loop."""
    loops = []
    n = len(moves)
    for i, m in enumerate(moves):
        if m["cmd"] != "G0" or not m["has_z"]:
            continue
        ex, ey = m["x"], m["y"]
        for j in range(i + 2, min(i + 80, n)):
            if moves[j]["cmd"] == "G0":          # next entry -> no loop here
                break
            if abs(moves[j]["x"] - ex) < tol and abs(moves[j]["y"] - ey) < tol:
                if j + 1 < n and moves[j + 1]["cmd"] != "G0":
                    loops.append({
                        "entry": (ex, ey),
                        "in_dir": _unit(moves[j - 1]["x"], moves[j - 1]["y"],
                                        ex, ey),                  # t2 -> E
                        "out_dir": _unit(ex, ey, moves[j + 1]["x"],
                                         moves[j + 1]["y"]),       # E -> dep1
                        "pts": [(moves[k]["x"], moves[k]["y"])
                                for k in range(i + 1, j)],         # t1 .. t2
                    })
                break
    return loops


# --------------------------------------------------------------------------- #
# fixtures: a 10x10 square, 4 layers, 90 deg basketweave. bbox = [5,15] x [5,15].
# --------------------------------------------------------------------------- #
BBOX = (5.0, 15.0, 5.0, 15.0)      # xmin, xmax, ymin, ymax of the footprint


def gen(link_loops="off", shape="rectangle", layers=4, g2g3=False,
        link_loop_angle=25.0, link_loop_dist=0.5, link_loop_travel=True):
    return sg.generate_scaffold_gcode(
        10.0, 10.0, 1.0, 90.0, 0.2, layers, 600.0,
        start_x=5.0, start_y=5.0, arc_radius=1.5, shape=shape, g2g3=g2g3,
        link_loops=link_loops, link_loop_angle=link_loop_angle,
        link_loop_dist=link_loop_dist, link_loop_travel=link_loop_travel)


def gen_wave(link_loops="manual", layers=4, link_loop_dist=0.5,
             pattern="-0.3,0.2; -0.1,-0.2; 0.1,0.2; 0.3,-0.2", wavelength=0.8):
    """Same square as gen(), but every fibre is waved by a linear pattern -- the
    case where the interlayer linking loop used to misfire."""
    return sg.generate_scaffold_gcode(
        10.0, 10.0, 1.0, 90.0, 0.2, layers, 600.0,
        start_x=5.0, start_y=5.0, arc_radius=1.5, link_loops=link_loops,
        link_loop_dist=link_loop_dist,
        linear_pattern=sg.parse_linear_pattern(pattern),
        linear_wavelength=wavelength)


def _outside_bbox(px, py, eps=1e-6):
    xmin, xmax, ymin, ymax = BBOX
    return (px < xmin - eps or px > xmax + eps
            or py < ymin - eps or py > ymax + eps)


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_off_is_inert():
    """'off' adds no loops and is byte-identical to the default."""
    g_off = gen("off")
    assert g_off == gen(), "default must equal link_loops='off'"
    assert find_loops(parse_moves(g_off)) == [], "off must not insert any loop"


def test_manual_inserts_one_loop_per_transition():
    loops = find_loops(parse_moves(gen("manual")))
    assert len(loops) == 3, f"expected 3 loops (layers-1), got {len(loops)}"


def test_loops_realign_with_next_fibre():
    """The loop must re-enter the corner travelling along the next layer's first
    fibre -- the whole point. in_dir (t2->E) and out_dir (E->dep1) are parallel."""
    for lp in find_loops(parse_moves(gen("manual"))):
        d = lp["in_dir"][0] * lp["out_dir"][0] + lp["in_dir"][1] * lp["out_dir"][1]
        assert d > 0.999, f"loop re-entry not aligned with next fibre (dot={d:.4f})"


def test_loops_bulge_outboard():
    """Every loop must reach outside the footprint bbox (never cut across it)."""
    for lp in find_loops(parse_moves(gen("manual"))):
        assert any(_outside_bbox(px, py) for px, py in lp["pts"]), \
            "loop does not bulge outboard of the footprint"


def test_auto_angle_gate():
    """auto loops a 90 deg square corner at the default angle, but not when the
    threshold is raised above 90."""
    assert len(find_loops(parse_moves(gen("auto")))) == 3, \
        "auto should loop 90 deg corners at the default 25 deg threshold"
    assert find_loops(parse_moves(gen("auto", link_loop_angle=120.0))) == [], \
        "auto must skip corners below the angle threshold"


def test_auto_distance_gate():
    """The coincidence gate, which `link_loop_travel=False` restores. A circle's
    layer changes are far apart (not coincident corners), so with the opt-out set
    even 'manual' must not loop them -- they route around / fail the gate.

    With the opt-out UNSET (the default) those same changes DO loop, off the
    arriving travel fibre -- `test_travel_arrival_loops_a_circle` below."""
    assert find_loops(parse_moves(gen("manual", shape="circle",
                                      link_loop_travel=False))) == [], \
        "circle footprint should get no linking loops with travel loops off"


def test_travel_arrival_loops_a_circle():
    """The feature: a layer change whose entry is reached along a TRAVEL fibre is
    a real corner (in continuous-flow MEW the hop is deposited), so it loops --
    and the loop still obeys both structural contracts, re-entering aligned with
    the next layer's first fibre and bulging outboard of the footprint."""
    loops = find_loops(parse_moves(gen("manual", shape="circle")))
    assert loops, "a travel arrival should now get a linking loop"
    for lp in loops:
        d = lp["in_dir"][0] * lp["out_dir"][0] + lp["in_dir"][1] * lp["out_dir"][1]
        assert d > 0.999, f"travel loop re-entry not aligned (dot={d:.4f})"
        assert any(_outside_bbox(px, py) for px, py in lp["pts"]), \
            "travel loop does not bulge outboard of the footprint"


def test_g2g3_loops_present_and_aligned():
    """g2g3 mode emits the loop arc as one G2/G3; closure + alignment still hold."""
    loops = find_loops(parse_moves(gen("manual", g2g3=True)))
    assert len(loops) == 3, f"expected 3 g2g3 loops, got {len(loops)}"
    for lp in loops:
        d = lp["in_dir"][0] * lp["out_dir"][0] + lp["in_dir"][1] * lp["out_dir"][1]
        assert d > 0.999, f"g2g3 loop re-entry not aligned (dot={d:.4f})"


def test_link_loop_circle_geometry():
    """Direct check of the solver on the canonical square corner: both feet sit on
    the circle, the centre is outboard, and the head turns the long way (270 deg)."""
    p = (0.0, 0.0)
    sol = sg._link_loop_circle(p, (1.0, 0.0), (0.0, 1.0), 1.5, (-5.0, 5.0))
    assert sol is not None, "expected a loop for a 90 deg outboard corner"
    cx, cy, R, a1, sweep, t1, t2, ccw = sol
    assert abs(math.hypot(t1[0] - cx, t1[1] - cy) - R) < 1e-9, "t1 not on circle"
    assert abs(math.hypot(t2[0] - cx, t2[1] - cy) - R) < 1e-9, "t2 not on circle"
    # centre outboard: away from the part centre (which is up-left of the corner)
    assert cx > 0 and cy < 0, "loop centre should sit outboard of the corner"
    assert abs(abs(sweep) - 1.5 * math.pi) < 1e-6, "square corner should sweep 270 deg"


def test_link_loop_circle_rejects_degenerate():
    """No loop for a straight-through 'turn' or one that would bulge into the part."""
    assert sg._link_loop_circle((0.0, 0.0), (1.0, 0.0), (1.0, 0.0),
                                1.5, (-5.0, 5.0)) is None, "straight -> no loop"
    # same headings as the geometry test, but the part now sits on the loop's
    # outboard side, so the only loop would cross it -> rejected
    assert sg._link_loop_circle((0.0, 0.0), (1.0, 0.0), (0.0, 1.0),
                                1.5, (5.0, -5.0)) is None, "inboard -> no loop"
    assert sg._link_loop_circle((0.0, 0.0), (1.0, 0.0), (0.0, 1.0),
                                0.0, (-5.0, 5.0)) is None, "zero radius -> no loop"


def test_validation_rejects_bad_mode():
    try:
        sg.generate_scaffold_gcode(10.0, 10.0, 1.0, 90.0, 0.2, 2, 600.0,
                                   link_loops="sometimes")
    except ValueError:
        return
    raise AssertionError("bad link_loops value should raise ValueError")


# --------------------------------------------------------------------------- #
# Linear-pattern (wave) turnaround placement
# --------------------------------------------------------------------------- #
# A waved fiber must BEGIN and END exactly on the bare pass end (the footprint
# grid corner): NEITHER wave offset may survive at the tips. `along` would slide
# the tip off the footprint edge; `perp` would slide it ALONG the edge, and since
# the wave rotates with the layer that slide flips between a layer and its
# 180-degree partner, staggering the turnaround loops on alternating layers.
_WAVE_TABLE = sg.parse_linear_pattern("-0.3,0.2; -0.1,-0.2; 0.1,0.2; 0.3,-0.2")
_WAVE_PATTERN = (_WAVE_TABLE, 0.8, 0.0)


def test_wave_endpoints_pinned_to_pass_ends():
    """`_wave_pass` must return the EXACT straight pass endpoints as its first and
    last points -- both the along AND the perp wave offset zeroed there. Checked
    across several layer angles and a non-origin pass, since both offsets are phase
    (position) dependent."""
    table, wl, sp = _WAVE_PATTERN
    period = wl + sp
    for angle in (0.0, 37.0, 90.0, 180.0, 270.0):
        th = math.radians(angle)
        tangent = (math.cos(th), math.sin(th))
        # a pass of length 10 running along the layer direction from an arbitrary a
        ax, ay = -5.0 + 0.7, 2.3
        bx = ax + 10.0 * tangent[0]
        by = ay + 10.0 * tangent[1]
        pts = sg._wave_pass((ax, ay), (bx, by), table, period, tangent)
        for (px, py), (ex, ey), name in (
                (pts[0], (ax, ay), "start"), (pts[-1], (bx, by), "end")):
            assert math.hypot(px - ex, py - ey) < 1e-9, (
                f"angle {angle} {name} tip is {math.hypot(px-ex, py-ey):.4f}mm "
                f"off the bare pass end (wave offset leaked to the tip)")


def test_wave_pin_tips_false_keeps_first_vertex_on_the_line():
    """With `pin_tips=False` (the authoring-preview path), `_wave_pass` must NOT
    flatten the tips: its first point must land on the authored vertex 0 so the
    live graph's line connects to the (green) anchor marker. Regression for the
    preset-linear-pattern bug where the tip-pinning -- correct for a real footprint
    edge -- disconnected the first segment in the abstract preview."""
    table = [(-0.3, 0.2), (-0.1, -0.2), (0.1, 0.2), (0.3, -0.2)]
    period = 0.8
    pts = sg._wave_pass((0.0, 0.0), (period, 0.0), table, period, (1.0, 0.0),
                        pin_tips=False)
    x0, y0 = pts[0]
    assert math.hypot(x0 - table[0][0], y0 - table[0][1]) < 1e-9, (
        f"first preview point {pts[0]} is not on vertex 0 {table[0]} -- the "
        f"green anchor would float off the line")
    # ...and pinning ON (the default, real-scaffold path) still flattens it.
    pinned = sg._wave_pass((0.0, 0.0), (period, 0.0), table, period, (1.0, 0.0))
    assert math.hypot(pinned[0][0], pinned[0][1]) < 1e-9, (
        "default pin_tips=True must still pin the start tip to the pass end")


def test_wave_loops_uniform_across_rotated_layers():
    """The turnaround loops of a layer and its 180-degree partner must sit at the
    SAME points along the perimeter -- the wave (which rotates with the layer) must
    not stagger them. Compares the right-edge (x=+5) loop centres of the angle-0
    and angle-180 waved layers against the no-pattern placement."""
    def right_centres(loops):
        out = []
        for lp in loops:
            xs = [p[0] for p in lp]
            ys = [p[1] for p in lp]
            if max(xs) > 4.0:                       # a right-edge (x=+5) loop
                out.append(round((min(ys) + max(ys)) / 2.0, 6))
        return sorted(out)
    c0 = right_centres(_flatten_loops(0.0))
    c180 = right_centres(_flatten_loops(180.0))
    assert c0 == c180, f"angle 0 vs 180 loops staggered: {c0} != {c180}"


def _flatten_loops(angle):
    """Turnaround loops of one waved angle-0-family layer as point runs, using
    the same link-tagging `_emit_one_scaffold` relies on."""
    th = math.radians(angle)
    layer_dir = (math.cos(th), math.sin(th))
    passes = sg._layer_passes(angle, 10.0, 10.0, 0.5, flip=False, polygon=None)
    pts = sg._flatten_passes(passes, 1.5 * 0.5, 8, 0.5, g2g3=False,
                             pattern=_WAVE_PATTERN, vflip=False,
                             layer_dir=layer_dir)
    loops, cur = [], []
    for i, (x, y, link, _arc) in enumerate(pts):
        if link:
            if not cur:
                cur = [(pts[i - 1][0], pts[i - 1][1])]
            cur.append((x, y))
        elif cur:
            loops.append(cur)
            cur = []
    if cur:
        loops.append(cur)
    return loops


def test_linear_pattern_loops_symmetric():
    """Opposite-edge turnaround loops of a waved layer must reach EQUALLY far past
    the footprint edge. angle 0: fibers span x in [-5, 5]; loops alternate the
    right (x=+5) and left (x=-5) edges. The wave must not push one edge's loops
    inboard while the other's go outboard."""
    loops = _flatten_loops(0.0)
    right = [max(x for x, _ in lp) - 5.0 for lp in loops if lp[0][0] > 0]
    left = [(-5.0) - min(x for x, _ in lp) for lp in loops if lp[0][0] < 0]
    assert right and left, "expected loops on both edges"
    assert abs(max(right) - min(right)) < 1e-6, "right-edge loops not uniform"
    assert abs(max(left) - min(left)) < 1e-6, "left-edge loops not uniform"
    assert abs(right[0] - left[0]) < 1e-6, (
        f"opposite edges asymmetric: right clears {right[0]:.3f}mm, "
        f"left clears {left[0]:.3f}mm")


# --------------------------------------------------------------------------- #
# Interlayer linking loops UNDER a linear pattern
# --------------------------------------------------------------------------- #
# The wave rides to the pass tips, so consecutive layers' exit/entry tips no
# longer coincide and their end LEGS wobble off the fibre axis. The loop headings
# must come from the fibre axes + part centre, and the coincidence gate must
# tolerate the wave offset -- else loops misfire (some corners get none, others
# bulge wrong) exactly as reported.
def test_pattern_interlayer_loops_fire_every_transition():
    """manual mode must place a loop at EVERY layer change of a waved square --
    not the intermittent, sometimes-inboard mess the wavy end-legs produced."""
    loops = find_loops(parse_moves(gen_wave("manual", layers=6)))
    assert len(loops) == 5, f"expected 5 loops (layers-1), got {len(loops)}"


def test_pattern_interlayer_loops_bulge_outboard():
    """Every waved-square linking loop must reach outside the footprint bbox."""
    for lp in find_loops(parse_moves(gen_wave("manual", layers=6))):
        assert any(_outside_bbox(px, py) for px, py in lp["pts"]), \
            "waved-pattern linking loop does not bulge outboard"


def test_pattern_interlayer_auto_fires_at_default_dist():
    """auto mode at the DEFAULT coincidence distance must still loop a waved
    square: the gate is widened by the wave's perpendicular reach, so the tips'
    wave gap no longer suppresses the loop ('raise the distance' was the old
    workaround)."""
    loops = find_loops(parse_moves(gen_wave("auto", layers=6, link_loop_dist=0.5)))
    assert len(loops) == 5, \
        f"auto+default dist should loop every waved corner, got {len(loops)}"


def test_pattern_interlayer_off_is_inert():
    """A waved scaffold with link_loops off gets NO linking loops."""
    assert find_loops(parse_moves(gen_wave("off", layers=6))) == [], \
        "off must insert no loop even with a pattern"


def test_pattern_does_not_disturb_plain_link_loops():
    """Guard: the pattern branch is dead code when no pattern is set -- a plain
    square's linking-loop g-code is unchanged by the new code path."""
    assert gen("manual", layers=6) == sg.generate_scaffold_gcode(
        10.0, 10.0, 1.0, 90.0, 0.2, 6, 600.0, start_x=5.0, start_y=5.0,
        arc_radius=1.5, link_loops="manual"), \
        "non-pattern linking-loop output must be byte-identical"


# --------------------------------------------------------------------------- #
def test_footprint_angle_flat_path_rotates():
    """generate_scaffold_gcode (the SINGLE-scaffold flat wrapper -- the GUI's n==1
    path and the --footprint-angle CLI) must honour footprint_angle. Regression: a
    lone scaffold's Positioning-page rotation was silently dropped because only the
    JSON multi-scaffold path forwarded footprint_angle, so the flat wrapper (and the
    CLI) had no way to rotate a single scaffold -- the g-code / STL never changed."""
    kw = dict(width=40, height=10, stepover=0.5, rotation=90,
              fiber_diameter=0.02, layers=3, speed=600, start_x=0, start_y=0)
    g0 = sg.generate_scaffold_gcode(**kw, footprint_angle=0.0)
    assert g0 == sg.generate_scaffold_gcode(**kw), \
        "footprint_angle=0 must be byte-identical to omitting it"
    g55 = sg.generate_scaffold_gcode(**kw, footprint_angle=55.0)
    assert g55 != g0, "footprint_angle=55 did not change the g-code"

    def yext(g):
        ys = [m["y"] for m in parse_moves(g)]
        return max(ys) - min(ys)

    # a 40x10 footprint is wide + short at 0 deg; rotating 55 deg swings the long
    # axis up, so the Y-extent must grow well past the bare 10 mm height
    assert yext(g55) > yext(g0) + 5.0, \
        f"rotation didn't change the footprint ({yext(g0):.1f} -> {yext(g55):.1f} mm Y)"


# --------------------------------------------------------------------------- #
# footprint_angle must be a RIGID rotation: rotating the whole scaffold may not
# desync consecutive layers' shared corners, or the linking loops silently drop
# (the old path re-clipped the fibres against a ROTATED polygon and lost the
# edge-parallel extreme pass, so the coincidence gate failed and a bare travel
# fibre was dragged where a loop belongs). Regression for the Positioning-tab
# rotate handle.
# --------------------------------------------------------------------------- #
def _gen_fa(footprint_angle, layers=6, link_loops="auto"):
    """The gen() square (10x10, stepover 1, 90 deg basketweave) turned on the
    plate by `footprint_angle` -- exactly what the Positioning rotate handle writes."""
    return sg.generate_scaffold_gcode(
        10.0, 10.0, 1.0, 90.0, 0.2, layers, 600.0,
        start_x=5.0, start_y=5.0, arc_radius=1.5,
        link_loops=link_loops, footprint_angle=footprint_angle)


def _max_interlayer_travel(moves):
    """Longest G0 hop after the first layer entry -- a coincident-corner square
    should hop 0 mm between layers (the next layer starts where the last ended)."""
    worst = 0.0
    px = py = None
    seen_first = False
    for m in moves:
        if m["cmd"] == "G0" and px is not None and seen_first:
            worst = max(worst, math.hypot(m["x"] - px, m["y"] - py))
        if m["cmd"] == "G0" and m["has_z"]:
            seen_first = True
        px, py = m["x"], m["y"]
    return worst


def test_footprint_angle_keeps_every_linking_loop():
    """A rotated square must fire the SAME linking loops as the un-rotated one and
    drag NO stray inter-layer travel. On the old (re-clip) path a 40 deg rotation
    dropped 4 of the 5 loops and left ~1.4 mm travel fibres in their place."""
    layers = 6
    base = len(find_loops(parse_moves(_gen_fa(0.0, layers))))
    assert base == layers - 1, \
        f"fixture sanity: expected {layers - 1} loops at 0 deg, got {base}"
    for fa in (40.0, 12.3456, 25.0, 55.0, 130.0):
        moves = parse_moves(_gen_fa(fa, layers))
        n = len(find_loops(moves))
        assert n == base, \
            f"footprint_angle={fa}: {n} linking loops, expected {base} " \
            f"(rotation desynced the corners and dropped a loop)"
        travel = _max_interlayer_travel(moves)
        assert travel < 1e-3, \
            f"footprint_angle={fa}: {travel:.3f} mm stray inter-layer travel " \
            f"fibre where a linking loop belongs"


def test_footprint_angle_deposits_rigidly_rotated_grid():
    """The rotated scaffold's deposited fibre grid must be a RIGID rotation of the
    un-rotated grid: identical pass count (no dropped/added fibre) and every pass
    in its rotated place to within g-code precision. Guards the pass-generation fix
    (generate un-rotated, then rotate) against any future re-clip regression."""
    layers = 5
    kw = dict(width=10.0, height=10.0, stepover=0.5, rotation=90.0,
              fiber_diameter=0.02, layers=layers, speed=600.0,
              start_x=5.0, start_y=5.0)
    ox, oy = 10.0, 10.0                      # footprint centre (start + w/2, h/2)

    def long_passes(g):
        """Deposition passes only: G1/G2/G3 segments >> the loop/turnaround scale."""
        segs, prev = [], None
        for m in parse_moves(g):
            if prev is not None and m["cmd"] != "G0" \
                    and math.hypot(m["x"] - prev[0], m["y"] - prev[1]) >= 3.0:
                segs.append(((prev[0] + m["x"]) / 2.0, (prev[1] + m["y"]) / 2.0))
            prev = (m["x"], m["y"])
        return segs

    mids0 = long_passes(sg.generate_scaffold_gcode(**kw, footprint_angle=0.0))
    for fa in (40.0, 25.0, 55.0, 130.0):
        midf = long_passes(sg.generate_scaffold_gcode(**kw, footprint_angle=fa))
        assert len(midf) == len(mids0), \
            f"footprint_angle={fa} changed the deposited pass count " \
            f"({len(mids0)} -> {len(midf)})"
        a = math.radians(-fa)
        c, s = math.cos(a), math.sin(a)
        worst = 0.0
        for (mx, my) in midf:                # un-rotate about the centre
            ux, uy = mx - ox, my - oy
            rx, ry = ux * c - uy * s + ox, ux * s + uy * c + oy
            worst = max(worst, min(math.hypot(rx - qx, ry - qy)
                                   for (qx, qy) in mids0))
        assert worst < 2e-4, \
            f"footprint_angle={fa}: deposited grid is not a rigid rotation " \
            f"(a pass is {worst:.2e} mm off its rotated position)"


# --------------------------------------------------------------------------- #
# per-layer stepover + period offset (ride an angle_pattern)
# --------------------------------------------------------------------------- #
def _layers(g):
    """Split parsed moves into layers at each Z-bearing G0 (a layer entry)."""
    out, cur = [], None
    for m in parse_moves(g):
        if m["cmd"] == "G0" and m["has_z"]:
            cur = []
            out.append(cur)
        if cur is not None:
            cur.append(m)
    return out


def _pl(**kw):
    """A 4-layer all-horizontal (angle 0) 10x10 square at (5, 5). angle_pattern is
    forced so the per-layer stepover/offset lists apply; arc_radius left DEFAULT
    (so the loop radius tracks each layer's stepover) unless overridden."""
    base = dict(width=10.0, height=10.0, stepover=1.0, rotation=90.0,
                fiber_diameter=0.02, layers=4, speed=600.0,
                start_x=5.0, start_y=5.0, angle_pattern=[0.0, 0.0, 0.0, 0.0])
    base.update(kw)
    return sg.generate_scaffold_gcode(**base)


def test_angle_stepover_offset_inert_when_unset():
    """None / all-None / blank lists are byte-identical to omitting them."""
    g0 = _pl()
    assert g0 == _pl(angle_stepovers=None, angle_offsets=None), \
        "None lists must be byte-identical"
    assert g0 == _pl(angle_stepovers=[None, None, None, None],
                     angle_offsets=[None, None, None, None]), \
        "all-None lists must be byte-identical"
    assert sg.parse_layer_value_list("") is None
    assert sg.parse_layer_value_list(",,") is None
    assert sg.parse_layer_value_list("0.3,,0.5") == [0.3, None, 0.5]


def test_per_layer_stepover_sets_pitch():
    """_layer_passes honours a per-layer stepover: the pass count drops and the
    pitch matches the requested stepover (measured on the perpendicular = Y for
    angle-0 fibres)."""
    fine = sg._layer_passes(0.0, 10.0, 10.0, 1.0)
    coarse = sg._layer_passes(0.0, 10.0, 10.0, 2.0)
    assert len(coarse) < len(fine), "a coarser stepover must lay fewer passes"
    ys = sorted(p[0][1] for p in coarse)
    diffs = [b - a for a, b in zip(ys, ys[1:])]
    assert all(abs(d - 2.0) < 1e-9 for d in diffs), \
        f"pitch should equal the stepover (2.0), got {diffs}"


def test_period_offset_shifts_grid_and_still_fills():
    """A period offset slides the whole band by that distance perpendicular to the
    fibres (base grid + offset), and the footprint stays filled (guard lines)."""
    step = 1.0
    base = sorted(p[0][1] for p in sg._layer_passes(0.0, 10.0, 10.0, step))
    shifted = sorted(p[0][1] for p in
                     sg._layer_passes(0.0, 10.0, 10.0, step, phase_offset=0.3))
    # every shifted line sits on the base grid displaced by exactly +0.3
    for y in shifted:
        frac = (y - 0.3 - base[0]) / step
        assert abs(frac - round(frac)) < 1e-6, \
            f"line {y} is not the base grid shifted by 0.3"
    # the band still spans the footprint (no starved edge)
    assert min(shifted) <= -5.0 + step + 1e-9 and max(shifted) >= 5.0 - step - 1e-9, \
        f"offset thinned the footprint: span [{min(shifted)}, {max(shifted)}]"
    # and it actually moved the g-code
    assert _pl(angle_offsets=[0.3, None, None, None]) != _pl(), \
        "a period offset must change the emitted g-code"


def _reach(layer):
    """How far this layer's moves balloon PAST the footprint x-edges (x in [5, 15])
    -- the turnaround-loop excursion for angle-0 fibres."""
    return max(max(5.0 - m["x"], m["x"] - 15.0, 0.0) for m in layer)


def test_per_layer_loop_radius_tracks_stepover():
    """With arc_radius at its 1.5 x stepover DEFAULT, a wider-stepover layer gets
    wider turnaround loops; an EXPLICIT arc_radius does NOT scale with the layer's
    stepover (it applies as given to every layer)."""
    base = _reach(_layers(_pl())[0])
    wide = _reach(_layers(_pl(angle_stepovers=[2.0, None, None, None]))[0])
    fixed = _reach(_layers(_pl(angle_stepovers=[2.0, None, None, None],
                               arc_radius=1.5))[0])
    assert wide > base + 1.0, \
        f"default-arc loops did not scale with the layer stepover ({base:.2f} -> {wide:.2f})"
    assert fixed < wide - 1.0, \
        f"explicit arc_radius wrongly scaled ({fixed:.2f} vs wide {wide:.2f})"
    # an explicit radius keeps this layer near the baseline reach, not the wide one
    assert abs(fixed - base) < abs(fixed - wide), \
        f"explicit arc should ride the given radius, not the stepover ({base:.2f}/{fixed:.2f}/{wide:.2f})"


# --------------------------------------------------------------------------- #
# Non-finite numbers never reach the g-code
# --------------------------------------------------------------------------- #
# RED on cc329c7: `--start-x nan` exited 0 and wrote `G1 Xnan Y10` on every
# line; `--start-z nan`, `--exit-margin nan`, `--origin-offset nan 0 0` and
# `--plate-top-z nan` did the same, and a --scaffolds-json `start_z: Infinity`
# wrote `G0 Zinf` to disk before a traceback. A printer handed that file
# either rejects it mid-job or moves to an undefined position.
_NONFINITE_FLAGS = (["--start-x", "nan"], ["--start-y", "nan"],
                    ["--start-z", "nan"], ["--exit-margin", "nan"],
                    ["--origin-offset", "nan", "0", "0"],
                    ["--plate-top-z", "nan"], ["--rotation", "inf"],
                    ["--start-angle", "nan"], ["--arc-radius-mult", "nan"],
                    ["--travel-offset", "nan"],
                    ["--stabilization", "--stab-start-x", "nan"])


def _main_exit(argv):
    import contextlib
    import io
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            rc = sg.main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, err.getvalue()


def test_cli_rejects_nonfinite_numbers():
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "x.gcode")
        bad = []
        for flag in _NONFINITE_FLAGS:
            rc, err = _main_exit(["-o", out] + flag)
            msg = err.strip().splitlines()[-1] if err.strip() else ""
            if rc != 2 or os.path.exists(out) or "finite" not in msg:
                bad.append((flag, rc, os.path.exists(out), msg[-80:]))
            if os.path.exists(out):
                os.remove(out)
        # CONTROL: the same door with a finite value writes a file
        rc, _ = _main_exit(["-o", out, "--start-x", "5", "--layers", "1",
                            "--width", "3", "--height", "3"])
        assert rc in (0, None) and os.path.exists(out), \
            "control: a finite --start-x must still generate"
    assert not bad, f"non-finite values not rejected: {bad}"


def test_scaffold_spec_rejects_nonfinite_numbers():
    base = dict(width=10.0, height=10.0, stepover=0.5, rotation=90.0,
                fiber_diameter=0.02, layers=2, speed=600.0, start_x=0.0,
                start_y=10.0, start_z=0.0, start_angle=0.0, arc_segments=8)
    for key, val in (("start_x", float("nan")), ("start_z", float("inf")),
                     ("rotation", float("nan")),
                     ("angle_pattern", [0.0, float("nan")])):
        try:
            sg.generate_multi_scaffold_gcode(
                [sg._scaffold_from_spec(dict(base, **{key: val}))])
        except ValueError as exc:
            assert key in str(exc) and "finite" in str(exc), \
                f"{key}: the message should name the field: {exc}"
            continue
        raise AssertionError(f"{key}={val} should raise ValueError")
    sg.generate_multi_scaffold_gcode([sg._scaffold_from_spec(base)])  # CONTROL


def test_scaffolds_json_errors_name_the_scaffold_and_field():
    """RED on cc329c7: a spec missing `stepover` failed with the bare message
    `'stepover'`, and `"width": "6"` with `unsupported operand type(s) for +:
    'int' and 'str'` -- Python's words, not the file's."""
    import json
    import os
    import tempfile
    good = dict(width=6, height=6, stepover=0.5, rotation=90,
                fiber_diameter=0.02, layers=2, speed=600)
    cases = ((dict(good, stepover=None), "stepover", "missing"),
             (dict(good, width="6"), "width", "number"),
             (dict(good, layers=True), "layers", "number"),
             ("not a dict", "scaffold 2", "dict"))
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "x.gcode")
        bad = []
        for spec, field, word in cases:
            if isinstance(spec, dict) and spec.get("stepover", 0) is None:
                spec = {k: v for k, v in spec.items() if k != "stepover"}
            p = os.path.join(d, "s.json")
            with open(p, "w") as fh:
                json.dump([good, spec] if field == "scaffold 2" else [spec], fh)
            rc, err = _main_exit(["-o", out, "--scaffolds-json", p])
            msg = err.strip().splitlines()[-1] if err.strip() else ""
            if rc != 2 or field not in msg or word not in msg:
                bad.append((field, rc, msg[-90:]))
        # CONTROL: the good spec generates
        p = os.path.join(d, "g.json")
        with open(p, "w") as fh:
            json.dump([good], fh)
        rc, _ = _main_exit(["-o", out, "--scaffolds-json", p])
        assert rc in (0, None) and os.path.exists(out), "control spec failed"
    assert not bad, f"unhelpful --scaffolds-json errors: {bad}"


def test_stabilization_inherits_link_loops():
    """The stabilization base takes scaffold 1's interlayer linking loops.

    The base is the g-code BEFORE the first move into the scaffold (which
    starts at x = 30, the base at x = 0..10). Turning scaffold 1's loops on
    must change that section and lengthen it -- each loop is extra moves --
    while with no stabilization the same switch leaves the (empty) prefix
    alone. RED before the fix: the base was emitted with link_loops at its
    default, "off", whatever scaffold 1 asked for, so both sections matched."""
    base = dict(width=10.0, height=10.0, stepover=1.0, rotation=90.0,
                fiber_diameter=0.02, layers=4, speed=600.0, start_angle=0.0,
                arc_radius=None, arc_segments=8, start_x=30.0, start_y=0.0)
    # the GUI's default base, 15 mm at 1.7 mm: not a whole number of
    # stepovers, so each layer ends 0.99 mm from where the next starts at the
    # same corner -- the case the corner test refused (test below)
    stab = dict(width=15.0, height=15.0, stepover=1.7, fiber_diameter=0.02,
                layers=4, arc_radius=None, start_x=0.0, start_y=0.0)

    def base_section(link):
        g = sg.generate_multi_scaffold_gcode(
            [dict(base, link_loops=link)], stabilization=stab)
        out = []
        for line in g.splitlines():
            xs = [float(t[1:]) for t in line.split()[1:] if t[0] == "X"]
            if xs and xs[0] >= 20.0:
                break
            out.append(line)
        return out

    off, on = base_section("off"), base_section("manual")
    assert len(off) > 50, f"base section not found ({len(off)} lines)"
    assert len(on) > len(off), (
        "scaffold 1's linking loops did not reach the stabilization base "
        f"({len(on)} vs {len(off)} lines)")


def test_link_loops_fire_on_non_multiple_footprint():
    """A footprint that is not a whole number of stepovers still gets its
    interlayer loops. 15 mm at 1.7 mm leaves a 0.7 mm margin, so each layer
    ends 0.99 mm from where the next starts at the SAME corner. RED before
    the fix: the 0.5 mm link distance alone refused every such corner, and
    "manual" drew nothing. Control: an exact multiple (15 mm at 1.5 mm) is
    unchanged by the fix and keeps its loops."""
    def n_lines(w, so, link):
        b = dict(width=w, height=w, stepover=so, rotation=90.0,
                 fiber_diameter=0.02, layers=6, speed=600.0, start_angle=0.0,
                 arc_radius=None, arc_segments=8, start_x=0.0, start_y=0.0,
                 link_loops=link)
        return len(sg.generate_multi_scaffold_gcode([b]).splitlines())

    for so in (1.7, 1.5):
        off, man, auto = (n_lines(15.0, so, m)
                          for m in ("off", "manual", "auto"))
        assert man > off, f"15 mm / {so} mm: manual drew no loops"
        assert auto == man, f"15 mm / {so} mm: auto refused a 90 deg corner"


def test_link_loop_bridges_ends_that_do_not_meet():
    """On 15 mm at 1.7 mm layer 1 ends at (15, 14.3) heading +x and layer 2
    starts at (14.3, 15) heading -y. The loop must run on along the old fibre
    and come down the new one: the climb is a pure Z hop at the exit, the
    loop's first move stays on y = 14.3 going +x, and its last move arrives at
    the entry heading -y, in line with the pass that follows. RED before the
    fix: the climb hopped diagonally to the entry and the loop set off along
    y = 15, 0.7 mm off the fibre it continues."""
    spec = dict(width=15.0, height=15.0, stepover=1.7, rotation=90.0,
                fiber_diameter=0.02, layers=2, speed=600.0, start_angle=0.0,
                arc_radius=None, arc_segments=8, start_x=0.0, start_y=0.0)

    def xy(line):
        w = {t[0]: t[1:] for t in line.split()[1:]}
        return float(w["X"]), float(w["Y"])

    def climb(g):
        return next(k for k, ln in enumerate(g) if ln.startswith("G0")
                    and "Z0.02" in ln)

    off = sg.generate_multi_scaffold_gcode([dict(spec, link_loops="off")])
    entry = xy(off.splitlines()[climb(off.splitlines())])
    g = sg.generate_multi_scaffold_gcode(
        [dict(spec, link_loops="manual")]).splitlines()
    k = climb(g)
    exit_ = xy(g[k - 1])
    assert entry == (14.3, 15.0) and exit_ == (15.0, 14.3), (entry, exit_)
    assert xy(g[k]) == exit_, f"climb is not a pure Z hop: {g[k - 1]} -> {g[k]}"
    first = xy(g[k + 1])
    assert first[1] == exit_[1] and first[0] > exit_[0], (
        f"loop leaves the old fibre's line: {g[k + 1]}")
    land = next(j for j in range(k + 1, len(g)) if xy(g[j]) == entry)
    a, b = xy(g[land - 1]), xy(g[land + 1])
    assert a[0] == entry[0] == b[0] and a[1] > entry[1] > b[1], (
        f"loop does not come down the new pass: {g[land - 1:land + 2]}")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
