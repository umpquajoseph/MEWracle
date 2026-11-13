"""Split feeds on a mandrel -- Aerotech's F (linear) + E (rotary) words.

`scaffold_gcode`'s mandrel emitters take `rotary_feed="split"`: every move
carries F = S*|du|/L and E = S*|dA|/L, so it lasts L/S at the commanded surface
speed S. The readers (`parse_gcode`, the player's `parse_path`, the print-time
estimate, the G-code page's checks) learn to read that file.

COMPARATORS, none written by the code under test:

  1. **Aerotech's documented timing rule** (Automation1 reference, "G-Code
     Commands"): a move with any linear axis runs at F over the linear axes;
     only a rotation-only move runs at E. `_aerotech_seconds` below is that rule
     walked over the file TEXT, written here, independently of the emitter.
     The emitter is also checked under the other reading a controller could
     take (wait for the slower axis), because the derivation claims both agree.
  2. **The combined file's clock.** One F word, the resultant surface speed, is
     the convention every mandrel file has used and that `parse_gcode` and the
     player were validated on. The same tube written split must lay the same
     fibres at the same times.
  3. **Invariants**: no F0 or E0, no F on a move with no linear motion, no E on
     a move with no rotation.

THE HARNESS SWINGS BOTH WAYS: `test_the_timing_harness_can_fail` breaks one
emitted F and requires the per-move check to reject it. RED against the
pre-change readers: run with the HEAD copies of gcode_to_fibers.py,
gcode_player.py and mew_gcode.py, the parse, player, auto-detect and
G-code-page cases fail (the auto-detect read every E as extrusion and the split
file parsed as an empty print).
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

import gcode_player as gp
import gcode_to_fibers as gf
import mew_frame
import mew_gcode
import mew_unit_cell
import scaffold_gcode as sg

_PASS, _FAIL = [], []


def check(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       ("   " + detail) if detail else ""))


DIAM, OMEGA, LENGTH = 1.5, 20.0, 3.24
R0 = 0.5 * DIAM
FIBER, SPEED, PLIES = 0.02, 600.0, 4
CELL_D = 3.0
TOL = 2e-3      # relative; coordinates and rates are written at 4 decimals


def _zigzag(**kw):
    return sg.generate_mandrel_gcode(DIAM, LENGTH, OMEGA, FIBER, PLIES, SPEED,
                                     **kw)


def _cell(family, **kw):
    # test_mandrel_unit_cell's re-entrant fixture: 3 mm rod, 30 deg ribs
    extra = {"rib_angle_deg": 30.0} if family == "reentrant" else {}
    cell = mew_unit_cell.design_tube_cells(family, CELL_D, 6.0, 1.5, 2.4,
                                           **extra)
    return sg.generate_mandrel_cell_gcode(CELL_D, cell, FIBER, PLIES, SPEED,
                                          **kw)


def _moves(text):
    """[(code, x0, a0, z0, x1, a1, z1, F, E, words)] per G0/G1, F and E modal
    and per MINUTE, the time base read from G75/G76."""
    x = a = z = 0.0
    f = e = None
    per = 1.0
    out = []
    for ln in text.splitlines():
        t = ln.split()
        if not t:
            continue
        if t[0] in ("G75", "G76"):
            per = 60.0 if t[0] == "G76" else 1.0
            continue
        if t[0] not in ("G0", "G1"):
            continue
        w = {tok[0]: float(tok[1:]) for tok in t[1:]}
        if "F" in w:
            f = w["F"] * per
        if "E" in w:
            e = w["E"] * per
        nx, na, nz = w.get("X", x), w.get("A", a), w.get("Z", z)
        out.append((t[0], x, a, z, nx, na, nz, f, e, w))
        x, a, z = nx, na, nz
    return out


def _aerotech_seconds(m):
    _, x0, a0, z0, x1, a1, z1, f, e, _w = m
    lin = math.hypot(x1 - x0, z1 - z0)
    if lin > 0:
        return lin / (f / 60.0)
    if abs(a1 - a0) > 0:
        return abs(a1 - a0) / (e / 60.0)
    return 0.0


def _slower_axis_seconds(m):
    _, x0, a0, z0, x1, a1, z1, f, e, _w = m
    t = []
    if x1 != x0:
        t.append(abs(x1 - x0) / (f / 60.0))
    if a1 != a0 and e:          # a combined file has no E to wait on
        t.append(abs(a1 - a0) / (e / 60.0))
    return max(t) if t else 0.0


def _timing_errors(text, radius, surface_z=0.0, omega=False):
    """Worst relative error of every G1's duration against L/S, under both
    controller readings. L is the surface length at R0 + (Z - surface_z)."""
    worst = [0.0, 0.0]
    for m in _moves(text):
        if m[0] != "G1":
            continue
        _, x0, a0, z0, x1, a1, z1, f, e, _w = m
        r = radius + (z1 - surface_z)
        length = math.hypot(x1 - x0, r * math.radians(a1 - a0))
        s = SPEED * (radius + z1) / radius if omega else SPEED
        want = length / (s / 60.0)
        for k, got in enumerate((_aerotech_seconds(m), _slower_axis_seconds(m))):
            worst[k] = max(worst[k], abs(got - want) / want)
    return worst


def _tmp(text):
    fd, path = tempfile.mkstemp(suffix=".gcode")
    os.close(fd)
    io.open(path, "w", encoding="utf-8").write(text)
    return path


# --------------------------------------------------------------------------- #
# 1. the emitter: every move lasts L / S
# --------------------------------------------------------------------------- #
def test_every_move_lasts_its_surface_length_over_the_speed():
    cases = [
        ("zigzag", _zigzag(rotary_feed="split"), R0, 0.0, False),
        ("zigzag per second", _zigzag(rotary_feed="split", time_base="s"),
         R0, 0.0, False),
        ("zigzag omega", _zigzag(rotary_feed="split", speed_mode="omega"),
         R0, 0.0, True),
        # a machine zeroed on the rod's AXIS: Z carries the radius, and the
        # rates must still use the true deposition radius
        ("zigzag surface_z", _zigzag(rotary_feed="split", start_z=2.0,
                                     surface_z=2.0), R0, 2.0, False),
        ("re-entrant", _cell("reentrant", rotary_feed="split"), 0.5 * CELL_D, 0.0,
         False),
        ("brick", _cell("brick-closed", rotary_feed="split", time_base="s"),
         0.5 * CELL_D, 0.0, False),
    ]
    for name, text, radius, sz, omega in cases:
        aero, slower = _timing_errors(text, radius, sz, omega)
        check("%s: every G1 lasts L/S as Aerotech times it" % name,
              aero < TOL, "worst %.2e" % aero)
        check("%s: ...and as a slower-axis controller would" % name,
              slower < TOL, "worst %.2e" % slower)


def test_the_timing_harness_can_fail():
    text = _zigzag(rotary_feed="split")
    lines = text.splitlines()
    k = next(i for i, l in enumerate(lines)
             if l.startswith("G1") and " F" in l)
    toks = lines[k].split()
    toks = [("F%s" % (float(t[1:]) * 1.1) if t.startswith("F") else t)
            for t in toks]
    lines[k] = " ".join(toks)
    aero, _ = _timing_errors("\n".join(lines), R0)
    check("a 10% wrong F on one move is caught", aero > 0.05,
          "worst %.2e" % aero)
    aero, _ = _timing_errors(_zigzag(), R0)
    check("...and a COMBINED file read as Aerotech is wrong (the bug)",
          aero > 0.05, "worst %.2e" % aero)


def test_words_appear_only_where_their_axis_moves():
    for name, text in (("zigzag", _zigzag(rotary_feed="split")),
                       ("brick", _cell("brick-closed", rotary_feed="split")),
                       ("re-entrant", _cell("reentrant", rotary_feed="split"))):
        bad = []
        for m in _moves(text):
            code, x0, a0, z0, x1, a1, z1, f, e, w = m
            if code != "G1":
                continue
            if w.get("F", 1.0) <= 0 or w.get("E", 1.0) <= 0:
                bad.append("zero rate")
            if "F" in w and x1 == x0:
                bad.append("F on a ring move")
            if "E" in w and a1 == a0:
                bad.append("E on an axial move")
        check("%s: no F0/E0, no F without X travel, no E without rotation"
              % name, not bad, ", ".join(sorted(set(bad))))


def test_the_zigzag_changes_rates_once_per_ply_and_cells_per_strut():
    text = _zigzag(rotary_feed="split")
    g1 = [l for l in text.splitlines() if l.startswith("G1")]
    n_e = sum(" E" in l for l in g1)
    check("zigzag: one E per ply (the radius steps out each ply)",
          n_e == PLIES, "%d E words, %d plies" % (n_e, PLIES))
    text = _cell("reentrant", rotary_feed="split")
    g1 = [l for l in text.splitlines() if l.startswith("G1")]
    n_e = sum(" E" in l for l in g1)
    check("re-entrant: the rates change strut by strut",
          n_e > 4 * PLIES, "%d E words over %d moves" % (n_e, len(g1)))


def test_the_header_and_the_default():
    split = _zigzag(rotary_feed="split").splitlines()
    sec = _zigzag(rotary_feed="split", time_base="s").splitlines()
    comb = _zigzag()
    check("split per minute writes G75 after G21 G90", split[:3]
          == ["G21", "G90", "G75"], str(split[:3]))
    check("split per second writes G76", sec[2] == "G76", sec[2])
    check("combined writes neither, and no E word at all",
          "G75" not in comb and "G76" not in comb
          and not any(w.startswith("E") for l in comb.splitlines()
                      for w in l.split()))
    check("combined is the default, byte for byte",
          comb == _zigzag(rotary_feed="combined"))
    # per second is per minute / 60, word for word
    pm = [w for l in split for w in l.split() if w[0] in "FE"]
    ps = [w for l in sec for w in l.split() if w[0] in "FE"]
    ok = len(pm) == len(ps) and all(
        a[0] == b[0] and abs(float(a[1:]) / 60.0 - float(b[1:])) <= 1e-4
        for a, b in zip(pm, ps))
    check("per-second words are the per-minute ones / 60", ok)
    for bad in ({"rotary_feed": "aerotech"}, {"time_base": "hour"}):
        try:
            _zigzag(**bad)
        except ValueError:
            check("refuses %r" % (bad,), True)
        else:
            check("refuses %r" % (bad,), False)


# --------------------------------------------------------------------------- #
# 2. the readers agree with the combined clock
# --------------------------------------------------------------------------- #
def _fibre_dts(paths, ptimes):
    return [np.diff(t) for t in ptimes], paths


def test_the_converter_reads_a_split_file_as_the_combined_one():
    for name, comb, split, radius in (
            ("zigzag", _zigzag(), _zigzag(rotary_feed="split", time_base="s"),
             R0),
            ("re-entrant", _cell("reentrant"),
             _cell("reentrant", rotary_feed="split"), 0.5 * CELL_D)):
        pc, ps = _tmp(comb), _tmp(split)
        try:
            check("%s: the split file is not read as slicer extrusion" % name,
                  not gf.file_uses_extrusion(ps))
            frame = mew_frame.CylinderFrame(radius)
            a_paths, a_t = gf.parse_gcode(pc, mode="auto", with_times=True,
                                          frame=frame)
            b_paths, b_t = gf.parse_gcode(ps, mode="auto", with_times=True,
                                          frame=frame)
        finally:
            os.unlink(pc)
            os.unlink(ps)
        same_geom = (len(a_paths) == len(b_paths) and len(b_paths) > 0
                     and all(p.shape == q.shape and np.allclose(p, q, atol=1e-6)
                             for p, q in zip(a_paths, b_paths)))
        check("%s: the same fibres" % name, same_geom,
              "%d vs %d paths" % (len(a_paths), len(b_paths)))
        if not same_geom:
            continue
        worst = max(float(np.max(np.abs(np.diff(t1) - np.diff(t0))
                                 / np.maximum(np.diff(t0), 1e-12)))
                    for t0, t1 in zip(a_t, b_t) if len(t0) > 1)
        check("%s: laid at the same per-segment times" % name, worst < TOL,
              "worst %.2e" % worst)


def test_the_player_reads_the_surface_speed_back():
    for name, comb, split, radius in (
            ("zigzag", _zigzag(), _zigzag(rotary_feed="split"), R0),
            ("brick", _cell("brick-closed"),
             _cell("brick-closed", rotary_feed="split", time_base="s"), 0.5 * CELL_D)):
        frame = mew_frame.CylinderFrame(radius)
        pc, ps = _tmp(comb), _tmp(split)
        try:
            a = gp.parse_path(pc, frame=frame)
            b = gp.parse_path(ps, frame=frame)
        finally:
            os.unlink(pc)
            os.unlink(ps)
        dep = np.asarray(b[2], dtype=bool)
        feeds = np.asarray(b[3])[dep]
        worst = float(np.max(np.abs(feeds - SPEED))) / SPEED
        check("%s: every deposition segment plays at the surface speed"
              % name, worst < TOL, "worst %.2e" % worst)
        check("%s: the initial-solve feed is the surface speed" % name,
              abs(b[5] - SPEED) / SPEED < TOL, "%.3f" % b[5])
        da, db = np.diff(a[1])[dep], np.diff(b[1])[dep]
        same = a[0].shape == b[0].shape and np.allclose(a[0], b[0], atol=1e-6)
        worst = float(np.max(np.abs(db - da) / np.maximum(da, 1e-12))) \
            if same else float("inf")
        check("%s: the same deposition clock as the combined file" % name,
              same and worst < TOL, "worst %.2e" % worst)


def test_the_print_time_estimate_follows_aerotech():
    for name, text in (("zigzag", _zigzag(rotary_feed="split")),
                       ("brick per second",
                        _cell("brick-closed", rotary_feed="split",
                              time_base="s"))):
        want = sum(_aerotech_seconds(m) for m in _moves(text))
        got = sg.print_path_stats(text)[0]
        check("%s: estimate = the Aerotech rule over every move" % name,
              abs(got - want) <= 1e-6 * want, "%.4f vs %.4f s" % (got, want))


def test_the_extrusion_detect_still_sees_a_slicer_file():
    p = _tmp("G21\nG90\nG1 X1 Y0 E0.5 F600\nG1 X2 Y0 E1.0\n")
    try:
        check("a planar E file is still slicer extrusion",
              gf.file_uses_extrusion(p))
    finally:
        os.unlink(p)


def test_the_gcode_page_does_not_flag_the_rates():
    split = _zigzag(rotary_feed="split")
    codes = {p.code for p in mew_gcode.problems(split, cts_mm_s=1000.0)}
    check("split mandrel: no slicer-E finding, no F-vs-CTS finding",
          "e_words" not in codes and "below_cts" not in codes, str(codes))
    planar = "G21\nG90\nG1 X1 Y0 E0.5 F600\nG1 X2 Y0 E1.0\n"
    codes = {p.code for p in mew_gcode.problems(planar, cts_mm_s=1000.0)}
    check("...while a planar E file still gets both",
          "e_words" in codes and "below_cts" in codes, str(codes))


def main():
    test_every_move_lasts_its_surface_length_over_the_speed()
    test_the_timing_harness_can_fail()
    test_words_appear_only_where_their_axis_moves()
    test_the_zigzag_changes_rates_once_per_ply_and_cells_per_strut()
    test_the_header_and_the_default()
    test_the_converter_reads_a_split_file_as_the_combined_one()
    test_the_player_reads_the_surface_speed_back()
    test_the_print_time_estimate_follows_aerotech()
    test_the_extrusion_detect_still_sees_a_slicer_file()
    test_the_gcode_page_does_not_flag_the_rates()
    print("\n%d passed, %d failed" % (len(_PASS), len(_FAIL)))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
