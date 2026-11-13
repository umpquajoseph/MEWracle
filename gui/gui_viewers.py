#!/usr/bin/env python3
"""gui_viewers.py -- Viewers mixin: the result viewer tabs (embedded G-code text, windowed
STL + player) and the View/Play launchers (the bounds/colour args they forward).

Part of the gcode_to_fibers_gui split; see gui_common.py."""
from gui_common import *


# Poll cadence (ms) for the main-thread waiter that delivers the on-demand exact
# mesh build's completion; mirrors gui_sweeps.SWEEP_POLL_MS.
EXACT_POLL_MS = 90
#: an exact STL whose files would pass this many bytes is not built until the
#: user says so: the converter counts the triangles first and writes nothing
#: (gcode_to_fibers --max-stl-bytes). 1 GB is about 20 million triangles,
#: past what common STL viewers and slicers open comfortably.
STL_ASK_BYTES = 1.0e9


class _ViewerMixin:
    def _build_viewer_tabs(self):
        """Three result tabs in the main notebook: Simulation and STL viewer each
        open a separate VTK window (kept out-of-process so a heavy mesh can't
        freeze this one), and G-code text shows the last g-code inline. The
        View/Play buttons created here are the same widgets the run machinery
        enables/disables (via _sync_result_buttons / _refresh_result_controls)."""
        pad = self.pad
        self._viewer_frames = set()

        # Simulation (animated player, own window)
        sim = ttk.Frame(self.nb, padding=16)
        self.nb.add(sim, text="  Simulation  ")
        self._viewer_frames.add(sim)
        # kept so a finished run can land on this tab (the animated toolpath is
        # the default landing view after a generation; see gui_run._on_finished)
        self._sim_view_frame = sim
        ttk.Label(sim, text="3D toolpath simulation",
                  font=("", 11, "bold")).pack(anchor="w")
        ttk.Label(sim, justify="left", foreground="#777", wraplength=560,
                  text="Animates the printhead along the last generated g-code. "
                       "Opens in its own window so VTK's 3D view can't freeze or "
                       "crash this one.").pack(anchor="w", pady=(4, 10))
        self.sim_status = tk.StringVar(value="No g-code generated yet.")
        ttk.Label(sim, textvariable=self.sim_status,
                  foreground="#777").pack(anchor="w", pady=(0, 8))
        # Deposition render quality (locked sim), same choices as the STL viewer.
        # Wireframe / tubes render the sagged centrelines; Exact builds the full
        # mesh. Chosen before opening -- the deposition is built when you press D,
        # and the live-adjust march keeps its own incremental tubes.
        # The default is the SMOOTH tube because a 6-facet tube renders crossings
        # with a visible float: a hexagon's flats sit inside the true contact
        # radius. The player also floors any tube at DEPO_FACES, so even a saved
        # "fast" never floats.
        self.sim_render = tk.StringVar(value=RENDER_VAL2LBL["tube:12"])
        srow = ttk.Frame(sim)
        srow.pack(anchor="w", pady=(0, 8))
        ttk.Label(srow, text="Deposition quality:").pack(side="left")
        ttk.Combobox(srow, textvariable=self.sim_render, state="readonly", width=18,
                     values=[lbl for lbl, _ in RENDER_CHOICES]).pack(
            side="left", padx=(6, 0))
        self.play_btn = ttk.Button(sim, text="Open simulation window",
                                   command=self.play_gcode, state="disabled")
        self.play_btn.pack(anchor="w")
        # hook: the real-time-adjustment launcher + its note are added by gui_sim,
        # injected here so this tab carries no sim code inline -- the same seam as
        # _build_sim_tab / _sim_play_args. The button it creates (self.play_live_btn) is toggled
        # alongside play_btn in _sync_result_buttons.
        self._build_sim_extra(sim)

        # STL viewer (own window)
        stlf = ttk.Frame(self.nb, padding=16)
        self.nb.add(stlf, text="  STL viewer  ")
        self._viewer_frames.add(stlf)
        self._stl_view_frame = stlf
        ttk.Label(stlf, text="STL viewer",
                  font=("", 11, "bold")).pack(anchor="w")
        ttk.Label(stlf, justify="left", foreground="#777", wraplength=560,
                  text="Shows the last generated STL (orbit / pan / zoom). Opens "
                       "in its own window so a large mesh can't freeze or crash "
                       "this one.").pack(anchor="w", pady=(4, 10))
        self.stl_status = tk.StringVar(value="No STL generated yet.")
        ttk.Label(stlf, textvariable=self.stl_status,
                  foreground="#777").pack(anchor="w", pady=(0, 8))
        # View quality, chosen before opening: Wireframe / Tubes (centrelines from
        # the .depo cache -- a fast glance that never reads the multi-GB mesh) or
        # Exact mesh (the STL the run wrote). Re-open to switch.
        self.stl_render = tk.StringVar(value=RENDER_VAL2LBL["wireframe"])
        qrow = ttk.Frame(stlf)
        qrow.pack(anchor="w", pady=(0, 8))
        ttk.Label(qrow, text="View quality:").pack(side="left")
        ttk.Combobox(qrow, textvariable=self.stl_render, state="readonly", width=18,
                     values=[lbl for lbl, _ in RENDER_CHOICES]).pack(
            side="left", padx=(6, 0))
        # its state is kept by _refresh_result_controls
        self.view_btn = ttk.Button(stlf, text="View STL", command=self.view_stl,
                                   state="disabled")
        self.view_btn.pack(anchor="w")

        # G-code text (embedded)
        gtxt = ttk.Frame(self.nb, padding=8)
        self.nb.add(gtxt, text="  G-code text  ")
        self._viewer_frames.add(gtxt)
        self._gcode_text_frame = gtxt
        bar = ttk.Frame(gtxt)
        bar.pack(fill="x")
        self.gcode_text_status = tk.StringVar(value="No g-code generated yet.")
        ttk.Label(bar, textvariable=self.gcode_text_status).pack(side="left")
        self.gcode_copy_btn = ttk.Button(bar, text="Copy all",
                                         command=self._copy_gcode_text,
                                         state="disabled")
        self.gcode_copy_btn.pack(side="right", padx=4)
        self.view_gcode_btn = ttk.Button(bar, text="Refresh",
                                         command=self.view_gcode, state="disabled")
        self.view_gcode_btn.pack(side="right", padx=4)
        # jump the sim to the frame that runs the clicked line
        self.gcode_find_btn = ttk.Button(bar, text="Find in simulation",
                                         command=self._find_in_sim,
                                         state="disabled")
        self.gcode_find_btn.pack(side="right", padx=4)
        body = ttk.Frame(gtxt, padding=(0, 6, 0, 0))
        body.pack(fill="both", expand=True)
        self._make_gcode_textview(body, ("Consolas", 9))

        # Log / debug output (embedded) -- the last tab. This was a fixed strip
        # pinned under the notebook; living in its own scrolling tab means a short
        # or display-scaled window can never push it off the bottom edge. It is a
        # viewer (non-input) tab, so selecting it leaves Generate targeting the
        # last input mode. self.log keeps its name, so the run machinery in
        # gui_run (which writes to self.log) is untouched.
        logtab = ttk.Frame(self.nb, padding=8)
        self.nb.add(logtab, text="  Log  ")
        self._viewer_frames.add(logtab)
        self._log_frame = logtab
        logtab.rowconfigure(0, weight=1)
        logtab.columnconfigure(0, weight=1)
        self.log = tk.Text(logtab, wrap="word", state="disabled",
                           font=("Consolas", 9), background="#111",
                           foreground="#ddd")
        self.log.grid(row=0, column=0, sticky="nsew")
        logsb = ttk.Scrollbar(logtab, command=self.log.yview)
        logsb.grid(row=0, column=1, sticky="ns")
        self.log["yscrollcommand"] = logsb.set

    def _make_gcode_textview(self, body, font):
        """Build the embedded g-code text view: a synced line-number gutter + the
        read-only text + scrollbars, with click-to-select-a-line for "Find in
        simulation". Shared by the classic (this file) and modern (gui_shell)
        G-code text tabs so both get the gutter and the same behaviour. Sets
        self.gcode_text and self.gcode_gutter; `body` is an empty frame the caller
        owns. `font` is the tab's mono font (differs classic vs modern)."""
        body.rowconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)          # the text stretches, not the gutter
        gutter = tk.Text(body, width=5, wrap="none", font=font,
                         background="#0b0b0b", foreground="#666", state="disabled",
                         takefocus=0, cursor="arrow", borderwidth=0,
                         highlightthickness=0)
        gutter.grid(row=0, column=0, sticky="ns")
        text = tk.Text(body, wrap="none", font=font, background="#111",
                       foreground="#ddd", state="disabled", borderwidth=0,
                       highlightthickness=0)
        text.grid(row=0, column=1, sticky="nsew")

        def _yview(*a):                            # scrollbar drives text + gutter
            text.yview(*a)
            gutter.yview(*a)
        sb = ttk.Scrollbar(body, command=_yview)
        sb.grid(row=0, column=2, sticky="ns")

        def _on_yscroll(lo, hi):                   # any text scroll re-syncs the gutter
            sb.set(lo, hi)
            gutter.yview_moveto(lo)
        text["yscrollcommand"] = _on_yscroll
        sbx = ttk.Scrollbar(body, orient="horizontal", command=text.xview)
        sbx.grid(row=1, column=1, sticky="ew")
        text["xscrollcommand"] = sbx.set

        def _wheel(ev):                            # wheel over the narrow gutter, too
            text.yview_scroll(int(-1 * (ev.delta / 120)), "units")
            gutter.yview_moveto(text.yview()[0])
            return "break"
        gutter.bind("<MouseWheel>", _wheel)
        # click a line to select it (works on the read-only widget); the highlight
        # tag marks the pick that "Find in simulation" will jump to
        text.tag_configure("findsel", background="#2d4a6b")
        text.bind("<Button-1>", self._on_gcode_line_click)
        self.gcode_text = text
        self.gcode_gutter = gutter
        self._gcode_selected_line = None

    def _set_gcode_text(self, text):
        """Replace the embedded g-code viewer's contents (read-only widget), then
        renumber the gutter to match."""
        self.gcode_text.configure(state="normal")
        self.gcode_text.delete("1.0", "end")
        if text:
            self.gcode_text.insert("1.0", text)
        self.gcode_text.configure(state="disabled")
        self._populate_gcode_gutter()

    def _populate_gcode_gutter(self):
        """Fill the line-number gutter to match the g-code text's line count and
        clear any stale selection. No-op if the gutter isn't built (defensive)."""
        g = getattr(self, "gcode_gutter", None)
        if g is None:
            return
        content = self.gcode_text.get("1.0", "end-1c")
        n = int(self.gcode_text.index("end-1c").split(".")[0]) if content else 0
        nums = "\n".join(str(i) for i in range(1, n + 1))
        g.configure(state="normal")
        g.delete("1.0", "end")
        if nums:
            g.insert("1.0", nums)
        g.configure(state="disabled")
        g.yview_moveto(self.gcode_text.yview()[0])
        # a fresh document invalidates the previous line pick
        self._gcode_selected_line = None
        self.gcode_text.tag_remove("findsel", "1.0", "end")

    def _on_gcode_line_click(self, event):
        """Record + highlight the clicked g-code line so "Find in simulation" knows
        which line to jump to. Returns None so the default binding still runs."""
        idx = self.gcode_text.index(f"@{event.x},{event.y}")
        line = int(idx.split(".")[0])
        self._gcode_selected_line = line
        self.gcode_text.tag_remove("findsel", "1.0", "end")
        self.gcode_text.tag_add("findsel", f"{line}.0", f"{line}.0 lineend+1c")
        return None

    def _find_in_sim(self):
        """Open the simulation PAUSED at the frame that executes the selected g-code
        line (--goto-line). Uses the clicked line, or line 1 if none was picked. In
        the modern shell _spawn_player routes this to the embedded unified Viewer;
        the classic UI opens a window."""
        if not (self.last_gcode and os.path.isfile(self.last_gcode)):
            self._refuse("Nothing to simulate yet. Press Generate first.")
            return
        args = self._player_args(static=True)
        if args is None:
            self._refuse("Nothing to simulate yet. Press Generate first.")
            return
        line = int(getattr(self, "_gcode_selected_line", None) or 1)
        args += ["--goto-line", str(line)]
        self._spawn_player(args, static=True)

    def _load_gcode_text(self):
        """Load self.last_gcode into the embedded viewer (no tab switch). Updates
        the status line and the Copy button. Safe to call when nothing exists."""
        path = self.last_gcode
        if path and os.path.isfile(path):
            try:
                with open(path, "r", errors="ignore") as fh:
                    text = fh.read()
            except OSError as exc:
                self._set_gcode_text("")
                self.gcode_text_status.set(f"Could not read g-code: {exc}")
                self._set_copy_btn_state("disabled")
                return
            # travel-as-G1: show the swapped g-code in the tab (scaffold output
            # only; self-gated). Purely display -- the file on disk stays G0.
            text = self._gcode_for_display_export(text)
            self._set_gcode_text(text)
            self.gcode_text_status.set(
                f"{os.path.basename(path)}  ({text.count(chr(10))} lines)")
            self._set_copy_btn_state("normal")
            self._set_find_btn_state("normal")
        else:
            self._set_gcode_text("")
            self.gcode_text_status.set("No g-code generated yet.")
            self._set_copy_btn_state("disabled")
            self._set_find_btn_state("disabled")

    def _set_copy_btn_state(self, state):
        """The old g-code tab's 'Copy all'; the G-code page has none, so a
        build without it is a no-op."""
        btn = getattr(self, "gcode_copy_btn", None)
        if btn is not None:
            btn.configure(state=state)

    def _set_find_btn_state(self, state):
        """Enable/disable the 'Find in simulation' button (both g-code tabs create
        it; guarded so a build/tab without it is a no-op)."""
        btn = getattr(self, "gcode_find_btn", None)
        if btn is not None:
            btn.configure(state=state)

    def _copy_gcode_text(self):
        self._copy_text(self.gcode_text.get("1.0", "end-1c"))
        self.status.set("G-code copied to clipboard.")

    def _refresh_gcode_display_on_toggle(self):
        """Re-render the embedded g-code text when the travel-as-G1 box flips, so
        the shown g-code matches the checkbox without a regenerate. Display only --
        the geometry pipeline ignores the var. No-op before the viewer exists or
        during a bulk preset load."""
        if getattr(self, "_loading_preset", False):
            return
        if hasattr(self, "gcode_text"):
            self._load_gcode_text()

    def _refresh_viewer_tabs(self):
        """Update the viewer tabs' status lines and reload the embedded g-code
        text. Called when a viewer tab is shown and after a run finishes."""
        has_s = bool(self.last_stl and os.path.isfile(self.last_stl))
        has_g = bool(self.last_gcode and os.path.isfile(self.last_gcode))
        self.stl_status.set(
            os.path.basename(self.last_stl) if has_s else "No STL generated yet.")
        self.sim_status.set(
            os.path.basename(self.last_gcode) if has_g
            else "No g-code generated yet.")
        # the modern shell's unified Viewer status; getattr keeps classic
        ustat = getattr(self, "unified_status", None)
        if ustat is not None:
            # the name Save offers and Send stores, not the scratch file's
            # ("edited.gcode", "sweep_print.gcode"): it is one file to the
            # person watching it, so it has one name
            name = getattr(self, "_ml_default_upload_name", None)
            try:
                shown = name() if (has_g and callable(name)) else None
            except Exception:  # noqa: BLE001 -- a label, never a blocker
                shown = None
            text = ((shown or os.path.basename(self.last_gcode)) if has_g
                    else "No design generated yet.")
            # a file that sets its own nozzle temperature is simulated at it
            # (gui_sim._sim_temperature_arg), and the panel says so
            note_fn = getattr(self, "_sim_temperature_note", None)
            note = (note_fn(self.last_gcode)
                    if has_g and callable(note_fn) else "")
            ustat.set(text + ("\n" + note if note else ""))
        self._load_gcode_text()

    def _sync_result_buttons(self):
        """Match the View/Play buttons to what actually exists on disk. Pointers
        to files that have since been deleted (by Save/Delete, or removed outside
        the app) are cleared so their buttons grey out instead of acting on
        nothing. Called when a run finishes and after Save/Delete settles the
        generated files."""
        if not (self.last_gcode and os.path.isfile(self.last_gcode)):
            self.last_gcode = None
        if not (self.last_stl and os.path.isfile(self.last_stl)):
            self.last_stl = None
        # the .depo cache (sagged centrelines) backs the wireframe/tube views and
        # the on-demand exact build, so track it like the STL
        if not (getattr(self, "last_depo", None) and os.path.isfile(self.last_depo)):
            self.last_depo = None
        gco_state = "normal" if self.last_gcode else "disabled"
        self.view_gcode_btn.configure(state=gco_state)
        self.play_btn.configure(state=gco_state)
        # the real-time-adjustment launcher (created by gui_sim's _build_sim_extra)
        # toggles with the plain one; getattr tolerates its absence
        live_btn = getattr(self, "play_live_btn", None)
        if live_btn is not None:
            live_btn.configure(state=gco_state)
        # the modern shell's unified Viewer launch; getattr keeps the classic
        # UI, which has no such button, working unchanged
        uni_btn = getattr(self, "unified_view_btn", None)
        if uni_btn is not None:
            uni_btn.configure(state=gco_state)
        # "Save g-code as" exports whatever last_gcode points at (the generated
        # scaffold g-code, or the g-code tab's input)
        self.save_gcode_btn.configure(state=gco_state)
        # the result buttons (View/Save STL) key off last_stl / last_depo,
        # which we've just reconciled
        self._refresh_result_controls()
        # keep the viewer tabs' statuses + embedded g-code text in sync too
        self._refresh_viewer_tabs()

    def _invalidate_deposited_result(self):
        """Discard the cached exact STL + .depo when a setting that changes the
        DEPOSITED geometry is flipped (the travel-render toggle), so an export
        can't save a mesh that no longer matches the form. Called from the
        `s_travel_in_stl` write-trace: with the box now unticked a previously
        generated travel mesh would still ride in `last_stl`/`last_depo`, and
        "Save STL as" copies the last result verbatim -- so it must be cleared,
        greying the View/Save-STL buttons until the next Generate rebuilds them.

        The g-code is unaffected (render-travel is a convert-time choice, not a
        g-code one), so `last_gcode` is kept and the g-code View/Save/Play stay
        live. Exempt during construction (before the run-state attributes exist)
        and during a bulk preset load (`_loading_preset`): a preset load is not a
        user toggle, and any result it might invalidate is reconciled by the
        preset apply path itself."""
        if not hasattr(self, "last_stl"):
            return                          # trace fired before __init__ set state
        if getattr(self, "_loading_preset", False):
            return                          # bulk preset apply, not a user toggle
        if self.last_stl is None and getattr(self, "last_depo", None) is None:
            return                          # nothing generated -> nothing to discard
        self.last_stl = None
        self.last_depo = None
        # refresh the context-aware result buttons off the cleared pointers, if the
        # viewer chrome is built yet (it is, by the time a user can toggle the box)
        if hasattr(self, "save_gcode_btn"):
            self._sync_result_buttons()

    def _invalidate_order_result(self):
        """Discard the cached run when the Export "Deposition order" control is
        flipped. Unlike `_invalidate_deposited_result`, this ALSO clears
        `last_gcode`: "by layer height" re-orders the emitted g-code itself (every
        array copy sorted by Z), so a previously generated g-code no longer matches
        the form either -- "Save g-code as" / Play would otherwise export the stale
        order. The user must Generate again. Exempt during construction and a bulk
        preset load, like `_invalidate_deposited_result`."""
        if not hasattr(self, "last_stl"):
            return                          # trace fired before __init__ set state
        if getattr(self, "_loading_preset", False):
            return                          # bulk preset apply, not a user toggle
        if (self.last_stl is None and getattr(self, "last_depo", None) is None
                and getattr(self, "last_gcode", None) is None):
            return                          # nothing generated -> nothing to discard
        self.last_stl = None
        self.last_depo = None
        self.last_gcode = None
        if hasattr(self, "save_gcode_btn"):
            self._sync_result_buttons()
        if hasattr(self, "stl_status"):
            self._refresh_viewer_tabs()

    def view_stl(self):
        """Open the STL viewer at the chosen View quality. Wireframe / Tubes render
        the sagged centrelines from the .depo cache (no mesh read -- works even when
        the run was a preview that skipped the mesh build). 'Exact mesh' shows the
        real STL, building it on demand from the cache (no re-solve) if a preview
        was generated."""
        render = RENDER_LBL2VAL.get(self.stl_render.get(), "wireframe")
        if render == "exact":
            if self.last_stl and os.path.isfile(self.last_stl):
                self._launch_stl_viewer(self.last_stl, "exact")
            elif self.last_depo and os.path.isfile(self.last_depo):
                self._build_exact_stl(
                    then=lambda p: self._launch_stl_viewer(p, "exact"))
            else:
                self._refuse("No STL yet. Press Generate with an STL in the "
                             "export, then view it.")
            return
        # wireframe / tube: render the .depo centrelines (no mesh read). Hand the
        # viewer the STL PATH (real, or the would-be path beside the cache) so it
        # finds the .depo; the source g-code (for travel hops) and live colours ride
        # along. The machine-bounds box stays the player's, not sent here.
        if self.last_depo and os.path.isfile(self.last_depo):
            stl_path = self.last_stl or (self._depo_stem() + ".stl")
            self._launch_stl_viewer(stl_path, render)
        elif self.last_stl and os.path.isfile(self.last_stl):
            self._launch_stl_viewer(self.last_stl, render)   # no cache -> mesh fallback
        else:
            self._refuse("Nothing to view yet. Press Generate first.")

    def _depo_stem(self):
        """The output stem the .depo cache belongs to -- last_depo with the FULL
        `.depo.npz` suffix stripped (os.path.splitext only drops `.npz`, leaving a
        stray `.depo`, which would make the viewer look for `<stem>.depo.depo.npz`
        and silently fail to open). The sibling STL is `<stem>.stl`."""
        d = self.last_depo
        if not d:
            return None
        if d.endswith(DEPO_CACHE_EXT):
            return d[:-len(DEPO_CACHE_EXT)]
        return os.path.splitext(d)[0]

    def _launch_stl_viewer(self, path, render):
        """Open stl_viewer on `path` at `render` quality, in its own window (own
        process, so VTK's loop stays clear of Tk's).

        CLASSIC (developer) UI ONLY, and deprecated with it -- see stl_viewer's
        module docstring. The modern shell folded STL into the unified Viewer at
        U2 and never reaches here: its "View STL" button is built on gui_shell's
        never-gridded `_legacy_holder`.
        Add viewer features to the unified Viewer, not here."""
        args = worker_cmd("worker:viewer", path, unbuffered=False)
        if self.last_gcode and os.path.isfile(self.last_gcode):
            args.append(self.last_gcode)
        args += self._viewer_color_args()
        args += ["--render", render]
        self._spawn_stl_viewer(args, path)

    def _spawn_stl_viewer(self, args, path):
        """The windowed launch (the classic default; see _launch_stl_viewer)."""
        try:
            subprocess.Popen(args, cwd=HERE)
        except Exception as exc:  # noqa: BLE001
            self._refuse('Could not open the viewer: %s. Try again, or restart the app if it keeps failing.' % exc)

    def _fiber_diam_faces(self):
        """(diameter, faces) parsed from the run's fiber params, with fallbacks --
        the radius + facet count for an on-demand exact mesh build."""
        params = self.last_fiber_params or []
        diam, faces = 0.02, 12
        for i, a in enumerate(params):
            if a == "--diameter" and i + 1 < len(params):
                try:
                    diam = float(params[i + 1])
                except ValueError:
                    pass
            elif a == "--faces" and i + 1 < len(params):
                try:
                    faces = int(params[i + 1])
                except ValueError:
                    pass
        return diam, faces

    def _fiber_mesh_tol(self):
        """The --mesh-tolerance an on-demand exact mesh must be built at: the one
        the run's VIEWER is already using.

        Read from the frozen fiber params, not the live "Mesh detail" combo, so a
        build started after the combo moved still matches the view it will be
        compared against. Two rules mirror gcode_player exactly, because matching
        it IS the requirement:

          * absent -> 0.0, the player's own CLI default. Leaving the flag off the
            converter instead took ITS default of 1.0, which silently decimated an
            STL the user had asked to be "Full (largest file)".
          * --no-sag -> 0.0, because neither side thins that mode (see
            gcode_to_fibers.main and build_deposition_mesh)."""
        params = self.last_fiber_params or []
        if "--no-sag" in params:
            return 0.0
        tol = 0.0
        for i, a in enumerate(params):
            if a == "--mesh-tolerance" and i + 1 < len(params):
                try:
                    tol = float(params[i + 1])
                except ValueError:
                    pass
        return tol

    def _build_exact_stl(self, then=None, ask=True):
        """Build the exact STL from the .depo cache (converter --from-depo: tube +
        write, NO sag re-solve), off the Tk thread, then call `then(stl_path)`. Used
        when a preview run skipped the mesh but the exact mesh is now needed (Export,
        or viewing 'Exact'). Sets last_stl on success; falls back gracefully.

        With `ask`, the converter first counts the triangles and writes nothing
        when the files would pass STL_ASK_BYTES; `_exact_built` then says how
        big they would be and builds only on a yes (`ask=False`)."""
        depo = self.last_depo
        if not (depo and os.path.isfile(depo)):
            self._refuse("Nothing to build from yet. Press Generate first.")
            return
        if getattr(self, "_exact_build_busy", False):
            return
        if self._run_in_flight():          # the build would take over self.proc
            self._tell_run_in_flight("STL")
            return
        out = self._depo_stem() + ".stl"      # NOT splitext (.depo.npz is 2 exts)
        gco = (self.last_gcode if (self.last_gcode and os.path.isfile(self.last_gcode))
               else out)
        diam, faces = self._fiber_diam_faces()
        args = worker_cmd("worker:convert", gco, "--from-depo", depo,
                          "-d", repr(float(diam)), "-f", str(int(faces)),
                          # the run's own Mesh detail (see _fiber_mesh_tol); the
                          # converter's default is 1.0, so omitting it thinned a
                          # "Full" export and mismatched every other setting
                          "--mesh-tolerance", repr(self._fiber_mesh_tol()),
                          "-o", out)
        if ask:
            args += ["--max-stl-bytes", "%d" % STL_ASK_BYTES]
        # The travel decision is NOT a property of the cache -- the .depo always
        # carries the travel fiber and the converter trims it on the way into the
        # mesh (see gcode_to_fibers' travel_spans). So the box has to ride along
        # here too, or an export built from the cache would drop travel the same
        # settings keep on a direct Generate.
        # a calibration slide answers to its own page's box (gui_sweeps)
        tis = (self.sw_vars.get("travel_in_stl")
               if getattr(self, "_last_gcode_is_calibration", False)
               else getattr(self, "s_travel_in_stl", None))
        if bool(tis is not None and tis.get()):
            args.append("--travel-in-stl")
        self.status.set("Building exact mesh (no re-solve)…")
        self._launch_exact_build(args, out, then)

    def _launch_exact_build(self, args, out, then):
        """Launch the exact-mesh subprocess off the Tk thread and route completion
        to _exact_built. Uses Popen (NOT subprocess.run) and REGISTERS the handle
        as self.proc, so the shared bottom-bar Cancel (self.cancel ->
        self.proc.terminate) can actually kill a heavy mesh build. Previously it ran
        as an unregistered subprocess.run in a daemon thread, so Cancel was a no-op
        and Generate/View stayed disabled for the whole build.

        The worker touches only self.proc (an atomic attribute set) and a result
        box; completion is delivered by a MAIN-thread poller -- self.root.after is
        only ever called from the Tk thread (calling it from the worker raises "main
        thread is not in main loop"). Factored out so a test can drive the
        build/cancel path with a stub process."""
        self._exact_build_busy = True
        self.cancelled = False        # the shared cancel() flag; a prior run's value
                                      # must not mislabel this build as cancelled
        self.run_btn.configure(state="disabled")
        self.view_btn.configure(state="disabled")
        try:
            self.cancel_btn.configure(state="normal")
        except (AttributeError, tk.TclError):
            pass
        creationflags = 0x08000000 if IS_WINDOWS else 0   # CREATE_NO_WINDOW
        box = {}

        def work():
            ok = False
            try:
                proc = subprocess.Popen(
                    args, cwd=HERE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, creationflags=creationflags,
                    **WORKER_PIPE_UTF8)
                self.proc = proc          # register so the shared Cancel reaches it
                text = proc.communicate()[0] or ""   # wait + drain the pipe
                ok = (proc.returncode == 0 and os.path.isfile(out))
                if proc.returncode == STL_TOO_BIG_EXIT:
                    box["too_big"] = _stl_too_big(text)
            except Exception:  # noqa: BLE001
                ok = False
            box["ok"] = ok
        worker = threading.Thread(target=work, daemon=True)
        worker.start()

        def poll():
            if worker.is_alive():
                self.root.after(EXACT_POLL_MS, poll)
                return
            self._exact_built(box.get("ok", False), out, then,
                              too_big=box.get("too_big"))
        self.root.after(EXACT_POLL_MS, poll)

    def _exact_built(self, ok, out, then, too_big=None):
        self._exact_build_busy = False
        self.proc = None
        self.run_btn.configure(state="normal")
        self.view_btn.configure(state="normal")
        try:
            self.cancel_btn.configure(state="disabled")
        except (AttributeError, tk.TclError):
            pass
        if getattr(self, "cancelled", False):
            self.cancelled = False
            self.status.set("Exact mesh build cancelled.")
            return
        if too_big is not None:
            self._ask_big_stl(too_big, then)
            return
        if ok:
            self.last_stl = out
            self.status.set("Exact mesh ready.")
            self._sync_result_buttons()
            if then:
                then(out)
        else:
            self.status.set("Exact mesh build failed.")

    def _ask_big_stl(self, size, then):
        """The converter refused to write an STL past STL_ASK_BYTES and said
        how big it would be (`size` = (bytes, triangles)). Say so, with what
        makes it smaller, and build it only on a yes."""
        n_bytes, n_tri = size
        big = _fmt_bytes(n_bytes)
        free = None
        try:
            free = shutil.disk_usage(os.path.dirname(
                os.path.abspath(self._depo_stem() + ".stl"))).free
        except OSError:
            pass
        disk = ""
        if free is not None and free < 2 * n_bytes:
            # built in the scratch folder, then copied to where it is saved
            disk = (" This disk has %s free, and the file is written once "
                    "here and again where you save it." % _fmt_bytes(free))
        msg = ("This STL would be about %s (%s triangles). Most STL viewers "
               "and slicers cannot open a file that size, and it takes a "
               "long time to write.%s\n\nTo make it smaller, lower Tube "
               "facets or Mesh detail under Settings (⚙) → Quality, then "
               "Generate again.\n\nBuild it anyway?"
               % (big, _fmt_count(n_tri), disk))
        # "Don't ask again" covers the size advice only: a disk too small
        # for the file still asks every time
        import gui_prefs
        key = None if disk else "big-stl"
        ask = getattr(self, "_gw_dialog", None)     # the modern App's
        if key and gui_prefs.is_hidden(key):
            yes = True
        elif callable(ask):
            yes = ask("A very large STL", msg,
                      [("yes", "Build it anyway"), ("no", "Don't build")],
                      default=0, cancel="no", dont_ask=key,
                      remember=("yes",)) == "yes"
        else:
            yes = messagebox.askyesno("A very large STL", msg,
                                      icon="warning", parent=self.root)
        if yes:
            self._build_exact_stl(then=then, ask=False)
        else:
            self._refuse("Not built: the STL would be about %s. Lower Tube "
                         "facets or Mesh detail under Settings (⚙) → "
                         "Quality, then Generate again." % big)

    def _viewer_color_args(self):
        """`--colors name=rrggbb,...` for stl_viewer, from the scaffold colour
        fields. The stabilization base takes scaffold 1's colour: it is printed
        as scaffold 1's lead-in and has no colour field of its own. Blank fields
        are dropped so the viewer keeps its rainbow default there; [] when
        nothing is set.

        When any scaffold is arrayed the converter names the tiled parts
        scaffold1..scaffoldK across the WHOLE expanded stack, so colour each copy by
        the ORIGINAL scaffold it came from (its source index -> that scaffold's
        colour). Read live, like the rest, so re-opening recolours without a
        regenerate."""
        items = []
        n = self._scaffold_count()
        any_array = any(self.scaf_vars[i]["array_on"].get() for i in range(n))
        if any_array:
            try:
                sources = self._array_sources()
            except (ValueError, tk.TclError):
                sources = []
            for j, src in enumerate(sources):
                hexv = self.scaf_vars[src]["color"].get().strip().lstrip("#")
                if hexv:
                    items.append(f"scaffold{j + 1}={hexv}")
        else:
            for idx in range(len(self.scaf_vars)):
                hexv = self.scaf_vars[idx]["color"].get().strip().lstrip("#")
                if hexv:
                    items.append(f"scaffold{idx + 1}={hexv}")
        stab = self.scaf_vars[0]["color"].get().strip().lstrip("#")
        if stab:
            items.append(f"stab={stab}")
        return ["--colors", ",".join(items)] if items else []

    def play_gcode(self, live=False, static=False):
        """Open the printhead animator (gcode_player.py) on the last g-code, in
        its OWN process -- same isolation as the STL viewer, so its VTK loop and
        the (lazily loaded) STL overlay can never affect the converter or this
        window. The last STL is passed only as an optional overlay path; the
        player doesn't read that file unless the user toggles the overlay on.

        live=True adds --live-adjust, enabling the player's real-time parameter
        editing (the second Simulation-tab button); the default launch is locked.

        static=True adds --static: the player opens PAUSED on the finished scaffold
        with the machine/printhead/jet/travels hidden -- the modern shell's unified
        Viewer default (visually the STL view; un-pause to animate). live and static
        are mutually exclusive; the modern _spawn_player routes static to its own
        panel. Never passed by the classic UI, so its behaviour is unchanged."""
        args = self._player_args(live=live, static=static)
        if args is None:
            self._refuse("Nothing to play yet. Press Generate first.")
            return
        self._spawn_player(args, live, static=static)

    # -- the scene a result was generated in ------------------------------- #
    #: Machine-page keys that stay LIVE for a frozen result: they choose what
    #: to draw, not which machine, so ticking Hide on a part after Generate
    #: still hides it.
    _VIEWER_LIVE_MACHINE_KEYS = ("machine.show", "machine.hide_head",
                                 "machine.hide_gantry", "machine.hide_bed",
                                 "machine.hide_frame")

    def _viewer_scene_note(self):
        """At Generate: record the machine this run is built on, as the
        Machine page's persisted values. Pending until the run succeeds
        (`_viewer_scene_commit`), so a failed Generate leaves the previous
        result with its own scene.

        Why. The Collector toggle loads its mode's machine onto the Machine
        page, so a viewer reading that page at Play time would play a 4-axis
        result opened after switching the toggle back to 3 axis on the 3-axis
        printer."""
        snap = None
        fn = getattr(self, "_snapshot_for_kind", None)
        if callable(fn) and hasattr(self, "_machine_args"):
            try:
                snap = {k: v for k, v in fn("machine").items()
                        if k not in self._VIEWER_LIVE_MACHINE_KEYS}
            except Exception:  # noqa: BLE001 -- no Machine page: read it live
                snap = None
        self._viewer_scene_pending = {"machine": snap}

    def _viewer_scene_commit(self, gcode):
        """A run succeeded: its noted scene now belongs to `gcode`."""
        pend = getattr(self, "_viewer_scene_pending", None)
        self._viewer_scene = (dict(pend, gcode=gcode) if pend is not None
                              else None)
        self._viewer_scene_pending = None

    def _viewer_scene_now(self):
        """The scene of the result on screen (`last_gcode`), or None when this
        session did not generate it."""
        scene = getattr(self, "_viewer_scene", None)
        if scene and scene.get("gcode") == getattr(self, "last_gcode", None):
            return scene
        return None

    def _viewer_machine_args(self):
        """`--machine` for the result on screen: `_machine_args`, the one
        writer, run on the Machine page's values AS THEY WERE AT GENERATE
        (`gui_sweeps.view_with`, which reads them without loading them into
        the page). No recorded scene -> the live page, as before."""
        scene = self._viewer_scene_now()
        if scene is None or scene.get("machine") is None:
            return self._machine_args()
        from gui_sweeps import view_with
        pmap = self._persist_map()
        values = {id(pmap[k]): v for k, v in scene["machine"].items()
                  if k in pmap}
        return view_with(self, values)._machine_args()

    def _player_args(self, live=False, static=False):
        """Assemble the gcode_player argv for the current print (quality, physics
        knobs, machine, origin, and the live/static mode flags), or None when there
        is no g-code. Factored from play_gcode so the unified Viewer can relaunch
        its panel in either mode (locked/static <-> live edit) without duplicating
        the arg build."""
        path = self.last_gcode
        if not path or not os.path.isfile(path):
            return None
        args = worker_cmd("worker:player", path, unbuffered=False)
        if self.last_stl and os.path.isfile(self.last_stl):
            args.append(self.last_stl)
        # the .depo sidecar this run wrote (next to the -o target). The player's
        # own search only looks next to the STL / the g-code, which BOTH miss for
        # a g-code import at a preview quality (no STL built; the import lives
        # outside the session folder) -- the locked sim then silently rebuilt the
        # deposition from the commanded g-code, dropping the physics rounding the
        # cache carries. Pass the path we already track so the locked sim always
        # rides the physics-shaped cache the export just wrote.
        if getattr(self, "last_depo", None) and os.path.isfile(self.last_depo):
            args += ["--depo", self.last_depo]
        # the build-plate sibling (<stem>_plate.stl), tracked from the -o stem so it
        # resolves for a g-code import too -- the player's own <stl>/<gcode>-adjacent
        # search misses it there (no STL, external g-code), leaving no plate toggle.
        if getattr(self, "last_plate", None) and os.path.isfile(self.last_plate):
            args += ["--plate-stl", self.last_plate]
        # forward the run's fiber params so the player's deposition view matches
        # the scaffold (diameter/faces/sag); empty before any run -> defaults
        if self.last_fiber_params:
            args += self.last_fiber_params
        # deposition render quality (Simulation tab): wireframe / tube:N render the
        # sagged centrelines (fast), exact builds the full mesh. The locked sim uses
        # it; the live-adjust march keeps its own incremental tubes (ignores it).
        render = RENDER_LBL2VAL.get(self.sim_render.get(), "tube:12")
        clear = getattr(self, "_render_note_clear", None)
        if callable(clear):
            clear()     # a new viewer says again if it has to step down
        if hasattr(self, "_gce_page_scope") and render.startswith("tube:"):
            # Smooth draws the tube facets the design was generated with; Fast
            # draws at most 6, as its label promises. Both used to take the
            # design's count, so choosing Fast changed nothing.
            faces = max(3, self._fiber_diam_faces()[1])
            if render != "tube:12":
                faces = min(faces, int(render.split(":", 1)[1]))
            render = "tube:%d" % faces
        args += ["--render", render]
        # _SimMixin forwards the printhead-animator knobs here; the unoverridden
        # hook returns [] so the player launches on its own defaults.
        args += self._sim_play_args(path)
        # player-only Taylor-cone toggle (kept out of _sim_play_args, which also feeds
        # the export); [] when the cone is left on
        args += self._sim_cone_args()
        # the g-code origin re-base the player must UNDO (so the rendered scene is
        # invariant to the origin choice); no machine-bounds box any more. Use the
        # value FROZEN at Generate (gui_run.run): the offset was baked into the
        # exported g-code then, so reading the origin fields live here would undo
        # the wrong offset if they were edited between Generate and Play (the
        # scaffold would detach from the plate). Fall back to a live read only when
        # nothing was frozen (no run yet -- play_btn is disabled then anyway).
        frozen_origin = getattr(self, "_frozen_origin_arg", None)
        args += frozen_origin if frozen_origin is not None \
            else self._viewer_origin_arg()
        # optional 3D printer model (printhead/gantry/bed STLs); [] when off.
        # The machine the RESULT was generated on (`_viewer_machine_args`), not
        # whatever the Collector toggle has loaded onto the Machine page since.
        # A calibration print plays on its own axis count's machine
        # (`gui_sweeps._sw_rig_machine_args`); the classic UI has no such page.
        rig = getattr(self, "_sw_rig_machine_args", None)
        args += rig() if callable(rig) else self._viewer_machine_args()
        # physics standalone printhead (the no-machine-model view): show the bare
        # frustum instead of the printhead STL when the printer model is off OR the
        # printhead is ticked Hidden, so those config toggles govern it too. With a
        # full machine model loaded the head hides via the profile's hidden_parts, so
        # this flag is moot there.
        if not self.machine_show.get() or self.machine_hide_head.get():
            args.append("--hide-printhead")
        if live and not static:
            args.append("--live-adjust")
            # diameter mode for the re-lay: --dynamic-diameter only when the diameter
            # is physics-driven, so a manual override re-lays at the typed diameter
            args += self._live_diam_args()
        if static:
            args.append("--static")
        return args

    def _spawn_player(self, args, live=False, static=False):
        """The windowed launch (the classic default; see play_gcode). `static` is
        accepted for signature parity with the modern shell's embed override (which
        routes it to the unified Viewer panel); a windowed launch just runs the argv
        as built (with --static already appended)."""
        try:
            subprocess.Popen(args, cwd=HERE)
        except Exception as exc:  # noqa: BLE001
            self._refuse('Could not open Simulation: %s. Try again, or restart the app if it keeps failing.' % exc)

    def view_gcode(self):
        """Load the last g-code into the embedded G-code text tab and switch to
        it (the text view is in-process now, not a separate window)."""
        if not (self.last_gcode and os.path.isfile(self.last_gcode)):
            self._refuse("No g-code yet. Press Generate first.")
            return
        self._load_gcode_text()
        try:
            self.nb.select(self._gcode_text_frame)
        except Exception:  # noqa: BLE001
            pass

    def _copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        # clipboard survives after the app closes
        self.root.update()
        self.status.set("G-code copied to clipboard.")


#: gcode_to_fibers.STL_TOO_BIG_EXIT, kept here so the GUI need not import the
#: converter (and numpy) to read a worker's exit code
STL_TOO_BIG_EXIT = 3


def _stl_too_big(text):
    """``(bytes, triangles)`` from the converter's ``STL_TOO_BIG`` line, or
    ``(0, 0)`` if it printed none."""
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "STL_TOO_BIG":
            try:
                return int(parts[1]), int(parts[2])
            except ValueError:
                break
    return 0, 0


def _fmt_bytes(n):
    """A file size in plain words: 9.8 GB, 640 MB, 12 KB."""
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= size:
            return "%.*f %s" % (1 if n < 10 * size else 0, n / size, unit)
    return "%d bytes" % n


def _fmt_count(n):
    """195 million, 2.3 million, 48,000."""
    if n >= 1e6:
        return "%.*f million" % (1 if n < 1e7 else 0, n / 1e6)
    return "{:,}".format(int(n))
