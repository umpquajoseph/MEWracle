#!/usr/bin/env python3
"""
array_travel.py

Route the inter-scaffold travel (G0) hop AROUND the scaffolds already laid down,
instead of straight across them.

When several scaffolds are arrayed onto one slide, the nozzle has to hop from the
scaffold it just finished to the start of the next one. In continuous-flow MEW
that hop is itself a dragged fiber, so a straight hop drags polymer ACROSS every
scaffold between the two -- ruining them. The old fix ran the nozzle one
`travel_offset` clear of the finished scaffold and then went straight; that still
cut back through the array.

This module models each scaffold as a square EXCLUSION ZONE -- its footprint
grown by `margin` (the travel offset) on every side -- and finds a travel path
that stays in the OPEN AREAS between those zones. The only zone it is allowed to
enter is the destination's, and only at the very end (the re-entry near the start
of the next scaffold). The path is the Euclidean shortest route among the zones:
a visibility graph over the zone corners, solved with Dijkstra. Among axis-
aligned rectangles the optimal path only ever turns at their corners, so the
corners (plus the start and goal) are the only nodes needed -- light and exact.

If the zones are packed so tightly that no open route exists (overlapping zones,
a boxed-in destination), it returns an empty path and the caller falls back to a
direct hop -- the implementation never fails to produce a move.

Pure geometry, no third-party dependencies, so it imports anywhere in the
toolchain (the scaffold generator wires it in directly).
"""

import heapq

_EPS = 1e-9
_INF = float("inf")


def _expand(box, margin):
    """A footprint (x0, y0, x1, y1) grown by `margin` on every side -- its
    exclusion zone."""
    x0, y0, x1, y1 = box
    return (x0 - margin, y0 - margin, x1 + margin, y1 + margin)


def _point_in_interior(px, py, rect, eps=_EPS):
    """True if (px, py) is STRICTLY inside `rect` (its open interior). A point on
    an edge or corner is not interior, so a zone corner never counts as inside its
    own zone."""
    x0, y0, x1, y1 = rect
    return (x0 + eps < px < x1 - eps) and (y0 + eps < py < y1 - eps)


def _seg_hits_interior(ax, ay, bx, by, rect, eps=_EPS):
    """True if the open segment (ax,ay)->(bx,by) passes through the open interior
    of `rect`. A segment that only touches a corner, or runs ALONG an edge, does
    not count -- those are legal travel lanes between abutting zones.

    Liang-Barsky clips the segment to the closed rectangle to get the overlap
    parameter range [t0, t1]; a positive-length overlap whose midpoint is strictly
    inside the rectangle is a real interior crossing."""
    dx, dy = bx - ax, by - ay
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, ax - rect[0]), (dx, rect[2] - ax),
                 (-dy, ay - rect[1]), (dy, rect[3] - ay)):
        if -eps < p < eps:
            if q < -eps:
                return False            # parallel to this edge and outside it
        else:
            r = q / p
            if p < 0.0:                 # entering this edge's half-plane
                if r > t1:
                    return False
                if r > t0:
                    t0 = r
            else:                       # leaving this edge's half-plane
                if r < t0:
                    return False
                if r < t1:
                    t1 = r
    if t1 - t0 <= eps:
        return False                    # touches at a point / runs along an edge
    tm = 0.5 * (t0 + t1)
    mx, my = ax + tm * dx, ay + tm * dy
    return _point_in_interior(mx, my, rect, eps)


def _visible(ax, ay, bx, by, rects):
    """True if the segment crosses no exclusion zone's interior -- a legal hop."""
    # Cheap segment-bbox vs zone-bbox reject before the Liang-Barsky interior
    # test: a zone whose bbox cannot overlap the segment's bbox cannot have its
    # open interior crossed, so skip it. Output-identical with the plain scan;
    # turns the per-call O(N) zone sweep into O(zones near the segment), which is
    # what kills the large-array routing cliff.
    lox, hix = (ax, bx) if ax <= bx else (bx, ax)
    loy, hiy = (ay, by) if ay <= by else (by, ay)
    for r in rects:
        if r[2] <= lox or r[0] >= hix or r[3] <= loy or r[1] >= hiy:
            continue
        if _seg_hits_interior(ax, ay, bx, by, r):
            return False
    return True


def _corners(rect):
    x0, y0, x1, y1 = rect
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def _free_corners(rect, rects):
    """The corners of `rect` not buried inside any other zone's interior -- the
    ones that can serve as travel waypoints."""
    return [c for c in _corners(rect)
            if not any(_point_in_interior(c[0], c[1], r) for r in rects)]


def plan_array_travel(start, dest_footprint, footprints, margin):
    """Travel waypoints (a list of (x, y)) that carry the nozzle from `start` to
    the destination scaffold WITHOUT crossing any scaffold's exclusion zone.

    `start`           the nozzle's current (x, y) -- already run clear of the
                      scaffold just finished (so it sits on that zone's boundary,
                      not inside it).
    `dest_footprint`  (x0, y0, x1, y1) of the scaffold being travelled to.
    `footprints`      (x0, y0, x1, y1) of EVERY scaffold in the array; each is
                      grown by `margin` into an exclusion zone.
    `margin`          the travel offset (mm) -- how far the zone extends past each
                      footprint.

    The returned list excludes `start` and ends at the nearest free corner of the
    destination's exclusion zone (the re-entry point); the caller emits each as a
    G0 and lets the scaffold emitter drag the final short hop inward to the
    footprint. Returns [] when routing is off (margin <= 0), unnecessary, or
    impossible (zones too tightly packed) -- the caller then hops directly."""
    if margin <= 0 or not footprints:
        return []

    rects = [_expand(b, margin) for b in footprints]
    dest = _expand(dest_footprint, margin)
    sx, sy = start

    # Aim for the corner of the destination zone nearest the start -- the shortest
    # re-entry. Skip corners buried inside another (overlapping) zone.
    goal = None
    best = _INF
    for cx, cy in _corners(dest):
        if any(_point_in_interior(cx, cy, r) for r in rects):
            continue
        d = (cx - sx) ** 2 + (cy - sy) ** 2
        if d < best:
            best, goal = d, (cx, cy)
    if goal is None:
        return []                       # destination boxed in -> direct fallback

    return _route(start, goal, rects)


def plan_exit_travel(start, exit_point, footprints, margin):
    """Travel waypoints carrying the nozzle from `start` to the off-the-slide
    `exit_point` WITHOUT crossing any scaffold's exclusion zone -- the end-of-print
    move that runs the trailing polymer clear of the part.

    Same routing as `plan_array_travel`, but the goal is a fixed point in free
    space (already off the slide, so outside every zone) rather than a scaffold
    corner. `start` must already sit clear of the last scaffold's zone (the caller
    runs the same outward exit first). ALWAYS ends at `exit_point`: if no open
    route exists it falls back to a direct hop, because the nozzle must still leave
    the slide. Returns [exit_point] when routing is off (margin <= 0)."""
    if margin <= 0 or not footprints:
        return [exit_point]
    rects = [_expand(b, margin) for b in footprints]
    return _route(start, exit_point, rects) or [exit_point]


def _route(start, goal, rects):
    """Shortest travel waypoints from `start` to `goal` that cross no rectangle's
    interior, via a visibility graph over the rectangle corners solved with
    Dijkstra. `start` and `goal` must each be reachable (on a zone boundary or in
    free space, not buried in an interior). Returns the waypoints AFTER `start`,
    ending at `goal`; [] if no open route exists. Among axis-aligned rectangles the
    optimal path only ever turns at their corners, so those (plus start and goal)
    are the only nodes needed."""
    sx, sy = start
    gx, gy = goal

    # Straight there if nothing is in the way.
    if _visible(sx, sy, gx, gy, rects):
        return [goal]

    # Otherwise route around the zones: visibility graph over their corners.
    nodes = [start, goal]
    for r in rects:
        nodes.extend(_free_corners(r, rects))

    n = len(nodes)
    dist = [_INF] * n
    prev = [-1] * n
    dist[0] = 0.0
    pq = [(0.0, 0)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist[u]:
            continue
        if u == 1:                      # reached the goal
            break
        ux, uy = nodes[u]
        for v in range(n):
            if v == u:
                continue
            vx, vy = nodes[v]
            if not _visible(ux, uy, vx, vy, rects):
                continue
            nd = d + ((vx - ux) ** 2 + (vy - uy) ** 2) ** 0.5
            if nd < dist[v]:
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))

    if dist[1] == _INF:
        return []                       # no open route -> caller decides fallback

    path = []
    u = 1
    while u != -1:
        path.append(nodes[u])
        u = prev[u]
    path.reverse()
    return path[1:]                     # drop start; waypoints incl. the goal
