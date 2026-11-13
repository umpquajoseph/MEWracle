#!/usr/bin/env python3
"""test_machine_link_yank.py -- the live-view YANK regression (no display needed
for the first four tests; the last one opens a real ModernApp).

The bug: `motion_report.live_position` is not a continuous physical quantity. A
`G28` makes Klipper `set_position` before each endstop-seeking move and again at the
trigger, REDEFINING the coordinate frame, so the reported position steps to places
the machine cannot be while nothing teleports. The live view mapped those steps
straight onto the machine bodies and rendered them as travel.

Everything here is driven by the REAL 4 Hz capture off the lab rig --
`_HOME_EVENTS` below is that capture, and `moonraker_link._FakeState` replays the
same sequence, so the localhost rig reproduces the defect the old instant-G28 fake
never could. Three earlier attempts at neighbouring features went offline-green /
rig-broken, so every test here is written to FAIL on the unfixed code: the ungated
baselines are asserted too, and if they ever stop yanking the test says so rather
than passing vacuously.

Run:  python tests/test_machine_link_yank.py   (PASS/FAIL per test, exit 1 on failure).
"""
import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import gcode_player

FAILURES = []

FPS = 60.0
POLL = 0.250                      # the measured Moonraker feed period (4 Hz)
# (axis, reported live_position, reported live_velocity) -- the rig capture. Note
# every step is far larger than the reported speed can account for, and most report
# a speed of ZERO: that mismatch is the signature of a frame redefinition.
HOME_EVENTS = [("z", -64.47, 0.0), ("z", 48.9, 10.0), ("z", 110.8, 4.1),
               ("y", -40.0, 0.0), ("y", 22.3, 40.0), ("y", 80.0, 0.0),
               ("x", -55.0, 0.0), ("x", 44.4, 40.0), ("x", 110.0, 0.0)]
TRAVEL = {"x": (0.0, 120.0), "y": (0.0, 120.0), "z": (0.0, 120.0)}   # Voron 0.2
# The view's own speed ceiling, expressed per rendered frame (+15% slack for frame
# timing). This is the real invariant: a re-seat is rate-limited, so the worst frame
# is bounded no matter how far the feed ran away. For scale, the un-gated homing
# burst renders 11.7 mm/frame -- 700 mm/s on screen.
MAX_FRAME_JUMP = gcode_player._LiveFeed.CATCHUP_SPEED / FPS * 1.15


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


class Clock:
    """A deterministic stand-in for time.time() so the glide is reproducible."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def worst_frame_jump(messages, clock, span=None):
    """Push `messages` (livepos payloads, `ts`-stamped) into a real `_LiveFeed` at
    their sample times while stepping a 60 fps renderer, exactly as the machine-live
    tick does. Returns (worst single-frame movement in mm, total rendered path mm)."""
    feed = gcode_player._LiveFeed(clock=clock)
    msgs = sorted(messages, key=lambda m: m["ts"])
    end = msgs[-1]["ts"] + feed.RENDER_DELAY + (span or 1.0)
    clock.t = msgs[0]["ts"]
    i, worst, total, prev = 0, 0.0, 0.0, None
    while clock.t < end:
        while i < len(msgs) and msgs[i]["ts"] <= clock.t:
            feed.push(msgs[i])
            i += 1
        p = feed.sample()
        if p is not None:
            feed.head = p                      # what _place_machine_live does
            if prev is not None:
                d = float(np.linalg.norm(p - prev))
                worst = max(worst, d)
                total += d
            prev = np.array(p, float)
        clock.t += 1.0 / FPS
    return worst, total


def homing_messages(hold, t0=1000.0, settle=3.0):
    """The captured homing burst as the GUI would forward it. `hold` decides whether
    the motion gate is engaged -- i.e. fixed vs unfixed behaviour, same input."""
    msgs, pos, t = [], {"x": 11.0, "y": 19.0, "z": 0.0}, t0
    for _ in range(4):                                  # steady, parked, pre-home
        msgs.append(dict(ts=t, hold=0, **pos))
        t += POLL
    for axis, value, speed in HOME_EVENTS:
        pos = dict(pos)
        pos[axis] = value
        for _ in range(2):                              # each state held ~2 samples
            msgs.append(dict(ts=t, hold=1 if hold else 0, spd=speed, **pos))
            t += POLL
    pos = {"x": 0.0, "y": 0.0, "z": 0.0}                # homed, settled
    # NOTE: bound computed ONCE. Testing against msgs[-1]["ts"] inside the loop is a
    # non-terminating condition (the bound advances with every append) and the list
    # grows until the machine is out of memory.
    for _ in range(int(settle / POLL) + 1):
        msgs.append(dict(ts=t, hold=0, spd=0.0, **pos))
        t += POLL
    return msgs


# -- 1. the interpolator refuses to sweep a dropout -------------------------- #
def test_interp_holds_across_a_dropout():
    buf = [(0.0, np.zeros(3)), (2.0, np.array([80.0, 0.0, 0.0]))]
    mid = gcode_player._interp_buf(buf, 1.0, 0.12, None)
    check("without max_seg it sweeps the gap", abs(mid[0] - 40.0) < 1e-9,
          "got %.3f" % mid[0])
    held = gcode_player._interp_buf(buf, 1.0, 0.12, 0.75)
    check("with max_seg it holds at the older sample", abs(held[0]) < 1e-9,
          "got %.3f" % held[0])
    # a NORMAL 4 Hz pair must still interpolate -- the guard must not disable smoothing
    ok = [(0.0, np.zeros(3)), (POLL, np.array([2.0, 0.0, 0.0]))]
    v = gcode_player._interp_buf(ok, POLL / 2, 0.12, 0.75)
    check("a normal sample pair still interpolates", abs(v[0] - 1.0) < 1e-9,
          "got %.3f" % v[0])


# -- 2. the travel-volume test (the frame-redefinition detector) ------------- #
def test_travel_volume_detector():
    import gui_machine_link

    class Probe:
        _ML_GATE_MARGIN = gui_machine_link._MachineLinkMixin._ML_GATE_MARGIN
        _ml_axis_limits = TRAVEL
        _ml_outside_travel = gui_machine_link._MachineLinkMixin._ml_outside_travel

    p = Probe()
    for axis, value, _spd in HOME_EVENTS:
        pos = [11.0, 19.0, 0.0]
        pos["xyz".index(axis)] = value
        inside = TRAVEL[axis][0] <= value <= TRAVEL[axis][1]
        if not inside:
            check("homing %s=%.1f flagged" % (axis, value), p._ml_outside_travel(pos))
    # THE regression the earlier rejected prototype died on: a legitimate jog to a
    # NEGATIVE WORK coordinate. gcode_position (5, -3, 0) with a work origin of
    # (11, 19, 0) is machine (16, 16, 0) -- comfortably inside the travel volume.
    check("legitimate jog to work (5,-3,0) NOT flagged",
          not p._ml_outside_travel([16.0, 16.0, 0.0]))
    check("in-volume extremes not flagged",
          not p._ml_outside_travel([0.0, 0.0, 0.0])
          and not p._ml_outside_travel([120.0, 120.0, 120.0]))
    # unknown limits must never guess
    class NoLimits(Probe):
        _ml_axis_limits = {}
    check("no configured limits -> detector off",
          not NoLimits()._ml_outside_travel([-999.0, 0.0, 0.0]))


# -- 3. the captured homing burst, gated vs ungated -------------------------- #
def test_homing_burst_is_not_rendered_as_travel():
    clock = Clock()
    bad, bad_total = worst_frame_jump(homing_messages(hold=False), clock)
    # the unfixed path, for scale -- if this ever stops yanking the test is vacuous
    check("UNGATED baseline still yanks (test is not vacuous)",
          bad > 3.0 and bad_total > 400.0,
          "worst %.2f mm/frame, %.0f mm rendered" % (bad, bad_total))
    clock = Clock()
    good, good_total = worst_frame_jump(homing_messages(hold=True), clock)
    check("gated: no frame jump above %.1f mm" % MAX_FRAME_JUMP,
          good <= MAX_FRAME_JUMP,
          "worst %.3f mm/frame (ungated %.2f)" % (good, bad))
    # the head must still ARRIVE at the homed position -- holding is not ignoring
    check("gated: total rendered travel collapses",
          good_total < 0.35 * bad_total,
          "%.0f mm vs ungated %.0f mm" % (good_total, bad_total))


def test_reseat_lands_on_the_feed():
    """After the hold releases, the view must actually reach the homed position --
    a glide that never arrives would be a different bug."""
    clock = Clock()
    msgs = homing_messages(hold=True)
    feed = gcode_player._LiveFeed(clock=clock)
    msgs = sorted(msgs, key=lambda m: m["ts"])
    clock.t = msgs[0]["ts"]
    i = 0
    end = msgs[-1]["ts"] + feed.RENDER_DELAY + 1.0
    while clock.t < end:
        while i < len(msgs) and msgs[i]["ts"] <= clock.t:
            feed.push(msgs[i])
            i += 1
        p = feed.sample()
        if p is not None:
            feed.head = p
        clock.t += 1.0 / FPS
    check("view settles at the homed position",
          float(np.linalg.norm(feed.head)) < 0.01,
          "ended at %s" % np.round(feed.head, 4))
    check("catch-up finished (not stuck mid-pursuit)", feed.catching_up is False)
    check("hold released", feed.hold is False)


# -- 4. the feed dropout (the 'first jog after idle' yank) ------------------- #
def test_dropout_during_motion_is_not_swept():
    def stream(gap):
        msgs, t, x = [], 1000.0, 10.0
        for _ in range(6):                       # moving at 40 mm/s
            msgs.append(dict(ts=t, x=x, y=10.0, z=0.0, hold=0))
            t += POLL
            x += 40.0 * POLL
        t += gap                                 # the blackout, head still moving
        x += 40.0 * gap
        for _ in range(8):
            msgs.append(dict(ts=t, x=x, y=10.0, z=0.0, hold=0))
            t += POLL
            x += 40.0 * POLL
        return msgs

    smooth, _ = worst_frame_jump(stream(0.0), Clock())
    check("no-gap baseline is smooth", smooth < 1.0, "worst %.3f mm" % smooth)
    for gap in (1.0, 2.0, 4.0):
        w, _ = worst_frame_jump(stream(gap), Clock())
        check("a %.0f s dropout draws no jump > %.1f mm" % (gap, MAX_FRAME_JUMP),
              w <= MAX_FRAME_JUMP, "worst %.3f mm" % w)


# -- 5. end to end through the REAL page against the REAL fake rig ----------- #
def test_real_gui_path_holds_during_homing():
    """The one that matters. Everything above proves the mechanism; this proves the
    WIRING -- the real ModernApp page, the real LinkPoller, the real forward rule in
    _ml_handle_snapshot -- against a FakeMoonraker replaying the captured homing.

    The three earlier failed attempts on this branch were all 'green in a harness,
    broken on the rig', because the harness bypassed this path. So the livepos
    payloads are captured at the panel boundary (exactly what would cross to the
    child) and then rendered through a real _LiveFeed."""
    import tkinter as tk
    import gui_fonts
    import mewracle_gui
    import moonraker_link as ml

    root = tk.Tk()
    root.withdraw()
    gui_fonts.init_fonts(root)
    app = mewracle_gui.ModernApp(root)
    root.update()
    fake = ml.FakeMoonraker().start()
    fake.state.home_seconds = 2.0
    sent = []

    class PanelSpy:
        def send_child(self, **kw):
            sent.append(dict(kw))

    def pump(cond, timeout=10.0):
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            root.update()
            if cond():
                return True
            time.sleep(0.02)
        return cond()

    try:
        app.ml_host.set("127.0.0.1")
        app.ml_port.set(str(fake.port))
        app.ml_api_key.set("")
        app._ml_autolaunch_viewer = False
        app._ml_connect()
        assert pump(lambda: app._ml_connected, 10.0), app.ml_conn_status.get()
        # the travel volume must have been discovered -- without it the external-G28
        # detector is silently off, which is exactly the kind of quiet regression
        # that produced 'green here, broken there'
        check("axis limits discovered from the printer",
              app._ml_axis_limits.get("x") == (0.0, 120.0),
              "got %r" % (app._ml_axis_limits,))
        app._ml_launched = True
        app._ml_panel = PanelSpy()
        assert pump(lambda: len(sent) >= 2, 5.0), "no livepos forwarded"
        n0 = len(sent)
        app._ml_home("all")
        assert pump(lambda: any(m.get("hold") for m in sent[n0:]), 8.0), \
            "the view was never held during homing"
        check("state line explains the held view",
              "held" in app.ml_state_text.get().lower(),
              "state line reads %r" % app.ml_state_text.get())
        # let homing finish and the gate settle
        assert pump(lambda: (not app._ml_gate_reasons) and len(sent) > n0 + 12, 20.0), \
            "gate never released (reasons=%r)" % (app._ml_gate_reasons,)
        held = [m for m in sent[n0:] if m.get("hold")]
        check("hold was actually asserted during homing", len(held) >= 2,
              "%d held samples" % len(held))
        # Now RENDER what the child would have received. Include a few PRE-home
        # samples: without them the feed starts at its zero head, which happens to
        # equal the fake's homed position, and the catch-up leg is never exercised.
        clock = Clock()
        start = max(0, n0 - 4)
        msgs = [dict(m, ts=m["ts"]) for m in sent[start:] if "x" in m]
        base = msgs[0]["ts"]
        for m in msgs:
            m["ts"] = clock.t + (m["ts"] - base)
        worst, moved = worst_frame_jump(msgs, clock)
        check("no yank rendered from the real forwarded stream",
              worst <= MAX_FRAME_JUMP, "worst %.3f mm/frame" % worst)
        # ...and the view must still ARRIVE (a view that renders nothing would pass
        # the jump check vacuously)
        check("the view still travels to the homed position", moved > 5.0,
              "only %.2f mm rendered in total" % moved)
        # and the same stream WITHOUT the gate must still yank, or this proves nothing
        clock2 = Clock()
        ungated = [dict(m, hold=0) for m in msgs]
        base = ungated[0]["ts"]
        for m in ungated:
            m["ts"] = clock2.t + (m["ts"] - base)
        bad, _ = worst_frame_jump(ungated, clock2)
        check("the same stream ungated DOES yank (guard is load-bearing)",
              bad > 3.0, "ungated worst %.3f mm/frame" % bad)
    finally:
        try:
            app._ml_panel = None
            app._ml_disconnect()
        except Exception:  # noqa: BLE001
            pass
        fake.stop()
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass


# -- 6. Generate must not disturb the live machine view --------------------- #
def test_generate_does_not_relaunch_the_machine_view():
    """A successful Generate relaunches every open embed panel onto the fresh
    artifacts (gui_embed._on_finished). The machine view shows the PRINTER, not the
    print -- it has no fresh artifacts -- so relaunching it killed and respawned the
    machine-live child, dropping the position feed and the user's camera, on every
    Generate that happened to run while connected."""
    import tkinter as tk
    import gui_fonts
    import mewracle_gui

    root = tk.Tk()
    root.withdraw()
    gui_fonts.init_fonts(root)
    app = mewracle_gui.ModernApp(root)
    root.update()
    try:
        relaunched = []

        class FakeSession:
            def alive(self):
                return True

            def destroy(self):
                pass

        for role, panel in app._embed_panels.items():
            panel.session = FakeSession()
            panel.relauncher = (lambda r=role: relaunched.append(r))
        # the machine panel must survive a successful run untouched...
        app._on_finished(0)
        check("Generate does not relaunch the live machine view",
              "machine" not in relaunched, "relaunched %r" % (relaunched,))
        # ...while the normal viewer panels still refresh onto the new artifacts,
        # or this guard would be hiding a different regression
        check("other panels still relaunch", "unified" in relaunched,
              "relaunched %r" % (relaunched,))
    finally:
        for panel in app._embed_panels.values():
            panel.session = None
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass


TESTS = [
    ("interp_holds_across_a_dropout", test_interp_holds_across_a_dropout),
    ("travel_volume_detector", test_travel_volume_detector),
    ("homing_burst_is_not_rendered_as_travel",
     test_homing_burst_is_not_rendered_as_travel),
    ("reseat_lands_on_the_feed", test_reseat_lands_on_the_feed),
    ("dropout_during_motion_is_not_swept", test_dropout_during_motion_is_not_swept),
    ("real_gui_path_holds_during_homing", test_real_gui_path_holds_during_homing),
    ("generate_does_not_relaunch_the_machine_view",
     test_generate_does_not_relaunch_the_machine_view),
]

if __name__ == "__main__":
    for name, fn in TESTS:
        print(name)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            FAILURES.append(name)
            print(f"  FAIL  {name}: {exc!r}")
    print("\n" + ("ALL PASS" if not FAILURES else "FAILURES: %s" % FAILURES))
    sys.exit(1 if FAILURES else 0)
