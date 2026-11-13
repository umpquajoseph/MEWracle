#!/usr/bin/env python3
"""gui_shell.py -- MODERN-UI ONLY: the top-row shell. _ShellMixin overrides
_CoreMixin._build_widgets and re-mounts the EXISTING tab builders (scaffold /
build-plate / g-code / calibration / printhead-sim / viewers) under one row that
reads as the job, left to right: Home, Setup, Calibrate, Design, Simulation,
More. The row drives a stack of pages (gui_pagestack), and the bottom row ends
the line: Generate, Save, Send to printer.

`self.nb` presents the notebook API, which is why the identity-based glue keeps
working unmodified -- _active_mode's frame checks, gui_run._on_finished's
g-code-only surfacing, view_gcode's jump to the G-code text page, and the
<<NotebookTabChanged>> event the row listens to for its highlight.

Every field keeps its Tk variable, validator, CLI arg-builder, preset key and
grey-out glue because the builders are REUSED, not rewritten. The only builder
this mixin redirects is the physics `_build_sim_tab` hook: the Printhead-sim panel
mounts on the Physics page instead of a buried sub-tab of the scaffold tab's global
notebook. The classic (developer) UI never imports this module."""

from gui_common import *

import gui_core
import gui_curtain
import gui_fonts
import gui_form
import gui_pagestack
import gui_physics_diagrams
import gui_widgets
import gui_tips
from gui_tips import add_tip


class _SaveGcodeButton(ttk.Button):
    """Save g-code as: `follower` (Send to printer) greys with it. Six places
    set this button's state, and whether there is a file to send is the same
    question each time."""

    follower = None

    def configure(self, cnf=None, **kw):
        if "state" in kw and self.follower is not None:
            self.follower.configure(state=kw["state"])
        return super().configure(cnf, **kw)

    config = configure


class _ShellMixin:
    SHELL_MIN_W, SHELL_MIN_H = 1080, 640

    # The top row, left to right: (tab, its label, the pages under it). The
    # first page is where a click lands. More's pages are a menu; a tab with
    # two pages shows them on the strip under the row (`_nav_refresh`).
    NAV_TABS = (("home", "Home", ("home",)),
                ("setup", "Setup", ("machine", "physics")),
                ("calibrate", "Calibrate", ("calibration",)),
                ("design", "Design", ("gcode", "scaffold")),
                ("simulation", "Simulation", ("view",)),
                ("more", "More ▾", ("history", "machine_link", "log")))

    # Generate-button labels per input mode: "Generate" on every page. Home's
    # step 3, the tour and Ctrl+G all call it Generate, and a G-code page run
    # can write the deposited fibers (.depo) with no STL, so "Generate STL"
    # would be a second name and not always a true one.
    _MODE_TARGETS = {
        "scaffold": "Generate",
        "gcode": "Generate",
        "calibration": "Generate",
        "mandrel": "Generate",
    }

    # ------------------------------------------------------------------ #
    # the window skeleton
    # ------------------------------------------------------------------ #
    def _build_widgets(self):
        self.pad = dict(gui_fonts.PAD)          # the mounted builders read this
        self.root.minsize(self.SHELL_MIN_W, self.SHELL_MIN_H)
        self._noninput_frames = set()           # pages that never retarget Generate
        # The shell launches on Home (a non-input page), and Design is the CAD
        # page, so Generate starts out pointed at that page's design.
        self._last_input_mode = "gcode"

        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)
        self._shell_outer = outer       # the Legacy UI's sidebar packs before it
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        # before the pages: the G-code page puts its tools in the row's context
        # box (`_nav_context`)
        self._build_top_row(outer)

        # The page container. NOT a Notebook: a notebook shows a page by
        # unmapping the old one and mapping the new one, and that remap is what
        # made a page switch fill in from the bottom over most of a second (the
        # paned re-division, the scroll-frame's width/scrollregion pass and the
        # preview re-render it sets off, with Windows painting ~1200 child HWNDs
        # in between). gui_pagestack keeps every page mapped and switches with
        # lift(), so a revisit does no geometry work at all. It presents the
        # notebook API -- add / select / tabs / nametowidget and
        # <<NotebookTabChanged>> -- so every frame-identity check, view_gcode's
        # jump and the top row's highlight keep working unmodified.
        self.nb = gui_pagestack.PageStack(outer)
        self.nb.ready = self._page_arrived
        self.nb.grid(row=2, column=0, sticky="nsew")

        pages = []          # (page_id, top-row tab, label, frame)

        # --- Home (non-input) -------------------------------------------- #
        home = VScrollFrame(self.nb, padding=gui_fonts.PAGE_MARGIN)
        self.nb.add(home, text="Home")
        self._build_home_page(home.body)
        self.tab_home = home
        self._noninput_frames.add(home)
        pages.append(("home", "home", "Home", home))

        # --- Machine & collector: a module grid under the 3/4/5-axis toggle
        #     (gui_pages._build_machine_grid). The toggle itself waits for the
        #     Scaffold designer, which creates s_collector -- see
        #     _finish_machine_page below. ----------------------------------- #
        self.tab_machine = VScrollFrame(self.nb, padding=gui_fonts.PAGE_MARGIN)
        self.nb.add(self.tab_machine, text="Printer")
        mbody = self.tab_machine.body
        mbody.columnconfigure(0, weight=1)
        ttk.Label(mbody, text="Printer & collector",
                  style="Title.TLabel").grid(row=0, column=0, sticky="w")
        gui_form.page_intro(mbody, "Set up your printer once and save "
                                   "it as a preset.")
        self._build_machine_grid(mbody)
        pages.append(("machine", "setup", "Printer & collector",
                      self.tab_machine))

        # --- Physics: one tiered page replacing the old Calibration + Printhead-sim
        #     pages. Process (per-print) knobs on top, per-rig calibration below. Built
        #     BEFORE the scaffold tab: _build_scaffold_tab fires the _build_sim_tab hook,
        #     which this mixin redirects onto the Process host (_sim_host) here. Gated on
        #     _build_sim_controls; the calibration section no-ops without physics. --- #
        self._sim_page = None
        self._physics_page = None
        if hasattr(self, "_build_sim_controls"):
            page = VScrollFrame(self.nb, padding=gui_fonts.PAGE_MARGIN)
            self.nb.add(page, text="Physics")
            self._physics_page = page
            self._sim_page = page            # _build_sim_tab targets _sim_host
            self.tab_physics = page
            self._noninput_frames.add(page)
            pbody = page.body
            pbody.columnconfigure(0, weight=1)
            ttk.Label(pbody, text="Physics", style="Title.TLabel").grid(
                row=0, column=0, sticky="w")
            intro = gui_form.page_intro(
                pbody, self.PHYS_INTRO, wrap=900)
            # the line also says whose values a fresh install shows, while it
            # shows them (gui_pages._phys_intro_text)
            self._phys_intro_label = intro.winfo_children()[0]
            # THE MODULE GRID. Three columns, not the Scaffold page's two: there is
            # no preview panel on this page, so the whole window width is the form
            # pane. Card BODIES get that width either way (ModuleGrid opens a module
            # over the entire grid), so the column count sizes the TILES alone.
            grid = gui_widgets.ModuleGrid(pbody, columns=3)
            grid.grid(row=2, column=0, sticky="nsew")
            pbody.rowconfigure(2, weight=1)
            self._physics_grid = grid
            # PROCESS, first in reading order and registered here so it leads the
            # grid; its body is filled by the _build_sim_tab hook when the scaffold
            # tab builds (self.scaf_vars must exist first, as in the classic UI).
            # Two more cards are in the same position -- Fiber diameter and
            # Visualization -- and _build_calibration_section stashes their hosts
            # in the same dict.
            proc = grid.add("process", "Process parameters", info=(
                "What you set for this print: the nozzle–collector gap, "
                "the applied voltage, "
                "the melt pressure and the melt temperature.\n\n"
                "Print speed is set per scaffold under Parameters on the "
                "Design page, because each scaffold in a stack can run at its "
                "own speed."))
            # An inner frame, NOT proc.body itself. `ModuleGrid.add` weights
            # body's column 0 so an opened module fills the pane, and `_field`
            # grids its label sticky="e" in column 0 -- so mounting the knobs
            # straight into body right-aligned all four against the far edge of a
            # 1400 px pane, 1200 px from the heading they belong under.
            # Same cause as the coiling-wave fields; the other cards get their
            # unweighted frames from `_phys_card`, and this one needs one too.
            self._sim_proc_fields = ttk.Frame(proc.body)
            self._sim_proc_fields.grid(row=0, column=0, sticky="nw")
            # the rig picture beside the knobs, as on every law card
            proc._figure = gui_physics_diagrams.mount(proc.body, "process",
                                                      0, 0, 1)
            self._sim_hosts = {"process": self._sim_proc_fields}
            # kept under its old name too: the hook has targeted _sim_host since
            # before the grid existed, and the classic path still reads it.
            self._sim_host = self._sim_proc_fields
            self._build_calibration_section(grid)
            pages.append(("physics", "setup", "Physics", page))

        # --- Design pages -------------------------------------------------- #
        # Scaffold designer: the scrolling form on the left, the embedded live 3D
        # preview (gui_preview) fixed on the right -- OUTSIDE the scroll canvas so it
        # stays visible while the form scrolls, and so the native VTK child window never
        # lives inside a scrolled canvas. The page frame carries the tab identity glue.
        self.tab_scaffold = ttk.Frame(self.nb)
        self.nb.add(self.tab_scaffold, text="Scaffold")
        self.tab_scaffold.rowconfigure(0, weight=1)
        self.tab_scaffold.columnconfigure(0, weight=1)
        build_preview = getattr(self, "_build_scaffold_preview", None)
        if build_preview is not None:
            # physics build: the form and the live 3D preview share a ttk.PanedWindow so
            # the divider can be DRAGGED instead of a fixed column that clipped the form.
            # The preview's offscreen-blit render makes this safe -- Tk owns the pixels,
            # so the sash slides over it with no native-child clipping.
            paned = ttk.PanedWindow(self.tab_scaffold, orient="horizontal")
            paned.grid(row=0, column=0, sticky="nsew")
            scaf_scroll = VScrollFrame(paned, padding=gui_fonts.PAGE_MARGIN)
            self._build_scaffold_tab(scaf_scroll.body)
            paned.add(scaf_scroll, weight=2)
            build_preview(paned)                  # adds itself as the 2nd pane
            self._scaf_paned = paned
            self._scaf_sash_set = False
            paned.bind("<Map>", self._scaf_init_sash, add="+")
            paned.bind("<Configure>", self._scaf_init_sash, add="+")
        else:
            scaf_scroll = VScrollFrame(self.tab_scaffold,
                                       padding=gui_fonts.PAGE_MARGIN)
            scaf_scroll.grid(row=0, column=0, sticky="nsew")
            self._build_scaffold_tab(scaf_scroll.body)
        pages.append(("scaffold", "design", "Scaffold designer",
                      self.tab_scaffold))
        # s_collector exists now: give the Machine page its toggle
        self._finish_machine_page()

        # G-code: the editor of the exact file Save writes, with the designer's
        # preview beside it (gui_gcode_editor). It absorbs both the old
        # "G-code import" page (Open file + the Convert-to-STL settings) and
        # the old Results "G-code text" page, so one page, one name. It stays
        # page id "gcode" and the "gcode" input mode.
        if hasattr(self, "_build_gcode_editor_page"):
            self.tab_gcode = ttk.Frame(self.nb)
            self.nb.add(self.tab_gcode, text="G-code")
            self._build_gcode_editor_page(self.tab_gcode)
            pages.append(("gcode", "design", "G-code window", self.tab_gcode))
        else:
            self.tab_gcode = VScrollFrame(self.nb,
                                          padding=gui_fonts.PAGE_MARGIN)
            self.nb.add(self.tab_gcode, text="G-code")
            gbody = self.tab_gcode.body
            gbody.columnconfigure(0, weight=1)
            ttk.Label(gbody, text="Convert existing G-code",
                      style="Title.TLabel").grid(row=0, column=0, sticky="w")
            gui_form.page_intro(
                gbody, "Turn a g-code file from any source into a "
                       "deposited-fiber STL, with fibers drooping between the "
                       "crossings that hold them.")
            gform = ttk.Frame(gbody)
            gform.grid(row=2, column=0, sticky="ew")
            self._build_gcode_tab(gform)
            pages.append(("gcode", "design", "G-code import", self.tab_gcode))

        # (the Mandrel page is now the "mandrel" value of the Scaffold designer's
        #  collector toggle, not a page of its own, so the two collector geometries share
        #  layers/speed/diameter, the sag solver, facets, the preview colour, the export
        #  options and the presets. See gui_scaffold._build_collector_toggle.)

        # (the Printhead-sim page was merged into the Setup-section "Physics"
        #  page above -- no separate row here anymore.)

        # --- Simulation and the Log ------------------------------------------#
        self._build_viewer_pages(pages)

        # --- Print history (physics hook; may add nothing) ------------------#
        # Every Generate is auto-recorded. A More page with a no-op-when-absent
        # posture: if the builder adds no tab, there is no page here.
        before = len(self.nb.tabs())
        if hasattr(self, "_build_history_tab"):
            self._build_history_tab(self.nb)
        if len(self.nb.tabs()) > before:
            hist_frame = self.nb.nametowidget(self.nb.tabs()[-1])
            self._noninput_frames.add(hist_frame)
            self.tab_history = hist_frame
            pages.append(("history", "more", "Print history", hist_frame))

        # --- Machine link (physics hook; may add nothing) --------------------#
        # The live printer link: connect to Klipper/Moonraker over the network to watch
        # the machine move, send prints, set temps and jog. A NON-input page (it drives
        # the real printer, not the Generate pipeline), so it joins _noninput_frames like
        # Print history; same tab-count-delta registration.
        before = len(self.nb.tabs())
        if hasattr(self, "_build_machine_link_tab"):
            self._build_machine_link_tab(self.nb)
        if len(self.nb.tabs()) > before:
            ml_frame = self.nb.nametowidget(self.nb.tabs()[-1])
            self._noninput_frames.add(ml_frame)
            self.tab_machine_link = ml_frame
            pages.append(("machine_link", "more", "Printer link", ml_frame))

        # --- persistent action bar + status --------------------------------#
        self._build_action_bar(outer)

        # --- the row's registry (built last: every page frame now exists) ---#
        self._finish_top_row(pages)

        # the same tab-changed glue as the classic UI, plus the shell's
        # top-row-highlight / home-refresh listener
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.nb.bind("<<NotebookTabChanged>>", self._on_shell_tab_changed,
                     add="+")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._bind_shell_keys()

        self.nb.select(home)
        self._on_tab_changed()
        self._on_shell_tab_changed()
        if hasattr(self, "_summary_start"):
            self._summary_start()

        # Freeze the page area at the largest page's size, then map the other
        # twelve pages on a timer once the window is up. Mapping them here
        # instead cost 4.8 s of boot; leaving them unmapped would make the
        # window grow the first time a heavy page was visited. See
        # gui_pagestack.pin / warm.
        self.nb.pin()
        self.nb.warm()
        gui_curtain.watch(self.root)

    def _scaf_init_sash(self, _e=None):
        """One-shot: seat the scaffold form|preview sash so the preview starts at 60% of
        the page, the larger half; the pane weights match the seat (2:3) so a window
        resize keeps the split where it started instead of drifting back toward the form.
        Fires on the first map/configure that has a real width; the user's own drags
        are theirs to keep.

        ONE FLOOR, on the seat and on every later window resize: the form is never
        left narrower than its module cards ask, up to 60 % of the page (so the
        preview never becomes the smaller half). At 1280x720 the 40 % seat gave
        359 px of card grid against the 508 the cards ask, and every card title
        and On/Off badge was cut; shrinking a wide window scaled the form down by
        the pane weights to the same state. A sash DRAG does not configure the
        pane, so it is never undone -- only a resize that would cut the cards is."""
        paned = getattr(self, "_scaf_paned", None)
        if paned is None:
            return
        w = paned.winfo_width()
        if w < 200:
            return
        if not getattr(self, "_scaf_sash_set", True):
            try:
                paned.sashpos(0, int(w * 0.40))     # form 40%, preview 60%
            except tk.TclError:
                return
            self._scaf_sash_set = True
        # The floor waits for idle: the scrolling form re-lays its content a
        # pass behind the sash, and a flush from inside this handler reordered
        # the first seat itself (582 -> 556 px at 1680 wide).
        if not getattr(self, "_scaf_floor_pending", False):
            self._scaf_floor_pending = True
            self.root.after_idle(self._scaf_keep_cards)

    def _scaf_keep_cards(self):
        """Widen the form by the cards' measured shortfall and look again. Only
        ever rightward and never past 60 % of the page, so it ends."""
        self._scaf_floor_pending = False
        paned = getattr(self, "_scaf_paned", None)
        if paned is None:
            return
        try:
            cap = int(paned.winfo_width() * 0.60)
            for _ in range(4):
                paned.update_idletasks()
                short, pos = self._scaf_cards_short(paned), paned.sashpos(0)
                if short <= 0 or pos >= cap:
                    break
                paned.sashpos(0, min(pos + short, cap))
        except tk.TclError:
            return

    def _scaf_cards_short(self, paned, cache="_scaf_card_grids"):
        """How many pixels the narrowest-served shown ModuleGrid in the form
        pane is short of the width its cards ask for (0 when all fit). The
        grids are found once, under `cache` (one attribute per page that asks:
        the Calibration page keeps its own); the collector mode only maps and
        unmaps them."""
        grids = getattr(self, cache, None)
        if grids is None:
            try:
                form = paned.nametowidget(paned.panes()[0])
            except (tk.TclError, IndexError):
                return 0
            grids, todo = [], [form]
            while todo:
                w = todo.pop()
                if isinstance(w, gui_widgets.ModuleGrid):
                    grids.append(w)
                todo.extend(w.winfo_children())
            setattr(self, cache, grids)
        return max([g._tiles.winfo_reqwidth() - g._tiles.winfo_width()
                    for g in grids if g.winfo_ismapped()] + [0])

    # ------------------------------------------------------------------ #
    # the top row
    # ------------------------------------------------------------------ #
    def _build_top_row(self, outer):
        """The row itself: wordmark and tabs on the left; the design's one-line
        state, the G-code page's tools and the gear on the right. Under it, the
        strip for a tab with two pages, in the grid only while it is wanted."""
        wrap = ttk.Frame(outer, style="Bar.TFrame")
        wrap.grid(row=0, column=0, sticky="ew")
        self._nav_wrap = wrap
        row = ttk.Frame(wrap, style="Bar.TFrame", padding=(12, 1, 8, 1))
        row.pack(fill="x")
        gui_widgets.hairline(wrap).pack(fill="x")
        self._nav_row = row
        ttk.Label(row, text="MEWracle", style="TopWordmark.TLabel").pack(
            side="left", padx=(0, 16))

        self._nav_btns = {}         # tab -> its button
        for tab, label, _pids in self.NAV_TABS:
            btn = ttk.Button(row, text=label, style="TopTab.TButton",
                             command=lambda t=tab: self._nav_open(t))
            btn.pack(side="left", padx=1)
            self._nav_btns[tab] = btn

        # packed right, in this order, so a narrow window shortens the design's
        # line and never the tabs or the tools
        gear = ttk.Frame(row, style="Bar.TFrame")
        gear.pack(side="right", padx=(8, 0))
        self._nav_gear = gear
        # the G-code page's tools; `_gce_full_window` shows them with the page
        self._nav_context = ttk.Frame(row, style="Bar.TFrame")

        self._nav_strip = ttk.Frame(outer, padding=(gui_fonts.PAGE_MARGIN, 8,
                                                    gui_fonts.PAGE_MARGIN, 0))
        self._nav_strip.grid(row=1, column=0, sticky="ew")
        self._nav_strip.grid_remove()

    def _finish_top_row(self, pages):
        """Every page frame exists now: the registry, the strip's bars, the
        More menu, the design's line and the gear."""
        by_id = {pid: (tab, label, frame) for pid, tab, label, frame in pages}
        self._page_frames = {}      # page id -> frame
        self._page_order = []       # [(page id, frame)] in top-row order
        self._page_labels = {}      # page id -> its name
        self._nav_tab_of = {}       # str(frame) -> tab
        self._nav_pid_of = {}       # str(frame) -> page id
        listed = [p for _t, _l, pids in self.NAV_TABS for p in pids]
        for pid in listed + [p for p in by_id if p not in listed]:
            if pid not in by_id:
                continue
            tab, label, frame = by_id[pid]
            self._page_frames[pid] = frame
            self._page_order.append((pid, frame))
            self._page_labels[pid] = label
            self._nav_tab_of[str(frame)] = tab
            self._nav_pid_of[str(frame)] = pid

        self._nav_seen = set()      # tabs shown this session (Home's next step)
        self._nav_last_setup = "machine"
        self._nav_bars = {}         # tab -> (SegmentedBar, its var)
        self._nav_more_menu = tk.Menu(self.root, tearoff=False)
        for tab, _label, pids in self.NAV_TABS:
            here = [(p, self._page_labels[p]) for p in pids
                    if p in self._page_frames]
            if tab == "more":
                for pid, label in here:
                    self._nav_more_menu.add_command(
                        label=label, command=lambda p=pid: self._nav_select(p))
            elif len(here) > 1:
                var = tk.StringVar(value=here[0][0])
                bar = gui_widgets.SegmentedBar(
                    self._nav_strip, here, var,
                    command=lambda v=var: self._nav_select(v.get()))
                self._nav_bars[tab] = (bar, var)

        if hasattr(self, "_build_design_summary"):
            self._build_design_summary(self._nav_row).pack(
                side="right", fill="x", expand=True, padx=(16, 8))
        # the dark_mode var and its pref, as in the classic strip -- the toggle
        # itself lives in the Settings panel (gui_settings), so the checkbox is
        # built and never shown
        chk = self._build_theme_toggle(self._nav_gear)
        chk.pack_forget()
        if hasattr(self, "_build_settings_button"):
            self._build_settings_button(self._nav_gear).pack(side="right")
        else:
            chk.pack(side="right")

    def _nav_tab(self):
        """Which tab is lit: the tab of the page on top."""
        return self._nav_tab_of.get(self.nb.select(), "home")

    def _nav_open(self, tab):
        """A tab was pressed. Setup comes back to the page it was left on;
        Design always lands on the CAD page, the heart of the app."""
        if tab == "more":
            btn = self._nav_btns["more"]
            self._nav_more_menu.tk_popup(btn.winfo_rootx(), btn.winfo_rooty()
                                         + btn.winfo_height())
        elif tab == "setup":
            self._nav_select(self._nav_last_setup)
        else:
            self._nav_select(next(pids[0] for t, _l, pids in self.NAV_TABS
                                  if t == tab))

    def _nav_refresh(self):
        """Light the tab of the page on top, and show the strip under the row
        for a tab with two pages. The CAD page keeps its height and shows no
        strip: the Scaffold designer is reached from Home's More tools (the
        CAD page's Designer group was removed on purpose, 91e487b2)."""
        tab = self._nav_tab()
        for key, btn in self._nav_btns.items():
            btn.configure(style="TopTabSel.TButton" if key == tab
                          else "TopTab.TButton")
        pid = self._nav_pid_of.get(self.nb.select())
        self._nav_seen.add(tab)
        if tab == "setup" and pid is not None:
            self._nav_last_setup = pid
        for bar, _var in self._nav_bars.values():
            bar.pack_forget()
        shown = self._nav_bars.get(tab)
        if shown is not None and pid != "gcode":
            shown[1].set(pid)
            shown[0].pack(side="left")
            self._nav_strip.grid()
        else:
            self._nav_strip.grid_remove()

    def _on_shell_tab_changed(self, _event=None):
        """Light the top row's tab for the active page (fires on user clicks AND
        programmatic nb.select calls, e.g. view_gcode's jump to the G-code text
        page), and refresh the Home page's live bits whenever it is shown."""
        try:
            cur = self.nb.select()
        except tk.TclError:
            return
        self._nav_refresh()
        follow = getattr(self, "_tour_follow_page", None)
        if callable(follow):
            follow()
        home = self._page_frames.get("home")
        if home is not None and cur == str(home):
            self._refresh_home()
        # Home's step 4 is done once Simulation has been shown this result
        view = self._page_frames.get("view")
        if view is not None and cur == str(view):
            g = getattr(self, "last_gcode", None)
            if g and os.path.isfile(g):
                self._home_sim_seen = g
        # The Machine link's "Will send" line is otherwise refreshed only by
        # the connection pump, so a printer not yet connected read "No g-code
        # generated yet." straight after a Generate.
        link = self._page_frames.get("machine_link")
        if link is not None and cur == str(link) \
                and hasattr(self, "_ml_refresh_print_name"):
            self._ml_refresh_print_name()
        # A page's guide ("Design: the tree on the left is your print…",
        # Calibrate's "Calibrate is optional…") belongs to that page. Leaving
        # it takes the guide down, unless something else has been said since:
        # reported, Print history still read Design's guide.
        gce = self._page_frames.get("gcode")
        guides = tuple(g for g in (getattr(self, "_GCE_ARRIVAL", None),
                                   getattr(self, "_CAL_GUIDE", None)) if g)
        if gce is not None and cur != str(gce) and guides:
            try:
                if self.status.get() in guides:
                    self.status.set("")
            except (AttributeError, tk.TclError):
                pass
        # a page that held back its builds while hidden builds on arrival
        arrived = getattr(self, "_mandrel_page_arrived", None)
        if arrived is not None:
            arrived()
        # the Generate target can change with the page
        if hasattr(self, "_summary_refresh"):
            self._summary_refresh()

    def goto_page(self, frame):
        """Switch pages -- what every user-facing control calls.

        NO paint fence here, and that is a measured decision rather than an
        omission. gui_paint's WS_EX_COMPOSITED buffering is a win for a module
        card but a LOSS for a page: three arms alternated inside one process
        put the first-change spread
        at 315 / 355 / 584 ms with it off against 432 / 558 / 703 ms with it on,
        worst of all when it also forced the subtree invalid. Compositing a
        page-sized subtree costs more painting than it saves. Do not re-add it
        without re-running that A/B.

        What is left, after the stack removed the relayout cascade, is Windows
        delivering a WM_PAINT to each newly exposed child window; nothing
        in-process batches that, so `gui_curtain` hides it instead (from
        `PageStack.select`, which every switch goes through).
        """
        self.nb.select(frame)

    def _page_arrived(self):
        """`gui_curtain`'s question: is there a page's worth of content still
        on its way from outside the event loop?"""
        return True

    def _nav_select(self, page_id):
        """Jump to a page by registry id: the top row and the Home steps."""
        frame = self._page_frames.get(page_id)
        if frame is not None:
            self.goto_page(frame)
        # an open tour hides off its step's page (Design -> Calibrate is the
        # same frame, so no tab-change event says so)
        follow = getattr(self, "_tour_follow_page", None)
        if callable(follow):
            follow()

    def _generate_next_step(self, lead="Done."):
        """A Generate succeeded: say what comes next, in the words of the top
        row and the bottom row, instead of a bare "Done.". It keeps the
        "Done." prefix that scripts read. The G-code window also puts it in
        its own notice bar, with the way to Simulation on it -- ONCE, the
        first time it is shown (`generate_banner_seen` in gui_prefs.json, so
        it survives a restart); every later Generate gets the status line
        only. Another notice already up wins.
        `lead` is what was made; Calibrate passes its own ("Generated every
        slide → sweep_print.stl.")."""
        try:
            pid = self._nav_pid_of.get(self.nb.select())
        except (AttributeError, tk.TclError):
            pid = None
        # (Simulation folds the status row away too, and needs no pointer)
        text = (lead + " Next: watch it print in Simulation, then Save "
                "g-code as or Send to printer.")
        full = getattr(self, "_gce_pv_full", None)
        if pid == "gcode" and full and self._gce_off_plate(full[1] or {}):
            # the picture's plate check: said at the moment the file is made
            text = (lead + " Part of the print is off the plate: check "
                    "Setup's Collector card before you print it.")
        self.status.set(text)
        show = getattr(self, "_gce_notice_show", None)
        # a send's outcome was about the file before this one: the new
        # result replaces it rather than leave "Sent …" over a file not sent
        if (pid != "gcode" or not callable(show)
                or getattr(self, "_gce_notice_kind", None)
                not in (None, "generated", "send")):
            return
        import gui_prefs
        if gui_prefs.get("generate_banner_seen"):
            return
        gui_prefs.update(generate_banner_seen=True)

        def to_sim():
            self._gce_notice_hide_if("generated")
            self._nav_select("view")
        show(text, [("Open Simulation", to_sim),
                    ("OK", lambda: self._gce_notice_hide_if("generated"))],
             kind="generated")
        seq = self._gen_notice_seq = getattr(self, "_gen_notice_seq", 0) + 1

        def expire():
            if seq == self._gen_notice_seq:
                self._gce_notice_hide_if("generated")
        self.root.after(15000, expire)

    # ------------------------------------------------------------------ #
    # action bar (the classic bottom strip, relocated + relabelled)
    # ------------------------------------------------------------------ #
    def _build_action_bar(self, outer):
        wrap = ttk.Frame(outer, style="Bar.TFrame")
        wrap.grid(row=3, column=0, sticky="ew")
        gui_widgets.hairline(wrap).pack(fill="x")   # 1-px edge over the bar
        bar = ttk.Frame(wrap, style="Bar.TFrame", padding=(12, 10))
        bar.pack(fill="x")
        self._shell_bar = bar   # the G-code page packs its display strip in

        self.run_btn = ttk.Button(bar, text="Generate", style="Primary.TButton",
                                  command=self._generate)
        self.run_btn.pack(side="left", padx=(0, 4))
        add_tip(self.run_btn, "Build what the active design page describes "
                              "(Ctrl+G).")
        self.cancel_btn = ttk.Button(bar, text="Cancel", command=self.cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=4)

        # Generation render quality -- same semantics/var as the classic strip
        # (lines/fibers skip the slow STL mesh; Export still builds exact).
        # Fibers (smooth) by default -- see the note on the classic strip's copy.
        # Its box is in the G-code page's Quality panel: Generate reads this
        # var on every page
        self.gen_render = tk.StringVar(value=RENDER_VAL2LBL["tube:12"])

        # determinate + empty when idle -- see gui_core's copy
        self.progress = ttk.Progressbar(bar, mode="determinate", length=160,
                                        maximum=1.0, value=0)
        # off the row until a run starts (gui_run._progress_show)

        # packed FIRST (before Generate), so a narrow window squeezes the
        # progress bar, packed last, and never the Save buttons off the edge
        savef = ttk.Frame(bar, style="Bar.TFrame")
        savef.pack(side="right", before=self.run_btn)
        self.save_result_btn = ttk.Button(savef, text="Save STL as…",
                                          command=self.save_result_as,
                                          state="disabled")
        self.save_result_btn.pack(side="left", padx=2)
        self.save_gcode_btn = _SaveGcodeButton(savef, text="Save g-code as…",
                                               command=self.save_gcode_as,
                                               state="disabled")
        self.save_gcode_btn.pack(side="left", padx=2)
        add_tip(savef, "Generated files live in a throwaway per-session "
                       "scratch folder; save a copy to keep one. The scratch "
                       "folder is removed when the app closes.")
        # the end of the line: what Save would write, sent to the printer
        self.send_btn = ttk.Button(savef, text="Send to printer",
                                   command=self.send_to_printer,
                                   state="disabled")
        self.send_btn.pack(side="left", padx=2)
        self.save_gcode_btn.follower = self.send_btn
        add_tip(self.send_btn, "Send this g-code to the printer that Printer "
                               "link is connected to. Connect it under More.")

        self.status = tk.StringVar(value="Ready.")
        # on every page, Simulation and the G-code window included: refusals
        # and run results are written here, so the row never folds away
        self._shell_status = ttk.Label(outer, textvariable=self.status,
                                       anchor="w", style="Hint.TLabel")
        self._shell_status.grid(row=4, column=0, sticky="ew", padx=12,
                                pady=(3, 4))
        # wrap to the window instead of running off its edge: on a 1080 px
        # window, or with display scaling, Calibrate's one-line guide was
        # cut mid-sentence
        st = self._shell_status
        st.bind("<Configure>", lambda e: st.configure(
            wraplength=max(1, e.width - 4)), add="+")

    def _render_stepped_down(self, asked, shown):
        """The viewer drew `shown` instead of the Display quality asked for
        (not enough free memory for `asked`). The box keeps the request, so
        a smaller design gets it back; the line under it says what is on
        screen."""
        lbl = getattr(self, "_sim_render_note_lbl", None)
        if lbl is None or not shown:
            return
        # ...and how to still see the fibers: many stacked layers drawn as
        # lines fill the picture, but the first layers and a close view do not
        self._sim_render_note.set(
            "Showing %s: %s needs more free memory than this computer has "
            "now. To see single fibers, zoom in, or drag the timeline back "
            "to the first layers." % (shown, asked or "the quality above"))
        lbl.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _render_note_clear(self):
        lbl = getattr(self, "_sim_render_note_lbl", None)
        if lbl is not None:
            self._sim_render_note.set("")
            lbl.grid_remove()
        # the same note in the status line goes with it, or it names a
        # quality the box no longer reads
        st = getattr(self, "status", None)
        if st is not None and str(st.get()).startswith("Large design: showing"):
            st.set("")

    def _sim_render_chosen(self):
        """Display quality picked: re-open the simulation that is showing, at
        the new quality. Nothing open, nothing to do: the next open uses it."""
        panel = getattr(self, "_embed_panel", lambda _r: None)("unified")
        if panel is None or getattr(panel, "session", None) is None:
            return
        self.play_gcode(static=True)

    def _viewer_running_changed(self, role, running):
        """An embedded viewer opened or went idle (ViewerPanel._set_running)."""
        if role == "unified":
            self._sync_overlay_buttons()

    def _sync_overlay_buttons(self):
        """Grey the micrograph tools the Simulation page cannot use yet."""
        imp = getattr(self, "_overlay_import_btn", None)
        if imp is None:
            return
        panel = getattr(self, "_embed_panel", lambda _r: None)("unified")
        open_ = panel is not None and getattr(panel, "session", None) is not None
        has_img = (getattr(self, "_an_micro", {}) or {}).get("image") is not None
        for btn, ok in ((imp, open_),
                        (self._overlay_scale_btn, open_ and has_img)):
            try:
                btn.state(["!disabled"] if ok else ["disabled"])
            except tk.TclError:
                pass

    def _generate(self):
        """The bar's Generate (and Ctrl+G), from any page. A Design page that
        has never filled in builds its design anyway (`run` in
        gui_gcode_editor), so the first press builds and no page is
        switched."""
        self.run()

    def send_to_printer(self):
        """Send what Save would write. On the CAD page, in Design or Calibrate,
        that is the page's text (`_gce_send`, which speaks in the page's
        notice); anywhere else it is
        the last result, through the Machine link's own upload and its
        out-of-date check. What stops a send is on the Machine link page, so a
        printer that is not connected, or a result that is out of date, opens
        that page instead of naming a button on it from afar."""
        if self._gce_page_shown():
            self._gce_send()
            return
        if not getattr(self, "_ml_connected", False) \
                or getattr(self, "_ml_client", None) is None:
            self.status.set("Not sent: connect the Printer link to a printer, "
                            "then press Send again.")
            self._nav_select("machine_link")
            return
        if self._ml_stale_reason():
            self._ml_refresh_print_name()
            self.status.set("Not sent: the result is out of date.")
            self._nav_select("machine_link")
            return
        self._ml_upload(False)
        self.status.set(self.ml_conn_status.get())

    # ------------------------------------------------------------------ #
    # overrides of the classic glue (each calls super() where one exists)
    # ------------------------------------------------------------------ #
    def _build_sim_tab(self, nb):
        """Shell override of the physics hook: the printhead-sim controls mount into
        the Physics page's module-grid cards instead of a sub-tab of the scaffold
        notebook (`nb` is ignored). Called by the shared _build_scaffold_tab at the
        same point as in the classic UI, so self.scaf_vars already exists when
        _populate_sim_controls wires its per-scaffold speed traces.

        FOUR hosts, not one. The panel's controls belong to four different laws, so
        the grid splits them: the process knobs, the diameter mode, the fusion
        ON/OFF and the render switches each go to the card that owns them. All four
        bodies were created at page-build time and stashed in `_sim_hosts` (the
        "process" one is also still `_sim_host`); `_populate_sim_controls` makes the
        vars either way, which is why the badges can only be bound here."""
        hosts = dict(getattr(self, "_sim_hosts", None) or {})
        host = hosts.get("process") or getattr(self, "_sim_host", None)
        if host is None or not hasattr(self, "_populate_sim_controls"):
            return
        # NO columnconfigure(weight=...) on these hosts. `_field` grids its label
        # sticky="e" in column 0, so a weighted column 0 stretches to the pane and
        # right-aligns the label against its far edge: the coiling-wave amplitude
        # and wavelength ended up ~1400 px out, detached from the checkbox they
        # belong to.
        # The card body already stretches these frames with sticky="ew"; their
        # CONTENT should stay at the left.
        hosts["process"] = host
        self._populate_sim_controls(host, hosts=hosts)
        # The badges: a card whose module has an ON/OFF shows it, exactly as a
        # Disclosure did. Bound now because these vars did not exist when the
        # cards were registered -- the `stab.bind_status(self.stab_on)` precedent
        # on the scaffold page.
        # One per law switch. Export/Visualization has none: it carries no law.
        for attr, var_name in (("_phys_lag_card", "sim_physics_deposit"),
                               ("_phys_sag_card", "sim_sag_law"),
                               ("_phys_diameter_card", "sim_diam_law"),
                               ("_phys_fusion_card", "sim_physical_fusion")):
            card = getattr(self, attr, None)
            var = getattr(self, var_name, None)
            if card is not None and var is not None:
                card.bind_status(var)
        # the sim.* vars are only now traceable, so re-run the (idempotent) watch
        # installer and settle every tile's summary line
        installer = getattr(self, "_install_phys_summary_traces", None)
        if callable(installer):
            installer()
            self._refresh_phys_summaries()

    #: ...plus the modern-only Calibration page. Without it every viewer page
    #: answered `_active_mode` with whatever input tab preceded the Calibration
    #: one, and `gui_viewers._play_args` drew that tab's five-axis collector
    #: under a flat calibration slide.
    _REMEMBERED_MODES = gui_core._CoreMixin._REMEMBERED_MODES + ("calibration",)

    def _active_mode(self):
        """Shell pages that are not input modes (Home, Physics) must not retarget
        Generate -- the same rule the classic UI applies to the Machine tab and the
        viewer tabs (super() handles those). Calibrate is an input mode too, answered
        by `gui_gcode_calibrate._active_mode` from the CAD window
        ("calibration"): the bottom bar's Generate builds the target slide and Save
        g-code batch-exports every plate. It is a modern-only mode, so the shared 3-way
        run() switch never sees it -- run() intercepts first."""
        try:
            frame = self.nb.nametowidget(self.nb.select())
        except Exception:  # noqa: BLE001
            return self._last_input_mode
        if frame in self._noninput_frames:
            return self._last_input_mode
        return super()._active_mode()

    def run(self):
        """Shell override: Calibrate has its own generate (build the
        target slide's STL/.depo, feed the shared result flow); every other page
        keeps the classic 3-way run()."""
        if hasattr(self, "_summary_note_run"):
            self._summary_note_run()        # what this Generate was asked for
        if self._active_mode() == "calibration":
            self._sw_generate()
            return
        super().run()

    def save_gcode_as(self):
        """Shell override: a calibration print emits N g-code files (+ an optional
        CSV), which the single-file classic export can't express, so on the
        Calibration page the shared 'Save g-code as…' batch-writes every plate.
        Elsewhere unchanged."""
        if self._active_mode() == "calibration":
            self._sw_export_gcode_dialog()
            return
        super().save_gcode_as()

    def _refresh_result_controls(self):
        """super() points the result buttons at what exists; then name the
        Generate target explicitly so the button reads unambiguously from ANY
        page (the classic 'Generate' gives no clue what it will run)."""
        super()._refresh_result_controls()
        mode = self._active_mode()
        label = self._MODE_TARGETS.get(mode)
        if label:
            self.run_btn.configure(text=label)
        # On the Calibration page "Save g-code as…" batch-writes every slide
        # straight from the engine's texts (`_sw_export_gcode_dialog`), so it is
        # usable as soon as the preview has built a print -- not only after
        # Generate has built one slide's STL, which is what `last_gcode` tracks
        # and what greyed it out before. Every other page keeps that rule.
        btn = getattr(self, "save_gcode_btn", None)
        if btn is not None:
            if mode == "calibration":
                ready = getattr(self, "_sw_result", None) is not None
            else:
                ready = bool(getattr(self, "last_gcode", None))
            btn.configure(state="normal" if ready else "disabled")

    # ------------------------------------------------------------------ #
    # the Simulation page and the Log (the classic viewer tabs; same widget
    # names so gui_run / _sync_result_buttons work unchanged)
    # ------------------------------------------------------------------ #
    def _build_viewer_pages(self, pages):
        pad = self.pad
        self._viewer_frames = set()
        margin = gui_fonts.PAGE_MARGIN
        make_panel = getattr(self, "_make_embed_panel", None)
        # deposition render quality shared by the unified Viewer + the locked sim
        # (both drive the player's --render); a plain session var, not persisted.
        self.sim_render = tk.StringVar(value=RENDER_VAL2LBL["tube:12"])

        # --- Unified Viewer -------------------------------------------------#
        # The player launched --static: ONE 3-D scene that opens on the finished scaffold
        # (the STL view) and animates when un-paused. Deviation, the micrograph layer and
        # the simulation controls are in-scene modes of it: un-pausing is the locked
        # sim, and the rail's Live edit relaunches it as the live one.
        #
        # The viewer IS the page, edge to edge. The loaded file and the page's
        # own tools sit in a card under its rail, over the window buttons.
        if make_panel is not None:
            view = ttk.Frame(self.nb)
            self.nb.add(view, text="Simulation")
            self._viewer_frames.add(view)
            pages.append(("view", "simulation", "Simulation", view))
            view.columnconfigure(0, weight=1)
            view.rowconfigure(0, weight=1)
            self._unified_view_frame = view
            # NOT "then press Open viewer": a finished Generate brings the
            # Viewer up on its own (on page entry, or at once when the run ends
            # with this page on screen), so that would describe a button press
            # the user never has to make. The button stays -- it reopens a
            # viewer that was closed -- but it is not the way in.
            def idle_text():
                # closed with a result on disk is not "nothing yet": say how
                # to get it back instead of asking for a fresh Generate
                if (getattr(self, "proc", None) is not None
                        or getattr(self, "_sw_build_busy", False)):
                    return ("Generating.\n\nThe result opens here when "
                            "it is done.")
                g = getattr(self, "last_gcode", None)
                if g and os.path.isfile(g):
                    return ("The simulation is closed.\n\nPress Open "
                            "simulation to see the last result again.")
                return ("Nothing to simulate yet.\n\nGenerate a design "
                        "and it opens here.")
            panel = make_panel(view, "unified", idle_text,
                               lambda: self.play_gcode(static=True),
                               rail_tools=True)
            panel.grid(row=0, column=0, sticky="nsew")
            self._unified_panel = panel     # the tour points at its Play bar
            right = panel.rail_tools
            right.columnconfigure((0, 1), weight=1, uniform="tools")
            self.unified_status = tk.StringVar(value="No design generated yet.")
            # a live status, the same shape as G-code text
            gui_form.mark(ttk.Label(
                right, textvariable=self.unified_status,
                style="ViewerCardKey.TLabel", wraplength=300,
                justify="left"), "live").grid(
                row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
            disp_tip = ("How the open viewer draws the fibers. It changes "
                        "only the picture, never the exported files.\n\n"
                        + gui_tips.GLOSSARY.get("render quality", ""))
            quality = ttk.Frame(right, style="ViewerCard.TFrame")
            quality.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6))
            quality.columnconfigure(1, weight=1)
            dl = gui_form.mark(ttk.Label(quality, text="Display quality:",
                                         style="ViewerCard.TLabel"), "label")
            dl.grid(row=0, column=0, sticky="w")
            dq = gui_form.mark(ttk.Combobox(
                quality, textvariable=self.sim_render, state="readonly",
                width=14, values=[lbl for lbl, _ in RENDER_CHOICES]), "field")
            dq.grid(row=0, column=1, sticky="ew", padx=(6, 0))
            add_tip(dl, disp_tip)
            add_tip(dq, disp_tip)
            # what the viewer actually drew, when a large design made it
            # step down from the quality above (`_render_stepped_down`)
            self._sim_render_note = tk.StringVar(value="")
            self._sim_render_note_lbl = gui_form.mark(ttk.Label(
                quality, textvariable=self._sim_render_note,
                style="ViewerCard.TLabel", wraplength=300, justify="left"),
                "live")
            self.sim_render.trace_add(
                "write", lambda *_a: self._render_note_clear())
            # A new quality applies at once to the simulation that is open.
            # It used to wait for Open simulation, so a reader who picked
            # Fibers (fast) kept looking at the old picture and the old note.
            dq.bind("<<ComboboxSelected>>",
                    lambda _e: self._sim_render_chosen(), add="+")
            self.unified_view_btn = ttk.Button(
                right, text="Open simulation", style="ViewerBtn.TButton",
                command=lambda: self.play_gcode(static=True), state="disabled")
            self.unified_view_btn.grid(row=2, column=0, columnspan=2,
                                       sticky="ew")
            # micrograph overlay (physics): import / scale an image, pushed DOWN to the
            # open viewer as a live layer. File dialogs are naturally GUI-side; the
            # manipulation sliders appear in the viewer's sidebar.
            if hasattr(self, "_an_micro_load"):
                # greyed until they can act: Import needs an open simulation
                # to lay the image in, Scale needs an imported image
                self._overlay_import_btn = ttk.Button(
                    right, text="Import image\u2026", style="ViewerBtn.TButton",
                    command=self._unified_import_image, state="disabled")
                self._overlay_import_btn.grid(
                    row=3, column=0, sticky="ew", pady=(4, 0), padx=(0, 2))
                add_tip(self._overlay_import_btn,
                        "Lay a micrograph of the printed scaffold under the "
                        "simulated one, to compare them. Needs an open "
                        "simulation.")
                self._overlay_scale_btn = ttk.Button(
                    right, text="Scale image\u2026", style="ViewerBtn.TButton",
                    command=self._unified_scale_image, state="disabled")
                self._overlay_scale_btn.grid(
                    row=3, column=1, sticky="ew", pady=(4, 0), padx=(2, 0))
                add_tip(self._overlay_scale_btn,
                        "Set the imported image's true size: click both ends "
                        "of a length you know and type it in mm.")

        # --- Retired viewer pages --------------------------------------------#
        # The five separate viewer pages (Locked / Live simulation, STL, Micrograph,
        # Deviation) all FOLD into the ONE unified Viewer above. A few of their widgets
        # are still referenced by the shared _sync_result_buttons / _refresh_viewer_tabs
        # authority, so keep them alive on a hidden holder (never shown): that leaves the
        # shared code -- and the classic UI, which still builds real pages -- untouched.
        self.sim_status = tk.StringVar(value="No g-code generated yet.")
        self.stl_status = tk.StringVar(value="No STL generated yet.")
        self.stl_render = tk.StringVar(value=RENDER_VAL2LBL["wireframe"])
        self._legacy_holder = ttk.Frame(self.root)          # never gridded -> hidden
        self.play_btn = ttk.Button(self._legacy_holder, command=self.play_gcode,
                                   state="disabled")
        self.view_btn = ttk.Button(self._legacy_holder, command=self.view_stl,
                                   state="disabled")
        if hasattr(self, "_build_sim_controls"):
            self.play_live_btn = ttk.Button(
                self._legacy_holder, command=lambda: self.play_gcode(live=True),
                state="disabled")

        # --- G-code text ------------------------------------------------------#
        # Folded into the Design tab's G-code page when the editor is
        # built (it owns gcode_text and the buttons below by the same names).
        if hasattr(self, "_build_gcode_editor_page"):
            self._build_log_page(pages, margin)
            return
        gtxt = ttk.Frame(self.nb, padding=(margin, 8))
        self.nb.add(gtxt, text="G-code text")
        self._viewer_frames.add(gtxt)
        self._gcode_text_frame = gtxt
        pages.append(("view_gtext", "more", "G-code text", gtxt))
        bar = ttk.Frame(gtxt)
        bar.pack(fill="x")
        self.gcode_text_status = tk.StringVar(value="No g-code generated yet.")
        # the Viewer page's shape: the page title, and its live status beside
        # it in the grey a computed line takes (gui_form's "live")
        ttk.Label(bar, text="G-code text", style="Title.TLabel").pack(
            side="left")
        gui_form.mark(ttk.Label(bar, textvariable=self.gcode_text_status,
                                style="Hint.TLabel"), "live").pack(
            side="left", padx=(gui_fonts.SP["m"], 0), pady=(4, 0))
        self.gcode_copy_btn = ttk.Button(bar, text="Copy all",
                                         command=self._copy_gcode_text,
                                         state="disabled")
        self.gcode_copy_btn.pack(side="right", padx=4)
        self.view_gcode_btn = ttk.Button(bar, text="Refresh",
                                         command=self.view_gcode,
                                         state="disabled")
        self.view_gcode_btn.pack(side="right", padx=4)
        # jump the embedded Viewer to the frame that runs the clicked line
        self.gcode_find_btn = ttk.Button(bar, text="Find in simulation",
                                         command=self._find_in_sim,
                                         state="disabled")
        self.gcode_find_btn.pack(side="right", padx=4)
        body = ttk.Frame(gtxt, padding=(0, 6, 0, 0))
        body.pack(fill="both", expand=True)
        self._make_gcode_textview(body, "MewMono")
        self._build_log_page(pages, margin)

    def _build_log_page(self, pages, margin):
        # --- Log ---------------------------------------------------------------#
        logtab = ttk.Frame(self.nb, padding=(margin, 8))
        self.nb.add(logtab, text="Log")
        self._viewer_frames.add(logtab)
        self._log_frame = logtab
        pages.append(("log", "more", "Log", logtab))
        logbar = ttk.Frame(logtab)
        logbar.pack(fill="x")
        ttk.Label(logbar, text="Log", style="Title.TLabel").pack(side="left")
        ttk.Button(logbar, text="Copy log", command=self._copy_log_text).pack(
            side="right", padx=4)
        ttk.Label(logtab, text="What each Generate, save and send did, step "
                               "by step. Copy it into a bug report when "
                               "something goes wrong.",
                  style="Hint.TLabel").pack(anchor="w", pady=(2, 0))
        logbody = ttk.Frame(logtab, padding=(0, 6, 0, 0))
        logbody.pack(fill="both", expand=True)
        logbody.rowconfigure(0, weight=1)
        logbody.columnconfigure(0, weight=1)
        self.log = tk.Text(logbody, wrap="word", state="disabled",
                           font="MewMono", background="#111",
                           foreground="#ddd")
        self.log.grid(row=0, column=0, sticky="nsew")
        # The empty state (direction page §6): what will appear, and what makes
        # it appear. Tagged so `append_log` drops it on the first real line
        # and Copy log never copies it.
        self.log.configure(state="normal")
        self.log.insert("1.0", "Nothing logged yet. Press Generate, save or "
                               "send, and each step appears here.", "empty")
        self.log.tag_configure("empty", foreground="#8a8f96")
        self.log.configure(state="disabled")
        logsb = ttk.Scrollbar(logbody, command=self.log.yview)
        logsb.grid(row=0, column=1, sticky="ns")
        self.log["yscrollcommand"] = logsb.set

    def _copy_log_text(self):
        try:
            if self.log.tag_ranges("empty"):
                self.status.set("Nothing to copy yet: the log is empty.")
                return
            self._copy_text(self.log.get("1.0", "end-1c"))
            self.status.set("Log copied to clipboard.")
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    # keyboard
    # ------------------------------------------------------------------ #
    def _bind_shell_keys(self):
        r = self.root
        r.bind("<Control-g>", self._kb_generate)
        r.bind("<Escape>", self._kb_escape)
        r.bind("<Control-d>", self._kb_dark)
        # The control finder (gui_finder). Bound on the "all" tag AND on the
        # text-entry classes, which is not belt-and-braces: Tk's bindtag order
        # is widget, class, toplevel, all, and Entry's CLASS binding already
        # owns <Control-f> as "move the insert cursor one character right". A
        # toplevel or all-tag binding alone therefore opens the finder but lets
        # the cursor jump first, and "where do I set X" gets asked from inside a
        # form more often than from a blank page. Overriding the class binding
        # makes Ctrl+F mean Find everywhere, as it does in every other app.
        if hasattr(self, "open_finder"):
            r.bind_all("<Control-f>", self.open_finder)
            for klass in ("Entry", "TEntry", "TCombobox", "TSpinbox",
                          "Spinbox", "Text", "Listbox"):
                r.bind_class(klass, "<Control-f>", self.open_finder)
        for i, tab in enumerate(self._nav_btns, start=1):
            r.bind(f"<Control-Key-{i}>",
                   lambda _e, t=tab: self._nav_open(t))

    def _kb_generate(self, _event=None):
        try:
            if str(self.run_btn["state"]) != "disabled":
                self._generate()
        except tk.TclError:
            pass

    def _kb_escape(self, _event=None):
        """Esc closes the Settings panel if it is open, and cancels a run only
        when it is not: dismissing a panel must never cancel a job."""
        if getattr(self, "_settings_open", lambda: False)():
            self.close_settings()
            return "break"
        self.cancel()

    def _kb_dark(self, _event=None):
        try:
            self.dark_mode.set(not self.dark_mode.get())
            self._toggle_theme()
        except tk.TclError:
            pass
