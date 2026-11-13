#!/usr/bin/env python3
"""Recover the FLAT lag amplitude `A` from one plate of printed circles.

WHAT THIS IS FOR. The 3-axis analogue of `mew_lag_fit`.
`Calibration.lag_amplitude` (A) is the one per-rig number in the planar jet-lag
law, lag = H*A*(1 - eps^-b); b (`lag_saturation_exp`) is a literature constant
and is NOT fitted here.
THERE IS NO `k`. A dimensionless per-rig multiplier riding on top of a
FIXED A, fitted here, the two were one degree of
freedom written twice, so they were collapsed and what is fitted here is the
amplitude itself. A fitted A is therefore directly comparable with Hrynevich's
published 10.02/3.5 = 2.8629 rather than with a 1.
THE PROCEDURE. Ramp the speed until coiling stops -- that is CTS -- print the
circle plate at the OPERATING gap, one lap per specimen over several speed
ratios, measure each deposited radius EXCLUDING the first 90 deg of arc after
the tangent point, and feed the (commanded R, r_cp, eps) rows plus the gap to
`fit_amplitude`. WHY A CIRCLE: on a straight run the lag is a pure along-track
shift and the fibre still spans commanded-start to commanded-end, so L is
invisible; curvature turns it into a cross-track INSET, a steady state.
THE RELATION IS NOT CAO EQ 11. [S74] Cao 2022 Eq 11 gives
r_np^2 = r_cp^2 + l_0^2, the TANGENCY half, which we keep; what it omits is that
the contact runs a SHORTER circle than the nozzle, so its ground speed is v*rho
(rho = r_cp/R) and it holds the shipped law's lag at THAT speed.
`integrate_contact_path_ray` carries both; its steady state, CLOSED FORM:

    R * sqrt(1 - rho^2)  =  H*A * (1 - (rho*eps)^-b)                (STEADY)
    A = R*sqrt(1 - rho^2) / ( H * (1 - (rho*eps)^-b) )              (INVERT)

CTS cancels identically, so only eps enters; Eq 11 is the rho -> 1 limit,
recovered as L/R -> 0, which is why the sizing rule holds L/R down.
**b must be PINNED** (A.4a): CTS and b are near-degenerate on this observable, so
with b free a CTS error is absorbed into b, A moves an order of magnitude more,
and the residual goes DOWN -- it cannot police itself. The per-specimen spread
across eps is a CHECK, not a precision gain: CTS is common to the plate, so its
error is a SYSTEMATIC eps-averaging cannot reduce. That check has a FLOOR A.4a
did not name -- the placement rule itself trends +18.9 %/decade at lag/radius
0.45 against +10.5 % for a CTS believed 5 % low, falling to +5.7 % at 0.25 -- so
`ambiguity_trend` computes it and `fit_amplitude` reports the trend against it.
CONTROLS: A -> 0 returns r_cp = R exactly; the round trip
`amplitude_from_radius(R, contact_radius(R, ..., A)) == A`; (STEADY) is a hand
derivation, so the suite checks it against `integrate_contact_path_ray` marching
a real circle; r_cp >= R, or a contact below CTS, is REFUSED. A is conditional on
THE GAP it was fitted at -- the law carries it as H^1 and the literature levers
disagree on that exponent (A.3) -- so `Calibration.lag_amplitude_gap` records
which gap and `fit_amplitude` refuses to guess it."""
from __future__ import annotations

if __name__ == "__main__":  # run as a script: put the program's folders on sys.path
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "engine"))
    import app_paths  # noqa: E402,F401

import dataclasses
import io
import math

#: Hrynevich 2020 Eq.4's own gap-normalised amplitude and exponent, as shipped
#: in `Calibration.lag_amplitude` / `lag_saturation_exp`. Defaults only --
#: `fit_amplitude` reads them off the live profile so a re-fit of the SHAPE flows
#: through instead of being silently overridden here.
A_DEFAULT = 10.02 / 3.5                                # 2.8629
B_SAT = 0.304

#: The gap at which the shipped A is a definition rather than a measurement:
#: A and b were fitted to Hrynevich's curve there.
REFERENCE_GAP_MM = 3.5

#: The design dial: the ratio L/R the specimen radii are sized to. Every error
#: term in the recovered A is a function of this alone, and they pull opposite
#: ways -- modelling ambiguity rises with it, measurement error falls.
#: What sets it is the eps-flatness CHECK, not the value of A: the check's floor
#: is the placement-rule ambiguity (`ambiguity_trend`) and it has to sit below
#: the CTS error the check is meant to catch -- +5.7 %/decade at 0.25 against
#: +9.3 % for a CTS 5 % low, but +18.9 % against +10.5 % at 0.45, where the
#: check is blind. The crossover is near 0.33. 0.25 was unaffordable flat (six
#: plates) and is two nested, which is what the nested layout is for; on the
#: value of A itself the ambiguity is 1.8 % here against 5.9 % at 0.45.
DEFAULT_LR = 0.25

#: Degrees of arc after the tangent point that must be excluded from the radius
#: fit. Measured, not assumed: the contact enters the arc holding the STRAIGHT
#: lag and has to shed the difference down to the (smaller) arc lag. At 20 deg
#: -- A.4a's original guess -- the fit carries up to 44 um of that transient; at
#: 90 deg it carries under 2 um and 270 deg of arc is left to fit.
EXCLUDE_DEG = 90.0


# --------------------------------------------------------------------------- #
# the geometry: (STEADY) and (INVERT)
# --------------------------------------------------------------------------- #
def contact_radius(R, eps, *, gap, amp=A_DEFAULT, sat_exp=B_SAT):
    """Deposited radius `r_cp` (mm) for a commanded circle of radius `R` at speed
    ratio `eps`, from (STEADY). `amp` is the lag amplitude A. None when no steady
    contact circle exists -- the lag is large enough that the contact would have
    to run below CTS, which is the ray rule's form of Cao Eq 11's "no real
    root"."""
    R, eps, gap, A = float(R), float(eps), float(gap), float(amp)
    if R <= 0.0 or gap <= 0.0:
        raise ValueError("circle radius and gap must both be > 0 mm")
    if eps <= 1.0:
        raise ValueError(f"speed ratio {eps:g} is at or below CTS -- the jet "
                         f"coils there and lays no steady circle")
    if A == 0.0:
        return R                                   # the no-lag control
    amp = A * gap

    def f(rho):
        return R * math.sqrt(max(0.0, 1.0 - rho * rho)) \
            - amp * (1.0 - (rho * eps) ** -sat_exp)

    lo, hi = 1.0 / eps + 1e-12, 1.0 - 1e-15        # rho*eps > 1, rho < 1
    if f(hi) > 0.0:
        return None                                # lag outruns the arc
    for _ in range(200):                           # bisection: f decreases
        mid = 0.5 * (lo + hi)                      # monotonically on (1/eps, 1)
        if f(mid) > 0.0:
            lo = mid
        else:
            hi = mid
    return R * 0.5 * (lo + hi)


def amplitude_from_radius(R, r_cp, eps, *, gap, sat_exp=B_SAT):
    """`A` from one commanded/deposited radius pair -- (INVERT), closed form."""
    R, r_cp, eps, gap = float(R), float(r_cp), float(eps), float(gap)
    if R <= 0.0 or gap <= 0.0:
        raise ValueError("circle radius and gap must both be > 0 mm")
    if eps <= 1.0:
        raise ValueError(f"speed ratio {eps:g} is at or below CTS")
    if r_cp >= R:
        raise ValueError(
            f"deposited radius {r_cp:g} mm is not inside the commanded "
            f"{R:g} mm -- the lag pulls the contact IN, so this is the two "
            f"radii swapped or a scale error, not a rig with a negative lag")
    if r_cp <= 0.0:
        raise ValueError("deposited radius must be > 0 mm")
    rho = r_cp / R
    if rho * eps <= 1.0:
        raise ValueError(
            f"the deposited circle implies a contact speed of {rho * eps:.3f} "
            f"x CTS -- at or below CTS there is no steady contact circle, so "
            f"either the measurement or the believed CTS is wrong")
    return R * math.sqrt(1.0 - rho * rho) \
        / (gap * (1.0 - (rho * eps) ** -sat_exp))


def _filter_amplitude(R, r_cp, eps, *, gap, sat_exp=B_SAT):
    """The A the RETIRED first-order filter would report for the same pair.

    Its steady circle is |C| = R/sqrt(1 + (omega*tau)^2) = R^2/sqrt(R^2 + L^2),
    so L follows in closed form. Not a rival we ship -- it is the rival A.4a
    says Cao cannot exclude, and the gap between the two is the honest width of
    "which placement rule you inverted"."""
    L = math.sqrt(max(0.0, R ** 4 / (r_cp * r_cp) - R * R))
    return L / (gap * (1.0 - eps ** -sat_exp))


def _trend_pct_per_decade(pairs):
    """Least-squares slope of A against log10(eps), as % of the mean A."""
    xs = [math.log10(e) for e, _a in pairs]
    amps = [a for _e, a in pairs]
    xm = sum(xs) / len(xs)
    am = sum(amps) / len(amps)
    sxx = sum((x - xm) ** 2 for x in xs)
    if sxx <= 1e-12 or am == 0.0:
        return 0.0
    slope = sum((x - xm) * (a - am) for x, a in zip(xs, amps)) / sxx
    return 100.0 * slope / am


def ambiguity_trend(rows, *, gap, amp=A_DEFAULT, sat_exp=B_SAT):
    """How much eps-trend the PLACEMENT RULE alone would produce on this set.

    This is the flatness check's floor, and it is not small. A.4a says a trend
    means the believed CTS or the shape is wrong; measured, there is a third
    source it did not name. Feed the shipped ray rule's own circles to the
    retired filter's inversion and the recovered A is NOT flat in eps -- at the
    shipped eta = 0.45 it trends +18.9 %/decade on the {2,3,4,6} ladder, while a
    CTS believed 5 % low trends only +10.5 %. Both are positive and monotone, so
    they ADD; the check cannot see a CTS error smaller than the modelling
    ambiguity it is sitting on.

    The floor falls with the ratio -- +11.4 %/decade at eta = 0.35, +8.3 % at
    0.30, +5.7 % at 0.25 -- because all three placement rules converge as
    L/R -> 0. Sizing R per eps does not cause this and does not cure it: at a
    FIXED 8 mm radius the same ladder trends +18.3 %/decade. It is a property of
    the observable, so the fit reports it as a band rather than pretending the
    trend is clean."""
    pairs = []
    for r in rows:
        R = float(r.commanded_r_mm)
        eps = float(r.eps)
        ray = contact_radius(R, eps, gap=gap, amp=amp, sat_exp=sat_exp)
        if ray is None:
            continue
        pairs.append((eps, _filter_amplitude(R, ray, eps, gap=gap,
                                             sat_exp=sat_exp)))
    if len(pairs) < 2:
        return 0.0
    return _trend_pct_per_decade(pairs)


def commanded_radius(eps, *, gap, amp_prior=A_DEFAULT, lr_target=DEFAULT_LR,
                     sat_exp=B_SAT):
    """The specimen radius the sizing rule asks for: R = L / lr_target, with L
    the straight-run lag at the PRIOR amplitude. R is proportional to the gap
    because L is, which makes the whole dimensionless problem -- and therefore
    every error term -- a function of `lr_target` alone.

    The prior amplitude and the gap are refused unless positive: either at 0
    sizes a zero-radius circle, which divided by zero further down the layout."""
    if not (math.isfinite(lr_target) and lr_target > 0.0):
        raise ValueError("the lag/radius ratio must be > 0")
    if not (math.isfinite(amp_prior) and amp_prior > 0.0):
        raise ValueError("the prior lag amplitude A must be > 0")
    if not (math.isfinite(gap) and gap > 0.0):
        raise ValueError("the printed gap must be > 0 mm")
    if eps <= 1.0:
        raise ValueError(f"speed ratio {eps:g} is at or below CTS")
    lag = float(amp_prior) * float(gap) * (1.0 - float(eps) ** -sat_exp)
    return lag / float(lr_target)


# --------------------------------------------------------------------------- #
# the fit
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class CircleRow:
    """One measured specimen. `deposited_r_mm` is the radius fitted to the
    deposited arc with the entry sector excluded (see EXCLUDE_DEG)."""
    eps: float
    commanded_r_mm: float
    deposited_r_mm: float
    label: str = ""

    @property
    def inset_mm(self):
        return self.commanded_r_mm - self.deposited_r_mm


@dataclasses.dataclass(frozen=True)
class CircleFit:
    A: float                     # the mean per-specimen amplitude
    A_gap_mm: float              # the gap it was fitted at -- A means nothing
                                 # without it; this is what goes to Calibration
    per_row: tuple               # ((label, eps, A, inset_mm), ...)
    spread_pct: float            # (max - min) / mean over the specimens
    trend_pct_per_decade: float  # d A / d log10(eps), as % of A
    ambiguity_trend_pct: float   # the trend the PLACEMENT RULE alone would
                                 # produce on this geometry -- the flatness
                                 # check's floor, not a measurement
    n: int
    notes: tuple                 # always non-empty


def fit_amplitude(rows, *, gap, calibration=None):
    """Fit `Calibration.lag_amplitude` (A) from measured circles.

    `rows` are `CircleRow`s (or tuples in its field order). `gap` is the gap the
    plate was PRINTED at and is required -- A is meaningless without it, and
    guessing it is exactly the defect `lag_amplitude_gap` exists to close.
    `calibration` supplies b; the profile's own `lag_amplitude` is deliberately
    NOT read, because it is the quantity being measured -- seeding the inversion
    with a previous fit would compound two calibrations.
    """
    rows = [r if isinstance(r, CircleRow) else CircleRow(*r) for r in rows]
    if not rows:
        raise ValueError("need at least one measured circle")
    gap = float(gap)
    if gap <= 0.0:
        raise ValueError("the printed gap must be > 0 mm")

    sat_exp = B_SAT
    if calibration is not None:
        sat_exp = float(calibration.lag_saturation_exp)

    per = []
    for r in rows:
        A = amplitude_from_radius(r.commanded_r_mm, r.deposited_r_mm, r.eps,
                                  gap=gap, sat_exp=sat_exp)
        per.append((r.label or f"eps{r.eps:g}", float(r.eps), A, r.inset_mm))

    amps = [p[2] for p in per]
    a_mean = sum(amps) / len(amps)
    spread = 100.0 * (max(amps) - min(amps)) / a_mean if a_mean else 0.0

    # least-squares slope of A against log10(eps): the flatness check. One
    # specimen, or several at one eps, cannot have a trend -- report 0 and say so.
    xs = [math.log10(p[1]) for p in per]
    xm = sum(xs) / len(xs)
    sxx = sum((x - xm) ** 2 for x in xs)
    trend = (_trend_pct_per_decade([(p[1], p[2]) for p in per])
             if sxx > 1e-12 else 0.0)
    # ... and the floor it has to clear before it means anything about CTS
    band = ambiguity_trend(rows, gap=gap, amp=a_mean,
                           sat_exp=sat_exp) if sxx > 1e-12 else 0.0

    notes = [f"A = {a_mean:.4f} at a {gap:g} mm gap. It is CONDITIONAL on that "
             f"gap: the law carries the gap as H^1 and the literature levers "
             f"disagree on the exponent, so this absorbs that disagreement at "
             f"{gap:g} mm and says nothing about any other gap.",
             "b was pinned. CTS and b are near-degenerate on this observable, "
             "so a free b absorbs a CTS error and LOWERS the residual while "
             "moving A badly -- the residual cannot police it."]
    if gap != REFERENCE_GAP_MM:
        notes.append(
            f"the shipped A = {A_DEFAULT:.4f} and b were fitted to "
            f"Hrynevich's curve at {REFERENCE_GAP_MM:g} mm. This fit is at "
            f"{gap:g} mm, so a departure from {A_DEFAULT:.4f} here is not by "
            f"itself a rig result.")
    if sxx <= 1e-12:
        notes.append("all specimens sit at one speed ratio, so there is no "
                     "flatness check -- a CTS error would be invisible.")
    elif abs(trend) <= abs(band) + 1e-9:
        notes.append(
            f"A trends {trend:+.1f} % per decade of eps, INSIDE the "
            f"{abs(band):.1f} % the placement-rule ambiguity alone spans at "
            f"these radii. That is not evidence about CTS: inverting the "
            f"retired first-order filter instead of the shipped ray rule "
            f"would move A across the ladder by that much on its own. Shrink "
            f"the lag/radius ratio to shrink the floor.")
    else:
        notes.append(
            f"A trends {trend:+.1f} % per decade of eps, ABOVE the "
            f"{abs(band):.1f} % floor the placement-rule ambiguity spans at "
            f"these radii. The excess means the believed CTS or the shape is "
            f"wrong; it CANNOT say which, and since CTS is independently "
            f"measurable and b is not, treat it as a CTS check.")
    notes.append("CTS is common to every specimen on the plate, so its error "
                 "is a systematic that replicates; averaging over eps does not "
                 "reduce it.")

    return CircleFit(A=a_mean, A_gap_mm=gap, per_row=tuple(per),
                     spread_pct=spread, trend_pct_per_decade=trend,
                     ambiguity_trend_pct=band, n=len(per),
                     notes=tuple(notes))


def summarise(fit: CircleFit) -> str:
    """A human-readable report -- what a runbook or the GUI prints."""
    out = [f"lag amplitude A = {fit.A:.4f}   (fitted at a "
           f"{fit.A_gap_mm:g} mm gap, {fit.n} specimen(s); "
           f"Hrynevich's own A = {A_DEFAULT:.4f})",
           f"  spread across specimens {fit.spread_pct:.1f} %, "
           f"trend {fit.trend_pct_per_decade:+.1f} % per decade of eps "
           f"(placement-rule floor {abs(fit.ambiguity_trend_pct):.1f} %)", ""]
    out.append(f"  {'specimen':<18}{'eps':>7}{'inset mm':>11}{'A':>9}")
    for label, eps, amp, inset in fit.per_row:
        out.append(f"  {label:<18}{eps:>7.3g}{inset:>11.4f}{amp:>9.4f}")
    out.append("")
    out += [f"  - {n}" for n in fit.notes]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# the operator's entry point: the page's own measurement CSV, filled in
# --------------------------------------------------------------------------- #
def rows_from_csv(path):
    """Read the Experiment-sweeps measurement CSV back after the
    `measured_r_mm` column has been filled in. Returns (rows, gap).

    The gap comes from the file, not from the operator's memory: it is the gap
    the plate was PRINTED at, the generator wrote it into every line, and A has
    no meaning without it."""
    import csv as _csv
    rows, gaps = [], set()
    with io.open(path, newline="", encoding="utf-8-sig") as fh:
        for rec in _csv.DictReader(fh):
            raw = (rec.get("measured_r_mm") or "").strip()
            if not raw:
                continue                       # not measured yet -- skip, quietly
            rows.append(CircleRow(eps=float(rec["eps"]),
                                  commanded_r_mm=float(rec["commanded_r_mm"]),
                                  deposited_r_mm=float(raw),
                                  label=rec.get("label", "")))
            gaps.add(round(float(rec["gap_mm"]), 6))
    if not rows:
        raise ValueError(f"{path}: no row has a measured_r_mm value yet")
    if len(gaps) != 1:
        raise ValueError(f"{path}: rows were printed at different gaps "
                         f"({sorted(gaps)}) -- A is per-gap, so fit them "
                         f"separately")
    return rows, gaps.pop()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Fit Calibration.lag_amplitude (A) from a printed circle "
                    "plate. Give it the measurement CSV the Experiment-sweeps "
                    "page exported, with measured_r_mm filled in.")
    ap.add_argument("csv", help="the *_measure.csv, measured_r_mm filled in")
    ap.add_argument("--gap", type=float, default=None,
                    help="override the gap the CSV records (mm). Only for a "
                         "plate whose gap the file got wrong -- A is per-gap")
    a = ap.parse_args(argv)
    rows, gap = rows_from_csv(a.csv)
    if a.gap is not None:
        gap = a.gap
    fit = fit_amplitude(rows, gap=gap)
    print(summarise(fit))
    print()
    print("Set these two on the Physics page's Calibration tab:")
    print(f"  cal.lag_amplitude      = {fit.A:.6g}")
    print(f"  cal.lag_amplitude_gap  = {fit.A_gap_mm:g}")
    return 0


__all__ = ["A_DEFAULT", "B_SAT", "REFERENCE_GAP_MM", "DEFAULT_LR",
           "EXCLUDE_DEG", "contact_radius", "amplitude_from_radius",
           "commanded_radius", "ambiguity_trend", "CircleRow", "CircleFit",
           "fit_amplitude", "summarise", "rows_from_csv"]


if __name__ == "__main__":
    import sys
    sys.exit(main())
