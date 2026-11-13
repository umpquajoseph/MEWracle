"""G-code fixtures for `test_mew_gcode.py`: a flat scaffold, side-by-side scaffolds
with their layer boundaries, and a five-axis dome, all from the real generators.

Not a suite: nothing here runs on its own.
"""
import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import scaffold_gcode as sg


def _flat(**kw):
    return sg.generate_scaffold_gcode(5, 5, 0.5, 90.0, 0.02, 4, 600, **kw)



def _scaffolds(*start_zs, layers=3):
    """`n` side-by-side scaffolds at the given start Zs, plus the
    ``(ordinal, height)`` boundaries the GUI would compute for them: no plate
    lead-in (scaffold 1 starts at 0), no stabilization base, so scaffold k's
    first Z-bearing line is the k*layers'th."""
    base = dict(width=5, height=5, stepover=0.5, rotation=90.0,
                fiber_diameter=0.02, layers=layers, speed=600, start_angle=0.0,
                arc_radius=None, arc_segments=8, start_x=0.0, start_y=0.0,
                start_z=0.0)
    specs = [dict(base, start_x=10.0 * k, start_z=z)
             for k, z in enumerate(start_zs)]
    bounds = tuple((layers * k, z) for k, z in enumerate(start_zs) if k)
    return sg.generate_multi_scaffold_gcode(specs), bounds


#: the dome's arc radius, mm (test_flat_layer_z's own value; it was left
#: behind when these fixtures moved here, so `_five_axis` raised NameError and
#: test_mew_gcode skipped both five-axis fixtures behind its NOTE)
_R5 = 12.0


def _five_axis(spin="C", tilt="A"):
    """Real `generate_surface_gcode` output for a dome on a two-rotary table.

    The generator again, not a fixture: a change to how a five-axis move is
    spelled has to fail here rather than pass against a stale sample. The words
    are given to the JOINTS, and both spellings are exercised below -- A/C, and
    Terranova's own U/V -- because a guard that hardcoded a letter would pass
    one and corrupt the other."""
    import numpy as np
    import machine_model
    import mew_frame
    import mew_project
    machine = machine_model.MachineProfile.from_dict(
        {"parts": {"head": "h.stl", "gantry": "g.stl", "bed": "b.stl"},
         "kinematics": "mewron", "tip": [0, 0, 0], "standoff": 1.0,
         "rotaries": [{"word": spin, "role": "bed", "axis": "z"},
                      {"word": tilt, "role": "bed", "axis": "x",
                       "pivot": [0.0, 0.0, -8.0]}]})
    frame = mew_frame.RevolutionFrame(mew_frame.ArcProfile(_R5), u0=2.0, u1=10.0)
    surf = mew_project.FrameSurface(frame, height=0.0)
    pat = mew_project.serpentine_pattern(6.0, 4.0, 0.8, sample_step=0.6)
    pp = mew_project.project_pattern(surf, pat, np.array([6.0, 0.0]), step=0.25)
    return sg.generate_surface_gcode(machine, pp, speed=600.0)
