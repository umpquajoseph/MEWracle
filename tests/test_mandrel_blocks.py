"""Layer blocks -- one tube program made of several patterns laid outward.

`scaffold_gcode.generate_mandrel_blocks_gcode` lays a list of `TubeBlock`s in
order: e.g. 4 layers of winding, then 4 of re-entrant, then 4 of winding.
A layer is ONE WHOLE SHEET of its pattern.

COMPARATORS, none of them the blocks code:

  1. **Each family's own sheet.** A winding layer is rebuilt here from McColl
     Eq 6.1 plus the end turn, from the design's own fields; a cell layer is
     `mew_unit_cell.tube_cell_route`. Every parsed path must be its block's
     sheet up to a constant shift in v -- read back by the independently
     written `gcode_to_fibers.parse_gcode(frame=CylinderFrame)`.
     **That harness swings both ways**: a path from the WRONG block must fail
     the same comparison (`test_the_sheet_harness_can_fail`).
  2. **Invariants.** Heights climb by each block's step; the join between two
     blocks does not turn the mandrel; every
     block shares one length, and a block that does not is refused.
  3. **The CLI.** `--tube-blocks` must build the same program the call does.
  4. **Copies side by side** (HANDOFF step 4). Each copy is its blocks'
     sheets again, one pitch along X: the near sheets above for every copy
     but the first, which is laid from its FAR end -- its winding the
     u-mirror of the sheet rebuilt here, its cell layer the same closed
     circuit, judged by its multiset of (u, u', dv) segments, which a v
     shift and a restart leave alone. **Both swing**: the first copy's
     winding is NOT the unmirrored sheet, and a chevron layer's segments
     are NOT a re-entrant one's (`test_copies_lay_side_by_side`).
  5. **Per-layer settings -> blocks.** `tube_layer_blocks` against the
     groupings written out by hand from its contract.
  6. **Several tubes** (`generate_mandrel_scaffolds_gcode`, the G-code
     page's 4-axis scaffolds). Each tube is its own blocks' sheets again at
     its own start X and Z -- the same sheet comparator as 1 -- and its own
     scaffold group. Invariants: a join holds A, goes OUT before it goes
     along when the next tube is farther out; the first tube is laid from
     the end that faces the second, MIRRORED when the second lies on its
     -X side (control: the unmirrored sheet fails); the join between two
     tubes side by side runs over the gap alone, and a last tube whose
     exit would reach another keeps its near end and parks in the gap. The CLI: `--tube-scaffolds` writes the
     call's program, and a key left out of an entry is the FLAG'S default,
     not the command line's (tube 2 does not take the line's blocks).
     Copies stay byte-identical to the emitter before this one: checked
     once against `git show 60f0f1f:scaffold_gcode.py` (24 configurations,
     24 identical, a perturbed control differs) -- not re-run
     here because it needs the old file.

Run: python tests/test_mandrel_blocks.py
"""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import contextlib
import io
import json
import math
import os
import sys
import tempfile

import numpy as np

import gcode_to_fibers as gf
import mew_frame
import mew_unit_cell as uc
import scaffold_gcode as sg

_PASS, _FAIL = [], []


def check(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       ("   " + detail) if detail else ""))


# The handoff's probe tube: an 8 mm rod, 43.53 mm long, 30 deg winding.
DIAM, RADIUS, LENGTH = 8.0, 4.0, 43.53
FIBER, SPEED = 0.02, 600.0
WIND = sg.mandrel_design(DIAM, 30.0, LENGTH, pivots=20)
RE = uc.design_tube_cells("reentrant", DIAM, LENGTH, 2.0, 4.18, fit="fit",
                          rib_angle_deg=30.0)
CHEV = uc.design_tube_cells("chevron", DIAM, LENGTH, 2.0, 4.18, fit="fit",
                            amplitude=0.3)


def parse(text, radius=RADIUS):
    fd, path = tempfile.mkstemp(suffix=".gcode")
    os.close(fd)
    try:
        io.open(path, "w", encoding="utf-8").write(text)
        return gf.parse_gcode(path, mode="g1",
                              frame=mew_frame.CylinderFrame(radius))
    finally:
        os.unlink(path)


def sheet(design):
    """One layer of `design` in (u, v), starting at v = 0 -- built from the
    design's fields and the route, never from the emitter."""
    if isinstance(design, uc.TubeCellDesign):
        r = uc.tube_cell_route(design)
        return np.array([(u, v - r[0][1]) for u, v in r])
    dt = design.pass_rev * 2.0 * math.pi * design.radius
    turn = math.radians(design.end_turn_deg) * design.radius
    pts = [(0.0, 0.0)]
    for i in range(design.n_pivots):
        u = 0.0 if i % 2 else design.length
        if turn > 1e-12:
            pts.append((u, (i + 1) * dt - turn))
        pts.append((u, (i + 1) * dt))
    return np.array(pts)


def off_sheet(path, want):
    """Largest (u, v) distance from `path` to `want` shifted to start where
    the path does; inf when the point counts differ."""
    if len(path) != len(want):
        return float("inf")
    shifted = want + np.array([0.0, path[0, 1] - want[0, 1]])
    return float(np.abs(path[:, :2] - shifted).max())


def three_blocks():
    return [sg.TubeBlock(WIND, 4), sg.TubeBlock(RE, 4), sg.TubeBlock(WIND, 4)]


# --------------------------------------------------------------------------- #
# 1. each layer is its block's sheet
# --------------------------------------------------------------------------- #
def test_every_layer_is_its_blocks_sheet():
    text = sg.generate_mandrel_blocks_gcode(DIAM, three_blocks(), FIBER, SPEED)
    paths = parse(text)
    want = [sheet(WIND)] * 4 + [sheet(RE)] * 4 + [sheet(WIND)] * 4
    worst = (max(off_sheet(p, w) for p, w in zip(paths, want))
             if len(paths) == len(want) else float("inf"))
    # the floor is the 4-decimal A word, R * radians(0.5e-4) ~ 3.5e-6 mm
    check("winding x4 / re-entrant x4 / winding x4 is 12 layers, each its "
          "block's whole sheet", worst < 1e-4,
          "%d paths, worst (u, v) error %.2e mm" % (len(paths), worst))
    us = [(float(p[:, 0].min()), float(p[:, 0].max())) for p in paths]
    check("...all on the one tube, u 0 to %.2f" % LENGTH,
          all(lo > -1e-9 and hi < LENGTH + 1e-4 for lo, hi in us), "%s" % us[:2])


def test_the_sheet_harness_can_fail():
    """THE CONTROL: the same comparison must reject a layer of the wrong
    block, or `test_every_layer_is_its_blocks_sheet` proves nothing."""
    paths = parse(sg.generate_mandrel_blocks_gcode(DIAM, three_blocks(),
                                                   FIBER, SPEED))
    swapped = off_sheet(paths[4], sheet(WIND))
    # the same POINT COUNT and the wrong content: a 35 deg winding on the same
    # 20 pivots, so the comparison is shown judging coordinates, not lengths
    near = sheet(sg.mandrel_design(DIAM, 35.0, LENGTH, pivots=20))
    other = off_sheet(paths[0], near)
    check("CONTROL: a re-entrant layer is NOT a winding sheet, nor a 30 deg "
          "winding a 35 deg one of the same shape",
          swapped > 1e-3 and math.isfinite(other) and other > 1e-3,
          "errors %.3g, %.3g mm (%d vs %d points)"
          % (swapped, other, len(paths[0]), len(near)))


# --------------------------------------------------------------------------- #
# 2. invariants
# --------------------------------------------------------------------------- #
def test_heights_climb_by_each_blocks_step():
    blocks = [sg.TubeBlock(WIND, 2), sg.TubeBlock(CHEV, 2, step=0.04),
              sg.TubeBlock(WIND, 1)]
    paths = parse(sg.generate_mandrel_blocks_gcode(DIAM, blocks, FIBER, SPEED,
                                                   start_z=0.1))
    hs = [sorted(set(np.round(p[:, 2], 9))) for p in paths]
    want = [[0.1], [0.12], [0.16], [0.2], [0.22]]
    check("each layer sits at one h, stepping by its block's own step",
          hs == want, "%s vs %s" % (hs, want))
    paths = parse(sg.generate_mandrel_blocks_gcode(
        DIAM, three_blocks(), FIBER, SPEED, layer_step=0.05))
    hs = [round(float(p[0, 2]), 9) for p in paths]
    check("...and `layer_step` sets the default for every block",
          hs == [round(0.05 * i, 9) for i in range(12)], "%s" % hs[:4])


def test_a_join_does_not_turn_the_mandrel():
    """No turn between blocks: the next block starts at
    the A the last stopped at, so its first point is the last one's v. Only a
    different start u moves the head -- the chevron's overhang -- and then by
    a `G0 X` alone."""
    blocks = [sg.TubeBlock(WIND, 2), sg.TubeBlock(RE, 2),
              sg.TubeBlock(CHEV, 1), sg.TubeBlock(WIND, 1)]
    text = sg.generate_mandrel_blocks_gcode(DIAM, blocks, FIBER, SPEED)
    paths = parse(text)
    dv = [float(paths[i][0, 1] - paths[i - 1][-1, 1]) for i in (2, 4, 5)]
    check("the first point of each block is the last block's v",
          len(paths) == 6 and max(abs(x) for x in dv) < 1e-4,
          "v jumps %s mm" % [round(x, 6) for x in dv])
    travels = [ln for ln in text.splitlines() if ln.startswith("G0")]
    check("...and the only joins are the Z step, plus a G0 X to the "
          "chevron's overhang and back",
          travels == ["G0 X0 A0 Z0 F600", "G0 Z0.02", "G0 Z0.04", "G0 Z0.06",
                      "G0 X-0.3", "G0 Z0.08", "G0 X0", "G0 Z0.1"],
          "%s" % travels)


def test_one_block_is_the_single_family_program():
    """The two single-family emitters ARE one-block calls; this pins that a
    one-block list writes no `block` mark, so a single tube's line map is
    the one it always was, while several blocks are each marked."""
    one = sg.generate_mandrel_blocks_gcode(DIAM, [sg.TubeBlock(RE, 3)], FIBER,
                                           SPEED)
    check("one block is byte-identical to generate_mandrel_cell_gcode",
          one == sg.generate_mandrel_cell_gcode(DIAM, RE, FIBER, 3, SPEED))
    with sg.record_line_map() as marks:
        sg.generate_mandrel_cell_gcode(DIAM, RE, FIBER, 3, SPEED)
    kinds = sorted({m["kind"] for m in marks})
    with sg.record_line_map() as many:
        sg.generate_mandrel_blocks_gcode(DIAM, three_blocks(), FIBER, SPEED)
    blocks = [m["block"] for m in many if m["kind"] == "block"]
    layers = [m["layer"] for m in many if m["kind"] == "layer"]
    check("...a single tube marks no block; three blocks mark three, and "
          "the layers count on across them",
          kinds == ["group", "layer"] and blocks == [0, 1, 2]
          and layers == list(range(12)),
          "%s / blocks %s / layers %s" % (kinds, blocks, layers))


def test_each_blocks_speed_is_its_own():
    blocks = [sg.TubeBlock(WIND, 1), sg.TubeBlock(RE, 1, speed=900.0)]
    text = sg.generate_mandrel_blocks_gcode(DIAM, blocks, FIBER, SPEED)
    fs = [w for ln in text.splitlines() for w in ln.split()
          if w.startswith("F")]
    check("a block's own speed is emitted once, where it starts",
          fs == ["F600", "F900"], "%s" % fs)


def _raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except Exception as exc:
        return type(exc).__name__, str(exc)
    return None, ""


def test_it_refuses_rather_than_guessing():
    scaled = uc.design_tube_cells("reentrant", DIAM, LENGTH, 2.0, 4.18,
                                  rib_angle_deg=30.0)       # fit="scale"
    name, msg = _raises(sg.generate_mandrel_blocks_gcode, DIAM,
                        [sg.TubeBlock(WIND, 1), sg.TubeBlock(scaled, 1)],
                        FIBER, SPEED)
    check("a block of another length is refused, naming the cure",
          name == "ValueError" and "shares one length" in msg
          and "fit='fit'" in msg, msg[:90])
    other = uc.design_tube_cells("reentrant", 3.0, LENGTH, 2.0, 4.18,
                                 fit="fit")
    name, msg = _raises(sg.generate_mandrel_blocks_gcode, DIAM,
                        [sg.TubeBlock(WIND, 1), sg.TubeBlock(other, 1)],
                        FIBER, SPEED)
    check("a block resolved on another rod is refused",
          name == "ValueError" and "block 2" in msg, msg[:60])
    for bad, why in (([], "no blocks"), ([sg.TubeBlock(WIND, 0)], "0 layers"),
                     ([sg.TubeBlock(WIND, 1, step=0.0)], "a zero step"),
                     ([sg.TubeBlock(WIND, 1, speed=-1.0)], "a negative speed")):
        name, _ = _raises(sg.generate_mandrel_blocks_gcode, DIAM, bad, FIBER,
                          SPEED)
        check("%s is refused" % why, name == "ValueError")


def test_a_custom_cell_starts_on_the_near_end():
    """A custom drawing's circuit starts at its lowest-numbered node, which
    may sit inside the tube -- the module's own honeycomb starts at u = 0.375.
    The emitter restarts the closed circuit on the near end, so every block
    joins at u = 0 and no join crosses the wall. CONTROL: the route itself
    really does start inside, so the restart did something."""
    graph = ("0.25,0; 0.75,0; 0,0.5; 1,0.5; 0.25,1; 0.75,1 | "
             "1-2:free; 3-1; 2-4; 3-5; 4-6")
    d = uc.design_tube_cells("custom", DIAM, LENGTH, 2.0, 4.18, fit="fit",
                             graph=graph)
    route = uc.tube_cell_route(d)
    paths = parse(sg.generate_mandrel_blocks_gcode(
        DIAM, [sg.TubeBlock(WIND, 1), sg.TubeBlock(d, 2)], FIBER, SPEED))
    starts = [round(float(p[0, 0]), 9) for p in paths]
    whole = all(len(p) == len(route) for p in paths[1:])
    closes = all(abs(float(p[-1, 0] - p[0, 0])) < 1e-9 for p in paths)
    check("a custom cell starts and ends its layers at u = 0",
          starts == [0.0, 0.0, 0.0] and whole and closes,
          "starts %s, whole %s, closes %s" % (starts, whole, closes))
    check("CONTROL: its route does start inside the tube",
          route[0][0] > 0.1, "route starts at u = %.4f" % route[0][0])


# --------------------------------------------------------------------------- #
# 3. the CLI
# --------------------------------------------------------------------------- #
def _cli(argv):
    fd, path = tempfile.mkstemp(suffix=".gcode")
    os.close(fd)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = sg.main(argv + ["-o", path])
            except SystemExit as exc:
                rc = exc.code
        text = io.open(path, encoding="utf-8").read() if not rc else ""
        return rc, text, buf.getvalue()
    finally:
        os.unlink(path)


def test_the_cli_builds_the_same_blocks():
    fd, spec = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        with open(spec, "w") as fh:
            json.dump([{"family": "zigzag", "layers": 4,
                        "winding_angle": 30, "tube_pivots": 20},
                       {"family": "reentrant", "layers": 4,
                        "cell_fit": "fit", "rib_angle": 30},
                       {"family": "zigzag", "layers": 4,
                        "winding_angle": 30, "tube_pivots": 20}], fh)
        # the flags are the base TUBE, whose length every block shares
        # (`tube_blocks_from_entries`): a 40 deg default with no pivots
        # would close at 44.93 mm, not the probe's 43.53
        base = ["--mandrel", "8", "--tube-length", str(LENGTH), "-d",
                str(FIBER), "--speed", str(SPEED), "--no-exit-move",
                "--winding-angle", "30", "--tube-pivots", "20"]
        rc, text, out = _cli(base + ["--tube-blocks", spec])
        want = sg.generate_mandrel_blocks_gcode(DIAM, three_blocks(), FIBER,
                                                SPEED)
        check("--tube-blocks writes the program the call does",
              rc == 0 and text == want and "3 block(s), 12 layer(s)" in out,
              "rc %r, %s" % (rc, out.splitlines()[0] if out else ""))
        with open(spec, "w") as fh:
            json.dump([{"family": "zigzag", "pivots": 20}], fh)
        rc, _, _ = _cli(base + ["--tube-blocks", spec])
        check("an unknown block key is refused", rc not in (0, None))
        rc, text, _ = _cli(base + ["--layers", "3",
                                   "--tube-layer-step", "0.05"])
        zs = [ln for ln in text.splitlines() if ln.startswith("G0 Z")]
        check("--tube-layer-step sets the step", zs == ["G0 Z0.05", "G0 Z0.1"],
              "%s" % zs)
    finally:
        os.unlink(spec)


# --------------------------------------------------------------------------- #
# 4. copies side by side
# --------------------------------------------------------------------------- #
def segments(path):
    """A layer's segments as (u, u', dv): blind to where on a closed circuit
    it starts and to a shift in v, and to nothing else."""
    p = np.asarray(path)[:, :2]
    return [(float(a[0]), float(b[0]), float(b[1] - a[1]))
            for a, b in zip(p[:-1], p[1:])]


def same_segments(a, b, tol=1e-3):
    if len(a) != len(b):
        return False
    left = list(b)
    for s in a:
        j = next((i for i, t in enumerate(left)
                  if all(abs(x - y) < tol for x, y in zip(s, t))), None)
        if j is None:
            return False
        left.pop(j)
    return True


def test_copies_lay_side_by_side():
    blocks = [sg.TubeBlock(WIND, 2), sg.TubeBlock(RE, 2)]
    gap = 5.0
    pitch = sg.tube_copy_pitch(blocks, gap)
    with sg.record_line_map() as marks:
        text = sg.generate_mandrel_blocks_gcode(DIAM, blocks, FIBER, SPEED,
                                                copies=3, copy_gap=gap)
    paths = parse(text)
    check("3 copies of 2 + 2 layers are 12 layers", len(paths) == 12,
          "%d paths" % len(paths))
    if len(paths) != 12:
        return
    over = sg.tube_overhang(blocks)
    check("the pitch is the tube, its overhang both ends, and the gap",
          abs(pitch - (LENGTH + 2 * over + gap)) < 1e-12, "%.4f" % pitch)
    spans = []
    for ci in range(3):
        us = np.concatenate([p[:, 0] for p in paths[4 * ci:4 * ci + 4]])
        spans.append((float(us.min() - ci * pitch),
                      float(us.max() - ci * pitch)))
    check("...and each copy lies on its own stretch of rod, a pitch on",
          all(lo > -over - 1e-6 and hi < LENGTH + over + 1e-4
              for lo, hi in spans), "%s" % [(round(a, 3), round(b, 3))
                                            for a, b in spans])
    # the first copy from its far end: its winding the mirror in u
    mirror = np.array([(LENGTH - u, v) for u, v in sheet(WIND)])
    w0 = max(off_sheet(paths[k], mirror) for k in (0, 1))
    plain = off_sheet(paths[0], sheet(WIND))
    check("copy 1's winding is the u-mirror of the sheet (laid from the far "
          "end); CONTROL: it is not the sheet itself",
          w0 < 1e-4 and plain > 1e-3, "%.2e / control %.3g" % (w0, plain))
    re_near = segments(sheet(RE))
    ok_cell = all(same_segments(segments(paths[k]), re_near) for k in (2, 3))
    chev = same_segments(segments(sheet(CHEV)), re_near)
    starts = [round(float(paths[k][0, 0]), 6) for k in (2, 3)]
    check("...its cell layers the same closed circuit, started on the far "
          "end; CONTROL: a chevron's segments are not a re-entrant's",
          ok_cell and not chev and min(starts) > LENGTH - 0.5,
          "starts at u %s" % starts)
    later = []
    for ci in (1, 2):
        for k, want in ((0, sheet(WIND)), (1, sheet(WIND)), (2, sheet(RE)),
                        (3, sheet(RE))):
            q = paths[4 * ci + k].copy()
            q[:, 0] -= ci * pitch
            later.append(off_sheet(q, want))
    check("copies 2 and 3 are the near sheets, a pitch on each",
          max(later) < 1e-4, "worst %.2e mm" % max(later))
    groups = [m["group"] for m in marks if m["kind"] == "group"]
    per = [sum(1 for m in marks if m["kind"] == "layer"
               and m["line"] >= g["line"]
               and all(m["line"] < h["line"] for h in marks
                       if h["kind"] == "group" and h["line"] > g["line"]))
           for g in marks if g["kind"] == "group"]
    check("each copy is a scaffold group of its own, of 4 layers",
          groups == [0, 1, 2] and per == [4, 4, 4], "%s %s" % (groups, per))
    lines = text.splitlines()
    joins = [lines[i:i + 2] for i, ln in enumerate(lines)
             if ln.startswith("G0 X") and i and lines[i + 1] == "G0 Z0"]
    xs = [float(j[0].split("X")[1]) for j in joins]
    check("the joins hold A (a G0 X, then down to the first layer) to each "
          "copy's near end", xs == [round(pitch, 4), round(2 * pitch, 4)]
          and all(len(j[0].split()) == 2 for j in joins), "%s" % joins)
    ends = [float(paths[4 * ci + 3][-1, 0]) for ci in (0, 1)]
    check("copy 1 ends at its far end, facing the gap; copy 2 at its near "
          "end, so the join to copy 3 crosses its wall",
          abs(ends[0] - LENGTH) < 1e-4 and abs(ends[1] - pitch) < 1e-4
          and list(sg.tube_copy_crossings(3)) == [3]
          and list(sg.tube_copy_crossings(2)) == [], "ends %s" % ends)
    one = sg.generate_mandrel_blocks_gcode(DIAM, blocks, FIBER, SPEED)
    check("copies=1 is the program it always was",
          one == sg.generate_mandrel_blocks_gcode(DIAM, blocks, FIBER, SPEED,
                                                  copies=1, copy_gap=gap))
    name, msg = _raises(sg.generate_mandrel_blocks_gcode, DIAM, blocks, FIBER,
                        SPEED, copies=2, copy_gap=1.0, exit_margin=5.0)
    check("an exit longer than the gap is refused: it would park on the copy "
          "before", name == "ValueError" and "gap behind it" in msg, msg[:70])
    name, _ = _raises(sg.generate_mandrel_blocks_gcode, DIAM, blocks, FIBER,
                      SPEED, copies=2, copy_gap=5.0, exit_margin=5.0)
    check("...and one that fits is not", name is None)
    for bad in ({"copies": 0}, {"copies": 2, "copy_gap": -1.0}):
        name, _ = _raises(sg.generate_mandrel_blocks_gcode, DIAM, blocks,
                          FIBER, SPEED, **bad)
        check("%s is refused" % bad, name == "ValueError")


# --------------------------------------------------------------------------- #
# 5. per-layer settings -> blocks
# --------------------------------------------------------------------------- #
def test_layer_settings_group_into_blocks():
    tlb = sg.tube_layer_blocks
    cases = (
        ("no settings is one block", (12, {}), [{"layers": 12}]),
        ("layers 5-8 re-entrant is three blocks",
         (12, {k: {"family": "reentrant"} for k in range(4, 8)}),
         [{"layers": 4}, {"family": "reentrant", "layers": 4},
          {"layers": 4}]),
        ("a step on layer 1 has nothing below it, and is ignored",
         (6, {0: {"step": 0.05}}), [{"layers": 6}]),
        ("a step on layer 6 alone is a one-layer block",
         (12, {5: {"step": 0.1}}),
         [{"layers": 5}, {"layers": 1, "step": 0.1}, {"layers": 6}]),
        ("a block that starts the tube takes its second layer's step",
         (4, {0: {"speed": 900.0, "step": 9.0},
              1: {"speed": 900.0, "step": 0.05}}),
         [{"speed": 900.0, "layers": 2, "step": 0.05}, {"layers": 2}]),
        ("layers past the count are ignored",
         (3, {7: {"family": "chevron"}}), [{"layers": 3}]),
    )
    for name, args, want in cases:
        got = tlb(*args)
        check(name, got == want, "%s" % got)
    name, msg = _raises(tlb, 3, {1: {"pivots": 20}})
    check("an unknown layer setting is refused, naming the layer",
          name == "ValueError" and "layer 2" in msg, msg[:60])


def test_the_cli_takes_inline_blocks_and_copies():
    base = ["--mandrel", "8", "--tube-length", str(LENGTH), "-d", str(FIBER),
            "--speed", str(SPEED), "--no-exit-move", "--winding-angle", "30",
            "--tube-pivots", "20"]
    spec = json.dumps([{"layers": 2}, {"family": "reentrant", "layers": 2,
                                       "cell_fit": "fit", "rib_angle": 30}])
    rc, text, out = _cli(base + ["--tube-blocks", spec])
    want = sg.generate_mandrel_blocks_gcode(
        DIAM, [sg.TubeBlock(WIND, 2), sg.TubeBlock(RE, 2)], FIBER, SPEED)
    check("--tube-blocks takes the JSON itself; a block with no pattern key "
          "is the flags' own tube", rc == 0 and text == want,
          "rc %r" % (rc,))
    rc, text, out = _cli(base + ["--layers", "2", "--tube-copies", "3",
                                 "--tube-copy-gap", "5"])
    want = sg.generate_mandrel_gcode(DIAM, LENGTH, 30.0, FIBER, 2, SPEED,
                                     design=WIND, copies=3, copy_gap=5.0)
    check("--tube-copies / --tube-copy-gap write the call's program, and "
          "say which join crosses a wall",
          rc == 0 and text == want and "3 copies side by side" in out
          and "NOTE the join to copy 3 crosses" in out,
          "rc %r, %s" % (rc, [ln for ln in out.splitlines()
                               if "cop" in ln][:2]))
    rc, _, out = _cli(base + ["--tube-copies", "2"])
    check("...and two copies cross nothing", rc == 0
          and "NOTE the join" not in out)


# --------------------------------------------------------------------------- #
# 6. several tubes on one rod
# --------------------------------------------------------------------------- #
def _groups(marks):
    out = []
    for m in marks:
        if m["kind"] == "group":
            out.append(0)
        elif m["kind"] == "layer" and out:
            out[-1] += 1
    return out


def test_several_tubes_each_its_own_sheets():
    a = sg.TubeScaffold([sg.TubeBlock(WIND, 2)], FIBER, SPEED, start_x=1.0)
    b = sg.TubeScaffold([sg.TubeBlock(RE, 3)], 0.03, 500.0,
                        start_x=1.0 + LENGTH + 6.0, start_z=0.0)
    with sg.record_line_map() as marks:
        text = sg.generate_mandrel_scaffolds_gcode(DIAM, [a, b])
    paths = parse(text)
    check("two tubes of 2 and 3 layers are 5 layers, in two groups",
          len(paths) == 5 and _groups(marks) == [2, 3],
          "%d paths, groups %s" % (len(paths), _groups(marks)))
    if len(paths) != 5:
        return
    mirror = np.array([(LENGTH - u, v) for u, v in sheet(WIND)])
    w = max(off_sheet(p - [1.0, 0, 0][:p.shape[1]], mirror)
            for p in paths[:2])
    check("tube 1 (the second lies on its +X side) is laid from its far "
          "end: the mirrored sheet, one start X along", w < 1e-4,
          "%.2e" % w)
    re_near = segments(sheet(RE))
    ok = all(same_segments(segments(np.asarray(p) - [b.start_x, 0, 0][
        :np.asarray(p).shape[1]]), re_near) for p in paths[2:])
    check("tube 2's layers are its own cell's circuit, at its own start X",
          ok)
    hs = [round(float(np.asarray(p)[0, 2]), 6) for p in paths]
    check("tube 2 steps out by ITS fibre (0.03), from the rod",
          hs[2:] == [0.0, 0.03, 0.06], "%s" % hs)
    feeds = [ln for ln in text.splitlines() if " F" in ln]
    check("tube 2 runs at ITS speed", any("F500" in ln for ln in feeds),
          "%s" % feeds[:3])


def test_mirror_when_the_second_lies_on_the_left():
    a = sg.TubeScaffold([sg.TubeBlock(WIND, 1)], FIBER, SPEED,
                        start_x=LENGTH + 10.0)
    b = sg.TubeScaffold([sg.TubeBlock(WIND, 1)], FIBER, SPEED, start_x=0.0)
    paths = parse(sg.generate_mandrel_scaffolds_gcode(DIAM, [a, b]))
    q = np.asarray(paths[0]).copy()
    q[:, 0] -= a.start_x
    near = off_sheet(q, sheet(WIND))
    mirror = off_sheet(q, np.array([(LENGTH - u, v)
                                    for u, v in sheet(WIND)]))
    check("the second tube on the -X side: tube 1 is laid from its NEAR "
          "end, facing it; CONTROL: the mirrored sheet does not fit",
          near < 1e-4 and mirror > 1e-3, "%.2e / control %.3g"
          % (near, mirror))


def test_a_stacked_join_goes_out_first():
    a = sg.TubeScaffold([sg.TubeBlock(WIND, 2)], FIBER, SPEED)
    b = sg.TubeScaffold([sg.TubeBlock(WIND, 2)], FIBER, SPEED,
                        start_z=2 * FIBER)
    text = sg.generate_mandrel_scaffolds_gcode(DIAM, [a, b])
    lines = text.splitlines()
    i = next(i for i, ln in enumerate(lines) if ln == "G0 Z0.04")
    check("a tube stacked on another: the join steps OUT, then along, and "
          "holds A", lines[i + 1].startswith("G0 X")
          and len(lines[i + 1].split()) == 2, "%s" % lines[i:i + 2])
    paths = parse(text)
    hs = [round(float(np.asarray(p)[0, 2]), 6) for p in paths]
    check("...and its layers start where the wall below ended",
          hs == [0.0, 0.02, 0.04, 0.06], "%s" % hs)
    side = sg.generate_mandrel_scaffolds_gcode(DIAM, [
        a, sg.TubeScaffold([sg.TubeBlock(WIND, 2)], FIBER, SPEED,
                           start_x=LENGTH + 5.0)]).splitlines()
    j = next(i for i, ln in enumerate(side) if ln.startswith("G0 X") and i > 2)
    check("CONTROL: side by side at the same Z, the join goes along, then "
          "down", side[j + 1] == "G0 Z0", "%s" % side[j:j + 2])


def test_an_exit_onto_another_tube_parks_in_the_gap():
    """The last tube's exit would land on scaffold 1. The tube keeps its
    near end, so the join runs over the gap alone, and the exit parks
    halfway across it. RED: laying the last tube from its far end to free
    the exit sent the join along tube 2's whole length before it was laid
    (Demo: Two tubes on a mandrel, G0 X13 -> X27 over X17..27)."""
    a = sg.TubeScaffold([sg.TubeBlock(WIND, 1)], FIBER, SPEED)
    b = sg.TubeScaffold([sg.TubeBlock(WIND, 1)], FIBER, SPEED,
                        start_x=LENGTH + 2.0)
    joins = []
    text = sg.generate_mandrel_scaffolds_gcode(DIAM, [a, b], exit_margin=5.0,
                                               joins=joins)
    (k, x_from, x_to, crossed), = joins
    lo, hi = b.span()
    check("the join to tube 2 runs from tube 1's end to tube 2's near end, "
          "over no tube", k == 1 and abs(x_from - a.span()[1]) < 1e-9
          and abs(x_to - lo) < 1e-9 and crossed == [],
          "%s" % (joins,))
    park = float(text.splitlines()[-1].split()[1][1:])
    check("...and the exit parks halfway across the gap",
          abs(park - 0.5 * (a.span()[1] + lo)) < 1e-4,
          "park %.4f, gap %.4f..%.4f" % (park, a.span()[1], lo))
    plain = sg.generate_mandrel_scaffolds_gcode(DIAM, [a, b],
                                                exit_margin=1.5)
    park = float(plain.splitlines()[-1].split()[1][1:])
    check("CONTROL: an exit that fits in the gap goes its whole distance",
          abs(park - (lo - 1.5)) < 1e-4, "park %.4f, tube 2 from %.4f"
          % (park, lo))
    # three in a row: the join to the third crosses the second, the fewest
    # any order of ends allows; a last tube boxed in parks in a gap
    c = sg.TubeScaffold([sg.TubeBlock(WIND, 1)], FIBER, SPEED,
                        start_x=2 * LENGTH + 4.0)
    joins = []
    text = sg.generate_mandrel_scaffolds_gcode(DIAM, [a, b, c],
                                               exit_margin=5.0, joins=joins)
    check("three in a row: only the join to the third crosses a tube, "
          "the second", [j[3] for j in joins] == [[], [1]], "%s" % (joins,))
    joins = []
    text = sg.generate_mandrel_scaffolds_gcode(DIAM, [a, c, b],
                                               exit_margin=5.0, joins=joins)
    park = float(text.splitlines()[-1].split()[1][1:])
    check("a tube laid last between two others is not refused: it parks "
          "in a gap, on no tube", not any(
              s.span()[0] < park < s.span()[1] for s in (a, b, c)),
          "park %.4f" % park)


def test_the_cli_takes_several_tubes():
    base = ["--mandrel", "8", "--no-exit-move", "-d", str(FIBER)]
    spec = [{"tube_length": str(LENGTH), "winding_angle": "30",
             "tube_pivots": "20", "layers": "2", "speed": str(SPEED),
             "tube_blocks": json.dumps([{"layers": 1}, {
                 "family": "reentrant", "layers": 1, "cell_fit": "fit",
                 "rib_angle": 30}])},
            {"tube_length": str(LENGTH), "winding_angle": "30",
             "tube_pivots": "20", "layers": "2", "speed": str(SPEED),
             "start_x": str(LENGTH + 5.0)}]
    # the command line carries blocks and a layer count of its own: tube 2
    # names neither, and must take the flags' DEFAULTS, not these
    rc, text, out = _cli(base + ["--tube-scaffolds", json.dumps(spec),
                                 "--tube-blocks", json.dumps([{"layers": 3}]),
                                 "--tube-pivots", "20"])
    want = sg.generate_mandrel_scaffolds_gcode(DIAM, [
        sg.TubeScaffold([sg.TubeBlock(WIND, 1), sg.TubeBlock(RE, 1)],
                        FIBER, SPEED),
        sg.TubeScaffold([sg.TubeBlock(WIND, 2)], FIBER, SPEED,
                        start_x=LENGTH + 5.0)])
    check("--tube-scaffolds writes the call's program; tube 2, which names "
          "no blocks, takes none -- not the command line's",
          rc == 0 and text == want,
          "rc %r" % (rc,))
    rc, _, _ = _cli(base + ["--tube-scaffolds", json.dumps(spec),
                            "--tube-copies", "2"])
    check("...and is refused beside --tube-copies", rc not in (0, None))
    rc, _, _ = _cli(base + ["--tube-scaffolds",
                            json.dumps([{"pivots": "20"}])])
    check("an unknown key is refused", rc not in (0, None))


def main():
    test_every_layer_is_its_blocks_sheet()
    test_the_sheet_harness_can_fail()
    test_heights_climb_by_each_blocks_step()
    test_a_join_does_not_turn_the_mandrel()
    test_one_block_is_the_single_family_program()
    test_each_blocks_speed_is_its_own()
    test_it_refuses_rather_than_guessing()
    test_a_custom_cell_starts_on_the_near_end()
    test_the_cli_builds_the_same_blocks()
    test_copies_lay_side_by_side()
    test_layer_settings_group_into_blocks()
    test_the_cli_takes_inline_blocks_and_copies()
    test_several_tubes_each_its_own_sheets()
    test_mirror_when_the_second_lies_on_the_left()
    test_a_stacked_join_goes_out_first()
    test_an_exit_onto_another_tube_parks_in_the_gap()
    test_the_cli_takes_several_tubes()
    print()
    print("%d passed, %d failed, %d total"
          % (len(_PASS), len(_FAIL), len(_PASS) + len(_FAIL)))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
