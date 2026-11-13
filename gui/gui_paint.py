"""gui_paint.py -- make a MODULE-CARD swap arrive as one frame instead of
filling in. Cards only: see the scope note at the bottom.

**The symptom.** Opening or closing a module card painted the new content in
stages -- you watched it fill in over most of a second.

**Why.** On Windows every Tk widget is a real HWND. The Scaffold page holds
~1200 of them and the whole app ~3200, so a swap exposes hundreds of separate
windows and Windows delivers a `WM_PAINT` to each, straight to the screen. Tk
buffers none of it. Profiling a page switch put 590 ms of 623 ms inside Tcl with
barely a thousand Python calls in the whole switch, so this is not a slow
callback and no Python tuning touches it.

That is also why making the swap FASTER did not make it LOOK different: an
earlier pass cut the card swap 3.4x by stacking the two halves and `lift()`ing
instead of unmapping, and the staged fill survived untouched. Fewer maps, same
number of separately-painted child windows.

**What this does.** `WS_EX_COMPOSITED` asks Windows to composite a window's
entire child hierarchy into an off-screen buffer and blit it once. Under it the
old content stays on screen until the new content is completely drawn.

**Measured, with a screen camera** (a probe that
photographs real frames at ~50 fps from a separate process, with a synthetic
bottom-up fill as its control):

    module card open, first-change spread across the window
        without the fence   1248 ms
        with the fence       275 ms

The cards were also confirmed by eye.

**SCOPE -- it has exactly ONE caller left, `ModuleGrid.open`, and three sites
have now measured it as harmful.** Wire it somewhere new only with a measurement
in hand.

**It flashes BLACK wherever the swap UNCOVERS area.** Compositing presents the
redirection surface before the parent has repainted its background into the
hole, so Windows shows the hole. Photographed at ~120 fps against a deliberate
one-paint-cycle control flash:

    scaffold pill-rail swap, fence on    116.8 vs 247 -- 53% darker, 5 reps of 5
    module card CLOSE,       fence on    115.9 vs 241 -- 52% darker, 4 reps of 4
    module card OPEN,        fence on    no dark frame in 4 reps
    either of those,         fence off   no dark frame in any rep

An open COVERS area and does not flash; that is the one place it is still used.

**And it is a LOSS on a PAGE.** Three arms alternated inside one process
put a page switch's first-change
spread at 315 / 355 / 584 ms with the fence OFF against 432 / 558 / 703 ms with
it ON -- worst of all with `invalidate=True`. Compositing a page-sized subtree
costs more painting than it saves, so `gui_shell.goto_page` deliberately has no
fence; its docstring says so. Wire this into a card-sized swap, not a page-sized
one, and re-run that A/B before believing otherwise.

The page switch's own defect was never a paint-batching problem at all: a
notebook UNMAPS the old page and MAPS the new one, and the remap sets off a
relayout cascade (paned re-division, scroll-frame width + scrollregion, preview
re-render) delivered as `<Configure>` events after the switching call returns.
`gui_pagestack` removes it by never unmapping a page. Read that module before
theorising about page switches again.

**Why it is set around a swap and cleared after, never left on.**
`PrintWindow(PW_RENDERFULLCONTENT)` -- which `capture_ui.grab` uses for every
visual check in this repo -- HANGS on a composited window. A probe that called
it burned six minutes of CPU and produced nothing. So the style must be off
whenever the oracle might photograph the window, and the narrowest way to
guarantee that is to hold it across one swap only.

The usual objection -- that `WS_EX_COMPOSITED` breaks OpenGL child windows --
does not apply here: nothing in this app parents a render window to a Tk widget.
Every VTK view renders offscreen and is blitted into a `tk.Label` as a
PhotoImage (`gui_preview.PreviewPanel._render`). Grep for `SetParentId` /
`vtkTkRenderWindow` before assuming that changed.

No-op off Windows, and no-op if the toggle below is off.
"""

import contextlib
import sys

#: Set False to get the old, unbuffered behaviour back for an A/B.
ENABLED = True

_IS_WINDOWS = sys.platform.startswith("win")

_GWL_EXSTYLE = -20
_WS_EX_COMPOSITED = 0x02000000

# RedrawWindow flags: dispatch the already-pending WM_ERASEBKGND and WM_PAINT for
# this window and every descendant RIGHT NOW, without re-entering the event loop.
_RDW_UPDATENOW = 0x0100
_RDW_ALLCHILDREN = 0x0080
# Mark the whole subtree invalid as well. Needed after a lift(): Tk reorders the
# window with SetWindowPos and Windows invalidates the newly exposed region
# ASYNCHRONOUSLY, so at the moment the fence flushes there is nothing marked
# invalid yet and RDW_UPDATENOW alone dispatches nothing. Measured: without it a
# page switch left 48-274 ms of painting to happen AFTER the fence had cleared,
# which is the staged fill the fence was there to prevent.
_RDW_INVALIDATE = 0x0001

#: paint_fence nesting depth; only the outermost fence owns the style.
_depth = 0


def _user32():
    import ctypes
    u = ctypes.windll.user32
    u.GetWindowLongW.restype = ctypes.c_long
    u.SetWindowLongW.restype = ctypes.c_long
    return u


def set_composited(widget, on):
    """Turn WS_EX_COMPOSITED on or off for THIS widget's own window. Returns the
    state actually read back, or None if this platform has no such thing.

    The widget itself, not its toplevel. Compositing buffers the window it is set
    on plus every descendant, so setting it on the smallest container that
    encloses the change keeps the buffered repaint the size of the change.
    Setting it on the toplevel instead makes every swap repaint the entire
    1680x1000 window: measured, that turned a module-card open from 199 ms into
    589 ms and a close from 91 ms into 518 ms. Pass the notebook for a page
    switch and the grid for a card swap.
    """
    if not _IS_WINDOWS:
        return None
    try:
        hwnd = widget.winfo_id()
    except Exception:  # noqa: BLE001
        return None
    if not hwnd:
        return None
    u = _user32()
    ex = u.GetWindowLongW(hwnd, _GWL_EXSTYLE)
    want = (ex | _WS_EX_COMPOSITED) if on else (ex & ~_WS_EX_COMPOSITED)
    if want != ex:
        u.SetWindowLongW(hwnd, _GWL_EXSTYLE, want)
    return bool(u.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_COMPOSITED)


def flush_paints(widget, invalidate=False):
    """Paint everything that is pending under `widget`, synchronously, now.

    `invalidate=True` first marks the whole subtree as needing paint, for a swap
    that re-EXPOSED windows rather than changing them -- a lift(). See
    _RDW_INVALIDATE.

    `update_idletasks()` alone is not enough on Windows. Tk draws its own
    redisplay at idle, but a freshly MAPPED window is painted by Windows in
    response to a WM_PAINT that is only dispatched when the message queue is
    pumped -- after `update_idletasks()` has returned, i.e. after the fence has
    cleared the style.

    `RedrawWindow(..., RDW_UPDATENOW | RDW_ALLCHILDREN)` dispatches those paints
    immediately, inside the fence, without re-entering the Tk event loop --
    which `update()` would do, letting a queued click run mid-switch.
    """
    if not _IS_WINDOWS:
        return
    try:
        hwnd = widget.winfo_id()
    except Exception:  # noqa: BLE001
        return
    if hwnd:
        # NO RDW_ERASENOW. It forces WM_ERASEBKGND ahead of the content, and
        # with it every card flashed BLACK for a moment. RDW_UPDATENOW alone
        # dispatches the WM_PAINTs, which is all that was wanted.
        flags = _RDW_UPDATENOW | _RDW_ALLCHILDREN
        if invalidate:
            flags |= _RDW_INVALIDATE
        _user32().RedrawWindow(hwnd, None, None, flags)


@contextlib.contextmanager
def paint_fence(widget, invalidate=False):
    """Hold the window's painting in one off-screen buffer for the body.

    Pass the SMALLEST container that encloses what is about to change -- the
    ModuleGrid for a card swap -- not the toplevel; see `set_composited` for
    what the toplevel costs::

        with gui_paint.paint_fence(self):
            self._open_inner(key, card)

    The body is followed, INSIDE the fence, by one `update_idletasks()` for Tk's
    own layout and redisplay and then one `flush_paints()` for the WM_PAINTs
    Windows owes the newly mapped windows. Both are needed -- see
    `flush_paints`. Without them the fence clears before anything is drawn and
    does nothing at all.

    This does not make the swap shorter -- it makes it atomic. The old content
    is what the user looks at while the new content is built, instead of a
    half-built version of it. Cutting the duration is a separate job.

    Nests: only the OUTERMOST fence owns the style, so an inner one cannot
    un-buffer the swap it is part of half way through.

    Always clears the style, including on an exception: `capture_ui.grab` hangs
    against a composited window (see the module docstring).
    """
    global _depth
    armed = False
    if ENABLED and _depth == 0:
        armed = bool(set_composited(widget, True))
    _depth += 1
    try:
        yield
        if armed:
            try:
                widget.update_idletasks()
                flush_paints(widget, invalidate=invalidate)
            except Exception:  # noqa: BLE001
                pass
    finally:
        _depth -= 1
        if armed:
            set_composited(widget, False)
