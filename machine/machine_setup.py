"""machine_setup.py -- what is set up on the Machine & collector page, and what
is not. No Tk: the page reads its vars into a plain dict and asks this module.

ONE ANSWER, ONE PLACE. Statuses are shown only in the Instructions tile's
checklist. The cards and their tile summaries carry none: a tile names what its
card holds (`card_summary`).

`values` is keyed by the page's PERSIST keys (`machine.x_stl`, `rotary.j1_pivot`,
...) because those are the stable names every preset already uses. `defaults`
is the same mapping at the page's factory values; an item whose fields all
still equal them reads "Default" (there is no approving it: change a value, or
leave the ? as the reminder it is).

`mode` is the collector toggle: "plate" (3 axis), "mandrel" (4 axis) or
"surface" (5 axis).
"""
import os
from collections import namedtuple

DONE, MISSING, AUTO, DEFAULT, WAITING, OPTIONAL = (
    "done", "missing", "auto", "default", "waiting", "optional")

SYMBOL = {DONE: "✓", MISSING: "✗", AUTO: "!", DEFAULT: "?", WAITING: "–",
          OPTIONAL: "○"}
WORD = {DONE: "Done", MISSING: "Missing", AUTO: "Auto", DEFAULT: "Default",
        WAITING: "Waiting", OPTIONAL: "Optional"}

TIP_AUTO = ("Auto-detected. If results look broken in the viewer, try manual "
            "calibration.")
TIP_DEFAULT = "Default values loaded. Check they match your setup."
#: A value that came from a SHIPPED example printer preset is a default too: it
#: was measured on somebody else's printer, so "Done" would tell a stranger the
#: lab's numbers are theirs.
TIP_EXAMPLE = ("From the example printer preset that came with the app, not "
               "from your printer. Check it matches your setup.")
TIP_MISSING = "Not set yet."
TIP_OPTIONAL = "Optional. Only needed to see your printer in the simulation."

Item = namedtuple("Item", "key label state tip card")

#: The step numbers, in the order the Instructions tile lists them. REQUIRED
#: BEFORE OPTIONAL: a first print needs the collector and the g-code settings,
#: and on a plate it does not need the 3-D printer model at all, so the model
#: and its alignment come after them. Every "(step N)" a hint prints reads
#: this table, so the numbers cannot drift from the list.
STEP_NO = {"collector": 1, "gcode": 2, "machine": 3, "calibrate": 4, "save": 5}
_MODEL_STEP = "(step %d)" % STEP_NO["machine"]
Step = namedtuple("Step", "key number title state items card")

MODES = ("plate", "mandrel", "surface")

#: the kinematics chain is a default-able choice in every mode
_KINEMATICS_KEYS = ("machine.kinematics",)
_PLATE_KEYS = ("plate.shape", "plate.x", "plate.y", "plate.diameter",
               "plate.thickness")
_POSITION_KEYS = ("slide.offset_x", "slide.offset_y")
_ZERO_PLATE_KEYS = ("origin.face", "origin.anchor", "origin.custom")
_ROD_KEYS = ("rotary.rod_length", "rotary.rod_x")
_ZERO_ROD_KEYS = ("rotary.origin_axial", "rotary.origin_radial",
                  "rotary.origin_custom")


def _txt(values, key):
    v = values.get(key, "")
    text = "" if v is None else str(v).strip()
    if key == "origin.gcode_rules" and text == "[]":
        return ""
    return text


def _file_item(key, label, values, pkey, required, file_exists, card):
    path = _txt(values, pkey)
    if not path:
        return Item(key, label, MISSING if required else OPTIONAL,
                    TIP_MISSING if required else TIP_OPTIONAL, card)
    if not file_exists(path):
        # An OPTIONAL model part whose file is not on this computer stays
        # optional: a shipped preset can carry another lab's path, and then
        # "Next: Printhead STL (step 1)" once sent a newcomer after a file the
        # first print does not need, ahead of the collector it does.
        return Item(key, label, MISSING if required else OPTIONAL,
                    "File not found: %s" % os.path.basename(path)
                    + ("" if required else ". " + TIP_OPTIONAL), card)
    return Item(key, label, DONE, "", card)


def _default_item_ex(key, label, values, defaults, pkeys, card,
                     example=None):
    """Default while every field still equals the factory value, or the value
    the loaded example preset (`example`, keyed like `values`) put there."""
    same = all(_txt(values, k) == _txt(defaults, k) for k in pkeys)
    if same:
        return Item(key, label, DEFAULT, TIP_DEFAULT, card)
    if example and all(k in example and _txt(values, k) == _txt(example, k)
                       for k in pkeys):
        return Item(key, label, DEFAULT, TIP_EXAMPLE, card)
    return Item(key, label, DONE, "", card)


def _pick_item(key, label, values, pkey, prereq, prereq_name, card):
    """A calibrator pick stored whole as one "z,x,y" string."""
    if prereq is not None and not prereq:
        return Item(key, label, WAITING,
                    "Load the %s STL first %s." % (prereq_name, _MODEL_STEP),
                    card)
    got = _txt(values, pkey)
    ok = len([p for p in got.split(",") if p.strip()]) == 3
    return Item(key, label, DONE if ok else MISSING,
                "" if ok else TIP_MISSING, card)


def _loaded(values, pkey, file_exists):
    path = _txt(values, pkey)
    return bool(path) and file_exists(path)


def step_state(items):
    """The worst item wins: Missing, then Waiting, then Auto / Default. A step
    with nothing but optional items left blank is Optional."""
    states = [i.state for i in items]
    for s in (MISSING, WAITING, AUTO, DEFAULT):
        if s in states:
            return s
    if states and all(s == OPTIONAL for s in states):
        return OPTIONAL
    return DONE


def setup_steps(values, mode, defaults=None, file_exists=os.path.isfile,
                example=None):
    """The five steps for `mode`, each with its items. Step 5 (Save) has no
    items and no state. `example` is the field values a shipped example
    printer preset loaded (None when the loaded preset is the user's own).

    THE PRINTER MODEL IS OPTIONAL IN EVERY MODE. Its STLs and alignment feed
    only the simulation (`_machine_args`, the player's --machine profile); a
    mandrel's g-code needs its rotary axis letter, not the holder's STL, and
    generates without one. A mandrel item still reads Missing when its file
    IS loaded and its pick is not, because then the simulation would place
    the stage wrongly."""
    if mode not in MODES:
        mode = "plate"
    defaults = defaults or {}

    def _default_item(key, label, values, defaults, pkeys, card):
        return _default_item_ex(key, label, values, defaults, pkeys, card,
                                example)
    flat = mode == "plate"
    head = _loaded(values, "machine.x_stl", file_exists)

    # -- 1 Machine ------------------------------------------------------- #
    m = "machine"
    s1 = [_file_item("printhead", "Printhead STL", values, "machine.x_stl",
                     mode == "surface", file_exists, m)]
    if mode == "mandrel":
        s1.append(_file_item("holder", "Mandrel holder STL", values,
                             "machine.z_stl", False, file_exists, m))
    else:
        s1.append(_file_item("bed", "Bed STL", values, "machine.z_stl", False,
                             file_exists, m))
    if mode != "surface" and not head:
        s1.append(Item("kinematics", "How it moves", OPTIONAL, TIP_OPTIONAL, m))
    else:
        s1.append(_default_item("kinematics", "How it moves", values, defaults,
                                _KINEMATICS_KEYS, m))
    if mode == "mandrel":
        s1.append(Item("letter", "Rotary axis letter",
                       DONE if _txt(values, "rotary.word") else MISSING,
                       "" if _txt(values, "rotary.word") else TIP_MISSING, m))
    if mode == "surface":
        for n in (1, 2):
            it = _file_item("joint%d" % n, "Joint %d body" % n, values,
                            "rotary.j%d_stl" % n, True, file_exists, m)
            if it.state == DONE and not (
                    _txt(values, "rotary.j%d_word" % n)
                    and _txt(values, "rotary.j%d_axis" % n)):
                it = it._replace(state=MISSING,
                                 tip="Set the joint's letter and the axis it "
                                     "turns about.")
            s1.append(it)

    # -- 2 Calibrate ----------------------------------------------------- #
    c = "calibrate"
    tip_set = all(_txt(values, "machine.tip_" + a) for a in "xyz")
    if not head:
        surface = mode == "surface"
        s2 = [Item("tip", "Nozzle", WAITING if surface else OPTIONAL,
                   "Load the printhead STL first %s." % _MODEL_STEP
                   if surface else TIP_OPTIONAL, c)]
    else:
        s2 = [Item("tip", "Nozzle", DONE if tip_set else AUTO,
                   "" if tip_set else TIP_AUTO, c)]
    datum_set = all(_txt(values, k) for k in ("bed.datum_z", "bed.corner_x",
                                              "bed.corner_y"))
    bed = _loaded(values, "machine.z_stl", file_exists)
    if mode == "plate":
        if not bed:
            s2.append(Item("bed_face", "Bed top face", OPTIONAL, TIP_OPTIONAL,
                           c))
        else:
            s2.append(Item("bed_face", "Bed top face",
                           DONE if datum_set else AUTO,
                           "" if datum_set else TIP_AUTO, c))
    elif mode == "mandrel":
        if not bed:
            s2.append(Item("mandrel_axis", "Mandrel axis", OPTIONAL,
                           TIP_OPTIONAL, c))
        else:
            s2.append(Item("mandrel_axis", "Mandrel axis",
                           DONE if datum_set else MISSING,
                           "" if datum_set else TIP_MISSING, c))
    else:
        for n in (1, 2):
            s2.append(_pick_item(
                "joint%d_axis" % n, "Joint %d axis" % n, values,
                "rotary.j%d_pivot" % n,
                _loaded(values, "rotary.j%d_stl" % n, file_exists),
                "joint %d body" % n, c))

    # -- 3 Collector ----------------------------------------------------- #
    k = "collector"

    def position(label):
        # the offset places the collector under the PRINTER MODEL and reaches
        # nothing else (gui_buildplate._machine_args): with no printhead STL
        # it changes no output, so it is not a thing to check yet
        if not head:
            return Item("position", label, OPTIONAL, TIP_OPTIONAL, k)
        return _default_item("position", label, values, defaults,
                             _POSITION_KEYS, k)
    if mode == "plate":
        s3 = [_default_item("plate", "Plate", values, defaults, _PLATE_KEYS, k),
              position("Position on bed"),
              _default_item("zero_plate", "G-code zero", values, defaults,
                            _ZERO_PLATE_KEYS, k)]
    elif mode == "mandrel":
        s3 = [_default_item("rod", "Mandrel", values, defaults, _ROD_KEYS, k),
              position("Position on axis"),
              _default_item("zero_rod", "G-code zero", values, defaults,
                            _ZERO_ROD_KEYS, k)]
    else:
        j1 = _loaded(values, "rotary.j1_stl", file_exists)
        s3 = [_pick_item("seat", "Collector seat", values, "rotary.j1_seat",
                         j1, "joint 1 body", k),
              _pick_item("zero_pick", "G-code zero", values, "rotary.home",
                         j1, "joint 1 body", k)]

    s4 = [_default_item("gcode_flavor", "Flavor", values, defaults,
                        ("origin.gcode_flavor",), "gcode"),
          _default_item("gcode_blocks", "Start and end", values, defaults,
                        ("origin.gcode_start", "origin.gcode_end"), "gcode"),
          _default_item("gcode_rules", "Automatic commands", values, defaults,
                        ("origin.gcode_rules",), "gcode")]
    return [
        Step("collector", STEP_NO["collector"], "Collector", step_state(s3),
             tuple(s3), "collector"),
        Step("gcode", STEP_NO["gcode"], "G-code", step_state(s4), tuple(s4),
             "gcode"),
        Step("machine", STEP_NO["machine"], "Printer model", step_state(s1),
             tuple(s1), "machine"),
        Step("calibrate", STEP_NO["calibrate"], "Calibrate",
             step_state(s2), tuple(s2), "calibrate"),
        Step("save", STEP_NO["save"], "Save as a preset", None, (), "presets"),
    ]


#: why a "to check" item is flagged, said on the closed Instructions tile
_CHECK_WHY = {AUTO: "it was found automatically",
              DEFAULT: "it still has the default values"}


def next_action(steps):
    """One line for the Instructions tile: the first thing still to do, else
    the first thing to check, else that it is all set.

    Each names the CARD to open, since a step's title is its card's title.
    A check is a reminder, not a blocker (a Default cannot be approved, only
    changed), so its line says first that nothing is missing: a newcomer
    whose plate really is the default one reads that setup is finished."""
    for st in steps:
        for it in st.items:
            if it.state == MISSING:
                return "To do: “%s” on the %s card (step %d)." % (
                    it.label, st.title, st.number)
    for st in steps:
        for it in st.items:
            if it.state in _CHECK_WHY:
                return ("Nothing missing. To check: “%s” on the %s card "
                        "(step %d), as %s." % (it.label, st.title, st.number,
                                              _CHECK_WHY[it.state]))
    return "All set. Save it as a printer preset under Presets (step 5)."


def card_summary(steps, card, optional=False):
    """The tile line for one card: what it holds in this mode, by name. No
    status -- statuses live only in the Instructions checklist. An `optional`
    card says so first, so a closed tile tells a newcomer it can be skipped."""
    text = " · ".join(it.label for st in steps for it in st.items
                      if it.card == card)
    return ("Optional · " + text) if optional and text else (
        "Optional" if optional else text)


def item(steps, key):
    for st in steps:
        for it in st.items:
            if it.key == key:
                return it
    return None
