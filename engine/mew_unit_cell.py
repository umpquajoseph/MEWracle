"""Literature-defined unit-cell pattern families for 4-axis (mandrel) MEW tubes.

Pure geometry and graph theory: stdlib only, no numpy, no VTK, no Tk, and NO
g-code strings. It resolves a cell family + a requested cell size onto a mandrel
circumference, tiles it on the unwrapped (length x circumference) plane, and
returns ONE continuous polyline a jet can lay without stopping, which
`scaffold_gcode.generate_mandrel_cell_gcode` turns into X/A/Z. The legacy McColl
diamond helix is NOT here: it stays `scaffold_gcode`'s byte-identical "zigzag".

WHERE EACH DEFINITION COMES FROM -- two primary sources, read as PDFs with the
figures rasterised: McCosker 2022 (Adv. Mater. Technol. 7, 2200259, Table 1,
Sec. 2.1, 4, Fig. 1) and Devlin 2024 (ibid. 9, 2400419, Fig. 2B):
  "reentrant"    McCosker Fig. 1 "Auxetic" + Table 1 row 2; Devlin T02. Only
                 the TOPOLOGY comes from the figure -- struts of length h in
                 columns W/2 apart, alternate columns staggered by H/2, four
                 ribs per cell each from the TOP of a strut DOWN to the
                 neighbour's BOTTOM. W = 2*l*cos(theta), H = 2*(h - l*sin(theta))
                 are textbook relations IMPOSED here; the panel is schematic.
  "brick-closed" McCosker "Closed Cell" straight-radial; Devlin T04. Complete
                 rings every W, complete longitudinals every H.
  "brick-open"   McCosker "Open Cell" straight-radial; Devlin T03. Same rings;
                 each band's longitudinals are offset by H/2 and stop at a ring.
  "chevron"      Devlin T06 = McCosker's "wavy radial fibres": rings become a
                 TRIANGLE wave of half-period H/2 with the longitudinals on its
                 extremes. Devlin's staggered T05 is not built.

The brick families instead take `amplitude` > 0 as a SINUSOID with the
longitudinal nodes on the ZERO CROSSINGS, so the node set and topology stay
bit-for-bit the straight family's and `amplitude = 0` short-circuits to an
identical output, not a close one. Either
wave OVERHANGS the tube ends -- u runs -A to length + A, which is what both
papers draw, and `params["axial_overhang"]` carries A.

WHY THE ROUTE RETRACES INSTEAD OF RESTARTING: a MEW jet never stops, so a G0
inside a layer is a dragged fibre and a graph with odd-degree nodes cannot be
printed by restarting the pattern. Retracing is what McCosker did (Sec. 2.1) and
WHAT to retrace is a mechanical choice (`_RETRACE_PENALTY`); their second
criterion, constant mandrel rotation, is only a tie-break in the Euler walk and
is REPORTED as `TubeCellDesign.reversals`, never optimised.
"""

import heapq
import math
import re
from dataclasses import dataclass, field

#: The cell families this module builds. "zigzag" is NOT here: the legacy
#: McColl diamond helix stays `scaffold_gcode.MANDREL_STRATEGIES` and is
#: untouched by any of this.
#: "custom" is the ONE family whose geometry is not defined here: it is drawn by
#: the user and arrives as a `parse_cell_graph` string. It tiles and routes
#: through exactly the same machinery as the four literature families, which is
#: what makes it a family rather than a second engine.
MANDREL_FAMILIES = ("reentrant", "brick-closed", "brick-open", "chevron",
                    "custom")

#: Devlin's tubular cases (Sec. 2.3, 5). "scale" applies ONE factor to both cell
#: dimensions, so the aspect ratio survives and the tube LENGTH moves; "fit" sets
#: the circumferential dimension from the circumference and the axial one from
#: the request, so the length is exact and the aspect ratio distorts. Devlin's
#: third option, "no change", is absent on purpose: a tube has no edge in v, so
#: leaving the cell at its requested height makes the last cell around the
#: mandrel a different height from the other n_around - 1 -- a seam defect.
#: See `design_tube_cells` for where this repo diverges from their Fig. 1A.
FIT_MODES = ("scale", "fit")

#: FLOOR for the straight chords per wave period of a sinusoidal ring, and the
#: count used whenever it already meets the sagitta budget below. A multiple of
#: 4, so a sample lands on every zero crossing and every extreme. Sagitta at 16
#: is A*(1 - cos(pi/16)) = 0.0192*A.
WAVE_CHORDS_PER_PERIOD = 16

#: Largest chord sagitta a sinusoidal ring may be drawn with, in MILLIMETRES --
#: a quarter of a 20 um fibre, so the sampling error stays inside the fibre.
#: A FIXED 16 chords was the defect it fixes: that sagitta is 0.0192*A whatever
#: A is, so the largest amplitude the guard admits was drawn 18.85 um off the
#: true sinusoid (a whole fibre diameter) where a 0.2 mm wave was drawn to
#: 3.8 um. The count now follows A: 16 chords to A = 0.26 mm, 32 at A = 1.0 mm,
#: 36 at A = 1.05 mm.
#:
#: A module constant rather than a fibre-diameter argument because the geometry
#: is resolved before any fibre exists -- `design_tube_cells` has no
#: `fiber_diameter` in scope, and the emitter that does know it is handed an
#: already-built design.
WAVE_SAGITTA_MAX_MM = 0.005

#: Refuse rather than run a k-source Dijkstra over a graph this large.
MAX_ODD_NODES = 1600

#: Refuse rather than TILE a tube with more cells than this. MAX_ODD_NODES
#: fires only once the tiling exists, and a brick has too few odd nodes to trip
#: it, so a fine cell runs the machine out of memory first. Measured on an
#: 8 mm rod, closed brick: 25 000 cells (0.1 mm) designed in 35 s;
#: 100 000 (0.05 mm) ran out of a 2.5 GB cap after 99 s.
MAX_CELLS = 50000

#: Cost multiplier for RETRACING a member, by role. Circumferential members
#: are free to double (McCosker Sec. 2.1: doubled struts are "non-load-bearing
#: in the tensile regime"); load-bearing ones are not. See the module
#: docstring for why a plain minimum-LENGTH postman gets the auxetic wrong.
_AXIAL_PENALTY = 1000.0
_RETRACE_PENALTY = {
    "strut": 1.0,       # re-entrant vertical strut, runs in v
    "ring": 1.0,        # a circumferential fibre
    "rib": _AXIAL_PENALTY,
    "long": _AXIAL_PENALTY,
}

#: Node identity tolerance, mm. Coordinates are exact multiples of H/2 and
#: W/2, so accumulated float error is ~1e-15 mm; 1e-7 mm is 0.1 nm.
_SNAP = 1e-7

#: Bounded improvement sweeps over the greedy matching.
_MATCH_SWEEPS = 8


class UnitCellError(ValueError):
    """A cell family, a cell size or a fit that cannot be built."""


# --------------------------------------------------------------------------- #
# The resolved design
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TubeCellDesign:
    """A cell family resolved onto one mandrel, plus its routed layer.

    `cell_w` / `cell_h` are the RESOLVED cell size on the unwrapped plane:
    `cell_w` along the mandrel axis (u), `cell_h` as arc length around it (v).
    `requested` keeps what the caller asked for so a UI can show the
    compromise; `params` keeps the family knobs actually used.

    The route diagnostics are the honest cost of making a non-Eulerian cell
    graph printable without lifting the jet: `retraced_length` of
    `total_length` is laid twice, over `n_retraced` of `n_members` traversals.
    """

    family: str
    radius: float
    n_axial: int
    n_around: int
    cell_w: float
    cell_h: float
    length: float
    period_v: float
    fit: str
    params: dict
    requested: dict
    unfold_strain: dict           # re-entrant only, else None
    retraced_length: float
    total_length: float
    reversals: int
    n_members: int
    n_retraced: int
    #: the routed layer, (u, v) on the unwrapped plane. Read it through
    #: `tube_cell_route`, which is the public name.
    _route: tuple = field(default=(), repr=False, compare=False)
    #: the tiled members, and the indices of those the route lays TWICE. Read
    #: them through `tube_cell_members` / `tube_cell_retraced`.
    _members: tuple = field(default=(), repr=False, compare=False)
    _retraced: tuple = field(default=(), repr=False, compare=False)


def tube_cell_route(design):
    """ONE layer as an unwrapped (u, v) polyline -- the public route accessor.

    Geometrically continuous: consecutive points are the ends of one member,
    never a jump. Closed in the sense a rotary program needs: the last point
    has the same u as the first and a v differing by a whole number of
    `period_v`, so the next layer starts exactly where this one ended and the
    A word simply keeps unwrapping.
    """
    return list(design._route)


def tube_cell_members(design):
    """Every member of the tiled layer ONCE, as (role, [(u, v), ...]).

    Roles are "strut" / "rib" (re-entrant) and "ring" / "long" (the brick and
    chevron families). This is the pattern before routing, which is what a
    preview wants to draw; `tube_cell_route` is what the printer follows.
    """
    return [(m.role, [(m.us[i], m.v0 + m.dvs[i]) for i in range(len(m.us))])
            for m in design._members]


def tube_cell_retraced(design):
    """The members the route lays TWICE, in the same shape as
    `tube_cell_members`. For the re-entrant family this is McCosker's doubled
    struts; for the closed brick it is alternate end-ring segments."""
    all_members = tube_cell_members(design)
    return [all_members[i] for i in sorted(set(design._retraced))]


# --------------------------------------------------------------------------- #
# Re-entrant kinematics -- DERIVED HERE, not ported
# --------------------------------------------------------------------------- #
# McCosker's Table 1 gives the auxetic cell as two strains "at which the unit cell
# is completely unfolded", but the equations behind them are their Figure S1,
# which is NOT held. What follows is DERIVED from the geometry read off Figure 1,
# on three assumptions: rigid ribs with free hinges, rigid struts parallel to v,
# and "completely unfolded" meaning theta = 0, the bow-tie opened into a rectangle
# (Sec. 2.3). A GEOMETRIC UNFOLD LIMIT: no stiffness, no rib bending, no node
# welding, no Poisson's ratio. W = 2*l*cos(theta), H = 2*(h - l*sin(theta)), and
# unfolding sends W -> 2*l, H -> 2*h, so eps_longitudinal = sec(theta) - 1,
# eps_radial = (W/H)*tan(theta), theta = arccos(1/(1 + eps_u)), W = H*eps_v/tan(theta).

def reentrant_strains(cell_w, cell_h, rib_angle_deg):
    """(eps_longitudinal, eps_radial) at complete unfold, as FRACTIONS.

    Derived kinematics -- see the block comment above for the three
    assumptions and for why this is not a mechanics prediction.
    """
    theta = _check_rib_angle(rib_angle_deg)
    w = _positive("cell_w", cell_w)
    h = _positive("cell_h", cell_h)
    eps_u = 1.0 / math.cos(theta) - 1.0
    eps_v = (w / h) * math.tan(theta)
    return eps_u, eps_v


def reentrant_from_strains(eps_longitudinal, eps_radial, cell_h):
    """The inverse of `reentrant_strains`: -> (cell_w, rib_angle_deg).

    This is McCosker's Table 1 input route -- the user asks for the strains at
    which the cell is completely unfolded and the cell size follows.
    """
    eps_u = float(eps_longitudinal)
    eps_v = float(eps_radial)
    h = _positive("cell_h", cell_h)
    if not eps_u > 0.0:
        raise UnitCellError(
            "longitudinal unfold strain must be > 0, got %r. At zero the ribs "
            "are already flat and the cell is a rectangle, which is "
            "'brick-closed', not an auxetic." % (eps_longitudinal,))
    if not eps_v > 0.0:
        raise UnitCellError(
            "radial unfold strain must be > 0, got %r -- a re-entrant cell "
            "that does not expand radially has no auxetic behaviour to "
            "program." % (eps_radial,))
    theta = math.acos(1.0 / (1.0 + eps_u))
    if not theta < 0.5 * math.pi - 1e-9:
        raise UnitCellError(
            "longitudinal unfold strain %r drives the rib angle to 90 deg; "
            "the ribs would be circumferential and the cell degenerate"
            % (eps_longitudinal,))
    cell_w = h * eps_v / math.tan(theta)
    return cell_w, math.degrees(theta)


def _check_rib_angle(rib_angle_deg):
    ang = float(rib_angle_deg)
    if not 0.0 < ang < 90.0:
        raise UnitCellError(
            "rib_angle_deg must be strictly between 0 and 90, got %r. At 0 the "
            "ribs are axial and the cell is a plain grid; at 90 they are "
            "circumferential and the cell has no width." % (rib_angle_deg,))
    return math.radians(ang)


def _positive(name, value):
    v = float(value)
    if not v > 0.0:
        raise UnitCellError("%s must be positive, got %r" % (name, value))
    return v


# --------------------------------------------------------------------------- #
# Members: one fibre run between two nodes, on the unwrapped plane
# --------------------------------------------------------------------------- #
class _Member(object):
    """One member of the tiled cell graph.

    `us` are absolute axial coordinates. `dvs` are circumferential offsets
    FROM THE MEMBER'S FIRST POINT, so a traversal can be replayed at any
    unwrapped v -- which is what keeps a route continuous across the seam
    instead of jumping by a period.
    """

    __slots__ = ("role", "v0", "us", "dvs", "length", "a", "b")

    def __init__(self, role, pts):
        self.role = role
        v0 = pts[0][1]
        #: the absolute v the tiler built this member at. Only `_build_graph`
        #: reads it -- once node ids exist, a traversal is replayed from
        #: `dvs` at whatever unwrapped v the route has reached.
        self.v0 = v0
        self.us = tuple(p[0] for p in pts)
        self.dvs = tuple(p[1] - v0 for p in pts)
        self.length = sum(
            math.hypot(self.us[i + 1] - self.us[i], self.dvs[i + 1] - self.dvs[i])
            for i in range(len(pts) - 1))
        self.a = None
        self.b = None

    @property
    def dv(self):
        return self.dvs[-1]


def _node_key(u, v, period_v):
    """Seam-aware node identity: v is taken modulo the circumference.

    Without the modulo, a member that crosses the seam would land on a node the
    tiler already created one period away and the ring would come back
    disconnected. `_SNAP` quantises; the `period_v - vk` guard folds a value
    that lands a float-epsilon BELOW the period back onto zero.
    """
    vk = v % period_v
    if period_v - vk < _SNAP:
        vk = 0.0
    return (int(round(u / _SNAP)), int(round(vk / _SNAP)))


# --------------------------------------------------------------------------- #
# Family tilers -- each returns a list of _Member on the unwrapped plane
# --------------------------------------------------------------------------- #
def _reentrant_members(w, h_cell, n_axial, n_around, rib_angle_deg):
    """McCosker Fig. 1 "Auxetic" / Devlin Fig. 2B T02.

    Strut columns every W/2 along the axis, alternate columns staggered by H/2
    around the circumference; each rib runs from the TOP end of a strut DOWN
    to the BOTTOM end of the neighbouring column's strut, which is centred
    half a cell further round. That drop of l*sin(theta) is the re-entrant
    sense, and it is what was measured off Fig. 1.

    Interior columns give every strut end degree 3 (one strut + two ribs). The
    two END columns give degree 2, because their struts have ribs on one side
    only -- so the tube's end columns are NOT retraced, and the postman's
    answer is the INTERIOR strut set.
    """
    theta = _check_rib_angle(rib_angle_deg)
    strut_h = 0.5 * h_cell + 0.5 * w * math.tan(theta)
    rib_l = 0.5 * w / math.cos(theta)
    if strut_h > h_cell + _SNAP:
        raise UnitCellError(
            "re-entrant struts would overlap: strut length h = %.6f mm exceeds "
            "the cell height H = %.6f mm, so consecutive struts in one column "
            "run through each other. Reduce the rib angle (%.2f deg) or the "
            "cell width (%.6f mm), or raise the cell height."
            % (strut_h, h_cell, rib_angle_deg, w))

    n_col = 2 * n_axial + 1
    members = []

    def centre(col, j):
        return j * h_cell + (0.5 * h_cell if col % 2 else 0.0)

    for col in range(n_col):
        u = col * 0.5 * w
        for j in range(n_around):
            c = centre(col, j)
            members.append(_Member(
                "strut", [(u, c - 0.5 * strut_h), (u, c + 0.5 * strut_h)]))

    for col in range(n_col - 1):
        u0 = col * 0.5 * w
        u1 = (col + 1) * 0.5 * w
        for j in range(n_around):
            c = centre(col, j)
            # top of this strut -> bottom of the strut half a cell ABOVE
            members.append(_Member("rib", [
                (u0, c + 0.5 * strut_h),
                (u1, c + 0.5 * h_cell - 0.5 * strut_h)]))
            # bottom of this strut -> top of the strut half a cell BELOW
            members.append(_Member("rib", [
                (u0, c - 0.5 * strut_h),
                (u1, c - 0.5 * h_cell + 0.5 * strut_h)]))

    return members, {"rib_angle_deg": float(rib_angle_deg),
                     "strut_h": strut_h, "rib_l": rib_l}


def _ring_offset(v, h_cell, amplitude, wave):
    """Axial offset of a ring at circumferential position `v`."""
    if wave == "straight" or amplitude == 0.0:
        return 0.0
    if wave == "sine":
        return amplitude * math.sin(2.0 * math.pi * v / h_cell)
    # triangle wave, period H: -A at v = j*H, +A at v = (j+1/2)*H
    frac = (v / h_cell) % 1.0
    return amplitude * (4.0 * frac - 1.0 if frac < 0.5 else 3.0 - 4.0 * frac)


def _wave_chords_per_period(amplitude):
    """Chords per period H for a sinusoid of this amplitude.

    The smallest multiple of 4 at or above `WAVE_CHORDS_PER_PERIOD` whose peak
    sagitta `A*(1 - cos(pi/n))` fits `WAVE_SAGITTA_MAX_MM`. A multiple of 4 is
    what keeps a sample on every zero crossing and every extreme, and the floor
    of 16 is what makes every amplitude at or under 0.2 mm -- which is every
    amplitude this repo has frozen an output for -- sample exactly as it did
    before the count could vary."""
    n = WAVE_CHORDS_PER_PERIOD
    if amplitude <= 0.0:
        return n
    while (amplitude * (1.0 - math.cos(math.pi / n)) > WAVE_SAGITTA_MAX_MM
           and n < 4096):
        n += 4
    return n


def _ring_polyline(u_k, v0, v1, h_cell, amplitude, wave):
    """One ring segment from v0 to v1 as straight chords."""
    if wave == "straight" or amplitude == 0.0:
        return [(u_k, v0), (u_k, v1)]
    if wave == "sine":
        per = _wave_chords_per_period(amplitude)
        n = max(1, int(round(abs(v1 - v0) / h_cell * per)))
        return [(u_k + _ring_offset(v0 + (v1 - v0) * i / n, h_cell,
                                    amplitude, wave),
                 v0 + (v1 - v0) * i / n) for i in range(n + 1)]
    # chevron: the only interior vertices are the wave's extremes, which sit on
    # the half-cell boundaries. A closed-brick segment spans a whole period and
    # so carries exactly one; an open-brick segment spans a half and carries
    # none, which is why a chevron ring is piecewise linear with every bend on
    # a node or an apex and nothing in between.
    pts = [(u_k + _ring_offset(v0, h_cell, amplitude, wave), v0)]
    step = 0.5 * h_cell
    k = int(math.floor(v0 / step)) + 1
    while k * step < v1 - _SNAP:
        v = k * step
        pts.append((u_k + _ring_offset(v, h_cell, amplitude, wave), v))
        k += 1
    pts.append((u_k + _ring_offset(v1, h_cell, amplitude, wave), v1))
    return pts


def _brick_members(w, h_cell, n_axial, n_around, staggered, amplitude, wave):
    """McCosker Fig. 1 "Closed Cell"/"Open Cell"; Devlin Fig. 2B T04/T03.

    Complete rings every W along the axis. Longitudinals span one axial band
    each; `staggered` offsets every other band by H/2 so a longitudinal does
    NOT continue across a ring, which is the brick wall of Devlin T03.

    The circumferential node levels of ring k are the union of the levels of
    the bands on either side of it, so a staggered ring is cut into 2*n_around
    segments of H/2 and an unstaggered one into n_around segments of H.
    """
    period_v = n_around * h_cell

    def band_offset(k):
        return 0.5 * h_cell if (staggered and k % 2) else 0.0

    members = []
    for k in range(n_axial + 1):
        u_k = k * w
        offsets = set()
        if k > 0:
            offsets.add(band_offset(k - 1))
        if k < n_axial:
            offsets.add(band_offset(k))
        levels = sorted(j * h_cell + off
                        for off in offsets for j in range(n_around))
        for i, v0 in enumerate(levels):
            v1 = levels[i + 1] if i + 1 < len(levels) else levels[0] + period_v
            members.append(_Member(
                "ring", _ring_polyline(u_k, v0, v1, h_cell, amplitude, wave)))

    for k in range(n_axial):
        off = band_offset(k)
        du = _ring_offset(off, h_cell, amplitude, wave)
        for j in range(n_around):
            v = j * h_cell + off
            # the longitudinal is axial and straight; it meets each ring where
            # that ring actually is, which for a chevron is the apex offset.
            members.append(_Member("long", [(k * w + du, v),
                                            ((k + 1) * w + du, v)]))

    return members


# --------------------------------------------------------------------------- #
# The custom family -- a cell the user draws
# --------------------------------------------------------------------------- #
# THE CELL IS A TORUS TILE, and that is the whole trick. Coordinates reach
# `_build_graph` absolute in u and modulo the circumference in v (`_node_key`),
# so a node authored at u = 1 welds to the next cell's u = 0 by plain coordinate
# equality and a node at v = 1 welds to the next ring by the modulo. Authoring
# in the unit square therefore needs NO wrap syntax -- and the price of that
# convenience is `_custom_duplicate`, below: draw the same edge on both the
# u = 0 and the u = 1 boundary and the tiling lays one fibre twice.
#
# ROLES ARE THE RETRACE PRICE, NOT A DECORATION. `_RETRACE_PENALTY` is 1.0 for
# "ring" and 1000 for "long", so an edge marked `:free` is one the postman may
# double cheaply (McCosker's doubled struts, "non-load-bearing in the tensile
# regime") and a plain edge is one it will double only if nothing else closes
# the walk. The DEFAULT IS LOAD-BEARING: doubling a structural member without
# being asked is the silent failure, so it costs 1000 unless the drawing says
# otherwise.

#: `:free` -> a member the Euler walk may retrace cheaply; the default is the
#: load-bearing role. Both keys already exist in `_RETRACE_PENALTY`.
_CUSTOM_ROLES = {"free": "ring", "bear": "long"}

#: Slack on the unit square, in cell fractions. A click lands on a snap grid, so
#: a coordinate is exact; this only absorbs a decimal typed as 0.9999999.
_CELL_EPS = 1e-9


def parse_cell_graph(text):
    """Parse a custom cell drawing into ``(nodes, edges)``.

    Format -- nodes, a ``'|'``, then edges. This one is a honeycomb::

        0.25,0; 0.75,0; 0,0.5; 1,0.5; 0.25,1; 0.75,1 | 1-2:free; 3-1; 2-4; 3-5; 4-6

    Nodes 5 and 6 sit on the v = 1 edge, which IS the next ring's v = 0, so
    edges 3-5 and 4-6 are what stitches ring to ring; nodes 3 and 4 sit on
    u = 0 and u = 1, stitching cell to cell along the axis.

    NODES are ``(u, v)`` in the UNIT SQUARE: u along the mandrel axis, v around
    it, both a FRACTION of the resolved cell -- so one drawing resolves onto any
    mandrel and any cell size, which is what lets the fit modes move the cell
    without redrawing it. EDGES are 1-BASED node indices ``i-j``, optionally
    suffixed ``:free`` (see the block comment above for what that buys).

    Separators follow `scaffold_gcode.parse_linear_pattern`: ``';'`` or newlines
    between items, comma and/or whitespace inside a node, surrounding brackets
    tolerated. Returns ``(None, None)`` for blank/None input -- an empty drawing
    is not an error, it is a page nobody has drawn on yet.

    Raises `UnitCellError` naming the offending chunk. Everything it refuses is
    something the tiler would otherwise turn into a wrong print rather than a
    crash: a node off the unit square, an index that names no node, an edge from
    a node to itself, the same edge twice, and two nodes at one point.
    """
    if text is None:
        return None, None
    s = str(text).strip()
    if not s:
        return None, None
    if s.count("|") != 1:
        raise UnitCellError(
            "a custom cell is 'nodes | edges' -- one '|' separating the node "
            "list from the edge list; got %d" % (s.count("|"),))
    node_text, edge_text = s.split("|")

    nodes = []
    for chunk in re.split(r"[;\n]+", node_text.strip().strip("()[]{}")):
        chunk = chunk.strip().strip("()[]{}").strip()
        if not chunk:
            continue
        toks = [t for t in re.split(r"[,\s]+", chunk) if t]
        if len(toks) != 2:
            raise UnitCellError(
                "cell node %r must be two numbers (u, v)" % (chunk,))
        try:
            u, v = float(toks[0]), float(toks[1])
        except ValueError:
            raise UnitCellError(
                "cell node %r is not two numbers (u, v)" % (chunk,)) from None
        if not (-_CELL_EPS <= u <= 1.0 + _CELL_EPS
                and -_CELL_EPS <= v <= 1.0 + _CELL_EPS):
            raise UnitCellError(
                "cell node (%g, %g) is outside the unit cell: u and v are "
                "FRACTIONS of the cell, so both must be within 0..1. A node on "
                "the u = 1 or v = 1 edge joins the neighbouring cell; there is "
                "nothing beyond it to reach." % (u, v))
        nodes.append((min(1.0, max(0.0, u)), min(1.0, max(0.0, v))))
    if len(nodes) < 2:
        raise UnitCellError(
            "a custom cell needs at least 2 nodes; got %d" % (len(nodes),))

    seen_pt = {}
    for i, pt in enumerate(nodes):
        key = (round(pt[0] / _SNAP), round(pt[1] / _SNAP))
        if key in seen_pt:
            raise UnitCellError(
                "nodes %d and %d are both at (%g, %g). Two nodes at one point "
                "make a zero-length member, which cannot be routed -- move one "
                "or delete it." % (seen_pt[key] + 1, i + 1, pt[0], pt[1]))
        seen_pt[key] = i

    edges, seen_edge = [], {}
    for chunk in re.split(r"[;\n]+", edge_text.strip().strip("()[]{}")):
        chunk = chunk.strip().strip("()[]{}").strip()
        if not chunk:
            continue
        body, _sep, tag = chunk.partition(":")
        tag = tag.strip().lower()
        if tag and tag not in _CUSTOM_ROLES:
            raise UnitCellError(
                "edge %r has role %r; the only role a drawing may name is "
                "'free' (the member the route may retrace cheaply). Leave it "
                "off for a load-bearing member." % (chunk, tag))
        role = _CUSTOM_ROLES[tag] if tag else _CUSTOM_ROLES["bear"]
        ends = [t for t in re.split(r"[-\s]+", body.strip()) if t]
        if len(ends) != 2:
            raise UnitCellError(
                "cell edge %r must be two 1-based node numbers, 'i-j'"
                % (chunk,))
        try:
            a, b = int(ends[0]), int(ends[1])
        except ValueError:
            raise UnitCellError(
                "cell edge %r must be two 1-based node numbers, 'i-j'"
                % (chunk,)) from None
        for n in (a, b):
            if not 1 <= n <= len(nodes):
                raise UnitCellError(
                    "cell edge %r names node %d, but the drawing has %d node(s)"
                    % (chunk, n, len(nodes)))
        if a == b:
            raise UnitCellError(
                "cell edge %r joins node %d to itself" % (chunk, a))
        key = (min(a, b), max(a, b))
        if key in seen_edge:
            raise UnitCellError(
                "edge %d-%d is drawn twice. One member laid twice is what the "
                "route decides, not the drawing -- delete the duplicate."
                % key)
        seen_edge[key] = True
        edges.append((a - 1, b - 1, role))
    if not edges:
        raise UnitCellError("a custom cell needs at least 1 edge; got none")
    return nodes, edges


def format_cell_graph(nodes, edges):
    """The inverse of `parse_cell_graph` -- the canonical string form.

    An editor keeps ONE string as its source of truth (the pattern
    `gui_scaffold`'s linear-pattern editor set), so this is what a canvas writes
    back after every click. `%g` matches that editor's formatting, so a drawing
    that round-trips is byte-identical, not merely equal.
    """
    ns = "; ".join("%g,%g" % (u, v) for u, v in nodes)
    es = "; ".join("%d-%d%s" % (a + 1, b + 1,
                                ":free" if role == _CUSTOM_ROLES["free"] else "")
                   for a, b, role in edges)
    return "%s | %s" % (ns, es)


def _custom_duplicate(nodes, edges):
    """The boundary trap: an edge drawn on BOTH the u = 0 and the u = 1 side (or
    v = 0 and v = 1) is ONE member of the tiled graph, authored twice.

    Returns a message naming the pair, or None. Caught here rather than left to
    `_build_graph` because the tiling would accept it silently and lay that
    fibre twice on every cell -- a doubled wall thickness nobody asked for.
    """
    for axis, name, other in ((0, "u", "v"), (1, "v", "u")):
        lo, hi = {}, {}
        for a, b, _role in edges:
            pa, pb = nodes[a], nodes[b]
            if pa[axis] == 0.0 and pb[axis] == 0.0:
                lo[tuple(sorted((round(pa[1 - axis] / _SNAP),
                                 round(pb[1 - axis] / _SNAP))))] = (a, b)
            elif pa[axis] == 1.0 and pb[axis] == 1.0:
                hi[tuple(sorted((round(pa[1 - axis] / _SNAP),
                                 round(pb[1 - axis] / _SNAP))))] = (a, b)
        for key in set(lo) & set(hi):
            return ("the edge on the %s = 0 boundary and the one on %s = 1 are "
                    "the SAME member once the cell tiles -- %s = 1 of one cell "
                    "IS %s = 0 of the next. Draw it on one side only (nodes "
                    "%d-%d and %d-%d, both spanning %s %g..%g)."
                    % (name, name, name, name,
                       lo[key][0] + 1, lo[key][1] + 1,
                       hi[key][0] + 1, hi[key][1] + 1,
                       other, key[0] * _SNAP, key[1] * _SNAP))
    return None


# --------------------------------------------------------------------------- #
# Why a drawing will not print, and the one repair that is not a guess
# --------------------------------------------------------------------------- #
# A CROSSING IS NOT A JUNCTION.  The tiler builds a GRAPH from nodes and edges;
# two members whose lines cross on screen share a point, not a node, and the
# Euler walk can only turn where a node is.  So a perfectly sensible-looking
# drawing -- a chevron laid over a longitudinal, say -- tiles into two
# interleaved patterns that never touch, and is refused.
#
# Requiring the node is right: the router has to be TOLD it may turn there, and
# a splice changes the route (and the retraced fraction) rather than merely
# annotating it.  Requiring the user to spot the crossing and type its
# coordinates is not -- on the shipped 0.25 snap most crossings cannot even be
# clicked.  `splice_cell_crossings` is that repair, and it is deliberately
# MINIMAL: it adds a node only where doing so merges two pieces that are
# otherwise unreachable from each other, never at a crossing inside one piece,
# because a self-crossing is a thing the designer chose and a node there would
# add a turn nobody asked for.

def _weld_pairs(nodes, edges):
    """The boundary twins, as ``[(i, j), ...]`` -- what makes the tiling join.

    `u = 1` of one cell IS `u = 0` of the next and `v = 1` IS the next ring's
    `v = 0`, so a node on one boundary welds to a node at the SAME other
    coordinate on the opposite one. Only nodes an edge actually touches count:
    a stray node welds nothing.
    """
    used = {n for a, b, _r in edges for n in (a, b)}
    pairs = []
    for axis in (0, 1):
        other = 1 - axis
        lo, hi = {}, {}
        for i, pt in enumerate(nodes):
            if i not in used:
                continue
            key = round(pt[other] / _SNAP)
            if pt[axis] == 0.0:
                lo[key] = i
            elif pt[axis] == 1.0:
                hi[key] = i
        for key in sorted(set(lo) & set(hi)):
            pairs.append((lo[key], hi[key]))
    return pairs


def cell_components(nodes, edges):
    """The drawing's connected pieces, as sorted lists of 0-based node indices.

    Connectivity here is the TILED graph's, reached without tiling anything:
    the edges, plus `_weld_pairs`. One piece means the pattern can be walked as
    one fibre; more than one means it cannot, and is what `_hierholzer` reports
    after the fact as "an Euler circuit covered N of M traversals".

    NOT a second connectivity test -- `design_tube_cells` still decides, and
    this agrees with it on everything except the winding-parity case named in
    `cell_connect_fault`.
    """
    par = list(range(len(nodes)))

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            par[a] = b

    for a, b in _weld_pairs(nodes, edges):
        union(a, b)
    for a, b, _r in edges:
        union(a, b)
    groups = {}
    for i in {n for a, b, _r in edges for n in (a, b)}:
        groups.setdefault(find(i), []).append(i)
    return [sorted(g) for _k, g in sorted(groups.items())]


def cell_connect_fault(nodes, edges):
    """Why the drawing will not print as one fibre -- ``(code, message)``.

    `code` is one of "axial", "ring", "pieces", or None when the drawing looks
    printable. It names the fault instead of counting traversals, which is what
    the Euler walk can do and this cannot: the walk reports the SIZE of the
    piece it landed in, having no idea why the rest was unreachable.

    ONE case it cannot see, and it is why the engine keeps the last word: two
    diagonals that only ever step (+-1, +-1) satisfy every test here and still
    split the tiling into its odd and even halves. `design_tube_cells` refuses
    it; this returns None. The page always shows the ENGINE's verdict and uses
    this only to explain one.
    """
    if not edges:
        return None, ""
    used = {n for a, b, _r in edges for n in (a, b)}
    welds = []
    for axis in (0, 1):
        other = 1 - axis
        lo = {round(nodes[i][other] / _SNAP) for i in used
              if nodes[i][axis] == 0.0}
        hi = {round(nodes[i][other] / _SNAP) for i in used
              if nodes[i][axis] == 1.0}
        welds.append(bool(lo & hi))
    if not welds[0]:
        return ("axial",
                "nothing joins one cell to the next ALONG the axis: no node on "
                "the u = 0 edge has a twin at the same v on the u = 1 edge, so "
                "the pattern tiles into rings that never meet.")
    if not welds[1]:
        return ("ring",
                "nothing joins one ring to the next AROUND the tube: no node "
                "on the v = 0 edge has a twin at the same u on the v = 1 edge, "
                "so the pattern tiles into strips that never meet.")
    comps = cell_components(nodes, edges)
    if len(comps) > 1:
        named = " and ".join("{%s}" % ", ".join(str(i + 1) for i in g)
                             for g in comps[:3])
        return ("pieces",
                "the drawing is in %d separate pieces -- %s%s. Lines that "
                "CROSS do not join: only a shared node does."
                % (len(comps), named, " and more" if len(comps) > 3 else ""))
    return None, ""


def _seg_cross(p, q, r, s):
    """Parameters ``(t, u)`` where segments pq and rs meet, or None.

    Both are clamped to [0, 1] with `_CELL_EPS` slack, so a crossing AT an
    endpoint is reported rather than missed -- that is the whole of the case
    where a chevron's apex already sits on a longitudinal and the only repair
    needed is to route the longitudinal through the node already there.
    """
    d1 = (q[0] - p[0], q[1] - p[1])
    d2 = (s[0] - r[0], s[1] - r[1])
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < 1e-12:                 # parallel, including collinear
        return None
    t = ((r[0] - p[0]) * d2[1] - (r[1] - p[1]) * d2[0]) / den
    u = ((r[0] - p[0]) * d1[1] - (r[1] - p[1]) * d1[0]) / den
    if -_CELL_EPS <= t <= 1.0 + _CELL_EPS and -_CELL_EPS <= u <= 1.0 + _CELL_EPS:
        return t, u
    return None


def _interior(t):
    return _CELL_EPS < t < 1.0 - _CELL_EPS


def _find_join(nodes, edges, comp_of):
    """The one crossing to splice next, or None.

    Deterministic and cheapest-first: a crossing inside the authored cell beats
    one against a neighbouring copy, then the lower edge indices. Only pairs in
    DIFFERENT pieces are considered, so a self-crossing is never touched.

    The neighbour offsets matter and are not decoration. Two members can miss
    inside the square and still meet once the cell tiles, and the point they
    meet at is expressed in each member's OWN cell, one differing from the
    other by the integer offset. `_build_graph` welds those two by plain
    coordinate identity, so each edge is split in its own frame and nothing has
    to know they were ever the same point.
    """
    best = None
    for i, (a1, b1, _r1) in enumerate(edges):
        for j in range(i + 1, len(edges)):
            a2, b2, _r2 = edges[j]
            if comp_of.get(a1) == comp_of.get(a2):
                continue
            p, q = nodes[a1], nodes[b1]
            for du in (0, -1, 1):
                for dv in (0, -1, 1):
                    r = (nodes[a2][0] + du, nodes[a2][1] + dv)
                    t = (nodes[b2][0] + du, nodes[b2][1] + dv)
                    hit = _seg_cross(p, q, r, t)
                    if hit is None:
                        continue
                    ta, tb = hit
                    if not (_interior(ta) or _interior(tb)):
                        continue        # they only touch at ends, which weld
                    rank = (abs(du) + abs(dv), i, j, round(ta, 9))
                    if best is None or rank < best[0]:
                        best = (rank, i, j, ta, tb)
    return None if best is None else best[1:]


def _lerp(p, q, t):
    return (p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1]))


def _node_at(nodes, pt):
    """Index of the node at `pt`, appending one if there is none."""
    key = (round(pt[0] / _SNAP), round(pt[1] / _SNAP))
    for i, n in enumerate(nodes):
        if (round(n[0] / _SNAP), round(n[1] / _SNAP)) == key:
            return i
    nodes.append((min(1.0, max(0.0, pt[0])), min(1.0, max(0.0, pt[1]))))
    return len(nodes) - 1


def _split_edge(nodes, edges, ei, t):
    """Split edge `ei` at its own parameter `t`, in place. Returns the node.

    A `t` at either end splits nothing and returns the endpoint already there,
    which is what lets a drawing whose apex ALREADY sits on the member it has
    to join gain zero new nodes -- only the routing through it changes.
    """
    a, b, role = edges[ei]
    if not _interior(t):
        return a if t <= 0.5 else b
    mid = _node_at(nodes, _lerp(nodes[a], nodes[b], t))
    if mid in (a, b):
        return mid
    edges[ei] = (a, mid, role)
    edges.insert(ei + 1, (mid, b, role))
    return mid


def splice_cell_crossings(nodes, edges):
    """Join the drawing's separate pieces at the points where they CROSS.

    Returns ``(nodes, edges, added)`` -- new lists and how many nodes were
    added, `0` when nothing needed doing or nothing could be done. The geometry
    is untouched: every splice puts a node on a line that was already drawn
    exactly there, so the picture is identical and only the topology changes.

    Minimal by construction. One crossing is spliced at a time and the pieces
    are recounted after each, so a second crossing between two pieces that are
    already joined is left alone -- the drawing gets the nodes it needs to
    print and no others.

    It can return with the drawing still in pieces: two patterns that never
    cross have no junction to add, and saying so is the useful answer. Neither
    boundary-weld fault is repairable here either, since a missing twin is a
    node the user has to place.
    """
    nodes = [tuple(n) for n in nodes]
    edges = [tuple(e) for e in edges]
    added = 0
    for _round in range(2 * len(edges) + 4):
        comps = cell_components(nodes, edges)
        if len(comps) <= 1:
            break
        comp_of = {}
        for k, g in enumerate(comps):
            for i in g:
                comp_of[i] = k
        found = _find_join(nodes, edges, comp_of)
        if found is None:
            break
        i, j, ta, tb = found
        before = len(nodes)
        # j first: splitting i inserts an edge and would shift j's index.
        _split_edge(nodes, edges, j, tb)
        _split_edge(nodes, edges, i, ta)
        added += len(nodes) - before
        if len(cell_components(nodes, edges)) == len(comps):
            break                       # made no progress; do not spin
    return nodes, edges, added


def _custom_members(w, h_cell, n_axial, n_around, nodes, edges):
    """Tile a drawn cell over the unwrapped plane -- one `_Member` per edge per
    cell, at absolute (u, v). The weld to the neighbouring cells is done by
    `_build_graph`'s coordinate identity, not here."""
    members = []
    for i in range(n_axial):
        for j in range(n_around):
            for a, b, role in edges:
                ua, va = nodes[a]
                ub, vb = nodes[b]
                members.append(_Member(role, [((i + ua) * w, (j + va) * h_cell),
                                              ((i + ub) * w, (j + vb) * h_cell)]))
    return members


def preview_route(nodes, edges, mandrel_diameter, cell_w, n_around,
                  n_axial=3):
    """Route a SHORT tile of a drawn cell -- the live-validator entry point.

    Returns the same ``(points, diagnostics)`` as the full solve, for a tube
    only `n_axial` cells long. `n_around` is NOT shortened: the wrap has to
    close, so the circumferential count is the real one.

    WHY A SHORT TILE IS A FAIR PROXY, measured on this branch: routing cost is
    superlinear (599 ms at 320 cells, 2.1 s at 600) but every per-length figure
    has converged by three cells. On a degree-3 honeycomb the retraced fraction
    is IDENTICAL at n_axial 2, 3, 6 and 20, and the fraction of layer time
    below CTS moves 27.9% -> 28.4% (a 4.19 mm cell) and 42.0% -> 42.3% (a
    1.57 mm cell) between three cells and twenty. `reversals` is the one figure
    that does not converge -- it scales with the axial count, so read it per
    cell, never as the whole tube's.
    """
    radius = 0.5 * _positive("mandrel_diameter", mandrel_diameter)
    period_v = 2.0 * math.pi * radius
    n_around = _pinned_count("n_around", n_around)
    n_axial = _pinned_count("n_axial", n_axial)
    if n_around is None or n_axial is None:
        raise UnitCellError("preview_route needs both cell counts")
    dup = _custom_duplicate(nodes, edges)
    if dup is not None:
        raise UnitCellError(dup)
    members = _custom_members(_positive("cell_w", cell_w),
                              period_v / n_around, n_axial, n_around,
                              nodes, edges)
    return _route(members, period_v)


# --------------------------------------------------------------------------- #
# Graph, Chinese postman, Euler circuit
# --------------------------------------------------------------------------- #
def _build_graph(members, period_v):
    """Assign node ids to every member end and return the adjacency."""
    keys = {}
    pos = []
    for m in members:
        ends = []
        for u, dv in ((m.us[0], m.dvs[0]), (m.us[-1], m.dvs[-1])):
            v_abs = m.v0 + dv
            key = _node_key(u, v_abs, period_v)
            idx = keys.get(key)
            if idx is None:
                idx = len(pos)
                keys[key] = idx
                pos.append((u, v_abs % period_v))
            ends.append(idx)
        m.a, m.b = ends
        if m.a == m.b:
            raise UnitCellError(
                "a %s member closes on itself; the tiling produced a "
                "zero-length or whole-circumference member, which cannot be "
                "routed" % (m.role,))
    adj = [[] for _ in pos]
    for mi, m in enumerate(members):
        w = m.length * _RETRACE_PENALTY[m.role]
        adj[m.a].append((mi, w, m.b))
        adj[m.b].append((mi, w, m.a))
    return pos, adj


def _dijkstra(adj, src, targets, n_nodes):
    inf = float("inf")
    dist = [inf] * n_nodes
    prev = [None] * n_nodes
    settled = [False] * n_nodes
    dist[src] = 0.0
    remaining = len(targets)
    pq = [(0.0, src)]
    while pq and remaining > 0:
        d, u = heapq.heappop(pq)
        if settled[u]:
            continue
        settled[u] = True
        if u in targets:
            remaining -= 1
        for mi, w, nb in adj[u]:
            nd = d + w
            if nd < dist[nb] - 1e-12:
                dist[nb] = nd
                prev[nb] = (u, mi)
                heapq.heappush(pq, (nd, nb))
    return dist, prev


def _greedy_matching(odd, cost):
    """Greedy over the sorted pair costs, then bounded 2-opt. Deterministic.

    A heuristic, not a proof of minimality.
    """
    k = len(odd)
    pairs = sorted((cost[i][j], i, j) for i in range(k) for j in range(i + 1, k))
    taken = [False] * k
    match = []
    for c, i, j in pairs:
        if not taken[i] and not taken[j]:
            taken[i] = taken[j] = True
            match.append((i, j))
    for _ in range(_MATCH_SWEEPS):
        improved = False
        for x in range(len(match)):
            for y in range(x + 1, len(match)):
                a, b = match[x]
                c, d = match[y]
                cur = cost[a][b] + cost[c][d]
                alt1 = cost[a][c] + cost[b][d]
                alt2 = cost[a][d] + cost[b][c]
                if alt1 < cur - 1e-12 and alt1 <= alt2:
                    match[x], match[y] = (a, c), (b, d)
                    improved = True
                elif alt2 < cur - 1e-12:
                    match[x], match[y] = (a, d), (b, c)
                    improved = True
        if not improved:
            break
    return match


def _eulerize(members, adj, n_nodes):
    """Return the extra member instances that make every degree even.

    Chinese postman: pair the odd-degree nodes by a minimum-cost perfect
    matching over the shortest-path metric, then duplicate every member on
    each matched path. The metric is `length * _RETRACE_PENALTY[role]`, which
    is a MECHANICAL cost, not a length -- see the module docstring.
    """
    deg = [0] * n_nodes
    for m in members:
        deg[m.a] += 1
        deg[m.b] += 1
    odd = [n for n in range(n_nodes) if deg[n] % 2]
    if not odd:
        return []
    if len(odd) > MAX_ODD_NODES:
        raise UnitCellError(
            "%d odd-degree nodes, over the %d cap: routing this would run a "
            "Dijkstra from every one of them. Use a larger cell, a shorter "
            "tube, or a smaller mandrel." % (len(odd), MAX_ODD_NODES))

    target = set(odd)
    index = {n: i for i, n in enumerate(odd)}
    k = len(odd)
    cost = [[0.0] * k for _ in range(k)]
    prevs = []
    for i, src in enumerate(odd):
        dist, prev = _dijkstra(adj, src, target, n_nodes)
        prevs.append(prev)
        for j, dst in enumerate(odd):
            cost[i][j] = dist[dst]

    extra = []
    for i, j in _greedy_matching(odd, cost):
        node = odd[j]
        prev = prevs[i]
        guard = 0
        while node != odd[i]:
            step = prev[node]
            if step is None:
                raise UnitCellError(
                    "the tiled graph is disconnected: no path between two "
                    "odd-degree nodes, so no single continuous fibre can "
                    "cover it")
            node, mi = step
            extra.append(mi)
            guard += 1
            if guard > len(members) + 1:
                raise UnitCellError("shortest-path reconstruction did not "
                                    "terminate")
    return extra


def _hierholzer(members, instances, n_nodes, start):
    """Euler circuit over `instances` (member indices, with duplicates).

    The unused-edge pick prefers to keep the mandrel turning the same way --
    McCosker Sec. 2.1 criterion 2 -- by taking, where one exists, an edge whose
    signed dv matches the sign of the edge we arrived on. That is a tie-break
    inside an otherwise standard Hierholzer, not an optimisation: the reversal
    count it happens to reach is REPORTED, never asserted.
    """
    adj = [[] for _ in range(n_nodes)]
    for ii, mi in enumerate(instances):
        m = members[mi]
        adj[m.a].append(ii)
        adj[m.b].append(ii)
    used = [False] * len(instances)

    def pick(node, want_sign):
        fallback = None
        for ii in adj[node]:
            if used[ii]:
                continue
            m = members[instances[ii]]
            dv = m.dv if m.a == node else -m.dv
            if want_sign and dv * want_sign > 0.0:
                return ii
            if fallback is None:
                fallback = ii
        return fallback

    node_stack = [start]
    edge_stack = []
    circuit = []
    sign = 0.0
    while node_stack:
        node = node_stack[-1]
        ii = pick(node, sign)
        if ii is None:
            node_stack.pop()
            if edge_stack:
                circuit.append(edge_stack.pop())
            sign = 0.0
        else:
            used[ii] = True
            m = members[instances[ii]]
            forward = (m.a == node)
            nxt = m.b if forward else m.a
            dv = m.dv if forward else -m.dv
            sign = 1.0 if dv > 0.0 else (-1.0 if dv < 0.0 else sign)
            edge_stack.append((ii, forward))
            node_stack.append(nxt)
    circuit.reverse()
    if len(circuit) != len(instances):
        raise UnitCellError(
            "the tiled graph is not connected: an Euler circuit covered %d of "
            "%d traversals, so the pattern cannot be printed as one fibre"
            % (len(circuit), len(instances)))
    return circuit


def _route_points(members, instances, circuit, pos, start):
    u0, v0 = pos[start]
    pts = [(u0, v0)]
    cv = v0
    for ii, forward in circuit:
        m = members[instances[ii]]
        if forward:
            for i in range(1, len(m.us)):
                pts.append((m.us[i], cv + m.dvs[i]))
            cv += m.dv
        else:
            base = cv - m.dv
            for i in range(len(m.us) - 2, -1, -1):
                pts.append((m.us[i], base + m.dvs[i]))
            cv = base
    return pts


def _count_reversals(pts):
    sign = 0
    reversals = 0
    for i in range(1, len(pts)):
        dv = pts[i][1] - pts[i - 1][1]
        if abs(dv) < 1e-12:
            continue
        s = 1 if dv > 0.0 else -1
        if sign and s != sign:
            reversals += 1
        sign = s
    return reversals


@dataclass(frozen=True)
class CellSize:
    """The cell's RESOLVED size on one mandrel, before anything is tiled.

    Inlined in `design_tube_cells`, this arithmetic would be unreachable
    without also routing an Euler circuit -- so a caller that wanted only
    "how big is one cell, really" would pay the whole solve or
    keep a second copy of the rounding. The GUI's canvas readout is that
    caller. Splitting it out is what keeps ONE answer to the question.
    """
    radius: float
    period_v: float          #: the circumference, mm -- v's period
    n_around: int
    n_axial: int
    cell_w: float            #: RESOLVED axial size, mm (may differ from asked)
    cell_h: float            #: RESOLVED arc size, mm (always closes exactly)
    length: float            #: n_axial * cell_w, the tube the design will be
    fit: str


def resolve_cell_size(mandrel_diameter, target_length, cell_w, cell_h,
                      *, fit="scale", n_around=None, n_axial=None):
    """Round a requested cell onto a mandrel -> `CellSize`. No tiling, no route.

    THE ONE PLACE the two fit modes are spelled. `design_tube_cells` calls it
    and builds on its answer, so a readout that calls it is showing the number
    the generator will actually use, not a re-derivation of it.
    """
    if fit not in FIT_MODES:
        raise UnitCellError(
            "unknown fit mode %r; expected one of %s. Devlin's third option, "
            "'no change', cannot close a circumference and is not offered."
            % (fit, ", ".join(FIT_MODES)))
    radius = 0.5 * _positive("mandrel_diameter", mandrel_diameter)
    req_len = _positive("target_length", target_length)
    req_w = _positive("cell_w", cell_w)
    req_h = _positive("cell_h", cell_h)
    period_v = 2.0 * math.pi * radius

    pinned_around = _pinned_count("n_around", n_around)
    pinned_axial = _pinned_count("n_axial", n_axial)

    # ROUND, in both places. Devlin's Fig. 1A is the closest worked comparator
    # held -- a 3 mm mandrel (period_v = 9.42478 mm), a 0.950 mm / 950 um cell,
    # a 4.0 mm request -- and n_axial reproduces their scaled 4.284 mm / 856.8 um /
    # 0.3670 mm^2 / 3.427 mm only at n_axial = 5, i.e. under ceil(): round() over
    # a 0.8568 mm resolved cell gives 4, as it does for their fitted p = 800 um.
    # n_around does NOT follow: round(9.9209) and ceil(9.9208) are both 10 where
    # Devlin resolves 11, so that one is not a rounding difference at all and
    # stays UNEXPLAINED without their Supporting Information C, which is not held.
    n_a = (pinned_around if pinned_around is not None
           else int(round(period_v / req_h)))
    if n_a < 2:
        raise UnitCellError(
            "a cell height of %.4f mm puts %d cell(s) around a %.4f mm "
            "circumference; at least 2 are needed for a pattern to close. Use "
            "a cell height under %.4f mm or a larger mandrel."
            % (req_h, n_a, period_v, 0.5 * period_v))

    if fit == "scale":
        scale = period_v / (n_a * req_h)
        res_w = scale * req_w
        res_h = scale * req_h
        n_x = (pinned_axial if pinned_axial is not None
               else max(1, int(round(req_len / res_w))))
    else:
        res_h = period_v / n_a
        n_x = (pinned_axial if pinned_axial is not None
               else max(1, int(round(req_len / req_w))))
        res_w = req_len / n_x

    if abs(n_a * res_h - period_v) > 1e-9 * max(1.0, period_v):
        raise UnitCellError(
            "internal: %d cells of %.12f mm do not close the %.12f mm "
            "circumference" % (n_a, res_h, period_v))

    return CellSize(radius=radius, period_v=period_v, n_around=n_a,
                    n_axial=n_x, cell_w=res_w, cell_h=res_h,
                    length=n_x * res_w, fit=fit)


# --------------------------------------------------------------------------- #
# The public resolver
# --------------------------------------------------------------------------- #
def design_tube_cells(family, mandrel_diameter, target_length, cell_w, cell_h,
                      *, fit="scale", n_around=None, n_axial=None,
                      **family_params):
    """Resolve a cell family onto a mandrel and route one printable layer.

    family: one of `MANDREL_FAMILIES`.
    mandrel_diameter: mm. `cell_w`/`cell_h` are the REQUESTED cell size on the
      unwrapped plane -- `cell_w` along the axis, `cell_h` as arc length around
      the circumference (McCosker Sec. 4's "longitudinal spacing" and "radial
      spacing"; their homogeneous prints used 2 mm and 4.18 mm on an 8 mm
      mandrel, which is 6 cells around).
    target_length: mm, a REQUEST -- an integer number of cells has to fit.
    fit: one of `FIT_MODES`; see the module docstring.
    n_around / n_axial: OPTIONAL positive-integer overrides for the two counts
      this function otherwise rounds to. They exist because the rounding is a
      choice with neighbours worth offering -- one cell more around the
      circumference is a cell `n/(n+1)` as tall, on the same rod, and the
      designer page tabulates that ladder. Given, each one REPLACES its
      `round()` and changes nothing else: the fit still decides which side of
      the cell absorbs the compromise, the closure check still runs, and the
      tiling and route are the ones the rounded count would have produced had
      it landed there. Both `None` (the default) is the historical route,
      byte for byte.
    family_params: `rib_angle_deg` for "reentrant"; `amplitude` (mm) for the
      brick families and, required and non-zero, for "chevron"; `graph` for
      "custom" -- either a `parse_cell_graph` string or an already-parsed
      ``(nodes, edges)``, whose node coordinates are FRACTIONS of the resolved
      cell, so the fit modes move the cell without redrawing it.
    """
    if family not in MANDREL_FAMILIES:
        raise UnitCellError(
            "unknown tube cell family %r; built: %s (the McColl diamond helix "
            "is not here -- it is scaffold_gcode's 'zigzag' strategy)"
            % (family, ", ".join(MANDREL_FAMILIES)))
    size = resolve_cell_size(mandrel_diameter, target_length, cell_w, cell_h,
                             fit=fit, n_around=n_around, n_axial=n_axial)
    radius, period_v = size.radius, size.period_v
    n_around, n_axial = size.n_around, size.n_axial
    res_w, res_h, length = size.cell_w, size.cell_h, size.length
    if n_around * n_axial > MAX_CELLS:
        raise UnitCellError(
            "%d cells around x %d along is %d cells, over the %d cap: tiling "
            "and routing a tube this fine takes gigabytes. Use a larger cell "
            "(%.4f x %.4f mm resolved) or a shorter tube."
            % (n_around, n_axial, n_around * n_axial, MAX_CELLS, res_w,
               res_h))
    # re-read for the `requested` record: `resolve_cell_size` has already
    # refused any non-positive one, so these cannot raise here.
    req_len, req_w, req_h = float(target_length), float(cell_w), float(cell_h)

    params = _resolve_params(family, res_w, family_params)
    amplitude = params.get("amplitude", 0.0)
    if amplitude and 2.0 * amplitude >= res_w - _SNAP:
        raise UnitCellError(
            "amplitude %.4f mm is too large for a resolved cell width of "
            "%.4f mm: the wave's peak-to-peak excursion (%.4f mm) reaches the "
            "next ring, so neighbouring rings would intersect. Keep the "
            "amplitude under %.4f mm."
            % (amplitude, res_w, 2.0 * amplitude, 0.5 * res_w))

    if family == "custom":
        nodes, edges = params["graph"]
        members = _custom_members(res_w, res_h, n_axial, n_around, nodes, edges)
        strains = None
    elif family == "reentrant":
        members, extra_params = _reentrant_members(
            res_w, res_h, n_axial, n_around, params["rib_angle_deg"])
        params.update(extra_params)
        eps_u, eps_v = reentrant_strains(res_w, res_h, params["rib_angle_deg"])
        strains = {"longitudinal": eps_u, "radial": eps_v}
    else:
        members = _brick_members(
            res_w, res_h, n_axial, n_around,
            staggered=(family == "brick-open"),
            amplitude=amplitude, wave=params["wave"])
        strains = None

    route, diag = _route(members, period_v)

    return TubeCellDesign(
        family=family, radius=radius, n_axial=n_axial, n_around=n_around,
        cell_w=res_w, cell_h=res_h, length=length, period_v=period_v,
        fit=fit, params=params,
        requested={"target_length": req_len, "cell_w": req_w, "cell_h": req_h},
        unfold_strain=strains,
        retraced_length=diag["retraced_length"],
        total_length=diag["total_length"], reversals=diag["reversals"],
        n_members=len(members), n_retraced=diag["n_retraced"],
        _route=tuple(route), _members=tuple(members),
        _retraced=diag["retraced_ids"])


def _pinned_count(name, value):
    """`None`, or a positive int -- a count override is never a rounded float.

    Refused rather than coerced: a caller that has 6.5 cells around has a bug,
    and silently taking 6 of them is the clamping this page exists to remove.
    """
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise UnitCellError(
            "%s must be a whole number of cells or None, got %r"
            % (name, value)) from None
    if n != value or n < 1:
        raise UnitCellError(
            "%s must be a positive whole number of cells, got %r"
            % (name, value))
    return n


def _resolve_params(family, res_w, given):
    """Validate and default the per-family knobs; reject anything unknown."""
    allowed = {"reentrant": ("rib_angle_deg",),
               "brick-closed": ("amplitude",),
               "brick-open": ("amplitude",),
               "chevron": ("amplitude",),
               "custom": ("graph",)}[family]
    unknown = sorted(set(given) - set(allowed))
    if unknown:
        raise UnitCellError(
            "family %r takes %s; got unexpected %s"
            % (family, ", ".join(allowed) or "no parameters",
               ", ".join(unknown)))
    if family == "custom":
        graph = given.get("graph")
        nodes, edges = (parse_cell_graph(graph)
                        if isinstance(graph, str) or graph is None
                        else graph)
        if not nodes or not edges:
            raise UnitCellError(
                "the custom family needs a cell drawing: 'graph' is the "
                "'nodes | edges' string a cell editor produces, and it is "
                "empty. Draw a cell, or pick one of the built families: %s"
                % (", ".join(f for f in MANDREL_FAMILIES if f != "custom"),))
        dup = _custom_duplicate(nodes, edges)
        if dup is not None:
            raise UnitCellError(dup)
        # the RESOLVED drawing, so `TubeCellDesign.params` records what was
        # actually tiled rather than the text it was typed as.
        return {"graph": (tuple(nodes), tuple(edges)),
                "n_nodes": len(nodes), "n_edges": len(edges)}
    if family == "reentrant":
        ang = given.get("rib_angle_deg", 30.0)
        _check_rib_angle(ang)
        return {"rib_angle_deg": float(ang)}

    amp = float(given.get("amplitude", 0.0))
    if amp < 0.0:
        raise UnitCellError("amplitude must be >= 0, got %r" % (amp,))
    if family == "chevron":
        if amp <= 0.0:
            raise UnitCellError(
                "the chevron family needs an amplitude > 0; a chevron of zero "
                "amplitude is the straight closed brick, so ask for "
                "'brick-closed' instead")
        return {"amplitude": amp, "wave": "chevron", "axial_overhang": amp}
    return {"amplitude": amp, "wave": "sine" if amp > 0.0 else "straight",
            "axial_overhang": amp}


def _route(members, period_v):
    """Tile -> Eulerize -> Hierholzer -> polyline, plus the diagnostics.

    Returns (points, diagnostics). The circuit starts at the lowest-numbered
    node so the route is reproducible run to run.
    """
    pos, adj = _build_graph(members, period_v)
    extra = _eulerize(members, adj, len(pos))
    instances = list(range(len(members))) + extra
    start = min(members[0].a, members[0].b)
    circuit = _hierholzer(members, instances, len(pos), start)
    pts = _route_points(members, instances, circuit, pos, start)

    base_len = sum(m.length for m in members)
    retraced = sum(members[mi].length for mi in extra)
    closure = (pts[-1][1] - pts[0][1]) / period_v
    if (abs(pts[-1][0] - pts[0][0]) > 1e-9
            or abs(closure - round(closure)) > 1e-9):
        raise UnitCellError(
            "internal: the routed layer did not close -- it ends at "
            "(%.9f, %.9f) against a start of (%.9f, %.9f), which is %.6f "
            "periods" % (pts[-1][0], pts[-1][1], pts[0][0], pts[0][1], closure))

    return pts, {"retraced_length": retraced,
                 "total_length": base_len + retraced,
                 "reversals": _count_reversals(pts),
                 "n_retraced": len(extra),
                 "retraced_ids": tuple(extra)}
