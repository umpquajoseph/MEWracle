#!/usr/bin/env python3
"""test_viewer_embed.py -- MODERN-UI ONLY: self-running tests for the embedded
viewers. Comparator: the child-process protocol driven exactly the way
`gui_embed`'s EmbedSession drives it, over real pipes and real shared memory with
real VTK offscreen rendering -- not a stub of either side.

Run:  python tests/test_viewer_embed.py   (PASS/FAIL per test, exit 1 on failure).
Headless: no window ever opens.

What it guards:
  * hello/controls/footers arrive and match the viewer (stl toggles, player
    playback+view sections, live's Reset params + knob key legend)
  * frames stream through shared memory (correct size, non-blank)
  * a button command flips its label; forwarded mouse drag/wheel re-render;
    forwarded keys act (space pauses); scrub moves the clock
  * ANIMATION RATE: the locked player must publish >= 45 fps at panel size --
    a publish cap mis-set against the 16 ms tick halves the stream to ~30 fps,
    and the Windows 15.6 ms timer resolution quantizes the loop
  * clean exit on quit (rc 0), non-zero exit on a broken launch (the crash
    state the GUI panel shows instead of dying)
"""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from multiprocessing import shared_memory

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

MAXW, MAXH = 800, 600
SLOT = MAXW * MAXH * 3
import perf_budget
# Locked-player publish rate at 640x480, as measured on the desktop. Scaled
# for a GPU-less host -- see perf_budget; on the desktop this is exactly 45.0.
FPS_FLOOR = perf_budget.rate(45.0)

FAILURES = []
_TMP = tempfile.mkdtemp(prefix="mew_embed_test_")
GCODE = os.path.join(_TMP, "tiny.gcode")
STL = os.path.join(_TMP, "tiny.stl")


def check(name, fn):
    try:
        fn()
        print(f"PASS  {name}")
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(name)
        print(f"FAIL  {name}: {exc!r}")


def make_inputs():
    with open(GCODE, "w") as fh:
        fh.write("G90\nG0 X0 Y0 Z0.02 F3000\n")
        for i in range(3):
            fh.write(f"G0 X0 Y0 Z{0.02 * (i + 1):.3f}\n")
            fh.write("G1 X4 Y0 F600\nG1 X4 Y4\nG1 X0 Y4\nG1 X0 Y0\n")
        fh.write("G0 Z5\n")
    r = subprocess.run([sys.executable, os.path.join(HERE, "engine", "gcode_to_fibers.py"),
                        GCODE, "-o", STL, "-d", "0.05", "-f", "8"],
                       capture_output=True, text=True, cwd=HERE)
    assert r.returncode == 0 and os.path.isfile(STL), \
        f"tiny STL build failed: {r.stderr[-400:]}"


class Child:
    """A minimal parent-side harness: pipes + shared memory + reader thread,
    mirroring gui_embed.EmbedSession without Tk."""

    def __init__(self, argv):
        self.name = f"mewembedtest_{os.getpid()}_{id(self) & 0xffff}"
        self.shm = shared_memory.SharedMemory(create=True, name=self.name,
                                              size=2 * SLOT)
        self.proc = subprocess.Popen(
            argv + ["--embed", f"{self.name}:{MAXW}x{MAXH}"],
            cwd=HERE, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace")
        self.q = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                try:
                    self.q.put(json.loads(line))
                except ValueError:
                    pass
        self.q.put({"t": "__eof__"})

    def send(self, **obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def expect(self, kind, timeout=30.0, keep=None):
        """Wait for the next message of `kind`; other kinds are appended to
        `keep` (a dict of lists) when given, else dropped."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                msg = self.q.get(timeout=0.5)
            except queue.Empty:
                assert self.proc.poll() is None, \
                    f"child died (rc {self.proc.returncode}) awaiting {kind}: " \
                    f"{self.stderr_tail()}"
                continue
            if msg.get("t") == kind:
                return msg
            if keep is not None:
                keep.setdefault(msg.get("t"), []).append(msg)
        raise AssertionError(f"timed out waiting for {kind}")

    def frame_bytes(self, msg):
        off = int(msg["slot"]) * SLOT
        n = int(msg["w"]) * int(msg["h"]) * 3
        return bytes(self.shm.buf[off:off + n])

    def quit(self, expect_rc=0):
        try:
            self.send(t="quit")
        except OSError:
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise AssertionError("child ignored quit")
        assert self.proc.returncode == expect_rc, \
            f"rc {self.proc.returncode} != {expect_rc}: {self.stderr_tail()}"

    def stderr_tail(self):
        try:
            return (self.proc.stderr.read() or "")[-500:]
        except Exception:  # noqa: BLE001
            return ""

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.kill()
        except Exception:  # noqa: BLE001
            pass
        self.shm.close()
        try:
            self.shm.unlink()
        except Exception:  # noqa: BLE001
            pass


def drag(c, x0=300, y0=200, dx=80):
    c.send(t="mouse", ev="press", btn=1, x=x0, y=y0, shift=False, ctrl=False)
    for step in range(0, dx, 10):
        c.send(t="mouse", ev="move", btn=1, x=x0 + step, y=y0,
               shift=False, ctrl=False)
    c.send(t="mouse", ev="release", btn=1, x=x0 + dx, y=y0,
           shift=False, ctrl=False)


# ---------------------------------------------------------------------------
def pure_helpers():
    from viewer_embed import parse_embed_spec, pop_embed_arg
    assert parse_embed_spec("abc:800x600") == ("abc", 800, 600)
    for bad in ("abc", "abc:0x600", ":800x600", "abc:800"):
        try:
            parse_embed_spec(bad)
            raise AssertionError(f"accepted {bad!r}")
        except ValueError:
            pass
    rest, spec = pop_embed_arg(["a.stl", "--embed", "n:8x8", "--render", "x"])
    assert rest == ["a.stl", "--render", "x"] and spec == "n:8x8"
    rest, spec = pop_embed_arg(["a.stl", "--embed=n:8x8"])
    assert rest == ["a.stl"] and spec == "n:8x8"
    rest, spec = pop_embed_arg(["a.stl"])
    assert rest == ["a.stl"] and spec is None


def value_controls_unit():
    """EmbedChrome slider/number controls: export the right spec, apply_value
    dispatches on_set, and current_values snapshots value_fn (so the host can
    push "values" back to the rail after a programmatic change)."""
    from viewer_embed import EmbedChrome
    c = EmbedChrome(None, "Micrograph", "img.png")
    state = {"opacity": 0.7, "x": 1.0}
    c.section("Opacity")
    sid = c.slider("Image opacity", lambda: state["opacity"], 0.0, 1.0,
                   lambda v: state.__setitem__("opacity", float(v)))
    c.section("Position")
    nid = c.number("X (mm)", lambda: state["x"],
                   lambda v: state.__setitem__("x", float(v)))
    specs = {s["id"]: s for s in c.controls}
    assert specs[sid]["type"] == "slider" and specs[sid]["section"] == "Opacity"
    assert specs[sid]["min"] == 0.0 and specs[sid]["max"] == 1.0
    assert specs[nid]["type"] == "number" and specs[nid]["section"] == "Position"
    # a rail edit dispatches to on_set (float from a slider, string from an entry)
    assert c.apply_value(sid, 0.25) and abs(state["opacity"] - 0.25) < 1e-9
    assert c.apply_value(nid, "3.5") and abs(state["x"] - 3.5) < 1e-9
    assert not c.apply_value("nope", 1.0)
    vals = c.current_values()
    assert abs(vals[sid] - 0.25) < 1e-9 and abs(vals[nid] - 3.5) < 1e-9
    # REGRESSION: buttons interspersed AFTER sliders/numbers must still dispatch
    # + label correctly (a zip(controls, buttons) misaligns the two lists and
    # every button click went to the wrong / no control).
    fired = []
    c.section("View")
    c.button(lambda: "Snap", lambda: fired.append("snap"))
    c.button(lambda: "Reset", lambda: fired.append("reset"))
    bids = [s["id"] for s in c.controls if s["type"] == "button"]
    assert c.invoke(bids[0]) and c.invoke(bids[1]), "button dispatch broke"
    assert fired == ["snap", "reset"], fired
    labels = c.current_labels()
    assert labels[bids[0]] == "Snap" and labels[bids[1]] == "Reset", labels
    assert not c.invoke("does-not-exist")


def rail_layout_hints_unit():
    """EmbedChrome's layout hints for the embedded rail: a `toggle` button exports
    its name + live on state, a `role` button exports its role, `current_states`
    tracks the state fn, and `readout()` hands back the rows plus a STATUS only
    when the HUD text no longer matches the text the rows were built with (a
    transient "Computing deviation…"). The label still exports, so the
    fullscreen sidebar -- which reads only labels -- is unaffected."""
    from viewer_embed import EmbedChrome

    class _Hud:
        def __init__(self):
            self.t = ""

        def GetInput(self):                     # noqa: N802
            return self.t

    c = EmbedChrome(None, "Locked simulation", "x.gcode")
    st = {"jet": False}
    c.section("Playback")
    c.button(lambda: "Play", lambda: None, role="play")
    c.section("Simulation")
    c.button(lambda: "Hide jet" if st["jet"] else "Show jet",
             lambda: st.__setitem__("jet", not st["jet"]),
             toggle=("Jet", lambda: st["jet"]))
    specs = {s["label"]: s for s in c.controls}
    assert specs["Play"]["role"] == "play" and "toggle" not in specs["Play"]
    jet = specs["Show jet"]
    assert jet["toggle"] == "Jet" and jet["on"] is False and "role" not in jet
    assert c.invoke(jet["id"]) and c.current_states() == {jet["id"]: True}
    c.refresh_labels()
    assert c.current_labels()[jet["id"]] == "Hide jet"
    assert c.readout() is None                   # no provider registered
    hud = _Hud()
    c.set_hud(hud)
    rows = [{"g": "Print", "k": "Line", "v": "12"}]
    made = {"text": "line 12"}
    c.set_readout(lambda: (rows, made["text"]))
    hud.t = "line 12"
    assert c.readout() == (rows, ""), c.readout()
    hud.t = "  Computing deviation…  "
    assert c.readout() == (rows, "Computing deviation…"), c.readout()


def fullscreen_chrome_slider_drag():
    """The native-fullscreen sidebar (gui_viewer_shell.ViewerChrome) renders a
    real slider whose track press JUMPS the value and MouseMove SCRUBS it, with
    release clearing the grab -- so opacity/scale/etc. are adjustable in
    fullscreen exactly like the embedded rail (not just buttons)."""
    try:
        import vtk
        import gui_viewer_shell as shell
    except Exception as exc:  # noqa: BLE001
        print(f"  (skipped: no vtk/shell: {exc})")
        return
    win = vtk.vtkRenderWindow()
    win.SetOffScreenRendering(1)
    win.SetSize(800, 600)
    ren = vtk.vtkRenderer()
    win.AddRenderer(ren)
    iren = vtk.vtkRenderWindowInteractor()
    iren.SetRenderWindow(win)
    st = {"v": 0.5}
    c = shell.ViewerChrome(win, iren, ren, title="T", subtitle="", dark=True)
    c.section("Opacity")
    c.slider("Image", lambda: st["v"], 0.0, 1.0,
             lambda v: st.__setitem__("v", float(v)))
    c.layout()
    win.Render()
    c.install_click_handler(iren)
    rx, ry, rw, rh = c._sliders[0]["hit"]
    cy = ry + rh // 2
    iren.SetEventInformation(rx + int(rw * 0.9), cy, 0, 0, chr(0), 0, None)
    iren.LeftButtonPressEvent()
    assert st["v"] > 0.8, f"press near right end -> {st['v']}"
    iren.SetEventInformation(rx + 2, cy, 0, 0, chr(0), 0, None)
    iren.MouseMoveEvent()
    assert st["v"] < 0.2, f"drag to left -> {st['v']}"
    iren.LeftButtonReleaseEvent()
    assert c._grab is None, "grab not cleared on release"


def fullscreen_transport_bar():
    """The players' fullscreen timeline (ViewerChrome.transport): a bottom bar
    OVER THE 3D AREA (outside the sidebar -- the strict regression here, since
    the press handler used to bail out right of the sidebar) whose track press
    jumps/scrubs via on_set, whose play/pause card fires on_play, and whose
    knob + time text follow value_fn on refresh_labels. It replaced the
    GUI-side topmost Tk strip, which DWM never composited over the fullscreen
    window while it held the foreground (Bug B, HANDOFF_VIEWER_FULLSCREEN)."""
    try:
        import vtk
        import gui_viewer_shell as shell
    except Exception as exc:  # noqa: BLE001
        print(f"  (skipped: no vtk/shell: {exc})")
        return
    win = vtk.vtkRenderWindow()
    win.SetOffScreenRendering(1)
    win.SetSize(1200, 700)
    ren = vtk.vtkRenderer()
    win.AddRenderer(ren)
    iren = vtk.vtkRenderWindowInteractor()
    iren.SetRenderWindow(win)
    st = {"t": 10.0, "playing": False}
    c = shell.ViewerChrome(win, iren, ren, title="T", subtitle="", dark=True)
    c.transport(lambda: st["t"], 0.0, 100.0,
                lambda v: st.__setitem__("t", float(v)),
                text_fn=lambda: f"{st['t']:.0f} / 100",
                play_label_fn=lambda: "Pause" if st["playing"] else "Play",
                on_play=lambda: st.__setitem__("playing", not st["playing"]))
    c.layout()
    win.Render()
    c.install_click_handler(iren)
    # the bar must sit in the 3D area, right of the sidebar
    rx, ry, rw, rh = c._transport["hit"]
    assert rx > shell.SIDEBAR_WIDTH_PX, f"track at x={rx}, inside the sidebar"
    cy = ry + rh // 2
    iren.SetEventInformation(rx + int(rw * 0.9), cy, 0, 0, chr(0), 0, None)
    iren.LeftButtonPressEvent()
    assert st["t"] > 80.0, f"press near right end -> {st['t']}"
    iren.SetEventInformation(rx + 2, cy, 0, 0, chr(0), 0, None)
    iren.MouseMoveEvent()
    assert st["t"] < 20.0, f"drag to left -> {st['t']}"
    iren.LeftButtonReleaseEvent()
    assert c._grab is None, "grab not cleared on release"
    # the play/pause card toggles and its label refreshes
    bx, by, bw, bh = c._transport_btn["hit"]
    assert bx > shell.SIDEBAR_WIDTH_PX, f"play card at x={bx}, in the sidebar"
    iren.SetEventInformation(bx + bw // 2, by + bh // 2, 0, 0, chr(0), 0, None)
    iren.LeftButtonPressEvent()
    iren.LeftButtonReleaseEvent()
    assert st["playing"], "play card did not fire on_play"
    assert c._transport_btn["actor"].GetInput() == "Pause", \
        c._transport_btn["actor"].GetInput()
    # knob + time text track value_fn on refresh (the per-tick native path)
    st["t"] = 50.0
    c.refresh_labels()
    assert c._transport["actor"].GetInput() == "50 / 100", \
        c._transport["actor"].GetInput()


def coalesce_moves_unit():
    """Task B: _coalesce_moves collapses runs of consecutive same-kind moves
    to the newest, but never swallows a click or a modifier change."""
    from viewer_embed import _coalesce_moves

    def mv(x, btn=1, shift=False, ctrl=False):
        return {"t": "mouse", "ev": "move", "btn": btn, "x": x, "y": 0,
                "shift": shift, "ctrl": ctrl}

    # a plain run collapses to its last
    out = _coalesce_moves([mv(1), mv(2), mv(3)])
    assert out == [mv(3)], out
    # a press/release between moves breaks the run (click never swallowed)
    press = {"t": "mouse", "ev": "press", "btn": 1, "x": 5, "y": 0}
    rel = {"t": "mouse", "ev": "release", "btn": 1, "x": 9, "y": 0}
    out = _coalesce_moves([mv(1), mv(2), press, mv(3), rel])
    assert out == [mv(2), press, mv(3), rel], out
    # a modifier change breaks the run (both survive)
    out = _coalesce_moves([mv(1), mv(2, shift=True)])
    assert out == [mv(1), mv(2, shift=True)], out
    # a different button breaks the run
    out = _coalesce_moves([mv(1, btn=1), mv(2, btn=3)])
    assert out == [mv(1, btn=1), mv(2, btn=3)], out
    # non-move commands are order-preserved and untouched
    wheel = {"t": "mouse", "ev": "wheel", "x": 0, "y": 0, "delta": 1}
    key = {"t": "key", "sym": "space"}
    out = _coalesce_moves([mv(1), mv(2), wheel, key])
    assert out == [mv(2), wheel, key], out
    # empty / single pass through
    assert _coalesce_moves([]) == []
    assert _coalesce_moves([mv(7)]) == [mv(7)]
    # MIDDLE button (2) = incremental trackball PAN -> NEVER coalesced (bug1: it
    # teleported when intermediate moves were dropped); every pan move survives
    out = _coalesce_moves([mv(1, btn=2), mv(2, btn=2), mv(3, btn=2)])
    assert out == [mv(1, btn=2), mv(2, btn=2), mv(3, btn=2)], out
    # left/right still collapse as before
    assert _coalesce_moves([mv(1, btn=1), mv(2, btn=1)]) == [mv(2, btn=1)]
    assert _coalesce_moves([mv(1, btn=3), mv(2, btn=3)]) == [mv(2, btn=3)]


def stl_embed():
    c = Child([sys.executable, os.path.join(HERE, "viewers", "stl_viewer.py"),
               STL, GCODE, "--render", "exact"])
    try:
        hello = c.expect("hello")
        assert hello["title"] == "STL viewer"
        assert not hello["has_transport"]
        labels = {x["label"] for x in hello["controls"]}
        # the STL viewer's "travels" is its own overlay of the commanded G0
        # LINES over a static mesh -- unrelated to the sim's travel FIBRE,
        # so it keeps the old label
        assert "Hide scaffold" in labels and "Hide travels" in labels, labels
        # the print-time estimate moved into the exported footers
        assert any("print time" in f.get("section", "").lower()
                   or "print time" in f.get("text", "").lower()
                   for f in hello["footers"]), hello["footers"]
        c.send(t="resize", w=640, h=480)
        f = c.expect("frame")
        assert (f["w"], f["h"]) == (640, 480), (f["w"], f["h"])
        base = c.frame_bytes(f)
        assert len(set(base[:30000])) > 1, "blank frame"
        # a control command flips its label (Hide scaffold -> Show scaffold)
        cid = next(x["id"] for x in hello["controls"]
                   if x["label"] == "Hide scaffold")
        c.send(t="btn", id=cid)
        lab = c.expect("labels")
        assert lab["labels"].get(cid) == "Show scaffold", lab
        # forwarded interaction re-renders: drag + wheel produce fresh frames
        drag(c)
        c.send(t="mouse", ev="wheel", x=320, y=240, delta=1,
               shift=False, ctrl=False)
        f2 = c.expect("frame")
        assert f2["seq"] > f["seq"]
        c.quit()
    finally:
        c.close()


def player_embed_stream_and_fps():
    c = Child([sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"),
               GCODE, "--render", "tube:6"])
    try:
        keep = {}
        hello = c.expect("hello")
        assert hello["title"] == "Simulation"
        assert hello["has_transport"] and hello["total"] > 0
        sections = {x["section"] for x in hello["controls"]}
        assert sections >= {"Playback", "Scaffold", "Simulation"}, sections
        assert any(f.get("section") == "Keys" for f in hello["footers"])
        c.send(t="resize", w=640, h=480)
        c.expect("frame", keep=keep)
        # HUD streams (the sidebar readout now lives in the GUI rail)
        hud = c.expect("hud", keep=keep)
        assert "t " in hud["text"] or "PLAY" in hud["text"]
        # fps window: count frames for 3 s while playing
        n0 = 0
        t0 = time.time()
        while time.time() - t0 < 3.0:
            c.expect("frame", timeout=5.0, keep=keep)
            n0 += 1
        fps = n0 / (time.time() - t0)
        print(f"      locked-player publish rate: {fps:.1f} fps")
        assert fps >= FPS_FLOOR, f"{fps:.1f} fps < {FPS_FLOOR} floor"
        # scrub jumps the clock and pauses
        c.send(t="scrub", time=0.5)
        end = time.time() + 5
        seen = None
        while time.time() < end:
            st = c.expect("state", timeout=5.0)
            if not st["playing"]:
                seen = st
                break
        assert seen is not None and abs(seen["time"] - 0.5) < 0.05, seen
        # space resumes (forwarded key acts)
        c.send(t="key", sym="space", char=" ", shift=False, ctrl=False)
        end = time.time() + 5
        ok = False
        while time.time() < end:
            st = c.expect("state", timeout=5.0)
            if st["playing"] and st["time"] > 0.5:
                ok = True
                break
        assert ok, "space did not resume playback"
        c.quit()
    finally:
        c.close()


def player_static_default():
    """U2a/U2e: `--static` opens the player paused on the FINISHED scaffold (t ==
    total, not playing) with the printhead + jet HIDDEN (travels default ON since
    2026-07-21). The sidebar labels must read "Show printhead/jet" (NOT the stale
    "Hide ...", the backwards-toggle bug) and "Hide travels", grouped under
    Playback / Scaffold / Simulation. Scrubbing reveals the printhead + jet, so
    those labels FLIP to "Hide ..." (travels stay shown)."""
    c = Child([sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"),
               GCODE, "--render", "tube:6", "--static"])
    try:
        hello = c.expect("hello")
        assert hello["title"] == "Simulation"
        total = hello["total"]
        assert total > 0
        sections = {x.get("section") for x in hello["controls"]}
        assert sections >= {"Playback", "Scaffold", "Simulation"}, sections
        labels = {x.get("label") for x in hello["controls"]}
        # printhead + jet open HIDDEN; travels default ON
        for want in ("Show printhead", "Show jet", "Hide travels"):
            assert want in labels, (want, sorted(labels))
        for bad in ("Hide printhead", "Hide jet", "Show travels"):
            assert bad not in labels, (bad, sorted(labels))  # not the stale label
        # the embedded rail's layout hints: every playback button carries its
        # transport role, and the printhead / travel toggles ship as named
        # switches whose `on` matches the label (hidden / shown)
        roles = {x.get("role") for x in hello["controls"]}
        assert roles >= {"play", "slower", "faster", "restart"}, roles
        sw = {x["toggle"]: x["on"] for x in hello["controls"] if x.get("toggle")}
        assert sw.get("Printhead") is False and sw.get("Travels") is True, sw
        assert hello.get("has_rows") is True, hello.get("has_rows")
        c.send(t="resize", w=640, h=480)
        c.expect("frame")
        st = c.expect("state", timeout=5.0)
        assert not st["playing"], f"static launch should be paused: {st}"
        assert abs(st["time"] - total) < 0.05, f"static should park at t_end: {st}"
        # scrub -> sim interaction -> reveal printhead + jet -> labels flip to
        # "Hide ..." (travels are already shown, so only these two flip)
        c.send(t="scrub", time=total * 0.3)
        want = {"Hide printhead", "Hide jet"}
        seen = set()
        keep = {}
        end = time.time() + 6
        while time.time() < end and not want <= seen:
            try:
                msg = c.expect("labels", timeout=1.0, keep=keep)
            except AssertionError:
                break
            seen.update(msg.get("labels", {}).values())
        assert want <= seen, f"scrub did not reveal the sim tool: {sorted(seen)}"
        c.quit()
    finally:
        c.close()


def player_view_snap_top():
    """The unified sim viewer carries a "View" section with a "Snap top-down"
    camera preset (the micrograph viewer's snap, ported after the viewers merged).
    The button must appear in the hello, and pressing it must render a fresh frame
    (it reorients the camera locally -- no parent round-trip like deviation)."""
    c = Child([sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"),
               GCODE, "--render", "tube:6", "--static"])
    try:
        hello = c.expect("hello")
        sections = {x.get("section") for x in hello["controls"]}
        assert "View" in sections, sections
        snap_id = next((x.get("id") for x in hello["controls"]
                        if x.get("label") == "Snap top-down"), None)
        assert snap_id is not None, [x.get("label") for x in hello["controls"]]
        c.send(t="resize", w=640, h=480)
        c.expect("frame")
        # press it -> a straight-down reframe -> a fresh frame streams back
        c.send(t="btn", id=snap_id)
        c.expect("frame", timeout=6.0)
        c.quit()
    finally:
        c.close()


def player_view_cube_toggle():
    """The ViewCube is SCENE-side (its own layer-1 renderer in the child's
    window) while its show/hide is a plain chrome.button -- that split is what
    puts the gizmo in the embedded panel, where there is no ViewerChrome at all,
    AND in native fullscreen, from one construction.

    So the contract is: the toggle ships in the "View" section of the FIRST
    hello reading "Hide view cube" (the cube opens visible, and a label built
    before the flag is set is the classic backwards-toggle bug), pressing it
    flips the label, and the child re-renders rather than round-tripping to the
    parent (the snap/toggle are local, unlike deviation)."""
    c = Child([sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"),
               GCODE, "--render", "tube:6", "--static"])
    try:
        hello = c.expect("hello")
        view = [x for x in hello["controls"] if x.get("section") == "View"]
        labels = {x.get("label") for x in view}
        assert "Hide view cube" in labels, view
        # honest from the first hello -- never "Show" while the cube IS showing
        assert "Show view cube" not in labels, view
        cid = next(x["id"] for x in view if x["label"] == "Hide view cube")
        c.send(t="resize", w=640, h=480)
        c.expect("frame")
        c.send(t="btn", id=cid)
        lab = c.expect("labels", timeout=6.0)
        assert lab["labels"].get(cid) == "Show view cube", lab
        c.expect("frame", timeout=6.0)
        # and back
        c.send(t="btn", id=cid)
        lab = c.expect("labels", timeout=6.0)
        assert lab["labels"].get(cid) == "Hide view cube", lab
        c.quit()
    finally:
        c.close()


def player_view_cube_snaps_on_click():
    """Clicking the cube must SNAP the camera, in the embedded panel as much as
    in fullscreen -- the whole point of the gizmo. The panel forwards clicks
    down stdin into the child's real interactor, so a synthesized press at the
    gizmo centre exercises the same observer the OS drives when maximized.

    Asserted by pixels: the click lands on the corner facing the camera, which
    re-frames the scene, so the streamed frame must CHANGE. A press well away
    from the gizmo box must not snap (it belongs to the orbit style)."""
    c = Child([sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"),
               GCODE, "--render", "tube:6", "--static"])
    try:
        c.expect("hello")
        c.send(t="resize", w=640, h=480)
        f = c.expect("frame")
        before = bytes(c.frame_bytes(f))
        # the gizmo box, in the child's own pixel terms
        sys.path.insert(0, HERE)
        import view_cube
        x0, y0, x1, y1 = view_cube.viewport_rect(640, 480)
        cx = (x0 + x1) * 0.5 * 640
        # the panel's y is top-down; the child flips it (viewer_embed._flip_y)
        cy_gl = (y0 + y1) * 0.5 * 480
        cy = 480 - cy_gl
        c.send(t="mouse", ev="press", btn=1, x=int(cx), y=int(cy),
               shift=False, ctrl=False)
        c.send(t="mouse", ev="release", btn=1, x=int(cx), y=int(cy),
               shift=False, ctrl=False)
        end = time.time() + 6
        changed = False
        while time.time() < end and not changed:
            try:
                f2 = c.expect("frame", timeout=1.0)
            except AssertionError:
                break
            changed = bytes(c.frame_bytes(f2)) != before
        assert changed, "a click on the ViewCube did not snap the view"
        c.quit()
    finally:
        c.close()


def player_plate_shown_by_default():
    """The unified viewer grounds the scaffold: when the export wrote a build-plate
    sibling (<stem>_plate.stl), the player shows it by DEFAULT, and the sidebar
    COLLECTOR toggle reads "Hide collector" from the FIRST hello (the plate is
    revealed before the buttons are built, so there is no one-frame "Show
    collector" flicker). The button was renamed from "plate" on 2026-09-02 when
    it took on the mandrel rod as well -- one collector control, either mode."""
    d = tempfile.mkdtemp(prefix="mew_plate_")
    gc = os.path.join(d, "p.gcode")
    st = os.path.join(d, "p.stl")
    with open(gc, "w") as fh:
        fh.write("G90\nG0 X0 Y0 Z0.02 F3000\n")
        for i in range(2):
            fh.write(f"G0 X0 Y0 Z{0.02 * (i + 1):.3f}\n")
            fh.write("G1 X6 Y0 F600\nG1 X6 Y6\nG1 X0 Y6\nG1 X0 Y0\n")
        fh.write("G0 Z5\n")
    r = subprocess.run([sys.executable, os.path.join(HERE, "engine", "gcode_to_fibers.py"),
                        gc, "-o", st, "-d", "0.05", "-f", "8", "--plate"],
                       capture_output=True, text=True, cwd=HERE)
    assert r.returncode == 0, r.stderr[-300:]
    assert os.path.isfile(os.path.splitext(st)[0] + "_plate.stl"), "no plate sibling"
    c = Child([sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"),
               gc, st, "--render", "tube:6", "--static"])
    try:
        hello = c.expect("hello")
        labels = {x.get("label") for x in hello["controls"]}
        assert "Hide collector" in labels, sorted(labels)   # shown by default
        assert "Show collector" not in labels, sorted(labels)  # not the stale
        assert not any("plate" in (x or "").lower() for x in labels), sorted(labels)
        c.quit()
    finally:
        c.close()


def player_pan_no_teleport():
    """Regression (U2-fix): the embedded player's middle-button PAN must not
    teleport. Root cause was the host loop invoking the interactor's generic
    TimerEvent to drive the clock -- vtkInteractorStyle::OnTimer then RE-APPLIED
    the in-progress pan State off the last event positions, so every idle clock
    tick panned the camera (a jump on first drag, drift while held). Orbit/turn/
    zoom never hit it because install_cad_camera intercepts them (base State stays
    NONE); middle-pan is the one gesture the base trackball still owns. The fix
    drives the clock through host.tick_hook (a direct _on_tick) instead.

    The decisive symptom: a middle PRESS followed by IDLE ticks (no moves) used
    to walk the focal point away. Assert it stays put, and that an actual small
    move produces only a small, bounded pan."""
    c = Child([sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"),
               GCODE, "--render", "tube:6", "--static"])
    try:
        c.expect("hello")
        W, H = 640, 480
        c.send(t="resize", w=W, h=H)
        c.expect("frame")
        st0 = c.expect("state", timeout=5.0)
        f0 = st0.get("focal")
        assert f0 is not None, f"state carries no focal: {st0}"

        # middle-button PRESS at the panel centre, then NO moves -- just let the
        # 16 ms clock tick for ~0.6 s. A press alone (and idle ticks) must never
        # pan. Pre-fix this teleported tens of mm on the first idle tick.
        c.send(t="mouse", ev="press", btn=2, x=W // 2, y=H // 2,
               shift=False, ctrl=False)
        worst = 0.0
        end = time.time() + 0.6
        while time.time() < end:
            try:
                st = c.expect("state", timeout=0.5)
            except AssertionError:
                break
            f = st.get("focal") or f0
            worst = max(worst, max(abs(f[i] - f0[i]) for i in range(3)))
        assert worst < 0.05, \
            f"held middle-button PAN drifted on idle ticks: {worst:.3f} mm " \
            f"(from {f0}); the OnTimer re-pan regressed"

        # a real small move now pans smoothly and by a BOUNDED amount (not a
        # teleport). ~40 px on a 640-wide panel is a few mm, never tens.
        c.send(t="mouse", ev="move", btn=2, x=W // 2 + 40, y=H // 2,
               shift=False, ctrl=False)
        c.send(t="mouse", ev="release", btn=2, x=W // 2 + 40, y=H // 2,
               shift=False, ctrl=False)
        moved = 0.0
        end = time.time() + 0.6
        while time.time() < end:
            try:
                st = c.expect("state", timeout=0.5)
            except AssertionError:
                break
            f = st.get("focal") or f0
            moved = max(moved, max(abs(f[i] - f0[i]) for i in range(3)))
        assert 0.0 < moved < 8.0, \
            f"a 40px middle move should pan a few mm, got {moved:.3f} mm"
        c.quit()
    finally:
        c.close()


def deviation_in_scene():
    """U2c end-to-end: the unified viewer's 'Show deviation' button sends a
    child->parent REQUEST; the GUI computes a sidecar and pushes it back; the
    player recolours the scaffold IN PLACE (no relaunch). Drives the child
    directly: click -> request(action=deviation); push a real sidecar -> the label
    flips to 'Hide deviation' and a frame still renders (the recoloured actor)."""
    import mew_analysis
    depo = os.path.splitext(STL)[0] + ".depo.npz"
    assert os.path.isfile(depo), "make_inputs did not write a .depo cache"
    side = os.path.join(_TMP, "dev.npz")
    cmd = mew_analysis.bundle_from_gcode(GCODE, 0.05)
    pred = mew_analysis.bundle_from_depo(depo, fallback_diameter=0.05)
    dev = mew_analysis._deviation_by_fiber(cmd, pred)
    mew_analysis.write_deviation_sidecar(side, pred, cmd, dev_by_fiber=dev,
                                         depo_path=depo)
    assert os.path.isfile(side)
    c = Child([sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"),
               GCODE, STL, "--render", "tube:6", "--static"])
    try:
        hello = c.expect("hello")
        dev_id = next((x.get("id") for x in hello["controls"]
                       if x.get("label") == "Show deviation"), None)
        assert dev_id is not None, [x.get("label") for x in hello["controls"]]
        c.send(t="resize", w=640, h=480)
        c.expect("frame")
        # click "Show deviation" -> the child asks the parent (the U2b channel)
        c.send(t="btn", id=dev_id)
        req = c.expect("request", timeout=6.0)
        assert req.get("action") == "deviation", req
        # a persistent "Computing deviation" HUD shows while it waits (bug3a: the
        # per-tick readout used to overwrite it in 16 ms so there was no status)
        seen_hud, keep_h = False, {}
        end = time.time() + 4
        while time.time() < end and not seen_hud:
            try:
                h = c.expect("hud", timeout=1.0, keep=keep_h)
            except AssertionError:
                break
            seen_hud = "Computing deviation" in (h.get("text") or "")
        assert seen_hud, "no 'Computing deviation' status while analysing"
        # simulate the parent computing + pushing the sidecar DOWN (apply-in-place)
        c.send(t="deviation", sidecar=side)
        want = "Hide deviation"       # label flips once the recolour is applied
        seen, keep = set(), {}
        end = time.time() + 8
        while time.time() < end and want not in seen:
            try:
                msg = c.expect("labels", timeout=1.0, keep=keep)
            except AssertionError:
                break
            seen.update(msg.get("labels", {}).values())
        assert want in seen, f"deviation was not applied in place: {sorted(seen)}"
        c.expect("frame", keep=keep)     # still streaming (the recoloured scene)
        c.quit()
    finally:
        c.close()


def deviation_failure_stays_on_the_hud():
    """When the GUI pushes a FAILED deviation back (empty sidecar + error), the
    reason has to stay on the HUD -- the rail's status line -- until the next
    attempt. It was written once with hud.SetInput and the next readout refresh
    overwrote it, so a refused analysis looked like the button did nothing
    (reported: "I tried to toggle deviation, nothing happened").

    Comparator: the last HUD and readout status the child publishes AFTER a scrub
    has forced fresh readout refreshes. RED against the one-shot SetInput: the
    scrub's readout replaces the message."""
    c = Child([sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"),
               GCODE, STL, "--render", "tube:6", "--static"])
    try:
        hello = c.expect("hello")
        dev_id = next((x.get("id") for x in hello["controls"]
                       if x.get("label") == "Show deviation"), None)
        assert dev_id is not None, [x.get("label") for x in hello["controls"]]
        total = float(hello.get("total") or 0.0)
        c.send(t="resize", w=640, h=480)
        c.expect("frame")
        c.send(t="btn", id=dev_id)
        assert c.expect("request", timeout=6.0).get("action") == "deviation"
        reason = "the g-code has 3 fibres but the .depo has 4"
        c.send(t="deviation", sidecar="", error=reason)
        time.sleep(0.5)
        # force readout refreshes: the playhead moves, so the readout text changes
        for frac in (0.3, 0.6, 0.9):
            c.send(t="scrub", time=total * frac)
            time.sleep(0.3)
        # drain everything published since the request, in order
        keep, last_hud, last_status = {}, None, None
        end = time.time() + 3.0
        while time.time() < end:
            try:
                c.expect("__none__", timeout=0.5, keep=keep)
            except AssertionError:
                pass
        for m in keep.get("hud", []):
            last_hud = m.get("text") or ""
        for m in keep.get("readout", []):
            last_status = m.get("status") or ""
        assert last_hud is not None and reason in last_hud, \
            f"the failure left the HUD: last HUD {last_hud!r}"
        if last_status is not None:
            assert reason in last_status, \
                f"the rail status lost the failure: {last_status!r}"
        c.quit()
    finally:
        c.close()


def deviation_reveal_tracks_clock():
    """The deviation view rides the SAME progressive reveal as the plain deposition:
    with 'Show deviation' on, scrubbing the clock SHRINKS the revealed (deviation-
    coloured) cell count and deviation STAYS on -- it is now a colour scheme on the
    deposition mesh, not a separate whole-scaffold actor that parked the clock.
    Pre-feature, turning deviation on set t=total and any play/scrub CLEARED it, so
    this test FAILS against the old behaviour (dev_on drops, dev_cells never shrinks)."""
    import mew_analysis
    depo = os.path.splitext(STL)[0] + ".depo.npz"
    side = os.path.join(_TMP, "dev_reveal.npz")
    cmd = mew_analysis.bundle_from_gcode(GCODE, 0.05)
    pred = mew_analysis.bundle_from_depo(depo, fallback_diameter=0.05)
    dev = mew_analysis._deviation_by_fiber(cmd, pred)
    mew_analysis.write_deviation_sidecar(side, pred, cmd, dev_by_fiber=dev,
                                         depo_path=depo)
    c = Child([sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"),
               GCODE, STL, "--render", "tube:6", "--static"])
    try:
        hello = c.expect("hello")
        dev_id = next((x.get("id") for x in hello["controls"]
                       if x.get("label") == "Show deviation"), None)
        assert dev_id is not None, [x.get("label") for x in hello["controls"]]
        c.send(t="resize", w=640, h=480)
        c.expect("frame")
        c.send(t="btn", id=dev_id)
        assert c.expect("request", timeout=6.0).get("action") == "deviation"
        c.send(t="deviation", sidecar=side)
        # wait until the deviation scheme is live on the revealed mesh (dev_on with
        # cells shown) -- static opens at t=total, so the whole mesh is revealed
        keep, full = {}, None
        end = time.time() + 12
        while time.time() < end and full is None:
            st = c.expect("state", timeout=1.0, keep=keep)
            if st.get("dev_on") and int(st.get("dev_cells", 0)) > 0:
                full = st
        assert full is not None, "deviation never applied on the revealed mesh"
        total = float(full["total"])
        full_cells = int(full["dev_cells"])
        # scrub back to 30% -- the revealed cell count must SHRINK, deviation stay on
        c.send(t="scrub", time=total * 0.3)
        shrank, end = False, time.time() + 8
        while time.time() < end and not shrank:
            st = c.expect("state", timeout=1.0, keep=keep)
            assert st.get("dev_on"), "scrubbing CLEARED the deviation view (regressed)"
            n = int(st.get("dev_cells", -1))
            shrank = 0 <= n < full_cells
        assert shrank, f"reveal did not track the clock: still {full_cells} cells at 30%"
        # scrub forward to the end -- the whole deviation-coloured mesh returns
        c.send(t="scrub", time=total)
        grew, end = False, time.time() + 8
        while time.time() < end and not grew:
            st = c.expect("state", timeout=1.0, keep=keep)
            assert st.get("dev_on"), "deviation dropped on scrub-to-end"
            grew = int(st.get("dev_cells", 0)) >= full_cells
        assert grew, "reveal did not return to full at t=total"
        c.quit()
    finally:
        c.close()


def micrograph_in_scene():
    """U2d: pushing a micrograph image DOWN adds an overlay plane and re-sends the
    hello with a Micrograph slider section (opacity / origin / scale / rotation). A
    slider edit dispatches to the image state; a frame still renders."""
    from PIL import Image
    img = os.path.join(_TMP, "micro.png")
    Image.new("RGB", (64, 48), (180, 120, 60)).save(img)
    c = Child([sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"),
               GCODE, "--render", "tube:6", "--static"])
    try:
        c.expect("hello")                       # the initial hello
        c.send(t="resize", w=640, h=480)
        c.expect("frame")
        c.send(t="micrograph", image=img)       # simulate the GUI's Import push
        h2 = c.expect("hello", timeout=8.0)     # re-hello with the Micrograph section
        sliders = [x for x in h2["controls"]
                   if x.get("section") == "Micrograph"
                   and x.get("type") == "slider"]
        labels = {x["label"] for x in sliders}
        # Origin X / Y / Z are all present -- the Z slider (scrubs the image height)
        # regressed out of the in-scene sidebar during the UI merge; guard it back in.
        assert {"Image opacity", "Origin X (mm)", "Origin Y (mm)", "Origin Z (mm)",
                "Scale (mm/px)", "Rotation (deg)"} <= labels, labels
        # driving a slider dispatches to the live image state (no crash, still streams)
        op = next(x for x in sliders if x["label"] == "Image opacity")
        c.send(t="ctl", id=op["id"], value=0.4)
        c.expect("frame")
        # driving the Z-origin slider moves the plane in Z (dispatches, still streams)
        oz = next(x for x in sliders if x["label"] == "Origin Z (mm)")
        c.send(t="ctl", id=oz["id"], value=0.5)
        c.expect("frame")
        # "Exit micrograph" removes the whole section from the sidebar (bug5:
        # "Remove image" left the sliders behind)
        ex = next((x.get("id") for x in h2["controls"]
                   if x.get("label") == "Exit micrograph"), None)
        assert ex is not None, [x.get("label") for x in h2["controls"]]
        c.send(t="btn", id=ex)
        h3 = c.expect("hello", timeout=8.0)     # re-hello WITHOUT the Micrograph section
        assert "Micrograph" not in {x.get("section") for x in h3["controls"]}, \
            [x.get("section") for x in h3["controls"]]
        c.quit()
    finally:
        c.close()


def machine_off_turns_printhead_off():
    """The printhead is part of the machine: Machine off turns the Printhead
    switch off too, so ONE click then shows the head alone (and draws it), and
    Machine on turns it back on. The switch used to stay on while the head was
    hidden, so the head alone took two clicks (reported 2026-10-04)."""
    import tempfile
    from test_machine_live import _machine_json
    c = Child([sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"), GCODE,
               "--render", "tube:6", "--machine",
               _machine_json(tempfile.mkdtemp(prefix="mew_mach_"))])
    try:
        hello = c.expect("hello")
        ids = {x["toggle"]: x["id"] for x in hello["controls"]
               if x.get("toggle")}
        states = {x["id"]: x.get("on") for x in hello["controls"]
                  if x.get("toggle")}
        mach, head = ids["Machine"], ids["Printhead"]
        c.send(t="resize", w=320, h=240)
        c.expect("frame")
        c.send(t="scrub", time=2.0)          # reveal the sim tool, mid-print

        def settle():
            """Drain for a beat; return the newest frame's pixels."""
            end, last = time.time() + 1.5, None
            while time.time() < end:
                try:
                    m = c.q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if m.get("t") == "states":
                    states.update(m["states"])
                elif m.get("t") == "frame":
                    last = c.frame_bytes(m)
            return last

        settle()
        assert states[mach] and states[head], states
        c.send(t="btn", id=mach)
        off = settle()
        assert states[mach] is False and states[head] is False, \
            f"Machine off left Printhead {states[head]}"
        c.send(t="btn", id=head)
        alone = settle()
        assert states[head] is True, "one click did not turn Printhead on"
        assert off is not None and alone is not None and off != alone, \
            "the head was not drawn after one click"
        c.send(t="btn", id=mach)
        settle()
        assert states[mach] is True and states[head] is True, states
        c.quit()
    finally:
        c.close()


def live_edit_request():
    """U2e: the unified viewer's 'Live edit' button asks the GUI to relaunch in
    real-time mode (an unavoidable relaunch -- the live re-lay can't jump to the
    finished part). Verify the button is present in the static viewer and clicking
    it emits a live_edit request (on=True)."""
    c = Child([sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"),
               GCODE, "--render", "tube:6", "--static"])
    try:
        hello = c.expect("hello")
        le = next((x.get("id") for x in hello["controls"]
                   if x.get("label") == "Live edit"), None)
        assert le is not None, [x.get("label") for x in hello["controls"]]
        c.send(t="btn", id=le)
        req = c.expect("request", timeout=6.0)
        assert req.get("action") == "live_edit" and req.get("on") is True, req
        c.quit()
    finally:
        c.close()


def live_embed_knobs():
    c = Child([sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"),
               GCODE, "--render", "tube:6", "--live-adjust"])
    try:
        hello = c.expect("hello")
        assert hello["title"] == "Live simulation"
        labels = {x["label"] for x in hello["controls"]}
        assert "Reset params" in labels, labels
        # deviation is a locked-sim-only view (the live re-lay has no matching
        # per-print sidecar), so its Show/Hide button must NOT appear in the live sim
        assert not ({"Show deviation", "Hide deviation"} & labels), labels
        keys_txt = " ".join(f.get("text", "") for f in hello["footers"])
        assert "voltage" in keys_txt, keys_txt
        c.expect("frame")
        # the v key bumps voltage: the HUD readout must show the new value
        c.send(t="key", sym="v", char="v", shift=False, ctrl=False)
        end = time.time() + 10
        ok = False
        while time.time() < end:
            hud = c.expect("hud", timeout=10.0)
            if "V 4.5" in hud["text"]:
                ok = True
                break
        assert ok, "voltage bump not reflected in HUD"
        c.quit()
    finally:
        c.close()


def move_flood_stays_responsive():
    """Task B: flooding ~200 queued mouse-moves must not wedge the child --
    it coalesces them and stays responsive (a control command still flips a
    label promptly, the camera still moved, quit still exits 0)."""
    c = Child([sys.executable, os.path.join(HERE, "viewers", "stl_viewer.py"),
               STL, GCODE, "--render", "exact"])
    try:
        hello = c.expect("hello")
        c.send(t="resize", w=640, h=480)
        f0 = c.expect("frame")
        # press, then dump 200 moves as fast as the pipe takes them
        c.send(t="mouse", ev="press", btn=1, x=300, y=240,
               shift=False, ctrl=False)
        for i in range(200):
            c.send(t="mouse", ev="move", btn=1, x=300 + (i % 120), y=240,
                   shift=False, ctrl=False)
        c.send(t="mouse", ev="release", btn=1, x=420, y=240,
               shift=False, ctrl=False)
        # still responsive: a button command flips its label within a beat
        cid = next(x["id"] for x in hello["controls"]
                   if x["label"] == "Hide scaffold")
        c.send(t="btn", id=cid)
        lab = c.expect("labels", timeout=10.0)
        assert lab["labels"].get(cid) == "Show scaffold", lab
        # and the drag actually moved the camera (fresh frame differs)
        f1 = c.expect("frame", timeout=10.0)
        assert f1["seq"] > f0["seq"]
        c.quit()
    finally:
        c.close()


def native_fs_roundtrip():
    """Task A: native_fs on -> the child's render window goes onscreen
    (borderless, at the given exact rect -- small and brief here), frame
    publishing suspends, labels/state keep flowing; a native Esc keypress
    (posted as a real WM_KEYDOWN) exits truthfully; the stream resumes at the
    panel size. Windows-only, like the feature."""
    if os.name != "nt":
        print("      (skipped: native_fs is Windows-only)")
        return
    import ctypes
    c = Child([sys.executable, os.path.join(HERE, "viewers", "stl_viewer.py"),
               STL, GCODE, "--render", "tube:6"])
    try:
        hello = c.expect("hello")
        assert hello.get("native_fs") is True, hello
        c.send(t="resize", w=640, h=480)
        f = c.expect("frame")
        # enter: exact small rect so the test never flashes a real fullscreen
        c.send(t="native_fs", on=True, x=120, y=120, w=480, h=360, exact=True)
        r = c.expect("native_fs")
        assert r["on"] is True, r
        # frames are suspended while native: drain, then expect silence
        time.sleep(0.5)
        while True:
            try:
                msg = c.q.get_nowait()
            except queue.Empty:
                break
            assert msg.get("t") != "frame" or msg.get("seq", 0) <= f["seq"] + 2, \
                f"frame published while native: {msg}"
        # native Esc: post a REAL WM_KEYDOWN to the child's window and expect
        # the truthful off reply (the user-pressed-Esc path)
        hwnd = ctypes.windll.user32.FindWindowW(
            None, f"STL viewer  --  {os.path.basename(STL)}")
        assert hwnd, "native window not found by title"
        ctypes.windll.user32.PostMessageW(hwnd, 0x0100, 0x1B, 0)  # VK_ESCAPE
        r2 = c.expect("native_fs", timeout=10.0)
        assert r2["on"] is False, r2
        # streaming resumes at the pre-fullscreen size
        f2 = c.expect("frame", timeout=10.0)
        assert (f2["w"], f2["h"]) == (640, 480), (f2["w"], f2["h"])
        assert f2["seq"] > f["seq"]
        # ⛶ again: enter + exit via the command path too
        c.send(t="native_fs", on=True, x=120, y=120, w=480, h=360, exact=True)
        assert c.expect("native_fs")["on"] is True
        c.send(t="native_fs", on=False)
        assert c.expect("native_fs", timeout=10.0)["on"] is False
        c.expect("frame", timeout=10.0)
        c.quit()
    finally:
        c.close()


def docked_roundtrip():
    """Docked panel: the GUI hands over its viewport HWND and the child's
    render window becomes a WS_CHILD of it at (0, 0, w, h), drawing onscreen
    (streaming at a maximised panel capped the Tk paste near 20 fps). It must
    follow a resize, leave the panel for fullscreen and come BACK on exit
    (not to streaming), and undock to streaming on hwnd 0. RED on a child
    without the dock handler: no dock reply ever arrives. Windows-only."""
    if os.name != "nt":
        print("      (skipped: docking is Windows-only)")
        return
    import ctypes
    import tkinter as tk
    u = ctypes.windll.user32
    u.GetWindow.restype = ctypes.c_void_p
    u.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]

    class _R(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]

    root = tk.Tk()
    root.geometry("440x340+60+60")
    host = tk.Frame(root, width=400, height=300, background="#202020")
    host.place(x=10, y=10)
    root.update()
    parent = int(host.winfo_id())

    def docked_child():
        return u.GetWindow(parent, 5) or 0                  # GW_CHILD

    def child_size():
        r = _R()
        u.GetClientRect(ctypes.c_void_p(docked_child()), ctypes.byref(r))
        return r.r - r.l, r.b - r.t

    def wait(kind, timeout=20.0):
        # pump Tk while waiting: the parent HWND lives on this thread
        end = time.time() + timeout
        while time.time() < end:
            root.update()
            try:
                msg = c.q.get(timeout=0.02)
            except queue.Empty:
                assert c.proc.poll() is None, c.stderr_tail()
                continue
            if msg.get("t") == kind:
                return msg
        raise AssertionError(f"timed out waiting for {kind}")

    c = Child([sys.executable, os.path.join(HERE, "viewers", "stl_viewer.py"),
               STL, GCODE, "--render", "tube:6"])
    try:
        hello = wait("hello")
        assert hello.get("dock") is True, hello
        c.send(t="resize", w=400, h=300)
        f = wait("frame")
        assert docked_child() == 0
        c.send(t="dock", hwnd=parent, w=400, h=300)
        assert wait("dock")["on"] is True
        assert docked_child(), "render window not parented into the viewport"
        assert child_size() == (400, 300), child_size()
        # frames are suspended while docked
        c.send(t="btn", id=hello["controls"][0]["id"])
        wait("labels")
        end = time.time() + 0.5
        while time.time() < end:
            root.update()
            try:
                msg = c.q.get(timeout=0.02)
            except queue.Empty:
                continue
            assert msg.get("t") != "frame", f"frame published while docked: {msg}"
        # a panel resize resizes the docked window
        c.send(t="resize", w=320, h=240)
        end = time.time() + 5
        while child_size() != (320, 240) and time.time() < end:
            root.update()
            time.sleep(0.02)
        assert child_size() == (320, 240), child_size()
        # fullscreen takes the window out of the panel; exit re-docks it
        c.send(t="native_fs", on=True, x=120, y=120, w=480, h=360, exact=True)
        assert wait("native_fs")["on"] is True
        assert docked_child() == 0, "still parented while fullscreen"
        c.send(t="native_fs", on=False)
        assert wait("native_fs")["on"] is False
        assert wait("dock")["on"] is True
        assert docked_child(), "fullscreen exit did not re-dock"
        assert child_size() == (320, 240), child_size()
        # hwnd 0 undocks back to streaming
        c.send(t="dock", hwnd=0)
        assert wait("dock")["on"] is False
        assert docked_child() == 0
        f2 = wait("frame")
        assert f2["seq"] > f["seq"] and (f2["w"], f2["h"]) == (320, 240), f2
        c.quit()
    finally:
        c.close()
        root.destroy()


def docked_hides_behind_page():
    """The shell's PageStack lifts pages and never unmaps them, so a DOCKED
    viewer (a foreign child HWND) drew over whatever page was in front: a
    relaunch-on-generate flashed the scene over Scaffold designer. A real
    ViewerPanel in a real PageStack must keep its docked window hidden while
    another page is selected -- including through a relaunch -- and show it
    again on return. RED on the panel before the fix: the docked window stays
    visible after selecting the other page. Windows-only, like docking."""
    if os.name != "nt":
        print("      (skipped: docking is Windows-only)")
        return
    import ctypes
    import tkinter as tk
    from tkinter import ttk
    import gui_embed
    import gui_fonts
    import gui_pagestack
    u = ctypes.windll.user32
    u.GetWindow.restype = ctypes.c_void_p
    u.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    root = tk.Tk()
    gui_fonts.init_fonts(root)
    root.geometry("900x640+40+40")

    class _App:
        pass
    app = _App()
    app.root = root
    app.dark_mode = tk.BooleanVar(value=False)
    stack = gui_pagestack.PageStack(root)
    stack.pack(fill="both", expand=True)
    front = ttk.Frame(stack)
    vpage = ttk.Frame(stack)
    stack.add(front, text="Scaffold")
    stack.add(vpage, text="Viewer")
    panel = gui_embed.ViewerPanel(vpage, app, "unified", "empty")
    panel.pack(fill="both", expand=True)
    stack.select(vpage)
    seen = {"vis": False}

    def docked_visible():
        h = u.GetWindow(int(panel.viewport.winfo_id()), 5) or 0  # GW_CHILD
        return bool(h and u.IsWindowVisible(ctypes.c_void_p(h)))

    def pump(until, secs, watch=False):
        end = time.time() + secs              # computed once: bounded
        while time.time() < end:
            root.update()
            if watch and docked_visible():
                seen["vis"] = True
            if until():
                return True
            time.sleep(0.01)
        return until()

    argv = [sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"),
            GCODE, "--render", "tube:6", "--static"]
    try:
        panel.launch(argv)
        assert pump(lambda: panel._docked, 90.0), "never docked"
        assert pump(docked_visible, 5.0), "docked window not shown on its page"
        stack.select(front)
        assert pump(lambda: not docked_visible(), 5.0), \
            "docked window still visible with another page in front"
        # the reported case: a relaunch while another page is in front
        panel.launch(argv)
        pump(lambda: False, 12.0, watch=True)
        assert not seen["vis"], "relaunched viewer showed over the front page"
        stack.select(vpage)
        assert pump(lambda: panel._docked and docked_visible(), 90.0), \
            "viewer did not dock/show on returning to its page"
        panel.stop()
        pump(lambda: panel.session is None, 10.0)
    finally:
        if panel.session is not None:
            panel.session.destroy()
        root.destroy()


def native_fs_no_layer_error():
    """The STL viewer's orientation-marker widget owns a renderer at layer 1.
    Exiting native fullscreen must not orphan it above the window's layer
    count -- that logged 'Invalid layer for renderer: not rendered' (a Win32
    popup in the real child) on every fullscreen exit. VTK output is routed to
    stderr in embed mode, so assert the message never appears there."""
    if os.name != "nt":
        print("      (skipped: native_fs is Windows-only)")
        return
    import ctypes
    c = Child([sys.executable, os.path.join(HERE, "viewers", "stl_viewer.py"),
               STL, GCODE, "--render", "exact"])
    try:
        c.expect("hello")
        c.send(t="resize", w=640, h=480)
        c.expect("frame")
        c.send(t="native_fs", on=True, x=120, y=120, w=480, h=360, exact=True)
        assert c.expect("native_fs")["on"] is True
        time.sleep(0.4)
        c.send(t="native_fs", on=False)
        assert c.expect("native_fs", timeout=10.0)["on"] is False
        # keep streaming a beat so the post-teardown renders happen
        c.expect("frame", timeout=10.0)
        time.sleep(0.4)
        c.send(t="quit")
        c.proc.wait(timeout=15)
        err = c.stderr_tail()
        assert "Invalid layer" not in err, \
            f"layer error on fullscreen exit:\n{err[-600:]}"
    finally:
        c.close()


# ---------------------------------------------------------------------------
# sidebar parity: the minimized ttk rail (gui_embed) and the maximized VTK
# sidebar (gui_viewer_shell.ViewerChrome) are two renderers of ONE control spec.
# These guard the visual contract that keeps them looking identical -- the width
# they must share and the centred card button -- so a future edit to one
# renderer that forgets the other is caught here, not by eye.
# ---------------------------------------------------------------------------
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


def sidebar_width_parity():
    """The shared FLOOR: both renderers start from viewer_sidebar_spec.SIDEBAR_W
    and autofit UPWARD to their own content, so this guards that neither renderer
    hardcodes a DIFFERENT floor (which would let the bounded readout fit one and
    clip the other on a standard-DPI machine)."""
    import viewer_sidebar_spec
    import gui_viewer_shell
    import gui_embed
    assert gui_viewer_shell.SIDEBAR_WIDTH_PX == viewer_sidebar_spec.SIDEBAR_W, \
        gui_viewer_shell.SIDEBAR_WIDTH_PX
    assert gui_embed.RAIL_W == viewer_sidebar_spec.SIDEBAR_W, gui_embed.RAIL_W


def _mk_chrome(width, height, labels=None, hud_lines=3, footer_lines=7):
    """Build an offscreen ViewerChrome with a live-sim-like control set at a given
    window size, laid out. Returns (chrome, shell_module)."""
    import vtk
    import gui_viewer_shell as shell
    win = vtk.vtkRenderWindow()
    win.SetOffScreenRendering(1)
    win.SetSize(width, height)
    ren = vtk.vtkRenderer()
    win.AddRenderer(ren)
    iren = vtk.vtkRenderWindowInteractor()
    iren.SetRenderWindow(win)
    ch = shell.ViewerChrome(win, iren, ren, title="Simulation",
                            subtitle="print.gcode", dark=True)
    hud = vtk.vtkTextActor()
    hud.SetInput("\n".join(f"line {i}" for i in range(hud_lines)))
    ch.set_hud(hud, max_lines=hud_lines)
    ch.section("View")
    for lbl in (labels or ["Snap top-down", "Hide view cube", "Hide build plate",
                           "Hide printhead", "Show deviation",
                           "Physics-deposited scaffold"]):
        ch.button(lambda l=lbl: l, lambda: None)
    ch.footer_section("Keys")
    ch.footer_label("\n".join(f"k{i}  action" for i in range(footer_lines)),
                    lines=footer_lines)
    ch.layout()
    win.Render()
    return ch, shell


def vtk_sidebar_compresses_to_fit():
    """The fullscreen VTK sidebar shrinks its whole rhythm + type when the stacked
    rows would overflow a short window, so nothing runs off the bottom -- and it
    stays at full size when the window is tall. Regression guard for the vertical
    autofit."""
    if not _can_render():
        print("  (skipped: no offscreen VTK)")
        return
    # tall window: everything fits -> no compression (design sizes intact)
    tall, shell = _mk_chrome(1000, 900)
    import gui_viewer_shell as gvs
    total1 = 16 + tall._content_height1() + 8
    assert total1 <= 900, f"content {total1} should fit 900"
    assert abs(tall._title.GetTextProperty().GetFontSize()
               - gvs._TITLE_SIZE) < 1, "compressed a window that fits"
    # short window: must compress AND every placed row stays on-screen (y >= 0)
    short, _ = _mk_chrome(1000, 460)
    assert (16 + short._content_height1() + 8) > 460, "test window should overflow"
    assert short._title.GetTextProperty().GetFontSize() < gvs._TITLE_SIZE, \
        "short window did not compress the title"
    lowest = min(r["hit"][1] for r in (short._top_rows + short._footer_rows)
                 if r.get("hit") is not None)
    assert lowest >= 0, f"a row ran off the bottom (y={lowest})"


def vtk_sidebar_autofits_width():
    """The VTK sidebar grows WIDER than the floor to fit a very long label, and
    stays at the floor for short labels (autofit lengthwise), capped so it never
    eats the scene."""
    if not _can_render():
        print("  (skipped: no offscreen VTK)")
        return
    import viewer_sidebar_spec as spec
    narrow, _ = _mk_chrome(1200, 900, labels=["Play", "Stop"])
    assert narrow._sidebar_w == spec.SIDEBAR_W, \
        f"short labels should stay at the floor, got {narrow._sidebar_w}"
    wide, _ = _mk_chrome(1200, 900, labels=[
        "An extremely long viewer control label well past the floor width"])
    assert wide._sidebar_w > spec.SIDEBAR_W, \
        f"long label did not widen the sidebar ({wide._sidebar_w})"
    assert wide._sidebar_w <= int(0.42 * 1200) + 1, \
        f"sidebar width not capped ({wide._sidebar_w})"


def _fs_rail_host(viewer_embed_mod, w=1400, h=900):
    """An EmbedHost wired just enough for _build_fs_chrome: an offscreen window
    and a live-sim-shaped EmbedChrome (transport roles, switches, a plain
    button, knob steppers + reset, a readout provider, a key legend)."""
    import vtk
    win = vtk.vtkRenderWindow()
    win.SetOffScreenRendering(1)
    win.SetSize(w, h)
    ren = vtk.vtkRenderer()
    win.AddRenderer(ren)
    iren = vtk.vtkRenderWindowInteractor()
    iren.SetRenderWindow(win)
    host = object.__new__(viewer_embed_mod.EmbedHost)
    host.window, host.iren, host.renderer = win, iren, ren
    host._dark = False
    host._hello_transport = True
    st = {"jet": False, "playing": False, "speed": 1.0, "v": 4.0,
          "pressed": []}
    host.state_provider = lambda: {"playing": st["playing"],
                                   "speed": st["speed"]}
    c = viewer_embed_mod.EmbedChrome(host, "Live simulation", "x.gcode")
    host.chrome = c
    press = lambda name: (lambda: st["pressed"].append(name))  # noqa: E731
    c.section("Playback")
    c.button(lambda: "Play", press("play"), role="play")
    c.button(lambda: "Slower", press("slower"), role="slower")
    c.button(lambda: "Faster", press("faster"), role="faster")
    c.button(lambda: "Restart", press("restart"), role="restart")
    c.section("Scaffold")
    c.button(lambda: "Show deposition", press("dep"),
             toggle=("Deposition", lambda: True))
    c.section("Simulation")
    c.button(lambda: "Hide jet" if st["jet"] else "Show jet",
             lambda: st.__setitem__("jet", not st["jet"]),
             toggle=("Jet", lambda: st["jet"]))
    c.section("View")
    c.button(lambda: "Snap top-down", press("snap"))
    c.button(lambda: "+", lambda: st.__setitem__("v", st["v"] + 0.5),
             role="knob:voltage:+")
    c.button(lambda: "-", lambda: st.__setitem__("v", st["v"] - 0.5),
             role="knob:voltage:-")
    c.button(lambda: "Reset params", press("reset"), role="knob:reset")

    hud = vtk.vtkTextActor()                   # the viewer's HUD actor
    hud.SetInput("hud")
    st["hud"] = hud
    c.set_hud(hud)
    c.set_readout(lambda: ([{"g": "Print", "k": "Line", "v": "12"},
                            {"g": "Process", "k": "Voltage",
                             "v": f"{st['v']:.1f} kV", "knob": "voltage"}],
                           "hud"))
    c.footer_section("Keys")
    c.footer_label("Space  play / pause\nR  restart")
    return host, st


def fullscreen_rail_parity():
    """Native fullscreen draws the EMBEDDED rail's layout, from the same spec
    (viewer_embed._build_fs_chrome -> gui_viewer_shell.RailChrome): the
    controls split into the SAME tabs the ttk rail shows
    (viewer_sidebar_spec.rail_tabs), on/off controls are switches, the
    transport roles leave the sidebar for the bottom bar, the readout is a
    table whose knob rows carry -/+ steppers, and every one of those answers a
    click. RED against the old builder, which drew every control -- Slower,
    Faster and Restart included -- as one labelled card per row with no tabs."""
    if not _can_render():
        print("      (skipped: no offscreen VTK)")
        return
    import viewer_embed
    _fs_rail_checks(viewer_embed)


def _fs_rail_checks(viewer_embed_mod):
    import viewer_sidebar_spec as spec
    host, st = _fs_rail_host(viewer_embed_mod)
    fs = host._build_fs_chrome()
    host.window.Render()
    labels = [a.GetInput().strip() for a, _fn, _cb in fs.buttons]
    for role_label in ("Slower", "Faster", "Restart"):
        assert role_label not in labels, \
            f"{role_label!r} drawn as a sidebar button: {labels}"
    want = list(spec.rail_tabs(host.chrome.controls, True))
    assert want == ["Scene", "Simulation", "View"], want
    assert list(getattr(fs, "_segs", {})) == want, \
        f"fullscreen tabs {list(getattr(fs, '_segs', {}))} != rail {want}"

    def click(x, y):
        fs.iren.SetEventInformation(int(x), int(y), 0, 0, chr(0), 0, None)
        fs.iren.LeftButtonPressEvent()
        fs.iren.LeftButtonReleaseEvent()

    def hit_of(entry_pred):
        return next(h for h in fs._hits if entry_pred(h[4]))

    # a tab click shows that tab's items and hides the rest
    sx, sy, sw, sh, _e = hit_of(lambda e: e[0] is fs._segs["Simulation"]["text"])
    click(sx + sw / 2, sy + sh / 2)
    assert fs._tab == "Simulation", fs._tab
    jet = fs._items["Simulation"][0]
    assert jet["kind"] == "toggle" and jet["name"].GetVisibility(), jet["kind"]
    assert not fs._items["Scene"][0]["name"].GetVisibility(), \
        "a hidden tab's switch is still drawn"
    # a switch row toggles its control and the switch repaints lit
    jx, jy, jw, jh, _e = hit_of(lambda e: e is jet["entry"])
    click(jx + 5, jy + jh / 2)
    assert st["jet"] is True and jet["on"] is True, (st["jet"], jet["on"])
    # the readout table: the value arrives, and + steps the knob
    volt = next(r for r in fs._table["rows"] if r["k"] == "Voltage")
    assert volt["val"].GetInput() == "4.0 kV", volt["val"].GetInput()
    plus = volt["knob"][1]
    px, py, pw, ph, _e = hit_of(lambda e: e is plus["entry"])
    click(px + pw / 2, py + ph / 2)
    assert volt["val"].GetInput() == "4.5 kV", volt["val"].GetInput()
    # a transient status covers the table; the card keeps its place
    st["hud"].SetInput("Computing deviation…")
    fs.refresh_labels()
    assert fs._table["status"].GetVisibility() and \
        not volt["key"].GetVisibility(), "status did not replace the table"
    # the transport bar carries the panel's restart / slower / faster
    fs.transport(lambda: 1.0, 0.0, 5.0, lambda v: None,
                 text_fn=lambda: "0:01 / 0:05",
                 play_label_fn=lambda: "Play", on_play=lambda: None)
    fs.layout()
    for b in fs._transport_extra:
        bx, by, bw, bh = b["hit"]
        assert bx > fs._sidebar_w, f"{b['role']} card inside the sidebar"
        click(bx + bw / 2, by + bh / 2)
    assert st["pressed"] == ["restart", "slower", "faster"], st["pressed"]
    host.window.Finalize()


def vtk_button_centered():
    """The maximized (VTK) card button's label is centred on BOTH axes -- the
    'not centred in x or y' bug. layout() anchors it at the row mid-point, so
    the text property must carry centred justification for that to read right."""
    if not _can_render():
        print("      (skipped: no VTK render window)")
        return
    import vtk
    import gui_viewer_shell
    ren = vtk.vtkRenderer()
    win = vtk.vtkRenderWindow()
    win.SetOffScreenRendering(1)
    win.AddRenderer(ren)
    win.SetSize(640, 480)
    iren = vtk.vtkGenericRenderWindowInteractor()
    iren.SetRenderWindow(win)
    chrome = gui_viewer_shell.ViewerChrome(win, iren, ren, title="t")
    actor = chrome.button(lambda: "Play", lambda: None)
    tp = actor.GetTextProperty()
    assert tp.GetJustification() == vtk.VTK_TEXT_CENTERED, tp.GetJustification()
    assert tp.GetVerticalJustification() == vtk.VTK_TEXT_CENTERED, \
        tp.GetVerticalJustification()
    win.Finalize()


def rail_button_style_centered():
    """The minimized (ttk) rail's control button is the bordered ViewerBtn.TButton
    with CENTRED text -- the twin of the VTK card button. If it silently reverts
    to the flat, left-aligned Nav.TButton the two states drift apart, so pin the
    anchor here (and confirm it is genuinely distinct from Nav)."""
    try:
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
        root.withdraw()
    except Exception as exc:  # noqa: BLE001 -- no display -> skip, not fail
        print(f"      (skipped: no Tk display: {exc})")
        return
    try:
        import gui_fonts
        import gui_modern_theme
        gui_fonts.init_fonts(root)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        gui_modern_theme._ModernThemeMixin._style_modern(
            style, gui_modern_theme.LIGHT)
        anchor = str(style.lookup("ViewerBtn.TButton", "anchor"))
        assert anchor == "center", f"ViewerBtn.TButton anchor={anchor!r}"
        # genuinely a different style from the flat, left-aligned nav button
        assert str(style.lookup("Nav.TButton", "anchor")) in ("w", "west")
    finally:
        root.destroy()


def stream_makes_no_layout_calls():
    """A playing clock sends `state` and `readout` many times a second, and the
    panel must make no grid or raise call on them. gui_curtain reads each one as
    the click's layout still arriving: Play raised the cover and the picture sat
    frozen under it for the whole 4 s timeout, with the clock running beneath.

    Comparator: gui_curtain's own `_changes_layout`, on the same execution trace
    `watch` installs. RED, observed: with _sync_state_tag's bare tag.grid() and
    _set_readout's unconditional tkraise restored, this fails at 120 calls."""
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001 -- no display -> skip, not fail
        print(f"      (skipped: no Tk display: {exc})")
        return
    import gui_curtain
    import gui_embed
    import gui_fonts
    gui_fonts.init_fonts(root)

    class _App:
        pass
    app = _App()
    app.root = root
    app.dark_mode = tk.BooleanVar(value=False)
    try:
        panel = gui_embed.ViewerPanel(root, app, "unified", "idle")
        panel.pack(fill="both", expand=True)
        root.update()
        seen = []

        def traced(cmd, _op):
            if gui_curtain._changes_layout(cmd):
                seen.append(cmd)
        name = root.register(traced)
        for cmd in ("grid", "pack", "place", "raise", "lower"):
            root.tk.call("trace", "add", "execution", cmd, "enter", name)
        for i in range(60):
            panel._on_event("state", {"time": i * 0.1, "total": 9.0,
                                      "playing": True, "speed": 1.0})
            panel._on_event("readout", {"rows": [], "status": ""})
        assert not seen, f"{len(seen)} layout calls, first {seen[:2]}"
        panel._on_event("readout", {"rows": [], "status": "Computing"})
        panel._on_event("readout", {"rows": [], "status": ""})
        assert len(seen) == 2, f"a status must still raise: {seen}"
    finally:
        root.destroy()


def panel_through_real_session():
    """The Viewer page's rail, driven end to end: a real ViewerPanel launching a
    real player through a real EmbedSession (the GUI's own pipe, not a replay of
    its messages into _on_event). Guards the redesign's rejected state:

      * the rail and the timeline row exist while idle, at the width they keep
        when a viewer is open and after it closes
      * the readout fills (it sat on "Starting…" when EmbedSession's message
        allow-list dropped the "readout" stream)
      * the switches start at the scene's truth and a click moves them (the
        same allow-list dropped "states")
      * Play flips the Paused/Playing chip at once and it stays flipped

    RED, observed: with EmbedSession._poll restored to its allow-list
    ("hello", "labels", "values", "hud", "state", "native_fs", "request") this
    fails on "readout never filled"."""
    try:
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001 -- no display -> skip, not fail
        print(f"      (skipped: no Tk display: {exc})")
        return
    import gui_embed
    import gui_fonts
    import gui_modern_theme
    gui_fonts.init_fonts(root)
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    gui_modern_theme._ModernThemeMixin._style_modern(style,
                                                     gui_modern_theme.LIGHT)
    root.geometry("1100x760+20+20")

    class _App:
        pass
    app = _App()
    app.root = root
    app.dark_mode = tk.BooleanVar(value=False)
    panel = None

    def pump(until, secs):
        end = time.time() + secs              # computed once: bounded
        while time.time() < end:
            root.update()
            if until():
                return True
            time.sleep(0.01)
        return until()

    try:
        panel = gui_embed.ViewerPanel(root, app, "unified", "empty")
        panel.pack(fill="both", expand=True)
        pump(lambda: False, 0.5)
        width = int(panel.rail.cget("width"))

        def layout(when):
            assert int(panel.rail.cget("width")) == width,                 f"{when}: rail width {panel.rail.cget('width')} != idle {width}"
            assert panel.transport.place_info(), f"{when}: timeline row not shown"
        layout("idle")
        assert panel._toggles, "idle: no switches in the rail"
        assert str(panel._tbtn["play"].cget("state")) == "disabled"

        panel.launch([sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"),
                      GCODE, "--render", "tube:6", "--static"])
        time_key = ("Print", "Time")
        filled = pump(lambda: panel._readout_vals.get(time_key) is not None
                      and panel._readout_vals[time_key].get()
                      != gui_embed._EMPTY_VALUE, 90.0)
        assert filled, "readout never filled"
        layout("open")
        assert panel._state_tag.cget("text") == "Paused"
        by_name = {c["toggle"]: panel._toggles[c["id"]]
                   for c in panel._hello["controls"]
                   if c.get("toggle") and c["id"] in panel._toggles}
        assert by_name["Travels"].get() is True, "travels open shown"
        if "Printhead" in by_name:
            assert by_name["Printhead"].get() is False, "printhead opens hidden"
        by_name["Travels"].invoke()
        assert pump(lambda: by_name["Travels"].get() is False, 10.0),             "the Travels switch did not move after a click"

        panel._tbtn["play"].invoke()
        root.update()
        assert panel._state_tag.cget("text") == "Playing", "chip lagged Play"
        flips = set()
        pump(lambda: flips.add(panel._state_tag.cget("text")) and False, 1.5)
        assert flips == {"Playing"}, f"chip flicked back: {flips}"

        panel.stop()
        pump(lambda: False, 0.5)
        layout("closed")
        assert panel._readout_vals[time_key].get() == gui_embed._EMPTY_VALUE
    finally:
        if panel is not None and panel.session is not None:
            panel.stop(quiet=True)
        root.destroy()


def projection_switch_through_real_session():
    """The camera-projection row, driven end to end through a real EmbedSession:
    the player child builds it, the hello spec carries it to the GUI, a click
    crosses the pipe, the child flips the camera, and the new state streams back
    and moves the switch.

    The comparator is the SWITCH STATE, which the child derives from the live
    camera (vtk_helpers.projection_toggle reads GetParallelProjection, it does not
    cache a flag). So the switch moving is evidence the real camera changed, not
    that a boolean in the GUI was toggled.

    Two controls make that non-vacuous. It must START off -- orthographic is the
    default every viewer sets -- and it must come BACK off on a second click; a
    switch wired to a constant, or one the rail flips locally without the child
    agreeing, fails one or the other.

    RED, observed: with the `view.append((_proj_lbl, ...))` line removed from
    gcode_player._build_buttons this fails on "no Perspective row in the hello
    spec" -- the row never reaches the rail."""
    try:
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001 -- no display -> skip, not fail
        print(f"      (skipped: no Tk display: {exc})")
        return
    import gui_embed
    import gui_fonts
    import gui_modern_theme
    gui_fonts.init_fonts(root)
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    gui_modern_theme._ModernThemeMixin._style_modern(style,
                                                     gui_modern_theme.LIGHT)
    root.geometry("1100x760+20+20")

    class _App:
        pass
    app = _App()
    app.root = root
    app.dark_mode = tk.BooleanVar(value=False)
    panel = None

    def pump(until, secs):
        end = time.time() + secs              # computed once: bounded
        while time.time() < end:
            root.update()
            if until():
                return True
            time.sleep(0.01)
        return until()

    try:
        panel = gui_embed.ViewerPanel(root, app, "unified", "empty")
        panel.pack(fill="both", expand=True)
        pump(lambda: False, 0.5)
        panel.launch([sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"),
                      GCODE, "--render", "tube:6", "--static"])
        time_key = ("Print", "Time")
        assert pump(lambda: panel._readout_vals.get(time_key) is not None
                    and panel._readout_vals[time_key].get()
                    != gui_embed._EMPTY_VALUE, 90.0), "readout never filled"

        by_name = {c["toggle"]: panel._toggles[c["id"]]
                   for c in panel._hello["controls"]
                   if c.get("toggle") and c["id"] in panel._toggles}
        assert "Perspective" in by_name,             f"no Perspective row in the hello spec: {sorted(by_name)}"
        sw = by_name["Perspective"]
        # control 1: orthographic is the default, so the switch opens OFF
        assert sw.get() is False, "the projection switch did not open off (ortho)"
        sw.invoke()
        assert pump(lambda: sw.get() is True, 10.0),             "clicking Perspective never lit the switch (the child's camera did "             "not report perspective back)"
        # control 2: and it comes back, so the row is not stuck on
        sw.invoke()
        assert pump(lambda: sw.get() is False, 10.0),             "the projection switch would not go back to orthographic"
    finally:
        if panel is not None and panel.session is not None:
            panel.stop(quiet=True)
        root.destroy()


def broken_launch_exits_nonzero():
    """The crash-protection contract: a child that cannot start dies alone
    with a non-zero exit (the GUI shows its calm crashed state); it must not
    hang."""
    c = Child([sys.executable, os.path.join(HERE, "viewers", "gcode_player.py"),
               os.path.join(_TMP, "missing.gcode")])
    try:
        try:
            c.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            raise AssertionError("broken child hung instead of exiting")
        assert c.proc.returncode not in (None, 0), c.proc.returncode
    finally:
        c.close()


def main():
    print("building tiny inputs...")
    make_inputs()
    try:
        check("pure helpers (spec parse / --embed pop)", pure_helpers)
        check("value controls (slider/number export + dispatch)",
              value_controls_unit)
        check("rail layout hints (toggle / role / readout rows + status)",
              rail_layout_hints_unit)
        check("fullscreen chrome slider drag (press/scrub/release)",
              fullscreen_chrome_slider_drag)
        check("fullscreen transport bar (jump/scrub/play/refresh)",
              fullscreen_transport_bar)
        check("move coalescing unit (runs collapse, clicks survive)",
              coalesce_moves_unit)
        check("sidebar parity: rail + VTK share the floor width",
              sidebar_width_parity)
        check("VTK sidebar compresses vertically to fit a short window",
              vtk_sidebar_compresses_to_fit)
        check("VTK sidebar autofits width to the widest label",
              vtk_sidebar_autofits_width)
        check("sidebar parity: VTK card button label centred (x + y)",
              vtk_button_centered)
        check("sidebar parity: ttk rail button is the centred card style",
              rail_button_style_centered)
        check("sidebar parity: fullscreen draws the rail's tabs/switches/"
              "readout and they answer clicks", fullscreen_rail_parity)
        check("Machine off turns Printhead off; one click shows the head",
              machine_off_turns_printhead_off)
        check("stl viewer embed round-trip", stl_embed)
        check("locked player: stream, fps floor, scrub, keys",
              player_embed_stream_and_fps)
        check("static default: paused at t_end + jet toggle",
              player_static_default)
        check("view section: 'Snap top-down' present + renders on press",
              player_view_snap_top)
        check("view cube: toggle ships honest in hello + flips both ways",
              player_view_cube_toggle)
        check("view cube: a click snaps the view in the embedded panel",
              player_view_cube_snaps_on_click)
        check("build plate shown by default when a sibling exists",
              player_plate_shown_by_default)
        check("middle-button pan: no teleport on idle clock ticks",
              player_pan_no_teleport)
        check("deviation in-scene: request -> push -> recolour (U2c)",
              deviation_in_scene)
        check("deviation failure stays on the HUD past a readout refresh",
              deviation_failure_stays_on_the_hud)
        check("deviation reveal tracks the clock (scrub shrinks, stays on)",
              deviation_reveal_tracks_clock)
        check("micrograph in-scene: push -> overlay + slider section (U2d)",
              micrograph_in_scene)
        check("live-edit request from the viewer (U2e)", live_edit_request)
        check("live player: knob key + HUD", live_embed_knobs)
        check("move flood stays responsive (coalescing)",
              move_flood_stays_responsive)
        check("native fullscreen round-trip (on/Esc/off, stream resumes)",
              native_fs_roundtrip)
        check("docked in the panel (parent/resize/fullscreen/undock)",
              docked_roundtrip)
        check("docked viewer hides behind another page (incl. relaunch)",
              docked_hides_behind_page)
        check("native fullscreen exit: no orphaned-layer error",
              native_fs_no_layer_error)
        check("a playing clock makes no layout calls in the panel",
              stream_makes_no_layout_calls)
        check("viewer page rail through a real EmbedSession",
              panel_through_real_session)
        check("camera projection switch through a real EmbedSession",
              projection_switch_through_real_session)
        check("broken launch exits non-zero (crash contract)",
              broken_launch_exits_nonzero)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    total = 30
    n_fail = len(FAILURES)
    print(f"\n{'FAIL' if n_fail else 'PASS'}: {total - n_fail}/{total} tests passed")
    if n_fail:
        print("failed:", ", ".join(FAILURES))
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
