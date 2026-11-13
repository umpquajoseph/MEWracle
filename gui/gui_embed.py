#!/usr/bin/env python3
"""gui_embed.py -- MODERN-UI ONLY: the GUI side of the embedded viewers.

The STL viewer and the locked/live simulations render INSIDE the shell's result
pages while staying separate PROCESSES (the crash protection the old windowed
launches existed for): the child renders offscreen into a shared-memory double
buffer owned here, this side blits it onto a plain Tk label and forwards
mouse/keyboard input back down the child's stdin. All the 2D chrome -- toggle buttons,
HUD readout, keybind legend, print-time
-- arrives as a control SPEC in the child's `hello` message and is rendered here
as real ttk widgets, in the app sidebar's visual language.

Pieces:
  EmbedSession   -- owns one child: the shared memory, the Popen, the stdout
                    reader thread (JSON events -> Tk-thread poll), the stderr
                    tail (crash report), and the command writer.
  EmbedViewport  -- the blit surface: PhotoImage frames + input capture
                    (orbit/pan/zoom drags, wheel, keys) forwarded verbatim.
                    On Windows the panel DOCKS the child's render window into
                    it after the first frame (ViewerPanel._maybe_dock), so it
                    draws GPU-direct and takes OS input itself; the blit path
                    is the fallback (MEWRACLE_EMBED_BLIT=1 forces it).
  ViewerPanel    -- one embedded viewer: control rail + viewport + optional
                    transport bar, with the idle / starting / running / stopped
                    / crashed states and the fullscreen toggle. Fullscreen is
                    NATIVE: the CHILD flips its render window to a borderless
                    onscreen surface on the app's monitor -- GPU-direct and
                    vsynced, no Tk blit ceiling -- carrying the same chrome as a
                    themed VTK sidebar. The child replies on every actual
                    transition, Esc in the native window included, so the state
                    here is always truthful; frames resume at panel size on exit.
  _EmbedMixin    -- the ModernApp glue: reroutes the classic `_spawn_player`
                    into the panels, relaunches running viewers when a new run
                    lands, forwards dark-mode flips, and shuts sessions down with
                    the app. It does NOT reroute stl_viewer -- since the U2 merge
                    the modern shell never opens that window.

A child that dies (any exit while unrequested) surfaces as a calm in-panel
message with the stderr tail and a Relaunch button -- the GUI never goes down
with it. The classic (developer) UI never imports this module; its windowed
launches are untouched."""

import itertools
import json
import os
import re
import queue
import subprocess
import sys
import threading
import time
from collections import deque

import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont

import gui_fonts
import gui_form
import gui_pagestack
import gui_tips
import gui_widgets
# the shared scrollable container (build into .body; auto-hiding bar), so a tall
# rail (the live sim's long key legend) scrolls instead of clipping
from gui_common import VScrollFrame
# the width shared with the fullscreen VTK sidebar (gui_viewer_shell). Import-free
# module, so it never drags vtk into this GUI-process file.
from viewer_sidebar_spec import SIDEBAR_W
import viewer_sidebar_spec

# PIL gives the fast blit path: ImageTk.PhotoImage.paste() updates the SAME
# Tk image in place (Tk_PhotoPutBlock) -- ~6.5 ms/frame at panel size vs
# ~22 ms for a fresh tk.PhotoImage from PPM bytes (a 45 fps ceiling that read
# as stutter). PIL ships with this install; degrade to the PPM path without it.
try:
    from PIL import Image as _PILImage
    from PIL import ImageTk as _PILImageTk
except Exception:  # noqa: BLE001
    _PILImage = _PILImageTk = None

from app_paths import resource_path as _resource_path  # noqa: E402
HERE = _resource_path()   # the repo root from source, the bundle root frozen
IS_WINDOWS = os.name == "nt"

# viewport backdrop -- mirrors gui_viewer_shell.viewport_bg (not imported:
# that module imports vtk at module scope; this one must stay vtk-free)
_VIEW_BG_DARK = "#1a1c1f"
_VIEW_BG_LIGHT = "#e7eaee"

RAIL_W = SIDEBAR_W           # the control rail's FLOOR width -- the shared minimum
                             # with the fullscreen VTK sidebar (viewer_sidebar_spec.
                             # SIDEBAR_W). Clears the player's widest live readout
                             # line -- "v(act/set) NN.N/NN.N mm/s xN.NN" -- at the
                             # DEFAULT font. _build_rail AUTOFITS upward from here to
                             # the measured content in the LIVE font, so a high-DPI /
                             # display-scaled machine (bigger ttk fonts) never clips a
                             # button label. The width only ever grows (see
                             # _fit_rail_width), so the rail is pinned per-frame (no
                             # readout jitter; see pack_propagate in ViewerPanel).
_POLL_MS = 8                 # session event pump cadence. Deliberately tight:
                             # the child publishes ~60 frames/s and this pump
                             # is the only thing between a frame arriving and
                             # its blit -- 30 ms here quantized playback to a
                             # visibly stuttery ~30 fps
_RESIZE_DEBOUNCE_MS = 140    # viewport Configure -> child resize
_SCRUB_MIN_DT = 0.06         # transport drag -> scrub command throttle, s

_session_counter = itertools.count(1)


def _widget_alive(w):
    try:
        return bool(w.winfo_exists())
    except tk.TclError:
        return False


# The rail's layout data is shared with the fullscreen VTK rail
# (gui_viewer_shell.RailChrome) through the import-free spec module.
_TAB_OF_SECTION = viewer_sidebar_spec.TAB_OF_SECTION
_TRANSPORT_ROLES = viewer_sidebar_spec.TRANSPORT_ROLES
_EMPTY_VALUE = viewer_sidebar_spec.EMPTY_VALUE
_READOUT_ROWS = viewer_sidebar_spec.READOUT_ROWS

_LISTING_REPLACES = viewer_sidebar_spec.LISTING_REPLACES
_LISTING_ROWS = viewer_sidebar_spec.LISTING_ROWS
_LISTING_COLS = viewer_sidebar_spec.LISTING_COLS
_legend_rows = viewer_sidebar_spec.legend_rows


def _skeleton_hello():
    """The rail the Viewer page shows before any viewer has run: the player's
    default --static launch, in the order gcode_player._build_buttons registers
    it, so its control ids (b0, b1, ...) are the ones the child will send.

    The rail is built from this at page build and disabled. When a viewer
    opens with the same layout (_rail_signature), the rail is only enabled and
    filled -- nothing is rebuilt, so nothing paints in. A launch with a
    different layout (machine on, live edit) rebuilds once, in one callback,
    and that hello becomes the skeleton for the next idle."""
    specs = [("Playback", "Play", {"role": "play"}),
             ("Playback", "Slower", {"role": "slower"}),
             ("Playback", "Faster", {"role": "faster"}),
             ("Playback", "Restart", {"role": "restart"}),
             ("Scaffold", "Hide collector", {"toggle": "Collector"}),
             ("Scaffold", "Hide deposition", {"toggle": "Deposition"}),
             ("Scaffold", "Show g-code path", {"toggle": "G-code path"}),
             ("Scaffold", "Scaffold colors", {"toggle": "Scaffold colors"}),
             ("Printer", "Show printer", {"toggle": "Machine"}),
             ("Printer", "Show printhead", {"toggle": "Printhead"}),
             ("Printer", "See-through printhead",
              {"toggle": "See-through printhead"}),
             ("Printer", "Show jet", {"toggle": "Jet"}),
             ("Simulation", "Hide travels", {"toggle": "Travels"}),
             ("Simulation", "Live edit", {"toggle": "Live edit"}),
             ("Deviation", "Show deviation", {"toggle": "Deviation"}),
             ("View", "Snap top-down", {}),
             ("View", "Flat lighting", {"toggle": "Studio lighting"}),
             ("View", "Hide view cube", {"toggle": "View cube"})]
    controls = []
    for i, (section, label, extra) in enumerate(specs):
        spec = {"id": f"b{i}", "type": "button", "section": section,
                "label": label, "swatch": None, **extra}
        if "toggle" in extra:
            spec["on"] = False
        controls.append(spec)
    return {"t": "hello", "title": "Simulation", "subtitle": "",
            "controls": controls, "footers": [], "has_transport": True,
            "total": 0.0, "native_fs": True, "has_readout": True,
            "has_rows": True}


def _rail_signature(hello):
    """What decides the rail's LAYOUT. Labels are not in it (they stream);
    ids are, so an equal signature means the built widgets already carry the
    ids the child will send."""
    return (hello.get("title", ""), bool(hello.get("has_transport")),
            bool(hello.get("has_rows")), bool(hello.get("has_readout", True)),
            tuple((c.get("id"), c.get("type", "button"), c.get("section", ""),
                   c.get("role", ""), c.get("toggle", ""),
                   c.get("min"), c.get("max"))
                  for c in hello.get("controls", [])))


def _fmt_time(secs):
    secs = max(0, int(round(float(secs or 0.0))))
    m, s = divmod(secs, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def embed_frame_cap(root):
    """Largest frame (w, h) the child may render: the whole virtual desktop.
    Tk's winfo_screenwidth/height is the PRIMARY monitor only on Windows, so a
    window dragged onto a bigger secondary display (an ultrawide beside a
    laptop panel) had its frames clamped to the laptop's size and drawn small
    inside the viewport. The GUI is DPI-aware, so these are physical pixels."""
    w = int(root.winfo_screenwidth())
    h = int(root.winfo_screenheight())
    if IS_WINDOWS:
        try:
            import ctypes
            gm = ctypes.windll.user32.GetSystemMetrics
            w = max(w, int(gm(78)))      # SM_CXVIRTUALSCREEN
            h = max(h, int(gm(79)))      # SM_CYVIRTUALSCREEN
        except Exception:  # noqa: BLE001
            pass
    return max(w, 1280), max(h, 800)


# ---------------------------------------------------------------------------
# the child process handle
# ---------------------------------------------------------------------------
class EmbedSession:
    """One embedded-viewer child process + its frame pipe. Events are
    delivered on the Tk thread via `on_event(kind, msg)`:
      'hello' / 'labels' / 'hud' / 'state' -> the child's JSON payload
      'frame'  -> msg + the frame already copied out of shared memory
      'exit'   -> {'code': int|None, 'stderr': tail, 'expected': bool}
    """

    def __init__(self, root, argv, on_event):
        self.root = root
        self.on_event = on_event
        self.expected_exit = False
        self._exit_sent = False
        max_w, max_h = embed_frame_cap(root)
        self.max_w, self.max_h = max_w, max_h
        self._slot_bytes = max_w * max_h * 3

        from multiprocessing import shared_memory
        name = f"mewembed_{os.getpid()}_{next(_session_counter)}"
        self.shm = shared_memory.SharedMemory(create=True, name=name,
                                              size=2 * self._slot_bytes)
        try:
            flags = 0x08000000 if IS_WINDOWS else 0   # CREATE_NO_WINDOW
            # The embed child streams JSON frames line-by-line over stdout. From
            # source it is launched `python -u ...` (unbuffered). The FROZEN relaunch
            # argv carries no -u, so force line-unbuffered stdio via the environment
            # to keep the frame stream flowing; None (unchanged) from source, so a
            # normal run is byte-identical.
            env = None
            if getattr(sys, "frozen", False):
                env = os.environ.copy()
                env["PYTHONUNBUFFERED"] = "1"
            self.proc = subprocess.Popen(
                list(argv) + ["--embed", f"{name}:{max_w}x{max_h}"],
                cwd=HERE, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", creationflags=flags, env=env)
        except Exception:
            self._close_shm()
            raise
        self._q = queue.Queue()
        self._err_tail = deque(maxlen=120)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self._poll_id = self.root.after(_POLL_MS, self._poll)

    # -- io threads (enqueue only; Tk work happens in _poll) ----------------- #
    def _read_stdout(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._q.put(json.loads(line))
                except ValueError:
                    continue
        except Exception:  # noqa: BLE001
            pass
        self._q.put({"t": "__eof__"})

    def _read_stderr(self):
        try:
            for line in self.proc.stderr:
                self._err_tail.append(line.rstrip("\n"))
        except Exception:  # noqa: BLE001
            pass

    # -- commands down ------------------------------------------------------- #
    def send(self, obj):
        try:
            self.proc.stdin.write(json.dumps(obj, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError, AttributeError):
            pass

    # -- the Tk-thread event pump --------------------------------------------#
    def _poll(self):
        self._poll_id = None
        alive_widget = True
        try:
            frame_msg = None
            eof = False
            while True:
                try:
                    msg = self._q.get_nowait()
                except queue.Empty:
                    break
                t = msg.get("t")
                if t == "frame":
                    frame_msg = msg          # latest-only: never blit stale
                elif t == "__eof__":
                    eof = True
                elif t:
                    # every other message type goes to the panel, which ignores
                    # what it doesn't know. An allow-list here once silently
                    # dropped the "states" and "readout" streams: the switches
                    # never moved and the readout sat on its placeholder.
                    self.on_event(t, msg)
            if frame_msg is not None:
                data = self._frame_bytes(frame_msg)
                if data is not None:
                    self.on_event("frame", {**frame_msg, "data": data})
            if eof or self.proc.poll() is not None:
                self._finish()
                return
        except tk.TclError:
            alive_widget = False
        if alive_widget:
            try:
                self._poll_id = self.root.after(_POLL_MS, self._poll)
            except tk.TclError:
                pass

    def _frame_bytes(self, msg):
        try:
            w, h, slot = int(msg["w"]), int(msg["h"]), int(msg["slot"])
        except (KeyError, TypeError, ValueError):
            return None
        n = w * h * 3
        if n <= 0 or n > self._slot_bytes or slot not in (0, 1):
            return None
        off = slot * self._slot_bytes
        return bytes(self.shm.buf[off:off + n])

    def _finish(self):
        try:
            self.proc.wait(timeout=0.5)
        except Exception:  # noqa: BLE001
            pass
        code = self.proc.poll()
        self._close_shm()
        if not self._exit_sent:
            self._exit_sent = True
            tail = "\n".join(list(self._err_tail)[-25:])
            self.on_event("exit", {"code": code, "stderr": tail,
                                   "expected": self.expected_exit})

    # -- lifecycle -------------------------------------------------------------#
    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        """Ask the child to exit; escalate to terminate if it lingers. The
        pump keeps running and reports the exit as expected."""
        self.expected_exit = True
        self.send({"t": "quit"})

        def _escalate():
            if self.alive():
                try:
                    self.proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
        try:
            self.root.after(900, _escalate)
        except tk.TclError:
            _escalate()

    def destroy(self):
        """Hard teardown (app close): kill, stop polling, release memory."""
        self.expected_exit = True
        self._exit_sent = True           # nobody left to show a state to
        if self._poll_id is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        try:
            if self.alive():
                self.proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        self._close_shm()

    def _close_shm(self):
        try:
            self.shm.close()
            self.shm.unlink()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# the blit surface + input capture
# ---------------------------------------------------------------------------
class EmbedViewport(tk.Label):
    """A plain Tk label showing the child's frames. Owns nothing but pixels:
    every mouse/key event is forwarded to the session; the placeholder text
    doubles as the idle/starting/crashed message surface."""

    def __init__(self, parent, dark=False):
        super().__init__(parent, background=_VIEW_BG_DARK if dark
                         else _VIEW_BG_LIGHT,
                         text="", font="MewHint", foreground="#888888",
                         anchor="center", justify="center",
                         takefocus=1, cursor="crosshair",
                         highlightthickness=1, borderwidth=0)
        self.session = None
        self._photo = None
        self._photo_size = None
        self._resize_id = None
        self._dark = dark
        self._move_last_t = 0.0          # GUI-side move-send throttle (Task B)
        self._move_pending = None        # newest move held back by the throttle
        self.bind("<Configure>", self._on_configure)
        self.bind("<Map>", self._on_map)
        self.bind("<Unmap>", self._on_unmap)
        for btn in (1, 2, 3):
            self.bind(f"<Button-{btn}>",
                      lambda e, b=btn: self._mouse("press", b, e))
            self.bind(f"<B{btn}-Motion>",
                      lambda e, b=btn: self._mouse("move", b, e))
            self.bind(f"<ButtonRelease-{btn}>",
                      lambda e, b=btn: self._mouse("release", b, e))
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<KeyPress>", self._on_key)

    # -- wiring ----------------------------------------------------------------#
    def attach(self, session):
        self.session = session
        self._send_resize()
        self._watch_page()
        self.send_visible()

    # The shell's PageStack keeps every page MAPPED and switches with lift(), so
    # <Map>/<Unmap> never fire on a page switch. A DOCKED render window is a
    # foreign child HWND that draws whenever its process renders, over whatever
    # page is in front -- the flash a relaunch-on-generate showed on Scaffold
    # designer. So visibility is "my page is the selected one", not "mapped".
    def _page_and_stack(self):
        w = self
        while w is not None and w.master is not None:
            if isinstance(w.master, gui_pagestack.PageStack):
                return w, w.master
            w = w.master
        return None, None

    def _watch_page(self):
        if getattr(self, "_page_bound", False):
            return
        _page, stack = self._page_and_stack()
        if stack is not None:
            stack.bind("<<NotebookTabChanged>>",
                       lambda _e: self.send_visible(), add="+")
        self._page_bound = True

    def shown(self):
        """Mapped AND (outside a PageStack, or on its selected page)."""
        try:
            if not self.winfo_ismapped():
                return False
            page, stack = self._page_and_stack()
            return page is None or str(stack.select()) == str(page)
        except tk.TclError:
            return False

    def send_visible(self):
        if self.session is not None:
            self.session.send({"t": "visible", "on": self.shown()})

    def detach(self):
        self.session = None
        self._photo = None
        self._photo_size = None
        self.configure(image="")

    def blit(self, w, h, data):
        if self.session is None:
            return
        try:
            if _PILImageTk is not None:
                img = _PILImage.frombuffer("RGB", (w, h), data,
                                           "raw", "RGB", 0, 1)
                if (self._photo is None
                        or getattr(self, "_photo_size", None) != (w, h)):
                    self._photo = _PILImageTk.PhotoImage(img)
                    self._photo_size = (w, h)
                    self.configure(image=self._photo, text="")
                else:
                    self._photo.paste(img)   # in-place: the fast 60 fps path
            else:
                ppm = b"P6 %d %d 255\n" % (w, h) + data
                self._photo = tk.PhotoImage(master=self, data=ppm)
                self.configure(image=self._photo, text="")
        except tk.TclError:
            pass

    def say(self, text):
        """Placeholder text (clears any frame)."""
        self._photo = None
        self._photo_size = None
        self.configure(image="", text=text)

    def retheme(self, dark, pal):
        self._dark = dark
        self.configure(background=_VIEW_BG_DARK if dark else _VIEW_BG_LIGHT,
                       foreground=pal.get("hint", "#888888"),
                       highlightbackground=pal.get("hairline", "#d9dce1"),
                       highlightcolor=pal.get("select_bg", "#2f6fd6"))

    # -- geometry ---------------------------------------------------------------#
    def _on_configure(self, _e=None):
        if self._resize_id is not None:
            self.after_cancel(self._resize_id)
        self._resize_id = self.after(_RESIZE_DEBOUNCE_MS, self._send_resize)

    def _send_resize(self):
        self._resize_id = None
        if self.session is None:
            return
        w = max(self.winfo_width(), 32)
        h = max(self.winfo_height(), 32)
        self.session.send({"t": "resize", "w": w, "h": h})

    def _on_map(self, _e=None):
        if self.session is not None:
            self.send_visible()
            self._send_resize()

    def _on_unmap(self, _e=None):
        if self.session is not None:
            self.session.send({"t": "visible", "on": False})

    # -- input forwarding ---------------------------------------------------------#
    @staticmethod
    def _mods(e):
        return {"shift": bool(e.state & 0x0001), "ctrl": bool(e.state & 0x0004)}

    _MOVE_MIN_DT = 0.005                 # >=5 ms between forwarded moves (~200 Hz)

    def _mouse(self, ev, btn, e):
        if self.session is None:
            if ev == "press":
                self.focus_set()
            return
        msg = {"t": "mouse", "ev": ev, "btn": btn,
               "x": e.x, "y": e.y, **self._mods(e)}
        if ev == "move":
            # Throttle the B*-Motion flood at the source (belt-and-braces to the
            # child's coalescing): drop a move that lands within 5 ms of the
            # last one sent, but REMEMBER it so the run's final position is
            # never lost -- press/release flush it. Without this the camera
            # could settle a few px shy of the cursor at drag end.
            now = time.perf_counter()
            if now - self._move_last_t < self._MOVE_MIN_DT:
                self._move_pending = msg
                return
            self._move_last_t = now
            self._move_pending = None
            self.session.send(msg)
            return
        if ev == "press":
            self.focus_set()             # keys route here once clicked
        elif ev == "release" and self._move_pending is not None:
            self.session.send(self._move_pending)   # flush the held-back move
        self._move_pending = None
        self.session.send(msg)

    def _on_wheel(self, e):
        if self.session is not None:
            self.session.send({"t": "mouse", "ev": "wheel", "x": e.x, "y": e.y,
                               "delta": 1 if e.delta > 0 else -1,
                               **self._mods(e)})
        return "break"

    # keys the GUI keeps for itself: Esc leaves fullscreen (panel binding),
    # q would kill the child out from under the page
    _SWALLOW = {"Escape", "q", "Q", "F11"}

    def _on_key(self, e):
        if e.keysym in self._SWALLOW:
            return None
        if self.session is not None:
            self.session.send({"t": "key", "sym": e.keysym,
                               "char": e.char or "", **self._mods(e)})
        return "break"


def _transport_icon(master, kind, size, color):
    """The play button's ▶ / ❚❚ as a drawn `size`-px image with clear space
    after it. Drawn, not typed: no UI font carries the media glyphs, and the
    face Windows substitutes for ❚ is an emoji font twice the expected width,
    which overflowed the button and cut "Pause" to "Paus"."""
    gap = size // 2 + 2
    img = tk.PhotoImage(master=master, width=size + gap, height=size)
    if kind == "pause":
        bar = max(2, round(size * 0.3))
        img.put(color, to=(0, 0, bar, size))
        img.put(color, to=(size - bar, 0, size, size))
    else:                                    # a right-pointing triangle
        w = round(size * 0.85)
        for y in range(size):
            run = round(w * (1.0 - abs(2 * y + 1 - size) / size))
            if run > 0:
                img.put(color, to=(0, y, run, y + 1))
    return img


# ---------------------------------------------------------------------------
# the panel: rail + viewport + transport + states + fullscreen
# ---------------------------------------------------------------------------
class ViewerPanel(ttk.Frame):
    """One embedded viewer, self-contained: give it a parent, the app, and a
    zero-arg `relauncher` (the same method the page's launch button runs) and
    call `launch(argv)`; everything else -- control rail, transport, states,
    fullscreen, crash reporting -- is handled here."""

    _styled_for = None       # the palette _card_styles last configured (shared)

    def __init__(self, parent, app, role, empty_text, relauncher=None,
                 no_rail=False, rail_tools=False):
        """`rail_tools` puts the window buttons (Controls / Close / Fullscreen)
        in a card of their own under the rail, over `self.rail_tools`, a frame for
        the page's own controls, so the page needs no row. Without it the panel
        draws its own status + button bar above the viewport."""
        super().__init__(parent)
        # The rail is its OWN single builder: every viewer's controls come from
        # one spec (viewer_embed.EmbedChrome) and are drawn here in parity with
        # the fullscreen VTK sidebar (test_viewer_embed), so it is not rebuilt
        # through gui_form. The one-pattern guard is told so, with the reason.
        gui_form.exempt(self, "the embedded viewer: its rail is drawn from one "
                              "spec in parity with the fullscreen sidebar, and "
                              "its bar is transport and live status")
        self.app = app
        self.role = role                     # "locked" | "live" | "stl" | "machine"
        self.empty_text = empty_text
        self.relauncher = relauncher
        # A panel with no_rail hides the whole 240-px control rail entirely (not just
        # its contents) so the 3-D view is full width -- used by the Machine-link
        # page, whose form already owns the state/position readout (the rail there is
        # pure duplication). The top toolbar (status / fullscreen / close) stays.
        self._no_rail = bool(no_rail)
        self.session = None
        self.state = "idle"                  # idle/starting/running/crashed
        self._hello = None                   # last hello payload (rail spec)
        self._last_state = None              # last child `state` payload (see _on_event)
        self._play_expect = None             # (playing, deadline) after a Play press
        self._label_vars = {}                # control id -> StringVar (buttons)
        self._value_scales = {}              # id -> (scale, entvar, entry, spec)
        self._value_entries = {}             # id -> (var, entry, spec) [number]
        self._value_guard = False            # suppress echo while applying values
        self._ctl_last = {}                  # control id -> last-send perf clock
        self._hud_var = tk.StringVar(value="")
        # The readout sits ABOVE the control sections, so its height drives where
        # every button lands. A viewer that swaps a SHORT status into the HUD (the
        # 1-line "Computing deviation…" while the analysis runs) would collapse the
        # ~11-line readout and jerk the whole control stack up, then back when the
        # readout returns -- the "sidebar structure briefly changes" flash. Pin the
        # readout to the tallest text it has shown (pad shorter text with blank
        # lines) so its height never shrinks and the controls never jump.
        self._hud_max_lines = 0
        self._time_var = tk.StringVar(value="")
        self._total = 0.0
        self._scrub_guard = False
        self._scrub_last = 0.0
        self._scrub_drag = False
        self._scales = []                    # every live transport scale
        self._fs_native = False              # child-owned native fullscreen up?
        self._docked = False                 # child's window drawing in the viewport?
        self._dock_asked = False             # dock requested for this session
        self._rail_scroller = None           # the built rail's VScrollFrame
        self._role_ids = {}                  # button role -> control id
        self._toggles = {}                   # control id -> ToggleSwitch
        self._rail_tab = tk.StringVar(value="")   # survives a hello rebuild
        self._tab_panes = {}                 # tab key -> pane frame
        self._cards = []                     # rail card tk.Frames (recoloured)
        self._card_paths = set()             # their widget paths (label surface)
        self._state_tag = None               # header Paused/Playing chip
        self._readout = None                 # the readout card's widgets
        self._readout_vals = {}              # row key -> value StringVar
        self._listing = None                 # the g-code listing's widgets
        self._listing_last = None            # its last data, kept across a rebuild
        self._legend = []                    # _legend_rows of the last hello
        self._legend_pop = None              # the open Controls popup
        self._rule_frames = []               # readout divider lines

        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)

        # -- rail (left): built/refilled per session from the hello spec ---- #
        # The rail and the transport bar are OVERLAYS on the viewport, placed
        # over it (`_place_overlays`) like the G-code page's boxes, so the 3-D
        # view takes the whole panel. Both stay children of the panel: a place()
        # master only has to be a sibling's descendant, and a child of the
        # viewport itself would sit under the docked render window.
        self.rail = ttk.Frame(self, style="Sidebar.TFrame", width=RAIL_W,
                              borderwidth=1, relief="solid")
        # Lock the rail to RAIL_W. The rail's children are managed by PACK
        # (_build_rail packs `outer`; the empty/crashed fills pack `body`), so
        # grid_propagate alone does NOT stop it resizing -- pack_propagate is
        # the one that matters. Without this, the live HUD readout (whose width
        # jumps as v_act fluctuates or the "xN.NN" speed scale is appended)
        # widens/narrows the rail every frame, resizing the viewport column and
        # visibly shaking the embedded viewer. grid_propagate stays for the
        # (unused) grid path; pack_propagate is what pins the width in practice.
        self.rail.grid_propagate(False)
        self.rail.pack_propagate(False)
        self._rail_sig = None                # _rail_signature of the built rail
        self._rail_fitted = False            # width measured (once, at first build)
        self._rail_inputs = []               # widgets enabled only while running

        # -- window controls: on the floating bar when the page asks for it,
        #    else a status + button bar of the panel's own over the viewport --- #
        self._bar_status = tk.StringVar(value="")
        self.rail_tools = self._foot = None
        self._foot_cards = []                # the foot outlives a rail rebuild
        if rail_tools:
            self._foot, inner = self._card(self)
            self._foot_cards = self._cards[:]
            self.rail_tools = ttk.Frame(inner, style="ViewerCard.TFrame")
            self.rail_tools.grid(row=0, column=0, sticky="ew")
            host = ttk.Frame(inner, style="ViewerCard.TFrame")
            host.grid(row=1, column=0, sticky="ew", pady=(6, 0))
            side = "right"
        else:
            host = ttk.Frame(self)
            host.grid(row=0, column=1, sticky="ew", pady=(0, 4))
            ttk.Label(host, textvariable=self._bar_status,
                      style="Hint.TLabel").pack(side="left")
            side = "right"
        # packed right-to-left, so they read Controls | Close | Fullscreen. The
        # foot's width is the rail's: there they share it by their words
        foot = dict(style="ViewerStep.TButton") if rail_tools else {}
        self.fs_btn = ttk.Button(host, text="⛶  Fullscreen",
                                 width=0 if rail_tools else 13,
                                 command=self.toggle_fullscreen,
                                 state="disabled", **foot)
        self.fs_btn.pack(side=side, padx=(4, 0), fill="x", expand=rail_tools)
        self.stop_btn = ttk.Button(host, text="✕  Close",
                                   width=0 if rail_tools else 9,
                                   command=self.stop, state="disabled", **foot)
        self.stop_btn.pack(side=side, padx=(4, 0), fill="x", expand=rail_tools)
        self.controls_btn = ttk.Button(host, text="ⓘ  Controls",
                                       width=0 if rail_tools else 11,
                                       command=self._toggle_legend,
                                       state="disabled", **foot)
        self.controls_btn.pack(side=side, padx=(4 if not rail_tools else 0, 0),
                               fill="x", expand=rail_tools)

        # -- viewport ---------------------------------------------------------#
        self.viewport = EmbedViewport(self, dark=self._is_dark())
        self.viewport.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self.viewport.say(self._idle_text())

        # -- transport + rail: the whole layout exists from page build. Idle is
        #    the same layout disabled, never a different one. A panel without a
        #    rail (the Machine-link page) has no playback clock either.
        self.transport = self._build_transport(self)
        if not self._no_rail:
            self._build_rail(self.rail, _skeleton_hello())
            self._set_running(False)
            self.viewport.bind("<Configure>", self._place_overlays, add="+")
            self.after_idle(self._place_overlays)

        self.bind("<Destroy>", self._on_destroy, add="+")
        gui_widgets._REGISTRY.add(self)      # retheme() rides the palette hook

    # -- helpers ------------------------------------------------------------- #
    def _is_dark(self):
        try:
            return bool(self.app.dark_mode.get())
        except (AttributeError, tk.TclError):
            return False

    def _send(self, obj):
        if self.session is not None:
            self.session.send(obj)

    def _send_key(self, sym):
        self._send({"t": "key", "sym": sym, "char": "",
                    "shift": False, "ctrl": False})

    def _build_transport(self, parent):
        """The panel's transport bar (in native fullscreen the CHILD draws its
        own timeline in-window instead). Every scale registers in self._scales
        so the state stream keeps them all in sync."""
        bar = ttk.Frame(parent, padding=(8, 4, 8, 4), borderwidth=1,
                        relief="solid")
        bar.columnconfigure(5, weight=1)
        self._speed_var = tk.StringVar(value="")
        # the play button's text box fits the longer word, measured, so the
        # button keeps one width across Play <-> Pause; the icon is an image
        # beside it (_set_play_label), outside that box
        font = tkfont.nametofont(ttk.Style().lookup("TButton", "font")
                                 or "TkDefaultFont")
        play_w = -(-max(font.measure("Play"), font.measure("Pause"))
                   // max(1, font.measure("0")))
        self._tbtn = {
            "restart": ttk.Button(bar, text="⏮", width=3,
                                  command=lambda: self._press_role("restart")),
            "play": ttk.Button(bar, text="Play", width=play_w, compound="left",
                               command=lambda: self._press_role("play")),
            "slower": ttk.Button(bar, text="−", width=3,
                                 command=lambda: self._press_role("slower")),
            "faster": ttk.Button(bar, text="+", width=3,
                                 command=lambda: self._press_role("faster")),
        }
        self._make_play_icons()
        self._set_play_label("Play")
        gui_tips.add_tip(self._tbtn["restart"], "Restart from the beginning")
        gui_tips.add_tip(self._tbtn["slower"], "Slower playback")
        gui_tips.add_tip(self._tbtn["faster"], "Faster playback")
        self._tbtn["restart"].grid(row=0, column=0, padx=(0, 4))
        self._tbtn["play"].grid(row=0, column=1, padx=(0, 10))
        self._tbtn["slower"].grid(row=0, column=2)
        self._speed_lbl = ttk.Label(bar, textvariable=self._speed_var, width=6,
                                    anchor="center", style="Hint.TLabel")
        self._speed_lbl.grid(row=0, column=3)
        self._tbtn["faster"].grid(row=0, column=4, padx=(0, 10))
        scale = ttk.Scale(bar, from_=0.0, to=max(self._total, 1e-6),
                          orient="horizontal", command=self._on_scale)
        scale.grid(row=0, column=5, sticky="ew")
        scale.bind("<ButtonPress-1>", self._on_scale_press)
        scale.bind("<ButtonRelease-1>",
                   lambda e, s=scale: self._on_scale_release(s))
        self._scales.append(scale)
        ttk.Label(bar, textvariable=self._time_var,
                  style="Hint.TLabel").grid(row=0, column=6, padx=(8, 0))
        return bar

    def _press_role(self, role):
        """A transport button: dispatch the child's button for that role. A
        viewer that exports no roles still gets play/pause through the key."""
        if role == "play" and self.session is not None:
            # show the new state now; the child's own "state" confirms it. A
            # state already in flight still carries the old value, so ignore a
            # contrary one for a moment rather than flick back.
            playing = not bool((self._last_state or {}).get("playing"))
            self._play_expect = (playing, time.perf_counter() + 1.0)
            self._sync_state_tag({"playing": playing})
            self._set_play_label("Pause" if playing else "Play")
        cid = self._role_ids.get(role)
        if cid is not None:
            self._send({"t": "btn", "id": cid})
        elif role == "play":
            self._send_key("space")

    def _sync_transport_roles(self):
        """Show only the transport buttons the child has a role for (play stays,
        falling back to the Space key)."""
        for role, btn in self._tbtn.items():
            try:
                if role == "play" or role in self._role_ids:
                    btn.grid()
                else:
                    btn.grid_remove()
            except tk.TclError:
                pass
        try:
            if "slower" in self._role_ids or "faster" in self._role_ids:
                self._speed_lbl.grid()
            else:
                self._speed_lbl.grid_remove()
        except tk.TclError:
            pass

    def _make_play_icons(self):
        """(Re)draw the play/pause icons in the palette's text colours, a normal
        and a disabled copy of each, sized to the button font's ascent."""
        pal = gui_widgets._PAL
        font = tkfont.nametofont(ttk.Style().lookup("TButton", "font")
                                 or "TkDefaultFont")
        size = max(7, round(font.metrics("ascent") * 0.7))
        self._play_icons = {
            (kind, st): _transport_icon(self, kind, size, pal[key])
            for kind in ("play", "pause")
            for st, key in (("on", "fg"), ("off", "disabled"))}

    def _set_play_label(self, label):
        playing = (label or "").strip().lower().startswith("pause")
        self._play_showing = playing
        kind = "pause" if playing else "play"
        icons = self._play_icons
        try:
            self._tbtn["play"].configure(
                text="Pause" if playing else "Play",
                image=(icons[(kind, "on")], "disabled", icons[(kind, "off")]))
        except tk.TclError:
            pass

    def _place_overlays(self, _e=None):
        """Lay the rail, its foot card and the transport bar over the picture.
        The rail is only as tall as its content, so the picture shows under it,
        and scrolls once the foot leaves it less. Called on every viewport resize
        and whenever the rail's size changes; a panel without a rail keeps its bar
        in the grid."""
        if self._no_rail:
            return
        vp = self.viewport
        try:
            W, H = vp.winfo_width(), vp.winfo_height()
            rail_w = int(self.rail.cget("width"))
        except (tk.TclError, ValueError):
            return
        if W < 200 or H < 160:
            return
        m = gui_fonts.SP["m"]
        bar_h = max(self.transport.winfo_reqheight(), 28)
        room = H - 2 * m
        if self._foot is not None:
            foot_h = self._foot.winfo_reqheight()
            room -= foot_h + m
            self._foot.place(in_=vp, x=m, y=H - m - foot_h, width=rail_w,
                             height=foot_h)
            self._foot.lift()
        body = self._rail_scroller.body if self._rail_scroller else None
        want = body.winfo_reqheight() + 2 if body is not None else room
        self.rail.place(in_=vp, x=m, y=m, width=rail_w, height=min(want, room))
        left = m + rail_w + m
        self.transport.place(in_=vp, x=left, y=H - m - bar_h, anchor="nw",
                             width=max(W - left - m, 240), height=bar_h)
        self.rail.lift()
        self.transport.lift()

    # -- lifecycle ------------------------------------------------------------- #
    def launch(self, argv, subtitle=""):
        """Start (or restart) the child on `argv`. The panel owns the --embed
        plumbing; callers pass exactly the windowed-viewer argv."""
        self.stop(quiet=True)
        self._docked = self._dock_asked = False
        self.state = "starting"
        self._bar_status.set(subtitle or "starting…")
        # it opens by itself: a 20-layer graft took ~30 s here with nothing
        # said, and was mistaken for a blank page waiting on a button
        self.viewport.say("Starting the viewer…\n\nIt opens by itself. A large "
                          "design can take up to a minute.")
        try:
            self.session = EmbedSession(self.app.root, argv, self._on_event)
        except Exception as exc:  # noqa: BLE001
            self.state = "crashed"
            self.viewport.say(f"Could not start the viewer\n({exc})")
            return
        self.viewport.attach(self.session)
        self.stop_btn.configure(state="normal")

    def stop(self, quiet=False):
        """Close the child (the panel returns to its idle state). A native
        fullscreen window dies with the child process."""
        self._set_fs_native(False)
        if self.session is not None:
            s = self.session
            self.session = None
            self.viewport.detach()
            s.on_event = lambda *_a, **_k: None   # a replaced session is mute
            s.stop()
        if not quiet:
            self._to_idle()

    def _idle_text(self):
        """What the empty viewport says. `empty_text` may be a callable, for a
        page whose idle has two meanings (nothing made yet, or a result whose
        viewer was closed)."""
        t = self.empty_text
        try:
            return t() if callable(t) else t
        except Exception:  # noqa: BLE001 -- a wording hook must never break idle
            return ""

    def refresh_idle(self):
        """Re-say the idle line when the page's state behind it changed (a run
        started or ended) while no viewer is open."""
        if self.state == "idle" and self.session is None:
            self.viewport.say(self._idle_text())

    def _to_idle(self):
        self.state = "idle"
        self._hud_var.set("")
        self._bar_status.set("")
        self.viewport.say(self._idle_text())
        self.stop_btn.configure(state="disabled")
        self.fs_btn.configure(state="disabled", text="⛶  Fullscreen")
        self.controls_btn.configure(state="disabled")
        self._close_legend()
        if self._no_rail:
            self.transport.grid_forget()
        else:
            self._set_running(False)

    def _on_destroy(self, _e=None):
        if self.session is not None:
            self.session.destroy()
            self.session = None

    # -- session events ----------------------------------------------------------#
    def _on_event(self, kind, msg):
        if kind == "hello":
            self.state = "running"
            self._hello = msg
            self._bar_status.set(msg.get("subtitle", ""))
            self._total = float(msg.get("total") or 0.0)
            for scale in self._scales:
                scale.configure(to=max(self._total, 1e-6))
            if self._no_rail:
                if msg.get("has_transport"):
                    self.transport.grid(row=2, column=1, sticky="ew",
                                        pady=(6, 0))
                else:
                    self.transport.grid_forget()
            self._role_ids = {c["role"]: c["id"]
                              for c in msg.get("controls", []) if c.get("role")}
            self._sync_transport_roles()
            play = self._role_ids.get("play")
            for c in msg.get("controls", []):
                if c.get("id") == play:
                    self._set_play_label(c.get("label", ""))
            if not self._no_rail:
                if _rail_signature(msg) != self._rail_sig:
                    self._build_rail(self.rail, msg)
                else:                          # same layout: fill, don't rebuild
                    self._fill_rail(msg)
                self._set_running(True)
            self._legend = _legend_rows(msg.get("footers"))
            self.controls_btn.configure(
                state="normal" if self._legend else "disabled")
            self.fs_btn.configure(
                state="normal" if msg.get("native_fs", True) else "disabled")
            self.stop_btn.configure(state="normal")
        elif kind == "labels":
            play = self._role_ids.get("play")
            for cid, text in (msg.get("labels") or {}).items():
                var = self._label_vars.get(cid)
                if var is not None:
                    var.set(text)
                if cid == play and self._play_expect is None:
                    self._set_play_label(text)
        elif kind == "states":
            for cid, on in (msg.get("states") or {}).items():
                sw = self._toggles.get(cid)
                if sw is not None:
                    try:
                        sw.set(on)
                    except tk.TclError:
                        pass
        elif kind == "readout":
            self._set_readout(msg.get("rows") or [], msg.get("status") or "")
        elif kind == "listing":
            self._set_listing(msg.get("data"))
        elif kind == "values":
            self._apply_values(msg.get("values") or {})
        elif kind == "hud":
            self._set_hud_text(msg.get("text", ""))
        elif kind == "note":
            # one line the viewer wants the user to read (it stepped down a
            # display quality): the status line, where every page reports
            st = getattr(self.app, "status", None)
            if st is not None and msg.get("text"):
                st.set(str(msg["text"]))
        elif kind == "render_used":
            # the viewer drew a coarser quality than the one asked for
            fn = getattr(self.app, "_render_stepped_down", None)
            if callable(fn):
                fn(str(msg.get("asked") or ""), str(msg.get("shown") or ""))
        elif kind == "state":
            # keep the last one: it is the only window into what the CHILD actually
            # thinks it is showing (playhead, and for machine-live the reveal cursor
            # + frame offset), which is what makes an end-to-end embed test possible
            self._last_state = msg
            self._sync_transport(msg)
            exp = self._play_expect
            if exp is not None and "playing" in msg:
                if (bool(msg["playing"]) == exp[0]
                        or time.perf_counter() > exp[1]):
                    self._play_expect = None
            if self._play_expect is None:
                self._sync_state_tag(msg)
                if "playing" in msg:             # the truth, not the label
                    self._set_play_label("Pause" if msg["playing"] else "Play")
        elif kind == "frame":
            if not self._fs_native:      # no stale frame over the placeholder
                self.viewport.blit(msg["w"], msg["h"], msg["data"])
                self._maybe_dock()
        elif kind == "dock":
            self._docked = bool(msg.get("on"))
        elif kind == "native_fs":
            self._set_fs_native(bool(msg.get("on")))
            if msg.get("tab"):          # the tab fullscreen was left on
                self._reveal_rail_tab(msg["tab"])
        elif kind == "request":
            # a child->parent request (a viewer sidebar button that needs
            # GUI-side work). Hand it to the app on the Tk thread; the app runs
            # the work and pushes the result back down via send_child.
            app = getattr(self, "app", None)
            if app is not None and hasattr(app, "_handle_viewer_request"):
                app._handle_viewer_request(self, msg)
        elif kind == "exit":
            self._on_exit(msg)

    def _set_hud_text(self, text):
        """Set the readout, padding it to the TALLEST it has shown so a short
        transient status (e.g. "Computing deviation…") never collapses the block
        and jerks the controls below it (see _hud_max_lines). A rebuild resets the
        max, so a different viewer's readout size never carries over."""
        lines = text.split("\n")
        self._hud_max_lines = max(self._hud_max_lines, len(lines))
        if len(lines) < self._hud_max_lines:
            text = text + "\n" * (self._hud_max_lines - len(lines))
        self._hud_var.set(text)

    def send_child(self, **kw):
        """Send a command DOWN to this panel's viewer child (apply-in-place: a
        computed deviation sidecar, an imported image, a mesh path). The child
        applies it live via a host.on(...) handler. No-op (returns False) when no
        session is running."""
        s = self.session
        if s is not None and s.alive():
            try:
                s.send(kw)
                return True
            except Exception:  # noqa: BLE001
                return False
        return False

    def _on_exit(self, msg):
        self._set_fs_native(False)
        self._docked = False             # the child's window died with it
        self.session = None
        self.viewport.detach()
        if msg.get("expected") or not msg.get("code"):
            self._to_idle()
            return
        self.state = "crashed"
        tail = (msg.get("stderr") or "").strip()
        code = msg.get("code")
        # its stderr tail is the child's own chatter ("slide offset: ..."),
        # which explains nothing to a researcher: it goes to the Log below.
        # A Windows exception status is named in hex, not as a 10-digit
        # decimal, and is almost always memory on a large print
        crashed = isinstance(code, int) and (code < 0 or code > 0x7FFFFFFF)
        why = ("it crashed (Windows status 0x%08X), most often from running "
               "out of memory on a large print" % (code & 0xFFFFFFFF)
               if crashed else "exit code %s" % code)
        if self.role == "unified":
            self.viewport.say(
                f"The simulation stopped unexpectedly: {why}.\n\n"
                "Try a lighter Display quality, such as Lines (instant), "
                "then press Open simulation. The Log page has the details. "
                "The rest of the app is unaffected.")
        else:
            self.viewport.say(
                f"The live view stopped unexpectedly: {why}.\n\n"
                "The Log page has the details. The rest of the app is "
                "unaffected.")
        self._bar_status.set("stopped unexpectedly")
        self.fs_btn.configure(state="disabled")
        self.stop_btn.configure(state="disabled")
        if self._no_rail:
            self.transport.grid_forget()
        else:
            self._set_running(False)
        # the full tail belongs in the Log page for a real diagnosis
        if tail and hasattr(self.app, "append_log"):
            self.app.append_log(f"\n[embedded {self.role} viewer exited "
                                f"{msg.get('code')}]\n{tail}\n")

    # -- the control rail ----------------------------------------------------------#
    def _clear(self, parent):
        for w in parent.winfo_children():
            w.destroy()

    def _build_rail(self, rail, hello):
        """Fill the control rail from the hello spec: a title row with the play
        state, the readout, then the controls as TABS over one fixed-height card.

        Every tab's pane sits in the SAME grid cell and the shown one is raised,
        so the card is always as tall as the tallest tab: switching tabs never
        moves anything below or beside it. Buttons that carry a `role` are drawn
        elsewhere (the transport bar, the readout's steppers) and skipped here;
        buttons that carry a `toggle` are drawn as switches."""
        self._clear(rail)
        self._hud_max_lines = 0               # readout height re-measures per viewer
        self._label_vars = {}
        self._value_scales = {}
        self._value_entries = {}
        self._ctl_last = {}
        self._toggles = {}
        self._tab_panes = {}
        self._cards = self._foot_cards[:]
        self._card_paths = {str(w) for pair in self._cards for w in pair}
        self._readout = None
        self._readout_vals = {}
        self._listing = None
        self._rule_frames = []
        self._rail_inputs = []
        self._title_lbl = None
        self._rail_sig = _rail_signature(hello)
        self._role_ids = {c["role"]: c["id"]
                          for c in hello.get("controls", []) if c.get("role")}
        self._card_styles()
        # scrollable: a short panel (or the micrograph tab's sliders) can exceed
        # the rail's height. VScrollFrame shows a scrollbar only while the content
        # overflows.
        scroller = VScrollFrame(rail, padding=(10, 8, 8, 8),
                                style="Sidebar.TFrame")
        scroller.pack(fill="both", expand=True)
        scroller.body.configure(style="Sidebar.TFrame")
        self._rail_scroller = scroller
        scroller.body.bind("<Configure>", self._place_overlays, add="+")
        self._retheme_rail_canvas()          # sidebar-tone the scroll canvas
        body = scroller.body
        body.columnconfigure(0, weight=1)
        r = 0
        GAP = gui_fonts.SP["s"]

        # title row: the viewer's name + a Paused/Playing chip (the file name is
        # already in the foot card, so the subtitle is not repeated here)
        head = ttk.Frame(body, style="Sidebar.TFrame")
        head.grid(row=r, column=0, sticky="ew", pady=(0, GAP))
        head.columnconfigure(0, weight=1)
        r += 1
        title = (hello.get("title") or "").strip()
        self._title_lbl = ttk.Label(head, text=title, style="Wordmark.TLabel")
        self._title_lbl.grid(row=0, column=0, sticky="w")
        # a fixed width (the longer word) and centred text: Paused <-> Playing
        # changes the words and the colours, never the chip's size
        self._state_tag = tk.Label(head, text="Paused", font="MewHint", padx=7,
                                   pady=1, bd=0, highlightthickness=1,
                                   width=len("Playing"), anchor="center")
        self._state_tag.grid(row=0, column=1, sticky="e", padx=(8, 0))
        if hello.get("has_transport") and self.session is not None:
            self._sync_state_tag(self._last_state or {"playing": False})
        else:
            # nothing loaded: no Paused chip, which would say a run is sitting
            # stopped when there is none (the viewport says what to do instead)
            self._state_tag.grid_remove()
        # the readout: a labelled table when the child sends rows, else the
        # HUD text block (a viewer that only has text keeps working). The table
        # is drawn now, from _READOUT_ROWS, not when the first readout lands.
        if hello.get("has_rows"):
            card, inner = self._card(body)
            card.grid(row=r, column=0, sticky="ew", pady=(0, GAP))
            r += 1
            table = ttk.Frame(inner, style="ViewerCard.TFrame")
            table.grid(row=0, column=0, sticky="nsew")
            table.columnconfigure(1, weight=1)
            status = tk.Label(inner, text="", font="MewBody",
                              anchor="nw", justify="left",
                              wraplength=RAIL_W - 40)
            status.grid(row=0, column=0, sticky="nsew")
            self._readout = {"card": card, "inner": inner, "table": table,
                             "status": status}
            knobs = viewer_sidebar_spec.readout_knobs(hello.get("controls", []))
            self._build_readout_table(table, list(_READOUT_ROWS), knobs)
            table.tkraise()
            self._readout["top"] = "table"
            self._paint_cards()
        elif hello.get("has_readout", True):
            ttk.Label(body, text="READOUT", style="SidebarSection.TLabel").grid(
                row=r, column=0, sticky="w")
            r += 1
            hud = ttk.Label(body, textvariable=self._hud_var, font="MewBody",
                            justify="left")
            hud.grid(row=r, column=0, sticky="w", pady=(2, 4))
            self._style_rail_label(hud)
            r += 1

        # knob roles are drawn beside the readout rows only when there IS a
        # table to draw them in; otherwise they stay ordinary tab buttons
        tabs = viewer_sidebar_spec.rail_tabs(hello.get("controls", []),
                                             bool(hello.get("has_rows")))

        if tabs:
            keys = list(tabs)
            if self._rail_tab.get() not in keys:
                self._rail_tab.set(keys[0])
            if len(keys) > 1:
                bar = gui_widgets.SegmentedBar(
                    body, options=[(k, k) for k in keys],
                    variable=self._rail_tab, command=self._show_tab)
                for lb in bar._segs.values():
                    lb.configure(padx=6)
                bar.grid(row=r, column=0, sticky="ew", pady=(0, 4))
                r += 1
            card, inner = self._card(body)
            card.grid(row=r, column=0, sticky="ew")
            r += 1
            for key in keys:
                pane = ttk.Frame(inner, style="ViewerCard.TFrame")
                pane.grid(row=0, column=0, sticky="nsew")
                pane.columnconfigure(0, weight=1)
                self._tab_panes[key] = pane
                # The panes are stacked in one cell and switched with tkraise,
                # so nothing about a shut one says it is shut. Tell the Ctrl+F
                # finder how to open this one; without it the finder indexes
                # these controls with an empty reveal chain and a jump lands on
                # the page with the wrong tab still in front (gui_finder._walk).
                pane._finder_reveal = (lambda k=key: self._reveal_rail_tab(k))
                pr = 0
                for spec in tabs[key]:
                    ctype = spec.get("type", "button")
                    if ctype == "slider":
                        pr = self._build_rail_slider(pane, pr, spec)
                    elif ctype == "number":
                        pr = self._build_rail_number(pane, pr, spec)
                    elif spec.get("toggle"):
                        pr = self._build_rail_toggle(pane, pr, spec)
                    else:
                        pr = self._build_rail_button(pane, pr, spec)
            self._paint_cards()
            self._show_tab()

        # measured when Tk is idle, not here: update_idletasks mid-callback
        # runs the whole app's pending layout (~1 s), stalling the hello
        self.after_idle(lambda h=hello: self._fit_rail_width(h))
        self._set_running(self.session is not None)

    def _build_rail_toggle(self, pane, r, spec):
        """One switch row: the name left, the switch right. Clicking the name
        toggles too, so the whole row is the target."""
        cid = spec["id"]
        row = ttk.Frame(pane, style="ViewerCard.TFrame")
        row.grid(row=r, column=0, sticky="ew", pady=2)
        row.columnconfigure(0, weight=1)
        press = lambda cid=cid: self._send({"t": "btn", "id": cid})
        name = ttk.Label(row, text=spec["toggle"], style="ViewerCard.TLabel",
                         cursor="hand2")
        name.grid(row=0, column=0, sticky="w")
        sw = gui_widgets.ToggleSwitch(row, on=spec.get("on", False),
                                      command=press)
        sw.grid(row=0, column=1, sticky="e")
        name.bind("<Button-1>", lambda _e, sw=sw: sw.invoke())
        self._toggles[cid] = sw
        self._rail_inputs.append(sw)
        return r + 1

    def _build_rail_button(self, pane, r, spec):
        var = self._label_vars.get(spec["id"])
        if var is None:
            var = tk.StringVar(value=spec.get("label", ""))
            self._label_vars[spec["id"]] = var
        row = ttk.Frame(pane, style="ViewerCard.TFrame")
        row.grid(row=r, column=0, sticky="ew", pady=(4, 2))
        row.columnconfigure(1, weight=1)
        sw = spec.get("swatch")
        if sw:
            chip = tk.Frame(row, width=8, height=8,
                            background="#%02x%02x%02x" % tuple(
                                int(round(c * 255)) for c in sw[:3]))
            chip.grid(row=0, column=0, padx=(0, 6))
        btn = ttk.Button(row, textvariable=var, style="ViewerBtn.TButton",
                         command=lambda cid=spec["id"]:
                         self._send({"t": "btn", "id": cid}))
        btn.grid(row=0, column=1, sticky="ew")
        self._rail_inputs.append(btn)
        return r + 1

    def _fill_rail(self, hello):
        """A hello whose layout matches the built rail: bring the title, the
        switch positions and the button labels up to date without rebuilding."""
        try:
            if self._title_lbl is not None:
                self._title_lbl.configure(text=(hello.get("title") or "").strip())
        except tk.TclError:
            pass
        for spec in hello.get("controls", []):
            cid = spec.get("id")
            sw = self._toggles.get(cid)
            if sw is not None and "on" in spec:
                sw.set(spec["on"])
            var = self._label_vars.get(cid)
            if var is not None:
                var.set(spec.get("label", ""))
        self._apply_values({c["id"]: c["value"]
                            for c in hello.get("controls", [])
                            if c.get("type") in ("slider", "number")
                            and "value" in c})

    def _set_running(self, running):
        """Idle and running are one layout. Idle greys every control, empties
        the readout, resets the transport and hides the Paused chip; a viewer's hello
        enables them and its streams fill them."""
        state = "normal" if running else "disabled"
        for w in self._rail_inputs:
            try:
                if isinstance(w, gui_widgets.ToggleSwitch):
                    w.set_enabled(running)
                    if not running:
                        w.set(False)
                elif isinstance(w, ttk.Scale):
                    w.state(["!disabled"] if running else ["disabled"])
                else:
                    w.configure(state=state)
            except tk.TclError:
                pass
        for btn in self._tbtn.values():
            try:
                btn.configure(state=state)
            except tk.TclError:
                pass
        for scale in self._scales:
            try:
                scale.state(["!disabled"] if running else ["disabled"])
            except tk.TclError:
                pass
        # the page's own tools that need an open viewer (the micrograph
        # buttons) follow the same idle/running switch
        hook = getattr(self.app, "_viewer_running_changed", None)
        if callable(hook):
            try:
                hook(self.role, running)
            except Exception:  # noqa: BLE001 -- a page hook never breaks the panel
                pass
        if running:
            return
        self._last_state = None
        self._play_expect = None
        self._set_readout([], "")
        if self._state_tag is not None and _widget_alive(self._state_tag):
            self._state_tag.grid_remove()       # idle: nothing to call Paused
        self._set_play_label("Play")
        self._time_var.set(f"{_fmt_time(0.0)} / {_fmt_time(0.0)}")
        self._speed_var.set("")
        self._scrub_guard = True
        try:
            for scale in self._scales:
                scale.set(0.0)
        finally:
            self._scrub_guard = False

    def _reveal_rail_tab(self, key):
        """Bring one rail tab to the front, by name. The Ctrl+F finder's reveal
        step for every control on that tab."""
        if key in self._tab_panes:
            self._rail_tab.set(key)
            self._show_tab()

    def _show_tab(self):
        pane = self._tab_panes.get(self._rail_tab.get())
        if pane is not None:
            try:
                pane.tkraise()
            except tk.TclError:
                pass

    # -- the readout table ----------------------------------------------------- #
    def _set_readout(self, rows, status):
        """Fill the readout card's values. The table's rows are fixed at rail
        build (_READOUT_ROWS), so a tick only changes text: a row the child
        did not send reads _EMPTY_VALUE, and a row the table does not have is
        ignored. A transient status ("Computing deviation…") covers the table
        in the same grid cell, so the card keeps its height."""
        ro = self._readout
        if ro is None or not _widget_alive(ro["table"]):
            return
        got = {(row.get("g", ""), row.get("k", "")): row for row in rows}
        # the move is named as the saved and sent file spells it, not as the
        # scratch copy the viewer plays ("G0" there, "G1" in the file)
        spell = getattr(self.app, "_readout_move_as_saved", None)
        for key, var in self._readout_vals.items():
            row = got.get(key)
            text = row.get("v", "") if row is not None else _EMPTY_VALUE
            if key[1] == "Move" and row is not None and callable(spell):
                text = spell(text)
            if var.get() != text:
                var.set(text)
        # This runs on every readout of a playing clock, so it raises only on a
        # change: a repeated raise reads to gui_curtain as a layout still in
        # progress, and the cover stayed up over the picture for its whole 4 s.
        try:
            if status:
                ro["status"].configure(text=status)
            top = "status" if status else "table"
            if ro.get("top") != top:
                ro["top"] = top
                ro[top].tkraise()
        except tk.TclError:
            pass

    def _build_readout_table(self, table, rows, knobs):
        self._clear(table)
        self._readout_vals = {}
        self._rule_frames = []
        r = 0
        group = None
        reset = self._role_ids.get("knob:reset")
        last_knob_row = None
        listing_rows = []       # the Line / Move rows the listing stands in for
        for row in rows:
            g = row.get("g", "")
            if g != group:
                if group is not None:
                    tk.Frame(table, height=1, bg=gui_widgets._PAL["hairline"]
                             ).grid(row=r, column=0, columnspan=2, sticky="ew",
                                    pady=5)
                    self._rule_frames.append(table.grid_slaves(row=r)[0])
                    r += 1
                if g:
                    ttk.Label(table, text=g.upper(),
                              style="ViewerCardGroup.TLabel").grid(
                        row=r, column=0, columnspan=2, sticky="w", pady=(0, 2))
                    r += 1
                group = g
            key = (g, row.get("k", ""))
            klabel = ttk.Label(table, text=row.get("k", ""),
                               style="ViewerCardKey.TLabel")
            klabel.grid(row=r, column=0, sticky="w", padx=(0, 10), pady=1)
            var = tk.StringVar(value=_EMPTY_VALUE)
            self._readout_vals[key] = var
            knob = row.get("knob") if row.get("knob") in knobs else None
            if knob:
                cell = ttk.Frame(table, style="ViewerCard.TFrame")
                cell.grid(row=r, column=1, sticky="ew", pady=1)
                cell.columnconfigure(1, weight=1)
                bm = ttk.Button(cell, text="−", width=2,
                                style="ViewerStep.TButton",
                                command=lambda c=f"knob:{knob}:-":
                                self._press_role(c))
                bm.grid(row=0, column=0)
                ttk.Label(cell, textvariable=var, anchor="center",
                          style="ViewerCard.TLabel").grid(
                    row=0, column=1, sticky="ew", padx=4)
                bp = ttk.Button(cell, text="+", width=2,
                                style="ViewerStep.TButton",
                                command=lambda c=f"knob:{knob}:+":
                                self._press_role(c))
                bp.grid(row=0, column=2)
                self._rail_inputs += [bm, bp]
                last_knob_row = r
            else:
                vlabel = ttk.Label(table, textvariable=var,
                                   style="ViewerCard.TLabel")
                vlabel.grid(row=r, column=1, sticky="w", pady=1)
                if key in _LISTING_REPLACES:
                    listing_rows.append((r, klabel, vlabel))
            r += 1
            if (last_knob_row == r - 1 and reset is not None
                    and not any(x.get("knob") in knobs and x.get("g") == g
                                for x in rows[rows.index(row) + 1:])):
                rb = ttk.Button(table, text="Reset", style="ViewerBtn.TButton",
                                command=lambda: self._press_role("knob:reset"))
                rb.grid(row=r, column=1, sticky="e", pady=(3, 0))
                self._rail_inputs.append(rb)
                r += 1
        self._build_listing(table, listing_rows)

    def _build_listing(self, table, listing_rows):
        """The g-code listing, built hidden in the place of the Line and Move
        rows (it shows when the viewer's "G-code listing" switch is on)."""
        self._listing = None
        if not listing_rows:
            return
        try:
            font = tkfont.nametofont("MewMono")
        except tk.TclError:
            font = tkfont.nametofont("TkFixedFont")
        text = tk.Text(table, width=_LISTING_COLS, height=_LISTING_ROWS,
                       wrap="none", font=font, bd=0, highlightthickness=0,
                       padx=4, pady=2, cursor="arrow", takefocus=0,
                       state="disabled")
        self._listing = {"text": text, "row": listing_rows[0][0],
                         "span": len(listing_rows),
                         "rows": [(k, v) for _r, k, v in listing_rows]}
        self._paint_listing()
        self._set_listing(self._listing_last)

    def _paint_listing(self):
        lst = self._listing
        if lst is None or not _widget_alive(lst["text"]):
            return
        pal = gui_widgets._PAL
        lst["text"].configure(bg=pal.get("card", "#ffffff"), fg=pal["fg"])
        lst["text"].tag_configure("num", foreground=pal["hint"])
        lst["text"].tag_configure("cur", background=pal["select_bg"],
                                  foreground=pal["select_fg"])

    def _set_listing(self, data):
        """Show the listing (`data` = {"cur", "lines"}) in place of the Line
        and Move rows, or None: take it away and give the rows back."""
        self._listing_last = data
        lst = self._listing
        if lst is None or not _widget_alive(lst["text"]):
            return
        text = lst["text"]
        try:
            if not data or not data.get("lines"):
                if text.winfo_manager():
                    text.grid_remove()
                    for k, v in lst["rows"]:
                        k.grid()
                        v.grid()
                return
            if not text.winfo_manager():
                for k, v in lst["rows"]:
                    k.grid_remove()
                    v.grid_remove()
                text.grid(row=lst["row"], column=0, columnspan=2,
                          rowspan=lst["span"], sticky="ew", pady=1)
            lines = data["lines"]
            wide = len(str(lines[-1][0]))
            text.configure(state="normal")
            text.delete("1.0", "end")
            for i, (n, body) in enumerate(lines):
                now = n == data["cur"]
                num = str(n).rjust(wide) + "  "
                room = max(_LISTING_COLS - len(num), 4)
                body = body if len(body) <= room else body[:room - 1] + "…"
                text.insert("end", num, ("cur" if now else "num",))
                text.insert("end", body.ljust(room) if now else body,
                            ("cur",) if now else ())
                if i < len(lines) - 1:
                    text.insert("end", "\n")
            text.configure(state="disabled")
        except tk.TclError:
            pass

    def _sync_state_tag(self, msg):
        tag = self._state_tag
        if tag is None or not _widget_alive(tag) or "playing" not in msg:
            return
        pal = gui_widgets._PAL
        playing = bool(msg.get("playing"))
        try:
            if self.session is None:
                # nothing loaded: no chip at all, from any caller (a retheme
                # re-syncs it too), since "Paused" would say a run is sitting
                # stopped when there is none
                tag.grid_remove()
                return
            if playing:
                tag.configure(text="Playing", bg=pal["select_bg"],
                              fg=pal["select_fg"],
                              highlightbackground=pal["select_bg"])
            else:
                ok = pal.get("st_ok", pal["fg"])
                tag.configure(text="Paused", bg=pal.get("card", "#ffffff"),
                              fg=ok, highlightbackground=ok)
            if not tag.winfo_manager():      # per state message: see _set_readout
                tag.grid()
        except tk.TclError:
            pass

    # -- cards ------------------------------------------------------------------ #
    def _card(self, parent):
        """A rail card: a hairline-ringed surface in the palette's card tone.
        Returns (card, inner) -- build into `inner`, grid `card`."""
        card = tk.Frame(parent, bd=0, highlightthickness=1)
        inner = tk.Frame(card, bd=0, highlightthickness=0)
        inner.pack(fill="both", expand=True, padx=10, pady=8)
        inner.columnconfigure(0, weight=1)
        inner.rowconfigure(0, weight=1)
        self._cards.append((card, inner))
        self._card_paths.update((str(card), str(inner)))
        return card, inner

    def _in_card(self, w):
        path = str(w)
        return any(path == c or path.startswith(c + ".")
                   for c in self._card_paths)

    def _card_styles(self):
        """Configure the rail's ttk styles for the current palette. Only when
        the palette changed: every ttk.Style().configure restyles EVERY ttk
        widget in the app, which made a rail rebuild take seconds."""
        pal = gui_widgets._PAL
        card = pal.get("card", "#ffffff")
        key = (card, pal["fg"], pal["hint"], pal["hairline"])
        if ViewerPanel._styled_for == key:
            return
        ViewerPanel._styled_for = key
        st = ttk.Style()
        st.configure("ViewerCard.TFrame", background=card)
        st.configure("ViewerCard.TLabel", background=card, foreground=pal["fg"],
                     font="MewBody")
        st.configure("ViewerCardKey.TLabel", background=card,
                     foreground=pal["hint"], font="MewBody")
        st.configure("ViewerCardGroup.TLabel", background=card,
                     foreground=pal["hint"], font="MewNavSection")
        # the readout's -/+ steppers: ViewerBtn's look, with tight padding so a
        # value fits between them in the rail's width
        opts = ("font", "anchor", "background", "foreground", "borderwidth",
                "relief", "bordercolor", "lightcolor", "darkcolor", "focuscolor")
        base = {k: st.lookup("ViewerBtn.TButton", k) for k in opts}
        st.configure("ViewerStep.TButton", padding=(4, 1),
                     **{k: v for k, v in base.items() if v not in ("", None)})
        for opt in ("background", "bordercolor", "foreground"):
            spec = st.map("ViewerBtn.TButton", query_opt=opt)
            if spec:
                st.map("ViewerStep.TButton", **{opt: spec})

    def _paint_cards(self):
        pal = gui_widgets._PAL
        card_bg = pal.get("card", "#ffffff")
        for card, inner in self._cards:
            try:
                if not _widget_alive(card):
                    continue
                card.configure(bg=card_bg, highlightbackground=pal["hairline"],
                               highlightcolor=pal["hairline"])
                inner.configure(bg=card_bg)
            except tk.TclError:
                pass
        ro = self._readout
        if ro is not None and _widget_alive(ro["status"]):
            ro["status"].configure(bg=card_bg, fg=pal["hint"])
        for f in self._rule_frames:
            try:
                f.configure(bg=pal["hairline"])
            except tk.TclError:
                pass
        self._paint_listing()
        self._sync_state_tag(self._last_state or {"playing": False})

    # -- the Controls legend popup ------------------------------------------------ #
    def _toggle_legend(self):
        if self._legend_pop is not None:
            self._close_legend()
            return
        if not self._legend:
            return
        pal = gui_widgets._PAL
        top = tk.Toplevel(self)
        top.withdraw()
        top.overrideredirect(True)
        top.transient(self.winfo_toplevel())
        frame = tk.Frame(top, bg=pal.get("card", "#ffffff"), bd=0,
                         highlightthickness=1,
                         highlightbackground=pal["hairline"])
        frame.pack(fill="both", expand=True)
        r = 0
        for section, rows in self._legend:
            if section:
                tk.Label(frame, text=section.upper(), font="MewNavSection",
                         bg=pal.get("card"), fg=pal["hint"], anchor="w").grid(
                    row=r, column=0, columnspan=2, sticky="w", padx=12,
                    pady=(10 if r == 0 else 8, 2))
                r += 1
            for key, action in rows:
                tk.Label(frame, text=key, font="MewBody", bg=pal.get("card"),
                         fg=pal["fg"], anchor="w").grid(
                    row=r, column=0, sticky="w", padx=(12, 14),
                    columnspan=1 if action else 2)
                if action:
                    tk.Label(frame, text=action, font="MewBody",
                             bg=pal.get("card"), fg=pal["hint"], anchor="w"
                             ).grid(row=r, column=1, sticky="w", padx=(0, 12))
                r += 1
        tk.Frame(frame, height=8, bg=pal.get("card")).grid(row=r, column=0)
        top.update_idletasks()
        btn = self.controls_btn
        x = btn.winfo_rootx() + btn.winfo_width() - top.winfo_reqwidth()
        y = btn.winfo_rooty() + btn.winfo_height() + 4
        if self._foot is not None:           # in the foot card: beside the rail
            x = self.rail.winfo_rootx() + self.rail.winfo_width() + 4
            y = btn.winfo_rooty() + btn.winfo_height() - top.winfo_reqheight()
        top.geometry(f"+{max(0, x)}+{y}")
        top.deiconify()
        top.bind("<Escape>", lambda _e: self._close_legend())
        top.bind("<FocusOut>", lambda _e: self.after(120, self._close_legend))
        top.focus_set()
        self._legend_pop = top

    def _close_legend(self):
        top, self._legend_pop = self._legend_pop, None
        if top is not None:
            try:
                top.destroy()
            except tk.TclError:
                pass

    def _fit_rail_width(self, hello):
        """Size the rail WIDTH to its content in the LIVE font, floored at
        RAIL_W, so a display-scaled machine (bigger ttk fonts) never clips a
        label. Measured with every readout row holding its widest expected
        value (_READOUT_ROWS' `sample`), so the width is final before any
        viewer runs. It only ever GROWS: a readout tick, a hello or a close
        never narrows it, so the viewport beside it never jumps."""
        body = self._rail_scroller.body
        if not _widget_alive(body):
            return
        saved = {k: v.get() for k, v in self._readout_vals.items()}
        samples = {(r["g"], r["k"]): r["sample"] for r in _READOUT_ROWS}
        for key, var in self._readout_vals.items():
            var.set(samples.get(key, var.get()))
        body.update_idletasks()
        need = body.winfo_reqwidth()
        for key, var in self._readout_vals.items():
            var.set(saved[key])
        if hello.get("has_readout", True) and not hello.get("has_rows"):
            # the text readout is empty at build time: size for its widest line
            need = max(need, tkfont.nametofont("MewBody").measure(
                "v(act/set) 000.0/000.0 mm/s x00.00"))
        # + the VScrollFrame inset (10+8) + the auto-hiding scrollbar gutter + slack,
        # floored at RAIL_W and capped at 2x so a pathological label can't eat
        # the viewport -- past the cap it clips.
        want = max(RAIL_W, min(need + 44, 2 * RAIL_W))
        if self._foot is not None:
            want = max(want, self._foot.winfo_reqwidth())
        try:
            have = int(self.rail.cget("width"))
        except (tk.TclError, ValueError):
            have = 0
        if want > have:
            self.rail.configure(width=want)
            self._place_overlays()

    def _build_rail_slider(self, body, r, spec):
        """A value line (label + a type-in entry) over a ttk.Scale. Dragging the
        scale sends a throttled {"t":"ctl"} (release flushes the final value);
        typing an exact value into the entry + Return/focus-out clamps it to
        [min,max], moves the scale, and sends it too -- so a KNOWN target needs
        no fine-scrubbing. A typed edit updates the child's state immediately, so
        the native-fullscreen slider (which reads that same state) shows it on
        maximize -- no typing needed there. Both widgets re-sync when the child
        pushes a "values" update (Reset / scale-from-2D / a fullscreen drag)."""
        cid = spec["id"]
        lo = float(spec.get("min", 0.0))
        hi = float(spec.get("max", 1.0))
        val = float(spec.get("value", lo))
        fmt = spec.get("fmt", "%.3g")
        label = spec.get("label", "")
        frame = ttk.Frame(body, style=self._surface_style(body))
        frame.grid(row=r, column=0, sticky="ew", pady=(2, 1))
        frame.columnconfigure(0, weight=1)
        lbl = ttk.Label(frame, text=label, style="Hint.TLabel")
        lbl.grid(row=0, column=0, sticky="w")
        self._style_rail_label(lbl)
        entvar = tk.StringVar(value=fmt % val)
        ent = ttk.Entry(frame, textvariable=entvar, width=7, justify="right")
        ent.grid(row=0, column=1, sticky="e")
        scale = ttk.Scale(frame, from_=lo, to=hi, orient="horizontal")
        scale.set(val)
        scale.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(1, 0))

        def _on_scale(v, cid=cid, fmt=fmt):
            if self._value_guard:
                return
            try:
                fv = float(v)
            except (ValueError, tk.TclError):
                return
            try:                               # don't stomp a value being typed
                if ent.focus_get() is not ent:
                    entvar.set(fmt % fv)
            except (KeyError, tk.TclError):
                entvar.set(fmt % fv)
            now = time.perf_counter()
            if now - self._ctl_last.get(cid, 0.0) < _SCRUB_MIN_DT:
                return
            self._ctl_last[cid] = now
            self._send({"t": "ctl", "id": cid, "value": fv})

        def _flush(_e=None, cid=cid):
            try:
                self._send({"t": "ctl", "id": cid, "value": float(scale.get())})
            except (ValueError, tk.TclError):
                pass

        def _commit_entry(_e=None, cid=cid, lo=lo, hi=hi, fmt=fmt):
            try:
                fv = float(entvar.get())
            except (ValueError, tk.TclError):
                entvar.set(fmt % float(scale.get()))    # bad text -> snap back
                return
            fv = max(lo, min(hi, fv))
            entvar.set(fmt % fv)
            self._value_guard = True                     # move knob, no echo
            try:
                scale.set(fv)
            except tk.TclError:
                pass
            finally:
                self._value_guard = False
            self._send({"t": "ctl", "id": cid, "value": fv})

        scale.configure(command=_on_scale)
        scale.bind("<ButtonRelease-1>", _flush)
        ent.bind("<Return>", _commit_entry)
        ent.bind("<FocusOut>", _commit_entry)
        self._value_scales[cid] = (scale, entvar, ent, spec)
        self._rail_inputs += [scale, ent]
        return r + 1

    def _build_rail_number(self, body, r, spec):
        """A label + ttk.Entry for exact typing; commits on Return / focus-out.

        Like the slider's entry, the commit path PARSES the text as a float,
        CLAMPS it to [min, max] when the spec bounds it, reformats the entry to
        the clamped value, and REVERTS to the last good value on garbage --
        rather than shipping the raw string straight to the child. (The child's
        on_set does accept text, but a public chrome control should validate at
        the rail like the slider does; no in-tree caller relied on the raw
        pass-through.)"""
        cid = spec["id"]
        fmt = spec.get("fmt", "%.4g")
        val = float(spec.get("value", 0.0))
        lo = spec.get("min")
        hi = spec.get("max")
        frame = ttk.Frame(body, style=self._surface_style(body))
        frame.grid(row=r, column=0, sticky="ew", pady=1)
        frame.columnconfigure(1, weight=1)
        lbl = ttk.Label(frame, text=spec.get("label", ""), style="Hint.TLabel")
        lbl.grid(row=0, column=0, sticky="w", padx=(0, 6))
        self._style_rail_label(lbl)
        var = tk.StringVar(value=fmt % val)
        ent = ttk.Entry(frame, textvariable=var, width=8)
        ent.grid(row=0, column=1, sticky="ew")
        # last committed value, so bad text reverts to it (not to the seed)
        self._value_entries[cid] = (var, ent, spec)

        def _commit(_e=None, cid=cid, lo=lo, hi=hi, fmt=fmt):
            try:
                fv = float(var.get())
            except (ValueError, tk.TclError):
                var.set(fmt % float(spec.get("value", 0.0)))   # revert garbage
                return
            if lo is not None:
                fv = max(float(lo), fv)
            if hi is not None:
                fv = min(float(hi), fv)
            var.set(fmt % fv)                                  # echo the clamp
            spec["value"] = fv                                 # remember good val
            self._send({"t": "ctl", "id": cid, "value": fv})

        ent.bind("<Return>", _commit)
        ent.bind("<FocusOut>", _commit)
        self._rail_inputs.append(ent)
        return r + 1

    def _apply_values(self, values):
        """Push child-side value changes (Reset / scale-from-2D / a fullscreen
        drag) into the rail widgets without echoing them back as fresh edits."""
        self._value_guard = True
        try:
            for cid, v in values.items():
                try:
                    fv = float(v)
                except (ValueError, TypeError):
                    continue
                if cid in self._value_scales:
                    scale, entvar, ent, spec = self._value_scales[cid]
                    try:
                        scale.set(fv)
                    except tk.TclError:
                        pass
                    try:                       # don't stomp an active edit
                        typing = ent.focus_get() is ent
                    except (KeyError, tk.TclError):
                        typing = False
                    if not typing:
                        entvar.set(spec.get("fmt", "%.3g") % fv)
                elif cid in self._value_entries:
                    var, ent, spec = self._value_entries[cid]
                    try:
                        if ent.focus_get() is ent:
                            continue
                    except (KeyError, tk.TclError):
                        pass
                    var.set(spec.get("fmt", "%.4g") % fv)
        finally:
            self._value_guard = False

    def _surface_style(self, parent):
        return "ViewerCard.TFrame" if self._in_card(parent) else "Sidebar.TFrame"

    def _style_rail_label(self, lbl):
        """Rail labels sit on the sidebar surface (or a card inside it); give
        them that background so they don't punch holes in the panel."""
        try:
            pal = gui_widgets._PAL
            tone = "card" if self._in_card(lbl) else "sidebar"
            lbl.configure(background=pal.get(tone, "#eef0f4"))
        except tk.TclError:
            pass

    def _retheme_rail_canvas(self):
        """Keep the scroll canvas the SIDEBAR tone. gui_theme's _recolor_classic
        paints every VScrollFrame canvas the PAGE bg (its usual home is a page
        body); the rail wants the sidebar colour so the strip below short content
        matches the panel. Runs after _recolor_classic in the theme pass."""
        sc = self._rail_scroller
        if sc is None or not _widget_alive(sc):
            return
        tone = gui_widgets._PAL.get("sidebar", "#eef0f4")
        try:
            sc._canvas.configure(background=tone, highlightbackground=tone)
        except (tk.TclError, AttributeError):
            pass

    # -- transport ---------------------------------------------------------------#
    def _sync_transport(self, msg):
        try:
            t = float(msg.get("time", 0.0))
            total = float(msg.get("total", self._total) or self._total)
        except (TypeError, ValueError):
            return
        self._total = total or self._total
        self._time_var.set(f"{_fmt_time(t)} / {_fmt_time(self._total)}")
        try:
            self._speed_var.set(f"x{float(msg['speed']):g}")
        except (KeyError, TypeError, ValueError):
            pass
        if not self._scrub_drag:
            self._scrub_guard = True
            try:
                for scale in self._scales:
                    scale.set(t)
            finally:
                self._scrub_guard = False

    def _on_scale_press(self, e=None):
        """A press on the timeline. On the track it jumps there: a ttk.Scale's
        own track click only steps by 1 (ttk/scale.tcl `Increment`), which here
        is one second of print, so a click looked like it did nothing. The
        jump lands the handle under the pointer before the class binding runs,
        so that binding sees the slider and a drag carries straight on. The
        G-code window's MoveScrub does the same."""
        self._scrub_drag = True
        w = getattr(e, "widget", None)
        if w is None:
            return
        try:
            if "slider" not in str(w.identify(e.x, e.y)):
                w.set(w.get(e.x, e.y))
                w.update_idletasks()
        except tk.TclError:
            pass

    def _on_scale(self, value):
        if self._scrub_guard or self.session is None:
            return
        now = time.perf_counter()
        if now - self._scrub_last < _SCRUB_MIN_DT:
            return
        self._scrub_last = now
        try:
            self._send({"t": "scrub", "time": float(value)})
        except ValueError:
            pass

    def _on_scale_release(self, scale):
        self._scrub_drag = False
        if self.session is not None:
            try:
                self._send({"t": "scrub", "time": float(scale.get())})
            except (ValueError, tk.TclError):
                pass

    # -- fullscreen (native, child-owned; see the class docstring) --------------#
    def toggle_fullscreen(self):
        """⛶: ask the CHILD to flip its render window to (or from) a native
        borderless fullscreen surface. State only changes when the child
        replies -- the button is always truthful."""
        if self.session is None or self._hello is None:
            return
        if self._fs_native:
            self._send({"t": "native_fs", "on": False})
        else:
            self._allow_child_foreground()
            r = self.app.root
            # `tab`: the fullscreen rail opens on the tab the rail is showing
            self._send({"t": "native_fs", "on": True,
                        "x": int(r.winfo_rootx()), "y": int(r.winfo_rooty()),
                        "w": int(r.winfo_screenwidth()),
                        "h": int(r.winfo_screenheight()),
                        "tab": self._rail_tab.get()})

    def _allow_child_foreground(self):
        """Grant the child the right to bring its new window to the front
        (Windows only lets the foreground process delegate that)."""
        if not IS_WINDOWS or self.session is None:
            return
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(
                int(self.session.proc.pid))
        except Exception:  # noqa: BLE001
            pass

    def _maybe_dock(self):
        """After the first streamed frame (the scene is built), hand the child
        the viewport's HWND so its render window draws IN the panel, GPU-direct
        (viewer_embed.EmbedHost._dock). Streaming caps a maximised panel near
        20 fps on the Tk paste alone. Once per session; the frame just blitted
        stays underneath, so nothing flashes. MEWRACLE_EMBED_BLIT=1 opts out."""
        if (self._dock_asked or self.session is None or not IS_WINDOWS
                or not (self._hello or {}).get("dock")
                or os.environ.get("MEWRACLE_EMBED_BLIT") == "1"):
            return
        self._dock_asked = True
        try:
            vp = self.viewport
            self._send({"t": "dock", "hwnd": int(vp.winfo_id()),
                        "w": max(vp.winfo_width(), 32),
                        "h": max(vp.winfo_height(), 32)})
        except tk.TclError:
            pass

    def _set_fs_native(self, on):
        """Reflect the child's replied fullscreen state in the panel. The
        fullscreen TRANSPORT is the child's own (drawn inside the native
        window by its ViewerChrome -- see gcode_player._mount_fs_transport):
        a GUI-side topmost strip was tried and is fundamentally unreliable,
        because DWM hands a monitor-covering foreground window the display
        (independent flip) and stops composing other processes' windows over
        it. Nothing to open or close here."""
        on = bool(on)
        if on == self._fs_native:
            return
        self._fs_native = on
        try:
            if on:
                self.fs_btn.configure(text="⛶  Exit fullscreen")
                self.viewport.say("Shown in fullscreen.\nEsc or ⛶ returns.")
            else:
                self.fs_btn.configure(text="⛶  Fullscreen")
                if self.session is not None:
                    # resume streaming at the panel's own size
                    self.viewport.say("")
                    self.viewport._send_resize()
                    self.viewport.send_visible()
        except tk.TclError:
            pass

    def set_perspective(self, on):
        """The Settings window's Perspective switch: the viewer on show
        follows it live (a viewer opened later reads it from gui_prefs)."""
        self._send({"t": "projection", "perspective": bool(on)})

    def set_origin_visible(self, on):
        """The Settings window's "Show G-code zero", live."""
        self._send({"t": "origin", "on": bool(on)})

    # -- theming (called by gui_widgets.set_palette via the registry) -----------#
    def retheme(self):
        dark = self._is_dark()
        pal = gui_widgets._PAL
        self.viewport.retheme(dark, pal)
        self._send({"t": "theme", "dark": dark})
        # rail label backgrounds follow the sidebar surface; a theme pass
        # restyles even when the palette key matches (ViewerBtn may have changed)
        ViewerPanel._styled_for = None
        self._card_styles()
        for w in self.rail.winfo_children():
            self._retheme_labels(w)
        self._paint_cards()
        self._retheme_rail_canvas()
        self._make_play_icons()
        self._set_play_label("Pause" if self._play_showing else "Play")

    def _retheme_labels(self, w):
        for c in w.winfo_children():
            self._retheme_labels(c)
        if isinstance(w, ttk.Label) and not str(
                w.cget("style")).startswith("ViewerCard"):
            self._style_rail_label(w)


# ---------------------------------------------------------------------------
# the ModernApp mixin
# ---------------------------------------------------------------------------
class _EmbedMixin:
    """Reroutes the viewer launches into the embedded panels and manages their
    lifecycle. Sits before _ShellMixin's bases in the ModernApp MRO so its
    _spawn_player / _launch_stl_viewer overrides beat gui_viewers' Popen."""

    # panels are created by gui_shell's page builders via _make_embed_panel
    def _make_embed_panel(self, parent, role, empty_text, relauncher, no_rail=False,
                          rail_tools=False):
        panel = ViewerPanel(parent, self, role, empty_text, relauncher,
                            no_rail=no_rail, rail_tools=rail_tools)
        if not hasattr(self, "_embed_panels"):
            self._embed_panels = {}
        self._embed_panels[role] = panel
        return panel

    def _embed_panel(self, role):
        return getattr(self, "_embed_panels", {}).get(role)

    # -- launch reroutes ------------------------------------------------------ #
    # gui_viewers assembles the argv exactly as for the windowed launch and hands
    # it to this seam; here it routes into the panels instead of Popen-ing a
    # window. super() = the classic windowed fallback.
    # There is deliberately NO _spawn_stl_viewer / _spawn_deviation_viewer
    # override: the U2 merge folded STL and deviation into the unified Viewer as
    # in-scene modes, so either override could only take its panel-is-None branch
    # and fall through to the windowed launch anyway. Deviation now arrives via
    # panel.send_child(t="deviation") from gui_deviation, coloured by stl_viewer's
    # LUT helpers inside gcode_player.
    def _spawn_player(self, args, live=False, static=False):
        # the unified Viewer: a static launch rides its own "unified" panel on
        # the single "view" page; locked/live keep their existing panels/pages.
        if static:
            role, page_id = "unified", "view"
        else:
            role, page_id = ("live", "view_sim_live") if live else \
                            ("locked", "view_sim")
        panel = self._embed_panel(role)
        if panel is None:
            return super()._spawn_player(args, live, static=static)
        page = self._page_frames.get(page_id)
        if page is not None:
            self.nb.select(page)
        # stamp the panel with the result generation it is about to show, so
        # entering the Viewer page can tell a current panel from a stale one
        # (_on_shell_tab_changed). Set for every role: only "unified" is read,
        # but a role-specific stamp would go stale silently if that changed.
        panel._result_gen = getattr(self, "_result_gen", 0)
        panel.launch(args, subtitle=os.path.basename(self.last_gcode or ""))

    # -- child->parent request channel -------------------------------------#
    def _handle_viewer_request(self, panel, msg):
        """Dispatch a child->parent request from an embedded viewer -- a sidebar
        button that needs GUI-side work the VTK child structurally can't do (run
        the deviation analysis, open a Tk file dialog, build the exact mesh). Runs on
        the Tk thread (from the poll pump) so it can open dialogs and call app
        methods directly; heavy work goes to a worker/thread and the result is
        pushed back DOWN to the child (apply-in-place, via panel.send_child).
        Unknown actions are ignored. Feature handlers register in
        _viewer_request_handlers (extended by U2c deviation / U2d micrograph)."""
        action = msg.get("action")
        handler = self._viewer_request_handlers().get(action)
        if handler is None:
            return
        try:
            handler(panel, msg)
        except Exception as exc:  # noqa: BLE001
            try:
                self.append_log(f"[viewer request '{action}' failed] {exc}\n")
            except Exception:  # noqa: BLE001
                pass

    # -- micrograph overlay push -------------------------------------------#
    def _unified_import_image(self):
        """Viewer top-row 'Import image…': open the file dialog (GUI-side) then push
        the image path DOWN to the open unified viewer as a live layer."""
        if not hasattr(self, "_an_micro_load"):
            return
        self._an_micro_load()          # file dialog -> _an_micro["path"]/["image"]
        micro = getattr(self, "_an_micro", {}) or {}
        if micro.pop("_fresh", False):     # a file was picked and read
            if self._push_micrograph_overlay():
                self.status.set(
                    os.path.basename(micro["path"]) + " is laid under the "
                    "scaffold. Fade, move or rotate it on the rail's "
                    "Micrograph tab, and set its true size with Scale image…")
        sync = getattr(self, "_sync_overlay_buttons", None)
        if callable(sync):
            sync()

    def _unified_scale_image(self):
        """Viewer top-row 'Scale image…': pin a real mm/px on the image (GUI-side 2D
        window), then re-push so the overlay rescales in place."""
        if not hasattr(self, "_an_micro_scale_window"):
            return
        self._an_micro_scale_window()  # -> _an_micro["scale_mmpx"]
        self._push_micrograph_overlay()

    def _push_micrograph_overlay(self):
        panel = self._embed_panel("unified")
        micro = getattr(self, "_an_micro", {}) or {}
        path = micro.get("path")
        if not path:
            return False
        pushed = panel is not None and panel.send_child(
            t="micrograph", image=path, scale_mmpx=micro.get("scale_mmpx"))
        if not pushed:
            try:
                self.status.set("Nothing is open to lay the image over. "
                                "Generate a design, or press Open simulation, "
                                "then import the image again.")
            except Exception:  # noqa: BLE001
                pass
        return bool(pushed)

    def _viewer_request_handlers(self):
        """action -> handler(panel, msg) for embedded-viewer child->parent
        requests. Each feature contributes its handler if the mixin is present
        (deviation, micrograph), so a build without that engine simply
        never advertises the action."""
        h = {}
        if hasattr(self, "_req_viewer_deviation"):
            h["deviation"] = self._req_viewer_deviation
        if hasattr(self, "_player_args"):
            h["live_edit"] = self._req_viewer_live_edit
        return h

    def _req_viewer_live_edit(self, panel, msg):
        """Child->parent 'live_edit' request from the unified viewer: relaunch
        THIS panel with --live-adjust (real-time editing, from t=0) or back to the
        static default. A relaunch is unavoidable -- the live re-lay is a different
        deposition engine that cannot jump to the finished scaffold."""
        want_live = bool(msg.get("on", True))
        args = self._player_args(live=want_live, static=not want_live)
        if args is None:
            return
        page = self._page_frames.get("view")
        if page is not None:
            self.nb.select(page)
        # the same stamp _spawn_player sets: a live-edit relaunch is showing the
        # CURRENT result, so re-entering the page must not respawn it
        panel._result_gen = getattr(self, "_result_gen", 0)
        panel.launch(args, subtitle=os.path.basename(
            getattr(self, "last_gcode", "") or ""))

    # -- lifecycle hooks ---------------------------------------------------------#
    def _embed_shutdown(self):
        for panel in getattr(self, "_embed_panels", {}).values():
            if panel.session is not None:
                panel.session.destroy()
                panel.session = None

    def on_close(self):
        self._embed_shutdown()
        super().on_close()

    def _on_finished(self, code):
        """After a successful run, an embedded viewer left open would show the
        PREVIOUS result -- relaunch it on the fresh artifacts instead (an
        embedded page must always show the current result).

        Two rules keep this honest:
          * The relaunch loop must NOT steal navigation. Each _spawn_* calls
            nb.select(page), so without care the LAST relaunched panel would
            yank the user off whatever super()._on_finished landed them on (the
            sim page). We capture the intended page after super() and re-select
            it once the loop is done -- a single-panel relaunch still lands on
            the sim page as designed, because that IS what super() selected.
          * Stale deviation colours are handled by _invalidate_analysis, not by
            skipping a panel: a relaunch cannot re-analyze, so carrying the OLD
            scaffold's deviation onto the new print would be wrong. (Before the
            U2 merge this loop also skipped a separate "deviation" panel; that
            role no longer exists -- deviation is an in-scene mode of the one
            unified Viewer, and a relaunch drops it along with the rest of the
            scene.)

        GENERATE NEVER NAVIGATES TO THE VIEWER. It briefly did, in 08d4c13, and
        that was a misreading of the request: what was asked for is that the
        Viewer loads the newest result WHEN THE USER OPENS IT, not that pressing
        Generate takes them there. The loading now happens on page entry
        (`_on_shell_tab_changed` below), and all this does for it is stamp the
        result with `_mark_result_fresh` so entry can tell a stale panel from a
        current one."""
        running = [(role, p)
                   for role, p in getattr(self, "_embed_panels", {}).items()
                   if p.session is not None and p.session.alive()]
        super()._on_finished(code)
        # the run is over: the idle line stops saying one is under way
        self._refresh_viewer_idle()
        if code != 0:
            return
        # a fresh print invalidates any prior deviation analysis (both shells)
        self._invalidate_analysis()
        # BEFORE the relaunch loop, so the panels it respawns record THIS
        # result's stamp and page entry does not then relaunch them again
        self._mark_result_fresh()
        # the page super() intended the user to land on (sim / g-code text);
        # restore it after the relaunch loop so no panel steals focus
        intended = None
        try:
            intended = self.nb.select()
        except Exception:  # noqa: BLE001
            pass
        for role, panel in running:
            if role == "machine":
                # The live machine view shows the PRINTER, not the print -- there are
                # no "fresh artifacts" for it, so the premise of this loop does not
                # apply. Relaunching killed and respawned the machine-live child (and
                # with it the position feed and the camera the user had set) on every
                # Generate, purely as a side effect of being open. Leave it running.
                continue
            if panel.relauncher is not None:
                try:
                    panel.relauncher()
                except Exception:  # noqa: BLE001
                    pass
        if intended is not None:
            try:
                self.nb.select(intended)
            except Exception:  # noqa: BLE001
                pass
        # a run that ended with Simulation on screen loads there now
        self._load_viewer_if_shown()

    # -- the Viewer loads the newest result WHEN THE USER OPENS IT ---------------#
    #
    # Pressing Generate must leave the user on the page they pressed it from.
    # What they asked for is the other half: "when you switch to the viewer tab
    # on your own, it automatically loads the new generated result". So the work
    # happens on page ENTRY, and the only thing a finished run does is stamp its
    # result so entry can tell a stale panel from a current one.
    #
    # The stamp is a COUNTER, not the file paths. Every scaffold Generate writes
    # to the same `scaffold.gcode` / `scaffold.depo.npz` in the session folder,
    # so comparing paths would call a brand-new result identical to the one on
    # screen and never reload. `_result_gen` increments once per settled result;
    # a panel records the value it was launched at.

    def _refresh_viewer_idle(self):
        """Every idle panel re-reads its idle line. A run's start and end call
        it, since the Simulation line says whether a run is under way."""
        for panel in list(getattr(self, "_embed_panels", {}).values()):
            try:
                panel.refresh_idle()
            except Exception:  # noqa: BLE001 -- wording must never break a run
                pass

    def _mark_result_fresh(self):
        """A new result has settled: bump the generation counter every open
        Viewer is measured against. Called by `_on_finished` (before its relaunch
        loop, so the panels that loop respawns record THIS generation) and by the
        Calibration build, which never reaches `_on_finished` at all -- see
        `gui_sweeps._sw_launch_build`'s docstring for why."""
        self._result_gen = getattr(self, "_result_gen", 0) + 1

    def _viewer_result_ready(self):
        """True when there is something for the unified Viewer to draw: the panel
        exists, and there is BOTH a g-code file and a 3-D result (`last_stl` or
        the `.depo` cache) on disk.

        The 3-D half is what keeps this off a g-code-only run, which has no
        geometry and which `gui_run._on_finished` deliberately sends to the
        G-code text page. A failed or cancelled run leaves the previous result's
        pointers reconciled against disk by `_sync_result_buttons`, so it cannot
        make a stale panel look fresh either -- the counter, not this, is what
        decides that."""
        if self._embed_panel("unified") is None:
            return False
        gco = getattr(self, "last_gcode", None)
        if not (gco and os.path.isfile(gco)):
            return False
        stl = getattr(self, "last_stl", None)
        depo = getattr(self, "last_depo", None)
        return bool((stl and os.path.isfile(stl))
                    or (depo and os.path.isfile(depo)))

    def _on_shell_tab_changed(self, _event=None):
        """The shell's page-entry hook, extended: entering the Viewer page loads
        the newest result into it, with no "Open viewer" press.

        Three states, and only the first two do anything:

          * no session -- the first Generate of a session, which is the case
            reported. Launch.
          * a session showing an OLDER result (`panel._result_gen` behind
            `self._result_gen`). Relaunch. This is the Calibration build's case:
            it has no relaunch loop of its own, so an open Viewer keeps showing
            the previous slide until the user comes back to it.
          * a session already at the current generation. Leave it entirely --
            relaunching would kill and respawn the child, losing the camera the
            user set, every time they glanced at another page and came back.

        `play_gcode(static=True)` also calls `nb.select` on the Viewer page, but
        that page is already current by the time this runs, so no tab-change
        event fires and there is no recursion. `_viewer_entry_busy` guards the
        case anyway, because `panel.launch` pumps the event loop."""
        super()._on_shell_tab_changed(_event)
        self._load_viewer_if_shown()

    def _load_viewer_if_shown(self):
        """Load the newest result into the Simulation page if that page is the
        one on screen. Page entry calls it, and so does a finished run: a run
        that ends while the user is already ON Simulation fires no page entry,
        so without this second call the page kept saying "Generate a design and
        it opens here" over a finished result until Open simulation was pressed.
        It loads the page the user is on and never moves them to another."""
        if getattr(self, "_viewer_entry_busy", False):
            return
        view = self._page_frames.get("view")
        if view is None:
            return
        try:
            if self.nb.select() != str(view):
                return
        except Exception:  # noqa: BLE001
            return
        if not self._viewer_result_ready():
            return
        panel = self._embed_panel("unified")
        live = panel.session is not None and panel.session.alive()
        if live and getattr(panel, "_result_gen", None) == getattr(
                self, "_result_gen", 0):
            return                      # already showing this result
        self._viewer_entry_busy = True
        try:
            self.play_gcode(static=True)
        except Exception as exc:  # noqa: BLE001 -- never break page navigation
            try:
                self.append_log(f"[viewer autoload failed] {exc}\n")
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._viewer_entry_busy = False

    def _invalidate_analysis(self):
        """A new successful run makes any prior deviation sidecar stale (it was
        computed against the PREVIOUS print). Lives on the embed mixin so both the
        classic and modern shells hit it through the one _on_finished
        interception. No-op without gui_deviation."""
        fn = getattr(self, "_invalidate_deviation", None)
        if fn is not None:
            fn()
