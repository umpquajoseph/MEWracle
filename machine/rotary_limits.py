"""Does a finished five-axis g-code file ask the rotaries for anything the rig
cannot physically do? A reader of the FILE, not of the solver.

WHY IT EXISTS. The generator's own spin report reads the angles it chose; a
check of feasibility has to read what was written, with the clock the machine
will actually run, or it only repeats the solver's opinion of itself. The
software must never command a move that is not merely bad for the jet but
physically impossible for the joint. So this fails on ANY move whose
implied rotary rate, or rate change, exceeds the limits it is given -- the
same two numbers the Curved-collector page sends the generator
(`--surface-max-spin`, `--surface-max-accel`).

THE CLOCK. A move lasts `60 * norm / F` seconds, `norm` being X/Y/Z in mm and
every rotary word in degrees taken in quadrature -- the vector a controller
divides `F` by, which `test_five_axis_gcode` confirms against Terranova's own
files (`mew_kinematics.machine_norm`). `F` is modal. This is the convention
the five-axis collector checks use, so the two agree.

RATE of a move is its signed rotary change over its duration. ACCELERATION at
the vertex between two moves is the change of that rate over the mean of the
two durations -- a piecewise-constant-rate path, which is what a list of
linear moves commands. The very first move starts from wherever the machine
stood and is not charged an acceleration from rest; everything after it is.
A zero-length move carries no time, so its rate is 0 if nothing turns and
infinite if something does.

ROUNDING. Words are written to a few decimals, so the rate read back is off by
up to one last-digit unit per endpoint over the move's duration. That slack is
granted exactly, per move, and nothing more (`--decimals`, default 4, the
generator's `precision`).

No engine imports: usable on any file, from any branch.

    python machine/rotary_limits.py FILE --max-rate 360 --max-accel 720 [--words C,A]
"""

if __name__ == "__main__":  # run as a script: put the program's folders on sys.path
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "engine"))
    import app_paths  # noqa: E402,F401

import argparse
import math
import re
import sys


# --------------------------------------------------------------------------- #
# THE RIG'S LIMITS, AND THE ONE PLACE THEY ARE WRITTEN DOWN
# --------------------------------------------------------------------------- #
# The Machine-and-Collector tab's rotary group ships these as its field
# defaults, `presets/machine/5_Axis_PhD_Printer.json` carries whatever the user
# leaves in them, and `test_five_axis_matrix.py` generates every case with
# them -- so a green matrix is a statement about the numbers a user actually
# presses Generate with, and not about a set the suite chose for itself. The
# ENGINE's own defaults stay 0 (off) so a file written without the flags is
# byte-identical to one written before the limiter existed; these are the
# PAGE's defaults, which is a different thing.
#
# 720 deg/s^2 is the only measured number: the C (spin) axis, from the rig's
# printer.cfg (memory `rotary-accel-limit-720`). The A (tilt) axis has never
# been measured and 720 is assumed for it -- the assumption is stated in the
# field's tooltip rather than hidden here.
#
# EVERY RATE AND THE FEED CEILING SHIP OFF (0). Nobody is to measure or hunt
# for them (the spec's own instruction); they are fields so that the day a
# number is known it is typed in once, on the machine, and every design gets
# it. A limit at 0 is not applied, so shipping them off changes no file.
RIG_SPIN_ACCEL_DEG_S2 = 720.0
RIG_TILT_ACCEL_DEG_S2 = 720.0
RIG_SPIN_RATE_DEG_S = 0.0
RIG_TILT_RATE_DEG_S = 0.0
RIG_MAX_FEED_MM_MIN = 0.0

#: persist key -> (attribute name on `App`, shipped default), in the order the
#: fields are drawn. `gui_buildplate` builds the widgets from this and
#: `_machine_persist_keys` takes its keys, so a field cannot be added to one
#: and forgotten in the other.
RIG_LIMIT_FIELDS = (
    ("rotary.max_spin_rate", "rot_max_spin_rate", RIG_SPIN_RATE_DEG_S),
    ("rotary.max_spin_accel", "rot_max_spin_accel", RIG_SPIN_ACCEL_DEG_S2),
    ("rotary.max_tilt_rate", "rot_max_tilt_rate", RIG_TILT_RATE_DEG_S),
    ("rotary.max_tilt_accel", "rot_max_tilt_accel", RIG_TILT_ACCEL_DEG_S2),
    ("rotary.max_feed", "rot_max_feed", RIG_MAX_FEED_MM_MIN),
)

#: the generator flag each of those fields feeds, same order.
RIG_LIMIT_FLAGS = ("--surface-max-spin", "--surface-max-spin-accel",
                   "--surface-max-tilt-rate", "--surface-max-tilt-accel",
                   "--surface-max-feed")


def default_argv():
    """`scaffold_gcode` argv for the SHIPPED rig limits -- what a Generate
    sends when nothing on the Machine tab has been touched. A limit at 0 is
    left out entirely, so the flag list is empty when every limit is off."""
    out = []
    for flag, (_key, _attr, val) in zip(RIG_LIMIT_FLAGS, RIG_LIMIT_FIELDS):
        if float(val) > 0.0:
            out += [flag, repr(float(val))]
    return out

_WORD = re.compile(r"([A-Z])(-?\d+(?:\.\d*)?|-?\.\d+)")


def read_moves(path, words=("C", "A")):
    """`[(line_no, dt_s, {word: delta_deg})]` for every G0/G1 move."""
    st = {k: 0.0 for k in "XYZ"}
    st.update({w: 0.0 for w in words})
    feed = None
    out = []
    with open(path) as fh:
        for ln, line in enumerate(fh, 1):
            s = line.strip()
            if not s.startswith(("G0", "G1")) or s.startswith(("G02", "G03")):
                continue
            got = {k: float(v) for k, v in _WORD.findall(s[2:])}
            if "F" in got:
                feed = got["F"]
            new = dict(st)
            for k in list(st):
                if k in got:
                    new[k] = got[k]
            d = {k: new[k] - st[k] for k in st}
            norm = math.sqrt(sum(v * v for v in d.values()))
            dt = 60.0 * norm / feed if feed else float("inf")
            out.append((ln, dt, {w: d[w] for w in words}))
            st = new
    return out


def check(path, max_rate, max_accel, words=("C", "A"), decimals=4,
          per_word=None):
    """`{"ok", "violations": [...], "peak": {word: (rate, accel)}}`.

    A limit of 0 or None is not checked. Each violation is
    `(line, word, "rate"|"accel", value, limit)`.

    `per_word` is `{word: (rate, accel)}` overriding `max_rate`/`max_accel`
    for the words it names, because THE TWO JOINTS ARE NOT THE SAME MACHINE
    (`RIG_LIMIT_FIELDS`: the spin's four numbers and the tilt's are separate
    fields). It is not the same thing as calling this twice with one word
    each: `read_moves` takes the quadrature norm over X/Y/Z AND every word it
    is given, so dropping a word from `words` shortens every duration in the
    file and reads every rate back too fast. Both joints are always read; only
    the limit they are judged against differs.
    """
    unit = 10.0 ** (-int(decimals))
    moves = read_moves(path, words)
    lim = {w: (max_rate, max_accel) for w in words}
    lim.update({w: v for w, v in (per_word or {}).items() if w in lim})
    bad = []
    peak = {w: [0.0, 0.0] for w in words}
    prev = None
    for ln, dt, d in moves:
        rates = {}
        for w in words:
            if dt <= 0.0:
                rate = 0.0 if abs(d[w]) <= unit else float("inf")
                slack = 0.0
            else:
                rate = d[w] / dt
                slack = unit / dt
            rates[w] = (rate, slack)
            peak[w][0] = max(peak[w][0], abs(rate))
            w_rate = lim[w][0]
            if w_rate and abs(rate) > w_rate + slack:
                bad.append((ln, w, "rate", abs(rate), w_rate))
        if prev is not None:
            pdt, prates = prev
            mean = 0.5 * (pdt + dt)
            for w in words:
                if mean <= 0.0 or not math.isfinite(mean):
                    continue
                r1, s1 = rates[w]
                r0, s0 = prates[w]
                acc = abs(r1 - r0) / mean
                if not math.isfinite(acc):
                    continue
                peak[w][1] = max(peak[w][1], acc)
                w_accel = lim[w][1]
                if w_accel and acc > w_accel + (s0 + s1) / mean:
                    bad.append((ln, w, "accel", acc, w_accel))
        prev = (dt, rates)
    return {"ok": not bad, "violations": bad,
            "peak": {w: tuple(v) for w, v in peak.items()},
            "moves": len(moves)}


def report_lines(path, per_word, decimals=4):
    """Run-log lines saying whether the WRITTEN file holds its rotary limits.

    WHY THE FILE AND NOT THE SOLVER. `_limit_rotary_motion`'s own verdict
    (`stats["remaining"]`, `scaffold_gcode.py`) is computed on the command
    list -- one entry per design point -- and the writer afterwards splits a
    routed travel into pieces `_travel_waypoints` invents and
    `_plan_piece_feeds` plans separately, explicitly leaving that chain's two
    END vertices to the plan they came from. So a violation that lands in the
    lead-in is structurally invisible to `remaining`, and the warning it
    guards could never fire for it. Measured on a matrix of fifteen
    generated files: ten of the fifteen cases were written with
    moves over 720 deg/s^2 and every run reported nothing, because every one
    of those moves sat at or before the file's first `G1` (bowl peak 2188 on
    C at line 7, of a 2607-move file).

    Reading the file closes that: it sees the pieces, because they are lines.

    NOT A FAILURE. These files are the ones a lead-in demands -- the contact
    point crosses 0.2 mm while the collector turns 4 deg, so holding the CTS
    floor alone forces the rate -- and the floor outranks the acceleration
    limit by instruction (`_plan_feed`: "falling below the CTS is
    unnaceptable"). Generate still writes the file and still exits 0. What
    changes is that it says so.

    `per_word` is `{word: (rate, accel)}`, the limits as the joints were
    given them. Returns [] when no limit is set or nothing is over.
    """
    words = tuple(per_word)
    if not words or not any(v for lim in per_word.values() for v in lim):
        return []
    res = check(path, 0.0, 0.0, words, decimals, per_word=per_word)
    if res["ok"]:
        return ["  rotary limits hold on every one of the %d written move(s)"
                % res["moves"]]
    out = []
    for w in words:
        v = [x for x in res["violations"] if x[1] == w]
        if not v:
            continue
        worst = max(v, key=lambda x: x[3] / x[4])
        out.append("  WARNING: %s exceeds its limit on %d of %d written "
                   "move(s); worst %s %.0f against %g at line %d"
                   % (w, len(v), res["moves"], worst[2], worst[3], worst[4],
                      worst[0]))
    lines = sorted({x[0] for x in res["violations"]})
    out.append("    at g-code line(s) %s%s -- the limiter plans the design's "
               "moves, not the pieces a routed travel is split into, and no "
               "move may be slowed below the surface-speed floor"
               % (", ".join(str(n) for n in lines[:8]),
                  ", ..." if len(lines) > 8 else ""))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("gcode")
    ap.add_argument("--max-rate", type=float, default=0.0,
                    help="deg/s; 0 does not check")
    ap.add_argument("--max-accel", type=float, default=0.0,
                    help="deg/s^2; 0 does not check")
    ap.add_argument("--words", default="C,A")
    ap.add_argument("--decimals", type=int, default=4)
    args = ap.parse_args(argv)
    words = tuple(w.strip().upper() for w in args.words.split(",") if w)
    res = check(args.gcode, args.max_rate, args.max_accel, words,
                args.decimals)
    print("%s  %d moves, limits rate %g deg/s, accel %g deg/s^2"
          % ("PASS" if res["ok"] else "FAIL", res["moves"], args.max_rate,
             args.max_accel))
    for w, (r, a) in res["peak"].items():
        n_r = sum(1 for v in res["violations"] if v[1] == w and v[2] == "rate")
        n_a = sum(1 for v in res["violations"] if v[1] == w
                  and v[2] == "accel")
        print("  %s: peak rate %.1f deg/s (%d over), peak accel %.0f deg/s^2 "
              "(%d over)" % (w, r, n_r, a, n_a))
    for ln, w, kind, val, lim in sorted(res["violations"],
                                        key=lambda v: -v[3] / v[4])[:5]:
        print("  worst: line %d %s %s %.1f > %g" % (ln, w, kind, val, lim))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
