"""Below-CTS coiling-wave indicator -- the shared centreline reshape.

A deliberately simple visual indicator, NOT the physical sewing-machine coil (the
closed-form Bessel-J model behind the separate, dormant `coil` flag). Below CTS
the straight-fibre lag model does not hold, so the engines otherwise leave the
fibre as its straight commanded path -- which on screen reads as the physics
having stopped computing. This reshapes it into a tight lateral sine instead.

It is an INDICATOR, not a model: amplitude and wavelength are fixed USER knobs, so
the wave conserves neither the deposited arc length nor any coil clock. It lives in
ONE module so the batch engine and the live engine apply a BYTE-IDENTICAL wave and
cannot drift apart.

Geometry -- a perpendicular offset in the collector (XY) plane, Z unchanged:

    p'(s) = p(s) + amp * sin(2*pi * s / wavelength) * n_hat(s)

where s is arc length along the COMMANDED centreline and n_hat is the left
normal of the local XY tangent. The commanded polyline below CTS is often just
its two endpoints, so it is first resampled to >= _SAMPLES_PER_WAVELENGTH points
per wavelength for a smooth wave; the per-sample TIMES are interpolated by
arc-length fraction of the COMMANDED path (never the lengthened wave), so the
player's progressive reveal still tracks the real head position. The offset is
tapered to zero over the first/last half wavelength so consecutive serpentine
passes stay registered at their commanded endpoints -- no lateral seam where one
below-CTS pass meets the next.
"""
import numpy as np

# Points laid per wavelength when densifying the commanded centreline. 24 keeps a
# tight wave smooth without exploding the vertex count on a long slow pass.
_SAMPLES_PER_WAVELENGTH = 24


def coil_wave_reshape(xyz, times, amp, wavelength):
    """Reshape one below-CTS fibre centreline into the lateral coiling-wave
    indicator. Returns (xyz2 (M,3), times2 (M,)), M >= len(xyz).

    A no-op (returns the inputs as float64 arrays, unchanged) when disabled
    (amp <= 0 or wavelength <= 0), degenerate (< 2 points) or zero-length, so a
    caller can hand every below-CTS fibre through unconditionally."""
    xyz = np.asarray(xyz, dtype=np.float64)
    times = np.asarray(times, dtype=np.float64)
    if amp <= 0.0 or wavelength <= 0.0 or len(xyz) < 2:
        return xyz, times

    # cumulative arc length along the COMMANDED centreline
    seg = np.linalg.norm(np.diff(xyz, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(s[-1])
    if total <= 0.0:
        return xyz, times

    # resample by arc length -- dense enough for the wave, but keep every
    # commanded vertex exactly (so corners/turnarounds are not rounded away)
    n = int(np.ceil(total / wavelength * _SAMPLES_PER_WAVELENGTH)) + 1
    su = np.linspace(0.0, total, max(n, 2))
    su = np.unique(np.concatenate([s, su]))

    px = np.interp(su, s, xyz[:, 0])
    py = np.interp(su, s, xyz[:, 1])
    pz = np.interp(su, s, xyz[:, 2])
    tt = np.interp(su, s, times)
    base = np.column_stack([px, py, pz])

    # in-plane (XY) tangent -> left normal, from arc-length central differences
    tang = np.gradient(base, su, axis=0)
    tx, ty = tang[:, 0], tang[:, 1]
    tnorm = np.hypot(tx, ty)
    tnorm = np.where(tnorm > 1e-12, tnorm, 1.0)
    nx, ny = -ty / tnorm, tx / tnorm

    off = amp * np.sin(2.0 * np.pi * su / wavelength)
    # taper to 0 over the first/last half wavelength: anchors both endpoints so
    # neighbouring passes meet with no lateral jump, full amplitude in between
    edge = min(0.5 * wavelength, 0.5 * total)
    if edge > 0.0:
        off = off * np.clip(np.minimum(su, total - su) / edge, 0.0, 1.0)

    base[:, 0] += off * nx
    base[:, 1] += off * ny
    return base, tt


def below_cts_runs(feed, v_eff, seg_len, cts, rel_tol=1e-6):
    """Split one fibre's intervals into REGIME RUNS: `[(i0, i1, below), ...]`,
    interval indices `i0 <= i < i1`, covering the fibre in order.

    WHY RUNS. A fibre is one unbroken stretch of extrusion, and it can hold
    several commanded feeds -- a continuous 4-axis CTS ladder is ONE fibre
    from its fastest rung to its slowest. One verdict per fibre, from its mean
    speed, coiled every rung or none of them. A run is a maximal stretch of
    one commanded `feed` (and one `cts`); its verdict is its own length-
    weighted mean `v_eff` against CTS, which is exactly the whole-fibre test
    when the fibre has one feed. Neighbouring runs with the same verdict
    merge, so a fibre whose feeds all sit on one side of CTS comes back as ONE
    run and is laid exactly as before.

    Not per interval: `v_eff` dips below CTS at every decelerating corner,
    and a corner is not a coil. The feed is the commanded one (the naive
    clock's speed), so only a commanded change of regime splits a fibre.

    `cts` is a scalar or one value per interval (the live schedule can move
    it). A non-advancing interval (feed <= 0) joins the run it sits in."""
    feed = np.asarray(feed, dtype=np.float64)
    v_eff = np.asarray(v_eff, dtype=np.float64)
    seg_len = np.asarray(seg_len, dtype=np.float64)
    n = len(feed)
    if n == 0:
        return []
    cts = np.broadcast_to(np.asarray(cts, dtype=np.float64), (n,))

    def same(a, b):
        return abs(a - b) <= rel_tol * max(abs(a), abs(b), 1e-12)

    groups = []                               # [i0, i1, feed or None, cts]
    for i in range(n):
        f = feed[i]
        g = groups[-1] if groups else None
        if f <= 0.0:
            if g is not None:
                g[1] = i + 1
            else:
                groups.append([i, i + 1, None, cts[i]])
        elif g is not None and g[2] is None:  # a leading still stretch
            g[1], g[2], g[3] = i + 1, f, cts[i]
        elif g is not None and same(f, g[2]) and same(cts[i], g[3]):
            g[1] = i + 1
        else:
            groups.append([i, i + 1, f, cts[i]])

    runs = []
    for i0, i1, _f, c in groups:
        tot = float(seg_len[i0:i1].sum())
        if tot > 0.0 and c > 0.0:
            mean = float((v_eff[i0:i1] * seg_len[i0:i1]).sum() / tot)
            below = mean < c * (1.0 - 1e-9)
        else:
            below = runs[-1][2] if runs else None
        if runs and (below is None or runs[-1][2] is None
                     or runs[-1][2] == below):
            runs[-1][1] = i1
            if runs[-1][2] is None:
                runs[-1][2] = below
        else:
            runs.append([i0, i1, below])
    return [(i0, i1, bool(b)) for i0, i1, b in runs]


def overlay_wave_runs(xyz, times, p_cmd, t_cmd, runs, amp, wavelength):
    """Add the wave over each BELOW run of an already-laid fibre.

    `xyz`/`times` are the laid fibre; `p_cmd`/`t_cmd` its commanded centreline
    and `runs` its `below_cts_runs`. The wave's offset is taken off the
    COMMANDED run exactly as `coil_wave_reshape` draws it, then added to the
    laid path at the same instants -- so the lag the fast runs built relaxes
    through the slow run under the wave rather than jumping to the nozzle.
    The offset is tapered to zero at both ends of a run, so it joins the fast
    runs with no step. Both engines call this, so they wave identically.
    Returns (xyz2, times2); every input sample is kept, samples are added."""
    xyz = np.asarray(xyz, dtype=np.float64)
    times = np.asarray(times, dtype=np.float64)
    if amp <= 0.0 or wavelength <= 0.0:
        return xyz, times
    p_cmd = np.asarray(p_cmd, dtype=np.float64)
    t_cmd = np.asarray(t_cmd, dtype=np.float64)
    add_t, add_off = [], []
    off_t, off_v = [], []
    for i0, i1, below in runs:
        if not below:
            continue
        sp, st = p_cmd[i0:i1 + 1], t_cmd[i0:i1 + 1]
        wx, wt = coil_wave_reshape(sp, st, amp, wavelength)
        if len(wt) < 2 or not wt[-1] > wt[0]:
            continue
        base = np.column_stack([np.interp(wt, st, sp[:, k]) for k in range(3)])
        off = wx - base
        add_t.append(wt)
        add_off.append(off)
        off_t.append(wt)
        off_v.append(off)
    if not add_t:
        return xyz, times
    # the laid path at the wave's instants, plus the offset there
    new_t = np.concatenate(add_t)
    laid = np.column_stack([np.interp(new_t, times, xyz[:, k]) for k in range(3)])
    new_xyz = laid + np.concatenate(add_off)
    # a wave instant that coincides with a laid one is that laid point: keep
    # the laid sample (which gets the offset below), drop the duplicate
    fresh = ~np.isin(new_t, times)
    new_t, new_xyz = new_t[fresh], new_xyz[fresh]
    # ...and the offset at the laid path's own instants inside each run
    out = xyz.copy()
    for wt, off in zip(off_t, off_v):
        m = (times > wt[0]) & (times < wt[-1])
        if m.any():
            for k in range(2):
                out[m, k] += np.interp(times[m], wt, off[:, k])
    # stable: the laid samples come first, so their own order (a repeated
    # instant included) is untouched
    all_t = np.concatenate([times, new_t])
    order = np.argsort(all_t, kind="stable")
    return np.concatenate([out, new_xyz])[order], all_t[order]


__all__ = ["coil_wave_reshape", "below_cts_runs", "overlay_wave_runs"]
