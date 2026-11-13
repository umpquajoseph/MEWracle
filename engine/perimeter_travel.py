#!/usr/bin/env python3
"""
perimeter_travel.py

Route a travel (G0) hop along a scaffold's PERIMETER at a set distance, instead
of the axis-aligned dog-leg round its bounding box.

In continuous-flow MEW a travel hop is a dragged fibre, so where it runs matters.
The older routers (`array_travel`, and the around-the-part route in
`scaffold_gcode._emit_one_scaffold`) treat a scaffold as its bounding BOX, so on a
trapezoid or a circle the travel goes along y, turns 90 degrees, and goes along x,
well clear of the part on the slanted side. With the perimeter option on, the hop
follows the part's own outline at the offset the user typed.

THE LANE. A scaffold's lane is the convex hull of everything it has deposited
(turnaround balloons, linking loops and wave overhang included, so the offset is
measured from the outermost FIBRE, not from the bare footprint), grown outward by
`offset` with rounded corners. The hull of a convex footprint is convex, and the
grown hull is the Minkowski sum of it with a disc, so a lane is always a convex
polygon and a travel that stays outside it never touches the scaffold.

THE ROUTE. Between two points on one lane the shortest way outside it is along
the lane's boundary, the shorter way round (`boundary_walk`). Between lanes, or
from a point that is not on one, it is the shortest path that avoids every lane's
interior: a visibility graph over the lane vertices solved with Dijkstra
(`route_between`), like `array_travel`, but over convex polygons rather than
axis-aligned rectangles.

Pure geometry, standard library only, so it imports anywhere in the toolchain.
"""

import heapq
import math

_INF = float("inf")
_TOL = 1e-6                 # mm: how far inside a lane counts as "inside"

#: Corner sampling of a lane, degrees per chord. The chords are tangent to the
#: true offset circle (not inscribed in it), so a lane is never closer than the
#: asked offset, and 20 degrees keeps a lane to a few dozen vertices.
ARC_STEP_DEG = 20.0

#: Refuse the visibility graph above this many nodes and let the caller fall back
#: to its older routing; it is O(n^2) visibility tests.
MAX_NODES = 900


# --------------------------------------------------------------------------- #
# Lanes
# --------------------------------------------------------------------------- #
def convex_hull(points):
    """Convex hull of (x, y) points, CCW, no repeated or collinear vertices
    (Andrew's monotone chain). One or two distinct points come back as is."""
    pts = sorted(set((float(x), float(y)) for x, y in points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0.0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0.0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def lane_polygon(points, offset, arc_step_deg=ARC_STEP_DEG):
    """The lane round `points`: their convex hull grown outward by `offset` mm
    (>= 0), as a CCW convex polygon with rounded corners. None when there are no
    points. A lone point or a line gives a disc or a stadium, which is the right
    answer for a one-fibre scaffold."""
    pts = [(float(x), float(y)) for x, y in points]
    if not pts:
        return None
    base = convex_hull(pts)
    if offset <= 0.0:
        return base if len(base) >= 3 else None
    step = math.radians(arc_step_deg)
    n = max(8, int(math.ceil(2.0 * math.pi / step)))
    # circumscribed sampling: the chord's midpoint sits at `offset`
    r = offset / math.cos(math.pi / n)
    ring = [(r * math.cos(2.0 * math.pi * k / n),
             r * math.sin(2.0 * math.pi * k / n)) for k in range(n)]
    return convex_hull([(x + dx, y + dy) for x, y in base for dx, dy in ring])


def box_polygon(box, grow=0.0):
    """A (x0, y0, x1, y1) box grown by `grow` on every side, as a CCW polygon: the
    lane of a scaffold that has no perimeter option of its own."""
    x0, y0, x1, y1 = box
    return [(x0 - grow, y0 - grow), (x1 + grow, y0 - grow),
            (x1 + grow, y1 + grow), (x0 - grow, y1 + grow)]


def arc_samples(p0, p1, centre, ccw, step_deg=15.0):
    """Points along the circular arc from `p0` to `p1` about `centre` (excluding
    `p0`, including `p1`). Used so a G2/G3 turnaround widens a lane by its real
    bulge and not only by its end points."""
    cx, cy = centre
    a0 = math.atan2(p0[1] - cy, p0[0] - cx)
    a1 = math.atan2(p1[1] - cy, p1[0] - cx)
    sweep = (a1 - a0) % (2.0 * math.pi)
    if not ccw:
        sweep -= 2.0 * math.pi
    if abs(sweep) < 1e-12 and (p0 != p1):
        sweep = 2.0 * math.pi if ccw else -2.0 * math.pi
    r = math.hypot(p0[0] - cx, p0[1] - cy)
    n = max(1, int(math.ceil(abs(math.degrees(sweep)) / step_deg)))
    out = [(cx + r * math.cos(a0 + sweep * k / n),
            cy + r * math.sin(a0 + sweep * k / n)) for k in range(1, n)]
    out.append((float(p1[0]), float(p1[1])))
    return out


# --------------------------------------------------------------------------- #
# Point and segment tests against a convex CCW polygon
# --------------------------------------------------------------------------- #
def _edges(poly):
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        yield a, b


def _inward_depth(poly, p):
    """Signed distance from `p` to the boundary of the convex CCW `poly`:
    positive inside, negative outside (the smallest edge distance)."""
    d = _INF
    for (ax, ay), (bx, by) in _edges(poly):
        ex, ey = bx - ax, by - ay
        ln = math.hypot(ex, ey)
        if ln < 1e-12:
            continue
        d = min(d, (ex * (p[1] - ay) - ey * (p[0] - ax)) / ln)
    return d


def inside(poly, p, tol=_TOL):
    """True if `p` is strictly inside the lane `poly`."""
    return len(poly) >= 3 and _inward_depth(poly, p) > tol


def segment_enters(poly, a, b, tol=_TOL):
    """True if the open segment a->b passes through the open interior of the
    convex CCW polygon `poly`. A segment that only touches a corner or runs along
    an edge is a legal lane, so it does not count."""
    if len(poly) < 3:
        return False
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for (ux, uy), (vx, vy) in _edges(poly):
        ex, ey = vx - ux, vy - uy
        ln = math.hypot(ex, ey)
        if ln < 1e-12:
            continue
        fa = (ex * (a[1] - uy) - ey * (a[0] - ux)) / ln     # inside: > 0
        df = (ex * dy - ey * dx) / ln
        if abs(df) < 1e-12:
            if fa < -tol:
                return False                # parallel to this edge and outside
            continue
        t = -fa / df
        if df > 0.0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t1 - t0 <= 1e-12:
            return False
    tm = 0.5 * (t0 + t1)
    mid = (a[0] + tm * dx, a[1] + tm * dy)
    return _inward_depth(poly, mid) > tol


def nearest_on_boundary(poly, p):
    """The point of the polygon's boundary nearest `p`."""
    best, bd = None, _INF
    for (ax, ay), (bx, by) in _edges(poly):
        ex, ey = bx - ax, by - ay
        d2 = ex * ex + ey * ey
        t = 0.0 if d2 < 1e-18 else ((p[0] - ax) * ex + (p[1] - ay) * ey) / d2
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
        q = (ax + t * ex, ay + t * ey)
        d = math.hypot(p[0] - q[0], p[1] - q[1])
        if d < bd:
            bd, best = d, q
    return best


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def _perimeter_position(poly, p):
    """Arc length along the boundary at which `p` (a point on it) sits."""
    cum, best_s, bd = 0.0, 0.0, _INF
    for (ax, ay), (bx, by) in _edges(poly):
        ex, ey = bx - ax, by - ay
        ln = math.hypot(ex, ey)
        d2 = ln * ln
        t = 0.0 if d2 < 1e-18 else ((p[0] - ax) * ex + (p[1] - ay) * ey) / d2
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
        d = math.hypot(p[0] - (ax + t * ex), p[1] - (ay + t * ey))
        if d < bd:
            bd, best_s = d, cum + t * ln
        cum += ln
    return best_s


def boundary_walk(poly, a, b):
    """Waypoints from `a` to `b` (both on the boundary of `poly`) along that
    boundary the SHORTER way round: the lane's vertices between them, then `b`.
    `a` itself is not repeated."""
    n = len(poly)
    cum = [0.0]
    for i in range(n):
        p, q = poly[i], poly[(i + 1) % n]
        cum.append(cum[-1] + math.hypot(q[0] - p[0], q[1] - p[1]))
    total = cum[-1]
    sa = _perimeter_position(poly, a)
    sb = _perimeter_position(poly, b)
    fwd = (sb - sa) % total
    if total <= 0.0:
        return [b]
    if fwd <= total - fwd:
        way = [(((cum[k] - sa) % total), poly[k % n]) for k in range(n)]
        way = [(d, v) for d, v in way if 1e-9 < d < fwd - 1e-9]
        way.sort(key=lambda t: t[0])
    else:
        back = total - fwd
        way = [(((sa - cum[k]) % total), poly[k % n]) for k in range(n)]
        way = [(d, v) for d, v in way if 1e-9 < d < back - 1e-9]
        way.sort(key=lambda t: t[0])
    return [v for _d, v in way] + [b]


def route_between(start, goal, lanes):
    """The shortest path from `start` to `goal` that never enters a lane's
    interior, as waypoints after `start` ending at `goal`. `start` and `goal`
    must each be on a lane boundary or in free space. Returns None when no route
    exists or the graph would be too large (the caller then falls back)."""
    if not any(segment_enters(p, start, goal) for p in lanes):
        return [goal]
    nodes = [start, goal]
    for k, poly in enumerate(lanes):
        for v in poly:
            if not any(j != k and inside(q, v) for j, q in enumerate(lanes)):
                nodes.append(v)
    if len(nodes) > MAX_NODES:
        return None
    boxes = []
    for poly in lanes:
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        boxes.append((min(xs), min(ys), max(xs), max(ys)))

    def visible(a, b):
        lox, hix = (a[0], b[0]) if a[0] <= b[0] else (b[0], a[0])
        loy, hiy = (a[1], b[1]) if a[1] <= b[1] else (b[1], a[1])
        for poly, (x0, y0, x1, y1) in zip(lanes, boxes):
            if x1 <= lox or x0 >= hix or y1 <= loy or y0 >= hiy:
                continue
            if segment_enters(poly, a, b):
                return False
        return True

    n = len(nodes)
    dist = [_INF] * n
    prev = [-1] * n
    dist[0] = 0.0
    pq = [(0.0, 0)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist[u]:
            continue
        if u == 1:
            break
        ux, uy = nodes[u]
        for v in range(n):
            if v == u:
                continue
            nd = d + math.hypot(nodes[v][0] - ux, nodes[v][1] - uy)
            if nd >= dist[v]:
                continue
            if not visible(nodes[u], nodes[v]):
                continue
            dist[v] = nd
            prev[v] = u
            heapq.heappush(pq, (nd, v))
    if dist[1] == _INF:
        return None
    path, u = [], 1
    while u != -1:
        path.append(nodes[u])
        u = prev[u]
    path.reverse()
    return path[1:]


def _containing(lanes, p):
    """The index of the lane `p` is strictly inside, else None."""
    for k, poly in enumerate(lanes):
        if inside(poly, p):
            return k
    return None


def plan(start, goal, lanes, own=None):
    """Waypoints that carry the nozzle from `start` to `goal` along the lanes.

    `lanes` are every lane the travel must stay outside. `start` and `goal` are
    fibre ends, which sit INSIDE the lane of the scaffold they belong to, so each
    is first stepped straight out to that lane's boundary (the nearest boundary
    point), and the route runs between those two boundary points. `own`, if
    given, is the index in `lanes` of the lane `goal` belongs to.

    Returns the waypoints after `start`, ending at `goal`, or None when no route
    exists (the caller falls back to its older travel)."""
    if not lanes:
        return [goal]
    ks = _containing(lanes, start)
    kg = own if own is not None else _containing(lanes, goal)
    s2 = nearest_on_boundary(lanes[ks], start) if ks is not None else start
    g2 = nearest_on_boundary(lanes[kg], goal) if kg is not None else goal
    if ks is not None and ks == kg:
        mid = boundary_walk(lanes[ks], s2, g2)
    else:
        mid = route_between(s2, g2, lanes)
        if mid is None:
            return None
    out = []
    if _dist(s2, start) > 1e-9:
        out.append(s2)
    out.extend(mid)
    if _dist(g2, goal) > 1e-9 or not out or _dist(out[-1], goal) > 1e-9:
        out.append(goal)
    return _dedupe(out, start)


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _dedupe(points, start):
    """Drop consecutive repeats (and a first point equal to `start`)."""
    out, last = [], start
    for p in points:
        if _dist(p, last) > 1e-9:
            out.append(p)
            last = p
    return out
