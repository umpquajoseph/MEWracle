#!/usr/bin/env python3
"""gui_fiber_target.py -- MODERN-UI ONLY: Fiber target, the process settings
that make the fiber a researcher asks for.

A researcher starts from the scaffold they want ("15 um fibers"), not from
machine settings. The panel runs the app's own physics backwards
(`mew_target`, which bisects `MEWModel.solve` itself) and shows the answer as
ONE chart with all three knobs on it:

  * **Speed (x) by pressure (y), one line per melt temperature.** At a held
    temperature and needle the settings that give the target diameter are a
    curve through the origin (a straight line with the shipped laws, P ~ v).
    Temperature only changes its slope, through the melt viscosity eta(T), so
    each temperature is one line, labelled at its end ("90 °C"). The
    temperatures are round steps across the printer's melt-temperature limits
    plus the current one. Any point on any line prints the fiber.
  * **Grey only where you cannot print**: below CTS the jet coils and the
    diameter law does not hold, and past the printer's process limits. A line
    is dashed there.
  * **Two dots**: the current settings (a ring, "now") and the answer (the
    accent dot). A jet-lag target fixes the speed, so it is a vertical line
    and its crossing with a temperature line is the answer.
  * **Solve for** Pressure / Speed / Temperature moves that one knob; the
    other two are HELD, at the value typed in their Hold field (pressure,
    temperature, speed, beside the targets: "solve it at 170 °C") or where
    they are now when the field is blank. Blank fields show the current
    value greyed, and the solved knob's field is off and shows the answer.
    A click on a line types that point's speed and temperature into their
    fields and solves the pressure, so a pick is the same rule and the
    fields always say what the answer holds (`_ft_solve`).

Text is two rows: the inputs above the chart (fiber diameter, optional jet
lag, the holds, needle, Solve for) and the result below it, with Apply and
Put back.
Temperature is never greyed: pressure and speed rest on the same viscosity
as temperature does, so the row says where the viscosity and Ea came from in
the Physics page's words ("(default)" while they are the lab's), with a jump
to Calibrate.

Apply writes the pressure / temperature to the Physics page, the needle bore
to its Material card, and the speed to the chosen scaffold(s) of the G-code
window's design. With "Fiber diameter from physics" on (the default) the
diameter then follows by itself; with it off, Apply also writes the target
into the scaffold's typed diameter. "Put back" undoes it. Every
scaffold in scope gets the answer's speed, so "All scaffolds" leaves none at
another speed (pressure and temperature are one setting for the print).

ON A MANDREL (4-axis) each page scaffold is a tube, and its Speed row is the
deposition-SURFACE speed, the scalar the diameter and CTS laws take, so the
panel solves and applies it the same way; the chart's axis says "Surface
speed". The rig's Advanced settings can make that dishonest, and the panel
says so instead of writing a wrong value (`_ft_mandrel_speed`): fixed Feeds
on a split-feed flavor (F and E set every move, so the Speed row prints
nothing) offers no answer; the omega speed policy (the surface speed rises
layer by layer) is a note that the answer is the first layer's.

Reachable from both places a researcher looks for it: the G-code window's
ribbon (Modify -> Fiber target, in the page's floating edit box, NARROW) and
the Physics page (its own card, WIDE). One panel class, built in each; both
read and write the same App vars, so the two can never disagree.

The printer's PROCESS LIMITS (pressure, print speed, melt temperature) are
rig facts, so they live on Setup > Printer model beside the acceleration
limits and ride the printer preset (`_ft_range_vars`; persist keys
`machine.range_<knob>_<min|max>`). An answer outside them is shown as out of
reach, with a link to them.

LAYOUT. The chart is the point of the panel, so it must be on screen when
the panel opens, at 1366x768 too: `_TargetChart.fit()` sizes it to the room
left in its scrolling ancestor, between a floor and its full height."""
from gui_common import *
import math
import types

import gui_fonts
import gui_form
import gui_tips
import gui_units
import gui_widgets

try:
    import mew_target
    from mew_physics import Nozzle  # noqa: F401  (the physics is importable)
    _HAVE = True
except Exception:  # noqa: BLE001
    _HAVE = False

PSI_TO_PA = 6894.76
#: canonical units: psi, mm/min, degC. Generic until the user sets theirs.
DEFAULT_RANGES = {"pressure": (0.5, 15.0), "speed": (60.0, 3000.0),
                  "temperature": (70.0, 110.0)}
#: how near the current settings must already be to say so, fraction
_ALREADY = 0.005
#: the Solve-for choices, (value, label)
SOLVE_FOR = (("pressure", "Pressure"), ("speed", "Speed"),
             ("temperature", "Temperature"))
#: a held knob, in a sentence
_HOLD_WORD = {"pressure": "pressure", "speed": "speed",
              "temperature": "melt temperature"}
#: the most temperature lines the chart draws (the current one aside)
_MAX_LINES = 6


def _hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a, b, t):
    a, b = _hex(a), _hex(b)
    return "#%02x%02x%02x" % tuple(int(round(x + (y - x) * t))
                                   for x, y in zip(a, b))


def _nice_ticks(lo, hi, n=5):
    span = hi - lo
    if not span > 0:
        return [lo]
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = mag
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if span / step <= n:
            break
    t = math.ceil(lo / step - 1e-9) * step
    out = []
    while t <= hi + 1e-9 * step:
        out.append(round(t, 10))
        t += step
    return out


def _g(x, sig=3):
    """A number for a sentence: `sig` significant figures, no exponent."""
    if x is None or not math.isfinite(x):
        return "–"
    s = "%.*g" % (sig, x)
    if "e" in s:
        s = ("%.*f" % (max(0, sig - 1 - int(math.floor(math.log10(abs(x))))),
                       x)) if x else "0"
    return s


def temperature_steps(lo, hi, now=None):
    """The chart's line temperatures: round steps across [lo, hi] -- the
    smallest of 5/10/20/25/50 °C that gives at most `_MAX_LINES` -- plus
    `now` when it is not already one of them. Ascending."""
    out = []
    if hi > lo:
        for step in (5.0, 10.0, 20.0, 25.0, 50.0):
            a = math.ceil(lo / step - 1e-9)
            b = math.floor(hi / step + 1e-9)
            if b - a + 1 <= _MAX_LINES:
                out = [k * step for k in range(int(a), int(b) + 1)]
                break
        if not out:
            out = [lo, hi]
    if now is not None and math.isfinite(now) \
            and all(abs(now - t) > 0.05 for t in out):
        out.append(now)
    return sorted(out)


def _same(s, t):
    """Two Settings equal to the four figures Apply writes."""
    return (abs(s.pressure - t.pressure) <= 1e-3 * max(t.pressure, 1.0)
            and abs(s.speed - t.speed) <= 1e-3 * max(t.speed, 1e-6)
            and abs(s.temperature - t.temperature) <= 0.05
            and abs(s.bore - t.bore) <= 1e-9)


class _Answer:
    """The answer: the settings, the jet they give, and whether it is offered
    (`ok`) or why not (`why`, `limit` naming the process limit it broke)."""

    def __init__(self, key, settings=None, jet=None, ok=False, why=""):
        self.key = key
        self.settings, self.jet = settings, jet
        self.ok, self.why = ok, why
        self.limit = None


class _FiberTargetMixin:

    # ------------------------------------------------------------------ #
    # state shared by every panel
    # ------------------------------------------------------------------ #
    def _ft_vars(self):
        st = self.__dict__.get("_ft")
        if st is not None:
            return st
        st = self.__dict__["_ft"] = {
            "d": tk.StringVar(value=""),          # target diameter, mm
            "lag": tk.StringVar(value=""),        # target lag, mm; blank: none
            "solve": tk.StringVar(value="pressure"),  # SOLVE_FOR
            # the knobs held while the solved one moves, canonical units
            # (psi, mm/min, degC); blank holds it where it is now
            "hold_pressure": tk.StringVar(value=""),
            "hold_speed": tk.StringVar(value=""),
            "hold_temperature": tk.StringVar(value=""),
            "needle": tk.StringVar(value=""),     # combobox text
            "scope": tk.StringVar(value=""),      # "Scaffold n" / "All scaffolds"
            "undo": None,
            "rev": tk.IntVar(value=0),            # bumped to make panels redraw
        }
        # the printer's limits are the Printer model card's vars, the same
        # objects: the panel never keeps a copy of its own
        for (knob, end), var in self._ft_range_vars().items():
            st["r_%s_%s" % (knob, end)] = var
        return st

    def _ft_range_vars(self):
        """{(knob, "lo"|"hi"): StringVar} -- the printer's PROCESS LIMITS.

        Fields of Setup > Printer & collector > Printer model ("Process
        limits"), saved with the printer preset (`_ft_machine_keys`) and the
        session (`_ft_persist_entries`). They are rig facts -- what the
        regulator, the stage and the heater can hold -- so they sit with the
        acceleration limits, not in the panel that reads them. Canonical
        units psi, mm/min, degC; the form shows the user's units."""
        rv = self.__dict__.get("_ft_rv")
        if rv is None:
            rv = self.__dict__["_ft_rv"] = {}
            for knob, (lo, hi) in DEFAULT_RANGES.items():
                rv[(knob, "lo")] = tk.StringVar(value=_g(lo, 6))
                rv[(knob, "hi")] = tk.StringVar(value=_g(hi, 6))
        return rv

    @staticmethod
    def _ft_range_key(knob, end):
        return "machine.range_%s_%s" % (knob, "min" if end == "lo" else "max")

    def _ft_persist_entries(self):
        return {self._ft_range_key(k, e): v
                for (k, e), v in self._ft_range_vars().items()}

    def _ft_machine_keys(self):
        """The persist keys the MACHINE preset owns (gui_buildplate)."""
        return tuple(self._ft_range_key(k, e) for k in DEFAULT_RANGES
                     for e in ("lo", "hi"))

    def _build_machine_process_limits(self, parent, row):
        """The Printer model card's "Process limits" block, from `row` of
        `parent`: one row per knob, lowest and highest side by side."""
        rv = self._ft_range_vars()
        F = gui_form.Form(parent, row=row)
        F.section("Process limits", first=(row == 0))
        first = None
        for knob, label, unit, tip in (
                ("pressure", "Pressure", "psi",
                 "The air pressures your regulator holds steadily."),
                ("speed", "Print speed", "mm/min",
                 "The stage speeds your printer moves at cleanly."),
                ("temperature", "Melt temperature", "°C",
                 "The melt temperatures your heater holds.")):
            tip += (" Fiber target (Physics page, and the G-code window's "
                    "ribbon) shows a setting outside this range as out of "
                    "reach instead of offering it.")
            r = F.entry(label + ", from", rv[(knob, "lo")], tip, unit=unit)
            r.add_entry("to", rv[(knob, "hi")], tip, unit=unit)
            if first is None:
                first = r.field
        F.live(None, text=("Generic values until you set your printer's. "
                           "They ride the printer preset."))
        focus = getattr(self, "_setup_focus", None)
        if isinstance(focus, dict) and first is not None:
            focus["process_limits"] = first
        return F

    def _ft_open_limits(self):
        """The panel's "Change…": the Printer model card, on the limits."""
        try:
            self._nav_select("machine")
            self._setup_go("machine", "process_limits")
        except Exception:  # noqa: BLE001
            return
        field = getattr(self, "_setup_focus", {}).get("process_limits")
        if field is None:
            return

        def _show():
            # the rows sit under the STL and acceleration rows, below the
            # fold of the card at 1366x768: focus alone left them unseen
            try:
                from gui_finder import _scroll_into_view
                _scroll_into_view(field)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.root.after(60, _show)
        except (tk.TclError, AttributeError):
            _show()

    def _ft_ranges(self):
        """{knob: (lo, hi)} canonical (psi, mm/min, degC); a bad field falls
        back to the default for that knob."""
        st = self._ft_vars()
        out = {}
        for knob, dflt in DEFAULT_RANGES.items():
            try:
                lo = float(st["r_%s_lo" % knob].get())
                hi = float(st["r_%s_hi" % knob].get())
                if not (math.isfinite(lo) and math.isfinite(hi) and hi > lo
                        and (lo >= 0 or knob == "temperature")):
                    raise ValueError
            except (ValueError, tk.TclError):
                lo, hi = dflt
            out[knob] = (lo, hi)
        return out

    # ------------------------------------------------------------------ #
    # the model, the current settings, the scaffolds
    # ------------------------------------------------------------------ #
    def _ft_page_scaffolds(self):
        try:
            n = self._gce_scaffold_count()
            sv = self.gce_scaf_vars
            return sv[:max(1, min(n, len(sv)))]
        except Exception:  # noqa: BLE001
            return list(getattr(self, "scaf_vars", [])[:1])

    def _ft_scope_indices(self):
        """The page scaffolds Apply sets the speed of."""
        sv = self._ft_page_scaffolds()
        text = self._ft_vars()["scope"].get()
        if text == "All scaffolds":
            return list(range(len(sv)))
        try:
            i = int(text.split()[-1]) - 1
        except (ValueError, IndexError):
            i = 0
        return [max(0, min(i, len(sv) - 1))]

    def _ft_scope_even(self):
        """True when every scaffold in scope is at the same speed (one
        scaffold always is). The answer is worked at the first one's."""
        sv = self._ft_page_scaffolds()
        speeds = set()
        for i in self._ft_scope_indices():
            try:
                speeds.add(round(float(sv[i]["speed"].get()), 6))
            except (ValueError, tk.TclError, KeyError, IndexError):
                return False
        return len(speeds) <= 1

    def _ft_scope_values(self):
        n = len(self._ft_page_scaffolds())
        vals = ["Scaffold %d" % (i + 1) for i in range(n)]
        return vals + (["All scaffolds"] if n > 1 else [])

    def _ft_context(self):
        """(TargetModel, Settings now) or (None, reason)."""
        if not _HAVE:
            return None, "The physics modules are not installed."
        try:
            models = self._calibration_models()
        except Exception:  # noqa: BLE001
            models = None
        if models is None:
            return None, "The Physics page's values are not readable."
        material, nozzle, calibration = models
        try:
            gap = float(self.sim_standoff.get())
            volt = float(self.sim_voltage.get())
            p = float(self.sim_pressure.get()) * PSI_TO_PA
            t = float(self.sim_temperature.get())
        except (ValueError, tk.TclError, AttributeError):
            return None, ("Fill in the Physics page's Process parameters "
                          "(gap, voltage, pressure, temperature).")
        sv = self._ft_page_scaffolds()
        i = self._ft_scope_indices()[0]
        try:
            v = float(sv[i]["speed"].get()) / 60.0
        except (ValueError, tk.TclError, KeyError, IndexError):
            return None, "Scaffold %d's print speed is not a number." % (i + 1)
        if not (gap > 0 and v > 0):
            return None, "The gap and the print speed must be above zero."
        try:
            tm = mew_target.TargetModel(material, nozzle, calibration, gap,
                                        volt)
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)
        return tm, mew_target.Settings(p, v, t, nozzle.inner_radius)

    def _ft_provenance(self):
        """Where the melt viscosity and Ea came from, in the Physics page's
        words ("default", "measured", "from preset", "edited"), or ""."""
        fn = getattr(self, "_cal_provenance", None)
        if fn is None:
            return ""
        try:
            return fn("cal.mat_viscosity_ref", "cal.mat_activation_energy")
        except Exception:  # noqa: BLE001
            return ""

    def _ft_needles(self, tm, now):
        """[(label, needle radius mm)]: the current needle first, then stock
        gauges, each saying the pressure it needs at the current speed."""
        out = [("Current · %s mm inner diameter" % _g(2 * now.bore, 3),
                now.bore)]
        d = self._ft_target_d()
        p_unit = gui_units.unit("pressure")
        for gauge, idia in mew_target.NEEDLE_GAUGES:
            r = idia / 2.0
            if abs(r - now.bore) < 1e-6:
                continue
            need = ""
            if d is not None and tm is not None:
                s = tm.pressure_for(d, now.speed, now.temperature, r)
                if s is not None:
                    need = " · %s %s" % (_g(self._ft_p_show(s.pressure)),
                                         p_unit)
            out.append(("%d G · %s mm inner diameter%s"
                        % (gauge, _g(idia, 3), need), r))
        return out

    def _ft_needle_bore(self, tm, now):
        text = self._ft_vars()["needle"].get()
        for label, r in self._ft_needles(tm, now):
            if text and text.split(" · ")[0] == label.split(" · ")[0]:
                return r
        return now.bore

    # -- display units ------------------------------------------------------ #
    @staticmethod
    def _ft_p_show(pa):
        return pa / PSI_TO_PA * gui_units.factor(
            "pressure", "psi", gui_units.unit("pressure"))

    @staticmethod
    def _ft_v_show(mm_s):
        return mm_s * 60.0 * gui_units.factor(
            "speed", "mm/min", gui_units.unit("speed"))

    @staticmethod
    def _ft_d_show(mm):
        return mm * gui_units.factor("fiber_d", "mm", gui_units.unit("fiber_d"))

    def _ft_target_d(self):
        try:
            d = float(self._ft_vars()["d"].get())
        except (ValueError, tk.TclError):
            return None
        return d if math.isfinite(d) and d > 0 else None

    def _ft_target_lag(self):
        """The jet-lag target, mm: None when blank, NaN when not a number."""
        text = self._ft_vars()["lag"].get().strip()
        if not text:
            return None
        try:
            v = float(text)
        except ValueError:
            return float("nan")
        return v if math.isfinite(v) and v > 0 else float("nan")

    def _ft_holds(self):
        """({knob: value or None}, why): the typed hold fields in the
        model's units (Pa, mm/s, degC), None where blank. `why` names the
        first field that is not a number, else ""."""
        st = self._ft_vars()
        out = {}
        for knob, to_model, word in (
                ("pressure", lambda x: x * PSI_TO_PA, "pressure"),
                ("speed", lambda x: x / 60.0, "print speed"),
                ("temperature", lambda x: x, "melt temperature")):
            text = st["hold_" + knob].get().strip()
            if not text:
                out[knob] = None
                continue
            try:
                v = float(text)
            except ValueError:
                v = float("nan")
            if not math.isfinite(v) or (knob != "temperature" and v <= 0):
                return out, ("Type the %s to hold as a number above zero, or "
                             "leave it blank to hold the current one." % word)
            out[knob] = to_model(v)
        return out, ""

    def _ft_on_mandrel(self):
        try:
            return self.s_collector.get() == "mandrel"
        except (AttributeError, tk.TclError):
            return False

    def _ft_speed_word(self):
        """What the speed is called where it is written: on a mandrel it is
        the tube's surface speed (its Speed row), not an X-axis rate."""
        return "surface speed" if self._ft_on_mandrel() else "print speed"

    def _ft_mandrel_speed(self):
        """(why_not, note) for a 4-axis (mandrel) design, ("", "") otherwise.

        A tube's Speed row is the deposition-SURFACE speed -- the scalar the
        diameter and CTS laws take (`scaffold_gcode.
        generate_mandrel_blocks_gcode`), so the panel solves it as it solves
        a plate's print speed. Two of the rig's
        settings (Advanced) change what that row means:

          * Feeds "fixed" on a printer whose flavor splits the feed
            (Aerotech): the typed F and E set every move and the Speed row
            sets none, so no speed the panel solves or holds is the one
            printed -> `why_not`, and nothing is offered.
          * Speed policy "omega": the angular rate is held, so the surface
            speed is the Speed row on the first layer and rises by
            (R0 + h)/R0 outward; the answer is the first layer's -> `note`."""
        if not self._ft_on_mandrel():
            return "", ""
        why = note = ""
        try:
            if self.m_feed_mode.get() == "fixed":
                import mew_gcode
                if getattr(self, "gce_flavor", None) is not None:
                    name = self.gce_flavor_name()
                else:
                    name = self._machine_flavor_name()
                if mew_gcode.get_flavor(name).rotary_feed == "split":
                    why = ("Feeds are fixed (Advanced): your own F and E set "
                           "every move of the tube, so its Speed is not what "
                           "prints and no answer here would be. Set Feeds to "
                           "auto to use Fiber target.")
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.m_speed_mode.get() == "omega":
                note = ("Speed policy is omega (Advanced): this is the first "
                        "layer's surface speed. Each layer out runs faster, "
                        "by (R0 + h)/R0, so the outer layers print thinner "
                        "than the target.")
        except (AttributeError, tk.TclError):
            pass
        return why, note

    # ------------------------------------------------------------------ #
    # the answer
    # ------------------------------------------------------------------ #
    def _ft_judge(self, tm, a, ranges, typed=()):
        """Fill in `a.jet`, `a.ok`, `a.why` for its settings. `typed` names
        the knobs the researcher typed a hold for: a limit one of those
        breaks is said as theirs, not as the answer's."""
        s = a.settings
        a.jet = tm.jet(s)
        if a.jet is None:
            a.why = "the model has no answer there"
            return a
        r = mew_target.Ranges(
            pressure=tuple(x * PSI_TO_PA for x in ranges["pressure"]),
            speed=tuple(x / 60.0 for x in ranges["speed"]),
            temperature=ranges["temperature"])
        out = r.why_outside(s)
        for knob in typed:
            # a typed value off the printer is said first, before anything
            # the answer does there
            lo, hi = getattr(r, knob)
            val = getattr(s, knob)
            if lo * (1 - 1e-9) <= val <= hi * (1 + 1e-9):
                continue
            a.limit = knob
            show = self._ft_show(knob)
            a.why = ("the %s you typed, %s %s, is outside your printer's "
                     "%s–%s %s" % (
                         _HOLD_WORD[knob], _g(show[0](getattr(s, knob))),
                         show[1], _g(show[0](lo)), _g(show[0](hi)), show[1]))
            return a
        if not a.jet.straight_fibre:
            a.why = ("%s %s is below CTS (%s %s), where the fiber coils"
                     % (_g(self._ft_v_show(s.speed)), gui_units.unit("speed"),
                        _g(self._ft_v_show(a.jet.critical_speed)),
                        gui_units.unit("speed")))
            return a
        if out is not None:
            knob, lo, hi = out
            a.limit = knob
            show = self._ft_show(knob)
            val = getattr(s, knob)
            a.why = ("needs %s %s; your printer's range is %s–%s %s"
                     % (_g(show[0](val)), show[1], _g(show[0](lo)),
                        _g(show[0](hi)), show[1]))
            return a
        a.ok, a.why = True, ""
        return a

    def _ft_show(self, knob):
        """(model value -> shown value, unit) for one knob."""
        return {"pressure": (self._ft_p_show, gui_units.unit("pressure")),
                "speed": (self._ft_v_show, gui_units.unit("speed")),
                "temperature": (lambda x: x, "°C")}[knob]

    def _ft_solve(self):
        """Everything a panel draws, as one namespace.

        ONE rule decides every answer: the Solve-for knob moves, and the
        other two are HELD -- at the value typed in its hold field, or where
        it is now when that field is blank. A jet-lag target sets the speed,
        so with one the speed is neither held nor solved. A click on a chart
        line is the same rule: it types that point's speed and temperature
        into their fields and solves the pressure (`FiberTargetPanel.pick`),
        so the fields always say what the answer holds."""
        R = types.SimpleNamespace(
            tm=None, now=None, why="", d=None, lag=None, lag_v=None,
            bore=None, ranges=self._ft_ranges(), chosen=None, already=False,
            temps=[], hl_t=None, mode="", now_jet=None, held=None,
            typed=(), note="")
        lock, R.note = self._ft_mandrel_speed()
        tm, now = self._ft_context()
        if tm is None:
            R.why = now
            return R
        R.tm, R.now = tm, now
        R.bore = bore = self._ft_needle_bore(tm, now)
        R.now_jet = tm.jet(now)
        R.temps = temperature_steps(*R.ranges["temperature"],
                                    now=now.temperature)
        R.hl_t = now.temperature
        st = self._ft_vars()
        mode = st["solve"].get()
        if mode not in dict(SOLVE_FOR):
            mode = "pressure"
        if lock:
            R.why = lock
            return R
        d = R.d = self._ft_target_d()
        if d is None:
            R.why = "Type the fiber diameter you want."
            return R
        lag = R.lag = self._ft_target_lag()
        if lag is not None and not math.isfinite(lag):
            R.why = "Type the jet lag in mm, or leave it blank."
            return R
        if lag is not None and mode == "speed":
            mode = "pressure"            # the lag sets the speed
        R.mode = mode
        holds, bad = self._ft_holds()
        if bad:
            R.why = bad
            return R
        holds[mode] = None               # the solved knob is never held
        if lag is not None:
            holds["speed"] = None        # nor is the speed under a lag
        R.typed = tuple(k for k in ("pressure", "speed", "temperature")
                        if holds.get(k) is not None)
        p = holds["pressure"] if holds.get("pressure") is not None \
            else now.pressure
        v = holds["speed"] if holds.get("speed") is not None else now.speed
        t = holds["temperature"] if holds.get("temperature") is not None \
            else now.temperature
        R.held = mew_target.Settings(p, v, t, bore)
        R.hl_t = t
        if all(abs(t - x) > 0.05 for x in R.temps):
            R.temps = sorted(R.temps + [t])
        if lag is not None:
            R.lag_v = tm.speed_for_lag(lag, p, t, bore)
            if R.lag_v is None:
                R.why = ("The jet lag levels off at %s mm at this %s mm gap: "
                         "ask for less, or raise the gap on the Physics page."
                         % (_g(tm.lag_limit()), _g(tm.gap)))
                return R
        a = _Answer(mode)
        if mode == "pressure":
            s = (tm.pressure_for(d, v, t, bore) if lag is None else
                 tm.lag_and_diameter(d, lag, t, bore, p))
        elif mode == "speed":
            s = tm.speed_for(d, p, t, bore)
        else:
            s = tm.temperature_for(d, p, v if lag is None else R.lag_v,
                                   bore)
        if s is None:
            a.why = {"pressure": "no pressure gives it",
                     "speed": "no %s gives it" % self._ft_speed_word(),
                     "temperature": "no melt temperature gives it"}[mode]
            a.why += (" with the %s you typed" % " and ".join(
                _HOLD_WORD[k] for k in R.typed)
                if R.typed else " with the rest held")
        else:
            a.settings = s
            self._ft_judge(tm, a, R.ranges, typed=R.typed)
            R.hl_t = s.temperature
            if lag is not None:
                R.lag_v = s.speed
            if all(abs(s.temperature - x) > 0.05 for x in R.temps):
                R.temps = sorted(R.temps + [s.temperature])
        R.chosen = a
        if not self._ft_scope_even():
            pass                     # Apply still has scaffolds to bring in
        elif a.settings is not None and a.ok and _same(a.settings, now):
            R.already = True         # e.g. just applied
        elif not R.typed and R.now_jet is not None and bore == now.bore \
                and R.now_jet.straight_fibre \
                and abs(R.now_jet.fiber_diameter - d) <= _ALREADY * d \
                and (lag is None or abs(R.now_jet.lag_distance - lag)
                     <= _ALREADY * lag):
            R.already = True
        return R

    # ------------------------------------------------------------------ #
    # apply, and put back
    # ------------------------------------------------------------------ #
    def _ft_apply(self, R):
        a = R.chosen
        if a is None or not a.ok:
            return
        s, now = a.settings, R.now
        st = self._ft_vars()
        sv = self._ft_page_scaffolds()
        idx = self._ft_scope_indices()
        undo = []

        def put(var, text):
            undo.append((var, var.get()))
            var.set(text)
        said = []
        if abs(s.pressure - now.pressure) > 1e-4 * max(now.pressure, 1.0):
            put(self.sim_pressure, "%.4g" % (s.pressure / PSI_TO_PA))
            said.append("pressure %s %s" % (_g(self._ft_p_show(s.pressure)),
                                           gui_units.unit("pressure")))
        if abs(s.temperature - now.temperature) > 1e-6:
            put(self.sim_temperature, "%.4g" % s.temperature)
            said.append("temperature %s °C" % _g(s.temperature))
        if abs(s.bore - now.bore) > 1e-9:
            var = self.cal_vars.get("cal.noz_inner_diameter")
            if var is not None:
                put(var, "%.4g" % (2 * s.bore))
                said.append("needle inner diameter %s mm" % _g(2 * s.bore))
        # every scaffold in scope gets the answer's speed, not only those
        # still at the first one's: pressure and temperature are one setting
        # for the whole print, so a scaffold left at another speed would
        # print another fiber
        text = "%.4g" % (s.speed * 60.0)
        moved = False
        for i in idx:
            try:
                same = abs(float(sv[i]["speed"].get()) / 60.0 - s.speed) \
                    <= 1e-9 + 1e-4 * s.speed
            except (ValueError, tk.TclError):
                same = False
            if not same:
                put(sv[i]["speed"], text)
                moved = True
        # what went to the scaffolds is said "for" them; the rest is the
        # whole print's
        per = []
        if moved:
            per.append("%s %s %s" % (
                self._ft_speed_word(), _g(self._ft_v_show(s.speed)),
                gui_units.unit("speed")))
        if not self._diam_is_physics():
            for i in idx:
                put(sv[i]["diameter"], "%.4g" % R.d)
            per.append("fiber diameter %s %s" % (
                _g(self._ft_d_show(R.d)), gui_units.unit("fiber_d")))
        st["undo"] = undo
        who = ("all scaffolds" if len(idx) > 1 and len(idx) == len(sv)
               else "scaffold %d" % (idx[0] + 1))
        parts = []
        if said:
            parts.append(", ".join(said))
        if per:
            parts.append("%s for %s" % (", ".join(per), who))
        try:
            self.status.set("Fiber target: set %s. The G-code window "
                            "regenerates with it." % "; ".join(parts))
        except AttributeError:
            pass
        st["rev"].set(st["rev"].get() + 1)

    def _ft_put_back(self):
        st = self._ft_vars()
        undo, st["undo"] = st["undo"], None
        for var, val in reversed(undo or ()):
            try:
                var.set(val)
            except tk.TclError:
                pass
        try:
            self.status.set("Fiber target: put the settings back as they were.")
        except AttributeError:
            pass
        st["rev"].set(st["rev"].get() + 1)

    # ------------------------------------------------------------------ #
    # entry points
    # ------------------------------------------------------------------ #
    def _ft_open_cad(self):
        """The G-code window's ribbon tool: the panel in the page's floating
        edit box, applying to the selected scaffold."""
        if not hasattr(self, "_gce_host_open"):
            return
        host, body = self._gce_host_open("Fiber target")
        st = self._ft_vars()
        i = None
        try:
            i = self._gce_d_target()
        except Exception:  # noqa: BLE001
            pass
        if isinstance(i, int):
            st["scope"].set("Scaffold %d" % (i + 1))
        elif st["scope"].get() not in self._ft_scope_values():
            st["scope"].set("Scaffold 1")
        panel = FiberTargetPanel(self, body, wide=False)
        panel.grid(row=0, column=0, sticky="ew")
        body.columnconfigure(0, weight=1)
        close = ttk.Frame(host)
        close.grid(row=3, column=0, sticky="e", pady=(6, 0))
        ttk.Button(close, text="Close", command=self._gce_host_close).pack(
            side="right")
        try:
            self._gce_host_keys(host, ok=lambda: None,
                                esc=self._gce_host_close)
        except Exception:  # noqa: BLE001
            pass
        panel.focus_target()

    def _ft_build_physics_card(self, grid):
        """The Physics page's card: the same panel, the full page wide."""
        card = grid.add(
            "target", "Fiber target",
            info=("Type the fiber you want: every point on a line prints it, "
                  "one line per melt temperature."))
        card.body.columnconfigure(0, weight=1)
        self._ft_vars()["scope"].set("Scaffold 1")
        FiberTargetPanel(self, card.body, wide=True).grid(
            row=0, column=0, sticky="nsew")
        self._ft_card = card
        # the line also reads the G-code window's scaffold speeds, which come
        # and go with its scaffolds: settle it whenever the card is shown
        try:
            card.bind("<Map>", lambda _e: self._refresh_phys_summaries(),
                      add="+")
        except (AttributeError, tk.TclError):
            pass
        return card

    def _ft_summary_vars(self):
        """The vars the Physics card's one-line summary reads, beyond the
        Physics page's own (gui_pages watches these)."""
        st = self._ft_vars()
        out = [st["d"], st["lag"], st["solve"], st["needle"], st["scope"],
               st["rev"]]
        out += [st["hold_" + k] for k, _l in SOLVE_FOR]
        out += list(self._ft_range_vars().values())
        out += [getattr(self, n, None) for n in (
            "s_collector", "m_speed_mode", "m_feed_mode", "gcode_flavor")]
        return [v for v in out if v is not None]

    def _summary_phys_target(self):
        d = self._ft_target_d()
        if d is None:
            return "Type a fiber diameter; get the settings that make it"
        R = self._ft_solve()
        a = R.chosen
        dtxt = "%s %s" % (_g(self._ft_d_show(d)), gui_units.unit("fiber_d"))
        if a is None and R.why:
            # no answer was sought (e.g. fixed feeds on a mandrel): its
            # reason's head, not "out of reach"
            return "%s: %s" % (dtxt, R.why.split(". ")[0].split(":")[0])
        if a is None or not a.ok:
            return "%s: out of reach on this printer" % dtxt
        s = a.settings
        return "%s → %s %s at %s %s, %s °C" % (
            dtxt, _g(self._ft_p_show(s.pressure)), gui_units.unit("pressure"),
            _g(self._ft_v_show(s.speed)), gui_units.unit("speed"),
            _g(s.temperature))



# ====================================================================== #
# the panel
# ====================================================================== #
class FiberTargetPanel(ttk.Frame):
    """The Fiber target panel, built into any parent. Reads and writes the
    App's vars (`_FiberTargetMixin._ft_vars`), so two of them stay in step.

    Three rows: the inputs, the chart, the result. Wide (the Physics card)
    the targets and holds are one row and the needle and Solve for a second;
    narrow (the G-code window's edit box, ~530 px) the holds, needle and
    Solve for each take a row, and the result's buttons go under its
    numbers."""

    def __init__(self, app, parent, wide=False):
        super().__init__(parent)
        self.app = app
        self.wide = wide
        self.columnconfigure(0, weight=1)
        self._traces = []
        self._pending = None
        self._R = None
        st = app._ft_vars()
        sp = gui_fonts.SP
        # -- the inputs ----------------------------------------------------
        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="w")
        F = gui_form.Form(top)
        # the targets in one row, so the holds' row under them (narrow)
        # cannot push Jet lag out to the far edge of the box
        r = F.group("Target", None)
        self._d_entry = r.add_entry(
            "Fiber diameter", st["d"],
            "The deposited fiber diameter you want. It is set by the melt "
            "flow and the print speed: d = √(4Q/(πv)).", unit="mm")
        lag_tip = (
            "Optional: how far behind the nozzle you want the fiber to land. "
            "It depends on the print speed (through CTS and the gap), so a "
            "lag sets the speed: the chart draws it as a vertical line. It "
            "levels off at A × gap (the Jet lag card), so more is out of "
            "reach. Leave it blank for no lag target. On a mandrel this is "
            "the lag along the rod; around the rod it is r/(r + gap) of it.")
        self._lag_entry = r.add_entry("Jet lag", st["lag"], lag_tip,
                                      unit="mm")
        # the HELD knobs, right of the targets (wide) or on the row under
        # them (narrow): blank holds a knob where it is now, and the field
        # shows that value greyed; the solved knob's field is off and shows
        # the answer greyed (`_draw_holds`)
        hold_tip = (
            "Hold this setting at a value of your own while the Solve-for "
            "one moves, e.g. a melt temperature of 170 °C. Blank holds it "
            "where it is now (shown grey). The setting being solved is off "
            "and shows the answer.")
        if wide:
            hr = r
            hr.add_label("Hold", tip=hold_tip)
        else:
            hr = F.group("Hold", hold_tip)
        self._hold = {}
        for knob, label, unit in (("pressure", "Pressure", "psi"),
                                  ("temperature", "Temperature", "°C"),
                                  ("speed", "Speed", "mm/min")):
            e = hr.add_entry(label, st["hold_" + knob], hold_tip, unit=unit,
                             width=7)
            ph = gui_form.show_placeholder(e, st["hold_" + knob], "")
            self._hold[knob] = (e, ph)
        hr.add_button("Clear", self._clear_holds, tip=(
            "Empty the three fields: every held setting goes back to where "
            "it is now."), style="Link.TButton")
        r2 = F.group("Needle", None)
        needle_tip = (
            "The needle the chart and the answer use. Each stock gauge says "
            "the pressure it needs at your print speed. Inner diameters "
            "are nominal regular-wall sizes: measure yours, flow goes as its "
            "fourth power.")
        self._needle_cb = r2.add_combo("", st["needle"], (), needle_tip,
                                       width=30)
        if not wide:
            r2 = F.group("Solve for", None)
        else:
            r2.add_label("Solve for")
        self._bar = gui_widgets.SegmentedBar(
            r2.host(), SOLVE_FOR, st["solve"], command=self._solve_clicked)
        r2.add_widget(self._bar, tip=(
            "Which setting moves to give the fiber. The other two are held: "
            "at the value typed in their field, or where they are now when "
            "it is blank. A click on a line of the chart types that point's "
            "speed and temperature into their fields and solves the "
            "pressure."))
        # a mandrel note (the speed policy), under the inputs when it applies
        self._note = ttk.Label(top, style="Hint.TLabel", wraplength=500,
                               justify="left")
        gui_form.mark(self._note, "live")
        self._note_row = F.row
        F.row += 1
        self._needle_cb.bind("<<ComboboxSelected>>",
                             lambda _e: self._soon(), add="+")
        # -- the chart -----------------------------------------------------
        self._full_h = 640 if wide else 330
        self._min_h = 240 if wide else 220
        self.chart = _TargetChart(self, self, width=900 if wide else 520,
                                  height=self._full_h)
        self.chart.grid(row=1, column=0, sticky="ew", pady=(sp["xs"], 0))
        # -- the result ----------------------------------------------------
        bot = self._bot = ttk.Frame(self)
        bot.grid(row=2, column=0, sticky="ew", pady=(sp["s"], 0))
        bot.columnconfigure(1, weight=1)
        res = ttk.Frame(bot)
        res.grid(row=0, column=0, sticky="w")
        self._dot = tk.Canvas(res, width=12, height=12, highlightthickness=0,
                              bd=0)
        self._dot.pack(side="left", padx=(0, sp["s"]))
        self._res = ttk.Frame(res)
        self._res.pack(side="left")
        self._limits_btn = ttk.Button(res, text="Limits…", style="Link.TButton",
                                      command=app._ft_open_limits)
        gui_tips.Tooltip(self._limits_btn, (
            "Opens Setup > Printer & collector > Printer model, where your "
            "printer's process limits are kept with the printer preset."))
        acts = ttk.Frame(bot)
        if wide:
            acts.grid(row=0, column=1, sticky="w", padx=(sp["l"], 0))
        else:
            acts.grid(row=1, column=0, columnspan=2, sticky="w",
                      pady=(sp["xs"], 0))
        self._scope_lbl = ttk.Label(acts, text="Apply to")
        self._scope_cb = ttk.Combobox(acts, textvariable=st["scope"],
                                      state="readonly", width=14)
        self._scope_cb.bind("<<ComboboxSelected>>", lambda _e: self._soon(),
                            add="+")
        gui_tips.Tooltip(self._scope_cb, (
            "Which scaffolds of the G-code window's design take the speed "
            "(on a mandrel, each tube's surface speed). Pressure, "
            "temperature and the needle are one setting for the whole "
            "print, so every scaffold chosen gets the same speed."))
        self._apply = ttk.Button(acts, text="Apply", style="Primary.TButton",
                                 command=self._do_apply)
        self._apply.pack(side="left")
        gui_tips.Tooltip(self._apply, (
            "Sets the pressure and temperature on the Physics page, the "
            "needle's inner diameter on its Material card and the print "
            "speed of the "
            "G-code window's design. With Fiber diameter from physics off, "
            "it also types the target into the scaffold's fiber diameter."))
        self._back = ttk.Button(acts, text="Put back",
                                command=app._ft_put_back)
        self._back.pack(side="left", padx=(sp["xs"], 0))
        gui_tips.Tooltip(self._back, "Undo the last Apply.")
        prov = ttk.Frame(bot)
        if wide:
            prov.grid(row=0, column=2, sticky="e", padx=(sp["l"], 0))
        else:
            prov.grid(row=2, column=0, columnspan=2, sticky="w",
                      pady=(sp["xs"], 0))
        self._prov = ttk.Label(prov, style="Hint.TLabel")
        gui_form.mark(self._prov, "live")
        self._prov.pack(side="left")
        cal = ttk.Button(prov, text="Calibrate", style="Link.TButton",
                         command=lambda: app._goto_calibration_page(
                             "viscosity"))
        cal.pack(side="left", padx=(sp["xs"], 0))
        gui_tips.Tooltip(cal, (
            "Every line rests on the melt's viscosity and its temperature "
            "sensitivity Ea: they set the pressure each fiber needs and how "
            "far apart the temperature lines sit. Calibrate › Viscosity "
            "measures yours."))
        self._watch()
        self.bind("<Map>", lambda _e: self._soon(), add="+")
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._rt = gui_widgets.register(_PanelRetheme(self))
        self._soon()

    # -- wiring --------------------------------------------------------- #
    def _watch(self):
        app = self.app
        st = app._ft_vars()
        vs = [st["d"], st["lag"], st["solve"], st["needle"], st["scope"],
              st["rev"]]
        vs += [st["hold_" + k] for k, _l in SOLVE_FOR]
        vs += [st["r_%s_%s" % (k, e)] for k in DEFAULT_RANGES
               for e in ("lo", "hi")]
        for name in ("sim_pressure", "sim_temperature", "sim_voltage",
                     "sim_standoff", "sim_diam_override", "s_collector",
                     "m_speed_mode", "m_feed_mode", "gce_flavor",
                     "gcode_flavor"):
            v = getattr(app, name, None)
            if v is not None:
                vs.append(v)
        vs += list(getattr(app, "cal_vars", {}).values())
        # the G-code window's design only once it exists: asking for it
        # here would build it at boot, before the default preset has loaded
        cnt = app.__dict__.get("_gce_count")
        if cnt is not None:
            vs.append(cnt)
        for v in app.__dict__.get("_gce_sv") or ():
            vs.append(v.get("speed"))
        seen = {id(v) for v, _n in self._traces}
        for v in vs:
            if v is None or id(v) in seen:
                continue
            seen.add(id(v))
            try:
                self._traces.append((v, v.trace_add(
                    "write", lambda *_a: self._soon())))
            except (tk.TclError, AttributeError):
                pass

    def _on_destroy(self, e):
        if e.widget is not self:
            return
        for v, name in self._traces:
            try:
                v.trace_remove("write", name)
            except (tk.TclError, ValueError):
                pass
        self._traces = []

    def focus_target(self):
        try:
            self._d_entry.focus_set()
        except (AttributeError, tk.TclError):
            pass

    def _soon(self, *_a):
        if self._pending is not None:
            return
        try:
            self._pending = self.after(60, self._refresh)
        except tk.TclError:
            pass

    def _solve_clicked(self):
        """A Solve-for click: that knob moves, so it is no longer held."""
        st = self.app._ft_vars()
        mode = st["solve"].get()
        if mode in dict(SOLVE_FOR):
            st["hold_" + mode].set("")
        self._soon()

    def _clear_holds(self):
        st = self.app._ft_vars()
        for knob, _l in SOLVE_FOR:
            st["hold_" + knob].set("")
        self._soon()

    def _do_apply(self):
        if self._R is not None:
            self.app._ft_apply(self._R)

    def pick(self, speed, temperature):
        """A click on a line: hold that point's speed (mm/s) and temperature
        and solve the pressure -- typed into their fields, so the inputs say
        what the answer is. Under a jet-lag target the lag keeps the speed."""
        st = self.app._ft_vars()
        st["solve"].set("pressure")
        st["hold_pressure"].set("")
        st["hold_temperature"].set("%.4g" % temperature)
        if self._R is None or self._R.lag is None:
            st["hold_speed"].set("%.4g" % (speed * 60.0))
        self._soon()

    # -- drawing --------------------------------------------------------- #
    def _refresh(self):
        self._pending = None
        try:
            if not self.winfo_exists() or not self.winfo_ismapped():
                return
        except tk.TclError:
            return
        app = self.app
        st = app._ft_vars()
        self._watch()             # scaffolds added since the last refresh
        # the fields never hold what moves: the solved knob, and the speed
        # under a jet-lag target (the lag sets it)
        lag = app._ft_target_lag()
        if lag is not None and st["solve"].get() == "speed":
            st["solve"].set("pressure")
        if st["solve"].get() in dict(SOLVE_FOR) \
                and st["hold_" + st["solve"].get()].get().strip():
            st["hold_" + st["solve"].get()].set("")
        if lag is not None and st["hold_speed"].get().strip():
            st["hold_speed"].set("")
        if self._pending is not None:   # the sets above asked for a redraw:
            try:                        # this is it
                self.after_cancel(self._pending)
            except tk.TclError:
                pass
            self._pending = None
        R = self._R = app._ft_solve()
        self._bar.set_enabled("speed", R.lag is None)
        self._draw_holds(R)
        self._note.configure(text=R.note)
        if R.note:
            self._note.grid(row=self._note_row, column=0, columnspan=4,
                            sticky="w", padx=gui_form.PAD["padx"],
                            pady=(0, gui_fonts.SP["xs"]))
        else:
            self._note.grid_remove()
        vals = app._ft_scope_values()
        self._scope_cb.configure(values=vals)
        many = len(vals) > 1
        if many and not self._scope_cb.winfo_ismapped():
            self._scope_lbl.pack(side="left", before=self._apply)
            self._scope_cb.pack(side="left", padx=(gui_fonts.SP["xs"],
                                                   gui_fonts.SP["s"]),
                                before=self._apply)
        elif not many:
            self._scope_lbl.pack_forget()
            self._scope_cb.pack_forget()
        if R.tm is not None:
            needles = app._ft_needles(R.tm, R.now)
            labels = [n for n, _r in needles]
            self._needle_cb.configure(values=labels)
            cur = st["needle"].get().split(" · ")[0]
            match = next((n for n in labels if n.split(" · ")[0] == cur),
                         labels[0])
            if st["needle"].get() != match:
                st["needle"].set(match)
        self._draw_result(R)
        prov = app._ft_provenance()
        self._prov.configure(text=("Viscosity and Ea (%s)" % prov) if prov
                             else "Viscosity and Ea")
        self.update_idletasks()
        self.chart.fit(self._full_h, self._min_h,
                       below=self._bot.winfo_reqheight() + gui_fonts.SP["l"])
        self.chart.draw(R)
        ok = R.chosen is not None and R.chosen.ok and not R.already
        self._apply.state(["!disabled"] if ok else ["disabled"])
        self._back.state(["!disabled"] if st["undo"] else ["disabled"])

    def _draw_holds(self, R):
        """Each hold field: on, with the current value greyed when blank;
        off for the solved knob (and the speed under a lag), greyed with the
        answer's value."""
        app = self.app
        a = R.chosen
        mode = R.mode or app._ft_vars()["solve"].get()
        solved = {mode if mode in dict(SOLVE_FOR) else "pressure"}
        if R.lag is not None:
            solved.add("speed")
        for knob, (e, ph) in self._hold.items():
            off = knob in solved
            show, _u = app._ft_show(knob)
            src = None
            if off:
                if a is not None and a.settings is not None:
                    src = a.settings
            else:
                src = R.now
            text = ""
            if src is not None:
                val = getattr(src, knob)
                text = _g(show(val), 4 if knob == "temperature" else 3)
            try:
                e.state(["disabled"] if off else ["!disabled"])
                ph.configure(text=text)
            except tk.TclError:
                pass

    def _draw_result(self, R):
        """The one result row: the answer's settings, the changed ones in
        bold, and what the jet does there -- or one line saying why not."""
        app = self.app
        for w in self._res.winfo_children():
            w.destroy()
        self._limits_btn.pack_forget()
        pal = gui_widgets.palette()
        try:
            bg = ttk.Style().lookup("TFrame", "background") or pal["bg"]
        except tk.TclError:
            bg = pal["bg"]
        dot = self._dot
        dot.configure(background=bg)
        dot.delete("all")
        a = R.chosen
        parts = []                  # (text, emphasised)
        if R.tm is None or R.d is None or a is None:
            parts = [(R.why, False)]
        elif R.already:
            j = R.now_jet
            parts = [("Your settings already give %s %s"
                      % (_g(app._ft_d_show(j.fiber_diameter)),
                         gui_units.unit("fiber_d")), False),
                     ("lag %s mm" % _g(j.lag_distance), False),
                     ("%s × CTS" % _g(j.speed_ratio), False)]
            dot.create_oval(2, 2, 11, 11, fill=pal["select_bg"],
                            outline="")
        elif a.settings is None or not a.ok:
            why = a.why
            parts = [("Out of reach: " + why + ".", False)]
            if a.settings is not None and a.limit is not None:
                # past a limit the model still answers: say what it would
                # take, so a researcher whose rig goes further can widen
                # the limits rather than guess
                s = a.settings
                parts.append(("It would take %s %s at %s %s, %s °C." % (
                    _g(app._ft_p_show(s.pressure)),
                    gui_units.unit("pressure"),
                    _g(app._ft_v_show(s.speed)), gui_units.unit("speed"),
                    _g(s.temperature)), False))
            else:
                parts.append(("Click a solid line to pick a point.", False))
            if a.limit is not None:
                self._limits_btn.pack(side="left", padx=(gui_fonts.SP["s"],
                                                          0))
            dot.create_oval(2, 2, 11, 11, outline=pal["hint"], width=1.5)
        else:
            s, n, j = a.settings, R.now, a.jet
            pu, vu = gui_units.unit("pressure"), gui_units.unit("speed")
            parts = [
                ("%s %s" % (_g(app._ft_p_show(s.pressure)), pu),
                 abs(s.pressure - n.pressure) > 1e-6 * n.pressure),
                ("%s %s" % (_g(app._ft_v_show(s.speed)), vu),
                 abs(s.speed - n.speed) > 1e-9 * n.speed),
                ("%s °C" % _g(s.temperature),
                 abs(s.temperature - n.temperature) > 1e-6)]
            if abs(s.bore - n.bore) > 1e-9:
                parts.append(("%s mm needle" % _g(2 * s.bore), True))
            parts += [("lag %s mm" % _g(j.lag_distance), False),
                      ("%s × CTS" % _g(j.speed_ratio), False)]
            dot.create_oval(2, 2, 11, 11, fill=pal["select_bg"],
                            outline="")
        for i, (text, strong) in enumerate(parts):
            if i:
                ttk.Label(self._res, text="·", style="Hint.TLabel").pack(
                    side="left", padx=gui_fonts.SP["xs"])
            lb = ttk.Label(self._res, text=text,
                           font="MewSection" if strong else "MewBody")
            gui_form.mark(lb, "live")
            lb.pack(side="left")


class _PanelRetheme:
    def __init__(self, panel):
        self.panel = panel

    def __hash__(self):
        return id(self)

    def retheme(self):
        p = self.panel
        try:
            if p.winfo_exists():
                p._soon()
        except tk.TclError:
            pass


# ====================================================================== #
# the chart
# ====================================================================== #
class _TargetChart(tk.Canvas):
    """Print speed (x) by pressure (y), one line per melt temperature: every
    point on a line gives the target fiber at that temperature and needle.
    Grey where the printer cannot print it; a ring for now, the accent dot
    for the answer, a vertical line for a jet-lag target."""

    PAD_L, PAD_R, PAD_T, PAD_B = 54, 64, 24, 40
    #: how near the pointer must be to a line to snap to it, px
    SNAP = 14
    #: points per temperature line
    N_PTS = 48

    def __init__(self, panel, parent, width, height):
        super().__init__(parent, width=width, height=height,
                         highlightthickness=0, borderwidth=0,
                         cursor="crosshair")
        self.panel = panel
        self.H = height
        self._R = None
        self._ax = None
        self._lines = []          # [(T, [(v, p, x, y, ok), ...])]
        self._cache_key = None
        self._cache = {}
        self.bind("<Motion>", self._on_motion, add="+")
        self.bind("<Leave>", lambda _e: self.delete("hover"), add="+")
        self.bind("<Button-1>", self._on_click, add="+")
        self.bind("<Configure>", self._on_configure, add="+")

    # -- geometry --------------------------------------------------------- #
    def _width(self):
        w = self.winfo_width()
        return w if w > 50 else int(self.cget("width"))

    def _on_configure(self, e):
        if self._R is not None and abs(e.width - getattr(self, "_drawn_w", 0)) > 1:
            self.draw(self._R)

    def fit(self, full_h, min_h, below=0):
        """Take the height left between the chart's top and the bottom of
        whatever scrolls it (the page, or the G-code window's edit box), less
        `below` for the result row under it -- `full_h` at most, `min_h` at
        least, so the chart is on screen when the panel opens."""
        h = full_h
        try:
            anc = self.master
            while anc is not None and not isinstance(anc, tk.Canvas):
                anc = anc.master
            if anc is not None:
                bottom = anc.winfo_rooty() + anc.winfo_height()
                room = bottom - self.winfo_rooty() - below
                if room > 0:
                    h = max(min_h, min(full_h, room))
        except tk.TclError:
            pass
        if h != self.H:
            self.H = h
            self.configure(height=h)

    def _plot(self):
        return (self.PAD_L, self.PAD_T, self._width() - self.PAD_R,
                self.H - self.PAD_B)

    def _to_px(self, v, p):
        x0, y0, x1, y1 = self._plot()
        vl, vh, pl, ph = self._ax
        return (x0 + (v - vl) / (vh - vl) * (x1 - x0),
                y1 - (p - pl) / (ph - pl) * (y1 - y0))

    def _from_px(self, x, y):
        x0, y0, x1, y1 = self._plot()
        vl, vh, pl, ph = self._ax
        return (vl + (x - x0) / (x1 - x0) * (vh - vl),
                pl + (y1 - y) / (y1 - y0) * (ph - pl))

    def _axes(self, R):
        """From zero to a little past the printer's limits, or past the
        answer when it lies beyond them (so the dot is on the chart, in the
        grey); the pressure axis is cut to the lines, not to the limit, so a
        thin fiber's lines are not squeezed against the floor."""
        rg = R.ranges
        v_rng = rg["speed"][1] / 60.0
        p_rng = rg["pressure"][1] * PSI_TO_PA
        vs = [v_rng * 1.1, R.now.speed * 1.1]
        ps = [R.now.pressure]
        a = R.chosen
        if a is not None and a.settings is not None:
            vs.append(a.settings.speed * 1.1)
            ps.append(a.settings.pressure)
        v_hi = min(max(vs), 3 * v_rng)
        if R.d is not None:
            s = R.tm.pressure_for(R.d, min(v_hi, v_rng), R.hl_t, R.bore)
            if s is not None:
                ps.append(s.pressure)
        p_hi = min(max(ps) * 1.25, 3 * p_rng)
        if not p_hi > 0:
            p_hi = p_rng
        return 0.0, v_hi, 0.0, p_hi

    # -- what is printable ------------------------------------------------ #
    def _ok(self, R, v, p, T):
        rg = R.ranges
        cts = R.tm.cts(p, T, R.bore)
        return (cts is not None and v >= cts
                and rg["speed"][0] / 60 <= v <= rg["speed"][1] / 60
                and rg["pressure"][0] * PSI_TO_PA <= p
                <= rg["pressure"][1] * PSI_TO_PA
                and rg["temperature"][0] - 1e-6 <= T
                <= rg["temperature"][1] + 1e-6)

    def _why_not(self, R, v, p, T):
        """Why (v, p, T) cannot be printed, in a few words, or ""."""
        rg = R.ranges
        cts = R.tm.cts(p, T, R.bore)
        if cts is not None and v < cts:
            return "the fiber coils here (below CTS)"
        if not (rg["speed"][0] / 60 <= v <= rg["speed"][1] / 60
                and rg["pressure"][0] * PSI_TO_PA <= p
                <= rg["pressure"][1] * PSI_TO_PA):
            return "beyond your printer's limits"
        if not (rg["temperature"][0] - 1e-6 <= T
                <= rg["temperature"][1] + 1e-6):
            return "outside your printer's melt temperatures"
        return ""

    def _line_pts(self, R, T):
        """[(v, p)] of the target at temperature T, cached per context."""
        key = (repr(R.tm.material), repr(R.tm.nozzle), repr(R.tm.calibration),
               R.tm.gap, R.tm.voltage, R.d, R.bore, self._ax)
        if key != self._cache_key:
            self._cache_key, self._cache = key, {}
        k = round(T, 6)
        if k not in self._cache:
            vh = self._ax[1]
            speeds = [vh * (i / self.N_PTS) for i in range(1, self.N_PTS + 1)]
            self._cache[k] = [(v, p) for v, p in
                              R.tm.curve(R.d, T, R.bore, speeds)
                              if p is not None]
        return self._cache[k]

    # -- drawing ---------------------------------------------------------- #
    def _colours(self):
        pal = gui_widgets.palette()
        try:
            bg = ttk.Style().lookup("TFrame", "background") or pal["bg"]
        except tk.TclError:
            bg = pal["bg"]
        surf = pal.get("card", "#ffffff")
        hint = pal.get("hint", "#646a71")
        dark = sum(_hex(surf)) / 3 < 110
        return dict(bg=bg, surf=surf, fg=pal.get("fg", "#1c1e21"), hint=hint,
                    hair=pal.get("hairline", "#d9dce1"),
                    grid=_mix(surf, pal.get("hairline", "#d9dce1"), 0.6),
                    grey=_mix(surf, hint, 0.20 if dark else 0.13),
                    line=_mix(surf, hint, 0.85),
                    acc=pal.get("select_bg", "#2f6fd6"))

    def draw(self, R):
        self._R = R
        self._drawn_w = self._width()
        c = self._colours()
        self.configure(background=c["bg"])
        self.delete("all")
        self._lines = []
        x0, y0, x1, y1 = self._plot()
        self.create_rectangle(x0, y0, x1, y1, fill=c["surf"], outline="")
        if R.tm is None or R.now is None:
            self.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=R.why,
                             fill=c["hint"], font="MewHint",
                             width=max(80, x1 - x0 - 20))
            self.create_rectangle(x0, y0, x1, y1, outline=c["hair"])
            return
        self._ax = vl, vh, pl, ph = self._axes(R)
        vshow = self.panel.app._ft_v_show
        pshow = self.panel.app._ft_p_show
        xt = _nice_ticks(vshow(vl), vshow(vh), 6)
        yt = _nice_ticks(pshow(pl), pshow(ph), 5)
        for t in xt[1:]:
            x, _y = self._to_px(t / vshow(1.0), pl)
            self.create_line(x, y0, x, y1, fill=c["grid"])
        for t in yt[1:]:
            _x, y = self._to_px(vl, t / pshow(1.0))
            self.create_line(x0, y, x1, y, fill=c["grid"])
        self._draw_grey(R, c)
        self.create_rectangle(x0, y0, x1, y1, outline=c["hair"])
        # axes
        for t in xt:
            x, _y = self._to_px(t / vshow(1.0), pl)
            self.create_text(x, y1 + 4, text=_g(t, 4), anchor="n",
                             fill=c["hint"], font="MewHint")
        for t in yt:
            _x, y = self._to_px(vl, t / pshow(1.0))
            self.create_text(x0 - 6, y, text=_g(t, 3), anchor="e",
                             fill=c["hint"], font="MewHint")
        self.create_text((x0 + x1) / 2, self.H - 2, anchor="s",
                         fill=c["hint"], font="MewHint",
                         text="%s (%s)" % (
                             self.panel.app._ft_speed_word().capitalize(),
                             gui_units.unit("speed")))
        self.create_text(10, (y0 + y1) / 2, angle=90, fill=c["hint"],
                         font="MewHint",
                         text="Pressure (%s)" % gui_units.unit("pressure"))
        if R.d is None:
            return
        self._draw_lines(R, c)
        # the lag's speed
        if R.lag is not None and R.lag_v is not None and vl <= R.lag_v <= vh:
            x, _y = self._to_px(R.lag_v, pl)
            self.create_line(x, y0, x, y1, fill=c["acc"], dash=(6, 3),
                             width=1.5)
            self._boxed(x + 4, y1 - 4, "lag %s mm" % _g(R.lag), "sw",
                        c["acc"], c["surf"])
        # now, and the answer
        n = R.now
        if R.bore == n.bore:
            x, y = self._to_px(n.speed, n.pressure)
            if x0 <= x <= x1 and y0 <= y <= y1:
                self.create_oval(x - 5, y - 5, x + 5, y + 5, fill=c["surf"],
                                 outline=c["fg"], width=2)
                # no backing box: on a line, a box cut the line in two
                left = x > x1 - 40
                self.create_text(x - 8 if left else x + 8, y + 3, text="now",
                                 anchor="ne" if left else "nw", fill=c["fg"],
                                 font="MewHint")
        a = R.chosen
        if a is not None and a.settings is not None and not R.already:
            x, y = self._to_px(a.settings.speed, a.settings.pressure)
            if x0 <= x <= x1 and y0 <= y <= y1:
                if a.ok:
                    self.create_oval(x - 6, y - 6, x + 6, y + 6,
                                     fill=c["acc"], outline=c["surf"],
                                     width=2)
                else:
                    self.create_oval(x - 6, y - 6, x + 6, y + 6,
                                     fill=c["surf"], outline=c["acc"],
                                     width=2, dash=(2, 2))

    def _boxed(self, x, y, text, anchor, fill, surf, font="MewHint",
               tags=()):
        tid = self.create_text(x, y, text=text, anchor=anchor, fill=fill,
                               font=font, tags=tags)
        bb = self.bbox(tid)
        if bb:
            r = self.create_rectangle(bb[0] - 2, bb[1], bb[2] + 2, bb[3],
                                      fill=surf, outline="", tags=tags)
            self.tag_lower(r, tid)
        return tid

    def _draw_grey(self, R, c):
        """Grey where the printer cannot print the line: below CTS (at the
        highlighted temperature) and past its speed and pressure limits."""
        x0, y0, x1, y1 = self._plot()
        vl, vh, pl, ph = self._ax
        rg = R.ranges
        grey = c["grey"]
        sv_lo, sv_hi = rg["speed"][0] / 60.0, rg["speed"][1] / 60.0
        sp_lo, sp_hi = (rg["pressure"][0] * PSI_TO_PA,
                        rg["pressure"][1] * PSI_TO_PA)

        def rect(va, vb, pa, pb):
            va, vb = max(va, vl), min(vb, vh)
            pa, pb = max(pa, pl), min(pb, ph)
            if vb <= va or pb <= pa:
                return None
            xa, ya = self._to_px(va, pb)
            xb, yb = self._to_px(vb, pa)
            self.create_rectangle(xa, ya, xb, yb, fill=grey, outline="")
            return xa, ya, xb, yb
        right = rect(sv_hi, vh, pl, ph)
        rect(vl, sv_lo, pl, ph)
        top = rect(vl, vh, sp_hi, ph)
        rect(vl, vh, pl, sp_lo)
        # the jet coils below CTS: CTS may lean with pressure once a flow
        # hook is fitted, so it is a polygon, not a band
        pts = []
        for k in range(41):
            p = pl + k / 40 * (ph - pl)
            cts = R.tm.cts(max(p, 1e-9), R.hl_t, R.bore)
            if cts is None:
                continue
            pts.append(self._to_px(min(max(cts, vl), vh), p))
        coil_w = 0
        if len(pts) >= 2:
            poly = [self._to_px(vl, pl)] + pts + [self._to_px(vl, ph)]
            self.create_polygon(*[q for xy in poly for q in xy], fill=grey,
                                outline="")
            coil_w = min(x for x, _y in pts) - x0
            cx = min(x for x, _y in pts)
            self.create_line(*[q for xy in pts for q in xy],
                             fill=_mix(grey, c["hint"], 0.35))
        # say what each grey is, where there is room
        for text in ("fiber coils\n(below CTS)", "coils"):
            tid = self.create_text(x0 + coil_w / 2, y0 + 8, anchor="n",
                                   text=text, justify="center",
                                   fill=c["hint"], font="MewHint")
            bb = self.bbox(tid)
            if bb and bb[2] - bb[0] <= coil_w - 8:
                break
            self.delete(tid)
        if right is not None:
            # at the band's foot: every line leaves through the top or the
            # right edge, so the lines cross the band higher up
            w = right[2] - right[0]
            if w > 46:
                self.create_text((right[0] + right[2]) / 2, y1 - 6,
                                 anchor="s",
                                 text="beyond\nyour\nprinter"
                                 if w < 90 else "beyond your\nprinter",
                                 justify="center", fill=c["hint"],
                                 font="MewHint")
        if top is not None and top[3] - top[1] > 30:
            self.create_text((x0 + coil_w + (right[0] if right else x1)) / 2,
                             (top[1] + top[3]) / 2,
                             text="beyond your printer's pressure",
                             fill=c["hint"], font="MewHint")

    def _draw_lines(self, R, c):
        x0, y0, x1, y1 = self._plot()
        hl, now_t = R.hl_t, R.now.temperature
        order = sorted(R.temps, key=lambda t: (abs(t - hl) < 1e-6,
                                               abs(t - now_t) < 1e-6))
        labels = []               # (edge, pos, text, colour, font)
        for T in order:
            is_hl = abs(T - hl) < 1e-6
            is_now = abs(T - now_t) < 1e-6
            colour = c["acc"] if is_hl else (c["fg"] if is_now
                                             else c["line"])
            width = 2.5 if is_hl else (1.75 if is_now else 1.25)
            pts = []
            for v, p in self._line_pts(R, T):
                x, y = self._to_px(v, p)
                pts.append((v, p, x, y, self._ok(R, v, p, T)))
            pts = self._clip_top(pts, y0)
            if len(pts) < 2:
                continue
            self._lines.append((T, pts))
            # one polyline per run of printable / not printable
            run = [pts[0]]
            for q in pts[1:]:
                if q[4] != run[-1][4]:
                    run.append(q)
                    self._polyline(run, colour, width, run[0][4])
                    run = [q]
                else:
                    run.append(q)
            self._polyline(run, colour, width, run[0][4])
            _v, _p, ex, ey, _ok = pts[-1]
            text = "%s °C" % _g(T)
            font = "MewSection" if is_hl else "MewHint"
            if ey <= y0 + 0.5:
                labels.append(("top", ex, text, colour, font))
            else:
                labels.append(("right", ey, text, colour, font))
        self._place_labels(labels, c)

    @staticmethod
    def _clip_top(pts, y0):
        """Cut a polyline where it leaves the plot through the top."""
        out = []
        for q in pts:
            if q[3] >= y0:
                out.append(q)
                continue
            if out:
                a = out[-1]
                t = (a[3] - y0) / (a[3] - q[3])
                out.append((a[0] + t * (q[0] - a[0]), a[1] + t * (q[1] - a[1]),
                            a[2] + t * (q[2] - a[2]), y0, a[4]))
            break
        return out

    def _polyline(self, run, colour, width, ok):
        if len(run) < 2:
            return
        xy = [q for r in run for q in (r[2], r[3])]
        if ok:
            self.create_line(*xy, fill=colour, width=width, capstyle="round",
                             joinstyle="round")
        else:
            self.create_line(*xy, fill=colour, width=max(1.0, width - 0.75),
                             dash=(4, 3))

    def _place_labels(self, labels, c):
        """Each line's temperature at its end: above the plot for a line
        leaving through the top, right of it for one leaving at the side,
        nudged apart so none overlap."""
        x0, y0, x1, y1 = self._plot()
        tops = sorted((l for l in labels if l[0] == "top"), key=lambda l: l[1])
        last = -1e9
        for _e, x, text, colour, font in tops:
            tid = self.create_text(0, 0, text=text, font=font, anchor="s")
            bb = self.bbox(tid)
            w = (bb[2] - bb[0]) if bb else 30
            x = max(x, last + w / 2 + 6 if last > -1e9 else x)
            x = min(x, x1 + self.PAD_R - w / 2 - 2)
            self.coords(tid, x, y0 - 3)
            self.itemconfigure(tid, fill=colour)
            last = x + w / 2
        sides = sorted((l for l in labels if l[0] == "right"),
                       key=lambda l: l[1])
        ys = []
        for _e, y, text, colour, font in sides:
            ys.append(max(y, (ys[-1] + 15) if ys else y))
        # push the stack back up if it ran off the bottom
        over = (ys[-1] - (y1 + 6)) if ys else 0
        if over > 0:
            ys = [y - over for y in ys]
            for i in range(len(ys) - 2, -1, -1):
                ys[i] = min(ys[i], ys[i + 1] - 15)
        for (_e, _y, text, colour, font), y in zip(sides, ys):
            self.create_text(x1 + 5, y, text=text, anchor="w", fill=colour,
                             font=font)

    # -- the pointer ------------------------------------------------------ #
    def _snap(self, x, y):
        """(T, v, p, px, py) of the nearest line point within SNAP px, or
        None. With a jet-lag target the point is that line's crossing with
        the lag's speed."""
        best = None
        for T, pts in self._lines:
            for a, b in zip(pts, pts[1:]):
                dx, dy = b[2] - a[2], b[3] - a[3]
                L = dx * dx + dy * dy
                t = 0.0 if L == 0 else max(0.0, min(1.0, (
                    (x - a[2]) * dx + (y - a[3]) * dy) / L))
                px, py = a[2] + t * dx, a[3] + t * dy
                dist = math.hypot(x - px, y - py)
                if best is None or dist < best[0]:
                    best = (dist, T, a[0] + t * (b[0] - a[0]),
                            a[1] + t * (b[1] - a[1]), px, py)
        if best is None or best[0] > self.SNAP:
            return None
        _d, T, v, p, px, py = best
        R = self._R
        if R.lag is not None and R.lag_v is not None:
            v = R.lag_v
            s = R.tm.pressure_for(R.d, v, T, R.bore)
            if s is None:
                return None
            p = s.pressure
            px, py = self._to_px(v, p)
        return T, v, p, px, py

    def _readout(self, x, y, text, colour):
        """A small boxed note by the pointer, kept inside the chart."""
        c = self._colours()
        tid = self.create_text(0, 0, text=text, anchor="nw", fill=colour,
                               font="MewHint", tags="hover")
        bb = self.bbox(tid)
        w = (bb[2] - bb[0]) if bb else 120
        h = (bb[3] - bb[1]) if bb else 14
        tx = x + 14 if x + 14 + w + 6 < self._width() else x - 14 - w
        ty = y - 14 - h if y - 14 - h > 2 else y + 14
        self.coords(tid, tx, ty)
        r = self.create_rectangle(tx - 5, ty - 3, tx + w + 5, ty + h + 3,
                                  fill=c["surf"], outline=c["hair"],
                                  tags="hover")
        self.tag_lower(r, tid)

    def _on_motion(self, e):
        R = self._R
        self.delete("hover")
        if R is None or R.tm is None or self._ax is None:
            return
        x0, y0, x1, y1 = self._plot()
        if not (x0 <= e.x <= x1 and y0 <= e.y <= y1):
            return
        c = self._colours()
        app = self.panel.app
        vu, pu = gui_units.unit("speed"), gui_units.unit("pressure")
        hit = self._snap(e.x, e.y) if R.d is not None else None
        if hit is not None:
            T, v, p, px, py = hit
            self.create_oval(px - 6, py - 6, px + 6, py + 6, outline=c["acc"],
                             width=2, tags="hover")
            why = self._why_not(R, v, p, T)
            text = "%s °C · %s %s · %s %s" % (
                _g(T), _g(app._ft_v_show(v)), vu, _g(app._ft_p_show(p)), pu)
            text += ("\nCan't print: " + why) if why else "\nClick to pick"
            self._readout(px, py, text, c["fg"])
            return
        v, p = self._from_px(e.x, e.y)
        head = "%s %s · %s %s" % (_g(app._ft_v_show(v)), vu,
                                  _g(app._ft_p_show(p)), pu)
        j = R.tm.jet(mew_target.Settings(p, v, R.hl_t, R.bore)) \
            if v > 0 and p > 0 else None
        if j is None:
            text = head
        else:
            why = self._why_not(R, v, p, R.hl_t)
            text = "%s\n%s %s fiber at %s °C" % (
                head, _g(app._ft_d_show(j.fiber_diameter)),
                gui_units.unit("fiber_d"), _g(R.hl_t))
            if why:
                text += " · " + why
        self._readout(e.x, e.y, text, c["hint"])

    def _on_click(self, e):
        R = self._R
        if R is None or R.tm is None or R.d is None or self._ax is None:
            return
        hit = self._snap(e.x, e.y)
        if hit is None:
            return
        T, v, p, _px, _py = hit
        if self._why_not(R, v, p, T):
            return
        self.panel.pick(v, T)
