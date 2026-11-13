#!/usr/bin/env python3
"""moonraker_link.py -- headless Klipper/Moonraker client for the Machine-link page.

The network half of the "digital-twin" link between MEWracle and a real MEWron running
Klipper. It talks to the Pi's **Moonraker** HTTP API (the same server Fluidd/Mainsail
use) over plain ethernet, with the Python STANDARD LIBRARY only -- `urllib.request`,
no `requests`, no websocket library, preserving the no-new-dependency posture.

Four public pieces, all Tk/VTK-free so they unit-test without a display:

  * `MoonrakerClient` -- synchronous request wrapper: query printer objects, upload +
    start g-code, run g-code script, the print-control verbs, emergency stop, the
    console store, proc-stats, and heater/sensor/limit discovery. Optional `X-Api-Key`.
  * `LinkSnapshot` -- one immutable telemetry frame. Fast fields refresh every tick;
    slow fields are polled less often and CARRIED FORWARD, so every snapshot the GUI
    receives is complete.
  * `LinkPoller` -- a daemon thread polling at tiered cadences, pushing snapshots onto
    a `queue.Queue` and reconnecting with capped backoff. The GUI drains the queue on
    the Tk thread, so the Tk loop never blocks on the network.
  * `TelemetryRecorder` -- append-only CSV of snapshots for the lab notebook.

Plus `FakeMoonraker`, a stdlib `ThreadingHTTPServer` mimicking those endpoints
(scripted moving toolhead, recorded scripts + uploads, optional api-key / flaky modes).
It backs the whole test suite AND runs standalone via
`python tests/test_moonraker_link.py --serve`, so the real GUI can be pointed at `localhost`
with no rig on the bench. HV / pressure are deliberately NOT modelled -- they are not
Klipper objects on this rig, and the poller is discovery-driven, so if they are ever
added to printer.cfg they appear automatically.

Pause note: `/printer/print/pause` and `/printer/print/resume` are NOT primitives --
Moonraker runs the g-code commands *named* `PAUSE` and `RESUME`, which on most rigs are
`[gcode_macro]` wrappers. So this module exposes what a caller needs to tell the shapes
apart: `pause_resume.is_paused` on the snapshot, `has_object` (Klipper fails a WHOLE
combined query on one unknown object, so an optional object must be confirmed before
it is polled), and `gcode_help` (the renamed builtins a wrapper macro publishes).
`FakeMoonraker` models all three wirings.
Frame note: `motion_report.live_position` is in Klipper g-code coordinates -- the same
frame the exported file commands -- so a live-position feed can reuse the sim player's
`--origin`/`--machine`. `homing_origin` is surfaced so alignment can be verified.
verified on the rig.
"""

if __name__ == "__main__":  # run as a script: put the program's folders on sys.path
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "engine"))
    import app_paths  # noqa: E402,F401

import bisect
import csv
import http.client
import io
import json
import queue
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


# ==========================================================================
# Client
# ==========================================================================

def _is_timeout(exc):
    """True if `exc` is (or wraps) a socket read timeout rather than a failure to
    reach the host at all. urllib raises `socket.timeout` (an alias of the builtin
    `TimeoutError` since 3.10) directly on a read timeout, but wraps a connect-phase
    timeout in `URLError`, so the `.reason` is unwrapped too."""
    if isinstance(exc, urllib.error.URLError):
        exc = exc.reason
    return isinstance(exc, (socket.timeout, TimeoutError))


class MoonrakerError(RuntimeError):
    """Any Moonraker request failure. `status` is the HTTP code when there was a
    response (401, 404, 500 ...), or None for a transport error (host down,
    connection refused, timeout). `detail` is the printer's own error message
    when its reply carried one (Klipper's "Must home axis first", say), else
    ""."""

    def __init__(self, message, status=None, detail=""):
        super().__init__(message)
        self.status = status
        self.detail = detail


class MoonrakerClient:
    """Synchronous Moonraker HTTP client (stdlib urllib only).

    `host` may be a bare hostname ("mewron.local"), with or without a scheme;
    `port` defaults to Moonraker's 7125 (the browser reaches port 80 via nginx,
    but a desktop client talks to Moonraker directly). `api_key`, if given, is
    sent as the `X-Api-Key` header on every request.
    """

    def __init__(self, host, port=7125, api_key=None, timeout=4.0):
        host = (host or "").strip()
        # tolerate a pasted "http://host:7125" or "host" alike
        if "://" in host:
            parsed = urllib.parse.urlparse(host)
            host = parsed.hostname or host
            if parsed.port:
                port = parsed.port
        self.host = host
        self.port = int(port)
        self.api_key = (api_key or "").strip() or None
        self.timeout = float(timeout)
        self.base = "http://%s:%d" % (self.host, self.port)
        # Cached IPv4 address for `self.host`. A bare hostname like "mewron.local"
        # is re-resolved by urllib on EVERY request; on Windows that mDNS lookup
        # costs ~2 s and returns an unusable IPv6 link-local first, so the poller
        # stalls to a snapshot every several seconds and the live view / state look
        # frozen (the fake rig is a raw IP, so tests never saw it). We resolve once,
        # cache the IPv4, and connect straight to it -- ~0.02 s/request. Cleared on
        # any transport error so a changed DHCP lease re-resolves on reconnect.
        self._resolved_host = None

    # -- low-level ---------------------------------------------------------

    def _endpoint_host(self):
        """The host to actually connect to: a cached IPv4 address for `self.host`
        (so every request skips a fresh, slow, IPv6-first mDNS/DNS lookup). An IP
        literal is used as-is; a name that fails to resolve falls back to the raw
        host (let urllib try) without caching."""
        if self._resolved_host:
            return self._resolved_host
        host = self.host
        try:                       # already an IPv4 literal? use it directly.
            socket.inet_aton(host)
            self._resolved_host = host
            return host
        except OSError:
            pass
        try:
            infos = socket.getaddrinfo(host, self.port, socket.AF_INET,
                                       socket.SOCK_STREAM)
            if infos:
                self._resolved_host = infos[0][4][0]
                return self._resolved_host
        except OSError:
            pass
        return host

    def _request(self, method, path, *, body=None, content_type=None,
                 raw=False, timeout=None):
        """Issue one request; return the parsed JSON `result` (or the whole
        payload if there is no `result` key). With `raw=True` return the response
        bytes verbatim (for file downloads, which are not JSON). `timeout` overrides
        the client default (downloads want longer). Raises MoonrakerError on any
        failure, with `.status` set when the server answered."""
        url = "http://%s:%d%s" % (self._endpoint_host(), self.port, path)
        headers = {}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        if content_type:
            headers["Content-Type"] = content_type
        data = body if method == "POST" else None
        if method == "POST" and data is None:
            data = b""  # force a POST even with no body
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                    req, timeout=self.timeout if timeout is None else timeout) as resp:
                resp_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                payload = json.loads(exc.read().decode("utf-8", "replace"))
                detail = payload.get("error", {}).get("message", "") if isinstance(payload, dict) else ""
            except Exception:
                pass
            raise MoonrakerError(
                "HTTP %d for %s%s" % (exc.code, path, (": " + detail) if detail else ""),
                status=exc.code, detail=detail,
            )
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            # Drop the cached IP so the next attempt re-resolves -- but ONLY for a
            # genuine connection-level failure (host down, refused, DHCP moved). A
            # READ timeout is not evidence the host moved: it means the request was
            # accepted and the server is still working on it, which is the NORMAL
            # shape of a long blocking command (Klipper holds the gcode mutex for the
            # whole of a G28, ~20 s on this rig, far past the 4 s default). Clearing
            # the cache there forced the POLLER's next request through a ~2 s Windows
            # mDNS re-resolve, and a feed gap while the head is MOVING is rendered as
            # a yank (the interpolator sweeps the whole stale segment in real time).
            if not _is_timeout(exc):
                self._resolved_host = None
            raise MoonrakerError("cannot reach %s: %s" % (url, exc), status=None)
        except (http.client.HTTPException, ValueError) as exc:
            # http.client's own family, which is neither an OSError nor a
            # URLError: InvalidURL for a host typed with a space, IncompleteRead /
            # BadStatusLine for a server that drops or garbles its reply. Each
            # used to escape every caller's `except MoonrakerError` and strand
            # the page's connect thread on "Connecting...".
            raise MoonrakerError("bad exchange with %s: %s: %s"
                                 % (url, type(exc).__name__, exc), status=None)
        if raw:
            return resp_bytes
        if not resp_bytes:
            return {}
        try:
            payload = json.loads(resp_bytes.decode("utf-8", "replace"))
        except ValueError as exc:
            raise MoonrakerError("bad JSON from %s: %s" % (path, exc), status=None)
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload

    def _get(self, path):
        return self._request("GET", path)

    def _post(self, path, params=None, timeout=None):
        if params:
            path = path + "?" + urllib.parse.urlencode(params)
        return self._request("POST", path, timeout=timeout)

    # -- info / discovery --------------------------------------------------

    def info(self):
        """GET /printer/info -- the reachability + klippy-state probe."""
        return self._get("/printer/info")

    def server_info(self):
        """GET /server/info -- Moonraker's own state (klippy_state, etc.)."""
        return self._get("/server/info")

    def objects_list(self):
        """GET /printer/objects/list -> the list of available printer objects."""
        res = self._get("/printer/objects/list")
        return list(res.get("objects", [])) if isinstance(res, dict) else []

    def has_object(self, name):
        """True if Klipper exposes printer object `name`.

        Ask this BEFORE folding an optional object into a combined `query`: Klipper's
        webhook rejects the WHOLE query with "Unknown object" if any single requested
        object is missing, so blindly asking a rig that has no `[pause_resume]` for
        `pause_resume` would take the entire telemetry feed down, not just that field.
        An unreachable printer answers False (never guess a capability)."""
        try:
            return name in self.objects_list()
        except MoonrakerError:
            return False

    def gcode_help(self):
        """GET /printer/gcode/help -> {COMMAND: description} for every g-code command
        Klipper currently has registered -- macros included.

        This is how the page finds out whether the rig's `PAUSE`/`RESUME` are stock or
        are `[gcode_macro]` wrappers: a wrapper declares `rename_existing:`, which
        publishes the ORIGINAL under that new name (conventionally `PAUSE_BASE` /
        `BASE_PAUSE`), so the base command being present IS the evidence that the one
        Moonraker's endpoint calls is a wrapper. Best-effort: `{}` on any failure."""
        try:
            res = self._get("/printer/gcode/help")
        except MoonrakerError:
            return {}
        return dict(res) if isinstance(res, dict) else {}

    def query(self, objects):
        """GET /printer/objects/query. `objects` maps object-name -> list of
        fields (or None for all fields). Returns the `status` dict."""
        parts = []
        for name, fields in objects.items():
            enc = urllib.parse.quote(name, safe="")
            if fields:
                parts.append(enc + "=" + ",".join(fields))
            else:
                parts.append(enc)
        res = self._get("/printer/objects/query?" + "&".join(parts))
        return res.get("status", {}) if isinstance(res, dict) else {}

    def discover_heaters(self):
        """Settable heaters (nozzle/melt/bed) -- `heaters.available_heaters`."""
        st = self.query({"heaters": None})
        return list(st.get("heaters", {}).get("available_heaters", []))

    def discover_sensors(self):
        """Read-only temperature sensors -- `heaters.available_sensors`."""
        st = self.query({"heaters": None})
        return list(st.get("heaters", {}).get("available_sensors", []))

    def heater_limits(self):
        """Per-heater `max_temp` from `configfile.settings` (the UI clamps sets
        to these). Returns {heater_name: max_temp_float}; missing -> absent."""
        st = self.query({"configfile": ["settings"]})
        settings = st.get("configfile", {}).get("settings", {})
        out = {}
        for name, cfg in settings.items():
            if isinstance(cfg, dict) and "max_temp" in cfg:
                try:
                    out[name] = float(cfg["max_temp"])
                except (TypeError, ValueError):
                    pass
        return out

    def axis_limits(self):
        """Per-axis TRAVEL RANGE from `configfile.settings` -- {"x": (min, max), ...}
        read off `[stepper_x/y/z] position_min/position_max`. An axis missing either key
        is omitted (no guessing).

        This is the machine's own statement of where its toolhead can physically be, in
        exactly the frame `motion_report.live_position` reports, and it is what lets the
        live view tell a MOVE from a FRAME REDEFINITION: during `G28` Klipper calls
        `set_position`, so the reported position jumps OUTSIDE this range while nothing
        physical teleports. A sample outside the travel volume is not a position at all.

        Deliberately NOT a speed test: normal use tops out at ~10 mm/s while homing
        rapids imply 230-450 mm/s, so a speed threshold looks clean until a print's own
        G0 travels run fast and freeze the view mid-move."""
        st = self.query({"configfile": ["settings"]})
        settings = st.get("configfile", {}).get("settings", {})
        out = {}
        for axis in ("x", "y", "z"):
            cfg = settings.get("stepper_" + axis)
            if not isinstance(cfg, dict):
                continue
            try:
                lo = float(cfg["position_min"])
                hi = float(cfg["position_max"])
            except (KeyError, TypeError, ValueError):
                continue
            if hi > lo:
                out[axis] = (lo, hi)
        return out

    # -- g-code + control --------------------------------------------------

    def gcode(self, script, timeout=None):
        """POST /printer/gcode/script -- run raw g-code (jog, home, temps,
        overrides, console). Newlines run several commands in order.

        Klipper runs the script under the g-code mutex and only answers when it has
        finished, so a command that BLOCKS (`G28` homes synchronously -- ~20 s on
        this rig) outlives the client's short default timeout. Callers that send one
        pass a longer `timeout`; the request staying open for the duration is then a
        precise, inference-free signal that the move is still running (see
        `gui_machine_link._ml_motion_gate`)."""
        return self._post("/printer/gcode/script", {"script": script},
                          timeout=timeout)

    def emergency_stop(self):
        """POST /printer/emergency_stop -- Klipper M112. Immediate; never queue
        this behind the poller."""
        return self._post("/printer/emergency_stop")

    def print_start(self, filename):
        return self._post("/printer/print/start", {"filename": filename})

    def print_pause(self):
        return self._post("/printer/print/pause")

    def print_resume(self):
        return self._post("/printer/print/resume")

    def print_cancel(self):
        return self._post("/printer/print/cancel")

    def gcode_store(self, count=100):
        """GET /server/gcode_store -- the recent g-code console lines
        ({message, time, type})."""
        res = self._get("/server/gcode_store?count=%d" % int(count))
        return list(res.get("gcode_store", [])) if isinstance(res, dict) else []

    def proc_stats(self):
        """GET /machine/proc_stats -- Pi CPU + `throttled_state` (undervoltage /
        thermal throttle -- the silent print-quality gremlins)."""
        return self._get("/machine/proc_stats")

    # -- file upload -------------------------------------------------------

    def upload(self, path, filename=None, start=False, root="gcodes",
               timeout=60.0):
        """POST /server/files/upload -- multipart upload of a local g-code file.
        `start=True` asks Moonraker to begin printing it immediately. Returns the
        stored filename (server-side path relative to `root`).

        `timeout` is the download's, not a status query's 4 s: Moonraker answers
        only once the file is written, and a large file took longer than 4 s to
        answer after it had arrived -- reported as a failed upload. A local file
        that cannot be read is a MoonrakerError too, so the caller's one
        `except` covers it."""
        import os
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            raise MoonrakerError("cannot read %s: %s" % (path, exc), status=None)
        name = filename or os.path.basename(path)
        fields = {"root": root}
        if start:
            fields["print"] = "true"
        body, ctype = _encode_multipart(fields, "file", name, data)
        res = self._request("POST", "/server/files/upload", body=body,
                            content_type=ctype, timeout=timeout)
        if isinstance(res, dict):
            item = res.get("item", {})
            return item.get("path", name) if isinstance(item, dict) else name
        return name

    # -- file management ---------------------------------------------------

    def list_files(self, root="gcodes"):
        """GET /server/files/list -- every stored file under `root` (default
        gcodes): a list of {path, modified, size, permissions}."""
        res = self._get("/server/files/list?root=%s" % urllib.parse.quote(root))
        return list(res) if isinstance(res, list) else []

    def delete_file(self, filename, root="gcodes"):
        """DELETE /server/files/<root>/<filename> -- remove one stored file.
        Raises MoonrakerError(status=404) if it does not exist."""
        path = "/server/files/%s/%s" % (root, urllib.parse.quote(filename))
        return self._request("DELETE", path)

    def download_file(self, filename, dest_path, root="gcodes", timeout=60.0):
        """GET /server/files/<root>/<filename> -- fetch the raw bytes and write them
        to `dest_path` (returned). Longer timeout than a status query: g-code files
        can be many MB."""
        path = "/server/files/%s/%s" % (root, urllib.parse.quote(filename))
        data = self._request("GET", path, raw=True, timeout=timeout)
        with open(dest_path, "wb") as fh:
            fh.write(data if isinstance(data, (bytes, bytearray)) else b"")
        return dest_path


def _encode_multipart(fields, file_field, filename, filedata):
    """Build a multipart/form-data body (stdlib only -- `cgi` was removed in
    Python 3.13, so we hand-roll both sides). Returns (body_bytes, content_type)."""
    boundary = "----MEWracleBoundary7d3f9a1c"
    out = io.BytesIO()

    def w(s):
        out.write(s.encode("utf-8") if isinstance(s, str) else s)

    for key, value in fields.items():
        w("--%s\r\n" % boundary)
        w('Content-Disposition: form-data; name="%s"\r\n\r\n' % key)
        w("%s\r\n" % value)
    w("--%s\r\n" % boundary)
    w('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (file_field, filename))
    w("Content-Type: application/octet-stream\r\n\r\n")
    w(filedata)
    w("\r\n--%s--\r\n" % boundary)
    return out.getvalue(), "multipart/form-data; boundary=%s" % boundary


def _decode_multipart(content_type, body):
    """Parse a multipart/form-data body -> {field_name: {"value": bytes,
    "filename": str|None}}. Minimal, for the fake rig's upload endpoint."""
    ctype = content_type or ""
    if "boundary=" not in ctype:
        return {}
    boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
    sep = ("--" + boundary).encode("utf-8")
    out = {}
    for chunk in body.split(sep):
        # trim EXACTLY one leading + one trailing CRLF (the delimiters around each
        # part) -- never .strip(b"\r\n"), which would also eat real 0x0d/0x0a bytes
        # off the end of a binary payload (the old code's data-dependent flake).
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        if not chunk or chunk == b"--":
            continue
        if b"\r\n\r\n" not in chunk:
            continue
        head, value = chunk.split(b"\r\n\r\n", 1)
        head_txt = head.decode("utf-8", "replace")
        name = None
        filename = None
        for line in head_txt.split("\r\n"):
            if line.lower().startswith("content-disposition"):
                for tok in line.split(";"):
                    tok = tok.strip()
                    if tok.startswith("name="):
                        name = tok[5:].strip().strip('"')
                    elif tok.startswith("filename="):
                        filename = tok[9:].strip().strip('"')
        if name is not None:
            out[name] = {"value": value, "filename": filename}
    return out


# ==========================================================================
# Snapshot + poller
# ==========================================================================

@dataclass(frozen=True)
class LinkSnapshot:
    """One telemetry frame. `ok` False + `error` set means this tick failed to
    reach the printer (slow fields carry the last-good values)."""
    t: float = 0.0
    ok: bool = False
    error: Optional[str] = None
    pos: Optional[tuple] = None          # (x, y, z) mm, live_position (raw/machine)
    gpos: Optional[tuple] = None         # (x, y, z) mm, gcode_move.gcode_position
                                         # -- the coordinate G92/SET_GCODE_OFFSET zeroes
    vel: Optional[tuple] = None          # (vx, vy, vz) mm/s, live_velocity
    speed: Optional[float] = None        # scalar mm/s
    homed: str = ""                      # toolhead.homed_axes, e.g. "xyz"
    state: Optional[str] = None          # print_stats.state
    # pause_resume.is_paused -- Klipper's OWN pause flag, and the one every RESUME macro
    # guards on. NOT a restatement of `state`: a job paused by a macro calling `M25`
    # reports state "paused" with is_paused False, and in that condition `RESUME` does
    # nothing. None = the rig has no [pause_resume] section, which is the same diagnosis.
    is_paused: Optional[bool] = None
    filename: Optional[str] = None       # print_stats.filename
    # print_stats.message -- Klipper's own reason when a print ends in "error"
    # ("Move out of range: ..."); empty otherwise. Without it the page could only
    # say "State: Error", which leaves the operator nothing to act on.
    message: Optional[str] = None
    progress: Optional[float] = None     # virtual_sdcard.progress 0..1
    # The print's exact place IN THE FILE. `file_position` is the byte offset Klipper's
    # virtual_sdcard reader has reached; it advances by `len(line.encode()) + 1` after
    # EACH dispatched line, so a byte offset maps back to a source line number exactly,
    # which is what drives the live view's scaffold reveal. It is the only exact
    # print-progress signal: `progress` is a BYTE fraction (a fast travel line and a
    # slow deposition line weigh the same) and `print_duration` is a different clock.
    # Caveat the consumer must handle: this is the READER, and Klipper reads ahead until
    # ~2 s of moves are queued, so it LEADS the toolhead.
    file_position: Optional[int] = None  # virtual_sdcard.file_position (bytes)
    file_size: Optional[int] = None      # virtual_sdcard.file_size (bytes)
    speed_factor: Optional[float] = None  # gcode_move.speed_factor (M220)
    extrude_factor: Optional[float] = None  # gcode_move.extrude_factor (M221)
    temps: dict = field(default_factory=dict)   # name -> (actual, target|None)
    powers: dict = field(default_factory=dict)  # heater name -> PWM power 0..1
    throttled: Optional[dict] = None     # proc_stats throttled flags
    console: tuple = ()                   # NEW console lines since last snapshot
    homing_origin: Optional[tuple] = None  # gcode_move.homing_origin (frame check)
    cpos: Optional[tuple] = None         # (x, y, z) mm, gcode_move.position -- the last
                                         # COMMANDED position in the machine frame.
                                         # Paired with `gpos` from the same query it
                                         # gives the work-origin offset H = cpos - gpos
                                         # exactly (see `work_offset`); both are
                                         # commanded, so the difference is lag-free.


class LinkPoller(threading.Thread):
    """Daemon thread that polls a `MoonrakerClient` and pushes `LinkSnapshot`s onto
    `out_q`. It exists to feed a SMOOTH live 3-D view, so its whole shape is set by one
    measured fact about the hardware:

        A Moonraker `printer.objects.query` is gated to Klipper's status cadence -- on
        the MEWron rig one round trip costs ~250 ms and back-to-back queries top out at
        ~4 Hz, regardless of HTTP keep-alive or how many objects are asked for. No
        client trick makes it faster; a WebSocket subscription is the only lower-latency
        path, and it is future work.

    Two consequences drive the design:

      * ONE combined query per tick. Position, velocity, print state, progress,
        overrides, homed axes and every discovered temperature ride a SINGLE round trip,
        because the cost is the same whether you ask for one object or ten, and the loop
        is QUERY-paced -- it re-queries immediately, sleeping only to a small floor,
        turning the old ~2 Hz (0.25 s query plus a 0.2 s sleep) into a clean ~4 Hz and
        never starving the position feed with a separate 'slow' read.
      * The non-critical reads -- proc_stats and the g-code console store -- run on a
        SEPARATE daemon thread at a lazy cadence, so their latency NEVER blocks the
        position feed. Two threads share the client safely: each opens its own socket.

    On any error a tick is reported (ok=False) and the loop backs off, then resumes; no
    exception escapes either thread. `slow_hz` is accepted-but-ignored.
    """

    def __init__(self, client, out_q, *, temp_objects=(), pause_resume=False,
                 fast_hz=8.0, slow_hz=None, safety_period=5.0,
                 backoff_max=5.0):
        super().__init__(daemon=True, name="LinkPoller")
        self.client = client
        self.out_q = out_q
        self.temp_objects = list(temp_objects)
        # Poll `pause_resume.is_paused` only when the caller has CONFIRMED the object
        # exists (MoonrakerClient.has_object). Klipper fails a combined query outright
        # on an unknown object, so guessing here would cost the whole feed.
        self.pause_resume = bool(pause_resume)
        # a FLOOR between queries; the ~0.25 s query itself paces the loop, so this
        # only bites when the server answers fast (the fake rig) -- it caps a runaway
        # tight loop without throttling the real ~4 Hz feed.
        self.min_dt = 1.0 / float(fast_hz)
        self.safety_dt = float(safety_period)
        self.backoff_max = float(backoff_max)
        self._stop = threading.Event()
        # latest fast fields (set by _read_all each tick)
        self._pos = None
        self._gpos = None
        self._cpos = None
        self._vel = None
        self._speed = None
        # the file cursor -- FAST fields, not carried forward: they drive the live
        # view's reveal, so a stale value must read as "unknown", never as motion
        self._filepos = None
        self._filesize = None
        # slow fields, carried forward so every snapshot is complete
        self._slow = dict(homed="", state=None, is_paused=None, filename=None,
                          message=None,
                          progress=None, speed_factor=None, extrude_factor=None,
                          temps={}, powers={}, throttled=None, homing_origin=None)
        # safety-thread -> main-thread handoff (throttled flag + new console lines)
        self._safety_lock = threading.Lock()
        self._console_q = queue.Queue()
        self._console_seen = 0.0  # max gcode_store timestamp already forwarded

    def stop(self):
        self._stop.set()

    def stopped(self):
        return self._stop.is_set()

    # -- one combined telemetry read (the whole frame in one round trip) ----

    def _read_all(self):
        objs = {
            "motion_report": ["live_position", "live_velocity"],
            # `position` rides along with `gcode_position`: the PAIR is the exact
            # work-origin offset (see `work_offset`), and asking for both in the
            # same query is what makes their difference atomic.
            "gcode_move": ["speed_factor", "extrude_factor", "homing_origin",
                           "gcode_position", "position"],
            "print_stats": ["state", "filename", "message"],
            "virtual_sdcard": ["progress", "file_position", "file_size"],
            "toolhead": ["homed_axes"],
        }
        if self.pause_resume:
            objs["pause_resume"] = ["is_paused"]
        for name in self.temp_objects:
            objs[name] = ["temperature", "target", "power"]
        st = self.client.query(objs)
        mr = st.get("motion_report", {})
        gm = st.get("gcode_move", {})
        ps = st.get("print_stats", {})
        vs = st.get("virtual_sdcard", {})
        th = st.get("toolhead", {})
        self._pos = _xyz(mr.get("live_position"))
        self._gpos = _xyz(gm.get("gcode_position"))
        self._cpos = _xyz(gm.get("position"))
        self._filepos = _int_or_none(vs.get("file_position"))
        self._filesize = _int_or_none(vs.get("file_size"))
        # Real Klipper reports motion_report.live_velocity as a SCALAR magnitude
        # (mm/s), not an [vx,vy,vz] vector -- so `_xyz` returns None on hardware and
        # the vector stays unknown, but the scalar IS the speed. Both shapes handled.
        raw_vel = mr.get("live_velocity")
        self._vel = _xyz(raw_vel) if isinstance(raw_vel, (list, tuple)) else None
        if self._vel is not None:
            self._speed = (self._vel[0] ** 2 + self._vel[1] ** 2
                           + self._vel[2] ** 2) ** 0.5
        elif isinstance(raw_vel, (int, float)):
            self._speed = float(raw_vel)
        else:
            self._speed = None
        if gm.get("speed_factor") is not None:
            self._slow["speed_factor"] = gm.get("speed_factor")
        if gm.get("extrude_factor") is not None:
            self._slow["extrude_factor"] = gm.get("extrude_factor")
        ho = _xyz(gm.get("homing_origin"))
        if ho is not None:
            self._slow["homing_origin"] = ho
        self._slow["state"] = ps.get("state", self._slow["state"])
        if self.pause_resume:
            pr = st.get("pause_resume", {})
            self._slow["is_paused"] = pr.get("is_paused", self._slow["is_paused"])
        self._slow["filename"] = ps.get("filename", self._slow["filename"])
        self._slow["message"] = ps.get("message", self._slow["message"])
        if vs.get("progress") is not None:
            self._slow["progress"] = vs.get("progress")
        self._slow["homed"] = th.get("homed_axes", self._slow["homed"])
        temps = {}
        powers = {}
        for name in self.temp_objects:
            row = st.get(name, {})
            if "temperature" in row:
                temps[name] = (row.get("temperature"), row.get("target"))
            if row.get("power") is not None:
                powers[name] = row.get("power")
        if temps:
            self._slow["temps"] = temps
        if powers:
            self._slow["powers"] = powers

    # -- the separate, non-blocking safety/console reader -------------------

    def _run_safety(self):
        """proc_stats + the g-code console store, on their own thread so their
        latency never stalls the position feed. New console lines and the throttle
        flags are handed to the main loop (queue + lock). A short initial delay keeps
        the connect handshake from firing three heavy queries at once."""
        next_t = time.monotonic() + min(self.safety_dt, 0.2)
        while not self._stop.is_set():
            if time.monotonic() >= next_t:
                try:
                    stats = self.client.proc_stats()
                    with self._safety_lock:
                        self._slow["throttled"] = (
                            stats.get("throttled_state")
                            if isinstance(stats, dict) else None)
                except MoonrakerError:
                    pass
                try:
                    store = self.client.gcode_store(count=100)
                except MoonrakerError:
                    store = []
                for entry in store:
                    ts = float(entry.get("time", 0.0) or 0.0)
                    if ts > self._console_seen:
                        self._console_q.put(entry.get("message", ""))
                        self._console_seen = max(self._console_seen, ts)
                next_t = time.monotonic() + self.safety_dt
            self._sleep(min(0.2, self.safety_dt))

    def _drain_console(self):
        out = []
        while True:
            try:
                out.append(self._console_q.get_nowait())
            except queue.Empty:
                break
        return tuple(out)

    def run(self):
        threading.Thread(target=self._run_safety, daemon=True,
                         name="LinkSafety").start()
        backoff = self.min_dt
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                try:
                    self._read_all()
                except (AttributeError, TypeError, ValueError, KeyError) as exc:
                    # a reply of the wrong SHAPE (another service on the port, a
                    # Moonraker that answers differently): reported like a
                    # transport error. It used to kill this thread silently,
                    # leaving the page with no snapshot and no error.
                    raise MoonrakerError("unexpected reply from %s: %s"
                                         % (self.client.base, exc))
                with self._safety_lock:
                    slow = dict(self._slow)
                snap = LinkSnapshot(
                    t=time.time(), ok=True, error=None,
                    pos=self._pos, gpos=self._gpos, cpos=self._cpos,
                    vel=self._vel, speed=self._speed,
                    file_position=self._filepos, file_size=self._filesize,
                    console=self._drain_console(), **slow,
                )
                backoff = self.min_dt
                self._emit(snap)
            except MoonrakerError as exc:
                with self._safety_lock:
                    slow = dict(self._slow)
                self._emit(LinkSnapshot(t=time.time(), ok=False, error=str(exc),
                                        console=self._drain_console(), **slow))
                self._sleep(backoff)
                backoff = min(self.backoff_max, backoff * 2)
                continue
            dt = time.monotonic() - t0
            if dt < self.min_dt:                 # only sleeps when the query was fast
                self._sleep(self.min_dt - dt)

    def _emit(self, snap):
        try:
            self.out_q.put_nowait(snap)
        except queue.Full:
            pass

    def _sleep(self, dt):
        # interruptible sleep so stop() is responsive
        self._stop.wait(dt)


def _int_or_none(v):
    """Coerce a Moonraker numeric field to int, or None when absent/unparseable."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def work_offset(snap):
    """The work-origin offset H = `gcode_move.position - gcode_move.gcode_position`, as
    (hx, hy, hz), or None when either commanded position is missing.

    H registers FILE coordinates into the machine frame: a path exported at file
    coordinate p is physically printed at p + H. The live 3-D view draws the machine
    bodies at raw `motion_report.live_position`, so the reveal geometry -- which is in
    file coordinates -- must be shifted by +H to land under the nozzle.

    Both terms are COMMANDED quantities from the same status query, so their difference
    is atomic and lag-free, and it captures G92 and SET_GCODE_OFFSET alike. Two
    tempting alternatives are wrong, and both were tried on the rig:

      * `gcode_move.homing_origin` -- Klipper's `G92` modifies `base_position` only and
        leaves `homing_position` alone, and the MEWron's Zero buttons send G92, so
        homing_origin reads ~0 here and the overlay lands a whole work origin away.
      * `live_position - gcode_position` -- `live_position` is interpolated to NOW and
        trails the commanded point during motion, so the difference is H plus a
        velocity-dependent error that jitters every sample.
    """
    cp, gp = getattr(snap, "cpos", None), getattr(snap, "gpos", None)
    if cp is None or gp is None:
        return None
    return (cp[0] - gp[0], cp[1] - gp[1], cp[2] - gp[2])


def _s_value(upper_line, default):
    """The `S<number>` argument of a g-code line (M104 S220 -> 220.0), or default."""
    for tok in upper_line.split():
        if tok.startswith("S"):
            try:
                return float(tok[1:])
            except ValueError:
                return default
    return default


def _xyz(seq):
    """Coerce a Moonraker [x,y,z,(e)] list into an (x,y,z) float tuple, or None
    (also None for a scalar -- live_velocity is a scalar on real Klipper)."""
    if not isinstance(seq, (list, tuple)) or len(seq) < 3:
        return None
    try:
        return (float(seq[0]), float(seq[1]), float(seq[2]))
    except (TypeError, ValueError):
        return None


# ==========================================================================
# CSV recorder
# ==========================================================================

class TelemetryRecorder:
    """Append-only CSV of snapshots -- the lab-notebook trace of a print. Columns
    are fixed at construction from the heater names so every row aligns."""

    def __init__(self, path, heaters=()):
        self.path = path
        self.heaters = list(heaters)
        self._fh = open(path, "w", newline="")
        self._w = csv.writer(self._fh)
        # gx/gy/gz (gcode_position), cx/cy/cz (commanded position) and file_position
        # are here so a recorded trace can REPLAY a print offline: together they carry
        # the work offset H and the exact file cursor, which is everything the live
        # view's reveal consumes. Without them a trace cannot reproduce a rig session.
        cols = ["time", "ok", "x", "y", "z", "vx", "vy", "vz", "speed",
                "state", "progress", "speed_factor", "extrude_factor",
                "gx", "gy", "gz", "cx", "cy", "cz",
                "file_position", "file_size", "filename"]
        for h in self.heaters:
            cols += ["%s_actual" % h, "%s_target" % h]
        self._w.writerow(cols)
        self._fh.flush()

    def write(self, snap):
        px, py, pz = snap.pos if snap.pos else ("", "", "")
        vx, vy, vz = snap.vel if snap.vel else ("", "", "")
        gx, gy, gz = snap.gpos if snap.gpos else ("", "", "")
        cx, cy, cz = snap.cpos if snap.cpos else ("", "", "")
        row = [
            "%.3f" % snap.t, int(bool(snap.ok)), px, py, pz, vx, vy, vz,
            "" if snap.speed is None else "%.4f" % snap.speed,
            snap.state or "", "" if snap.progress is None else "%.4f" % snap.progress,
            "" if snap.speed_factor is None else snap.speed_factor,
            "" if snap.extrude_factor is None else snap.extrude_factor,
            gx, gy, gz, cx, cy, cz,
            "" if snap.file_position is None else snap.file_position,
            "" if snap.file_size is None else snap.file_size,
            snap.filename or "",
        ]
        for h in self.heaters:
            actual, target = snap.temps.get(h, ("", ""))
            row += ["" if actual is None else actual, "" if target is None else target]
        self._w.writerow(row)
        self._fh.flush()

    def close(self):
        try:
            self._fh.close()
        except OSError:
            pass


# ==========================================================================
# Fake rig (test harness + standalone --serve)
# ==========================================================================

class _FakeState:
    """A small STATEFUL printer simulator behind FakeMoonraker -- it OBEYS its own
    commands so every control in the GUI visibly works when pointed at it: idle it
    sits still; a print traces a path with advancing progress; jog/home move the
    toolhead; SET_HEATER_TEMPERATURE / M104 / M140 drive the temperatures toward
    their targets; pause/resume/cancel/emergency-stop change the state AND stop the
    motion. `scripts` / `uploads` keep recording every call for the test assertions."""

    _PRINT_SECONDS = 30.0                    # a demo print's duration
    _MOVE_SPEED = 5.0                        # printing-path speed, mm/s
    _SIDE = 40.0                             # printing-path square side, mm
    _TEMP_RATE = 4.0                         # heater approach, degC/s

    # The REAL homing signature, captured off a MEWron at 4 Hz:
    # `live_position` steps to coordinates far outside the 0..120 travel while
    # `live_velocity` reads ~0, because `G28` makes Klipper `set_position` once before
    # each endstop-seeking move and again at the trigger -- a FRAME REDEFINITION, not
    # motion. Replayed here (axis, value, reported speed) so the localhost fake
    # reproduces the live-view yank the old instant-G28-to-zero fake could never show;
    # that gap is exactly why three earlier fixes went offline-green / rig-broken.
    _HOME_EVENTS = (("z", -64.47, 0.0), ("z", 48.9, 10.0), ("z", 110.8, 4.1),
                    ("y", -40.0, 0.0), ("y", 22.3, 40.0), ("y", 80.0, 0.0),
                    ("x", -55.0, 0.0), ("x", 44.4, 40.0), ("x", 110.0, 0.0))
    _HOME_SECONDS = 2.5                      # compressed from the rig's ~21 s

    def __init__(self, *, require_key=None, flaky=0):
        self.require_key = require_key       # None, or the key that must match
        self.flaky_remaining = int(flaky)    # 503 this many times, then behave
        self.start = time.time()
        self.state = "ready"                 # print_stats.state
        self.filename = ""
        self.scripts = []                    # every gcode/script string received
        self.uploads = []                    # (filename, bytes, started)
        # stored g-code files (name -> {"data", "modified", "size"}); seeded with a
        # spread so the GUI's paging (page x of y) is exercised against the fake rig.
        self.gcode_files = {}
        self.estopped = False
        self.message = ""                    # print_stats.message (an error's reason)
        # Fault injection, for driving the GUI's failure paths: when set, every
        # upload is refused with this (HTTP status, message), as a printer
        # whose storage is full or whose Moonraker rejects the file would.
        self.reject_uploads = None
        # live simulation
        self.pos = [10.0, 10.0, 5.0]         # toolhead XYZ; jog/home/print move it
        self.gcode_offset = [0.0, 0.0, 0.0]  # G92 work-origin offset (gcode = pos-offset)
        self.vel = [0.0, 0.0, 0.0]
        self.homed = "xyz"
        self.printing = False
        self.paused = False
        # -- how THIS rig's PAUSE/RESUME are wired -------------------------
        # Two configurations, both real, kept because they FAIL DIFFERENTLY.
        # pause_resume=True (default): `[pause_resume]` is configured, so PAUSE sets
        # is_paused and saves the position, RESUME restores it (undoing any macro
        # z-hop), M24/M25 are remapped, and a second PAUSE is refused outright.
        # pause_resume=False: no `[pause_resume]`. PAUSE is a bare `[gcode_macro]`
        # calling M25 and lifting Z, so print_stats reads "paused" while is_paused
        # stays False, RESUME aborts with a console line and no motion, and every
        # further press lifts Z AGAIN -- the rig-observed failure the page's resume
        # escalation exists to survive.
        # `resume_macro_broken` is the third shape: `[pause_resume]` IS present but the
        # rig's RESUME wrapper swallows the call, so only the renamed base command works.
        self.pause_resume = True
        self.pause_z_hop = 2.0               # mm the rig's PAUSE macro lifts Z
        self.resume_macro_broken = False
        self.is_paused = False               # pause_resume.is_paused
        self.base_commands = ()              # e.g. ("PAUSE_BASE", "RESUME_BASE")
        # the lab's door-latch macro; False models every other printer, which
        # has none (the page then shows no Door unlock button)
        self.door_unlock = True
        self._pause_saved_z = None           # SAVE_GCODE_STATE's Z
        self.elapsed = 0.0                   # active print-time, s
        self.progress = 0.0
        # File-walking print (see `_load_print`). When the started file contains
        # parsable moves the fake EXECUTES it -- the toolhead follows the real
        # toolpath and `file_position` advances line by line, exactly as Klipper's
        # virtual_sdcard does -- so the localhost rig can drive the live view's
        # scaffold reveal end to end. A file with no moves falls back to the demo
        # square, keeping the older tests byte-identical.
        self._moves = []                     # [(byte_end, x, y, z, feed_mm_s, dist)]
        self._move_t = []                    # cumulative arrival time per move
        self.file_position = 0
        self.file_size = 0
        # Klipper reads AHEAD of the toolhead -- it keeps dispatching lines until
        # ~`buffer_time_high` (2 s) of motion is queued -- so `file_position` leads
        # the nozzle. Modelling that here is the point: a reveal driven by the file
        # cursor must be correct WITH the lead, and a lead-free fake would hide it.
        self.read_lead_s = 2.0
        self.speed_factor = 1.0
        self.extrude_factor = 1.0
        self.targets = {"extruder": 0.0, "heater_bed": 0.0}
        self.actuals = {"extruder": 24.0, "heater_bed": 23.0, "chamber": 24.0}
        self._tick = time.monotonic()
        # G28 replay: (start_monotonic, [(axis, value, speed), ...]) while homing is
        # running, else None. `realistic_homing=False` keeps the old instant
        # G28 -> 0,0,0 behaviour for the tests that only care about the command.
        self.realistic_homing = True
        self.home_seconds = self._HOME_SECONDS
        self._homing = None
        self.speed_override = None           # capture-reported speed while homing
        self.console = [                     # seed gcode_store
            {"message": "Klipper state: Ready", "time": self.start, "type": "response"},
        ]
        self._seed_files(23)
        self.lock = threading.Lock()

    def _seed_files(self, n):
        """Populate the file store with `n` plausible g-code files (staggered
        modified times) so the GUI's list + paging have real content to show."""
        for i in range(n):
            name = "sample_%02d.gcode" % i
            data = ("; sample scaffold %d\nG28\nG1 X%d Y%d F600\n"
                    % (i, i, i)).encode("utf-8")
            self.gcode_files[name] = {"data": data,
                                      "modified": self.start - (n - i) * 3600.0,
                                      "size": len(data)}

    # -- time-driven advance (request-driven, so lazy) --------------------

    def advance(self):
        """Move the simulation forward to now: advance a running print (position +
        progress), finish it at 100%, and drift the heaters toward their targets."""
        now = time.monotonic()
        dt = max(0.0, now - self._tick)
        self._tick = now
        if self._homing is not None:
            # step through the captured frame redefinitions, then settle homed
            t0, events = self._homing
            frac = (now - t0) / max(self.home_seconds, 1e-6)
            done = min(int(frac * len(events)), len(events))
            self.vel = [0.0, 0.0, 0.0]
            for axis, value, speed in events[:done]:
                self.pos["xyz".index(axis)] = value
                self.speed_override = speed
            if frac >= 1.0:
                self._homing = None
                self.pos = [0.0, 0.0, 0.0]
                self.speed_override = None
                self.homed = "xyz"
                self._log("Homing complete")
        elif self.printing and not self.paused:
            self.elapsed += dt
            if self._moves:
                self._advance_file_print()
            elif self.elapsed >= self._PRINT_SECONDS:
                self.printing = False
                self.state = "complete"
                self.progress = 1.0
                self.vel = [0.0, 0.0, 0.0]
                self._log("Print complete: %s" % self.filename)
            else:
                self.progress = self.elapsed / self._PRINT_SECONDS
                self.pos, self.vel = self._path_point(self.elapsed)
        else:
            self.vel = [0.0, 0.0, 0.0]
        # heaters approach their targets
        for h, tgt in self.targets.items():
            cur = self.actuals.get(h, 22.0)
            step = self._TEMP_RATE * dt
            self.actuals[h] = tgt if abs(tgt - cur) <= step else \
                cur + step * (1.0 if tgt > cur else -1.0)

    def _load_print(self, data):
        """Parse `data` (raw g-code bytes) into the move list the print walks.

        Each entry is (byte_end, x, y, z, feed_mm_s, dist) where `byte_end` is the
        file offset AFTER that line -- computed the way Klipper computes it, as a
        running sum of `len(line) + 1` over a split on b"\\n", so a CRLF file accounts
        correctly (the "\\r" stays inside the line). Only G0/G1 with a coordinate word
        produce a move; every other line still consumes its bytes, so the offsets stay
        exact. Returns False (and leaves the demo square in charge) when the file has
        no moves at all."""
        moves, cum = [], []
        x = y = z = 0.0
        feed = 30.0                                  # mm/s until the first F word
        off = 0
        t_acc = 0.0
        for raw in data.split(b"\n"):
            nxt = off + len(raw) + 1
            line = raw.decode("utf-8", "ignore").strip().upper()
            code = line.split(" ", 1)[0]
            if code in ("G0", "G1"):
                nx, ny, nz = x, y, z
                for tok in line.split()[1:]:
                    try:
                        val = float(tok[1:])
                    except (ValueError, IndexError):
                        continue
                    if tok[0] == "X":
                        nx = val
                    elif tok[0] == "Y":
                        ny = val
                    elif tok[0] == "Z":
                        nz = val
                    elif tok[0] == "F":
                        feed = max(val / 60.0, 1e-6)   # mm/min -> mm/s
                dist = ((nx - x) ** 2 + (ny - y) ** 2 + (nz - z) ** 2) ** 0.5
                if dist > 0.0:
                    t_acc += dist / feed
                    moves.append((nxt, nx, ny, nz, feed, dist))
                    cum.append(t_acc)
                x, y, z = nx, ny, nz
            off = nxt
        if not moves:
            return False
        self._moves, self._move_t = moves, cum
        self.file_size = len(data)
        self.file_position = 0
        return True

    def _advance_file_print(self):
        """Place the toolhead along the loaded file at `self.elapsed`, and set
        `file_position` to the READER's offset -- the line the machine will reach
        `read_lead_s` from now, capped at the end of the file. Finishing parks at the
        last point with file_position == file_size."""
        i = bisect.bisect_left(self._move_t, self.elapsed)
        if i >= len(self._moves):
            self.printing = False
            self.state = "complete"
            self.progress = 1.0
            self.file_position = self.file_size
            self.vel = [0.0, 0.0, 0.0]
            _, ex, ey, ez, _, _ = self._moves[-1]
            self.pos = [ex, ey, ez]
            self._log("Print complete: %s" % self.filename)
            return
        end, mx, my, mz, feed, dist = self._moves[i]
        t0 = self._move_t[i - 1] if i else 0.0
        px, py, pz = ((self._moves[i - 1][1], self._moves[i - 1][2],
                       self._moves[i - 1][3]) if i else (0.0, 0.0, 0.0))
        span = max(self._move_t[i] - t0, 1e-9)
        u = min(max((self.elapsed - t0) / span, 0.0), 1.0)
        self.pos = [px + (mx - px) * u, py + (my - py) * u, pz + (mz - pz) * u]
        self.vel = [(mx - px) / span, (my - py) / span, (mz - pz) / span]
        # the reader is ahead of the toolhead by the lookahead buffer
        j = bisect.bisect_left(self._move_t, self.elapsed + self.read_lead_s)
        j = min(j, len(self._moves) - 1)
        self.file_position = min(self._moves[j][0], self.file_size)
        self.progress = (self.file_position / self.file_size
                         if self.file_size else 0.0)

    def _path_point(self, t):
        """A 40 mm square traced at _MOVE_SPEED, looping -- the printing toolpath."""
        perim = 4 * self._SIDE
        d = (self._MOVE_SPEED * t) % perim
        s, sp = self._SIDE, self._MOVE_SPEED
        if d < s:
            x, y, vx, vy = d, 0.0, sp, 0.0
        elif d < 2 * s:
            x, y, vx, vy = s, d - s, 0.0, sp
        elif d < 3 * s:
            x, y, vx, vy = s - (d - 2 * s), s, -sp, 0.0
        else:
            x, y, vx, vy = 0.0, s - (d - 3 * s), 0.0, -sp
        return [x, y, 2.0], [vx, vy, 0.0]

    def homing_active(self):
        with self.lock:
            return self._homing is not None

    def wait_homing(self, poll=0.02):
        """Block until a running G28 replay finishes (Klipper's blocking-script
        semantics). Returns immediately when nothing is homing."""
        while self.homing_active():
            time.sleep(poll)
            with self.lock:
                self.advance()

    def _log(self, msg):
        self.console.append({"message": msg, "time": time.time(), "type": "response"})

    # -- command handlers -------------------------------------------------

    def start_print(self, filename):
        self.filename = filename or self.filename or "print.gcode"
        self.printing = True
        self.paused = False
        self.is_paused = False
        self._pause_saved_z = None
        self.elapsed = 0.0
        self.progress = 0.0
        self.state = "printing"
        self.message = ""
        # walk the STORED file when it has real moves (so the live view's reveal can
        # be driven end to end); otherwise fall back to the demo square
        self._moves, self._move_t = [], []
        self.file_position = 0
        self.file_size = 0
        entry = self.gcode_files.get(self.filename)
        if entry is not None:
            self._load_print(entry["data"])
        self._log("Starting print: %s" % self.filename)

    def _pause_base(self):
        """Klipper's own `PAUSE`: save the state, stop the job, set is_paused. Moves
        nothing, and refuses when the job is already paused."""
        if self.paused:
            self._log("Print already paused")
            return
        if not self.printing:
            return
        if self._pause_saved_z is None:
            self._pause_saved_z = self.pos[2]      # SAVE_GCODE_STATE
        self.paused = True
        self.state = "paused"
        self.is_paused = True
        self.vel = [0.0, 0.0, 0.0]
        self._log("Print paused")

    def pause(self):
        """The `PAUSE` command: the rig's macro lifts Z, then the job pauses.

        With `[pause_resume]` a second PAUSE is refused before anything moves (so the
        hop happens once); without it, the bare macro hops EVERY time it is called --
        the +2 mm-per-press the rig showed."""
        if not self.printing:
            return
        if self.pause_resume and self.paused:
            self._log("Print already paused")
            return
        if self._pause_saved_z is None:
            self._pause_saved_z = self.pos[2]      # SAVE_GCODE_STATE
        self.pos[2] += self.pause_z_hop            # the macro's z-hop
        if self.pause_resume:
            self._pause_base()
        else:                                      # a bare `M25` macro: no is_paused
            self.paused = True
            self.state = "paused"
            self.vel = [0.0, 0.0, 0.0]
            self._log("Print paused")

    def _resume_base(self):
        """Klipper's OWN `RESUME` (from `[pause_resume]`): refuses unless `is_paused`,
        otherwise `RESTORE_GCODE_STATE MOVE=1` puts the head back (undoing the hop)
        and the job carries on. The refusal is a console line, NOT an error -- which
        is exactly why a failed resume looks like nothing happening at all."""
        if not (self.printing and self.paused):
            return
        if not self.is_paused:
            self._log("Print is not paused, resume aborted")
            return
        if self._pause_saved_z is not None:
            self.pos[2] = self._pause_saved_z
        self._pause_saved_z = None
        self.paused = False
        self.is_paused = False
        self.state = "printing"
        self._log("Print resumed")

    def resume(self):
        """`RESUME` as the endpoint reaches it -- the rig's wrapper macro when it has
        one (`resume_macro_broken` = the wrapper that swallows the call)."""
        if self.resume_macro_broken:
            return
        self._resume_base()

    def sd_resume(self):
        """`M24`. On a rig with `[pause_resume]` Klipper remaps M24 onto RESUME; on one
        without, it goes straight to virtual_sdcard and restarts the stream -- which is
        the ONLY thing that resumes a job paused by a bare `M25` macro."""
        if self.pause_resume:
            self.resume()
            return
        if self.printing and self.paused:
            self.paused = False
            self.state = "printing"
            self._pause_saved_z = None
            self._log("Print resumed")

    def cancel(self):
        self.printing = False
        self.paused = False
        self.is_paused = False
        self._pause_saved_z = None
        self.state = "cancelled"
        self.vel = [0.0, 0.0, 0.0]
        self._log("Print cancelled")

    def estop(self):
        self.estopped = True
        self.printing = False
        self.paused = False
        self.is_paused = False
        self.state = "error"
        self.message = "Emergency stop (M112)"
        self.vel = [0.0, 0.0, 0.0]
        self._log("!! Emergency stop (M112) -- printer shut down")

    def fault(self, message="Move out of range: 130.000 10.000 0.200 [0.000]"):
        """Stop the running print on an error, as Klipper does when a move
        leaves the travel or a heater faults: print_stats goes to "error" with
        Klipper's reason in `message`, and the reason lands in the console."""
        self.printing = False
        self.paused = False
        self.is_paused = False
        self.state = "error"
        self.message = message
        self.vel = [0.0, 0.0, 0.0]
        self._log("!! %s" % message)

    def gcode_help(self):
        """The command table `/printer/gcode/help` publishes -- the stock verbs plus
        whatever base names the rig's wrapper macros renamed the originals to."""
        help_ = {"G28": "Home axes", "M24": "Start/resume SD print",
                 "M25": "Pause SD print", "PAUSE": "Pause the print",
                 "RESUME": "Resume the print"}
        if self.door_unlock:
            help_["DOOR_UNLOCK"] = "Unlock the door"
        for name in self.base_commands:
            help_[name] = "Renamed builtin"
        return help_

    def apply_gcode(self, script):
        """Interpret the g-code the GUI sends so its controls visibly act: jog
        (G91/G1), home (G28), heater sets (SET_HEATER_TEMPERATURE / M104 / M140),
        overrides (M220 / M221) and the print-control verbs the page escalates
        through (M24 / M25 / PAUSE / RESUME / a renamed base). Anything else is just
        recorded."""
        relative = False
        for raw in script.split("\n"):
            line = raw.strip()
            u = line.upper()
            if u == "M25" or u == "PAUSE":
                self.pause()
            elif u == "M24" or u == "RESUME":
                self.sd_resume() if u == "M24" else self.resume()
            elif u in self.base_commands:
                # a renamed builtin: <X>_BASE / BASE_<X> IS Klipper's own command
                if "RESUME" in u:
                    self._resume_base()
                elif "PAUSE" in u:
                    self.pause()
            elif u == "G91":
                relative = True
            elif u == "G90":
                relative = False
            elif u.startswith("G28"):
                axes = u[3:].split()
                if self.realistic_homing:
                    # replay the rig's frame redefinitions over `home_seconds` (see
                    # _HOME_EVENTS); `advance()` walks it and `homing_active()` tells
                    # the request handler to hold the response open, as Klipper does.
                    want = "".join(axes).lower() or "xyz"
                    self._homing = (time.monotonic(),
                                    [e for e in self._HOME_EVENTS if e[0] in want])
                    self.homed = ""
                    self._log("Homing: G28")
                else:
                    for i, ax in enumerate("XYZ"):
                        if not axes or ax in " ".join(axes):
                            self.pos[i] = 0.0
            elif u.startswith("G92"):
                # set the work origin: after G92 X0, gcode_position X reads 0 while
                # the raw pos is unchanged (offset = pos - requested value).
                toks = u.split()[1:]
                axes = toks if toks else ["X0", "Y0", "Z0"]
                for tok in axes:
                    if tok and tok[0] in "XYZ":
                        try:
                            v = float(tok[1:])
                        except ValueError:
                            continue
                        i = "XYZ".index(tok[0])
                        self.gcode_offset[i] = self.pos[i] - v
            elif u.startswith("G1") or u.startswith("G0"):
                for tok in u.split()[1:]:
                    if tok and tok[0] in "XYZ":
                        try:
                            v = float(tok[1:])
                        except ValueError:
                            continue
                        i = "XYZ".index(tok[0])
                        # absolute moves are in the GCODE (work) frame: G1 X<v>
                        # targets gcode_position=v, so the raw axis lands at
                        # v + offset (Klipper semantics). Relative adds the delta.
                        self.pos[i] = (self.pos[i] + v if relative
                                       else v + self.gcode_offset[i])
            elif u.startswith("SET_HEATER_TEMPERATURE"):
                d = {}
                for tok in line.split():
                    if "=" in tok:
                        k, v = tok.split("=", 1)
                        d[k.upper()] = v
                h, t = d.get("HEATER"), d.get("TARGET")
                if h and t is not None:
                    try:
                        self.targets[h] = float(t)
                    except ValueError:
                        pass
            elif u.startswith("M104") or u.startswith("M109"):
                self.targets["extruder"] = _s_value(u, self.targets["extruder"])
            elif u.startswith("M140") or u.startswith("M190"):
                self.targets["heater_bed"] = _s_value(u, self.targets["heater_bed"])
            elif u.startswith("M220"):
                self.speed_factor = _s_value(u, 100.0) / 100.0
            elif u.startswith("M221"):
                self.extrude_factor = _s_value(u, 100.0) / 100.0
            elif u.startswith("M117"):
                self._log("// " + line[4:].strip())


class _FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # keep the test output quiet

    # -- helpers -----------------------------------------------------------

    @property
    def st(self):
        return self.server.state

    def _auth_ok(self):
        need = self.st.require_key
        if not need:
            return True
        return self.headers.get("X-Api-Key") == need

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _result(self, result):
        self._send(200, {"result": result})

    def _error(self, code, message):
        self._send(code, {"error": {"code": code, "message": message}})

    def _gate(self):
        """Return True if the request should proceed; else write a response and
        return False (flaky 503, then auth 401)."""
        if self.st.flaky_remaining > 0:
            self.st.flaky_remaining -= 1
            self._error(503, "temporarily unavailable")
            return False
        if not self._auth_ok():
            self._error(401, "unauthorized")
            return False
        return True

    def _objects_status(self, want):
        """The `status` block for a query. Raises KeyError(name) for an object this
        printer does not have -- Klipper fails the WHOLE query that way ("Unknown
        object"), and a fake that quietly omitted it would hide exactly the trap
        `MoonrakerClient.has_object` exists to avoid."""
        st = self.st
        with st.lock:
            st.advance()
            pos = [st.pos[0], st.pos[1], st.pos[2], 0.0]
            gpos = [st.pos[i] - st.gcode_offset[i] for i in range(3)] + [0.0]
            # real Klipper reports live_velocity as a scalar magnitude, not a vector
            speed = (st.vel[0] ** 2 + st.vel[1] ** 2 + st.vel[2] ** 2) ** 0.5
            if st.speed_override is not None:
                # during the G28 replay the reported speed comes from the capture --
                # the whole point is that it does NOT agree with the position steps
                speed = st.speed_override
            ext = st.actuals.get("extruder", 24.0)
            bed = st.actuals.get("heater_bed", 23.0)
            cham = st.actuals.get("chamber", 24.0)

            def _pwm(name, cur):
                # a plausible closed-loop PWM: high while far below target, settling
                # to a small hold as it converges; 0 when off. Just enough for the
                # live graph to show movement against the temperature trace.
                tgt = st.targets.get(name, 0.0)
                if tgt <= 0.0:
                    return 0.0
                gap = tgt - cur
                return max(0.0, min(1.0, 0.05 + gap / 15.0)) if gap > 0 else 0.05
            avail = {
                "motion_report": {"live_position": pos, "live_velocity": speed,
                                  "live_extruder_velocity": 0.0},
                "gcode_move": {"speed_factor": st.speed_factor,
                              "extrude_factor": st.extrude_factor,
                              "homing_origin": [st.gcode_offset[0],
                                                st.gcode_offset[1],
                                                st.gcode_offset[2], 0.0],
                              "gcode_position": gpos,
                              # `position` is the COMMANDED machine-frame point; the
                              # fake has no lookahead, so it equals the toolhead. The
                              # pair (position, gcode_position) is what `work_offset`
                              # differences into H -- here exactly the G92 offset.
                              "position": [pos[0], pos[1], pos[2], 0.0]},
                "print_stats": {"state": st.state, "filename": st.filename,
                               "message": st.message,
                               "print_duration": st.elapsed},
                "virtual_sdcard": {"progress": st.progress,
                                  "is_active": st.printing,
                                  "file_position": st.file_position,
                                  "file_size": st.file_size,
                                  "file_path": st.filename},
                "toolhead": {"homed_axes": st.homed},
                "pause_resume": {"is_paused": st.is_paused},
                "heaters": {"available_heaters": ["extruder", "heater_bed"],
                           "available_sensors": ["extruder", "heater_bed", "chamber"]},
                "extruder": {"temperature": ext,
                            "target": st.targets.get("extruder", 0.0),
                            "power": _pwm("extruder", ext)},
                "heater_bed": {"temperature": bed,
                              "target": st.targets.get("heater_bed", 0.0),
                              "power": _pwm("heater_bed", bed)},
                "chamber": {"temperature": cham},
                "configfile": {"settings": {
                    "extruder": {"max_temp": 300.0},
                    "heater_bed": {"max_temp": 120.0},
                    # per-axis travel, the shape `axis_limits()` reads (a Voron 0.2
                    # -- the rig this page was brought up against)
                    "stepper_x": {"position_min": 0.0, "position_max": 120.0},
                    "stepper_y": {"position_min": 0.0, "position_max": 120.0},
                    "stepper_z": {"position_min": 0.0, "position_max": 120.0},
                }},
            }
            if not st.pause_resume:      # no [pause_resume] section on this rig
                avail.pop("pause_resume")
        status = {}
        for name, fields in want.items():
            if name not in avail:
                raise KeyError(name)
            row = avail[name]
            status[name] = row if not fields else {k: row[k] for k in fields if k in row}
        return status

    # -- verbs -------------------------------------------------------------

    def do_GET(self):
        if not self._gate():
            return
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path == "/printer/info":
            self._result({"state": "ready", "hostname": "fake-mewron",
                          "state_message": "Printer is ready"})
        elif path == "/server/info":
            self._result({"klippy_state": "ready", "klippy_connected": True})
        elif path == "/printer/objects/list":
            objects = ["motion_report", "gcode_move", "print_stats", "virtual_sdcard",
                       "toolhead", "heaters", "extruder", "heater_bed", "chamber",
                       "configfile"]
            if self.st.pause_resume:
                objects.append("pause_resume")
            self._result({"objects": objects})
        elif path == "/printer/gcode/help":
            with self.st.lock:
                self._result(self.st.gcode_help())
        elif path == "/printer/objects/query":
            want = {}
            # query string preserves order via the raw query, not parse_qs
            for token in parsed.query.split("&"):
                if not token:
                    continue
                if "=" in token:
                    name, fields = token.split("=", 1)
                    want[urllib.parse.unquote(name)] = fields.split(",")
                else:
                    want[urllib.parse.unquote(token)] = None
            try:
                status = self._objects_status(want)
            except KeyError as exc:
                self._error(400, "Unknown object %s" % (exc.args[0],))
                return
            self._result({"eventtime": time.time() - self.st.start,
                          "status": status})
        elif path == "/server/gcode_store":
            self._result({"gcode_store": list(self.st.console)})
        elif path == "/server/files/list":
            with self.st.lock:
                items = [{"path": n, "modified": m["modified"], "size": m["size"],
                          "permissions": "rw"}
                         for n, m in self.st.gcode_files.items()]
            self._result(items)
        elif path.startswith("/server/files/gcodes/"):
            name = urllib.parse.unquote(path[len("/server/files/gcodes/"):])
            with self.st.lock:
                meta = self.st.gcode_files.get(name)
            if meta is None:
                self._error(404, "file not found: %s" % name)
            else:
                body = meta["data"]
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        elif path == "/machine/proc_stats":
            self._result({"throttled_state": {"bits": 0, "flags": []},
                          "system_cpu_usage": {"cpu": 8.0}})
        else:
            self._error(404, "not found: %s" % path)

    def do_DELETE(self):
        if not self._gate():
            return
        path = urllib.parse.urlparse(self.path).path
        prefix = "/server/files/gcodes/"
        if path.startswith(prefix):
            name = urllib.parse.unquote(path[len(prefix):])
            with self.st.lock:
                existed = self.st.gcode_files.pop(name, None) is not None
            if existed:
                self._result({"item": {"path": name, "root": "gcodes"},
                              "action": "delete_file"})
            else:
                self._error(404, "file not found: %s" % name)
        else:
            self._error(404, "not found: %s" % path)

    def do_POST(self):
        if not self._gate():
            return
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        if path == "/printer/gcode/script":
            script = qs.get("script", [""])[0]
            with self.st.lock:
                self.st.scripts.append(script)
                self.st.apply_gcode(script)
            # Klipper runs the script under the g-code mutex and only answers when it
            # has FINISHED, so a G28 holds the response open for the whole homing.
            # The old fake answered instantly, which is why the blocking-POST side of
            # this (and the 4 s client timeout it blows through) was never seen on the
            # bench. ThreadingHTTPServer, so this blocks one connection, not the feed.
            self.st.wait_homing()
            self._result("ok")
        elif path == "/printer/emergency_stop":
            with self.st.lock:
                self.st.estop()
            self._result("ok")
        elif path == "/printer/print/start":
            with self.st.lock:
                self.st.start_print(qs.get("filename", [""])[0])
            self._result("ok")
        elif path == "/printer/print/pause":
            with self.st.lock:
                self.st.pause()
            self._result("ok")
        elif path == "/printer/print/resume":
            with self.st.lock:
                self.st.resume()
            self._result("ok")
        elif path == "/printer/print/cancel":
            with self.st.lock:
                self.st.cancel()
            self._result("ok")
        elif path == "/server/files/upload":
            if self.st.reject_uploads:
                return self._error(*self.st.reject_uploads)
            parts =_decode_multipart(self.headers.get("Content-Type"), body)
            filepart = parts.get("file", {})
            fname = filepart.get("filename", "upload.gcode")
            started = parts.get("print", {}).get("value", b"").decode(
                "utf-8", "replace").lower() == "true"
            blob = filepart.get("value", b"")
            with self.st.lock:
                self.st.uploads.append((fname, blob, started))
                self.st.gcode_files[fname] = {"data": blob, "modified": time.time(),
                                              "size": len(blob)}
                if started:
                    self.st.start_print(fname)
            self._result({"item": {"path": fname, "root": "gcodes"},
                          "print_started": started})
        else:
            self._error(404, "not found: %s" % path)


class FakeMoonraker:
    """A stdlib HTTP server that mimics the Moonraker endpoints this module uses.
    Use as a context manager in tests; `.url_host`/`.port` point a
    MoonrakerClient at it. `state` exposes the recorded scripts/uploads for
    assertions."""

    def __init__(self, port=0, *, require_key=None, flaky=0):
        self.state = _FakeState(require_key=require_key, flaky=flaky)
        self._server = ThreadingHTTPServer(("127.0.0.1", port), _FakeHandler)
        self._server.state = self.state
        self.port = self._server.server_address[1]
        self.host = "127.0.0.1"
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True, name="FakeMoonraker")
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def client(self, **kw):
        return MoonrakerClient(self.host, self.port, **kw)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def serve_forever(port=7125):
    """Run the fake rig in the foreground so the real GUI can be pointed at
    localhost with no hardware. Ctrl-C to stop."""
    fake = FakeMoonraker(port=port).start()
    print("FakeMoonraker serving on http://127.0.0.1:%d  (Ctrl-C to stop)" % fake.port)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        fake.stop()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Moonraker link -- fake rig server")
    ap.add_argument("--serve", action="store_true", help="run the fake rig")
    ap.add_argument("--port", type=int, default=7125)
    args = ap.parse_args()
    if args.serve:
        serve_forever(args.port)
    else:
        ap.print_help()
