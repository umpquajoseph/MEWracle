#!/usr/bin/env python3
"""test_layer_shift -- the G-code page's Layer shift (`mew_gcode.Document.
_layer_shift`): microscale layer shifting for melt electrowriting.

Self-running, no pytest: prints OK/FAIL per check, exits non-zero on any FAIL.

COMPARATORS. The definitions are the two papers', not this code's:

  * Liashenko, Hrynevich & Dalton, Adv. Mater. 2020, 32, 2001874 -- each
    layer's path offset layer-on-layer, the first layer the reference; the
    phase of a sinusoidal path shifted BACKWARD per layer to stand a
    forward-tilting wall up; the amplitude raised a percentage of its own
    per layer; branching by alternating the offset; domes by a radius that
    varies linearly or periodically with layer number.
  * Devlin et al., Adv. Mater. Technol. 2024, 9, 2400419 -- "a `phase'
    offset displaces each node in the print direction, while an
    `amplitude' offset shifts nodes in the direction perpendicular to the
    print", both growing as ``(n-1)`` times the step (`appGcodeP02.m`).

  and invariants that need no reference: a layer shift adds and removes no
  line; the first selected layer is byte-identical; every arc's two radii
  agree in the shifted file; a pass's two ends and its turnaround are
  byte-identical under phase and amplitude; the phase slide never folds a
  pass back. Positions are read with this file's own resolver (`_points`),
  not the engine's walker. Fixtures are the real generator's output
  (`scaffold_gcode.py --line-map`), wavy and straight, with G2/G3
  turnarounds.

RED PROOFS. `--inject NAME` puts one defect in and the run must go RED in
the check named; with no injection every check is green. Run them all with
`--red`, which fails if any injection stays green:
  ramp_from_1   the ramp counts from 1     -> first layer unchanged, X step
  no_taper      the phase slide untapered  -> a big slide never folds
  no_links      turnarounds taken as fibre -> inner nodes slide n steps
                (a G2/G3 turnaround then sits inside the "pass", and a pass
                holding an arc is left alone), amplitude grows, pre-link map
  no_ij         an arc's I/J left as it was -> arcs keep their circle
  rotary_ok     a rotary file let through  -> rotary file refused
Not proven RED by an injection: "neighbouring passes slide opposite ways"
(no hook flips the slide to the bed's frame without rewriting the slide).
"""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import math
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import mew_gcode as eng                                     # noqa: E402

FAILS = []
TOL = 1.5e-4            # mm: two 4-decimal roundings


def check(name, ok, detail=""):
    print(("OK   " if ok else "FAIL ") + name + (("  -- " + detail)
                                                 if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# --------------------------------------------------------------------------- #
# fixtures: the real generator
# --------------------------------------------------------------------------- #
WAVE = "0,0; 0.25,0.3; 0.5,0; 0.75,-0.3"


def generate(tmp, name, *extra):
    out = os.path.join(tmp, name + ".gcode")
    lm = os.path.join(tmp, name + ".map.json")
    argv = [sys.executable, os.path.join(HERE, "engine", "scaffold_gcode.py"), "-o", out,
            "--line-map", lm, "--width", "6", "--height", "6", "--stepover",
            "1", "--layers", "5", "--g2g3-arcs", "--no-exit-move"] + list(extra)
    r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(r.stdout + r.stderr)
    return open(out).read(), eng.LineMap.load(lm)


_W = re.compile(r"([A-Z])([-+]?\d*\.?\d+)")


def _points(text):
    """[(cmd, x0, y0, x1, y1, words)] per line (None for a non-move), X/Y
    modal, absolute only -- the test's own reading, not the engine's."""
    out, x, y = [], None, None
    for line in text.splitlines():
        code = line.split(";")[0].strip().upper()
        toks = code.split()
        if not toks or not re.fullmatch(r"G0*[0-3]", toks[0]):
            out.append(None)
            continue
        words = {m.group(1): float(m.group(2))
                 for m in map(_W.fullmatch, toks[1:]) if m}
        nx, ny = words.get("X", x), words.get("Y", y)
        out.append(("G" + (toks[0].lstrip("G").lstrip("0") or "0"),
                    x, y, nx, ny, words))
        x, y = nx, ny
    return out


def shifted(text, lm, scope=None, **params):
    doc = eng.Document(text, lm)
    c = doc.add(eng.Transform(op="layer_shift", params=dict(params),
                              scope=scope or eng.Scope("all"),
                              what="layer_shift"))
    return doc, c, doc.render("parse").text


def structure(lm, n):
    """Per line: (layer ordinal or -1, pass or -1, is_link) from the map's
    own marks -- which layer, which pass, and whether a turnaround."""
    addrs = lm.addresses()
    order, out = [], []
    link_at = {}
    for m in lm.marks:
        if m["kind"] == "link":
            a = addrs[int(m["line"])]
            link_at[(a.group, a.layer, a.pass_)] = a.k
    for i in range(n):
        a = addrs[i]
        if a.section != "body" or a.layer < 0:
            out.append((-1, -1, False))
            continue
        u = (a.group, a.layer)
        if u not in order:
            order.append(u)
        at = link_at.get((a.group, a.layer, a.pass_))
        out.append((order.index(u), a.pass_,
                    a.pass_ >= 0 and at is not None and a.k >= at))
    return out


def passes(lm, n):
    """{(layer ordinal, pass): [line, ...]} -- each pass's fibre lines,
    without its turnaround."""
    out = {}
    for i, (L, ps, link) in enumerate(structure(lm, n)):
        if L >= 0 and ps >= 0 and not link:
            out.setdefault((L, ps), []).append(i)
    return out


# --------------------------------------------------------------------------- #
# the checks
# --------------------------------------------------------------------------- #
def t_generator(tmp, wavy, wmap):
    """The link mark is out of band: the g-code is byte-identical with and
    without a map, and every layer of n passes has n - 1 turnarounds."""
    out = os.path.join(tmp, "nomap.gcode")
    subprocess.run([sys.executable, os.path.join(HERE, "engine", "scaffold_gcode.py"),
                    "-o", out, "--width", "6", "--height", "6", "--stepover",
                    "1", "--layers", "5", "--g2g3-arcs", "--no-exit-move",
                    "--linear-pattern", WAVE, "--linear-wavelength", "1"],
                   capture_output=True, timeout=120, check=True)
    check("generator: the g-code is byte-identical with the map recorded",
          open(out).read() == wavy)
    by_layer = {}
    for m in wmap.marks:
        if m["kind"] in ("pass", "link"):
            a = wmap.addresses()[int(m["line"])]
            by_layer.setdefault(a.layer, [0, 0])[m["kind"] == "link"] += 1
    check("generator: n passes, n - 1 turnarounds, every layer",
          by_layer and all(lk == ps - 1 for ps, lk in by_layer.values()),
          str(by_layer))


def t_profiles():
    f = eng.layer_shift_factor
    seq = lambda prof, **kw: [round(f(prof, n, **kw), 9) for n in range(6)]
    check("profile: ramp 0, 1, 2 ...", seq("ramp") == [0, 1, 2, 3, 4, 5])
    check("profile: constant", seq("constant") == [1] * 6)
    check("profile: alternate 0, +1, -1, +2, -2 (branching)",
          seq("alternate") == [0, 1, -1, 2, -2, 3])
    check("profile: wave of period 4", seq("wave", period=4)
          == [0, 1, 0, -1, 0, 1])
    check("profile: cycle repeats like an angle cycle",
          seq("cycle", cycle=eng.parse_shift_cycle("0, 1; 3"))
          == [0, 1, 3, 0, 1, 3])
    try:
        eng.parse_shift_cycle("0, one")
        ok = False
    except ValueError:
        ok = True
    check("profile: a cycle word that is not a number is refused", ok)


def t_rigid(wavy, wmap):
    """X / Y step: layer n moves exactly n steps; layer 0 byte-identical."""
    _d, _c, out = shifted(wavy, wmap, dx=15.0, dy=-7.0)
    a, b = wavy.splitlines(), out.splitlines()
    check("no line added or removed", len(a) == len(b))
    st = structure(wmap, len(a))
    first = [i for i, s in enumerate(st) if s[0] == 0]
    check("first selected layer unchanged",
          all(a[i] == b[i] for i in first))
    pa, pb = _points(wavy), _points(out)
    worst = 0.0
    for i, (L, _p, _l) in enumerate(st):
        if L < 0 or pa[i] is None or pa[i][3] is None:
            continue
        ex, ey = L * 0.015, L * -0.007
        worst = max(worst, abs(pb[i][3] - pa[i][3] - ex),
                    abs(pb[i][4] - pa[i][4] - ey))
    check("X step: every move of layer n moves n steps", worst < TOL,
          "worst %.6f mm" % worst)
    outside = [i for i, s in enumerate(st) if s[0] < 0]
    check("header untouched", all(a[i] == b[i] for i in outside))


def t_along(wavy, wmap):
    """Phase: each pass slides along its OWN travel direction; ends and
    turnarounds byte-identical; never folds."""
    D = -18.0                                       # µm, Liashenko's sign
    _d, _c, out = shifted(wavy, wmap, along=D)
    a, b = wavy.splitlines(), out.splitlines()
    pa, pb = _points(wavy), _points(out)
    st = structure(wmap, len(a))
    links = [i for i, s in enumerate(st) if s[2]]
    ends_ok, inner_err, perp_err, folds, dirs = True, 0.0, 0.0, 0, {}
    for (L, ps), idx in passes(wmap, len(a)).items():
        sx, sy = pa[idx[0]][1], pa[idx[0]][2]
        ex, ey = pa[idx[-1]][3], pa[idx[-1]][4]
        ln = math.hypot(ex - sx, ey - sy)
        tx, ty = (ex - sx) / ln, (ey - sy) / ln
        want = L * D * 1e-3
        if b[idx[-1]] != a[idx[-1]]:
            ends_ok = False
        prev_u = 0.0                            # the pass's own start
        for i in idx:
            u = (pa[i][3] - sx) * tx + (pa[i][4] - sy) * ty
            du = (pb[i][3] - pa[i][3]) * tx + (pb[i][4] - pa[i][4]) * ty
            dv = -(pb[i][3] - pa[i][3]) * ty + (pb[i][4] - pa[i][4]) * tx
            perp_err = max(perp_err, abs(dv))
            if want and min(u, ln - u) >= 2 * abs(want) + 1e-6:
                inner_err = max(inner_err, abs(du - want))
            u2 = (pb[i][3] - sx) * tx + (pb[i][4] - sy) * ty
            if u2 < prev_u - 1e-9:
                folds += 1
            prev_u = u2
        if L == 2:              # the middle node's slide, on the bed
            m = idx[len(idx) // 2]
            dirs[ps] = (pb[m][3] - pa[m][3], pb[m][4] - pa[m][4])
    check("along: pass ends and turnarounds byte-identical",
          ends_ok and all(a[i] == b[i] for i in links))
    check("along: inner nodes slide exactly n steps along the print",
          inner_err < TOL, "worst %.6f mm" % inner_err)
    check("along: nothing moves across the print", perp_err < TOL,
          "worst %.6f mm" % perp_err)
    check("along: no pass folds back on itself", folds == 0, str(folds))
    # a serpentine's neighbours run opposite ways, so they slide opposite
    # world directions: the sign is the print direction's, not the bed's
    opp = [(dirs[p][0] * dirs[p + 1][0] + dirs[p][1] * dirs[p + 1][1]
            < -0.9 * (2 * D * 1e-3) ** 2)
           for p in sorted(dirs) if p + 1 in dirs]
    check("along: neighbouring passes slide opposite ways on the bed",
          opp and all(opp))
    # a slide far past the wave's node spacing (0.25 mm here): layer 4
    # moves 0.6 mm, where an untapered slide pulls a pass's first node
    # behind its own start
    _d, _c, out = shifted(wavy, wmap, along=-150.0)
    pb = _points(out)
    folds = 0
    for (L, ps), idx in passes(wmap, len(a)).items():
        sx, sy = pa[idx[0]][1], pa[idx[0]][2]
        ex, ey = pa[idx[-1]][3], pa[idx[-1]][4]
        ln = math.hypot(ex - sx, ey - sy)
        tx, ty = (ex - sx) / ln, (ey - sy) / ln
        prev_u = 0.0                            # the pass's own start
        for i in idx:
            u2 = (pb[i][3] - sx) * tx + (pb[i][4] - sy) * ty
            folds += u2 < prev_u - 1e-9
            prev_u = u2
    check("along: a big slide (0.6 mm) still never folds a pass back",
          folds == 0, "%d nodes behind the one before" % folds)


def t_amplitude(wavy, wmap):
    """Amplitude: each pass's peak offset from its chord grows by n steps
    (µm), or by n percent of its own (%); its ends stay on the grid."""
    for kw, label in (({"amp": 25.0}, "µm"), ({"amp_pct": 2.0}, "%")):
        _d, _c, out = shifted(wavy, wmap, **kw)
        a, b = wavy.splitlines(), out.splitlines()
        pa, pb = _points(wavy), _points(out)
        err, along_err, ends = 0.0, 0.0, True
        for (L, _ps), idx in passes(wmap, len(a)).items():
            sx, sy = pa[idx[0]][1], pa[idx[0]][2]
            ex, ey = pa[idx[-1]][3], pa[idx[-1]][4]
            ln = math.hypot(ex - sx, ey - sy)
            tx, ty = (ex - sx) / ln, (ey - sy) / ln
            off = lambda P, i: -(P[i][3] - sx) * ty + (P[i][4] - sy) * tx
            w0 = max(abs(off(pa, i)) for i in idx)
            w1 = max(abs(off(pb, i)) for i in idx)
            want = (w0 + L * kw["amp"] * 1e-3 if "amp" in kw
                    else w0 * (1 + L * kw["amp_pct"] / 100.0))
            err = max(err, abs(w1 - want))
            for i in idx:
                along_err = max(along_err, abs(
                    (pb[i][3] - pa[i][3]) * tx + (pb[i][4] - pa[i][4]) * ty))
            ends = ends and b[idx[-1]] == a[idx[-1]]
        check("amplitude (%s): the peak grows by n steps" % label,
              err < TOL, "worst %.6f mm" % err)
        check("amplitude (%s): nothing slides along the print" % label,
              along_err < TOL, "worst %.6f mm" % along_err)
        check("amplitude (%s): pass ends stay on the grid" % label, ends)


def t_arcs(wavy, wmap, straight, smap):
    """Every G2/G3 keeps its circle under every step at once."""
    for name, text, lm in (("wavy", wavy, wmap), ("straight", straight, smap)):
        _d, _c, out = shifted(text, lm, dx=12.0, dy=5.0, along=-15.0,
                              amp=20.0, radius=40.0, profile="alternate")
        worst, n = 0.0, 0
        for P in _points(out):
            if P is None or P[0] not in ("G2", "G3") or "I" not in P[5]:
                continue
            cx, cy = P[1] + P[5]["I"], P[2] + P[5].get("J", 0.0)
            worst = max(worst, abs(math.hypot(P[1] - cx, P[2] - cy)
                                   - math.hypot(P[3] - cx, P[4] - cy)))
            n += 1
        check("arcs keep their circle (%s, %d arcs)" % (name, n),
              n > 0 and worst < 2 * TOL, "worst %.6f mm" % worst)


def t_radius(straight, smap):
    """Radius: each layer's farthest point from its centre moves n steps."""
    _d, _c, out = shifted(straight, smap, radius=50.0)
    a = straight.splitlines()
    st = structure(smap, len(a))
    pa, pb = _points(straight), _points(out)
    err = 0.0
    for L in sorted({s[0] for s in st if s[0] >= 0}):
        idx = [i for i, s in enumerate(st) if s[0] == L and pa[i] is not None
               and pa[i][3] is not None]
        xs = [pa[i][3] for i in idx]
        ys = [pa[i][4] for i in idx]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        r0 = max(math.hypot(pa[i][3] - cx, pa[i][4] - cy) for i in idx)
        r1 = max(math.hypot(pb[i][3] - cx, pb[i][4] - cy) for i in idx)
        err = max(err, abs(r1 - r0 - L * 0.05))
    check("radius: the farthest point moves out n steps", err < TOL,
          "worst %.6f mm" % err)
    d, c, out = shifted(straight, smap, amp=20.0, along=-15.0)
    said = eng.layer_shift_summary(d, c)
    check("straight passes: along and amplitude move nothing, and say so",
          out == straight and "straight line" in said, said)


def t_scope(wavy, wmap):
    """A layer range counts from its own first layer; the rest is kept."""
    _d, c, out = shifted(wavy, wmap, eng.Scope("layers", 3, 5), dx=10.0)
    a, b = wavy.splitlines(), out.splitlines()
    st = structure(wmap, len(a))
    pa, pb = _points(wavy), _points(out)
    keep = all(a[i] == b[i] for i, s in enumerate(st) if s[0] in (0, 1, 2))
    err = max(abs(pb[i][3] - pa[i][3] - (s[0] - 2) * 0.010)
              for i, s in enumerate(st) if s[0] >= 2 and pa[i] is not None
              and pa[i][3] is not None)
    check("scope: layers 3-5 counted from layer 3; 1-3 unchanged",
          keep and err < TOL, "worst %.6f mm" % err)
    lab = eng.chip_label(_d, c)
    check("chip label names the step and the layers",
          "X +10" in lab and "layers 3-5" in lab, lab)


def t_files(wavy, wmap):
    """What is refused or reduced: a rotary file; an opened file or a
    pre-link map (no passes: X / Y and radius only)."""
    rot = "G90\nG1 Z0.1\nG1 X0 Y0 A0\nG1 X1 Y0 A10\nG1 Z0.2\nG1 X0 Y1 A20\n"
    d, c, out = shifted(rot, None, dx=50.0)
    check("rotary file refused, untouched", out.strip() == rot.strip()
          and d.layer_shift_stats(c.id)["refused"] == "rotary")
    check("rotary refusal says why", "rotary" in eng.layer_shift_summary(d, c))
    for name, doc_map in (("opened file", None),
                          ("pre-link map", eng.LineMap(
                              [m for m in wmap.marks if m["kind"] != "link"],
                              wmap.n_lines, wmap.groups, sha1=wmap.sha1))):
        d, c, out = shifted(wavy, doc_map, along=-18.0)
        check("%s: along moves nothing" % name, out == wavy)
        check("%s: says the passes are missing" % name,
              "no passes" in eng.layer_shift_summary(d, c),
              eng.layer_shift_summary(d, c))
        _d, _c, out = shifted(wavy, doc_map, dx=10.0)
        check("%s: X step still moves it" % name, out != wavy)


def t_roundtrip(wavy, wmap):
    d, c, out = shifted(wavy, wmap, dy=8.0, along=-12.0, profile="cycle",
                        cycle="0, 1, 3")
    d2 = eng.Document.from_dict(d.to_dict(with_base=True))
    check("saved and re-opened, it renders the same",
          d2.render("parse").text == out)


INJECT = {}


def _inject(name):
    if name == "ramp_from_1":
        f = eng.layer_shift_factor
        eng.layer_shift_factor = lambda p, n, *a, **k: (
            n + 1.0 if p in (None, "ramp") else f(p, n, *a, **k))
    elif name == "no_taper":
        eng._PHASE_RAMP = 1e-12
    elif name == "no_links":
        eng.Document._link_starts = lambda self: ({}, True)
    elif name == "no_ij":
        put = eng._put_words
        eng._put_words = lambda line, values, *a: put(
            line, {k: v for k, v in values.items() if k not in "IJ"}, *a)
    elif name == "rotary_ok":
        eng.has_rotary_words = lambda text: False
    else:
        raise SystemExit("unknown injection %r" % name)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        wavy, wmap = generate(tmp, "wavy", "--linear-pattern", WAVE,
                              "--linear-wavelength", "1")
        straight, smap = generate(tmp, "straight")
        t_generator(tmp, wavy, wmap)
        t_profiles()
        t_rigid(wavy, wmap)
        t_along(wavy, wmap)
        t_amplitude(wavy, wmap)
        t_arcs(wavy, wmap, straight, smap)
        t_radius(straight, smap)
        t_scope(wavy, wmap)
        t_files(wavy, wmap)
        t_roundtrip(wavy, wmap)
    print()
    print("FAILED: %d" % len(FAILS) if FAILS else "all checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    if "--red" in sys.argv:
        bad = []
        for name in ("ramp_from_1", "no_taper", "no_links", "no_ij",
                     "rotary_ok"):
            r = subprocess.run([sys.executable, __file__, "--inject", name],
                               capture_output=True, text=True, timeout=600)
            red = [ln[5:].split("  --")[0] for ln in r.stdout.splitlines()
                   if ln.startswith("FAIL ")]
            print(("RED   " if red else "GREEN ") + name
                  + ("  -> " + "; ".join(red) if red else ""))
            if not red:
                bad.append(name)
        sys.exit(1 if bad else 0)
    if "--inject" in sys.argv:
        _inject(sys.argv[sys.argv.index("--inject") + 1])
    sys.exit(main())
