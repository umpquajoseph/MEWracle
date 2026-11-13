"""Display the fusion calibration as a fraction or a measured depth."""

import math
import tkinter as tk

import gui_form
import gui_lawedit
import mew_calibration
from mew_physics import MEWModel


# Plain-words labels and help for the modern Fusion card. The registry's own
# labels stay for the classic UI and Lite (whose field list mirrors it).
LABELS = {
    "fusion_overlap": "Junction sink",
    "fusion_wall_overlap": "Straight-span sink",
}
GATE_LABEL = "Straight spans have their own sink"
DESCS = {
    "fusion_overlap": (
        "How far a fiber sinks into the one it crosses, where two fibers "
        "cross at a point. As a coefficient it is φ, the sink as a fraction "
        "of the fiber diameter: 0 stacks the fibers with no sink, 0.35 sinks "
        "each one 35% of a diameter.\n\n"
        "Measure it from your crossover heights with Calibrate. Used only "
        "with Fusion on."),
    "fusion_wall_overlap": (
        "The sink where fibers lie along each other for a long run, as in a "
        "wall or a fiber sagging onto the parallel layer below, instead of "
        "crossing at a point.\n\n"
        "Unticked, straight spans sink by the junction value."),
    "fusion_max_overlap": (
        "The most any fiber may sink, as a fraction of the diameter, so the "
        "upper fiber never merges fully into the one below. It caps both "
        "sinks."),
}


def build(app, parent):
    """Build both overlap fields around the existing calibration variables.

    The diameter belongs to the measured specimen. It is used only to convert
    the entered overlap into the model's dimensionless reference coefficient.
    """
    F = gui_form.Form(parent)
    mode = tk.StringVar(value="Coefficient")
    diameter = tk.StringVar(value="")
    display = {key: tk.StringVar(value="") for key in
               ("cal.fusion_overlap", "cal.fusion_wall_overlap")}
    busy = [False]
    entries = {}

    F.combo("Enter sink as", mode, ("Coefficient", "µm", "mm"),
            "Coefficient: the sink as a fraction φ of the fiber diameter. "
            "µm or mm: the sink you measured, converted to φ with the fiber "
            "diameter below.")
    F.entry("Measured fiber diameter", diameter,
            "Measure the fiber diameter on the same specimen as the overlap. "
            "It converts a distance into a fraction.", unit="µm")
    message = tk.StringVar(value="")
    F.live(None, textvariable=message, wrap=440)

    fields = [fs for fs in mew_calibration.CALIBRATION_FIELDS
              if fs.section == "Fusion" and fs.group == "required"]
    for fs in fields:
        if fs.gate_label:
            F.check(GATE_LABEL, app.cal_vars[fs.gate_key], DESCS[fs.attr])
        row = F.entry(LABELS[fs.attr], display[fs.key], DESCS[fs.attr],
                      indent=1 if fs.gate_label else 0)
        entries[fs.key] = row.field
    limit = next(fs for fs in mew_calibration.CALIBRATION_FIELDS
                 if fs.attr == "fusion_max_overlap")
    F.entry("Sink limit, φ_max", app.cal_vars[limit.key],
            DESCS["fusion_max_overlap"], unit=limit.unit)

    def diameter_mm():
        try:
            value = float(diameter.get()) / 1000.0
            return value if math.isfinite(value) and value > 0 else None
        except ValueError:
            return None

    def conversion():
        """The shipped model's applied sink per unit coefficient and its cap."""
        d = diameter_mm()
        if d is None:
            return None, "Enter the measured fiber diameter to use a distance."
        if gui_lawedit.law_source(app, "fusion"):
            return None, "Distance entry is unavailable for a custom fusion law."
        try:
            mat, nozzle, cal = app._calibration_models()
            model = MEWModel(material=mat, nozzle=nozzle, calibration=cal)
            vr = 1.0
            if cal.fusion_thermal_visc_exp != 0:
                temperature = float(app.sim_temperature.get())
                ref = cal.fusion_temp_ref if cal.fusion_temp_ref is not None else mat.temperature_ref
                vr = model.viscosity(temperature) / model.viscosity(ref)
            # Ask the same model used by export, at a tiny coefficient so the
            # clamp does not obscure the linear factor we need to invert.
            factor = model._sink_from_overlap(1e-12, d, viscosity_ratio=vr) / 1e-12
            cap = cal.fusion_max_overlap * d
            if not (math.isfinite(factor) and factor > 0 and
                    math.isfinite(cap) and cap > 0):
                raise ValueError("fusion factor or limit")
            return (model, d, vr, factor, cap), ""
        except (ValueError, TypeError, AttributeError, ArithmeticError):
            return None, "Check the fusion settings and nozzle temperature."

    def refresh(*_):
        if busy[0]:
            return
        busy[0] = True
        try:
            unit = mode.get()
            conv, problem = conversion() if unit != "Coefficient" else (None, "")
            message.set(problem or "Distances use the measured fiber diameter and "
                        "current nozzle temperature. The sink limit, φ_max, applies.")
            for fs in fields:
                raw = app.cal_vars[fs.key].get()
                value = raw
                if unit != "Coefficient":
                    try:
                        value = ("%.8g" % (conv[0]._sink_from_overlap(
                            float(raw), conv[1], viscosity_ratio=conv[2]) *
                            (1000.0 if unit == "µm" else 1.0))) if conv else ""
                    except ValueError:
                        value = raw
                display[fs.key].set(value)
                enabled = (unit == "Coefficient" or conv is not None)
                if fs.gate_label:
                    enabled = enabled and app.cal_vars[fs.gate_key].get()
                entries[fs.key].configure(state="normal" if enabled else "disabled")
        finally:
            busy[0] = False

    def write(key):
        if busy[0]:
            return
        busy[0] = True
        try:
            raw = display[key].get().strip()
            unit = mode.get()
            if unit != "Coefficient":
                conv, _problem = conversion()
                if conv is None:
                    return
                try:
                    distance = float(raw) / (1000.0 if unit == "µm" else 1.0)
                    if not math.isfinite(distance) or distance < 0 or distance > conv[4]:
                        limit = conv[4] * (1000.0 if unit == "µm" else 1.0)
                        message.set("Enter a distance from 0 to %.6g %s; the "
                                    "fusion limit makes larger values unreachable."
                                    % (limit, unit))
                        entries[key].state(["invalid"])
                        raw = "invalid distance (outside fusion limit)"
                    else:
                        entries[key].state(["!invalid"])
                        message.set("Distances use the measured fiber diameter and "
                                    "current nozzle temperature. The sink limit, φ_max, applies.")
                        raw = "%.12g" % (distance / conv[3])
                except ValueError:
                    if raw:
                        message.set("Enter a non-negative fusion distance.")
                        entries[key].state(["invalid"])
                    else:
                        entries[key].state(["!invalid"])
                    # Preserve invalid input for the existing validator.
            app.cal_vars[key].set(raw)
        finally:
            busy[0] = False

    mode.trace_add("write", refresh)
    diameter.trace_add("write", refresh)
    for fs in fields:
        display[fs.key].trace_add("write", lambda *_a, k=fs.key: write(k))
        if fs.gate_label:
            app.cal_vars[fs.gate_key].trace_add("write", refresh)
    for var in app.cal_vars.values():
        var.trace_add("write", refresh)
    def watch_temperature():
        temperature = getattr(app, "sim_temperature", None)
        if temperature is not None:
            temperature.trace_add("write", refresh)
            refresh()
    parent.after_idle(watch_temperature)
    refresh()
    return (mode, diameter, display)
