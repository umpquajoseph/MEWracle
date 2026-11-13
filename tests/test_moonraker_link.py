#!/usr/bin/env python3
"""Self-running tests for moonraker_link.py (the stdlib Moonraker client + poller).

No pytest -- prints PASS/FAIL per test, exits non-zero on any failure, matching the
other suites. Everything runs against the in-file FakeMoonraker rig, so no rig and
no network are needed. `python tests/test_moonraker_link.py --serve [--port N]` runs that
same fake rig in the foreground so the real GUI can be pointed at localhost.

Covers: info/query round-trips, g-code-script + upload encoding (byte-identity,
start flag, newlines/spaces), print-control + estop verbs, the three pause/resume
wirings (a stock `[pause_resume]` rig; a rig whose PAUSE macro is a bare `M25` + a
2 mm z-hop, where RESUME answers 200 and does nothing and every press hops again;
and the renamed-builtin escape hatch), the api-key 401 gate,
heater/sensor/limit discovery, the poller (moving position, temps, console delta,
cadence) and its reconnect-after-flaky recovery, and the CSV recorder.
"""
import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import os
import queue
import sys
import tempfile
import time

import moonraker_link as ml

_FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        _FAILS.append(name)


def _collect(out_q, seconds, want=None):
    """Drain snapshots for up to `seconds`; stop early once `want(snap)` is True."""
    snaps = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            snap = out_q.get(timeout=0.05)
        except queue.Empty:
            continue
        snaps.append(snap)
        if want and want(snap):
            break
    return snaps


# -- client basics ---------------------------------------------------------- #
def test_info_and_query():
    with ml.FakeMoonraker() as fake:
        c = fake.client()
        info = c.info()
        check("info state ready", info.get("state") == "ready")
        st = c.query({"motion_report": ["live_position"], "toolhead": ["homed_axes"]})
        check("query returns motion_report", "motion_report" in st)
        check("query returns homed", st.get("toolhead", {}).get("homed_axes") == "xyz")
        pos = ml._xyz(st["motion_report"]["live_position"])
        check("live_position is xyz tuple", pos is not None and len(pos) == 3)


def test_objects_list():
    with ml.FakeMoonraker() as fake:
        objs = fake.client().objects_list()
        check("objects_list has motion_report", "motion_report" in objs)


def test_gcode_encoding_preserved():
    with ml.FakeMoonraker() as fake:
        c = fake.client()
        script = "G91\nG1 X5.0 F300\nG90"
        c.gcode(script)
        got = fake.state.scripts[-1]
        check("gcode newlines+spaces round-trip", got == script, f"got {got!r}")


def test_upload_byte_identity_and_start():
    data = os.urandom(2048)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "part7.gcode")
        with open(p, "wb") as fh:
            fh.write(data)
        with ml.FakeMoonraker() as fake:
            c = fake.client()
            name = c.upload(p, start=False)
            check("upload returns filename", name == "part7.gcode", name)
            fname, blob, started = fake.state.uploads[-1]
            check("upload bytes identical", blob == data)
            check("upload not started by default", started is False)
            check("upload leaves state ready", fake.state.state == "ready")
            c.upload(p, start=True)
            _, _, started2 = fake.state.uploads[-1]
            check("upload start=True flagged", started2 is True)
            check("upload start=True prints", fake.state.state == "printing")


def test_print_control_verbs():
    with ml.FakeMoonraker() as fake:
        c = fake.client()
        c.print_start("job.gcode")
        check("start -> printing", fake.state.state == "printing")
        c.print_pause()
        check("pause -> paused", fake.state.state == "paused")
        c.print_resume()
        check("resume -> printing", fake.state.state == "printing")
        c.print_cancel()
        check("cancel -> cancelled", fake.state.state == "cancelled")


def _z(client):
    st = client.query({"gcode_move": ["gcode_position"]})
    return ml._xyz(st["gcode_move"]["gcode_position"])[2]


def test_pause_hops_z_once_with_pause_resume():
    """A rig WITH [pause_resume]: the PAUSE macro's z-hop happens once (Klipper
    refuses the second PAUSE outright), is_paused tracks the pause, and RESUME puts
    the head back where it stopped."""
    with ml.FakeMoonraker() as fake:
        c = fake.client()
        check("pause_resume object is published", c.has_object("pause_resume"))
        c.print_start("job.gcode")
        z0 = _z(c)
        c.print_pause()
        check("pause lifts Z by the macro's hop", abs(_z(c) - (z0 + 2.0)) < 1e-6,
              "z %.3f -> %.3f" % (z0, _z(c)))
        check("is_paused is set", c.query({"pause_resume": ["is_paused"]})
              ["pause_resume"]["is_paused"] is True)
        c.print_pause()               # a second press must NOT hop again
        check("second pause does not hop again", abs(_z(c) - (z0 + 2.0)) < 1e-6,
              "z is now %.3f" % _z(c))
        c.print_resume()
        check("resume restarts the job", fake.state.state == "printing")
        # read the state directly: a query would advance the print past the restore
        check("resume puts Z back (RESTORE_GCODE_STATE MOVE=1)",
              abs(fake.state.pos[2] - z0) < 1e-6,
              "z is %.3f, expected %.3f" % (fake.state.pos[2], z0))


def test_bare_m25_pause_is_the_silent_resume_failure():
    """The rig-observed failure, end to end: a PAUSE macro that calls `M25` and lifts
    Z leaves print_stats "paused" with NO pause_resume flag. RESUME then declines
    (HTTP 200, a console line, no motion) and every further PAUSE lifts Z again --
    while `M24` straight to the SD stream is what actually resumes it."""
    with ml.FakeMoonraker() as fake:
        fake.state.pause_resume = False        # no [pause_resume] section
        c = fake.client()
        check("no pause_resume object to poll", not c.has_object("pause_resume"))
        c.print_start("job.gcode")
        z0 = _z(c)
        c.print_pause()
        check("job reports paused", fake.state.state == "paused")
        check("pause lifted Z", abs(_z(c) - (z0 + 2.0)) < 1e-6)
        c.print_resume()
        check("RESUME answers 200 but does nothing", fake.state.state == "paused")
        check("...and says so on the console",
              any("resume aborted" in e["message"] for e in fake.state.console))
        c.print_pause()                        # the +2 mm per press the rig showed
        check("every extra pause lifts Z again", abs(_z(c) - (z0 + 4.0)) < 1e-6,
              "z is %.3f, expected %.3f" % (_z(c), z0 + 4.0))
        c.gcode("M24")
        check("M24 resumes the SD stream", fake.state.state == "printing")


def test_query_fails_whole_request_on_unknown_object():
    """Klipper rejects a COMBINED query outright when one object is missing, which is
    why the poller must be told whether pause_resume exists rather than guessing --
    a wrong guess would cost the entire telemetry feed, not one field."""
    with ml.FakeMoonraker() as fake:
        fake.state.pause_resume = False
        c = fake.client()
        try:
            c.query({"toolhead": ["homed_axes"], "pause_resume": ["is_paused"]})
            check("unknown object rejects the query", False, "no error raised")
        except ml.MoonrakerError as exc:
            check("unknown object rejects the query", exc.status == 400, str(exc))
        check("the same query without it still works",
              c.query({"toolhead": ["homed_axes"]}).get("toolhead") is not None)


def test_poller_carries_is_paused():
    with ml.FakeMoonraker() as fake:
        c = fake.client()
        c.print_start("job.gcode")
        c.print_pause()
        out_q = queue.Queue()
        poller = ml.LinkPoller(c, out_q, fast_hz=50, pause_resume=True)
        poller.start()
        try:
            snaps = _collect(out_q, 3.0, want=lambda s: s.ok and s.is_paused is True)
        finally:
            poller.stop()
        check("snapshot carries is_paused", any(s.is_paused is True for s in snaps))
        # and a poller told there is no such object must not ask for it
        with ml.FakeMoonraker() as bare:
            bare.state.pause_resume = False
            out2 = queue.Queue()
            p2 = ml.LinkPoller(bare.client(), out2, fast_hz=50, pause_resume=False)
            p2.start()
            try:
                s2 = _collect(out2, 3.0, want=lambda s: s.ok)
            finally:
                p2.stop()
            check("feed survives a rig with no pause_resume",
                  any(s.ok for s in s2),
                  "errors: %s" % [s.error for s in s2][:2])
            check("is_paused stays unknown there",
                  all(s.is_paused is None for s in s2))


def test_gcode_help_exposes_renamed_builtins():
    with ml.FakeMoonraker() as fake:
        fake.state.base_commands = ("PAUSE_BASE", "RESUME_BASE")
        c = fake.client()
        help_ = c.gcode_help()
        check("help lists RESUME", "RESUME" in help_)
        check("help lists the renamed builtin", "RESUME_BASE" in help_)


def test_emergency_stop():
    with ml.FakeMoonraker() as fake:
        c = fake.client()
        c.emergency_stop()
        check("estop sets flag", fake.state.estopped is True)
        check("estop -> error state", fake.state.state == "error")


def _pos(client):
    st = client.query({"motion_report": ["live_position"]})
    return ml._xyz(st["motion_report"]["live_position"])


def test_fake_obeys_commands():
    """The fake is a real simulator: idle it sits STILL, jog/home move it, a print
    advances, and pause/cancel/estop stop the motion. This is what makes the GUI's
    controls visibly work against localhost (the bug the first cut had -- constant
    motion that ignored every command)."""
    with ml.FakeMoonraker() as fake:
        c = fake.client()
        p0 = _pos(c)
        time.sleep(0.15)
        p1 = _pos(c)
        check("idle toolhead sits still", p0 == p1, f"{p0} -> {p1}")
        # a relative jog moves it by exactly the delta
        c.gcode("G91\nG1 X5.000 F600\nG90")
        px = _pos(c)
        check("jog moves the toolhead +5 in X", abs(px[0] - (p0[0] + 5.0)) < 1e-6,
              f"{p0} -> {px}")
        # home zeroes it
        c.gcode("G28")
        check("home zeroes the toolhead", _pos(c) == (0.0, 0.0, 0.0))
        # a print advances progress and the toolhead moves
        c.print_start("demo.gcode")
        time.sleep(0.25)
        a = _pos(c)
        time.sleep(0.25)
        b = _pos(c)
        check("printing toolhead moves", a != b, f"{a} -> {b}")
        prog = c.query({"virtual_sdcard": ["progress"]})["virtual_sdcard"]["progress"]
        check("print progress advances past 0", prog > 0.0, str(prog))
        # pause freezes motion
        c.print_pause()
        time.sleep(0.05)
        q0 = _pos(c)
        time.sleep(0.2)
        q1 = _pos(c)
        check("pause freezes the toolhead", q0 == q1, f"{q0} -> {q1}")
        # estop stops everything
        c.emergency_stop()
        check("estop -> error + not printing",
              fake.state.state == "error" and not fake.state.printing)


def test_api_key_gate():
    with ml.FakeMoonraker(require_key="secret123") as fake:
        no_key = fake.client()
        raised = None
        try:
            no_key.info()
        except ml.MoonrakerError as exc:
            raised = exc
        check("no key -> MoonrakerError", raised is not None)
        check("no key -> 401 status", raised is not None and raised.status == 401)
        with_key = fake.client(api_key="secret123")
        check("key -> info ok", with_key.info().get("state") == "ready")


def test_transport_error_status_none():
    # nothing listening on this port -> connection refused -> status None
    c = ml.MoonrakerClient("127.0.0.1", 9, timeout=0.5)
    raised = None
    try:
        c.info()
    except ml.MoonrakerError as exc:
        raised = exc
    check("dead host -> MoonrakerError", raised is not None)
    check("dead host -> status None", raised is not None and raised.status is None)


def test_heater_discovery_and_limits():
    with ml.FakeMoonraker() as fake:
        c = fake.client()
        check("discover_heaters", c.discover_heaters() == ["extruder", "heater_bed"])
        check("discover_sensors has chamber", "chamber" in c.discover_sensors())
        lim = c.heater_limits()
        check("extruder max_temp", lim.get("extruder") == 300.0)
        check("bed max_temp", lim.get("heater_bed") == 120.0)


# -- multipart unit --------------------------------------------------------- #
def test_multipart_round_trip():
    data = os.urandom(64)
    body, ctype = ml._encode_multipart({"root": "gcodes", "print": "true"},
                                       "file", "x.gcode", data)
    parts = ml._decode_multipart(ctype, body)
    check("multipart file bytes", parts["file"]["value"] == data)
    check("multipart filename", parts["file"]["filename"] == "x.gcode")
    check("multipart field value", parts["print"]["value"] == b"true")
    check("multipart root field", parts["root"]["value"] == b"gcodes")
    # binary-safe: a payload whose own bytes END in CRLF (or a lone CR/LF) must
    # round-trip intact -- the old .strip(b"\r\n") ate them (a data-dependent flake).
    for tail in (b"\r\n", b"\n", b"\r", b"\r\n\r\n"):
        blob = b"G1 X10\r\nG1 Y10" + tail
        body2, ctype2 = ml._encode_multipart({"root": "gcodes"}, "file", "j.gcode", blob)
        got = ml._decode_multipart(ctype2, body2)["file"]["value"]
        check("multipart preserves trailing %r" % tail, got == blob,
              "%r != %r" % (got, blob))


# -- poller ----------------------------------------------------------------- #
def test_poller_moving_position_and_temps():
    with ml.FakeMoonraker() as fake:
        # the fake is a real simulator: idle it sits STILL, so start a print (the
        # toolhead then traces its path) and set a heater target (it drifts up).
        setup = fake.client()
        setup.print_start("demo.gcode")
        setup.gcode("SET_HEATER_TEMPERATURE HEATER=extruder TARGET=80")
        out_q = queue.Queue()
        poller = ml.LinkPoller(fake.client(), out_q,
                               temp_objects=["extruder", "heater_bed", "chamber"],
                               fast_hz=50, slow_hz=50, safety_period=0.1)
        poller.start()
        try:
            snaps = _collect(out_q, 0.7)
        finally:
            poller.stop()
        oks = [s for s in snaps if s.ok]
        check("poller produced ok snapshots", len(oks) > 0)
        check("poller has position", any(s.pos for s in oks))
        with_temp = [s for s in oks if s.temps]
        check("poller reads temps", len(with_temp) > 0)
        if with_temp:
            ext = with_temp[-1].temps.get("extruder")
            check("extruder target reflects the set", ext is not None and ext[1] == 80.0,
                  str(ext))
            check("extruder actual is a live number",
                  ext is not None and isinstance(ext[0], float))
            check("sensor chamber has no target",
                  with_temp[-1].temps.get("chamber") == (24.0, None))
        with_power = [s for s in oks if s.powers]
        check("poller reads heater PWM power", len(with_power) > 0)
        if with_power:
            pw = with_power[-1].powers.get("extruder")
            check("extruder PWM is a 0..1 fraction",
                  isinstance(pw, float) and 0.0 <= pw <= 1.0, str(pw))
            check("sensor chamber has no PWM power",
                  "chamber" not in with_power[-1].powers)
        positions = [s.pos for s in oks if s.pos]
        moved = len({(round(p[0], 3), round(p[1], 3)) for p in positions}) > 1
        check("toolhead moves while printing", moved)
        check("homed axes reported", any(s.homed == "xyz" for s in oks))


def test_poller_console_delta():
    with ml.FakeMoonraker() as fake:
        out_q = queue.Queue()
        poller = ml.LinkPoller(fake.client(), out_q, fast_hz=50,
                               slow_hz=50, safety_period=0.05)
        poller.start()
        try:
            # fixed window (no early break) so we see the seed line delivered ONCE
            snaps = _collect(out_q, 0.6)
        finally:
            poller.stop()
        with_seed = [s for s in snaps if any("Ready" in line for line in s.console)]
        check("console seed line delivered exactly once", len(with_seed) == 1,
              str(len(with_seed)))
        # the seed line must not repeat forever -- later snapshots have empty console
        empties = [s for s in snaps if s.ok and not s.console]
        check("console does not repeat every tick", len(empties) > 0)


def test_poller_reconnects_after_flaky():
    with ml.FakeMoonraker(flaky=3) as fake:
        out_q = queue.Queue()
        poller = ml.LinkPoller(fake.client(timeout=0.5), out_q,
                               fast_hz=50, slow_hz=50, safety_period=5.0)
        poller.start()
        try:
            snaps = _collect(out_q, 3.0, want=lambda s: s.ok and s.pos)
        finally:
            poller.stop()
        saw_fail = any(not s.ok for s in snaps)
        saw_ok = any(s.ok and s.pos for s in snaps)
        check("poller reported failures during flaky", saw_fail)
        check("poller recovered to ok after flaky", saw_ok)
        if snaps:
            check("failure carried an error string",
                  all(s.error for s in snaps if not s.ok))


# -- relative (work-origin) coordinates + G92 ------------------------------- #
def test_g92_zeroes_relative_coords():
    """gcode_move.gcode_position is the 'relative' coord the machine-link page shows
    under the absolute one; a G92 must zero it while the raw live_position is
    unchanged (the fake now models the work-origin offset)."""
    with ml.FakeMoonraker() as fake:
        c = fake.client()
        c.gcode("G90\nG1 X20 Y15 Z3")          # move to an absolute spot
        st = c.query({"motion_report": ["live_position"],
                      "gcode_move": ["gcode_position"]})
        live = ml._xyz(st["motion_report"]["live_position"])
        gpos = ml._xyz(st["gcode_move"]["gcode_position"])
        check("before G92: rel == abs", live == gpos == (20.0, 15.0, 3.0),
              "%s %s" % (live, gpos))
        c.gcode("G92 X0 Y0 Z0")                 # set work origin here
        st = c.query({"motion_report": ["live_position"],
                      "gcode_move": ["gcode_position"]})
        live2 = ml._xyz(st["motion_report"]["live_position"])
        gpos2 = ml._xyz(st["gcode_move"]["gcode_position"])
        check("after G92: rel zeroed", gpos2 == (0.0, 0.0, 0.0), str(gpos2))
        check("after G92: abs unchanged", live2 == (20.0, 15.0, 3.0), str(live2))
    # a single-axis zero touches only that axis (fresh rig, nothing zeroed yet)
    with ml.FakeMoonraker() as fake:
        c = fake.client()
        c.gcode("G90\nG1 X40 Y15 Z3")
        c.gcode("G92 X0")
        st = c.query({"gcode_move": ["gcode_position"]})
        g = ml._xyz(st["gcode_move"]["gcode_position"])
        check("single-axis zero rebases X only", g == (0.0, 15.0, 3.0), str(g))


def test_poller_reports_gpos():
    """The poller carries gcode_position through as LinkSnapshot.gpos."""
    with ml.FakeMoonraker() as fake:
        out_q = queue.Queue()
        poller = ml.LinkPoller(fake.client(), out_q, fast_hz=50, safety_period=5.0)
        poller.start()
        try:
            snaps = _collect(out_q, 0.5, want=lambda s: s.ok and s.gpos)
        finally:
            poller.stop()
        check("snapshot carries gpos", any(s.gpos for s in snaps if s.ok))


# -- host resolution caching (the real-rig "frozen live view" bug) ---------- #
def test_hostname_resolved_once_and_cached():
    """A bare hostname must be resolved to an IPv4 ONCE and reused -- on Windows an
    mDNS lookup per request costs ~2 s and returns an IPv6 link-local first, which
    stalled the poller to a snapshot every several seconds (frozen view + state)."""
    calls = {"n": 0}
    real = ml.socket.getaddrinfo

    def counting_getaddrinfo(host, port, *a, **kw):
        calls["n"] += 1
        return [(ml.socket.AF_INET, ml.socket.SOCK_STREAM, 6, "", ("10.9.8.7", port))]

    ml.socket.getaddrinfo = counting_getaddrinfo
    try:
        c = ml.MoonrakerClient("mewron.local", 7125)
        check("resolves hostname to cached IPv4", c._endpoint_host() == "10.9.8.7")
        c._endpoint_host(); c._endpoint_host()
        check("hostname resolved only once (cached)", calls["n"] == 1, str(calls["n"]))
        # a transport error clears the cache so a new lease re-resolves
        c._resolved_host = None
        check("re-resolves after cache cleared", c._endpoint_host() == "10.9.8.7")
        check("second resolution counted", calls["n"] == 2, str(calls["n"]))
        # an IP literal must NOT trigger a lookup at all
        calls["n"] = 0
        lit = ml.MoonrakerClient("127.0.0.1", 7125)
        check("IP literal used verbatim", lit._endpoint_host() == "127.0.0.1")
        check("IP literal skips getaddrinfo", calls["n"] == 0, str(calls["n"]))
    finally:
        ml.socket.getaddrinfo = real


def test_poller_speed_from_scalar_velocity():
    """Real Klipper's motion_report.live_velocity is a scalar mm/s (not a vector);
    the poller must still report a moving speed (the fake now mirrors that shape)."""
    with ml.FakeMoonraker() as fake:
        fake.client().print_start("demo.gcode")
        out_q = queue.Queue()
        poller = ml.LinkPoller(fake.client(), out_q, fast_hz=50, slow_hz=50,
                               safety_period=5.0)
        poller.start()
        try:
            snaps = _collect(out_q, 0.7, want=lambda s: s.ok and s.speed)
        finally:
            poller.stop()
        moving = [s for s in snaps if s.ok and s.speed]
        check("poller reports a non-zero speed while printing",
              any(s.speed and s.speed > 0 for s in moving),
              str([round(s.speed, 2) for s in moving[:3]]))


# -- recorder --------------------------------------------------------------- #
def test_recorder_writes_rows():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "trace.csv")
        rec = ml.TelemetryRecorder(p, heaters=["extruder", "heater_bed"])
        rec.write(ml.LinkSnapshot(t=1.0, ok=True, pos=(1.0, 2.0, 3.0),
                                  vel=(0.5, 0.0, 0.0), speed=0.5, state="printing",
                                  progress=0.1, temps={"extruder": (78.0, 80.0),
                                                       "heater_bed": (39.0, 40.0)}))
        rec.write(ml.LinkSnapshot(t=2.0, ok=True, pos=(2.0, 2.0, 3.0), state="printing"))
        rec.close()
        with open(p) as fh:
            lines = fh.read().strip().splitlines()
        check("recorder wrote header + 2 rows", len(lines) == 3, str(len(lines)))
        check("header has heater columns", "extruder_actual" in lines[0])
        check("row 1 has extruder target", ",80.0" in lines[1] or ",80" in lines[1])


def test_file_management():
    with ml.FakeMoonraker() as fake:
        c = fake.client()
        files = c.list_files()
        check("list returns seeded files",
              len(files) >= 5 and all(isinstance(f, dict) and "path" in f
                                      and "size" in f for f in files),
              str(len(files)))
        with tempfile.TemporaryDirectory() as d:
            payload = b"; unit test\nG28\nG1 X1 Y2 F600\n"
            p = os.path.join(d, "unit_upload.gcode")
            with open(p, "wb") as fh:
                fh.write(payload)
            c.upload(p)
            names = [f["path"] for f in c.list_files()]
            check("uploaded file appears in the list", "unit_upload.gcode" in names)
            # download it back -- byte identical
            dst = os.path.join(d, "roundtrip.gcode")
            c.download_file("unit_upload.gcode", dst)
            with open(dst, "rb") as fh:
                check("download round-trips the exact bytes", fh.read() == payload)
            # delete it -- gone from the list
            c.delete_file("unit_upload.gcode")
            names2 = [f["path"] for f in c.list_files()]
            check("deleted file removed from the list",
                  "unit_upload.gcode" not in names2)
            # deleting a missing file raises a 404
            try:
                c.delete_file("does_not_exist.gcode")
                check("missing delete raises", False)
            except ml.MoonrakerError as exc:
                check("missing delete raises 404", exc.status == 404)
            # downloading a missing file raises a 404
            try:
                c.download_file("does_not_exist.gcode", os.path.join(d, "x"))
                check("missing download raises", False)
            except ml.MoonrakerError as exc:
                check("missing download raises 404", exc.status == 404)


def test_every_failure_is_a_moonraker_error():
    """The page's worker threads catch `MoonrakerError` and nothing else, so
    anything else the client raises kills the thread and leaves the page on
    "Connecting..." / "Uploading..." for good. Found by a
    release-audit campaign: a host typed with a space raised http.client.InvalidURL,
    an upload whose local file had gone raised FileNotFoundError, and a JSON
    reply of the wrong shape (another service on 7125) raised AttributeError
    inside the poller, which died without one error snapshot.
    RED on cc329c7: the first two raised the bare errors; the poller emitted
    nothing and was dead."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def kind(fn):
        try:
            fn()
            return "no error"
        except ml.MoonrakerError:
            return "MoonrakerError"
        except Exception as exc:  # noqa: BLE001 -- the defect being tested
            return type(exc).__name__

    got = kind(lambda: ml.MoonrakerClient("mew ron.local", timeout=1).info())
    check("a host with a space is a MoonrakerError", got == "MoonrakerError", got)
    missing = os.path.join(tempfile.gettempdir(),
                           "no_such_upload_%d.gcode" % os.getpid())
    with ml.FakeMoonraker() as fake:
        got = kind(lambda: fake.client().upload(missing))
    check("an unreadable upload file is a MoonrakerError",
          got == "MoonrakerError", got)

    class Foreign(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({"result": {"status": {
                "motion_report": "n/a", "print_stats": [1, 2]}}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        do_POST = do_GET

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Foreign)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    q = queue.Queue()
    p = ml.LinkPoller(ml.MoonrakerClient("127.0.0.1", srv.server_address[1],
                                         timeout=1), q, backoff_max=0.2)
    p.start()
    try:
        snaps = _collect(q, 1.5, want=lambda s: not s.ok)
        check("a reply of the wrong shape is reported, not fatal",
              any(not s.ok and s.error for s in snaps) and p.is_alive(),
              "snapshots=%d alive=%s" % (len(snaps), p.is_alive()))
    finally:
        p.stop()
        srv.shutdown()
        srv.server_close()


def test_upload_waits_longer_than_a_status_query():
    """An upload is the one request whose reply can legitimately take longer
    than the 4 s status timeout: Moonraker answers only once the file is
    written. The campaign's 50 MB upload to a server that answered 6 s after
    reading it failed at 4.08 s with "timed out" -- the file had arrived.
    `download_file` already waits 60 s for the same reason. Here the server
    answers 5 s after the body, past the client's 4 s. RED on cc329c7:
    "cannot reach ...: timed out" at 4.0 s."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class SlowToAnswer(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            time.sleep(5.0)                 # writing + indexing a big file
            body = json.dumps({"result": {"item": {"path": "big.gcode"}}})
            body = body.encode()
            self.send_response(201)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), SlowToAnswer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    path = os.path.join(tempfile.mkdtemp(), "big.gcode")
    with open(path, "w") as fh:
        fh.write("G1 X1 Y1\n" * 1000)
    try:
        client = ml.MoonrakerClient("127.0.0.1", srv.server_address[1])
        try:
            got = client.upload(path)
        except ml.MoonrakerError as exc:
            got = str(exc)
        check("an upload outlasts the 4 s status timeout",
              got == "big.gcode", got)
    finally:
        srv.shutdown()
        srv.server_close()


def main():
    tests = [
        test_info_and_query,
        test_objects_list,
        test_gcode_encoding_preserved,
        test_upload_byte_identity_and_start,
        test_print_control_verbs,
        test_pause_hops_z_once_with_pause_resume,
        test_bare_m25_pause_is_the_silent_resume_failure,
        test_query_fails_whole_request_on_unknown_object,
        test_poller_carries_is_paused,
        test_gcode_help_exposes_renamed_builtins,
        test_emergency_stop,
        test_fake_obeys_commands,
        test_api_key_gate,
        test_transport_error_status_none,
        test_heater_discovery_and_limits,
        test_multipart_round_trip,
        test_poller_moving_position_and_temps,
        test_poller_console_delta,
        test_poller_reconnects_after_flaky,
        test_g92_zeroes_relative_coords,
        test_poller_reports_gpos,
        test_hostname_resolved_once_and_cached,
        test_poller_speed_from_scalar_velocity,
        test_recorder_writes_rows,
        test_file_management,
        test_every_failure_is_a_moonraker_error,
        test_upload_waits_longer_than_a_status_query,
    ]
    for t in tests:
        print(t.__name__)
        t()
    print()
    if _FAILS:
        print(f"FAILED ({len(_FAILS)}): {', '.join(_FAILS)}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    if "--serve" in sys.argv:
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--serve", action="store_true")
        ap.add_argument("--port", type=int, default=7125)
        args = ap.parse_args()
        ml.serve_forever(args.port)
    else:
        main()
