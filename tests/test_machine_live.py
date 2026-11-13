#!/usr/bin/env python3
"""Self-running tests for gcode_player.py --machine-live (the machine-only live view).

Drives the player child exactly like gui_embed's EmbedSession does -- over real
pipes + shared memory, real VTK offscreen render -- reusing test_viewer_embed's
`Child` harness. A tiny temp machine.json (+ cube part STLs, like test_player_scene)
stands in for a real printer profile; there is NO g-code file.

Guards: the child comes up with has_transport=False / has_readout=True and streams a
non-blank frame; a {"t":"livepos"} message moves the machine and updates the readout
(and a second one moves it again -- the feed is actually consumed); the CLI contract
(--machine-live needs --machine; g-code still required without --machine-live).

Run:  python tests/test_machine_live.py   (PASS/FAIL per test, exit 1 on failure).
"""
import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from test_viewer_embed import Child  # reuse the embed parent-side harness

PLAYER = os.path.join(HERE, "viewers", "gcode_player.py")
FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"PASS  {name}")
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(name)
        print(f"FAIL  {name}: {exc!r}")


def _can_render():
    try:
        import vtk
        w = vtk.vtkRenderWindow()
        w.SetOffScreenRendering(1)
        w.AddRenderer(vtk.vtkRenderer())
        w.Render()
        w.Finalize()
        return True
    except Exception:  # noqa: BLE001
        return False


def _write_stl(path, tris):
    with open(path, "w") as f:
        f.write("solid s\n")
        for t in tris:
            f.write("facet normal 0 0 0\n outer loop\n")
            for v in t:
                f.write("  vertex %f %f %f\n" % tuple(v))
            f.write(" endloop\nendfacet\n")
        f.write("endsolid s\n")


def _box_tris(x0, x1, y0, y1, z0, z1):
    c = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6), (0, 4, 5), (0, 5, 1),
             (1, 5, 6), (1, 6, 2), (2, 6, 7), (2, 7, 3), (3, 7, 4), (3, 4, 0)]
    return [(c[a], c[b], c[d]) for a, b, d in faces]


def _machine_json(d):
    """A minimal but valid mewron machine profile: a small downward head cone (tip
    at z=0) over a bed with the print surface at z=0."""
    head = os.path.join(d, "head.stl")
    ring = [(2, 0, 10), (-2, 0, 10), (0, 2, 10), (0, -2, 10)]
    _write_stl(head, [(ring[i], ring[(i + 1) % 4], (0, 0, 0)) for i in range(4)])
    bed = os.path.join(d, "bed.stl")
    _write_stl(bed, _box_tris(-20, 70, -20, 70, -10, -6)
               + _box_tris(0, 50, 0, 50, -6, 0))
    prof = {"name": "m", "kinematics": "mewron",
            "parts": {"head": head, "bed": bed},
            "standoff": 2.0, "scale": 1.0, "slide_offset": [5.0, 7.0]}
    mj = os.path.join(d, "machine.json")
    json.dump(prof, open(mj, "w"))
    return mj


def _child(mj):
    return Child([sys.executable, PLAYER, "--machine-live", "--machine", mj])


def _wait_head_x(c, want, tol=0.5, timeout=20.0):
    """Wait for a `state` message whose rendered toolhead head X is within tol of
    `want` (the machine-live view exposes its interpolated head in the state msg)."""
    end = time.time() + timeout
    keep = {}
    while time.time() < end:
        msg = c.expect("state", timeout=3.0, keep=keep)
        head = msg.get("head")
        if head and abs(head[0] - want) <= tol:
            return head
    raise AssertionError(f"toolhead head X never reached {want}")


# -- CLI contract (no render needed) ---------------------------------------- #
def test_requires_machine():
    r = subprocess.run([sys.executable, PLAYER, "--machine-live"],
                       capture_output=True, text=True, cwd=HERE)
    assert r.returncode != 0, "machine-live with no --machine should fail"
    assert "requires --machine" in r.stderr, r.stderr[-300:]


def test_gcode_still_required_without_flag():
    r = subprocess.run([sys.executable, PLAYER],
                       capture_output=True, text=True, cwd=HERE)
    assert r.returncode != 0, "no g-code and no --machine-live should fail"


# -- live view (render) ----------------------------------------------------- #
def test_hello_and_frame():
    with tempfile.TemporaryDirectory() as d:
        c = _child(_machine_json(d))
        try:
            hello = c.expect("hello")
            assert hello.get("has_transport") is False, "should have NO transport"
            # the sidebar readout was removed for machine-live (the page form owns it)
            assert not hello.get("has_readout"), "machine-live should have no readout"
            frame = c.expect("frame", keep={})
            data = c.frame_bytes(frame)
            assert any(data), "streamed frame is blank"
            c.quit(0)
        finally:
            c.close()


def _stream(c, x, secs, hz=20):
    """Emulate the ~5 Hz poller (here faster) reporting a stationary toolhead at x,
    so the child's interpolation buffer has time-spread samples to render."""
    end = time.time() + secs
    while time.time() < end:
        c.send(t="livepos", x=x, y=0.0, z=0.0, state="printing", progress=0.5)
        time.sleep(1.0 / hz)


def test_livepos_moves_machine():
    with tempfile.TemporaryDirectory() as d:
        c = _child(_machine_json(d))
        try:
            c.expect("hello")
            _stream(c, 5.0, 0.35)          # feed reports X=5
            _wait_head_x(c, 5.0)
            _stream(c, 17.0, 0.5)          # feed moves to X=17 -> head follows
            _wait_head_x(c, 17.0, timeout=6.0)
            c.quit(0)
        finally:
            c.close()


def main():
    tests = [test_requires_machine, test_gcode_still_required_without_flag]
    if _can_render():
        tests += [test_hello_and_frame, test_livepos_moves_machine]
    else:
        print("SKIP  render tests (no VTK offscreen display)")
    for t in tests:
        check(t.__name__, t)
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
