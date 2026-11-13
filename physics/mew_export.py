#!/usr/bin/env python3
"""mew_export.py -- the export wrapper for a physics-deposited scaffold.

A thin front-end the GUI launches in place of `gcode_to_fibers.py` (or
`scaffold_gcode.py`) when any of the Physics page's Jet lag, Fiber sagging or
Fiber fusion switches is on (`--no-jet-lag` when only the last two are). It does
NOT reimplement the converter: it installs the `mew_deposition` fibre transform
into `gcode_to_fibers`'s `_FIBER_TRANSFORM` seam, then dispatches to the SAME
`main()` the plain export uses. Because that seam is a module global, the transform
also threads through `scaffold_gcode.py`'s in-process chain, so one install
reshapes the merged STL, the per-scaffold sibling STLs and the deposition-cache
sidecar the player reads, with no change to the converter beyond the seam.

Usage (the GUI builds this; shown for reference):
    python physics/mew_export.py --target converter \\
        --standoff 4.5 --voltage 4.0 --pressure 20684 --temperature 90 \\
        input.gcode -o fibers.stl -d 0.02 -f 12 --sag-multiplier 1.0
    python physics/mew_export.py --target scaffold \\
        --standoff 4.5 --voltage 4.0 --pressure 20684 --temperature 90 \\
        -o scaffold.gcode --stl scaffold.stl --width 10 ... --stl-args...

Everything after the physics knobs is passed through UNTOUCHED to the chosen
downstream `main()`, so this never has to track the converter's / generator's CLI.

See the `physics-build` skill.
"""

if __name__ == "__main__":  # run as a script: put the program's folders on sys.path
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "engine"))
    import app_paths  # noqa: E402,F401

import argparse
import math
import sys

import gcode_to_fibers
import mew_calibration
from mew_physics import CELSIUS_TO_KELVIN
from mew_deposition import auto_corner_grid as make_corner_grid
from mew_deposition import make_transform


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    # Only the physics knobs + the dispatch target are parsed here; allow_abbrev
    # is OFF so a downstream flag (e.g. scaffold's --speed) is never mistaken for
    # an abbreviation of one of ours (--speed-scale). Everything else is left in
    # `rest` and forwarded verbatim to the downstream main().
    ap = argparse.ArgumentParser(
        add_help=False, allow_abbrev=False,
        description="Physics-deposited export wrapper (centreline reshaping).")
    ap.add_argument("--target", choices=["converter", "scaffold"],
                    default="converter",
                    help="which downstream main() to drive after installing the "
                         "physics fibre transform")
    ap.add_argument("--standoff", type=float, default=4.5,
                    help="needle-to-collector gap H, mm (the jet's working "
                         "distance; sets the lag distance H*tan(theta))")
    ap.add_argument("--voltage", type=float, default=4.0, help="kV")
    ap.add_argument("--pressure", type=float, default=2.0e4, help="melt pressure, Pa")
    ap.add_argument("--temperature", type=float, default=90.0, help="degC")
    ap.add_argument("--calibration", default=None,
                    help="path to a rig calibration profile JSON (mew_calibration; "
                         "the Material / Nozzle / Calibration anchors + hooks). "
                         "Omitted -> the factory defaults, byte-identical.")
    # RETIRED: accepted so existing command lines and the GUI's argv
    # builder keep working, but it reaches no geometry -- it damped the second-order
    # contact filter that `integrate_contact_path_ray` replaced.
    ap.add_argument("--zeta", type=float, default=None,
                    help="RETIRED and ignored: it damped the second-order contact "
                         "filter the ray placement step replaced. "
                         "Accepted so old command lines keep working.")
    ap.add_argument("--fill-per-tau", type=float, default=6.0,
                    help="refine grid: deposited samples per relaxation time "
                         "inside a corner/start transient (default: 6, the "
                         "engine's own -- byte-identical when omitted). It sets "
                         "how finely a TURNAROUND CREST is resolved; raise it "
                         "(12, 24) when crests read faceted. The point count and "
                         "the deposition time scale with it.")
    ap.add_argument("--auto-corner-grid", type=float, default=0.0,
                    metavar="FIBRE_DIAMETER_MM",
                    help="hold the DRAWN corner to a chord tolerance instead of "
                         "to a fixed sample count: march, measure how far the "
                         "polyline sits off the curve, re-march where it is over "
                         "(mew_deposition.auto_corner_grid). Takes the deposited "
                         "fibre diameter in mm, because the tolerance is a "
                         "fraction of it; 0 (the default) leaves the "
                         "--fill-per-tau grid alone, byte-identical. This is the "
                         "4-AXIS default -- a tube is corners nearly end to end "
                         "and its lag is anisotropic, so one global time step "
                         "faceted the whole part (31.7 um, 1.6 fibre diameters, "
                         "on a chevron tube at --fill-per-tau 6). --fill-per-tau "
                         "still seeds this grid and still raises it.")
    ap.add_argument("--curve-quality", default="standard",
                    choices=("draft", "standard", "fine"),
                    help="the Curve-quality tier for --auto-corner-grid: how far "
                         "the DRAWN fibre may sit from the curve it follows, as a "
                         "fraction of its own diameter (0.25 / 0.1 / 0.03), "
                         "together with the integration budget that makes that "
                         "tolerance mean something. Ignored without "
                         "--auto-corner-grid. See mew_deposition.CURVE_QUALITY "
                         "for the measured cost of each tier.")
    ap.add_argument("--dynamic-diameter", action="store_true",
                    help="vary the deposited fibre diameter ALONG its length with "
                         "the head's speed (d = sqrt(4Q/pi v) at the local speed -- "
                         "thicker on slower moves like the linking loops). Off -> a "
                         "uniform diameter (the converter's -d).")
    ap.add_argument("--coil", action="store_true",
                    help="enable the below-CTS sewing-machine coiling model (off by "
                         "default: below-CTS fibres pass through as commanded)")
    ap.add_argument("--coil-wave-amp", type=float, default=0.0,
                    help="below-CTS coiling-WAVE indicator amplitude, mm (a simple "
                         "fixed lateral sine marking that CTS was reached -- distinct "
                         "from the physical --coil model). <= 0 (default) -> off, "
                         "below-CTS fibres pass through as commanded.")
    ap.add_argument("--coil-wave-len", type=float, default=0.3,
                    help="below-CTS coiling-wave indicator wavelength, mm "
                         "(only meaningful with --coil-wave-amp > 0)")
    ap.add_argument("--render-travel", action="store_true",
                    help="CONTINUOUS JET: render travel (G0) moves as deposited "
                         "fibre and carry the jet's lag state THROUGH them into the "
                         "next fibre. In MEW the jet never stops -- it trails through "
                         "every travel and enters the next fibre already lagging, "
                         "with a sharp commanded turn at the entry. Off (default) "
                         "each fibre is laid in isolation from a fresh at-rest state "
                         "and travels deposit nothing -- byte-identical. Each laid "
                         "travel is merged into the fibre it leads into, so the fibre "
                         "count (and the per-scaffold grouping) is unchanged.")
    ap.add_argument("--physical-sag", action="store_true",
                    help="use the physical viscous-catenary sag law "
                         "(MEWModel.make_sag_law; span^2 exponent grounded, magnitude "
                         "calibrated) instead of the geometric droop -- "
                         "calibrated for flatter floor descents. Off -> geometric, "
                         "byte-identical. Stamps the .depo cache as physical-sag.")
    ap.add_argument("--physical-sag-viscous", action="store_true",
                    help="DORMANT future-work (not exposed in the GUI): sub-option of "
                         "--physical-sag that swaps the reduced-order droop for the "
                         "VISCOUS-CREEP law (MEWModel.bridge_sag_depth_viscous -- Nguyen "
                         "2019, temperature predicted via eta(T)). Default off; only "
                         "takes effect with --physical-sag. Needs a rig sag sweep to "
                         "validate before use -- see the sag dossier.")
    ap.add_argument("--physical-fusion", action="store_true",
                    help="sink stacked crossover peaks by the junction-fusion law "
                         "(MEWModel.make_fusion_law): a freshly-laid fibre coalesces "
                         "INTO the one below at each crossover, so it rests less than "
                         "a full diameter on top -- the suspended spans are untouched. "
                         "Off -> the geometric full-diameter stack, byte-identical. "
                         "Stamps the .depo cache as physical-fusion.")
    ap.add_argument("--mandrel-per-layer", action="store_true",
                    help="MANDREL ONLY, and off by default: re-evaluate the "
                         "cylindrical contact placement on each GROWN layer "
                         "(r -> r + wall height) instead of once per scaffold at "
                         "the bare radius. Neuhaus Eq 1-5 is a single-layer "
                         "model; composing it per layer leans the wall ~5.5 "
                         "um/layer and nothing measures that composition on a "
                         "mandrel, so the freeze is the default "
                         "(MANDREL_LAG_DERIVATION.md 12.8). No effect on a flat "
                         "print -- there is no placement at all. Not wired into "
                         "gcode_player, whose live re-lay is flat anyway (see "
                         "mew_live's module docstring), so a run with this on "
                         "should be viewed from the .depo cache it writes.")
    # Accepted and IGNORED: installing the transform below already marks the .depo
    # cache as physics-deposited (write_depo_cache transformed=). This flag exists only
    # so the GUI's shared sim args can also carry it to gcode_player (which uses it to
    # reject a stale cache from the other mode) without it leaking to the converter's
    # own argparse via `rest`.
    ap.add_argument("--physics-deposit", action="store_true",
                    help=argparse.SUPPRESS)
    # The GUI's per-law switches: with Jet lag off but sagging or fusion on, the
    # export still needs this wrapper for the grounded sag / fusion laws, but must
    # leave the commanded centreline alone. Off (default) -> the transform is
    # installed as always, so every other caller is byte-identical.
    ap.add_argument("--no-jet-lag", action="store_true",
                    help="do NOT install the jet-touchdown transform: the fibre "
                         "keeps its commanded path (no lag, corner rounding, "
                         "accel re-timing, coiling wave or along-length diameter) "
                         "while --physical-sag / --physical-fusion still apply")
    ap.add_argument("--commanded-out", default=None,
                    help=argparse.SUPPRESS)
    # machine motion limits for the accel-limited deposition re-timing
    # (mew_kinematics): the lag/diameter then track the head's REAL ramped speed
    # (slower into corners / tight turnarounds) instead of the commanded feed.
    # Printer properties (Klipper printer.cfg), Voron 0.2 defaults; <= 0 disables it.
    ap.add_argument("--max-accel", type=float, default=2000.0,
                    help="acceleration limit, mm/s^2 (Klipper max_accel); <= 0 -> the "
                         "naive per-move commanded speeds (no ramp)")
    ap.add_argument("--square-corner-velocity", type=float, default=6.0,
                    help="speed held through a 90deg corner, mm/s (Klipper SCV)")
    ap.add_argument("--rotary-accel", type=float, default=0.0,
                    help="the mandrel's rotary acceleration limit, deg/s^2; <= 0 "
                         "-> none, and --max-accel caps the whole move along the "
                         "surface. Set, --max-accel caps the linear axes only")
    known, rest = ap.parse_known_args(argv)

    # argparse's float() takes "nan" and "inf", and NaN passes a `<= 0` guard,
    # so each knob is checked for what the model can actually run on. Refused
    # here, the GUI's log gets one line; let through, a NaN became a "nan-nan um"
    # diameter in an STL written with exit code 0.
    if not (math.isfinite(known.standoff) and known.standoff > 0.0):
        sys.stderr.write("mew_export: --standoff must be positive (mm)\n")
        return 2
    for flag, val in (("--voltage", known.voltage),
                      ("--pressure", known.pressure),
                      ("--temperature", known.temperature)):
        if not math.isfinite(val):
            sys.stderr.write("mew_export: %s must be a finite number, got %r\n"
                             % (flag, val))
            return 2
    if known.temperature <= -CELSIUS_TO_KELVIN:
        sys.stderr.write("mew_export: --temperature must be above absolute "
                         "zero (-273.15 degC)\n")
        return 2
    if known.dynamic_diameter and known.pressure <= 0.0:
        # no melt flows at or below zero gauge pressure, so a physics-driven
        # diameter would be a 0 um fibre
        sys.stderr.write("mew_export: --pressure must be positive for a "
                         "physics-driven diameter (Pa)\n")
        return 2

    # Build the jet model from the rig calibration profile (Material / Nozzle /
    # Calibration anchors + hooks), or the factory defaults when no --calibration
    # is given (byte-identical to the pre-profile behaviour). The gap stays the
    # separate --standoff working distance. The viscous flag overlays the dormant
    # viscous-creep sag toggle (sub-option of --physical-sag): make_sag_law then
    # builds the viscous closure (temperature predicted via eta(T)).
    model = mew_calibration.build_model(
        known.calibration, known.standoff, viscous=known.physical_sag_viscous,
        per_layer_registration=known.mandrel_per_layer)
    zeta = (known.zeta if known.zeta is not None
            else model.calibration.contact_damping_zeta)
    transform = make_transform(
        model, pressure=known.pressure, voltage=known.voltage,
        temperature=known.temperature, zeta=zeta,
        dynamic_diameter=known.dynamic_diameter, coil=known.coil,
        coil_wave_amp=known.coil_wave_amp, coil_wave_len=known.coil_wave_len,
        max_accel=known.max_accel,
        square_corner_velocity=known.square_corner_velocity,
        rotary_accel=known.rotary_accel,
        simulate_travel=known.render_travel,
        fill_per_tau=known.fill_per_tau,
        corner_chord=(make_corner_grid(known.auto_corner_grid,
                                       known.curve_quality)
                      if known.auto_corner_grid > 0.0 else None),
        log=lambda m: print(m, flush=True))

    if known.commanded_out:
        import numpy as _np
        _orig_t = transform
        def _save_then_transform(paths, ptimes, travels=None, *, frame=None):
            kw = {}
            if paths:
                kw["cmd_xyz"] = _np.concatenate(paths, axis=0).astype(_np.float32)
                kw["cmd_splits"] = _np.cumsum(
                    [len(p) for p in paths])[:-1].astype(_np.int64)
                if ptimes:
                    kw["cmd_t"] = _np.concatenate(ptimes).astype(_np.float32)
            if travels:
                tv_arrs = []
                for t in travels:
                    if t is None:
                        continue
                    xyz = t[0] if isinstance(t, tuple) else t
                    if hasattr(xyz, '__len__') and len(xyz) > 0:
                        tv_arrs.append(_np.asarray(xyz, dtype=_np.float64))
                if tv_arrs:
                    kw["trav_xyz"] = _np.concatenate(
                        tv_arrs, axis=0).astype(_np.float32)
                    kw["trav_splits"] = _np.cumsum(
                        [len(a) for a in tv_arrs])[:-1].astype(_np.int64)
            _np.savez(known.commanded_out, **kw)
            return _orig_t(paths, ptimes, travels, frame=frame)
        transform = _save_then_transform

    try:
        # with_travels tells the converter to parse the G0 geometry and hand it to
        # the transform, which lays it and merges it in (see make_transform).
        if not known.no_jet_lag:
            gcode_to_fibers.set_fiber_transform(
                transform, with_travels=known.render_travel, with_frame=True)
        # Grounded sag law (opt-in): same module-global seam as the transform, so
        # it threads through the converter AND scaffold_gcode's in-process main()
        # chain. Installed inside the try so a raise here (e.g. an extreme
        # temperature) still hits the finally below and the seams don't leak.
        if known.physical_sag:
            gcode_to_fibers.set_sag_law(
                model.make_sag_law(temperature=known.temperature))
        # Junction fusion (opt-in): crossover-localised compaction, same module-
        # global seam, so it threads through the converter AND scaffold_gcode's
        # in-process main() chain and stamps the .depo cache via _FUSION_LAW is
        # not None.
        if known.physical_fusion:
            gcode_to_fibers.set_fusion_law(
                model.make_fusion_law(temperature=known.temperature),
                model.fusion_wall_band())
        if known.target == "scaffold":
            import scaffold_gcode
            return scaffold_gcode.main(rest)
        return gcode_to_fibers.main(rest)
    finally:
        gcode_to_fibers.set_fiber_transform(None)
        gcode_to_fibers.set_sag_law(None)
        gcode_to_fibers.set_fusion_law(None)


if __name__ == "__main__":
    raise SystemExit(main())
