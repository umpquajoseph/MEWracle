"""gui_curtain.py -- make anything a click changes arrive as one frame.

**The symptom.** Switching pages built the new page on screen in pieces: the
sidebar gone first, the ribbon sliced in over a half-erased old page, the tree
filling in a second later. Opening a panel did the same, field by field.

**Why nothing inside Tk can fix it.** Every Tk widget is a real HWND and Windows
paints each one separately, straight to the screen (`gui_paint`'s docstring has
the numbers). `gui_pagestack` removed the relayout tail and `gui_paint`'s
compositing fence loses on a page-sized subtree, so the painting itself stays.
What can change is whether anyone watches it.

**What this does.** Before the switch, the window's client area is copied off
the screen into a native layered popup placed exactly over it, so what is on
screen does not change by one pixel. The switch then runs and paints underneath
in full, and once it is finished the popup is destroyed: one composition pass
takes the old picture away and shows the finished page.

Native and not a Tk toplevel because a Tk window would itself paint late, in the
event loop that is about to be busy. `UpdateLayeredWindow` hands the bitmap to
the compositor at once, and `DwmFlush` waits until it is really on screen before
the switch starts. No thread and no `update()`: `hold` returns and the caller
carries on synchronously, so no page logic, and no test, sees any difference.
The only thing that moved is when the pixels show.

**When it lifts.** Only from Tk's idle queue, which is reached only when nothing
else is pending, with no WM_PAINT left for this thread, on `_QUIET_TICKS` passes
in a row, and with the caller's `ready()` true (the G-code page's regeneration
is what that waits for). `MAX_HOLD_MS` is the backstop.

**Foreground only.** The copy comes off the screen, so a window that is not in
front would copy whatever is covering it. A switch made while the user is in
another app (a Generate finishing) just happens the old way.

**Two ways in.** `PageStack.select` calls `hold` for every page switch, and
clicks made during that hold go nowhere, on purpose: they would otherwise reach a
page nobody can see yet. Everything else comes through `watch`, which needs no
call at any site, so a panel written tomorrow is covered too. Tk queues a layout
change and paints it only once control is back in the event loop, so the first
`grid` / `pack` / `place` / `raise` a click sets off (an execution trace, one
place for all of them) is still in time to take the picture. A click that moves
nothing never raises a cover and pays nothing. That cover lets clicks through,
because a double-click's second click lands while it is up.

What `_armed` keeps out is a change nobody just asked for (a worker finishing)
and a drag: a held button more than `_DRAG_S` old, or Windows' own move/size loop,
must keep drawing live. Typing disarms, so a field's hand-edit marker cannot
freeze the text being typed. A separate Toplevel is its own window, not covered.

No-op off Windows, and no-op if the toggle below is off.
"""

import ctypes
import sys
import time
from ctypes import wintypes

#: Set False to get the uncovered behaviour back for an A/B.
ENABLED = True

#: The longest a page may stay covered, whatever `ready` says.
MAX_HOLD_MS = 4000

_TICK_MS = 16
_QUIET_TICKS = 3

#: A layout change this long after a click still belongs to that click.
_CLICK_S = 1.5
#: A button held longer than this is a drag.
_DRAG_S = 0.3

_IS_WINDOWS = sys.platform.startswith("win")

_GA_ROOT = 2
_SRCCOPY = 0x00CC0020
_ULW_OPAQUE = 4
_SW_SHOWNOACTIVATE = 4
_QS_PAINT = 0x0020
_WS_POPUP = 0x80000000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_LAYERED = 0x00080000
_WS_EX_NOACTIVATE = 0x08000000
_IDC_APPSTARTING = 32650
_CLASS = "MEWracleCurtain"
_VK_LBUTTON, _VK_RETURN, _VK_CONTROL, _VK_ESCAPE = 0x01, 0x0D, 0x11, 0x1B
_GUI_INMOVESIZE = 0x0002


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HANDLE), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HANDLE)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwnds", wintypes.HWND * 6), ("rcCaret", wintypes.RECT)]


_api = None
_cover = None       # the one curtain up, or None
_click_at = 0.0     # monotonic time of the last button press or release
_press_at = 0.0     # ...of the last press


def _load():
    """The Win32 calls, once, with every handle declared pointer-sized."""
    global _api
    if _api is not None:
        return _api
    u = ctypes.WinDLL("user32", use_last_error=True)
    g = ctypes.WinDLL("gdi32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    d = ctypes.WinDLL("dwmapi")
    H, HDC, HWND = wintypes.HANDLE, wintypes.HDC, wintypes.HWND
    u.GetAncestor.argtypes = [HWND, wintypes.UINT]
    u.GetAncestor.restype = HWND
    u.GetForegroundWindow.restype = HWND
    u.IsIconic.argtypes = [HWND]
    u.GetClientRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
    u.ClientToScreen.argtypes = [HWND, ctypes.POINTER(wintypes.POINT)]
    u.GetDC.argtypes = [HWND]
    u.GetDC.restype = HDC
    u.ReleaseDC.argtypes = [HWND, HDC]
    u.DefWindowProcW.restype = ctypes.c_ssize_t
    u.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    u.LoadCursorW.restype = H
    u.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
    u.RegisterClassExW.restype = wintypes.ATOM
    u.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        HWND, H, wintypes.HINSTANCE, ctypes.c_void_p]
    u.CreateWindowExW.restype = HWND
    u.UpdateLayeredWindow.argtypes = [
        HWND, HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
        HDC, ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
        ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD]
    u.ShowWindow.argtypes = [HWND, ctypes.c_int]
    u.DestroyWindow.argtypes = [HWND]
    u.GetQueueStatus.argtypes = [wintypes.UINT]
    u.GetQueueStatus.restype = wintypes.DWORD
    u.GetAsyncKeyState.argtypes = [ctypes.c_int]
    u.GetAsyncKeyState.restype = ctypes.c_short
    u.GetGUIThreadInfo.argtypes = [wintypes.DWORD,
                                   ctypes.POINTER(_GUITHREADINFO)]
    g.CreateCompatibleDC.argtypes = [HDC]
    g.CreateCompatibleDC.restype = HDC
    g.CreateCompatibleBitmap.argtypes = [HDC, ctypes.c_int, ctypes.c_int]
    g.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    g.SelectObject.argtypes = [HDC, wintypes.HGDIOBJ]
    g.SelectObject.restype = wintypes.HGDIOBJ
    g.BitBlt.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, HDC, ctypes.c_int, ctypes.c_int,
                         wintypes.DWORD]
    g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    g.DeleteDC.argtypes = [HDC]
    k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    k.GetModuleHandleW.restype = wintypes.HMODULE
    k.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_char_p]
    k.GetProcAddress.restype = ctypes.c_void_p

    inst = k.GetModuleHandleW(None)
    # the class's window procedure is Windows' own: nothing here needs a
    # message of its own, and no Python callback means none can run mid-paint
    wc = _WNDCLASSEXW(
        cbSize=ctypes.sizeof(_WNDCLASSEXW),
        lpfnWndProc=k.GetProcAddress(k.GetModuleHandleW("user32"),
                                     b"DefWindowProcW"),
        hInstance=inst,
        hCursor=u.LoadCursorW(None, _IDC_APPSTARTING),
        lpszClassName=_CLASS)
    u.RegisterClassExW(ctypes.byref(wc))
    _api = (u, g, d, inst)
    return _api


def _raise_curtain(top, swallow):
    """Copy `top`'s client area off the screen into a popup over it. Returns
    the popup, on screen, or None."""
    u, g, d, inst = _load()
    ex = _WS_EX_LAYERED | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE
    if not swallow:
        ex |= _WS_EX_TRANSPARENT
    r = wintypes.RECT()
    if not u.GetClientRect(top, ctypes.byref(r)):
        return None
    w, h = r.right, r.bottom
    pt = wintypes.POINT(0, 0)
    if w <= 0 or h <= 0 or not u.ClientToScreen(top, ctypes.byref(pt)):
        return None
    hwnd = u.CreateWindowExW(ex, _CLASS, None, _WS_POPUP, pt.x, pt.y, w, h,
                             top, None, inst, None)
    if not hwnd:
        return None
    screen = u.GetDC(None)
    mem = g.CreateCompatibleDC(screen)
    bmp = g.CreateCompatibleBitmap(screen, w, h)
    old = g.SelectObject(mem, bmp)
    try:
        g.BitBlt(mem, 0, 0, w, h, screen, pt.x, pt.y, _SRCCOPY)
        done = u.UpdateLayeredWindow(
            hwnd, screen, ctypes.byref(pt), ctypes.byref(wintypes.SIZE(w, h)),
            mem, ctypes.byref(wintypes.POINT(0, 0)), 0,
            ctypes.byref(_BLENDFUNCTION(0, 0, 255, 0)), _ULW_OPAQUE)
    finally:
        g.SelectObject(mem, old)
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.ReleaseDC(None, screen)
    if not done:
        u.DestroyWindow(hwnd)
        return None
    u.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
    d.DwmFlush()
    return hwnd


def _lift_curtain():
    global _cover
    c, _cover = _cover, None
    if c is not None:
        _load()[0].DestroyWindow(c["hwnd"])


def _arm(c):
    try:
        c["widget"].after(_TICK_MS, lambda: _arrive(c))
    except Exception:  # noqa: BLE001 -- torn down: Windows took the popup with it
        _lift_curtain()


def _arrive(c):
    """The check runs from the idle queue, so a busy Tk cannot reach it."""
    try:
        c["widget"].after_idle(lambda: _check(c))
    except Exception:  # noqa: BLE001
        _lift_curtain()


def _check(c):
    if c is not _cover:
        return
    busy = bool(_load()[0].GetQueueStatus(_QS_PAINT) >> 16 & _QS_PAINT)
    if not busy and c["ready"] is not None:
        try:
            busy = not c["ready"]()
        except Exception:  # noqa: BLE001 -- a guard must never strand the cover
            busy = False
    c["quiet"] = 0 if busy else c["quiet"] + 1
    if c["quiet"] >= _QUIET_TICKS or time.monotonic() > c["until"]:
        _lift_curtain()
    else:
        _arm(c)


def _on_click(_e=None):
    global _click_at
    _click_at = time.monotonic()


def _on_press(_e=None):
    global _press_at
    _on_click()
    _press_at = _click_at


def _on_key(_e=None):
    global _click_at
    _click_at = 0.0


def _armed():
    """Is a layout change happening now the result of a click or a shortcut?
    A press handler runs before the `all` binding that stamps it, so a press
    too new to be stamped is read off the key state."""
    u = _load()[0]
    now = time.monotonic()
    if u.GetAsyncKeyState(_VK_LBUTTON) & 1:
        _on_press()
    elif u.GetAsyncKeyState(_VK_LBUTTON) & 0x8000 and now - _press_at > _DRAG_S:
        return False
    info = _GUITHREADINFO(cbSize=ctypes.sizeof(_GUITHREADINFO))
    if u.GetGUIThreadInfo(0, ctypes.byref(info)) \
            and info.flags & _GUI_INMOVESIZE:
        return False
    if time.monotonic() - _click_at < _CLICK_S:
        return True
    return any(u.GetAsyncKeyState(k) & 0x8000
               for k in (_VK_CONTROL, _VK_RETURN, _VK_ESCAPE))


def _changes_layout(cmd):
    """`grid configure .x`, `pack forget .x`, `place .x -x 0`, `raise .x` --
    not `grid rowconfigure`, `pack info`, and so on. A path under a Toplevel is
    a tip or a dialog: its own window, nothing under this cover."""
    words = cmd.split(None, 2)
    if ".!toplevel" in cmd:
        return False
    if words[0] in ("raise", "lower"):
        return True
    return len(words) > 1 and (words[1] in ("configure", "remove", "forget")
                               or words[1].startswith("."))


def watch(root):
    """Cover whatever a click goes on to change, anywhere under `root`. Call
    once, after the window is built."""
    if not (ENABLED and _IS_WINDOWS):
        return

    def traced(cmd, _op):
        try:
            if _changes_layout(cmd):
                if _cover is not None:
                    _cover["quiet"] = 0
                elif _armed():
                    hold(root, swallow=False)
        except Exception:  # noqa: BLE001 -- cosmetic: must not fail the layout
            pass

    name = root.register(traced)
    for cmd in ("grid", "pack", "place", "raise", "lower"):
        root.tk.call("trace", "add", "execution", cmd, "enter", name)
    root.bind_all("<ButtonPress>", _on_press, add="+")
    root.bind_all("<ButtonRelease>", _on_click, add="+")
    root.bind_all("<KeyPress>", _on_key, add="+")


def hold(widget, ready=None, swallow=True):
    """Cover `widget`'s window with a picture of itself until what follows has
    finished painting (and `ready()`, if given, is true). Call it BEFORE the
    change. Returns at once; a second call while one is up extends that one.
    `swallow` False lets clicks through to the window underneath."""
    global _cover
    if not (ENABLED and _IS_WINDOWS):
        return False
    if _cover is not None:
        _cover["quiet"] = 0
        _cover["ready"] = ready or _cover["ready"]
        return True
    try:
        u = _load()[0]
        top = u.GetAncestor(widget.winfo_id(), _GA_ROOT)
        if not top or u.GetForegroundWindow() != top or u.IsIconic(top):
            return False
        hwnd = _raise_curtain(top, swallow)
    except Exception:  # noqa: BLE001 -- cosmetic: switch uncovered
        return False
    if hwnd is None:
        return False
    _cover = dict(hwnd=hwnd, widget=widget, ready=ready, quiet=0,
                  until=time.monotonic() + MAX_HOLD_MS / 1000)
    _arm(_cover)
    return True
