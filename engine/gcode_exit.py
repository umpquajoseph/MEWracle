#!/usr/bin/env python3
"""
gcode_exit.py

Geometry for running the nozzle OFF the build plate once a print is finished.

In melt-electrowriting the polymer keeps flowing for a while after the last
commanded move. If the nozzle is left sitting over the final deposition point,
that trailing flow piles up on top of the scaffold and ruins it. The fix is to
end every program with one extra travel that carries the nozzle clear of the
part, so the trailing polymer lands OFF it.

This module is pure geometry -- it picks the exit POINT; the scaffold generator
emits the move and routes around the footprints (see scaffold_gcode's end-of-
print exit, which uses `plate_exit_point` for the goal and `outward_edge_exit`
to step clear of a finished scaffold). The plate footprint and machine envelope
are passed as (x0, x1, y0, y1) extents (a circular plate passes its bounding
square, positioned from the plate centre -- the same convention
gcode_to_fibers.py renders the plate slab with). No tuning is needed.

No third-party dependencies, so it imports anywhere in the toolchain.
"""

DEFAULT_MARGIN = 5.0        # mm past the plate edge the nozzle is sent


def mandrel_exit_x(x, x0, x1, margin=DEFAULT_MARGIN):
    """The end-of-print parking X for a ROTATING COLLECTOR, `margin` mm clear of
    whichever end of the printed span the nozzle finished nearer.

    A tube has no plate edge to run off, but it has two ends, and the axis the
    nozzle can leave along is the mandrel's own (machine X). `x0`/`x1` are the
    printed span's ends in machine X; `x` is where the last deposition move
    finished. Returns the X to park at: `x0 - margin` or `x1 + margin`.

    The nearer end is chosen, so the exit travel is the SHORTER of the two and
    crosses less of the finished tube. On a tie (a print that finishes exactly
    mid-span) it leaves via `x1`, matching the +X sense a tube is wound in.

    Rotation is deliberately not part of this: A is left where the print left
    it, so the exit is a pure axial translation and the trailing polymer is
    drawn off the end of the wall rather than wrapped around it once more."""
    return (x0 - margin) if (x - x0) < (x1 - x) else (x1 + margin)


def nearest_edge_exit(x, y, xmin, xmax, ymin, ymax, margin=DEFAULT_MARGIN):
    """Exit point `margin` mm beyond whichever plate edge (x,y) is nearest.

    Returns (ex, ey, edge): the target point, with the coordinate PERPENDICULAR
    to the chosen edge pushed `margin` past it and the parallel coordinate left
    unchanged, plus `edge` in {'left','right','bottom','top'} (X-min, X-max,
    Y-min, Y-max respectively). Used only as the fall-back for outward_edge_exit
    when the finish point sits exactly at the part centre (no outward direction).
    Distances may be negative if the point already sits outside the plate; the
    nearest (least, possibly negative) edge is still chosen."""
    dist = {
        "left":   x - xmin,        # walk out -X
        "right":  xmax - x,        # walk out +X
        "bottom": y - ymin,        # walk out -Y
        "top":    ymax - y,        # walk out +Y
    }
    edge = min(dist, key=dist.get)
    if edge == "left":
        return xmin - margin, y, edge
    if edge == "right":
        return xmax + margin, y, edge
    if edge == "bottom":
        return x, ymin - margin, edge
    return x, ymax + margin, edge


def outward_edge_exit(x, y, cx, cy, xmin, xmax, ymin, ymax,
                      margin=DEFAULT_MARGIN):
    """Exit point `margin` mm past a plate edge the finish point (x,y) faces,
    heading AWAY from the part centre (cx,cy).

    The finish FACES a horizontal edge (right if it sits right of centre, left if
    left of it) and a vertical edge (top if above centre, bottom if below). Of
    those facing edges it leaves by the NEAREST -- the shortest travel that still
    moves away from the part -- so the nozzle never tracks back across it. The
    perpendicular coordinate is pushed `margin` past that edge and the parallel
    coordinate is left unchanged. (A finish already past a faced edge gives a
    negative distance and is chosen first, i.e. pushed further out that side.)

    Returns (ex, ey, edge) with edge in {'left','right','bottom','top'}. When the
    finish coincides with the centre it faces no edge -- no outward direction --
    so it falls back to nearest_edge_exit."""
    faced = []
    if x > cx:
        faced.append(("right", xmax - x))
    elif x < cx:
        faced.append(("left", x - xmin))
    if y > cy:
        faced.append(("top", ymax - y))
    elif y < cy:
        faced.append(("bottom", y - ymin))
    if not faced:
        return nearest_edge_exit(x, y, xmin, xmax, ymin, ymax, margin)
    edge = min(faced, key=lambda e: e[1])[0]
    if edge == "right":
        return xmax + margin, y, edge
    if edge == "left":
        return xmin - margin, y, edge
    if edge == "top":
        return x, ymax + margin, edge
    return x, ymin - margin, edge


def plate_exit_point(x, y, part, plate, bounds=None, margin=DEFAULT_MARGIN):
    """The end-of-print parking point: from the finish (x, y), head outward off
    the PART (away from its centre) past the nearest PLATE edge, kept inside the
    machine BOUNDS -- choosing a side that still has ROOM beyond the part within
    the envelope, so clamping to the bounds can never reverse the exit back across
    the print.

    `part`/`plate`/`bounds` are (x0, x1, y0, y1) extents (`bounds` None = no clamp).
    The part centre is taken from `part` (the true footprint), NOT a move bounding
    box, so a lead-in from the machine origin can't skew which side is chosen.
    The finish faces an edge per axis (the side it sits on relative to the part
    centre); a faced edge is a candidate only if its clamped exit point still
    clears the part on that axis. Returns (ex, ey): of the candidates that clear,
    the nearest; if none clear (the part fills the envelope on every faced side),
    the nearest faced edge regardless (best effort)."""
    px0, px1, py0, py1 = part
    pcx, pcy = (px0 + px1) / 2.0, (py0 + py1) / 2.0
    x0, x1, y0, y1 = plate
    cbx = (lambda v: v) if bounds is None else \
        (lambda v: min(max(v, bounds[0]), bounds[1]))
    cby = (lambda v: v) if bounds is None else \
        (lambda v: min(max(v, bounds[2]), bounds[3]))
    eps = 1e-9
    cands = []                          # (clears_part, dist_to_edge, ex, ey)
    if x >= pcx:                        # finish right of centre -> exit right
        ex = cbx(x1 + margin)
        cands.append((ex > px1 + eps, abs(x1 - x), ex, y))
    if x <= pcx:                        # ...or left
        ex = cbx(x0 - margin)
        cands.append((ex < px0 - eps, abs(x - x0), ex, y))
    if y >= pcy:                        # ...up
        ey = cby(y1 + margin)
        cands.append((ey > py1 + eps, abs(y1 - y), x, ey))
    if y <= pcy:                        # ...down
        ey = cby(y0 - margin)
        cands.append((ey < py0 - eps, abs(y - y0), x, ey))
    if not cands:
        return cbx(x), cby(y)
    cands.sort(key=lambda c: (not c[0], c[1]))   # clears first, then nearest
    return cands[0][2], cands[0][3]
