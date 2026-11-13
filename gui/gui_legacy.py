#!/usr/bin/env python3
"""gui_legacy.py -- MODERN-UI ONLY: the Legacy UI, under More on the top row.

The sidebar the shell had before the top row and the CAD window, ported from
gui_shell's `_build_sidebar`, over the SAME pages. Nothing is built twice: the
pages, their variables and the bottom row stay as they are, and the switch only
trades the top row for the sidebar. Exit legacy UI sits at the sidebar's head,
so it is on screen on every page. The switch lasts for the session.

G-code text is the CAD page itself with everything but its text put away
(`_gce_text_only`), so what is typed there is the document Generate, Save and
Simulation already read.

A calibration slide exists only in the CAD window (gui_gcode_calibrate), so
Calibration opens it there, whole."""

from gui_common import *

from tkinter import font as tkfont

import gui_tips
import gui_widgets


class _LegacyMixin:
    SIDEBAR_W = 192
    # (section, ((page id, its name), ...)), as the sidebar listed them
    LEGACY_NAV = ((None, (("home", "Home"),)),
                  ("Setup", (("machine", "Machine & collector"),
                             ("physics", "Physics"))),
                  ("Design", (("scaffold", "Scaffold designer"),
                              ("calibration", "Calibration"))),
                  ("Results", (("history", "Print history"),
                               ("view", "Viewer"),
                               ("gcode", "G-code text"),
                               ("log", "Log"))),
                  ("Print", (("machine_link", "Machine link"),)))

    _legacy_on = False

    def _finish_top_row(self, pages):
        super()._finish_top_row(pages)
        self._nav_more_menu.add_separator()
        self._nav_more_menu.add_command(label="Legacy UI",
                                        command=self._legacy_enter)
        self._build_legacy_sidebar()

    def _build_legacy_sidebar(self):
        sidebar = ttk.Frame(self.root, style="Sidebar.TFrame",
                            width=self.SIDEBAR_W)
        sidebar.grid_propagate(False)
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(1, weight=1)
        self._legacy_sidebar = sidebar

        head = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=(6, 12, 6, 4))
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text="MEWracle", style="Wordmark.TLabel").pack(
            anchor="w", padx=8, pady=(0, 6))
        self._legacy_back = ttk.Button(head, text="←  Exit legacy UI",
                                       style="Nav.TButton",
                                       command=self._legacy_leave)
        self._legacy_back.pack(fill="x")
        gui_tips.add_tip(self._legacy_back, "Back to the top row.")

        body = ttk.Frame(sidebar, style="Sidebar.TFrame")
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        self._legacy_btns = {}      # page id -> its row
        labels = []
        r = 0
        for sec, rows in self.LEGACY_NAV:
            here = [(p, l) for p, l in rows if p in self._page_frames
                    or (p == "calibration" and hasattr(self, "_cal_enter"))]
            if sec is not None and here:
                ttk.Label(body, text=sec.upper(),
                          style="SidebarSection.TLabel").grid(
                    row=r, column=0, sticky="w", padx=14, pady=(10, 2))
                r += 1
            for pid, label in here:
                btn = ttk.Button(body, text=label, style="Nav.TButton",
                                 command=lambda p=pid: self._nav_select(p))
                btn.grid(row=r, column=0, sticky="ew", padx=6, pady=1)
                r += 1
                self._legacy_btns[pid] = btn
                labels.append(label)

        # the widest row MEASURED in the rendered font, so a display-scaled
        # machine never clips "Machine & collector"
        try:
            font = tkfont.nametofont("MewNav")
            widest = max(font.measure(t) for t in labels
                         + [self._legacy_back.cget("text")])
            sidebar.configure(width=max(self.SIDEBAR_W,
                                        widest + 2 * 12 + 2 * 6 + 22))
        except tk.TclError:
            pass

        # what Generate builds and whether the last result matches it: the top
        # row's design line (gui_summary), on the same variables
        if hasattr(self, "_sum_target"):
            summ = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=(14, 8))
            summ.grid(row=2, column=0, sticky="ew")
            for var in (self._sum_target, self._sum_state):
                ttk.Label(summ, textvariable=var, style="SidebarHint.TLabel",
                          wraplength=self.SIDEBAR_W - 28, justify="left").pack(
                    anchor="w")

        foot = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=(6, 4, 6, 6))
        foot.grid(row=3, column=0, sticky="ew")
        self._legacy_gear = None
        if hasattr(self, "_build_settings_button"):
            self._legacy_gear = ttk.Button(foot, text="⚙  Settings",
                                           style="Nav.TButton",
                                           command=self.toggle_settings)
            self._legacy_gear.pack(fill="x")

    def _legacy_enter(self):
        self._legacy_set(True)

    def _legacy_leave(self):
        self._legacy_set(False)

    def _legacy_set(self, on):
        if on == self._legacy_on:
            return
        if hasattr(self, "close_settings"):
            self.close_settings()
        self._legacy_on = on
        if on:
            self._nav_wrap.grid_remove()
            self._legacy_sidebar.pack(side="left", fill="y",
                                      before=self._shell_outer)
            self._legacy_top_gear = self._settings_btn
            self._settings_btn = self._legacy_gear or self._settings_btn
        else:
            self._legacy_sidebar.pack_forget()
            self._nav_wrap.grid()
            self._settings_btn = self._legacy_top_gear
        self._nav_refresh()
        if getattr(self, "gce", None) is not None:
            self._gce_sync_text_only()
            self._gce_full_window(self._gce_page_shown())

    def _nav_refresh(self):
        """The sidebar lights the page on top; the top row's strip stays out."""
        super()._nav_refresh()
        if not self._legacy_on:
            return
        self._nav_strip.grid_remove()
        cur = self._nav_pid_of.get(self.nb.select())
        if cur == "gcode" and self._cal_on:
            cur = "calibration"
        for pid, btn in self._legacy_btns.items():
            btn.configure(style="NavSel.TButton" if pid == cur
                          else "Nav.TButton")
        if getattr(self, "gce", None) is not None:
            self._gce_sync_text_only()
            self._gce_full_window(self._gce_page_shown())

    def start_tour(self, _event=None, steps=None, on_step=None):
        """The tour walks the top row."""
        self._legacy_leave()
        return super().start_tour(_event, steps, on_step)
