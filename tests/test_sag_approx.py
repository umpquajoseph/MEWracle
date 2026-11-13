#!/usr/bin/env python3
"""test_sag_approx.py -- self-running tests for the APPROXIMATE sag engine
(sag_approx.py, behind the Export panel's "Sag solver: Approximate (fast)").

NOT the scaffold-designer preview's sagging -- that is `preview_sag.py`, a
separate and much faster engine with its own suite (`test_preview_sag.py`).

Run:  python tests/test_sag_approx.py. Headless: pure numpy, no Tk.

Guards the engine's mechanism against hand-built geometry, its ENVELOPE against
the exact `apply_sag` on real generated scaffolds at 90 and 60 degrees (mean RMS
under ~half a fibre diameter), and the CONVERTER WIRING -- that `--sag-approx`
actually reaches this engine and stamps the `.depo` cache, which no unit test of
`drape_sag` can see.

ACCURACY IS NOT FIXED HERE: `sag_bench.py compare sag_approx` measures it and
`sag_bench.py invariants sag_approx` holds the phase-through and junction-shape
guards. Run both after any engine change."""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import gcode_to_fibers as g2f  # noqa: E402
import mew_frame               # noqa: E402
import sag_approx as ps       # noqa: E402

FAILURES = []
RAN = []


def check(name, fn):
    RAN.append(name)
    try:
        fn()
        print(f"PASS  {name}")
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(name)
        print(f"FAIL  {name}: {exc!r}")


def _line(p0, p1, z, n=9):
    t = np.linspace(0.0, 1.0, n)
    return np.column_stack([p0[0] + (p1[0] - p0[0]) * t,
                            p0[1] + (p1[1] - p0[1]) * t,
                            np.full(n, z)])


# ---------------------------------------------------------------------------
# mechanism, on hand-built geometry
# ---------------------------------------------------------------------------
def bottom_layer_rests_on_bed():
    """Commanded Z 0 is the PLATE, and the fibre rests its UNDERSIDE on it, so
    the centreline that comes back stands one radius up. It used to come back at
    0 with half the tube inside the plate -- the defect `shots.json`'s
    `underside_clearance_mm` read as -0.0109 mm."""
    d = 0.02
    paths = [_line((-5, 0), (5, 0), 0.0)]
    out, _ = ps.drape_sag(paths, None, d)
    assert np.allclose(out[0][:, 2], 0.5 * d, atol=1e-9), \
        f"bottom layer not seated: z in [{out[0][:, 2].min()}, {out[0][:, 2].max()}]"


def crossing_lifts_one_diameter():
    d = 0.02
    paths = [_line((-5, 0), (5, 0), 0.0),        # support along X
             _line((0, -5), (0, 5), d)]          # crosses it along Y
    out, _ = ps.drape_sag(paths, None, d)
    z = out[1][:, 2]
    # rests one diameter above the support's CENTRELINE, which itself seats one
    # radius off the plate; the far ends come back down onto that same seat
    assert abs(z.max() - 1.5 * d) < 0.25 * d, f"crossing peak {z.max()} != {1.5 * d}"
    assert z.min() < 0.75 * d, f"far ends did not sag to bed: {z.min()}"
    # the peak is AT the crossing (y=0), not somewhere else
    y = out[1][:, 1]
    assert abs(float(y[np.argmax(z)])) < 0.2, "peak not at the crossing"


def tight_pore_free_hangs():
    """Two supports closer than the droop can reach the floor: the bridging
    fibre must droop sag_depth(span), not slam to the bed."""
    d = 0.05
    L = 0.3                                       # droop 0.018 < diameter
    paths = [_line((-0.5 * L, -5), (-0.5 * L, 5), 0.0),
             _line((+0.5 * L, -5), (+0.5 * L, 5), 0.0),
             _line((-5, 0), (5, 0), d)]           # bridges both
    out, _ = ps.drape_sag(paths, None, d)
    b = out[2]
    mid = b[np.abs(b[:, 0]) < 0.05 * L, 2]
    # the bridge seats one diameter above the supports' centrelines, and they in
    # turn seat one radius off the plate
    want = 1.5 * d - g2f.sag_depth(L, d)
    assert len(mid), "no mid-span samples"
    got = float(mid.min())
    assert abs(got - want) < 0.3 * g2f.sag_depth(L, d) + 1e-6, \
        f"mid-span droop {d - got:.4f}, want {g2f.sag_depth(L, d):.4f}"


def deposition_order_stacks():
    """Same two crossed fibres, opposite order: whoever is laid SECOND rides."""
    d = 0.02
    a = _line((-5, 0), (5, 0), 0.0)
    b = _line((0, -5), (0, 5), d)
    out1, _ = ps.drape_sag([a, b], None, d)
    out2, _ = ps.drape_sag([b, a], None, d)
    assert abs(out1[1][:, 2].max() - 1.5 * d) < 0.25 * d
    assert np.allclose(out2[0][:, 2], 0.5 * d, atol=1e-9), \
        "first-laid must be flat on its seat"
    assert abs(out2[1][:, 2].max() - 1.5 * d) < 0.25 * d, "second-laid must ride"


def approx_engine_does_not_shred_a_curved_single_layer():
    """R1-2f: `apply_sag_approx` world-converts `paths` for a non-developable
    `frame` (the R1-2 currency patch, `gcode_to_fibers.py:5028-5031`) before
    handing them to `_split_layer_spanning`, which starts a new band whenever a
    point's Z departs from its band's anchor by more than `0.5 * diameter` --
    correct when Z tracks deposition-layer height (true on a flat bed, and true
    for the exact solver's own call at `gcode_to_fibers.py:4820`, which stays in
    chart space). On a dome, WORLD Z sweeps through the whole curved shape even
    within a single chart-height-constant layer, so one 200-point layer here
    gets cut into ~190 one-point bands, each of which then hits
    `drape_sag`'s `len(base) < 2` guard and is passed through completely
    unsolved -- found via R1-2c/d/e on the arbitrary-collector-surfaces task's 6
    mm dome regression (2506 bands from 10 real layers there).

    Comparator: `_split_layer_spanning`'s own band count for ONE path whose
    chart height (`h`) is constant throughout -- by the function's own contract
    a single true layer, so it must come back as exactly one band regardless of
    how much the collector curves under it."""
    frame = mew_frame.RevolutionFrame(
        mew_frame.ArcProfile(5.0, t0=0.5 * math.pi), u0=0.0, u1=7.0)
    n = 200
    u = np.linspace(1.0, 4.0, n)
    path = np.column_stack([u, np.zeros(n), np.zeros(n)])

    orig_split = g2f._split_layer_spanning
    captured = {}

    def spy(*a, **kw):
        subs, owner, stimes = orig_split(*a, **kw)
        captured["n_bands"] = 1 if subs is None else len(subs)
        return subs, owner, stimes

    g2f._split_layer_spanning = spy
    try:
        g2f.apply_sag_approx([path], 0.02, frame=frame)
    finally:
        g2f._split_layer_spanning = orig_split

    assert captured.get("n_bands") == 1, (
        "a single chart-height-constant layer on a dome was cut into %r "
        "bands -- _split_layer_spanning is keying on world Z instead of "
        "chart height" % captured.get("n_bands"))


def multiplier_zero_passthrough():
    d = 0.02
    paths = [_line((-5, 0), (5, 0), 0.0), _line((0, -5), (0, 5), d)]
    times = [np.linspace(0, 1, len(p)) for p in paths]
    out, ot = ps.drape_sag(paths, times, d, sag_multiplier=0.0)
    assert out is paths and ot is times, "mult<=0 must pass through untouched"


def times_resampled_monotonic():
    d = 0.02
    paths = [_line((-5, 0), (5, 0), 0.0), _line((0, -5), (0, 5), d)]
    times = [np.linspace(3.0, 7.0, len(p)) for p in paths]
    out, ot = ps.drape_sag(paths, times, d)
    for p, t in zip(out, ot):
        assert len(p) == len(t), "points/times length mismatch"
        assert np.all(np.diff(t) >= -1e-12), "times not monotonic"
    assert abs(ot[1][0] - 3.0) < 1e-9 and abs(ot[1][-1] - 7.0) < 1e-9, \
        "endpoint times not preserved"


def simplify_keeps_peaks():
    """Output stays THIN along flat resting spans while keeping the crossover
    peak (the look). Engine-agnostic form of the old _simplify check -- the
    budget is half-diameter sampling of the whole line, which any flat-span
    thinning must beat comfortably."""
    d = 0.02
    paths = [_line((-5, 0), (5, 0), 0.0), _line((0, -5), (0, 5), d)]
    out, _ = ps.drape_sag(paths, None, d)
    z = out[1][:, 2]
    assert abs(z.max() - 1.5 * d) < 0.25 * d, "peak lost in thinning"
    raw = 10.0 / (0.5 * d)          # dense half-diameter sampling reference
    assert len(z) < 0.6 * raw, f"no thinning: {len(z)} of ~{raw:.0f}"


# ---------------------------------------------------------------------------
# the load-bearing check: envelope vs the exact solver on real scaffolds
# ---------------------------------------------------------------------------
def _generated_paths(rotation, layers):
    import scaffold_gcode
    spec = dict(width=10.0, height=10.0, stepover=0.5, rotation=rotation,
                fiber_diameter=0.02, layers=layers, speed=600.0,
                start_x=0.0, start_y=0.0, start_z=0.0)
    kwargs = scaffold_gcode._scaffold_from_spec(spec)
    gcode = scaffold_gcode.generate_multi_scaffold_gcode([kwargs])
    import tempfile
    gpath = os.path.join(tempfile.gettempdir(), "test_preview_sag.gcode")
    with open(gpath, "w") as fh:
        fh.write(gcode)
    return g2f.parse_gcode(gpath, mode="g1", with_times=False, arc_segments=8)


def _rms_vs_exact(rotation, layers):
    d = 0.02
    paths = _generated_paths(rotation, layers)
    exact = g2f.apply_sag(paths, d)
    drape, _ = ps.drape_sag(paths, None, d)

    def s_of(p):
        seg = np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1)
        return np.concatenate([[0.0], np.cumsum(seg)])

    rms = []
    for e, a in zip(exact, drape):
        se, sa = s_of(e), s_of(a)
        grid = np.linspace(0.0, min(se[-1], sa[-1]), 1500)
        ze = np.interp(grid, se, e[:, 2])
        za = np.interp(grid, sa, a[:, 2])
        rms.append(float(np.sqrt(np.mean((ze - za) ** 2))))
    return np.mean(rms), np.max(rms), d


def envelope_matches_exact_90deg():
    mean, worst, d = _rms_vs_exact(90.0, 6)
    assert mean < 0.55 * d, f"mean RMS {mean * 1e3:.1f} um > 0.55 diam"
    assert worst < 1.0 * d, f"worst-layer RMS {worst * 1e3:.1f} um > 1 diam"


def envelope_matches_exact_60deg():
    mean, worst, d = _rms_vs_exact(60.0, 6)
    assert mean < 0.55 * d, f"mean RMS {mean * 1e3:.1f} um > 0.55 diam"
    assert worst < 1.0 * d, f"worst-layer RMS {worst * 1e3:.1f} um > 1 diam"


# --- junction fusion -------------------------------------------------------- #
# The drape has ONE floor (`f = H + diameter`), which is both of the exact solver's
# twin sink sites at once (support heights + the rest-on-top clamp), so fusion is
# just lowering it where a fibre -- not the bed -- is underneath. These pin that it
# reproduces the exact solver's falsifiable claim rather than merely "moves down".

_FD = 0.02          # fibre diameter for the fusion grid
_FSTEP = 0.4
_FSIZE = 2.4


def _fgrid(layers=6, npts=121):
    """Alternating-direction grid, one Z per layer, layer height = diameter."""
    paths = []
    n = int(round(_FSIZE / _FSTEP)) + 1
    for li in range(layers):
        z = li * _FD
        for k in range(n):
            c = k * _FSTEP
            t = np.linspace(0.0, _FSIZE, npts)
            f = np.full(npts, c)
            zz = np.full(npts, z)
            paths.append(np.column_stack([t, f, zz]) if li % 2 == 0
                         else np.column_stack([f, t, zz]))
    return paths, n


def _fpeaks(out, per, layers):
    return [max(p[:, 2].max() for p in out[li * per:(li + 1) * per])
            for li in range(layers)]


def fusion_off_is_byte_identical():
    """fuse_sink=0 (the default) must not move a single point."""
    paths, _ = _fgrid(4)
    a, _ = ps.drape_sag(paths, None, _FD)
    b, _ = ps.drape_sag(paths, None, _FD, fuse_sink=0.0)
    for i, (x, y) in enumerate(zip(a, b)):
        assert np.array_equal(x, y), f"fibre {i} moved with fusion off"


def fusion_bottom_layer_does_not_fuse():
    """The bottom layer rests on the BED, which never fuses -- only fibre-on-fibre
    contacts sink (the `_sink_fused_contacts` rule)."""
    paths, per = _fgrid(4)
    off, _ = ps.drape_sag(paths, None, _FD)
    on, _ = ps.drape_sag(paths, None, _FD, fuse_sink=0.4 * _FD)
    for i in range(per):                      # bottom layer = first `per` fibres
        assert np.array_equal(off[i], on[i]), \
            f"bottom-layer fibre {i} moved -- a bed contact was fused"


def fusion_reduces_column_slope_like_exact():
    """The load-bearing claim, shared with test_mew_fusion: the column rises by
    (d - delta) per layer instead of d, and the drape's peaks match the EXACT
    solver's to numerical precision. Guards the drape against drifting from the
    solve it previews."""
    layers = 6
    paths, per = _fgrid(layers)
    delta = 0.4 * _FD

    dr_on = _fpeaks(ps.drape_sag(paths, None, _FD, fuse_sink=delta)[0], per, layers)
    for a, b in zip(dr_on, dr_on[1:]):        # linear reduced slope
        assert abs((b - a) - (_FD - delta)) < 1e-9, \
            f"rise {b - a:.6f} != d - delta {_FD - delta:.6f}"

    law = _fusion_law_or_none(delta)
    if law is None:                           # physics modules absent -> slope only
        return
    g2f.set_fusion_law(law)
    try:
        ex_on = _fpeaks(g2f.apply_sag(paths, _FD), per, layers)
    finally:
        g2f.set_fusion_law(None)              # never leak into other tests
    worst = max(abs(a - b) for a, b in zip(dr_on, ex_on))
    assert worst < 1e-9, \
        f"drape peaks differ from exact by {worst:.6f} mm with fusion on"


# --------------------------------------------------------------------------- #
# The wall/junction split in the drape. The heightmap alone knows how HIGH the
# structure below is but not which fibre put it there, so the engine stamps an `A`
# raster beside `H` carrying the direction on top of each cell. These pin that it
# works AND that it agrees with the exact solver -- a preview that showed the split
# but got it backwards would be worse than one that ignored it.
# --------------------------------------------------------------------------- #
def _split_law(phi_j, phi_w):
    """(law, band) straight from the real model -- the pair gui_preview hands over."""
    from mew_physics import Calibration, MEWModel
    m = MEWModel(working_distance=4.0,
                 calibration=Calibration(fusion_overlap=phi_j,
                                         fusion_wall_overlap=phi_w))
    return m.make_fusion_law(temperature=90.0), m.fusion_wall_band()


def _drape_split(paths, phi_j, phi_w, d=_FD):
    law, band = _split_law(phi_j, phi_w)
    out, _ = ps.drape_sag([np.array(p) for p in paths], None, d,
                          sag_multiplier=1.0, fuse_sink=float(law(d)),
                          fuse_wall=float(law(d, True)), band=band)
    return max(p[:, 2].max() for p in out)


def _exact_split(paths, phi_j, phi_w, d=_FD):
    law, band = _split_law(phi_j, phi_w)
    g2f.set_fusion_law(law, band)
    try:
        out = g2f.apply_sag([np.array(p) for p in paths], d, sag_multiplier=1.0,
                            self_crossings=True)
    finally:
        g2f.set_fusion_law(None)
    return max(p[:, 2].max() for p in out)


def _wall_stack(n=8, d=_FD, npts=81, span=2.0):
    """A PURE wall: one straight line printed n times. Every contact collinear."""
    return [np.column_stack([np.linspace(0.0, span, npts),
                             np.full(npts, 1.0), np.full(npts, L * d)])
            for L in range(n)]


def _junction_tower(n=8, d=_FD, npts=81, span=2.0):
    """A PURE junction tower: +x / +y fibres crossing at 90 deg over one point."""
    out = []
    for L in range(n):
        s = np.linspace(0.0, span, npts)
        z = np.full(npts, L * d)
        out.append(np.column_stack([s, np.full(npts, 1.0), z]) if L % 2 == 0
                   else np.column_stack([np.full(npts, 1.0), s, z]))
    return out


def split_off_is_byte_identical_two_ways():
    """The split must be inert unless it says something: wall UNSET, and wall SET
    EQUAL to the junction phi, both reproduce the pre-split drape byte-for-byte. The
    second is the real guard -- it proves the `A` raster arms on the MAGNITUDES
    differing, not merely on a band being passed."""
    paths, _ = _fgrid(4)
    law, band = _split_law(0.35, None)
    base, _ = ps.drape_sag(paths, None, _FD, fuse_sink=float(law(_FD)))
    unset, _ = ps.drape_sag(paths, None, _FD, fuse_sink=float(law(_FD)),
                            fuse_wall=float(law(_FD, True)), band=band)
    law_e, band_e = _split_law(0.35, 0.35)
    equal, _ = ps.drape_sag(paths, None, _FD, fuse_sink=float(law_e(_FD)),
                            fuse_wall=float(law_e(_FD, True)), band=band_e)
    for i, (x, y) in enumerate(zip(base, unset)):
        assert np.array_equal(x, y), f"fibre {i} moved with the wall phi UNSET"
    for i, (x, y) in enumerate(zip(base, equal)):
        assert np.array_equal(x, y), f"fibre {i} moved with wall phi == junction phi"


def split_pure_wall_tracks_exact():
    """SELECTIVITY, half 1, against the exact solver: a collinear stack answers to
    phi_wall alone -- phi_junction at ANY value must not move it, in the PREVIEW as
    in the STL. The drape is the thing the user judges the calibration on, so it has
    to agree, not merely respond."""
    for phi_j, phi_w in [(0.35, 0.0), (0.9, 0.0), (0.35, 0.7), (0.0, 0.7)]:
        d = _drape_split(_wall_stack(), phi_j, phi_w)
        e = _exact_split(_wall_stack(), phi_j, phi_w)
        assert abs(d - e) < 1e-9, \
            f"phi_j={phi_j} phi_w={phi_w}: drape {d:.6f} != exact {e:.6f}"


def split_pure_junction_tracks_exact():
    """SELECTIVITY, half 2: a 90 deg tower answers to phi_junction alone -- phi_wall
    at ANY value must not move it, in the preview as in the STL."""
    for phi_j, phi_w in [(0.35, 0.0), (0.35, 0.9), (0.0, 0.9)]:
        d = _drape_split(_junction_tower(), phi_j, phi_w)
        e = _exact_split(_junction_tower(), phi_j, phi_w)
        assert abs(d - e) < 1e-9, \
            f"phi_j={phi_j} phi_w={phi_w}: drape {d:.6f} != exact {e:.6f}"


def split_wall_phi_is_monotone_in_the_preview():
    """More wall overlap -> a shorter wall in the preview, monotonically, and nothing
    is driven below the plate."""
    hs = [_drape_split(_wall_stack(), 0.35, p) for p in (0.0, 0.2, 0.4, 0.6, 0.9)]
    assert all(a > b for a, b in zip(hs, hs[1:])), hs
    law, band = _split_law(0.35, 0.9)
    out, _ = ps.drape_sag(_wall_stack(), None, _FD, fuse_sink=float(law(_FD)),
                          fuse_wall=float(law(_FD, True)), band=band)
    assert min(p[:, 2].min() for p in out) >= -1e-9


def _fusion_law_or_none(delta):
    """A law returning `delta` for our diameter, via the REAL physics seam when it is
    importable (so the test exercises the same closure the GUI installs)."""
    try:
        from mew_physics import Calibration, MEWModel
    except Exception:  # noqa: BLE001
        return None
    model = MEWModel(working_distance=4.0,
                     calibration=Calibration(fusion_overlap=delta / _FD))
    return model.make_fusion_law()


# --------------------------------------------------------------------------- #
# converter wiring -- the CLI, not the engine
# --------------------------------------------------------------------------- #

def _run_converter(tmpdir, gcode, flags):
    """Drive `gcode_to_fibers.main` on `gcode` with `flags`; return (paths, meta)
    read back from the `.depo` cache it writes."""
    import contextlib
    import io
    out = os.path.join(tmpdir, "out%s.stl" % abs(hash(tuple(flags))))
    argv = [gcode, "-o", out, "-d", "0.02", "--no-mesh"] + list(flags)
    with contextlib.redirect_stdout(io.StringIO()) as log:
        rc = g2f.main(argv)
    assert rc in (0, None), "converter exited %r" % (rc,)
    depo = os.path.splitext(out)[0] + g2f.DEPO_CACHE_EXT
    assert os.path.isfile(depo), "no .depo written for %r" % (flags,)
    with np.load(depo) as d:
        meta = np.asarray(d["meta"])
        z = np.asarray(d["xyz"])[:, 2]
    return z, meta, log.getvalue(), depo


def converter_wires_the_three_sag_modes():
    """`--sag-approx` reaches this engine, `--no-sag` still means straight, and
    the default is still the exact solve.

    The unit tests above all call `drape_sag` directly, so every one of them
    would stay green with the converter flag unwired -- this is the only case
    that fails if `main` stops routing to `apply_sag_approx`, or if it routes
    there by default."""
    import contextlib
    import io
    import shutil
    import tempfile

    import scaffold_gcode
    tmp = tempfile.mkdtemp(prefix="sagapprox_")
    try:
        gco = os.path.join(tmp, "s.gcode")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = scaffold_gcode.main(
                ["-o", gco, "--width", "6", "--height", "6", "--stepover", "0.5",
                 "--layers", "4", "-d", "0.02", "--speed", "600"])
        assert rc in (0, None)

        z_ex, m_ex, log_ex, _ = _run_converter(tmp, gco, [])
        z_ap, m_ap, log_ap, depo_ap = _run_converter(tmp, gco, ["--sag-approx"])
        z_off, m_off, _log, _ = _run_converter(tmp, gco, ["--no-sag"])

        # the log names the solver, so a silent fall-through is visible
        assert "APPROXIMATE solver" in log_ap, "approximate run did not say so"
        assert "APPROXIMATE" not in log_ex, "the DEFAULT run took the approx path"

        # both solvers sag; "off" does not (commanded Z is a handful of levels)
        assert len(np.unique(np.round(z_off, 6))) <= 8, "--no-sag still sagged"
        for name, z in (("exact", z_ex), ("approximate", z_ap)):
            assert len(np.unique(np.round(z, 6))) > 50, "%s did not sag" % name
        # ...and they agree closely without being the same array
        assert abs(z_ap.mean() - z_ex.mean()) < 0.1 * 0.02, \
            "approximate mean Z is more than 0.1 d from exact"
        assert z_ap.shape != z_ex.shape or not np.allclose(z_ap, z_ex), \
            "approximate output is identical to exact -- flag ignored?"

        # the cache meta must separate the three, or the player rides the wrong one.
        # Indices are POSITIVE and named: the meta grows by appending (see
        # mew_analysis._META_KEYS, "extra trailing keys are safe"), so `m[-1]`
        # silently re-points at the new field every time one is added -- it read
        # sag_approx until the cache generation was appended in 2026-08.
        I_NO_SAG, I_SAG_APPROX = 1, 9
        assert (m_ex[I_SAG_APPROX], m_ap[I_SAG_APPROX],
                m_off[I_SAG_APPROX]) == (0.0, 1.0, 0.0)
        assert (m_ex[I_NO_SAG], m_ap[I_NO_SAG], m_off[I_NO_SAG]) == (0.0, 0.0, 1.0)
        kw = dict(diameter=0.02, sag_multiplier=1.0)
        assert g2f.load_depo_cache(depo_ap, no_sag=False, sag_approx=True,
                                   **kw) is not None, \
            "an approximate consumer cannot load the approximate cache"
        assert g2f.load_depo_cache(depo_ap, no_sag=False, sag_approx=False,
                                   **kw) is None, \
            "an EXACT consumer accepted the approximate cache"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    check("bottom layer rests on bed", bottom_layer_rests_on_bed)
    check("approx engine does not shred a curved single layer",
          approx_engine_does_not_shred_a_curved_single_layer)
    check("crossing lifts one diameter", crossing_lifts_one_diameter)
    check("tight pore free-hangs sag_depth(span)", tight_pore_free_hangs)
    check("deposition order decides who rides", deposition_order_stacks)
    check("multiplier<=0 passes through", multiplier_zero_passthrough)
    check("times resampled, monotonic, endpoints kept",
          times_resampled_monotonic)
    check("simplify keeps peaks, thins flats", simplify_keeps_peaks)
    check("envelope vs exact apply_sag, 90 deg grid",
          envelope_matches_exact_90deg)
    check("envelope vs exact apply_sag, 60 deg triangle",
          envelope_matches_exact_60deg)
    check("fusion off is byte-identical", fusion_off_is_byte_identical)
    check("fusion leaves the bed layer alone", fusion_bottom_layer_does_not_fuse)
    check("wall/junction split off is byte-identical (unset AND equal)",
          split_off_is_byte_identical_two_ways)
    check("split: pure wall tracks exact (phi_wall only)",
          split_pure_wall_tracks_exact)
    check("split: pure junction tower tracks exact (phi_junction only)",
          split_pure_junction_tracks_exact)
    check("split: wall phi monotone in the preview, never below bed",
          split_wall_phi_is_monotone_in_the_preview)
    check("fusion reduces column slope, matching exact",
          fusion_reduces_column_slope_like_exact)
    check("converter wires --sag-approx / --no-sag / default",
          converter_wires_the_three_sag_modes)
    total = len(RAN)          # counted, not hand-kept: a literal here silently
    n_fail = len(FAILURES)    # under-reported for every test added since (12 vs 16)
    print(f"\n{'FAIL' if n_fail else 'PASS'}: {total - n_fail}/{total} tests passed")
    if n_fail:
        print("failed:", ", ".join(FAILURES))
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
