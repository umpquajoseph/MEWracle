#!/usr/bin/env python3
"""machine_model.py -- optional multi-body printer model for the g-code player.

The base player teleports one nozzle marker to the commanded point; a machine
model replaces that with the real printer broken into its moving parts. PURE
GEOMETRY + JSON -- no VTK, no physics; the player owns the VTK actor loading and
`test_machine_model.py` pins the placement math.

ROLES + CHAIN. Four possible parts, each with a fixed role: **head** (carries
the nozzle, required), **gantry** (an optional body the head rides on), **bed**
(carries the collector / deposited print), **frame** (static, world-fixed).
Each moving role declares the AXES it translates on and its PARENT, and that
chain is the whole kinematics -- every joint is a pure single-axis translation,
so forward kinematics is vector addition, no solver and no rotation. Presets
live in `CHAINS`; an arbitrary rig sets `kinematics:"custom"` with its own
`axes`/`parents`, and the old axis-keyed schema (`parts:{x,y,z}` +
`static_stl`) still loads as the MEWron chain (`from_dict`).

TWO RIGID RENDER FRAMES (Frame A). The MACHINE frame (world-fixed) holds the
static body and the moving parts, nozzle tip at a FIXED world Z. The BED frame
holds the collector and everything resting on it -- deposited scaffold AND
toolpath -- translated as one group by the collector's motion (`bed_offset` /
`bed_group`), so the reveal / sag / mesh pipeline is untouched.

SIGN CONVENTION. A part on the TOOL chain translates +g along each axis it
drives; a part on the COLLECTOR chain translates -g. The two combine so the
nozzle is ALWAYS exactly over the active print point, one standoff above it, on
every rig -- the invariant the tests pin. The machine is then rigidly shifted so
g-code (0,0,0) puts the tip at world (0, 0, standoff); `part_offsets` is the
per-machine nudge. THE SLIDE is registered to the loaded BED STL rather than to
a machine envelope: `find_bed_top_face`'s corner plus `slide_offset` is the
in-plane `registration` `place_scene` gives the build group AND the tool chain.
The bed STL never takes it and `contact_alignment` closes the contact normal, so
the slide is XY only. `limits` clamp a rendered body (3D-sim only), never bytes.

A ROTARY collector is the one joint that is not a translation, and it is kept
OUT of this arithmetic: rotating about the collector axis moves no part's
ORIGIN, so `place`/`place_scene` return what they always did and the rotation is
reported ALONGSIDE as (angle, axis, pivot) in the BUILD GROUP's own frame, which
makes mandrel-off byte-identical by construction. See `RotaryAxis` for the
rotary-mount and datum contracts.

A FIVE-AXIS collector is an ordered PAIR of those joints, and it is the same
arithmetic twice: `profile.rotaries` is a tuple, `place_scene` reports one
(angle, axis, pivot) triple per joint under `collector_rotations`, and a pair
still moves no part's origin. `rotaries` runs COLLECTOR-FIRST -- index 0 is the
joint the collector is bolted to, each later entry carries the one before it,
and that is also the order a consumer applies the triples to a point in the
collector's own frame. One joint is the shipped mandrel and is untouched: the
list is length 1, `rotary` is `rotaries[0]`, and every key it ever emitted is
emitted with the same value. `solve_rotaries` inverts the pair -- given a
surface normal in the collector's frame it returns the readings that bring that
normal up to the fixed vertical nozzle -- and `five_axis_command` adds the XYZ
that puts the nozzle over the point. Neither is used by `place`/`place_scene`;
they are the design side, and the round trip through the reported triples is
what `test_machine_model.py` checks them with."""
import json
import math
import os

# Bundled read-only assets (the printhead STL); stdlib-only, drags nothing in.
from app_paths import beside_app, resource_path

# The MEWron 0.2 printhead mesh: named by the shipped printer preset, the
# player's default head and the Machine link's fallback. MEWracle.spec bundles
# it at this same relative path.
PRINTHEAD_STL = "machine_models/MEWron_0.2/Printhead STL.stl"

# Bundled part meshes that moved, old -> new, relative to the repo root (or
# the bundle). A preset, a saved Setup page or a Print History snapshot
# written before the move still names the old path, and resolves to the new.
RENAMED_PARTS = {
    "new printhead STL.stl": PRINTHEAD_STL,
    # the old top-level head this build no longer ships: the MEWron 0.2 head
    "printhead.stl": PRINTHEAD_STL,
}


def resolve_part(name, base_dir=None):
    """Absolute path to a part STL that a profile or a Setup field names, or
    None for no name. An absolute path is taken as given. A relative one is
    tried against `base_dir` (when given), then the BUNDLE, then the folder
    beside the app (see MachineProfile._resolve for why each). A RENAMED_PARTS
    name found nowhere is retried under its new name, and so is an absolute
    path to the old file in the repo, bundle or app folder. A part found
    nowhere returns its first candidate, and the caller treats it as missing."""
    if not name:
        return None
    if os.path.isabs(name):
        if os.path.exists(name):
            return name
        homes = {os.path.normcase(os.path.abspath(p))
                 for p in (resource_path(), beside_app())}
        old = os.path.basename(name)
        if (old in RENAMED_PARTS and os.path.normcase(
                os.path.dirname(os.path.abspath(name))) in homes):
            hit = resolve_part(RENAMED_PARTS[old], base_dir)
            if os.path.exists(hit):
                return hit
        return name
    cands = ([os.path.join(base_dir, name)] if base_dir is not None else [])
    cands += [resource_path(name), beside_app(name)]
    for c in cands:
        if os.path.exists(c):
            return c
    new = RENAMED_PARTS.get(name.replace("\\", "/"))
    if new:
        hit = resolve_part(new, base_dir)
        if os.path.exists(hit):
            return hit
    return cands[0]


def part_exists(name):
    """True when `name` resolves (resolve_part) to a file that exists."""
    path = resolve_part(name)
    return bool(path) and os.path.isfile(path)

import numpy as np

# Canonical coordinate order (g-code XYZ -> array index).
AXES = ("x", "y", "z")
_AX = {"x": 0, "y": 1, "z": 2}

# The moving roles, leaf-to-root render-ish order. `frame` is the static root.
ROLES = ("head", "gantry", "bed")

# Built-in kinematic chains: per preset, the axes each role drives + its parent.
# `frame` (or a missing/None parent) is the static world root. These are the only
# rigs the GUI exposes as presets; "custom" reads axes/parents from the profile.
CHAINS = {
    # MEWron: XY printhead (X carriage on a Y gantry) + Z bed.
    "mewron": {
        "axes":    {"head": ("x",), "gantry": ("y",), "bed": ("z",)},
        "parents": {"head": "gantry", "gantry": "frame", "bed": "frame"},
    },
    # XYZ printhead over a static bed.
    "xyz_head": {
        "axes":    {"head": ("x", "y", "z"), "gantry": (), "bed": ()},
        "parents": {"head": "frame", "gantry": "frame", "bed": "frame"},
    },
    # XY bed (the print moves in plane) + Z printhead.
    "xy_bed_z_head": {
        "axes":    {"head": ("z",), "gantry": (), "bed": ("x", "y")},
        "parents": {"head": "frame", "gantry": "frame", "bed": "frame"},
    },
}


#: ISO 6983 / RS-274 rotary words and the linear axis each turns about. `A` about
#: X is what `scaffold_gcode.generate_mandrel_gcode` emits and what
#: `mew_frame.CylinderFrame` reads back, so it is the only one exercised; B and C
#: cost nothing to allow and stop a second collector orientation needing a schema
#: change. A word OUTSIDE this table is legal only when the profile also names
#: the linear axis it turns about: Terranova 2024's five-axis files use `U` and
#: `V`, which ISO reserves for secondary LINEAR axes, so there is no mapping to
#: infer -- but refusing the word outright is the schema change this table exists
#: to avoid. No axis and an unknown word still raises.
ROTARY_WORDS = {"a": "x", "b": "y", "c": "z"}


class RotaryAxis:
    """A REVOLUTE joint: the rotating mandrel the fibre is collected on.

    Schema, under `"rotary"` in a machine.json: `word` (A|B|C, the ISO 6983
    rotary word), `role` (which body the mandrel is mounted on), `radius` (BARE
    mandrel radius, mm), `length` (along the axis, mm), `stl` (an optional
    HOLDER body -- the rod itself is always drawn, from the JOB's radius).

    THE MOUNT. `role` is "bed" (the stage rides the Z collector, so the gap
    closes as it does on a plate) or "frame" (a static stage the tool moves
    around). WHERE the axis is comes from the manual DATUM on the holder in the
    bed slot, three numbers in that STL's own frame picked in `bed_calibrator`,
    and that is the ONLY source.

    THE RADIUS IS A JOB PARAMETER, not a machine one, and lives here only so the
    viewer has one number in one place: a mandrel is a consumable, so the
    profile's value is a default and the player overwrites it from the scaffold
    parameters before placing. Getting it wrong is silent, so `seat` names it.

    THE FRAME AND THE SIGN. The deposited tube arrives already wrapped in the
    collector's co-rotating frame (`mew_frame.CylinderFrame.to_world`), where
    the mandrel axis IS the group frame's `axis` through the ORIGIN, so the
    pivot is the origin and a machine.json cannot put the two out of register.
    Rotating the group by **+A** puts the point deposited at reading A back
    under the nozzle; asserted in `test_machine_model.py` rather than commented.

    THE PIVOT IS NOW A FIELD, and it defaults to the origin, so a mandrel is the
    sentence above unchanged. It exists for the five-axis PAIR: a tilt stacked on
    a rotate has a pivot that is NOT the origin -- the trunnion's tilt axis sits
    wherever the cradle bolts to the carriage -- and a pivot silently taken as
    the origin produces geometry that is plausible and wrong everywhere except
    on the axis itself.

    **IT IS A POINT IN THIS JOINT'S OWN STL FRAME**, which is what
    `bed_calibrator` picks and the same convention `bed_top_z` / `bed_corner`
    already use for the bed. That is not one frame per part in disguise: `place`
    loads EVERY part STL into one machine frame by the same rigid `shift` plus
    that part's driven motion, and the schema carries no per-part pose, so a
    point picked in the C body's file and one picked in the A body's file are
    directly comparable on any rig -- not only on one whose bodies happen to
    share a CAD frame.

    THE COLLECTOR'S ORIGIN SITS AT `rotaries[0].pivot`. The collector-carrying
    joint's axis passes through the collector's own origin -- that is the
    "mounted concentric with the spin axis" statement the Machine page used to
    DISCLOSE as an assumption, made structural instead. So the pivots the
    KINEMATICS want (`rotary_transform`, `collector_rotations`) are these ones
    taken RELATIVE to joint 0's: joint 0's comes out identically zero, and the
    joint under it carries the real axis-to-axis offset, which is the trunnion
    number the page used to disclose as the second assumption. Every shipped
    single-joint rig has no pivot at all, so relative and absolute agree there
    and nothing moves. `_relative_pivots` is the one place the rule lives.

    A word outside A/B/C is accepted only together with an explicit `axis`; see
    `ROTARY_WORDS`.

    `zero_deg` IS THE READING AT THE JOINT'S ZERO POSITION, and it is the third
    machine fact this class carries alongside the axis and the pivot. Our own
    generator homes every rotary at 0, so it defaults to 0 and nothing shipped
    moves. It is here rather than in whatever emits the g-code because a machine
    whose zero is not its geometric zero is the normal case, and the alternative
    is that constant living in the emitter AND in the parser -- two copies of
    one number, which is how they drift apart. `word_from_angle` /
    `angle_from_word` are the only two places it is ever applied.

    **NO VALUE IS ESTABLISHED FOR ANY REAL RIG, AND 90 IS NOT ONE.** This field
    was added after reading Terranova 2024's six supplied five-axis files, whose
    tilt word `U` opens at 90 in every one -- and the first version of this
    docstring called that "their tilt reading 90 where the table is flat", which
    the files do not say. `U` sweeps (69.28-110.63 in `Fig3D_Wavy.g`,
    0.72-145.00 in `Fig5_Bifurcating_vessel.g`), so 90 is a rest pose, and a
    machine homed at 0 whose part simply starts level-and-tilted opens at 90
    too. Their zero convention is unknown. Do not read `zero_deg = 90` off that
    paper and put it in a profile; the number has to come from the rig.
    """

    def __init__(self, word="A", role="bed", radius=1.5, length=30.0, stl=None,
                 pivot=None, axis=None, zero_deg=0.0, home=None,
                 seat=None, home_standoff=0.0):
        w = str(word or "A").strip().lower()
        ax = None if axis is None else str(axis).strip().lower()
        if ax is not None and ax not in _AX:
            raise ValueError("rotary axis must be one of x/y/z, got %r" % (axis,))
        if ax is None and w not in ROTARY_WORDS:
            raise ValueError("rotary word must be one of A/B/C (or name its "
                             "axis), got %r" % (word,))
        self.word = w.upper()
        #: the LINEAR axis the mandrel turns about ("x" for the A word).
        self.axis = ax if ax is not None else ROTARY_WORDS[w]
        #: a point on that axis, in the frame this joint is mounted in at its
        #: zero reading. The origin for every shipped single-joint rig.
        #: True when a pivot was actually supplied. A pick that lands on the
        #: origin is a REAL datum and must not read as "never calibrated" --
        #: which is the whole reason `rotary.axis_height` was deleted rather
        #: than defaulted. `to_dict` writes on this rather than on `any(pivot)`,
        #: so a pivot-less joint still writes no pivot and no shipped profile
        #: changes bytes.
        self.pivot_set = pivot is not None
        self.pivot = (np.zeros(3) if pivot is None
                      else np.asarray(pivot, float).reshape(3).copy())
        #: the WORD this joint reads when it is geometrically at zero. 0 on ours.
        self.zero_deg = float(zero_deg or 0.0)
        #: WHERE THE NOZZLE TIP IS AT G-CODE (0, 0, 0), picked on this joint's
        #: own STL, in that STL's frame -- the same convention as `pivot`, which
        #: is why it is a point here and not a height. It is the interim answer
        #: to a rig whose homed position could not be calibrated: for now the
        #: home is a point picked on the C axis, until a proper homing method
        #: replaces it. So it is deliberately a PICK and not a derivation, and `home_set` keeps an
        #: unpicked joint distinguishable from one picked at the origin for the
        #: same reason `pivot_set` does.
        self.home_set = home is not None
        self.home = (np.zeros(3) if home is None
                     else np.asarray(home, float).reshape(3).copy())
        #: THE WORKING DISTANCE `home` WAS PICKED AT, mm. The pick is a NOZZLE
        #: TIP position and every other datum in this program is a SURFACE, so
        #: the deposition point g-code (0, 0, 0) names is this far BELOW it.
        #: `gui_buildplate._rotary_pair_joints` writes it beside the pick, from
        #: the standoff field in force when the profile was authored -- it is
        #: stored rather than read live because an old pick must not change
        #: meaning the moment that field is edited.
        #:
        #: 0.0, the default, is the original reading: the pick taken AS a
        #: surface point. That keeps every profile written without the key, and every
        #: hand-built joint in the suites, on the arithmetic
        #: it already had.
        self.home_standoff = float(home_standoff or 0.0)
        #: WHERE THE COLLECTOR IS SEATED on this joint's body -- the point its
        #: BOTTOM EDGE touches, picked on this joint's own STL, same convention
        #: as `pivot` and `home`. Datum 1 of the two every printing mode needs
        #: where the collector (the thing being printed on) sits
        #: relative to whatever holds it -- the bed for 3 axis, the mandrel
        #: mount for 4 axis, the C axis for 5 axis. This is the five-axis
        #: counterpart of `bed_top_z` + `bed_corner` and of the mandrel axis in
        #: the holder's frame.
        #:
        #: IT IS NOT THE PIVOT, and that is the whole point of it existing. The
        #: pivot is a point on the axis of ROTATION; this is where the part
        #: STANDS. Before it, `_relative_pivots` used the pivot as the
        #: collector's origin, which put a dome's centre of curvature on the
        #: table and left its rim hanging `mount_axial` above it.
        #:
        #: FREE IN ALL THREE AXES, deliberately -- a collector bolted off-centre
        #: really does orbit the spin axis, and refusing to represent that would
        #: be the software insisting the rig is what it is not.
        #:
        #: `seat_set`, not `any(seat)`, for the reason `pivot_set` exists.
        self.seat_set = seat is not None
        self.seat_point = (np.zeros(3) if seat is None
                           else np.asarray(seat, float).reshape(3).copy())
        # "frame" is a legal mount and is NOT in ROLES -- ROLES is the moving
        # bodies, and the static root is exactly what a fixed rotary stage bolts
        # to. Validating against ROLES alone silently downgraded every static
        # stage to a bed-mounted one, which is invisible in the scene until the
        # collector starts sinking with Z.
        self.role = role if role in (ROLES + ("frame",)) else "bed"
        self.radius = float(radius)
        self.length = float(length)
        self.stl = stl or None

    @classmethod
    def from_dict(cls, d):
        if not d:
            return None
        return cls(word=d.get("word", "A"), role=d.get("role", "bed"),
                   radius=d.get("radius", 1.5), length=d.get("length", 30.0),
                   stl=d.get("stl"), pivot=d.get("pivot"),
                   axis=d.get("axis"),
                   zero_deg=d.get("zero_deg", 0.0), home=d.get("home"),
                   seat=d.get("seat"),
                   home_standoff=d.get("home_standoff", 0.0))
        # "axis_height" in an old profile
        # is read and dropped: it was a guess for the no-datum case (see above).

    def to_dict(self):
        # `pivot` and `axis` are written only when they carry information, so a
        # single-joint profile round trips to the same bytes it always did.
        d = {"word": self.word, "role": self.role,
             "radius": self.radius, "length": self.length}
        if self.stl:
            d["stl"] = self.stl
        if self.pivot_set:
            d["pivot"] = [float(v) for v in self.pivot]
        if self.axis != ROTARY_WORDS.get(self.word.lower()):
            d["axis"] = self.axis
        if self.zero_deg:
            d["zero_deg"] = self.zero_deg
        if self.home_set:
            d["home"] = [float(v) for v in self.home]
            if self.home_standoff:
                d["home_standoff"] = float(self.home_standoff)
        if self.seat_set:
            d["seat"] = [float(v) for v in self.seat_point]
        return d

    # -- the word, and the angle it means ----------------------------------- #
    def word_from_angle(self, deg):
        """The number to COMMAND for a geometric rotation of `deg`."""
        return float(deg) + self.zero_deg

    def angle_from_word(self, deg):
        """The geometric rotation a commanded reading of `deg` means."""
        return float(deg) - self.zero_deg

    # -- geometry ----------------------------------------------------------- #
    def direction(self):
        """Unit vector along the mandrel axis, in the build group's frame."""
        v = np.zeros(3)
        v[_AX[self.axis]] = 1.0
        return v

    def normal(self):
        """Unit vector from the axis to the point UNDER THE NOZZLE.

        `to_world` puts azimuth 0 at `+z` for the A word (`r*cos(0)`), which is
        the direction the nozzle looks down from. For B and C it is the next axis
        round in the same cyclic order, so the three stay consistent."""
        v = np.zeros(3)
        v[_AX[AXES[(_AX[self.axis] + 2) % 3]]] = 1.0
        return v

    def seat(self):
        """The extra translation the build group needs so that g-code Z keeps
        meaning WALL HEIGHT ABOVE THE BARE SURFACE.

        The wrapped geometry puts a point at wall height h a distance `R0 + h`
        from the axis, while every other datum in this program -- the standoff,
        the plate contact, the `underside_clearance` check -- is measured from a
        surface at 0. Seating the group by `-R0` along the nozzle normal makes
        the two agree, so the nozzle sits one standoff above the mandrel SURFACE
        rather than one standoff above its AXIS. Without it a 6 mm mandrel would
        hold the print 3 mm into the printhead."""
        return -self.radius * self.normal()

    def rotation(self, a_deg):
        """(angle_deg, axis unit vector, pivot) for the build group, its own frame."""
        return (float(a_deg), self.direction(), self.pivot.copy())

    def body(self, start=0.0):
        """(centre, direction, radius, length) of the collector cylinder, in the
        build group's frame -- what the viewer needs to draw the mandrel. `start`
        is where the tube's near end sits along the axis (the generator's
        `start_x`), so the drawn mandrel is centred on the print rather than on
        the origin."""
        d = self.direction()
        return (d * (start + 0.5 * self.length), d, self.radius, self.length)


# --------------------------------------------------------------------------- #
# TWO-AXIS KINEMATICS. Everything below is the FIVE-AXIS design side: it turns a
# surface normal in the collector's own frame into the pair of rotary readings
# that brings that normal up to the fixed vertical nozzle, and then into the
# commanded XYZ. `place`/`place_scene` never call any of it -- they still only
# REPORT (angle, axis, pivot) triples -- so a rig with one joint or none reaches
# none of this code. The check that the two sides agree is a round trip through
# the reported triples, in `test_machine_model.py`.
#
# THE NOZZLE IS FIXED AND VERTICAL. That is the whole premise of a table-table
# machine and the reason `+Z` is hardcoded as the target below rather than read
# from the profile: on this layout the head only translates, so the surface has
# to come to the nozzle. A head-tilting rig is a different machine and would
# need its own solver, not a parameter here.
# --------------------------------------------------------------------------- #

#: the direction a surface normal must end up pointing for the nozzle to be
#: perpendicular to the surface under it.
NOZZLE_UP = np.array([0.0, 0.0, 1.0])


class Unreachable(ValueError):
    """No rotary reading brings the requested surface normal under the nozzle.

    Raised rather than returned so a caller cannot mistake an unreachable
    orientation for a zero reading, which is what silently prints the underside
    of a collector at a plausible-looking angle."""


def rotation_matrix(axis, angle_deg):
    """Right-hand Rodrigues 3x3 for `angle_deg` about the unit vector `axis`.

    The same rotation `place_scene`'s reported (angle, axis, pivot) asks a viewer
    for, written once here so the solver and the report cannot drift."""
    k = np.asarray(axis, float)
    n = np.linalg.norm(k)
    if n == 0.0:
        raise ValueError("rotation axis must be non-zero")
    k = k / n
    th = np.radians(float(angle_deg))
    kx = np.array([[0.0, -k[2], k[1]],
                   [k[2], 0.0, -k[0]],
                   [-k[1], k[0], 0.0]])
    return np.eye(3) + np.sin(th) * kx + (1.0 - np.cos(th)) * (kx @ kx)


#: THE AXIS EVERY `mew_frame` COLLECTOR REVOLVES ABOUT, in the collector's own
#: frame. `CylinderFrame` and `RevolutionFrame` both put the meridian on X and
#: the parallels in Y-Z (`mew_frame.RevolutionFrame.to_world` returns
#: `(x, r sin phi, r cos phi)`, and `from_world` inverts it with `hypot(y, z)` /
#: `atan2(y, z)`), so a collector this program builds is X-axial whatever it is
#: bolted to. It is stated here rather than imported because this module is pure
#: geometry and imports no frame; `test_machine_model` reads the convention back
#: out of `mew_frame` and fails if the two ever disagree.
COLLECTOR_AXIS = "x"


def collector_mount(rotaries):
    """The 3x3 carrying a point from the COLLECTOR's own frame into the frame
    the collector-carrying joint turns in -- i.e. how the collector is bolted on.

    A collector is a part with an axis of revolution and a table has a spin
    axis; the two are the same line, because that is what bolting a body of
    revolution onto a turntable means. `COLLECTOR_AXIS` says where the first one
    points in the collector's own coordinates, `rotaries[0].axis` where the
    second one points on the machine, and this is the rotation between them.
    Nothing is invented and nothing is asked for: both are already machine
    facts.

    THE MINIMAL ROTATION, and the freedom it leaves is joint 0's. Any two
    rotations carrying one axis onto another differ by a spin ABOUT that axis,
    which is exactly what the collector-carrying joint does -- so the choice
    costs at most a constant offset in that joint's reading, which is what
    `zero_deg` is for. The geodesic one is taken because it is the only choice
    that needs no further convention.

    A PAIR ONLY. A single-joint rig is left at the identity even when its word
    is not A: every shipped mandrel spins about X, which is the collector axis,
    so the rotation would be the identity anyway -- and a mandrel declared about
    B or C already draws its rod along the joint axis while its tube geometry is
    built along X, which is a real pre-existing inconsistency and not this
    slice's to change. (Filed rather than fixed: no shipped profile does it.)
    """
    js = tuple(rotaries)
    if len(js) < 2:
        return np.eye(3)
    # MEMOISED ON THE ONE INPUT IT READS, joint 0's axis letter. A five-axis
    # layer solve asks for it at every point, and rebuilding one constant 3x3
    # per point was a tenth of the generator's rotary solve.
    # Handed out read-only so no caller can edit the shared copy.
    got = _MOUNT_CACHE.get(js[0].axis)
    if got is None:
        got = _collector_mount_for(js[0].axis)
        got.setflags(write=False)
        _MOUNT_CACHE[js[0].axis] = got
    return got


_MOUNT_CACHE = {}


def _collector_mount_for(axis):
    """`collector_mount`'s rotation for a collector-carrying joint about `axis`."""
    a = np.zeros(3)
    a[_AX[COLLECTOR_AXIS]] = 1.0
    b = np.zeros(3)
    b[_AX[axis]] = 1.0
    d = float(np.dot(a, b))
    if d > 1.0 - 1e-12:
        return np.eye(3)
    if d < -1.0 + 1e-12:
        # antiparallel: any perpendicular axis turns it, and the spin left over
        # is the joint's own. Pick one deterministically.
        perp = np.zeros(3)
        perp[(_AX[COLLECTOR_AXIS] + 1) % 3] = 1.0
        return rotation_matrix(perp, 180.0)
    return rotation_matrix(np.cross(a, b), np.degrees(np.arccos(d)))


def collector_mount_rotation(axis):
    """`(angle_deg, unit axis)` swinging a collector's own `COLLECTOR_AXIS` onto
    the linear axis `axis`, or `None` when it is already there.

    Split out of `collector_mount_triple` so that a consumer with no joints in
    hand -- `bed_calibrator`, drawing the collector in the picker window -- aims
    it by the SAME rotation the kinematics mount it by. A second `if axis == z`
    in a viewer is how a picker comes to show a dome standing up while the
    player lays it on its side."""
    a = np.zeros(3)
    a[_AX[COLLECTOR_AXIS]] = 1.0
    b = np.zeros(3)
    b[_AX[str(axis).strip().lower()]] = 1.0
    k = np.cross(a, b)
    if np.linalg.norm(k) < 1e-12:
        if float(np.dot(a, b)) > 0.0:
            return None                       # already aligned
        k = np.zeros(3)
        k[(_AX[COLLECTOR_AXIS] + 1) % 3] = 1.0
        return (180.0, k)
    return (float(np.degrees(np.arccos(float(np.dot(a, b))))),
            k / np.linalg.norm(k))


def collector_mount_triple(rotaries):
    """`collector_mount` as an `(angle_deg, axis, pivot)` triple, or `None` when
    it is the identity -- so a chain of triples and the matrix cannot drift."""
    js = tuple(rotaries)
    if len(js) < 2 or js[0].axis == COLLECTOR_AXIS:
        return None
    rot = collector_mount_rotation(js[0].axis)
    if rot is None:
        return None
    return (rot[0], rot[1], np.zeros(3))


def collector_origin(rotaries, mount_axial=None):
    """WHERE THE COLLECTOR'S OWN ORIGIN SITS, in joint 0's STL frame.

    THE COLLECTOR STANDS ON ITS MOUNT DATUM, whichever datum that is. The
    collector's origin is that datum walked back along joint 0's axis by
    `mount_axial` -- the collector's own axial coordinate at its bottom edge
    (`mew_frame.DepositionFrame.mount_axial`). The axis is joint 0's, which is
    where a collector bolted to that joint points.

    WHICH DATUM is a calibration question, and it is the only thing the pick
    changes:

      * **seat picked** -- `rotaries[0].seat_point`, the measured top face.
      * **nothing picked** -- joint 0's pivot, the axis point. The rig is
        uncalibrated, so the best available statement is "concentric with the
        C axis and resting on the picked axis point", which is what the
        `[rig]` banner already tells the user it is assuming.

    THE `mount_axial` TERM IS NOT PART OF THAT CHOICE. Subtracted only on the
    seat branch, an uncalibrated rig would nail the collector's CONSTRUCTION
    origin to the pivot. That origin is a
    point in mid-air inside the part -- the centre of curvature on a dome or a
    bowl -- so the part hung off the pivot by its own geometry: a bowl of
    radius 10 sat entirely BELOW the pivot, its rim 3.62 mm down and its base
    10 mm down (the bowl collector sat under the C axis) with no seat
    picked. The same arithmetic hung a dome ABOVE the pivot by the same
    amounts. The answer is one datum less: the term
    that puts the part's BOTTOM EDGE on its mount applies to both datums,
    because it describes the COLLECTOR and not the pick.

    Byte-compatibility for a pre-seat profile is therefore deliberately NOT
    preserved. It was only ever compatibility with the misplacement.

    THE MACHINE MODEL NEVER GUESSES `mount_axial`. The frame owns it, for the
    same reason `seat_offset` is the frame's: it is a fact about the collector's
    geometry. `None` means the caller has no frame in hand (`place_scene` from a
    consumer that never built one), and then neither datum can be honoured as
    more than a point -- there is no extent to walk back by -- so the answer is
    the pivot. Widening that branch to return the seat would discard a real
    measurement on one path for the sake of a different one.

    Empty in, zeros out. `mount_axial` is IGNORED on a mandrel: a rod is gripped
    at its ends and its placement is `RotaryAxis.seat`, untouched by any of this.
    """
    js = tuple(rotaries)
    if not js:
        return np.zeros(3)
    # A MANDREL IS NOT ON THIS CHAIN: a rod is gripped at its ends and placed by
    # `RotaryAxis.seat`, so a single joint keeps the bare pivot it always had.
    # NO FRAME, no extent to walk back by, and the pre-seat answer stands --
    # unchanged, and deliberately not widened by this fix.
    if len(js) < 2 or mount_axial is None:
        return js[0].pivot.copy()
    datum = js[0].seat_point if js[0].seat_set else js[0].pivot
    a = np.zeros(3)
    a[_AX[js[0].axis]] = 1.0
    return datum - float(mount_axial) * a


def collector_home_offset(rotaries, mount_axial=None):
    """The vector from the collector's origin to the picked G-CODE ZERO, in
    joint 0's STL frame -- zeros when no home has been picked.

    Datum 2 of the two: where the nozzle is zeroed relative to the
    collector, the origin of the g-code. This is often at the edge of the
    collector or off it entirely, so the travel fibre does not cross an
    important part of the print. Its three-axis counterpart is
    the plate's face + anchor corner and its four-axis one is the rod's near end
    + surface; on five axes it is picked, because collectors vary too much for a
    ladder.

    It is expressed against the COLLECTOR, so it is the pick less
    `collector_origin` -- which is why moving the seat does not move the zero,
    and re-picking the zero does not move the collector. Two datums, two facts,
    no coupling.

    THE PICK IS A NOZZLE TIP AND THE ANSWER IS A SURFACE POINT, so
    `RotaryAxis.home_standoff` comes off it here, once, in the only function
    that turns the pick into geometry. The home is picked hovering one
    standoff (3.5 mm) up -- the ordinary way of setting a Z zero -- while
    g-code (0, 0, 0) names a DEPOSITION point, because `place_scene` holds the
    tip one standoff above whatever the file commands. Reading the pick
    directly made the file's zero the tip position, which is one standoff too
    high everywhere.

    IT COMES OFF ALONG +Z, the contact normal, and that is the same assumption
    the rest of this module already runs on: `bed_top_z` is a Z, the contact
    gap is a Z, and `gcode_player._build_machine` reads `collector_home[2]`. A
    rig whose nozzle does not look down -Z needs all of those changed
    together, not this one line.

    `home_standoff` defaults to 0 on a joint built without it, so every profile
    written before the key existed reads exactly as it did.
    """
    js = tuple(rotaries)
    if not js or not js[0].home_set:
        return np.zeros(3)
    lift = np.zeros(3)
    lift[_AX["z"]] = float(js[0].home_standoff)
    return js[0].home - lift - collector_origin(js, mount_axial)


def _relative_pivots(rotaries, mount_axial=None):
    """Each joint's pivot in the COLLECTOR's own frame, collector-first.

    `RotaryAxis.pivot` is a point in that joint's own STL file, which is what
    the calibrator picks. The KINEMATICS want the same points measured from the
    collector's origin, so the conversion is one subtraction -- of
    `collector_origin`. A PICKED pivot can come out a real offset, because a
    collector seated off the spin axis genuinely orbits it.

    AN UNPICKED PIVOT IS NOT A POINT AT THE STL ORIGIN, and subtracting from it
    would invent geometry. It is the uncalibrated assumption the `[rig]` banner
    states in words -- *"collector assumed concentric with the C axis, and the
    A tilt axis assumed through the collector origin"* -- so its answer is
    zero BY DEFINITION, wherever the collector origin happens to be.

    With nothing picked, `collector_origin` IS the zero pivot, so the
    subtraction would return zero on its own -- but only by accident, since
    standing the collector on its mount moves the origin off that point. Relying
    on it slides an R=10 dome's assumed tilt axis 3.6236 mm off the collector and
    bends the file: on a profile that picks nothing, the 232 deposition moves
    stop being a rigid translation and take 65 distinct shift vectors spanning
    1.083 to 3.217 mm, with no rotary word moving. `pivot_set` makes the banner's sentence the
    code's rule instead of a coincidence, and on every profile that picks its
    pivots -- including the rig's own preset -- nothing here changes.

    See `RotaryAxis`'s docstring for why the two files' frames are comparable at
    all. Empty in, empty out; every shipped single-joint rig has no pivot and no
    seat, so this returns the zeros it always had."""
    js = tuple(rotaries)
    if not js:
        return ()
    p0 = collector_origin(js, mount_axial)
    return tuple((j.pivot - p0) if j.pivot_set else np.zeros(3) for j in js)


def compose_rotations(triples):
    """(R, t) for applying `(angle_deg, axis, pivot)` triples IN ORDER to a
    point: `p -> R @ p + t`.

    The one composition rule in the program, so a consumer that applies the
    triples one at a time and a caller that wants a single matrix cannot end up
    doing different things. `place_scene` reports triples rather than matrices
    on purpose -- a reader can check them with its own Rodrigues -- and this is
    how a renderer turns a reported chain into the 4x4 VTK actually takes."""
    r = np.eye(3)
    t = np.zeros(3)
    for ang, axis, pivot in triples:
        rj = rotation_matrix(axis, ang)
        piv = np.asarray(pivot, float).reshape(3)
        r = rj @ r
        t = rj @ (t - piv) + piv
    return r, t


def rotary_transform(rotaries, angles_deg, mount_axial=None):
    """(R, t) mapping a point in the COLLECTOR's own frame to EXPORTED G-CODE
    coordinates: `g = R @ p + t`.

    `rotaries` is collector-first, `angles_deg` runs with it, and each joint is
    applied about its own pivot before the next one carries the result -- which
    is exactly what a consumer does with the `collector_rotations` triples, in
    list order. The pivots are the COLLECTOR-frame ones (`_relative_pivots`),
    because that is the frame the point being carried is in; joint 0's is zero
    there, so a shipped mandrel comes back as (R, 0) exactly as before.

    THE MOUNT COMES FIRST. A collector is X-axial in its own coordinates
    (`COLLECTOR_AXIS`) and the joint it is bolted to need not be, so
    `collector_mount` reorients it before any joint turns it. It is the identity
    for a mandrel and for any pair whose collector-carrying joint is X, so every
    scene that worked before this is bit-identical. Composing it HERE rather
    than at each call site is what keeps the generator
    (`scaffold_gcode._five_axis_point`), the parser
    (`gcode_to_fibers.FiveAxisReader`) and the renderer
    (`gcode_player._collector_pose`) on one chain -- all three reach it through
    this function, so none of them can be mounted differently from the others.

    THE G-CODE ZERO IS SUBTRACTED HERE, LAST, and that is what makes the return
    exported coordinates rather than the mount frame's. `collector_home_offset`
    is a constant in the MACHINE frame -- you jog the nozzle to a spot with the
    rotaries at zero and set the datum there, and turning a rotary afterwards
    does not move it -- so it comes off after the rotation, not before. With no
    home picked the offset is zeros and this is the mount frame it always was.

    Doing it in this function rather than in `five_axis_command` is the same
    decision as the mount above: the generator, the parser and the renderer all
    arrive here, so a file written from one zero cannot be read or drawn against
    another."""
    r, t = rotary_transforms(rotaries, [tuple(angles_deg)], mount_axial)
    return r[0], t[0]


def rotary_transforms(rotaries, angles_deg, mount_axial=None):
    """`rotary_transform` for N poses at once: `(R[N, 3, 3], t[N, 3])`.

    `angles_deg` is `(N, joints)`. THE ONE CHAIN, batched -- `rotary_transform`
    is this with one row, so the generator placing a whole layer and the parser
    reading one move back cannot be composed differently. Each link is
    `compose_rotations`' own step, `r = rj @ r; t = rj @ (t - piv) + piv`,
    with `rj` stacked over the rows.
    """
    js = tuple(rotaries)
    ang = np.asarray(angles_deg, dtype=np.float64).reshape(-1, len(js))
    n = ang.shape[0]
    mount = collector_mount_triple(js)
    chain = [] if mount is None else [(np.full(n, float(mount[0])), mount[1],
                                       mount[2])]
    chain += [(ang[:, i], j.direction(), piv)
              for i, (j, piv) in enumerate(zip(js, _relative_pivots(
                  js, mount_axial)))]
    r = np.broadcast_to(np.eye(3), (n, 3, 3))
    t = np.zeros((n, 3))
    for a, axis, pivot in chain:
        rj = _rotation_matrices(axis, a)
        piv = np.asarray(pivot, float).reshape(3)
        r = rj @ r
        t = (rj @ (t - piv)[:, :, None])[:, :, 0] + piv
    return r, t - collector_home_offset(js, mount_axial)


def _rotation_matrices(axis, angles_deg):
    """`rotation_matrix` for one axis and N angles, `(N, 3, 3)`. Same formula."""
    k = np.asarray(axis, float)
    nk = np.linalg.norm(k)
    if nk == 0.0:
        raise ValueError("rotation axis must be non-zero")
    k = k / nk
    th = np.radians(np.asarray(angles_deg, float))
    kx = np.array([[0.0, -k[2], k[1]],
                   [k[2], 0.0, -k[0]],
                   [-k[1], k[0], 0.0]])
    return (np.eye(3) + np.sin(th)[:, None, None] * kx
            + (1.0 - np.cos(th))[:, None, None] * (kx @ kx))


def _signed_angle(u, v, axis):
    """Degrees of right-hand rotation about `axis` that carries `u` onto `v`.

    Both are projected perpendicular to the axis first, so this answers only the
    part of the question a revolute joint can answer. A vector lying ALONG the
    axis has no projection and no answer -- gimbal lock -- and 0 is returned,
    which is correct there precisely because the rotation does nothing to it."""
    # PLAIN FLOAT ARITHMETIC on purpose: this runs several times per point of
    # every five-axis layer, and numpy's per-call overhead on 3-vectors was
    # most of the generator's rotary solve. Same formula.
    ax, ay, az = (float(c) for c in axis)
    an = math.sqrt(ax * ax + ay * ay + az * az)
    ax, ay, az = ax / an, ay / an, az / an
    ux, uy, uz = (float(c) for c in u)
    vx, vy, vz = (float(c) for c in v)
    du = ux * ax + uy * ay + uz * az
    dv = vx * ax + vy * ay + vz * az
    ux, uy, uz = ux - du * ax, uy - du * ay, uz - du * az
    vx, vy, vz = vx - dv * ax, vy - dv * ay, vz - dv * az
    if (math.sqrt(ux * ux + uy * uy + uz * uz) < 1e-12
            or math.sqrt(vx * vx + vy * vy + vz * vz) < 1e-12):
        return 0.0
    cx = uy * vz - uz * vy
    cy = uz * vx - ux * vz
    cz = ux * vy - uy * vx
    return math.degrees(math.atan2(cx * ax + cy * ay + cz * az,
                                   ux * vx + uy * vy + uz * vz))


def rotary_solutions(rotaries, normal):
    """EVERY reading tuple (collector-first, degrees) that brings the
    collector-frame unit `normal` onto `NOZZLE_UP`. Empty when there is none.

    THE GEOMETRY, because it is short and it is the whole slice. Write the base
    joint's axis `a0` and the collector-carrying joint's `a1`, and let `m` be the
    normal after the inner joint has turned but before the base one has. Turning
    about `a1` cannot change a vector's component along `a1`, so `m . a1 =
    normal . a1`; turning about `a0` cannot change one along `a0`, so `m . a0 =
    NOZZLE_UP . a0`. Two plane constraints and `|m| = 1`: a circle met by two
    planes, so generically TWO solutions, the pair every five-axis post
    processor has to choose between. Each reading then falls out as a signed
    angle in its own joint's plane.

    Degenerate cases are refused rather than approximated: parallel axes are not
    a two-axis wrist and raise, and a normal outside the reachable band returns
    empty. One joint is solved directly and returns at most one reading -- a
    mandrel can only present the normals that lie on its own circle."""
    n = np.asarray(normal, float)
    nn = np.linalg.norm(n)
    if nn < 1e-12:
        raise ValueError("surface normal must be non-zero")
    n = n / nn
    js = tuple(rotaries)
    # THE NORMAL ARRIVES IN THE COLLECTOR'S OWN FRAME and the joint axes are in
    # the frame it is bolted into, so the mount is applied before anything is
    # solved -- the same rotation `rotary_transform` composes, from the same
    # function, so the readings this returns and the placement those readings
    # produce cannot describe two different mountings. Identity for a mandrel
    # and for any pair whose collector-carrying joint is already X.
    n = collector_mount(js) @ n
    if not js:
        raise ValueError("a five-axis solve needs a rotary joint")
    if len(js) > 2:
        raise ValueError("at most two rotary joints, got %d" % len(js))
    if len(js) == 1:
        a0 = js[0].direction()
        # the single circle a mandrel can present: same component along the axis
        if abs(float(np.dot(n, a0)) - float(np.dot(NOZZLE_UP, a0))) > 1e-9:
            return []
        return [(_signed_angle(n, NOZZLE_UP, a0),)]
    # The same closed form on plain floats -- it runs at every point of every
    # five-axis layer, where numpy's per-call cost on 3-vectors dominated it.
    a1 = tuple(float(c) for c in js[0].direction())   # inner -- carries it
    a0 = tuple(float(c) for c in js[1].direction())   # base -- on the machine
    nv = tuple(float(c) for c in n)
    d = a0[0] * a1[0] + a0[1] * a1[1] + a0[2] * a1[2]
    if abs(abs(d) - 1.0) < 1e-12:
        raise Unreachable("the two rotary axes are parallel (%s and %s); that is "
                          "one joint drawn twice, not a wrist"
                          % (js[0].word, js[1].word))
    up = tuple(float(c) for c in NOZZLE_UP)
    c0 = up[0] * a0[0] + up[1] * a0[1] + up[2] * a0[2]
    c1 = nv[0] * a1[0] + nv[1] * a1[1] + nv[2] * a1[2]
    den = 1.0 - d * d
    alpha = (c0 - c1 * d) / den
    beta = (c1 - c0 * d) / den
    # |alpha a0 + beta a1 + g (a0 x a1)| = 1, with |a0 x a1|^2 = den
    g2 = (1.0 - alpha * alpha - beta * beta - 2.0 * alpha * beta * d) / den
    if g2 < -1e-12:
        return []                             # the tilt cone does not reach it
    g = math.sqrt(max(g2, 0.0))
    cross = (a0[1] * a1[2] - a0[2] * a1[1],
             a0[2] * a1[0] - a0[0] * a1[2],
             a0[0] * a1[1] - a0[1] * a1[0])
    out = []
    for s in ((1.0, -1.0) if g > 1e-12 else (0.0,)):
        m = [alpha * a0[k] + beta * a1[k] + (s * g) * cross[k]
             for k in range(3)]
        mn = math.sqrt(m[0] * m[0] + m[1] * m[1] + m[2] * m[2])
        m = (m[0] / mn, m[1] / mn, m[2] / mn)
        out.append((_signed_angle(nv, m, a1), _signed_angle(m, up, a0)))
    return out


def solve_rotaries(rotaries, normal):
    """The reading tuple to command for `normal`, collector-first, degrees.

    Of the two solutions `rotary_solutions` returns, the one with the smaller
    BASE reading is chosen, ties broken on the inner one. On a trunnion the base
    joint is the tilt, and the smaller tilt is both the shorter move and the one
    further from the cradle's travel limits. The rule is stated rather than
    implied because a five-axis post processor's solution choice is normally a
    continuity problem across a whole path, and that belongs to whatever emits
    the path -- `rotary_solutions` stays public so it can be done there without
    reopening this function.

    **THE RETURNED READING IS IN (-180, 180] AND A SPIN JOINT'S IS NOT.** This
    function sees one orientation and has no path to be continuous along, so a
    principal-range answer is the only one it can give. A collector spin word
    does NOT wrap: Terranova 2024's `V` runs monotonically past -27000 degrees
    in one file and past +42000 in another, dozens of turns, the same way our
    own `A` word accumulates on a mandrel (`mew_frame.CylinderFrame.unroll`
    keeps `v` unwrapped on purpose). So a caller marching a path must ADD the
    turns it has already made -- and must never compare two readings modulo 360
    -- or the collector silently unwinds mid-print."""
    sols = rotary_solutions(rotaries, normal)
    if not sols:
        raise Unreachable("no rotary reading brings the normal %s under the "
                          "nozzle on this pair" % (np.round(normal, 6).tolist(),))
    return min(sols, key=lambda a: (abs(a[-1]), abs(a[0])))


def five_axis_command(profile, point, normal, mount_axial=None):
    """(xyz, readings) that put the nozzle over the collector-frame `point` with
    the surface `normal` there pointing straight up at it.

    `xyz` is in EXPORTED g-code coordinates, i.e. what `place_scene` takes as
    `head`. It is the rotated point plus the collector seat, and that is not a
    new convention: it is what makes `place_scene`'s standing invariant -- the
    tip is one standoff above the active print point, on every rig -- come out
    true, and substituting the shipped mandrel reproduces its `(u, 0, h)` exactly.

    `mount_axial` is the COLLECTOR's own axial coordinate at its bottom edge
    (`mew_frame.DepositionFrame.mount_axial`), and it is handed in rather than
    derived because the frame owns that number and this module must not invent
    one. It reaches the maths only through `rotary_transform`, which is also
    where the picked g-code zero is subtracted; both are inert on a profile with
    neither a seat nor a home picked.

    `readings` are the WORDS to command, not the raw geometric angles: a joint
    whose `zero_deg` is not 0 reads something else where it is geometrically
    straight, and handing back the angle would be a number no machine takes.
    `place_scene` converts back, so the two stay a round trip.

    An axis no body drives cannot carry its component, so a point that needs one
    raises instead of being commanded into a coordinate the machine does not
    have."""
    if not profile.rotaries:
        raise Unreachable("this profile has no rotary collector")
    angles = solve_rotaries(profile.rotaries, normal)
    readings = tuple(j.word_from_angle(a)
                     for j, a in zip(profile.rotaries, angles))
    r, t = rotary_transform(profile.rotaries, angles, mount_axial)
    p = r @ np.asarray(point, float).reshape(3) + t
    seat = (profile.rotaries[0].seat() if len(profile.rotaries) == 1
            else np.zeros(3))
    g = p + seat
    for i, ax in enumerate(AXES):
        if profile.axis_side(ax) is None and abs(g[i]) > 1e-9:
            raise Unreachable("no body drives %s, but the point needs %.4f mm "
                              "there" % (ax, g[i]))
    return g, readings


def _rotary_angles(rotaries, rotary_deg):
    """Per-joint readings, collector-first, from a scalar or a sequence.

    A scalar is the four-axis call every existing caller makes and lands on the
    collector-carrying joint; a short sequence pads with zeros. `x or 0.0` keeps
    a None reading as 0.0, which is what `place_scene` has always done."""
    if rotary_deg is None:
        vals = ()
    elif np.ndim(rotary_deg) == 0:
        vals = (rotary_deg,)
    else:
        vals = tuple(rotary_deg)
    return tuple(float(vals[i] or 0.0) if i < len(vals) else 0.0
                 for i in range(len(rotaries)))


class MachineProfile:
    """A printer's parts + kinematic chain + nozzle geometry, from a machine.json.

    Schema (all fields optional except at least one part or a static body)::

        {
          "name": "MEWron 0.2",
          "kinematics": "mewron",   # mewron | xyz_head | xy_bed_z_head | custom
          "parts": {"head": PRINTHEAD_STL, "gantry": ..., "bed": ...,
                    "frame": ...},                 # one STL per role, any subset
          "axes":    {"head": ["x"], "gantry": ["y"], "bed": ["z"]},  # custom only
          "parents": {"head": "gantry", "gantry": "frame", "bed": "frame"},
          "tip": [x, y, z],         # in the head STL's frame; omit -> auto-detect
          "standoff": 1.0,          # mm the nozzle hovers above the layer
          "scale": 1.0,             # uniform STL scale (mm assumed)
          "frame": "machine",       # viewer frame: "machine" = A; "print" = B
          "collector_normal": "z",  # axis the collector keeps contact along
          "part_offsets": {"bed": [0, 0, 0]},       # constant per-part nudge, mm
          "limits": {"head": {"x": [0, 150]}},      # 3D-sim render clamp only
          "rotary": {"word": "A", "role": "bed", "radius": 1.5, "length": 30.0},
          "rotaries": [{...}, {...}],   # a five-axis PAIR, collector-first;
                                        # supersedes "rotary" when present
          "slide_offset": [0, 0],   # collector XY from the bed-top bottom-left
          "hidden_parts": ["frame"] # NOT drawn; a hidden head -> nozzle frustum
        }

    Legacy form (still loads): `parts:{x,y,z}` + `static_stl`, no `kinematics` ->
    the MEWron chain (x=head, y=gantry, z=bed). Paths resolve against base_dir."""

    def __init__(self, *, parts, static=None, tip=None, standoff=1.0, scale=1.0,
                 frame="machine", part_offsets=None, collector_axis="z",
                 name="machine", base_dir=".",
                 kinematics="mewron", axes=None, parents=None,
                 collector_normal="z", limits=None, slide_offset=None,
                 bed_top_z=None, bed_corner=None, hidden_parts=None,
                 rotary=None, rotaries=None):
        # STL paths by moving role (head/gantry/bed); the static body is `static`.
        self.parts = {r: p for r, p in (parts or {}).items() if p and r in ROLES}
        self.static = static or None
        self.tip = None if tip is None else np.asarray(tip, float)
        self.standoff = float(standoff)
        if not math.isfinite(self.standoff):
            # a NaN standoff loaded and put every placed body at NaN
            raise ValueError("standoff must be a finite number, got %r"
                             % standoff)
        self.scale = float(scale)
        # viewer frame convention (A: bed carries Z; B: print world-fixed). NOTE
        # this `frame` is the render mode -- distinct from the `frame` ROLE (the
        # static body, stored in `self.static`).
        self.frame = "print" if str(frame).lower() == "print" else "machine"
        self.part_offsets = {k: np.asarray(v, float)
                             for k, v in (part_offsets or {}).items()}
        # runtime deposition-frame shift (the player sets this from the loaded STL
        # bounds via contact_alignment): on a rig whose contact axis is driven by
        # the nozzle assembly the bed is FIXED, so the whole DEPOSITION FRAME -- the
        # printed scaffold + collector-plate group -- drops with the descending tool
        # onto the bed, rather than the bed rising to the tool. Zero for a bed-driven
        # contact axis (MEWron), so place_scene's bed_group is unchanged there.
        self.deposition_offset = np.zeros(3)
        self.collector_axis = collector_axis            # vestigial; kept for compat
        # the axis the collector keeps contact with the bed along (the bed's surface
        # normal). All real beds are horizontal -> "z". Used by the bounds-offset
        # distribution so the bed tracks the build in its contact direction.
        self.collector_normal = (collector_normal if collector_normal in _AX
                                 else "z")
        # Per-part MOTION LIMITS (3D-sim only): {role: {axis: (lo, hi)}}, the travel
        # range each part may translate over, in the commanded (machine) frame. They
        # clamp the rendered part BODY so a body is never drawn off its rails; the
        # nozzle tip + the build group stay on the unclamped kinematics, so an in-range
        # print (the norm) is unaffected. Empty -> no clamp anywhere.
        self.limits = {}
        for role, axmap in (limits or {}).items():
            if role not in ROLES:
                continue
            rl = {}
            for ax, span in (axmap or {}).items():
                if ax in _AX and span is not None and span[0] is not None \
                        and span[1] is not None:
                    lo, hi = float(span[0]), float(span[1])
                    rl[ax] = (min(lo, hi), max(lo, hi))
            if rl:
                self.limits[role] = rl
        # (dx, dy) the collector/print slide sits at, measured from the bottom-left
        # corner of the bed STL's top face (when a bed STL is loaded) or from the world
        # origin (no bed). The PLAYER turns this into the build `registration` it feeds
        # place_scene -- this module only stores the datum the player reads. Sim-only:
        # the exported g-code is invariant to it (origin = collector). Default (0,0).
        self.slide_offset = (np.zeros(2) if slide_offset is None
                             else np.asarray(slide_offset, float)[:2])
        # MANUAL bed datum (bed STL frame): the top-face Z and bottom-left corner the
        # player uses INSTEAD of find_bed_top_face when set. The escape hatch for
        # assembly STLs where auto-detect can't isolate the print bed from the
        # surrounding hardware (a real bed-slinger). None -> auto-detect.
        self.bed_top_z = None if bed_top_z is None else float(bed_top_z)
        self.bed_corner = (None if bed_corner is None
                           else np.asarray(bed_corner, float)[:2])
        # per-part VISIBILITY (3D-sim only): the bodies the player should NOT draw.
        # A subset of {head, gantry, bed, frame} ("frame" is the static body). The
        # player swaps a hidden head for the nozzle frustum; the rest are just not
        # drawn. Pure render state -- placement + the exported g-code are untouched.
        self.hidden_parts = {r for r in (hidden_parts or ())
                             if r in ROLES or r == "frame"}
        # The REVOLUTE joints carrying the collector, COLLECTOR-FIRST: index 0 is
        # the one the collector is bolted to, index 1 (a five-axis pair) the one
        # carrying that. Empty for the flat collector every other rig here has,
        # and empty is the untouched historical path: `place`/`place_scene` never
        # consult it and the collector keys they return only exist when it is not.
        # `rotary` is the single-joint view of the same tuple and is what every
        # existing reader and writer uses; it is a property, below.
        if rotaries is None:
            self.rotaries = () if rotary is None else (rotary,)
        else:
            self.rotaries = tuple(j for j in rotaries if j is not None)
        if len(self.rotaries) > 2:
            raise ValueError("at most two rotary joints, got %d"
                             % len(self.rotaries))
        # A PICKED PIVOT IS IN MILLIMETRES, so a scaled profile is refused
        # rather than silently misplaced. The pivot travels down two chains that
        # scale differently: the BODY is drawn in STL coordinates and takes
        # `scale`, while the collector chain (`rotary_transform`) is the same
        # arithmetic `scaffold_gcode` wrote the file with and
        # `gcode_to_fibers.FiveAxisReader` reads it back with, neither of which
        # has a `scale` to apply. One number cannot be right in both unless the
        # two agree, and they agree exactly when `scale` is 1. Every STL this
        # program has ever loaded is in mm, so nothing shipped is affected --
        # and a rig that genuinely is not can rescale its files, which is a
        # thing a user can do, unlike noticing a trunnion offset that is out by
        # a factor nothing on screen names.
        if float(self.scale) != 1.0 and any(j.pivot_set for j in self.rotaries):
            raise ValueError(
                "a rotation centre is a point in millimetres, so a profile "
                "with scale %g cannot carry one: the collector kinematics "
                "(shared with the generator and the parser) have no scale to "
                "apply to it. Export the body STLs in mm and set scale to 1."
                % self.scale)
        self.name = name
        self.base_dir = base_dir

        # resolve the kinematic chain: preset (from CHAINS) or custom (from the
        # given axes/parents, defaulting any unspecified role to the MEWron chain).
        self.kinematics = kinematics or "mewron"
        base = CHAINS.get(self.kinematics, CHAINS["mewron"])
        self.axes = {r: tuple(a) for r, a in base["axes"].items()}
        self.parents = dict(base["parents"])
        for r, a in (axes or {}).items():
            if r in ROLES:
                self.axes[r] = tuple(x for x in a if x in _AX)
        for r, p in (parents or {}).items():
            if r in ROLES:
                self.parents[r] = p
        # tool chain = the nozzle's ancestors (incl. head); collector chain = the
        # bed's. Precomputed once -- they fix each part's +g / -g sign.
        self._tool_set = set(self._chain("head"))
        self._coll_set = set(self._chain("bed"))

    # -- construction ------------------------------------------------------- #
    @classmethod
    def from_dict(cls, d, base_dir="."):
        raw = d.get("parts", {}) or {}
        new_style = bool(d.get("kinematics")) or any(
            k in raw for k in ("head", "gantry", "bed", "frame"))
        if new_style:
            parts = {r: raw.get(r) for r in ROLES if raw.get(r)}
            static = d.get("static_stl") or d.get("static") or raw.get("frame")
        else:
            # legacy axis-keyed form -> MEWron roles (x=head, y=gantry, z=bed)
            parts = {}
            for ax, role in (("x", "head"), ("y", "gantry"), ("z", "bed")):
                if raw.get(ax):
                    parts[role] = raw[ax]
            static = d.get("static_stl") or d.get("static")
        kin = d.get("kinematics", "mewron")
        # explicit axes/parents are honoured only for a non-preset (custom) chain;
        # a known preset's chain always comes from CHAINS.
        custom = kin not in CHAINS
        return cls(parts=parts,
                   static=static,
                   tip=d.get("tip"),
                   standoff=d.get("standoff", 1.0),
                   scale=d.get("scale", 1.0),
                   frame=d.get("frame", "machine"),
                   part_offsets=d.get("part_offsets"),
                   collector_axis=d.get("collector_axis", "z"),
                   collector_normal=d.get("collector_normal", "z"),
                   limits=d.get("limits"),
                   slide_offset=d.get("slide_offset"),
                   bed_top_z=d.get("bed_top_z"),
                   bed_corner=d.get("bed_corner"),
                   hidden_parts=d.get("hidden_parts"),
                   rotaries=_rotaries_from_dict(d),
                   name=d.get("name", "machine"),
                   base_dir=base_dir,
                   kinematics=kin,
                   axes=d.get("axes") if custom else None,
                   parents=d.get("parents") if custom else None)

    @classmethod
    def load(cls, path):
        """Read a machine.json. A file of the wrong SHAPE -- empty, not an
        object, a list where an object belongs, a string where a number does
        -- raises ValueError naming the file, not the bare AttributeError /
        TypeError it used to die in, which named neither the file nor the key.
        (A machine.json can be hand-written: `frame` above is still read from
        one.)"""
        try:
            with open(path, "r") as fh:
                d = json.load(fh)
            if not isinstance(d, dict):
                raise ValueError("the top level must be a JSON object {...}")
            return cls.from_dict(
                d, base_dir=os.path.dirname(os.path.abspath(path)))
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise ValueError("machine profile %s: %s" % (path, exc)) from exc

    def is_active(self):
        """True if the profile actually names geometry to render."""
        return bool(self.parts) or bool(self.static)

    @property
    def rotary(self):
        """The joint the collector is bolted to, or None. The four-axis view of
        `rotaries`, and identical to it on every shipped rig."""
        return self.rotaries[0] if self.rotaries else None

    @rotary.setter
    def rotary(self, joint):
        """Setting a joint replaces the COLLECTOR-CARRYING one and leaves any
        joint under it alone; setting None removes the collector entirely, which
        is what the player means when it hands a rotary rig a flat job."""
        self.rotaries = (() if joint is None
                         else (joint,) + tuple(self.rotaries[1:]))

    @property
    def rotary_count(self):
        """How many revolute joints carry the collector: 0, 1 or 2."""
        return len(self.rotaries)

    def is_rotary(self):
        """True if the collector is carried by any revolute joint."""
        return bool(self.rotaries)

    def is_five_axis(self):
        """True if the collector is carried by an ordered PAIR of them."""
        return len(self.rotaries) == 2

    # -- the kinematic chain ------------------------------------------------ #
    def _chain(self, role):
        """The moving roles from `role` up to (not including) the static root:
        [role, parent, grandparent, ...]. Stops at `frame`/None/unknown. Cycle-safe."""
        out, r, seen = [], role, set()
        while r in ROLES and r not in seen:
            seen.add(r)
            out.append(r)
            r = self.parents.get(r)
        return out

    def _sign(self, role):
        """+1 if the role is on the tool chain, -1 on the collector chain, else 0.
        This is the Frame-A convention that makes the nozzle track the print."""
        if role in self._tool_set:
            return 1.0
        if role in self._coll_set:
            return -1.0
        return 0.0

    def axis_side(self, axis):
        """Which kinematic side drives translation along `axis`: 'tool' if a body on
        the nozzle (tool) chain drives it, 'collector' if a body on the bed chain
        does, else None (no body moves on that axis). Used to decide which assembly
        closes the nozzle<->bed standoff gap -- see contact_alignment."""
        if axis not in _AX:
            return None
        if any(axis in self.axes.get(r, ()) for r in self._chain("head")):
            return "tool"
        if any(axis in self.axes.get(r, ()) for r in self._chain("bed")):
            return "collector"
        return None

    def _descendants(self, role):
        """The moving roles that RIDE `role` (its children in the chain) -- a part the
        clamp moves must carry these so the assembly stays rigid. E.g. on the MEWron
        the head rides the gantry, so descendants('gantry') == ['head']."""
        return [r for r in ROLES if r != role and role in self._chain(r)]

    def orphaned_axes(self):
        """{role: [axes]} for any role that DRIVES an axis yet sits on neither the tool
        (head) chain nor the collector (bed) chain -- so its commanded motion reaches
        no leaf and that axis silently VANISHES from the rendered scene (a body drives
        Y, but nothing on the nozzle or the print moves in Y). Only reachable with a
        Custom kinematic parenting where a driving body -- typically the gantry -- is
        parented to `frame` while neither head nor bed rides it. Empty for every stock
        profile (MEWron / Prusa) and any well-formed custom. A pure query: it never
        raises, so a hand-written machine.json still LOADS; the GUI calls it to warn /
        block before a scene is built from an unrenderable configuration."""
        tool = set(self._chain("head"))
        coll = set(self._chain("bed"))
        out = {}
        for r in ROLES:
            driven = [a for a in self.axes.get(r, ()) if a in _AX]
            if driven and r not in tool and r not in coll:
                out[r] = driven
        return out

    def contact_alignment(self, gap):
        """How to close the nozzle<->bed standoff `gap`, along the contact-normal axis.

        `gap` is the signed distance (the player measures it from the loaded STL
        bounds) the BED would have to RISE so its top sits one standoff below the
        nozzle tip. WHO actually moves depends on which side drives that axis:

          * the COLLECTOR (bed) chain drives it -- or nothing does -> the bed rises
            by `gap` to the Z-fixed nozzle (the MEWron: its bed STL is parked low).
            The deposition frame is untouched. Byte-for-byte the prior behaviour.
          * the TOOL (nozzle) chain drives it -> the bed is FIXED on that axis, so it
            must NOT move; instead the whole nozzle assembly (every body on the tool
            chain) AND the deposition frame (printed scaffold + plate group) drop by
            `gap` onto the bed. Dropping the print with the tool keeps the nozzle one
            standoff above it; dropping the entire tool chain keeps the printhead +
            gantry rigid and shows the gantry descending its Z rails -- the real
            machine's motion -- instead of the plate floating up to the printhead.

        Returns (part_offsets, deposition_offset): a {role: np.array(3)} of own nudges
        to ADD into `part_offsets`, plus the np.array(3) to assign to
        `deposition_offset`. Pure kinematics -- the caller supplies `gap`."""
        i = _AX[self.collector_normal]
        if self.axis_side(self.collector_normal) == "tool":
            drop = np.zeros(3)
            drop[i] = -gap
            parts = {r: drop.copy() for r in self._chain("head")}
            return parts, drop.copy()
        rise = np.zeros(3)
        rise[i] = gap
        return {"bed": rise}, np.zeros(3)

    def _chain_driven(self, role, g):
        """Cumulative driven translation at `role`: the signed sum, over `role` and
        its ancestors, of g along each axis they drive. `g` is an (x,y,z) array."""
        total = np.zeros(3)
        for r in self._chain(role):
            sgn = self._sign(r)
            for a in self.axes.get(r, ()):
                total[_AX[a]] += sgn * g[_AX[a]]
        return total

    def _distribute(self, chain, v):
        """Spread vector `v` across a chain (leaf-first) so the cumulative offset at
        the leaf equals `v` while the parts stay assembled: each axis is carried by
        the lowest part (nearest the leaf) that drives it; an axis no part drives is
        mounted rigidly on the chain root. Returns {role: own-offset}."""
        offs = {r: np.zeros(3) for r in chain}
        if not chain:
            return offs
        for i, a in enumerate(AXES):
            carrier = next((r for r in chain if a in self.axes.get(r, ())), None)
            if carrier is None:
                carrier = chain[-1]          # root mount for an undriven axis
            offs[carrier][i] += v[i]
        return offs

    @staticmethod
    def _cumulative(chain, offs):
        """Per-part rendered offset = sum of own-offsets from the part up to the
        root (a child inherits its parent's position). `chain` is leaf-first."""
        rendered, running = {}, np.zeros(3)
        for r in reversed(chain):            # root-first
            running = running + offs[r]
            rendered[r] = running.copy()
        return rendered

    # -- path resolution ---------------------------------------------------- #
    def _resolve(self, name):
        """Absolute path to one part's STL, or None when the profile names none.

        A relative name joins `base_dir` as always; if that misses, the BUNDLE is
        tried. That fallback is what lets a SHIPPED preset name a part at all --
        base_dir is machine.json's directory, i.e. the throwaway per-PID
        SESSION_DIR, so no path relative to it survives a launch. Reached only
        when the primary path is absent, so a profile whose files ARE present
        resolves byte for byte as before. Last, the folder beside the app is
        tried: an optional download such as `machine_models/` is unzipped next
        to MEWracle.exe, outside the bundle. A part found nowhere resolves to the
        missing `base_dir` path, and the player skips it. A part that moved
        (RENAMED_PARTS) is found under its new name."""
        return resolve_part(name, self.base_dir)

    @property
    def head_path(self):
        return self._resolve(self.parts.get("head"))

    @property
    def gantry_path(self):
        return self._resolve(self.parts.get("gantry"))

    @property
    def bed_path(self):
        return self._resolve(self.parts.get("bed"))

    @property
    def static_path(self):
        return self._resolve(self.static)

    @property
    def rotary_paths(self):
        """ONE resolved STL path per rotary joint, collector-first -- `None` in the
        slot of a joint that names no body. `()` without a rotary collector.

        A mandrel names at most one: the chuck/stage that grips the rod (the rod
        itself is not a part, it is drawn from the job's radius). A five-axis
        table names two REAL BODIES, the spinning table and the tilt cradle
        under it, and each of them turns about its own line -- so resolving only
        the first, which is what this property used to do, drew half a machine.

        Indexed rather than named because the joints are already an ordered
        tuple and a second naming scheme beside it is a second thing to keep in
        register."""
        return tuple(self._resolve(j.stl) if j.stl else None
                     for j in self.rotaries)

    @property
    def collector_datum(self):
        """Joint 0's picked AXIS POINT, in that joint's STL frame -- or `None`
        when nothing has been picked.

        SUPERSEDED AS THE COLLECTOR'S SEAT by `collector_origin`, and kept because it
        is still the answer to its own question (where the
        spin axis is). What it was wrong about is below: a point on the axis of
        ROTATION is not the point the collector STANDS on, and using it as both
        left a dome's rim hanging `mount_axial` above the table -- 3.624 mm on
        an R=10 dome spanning 0-12 mm, more on a bigger one.

        One pick, two facts, which is why there is no separate collector-height
        field anywhere: it is a point on the collector-carrying joint's axis AND
        the face the collector is bolted to. It plays exactly the part
        `bed_top_z` + `bed_corner` play for a mandrel (the axis in the holder's
        own frame), so the player seats a table the same way it seats a rod
        rather than growing a second seating path.

        `None` is the UNCALIBRATED case and it has to stay distinguishable from
        a pick that landed on the origin -- a believable number in this slot is
        what `rotary.axis_height`'s deletion was about. Hence `pivot_set`, not
        `any(pivot)`."""
        if not self.rotaries or not self.rotaries[0].pivot_set:
            return None
        return self.rotaries[0].pivot.copy()

    def collector_origin(self, mount_axial=None):
        """WHERE THE COLLECTOR'S OWN ORIGIN SITS on the machine, in joint 0's
        STL frame -- or `None` when nothing has been picked.

        The module-level `collector_origin` with this profile's joints, plus the
        uncalibrated `None` that `collector_datum` returns. Two picks feed it and
        which of them applies is stated there:

          * `seat` picked -- the collector STANDS on that point.
          * seat unpicked -- it stands on joint 0's pivot instead.

        Either way the origin is that datum walked back up joint 0's axis by
        `mount_axial`, so a dome's rim lands on the table rather than its centre
        of curvature landing there. Which datum is the only thing the pick
        chooses.

        NOTE THE RETURN TYPE DIFFERS FROM THE MODULE-LEVEL FUNCTION'S. This one
        answers `None` on a joint with NEITHER pick, because a profile that has
        calibrated nothing has no answer to give a caller asking about the
        machine; the module-level function always returns a vector, because its
        callers are the kinematics and they need a frame to work in. A caller
        that wants the geometry wants the module-level one.

        `mount_axial` is the FRAME's number and is never invented here; see the
        module-level function."""
        if not self.rotaries:
            return None
        j = self.rotaries[0]
        if not (j.pivot_set or j.seat_set):
            return None
        return collector_origin(self.rotaries, mount_axial)

    @property
    def collector_home(self):
        """WHAT G-CODE (0, 0, 0) NAMES: the DEPOSITION point the file zeroes on,
        in the collector-carrying joint's own STL frame -- or `None` when
        nothing has been picked, which is every shipped profile.

        NOT the raw pick, and the difference is one standoff. The operator jogs
        the NOZZLE to a spot and datums there, hovering one standoff (3.5 mm)
        up, so `RotaryAxis.home` is a tip position;
        every other datum in this program is a surface, and `place_scene` holds
        the tip one standoff above whatever the file commands. `home_standoff`
        is the working distance the pick was made at and
        `collector_home_offset` is where it comes off -- this property reads
        through the same function so the scene and the exported file cannot
        disagree about where the zero is. `raw_collector_home` is the pick
        itself, for anything that wants to show the operator their own number.

        It replaces the derivation from `collector_datum` rather than adding a
        term to it: a picked home is an observation, and deriving from it would
        put a measured number and a modelled one in series.

        It reads off `rotaries[0]` for the same reason `collector_datum` does --
        that is the joint the collector is bolted to. `home_set`, not
        `any(home)`: a home picked at the origin is a real calibration."""
        if not self.rotaries or not self.rotaries[0].home_set:
            return None
        # through `collector_home_offset` and back, so the standoff subtraction
        # lives in exactly one place. `mount_axial` cancels: it enters the
        # offset only via `collector_origin`, which is added straight back.
        return (collector_home_offset(self.rotaries)
                + collector_origin(self.rotaries))

    @property
    def raw_collector_home(self):
        """The g-code-home pick VERBATIM -- a nozzle TIP position, one
        `home_standoff` above what `collector_home` reports. For read-outs and
        pickers that must show the operator the number they set; never for
        placement, which is `collector_home`'s job."""
        if not self.rotaries or not self.rotaries[0].home_set:
            return None
        return self.rotaries[0].home.copy()


def _rotaries_from_dict(d):
    """The joint tuple from a machine.json, collector-first.

    `"rotaries"` (a list) wins when present; otherwise the historical
    `"rotary"` (one object) is read exactly as it always was, so no shipped
    profile changes meaning. An empty or absent both -> ()."""
    lst = d.get("rotaries")
    if lst:
        return tuple(j for j in (RotaryAxis.from_dict(x) for x in lst)
                     if j is not None)
    one = RotaryAxis.from_dict(d.get("rotary"))
    return () if one is None else (one,)


def find_tip(points, tol=1e-3):
    """Auto-detect the nozzle tip in the head STL's own frame: the centroid of the
    lowest ring of points (the syringe nozzle). `points` is an (N, 3) array. This
    is the same heuristic the physics build uses, so any printhead model works
    without an explicit tip in the profile. An empty cloud (a printhead STL VTK
    could not parse) returns the origin rather than raising -- the same defensive
    contract as find_bed_top_face, so a caller never crashes on a degenerate mesh."""
    pts = np.asarray(points, float)
    if pts.size == 0:
        return np.zeros(3)
    zmin = float(pts[:, 2].min())
    low = pts[pts[:, 2] <= zmin + tol]
    return low.mean(axis=0)


def find_bed_top_face(points, tol=0.5, extent_frac=0.2):
    """Find the bed's printing surface in the bed STL's own frame: return
    `(top_z, corner_x, corner_y)` -- the Z of the top face and the (min X, min Y)
    bottom-left corner of THAT face.

    The bed is the HIGHEST horizontal face that is large in BOTH XY directions.
    Scanning Z from the very top downward in tol-thick bands, a band qualifies when its
    SMALLER XY extent is at least `extent_frac` of the model's smaller overall extent,
    and the print surface is the highest band that qualifies. Requiring width in BOTH
    directions (not total area) is what makes it robust:

      * a small raised nub/clip above the plate is tiny in both -> skipped (so the top
        is NOT plain `max(z)`, which a nub would win);
      * a thin wide arm/rail is small in ONE direction -> skipped, even though its area
        is large;
      * a wider base/underside BELOW the plate qualifies too, but the plate is higher,
        so the top-down scan returns the plate first -- it never falls THROUGH to the
        underside (the "slide phases through the bed" bug), even when the base is the
        biggest face in the model.

    Using XY EXTENT (not vertex count) keeps it independent of tessellation. For an
    unusual bed where auto-detect still misses, the user sets the bed-top Z by hand
    (the machine profile's bed nudge / a future calibration). Falls back to the bbox
    top corner on degenerate input.

    `points` is an (N, 3) array (the bed STL vertices, in the STL's own units)."""
    pts = np.asarray(points, float)
    if pts.size == 0:
        return 0.0, 0.0, 0.0
    x, y, z = pts[:, 0], pts[:, 1], pts[:, 2]
    zmax, zmin = float(z.max()), float(z.min())
    # scale the band thickness to the model so it works whatever the units/size: a
    # band must be thick enough to gather a face's vertices, thin enough to separate
    # the print surface from a nub a few mm above it.
    tol = max(tol, 0.01 * (zmax - zmin))
    need = extent_frac * min(x.max() - x.min(), y.max() - y.min())
    top = zmax
    while top >= zmin - tol:
        m = (z <= top + 1e-9) & (z >= top - tol)
        if np.count_nonzero(m) >= 3:
            ext = min(x[m].max() - x[m].min(), y[m].max() - y[m].min())
            if ext >= need:
                top_z = float(z[m].max())
                face = pts[np.abs(z - top_z) <= tol]
                return top_z, float(face[:, 0].min()), float(face[:, 1].min())
        top -= tol
    return zmax, float(x.min()), float(y.min())


def place(profile, tip, gx, gy, gz):
    """Per-frame translation for each machine part + the bed-group Z offset.

    `tip` is the nozzle-tip offset in the head STL's own frame (auto-detected via
    find_tip or taken from the profile). (gx, gy, gz) is the commanded point in
    g-code coordinates. Returns a dict of np.array(3) translations to feed straight
    into each actor's SetPosition:

        head / gantry / bed / static : the machine-body parts (by role)
        bed_offset                   : the Z/XY shift for the g-code-frame content
                                       resting on the collector (deposited mesh,
                                       collector plate, toolpath lines, origin marker)
        tip_world                    : where the nozzle tip lands (for the jet, HUD)

    Frame A (default): the tool chain carries +g on the axes it drives, the
    collector chain carries -g; the nozzle tip ends up exactly over the active print
    point, one standoff above. Frame B (`frame:"print"`): the print is world-fixed
    (bed_offset = 0) and the tool carries the FULL commanded motion -- a viewer
    convention retained for the single-marker case (place_scene always uses A)."""
    s = profile.standoff
    t = np.asarray(tip, float) * profile.scale
    # rigid whole-machine shift: parks the tip at world (0,0,s) when the commanded
    # point is g-code (0,0,0), keeping every part assembled around that datum.
    shift = np.array([-t[0], -t[1], s - t[2]], float)

    if profile.frame != "print":                    # Frame A (physically faithful)
        g = np.array([gx, gy, gz], float)
        cd = {r: profile._chain_driven(r, g) for r in ROLES}
        out = {
            "head":   shift + cd["head"],
            "gantry": shift + cd["gantry"],
            "bed":    shift + cd["bed"],
            "static": shift.copy(),
            "bed_offset": cd["bed"],                # the print rides the collector
        }
    else:                                           # Frame B (print world-fixed)
        tool = profile._chain("head")
        rendered = profile._cumulative(
            tool, profile._distribute(tool, np.array([gx, gy, gz], float)))
        out = {
            "head":   shift + rendered.get("head", np.zeros(3)),
            "gantry": shift + rendered.get("gantry", np.zeros(3)),
            "bed":    shift.copy(),
            "static": shift.copy(),
            "bed_offset": np.zeros(3),
        }
    # constant per-part fine-tuning nudges from the profile (machine-specific
    # alignment, e.g. the player's bed auto-raise), applied on top of the kinematics.
    for key, off in profile.part_offsets.items():
        if key in out:
            out[key] = out[key] + off
    # tip rides with the head (after any head nudge), so the jet/HUD stay anchored.
    out["tip_world"] = out["head"] + t
    return out


def place_scene(profile, tip, head, origin=None, registration=None,
                rotary_deg=None, rotary_start=0.0, mount_axial=None):
    """Full machine-model placement for one commanded g-code point: the viewer
    conventions layered on the raw Frame-A kinematics in `place`.

    `head` is the commanded point in EXPORTED g-code coords. `origin` is the
    exporter's g-code-origin re-base, UNDONE here so the rendered scene is the
    same whichever origin the user picked -- only the (0,0,0) triad moves.
    `registration` is the in-plane (dx, dy) seating the collector on the bed
    (bed-top bottom-left corner + slide_offset), split by which side drives each
    axis: a TOOL-driven axis moves the build AND the tool chain, a
    COLLECTOR-driven axis slides the BED under a fixed nozzle, so a bed-slinger's
    gantry is never shoved off its frame. XY only; `contact_alignment` owns Z.
    MOTION LIMITS then clamp the part BODIES over the whole driven motion
    (commanded AND registration), from the parked home and carried to
    descendants; tip and build group stay UNCLAMPED, so in-range is identical.

    `rotary_deg` is the commanded rotary word in degrees -- or, on a five-axis
    pair, a SEQUENCE of them running collector-first with `profile.rotaries`.
    `rotary_start` is where the rod's near end sits along the mandrel axis (the
    DESIGN's collector interval, so bare rod shows). Both are ignored without a
    rotary joint; the MOUNT is the base joint's `role`, and "frame" is a static
    stage the build group does not ride.

    Returns np.array(3) under `head`, `gantry`, `bed`, `static`, `tip_world` and
    `bed_group` (one SetPosition for the whole build group). A rotary profile
    adds `collector_rotations`: one (angle_deg, axis, pivot) per joint, in the
    build group's frame, to be applied IN LIST ORDER to a point in it and then
    translated by `bed_group`.

    A single joint -- every shipped rotary rig -- additionally gets the three
    keys it always had, with the values it always had: `collector_rotation` (the
    one triple), `collector_body` (centre, axis, radius, length) and
    `collector_seat` (the `-R0` already folded into `bed_group`). A PAIR gets
    none of them, deliberately: all three are mandrel facts. `collector_body`
    would draw a rod that is not on the machine, and `collector_seat` would seat
    a curved collector by a radius it does not have -- its height datum belongs
    to the surface, not to this module. A four-axis consumer reading only
    `collector_rotation` therefore sees a five-axis rig as having no collector at
    all, which is visibly wrong rather than half-right; migrating those readers
    to `collector_rotations` is the player's own slice."""
    o = np.zeros(3) if origin is None else np.asarray(origin, float)
    if registration is None:
        reg = np.zeros(3)
    else:
        r = np.asarray(registration, float)
        reg = np.array([r[0], r[1], 0.0])             # XY only -- Z is contact-aligned
    mh = np.asarray(head, float) - o                  # commanded point, machine frame
    pl = place(profile, tip, mh[0], mh[1], mh[2])

    # Seat the collector by the registration, split by WHO drives each in-plane axis:
    #   * a TOOL-driven axis (a head/gantry rail, e.g. the MEWron's X+Y, a Prusa's X)
    #     -> the nozzle assembly + the build BOTH take it, so the head rides its rail to
    #        follow the moved print (the nozzle tracks);
    #   * a COLLECTOR-driven axis (the bed's own travel, e.g. a Prusa's slinging Y)
    #     -> the BED slides UNDER the fixed nozzle by the opposite amount; the nozzle and
    #        build do NOT move. Choosing where the collector sits along the bed's travel
    #        is the same as sliding the bed beneath it -- so a bed-slinger's gantry is
    #        NEVER shoved off its frame by the seating (the bug the diagnostic exposed).
    #   * an axis no part drives -> there's nothing to move it, so it rides with the tool
    #     (a rigid registration of the whole build), as before.
    reg_tool = np.array([reg[i] if profile.axis_side(a) != "collector" else 0.0
                         for i, a in enumerate(AXES)])
    reg_coll = reg - reg_tool                         # the collector-driven remainder
    tool = profile._chain("head")
    tool_off = MachineProfile._cumulative(tool, profile._distribute(tool, reg_tool))
    coll = profile._chain("bed")
    coll_off = MachineProfile._cumulative(coll, profile._distribute(coll, reg_coll))

    # EVERY body takes its own share of both seatings, not just the leaf of each
    # chain. `_cumulative` already spreads the collector-driven seating across
    # the whole collector chain -- and if only `bed` read it back, then on a
    # rig whose bed RIDES the gantry (an Aerotech-style plus-sign stage, the
    # custom chain the five-axis rig is described with) the lower half would take
    # the seating the upper half is supposed to carry and be drawn off its own
    # rails by it. Every shipped chain has a single-body collector chain
    # (`_chain("bed") == ["bed"]`), so `coll_off` has no other entry there and
    # this is byte-identical on all of them.
    out = {
        "head":      pl["head"] + tool_off.get("head", np.zeros(3))
                     - coll_off.get("head", np.zeros(3)),
        "gantry":    pl["gantry"] + tool_off.get("gantry", np.zeros(3))
                     - coll_off.get("gantry", np.zeros(3)),
        "bed":       pl["bed"] - coll_off.get("bed", np.zeros(3)),  # slides under tool
        "static":    pl["static"],                    # the one fixed body
        "tip_world": pl["tip_world"] + reg_tool,      # tracks tool-axis seating only
    }
    # the build group (collector / scaffold / triad) rides the collector motion and the
    # TOOL-axis seating (the bed-slide handles the collector-axis seating), with the
    # origin re-base undone, plus the deposition-frame drop (non-zero only when a Z-driven
    # nozzle assembly lowers the print onto a fixed bed -- see contact_alignment).
    # WHICH BODY CARRIES THE BUILD. Normally the collector chain (`bed_offset`).
    # A rotary stage bolted to the static FRAME is the exception: it does not ride
    # the Z collector, so the build group takes no collector motion at all and the
    # tool owns the whole commanded path. `role` is the only thing that says so --
    # the translation kinematics cannot, because the mandrel is not one of ROLES.
    carrier = pl["bed_offset"]
    # the MOUNT is the BASE joint's role -- the last entry -- because that is the
    # one actually bolted to a body; on a single-joint rig it is the same object.
    mount = profile.rotaries[-1] if profile.rotaries else None
    if mount is not None and mount.role == "frame":
        carrier = np.zeros(3)
        # ...and neither does the BODY. The part in the collector slot is the
        # mandrel HOLDER on a 4-axis rig (a rig with no bed has nothing else to
        # put there), so a stage bolted to the static frame must hold both the
        # rod and the thing gripping it still. Freezing only the build group
        # left the holder sinking down the Z rails with a rod hanging in the
        # air above it. Applied here rather than in the kinematics because
        # `role` is the only thing that says so -- the mandrel is not one of
        # ROLES, and the translation chain cannot express "this body is bolted
        # elsewhere".
        out["bed"] = (pl["bed"] - coll_off.get("bed", np.zeros(3))
                      - pl["bed_offset"])
    out["bed_group"] = carrier - o + reg_tool + profile.deposition_offset

    # A mandrel collector. The wrapped tube measures from the mandrel AXIS while
    # every other datum here measures from a SURFACE, so the group is seated by
    # -R0 along the nozzle normal (see RotaryAxis.seat) and the nozzle then sits
    # one standoff above the mandrel surface exactly as it does above a plate.
    # The rotation is reported, not applied: it belongs on the group's transform,
    # before the translation, and nothing in the part kinematics can express it.
    if profile.rotaries:
        angles = _rotary_angles(profile.rotaries, rotary_deg)
        # `rotary_deg` is the commanded WORD; the joint turns by the word less
        # its zero reading, which is the word itself on every rig of ours.
        geo = [j.angle_from_word(a) for j, a in zip(profile.rotaries, angles)]
        # the COLLECTOR's chain, in the collector's own frame -- so the pivots
        # are the relative ones (`_relative_pivots`), which is what makes joint
        # 0's zero and a mandrel's the origin it always was.
        #
        # THE MOUNT IS THE FIRST LINK, when there is one. A collector is X-axial
        # in its own coordinates and the joint it is bolted to need not be, so
        # the chain a consumer applies has to start by reorienting it -- and
        # then `compose_rotations(collector_rotations)` is `rotary_transform`'s
        # own answer, term for term, rather than a second description of it.
        # `None` for a mandrel and for any pair already mounted on X, so every
        # shipped rig's list is exactly the list it always was.
        mount = collector_mount_triple(profile.rotaries)
        out["collector_rotations"] = ([] if mount is None else [mount]) + [
            (float(g), j.direction(), piv.copy())
            for j, g, piv in zip(profile.rotaries, geo,
                                 _relative_pivots(profile.rotaries,
                                                  mount_axial))]
        # ...and the SAME g-code-zero re-base `rotary_transform` applies, so a
        # consumer that composes these triples itself lands where a caller of
        # that function lands. Zeros without a picked home.
        out["collector_zero"] = -collector_home_offset(profile.rotaries,
                                                       mount_axial)
        # ...and one chain per BODY, in that body's own STL frame scaled by the
        # profile's `scale`, ready to be composed and then translated by
        # wherever the part it is bolted to has been placed.
        #
        # A BODY IS TURNED BY ITS OWN JOINT AND EVERY JOINT UNDER IT. One STL
        # per joint IS the moving body of that joint -- a tilt cradle, a
        # spinning table -- so the cradle tilts, the table on it tilts AND
        # spins, and the collector on that rides both, which is
        # `collector_rotations`. Numbering runs the same way the joints do:
        # index 0 is the collector-carrying joint and the later ones carry it.
        #
        # A JOINT WITH NO PICKED PIVOT TURNS NOTHING -- not its own body and not
        # the bodies above it. There is no line to turn about, and turning a
        # body about its STL's ORIGIN instead would fling it across the scene at
        # a plausible-looking angle: the same class of number `rotary.axis_height`
        # was deleted for. `_report_machine` names the joint instead. This is
        # also what keeps a MANDREL byte-identical: no shipped mandrel profile
        # carries a pivot, so its holder's chain is empty and the body is placed
        # by the SetPosition it always had.
        #
        # These pivots are ABSOLUTE (the picked points), not relative: a body is
        # drawn in its own file's coordinates, so the line it turns about is the
        # line that was picked in that file. The COLLECTOR's are relative,
        # because the collector is not drawn in any file.
        out["rotary_bodies"] = [
            [(float(g), j.direction(), profile.scale * j.pivot)
             for j, g in list(zip(profile.rotaries, geo))[i:] if j.pivot_set]
            for i in range(len(profile.rotaries))]
        # A PAIR IS NOT SEATED, and that is not an omission -- it is the
        # generator's own convention read back. `five_axis_command` writes
        # `g = R @ point + t` with the seat term taken ONLY when there is one
        # joint, so on a pair the exported g-code already measures from the
        # collector's own ORIGIN, which lies on the collector-carrying joint's
        # axis. Folding a seat in here would move the drawn collector off the
        # coordinates its own file is written in, by exactly a radius.
        #
        # A mandrel's `-R0` below is the other case and is this module's to
        # compute, because a rod's geometry IS the joint's. A pair does not take a
        # `collector_seat=` argument from the frame, because the two sides would
        # then disagree by a radius -- the "two copies of one law" failure, with
        # the copies in different files.
        if len(profile.rotaries) == 1:
            # the mandrel keys, unchanged. See the docstring for why a PAIR gets
            # none of them.
            joint = profile.rotaries[0]
            seat = joint.seat()
            out["bed_group"] = out["bed_group"] + seat
            out["collector_seat"] = seat
            out["collector_rotation"] = out["collector_rotations"][0]
            out["collector_body"] = joint.body(rotary_start)

    # motion-limit clamp (3D-sim only): hold each limited part body within its driven
    # travel, measured from its parked home so the contact registration is not counted
    # as travel. The clamp delta rides down to the part's descendants (a child rides
    # its parent); the tip + build group are NOT clamped, so the nozzle keeps tracking
    # the print and only a genuinely out-of-reach command separates them.
    if profile.limits:
        home0 = place(profile, tip, 0.0, 0.0, 0.0)    # parked bodies (contact applied)
        for role, axmap in profile.limits.items():
            sgn = profile._sign(role)                 # +1 tool chain, -1 collector
            if sgn == 0.0:
                continue                              # an undriven part has no travel
            for ax, (lo, hi) in axmap.items():
                i = _AX[ax]
                # the part's COMMANDED travel coordinate (machine frame, the frame the
                # user sets [lo, hi] in) = its rendered displacement times the drive
                # sign -- a collector body (e.g. a Z bed) renders -travel as it sinks.
                t = sgn * (out[role][i] - home0[role][i])
                delta = sgn * (min(max(t, lo), hi) - t)
                if delta == 0.0:
                    continue
                for r in [role] + profile._descendants(role):
                    out[r][i] = out[r][i] + delta
    return out
