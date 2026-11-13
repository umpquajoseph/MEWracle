#!/usr/bin/env python3
"""test_move_gimbal.py -- the Move gimbal's geometry (move_gimbal.py).

Comparators, none written by the module under test:
  * the drag invariant: a point held by a PLANE handle (or the ball) stays
    under the cursor -- the moved origin, projected by the real VTK
    renderer, lands exactly where the mouse went; under an ARROW, the moved
    origin projects onto the cursor's foot on the arrow's screen line;
  * hand-made projections (an orthographic top view, an oblique one) for
    the hit test, where which handle is under a pixel can be read off by
    eye from the numbers.

RED proofs (in memory, each run with `--red`; every one must fail a case):
  axis_sign      `handle_point` returning origin - a*t: an arrow drag runs
                 the wrong way (all three "cursor's foot" cases).
  plane_normal   the "xy" square constrained by the X normal instead of Z:
                 its drag in the real window picks up Z.
  edge_on        `hit_handle` without the edge-on guard: a top view's Z
                 arrow (a dot on screen) is grabbed in the ball's place.
  arrow_first    an arrow winning anywhere within PICK_PX (no core): the
                 square crossed by an arrow reads as the arrow -- seen on
                 the page 2026-10-30, the XY square's middle grabbed Z.

    python tests/test_move_gimbal.py [--red]
"""
import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import math
import sys

import move_gimbal as mg

_FAILS = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name
          + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        _FAILS.append(name)


def top_view(scale=10.0):
    """Looking down -Z: world (x, y) -> pixels (x*scale, y*scale)."""
    return lambda p: (p[0] * scale, p[1] * scale)


def _basis(view=(-1.0, -1.3, 1.1)):
    """An orthonormal camera: right, up, and the direction it looks along
    (a real parallel camera's projection is a similarity, so the test's
    must be too)."""
    import numpy as np
    d = -np.asarray(view, float)
    d /= np.linalg.norm(d)
    r = np.cross(d, [0.0, 0.0, 1.0])
    r /= np.linalg.norm(r)
    u = np.cross(r, d)
    return r, u, d


def oblique(scale=10.0):
    """An oblique parallel view, z up on screen: world -> pixels."""
    r, u, _d = _basis()
    return lambda p: (scale * float(r @ p), scale * float(u @ p))


def hit_test_reads_the_handles():
    proj = top_view()
    o, L = (0.0, 0.0, 0.0), 10.0           # 100 px arrows
    check("the ball at the origin", mg.hit_handle(proj, o, L, 1, 1)
          == "center")
    check("the X arrow mid-shaft", mg.hit_handle(proj, o, L, 60, 2) == "x")
    check("the Y arrow mid-shaft", mg.hit_handle(proj, o, L, 2, 60) == "y")
    c = (PL := (mg.PLANE_LO + mg.PLANE_HI) / 2 * 100)
    check("the XY square's middle", mg.hit_handle(proj, o, L, c, PL)
          == "xy")
    check("the Z arrow seen end-on is never grabbed",
          mg.hit_handle(proj, o, L, 0, 0) == "center"
          and mg.hit_handle(proj, o, L, 0, 9) not in ("z",))
    check("the edge-on YZ and ZX squares are never grabbed",
          mg.hit_handle(proj, o, L, 0, 40) in ("y", None)
          and mg.hit_handle(proj, o, L, 40, 0) in ("x", None))
    check("empty space", mg.hit_handle(proj, o, L, -50, -50) is None)


def square_beats_a_crossing_arrow():
    """An oblique view where an arrow runs across a square on screen
    (found, not assumed): the square's middle is the square while the
    arrow is off it by more than its core; the arrow's own core is the
    arrow."""
    proj = oblique()
    o, L = (0.0, 0.0, 0.0), 10.0
    sh = mg.handle_shapes(o, L)
    found = None
    for k in mg.PLANES:
        q = [proj(p) for p in sh[k]]
        mid = (sum(p[0] for p in q) / 4, sum(p[1] for p in q) / 4)
        for a in mg.AXES:
            dist = mg._seg_dist(mid, proj(sh[a][0]), proj(sh[a][1]))
            if 3.0 < dist <= mg.PICK_PX:     # the shipped core, by value
                found = (k, a, mid, dist)
    check("this view has an arrow across a square", found is not None)
    if found:
        k, a, mid, dist = found
        got = mg.hit_handle(proj, o, L, *mid)
        check(f"the {k} square's middle reads as {k}, not {a} "
              f"({dist:.1f} px off it)", got == k, got)
    a, b = proj(sh["x"][0]), proj(sh["x"][1])
    on_x = (a[0] + 0.8 * (b[0] - a[0]), a[1] + 0.8 * (b[1] - a[1]))
    got = mg.hit_handle(proj, o, L, *on_x)
    check("a press on the X arrow (no square there) reads as X",
          got == "x", got)


def _ray_ortho(view):
    """The parallel ray under a pixel of `oblique()`: the point on the
    screen's plane through the origin, and the camera's direction."""
    r, u, d = _basis()
    s = 10.0

    def ray(x, y):
        p = r * (x / s) + u * (y / s)
        return tuple(p), tuple(d)
    return ray, tuple(d)


def drag_keeps_the_point_under_the_cursor():
    proj = oblique()
    ray, vd = _ray_ortho(proj)
    o = (1.0, 2.0, 0.5)
    for handle in ("xy", "yz", "zx", "center"):
        sx, sy = proj(o)
        r0 = ray(sx, sy)
        p0 = mg.handle_point(handle, o, *r0, view_dir=vd)
        r1 = ray(sx + 37.0, sy - 21.0)
        p1 = mg.handle_point(handle, o, *r1, view_dir=vd)
        if p0 is None or p1 is None:
            check(f"{handle}: a point under the cursor", False, "None")
            continue
        d = mg.constrain(handle, tuple(b - a for a, b in zip(p0, p1)))
        moved = proj(tuple(a + b for a, b in zip(o, d)))
        err = math.hypot(moved[0] - (sx + 37.0), moved[1] - (sy - 21.0))
        check(f"{handle}: the held point follows the cursor", err < 1e-6,
              f"{err:.3g} px off")
        if handle in mg.PLANES:
            n = mg.AXES[mg.PLANES[handle][2]]
            off = abs(sum(a * b for a, b in zip(d, n)))
            check(f"{handle}: the delta stays in its plane", off < 1e-9,
                  f"{off:.3g}")


def axis_drag_follows_the_cursor_foot():
    proj = oblique()
    ray, vd = _ray_ortho(proj)
    o = (0.0, 0.0, 0.0)
    for handle in ("x", "y", "z"):
        a = mg.AXES[handle]
        sx, sy = proj(o)
        ax = proj(a)
        ux, uy = ax[0] - sx, ax[1] - sy
        n = math.hypot(ux, uy)
        ux, uy = ux / n, uy / n
        # 50 px along the arrow's screen line, plus 30 px across it
        cx, cy = sx + 50 * ux - 30 * uy, sy + 50 * uy + 30 * ux
        p0 = mg.handle_point(handle, o, *ray(sx, sy), view_dir=vd)
        p1 = mg.handle_point(handle, o, *ray(cx, cy), view_dir=vd)
        d = mg.constrain(handle, tuple(b - c for c, b in zip(p0, p1)))
        moved = proj(d)
        foot = (sx + 50 * ux, sy + 50 * uy)
        err = math.hypot(moved[0] - foot[0], moved[1] - foot[1])
        check(f"{handle} arrow: the origin goes to the cursor's foot",
              err < 1e-6, f"{err:.3g} px off")
        off = math.sqrt(sum((d[i] - a[i] * sum(d[j] * a[j]
                                                for j in range(3))) ** 2
                            for i in range(3)))
        check(f"{handle} arrow: the delta is along {handle}", off < 1e-12,
              f"{off:.3g}")
    check("an axis along the line of sight gives no point",
          mg.handle_point("x", o, (0, 0, 0), (1.0, 0.0, 0.0)) is None)


def vtk_drag_round_trip():
    """The real class in a real offscreen window: drag the XY square 40 px
    right, 25 px up; the moved origin, projected by the scene renderer,
    is where the cursor went."""
    try:
        import vtk
    except Exception as exc:  # noqa: BLE001
        check("vtk available", False, str(exc))
        return
    win = vtk.vtkRenderWindow()
    win.SetOffScreenRendering(1)
    win.SetSize(640, 480)
    ren = vtk.vtkRenderer()
    win.AddRenderer(ren)
    cam = ren.GetActiveCamera()
    cam.ParallelProjectionOn()
    cam.SetFocalPoint(5, 5, 0)
    cam.SetPosition(-20, -25, 22)
    cam.SetViewUp(0, 0, 1)
    cam.SetParallelScale(12)
    g = mg.MoveGimbal(win, ren)
    g.show((5.0, 5.0, 0.0))
    win.Render()
    check("the gimbal is its arrow length in pixels",
          abs(g.length - mg.GIMBAL_PX * 24.0 / 480) < 1e-9, g.length)
    sx, sy = g._project(g.origin)
    sh = mg.handle_shapes(g.origin, g.length)
    q = [g._project(p) for p in sh["xy"]]
    mid = (sum(p[0] for p in q) / 4, sum(p[1] for p in q) / 4)
    check("the XY square is hit in the real window",
          g.hit(*mid) == "xy", g.hit(*mid))
    p0 = g.point("xy", *mid)
    p1 = g.point("xy", mid[0] + 40, mid[1] + 25)
    d = mg.constrain("xy", tuple(b - a for a, b in zip(p0, p1)))
    tx, ty = g._project(tuple(a + b for a, b in zip(g.origin, d)))
    err = math.hypot(tx - (sx + 40), ty - (sy + 25))
    check("XY drag in VTK: the origin lands under the cursor", err < 1e-3,
          f"{err:.4f} px")
    check("XY drag in VTK: no Z", abs(d[2]) < 1e-9, d)
    g.show(None)
    win.Render()
    check("hidden: nothing is hit", g.hit(*mid) is None)
    win.Finalize()


CASES = [hit_test_reads_the_handles, square_beats_a_crossing_arrow,
         drag_keeps_the_point_under_the_cursor,
         axis_drag_follows_the_cursor_foot, vtk_drag_round_trip]


def _run():
    for c in CASES:
        c()
    return list(_FAILS)


def _red():
    """Each injection must turn at least one case red."""
    real = {k: getattr(mg, k) for k in ("handle_point", "hit_handle",
                                        "PLANES", "ARROW_CORE_PX")}

    def axis_sign(handle, origin, ray_o, ray_d, view_dir=None):
        p = real["handle_point"](handle, origin, ray_o, ray_d, view_dir)
        if p is None or handle not in mg.AXES:
            return p
        return tuple(2 * o - v for o, v in zip(origin, p))

    def edge_on(project, origin, length, x, y, pick_px=mg.PICK_PX):
        shapes = mg.handle_shapes(origin, length)
        best = None
        for k in mg.AXES:
            a, b = project(shapes[k][0]), project(shapes[k][1])
            d = mg._seg_dist((x, y), a, b)
            if d <= pick_px and (best is None or d < best[0]):
                best = (d, k)
        return best[1] if best else real["hit_handle"](
            project, origin, length, x, y, pick_px)

    arms = {
        "axis_sign": lambda: setattr(mg, "handle_point", axis_sign),
        "plane_normal": lambda: setattr(mg, "PLANES", dict(
            real["PLANES"], xy=("x", "y", "x"))),
        "edge_on": lambda: setattr(mg, "hit_handle", edge_on),
        "arrow_first": lambda: setattr(mg, "ARROW_CORE_PX", 1e9),
    }
    bad = []
    for name, arm in arms.items():
        _FAILS.clear()
        arm()
        print(f"--- RED arm {name}")
        got = _run()
        for k, v in real.items():
            setattr(mg, k, v)
        print(f"--- {name}: {'RED' if got else 'STAYED GREEN'} {got}")
        if not got:
            bad.append(name)
    _FAILS.clear()
    return bad


if __name__ == "__main__":
    if "--red" in sys.argv:
        bad = _red()
        print("RED arms that stayed green:", bad or "none")
        sys.exit(1 if bad else 0)
    fails = _run()
    print(f"\n{len(fails)} failed" if fails else "\nall passed")
    sys.exit(1 if fails else 0)
