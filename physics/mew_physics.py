"""Reduced-order physics for the melt-electrowriting (MEW) jet.

Given the four process knobs (pressure, voltage, speed, temperature) and the
machine geometry, what jet does the head produce? The answer is a plain
`JetState`. Pure module: no VTK, no file I/O, no GUI, no meshes, and no knowledge
of its consumers (`gcode_player`, `mew_deposition`, `mew_live`).

Grounded relations are physical; unknown dependences are fittable hooks whose
exponents default to zero. `Calibration` is the single seam where measured data
enters, and every provenance note lives beside the constant it explains.

The five laws
1. Flow -- Hagen-Poiseuille with an Arrhenius melt viscosity:
       Qv = pi r^4 dP / (8 eta(T) L),  eta(T) = eta_ref exp(Ea/R (1/T - 1/T_ref))
   Only the lumped group K = pi r^4/(8 eta_ref L) is identifiable, so the
   magnitude is ONE anchor and the response shape is predicted. The pressure half
   is validated (d ~ sqrt(dP), R^2 = 0.990, [S16]); the temperature half rides
   entirely on Ea. See `flow_rate` and `viscosity`.
2. Diameter -- mass conservation, the central MEW relation ([S1] Eq. 1):
       Qv = (pi/4) d^2 v  =>  d = sqrt(4 Qv/(pi v))              (d ~ v^-0.5)
   It contains no voltage, which is why voltage ranks least influential on
   diameter ([S2], p = 0.37). See `fiber_diameter`.
3. CTS -- the jet's landing speed, and the least-predictive quantity here: a
   MEASURED per-session anchor with power-law hooks that all default to 0.
   Predicting it is equivalent to predicting the touchdown diameter, so no closed
   form exists (JetCal 2026, doi:10.1007/s00170-026-17659-9). See `critical_speed`.
4. Jet lag -- the landing point trails the nozzle by lag_distance = H*tan(theta):
       theta(eps) = theta_cts + arctan(A (1 - eps^-b)),   eps = v/CTS >= 1
   A and b are Hrynevich 2020 Eq.4's own coefficients with the gap divided out, so
   nothing is fitted on our side. See `lag_angle` and the Calibration lag_* block
   for the per-rig amplitude k and the gap scaling.
5. Cone-jet profile -- the cone is the thick TOP of the jet, r(s) =
   sqrt(Q/(pi v(s))). RENDER-ONLY: pinned at both ends, so diameter, CTS, lag and
   the .depo are invariant to it. See `jet_speed_at`.

Units: length mm | speed mm/s | pressure Pa | viscosity Pa.s | temperature degC
| voltage kV | flow mm^3/s | angles returned in degrees. Sources, with DOIs and
the full ledger: sources.txt -- [S1] Ashour & Xu 2022, [S2] Xie et al. 2023, [S3]
Hrynevich et al. 2020, the lag anchor for magnitude and shape and the ray rule.
"""

from __future__ import annotations

import math
import dataclasses
from dataclasses import dataclass, field
from enum import Enum

from mew_field import AnalyticField, PointCharge

# Physical constants
GAS_CONSTANT = 8.314462618          # J/(mol*K)
CELSIUS_TO_KELVIN = 273.15
MM3_PER_M3 = 1.0e9                  # m^3 -> mm^3


# --------------------------------------------------------------------------- #
# Static configuration -- material, geometry, and the calibration seam
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Material:
    """Melt properties, defaults illustrative for PCL (Mn ~45 kDa): they set the
    FORM correctly and are meant to be calibrated. Viscosity is an interpretable
    Arrhenius form (reference value, reference temperature, activation energy)
    rather than a bare pre-exponential, so every number means something."""
    density: float = 1.09e-3           # g/mm^3 (amorphous PCL melt). Used ONLY by the
    #                                    sag self-weight: Qv is volumetric, so density
    #                                    never enters the flow law.
    viscosity_ref: float = 1040.6      # Pa.s at temperature_ref. The magnitude anchor:
    #                                    with the 21 G bore and the measured 25.4 mm
    #                                    needle it reproduces the real MEWron run (3 psi,
    #                                    100 degC, 7.5 mm/s -> ~15 um,
    #                                    test_matches_mewron_datapoint) AND is a physical
    #                                    high-Mn PCL melt viscosity (~1e3 Pa.s at 100 degC,
    #                                    [S9]). Only pi*r^4/(8*eta_ref*L) is identifiable,
    #                                    so RE-ANCHOR whenever r or L changes.
    temperature_ref: float = 100.0     # degC -- the Arrhenius PIVOT, set to the [D1] anchor
    #                                    run temperature so viscosity_ref IS eta there and
    #                                    the Ea fit is orthogonal to the magnitude anchor.
    activation_energy: float = 31.3e3  # J/mol -- MEASURED neat-PCL flow activation energy
    #                                    (Kelly et al. 2013, Eur. Polym. J. 49(2):464-470,
    #                                    DOI 10.1016/j.eurpolymj.2012.11.021, [S23]; neat
    #                                    PCL, 80-120 degC, parallel-plate rheometry with
    #                                    time-temperature superposition); the LOW
    #                                    end of a primary-read 31-41 kJ/mol band ([S49]
    #                                    32.5-40.8, [S50] 35/38, [S51] 40.0). Ea is the SOLE
    #                                    driver of the model's temperature sensitivity and is
    #                                    grade/Mn-dependent, but thermal degradation moves
    #                                    eta_ref and NOT this slope, so Ea stays fixed as
    #                                    viscosity_ref drifts. FIT ME by MEASURING it on a
    #                                    T-sweep (ln d vs 1/T), never by model inversion.


@dataclass(frozen=True)
class Nozzle:
    """Needle capillary geometry -- MEASURED, not calibrated (the r and L of
    Hagen-Poiseuille). Default bore = 21 G, the MEWron needle."""
    inner_radius: float = 0.257        # mm -- 21 G regular-wall inner radius (ID 0.514 mm).
    #                                    MEASURE yours: the bore enters Qv as r^4, and a
    #                                    thin-wall 21 G (~0.30 mm radius) flows ~1.9x more.
    #                                    Also sets the drawn cone base = bore.
    length: float = 25.4               # mm -- length of the NARROW BORE: the metal cannula
    #                                    from the luer-hub transition to the tip. NOT the
    #                                    syringe barrel (negligible resistance) and NOT just
    #                                    the stick-out below the clamp -- the melt flows the
    #                                    whole cannula. Qv ~ 1/L. L trades off against
    #                                    eta_ref at fixed anchor, so no PREDICTION depends
    #                                    on it; = 1 inch, the MEWron G21 needle, measured.


@dataclass(frozen=True)
class Calibration:
    """Every tunable that a real-data fit would touch, in one place. Changing
    these is how the model is corrected against measured prints -- no equation
    in this file should need editing to calibrate.

    CTS = cts_ref * (V/cts_voltage_ref)^cts_voltage_exp * (gap/cts_gap_ref)^cts_gap_exp
          * (Q/cts_flow_ref)^cts_flow_exp * (eta/cts_visc_ref)^cts_visc_exp
    All exponents default to 0, so CTS = cts_ref until a fit says otherwise. A
    nonzero flow / viscosity exponent REQUIRES its reference value (cts_flow_ref /
    cts_visc_ref), measured at the same anchor run as cts_ref."""
    # -- CTS: a measured anchor plus fittable dependence (exponents 0 = unknown) #
    # cts_ref is the COILING ONSET -- the LOWEST straight-fiber speed -- and a
    # MEASURED per-session anchor, never a prediction (see `critical_speed`).
    # DETERMINE IT by an ONSET SWEEP: at fixed P/V/T/gap step the speed DOWN until
    # the fiber first meanders, and take the lowest still-straight speed.
    # The default 7.5 is from the [D1] MEWron run (5 kV, 100 degC, straight at BOTH
    # 4.0 and 4.5 mm gaps -- hence cts_gap_exp = 0). It is an UPPER BOUND: the onset
    # below was never probed. It is also NOT externally corroborated -- published PCL
    # CTS is strongly condition-specific (Ashour & Xu 1.3 mm/s at a different
    # operating point) -- and DRIFTS per session with melt degradation and ambient T.
    cts_ref: float = 7.5               # measured CTS, mm/s, at the reference conditions below
    cts_voltage_ref: float = 5.0       # kV  -- reference conditions of that measurement
    cts_gap_ref: float = 4.0           # mm
    cts_voltage_exp: float = 0.0       # FIT ME (0): SIGN known +ve (stronger draw->faster jet->higher CTS), magnitude unquantified, anchor single-voltage
    cts_gap_exp: float = 0.0           # FIT ME (0): field V/H (down) vs flight length (up) cancel -> no effect over 4.0-4.5 mm; untested beyond
    # PRESSURE enters CTS only through flow rate Q; TEMPERATURE through Q and eta.
    # Each ref is the value AT the anchor run and is REQUIRED once its exponent != 0.
    cts_flow_ref: float | None = None  # mm^3/s, Q at the reference run (set when fitting flow)
    cts_flow_exp: float = 0.0          # FIT ME (0): landing speed is electrostatic, flow sets DIAMETER not CTS -- the 4Q/pi d_CTS^2 identity bounds c in [0,1]
    cts_visc_ref: float | None = None  # Pa.s, eta at the reference run (set when fitting temperature)
    cts_visc_exp: float = 0.0          # FIT ME (0): T enters only weakly via eta & Q -- the model's known weak link (no quantified CTS-vs-T law)
    # -- jet-lag angle vs v/CTS -- Hrynevich 2020 Eq.4 with the GAP factored   #
    #    out (search tags: LAG-POWER-FORM, LAG-REANCHOR-HRYNEVICH) ----------- #
    # theta is the CHORD angle of the nozzle->touchdown lag (lag_distance =
    # H*tan(theta)), zero at CTS and rising as a POWER LAW toward a finite ceiling:
    #
    #     theta(eps) = theta_cts + arctan( A * (1 - eps^-b) ),   A = tan(ceiling)
    #     lag        = H * tan(theta)  =  H * A * (1 - eps^-b)   when theta_cts = 0
    #
    # A and b are NOT fitted here: they are Hrynevich et al. 2020's own Eq.4 --
    # L_stable = 10.02*(1 - SR^-0.304) mm, n=7, adjusted R^2 = 0.9811, measured over
    # SR 1.125-17 AT GAP 3.5 mm -- with the gap divided out, A = 10.02/3.5 = 2.8629,
    # so the only per-rig lag input is CTS.

    # The ceiling is RAISED, not removed: A*(1 - eps^-b) -> A, so the lag saturates
    # at A*H = 2.863*H instead of diverging, and the approach is slow (at 20*CTS the
    # curve is only 60% of the way there), which is why
    # test_lag_saturates_not_diverges_at_extreme_speed bounds on A*H rather than on a
    # plateau.

    # THE FIELDS WERE RENAMED, NOT REVALUED, AND THAT WAS THE POINT. An older
    # profile carries lag_saturation_k and lag_angle_max_deg = 48.94;
    # models_from_profile ignores unknown keys, so those fall back to the defaults
    # below. Had lag_angle_max_deg kept its name that stale value would have been
    # applied as A = tan(48.94 deg) = 1.148, silently reinstating the old cap.

    # theta_cts is PINNED to 0, not fitted: the lag->0 boundary at CTS that the data
    # demands and that Hrynevich's own algorithm uses. It sits below his lowest
    # measured speed (1.125*CTS), so it is UNIDENTIFIABLE, not measured as zero. A
    # floor must not move the CEILING, and does not: the amplitude is
    # (A - tan(theta_cts)), composed in lag space (see lag_angle,
    # test_lag_floor_does_not_move_the_ceiling). Ashour & Xu's ~4 deg is the jet's
    # local TANGENT angle, a different quantity from our net CHORD angle. Reset
    # theta_cts = arctan(lag_CTS/H) once a rig sweep resolves a CTS lag above the
    # ~0.16 mm near-CTS noise.

    # GAP-SCALING IS FIRST-ORDER: lag ~ H^1 with theta(v/CTS) gap-INDEPENDENT. A
    # 3-way digitization of Hrynevich Fig.3E found the dimensionless SHAPE
    # gap-invariant with a UNIFORM ~8-10% magnitude excess above H-proportional, and
    # the two literature levers disagree about the SIGN of any gap exponent
    # (Hrynevich's own two gaps imply n = 1.339, Hrynevich-vs-Neuhaus n = 0.81-0.91,
    # a joint fit against Neuhaus Fig 3C lands NEAR 1). QUOTE "NEAR 1", NEVER A THIRD
    # DECIMAL -- four points off a published figure cannot separate 0.99 from 1.00.
    # So no gap term is added. The default working_distance is 4.0 mm, so against a
    # 4.5 mm print the lag runs ~11% low until H is set (a flat scale; CTS is
    # unchanged, the gap->CTS hook being off). See MANDREL_LAG_DERIVATION.md,
    # sources.txt [S3], and test_lag_distance_matches_hrynevich_full_range.
    lag_angle_cts_deg: float = 0.0     # lag CHORD-angle floor at CTS, deg; PINNED to 0 (the boundary lag->0 at CTS), NOT fitted -- see comment above. was 0.84/3.3/4.0
    # A -- Eq.4's gap-normalised amplitude, dimensionless, and THE ONE LAG
    # CONSTANT A RIG IS EXPECTED TO MOVE. `lag = H * A * (1 - eps^-b)`, so A is
    # the saturation value of lag/H and tan(theta_ceiling) is the same number.
    #
    # STORED AS A, NOT AS AN ANGLE. It was `lag_ceiling_angle_deg = 70.745620335`,
    # whose tangent is A -- a lossy round trip: that angle tangents to
    # 2.8628571428152698 against the published 10.02/3.5 = 2.8628571428571425, and
    # the digits had to be carried to nine decimals to keep even that. Here the
    # ratio IS the field, exactly as Hrynevich publishes it.
    #
    # THERE IS NO SEPARATE PER-RIG MULTIPLIER. A per-rig scale k multiplying this
    # whole law would be the same single degree of freedom written twice, and
    # fitting both to one dataset is meaningless. Fitting the amplitude means
    # fitting THIS. The default is Hrynevich's own
    # value: a PRIOR, not a validated transfer, because the only cross-rig test of
    # the flat law is Neuhaus 2025's mandrel jet-lag ellipse, where these constants
    # land on her circumferential radius and miss her axial one by a rig factor no
    # published lever supplies -- the FORM transfers and the AMPLITUDE does not
    # (MANDREL_LAG_DERIVATION.md 11.1 and 12.9).
    lag_amplitude: float = 10.02 / 3.5   # A, dimensionless = 2.8628571428571425. Hrynevich 2020 Eq.4's own amplitude with the gap divided out. Re-fit it from a rig's own circles (mew_circle_fit) or mandrel (mew_lag_fit); b is PINNED while you do
    lag_saturation_exp: float = 0.304  # b, the power-law exponent of the approach to that ceiling. Hrynevich Eq.4's own exponent, unchanged
    # The gap A was FITTED AT, mm. A RECORD, not a law input: nothing reads it and
    # the lag is unchanged by it. It exists because a fitted A has no meaning
    # without it -- the law carries the gap as H^1 and the two literature levers
    # disagree on that exponent, so calibrating at the operating gap absorbs the
    # disagreement AT that gap and establishes nothing about any other. Without
    # this field a profile silently applies its A at any --standoff, an invisible
    # extrapolation. 3.5 is where the shipped A is a DEFINITION rather than a
    # measurement (A and b were fitted to Hrynevich's curve there). Distinct from
    # the working distance (`sim_standoff`), which is the gap of the print about to
    # run; this is a property of the CALIBRATION. `mew_circle_fit` writes it.
    lag_amplitude_gap: float = 3.5    # mm

    @property
    def lag_ceiling_angle_deg(self) -> float:
        """The asymptotic chord ANGLE, derived: atan(A). READ-ONLY, and not a
        field -- it was one, and holding both it and a multiplier `k` was one
        degree of freedom written twice.

        It stays expressible because the paper states the law both ways
        (tan(theta) = A(1 - eps^-b)) and because the paper repo's tracker
        comparator reads it off a live `Calibration`. Nothing in this repo
        computes the lag from it; `lag_angle` and `lag_ray_constants` both take
        `lag_amplitude` directly."""
        return math.degrees(math.atan(self.lag_amplitude))
    stable_window: float = 2.2         # upper edge of the usable window, in units of CTS
    #   = the top of Ashour & Xu 2022's IMAGED range -- a usability bound, NOT a
    #   measured fiber-breakup boundary. Labels the regime only; never changes
    #   geometry. Widen once a breakup/quality-vs-SR sweep exists.
    # -- electrostatic field (mew_field seam) ------------------------------- #
    # Tip field-enhancement strength (kV*mm) for the image-charge source at the
    # needle tip. 0 = none -> the field is the bare uniform gap field, asserting
    # only V/H. A FIT-ME hook in the same spirit as the CTS exponents above:
    # turn it on when tip-geometry data (or a Phase-4 perturbation) justifies it.
    field_tip_strength: float = 0.0
    # -- contact dynamics: damping of the second-order whip ------------------ #
    # The fiber touchdown point tracks the moving nozzle as a damped oscillator
    # (see integrate_contact_path_2nd). The INSIDE corner rounding every print shows
    # is the LAG, present for ANY zeta; zeta governs only an ADDITIONAL outward whip,
    # which no MEW print has ever shown -- hence the critically-damped default. The
    # steady lag is independent of zeta by construction, so this never moves the
    # calibrated lean.
    contact_damping_zeta: float = 1.0
    # -- below-CTS coiling: the fluid-mechanical sewing machine -------------- #
    # Active only when v < CTS (Regime.COILING). The jet delivers fiber length at
    # rate ~CTS while the collector takes up only v, so the surplus CTS/v cannot lay
    # straight and buckles into meanders -> loops -> coils (Chiu-Webster & Lister
    # 2006; Morris et al. 2008; Brun et al. 2015). The deposited centreline is
    # reshaped to absorb that excess while CONSERVING arc length, so the buckling is
    # grounded rather than cosmetic. The ONE new scale is the coil radius R; the
    # clock follows from the geometric identity CTS = R*Omega_c. R does NOT transfer
    # from gravitational rope-coiling theory -- the MEW jet is electrohydrodynamically
    # drawn, not gravity-driven -- so it is a MEASURED anchor, FIT ME like cts_ref.
    coil_radius_ref: float = 0.07      # R, mm = half the 139 um peak-to-peak (139 +/- 17 um, wavelength 366 +/- 39 um) Hochleitner et al. 2018 (Acta Biomater. 72:110-120, [S31]) measured at 68% CTS -- on the PCLAC ELASTOMER, not neat PCL. FIT ME
    coil_freq_eps_exp: float = 0.0     # FIT ME: f_coil ~ (v/CTS)^exp (0 = constant clock)
    # -- bridge sag: a REDUCED-ORDER, RIG-CALIBRATED droop for suspended spans (OPT-IN) ---
    # Replaces gcode_to_fibers' geometric heuristic (depth = SAG_COEFF*span^2/d, whose
    # 1/d made THIN fibers sag more). The framing is settled -- do NOT re-muddle it:
    #   (A) CONTACT GEOMETRY (rest-on-top stacking, crossover heights) is exact,
    #       resolution-independent and PREDICTIVE, and this droop law does not touch it.
    #   (B) the SUSPENDED-SPAN DROOP depth (this law) is a reduced-order, rig-calibrated
    #       renderer, NOT a validated first-principles mechanism.
    # The real droop mechanism is VISCO-ELASTIC ([S41] Nguyen, Kim & Jeong 2019) and
    # electrostatically coupled to the collector -- both out of scope. The
    # static-tension MAGNITUDE claim is RETRACTED; the magnitude stays a calibration.

    #     depth = sag_coeff * mult * span^sag_span_exp
    #             * (d / sag_diam_ref)^sag_diam_exp          # = 1 at the default exp 0
    #             * (eta(T)/eta(T_ref))^sag_thermal_visc_exp
    # At the default sag_diam_exp = 0 this is paper Eq (7) depth = c*span^2 with
    # c = sag_coeff (mm^-1) and sag_diam_ref inert. Installed opt-in (--physical-sag);
    # off -> the geometric law, byte-identical. Per-exponent grounding is on the fields
    # below and in bridge_sag_depth; the deliberation is the PHYSICS_DEPENDENCY_GRAPH.md
    # sag dossier.
    sag_coeff: float = 0.341588        # mm^-1, magnitude c in paper Eq (7) depth=c*span^2: ONE rig calibration (a single-condition visual anchor set ~0.4x the geometric SAG_COEFF for flatter floor descents), NOT fitted to sag data; FIT ME. = old 0.004 / sag_diam_ref 0.01171 = 0.3415884, folded when d_ref left the default form; 6 sig-figs so it round-trips the profile store. `mult` (GUI Sag multiplier) scales it
    sag_span_exp: float = 2.0          # viscous-catenary EXPONENT: developed sag (>> d) is stretching-dominated -> span^2 ([S44]/[S45]; shape [S32]/[S33]; visco-elastic regime [S41]). span^4 is only the sub-d bending transient. Grounds the exponent, not the magnitude
    sag_diam_exp: float = 0.0          # DEFAULT 0: assert no diameter dependence (unknown -> 0). Unidentifiable on uniform prints, observable only on mixed-d; the sign is regime-dependent and UNMEASURED, with the physics estimate near -1..0. FIT ME (SAG_EXPONENT_EXPERIMENT.md); +2/-2 stay selectable
    sag_diam_ref: float = 0.01171      # mm, anchor diameter = the calibration preset's fiber. Pivot for the diameter hook (d/d_ref)^q ONLY; genuinely INERT at the default sag_diam_exp=0 (folded into sag_coeff). Also used by the dormant creep law below
    sag_thermal_visc_exp: float = 0.0  # fittable HOOK, default OFF (assert no T-dependence). Nguyen: T dominates, sign <0 (hotter sags more); magnitude unvalidated -> FIT ME
    sag_temp_ref: float | None = None  # degC, temperature at the sag anchor run (None -> Material.temperature_ref)
    # -- bridge sag, VISCOUS-CREEP mode (DORMANT future-work; default OFF; not GUI-exposed) -
    # The grounded reconciliation with [S41]: a just-laid fiber creeps visco-elastically
    # under self-weight, resisted by eta(T), over a soft-window time tau, so
    #   depth = C_geom * (rho*g*tau / eta(T)) * span^p / d^|q|   (viscous beam: p=4, q=2)
    # Two wins over the static form: TEMPERATURE is PREDICTED via the Arrhenius eta(T)
    # already in the model (depth ~ 1/eta, a physical power and not a fitted hook), and
    # the magnitude constant becomes a physical creep time rather than a fake tension
    # (back-solving the static law's sag_coeff gives an absurd ~0.4 nN).

    #   depth = sag_creep_coeff * mult * (eta(T_ref)/eta(T))
    #           * (span / sag_creep_span_ref) ** sag_creep_span_exp
    #           * (diameter / sag_diam_ref)  ** sag_creep_diam_exp
    # DORMANT: reachable only via the Calibration seam or the CLI (mew_export /
    # gcode_player --physical-sag-viscous), and OFF is byte-identical to
    # bridge_sag_depth. It is the FUTURE-WORK starting point for grounding sag once a
    # rig sag-vs-(span, d, T) sweep decides span 2-vs-4, fixes the diameter sign and
    # pins tau. VALIDATE before ever making it a default.
    sag_viscous_creep: bool = False    # DORMANT future-work toggle; False -> reduced-order parabola (default, byte-identical); True -> viscous-creep law (temperature predicted via eta(T))
    sag_creep_coeff: float = 0.08543   # mm, magnitude anchor: depth at (sag_creep_span_ref, sag_diam_ref, T_ref); encodes the soft-window creep time tau. Default = the static law's depth there (continuity); FIT ME to a rig sag sweep
    sag_creep_span_ref: float = 0.5    # mm, span anchor (a typical pore) for the creep-law magnitude
    sag_creep_span_exp: float = 4.0    # viscous-BENDING self-weight span law (s^4); FIT ME (string/tension regime -> ~2)
    sag_creep_diam_exp: float = -2.0   # viscous-BENDING diameter law (1/d^2: thicker = stiffer at fixed T, but eta(T) dominates across T); FIT ME (string regime -> ~0)
    # -- junction fusion: crossover-localised inter-layer compaction (OPT-IN) --
    # A freshly-laid MEW fiber is deposited molten; where it crosses OVER the
    # structure below it lands in loaded contact and partially COALESCES into the
    # fiber beneath (viscous neck growth) before it freezes, so its resting CENTRE
    # sits LESS than a full diameter above the one below and stacked crossover peaks
    # are shorter than the geometric N*d. LOCALISED to crossovers by PHYSICS, not by
    # fiat: fusion needs fiber-fiber contact, and a suspended span bridges the pore
    # with nothing beneath it, so the sag solver sinks ONLY where a fiber rests on
    # another (gcode_to_fibers' resolve_supports + the rest-on-top clamp).

    # Reduced-order form -- the magnitude is EMPIRICAL (one calibrated overlap; the
    # rigorous coalescence law needs a melt contact-time model), so this is graded one
    # rung below the catenary sag law:
    #   sink delta = fusion_overlap * d
    #               * (d / fusion_diam_ref) ** fusion_diam_exp
    #               * (eta(T)/eta(T_ref)) ** fusion_thermal_visc_exp     (clamped)
    # delta is how far the upper fiber's CENTRE drops below rest-on-top; the column
    # height after N aligned layers is then ~ d + (N-1)*(d - delta), so the peak ratio
    # to geometric -> (1 - fusion_overlap) for large N (phi = 0.35 -> ~0.65x).
    # fusion_overlap = 0 installs NOTHING -> the geometric stack, byte-identical.

    # A cooling-time hook on phi was TRIED AND REVERTED: layer DURATION is the wrong
    # proxy for Lamb's variable, which is exposure UNDER THE PRINT HEAD. Our own
    # fusion-showing scaffold (the 30x30mm 0.5mm-pitch grid @600mm/min) sits in the
    # same long-layer-time regime as Lamb's anchor yet fuses fully, and the hook drove
    # phi_eff -> ~0 against the print. A correctly-scoped cooling model would track
    # dwell AWAY from the head and needs a rig deposition-delay sweep.
    fusion_overlap: float = 0.35       # phi: per-junction centre sink as a fraction of d; 0 = no fusion (geometric). An EYEBALL fit to our own prints on an UNHEATED bed -- collector temperature is a fixed boundary condition on this rig, not a model variable, which is why there is no collector-temp term. It is ~4x Lamb 2024's unheated 0.92x N*d point (at his 26 degC), which is a different scaffold on a different rig and which Lamb says cannot be directly extrapolated. Do NOT carry phi to a heated collector or another rig; FIT ME to a height-vs-N sweep
    fusion_diam_exp: float = 0.0       # delta ~ d^(1+exp); 0 = the sink scales with d ALONE, i.e. phi is a pure fraction of d (diameter-INDEPENDENT height compaction). Lamb 2024 supports 0 for HEIGHT but measures it as a TEMPERATURE response on a heated collector, which we do not hold -- so extending it to our unheated baseline is an assumption, not a measurement. (Lamb's "+5um -> +70%" is bond STRENGTH, a mechanical metric this geometric model does not represent; do not use it to justify >0 here.)
    fusion_diam_ref: float = 0.01171   # mm, anchor diameter (the calibration preset's fiber); inert while fusion_diam_exp == 0
    fusion_thermal_visc_exp: float = 0.0  # FIT ME: phi ~ (eta(T)/eta(T_ref))^exp (0 = no temp effect; <0 = hotter/less-viscous fuses more). NOTE: this is the MELT/nozzle temperature (via melt viscosity), NOT the COLLECTOR temperature -- the MEWron's unheated bed holds collector temp fixed, so it is out of scope by hardware
    fusion_temp_ref: float | None = None  # degC at the fusion anchor run (None -> Material.temperature_ref)
    fusion_max_overlap: float = 0.9    # hard clamp on phi-effective so the upper centre never reaches the lower fiber's centre (full merge); keeps tubes overlapping, not coincident
    # -- wall (straight-span) fusion: the SAME coalescence at a COLLINEAR contact -- #
    # Two fibers CROSSING at angle theta touch over an overlap length ~ d/sin(theta):
    # a square crossing is a POINT contact, while a collinear stack -- the classic MEW
    # wall, and equally layer N+2 drooping onto the parallel layer N in a 0/90 grid --
    # is a LINE contact running the whole span. Same physics, very different contact
    # length and shared thermal mass, so the calibrated overlap need not be the same
    # number. This matters for the EXISTING calibration too: fit_fusion_overlap backs
    # phi out of a measured N-layer WALL height (a pure collinear stack) and that phi
    # is then applied at CROSSINGS. None -> wall contacts reuse fusion_overlap, which
    # is byte-identical to the pre-split behaviour and why it is the default.
    fusion_wall_overlap: float | None = None   # phi_wall: per-contact centre sink as a fraction of d at a COLLINEAR (wall / straight-span) contact. None -> use fusion_overlap (no split; byte-identical). FIT ME to a wall height-vs-N sweep (fit_fusion_overlap measures exactly this)
    # The wall<->junction transition is a BLEND, not a switch, and it has to be:
    # sin(theta) is continuous along a curved fiber, so a hard threshold would flip
    # classification mid-fiber and put a (phi_j - phi_w)*d STEP in a continuous
    # centreline. Measured contact-angle populations: collinear
    # contacts sit in a razor spike at 0-2 deg, genuine junctions at the rotation
    # series' crossing angle (36/45/60/90 deg for every standard pore shape), with a
    # few percent of 2-20 deg grazes where a turnaround loop arcs over a straight
    # fiber. The default band spans that empty gap. Not a fitted law -- a
    # classification band -- so it is a seam constant, not a GUI knob.
    fusion_wall_angle_lo: float = 5.0   # deg; crossing angle at/below which a contact is fully WALL (collinear line contact)
    fusion_wall_angle_hi: float = 25.0  # deg; crossing angle at/above which a contact is fully JUNCTION (point contact). Sits below the 36 deg smallest standard pore-shape crossing, above the loop-graze band
    # -- Taylor cone / cone-jet profile (the molten transition at the nozzle) -- #
    # -- CONE-KNEE-ASHOUR: why a knee fraction, not a fixed exponent ---------------- #
    # The cone is the thick TOP of the jet: r(s) = sqrt(Q/(pi v(s))), so the melt is
    # widest where it is slowest and necks smoothly to the fiber. The one shape
    # unknown is how fast the jet accelerates, i.e. HOW TALL the cone is. A fixed
    # exponent makes that height collapse as the base/fiber ratio rho grows, so a
    # value fit to Ashour's rho ~ 5.4 leaves a stubby nub on a 40:1 rig. Instead the
    # cone KNEE (where the jet has necked to half the base width, v = 4*v_exit) is
    # pinned to a fixed fraction of the drop and the exponent is DERIVED per jet from
    # rho (_cone_exp), so the cone is the same height fraction on any rig --
    # test_cone_knee_height_is_ratio_independent asserts exactly that.

    # STILL RENDER-ONLY: at dropfrac 0/1 the profile is pinned to v_exit / v_land
    # whatever this knob is, so touchdown, fiber diameter, CTS, lag and the .depo are
    # invariant BY CONSTRUCTION; the only consumer is gcode_player's jet-tube radius.
    # NOT MODELLED, though real and visible in Ashour's Fig.2A: the cone base widens
    # with translation speed, and a below-1.6xCTS necking bulge near touchdown that a
    # monotonic profile cannot produce. dropfrac follows the electric POTENTIAL along
    # the jet (mew_field), so the cone is field-coupled.
    # Provenance: Taylor 1964 and Yarin et al. 2001 give the static 49.3 deg cone
    # against the slenderer ~33.5 deg active one this profile reproduces; Mieszczanek
    # et al. 2021, Adv. Mater. 33:2100519 finds cone size has no closed-form predictor.
    cone_knee_frac: float = 0.12        # cone half-base height as a fraction of the gap.
    #                                     A cross-rig VISUAL compromise: Ashour's own
    #                                     low-ratio rig reads ~0.20 by eye off a
    #                                     published figure (n=1, one operating point),
    #                                     but 0.20 on a high-ratio rig draws a horn that
    #                                     dwarfs the jet. Re-tune for a rig-specific cone.

    # -- mandrel placement deviation (mew_mandrel, Neuhaus 2025 Eqs 1-5) ----- #
    # On a CYLINDER the jet lands off the programmed path and keeps deflecting until
    # the suspended and deposited fiber are collinear again viewed along the surface
    # normal. It has no flat-collector analogue and does nothing on a flat print.
    # OFF BY DEFAULT, and the off case is EXACT: the two lag radii below are the jet
    # lag at axial and circumferential winding angles, and ZERO LAG GIVES ZERO
    # DEVIATION identically (a control the Fig 3 fixture asserts). They are None
    # because MEWracle does not yet DERIVE them -- our own lag law yields one number,
    # while the measured locus is an ELLIPSE whose two radii differ because the
    # collector curves away circumferentially but not axially. Deriving them is
    # not done yet; until then they are measured per rig.

    # TWO SEMANTICS, ONE PAIR OF FIELDS -- know which caller you are in.
    #   `mew_deposition` (the deposition path, and what a print uses) reads their
    #   RATIO, in place of the geometric r/(r+H). The lag itself comes from the
    #   jet model per speed. With `mandrel_lag_speed` ALSO set the
    #   pair is ABSOLUTE at that speed and a rotary lay runs on a MANDREL-ONLY
    #   amplitude R_ax / L(eps_cal), so R_ax(eps) = R_ax,cal * L(eps)/L(eps_cal)
    #   (`mew_lag_fit.mandrel_calibration`); without it the amplitude is
    #   `lag_amplitude` (A), as before. A FLAT lay never reads any of this: a
    #   mandrel calibration must not move a flat print.
    #   `mew_mandrel.deviation_from_calibration` (the standalone Neuhaus evaluator,
    #   tests only) reads them as ABSOLUTE radii, which is what makes it a faithful
    #   port of Eqs 1-5. Its Fig 3 starting values live there.
    # So: supply the pair you MEASURED at your calibration speed and gap, and get the
    # amplitude from A = (measured R_ax) / (H * (1 - eps^-b)) at that speed and gap --
    # the micrograph inversion of MANDREL_LAG_DERIVATION.md I.1-2c, which `mew_lag_fit`
    # performs. It reports A on the same scale as Hrynevich's 2.8629, so a mandrel fit
    # and a circle fit are directly comparable numbers.
    mandrel_lag_axial: float | None = None    # R_ax, mm, MEASURED at the calibration condition. Deposition uses only R_cir/R_ax; unset -> the geometric r/(r+H)
    mandrel_lag_circ: float | None = None     # R_cir, mm, ditto -- the pair is read as a ratio. Always < R_ax on a cylinder
    mandrel_lag_speed: float | None = None    # mm/s, the SURFACE speed the pair was measured at. Set -> the pair is absolute there and sets a mandrel-only amplitude; None -> the pair is a ratio only
    mandrel_cts: float | None = None          # mm/s, the CTS measured ON THE MANDREL. A rotary lay uses it in place of cts_ref; None -> cts_ref. Never read by a flat lay
    # S, the support-vector length: a virtual point below the nozzle that models the
    # jet's curvature. Neuhaus calls it the model's weak point and FITS it, but
    # S = r + 0.75*gap - 0.002*v reproduces all four Fig 3 panels, so it may be
    # geometry plus one speed coefficient rather than a free parameter (one rig is
    # not two). So the GEOMETRY is the default and the speed slope defaults to 0.
    # TRAP: S is measured from the mandrel AXIS, so it MUST track the mandrel radius
    # at fixed gap. Holding it flat gives 251 um at r = 2 mm against a measured ~136
    # -- an 85% error that GROWS as the mandrel shrinks.
    mandrel_support_mm: float | None = None   # S, mm, measured from the mandrel AXIS. None -> r + frac*gap + speed_coeff*v (the geometric form)
    mandrel_support_frac: float = 0.75        # the "3/4 of the way down the gap" fraction in S. Neuhaus's value; FIT ME
    mandrel_support_speed_coeff: float = 0.0  # FIT ME (0 = hold S constant): dS/dv, mm per mm/s. Neuhaus's best-agreement variant is -0.002; a constant S over-predicts ~27% at 100 mm/s but <2% below 25 mm/s
    # WHERE LAYER i REGISTERS ON THE WALL BENEATH IT -- a registration hook, not a
    # physics switch, and it is OFF because nothing measures it. Eq 1-5 is a
    # SINGLE-LAYER model, so re-evaluating it on each grown layer (r -> r + h) is a
    # composition rule, and that composition is unmeasured on ANY mandrel: all 43 of
    # Neuhaus's deviation prints are single layer, and registration does NOT erase a
    # systematic per-layer bias. So the DEFAULT freezes the whole placement at the
    # bare radius, once per scaffold -- exactly the 3-axis treatment. ON re-enables
    # the per-layer evaluation, as a WHOLE-BRANCH switch on purpose: a partial freeze
    # re-creates a lean of the opposite sign, the six per-layer terms opposing each
    # other (MANDREL_LAG_DERIVATION.md 12.1 treatment C, 12.8). What would settle it
    # is a cross-section or oblique SEM of a 15+ layer wall: per-layer predicts a
    # lean of ~1%/layer of the calibrated deviation, the freeze predicts radial.
    # NOT frozen by this, deliberately: the jet's own speed at the wall radius
    # (`_metric_path`), which leaves a residual per-layer drift ~400x smaller than
    # the frozen terms.
    mandrel_per_layer_registration: bool = False   # OFF = one placement per scaffold at the bare radius (§12.8). ON = re-evaluate on each grown layer

    # -- USER-REPLACED LAWS (mew_lawlang) ---------------------------------- #
    # `law_source` maps a law key from `mew_lawlang.LAWS` to the user's own
    # expression for it; `law_consts` maps the same key to that user's named
    # coefficients. Both EMPTY by default, and empty means the shipped method
    # runs with `mew_lawlang` never imported -- a user override is opt-in per
    # law, never a mode the whole build sits in.
    #
    # Deliberately NOT `CALIBRATION_FIELDS` entries. That registry is a flat
    # table of floats, `lite/lite_params.py` hand-mirrors it, and
    # the two must match key-for-key -- so a dict-valued FieldSpec would
    # force Lite's whole Physics
    # form along with it. These ride the profile JSON under their own two keys
    # instead (`mew_calibration.LAW_SOURCE_KEY` / `LAW_CONSTS_KEY`), which
    # `models_from_profile` handles explicitly.
    #
    # `field(default_factory=...)`, not `= {}`: a bare dict default is shared by
    # every Calibration ever built, so one profile's laws would leak into the
    # next. `dataclasses` refuses it outright, which is the only reason this is
    # not a latent bug instead of a comment.
    law_source: dict = field(default_factory=dict)
    law_consts: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Process knobs and the resulting jet state
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProcessParams:
    """The four knobs the user wants to predict from."""
    pressure: float       # applied melt pressure, Pa (gauge)
    voltage: float        # kV
    speed: float          # translation speed, mm/s
    temperature: float    # degC


class Regime(Enum):
    """Which deposition regime the speed lands in, relative to CTS."""
    COILING = "coiling"        # v < CTS: jet over-supplies, fiber buckles/coils
    STABLE = "stable"          # CTS <= v <= window: straight, direct-written
    OVERSTRETCH = "overstretch"  # v > window: a USABILITY label, NOT a breakup
    #   boundary -- the fiber is still straight laydown here (Hrynevich 2020 measures
    #   stable lag to 17x CTS). The label drives NO geometry; the lone geometric
    #   boundary is eps = 1, coiling vs straight.


@dataclass(frozen=True)
class JetState:
    """Everything the model predicts for one parameter set -- the steady
    per-condition SNAPSHOT. A pure compilation of the laws in `MEWModel.solve`.

    Trust map -- confidence is NOT uniform, so do not read the bundle as one
    uniformly-trustworthy prediction (full defense: PHYSICS_DEPENDENCY_GRAPH.md):
      * fiber_diameter  -- VALIDATED (mass conservation; d ~ v^-0.5, R^2 = 0.974).
      * flow_rate/viscosity -- pressure axis validated; temperature rides on Ea.
      * cone_base_radius/nozzle_exit_speed -- base pinned to the bore; v_exit ~ Q.
      * lag_*           -- externally anchored to Hrynevich 2020; rides on CTS.
      * speed_ratio/regime/straight_fibre -- eps and the eps=1 boundary grounded;
                           the regime/window are LABELS only; all inherit CTS.
      * critical_speed  -- the LEAST-predictive field: an upper-bound anchor, not
                           externally corroborated, drifting per session.

    `straight_fibre` is the trust GATE: the diameter and lag relations assume a
    pulled straight fiber, so below CTS those fields are EXTRAPOLATIONS. This
    snapshot is NOT the deposited geometry -- `mew_deposition` / `mew_live`
    re-evaluate the same laws per-point along the path."""
    flow_rate: float        # Qv, mm^3/s
    viscosity: float        # eta(T), Pa.s
    critical_speed: float   # CTS, mm/s
    speed_ratio: float      # v / CTS
    fiber_diameter: float   # d, mm  (valid for straight fiber, i.e. v >= CTS)
    lag_angle_deg: float    # jet-lag angle, degrees
    lag_distance: float     # horizontal trail of the landing point, mm
    lag_time: float         # print-time the landing point trails the head, s
    regime: Regime
    straight_fibre: bool    # True when the straight-fiber model applies (v >= CTS)
    cone_base_radius: float  # cone base = nozzle bore radius, mm; the base is PINNED here
    nozzle_exit_speed: float # melt speed at the bore exit, mm/s; the jet starts here


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #
#: The fraction above CTS that `MEWModel.min_print_speed` floors a slowed
#: five-axis feed at. See that method for why it is 10% and not 0.
CTS_FLOOR_MARGIN = 1.10


class MEWModel:
    """Reduced-order MEW jet model. Construct once from the static config, then
    call `solve(params)` per parameter set (cheap, allocation-light)."""

    def __init__(self, material: Material | None = None,
                 nozzle: Nozzle | None = None,
                 working_distance: float = 4.0,
                 calibration: Calibration | None = None) -> None:
        # `not (x > 0)` rather than `x <= 0`: NaN fails every comparison, so the
        # obvious guard waved a NaN gap (or anchor) through into every law.
        if not (math.isfinite(working_distance) and working_distance > 0.0):
            raise ValueError("working_distance must be positive (mm)")
        self.material = material or Material()
        self.nozzle = nozzle or Nozzle()
        # The three constants Hagen-Poiseuille divides by or takes a power of.
        # Zero raised ZeroDivisionError from inside `flow_rate`, a negative
        # viscosity a math domain error; say which constant instead.
        for what, val in (("viscosity_ref (Pa.s)", self.material.viscosity_ref),
                          ("nozzle inner_radius (mm)", self.nozzle.inner_radius),
                          ("nozzle length (mm)", self.nozzle.length)):
            if not (math.isfinite(val) and val > 0.0):
                raise ValueError("%s must be a positive number, got %r"
                                 % (what, val))
        self.working_distance = float(working_distance)   # gap H, mm
        self.calibration = calibration or Calibration()
        # The process values a USER-REPLACED law may name even though they are
        # not that law's own arguments -- voltage, pressure, temperature, gap.
        # They are constant within a print, which is the whole reason this can be
        # model state rather than another argument threaded through every call
        # site. `solve()` fills it; a worker that never calls `solve` sets it
        # from its own argv (see `set_context`). Empty is fine: a law that names
        # a value nobody supplied fails with a message saying so, rather than
        # silently reading a stale number.
        self.context = {}
        self._law_cache = {}      # law key -> (source text, compiled callable)
        self._law_env = None      # the constant half of the namespace, built once
        self._derived_env = {}    # derived quantity -> value at THIS context
        self._deriving = set()    # the cycle guard, see `_derive_into`

    # -- the user-replaced-law seam (mew_lawlang) --------------------------- #
    # Three small methods, and every law that can be overridden goes through
    # exactly this shape:
    #
    #     fn = self._law("sag")
    #     if fn is not None:
    #         return fn(self._law_scope("sag", span=span, d=d, ...))
    #     ...the shipped arithmetic, untouched...
    #
    # The shipped path is FIRST-CLASS, not a fallback: with no override the
    # method runs exactly the code it ran before, `mew_lawlang` is never
    # imported, and nothing allocates. That matters because `bridge_sag_depth`
    # is called once per span.
    def set_context(self, **values):
        """Record the process values a user law may name (voltage / pressure /
        temperature / gap). `solve()` calls this; a worker that never calls
        `solve` -- the exporter, the live re-lay -- sets it from its own argv so
        a law with a voltage term works there too. Unknown keys are kept: the
        scope check is `mew_lawlang.LAWS`' business, not this method's."""
        for k, v in values.items():
            if v is not None:
                self.context[k] = float(v)
        self._derived_env.clear()    # eta, Q, d and CTS all move with these
        return self

    def _law(self, key):
        """The compiled override for `key`, or None when the shipped law stands.

        Compiled once and cached against its own source text, so editing a law in
        the GUI recompiles and an unchanged one does not. A source that will not
        compile is NOT silently ignored -- `mew_lawlang.LawError` propagates, so
        a bad expression fails where it is used instead of quietly reverting to
        the shipped law and looking like the user's law did nothing."""
        src = (self.calibration.law_source or {}).get(key)
        if not src or not str(src).strip():
            return None
        hit = self._law_cache.get(key)
        if hit is not None and hit[0] == src:
            return hit[1]
        import mew_lawlang
        spec = mew_lawlang.LAWS.get(key)
        if spec is None:
            raise mew_lawlang.LawError("%r is not an editable law" % (key,))
        names = set(self._law_names())
        names |= {a for a, _d in spec.args}
        # EVERY quantity, not just this law's own arguments. A name the editor's
        # table lists and the compiler then refuses is worse than no table at
        # all, and the ones no law is handed are derived in `_law_scope`.
        names |= set(mew_lawlang.QUANTITIES)
        names |= set(mew_lawlang.CONTEXT_NAMES)
        # the user's OWN coefficients for this law -- the "add your own
        # constant" half of the feature, and they have to be in the compile-time
        # name set or the expression that uses one is refused as unknown
        names |= {str(n) for n in
                  ((self.calibration.law_consts or {}).get(key) or {})}
        fn = mew_lawlang.compile_law(src, names)
        self._law_cache[key] = (src, fn)
        return fn

    def _law_names(self):
        """Every constant name a law may MENTION -- wider than `_law_constants`
        on purpose.

        An optional reference (`cts_flow_ref`, `sag_temp_ref`,
        `fusion_wall_overlap`) is None until its hook is fitted, so it has no
        value to put in the environment. It must still be a legal NAME, or the
        shipped CTS law -- which guards its flow term with
        `... if cts_flow_exp != 0 else 1` -- would not even compile at the
        factory calibration. Compiling is about what the law may refer to;
        having a value is a question asked per evaluation, and `if/else` only
        evaluates the branch it takes, so a guarded term never reads the None.

        This split was a genuine finding, not a precaution:
        `test_law_lang.shipped_expressions_match_the_methods` refused the
        registry's own CTS string with "unknown name 'cts_flow_ref'"."""
        names = set()
        for obj in (self.material, self.nozzle, self.calibration):
            for fld in dataclasses.fields(obj):
                val = getattr(obj, fld.name)
                if isinstance(val, bool):
                    continue
                if val is None or isinstance(val, (int, float)):
                    names.add(fld.name)
        names.add("gap")
        # ...and the same constants under the symbol the CARD prints, so a law
        # may be written either way round. `_law_constants` puts the value under
        # both spellings; this is only the compile-time permission.
        import mew_lawlang
        names |= {sym for sym, attr in mew_lawlang.SYMBOLS.items()
                  if attr in names}
        return names

    def _law_constants(self):
        """Every constant that HAS a numeric value right now: the Material,
        Nozzle and Calibration fields, plus `gap` for the working distance.
        Built once -- the dataclasses are frozen and the model is constructed
        per run. An unset optional is absent, so naming one outside its guard
        gives "X has no value at this operating point" rather than a wrong
        number."""
        if self._law_env is None:
            env = {}
            for obj in (self.material, self.nozzle, self.calibration):
                for fld in dataclasses.fields(obj):
                    val = getattr(obj, fld.name)
                    if isinstance(val, bool) or not isinstance(
                            val, (int, float)):
                        continue          # flags, dicts and the optional Nones
                    env[fld.name] = float(val)
            env["gap"] = self.working_distance
            # each constant AGAIN under the symbol its card typesets, so `c` and
            # `sag_coeff` are one value and not two that could drift apart
            import mew_lawlang
            for sym, attr in mew_lawlang.SYMBOLS.items():
                if attr in env:
                    env[sym] = env[attr]
            self._law_env = env
        return self._law_env

    def _law_scope(self, key, **args):
        """The namespace one law evaluation sees: the constants, then the live
        process context, then the user's own coefficients for THIS law, then the
        law's own arguments -- in that order, so an argument always wins over a
        same-named constant and a user coefficient can shadow a shipped one.

        Last comes the DERIVED half: a quantity this law was not handed, that
        the process context is enough to compute. That is what lets the fiber
        diameter law name `eta` although eta is the Material card's law's
        output, which is the whole reason the editor can now show one vocabulary
        instead of a different list per card."""
        env = dict(self._law_constants())
        env.update(self.context)
        consts = (self.calibration.law_consts or {}).get(key) or {}
        for nm, val in consts.items():
            try:
                env[str(nm)] = float(val)
            except (TypeError, ValueError):
                continue
        env.update(args)
        self._derive_into(key, env)
        return env

    # A derived quantity is only worth the call when the expression names it,
    # and `compile_law` records exactly that on the closure. The sag law runs
    # once per span, so `c * span**p` must not pay for four physics calls it
    # never reads.
    def _derive_into(self, key, env):
        import mew_lawlang
        hit = self._law_cache.get(key)
        uses = getattr(hit[1], "uses", None) if hit else None
        if not uses:
            return
        for nm in uses:
            if nm in env:
                continue
            chain = mew_lawlang.DERIVED_BY.get(nm)
            if not chain or key in chain:
                continue            # not derivable, or deriving it needs THIS law
            if nm in self._deriving:
                continue            # a cycle: leave it absent and say so
            self._deriving.add(nm)
            try:
                val = self._derived(nm, env)
            except (ValueError, ZeroDivisionError, OverflowError, TypeError,
                    KeyError):
                val = None          # "no value at this operating point"
            finally:
                self._deriving.discard(nm)
            if val is not None:
                env[nm] = float(val)

    def _derived(self, name, env):
        """One derived quantity from the process context, or None when the
        context does not carry what it needs. Memoised per context, because
        every one of these depends on the context alone -- `set_context` clears
        the cache."""
        if name in self._derived_env:
            return self._derived_env[name]
        temp = env.get("temperature", self.context.get("temperature"))
        press = env.get("pressure", self.context.get("pressure"))
        speed = env.get("speed", self.context.get("speed"))
        val = None
        if name == "eta" and temp is not None:
            val = self.viscosity(float(temp))
        elif name == "visc_ratio" and temp is not None:
            t_ref = (self.calibration.sag_temp_ref
                     if self.calibration.sag_temp_ref is not None
                     else self.material.temperature_ref)
            ref = self.viscosity(float(t_ref))
            val = (self.viscosity(float(temp)) / ref) if ref else None
        elif name == "Q" and temp is not None and press is not None:
            val = self.flow_rate(float(press), float(temp))
        elif name == "d" and speed is not None and float(speed) > 0.0:
            q = env.get("Q")
            if q is None and temp is not None and press is not None:
                q = self.flow_rate(float(press), float(temp))
            if q is not None:
                val = self.fiber_diameter(float(q), float(speed))
        elif name == "CTS":
            volt = env.get("voltage", self.context.get("voltage"))
            gap = env.get("gap", self.context.get("gap", self.working_distance))
            if volt is not None and gap is not None:
                val = self.critical_speed(float(volt), float(gap))
        self._derived_env[name] = val
        return val

    # -- constitutive pieces (each a single physical law) ------------------- #
    def viscosity(self, temperature_c: float) -> float:
        """Arrhenius melt viscosity eta(T) = eta_ref exp[Ea/R (1/T - 1/T_ref)], Pa.s
        ([S5]). Arrhenius, not WLF, is the standard form for a polymer melt well above
        its Tm -- which PCL is at MEW temperatures (Tm ~60 degC, printed 70-100 degC).

        Ea is the SOLE driver of the model's temperature sensitivity: the eta_ref
        prefactor cancels from d ln(eta)/dT, so magnitude error absorbed into eta_ref
        does not bias the temperature scaling. Measure Ea on YOUR PCL for a
        publishable temperature claim.

        CAVEAT -- this is the FRESH-melt viscosity at fixed Mn. Held molten, PCL
        chain-scissions: Mn falls, eta falls, and fiber diameter DRIFTS upward over
        the hours a melt sits at temperature ([S24]). Not modelled.

        USER-REPLACEABLE (`mew_lawlang` law `viscosity`). A shear-thinning melt is
        a change of FORM, not of exponent, so no hook could have expressed it. The
        override feeds `flow_rate` and therefore `fiber_diameter` as well -- eta is
        one quantity whichever law produced it."""
        m = self.material
        fn = self._law("viscosity")
        if fn is not None:
            return fn(self._law_scope("viscosity",
                                      temperature=float(temperature_c)))
        t = temperature_c + CELSIUS_TO_KELVIN
        if not (math.isfinite(t) and t > 0.0):
            raise ValueError("temperature must be above absolute zero "
                             "(-273.15 degC), got %r" % temperature_c)
        t_ref = m.temperature_ref + CELSIUS_TO_KELVIN
        return m.viscosity_ref * math.exp(
            m.activation_energy / GAS_CONSTANT * (1.0 / t - 1.0 / t_ref))

    def flow_rate(self, pressure_pa: float, temperature_c: float) -> float:
        """Volumetric melt flow Qv through the needle, mm^3/s -- Hagen-Poiseuille
        Qv = pi r^4 dP / (8 eta(T) L) ([S4]).

        Computed in SI then converted. Pressure at or below ambient yields no flow
        rather than a negative number.

        Assumptions, all met at MEW conditions (state them when publishing):
          * NEWTONIAN melt -- the wall shear rate is ~0.1-0.3 1/s here, deep in PCL's
            Newtonian plateau ([S26]).
          * dP across the NEEDLE ~ applied gauge pressure: resistance ~ 1/r^4, so the
            wide syringe barrel drops ~1e4-1e5x less pressure than the bore.
          * Neglected: the surface-tension start threshold 2 gamma/r (~0.07 psi
            against a typical 1-5 psi) and the electrostatic tip-draw assist.
        Only the lumped group pi r^4/(8 eta_ref L) is identifiable from flow data --
        r, L and eta_ref trade off (see `Nozzle.length`).

        USER-REPLACEABLE (`mew_lawlang` law `flow`). `eta` is passed INTO that law
        rather than left for it to recompute, so a replaced viscosity law reaches a
        replaced flow law with no second seam to keep in step. The zero-pressure
        guard is inside the law's own expression, not in front of it: a user who
        writes a flow law with a start threshold owns that decision."""
        eta = self.viscosity(temperature_c)
        fn = self._law("flow")
        if fn is not None:
            return fn(self._law_scope("flow", pressure=float(pressure_pa),
                                      temperature=float(temperature_c),
                                      eta=float(eta)))
        if pressure_pa <= 0.0:
            return 0.0
        r = self.nozzle.inner_radius * 1.0e-3   # mm -> m
        length = self.nozzle.length * 1.0e-3    # mm -> m
        q_si = math.pi * r ** 4 * pressure_pa / (8.0 * eta * length)  # m^3/s
        return q_si * MM3_PER_M3                                      # mm^3/s

    def fiber_diameter(self, flow_rate: float, speed: float) -> float:
        """Deposited fiber diameter from mass conservation, mm (d ~ v^-0.5).
        Field-independent by construction -- matches the empirical finding that
        voltage barely moves diameter. Validated on PCL by Hrynevich 2018
        (sources.txt [S16], primary text confirmed): d ~ v^-0.5
        (R^2=0.974, CTS..13xCTS) AND d ~ P^0.5 via Q ~ P (R^2=0.990); this model
        reproduces both. Steady fiber (v >= CTS) only -- the transient-diameter
        caveat lives at [S16]/[S3]."""
        if speed <= 0.0:
            raise ValueError("speed must be positive (mm/s)")
        fn = self._law("diameter")
        if fn is not None:
            return fn(self._law_scope("diameter", Q=float(flow_rate),
                                      speed=float(speed)))
        return math.sqrt(4.0 * flow_rate / (math.pi * speed))

    def min_print_speed(self, voltage_kv: float, pressure_pa: float,
                        temperature_c: float,
                        margin: float = CTS_FLOOR_MARGIN) -> float:
        """The slowest surface speed (MM/MIN) a five-axis run may be slowed to
        while the rotary limiter buys time instead of perpendicularity: this
        model's CTS at these settings, times `margin`.

        ONE FORMULA, TWO CALLERS. The Curved-collector page's blank "Min print
        speed" resolves through here, and `test_five_axis_matrix` generates
        every case through here, so the suite's files are timed the way the
        page's files are. Written as a number in either place, the two drift
        and the matrix stops saying anything about what a user gets.

        `margin` is 10% because CTS is a calibrated anchor rather than a closed
        form (`Calibration.cts_ref`), and the one lag measurement nearest it was
        taken at 1.125 x CTS -- the floor stays inside ground the rig has
        actually printed on. Below CTS the jet coils instead of drawing a
        straight fibre, so this is a floor and not a preference.
        """
        return 60.0 * margin * self.cts_at(voltage_kv, pressure_pa,
                                           temperature_c)

    def cts_at(self, voltage_kv: float, pressure_pa: float,
               temperature_c: float) -> float:
        """CTS (mm/s) at this model's own working distance and these machine
        settings -- `critical_speed` with the flow rate and viscosity those
        settings imply passed in.

        This is what a caller holding KNOB VALUES should use. `critical_speed`
        takes flow and viscosity as optional arguments, and a call that leaves
        them out raises on a rig with a fitted `cts_flow_exp` / `cts_visc_exp`:
        a page that caught that and substituted a constant showed a different
        CTS from every other page. Deriving the two here means there is one way
        to ask "what is CTS for this rig right now", and it cannot be asked
        with half its arguments."""
        return self.critical_speed(
            voltage_kv, self.working_distance,
            flow_rate=self.flow_rate(pressure_pa, temperature_c),
            viscosity=self.viscosity(temperature_c))

    def critical_speed(self, voltage_kv: float, gap_mm: float,
                       flow_rate: float | None = None,
                       viscosity: float | None = None) -> float:
        """CTS (mm/s): the jet's landing speed -- the translation speed that lays the
        fiber straight. Operationally the coiling ONSET (the LOWEST straight-fiber
        speed) and a MEASURED per-session anchor, NOT a prediction: by mass
        conservation CTS = 4Q/(pi d_CTS^2), so predicting it is equivalent to
        predicting the touchdown diameter, an EHD unknown with no closed form.

            CTS = cts_ref * (V/V_ref)^a * (gap/gap_ref)^b
                          * (Q/Q_ref)^c * (eta/eta_ref)^d

        With every exponent 0 (the default) this is just cts_ref -- no claim about how
        any knob moves CTS until a fit supplies an exponent, and the zero defaults are
        GROUNDED rather than lazy: CTS is a draw-vs-drag balance, so flow and
        temperature set the DIAMETER, not the landing speed. Voltage and gap need no
        extra arguments; the flow and viscosity terms stay inert unless their exponent
        is set, and then REQUIRE the current flow_rate / viscosity (which `solve`
        passes) plus the matching reference in Calibration. PRESSURE enters through Q,
        TEMPERATURE through Q and eta. The shipped anchor is an UPPER BOUND until an
        onset sweep pins it -- see the Calibration cts_* block."""
        cal = self.calibration
        fn = self._law("cts")
        if fn is not None:
            # Q and eta are OPTIONAL arguments here, so they go into the scope
            # only when the caller actually has them. Left out, a law that names
            # one fails with "Q has no value at this operating point" -- which is
            # the truth, and better than a silent 0.0 that would make the law
            # quietly wrong. `solve` always passes both.
            args = {"voltage": max(float(voltage_kv), 0.0),
                    "gap": max(float(gap_mm), 1e-9)}
            if flow_rate is not None:
                args["Q"] = float(flow_rate)
            if viscosity is not None:
                args["eta"] = float(viscosity)
            return fn(self._law_scope("cts", **args))
        v = max(voltage_kv, 0.0) / cal.cts_voltage_ref
        g = max(gap_mm, 1e-9) / cal.cts_gap_ref
        cts = cal.cts_ref * v ** cal.cts_voltage_exp * g ** cal.cts_gap_exp
        # Flow (pressure) and viscosity (temperature) hooks: each is a factor of
        # exactly 1 until its exponent is fitted, so the default CTS is voltage&gap
        # only and a plain critical_speed(V, gap) call is unchanged.
        if cal.cts_flow_exp != 0.0:
            if cal.cts_flow_ref is None or flow_rate is None:
                raise ValueError(
                    "cts_flow_exp is set but cts_flow_ref or the flow_rate "
                    "argument is missing -- both are needed to fit CTS vs flow")
            cts *= (max(flow_rate, 1e-12) / cal.cts_flow_ref) ** cal.cts_flow_exp
        if cal.cts_visc_exp != 0.0:
            if cal.cts_visc_ref is None or viscosity is None:
                raise ValueError(
                    "cts_visc_exp is set but cts_visc_ref or the viscosity "
                    "argument is missing -- both are needed to fit CTS vs viscosity")
            cts *= (max(viscosity, 1e-12) / cal.cts_visc_ref) ** cal.cts_visc_exp
        return cts

    def lag_angle(self, speed_ratio: float) -> float:
        """Jet-lag CHORD angle in degrees from the speed ratio eps = v/CTS.

        theta = theta_cts + arctan(A * (1 - eps^-b)), A = `lag_amplitude`.
        With theta_cts pinned to 0 that makes lag_distance = H*A*(1 - eps^-b), which
        IS Hrynevich 2020's Eq.4 with the gap divided out -- exact at every speed
        ratio, not fitted to a window (LAG-POWER-FORM). The lag SATURATES at A*H, so
        it cannot diverge. Below CTS the jet is near-vertical, so we return the floor.

        THE AMPLITUDE IS A AND NOTHING ELSE. A per-rig multiplier k used to scale
        this whole expression on top of A, which is one degree of freedom carried
        twice; a rig that needs a different amplitude now moves A itself, and
        `lag_ray_constants` hands the tracker the same A in lag space. Provenance of
        A and b: the Calibration lag_* block."""
        cal = self.calibration
        floor = cal.lag_angle_cts_deg
        if speed_ratio <= 1.0:
            return floor
        # The floor is SUBTRACTED from the amplitude, not added on top of it: the
        # ceiling must stay Eq.4's asymptote A whatever the floor is. Composed in LAG
        # space rather than angle space because that is where A lives --
        # tan(theta_cts + arctan(A)) != tan(theta_cts) + A, and adding in angle space
        # inflated the asymptote by 19% at a 2.9 deg floor.
        ceiling = float(cal.lag_amplitude)
        base = math.tan(math.radians(floor))
        return math.degrees(math.atan(
            base + (ceiling - base)
            * (1.0 - speed_ratio ** -cal.lag_saturation_exp)))

    def lag_ray_constants(self):
        """`(lag_ceiling, lag_floor, saturation_exp)` for `integrate_contact_path_ray`.

        The tracker needs the two saturation constants as DISTANCES at this gap, and
        composes them in lag space exactly as `lag_angle` does. The ceiling is
        `lag_amplitude` (A) times the gap -- straight out of Eq.4, no second
        multiplier -- and the floor is the floor ANGLE's tangent times the gap.

        It exists so that the amplitude cannot be applied in one place and forgotten
        in another. `mew_deposition` and `mew_live` both build these three numbers and
        must agree with `solve`'s `lag_distance` to the last bit: a ceiling that
        disagrees with the tau it normalises against mis-sizes V_JCP everywhere. Call
        this rather than re-deriving them.
        """
        cal = self.calibration
        H = self.working_distance
        return (H * float(cal.lag_amplitude),
                H * math.tan(math.radians(cal.lag_angle_cts_deg)),
                cal.lag_saturation_exp)

    def circumferential_lag(self, R_ax, radius, gap=None):
        """`R_cir` in mm of arc on a mandrel of surface radius `radius`, UNCAPPED
        -- the paper's Eq. 7 unless the user replaced it.

        USER-REPLACEABLE (`mew_lawlang` law `rcir`). The shipped form lives in
        `mew_mandrel.circumferential_lag` (its docstring carries the derivation
        and what it costs against Neuhaus); this method is only the seam, so every
        mandrel site asks HERE and a typed law reaches the placement, the
        prediction and the Physics card through one path.

        `gap` defaults to this model's working distance; the placement passes its
        own frame's gap, which is the same number on a 4-axis lay. A non-positive
        lag returns itself without consulting any law: no lag is no contact
        offset, whatever form the arc takes. `cap_circumferential_lag` is the
        caller's job, as it is for the shipped form."""
        R_ax = float(R_ax)
        gap = self.working_distance if gap is None else float(gap)
        fn = self._law("rcir")
        if fn is None or R_ax <= 0.0:
            import mew_mandrel
            return mew_mandrel.circumferential_lag(R_ax, radius, gap)
        return fn(self._law_scope("rcir", R_ax=R_ax,
                                  theta=math.atan2(R_ax, gap),
                                  r_m=float(radius), gap=gap))

    # -- below-CTS coiling: the sewing machine ------------------- #
    def coil_tangent_amplitude(self, speed_ratio: float) -> float:
        """Tangent-oscillation amplitude Psi of the below-CTS coil at eps=v/CTS.
        Fixed by arc-length conservation: J0(Psi)=eps makes the deposited length
        CTS/v=1/eps (Brun et al. 2015; Morris et al. 2008). 0 at eps>=1 (straight,
        the forward-Hopf onset), rising to the coil as eps falls. The geometry
        module integrates this into the laid centreline (see mew_deposition)."""
        return invert_j0_unit(speed_ratio)

    def coil_angular_frequency(self, cts: float, speed_ratio: float) -> float:
        """Coiling clock omega (rad/s) below CTS. The geometric identity
        CTS = R*Omega_c (Ribe & Lister 2006; Brun et al. 2015; Sze et al. 2024) fixes the onset value
        omega = CTS/R, with R the coil radius (`coil_radius_ref`); omega_c is
        nearly the steady-coiling frequency at onset (Morris et al. 2008). The mild
        dependence on v/CTS is a fittable hook (`coil_freq_eps_exp`; 0 = constant
        clock, the honest default until below-CTS frequency data justify a slope).
        Returns 0 for a non-positive radius or CTS (coiling then disabled)."""
        cal = self.calibration
        if cal.coil_radius_ref <= 0.0 or cts <= 0.0:
            return 0.0
        eps = min(max(speed_ratio, 1e-9), 1.0)
        return (cts / cal.coil_radius_ref) * eps ** cal.coil_freq_eps_exp

    # -- bridge sag: the grounded droop law (physics build, opt-in) --------- #
    def bridge_sag_depth(self, span, diameter, sag_multiplier: float = 1.0,
                         viscosity_ratio: float = 1.0):
        """Free midspan droop (mm) of a fiber BRIDGING an unsupported `span` (mm) at
        fiber diameter `diameter` (mm) -- the reduced-order, rig-calibrated
        replacement for the geometric `gcode_to_fibers.sag_depth` heuristic.

            depth = sag_coeff * mult
                    * span^sag_span_exp
                    * (d / sag_diam_ref)^sag_diam_exp        # 1 at the default exp 0
                    * viscosity_ratio^sag_thermal_visc_exp

        This is a renderer for the suspended-span droop MAGNITUDE, not a validated
        magnitude mechanism: the static-tension basis is RETRACTED and the real
        drivers (visco-elastic creep [S41], electrostatic collector coupling) are out
        of scope. The span^2 EXPONENT is grounded -- a viscous fiber bridging a pore
        is a viscous catenary ([S44]/[S45]), stretching-dominated for every sag we
        render, with span^4 only the sub-d bending transient. The contact geometry the
        solver feeds is exact and magnitude-INVARIANT; only this depth is calibrated.

        At the shipped default (sag_diam_exp = 0) the diameter factor is exactly 1 and
        sag_diam_ref drops out, so the law is paper Eq (7) depth = c*span^2 with
        c = sag_coeff (mm^-1). Engaging the diameter hook anchors the magnitude at
        d = sag_diam_ref -- and with sag_coeff held fixed, flipping q rescales a
        uniform print's absolute sag (a 20 um print sags 0.34x at q = 0 against the old
        q = 2), so the hook is inert only at the default. `viscosity_ratio` is
        eta(T_print)/eta(T_ref), read only when sag_thermal_visc_exp != 0."""
        cal = self.calibration
        fn = self._law("sag")
        if fn is not None:
            # `sag_multiplier` is NOT in the law's scope. It was the GUI's "Sag
            # multiplier" box, which is gone (it scaled a calibrated coefficient
            # from a different page, so the number on the Physics card was not
            # the number the export used). The shipped path below still honours
            # it for `--sag-multiplier` and old presets; a user law is written
            # against `c` on the card, which is the one place to scale it now.
            #
            # The exact solver passes a whole ARRAY of spans (`parab`), which the
            # shipped arithmetic below broadcasts over. `fn.array` does the same
            # for a user law; per-span calls doubled a default-scaffold solve.
            if getattr(span, "ndim", 0) or getattr(diameter, "ndim", 0):
                return fn.array(self._law_scope(
                    "sag", span=span, d=diameter,
                    visc_ratio=float(viscosity_ratio)))
            return fn(self._law_scope(
                "sag", span=float(span), d=float(diameter),
                visc_ratio=float(viscosity_ratio)))
        # Diameter enters ONLY through the opt-in exponent hook, as the ratio
        # (d / sag_diam_ref)^q anchored at the reference fiber. At the shipped
        # default (sag_diam_exp = 0) this factor is exactly 1 and sag_diam_ref is
        # genuinely inert, so the law reduces to the single identifiable magnitude
        # c = sag_coeff times span^2 (d_ref folded in; see the sag_* block). Guard
        # mirrors the fusion law so d_ref never touches the default path.
        diam_factor = 1.0
        if cal.sag_diam_exp != 0.0 and cal.sag_diam_ref > 0.0:
            diam_factor = (diameter / cal.sag_diam_ref) ** cal.sag_diam_exp
        thermal = (viscosity_ratio ** cal.sag_thermal_visc_exp
                   if cal.sag_thermal_visc_exp != 0.0 else 1.0)
        return (cal.sag_coeff * sag_multiplier
                * span ** cal.sag_span_exp * diam_factor * thermal)

    def bridge_sag_depth_viscous(self, span, diameter, sag_multiplier: float = 1.0,
                                 viscosity_ratio: float = 1.0):
        """Free midspan droop (mm) under the OPT-IN VISCOUS-CREEP model
        (Calibration.sag_viscous_creep = True) -- the grounded reconciliation with
        [S41], where a just-laid fiber creeps visco-elastically under self-weight
        rather than bending as an elastic beam:

            depth = sag_creep_coeff * mult * (eta(T_ref) / eta(T))
                    * (span / sag_creep_span_ref) ** sag_creep_span_exp
                    * (diameter / sag_diam_ref)  ** sag_creep_diam_exp

        so depth = sag_creep_coeff at the three reference values. Unlike
        bridge_sag_depth, TEMPERATURE is PREDICTED here: the factor is the RECIPROCAL
        of `viscosity_ratio` and is ALWAYS active, because it is the physics rather
        than an optional hook. Exponents default to the viscous-BENDING regime and
        both stay FIT ME -- there is no depth-vs-span data to decide them."""
        cal = self.calibration
        dref = cal.sag_diam_ref if cal.sag_diam_ref > 0.0 else (
            diameter if diameter > 0.0 else 1.0)
        sref = cal.sag_creep_span_ref if cal.sag_creep_span_ref > 0.0 else 1.0
        # depth ~ 1/eta(T): viscosity_ratio = eta(T)/eta(T_ref), so the factor is its
        # reciprocal (eta_ref/eta). Predictive -- the -1 power is physics, not a hook.
        temp_factor = (1.0 / viscosity_ratio) if viscosity_ratio > 0.0 else 1.0
        return (cal.sag_creep_coeff * sag_multiplier * temp_factor
                * (span / sref) ** cal.sag_creep_span_exp
                * (diameter / dref) ** cal.sag_creep_diam_exp)

    def make_sag_law(self, *, temperature: float | None = None):
        """Build the droop-law closure to install into `gcode_to_fibers`'s sag-law
        seam (`set_sag_law`). It has the SAME (span, diameter, sag_multiplier)
        signature as the geometric `sag_depth`, so the batch solver AND the live
        `_SagState` march call it unchanged -- a single install reshapes the
        exported scaffold and the real-time re-lay alike.

        Picks the law from Calibration.sag_viscous_creep: the default (False) is the
        reduced-order `bridge_sag_depth`; True selects the DORMANT future-work
        `bridge_sag_depth_viscous` (the Nguyen reconciliation -- temperature predicted
        via eta(T)). The run's `temperature` is folded ONCE into a melt-viscosity ratio
        eta(T)/eta(T_ref). The viscous law ALWAYS needs it (temperature is its physics);
        the reduced-order law needs it only when its thermal hook is on
        (sag_thermal_visc_exp != 0). When it is not needed (or `temperature` is None) the
        ratio is 1, so the DEFAULT path stays byte-identical. Pure: returns a plain
        callable, no module state."""
        cal = self.calibration
        # The viscous-creep law predicts temperature (always needs eta(T)); the static
        # law needs it only when its thermal hook is enabled.
        need_vr = cal.sag_viscous_creep or cal.sag_thermal_visc_exp != 0.0
        if temperature is None or not need_vr:
            vr = 1.0
        else:
            t_ref = (cal.sag_temp_ref if cal.sag_temp_ref is not None
                     else self.material.temperature_ref)
            vr = self.viscosity(temperature) / self.viscosity(t_ref)

        depth_fn = (self.bridge_sag_depth_viscous if cal.sag_viscous_creep
                    else self.bridge_sag_depth)

        def law(span, diameter, sag_multiplier=1.0):
            return depth_fn(span, diameter, sag_multiplier, viscosity_ratio=vr)
        return law

    def _sink_from_overlap(self, phi, diameter, viscosity_ratio: float = 1.0):
        """Run a base overlap fraction `phi` through the shared fusion hook chain ->
        a centre-sink delta (mm). Factored out of `junction_sink` so the WALL overlap
        gets the identical diameter / thermal / clamp treatment -- the two contact
        kinds differ only in their calibrated magnitude, never in the hooks."""
        cal = self.calibration
        if phi <= 0.0 or diameter <= 0.0:
            return 0.0
        fn = self._law("fusion")
        if fn is not None:
            # ONE expression covers junction and wall: `phi` arrives already set
            # to the right magnitude for the contact kind, exactly as the shipped
            # chain below takes it.
            return fn(self._law_scope(
                "fusion", phi=float(phi), d=float(diameter),
                visc_ratio=float(viscosity_ratio)))
        if cal.fusion_diam_exp != 0.0 and cal.fusion_diam_ref > 0.0:
            phi = phi * (diameter / cal.fusion_diam_ref) ** cal.fusion_diam_exp
        if cal.fusion_thermal_visc_exp != 0.0:
            phi = phi * viscosity_ratio ** cal.fusion_thermal_visc_exp
        phi = max(0.0, min(phi, cal.fusion_max_overlap))
        return phi * diameter

    def junction_sink(self, diameter, viscosity_ratio: float = 1.0):
        """Centre-sink delta (mm) of a fiber CROSSING OVER the structure below at
        fiber diameter `diameter` (mm): how far its resting centre drops below the
        geometric rest-on-top height because it coalesces INTO the fiber beneath.
        The crossover-localised twin of `bridge_sag_depth` -- see the Calibration
        `fusion_*` block for the physical basis (contact-localised coalescence) and
        the grading note (empirical magnitude, a single calibrated overlap):

            delta = fusion_overlap * d
                    * (d / fusion_diam_ref) ** fusion_diam_exp
                    * viscosity_ratio ** fusion_thermal_visc_exp

        clamped to [0, fusion_max_overlap * d] so the upper centre never reaches the
        lower fiber's centre (a full merge). `viscosity_ratio` is eta(T_print)/
        eta(T_ref) (bound once by make_fusion_law; used only when
        fusion_thermal_visc_exp != 0). Pure; no numpy import (scalar `**`).
        fusion_overlap == 0 (or non-positive diameter) -> 0.0 (no fusion)."""
        return self._sink_from_overlap(self.calibration.fusion_overlap,
                                       diameter, viscosity_ratio)

    def wall_sink(self, diameter, viscosity_ratio: float = 1.0):
        """Centre-sink delta (mm) at a COLLINEAR (wall / straight-span) contact --
        one fiber laid or drooping along another rather than across it, a LINE
        contact. Identical hook chain to `junction_sink`, driven by
        `fusion_wall_overlap`; None there -> the junction overlap, so an unsplit
        calibration gives wall_sink == junction_sink and the solver's angular path
        stays switched off (byte-identical). See the Calibration wall-fusion block."""
        cal = self.calibration
        phi = (cal.fusion_overlap if cal.fusion_wall_overlap is None
               else cal.fusion_wall_overlap)
        return self._sink_from_overlap(phi, diameter, viscosity_ratio)

    def fusion_wall_band(self):
        """The (sin_lo, sin_hi) crossing-angle band the solver blends wall -> junction
        over, or None when this calibration draws NO distinction (fusion_wall_overlap
        unset -> one magnitude everywhere). None is what keeps the default solve on its
        scalar, byte-identical path: the solver only pays for the per-contact angle
        when a band is installed. Degenerate/inverted bands (hi <= lo) collapse to a
        hard switch at lo rather than dividing by zero."""
        cal = self.calibration
        if cal.fusion_wall_overlap is None:
            return None
        lo = math.sin(math.radians(max(0.0, min(90.0, cal.fusion_wall_angle_lo))))
        hi = math.sin(math.radians(max(0.0, min(90.0, cal.fusion_wall_angle_hi))))
        return (lo, hi)

    def make_fusion_law(self, *, temperature: float | None = None):
        """Build the fusion closure for gcode_to_fibers' fusion seam
        (`set_fusion_law`): a callable `sink(diameter, wall=False) -> delta` (mm), the
        contact-localised inter-layer compaction the sag solver applies at fiber-on-
        fiber contacts ONLY. `wall=False` (the default, so an old one-arg call site
        still reads the junction magnitude) -> the CROSSING sink; `wall=True` -> the
        COLLINEAR sink. The solver blends between the two per contact using the band
        from `fusion_wall_band` -- it, not this closure, does the vectorised blend, so
        this module stays numpy-free. Mirror of `make_sag_law`: the run's
        `temperature` is folded ONCE into a melt-viscosity ratio (used only when
        fusion_thermal_visc_exp != 0); None, or a zero thermal exponent, -> ratio 1 (no
        thermal factor). Pure: returns a plain callable, no module state. With
        fusion_overlap == 0 the callable returns 0 everywhere (the caller treats that
        as 'no fusion')."""
        cal = self.calibration
        if temperature is None or cal.fusion_thermal_visc_exp == 0.0:
            vr = 1.0
        else:
            t_ref = (cal.fusion_temp_ref if cal.fusion_temp_ref is not None
                     else self.material.temperature_ref)
            vr = self.viscosity(temperature) / self.viscosity(t_ref)

        def law(diameter, wall=False):
            if wall:
                return self.wall_sink(diameter, viscosity_ratio=vr)
            return self.junction_sink(diameter, viscosity_ratio=vr)
        return law

    # -- cone-jet profile: the cone is the thick top of the jet ------------- #
    def jet_radius(self, flow_rate: float, speed: float) -> float:
        """Jet radius from mass conservation, mm: r = sqrt(Q / (pi v)). The cone, the
        neck, and the fiber are all this ONE relation at different speeds -- the melt
        is fattest where it is slowest. (Equivalently 0.5 * fiber_diameter(Q, v).)
        Non-positive speed -> 0."""
        if speed <= 0.0:
            return 0.0
        return math.sqrt(max(flow_rate, 0.0) / (math.pi * speed))

    def nozzle_exit_speed(self, flow_rate: float) -> float:
        """Mean melt speed at the bore exit, mm/s = Q / (pi r_nozzle^2). The jet
        STARTS at this speed, so jet_radius(Q, this) = r_nozzle EXACTLY for any Q --
        which is precisely why the cone base is pinned to the bore and never widens
        with flow (raising Q raises this speed in proportion, leaving the base put)."""
        r = self.nozzle.inner_radius
        return flow_rate / (math.pi * r * r) if r > 0.0 else math.inf

    def _cone_exp(self, v_exit: float, v_land: float) -> float:
        """The front-loading exponent for the cone profile, DERIVED per jet so the
        cone knee (where the jet has necked to half the base width, i.e. v = 4*v_exit)
        sits at cone_knee_frac of the drop -- independent of the base/fiber ratio.

        r = r_base/2  <=>  v = 4*v_exit  <=>  (v - v_exit)/(v_land - v_exit) = 3/(R-1)
        where R = v_land/v_exit = (r_base/r_fibre)^2. Setting that equal to
        knee_frac ** exp and solving:  exp = ln(3/(R-1)) / ln(knee_frac).
        Clamped to [1, 6]; falls back to 1 (linear) when there is no real cone
        (base < 2x fiber, R <= 4) or the inputs are degenerate. See the Calibration
        cone_knee_frac block (CONE-KNEE-ASHOUR)."""
        knee = self.calibration.cone_knee_frac
        if v_exit <= 0.0 or v_land <= v_exit or not (0.0 < knee < 1.0):
            return 1.0
        val = 3.0 / (v_land / v_exit - 1.0)          # = knee**exp at the half-base knee
        if val >= 1.0:                               # base < 2x fiber: no cone, linear
            return 1.0
        return min(max(math.log(val) / math.log(knee), 1.0), 6.0)

    def jet_speed_at(self, flow_rate: float, landing_speed: float,
                     drop_fraction: float) -> float:
        """Jet speed (mm/s) at a point that has fallen through `drop_fraction` in
        [0, 1] of the nozzle->collector potential. The jet accelerates from the
        bore-exit speed to the landing speed as the field draws it down:

            v = v_exit + (v_land - v_exit) * clamp(drop_fraction, 0, 1) ** exp

        The exponent is DERIVED (`_cone_exp`) so the cone stands a fixed fraction
        (cone_knee_frac, default 0.12) of the gap tall regardless of the
        base/fiber ratio -- a fixed exponent instead makes the cone collapse in the
        top few percent on high-ratio rigs (the 'tiny cone in z' bug). Search tag
        CONE-KNEE-ASHOUR (Calibration).

        Feed v back through jet_radius to trace the cone -> fiber profile: at
        drop_fraction 0 (the nozzle) v = v_exit so r = r_nozzle (the pinned base); at
        drop_fraction 1 (touchdown) v = landing_speed so r = the fiber radius; in
        between the melt necks smoothly. drop_fraction comes from the electric
        potential along the jet (mew_field), so a stronger tip field sharpens the cone.

        The profile is PINNED at both ends -- 0**exp = 0 and 1**exp = 1 -- so v_exit
        and the landing speed (hence the deposited fiber diameter, CTS, lag and the
        .depo) are INVARIANT to cone_knee_frac. It reshapes only the jet's interior,
        whose one consumer is the drawn jet tube."""
        v0 = self.nozzle_exit_speed(flow_rate)
        exp = self._cone_exp(v0, landing_speed)
        f = min(max(drop_fraction, 0.0), 1.0) ** exp
        return v0 + (landing_speed - v0) * f

    # -- the electrostatic field (mew_field seam) --------------------------- #
    def build_field(self, voltage_kv: float) -> AnalyticField:
        """The gap's electric field for the current voltage, in the field frame
        (collector grounded at z = 0, needle tip at (0, 0, working_distance)).

        With `field_tip_strength` = 0 (the default) this is the bare uniform gap
        field, E = V/H downward -- the spatial analog of CTS = cts_ref. A nonzero
        strength adds an image-paired tip source (and is where a future
        deposited-fiber perturbation would superpose more sources). Cheap to build;
        a consumer that samples it many times per frame should build it once per
        parameter change, not per query."""
        sources = ()
        if self.calibration.field_tip_strength != 0.0:
            sources = (PointCharge(height=self.working_distance,
                                   strength=self.calibration.field_tip_strength),)
        return AnalyticField(voltage=max(voltage_kv, 0.0),
                             gap=self.working_distance, sources=sources)

    # -- the one entry point ------------------------------------------------ #
    def solve(self, params: ProcessParams) -> JetState:
        """Predict the full jet state for one parameter set."""
        for name in ("pressure", "voltage", "speed", "temperature"):
            if not math.isfinite(getattr(params, name)):
                raise ValueError("%s must be a finite number, got %r"
                                 % (name, getattr(params, name)))
        if params.speed <= 0.0:
            raise ValueError("speed must be positive (mm/s)")
        # Publish this parameter set BEFORE any law runs, so a user-replaced law
        # naming `voltage` or `temperature` sees THIS solve's values rather than
        # the previous one's. No-op when no law has been replaced.
        self.set_context(voltage=params.voltage, pressure=params.pressure,
                         temperature=params.temperature,
                         gap=self.working_distance, speed=params.speed)

        qv = self.flow_rate(params.pressure, params.temperature)
        eta = self.viscosity(params.temperature)
        cts = self.critical_speed(params.voltage, self.working_distance,
                                  flow_rate=qv, viscosity=eta)
        ratio = params.speed / cts if cts > 0.0 else math.inf

        diameter = self.fiber_diameter(qv, params.speed)

        if ratio < 1.0:
            regime = Regime.COILING
        elif ratio <= self.calibration.stable_window:
            regime = Regime.STABLE
        else:
            regime = Regime.OVERSTRETCH
        straight = ratio >= 1.0

        angle = self.lag_angle(ratio)
        lag_distance = self.working_distance * math.tan(math.radians(angle))
        lag_time = lag_distance / params.speed if params.speed > 0.0 else 0.0

        return JetState(
            flow_rate=qv,
            viscosity=eta,
            critical_speed=cts,
            speed_ratio=ratio,
            fiber_diameter=diameter,
            lag_angle_deg=angle,
            lag_distance=lag_distance,
            lag_time=lag_time,
            regime=regime,
            straight_fibre=straight,
            cone_base_radius=self.nozzle.inner_radius,
            nozzle_exit_speed=self.nozzle_exit_speed(qv),
        )


# --------------------------------------------------------------------------- #
# Transient contact dynamics -- a pure integrator
# --------------------------------------------------------------------------- #
def integrate_contact_path(points, times, relax_times):
    """Trace the fiber CONTACT point as a first-order relaxation of the moving
    nozzle, so lag BUILDS at a path start, SHEDS when the head stops, and eases
    through speed changes instead of teleporting. Superseded as the shipped
    placement rule by `integrate_contact_path_ray`; kept for comparison.

        dC/dt = (N(t) - C(t)) / tau        (per spatial component)

    At constant speed on a straight run this settles to C trailing N by exactly
    v*tau, so with tau the model's steady lag time it REPRODUCES the steady jet-lag
    geometry and departs only through transients (test_contact_reduces_to_steady_lag).
    Integration is the closed-form solution over each interval with N linear and tau
    constant -- EXACT for a polyline path and unconditionally stable at any step size,
    the update being a convex blend that cannot overshoot or ring.

    Args:
        points:      sequence of N nozzle positions, each an indexable of D coords
                     (D = 2 to track the ground track, 3 to also carry height).
        times:       sequence of N strictly non-decreasing sample times (s).
        relax_times: sequence of N-1 relaxation times tau (s), one per interval
                     points[k]->points[k+1]. tau <= 0 means "no lag" (the contact
                     pins to the nozzle across that interval).

    Returns:
        A list of N contact positions (tuples of D floats). contact[0] starts under
        the nozzle (zero initial lag -- the jet has not yet trailed at ignition)."""
    n = len(points)
    if n == 0:
        return []
    dims = len(points[0])
    contact = [tuple(float(v) for v in points[0])]
    c = [float(v) for v in points[0]]
    for k in range(n - 1):
        dt = times[k + 1] - times[k]
        if dt <= 0.0:                       # zero-length / non-advancing: no time
            contact.append(tuple(c))        # passes, so the contact cannot move
            continue
        tau = relax_times[k]
        p0, p1 = points[k], points[k + 1]
        if tau <= 1e-12:                    # no lag here -> contact rides the nozzle
            c = [float(p1[d]) for d in range(dims)]
            contact.append(tuple(c))
            continue
        # exact solution of C' = (N - C)/tau with N linear over [t_k, t_{k+1}]:
        #   C(dt) = N1 - V*tau + (C0 - N0 + V*tau) e^{-dt/tau},  V = (N1-N0)/dt
        decay = math.exp(-dt / tau) if dt / tau < 700.0 else 0.0
        for d in range(dims):
            v = (p1[d] - p0[d]) / dt
            c[d] = p1[d] - v * tau + (c[d] - p0[d] + v * tau) * decay
        contact.append(tuple(c))
    return contact


def integrate_contact_path_2nd(points, times, relax_times, zeta=1.0, *,
                               init=None, with_state=False):
    """Trace the fiber CONTACT point as a damped SECOND-ORDER tracker of the moving
    nozzle -- the generalisation of `integrate_contact_path` that ADMITS (but does
    not assert) a corner WHIP. Superseded as the shipped placement rule by
    `integrate_contact_path_ray`; kept for comparison.

    READ FIRST, because this docstring once fell into the trap: the INSIDE corner
    ROUNDING every MEW print shows is the LAG, not zeta. The contact trails the nozzle,
    so it cuts the inside of every turn for ANY zeta; zeta governs only whether it ALSO
    overshoots to the OUTSIDE, which no MEW print has shown -- hence the
    critically-damped default. zeta < 1 is a speculative, off-by-default extension for
    the one reported overshoot (Hrynevich 2020, a jet exceeding L_stable while
    accelerating), attributed there to electrostatics, so it reproduces the PHENOMENON
    via an inertial lump, not a mechanism.

        C'' + 2 zeta omega C' + omega^2 (C - N) = 0 ,   omega = 2 zeta / tau

    omega is PINNED to 2 zeta / tau so the steady trailing error is exactly
    v*tau = lag_distance for ANY zeta: zeta changes only the transient, never the steady
    geometry the model is calibrated against, and zeta -> infinity reduces this EXACTLY
    to `integrate_contact_path`. Integration is the interval-exact state transition,
    unconditionally stable. `init` (c0, w0) starts from a handed-in contact position
    and velocity instead of the at-rest ignition state -- the seam that runs ONE
    continuous jet across separately-laid segments; `with_state` returns
    (contact, (c, w)) for the next. The tau <= 0 branch PINS the contact to the nozzle,
    discarding any inherited state: correct, but a position jump, and only below CTS."""
    n = len(points)
    if n == 0:
        return ([], (list(init[0]), list(init[1])) if init else ([], []))\
            if with_state else []
    dims = len(points[0])
    z = max(float(zeta), 1e-3)              # zeta -> 0 is degenerate (omega -> 0)
    if init is None:
        c = [float(v) for v in points[0]]
        w = [0.0] * dims                    # contact velocity, at rest at ignition
    else:
        c = [float(v) for v in init[0]]     # the jet arrives already trailing ...
        w = [float(v) for v in init[1]]     # ... and already moving
    contact = [tuple(c)]
    for k in range(n - 1):
        dt = times[k + 1] - times[k]
        p0, p1 = points[k], points[k + 1]
        if dt <= 0.0:                       # no time passes -> contact cannot move
            contact.append(tuple(c))
            continue
        tau = relax_times[k]
        if tau <= 1e-12:                    # no lag here -> contact rides the nozzle
            for d in range(dims):
                c[d] = float(p1[d])
                w[d] = (p1[d] - p0[d]) / dt
            contact.append(tuple(c))
            continue
        omega = 2.0 * z / tau              # pins steady trail to v*tau (= lag_dist)
        sigma = z * omega                  # = decay rate of the transient
        # CH, SO: e^{-sigma dt} * {cosh, sinh(mu dt)/mu} with mu^2 = sigma^2-omega^2
        # -- the two scalars that build the exact transition matrix, each formed to
        # avoid overflow whatever dt is.
        if z > 1.0 + 1e-9:                  # overdamped: real poles -sigma +/- m
            m = math.sqrt(sigma * sigma - omega * omega)
            ep = math.exp((m - sigma) * dt)        # both exponents <= 0 -> bounded
            en = math.exp(-(m + sigma) * dt)
            ch, so = 0.5 * (ep + en), 0.5 * (ep - en) / m
        elif z < 1.0 - 1e-9:               # underdamped: the oscillatory whip
            wd = math.sqrt(omega * omega - sigma * sigma)
            e = math.exp(-sigma * dt) if sigma * dt < 700.0 else 0.0
            ch, so = e * math.cos(wd * dt), e * math.sin(wd * dt) / wd
        else:                              # critically damped (zeta == 1)
            e = math.exp(-sigma * dt) if sigma * dt < 700.0 else 0.0
            ch, so = e, e * dt
        phi11 = ch + sigma * so            # transition matrix [[phi11, phi12],
        phi12 = so                         #                    [phi21, phi22]]
        phi21 = -omega * omega * so        # acting on the (error, error-rate) state
        phi22 = ch - sigma * so
        for d in range(dims):
            vn = (p1[d] - p0[d]) / dt      # nozzle velocity over this interval
            xp = -vn * tau                 # particular soln: steady trailing error
            y0 = (c[d] - p0[d]) - xp       # homogeneous error at interval start
            y0p = w[d] - vn                # ... and its rate
            c[d] = p1[d] + xp + phi11 * y0 + phi12 * y0p
            w[d] = vn + phi21 * y0 + phi22 * y0p
        contact.append(tuple(c))
    return (contact, (list(c), list(w))) if with_state else contact

# The ray tracker's default internal subdivision. The ray update is only FIRST-order
# accurate in dt, so at 1 (Hrynevich's own "one step per point") the answer inherits
# whatever output grid the caller happens to use. Measured on real generator toolpaths
# (a 20 mm square and a 20 mm decagon, 10 layers each) against a converged sub = 256
# solve, and on the corner CUTBACK -- the metric the tracker was selected on:

#     sub      max dev      rms dev     cutback shift     preview relay (30x30/0.5/x10)
#       1   42-64 um    13-18 um          26.4 um            0.26 s
#       6    9-12 um   3.9-4.3 um          4.5 um            0.42 s
#      12   4.5-6.0 um 2.0-2.1 um          2.2 um            0.60 s
#      16   3.4-4.4 um 1.5-1.6 um          1.6 um            0.74 s
#      32   1.6-2.1 um 0.7-0.8 um          0.8 um            1.22 s

# The error is clean first-order (halving sub halves it), so 16 is a CHOICE OF WHERE
# TO STOP, not a convergence claim: it is where RMS drops below a tenth of a nominal
# 20 um fiber and the cutback stops moving at the 2 um level, against the 56-759 um
# the swap itself moves that cutback by. It is also 16 x `fill_per_tau` = 96 steps
# per relaxation time, the grid the paper repo's grid_check.py used as its converged
# reference. Cost: +0.5 s on the preview relay against its 3.0 s budget
# (`test_gui_preview.app_latency_budget`), +0.4 s on a 20 mm x10 export, +52 ms per
# live-adjust knob edit. 32 buys 1.8 um of RMS and puts the preview on its budget line.
RAY_SUBSTEPS = 16
# The same calibration read the other way round. The line above fixes sub-steps PER
# OUTPUT SAMPLE, which only equals the 96-steps-per-relaxation-time grid it was
# calibrated against while the output grid is the default `fill_per_tau` of 6. Refine
# the OUTPUT and the integration is refined with it at 16x the cost, though it had
# already converged -- which is what made the adaptive corner grid expensive.
# `steps_per_tau` holds the calibrated quantity instead and lets the sub-step count
# follow dt/tau, so the inner work stays put however finely the output is sampled.
# Opt-in: a caller that does not pass it gets the fixed count, byte for byte.
RAY_STEPS_PER_TAU = 96.0


def integrate_contact_path_ray(points, times, relax_times, *, lag_ceiling,
                               lag_floor=0.0, saturation_exp,
                               substeps=None, steps_per_tau=None,
                               init=None, with_state=False):
    """Trace the fiber CONTACT point by Hrynevich 2020's OWN algorithm -- the ray /
    V_JCP step (Sec. 2.1, Fig. 2, Eqs. (1)-(3), (5)) -- and NOT as a linear filter.
    This is the shipped placement rule; the two filters above are what it replaced.

    THE RULE: the jet's projection on the collector is tangent to the deposited fiber
    at the contact point, and the contact trails the nozzle by the stable lag of its
    own speed. A per-component linear filter matches only the steady trail, so the two
    rules can differ only in the TRANSIENT -- in CORNER ROUNDING: here the lag relaxes
    towards the stable lag of the RECEDING speed (Eq. (1)'s V_MA) and each step
    re-aims along the new ray. The measured case for the swap is in the guards.

    V_JCP IS THE LAG LAW INVERTED -- Eq. (5) is to Eq. (4) exactly that -- and this
    build's law is linear in the saturating factor, so the inverse is closed-form.
    There is no CTS argument: `relax_times` already sizes each interval's steady lag,
    carrying three seams this tracker must not discard (the accel-limited speed, the
    live `speed_scale`, the mandrel's along-track lag), so V_JCP is normalised through
    L_ss = tau * v_nozzle, in which CTS cancels identically -- checked against the
    paper repo's reference (`test_ray_matches_the_paper_repo_reference`). The speed
    factor K is carried across a NON-ADVANCING interval, making the settle tail work.

    First-order in dt, so unlike the two filters its answer moves with the grid; it is
    sub-stepped internally instead (`substeps`, None -> `RAY_SUBSTEPS`), leaving the
    OUTPUT sampling untouched. `lag_ceiling`/`lag_floor`/`saturation_exp` are the law's
    amp / L0 / b in mm; `init`/`with_state` are the continuous-jet seam, a 3-tuple so
    K0 survives a handover onto a stationary interval."""
    n = len(points)
    if n == 0:
        return ([], (list(init[0]), list(init[1]),
                     float(init[2]) if len(init) > 2 else 0.0)
                if init else ([], [], 0.0))\
            if with_state else []
    dims = len(points[0])
    amp = float(lag_ceiling)
    L0 = float(lag_floor)
    b = float(saturation_exp)
    if not (amp > L0) or not (b > 0.0):
        raise ValueError("integrate_contact_path_ray: need lag_ceiling > lag_floor "
                         "and saturation_exp > 0")
    span = amp - L0
    inv_b = -1.0 / b
    m_fixed = max(1, int(RAY_SUBSTEPS if substeps is None else substeps))
    per_tau = None if steps_per_tau is None else float(steps_per_tau)
    if init is None:
        c = [float(v) for v in points[0]]
        w = [0.0] * dims
        K = 0.0
    else:
        c = [float(v) for v in init[0]]
        w = [float(v) for v in init[1]]
        # the speed factor, inherited when the caller has one. A 2-tuple means "not
        # known": correct for a pinned handover (lag zero, nothing to run in) and
        # exact for any handover mid-motion, since the next moving interval recomputes
        # it -- but it would freeze a contact handed over onto a settle tail.
        K = float(init[2]) if len(init) > 2 else 0.0
    contact = [tuple(c)]                    # K = the interval speed factor, unscaled CTS
    for k in range(n - 1):
        dt = times[k + 1] - times[k]
        p0, p1 = points[k], points[k + 1]
        if dt <= 0.0:                       # no time passes -> contact cannot move
            contact.append(tuple(c))
            continue
        tau = relax_times[k]
        if tau <= 1e-12:                    # no lag here -> contact rides the nozzle
            for d in range(dims):
                w[d] = (p1[d] - c[d]) / dt
                c[d] = float(p1[d])
            contact.append(tuple(c))
            continue
        # This interval's steady lag, read back from the caller's tau so every
        # upstream seam (accel limit, speed scale, mandrel anisotropy) survives.
        v_noz = math.sqrt(sum((float(p1[d]) - float(p0[d])) ** 2
                              for d in range(dims))) / dt
        if v_noz > 0.0:
            L_ss = min(tau * v_noz, amp - span * 1e-12)
            K = v_noz * ((amp - L_ss) / span) ** (1.0 / b)
        # else: a stationary interval (the settle tail) -- keep the last K, and let
        # the ray run the contact in at V_JCP against a lag target of L0.
        if K <= 0.0:                        # nothing has moved yet: nothing to trail
            contact.append(tuple(c))
            continue
        q0 = list(c)
        # Sub-steps per OUTPUT interval. Fixed (the historical rule) unless the
        # caller asked for a steps-per-tau budget, in which case a finer output
        # interval needs proportionally fewer of them -- the integration grid is
        # then invariant to the output sampling, which is the whole point.
        m_sub = (m_fixed if per_tau is None
                 else max(1, min(m_fixed, int(math.ceil(per_tau * dt / tau)))))
        hdt = dt / m_sub
        if dims == 2:
            # Scalar 2-D inner loop -- BOTH production callers are 2-D
            # (`mew_deposition._lay_one`, `mew_live._relay_one` both track the
            # ground path and let z ride through), and this loop runs `substeps`
            # times per output sample, so the list allocations the general branch
            # below makes per sub-step dominated the cost. Same arithmetic, same
            # order of operations: `test_ray_2d_fast_path_matches_the_general_one`
            # holds them bit-identical, which is the only reason two branches are
            # allowed to exist here.
            x0, y0 = float(p0[0]), float(p0[1])
            dx, dy = float(p1[0]) - x0, float(p1[1]) - y0
            cx, cy = c[0], c[1]
            for j in range(m_sub):
                a0, a1 = j / m_sub, (j + 1) / m_sub
                n0x, n0y = x0 + a0 * dx, y0 + a0 * dy
                n1x, n1y = x0 + a1 * dx, y0 + a1 * dy
                ex, ey = n0x - cx, n0y - cy
                L_i = math.sqrt(ex * ex + ey * ey)
                rx, ry = n1x - cx, n1y - cy
                RL = math.sqrt(rx * rx + ry * ry)
                if RL <= 1e-15:
                    continue
                eps = ((RL - L_i) / hdt) / K
                Ls = L0 + span * (1.0 - eps ** -b) if eps > 1.0 else L0
                x = (amp - L_i) / span
                L_new = 0.0 if x <= 1e-12 else RL - K * x ** inv_b * hdt
                L_new = min(L_new, Ls) if L_i < Ls else max(L_new, Ls)
                L_new = min(max(L_new, 0.0), RL)
                u = L_new / RL
                cx, cy = n1x - u * rx, n1y - u * ry
            c = [cx, cy]
        else:
            for j in range(m_sub):
                a0, a1 = j / m_sub, (j + 1) / m_sub
                n0 = [float(p0[d]) + a0 * (float(p1[d]) - float(p0[d]))
                      for d in range(dims)]
                n1 = [float(p0[d]) + a1 * (float(p1[d]) - float(p0[d]))
                      for d in range(dims)]
                L_i = math.sqrt(sum((n0[d] - c[d]) ** 2 for d in range(dims)))
                ray = [n1[d] - c[d] for d in range(dims)]
                RL = math.sqrt(sum(r * r for r in ray))
                if RL <= 1e-15:
                    continue
                # Eq. (1)/(2): the target lag of the speed the nozzle moves AWAY at.
                eps = ((RL - L_i) / hdt) / K
                Ls = L0 + span * (1.0 - eps ** -b) if eps > 1.0 else L0
                # Eq. (3) via Eq. (5): advance the contact along the ray at V_JCP.
                x = (amp - L_i) / span
                L_new = 0.0 if x <= 1e-12 else RL - K * x ** inv_b * hdt
                L_new = min(L_new, Ls) if L_i < Ls else max(L_new, Ls)
                L_new = min(max(L_new, 0.0), RL)
                u = L_new / RL
                c = [n1[d] - u * ray[d] for d in range(dims)]
        for d in range(dims):
            w[d] = (c[d] - q0[d]) / dt
        contact.append(tuple(c))
    return (contact, (list(c), list(w), K)) if with_state else contact


# --------------------------------------------------------------------------- #
# Below-CTS sewing-machine kinematics -- the Bessel-J0 length closure
# --------------------------------------------------------------------------- #
# The coil is generated as a tangent-angle oscillation psi(t) = Psi * sin(phase)
# of the contact point about the local travel direction, laid at the constant
# landing speed CTS. The time-average of cos(psi) over a period is exactly the
# Bessel function J0(Psi), so choosing J0(Psi) = eps = v/CTS makes the deposited
# arc length per unit translation exactly CTS/v = 1/eps -- arc-length conservation
# (Brun et al. 2015; Morris et al. 2008). Psi=0 at eps=1 (straight, the forward-Hopf
# onset) and grows to the first zero of J0 (pure coiling) as eps -> 0. One waveform
# spans meander -> loops -> coils, and its 2nd harmonic reproduces the grounded 2:1
# longitudinal companion B = A^2 omega/(8 v) (Morris 2008; Welch et al. 2012).
_J0_FIRST_ZERO = 2.404825557695773   # first positive root of J0; J0 falls 1 -> 0
                                     # monotonically across [0, this]


def bessel_j0(x: float) -> float:
    """Bessel function J0(x) via its power series. Exact to ~1e-16 over the only
    range we use it -- the monotone arm [0, 2.4048] where J0 decreases from 1 to 0
    (the series converges in a handful of terms there). Pure Python so the build
    keeps shipping only numpy + vtk (no SciPy dependency)."""
    xx = 0.25 * float(x) * float(x)
    term = 1.0
    s = 1.0
    for m in range(1, 60):
        term *= -xx / (m * m)
        s += term
        if abs(term) <= 1e-17 * (abs(s) + 1.0):
            break
    return s


def invert_j0_unit(eps: float) -> float:
    """Smallest psi in [0, 2.4048] with J0(psi) = eps, for eps in [0, 1] -- the
    coil's tangent-oscillation amplitude (see the note above). J0 is monotone there,
    so a bisection is exact and robust. eps>=1 -> 0 (straight); eps<=0 -> the first
    zero (pure coiling)."""
    if eps >= 1.0:
        return 0.0
    if eps <= 0.0:
        return _J0_FIRST_ZERO
    lo, hi = 0.0, _J0_FIRST_ZERO
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bessel_j0(mid) > eps:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def bessel_jn(n: int, x: float) -> float:
    """Bessel function J_n(x), n >= 0, via its power series
    J_n(x) = sum_m (-1)^m / (m!(m+n)!) (x/2)^(2m+n). Exact to ~1e-15 over the range
    the coil needs it (0 <= x <= 2.405, n <= ~8) and overflow-free there -- unlike a
    downward recurrence, which blows up for small x. Pure Python, like bessel_j0
    (which it returns for n == 0), so the build keeps shipping only numpy + vtk."""
    n = int(n)
    if n == 0:
        return bessel_j0(x)
    xh = 0.5 * float(x)
    term = 1.0
    for k in range(1, n + 1):          # term <- (x/2)^n / n!  (the m=0 term)
        term *= xh / k
    xx = xh * xh                       # (x/2)^2
    s = term
    for m in range(1, 80):
        term *= -xx / (m * (m + n))
        s += term
        if abs(term) <= 1e-18 * (abs(s) + 1.0):
            break
    return s


# How many harmonics of the coil waveform to keep. The k-th term carries J_k(Psi),
# and J_k(2.405) < 1e-3 for k >= 7, so four harmonics (perp 1,3,5,7; along 2,4,6)
# reproduce the coil -- and conserve its arc length -- to ~1e-3 across the whole
# eps range: effectively the exact sewing-machine waveform (verified vs the integral).
_COIL_N_HARMONICS = 4


def coil_offset_series(psi: float, n_harmonics: int = _COIL_N_HARMONICS):
    """Fourier coefficients of the steady below-CTS coil's OFFSET about the
    travelling contact, in units of the coil radius R = CTS/omega. From the
    Jacobi-Anger expansion of the tangent-angle oscillation psi*cos(phase):

        perp_off  = R * sum over odd  k of c_k * sin(k*phase)   (the meander)
        along_off = R * sum over even k of c_k * sin(k*phase)   (the 2:1 companion)

    with c_k = -2(-1)^m J_k(psi)/k for perp (k=2m+1) and +2(-1)^m J_k(psi)/k for
    along (k=2m). The dominant terms are the documented perp ~ 2 J1(psi) R sin(phase)
    and along ~ -J2(psi) R sin(2 phase). EVERY coefficient carries a factor J_k(psi),
    so the entire offset -> 0 as psi -> 0 (eps -> 1): the coil joins the straight
    fiber continuously AND leaves no residual when the jet stops coiling -- which is
    why this closed form is used instead of integrating the coil velocity (that
    integral freezes at a non-zero residual when coiling ends). Returns
    (perp_terms, along_terms), each a list of (k, c). Pure function of psi."""
    perp = [(2 * m + 1, -2.0 * (-1) ** m * bessel_jn(2 * m + 1, psi) / (2 * m + 1))
            for m in range(n_harmonics)]
    along = [(2 * m, 2.0 * (-1) ** m * bessel_jn(2 * m, psi) / (2 * m))
             for m in range(1, n_harmonics)]
    return perp, along


__all__ = [
    "Material", "Nozzle", "Calibration",
    "ProcessParams", "Regime", "JetState", "MEWModel",
    "integrate_contact_path", "integrate_contact_path_2nd",
    "integrate_contact_path_ray", "RAY_SUBSTEPS",
    "bessel_j0", "bessel_jn", "invert_j0_unit", "coil_offset_series",
]
