#!/usr/bin/env python3
"""Recover the mandrel lag radii from ONE calibration print -- micrograph-only.

WHAT THIS IS FOR. `mew_mandrel` predicts fibre deviation from two lag radii, and
MANDREL_LAG_DERIVATION.md §12.9 froze how they are obtained: the planar jet-lag
law supplies the FORM and the ANISOTROPY, the AMPLITUDE is calibrated per rig and
never carried. This module is that calibration, off gaps measured on a micrograph
of a printed tube -- the paper's "micrograph-only" claim (§I.3). numpy only, no
Tk, no VTK, no scipy: the fit is a 2-parameter Levenberg-Marquardt written here.

THE PROCEDURE (§I.1 step 2, ~half a day). Ramp the speed on the mandrel until
coiling stops -- that is CTS. Print a calibration tube of parallel lines in
ALTERNATING directions about 0.5 mm apart, at three winding angles with one near
45 deg, micrograph it, measure ONE WIDE and ONE NARROW gap per angle, and feed
those rows plus (mandrel radius, gap, print speed, CTS) to `fit_radii`.

WHY WIDE AND NARROW. Alternating print directions push neighbouring lines
opposite ways, so gaps alternate `s - 2D` and `s + 2D` about the intended spacing
`s` and the alternation IS the deviation: `D = (wide - narrow) / 4`. `s` cancels,
so no absolute scale is needed; the spacing is used only as a CHECK.

WHAT IS FITTED. `deviation(alpha)` has exactly two shape parameters, `R_ax` and
`R_cir`, so three or more angles determine both; they are fitted in LOG space so
neither can go negative. **`S` is HELD at its geometric `r + 0.75*H`**: it is
partly degenerate with the amplitude -- the recovered `R_ax` moves 43% -> -3% as
S goes 3.60 -> 4.00 -- and separating them needs a speed sweep this procedure
does not ask for, so the fitted amplitude carries S's uncertainty and must say
so. The TANGENCY CAP is not applied while fitting: the fit must invert exactly
the function the published inversion did, and the cap lives downstream (§12.5).

THE CONTROLS, which travel with the method. Eq 5 is identically zero at 0 and
90 deg, so a row at either is a NULL CHECK and is never fitted (Neuhaus's own
prints read 1.3 and 1.9 um; a large value means the measurement). The mean
measured spacing `(wide+narrow)/2` must land on the programmed spacing -- hers
agree to 0.2-0.3%, and a few percent means the micrograph scale is wrong.
`wide < narrow` is REFUSED rather than fitted: it flips the deviation's sign and
would fit a mirror-image rig.

NOT PORTED: `measure_deviation.py`'s image half assumes Neuhaus's stitched
unrolled scan at 302.5 px/mm, so measuring stays the viewer's measure tool.
COMPARATOR: her published inversion."""
from __future__ import annotations

import dataclasses
import math

import numpy as np

import mew_mandrel as mm
from mew_physics import Calibration, MEWModel

#: A row at or beyond this many degrees from 0 or 90 carries shape information;
#: closer than this Eq 5 is too flat to fit and the row is treated as a null
#: check instead. Neuhaus's own sweep steps in 10 deg, so this excludes exactly
#: her 0 and 90 rows.
NULL_TOL_DEG = 2.0

#: Below this the fit has fewer informative angles than parameters.
MIN_FIT_ROWS = 2

#: Bracket on either lag radius, mm. The same [1e-4, 60] `run_stage2.py` used
#: for its root find, and it is load-bearing rather than decorative: the fit runs
#: in LOG space, so an unclamped Levenberg step out of a near-singular Jacobian
#: overflows `math.exp` and kills the run. Measured on a rig whose
#: true radii are 6x the default seed.
R_MIN, R_MAX = 1e-4, 60.0
LOG_MIN, LOG_MAX = math.log(R_MIN), math.log(R_MAX)


@dataclasses.dataclass(frozen=True)
class GapRow:
    """One winding angle's pair of measured gaps, in mm.

    `wide` and `narrow` are ADJACENT gaps between printed lines -- one of each,
    read off the micrograph at the same place. Which is which is decided by the
    measurement, not by the model; see the wrong-way control above.
    """
    angle_deg: float
    wide_mm: float
    narrow_mm: float
    #: this ROW's programmed spacing, mm, for the scale check; None falls back
    #: to `fit_radii`'s one `spacing_mm`. Per row because the calibration tube
    #: (`mew_sweeps.generate_lag_tube`) closes each band around the mandrel,
    #: which nudges each angle's spacing by a different amount.
    programmed_mm: float | None = None

    @property
    def deviation_mm(self) -> float:
        return 0.25 * (self.wide_mm - self.narrow_mm)

    @property
    def spacing_mm(self) -> float:
        return 0.5 * (self.wide_mm + self.narrow_mm)


@dataclasses.dataclass(frozen=True)
class LagFit:
    """What one calibration print yields, and what it does not.

    What a calibration KEEPS is mandrel-only: the pair
    `mandrel_lag_axial` / `mandrel_lag_circ` and the speed it was fitted at,
    `mandrel_lag_speed`. `A` is reported, on Hrynevich's scale, but NOT written
    to `Calibration.lag_amplitude`: flat prints read that constant, and a
    mandrel calibration must not move them. A rotary lay recovers the same
    amplitude from the pair and its speed (`mandrel_calibration`).
    """
    R_ax: float                  # mm, fitted
    R_cir: float                 # mm, fitted
    ratio: float                 # R_cir / R_ax -- what the deposition path uses
    A: float                     # R_ax / L_unit: Calibration.lag_amplitude
    lag_ref_mm: float            # L_unit = H*(1 - eps^-b), the law at A = 1
    eps: float                   # speed / CTS
    S: float                     # HELD at r + frac*H, not fitted
    rms_um: float                # of the fitted rows
    n_fit: int
    residuals_um: tuple          # (angle_deg, measured_um, model_um) per fit row
    nulls: tuple                 # (angle_deg, measured_um) per null row
    scale_pct: float | None      # measured mean spacing vs intended, %
    notes: tuple                 # human-readable caveats, always non-empty


def _lag_reference(gap, speed, cts, calibration=None):
    """`L_unit` -- the shipped lag law's SHAPE at this gap and speed, A = 1.

    Read from `MEWModel.lag_angle` rather than re-derived: the amplitude being
    calibrated multiplies the law as shipped, so the two must be the same
    expression, floor and all. `lag_amplitude` is forced to 1 here whatever the
    profile holds -- that is what makes the quotient `R_ax / L_unit` the
    amplitude A itself rather than a correction to whatever A the profile
    already carried.
    """
    cal = Calibration() if calibration is None else calibration
    cal = dataclasses.replace(cal, lag_amplitude=1.0)
    model = MEWModel(calibration=cal, working_distance=float(gap))
    eps = float(speed) / float(cts)
    return (model.working_distance
            * math.tan(math.radians(model.lag_angle(eps)))), eps


def _seed(rows, radius, S, gap):
    """A starting `(R_ax, R_cir)` sized to THIS rig, not to Neuhaus's.

    A fixed seed is fine when the rig resembles the one the seed came from and
    useless otherwise: on r = 1.5, H = 3.5 the true radii are ~6x Neuhaus's, and
    starting there sent the log-space fit out of range. So scan `R_ax` on a log
    grid against the single measured angle nearest 45 deg -- where the deviation
    curve peaks and is most sensitive -- holding the anisotropy at the
    parameter-free `r/(r+H)`. 40 evaluations of one angle, about 0.2 s.
    """
    row = min(rows, key=lambda r: abs(r.angle_deg - 45.0))
    target = row.deviation_mm
    ratio = radius / (radius + gap)
    best, best_err = 1.0, float("inf")
    for R_ax in np.exp(np.linspace(math.log(1e-3), math.log(R_MAX), 40)):
        d = mm.deviation(radius, row.angle_deg, S, R_ax, R_ax * ratio)[0]
        err = abs(d - target)
        if err < best_err:
            best, best_err = float(R_ax), err
    return best, best * ratio


def _levenberg_marquardt(resid, p0, max_iter=60, step_tol=1e-10):
    """Least squares for a handful of parameters, numpy only.

    scipy is not a dependency of this application (nothing shipped imports it),
    and the problem is two parameters over a smooth landscape, so a plain LM
    with a central-difference Jacobian is enough. `max_iter` is a real budget,
    not a formality: `mew_mandrel.deviation` is a 4001-point scan plus 90
    bisections, ~4 ms a call, so a three-angle fit costs about 0.1 s an
    iteration and this bounds a GUI button at a few seconds.
    """
    p = np.asarray(p0, dtype=np.float64).copy()
    r = np.asarray(resid(p), dtype=np.float64)
    cost = float(r @ r)
    lam = 1e-3
    n = len(p)
    for _ in range(max_iter):
        J = np.empty((len(r), n), dtype=np.float64)
        for j in range(n):
            h = 1e-6 * max(1.0, abs(p[j]))
            up, dn = p.copy(), p.copy()
            up[j] += h
            dn[j] -= h
            J[:, j] = (np.asarray(resid(up), dtype=np.float64)
                       - np.asarray(resid(dn), dtype=np.float64)) / (2.0 * h)
        g = J.T @ r
        H = J.T @ J
        improved = False
        for _ in range(30):
            damped = H + lam * np.diag(np.maximum(np.diag(H), 1e-12))
            try:
                step = np.linalg.solve(damped, -g)
            except np.linalg.LinAlgError:
                lam *= 10.0
                continue
            pn = p + step
            rn = np.asarray(resid(pn), dtype=np.float64)
            cn = float(rn @ rn)
            if cn < cost:
                p, r, cost = pn, rn, cn
                lam = max(lam * 0.3, 1e-12)
                improved = True
                break
            lam *= 10.0
        if not improved or float(np.linalg.norm(step)) < step_tol:
            break
    return p, cost


def fit_radii(rows, *, radius, gap, speed, cts, spacing_mm=None,
              calibration=None, support_frac=0.75, p0=None):
    """Invert `mew_mandrel.deviation` for `(R_ax, R_cir)`, and report `A`.

    `rows` are `GapRow`s -- see the module docstring for how they are measured.
    `radius`/`gap` are the mandrel radius and the nozzle-to-surface gap in mm;
    `speed` is the print speed and `cts` the critical translation speed from the
    coiling ramp, both mm/s. `spacing_mm` is the spacing the g-code programmed,
    used ONLY for the scale check.

    Raises `ValueError` on an input the method cannot mean: a gap pair read the
    wrong way round, a non-positive geometry, or fewer than two informative
    angles.
    """
    radius, gap = float(radius), float(gap)
    if radius <= 0.0 or gap <= 0.0:
        raise ValueError("mandrel radius and gap must both be positive "
                         "(got r = %r, H = %r)" % (radius, gap))
    if float(cts) <= 0.0 or float(speed) <= 0.0:
        raise ValueError("print speed and CTS must both be positive")

    fit_rows, nulls = [], []
    for row in rows:
        if row.wide_mm < row.narrow_mm:
            raise ValueError(
                "row at %.1f deg has wide %.4f mm < narrow %.4f mm: the two "
                "gaps are the wrong way round. The WIDE gap is the one the two "
                "neighbouring lines were pushed apart by; reading them swapped "
                "flips the sign of the deviation and would fit a mirror-image "
                "rig." % (row.angle_deg, row.wide_mm, row.narrow_mm))
        a = float(row.angle_deg)
        if a < NULL_TOL_DEG or a > 90.0 - NULL_TOL_DEG:
            nulls.append((a, row.deviation_mm * 1000.0))
        else:
            fit_rows.append(row)
    if len(fit_rows) < MIN_FIT_ROWS:
        raise ValueError(
            "need at least %d winding angles away from 0 and 90 deg to fit two "
            "radii; got %d (rows at 0/90 are null checks, not fit points)"
            % (MIN_FIT_ROWS, len(fit_rows)))

    S = mm.support_length(radius, gap, frac=support_frac)
    angles = [float(r.angle_deg) for r in fit_rows]
    meas_um = np.array([r.deviation_mm * 1000.0 for r in fit_rows])

    def radii_of(p):
        """`(R_ax, R_cir)` from the fit's parameters, with the ANISOTROPY BOUNDED.

        The fit runs on `(log R_ax, logit ratio)` rather than on two independent
        log radii, so `0 < R_cir < R_ax` holds structurally. That is the model's
        own invariant, not a convenience: on a cylinder the collector curves away
        circumferentially and not axially, which is exactly why the contact locus
        is an ellipse with `R_ax > R_cir` (`mew_mandrel.ContactPlacement`), and
        the geometric `R_cir` (`mew_mandrel.circumferential_lag`) is below
        `R_ax` for every real geometry.

        WHAT THE BOUND BUYS, measured both ways. On a curve that IS
        the model's shape it buys nothing: bounded and unbounded agree to the
        last digit on Neuhaus's three-angle subset (0.9107 / 0.6935, RMS 1.63
        um). What it buys is on a curve that is NOT -- a mis-transcribed row,
        the likeliest input error here, since the deviation table is read off a
        micrograph by hand. Given one angle's deviation swapped for its
        neighbour's, the unbounded fit returned R_ax 0.65 / R_cir 0.82, ratio
        1.26, which no cylinder can produce and which nothing downstream would
        question. Bounded, the same input pins the ratio at 1.0 and the RMS note
        below fires at 21% of the mean deviation, so a bad transcription reads
        as a bad transcription instead of as a calibration.
        """
        R_ax = math.exp(min(max(p[0], LOG_MIN), LOG_MAX))
        ratio = 1.0 / (1.0 + math.exp(-min(max(p[1], -30.0), 30.0)))
        return R_ax, R_ax * ratio

    def model_um(p):
        R_ax, R_cir = radii_of(p)
        return np.array([mm.deviation(radius, a, S, R_ax, R_cir)[0] * 1000.0
                         for a in angles])

    def start(R_ax, ratio):
        ratio = min(max(ratio, 1e-3), 1.0 - 1e-3)
        return np.array([math.log(min(max(R_ax, R_MIN), R_MAX)),
                         math.log(ratio / (1.0 - ratio))])

    # MULTI-START. One start is enough on a well-conditioned nine-angle sweep and
    # is not on three, so the geometric anisotropy is tried alongside a flatter
    # and a rounder one and the lowest cost wins. `_seed` sizes R_ax from the data
    # first, so every start is already in the right decade.
    if p0 is not None:
        starts = [start(p0[0], p0[1] / p0[0])]
    else:
        R_ax0, _R_cir0 = _seed(fit_rows, radius, S, gap)
        # The geometric anisotropy AT THE SEED's own R_ax: since
        # `mew_mandrel.circumferential_lag` is non-linear in R_ax there is no
        # single geometric ratio, so the start is evaluated where the fit
        # begins rather than read off a formula.
        geometric = (mm.circumferential_lag(R_ax0, radius, gap) / R_ax0
                     if R_ax0 > 0.0 else 0.75)
        starts = [start(R_ax0, q) for q in (geometric, 0.45, 0.90)]

    best_p, best_cost = None, float("inf")
    for p_start in starts:
        p, cost = _levenberg_marquardt(lambda p: model_um(p) - meas_um, p_start)
        if cost < best_cost:
            best_p, best_cost = p, cost
    p, cost = best_p, best_cost
    R_ax, R_cir = radii_of(p)
    model = model_um(p)
    rms = float(math.sqrt(cost / len(angles)))

    ratio_pinned = (R_cir / R_ax) > 0.995 if R_ax else False
    lag_ref, eps = _lag_reference(gap, speed, cts, calibration)
    amp = R_ax / lag_ref if lag_ref > 0.0 else float("nan")

    scale_pct = None
    if any(r.programmed_mm for r in rows):
        # per-row programmed spacings: the mean of each row's own ratio, over
        # the rows that have one (a row without falls back to `spacing_mm`)
        ratios = [r.spacing_mm / float(r.programmed_mm or spacing_mm)
                  for r in rows if r.programmed_mm or spacing_mm]
        scale_pct = 100.0 * (float(np.mean(ratios)) - 1.0)
    elif spacing_mm:
        meas_spacing = float(np.mean([r.spacing_mm for r in rows]))
        scale_pct = 100.0 * (meas_spacing / float(spacing_mm) - 1.0)

    notes = ["S held at its geometric value %.3f mm (r + %.2f*H), not fitted: "
             "S and the amplitude are partly degenerate (+/-20%% over a "
             "plausible S range on Neuhaus's data), and separating them needs a "
             "speed sweep this procedure does not ask for."
             % (S, support_frac)]
    if ratio_pinned:
        notes.append("the fitted anisotropy has run to its physical ceiling "
                     "(R_cir = R_ax). A cylinder curves away circumferentially "
                     "and not axially, so R_cir is always the smaller: this "
                     "means the measured curve is not the model's shape, not "
                     "that the rig is isotropic.")
    if len(angles) > 2 and rms > 0.15 * float(np.mean(np.abs(meas_um))):
        notes.append("RMS is %.0f%% of the mean measured deviation: the curve's "
                     "SHAPE is not being reproduced, and no amplitude can fix "
                     "that. Check the winding angles, and which gap was read as "
                     "the wide one, before trusting A."
                     % (100.0 * rms / float(np.mean(np.abs(meas_um)))))
    if len(angles) < 3:
        notes.append("only %d fit angle(s) for two parameters: the fit is "
                     "exactly determined, so the RMS is 0 by construction and "
                     "says nothing about the fit's quality. Three angles is the "
                     "procedure's minimum." % len(angles))
    if not any(abs(a - 45.0) <= 15.0 for a in angles):
        notes.append("no angle within 15 deg of 45: the deviation curve peaks "
                     "there, so the fit is leaning on its flanks.")
    if nulls:
        worst = max(abs(d) for _a, d in nulls)
        notes.append("null check at %s deg: worst |D| = %.1f um (Eq 5 is "
                     "identically 0 there, so this is the measurement's own "
                     "noise floor)."
                     % ("/".join("%.0f" % a for a, _d in nulls), worst))
    else:
        notes.append("no row at 0 or 90 deg, so the null check did not run.")
    if scale_pct is None:
        notes.append("no intended spacing given, so the scale check did not "
                     "run; A inherits any micrograph scale error.")
    elif abs(scale_pct) > 2.0:
        notes.append("SCALE CHECK: measured spacing is %+.1f%% off the "
                     "programmed spacing. Above ~2%% suspect the micrograph "
                     "scale before the model; every absolute number here "
                     "inherits it." % scale_pct)
    else:
        notes.append("scale check: measured spacing %+.2f%% vs programmed."
                     % scale_pct)
    if eps <= 1.0:
        notes.append("speed is at or below CTS (eps = %.2f), where the lag law "
                     "floors to zero: A is not meaningful from this print."
                     % eps)

    return LagFit(
        R_ax=R_ax, R_cir=R_cir, ratio=(R_cir / R_ax if R_ax else float("nan")),
        A=amp, lag_ref_mm=lag_ref, eps=eps, S=S, rms_um=rms, n_fit=len(angles),
        residuals_um=tuple((a, float(m), float(g))
                           for a, m, g in zip(angles, meas_um, model)),
        nulls=tuple(nulls), scale_pct=scale_pct, notes=tuple(notes))


def predict_deviation(alpha_deg, *, radius, gap, speed, cts,
                      calibration=None, support_frac=0.75, model=None):
    """The deviation D (mm) this module's own model predicts -- `fit_radii`
    run FORWARDS, for sizing a calibration tube before it is printed.

    The semantics are the fit's, so a prediction and a later fit of the same
    rig agree by construction: `R_ax = A * L_unit` (A the profile's
    `lag_amplitude`, `L_unit` the law at A = 1), `R_cir` the calibrated ratio
    `mandrel_lag_circ / mandrel_lag_axial` times `R_ax` when the profile holds
    that pair, else the R_cir law (`MEWModel.circumferential_lag`: Eq. 7, or
    the user's `rcir` override -- `model` carries the rig's material and nozzle
    for that law, see `predict_radii`), capped at tangency as
    the deposition path caps it, and `S` held at `r + support_frac * H`.
    Returns 0.0 at 0 and 90 deg (Eq 5 is identically zero there) and at or
    below CTS (the lag law floors to zero).
    """
    R_ax, R_cir = predict_radii(radius=radius, gap=gap, speed=speed, cts=cts,
                                calibration=calibration, model=model)
    if R_ax <= 0.0:
        return 0.0
    S = mm.support_length(radius, gap, frac=support_frac)
    return mm.deviation(radius, float(alpha_deg), S, R_ax, R_cir)[0]


def mandrel_calibration(cal, gap):
    """The Calibration a ROTARY lay runs on: `cal` with its two mandrel-only
    values folded in, or `cal` itself when it carries neither.

    * `mandrel_cts` replaces `cts_ref` -- the CTS measured on the mandrel.
    * the pair `mandrel_lag_axial` / `_circ` WITH `mandrel_lag_speed` replaces
      `lag_amplitude` by R_ax / L(eps_cal): the amplitude that puts the jet
      model's lag at exactly R_ax at the speed the pair was measured at, and
      scales it with the lag law everywhere else, R_ax(eps) = R_ax,cal *
      L(eps) / L(eps_cal). eps_cal uses the CTS this lay will use, so the
      identity at the calibration speed holds whatever CTS that is.

    Only a rotary lay may call this (`mandrel_model`); a flat one never does,
    which is the whole guarantee that a mandrel calibration leaves every flat
    print byte-identical. The pair's RATIO is read separately, by
    `mew_mandrel.placement_for_frame`, exactly as before."""
    changes = {}
    cts_m = getattr(cal, "mandrel_cts", None)
    if cts_m:
        changes["cts_ref"] = float(cts_m)
    ax = getattr(cal, "mandrel_lag_axial", None)
    ci = getattr(cal, "mandrel_lag_circ", None)
    v_cal = getattr(cal, "mandrel_lag_speed", None)
    if ax and ci and v_cal:
        cts = float(cts_m or cal.cts_ref)
        lag_unit, _eps = _lag_reference(gap, float(v_cal), cts, cal)
        if lag_unit > 0.0:
            changes["lag_amplitude"] = float(ax) / lag_unit
    return dataclasses.replace(cal, **changes) if changes else cal


def mandrel_model(model):
    """`model` for a ROTARY lay: the same jet model on `mandrel_calibration`.
    Returns `model` itself when there is nothing mandrel-only to fold in."""
    cal = mandrel_calibration(model.calibration, model.working_distance)
    if cal is model.calibration:
        return model
    out = MEWModel(material=model.material, nozzle=model.nozzle,
                   working_distance=model.working_distance, calibration=cal)
    out.context = dict(getattr(model, "context", {}) or {})
    return out


def predict_radii(*, radius, gap, speed, cts, calibration=None, model=None):
    """`(R_ax, R_cir)` in mm as `predict_deviation` forms them -- see there.
    `(0.0, 0.0)` at or below CTS, where the lag law floors to zero. The
    amplitude is the one a rotary lay runs on (`mandrel_calibration`).

    R_cir goes through `MEWModel.circumferential_lag`, the same seam the
    placement asks (`mew_mandrel.placement_for_frame`), so a user-replaced
    R_cir law moves the prediction exactly as it moves the lay. `model`, when
    given, is the rig's model -- its material and nozzle are what that law
    sees; `calibration` wins over its calibration. Without one, a model is
    built on `calibration` with the factory material and nozzle."""
    if calibration is None:
        calibration = (model.calibration if model is not None
                       else Calibration())
    cal = mandrel_calibration(calibration, gap)
    lag_unit, _eps = _lag_reference(gap, speed, cts, cal)
    R_ax = float(cal.lag_amplitude) * lag_unit
    if R_ax <= 0.0:
        return 0.0, 0.0
    ax = getattr(cal, "mandrel_lag_axial", None)
    ci = getattr(cal, "mandrel_lag_circ", None)
    if ax and ci:
        R_cir = R_ax * float(ci) / float(ax)
    elif str((getattr(cal, "law_source", None) or {}).get("rcir")
             or "").strip():
        lawm = MEWModel(
            material=getattr(model, "material", None),
            nozzle=getattr(model, "nozzle", None),
            working_distance=gap, calibration=cal)
        lawm.context = dict(getattr(model, "context", {}) or {})
        R_cir = lawm.circumferential_lag(R_ax, radius, gap)
    else:
        R_cir = mm.circumferential_lag(R_ax, radius, gap)
    return R_ax, mm.cap_circumferential_lag(radius, gap, R_cir)[0]


def summarise(fit: LagFit) -> str:
    """One block of plain text for a GUI result strip or a log."""
    lines = [
        "R_ax = %.4f mm, R_cir = %.4f mm, ratio = %.4f" % (fit.R_ax, fit.R_cir,
                                                           fit.ratio),
        "lag amplitude A = %.4f  (= R_ax / %.4f mm, the law's unit-amplitude "
        "lag at eps = %.2f; Hrynevich's own A = 2.8629)"
        % (fit.A, fit.lag_ref_mm, fit.eps),
        "RMS %.2f um over %d angle(s)" % (fit.rms_um, fit.n_fit),
    ]
    lines += [" - " + n for n in fit.notes]
    return "\n".join(lines)
