#!/usr/bin/env python3
"""bed_calibrator.py -- visual datum picker for the machine model.

One window, one datum, two meanings -- following the body in the "carries the print"
slot, which is a bed on a 3-axis rig and the mandrel HOLDER on a 4-axis one (the same
split `gui_buildplate._sync_collector_words` makes in the labels).

**Flat bed (no --axis).** Auto-detecting the print surface of a full bed-slinger
ASSEMBLY STL is unreliable -- the heatbed is not cleanly separable from the Y-axis
hardware at the same height -- so orbit to the bed, hover its bottom-left corner and
press P.

**Nozzle tip (--pick tip).** Pointed at the PRINTHEAD STL, where the same three
numbers mean the nozzle tip in that STL's own frame. `find_tip` auto-detects it as the
centroid of the lowest points, which is right for a bare nozzle and wrong for any head
whose lowest geometry is a shroud, a duct or a probe. The read-out adds the pick's
height above the STL's lowest point, because a tip that is not within a hair of that
is almost always a mis-click onto surrounding hardware.

**Rotary joint axis (--pick joint --axis z).** The same window aimed at a rotary BODY
of a five-axis collector table -- the C plate, the A cradle -- where the three numbers
are a point on that joint's axis of rotation, in that body's own STL frame. Identical
to the holder mode below minus the rod: a collector table turns no mandrel, so drawing
one would be a rod that is not on the machine. The axis LINE is the instrument here,
and it is what you centre on the joint's bore.

A joint that spins about **Z** opens on a GUESS -- `find_spin_face`, the centre of the
body's top face, which is where a collector mounts and therefore where its spin axis
is. Named as a guess in the read-out, and written nowhere until you move it or press P,
exactly as tip mode's seed is. Every other joint picks cold: a tilt axis is a bearing
line through the side of a cradle and there is no face whose centre it is. Unlike tip
mode, **P is not demoted here** -- a nozzle is hollow so a ray hits nothing at its
centre, while a mounting face is solid and clicking it works. The face is
auto-detected, and a manual pick always stays possible.

**G-code zero (--pick home --axis z).** INTERIM -- see the note at the end of this
docstring. The same window, aimed at the SAME body a `--pick joint` window is aimed
at (the joint that spins the collector), where the three numbers are where the NOZZLE
TIP sits when the machine is at `G0 X0 Y0 Z0`. It is a point on that joint's axis, so
the yellow line is the instrument here too: creep ALONG it until the marker is where
the nozzle should start. It seeds cold unless the caller passes `--datum`; the caller
passes the joint's own picked axis point, which is the one point on that line the
software already knows.

**Collector seat (--pick seat --axis z).** The other half of the five-axis pair, the
one `--pick home` is measured against: where the COLLECTOR SITS on the joint that
spins it, written to `rotary.j1_seat`. Same window and same body as `--pick joint`,
but the three numbers are the seat rather than the axis point, and the COLLECTOR
FOLLOWS THE MARKER: `--collector-seat` is ignored here (it is `--pick home`'s input,
where the seat is already known), so what you watch as you nudge is the rod moving
with you. Like `home`, it seeds cold unless the caller passes `--datum`. The seat and
the home are two SEPARATE calibrated datums, neither substitutes for the other, and
`test_five_axis_frame.py` is the suite that measures them.

**Custom G-code zero (--pick zero).** Not an STL at all: the window draws the
COLLECTOR ITSELF from its size -- the plate (`--zero-plate W,H,T`, or a wafer,
`--zero-disc D,T`) or the mandrel (`--zero-rod D,L`) -- and the three numbers are
the point the printer is zeroed on, for a researcher whose zero is a slide edge,
a clip or a mark rather than a corner or an end. The frames are the Setup page's:
the plate from its bottom-left corner and bottom face (`_plate_footprint_xy`), the
mandrel with its near end at X0 on the axis. On the mandrel only X is read --
a tube program has no Y word and its Z datum is the "Radially" choice.

**Rotary holder (--axis x).** THIS WINDOW IS WHERE THE MANDREL IS POSITIONED, not just
where a number is read. Press P on the holder to drop the axis through the point under
the cursor, then nudge it onto the seat while watching the rod you will print on;
nothing has to be generated first. Positioning is by EYE, not by fitting geometry: a
cylinder fit was built and removed the same day, exact on the holder it was written
against and useless on the next, because a mandrel seat is pins here, a V-groove
there, a bore, a bracket, a pair of bearings. The 0.5 mm marker is the instrument
instead -- on a 2 mm pin face it is a quarter of the width.

THE AXIS DIRECTION IS NOT PICKED. It is the linear axis the rotary word turns about
(`--axis`, X for the A word), the convention `scaffold_gcode` and
`mew_frame.CylinderFrame` already share; the pick supplies a POINT on it.

Standalone VTK subprocess. Pure read of the STL; the only output is the small datum
file, written on every change and again on close, so closing never loses a nudge.

    python machine/bed_calibrator.py <part.stl> --out datum.txt [--datum z,x,y]
        [--axis x] [--mandrel-diameter 3.0] [--mandrel-length 50]
        [--pick bed|tip|joint|home|seat]
    python machine/bed_calibrator.py --pick zero --out zero.txt
        (--zero-plate W,H,T | --zero-disc D,T | --zero-rod D,L) [--datum z,x,y]

The output is ALWAYS "z,x,y" whatever is picked -- one format, so a caller cannot read
a tip file as if it were a bed file (`gui_buildplate._read_datum_triple`).

INTERIM -- `--pick home` only. For now the home is a point picked on the
C axis, until a proper homing method replaces it. This mode is that stopgap and
nothing more. It is NOT a homing model
and NOT a datum ladder like the 4-axis rod's surface/axis choice; when the real one
lands, delete this mode, its branches below and the Machine page's row that drives it.
"""

if __name__ == "__main__":  # run as a script: put the program's folders on sys.path
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "engine"))
    import app_paths  # noqa: E402,F401

import argparse
import os
import sys

import numpy as np
import vtk
from vtk.util import numpy_support

# for `find_tip` ONLY, so the window seeds tip mode from the very number the
# machine model would use if these fields were left blank -- one auto-detect, not
# a second one that can disagree with it. Pure geometry + numpy, no VTK, no Tk.
import machine_model

# Launched from the GUI's Calibrate button, which from a desktop shortcut runs under
# pythonw.exe -- no console, so sys.stdout/sys.stderr are None and an unguarded write
# would crash the window before it opens. Route None streams to a null sink.
for _std in ("stdout", "stderr"):
    if getattr(sys, _std, None) is None:
        try:
            setattr(sys, _std, open(os.devnull, "w"))
        except OSError:
            pass

_AX = {"x": 0, "y": 1, "z": 2}

#: Nudge steps, mm. Spans the range the job actually needs: 0.01 is finer than
#: any holder is machined, 5 gets across a plate in a few presses.
STEPS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0)
DEFAULT_STEP = 4                                  # 0.5 mm

#: Datum marker DIAMETER, mm, in rotary mode. Deliberately absolute rather than a
#: fraction of the part: it judges whether the point is centred on a pin face a couple
#: of mm across, and the old `0.02 * diagonal` drew a 2 mm ball on this holder and a
#: 6 mm one on a bed assembly. On a flat bed the scaled marker is kept, because there
#: the job is to find a corner on a large assembly, not to centre on a face.
MARKER_MM = 0.5

#: (KeySym, axis index, sign) for the nudge keys. Arrows work the two axes you
#: are looking at in the default camera -- along the rod and up -- and PageUp /
#: PageDown take the third. None of these are bound by
#: vtkInteractorStyleTrackballCamera, whose own keys are r/w/s/e/q/f/3.
NUDGE = {"Left": (0, -1), "Right": (0, +1),
         "Up": (2, +1), "Down": (2, -1),
         "Prior": (1, +1), "Next": (1, -1)}


#: --pick zero on a PLATE, seen from above: the arrows move across its face
#: (X / Y) and PageUp / PageDown move off it (Z).
ZERO_PLATE_NUDGE = {"Left": (0, -1), "Right": (0, +1),
                    "Up": (1, +1), "Down": (1, -1),
                    "Prior": (2, +1), "Next": (2, -1)}


#: The read-out's line after a P that hit no part of the STL.
MISSED_TEXT = ("P found no part under the cursor. Hover the part itself and "
               "press P again; the datum is unchanged.")


def _triple(s):
    try:
        z, x, y = (float(v) for v in s.split(","))
        return z, x, y
    except (ValueError, AttributeError):
        return None


def positionable(ax, tip):
    """Whether the arrows move the datum in this mode.

    Yes wherever the target is a POINT that has to be crept up on: the mandrel
    axis (a line in air) and the nozzle tip (the centre of a bore, which is a
    hole -- a click there hits nothing). No for a flat bed corner, which is
    clickable by definition and has nothing to creep toward."""
    return ax is not None or bool(tip)


def seeds_spin_face(joint, axis, seat=False):
    """Whether this window opens on `find_spin_face`'s guess.

    ONLY about Z, and only for the two picks that mean a point on that body's
    mounting FACE. A tilt joint's axis is a bearing line through the side of a
    cradle and there is no face whose centre it is, so it picks cold rather than
    opening on a number that would be plausible and wrong. Named as a function,
    not written inline, so the gate can be checked against the cases it refuses.

    The SEAT is seeded by that face for a better reason than the axis is:
    `find_spin_face` returns the centre of the body's mounting face, and a
    collector bolted to the middle of it is where one lands by default. The axis
    pick BORROWS that answer because a round plate's face centre is also its
    spin axis; the seat pick IS that answer."""
    return bool(joint or seat) and str(axis or "").strip().lower() == "z"


def find_spin_face(points, tol=0.5):
    """Seed for a joint that spins about Z: the CENTRE of the body's mounting
    face, as `(z, x, y)` -- or None on degenerate input.

    The collector goes on the top face of the C body, which should be its max
    Z position, and auto-detecting it must never stop a manual pick.
    So this is a SEED, never an answer: the window opens on it, writes nothing until
    the user moves or
    clicks, and P still drops the axis wherever the cursor is.

    TWO DELIBERATE CHOICES, and both are about not shipping a plausible guess.

    The face is `machine_model.find_bed_top_face`'s, not plain `max(z)`. That
    function already refuses a small raised nub and a thin wide rail, taking
    the highest band that is large in BOTH XY directions -- which is exactly the
    objection to a raw highest-points centroid, that a boss, a clamp or a
    fixture wins it. One auto-detect for "the top face of a body", not a second
    one that can disagree with it.

    The point in that face is the MIDPOINT OF ITS XY BOUNDING BOX, not the
    centroid of its vertices. A spin plate is round, so the bbox midpoint IS its
    axis exactly, while a vertex centroid drifts toward whatever is finely
    tessellated -- a bolt circle, a keyway, an engraved label. `find_tip` can
    use a centroid because a nozzle's lowest ring is symmetric by construction;
    a mounting face is not.

    This is NOT a geometry FIT. A cylinder fit was built for the mandrel and
    removed the same day (see this module's docstring); nothing here solves for
    a shape. It reads a face the software already knows how to find and takes
    the centre of its extent.
    """
    pts = np.asarray(points, float)
    if pts.size == 0:
        return None
    top_z = float(machine_model.find_bed_top_face(pts, tol=tol)[0])
    band = pts[np.abs(pts[:, 2] - top_z) <= tol]
    if band.size == 0:
        return None
    return (top_z,
            0.5 * float(band[:, 0].min() + band[:, 0].max()),
            0.5 * float(band[:, 1].min() + band[:, 1].max()))


def _floats(text, n):
    try:
        vals = [float(v) for v in str(text).split(",")]
    except (TypeError, ValueError):
        return None
    return vals if len(vals) == n and all(v > 0 for v in vals) else None


def zero_collector_polydata(args):
    """The collector a custom zero is picked on, built from its SIZE, as
    triangles in the Setup page's frame -- or None without a usable size.

    The plate's frame is `gui_buildplate._plate_footprint_xy`'s: X/Y from its
    bottom-left corner, Z from its bottom face, so a picked point needs no
    conversion on the way back. A wafer sits in the same diameter-sized square.
    The mandrel's near end is at X0, on its axis."""
    plate = _floats(args.zero_plate, 3)
    disc = _floats(args.zero_disc, 2)
    rod = _floats(args.zero_rod, 2)
    xf = vtk.vtkTransform()
    if plate is not None:
        w, h, t = plate
        src = vtk.vtkCubeSource()
        src.SetBounds(0.0, w, 0.0, h, 0.0, t)
    elif disc is not None:
        d, t = disc
        src = vtk.vtkCylinderSource()
        src.SetRadius(d / 2.0)
        src.SetHeight(t)
        src.SetResolution(96)
        xf.Translate(d / 2.0, d / 2.0, t / 2.0)
        xf.RotateX(90.0)                        # +Y -> +Z
    elif rod is not None:
        d, length = rod
        src = vtk.vtkCylinderSource()
        src.SetRadius(d / 2.0)
        src.SetHeight(length)
        src.SetResolution(96)
        xf.Translate(length / 2.0, 0.0, 0.0)
        xf.RotateZ(-90.0)                       # +Y -> +X
    else:
        return None
    tf = vtk.vtkTransformPolyDataFilter()
    tf.SetInputConnection(src.GetOutputPort())
    tf.SetTransform(xf)
    tri = vtk.vtkTriangleFilter()
    tri.SetInputConnection(tf.GetOutputPort())
    tri.Update()
    return tri.GetOutput()


def nudge(datum, axis_index, delta):
    """Move a (z, x, y) datum by `delta` mm along world axis 0/1/2 (x/y/z).

    The datum triple is stored Z-FIRST -- the order the GUI's three fields and
    the datum file have always used -- while every geometric index in this file
    is the ordinary x=0, y=1, z=2. Converting in one named place is what stops a
    nudge on Y quietly moving Z."""
    z, x, y = datum
    p = [x, y, z]
    p[axis_index] += float(delta)
    return (p[2], p[0], p[1])


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("bed", nargs="?", default=None,
                    help="bed / mandrel-holder STL path (not with --pick zero)")
    ap.add_argument("--out", required=True, help="file to write the picked 'z,x,y'")
    ap.add_argument("--datum", default=None, help="current datum 'z,x,y' to show")
    ap.add_argument("--axis", default=None, choices=("x", "y", "z"),
                    help="rotary axis: draw the mandrel and enable positioning")
    ap.add_argument("--pick", default="bed",
                    choices=("bed", "tip", "joint", "home", "seat", "zero"),
                    help="what the three numbers mean: a bed corner, the nozzle "
                         "tip in the head STL's frame, (with --axis) a point "
                         "on a rotary JOINT's axis in that body's STL frame, "
                         "(with --axis) the COLLECTOR SEAT -- where the "
                         "collector's bottom edge rests on that body -- or "
                         "(with --axis) G-CODE ZERO, where the nozzle tip "
                         "sits at G0 X0 Y0 Z0, in that same frame")
    # THE COLLECTOR, drawn so a seat or a home is picked against the part rather
    # than against an empty table, chosen over picking blind: the
    # zero is often at the edge of the collector or off it entirely, so the
    # travel fibre does not cross an important part of the print -- a judgement you cannot make without seeing
    # where the collector's edge is. The five words are the Surface designer's
    # own fields and go straight to `scaffold_gcode.surface_frame`, the ONE
    # builder, so the dome in this window and the dome in the file are the same
    # dome.
    ap.add_argument("--collector", default=None,
                    help="collector shape to draw: dome|bowl|cone|torus")
    ap.add_argument("--collector-radius", type=float, default=10.0,
                    help="arc / base radius, mm")
    ap.add_argument("--collector-ring", type=float, default=0.0,
                    help="torus ring radius, mm")
    ap.add_argument("--collector-half-angle", type=float, default=20.0,
                    help="cone half-angle, degrees")
    ap.add_argument("--collector-span", default="0,12",
                    help="meridian span 'from,to', mm -- the part of the "
                         "surface that physically exists")
    ap.add_argument("--collector-seat", default=None,
                    help="with --pick home: the ALREADY PICKED seat 'z,x,y', so "
                         "the collector is drawn where it actually stands. "
                         "Ignored with --pick seat, where the marker IS the "
                         "seat and the collector follows it")
    ap.add_argument("--joint-label", default=None,
                    help="with --pick joint: what to call this joint in the "
                         "read-out, e.g. 'C -- spins the collector'")
    # --pick zero: the collector to draw, by size, in the Setup page's frame
    ap.add_argument("--zero-plate", default=None,
                    help="with --pick zero: a rectangular plate 'W,H,T', mm")
    ap.add_argument("--zero-disc", default=None,
                    help="with --pick zero: a round wafer 'D,T', mm")
    ap.add_argument("--zero-rod", default=None,
                    help="with --pick zero: the mandrel 'D,L', mm")
    ap.add_argument("--mandrel-diameter", type=float, default=3.0,
                    help="drawn rod diameter, mm (the 4-axis designer's)")
    ap.add_argument("--mandrel-length", type=float, default=None,
                    help="drawn rod length, mm (the Machine page's rod length)")
    ap.add_argument("--shot", default=None,
                    help="render the scene offscreen to this PNG and exit -- no "
                         "window, no interactor. The verification path: what it "
                         "draws is what the window draws, because it IS this "
                         "function, rather than a harness that mirrors it.")
    ap.add_argument("--nudge", default=None,
                    help="with --shot: 'axis:mm' steps applied through `nudge` "
                         "before rendering, e.g. 'z:0.5'. Axis is x|y|z.")
    args = ap.parse_args(argv[1:])
    ax = _AX.get(args.axis) if args.axis else None
    tip = ax is None and args.pick == "tip"
    # A rotary JOINT of a collector table: the axis line, the marker and the
    # arrows, and deliberately no rod. `--axis` says which way the line runs;
    # without one there is no line to draw, so the flag is required together.
    joint = ax is not None and args.pick == "joint"
    # THE G-CODE HOME (INTERIM). Drawn exactly like a joint pick -- axis line,
    # 0.5 mm marker, no rod -- because it is a point on the SAME line, picked on
    # the same body. Only the read-out and the seed rule differ, and those are
    # the two places it must not read as a rotation centre. `line_only` is what
    # the two modes share, so a later deletion of this mode is a deletion and
    # not an untangling.
    home = ax is not None and args.pick == "home"
    # THE COLLECTOR SEAT. Drawn like a joint pick -- axis line, 0.5 mm marker,
    # no rod -- because it is a point on the same body, plus the collector
    # itself standing on the marker. It is NOT the joint's pivot: the pivot is a
    # point on the axis of ROTATION and this is the point the part STANDS on,
    # which is the distinction that let a dome hang above the table.
    seat = ax is not None and args.pick == "seat"
    line_only = joint or home or seat
    # A CUSTOM G-CODE ZERO, on the collector drawn from its size. No --axis:
    # it is a point on a part, not on a line, so no axis line and no rod.
    zero = ax is None and args.pick == "zero"
    zero_rod = zero and bool(args.zero_rod)

    if zero:
        poly = zero_collector_polydata(args)
        if poly is None:
            sys.stderr.write("bed_calibrator: --pick zero needs --zero-plate, "
                             "--zero-disc or --zero-rod\n")
            return 1
    elif not args.bed:
        sys.stderr.write("bed_calibrator: no STL given\n")
        return 1
    else:
        reader = vtk.vtkSTLReader()
        reader.SetFileName(args.bed)
        reader.Update()
        poly = reader.GetOutput()
    if poly.GetNumberOfPoints() == 0:
        sys.stderr.write("bed_calibrator: empty / unreadable STL\n")
        return 1
    pts = numpy_support.vtk_to_numpy(poly.GetPoints().GetData())
    lo3, hi3 = pts.min(axis=0), pts.max(axis=0)
    zlo, zhi = float(lo3[2]), float(hi3[2])
    diag = float(np.linalg.norm(hi3 - lo3)) or 1.0
    rod_r = max(float(args.mandrel_diameter), 1e-3) / 2.0
    rod_len = (float(args.mandrel_length) if args.mandrel_length
               else float(hi3[ax] - lo3[ax]) if ax is not None else 1.0)

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(poly)
    actor = vtk.vtkActor()                       # drawn UNSCALED -> picks are STL units
    actor.SetMapper(mapper)
    actor.GetProperty().SetColor(0.60, 0.65, 0.72)
    actor.GetProperty().SetSpecular(0.2)

    ren = vtk.vtkRenderer()
    ren.SetBackground(0.11, 0.11, 0.13)
    ren.AddActor(actor)
    win = vtk.vtkRenderWindow()
    win.AddRenderer(ren)
    win.SetSize((1400, 900) if args.shot else (960, 720))
    win.SetWindowName("G-code zero" if (home or zero) else
                      "Rotary axis" if joint else
                      "Mandrel position" if ax is not None else
                      "Nozzle tip" if tip else "Bed calibration")
    if args.shot:
        win.SetOffScreenRendering(1)             # no window, no interactor loop
    iren = vtk.vtkRenderWindowInteractor()
    iren.SetRenderWindow(win)
    iren.SetInteractorStyle(vtk.vtkInteractorStyleTrackballCamera())

    # The datum marker. In rotary mode it is a fixed 0.5 mm across so it can be
    # judged against the face it is meant to be centred on (see MARKER_MM).
    marker = vtk.vtkSphereSource()
    marker.SetThetaResolution(24)
    marker.SetPhiResolution(24)
    marker.SetRadius(MARKER_MM / 2.0 if (ax is not None or tip)
                     else 0.01 * diag if zero else 0.02 * diag)
    msrc = vtk.vtkPolyDataMapper()
    msrc.SetInputConnection(marker.GetOutputPort())
    mact = vtk.vtkActor()
    mact.SetMapper(msrc)
    mact.GetProperty().SetColor(1.0, 0.25, 0.20)
    mact.GetProperty().SetLighting(False)
    mact.SetVisibility(False)
    # THE MARKER IS THE INSTRUMENT, so it is never allowed to be hidden by what it is
    # being judged against. The rod seats its NEAR FACE on the datum, so on a plain
    # single-layer render the marker sits exactly inside the rod's end cap and vanishes
    # the moment the rod is shown. A second render layer sharing the camera draws it
    # over everything instead of fighting the depth buffer with opacity.
    top = vtk.vtkRenderer()
    top.SetLayer(1)
    top.InteractiveOff()
    top.SetActiveCamera(ren.GetActiveCamera())
    top.AddActor(mact)
    win.SetNumberOfLayers(2)
    win.AddRenderer(top)

    # The mandrel axis, run past both ends of the part. Drawn in BOTH modes: on a
    # bed it shows nothing useful, so it is rotary-only.
    line_src = vtk.vtkLineSource()
    line_map = vtk.vtkPolyDataMapper()
    line_map.SetInputConnection(line_src.GetOutputPort())
    line_act = vtk.vtkActor()
    line_act.SetMapper(line_map)
    line_act.GetProperty().SetColor(1.0, 0.72, 0.15)
    line_act.GetProperty().SetLineWidth(2.0)
    line_act.GetProperty().SetLighting(False)
    line_act.SetVisibility(False)
    ren.AddActor(line_act)

    # The mandrel itself, at the job's diameter, its NEAR FACE at the datum --
    # so what you are looking at is the rod you are about to print on, in the
    # place it will be. This is the "show me where the mandrel ends up without
    # generating a scaffold" view.
    rod_src = vtk.vtkCylinderSource()
    rod_src.SetResolution(48)
    rod_src.CappingOn()
    rod_src.SetRadius(rod_r)
    rod_src.SetHeight(rod_len)
    rod_map = vtk.vtkPolyDataMapper()
    rod_map.SetInputConnection(rod_src.GetOutputPort())
    rod_act = vtk.vtkActor()
    rod_act.SetMapper(rod_map)
    rod_act.GetProperty().SetColor(0.95, 0.42, 0.28)
    rod_act.GetProperty().SetOpacity(0.45)
    rod_act.SetVisibility(False)
    ren.AddActor(rod_act)

    # THE COLLECTOR ITSELF, standing on the seat. Built through
    # `scaffold_gcode.surface_frame` -- the ONE builder the designer and the
    # emitter both use -- and aimed by `machine_model.collector_mount_rotation`,
    # the same swing the kinematics mount it by, so this window cannot show a
    # dome standing up while the player lays it on its side.
    coll_act = None
    coll_frame = None
    coll_axial = 0.0
    if args.collector and ax is not None:
        try:
            import scaffold_gcode
            import stl_viewer
            span = tuple(float(v) for v in args.collector_span.split(","))
            coll_frame = scaffold_gcode.surface_frame(
                args.collector, radius=args.collector_radius,
                ring=args.collector_ring,
                half_angle_deg=args.collector_half_angle, span=span)
            coll_axial = float(coll_frame.mount_axial())
            coll_act = stl_viewer.revolution_actor(coll_frame)
        except Exception as exc:                       # noqa: BLE001
            sys.stderr.write("bed_calibrator: could not draw the "
                             "collector: %s\n" % (exc,))
            coll_act = None
    if coll_act is not None:
        rot = machine_model.collector_mount_rotation(args.axis)
        if rot is not None:
            coll_act.RotateWXYZ(rot[0], *rot[1])
        # THE ROD'S COLOUR, not the plate's. `revolution_actor` wears
        # `PLATE_COLOR` so that the three collectors read as one material in the
        # PLAYER's scene -- but here the body underneath is already that colour,
        # and a 10 mm dome in machine grey on a 500 mm grey plate reads as a
        # dimple in the plate rather than as a part standing on it. The rod in
        # this same window is orange for the identical reason.
        coll_act.GetProperty().SetColor(0.95, 0.42, 0.28)
        coll_act.GetProperty().SetOpacity(0.75)
        coll_act.SetVisibility(False)
        ren.AddActor(coll_act)
    #: where the collector stands, in this STL's frame -- the marker in seat
    #: mode, and the ALREADY PICKED seat in home mode (where the marker is the
    #: nozzle, not the part). None means there is nothing to stand it on and it
    #: is not drawn, which is the honest answer to "pick your zero against the
    #: collector" before a seat exists.
    coll_seat = _triple(args.collector_seat) if not seat else None

    info = vtk.vtkTextActor()
    info.GetTextProperty().SetFontSize(15)
    info.GetTextProperty().SetColor(0.92, 0.92, 0.92)
    info.SetDisplayPosition(12, 12)
    ren.AddActor2D(info)

    picker = vtk.vtkCellPicker()
    picker.SetTolerance(0.002)
    state = {"datum": _triple(args.datum), "step": DEFAULT_STEP,
             "rod": not line_only, "touched": False, "seeded": False,
             "missed": False}

    # A NOZZLE IS HOLLOW, so the one point you most want -- the centre of the tip face
    # -- is the one place a ray hits nothing at all; you can only click the annulus
    # around the bore. So tip mode does not start from a click. It starts from
    # `find_tip`, the SAME auto-detect the machine model falls back to: the centroid of
    # the lowest points, which on a hollow nozzle IS the bore centre. Then the arrows
    # move it, and the click is demoted to a coarse way of getting near something else.
    if tip and state["datum"] is None:
        auto = machine_model.find_tip(pts)
        if auto is not None:
            state["datum"] = (float(auto[2]), float(auto[0]), float(auto[1]))
            state["seeded"] = True

    # A JOINT THAT SPINS ABOUT Z gets the same treatment for a different reason.
    # Its mounting face is the collector's seat and its centre is the spin axis,
    # so opening the window on `find_spin_face` starts the user at the answer
    # for a plain round plate. Not saved until touched, exactly as above.
    #
    # ONLY for a Z joint. A tilt joint's axis is a BEARING LINE through the side
    # of a cradle, and there is no face whose centre it is -- it picks cold, and
    # inventing a guess there would be the plausible-and-wrong number this whole
    # slice exists to avoid. And unlike tip mode, P is NOT demoted here: a
    # nozzle is hollow so a ray hits nothing at its centre, while a mounting
    # face is solid and clicking it works. Auto-detect seeds it, and a manual
    # pick always stays possible.
    if seeds_spin_face(joint, args.axis, seat) and state["datum"] is None:
        auto = find_spin_face(pts)
        if auto is not None:
            state["datum"] = auto
            state["seeded"] = True

    def _write():
        # NOTHING IS WRITTEN UNTIL THE USER TOUCHES SOMETHING. The seed above is what
        # the software already does on its own, so writing it would silently convert
        # "auto-detect" into a hardcoded override just for opening the window -- and the
        # page's blank fields ARE the auto-detect switch.
        if state["datum"] is not None and state["touched"]:
            try:
                with open(args.out, "w") as fh:
                    fh.write("{},{},{}".format(*state["datum"]))
            except OSError as exc:
                sys.stderr.write(f"bed_calibrator: could not write datum: {exc}\n")

    def _place():
        """Put the marker, the axis and the rod at the current datum."""
        d = state["datum"]
        if d is None:
            return
        p = np.array([d[1], d[2], d[0]])          # (z,x,y) -> (x,y,z)
        mact.SetPosition(*p)
        mact.SetVisibility(True)
        if ax is None:
            return
        a, b = p.copy(), p.copy()
        # HOW FAR THE LINE RUNS PAST THE PART, and the two modes want different
        # answers. A MANDREL holder is long along its own rotary axis, so 8% of
        # that extent is a visible overhang. A collector table's spin body is a
        # PLATE: it is ~60 mm thick and ~500 mm across, so 8% of its Z extent is
        # a 5 mm stub you cannot aim with -- measured on the development rig's C body, whose
        # Z range is [1046.155, 1106.080]. In joint mode the line is padded by a
        # quarter of the part's DIAGONAL instead, so it reads as a line through
        # the bore from any orbit. The mandrel's rule is untouched.
        pad = (0.25 * diag if line_only
               else 0.08 * float(hi3[ax] - lo3[ax] or 1.0))
        a[ax], b[ax] = float(lo3[ax]) - pad, float(hi3[ax]) + pad
        line_src.SetPoint1(*a)
        line_src.SetPoint2(*b)
        line_act.SetVisibility(True)
        # vtkCylinderSource builds about +Y; swing it onto the rotary axis, then
        # seat its NEAR face on the datum rather than its centre.
        rod_act.SetOrientation(0.0, 0.0, 0.0)
        if ax == 0:
            rod_act.RotateZ(-90.0)
        elif ax == 2:
            rod_act.RotateX(90.0)
        c = p.copy()
        c[ax] += rod_len / 2.0
        rod_act.SetPosition(*c)
        rod_act.SetVisibility(state["rod"])
        # THE COLLECTOR STANDS ON ITS SEAT. Its own origin is the seat walked
        # back up the joint's axis by `mount_axial` -- one subtraction, the same
        # one `machine_model.collector_origin` makes, so what is drawn here is
        # where the part will actually be. In seat mode the seat is the marker,
        # so it follows every nudge; in home mode it is the fixed pick handed in
        # and the marker moves against it.
        if coll_act is not None:
            base = p.copy() if seat else (None if coll_seat is None else
                                          np.array([coll_seat[1], coll_seat[2],
                                                    coll_seat[0]]))
            if base is None:
                coll_act.SetVisibility(False)
            else:
                o = base.copy()
                o[ax] -= coll_axial
                coll_act.SetPosition(*o)
                coll_act.SetVisibility(True)

    def _refresh():
        d = state["datum"]
        cur = (("not set -- hover the nozzle tip and press P" if tip else
                "not set -- press P near the axis, then creep along it" if home
                else
                "not set -- hover the collector's mounting face and press P"
                if seat else
                "not set -- hover the mandrel seat and press P") if d is None else
               f"Z={d[0]:.4f}   X={d[1]:.4f}   Y={d[2]:.4f}")
        if tip:
            # How far above the STL's LOWEST point the pick sits. On a printhead that
            # number is the whole check: the nozzle tip is the bottom of the model, so
            # anything but ~0 means the click landed on a shroud, a duct or a probe.
            drop = ("" if d is None else
                    "  point sits %.4f mm above the STL's lowest point\n"
                    % (d[0] - zlo))
            head = ("Nozzle tip\n"
                    "  Orbit (drag) to the nozzle. A nozzle is HOLLOW, so the\n"
                    "  centre of the tip face cannot be clicked -- creep to it.\n"
                    "  arrows       move across / up + down\n"
                    "  PgUp PgDn    move the third axis\n"
                    f"  [ ]          step size   (now {STEPS[state['step']]} mm)\n"
                    f"  P            jump to the cursor ({MARKER_MM} mm marker)\n"
                    + ("  SHOWING THE AUTO-DETECTED TIP. Nothing is saved and\n"
                       "  the page stays on auto-detect until you move it.\n"
                       if state["seeded"] else
                       "  Blank the fields on the page to return to auto-detect.\n")
                    + drop)
        elif home:
            # INTERIM (see the module docstring). Every line here has to say
            # NOZZLE, not axis: the window is the joint picker's window and the
            # marker is the joint picker's marker, so the only thing separating
            # "a point on the C axis" from "the rotation centre" is this text.
            # The axis line is still named, because creeping ALONG it is how the
            # point is placed.
            head = ("G-code zero  (INTERIM)\n"
                    "  Where the NOZZLE TIP sits at G0 X0 Y0 Z0.\n"
                    "  Orbit (drag), then creep the marker ALONG the yellow\n"
                    "  line -- this joint's axis of rotation, along "
                    f"{args.axis.upper()}.\n"
                    f"  P            jump to the cursor "
                    f"({MARKER_MM} mm marker)\n"
                    "  arrows       move across / up + down\n"
                    "  PgUp PgDn    move the third axis\n"
                    f"  [ ]          step size   (now {STEPS[state['step']]} mm)\n"
                    + ("  the collector is drawn on its picked seat: put the\n"
                       "  marker where the nozzle should START, which is often\n"
                       "  off the part so the travel fibre misses the print.\n"
                       if coll_act is not None else
                       "  NO COLLECTOR IS DRAWN. Pick the collector SEAT first\n"
                       "  and this window will show the part you zero against.\n")
                    + "  This is NOT the rotation centre and NOT the collector\n"
                    "  seat. Nothing is saved until you move it or press P.\n")
        elif seat:
            # THE PART, not the axis. Every line has to say COLLECTOR: this is
            # the joint picker's window and the joint picker's marker, so the
            # text is the only thing separating "where the collector stands"
            # from "where it spins". The drawn collector is the real instrument
            # -- it follows the marker, so you are aiming a PART at a face
            # rather than a dot at a coordinate.
            head = ("Collector seat\n"
                    "  Where the COLLECTOR'S BOTTOM EDGE rests on this body.\n"
                    "  Orbit (drag) to its mounting face, then press P.\n"
                    f"  P            drop the seat at the cursor "
                    f"({MARKER_MM} mm marker)\n"
                    "  arrows       move across / up + down\n"
                    "  PgUp PgDn    move the third axis\n"
                    f"  [ ]          step size   (now {STEPS[state['step']]} mm)\n"
                    + ("  the %s is drawn standing on the marker (its edge is\n"
                       "  %.4f mm up its own axis).\n"
                       % (args.collector, coll_axial)
                       if coll_act is not None else
                       "  NO COLLECTOR SHAPE PASSED, so none is drawn: the\n"
                       "  marker alone is the seat.\n")
                    + "  This is NOT the axis of rotation.\n"
                    + ("  SHOWING A GUESS: the centre of this body's top face.\n"
                       "  Nothing is saved, and the page stays uncalibrated,\n"
                       "  until you move it or press P.\n"
                       if state["seeded"] else ""))
        elif joint:
            # No rod, and no "M". A collector table turns no mandrel, so a rod
            # here would be one that is not on the machine. The instrument is
            # the AXIS LINE through the marker: orbit until you are looking
            # down the joint's bore, then creep the line onto its centre.
            head = (f"Rotary axis -- {args.joint_label or args.axis.upper()}\n"
                    "  Orbit (drag) to the joint's bore / bearing.\n"
                    f"  P            drop the axis at the cursor "
                    f"({MARKER_MM} mm marker)\n"
                    "  arrows       move across / up + down\n"
                    "  PgUp PgDn    move the third axis\n"
                    f"  [ ]          step size   (now {STEPS[state['step']]} mm)\n"
                    "  the yellow line is this joint's axis of rotation, "
                    f"along {args.axis.upper()}.\n"
                    # THE GUESS IS NAMED. It is the centre of the body's top
                    # face, which is right on a plain round plate and wrong on
                    # one whose highest large face is a fixture -- and a wrong
                    # seed that is named is visible, where a wrong seed that is
                    # not reads as a measurement.
                    + ("  SHOWING A GUESS: the centre of this body's top face.\n"
                       "  Nothing is saved, and the page stays uncalibrated,\n"
                       "  until you move it or press P.\n"
                       if state["seeded"] else ""))
        elif zero:
            # the point is read in the Setup page's words: from the plate's
            # bottom-left corner, or along the mandrel from its near end
            keys = ("  P            jump to the cursor\n"
                    + ("  Left Right   move along the mandrel\n" if zero_rod else
                       "  arrows       move across X / Y\n"
                       "  PgUp PgDn    move up / down\n")
                    + f"  [ ]          step size   (now {STEPS[state['step']]} mm)\n")
            head = ("G-code zero  --  custom\n"
                    "  Orbit (drag), hover the point your printer is zeroed on\n"
                    "  (a mark, a clip, an edge) and press P.\n" + keys
                    + "  Close the window to keep the point.\n")
            if d is not None:
                cur = (f"X = {d[1]:.3f} mm along the mandrel from its near end"
                       if zero_rod else
                       f"X = {d[1]:.3f}  Y = {d[2]:.3f} mm from the plate's "
                       f"bottom-left corner,  Z = {d[0]:.3f} mm above its "
                       f"bottom face (top face Z = {zhi:.3f})")
            else:
                cur = "not set -- hover the collector and press P"
        elif ax is None:
            head = ("Bed calibration\n"
                    "  Orbit (drag) to the print surface.\n"
                    "  Hover its BOTTOM-LEFT corner and press P to set the datum.\n"
                    "  Close the window when the marker sits on the corner.\n")
        else:
            head = ("Mandrel position\n"
                    "  Orbit (drag) to the mandrel seat.\n"
                    f"  P            drop the axis at the cursor "
                    f"({MARKER_MM} mm marker)\n"
                    "  arrows       move along the rod / up + down\n"
                    "  PgUp PgDn    move sideways\n"
                    f"  [ ]          step size   (now {STEPS[state['step']]} mm)\n"
                    f"  M            rod {'on' if state['rod'] else 'off'}"
                    f"   (ø{2 * rod_r:.3f} x {rod_len:.3f} mm,"
                    " near face on the datum)\n")
        info.SetInput(head
                      + ("" if zero else
                         f"  STL Z-range: [{zlo:.3f}, {zhi:.3f}]\n")
                      + f"  {'zero' if zero else 'datum'}: {cur}"
                      + (f"\n  {MISSED_TEXT}" if state["missed"] else ""))
        win.Render()

    def _on_key(_obj, _ev):
        k = iren.GetKeySym()
        if k in ("p", "P"):
            x, y = iren.GetEventPosition()
            if picker.Pick(x, y, 0, ren) and picker.GetActor() is actor:
                px, py, pz = picker.GetPickPosition()
                state["datum"] = (pz, px, py)      # STL frame (actor is unscaled)
                state["touched"] = True
                state["seeded"] = False
                state["missed"] = False
                _place()
                _write()
                _refresh()
            else:
                # P over empty space (or the marker, the rod) used to do
                # nothing at all: the read-out says so, and the datum is kept
                state["missed"] = True
                sys.stdout.write("bed_calibrator: %s\n" % MISSED_TEXT)
                sys.stdout.flush()
                _refresh()
            return
        if not (positionable(ax, tip) or zero):
            return
        if k in NUDGE and state["datum"] is not None:
            idx, sign = (ZERO_PLATE_NUDGE if zero and not zero_rod
                         else NUDGE)[k]
            state["datum"] = nudge(state["datum"], idx, sign * STEPS[state["step"]])
            state["touched"] = True
            state["seeded"] = False
            _place()
            _write()
            _refresh()
            return
        if k in ("bracketleft", "bracketright"):
            state["step"] = max(0, min(len(STEPS) - 1,
                                       state["step"] + (1 if k == "bracketright"
                                                        else -1)))
            _refresh()
            return
        if k in ("m", "M") and not line_only:
            state["rod"] = not state["rod"]
            rod_act.SetVisibility(state["rod"] and state["datum"] is not None)
            _refresh()

    iren.AddObserver("KeyPressEvent", _on_key)
    ren.ResetCamera()
    # look slightly down so the bed top reads as a surface, not edge-on
    cam = ren.GetActiveCamera()
    cam.Azimuth(30)
    cam.Elevation(-35)
    ren.ResetCameraClippingRange()
    if args.shot:
        if args.nudge and state["datum"] is not None:
            which, _, mm = args.nudge.partition(":")
            state["datum"] = nudge(state["datum"], _AX[which.strip().lower()],
                                   float(mm))
            # stands in for a real key press, so it must mark the datum touched
            # exactly as one does -- otherwise the verification path exercises a
            # write gate the window does not have.
            state["touched"] = True
            state["seeded"] = False
        _place()
        _refresh()
        win.Render()
        w2i = vtk.vtkWindowToImageFilter()
        w2i.SetInput(win)
        w2i.Update()
        png = vtk.vtkPNGWriter()
        png.SetFileName(args.shot)
        png.SetInputConnection(w2i.GetOutputPort())
        png.Write()
        _write()
        sys.stdout.write(f"bed_calibrator: wrote {args.shot}\n")
        return 0
    iren.Initialize()
    _place()
    _refresh()
    win.Render()
    iren.Start()
    _write()                                        # persist the last pick on close
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
