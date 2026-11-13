#!/usr/bin/env python3
"""make_tip_images.py -- render the pictures shown inside the modern UI's hover
tips and ⓘ popups (gui_tips.pic) into this folder.

Every picture is drawn from the ENGINE's own output, never freehand, so it shows
what the software actually does with the setting it explains:

  * the geometry pictures (stepover, layer rotation, turnaround arc radius,
    linking loops, per-layer angle pattern, stabilization base) plot the paths
    `gcode_to_fibers.parse_gcode` reads back out of `scaffold_gcode`'s g-code;
  * jet lag and CTS plot `mew_deposition.laid_fibers` -- the physics-deposited
    touchdown path -- against the commanded one, at the default MEWModel;
  * sag plots `gcode_to_fibers.apply_sag` on a small two-layer grid.

matplotlib is used here only, at build time; the app just loads the PNGs. The
backgrounds are transparent and the inks mid-tone, so one file reads on the
light tooltip and on the dark one.

Run from the repo root after changing any of the above:

    .venv/Scripts/python.exe assets/tips/make_tip_images.py
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)

import numpy as np                                        # noqa: E402
import matplotlib                                         # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                           # noqa: E402

import scaffold_gcode as sg                               # noqa: E402
import gcode_to_fibers as g2f                             # noqa: E402
from mew_physics import MEWModel                          # noqa: E402
from mew_deposition import laid_fibers                    # noqa: E402

# 330 px wide: inside the tooltip's 360 px wrap with its padding
W_IN, DPI = 3.3, 100
FIBER = "#3b73d9"           # the preview's blue family
FIBER2 = "#8fb0ea"          # a second layer
FIBER3 = "#1f4ea8"
REF = "#9aa0a6"             # the commanded path / a support
INK = "#8a8f96"             # captions and dimension lines
LAYER_INKS = ("#3b73d9", "#e0913a", "#3fa36b", "#b061c9")


# --------------------------------------------------------------------------- #
# engine calls
# --------------------------------------------------------------------------- #
def scaffold_paths(**kw):
    """The deposition polylines of one generated scaffold, as parse_gcode reads
    them back out of the g-code the generator wrote."""
    args = dict(width=3.0, height=3.0, stepover=0.6, rotation=90.0,
                fiber_diameter=0.02, layers=2, speed=600.0)
    args.update(kw)
    text = sg.generate_scaffold_gcode(**args)
    fd, path = tempfile.mkstemp(suffix=".gcode")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        return g2f.parse_gcode(path, mode="g1")
    finally:
        os.remove(path)


def by_layer(paths):
    """{z: [polylines]} in ascending z. A MEWron program is one continuous fiber,
    so a single polyline can climb through several layers: it is split wherever
    its z changes, and the climbing segment itself is dropped."""
    out = {}
    for p in paths:
        p = np.asarray(p)
        z = np.round(p[:, 2], 4)
        start = 0
        for i in range(1, len(p) + 1):
            if i == len(p) or z[i] != z[start]:
                if i - start >= 2:
                    out.setdefault(float(z[start]), []).append(p[start:i])
                start = i
    return dict(sorted(out.items()))


FAINT = dict(color=REF, alpha=0.45)


def _timed(points2d, speed):
    """(p (N,3), t (N,)) for a polyline drawn at constant `speed` (mm/s)."""
    p = np.array([[x, y, 0.0] for (x, y) in points2d], dtype=np.float64)
    seg = np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1)
    return p, np.concatenate([[0.0], np.cumsum(seg) / speed])


def _densify(corners, step=0.02):
    pts = []
    for a, b in zip(corners[:-1], corners[1:]):
        a, b = np.asarray(a, float), np.asarray(b, float)
        n = max(2, int(np.linalg.norm(b - a) / step))
        for s in np.linspace(0.0, 1.0, n, endpoint=False):
            pts.append(a + (b - a) * s)
    pts.append(np.asarray(corners[-1], float))
    return pts


# the default process the Physics page opens with (test_mew_deposition's knobs)
_P, _V, _T, _GAP = 8.0e4, 4.0, 90.0, 4.0


def _model():
    return MEWModel(working_distance=_GAP)


def _cts(model):
    return model.critical_speed(
        _V, model.working_distance,
        flow_rate=model.flow_rate(_P, _T), viscosity=model.viscosity(_T))


def laid(points2d, speed, **kw):
    p, t = _timed(points2d, speed)
    out, _t, info = laid_fibers([p], [t], _model(), pressure=_P, voltage=_V,
                                temperature=_T, **kw)
    return np.asarray(out[0]), info


# --------------------------------------------------------------------------- #
# drawing helpers
# --------------------------------------------------------------------------- #
def figure(h_in, ncols=1):
    fig, axes = plt.subplots(1, ncols, figsize=(W_IN, h_in), dpi=DPI)
    fig.patch.set_alpha(0.0)
    axes = np.atleast_1d(axes)
    for ax in axes:
        ax.set_aspect("equal")
        ax.axis("off")
        ax.patch.set_alpha(0.0)
    return fig, axes


def caption(ax, text):
    ax.set_title(text, fontsize=8, color=INK, pad=2)


def plot_xy(ax, paths, color, lw=1.4, **kw):
    for p in paths:
        p = np.asarray(p)
        ax.plot(p[:, 0], p[:, 1], color=color, lw=lw,
                solid_capstyle="round", **kw)


def save(fig, name):
    path = os.path.join(HERE, name + ".png")
    fig.subplots_adjust(left=0.02, right=0.98, top=0.9, bottom=0.03,
                        wspace=0.12)
    fig.savefig(path, dpi=DPI, transparent=True)
    plt.close(fig)
    print("wrote", os.path.relpath(path, REPO))


def crop(ax, x0, x1, y0, y1):
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)


# --------------------------------------------------------------------------- #
# the pictures
# --------------------------------------------------------------------------- #
def img_stepover():
    """Two layers of a square grid, with the stepover marked between two
    neighbouring fibers of the lower one: the stepover IS the pore."""
    s = 0.6
    layers = by_layer(scaffold_paths(stepover=s, arc_radius=0.0))
    zs = list(layers)
    fig, (ax,) = figure(1.55)
    plot_xy(ax, layers[zs[1]], FIBER2, lw=1.6)
    plot_xy(ax, layers[zs[0]], FIBER, lw=1.6)
    # layer 0 (0°) runs along X, so its passes are spaced in Y. The whole
    # layer is ONE serpentine polyline: its distinct y values are the passes.
    p0 = layers[zs[0]][0]
    run_y = sorted({round(float(y), 3) for y in p0[:, 1]})
    ya, yb = run_y[-3], run_y[-2]
    x = 3.3
    ax.annotate("", xy=(x, ya), xytext=(x, yb),
                arrowprops=dict(arrowstyle="<->", color=INK, lw=1.0,
                                shrinkA=0, shrinkB=0))
    for yy in (ya, yb):
        ax.plot([3.05, 3.4], [yy, yy], color=INK, lw=0.6)
    ax.text(x + 0.15, (ya + yb) / 2, "fiber spacing", ha="left", va="center",
            fontsize=8, color=INK)
    crop(ax, -0.1, 5.0, -0.1, 3.1)
    save(fig, "stepover")


def img_layer_rotation():
    fig, axes = figure(1.35, ncols=3)
    for ax, rot in zip(axes, (90.0, 60.0, 45.0)):
        n = int(round(180.0 / rot))
        layers = by_layer(scaffold_paths(
            rotation=rot, layers=n, stepover=0.5, arc_radius=0.0,
            width=3.0, height=3.0))
        for i, (_z, paths) in enumerate(layers.items()):
            plot_xy(ax, paths, LAYER_INKS[i % len(LAYER_INKS)], lw=1.1)
        crop(ax, -0.4, 3.4, -0.4, 3.4)
        caption(ax, "%g°" % rot)
    save(fig, "layer_rotation")


def img_arc_radius():
    """The turnaround at the end of each pass: a straight connector (0) against
    the default ballooning loop (1.5 × stepover). Cropped to the right-hand
    ends of the passes, where the turns are."""
    s = 0.5
    fig, axes = figure(1.5, ncols=2)
    for ax, mult, label in ((axes[0], 0.0, "Arc radius 0"),
                            (axes[1], 1.5, "1.5 × stepover (default)")):
        layers = by_layer(scaffold_paths(stepover=s, arc_radius=mult * s,
                                         layers=1, width=3.0, height=3.0))
        plot_xy(ax, next(iter(layers.values())), FIBER, lw=1.5)
        crop(ax, 1.4, 5.0, -0.5, 3.5)
        caption(ax, label)
    save(fig, "arc_radius")


def img_linking_loops():
    """Where one layer ends and the next begins at the same square corner: an
    in-place turn without linking loops, an outboard loop with them. The loop's
    radius IS the turnaround arc radius, so both keep the default one."""
    kw = dict(stepover=0.5, layers=2, width=2.0, height=2.0)
    off = scaffold_paths(link_loops="off", **kw)
    on = scaffold_paths(link_loops="manual", **kw)
    # where the two programs differ is the loop: centre the crop on it
    a = np.concatenate([np.asarray(p)[:, :2] for p in off])
    b = np.concatenate([np.asarray(p)[:, :2] for p in on])
    d = np.min(np.linalg.norm(b[:, None, :] - a[None, :, :], axis=2), axis=1)
    extra = b[d > 1e-3]
    assert len(extra), "linking loops changed nothing: no picture to draw"
    # zoomed on the corner the loop is added at; the rest of the part is faint
    # so the one change reads
    (x0, y0), (x1, y1) = extra.min(axis=0) - 0.6, extra.max(axis=0) + 0.6
    fig, axes = figure(1.6, ncols=2)
    for ax, paths, label in ((axes[0], off, "Off: turns in place"),
                             (axes[1], on, "On: loops outboard")):
        layers = by_layer(paths)
        for i, (_z, ps) in enumerate(layers.items()):
            plot_xy(ax, ps, (FIBER, "#e0913a")[i % 2], lw=1.5)
        crop(ax, x0, x1, y0, y1)
        caption(ax, label)
    save(fig, "linking_loops")


def img_angle_pattern():
    pattern = [0.0, 90.0, 45.0, 135.0]
    layers = by_layer(scaffold_paths(
        angle_pattern=pattern, layers=4, stepover=0.5, arc_radius=0.0,
        width=3.0, height=3.0))
    fig, axes = figure(1.3, ncols=4)
    shown = []
    for i, (ax, (_z, paths)) in enumerate(zip(axes, layers.items())):
        for ps in shown:
            plot_xy(ax, ps, lw=0.7, **FAINT)
        plot_xy(ax, paths, LAYER_INKS[i % 4], lw=1.2)
        shown.append(paths)
        crop(ax, -0.4, 3.4, -0.4, 3.4)
        caption(ax, "layer %d: %g°" % (i + 1, pattern[i]))
    save(fig, "angle_pattern")


def img_stabilization():
    stab = dict(width=4.0, height=4.0, stepover=0.25, fiber_diameter=0.02,
                layers=2, start_x=-0.5, start_y=-0.5, arc_radius=0.0)
    kw = dict(stepover=0.6, arc_radius=0.0, layers=2, width=3.0, height=3.0,
              start_x=0.0, start_y=0.0)
    both = np.concatenate([np.asarray(p) for p in
                           scaffold_paths(stabilization=stab, **kw)])
    alone = np.concatenate([np.asarray(p) for p in scaffold_paths(**kw)])
    # the base is printed FIRST in the same continuous fiber, so the program
    # with it is the base's points followed by the scaffold's own
    n = len(alone)
    base, top = [both[:len(both) - n + 1]], [both[len(both) - n:]]
    fig, (ax,) = figure(1.55)
    plot_xy(ax, base, lw=0.9, **FAINT)
    plot_xy(ax, top, FIBER, lw=1.6)
    ax.text(3.75, 3.5, "base, printed first", ha="left", va="center",
            fontsize=8, color=INK)
    ax.text(3.75, 1.5, "the scaffold", ha="left", va="center",
            fontsize=8, color=FIBER)
    crop(ax, -0.7, 7.2, -0.7, 3.7)
    save(fig, "stabilization")


def img_jet_lag():
    """A square laid at twice CTS: the fiber lands behind the nozzle, so it
    cuts every corner and trails the commanded path."""
    m = _model()
    cts = _cts(m)
    L = 10.0
    corners = [(0, 0), (L, 0), (L, L), (0, L), (0, 0)]
    pts = _densify(corners, step=0.05)
    speed = 2.0 * cts
    out, info = laid(pts, speed)
    fig, (ax,) = figure(1.6)
    c = np.asarray(corners, float)
    ax.plot(c[:, 0], c[:, 1], color=REF, lw=1.0, ls=(0, (3, 2)))
    ax.plot(out[:, 0], out[:, 1], color=FIBER, lw=1.6)
    ax.text(L + 1.0, L - 0.5, "g-code path", fontsize=8, color=INK,
            va="center")
    ax.text(L + 1.0, L - 2.4, "laid fiber", fontsize=8, color=FIBER,
            va="center")
    ax.text(L + 1.0, 0.5, "print speed\n= 2 × CTS", fontsize=8, color=INK,
            va="center")
    crop(ax, -1.0, L + 9.5, -1.0, L + 1.0)
    save(fig, "jet_lag")
    return info


def img_cts():
    m = _model()
    cts = _cts(m)
    pts = _densify([(0, 0), (6, 0)], step=0.01)
    slow, _ = laid(pts, 0.6 * cts, coil=True)
    fast, _ = laid(pts, 1.5 * cts, coil=True)
    fig, (ax,) = figure(1.25)
    ax.plot(slow[:, 0], slow[:, 1] + 1.2, color=FIBER, lw=1.1)
    ax.plot(fast[:, 0], fast[:, 1], color=FIBER, lw=1.4)
    ax.text(6.3, 1.2, "below CTS:\ncoils", fontsize=8, color=INK,
            va="center")
    ax.text(6.3, 0.0, "above CTS:\nstraight", fontsize=8, color=INK,
            va="center")
    crop(ax, -0.2, 8.6, -0.55, 1.75)
    save(fig, "cts")


def img_sag():
    """Side view of the top layer of a two-layer grid: each fiber droops
    between the crossings that hold it up. Heights are exaggerated to be seen."""
    # A span short enough that the droop stays in free air. At 1 mm the same
    # solver lets a 0.05 mm fiber come down to rest, which draws a flat-bottomed
    # span: true, but it pictures resting rather than droop.
    d = 0.1
    paths = scaffold_paths(stepover=0.4, fiber_diameter=d, layers=2,
                           width=2.0, height=2.0, arc_radius=0.0)
    sagged = g2f.apply_sag([np.asarray(p, float) for p in paths], d)
    sag_layers = by_layer(sagged)
    cmd_layers = by_layer(paths)
    zc = list(cmd_layers)
    # layer 0 runs along X (its passes sit at fixed y); layer 1 along Y. Take
    # the sagged points of layer 1 near one pass x, and view them side-on (y, z).
    top_cmd = cmd_layers[zc[-1]][0]
    xs = sorted({round(float(x), 3) for x in top_cmd[:, 0]})
    x_run = xs[len(xs) // 2]
    top_pts = np.concatenate([np.asarray(p) for p in sagged])
    run = top_pts[(np.abs(top_pts[:, 0] - x_run) < 1e-3)
                  & (top_pts[:, 2] > zc[0] + d * 0.75)]
    run = run[np.argsort(run[:, 1])]
    ys = sorted({round(float(y), 3) for y in cmd_layers[zc[0]][0][:, 1]})
    droop = float(run[:, 2].max() - run[:, 2].min())
    ex = max(1.0, round(0.35 / max(droop, 1e-6)))
    fig, axes = figure(1.2)
    ax = axes[0]
    ax.set_aspect("auto")
    from matplotlib.patches import Ellipse
    z_sup = float(np.median(np.concatenate(
        [p[:, 2] for p in sag_layers[min(sag_layers)]])))
    for y in ys:                        # the lower layer, seen end-on
        ax.add_patch(Ellipse((y, z_sup * ex), d, d * ex, color=REF, lw=0))
    # at its true thickness, so it is seen resting ON the supports
    ax.fill_between(run[:, 1], (run[:, 2] - d / 2) * ex,
                    (run[:, 2] + d / 2) * ex, color=FIBER, lw=0)
    ax.axhline(0.0, color=REF, lw=1.0)                # the plate
    ax.text(ys[-1], -d * ex * 0.25, "height × %g" % ex, ha="right",
            va="top", fontsize=8, color=INK)
    ax.set_xlim(ys[0] - 0.05, ys[-1] + 0.05)
    lo = -d * ex * 1.3
    hi = float(run[:, 2].max()) * ex + d * ex
    ax.set_ylim(lo, hi)
    save(fig, "sag")


ALL = (img_stepover, img_layer_rotation, img_arc_radius, img_linking_loops,
       img_angle_pattern, img_stabilization, img_jet_lag, img_cts, img_sag)


if __name__ == "__main__":
    want = set(sys.argv[1:])
    for fn in ALL:
        if not want or fn.__name__[4:] in want:
            fn()
