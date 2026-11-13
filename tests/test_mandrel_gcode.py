"""The mandrel generator -- strategy "zigzag".

FOUR COMPARATORS, none of them written by the code under test:

  1. **An independently-written parser.** `scaffold_gcode.generate_mandrel_gcode`
     serialises a path and `gcode_to_fibers.parse_gcode(frame=CylinderFrame(...))`
     reads g-code back into surface coordinates; neither knows about the other,
     and the phase-3 acceptance bar is that a generated tube round-trips back to
     the commanded helix. **That harness swings both ways**:
     `test_the_roundtrip_harness_can_fail` perturbs one emitted coordinate and
     requires the same comparison to reject it.
  2. **Invariants needing no reference.** A layer must return to its own start:
     `u` back where it began AND the mandrel turned a WHOLE number of
     revolutions. That is the closure McColl's Eqs 3.1/4.2 exist to buy, and it
     is checkable with no fixture at all.
  3. **McColl's printed Fig 2a**, the worked example `mew_tube` reproduces: a 1.5 mm mandrel at 20 deg closes at 3.2368 mm with 4 pivot points
     and a 90 deg pivot angle. Here the g-code itself must show that -- four
     passes, and the A word stepping by exactly 90 deg.
  4. **The planar generator, untouched.** Mandrel mode is a sibling emitter, so
     flat output is byte-identical by construction; `test_scaffold_gcode.py` is
     what actually asserts it and it is NOT duplicated here.

THE TRAP THIS SUITE EXISTS TO CATCH: headings must be read in
the UNROLLED frame. In world XYZ the mandrel's rotation reads as travel and
every winding angle comes out 90 deg. Every angle assertion below is taken from
the parser's (u, v) output, never from world coordinates.
"""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import io
import math
import os
import sys
import tempfile

import numpy as np

import gcode_to_fibers as gf
import mew_frame
import mew_tube
import scaffold_gcode as sg

_PASS, _FAIL = [], []


def check(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       ("   " + detail) if detail else ""))


# McColl Fig 2a / Table 2 row eta = 4: a 1.5 mm-diameter mandrel at 20 deg
# closes at 3.24 mm with 4 pivot points and a 90 deg pivot angle.
DIAM, OMEGA, LENGTH = 1.5, 20.0, 3.24
RADIUS = 0.5 * DIAM
FIBER, SPEED = 0.02, 600.0


def _parse(text, radius=RADIUS, **kw):
    """g-code text -> the parser's surface-frame polylines. Writes a temp file
    because `parse_gcode` takes a path; nothing else here touches the disk."""
    fd, path = tempfile.mkstemp(suffix=".gcode")
    os.close(fd)
    try:
        io.open(path, "w", encoding="utf-8").write(text)
        return gf.parse_gcode(path, mode="g1",
                              frame=mew_frame.CylinderFrame(radius), **kw)
    finally:
        os.unlink(path)


def _commanded(design, layers, start_x=0.0, start_z=0.0, fiber=FIBER):
    """The path the generator was ASKED for, rebuilt from McColl Eq 6.1 here --
    deliberately a second expression of the same intent, so the round-trip
    compares two independent constructions rather than one function with
    itself. A LAYER IS ONE WHOLE CIRCUIT, out and back over all `n_pivots`
    (2026-10-28), and the nozzle steps out one fibre diameter per
    layer, so there is one returned array per layer and per circuit.

    The pivot ladder is continuous across the whole program, so `t`
    accumulates across layers rather than restarting."""
    dt = (design.fm + design.rm) * 2.0 * math.pi
    out = []
    t = 0.0
    u = start_x
    for li in range(layers):
        h = start_z + li * fiber
        pts = [(u, design.radius * t, h)]
        for i in range(design.n_pivots):
            t += dt
            u = start_x + (0.0 if i % 2 else design.length)
            pts.append((u, design.radius * t, h))
        out.append(np.asarray(pts, dtype=np.float64))
    return out


# --------------------------------------------------------------------------- #
# 1. the acceptance bar -- the round trip, and proof the harness can fail
# --------------------------------------------------------------------------- #
def test_a_generated_tube_roundtrips_to_the_commanded_helix():
    """The mandrel generator's acceptance criteria."""
    d = sg.mandrel_design(DIAM, OMEGA, LENGTH)
    text = sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, FIBER, 4, SPEED)
    got = _parse(text)
    want = _commanded(d, 4)
    shapes = [g.shape for g in got] == [w.shape for w in want]
    check("the parser returns one path per LAYER, one circuit each",
          shapes, "%d paths of %s points, %d layers of %s"
          % (len(got), [len(g) for g in got], len(want),
             [len(w) for w in want]))
    if not shapes:
        return
    worst = max(float(np.abs(g - w).max()) for g, w in zip(got, want))
    # the floor is the emitted coordinate precision (4 dp), not the maths
    check("every vertex round-trips to the commanded helix",
          worst < 1e-4, "worst |delta| = %.2e mm" % worst)


def test_the_roundtrip_harness_can_fail():
    """THE CONTROL. Perturb one emitted X by 0.5 mm and the same comparison must
    reject it. An instrument that answers the same for every input is not
    evidence, so this is the other half of the test above."""
    d = sg.mandrel_design(DIAM, OMEGA, LENGTH)
    text = sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, FIBER, 4, SPEED)
    lines = text.splitlines()
    idx = next(i for i, l in enumerate(lines) if l.startswith("G1 X3.2368"))
    lines[idx] = lines[idx].replace("X3.2368", "X3.7368", 1)
    got = _parse("\n".join(lines) + "\n")
    want = _commanded(d, 4)
    worst = (max(float(np.abs(g - w).max()) for g, w in zip(got, want))
             if [g.shape for g in got] == [w.shape for w in want]
             else float("inf"))
    check("a 0.5 mm perturbation IS caught by that comparison",
          worst > 0.4, "worst |delta| = %.4f mm" % worst)


def test_a_pure_rotation_is_no_longer_dropped():
    """The defect phase 1 fixed, asserted from the generator's own output: the
    tube's total circumferential travel must be REAL, not zero. Under the old
    XYZ-only whitelist a rotary-only component vanished silently."""
    text = sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, FIBER, 1, SPEED)
    got = _parse(text)
    # a full revolution is a CIRCUIT, which is one layer
    v_span = float(got[-1][-1, 1] - got[0][0, 1])
    check("the circumferential travel survives the parser",
          abs(v_span - 2.0 * math.pi * RADIUS) < 1e-6,
          "v = %.4f mm, one revolution = %.4f mm"
          % (v_span, 2.0 * math.pi * RADIUS))


# --------------------------------------------------------------------------- #
# 2. invariants -- no reference needed
# --------------------------------------------------------------------------- #
def test_a_layer_closes_on_itself():
    """u returns to its start AND the mandrel turns a whole number of
    revolutions. Fail either and the layer above lands between this one's
    fibres instead of on them (McColl, Fig 2b).

    A layer is one whole circuit, so each parsed layer
    must close on its own. From 2026-09-03 to then a layer was HALF a
    circuit, which closes by construction never -- that half is the control
    below, read off the first `n_pivots // 2` passes of the same path."""
    for eta in (2, 3, 4, 6, 7, 12):
        d = sg.mandrel_design(DIAM, OMEGA, LENGTH, eta=eta)
        text = sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, FIBER, 2, SPEED,
                                         eta=eta)
        parsed = _parse(text)
        check("eta=%d: two layers are two parsed paths" % eta,
              len(parsed) == 2, "%d paths" % len(parsed))
        for li, p in enumerate(parsed):
            du = abs(float(p[-1, 0] - p[0, 0]))
            revs = float(p[-1, 1] - p[0, 1]) / (2.0 * math.pi * RADIUS)
            check("eta=%d: layer %d returns to its start in u" % (eta, li),
                  du < 1e-9 and len(p) == d.n_pivots + 1,
                  "|du| = %.2e mm, %d passes" % (du, len(p) - 1))
            check("eta=%d: and turns a whole number of revolutions"
                  % eta, abs(revs - round(revs)) < 1e-9 and revs >= 1.0,
                  "%.9f revolutions, N=%d" % (revs, d.n_pivots))
        # ...and the HALF must not, or the closure above proves nothing
        first = parsed[0][:d.n_pivots // 2 + 1]
        half_revs = float(first[-1, 1] - first[0, 1]) / (2.0 * math.pi * RADIUS)
        check("eta=%d: CONTROL: half a circuit does NOT close" % eta,
              abs(half_revs - round(half_revs)) > 1e-9
              or abs(float(first[-1, 0] - first[0, 0])) > 1e-9,
              "%.9f revolutions" % half_revs)


def test_every_pass_holds_the_commanded_winding_angle():
    """Read in the UNROLLED frame. In world XYZ this comes out 90 deg for every
    pass and means nothing -- that is the trap."""
    # eta is PINNED rather than solved for a fixed target length: the closing
    # ladder is coarse on a 0.75 mm mandrel, and at 45 deg nothing closes near
    # 3.24 mm (the solver says so, correctly). The winding angle is what is
    # under test here, not the length solver.
    for omega in (10.0, 20.0, 45.0, 70.0):
        d = sg.mandrel_design(DIAM, omega, LENGTH, eta=4)
        text = sg.generate_mandrel_gcode(DIAM, LENGTH, omega, FIBER, 2, SPEED,
                                         eta=4)
        angles = []
        for p in _parse(text, radius=RADIUS):
            dd = np.diff(p, axis=0)
            angles.extend(np.degrees(np.arctan2(np.abs(dd[:, 1]),
                                                np.abs(dd[:, 0]))))
        worst = max(abs(a - omega) for a in angles)
        check("omega=%.0f deg is held by every pass" % omega, worst < 1e-3,
              "worst error %.2e deg over %d passes (L=%.4f)"
              % (worst, len(angles), d.length))


def test_the_pivot_reverses_u_and_never_v():
    """Strategy 3.1 is +alpha / -alpha: the axial direction flips at every pivot
    while the mandrel keeps turning the SAME way. A v that reverses would be the
    infinity loop (strategy 3.2), which is not built."""
    text = sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, FIBER, 2, SPEED)
    ok_u, ok_v = True, True
    for p in _parse(text):
        du = np.sign(np.diff(p[:, 0]))
        dv = np.diff(p[:, 1])
        ok_u = ok_u and bool(np.all(du[1:] == -du[:-1]))
        ok_v = ok_v and bool(np.all(dv > 0))
    check("the axial direction alternates at every pivot", ok_u)
    check("the mandrel never reverses (strategy 3.1)", ok_v)


def test_layers_register_on_each_other():
    """McColl: 'All proceeding layers follow the same pattern.' A layer must
    trace the layer below it, shifted by whole revolutions, one fibre
    diameter further out. A layer is one whole circuit, both fibre families,
    so it lands on the one below rather than crossing it. Registration is
    what a closing length buys, and it is why the length is rejected when it
    does not close."""
    period = 2.0 * math.pi * RADIUS

    def worst(paths):
        uv, h = 0.0, 0.0
        for a, b in zip(paths[:-1], paths[1:]):
            shift = round(float(b[0, 1] - a[0, 1]) / period) * period
            uv = max(uv, float(np.abs(b[:, 0] - a[:, 0]).max()),
                     float(np.abs((b[:, 1] - shift) - a[:, 1]).max()))
            h = max(h, abs(float(b[0, 2] - a[0, 2]) - FIBER))
        return uv, h

    text = sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, FIBER, 6, SPEED)
    worst_uv, worst_h = worst(_parse(text))
    check("each layer repeats the layer below it in (u, v)", worst_uv < 1e-9,
          "worst |delta| = %.2e mm" % worst_uv)
    check("...one fibre diameter further out", worst_h < 1e-12,
          "worst |delta h - d| = %.2e mm" % worst_h)
    # CONTROL: a circuit that does NOT close by a whole turn must fail the
    # same comparison, or it registers anything
    bad = sg.mandrel_design(DIAM, OMEGA, LENGTH)
    bad.pass_rev *= 1.01
    near, _ = worst(_parse(sg.generate_mandrel_gcode(
        DIAM, LENGTH, OMEGA, FIBER, 6, SPEED, design=bad)))
    check("CONTROL: a circuit 1% off closure does NOT register",
          near > 1e-6, "worst |delta| = %.2e mm" % near)


def test_the_world_geometry_is_concentric_shells_outside_the_mandrel():
    """The look the (u, v) checks structurally CANNOT do. Everything above is
    read in the unrolled frame, where a sign error in the wrap, a swapped axis
    or a radius measured from the wrong datum is invisible -- (u, v) would still
    be a tidy zigzag. Wrapped back to world, circuit i must be a shell at
    exactly R0 + i*d and NO deposited point may lie inside the bare mandrel.

    This is the cheap form of the mandrel viewer's acceptance, which
    replaces `shots.json`'s flat `underside_clearance_mm` with a radial test --
    negative clearance there means geometry through the bed; here it would mean
    fibre inside the collector.
    """
    radius, fiber, layers = 1.5, 0.05, 6
    text = sg.generate_mandrel_gcode(2.0 * radius, 12.0, 45.0, fiber, layers,
                                     SPEED)
    frame = mew_frame.CylinderFrame(radius)
    worst_shell, min_radius = 0.0, float("inf")
    for i, p in enumerate(_parse(text, radius=radius)):
        w = mew_frame.wrap_polyline(frame, p)
        r = np.hypot(w[:, 1], w[:, 2])
        worst_shell = max(worst_shell,
                          float(np.abs(r - (radius + i * fiber)).max()))
        min_radius = min(min_radius, float(r.min()))
    check("each circuit wraps to a shell at exactly R0 + i*d",
          worst_shell < 1e-9, "worst |delta r| = %.2e mm" % worst_shell)
    check("no deposited point lies inside the bare mandrel",
          min_radius >= radius - 1e-12,
          "min radius %.4f mm vs R0 %.4f" % (min_radius, radius))


# --------------------------------------------------------------------------- #
# 3. McColl's printed worked example, in the g-code itself
# --------------------------------------------------------------------------- #
def test_fig2a_prints_as_four_passes_ninety_degrees_apart():
    text = sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, FIBER, 1, SPEED)
    moves = [l for l in text.splitlines() if l.startswith("G1 ")]
    check("Fig 2a lays 4 passes", len(moves) == 4, "%d G1 moves" % len(moves))
    a = [float(w[1:]) for l in moves for w in l.split() if w.startswith("A")]
    steps = [b - c for b, c in zip(a[1:], a[:-1])]
    check("the A word steps by the 90 deg pivot angle",
          all(abs(s - 90.0) < 1e-9 for s in steps) and abs(a[0] - 90.0) < 1e-9,
          "A = %s" % a)
    x = sorted({w for l in moves for w in l.split() if w.startswith("X")})
    check("between the two ends of the closing 3.2368 mm length",
          x == ["X0", "X3.2368"], "%s" % x)


# --------------------------------------------------------------------------- #
# 4. the file itself -- house rules
# --------------------------------------------------------------------------- #
def test_the_file_obeys_the_repo_gcode_rules():
    text = sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, FIBER, 4, SPEED)
    lines = text.splitlines()
    check("no ';' comments anywhere (hard repo rule)",
          not any(";" in l for l in lines))
    check("the G21/G90 header is emitted once",
          lines[:2] == ["G21", "G90"], "%s" % lines[:2])
    check("no M30 footer (Klipper errors on it)",
          not any(l.startswith("M30") for l in lines))
    check("every deposition move carries the rotary word",
          all(" A" in l for l in lines if l.startswith("G1 ")))
    check("a layer change is a travel, so Z never moves while depositing",
          not any("Z" in l for l in lines if l.startswith("G1 ")))


def test_the_feed_is_the_resultant_and_is_emitted_once():
    """`parse_gcode` under a rotary frame clocks on the true surface path and
    says in its docstring that it assumes OUR generator feeds that resultant.
    Surface mode therefore emits one F for the whole print."""
    text = sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, FIBER, 4, SPEED)
    fs = [w for l in text.splitlines() for w in l.split() if w.startswith("F")]
    check("surface mode emits F exactly once", fs == ["F600"], "%s" % fs)


def test_omega_mode_scales_the_feed_with_the_wall():
    """The constant-rate opt-in: holding the mandrel's angular rate
    constant makes the surface speed drift by (R0 + h)/R0. The drift is the
    POINT -- it is exposed so it can be predicted, not hidden."""
    text = sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, 0.05, 4, SPEED,
                                     speed_mode="omega")
    fs = [float(w[1:]) for l in text.splitlines() for w in l.split()
          if w.startswith("F")]
    # one F per PLY since 2026-09-03: the nozzle steps out per ply, and the
    # feed follows the radius it steps to. 4 layers -> 4 feeds.
    want = [SPEED * (RADIUS + i * 0.05) / RADIUS for i in range(4)]
    check("omega mode emits one F per ply, rising with the wall",
          len(fs) == 4 and all(abs(a - b) < 1e-6 for a, b in zip(fs, want)),
          "%s vs %s" % ([round(f, 3) for f in fs], [round(w, 3) for w in want]))
    flat = sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, 0.05, 4, SPEED)
    check("...and changes only the feed, never the geometry",
          [l.split(" F")[0] for l in text.splitlines()]
          == [l.split(" F")[0] for l in flat.splitlines()])


# --------------------------------------------------------------------------- #
# 5. what it refuses
# --------------------------------------------------------------------------- #
def _raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except Exception as exc:
        return type(exc).__name__, str(exc)
    return None, ""


def test_it_refuses_rather_than_guessing():
    name, msg = _raises(sg.generate_mandrel_gcode, DIAM, LENGTH, OMEGA, FIBER,
                        1, SPEED, strategy="infinity")
    check("an unbuilt strategy is refused, not silently laid as a zigzag",
          name == "ValueError" and "not built" in msg, "%s: %s" % (name, msg[:60]))
    name, _ = _raises(sg.generate_mandrel_gcode, DIAM, LENGTH, OMEGA, FIBER,
                      1, SPEED, speed_mode="rpm")
    check("an unknown speed mode is refused", name == "ValueError")
    name, _ = _raises(sg.mandrel_design, 0.0, OMEGA, LENGTH)
    check("a zero-diameter mandrel is refused", name == "ValueError")
    # McColl Fig 2b: 3.25 mm does not close on this mandrel
    name, msg = _raises(sg.mandrel_design, DIAM, OMEGA, 3.25,
                        max_relative_error=1e-4)
    check("a non-closing length is refused, not rounded to silently",
          name == "TubeDesignError", "%s" % name)
    d = sg.mandrel_design(DIAM, OMEGA, 3.25)
    check("...while the default tolerance returns the NEAREST closing length",
          abs(d.length - 3.2368) < 1e-3, "%.4f mm" % d.length)
    for bad in (dict(fiber_diameter=0.0), dict(layers=0), dict(speed=0.0)):
        kw = dict(fiber_diameter=FIBER, layers=2, speed=SPEED)
        kw.update(bad)
        name, _ = _raises(sg.generate_mandrel_gcode, DIAM, LENGTH, OMEGA,
                          kw["fiber_diameter"], kw["layers"], kw["speed"])
        check("%s is refused" % list(bad)[0], name == "ValueError")


def test_a_pinned_design_skips_the_solve():
    d = mew_tube.TubeDesign(RADIUS, OMEGA, 6, 1)
    text = sg.generate_mandrel_gcode(DIAM, 999.0, OMEGA, FIBER, 1, SPEED,
                                     design=d)
    moves = [l for l in text.splitlines() if l.startswith("G1 ")]
    check("a supplied TubeDesign is used verbatim, target_length ignored",
          len(moves) == d.n_pivots and ("X%s" % sg.format_coord(d.length))
          in text, "%d moves, N=%d, L=%.4f" % (len(moves), d.n_pivots, d.length))


def test_a_layer_is_one_whole_circuit():
    """`layers` counts WHOLE CIRCUITS: a layer is one
    full sheet of the pattern, out and back over every pivot, and the nozzle
    steps out `layer_step` (default one fibre diameter) between them. Any
    count from 1 is legal, and every layer ends back at the near end.

    This REVERSES 2026-09-03's meaning, under which a layer was a ply, HALF
    a circuit, and an odd count stopped at the far end. The control is that
    meaning's own number: 1 layer must lay N passes, not N/2.

    RED CONTROLS INCLUDED, because "it emitted something" is not evidence:
      * `layer_step` must move the radii, so the default is not the only
        step the emitter can take;
      * layers=0 must still be refused, so the floor did not simply vanish.
    """
    d = sg.mandrel_design(DIAM, OMEGA, LENGTH)
    for n in (1, 2, 3, 4, 5):
        text = sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, FIBER, n, SPEED)
        paths = _parse(text)
        zs = sorted({round(float(np.asarray(q)[0, 2]), 9) for q in paths})
        moves = sum(len(np.asarray(q)) - 1 for q in paths)
        check("layers=%d emits %d layer(s) at %d radii" % (n, n, n),
              len(paths) == n and len(zs) == n
              and abs(zs[-1] - (n - 1) * FIBER) < 1e-12,
              "%d paths, radii %s" % (len(paths), zs))
        check("...each one a whole circuit -- %d passes" % d.n_pivots,
              moves == n * d.n_pivots
              and all(abs(float(q[-1, 0])) < 1e-9 for q in paths),
              "%d passes for %d layers" % (moves, n))

    # RED CONTROL 1: the step is the step asked for
    stepped = _parse(sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, FIBER, 3,
                                               SPEED, layer_step=0.05))
    zs = [round(float(q[0, 2]), 9) for q in stepped]
    check("CONTROL: layer_step=0.05 puts the layers 0.05 mm apart",
          zs == [0.0, 0.05, 0.1], "radii %s" % zs)

    # RED CONTROL 2: zero is still not a wall
    try:
        sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, FIBER, 0, SPEED)
        refused = False
    except ValueError:
        refused = True
    check("CONTROL: layers=0 is still refused", refused)


def test_the_tube_runs_off_the_end_it_finishes_on():
    """The rotating collector's answer to the plate's end-of-print exit move.

    A tube has no plate edge, so the axis the nozzle leaves along is the
    mandrel's own, and the rule is simply: leave by whichever end the print
    finished nearer.

    Since a layer became a whole circuit every tube finishes at
    the NEAR end, so the emitter only ever shows one side of the rule. The
    expectation is still read off the last commanded move rather than
    assumed, and the far-end side is driven through `_append_mandrel_exit`
    itself, on a program that stops at the far end.

    RED CONTROLS INCLUDED: a program that finishes at the FAR end must exit
    the other way (a rule that always answered the same would pass
    trivially), and with the option off the file must be byte-identical to
    one generated before the option existed.
    """
    lo = 2.0
    hi = lo + sg.mandrel_design(DIAM, OMEGA, LENGTH).length
    for n in (1, 2, 3, 4):
        text = sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, FIBER, n, SPEED,
                                         start_x=lo, exit_margin=5.0)
        lines = text.splitlines()
        finish = float([l for l in lines if l.startswith('G1 X')][-1].split()[1][1:])
        want = (lo - 5.0) if (finish - lo) < (hi - finish) else (hi + 5.0)
        got = float(lines[-1].split()[1][1:])
        check("layers=%d exits past the end it finished on" % n,
              lines[-1].startswith('G0 X') and abs(got - want) < 1e-6
              and abs(finish - lo) < 1e-9,
              "finished X%.4f -> %s, wanted X%.4f" % (finish, lines[-1], want))

    # CONTROL: a program stopped at the FAR end exits past the far end, so
    # the rule is shown picking BOTH ends rather than always the same one.
    far = ["G21", "G90", "G0 X%s A0 Z0" % sg.format_coord(lo),
           "G1 X%s A90" % sg.format_coord(hi)]
    sg._append_mandrel_exit(far, lo, hi, 5.0, sg.format_coord)
    check("CONTROL: the rule picks BOTH ends, not always one",
          abs(float(far[-1].split()[1][1:]) - (hi + 5.0)) < 1e-4,
          "far-end finish -> %s" % far[-1])

    m = sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, FIBER, 4, SPEED,
                                  start_x=lo, exit_margin=2.5)
    tail = m.splitlines()[-1]
    check("the margin is the distance past that end",
          abs(abs(float(tail.split()[1][1:]) - lo) - 2.5) < 1e-6 or abs(abs(float(tail.split()[1][1:]) - hi) - 2.5) < 1e-6, tail)

    off = sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, FIBER, 4, SPEED,
                                    start_x=lo)
    on = sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, FIBER, 4, SPEED,
                                   start_x=lo, exit_margin=5.0)
    check("CONTROL: with no margin the file is byte-identical to before",
          off == "\n".join(on.splitlines()[:-1]) + "\n"
          and not off.splitlines()[-1].startswith('G0 X'),
          "off ends %r" % off.splitlines()[-1])


def test_an_end_turn_tube_is_exactly_what_was_asked():
    """`--tube-pivots`: angle, length AND pore, all built.

    THE CASE that started it: 3 mm rod, 36 deg, 20 mm, 800 um. McColl's Eq
    3.1 closes there only with N = 2 -- one 7.8 mm diamond, 9 lines of g-code.
    COMPARATORS, none of them the code under test: the PARSER's (u, v) read
    of the emitted file (angle, length, turns, pivot spacing, closure), and
    `mew_tube.TubeDesign`'s Eq 5.3 brute-forced over every even N for the
    nearest pore. Swept over N = 4..40 so an N whose naive pass count shares
    a factor with N/2 (N = 28 here) is in the sweep: without the coprime rule
    its forward passes land on half the pivots (RED arms: noturn / nogcd /
    pore)."""
    r, w, L = 1.5, 36.0, 20.0
    circ = 2.0 * math.pi * r
    best, _alts = mew_tube.solve_turned_tube(r, w, L, 0.8 ** 2)

    def side(n):
        return math.sqrt(mew_tube.TubeDesign(r, w, n).pore_area)
    want_n = min(range(2, 362, 2), key=lambda n: abs(math.log(side(n) / 0.8)))
    check("the pore is the nearest any even pivot count gives",
          best.n_pivots == want_n and abs(
              best.pore_area - mew_tube.TubeDesign(r, w, want_n).pore_area)
          < 1e-12, "picked N=%d, brute force N=%d (%.0f um)"
          % (best.n_pivots, want_n, 1000.0 * side(want_n)))
    bad = []
    for n in range(4, 42, 2):
        d = mew_tube.TurnedTubeDesign(r, w, n, L)
        text = sg.generate_mandrel_gcode(2 * r, L, w, FIBER, 4, SPEED,
                                         design=d)
        paths = _parse(text, radius=r)
        runs, turns, starts = [], [], []
        for p in paths:
            for a, b in zip(p[:-1], p[1:]):
                du, dv = b[0] - a[0], b[1] - a[1]
                if abs(du) > 1e-6:
                    runs.append((abs(du), math.degrees(math.atan2(dv, abs(du)))))
                    if du > 0:      # in pivot steps; never wrapped by % circ
                        starts.append(a[1] / circ * (n // 2))
                else:
                    turns.append((a[0], dv))
        ang = max(abs(x[1] - w) for x in runs)
        lng = max(abs(x[0] - L) for x in runs)
        ends_ok = all(min(abs(u), abs(u - L)) < 1e-6 and dv >= -1e-9
                      for u, dv in turns)
        # the N/2 forward passes must start on N/2 evenly spaced angles: each
        # a whole number of steps of 1/(N/2) revolution, all N/2 of them hit
        s = {int(round(q)) % (n // 2) for q in starts}
        spaced = len(s) == n // 2 and all(
            abs(q - round(q)) < 1e-4 for q in starts)
        closed = abs((paths[1][-1][1] / circ)
                     - round(paths[1][-1][1] / circ)) < 1e-4
        if not (len(paths) == 4 and ang < 1e-3 and lng < 1e-3 and ends_ok
                and spaced and closed):
            bad.append((n, len(paths), round(ang, 4), round(lng, 4),
                        ends_ok, len(s), closed))
    check("every N builds the asked angle and length, turns only at the "
          "ends, and closes on evenly spaced pivots", not bad,
          "failing (N, paths, angle err, length err, ends, pivots, closed): "
          "%s" % (bad[:4],))
    # a length that closes on its own needs no turn, and then the file IS
    # McColl's: byte-identical to the length-closed design
    mc = mew_tube.TubeDesign(r, w, 2, 1)
    tt = mew_tube.TurnedTubeDesign(r, w, 2, mc.length)
    same = (sg.generate_mandrel_gcode(2 * r, mc.length, w, FIBER, 4, SPEED,
                                      design=mc)
            == sg.generate_mandrel_gcode(2 * r, mc.length, w, FIBER, 4,
                                         SPEED, design=tt))
    check("a length that closes needs no turn and emits McColl's file",
          same and tt.end_turn_deg < 1e-9,
          "turn %.2e deg, identical %s" % (tt.end_turn_deg, same))


def main():
    test_a_generated_tube_roundtrips_to_the_commanded_helix()
    test_the_roundtrip_harness_can_fail()
    test_a_pure_rotation_is_no_longer_dropped()
    test_a_layer_closes_on_itself()
    test_a_layer_is_one_whole_circuit()
    test_the_tube_runs_off_the_end_it_finishes_on()
    test_every_pass_holds_the_commanded_winding_angle()
    test_the_pivot_reverses_u_and_never_v()
    test_layers_register_on_each_other()
    test_the_world_geometry_is_concentric_shells_outside_the_mandrel()
    test_fig2a_prints_as_four_passes_ninety_degrees_apart()
    test_the_file_obeys_the_repo_gcode_rules()
    test_the_feed_is_the_resultant_and_is_emitted_once()
    test_omega_mode_scales_the_feed_with_the_wall()
    test_it_refuses_rather_than_guessing()
    test_a_pinned_design_skips_the_solve()
    test_an_end_turn_tube_is_exactly_what_was_asked()
    print("\n%d passed, %d failed, %d total"
          % (len(_PASS), len(_FAIL), len(_PASS) + len(_FAIL)))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
