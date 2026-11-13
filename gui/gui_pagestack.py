"""gui_pagestack.py -- the modern shell's page container: one area, every page
mapped for the life of the app, a switch is one `lift()`.

**What it replaces and why.** The shell used a hidden-tab `ttk.Notebook`. A
notebook shows one page by UNMAPPING the old one and MAPPING the new one, and on
Windows every Tk widget is a real HWND, so a switch un-mapped and re-mapped up
to ~1200 of them. The cost was never the map itself. It was the tail that a
remap sets off, all of it delivered as `<Configure>` events AFTER the switching
call has returned:

  * `ttk.PanedWindow` re-divides its panes from their weights, so the scaffold
    page's form column resettles (measured 809 -> 798 px) and everything below
    the pill rail shifts;
  * `VScrollFrame`'s canvas re-issues its body width and then its scrollregion,
    a second full pass over the page's widgets;
  * `PreviewPanel` re-renders, because its host got a Configure.

Windows paints the newly mapped HWNDs in between those passes, which is what
"the page fills in from the bottom" was. Photographed with a frame camera,
the first-change spread across the window was
398-983 ms depending on the page.

A page that is never unmapped keeps its geometry, so a revisit runs none of
that: Tk changes one window's stacking order and Windows repaints the exposed
region in a frame or two.

**Mapped off the critical path, and why the size has to be pinned while that
happens.** Mapping all thirteen pages inside `_build_widgets` cost 4.8 s of
extra boot (9.7 s -> 14.5 s, medians of three alternated runs). So `add` only
registers a page; `pin()` then `warm()` map them on a timer once the window is
up, one per tick, and `select` maps a page immediately if the user gets there
first. Boot returns to the notebook's, and no visit is ever slow.

The pin is what makes that safe. The toplevel starts from what its contents
request, and a notebook requests the MAXIMUM over all its pages. `pin()` fixes the
stack's requested size at the largest page's, readable without mapping anything,
since Tk computes a requested size as widgets are built. It stays pinned: a placed
page requests nothing of its master.

**Only the page on top follows the stack's size.** Pages are `place`d at an
explicit size rather than gridded into one cell, so a hidden page does not resize
when the stack does. In a cell they all did: the G-code page folds the status
row away, the stack changes size, and every page laid itself out again, ~360
`<Configure>` for pages nobody could see. A hidden page catches up in
`_fit_current` when it is next selected, and only if the window was resized in
the meantime, because the sizes it alternates between are its own.

**A switch is covered while it paints.** `select` raises `gui_curtain` first, so
the old page stays on screen, untouched, until the new one is finished. The page
itself still changes synchronously inside `select`.

**API.** Only what the app actually uses, so `self.nb` keeps working unchanged
at all ~180 call sites: `add`, `select` (getter and setter), `tabs`, `tab`,
`index`, `forget`, plus `nametowidget` and `bind` inherited from `ttk.Frame`.
`select` fires `<<NotebookTabChanged>>` so the top row's highlight and the Home
refresh still run.

Two deliberate differences from `ttk.Notebook`:

  * `<<NotebookTabChanged>>` is generated SYNCHRONOUSLY (`when="now"`), where
    the notebook queues it. The top row's highlight then repaints in the same
    pass as the page, instead of a frame later -- which is the whole point.
    Re-entrancy is guarded, so a handler that selects another page cannot
    recurse.
  * **Every page reads as mapped**, including the ones nobody is looking at.
    `winfo_ismapped()` is no longer "is this page showing". Code that needs
    that must compare against `stack.select()`. The four in-tree readers were
    audited when this landed: `gui_widgets._reclamp_scroll`,
    `gui_preview` (x2) and `gui_embed` all use it to mean "are we laid out yet",
    which is still exactly what it answers.

**Tab stays on the page on top.** Keyboard traversal walks every mapped
widget in the toplevel, so with every page mapped, Tab from the top row went
into the cards of pages behind the one showing, and Return then acted on a card
nobody could see. The notebook prevented that by unmapping. Here Tk's own
eligibility test, `::tk::FocusOK`, is wrapped once per interpreter to refuse
any widget under a page that is not on top (`_guard_focus`); `select` and
`_map` keep that list of covered pages current. Tab, Shift-Tab and every
`tk_focusNext` caller read the same answer, and widgets outside the stack (the
top row, the bottom row, dialogs) are untouched.

The classic developer UI keeps its real, visible-tab `ttk.Notebook` -- it needs
the tab strip. This module is modern-shell only.
"""

import tkinter as tk
from tkinter import ttk

import gui_curtain


class PageStack(ttk.Frame):
    """A hidden-tab notebook's API over a stack of permanently mapped frames."""

    def __init__(self, parent, **kw):
        # `style=` is meaningless here: there is no tab strip to hide. Accepted
        # and dropped so the call site reads the same as the notebook's did.
        kw.pop("style", None)
        super().__init__(parent, **kw)
        self._order = []            # [path str] in the order added
        self._text = {}             # path str -> tab text
        self._cur = None            # path str of the page on top
        self._firing = False
        self._pending = []          # registered but not yet gridded
        self._pinned = False        # requested size frozen
        self._warming = None        # the pending after() id
        self._size = {}             # path str -> (w, h) it was last placed at
        self.ready = None           # gui_curtain's: is the page done arriving
        self.bind("<Configure>", self._fit_current, add="+")
        self._guard_focus()

    # -- keyboard traversal ---------------------------------------------- #
    _FOCUS_GUARD = r'''
        catch {tk_focusNext .}
        if {[info procs ::tk::FocusOK_beforeMewPages] eq ""
                && [info procs ::tk::FocusOK] ne ""} {
            rename ::tk::FocusOK ::tk::FocusOK_beforeMewPages
            proc ::tk::FocusOK {w} {
                global mew_covered_pages
                if {[info exists mew_covered_pages]} {
                    foreach p $mew_covered_pages {
                        if {$w eq $p || [string first "$p." $w] == 0} {
                            return 0
                        }
                    }
                }
                return [::tk::FocusOK_beforeMewPages $w]
            }
        }
    '''

    def _guard_focus(self):
        """Install the FocusOK wrapper (once per interpreter; see the module
        docstring) and tell it which pages are covered now."""
        try:
            self.tk.eval(self._FOCUS_GUARD)
            covered = [p for p in self._order if p != self._cur]
            self.tk.call("set", "mew_covered_pages", covered)
        except tk.TclError:
            pass                                # torn down

    # -- helpers ---------------------------------------------------------- #
    def _path(self, tab_id):
        """Resolve anything the notebook would accept -- a widget, a path
        string, an integer index, "current" -- to a path string."""
        if tab_id is None:
            return self._cur
        if isinstance(tab_id, int):
            return self._order[tab_id]
        s = str(tab_id)
        if s == "current":
            return self._cur
        if s in self._text:
            return s
        raise tk.TclError("tab %r not managed by this page stack" % (tab_id,))

    # -- the notebook API ------------------------------------------------- #
    def _map(self, path):
        """Place a page in the stack, permanently, keeping the current page on
        top. This is the only place a page is ever mapped, and nothing ever
        un-maps one -- that is the whole mechanism."""
        size = self.winfo_width(), self.winfo_height()
        self._size[path] = size
        self.nametowidget(path).place(in_=self, x=0, y=0, width=size[0],
                                      height=size[1])
        if path != self._cur and self._cur is not None:
            self.nametowidget(self._cur).lift()
        self._guard_focus()

    def _fit_current(self, _e=None):
        """The page on top takes the stack's size, and no other page does."""
        path = self._cur
        size = self.winfo_width(), self.winfo_height()
        if path is None or self._size.get(path) == size:
            return
        self._size[path] = size
        try:
            self.nametowidget(path).place_configure(width=size[0],
                                                    height=size[1])
        except tk.TclError:
            pass                                # torn down

    def add(self, child, text="", **_kw):
        """Register a page. The FIRST one is mapped and becomes the page on top;
        the rest wait for `warm` (or for a `select` that gets there first)."""
        path = str(child)
        if path in self._text:
            self._text[path] = text
            return
        self._order.append(path)
        self._text[path] = text
        if self._cur is None:
            self._cur = path
            self._map(path)
        else:
            self._pending.append(path)

    def pin(self):
        """Freeze the requested size at the largest page's, so the window does
        not grow as `warm` maps the rest. Call once, after every page is added
        and before the toplevel is first mapped."""
        if self._pinned or not self._order:
            return
        ws = [self.nametowidget(p) for p in self._order]
        self.configure(width=max(w.winfo_reqwidth() for w in ws),
                       height=max(w.winfo_reqheight() for w in ws))
        self._pinned = True

    def warm(self, every_ms=16):
        """Map the pages still pending, one per timer tick.

        A timer and not `after_idle`: Tk drains its idle queue in one pass, so an
        idle handler that re-arms itself would map all thirteen pages inside a
        single idle and put the 4.8 s straight back where it was taken from.

        `blocking=False` in spirit -- the caller returns immediately. Mapping a
        page is ~0.4 s of Tk and Windows painting, so a full warm takes a few
        seconds in the background while the window is already usable.
        """
        self._warming = None
        try:
            if self._pending:
                self._map(self._pending.pop(0))
                self._warming = self.after(every_ms, lambda: self.warm(every_ms))
        except tk.TclError:
            pass                                # torn down mid-warm

    def warm_now(self):
        """Map every pending page synchronously. For tests, and for anything that
        needs the stack fully settled before it measures."""
        if self._warming is not None:
            try:
                self.after_cancel(self._warming)
            except tk.TclError:
                pass
            self._warming = None
        while self._pending:
            self._map(self._pending.pop(0))

    def forget(self, tab_id):
        path = self._path(tab_id)
        if path in self._pending:
            self._pending.remove(path)
        else:
            self.nametowidget(path).place_forget()
        self._order.remove(path)
        self._text.pop(path, None)
        if self._cur == path:
            self._cur = self._order[0] if self._order else None
            if self._cur is not None:
                self._map(self._cur)

    def tabs(self):
        return tuple(self._order)

    def index(self, tab_id):
        if str(tab_id) == "end":
            return len(self._order)
        return self._order.index(self._path(tab_id))

    def tab(self, tab_id, option=None, **kw):
        """Only `-text` is stored; nothing in the app sets anything else on a
        shell tab, and the tab strip it would have drawn does not exist."""
        path = self._path(tab_id)
        if kw:
            if "text" in kw:
                self._text[path] = kw["text"]
            return None
        if option in ("text", "-text"):
            return self._text[path]
        return {"text": self._text[path]}

    def select(self, tab_id=None):
        """Getter with no argument (returns the current page's path string, as
        the notebook does); otherwise raise that page and fire
        `<<NotebookTabChanged>>`."""
        if tab_id is None:
            return self._cur
        path = self._path(tab_id)
        if path == self._cur:
            return None
        gui_curtain.hold(self, ready=self.ready)
        self._cur = path
        if path in self._pending:
            # the user got here before `warm` did; map it now and pay the same
            # cost the notebook paid on every single visit, once
            self._pending.remove(path)
            self._map(path)
        page = self.nametowidget(path)
        page.lift()
        self._guard_focus()
        # Every page stays mapped under the raised one, so keyboard focus
        # left in a control on the page just covered stayed there, invisible:
        # after the G-code window, Ctrl+F on Physics went to its hidden Find
        # box and did nothing. Focus follows the page.
        try:
            held = str(self.focus_get() or "")
        except (KeyError, tk.TclError):
            held = ""
        if held.startswith(str(self) + ".") and held != path \
                and not held.startswith(path + "."):
            page.focus_set()
        if not self._firing:
            self._firing = True
            try:
                self.event_generate("<<NotebookTabChanged>>", when="now")
            finally:
                self._firing = False
        # after the handlers: a page that folds the shell changes the stack's
        # size, and this page should be laid out once, at the size it ends on
        self.after_idle(self._fit_current)
        return None
