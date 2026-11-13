"""Keep a scaffold's canonical mm/min speed and its CTS ratio together."""

import json
import math
import tkinter as tk
from tkinter import ttk

import gui_units


def attach(app, values):
    """Attach once to one design's variables, including G-code-page copies."""
    speed = values["speed"]
    if getattr(speed, "_cts_ratio_attached", False):
        return
    if "speed_unit" not in values:
        values["speed_unit"] = tk.StringVar(value="Standard units")
    if "speed_ratio" not in values:
        values["speed_ratio"] = tk.StringVar(value="")
    for key in ("layer_speed_ratios", "m_layer_speed_ratios"):
        if key not in values:
            values[key] = tk.StringVar(value="")
    mode, ratio = values["speed_unit"], values["speed_ratio"]
    flat_cts = getattr(app, "cal_vars", {}).get("cal.cts_ref")
    if flat_cts is None:
        return
    speed._cts_ratio_attached = True
    note = speed._cts_ratio_note = tk.StringVar(value="")
    busy = [False]
    rescaling = [False]

    def finite(raw):
        try:
            n = float(raw)
            return n if math.isfinite(n) and n > 0 else None
        except (ValueError, TypeError):
            return None

    def current_cts():
        collector = getattr(app, "s_collector", None)
        mandrel = getattr(app, "m_cts", None)
        if collector is not None and collector.get() == "mandrel" and mandrel is not None:
            raw = mandrel.get().strip()
            if raw:
                return finite(raw), "mandrel"
        return finite(flat_cts.get()), "plate"

    def explain():
        c, source = current_cts()
        n = finite(ratio.get())
        if c is None:
            note.set("Enter a positive %s CTS on the Physics page." % source)
        elif n is None:
            note.set("Enter a positive ratio; %s CTS is %.5g mm/min."
                     % (source, c * 60.0))
        else:
            note.set("%.5g × %s CTS %.5g mm/min = %.5g mm/min"
                     % (n, source, c * 60.0, n * c * 60.0))

    def from_speed(*_):
        if busy[0] or getattr(app, "_loading_preset", False):
            return
        n, c = finite(speed.get()), current_cts()[0]
        busy[0] = True
        try:
            ratio.set("%.12g" % (n / (60.0 * c)) if n and c else "")
        finally:
            busy[0] = False
        explain()

    def from_ratio(*_):
        if busy[0] or getattr(app, "_loading_preset", False) or mode.get() != "× CTS":
            return
        n, c = finite(ratio.get()), current_cts()[0]
        busy[0] = True
        try:
            # Invalid input cannot silently use the last valid speed.
            speed.set("%.12g" % (n * c * 60.0) if n and c else "")
        finally:
            busy[0] = False
        explain()

    def switched(*_):
        if mode.get() == "× CTS":
            from_speed()
            from_ratio()

    def rescale_layers():
        if getattr(app, "_loading_preset", False):
            return
        c, _source = current_cts()
        for meta_key, override_key in (("layer_speed_ratios", "layer_overrides"),
                                       ("m_layer_speed_ratios", "m_layer_overrides")):
            if override_key not in values:
                continue
            try:
                ratios = json.loads(values[meta_key].get() or "{}")
                overrides = json.loads(values[override_key].get() or "{}")
                if not isinstance(ratios, dict) or not isinstance(overrides, dict):
                    continue
                changed = False
                for layer, raw in ratios.items():
                    if layer not in overrides or "speed" not in overrides[layer]:
                        continue
                    n = finite(raw)
                    result = "%.12g" % (n * c * 60.0) if n and c else "invalid CTS"
                    if str(overrides[layer]["speed"]) != result:
                        overrides[layer]["speed"] = result
                        changed = True
                if changed:
                    rescaling[0] = True
                    try:
                        values[override_key].set(json.dumps(overrides))
                    finally:
                        rescaling[0] = False
            except (ValueError, TypeError, AttributeError):
                continue

    def reconcile_layers(meta_key, override_key):
        """Drop a ratio when another editor removes or replaces its speed."""
        if rescaling[0] or getattr(app, "_loading_preset", False):
            return
        try:
            ratios = json.loads(values[meta_key].get() or "{}")
            overrides = json.loads(values[override_key].get() or "{}")
            c, _source = current_cts()
            if not isinstance(ratios, dict) or not isinstance(overrides, dict):
                return
            keep = {}
            for layer, raw in ratios.items():
                n = finite(raw)
                speed_value = overrides.get(layer, {}).get("speed")
                if n and c is None and speed_value == "invalid CTS":
                    keep[layer] = raw
                    continue
                actual = finite(speed_value)
                if n and c and actual and math.isclose(actual, n * c * 60.0,
                                                       rel_tol=1e-9, abs_tol=1e-9):
                    keep[layer] = raw
            if keep != ratios:
                values[meta_key].set(json.dumps(keep) if keep else "")
        except (ValueError, TypeError, AttributeError):
            pass

    def changed_cts(*_):
        from_ratio()
        rescale_layers()

    speed.trace_add("write", from_speed)
    ratio.trace_add("write", from_ratio)
    mode.trace_add("write", switched)
    speed._cts_current = current_cts
    for meta_key, override_key in (("layer_speed_ratios", "layer_overrides"),
                                   ("m_layer_speed_ratios", "m_layer_overrides")):
        if override_key in values:
            values[override_key].trace_add(
                "write", lambda *_a, m=meta_key, o=override_key: reconcile_layers(m, o))
    flat_cts.trace_add("write", changed_cts)
    collector = getattr(app, "s_collector", None)
    if collector is not None:
        collector.trace_add("write", changed_cts)
    def watch_mandrel():
        mandrel = getattr(app, "m_cts", None)
        if mandrel is not None:
            mandrel.trace_add("write", changed_cts)
            if mode.get() == "× CTS":
                from_ratio()
            else:
                from_speed()
    app.root.after_idle(watch_mandrel)
    def settle():
        (from_ratio() if mode.get() == "× CTS" else from_speed())
        rescale_layers()
    speed._cts_ratio_settle = settle
    from_speed()


def _selector(combo, display, mode):
    """Show the actual Settings speed unit while retaining a stable mode key."""
    def refresh(*_):
        unit = gui_units.shown("speed", "mm/min")
        combo.configure(values=(unit, "× CTS"))
        display.set("× CTS" if mode.get() == "× CTS" else unit)

    def selected(_event):
        mode.set("× CTS" if display.get() == "× CTS" else "Standard units")

    combo.bind("<<ComboboxSelected>>", selected, add="+")
    mode.trace_add("write", refresh)
    gui_units.track_callback(combo, refresh)


def form_rows(app, form, values):
    """Put the two representations at the same print-speed control."""
    attach(app, values)
    mode = values["speed_unit"]
    display = tk.StringVar(value="")
    row = form.combo("Print speed units", display, (),
                     "× CTS uses the CTS you entered on the Physics page.")
    _selector(row.field, display, mode)
    absolute = form.entry("Print speed", values["speed"],
                          "The feed rate the fiber is laid at.", unit="mm/min")
    ratio = form.entry("Print speed", values["speed_ratio"],
                       "Print speed divided by your measured CTS.", unit="× CTS")
    note = form.live(None, textvariable=values["speed"]._cts_ratio_note, wrap=430)

    def show(*_):
        absolute.show(mode.get() != "× CTS")
        ratio.show(mode.get() == "× CTS")
        note.show(mode.get() == "× CTS")
    mode.trace_add("write", show)
    show()


def compact_row(app, form, values, row):
    """The mandrel form uses fixed row numbers, so share a single row."""
    attach(app, values)
    mode = values["speed_unit"]
    absolute = form.entry("Print speed", values["speed"],
                          "Resultant speed at the collector surface.",
                          unit="mm/min", row=row)
    ratio = form.entry("Print speed", values["speed_ratio"],
                       "Speed divided by the measured CTS on the Physics page.",
                       unit="× CTS", row=row)
    tail = ttk.Frame(form.frame)
    tail.grid(row=row, column=2, sticky="w")
    display = tk.StringVar(value="")
    selector = ttk.Combobox(tail, textvariable=display, state="readonly", width=14)
    _selector(selector, display, mode)
    selector.pack(anchor="w")
    note = ttk.Label(tail, textvariable=values["speed"]._cts_ratio_note,
                     style="Hint.TLabel", wraplength=200)
    note.pack(anchor="w")

    def show(*_):
        absolute.show(mode.get() != "× CTS")
        ratio.show(mode.get() == "× CTS")
        if mode.get() == "× CTS":
            note.pack(anchor="w")
        else:
            note.pack_forget()
    mode.trace_add("write", show)
    show()
