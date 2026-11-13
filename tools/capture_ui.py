#!/usr/bin/env python
"""capture_ui.py -- render every page of the modern shell to a PNG and check it.

    python tools/capture_ui.py                      # every page -> .claude/visual/
    python tools/capture_ui.py --list               # just list the page ids
    python tools/capture_ui.py --pages scaffold,machine
    python tools/capture_ui.py --preset presets/default_square.json --tag square
    python tools/capture_ui.py --out somewhere/     # different output directory

**Why this exists.** Half of all sessions in this project's history open as a bug
report about the previous session's work, and the defects are almost always ones
seen *on sight* the moment the app opens -- a viewer showing the wrong
thing, controls that do not load, geometry through the bed. Meanwhile the working
rule is "verify it in the running program", and until now the repo
offered no way to do that: nothing rendered the GUI to anything anyone could look
at, and no test asserted on a rendered frame. This closes that gap. An agent runs
this, `Read`s the PNGs back (the Read tool renders images), and describes what it
actually saw -- instead of reasoning about why the code ought to work.

**How the capture works.** Tk cannot be screenshotted while withdrawn, and
`ImageGrab` photographs whatever is physically on screen -- so an occluded window,
or one behind a screensaver, silently yields garbage. Instead this uses the Win32
`PrintWindow(PW_RENDERFULLCONTENT)` call, which asks the window to paint itself
into an off-screen device context. That works while occluded, does not require
focus, and does not steal it. The window is still mapped (Tk will not lay out a
withdrawn widget tree), but it is placed off to the side and never raised.

**What it asserts**, beyond producing images:

* no page raised -- including exceptions swallowed by Tk's callback handler, which
  is how most GUI bugs actually surface;
* no frame is blank -- a page that renders a flat background is broken even though
  nothing raised;
* consecutive pages differ -- if two captures are pixel-identical the navigation
  did not actually switch, so the whole run is meaningless.

These are the machine-checkable half. The images are the other half, and they are
the point: a human or an agent still has to look.

Exits non-zero if any check fails, so the Stop gate can treat it as a suite.
"""

import os as _os, sys as _sys
# A developer script: the program's folders sit beside this one, and
# app_paths puts all of them on sys.path.
_sys.path.insert(0, _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "engine"))
import app_paths  # noqa: E402,F401

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

import perf_budget

# `ctypes.wintypes` does not import off Windows -- its VARIANT_BOOL uses a
# simple-type code only the Windows build of ctypes defines. At module scope it
# would take this file down on Linux BEFORE main()'s platform check runs, which
# is a DIFFERENT failure from the one main() is written to report, and one
# whose exit code says nothing about whether a capture happened. Import it, and
# the two structures built out of it, only where they can exist.
IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import ctypes.wintypes as wt

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / ".claude" / "visual"

# Fixed so captures are comparable between runs and between machines. `--window`
# overrides both, for the one job the fixed size is wrong for: showing a human
# what the UI ACTUALLY looks like on their monitor. A capture at 1680x1000 viewed
# on a 2560x1440 screen reads as "the fonts are too big" -- the fonts are the same
# points, the frame is smaller. Comparisons between runs still want the default.
WIN_W, WIN_H = 1680, 1000

# Off to the side: still mapped (Tk needs that to lay out), never raised.
WIN_X, WIN_Y = 60, 40

# `--window screen` sets this. The window is then sized by the APP's own path
# (_fit_to_screen + _maximize_window, mewracle_gui.py:198-199) rather than by a
# geometry string, which is the only way to be sure the capture matches what a
# user sees. Two consequences, both deliberate: a maximized window is MAPPED, so
# it appears on screen for the length of the run; and a screen-sized window
# parked off-desktop at WIN_X/WIN_Y would have its overhanging strip come back
# blank from PrintWindow, which is what the maximize avoids.
WIN_MAXIMIZE = False

# `--scale PCT` sets this: Windows display scaling to emulate (125, 150, 200).
# Tk is told the matching dpi (`tk scaling`) before any font or widget exists,
# so point-sized text and every "1i"-based size (gui_fonts.dpi_px, row_height)
# grow exactly as on such a display. NOT emulated: the native theme parts
# Windows draws at the real system dpi (check boxes, radio dots, scrollbar
# arrows, combobox buttons) and the title bar. None means the real dpi.
TK_SCALE_PCT = None

# Event-loop pumps after a page switch. Scaled alongside SETTLE_MAX_S below,
# because the two bounds have to be reachable together: at a flat 40 pumps with
# a 10 ms sleep the loop gives up after ~0.4 s no matter how large the wall-clock
# ceiling is, so raising the ceiling alone changed nothing (measured: the same
# 85 s run, the same half-drawn page). On the desktop FACTOR is 1.0 and this is
# the 40 that was tuned there.
SETTLE_PUMPS = int(40 * perf_budget.FACTOR)
# ...but never spin longer than this. Scaled, because the ceiling has to outlast
# whatever the page draws: the scaffold designer's embedded preview takes a
# single VTK frame, which is ~10 ms on a GPU and 4.5-7.0 s through llvmpipe on a
# GPU-less VM. At a flat 3 s the capture fired first and photographed an empty
# preview pane still reading "updating...", which is precisely the blank-frame
# defect these shots exist to catch -- reported as a clean page. On the desktop
# perf_budget.FACTOR is 1.0, so this stays the 3.0 s that was measured there.
SETTLE_MAX_S = perf_budget.seconds(3.0)
MIN_DISTINCT_COLOURS = 12  # below this a frame is effectively blank

PW_RENDERFULLCONTENT = 0x00000002


# ------------------------------------------------------------------ capture

if IS_WINDOWS:
    class _RECT(ctypes.Structure):
        _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                    ("right", wt.LONG), ("bottom", wt.LONG)]

    class _BMIH(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                    ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                    ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                    ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


def _hwnd_of(root):
    """The real top-level frame handle, not the client area.

    `winfo_id()` is the client widget; capturing that against a window DC yields
    a frame-offset image (the spike that proved this technique showed a black
    band down one side until the frame handle was used instead).
    """
    try:
        return int(root.wm_frame(), 16)
    except Exception:
        return root.winfo_id()


def _grab_x11(root):
    """Capture the window from the X server, or raise. Returns a PIL RGB image.

    There is no PrintWindow off Windows: X11 has no "ask a window to redraw
    itself into this DC" call, and reading a window's own drawable comes back
    unpopulated for anything the compositor owns. What is reliable is to read
    the SCREEN and cut the window out of it, which is what Xvfb makes easy --
    the virtual root holds exactly what was drawn and nothing can overlap it,
    because nothing else is running on that display.

    `winfo_rootx/rooty` are the CLIENT origin, which is what we want: under a
    bare Xvfb there is no window manager and so no frame to exclude, and under a
    WM the decorations are not part of the app anyway. The crop is clamped to
    the screen so a window pushed partly off-screen yields a smaller image
    rather than an exception from PIL.
    """
    from PIL import ImageGrab
    root.update_idletasks()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    w, h = root.winfo_width(), root.winfo_height()
    if w <= 1 or h <= 1:
        raise RuntimeError("window has no extent (w=%d h=%d)" % (w, h))
    # xdisplay="" forces PIL's X11 path. Left as None it shells out to
    # gnome-screenshot, which is not installed on a headless VM and fails with a
    # message about the desktop portal -- unrelated to anything here.
    shot = ImageGrab.grab(xdisplay="")
    sw, sh = shot.size
    box = (max(0, x), max(0, y), min(sw, x + w), min(sh, y + h))
    if box[2] <= box[0] or box[3] <= box[1]:
        raise RuntimeError(
            "window at (%d,%d) %dx%d lies outside the %dx%d X screen -- give "
            "Xvfb a bigger screen (-screen 0 %dx%dx24)"
            % (x, y, w, h, sw, sh, x + w, y + h))
    if box != (x, y, x + w, y + h):
        print("capture_ui: WARNING window %dx%d at (%d,%d) is clipped by the "
              "%dx%d X screen -- captured %dx%d. Enlarge the Xvfb screen or the "
              "shot is not the whole UI."
              % (w, h, x, y, sw, sh, box[2] - box[0], box[3] - box[1]))
    return shot.crop(box).convert("RGB")


def grab(root):
    """Capture the window to a PIL image, or raise.

    Under `blind_rig/hidden.py` the window lives on a desktop Windows never
    shows, and PrintWindow there lies in two ways:
      * Windows does not invalidate what a raised page uncovers, so Tk keeps
        the previous page's pixels ("ghosting"). Every mapped widget is
        handed an Expose first, lowest in the stacking order first.
      * OpenGL output never reaches PrintWindow, so a 3-D view drawn straight
        into a Tk frame (gui_preview's GPU-direct path) comes back empty. It
        is read back from VTK and pasted wherever Tk says that frame is on
        top, so panels floating over it stay visible.
    Both are no-ops for a non-Tk target and harmless on a visible desktop."""
    if not IS_WINDOWS:
        return _grab_x11(root)
    if hasattr(root, "tk"):
        _repaint_tk(root)
    img = _grab_window(root)
    if hasattr(root, "tk") and _GL_WINDOWS:
        _overlay_gl(root, img)
    return img


#: every vtkRenderWindow the app has made (track_vtk_windows), so grab() can
#: read a 3-D view back from VTK when PrintWindow cannot see it
_GL_WINDOWS = []


def track_vtk_windows():
    """Remember each VTK render window and the Tk frame it draws into.
    Call before the app builds its 3-D views (build_app does)."""
    try:
        import vtk
    except Exception:  # noqa: BLE001 -- no VTK: nothing to read back
        return
    real = vtk.vtkRenderWindow
    if getattr(real, "_cu_tracked", False):
        return

    def tracked(*a, **k):
        w = real(*a, **k)
        real_set = w.SetWindowInfo

        def set_info(s, _w=w, _real=real_set):
            try:
                _w._cu_hwnd = int(str(s), 0)       # the host frame's HWND
            except ValueError:
                pass
            return _real(s)
        w.SetWindowInfo = set_info
        _GL_WINDOWS.append(w)
        return w
    tracked._cu_tracked = True
    vtk.vtkRenderWindow = tracked


def _repaint_tk(win):
    """Expose every mapped widget under `win`'s toplevel, depth first in
    stacking order, drawing each before its children."""
    tk_ = win.tk
    todo = [str(win.winfo_toplevel())]
    while todo:
        p = todo.pop()
        try:
            if not int(tk_.call("winfo", "ismapped", p)):
                continue
            tk_.call("event", "generate", p, "<Expose>", "-x", 0, "-y", 0,
                     "-width", tk_.call("winfo", "width", p),
                     "-height", tk_.call("winfo", "height", p))
            kids = [str(c) for c in tk_.call("winfo", "children", p)]
            todo.extend(reversed(kids))
            tk_.call("update", "idletasks")
        except Exception:  # noqa: BLE001 -- a widget gone mid-walk
            continue


def _tk_rect(tk_, p):
    x, y = int(tk_.call("winfo", "rootx", p)), int(tk_.call("winfo", "rooty", p))
    return (x, y, x + int(tk_.call("winfo", "width", p)),
            y + int(tk_.call("winfo", "height", p)))


def _above(tk_, top, host, box):
    """Screen rects of the mapped widgets stacked above `host` that overlap
    `box`: at each ancestor of `host`, the later siblings of the branch that
    leads to it. Tk's own tree, so it needs no hit test (`winfo containing`
    sees nothing through drive_ui's click-through window)."""
    out, node = [], top
    while node != host:
        kids = [str(c) for c in tk_.call("winfo", "children", node)]
        nxt = next((k for k in kids if host == k or host.startswith(k + ".")), None)
        if nxt is None:
            break
        for k in kids[kids.index(nxt) + 1:]:      # stacking order, lowest first
            if (str(tk_.call("winfo", "toplevel", k)) != top
                    or not int(tk_.call("winfo", "ismapped", k))):
                continue
            for r in _shape(tk_, k):
                r = (max(r[0], box[0]), max(r[1], box[1]),
                     min(r[2], box[2]), min(r[3], box[3]))
                if r[0] < r[2] and r[1] < r[3]:
                    out.append(r)
        node = nxt
    return out


def _shape(tk_, p):
    """The screen rects a widget covers: its whole rect, or only the pieces
    of a window region cut into it (gui_gcode_cad.py's layer-tree panel shows
    the 3-D view between its rows that way)."""
    r = _tk_rect(tk_, p)
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    gdi32.CreateRectRgn.restype = wt.HRGN
    user32.GetWindowRgn.argtypes = (wt.HWND, wt.HRGN)
    gdi32.GetRegionData.argtypes = (wt.HRGN, wt.DWORD, ctypes.c_void_p)
    gdi32.DeleteObject.argtypes = (wt.HGDIOBJ,)
    rgn = gdi32.CreateRectRgn(0, 0, 0, 0)
    try:
        if not user32.GetWindowRgn(int(str(tk_.call("winfo", "id", p)), 0), rgn):
            return [r]                                  # ERROR: no region set
        n = gdi32.GetRegionData(rgn, 0, None)
        buf = ctypes.create_string_buffer(n)
        if not gdi32.GetRegionData(rgn, n, buf):
            return [r]
        count = ctypes.c_uint32.from_buffer(buf, 8).value   # RGNDATAHEADER.nCount
        rects = (wt.LONG * (4 * count)).from_buffer(buf, 32)
        return [(r[0] + rects[i], r[1] + rects[i + 1], r[0] + rects[i + 2],
                 r[1] + rects[i + 3]) for i in range(0, 4 * count, 4)]
    finally:
        gdi32.DeleteObject(rgn)


def _tk_path_of(tk_, top, hwnd):
    todo = [top]
    while todo:
        p = todo.pop()
        if int(str(tk_.call("winfo", "id", p)), 0) == hwnd:
            return p
        todo.extend(str(c) for c in tk_.call("winfo", "children", p))
    return None


def _overlay_gl(win, img):
    """Paste each visible VTK view into `img` (a grab of `win`'s frame),
    leaving the widgets stacked above that view as PrintWindow drew them."""
    from PIL import Image
    user32 = ctypes.windll.user32
    user32.GetAncestor.restype = wt.HWND
    user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
    frame = _hwnd_of(win)
    fr = _RECT()
    user32.GetWindowRect(frame, ctypes.byref(fr))
    tk_ = win.tk
    for w in list(_GL_WINDOWS):
        hwnd = getattr(w, "_cu_hwnd", 0)
        try:
            if (not hwnd or not user32.IsWindowVisible(hwnd)
                    or user32.GetAncestor(hwnd, 2) != frame
                    or w.GetOffScreenRendering() or w.GetNeverRendered()):
                continue
            import vtk
            from vtk.util import numpy_support
            f = vtk.vtkWindowToImageFilter()
            f.SetInput(w)
            f.ReadFrontBufferOff()
            f.ShouldRerenderOn()
            f.Update()
            data = f.GetOutput()
            gw, gh, _ = data.GetDimensions()
            arr = numpy_support.vtk_to_numpy(data.GetPointData().GetScalars())
            gl = Image.fromarray(arr.reshape(gh, gw, -1)[::-1, :, :3].copy(), "RGB")
        except Exception:  # noqa: BLE001 -- a view torn down mid-shot
            continue
        r = _RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.right <= r.left or r.bottom <= r.top:
            continue
        top = str(win.winfo_toplevel())
        box = (r.left, r.top, r.right, r.bottom)
        try:
            host = _tk_path_of(tk_, top, hwnd)
            if host is None:
                continue
            over = _above(tk_, top, host, box)
        except Exception:  # noqa: BLE001 -- a widget destroyed mid-walk
            continue
        keep = [(img.crop((a - fr.left, b - fr.top, c - fr.left, d - fr.top)),
                 (a - fr.left, b - fr.top)) for a, b, c, d in over]
        img.paste(gl.resize((r.right - r.left, r.bottom - r.top)),
                  (r.left - fr.left, r.top - fr.top))
        for piece, at in keep:
            img.paste(piece, at)


def _grab_window(root):
    """PrintWindow of the window's frame, as a PIL image."""
    from PIL import Image
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    hwnd = _hwnd_of(root)

    rect = _RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise RuntimeError("window has no extent (w=%d h=%d)" % (w, h))

    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    try:
        if not user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT):
            raise RuntimeError("PrintWindow failed")
        bi = _BMIH()
        bi.biSize = ctypes.sizeof(_BMIH)
        bi.biWidth, bi.biHeight = w, -h       # negative height = top-down rows
        bi.biPlanes, bi.biBitCount = 1, 32
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
        return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1
                                ).convert("RGB")
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)


def _capture_backend_missing():
    """"" if a capture can actually happen here, else why it cannot.

    Checked BEFORE building the app, because the answer decides whether this run
    can produce a G4 look at all -- and a run that cannot must say so instead of
    exiting 0 with no pixels. The Windows path needs nothing beyond Windows; the
    X11 path needs Pillow and a display to read.
    """
    if IS_WINDOWS:
        return ""
    try:
        from PIL import ImageGrab  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return "Pillow is not importable: %r" % (exc,)
    if not os.environ.get("DISPLAY"):
        return ("no DISPLAY on %s -- run under `xvfb-run -a -s \"-screen 0 "
                "%dx%dx24\"`" % (sys.platform, WIN_W + 2 * WIN_X,
                                 WIN_H + 2 * WIN_Y))
    return ""


def distinct_colours(img):
    cols = img.getcolors(maxcolors=1 << 20)
    return len(cols) if cols else 1 << 20      # None => more than maxcolors


# ------------------------------------------------------------------- driver

def settle(root, pumps=SETTLE_PUMPS, max_s=SETTLE_MAX_S):
    """Pump the event loop so layout and any idle redraw complete.

    Bounded by BOTH a pump count and a wall clock, computed before the loop and
    not mutated inside it -- an unbounded settle loop is exactly the shape that
    has frozen this machine before.
    """
    deadline = time.time() + max_s
    for _ in range(pumps):
        if time.time() >= deadline:
            break
        try:
            root.update_idletasks()
            root.update()
        except Exception:
            break
        time.sleep(0.01)


# ---------------------------------------------------------------- generate

# Hard ceiling on the Generate wait. Bounded because an unattended capture that
# waits forever on a wedged worker is the exact shape that has frozen this
# machine before: the bound is computed once, before the loop, and never
# recomputed inside it.
GENERATE_MAX_S = 600.0

# Dialogs are FATAL to an unattended run -- there is nobody to click them, so a
# modal would block the event loop until something kills the process. Every one
# is redirected into a list, which has the useful side effect of turning a
# validation refusal into a recorded problem instead of a hang.
_DIALOG_FUNCS = ("showerror", "showwarning", "showinfo", "askyesno",
                 "askokcancel", "askretrycancel", "askquestion")


def silence_dialogs(sink):
    """Redirect tkinter's modal dialogs into `sink`. Returns an undo callable.

    Patches the `tkinter.messagebox` module object itself, so it covers every
    caller: the GUI modules reach these through `from gui_common import *`,
    which re-exports that same module, and they call them by attribute.
    """
    from tkinter import messagebox

    saved = {}
    for name in _DIALOG_FUNCS:
        fn = getattr(messagebox, name, None)
        if fn is None:
            continue
        saved[name] = fn

        def make(_name):
            def _stub(title=None, message=None, **_kw):
                sink.append("%s: %s -- %s" % (_name, title, message))
                # Answer every question in the negative. For an unattended run
                # the safe reply is the one that does not commit to a further
                # action nobody asked for.
                if _name == "askquestion":
                    return "no"
                return False if _name.startswith("ask") else "ok"
            return _stub

        setattr(messagebox, name, make(name))

    def undo():
        for name, fn in saved.items():
            setattr(messagebox, name, fn)
    return undo


def run_generate(root, app, dialogs, max_s=GENERATE_MAX_S, here=False):
    """Press Generate and pump the event loop until the worker exits.

    This is what makes a capture worth looking at. Without it every page is
    photographed in the state the app boots into -- the Viewer reads "No
    scaffold loaded", the result buttons are greyed, the status lines are
    placeholders -- so the whole class of defects that live in rendered geometry
    is outside the frame.

    Pumping (rather than sleeping) is required: the worker is a subprocess whose
    output is drained by `_poll_output`, which reschedules itself with
    `root.after`, so progress only happens while the event loop turns.
    """
    rep = {"started": False, "finished": False, "exit_code": None,
           "seconds": None, "problems": []}
    t0 = time.time()
    deadline = t0 + max_s        # computed ONCE, before the loop

    # Generate builds the Design page's text, which is written when the page
    # first opens: open it and wait, as a person sees it fill, then press
    # (`here`: press it on the page already showing, as a user on the
    # Scaffold designer or Calibrate does)
    if not here and hasattr(app, "_nav_open") and hasattr(app, "gce"):
        app._nav_open("design")
        while time.time() < deadline and not app.gce.get_text().strip():
            settle(root, pumps=4, max_s=0.25)

    try:
        app.run()
    except Exception as exc:  # noqa: BLE001
        rep["problems"].append("run() raised: %r" % (exc,))
        return rep

    # Calibrate builds its slides first and launches the worker a moment
    # after run() returns: give a late start a few seconds before calling
    # it refused (a refusal that shows a dialog ends the wait at once; one
    # that does not costs this wait)
    late = time.time() + 8
    n_dialogs = len(dialogs)
    while (getattr(app, "proc", None) is None and time.time() < late
           and len(dialogs) == n_dialogs):
        settle(root, pumps=4, max_s=0.25)

    if getattr(app, "proc", None) is None:
        # run() returned without launching: validation refused it, or the
        # worker failed to start. Either way the dialog text says why.
        rep["problems"].append(
            "Generate did not start" +
            (" -- " + dialogs[-1] if dialogs else " (no dialog explained why)"))
        return rep

    rep["started"] = True
    while time.time() < deadline:
        settle(root, pumps=4, max_s=0.25)
        if getattr(app, "proc", None) is None:
            rep["finished"] = True
            break
    rep["seconds"] = round(time.time() - t0, 2)

    if not rep["finished"]:
        # A worker still running when we give up must be stopped -- otherwise it
        # outlives this process and keeps writing into the scratch folder.
        rep["problems"].append("worker did not finish within %.0fs -- cancelled"
                               % max_s)
        try:
            app.cancel()
            settle(root, pumps=60, max_s=5.0)
        except Exception as exc:  # noqa: BLE001
            rep["problems"].append("cancel failed: %r" % (exc,))
        return rep

    # What the run actually produced. These are the attributes the result
    # buttons and the viewers read, so they double as a check that _on_finished
    # wired everything up.
    for attr in ("last_stl", "last_depo", "last_gcode"):
        rep[attr] = getattr(app, attr, None)
    try:
        rep["status"] = app.status.get()
    except Exception:  # noqa: BLE001
        pass
    if not (rep.get("last_stl") or rep.get("last_depo")):
        rep["problems"].append(
            "run finished but produced no STL and no .depo cache")
    return rep


#: how long to wait for an embedded viewer child to announce itself, once a page
#: has started one. Scaled like the other budgets; the child is a VTK process
#: that parses the g-code and builds the deposition mesh before its first frame.
EMBED_HELLO_MAX_S = perf_budget.seconds(60.0)


def wait_for_embedded_viewer(root, app):
    """Pump until every embedded panel that is RUNNING has announced itself.

    Opening the Viewer page now starts the child by itself (the page loads the
    newest result with no button press -- gui_embed._on_shell_tab_changed), and
    that child takes far longer than `settle`'s three seconds to publish a first
    frame. Without this the Viewer page is photographed reading "Starting the
    viewer...", which passes every machine check here -- it is not blank, it did
    not raise, it did switch -- while being exactly the page nobody can learn
    anything from.

    Costs nothing on the other twelve pages: no panel is starting, immediate
    return. Bounded, and a child that never says hello is left to the capture,
    because a half-started viewer in the image is better evidence than a hang.

    Reads `panel.state`, not `panel._hello`: `launch` sets the state to
    "starting" on every relaunch and the hello moves it to "running", whereas
    `_hello` holds the LAST payload and is never cleared, so a relaunched panel
    would look ready before its new child had said anything."""
    panels = getattr(app, "_embed_panels", None)
    if not panels:
        return
    deadline = time.time() + EMBED_HELLO_MAX_S
    while time.time() < deadline:
        if not [p for p in panels.values()
                if getattr(p, "state", None) == "starting"]:
            return
        settle(root, pumps=20, max_s=0.5)


def _parse_window(spec):
    """`WxH` -> (w, h). "screen" is handled in main(), not here: it is a window
    STATE (maximized), not a size."""
    m = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", spec)
    if not m:
        raise SystemExit("--window wants WxH (e.g. 2560x1440) or \"screen\"")
    return int(m.group(1)), int(m.group(2))


def build_app(errors):
    """Construct the modern shell, routing Tk's swallowed exceptions into `errors`."""
    import tkinter as tk
    import mewracle_gui

    # MUST precede tk.Tk(), and must match what the real entry points do
    # (mewracle_gui.py:178 / gcode_to_fibers_gui.py:73). Skipping it renders the
    # capture at 96 DPI on a display where the app renders at the monitor's --
    # i.e. the oracle photographs a window the user never sees. No-op off Windows
    # and at 100% scaling.
    mewracle_gui.enable_dpi_awareness()
    if IS_WINDOWS:
        track_vtk_windows()     # before any 3-D view exists; grab() reads them back

    root = tk.Tk()
    if TK_SCALE_PCT:
        root.tk.call("tk", "scaling", 96.0 * TK_SCALE_PCT / 100.0 / 72.0)
    root.geometry("%dx%d+%d+%d" % (WIN_W, WIN_H, WIN_X, WIN_Y))

    # Tk swallows exceptions raised inside callbacks and prints them to stderr,
    # so a broken page looks like a working one to the caller. Capture them.
    def _hook(exc, val, tb):
        errors.append("".join(traceback.format_exception(exc, val, tb)))
    root.report_callback_exception = _hook

    # The named-font ramp, BEFORE the widgets -- exactly as mewracle_gui.main()
    # does it (mewracle_gui.py:196). Without it every capture renders in Tk's
    # default family instead of MEWracle's, because init_fonts also retunes
    # TkDefaultFont & co. That is a photograph of a window no user ever sees,
    # which is the one thing this tool exists not to produce.
    import gui_fonts
    gui_fonts.init_fonts(root)

    app = mewracle_gui.ModernApp(root)
    if WIN_MAXIMIZE:
        from gcode_to_fibers_gui import _fit_to_screen
        _fit_to_screen(root)          # the restore geometry...
        mewracle_gui._maximize_window(root)   # ...then zoom, exactly as main() does
    settle(root)
    return root, app


def page_list(app):
    order = getattr(app, "_page_order", None)
    if not order:
        raise RuntimeError("shell exposes no _page_order; gui_shell.py changed")
    return [pid for pid, _frame in order]


# ------------------------------------------------------------ opened cards

#: the page states that change what an OPENED card draws. The collector mode
#: swaps whole sections on the two pages that carry it; Calibration shows one
#: tab's frame inside each per-tab card. Every other page has one state.
COLLECTOR_STATES = (("3ax", "plate"), ("4ax", "mandrel"), ("5ax", "surface"))
COLLECTOR_PAGES = ("scaffold", "machine")

#: a card taller than this many viewports is photographed in this many slices
#: and no more -- a bound, computed before the loop, on a page that could
#: otherwise ask for an arbitrary number of grabs.
MAX_SLICES = 8


def _descendants(widget):
    """Every widget under `widget`, depth first. Iterative, so a deep form
    cannot hit the recursion limit."""
    stack = list(widget.winfo_children())
    while stack:
        w = stack.pop()
        yield w
        stack.extend(w.winfo_children())


def _outer_grids(widget, exclude=None):
    """The ModuleGrids under `widget` that no other ModuleGrid under it
    contains, in widget-tree order. A grid nested inside a card's body is
    reached by opening that card, not from the page."""
    import gui_widgets
    grids = [w for w in _descendants(widget)
             if isinstance(w, gui_widgets.ModuleGrid) and w is not exclude]
    names = [str(g) for g in grids]
    outer = [g for g in grids
             if not any(str(g).startswith(n + ".") for n in names)]
    return sorted(outer, key=str)


def _scroller_of(widget):
    """The nearest VScrollFrame holding `widget`, or None."""
    from gui_common import VScrollFrame
    w = widget
    while w is not None:
        if isinstance(w, VScrollFrame):
            return w
        w = getattr(w, "master", None)
    return None


def _page_scroller(frame):
    """The VScrollFrame that scrolls a page's form: the page itself when it is
    one, else the largest mapped one inside it (the Scaffold designer's form
    pane sits beside its preview in a PanedWindow). A small scroller nested in
    a panel is not the page's, and slicing by it photographs one frame N
    times."""
    from gui_common import VScrollFrame
    if isinstance(frame, VScrollFrame):
        return frame
    best, area = None, 0
    for w in _descendants(frame):
        if isinstance(w, VScrollFrame) and w.winfo_ismapped():
            a = w.winfo_width() * w.winfo_height()
            if a > area:
                best, area = w, a
    return best


def _open_disclosures(widget):
    """Open every collapsed Disclosure under `widget`, so a card's nested
    groups are in the photograph. Returns the ones it opened, to shut again."""
    import gui_widgets
    opened = []
    for w in _descendants(widget):
        if isinstance(w, gui_widgets.Disclosure) and not w.is_open():
            try:
                w.set_open(True)
                opened.append(w)
            except Exception:  # noqa: BLE001
                pass
    return opened


def page_states(app, pid):
    """[(tag, apply)] for one page -- `apply()` puts the page in that state."""
    if pid in COLLECTOR_PAGES and hasattr(app, "s_collector"):
        return [(tag, (lambda m=mode: app.s_collector.set(m)))
                for tag, mode in COLLECTOR_STATES]
    if pid == "gcode" and getattr(app, "_cal_proc_order", None):
        # Calibrate is a mode of this page: each type, then Design again
        def cal(key):
            app._nav_select("calibration")
            app._cal_tab_var.set(key)
        return ([("", None)]
                + [("cal-" + key, (lambda k=key: cal(k)))
                   for key in app._cal_proc_order]
                + [("back", lambda: app._nav_select("gcode"))])
    return [("", None)]


def _slices(root, scroller):
    """Photograph the scroller's page top to bottom, one viewport at a time.
    Returns the images. Leaves the page scrolled back to the top."""
    if scroller is None:
        return [grab(root)]
    canvas = scroller._canvas
    if canvas.winfo_height() <= 1:
        return [grab(root)]
    canvas.yview_moveto(0.0)
    settle(root, pumps=10, max_s=0.5)
    lo, hi = canvas.yview()
    span = max(hi - lo, 1e-6)
    n = min(MAX_SLICES, max(1, int(-(-1.0 // span))))   # ceil(1/span)
    imgs = []
    for i in range(n):
        canvas.yview_moveto(min(i * span, 1.0 - span))
        settle(root, pumps=10, max_s=0.5)
        imgs.append(grab(root))
    canvas.yview_moveto(0.0)
    settle(root, pumps=4, max_s=0.25)
    return imgs


def capture_cards(root, app, pages, out, tag, errors, check):
    """The opened-card capture: every card on every page, opened, in every
    state that changes what it draws, sliced top to bottom.

    `--generate` photographs pages with their cards CLOSED, and every layout
    complaint about a parameter row lives inside an OPENED card -- so that
    capture is blind to the class this one exists for. Files are named
    `<page>[-<state>]__<card>[.<nested card>]_sN.png`; the closed tile view of
    each page state is `...__tiles_sN.png`.

    `check(img, rec, before)` is main()'s per-image check (blank / raised /
    unswitched). One extra check lives here: a card that did not actually
    open is a failure, because its photograph is of something else."""
    records = []

    def shoot(name, scroller, rec):
        before = len(errors)
        for i, img in enumerate(_slices(root, scroller)):
            path = out / ("%s_s%d%s.png" % (name, i, tag))
            img.save(path)
            rec.setdefault("files", []).append(str(path))
            check(img, rec, before)
            before = len(errors)

    def walk_grid(grid, prefix, scroller):
        for key in grid.keys(visible_only=True):
            rec = {"page": "%s__%s" % (prefix, key), "ok": True,
                   "problems": []}
            try:
                grid.open(key)
                settle(root)
                if grid.opened() != key:
                    rec["ok"] = False
                    rec["problems"].append("card %r did not open" % key)
                card = grid.card(key)
                shut = _open_disclosures(card.body)
                settle(root)
                shoot("%s__%s" % (prefix, key), scroller, rec)
                records.append(rec)
                for inner in _outer_grids(card.body, exclude=grid):
                    walk_grid(inner, "%s__%s" % (prefix, key), scroller)
                    inner.close()
                for d in shut:
                    d.set_open(False)
            except Exception as exc:  # noqa: BLE001
                rec["ok"] = False
                rec["problems"].append("capture raised: %r" % (exc,))
                if rec not in records:
                    records.append(rec)
            finally:
                try:
                    grid.close()
                except Exception:  # noqa: BLE001
                    pass
                settle(root, pumps=10, max_s=0.5)

    for pid in pages:
        frame = app._page_frames[pid]
        app._nav_select(pid)
        settle(root)
        wait_for_embedded_viewer(root, app)
        for state, apply in page_states(app, pid):
            prefix = pid + ("-" + state if state else "")
            if apply is not None:
                apply()
                settle(root)
            grids = [g for g in _outer_grids(frame) if g.winfo_ismapped()]
            scroller = _page_scroller(frame)
            rec = {"page": prefix + "__tiles", "ok": True, "problems": []}
            shoot(prefix + "__tiles", scroller, rec)
            records.append(rec)
            for grid in grids:
                walk_grid(grid, prefix, _scroller_of(grid) or scroller)
    return records


def main(argv=None):
    global WIN_W, WIN_H   # --window rewrites them; declared here because the
                          # flag's help text reads them first.
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--pages", default="", help="comma-separated page ids")
    ap.add_argument("--preset", default="", help="preset json to apply first")
    ap.add_argument("--tag", default="", help="suffix for the output filenames")
    ap.add_argument("--list", action="store_true", help="list page ids and exit")
    ap.add_argument("--window", default="",
                    help="capture window size as WxH (default %dx%d), or "
                         "\"screen\" to open MAXIMIZED via the app's own sizing "
                         "path -- what a user actually sees. \"screen\" maps the "
                         "window, so it is visible while the run lasts."
                         % (WIN_W, WIN_H))
    ap.add_argument("--scale", type=float, default=0,
                    help="emulate this Windows display scaling, in percent")
    ap.add_argument("--generate", action="store_true",
                    help="press Generate and wait for the worker before "
                         "capturing, so the pages show a real result")
    ap.add_argument("--generate-timeout", type=float, default=GENERATE_MAX_S,
                    help="seconds to wait for the worker (default %d)"
                         % GENERATE_MAX_S)
    ap.add_argument("--shots", default="",
                    help="after --generate, also write offscreen multi-angle "
                         "geometry renders to this directory")
    ap.add_argument("--cards", action="store_true",
                    help="open every card on every page and photograph each, "
                         "in each collector mode (Machine, Scaffold designer) "
                         "and each Calibration tab, sliced top to bottom")
    args = ap.parse_args(argv)

    missing = _capture_backend_missing()
    if missing:
        # Returning 0 here is what once let the Stop gate report a UI change as
        # looked-at while NOTHING was rendered -- a green failure, and the exact
        # thing the G4 gate exists to prevent. `_render_evidence_fresh` does not
        # save us: its verdict goes only to telemetry (`_claim_observe` records,
        # never blocks).
        #
        # A Mac/Linux fork still must not be blocked by tooling it never asked
        # for, so the two cases are separated by whether THIS repo's gate is the
        # thing judging the work. In a cloud session it is, and an unrendered UI
        # change has to be loud enough to stop the turn.
        print("capture_ui: no capture backend here (%s) -- NOTHING WAS RENDERED."
              % missing)
        if os.environ.get("CLAUDE_CODE_REMOTE") == "true":
            print("capture_ui: cloud session -- the G4 look did NOT happen. "
                  "Do not report this UI change as visually verified.")
            return 2
        return 0

    if args.scale:
        global TK_SCALE_PCT
        TK_SCALE_PCT = args.scale
    if args.window:
        if args.window.strip().lower() == "screen":
            global WIN_MAXIMIZE
            WIN_MAXIMIZE = True
        else:
            WIN_W, WIN_H = _parse_window(args.window)

    errors = []
    dialogs = []
    # Installed BEFORE the app is built: a modal raised during construction
    # would hang just as hard as one raised during the run.
    restore_dialogs = silence_dialogs(dialogs)
    root, app = build_app(errors)

    try:
        pages = page_list(app)
        if args.list:
            for p in pages:
                print(p)
            return 0

        if args.pages:
            want = [p.strip() for p in args.pages.split(",") if p.strip()]
            unknown = [p for p in want if p not in pages]
            if unknown:
                print("unknown page ids: %s" % ", ".join(unknown))
                print("available: %s" % ", ".join(pages))
                return 2
            pages = want

        if args.preset:
            data = json.loads(Path(args.preset).read_text(encoding="utf-8"))
            kind = data.get("kind") or "scaffold"
            payload = data.get("values", data)
            app._apply_preset_for_kind(kind, payload)
            settle(root)

        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        tag = ("_" + args.tag) if args.tag else ""

        gen = None
        shots = None
        if args.generate:
            print("generating (up to %.0fs)..." % args.generate_timeout)
            gen = run_generate(root, app, dialogs, max_s=args.generate_timeout)
            print("  %s in %ss -> stl=%s depo=%s"
                  % ("finished" if gen.get("finished") else "DID NOT FINISH",
                     gen.get("seconds"), gen.get("last_stl"),
                     gen.get("last_depo")))
            for p in gen["problems"]:
                print("  - %s" % p)

            # Offscreen geometry renders from whatever the run cached. Run as a
            # SUBPROCESS, not imported: this process already owns a live Tk
            # main window, and standing a VTK render window up beside it is a
            # combination the rest of the app deliberately never makes.
            #
            # `stl_viewer.py --shots` is CORRECT here and is not the deprecated
            # viewer. That file's interactive window is dead (the unified Viewer
            # replaced it), but --shots/--thumbnail are its live offscreen
            # backend and are the only headless way to look at real geometry.
            # Do not "modernise" this to the unified Viewer: that one is an
            # embedded child of a running GUI and cannot render headless.
            stem = gen.get("last_stl") or gen.get("last_depo")
            if args.shots and stem:
                stem = stem.replace(".depo.npz", ".stl")
                cmd = [sys.executable, str(REPO / "viewers" / "stl_viewer.py"), stem,
                       "--shots", args.shots]
                try:
                    cp = subprocess.run(cmd, capture_output=True, text=True,
                                        timeout=300, cwd=str(REPO))
                    shots = {"cmd": cmd, "returncode": cp.returncode,
                             "stdout": cp.stdout.strip()}
                    print(cp.stdout.rstrip() or cp.stderr.rstrip())
                except subprocess.TimeoutExpired:
                    shots = {"cmd": cmd, "returncode": None,
                             "stdout": "timed out after 300s"}
                    print("  - shots timed out")
            elif args.shots:
                print("  - --shots given but the run cached no geometry")

        records, failures = [], []
        last = {"img": None}

        def check(img, rec, before):
            """The machine-checkable half, for one photograph."""
            rec["size"] = list(img.size)
            n = distinct_colours(img)
            rec["distinct_colours"] = n
            if n < MIN_DISTINCT_COLOURS:
                rec["ok"] = False
                rec["problems"].append(
                    "frame is effectively blank (%d distinct colours)" % n)

            raised = errors[before:]
            if raised:
                rec["ok"] = False
                rec["problems"].append(
                    "%d exception(s) during render; first:\n%s"
                    % (len(raised), raised[0].strip().splitlines()[-1]))

            cur = img.tobytes()
            if last["img"] is not None and cur == last["img"]:
                rec["ok"] = False
                rec["problems"].append(
                    "pixel-identical to the previous capture — navigation did "
                    "not switch, so this run proves nothing")
            last["img"] = cur

        if args.cards:
            records = capture_cards(root, app, pages, out, tag, errors, check)
            failures = [r["page"] for r in records if not r["ok"]]

        for pid in ([] if args.cards else pages):
            rec = {"page": pid, "ok": True, "problems": []}
            before = len(errors)
            try:
                app._nav_select(pid)
                settle(root)
                wait_for_embedded_viewer(root, app)
                img = grab(root)
            except Exception as exc:
                rec["ok"] = False
                rec["problems"].append("capture raised: %r" % (exc,))
                records.append(rec)
                failures.append(pid)
                continue

            path = out / ("%s%s.png" % (pid, tag))
            img.save(path)
            rec["file"] = str(path)
            check(img, rec, before)

            records.append(rec)
            if not rec["ok"]:
                failures.append(pid)

        if gen and gen["problems"]:
            # A capture whose Generate failed is a capture of the empty app --
            # exactly the blind spot --generate exists to remove. It must not
            # report success.
            failures.append("generate")

        manifest = {
            "window": [WIN_W, WIN_H],
            "preset": args.preset or None,
            "generate": gen,
            "shots": shots,
            "dialogs": dialogs,
            "pages": records,
            "failures": failures,
        }
        (out / ("manifest%s.json" % tag)).write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")

        for r in records:
            mark = "ok  " if r["ok"] else "FAIL"
            files = r.get("files") or [r.get("file", "")]
            print("%s %-16s %s" % (mark, r["page"], files[0]
                                   + (" (+%d)" % (len(files) - 1)
                                      if len(files) > 1 else "")))
            for p in r["problems"]:
                print("       - %s" % p.replace("\n", "\n         "))

        print("\n%d captured, %d failed -> %s"
              % (len(records), len(failures), out))
        if failures:
            print("LOOK AT THE IMAGES. A green run is not a verified run — the "
                  "checks here only catch blank, raised, and unswitched.")
        return 1 if failures else 0
    finally:
        restore_dialogs()
        try:
            root.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
