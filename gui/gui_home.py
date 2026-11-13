#!/usr/bin/env python3
"""gui_home.py -- MODERN-UI ONLY: the Home page. A calm landing surface built as
one guided PATH: a one-line welcome, a numbered "Get started" flow that walks the
top row left to right (Setup -> Calibrate -> Design -> Simulation), each step
marked ✓ with one line saying where it stands once it is done and the
recommended next step highlighted live, a compact "More tools" list for the
optional extras -- only the ones this build actually has -- and what the app
keeps between sessions.

Home is a NON-INPUT page: it never changes what Generate targets. _refresh_home
runs each time Home is shown, so the ticks and the "next step" track the
session live; every step stays clickable regardless.

The classic (developer) UI never imports this module."""

from gui_common import *

import gui_fonts
import gui_form
import gui_tips
import gui_widgets


class _HomeMixin:
    def _build_home_page(self, parent):
        HEAD, GAP = gui_fonts.GAP_HEADER, gui_fonts.GAP_SECTION
        parent.columnconfigure(0, weight=1)
        # On a wide window the rows stop at a reading width: at 1920 px a
        # full-width row put its arrow a screen away from its words. Grid has
        # no maximum size, so past the cap the empty column 1 takes the rest.
        cap = gui_fonts.dpi_px(parent, 960)

        def _cap(e):
            wide = e.width > cap
            parent.columnconfigure(0, weight=0 if wide else 1,
                                   minsize=cap if wide else 0)
            parent.columnconfigure(1, weight=1 if wide else 0)
        parent.bind("<Configure>", _cap, add="+")
        r = 0

        # -- welcome ------------------------------------------------------- #
        ttk.Label(parent, text="Welcome to MEWracle",
                  style="Title.TLabel").grid(row=r, column=0, sticky="w")
        r += 1
        # the direction page's one sentence (§1), word for word: Home, the
        # README and the tour all open on it
        gui_form.page_intro(
            parent, "MEWracle turns a scaffold design into g-code your "
                    "melt-electrowriting printer can run, and shows you how "
                    "the fiber will land before you print it.",
            row=r, wrap=680)
        r += 1
        # the guided tour (gui_tour), for a first-time user; here rather than
        # auto-started, because Home is where the app opens
        if hasattr(self, "start_tour"):
            tour = ttk.Button(parent, text="Take the tour  →",
                              style="Link.TButton", command=self.start_tour)
            tour.grid(row=r, column=0, sticky="w", pady=(0, GAP))
            gui_tips.add_tip(tour, "A two-minute walk along the top row, "
                                   "from printer setup to saving a print.")
            r += 1

        # -- the guided path ---------------------------------------------- #
        # The numbered steps are the top row, in the order a print actually
        # happens. The recommended one is highlighted by _refresh_home. Each row
        # jumps to its page via the shell's _nav_select.
        gui_widgets.section_header(parent, "Get started").grid(
            row=r, column=0, sticky="w", pady=(0, HEAD))
        r += 1
        steps = ttk.Frame(parent)
        steps.grid(row=r, column=0, sticky="ew", pady=(0, GAP))
        steps.columnconfigure(0, weight=1)
        r += 1

        self._home_steps = {}
        step_defs = [
            ("machine", "Set up your printer",
             "Set the collector, G-code zero and g-code flavor, then save a "
             "printer preset to finish. On Physics: gap, voltage, pressure, "
             "temperature."),
            ("calibration", "Calibrate to your printer",
             "Print a test pattern, measure it, and press Apply. Optional for "
             "a first print: until then the app uses the lab's measured "
             "values."),
            ("gcode", "Design your print",
             "Build the scaffold on the Design tab: Parameters sets its "
             "size, layers, layer angles and fiber spacing. Then press Generate "
             "in the bottom row."),
            ("view", "Check it in Simulation",
             "Watch the printhead lay the fiber in 3-D, then save the g-code "
             "or send it to the printer from the bottom row."),
        ]
        for i, (pid, title, hint) in enumerate(step_defs):
            if i:
                gui_widgets.hairline(steps).grid(
                    row=2 * i - 1, column=0, sticky="ew")
            row_w = gui_widgets.StepRow(
                steps, i + 1, title, hint,
                command=lambda t=pid: self._nav_select(t))
            row_w.grid(row=2 * i, column=0, sticky="ew")
            self._home_steps[pid] = row_w

        # -- more tools (only the ones this build actually has) ------------ #
        # The optional destinations, kept OFF the golden path. Feature-gated exactly
        # like the shell gates the matching pages, so a row never points at a page
        # that was not built.
        # The process settings are step 1's second page and the card-based
        # Scaffold designer is legacy, so neither is offered here: a newcomer
        # is never led off the path or into the older form.
        tools = []
        if hasattr(self, "_gce_open_file"):
            tools.append(
                ("open_gcode", "Open a g-code file you already have",
                 "See how its fiber lands, edit it on the Design tab, and "
                 "send it to the printer."))
        if hasattr(self, "_build_machine_link_tab"):
            tools.append(
                ("machine_link", "Printer link",
                 "Send prints to your printer's Klipper over the network, "
                 "watch it move, set temperatures and jog."))
        if tools:
            gui_widgets.section_header(parent, "More tools").grid(
                row=r, column=0, sticky="w", pady=(0, HEAD))
            r += 1
            holder = ttk.Frame(parent)
            holder.grid(row=r, column=0, sticky="ew", pady=(0, GAP))
            holder.columnconfigure(0, weight=1)
            r += 1
            for i, (pid, title, hint) in enumerate(tools):
                if i:
                    gui_widgets.hairline(holder).grid(
                        row=2 * i - 1, column=0, sticky="ew")
                gui_widgets.ActionRow(
                    holder, title, hint,
                    command=lambda t=pid: self._home_tool(t)).grid(
                    row=2 * i, column=0, sticky="ew")

        # -- what survives a restart ---------------------------------------- #
        # Reported: a stranger could not tell what the app keeps when it
        # closes. Each sentence is what the code does: a printer or physics
        # preset set as default loads at startup (gui_presets),
        # Apply keeps a calibration (gui_calibration._cal_keep_applied), the
        # Design page asks before closing on unsaved work (gui_gcode_work), and
        # generated files live in a scratch folder removed on close.
        gui_widgets.section_header(parent, "What the app keeps").grid(
            row=r, column=0, sticky="w", pady=(0, HEAD))
        r += 1
        ttk.Label(
            parent, style="Hint.TLabel", justify="left", wraplength=680,
            text="Your default printer preset and your calibration load at "
                 "every start. A design is kept when you save it (Ctrl+S on "
                 "Design), and the app asks before closing on unsaved work. "
                 "Generated g-code is temporary until you press Save g-code "
                 "as… in the bottom row.").grid(
            row=r, column=0, sticky="w", pady=(0, GAP))
        r += 1

        # -- footer: one quiet keyboard line (no font report / dev path) --- #
        # REFERENCE text, not help for a control: the shortcuts are read, not
        # hovered, so the line stays and is tagged as such
        gui_form.mark(ttk.Label(
            parent, style="Hint.TLabel", justify="left",
            text="Ctrl+G  Generate      ·      Esc  Cancel      ·      "
                 "Ctrl+F  Find a setting (in the g-code text, find text)"
                 "      ·      "
                 "Ctrl+D  Dark mode      ·      Ctrl+1–6  Jump along the top row"),
            "reference").grid(row=r, column=0, sticky="w", pady=(GAP, 4))

    def _home_tool(self, pid):
        """A More tools row: go to its page; the g-code row also opens the
        file picker, since that is what the row promises."""
        if pid == "open_gcode":
            self._nav_select("gcode")
            self._gce_open_file()
            return
        self._nav_select(pid)

    # -- where each step stands --------------------------------------------- #
    def _home_setup_state(self):
        """(done, line) for step 1. Done once a printer preset of the user's
        own loads at startup and nothing the first print needs is missing:
        the direction page's "preset saved". The example printer that ships
        with the app is never "done", since it describes the lab's printer."""
        st = getattr(self, "_pstores", {}).get("machine")
        if st is None:
            return False, ""
        mode = getattr(self, "s_collector", None)
        mode = mode.get() if mode is not None else "plate"
        name = st["defaults"].get(mode if None not in st["defaults"] else None)
        own = bool(name) and not getattr(
            self, "_preset_is_shipped", lambda *_a: True)("machine", name)
        missing = []
        if hasattr(self, "_setup_steps"):
            try:
                import machine_setup as ms
                missing = [it.label for s in self._setup_steps()
                           if s.key in ("collector", "gcode")
                           for it in s.items if it.state == ms.MISSING]
            except Exception:  # noqa: BLE001 -- Home must always draw
                missing = []
        if missing:
            return False, "Still to set: " + ", ".join(missing) + "."
        if own:
            return True, "Printer preset “%s” loads at every start." % name
        if "setup" in getattr(self, "_nav_seen", ()):
            return False, ("Not saved yet: save your printer under Presets "
                           "on Setup, then Set as default.")
        return False, ""

    def _home_calibrate_state(self):
        """(done, line) for step 2: done once any Physics value is one
        Calibrate applied and it still holds."""
        rec = getattr(self, "_cal_measured_record", lambda: {})()
        is_meas = getattr(self, "_cal_is_measured", None)
        if not rec or is_meas is None:
            return False, ""
        names = []
        try:
            import mew_calibration
            label = {fs.key: fs.label
                     for fs in mew_calibration.CALIBRATION_FIELDS}
        except Exception:  # noqa: BLE001
            label = {}
        for key in rec:
            try:
                if is_meas(key):
                    names.append(label.get(key, key))
            except Exception:  # noqa: BLE001
                pass
        if not names:
            return False, ""
        return True, "Measured on your printer: " + ", ".join(names) + "."

    def _refresh_home(self):
        """Refresh Home's live bits each time it is shown: a ✓ and one status
        line on each step that is done, and the recommended next step
        highlighted. Every step stays clickable at all times.

        Reported by a blind tester: the steps never showed what was done, so
        Home could not answer "where am I". Next is the first step not done,
        except Calibrate, which is optional: once it has been visited, or a
        design has been generated, it is passed over."""
        steps = getattr(self, "_home_steps", None)
        if not steps:
            return
        have = bool(self.last_gcode and os.path.isfile(self.last_gcode))
        seen = getattr(self, "_nav_seen", ())
        state = {
            "machine": self._home_setup_state(),
            "calibration": self._home_calibrate_state(),
            "gcode": (have, ("Generated: %s." % os.path.basename(
                self.last_gcode)) if have else ""),
            "view": (have and getattr(self, "_home_sim_seen", None)
                     == self.last_gcode,
                     "Checked in Simulation." if have and getattr(
                         self, "_home_sim_seen", None) == self.last_gcode
                     else ""),
        }
        if not state["calibration"][0] and not state["calibration"][1] \
                and "calibrate" in seen:
            state["calibration"] = (False, "Skipped for now: the lab's "
                                           "measured values are in use.")
        for pid, row in steps.items():
            done, line = state.get(pid, (False, ""))
            row.set_state(done, line)
            row.set_accent(False)
        skippable = have or "calibrate" in seen
        # a fresh result not yet looked at outranks an unsaved printer preset:
        # it is what the user just made
        if have and not state["view"][0]:
            nxt = "view"
        else:
            nxt = next((p for p in steps if not state[p][0]
                        and not (p == "calibration" and skippable)), None)
        if nxt is None:
            return
        if nxt == "view":
            tag = "Ready"
        elif nxt == next(iter(steps)) and not any(s[0] for s in state.values()):
            tag = "Start here"
        else:
            tag = "Next"
        steps[nxt].set_accent(True, tag=tag)
