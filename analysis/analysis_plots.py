#!/usr/bin/env python3
"""analysis_plots.py -- matplotlib figure builders.

The app calls one of them: `history_series_figure`, the Print History page's
trend chart. The rest (parity, residual, sweep, tornado, geometry overlay,
heatmap, micrograph overlay, `write_report`) are not called by the app; they
stay for scripts that run `mew_analysis` by hand.

Matplotlib is an OPTIONAL dependency, so this module guards its import and
exposes `HAVE_MPL`, and the GUI never calls a builder when it is False.

The builders are GUI-AGNOSTIC: each returns a bare `matplotlib.figure.Figure`, built
without pyplot, so there is no global state and no forced backend. The GUI embeds it
with `FigureCanvasTkAgg` and writes it with `save_figure`; figure construction needs no
display, so the same code works headless.

Inputs are the plain dicts the `mew_analysis` engine returns, so this module depends
only on `mew_analysis` (for labels) and matplotlib, never on Tk or VTK.
"""
from __future__ import annotations

import html
import json
import os

try:
    from matplotlib.figure import Figure
    import matplotlib
    HAVE_MPL = True
except Exception:  # noqa: BLE001 -- absence is a supported, graceful state
    HAVE_MPL = False

import numpy as np

import mew_analysis as A


def _dec(xy, cap=4000):
    """Stride-decimate a polyline for PLOTTING only — a deposited fibre carries ~10^5
    sag-resampled vertices but sub-pixel detail is invisible, so cap the line."""
    if len(xy) <= cap:
        return xy
    idx = np.linspace(0, len(xy) - 1, cap).astype(np.int64)
    return xy[idx]

# A small, print-friendly palette. Commanded = the build's deposit gold; predicted =
# a deep crimson; measured points = ink; calibration vs validation split by hue.
_GOLD = "#caa23a"
_CRIMSON = "#b3322c"
_INK = "#1a1a1a"
_CAL = "#3a7ca5"        # calibration points (fit on these)
_VAL = "#b3322c"        # validation points (held-out)
_REGIME_TINT = {"coiling": "#f2d9d6", "stable": "#e7f0e9",
                "overstretch": "#f0ead9"}

# Figure-save formats matplotlib gives us for the "Save figure..." dialog.
SAVE_FORMATS = (("PNG image", "*.png"), ("SVG vector", "*.svg"),
                ("PDF document", "*.pdf"))


def _require():
    if not HAVE_MPL:
        raise RuntimeError("matplotlib is not installed (optional analysis dep)")


def save_figure(fig, path) -> bool:
    """Write a figure to PNG/SVG/PDF (format inferred from the extension). Tight
    bounding box + 200 dpi so a raster export is paper-usable. False on error."""
    try:
        fig.savefig(path, dpi=200, bbox_inches="tight")
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Predicted vs measured: parity + residuals
# --------------------------------------------------------------------------- #
def parity_figure(parity, label="metric", unit=""):
    """A parity (predicted vs measured) scatter with the 1:1 line and the fit stats.
    Calibration and validation rows are drawn distinctly so a held-out point is
    visible. `parity` is a `mew_analysis.parity_for_measurements` result."""
    _require()
    fig = Figure(figsize=(4.6, 4.6), tight_layout=True)
    ax = fig.add_subplot(111)
    pairs = parity.get("pairs", [])
    if not pairs:
        ax.text(0.5, 0.5, "no measured points for this metric",
                ha="center", va="center", transform=ax.transAxes, color="#777")
        return fig
    meas = [p[0] for p in pairs]
    pred = [p[1] for p in pairs]
    for role, colour, name in ((("calibration",), _CAL, "calibration"),
                               (("validation",), _VAL, "validation"),
                               (("", None), _INK, "unlabelled")):
        xs = [p[0] for p in pairs if p[2] in role]
        ys = [p[1] for p in pairs if p[2] in role]
        if xs:
            ax.scatter(xs, ys, c=colour, s=36, edgecolor="white",
                       linewidth=0.6, zorder=3, label=name)
    lo = min(min(meas), min(pred))
    hi = max(max(meas), max(pred))
    pad = 0.08 * (hi - lo or 1.0)
    line = [lo - pad, hi + pad]
    ax.plot(line, line, "--", color="#888", lw=1.0, zorder=1, label="1:1")
    ax.set_xlim(line); ax.set_ylim(line)
    ax.set_aspect("equal", "box")
    u = f" ({unit})" if unit else ""
    ax.set_xlabel(f"Measured {label}{u}")
    ax.set_ylabel(f"Predicted {label}{u}")
    st = parity.get("stats", {})
    txt = _stats_caption(st)
    sv = parity.get("stats_validation")
    if sv and sv.get("n"):
        txt += f"\nvalidation: R²={_fmt(sv.get('r2'))}  RMSE={_fmt(sv.get('rmse'))}"
    ax.text(0.04, 0.96, txt, transform=ax.transAxes, va="top", ha="left",
            fontsize=8.5, family="monospace",
            bbox=dict(boxstyle="round", fc="white", ec="#ccc", alpha=0.9))
    ax.legend(loc="lower right", fontsize=8, framealpha=0.9)
    ax.set_title(f"Parity — {label}", fontsize=10)
    return fig


def residual_figure(parity, label="metric", unit=""):
    """Residual (predicted − measured) vs measured, with the zero line and an RMSE
    band. Reveals systematic bias the parity plot can hide."""
    _require()
    fig = Figure(figsize=(5.0, 3.4), tight_layout=True)
    ax = fig.add_subplot(111)
    pairs = parity.get("pairs", [])
    if not pairs:
        ax.text(0.5, 0.5, "no measured points", ha="center", va="center",
                transform=ax.transAxes, color="#777")
        return fig
    for role, colour in (("calibration", _CAL), ("validation", _VAL), ("", _INK)):
        xs = [p[0] for p in pairs if p[2] == role]
        rs = [p[1] - p[0] for p in pairs if p[2] == role]
        if xs:
            ax.scatter(xs, rs, c=colour, s=34, edgecolor="white", linewidth=0.6,
                       zorder=3, label=(role or "unlabelled"))
    ax.axhline(0.0, color="#888", lw=1.0, ls="--")
    st = parity.get("stats", {})
    if st.get("rmse") is not None:
        ax.axhspan(-st["rmse"], st["rmse"], color="#3a7ca5", alpha=0.08,
                   label="±RMSE")
    u = f" ({unit})" if unit else ""
    ax.set_xlabel(f"Measured {label}{u}")
    ax.set_ylabel(f"Residual{u}")
    ax.set_title(f"Residuals — {label}", fontsize=10)
    ax.legend(loc="best", fontsize=8, framealpha=0.9)
    return fig


# --------------------------------------------------------------------------- #
# Sweep: predicted curve + measured overlay + regime shading
# --------------------------------------------------------------------------- #
def sweep_figure(sweep, metric_key, measured=None):
    """A predicted curve for `metric_key` across the swept axis, with regime tinting
    and optional measured points overlaid. `sweep` is an `evaluate_sweep` result;
    `measured` is an optional list of (axis_value, measured_value)."""
    _require()
    fig = Figure(figsize=(5.4, 3.8), tight_layout=True)
    ax = fig.add_subplot(111)
    xs = sweep["values"]
    ys = sweep["metrics"].get(metric_key, [])
    # regime tint bands (contiguous runs of the same regime)
    _shade_regimes(ax, xs, sweep.get("regimes", []))
    fx = [x for x, y in zip(xs, ys) if y is not None]
    fy = [y for y in ys if y is not None]
    if fx:
        ax.plot(fx, fy, "-o", color=_CRIMSON, ms=4, lw=1.6, zorder=3,
                label="predicted")
    if measured:
        mx = [m[0] for m in measured]
        my = [m[1] for m in measured]
        ax.scatter(mx, my, c=_INK, s=40, marker="s", edgecolor="white",
                   linewidth=0.6, zorder=4, label="measured")
    axis_u = f" ({sweep['unit']})" if sweep.get("unit") else ""
    munit = sweep.get("metric_units", {}).get(metric_key, "")
    mlabel = sweep.get("labels", {}).get(metric_key, metric_key)
    ax.set_xlabel(f"{sweep['axis_label']}{axis_u}")
    ax.set_ylabel(f"{mlabel}{f' ({munit})' if munit else ''}")
    ax.set_title(f"{mlabel} vs {sweep['axis_label']}", fontsize=10)
    ax.legend(loc="best", fontsize=8, framealpha=0.9)
    ax.grid(True, alpha=0.25)
    return fig


def _shade_regimes(ax, xs, regimes):
    if not regimes or len(regimes) != len(xs):
        return
    seen = set()
    start = 0
    for i in range(1, len(xs) + 1):
        if i == len(xs) or regimes[i] != regimes[start]:
            reg = regimes[start]
            tint = _REGIME_TINT.get(reg)
            if tint:
                lab = reg if reg not in seen else None
                seen.add(reg)
                ax.axvspan(xs[start], xs[min(i, len(xs) - 1)], color=tint,
                           alpha=0.6, zorder=0, label=lab)
            start = i


# --------------------------------------------------------------------------- #
# Sensitivity: tornado
# --------------------------------------------------------------------------- #
def tornado_figure(sens, metric_key):
    """A tornado of the per-input elasticities for one metric, sorted by magnitude.
    At MEW conditions speed/pressure dominate while voltage/gap sit at ~0 -- the
    reduced-order scoping made visible. `sens` is a `sensitivity` result."""
    _require()
    fig = Figure(figsize=(5.2, 3.4), tight_layout=True)
    ax = fig.add_subplot(111)
    row = sens["metrics"].get(metric_key, {})
    items = [(a, row.get(a)) for a in sens["axes"] if row.get(a) is not None]
    items.sort(key=lambda kv: abs(kv[1]))
    if not items:
        ax.text(0.5, 0.5, "no sensitivity (metric not predictable here)",
                ha="center", va="center", transform=ax.transAxes, color="#777")
        return fig
    names = [A.AXIS_LABELS.get(a, (a, ""))[0] for a, _ in items]
    vals = [v for _, v in items]
    colours = [_CRIMSON if v < 0 else _CAL for v in vals]
    ax.barh(range(len(vals)), vals, color=colours, edgecolor="white")
    ax.set_yticks(range(len(vals)))
    ax.set_yticklabels(names)
    ax.axvline(0.0, color="#444", lw=1.0)
    ax.set_xlabel("Elasticity  d ln(output) / d ln(input)")
    label = sens["labels"].get(metric_key, metric_key)
    ax.set_title(f"What moves {label} (±{int(sens['frac']*100)}%)", fontsize=10)
    for i, v in enumerate(vals):
        ax.text(v, i, f" {v:+.2f}", va="center",
                ha="left" if v >= 0 else "right", fontsize=8)
    ax.margins(x=0.18)
    return fig


# --------------------------------------------------------------------------- #
# Predicted vs commanded: 2D geometry overlay
# --------------------------------------------------------------------------- #
def geometry_overlay_figure(commanded, predicted, digitized=None, max_fibers=400):
    """A top-down (XY) diff overlay: commanded centrelines in gold, the physics-
    deposited prediction in crimson. The shift between them is the jet-lag trail +
    corner rounding the metrics quantify. `digitized` (optional) overlays measured
    points clicked off a micrograph -- the hook for a future in-app digitiser, drawn
    here so it docks into the same view. Bundles are `GeometryBundle`s."""
    _require()
    fig = Figure(figsize=(5.6, 5.2), tight_layout=True)
    ax = fig.add_subplot(111)
    _draw_bundle(ax, commanded, _GOLD, "commanded", max_fibers)
    _draw_bundle(ax, predicted, _CRIMSON, "predicted", max_fibers)
    if digitized is not None and len(digitized):
        d = list(zip(*digitized))
        ax.scatter(d[0], d[1], c=_INK, s=18, marker="x", zorder=5,
                   label="measured")
    ax.set_aspect("equal", "box")
    ax.set_xlabel("X (mm)"); ax.set_ylabel("Y (mm)")
    ax.set_title("Commanded vs predicted (top-down)", fontsize=10)
    ax.legend(loc="upper right", fontsize=8, framealpha=0.9)
    ax.grid(True, alpha=0.2)
    return fig


def _draw_bundle(ax, bundle, colour, label, max_fibers):
    if bundle is None or not bundle.fibers:
        return
    fibers = bundle.fibers[:max_fibers]
    first = True
    for f in fibers:
        xy = _dec(f.xyz[:, :2])
        ax.plot(xy[:, 0], xy[:, 1], "-", color=colour, lw=0.9, alpha=0.85,
                label=label if first else None)
        first = False


def history_series_figure(series, label="value", unit=""):
    """A time series of one saved-run parameter vs each run's date/time -- the
    saved-log 'how has this changed across my runs' graph. `series` is a list of
    (datetime, value) as `mew_history.series_for_key` returns (already time-sorted).
    Points are drawn in time order with markers; a single point still shows as a dot;
    an empty series yields a 'no data' figure rather than raising."""
    _require()
    fig = Figure(figsize=(5.6, 3.4), tight_layout=True)
    ax = fig.add_subplot(111)
    if not series:
        ax.text(0.5, 0.5, "no numeric values for this parameter\n"
                "in the selected runs", ha="center", va="center",
                transform=ax.transAxes, color="#777")
        ax.set_axis_off()
        return fig
    xs = [t for t, _v in series]
    ys = [v for _t, v in series]
    ax.plot(xs, ys, "-o", color=_CRIMSON, ms=5, lw=1.6, zorder=3)
    u = f" ({unit})" if unit else ""
    ax.set_xlabel("Run date / time")
    ax.set_ylabel(f"{label}{u}")
    ax.set_title(f"{label} over time", fontsize=10)
    ax.grid(True, alpha=0.25)
    try:
        fig.autofmt_xdate()          # slant the date ticks so they don't collide
    except Exception:  # noqa: BLE001 -- cosmetic only
        pass
    return fig


def heatmap_figure(grid, contour_cts=True):
    """A process-map heatmap: one scalar metric over a 2D grid of two process axes.
    Invalid cells (None — e.g. diameter below CTS) render as gaps. With `contour_cts`
    a dashed line marks the v/CTS = 1 boundary when the speed-ratio is derivable from
    the regime field. `grid` is an `evaluate_grid` result."""
    _require()
    fig = Figure(figsize=(5.8, 4.4), tight_layout=True)
    ax = fig.add_subplot(111)
    X = np.asarray(grid["x_values"], float)
    Y = np.asarray(grid["y_values"], float)
    Z = np.array([[np.nan if v is None else v for v in row]
                  for row in grid["z"]], float)
    cmap = matplotlib.colormaps["viridis"].copy()   # modern registry (mpl >= 3.6)
    cmap.set_bad("#dddddd")
    pcm = ax.pcolormesh(X, Y, np.ma.masked_invalid(Z), shading="auto", cmap=cmap)
    cb = fig.colorbar(pcm, ax=ax)
    cb.set_label(f"{grid['metric_label']}"
                 + (f" ({grid['metric_unit']})" if grid['metric_unit'] else ""))
    # CTS boundary: contour the regime map where it flips coiling<->stable
    if contour_cts:
        reg = np.array([[0 if r == "coiling" else 1 for r in row]
                        for row in grid["regimes"]], float)
        if 0 < reg.sum() < reg.size:
            try:
                ax.contour(X, Y, reg, levels=[0.5], colors="white",
                           linewidths=1.2, linestyles="--")
            except Exception:  # noqa: BLE001 -- contour is a nicety, never fatal
                pass
    ax.set_xlabel(f"{grid['x_label']}"
                  + (f" ({grid['x_unit']})" if grid['x_unit'] else ""))
    ax.set_ylabel(f"{grid['y_label']}"
                  + (f" ({grid['y_unit']})" if grid['y_unit'] else ""))
    ax.set_title(f"{grid['metric_label']} over "
                 f"{grid['x_label']} × {grid['y_label']}", fontsize=10)
    return fig


def micrograph_overlay_figure(image, predicted=None, commanded=None,
                              transform=None, opacity=0.7, measure_lines=None,
                              scale_pts=None, max_fibers=400):
    """Overlay the predicted (and optionally commanded) geometry on a real top-down
    micrograph — the one image task ImageJ can't do, because it needs YOUR model.
    The image is drawn in PIXEL space at alpha=`opacity`; geometry is mapped mm->px
    by `transform` (a callable taking an (N,2) mm array, returning (N,2) px) and
    drawn over it. `measure_lines` are click-measured pixel segments; `scale_pts`
    are the two pixel points of the scale bar. Pure render — all interaction (clicks,
    nudge) lives in the GUI, which rebuilds `transform` and re-calls this."""
    _require()
    fig = Figure(figsize=(6.0, 5.4), tight_layout=True)
    ax = fig.add_subplot(111)
    img = np.asarray(image)
    h, w = img.shape[:2]
    ax.imshow(img, alpha=float(opacity), origin="upper",
              extent=[0, w, h, 0], zorder=0,
              cmap=(None if img.ndim == 3 else "gray"))
    if transform is not None:
        for bundle, colour, label in ((commanded, _GOLD, "commanded"),
                                      (predicted, _CRIMSON, "predicted")):
            if bundle is None:
                continue
            first = True
            for f in bundle.fibers[:max_fibers]:
                px = transform(_dec(f.xyz[:, :2]))
                ax.plot(px[:, 0], px[:, 1], "-", color=colour, lw=1.0, alpha=0.9,
                        zorder=2, label=label if first else None)
                first = False
    if scale_pts:
        (x0, y0), (x1, y1) = scale_pts
        ax.plot([x0, x1], [y0, y1], "-", color="#00d0ff", lw=2.0, zorder=4,
                label="scale")
    for seg in (measure_lines or []):
        (x0, y0), (x1, y1) = seg
        ax.plot([x0, x1], [y0, y1], "-", color="#1a1a1a", lw=1.4, zorder=4)
        ax.scatter([x0, x1], [y0, y1], c="#1a1a1a", s=12, zorder=5)
    ax.set_xlim(0, w); ax.set_ylim(h, 0)
    ax.set_xlabel("image X (px)"); ax.set_ylabel("image Y (px)")
    ax.set_title("Predicted overlay on micrograph", fontsize=10)
    handles, labels = ax.get_legend_handles_labels()
    if labels:
        ax.legend(loc="upper right", fontsize=8, framealpha=0.9)
    return fig


# --------------------------------------------------------------------------- #
# small caption helpers
# --------------------------------------------------------------------------- #
def _fmt(v, nd=4):
    if v is None:
        return "n/a"
    return f"{v:.{nd}g}"


def _stats_caption(st):
    return (f"n={st.get('n', 0)}\n"
            f"R²   = {_fmt(st.get('r2'))}\n"
            f"RMSE = {_fmt(st.get('rmse'))}\n"
            f"MAE  = {_fmt(st.get('mae'))}\n"
            f"bias = {_fmt(st.get('bias'))}\n"
            f"slope= {_fmt(st.get('slope'))}")


# --------------------------------------------------------------------------- #
# Report export -- a self-contained folder of figures + tables + an index.html
# --------------------------------------------------------------------------- #
def _safe(name):
    """Sanitise a report entry name into a filesystem-safe stem."""
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(name)) or "x"


def _report_html(title, items):
    esc = html.escape
    parts = ["<!doctype html>", "<meta charset='utf-8'>",
             f"<title>{esc(title)}</title>",
             "<style>body{font-family:system-ui,Arial,sans-serif;margin:2rem;"
             "color:#1c1e21}figure{display:inline-block;margin:0 1rem 1rem 0}"
             "img{border:1px solid #ddd}</style>",
             f"<h1>{esc(title)}</h1>"]
    figs = [it for it in items if it[0] == "figure"]
    if figs:
        parts.append("<h2>Figures</h2>")
        for _kind, name, fn in figs:
            parts.append(f"<figure><img src='{esc(fn)}' style='max-width:640px'>"
                         f"<figcaption>{esc(name)}</figcaption></figure>")
    others = [it for it in items if it[0] != "figure"]
    if others:
        parts.append("<h2>Tables &amp; data</h2><ul>")
        for kind, name, fn in others:
            parts.append(f"<li><a href='{esc(fn)}'>{esc(name)}</a> "
                         f"<em>({esc(kind)})</em></li>")
        parts.append("</ul>")
    return "\n".join(parts) + "\n"


def write_report(folder, figures=None, tables=None, profile=None,
                 title="MEWracle analysis report"):
    """Assemble a self-contained report folder: each Figure -> PNG, each table
    (name -> CSV text) -> .csv, the calibration profile -> JSON, and an index.html
    linking them all. Works WITHOUT matplotlib (the figures dict is then empty; the
    tables + profile + HTML are still written). None-valued figures/tables are
    skipped so a caller can pass whatever it currently has. Returns the index path."""
    os.makedirs(folder, exist_ok=True)
    items = []
    for name, fig in (figures or {}).items():
        if fig is None:
            continue
        fn = _safe(name) + ".png"
        if save_figure(fig, os.path.join(folder, fn)):
            items.append(("figure", name, fn))
    for name, text in (tables or {}).items():
        if not text:
            continue
        fn = _safe(name) + ".csv"
        try:
            with open(os.path.join(folder, fn), "w", newline="") as fh:
                fh.write(text)
            items.append(("table", name, fn))
        except OSError:
            pass
    if profile:
        try:
            with open(os.path.join(folder, "calibration_profile.json"), "w") as fh:
                json.dump(profile, fh, indent=2)
            items.append(("data", "Calibration profile",
                          "calibration_profile.json"))
        except OSError:
            pass
    index = os.path.join(folder, "index.html")
    with open(index, "w") as fh:
        fh.write(_report_html(title, items))
    return index


__all__ = [
    "HAVE_MPL", "SAVE_FORMATS", "save_figure",
    "parity_figure", "residual_figure", "sweep_figure", "tornado_figure",
    "geometry_overlay_figure", "heatmap_figure", "history_series_figure",
    "micrograph_overlay_figure", "write_report",
]
