#!/usr/bin/env python3
"""gui_units.py -- display units: the Settings panel's speed, pressure and
fiber-diameter choices, applied to the fields that show those quantities.

DISPLAY ONLY. Every Tk var keeps its canonical unit -- the one the engine,
the presets, the persist map and the g-code already use -- and nothing here
ever changes what a var holds unless the user types into a converted field.
A field whose canonical unit differs from the chosen display unit gets a
second, DISPLAY var: the entry shows that, and the two are kept in step by a
pair of traces.

- canonical -> display: whenever the canonical var is written (a preset
  load, the physics diameter, a seed), the display var is re-formatted.
- display -> canonical: ONLY when the display var is written by typing.
  A guard flag stops the write-back that the first direction would
  otherwise trigger, so a preset load can never round a stored value.

A field whose canonical unit already IS the display unit is left bound to
its canonical var directly, so it behaves exactly as it did before this
module existed.

Why a guard and not "reformat on focus-out": reformatting while the user is
typing ate a trailing zero ("13.30" came back as "13.3") and moved the
cursor. The display var is only re-formatted from the canonical side.

Which fields convert is decided from the unit a field is BUILT with
(`gui_form` passes it): mm/min and mm/s are speeds, psi is a pressure, and
an "mm" field whose label names a fiber diameter is a fiber diameter. A
mandrel or plate diameter in mm is a length, not a fiber diameter, and is
left alone.

MODERN SHELL ONLY: `enable()` is called by `ModernApp` before any widget is
built. The classic developer UI never calls it and keeps its canonical units.
"""
import re
import tkinter as tk

import gui_prefs

# factor that turns ONE display unit into the quantity's base unit
QUANTITIES = {
    "speed":    {"mm/min": 1.0, "mm/s": 60.0},
    "pressure": {"psi": 1.0, "bar": 14.503773773, "kPa": 0.14503773773},
    "fiber_d":  {"mm": 1.0, "µm": 0.001},
    # readout-only quantities: the Scaffold designer's Design summary. No field
    # is built with these units, so they never convert an entry.
    "pore":       {"mm": 1.0, "µm": 0.001},
    "fiber_len":  {"mm": 1.0, "cm": 10.0, "m": 1000.0},
    "volume":     {"mm³": 1.0, "nL": 0.001},
    "print_time": {"s": 1.0, "min": 60.0, "h": 3600.0},
}
DEFAULTS = {"speed": "mm/min", "pressure": "psi", "fiber_d": "µm",
            "pore": "µm", "fiber_len": "m", "volume": "mm³",
            "print_time": "min"}
LABELS = {"speed": "Speed", "pressure": "Pressure", "fiber_d": "Fiber diameter",
          "pore": "Pore size, height", "fiber_len": "Fiber length",
          "volume": "Volume", "print_time": "Print time"}
_UNIT_QUANTITY = {"mm/min": "speed", "mm/s": "speed", "psi": "pressure"}
_FIBER_D = re.compile(r"\bfib(er|re)\b.*(diameter|Ø|Ø)", re.I)
_UNIT_IN_TEXT = re.compile(r"\s*\((mm/min|mm/s|psi|mm)\)\s*:?\s*$")

_enabled = False
_bindings = []          # every live _Binding, for a unit change to reach
_display_of = {}        # display var name -> its canonical var name


def enable():
    global _enabled
    _enabled = True


def enabled():
    return _enabled


def unit(quantity):
    """The display unit the user chose for `quantity`."""
    u = gui_prefs.get("units_" + quantity, DEFAULTS[quantity])
    return u if u in QUANTITIES[quantity] else DEFAULTS[quantity]


def quantity_of(unit_str, label=""):
    """The quantity a field built with `unit_str` shows, or None."""
    if unit_str in _UNIT_QUANTITY:
        return _UNIT_QUANTITY[unit_str]
    if unit_str == "mm" and _FIBER_D.search(label or ""):
        return "fiber_d"
    return None


def split_label(text, unit_str):
    """(text, unit) with a unit written INTO the label text pulled out:
    ``"Print speed (mm/min):", None`` -> ``("Print speed", "mm/min")``."""
    if unit_str or not text:
        return text, unit_str
    m = _UNIT_IN_TEXT.search(text)
    if not m:
        return text, unit_str
    return text[:m.start()], m.group(1)


def factor(quantity, from_unit, to_unit):
    q = QUANTITIES[quantity]
    return q[from_unit] / q[to_unit]


def fmt_number(x):
    """A converted number for a field: five significant figures, no
    trailing zeros, no exponent for the magnitudes these fields carry."""
    s = "%.5g" % x
    if "e" in s:
        s = ("%.6f" % x).rstrip("0").rstrip(".")
    return s


def store_number(x):
    """A converted number for a STORED value: twelve significant figures.
    What the user typed in a display unit goes back to the canonical unit at
    this precision, so it survives the round trip to every emitter's four
    decimals: 400 mm/min is kept as 6.66666666667 mm/s and written as F400.
    (Five figures kept 6.6667, which every output then wrote as F400.002.)"""
    s = "%.12g" % x
    if "e" in s:
        fixed = ("%.15f" % x).rstrip("0").rstrip(".")
        if fixed not in ("", "-0", "0", "-"):
            s = fixed           # else too small to write without the exponent
    return s


def convert_text(text, quantity, from_unit, to_unit, store=False):
    """`text` (a number in `from_unit`) re-expressed in `to_unit`. Text that
    is blank or not a number passes through unchanged, so a blank field
    stays blank and a typo still reaches the validator as typed. `store`
    is for a typed value going to the canonical var: full precision
    (`store_number`) instead of the five figures a field shows."""
    if from_unit == to_unit:
        return text
    try:
        x = float(str(text).strip())
    except ValueError:
        return text
    x *= factor(quantity, from_unit, to_unit)
    return store_number(x) if store else fmt_number(x)


def shown(quantity, canonical_unit):
    """The unit a readout of `quantity` prints (canonical outside the shell)."""
    return unit(quantity) if _enabled else canonical_unit


def num(quantity, text, canonical_unit):
    """Just the number of a readout, in the display unit: for a line that
    prints several numbers against one unit ("600 → 240 mm/min")."""
    return convert_text(text, quantity, canonical_unit,
                        shown(quantity, canonical_unit))


def readout(quantity, value, canonical_unit, digits=3):
    """A computed value (a float in `canonical_unit`) with its display unit,
    at `digits` significant figures and never in exponent form:
    ``readout("fiber_len", 2310.0, "mm")`` -> "2.31 m"."""
    u = shown(quantity, canonical_unit)
    x = float(value) * factor(quantity, canonical_unit, u)
    if x and abs(x) >= 10 ** digits:
        # ungrouped, as the g-code writes F1200: "1,200 mm/min" read as 1.2
        s = "%.0f" % x
    else:
        s = "%.*g" % (digits, x)
        if "e" in s:
            s = ("%.6f" % x).rstrip("0").rstrip(".")
    return "%s %s" % (s, u)


def show(quantity, text, canonical_unit):
    """A read-only readout with its unit: ``show("speed", "480", "mm/min")``
    -> "8 mm/s" when the display unit is mm/s. For summaries and live text,
    not fields. Non-numeric text ("?", blank) keeps its text and gains the
    display unit."""
    u = unit(quantity) if _enabled else canonical_unit
    return "%s %s" % (convert_text(text, quantity, canonical_unit, u), u)


# -- prose: instructions, tooltips, info dots ------------------------------ #
# Unit phrases in text are rewritten only where they cannot mean anything else:
# a speed or pressure unit, and the explicit {fiber_d} token a fiber diameter
# is written with (a bare "mm" is a length far more often than a fiber
# diameter, so it is never guessed at). "(mm/s²)" is an acceleration and is
# left alone by the lookahead.
_NUM_UNIT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(\s?)(mm/min|mm/s|psi)(?![\w²/])")
_PAREN_UNIT = re.compile(r"\((mm/min|mm/s|psi)\)")
_IN_UNIT = re.compile(r"\bin (mm/min|mm/s|psi)(?![\w²/])")
_tracked = []           # (label widget, template) re-rendered on a unit change


def _shown_unit(canonical):
    q = _UNIT_QUANTITY[canonical]
    return q, (unit(q) if _enabled else canonical)


def localize(text):
    """`text` with its speed, pressure and {fiber_d} units in the display
    units. Numbers written WITH their unit ("240 mm/min") are converted; a
    number with no unit beside it is not touched, which is why the registry
    writes a ladder as "600 mm/min down to 240 mm/min". Outside the modern
    shell only the {fiber_d} token is filled in, with the canonical mm."""
    if not text:
        return text
    text = str(text).replace("{fiber_d}",
                             unit("fiber_d") if _enabled else "mm")
    if not _enabled:
        return text

    def num(m):
        q, u = _shown_unit(m.group(3))
        return convert_text(m.group(1), q, m.group(3), u) + m.group(2) + u

    text = _NUM_UNIT.sub(num, text)
    text = _PAREN_UNIT.sub(lambda m: "(%s)" % _shown_unit(m.group(1))[1], text)
    return _IN_UNIT.sub(lambda m: "in %s" % _shown_unit(m.group(1))[1], text)


def track(widget, template, fmt=None):
    """Show `template` localized on `widget` now and after every unit
    change. `fmt` wraps the localized text (a step's "3.  " prefix)."""
    def render():
        t = localize(template)
        widget.configure(text=fmt(t) if fmt else t)
    try:
        render()
    except tk.TclError:
        return widget
    _tracked.append((widget, render))
    return widget


def track_callback(widget, render):
    """Refresh a composite display when the Settings units change."""
    render()
    _tracked.append((widget, render))
    return widget


class _Binding:
    """One converted field: its canonical var, its display var, the label
    whose unit suffix follows the setting."""

    def __init__(self, entry, var, quantity, canonical_unit, label, text,
                 relabel):
        self.entry = entry
        self.var = var
        self.quantity = quantity
        self.canonical = canonical_unit
        self.label = label
        self.text = text
        self.relabel = relabel
        self.shown = canonical_unit
        self.display = None
        self._syncing = False
        var.trace_add("write", self._from_canonical)

    def apply(self):
        """Bind the entry to the var that matches the chosen unit."""
        u = unit(self.quantity)
        self.shown = u
        try:
            if u == self.canonical:
                self.entry.configure(textvariable=self.var)
            else:
                if self.display is None:
                    self.display = tk.StringVar(master=self.entry)
                    self.display.trace_add("write", self._from_display)
                    _display_of[str(self.display)] = str(self.var)
                self._push()
                self.entry.configure(textvariable=self.display)
            if self.label is not None and self.relabel is not None:
                self.label.configure(text=self.relabel(self.text, u))
        except tk.TclError:
            pass

    def _push(self):
        if self.display is None or self.shown == self.canonical:
            return
        self._syncing = True
        try:
            self.display.set(convert_text(self.var.get(), self.quantity,
                                          self.canonical, self.shown))
        finally:
            self._syncing = False

    def _from_canonical(self, *_a):
        if not self._syncing:
            self._push()

    def _from_display(self, *_a):
        if self._syncing or self.shown == self.canonical:
            return
        self._syncing = True
        try:
            self.var.set(convert_text(self.display.get(), self.quantity,
                                      self.shown, self.canonical, store=True))
        finally:
            self._syncing = False


def canonical_name(name):
    """The canonical var behind an entry's textvariable `name` (itself when
    the field is not converted): for code that finds a field by its var."""
    return _display_of.get(str(name), str(name))


def bind(entry, var, unit_str, label=None, text="", relabel=None):
    """Called by gui_form for every entry it builds with a unit. Returns the
    unit to print in the label (the display unit for a converted field, else
    `unit_str` unchanged). A no-op outside the modern shell."""
    if not _enabled or var is None:
        return unit_str
    q = quantity_of(unit_str, text)
    if q is None:
        return unit_str
    b = _Binding(entry, var, q, unit_str, label, text, relabel)
    _bindings.append(b)
    b.apply()
    return b.shown


class _Rounded:
    """An entry that SHOWS its var's number at five significant figures and
    writes back only what the user types. For the Physics page, whose stored
    values carry every digit a fit produced (`228.16090808099523`) because the
    workers read them: rounding the var would quantise the model, so only the
    text on screen is rounded. Same one-way guard as `_Binding`."""

    def __init__(self, entry, var):
        self.var = var
        self.display = tk.StringVar(master=entry)
        self._syncing = False
        _display_of[str(self.display)] = str(var)
        var.trace_add("write", self._from_canonical)
        self.display.trace_add("write", self._from_display)
        self._push()
        entry.configure(textvariable=self.display)

    @staticmethod
    def _round(text):
        s = str(text).strip()
        try:
            x = float(s)
        except ValueError:
            return text
        return fmt_number(x) if s and x == x and abs(x) != float("inf") else text

    def _push(self):
        self._syncing = True
        try:
            self.display.set(self._round(self.var.get()))
        finally:
            self._syncing = False

    def _from_canonical(self, *_a):
        if not self._syncing:
            self._push()

    def _from_display(self, *_a):
        if self._syncing:
            return
        self._syncing = True
        try:
            self.var.set(self.display.get())
        finally:
            self._syncing = False


def bind_rounded(entry, var):
    """Show `var` rounded in `entry` (see `_Rounded`), unless the entry is
    already bound to a unit-converting display var, which rounds the same
    way. A no-op outside the modern shell."""
    if not _enabled or var is None:
        return
    try:
        if str(entry.cget("textvariable")) != str(var):
            return
    except tk.TclError:
        return
    _Rounded(entry, var)


def set_unit(quantity, u):
    """The Settings panel's setter: save, then re-bind every live field.
    Readouts are refreshed by the caller (`_settings_set_unit`)."""
    if u not in QUANTITIES[quantity]:
        return
    gui_prefs.update(**{"units_" + quantity: u})
    alive = []
    for b in _bindings:
        try:
            if not b.entry.winfo_exists():
                continue
        except tk.TclError:
            continue
        alive.append(b)
        if b.quantity == quantity:
            b.apply()
    _bindings[:] = alive
    keep = []
    for w, render in _tracked:
        try:
            if w.winfo_exists():
                render()
                keep.append((w, render))
        except tk.TclError:
            pass
    _tracked[:] = keep
