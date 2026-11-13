#!/usr/bin/env python3
"""Self-running tests for machine_model.py (the multi-body printer model).

No pytest -- prints PASS/FAIL per test, exits non-zero on any failure. Covers
profile parsing, tip + bed-top detection, the Frame A / Frame B placement math,
the bed-anchored build registration, the per-part motion-limit clamp, and the
five-axis rotary PAIR -- whose comparator is a forward round trip through the
transform `place_scene` reports, never the inverse solver\'s own arithmetic."""
import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import sys

import numpy as np

import machine_model as mm
import mew_frame

_FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        _FAILS.append(name)


def near(a, b, tol=1e-9):
    return np.all(np.abs(np.asarray(a, float) - np.asarray(b, float)) <= tol)


# -- profile parsing -------------------------------------------------------- #
def test_a_malformed_machine_json_names_the_file():
    """A hand-written machine.json of the wrong shape is refused with a
    ValueError naming the FILE, and a NaN standoff is refused rather than
    loaded. Found by a release-audit campaign on
    malformed machine.json files: an
    empty file, a top-level list, `parts` as a list, `rotary` as a list,
    `standoff: null` and `tip: "0,0,0"` each died in a bare JSONDecodeError /
    AttributeError / TypeError that named neither the file nor the key, and
    `standoff: NaN` LOADED and placed every body at NaN.
    RED on cc329c7: each case below raised the bare error or loaded."""
    import json
    import os
    import tempfile
    d = tempfile.mkdtemp(prefix="mm_bad_json_")
    path = os.path.join(d, "machine.json")
    base = {"kinematics": "mewron", "standoff": 4.5, "parts": {}}
    cases = [("empty file", ""), ("top-level list", "[]"),
             ("parts is a list", json.dumps(dict(base, parts=["a.stl"]))),
             ("rotary is a list", json.dumps(dict(base, rotary=[{"word": "A"}]))),
             ("standoff null", json.dumps(dict(base, standoff=None))),
             ("tip a string", json.dumps(dict(base, tip="0,0,0"))),
             ("standoff NaN", '{"standoff": NaN, "parts": {}}')]
    for name, text in cases:
        with open(path, "w") as fh:
            fh.write(text)
        try:
            mm.MachineProfile.load(path)
            got = "loaded"
        except ValueError as exc:
            got = "named" if "machine.json" in str(exc) else "unnamed: %s" % exc
        except Exception as exc:  # noqa: BLE001 -- the defect being tested
            got = "bare %s" % type(exc).__name__
        check("malformed machine.json refused by name: " + name,
              got == "named", got)
    with open(path, "w") as fh:
        json.dump(base, fh)
    check("a good machine.json still loads",
          mm.MachineProfile.load(path).standoff == 4.5)


def test_profile_from_dict():
    d = {"name": "M", "parts": {"x": "head.stl", "y": "g.stl", "z": "bed.stl"},
         "static_stl": "frame.stl", "standoff": 2.5, "scale": 0.5,
         "tip": [1, 2, 3], "frame": "print", "part_offsets": {"bed": [0, 0, 1]}}
    p = mm.MachineProfile.from_dict(d, base_dir="/m")
    check("name parsed", p.name == "M")
    # legacy axis-keyed parts map to the role-keyed form (x=head, y=gantry, z=bed)
    check("parts parsed (legacy->roles)",
          p.parts == {"head": "head.stl", "gantry": "g.stl", "bed": "bed.stl"})
    check("legacy -> mewron kinematics", p.kinematics == "mewron")
    check("standoff parsed", p.standoff == 2.5)
    check("scale parsed", p.scale == 0.5)
    check("tip parsed", near(p.tip, [1, 2, 3]))
    check("frame parsed", p.frame == "print")
    check("offset parsed", near(p.part_offsets["bed"], [0, 0, 1]))
    check("is_active", p.is_active())


def test_profile_defaults_and_paths():
    p = mm.MachineProfile.from_dict({"parts": {"x": "head.stl"}}, base_dir="/m")
    check("default standoff", p.standoff == 1.0)
    check("default scale", p.scale == 1.0)
    check("default frame is A", p.frame == "machine")
    check("tip None -> auto", p.tip is None)
    check("head path resolved", p.head_path.replace("\\", "/") == "/m/head.stl")
    check("missing gantry -> None", p.gantry_path is None)
    check("empty profile inactive", not mm.MachineProfile.from_dict({}).is_active())


def test_absolute_path_kept():
    ap = "C:/abs/head.stl" if sys.platform.startswith("win") else "/abs/head.stl"
    p = mm.MachineProfile.from_dict({"parts": {"x": ap}}, base_dir="/m")
    check("absolute path untouched", p.head_path == ap)


def test_slide_offset_and_limits_parse():
    p = mm.MachineProfile.from_dict({"parts": {"x": "h.stl"}})
    check("default slide_offset is zero", near(p.slide_offset, [0, 0]))
    check("default limits empty", p.limits == {})
    p2 = mm.MachineProfile.from_dict(
        {"parts": {"x": "h.stl", "y": "g.stl", "z": "b.stl"},
         "slide_offset": [12.0, -4.0],
         "limits": {"head": {"x": [10, 0]},          # given hi<lo -> normalised
                    "gantry": {"y": [0, 150]},
                    "bed": {"z": [None, 5]},          # incomplete -> dropped
                    "frame": {"x": [0, 1]}}})         # not a moving role -> dropped
    check("slide_offset parsed (XY)", near(p2.slide_offset, [12.0, -4.0]))
    check("limit normalised lo<=hi", p2.limits["head"]["x"] == (0.0, 10.0))
    check("limit gantry parsed", p2.limits["gantry"]["y"] == (0.0, 150.0))
    check("incomplete limit dropped", "bed" not in p2.limits)
    check("non-role limit dropped", "frame" not in p2.limits)
    # the head rides the gantry on the MEWron chain -> a gantry clamp must carry it
    check("descendants: head rides the gantry",
          p2._descendants("gantry") == ["head"])
    check("descendants: head is a leaf", p2._descendants("head") == [])
    check("descendants: bed drives nothing else", p2._descendants("bed") == [])


def test_hidden_parts_parse():
    p = mm.MachineProfile.from_dict({"parts": {"x": "h.stl"}})
    check("default hidden_parts empty", p.hidden_parts == set())
    p2 = mm.MachineProfile.from_dict(
        {"parts": {"x": "h.stl"}, "static_stl": "f.stl",
         "hidden_parts": ["head", "frame", "bed", "bogus"]})
    # head/gantry/bed are ROLES; "frame" (the static body) is allowed too; anything
    # else is dropped so a typo can't smuggle in a non-part.
    check("hidden_parts keeps valid roles + frame",
          p2.hidden_parts == {"head", "frame", "bed"})
    check("hidden_parts drops unknown roles", "bogus" not in p2.hidden_parts)


# -- tip detection ---------------------------------------------------------- #
def test_find_tip():
    # a cone-ish cloud: a wide top ring at z=10, a single apex point at z=0
    top = np.array([[c, s, 10.0] for c, s in
                    [(1, 0), (-1, 0), (0, 1), (0, -1)]])
    apex = np.array([[0.3, -0.2, 0.0]])
    pts = np.vstack([top, apex])
    tip = mm.find_tip(pts)
    check("tip is the lowest point", near(tip, [0.3, -0.2, 0.0]))

    # a flat bottom face -> centroid of that face
    flat = np.array([[0, 0, 0], [2, 0, 0], [0, 2, 0], [2, 2, 0], [1, 1, 5]], float)
    check("flat-bottom tip = face centroid", near(mm.find_tip(flat), [1, 1, 0]))

    # an empty cloud (a printhead STL VTK could not parse) returns the origin rather
    # than raising -- the same defensive contract as find_bed_top_face, so the player's
    # auto tip detection never crashes on a degenerate mesh.
    check("empty cloud safe (no crash)", near(mm.find_tip(np.zeros((0, 3))), [0, 0, 0]))


# -- bed top-face detection (robust to raised nubs) ------------------------- #
def test_find_bed_top_face():
    # a plate top at z=10 spanning x[2,102] y[3,73], walls down to z=0, and two
    # small nubs poking up to z=13 in a corner. max(z) would pick a nub; the
    # detector must return the plate (z=10) and ITS bottom-left corner.
    plate = [[2, 3, 10], [102, 3, 10], [2, 73, 10], [102, 73, 10]]
    walls = [[2, 3, 0], [102, 3, 0], [2, 73, 0], [102, 73, 0]]
    nub1 = [[5, 5, 13], [7, 5, 13], [5, 7, 13], [7, 7, 13]]
    nub2 = [[9, 5, 13], [11, 5, 13], [9, 7, 13], [11, 7, 13]]
    pts = np.array(plate + walls + nub1 + nub2, float)
    top_z, cx, cy = mm.find_bed_top_face(pts)
    check("bed top is the plate, not the nub", near(top_z, 10.0),
          f"got top_z={top_z}")
    check("bed corner is the plate bottom-left", near([cx, cy], [2.0, 3.0]),
          f"got corner=({cx},{cy})")
    # a plain thin plate (no nubs): top = max z, corner = bbox min
    plain = np.array([[0, 0, 1], [50, 0, 1], [0, 30, 1], [50, 30, 1],
                      [0, 0, 0], [50, 0, 0], [0, 30, 0], [50, 30, 0]], float)
    tz, px, py = mm.find_bed_top_face(plain)
    check("plain plate top", near(tz, 1.0))
    check("plain plate corner", near([px, py], [0.0, 0.0]))
    # the print surface is SMALLER than the whole assembly: a wide thin base at z=-5
    # spans x[-30,130] but the heatbed (the biggest FACE) is x[0,100] y[0,60] at z=0.
    # Thresholding on the global footprint skipped the heatbed and fell through to the
    # base (the "slide phases through the bed" bug); thresholding on the biggest face
    # keeps the heatbed.
    plate = [[0, 0, 0], [100, 0, 0], [0, 60, 0], [100, 60, 0]]
    base = [[-30, -10, -5], [130, -10, -5], [-30, 70, -5], [130, 70, -5]]
    nub = [[5, 5, 2], [7, 5, 2], [5, 7, 2], [7, 7, 2]]
    pts2 = np.array(plate + base + nub, float)
    tz2, cx2, cy2 = mm.find_bed_top_face(pts2)
    check("top is the heatbed, not the wider base or the nub", near(tz2, 0.0),
          f"got top_z={tz2}")
    check("corner is the heatbed corner", near([cx2, cy2], [0.0, 0.0]),
          f"got ({cx2},{cy2})")
    # degenerate: empty cloud -> zeros, no crash
    check("empty cloud safe", mm.find_bed_top_face(np.zeros((0, 3))) == (0.0, 0.0, 0.0))


# -- placement: Frame A (default) ------------------------------------------- #
def test_place_frame_a_tip_tracking():
    p = mm.MachineProfile.from_dict({"parts": {"x": "h.stl"}})   # tip auto, standoff 1
    tip = np.array([5.0, 7.0, 3.0])     # head STL tip offset
    r = mm.place(p, tip, gx=10.0, gy=20.0, gz=4.0)
    # tip must land exactly over the commanded XY, standoff above world 0
    check("A: tip_world over commanded XY", near(r["tip_world"][:2], [10.0, 20.0]))
    check("A: tip_world Z == standoff", near(r["tip_world"][2], 1.0))
    # bed (and the print on it) sinks by the commanded layer height
    check("A: bed_offset == (0,0,-gz)", near(r["bed_offset"], [0, 0, -4.0]))


def test_place_frame_a_chain_assembled():
    p = mm.MachineProfile.from_dict({"parts": {"x": "h.stl", "y": "g.stl"}})
    tip = np.array([5.0, 7.0, 3.0])
    r = mm.place(p, tip, gx=10.0, gy=20.0, gz=4.0)
    head, gantry = r["head"], r["gantry"]
    # head rides the gantry: identical Y and Z translation, differ only by X = gx
    check("A: head/gantry share Y", near(head[1], gantry[1]))
    check("A: head/gantry share Z", near(head[2], gantry[2]))
    check("A: head leads gantry in X by gx", near(head[0] - gantry[0], 10.0))
    # gantry's X is independent of gx (it doesn't move in X)
    r2 = mm.place(p, tip, gx=99.0, gy=20.0, gz=4.0)
    check("A: gantry X fixed vs gx", near(gantry[0], r2["gantry"][0]))
    check("A: gantry Y tracks gy", near(gantry[1], 20.0 - tip[1]))


def test_place_frame_a_bed_only_z():
    p = mm.MachineProfile.from_dict({"parts": {"z": "bed.stl"}})
    tip = np.zeros(3)
    a = mm.place(p, tip, gx=10.0, gy=20.0, gz=4.0)
    b = mm.place(p, tip, gx=99.0, gy=88.0, gz=4.0)
    check("A: bed X ignores gx", near(a["bed"][0], b["bed"][0]))
    check("A: bed Y ignores gy", near(a["bed"][1], b["bed"][1]))
    c = mm.place(p, tip, gx=10.0, gy=20.0, gz=9.0)
    check("A: bed Z tracks gz", near(a["bed"][2] - c["bed"][2], 5.0))


# -- placement: Frame B ----------------------------------------------------- #
def test_place_frame_b():
    p = mm.MachineProfile.from_dict({"parts": {"x": "h.stl"}, "frame": "print"})
    tip = np.array([5.0, 7.0, 3.0])
    r = mm.place(p, tip, gx=10.0, gy=20.0, gz=4.0)
    check("B: bed_offset is zero", near(r["bed_offset"], [0, 0, 0]))
    check("B: tip rides up with layer", near(r["tip_world"][2], 1.0 + 4.0))
    check("B: tip_world over commanded XY", near(r["tip_world"][:2], [10.0, 20.0]))


# -- scale + per-part offsets ----------------------------------------------- #
def test_scale_and_offsets():
    p = mm.MachineProfile.from_dict(
        {"parts": {"x": "h.stl"}, "scale": 2.0,
         "part_offsets": {"head": [1.0, 0.0, 0.0]}})
    tip = np.array([1.0, 0.0, 0.0])
    r = mm.place(p, tip, gx=0.0, gy=0.0, gz=0.0)
    # scaled tip is 2.0 in X; head shift = -2.0; +1.0 nudge => -1.0
    check("scale applied to tip shift", near(r["head"][0], -1.0))
    # tip_world still lands at the (nudged) commanded point: head + scaled tip
    check("tip_world includes nudge", near(r["tip_world"][0], 0.0 + 1.0))


# -- full scene: origin re-base + bed-anchored registration ----------------- #
# place_scene layers three conventions over `place`: the registration seats the
# collector on the bed (build group AND tracking printhead take the in-plane
# offset, bed STL and static frame do not); the g-code origin re-base is undone
# so toggling the origin moves only the (0,0,0) triad; and motion limits clamp
# the part bodies, leaving tip and build unclamped. The cardinal rule, asserted
# every which way: the printhead is ALWAYS over the scaffold.
_SCENE_TIP = np.array([5.0, 7.0, 3.0])


def _scene_profile(bed_raise=None):
    """A 4-part profile (head/gantry/bed/static), standoff 2, optionally with the bed
    auto-raise nudge the player derives from the bed STL so its top meets the
    deposition plane."""
    p = mm.MachineProfile.from_dict(
        {"parts": {"x": "h.stl", "y": "g.stl", "z": "bed.stl"},
         "static_stl": "frame.stl", "standoff": 2.0})
    if bed_raise is not None:
        p.part_offsets["bed"] = np.array([0.0, 0.0, bed_raise])
    return p


def _scaffold_world(sc, head_exported):
    """Where a commanded scaffold point renders (rides the build group)."""
    return np.asarray(head_exported, float) + sc["bed_group"]


def test_scene_default_matches_place():
    # registration 0 + no origin re-base -> byte-for-byte the raw kinematics, so the
    # multi-body view is unchanged from the bare `place`.
    head = [10.0, 20.0, 4.0]
    sc = mm.place_scene(_scene_profile(), _SCENE_TIP, head)
    pl = mm.place(_scene_profile(), _SCENE_TIP, *head)
    for k in ("head", "gantry", "bed", "static", "tip_world"):
        check(f"default scene {k} == place", near(sc[k], pl[k]))
    check("default bed_group == place bed_offset",
          near(sc["bed_group"], pl["bed_offset"]))


def test_scene_printhead_over_scaffold_always():
    # THE cardinal invariant: the nozzle is directly over (one standoff above) the
    # scaffold point it is laying -- for every registration and origin.
    cx, cy, ptz = -40.0, 0.0, 1.0
    for o in (np.zeros(3), np.array([-cx, -cy, -ptz])):
        p = np.array([cx + 12.0, cy + 9.0, ptz])          # a point on the print
        for reg in ((0, 0), (-60, -20), (30, 15)):
            sc = mm.place_scene(_scene_profile(), _SCENE_TIP, p + o, origin=o,
                                registration=reg)
            scaf = _scaffold_world(sc, p + o)
            check(f"reg={reg}: nozzle over scaffold XY",
                  near(sc["tip_world"][:2], scaf[:2]))
            check(f"reg={reg}: nozzle one standoff above scaffold",
                  near(sc["tip_world"][2] - scaf[2], 2.0))


def test_scene_static_and_bed_fixed_under_registration():
    # The registration seats the collector on the bed: the static FRAME and the BED
    # STL stay put (the build slides onto them); everything that carries the print --
    # build group AND the tracking printhead -- moves.
    head = [10.0, 20.0, 4.0]
    a = mm.place_scene(_scene_profile(), _SCENE_TIP, head, registration=(0, 0))
    b = mm.place_scene(_scene_profile(), _SCENE_TIP, head, registration=(-60, -20))
    check("registration leaves the static frame put", near(a["static"], b["static"]))
    check("registration leaves the bed STL put", near(a["bed"], b["bed"]))
    for k in ("head", "gantry", "tip_world", "bed_group"):
        check(f"registration DOES move {k}", not near(a[k], b[k]))


def test_scene_build_group_rides_registration():
    # The whole build group -- collector + scaffold + origin triad, one SetPosition
    # (bed_group) -- shifts by exactly the (in-plane) registration delta.
    head = [10.0, 20.0, 4.0]
    a = mm.place_scene(_scene_profile(), _SCENE_TIP, head, registration=(0, 0))
    b = mm.place_scene(_scene_profile(), _SCENE_TIP, head, registration=(7, -3))
    check("build group shifts by the registration delta",
          near(b["bed_group"] - a["bed_group"], [7, -3, 0]))


def test_scene_printhead_tracks_build_in_plane():
    # The printhead tracks the seated build: the head takes the full in-plane offset
    # (X on its rail, Y via the gantry); the gantry carries Y but not X (its beam is
    # fixed); the bed STL never takes the registration.
    head = [10.0, 20.0, 4.0]
    a = mm.place_scene(_scene_profile(), _SCENE_TIP, head, registration=(0, 0))
    d = (7.0, -3.0)
    b = mm.place_scene(_scene_profile(), _SCENE_TIP, head, registration=d)
    check("head takes the full in-plane offset",
          near(b["head"] - a["head"], [d[0], d[1], 0.0]))
    check("nozzle takes the full in-plane offset",
          near(b["tip_world"] - a["tip_world"], [d[0], d[1], 0.0]))
    check("gantry takes Y, not X", near(b["gantry"] - a["gantry"], [0, d[1], 0]))
    check("bed STL never takes the registration", near(b["bed"] - a["bed"], [0, 0, 0]))


def test_scene_collector_grounded_to_registration():
    # The collector corner (machine-frame (corner_x, corner_y)) renders at
    # registration + (corner_x, corner_y) for ANY registration (the plate rides the
    # build group). Z flat for a clean XY check.
    cx, cy = -40.0, 0.0
    for reg in ((0, 0), (-60, -20), (10, 5)):
        sc = mm.place_scene(_scene_profile(), _SCENE_TIP, [cx, cy, 1.0],
                            registration=reg)
        corner_world = np.array([cx, cy, 1.0]) + sc["bed_group"]
        rel = corner_world[:2] - np.asarray(reg, float)
        check(f"collector corner is registration-relative @ reg={reg}",
              near(rel, [cx, cy]))


def test_scene_origin_triad_on_collector_corner():
    # The (0,0,0) origin triad sits on the chosen collector corner for ANY
    # registration. Evaluated at the home point so the bed hasn't descended.
    cx, cy, ptz = -40.0, 0.0, 1.0
    for reg in ((0, 0), (-60, -20), (25, 10)):
        prof = _scene_profile()
        o = np.array([-cx, -cy, -ptz])
        c = mm.place_scene(prof, _SCENE_TIP, np.zeros(3) + o, origin=o,
                           registration=reg)
        triad_c = np.zeros(3) + c["bed_group"]
        check(f"collector triad on the collector corner @ reg={reg}",
              near(triad_c, np.asarray(reg, float).tolist() + [0.0]
                   + np.array([cx, cy, ptz])))


def test_scene_origin_toggle_moves_nothing_but_triad():
    # Switching the g-code origin re-base must leave every rendered part in place;
    # only the (0,0,0) triad relocates to the new datum.
    cx, cy, ptz = -40.0, 0.0, 1.0
    o = np.array([-cx, -cy, -ptz])               # collector origin re-base
    p = np.array([3.0, 4.0, ptz])                # a physical commanded point
    m = mm.place_scene(_scene_profile(), _SCENE_TIP, p, registration=(-60, -20))
    c = mm.place_scene(_scene_profile(), _SCENE_TIP, p + o, origin=o,
                       registration=(-60, -20))
    for k in ("head", "gantry", "bed", "static", "tip_world"):
        check(f"origin toggle keeps {k} put", near(m[k], c[k]))
    f_machine = np.array([cx, cy, ptz])
    check("origin toggle keeps the scaffold/collector put",
          near(f_machine + m["bed_group"], (f_machine + o) + c["bed_group"]))
    check("origin triad relocates to the chosen datum",
          near(c["bed_group"] - m["bed_group"], -o))


def test_scene_assembly_intact():
    # The printhead rides the gantry (shared Y and Z; leads in X by the commanded X
    # plus the in-plane registration X) and the bed descends with the layer.
    cx, cy, ptz = -40.0, 0.0, 1.0
    for o in (np.zeros(3), np.array([-cx, -cy, -ptz])):
        for reg in ((0, 0), (-60, -20)):
            prof = _scene_profile()
            lo = mm.place_scene(prof, _SCENE_TIP, np.array([10., 12., ptz]) + o,
                                origin=o, registration=reg)
            hi = mm.place_scene(prof, _SCENE_TIP, np.array([10., 12., ptz + 5.]) + o,
                                origin=o, registration=reg)
            check(f"head rides gantry in Y (reg={reg})",
                  near(lo["head"][1], lo["gantry"][1]))
            check(f"head rides gantry in Z (reg={reg})",
                  near(lo["head"][2], lo["gantry"][2]))
            check(f"head leads gantry in X (reg={reg})",
                  near(lo["head"][0] - lo["gantry"][0], 10.0 + reg[0]))
            check(f"bed descends with the layer (reg={reg})",
                  near(lo["bed"][2] - hi["bed"][2], 5.0))
            check(f"scaffold sinks with the bed (reg={reg})",
                  near(lo["bed_group"][2] - hi["bed_group"][2], 5.0))


def test_scene_combos_printhead_always_over_print():
    # Bulletproofing sweep: plate corner X registration X scaffold point X origin.
    for cx, cy in ((-40.0, 0.0), (0.0, 0.0), (12.5, -7.0)):
        for ptz in (1.0, 0.5):
            for reg in ((0, 0), (-60, -20), (25, 40)):
                for o in (np.zeros(3), np.array([-cx, -cy, -ptz])):
                    prof = _scene_profile()
                    for sx, sy in ((cx + 3, cy + 4), (cx + 30, cy + 18)):
                        head = np.array([sx, sy, ptz]) + o
                        sc = mm.place_scene(prof, _SCENE_TIP, head, origin=o,
                                            registration=reg)
                        scaf = _scaffold_world(sc, head)
                        check(f"over-print c=({cx},{cy}) reg={reg}",
                              near(sc["tip_world"][:2], scaf[:2]))
                    col = (np.array([cx, cy, 0.0]) + o) + sc["bed_group"]
                    check(f"collector grounded c=({cx},{cy}) reg={reg}",
                          near(col[:2] - np.asarray(reg, float), [cx, cy]))


def test_scene_elevation_bake_neutral_to_sim():
    # The "add printhead elevation to Z" checkbox raises the exported g-code Z by
    # baking it into the origin re-base. The player UNDOES the whole re-base, so the
    # 3D scene must be identical whether or not the elevation is baked in.
    cx, cy, ptz, elev = -40.0, 0.0, 1.0, 5.0
    bed_top = 1.0
    raise_z = (_SCENE_TIP[2] - bed_top) - 2.0
    prof = _scene_profile(bed_raise=raise_z)
    reg = (-60, -20)
    p = np.array([cx + 6.0, cy + 8.0, ptz])              # physical first-layer point
    plain = np.array([-cx, -cy, -ptz])                   # collector origin re-base
    baked = plain + np.array([0.0, 0.0, elev])           # + elevation (checkbox on)
    a = mm.place_scene(prof, _SCENE_TIP, p + plain, origin=plain, registration=reg)
    b = mm.place_scene(prof, _SCENE_TIP, p + baked, origin=baked, registration=reg)
    for k in ("head", "gantry", "bed", "static", "tip_world"):
        check(f"elevation bake leaves {k} unchanged in the sim", near(a[k], b[k]))
    check("elevation bake leaves the rendered scaffold put",
          near((p + plain) + a["bed_group"], (p + baked) + b["bed_group"]))
    scaf = _scaffold_world(b, p + baked)
    check("baked: printhead still over the scaffold", near(b["tip_world"][:2], scaf[:2]))


def test_scene_collector_sits_on_bed_z():
    # End to end in Z: with the player's bed auto-raise, the collector bottom rests on
    # the bed STL top. raise = tip_z - bed_top - standoff (scale 1).
    standoff, bed_top, thickness, floor = 2.0, 1.0, 1.0, 0.0
    raise_z = (_SCENE_TIP[2] - bed_top) - standoff
    cx, cy = -40.0, 0.0
    p = np.array([cx + 6.0, cy + 8.0, floor + thickness])     # laying the first layer
    o = np.array([-cx, -cy, -(floor + thickness)])
    sc = mm.place_scene(_scene_profile(bed_raise=raise_z), _SCENE_TIP, p + o,
                        origin=o, registration=(-60, -20))
    bed_top_world = bed_top + sc["bed"][2]                  # bed STL top
    col_bottom_exported = np.array([cx, cy, floor]) + o     # collector bottom
    col_bottom_world = (col_bottom_exported + sc["bed_group"])[2]
    check("collector bottom rests on the bed top",
          near(col_bottom_world, bed_top_world))


# -- motion-limit clamp (3D-sim only) --------------------------------------- #
def test_motion_limit_clamp():
    # In-range commands are a no-op (identical to no limits); an out-of-range command
    # pins the driven body at its limit while the tip + build run on unclamped.
    base = mm.place_scene(_scene_profile(), _SCENE_TIP, [10.0, 20.0, 4.0])
    lim = mm.MachineProfile.from_dict(
        {"parts": {"x": "h.stl", "y": "g.stl", "z": "b.stl"},
         "static_stl": "f.stl", "standoff": 2.0,
         "limits": {"head": {"x": [0, 50]}, "gantry": {"y": [0, 50]},
                    "bed": {"z": [0, 50]}}})
    inr = mm.place_scene(lim, _SCENE_TIP, [10.0, 20.0, 4.0])     # all within [0,50]
    for k in ("head", "gantry", "bed", "tip_world", "bed_group"):
        check(f"in-range clamp is a no-op ({k})", near(inr[k], base[k]))
    # command X=80 (> head limit 50): the head BODY pins at X=50 while the tip tracks
    over = mm.place_scene(lim, _SCENE_TIP, [80.0, 20.0, 4.0])
    free = mm.place_scene(_scene_profile(), _SCENE_TIP, [80.0, 20.0, 4.0])
    clamped = mm.place_scene(_scene_profile(), _SCENE_TIP, [50.0, 20.0, 4.0])
    check("out-of-range head body pins at the limit",
          near(over["head"], clamped["head"]))
    check("out-of-range tip still tracks the command",
          near(over["tip_world"], free["tip_world"]))
    check("out-of-range build group is unclamped",
          near(over["bed_group"], free["bed_group"]))


def test_motion_limit_clamps_registration_too():
    # THE issue-2 fix: the clamp must bound the WHOLE driven motion -- commanded AND
    # the in-plane registration -- so a part never leaves its rails when the build is
    # seated past its reach. A bare commanded-only clamp let the registration shove the
    # gantry off the frame.
    lim = mm.MachineProfile.from_dict(
        {"parts": {"x": "h.stl", "y": "g.stl", "z": "b.stl"},
         "static_stl": "f.stl", "standoff": 2.0,
         "limits": {"gantry": {"y": [0, 100]}}})
    free = _scene_profile()
    # command Y=10 (in range) but registration Y=-80 seats the build far in -Y; the
    # gantry's driven Y displacement = 10 + (-80) = -70, below the limit 0 -> pin at 0.
    sc = mm.place_scene(lim, _SCENE_TIP, [10.0, 10.0, 4.0], registration=(0.0, -80.0))
    home0 = mm.place(lim, _SCENE_TIP, 0.0, 0.0, 0.0)
    check("gantry Y pinned at its lower limit (stays on the frame)",
          near(sc["gantry"][1] - home0["gantry"][1], 0.0),
          f"disp={sc['gantry'][1] - home0['gantry'][1]}")
    # the head RIDES the gantry, so the clamp delta carried to it (shared Y with the
    # gantry) -- the assembly stays rigid, not torn apart by the clamp.
    check("head rides the clamped gantry in Y",
          near(sc["head"][1], sc["gantry"][1]))
    # the nozzle is NOT clamped -- it floats to the (unreachable) seated print, honest
    # about the machine maxing out
    free_sc = mm.place_scene(free, _SCENE_TIP, [10.0, 10.0, 4.0],
                             registration=(0.0, -80.0))
    check("tip still tracks the seated print (unclamped)",
          near(sc["tip_world"], free_sc["tip_world"]))
    # with a generous limit nothing pins and it matches the unlimited placement
    ok = mm.MachineProfile.from_dict(
        {"parts": {"x": "h.stl", "y": "g.stl", "z": "b.stl"},
         "static_stl": "f.stl", "standoff": 2.0,
         "limits": {"gantry": {"y": [-200, 200]}}})
    sc2 = mm.place_scene(ok, _SCENE_TIP, [10.0, 10.0, 4.0], registration=(0.0, -80.0))
    for k in ("head", "gantry", "bed", "tip_world", "bed_group"):
        check(f"in-reach registration is a no-op ({k})", near(sc2[k], free_sc[k]))


# -- bed-anchored slide registration helper (player-side math) -------------- #
def test_slide_registration_seats_collector_on_bed():
    # Mirror what the player computes: registration = bed_corner + slide_offset -
    # canonical_collector_origin, so the collector bottom-left lands at
    # bed_corner + slide_offset in world. cx,cy are the collector corner in g-code.
    prof = mm.MachineProfile.from_dict(
        {"parts": {"x": "h.stl", "y": "g.stl", "z": "b.stl"}, "standoff": 2.0,
         "slide_offset": [4.0, 6.0]})
    cx, cy, ptz = -40.0, 0.0, 1.0
    o = np.array([-cx, -cy, -ptz])
    bed_corner = np.array([100.0, 50.0])      # bed STL top-face bottom-left (world)
    # canonical collector-corner world (registration 0): the g-code corner (cx,cy)
    # rides bed_group with no registration.
    canon = mm.place_scene(prof, _SCENE_TIP, np.array([cx, cy, ptz]) + o, origin=o)
    canon_corner = (np.array([cx, cy, ptz]) + o + canon["bed_group"])[:2]
    reg = bed_corner + prof.slide_offset - canon_corner
    sc = mm.place_scene(prof, _SCENE_TIP, np.array([cx, cy, ptz]) + o, origin=o,
                        registration=reg)
    corner_world = (np.array([cx, cy, ptz]) + o + sc["bed_group"])[:2]
    check("collector corner seats at bed_corner + slide_offset",
          near(corner_world, bed_corner + prof.slide_offset))
    # and the nozzle is still over that corner
    check("nozzle over the seated corner", near(sc["tip_world"][:2], corner_world))


# -- generalized kinematics: other axis setups ----------------------------- #
def _gen_profile(kinematics, standoff=2.0, bed_raise=None):
    """A 4-part profile (head/gantry/bed/frame) on the named kinematics preset."""
    p = mm.MachineProfile.from_dict(
        {"kinematics": kinematics,
         "parts": {"head": "h.stl", "gantry": "g.stl", "bed": "b.stl",
                   "frame": "f.stl"},
         "standoff": standoff})
    if bed_raise is not None:
        p.part_offsets["bed"] = np.array([0.0, 0.0, bed_raise])
    return p


def test_generalized_nozzle_over_print():
    # THE cardinal invariant for EVERY supported rig: the nozzle tip sits directly
    # over (one standoff above) the active scaffold point -- swept over each
    # kinematics preset, every registration, and both g-code origins.
    standoff = 2.0
    for kin in ("mewron", "xyz_head", "xy_bed_z_head"):
        for cx, cy, ptz in ((-40.0, 0.0, 1.0), (0.0, 0.0, 0.5), (12.5, -7.0, 1.0)):
            for reg in ((0, 0), (-60, -20), (25, 15)):
                for o in (np.zeros(3), np.array([-cx, -cy, -ptz])):
                    prof = _gen_profile(kin, standoff=standoff)
                    for sx, sy in ((cx + 3, cy + 4), (cx + 30, cy + 18)):
                        head = np.array([sx, sy, ptz]) + o
                        sc = mm.place_scene(prof, _SCENE_TIP, head, origin=o,
                                            registration=reg)
                        scaf = _scaffold_world(sc, head)
                        check(f"{kin} reg={reg}: nozzle over scaffold XY",
                              near(sc["tip_world"][:2], scaf[:2]))
                        check(f"{kin} reg={reg}: nozzle one standoff above scaffold",
                              near(sc["tip_world"][2] - scaf[2], standoff))


def test_legacy_equals_explicit_mewron():
    # The legacy axis-keyed schema must produce byte-identical placement to an
    # explicit mewron chain -- the back-compat guarantee for old saved presets.
    legacy = mm.MachineProfile.from_dict(
        {"parts": {"x": "h.stl", "y": "g.stl", "z": "b.stl"},
         "static_stl": "f.stl", "standoff": 2.0})
    explicit = mm.MachineProfile.from_dict(
        {"kinematics": "mewron",
         "parts": {"head": "h.stl", "gantry": "g.stl", "bed": "b.stl",
                   "frame": "f.stl"},
         "standoff": 2.0})
    cx, cy, ptz = -40.0, 0.0, 1.0
    for o in (np.zeros(3), np.array([-cx, -cy, -ptz])):
        for p in (np.array([cx + 5, cy + 6, ptz]),
                  np.array([cx + 20, cy + 11, ptz + 4])):
            a = mm.place_scene(legacy, _SCENE_TIP, p + o, origin=o,
                               registration=(-60, -20))
            b = mm.place_scene(explicit, _SCENE_TIP, p + o, origin=o,
                               registration=(-60, -20))
            for k in ("head", "gantry", "bed", "static", "tip_world", "bed_group"):
                check(f"legacy==mewron {k}", near(a[k], b[k]))


def test_xyz_head_static_collector():
    # XYZ head over a static bed: the collector never moves (the bed drives nothing) --
    # the nozzle carries the whole motion. Raising the layer lifts the nozzle and
    # leaves the print put (opposite of the MEWron's descending bed).
    prof = _gen_profile("xyz_head", standoff=2.0)
    lo = mm.place_scene(prof, _SCENE_TIP, [10.0, 12.0, 1.0])
    hi = mm.place_scene(prof, _SCENE_TIP, [10.0, 12.0, 6.0])
    check("xyz_head: print stays put as Z grows",
          near(lo["bed_group"], hi["bed_group"]))
    check("xyz_head: nozzle rises with Z",
          near(hi["tip_world"][2] - lo["tip_world"][2], 5.0))


def test_xy_bed_z_head_collector_tracks_xy():
    # XY bed + Z head: the print rides the bed in XY (the nozzle XY is fixed); the
    # head carries only Z. Sweeping X slides the build under a stationary nozzle.
    prof = _gen_profile("xy_bed_z_head", standoff=2.0)
    a = mm.place_scene(prof, _SCENE_TIP, [10.0, 5.0, 1.0])
    b = mm.place_scene(prof, _SCENE_TIP, [17.0, 5.0, 1.0])
    check("xy_bed: print slides in X with the command (bed carries -g)",
          near(b["bed_group"][0] - a["bed_group"][0], -7.0))
    check("xy_bed: nozzle XY is fixed", near(a["tip_world"][:2], b["tip_world"][:2]))


def test_bed_rides_gantry_carriage():
    # A carriage UNDER the bed: head on the frame drives Z, the gantry (the carriage)
    # drives Y, and the bed rides the gantry driving X. The gantry must be LIVE -- on
    # the collector chain, moving with its Y command -- and the bed must inherit the
    # carriage's Y. This is the PhD-printer rig the GUI's bed-parent choice enables.
    prof = mm.MachineProfile.from_dict(
        {"kinematics": "custom",
         "parts": {"head": "h.stl", "gantry": "g.stl", "bed": "b.stl",
                   "frame": "f.stl"},
         "axes": {"head": ["z"], "gantry": ["y"], "bed": ["x"]},
         "parents": {"head": "frame", "gantry": "frame", "bed": "gantry"},
         "standoff": 2.0})
    check("bed-rides-gantry: gantry is on the collector chain",
          "gantry" in prof._coll_set)
    check("bed-rides-gantry: gantry sign is -1 (a collector body, not orphaned)",
          prof._sign("gantry") == -1.0)
    check("bed-rides-gantry: bed is a descendant of the gantry (carried by it)",
          prof._descendants("gantry") == ["bed"])
    lo = mm.place(prof, np.zeros(3), 0.0, 0.0, 0.0)
    hi = mm.place(prof, np.zeros(3), 0.0, 50.0, 0.0)        # command Y = 50
    check("bed-rides-gantry: the gantry/carriage MOVES with the Y command",
          not near(lo["gantry"][1], hi["gantry"][1]))
    check("bed-rides-gantry: the bed inherits the carriage's Y (rides it)",
          near(hi["bed"][1] - lo["bed"][1], hi["gantry"][1] - lo["gantry"][1]))
    check("bed-rides-gantry: the print (bed_group) rides the carriage in Y",
          not near(lo["bed_offset"][1], hi["bed_offset"][1]))

    # the regression guard: a gantry that drives an axis but carries NEITHER the head
    # nor the bed (both on the frame) is orphaned -- sign 0, never moves. This is the
    # bug the bed-parent choice fixes; here we PIN that the broken chain is detectably
    # dead so the GUI default (bed on frame, gantry idle) can't silently regress.
    orphan = mm.MachineProfile.from_dict(
        {"kinematics": "custom",
         "parts": {"head": "h.stl", "gantry": "g.stl", "bed": "b.stl"},
         "axes": {"head": ["z"], "gantry": ["y"], "bed": ["x"]},
         "parents": {"head": "frame", "gantry": "frame", "bed": "frame"}})
    check("orphaned gantry (carries nothing) has sign 0",
          orphan._sign("gantry") == 0.0)
    o_lo = mm.place(orphan, np.zeros(3), 0.0, 0.0, 0.0)
    o_hi = mm.place(orphan, np.zeros(3), 0.0, 50.0, 0.0)
    check("orphaned gantry does NOT move (the reported bug)",
          near(o_lo["gantry"], o_hi["gantry"]))


# -- nozzle<->bed contact alignment (which body closes the standoff gap) --------- #
# The player measures `gap` from the STL bounds and asks
# machine_model.contact_alignment which body to move: a bed-driven contact axis
# (MEWron) raises the bed, a nozzle-driven one (a Prusa-style Z gantry) keeps the
# bed FIXED and drops the tool and print onto it.
def _prusa_profile(standoff=2.0):
    """X printhead on a Z gantry + a Y bed -- a Prusa-style Cartesian rig (custom
    chain). The contact axis (Z) is driven by the gantry, i.e. the tool chain."""
    return mm.MachineProfile.from_dict(
        {"kinematics": "custom",
         "parts": {"head": "h.stl", "gantry": "g.stl", "bed": "b.stl",
                   "frame": "f.stl"},
         "axes": {"head": ["x"], "gantry": ["z"], "bed": ["y"]},
         "parents": {"head": "gantry", "gantry": "frame", "bed": "frame"},
         "standoff": standoff})


def test_axis_side():
    check("mewron Z is collector-driven",
          mm.MachineProfile.from_dict({"kinematics": "mewron",
              "parts": {"head": "h"}}).axis_side("z") == "collector")
    check("xyz_head Z is tool-driven",
          _gen_profile("xyz_head").axis_side("z") == "tool")
    check("xy_bed_z_head Z is tool-driven",
          _gen_profile("xy_bed_z_head").axis_side("z") == "tool")
    pr = _prusa_profile()
    check("prusa Z (gantry) is tool-driven", pr.axis_side("z") == "tool")
    check("prusa Y (bed) is collector-driven", pr.axis_side("y") == "collector")
    check("prusa X (head) is tool-driven", pr.axis_side("x") == "tool")


def test_orphaned_axes_custom_chain():
    # gantry drives Y but head & bed both mount straight on the frame -> gantry is on
    # neither chain, so its Y is unrenderable. orphaned_axes() must flag exactly that.
    orphan = mm.MachineProfile.from_dict(
        {"parts": {"head": "h"}, "kinematics": "custom",
         "axes": {"head": ["x"], "gantry": ["y"], "bed": ["z"]},
         "parents": {"head": "frame", "gantry": "frame", "bed": "frame"}})
    check("orphaned gantry Y detected", orphan.orphaned_axes() == {"gantry": ["y"]})
    # head rides the gantry -> gantry is on the tool chain -> nothing orphaned.
    ok = mm.MachineProfile.from_dict(
        {"parts": {"head": "h"}, "kinematics": "custom",
         "axes": {"head": ["x"], "gantry": ["y"], "bed": ["z"]},
         "parents": {"head": "gantry", "gantry": "frame", "bed": "frame"}})
    check("well-formed custom has no orphan", ok.orphaned_axes() == {})
    # bed rides the gantry (under-bed carriage) -> gantry on the collector chain -> ok.
    ok2 = mm.MachineProfile.from_dict(
        {"parts": {"head": "h"}, "kinematics": "custom",
         "axes": {"head": ["x"], "gantry": ["y"], "bed": ["z"]},
         "parents": {"head": "frame", "gantry": "frame", "bed": "gantry"}})
    check("bed-carriage custom has no orphan", ok2.orphaned_axes() == {})
    # stock presets never orphan
    check("mewron preset no orphan",
          mm.MachineProfile.from_dict({"kinematics": "mewron",
              "parts": {"head": "h"}}).orphaned_axes() == {})


def test_contact_alignment_bed_vs_tool():
    # MEWron: the bed rises by gap; the deposition frame is untouched.
    mew = _gen_profile("mewron")
    parts, depo = mew.contact_alignment(3.0)
    check("mewron: only the bed moves", set(parts) == {"bed"})
    check("mewron: bed rises by +gap", near(parts["bed"], [0, 0, 3.0]))
    check("mewron: deposition frame untouched", near(depo, [0, 0, 0]))
    # Prusa: the bed must NOT move; the whole tool chain + the print drop by gap.
    pr = _prusa_profile()
    parts, depo = pr.contact_alignment(3.0)
    check("prusa: bed is not nudged", "bed" not in parts)
    check("prusa: tool chain (head+gantry) drops", set(parts) == {"head", "gantry"})
    check("prusa: head drops by gap", near(parts["head"], [0, 0, -3.0]))
    check("prusa: gantry drops by gap", near(parts["gantry"], [0, 0, -3.0]))
    check("prusa: deposition frame drops with the tool", near(depo, [0, 0, -3.0]))


def _apply_contact(profile, tip, bed_top):
    """Mimic the player's _build_machine contact step: measure gap from bed_top,
    distribute it via contact_alignment into the profile's offsets. Returns gap."""
    gap = profile.scale * (tip[2] - bed_top) - profile.standoff
    parts, depo = profile.contact_alignment(gap)
    for role, off in parts.items():
        profile.part_offsets[role] = profile.part_offsets.get(role, np.zeros(3)) + off
    profile.deposition_offset = profile.deposition_offset + depo
    return gap


def test_prusa_fixed_bed_drops_tool_not_plate():
    # End to end: with the contact step applied, the bed STL top stays exactly where
    # it was modelled (it does NOT lift to the nozzle), the collector bottom rests on
    # that fixed bed top, the nozzle sits one standoff above the deposited scaffold,
    # and the gantry has descended by the gap.
    bed_top, standoff, floor = 0.0, 2.0, 0.0
    cx, cy, ptz = -40.0, 0.0, 1.0
    head = np.array([cx + 6.0, cy + 8.0, ptz])     # laying the first layer
    base = mm.place_scene(_prusa_profile(), _SCENE_TIP, head)   # no contact applied
    prof = _prusa_profile()
    gap = _apply_contact(prof, _SCENE_TIP, bed_top)
    check("prusa: gap is non-zero (bed parked low)", abs(gap) > 1e-6)
    sc = mm.place_scene(prof, _SCENE_TIP, head)
    check("prusa: bed actor stays put under contact", near(sc["bed"], base["bed"]))
    bed_top_world = bed_top + sc["bed"][2]
    col_bottom_world = (np.array([cx, cy, floor]) + sc["bed_group"])[2]
    check("prusa: collector bottom rests on the fixed bed top",
          near(col_bottom_world, bed_top_world))
    scaf = _scaffold_world(sc, head)
    check("prusa: nozzle one standoff above the scaffold",
          near(sc["tip_world"][2] - scaf[2], standoff))
    check("prusa: gantry descends by the gap",
          near(base["gantry"][2] - sc["gantry"][2], gap))
    check("prusa: head descends with the gantry",
          near(base["head"][2] - sc["head"][2], gap))


def test_prusa_layers_rise_bed_fixed():
    # As the print grows (commanded Z up), the nozzle + gantry rise and the fixed bed
    # stays put -- the opposite of the MEWron's descending bed. Contact applied.
    prof = _prusa_profile()
    _apply_contact(prof, _SCENE_TIP, bed_top=1.0)
    lo = mm.place_scene(prof, _SCENE_TIP, [10.0, 12.0, 1.0])
    hi = mm.place_scene(prof, _SCENE_TIP, [10.0, 12.0, 6.0])
    check("prusa: bed fixed in Z as the layer grows", near(lo["bed"], hi["bed"]))
    check("prusa: nozzle rises with the layer",
          near(hi["tip_world"][2] - lo["tip_world"][2], 5.0))
    check("prusa: gantry rises with the layer",
          near(hi["gantry"][2] - lo["gantry"][2], 5.0))
    scaf_hi = _scaffold_world(hi, [10.0, 12.0, 6.0])
    check("prusa: nozzle tracks the scaffold up", near(hi["tip_world"][2] - scaf_hi[2], 2.0))


def test_prusa_nozzle_over_print_sweep():
    # The cardinal invariant survives the contact drop for the Prusa rig across
    # registrations + both origins: nozzle one standoff directly above the active point.
    standoff = 2.0
    for cx, cy, ptz in ((-40.0, 0.0, 1.0), (12.5, -7.0, 0.5)):
        for reg in ((0, 0), (-60, -20), (25, 15)):
            for o in (np.zeros(3), np.array([-cx, -cy, -ptz])):
                prof = _prusa_profile(standoff=standoff)
                _apply_contact(prof, _SCENE_TIP, bed_top=1.0)
                for sx, sy in ((cx + 3, cy + 4), (cx + 30, cy + 18)):
                    head = np.array([sx, sy, ptz]) + o
                    sc = mm.place_scene(prof, _SCENE_TIP, head, origin=o,
                                        registration=reg)
                    scaf = _scaffold_world(sc, head)
                    check(f"prusa reg={reg}: nozzle over scaffold XY",
                          near(sc["tip_world"][:2], scaf[:2]))
                    check(f"prusa reg={reg}: nozzle one standoff above scaffold",
                          near(sc["tip_world"][2] - scaf[2], standoff))


def test_prusa_seating_slides_bed_not_gantry():
    # THE bed-slinger fix (from the real diagnostic): on a Prusa (head X, gantry Z, bed
    # Y) the registration must seat the collector WITHOUT shoving the gantry in Y. The
    # X part (tool-driven) moves the build + nozzle on the head's rail; the Y part
    # (collector-driven) slides the BED under the fixed nozzle -- the gantry, which has
    # no Y travel, must NOT move (the reported "gantry off the frame in -Y").
    prof = _prusa_profile()
    _apply_contact(prof, _SCENE_TIP, bed_top=1.0)
    base = mm.place_scene(prof, _SCENE_TIP, [10.0, 12.0, 1.0])
    prof2 = _prusa_profile()
    _apply_contact(prof2, _SCENE_TIP, bed_top=1.0)
    sc = mm.place_scene(prof2, _SCENE_TIP, [10.0, 12.0, 1.0], registration=(7.0, -3.0))
    # the gantry is NOT shoved in Y (or anywhere) by the seating -- the actual bug fix
    check("prusa: gantry is NOT moved by the seating", near(sc["gantry"], base["gantry"]))
    check("prusa: static frame never moves", near(sc["static"], base["static"]))
    # the bed slides under the nozzle for the Y (collector-driven) seating: -reg_y
    check("prusa: the bed slides for the Y seating (under the fixed nozzle)",
          near(sc["bed"] - base["bed"], [0.0, 3.0, 0.0]))
    # the build + nozzle take ONLY the X (tool-driven) part -- the head rides its rail
    check("prusa: build group takes only the tool-axis (X) seating",
          near(sc["bed_group"] - base["bed_group"], [7.0, 0.0, 0.0]))
    check("prusa: nozzle takes only the tool-axis (X) seating",
          near(sc["tip_world"] - base["tip_world"], [7.0, 0.0, 0.0]))
    # and the cardinal invariant holds: nozzle still over the active print point
    check("prusa: nozzle stays over the print under seating",
          near(sc["tip_world"][:2], _scaffold_world(sc, [10.0, 12.0, 1.0])[:2]))


def test_mewron_contact_unchanged():
    # Guard the byte-for-byte MEWron path: applying the contact step still moves ONLY
    # the bed (rising to the nozzle) and leaves the deposition frame at zero.
    prof = _gen_profile("mewron")
    base = mm.place_scene(_gen_profile("mewron"), _SCENE_TIP, [10.0, 12.0, 1.0])
    gap = _apply_contact(prof, _SCENE_TIP, bed_top=1.0)
    check("mewron: deposition offset stays zero", near(prof.deposition_offset, [0, 0, 0]))
    sc = mm.place_scene(prof, _SCENE_TIP, [10.0, 12.0, 1.0])
    check("mewron: bed rises by the gap", near(sc["bed"][2] - base["bed"][2], gap))
    for k in ("head", "gantry", "static", "tip_world", "bed_group"):
        check(f"mewron: contact leaves {k} put", near(sc[k], base[k]))


# -- the rotary joint (a mandrel collector) --------------------------------- #
def _rot_about(axis, angle_deg, pts):
    """Rodrigues rotation of (N,3) points about a unit `axis` through the origin.

    Written here rather than taken from machine_model, which only ever REPORTS
    an (angle, axis, pivot) and never turns one into a matrix -- so this is an
    independent reading of what the model asks the viewer to do, not a rerun of
    its own arithmetic."""
    k = np.asarray(axis, float)
    k = k / np.linalg.norm(k)
    p = np.atleast_2d(np.asarray(pts, float))
    th = np.radians(angle_deg)
    return (p * np.cos(th) + np.cross(k, p) * np.sin(th)
            + np.outer(p @ k, k) * (1.0 - np.cos(th)))


def _mandrel_profile(radius=1.5, length=30.0, standoff=1.0):
    return mm.MachineProfile.from_dict(
        {"parts": {"head": "h.stl", "gantry": "g.stl", "bed": "b.stl"},
         "kinematics": "mewron", "tip": [0, 0, 0], "standoff": standoff,
         "rotary": {"word": "A", "role": "bed",
                    "radius": radius, "length": length}})


def test_rotary_absent_is_the_old_scene():
    """A profile with no rotary must come back with the keys it always had --
    the rotary ones only exist when there is a joint to describe."""
    p = mm.MachineProfile.from_dict(
        {"parts": {"head": "h.stl", "gantry": "g.stl", "bed": "b.stl"},
         "kinematics": "mewron", "tip": [0, 0, 0]})
    a = mm.place_scene(p, [0, 0, 0], [3, 4, 5])
    b = mm.place_scene(p, [0, 0, 0], [3, 4, 5], rotary_deg=137.0)
    check("no rotary -> no rotary keys",
          not any(k.startswith("collector_") for k in a))
    check("no rotary -> a rotary word changes nothing",
          all(near(a[k], b[k]) for k in a), "keys %s" % sorted(a))
    check("is_rotary is False", not p.is_rotary())


def test_rotary_schema_round_trip():
    p = _mandrel_profile(radius=3.0, length=12.0)
    check("rotary parsed", p.is_rotary() and p.rotary.word == "A")
    check("A turns about X", p.rotary.axis == "x")
    check("radius + length parsed",
          p.rotary.radius == 3.0 and p.rotary.length == 12.0)
    check("round trips", mm.RotaryAxis.from_dict(p.rotary.to_dict()).to_dict()
          == p.rotary.to_dict())
    check("absent -> None", mm.RotaryAxis.from_dict(None) is None)
    try:
        mm.RotaryAxis(word="Q")
        bad = False
    except ValueError:
        bad = True
    check("an unknown rotary word raises rather than defaulting", bad)


def test_rotary_sign_puts_the_fresh_fibre_under_the_nozzle():
    """THE sign test. `mew_frame.to_world` puts a point deposited at mandrel
    reading A at body azimuth A; rotating the group by what the model reports
    must bring that point back to azimuth 0 -- +z, straight under the nozzle,
    where the jet just laid it. A flipped sign spins the tube the wrong way and
    nothing else in the scene complains."""
    R0 = 1.5
    p = _mandrel_profile(radius=R0)
    frame = mew_frame.CylinderFrame(R0)
    worst = 0.0
    for a_deg in (0.0, 30.0, 90.0, 210.0, 719.0):
        v = R0 * np.radians(a_deg)                 # the fibre laid at this reading
        w = np.column_stack(frame.to_world(np.array([5.0]), np.array([v]),
                                           np.array([0.0])))
        ang, axis, pivot = mm.place_scene(
            p, [0, 0, 0], [5, 0, 0], rotary_deg=a_deg)["collector_rotation"]
        check("the pivot is the axis through the origin", near(pivot, [0, 0, 0]))
        spun = _rot_about(axis, ang, w - pivot)[0] + pivot
        worst = max(worst, abs(spun[1]), abs(spun[2] - R0))
    check("the point laid at reading A ends up under the nozzle",
          worst < 1e-9, "worst offset %.2e mm" % worst)
    # the control: the opposite sign must NOT land there, or the test above
    # would pass on a mandrel that never turned
    ang, axis, pivot = mm.place_scene(
        p, [0, 0, 0], [5, 0, 0], rotary_deg=90.0)["collector_rotation"]
    w = np.column_stack(frame.to_world(np.array([5.0]),
                                       np.array([R0 * np.radians(90.0)]),
                                       np.array([0.0])))
    back = _rot_about(axis, -ang, w - pivot)[0]
    check("CONTROL: the opposite sign does not", abs(back[2] - R0) > 0.5 * R0,
          "z %.4f vs R0 %.4f" % (back[2], R0))


def test_rotary_nozzle_sits_one_standoff_above_the_SURFACE():
    """The invariant every other rig in this file is pinned to, on a mandrel:
    the tip is exactly over the active print point, one standoff above it.

    It is the seat that earns this. The wrapped tube measures from the mandrel
    AXIS, so without `-R0` the nozzle would hover one standoff above the axis --
    on a 6 mm mandrel, 3 mm INSIDE the collector."""
    R0, standoff = 1.5, 1.0
    p = _mandrel_profile(radius=R0, standoff=standoff)
    frame = mew_frame.CylinderFrame(R0)
    worst = 0.0
    for u, h, a_deg in ((0.0, 0.0, 0.0), (7.5, 0.02, 415.0),
                        (12.0, 0.40, 1080.0), (3.0, 0.10, 37.5)):
        sc = mm.place_scene(p, [0, 0, 0], [u, 0.0, h], rotary_deg=a_deg)
        ang, axis, pivot = sc["collector_rotation"]
        v = R0 * np.radians(a_deg)
        w = np.column_stack(frame.to_world(np.array([u]), np.array([v]),
                                           np.array([h])))
        world = _rot_about(axis, ang, w - pivot)[0] + pivot + sc["bed_group"]
        worst = max(worst, float(np.abs(sc["tip_world"]
                                        - (world + [0, 0, standoff])).max()))
    check("the nozzle tip is one standoff above the fresh fibre, every case",
          worst < 1e-9, "worst %.2e mm" % worst)
    flat = mm.MachineProfile.from_dict(
        {"parts": {"head": "h.stl", "gantry": "g.stl", "bed": "b.stl"},
         "kinematics": "mewron", "tip": [0, 0, 0], "standoff": standoff})
    check("...and the seat is exactly -R0 along the nozzle normal",
          near(mm.place_scene(p, [0, 0, 0], [1, 0, 0])["collector_seat"],
               [0, 0, -R0])
          and near(mm.place_scene(p, [0, 0, 0], [1, 0, 0])["bed_group"]
                   - mm.place_scene(flat, [0, 0, 0], [1, 0, 0])["bed_group"],
                   [0, 0, -R0]))


def test_rotary_body_is_placed_not_decorated():
    """The plan's bar: move the mandrel and the collector follows. The body is
    reported in the same frame as the rotation and the geometry, so a change of
    radius or of where the tube sits moves the drawn cylinder with it."""
    p = _mandrel_profile(radius=1.5, length=30.0)
    c0, d0, r0, l0 = mm.place_scene(p, [0, 0, 0], [1, 0, 0],
                                    rotary_start=0.0)["collector_body"]
    c1, _d, _r, _l = mm.place_scene(p, [0, 0, 0], [1, 0, 0],
                                    rotary_start=20.0)["collector_body"]
    check("the cylinder is centred on the print along its own axis",
          near(c0, [15.0, 0, 0]) and near(c1, [35.0, 0, 0]))
    check("...along the rotation axis", near(d0, [1, 0, 0]) and r0 == 1.5
          and l0 == 30.0)
    p.rotary.radius = 3.0
    c2, _d, r2, _l = mm.place_scene(p, [0, 0, 0], [1, 0, 0],
                                    rotary_start=0.0)["collector_body"]
    seat2 = mm.place_scene(p, [0, 0, 0], [1, 0, 0])["collector_seat"]
    check("a bigger mandrel moves the body AND the seat together",
          r2 == 3.0 and near(seat2, [0, 0, -3.0]))


def test_rotary_leaves_the_part_kinematics_alone():
    """A rotation moves no part's ORIGIN, so the bodies must place exactly as
    they do on the same rig with the joint removed -- only the build group is
    seated, and only by the radius."""
    p = _mandrel_profile(radius=1.5)
    flat = mm.MachineProfile.from_dict(
        {"parts": {"head": "h.stl", "gantry": "g.stl", "bed": "b.stl"},
         "kinematics": "mewron", "tip": [0, 0, 0]})
    a = mm.place_scene(p, [0, 0, 0], [4, 0, 0.3], rotary_deg=250.0)
    b = mm.place_scene(flat, [0, 0, 0], [4, 0, 0.3])
    check("head / gantry / bed / static / tip are untouched",
          all(near(a[k], b[k]) for k in
              ("head", "gantry", "bed", "static", "tip_world")))


# -- five axes: an ordered PAIR of rotary joints ----------------------------- #
# The nozzle on a table-table machine only translates, so every orientation has
# to be made by the collector. These cases ask one question in several shapes:
# does a commanded XYZ plus two rotary words put the nozzle over the intended
# surface point with its axis along that point's normal?
#
# THE COMPARATOR IS A FORWARD ROUND TRIP, never the solver's own arithmetic.
# `machine_model.solve_rotaries` is asked for the readings; then the readings go
# through `place_scene`, and the (angle, axis, pivot) triples it REPORTS are
# applied by `_rot_about` -- the independent Rodrigues already in this file --
# to the surface point and to its normal. What is checked is where those two
# land, against `tip_world` and against +Z. Nothing in that path re-runs the
# inverse solve, so a solver that agrees with itself and with nothing else fails.
#
# The dome and torus normals below are written out in closed form here rather
# than taken from a frame class, so this suite does not wait on one and does not
# inherit its conventions.


def _trunnion(tilt_pivot=(0.0, 0.0, -8.0), standoff=1.0):
    """An A-C trunnion: a C table (about Z) carried by an A cradle (about X).

    Collector-first, so `rotaries[0]` is C -- the joint the collector is bolted
    to -- and `rotaries[1]` is A, the cradle bolted to the machine. The cradle's
    axis is deliberately NOT through the origin: on a real trunnion it sits below
    the table top, and a pivot silently taken as the origin is right on the axis
    and wrong everywhere else."""
    return mm.MachineProfile.from_dict(
        {"parts": {"head": "h.stl", "gantry": "g.stl", "bed": "b.stl"},
         "kinematics": "mewron", "tip": [0, 0, 0], "standoff": standoff,
         "rotaries": [{"word": "C", "role": "bed"},
                      {"word": "A", "role": "bed",
                       "pivot": list(tilt_pivot)}]})


# THE TWO SHAPES BELOW ARE IN THE COLLECTOR'S OWN FRAME, which is X-AXIAL:
# pole on +x, parallels in Y-Z, azimuth measured from +z. That is
# `machine_model.COLLECTOR_AXIS` and it is what `mew_frame` builds -- its
# `to_world` returns `(x, r sin phi, r cos phi)` and its `from_world` inverts
# with `hypot(y, z)` / `atan2(y, z)`.
#
# They used to be Z-axial, and that is worth a line because it is how a real
# defect hid: a Z-axial shape handed to a rig whose collector-carrying joint
# spins about Z needs no mount rotation at all, so these cases stayed green
# while every collector the program actually BUILDS came out lying on its side.
# It was found by looking at a real rig: the dome did not seat on the C axis's
# surface but stood perpendicular to the C axis.
def _dome_point_and_normal(radius, polar_deg, azim_deg):
    """A point on a dome of `radius` and its outward normal, in the COLLECTOR's
    own frame (pole on +x). Closed form, so it owes nothing to `mew_frame`."""
    t, a = np.radians(polar_deg), np.radians(azim_deg)
    n = np.array([np.cos(t), np.sin(t) * np.sin(a), np.sin(t) * np.cos(a)])
    return radius * n, n


def _torus_point_and_normal(major, minor, u_deg, v_deg):
    """A point on a torus about the COLLECTOR's own axis (+x), and its outward
    normal. Closed form."""
    u, v = np.radians(u_deg), np.radians(v_deg)
    rho = major + minor * np.cos(v)
    p = np.array([minor * np.sin(v), rho * np.sin(u), rho * np.cos(u)])
    n = np.array([np.sin(v), np.cos(v) * np.sin(u), np.cos(v) * np.cos(u)])
    return p, n


def _apply_reported(triples, vec, *, is_point):
    """Run `vec` through the (angle, axis, pivot) triples place_scene reported,
    in list order, using this file's own Rodrigues. A DIRECTION ignores pivots."""
    out = np.asarray(vec, float)
    for ang, axis, pivot in triples:
        piv = np.asarray(pivot, float) if is_point else np.zeros(3)
        out = _rot_about(axis, ang, out - piv)[0] + piv
    return out


def _five_axis_miss(profile, point, normal, *, triples=None, readings=None):
    """(position error mm, normal error) for one commanded point, measured the
    long way round through place_scene's reported transform."""
    xyz, sol = mm.five_axis_command(profile, point, normal)
    if readings is not None:
        sol = readings
    sc = mm.place_scene(profile, [0, 0, 0], xyz, rotary_deg=sol)
    tri = sc["collector_rotations"] if triples is None else triples
    world = _apply_reported(tri, point, is_point=True) + sc["bed_group"]
    want = sc["tip_world"] - np.array([0.0, 0.0, profile.standoff])
    up = _apply_reported(tri, normal, is_point=False)
    return (float(np.abs(world - want).max()),
            float(np.abs(up - [0, 0, 1]).max()))


def test_five_axis_pair_parses_and_keeps_its_order():
    p = _trunnion()
    check("two joints, collector-first", p.rotary_count == 2
          and p.rotaries[0].word == "C" and p.rotaries[1].word == "A")
    check("is_five_axis", p.is_five_axis() and p.is_rotary())
    check("`rotary` is the collector-carrying one", p.rotary is p.rotaries[0])
    check("the cradle pivot survives the schema",
          near(p.rotaries[1].pivot, [0, 0, -8]))
    check("a joint round trips through to_dict",
          mm.RotaryAxis.from_dict(p.rotaries[1].to_dict()).to_dict()
          == p.rotaries[1].to_dict())
    # a shipped single-joint profile must round trip to the SAME BYTES it did
    # before the pivot field existed, or every saved machine.json changes.
    plain = mm.RotaryAxis(word="A", role="bed", radius=1.5, length=30.0)
    check("a pivot-less joint writes no pivot and no axis",
          plain.to_dict() == {"word": "A", "role": "bed",
                              "radius": 1.5, "length": 30.0})
    # Terranova 2024's five-axis files use U and V, which ISO reserves for
    # secondary LINEAR axes -- there is no axis to infer, so one must be named.
    uv = mm.RotaryAxis(word="U", role="bed", axis="y")
    check("an unlisted word is legal when it names its axis",
          uv.word == "U" and uv.axis == "y"
          and mm.RotaryAxis.from_dict(uv.to_dict()).axis == "y")
    try:
        mm.RotaryAxis(word="U")
        bad = False
    except ValueError:
        bad = True
    check("...and still raises without one", bad)


def test_five_axis_puts_the_nozzle_on_the_normal_of_a_DOME():
    """THE case. A dome is the simplest doubly-curved collector and the one with
    no distinguished directions, so every reachable orientation is exercised by
    walking the polar angle out from the apex."""
    p = _trunnion()
    worst_pos = worst_n = 0.0
    for polar in (0.0, 5.0, 17.5, 40.0, 65.0, 85.0):
        for azim in (0.0, 37.0, 123.0, 251.0, 340.0):
            pt, n = _dome_point_and_normal(10.0, polar, azim)
            dp, dn = _five_axis_miss(p, pt, n)
            worst_pos, worst_n = max(worst_pos, dp), max(worst_n, dn)
    check("the surface point ends up exactly under the nozzle tip",
          worst_pos < 1e-9, "worst %.2e mm" % worst_pos)
    check("...and its normal ends up pointing straight up at it",
          worst_n < 1e-9, "worst %.2e" % worst_n)


def test_five_axis_puts_the_nozzle_on_the_normal_of_a_TORUS():
    """A torus is the shape a dome cannot test: its two principal curvatures
    differ everywhere, it has a saddle band on the inside, and its normals sweep
    the whole sphere -- including ones the cradle has to tilt past vertical for."""
    p = _trunnion()
    worst_pos = worst_n = 0.0
    for u in (0.0, 45.0, 100.0, 190.0, 300.0):
        for v in (-80.0, -35.0, 0.0, 20.0, 60.0, 110.0):
            pt, n = _torus_point_and_normal(12.0, 4.0, u, v)
            dp, dn = _five_axis_miss(p, pt, n)
            worst_pos, worst_n = max(worst_pos, dp), max(worst_n, dn)
    check("every torus point ends up under the tip", worst_pos < 1e-9,
          "worst %.2e mm" % worst_pos)
    check("...on its own normal", worst_n < 1e-9, "worst %.2e" % worst_n)


def test_CONTROL_the_round_trip_can_fail():
    """The two controls. A harness that answers the same for every input is not
    a measurement, so each half of the check above is shown moving.

    The ORDER control: `collector_rotations` is collector-first and a consumer
    applies it in list order. Applied backwards the same two readings are a
    different orientation, so the normal must miss.

    The PIVOT control: the cradle axis is 8 mm below the table. Taking its pivot
    as the origin -- what the model did before this slice, and what looks right
    because it IS right on the axis -- must move the point off the nozzle."""
    p = _trunnion()
    pt, n = _dome_point_and_normal(10.0, 40.0, 123.0)
    xyz, sol = mm.five_axis_command(p, pt, n)
    tri = mm.place_scene(p, [0, 0, 0], xyz, rotary_deg=sol)["collector_rotations"]
    _dp, dn = _five_axis_miss(p, pt, n, triples=list(reversed(tri)))
    check("CONTROL: applying the pair in the wrong order misses the nozzle axis",
          dn > 0.1, "normal error %.2e" % dn)
    flat_pivot = [(a, ax, np.zeros(3)) for a, ax, _q in tri]
    dp2, _dn2 = _five_axis_miss(p, pt, n, triples=flat_pivot)
    check("CONTROL: forgetting the cradle pivot misses the point",
          dp2 > 1.0, "position error %.4f mm" % dp2)


def test_the_four_axis_solve_reproduces_the_shipped_sign():
    """The solver's outside tie. On ONE joint the answer is already fixed: the
    shipped rule is that rotating the group by +A brings the fibre laid at
    reading A back under the nozzle, and `mew_frame.CylinderFrame.to_world` is
    what defines where azimuth A is. So solving for the surface normal at
    azimuth A must return A -- an agreement with two objects written before this
    slice, neither of which knows the solver exists."""
    R0 = 1.5
    p = _mandrel_profile(radius=R0)
    frame = mew_frame.CylinderFrame(R0)
    worst = 0.0
    for a_deg in (0.0, 30.0, 90.0, -137.0, 179.0):
        v = R0 * np.radians(a_deg)
        # the outward normal at that azimuth, straight off the shipped frame
        w = np.column_stack(frame.to_world(np.array([5.0]), np.array([v]),
                                           np.array([0.0])))[0]
        n = np.array([0.0, w[1], w[2]]) / R0
        (got,) = mm.solve_rotaries(p.rotaries, n)
        worst = max(worst, abs(got - a_deg))
    check("the one-joint solve returns the mandrel reading itself",
          worst < 1e-9, "worst %.2e deg" % worst)
    # a mandrel presents only the normals on its own circle; anything off it has
    # no reading at all, and saying so is the point of returning nothing.
    check("a normal off the mandrel's circle has no solution",
          mm.rotary_solutions(p.rotaries, [0.6, 0.0, 0.8]) == [])


def test_both_five_axis_solutions_are_real():
    """A two-axis wrist has TWO readings for almost every normal, and the choice
    between them is a path-continuity question this module deliberately does not
    own. Both must actually work, or `rotary_solutions` is offering a branch that
    only `solve_rotaries`'s preference is hiding."""
    p = _trunnion()
    pt, n = _dome_point_and_normal(10.0, 55.0, 71.0)
    sols = mm.rotary_solutions(p.rotaries, n)
    check("two distinct solutions", len(sols) == 2
          and abs(sols[0][1] - sols[1][1]) > 1e-6)
    worst = 0.0
    for s in sols:
        r, _t = mm.rotary_transform(p.rotaries, s)
        worst = max(worst, float(np.abs(r @ n - [0, 0, 1]).max()))
    check("both bring the normal up to the nozzle", worst < 1e-9,
          "worst %.2e" % worst)
    check("the preferred one is the smaller tilt",
          abs(mm.solve_rotaries(p.rotaries, n)[1])
          <= max(abs(s[1]) for s in sols))


def test_five_axis_refuses_rather_than_approximates():
    p = _trunnion()
    twin = mm.MachineProfile.from_dict(
        {"parts": {"head": "h.stl", "gantry": "g.stl", "bed": "b.stl"},
         "kinematics": "mewron", "tip": [0, 0, 0],
         "rotaries": [{"word": "A", "role": "bed"},
                      {"word": "A", "role": "bed"}]})
    try:
        mm.solve_rotaries(twin.rotaries, [0, 0.6, 0.8])
        bad = False
    except mm.Unreachable:
        bad = True
    check("two parallel axes are refused, not solved", bad)
    # an axis no body drives cannot carry a coordinate, so a point needing one is
    # not commandable -- the silent version of this is a print in mid-air.
    static = mm.MachineProfile.from_dict(
        {"parts": {"head": "h.stl"}, "kinematics": "custom",
         "axes": {"head": ["x", "z"]}, "parents": {"head": "frame"},
         "tip": [0, 0, 0],
         "rotaries": [{"word": "C", "role": "bed"},
                      {"word": "A", "role": "bed"}]})
    try:
        mm.five_axis_command(static, [3.0, 4.0, 5.0], [0, 0, 1])
        bad2 = False
    except mm.Unreachable:
        bad2 = True
    check("a point needing an undriven axis is refused", bad2)
    check("more than two joints is a schema error",
          _raises(lambda: mm.MachineProfile.from_dict(
              {"parts": {"head": "h.stl"},
               "rotaries": [{"word": "A"}, {"word": "B"}, {"word": "C"}]}),
              ValueError))


def _raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    return False


def test_a_pair_emits_no_mandrel_keys_and_one_joint_emits_all_of_them():
    """The compatibility statement, both directions. One joint keeps every key it
    ever had with the value it had; a pair gets `collector_rotations` and nothing
    else, because a seat of -R0 and a drawn rod are mandrel facts and a curved
    collector has neither."""
    one = _mandrel_profile(radius=1.5)
    sc = mm.place_scene(one, [0, 0, 0], [5, 0, 0], rotary_deg=90.0)
    check("one joint -> one reported rotation",
          len(sc["collector_rotations"]) == 1)
    a, ax, q = sc["collector_rotations"][0]
    b, bx, r = sc["collector_rotation"]
    check("...and it IS the historical collector_rotation",
          a == b and near(ax, bx) and near(q, r))
    check("...with the mandrel keys beside it",
          "collector_seat" in sc and "collector_body" in sc)
    pair = _trunnion()
    sp = mm.place_scene(pair, [0, 0, 0], [5, 0, 0], rotary_deg=(30.0, 12.0))
    # The chain a consumer applies is MOUNT then joints, collector-first. The
    # trunnion's collector-carrying joint spins about z and a collector is
    # X-axial in its own frame, so there is a mount link in front of the two.
    joints = sp["collector_rotations"][-2:]
    check("a pair reports both joints, collector-first",
          len(sp["collector_rotations"]) == 3
          and joints[0][0] == 30.0 and joints[1][0] == 12.0
          and near(joints[0][1], [0, 0, 1]) and near(joints[1][1], [1, 0, 0]))
    check("...behind ONE mount link, which is the collector's own axis brought "
          "onto the spin axis",
          near(mm.compose_rotations([sp["collector_rotations"][0]])[0]
               @ np.array([1.0, 0.0, 0.0]), [0, 0, 1]))
    check("...and the whole chain IS rotary_transform, not a second copy of it",
          all(near(a, b) for a, b in zip(
              mm.compose_rotations(sp["collector_rotations"]),
              mm.rotary_transform(pair.rotaries, (30.0, 12.0)))))
    check("a pair reports no mandrel seat, body or single rotation",
          not any(k in sp for k in ("collector_seat", "collector_body",
                                    "collector_rotation")))
    check("...so a pair does not seat the build group by a radius it lacks",
          near(sp["bed_group"], mm.place_scene(
              mm.MachineProfile.from_dict(
                  {"parts": {"head": "h.stl", "gantry": "g.stl", "bed": "b.stl"},
                   "kinematics": "mewron", "tip": [0, 0, 0]}),
              [0, 0, 0], [5, 0, 0])["bed_group"]))
    # a scalar rotary_deg is the four-axis call and must still land on joint 0
    # -- which is the first JOINT link, behind the mount.
    check("a scalar reading still drives the collector-carrying joint",
          mm.place_scene(pair, [0, 0, 0], [5, 0, 0],
                         rotary_deg=44.0)["collector_rotations"][-2][0] == 44.0)


def test_a_joint_can_read_something_other_than_zero_at_zero():
    """`zero_deg`, and why it is a field rather than the emitter's problem.

    Terranova 2024's six supplied five-axis files all open on `U90` -- their
    tilt reads 90 where the table is flat -- and their words are U and V, which
    ISO reserves for secondary LINEAR axes. So the commanded number and the
    geometric angle are not the same number on a real machine of this kind.

    The check is that a homed-at-90 rig commands 90 where an ours-style rig
    commands 0, that the SCENE is identical either way (the same table, flat,
    described twice), and that the round trip still lands the point under the
    nozzle -- which it only can if `place_scene` takes the word back off."""
    def prof(zero):
        return mm.MachineProfile.from_dict(
            {"parts": {"head": "h.stl", "gantry": "g.stl", "bed": "b.stl"},
             "kinematics": "mewron", "tip": [0, 0, 0], "standoff": 1.0,
             "rotaries": [{"word": "V", "role": "bed", "axis": "z"},
                          {"word": "U", "role": "bed", "axis": "x",
                           "pivot": [0, 0, -8.0], "zero_deg": zero}]})

    ours, theirs = prof(0.0), prof(90.0)
    pt, n = _dome_point_and_normal(10.0, 0.0, 0.0)      # the apex: no tilt at all
    _g_a, r_a = mm.five_axis_command(ours, pt, n)
    g_b, r_b = mm.five_axis_command(theirs, pt, n)
    check("an untilted apex commands 0 on ours and 90 on theirs",
          abs(r_a[1]) < 1e-12 and abs(r_b[1] - 90.0) < 1e-12,
          "ours %.3f, theirs %.3f" % (r_a[1], r_b[1]))
    # the two rigs are the same table described twice, so the SCENE must match
    sc_a = mm.place_scene(ours, [0, 0, 0], g_b, rotary_deg=r_a)
    sc_b = mm.place_scene(theirs, [0, 0, 0], g_b, rotary_deg=r_b)
    check("...and the two describe the same scene",
          all(near(sc_a["collector_rotations"][i][0],
                   sc_b["collector_rotations"][i][0]) for i in (0, 1)))
    check("a zero_deg joint round trips through to_dict",
          mm.RotaryAxis.from_dict(theirs.rotaries[1].to_dict()).zero_deg == 90.0)
    check("...and a joint homed at 0 writes no zero_deg at all",
          "zero_deg" not in ours.rotaries[1].to_dict())
    # the whole round trip, off the apex, where the tilt is not a special case
    worst_pos = worst_n = 0.0
    for polar in (12.0, 47.0, 78.0):
        for azim in (0.0, 214.0):
            q, m = _dome_point_and_normal(10.0, polar, azim)
            dp, dn = _five_axis_miss(theirs, q, m)
            worst_pos, worst_n = max(worst_pos, dp), max(worst_n, dn)
    check("a homed-at-90 rig still puts the point under the nozzle",
          worst_pos < 1e-9 and worst_n < 1e-9,
          "pos %.2e mm, normal %.2e" % (worst_pos, worst_n))


# --------------------------------------------------------------------------- #
# THE TWO ROTARY BODIES. A five-axis table is not two abstract joints: it is a
# tilt cradle with a spinning table bolted to it, each an STL of its own, each
# turning about a line the user PICKED on it. The comparator here is this file's
# own Rodrigues (`_rot_about`) plus two facts that need no reference at all -- a
# point ON an axis does not move, and two bolted bodies stay rigid -- never
# machine_model's own composition.
# --------------------------------------------------------------------------- #
def _table(pick_c=(20.0, -5.0, 40.0), pick_a=(60.0, -5.0, 12.0), c_pivot=True):
    """A C table on an A cradle, both bodies picked in one shared STL frame --
    the shape the development rig's five exported bodies have."""
    c = {"word": "C", "role": "bed", "axis": "z", "stl": "c.stl"}
    if c_pivot:
        c["pivot"] = list(pick_c)
    return mm.MachineProfile.from_dict(
        {"parts": {"head": "h.stl", "bed": "b.stl"},
         "kinematics": "mewron", "tip": [0, 0, 0], "standoff": 1.0,
         "rotaries": [c, {"word": "A", "role": "bed", "axis": "x",
                          "stl": "a.stl", "pivot": list(pick_a)}]})


def _body(sc, i, base="bed"):
    """(R, t) a rotary body is drawn by, composed HERE from the reported triples
    with this file's own rotation, then translated by the part it is bolted to."""
    r = np.eye(3)
    t = np.zeros(3)
    for ang, axis, piv in sc["rotary_bodies"][i]:
        piv = np.asarray(piv, float)
        rj = np.column_stack([_rot_about(axis, ang, np.eye(3)[k])[0]
                              for k in range(3)])
        r = rj @ r
        t = rj @ (t - piv) + piv
    return r, t + np.asarray(sc[base], float)


def test_each_rotary_body_turns_about_its_own_picked_line():
    p = _table()
    pick_c = np.array([20.0, -5.0, 40.0])
    pick_a = np.array([60.0, -5.0, 12.0])
    sc0 = mm.place_scene(p, p.tip, np.zeros(3), rotary_deg=(0.0, 0.0))
    check("two joints -> two body chains", len(sc0["rotary_bodies"]) == 2)
    r, t = _body(sc0, 0)
    check("at zero neither body is rotated",
          near(r, np.eye(3)) and near(t, sc0["bed"]))

    # C alone: the C TABLE turns, the A cradle under it does not.
    sc = mm.place_scene(p, p.tip, np.zeros(3), rotary_deg=(37.0, 0.0))
    rc, tc = _body(sc, 0)
    ra, ta = _body(sc, 1)
    check("commanding C leaves the A cradle exactly where it was",
          near(ra, np.eye(3)) and near(ta, sc0["bed"]))
    check("...and turns the C table by C, about +z",
          near(rc @ np.array([1.0, 0.0, 0.0]),
               _rot_about((0, 0, 1), 37.0, [1.0, 0.0, 0.0])[0]))
    check("...about the point picked on the C body, which does not move",
          near(rc @ pick_c + tc, pick_c + sc0["bed"]))

    # A alone: the cradle tilts and the table rides it, rigidly.
    sc = mm.place_scene(p, p.tip, np.zeros(3), rotary_deg=(0.0, 23.0))
    rc, tc = _body(sc, 0)
    ra, ta = _body(sc, 1)
    check("commanding A tilts the cradle about the point picked on IT",
          near(ra @ pick_a + ta, pick_a + sc0["bed"]))
    check("...and the C table rides it RIGIDLY (same R, same t)",
          near(rc, ra) and near(tc, ta))
    check("...so the C body's own pick moves exactly as the cradle carries it",
          near(rc @ pick_c + tc,
               _rot_about((1, 0, 0), 23.0, pick_c - pick_a)[0] + pick_a
               + sc0["bed"]))


def test_the_collector_turns_on_the_same_lines_its_body_does():
    """The pivot relativization, stated as the only thing it is for.

    `rotaries[0].pivot` is a point in the C BODY's own file; the collector's
    origin sits on that same axis. So the two chains this module reports must be
    the SAME rotations, differing only by that one offset -- the C table and the
    dome bolted to it can then never shear apart, whatever the seating does with
    them afterwards. (That the seating actually brings the two into register is a
    SCENE fact, not this module's: it is measured on the development rig's real
    bodies.) The RED control is the arithmetic this
    replaced: reporting the collector's pivots as the picked points themselves."""
    pick_c = np.array([20.0, -5.0, 40.0])
    pick_a = np.array([60.0, -5.0, 12.0])
    p = _table()
    for readings in ((44.0, 0.0), (0.0, 31.0), (44.0, 31.0)):
        sc = mm.place_scene(p, p.tip, np.zeros(3), rotary_deg=readings)
        # the JOINT links only: the chain leads with the mount, which turns the
        # collector's own frame onto the spin axis and has no body counterpart
        coll = sc["collector_rotations"][-2:]
        body = sc["rotary_bodies"][0]
        same = all(a1 == a2 and near(x1, x2)
                   for (a1, x1, _p1), (a2, x2, _p2) in zip(coll, body))
        check("same angles about the same axes at C,A = %s" % (readings,), same)
        check("...and every collector pivot is its body pivot less the C pick",
              all(near(pc, pb - pick_c)
                  for (_a, _x, pc), (_b, _y, pb) in zip(coll, body)))
    check("so the collector's OWN joint turns about the collector's origin",
          near(mm.place_scene(p, p.tip, np.zeros(3),
                              rotary_deg=(44.0, 0.0))["collector_rotations"][-2][2],
               np.zeros(3)))
    check("...and the tilt under it about the real axis-to-axis offset",
          near(mm.place_scene(p, p.tip, np.zeros(3),
                              rotary_deg=(0.0, 31.0))["collector_rotations"][-1][2],
               pick_a - pick_c))
    check("RED CONTROL: reporting the picked points themselves would put the "
          "collector's own joint off its own origin",
          not near(pick_c, np.zeros(3)))


def test_an_unpicked_joint_turns_no_body_and_a_mandrel_holder_never_moves():
    """No datum, no rotation -- and that is what keeps four-axis untouched.

    A joint with no picked pivot has no line to turn a body about, so turning it
    about the STL's origin instead would fling the body across the scene at a
    plausible angle. Nothing shipped carries a pivot, so every mandrel holder's
    chain is empty and it is placed by the SetPosition it always had."""
    p = _table(c_pivot=False)
    sc = mm.place_scene(p, p.tip, np.zeros(3), rotary_deg=(90.0, 0.0))
    r, t = _body(sc, 0)
    check("an unpicked joint turns its own body not at all, at 90 degrees",
          near(r, np.eye(3)) and near(t, sc["bed"]))
    check("...and appears in nobody's chain",
          all(not near(piv, [20.0, -5.0, 40.0])
              for chain in sc["rotary_bodies"] for _a, _x, piv in chain))
    sc = mm.place_scene(p, p.tip, np.zeros(3), rotary_deg=(0.0, 45.0))
    r, _t = _body(sc, 0)
    check("...but a PICKED joint under it still carries that body",
          not near(r, np.eye(3)))

    # A SCALED PROFILE WITH A PICKED PIVOT IS REFUSED, not approximated. The
    # body chain scales its pivots (the body is drawn in STL coordinates) while
    # the collector chain cannot -- it is the arithmetic the generator wrote the
    # file with and the parser reads it back with, and neither has a scale. One
    # number cannot be right in both unless scale is 1.
    scaled = {"parts": {"head": "h.stl", "bed": "b.stl"}, "tip": [0, 0, 0],
              "scale": 0.5,
              "rotaries": [{"word": "C", "role": "bed", "axis": "z",
                            "stl": "c.stl", "pivot": [20.0, -5.0, 40.0]},
                           {"word": "A", "role": "bed", "axis": "x",
                            "stl": "a.stl", "pivot": [60.0, -5.0, 12.0]}]}
    try:
        mm.MachineProfile.from_dict(scaled)
        refused = False
    except ValueError:
        refused = True
    check("a scaled profile carrying a picked pivot is refused", refused)
    unscaled = dict(scaled, scale=1.0)
    sc = mm.place_scene(mm.MachineProfile.from_dict(unscaled), np.zeros(3),
                        np.zeros(3), rotary_deg=(10.0, 20.0))
    check("CONTROL: the same profile at scale 1 loads and places",
          near(sc["rotary_bodies"][0][1][2], [60.0, -5.0, 12.0]))
    no_pivot = dict(scaled, rotaries=[{"word": "C", "role": "bed", "axis": "z"},
                                      {"word": "A", "role": "bed", "axis": "x"}])
    check("...and a scaled profile with NO pivot is untouched",
          mm.MachineProfile.from_dict(no_pivot).scale == 0.5)

    one = mm.MachineProfile.from_dict(
        {"parts": {"head": "h.stl", "bed": "b.stl"}, "tip": [0, 0, 0],
         "rotary": {"word": "A", "role": "bed", "radius": 3.0,
                    "stl": "chuck.stl"}})
    sc = mm.place_scene(one, one.tip, np.zeros(3), rotary_deg=250.0)
    check("a mandrel's holder chain is empty at any reading",
          sc["rotary_bodies"] == [[]])
    check("...and the holder still resolves as a part",
          one.rotary_paths == (one._resolve("chuck.stl"),))


def test_a_picked_pivot_at_the_origin_is_still_a_datum():
    """`pivot_set`, and why `any(pivot)` was not enough. A pick that lands on the
    STL origin is a real calibration; reading it as "never calibrated" is the
    plausible-blank that cost `rotary.axis_height` its existence."""
    j = mm.RotaryAxis.from_dict({"word": "C", "axis": "z", "pivot": [0, 0, 0]})
    check("a zero pick is remembered", j.pivot_set)
    check("...and round trips", j.to_dict().get("pivot") == [0.0, 0.0, 0.0])
    j2 = mm.RotaryAxis.from_dict({"word": "A"})
    check("a pivot-less joint still writes none",
          not j2.pivot_set and "pivot" not in j2.to_dict())
    check("...and collector_datum tells the two apart",
          _table(c_pivot=False).collector_datum is None
          and near(_table().collector_datum, [20.0, -5.0, 40.0]))


def test_a_picked_home_is_carried_and_is_not_the_pivot():
    """`home` -- where the nozzle tip is at g-code (0, 0, 0), picked on the
    collector-carrying joint's own STL.

    On the five-axis Machine page the homed position could not be calibrated
    (2026-09-09), so for now the home is a point picked on the C axis. It rides `pivot`'s
    convention -- a point, in that joint's own STL frame -- rather than
    declaring a second one, and it is a SEPARATE fact from the pivot: the pivot
    says where the collector is bolted, the home says where the nozzle starts.
    A profile that confused the two would still pass every check above.
    """
    j = mm.RotaryAxis.from_dict({"word": "C", "axis": "z",
                                 "pivot": [1.0, 2.0, 3.0],
                                 "home": [1.0, 2.0, 9.0]})
    check("a picked home is remembered and is not the pivot",
          j.home_set and near(j.home, [1.0, 2.0, 9.0])
          and not near(j.home, j.pivot))
    check("...and round trips through to_dict",
          j.to_dict().get("home") == [1.0, 2.0, 9.0])
    check("a home picked at the origin is still a calibration",
          mm.RotaryAxis.from_dict({"word": "C", "axis": "z",
                                   "home": [0, 0, 0]}).home_set)
    j2 = mm.RotaryAxis.from_dict({"word": "A"})
    check("CONTROL: an unpicked joint writes no home at all",
          not j2.home_set and "home" not in j2.to_dict())
    # and the profile-level accessor tells the two states apart, the way
    # `collector_datum` does -- `home_set`, never `any(home)`.
    t = _table()
    check("collector_home is None until something is picked",
          t.collector_home is None)
    t.rotaries[0].home_set = True
    t.rotaries[0].home = np.array([20.0, -5.0, 61.0])
    check("...and then it is the pick, not the pivot",
          near(t.collector_home, [20.0, -5.0, 61.0])
          and near(t.collector_datum, [20.0, -5.0, 40.0]))


def test_the_home_pick_is_a_nozzle_tip_and_is_reduced_by_its_standoff():
    """The g-code home is picked with the NOZZLE, so what g-code (0, 0, 0)
    names is one working distance BELOW it.

    The pick is made hovering one standoff (3.5 mm) up, which is
    how a Z zero is set on any print. Every other
    datum in this program is a SURFACE (the plate's top face, the mandrel's
    axis) and `place_scene` adds the standoff to it, so a tip pick has to be
    reduced exactly once or the whole file sits one standoff high.

    `home_standoff` travels WITH the pick rather than being read live off the
    profile, so editing the standoff field cannot silently re-interpret an old
    calibration. Zero -- every profile written before the key existed -- is the
    pick read as a surface point, unchanged.
    """
    def joint(hs):
        d = {"word": "C", "axis": "z", "pivot": [0.0, 0.0, 0.0],
             "home": [4.0, -2.0, 20.0]}
        if hs is not None:
            d["home_standoff"] = hs
        return mm.RotaryAxis.from_dict(d)

    tilt = mm.RotaryAxis.from_dict({"word": "A", "axis": "x",
                                    "pivot": [0.0, 0.0, -8.0]})
    lifted = (joint(3.5), tilt)
    flat = (joint(None), tilt)
    off_l = mm.collector_home_offset(lifted)
    off_f = mm.collector_home_offset(flat)
    check("the standoff comes off the pick, in Z only",
          near(off_f - off_l, [0.0, 0.0, 3.5]),
          "difference %s" % (np.round(off_f - off_l, 6),))
    check("...and it is the pick less the working distance",
          near(off_l, [4.0, -2.0, 16.5]), "%s" % (np.round(off_l, 6),))
    check("CONTROL: with no home_standoff the pick is taken as-is",
          near(off_f, [4.0, -2.0, 20.0]), "%s" % (np.round(off_f, 6),))
    # the profile-level accessors are the two halves of the same fact
    p = mm.MachineProfile.from_dict(
        {"parts": {"head": "h.stl"}, "kinematics": "mewron", "standoff": 3.5,
         "rotaries": [joint(3.5).to_dict(), tilt.to_dict()]})
    check("collector_home reports the DEPOSITION point",
          near(p.collector_home, [4.0, -2.0, 16.5]),
          "%s" % (np.round(p.collector_home, 6),))
    check("...and raw_collector_home reports the operator's own number",
          near(p.raw_collector_home, [4.0, -2.0, 20.0]))
    check("...and home_standoff round trips through to_dict",
          p.rotaries[0].to_dict().get("home_standoff") == 3.5)


def test_the_collector_stands_on_its_picked_seat():
    """`collector_origin` -- datum 1 of the two every printing mode needs.

    That datum is where the collector (the thing being printed on)
    sits relative to whatever holds it: the bed for 3 axis, the mandrel mount
    for 4 axis, the C axis for 5 axis.

    THE PIVOT IS NOT THAT POINT and using it as both is the defect this
    replaces. A pivot is a point on the axis of ROTATION; a collector STANDS on
    a face. `mount_axial` is the collector's own axial coordinate at its bottom
    edge, so the origin is the seat walked back up the joint's axis by it -- and
    on a dome that origin is the centre of curvature, a point in mid-air inside
    the part, which is exactly why nailing it to the axis point left the rim
    hanging.

    The comparator is the DATUM ITSELF: whatever `collector_origin` returns, the
    point `mount_axial` further up the joint's axis has to be that datum again.
    That is a different statement from the formula, not a restatement of it,
    because it is asked in the direction the physical part is measured in.

    AND IT HOLDS ON AN UNCALIBRATED RIG TOO. This guard used to
    assert the opposite -- that with no seat the origin is the bare pivot -- and
    that is the misplacement, not a baseline: `mount_axial` describes the
    COLLECTOR, so leaving it out on the uncalibrated path nailed the part's
    construction origin (mid-air, inside it) to the axis point. RED before the
    fix, on the rig's own preset through the player's actor matrices
    (`_scratch_mount/probe_mount.py`): a bowl R=10 span 0-12 hung 10.000 mm
    BELOW the pivot, a dome of the same size 3.624 mm above it and a torus
    1.411 mm above it; all three now read a 0.0000 mm gap, seated and unseated.
    Reported as *"the bowl collector is under the c axis"*, with no seat picked.
    """
    t = _table()                                   # C axis is z, pivot z = 40
    ma = 3.6236                                    # an R=10 dome, span 0-12 mm
    o0 = t.collector_origin(ma)
    check("with no seat, the origin is the PIVOT less the collector's own edge",
          near(o0, [20.0, -5.0, 40.0 - ma]))
    check("...so the bottom edge lands on the pivot, not the mid-air origin",
          near(o0 + np.array([0.0, 0.0, ma]), [20.0, -5.0, 40.0]))
    t.rotaries[0].seat_set = True
    t.rotaries[0].seat_point = np.array([20.0, -5.0, 61.0])
    o = t.collector_origin(ma)
    check("with a seat, the origin is the seat less the collector's own edge",
          near(o, [20.0, -5.0, 61.0 - ma]))
    check("...so the bottom edge lands BACK on the seat",
          near(o + np.array([0.0, 0.0, ma]), [20.0, -5.0, 61.0]))
    check("CONTROL: without the collector's number it cannot be honoured",
          near(t.collector_origin(None), [20.0, -5.0, 40.0]))
    # OFF-CENTRE IS REPRESENTABLE, deliberately: a collector bolted off the spin
    # axis really does orbit it. What says so is the PERPENDICULAR part of joint
    # 0's relative pivot -- a component along the spin axis moves nothing,
    # because a point on the axis is fixed by the rotation, so the in-plane part
    # is the whole claim.
    def _perp(v):
        return np.asarray(v, float)[:2]             # joint 0 spins about z

    t.rotaries[0].seat_point = np.array([23.0, -5.0, 61.0])
    check("a seat 3 mm off the spin axis makes joint 0 orbit by 3 mm",
          near(_perp(mm._relative_pivots(t.rotaries, ma)[0]), [-3.0, 0.0]))
    t.rotaries[0].seat_point = np.array([20.0, -5.0, 61.0])
    check("...and a concentric seat leaves it ON the axis",
          near(_perp(mm._relative_pivots(t.rotaries, ma)[0]), [0.0, 0.0]))


def test_the_exported_zero_is_the_picked_home():
    """`rotary_transform` returns EXPORTED coordinates, and g-code (0, 0, 0) is
    the picked home -- datum 2.

    That datum is where the nozzle is zeroed relative to the
    collector, the origin of the g-code. It is often at the edge of the
    collector or off it entirely, so the travel fibre does not cross an
    important part of the print. So it is a FREE pick, not a
    point on the spin axis, and the player used to keep only its Z on the
    reasoning that its XY was the pivot's.

    The subtraction is LAST, after the rotation, because the zero is a constant
    in the machine frame: you jog the nozzle to a spot with the rotaries at zero
    and datum there, and turning a rotary afterwards does not move it.
    """
    ma = 3.6236
    t = _table()
    t.rotaries[0].seat_set = True
    t.rotaries[0].seat_point = np.array([20.0, -5.0, 61.0])
    r0, t0 = mm.rotary_transform(t.rotaries, (0.0, 0.0), ma)
    check("with no home, nothing is subtracted", near(t0, np.zeros(3)))
    # a home 7 mm off the axis in x and 2 mm up
    t.rotaries[0].home_set = True
    t.rotaries[0].home = np.array([27.0, -5.0, 63.0])
    r1, t1 = mm.rotary_transform(t.rotaries, (0.0, 0.0), ma)
    check("the rotation is untouched by the zero", near(r1, r0))
    check("...and the translation moved by exactly home - origin",
          near(t1 - t0, -(np.array([27.0, -5.0, 63.0])
                          - t.collector_origin(ma))))
    # THE POINT OF IT, asked against an INDEPENDENT quantity. Adding
    # `collector_home_offset` back to the same transform it was subtracted from
    # is algebra, not a measurement -- it comes out `home` whatever the sign or
    # the order. So the machine position of a collector point is rebuilt from
    # the UNHOMED transform (which is the frame the STL picks are made in) and
    # the file's zero is solved for in the HOMED one. A subtraction with the
    # wrong sign, or applied before the rotation, separates the two.
    def _machine(pt, angles):
        """Where a collector point actually is, in joint 0's STL frame."""
        t.rotaries[0].home_set = False
        rn, tn = mm.rotary_transform(t.rotaries, angles, ma)
        t.rotaries[0].home_set = True
        return t.collector_origin(ma) + rn @ np.asarray(pt, float) + tn

    for angles in ((0.0, 0.0), (37.0, -11.0), (-98.0, 63.0)):
        rh, th = mm.rotary_transform(t.rotaries, angles, ma)
        zero_pt = np.linalg.solve(rh, -th)          # the point the file calls 0
        check("g-code (0,0,0) IS the picked home at C%.0f A%.0f" % angles,
              near(_machine(zero_pt, angles), [27.0, -5.0, 63.0]))
    # ...and it is a real map, not the identity: an arbitrary collector point
    # exports as its machine position less the home.
    for angles in ((0.0, 0.0), (37.0, -11.0)):
        rh, th = mm.rotary_transform(t.rotaries, angles, ma)
        pt = np.array([2.0, -3.5, 7.25])
        check("...and any point exports as machine position less home "
              "(C%.0f A%.0f)" % angles,
              near(rh @ pt + th,
                   _machine(pt, angles) - np.array([27.0, -5.0, 63.0])))
    # ...and turning a joint does not move it, which is what "after the
    # rotation" means and what "before" would have got wrong.
    _r2, t2 = mm.rotary_transform(t.rotaries, (37.0, -11.0), ma)
    _r3, t3 = mm.rotary_transform(t.rotaries, (37.0, -11.0), ma)
    off = mm.collector_home_offset(t.rotaries, ma)
    t.rotaries[0].home_set = False
    _r4, t4 = mm.rotary_transform(t.rotaries, (37.0, -11.0), ma)
    t.rotaries[0].home_set = True
    check("a turned pair takes the SAME constant zero, not a rotated one",
          near(t2, t3) and near(t2, t4 - off))
    # DATUM 1 AND DATUM 2 ARE INDEPENDENT: moving the seat must not move the
    # file's zero. `collector_home_offset` is measured against the origin, so
    # both terms move together and cancel -- a property, not a coincidence.
    t.rotaries[0].seat_point = np.array([20.0, -5.0, 86.0])
    rr, tt = mm.rotary_transform(t.rotaries, (0.0, 0.0), ma)
    zero_pt = np.linalg.solve(rr, -tt)
    check("moving the seat 25 mm does not move the g-code zero",
          near(_machine(zero_pt, (0.0, 0.0)), [27.0, -5.0, 63.0]))
    # RED CONTROL, so the line above is a measurement and not an identity:
    # moving the HOME does move it, by exactly the amount it moved.
    t.rotaries[0].home = np.array([27.0, -5.0, 71.0])
    rr, tt = mm.rotary_transform(t.rotaries, (0.0, 0.0), ma)
    check("CONTROL: moving the home DOES move the g-code zero",
          near(_machine(np.linalg.solve(rr, -tt), (0.0, 0.0)),
               [27.0, -5.0, 71.0]))


def test_a_mandrel_ignores_the_collector_datums_entirely():
    """A rod is gripped at its ends and its placement is `RotaryAxis.seat`,
    which none of this touches. The four-axis path must be BYTE-identical, so
    the guard is that handing it a `mount_axial` changes nothing at all."""
    m = mm.MachineProfile.from_dict(
        {"parts": {"head": "h.stl", "bed": "b.stl"}, "kinematics": "mewron",
         "tip": [0, 0, 0], "standoff": 1.0,
         "rotary": {"word": "A", "role": "bed", "radius": 1.5, "length": 30.0}})
    for ang in (0.0, 33.0, -117.0):
        a = mm.rotary_transform(m.rotaries, (ang,), 3.6236)
        b = mm.rotary_transform(m.rotaries, (ang,), None)
        check("a mandrel at %.0f deg ignores mount_axial" % ang,
              near(a[0], b[0]) and near(a[1], b[1]))
    check("...and its collector origin is the zeros it always was",
          near(mm.collector_origin(m.rotaries, 3.6236), np.zeros(3)))
    # A SEAT ON A ONE-JOINT RIG IS IGNORED, and this is the case that makes the
    # `len(js) < 2` term in `collector_origin` load-bearing rather than dead.
    # Nothing in the GUI writes one -- the seat row is pair-only -- but a
    # hand-written machine.json can, and a rod seated by a dome's bottom edge
    # would be placed by a number belonging to a collector it does not have. Its
    # placement is `RotaryAxis.seat`, the rod radius, untouched by any of this.
    m.rotaries[0].seat_set = True
    m.rotaries[0].seat_point = np.array([9.0, -4.0, 31.0])
    check("a seat on a MANDREL joint is ignored, not applied",
          near(mm.collector_origin(m.rotaries, 3.6236), np.zeros(3)))
    check("...and its transform is still the one it always had",
          near(mm.rotary_transform(m.rotaries, (33.0,), 3.6236)[1],
               mm.rotary_transform(m.rotaries, (33.0,), None)[1]))
    m.rotaries[0].seat_set = False
    # CONTROL, so the check above is not vacuous: on a PAIR with a seat, the
    # same argument does move the answer.
    t = _table()
    t.rotaries[0].seat_set = True
    t.rotaries[0].seat_point = np.array([20.0, -5.0, 61.0])
    check("CONTROL: on a seated pair, mount_axial does move it",
          not near(mm.collector_origin(t.rotaries, 3.6236),
                   mm.collector_origin(t.rotaries, None)))



def test_a_pair_is_not_seated_and_a_mandrel_still_is():
    """The two collectors are seated by different rules, and the rule for each
    is the one its own g-code was written with.

    `five_axis_command` takes the seat term ONLY when there is one joint, so a
    pair's exported file already measures from the collector's own ORIGIN --
    which lies on the collector-carrying joint's axis, i.e. at the point picked
    on the spinning body. Seating one here would move the drawn collector off
    the coordinates its own file is written in, by exactly a radius. (Slice 1
    briefly did: it gave `place_scene` a `collector_seat=` argument taken from
    the frame, and slice 4 removed it when the probe measured the two sides
    disagreeing.) A mandrel's `-R0` is the other case and is unchanged."""
    p = _table()
    sc = mm.place_scene(p, p.tip, np.zeros(3), rotary_deg=(0.0, 0.0))
    check("a pair reports no seat at all", "collector_seat" not in sc)
    flat = mm.place_scene(
        mm.MachineProfile.from_dict(
            {"parts": {"head": "h.stl", "bed": "b.stl"}, "tip": [0, 0, 0]}),
        np.zeros(3), np.zeros(3))
    check("...so its build group is the unseated one, to the bit",
          _bytes_eq(sc["bed_group"], flat["bed_group"]))
    one = mm.MachineProfile.from_dict(
        {"parts": {"head": "h.stl", "bed": "b.stl"}, "tip": [0, 0, 0],
         "rotary": {"word": "A", "role": "bed", "radius": 3.0}})
    m = mm.place_scene(one, one.tip, np.zeros(3), rotary_deg=0.0)
    check("a MANDREL is still seated by its own rod radius",
          near(m["collector_seat"], one.rotary.seat())
          and near(m["bed_group"], flat["bed_group"] + one.rotary.seat()))
    check("...and that is a real displacement, so the two cases differ",
          not near(m["bed_group"], sc["bed_group"]))


def _bytes_eq(a, b):
    return np.asarray(a, float).tobytes() == np.asarray(b, float).tobytes()


def test_the_seating_reaches_every_body_on_the_collector_chain():
    """A plus-sign stage: the X half rides the Y half. Seating the collector must
    move the half that DRIVES each axis, not dump both onto the leaf -- otherwise
    the lower body is drawn off its own rails by the other axis's seating, and
    every body bolted to it goes with it. The RED control recomputes what the
    expression this replaced produced: `coll_off` read back on `bed` alone."""
    p = mm.MachineProfile.from_dict(
        {"parts": {"head": "h.stl", "gantry": "g.stl", "bed": "b.stl"},
         "kinematics": "custom", "tip": [0, 0, 0], "standoff": 1.0,
         "axes": {"head": ["z"], "gantry": ["y"], "bed": ["x"]},
         "parents": {"head": "frame", "gantry": "frame", "bed": "gantry"}})
    home = mm.place_scene(p, p.tip, np.zeros(3), registration=(0.0, 0.0))
    sc = mm.place_scene(p, p.tip, np.zeros(3), registration=(7.0, -13.0))
    dg = np.asarray(sc["gantry"]) - home["gantry"]
    db = np.asarray(sc["bed"]) - home["bed"]
    check("the Y half takes the whole Y seating and no X",
          near(dg, [0.0, 13.0, 0.0]))
    check("the X half takes that PLUS its own X seating",
          near(db, [-7.0, 13.0, 0.0]))
    check("...so their relative pose changes ONLY along the axis the X half "
          "drives", near(db - dg, [-7.0, 0.0, 0.0]))
    check("RED CONTROL: with the Y half taking none of it, the two halves move "
          "apart in Y as well -- the body drawn off its own rails",
          not near(db - np.zeros(3), [-7.0, 0.0, 0.0]))
    single = mm.MachineProfile.from_dict(
        {"parts": {"head": "h.stl", "gantry": "g.stl", "bed": "b.stl"},
         "kinematics": "mewron", "tip": [0, 0, 0]})
    s0 = mm.place_scene(single, single.tip, np.zeros(3), registration=(0.0, 0.0))
    s1 = mm.place_scene(single, single.tip, np.zeros(3), registration=(4.0, 5.0))
    # On the MEWron the collector chain is `["bed"]` alone and the gantry is on
    # the TOOL chain, so `coll_off` has no gantry entry to subtract and the
    # gantry keeps exactly the tool seating it always took: its own Y, no X.
    check("a single-body collector chain (every shipped rig) is untouched",
          near(np.asarray(s1["gantry"]) - s0["gantry"], [0.0, 5.0, 0.0])
          and near(np.asarray(s1["head"]) - s0["head"], [4.0, 5.0, 0.0])
          and near(np.asarray(s1["bed"]) - s0["bed"], [0.0, 0.0, 0.0]))


# --------------------------------------------------------------------------- #
# THE MOUNT. A collector is a body of revolution with an axis of its own, and a
# table has a spin axis; bolting one to the other makes them the same line. The
# collector's axis is X in its own coordinates and the table's is whatever the
# rig says, so a rotation stands between them -- and it went missing, which is
# how the dome came to sit perpendicular to the C axis it is bolted to.
# --------------------------------------------------------------------------- #
def test_the_collector_axis_constant_is_the_one_mew_frame_actually_builds():
    """`COLLECTOR_AXIS` is a copy of a convention that lives in another module,
    so it is READ BACK from that module rather than trusted.

    `machine_model` imports no frame on purpose (pure geometry), which is what
    makes the constant necessary and what makes this check necessary with it.
    The comparator is `mew_frame`'s own output: sweep the meridian and see which
    world coordinate moves, and sweep the parallel and see which two do."""
    frame = mew_frame.CylinderFrame(radius=4.0)
    u = np.array([0.0, 3.0, 7.0])
    z = np.zeros(3)
    along = np.column_stack(frame.to_world(u, z, z))
    moved = [i for i in range(3)
             if float(np.ptp(along[:, i])) > 1e-9]
    check("the meridian moves exactly one world axis", len(moved) == 1)
    check("...and it is the one `machine_model.COLLECTOR_AXIS` names",
          mm.AXES[moved[0]] == mm.COLLECTOR_AXIS,
          "mew_frame says %s, machine_model says %s"
          % (mm.AXES[moved[0]], mm.COLLECTOR_AXIS))
    v = np.array([0.0, 1.0, 2.0])
    around = np.column_stack(frame.to_world(z, v, z))
    spun = [i for i in range(3) if float(np.ptp(around[:, i])) > 1e-9]
    check("...and the parallels lie in the other two",
          sorted(spun) == sorted(i for i in range(3) if i != moved[0]))


def test_the_mount_brings_the_collector_axis_onto_the_spin_axis():
    def pair(j0_axis):
        return mm.MachineProfile.from_dict(
            {"parts": {"head": "h.stl", "bed": "b.stl"}, "tip": [0, 0, 0],
             "standoff": 1.0,
             "rotaries": [{"word": "C", "role": "bed", "axis": j0_axis},
                          {"word": "A", "role": "bed", "axis": "x"
                           if j0_axis != "x" else "y"}]})

    coll = np.zeros(3)
    coll[mm.AXES.index(mm.COLLECTOR_AXIS)] = 1.0
    for ax in ("x", "y", "z"):
        p = pair(ax)
        want = np.zeros(3)
        want[mm.AXES.index(ax)] = 1.0
        check("the mount carries the collector's axis onto a %s spin axis" % ax,
              near(mm.collector_mount(p.rotaries) @ coll, want))
    check("a pair already mounted on the collector's own axis needs no mount",
          near(mm.collector_mount(pair("x").rotaries), np.eye(3))
          and mm.collector_mount_triple(pair("x").rotaries) is None)
    check("...and neither does a MANDREL, whatever its word",
          all(near(mm.collector_mount(
              (mm.RotaryAxis(word=w, axis=a),)), np.eye(3))
              for w, a in (("A", "x"), ("B", "y"), ("C", "z"))))
    # the triple and the matrix are one rotation described twice; they must not
    # be two rotations that happen to agree today
    for ax in ("y", "z"):
        p = pair(ax)
        check("the reported mount triple IS the mount matrix (%s)" % ax,
              near(mm.compose_rotations([mm.collector_mount_triple(p.rotaries)])[0],
                   mm.collector_mount(p.rotaries)))


def test_a_dome_on_a_vertical_spindle_comes_up_under_the_nozzle():
    """The dome-on-its-side defect, as a check.

    A dome bolted to a table that spins about Z. Its own frame is X-axial, so
    without the mount its apex normal points along the machine's X and no
    reading brings it up -- the dome stands on its side, "perpendicular to the C
    axis". The RED control is that same solve with the mount taken away, run
    through this file's own rotation, and it must MISS.
    """
    p = mm.MachineProfile.from_dict(
        {"parts": {"head": "h.stl", "bed": "b.stl"}, "tip": [0, 0, 0],
         "standoff": 1.0,
         "rotaries": [{"word": "C", "role": "bed", "axis": "z"},
                      {"word": "A", "role": "bed", "axis": "x",
                       "pivot": [0, 0, -8.0]}]})
    worst_pos = worst_n = 0.0
    for polar in (0.0, 17.0, 52.0):
        for azim in (0.0, 143.0):
            q, n = _dome_point_and_normal(10.0, polar, azim)
            dp, dn = _five_axis_miss(p, q, n)
            worst_pos, worst_n = max(worst_pos, dp), max(worst_n, dn)
    check("every point on an X-axial dome comes up under the nozzle on a "
          "Z-spinning table", worst_pos < 1e-9 and worst_n < 1e-9,
          "pos %.2e mm, normal %.2e" % (worst_pos, worst_n))
    # RED CONTROL: drop the mount link from the reported chain and re-run the
    # SAME readings. The apex is the clearest case -- its normal is the
    # collector's own axis, so with no mount it stays pointing along machine X.
    q, n = _dome_point_and_normal(10.0, 0.0, 0.0)
    xyz, sol = mm.five_axis_command(p, q, n)
    sc = mm.place_scene(p, [0, 0, 0], xyz, rotary_deg=sol)
    no_mount = sc["collector_rotations"][-2:]
    up = _apply_reported(no_mount, n, is_point=False)
    check("RED CONTROL: without the mount the dome's apex normal is not up",
          float(np.abs(up - [0, 0, 1]).max()) > 0.5,
          "apex normal came out %s" % np.round(up, 6).tolist())
    check("...and it is lying along the machine's X, which is the symptom",
          near(np.abs(up), [1, 0, 0], 1e-9))


def main():
    tests = [test_a_malformed_machine_json_names_the_file,
             test_profile_from_dict,test_profile_defaults_and_paths,
             test_absolute_path_kept, test_slide_offset_and_limits_parse,
             test_hidden_parts_parse,
             test_find_tip, test_find_bed_top_face,
             test_place_frame_a_tip_tracking, test_place_frame_a_chain_assembled,
             test_place_frame_a_bed_only_z, test_place_frame_b,
             test_scale_and_offsets,
             test_scene_default_matches_place,
             test_scene_printhead_over_scaffold_always,
             test_scene_static_and_bed_fixed_under_registration,
             test_scene_build_group_rides_registration,
             test_scene_printhead_tracks_build_in_plane,
             test_scene_collector_grounded_to_registration,
             test_scene_origin_triad_on_collector_corner,
             test_scene_origin_toggle_moves_nothing_but_triad,
             test_scene_assembly_intact,
             test_scene_combos_printhead_always_over_print,
             test_scene_elevation_bake_neutral_to_sim,
             test_scene_collector_sits_on_bed_z,
             test_motion_limit_clamp,
             test_motion_limit_clamps_registration_too,
             test_slide_registration_seats_collector_on_bed,
             test_generalized_nozzle_over_print,
             test_legacy_equals_explicit_mewron,
             test_xyz_head_static_collector,
             test_xy_bed_z_head_collector_tracks_xy,
             test_bed_rides_gantry_carriage,
             test_axis_side, test_orphaned_axes_custom_chain,
             test_contact_alignment_bed_vs_tool,
             test_prusa_fixed_bed_drops_tool_not_plate,
             test_prusa_layers_rise_bed_fixed,
             test_prusa_nozzle_over_print_sweep,
             test_prusa_seating_slides_bed_not_gantry,
             test_mewron_contact_unchanged,
             test_rotary_absent_is_the_old_scene,
             test_rotary_schema_round_trip,
             test_rotary_sign_puts_the_fresh_fibre_under_the_nozzle,
             test_rotary_nozzle_sits_one_standoff_above_the_SURFACE,
             test_rotary_body_is_placed_not_decorated,
             test_rotary_leaves_the_part_kinematics_alone,
             test_five_axis_pair_parses_and_keeps_its_order,
             test_five_axis_puts_the_nozzle_on_the_normal_of_a_DOME,
             test_five_axis_puts_the_nozzle_on_the_normal_of_a_TORUS,
             test_CONTROL_the_round_trip_can_fail,
             test_the_four_axis_solve_reproduces_the_shipped_sign,
             test_both_five_axis_solutions_are_real,
             test_five_axis_refuses_rather_than_approximates,
             test_a_pair_emits_no_mandrel_keys_and_one_joint_emits_all_of_them,
             test_a_joint_can_read_something_other_than_zero_at_zero,
             test_each_rotary_body_turns_about_its_own_picked_line,
             test_the_collector_turns_on_the_same_lines_its_body_does,
             test_an_unpicked_joint_turns_no_body_and_a_mandrel_holder_never_moves,
             test_a_picked_pivot_at_the_origin_is_still_a_datum,
             test_a_picked_home_is_carried_and_is_not_the_pivot,
             test_the_home_pick_is_a_nozzle_tip_and_is_reduced_by_its_standoff,
             test_the_collector_stands_on_its_picked_seat,
             test_the_exported_zero_is_the_picked_home,
             test_a_mandrel_ignores_the_collector_datums_entirely,
             test_a_pair_is_not_seated_and_a_mandrel_still_is,
             test_the_seating_reaches_every_body_on_the_collector_chain,
             test_the_collector_axis_constant_is_the_one_mew_frame_actually_builds,
             test_the_mount_brings_the_collector_axis_onto_the_spin_axis,
             test_a_dome_on_a_vertical_spindle_comes_up_under_the_nozzle]
    for t in tests:
        print(t.__name__)
        t()
    print()
    if _FAILS:
        print(f"FAILED ({len(_FAILS)}): {', '.join(_FAILS)}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
