#!/usr/bin/env python3
"""mew_calibration.py -- the per-rig CALIBRATION PROFILE seam.

`mew_physics.Calibration` (plus `Material` and `Nozzle`) is the single place every
tunable lives; this module makes a rig's calibration a user-editable, shareable
object rather than a dataclass default:

  * a flat PROFILE DICT (string-valued, JSON-serialisable) that the GUI's
    Calibration tab edits, its presets store, and the workers read;
  * a FIELD REGISTRY (`CALIBRATION_FIELDS`) driving the GUI -- one entry per
    exposed knob with its label, unit, section, required/advanced group and a
    one-line description. Defaults are pulled LIVE from the dataclasses, so the
    registry cannot drift from the laws it documents;
  * `models_from_profile` / `build_model`, turning a profile back into the
    `(Material, Nozzle, Calibration)` triple or a ready `MEWModel`. An empty or
    absent profile reproduces the hardcoded defaults BYTE-FOR-BYTE, so a run with
    no profile is unchanged;
  * the GUIDED-FIT ESTIMATORS -- pure functions inverting one measured print into
    one calibration value. They reuse the real `mew_physics` laws rather than
    reimplementing them, so a fit is always consistent with what the model then
    predicts.

Design intent mirrors the rest of the physics build: expose every tunable, keep the
REQUIRED set tiny. A new rig measures a handful of anchors and leaves the long
`advanced` tail at its defensible default until a sweep justifies moving it.

DELIBERATELY NOT EXPOSED: the dormant viscous-creep sag block (`sag_viscous_creep`
+ `sag_creep_*`), which is unvalidated future work reachable only from code or the
CLI `--physical-sag-viscous` flag; re-exposing it here re-opens a door that was
shut on purpose. Gap / working distance is absent for a different reason -- it is
the printhead elevation (`sim_standoff`), a single source of truth passed to the
workers as `--standoff`, never duplicated here.

See the `physics-build` skill.
"""
from __future__ import annotations

import dataclasses
import json
import math

from mew_physics import (
    Material, Nozzle, Calibration, MEWModel,
    GAS_CONSTANT, CELSIUS_TO_KELVIN,
)

# Every profile key is namespaced so the flat dict (and the GUI persist map /
# preset store) never collides with the scaffold or machine keys.
KEY_PREFIX = "cal."

_TARGETS = {"material": Material, "nozzle": Nozzle, "calibration": Calibration}


# --------------------------------------------------------------------------- #
# The field registry: one entry per exposed knob.
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class FieldSpec:
    """One exposed calibration knob. `target`/`attr` say which dataclass field it
    drives; `default` is read LIVE from that dataclass (never hand-copied). The
    rest is GUI metadata. `optional` marks a field whose dataclass default is
    None (a reference value only needed once its exponent is fitted): blank in the
    GUI means "unset" (None).

    `gate_label` marks an optional field the GUI puts behind its own TICKBOX, whose
    text this is. Unticked -> the entry is greyed AND reads as blank (unset), so the
    model gets the dataclass default -- "inherit", the unsplit behaviour -- however
    the entry was last filled in. That sentinel is folded in GUI-side (the
    `_sag_reset_arg` idiom) so a remembered value can never leak back once the user
    unticks. The tickbox is a GUI-only var keyed `gate_key`: no dataclass field
    stands behind it and `models_from_profile` skips it as an unknown key. Only
    meaningful together with `optional` -- blank has to mean something.

    `shown_as` / `scale` make a field SHOWN, typed and saved as a different
    quantity than the dataclass holds: the needle is a researcher's inner
    DIAMETER (`shown_as="inner_diameter"`, `scale=2.0`) while the laws keep
    the radius `Nozzle.inner_radius`. The key, the default and every stored
    value are the shown quantity; `models_from_profile` divides by `scale`
    on the way in, the one place the two meet."""
    attr: str                 # dataclass field name, e.g. "cts_ref"
    target: str               # "material" | "nozzle" | "calibration"
    label: str                # GUI label
    unit: str                 # shown after the label, e.g. "mm/s" ("" for none)
    section: str              # grouping header
    group: str                # "required" | "advanced"
    desc: str                 # one-line help, distilled from the docstrings/map
    optional: bool = False    # dataclass default is None -> blank means None
    gate_label: str = ""      # non-empty -> render behind a tickbox with this text
    shown_as: str = ""        # the shown quantity's name, when not `attr`
    scale: float = 1.0        # shown value = dataclass value * scale

    @property
    def key(self) -> str:
        return KEY_PREFIX + ("noz_" if self.target == "nozzle"
                             else "mat_" if self.target == "material"
                             else "") + (self.shown_as or self.attr)

    @property
    def gate_key(self) -> str:
        """Persist key of this field's tickbox. GUI-only -- deliberately NOT a
        dataclass field, so it flows through the profile untouched and unread."""
        return self.key + ".on"

    @property
    def default(self):
        """The current dataclass default for this field (live, never copied),
        as shown (times `scale`)."""
        for fld in dataclasses.fields(_TARGETS[self.target]):
            if fld.name == self.attr:
                if self.scale != 1.0 and fld.default is not None:
                    return fld.default * self.scale
                return fld.default
        raise KeyError(f"{self.target}.{self.attr} is not a dataclass field")


# Order = GUI render order. Sections group related knobs; `required` is the short
# must-measure-per-rig set, `advanced` the long off-by-default / fitted-constant
# tail. Descriptions are one-liners -- the deep story stays in the Calibration
# docstrings and the PHYSICS_DEPENDENCY_GRAPH.md dossiers.
CALIBRATION_FIELDS = [
    # -- Nozzle geometry (measured, not fitted) ----------------------------- #
    FieldSpec("inner_radius", "nozzle", "Needle inner diameter", "mm",
              "Nozzle (measured)", "required",
              "Inner diameter of the needle. Its half, the radius r, enters "
              "flow as r^4 and sets the Taylor-cone base, so measure yours "
              "(21 G regular wall = 0.514).",
              shown_as="inner_diameter", scale=2.0),
    FieldSpec("length", "nozzle", "Needle length", "mm",
              "Nozzle (measured)", "required",
              "Length of the narrow cannula the melt is pushed through (the metal "
              "nozzle, NOT the syringe barrel). Flow ~ 1/L; trades off with the "
              "viscosity anchor."),
    # -- Material / melt ---------------------------------------------------- #
    FieldSpec("temperature_ref", "material", "Anchor temperature", "°C",
              "Material (melt)", "required",
              "Temperature of your magnitude-anchor run and the Arrhenius pivot. "
              "Set it to the run where you measured the anchor fiber/viscosity."),
    FieldSpec("viscosity_ref", "material", "Melt viscosity at anchor T", "Pa·s",
              "Material (melt)", "required",
              "The single calibrated magnitude anchor. Fit it (Guided fit) so the "
              "model reproduces one measured fiber diameter; only the lumped "
              "pi*r^4/8*eta*L is identifiable."),
    FieldSpec("activation_energy", "material", "Flow activation energy Ea",
              "J/mol", "Material (melt)", "required",
              "The SOLE driver of temperature sensitivity. Fit to a temperature "
              "sweep on YOUR polymer (default 31300 = neat PCL)."),
    FieldSpec("density", "material", "Melt density", "g/mm³",
              "Material (melt)", "advanced",
              "Used only by the sag self-weight, never by flow (Qv is volumetric). "
              "Rarely needs changing."),
    # -- Critical translation speed (CTS) ----------------------------------- #
    FieldSpec("cts_ref", "calibration", "CTS", "mm/s",
              "Critical translation speed", "required",
              "Measured onset speed: the lowest still-straight print speed. The "
              "single most important per-rig anchor (an upper bound until an onset "
              "sweep pins it)."),
    FieldSpec("cts_voltage_ref", "calibration", "CTS reference voltage", "kV",
              "Critical translation speed", "advanced",
              "Voltage at the CTS anchor run. Only used once the CTS-voltage "
              "exponent is non-zero."),
    FieldSpec("cts_gap_ref", "calibration", "CTS reference gap", "mm",
              "Critical translation speed", "advanced",
              "Gap at the CTS anchor run. Only used once the CTS-gap exponent is "
              "non-zero."),
    FieldSpec("cts_voltage_exp", "calibration", "CTS-voltage exponent", "",
              "Critical translation speed", "advanced",
              "Off (0) by default. Sign is known positive (stronger draw -> higher "
              "CTS); magnitude unquantified. Fit if you sweep voltage."),
    FieldSpec("cts_gap_exp", "calibration", "CTS-gap exponent", "",
              "Critical translation speed", "advanced",
              "Off (0). Field (V/H) and flight length roughly cancel over "
              "4.0-4.5 mm; untested beyond."),
    FieldSpec("cts_flow_ref", "calibration", "CTS reference flow", "mm³/s",
              "Critical translation speed", "advanced",
              "Flow Q at the anchor run. REQUIRED once the CTS-flow exponent is "
              "set. Blank = unset.", optional=True),
    FieldSpec("cts_flow_exp", "calibration", "CTS-flow exponent", "",
              "Critical translation speed", "advanced",
              "Off (0). Flow sets diameter, not CTS; the 4Q/pi*d^2 identity bounds "
              "it in [0, 1]."),
    FieldSpec("cts_visc_ref", "calibration", "CTS reference viscosity", "Pa·s",
              "Critical translation speed", "advanced",
              "Viscosity eta at the anchor run. REQUIRED once the CTS-viscosity "
              "exponent is set. Blank = unset.", optional=True),
    FieldSpec("cts_visc_exp", "calibration", "CTS-viscosity exponent", "",
              "Critical translation speed", "advanced",
              "Off (0). The model's known weak link -- no quantified CTS-vs-T law."),
    # -- Jet lag (the amplitude, its gap, and 2 shape constants) ------------ #
    FieldSpec("lag_amplitude", "calibration", "Lag amplitude A", "",
              "Jet lag", "advanced",
              "Eq.4's gap-normalised amplitude: lag = H*A*(1 - eps^-b), so the "
              "lag saturates at A*H. 2.8629 = Hrynevich 2020's own 10.02/3.5, a "
              "prior rather than a measurement -- it lands on Neuhaus's "
              "circumferential mandrel radius and misses her axial one by "
              "1.3-1.4x. THE ONE LAG CONSTANT TO MOVE FOR YOUR RIG: fit it from "
              "your own circles or mandrel and leave the exponent alone, since "
              "fitting both to one dataset counts the same freedom twice."),
    FieldSpec("lag_amplitude_gap", "calibration", "Gap A was fitted at", "mm",
              "Jet lag", "advanced",
              "A RECORD, not a law input -- nothing reads it and the lag does not "
              "change with it. A fitted A has no meaning without it: the law "
              "carries the gap as H^1 and the literature disagrees on that "
              "exponent, so an A fitted at one gap is an explicit extrapolation "
              "at any other. 3.5 mm is where the shipped A is a definition (A "
              "and b were fitted to Hrynevich's curve there). Set it to the gap "
              "the calibration print actually ran at; the circle protocol writes "
              "it for you."),
    FieldSpec("lag_angle_cts_deg", "calibration", "Lag floor angle at CTS", "deg",
              "Jet lag", "advanced",
              "Chord-angle floor, pinned to 0 so lag -> 0 at CTS. Not fitted; "
              "reset it only if a rig sweep resolves a lag at CTS."),
    FieldSpec("lag_saturation_exp", "calibration", "Lag saturation exponent b", "",
              "Jet lag", "advanced",
              "Power-law exponent of the approach to the ceiling, "
              "lag = H*A*(1 - eps^-b). Hrynevich Eq.4's own exponent. It is "
              "PINNED while the amplitude is fitted: b and the believed CTS are "
              "near-degenerate on every observable the calibration prints "
              "measure, so a free b absorbs a CTS error and lowers the residual "
              "while moving A badly. Re-fit it only from a lag sweep of your "
              "own, and then re-fit A with it."),
    FieldSpec("stable_window", "calibration", "Stable window", "x CTS",
              "Jet lag", "advanced",
              "Upper edge of the usable window (in units of CTS). Labels the "
              "OVERSTRETCH regime only; never changes geometry."),
    # -- Electrostatic field ------------------------------------------------ #
    FieldSpec("field_tip_strength", "calibration", "Tip field strength", "kV.mm",
              "Electrostatic field", "advanced",
              "Off (0) -> the bare uniform V/H gap field. A tip field-enhancement "
              "image-charge hook; turn on only with tip-geometry data."),
    # -- Contact dynamics --------------------------------------------------- #
    FieldSpec("contact_damping_zeta", "calibration", "Contact damping zeta", "",
              "Contact dynamics", "advanced",
              "RETIRED and has NO effect: it damped the second-order "
              "contact filter that Hrynevich's ray / V_JCP placement step replaced, "
              "and that step has no damping knob. Corner rounding is now entirely "
              "the lag's. Kept so old profiles still load; ignore it."),
    # -- Bridge sag (suspended-span droop) ---------------------------------- #
    FieldSpec("sag_coeff", "calibration", "Sag magnitude coefficient", "1/mm",
              "Bridge sag", "required",
              "ONE rig calibration for suspended-span droop depth (a calibrated "
              "droop-depth setting, not a physical simulation): c in depth = "
              "c*span^2. Scaled by the Sag multiplier (the Sag multiplier is on "
              "the Scaffold designer page). Used only with Physical sag on."),
    FieldSpec("sag_span_exp", "calibration", "Sag span exponent", "",
              "Bridge sag", "advanced",
              "Span^2 = the viscous-catenary regime: a hot fiber bridging a pore "
              "sags as a stretching-dominated catenary once its droop exceeds the "
              "fiber thickness (Teichman & Mahadevan 2003). Grounds the exponent, "
              "not the magnitude."),
    FieldSpec("sag_diam_exp", "calibration", "Sag diameter exponent", "",
              "Bridge sag", "advanced",
              "Off (0): assert no diameter dependence (unidentifiable on uniform "
              "prints). Sign is regime-dependent; +2 / -2 selectable. Fit on "
              "mixed-diameter scaffolds."),
    # sag_diam_ref is NOT a GUI field: it is inert at the default diameter exponent 0
    # (folded into sag_coeff = c) and only re-enters as the pivot of the opt-in diameter
    # hook (d/sag_diam_ref)^q. Kept as an internal dataclass constant (the calibration
    # fiber, 11.71 um), reachable via the Calibration seam / CLI. See DORMANT_CALIBRATION_ATTRS.
    FieldSpec("sag_thermal_visc_exp", "calibration", "Sag-temperature exponent",
              "", "Bridge sag", "advanced",
              "Off (0). Nguyen 2019: temperature dominates sag (hotter -> more), "
              "sign < 0; magnitude unvalidated."),
    FieldSpec("sag_temp_ref", "calibration", "Sag anchor temperature", "°C",
              "Bridge sag", "advanced",
              "Temperature at the sag anchor run. Blank -> the material anchor "
              "temperature.", optional=True),
    # -- Fusion ------------------------------------------------------------- #
    FieldSpec("fusion_overlap", "calibration", "Junction sink φ", "",
              "Fusion", "required",
              "Per-crossing centre sink as a fraction of d (0 = geometric stack), "
              "where fibers cross briefly at a point. Calibrate to your crossover "
              "peak heights. Used only with Fusion on."),
    FieldSpec("fusion_wall_overlap", "calibration", "Straight-span sink φ", "",
              "Fusion", "required",
              "Centre sink as a fraction of d where fibers stack COLLINEARLY for a "
              "long run -- a wall, or a fiber sagging into a valley onto the "
              "parallel layer below -- instead of crossing at a point. Unticked: "
              "walls fuse by the junction φ (one magnitude everywhere).",
              optional=True,
              gate_label="Different fusion magnitude for straight spans"),
    FieldSpec("fusion_diam_exp", "calibration", "Fusion diameter exponent", "",
              "Fusion", "advanced",
              "Off (0): φ is a pure fraction of d (diameter-independent height "
              "compaction; Lamb 2024 supports 0 for height)."),
    FieldSpec("fusion_diam_ref", "calibration", "Fusion anchor diameter", "mm",
              "Fusion", "advanced",
              "Anchor diameter; inert while the fusion diameter exponent is 0."),
    FieldSpec("fusion_thermal_visc_exp", "calibration",
              "Fusion-temperature exponent", "", "Fusion", "advanced",
              "Off (0). The MELT (nozzle) temperature via viscosity, NOT the "
              "collector temperature (the unheated bed is fixed by hardware)."),
    FieldSpec("fusion_temp_ref", "calibration", "Fusion anchor temperature",
              "°C", "Fusion", "advanced",
              "Temperature at the fusion anchor run. Blank -> the material anchor "
              "temperature.", optional=True),
    FieldSpec("fusion_max_overlap", "calibration", "Fusion overlap clamp", "",
              "Fusion", "advanced",
              "Hard cap on the effective φ so the upper fiber's centre never "
              "fully merges into the one below. Applies to both φ."),
    # -- Taylor cone (player jet render only) ------------------------------- #
    FieldSpec("cone_knee_frac", "calibration", "Cone knee height (fraction of gap)",
              "", "Taylor cone (render only)", "advanced",
              "Player jet render only -- never touches the deposited fiber. The height "
              "(as a fraction of the nozzle-collector gap) at which the drawn jet has "
              "necked to half the cone base width -- i.e. how TALL the Taylor cone "
              "looks. ~0.20 from Ashour & Xu 2022. Ratio-independent (see mew_physics "
              "CONE-KNEE-ASHOUR)."),
]

_BY_KEY = {fs.key: fs for fs in CALIBRATION_FIELDS}

# Section render order (first appearance in CALIBRATION_FIELDS).
SECTIONS = list(dict.fromkeys(fs.section for fs in CALIBRATION_FIELDS))


# --------------------------------------------------------------------------- #
# Drift guard helpers (also used by the tests).
# --------------------------------------------------------------------------- #
# Calibration fields deliberately NOT exposed (dormant viscous-creep future work,
# reachable only from code / the --physical-sag-viscous CLI flag).
DORMANT_CALIBRATION_ATTRS = frozenset({
    # The dormant viscous-creep block (future-work sag law; CLI/seam only):
    "sag_viscous_creep", "sag_creep_coeff", "sag_creep_span_ref",
    "sag_creep_span_exp", "sag_creep_diam_exp",
    # Below-CTS coiling anchors: coiling was removed from the Physics
    # tab (no UI toggle, never emitted to the workers), so these are no longer
    # user-calibratable. The engine fields remain for the dormant --coil code path.
    "coil_radius_ref", "coil_freq_eps_exp",
    # Internal anchor for the opt-in diameter hook: inert at the default sag_diam_exp=0
    # (folded into sag_coeff), fixed at the calibration fiber, not a user knob:
    "sag_diam_ref",
    # The wall<->junction CLASSIFICATION band. Not a fitted law and not
    # rig-specific: it spans the empty gap between the two measured contact-angle
    # populations (collinear contacts spike at 0-2 deg; the smallest crossing any standard
    # pore shape makes is 36 deg). A seam constant, exposed to code, not a user knob:
    "fusion_wall_angle_lo", "fusion_wall_angle_hi",
    # The mandrel PLACEMENT model (mew_mandrel, Neuhaus 2025 Eqs 1-5).
    # Not dormant -- live and off by default -- but deliberately not on the
    # PHYSICS tab: they describe a cylindrical collector, so they belong on the
    # Mandrel parameter page beside diameter, winding angle and strategy. If
    # they get fields there, MOVE THEM OUT OF THIS SET -- leaving them here is what
    # would make them silently
    # un-calibratable, which is the exact failure this guard exists to catch.
    "mandrel_lag_axial", "mandrel_lag_circ", "mandrel_support_mm",
    "mandrel_support_frac", "mandrel_support_speed_coeff",
    # ...and the two that make a mandrel calibration MANDREL-ONLY:
    # the speed the pair was measured at, and the CTS measured on the mandrel.
    # Same home as the pair -- mandrel vars folded into the profile for a
    # 4-axis run (gui_mandrel `_MANDREL_CAL_VARS`), shown on the Physics page's
    # Jet lag card -- and the same reason they are not flat registry fields.
    "mandrel_lag_speed", "mandrel_cts",
    # The per-layer REGISTRATION hook (MANDREL_LAG_DERIVATION.md §12.8). Unlike the
    # five above this one is NOT waiting for its parameter page: it is a
    # modelling-composition switch, not a rig constant, so it has no place among
    # knobs a user measures. Reachable from code (the
    # `ContactPlacement` constructor) and from the `--mandrel-per-layer` CLI
    # flag, exactly as `sag_viscous_creep` is. Leave it here when the mandrel
    # knobs move out.
    "mandrel_per_layer_registration",
    # The USER-REPLACED LAWS (mew_lawlang). Live, user-facing and
    # user-editable -- so excluded here for the opposite reason to everything
    # above: not "no GUI field", but "not a FLOAT". They are a law expression and
    # a bag of the user's own named coefficients, and CALIBRATION_FIELDS is a
    # flat table of floats that `lite/lite_params.py` hand-mirrors key-for-key.
    # They travel the same profile dict under LAW_SOURCE_KEY / LAW_CONSTS_KEY,
    # which `models_from_profile` handles explicitly, so nothing about them is
    # unreachable. Do NOT "fix" this by inventing a dict-valued FieldSpec: that
    # fires the Lite parity guard by design and drags Lite's Physics form along.
    "law_source", "law_consts",
})


def fmt_value(value) -> str:
    """Format a dataclass default for a string-valued profile / GUI field: None
    (an unset optional reference) -> blank; a float -> a compact decimal.

    `%g` carries 6 significant digits, which is comfortable for every knob a
    human types -- but the profile store is not only a display format, it is what
    `build_model` feeds the workers, so a constant that needs more digits than
    that would be SILENTLY QUANTISED on every round trip. That is not
    hypothetical: the retired `lag_ceiling_angle_deg = 70.745620335` came out as
    70.7456, moving Hrynevich Eq.4's amplitude by 1.1e-6 and breaking
    a profile round trip. That field is gone -- the amplitude A is stored directly now, so
    six digits would cost 1e-7 rather than 1e-6 -- but the rule it bought stands
    for every other constant. So `%g` is used only when it round-trips exactly, and `repr`
    -- the shortest string that reproduces the float -- otherwise. Ordinary
    fields are unaffected; nothing gets longer that did not have to."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        compact = f"{value:g}"
        return compact if float(compact) == value else repr(value)
    return str(value)


def default_profile() -> dict:
    """The factory profile: every field at its live dataclass default, as the
    string values the GUI / preset store hold. `models_from_profile` of this
    reproduces the default Material / Nozzle / Calibration exactly."""
    return {fs.key: fmt_value(fs.default) for fs in CALIBRATION_FIELDS}


def grid_labels() -> dict:
    """key -> short heading for the calibration preset comparison grid (mirrors
    gui_presets.MACHINE_PRESET_COL_LABELS, but derived from the registry so it
    never drifts)."""
    out = {}
    for fs in CALIBRATION_FIELDS:
        out[fs.key] = f"{fs.label} ({fs.unit})" if fs.unit else fs.label
        if fs.gate_label:
            out[fs.gate_key] = fs.gate_label
    # the two law keys are not FieldSpecs but they ARE persisted, so the preset
    # comparison grid needs headings for them or their columns read as raw keys
    out[LAW_SOURCE_KEY] = "Edited laws"
    out[LAW_CONSTS_KEY] = "Law coefficients"
    out[MEASURED_KEY] = "Measured by Calibrate"
    return out


# --------------------------------------------------------------------------- #
# profile  <->  models
# --------------------------------------------------------------------------- #
_SKIP = object()


def _coerce(raw, fs: FieldSpec):
    """A raw profile value (usually a string) -> a float to set on the dataclass,
    or `_SKIP` to leave the field at its default. Blank always skips (so an unset
    optional stays None and an untouched required field stays at its default); a
    non-numeric value skips rather than raising, so one bad field never breaks a
    whole profile load. "nan", "inf" and "1e400" parse as floats but are no
    more a value than "abc" is, so they skip too."""
    if raw is None:
        return _SKIP
    s = str(raw).strip()
    if s == "":
        return _SKIP
    try:
        val = float(s)
    except (TypeError, ValueError):
        return _SKIP
    return val if math.isfinite(val) else _SKIP


# -- the two USER-LAW keys ------------------------------------------------- #
# A user-replaced law is a STRING, not a float, so it cannot be a `FieldSpec`:
# CALIBRATION_FIELDS is a flat float table, `lite/lite_params.py` hand-mirrors
# it key-for-key. These ride the
# same profile dict under their own two keys instead, JSON-encoded so one profile
# entry carries every law at once and the GUI can hold each as one StringVar --
# which is what puts them in the persist map, in a saved preset and in the
# worker's profile file with no further plumbing.
#
# Both are OPTIONAL and both default to {}. A profile that has never seen a law
# editor loads exactly as it would without one.
LAW_SOURCE_KEY = KEY_PREFIX + "law_source"
LAW_CONSTS_KEY = KEY_PREFIX + "law_consts"

#: Which Physics values came from the user's own Calibrate run. One JSON object,
#: key -> the text Apply wrote ({"cal.cts_ref": "8.25"}), held in one StringVar so
#: it rides a physics preset and the session exactly as the law keys do. A value
#: is "measured" while its field still reads that text, and "default" otherwise:
#: typing over it, loading a preset or Reset to defaults all make it a default
#: again without anything having to clear the record. It changes no physics, so
#: `models_from_profile` skips it as an unknown key.
MEASURED_KEY = KEY_PREFIX + "measured"


def _law_dict(raw, values_are_numbers=False):
    """Decode one of the two law keys. Anything unreadable -> {}, on the same
    principle as `_coerce`: one bad entry must not stop a profile loading, and a
    law that fails to decode leaves the SHIPPED law running, which is the safe
    direction to fail in."""
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
    else:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, val in data.items():
        if values_are_numbers:
            if not isinstance(val, dict):
                continue
            consts = {}
            for nm, num in val.items():
                try:
                    consts[str(nm)] = float(num)
                except (TypeError, ValueError):
                    continue
            if consts:
                out[str(key)] = consts
        elif isinstance(val, str) and val.strip():
            out[str(key)] = val
    return out


#: The retired pre-A jet-lag keys. `lag_amplitude` used to be a dimensionless
#: per-rig multiplier k on top of `lag_ceiling_angle_deg`, whose tangent was the
#: amplitude A; the two were one degree of freedom written twice, so they were
#: collapsed into `lag_amplitude` = A itself.
AMPLITUDE_KEY = KEY_PREFIX + "lag_amplitude"
_LEGACY_CEILING_KEY = KEY_PREFIX + "lag_ceiling_angle_deg"
_LEGACY_AMPLITUDE_DEFAULT = 1.0


def migrate_lag_amplitude(profile: dict) -> dict:
    """Fold a pre-A profile's `k` and ceiling ANGLE into today's `lag_amplitude`.

    A profile written before the collapse carries `cal.lag_ceiling_angle_deg` (an
    angle whose tangent is A) and `cal.lag_amplitude` (a multiplier k on top of
    it), and their product k*tan(ceiling) is the amplitude the print actually ran
    at. Without this, the ceiling key would be ignored as unknown and the k would
    be read STRAIGHT INTO A -- a rig that had fitted k = 1.15 would silently drop
    from A = 3.29 to A = 1.15, a 2.9x error in every corner it prints.

    Only fires when the legacy angle key is present, so a profile written today is
    untouched (and so is one that carries A alone). Returns the profile unchanged
    in that case rather than a copy.

    IT IS NOT ENOUGH TO CALL THIS HERE. The GUI loads a physics preset straight
    into its Tk vars through the persist map, never through `models_from_profile`,
    so the preset path has to run it too -- `gui_presets._migrate_legacy_lag_amplitude`
    delegates to this function so there is one rule and not two. That was not
    hypothetical: the shipped `presets/physics/MEWron_0.2_22G.json` carries the pair,
    is the boot default, and without the preset-side call the app opened at A = 1
    against a shipped 2.8629 -- a 2.9x under-lag on every corner, with the Jet lag
    tile quietly reading "A 1"."""
    if _LEGACY_CEILING_KEY not in profile:
        return profile
    out = dict(profile)
    angle = out.pop(_LEGACY_CEILING_KEY)
    try:
        ceiling = math.tan(math.radians(float(str(angle).strip())))
    except (TypeError, ValueError):
        return out
    try:
        k = float(str(out.get(AMPLITUDE_KEY,
                              _LEGACY_AMPLITUDE_DEFAULT)).strip())
    except (TypeError, ValueError):
        k = _LEGACY_AMPLITUDE_DEFAULT
    out[AMPLITUDE_KEY] = repr(k * ceiling)
    return out


#: The needle's key before it was shown as an inner diameter: it held the
#: RADIUS. `migrate_inner_diameter` turns it into today's key, doubled.
_LEGACY_RADIUS_KEY = KEY_PREFIX + "noz_inner_radius"
INNER_DIAMETER_KEY = KEY_PREFIX + "noz_inner_diameter"


def migrate_inner_diameter(profile: dict) -> dict:
    """A profile or preset saved when the needle was a bore RADIUS
    (`cal.noz_inner_radius`) brought to today's `cal.noz_inner_diameter`,
    twice the value, so it loads as the same needle. A profile carrying both
    keeps its diameter. Returns the profile unchanged (not a copy) when it
    has no old key.

    Like `migrate_lag_amplitude`, the GUI's preset path runs this too
    (`gui_presets._migrate_preset_data`): presets reach the Tk vars through
    the persist map, never through `models_from_profile`."""
    if not isinstance(profile, dict) or _LEGACY_RADIUS_KEY not in profile:
        return profile
    out = dict(profile)
    raw = out.pop(_LEGACY_RADIUS_KEY)
    if INNER_DIAMETER_KEY in out:
        return out
    s = str(raw).strip() if raw is not None else ""
    try:
        out[INNER_DIAMETER_KEY] = fmt_value(2.0 * float(s)) if s else ""
    except (TypeError, ValueError):
        out[INNER_DIAMETER_KEY] = s
    return out


def migrate_profile(profile: dict) -> dict:
    """Every legacy key of a calibration profile brought up to date."""
    return migrate_inner_diameter(migrate_lag_amplitude(profile))


def models_from_profile(profile: dict):
    """A profile dict -> (Material, Nozzle, Calibration). Unknown keys are ignored
    and unparseable / blank values fall back to the dataclass default, so a
    partial or slightly-stale profile still loads. An empty profile returns the
    factory triple."""
    over = {"material": {}, "nozzle": {}, "calibration": {}}
    if isinstance(profile, dict):
        profile = migrate_profile(profile)
        for key, raw in profile.items():
            fs = _BY_KEY.get(key)
            if fs is None:
                continue
            val = _coerce(raw, fs)
            if val is not _SKIP:
                over[fs.target][fs.attr] = val / fs.scale
        # the two law keys are not FieldSpecs, so the loop above skipped them
        over["calibration"]["law_source"] = _law_dict(
            profile.get(LAW_SOURCE_KEY))
        over["calibration"]["law_consts"] = _law_dict(
            profile.get(LAW_CONSTS_KEY), values_are_numbers=True)
    material = dataclasses.replace(Material(), **over["material"])
    nozzle = dataclasses.replace(Nozzle(), **over["nozzle"])
    calibration = dataclasses.replace(Calibration(), **over["calibration"])
    return material, nozzle, calibration


def load_profile(path) -> dict:
    """Read a profile JSON written by the GUI; {} on any missing / corrupt file
    (so the worker falls back to the defaults rather than failing to launch)."""
    if not path:
        return {}
    try:
        with open(path, "r") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def build_model(profile_path, working_distance, viscous=False,
                per_layer_registration=False) -> MEWModel:
    """Construct the `MEWModel` the workers (gcode_player / mew_export) run from,
    sourcing Material / Nozzle / Calibration from the rig profile at
    `profile_path` (the gap stays the separate `--standoff` working distance).
    `profile_path` falsey / unreadable -> the factory defaults, byte-identical to
    the pre-profile `MEWModel(nozzle=Nozzle(), working_distance=...)`. `viscous`
    overlays the dormant viscous-creep sag toggle (the CLI
    `--physical-sag-viscous`), and `per_layer_registration` the mandrel
    per-layer registration hook (the CLI `--mandrel-per-layer`); those two are
    the calibration flags set outside the profile."""
    material, nozzle, calibration = models_from_profile(load_profile(profile_path))
    if viscous:
        calibration = dataclasses.replace(calibration, sag_viscous_creep=True)
    if per_layer_registration:
        calibration = dataclasses.replace(
            calibration, mandrel_per_layer_registration=True)
    return MEWModel(material=material, nozzle=nozzle,
                    working_distance=working_distance, calibration=calibration)


# --------------------------------------------------------------------------- #
# Guided-fit estimators: one measured print -> one calibration value.
#
# Each is a PURE function that reuses the real mew_physics laws (never a private
# re-derivation), so a fitted value is always consistent with what the model then
# predicts. They are unit-tested by round-trip (synthesise a measurement from a
# known model, fit, recover the constant).
# --------------------------------------------------------------------------- #
def _positive(value) -> bool:
    """True for a finite number above zero. Spelled out because the obvious
    `value <= 0` guard lets NaN through -- every comparison with NaN is False --
    and a NaN measurement then fits to a NaN constant with no error."""
    return math.isfinite(value) and value > 0.0


def _kelvin(temperature_c: float) -> float:
    """degC -> K, refusing what is not a temperature a melt can be at."""
    t = temperature_c + CELSIUS_TO_KELVIN
    if not _positive(t):
        raise ValueError("temperature must be a real temperature above absolute "
                         "zero (-273.15 degC), got %r" % temperature_c)
    return t


def cts_from_diameter(flow_rate_mm3_s: float, d_cts_mm: float) -> float:
    """The mass-conservation cross-check for the CTS onset: CTS = 4Q/(pi d_CTS^2)
    (the jet's landing speed = its touchdown diameter, conserving volume). A
    second, independent read on the onset speed from the measured fiber diameter
    AT onset -- compare it to the speed sweep's lowest-straight value."""
    if not _positive(d_cts_mm):
        raise ValueError("d_cts must be positive (mm)")
    return 4.0 * flow_rate_mm3_s / (math.pi * d_cts_mm * d_cts_mm)


def fit_viscosity_ref(model: MEWModel, measured_d_mm: float, pressure_pa: float,
                      temperature_c: float, speed_mm_s: float) -> float:
    """Back out the viscosity magnitude anchor `viscosity_ref` so the model
    reproduces ONE measured fiber diameter. Because d ~ sqrt(Q) ~ 1/sqrt(eta_ref)
    (mass conservation through Hagen-Poiseuille), the new anchor is the old one
    scaled by (d_model / d_measured)^2 -- which folds in r, L, Ea and T_ref via the
    model's own flow law, so no separate bookkeeping. One print at one (P, T, v)
    pins the whole magnitude.

    Refuses a pressure at or below zero: no melt flows there (`flow_rate` is 0),
    so the anchor would come out 0 -- and a zero viscosity anchor, once applied,
    divides by zero in every later fit."""
    if not _positive(measured_d_mm):
        raise ValueError("measured diameter must be positive (mm)")
    if not _positive(speed_mm_s):
        raise ValueError("speed must be positive (mm/s)")
    if not _positive(pressure_pa):
        raise ValueError("pressure must be positive -- no melt flows at or "
                         "below zero")
    _kelvin(temperature_c)
    q = model.flow_rate(pressure_pa, temperature_c)
    d_model = model.fiber_diameter(q, speed_mm_s)
    val = model.material.viscosity_ref * (d_model / measured_d_mm) ** 2
    if not _positive(val):
        raise ValueError("the model predicts no fiber at these conditions, so "
                         "there is nothing to scale -- check the Physics page")
    return val


def fit_activation_energy(points) -> float:
    """Fit the flow activation energy Ea (J/mol) from >= 2 (temperature_degC,
    diameter_mm) measurements taken at a FIXED pressure and speed. Since
    d^2 ~ 1/eta(T) ~ exp(-Ea/R * 1/T), ln(d^2) is linear in 1/T_kelvin with slope
    -Ea/R, so Ea = -slope * R by ordinary least squares. Pressure and speed need
    not be known -- they only shift the (discarded) intercept. Raises on < 2
    distinct temperatures, and on an Ea that comes out zero or NEGATIVE: that
    is fibres getting thinner as the melt gets hotter, a viscosity that rises
    with temperature, which is a mislabelled row rather than a polymer."""
    xs, ys = [], []
    for t_c, d_mm in points:
        if not _positive(d_mm):
            raise ValueError("diameters must be positive (mm)")
        xs.append(1.0 / _kelvin(t_c))
        ys.append(2.0 * math.log(d_mm))           # ln(d^2)
    n = len(xs)
    if n < 2:
        raise ValueError("need at least two (temperature, diameter) points")
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0.0:
        raise ValueError("temperatures must differ to fit Ea")
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    ea = -slope * GAS_CONSTANT
    if not _positive(ea):
        raise ValueError(
            "these diameters shrink as the temperature rises, which would make "
            "Ea %.4g J/mol -- a melt that thickens when heated. Check each row "
            "pairs the right slide with the right temperature." % ea)
    return ea


def fit_sag_coeff(model: MEWModel, measured_depth_mm: float, span_mm: float,
                  diameter_mm: float, temperature_c: float) -> float:
    """Back out the suspended-span sag magnitude `sag_coeff` from one measured
    midspan droop. depth is LINEAR in sag_coeff, so we evaluate the real
    `bridge_sag_depth` at sag_coeff = 1 (carrying the model's own span/diameter/
    temperature exponents) and scale. The fit therefore tracks whatever span and
    diameter exponents the rig is set to."""
    if not _positive(measured_depth_mm):
        raise ValueError("measured sag depth must be positive (mm)")
    if not (_positive(span_mm) and _positive(diameter_mm)):
        raise ValueError("span and diameter must be positive (mm)")
    cal = dataclasses.replace(model.calibration, sag_coeff=1.0)
    unit_model = MEWModel(material=model.material, nozzle=model.nozzle,
                          working_distance=model.working_distance,
                          calibration=cal)
    t_ref = (cal.sag_temp_ref if cal.sag_temp_ref is not None
             else model.material.temperature_ref)
    vr = (model.viscosity(temperature_c) / model.viscosity(t_ref)
          if cal.sag_thermal_visc_exp != 0.0 else 1.0)
    unit_depth = unit_model.bridge_sag_depth(span_mm, diameter_mm,
                                             viscosity_ratio=vr)
    if not _positive(unit_depth):
        raise ValueError("degenerate span/diameter -> cannot fit sag_coeff")
    return measured_depth_mm / unit_depth


#: the sag ladder's refusal rule. Below EITHER of these the span exponent is not
#: identifiable from the row and the fit falls back to the rig's own p, fitting the
#: magnitude alone: three points is two parameters plus one, and a row spanning less
#: than 3x in span leaves the slope resting on measurement noise rather than on a
#: decade of lever arm. `fit_sag_ladder` never raises on this -- a ladder that only
#: came out with two good spans is still worth the magnitude.
SAG_MIN_SPANS = 3
SAG_MIN_SPAN_RATIO = 3.0


@dataclasses.dataclass(frozen=True)
class SagLadderFit:
    """What one sag ladder yields: the droop law's magnitude, its span exponent,
    and how well the power law described the row.

    `coeff` is `c` in depth = c * span^p, so its unit is mm^(1-p) and it is ONLY
    mm^-1 at p = 2 (the shipped default). `span_exp` is p. `exp_fitted` False means
    the refusal rule bit: p is the value handed in from the rig and `reason` says
    which threshold failed. `r2` is in LOG-LOG space -- the space the fit is done in
    -- and is None when it cannot be formed (one point, or every span equal).
    """
    coeff: float
    span_exp: float
    exp_fitted: bool
    r2: float | None
    n_spans: int
    span_ratio: float
    reason: str = ""


def fit_sag_ladder(points, fallback_span_exp: float = 2.0) -> SagLadderFit:
    """Fit depth = c * span^p from (span_mm, droop_mm) measurements off ONE ladder.

    ln(depth) = ln(c) + p*ln(span) is linear, so ordinary least squares in log-log
    space gives p as the slope and c as exp(intercept). That is the whole reason the
    ladder print exists: a single scaffold pore gives one point and can only invert
    the magnitude (`fit_sag_coeff`), while a row of spans gives the exponent too.

    NO fiber diameter and NO temperature enter here, and that is deliberate rather
    than an omission: one ladder is one fiber at one temperature, so the diameter and
    thermal factors of `bridge_sag_depth` are constant across every point and fold
    into the intercept -- into c. At the shipped sag_diam_exp = 0 and
    sag_thermal_visc_exp = 0 both factors are exactly 1 anyway. The fitted c is
    therefore the magnitude AT THE FIBER THIS LADDER WAS PRINTED WITH; nothing here
    measures that fiber, so nothing here writes `sag_diam_ref`.

    Below `SAG_MIN_SPANS` usable spans or `SAG_MIN_SPAN_RATIO` of span range the
    exponent is not fitted: p is taken from `fallback_span_exp` (the rig's own) and
    only the magnitude is fitted, as the geometric mean of depth/span^p. The result
    says so through `exp_fitted` / `reason` rather than raising, because that ladder
    is still a usable magnitude measurement.

    Raises on no usable point, or on a non-positive span or depth (a bottomed-out
    fiber is not entered at all -- see the operator steps in
    `calibration_procedures`)."""
    xs, ys, spans = [], [], []
    for span_mm, depth_mm in points:
        span = float(span_mm)
        depth = float(depth_mm)
        if not _positive(span):
            raise ValueError("spans must be positive (mm)")
        if not _positive(depth):
            raise ValueError("measured droops must be positive (mm)")
        xs.append(math.log(span))
        ys.append(math.log(depth))
        spans.append(round(span, 6))
    if not xs:
        raise ValueError("enter at least one (span, droop) measurement")
    distinct = sorted(set(spans))
    n_spans = len(distinct)
    span_ratio = distinct[-1] / distinct[0]

    reason = ""
    if n_spans < SAG_MIN_SPANS:
        reason = ("only %d usable span%s -- %d are needed to fit the exponent"
                  % (n_spans, "" if n_spans == 1 else "s", SAG_MIN_SPANS))
    elif span_ratio < SAG_MIN_SPAN_RATIO:
        reason = ("the spans cover only %.2fx -- %gx is needed to fit the exponent"
                  % (span_ratio, SAG_MIN_SPAN_RATIO))

    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    if reason:
        p = float(fallback_span_exp)
        # the magnitude alone, at a FIXED slope: the mean log residual, which is the
        # geometric mean of depth/span^p and the least-squares intercept for that p
        intercept = my - p * mx
    else:
        sxx = sum((x - mx) ** 2 for x in xs)
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        p = sxy / sxx
        intercept = my - p * mx
    ss_res = sum((y - (intercept + p * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = None if (n < 2 or ss_tot <= 0.0) else 1.0 - ss_res / ss_tot
    return SagLadderFit(coeff=math.exp(intercept), span_exp=p,
                        exp_fitted=not reason, r2=r2, n_spans=n_spans,
                        span_ratio=span_ratio, reason=reason)


def fit_fusion_overlap(measured_height_mm: float, n_layers: int,
                       diameter_mm: float) -> float:
    """Back out a fusion overlap phi from a measured N-layer stacked column height.
    With the default diameter-independent compaction the column height after N
    aligned layers is h = d * [1 + (N-1)*(1 - phi)], so phi = 1 - (h/d - 1)/(N - 1).
    Clamped to [0, 1). Assumes the fusion diameter exponent is 0 (the calibrated
    default); on a mixed-diameter fit, fit per diameter.

    WHICH phi this fits is set by WHAT YOU MEASURED, and the two are not
    interchangeable: a straight stacked WALL (one line printed N times, every contact
    collinear) fits `fusion_wall_overlap`; a CROSSOVER peak tower (0/90 layers meeting
    at a point) fits `fusion_overlap`. Both obey the same height law, which is exactly
    why the same estimator serves both -- and exactly why the number it returns must be
    put in the matching field. Before the wall/junction split existed this estimator's
    wall reading was the only phi there was, so it was fitted on a wall and then applied
    at crossings; that conflation is what the split undoes."""
    if n_layers < 2:
        raise ValueError("need at least two layers to read a peak height")
    if not _positive(diameter_mm):
        raise ValueError("diameter must be positive (mm)")
    if not _positive(measured_height_mm):
        raise ValueError("the measured height must be positive (mm)")
    phi = 1.0 - (measured_height_mm / diameter_mm - 1.0) / (n_layers - 1)
    return max(0.0, min(phi, 0.999))


@dataclasses.dataclass(frozen=True)
class FusionFit:
    """One fusion phi fitted over several (layers, height) readings of ONE print.

    `phi` is the least-squares answer; `per_row` is each reading's own
    `fit_fusion_overlap`, kept so the operator can see the spread the single
    number hides."""
    phi: float
    per_row: tuple
    n_rows: int


def fit_fusion_rows(rows, diameter_mm: float) -> FusionFit:
    """Fit phi over several (n_layers, measured_height_mm) readings of one print.

    Same height law as `fit_fusion_overlap`, h = d * [1 + (N-1)*(1 - phi)], which is
    linear through the origin in x = N-1, y = h/d - 1: y = x*(1 - phi). Least squares
    gives 1 - phi = sum(x*y) / sum(x^2), so a taller stack -- which carries more of
    the compaction signal -- weighs more than a two-layer one. One row reduces to
    `fit_fusion_overlap` exactly. Clamped to [0, 1) like it.

    Fit junction readings and wall readings SEPARATELY (see `fit_fusion_overlap` for
    why the two phis are not interchangeable). The reference height N*d is what the
    same stack would reach unfused, so the answer is tied to the scaffold measured:
    fusion depends on layer time, which this law does not carry."""
    if not _positive(diameter_mm):
        raise ValueError("diameter must be positive (mm)")
    rows = [(int(n), float(h)) for n, h in rows]
    if not rows:
        raise ValueError("enter at least one (layers, height) reading")
    for n, h in rows:
        if n < 2:
            raise ValueError("every reading needs at least two layers")
        if not _positive(h):
            raise ValueError("every height must be positive (mm)")
    sxy = sum((n - 1) * (h / diameter_mm - 1.0) for n, h in rows)
    sxx = sum((n - 1) ** 2 for n, _h in rows)
    phi = max(0.0, min(1.0 - sxy / sxx, 0.999))
    per_row = tuple(fit_fusion_overlap(h, n, diameter_mm) for n, h in rows)
    return FusionFit(phi=phi, per_row=per_row, n_rows=len(rows))


def coil_radius_from_peak_to_peak(peak_to_peak_mm: float) -> float:
    """The below-CTS coil radius anchor from a measured loop peak-to-peak width:
    R ~ peak_to_peak / 2 (Hochleitner 2018's pp = loop diameter = 2R)."""
    if not _positive(peak_to_peak_mm):
        raise ValueError("peak-to-peak must be positive (mm)")
    return peak_to_peak_mm / 2.0


__all__ = [
    "FieldSpec", "CALIBRATION_FIELDS", "SECTIONS", "KEY_PREFIX",
    "DORMANT_CALIBRATION_ATTRS", "fmt_value", "default_profile", "grid_labels",
    "models_from_profile", "load_profile", "build_model",
    "LAW_SOURCE_KEY", "LAW_CONSTS_KEY", "MEASURED_KEY",
    "cts_from_diameter", "fit_viscosity_ref", "fit_activation_energy",
    "fit_sag_coeff", "fit_fusion_overlap", "FusionFit", "fit_fusion_rows",
    "coil_radius_from_peak_to_peak",
]
