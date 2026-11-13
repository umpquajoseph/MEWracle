#!/usr/bin/env python3
"""mew_target.py -- the process settings that give a target fiber: the app's
own forward model, run backwards.

A researcher starts from the fiber they want ("15 um"), not from machine
settings. This module answers "which pressure / speed / temperature / needle
give it on this printer", and it does so WITHOUT a second copy of the physics:
every answer is a root of `MEWModel.solve` itself, found by bisection, and is
put back through `solve` before it is offered. A user-replaced law
(`mew_lawlang`) is therefore inverted exactly as it runs, and an answer can
never disagree with the diameter Generate will compute from it.

What the forward model says, and so what can be solved for:

* Diameter, d = sqrt(4Q/(pi v)) over Hagen-Poiseuille Q = pi r^4 P/(8 eta(T) L).
  ONE target cannot fix several knobs: at a held temperature and needle the
  set that gives d is a curve in the (speed, pressure) plane (with the shipped
  laws the straight line P ~ v through the origin). `pressure_for` /
  `speed_for` / `temperature_for` each move ONE knob with the others held;
  `curve` is the whole set at a held temperature and needle.
* Jet lag, lag = H * A * (1 - (v/CTS)^-b): it depends on the speed (through
  CTS and the gap), so a lag target fixes the speed (`speed_for_lag`). It
  levels off at A*H, so a target at or past that is out of reach by physics.
* The diameter law holds only for a straight fiber, v >= CTS. Below CTS the
  jet coils and the law is an extrapolation, so no answer there is offered.

Temperature enters only through eta(T), so each temperature is one line of
the (speed, pressure) set; pressure and speed rest on the same viscosity, so
`gui_fiber_target` treats all three alike and says where eta and Ea came from.
This module solves whatever it is asked.

Units are `mew_physics`'s: mm, mm/s, Pa, degC. Pure: no Tk, no file I/O."""
from __future__ import annotations

import dataclasses
import math

from mew_physics import MEWModel, ProcessParams

#: Stock needle bores: (gauge, nominal INNER diameter, mm), regular wall.
#: Nominal only -- a needle's real bore varies by maker and wall type, and the
#: bore enters the flow as r^4, so the Physics page asks for the MEASURED one.
#: 21 G 0.514 mm is `mew_physics.Nozzle`'s own default bore.
NEEDLE_GAUGES = ((18, 0.838), (19, 0.686), (20, 0.603), (21, 0.514),
                 (22, 0.413), (23, 0.337), (25, 0.260), (27, 0.210))

#: how close a bisected answer must land, as a fraction of the target, before
#: it is offered (it is put back through the forward model to check)
TOLERANCE = 1e-4

#: the spans searched for what a target NEEDS, wider than any printer's range
#: so an out-of-reach answer can say how far out it is
PRESSURE_SPAN = (1.0, 1.0e8)        # Pa
SPEED_SPAN = (1.0e-3, 1.0e5)        # mm/s
TEMPERATURE_SPAN = (0.0, 300.0)     # degC


@dataclasses.dataclass(frozen=True)
class Settings:
    """One point in the process space."""
    pressure: float      # Pa
    speed: float         # mm/s
    temperature: float   # degC
    bore: float          # needle inner RADIUS, mm


@dataclasses.dataclass(frozen=True)
class Ranges:
    """The printer's usable range of each knob. Pressure Pa, speed mm/s,
    temperature degC. A knob outside it is out of reach, not offered."""
    pressure: tuple
    speed: tuple
    temperature: tuple

    def why_outside(self, s):
        """None when `s` is inside every range, else (knob, low, high)."""
        for knob, val in (("pressure", s.pressure), ("speed", s.speed),
                          ("temperature", s.temperature)):
            lo, hi = getattr(self, knob)
            if val < lo * (1 - 1e-9) or val > hi * (1 + 1e-9):
                return knob, lo, hi
        return None


def _bisect(f, target, lo, hi, *, log=True, iters=200):
    """x in [lo, hi] with f(x) == target, f monotone either way. None when
    the ends do not bracket the target or f has no value at an end."""
    try:
        flo, fhi = f(lo), f(hi)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    if flo is None or fhi is None:
        return None
    if not min(flo, fhi) <= target <= max(flo, fhi):
        return None
    rising = fhi > flo
    for _ in range(iters):
        mid = math.sqrt(lo * hi) if log else 0.5 * (lo + hi)
        try:
            fm = f(mid)
        except (ValueError, ZeroDivisionError, OverflowError):
            return None
        if fm is None:
            return None
        if (fm < target) == rising:
            lo = mid
        else:
            hi = mid
        if (hi / lo - 1.0 if log else hi - lo) < 1e-12:
            break
    return math.sqrt(lo * hi) if log else 0.5 * (lo + hi)


class TargetModel:
    """The forward model at the Physics page's settings, with the needle as a
    knob. Construct per refresh (it caches one `MEWModel` per bore)."""

    def __init__(self, material, nozzle, calibration, gap, voltage):
        self.material = material
        self.nozzle = nozzle
        self.calibration = calibration
        self.gap = float(gap)
        self.voltage = float(voltage)
        self._models = {}

    # -- the forward model -------------------------------------------------- #
    def model(self, bore=None):
        r = self.nozzle.inner_radius if bore is None else float(bore)
        key = round(r, 12)
        m = self._models.get(key)
        if m is None:
            m = self._models[key] = MEWModel(
                material=self.material,
                nozzle=dataclasses.replace(self.nozzle, inner_radius=r),
                working_distance=self.gap, calibration=self.calibration)
        return m

    def jet(self, s):
        """`MEWModel.solve` at `s`, or None where the model has no answer."""
        try:
            return self.model(s.bore).solve(ProcessParams(
                pressure=s.pressure, voltage=self.voltage, speed=s.speed,
                temperature=s.temperature))
        except (ValueError, ZeroDivisionError, OverflowError, TypeError):
            return None

    def diameter(self, s):
        st = self.jet(s)
        return None if st is None else st.fiber_diameter

    def cts(self, pressure, temperature, bore=None):
        """CTS (mm/s) at these settings -- `cts_at`, so a fitted flow or
        viscosity hook is honoured."""
        try:
            return self.model(bore).cts_at(self.voltage, pressure, temperature)
        except (ValueError, ZeroDivisionError, OverflowError, TypeError):
            return None

    def lag_limit(self):
        """The lag the jet levels off at, A*H, mm (`lag_ray_constants`)."""
        return self.model().lag_ray_constants()[0]

    # -- one knob, the others held ----------------------------------------- #
    def _checked(self, s, target):
        d = self.diameter(s)
        if d is None or abs(d - target) > TOLERANCE * target:
            return None
        return s

    def pressure_for(self, d, speed, temperature, bore):
        """The pressure that gives diameter `d` with the rest held, or None."""
        x = _bisect(lambda p: self.diameter(
            Settings(p, speed, temperature, bore)), d, *PRESSURE_SPAN)
        return None if x is None else self._checked(
            Settings(x, speed, temperature, bore), d)

    def speed_for(self, d, pressure, temperature, bore):
        x = _bisect(lambda v: self.diameter(
            Settings(pressure, v, temperature, bore)), d, *SPEED_SPAN)
        return None if x is None else self._checked(
            Settings(pressure, x, temperature, bore), d)

    def temperature_for(self, d, pressure, speed, bore):
        x = _bisect(lambda t: self.diameter(
            Settings(pressure, speed, t, bore)), d, *TEMPERATURE_SPAN,
            log=False)
        return None if x is None else self._checked(
            Settings(pressure, speed, x, bore), d)

    def speed_for_lag(self, lag, pressure, temperature, bore):
        """The speed whose steady jet lag is `lag` (mm), or None: at or past
        the A*H ceiling, or at no lag at all."""
        if not lag > 0.0:
            return None
        cts = self.cts(pressure, temperature, bore)
        if not cts:
            return None

        def lag_at(v):
            st = self.jet(Settings(pressure, v, temperature, bore))
            return None if st is None else st.lag_distance
        x = _bisect(lag_at, lag, cts * (1 + 1e-9), cts * 1e5)
        if x is None:
            return None
        got = lag_at(x)
        return x if got is not None and abs(got - lag) <= TOLERANCE * lag \
            else None

    def lag_and_diameter(self, d, lag, temperature, bore, pressure0):
        """Both targets at once: the speed the lag needs and the pressure the
        diameter needs at that speed. CTS may move with pressure once a flow
        hook is fitted, so the pair is iterated to a fixed point (one pass
        with the shipped laws). None when either has no answer."""
        p = pressure0
        s = None
        for _ in range(40):
            v = self.speed_for_lag(lag, p, temperature, bore)
            if v is None:
                return None
            s = self.pressure_for(d, v, temperature, bore)
            if s is None:
                return None
            if abs(s.pressure - p) <= 1e-9 * max(p, 1.0):
                break
            p = s.pressure
        return s

    # -- the whole set, for the map ---------------------------------------- #
    def curve(self, d, temperature, bore, speeds):
        """[(speed, pressure or None)] -- the target's set at a held
        temperature and needle, one point per speed."""
        out = []
        for v in speeds:
            s = self.pressure_for(d, v, temperature, bore) if v > 0 else None
            out.append((v, None if s is None else s.pressure))
        return out

    def grid(self, temperature, bore, speeds, pressures):
        """Diameter rows (one per pressure) over the speeds, None where the
        model has no answer, plus the CTS at each pressure."""
        rows, ctss = [], []
        for p in pressures:
            ctss.append(self.cts(p, temperature, bore))
            row = []
            for v in speeds:
                row.append(self.diameter(Settings(p, v, temperature, bore))
                           if v > 0 and p > 0 else None)
            rows.append(row)
        return rows, ctss
