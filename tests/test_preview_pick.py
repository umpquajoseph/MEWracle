#!/usr/bin/env python3
"""test_preview_pick.py -- click-to-select in the preview (`preview_pick` +
`PreviewPanel.pick` / `pick_at` / `set_layer_highlight` / `on_pick`), on the
G-code page's own panel in a real, SHOWN ModernApp: the picture is rendered,
so the depth buffer the pick reads is the real one.

The picture is built with physics and sag off, so the drawn fiber IS the
commanded path and every comparator below has an answer of its own:

  visible      the fiber picked is the one SEEN at that pixel. Comparator: VTK's
               own depth read and unprojection at the click (`ren.GetZ` +
               `DisplayToWorld`, the orbit pivot's code) -- a point on a tube's
               surface, one radius from the centreline that drew it. The pick
               must land within that radius (plus 2 px of slack), at top-down,
               isometric and low oblique views, zoomed in far enough that the
               tubes are many pixels wide. A lower layer under the same pixel
               is a fiber diameter further away and fails it.
  line         the line returned laid the fiber clicked. Comparator: the
               player's parse of the text (`line_clock` verts + seg_line): the
               picked point lies on that line's own commanded segment (XY).
  layer        the layer returned holds that line. Comparator: the document's
               render keys (`RLine.key`), read directly, not via the outline.
  highlight    `set_layer_highlight(first, last)` lights exactly that layer's
               fiber: every lit point is on a commanded segment of a line in
               [first, last], and the lit length matches the layer's commanded
               deposition length.
  click        a real <Button-1>/<ButtonRelease-1> pair fires `on_pick` with a
               hit and leaves the camera where it was; a 40 px drag orbits and
               fires nothing; with `on_pick` None (the Scaffold designer) a
               press-release orbits as before (no pick, no error).
  zoomed-out   default framing, tubes on and then OFF (1 px centrelines): a
               click near a drawn fiber still resolves, and to a correct line
               and layer.

RED proofs (`--red <name>` injects the defect in memory; each turned its
case FAIL, the clean run is the GREEN control):
  depth   pick_segment with no visibility test and the FARTHEST candidate
          winning -> `visible` FAIL.
  line    line_at_time answering the next segment's line -> `line` FAIL.
  layer   layer_at_line returning the 0-based layer -> `layer` FAIL.
  slop    _CLICK_SLOP_PX = -1 (every click is a drag) -> `click` FAIL.

    ./.venv/Scripts/python.exe tests/test_preview_pick.py [--red depth|line|layer|slop]

SKIPS with no Tk display. Bounded: every wait has a deadline."""
import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import math
import os
import sys
import time

try:
    import tkinter as tk
except Exception:  # noqa: BLE001
    print("SKIP: tkinter unavailable")
    sys.exit(0)

import numpy as np

_FAILS = []
_M = {}
RED = sys.argv[sys.argv.index("--red") + 1] if "--red" in sys.argv else None


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  -- {detail}" if detail else ""))
    if not cond:
        _FAILS.append(name)


def pump(sec, until=None):
    root = _M["root"]
    t = time.time()
    while time.time() - t < sec:
        root.update()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until is not None else True


def inject(name):
    import gui_preview
    import preview_pick as pp
    if name == "depth":
        def bad(index, M, w, h, x, y, zread, radius_px=6.0, depth_eps=0.0,
                surface_tol=None):
            P, seg, starts = index["P"], index["seg"], index["starts"]
            sx, sy, sz = pp.project(P, M, w, h)
            ax, ay, bx, by = sx[seg], sy[seg], sx[seg + 1], sy[seg + 1]
            dx, dy = bx - ax, by - ay
            L2 = np.maximum(dx * dx + dy * dy, 1e-300)
            t = np.clip(((x - ax) * dx + (y - ay) * dy) / L2, 0, 1)
            dist = np.hypot(ax + t * dx - x, ay + t * dy - y)
            ok = np.flatnonzero(dist <= radius_px)
            if not len(ok):
                return None
            depth = sz[seg[ok]] + t[ok] * (sz[seg[ok] + 1] - sz[seg[ok]])
            j = ok[np.argmax(depth)]
            g = int(seg[j])
            path = int(np.searchsorted(starts, g, side="right") - 1)
            return dict(path=path, seg=int(g - starts[path]), t=float(t[j]),
                        point=P[g] + t[j] * (P[g + 1] - P[g]),
                        dist_px=float(dist[j]))
        pp.pick_segment = bad
    elif name == "line":
        real = pp.line_at_time

        def bad(clock, tt):
            sl = np.asarray(clock["seg_line"])
            n = real(clock, tt)
            j = int(np.searchsorted(sl, n, side="right"))
            return int(sl[min(j, len(sl) - 1)])
        pp.line_at_time = bad
    elif name == "layer":
        real = pp.layer_at_line

        def bad(nodes, line):
            got = real(nodes, line)
            if got and got["layer"]:
                got["layer"] -= 1
            return got
        pp.layer_at_line = bad
    elif name == "slop":
        gui_preview._CLICK_SLOP_PX = -1


# --------------------------------------------------------------------------- #
def boot():
    import gui_fonts
    import mewracle_gui
    root = tk.Tk()
    # SHOWN, so the picture renders and its depth buffer is real -- but off
    # the desktop and fully transparent, so a run never flashes a window up
    # over the user's work or takes the taskbar
    try:
        root.attributes("-alpha", 0.0)
        root.attributes("-toolwindow", True)
    except tk.TclError:
        pass
    gui_fonts.init_fonts(root)
    app = mewracle_gui.ModernApp(root)
    root.geometry("1500x950+-4000+-4000")
    root.update()
    _M.update(root=root, app=app)
    app._nav_select("gcode")
    ok = pump(90, lambda: bool(app.gce.get_text())
              and app._gce_regen is None
              and getattr(app, "_gce_model", None) is not None)
    if not ok:
        return False
    # the picture with physics and sag off: the fiber IS the commanded path
    orig = app._pv_snapshot_options
    app._pv_snapshot_options = lambda i: dict(orig(i), physics=None,
                                              sag=False)
    old = getattr(app, "_gce_hl", None)
    app._gce_pv_schedule(now=True)
    ok = pump(120, lambda: getattr(app, "_gce_hl", None) is not old
              and app._gce_pv_panel._state is not None
              and app._gce_pv_panel._win is not None)
    panel = app._gce_pv_panel
    m = app._gce_model
    # what the page will do (the page's side of the seam)
    panel.pick_outline = lambda: (m.outline(m.render()), app.gce.get_text())
    _M.update(panel=panel, model=m)
    return ok and panel._state["meta"].get("line_clock") is not None


def _geometry():
    st = _M["panel"]._state
    P = np.concatenate(st["paths"], axis=0)
    meta = st["meta"]
    d = float(meta.get("tube_diameter", meta.get("fiber_diameter")) or 0.02)
    return st, P, d


def _aim(direction, up, scale, at=None):
    """Point the camera at `at` (default: the top of the scaffold's middle)
    from `direction`, parallel scale `scale` mm, and render."""
    panel = _M["panel"]
    _st, P, _d = _geometry()
    if at is None:
        # the middle of the scaffold's own points (the lead-in from the
        # machine origin stretches the bbox off it), on the top layer
        top = P[P[:, 2] >= P[:, 2].max() - 1e-6]
        c = np.median(top, axis=0)
        at = top[np.argmin(np.linalg.norm(top - c, axis=1))]
    cam = panel._ren.GetActiveCamera()
    v = np.asarray(direction, float)
    v /= np.linalg.norm(v)
    cam.SetFocalPoint(*at)
    cam.SetPosition(*(np.asarray(at) + 200.0 * v))
    cam.SetViewUp(*up)
    cam.SetParallelScale(scale)
    panel._update_display_mode()
    panel._fit_clipping()
    panel._render()
    _M["root"].update()


def _surface(x, y):
    """VTK's own world point under Tk pixel (x, y), or None on background."""
    panel = _M["panel"]
    ren, h = panel._ren, panel._win.GetSize()[1]
    z = ren.GetZ(int(x), int(h - y))
    if not 0.0 < z < 1.0:
        return None
    ren.SetDisplayPoint(int(x), int(h - y), z)
    ren.DisplayToWorld()
    w = ren.GetWorldPoint()
    return np.array(w[:3]) / w[3]


def _seg_dist(q, A, B):
    ab = B - A
    s = np.clip(np.einsum("ij,ij->i", q - A, ab)
                / np.maximum(np.einsum("ij,ij->i", ab, ab), 1e-30), 0, 1)
    return np.linalg.norm(q - (A + s[:, None] * ab), axis=1)


def _segments():
    st = _M["panel"]._state
    A = np.concatenate([p[:-1] for p in st["paths"] if len(p) > 1])
    B = np.concatenate([p[1:] for p in st["paths"] if len(p) > 1])
    return A, B


def _line_ok(hit, res):
    """(on_its_line, layer_ok): the comparators `line` and `layer`."""
    c = _M["panel"]._state["meta"]["line_clock"]
    v, sl = np.asarray(c["verts"]), np.asarray(c["seg_line"])
    j = np.flatnonzero(sl == res["line"])
    if not len(j):
        return False, False
    dxy = _seg_dist(np.repeat(hit["point"][None, :2], len(j), 0),
                    v[j, :2], v[j + 1, :2]).min()
    r = _M["model"].render()
    key = r.lines[res["line"] - 1].key
    layer_ok = (bool(key) and key[1] == "body" and key[3] + 1 == res["layer"]
                and res["first"] <= res["line"] <= res["last"])
    return dxy < 1e-4, layer_ok


# --------------------------------------------------------------------------- #
def picks_at_views():
    """`visible`, `line`, `layer` at three views, zoomed in."""
    panel = _M["panel"]
    st, P, d = _geometry()
    views = [("top-down", (0, 0, 1), (0, 1, 0)),
             ("isometric", (1, -1, 1.2), (0, 0, 1)),
             ("low oblique", (0.3, -1, 0.35), (0, 0, 1))]
    import gui_preview
    vtk = gui_preview._VTK
    for name, direction, up in views:
        _aim(direction, up, scale=0.35)
        w, h = panel._win.GetSize()
        wpp = 2.0 * 0.35 / h
        check(f"{name}: drawn as tubes, the mode this comparator needs",
              panel._state.get("display_mode") == "tubes")
        # VTK's own ray cast against the drawn tubes: the fiber surface the
        # ray from this pixel meets first (the plate is not in its list)
        picker = vtk.vtkCellPicker()
        picker.SetTolerance(0.0)
        picker.PickFromListOn()
        picker.AddPickList(panel._actor)
        tol = 0.5 * d / math.cos(math.pi / 6) + 2 * wpp
        tried = vis_bad = line_bad = layer_bad = 0
        worst = 0.0
        for y in range(h // 2 - 150, h // 2 + 150, 11):
            for x in range(w // 2 - 150, w // 2 + 150, 11):
                if not picker.Pick(x, h - y, 0, panel._ren):
                    continue
                s = np.array(picker.GetPickPosition())
                # ...and only where that surface is what the pixel SHOWS: the
                # lead-in from the origin is drawn at z = 0, half inside the
                # plate, and the ray finds it where the plate covers it
                seen = _surface(x, y)
                if seen is None or np.linalg.norm(seen - s) > 2 * wpp:
                    continue
                hit = panel.pick(x, y)
                res = panel.pick_at(x, y)
                tried += 1
                if hit is None or res is None:
                    vis_bad += 1
                    continue
                # the seen surface lies on the PICKED fiber's own tube: within
                # a 6-gon tube's corner radius (r / cos 30) of its centreline
                q = st["paths"][hit["path"]]
                e = float(_seg_dist(s[None], q[hit["seg"]][None],
                                    q[hit["seg"] + 1][None])[0])
                worst = max(worst, e)
                if e > tol:
                    vis_bad += 1
                on_line, layer_ok = _line_ok(hit, res)
                line_bad += not on_line
                layer_bad += not layer_ok
        check(f"visible ({name}): the pick is the fiber seen at the pixel",
              tried >= 30 and vis_bad == 0,
              f"{tried} pixels, {vis_bad} wrong, worst {worst*1000:.1f} um "
              f"vs {tol*1000:.1f} um")
        check(f"line ({name}): the line returned laid the clicked fiber",
              tried >= 30 and line_bad == 0, f"{line_bad}/{tried} off")
        check(f"layer ({name}): the layer returned holds that line",
              tried >= 30 and layer_bad == 0, f"{layer_bad}/{tried} off")


def highlight_is_the_layer():
    panel = _M["panel"]
    _aim((1, -1, 1.2), (0, 0, 1), scale=0.35)
    w, h = panel._win.GetSize()
    res = None
    for dx in range(0, 80, 4):
        res = panel.pick_at(w // 2 + dx, h // 2)
        if res and res.get("layer"):
            break
    check("highlight: a layer was picked to light", bool(res and
                                                         res.get("layer")))
    if not res:
        return
    panel.set_layer_highlight(res["first"], res["last"])
    polys = panel._hl["data"][0]
    c = panel._state["meta"]["line_clock"]
    v, sl = np.asarray(c["verts"]), np.asarray(c["seg_line"])
    j = np.flatnonzero((sl >= res["first"]) & (sl <= res["last"]))
    lit = np.concatenate(polys) if polys else np.zeros((0, 3))
    off = (_seg_dist(lit[:, None, :2].repeat(len(j), 1).reshape(-1, 2),
                     np.tile(v[j, :2], (len(lit), 1)),
                     np.tile(v[j + 1, :2], (len(lit), 1)))
           .reshape(len(lit), len(j)).min(axis=1).max() if len(lit) else 1)
    lit_len = sum(float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum())
                  for p in polys)
    seg_len = np.linalg.norm(v[j + 1] - v[j], axis=1)
    # the drawn fiber is the deposition moves; the layer's travels are not:
    # the player's own is_dep, from its parse of the same text
    import tempfile
    import gcode_player
    fd, tmp = tempfile.mkstemp(suffix=".gcode")
    with os.fdopen(fd, "w") as fh:
        fh.write(c["text"])
    _v, _t, is_dep, *_rest = gcode_player.parse_path(tmp)
    os.remove(tmp)
    dep = np.asarray(is_dep)
    cmd_len = float(seg_len[dep[np.minimum(j, len(dep) - 1)]].sum())
    check("highlight: every lit point is on the layer's own lines",
          len(lit) > 0 and off < 1e-4, f"worst {off:.2e} mm")
    check("highlight: the lit length is the layer's commanded length",
          abs(lit_len - cmd_len) <= 0.02 * cmd_len + 3 * 0.02,
          f"lit {lit_len:.2f} mm vs commanded {cmd_len:.2f} mm "
          f"({res['label']})")
    panel.set_layer_highlight(None, None)
    check("highlight: (None, None) clears",
          not panel._hl["data"][0])


def clicks_and_drags():
    import gui_preview
    panel, root = _M["panel"], _M["root"]
    _aim((0, 0, 1), (0, 1, 0), scale=0.35)
    w, h = panel._win.GetSize()
    host = panel._host
    got = []
    panel.on_pick = got.append
    # find a pixel that picks something
    x = y = None
    for dx in range(0, 120, 3):
        if panel.pick_at(w // 2 + dx, h // 2):
            x, y = w // 2 + dx, h // 2
            break
    cam = panel._ren.GetActiveCamera()
    before = (cam.GetPosition(), cam.GetViewUp())
    if x is not None:
        host.event_generate("<Button-1>", x=x, y=y)
        host.event_generate("<B1-Motion>", x=x + 2, y=y + 1)
        host.event_generate("<ButtonRelease-1>", x=x + 2, y=y + 1)
        root.update()
    after = (cam.GetPosition(), cam.GetViewUp())
    check("click: a press-release fires on_pick with the layer",
          len(got) == 1 and got[0] is not None and got[0].get("layer"),
          f"{got}")
    check("click: the camera did not move under the click",
          np.allclose(before[0], after[0]) and np.allclose(before[1], after[1]))
    got.clear()
    host.event_generate("<Button-1>", x=w // 2, y=h // 2)
    for k in range(1, 9):
        host.event_generate("<B1-Motion>", x=w // 2 + 5 * k, y=h // 2)
    host.event_generate("<ButtonRelease-1>", x=w // 2 + 40, y=h // 2)
    root.update()
    moved = not np.allclose(before[0], cam.GetPosition())
    check("click: a 40 px drag orbits and picks nothing",
          not got and moved, f"picked {len(got)}, moved {moved}")
    # empty space: far off the scaffold
    got.clear()
    _aim((0, 0, 1), (0, 1, 0), scale=0.35,
         at=np.r_[_geometry()[1].max(axis=0)[:2] + 5.0, 0.0])
    host.event_generate("<Button-1>", x=w // 2, y=h // 2)
    host.event_generate("<ButtonRelease-1>", x=w // 2, y=h // 2)
    root.update()
    check("click: on empty space on_pick gets None",
          got == [None], f"{got}")
    # the designer's posture: no on_pick, Button-1 is the orbit it was
    panel.on_pick = None
    _aim((0, 0, 1), (0, 1, 0), scale=0.35)
    before = cam.GetPosition()
    host.event_generate("<Button-1>", x=w // 2, y=h // 2)
    host.event_generate("<B1-Motion>", x=w // 2 + 3, y=h // 2)
    host.event_generate("<ButtonRelease-1>", x=w // 2 + 3, y=h // 2)
    root.update()
    check("click: with on_pick None a 3 px drag still orbits (designer)",
          not np.allclose(before, cam.GetPosition())
          and panel._click0 is None)
    _ = gui_preview


def zoomed_out_lines():
    """Run twice: the tubes switch on (the default) and OFF, the 1 px
    centrelines a user gets by unticking "Draw fibers as 3-D tubes"."""
    app = _M["app"]
    was = app.pv_show_tubes.get()
    try:
        for tubes in (True, False):
            app.pv_show_tubes.set(tubes)
            _zoomed_out(tubes)
    finally:
        app.pv_show_tubes.set(was)
        _M["root"].update()


def _zoomed_out(tubes):
    panel = _M["panel"]
    panel.reset_view()
    panel._update_display_mode(force=True)
    panel._fit_clipping()
    panel._render()
    _M["root"].update()
    w, h = panel._win.GetSize()
    ren = panel._ren
    n = bad = 0
    for y in range(h // 2 - 120, h // 2 + 120, 17):
        for x in range(w // 2 - 120, w // 2 + 120, 17):
            hit = panel.pick(x, y)
            res = panel.pick_at(x, y)
            if hit is None or res is None:
                continue
            n += 1
            on_line, layer_ok = _line_ok(hit, res)
            bad += not (on_line and layer_ok)
    mode = panel._state.get("display_mode")
    check(f"zoomed-out ({'tubes' if tubes else 'lines'}): drawn that way",
          mode == ("tubes" if tubes else "lines"), f"mode {mode}")
    check(f"zoomed-out ({mode}): clicks resolve, each to its own line and "
          "layer", n >= 20 and bad == 0, f"{n} picks, {bad} wrong")
    _ = ren


def pick_cost():
    panel = _M["panel"]
    w, h = panel._win.GetSize()
    t = time.perf_counter()
    for _ in range(5):
        panel.pick_at(w // 2, h // 2)
    ms = (time.perf_counter() - t) / 5 * 1000
    n = sum(len(p) for p in panel._state["paths"])
    print(f"  NOTE  one pick_at: {ms:.0f} ms over {n} drawn points")


def main():
    if RED:
        inject(RED)
        print(f"RED: {RED} injected")
    try:
        ok = boot()
    except tk.TclError as exc:
        print(f"SKIP: no display ({exc})")
        return 0
    check("boot: the G-code page drew a picture with a line clock", ok)
    if ok:
        for case in (picks_at_views, highlight_is_the_layer, clicks_and_drags,
                     zoomed_out_lines, pick_cost):
            print(case.__name__)
            try:
                case()
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(f"{case.__name__} raised", False, repr(exc))
    try:
        _M["root"].destroy()
    except Exception:  # noqa: BLE001
        pass
    print("FAIL" if _FAILS else "PASS", f"({len(_FAILS)} failed)")
    return 1 if _FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
