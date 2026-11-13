#!/usr/bin/env python3
"""gui_run.py -- Run mixin: launching the worker subprocess, streaming its output to the
log, cancel, the Keep/Save/Delete output bookkeeping, and on-close cleanup.

Part of the gcode_to_fibers_gui split; see gui_common.py."""
from gui_common import *
import gui_prefs
import mew_gcode

# A worker prints this line (prefix + space + the g-code path) the moment the g-code
# is on disk, BEFORE any slow STL/sag build, so the View/Play/Simulate buttons can
# enable mid-run. Keep in sync with the marker scaffold_gcode.py emits.
GCODE_READY_MARKER = "[GCODE-READY]"

# A worker under --progress prints this prefix, a fraction 0..1 and the stage name,
# every time it has moved measurably. Keep in sync with
# gcode_to_fibers.PROGRESS_MARKER. Read here rather than logged: a few hundred of
# them would bury the run's real output.
PROGRESS_MARKER = "[PROGRESS]"


class _RunMixin:
    def append_log(self, text):
        self.log.configure(state="normal")
        # the modern Log page's empty-state line goes with the first real one
        empty = self.log.tag_ranges("empty")
        if empty:
            self.log.delete(*empty)
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def run(self):
        mode = self._active_mode()
        if mode == "scaffold":
            err = self._validate_scaffold()
        else:
            err = self._validate_gcode()
        # the Physics page's fields ride every run in calibration.json; a typo
        # there would otherwise run silently at the factory value
        err = err or getattr(self, "_validate_calibration", lambda: None)()
        if err:
            self._refuse("Not generated: " + err)
            return

        # No output path is chosen up front: point this run's output StringVars at
        # the scratch session folder, then let the (unchanged) arg-builders read
        # them. A copy is exported on demand with "Save ... as".
        self._assign_output_paths(mode)

        if mode == "scaffold":
            args = self._build_args_scaffold()
        else:
            args = self._build_args_gcode()
        self.cancelled = False
        self._run_mode = mode
        self._gcode_ready_seen = False   # arm the mid-run "g-code ready" signal

        # Freeze the g-code-origin re-base at GENERATE time. The origin offset is BAKED
        # into the exported g-code here (the arg-builders above read _origin_offset()
        # live), so the player must undo THAT offset, not whatever the origin fields say
        # later -- otherwise editing them between Generate and Play detaches the
        # scaffold from the plate. play_gcode replays this frozen value and the
        # embedded-relaunch path inherits it. Same discipline as run_fiber_params below.
        try:
            self._frozen_origin_arg = list(self._viewer_origin_arg())
        except Exception:  # noqa: BLE001  (fall back to live read in play_gcode)
            self._frozen_origin_arg = None
        # ...and the machine and collector this run is built on, for the same
        # reason: the viewers must show the RESULT's rig, not the one the
        # Collector toggle has loaded since (committed on success, below).
        note = getattr(self, "_viewer_scene_note", None)
        if callable(note):
            note()

        # remember the fiber params this run uses, so a successful finish can forward
        # them to the player's deposition view
        if mode == "scaffold":
            self.run_fiber_params = self._fiber_params_scaffold()
        else:
            self.run_fiber_params = self._fiber_params_gcode()

        # Record what THIS run will produce, so the shared result tracking /
        # cancel-cleanup logic below handles every tab uniformly.
        self.expected_stl = self._stl_path_for_run(mode == "scaffold")
        # only the scaffold tab generates a g-code file; the g-code tab
        # consumes one the user supplied, which we must never delete
        self.expected_gcode = (self.s_gcode_out.get().strip()
                               if mode == "scaffold" else None)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.append_log("$ " + " ".join(f'"{a}"' if " " in a else a for a in args)
                        + "\n\n")

        self._keep_previous_result()
        creationflags = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=1, cwd=HERE, creationflags=creationflags,
                **WORKER_PIPE_UTF8)
        except Exception as exc:  # noqa: BLE001
            # a refusal, not a decision: the status line, as every other
            # refusal on the bar says it
            self._restore_previous_result()
            self.append_log("Could not start: %s\n" % exc)
            self.status.set("Not generated: the worker could not start (%s). "
                            "Check that the app's files are all present, then "
                            "press Generate again." % exc)
            self.proc = None
            return

        self.run_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        # Empty and determinate, filled by the worker's own reports (_on_progress).
        self._progress_idle(show=True)
        self.status.set("Generating…")
        refresh = getattr(self, "_refresh_viewer_idle", None)
        if callable(refresh):
            refresh()    # Simulation's idle line says a run is under way

        # Print history: snapshot the fields AFTER the progress bar starts, so nothing
        # here sits on the Generate-latency path. The worker is already launched and
        # reads the fields via argv, so the snapshot still matches what this run used.
        if hasattr(self, "_history_on_run_start"):
            self._history_on_run_start(mode, args)

        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.reader.start()

    def _assign_output_paths(self, mode):
        """Point this run's output StringVars at the per-session scratch folder. The
        user never picks an output path up front; a copy is exported on demand with
        "Save ... as". Names derive from the input stem (g-code) or a fixed
        base (scaffold), so distinct inputs do not collide within a session."""
        sd = self.session_dir
        # made once at startup; re-made here in case it vanished since (a disk
        # cleaner, someone tidying outputs/) -- else every later run fails
        try:
            os.makedirs(sd, exist_ok=True)
        except OSError:
            pass               # the worker's own write error then says why
        if mode == "scaffold":
            self.s_gcode_out.set(os.path.join(sd, "scaffold.gcode"))
            self.s_stl_out.set(os.path.join(sd, "scaffold.stl"))
        else:
            stem = os.path.splitext(
                os.path.basename(self.in_var.get().strip()))[0] or "output"
            self.out_var.set(os.path.join(sd, stem + ".stl"))

    def _run_output_files(self):
        """Every file this run writes over: its g-code (scaffold runs only),
        its STL, and the STL stem's deposition cache and plate."""
        stl = getattr(self, "expected_stl", None)
        files = [getattr(self, "expected_gcode", None), stl]
        if stl:
            stem = os.path.splitext(stl)[0]
            files += [stem + DEPO_CACHE_EXT, stem + "_plate.stl"]
        return [p for p in files if p]

    def _keep_previous_result(self):
        """A run writes to the same scratch names as the last one, so a
        cancelled or failed run used to leave the user with no result at all
        (the cancel scrub deleted the half-written g-code, which was the last
        good one's file), or with a new g-code beside the old run's deposition
        cache. Move the last result aside first; `_on_finished` discards it
        on success and puts it back otherwise."""
        self._kept_result = []
        # ...and what pointed at it: a mid-run sync drops a pointer whose
        # file is aside, so the pointers come back with the files
        self._kept_pointers = {n: getattr(self, n, None) for n in
                               ("last_gcode", "last_stl", "last_depo",
                                "last_plate")}
        keep = os.path.join(self.session_dir, "previous_result")
        for path in self._run_output_files():
            if not os.path.isfile(path):
                continue
            dest = os.path.join(keep, os.path.basename(path))
            try:
                os.makedirs(keep, exist_ok=True)
                os.replace(path, dest)
            except OSError:
                continue          # held open: the run overwrites it as before
            self._kept_result.append((dest, path))

    def _restore_previous_result(self):
        """Put the last result back over whatever this run left. True when
        there was one to put back."""
        kept = getattr(self, "_kept_result", None) or []
        self._kept_result = []
        for path in self._run_output_files():
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        for dest, path in kept:
            try:
                os.replace(dest, path)
            except OSError:
                pass
        if kept:
            for name, val in (getattr(self, "_kept_pointers", None)
                              or {}).items():
                setattr(self, name, val)
        return bool(kept)

    def _discard_previous_result(self):
        for dest, _path in getattr(self, "_kept_result", None) or []:
            try:
                os.remove(dest)
            except OSError:
                pass
        self._kept_result = []

    def _stl_path_for_run(self, scaffold):
        """Where this run will write its STL, or None if it won't produce one."""
        if scaffold:
            # Since the tab merge the mandrel is a mode of the scaffold page and shares
            # its Export panel, so "Also render STL" means the same on both collectors.
            if not self.s_make_stl.get():
                return None
            return self.s_stl_out.get().strip() or None
        # G-code tab: worker defaults to <input>.stl when no -o is given
        out = self.out_var.get().strip()
        if out:
            return out
        inp = self.in_var.get().strip()
        return (inp.rsplit(".", 1)[0] + ".stl") if inp else None

    def _read_output(self):
        try:
            for line in self.proc.stdout:
                self.out_q.put(("line", line))
        except Exception as exc:  # noqa: BLE001
            self.out_q.put(("line", f"\n[reader error: {exc}]\n"))
        finally:
            code = self.proc.wait()
            self.out_q.put(("done", code))

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            self.cancelled = True       # tell _on_finished to delete the outputs
            self.status.set("Cancelling…")
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    def _poll_output(self):
        try:
            while True:
                kind, payload = self.out_q.get_nowait()
                if kind == "line":
                    if payload.startswith(GCODE_READY_MARKER):
                        self._on_gcode_ready(payload[len(GCODE_READY_MARKER):].strip())
                    elif payload.startswith(PROGRESS_MARKER):
                        self._on_progress(payload[len(PROGRESS_MARKER):].strip())
                    else:
                        self.append_log(payload)
                elif kind == "done":
                    self._on_finished(payload)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_output)

    def _progress_idle(self, show=False):
        """Empty the shared bar: determinate at 0.

        The resting state between runs, and the state a reporting run starts
        in (`show=True`). Also un-borrows the bar from a `_progress_bounce`
        caller. At rest the bar is taken off the row: an empty trough beside
        Cancel reads as a text box nobody can type in."""
        try:
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=1.0, value=0)
        except (AttributeError, tk.TclError):   # a partial build has no bar
            return
        self._progress_show(show)

    def _progress_show(self, on):
        """Put the bar on the row after Cancel, or take it off."""
        bar = getattr(self, "progress", None)
        if bar is None:
            return
        try:
            if not on:
                bar.pack_forget()
            elif bar.winfo_manager() != "pack":
                cancel = getattr(self, "cancel_btn", None)
                if cancel is not None and cancel.master is bar.master:
                    bar.pack(side="left", padx=12, after=cancel)
                else:
                    bar.pack(side="left", padx=12)
        except tk.TclError:
            pass

    def _progress_bounce(self, interval=12):
        """Borrow the bar as an indeterminate bouncer, for the runs that report
        no fraction, such as the Design preview, which passes a slow
        `interval` (ms per step) so it reads as working, not as a flicker."""
        try:
            self.progress.configure(mode="indeterminate", value=0)
            self.progress.start(interval)
        except (AttributeError, tk.TclError):
            return
        self._progress_show(True)

    def _on_progress(self, payload):
        """A worker said how far through its WORK it is: '<fraction> <stage>'.

        Not a time estimate: the fraction counts layers solved, fibers tubed and
        triangles written (gcode_to_fibers.PROGRESS_WEIGHTS), which means the same thing
        on any machine. The stage name rides along because the weights differ per stage,
        so the bar moves at different rates and the name is what makes that legible. A
        malformed line is dropped rather than raised on -- this is a display."""
        frac_s, _, label = payload.partition(" ")
        try:
            frac = float(frac_s)
        except ValueError:
            return
        frac = min(1.0, max(0.0, frac))
        try:
            self.progress.configure(value=frac)
        except (AttributeError, tk.TclError):
            return
        self._progress_show(True)
        label = label.strip()
        self.status.set("%s — %d%%" % (label or "Generating", round(frac * 100)))

    def _on_gcode_ready(self, path):
        """Worker signalled the g-code is written, before its slow STL build: point the
        result buttons at it now so View g-code / Play / the real-time sim enable mid-run.
        The STL buttons stay greyed until _on_finished sees the STL on disk. Fires at
        most once per run -- the scaffold worker announces the g-code and the chained
        converter announces the same file, and the repeat must not reload the text."""
        if getattr(self, "_gcode_ready_seen", False):
            return
        if not (path and os.path.isfile(path)):
            return
        self._gcode_ready_seen = True
        self.last_gcode = path
        self.append_log("[g-code ready -- simulation available]\n")
        self._sync_result_buttons()

    def _on_finished(self, code):
        was_cancelled = self.cancelled   # the cancelled branch clears the flag below
        self._progress_idle()
        self.run_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.proc = None

        # the worker has exited, so its --scaffolds-json handoff (if any) is no
        # longer needed -- remove it whether the run succeeded, failed or cancelled
        if self._scaffolds_json_tmp and os.path.isfile(self._scaffolds_json_tmp):
            try:
                os.remove(self._scaffolds_json_tmp)
            except OSError:
                pass
        self._scaffolds_json_tmp = None

        if code == 0:
            self._discard_previous_result()
            self.append_log("\n[finished successfully]\n")
            self.last_fiber_params = self.run_fiber_params
            # All outputs live in the scratch session folder and are removed wholesale
            # on close, so there is no per-file cleanup tracking to do.
            # g-code to offer for viewing: the one just generated (scaffold tab),
            # or the input file the g-code tab consumed
            view_gco = self.expected_gcode or self.in_var.get().strip()
            if view_gco and os.path.isfile(view_gco):
                self.last_gcode = view_gco
                commit = getattr(self, "_viewer_scene_commit", None)
                if callable(commit):
                    commit(view_gco)
                # scaffold-generated g-code (every G0 is a travel) is eligible for
                # the travel-as-G1 emission swap; an imported file (g-code tab, where
                # expected_gcode is None) is not.
                self._last_gcode_is_scaffold = bool(
                    self.expected_gcode and view_gco == self.expected_gcode)
                self._last_gcode_is_calibration = False
            stl = self.expected_stl
            # exact STL only when the file was actually built: a preview run (--no-mesh)
            # writes just the .depo cache, so the viewers render from the cache
            self.last_stl = stl if (stl and os.path.isfile(stl)) else None
            # the result, and what to do with it next
            self.status.set(
                "Generated the g-code and its STL. Watch it print in "
                "Simulation, or save them with the buttons at the right."
                if self.last_stl else
                "Generated the g-code. Watch it print in Simulation, or "
                "press Save g-code as… to keep it.")
            # A worker's "warning:" lines (a file printing below the plate, Z
            # levels collapsed by a diameter in the wrong unit) went to the log
            # only, which nobody reads after a run that succeeded. The first is
            # put on the status line, which is where the eye goes next.
            try:
                warns = [ln.strip()[len("warning:"):].strip()
                         for ln in self.log.get("1.0", "end").splitlines()
                         if ln.strip().lower().startswith("warning:")]
            except Exception:  # noqa: BLE001 -- the status is a courtesy
                warns = []
            if warns:
                more = (" (and %d more in the log)" % (len(warns) - 1)
                        if len(warns) > 1 else "")
                self.status.set("Generated, with a warning: %s%s"
                                % (warns[0][:1].upper() + warns[0][1:], more))
            self.last_depo = None
            self.last_plate = None
            if stl:
                depo = os.path.splitext(stl)[0] + DEPO_CACHE_EXT
                if os.path.isfile(depo):
                    self.last_depo = depo
                    # ...and remember the PAIR, not just the newest of each. The
                    # Machine-link live view matches the printing file against these to
                    # skip re-solving the sag; keyed off last_gcode alone, any later
                    # Generate dropped it (see gui_core.register_export).
                    self.register_export(self.last_gcode, depo)
                # the build-plate sibling (<stem>_plate.stl) the converter writes for any
                # run that requests it, even a preview with no main STL. Tracked from the
                # -o STEM so the viewers can show it for a g-code import, where the stem
                # cannot be derived from a missing STL or an external g-code.
                plate = os.path.splitext(stl)[0] + "_plate.stl"
                if os.path.isfile(plate):
                    self.last_plate = plate
                # The STL viewer's quality follows the Generate quality, because the two
                # are genuinely coupled: only an "exact" run writes an STL for it to
                # read. Its own dropdown still overrides.
                #
                # The unified Viewer's "Display quality" (sim_render) is NOT synced, and
                # must not be. The locked sim always rides the .depo cache, so it can
                # draw any quality regardless of what Generate built, and the sync only
                # ever raised it -- generating at "Full mesh" silently flipped the Viewer
                # to `--render exact`, whose _tube_fibers path tubes EVERY sag sample.
                if hasattr(self, "gen_render"):
                    self.stl_render.set(self.gen_render.get())
            # point the context-aware result controls at this run's output (and grey out
            # View/Play/Save for anything not on disk), refresh the viewer tabs. A
            # successful run does NOT auto-jump to the Simulation tab.
            self._sync_result_buttons()
            # the Design page draws what this run solved (gui_gcode_editor)
            ready = getattr(self, "_gce_result_ready", None)
            if callable(ready):
                ready()
            if not (self.last_stl or self.last_depo) and self.last_gcode:
                # no 3-D result at all (e.g. a g-code-only run): surface the
                # embedded g-code text, since there's nothing else to show
                try:
                    self.nb.select(self._gcode_text_frame)
                except Exception:  # noqa: BLE001
                    pass
            # the modern shell says where to go next (gui_shell); the
            # classic UI keeps its plain "Done."
            nxt = getattr(self, "_generate_next_step", None)
            if callable(nxt):
                nxt()
        elif self.cancelled:
            # Cancelled run: scrub the partial files it produced so nothing lingers in
            # the viewer for the rest of the session. Never touch the g-code tab's input
            # -- expected_gcode is None there.
            self.cancelled = False
            # the partial files go, and the last good result comes back
            kept = self._restore_previous_result()
            # A mid-run [GCODE-READY] may already have pointed the result buttons and
            # embedded g-code text at the cancelled run's g-code. Re-arm the ready
            # flag and re-sync: _sync_result_buttons reconciles last_gcode/last_stl/
            # last_depo against disk. Mirrors the success branch.
            self._gcode_ready_seen = False
            self._sync_result_buttons()
            self.status.set("Cancelled. " + (
                "The last result is still loaded." if kept
                else "Nothing was generated."))
            self.append_log("\n[cancelled by user; partial files removed"
                            + ("; the last result restored" if kept else "")
                            + "]\n")
        else:
            kept = self._restore_previous_result()
            self._gcode_ready_seen = False
            self._sync_result_buttons()
            self.append_log(f"\n[worker exited with code {code}]\n")
            self._refuse("Generation failed. "
                         + self._failure_message(code)
                         + (" The last result is still loaded." if kept
                            else ""), log=False)

        # Print history: finalize the record started at Generate, off the now-settled
        # last_stl/last_gcode/last_depo. Guarded -> no-op without _HistoryMixin.
        if hasattr(self, "_history_on_run_finish"):
            status = ("ok" if code == 0
                      else ("cancelled" if was_cancelled else "failed"))
            self._history_on_run_finish(status, code)

    def _failure_message(self, code, gcode_ok=False):
        """What the "Generation failed" dialog says. It used to be a guess.

        Every non-zero exit read "If it ran out of memory, try fewer faces or a
        coarser sag resolution", and the failure a five-axis user actually hit
        was exit 2 -- argparse refusing the argv, with the reason printed on the
        log two lines above the dialog ("--surface needs a machine.json with
        exactly two rotary joints; ... has 0"). The dialog showed the memory sentence;
        nothing on screen said what was wrong.

        So the dialog QUOTES the last thing the worker said, and the memory hint
        is kept only where running out of memory is a real possibility. Exit 2
        is argparse, and argparse does not run out of memory.

        The log is read rather than the worker's stderr being buffered a second
        time: `append_log` is the one place every line lands, from both streams,
        for every target, so a second copy is a second thing to keep in step.
        """
        crashed = False
        if gcode_ok:
            # The five-axis chain: the g-code stage already wrote its file
            # before handing off to the STL converter, whose exit code this
            # is -- so "the worker exited with code N" would be true of the
            # g-code too and misleadingly imply the whole run failed.
            msg = (f"The g-code was written. The STL/mesh stage then exited "
                   f"with code {code} and did not build one.\n")
        elif code is not None and (code < 0 or code > 0x7FFFFFFF):
            # a Windows exception status (0xC0000017 no memory, 0xC00000FD
            # stack overflow...), which reads as noise in decimal
            msg = ("The worker crashed (Windows status 0x%08X).\n"
                   % (code & 0xFFFFFFFF))
            crashed = True
        else:
            msg = f"The worker exited with code {code}.\n"
        try:
            lines = [ln.strip() for ln in
                     self.log.get("1.0", "end").splitlines()]
        except Exception:  # noqa: BLE001 -- a dialog must never be the failure
            lines = []
        # "$ ..." is the argv echo and "[...]" the harness' own brackets;
        # neither is the worker telling you what went wrong.
        said = [ln for ln in lines
                if ln and not ln.startswith("$") and not ln.startswith("[")]
        # Exit 2 with "<script>.py: error: <why>" is a worker REFUSING the
        # input, in words written for the user. Shown as that refusal alone:
        # the script name and the exit code are the log's business.
        refusal = (re.match(r"^\S+\.py: error: (.+)$", said[-1])
                   if (code == 2 and said and not gcode_ok) else None)
        if refusal:
            why = refusal.group(1).strip()
            why = why[:1].upper() + why[1:]
            return ("This design cannot be generated:\n\n%s%s\n\nChange it "
                    "and press Generate again. The log has the details."
                    % (why[:600], "" if why.endswith(".") else "."))
        # A crash gives no reason: its last line is progress chatter ("Parsing
        # <scratch path> ... (mode: g1)"), which explains nothing.
        if said and not crashed:
            msg += "\nIt said:\n" + said[-1][:400] + "\n"
        msg += "\nSee the log for the rest."
        if code != 2 and not gcode_ok:
            # named as Settings' Quality section names them, with the way
            # there: a blind tester read "under Quality" and found no Quality
            msg += ("\n\nIf it ran out of memory, close other programs and "
                    "press Generate again, or choose a coarser Sag resolution "
                    "or fewer Tube facets in Settings (the gear at the top "
                    "right) › Quality, under STL & simulation.")
        return msg

    # ------------------------------------------------------------------ #
    # the pre-flight: what a saved file will and will not do on a printer
    # ------------------------------------------------------------------ #
    #: what Save's pre-flight can ask about, in the order it says them
    PREFLIGHT_KEYS = ("voltage", "pressure", "temperature", "end")

    def _preflight_accepted(self):
        """The pre-flight keys this printer's setup has accepted (Setup's
        G-code card, or "don't ask again" on the dialog)."""
        var = getattr(self, "preflight_accepted", None)
        try:
            raw = var.get() if var is not None else ""
        except tk.TclError:
            raw = ""
        return {k.strip() for k in str(raw).split(",") if k.strip()}

    def _preflight_accept(self, keys):
        var = getattr(self, "preflight_accepted", None)
        if var is not None:
            have = self._preflight_accepted() | set(keys)
            var.set(",".join(k for k in self.PREFLIGHT_KEYS if k in have))

    def _preflight_gaps(self, text, flavor, designed=True):
        """{key: sentence} for what the g-code `text` about to leave the app
        leaves to the printer and this printer has not accepted: a process
        setting it does not set, and an empty End block. Read off the text
        itself, so it describes the bytes that are saved. `designed`: the
        text is the generator's (an opened file's end is its own business).
        How far the axes move and where zero is are not here: they are facts
        to read, in the header and on Setup, not something to act on."""
        accepted = self._preflight_accepted()
        sp = mew_gcode.process_setpoints(text, flavor)
        out = {}
        missing = [n for n in ("voltage", "pressure", "temperature")
                   if not sp[n]["written"] and n not in accepted]
        if missing:
            # only the unaccepted settings, said as process_gap_text says them
            shown = {n: dict(sp[n], written=n not in missing)
                     for n in ("voltage", "pressure", "temperature")}
            out["process"] = mew_gcode.process_gap_text(
                shown, flavor, where="Setup's G-code card")
        if designed and "end" not in accepted:
            end = getattr(self, "machine_gcode_end", None)
            omit = getattr(self, "machine_omit_blocks", None)
            try:
                empty = (end is not None and not end.get().strip()
                         and not (omit is not None and omit.get()))
            except tk.TclError:
                empty = False
            if empty:
                out["end"] = ("The End block on Setup's G-code card is "
                              "empty, so nothing is switched off when the "
                              "print ends.")
        return out, missing

    def _preflight_items(self, text, flavor, missing, end_empty):
        """The pre-flight's list: one short line per setting the file leaves
        to the printer (`missing`), then the empty End block. The same facts
        `mew_gcode.process_gap_text` gives as a paragraph."""
        sp = mew_gcode.process_setpoints(text, flavor)
        fl = mew_gcode.get_flavor(flavor)
        items = []
        no_word = [n for n in missing if n != "temperature"
                   and not sp[n]["word"]]
        has_word = [n for n in missing if n != "temperature"
                    and sp[n]["word"]]
        if no_word:
            items.append("%s: set on the printer before you start (%s has "
                         "no command for %s)." % (
                             " and ".join(no_word).capitalize(), fl.label,
                             "it" if len(no_word) == 1 else "them"))
        for n in has_word:
            items.append("%s: not set. Add [[%s_command]] to the start "
                         "block on Setup's G-code card." % (n.capitalize(), n))
        if "temperature" in missing:
            if fl.heat_wait:
                items.append("Temperature: set on the printer, or tick “Wait "
                             "to print until target temperature is reached” "
                             "on Setup's G-code card.")
            else:
                items.append("Temperature: set on the printer (%s has no "
                             "heat command)." % fl.label)
        if end_empty:
            items.append("End of print: the End block is empty, so nothing "
                         "is switched off when the print ends.")
        return items

    def _preflight_ok(self, text, flavor=None, designed=True, send=False):
        """Before Save (or Send, `send`: the same bytes go to the printer),
        only when there is something to act on: a process
        setting the file leaves to the printer, or an empty End block, that
        this printer has not accepted. Says it, with the way to Setup's
        G-code card where those are set, and lets the researcher accept it
        for this printer so Save stops asking. True to go on and save. A
        harness or test app (no prompts) saves without asking."""
        # what the dialog just said, so a Send's "start now?" prompt that
        # follows it does not say it again (`_ml_heat_note`)
        self._preflight_said = set()
        ask = getattr(self, "_gw_interactive", None)
        if not text or not (callable(ask) and ask()):
            return True
        if flavor is None:
            flavor = self._gcode_export_flavor()
        try:
            gaps, missing = self._preflight_gaps(text, flavor, designed)
        except Exception:  # noqa: BLE001 -- a summary never blocks a save
            return True
        if not gaps:
            return True
        keys = list(missing) + (["end"] if "end" in gaps else [])
        try:
            items = self._preflight_items(text, flavor, missing,
                                          "end" in gaps)
        except Exception:  # noqa: BLE001 -- the long sentences still say it
            items = list(gaps.values())
        # one short line per thing, read at a glance (a tester called the
        # paragraph it replaced "a dense block")
        body = ("This file leaves these to your printer:\n\n"
                + "\n".join("•  " + s for s in items)
                + "\n\nYour printer's own commands go on Setup's G-code card."
                  "\n“Don't ask again” stops this for this printer.")
        verb = "Send" if send else "Save"
        key = self._gw_dialog(
            "Before you %s" % verb.lower(), body,
            [("save", "Send" if send else "Save…"),
             ("accept", "%s, don't ask again" % verb),
             ("setup", "Open Setup's G-code card"), ("cancel", "Cancel")])
        self._preflight_said = set(keys)
        if key == "setup":
            try:
                self._nav_select("machine")
                self._setup_go("gcode", "gcode_flavor")
            except (AttributeError, tk.TclError):
                pass
            return False
        if key == "accept":
            self._preflight_accept(keys)
            return True
        return key == "save"

    def save_gcode_as(self):
        """Export the g-code this run produced (or the g-code tab's input) to a
        path + name the user picks. The scratch original is left untouched -- it's
        swept with the session folder on close."""
        # g-code emission rewrite (travels-as-G1 and/or G0X padding): applied to the
        # EXPORTED copy only (the scratch file stays G0/G1/G2). Inactive (imported
        # g-code, or both options off) -> a plain byte-copy.
        # mid-run, last_gcode is the PREVIOUS run's path -- the very file this run
        # is rewriting -- until the worker announces the new one ready
        if self._run_in_flight() and not getattr(self, "_gcode_ready_seen", False):
            self._tell_run_in_flight("g-code")
            return
        xform = (self._gcode_for_display_export if self._gcode_export_active()
                 else None)
        name = forbid = None
        page = getattr(self, "_gce_page_save", None)
        spec = page() if callable(page) else None
        if spec is not None:
            # a Generate of the G-code page's own text: written as that
            # page's Save writes it, under the name it offers
            xform, name, forbid = spec
        # Saved off the Design page, this is the last Generate -- what
        # Simulation shows. If Design has changed since, Send refuses it as
        # out of date; Save keeps it (it is what was watched) but says so.
        stale = getattr(self, "_gw_stale_reason", None)
        why = stale() if callable(stale) else None
        shown = self._gcode_save_text()
        if shown is None:
            try:
                with open(self.last_gcode, encoding="utf-8",
                          errors="replace") as fh:
                    shown = fh.read()
            except (OSError, TypeError):
                shown = None
        if not self._preflight_ok(shown, designed=(
                spec is None and self._gcode_is_designer_output())):
            return None
        dest = self._export_as(self.last_gcode, "Save g-code as", ".gcode",
                               [("G-code", "*.gcode"), ("All files", "*.*")],
                               transform=xform, initialfile=name,
                               forbid=forbid)
        if dest:
            # the next Save offers a name no file in this folder has yet
            self._gce_saved_dir = os.path.dirname(os.path.abspath(dest))
        if dest and why:
            self.status.set(
                "Saved %s from your last Generate. %s Press Generate to save "
                "the change too." % (os.path.basename(dest), why["text"]))
        return dest

    def _readout_move_as_saved(self, line):
        """One move line of the played g-code, spelled as Save and Send
        write it. Simulation plays the scratch copy, where a travel is
        `G0`; the exported file writes it `G1` (travel as G1) or `G01`
        (padded), in the printer's flavor. A file the user opened, and the
        G-code page's own text, leave the app as typed, so they are shown
        as typed. Only the spelling of the one line is rewritten; anything
        that cannot be is shown unchanged."""
        if not line or line == "—":
            return line
        page = getattr(self, "_gce_page_save", None)
        if callable(page) and page() is not None:
            return line
        if not (getattr(self, "_last_gcode_is_scaffold", False)
                or getattr(self, "_last_gcode_is_calibration", False)):
            return line
        cache = getattr(self, "_readout_spell_cache", None)
        if cache is None:
            cache = self._readout_spell_cache = {}
        try:
            opts = self._gcode_output_options()
            flavor = self._gcode_export_flavor()
            key = (line, opts.travel_as_g1, opts.padded, flavor)
            if key not in cache:
                if len(cache) > 512:
                    cache.clear()
                out = mew_gcode.rewrite_gcode_lines(
                    line, opts.travel_as_g1, opts.padded)
                cache[key] = mew_gcode.machine_text(out, flavor, opts).strip() \
                    or line
            return cache[key]
        except Exception:  # noqa: BLE001 -- a readout, never a blocker
            return line

    def _gcode_save_text(self, filename=None):
        """The text Save g-code would write for this run's g-code, for an
        output that is not a Save (the Machine link's Upload, History's kept
        copy), or None when Save would copy the file byte for byte."""
        src = getattr(self, "last_gcode", None)
        if not (src and os.path.isfile(src)) or not self._gcode_export_active():
            return None
        with open(src, "r", errors="ignore") as fh:
            data = fh.read()
        self._gcode_export_filename = filename or os.path.basename(src)
        try:
            return self._gcode_for_display_export(data)
        finally:
            del self._gcode_export_filename

    def save_result_as(self):
        """Export this run's STL to a path + name the user picks. Export is ALWAYS the exact mesh: if the run was
        a preview (mesh skipped), build it from the .depo cache first (no re-solve),
        then save."""
        # Mid-run the previous run's STL / .depo are the paths this run is
        # rewriting, and an exact build would take over self.proc -- orphaning
        # the running worker from Cancel and from on_close.
        if self._run_in_flight():
            self._tell_run_in_flight("STL")
            return
        if self.last_stl and os.path.isfile(self.last_stl):
            self._export_as(self.last_stl, "Save STL as", ".stl",
                            [("STL", "*.stl"), ("All files", "*.*")])
        elif getattr(self, "last_depo", None) and os.path.isfile(self.last_depo):
            # preview run -> build the exact mesh on demand, then open the dialog
            self._build_exact_stl(
                then=lambda p: self._export_as(
                    p, "Save STL as", ".stl",
                    [("STL", "*.stl"), ("All files", "*.*")]))
        else:
            self._export_as(None, "Save STL as", ".stl", [])   # -> "nothing to save"

    def _run_in_flight(self):
        """True while a worker (a Generate run or an exact build) is running."""
        proc = getattr(self, "proc", None)
        return proc is not None and proc.poll() is None

    def _tell_run_in_flight(self, what):
        self._refuse(f"Not saved: the {what} is still being generated. Save "
                     "a copy once the run finishes, or Cancel it.")

    def _refuse(self, text, log=True):
        """A refusal or a failed result, in the status line under the bottom
        row: every page shows it there, and a popup is kept for what needs a
        decision. One line, so the text's own line breaks are folded."""
        line = " ".join(str(text).split())
        if log:
            try:
                self.append_log(line + "\n")
            except Exception:  # noqa: BLE001 -- a partial build has no log
                pass
        st = getattr(self, "status", None)
        if st is not None:
            st.set(line)

    def _export_as(self, src, title, ext, filetypes, transform=None,
                   initialfile=None, forbid=None):
        """Copy a scratch file out to a user-chosen destination. Only the single
        file is copied -- viewer aids (slide, deposition cache, per-scaffold parts)
        stay scratch-only. `transform`, when given, is a str->str filter applied to
        the file's text on the way out (used by the travel-as-G1 g-code swap); the
        scratch source is left untouched. Without it the file is byte-copied.
        Returns the path written, or None.

        The transformed text is read and written as UTF-8 with LF line ends,
        as Send writes the bytes it uploads: in Windows text mode this wrote
        CRLF, so the saved file and the sent one differed byte for byte, and
        a cp1252 read with errors="ignore" could drop bytes of a UTF-8
        comment.

        `initialfile` is the name offered (default: the scratch file's);
        `forbid` is a path never written -- the g-code file the user opened,
        which the G-code page's own Save also refuses to overwrite."""
        if not (src and os.path.isfile(src)):
            self._refuse("Nothing to save yet: press Generate first, then "
                         "save a copy.")
            return None
        dest = filedialog.asksaveasfilename(
            **gui_prefs.save_dir_kw(),
            title=title, defaultextension=ext, filetypes=filetypes,
            initialfile=initialfile or os.path.basename(src))
        if not dest:
            return None
        if forbid and os.path.normcase(os.path.abspath(dest)) == \
                os.path.normcase(os.path.abspath(forbid)):
            self._refuse("Not saved: MEWracle never overwrites the file you "
                         "opened. Save again under a new name.")
            return None
        try:
            if transform is not None:
                with open(src, "r", encoding="utf-8",
                          errors="surrogateescape") as fh:
                    data = fh.read()
                with open(dest, "w", encoding="utf-8",
                          errors="surrogateescape", newline="\n") as fh:
                    self._gcode_export_filename = os.path.basename(dest)
                    try:
                        fh.write(transform(data))
                    finally:
                        del self._gcode_export_filename
            else:
                shutil.copy2(src, dest)
        except OSError as exc:  # noqa: BLE001
            self._refuse("Not saved: %s. Pick another folder and save "
                         "again." % exc)
            return None
        self.status.set("Saved " + os.path.basename(dest) + ".")
        return dest

    def on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno(
                    "Quit", "A run is still in progress. Stop it and quit?"):
                return
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            # TerminateProcess is asynchronous on Windows: the worker may still hold
            # handles inside the session folder after terminate() returns, which makes
            # the rmtree below silently fail and leave scratch cruft. Give it a beat;
            # the next launch's stale-session sweep is the backstop either way.
            try:
                self.proc.wait(timeout=2)
            except Exception:  # noqa: BLE001  (TimeoutExpired or already-gone)
                pass

        # The session scratch folder is throwaway: everything this run generated lives
        # inside it, and any copy the user wanted was already exported. Remove it
        # wholesale. A crash skips this and leaves the folder for the next launch's
        # stale-session sweep -- see gui_common.prepare_session_dir.
        shutil.rmtree(self.session_dir, ignore_errors=True)
        self.root.destroy()
