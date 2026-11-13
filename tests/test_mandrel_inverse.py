"""`mew_tube.solve_tube_for_pore` -- the inverse direction, against an independent
enumeration of McColl's own equations.

THE COMPARATOR IS NOT THE SOLVER. Every ranking claim below is checked against
`_enumerate` / `_ladder` in this file, which walk the same search space over
whole `TubeDesign` OBJECTS (Eqs 2.1, 3.1, 4.1-4.3, 5.1-5.3 as the module
publishes them) instead of the solver's inline arithmetic. So a candidate the
solver scored with a shortcut that has drifted from `TubeDesign` scores
differently here and the ladder case goes red. The GREEN case rebuilds the
returned design from its own four fields and demands `==` on length and pore.

BOTH WAYS. Run before `solve_tube_for_pore` existed every case failed
(`module 'mew_tube' has no attribute 'solve_tube_for_pore'`), and with the
objective deliberately broken -- the odd-eta doubling removed from the pore key,
and separately the length tie-break inverted -- the banding and ladder cases went
red while the refusals stayed green.

THE CASES

  1. GREEN reconstruction -- r 0.75, L 12, 200 um pore, 20 um fibre.
  2. REFUSAL, fibre floor -- a 5 um pore under a 20 um fibre. Eq 5.1's LA and
     2 x Eq 5.2's HT are the pore's two diagonals; below one fibre width the
     walls meet. Asserted on the MESSAGE, not the type.
  3. REFUSAL, length -- `length_tol=1e-9`; the message must name the nearest
     closing length, and this file computes that length itself.
  4. REFUSAL, pore -- a 3 mm pore on a 0.2 mm-radius mandrel: no ladder reaches
     it, and the message must carry the nearest pore and the three ways out.
  5. THE BANDING CASE, which is why the objective quantises. Ranking on the raw
     pore error picks omega 66.5 / eta 22 -> 12.480 mm (+4.0 % length) / 399 um
     over omega 58.5 / eta 13 -> 11.995 mm (-0.0 %) / 401 um: 4 % of the length
     spent for 0.5 % of pore. Banding at 1 % on the SIDE makes the two equal on
     pore and the length tie-break picks eta 13.
  6. THE LADDER -- capped, deduplicated, all inside tolerance, best not repeated,
     and identical to the independently built bucket ladder.
  7. eta PAST THE CAP -- `DEFAULT_MAX_ETA` is 36 and a fine pore needs more.
     The target is what eta = 39 gives at omega 40 on r 1.5; capping the search
     at 36 cannot reach it.

Self-running, no pytest: prints PASS/FAIL per case, exits non-zero on any fail.
"""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import math
import sys
import time
import traceback

import mew_tube as mt

_PASS, _FAIL = [], []


def check(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       ("   " + detail) if detail else ""))


# --------------------------------------------------------------------------- #
# The comparator: the same search space, walked over TubeDesign objects.
# Written from the spec, not from the solver.
# --------------------------------------------------------------------------- #
def _omegas(omega_range, omega_step):
    lo, hi = omega_range
    n = int(math.floor((hi - lo) / omega_step + 1e-9))
    return [lo + i * omega_step for i in range(n + 1)]


def _candidates(radius, target_length, omega_range=(15.0, 75.0),
                omega_step=0.5, max_eta=120, max_rm=64):
    """Every (omega, eta, Rm) the solver may look at -- ignoring the tolerance."""
    out = []
    for w in _omegas(omega_range, omega_step):
        pitch = mt.pitch_length(radius, w)
        for eta in range(1, max_eta + 1):
            rm_star = target_length / pitch - 1.0 / eta
            for rm in sorted({0, int(math.floor(rm_star)), int(math.ceil(rm_star))}):
                if rm < 0 or rm > max_rm:
                    continue
                out.append(mt.TubeDesign(radius, w, eta, rm))
    return out


def _enumerate(radius, target_length, length_tol=0.05, fiber_diameter=None,
               **kw):
    """The feasible set: inside the length tolerance and above the fibre floor."""
    out = []
    for d in _candidates(radius, target_length, **kw):
        if abs(d.length - target_length) > length_tol * target_length:
            continue
        if fiber_diameter is not None:
            la = mt.pore_arc_length(radius, d.pivot_angle)
            ht = mt.pore_triangle_height(la, d.omega_deg)
            if la < fiber_diameter or 2.0 * ht < fiber_diameter:
                continue
        out.append(d)
    return out


def _key(d, target_length, target_pore_area, pore_band=0.01):
    side = abs(math.log(math.sqrt(d.pore_area / target_pore_area)))
    return (int(side / pore_band), abs(d.length - target_length),
            d.n_pivots, abs(d.omega_deg - 45.0))


def _ident(d):
    return (d.eta, d.rm, round(d.omega_deg, 3))


def _ladder(cands, target_length, target_pore_area, length_tol,
            n_alternatives=6):
    ordered = sorted(cands, key=lambda d: _key(d, target_length, target_pore_area))
    best = ordered[0]
    seen = {_ident(best)}
    out = []
    for bucket in (0.0, 0.01, 0.03, length_tol):
        for d in ordered:
            if len(out) >= n_alternatives:
                break
            if abs(d.length - target_length) <= bucket * target_length + 1e-12 \
                    and _ident(d) not in seen:
                seen.add(_ident(d))
                out.append(d)
                break
    for d in ordered:
        if len(out) >= n_alternatives:
            break
        if _ident(d) not in seen:
            seen.add(_ident(d))
            out.append(d)
    return best, out


# --------------------------------------------------------------------------- #
# 1 -- GREEN
# --------------------------------------------------------------------------- #
def test_green_reconstruction():
    """A 200 um pore on a 0.75 mm mandrel, 12 mm long, 20 um fibre."""
    d, alts = mt.solve_tube_for_pore(0.75, 12.0, 0.200 ** 2, fiber_diameter=0.020)
    check("green: length lands within 0.6 mm of the 12 mm asked for",
          abs(d.length - 12.0) <= 0.6,
          "%.4f mm (omega %.1f, eta %d, Rm %d)"
          % (d.length, d.omega_deg, d.eta, d.rm))
    rel = abs(d.pore_area - 0.200 ** 2) / (0.200 ** 2)
    check("green: pore area within 25 % of the target",
          rel <= 0.25, "%.6f mm^2, side %.1f um, %.2f %% off"
          % (d.pore_area, 1000.0 * math.sqrt(d.pore_area), 100.0 * rel))
    # the identity guard: a real TubeDesign, not a struct that has drifted
    again = mt.TubeDesign(d.radius, d.omega_deg, d.eta, d.rm)
    check("green: TubeDesign(r, omega, eta, Rm) reproduces length EXACTLY",
          again.length == d.length, "%r vs %r" % (again.length, d.length))
    check("green: ... and the pore area EXACTLY",
          again.pore_area == d.pore_area,
          "%r vs %r" % (again.pore_area, d.pore_area))
    check("green: it is a TubeDesign", isinstance(d, mt.TubeDesign))
    # and it is the same design the independent enumeration ranks first
    cands = _enumerate(0.75, 12.0, fiber_diameter=0.020)
    ref, _ = _ladder(cands, 12.0, 0.200 ** 2, 0.05)
    check("green: matches the independently enumerated winner",
          _ident(ref) == _ident(d),
          "comparator %r vs solver %r" % (_ident(ref), _ident(d)))


# --------------------------------------------------------------------------- #
# 2 -- REFUSAL: the fibre floor
# --------------------------------------------------------------------------- #
def test_refusal_names_the_fibre_floor():
    """A 5 um pore cannot exist inside a 20 um fibre. The message says so, with
    the smallest pore this mandrel and fibre DO admit."""
    try:
        mt.solve_tube_for_pore(0.75, 12.0, 0.005 ** 2, fiber_diameter=0.020)
    except mt.TubeDesignError as exc:
        msg = str(exc)
    else:
        check("floor: a sub-fibre pore is refused", False, "no exception raised")
        return
    check("floor: a sub-fibre pore is refused", True, msg)
    check("floor: the message names the fibre diameter", "0.020" in msg)
    smallest = min(d.pore_area for d in
                   _enumerate(0.75, 12.0, fiber_diameter=0.020))
    side_um = "%.1f" % (1000.0 * math.sqrt(smallest))
    check("floor: the message names the smallest admissible pore, %s um" % side_um,
          side_um in msg and ("%.6f" % smallest) in msg,
          "expected %s um / %.6f mm^2" % (side_um, smallest))


# --------------------------------------------------------------------------- #
# 3 -- REFUSAL: no closing length in tolerance
# --------------------------------------------------------------------------- #
def test_refusal_names_the_nearest_closing_length():
    try:
        mt.solve_tube_for_pore(0.75, 12.0, 0.200 ** 2, length_tol=1e-9)
    except mt.TubeDesignError as exc:
        msg = str(exc)
    else:
        check("length: an impossible tolerance is refused", False,
              "no exception raised")
        return
    check("length: an impossible tolerance is refused", True, msg)
    nearest = min(_candidates(0.75, 12.0), key=lambda d: abs(d.length - 12.0))
    check("length: the message names the nearest closing length, %.4f mm"
          % nearest.length, ("%.4f" % nearest.length) in msg,
          "expected %.4f (omega %.1f, eta %d, Rm %d)"
          % (nearest.length, nearest.omega_deg, nearest.eta, nearest.rm))


# --------------------------------------------------------------------------- #
# 4 -- REFUSAL: no ladder reaches the pore
# --------------------------------------------------------------------------- #
def test_refusal_names_the_nearest_pore():
    """A 3 mm pore on a 0.4 mm-diameter mandrel: the whole circumference is
    1.26 mm, so Eq 5.1 cannot deliver it at any eta."""
    try:
        mt.solve_tube_for_pore(0.2, 12.0, 3.0 ** 2)
    except mt.TubeDesignError as exc:
        msg = str(exc)
    else:
        check("pore: an unreachable pore is refused", False, "no exception raised")
        return
    check("pore: an unreachable pore is refused", True, msg)
    cands = _enumerate(0.2, 12.0)
    nearest = min(cands, key=lambda d: abs(math.log(d.pore_area / 9.0)))
    check("pore: the message names the nearest pore, %.1f um"
          % (1000.0 * math.sqrt(nearest.pore_area)),
          ("%.1f" % (1000.0 * math.sqrt(nearest.pore_area))) in msg,
          "nearest %.6f mm^2 (omega %.1f, eta %d)"
          % (nearest.pore_area, nearest.omega_deg, nearest.eta))
    check("pore: the message offers all three ways out",
          "length tolerance" in msg and "eta" in msg and "mandrel" in msg)


# --------------------------------------------------------------------------- #
# 5 -- THE BANDING CASE
# --------------------------------------------------------------------------- #
def test_banding_prefers_the_length_over_half_a_percent_of_pore():
    """r 1.5, 12 mm, 400 um pore. Un-banded this picks omega 66.5 / eta 22 --
    12.480 mm, +4.0 % of length, for 399 um. Banded it picks eta 13 at
    11.995 mm."""
    d, _ = mt.solve_tube_for_pore(1.5, 12.0, 0.400 ** 2)
    check("banding: eta is 13, not 22", d.eta == 13,
          "eta %d, omega %.1f, Rm %d, L %.4f mm, pore side %.1f um"
          % (d.eta, d.omega_deg, d.rm, d.length,
             1000.0 * math.sqrt(d.pore_area)))
    check("banding: the length is inside 1 %", abs(d.length - 12.0) < 0.01 * 12.0,
          "%.4f mm (%+.2f %%)" % (d.length, 100.0 * (d.length - 12.0) / 12.0))
    side = 1000.0 * math.sqrt(d.pore_area)
    check("banding: the pore side is inside 1 % of 400 um",
          abs(side - 400.0) <= 4.0, "%.1f um" % side)


# --------------------------------------------------------------------------- #
# 6 -- THE LADDER
# --------------------------------------------------------------------------- #
def test_the_alternatives_ladder_spans_the_trade():
    d, alts = mt.solve_tube_for_pore(0.75, 12.0, 0.200 ** 2, fiber_diameter=0.020)
    check("ladder: at most n_alternatives rows", len(alts) <= 6,
          "%d rows" % len(alts))
    idents = [_ident(a) for a in alts]
    check("ladder: deduplicated on (eta, Rm, omega)",
          len(set(idents)) == len(idents), "%r" % (idents,))
    check("ladder: the best design is not repeated in it",
          _ident(d) not in idents)
    check("ladder: every row is inside the length tolerance",
          all(abs(a.length - 12.0) <= 0.05 * 12.0 for a in alts),
          "%r" % ["%.4f" % a.length for a in alts])
    cands = _enumerate(0.75, 12.0, fiber_diameter=0.020)
    _, ref = _ladder(cands, 12.0, 0.200 ** 2, 0.05)
    check("ladder: identical to the independently built bucket ladder",
          [_ident(a) for a in ref] == idents,
          "comparator %r" % ([_ident(a) for a in ref],))
    errs = [abs(a.length - 12.0) / 12.0 for a in alts]
    check("ladder: the bucket rows really do span the length trade",
          len(alts) >= 2 and max(errs) > min(errs),
          "%r" % ["%.3f %%" % (100.0 * e) for e in errs])


# --------------------------------------------------------------------------- #
# 7 -- eta PAST DEFAULT_MAX_ETA
# --------------------------------------------------------------------------- #
def test_the_search_goes_past_the_default_eta_cap():
    """The target is exactly what eta = 39 gives at omega 40 on r = 1.5. The
    auto-solver's own cap (`DEFAULT_MAX_ETA` = 36) sits below it."""
    seed = mt.TubeDesign(1.5, 40.0, 39, 1)
    d, _ = mt.solve_tube_for_pore(1.5, seed.length, seed.pore_area)
    check("eta cap: the search reaches eta >= 37 (DEFAULT_MAX_ETA is %d)"
          % mt.DEFAULT_MAX_ETA, d.eta >= 37,
          "eta %d, omega %.1f, N %d, pore %.6f mm^2"
          % (d.eta, d.omega_deg, d.n_pivots, d.pore_area))
    check("eta cap: it finds the seeded design itself",
          d.eta == 39 and abs(d.omega_deg - 40.0) < 1e-9
          and d.pore_area == seed.pore_area,
          "%r" % (d,))
    # The cap control is stated AT A PINNED WINDING ANGLE, which is how the
    # measurement behind it was made ("pinned at 40 deg, eta = 15 gives a 485 um
    # pore and eta = 39 gives 187 um"). With omega free the capped search buys
    # the pore back by moving the winding angle instead -- measured, it lands
    # eta = 23 only 0.1 % off -- so a cap control over the full range would be
    # asserting something that is not true.
    pinned = dict(omega_range=(40.0, 40.0))
    capped, _ = mt.solve_tube_for_pore(1.5, seed.length, seed.pore_area,
                                       max_eta=mt.DEFAULT_MAX_ETA, **pinned)
    opened, _ = mt.solve_tube_for_pore(1.5, seed.length, seed.pore_area, **pinned)
    rel_capped = abs(capped.pore_area - seed.pore_area) / seed.pore_area
    rel_open = abs(opened.pore_area - seed.pore_area) / seed.pore_area
    check("eta cap: at omega 40 deg, capping at %d cannot reach the pore the "
          "open search hits exactly" % mt.DEFAULT_MAX_ETA,
          capped.eta <= mt.DEFAULT_MAX_ETA and rel_capped > 0.15 > rel_open
          and opened.eta == 39,
          "capped eta %d (N %d), %.1f %% off; open eta %d, %.3f %% off"
          % (capped.eta, capped.n_pivots, 100.0 * rel_capped, opened.eta,
             100.0 * rel_open))


# --------------------------------------------------------------------------- #
# 8 -- the preview debounce budget
# --------------------------------------------------------------------------- #
def test_the_default_call_fits_a_preview_debounce():
    """The page calls this inside a 350 ms debounce. Min of 5, measured."""
    ts = []
    for _ in range(5):
        t0 = time.perf_counter()
        mt.solve_tube_for_pore(0.75, 12.0, 0.200 ** 2, fiber_diameter=0.020)
        ts.append(time.perf_counter() - t0)
    best = min(ts)
    check("timing: the default call is under 100 ms", best < 0.100,
          "%.1f ms (min of 5)" % (1000.0 * best))


def main():
    tests = (test_green_reconstruction,
             test_refusal_names_the_fibre_floor,
             test_refusal_names_the_nearest_closing_length,
             test_refusal_names_the_nearest_pore,
             test_banding_prefers_the_length_over_half_a_percent_of_pore,
             test_the_alternatives_ladder_spans_the_trade,
             test_the_search_goes_past_the_default_eta_cap,
             test_the_default_call_fits_a_preview_debounce)
    for t in tests:
        try:
            t()
        except Exception as exc:                       # noqa: BLE001
            check(t.__name__, False, "%s: %s" % (type(exc).__name__, exc))
            traceback.print_exc()
    print("\n%d passed, %d failed, %d total"
          % (len(_PASS), len(_FAIL), len(_PASS) + len(_FAIL)))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
