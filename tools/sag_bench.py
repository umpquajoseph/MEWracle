"""sag_bench.py -- timing + accuracy harness for sag solvers.

Every candidate fast solver is judged against the EXACT solver
(`gcode_to_fibers.apply_sag`) -- the comparator this harness did not write -- on
the canonical target, the GUI-default scaffold at 30x30 mm / 0.5 stepover / 10
layers / 0.02 mm fibre and 90 deg rotation, plus harder variants (rotated
60 deg, wavy linear pattern, circle footprint).

Modes:
  python tools/sag_bench.py time             time the exact solver on each case
  python tools/sag_bench.py profile          cProfile the exact solver (default case)
  python tools/sag_bench.py compare M        compare candidate module M against exact
  python tools/sag_bench.py invariants M     hold M to the physical invariants, each
                                       with an injected-defect control

M must expose `drape_sag(paths, ptimes, diameter, sag_multiplier=..., ...)
-> (paths, times)`.

Accuracy metric (compare): both solvers emit points ON the commanded XY path
(z is what sagging changes), so each output fibre is parameterised by arc length
of its commanded centreline and the two z-profiles are interpolated onto a common
arc grid; report mean / RMS / max |dz| per case, in mm and in fibre diameters.
Never opens sag_approx.py (blindness contract).
"""

import os as _os, sys as _sys
# A developer script: the program's folders sit beside this one, and
# app_paths puts all of them on sys.path.
_sys.path.insert(0, _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "engine"))
import app_paths  # noqa: E402,F401

import cProfile
import io
import math
import importlib
import os
import pstats
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import scaffold_gcode                   # noqa: E402
import gcode_to_fibers as g2f           # noqa: E402


DEFAULT = dict(width=30.0, height=30.0, stepover=0.5, rotation=90.0,
               fiber_diameter=0.02, layers=10, speed=600.0)

CASES = [
    ("default_30x30_x10", dict(DEFAULT)),
    ("rot60_30x30_x10", dict(DEFAULT, rotation=60.0)),
    ("circle_30_x10", dict(DEFAULT, shape="circle")),
    # one sine period as [x, y] vertices (x in 0..1 of the wavelength)
    ("wavy_30x30_x6", dict(DEFAULT, layers=6,
                           linear_pattern=[[i / 16.0,
                                            0.6 * np.sin(2 * np.pi * i / 16.0)]
                                           for i in range(17)],
                           linear_wavelength=4.0, linear_spacing=0.0)),
]


def build_case(params):
    """Generate + parse one case -> (paths, ptimes, diameter)."""
    gcode = scaffold_gcode.generate_scaffold_gcode(**params)
    fd, path = tempfile.mkstemp(suffix=".gcode")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(gcode)
        paths, ptimes = g2f.parse_gcode(path, mode="g1", with_times=True)
    finally:
        os.unlink(path)
    return paths, ptimes, float(params["fiber_diameter"])


def run_exact(paths, ptimes, diameter):
    return g2f.apply_sag(paths, diameter, sag_multiplier=1.0,
                         point_times=ptimes)


def _profile_z(paths_in, paths_out):
    """Per-fibre (arc, z) profiles: arc position along the commanded XY path
    for every output point, so two solvers' outputs can be compared on z."""
    prof = []
    for pin, pout in zip(paths_in, paths_out):
        pin = np.asarray(pin, dtype=np.float64)
        pout = np.asarray(pout, dtype=np.float64)
        seg = np.hypot(np.diff(pin[:, 0]), np.diff(pin[:, 1]))
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        # output points ride the commanded XY path; recover each one's arc
        # position by walking the same cumulative-length map
        sego = np.hypot(np.diff(pout[:, 0]), np.diff(pout[:, 1]))
        cumo = np.concatenate([[0.0], np.cumsum(sego)])
        # normalise: both should end at the same total length (same XY path)
        if cumo[-1] > 0 and cum[-1] > 0:
            cumo = cumo * (cum[-1] / cumo[-1])
        prof.append((cumo, pout[:, 2], cum[-1]))
    return prof


def compare_outputs(paths_in, exact_out, cand_out, diameter, n_grid=2000):
    """Mean/RMS/max |dz| between two solved outputs, on a common arc grid."""
    pe = _profile_z(paths_in, exact_out)
    pc = _profile_z(paths_in, cand_out)
    errs = []
    for (ae, ze, tot), (ac, zc, _) in zip(pe, pc):
        if tot <= 0 or len(ae) < 2 or len(ac) < 2:
            continue
        grid = np.linspace(0.0, tot, n_grid)
        zeg = np.interp(grid, ae, ze)
        zcg = np.interp(grid, ac, zc)
        errs.append(np.abs(zeg - zcg))
    if not errs:
        return dict(mean=0.0, rms=0.0, mx=0.0)
    e = np.concatenate(errs)
    return dict(mean=float(e.mean()), rms=float(np.sqrt((e ** 2).mean())),
                mx=float(e.max()))


def _all_crossings(paths, diameter):
    """Every commanded XY crossing, found with the EXACT solver's own
    geometry tools (g2f._SegIndex / _seg_hits_xy -- independent of the
    engine under test): list of (i, arc_i, j, arc_j) with fibre i laid
    before fibre j (i < j in deposition order), plus self-crossings
    (i == j, arc_i < arc_j)."""
    cell = g2f._hash_cell(diameter)
    arrs = [np.asarray(p, dtype=np.float64) for p in paths]
    cums = []
    for p in arrs:
        seg = np.hypot(np.diff(p[:, 0]), np.diff(p[:, 1]))
        cums.append(np.concatenate([[0.0], np.cumsum(seg)]))
    out = []
    for i, pi in enumerate(arrs):
        if len(pi) < 2:
            continue
        idx = g2f._SegIndex(cell)
        idx.add_fiber(pi)
        for j in range(i, len(arrs)):
            pj = arrs[j]
            if len(pj) < 2:
                continue
            qi, si = idx.seg_pairs(pj[:-1, 0], pj[:-1, 1],
                                   pj[1:, 0], pj[1:, 1])
            if j == i:
                keep = si < qi - 1
                qi, si = qi[keep], si[keep]
            if qi.size == 0:
                continue
            hit, t, u = g2f._seg_hits_xy(
                pj[qi, 0], pj[qi, 1], pj[qi + 1, 0], pj[qi + 1, 1],
                pi[si, 0], pi[si, 1], pi[si + 1, 0], pi[si + 1, 1])
            if not hit.any():
                continue
            qh, sh = qi[hit], si[hit]
            arc_j = cums[j][qh] + t[hit] * (cums[j][qh + 1] - cums[j][qh])
            arc_i = cums[i][sh] + u[hit] * (cums[i][sh + 1] - cums[i][sh])
            for aj, ai in zip(arc_j.tolist(), arc_i.tolist()):
                if i == j:
                    lo, hi = min(ai, aj), max(ai, aj)
                    out.append((i, lo, j, hi))
                else:
                    out.append((i, ai, j, aj))
    return out


def _seam_joined(paths, i, diameter):
    """Does fibre i+1 open on fibre i's last vertex (the solver's own test)?"""
    return bool(len(paths[i]) and len(paths[i + 1])
                and math.hypot(paths[i + 1][0][0] - paths[i][-1][0],
                               paths[i + 1][0][1] - paths[i][-1][1])
                < g2f._SEAM_MATCH * diameter)


def crossing_separations(paths, solved, diameter, loop_flags=None):
    """Signed separation z_later - z_earlier at every commanded crossing.
    The rest-on-top physics guarantees ~one diameter; a separation far below
    it is the later strand PHASING THROUGH the earlier one. Crossings within
    1.5 d of a loop fibre's entry are excluded (entry continuity deliberately
    merges there). Returns (separations, positions) arrays."""
    prof = _profile_z(paths, solved)
    crossings = _all_crossings(paths, diameter)
    seps = []
    pos = []
    for i, ai, j, aj in crossings:
        if loop_flags is not None and loop_flags[j] and aj < 6.0 * diameter:
            continue
        if loop_flags is not None and loop_flags[i] and ai < 6.0 * diameter:
            continue
        # The layer-change seam: fibre j opens on fibre j-1's last vertex, one
        # continuous filament (`g2f.seam_tail_z`), so the "crossing" of the head
        # over the tail is the strand meeting itself, at separation 0 by design.
        if (j == i + 1 and aj < 6.0 * diameter
                and ai > float(prof[i][0][-1]) - 6.0 * diameter
                and _seam_joined(paths, i, diameter)):
            continue
        zi = float(np.interp(ai, prof[i][0], prof[i][1]))
        zj = float(np.interp(aj, prof[j][0], prof[j][1]))
        seps.append(zj - zi)
        pos.append((i, ai, j, aj))
    return np.asarray(seps), pos


def junction_profiles(paths, solved, diameter, fibre, half=0.25, n=41,
                      margin=3.0):
    """Aligned bump profiles of every INTERIOR crossing of `fibre` over the
    layer below: (n_junctions, n) z samples on +/- `half` mm around each
    crossing arc. Interior junctions of a uniform grid share identical
    commanded geometry, so their solved shapes must agree -- a spread far
    above the exact solver's own is the per-junction inconsistency defect."""
    arrs = [np.asarray(p, dtype=np.float64) for p in paths]
    prof = _profile_z(paths, solved)
    xs = np.concatenate([p[:, 0] for p in arrs])
    ys = np.concatenate([p[:, 1] for p in arrs])
    x0, x1 = xs.min() + margin, xs.max() - margin
    y0, y1 = ys.min() + margin, ys.max() - margin
    crossings = [c for c in _all_crossings(paths, diameter)
                 if c[2] == fibre and c[0] == fibre - 1]
    base = arrs[fibre]
    seg = np.hypot(np.diff(base[:, 0]), np.diff(base[:, 1]))
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    rows = []
    grid = np.linspace(-half, half, n)
    for _i, _ai, _j, aj in crossings:
        x = float(np.interp(aj, cum, base[:, 0]))
        y = float(np.interp(aj, cum, base[:, 1]))
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            continue
        if aj - half < 0 or aj + half > cum[-1]:
            continue
        rows.append(np.interp(aj + grid, prof[fibre][0], prof[fibre][1]))
    return np.asarray(rows), grid


def surface_penetration(paths, solved, diameter, loop_flags=None):
    """How far below the rest surface of everything laid BEFORE it any output
    point sits, judged by the exact solver's own `_SegIndex.rest_heights`.
    THE invariant a crossing-only check misses: a layer
    lying collinearly over the layer two below produces no commanded
    crossing, so a fibre could phase through it unseen. Returns
    (worst_interior, n_interior, worst_fringe, n_fringe) in diameters /
    counts of points >0.25 d, split at the commanded footprint (0.5 mm
    inset): the printed interior must be CLEAN; the turnaround fringe
    carries a documented bounded residual. Points within 6 d of a loop
    fibre's entry are excluded (entry continuity merges there by design)."""
    arrs = [np.asarray(p, dtype=np.float64) for p in paths]
    lay, nl = g2f._layer_ids(arrs, layer_tol=0.5 * diameter)
    idx = g2f._SegIndex(g2f._hash_cell(diameter))
    bed = min(float(np.median(p[:, 2])) for p in arrs if len(p))

    def sharp_arcs(cmd):
        """Arc positions of the commanded path's sharp turns (turnarounds) --
        geometry-general, unlike a bounding box (a circle footprint's edge
        ring is not 'interior' just because it fits in the box)."""
        seg = np.hypot(np.diff(cmd[:, 0]), np.diff(cmd[:, 1]))
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        d1 = np.diff(cmd[:, :2], axis=0)
        ln = np.maximum(np.hypot(d1[:, 0], d1[:, 1]), 1e-12)
        u = d1 / ln[:, None]
        dot = (u[:-1] * u[1:]).sum(axis=1)
        sharp = np.flatnonzero(dot < 0.94)          # > ~20 deg per vertex
        return cum, cum[sharp + 1]
    wi = wf = 0.0
    ni = nf = 0
    for li in range(nl):
        mem = [i for i in range(len(solved)) if lay[i] == li]
        for i in mem:
            p = np.asarray(solved[i])
            if idx.n and len(p):
                rest = idx.rest_heights(p[:, 0], p[:, 1], diameter / 2.0, bed)
                pen = (rest - p[:, 2]) / diameter
                seg = np.hypot(np.diff(p[:, 0]), np.diff(p[:, 1]))
                cum = np.concatenate([[0.0], np.cumsum(seg)])
                if loop_flags is not None and loop_flags[i]:
                    pen = np.where(cum < 6.0, -np.inf, pen)
                if i > 0 and _seam_joined(arrs, i - 1, diameter):
                    # the head of a joined fibre IS its predecessor's tail
                    pen = np.where(cum < 6.0 * diameter, -np.inf, pen)
                _ccum, turns = sharp_arcs(g2f._dedupe(arrs[i]))
                if turns.size:
                    j = np.clip(np.searchsorted(turns, cum), 1, turns.size)
                    near = np.minimum(np.abs(cum - turns[j - 1]),
                                      np.abs(cum - turns[np.minimum(
                                          j, turns.size - 1)]))
                    interior = near > 1.5
                else:
                    interior = np.ones(len(p), dtype=bool)
                pi = pen[interior]
                pf = pen[~interior]
                if pi.size:
                    wi = max(wi, float(pi.max()))
                    ni += int((pi > 0.25).sum())
                if pf.size:
                    wf = max(wf, float(pf.max()))
                    nf += int((pf > 0.25).sum())
        for i in mem:
            if len(np.asarray(solved[i])) >= 2:
                idx.add_fiber(np.asarray(solved[i]), diameter / 2.0)
    return wi, ni, wf, nf


def main(argv):
    mode = argv[1] if len(argv) > 1 else "time"
    if mode == "time":
        for name, params in CASES:
            paths, ptimes, d = build_case(params)
            npts = sum(len(p) for p in paths)
            t0 = time.perf_counter()
            out, _ = run_exact(paths, ptimes, d)
            dt = time.perf_counter() - t0
            nout = sum(len(p) for p in out)
            print("%-22s exact %.2f s   (%d fibres, %d -> %d pts)"
                  % (name, dt, len(paths), npts, nout))
        return 0
    if mode == "profile":
        paths, ptimes, d = build_case(CASES[0][1])
        pr = cProfile.Profile()
        pr.enable()
        run_exact(paths, ptimes, d)
        pr.disable()
        s = io.StringIO()
        pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(30)
        print(s.getvalue())
        return 0
    if mode == "invariants":
        # Physical invariants on the CANDIDATE's own output, thresholds
        # calibrated against the exact solver on the same geometry. These
        # exist for two historical defect classes the |dz| metric averaged
        # away: junctions that PHASE THROUGH each other, and identical-
        # geometry junctions solved to different shapes.
        cand = importlib.import_module(argv[2])
        fail = False
        cases = CASES + [("default+link_loops",
                          dict(DEFAULT, link_loops="manual"))]
        for name, params in cases:
            paths, ptimes, d = build_case(params)
            flags = g2f.loop_entry_flags(paths)
            exact_out, _ = g2f.apply_sag(paths, d, sag_multiplier=1.0,
                                         point_times=ptimes, loop_entry=flags)
            cand_out, _ = cand.drape_sag(paths, ptimes, d, sag_multiplier=1.0)
            se, _pe = crossing_separations(paths, exact_out, d, flags)
            sc, pc = crossing_separations(paths, cand_out, d, flags)
            n_bad = int((sc < 0.6 * d).sum())
            print("%-22s crossings %5d  min sep: exact %.3f d, cand %.3f d, "
                  "cand<0.6d: %d"
                  % (name, sc.size, se.min() / d if se.size else 9,
                     sc.min() / d if sc.size else 9, n_bad))
            if n_bad:
                fail = True
                worst = np.argsort(sc)[:5]
                for w in worst:
                    i, ai, j, aj = pc[w]
                    print("   PHASE-THROUGH fibre %d@%.2f under fibre %d@%.2f"
                          " sep %.3f d" % (i, ai, j, aj, sc[w] / d))
            # surface penetration -- the check the crossing walk cannot do
            # (collinear overlaps produce no crossing). Exact is at zero on every
            # case; these limits bound the approximate engine's ACCEPTED foot-only
            # departure (sag_approx's docstring has the cause and the rejected
            # fix). Banded from the measured worst case rather than tuned to pass:
            # interior 0.259 d on 1 pt (link_loops), fringe 0.871 d on up to
            # 5702 pts (wavy). Two things these numbers are NOT, and a session
            # reading a red gate needs both:
            #   * `nf`/`ni` are POINT COUNTS, so they move with any change in
            #     sampling density and are not comparable across one. The
            #     sampling-independent figure is penetrating ARC LENGTH, which
            #     this function does not return -- measured separately at
            #     4.5-5.8 mm of 16-21 m laid, and 47.5 mm of 17.6 m on wavy.
            #   * the interior/fringe split is `sharp_arcs`, distance from a
            #     >20 deg commanded turn. On `wavy_30x30_x6` the sine pattern
            #     turns every 0.65 mm, so 100.0% of points classify as FRINGE and
            #     `wi`/`ni` measure nothing there. Do not read "fringe" as
            #     "outside the printed part" on that case: 76% of its penetrating
            #     points are inside the footprint.
            wi, ni, wf, nf = surface_penetration(paths, cand_out, d, flags)
            pen_ok = ni <= 4 and wi < 0.35 and wf < 1.25 and nf < 8000
            print("   penetration: interior worst %.3f d (%d pts >0.25d), "
                  "fringe worst %.3f d (%d pts)  %s"
                  % (wi, ni, wf, nf, "ok" if pen_ok else "FAIL"))
            if not pen_ok:
                fail = True
        # ---- junction-shape consistency (default + rot60, two layers) ---- #
        for ci in (0, 1):
            paths, ptimes, d = build_case(dict(CASES[ci][1]))
            exact_out, _ = run_exact(paths, ptimes, d)
            cand_out, _ = cand.drape_sag(paths, ptimes, d, sag_multiplier=1.0)
            for fibre in (5, 9):
                re_, grid = junction_profiles(paths, exact_out, d, fibre)
                rc_, _g = junction_profiles(paths, cand_out, d, fibre)
                sp_e = float(np.ptp(re_, axis=0).max()) if len(re_) else 0.0
                sp_c = float(np.ptp(rc_, axis=0).max()) if len(rc_) else 0.0
                limit = max(1.3 * sp_e, 0.35 * d)
                ok = sp_c <= limit
                print("junction spread %s fibre %d: exact %.3f d, cand %.3f d"
                      " (limit %.3f d, %d junctions)  %s"
                      % (CASES[ci][0], fibre, sp_e / d, sp_c / d, limit / d,
                         len(rc_), "ok" if ok else "FAIL"))
                if not ok:
                    fail = True
        # ---- red proofs: the checks must FIRE on the defect classes ------- #
        paths, ptimes, d = build_case(dict(CASES[0][1]))
        exact_out, _ = run_exact(paths, ptimes, d)
        if getattr(cand, "_self_crossings_of", None) is not None:
            orig = cand._self_crossings_of
            cand._self_crossings_of = lambda *a, **k: []
            try:
                broken, _ = cand.drape_sag(paths, ptimes, d,
                                           sag_multiplier=1.0)
            finally:
                cand._self_crossings_of = orig
            sb_, _pb = crossing_separations(paths, broken, d)
            fired = (sb_ < 0.6 * d).any()
            print("red proof (self-cross phase-through): %s"
                  % ("fired" if fired else "DID NOT FIRE"))
            if not fired:
                fail = True
        if getattr(cand, "_find_pairs", None) is not None:
            orig = cand._find_pairs

            def leaky(*a, **k):
                p = orig(*a, **k)
                if p is not None and p.size > 20:
                    keep = np.arange(p.size) % 7 != 0     # drop every 7th
                    # ONLY the per-pair arrays. `__slots__` also carries a
                    # whole-set scalar (`_Pairs.periodic`), and masking that
                    # raised `TypeError: 'bool' object is not subscriptable`
                    # here, which would abort this whole mode before its
                    # verdict line. The candidate names its own array slots; the
                    # fallback keeps an engine that publishes no such list
                    # working exactly as before.
                    for nm in getattr(type(p), "PAIR_ARRAYS",
                                      type(p).__slots__):
                        v = getattr(p, nm)
                        # Six of them are periodic-only and are None on a flat
                        # design (`_Pairs.PAIR_ARRAYS_PERIODIC_ONLY`); there is
                        # nothing to mask, and masking it raised
                        # `TypeError: 'NoneType' object is not subscriptable`.
                        if v is None:
                            continue
                        setattr(p, nm, v[keep])
                return p
            cand._find_pairs = leaky
            try:
                broken, _ = cand.drape_sag(paths, ptimes, d,
                                           sag_multiplier=1.0)
            finally:
                cand._find_pairs = orig
            sb_, _pb = crossing_separations(paths, broken, d)
            r9, _g = junction_profiles(paths, broken, d, 9)
            spread_b = float(np.ptp(r9, axis=0).max()) if len(r9) else 0.0
            wi_b, ni_b, _wf, _nf = surface_penetration(paths, broken, d)
            fired = ((sb_ < 0.6 * d).any() or ni_b > 0 or spread_b > max(
                1.3 * float(np.ptp(junction_profiles(paths, exact_out, d, 9)[0],
                                   axis=0).max()), 0.35 * d))
            print("red proof (dropped contacts): %s (sep %.2f d, interior "
                  "pen %.2f d x%d, spread %.2f d)"
                  % ("fired" if fired else "DID NOT FIRE",
                     sb_.min() / d if sb_.size else 9, wi_b, ni_b,
                     spread_b / d))
            if not fired:
                fail = True
        # the collinear-overlap class specifically: the OLD engine's failure
        # (full-diameter penetration with no commanded crossing) must fire
        # the penetration check -- emulate it by disabling the grazing
        # windows so coincident overlaps lose their clamp coverage
        if getattr(cand, "_find_pairs", None) is not None:
            orig = cand._find_pairs

            def narrow(*a, **k):
                p = orig(*a, **k)
                if p is not None and p.size:
                    p.hw = np.minimum(p.hw, 0.06)      # kill grazing windows
                return p
            cand._find_pairs = narrow
            try:
                broken, _ = cand.drape_sag(paths, ptimes, d,
                                           sag_multiplier=1.0)
            finally:
                cand._find_pairs = orig
            wi_b, ni_b, _wf, _nf = surface_penetration(paths, broken, d)
            fired = ni_b > 0
            print("red proof (collinear phase-through): %s (interior pen "
                  "%.2f d x%d)" % ("fired" if fired else "DID NOT FIRE",
                                   wi_b, ni_b))
            if not fired:
                fail = True
        print("INVARIANTS:", "FAIL" if fail else "PASS")
        return 1 if fail else 0
    if mode == "compare":
        cand = importlib.import_module(argv[2])
        fail = False
        for name, params in CASES:
            paths, ptimes, d = build_case(params)
            t0 = time.perf_counter()
            exact_out, _ = run_exact(paths, ptimes, d)
            te = time.perf_counter() - t0
            t0 = time.perf_counter()
            cand_out, ct = cand.drape_sag(paths, ptimes, d, sag_multiplier=1.0)
            tc = time.perf_counter() - t0
            m = compare_outputs(paths, exact_out, cand_out, d)
            print("%-22s exact %6.2f s  cand %6.2f s  |dz| mean %.4f um  "
                  "rms %.4f um  max %.4f um (%.2f d)"
                  % (name, te, tc, m["mean"] * 1e3, m["rms"] * 1e3,
                     m["mx"] * 1e3, m["mx"] / d))
            for t in ct:
                if t is not None and len(t) > 1 and np.any(np.diff(t) < 0):
                    print("  ^^ FAIL: candidate times not monotonic")
                    fail = True
            if tc > 5.0:
                print("  ^^ FAIL: candidate over the 5 s budget")
                fail = True
            if m["mx"] > 2.0 * d:
                # a blunt ceiling: catastrophic drift (a fibre a whole layer
                # pair wrong). The physical phase-through class is owned by
                # `invariants` (surface_penetration), which measures against
                # the laid structure rather than against exact's sampling --
                # legitimate local shape differences (the wavy tangle) reach
                # ~1.6 d here without any interpenetration.
                print("  ^^ FAIL: max |dz| %.2f d over the 2.0 d gate"
                      % (m["mx"] / d))
                fail = True
        # ---- feature variants on the default case ---- #
        paths, ptimes, d = build_case(dict(CASES[0][1]))
        band = (0.09, 0.55)

        def law(diam, wall=False):
            return 0.1 * diam if wall else 0.3 * diam

        variants = [
            ("mult 0.4", dict(sag_multiplier=0.4), dict(sag_multiplier=0.4)),
            ("mult 2.5", dict(sag_multiplier=2.5), dict(sag_multiplier=2.5)),
            ("reset 4", dict(sag_multiplier=1.0, sag_reset_layers=4),
             dict(sag_multiplier=1.0, sag_reset_layers=4)),
            ("fusion", dict(sag_multiplier=1.0, _fusion=True),
             dict(sag_multiplier=1.0, fuse_sink=law(d), fuse_wall=law(d, True),
                  band=band)),
        ]
        for vname, ex_kw, cd_kw in variants:
            fus = ex_kw.pop("_fusion", False)
            if fus:
                g2f.set_fusion_law(law, band)
            try:
                t0 = time.perf_counter()
                exact_out, _ = g2f.apply_sag(paths, d, point_times=ptimes,
                                             **ex_kw)
                te = time.perf_counter() - t0
            finally:
                if fus:
                    g2f.set_fusion_law(None)
            t0 = time.perf_counter()
            cand_out, _ = cand.drape_sag(paths, ptimes, d, **cd_kw)
            tc = time.perf_counter() - t0
            m = compare_outputs(paths, exact_out, cand_out, d)
            print("%-22s exact %6.2f s  cand %6.2f s  |dz| mean %.4f um  "
                  "rms %.4f um  max %.4f um (%.2f d)"
                  % ("default+" + vname, te, tc, m["mean"] * 1e3,
                     m["rms"] * 1e3, m["mx"] * 1e3, m["mx"] / d))
            if tc > 5.0:
                print("  ^^ FAIL: candidate over the 5 s budget")
                fail = True
        # ---- the physics-relaid path (what the app actually drapes) ---- #
        # timing-gated here because nothing else guards it: the app's
        # end-to-end budget sits in test_gui_preview, but the drape's own
        # 5 s budget must hold on the input it really receives
        import mew_deposition
        import mew_physics
        paths, ptimes, d = build_case(dict(CASES[0][1]))
        tr = mew_deposition.make_transform(
            mew_physics.MEWModel(), pressure=3.0 * 6894.76, voltage=6.0,
            temperature=90.0)
        rp, rt = tr(paths, ptimes)
        t0 = time.perf_counter()
        cand_out, _ = cand.drape_sag(rp, rt, d, sag_multiplier=1.0)
        tc = time.perf_counter() - t0
        print("%-22s cand %6.2f s  (%d relaid pts)"
              % ("default+physics-relay", tc, sum(len(p) for p in rp)))
        if tc > 5.0:
            print("  ^^ FAIL: relaid drape over the 5 s budget")
            fail = True
        # ---- linking loops (exact needs the entry flags) ---- #
        paths, ptimes, d = build_case(dict(DEFAULT, link_loops="manual"))
        flags = g2f.loop_entry_flags(paths)
        t0 = time.perf_counter()
        exact_out, _ = g2f.apply_sag(paths, d, sag_multiplier=1.0,
                                     point_times=ptimes, loop_entry=flags)
        te = time.perf_counter() - t0
        t0 = time.perf_counter()
        cand_out, _ = cand.drape_sag(paths, ptimes, d, sag_multiplier=1.0)
        tc = time.perf_counter() - t0
        m = compare_outputs(paths, exact_out, cand_out, d)
        print("%-22s exact %6.2f s  cand %6.2f s  |dz| mean %.4f um  "
              "rms %.4f um  max %.4f um (%.2f d)"
              % ("default+link_loops", te, tc, m["mean"] * 1e3,
                 m["rms"] * 1e3, m["mx"] * 1e3, m["mx"] / d))
        if tc > 5.0:
            print("  ^^ FAIL: candidate over the 5 s budget")
            fail = True
        return 1 if fail else 0
    print("unknown mode %r" % mode)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
