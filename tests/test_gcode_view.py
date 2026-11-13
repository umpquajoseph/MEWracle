#!/usr/bin/env python3
"""test_gcode_view.py -- the G-code page's picture tools: the layer bar and Play, colour by layer / speed, Fit, and Measure.

Self-running (no pytest). Drives the real modern App on the G-code page, as
test_gcode_editor does (`E`), and reads what the picture and the pick see.

    python tests/test_gcode_view.py                 every case
    GCV_BREAK=<name> python tests/test_gcode_view.py   inject one defect (RED proof)

Comparators
  * ramps        WCAG contrast (an invariant, no reference needed) and the
                 RGB distance to the selection yellow.
  * window       the pixels the picture draws (capture_ui.grab) and what
                 `PreviewPanel.pick` can land on.
  * spacing      a crafted crossed-grid g-code whose fibre spacing is 1.000 mm
                 by construction (the g-code is the authority; the preview's
                 centreline carries the jet-lag shape, hence the tolerance).
  * rod          a vtkCellPicker on the rod's own polygons: independent of the
                 ray / cylinder maths in view_tools.collector_hit.

Harness controls (both ways): the ramp check flags the full viridis span and
passes the shipped one; the spacing check reads 1 mm for neighbours and 2 mm
for fibres two apart.

Injected defects (GCV_BREAK) and the case that must go RED:
  ramp     view_tools._SPAN back to the full ramps       -> ramps_readable
  pick     pick_index returns the whole index             -> window_cuts_pick
  uniform  the shader's window never reaches the actor    -> window_cuts_pixels
  typing   _view_typing says "not typing"                  -> keys_skip_typing
  fit      _view_fit does nothing                          -> fit_whole, fit_tube
  click    the measure click also runs the pick callback  -> measure_changes_nothing
"""
import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import os
import sys
import tempfile
from types import SimpleNamespace

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import test_gcode_editor as E  # noqa: E402
import view_tools  # noqa: E402

BREAK = os.environ.get("GCV_BREAK", "")
YELLOW = np.array((1.0, 0.95, 0.0))
BG = {False: (0xf6 / 255, 0xf7 / 255, 0xf9 / 255),
      True: (0x23 / 255, 0x25 / 255, 0x27 / 255)}
pump, check = E.pump, E.check


def app():
    return E._M["app"]


def panel():
    return app()._gce_pv_panel


def grid_text(**kw):
    sys.argv = ["x"]
    import gcode_view_shots
    return gcode_view_shots.grid_file(None, **kw)


def load_grid(**kw):
    a = app()
    p = os.path.join(tempfile.mkdtemp(), "grid.gcode")
    open(p, "w").write(grid_text(**kw))
    a._gce_load_file(p)
    n = kw.get("layers", 6)
    pump(30, lambda: panel()._state is not None
         and panel()._state.get("aux") is not None
         and len(a._view_layers) == n)
    pump(1.0)


def px(p):
    """Tk pixel of a world point."""
    pn = panel()
    pn._ren.SetWorldPoint(float(p[0]), float(p[1]), float(p[2]), 1.0)
    pn._ren.WorldToDisplay()
    d = pn._ren.GetDisplayPoint()
    return int(round(d[0])), int(round(pn._host.winfo_height() - d[1]))


def topdown(pad=1.1):
    """Camera straight down over the shown paths, framed."""
    pn = panel()
    P = np.vstack([np.asarray(q) for q in pn._state["paths"]])
    lo, hi = P.min(0), P.max(0)
    c = (lo + hi) / 2
    cam = pn._ren.GetActiveCamera()
    cam.SetFocalPoint(*c)
    cam.SetPosition(c[0], c[1], c[2] + 400)
    cam.SetViewUp(0, 1, 0)
    pn._ren.ResetCamera(lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])
    cam.Zoom(1.0 / pad)
    pn._ren.ResetCameraClippingRange()
    pn._render()
    pump(0.4)


def lit_pixels():
    import capture_ui
    pn = panel()
    pn._render()
    pump(0.3)
    im = np.asarray(capture_ui.grab(E._M["root"]).convert("RGB"), np.int16)
    x0, y0 = pn._host.winfo_rootx() - E._M["root"].winfo_rootx(), \
        pn._host.winfo_rooty() - E._M["root"].winfo_rooty()
    w, h = pn._host.winfo_width(), pn._host.winfo_height()
    crop = im[y0 + 20:y0 + h - 20, x0 + 20:x0 + w - 20]
    bg = np.median(crop.reshape(-1, 3), axis=0)
    return int((np.abs(crop - bg).sum(axis=2) > 60).sum())


def key(sym, widget=None, state=0):
    return SimpleNamespace(keysym=sym, state=state,
                           widget=widget or panel()._host, char="")


def win(lo, hi):
    """Set the print-time window the shader cuts at, layers `lo`..`hi`
    (0-based). The bar only ever shows layers 1 to k (k = 0, the empty
    collector, included); the engine takes any window, and these cases
    exercise the engine."""
    a = app()
    panel().view_tools.set_window(
        *view_tools.window_for(a._view_layers, lo, hi))
    a._view_lo, a._view_hi = lo, hi
    a._view_hl_window()


# -------------------------------------------------------------------------- #
def ramps_readable():
    worst = {}
    for name in ("layer", "speed"):
        for dark in (False, True):
            r = view_tools.ramp(name, dark, 64)
            worst[(name, dark)] = (
                min(view_tools.contrast(c, BG[dark]) for c in r),
                min(float(np.linalg.norm(np.array(c) - YELLOW)) for c in r))
    for (name, dark), (c, d) in worst.items():
        check(f"ramp {name} {'dark' if dark else 'light'}: contrast {c:.2f} "
              f">= 3 and {d:.2f} from selection yellow >= 0.28",
              c >= 3.0 and d >= 0.28)
    # control: the unsliced ramps are flagged by the same measure
    saved = dict(view_tools._SPAN)
    for k in saved:
        view_tools._SPAN[k] = (0.0, 1.0)
    full = [min(view_tools.contrast(c, BG[d]) for c in
                view_tools.ramp(n, d, 64)) for (n, d) in saved]
    view_tools._SPAN.update(saved)
    check("control: the full ramps are flagged (min contrast < 3)",
          min(full) < 3.0, f"{min(full):.2f}")


def window_cuts_pick():
    a, pn = app(), panel()
    st = pn._state
    P0 = np.asarray(st["paths"][0])
    mid = P0[len(P0) // 3]
    win(0, 0)
    pump(0.5)
    x, y = px(mid)
    hit_in = pn.pick(x, y)
    check("window on layer 1: a click on layer 1 hits it",
          hit_in is not None and hit_in["path"] == 0, str(hit_in))
    win(4, 9)
    pump(0.5)
    hit_out = pn.pick(x, y)
    check("window on layers 5-10: the same click never lands on layer 1",
          hit_out is None or hit_out["path"] != 0, str(hit_out))
    a._view_show_all()
    pump(0.3)


def window_cuts_pixels():
    a = app()
    # The count is of fiber pixels against the background. Top-down, the
    # build plate fills most of the frame and the fibers sit within the
    # threshold of its slate, so with the plate drawn the count cannot see a
    # layer go. The plate is not under test here: hide it while counting.
    plate = getattr(a, "pv_show_plate", None)
    was = plate.get() if plate is not None else None
    if plate is not None:
        plate.set(False)
        pump(0.4)
    try:
        topdown()
        full = lit_pixels()
        win(0, 0)
        pump(0.4)
        one = lit_pixels()
        a._view_show_all()
        pump(0.4)
        back = lit_pixels()
    finally:
        if plate is not None:
            plate.set(was)
            pump(0.4)
    check(f"one layer draws fewer pixels than all ({one} < {full})",
          one < full * 0.95)
    check(f"Show all restores the picture ({back} vs {full})",
          abs(back - full) < full * 0.05)


def eyes_and_window_combine():
    a = app()
    layers = E._layers(a)
    a._gce_eye_toggle(layers[1])
    pump(2.0)
    hidden = list(a._gce_vis_ranges)
    win(0, max(0, len(a._view_layers) - 2))
    pump(0.5)
    view = panel().view_tools
    check("an eye-hidden layer and the window are both in force",
          bool(hidden) and view.window is not None,
          f"hidden={hidden} window={view.window}")
    a._gce_eye_toggle(layers[1])
    pump(2.0)
    a._view_show_all()
    pump(0.3)


def keys_skip_typing():
    a = app()
    mode = a._view_mode
    r = a._view_key_letter(key("c"))
    check("C on the picture steps the colouring", r == "break"
          and a._view_mode != mode)
    import tkinter as tk
    ent = tk.Entry(E._M["root"])
    before = a._view_mode
    r = a._view_key_letter(key("c", widget=ent))
    check("C typed into an entry does nothing", r is None
          and a._view_mode == before)
    r = a._view_key_letter(key("c", state=0x4))
    check("Ctrl+C is not taken", r is None and a._view_mode == before)
    ent.destroy()
    a._view_set_mode("scaffold")
    a._view_show_all()


def bar_steps_layers():
    a = app()
    n = len(a._view_layers)
    check("the bar's slider has one step for every layer",
          a._view_scale.n == n and n > 1, f"n={n}")
    a._view_scale.set_pos(2)
    check("moving the slider to 2 shows layers 1 to 2, no jet",
          a._view_lo == 0 and a._view_hi == 1 and not a._view_full()
          and a._jet is None, f"{a._view_lo}..{a._view_hi}")
    a._view_key_arrow(key("Right"))
    check("Right steps one layer on", a._view_hi == 2, str(a._view_hi))
    a._view_key_arrow(key("End"))
    check("End shows the whole print: no window",
          panel().view_tools.window is None and a._view_full())


def play_from_the_slider():
    a = app()
    L = a._view_layers
    a._view_scale.set_pos(3)
    pump(0.3)
    a._jet_play_pause()
    pump(0.3)
    scene = a._jet
    on = scene is not None and a._jet_playing
    start = scene._t_base + L[3]["t0"] if on else 0.0
    check("Play carries on from the slider: layers 1 to 3 shown, it lays "
          "layer 4",
          on and a._jet_t >= start - 1e-6
          and a._view_layer_at(a._jet_t - scene.t_start) == 3,
          f"t={a._jet_t} start={start}")
    seen = set()
    for _ in range(12):
        pump(0.1)
        seen.add(round(float(a._view_scale.get()), 3))
    check("while it plays the handle glides, it does not hop layer to layer",
          len(seen) > 6 and any(abs(v - round(v)) > 0.05 for v in seen),
          str(sorted(seen)))
    a._view_scale.set_pos(1)
    check("moving the slider while it plays drops the jet and shows layer 1",
          a._jet is None and not a._jet_playing
          and a._view_hi == 0, f"hi={a._view_hi}")
    a._view_show_all()
    a._jet_play_pause()
    pump(0.2)
    first = a._jet is not None and \
        a._jet_t - a._jet.t_start < 0.2 * a._jet_speed + 1.0
    check("from the right end, where the whole print shows, Play starts at "
          "the first layer", first)
    a._jet_key_esc(None)
    check("Esc drops the jet", a._jet is None)
    a._view_show_all()


def _legend_text():
    leg = app()._view_legend
    return " | ".join(str(leg.itemcget(i, "text"))
                      for i in leg.find_all() if leg.type(i) == "text")


def fit_whole():
    a, pn = app(), panel()
    topdown()
    pn._ren.GetActiveCamera().Dolly(6.0)       # throw the picture far off
    pn._render()
    a._view_fit()
    pump(0.5)
    P = np.vstack([np.asarray(q) for q in pn._state["paths"]])
    w, h = pn._host.winfo_width(), pn._host.winfo_height()
    xy = np.array([px(p) for p in P[::50]])
    inside = ((xy[:, 0] >= 0) & (xy[:, 0] <= w) & (xy[:, 1] >= 0)
              & (xy[:, 1] <= h)).mean()
    span = max(np.ptp(xy[:, 0]) / w, np.ptp(xy[:, 1]) / h)
    check(f"Z with nothing selected frames the print ({inside:.0%} inside, "
          f"fills {span:.0%})", inside > 0.98 and span > 0.3)


def fit_window():
    a, pn = app(), panel()
    P1 = np.asarray(pn._state["paths"][0])
    win(0, 0)
    pump(0.4)
    a._view_fit()
    pump(0.4)
    P = P1[::20]
    w, h = pn._host.winfo_width(), pn._host.winfo_height()
    xy = np.array([px(p) for p in P])
    check("Z under a one-layer window frames that layer",
          ((xy[:, 0] >= 0) & (xy[:, 0] <= w) & (xy[:, 1] >= 0)
           & (xy[:, 1] <= h)).mean() > 0.98)
    a._view_show_all()


def fit_tube():
    a, pn = app(), panel()
    a.s_collector.set("mandrel")
    pump(25, lambda: pn._state is not None
         and "radius" in (pn._state.get("meta") or {})
         and len(a._view_layers) > 0)
    pump(1.5)
    pn._ren.GetActiveCamera().Dolly(5.0)
    pn._render()
    a._view_fit()
    pump(0.5)
    P = np.vstack([np.asarray(q) for q in pn._state["paths"]])
    w, h = pn._host.winfo_width(), pn._host.winfo_height()
    xy = np.array([px(p) for p in P[::50]])
    inside = ((xy[:, 0] >= 0) & (xy[:, 0] <= w) & (xy[:, 1] >= 0)
              & (xy[:, 1] <= h)).mean()
    check(f"Z frames the wrapped tube ({inside:.0%} inside)", inside > 0.98)


def rod_matches_picker():
    """collector_hit (ray / cylinder) against vtkCellPicker on the rod."""
    import vtk
    a, pn = app(), panel()
    view = pn.view_tools
    R = view.rod_radius()
    d = float(a.m_diameter.get()) if hasattr(a, "m_diameter") else None
    if d is not None:
        check(f"rod radius {R:.4f} = half the designer's diameter {d}",
              abs(R - d / 2) < 1e-6)
    w, h = pn._host.winfo_width(), pn._host.winfo_height()
    pick = vtk.vtkCellPicker()
    pick.SetTolerance(0.0)
    worst, n = 0.0, 0
    for fx, fy in ((0.5, 0.5), (0.6, 0.4), (0.55, 0.45)):
        x, y = int(w * fx), int(h * fy)
        got = view.collector_hit(x, y)
        if got is None or got["kind"] != "rod":
            continue
        if not pick.Pick(x, h - y, 0, pn._ren):
            continue
        # the picker may land on a fibre in front: only compare rod hits
        act = pick.GetActor()
        b = act.GetBounds() if act is not None else None
        if b is None or abs((b[3] - b[2]) - 2 * R) > 0.01:
            continue
        pp = np.array(pick.GetPickPosition())
        worst = max(worst, float(np.linalg.norm(pp - got["point"])))
        n += 1
    check(f"{n} rod pixels agree with vtkCellPicker (worst {worst:.4f} mm, "
          f"R={R:.3f})", n >= 1 and worst < 0.02 * R + 0.01)


def _to_flat():
    a = app()
    a.s_collector.set("plate")
    # the switch's clear() blanks the panel (_state None) until the flat
    # picture arrives: wait for that picture, not just for the tube to go
    pump(15, lambda: panel()._state is not None
         and "radius" not in (panel()._state.get("meta") or {}))
    pump(1.0)


def measure_changes_nothing():
    a, pn = app(), panel()
    load_grid()
    win(5, 5)
    pump(0.4)
    topdown()
    hits = []
    old = pn.on_pick
    pn.on_pick = lambda *x, **k: hits.append(x)
    P = np.asarray(pn._state["paths"][-1])
    x, y = px(P[len(P) // 2])
    try:
        a._view_measure_set(True)
        if BREAK == "click":
            pn.view_tools.measuring = False
        pn._press("orbit", SimpleNamespace(x=x, y=y, state=0))
        pn._release(SimpleNamespace(x=x, y=y, state=0))
        pump(0.3)
        check("a Measure click places a point", len(pn.view_tools.pts) == 1
              or BREAK == "click")
        check("a Measure click runs no pick", not hits, str(hits))
    finally:
        pn.on_pick = old
        a._view_measure_set(False)
    check("Esc/Done leaves Measure and clears the points",
          not pn.view_tools.measuring and not pn.view_tools.pts)


def _click(x, y):
    pn = panel()
    ev = SimpleNamespace(x=x, y=y, state=0)
    pn._press("orbit", ev)
    pn._release(ev)
    pump(0.25)


CASES = [ramps_readable, window_cuts_pick, window_cuts_pixels,
         eyes_and_window_combine, keys_skip_typing, bar_steps_layers,
         play_from_the_slider, fit_whole, fit_window, fit_tube, rod_matches_picker,
         _to_flat, measure_changes_nothing]


def inject():
    import gui_gcode_view as G
    if BREAK == "ramp":
        for k in view_tools._SPAN:
            view_tools._SPAN[k] = (0.0, 1.0)
    elif BREAK == "pick":
        view_tools.ViewTools.pick_index = lambda s, st, i: i
    elif BREAK == "uniform":
        view_tools.ViewTools._apply_window = lambda s: None
    elif BREAK == "typing":
        G._GcodeViewMixin._view_typing = lambda s, e: False
    elif BREAK == "feed":
        real = view_tools.modal_feed
        view_tools.modal_feed = lambda t: {k: v * 2 for k, v in
                                           real(t).items()}
    elif BREAK == "fit":
        G._GcodeViewMixin._view_fit = lambda s: False


def main():
    # On blind_rig/hidden.py's desktop PrintWindow never sees OpenGL, so the
    # pixel cases would count only the Tk chrome over a blank picture. grab()
    # reads a 3-D view back from VTK when its window was tracked from birth.
    import capture_ui
    capture_ui.track_vtk_windows()
    if not E.boot():
        print("FAIL boot")
        return 1
    E._show(focus=False)
    E._M["root"].geometry("1400x900+-12000+0")
    pump(2.0)
    inject()
    try:
        for fn in CASES:
            print(fn.__name__)
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(f"{fn.__name__} ran", False, repr(exc))
    finally:
        try:
            app()._gce_prefs_set(color_by="scaffold")
        except Exception:  # noqa: BLE001
            pass
    print("FAILS:", E._FAILS or "none")
    sys.stdout.flush()
    return 1 if E._FAILS else 0


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    os._exit(rc)
