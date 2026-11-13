"""gcode_view_shots.py -- render the G-code page with each new tool in use, flat and 4-axis
tube, both themes, and save downscaled PNGs for reading back.

    python tools/gcode_view_shots.py [outdir]

Uses capture_ui.grab (PrintWindow) on the real window, mapped off to the side,
so what is saved is what the page draws (GPU-direct picture included)."""
import os
import sys
import tempfile
import time
import tkinter as tk

HERE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))   # test_gcode_editor
sys.path.insert(0, os.path.join(HERE, "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
os.chdir(HERE)
import numpy as np  # noqa: E402

import test_gcode_editor as E  # noqa: E402

pump = E.pump
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    HERE, ".claude", "visual", "gcode_view_shots")
os.makedirs(OUT, exist_ok=True)


def grid_file(path, lines=14, spacing=1.0, layers=6):
    """A crossed-grid scaffold g-code: layer k runs along X (even) or Y
    (odd), `spacing` mm between lines, F stepping per layer."""
    out = ["G90", "G0 X0 Y0 Z0 F3000"]
    for k in range(layers):
        z = 0.02 * k
        f = 600 + 300 * k
        out.append(f"G0 Z{z:.3f}")
        out.append(f"G1 F{f}")
        for j in range(lines):
            a = j * spacing
            if k % 2 == 0:
                x0, x1 = (0, (lines - 1) * spacing)[::1 if j % 2 == 0 else -1]
                if j == 0:
                    out.append(f"G0 X{x0:.3f} Y{a:.3f}")
                out.append(f"G1 X{x0:.3f} Y{a:.3f}")
                out.append(f"G1 X{x1:.3f} Y{a:.3f}")
            else:
                y0, y1 = (0, (lines - 1) * spacing)[::1 if j % 2 == 0 else -1]
                if j == 0:
                    out.append(f"G0 X{a:.3f} Y{y0:.3f}")
                out.append(f"G1 X{a:.3f} Y{y0:.3f}")
                out.append(f"G1 X{a:.3f} Y{y1:.3f}")
    return "\n".join(out) + "\n"


def shot(name, scale=0.5):
    import capture_ui
    app, root = E._M["app"], E._M["root"]
    pump(0.6)
    app._gce_pv_panel._render()
    pump(0.3)
    im = capture_ui.grab(root)
    im = im.resize((int(im.width * scale), int(im.height * scale)))
    p = os.path.join(OUT, name + ".png")
    im.save(p)
    print("saved", p)


def crop(name, widgets, pad=14):
    """Full-scale crop around the union of `widgets` (the scrubber states)."""
    import capture_ui
    root = E._M["root"]
    pump(0.5)
    im = capture_ui.grab(root)
    x0 = min(w.winfo_rootx() for w in widgets) - root.winfo_rootx() - pad
    y0 = min(w.winfo_rooty() for w in widgets) - root.winfo_rooty() - pad
    x1 = max(w.winfo_rootx() + w.winfo_width() for w in widgets)         - root.winfo_rootx() + pad
    y1 = max(w.winfo_rooty() + w.winfo_height() for w in widgets)         - root.winfo_rooty() + pad + 24
    p = os.path.join(OUT, name + ".png")
    im.crop((max(x0, 0), max(y0, 0), x1, y1)).save(p)
    print("saved", p)


def scrub_states(tag):
    app = E._M["app"]
    bar = app._view_bar
    sc = app._view_scale
    ax = app._view_axis
    app._view_show_all()
    pump(0.3)
    crop(f"scrub_end_{tag}", [bar])
    app._view_on_scrub(len(ax) // 2)
    sc.set_pos(len(ax) // 2, notify=False)
    pump(0.3)
    crop(f"scrub_mid_{tag}", [bar])
    sc._focus(True)
    pump(0.2)
    crop(f"scrub_mid_focus_{tag}", [bar])
    sc._focus(False)
    sc.set_enabled(False)
    pump(0.3)
    crop(f"scrub_disabled_{tag}", [bar])
    app._view_refresh_ui()
    app._view_show_all()
    pump(0.2)


def theme(dark):
    app = E._M["app"]
    app.dark_mode.set(bool(dark))
    pump(1.0)


def main():
    if not E.boot():
        print("boot failed")
        return 1
    app, root = E._M["app"], E._M["root"]
    E._show(focus=False)
    root.geometry("1400x900+-12000+0")
    pump(2.0)
    # ---- flat, default design ----------------------------------------- #
    shot("flat_default_light")
    app._view_on_range(2, 5)
    pump(0.5)
    app._view_set_mode("layer")
    pump(0.5)
    shot("flat_layers3-6_layercolour_light")
    app._view_show_all()
    app._view_set_mode("scaffold")
    pump(0.3)
    # ---- 4-axis tube ------------------------------------------------------ #
    app.s_collector.set("mandrel")
    pump(25, lambda: app._gce_pv_panel._state is not None
         and "radius" in (app._gce_pv_panel._state.get("meta") or {})
         and len(app._view_layers) > 0)
    pump(2.0)
    print("tube layers", len(app._view_layers))
    shot("tube_default_light")
    app._view_on_range(0, max(0, len(app._view_layers) // 2))
    app._view_set_mode("layer")
    pump(0.5)
    shot("tube_layers_light")
    theme(True)
    shot("tube_layers_dark")
    app._view_show_all()
    app._view_set_mode("speed")
    pump(0.4)
    shot("tube_speed_dark")
    app.s_collector.set("plate")
    pump(10, lambda: "radius" not in (app._gce_pv_panel._state.get("meta") or {}))
    pump(2.0)
    app._view_show_all()
    app._view_set_mode("scaffold")
    # ---- a file with varied speed -------------------------------------- #
    path = os.path.join(tempfile.mkdtemp(), "grid.gcode")
    open(path, "w").write(grid_file(path))
    app._gce_load_file(path)
    pump(8, lambda: app._gce_pv_panel._state is not None
         and app._gce_pv_panel._state.get("aux") is not None
         and len(app._view_layers) == 6)
    pump(1.0)
    theme(False)
    app._view_set_mode("speed")
    pump(0.5)
    shot("file_speed_light")
    theme(True)
    shot("file_speed_dark")
    app._view_set_mode("layer")
    pump(0.4)
    shot("file_layer_dark")
    app._view_set_one(True)
    app._view_slider.step("hi", -2)
    pump(0.5)
    shot("file_onelayer_dark")
    app._view_show_all()
    pump(0.3)
    # scrub half way through
    ax = app._view_axis
    app._view_on_scrub(len(ax) // 2)
    pump(0.5)
    shot("file_scrub_dark")
    app._view_show_all()
    scrub_states("dark")
    theme(False)
    scrub_states("light")
    theme(True)
    # measure
    app._view_measure_set(True)
    pump(0.3)
    panel = app._gce_pv_panel
    from types import SimpleNamespace
    # find two neighbouring fibres top layer: project known points
    ren = panel._ren

    def disp(p):
        ren.SetWorldPoint(p[0], p[1], p[2], 1.0)
        ren.WorldToDisplay()
        d = ren.GetDisplayPoint()
        return d[0], panel._host.winfo_height() - d[1]
    p1, p2 = (5.0, 4.0, 0.04), (6.0, 4.3, 0.04)
    (x1, y1), (x2, y2) = disp(p1), disp(p2)
    panel.view_tools.hover(int(x1), int(y1), min_gap=0)
    pump(0.3)
    shot("file_measure_hover_dark")
    for x, y in ((x1, y1), (x2, y2)):
        ev = SimpleNamespace(x=int(x), y=int(y), state=0)
        panel._press("orbit", ev)
        panel._release(ev)
        pump(0.3)
    shot("file_measure_two_points_dark")
    print(app._view_text.get("1.0", "end"))
    theme(False)
    shot("file_measure_two_points_light")
    app._view_measure_set(False)
    # ---- fit -------------------------------------------------------------- #
    app._view_on_range(1, 2)
    pump(0.3)
    app._view_fit()
    pump(0.5)
    shot("file_fit_layers2-3_light")
    app._view_show_all()
    app._gce_prefs_set(color_by="scaffold")
    root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
