#!/usr/bin/env python3
"""viewer_embed.py -- MODERN-UI ONLY: the subprocess side of the embedded viewers.

The GUI embeds the STL viewer and the locked/live simulations WITHOUT giving up
their out-of-process crash protection: the viewer stays its own process, renders
OFFSCREEN, and streams frames into a shared-memory double buffer the GUI created;
the GUI blits them onto a plain Tk label and forwards mouse/key input back down
stdin. This module is everything the child process needs:

  EmbedHost    -- owns the protocol: the shared-memory writer, the stdout message
                  channel, the stdin command pump, and the main loop that replaces
                  `interactor.Start()`. Input commands are REPLAYED into the
                  interactor, so all existing interaction code (install_cad_camera,
                  the player's key map, the toggle callbacks) runs unchanged. On
                  Windows the child uses the PLATFORM (Win32) interactor from
                  launch even though the window starts offscreen: synthesized
                  events drive it identically, and it is what lets NATIVE
                  FULLSCREEN pump real OS input into the same observers.
  native_fs    -- the GUI's fullscreen button flips the child's render window
                  onscreen-borderless at the app's monitor bounds (GPU-direct,
                  vsynced -- no blit), builds a `gui_viewer_shell.ViewerChrome`
                  sidebar from the SAME captured controls the Tk rail renders, and
                  pumps `ProcessEvents()`. The child replies on every ACTUAL
                  transition, so the GUI's button state is always truthful. While
                  native, frame publishing is suspended but labels/HUD/state keep
                  flowing, so the Tk rail stays live.
  dock         -- (Windows) after the first streamed frame the GUI sends its
                  viewport HWND; the same render window becomes a WS_CHILD of it
                  and draws onscreen in the panel, input pumped as in native_fs.
                  Streaming stays the fallback (hwnd 0, a failed dock, non-
                  Windows, or MEWRACLE_EMBED_BLIT=1 in the GUI). See _dock.
  EmbedChrome  -- a ViewerChrome-SHAPED exporter: the same interface, but instead
                  of drawing VTK sidebar actors it EXPORTS the rows over the
                  protocol for the GUI to render as ttk controls. The viewers pick
                  a chrome with one branch and every call site stays verbatim.

Protocol, JSON lines. Child -> parent goes on the REAL stdout, dup'd before
`sys.stdout` is re-pointed at stderr so stray prints cannot corrupt the channel:
hello / labels / hud / state / frame / dock / bye. Parent -> child on stdin: mouse
/ key / btn / scrub / resize / theme / visible / dock / quit. Frames are RGB8, top-down,
written alternately into two shared-memory slots (`--embed name:WxH` names the
segment and the per-slot max size), so the reader never sees a torn image.

No Tk in here; no VTK imports at module scope beyond what the viewers already
loaded."""

import json
import os
import queue
import sys
import threading
import time

# publish caps. Frames publish on the 16 ms render tick; the guard exists only
# to bound interaction BURSTS (drag events), so it must sit comfortably BELOW
# the tick period -- at 1/61 it sat 0.4 ms above it and rejected every other
# tick, halving the animation to ~30 fps (the user-reported stutter). State ~10 Hz.
_FRAME_MIN_DT = 0.012
_STATE_MIN_DT = 0.10

# Native fullscreen extends 1px BELOW the monitor on purpose. A borderless
# window that EXACTLY covers the monitor and holds the foreground is promoted
# by DWM into independent-flip (direct-flip) fullscreen presentation; entering
# that mode costs one black frame -- the "black flash" (at launch for the
# continuously-presenting sims, on first interaction for the tick-less STL
# viewer, which DWM only promotes once it presents again). Overhanging the
# monitor by a pixel keeps the window in DWM's normal COMPOSITED path, so
# there is no mode transition and no flash. The 1px spills off-screen (nothing
# is below the monitor bottom); the in-window transport sits well above it.
# Direct-flip's "nothing composites on top" is not needed: the fullscreen
# chrome/transport is drawn inside the window itself.
_FS_OVERHANG = 1


def _coalesce_moves(cmds):
    """Collapse runs of CONSECUTIVE mouse-move commands that share the same
    button + modifiers down to their NEWEST. Tk fires B1-Motion at 125-200 Hz
    and the GUI forwards every event; on a heavy scene the child would replay
    each backlogged move, running a FULL render apiece -- 30 queued moves =
    30 renders of rubber-banding behind the cursor (the native windowed viewer
    never showed this because Windows coalesces WM_MOUSEMOVE for it). Applying
    only the newest of a run gives the same final camera with one render.

    A press / release / wheel / btn / key / any non-move command, or a move
    with a DIFFERENT button or modifier set, breaks the run (so a click is
    never swallowed and a modifier change is honoured). Order-preserving;
    everything except superseded consecutive moves passes through untouched."""
    out = []
    n = len(cmds)
    for i, m in enumerate(cmds):
        # The MIDDLE button (2) is VTK's trackball PAN, which is INCREMENTAL --
        # each move translates the camera by (this event - the last event). Dropping
        # intermediate moves makes the per-applied-event delta large, so the pan
        # lurches / teleports instead of tracking the cursor smoothly. Never coalesce
        # it. Orbit/turn (left/right, install_cad_camera) are press-relative, so
        # collapsing their redundant moves gives the SAME final camera in one render.
        if (m.get("t") == "mouse" and m.get("ev") == "move"
                and m.get("btn") != 2):
            nxt = cmds[i + 1] if i + 1 < n else None
            if (nxt is not None and nxt.get("t") == "mouse"
                    and nxt.get("ev") == "move"
                    and nxt.get("btn") == m.get("btn")
                    and bool(nxt.get("shift")) == bool(m.get("shift"))
                    and bool(nxt.get("ctrl")) == bool(m.get("ctrl"))):
                continue        # a newer same-kind move follows -> drop this one
        out.append(m)
    return out


def parse_embed_spec(spec):
    """'name:WxH' -> (name, max_w, max_h). Raises ValueError on garbage."""
    name, _, dims = spec.partition(":")
    w, _, h = dims.partition("x")
    w, h = int(w), int(h)
    if not name or w < 32 or h < 32:
        raise ValueError(f"bad embed spec: {spec!r}")
    return name, w, h


def _safe_call(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 -- a state read must never kill the loop
        return default


class _LabelCarrier:
    """A stand-in for the vtkTextActor in ViewerChrome's buttons list, so the
    player's _refresh_buttons (which calls actor.SetInput) works unchanged."""

    def __init__(self, text=""):
        self._text = text

    def SetInput(self, text):          # noqa: N802 -- vtk casing on purpose
        self._text = text

    def GetInput(self):                # noqa: N802
        return self._text


class EmbedChrome:
    """ViewerChrome's interface, exporting instead of drawing (module doc)."""

    def __init__(self, host, title="MEWracle", subtitle=""):
        self.host = host
        self.title = title
        self.subtitle = subtitle
        # `buttons` keeps ViewerChrome's VERBATIM shape -- (carrier, label_fn,
        # callback) 3-tuples -- because gcode_player._refresh_buttons and the
        # fullscreen builder iterate it expecting exactly that. Button ids ride
        # PARALLEL lists (`_button_ids`, `_buttons_by_id`); NEVER zip(controls,
        # buttons) -- once sliders/numbers intersperse, controls is longer and
        # every button dispatch/label lookup misaligns (or misses).
        self.buttons = []
        self._button_ids = []
        self._buttons_by_id = {}
        self.controls = []         # export spec: {id, type, section, label, ...}
        self.footers = []          # export spec: {section, text, hint}
        # value controls (slider/number): id -> {kind, label, value_fn, on_set,
        # fmt, ...}. Parallel to `buttons`; apply_value() dispatches on_set and
        # current_values() snapshots value_fn so the host can push "values" back
        # to the rail after a programmatic change (Reset / scale-from-2D).
        self._value_ctrls = {}
        self._section = None
        self._hud_actor = None
        self._hud_lines = 12
        # toggle buttons: id -> state_fn (a Show/Hide button that ALSO says what it
        # names and whether that thing is on, so the rail can draw a switch)
        self._toggle_fns = {}
        # structured readout: () -> (rows, text). `rows` is a list of
        # {"g": group, "k": label, "v": value, "knob"?: name}; `text`
        # is the HUD text the SAME refresh wrote, so a HUD that no longer matches
        # it is a transient status ("Computing deviation…") -- see readout().
        self._readout_fn = None
        # the g-code listing: () -> {"cur": line, "lines": [[n, text], ...]}
        # or None while it is off (see set_listing)
        self._listing_fn = None

    # -- the ViewerChrome interface (call sites stay verbatim) ------------- #
    def section(self, text):
        self._section = text

    def button(self, label_fn, callback, swatch=None, toggle=None, role=None):
        """`toggle=(name, state_fn)` marks an on/off button: the rail draws it as
        a switch called `name`, lit while `state_fn()` is true. `role` marks a
        button the rail places somewhere other than its section -- "play",
        "slower", "faster", "restart" on the transport, "knob:<name>:+|-" and
        "knob:reset" beside the readout. Both are optional and ADDITIVE: the label
        still streams, so the fullscreen sidebar (which ignores them) is
        unchanged."""
        carrier = _LabelCarrier(label_fn())
        cid = f"b{len(self.controls)}"
        self.buttons.append((carrier, label_fn, callback))   # ViewerChrome shape
        self._button_ids.append(cid)
        self._buttons_by_id[cid] = (carrier, label_fn, callback)
        spec = {
            "id": cid,
            "type": "button",
            "section": self._section or "",
            "label": label_fn().strip(),
            "swatch": (list(swatch) if swatch is not None else None),
        }
        if toggle is not None:
            name, state_fn = toggle
            self._toggle_fns[cid] = state_fn
            spec["toggle"] = str(name)
            spec["on"] = bool(_safe_call(state_fn, False))
        if role:
            spec["role"] = str(role)
        self.controls.append(spec)
        return carrier

    def slider(self, label, value_fn, lo, hi, on_set, step=None, fmt="%.3g"):
        """A continuous control: the rail draws a ttk.Scale, native fullscreen a
        themed track+knob. `value_fn()` -> the current value (float); `on_set(v)`
        applies a new one. `label` is static text (the value is shown beside it)."""
        cid = f"c{len(self.controls)}"
        self.controls.append({
            "id": cid, "type": "slider", "section": self._section or "",
            "label": label, "min": float(lo), "max": float(hi),
            "step": (float(step) if step is not None else None),
            "value": float(value_fn()), "fmt": fmt,
        })
        self._value_ctrls[cid] = {"kind": "slider", "label": label,
                                  "value_fn": value_fn, "on_set": on_set,
                                  "lo": float(lo), "hi": float(hi), "fmt": fmt}
        return cid

    def number(self, label, value_fn, on_set, lo=None, hi=None, fmt="%.4g"):
        """An exact-value control: the rail draws a ttk.Entry (type a value),
        native fullscreen a slider over [lo,hi] (typing isn't possible in a VTK
        overlay). `value_fn()` -> current value; `on_set(v)` applies text/float."""
        cid = f"c{len(self.controls)}"
        self.controls.append({
            "id": cid, "type": "number", "section": self._section or "",
            "label": label, "value": float(value_fn()), "fmt": fmt,
            "min": (float(lo) if lo is not None else None),
            "max": (float(hi) if hi is not None else None),
        })
        self._value_ctrls[cid] = {"kind": "number", "label": label,
                                  "value_fn": value_fn, "on_set": on_set,
                                  "lo": lo, "hi": hi, "fmt": fmt}
        return cid

    def apply_value(self, cid, value):
        """Dispatch a rail edit ({'t':'ctl'}) to the value control's on_set."""
        ctl = self._value_ctrls.get(cid)
        if ctl is None:
            return False
        try:
            ctl["on_set"](value)
        except Exception:  # noqa: BLE001 -- a bad edit must never kill the loop
            pass
        return True

    def current_values(self):
        """{control id: current numeric value} snapshot for the value controls,
        so the host can diff + push "values" when the child changes them."""
        out = {}
        for cid, ctl in self._value_ctrls.items():
            try:
                out[cid] = float(ctl["value_fn"]())
            except Exception:  # noqa: BLE001
                continue
        return out

    def remove_controls(self, ids):
        """Drop the given control ids from the exported spec -- used to REMOVE a
        section added mid-session (the micrograph sliders on "Exit micrograph").
        Keeps the parallel button lists in lockstep, and mutates
        `buttons` IN PLACE so the viewer's `self.buttons` reference stays valid."""
        ids = set(ids)
        self.controls = [c for c in self.controls if c.get("id") not in ids]
        for cid in ids:
            self._value_ctrls.pop(cid, None)
            self._buttons_by_id.pop(cid, None)
            self._toggle_fns.pop(cid, None)
        keep = [(bid, b) for bid, b in zip(self._button_ids, self.buttons)
                if bid not in ids]
        self._button_ids = [bid for bid, _b in keep]
        self.buttons[:] = [b for _bid, b in keep]

    def set_readout(self, rows_fn):
        """Register the structured readout provider (see __init__)."""
        self._readout_fn = rows_fn

    def readout(self):
        """(rows, status) for the rail, or None when no provider is set. `status`
        is the HUD text when a transient message has replaced the readout, else
        ""."""
        if self._readout_fn is None:
            return None
        try:
            rows, text = self._readout_fn()
        except Exception:  # noqa: BLE001 -- a readout must never kill the loop
            return None
        hud = self.hud_text()
        status = hud.strip() if (hud is not None and text is not None
                                 and hud != text) else ""
        return list(rows or []), status

    def set_listing(self, listing_fn):
        """Register the g-code listing provider: () -> {"cur": the current
        1-based line, "lines": [[n, text], ...]} (a window of the file around
        it), or None while the listing is off. Both renderers of the readout
        draw it in place of the Line and Move rows: the ttk rail from the
        "listing" message, the native-fullscreen rail from `listing()`."""
        self._listing_fn = listing_fn

    def listing(self):
        if self._listing_fn is None:
            return None
        try:
            return self._listing_fn()
        except Exception:  # noqa: BLE001 -- a readout must never kill the loop
            return None

    def current_states(self):
        """{toggle id: on} snapshot for the switch-drawn buttons."""
        return {cid: bool(_safe_call(fn, False))
                for cid, fn in self._toggle_fns.items()}

    def set_hud(self, actor, max_lines=12):
        self._hud_actor = actor
        self._hud_lines = max_lines     # the native-fullscreen chrome reuses it

    def footer_section(self, text):
        self.footers.append({"section": text, "text": "", "hint": True})

    def footer_label(self, text, lines=None, hint=True):
        if self.footers and not self.footers[-1]["text"]:
            self.footers[-1]["text"] = text
            self.footers[-1]["hint"] = hint
        else:
            self.footers.append({"section": "", "text": text, "hint": hint})

    def layout(self, *_):
        pass

    def install_click_handler(self, _interactor):
        return None

    def refresh_labels(self):
        for carrier, label_fn, _cb in self.buttons:
            try:
                carrier.SetInput(label_fn())
            except Exception:  # noqa: BLE001 -- a label must never kill the loop
                pass

    # -- host-side helpers -------------------------------------------------- #
    def hud_text(self):
        return self._hud_actor.GetInput() if self._hud_actor is not None else None

    def current_labels(self):
        """{button id: current label} snapshot (post refresh_labels). Safe zip:
        both lists are buttons-only and stay in lockstep."""
        return {cid: carrier.GetInput().strip()
                for cid, (carrier, _fn, _cb) in zip(self._button_ids,
                                                    self.buttons)}

    def invoke(self, control_id):
        entry = self._buttons_by_id.get(control_id)
        if entry is None:
            return False
        entry[2]()                             # the callback
        return True


class EmbedHost:
    """The child-process embed runtime. Typical wiring (see stl_viewer.py /
    gcode_player.py):

        host = EmbedHost(spec)                      # spec from --embed
        chrome = host.make_chrome("Simulation", subtitle)
        ... build the scene; mount every 2D control through `chrome` ...
        host.attach(window, interactor, renderer)   # offscreen + observers
        host.on("scrub", fn); host.state_provider = fn   # player only
        host.loop(tick_ms=16)                       # replaces iren.Start()
    """

    def __init__(self, spec):
        # Pre-import EVERYTHING the loop will need BEFORE the stdin reader
        # thread starts. A lazy `import numpy` later (first _publish_frame)
        # deadlocks on Windows until the parent happens to write to stdin --
        # the extension-DLL load stalls while another thread sits in a
        # blocking stdin read (observed live on Py3.13; the faulthandler
        # stack shows create_module parked until stdin data arrives).
        import numpy as np
        from multiprocessing import shared_memory
        try:
            from vtk.util import numpy_support as ns
        except ImportError:                     # vtk not imported yet: fine,
            ns = None                           # the viewers import it first
        self._np = np
        self._ns = ns

        # Route VTK's error/warning output to STDERR, never the default Win32
        # popup: an embedded viewer is a child with no window of its own for
        # dialogs, so a vtkOutputWindow popup interrupts the user (and the GUI
        # already captures our stderr for the crash tail / log). Best-effort;
        # older VTK without the stderr display mode just keeps its default.
        try:
            import vtk
            ow = vtk.vtkOutputWindow.GetInstance()
            if hasattr(ow, "SetDisplayModeToAlwaysStdErr"):
                ow.SetDisplayModeToAlwaysStdErr()
        except Exception:  # noqa: BLE001
            pass

        # Match Tk's system-DPI awareness (the GUI process is DPI-aware via
        # tkinter). With both sides in the same coordinate space, the monitor
        # bounds the GUI passes for native fullscreen are directly usable and
        # DWM never scale-blurs the native window. Offscreen buffers are
        # pixel-exact regardless, so the embedded stream is unaffected.
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:  # noqa: BLE001
                pass

        name, max_w, max_h = parse_embed_spec(spec)
        self.max_w, self.max_h = max_w, max_h
        self._slot_bytes = max_w * max_h * 3
        self._shm = shared_memory.SharedMemory(name=name)
        if self._shm.size < 2 * self._slot_bytes:
            raise ValueError("embed shared memory smaller than 2 slots")

        # the REAL stdout is the protocol channel; stray prints -> stderr
        self._out = os.fdopen(os.dup(sys.stdout.fileno()), "w",
                              buffering=1, encoding="utf-8", newline="\n")
        sys.stdout = sys.stderr
        self._stdin_fd = sys.stdin.fileno()

        self._q = queue.Queue()
        self._reader = threading.Thread(target=self._read_stdin, daemon=True)
        self._reader.start()

        self.chrome = None
        self.window = None
        self.iren = None
        self.renderer = None
        self.state_provider = None       # () -> dict, exported as "state"
        # Per-tick clock callback (the player's _on_tick). The loop calls THIS
        # directly instead of iren.InvokeEvent("TimerEvent") when it is set --
        # invoking the interactor's generic TimerEvent also fires
        # vtkInteractorStyle::OnTimer, which RE-APPLIES the active interaction
        # State (Pan/Rotate/...) using the last event positions. During a
        # middle-button PAN (the one gesture the base trackball still owns --
        # orbit/turn/zoom are intercepted by install_cad_camera, so its State
        # stays NONE), that means every clock tick re-pans off stale positions:
        # the camera teleports on the first drag and drifts while held. The
        # tickless STL viewer never hit it (tick_ms=0 -> no TimerEvent); the
        # player does. Driving the clock directly keeps OnTimer un-triggered.
        self.tick_hook = None            # () -> None; see the loop
        self._handlers = {}              # command type -> fn(msg)
        self._w2i = None
        self._dirty = True
        self._visible = True
        self._done = False
        self._slot = 0
        self._seq = 0
        self._last_frame_t = 0.0
        self._last_state_t = 0.0
        self._sent_labels = {}
        self._sent_values = {}
        self._sent_hud = None
        self._sent_states = {}
        self._sent_readout = None
        self._sent_listing = None
        self._dark = None

        # native-fullscreen state (Task A). fs_enter_hook(fs_chrome) /
        # fs_exit_hook() let a viewer mount extras in the native window --
        # the player uses them for its VTK transport scrubber.
        self._native = False
        self._fs_chrome = None
        self._fs_key_tags = []
        self._fs_exit_requested = False
        self._pre_fs_size = None
        self._pending_resize = None
        self.fs_enter_hook = None
        self.fs_exit_hook = None
        # docked state (Windows): the render window lives INSIDE the panel as a
        # child of the GUI's viewport HWND, drawing GPU-direct -- no readback, no
        # Tk paste. `_dock_parent` outlives a fullscreen trip so exit re-docks.
        self._docked = False
        self._dock_parent = 0

    # -- wiring ------------------------------------------------------------- #
    def make_chrome(self, title, subtitle=""):
        self.chrome = EmbedChrome(self, title, subtitle)
        return self.chrome

    def make_interactor(self):
        """The embed child's interactor. On Windows this is the PLATFORM
        (Win32) interactor even though the window starts offscreen:
        synthesized events drive it exactly like the generic one, and it is
        what lets native fullscreen pump real OS input (ProcessEvents) into
        the SAME observers -- one interactor for the child's whole life
        (verified in scratch/spike_native_fs.py). Off-Windows, the generic
        interactor; native_fs is refused there."""
        import vtk
        if os.name == "nt":
            return vtk.vtkRenderWindowInteractor()
        return vtk.vtkGenericRenderWindowInteractor()

    def on(self, cmd, fn):
        """Register a handler for a parent command ('scrub', ...)."""
        self._handlers[cmd] = fn

    def attach(self, window, interactor, renderer, dark=None):
        """Take ownership of the render pipeline: offscreen, initial size,
        dirty tracking, and the generic interactor's render bridge."""
        import vtk
        self.window = window
        self.iren = interactor
        self.renderer = renderer
        window.SetOffScreenRendering(1)
        w = min(960, self.max_w)
        h = min(640, self.max_h)
        window.SetSize(w, h)
        try:
            interactor.SetSize(w, h)
        except Exception:  # noqa: BLE001
            pass
        self._w2i = vtk.vtkWindowToImageFilter()
        self._w2i.SetInput(window)
        self._w2i.ReadFrontBufferOff()
        # every publish follows an explicit Render(); the filter's default
        # re-render before reading back is pure per-frame waste
        self._w2i.ShouldRerenderOff()
        window.AddObserver("EndEvent", self._mark_dirty)
        # vtkGenericRenderWindowInteractor.Render() only INVOKES RenderEvent;
        # bridge it to a real render so interaction actually draws. The
        # platform (Win32) interactor renders the window itself, so the
        # bridge would double-render there.
        if interactor.IsA("vtkGenericRenderWindowInteractor"):
            interactor.AddObserver("RenderEvent", lambda *_: window.Render())
        interactor.AddObserver("ExitEvent", lambda *_: self._set_done())
        if dark is None:
            try:
                import gui_viewer_shell
                dark = gui_viewer_shell.read_dark_pref()
            except Exception:  # noqa: BLE001
                dark = False
        self._dark = bool(dark)

    def _mark_dirty(self, *_):
        self._dirty = True

    def _set_done(self, *_):
        self._done = True

    # -- stdin pump ----------------------------------------------------------#
    def _read_stdin(self):
        """Raw-fd reads (not the buffered sys.stdin iterator): a thread parked
        in the TextIOWrapper's locked read participates in the Windows import
        deadlock described in __init__; os.read holds no Python-side locks."""
        buf = b""
        try:
            while True:
                chunk = os.read(self._stdin_fd, 65536)
                if not chunk:
                    break                     # parent closed the pipe
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._q.put(json.loads(line.decode("utf-8")))
                    except (ValueError, UnicodeDecodeError):
                        continue
        except Exception:  # noqa: BLE001
            pass
        self._q.put({"t": "quit"})       # parent gone -> exit cleanly

    # -- channel out ----------------------------------------------------------#
    def send(self, obj):
        try:
            self._out.write(json.dumps(obj, separators=(",", ":")) + "\n")
        except OSError:
            self._done = True

    def resend_hello(self):
        """Re-send the hello so the embedded rail REBUILDS with the CURRENT chrome
        controls. Used when a viewer adds a sidebar section mid-session -- e.g. the
        micrograph sliders appear once an image is loaded. The panel's
        _on_event('hello') rebuilds its rail; button ids stay stable so existing
        dispatch keeps working, and the new controls simply appear."""
        self._send_hello(getattr(self, "_hello_transport", False),
                         getattr(self, "_hello_total", 0.0))

    def _send_hello(self, has_transport, total):
        self._hello_transport = has_transport      # remembered for resend_hello
        self._hello_total = total
        c = self.chrome
        if c is not None:
            # the switch positions as they are NOW, not when each button was
            # registered (before the scene finished building)
            states = c.current_states()
            for spec in c.controls:
                if spec.get("id") in states:
                    spec["on"] = states[spec["id"]]
            self._sent_states = dict(states)
        # a hello rebuilds the panel's rail, which starts empty: forget what
        # was sent so the next pump sends it all again. Without this a paused
        # viewer that resent its hello (a micrograph loaded) left the whole
        # readout reading "—" until playback moved.
        self._sent_readout = None
        self._sent_hud = None
        self._sent_labels = {}
        self._sent_values = {}
        self._sent_listing = object()
        self.send({
            "t": "hello",
            "title": c.title if c else "",
            "subtitle": c.subtitle if c else "",
            "controls": (c.controls if c else []),
            "footers": (c.footers if c else []),
            "has_transport": bool(has_transport),
            "total": float(total or 0.0),
            "native_fs": os.name == "nt",
            # the panel may hand over its HWND to dock the render window in it
            "dock": os.name == "nt",
            # whether this viewer registered a live readout (set_hud). The sims
            # do; the STL viewer doesn't, so its rail omits the READOUT block
            # instead of showing a dead heading (the VTK sidebar already omits
            # it -- _hud_header is only built in set_hud).
            "has_readout": bool(c and c._hud_actor is not None),
            # the readout also arrives as labelled rows ("readout" messages), so
            # the rail can draw a table instead of the HUD text block
            "has_rows": bool(c and c._readout_fn is not None),
        })

    # -- input replay ----------------------------------------------------------#
    _BTN_EVENTS = {
        1: ("LeftButtonPressEvent", "LeftButtonReleaseEvent"),
        2: ("MiddleButtonPressEvent", "MiddleButtonReleaseEvent"),
        3: ("RightButtonPressEvent", "RightButtonReleaseEvent"),
    }

    def _flip_y(self, y):
        return max(0, self.window.GetSize()[1] - 1 - int(y))

    def _apply_mouse(self, m):
        iren = self.iren
        x = int(m.get("x", 0))
        y = self._flip_y(m.get("y", 0))
        ctrl = int(bool(m.get("ctrl")))
        shift = int(bool(m.get("shift")))
        iren.SetEventInformation(x, y, ctrl, shift, chr(0), 0, None)
        ev = m.get("ev")
        if ev == "move":
            iren.MouseMoveEvent()
        elif ev == "press":
            pair = self._BTN_EVENTS.get(int(m.get("btn", 1)))
            if pair:
                getattr(iren, pair[0])()
        elif ev == "release":
            pair = self._BTN_EVENTS.get(int(m.get("btn", 1)))
            if pair:
                getattr(iren, pair[1])()
        elif ev == "wheel":
            if float(m.get("delta", 0)) > 0:
                iren.MouseWheelForwardEvent()
            else:
                iren.MouseWheelBackwardEvent()

    def _apply_key(self, m):
        sym = m.get("sym") or ""
        char = m.get("char") or ""
        self.iren.SetKeyEventInformation(int(bool(m.get("ctrl"))),
                                         int(bool(m.get("shift"))),
                                         char[:1] or chr(0), 0, sym)
        self.iren.KeyPressEvent()

    def _apply(self, m):
        t = m.get("t")
        if t == "quit":
            self._done = True
        elif t == "mouse":
            self._apply_mouse(m)
        elif t == "key":
            self._apply_key(m)
        elif t == "btn":
            if self.chrome is not None and self.chrome.invoke(m.get("id", "")):
                self._dirty = True
                # render NOW in both modes: natively so the click shows, and
                # offscreen because w2i has ShouldRerenderOff -- without a
                # render the next published frame is the stale buffer (the
                # tickless STL viewer never repaints on its own)
                self.window.Render()
        elif t == "ctl":
            if (self.chrome is not None
                    and self.chrome.apply_value(m.get("id", ""), m.get("value"))):
                self._dirty = True
                self.window.Render()   # same: offscreen w2i grabs, not renders
        elif t == "resize":
            w = max(32, min(int(m.get("w", 32)), self.max_w))
            h = max(32, min(int(m.get("h", 32)), self.max_h))
            if self._native:
                # the window is the fullscreen surface right now; remember the
                # panel size and apply it when fullscreen exits
                self._pending_resize = (w, h)
            elif self._docked:
                self._fit_docked(w, h)
                self.window.Render()
            elif (w, h) != tuple(self.window.GetSize()):
                self.window.SetSize(w, h)
                try:
                    self.iren.SetSize(w, h)
                    self.iren.ConfigureEvent()
                except Exception:  # noqa: BLE001
                    pass
                self.window.Render()
        elif t == "theme":
            self._dark = bool(m.get("dark"))
            try:
                import gui_viewer_shell
                gui_viewer_shell.apply_viewport_bg(self.renderer, self._dark)
            except Exception:  # noqa: BLE001
                pass
            if self._native and self._fs_chrome is not None:
                # the fullscreen sidebar reads its palette at build time;
                # rebuild it so a dark-mode flip lands live
                tab = getattr(self._fs_chrome, "_tab", None)
                self._teardown_fs_chrome()
                self._fs_chrome = self._build_fs_chrome(tab=tab)
                if self.fs_enter_hook is not None:   # the transport bar too
                    try:
                        self.fs_enter_hook(self._fs_chrome)
                    except Exception:  # noqa: BLE001
                        pass
            self.window.Render()
        elif t == "projection":
            # the Settings window's Perspective switch, flipped while open
            try:
                import vtk_helpers
                vtk_helpers.set_prefer_perspective(m.get("perspective"))
                if vtk_helpers.set_projection(
                        self.renderer, parallel=not m.get("perspective")):
                    self._dirty = True
                    self.window.Render()
            except Exception:  # noqa: BLE001
                pass
        elif t == "native_fs":
            if bool(m.get("on")) and not self._native:
                self._enter_native_fs(m)
            elif not bool(m.get("on")) and self._native:
                self._exit_native_fs()
            else:                    # no-op ask: restate the truth anyway
                self.send({"t": "native_fs", "on": self._native})
        elif t == "dock":
            self._dock(m)
        elif t == "visible":
            self._visible = bool(m.get("on", True))
            if self._visible:
                self._dirty = True
            if self._docked:
                # a docked window draws over whatever page is in front, so it
                # hides with its page (the shell lifts pages, never unmaps them)
                self._show_docked(self._visible)
        else:
            fn = self._handlers.get(t)
            if fn is not None:
                fn(m)

    # -- native fullscreen (Task A) --------------------------------------------#
    def _hwnd(self):
        """The render window's Win32 HWND (GetGenericWindowId returns a SWIG
        pointer string like '_00000000000b093c_p_void')."""
        try:
            raw = self.window.GetGenericWindowId()
            if not raw:
                return 0
            if isinstance(raw, str):
                return int(raw.strip("_").split("_")[0], 16)
            return int(raw)
        except Exception:  # noqa: BLE001
            return 0

    def _fs_bounds(self, m):
        """The rect for the native fullscreen window. `exact` (tests) uses the
        passed x/y/w/h verbatim; otherwise the GUI's top-left point picks the
        MONITOR (MonitorFromPoint) so fullscreen lands on the app's screen,
        not the primary. Both processes are system-DPI aware (see __init__),
        so the coordinate spaces agree."""
        x = int(m.get("x") or 0)
        y = int(m.get("y") or 0)
        w = int(m.get("w") or 0)
        h = int(m.get("h") or 0)
        if m.get("exact") and w >= 32 and h >= 32:
            return x, y, w, h
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32

            class _MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD),
                            ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT),
                            ("dwFlags", wintypes.DWORD)]

            mon = user32.MonitorFromPoint(
                wintypes.POINT(x + 40, y + 40), 2)   # DEFAULTTONEAREST
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                r = mi.rcMonitor
                return (r.left, r.top, r.right - r.left,
                        r.bottom - r.top + _FS_OVERHANG)
            return (0, 0, int(user32.GetSystemMetrics(0)),
                    int(user32.GetSystemMetrics(1)) + _FS_OVERHANG)
        except Exception:  # noqa: BLE001
            pass
        return (0, 0, w, h) if (w >= 32 and h >= 32) else (0, 0, 1280, 800)

    def _make_borderless(self, x, y, w, h):
        """Force a genuinely borderless window at the given rect. VTK's
        SetBorders(0) only takes effect at window CREATION; our window was
        already realized offscreen, so it keeps its caption/frame on the flip
        (a title bar over 'fullscreen'). Drop WS_CAPTION|WS_THICKFRAME, set
        WS_POPUP, and SWP_FRAMECHANGED to re-apply -- the reliable path."""
        hwnd = self._hwnd()
        if not hwnd:
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            GWL_STYLE = -16
            WS_POPUP = 0x80000000
            WS_VISIBLE = 0x10000000
            WS_CAPTION = 0x00C00000
            WS_THICKFRAME = 0x00040000
            WS_MINMAXBOX = 0x00030000
            SWP_FRAMECHANGED = 0x0020
            SWP_NOZORDER = 0x0004
            style = u.GetWindowLongW(hwnd, GWL_STYLE)
            style &= ~(WS_CAPTION | WS_THICKFRAME | WS_MINMAXBOX)
            style |= WS_POPUP | WS_VISIBLE
            u.SetWindowLongW(hwnd, GWL_STYLE, style)
            u.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h),
                           SWP_FRAMECHANGED | SWP_NOZORDER)
        except Exception:  # noqa: BLE001
            pass

    def _raise_window(self):
        """Bring the just-shown native window to the front. The GUI granted
        this process foreground rights (AllowSetForegroundWindow) right before
        sending native_fs on; without them the window can open BEHIND the app."""
        hwnd = self._hwnd()
        if not hwnd:
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            flags = 0x0001 | 0x0002 | 0x0040   # NOSIZE | NOMOVE | SHOWWINDOW
            # a TOPMOST pulse lifts the window above every non-topmost window
            # even when foreground rights were NOT granted (the window keeps
            # its raised z-order after the release; it does not stay topmost,
            # so alt-tab layering behaves normally afterwards)
            u.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)   # HWND_TOPMOST
            u.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)   # HWND_NOTOPMOST
            u.SetForegroundWindow(hwnd)                   # focus, if allowed
            u.SetFocus(hwnd)
        except Exception:  # noqa: BLE001
            pass

    def _build_fs_chrome(self, tab=None):
        """The fullscreen sidebar: gui_viewer_shell.RailChrome, the embedded
        rail's layout drawn in VTK, built from the SAME captured spec
        EmbedChrome exports to the Tk rail (labels, switch states and the
        readout stay live -- both read the same callbacks). `tab` is the rail
        tab to open on (the one the embedded rail was showing)."""
        import gui_viewer_shell as shell
        c = self.chrome
        if c is None:
            c = EmbedChrome(self, "MEWracle")
        fs = shell.RailChrome(self.window, self.iren, self.renderer, c,
                              has_transport=bool(
                                  getattr(self, "_hello_transport", False)),
                              state_fn=self.state_provider, tab=tab,
                              dark=self._dark)
        fs.layout()
        fs.install_click_handler(self.iren)
        return fs

    def _teardown_fs_chrome(self):
        """Remove the fullscreen sidebar completely -- renderers, observers,
        viewport confinement -- so the offscreen frames never carry it."""
        fs = self._fs_chrome
        if fs is None:
            return
        self._fs_chrome = None
        for tag in (getattr(fs, "_click_tag", None),
                    getattr(fs, "_move_tag", None),
                    getattr(fs, "_release_tag", None),
                    getattr(fs, "_configure_tag", None)):
            if tag is not None:
                try:
                    self.iren.RemoveObserver(tag)
                except Exception:  # noqa: BLE001
                    pass
        try:
            self.window.RemoveRenderer(fs.bg)
            self.window.RemoveRenderer(fs.chrome)
        except Exception:  # noqa: BLE001
            pass
        # Size the layer count to the HIGHEST remaining renderer, never a hard
        # SetNumberOfLayers(1): the STL viewer's orientation-marker widget owns
        # its own renderer at layer 1, and orphaning it above the layer count
        # makes vtkRendererCollection log "Invalid layer for renderer: not
        # rendered" on the next render -- the popup seen on fullscreen exit
        # (scratch/spike_fs_teardown3.py).
        self.window.SetNumberOfLayers(max(1, self._max_renderer_layer() + 1))
        self.renderer.SetViewport(0.0, 0.0, 1.0, 1.0)

    def _max_renderer_layer(self):
        mx = 0
        rens = self.window.GetRenderers()
        rens.InitTraversal()
        r = rens.GetNextItem()
        while r is not None:
            mx = max(mx, r.GetLayer())
            r = rens.GetNextItem()
        return mx

    def _install_fs_keys(self, esc_exits=True):
        """While native, the host owns the fullscreen keys on the interactor:
        Esc / F11 exit fullscreen (never quit -- the exit is deferred to the
        loop so it can't run inside ProcessEvents), q/Q are swallowed (closing
        a viewer is the rail's ✕, exactly like the embedded panel), and every
        CharEvent is aborted so VTK's built-in shortcuts ('e'/'q' exit, 'w'
        wireframe...) stay dead, matching embed behaviour. Priority 20 beats
        the player's own key handling (0) and char-swallow (10). Docked, the
        same guard runs with `esc_exits` off: there is no fullscreen to leave."""
        def _on_key(obj, _evt):
            sym = obj.GetKeySym() or ""
            if esc_exits and sym in ("Escape", "F11"):
                self._fs_exit_requested = True
            if sym in ("Escape", "F11", "q", "Q"):
                try:
                    obj.GetCommand(self._fs_key_tags[0]).SetAbortFlag(1)
                except Exception:  # noqa: BLE001
                    pass

        def _on_char(obj, _evt):
            try:
                obj.GetCommand(self._fs_key_tags[1]).SetAbortFlag(1)
            except Exception:  # noqa: BLE001
                pass

        t1 = self.iren.AddObserver("KeyPressEvent", _on_key, 20.0)
        t2 = self.iren.AddObserver("CharEvent", _on_char, 20.0)
        self._fs_key_tags = [t1, t2]

    def _remove_fs_keys(self):
        for tag in self._fs_key_tags:
            try:
                self.iren.RemoveObserver(tag)
            except Exception:  # noqa: BLE001
                pass
        self._fs_key_tags = []

    def _enter_native_fs(self, m):
        """⛶ on: flip the SAME render window (same renderers, same actors,
        same interactor -- no scene rebuild) from offscreen to an onscreen
        borderless surface at the app's monitor bounds, mount the fullscreen
        chrome, and start pumping native input. Replies truthfully."""
        if os.name != "nt" or self.window is None or self.iren is None:
            self.send({"t": "native_fs", "on": False})
            return
        try:
            x, y, w, h = self._fs_bounds(m)
            self._pre_fs_size = tuple(self.window.GetSize())
            if self._docked:
                # out of the panel first; _dock_parent is kept so exit re-docks
                self._undock_window()
            self.window.SetOffScreenRendering(0)
            self.window.SetBorders(0)
            self.window.SetPosition(int(x), int(y))
            self.window.SetSize(int(w), int(h))
            try:
                self.iren.SetSize(int(w), int(h))
            except Exception:  # noqa: BLE001
                pass
            self._native = True
            self._fs_exit_requested = False
            self._fs_chrome = self._build_fs_chrome(tab=m.get("tab"))
            if self.fs_enter_hook is not None:
                try:
                    self.fs_enter_hook(self._fs_chrome)
                except Exception:  # noqa: BLE001
                    pass
            self._install_fs_keys()
            self.window.Render()
            self._make_borderless(x, y, w, h)   # strip the caption VTK kept
            self.window.Render()
            self._raise_window()
            self.send({"t": "native_fs", "on": True})
        except Exception:  # noqa: BLE001 -- never die on a fullscreen ask
            try:
                self._exit_native_fs()
            except Exception:  # noqa: BLE001
                self.send({"t": "native_fs", "on": False})

    def _exit_native_fs(self):
        """Esc / ⛶ off / entry failure: back to offscreen streaming at the
        panel size (the latest resize the GUI sent while native, else the
        pre-fullscreen size)."""
        if self.fs_exit_hook is not None:
            try:
                self.fs_exit_hook()
            except Exception:  # noqa: BLE001
                pass
        tab = getattr(self._fs_chrome, "_tab", None)
        self._remove_fs_keys()
        self._teardown_fs_chrome()
        w, h = (self._pending_resize or self._pre_fs_size or (960, 640))
        self._pending_resize = None
        if self._dock_parent:
            # back into the panel, still onscreen: no offscreen round trip
            self._native = False
            try:
                self._dock_into(int(w), int(h))
                self.send({"t": "native_fs", "on": False, "tab": tab})
                self.send({"t": "dock", "on": True})
                return
            except Exception:  # noqa: BLE001 -- fall back to streaming
                import traceback
                traceback.print_exc()
                self._undock_window()
                self._dock_parent = 0
                self.send({"t": "dock", "on": False})
        self.window.SetOffScreenRendering(1)
        self.window.SetSize(int(w), int(h))
        try:
            self.iren.SetSize(int(w), int(h))
            self.iren.ConfigureEvent()
        except Exception:  # noqa: BLE001
            pass
        self._native = False
        self.window.Render()
        self._dirty = True                 # refresh the panel immediately
        # `tab`: the embedded rail follows the tab fullscreen was left on
        self.send({"t": "native_fs", "on": False, "tab": tab})

    # -- docked in the panel (Windows) -------------------------------------------#
    # The streamed panel pays a GPU readback in this process and a Tk image
    # paste in the GUI for every frame; at a maximised panel (~1900x1150) the
    # paste alone is ~33 ms, capping the panel near 20 fps while fullscreen ran
    # smooth. Docked, the SAME render window becomes a WS_CHILD of the GUI's
    # viewport HWND and draws GPU-direct, like fullscreen: the platform
    # interactor pumps real OS input (ProcessEvents) into the same observers.
    # Cross-process parenting couples the two input queues, so a hung child
    # can stall the GUI's input -- accepted for the frame rate; set
    # MEWRACLE_EMBED_BLIT=1 in the GUI's environment to keep streaming.
    _GWL_STYLE = -16
    _WS_CHILD = 0x40000000
    _WS_POPUP = 0x80000000
    _WS_VISIBLE = 0x10000000
    _WS_CLIPSIBLINGS = 0x04000000
    _WS_FRAME = 0x00C00000 | 0x00040000 | 0x00030000  # caption|thick|minmax

    def _dock(self, m):
        """{"t":"dock","hwnd":H,"w":,"h":} -- H the viewport's HWND (0 undocks
        back to streaming). Replies {"t":"dock","on":bool} with the truth."""
        parent = int(m.get("hwnd") or 0)
        if os.name != "nt" or self.window is None or self.iren is None:
            self.send({"t": "dock", "on": False})
            return
        if not parent:
            if self._docked:
                self._undock_window()
                self._dock_parent = 0
                self.window.SetOffScreenRendering(1)
                self.window.Render()
                self._dirty = True
            self._dock_parent = 0
            self.send({"t": "dock", "on": False})
            return
        self._dock_parent = parent
        if self._native:              # fullscreen owns the window; exit docks
            return
        w = max(32, min(int(m.get("w") or 0) or self.window.GetSize()[0],
                        self.max_w))
        h = max(32, min(int(m.get("h") or 0) or self.window.GetSize()[1],
                        self.max_h))
        try:
            self._dock_into(w, h)
            self.send({"t": "dock", "on": True})
        except Exception:  # noqa: BLE001 -- never die on a dock ask
            import traceback
            traceback.print_exc()
            try:
                self._undock_window()
                self.window.SetOffScreenRendering(1)
            except Exception:  # noqa: BLE001
                pass
            self._dock_parent = 0
            self._dirty = True
            self.send({"t": "dock", "on": False})

    def _dock_into(self, w, h):
        """Make the render window a borderless child of `_dock_parent` at
        (0, 0, w, h) and draw onscreen. Restyled and parented BEFORE the
        onscreen flip, so it never shows as a top-level window first."""
        import ctypes
        u = ctypes.windll.user32
        hwnd = self._hwnd()
        if not hwnd or not u.IsWindow(self._dock_parent):
            raise RuntimeError("no window to dock")
        style = u.GetWindowLongW(hwnd, self._GWL_STYLE)
        style &= ~(self._WS_POPUP | self._WS_FRAME)
        style |= self._WS_CHILD | self._WS_CLIPSIBLINGS
        # masked: a popup style has bit 31 set, which overflows a signed LONG
        u.SetWindowLongW(hwnd, self._GWL_STYLE,
                         ctypes.c_long(style & 0xFFFFFFFF))
        u.SetParent(hwnd, self._dock_parent)
        self.window.SetBorders(0)
        self.window.SetOffScreenRendering(0)
        self._docked = True
        self._install_fs_keys(esc_exits=False)
        self._fit_docked(w, h)
        self.window.Render()

    def _fit_docked(self, w, h):
        w = max(32, min(int(w), self.max_w))
        h = max(32, min(int(h), self.max_h))
        self.window.SetSize(w, h)
        try:
            self.iren.SetSize(w, h)
            self.iren.ConfigureEvent()
        except Exception:  # noqa: BLE001
            pass
        try:
            import ctypes
            # VTK sizes with SWP_NOMOVE; pin the child to the parent's origin
            # and show it only while its page is in front (FRAMECHANGED
            # re-applies the stripped style)
            show = 0x0040 if self._visible else 0x0080   # SHOW / HIDEWINDOW
            ctypes.windll.user32.SetWindowPos(
                self._hwnd(), 0, 0, 0, w, h,
                0x0004 | 0x0010 | 0x0020 | show)  # NOZORDER|NOACTIVATE|FRAMECHANGED
        except Exception:  # noqa: BLE001
            pass

    def _show_docked(self, on):
        try:
            import ctypes
            # SW_SHOWNA (no activation) / SW_HIDE
            ctypes.windll.user32.ShowWindow(self._hwnd(), 8 if on else 0)
        except Exception:  # noqa: BLE001
            pass
        if on:
            self.window.Render()

    def _undock_window(self):
        """Take the window out of the panel (a hidden top-level popup again).
        Leaves `_dock_parent` alone: the fullscreen trip re-docks on exit."""
        self._remove_fs_keys()
        self._docked = False
        hwnd = self._hwnd()
        if not hwnd:
            return
        import ctypes
        u = ctypes.windll.user32
        u.ShowWindow(hwnd, 0)                                   # SW_HIDE
        style = u.GetWindowLongW(hwnd, self._GWL_STYLE)
        style &= ~self._WS_CHILD
        style |= self._WS_POPUP
        # masked: a popup style has bit 31 set, which overflows a signed LONG
        u.SetWindowLongW(hwnd, self._GWL_STYLE,
                         ctypes.c_long(style & 0xFFFFFFFF))
        u.SetParent(hwnd, 0)

    # -- publishing ----------------------------------------------------------#
    def _publish_frame(self):
        np, ns = self._np, self._ns
        if ns is None:                          # vtk arrived after __init__
            from vtk.util import numpy_support as ns
            self._ns = ns
        self._w2i.Modified()
        self._w2i.Update()
        img = self._w2i.GetOutput()
        w, h, _d = img.GetDimensions()
        if w < 2 or h < 2 or w * h * 3 > self._slot_bytes:
            return
        arr = ns.vtk_to_numpy(img.GetPointData().GetScalars())
        arr = arr.reshape(h, w, -1)[::-1, :, :3]         # vtk rows are bottom-up
        # ONE copy, straight into the shared-memory slot (fps matters here:
        # a tobytes() intermediate doubled the per-frame copy cost)
        off = self._slot * self._slot_bytes
        dst = np.ndarray((h, w, 3), dtype=np.uint8,
                         buffer=self._shm.buf, offset=off)
        np.copyto(dst, arr)
        self._seq += 1
        self.send({"t": "frame", "slot": self._slot, "w": w, "h": h,
                   "seq": self._seq})
        self._slot = 1 - self._slot
        self._dirty = False
        self._last_frame_t = time.perf_counter()

    def _flush(self, frames=True):
        now = time.perf_counter()
        c = self.chrome
        if c is not None:
            c.refresh_labels()
            labels = c.current_labels()
            diff = {k: v for k, v in labels.items()
                    if self._sent_labels.get(k) != v}
            if diff:
                self._sent_labels.update(diff)
                self.send({"t": "labels", "labels": diff})
            values = c.current_values()
            vdiff = {k: v for k, v in values.items()
                     if self._sent_values.get(k) != v}
            if vdiff:
                self._sent_values.update(vdiff)
                self.send({"t": "values", "values": vdiff})
            hud = c.hud_text()
            if hud is not None and hud != self._sent_hud:
                self._sent_hud = hud
                self.send({"t": "hud", "text": hud})
            states = c.current_states()
            sdiff = {k: v for k, v in states.items()
                     if self._sent_states.get(k) != v}
            if sdiff:
                self._sent_states.update(sdiff)
                self.send({"t": "states", "states": sdiff})
            ro = c.readout()
            if ro is not None and ro != self._sent_readout:
                self._sent_readout = ro
                self.send({"t": "readout", "rows": ro[0], "status": ro[1]})
            lst = c.listing()
            if lst != self._sent_listing:
                self._sent_listing = lst
                self.send({"t": "listing", "data": lst})
        if (self.state_provider is not None
                and now - self._last_state_t >= _STATE_MIN_DT):
            try:
                self.send({"t": "state", **self.state_provider()})
                self._last_state_t = now
            except Exception:  # noqa: BLE001
                pass
        if (frames and self._dirty and self._visible and not self._native
                and not self._docked
                and now - self._last_frame_t >= _FRAME_MIN_DT):
            self._publish_frame()

    # -- the main loop (replaces interactor.Start()) ---------------------------#
    def loop(self, tick_ms=0, has_transport=False, total=0.0):
        # Windows quantizes lock/sleep timeouts to the system timer resolution
        # (15.6 ms by default), which stretches a 16 ms q.get() wait to ~27 ms
        # and caps the published animation at ~36 fps -- visibly stuttery next
        # to the old native window. Request 1 ms resolution for this process
        # (it is a dedicated viewer child; measured 36 -> ~60 fps).
        if os.name == "nt" and tick_ms:
            try:
                import ctypes
                ctypes.windll.winmm.timeBeginPeriod(1)
            except Exception:  # noqa: BLE001
                pass
        self.iren.Initialize()
        try:
            import gui_viewer_shell
            gui_viewer_shell.apply_viewport_bg(self.renderer, self._dark)
        except Exception:  # noqa: BLE001
            pass
        # Render first, then hello, then an immediate flush: the scene is built
        # by the time its switch positions are read, and the GUI gets hello,
        # states, readout and state in one batch, so nothing flips after the
        # rail enables.
        self.window.Render()
        self._send_hello(has_transport, total)
        self._flush(frames=False)     # the first frame waits for the GUI's size
        self._dirty = True
        period = (tick_ms / 1000.0) if tick_ms else 0.0
        next_tick = time.perf_counter()
        try:
            while not self._done:
                now = time.perf_counter()
                if period:
                    if now >= next_tick:
                        while next_tick <= now:      # catch up, don't spiral
                            next_tick += period
                        # Drive the clock directly when a hook is set, NOT via the
                        # interactor's TimerEvent -- that would trigger the base
                        # style's OnTimer and re-apply an in-progress pan off stale
                        # positions (see tick_hook in __init__). Fallback keeps any
                        # TimerEvent-driven viewer working.
                        if self.tick_hook is not None:
                            self.tick_hook()
                        else:
                            self.iren.InvokeEvent("TimerEvent")
                        if self._native and self._fs_chrome is not None:
                            # keep the fullscreen chrome live while playing
                            # (the transport knob/time track the clock; shows
                            # on the next tick's render)
                            try:
                                self._fs_chrome.refresh_labels()
                            except Exception:  # noqa: BLE001
                                pass
                    timeout = max(0.001,
                                  min(next_tick - time.perf_counter(), 0.05))
                else:
                    timeout = 0.05
                if self._native or self._docked:
                    # pump real OS input into the interactor (mouse/keys on
                    # the native window) and keep the wait short so input
                    # latency stays a few ms. Esc exits are DEFERRED here so
                    # the window is never torn down inside ProcessEvents.
                    try:
                        self.iren.ProcessEvents()
                    except Exception:  # noqa: BLE001
                        pass
                    if self._fs_exit_requested and self._native:
                        self._fs_exit_requested = False
                        self._exit_native_fs()
                    else:
                        timeout = min(timeout, 0.005)
                # Drain the whole backlog THIS cycle, then coalesce redundant
                # mouse moves before applying (each apply may cost a full
                # render); see _coalesce_moves. Blocks briefly for the first
                # command so an idle child isn't a busy-loop.
                cmds = []
                try:
                    cmds.append(self._q.get(timeout=timeout))
                    while True:
                        cmds.append(self._q.get_nowait())
                except queue.Empty:
                    pass
                for m in _coalesce_moves(cmds):
                    self._apply(m)
                self._flush()
        finally:
            self.send({"t": "bye"})
            try:
                self._shm.close()
            except Exception:  # noqa: BLE001
                pass
        return 0


def pop_embed_arg(args):
    """Split ['--embed', 'name:WxH' | '--embed=name:WxH'] out of an argv list.
    Returns (remaining_args, spec_or_None). Used by stl_viewer's hand parser;
    gcode_player uses argparse instead."""
    rest, spec = [], None
    i = 0
    while i < len(args):
        key, eq, val = args[i].partition("=")
        if key == "--embed":
            if eq:
                spec = val
                i += 1
            elif i + 1 < len(args):
                spec = args[i + 1]
                i += 2
            else:
                i += 1
            continue
        rest.append(args[i])
        i += 1
    return rest, spec
