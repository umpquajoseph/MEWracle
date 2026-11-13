#!/usr/bin/env python3
"""
stl_viewer.py

The offscreen render backend and the shared VTK actor library.

**The interactive `view()` in this file is DEPRECATED**: the unified
Viewer page is the current viewer, and STL, deviation and micrograph are in-scene
modes of it. The classic developer UI still opens the window
(`gui_viewers.view_stl`) and that path is left working -- **do not add features
to it.** The modern shell has no route there.

Three things here ARE current, and four other modules depend on them:

  1. Offscreen renders that never open a window -- `render_thumbnail` /
     `--thumbnail` (the Print History run thumbnail) and `render_shots` /
     `--shots` (multi-angle renders + shots.json, which capture_ui.py and the
     autonomy Stop gate run to LOOK at a result without a human).
  2. The actor + reader library -- `_load_centerlines`, `_centerline_actor`,
     `_stl_actor`, `_render_sides`, `_travel_actor`, `_binary_stl_polydata`
     (the numpy fast read), `_RAINBOW` /
     `_SOLID_COLOR`. Imported by gcode_player.py, micrograph_viewer.py and
     perf_bench.py; mirrored by gui_common.py.
  3. The deviation colouring helpers -- `_load_deviation`, `_deviation_lut`,
     `_deviation_actor`, `_deviation_scalar_bar`. The `--deviation` WINDOW is
     deprecated with `view()`, but gcode_player imports these to colour
     deviation inside the unified Viewer, so they outlive it.

Hand-launching the deprecated window still works:

    python viewers/stl_viewer.py path/to/model.stl

This runs as its own process because VTK's event loop would fight Tkinter's, and
because a huge mesh that exhausts memory then dies alone. Same reason capture_ui
shells out to `--shots` rather than importing it: that process already owns a
live Tk main window.
"""

if __name__ == "__main__":  # run as a script: put the program's folders on sys.path
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "engine"))
    import app_paths  # noqa: E402,F401

import glob
import math
import os
import sys

import vtk

from vtk_helpers import (fit_clipping_range, install_cad_camera,
                         origin_marker, projection_toggle)

# The upper-right ViewCube. Guarded like the gui_viewer_shell import: a
# hand-launched viewer without the modern-shell modules still opens, just
# without the gizmo.
try:
    import view_cube as _view_cube
except Exception:  # noqa: BLE001
    _view_cube = None

# Launched by the GUI, which from a desktop shortcut runs under pythonw.exe -- no
# console, so sys.stdout/sys.stderr are None and an unguarded write would crash the
# viewer before it opens. Route None streams to a null sink.
for _std in ("stdout", "stderr"):
    if getattr(sys, _std, None) is None:
        try:
            setattr(sys, _std, open(os.devnull, "w"))
        except OSError:
            pass

# Default per-scaffold colours: a rainbow by scaffold index (1-based). The
# stabilization base gets a neutral grey so it reads as scaffolding, not a layer.
# A user --colors entry overrides any of these by part name.
_RAINBOW = [
    (1.00, 0.15, 0.15),   # scaffold 1 - red
    (1.00, 0.55, 0.10),   # scaffold 2 - orange
    (0.95, 0.85, 0.10),   # scaffold 3 - yellow
    (0.20, 0.80, 0.25),   # scaffold 4 - green
    (0.25, 0.45, 1.00),   # scaffold 5 - blue
]
_STAB_COLOR = (0.62, 0.62, 0.66)
# neutral colour every scaffold collapses to when the colour toggle is off
_SOLID_COLOR = (0.80, 0.80, 0.86)

# Modal feed (mm/min) assumed until the first F word, for the print-time estimate
# below. Mirrors gcode_to_fibers.DEFAULT_FEED / gcode_player so the time shown
# here matches the generator's printout and the player's clock. Hard-coded (not
# imported) so the viewer stays light and never pulls in the heavier converter.
DEFAULT_FEED = 600.0


def _hex_to_rgb(s):
    """'#rrggbb' or 'rrggbb' -> (r, g, b) floats in 0..1, or None if malformed."""
    s = s.strip().lstrip("#")
    if len(s) != 6:
        return None
    try:
        return (int(s[0:2], 16) / 255.0, int(s[2:4], 16) / 255.0,
                int(s[4:6], 16) / 255.0)
    except ValueError:
        return None


def _parse_colors(spec):
    """Parse a `name=rrggbb,name=rrggbb` string into {part_name: (r,g,b)}.
    Unparseable entries are skipped, so a bad colour never sinks the viewer."""
    out = {}
    if not spec:
        return out
    for item in spec.split(","):
        if "=" not in item:
            continue
        name, hx = item.split("=", 1)
        rgb = _hex_to_rgb(hx)
        if rgb is not None:
            out[name.strip()] = rgb
    return out


def _scaffold_index(filepath):
    """The integer N from a `<stem>_scaffoldN.stl` sibling (0 if unparseable)."""
    tail = os.path.basename(filepath).rsplit("_scaffold", 1)[-1]
    try:
        return int(tail.split(".")[0])
    except ValueError:
        return 0


def _discover_parts(path):
    """Per-scaffold sibling STLs next to `path`, as an ordered list of
    (part_name, filepath): the stabilization base first (if any), then each
    scaffold by index. Empty when the run wrote no parts (single-scaffold or a
    plain g-code -> STL conversion), so the viewer falls back to one actor."""
    stem = os.path.splitext(path)[0]
    parts = []
    stab = stem + "_stab.stl"
    if os.path.isfile(stab):
        parts.append(("stab", stab))
    for fp in sorted(glob.glob(glob.escape(stem) + "_scaffold*.stl"),
                     key=_scaffold_index):
        parts.append((f"scaffold{_scaffold_index(fp)}", fp))
    return parts


def _color_for(name, colors):
    """Resolve a part's colour: an explicit --colors entry wins, else the rainbow
    by scaffold index, else the stabilization grey."""
    if name in colors:
        return colors[name]
    if name == "stab":
        return _STAB_COLOR
    if name.startswith("scaffold"):
        try:
            k = int(name[len("scaffold"):])
        except ValueError:
            k = 1
        return _RAINBOW[(k - 1) % len(_RAINBOW)]
    return (0.80, 0.80, 0.86)


def _part_label(name):
    """A friendly toggle-button label for a part name."""
    if name == "stab":
        return "stabilization"
    if name.startswith("scaffold"):
        return "scaffold " + name[len("scaffold"):]
    return name


# A binary-STL triangle record: facet normal + 3 vertices (float32) + 2-byte
# attribute, packed to exactly 50 bytes (align=False). Deliberately re-declared
# here rather than imported from gcode_to_fibers -- this viewer stays standalone
# and must not drag the converter (and numpy-heavy solver) in to open a file.
_STL_REC = None                     # built lazily, on first binary read
# Records per np.fromfile call. Bounds peak memory to the point array plus one
# 50 MB block; a test lowers it to force the loop to run many times.
_STL_READ_CHUNK = 1_000_000


def _binary_stl_polydata(path):
    """vtkPolyData for a BINARY STL, read with numpy, or None to fall back.

    vtkSTLReader parses the 50-byte records one at a time in C++; on an 824 MB
    fibre mesh that is ~1.9 s of the open. numpy reads the same bytes as one
    structured array and the triangle soup becomes a vtkCellArray with two
    arange()s -- no per-triangle work in either language.

    Returns None (and the caller falls back to vtkSTLReader) for anything this
    fast path is not certain about: an ASCII STL, a truncated file, or a size
    that does not match the header's triangle count exactly. That check is the
    whole safety argument -- a binary STL's length is fixed by its own count."""
    global _STL_REC
    import numpy as np
    from vtk.util import numpy_support

    size = os.path.getsize(path)
    if size < 84:
        return None
    with open(path, "rb") as fh:
        head = fh.read(84)
        if head[:5].lower() == b"solid" and size != 84 + 50 * int(
                np.frombuffer(head[80:84], dtype="<u4")[0]):
            return None                       # ASCII STL
        n_tri = int(np.frombuffer(head[80:84], dtype="<u4")[0])
        if n_tri <= 0 or size != 84 + 50 * n_tri:
            return None                       # truncated / not what it claims
        if _STL_REC is None:
            _STL_REC = np.dtype([("n", "<f4", 3), ("v0", "<f4", 3),
                                 ("v1", "<f4", 3), ("v2", "<f4", 3),
                                 ("attr", "<u2")])
        # Fill a preallocated point array in chunks: never holds the whole file
        # AND the whole point soup at once (which is what makes this cheaper on
        # memory than the reader it replaces, not just faster).
        allpts = np.empty((3 * n_tri, 3), dtype=np.float32)
        step = max(1, int(_STL_READ_CHUNK))
        done = 0
        while done < n_tri:
            m = min(step, n_tri - done)
            rec = np.fromfile(fh, dtype=_STL_REC, count=m)
            if len(rec) != m:
                return None
            block = allpts[3 * done:3 * (done + m)].reshape(m, 3, 3)
            block[:, 0, :] = rec["v0"]
            block[:, 1, :] = rec["v1"]
            block[:, 2, :] = rec["v2"]
            done += m

    pts = vtk.vtkPoints()
    pts.SetData(numpy_support.numpy_to_vtk(allpts, deep=1))
    offsets = np.arange(0, 3 * n_tri + 1, 3, dtype=np.int64)
    conn = np.arange(3 * n_tri, dtype=np.int64)
    cells = vtk.vtkCellArray()
    cells.SetData(numpy_support.numpy_to_vtkIdTypeArray(offsets, deep=1),
                  numpy_support.numpy_to_vtkIdTypeArray(conn, deep=1))
    poly = vtk.vtkPolyData()
    poly.SetPoints(pts)
    poly.SetPolys(cells)
    return poly


def _stl_actor(path, color):
    """A vtkActor for an STL file, or None if the file is missing."""
    if not os.path.isfile(path):
        return None
    mapper = vtk.vtkPolyDataMapper()
    poly = None
    try:
        poly = _binary_stl_polydata(path)
    except Exception:  # noqa: BLE001 -- any surprise falls back to the reader
        poly = None
    if poly is not None:
        mapper.SetInputData(poly)
    else:
        reader = vtk.vtkSTLReader()
        reader.SetFileName(path)
        # Skip the point-merging locator on load. By default vtkSTLReader runs
        # every triangle vertex through an incremental point locator to weld
        # coincident points -- O(V log V) and the bulk of the read time on a big
        # fiber mesh. We only display the mesh (flat-shaded straight off the STL
        # facets, no smooth normals), so welding buys nothing; turning it off
        # makes the load markedly faster at the cost of some extra (unshared)
        # points in memory. The numpy path above produces the same unwelded soup.
        reader.SetMerging(0)
        mapper.SetInputConnection(reader.GetOutputPort())
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetColor(*color)
    actor.GetProperty().SetSpecular(0.3)
    actor.GetProperty().SetSpecularPower(20)
    return actor


# The build plate ("print slide") reads best as a MATTE surface: high ambient +
# no specular so its shade stays constant as the camera orbits. A glossy or
# purely-diffuse flat plate sweeps a bright hotspot across itself under the
# camera headlight and looks like it "changes colour with angle". A neutral
# steel-blue is a distinct mid-tone that reads clearly as the surface BELOW
# without competing with the light scaffold above it.
PLATE_COLOR = (0.38, 0.45, 0.56)


def _matte_plate(actor):
    """Give a loaded plate actor a flat, angle-stable matte look (in place)."""
    if actor is None:
        return actor
    prop = actor.GetProperty()
    prop.SetColor(*PLATE_COLOR)
    prop.SetSpecular(0.0)
    prop.SetAmbient(0.55)
    prop.SetDiffuse(0.45)
    return actor


def revolution_polydata(frame, u_steps=96, v_steps=96):
    """A `vtkPolyData` of a `mew_frame.RevolutionFrame`'s BARE surface, in the
    collector's own frame -- the five-axis peer of the mandrel's rod.

    Drawn rather than loaded, for the reason `gcode_player._mandrel_body` gives
    for the rod: a machine.json would otherwise be carrying an STL of a shape the
    software already knows analytically, and the two could then disagree. THE
    FRAME IS ASKED FOR EVERY POINT (`to_world`), so the surface drawn under the
    fibre and the surface the fibre was laid on are the same surface by
    construction, not by two copies of a profile.

    The mesh is a (u, v) grid of quads split into triangles. `v` is arc length at
    the bare collector and its period is a FUNCTION of u (`period_v_at`), so each
    parallel is sampled on its OWN circumference -- sampling a single global
    period would wrap short rings twice and leave long ones open, which on a dome
    is every ring but one.

    THE POLE IS DROPPED, not drawn. `to_world` raises `AxisSingularity` at a cone
    apex and every azimuth names one point at a dome's apex, so the last ring is
    degenerate; the surface is closed by a triangle fan to the pole point instead
    of by a ring of zero-area quads. `u0`/`u1` come from the frame, so the drawn
    collector spans exactly what the design said it spans.

    RHO IS SIGNED AND THAT IS NOT A DEFECT. `scaffold_gcode.surface_frame` builds
    a dome and a bowl as THE SAME CIRCLE traversed opposite ways, both starting at
    `t0 = 0`, which is the pole -- so a dome's `rho` runs NEGATIVE across its
    whole span and `period_v_at`, which is `2*pi*rho`, comes back negative with
    it. Nothing else in the repo calls that query, so this builder is the first
    code to meet it (measured: `surface_frame("dome", radius=10,
    span=(0, 12))` gives rho -0.000 .. -9.320). It is self-consistent everywhere
    it matters -- the projected fibre lands exactly on the sphere and grows
    OUTWARD from it for a dome and INWARD for a bowl, and `seat_offset` carries
    the same sign, so the seat lifts the printed region back onto the deposition
    plane. So the sign is kept rather than stripped: `v = phi * rho` makes
    `_phi(v, rho)` return `phi` exactly, which puts the ring at radius `|rho|`
    with a winding that follows the frame's own azimuth convention. Only the POLE
    TEST takes an absolute value, because a negative rho is a real ring and a
    zero one is not.
    """
    import numpy as np
    import vtk
    from vtk.util import numpy_support

    u0 = float(frame.u0)
    u1 = float(frame.u1 if frame.u1 is not None else frame.u0)
    if not (u1 > u0):
        return None
    us = np.linspace(u0, u1, max(2, int(u_steps)))
    nv = max(3, int(v_steps))
    phi = np.linspace(0.0, 2.0 * np.pi, nv, endpoint=False)

    pts = []
    rings = []                       # index of each ring's first point, or None
    for u in us:
        try:
            rho = float(frame.period_v_at(u)) / (2.0 * np.pi)
        except Exception:            # noqa: BLE001 -- a profile that cannot answer
            return None
        if abs(rho) <= 1e-9:         # a pole: ONE point, not a ring of them
            try:
                x, y, z = frame.to_world(u, 0.0, 0.0)
            except Exception:        # noqa: BLE001 -- a cone apex refuses outright
                rings.append(None)
                continue
            rings.append((len(pts), 1))
            pts.append((float(x), float(y), float(z)))
            continue
        v = phi * rho
        x, y, z = frame.to_world(np.full(nv, u), v, 0.0)
        rings.append((len(pts), nv))
        pts.extend(zip(np.broadcast_to(np.asarray(x, float), (nv,)).tolist(),
                       np.asarray(y, float).tolist(),
                       np.asarray(z, float).tolist()))
    if len(pts) < 3:
        return None

    tris = []
    for a, b in zip(rings, rings[1:]):
        if a is None or b is None:
            continue
        (ia, na), (ib, nb) = a, b
        if na == nb == nv:                       # ring -> ring: quads
            for k in range(nv):
                k2 = (k + 1) % nv
                tris.append((ia + k, ia + k2, ib + k2))
                tris.append((ia + k, ib + k2, ib + k))
        elif na == 1:                            # pole -> ring: a fan
            for k in range(nv):
                tris.append((ia, ib + k, ib + (k + 1) % nv))
        elif nb == 1:                            # ring -> pole: the other fan
            for k in range(nv):
                tris.append((ia + k, ia + (k + 1) % nv, ib))
    if not tris:
        return None

    arr = np.asarray(pts, dtype=np.float32)
    vpts = vtk.vtkPoints()
    vpts.SetData(numpy_support.numpy_to_vtk(arr, deep=1))
    cells = np.asarray(tris, dtype=np.int64)
    conn = np.column_stack([np.full(len(cells), 3, dtype=np.int64), cells]).ravel()
    ca = vtk.vtkCellArray()
    ca.SetCells(len(cells), numpy_support.numpy_to_vtkIdTypeArray(conn, deep=1))
    poly = vtk.vtkPolyData()
    poly.SetPoints(vpts)
    poly.SetPolys(ca)
    return poly


def _orient_from_frame(poly, frame):
    """Flip `poly`'s normals in place if they disagree with `frame.normal`.

    One sample decides it: the surface is a single sweep with consistent winding
    by the time this runs, so the whole sheet agrees or the whole sheet is
    inverted. The sample is taken at the span's middle, away from any pole, where
    the frame answers cleanly."""
    import numpy as np
    import vtk
    from vtk.util import numpy_support

    arr = poly.GetPointData().GetNormals()
    if arr is None:
        return
    u = float(frame.u_mid) if hasattr(frame, "u_mid") else float(frame.u0)
    try:
        want = np.array([float(c) for c in frame.normal(u, 0.0, 0.0)])
        here = np.array([float(c) for c in frame.to_world(u, 0.0, 0.0)])
    except Exception:                     # noqa: BLE001 -- a frame that refuses
        return
    P = numpy_support.vtk_to_numpy(poly.GetPoints().GetData()).astype(np.float64)
    N = numpy_support.vtk_to_numpy(arr).astype(np.float64)
    i = int(np.argmin(np.linalg.norm(P - here, axis=1)))
    if float(np.dot(N[i], want)) >= 0.0:
        return
    flipped = numpy_support.numpy_to_vtk(np.ascontiguousarray(-N), deep=1)
    flipped.SetName(arr.GetName() or "Normals")
    poly.GetPointData().SetNormals(flipped)
    rev = vtk.vtkReverseSense()
    rev.SetInputData(poly)
    rev.ReverseCellsOn()
    rev.ReverseNormalsOff()
    rev.Update()
    poly.ShallowCopy(rev.GetOutput())


def revolution_actor(frame, u_steps=96, v_steps=96):
    """`revolution_polydata` wrapped in a matte actor, or None.

    Matte for the same reason the rod is (`gcode_player._mandrel_body`): a
    specular band sweeping a curved collector as the camera orbits reads as the
    collector changing colour. It wears `PLATE_COLOR`, so the three collectors --
    plate, rod and curved surface -- are one material in the scene."""
    import vtk

    poly = revolution_polydata(frame, u_steps=u_steps, v_steps=v_steps)
    if poly is None:
        return None
    norms = vtk.vtkPolyDataNormals()
    norms.SetInputData(poly)
    norms.SplittingOff()
    norms.ConsistencyOn()
    norms.Update()
    out = norms.GetOutput()
    # ...and point them the way THE FRAME says is outward. `ConsistencyOn` only
    # makes the winding agree with itself; which side that is depends on the sign
    # of rho, so a dome and a bowl built from the same circle would light from
    # opposite sides. `AutoOrientNormals` is documented for CLOSED surfaces and
    # this is an open cap, so the frame is asked instead -- it is the authority
    # on its own outward direction, and asking it costs one query.
    _orient_from_frame(out, frame)
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(out)
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    return _matte_plate(actor)


def _travel_segments(path):
    """Travel (G0) hops in a MEWron-style continuous-flow g-code file.

    Returns a list of ((x0,y0,z0), (x1,y1,z1)) segments -- one per G0 that
    actually moves -- which are exactly the jumps from the end of one deposited
    fiber to the start of the next (the lines NCViewer draws as travels),
    including the lead-in hop from the origin (0,0,0) to the first line. This is
    a deliberately tiny standalone parser so the viewer stays light and never has
    to import the heavier converter; it mirrors the G0/absolute-XYZ handling in
    gcode_to_fibers.parse_gcode. Assumes absolute coordinates (G90, which the
    generator now always emits)."""
    if not os.path.isfile(path):
        return []
    segs = []
    pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    with open(path, "r", errors="ignore") as fh:
        for raw in fh:
            line = raw.split(";", 1)[0].strip()
            if not line:
                continue
            tokens = line.split()
            code = tokens[0].upper()
            if code not in ("G0", "G00"):
                # only track position through moves; G1 still updates `pos`
                if code in ("G1", "G01"):
                    for tok in tokens[1:]:
                        axis = tok[0].upper()
                        if axis in pos:
                            try:
                                pos[axis] = float(tok[1:])
                            except ValueError:
                                pass
                continue
            new_pos = dict(pos)
            for tok in tokens[1:]:
                axis = tok[0].upper()
                if axis in new_pos:
                    try:
                        new_pos[axis] = float(tok[1:])
                    except ValueError:
                        pass
            moved = any(abs(new_pos[a] - pos[a]) > 1e-12 for a in ("X", "Y", "Z"))
            if moved:                       # a real travel, not a zero-length marker
                segs.append(((pos["X"], pos["Y"], pos["Z"]),
                             (new_pos["X"], new_pos["Y"], new_pos["Z"])))
            pos = new_pos
    return segs


def _travel_actor(segments, color):
    """A vtkActor drawing `segments` as bright wireframe lines, or None if there
    are none. Lighting is off so the travels read as flat wires that stand out
    from the shaded fibers."""
    if not segments:
        return None
    points = vtk.vtkPoints()
    lines = vtk.vtkCellArray()
    for (a, b) in segments:
        i = points.InsertNextPoint(*a)
        j = points.InsertNextPoint(*b)
        lines.InsertNextCell(2)
        lines.InsertCellPoint(i)
        lines.InsertCellPoint(j)
    poly = vtk.vtkPolyData()
    poly.SetPoints(points)
    poly.SetLines(lines)
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(poly)
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetColor(*color)
    actor.GetProperty().SetLineWidth(1.5)
    actor.GetProperty().SetLighting(False)
    return actor


def _arc_xy(sx, sy, ex, ey, i, j, cw, step_deg=22.5):
    """XY points along a G2/G3 arc, EXCLUDING the start and INCLUDING the exact
    endpoint -- a tiny local copy of gcode_to_fibers.arc_xy_points so the viewer
    stays free of the heavier converter (22.5 deg/chord = the converter's default
    arc_segments=8). Returns [(ex, ey)] for a degenerate (zero-radius) arc."""
    cx, cy = sx + i, sy + j
    r = math.hypot(i, j)
    if r < 1e-12:
        return [(ex, ey)]
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    sweep = a1 - a0
    if cw:                              # G2: sweep must be negative (clockwise)
        while sweep >= 0.0:
            sweep -= 2.0 * math.pi
    else:                              # G3: sweep must be positive
        while sweep <= 0.0:
            sweep += 2.0 * math.pi
    n = max(1, int(math.ceil(abs(sweep) / math.radians(step_deg))))
    out = [(cx + r * math.cos(a0 + sweep * (k / n)),
            cy + r * math.sin(a0 + sweep * (k / n))) for k in range(1, n + 1)]
    out[-1] = (ex, ey)                  # exact endpoint, immune to I/J rounding
    return out


def _deposit_segments(path):
    """Deposition (G1/G2/G3) moves in a MEWron-style continuous-flow g-code file,
    as a list of ((x0,y0,z0),(x1,y1,z1)) segments -- the commanded toolpath the
    fibre is laid along (the gold "gcode lines" NCViewer and the playback sim
    draw), with G2/G3 arcs discretised into short chords. The TRAVEL (G0) hops are
    deliberately excluded -- they have their own overlay/toggle. A tiny standalone
    parser, like _travel_segments above, so the viewer never imports the heavier
    converter; arc handling mirrors gcode_to_fibers.arc_xy_points. Assumes absolute
    coordinates (G90, which the generator always emits)."""
    if not os.path.isfile(path):
        return []
    segs = []
    pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    with open(path, "r", errors="ignore") as fh:
        for raw in fh:
            line = raw.split(";", 1)[0].strip()
            if not line:
                continue
            tokens = line.split()
            code = tokens[0].upper()
            is_arc = code in ("G2", "G3", "G02", "G03")
            is_line = code in ("G1", "G01")
            if not is_arc and not is_line:
                if code in ("G0", "G00"):       # travel: advance the pen, draw nothing
                    for tok in tokens[1:]:
                        axis = tok[0].upper()
                        if axis in pos:
                            try:
                                pos[axis] = float(tok[1:])
                            except ValueError:
                                pass
                continue
            w = {}
            for tok in tokens[1:]:
                axis = tok[0].upper()
                if axis in ("X", "Y", "Z", "I", "J"):
                    try:
                        w[axis] = float(tok[1:])
                    except ValueError:
                        pass
            nx, ny, nz = w.get("X", pos["X"]), w.get("Y", pos["Y"]), w.get("Z", pos["Z"])
            if is_arc:
                xy = _arc_xy(pos["X"], pos["Y"], nx, ny,
                             w.get("I", 0.0), w.get("J", 0.0), cw=code in ("G2", "G02"))
                px, py, pz = pos["X"], pos["Y"], pos["Z"]
                m = len(xy)
                z0 = pz
                for k, (ax, ay) in enumerate(xy, 1):
                    az = z0 + (nz - z0) * (k / m)   # linear Z helix across the arc
                    segs.append(((px, py, pz), (ax, ay, az)))
                    px, py, pz = ax, ay, az
            elif any(abs(v - pos[a]) > 1e-12
                     for a, v in (("X", nx), ("Y", ny), ("Z", nz))):
                segs.append(((pos["X"], pos["Y"], pos["Z"]), (nx, ny, nz)))
            pos["X"], pos["Y"], pos["Z"] = nx, ny, nz
    return segs


def _load_centerlines(stl_path):
    """The sagged fibre centrelines from the `.depo` cache the converter wrote next
    to `stl_path`, as (list of (N,3) float32 polylines, list of per-fibre diameters,
    list of per-fibre scaffold-group names or None), or None if there's no cache.
    These are the SAME sagged paths the STL was tubed from, so a wireframe / on-the-fly
    tube of them is a faithful, instant view of the part -- no need to read the (often
    multi-GB) mesh. The per-fibre diameters (the cache's `pdia`, or the global meta
    diameter filled per fibre for older caches) let the tube radius vary per scaffold;
    the per-fibre group names (`pgroup`/`gname`, or None for an ungrouped/older cache)
    let the viewer split the fibres back into per-scaffold actors."""
    depo = os.path.splitext(stl_path)[0] + ".depo.npz"
    if not os.path.isfile(depo):
        return None
    try:
        import numpy as np
        with np.load(depo, allow_pickle=False) as d:
            xyz = np.ascontiguousarray(d["xyz"], dtype=np.float32)
            splits = d["splits"]
            meta = d["meta"]
            has_pdia = "pdia" in d
            pdia_all = [float(v) for v in d["pdia"]] if has_pdia else None
            # per-fibre scaffold-group name, resolved through the stored name list
            grp_all = None
            if "pgroup" in d and "gname" in d:
                gname = [str(n) for n in d["gname"]]
                grp_all = [gname[int(g)] if 0 <= int(g) < len(gname) else None
                           for g in d["pgroup"]]
        gdia = float(meta[0]) if len(meta) else 0.02
        # keep diameters + group names in lockstep with the kept (>=2-point) fibres
        paths, pdia, groups = [], [], []
        for i, p in enumerate(np.split(xyz, splits[:-1])):
            if len(p) >= 2:
                paths.append(p)
                pdia.append(pdia_all[i] if pdia_all is not None else gdia)
                groups.append(grp_all[i] if grp_all is not None else None)
        if not paths:
            return None
        return (paths, pdia, groups if grp_all is not None else None)
    except Exception:  # noqa: BLE001 -- a bad/old cache just disables the fast view
        return None


def _centerline_actor(paths, pdia, sides, color):
    """A vtkActor for the fibre centrelines: flat wireframe lines when `sides < 3`,
    or tubes generated on the fly by vtkTubeFilter with `sides` facets otherwise.
    `pdia` (one diameter per fibre) sizes the tube; when the diameters differ the
    radius varies per scaffold via a per-point scalar. The line cell-array is built
    vectorised (each polyline is a contiguous run of point ids), so even millions of
    points assemble in well under a second and the tube is a fast C++ filter pass --
    no Python per-point loop, no mesh file."""
    import numpy as np
    from vtk.util import numpy_support
    allpts = np.concatenate(paths).astype(np.float32)
    pts = vtk.vtkPoints()
    pts.SetData(numpy_support.numpy_to_vtk(np.ascontiguousarray(allpts), deep=1))
    lens = np.array([len(p) for p in paths], dtype=np.int64)
    offsets = np.concatenate([[0], np.cumsum(lens)]).astype(np.int64)   # M+1
    conn = np.arange(int(lens.sum()), dtype=np.int64)                   # points in order
    cells = vtk.vtkCellArray()
    cells.SetData(numpy_support.numpy_to_vtkIdTypeArray(offsets, deep=1),
                  numpy_support.numpy_to_vtkIdTypeArray(conn, deep=1))
    poly = vtk.vtkPolyData()
    poly.SetPoints(pts)
    poly.SetLines(cells)
    diam0 = float(pdia[0]) if pdia else 0.02
    vary = bool(pdia) and len({round(float(d), 9) for d in pdia}) > 1

    mapper = vtk.vtkPolyDataMapper()
    actor = vtk.vtkActor()
    if sides and sides >= 3:
        tube = vtk.vtkTubeFilter()
        tube.SetInputData(poly)
        tube.SetNumberOfSides(int(sides))
        tube.CappingOn()
        if vary:                            # per-scaffold radius: per-point scalar
            radii = np.concatenate([np.full(len(p), float(pdia[i]) / 2.0)
                                    for i, p in enumerate(paths)]).astype(np.float64)
            rad = numpy_support.numpy_to_vtk(np.ascontiguousarray(radii), deep=1)
            rad.SetName("radius")
            poly.GetPointData().SetScalars(rad)
            tube.SetVaryRadiusToVaryRadiusByAbsoluteScalar()
        else:
            tube.SetRadius(max(diam0, 1e-6) / 2.0)
        mapper.SetInputConnection(tube.GetOutputPort())
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(*color)
        actor.GetProperty().SetSpecular(0.3)
        actor.GetProperty().SetSpecularPower(20)
    else:                               # wireframe: flat bright lines
        mapper.SetInputData(poly)
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(*color)
        actor.GetProperty().SetLighting(False)
        actor.GetProperty().SetLineWidth(1.0)
    return actor


def _arc_length(sx, sy, ex, ey, i, j, cw):
    """Length (mm) of a G2/G3 arc from (sx, sy) to (ex, ey) about centre
    (sx + i, sy + j) -- the I/J offset-from-start convention. Mirrors the sweep
    handling in gcode_to_fibers.arc_xy_points (G2/CW sweeps negative, G3/CCW
    positive); the length is R * |sweep|."""
    r = math.hypot(i, j)
    if r < 1e-12:
        return 0.0
    cx, cy = sx + i, sy + j
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    sweep = a1 - a0
    if cw:                              # G2: sweep must be negative (clockwise)
        while sweep >= 0.0:
            sweep -= 2.0 * math.pi
    else:                              # G3: sweep must be positive
        while sweep <= 0.0:
            sweep += 2.0 * math.pi
    return r * abs(sweep)


def _print_time_seconds(path):
    """Estimate total print time (seconds) for a MEWron-style g-code file by
    integrating path length / feedrate over every move.

    A deliberately tiny standalone walk -- like _travel_segments above -- so the
    viewer never has to import the heavier converter. It uses the same timing
    convention gcode_to_fibers and gcode_player do: in continuous-flow MEW the
    head holds ONE modal feed for the whole print (DEFAULT_FEED until the first F
    word), so travels (G0) are timed at the same speed as deposition (G1/G2/G3);
    G2/G3 arcs contribute their true arc length. Starts at the origin and assumes
    absolute coordinates (G90, which the generator always emits). Returns 0.0 for
    a missing file or one with no timed motion."""
    if not os.path.isfile(path):
        return 0.0
    pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    feed = DEFAULT_FEED
    total = 0.0
    with open(path, "r", errors="ignore") as fh:
        for raw in fh:
            line = raw.split(";", 1)[0].strip()
            if not line:
                continue
            tokens = line.split()
            code = tokens[0].upper()
            is_arc = code in ("G2", "G3", "G02", "G03")
            if not is_arc and code not in ("G0", "G1", "G00", "G01"):
                continue
            w = {}
            for tok in tokens[1:]:
                axis = tok[0].upper()
                if axis in ("X", "Y", "Z", "I", "J", "F"):
                    try:
                        w[axis] = float(tok[1:])
                    except ValueError:
                        pass
            if w.get("F", 0.0) > 0:
                feed = w["F"]
            nx, ny, nz = w.get("X", pos["X"]), w.get("Y", pos["Y"]), w.get("Z", pos["Z"])
            if is_arc:
                seg = _arc_length(pos["X"], pos["Y"], nx, ny,
                                  w.get("I", 0.0), w.get("J", 0.0),
                                  cw=code in ("G2", "G02"))
                seg = math.hypot(seg, nz - pos["Z"])   # Z helix (planar arcs -> 0)
            else:
                seg = math.sqrt((nx - pos["X"]) ** 2 + (ny - pos["Y"]) ** 2
                                + (nz - pos["Z"]) ** 2)
            if feed > 0 and seg > 1e-12:
                total += seg / (feed / 60.0)
            pos["X"], pos["Y"], pos["Z"] = nx, ny, nz
    return total


def format_duration(seconds):
    """A duration in seconds as a compact 'Xh Ym Zs' string, dropping units above
    the largest non-zero one ('2h 5m 30s', '5m 30s', '45s'). Matches the friendly
    print-time the scaffold generator prints and the player's HUD shows."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _render_sides(render):
    """Facet count from a 'tube:N' render mode; 0 (wireframe) for anything else."""
    if render and render.startswith("tube:"):
        try:
            return max(3, int(render.split(":", 1)[1]))
        except ValueError:
            return 6
    return 0


# --------------------------------------------------------------------------- #
# Deviation view (the Analyze fidelity view). Colours the sagged fibre
# centrelines from the .depo cache by their lateral departure from the
# COMMANDED path (a deviation sidecar written by mew_analysis) -- blue = on
# target, red = the worst predicted lateral error. Additive to the plain
# scaffold view: a missing / mismatched sidecar simply falls back to it.
# --------------------------------------------------------------------------- #
def _deviation_lut(vtop):
    """A blue->white->red diverging lookup table over [0, vtop] (mm) for the
    deviation colouring: blue = on-target, red = the colour-cap departure (the p99
    cross-track deviation from the commanded path; the top 1% clamp to full red so a
    single outlier can't wash the scaffold blue). Returns a vtkLookupTable."""
    lut = vtk.vtkLookupTable()
    n = 256
    lut.SetNumberOfTableValues(n)
    lut.SetRange(0.0, max(float(vtop), 1e-9))
    for i in range(n):
        t = i / (n - 1)
        if t < 0.5:                            # blue -> white
            s = t / 0.5
            r, g, bl = s, s, 1.0
        else:                                  # white -> red
            s = (t - 0.5) / 0.5
            r, g, bl = 1.0, 1.0 - s, 1.0 - s
        lut.SetTableValue(i, r, g, bl, 1.0)
    lut.Build()
    return lut


def _deviation_actor(paths, dev, sides, vtop, mean_diam):
    """A single vtkActor for ALL fibre centrelines, coloured per-vertex by `dev` (the
    cross-track deviation of the predicted path from the commanded one, mm) over a
    [0, vtop] LUT (vtop = the p99 colour cap). `dev` is the
    concatenation of every fibre's per-point deviation, in the SAME order as
    `np.concatenate(paths)` -- the deviation sidecar and the .depo cache share their
    vertex order, so the mapping is 1:1. Tubes (sides>=3) carry the scalar through the
    tube filter; wireframe colours the lines. Returns (actor, lut)."""
    import numpy as np
    from vtk.util import numpy_support
    allpts = np.concatenate(paths).astype(np.float32)
    pts = vtk.vtkPoints()
    pts.SetData(numpy_support.numpy_to_vtk(np.ascontiguousarray(allpts), deep=1))
    lens = np.array([len(p) for p in paths], dtype=np.int64)
    offsets = np.concatenate([[0], np.cumsum(lens)]).astype(np.int64)
    conn = np.arange(int(lens.sum()), dtype=np.int64)
    cells = vtk.vtkCellArray()
    cells.SetData(numpy_support.numpy_to_vtkIdTypeArray(offsets, deep=1),
                  numpy_support.numpy_to_vtkIdTypeArray(conn, deep=1))
    poly = vtk.vtkPolyData()
    poly.SetPoints(pts)
    poly.SetLines(cells)
    # Display unit: a near-perfect print has a cap of ~1e-6 mm, which renders on the
    # colour key as an alarming "1.6e-06 mm". Below 0.01 mm show microns instead --
    # the scalars AND the LUT range scale together so the colours are unchanged, only
    # the labels become readable ("1.6 um"). `unit` is returned so the key titles it.
    unit, uscale = ("mm", 1.0)
    if 0.0 < float(vtop) < 0.01:
        unit, uscale = ("um", 1000.0)
    scal = numpy_support.numpy_to_vtk(
        np.ascontiguousarray(np.asarray(dev, dtype=np.float64) * uscale), deep=1)
    scal.SetName("deviation")
    poly.GetPointData().SetScalars(scal)
    lut = _deviation_lut(vtop * uscale)

    mapper = vtk.vtkPolyDataMapper()
    actor = vtk.vtkActor()
    if sides and sides >= 3:
        tube = vtk.vtkTubeFilter()
        tube.SetInputData(poly)
        tube.SetNumberOfSides(int(sides))
        tube.CappingOn()
        tube.SetRadius(max(float(mean_diam), 1e-6) / 2.0)
        mapper.SetInputConnection(tube.GetOutputPort())
        actor.GetProperty().SetSpecular(0.2)
    else:
        mapper.SetInputData(poly)
        actor.GetProperty().SetLighting(False)
        actor.GetProperty().SetLineWidth(2.0)
    mapper.SetLookupTable(lut)
    mapper.SetScalarRange(0.0, max(float(vtop) * uscale, 1e-9))
    mapper.ScalarVisibilityOn()
    mapper.SetScalarModeToUsePointData()
    actor.SetMapper(mapper)
    return actor, lut, unit


def _deviation_scalar_bar(lut, text_rgb=None, unit="mm"):
    """A 2D colour-key for the deviation actor (title 'Deviation <unit>'), added to the
    renderer as a 2D overlay so it never touches the 3D depth buffer (the cad-camera
    orbit pivot reads GetZ). Compact, bottom-right. `text_rgb` themes the title/label
    text to the universal viewer palette (the sidebar's fg) when the modern shell is
    active; None keeps a light default that reads on the dark fallback backdrop. `unit`
    ('mm' or 'um') labels the key to match the actor's display scaling."""
    bar = vtk.vtkScalarBarActor()
    bar.SetLookupTable(lut)
    bar.SetTitle(f"Deviation\n{unit}")
    bar.SetNumberOfLabels(5)
    bar.SetMaximumWidthInPixels(70)
    bar.SetMaximumHeightInPixels(240)
    bar.GetPositionCoordinate().SetValue(0.90, 0.10)
    bar.SetWidth(0.09)
    bar.SetHeight(0.42)
    rgb = text_rgb if text_rgb is not None else (0.90, 0.90, 0.92)
    try:
        for tp in (bar.GetTitleTextProperty(), bar.GetLabelTextProperty()):
            tp.SetColor(*rgb)
            tp.ShadowOff()
            # match the themed sidebar typeface where the shell exposes it
            try:
                import gui_viewer_shell as _s
                _s.apply_font(tp)
            except Exception:  # noqa: BLE001
                pass
        bar.GetTitleTextProperty().SetFontSize(12)
        bar.GetLabelTextProperty().SetFontSize(10)
        # the two-line title sat on the top label ("mm" over the max)
        bar.SetVerticalTitleSeparation(8)
    except Exception:  # noqa: BLE001
        pass
    return bar


def _load_deviation(depo_path, side_path):
    """Load a deviation sidecar (mew_analysis.write_deviation_sidecar) and verify it
    aligns with the .depo the centrelines are drawn from: one deviation per cached
    vertex, and matching fibre splits. Returns (dev, vcap) on a clean match, else None
    (the caller then draws the plain scaffold -- a mismatch never crashes the viewer).
    `vcap` is the p99 colour cap (the LUT's red end) -- `dev` itself stays in honest
    mm; a sidecar written before the cap existed falls back to its true `vmax`.
    `dev` is re-ordered/kept in lockstep with _load_centerlines' >=2-point filter."""
    if not side_path or not os.path.isfile(side_path):
        return None
    try:
        import numpy as np
        with np.load(side_path, allow_pickle=False) as d:
            dev = np.asarray(d["dev"], dtype=np.float64)
            splits = np.asarray(d["splits"], dtype=np.int64)
            vmax = float(d["vmax"])
            vcap = float(d["vcap"]) if "vcap" in d.files else vmax
            side_fp = str(d["depo_fp"]) if "depo_fp" in d.files else None
        with np.load(depo_path, allow_pickle=False) as d:
            depo_splits = np.asarray(d["splits"], dtype=np.int64)
            depo_xyz = np.asarray(d["xyz"])
            # A content fingerprint ties the sidecar to the EXACT scaffold it was
            # computed for. Without it, a different scaffold that happens to share
            # the vertex count would pass the count check below and be coloured with
            # a stale, wrong deviation field. A legacy sidecar (no fp) skips this.
            if side_fp is not None:
                import hashlib
                cur_fp = hashlib.sha1(
                    np.ascontiguousarray(depo_xyz).tobytes()).hexdigest()
                if cur_fp != side_fp:
                    sys.stderr.write("stl_viewer: deviation sidecar was computed "
                                     "for a different .depo (geometry mismatch); "
                                     "showing the plain scaffold.\n")
                    return None
        # the sidecar splits are the cumulative lengths of the KEPT (>=2 pt) fibres;
        # the .depo splits include every fibre. Rebuild the kept-fibre lengths from
        # the .depo and require the sidecar to describe the same total vertex run.
        lens = np.diff(np.concatenate([[0], depo_splits]))
        kept_total = int(lens[lens >= 2].sum())
        if len(dev) != kept_total or (len(splits) and int(splits[-1]) != len(dev)):
            sys.stderr.write("stl_viewer: deviation sidecar does not match the "
                             ".depo cache; showing the plain scaffold.\n")
            return None
        # Floor the colour cap at the float32-storage noise level (defense-in-depth for a
        # legacy sidecar that predates the writer's floor, or a vcap=vmax fallback): a
        # physics-OFF scaffold's only "deviation" is |c|*2^-24 storage dust, which the p99
        # would otherwise stretch to full red. Supersedes the old max(vtop, 1e-9) guard.
        # Formula mirrors mew_analysis._DEV_NOISE_ULP_K (K=8); NOT imported here on purpose
        # (stl_viewer stays physics-free: it must run as a bare subprocess).
        max_abs_coord = float(np.abs(depo_xyz).max()) if depo_xyz.size else 0.0
        vcap = max(vcap, 8.0 * 2.0 ** -24 * max_abs_coord)
        return dev, vcap
    except Exception:  # noqa: BLE001
        sys.stderr.write("stl_viewer: could not read the deviation sidecar; "
                         "showing the plain scaffold.\n")
        return None


def view(path, gcode_path=None, colors=None, render="exact", embed=None,
         deviation=None):
    """`embed` is an optional viewer_embed.EmbedHost: render offscreen inside
    the GUI's embedded viewer page (frames stream over shared memory, controls
    export to Tk) instead of opening a native window. None -> the classic
    windowed viewer, byte-identical behaviour.

    `deviation` is an optional path to a deviation sidecar (npz written by
    mew_analysis.write_deviation_sidecar): when given AND the .depo centrelines load,
    the scaffold is drawn as ONE actor coloured per-vertex by lateral deviation from
    the commanded path, with a themed colour key -- the same field the unified
    Viewer's "Show deviation" paints.
    A missing / mismatched sidecar falls back to the plain scaffold."""
    colors = colors or {}

    # Fast-glance modes ("wireframe" / "tube:N") draw the sagged fibre CENTRELINES
    # from the .depo cache instead of reading the (often multi-GB) STL: wireframe is
    # instant lines, tube:N grows them into N-sided tubes on the fly. "exact" (the
    # default, and the fallback when there's no cache) reads the STL mesh the run
    # wrote -- exact but heavy. Set per launch from the GUI's View-quality dropdown.
    # The STL file itself may NOT exist in wireframe/tube modes (a preview run skips
    # the mesh build); the .depo cache beside `path` is what those modes render.
    centerlines = _load_centerlines(path) if render != "exact" else None
    if centerlines is None and not os.path.isfile(path):
        where = "STL" if render == "exact" else "STL or .depo cache"
        sys.stderr.write(f"stl_viewer: no {where} to show at: {path}\n")
        return 1

    # Modern chrome: palette + themed sidebar match the GUI's modern shell.
    # Kept optional (guarded import) so a hand-launched viewer without the
    # shell module still runs -- degrades to the plain dark bg + old buttons.
    try:
        import gui_viewer_shell as _shell
        _bg = _shell.viewport_bg(_shell.read_dark_pref())
        _themed = True
    except Exception:  # noqa: BLE001
        _shell, _bg, _themed = None, (0.11, 0.11, 0.13), False

    renderer = vtk.vtkRenderer()
    renderer.SetBackground(*_bg)   # ViewerChrome re-sets this when themed

    scaffold_actors = []        # (label, actor, colour) stacked into toggles
    merged_actor = None         # the single un-split fibre actor, when there's one
    deviation_bar = None        # colour key when the deviation view is active
    # Deviation view: one actor for the whole scaffold, coloured by per-vertex lateral
    # deviation from the commanded path. Takes precedence over the per-scaffold split
    # (the colouring IS the information here). Only when the .depo centrelines loaded
    # and the sidecar aligns with them.
    dev_data = None
    if deviation is not None and centerlines is not None:
        depo = os.path.splitext(path)[0] + ".depo.npz"
        dev_data = _load_deviation(depo, deviation)
    if dev_data is not None:
        cl_paths, cl_pdia, _cl_groups = centerlines
        sides = _render_sides(render) or 12
        dev, vcap = dev_data                 # vcap = p99 colour cap (LUT red end)
        mean_diam = (sum(cl_pdia) / len(cl_pdia)) if cl_pdia else 0.02
        dev_actor, dev_lut, dev_unit = _deviation_actor(
            cl_paths, dev, sides, vcap, mean_diam)
        renderer.AddActor(dev_actor)
        merged_actor = dev_actor            # so the master Hide/Show still governs it
        # theme the colour key to the universal viewer palette when the shell is on
        _dev_text = None
        if _themed and _shell is not None:
            try:
                _dev_text = _shell._hex_to_rgb(_shell.palette()["fg"])
            except Exception:  # noqa: BLE001
                _dev_text = None
        deviation_bar = _deviation_scalar_bar(dev_lut, _dev_text, dev_unit)
        renderer.AddActor2D(deviation_bar)
    elif centerlines is not None:
        cl_paths, cl_pdia, cl_groups = centerlines
        sides = _render_sides(render)
        if cl_groups is not None:
            # A multi-scaffold cache tags each fibre with its scaffold group. Draw
            # one coloured centreline actor per group (so it can be toggled and
            # recoloured), exactly like the exact mesh path does with the per-
            # scaffold sibling STLs -- same colours, same labels, same toggles.
            # Preserve first-appearance order (g-code order: stab, then scaffolds).
            order = []
            for name in cl_groups:
                if name is not None and name not in order:
                    order.append(name)
            for name in order:
                idx = [i for i, g in enumerate(cl_groups) if g == name]
                col = _color_for(name, colors)
                a = _centerline_actor([cl_paths[i] for i in idx],
                                      [cl_pdia[i] for i in idx], sides, col)
                renderer.AddActor(a)
                scaffold_actors.append((_part_label(name), a, col))
        else:
            merged_actor = _centerline_actor(cl_paths, cl_pdia, sides, _SOLID_COLOR)
            renderer.AddActor(merged_actor)
    else:
        # A multi-scaffold run drops a sibling STL per scaffold next to the merged
        # one. If those exist, draw each as its own coloured actor (so it can be
        # toggled and recoloured); the merged STL is left unread to avoid drawing
        # the geometry twice. Otherwise fall back to the single merged actor.
        parts = _discover_parts(path)
        if parts:
            for name, fp in parts:
                col = _color_for(name, colors)
                a = _stl_actor(fp, col)
                if a is None:
                    continue
                renderer.AddActor(a)
                scaffold_actors.append((_part_label(name), a, col))
        else:
            merged_actor = _stl_actor(path, (0.80, 0.80, 0.86))
            if merged_actor is not None:
                renderer.AddActor(merged_actor)

    # Every deposited-fibre/scaffold actor (per-scaffold parts AND a single merged
    # one), for the master Hide/Show scaffold toggle further down.
    mesh_actors = [a for _lbl, a, _c in scaffold_actors]
    if merged_actor is not None:
        mesh_actors.append(merged_actor)

    # origin (0,0,0) marker -- a small axis triad sized to the whole model
    # (union of whatever fiber actors we just added)
    b = renderer.ComputeVisiblePropBounds()
    diag = math.dist((b[0], b[2], b[4]), (b[1], b[3], b[5])) or 1.0
    origin_actor = origin_marker(0.08 * diag)
    renderer.AddActor(origin_actor)
    import vtk_helpers                      # the Settings "Show G-code zero"
    origin_actor.SetVisibility(vtk_helpers.prefer_origin())

    # The fibers and the (optional) build plate are kept in separate STLs. If a
    # sibling <name>_plate.stl sits next to the model, load it as its own grey
    # actor so it can be toggled -- shown by default (the plate the print sits on)
    # but easy to hide when it blends into the fibers from some angles.
    plate_path = os.path.splitext(path)[0] + "_plate.stl"
    plate_actor = _matte_plate(_stl_actor(plate_path, PLATE_COLOR))
    if plate_actor is not None:
        renderer.AddActor(plate_actor)

    # Travel (G0) fibers. MEW lays down one continuous fiber, so the hops between
    # passes / between the stabilization and main scaffold are real material the
    # STL doesn't capture. If the source g-code was handed to us, draw those hops
    # as bright wires (shown by default) so the path reads the way it does in
    # NCViewer, without an STL that would be hard to tell from the scaffold.
    travel_actor = None
    if gcode_path:
        travel_actor = _travel_actor(_travel_segments(gcode_path),
                                     (1.0, 0.45, 0.10))
        if travel_actor is not None:
            renderer.AddActor(travel_actor)

    # Commanded g-code lines: the deposition (G1/G2/G3) toolpath the fibre is laid
    # along, drawn as thin gold wires -- the SAME gold the playback sim draws its
    # "gcode lines" in (gcode_player.DEPOSIT_COLOR). OFF by default (the scaffold is
    # the headline view); turn them on, and the scaffold off, to inspect the raw
    # toolpath. Needs the source g-code, exactly like the travels above; reuses the
    # same line-actor builder.
    gcode_lines_actor = None
    if gcode_path:
        gcode_lines_actor = _travel_actor(_deposit_segments(gcode_path),
                                          (0.96, 0.76, 0.20))
        if gcode_lines_actor is not None:
            gcode_lines_actor.SetVisibility(False)
            renderer.AddActor(gcode_lines_actor)

    # Estimated print time, integrated from the source g-code's feedrates (the
    # same clock the player animates on and the generator prints). Attached to
    # the sidebar footer when the modern shell is active (see below); a legacy
    # top-left overlay when it is not. Only when we were handed the g-code and
    # it has timed motion -- a plain STL with no source has nothing to estimate.
    _print_time_secs = 0.0
    if gcode_path:
        _print_time_secs = _print_time_seconds(gcode_path)
    time_label = None
    if _print_time_secs > 0 and not _themed and embed is None:
        time_label = vtk.vtkTextActor()
        time_label.SetInput(f" Est. print time: {format_duration(_print_time_secs)} ")
        tp = time_label.GetTextProperty()
        tp.SetFontSize(16)
        tp.SetColor(0.96, 0.86, 0.30)        # warm gold, so it reads as info
        tp.SetBackgroundColor(0.16, 0.18, 0.22)
        tp.SetBackgroundOpacity(0.85)
        renderer.AddActor2D(time_label)

    window = vtk.vtkRenderWindow()
    window.AddRenderer(renderer)
    window.SetSize(1080, 720)
    window.SetWindowName(f"STL viewer  --  {os.path.basename(path)}")

    # embedded mode drives the interactor from the GUI's forwarded events via
    # an external loop; the host picks the interactor (the platform one on
    # Windows, so native fullscreen can pump real OS input into it)
    interactor = (embed.make_interactor() if embed is not None
                  else vtk.vtkRenderWindowInteractor())
    interactor.SetRenderWindow(window)
    # trackball = orbit/pan/zoom with the mouse; far nicer than the default
    interactor.SetInteractorStyle(vtk.vtkInteractorStyleTrackballCamera())
    # ...with CAD-style orbit/zoom about the point under the cursor layered on,
    # so inspecting a detail doesn't fling the whole scaffold across the view
    install_cad_camera(interactor, renderer)

    # embedded: offscreen render + shared-memory streaming from here on (must
    # be attached BEFORE the first Render(), which would otherwise open a
    # native window)
    if embed is not None:
        embed.attach(window, interactor, renderer)

    # Corner gizmo so you never lose track of which way is up: the upper-right
    # ViewCube, which also SNAPS the view when you click a face/edge/corner.
    # There is no separate vtkAxesActor + vtkOrientationMarkerWidget -- the
    # cube rides its own axis triad, so a second would put two orientation
    # gizmos on one screen.
    cube = None
    if _view_cube is not None:
        try:
            cube = _view_cube.ViewCube(window, interactor, renderer,
                                       reframe=lambda: (renderer.ResetCamera(),
                                                        fit_clipping_range(renderer)))
        except Exception:  # noqa: BLE001 -- a gizmo must never sink a launch
            cube = None

    # --- modern-shell sidebar: ALL controls + readouts live here -------- #
    # ViewerChrome confines the 3D scene to a right-hand viewport and paints a
    # themed left sidebar (wordmark, section headers, toggle buttons, and the
    # print-time footer) in a separate overlay renderer -- so the 3D view is
    # the ONLY thing on the right and nothing floats over it. Every toggle
    # keeps the same callback it always had. Falls back to the legacy floating
    # buttons if gui_viewer_shell is absent (hand-launched without the shell).
    # Embedded: the same chrome CALLS export the rows to the GUI's control
    # rail instead of drawing sidebar actors (viewer_embed.EmbedChrome mirrors
    # ViewerChrome's interface) -- so every toggle below is built once, for
    # both the windowed and embedded viewers.
    if embed is not None:
        chrome = embed.make_chrome("STL viewer", os.path.basename(path))
    elif _themed:
        chrome = _shell.ViewerChrome(window, interactor, renderer,
                                     title="STL viewer",
                                     subtitle=os.path.basename(path))
    else:
        chrome = None
    if chrome is not None:
        chrome.section("Scaffold")
        # scaffold-master toggle first (drives per-scaffold state); it sits at
        # the top so it reads as the primary control
        scaffold_on = {"v": True}

        def _make_master():
            def _lbl():
                return "Hide scaffold" if scaffold_on["v"] else "Show scaffold"

            def _cb():
                scaffold_on["v"] = not scaffold_on["v"]
                for a in mesh_actors:
                    a.SetVisibility(scaffold_on["v"])
            return _lbl, _cb

        if mesh_actors:
            _lbl, _cb = _make_master()
            chrome.button(_lbl, _cb)

        # per-scaffold rows (no colour chip -- the STL viewer matches the sim
        # viewers' plain centred card buttons)
        def _make_scaffold_toggle(scaf_actor, name):
            def _lbl():
                v = bool(scaf_actor.GetVisibility()) and scaffold_on["v"]
                return f"Hide {name}" if v else f"Show {name}"

            def _cb():
                scaf_actor.SetVisibility(not bool(scaf_actor.GetVisibility()))
            return _lbl, _cb

        for label, scaf_actor, _col in scaffold_actors:
            _lbl, _cb = _make_scaffold_toggle(scaf_actor, label)
            chrome.button(_lbl, _cb)

        color_on = {"v": True}
        if len(scaffold_actors) >= 2:
            def _color_lbl():
                return "Solid color" if color_on["v"] else "Scaffold colors"

            def _color_cb():
                color_on["v"] = not color_on["v"]
                for _lbl, _act, _col in scaffold_actors:
                    _act.GetProperty().SetColor(
                        *(_col if color_on["v"] else _SOLID_COLOR))
            chrome.button(_color_lbl, _color_cb)

        # optional overlays: plate / travels / gcode lines. Each shows only
        # when its actor exists (matches the old behaviour); no colour chip, so
        # the rows read the same as the sim viewers' buttons.
        def _make_actor_toggle(a, name):
            def _lbl():
                return f"Hide {name}" if bool(a.GetVisibility()) else f"Show {name}"

            def _cb():
                a.SetVisibility(not bool(a.GetVisibility()))
            return _lbl, _cb

        has_overlay = any(a is not None
                          for a in (plate_actor, travel_actor, gcode_lines_actor))
        if has_overlay:
            chrome.section("Overlays")
        if plate_actor is not None:
            _lbl, _cb = _make_actor_toggle(plate_actor, "plate")
            chrome.button(_lbl, _cb)
        if travel_actor is not None:
            _lbl, _cb = _make_actor_toggle(travel_actor, "travels")
            chrome.button(_lbl, _cb)
        if gcode_lines_actor is not None:
            _lbl, _cb = _make_actor_toggle(gcode_lines_actor, "gcode lines")
            chrome.button(_lbl, _cb)

        # Camera controls. One chrome.button is the whole fan-out -- the ttk
        # rail, the VTK sidebar and the native-fullscreen chrome all draw these
        # same rows. The section is unconditional now: the projection row is
        # always available, whereas the ViewCube row needs the gizmo to exist.
        chrome.section("View")
        if cube is not None:
            chrome.button(cube.label, cube.toggle)
        # Orthographic (the default -- parallel fibres stay parallel) vs
        # perspective. State + label are read back off the camera, so the row
        # stays right even though nothing here caches the mode.
        _proj_lbl, _proj_cb, _proj_on = projection_toggle(renderer)
        chrome.button(_proj_lbl, _proj_cb, toggle=("Perspective", _proj_on))
        # the XYZ origin triad, on by default
        _lbl, _cb = _make_actor_toggle(origin_actor, "origin")
        chrome.button(_lbl, _cb, toggle=(
            "Origin", lambda: bool(origin_actor.GetVisibility())))

        # Print-time estimate: same info the old top-corner label carried,
        # relocated to the sidebar footer so the 3D view stays clean.
        if _print_time_secs > 0:
            chrome.footer_section("Info")
            chrome.footer_label(
                f"Est. print time\n{format_duration(_print_time_secs)}",
                hint=False, lines=2)

        chrome.footer_section("Mouse")
        chrome.footer_label(
            "L-drag  orbit\nShift+L-drag  pan\nR-drag  turn (Z)\nwheel   zoom", lines=4)

        chrome.layout()
        if chrome.buttons:
            chrome.install_click_handler(interactor)
    else:
        # Fallback -- gui_viewer_shell not available, keep the old floating
        # button stack. (Kept minimal; skipping decoration because the modern
        # shell is the target path -- this branch is a safety net.)
        buttons_legacy = []        # list of (text_actor, name, actor)
        for label, scaf_actor, col in scaffold_actors:
            b = vtk.vtkTextActor()
            b.SetInput(f" Hide {label} ")
            b.GetTextProperty().SetFontSize(16)
            renderer.AddActor2D(b)
            buttons_legacy.append((b, label, scaf_actor))
        if buttons_legacy or time_label is not None:
            def _place_legacy(*_):
                _, h = window.GetSize()
                top = h - 32
                if time_label is not None:
                    time_label.SetDisplayPosition(14, h - 30)
                    top = h - 64
                for idx, (b, _n, _a) in enumerate(buttons_legacy):
                    b.SetDisplayPosition(14, top - idx * 30)
            _place_legacy()
            interactor.AddObserver("ConfigureEvent", _place_legacy)

    # CAD-style camera, matching gcode_player: parallel (orthographic) projection
    # with Z up and an isometric default view. Beyond looking the same, parallel
    # projection is what removes the zoom limit -- install_cad_camera._zoom drives
    # it by shrinking the camera's parallel scale (no lower bound), whereas the old
    # perspective default zoomed with cam.Dolly(), whose near-clip clamp stopped
    # the fibers growing well before they filled the view. Set orientation BEFORE
    # ResetCamera, which keeps the view direction/up and only slides to fit.
    cam = renderer.GetActiveCamera()
    cam.ParallelProjectionOn()
    cam.SetFocalPoint(0.0, 0.0, 0.0)
    cam.SetPosition(1.0, -1.0, 0.8)        # direction only; ResetCamera sets distance
    cam.SetViewUp(0.0, 0.0, 1.0)           # Z up, like CAD
    renderer.ResetCamera()
    import vtk_helpers
    vtk_helpers.apply_preferred_projection(renderer)   # the Settings switch
    fit_clipping_range(renderer)
    if embed is not None:
        def _origin_msg(m):                 # the Settings switch, live
            origin_actor.SetVisibility(bool(m.get("on")))
            window.Render()
        embed.on("origin", _origin_msg)
        # the host owns the loop: stdin commands in, frames + labels out; it
        # returns when the GUI says quit (or the pipe closes)
        return embed.loop()
    window.Render()
    interactor.Initialize()
    interactor.Start()
    return 0


# The option flags the viewer parses, beyond the positional <file.stl>
# [source.gcode]. --colors drives per-part colour; --render picks the view quality.
# (The machine-bounds box is gone -- it was a player-only reference and the GUI no
# longer passes any bounds/home/origin flags to the STL inspector.)
_VIEWER_FLAGS = ("--colors", "--render", "--deviation", "--thumbnail",
                 "--shots", "--shots-size", "--shots-views")


def parse_viewer_argv(args):
    """Split the viewer's argument list (WITHOUT the program name) into
    (positionals, options). `options` is a dict keyed by flag name (--colors /
    --render), each None when absent.

    Every flag accepts BOTH the spaced ("--opt" "value") and the joined
    ("--opt=value") form, so a value with a leading '-' is taken literally rather than
    read as another flag, and a joined token never spills into the positionals (which
    once blew the count and stopped the viewer opening). Pure (no VTK, no file I/O) so
    the parsing is unit-testable."""
    opts = {flag: None for flag in _VIEWER_FLAGS}
    rest = []
    i = 0
    while i < len(args):
        key, eq, val = args[i].partition("=")
        if key in opts:
            if eq:                            # joined: --opt=value
                opts[key] = val
                i += 1
            elif i + 1 < len(args):           # spaced: --opt value
                opts[key] = args[i + 1]
                i += 2
            else:                             # dangling flag, no value -> ignore
                i += 1
            continue
        rest.append(args[i])
        i += 1
    return rest, opts


def render_thumbnail(stl_path, out_png, size=512, direction=(1.0, -1.0, 1.0),
                     up=(0.0, 0.0, 1.0), bg=(0.97, 0.97, 0.98),
                     fibre=(0.32, 0.40, 0.54)):
    """Offscreen-render JUST the deposited fibres of `stl_path` (no build plate,
    no machine, no travels) to a square PNG. The camera looks from the ViewCube's
    front-face TOP-RIGHT corner -- direction (1,-1,1), view-up +Z (see
    view_cube._FACES: FRONT (0,-1,0) + RIGHT (1,0,0) + TOP (0,0,1)) -- the classic
    3/4 hero shot. Reuses the same _stl_actor builder the windowed viewer uses and
    the same "set direction, then ResetCamera to frame" discipline as view()
    (stl_viewer.py ~:1079). Used for the Print History thumbnail; never opens a
    window. Raises ValueError if the STL can't be read."""
    actor = _stl_actor(stl_path, fibre)
    if actor is not None:
        actor.GetMapper().Update()
    if actor is None or not actor.GetMapper().GetInput().GetNumberOfCells():
        # an empty / corrupt STL reads as zero triangles, not as an error
        raise ValueError("could not read STL: " + str(stl_path))
    renderer = vtk.vtkRenderer()
    renderer.SetBackground(*bg)
    renderer.AddActor(actor)

    window = vtk.vtkRenderWindow()
    window.SetOffScreenRendering(1)          # never pop a native window
    window.AddRenderer(renderer)
    window.SetSize(int(size), int(size))

    cam = renderer.GetActiveCamera()
    cam.ParallelProjectionOn()               # match the viewer/player projection
    cam.SetFocalPoint(0.0, 0.0, 0.0)
    cam.SetPosition(*direction)              # DIRECTION only; ResetCamera sets distance
    cam.SetViewUp(*up)
    renderer.ResetCamera()                   # slide + scale to frame the fibre bounds
    window.Render()

    w2i = vtk.vtkWindowToImageFilter()
    w2i.SetInput(window)
    w2i.ReadFrontBufferOff()                 # read the back buffer (offscreen render)
    w2i.Update()
    writer = vtk.vtkPNGWriter()
    writer.SetFileName(out_png)
    writer.SetInputConnection(w2i.GetOutputPort())
    writer.Write()


# The camera directions --shots renders from, keyed by name. Directions and
# view-ups are the ViewCube's own canonical faces (view_cube._FACES) plus the
# 3/4 "hero" render_thumbnail already uses, so a shot name means the same thing
# here as the button that snaps the camera there in the running viewer.
#
# The two ELEVATIONS are the point of this mode. A 3/4 or top view flattens the
# Z axis into shading, which is exactly the axis the defects this project keeps
# shipping live on -- fibres floating off the plate, geometry sunk through the
# bed, a first layer resting at the wrong height. Edge-on against the plate
# those are unmistakable; from the hero angle they are invisible.
_SHOT_VIEWS = {
    "front": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "right": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "top":   ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
    "hero":  ((1.0, -1.0, 1.0), (0.0, 0.0, 1.0)),
}
_SHOT_DEFAULT = ("front", "right", "top", "hero")

# The views that look along a horizontal axis, i.e. the ones where Z is a screen
# axis rather than shading. Only these get the Z exaggeration below.
_SHOT_ELEVATIONS = ("front", "right")

# An elevation of a real scaffold is unreadable at true scale: 10 mm wide by
# 0.06 mm tall is an aspect ratio near 170:1, so the part frames as a hairline
# and the defect it was meant to expose is a fraction of a pixel. Elevations are
# therefore stretched in Z until the part occupies this fraction of the frame.
#
# That is a lie about the geometry, so it is labelled ON the image (see the
# readout below) -- the same bargain the Lite viewer's minimum-fibre-width makes
# for the same reason. Never exaggerate without printing the factor.
_SHOT_Z_FILL = 0.30
_SHOT_Z_MAX = 500.0


def _pad_bounds(b, frac=0.08):
    """Widen a VTK bounds 6-tuple by `frac` of its own largest span.

    Framing on the FIBRE bounds (not the scene's) is what keeps a 10 mm scaffold
    from becoming a speck on a 120 mm plate -- but framed exactly, the part fills
    the frame edge to edge and you cannot see whether its underside meets the
    plate or hangs above it. The pad buys that margin. One span drives all three
    axes so the padding is isotropic and a flat scaffold (near-zero Z span)
    still gets real headroom instead of a hairline.
    """
    xmin, xmax, ymin, ymax, zmin, zmax = b
    span = max(xmax - xmin, ymax - ymin, zmax - zmin, 1e-6)
    d = span * frac
    return (xmin - d, xmax + d, ymin - d, ymax + d, zmin - d, zmax + d)


def render_shots(stem, out_dir, size=900, views=_SHOT_DEFAULT, sides=12,
                 bg=(0.97, 0.97, 0.98), fibre=_SOLID_COLOR):
    """Offscreen-render the deposited geometry from several camera angles.

    `stem` is the run's STL path (or the same path with any extension -- only the
    stem is used). Geometry comes from the `.depo` cache beside it when present,
    which is both faster and more faithful than reading a multi-GB mesh; the STL
    is the fallback. The `<stem>_plate.stl` sibling is drawn when it exists,
    because "is the fibre ON the plate" is a question about two objects and
    cannot be answered from a picture of one.

    Returns a report dict: the file written per view, the fibre bounds, the
    plate top Z, and `clearance_mm` (fibre min Z minus plate top Z) when both
    are known. That number is the machine-checkable half of this mode -- a
    negative clearance is geometry through the bed and needs no eyes at all,
    while a large positive one is a floating first layer. The images are the
    other half, and they are still the point: a human or an agent has to look.

    Never opens a window. Raises ValueError if no geometry can be read.
    """
    # Imported here, not at module scope: `stl_viewer` is imported by the Print
    # History thumbnail path, and --shots is a rare offline mode -- no reason to
    # make every import of this module pay for them.
    import json
    import numpy as np

    base = os.path.splitext(str(stem))[0]
    # A --no-mesh run leaves only "<stem>.depo.npz", and that is the path a
    # caller naturally has in hand. One splitext turns it into "<stem>.depo",
    # which would then look for "<stem>.depo.depo.npz" and find nothing -- so
    # take the second extension off too.
    if base.endswith(".depo"):
        base = base[:-len(".depo")]
    stl_path = base + ".stl"
    report = {"stem": base, "source": None, "shots": [], "problems": []}

    # Fibres: prefer the .depo centrelines, fall back to the mesh.
    loaded = _load_centerlines(stl_path)
    if loaded is not None:
        paths, pdia, _groups = loaded
        fibre_actor = _centerline_actor(paths, pdia, sides, fibre)
        report["source"] = "depo"
        pts = np.concatenate(paths)
        fb = (float(pts[:, 0].min()), float(pts[:, 0].max()),
              float(pts[:, 1].min()), float(pts[:, 1].max()),
              float(pts[:, 2].min()), float(pts[:, 2].max()))
        report["fibre_count"] = len(paths)
        report["point_count"] = int(len(pts))
        if pdia:
            report["diameter_mm"] = float(pdia[0])
    else:
        fibre_actor = _stl_actor(stl_path, fibre)
        if fibre_actor is not None:
            fibre_actor.GetMapper().Update()
        if (fibre_actor is None
                or not fibre_actor.GetMapper().GetInput().GetNumberOfCells()):
            # an empty / corrupt STL reads as zero triangles, not as an error
            raise ValueError("no .depo cache and no readable STL at " + stl_path)
        report["source"] = "stl"
        fb = tuple(float(v) for v in fibre_actor.GetBounds())
    report["fibre_bounds"] = list(fb)

    plate_actor = _matte_plate(_stl_actor(base + "_plate.stl", PLATE_COLOR))
    if plate_actor is not None:
        plate_actor.GetMapper().Update()
        plate_top = float(plate_actor.GetBounds()[5])
        report["plate_top_z"] = plate_top
        report["clearance_mm"] = fb[4] - plate_top
        # The centreline clearance above is not the question anyone is asking.
        # A fibre is a tube, so what "resting on the plate" means is that its
        # UNDERSIDE -- centre minus one radius -- meets the plate top. Reporting
        # only the centreline makes a correctly seated fibre look like it floats
        # by a radius, and hides a fibre that really is floating by one.
        d = report.get("diameter_mm")
        if d:
            report["underside_clearance_mm"] = fb[4] - d / 2.0 - plate_top

    out = str(out_dir)
    if not os.path.isdir(out):
        os.makedirs(out)

    renderer = vtk.vtkRenderer()
    renderer.SetBackground(*bg)
    if plate_actor is not None:
        renderer.AddActor(plate_actor)
    renderer.AddActor(fibre_actor)

    window = vtk.vtkRenderWindow()
    window.SetOffScreenRendering(1)          # never pop a native window
    window.AddRenderer(renderer)
    window.SetSize(int(size), int(size))

    # The Z range an elevation must show: the fibres, plus the plate surface when
    # there is one. Framing on the fibres alone would push the plate off-screen
    # exactly when the clearance is large -- i.e. in the floating-fibre case the
    # view exists to catch.
    z_lo = min(fb[4], report["plate_top_z"]) if "plate_top_z" in report else fb[4]
    z_span = max(fb[5] - z_lo, 1e-9)
    xy_span = max(fb[1] - fb[0], fb[3] - fb[2], 1e-9)
    z_exag = min(max(_SHOT_Z_FILL * xy_span / z_span, 1.0), _SHOT_Z_MAX)

    readout = vtk.vtkTextActor()
    readout.GetTextProperty().SetFontSize(max(14, int(size / 40)))
    readout.GetTextProperty().SetColor(0.15, 0.15, 0.18)
    readout.SetDisplayPosition(int(size * 0.03), int(size * 0.03))
    renderer.AddViewProp(readout)        # AddActor2D is deprecated on VTK 9.5+

    for name in views:
        if name not in _SHOT_VIEWS:
            report["problems"].append("unknown view %r (have: %s)"
                                      % (name, ", ".join(sorted(_SHOT_VIEWS))))
            continue
        direction, up = _SHOT_VIEWS[name]

        # Elevations stretch Z (see _SHOT_Z_FILL); top/hero stay true to scale.
        # Both actors take the SAME scale or the plate would move relative to the
        # fibres and the clearance the shot is meant to show would be fiction.
        elevation = name in _SHOT_ELEVATIONS
        exag = z_exag if elevation else 1.0
        fibre_actor.SetScale(1.0, 1.0, exag)
        if plate_actor is not None:
            plate_actor.SetScale(1.0, 1.0, exag)
            # The plate is the DATUM in an elevation and a BACKDROP anywhere
            # else: framed on a 10 mm scaffold, a 76 mm plate fills the frame
            # edge to edge and the part loses all contrast against it. Hide it
            # for the top/hero angles -- the same call render_thumbnail makes,
            # for the same reason.
            plate_actor.SetVisibility(1 if elevation else 0)
        if exag > 1.0:
            readout.SetInput("%s  |  Z exaggerated %.0fx -- not to scale"
                             % (name, exag))
            framed = _pad_bounds((fb[0], fb[1], fb[2], fb[3],
                                  z_lo * exag, fb[5] * exag))
        else:
            readout.SetInput("%s  |  true scale" % name)
            framed = _pad_bounds(fb)

        cam = renderer.GetActiveCamera()
        cam.ParallelProjectionOn()           # match the viewer/player projection
        cam.SetFocalPoint(0.0, 0.0, 0.0)
        cam.SetPosition(*direction)          # DIRECTION only; ResetCamera sets distance
        cam.SetViewUp(*up)
        # Frame on the (padded) FIBRE bounds, not the renderer's -- otherwise the
        # plate, which is an order of magnitude larger, sets the scale.
        renderer.ResetCamera(framed)
        fit_clipping_range(renderer)         # keep the plate from clipping edge-on
        window.Render()

        path = os.path.join(out, "shot_%s.png" % name)
        w2i = vtk.vtkWindowToImageFilter()
        w2i.SetInput(window)
        w2i.ReadFrontBufferOff()             # read the back buffer (offscreen render)
        w2i.Modified()                       # else the filter re-emits the first frame
        w2i.Update()
        writer = vtk.vtkPNGWriter()
        writer.SetFileName(path)
        writer.SetInputConnection(w2i.GetOutputPort())
        writer.Write()
        report["shots"].append({"view": name, "file": path,
                                "z_exaggeration": round(exag, 2)})

    with open(os.path.join(out, "shots.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    return report


def main(argv):
    # --embed name:WxH (GUI-internal): run as the embedded-viewer child --
    # offscreen render, frames over shared memory, controls exported to the
    # GUI (viewer_embed). Popped BEFORE the hand parser so the classic
    # windowed argv handling stays untouched.
    args, embed_spec = _pop_embed(argv[1:])
    rest, opts = parse_viewer_argv(args)
    # --thumbnail OUT.png: one-shot offscreen fibre thumbnail (Print History), no
    # window, no interactor. Handled before the windowed usage check.
    if opts["--thumbnail"]:
        if not rest:
            sys.stderr.write("usage: stl_viewer.py <file.stl> --thumbnail out.png\n")
            return 2
        try:
            render_thumbnail(rest[0], opts["--thumbnail"])
        except Exception as exc:  # noqa: BLE001 -- a failed thumb must not crash caller
            sys.stderr.write("thumbnail failed: %s\n" % exc)
            return 1
        return 0
    # --shots DIR: offscreen multi-angle renders of the deposited geometry, for
    # looking at a result without opening (or driving) a window. Like --thumbnail
    # this is handled before the windowed usage check.
    if opts["--shots"]:
        if not rest:
            sys.stderr.write("usage: stl_viewer.py <file.stl> --shots out_dir "
                             "[--shots-size N] [--shots-views front,top,...]\n")
            return 2
        views = _SHOT_DEFAULT
        if opts["--shots-views"]:
            views = tuple(v.strip() for v in opts["--shots-views"].split(",")
                          if v.strip())
        try:
            size = int(opts["--shots-size"]) if opts["--shots-size"] else 900
        except ValueError:
            sys.stderr.write("--shots-size must be an integer\n")
            return 2
        try:
            rep = render_shots(rest[0], opts["--shots"], size=size, views=views)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("shots failed: %s\n" % exc)
            return 1
        for s in rep["shots"]:
            print("%-6s %s" % (s["view"], s["file"]))
        fb = rep["fibre_bounds"]
        print("source=%s  fibre z %.4f..%.4f mm" % (rep["source"], fb[4], fb[5]))
        if "clearance_mm" in rep:
            # The numbers worth reading before the pictures. The verdict is
            # driven by the UNDERSIDE gap, because that is what "on the plate"
            # means for a tube; the centreline figure is kept alongside it so
            # the two are never confused for one another.
            print("plate top %.4f mm -> centreline %+.4f mm"
                  % (rep["plate_top_z"], rep["clearance_mm"]))
            u = rep.get("underside_clearance_mm")
            d = rep.get("diameter_mm") or 0.0
            if u is None:
                print("  fibre diameter unknown -- underside gap not computed")
            else:
                tol = max(d * 0.02, 1e-9)
                if u < -tol:
                    verdict = "SUNK INTO THE PLATE"
                elif u <= tol:
                    verdict = "seated on the plate"
                else:
                    verdict = "FLOATING above the plate"
                print("  underside gap %+.4f mm (%+.2f diameters) -- %s"
                      % (u, (u / d) if d else float("nan"), verdict))
        else:
            print("no _plate.stl beside the STL -- clearance not checked")
        for p in rep["problems"]:
            print("  - %s" % p)
        return 1 if rep["problems"] else 0
    if len(rest) not in (1, 2):
        sys.stderr.write("usage: stl_viewer.py <file.stl> [source.gcode] "
                         "[--colors name=rrggbb,...] [--render exact|wireframe|tube:N] "
                         "[--deviation sidecar.npz] [--embed name:WxH]\n"
                         "       stl_viewer.py <file.stl> --shots out_dir "
                         "[--shots-size N] [--shots-views front,right,top,hero]\n")
        return 2
    gcode_path = rest[1] if len(rest) == 2 else None
    # --render picks the view: "exact" (default; the STL mesh), "wireframe", or
    # "tube:N" (centreline tubes with N facets). Unknown values fall back to exact.
    render = opts["--render"] or "exact"
    if render != "exact" and render != "wireframe" and not render.startswith("tube:"):
        render = "exact"
    # --deviation colours the sagged CENTRELINES from the .depo, so it needs a
    # centreline render, not the STL mesh. An "exact" request with a deviation
    # sidecar is promoted to tube:12 (the fidelity view is always the fibre paths).
    deviation = opts["--deviation"]
    if deviation and render == "exact":
        render = "tube:12"
    embed = None
    if embed_spec:
        from viewer_embed import EmbedHost
        embed = EmbedHost(embed_spec)
    return view(rest[0], gcode_path, _parse_colors(opts["--colors"]),
                render=render, embed=embed, deviation=deviation)


def _pop_embed(args):
    """Strip --embed <spec> / --embed=<spec> from an argv list (delegates to
    viewer_embed only when actually present, so a hand-launched windowed
    viewer never needs that module)."""
    if not any(a == "--embed" or a.startswith("--embed=") for a in args):
        return list(args), None
    from viewer_embed import pop_embed_arg
    return pop_embed_arg(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
