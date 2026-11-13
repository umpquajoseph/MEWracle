"""mew_gcode -- the G-code document behind the G-code page. No Tk.

THE ONE IDEA. The G-code page shows ONE text: the exact file Save writes. That
text is not stored. It is RENDERED from three things:

  * the BASE -- the generator's own output (the scratch file every converter,
    preview and player already reads), or an imported file;
  * its LINE MAP -- where every base line sits in the scaffold's structure
    (header, scaffold/group, layer, pass, exit), written out of band by
    `scaffold_gcode --line-map`, or guessed from Z steps for an imported file;
  * the CHANGES -- chips the user made: hand edits, rules, transforms, the
    output options and pieces. Each is recorded in STRUCTURAL terms ("after
    layer 7", "layer 3, pass 4: F -> 900"), never as a line number.

So a redesign is a new base and a new map with the same chips, and `render()`
puts every chip back at its structural place. A chip whose place no longer
exists is listed as "couldn't carry over" with its text kept, never dropped.

TWO FORMS of the same render:

  * the EXPORT form -- what the page shows and what Save / Copy / Send write:
    the output options applied (travels as G1, G0X padding, one layer Z, the
    heat-and-wait line), in the document's flavor;
  * the PARSE form -- what the parser, converter and player read: base lines
    before the output options (so a generated travel is still a G0, which is
    how the parser tells travel from deposition), user lines verbatim with
    comments stripped. With travels-as-G1 ON by default, a literal parse of the
    export form would deposit every travel, so the two forms are not optional.

WITH NO CHIPS the export form is byte-identical to what `gui_scaffold.
_gcode_for_display_export` wrote before this module existed; that function is
now a thin wrapper over `apply_output_options` here. `test_mew_gcode.py` holds
the comparator against the pre-move code.

Human-readable comments may appear in exports when the machine accepts them.
Structural metadata remains out of band and is never read from comments, with
one exception: the ``;LAYER:n`` layer markers ("Mark each layer in the file",
`OutputOptions.layer_markers`), a public convention other viewers read too.
They only say where a layer starts, and only for a file with no structure
of its own: the generator's map and a recalled one (`recall_structure`) come
first, and a file without them falls back to its motion
(`layer_starts_from_motion`). Groups, colours, passes and the
no-self-crossings marker never travel in a comment.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import math
import re
from dataclasses import dataclass, field, fields as _dc_fields

# =========================================================================== #
# 1. The user-facing output rewrite -- moved here byte-identically from
#    gui_scaffold.py (`_split_z_word`, `_is_five_axis_gcode`,
#    `_rewrite_gcode_lines`, the M109 line and the custom line after line N).
#    gui_scaffold keeps thin wrappers with the old names.
# =========================================================================== #

#: The rotary axis letters a MEWracle file can carry: the parser's
#: `gcode_to_fibers._UNREAD_ROTARY_WORDS` (U, V, B, C) plus A, which that parser
#: reads, plus W. Keep the alphabet in step with the parser's by hand.
ROTARY_WORDS = frozenset("ABCUVW")


def split_z_word(line):
    """``(tokens, index_of_Z, z_value)`` for a MOTION line carrying a Z word,
    else ``None``. Only ``G<digit> `` lines are considered, so a header
    (``G21``/``G90``) or a blank line can never be mistaken for one."""
    if len(line) < 3 or line[0] != "G" or not line[1].isdigit() \
            or line[2] != " ":
        return None
    toks = line.split()
    for i, t in enumerate(toks):
        if t[:1] == "Z" and len(t) > 1:
            try:
                return (toks, i, float(t[1:]))
            except ValueError:
                return None
    return None


def is_five_axis_gcode(text):
    """True when this file's moves carry TWO rotary words, which is what a
    five-axis collector file is and nothing else in this app writes.

    WHY THE FILE IS ASKED AND NOT THE PAGE. "Don't adjust height per layer"
    deletes Z words, and on a five-axis file Z is not a layer height: it is
    one coordinate of a point on a curved surface, coupled to the two rotary
    words on the same line. Deleting it does not flatten the ladder, it
    commands the nozzle somewhere off the part -- 3.59 mm off on the library
    dome. The rewrite is a pure text
    pass reached from the g-code text view, from Save-g-code-as, and from a
    harness that calls it with no GUI at all, so the guard has to be in the
    text or it is not in every route.

    TWO, not one, is the whole discrimination: `generate_surface_gcode`
    refuses a profile without exactly two rotary joints, and the four-axis
    mandrel carries exactly one (`G1 X.. A..`), where the option IS
    meaningful and drops the inter-ply `G0 Z<h>` as it always has. A flat
    file carries none.

    Only `G<digit> ` lines are read, the same gate `split_z_word` uses, so
    a header or a comment can never be mistaken for a move."""
    for line in text.splitlines():
        if len(line) < 3 or line[0] != "G" or not line[1].isdigit() \
                or line[2] != " ":
            continue
        seen = set()
        for t in line.split()[1:]:
            if len(t) > 1 and t[0] in ROTARY_WORDS:
                seen.add(t[0])
                if len(seen) >= 2:
                    return True
    return False


def _rewrite_line_list(lines, travel_as_g1, padded, drop_layer_z=False,
                       boundaries=(), five_axis=None):
    """The rewrite, line by line: one output per input line, ``None`` where
    the Z pass deleted the line. `lines` keep their line endings. This is the
    form `Document.render` needs, because it has to know which output line
    came from which base line; `rewrite_gcode_lines` joins it.

    `five_axis` is the guard's answer when the caller already has it (the
    document asks it of the whole base text); None asks it of `lines`."""
    if drop_layer_z:
        if five_axis is None:
            five_axis = is_five_axis_gcode("".join(lines))
        if five_axis:
            drop_layer_z = False
    out = list(lines)
    if drop_layer_z:
        want = {int(n): float(z) for n, z in boundaries}
        ordinal = -1
        held = None                         # the height the file is now in
        for i, line in enumerate(lines):
            found = split_z_word(line)
            if found is None:
                continue
            toks, zi, zval = found
            ordinal += 1
            if held is None:                # the height, named once, at the top
                held = zval
                continue
            z_at = want.get(ordinal)
            if (z_at is not None and abs(zval - z_at) <= 1e-6
                    and abs(zval - held) > 1e-6):
                held = zval                 # a scaffold boundary, still named
                continue
            rest = toks[:zi] + toks[zi + 1:]
            if len(rest) <= 1:              # the Z word was the whole move
                out[i] = None
                continue
            out[i] = " ".join(rest) + line[len(line.rstrip("\r\n")):]
    for i, line in enumerate(out):
        if line is None:
            continue
        if travel_as_g1 and line.startswith("G0 "):
            line = "G1 " + line[3:]
        if (padded and len(line) >= 3 and line[0] == "G"
                and line[1].isdigit() and line[2] == " "):
            line = "G0" + line[1:]          # G0->G00, G1->G01, G2->G02, G3->G03
        out[i] = line
    return out


def rewrite_gcode_lines(text, travel_as_g1, padded, drop_layer_z=False,
                        boundaries=()):
    """Rewrite the USER-FACING g-code per the emission options. A MEWron
    scaffold writes travels as ``G0``, deposition as ``G1`` and turnaround arcs
    as ``G2``/``G3``.

      travel_as_g1 -> the travel ``G0`` hops become ``G1`` printing moves.
      padded       -> every single-digit motion command (a ``G<digit>``
                      followed by a space) is zero-padded. Multi-digit codes
                      (``G21``, ``G90``, ...) and non-G lines are untouched.
      drop_layer_z -> "Don't adjust height per layer": the file names its
                      height ONCE. The first Z word survives; every later one
                      is deleted from its line, and a line that was nothing
                      BUT its Z word (the mandrel's inter-ply ``G0 Z<h>``)
                      goes with it. A line keeping an F word is kept even
                      when its Z was its only axis, because dropping it would
                      drop the feed.
      boundaries   -> ``(ordinal, height)`` pairs that still deserve a Z
                      word despite `drop_layer_z`: the start of each scaffold
                      after the first, so a stack of parts still steps
                      between the PARTS. `ordinal` counts Z-BEARING MOTION
                      LINES from 0, which the emitters make exactly one of
                      per layer (plus the plate lead-in) -- an ordinal, not a
                      height match, because a scaffold's own ladder can climb
                      THROUGH a later scaffold's start Z and a height alone
                      cannot tell the two apart. `height` is checked against
                      what is actually there: if the file is not the ladder
                      we predicted, the Z word is dropped like any other
                      rather than kept at a guessed line. A boundary that
                      commands the height already in force emits nothing --
                      "don't adjust the height" means exactly that.
                      Empty (the "Don't adjust height per scaffold" state, a
                      tube, or a lone scaffold) -> exactly one Z word.
                      IGNORED ON A FIVE-AXIS FILE: there Z is a surface
                      coordinate coupled to the two rotary words on its own
                      line, not a layer height, so the pass is skipped
                      whole and every Z word survives. See
                      `is_five_axis_gcode`. The travel and padding passes
                      below still run -- they change a spelling and not a
                      commanded position.

    They compose, and the Z pass runs FIRST, while every travel is still
    spelled ``G0``: a travel then becomes ``G1`` and pads to ``G01``. Only
    the spelling changes -- the PARSE form the converter / preview / player
    read never passes through here, so geometry and visuals are unaffected.
    That is the whole reason the Z suppression lives here and not in the
    generator: at one commanded Z the fibers still sag and stack, and that is
    the physical result worth predicting."""
    if drop_layer_z and is_five_axis_gcode(text):
        drop_layer_z = False
    if not (travel_as_g1 or padded or drop_layer_z):
        return text
    out = _rewrite_line_list(text.splitlines(keepends=True), travel_as_g1,
                             padded, drop_layer_z, boundaries, five_axis=False)
    return "".join(ln for ln in out if ln is not None)


def wait_temp_line(temperature, flavor="klipper"):
    """The heat-and-wait line for the top of the user-facing g-code, with its
    newline, or "" for no line: `temperature` (degC) None, unparseable, not
    finite or not positive writes no line rather than a guessed one.

    Klipper spells it ``M109 S<T>`` -- M109, not M108: M109 is set-and-wait on
    Klipper and Marlin, M108 cancels a wait (Marlin) or is unknown (Klipper).
    Other flavors use their profile's `heat_wait`; a flavor with none (GRBL)
    writes no line, and `problems()` says so."""
    if temperature is None:
        return ""
    try:
        t = float(temperature)
    except (TypeError, ValueError):
        return ""
    if not (math.isfinite(t) and t > 0.0):
        return ""
    tmpl = get_flavor(flavor).heat_wait
    if not tmpl:
        return ""
    return tmpl.replace("{t}", f"{t:g}") + "\n"


#: a NOZZLE heater set: M104/M109 (Marlin, Klipper, RepRap) or Klipper's
#: SET_HEATER_TEMPERATURE on the extruder. Matched after the comment is cut.
_NOZZLE_SET = re.compile(r"^\s*(?:N\d+\s+)?(M104|M109)\b(.*)$", re.I)
_KLIPPER_SET = re.compile(
    r"^\s*SET_HEATER_TEMPERATURE\b(?=.*\bHEATER\s*=\s*extruder\b)"
    r".*\bTARGET\s*=\s*([-+0-9.eE]+)", re.I)
_WORD_NUM = re.compile(r"([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))", re.I)


def nozzle_temperatures(lines):
    """Every nozzle temperature a g-code program SETS, in file order, as
    ``[(line_number, degC), ...]`` (1-based lines). `lines` is any iterable of
    text lines, so a file object streams.

    Counted: ``M104``/``M109`` with an ``S`` (or Marlin's M109 ``R``) word and
    no tool other than ``T0``, and Klipper's ``SET_HEATER_TEMPERATURE
    HEATER=extruder TARGET=``. A heater turned OFF (0) is not a print
    temperature and is skipped, so the usual ``M104 S0`` at the end of a file
    does not count. Comments never count."""
    out = []
    for n, raw in enumerate(lines, 1):
        line = raw.split(";", 1)[0]
        if not line.strip():
            continue
        t = None
        m = _NOZZLE_SET.match(line)
        if m:
            words = {k.upper(): v for k, v in _WORD_NUM.findall(m.group(2))}
            if "T" in words and float(words["T"]) != 0.0:
                continue
            t = words.get("S", words.get("R") if m.group(1).upper() == "M109"
                          else None)
        else:
            k = _KLIPPER_SET.match(line)
            if k:
                t = k.group(1)
        if t is None:
            continue
        try:
            t = float(t)
        except ValueError:
            continue
        if math.isfinite(t) and t > 0.0:
            out.append((n, t))
    return out


def insert_line_after(text, line, after):
    """`text` with `line` inserted after its line `after` (0 = the top; past
    the end = the last line). Counted on the lines of `text` as given, so the
    caller runs it LAST on user-facing g-code: the count is then lines of the
    file the user saves and sees, M109 header included."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += "\n"
    lines.insert(min(after, len(lines)), line + "\n")
    return "".join(lines)


#: Sentinel for `OutputOptions.boundaries`: derive the scaffold boundaries
#: from the document's line map instead of from a caller's ordinal list.
AUTO = "auto"


@dataclass
class OutputOptions:
    """The Export card's output options, as one value. Each is an option chip
    on the G-code page and each acts on the generator's OWN lines only, never
    on a line a chip added.

    travel_as_g1   "Emit travel moves as G1".
    padded         the G0X spelling (``G00``/``G01``...).
    drop_layer_z   "Don't adjust height per layer".
    boundaries     ``(ordinal, height)`` pairs (see `rewrite_gcode_lines`),
                   or `AUTO` to read them off the line map.
    wait_temp      degC for the heat-and-wait line, or None for none.
    custom_line    the machine setting's ``(line, after)`` -- one line after
                   line N of the FINAL file -- or None. Kept for presets that
                   carry it; on the G-code page it is a chip like the rest.
    incremental    the Machine page's "Incremental coordinates": the export
                   homes, moves to `origin`, switches to incremental and
                   writes every move as a delta (`machine_render`).
    origin         ``((letter, value), ...)`` -- where the MACHINE is when
                   the nozzle sits at G-code zero, as its readout shows it
                   after homing. Incremental only.
    rotary_axes    the rotary letters the machine has, e.g. "A": a home
                   line naming a rotary axis it lacks is not written.
    layer_markers  "Mark each layer in the file": a ``;LAYER:n`` comment
                   before each layer (and ``;LAYER_COUNT:N`` before the
                   first), from the generator's line map, so a file whose
                   height is named once still says where its layers are --
                   to another computer's MEWracle (`LineMap.guess`) and to
                   other viewers. A `Document` writes them; the plain-text
                   `apply_output_options` has no map and writes none.

    The caller decides what applies: an imported file passes all-off, which
    is today's invariant that an imported file is never rewritten."""
    travel_as_g1: bool = False
    padded: bool = False
    drop_layer_z: bool = False
    boundaries: object = ()
    wait_temp: object = None
    custom_line: object = None
    incremental: bool = False
    origin: tuple = ()
    rotary_axes: str = ""
    layer_markers: bool = False
    start_block: str = ""
    end_block: str = ""
    context: dict = field(default_factory=dict)
    rig_recipes: list = field(default_factory=list)

    def active(self, flavor="klipper"):
        """True when the export form differs from the base text."""
        return bool(self.travel_as_g1 or self.padded or self.drop_layer_z
                    or wait_temp_line(self.wait_temp, flavor)
                    or self.custom_line is not None
                    or self.incremental or self.start_block or self.end_block
                    or self.rig_recipes or self.layer_markers
                    or spells_moves(flavor))

    def to_dict(self):
        b = self.boundaries
        return {"travel_as_g1": bool(self.travel_as_g1),
                "padded": bool(self.padded),
                "drop_layer_z": bool(self.drop_layer_z),
                "boundaries": b if b == AUTO else [list(p) for p in b],
                "wait_temp": self.wait_temp,
                "custom_line": (None if self.custom_line is None
                                else list(self.custom_line)),
                "incremental": bool(self.incremental),
                "origin": [list(p) for p in self.origin],
                "rotary_axes": str(self.rotary_axes or ""),
                "layer_markers": bool(self.layer_markers),
                "start_block": self.start_block, "end_block": self.end_block,
                "context": dict(self.context), "rig_recipes": list(self.rig_recipes)}

    @classmethod
    def from_dict(cls, d):
        d = dict(d or {})
        b = d.get("boundaries", ())
        cl = d.get("custom_line")
        return cls(travel_as_g1=bool(d.get("travel_as_g1", False)),
                   padded=bool(d.get("padded", False)),
                   drop_layer_z=bool(d.get("drop_layer_z", False)),
                   boundaries=b if b == AUTO else tuple(tuple(p) for p in b),
                   wait_temp=d.get("wait_temp"),
                   custom_line=None if cl is None else (str(cl[0]), int(cl[1])),
                   incremental=bool(d.get("incremental", False)),
                   origin=tuple((str(p[0]), float(p[1]))
                                for p in d.get("origin", ())),
                   rotary_axes=str(d.get("rotary_axes", "") or ""),
                   layer_markers=bool(d.get("layer_markers", False)),
                   start_block=str(d.get("start_block", "") or ""),
                   end_block=str(d.get("end_block", "") or ""),
                   context=dict(d.get("context", {}) or {}),
                   rig_recipes=list(d.get("rig_recipes", ()) or ()))


def apply_output_options(text, opts, flavor="klipper"):
    """The user-facing g-code: `text` (the generator's output) with `opts`
    applied. Byte-identical to the pre-engine `_gcode_for_display_export`:
    the spelling rewrite first, the heat-and-wait line on AFTER it (so no
    spelling pass ever reads it), the custom line last of all.

    `opts.boundaries` must be an ordinal list here; `AUTO` needs a line map
    and so a `Document`."""
    if opts.boundaries == AUTO:
        raise ValueError("AUTO boundaries need a line map: use Document")
    if opts.rig_recipes:
        return Document(text, LineMap.guess_from_z(text), flavor, opts).export_text()
    if opts.travel_as_g1 or opts.padded or opts.drop_layer_z:
        text = rewrite_gcode_lines(text, opts.travel_as_g1, opts.padded,
                                   drop_layer_z=opts.drop_layer_z,
                                   boundaries=opts.boundaries)
    text = wait_temp_line(opts.wait_temp, flavor) + text
    text = machine_text(text, flavor, opts)
    if opts.custom_line is not None:
        line, after = opts.custom_line
        text = insert_line_after(text, line, after)
    start = render_machine_block(opts.start_block, opts.context, flavor)
    end = render_machine_block(opts.end_block, opts.context, flavor)
    text = start + text
    if end:
        if text and not text.endswith(("\n", "\r")):
            text += "\n"
        text += end
    return text


# =========================================================================== #
# 2. Flavors
# =========================================================================== #

@dataclass
class Flavor:
    """A firmware dialect, as data. Only what is GROUNDED is filled in: a
    field no primary source settles is left empty and the Insert form greys
    out, rather than guessing a command (the physics rule, "assert only what
    is grounded", applied to syntax). `sources` names the authority for each
    filled field.

    The document is G-code INSIDE, always: the base, the chips and the parse
    form the converter and player read. A flavor whose MOVE SPELLING is not
    G-code (Aerotech's `LINEAR`, a lab's `MOVE`) is translated at the end of
    the export render (`machine_render`) and back when the page records
    typing (`from_machine_line`), one table both ways, so the moves change
    spelling and nothing else does. A G-code flavor's table is the identity.

    move_word / rapid_word    the G1 and G0 words. An empty rapid word writes
                              a travel as the move word (MEW draws it anyway).
    arc_cw / arc_ccw          the G2 and G3 words. Empty: an arc is written as
                              short straight moves.
    abs_word / inc_word       the G90 and G91 words. Empty: no such command.
    home                      the home line(s), machine spelling. The first
                              token is the home word (G28's spelling).
    units_word                G21's word. Empty: units are a controller
                              setting and nothing is written.
    dwell_cmd                 G4's word.
    rotary_feed               "combined" (one F, the surface speed) or "split"
                              (F linear, E rotary: `scaffold_gcode`'s
                              `MANDREL_ROTARY_FEEDS`).

    dwell_word / dwell_unit   the G4 parameter and its unit, "ms" or "s"
                              ("" = unknown: no dwell form).
    heat_wait                 template with {t} (degC), "" when none.
    message                   template with {msg}, "" when none.
    pause                     the operator-pause line, "" when none.
    pressure / hv_on / hv_off / hv_level
                              MEW process commands. Empty for every built-in:
                              no firmware source defines them. Custom fills
                              them.
    comment                   the comment lead character.
    max_line                  maximum characters per line, 0 = no limit.
    line_numbers              write N<k> words.
    header / footer           lines framing every export.
    multi_per_line            several commands per line are legal (Duet).
    known                     words the controller accepts that are NOT
                              moves (a lab's `$AO`, a macro): kept as written
                              and never asked about by `unread_moves`.
    aliases                   READ-ONLY words, "WORD=G1" / "WORD=G0": a file
                              in this flavor may also spell a move this way
                              (Aerotech's RAPID, a lab's MOVEINC). The parse
                              form reads them as that G-code; the text and
                              the export keep the word as it was written, and
                              MEWracle never writes it.

    A command matters to MEWracle only when it changes what the preview,
    the converter and the player read: the moves (G0-G3), G90/G91, G20/G21,
    G28, G4 and G92 (`MEANING_COMMANDS`). Every other line goes to the
    machine exactly as written, assumed intentional: a lab's own command
    needs no field here (the Insert library writes it)."""
    name: str
    label: str
    dwell_word: str = "P"
    dwell_unit: str = "ms"
    heat_wait: str = ""
    message: str = ""
    pause: str = ""
    pressure: str = ""
    hv_on: str = ""
    hv_off: str = ""
    hv_level: str = ""
    comment: str = ";"
    max_line: int = 0
    line_numbers: bool = False
    header: tuple = ()
    footer: tuple = ()
    line_ending: str = "\n"
    multi_per_line: bool = False
    known: tuple = ()
    aliases: tuple = ()
    move_word: str = "G1"
    rapid_word: str = "G0"
    arc_cw: str = "G2"
    arc_ccw: str = "G3"
    abs_word: str = "G90"
    inc_word: str = "G91"
    home: tuple = ("G28",)
    units_word: str = "G21"
    dwell_cmd: str = "G4"
    rotary_feed: str = "combined"
    builtin: bool = True
    sources: dict = field(default_factory=dict)

    def to_dict(self):
        d = dict(self.__dict__)
        d["header"] = list(self.header)
        d["footer"] = list(self.footer)
        d["known"] = list(self.known)
        d["aliases"] = list(self.aliases)
        d["home"] = list(self.home)
        d["sources"] = dict(self.sources)
        return d

    @classmethod
    def from_dict(cls, d):
        """The profile `d` (`to_dict`) describes. A key this version does not
        know is dropped, so a flavor saved by a newer MEWracle still loads."""
        names = {f.name for f in _dc_fields(cls)}
        d = {k: v for k, v in dict(d).items() if k in names}
        for k in ("header", "footer", "known", "aliases", "home"):
            if k in d:
                d[k] = tuple(d[k] or ())
        d.setdefault("builtin", False)
        return cls(**d)


FLAVORS = {}        # filled in section 2b below


def get_flavor(name):
    """The profile called `name` (case-insensitive), or Klipper for an
    unknown name -- the default, and byte-identical to today."""
    if isinstance(name, Flavor):
        return name
    return FLAVORS.get(str(name or "klipper").lower(), FLAVORS["klipper"])


# =========================================================================== #
# 3. Structure -- the line map
# =========================================================================== #

LINE_MAP_VERSION = 1


@dataclass(frozen=True)
class Address:
    """Where one base line sits. `section` is "header" (everything before the
    first scaffold group: G21/G90 and the plate lead-in), "body" or "exit".
    `group` indexes `LineMap.groups` (the stabilization base is a group, as in
    `--scaffold-groups`); `layer` is the design layer within the group;
    `pass_` the deposition pass within the layer, -1 for the layer's entry
    (its travel hop and linking loop). `k` counts lines within that bucket.

    `key()` is the stable identity a hand edit is recorded against."""
    section: str
    group: int = -1
    layer: int = -1
    pass_: int = -1
    k: int = 0

    def key(self):
        return (self.section, self.group, self.layer, self.pass_, self.k)


class LineMap:
    """The structure of a base text, line by line.

    `marks` are what the generator records: ``{"line": i, "kind": ...}`` with
    kind "group" (+ "group": index), "layer" (+ "layer": design index),
    "pass" (+ "pass": index) or "exit", each meaning "line i starts this".
    `groups` mirror `--scaffold-groups` (name, layers, fiber_diameter).
    `guessed` is True for a map read off Z steps (an imported file), and the
    Outline says so.

    `sha1` is the base text's hash when the generator wrote the map, so a map
    is never applied to a text it does not describe (`matches`).

    `basis` says how a map that is not the generator's was found (`guess`):
    "comments" (the file's own ``;LAYER:n`` lines), "height" (Z steps) or
    "crossings" (where the fiber starts crossing the layer it laid). The
    Outline names it."""

    def __init__(self, marks, n_lines, groups=(), guessed=False, sha1=None,
                 basis=None):
        self.marks = sorted((dict(m) for m in marks),
                            key=lambda m: (int(m["line"]),))
        self.n_lines = int(n_lines)
        self.groups = [dict(g) for g in groups]
        self.guessed = bool(guessed)
        self.sha1 = sha1
        self.basis = basis or ("height" if self.guessed else None)
        self._addr = None
        self._seen = None       # (text, sha1, answer): `matches`' last call

    # -- io ---------------------------------------------------------------- #
    def to_dict(self):
        d = {"version": LINE_MAP_VERSION, "n_lines": self.n_lines,
             "groups": self.groups, "marks": self.marks,
             "guessed": self.guessed, "sha1": self.sha1}
        if self.guessed:
            d["basis"] = self.basis
        return d

    @classmethod
    def from_dict(cls, d):
        if int(d.get("version", 0)) != LINE_MAP_VERSION:
            raise ValueError("unknown line-map version %r" % d.get("version"))
        return cls(d["marks"], d["n_lines"], d.get("groups", ()),
                   d.get("guessed", False), d.get("sha1"), d.get("basis"))

    @classmethod
    def load(cls, path):
        with open(path) as fh:
            return cls.from_dict(json.load(fh))

    def save(self, path):
        with open(path, "w") as fh:
            json.dump(self.to_dict(), fh)

    def matches(self, text):
        """True when this map describes `text` (same hash, or same line count
        when the map carries no hash).

        The answer for the last text asked about is kept, by identity: the
        document asks about the same base text from every group name and
        every render, and hashing a large text each time held the window
        0.8-2 s per panel opened or closed."""
        seen = self._seen
        if seen is not None and seen[0] is text and seen[1] == self.sha1:
            return seen[2]
        if self.sha1:
            ok = self.sha1 == text_sha1(text)
        else:
            ok = self.n_lines == len(text.splitlines())
        self._seen = (text, self.sha1, ok)
        return ok

    # -- addresses --------------------------------------------------------- #
    def addresses(self):
        """One `Address` per base line."""
        if self._addr is not None:
            return self._addr
        at = {}
        for m in self.marks:
            at.setdefault(int(m["line"]), []).append(m)
        out = []
        section, group, layer, pass_ = "header", -1, -1, -1
        counts = {}
        for i in range(self.n_lines):
            for m in at.get(i, ()):
                kind = m["kind"]
                if kind == "group":
                    section, group, layer, pass_ = "body", int(m["group"]), -1, -1
                elif kind == "layer":
                    section, layer, pass_ = "body", int(m["layer"]), -1
                elif kind == "pass":
                    pass_ = int(m["pass"])
                elif kind == "exit":
                    section, group, layer, pass_ = "exit", -1, -1, -1
            b = (section, group, layer, pass_)
            k = counts.get(b, 0)
            counts[b] = k + 1
            out.append(Address(section, group, layer, pass_, k))
        self._addr = out
        return out

    @classmethod
    def guess_from_z(cls, text):
        """A map for a file with no generator map: one group, a new layer at
        every change of commanded Z, no passes. `guessed` is set."""
        marks = []
        z = None
        n = 0
        started = False
        for i, line in enumerate(text.splitlines()):
            n = i + 1
            cmd, words = parse_words(line)
            if cmd not in ("G0", "G1", "G2", "G3", "G00", "G01", "G02", "G03"):
                continue
            if not started:
                marks.append({"line": i, "kind": "group", "group": 0})
                started = True
            if "Z" in words:
                try:
                    zv = float(words["Z"])
                except ValueError:
                    continue
                if z is None or abs(zv - z) > 1e-9:
                    marks.append({"line": i, "kind": "layer",
                                  "layer": sum(1 for m in marks
                                               if m["kind"] == "layer")})
                    z = zv
        return cls(marks, n, [{"name": "file", "layers": sum(
            1 for m in marks if m["kind"] == "layer")}], guessed=True,
            sha1=text_sha1(text))

    @classmethod
    def guess(cls, text, comment=";"):
        """A map for a file that came with none of the generator's (an opened
        file, a piece), the layers found in this order of trust:

          1. the file's own layer comments, ``;LAYER:n``
             (`layer_starts_from_comments`): what MEWracle's saves carry
             unless "Mark each layer in the file" is off, and what Cura
             writes and most viewers read;
          2. its motion (`layer_starts_from_motion`): a new height, and, in
             a file that names its height once, where the fiber starts
             crossing the layer it just laid.

        One group, no passes. `guessed` is set and `basis` names which, so
        the Outline says how its layers were found."""
        lines = text.splitlines()
        first, starts = layer_starts_from_comments(lines, comment)
        basis = "comments"
        if not starts:
            first, starts, basis = layer_starts_from_motion(lines, comment)
        marks = []
        if first is not None:
            marks.append({"line": first, "kind": "group", "group": 0})
        marks += [{"line": i, "kind": "layer", "layer": k}
                  for k, i in enumerate(starts)]
        return cls(marks, len(lines), [{"name": "file",
                                        "layers": len(starts)}],
                   guessed=True, sha1=text_sha1(text), basis=basis)


#: what the Outline adds to a group whose layers were found by `guess`, by
#: its `basis`
GUESS_LABELS = {
    "height": " (layers guessed from height changes)",
    "crossings": " (layers guessed from where the fiber crosses itself)",
    "comments": " (layers from the file's layer comments)",
}

#: ``;LAYER:3``, Cura's spelling, which most g-code viewers and OctoPrint's
#: layer plugins read: what `Document` writes before each layer while
#: `OutputOptions.layer_markers` is on (without the comment character here)
_LAYER_COMMENT = re.compile(r"\s*LAYER\s*:\s*(-?\d+)\s*$", re.I)
_LINE_NO_LEAD = re.compile(r"\s*[Nn]\d+\s*")


def layer_marker(k, comment=";"):
    """The comment that opens layer `k` (0-based, counted over the file)."""
    return "%sLAYER:%d" % (comment, int(k))


def layer_count_marker(n_layers, comment=";"):
    """The comment written once, before the first layer's, as Cura does."""
    return "%sLAYER_COUNT:%d" % (comment, int(n_layers))


def layer_starts_from_comments(lines, comment=";"):
    """``(first line of the body, [line of each layer comment])`` for a file
    whose layers are marked ``;LAYER:n``; ``(None, [])`` when none is. The
    numbers in the comments are not trusted to count from anything: the
    k-th comment opens the k-th layer."""
    starts = []
    first = None
    lead = comment or ";"
    for i, line in enumerate(lines):
        s = line.lstrip()
        if s[:1] in ("N", "n"):
            s = _LINE_NO_LEAD.sub("", s, count=1)
        if s.startswith(lead):
            if _LAYER_COMMENT.match(s[len(lead):]):
                starts.append(i)
            continue
        if first is None and s and canon_cmd(
                parse_command(s, comment) or "") in _MOTION:
            first = i
    if not starts:
        return None, []
    return min(starts[0], first if first is not None else starts[0]), starts


def _seg_cells(ax, ay, bx, by, cell):
    """The grid cells a segment's samples fall in, one sample every half
    cell, so every point of it is within a quarter cell of a sample: two
    segments that meet then have samples in cells at most one apart."""
    n = max(1, int(math.hypot(bx - ax, by - ay) / (0.5 * cell)) + 1)
    out = set()
    for k in range(n + 1):
        t = k / n
        out.add((int(math.floor((ax + (bx - ax) * t) / cell)),
                 int(math.floor((ay + (by - ay) * t) / cell))))
    return out


def _segments_meet(a, b, tol):
    """How segment `a` meets `b` (each ``(x0, y0, x1, y1)``): True when it
    crosses it at a point more than `tol` from all four ends; the length
    (mm) it runs along it, collinear, when that is more than `tol`; else
    False. A touch at an end -- a turn that starts on a fiber, a pass that
    stops on one -- is neither."""
    ax, ay, bx, by = a
    cx, cy, dx, dy = b
    rx, ry = bx - ax, by - ay
    sx, sy = dx - cx, dy - cy
    lr, ls = math.hypot(rx, ry), math.hypot(sx, sy)
    if lr <= tol or ls <= tol:
        return False
    qx, qy = cx - ax, cy - ay
    den = rx * sy - ry * sx
    if abs(den) > 1e-9 * lr * ls:
        t = (qx * sy - qy * sx) / den
        u = (qx * ry - qy * rx) / den
        et, eu = tol / lr, tol / ls
        return et < t < 1 - et and eu < u < 1 - eu
    if abs(qx * ry - qy * rx) > tol * lr:
        return False                    # parallel, apart
    ux, uy = rx / lr, ry / lr
    t0 = qx * ux + qy * uy
    t1 = (dx - ax) * ux + (dy - ay) * uy
    along = min(lr, max(t0, t1)) - max(0.0, min(t0, t1))
    return along if along > tol else False


def _arc_points(x0, y0, x1, y1, i, j, cw, step):
    """An arc's chord ends after its start, about `step` apart."""
    cx, cy = x0 + i, y0 + j
    r = math.hypot(i, j)
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    d = (a0 - a1) if cw else (a1 - a0)
    while d <= 1e-12:
        d += 2.0 * math.pi
    n = max(2, min(64, int(r * d / max(step, 1e-9)) + 1))
    sgn = -1.0 if cw else 1.0
    return [(cx + r * math.cos(a0 + sgn * d * k / n),
             cy + r * math.sin(a0 + sgn * d * k / n))
            for k in range(1, n)] + [(x1, y1)]


#: how close (mm) two fibers must come to count as crossing or retracing:
#: well under any fiber MEW lays, well over a written coordinate's rounding
CROSSING_TOL_MM = 1e-3
#: a turn sharper than this (degrees) between two moves ends a stroke: the
#: corner between a pass and its turnaround loop, not a bend along a wave
STROKE_TURN_DEG = 30.0
#: how many long strokes of the layer so far one long stroke must cross to
#: start a new layer: two, so a layer of two passes is still seen
CROSSED_FIBERS = 2


def layer_starts_from_motion(lines, comment=";"):
    """``(first motion line, [layer start lines], basis)`` read off the moves
    of a file with no layer comments.

    HEIGHT. A layer starts where the nozzle settles at a new height. A raise
    that comes back down to the height it left (a travel hop) is not one,
    nor is a first height left for a lower one (the approach); the first
    layer starts at its first move in X/Y, so a ``G1 Z0`` lead-in stays
    above it. A file with no hops reads as the old one-layer-per-Z-change
    guess did.

    CROSSINGS. A file that names its height once ("Don't adjust height per
    layer") says nothing about layers in Z. What it still says is in the
    fiber: a layer is fibers laid side by side, and the next layer is laid
    ACROSS them (or along them, stacked at the same angle). So within each
    height, a new layer starts where a deposition move first crosses, or
    runs along, a fiber already laid in the current layer, an end-touch
    excepted (`_segments_meet`). The start is moved back to the last move
    that goes nowhere since that layer began -- the remnant of a removed
    layer height, which MEWracle's own files carry at every layer change --
    when there is one.

    Crossings are read only where they mean stacking: a file with no E
    words (continuous-flow MEW, where every G1 lays fiber) and no rotary
    words (on a tube a helix crosses the opposite helix within one layer).
    A file with E (a slicer's, whose infill may cross itself within a
    layer) or a rotary axis keeps heights only. A fiber that never crosses
    itself is one layer, so a single-layer file is never split.

    `basis` is "crossings" when a crossing started a layer, else
    "height"."""
    pos = [0.0, 0.0]
    z = None
    rel = False
    moves = []          # (line, x0, y0, x1, y1, z, cmd, words, zero)
    first = None
    has_e = rotary = False
    for i, line in enumerate(lines):
        if not line or line.lstrip()[:1] in (comment, ""):
            continue
        cmd, words = parse_words(line, comment)
        if not cmd:
            continue
        c = canon_cmd(cmd)
        if c == "G90":
            rel = False
            continue
        if c == "G91":
            rel = True
            continue
        if c == "G92":
            for k, L in ((0, "X"), (1, "Y")):
                if L in words:
                    try:
                        pos[k] = float(words[L])
                    except ValueError:
                        pass
            if "Z" in words:
                try:
                    z = float(words["Z"])
                except ValueError:
                    pass
            continue
        if c not in _MOTION:
            continue
        if first is None:
            first = i
        if "E" in words:
            has_e = True
        if not rotary and any(L in words for L in ROTARY_WORDS):
            rotary = True
        x0, y0 = pos
        nxt = list(pos)
        for k, L in ((0, "X"), (1, "Y")):
            if L in words:
                try:
                    v = float(words[L])
                except ValueError:
                    continue
                nxt[k] = nxt[k] + v if rel else v
        if "Z" in words:
            try:
                v = float(words["Z"])
                z = (z or 0.0) + v if rel else v
            except ValueError:
                pass
        pos = nxt
        zero = (abs(nxt[0] - x0) <= 1e-12 and abs(nxt[1] - y0) <= 1e-12
                and not any(L in words for L in ROTARY_WORDS))
        moves.append((i, x0, y0, nxt[0], nxt[1], z, c, words, zero))
    if first is None:
        return None, [], "height"

    # -- heights: runs of moves at one Z ---------------------------------- #
    runs = []           # [z, index of the run's first move, mm laid]
    for k, mv in enumerate(moves):
        zv = mv[5]
        if not runs or (zv is None) != (runs[-1][0] is None) or (
                zv is not None and abs(zv - runs[-1][0]) > 1e-9):
            runs.append([zv, k, 0.0])
        if mv[6] != "G0" and (not has_e or "E" in mv[7]):
            runs[-1][2] += math.hypot(mv[3] - mv[1], mv[4] - mv[2])
    laid_most = max(r[2] for r in runs)
    level = None
    z_starts = []       # move indices where a height layer starts
    for r in range(len(runs)):
        zr = runs[r][0]
        if zr is None:
            continue
        prev = runs[r - 1][0] if r > 0 else None
        nxt_z = runs[r + 1][0] if r + 1 < len(runs) else None
        if prev is not None and nxt_z is not None and zr > prev \
                and abs(nxt_z - prev) <= 1e-9:
            continue                    # a hop: up, along, back down
        if runs[r][2] <= 0.1 * laid_most and (
                nxt_z is None or nxt_z < zr):
            continue                    # the approach from above, a hop
                                        # down to the next height, the
                                        # lift at the end: nothing laid
        if level is None and nxt_z is not None and nxt_z < zr:
            continue                    # the approach from above
        if level is not None and abs(zr - level) <= 1e-9:
            continue                    # back at the height it left
        k = runs[r][1]
        if level is None:
            # the first layer starts with its first move in X/Y
            end = runs[r + 1][1] if r + 1 < len(runs) else len(moves)
            k = next((j for j in range(k, end)
                      if "X" in moves[j][7] or "Y" in moves[j][7]), k)
        level = zr
        z_starts.append(k)
    if has_e or rotary:
        return first, [moves[k][0] for k in z_starts], "height"

    # -- crossings, within each height ------------------------------------ #
    # The fiber, as STROKES: runs of deposition moves with no sharp turn
    # (`STROKE_TURN_DEG`), each the fiber of one pass, one turnaround loop,
    # one lead-in. A layer change is a long stroke that crosses long strokes
    # of the layer laid so far; a turnaround loop crossing the loop beside
    # it -- coiled loops are wider than the pass spacing -- is not.
    cos_turn = math.cos(math.radians(STROKE_TURN_DEG))
    strokes = []        # [[(move index, (x0, y0, x1, y1)), ...], ...]
    breaks = set(z_starts)
    cur = None
    prev_dir = None
    for k, (_ln, x0, y0, x1, y1, _zv, c, words, zero) in enumerate(moves):
        if k in breaks or zero or c == "G0":
            cur = prev_dir = None
            continue                    # a travel lays nothing
        if c in ("G2", "G3") and ("I" in words or "J" in words):
            try:
                pts = _arc_points(x0, y0, x1, y1, float(words.get("I", 0)),
                                  float(words.get("J", 0)), c == "G2",
                                  0.1)
            except ValueError:
                pts = [(x1, y1)]
        else:
            pts = [(x1, y1)]
        px, py = x0, y0
        for qx, qy in pts:
            dx, dy = qx - px, qy - py
            n = math.hypot(dx, dy)
            if n <= 1e-12:
                continue
            d = (dx / n, dy / n)
            if cur is None or prev_dir is None or \
                    d[0] * prev_dir[0] + d[1] * prev_dir[1] < cos_turn:
                cur = []
                strokes.append(cur)
            cur.append((k, (px, py, qx, qy)))
            prev_dir = d
            px, py = qx, qy
    if not strokes:
        return first, [moves[k][0] for k in z_starts], "height"

    def reach(st):
        """How far a stroke gets from where it began: a pass's length, but
        a turnaround loop's width, however much fiber it coils, so a loop
        is never long and loops crossing loops start no layer."""
        return math.hypot(st[-1][1][2] - st[0][1][0],
                          st[-1][1][3] - st[0][1][1])

    reaches = sorted(reach(st) for st in strokes)
    long_mm = 0.25 * reaches[min(len(reaches) - 1,
                                 int(0.9 * len(reaches)))]
    xs = [v for st in strokes for _k, s in st for v in (s[0], s[2])]
    ys = [v for st in strokes for _k, s in st for v in (s[1], s[3])]
    extent = max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
    seg_mm = sorted(math.hypot(s[2] - s[0], s[3] - s[1])
                    for st in strokes for _k, s in st)
    # a cell about two typical moves wide: few moves per cell, few cells
    # per move
    cell = min(max(2.0 * seg_mm[len(seg_mm) // 2], extent / 256.0),
               extent / 32.0)
    cell = max(cell, 0.01)
    tol = CROSSING_TOL_MM
    # each stroke's first move; a height start is a layer start already
    starts = list(z_starts)
    if not starts:
        # no height named at all: the first layer starts at the first move
        # in X/Y
        starts.append(next((k for k, mv in enumerate(moves)
                            if "X" in mv[7] or "Y" in mv[7]), 0))
    z_iter = iter(sorted(z_starts) + [len(moves)])
    next_z = next(z_iter)
    zeros = [k for k, mv in enumerate(moves) if mv[8]]
    state = {"grid": {}, "laid": []}    # the current layer's long strokes
    stroke_mm = {}
    crossed = False
    begun = starts[0]   # where the current layer began
    last_long = None    # the last move of its last long stroke

    def lay(sid, st, cells):
        grid, laid = state["grid"], state["laid"]
        for (_k, s), cs in zip(st, cells):
            j = len(laid)
            laid.append((sid, s))
            for cl in cs:
                grid.setdefault(cl, []).append(j)

    def lays_over(st, cells, length):
        """True when stroke `st` is laid over the current layer: it crosses
        `CROSSED_FIBERS` of its long strokes, or runs along one of them for
        half the shorter one's length (a layer stacked at the same angle)."""
        grid, laid = state["grid"], state["laid"]
        hit = set()
        along = {}
        for (_k, s), cs in zip(st, cells):
            near = set()
            for (cx, cy) in cs:
                for ddx in (-1, 0, 1):
                    for ddy in (-1, 0, 1):
                        near.update(grid.get((cx + ddx, cy + ddy), ()))
            x_lo, x_hi = min(s[0], s[2]) - tol, max(s[0], s[2]) + tol
            y_lo, y_hi = min(s[1], s[3]) - tol, max(s[1], s[3]) + tol
            for j in near:
                other, seg = laid[j]
                if other in hit:
                    continue
                if max(seg[0], seg[2]) < x_lo or min(seg[0], seg[2]) > x_hi \
                        or max(seg[1], seg[3]) < y_lo \
                        or min(seg[1], seg[3]) > y_hi:
                    continue
                how = _segments_meet(s, seg, tol)
                if how is True:
                    hit.add(other)
                    if len(hit) >= CROSSED_FIBERS:
                        return True
                elif how:
                    along[other] = along.get(other, 0.0) + how
                    if along[other] >= 0.5 * min(length, stroke_mm[other]):
                        return True
        return False

    for sid, st in enumerate(strokes):
        k0 = st[0][0]
        while k0 >= next_z:             # a new height: a new layer
            state["grid"], state["laid"] = {}, []
            begun, last_long = next_z, None
            next_z = next(z_iter)
        if reach(st) < long_mm:
            continue                    # a turn, a loop, a lead-in
        length = sum(math.hypot(s[2] - s[0], s[3] - s[1]) for _k, s in st)
        stroke_mm[sid] = length
        cells = [_seg_cells(s[0], s[1], s[2], s[3], cell) for _k, s in st]
        if state["laid"] and lays_over(st, cells, length):
            # a new layer: from the last move that went nowhere since the
            # layer began (a removed height's remnant), else from the move
            # after the layer's last fiber, so its lead-in turn comes with it
            at = k0
            i = _bisect_left(zeros, k0) - 1
            if i >= 0 and zeros[i] > begun:
                at = zeros[i]
            elif last_long is not None and begun < last_long + 1 < k0:
                at = last_long + 1
            starts.append(at)
            crossed = True
            begun = at
            state["grid"], state["laid"] = {}, []
        last_long = st[-1][0]
        lay(sid, st, cells)
    starts = sorted(set(starts))
    return (first, [moves[k][0] for k in starts],
            "crossings" if crossed else "height")


def text_sha1(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# The structure of a SAVED file, kept out of band
# --------------------------------------------------------------------------- #
# A saved MEWracle file loses its layers on the way out: "Don't adjust height
# per layer" (on by default for Klipper and the MEWron) names the height once,
# and travels-as-G1 spells the layer-change hop like a fiber. Opened again,
# `LineMap.guess_from_z` finds one Z and so ONE layer, and the play bar read
# "Layer 1 / 1" on a ten-layer print. Comments are no channel for groups
# and passes (stripped on save, and that structure is never read from a
# comment); the ``;LAYER:n`` markers carry layers alone to another computer,
# and `layer_starts_from_motion` reads layers from the moves when the file
# has neither. So, for the whole structure, the app keeps what it knew when
# it wrote the file: `Document.remember_structure` records the export's marks
# against the file's COMMAND lines, under the hash of those lines, and
# `recall_structure` gives them back only to a file whose command lines hash
# the same. A foreign file never matches and keeps the Z guess; a hand edit
# to the saved file is a different file and does too. Command lines, not the
# whole text, are hashed so the header's date, a stripped comment and line
# numbers do not matter: they carry no structure.

#: The folder `remember_structure` writes to and `recall_structure` reads
#: (the app sets it); None keeps nothing and recalls nothing.
STRUCTURE_DIR = None
#: How many saved files' structures are kept; the oldest go first.
STRUCTURE_KEEP = 500
STRUCTURE_VERSION = 1


def _command_lines(lines, comment=";"):
    """``[(line index, command)]``: each line that commands something, as
    one normalised string (comment, line number and checksum gone, words
    single-spaced, upper case). Blank and comment-only lines are left
    out."""
    out = []
    for i, raw in enumerate(lines):
        s = raw
        if comment in s or "(" in s:
            s = strip_comment(s, comment)
        if s[:1] in ("N", "n") or s.lstrip()[:1] in ("N", "n"):
            s = _LINE_NUMBER.sub(r"\1", s)
        if "*" in s:
            s = _CHECKSUM.sub("", s)
        s = " ".join(s.split()).upper()
        if s:
            out.append((i, s))
    return out


def _commands_sha1(cmds):
    return text_sha1("\n".join(c for _i, c in cmds))


def _structure_path(sha):
    import os
    return os.path.join(STRUCTURE_DIR, sha + ".json")


def write_structure(text, line_marks, groups, flavor="klipper",
                    saved_text=None):
    """Keep `line_marks` (``[(line index in text, mark)]``, a mark as
    `LineMap` holds one, without its "line") for the file `saved_text`
    (`text` when not given; it may differ from `text` only in lines that
    command nothing, as Strip comments makes it). Returns the path written,
    or None (no folder set, nothing to keep, the two texts command different
    things, or the disk refused: a structure not kept is a file opened with
    the Z guess, never an error)."""
    import os
    if not STRUCTURE_DIR or not line_marks:
        return None
    comment = get_flavor(flavor).comment
    cmds = _command_lines(text.splitlines(), comment)
    if not cmds:
        return None
    if saved_text is not None and saved_text != text:
        if [c for _i, c in _command_lines(saved_text.splitlines(),
                                          comment)] != [c for _i, c in cmds]:
            return None
    ordinal = {}
    o = 0
    for i, _c in cmds:
        ordinal[i] = o
        o += 1
    idx = [i for i, _c in cmds]
    marks = []
    for line, mk in line_marks:
        # a mark on a blank or comment line belongs to the next command
        j = next((ordinal[i] for i in idx[_bisect_left(idx, line):]), None)
        if j is None:
            continue
        m = {k: v for k, v in mk.items() if k != "line"}
        m["cmd"] = j
        marks.append(m)
    if not any(m["kind"] == "layer" for m in marks):
        return None
    sha = _commands_sha1(cmds)
    path = _structure_path(sha)
    try:
        os.makedirs(STRUCTURE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": STRUCTURE_VERSION, "commands": len(cmds),
                       "groups": groups, "marks": marks}, fh)
        os.replace(tmp, path)
        names = [n for n in os.listdir(STRUCTURE_DIR) if n.endswith(".json")]
        if len(names) > STRUCTURE_KEEP:
            full = sorted((os.path.join(STRUCTURE_DIR, n) for n in names),
                          key=os.path.getmtime)
            for old in full[:len(names) - STRUCTURE_KEEP]:
                if old != path:
                    os.remove(old)
    except (OSError, TypeError, ValueError):
        return None
    return path


def recall_structure(text, flavor="klipper", internal=None):
    """The `LineMap` this app recorded when it saved `text` (`write_structure`),
    or None for a file it did not write. `internal` is the text the map will
    describe (the document's base, `to_internal_text` of `text`: the same
    lines); `text` when not given."""
    import os
    if not STRUCTURE_DIR:
        return None
    lines = text.splitlines()
    cmds = _command_lines(lines, get_flavor(flavor).comment)
    if not cmds:
        return None
    path = _structure_path(_commands_sha1(cmds))
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        if int(d.get("version", 0)) != STRUCTURE_VERSION \
                or int(d["commands"]) != len(cmds):
            return None
        marks = []
        for m in d["marks"]:
            m = dict(m)
            m["line"] = cmds[int(m.pop("cmd"))][0]
            marks.append(m)
        body = internal if internal is not None else text
        return LineMap(marks, len(body.splitlines()), d.get("groups", ()),
                       guessed=False, sha1=text_sha1(body))
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def _bisect_left(a, x):
    import bisect
    return bisect.bisect_left(a, x)


_WORD = re.compile(r"([A-Za-z])([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)")
_LINE_NO = re.compile(r"[Nn]\d+")
_G_NUM = re.compile(r"G0*(\d+)")


def parse_words(line, comment=";"):
    """``(command, {letter: value_string})`` for one line, comments and an
    ``N<k>`` line number stripped. ``("", {})`` for a blank or comment line.
    A command that is not letter+number (``PAUSE``, ``OCTO1``) comes back as
    its first token with no words."""
    s = strip_comment(line, comment).strip()
    if not s:
        return "", {}
    toks = s.split()
    if _LINE_NO.fullmatch(toks[0]) and len(toks) > 1:
        toks = toks[1:]
    cmd = toks[0].upper()
    words = {}
    for t in toks[1:]:
        m = _WORD.fullmatch(t)
        if m:
            words[m.group(1).upper()] = m.group(2)
    return cmd, words


def parse_command(line, comment=";"):
    """``parse_words(line, comment)[0]`` without reading the words: a check
    that asks only which command each line is (`machine_render`) walks a
    large file's every line, and the words are most of the parse."""
    s = strip_comment(line, comment).strip()
    if not s:
        return ""
    toks = s.split(None, 2)
    if _LINE_NO.fullmatch(toks[0]) and len(toks) > 1:
        return toks[1].upper()
    return toks[0].upper()


def strip_comment(line, comment=";"):
    """`line` without its comment (`comment` to end of line, and a ``( )``
    paren comment). Quoted strings are respected so Duet's ``M117 "a;b"``
    survives."""
    if '"' not in line and "(" not in line:
        # no quote and no paren comment: the walk below keeps every
        # character up to the first `comment`. Most lines are this, and a
        # large file's open is mostly this parse
        if comment:
            line = line.split(comment, 1)[0]
        return line.rstrip("\r\n")
    out = []
    q = False
    depth = 0
    for ch in line:
        if ch == '"':
            q = not q
        if not q:
            if ch == "(":
                depth += 1
                continue
            if ch == ")" and depth:
                depth -= 1
                continue
            if depth:
                continue
            if comment and ch == comment:
                break
        out.append(ch)
    return "".join(out).rstrip("\r\n")


# =========================================================================== #
# 4. Changes -- the chips
# =========================================================================== #

def fmt_num(v, precision=4):
    """A number the way every MEWracle emitter writes one
    (`scaffold_gcode.format_coord`): fixed precision, trailing zeros and a
    bare dot trimmed, never ``-0``."""
    s = f"{float(v):.{precision}f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


_MOTION = frozenset(("G0", "G1", "G2", "G3"))


def canon_cmd(cmd):
    """``G01`` -> ``G1``, ``g0`` -> ``G0``; anything else upper-cased."""
    c = cmd.upper()
    m = _G_NUM.fullmatch(c)
    return "G" + m.group(1) if m else c


@dataclass
class Change:
    """Base of every chip. `id` is stable for the document's life; `on` is the
    tick in Changes (unticking is how a chip is undone); `label` is what the
    chip says (empty = `chip_label` writes one). `typed` marks a chip that
    `record_edit` made from typing rather than from a form: the page keeps
    those only until the design changes, unless the user keeps them.
    `what` is the kind the edit was made as -- an `INSERT_FORMS` kind,
    "custom" (your own lines), a `TRANSFORM_KINDS` op or "line" -- and stays
    when its g-code is changed by hand. `saved` is the name of the library
    entry it is kept as ("" = not saved). Subclasses add their fields."""
    id: str = ""
    on: bool = True
    label: str = ""
    kind: str = "change"
    typed: bool = False
    what: str = ""
    saved: str = ""

    def to_dict(self):
        d = {}
        for k, v in self.__dict__.items():
            d[k] = v.to_dict() if hasattr(v, "to_dict") else copy.deepcopy(v)
        if isinstance(d.get("key"), tuple):
            d["key"] = list(d["key"])
        if isinstance(d.get("at"), tuple):
            d["at"] = list(d["at"])
        return d


@dataclass
class Anchor:
    """Where an inserted line goes, in structural terms.

    unit   "start" | "end" | "header" | "group" | "layer" | "pass" | "line"
    side   "before" | "after"
    key    the unit's identity, piece first: () for start/end/header,
           (piece, group), (piece, group, layer), (piece, group, layer, pass),
           or a full line key (piece, section, group, layer, pass, k).
    """
    unit: str = "start"
    side: str = "after"
    key: tuple = ()

    def to_dict(self):
        return {"unit": self.unit, "side": self.side, "key": list(self.key)}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("unit", "start"), d.get("side", "after"),
                   tuple(d.get("key", ())))


@dataclass
class Insert(Change):
    """Lines the user added -- typed, or from an Insert form -- at an anchor.

    `lines` are in `flavor`, the flavor they were written in; the render
    re-spells them into the document's flavor, so a flavor change and its
    reverse return the exact text typed. `command` keeps an Insert form's
    ``{"kind": ..., fields...}`` so it is re-written, not converted."""
    kind: str = "insert"
    lines: list = field(default_factory=list)
    anchor: Anchor = field(default_factory=Anchor)
    command: object = None
    flavor: str = ""
    shift: dict = field(default_factory=dict)


@dataclass
class LineEdit(Change):
    """A change to ONE generated line, recorded against its line key
    (piece, section, group, layer, pass, k).

    op "words"    set/drop letter words: `set` {letter: value}, `drop` [letters]
    op "replace"  the line becomes `text` verbatim, in `flavor`
    op "delete"   the line is removed

    `shift` {letter: mm/deg}: an edit typed in an INCREMENTAL export. The
    machine runs deltas, so changing one moves that line and every move
    after it; the shift is that move, applied from this line on in both
    forms (`_apply_shifts`). An Insert carries one for the same reason.

    `more` (op "delete" only): the keys of the lines removed together with
    `key` -- one deletion of a block is ONE chip, not one per line. `unit`
    "layer" says the block is a whole layer, so the chip reads "Removed
    layer 3 of Scaffold 1".

    Carried over while a line with that key exists in the new base.

    `at`: a line that is not a generated line of its own -- a join travel,
    the move to G-code zero, an output option's line, a Start/End block
    line, the flavor's header, one place a rule fills in, an array copy of
    an insert, one line of a split -- is edited by its rendered identity
    (`RLine.uid`), on the rendered lines. `stage` "" applies it before the
    export re-spells the lines (the text is G-code, in `flavor`); "export"
    after (a line only the export has: the Start/End blocks, the zero
    move, the custom line; the text is as typed). `key`, when set on such
    an edit, only says where it sits (the tree, its label): the generated
    line it belongs to.
    Carried over while a line with that identity is rendered."""
    kind: str = "line_edit"
    key: tuple = ()
    op: str = "words"
    set: dict = field(default_factory=dict)
    drop: list = field(default_factory=list)
    text: str = ""
    flavor: str = ""
    shift: dict = field(default_factory=dict)
    more: list = field(default_factory=list)
    unit: str = ""
    at: tuple = ()
    stage: str = ""


#: Rule "where" choices, with the plain label the form shows.
WHERE_CHOICES = (
    ("start", "Start of file"),
    ("end", "End of file"),
    ("before_first_print", "Before the first printing move"),
    ("before_layer", "Before each layer"),
    ("after_layer", "After each layer"),
    ("before_group", "Before each scaffold or piece"),
    ("after_group", "After each scaffold or piece"),
    ("before_travel", "Before each travel"),
    ("after_travel", "After each travel"),
    ("every_n_moves", "Every N moves"),
    ("every_n_mm", "Every N mm of fiber"),
    ("every_n_seconds", "Every N seconds of print time"),
    ("at_z", "When Z first reaches a height"),
)
_WHERE_LABEL = dict(WHERE_CHOICES)

#: The placeholders a rule's text may carry, with what each is.
PLACEHOLDERS = (
    ("layer", "layer number, counted from 1 across the file"),
    ("z", "nozzle height, mm"),
    ("piece", "piece number, from 1"),
    ("time_s", "print time so far, seconds"),
    ("x", "nozzle X, mm"),
    ("y", "nozzle Y, mm"),
    ("n", "how many times this rule has fired, from 1"),
    ("date", "print date"), ("time", "print time"),
    ("filename", "export file name"),
    ("collector", "the collector: a flat plate, or a mandrel and its diameter"),
    ("zero", "where the g-code's X0 and Z0 are on the collector"),
    ("spacing", "scaffold spacing, mm (flat plate)"),
    ("layers", "layers per scaffold"),
    ("rotation", "layer rotation, degrees (flat plate)"),
    ("winding_angle", "winding angle from the mandrel axis, degrees "
                      "(mandrel)"),
    ("scaffold_length", "scaffold length along the mandrel, mm (mandrel)"),
    ("scaffold_count", "number of scaffolds"), ("pressure", "pressure, psi"),
    ("voltage", "voltage, kV"), ("temperature", "temperature, °C"),
    ("gap", "nozzle-to-collector gap, mm"),
    ("nozzle_height", "nozzle height above the collector, mm (the same "
                      "number as gap)"),
    ("speed", "speed, mm/min"),
    ("nozzle", "nozzle size"), ("polymer", "polymer name"),
    ("eta_ref", "melt viscosity at the reference temperature, Pa·s"),
    ("ea", "melt activation energy Ea, J/mol"),
    ("t_ref", "reference temperature for the viscosity, °C"),
    ("notes", "print notes"), ("version", "MEWracle build"),
    ("pressure_command", "flavor's pressure setpoint"),
    ("voltage_command", "flavor's voltage setpoint"),
    ("temperature_command", "flavor's temperature setpoint"),
)
#: Placeholders only a RULE can fill: they take the value at the moment the
#: rule fires (a layer, a move). The start and end blocks are written once,
#: before any move exists, so they know only the rest.
RULE_ONLY_PLACEHOLDERS = frozenset(
    ("layer", "z", "piece", "time_s", "x", "y", "n"))
#: Setpoint lines that print nothing when the flavor has no such command
#: (Klipper's built-in profile has none); every other placeholder keeps its
#: line even when its value is blank.
OPTIONAL_PLACEHOLDERS = frozenset(
    ("pressure_command", "voltage_command", "temperature_command"))
BLOCK_PLACEHOLDERS = tuple(
    (k, d) for k, d in PLACEHOLDERS if k not in RULE_ONLY_PLACEHOLDERS)
RULE_PLACEHOLDERS = tuple(
    (k, d) for k, d in PLACEHOLDERS if k in RULE_ONLY_PLACEHOLDERS)
NOT_SET = "(not set)"
_PLACEHOLDER = re.compile(r"\[\[([A-Za-z_]+)\]\]")

#: A print value of None means "does not describe this print" (a plate's
#: spacing on a mandrel print): a comment line naming it is left out of the
#: header, where "(not set)" would claim a setting the print never had.
NOT_THIS_PRINT = None

DEFAULT_START_BLOCK = "\n".join((
    "; MEWracle print: [[filename]]", "; Date: [[date]] [[time]]",
    "; Collector: [[collector]]", "; Zero: [[zero]]",
    "; Scaffolds: [[scaffold_count]]", "; Spacing (mm): [[spacing]]",
    "; Rotation (deg): [[rotation]]",
    "; Winding angle (deg from the mandrel axis): [[winding_angle]]",
    "; Scaffold length (mm): [[scaffold_length]]",
    "; Layers: [[layers]]",
    "; Pressure (psi): [[pressure]]", "; Voltage (kV): [[voltage]]",
    "; Temperature (C): [[temperature]]", "; Gap (mm): [[gap]]",
    "; Speed (mm/min): [[speed]]", "; Nozzle: [[nozzle]]",
    "; Polymer: [[polymer]]", "; MEWracle: [[version]]",
    "; Notes: [[notes]]", "[[pressure_command]]", "[[voltage_command]]",
    "[[temperature_command]]"))


@dataclass
class Rule(Change):
    """What g-code, where, how often -- the whole builder.

    text         the g-code, one or more lines, in `flavor`, with
                 placeholders (`PLACEHOLDERS`) in double square brackets --
                 Duet uses {...} for its own expressions.
    where        one of `WHERE_CHOICES`.
    every        for the layer and group choices: every Nth (1 = each).
    first/last   optional 1-based range of layers or groups, inclusive.
    n            the N of the every-N choices, or the Z height for "at_z".
    name         the rule's name, shown in the gutter on its lines.
    command      an Insert form's ``{"kind": ..., fields...}`` when the text
                 is a ready-made command: it is re-written for the flavor, as
                 an Insert's is. None once the text is the user's own."""
    kind: str = "rule"
    text: str = ""
    where: str = "after_layer"
    every: int = 1
    first: object = None
    last: object = None
    n: float = 0.0
    name: str = ""
    flavor: str = ""
    command: object = None


#: Transform kinds, with the plain label the menu shows. The first group
#: changes generated moves inside a scope and survives regeneration like a
#: hand edit; the second re-spells the whole file.
TRANSFORM_KINDS = (
    ("offset", "Offset X/Y/Z"),
    ("scale", "Scale X/Y/Z"),
    ("layer_shift", "Layer shift"),
    ("speed_set", "Set speed"),
    ("speed_scale", "Scale speed"),
    ("repeat", "Repeat a block"),
    ("round", "Round coordinates"),
    ("arcs_to_chords", "Arcs to straight segments"),
    ("to_relative", "Absolute to relative"),
    ("to_absolute", "Relative to absolute"),
    ("strip_comments", "Strip comments"),
    ("strip_e", "Strip slicer E words"),
    ("strip_line_numbers", "Strip line numbers"),
)
_TRANSFORM_LABEL = dict(TRANSFORM_KINDS)
_GEOMETRIC = frozenset(("offset", "scale", "layer_shift", "speed_set",
                        "speed_scale", "repeat", "round", "arcs_to_chords"))

#: Each transform's fields: (name, label, unit, default).
TRANSFORM_FIELDS = {
    "offset": (("dx", "X", "mm", 0.0), ("dy", "Y", "mm", 0.0),
               ("dz", "Z", "mm", 0.0), ("rz", "Rotate about Z", "°", 0.0),
               ("cx", "Pivot X", "mm", None), ("cy", "Pivot Y", "mm", None)),
    "scale": (("px", "X", "%", 100.0), ("py", "Y", "%", 100.0),
              ("pz", "Z", "%", 100.0)),
    "speed_set": (("mm_s", "Speed", "mm/s", 10.0),),
    "speed_scale": (("percent", "Speed", "%", 100.0),),
    "repeat": (("copies", "Extra copies", "", 1), ("dx", "Shift X", "mm", 0.0),
               ("dy", "Shift Y", "mm", 0.0), ("dz", "Shift Z", "mm", 0.0)),
    "round": (("decimals", "Decimals", "", 3),),
    "arcs_to_chords": (("max_seg", "Longest segment", "mm", 0.1),),
    "layer_shift": (("dx", "X step", "µm", 0.0), ("dy", "Y step", "µm", 0.0),
                    ("along", "Along the print", "µm", 0.0),
                    ("amp", "Amplitude", "µm", 0.0),
                    ("amp_pct", "Amplitude", "%", 0.0),
                    ("radius", "Radius", "µm", 0.0),
                    ("profile", "Profile", "", "ramp"),
                    ("period", "Wave period", "layers", 10),
                    ("cycle", "Cycle", "", "")),
}

#: A transform field that is a choice: {op: {field: ((value, label), ...)}}.
#: The form shows the labels; the chip stores the value.
TRANSFORM_CHOICES = {
    "layer_shift": {"profile": (
        ("ramp", "Ramp: layer n moves n steps"),
        ("constant", "Constant: every layer moves one step"),
        ("alternate", "Alternate: +1, -1, +2, -2 ... (branching)"),
        ("wave", "Wave: a sine of one step, per period"),
        ("cycle", "Cycle: your list of steps, repeating"))},
}

#: A field's tooltip when its label alone does not say enough.
TRANSFORM_TIPS = {
    "offset": {
        "rz": "Turn the selection about a vertical axis through the pivot, "
              "in degrees, counter-clockwise seen from above. Arcs turn "
              "with it.",
        "cx": "The pivot's X, in mm, where the selection is before the "
              "move. Blank: the middle of the selection.",
        "cy": "The pivot's Y, in mm. Blank: the middle of the selection.",
    },
    "layer_shift": {
        "dx": "Move each layer in X by this much per step, in µm. With Ramp "
              "the first selected layer stays put and layer n moves n "
              "steps: walls tilt, and big steps (hundreds of µm) build "
              "overhangs.",
        "dy": "Move each layer in Y by this much per step, in µm.",
        "along": "Slide each deposition pass along its OWN print direction "
                 "by this much per step, in µm: the phase shift. Negative "
                 "is backward, against the travel -- the correction for a "
                 "wavy wall that tilts forward (12-18 µm per layer in "
                 "Liashenko 2020). The two ends of every pass stay put, so "
                 "the turnarounds are untouched. Needs a generated flat "
                 "scaffold: an opened file has no passes.",
        "amp": "Grow the wave of each pass by this much per step at its "
               "peak, in µm, across the print direction (Devlin 2024's "
               "amplitude). The pass ends stay on the grid. A straight "
               "pass has no wave and is left alone.",
        "amp_pct": "Grow the wave of each pass by this percent of its own "
                   "amplitude per step (Liashenko 2020: +2% per layer "
                   "stood a sinusoidal wall up straight). Adds to the µm "
                   "amplitude above.",
        "radius": "Grow each layer about its own centre so its farthest "
                  "point moves out this much per step, in µm: a circle's "
                  "radius grows by exactly this. Negative tapers inward "
                  "(domes).",
        "profile": "How the step grows from layer to layer, counting the "
                   "selected layers from 0.",
        "period": "Wave only: the number of layers in one full sine.",
        "cycle": "Cycle only: the steps for layers 0, 1, 2 ... as a list, "
                 "e.g. 0, 1, 3, 6. It repeats past its end, like an angle "
                 "cycle.",
    },
}

#: Layer shift profiles, in TRANSFORM_CHOICES order.
LAYER_SHIFT_PROFILES = tuple(v for v, _l in
                             TRANSFORM_CHOICES["layer_shift"]["profile"])


def parse_shift_cycle(text):
    """The numbers of a layer shift's Cycle, ``"0, 1, 3, 6"`` -> ``[0.0, 1.0,
    3.0, 6.0]``, split on commas, semicolons or spaces. Raises ValueError
    with a sentence on a word that is not a number."""
    out = []
    for tok in re.split(r"[,;\s]+", str(text or "").strip()):
        if not tok:
            continue
        try:
            out.append(float(tok))
        except ValueError:
            raise ValueError("Cycle takes numbers, e.g. 0, 1, 3, 6 -- %r is "
                             "not one." % tok)
    return out


def layer_shift_factor(profile, n, period=10, cycle=()):
    """How many steps the selection's `n`-th layer (0-based) moves.

    "ramp" n -- Devlin 2024's ``pha*(n-1)`` and Liashenko 2020's
    layer-on-layer shift, the first layer the reference; "constant" 1;
    "alternate" 0, +1, -1, +2, -2 ... -- Liashenko's branching, where tilted
    layers alternate between the two branches so both grow at the same
    rate; "wave" ``sin(2 pi n / period)`` -- the periodic dome radius;
    "cycle" ``cycle[n % len]``, the user's own list."""
    if profile == "constant":
        return 1.0
    if profile == "alternate":
        return 0.0 if n == 0 else (1.0 if n % 2 else -1.0) * ((n + 1) // 2)
    if profile == "wave":
        return math.sin(2.0 * math.pi * n / max(1, int(period or 1)))
    if profile == "cycle":
        return float(cycle[n % len(cycle)]) if cycle else 0.0
    return float(n)


_ROTARY_MOVE = re.compile(r"^[ \t]*(?:[Nn]\d+\s+)?[Gg]0*[0-3](?![0-9])"
                          r"[^;(\n]*?\s[ABCUVWabcuvw][-+.\d]", re.M)


def has_rotary_words(text):
    """True when any move in `text` turns a rotary axis: a four- or
    five-axis file, which the layer shift does not take (its X/Y are not a
    flat bed's). One regex pass, as the tools ask it on every selection."""
    return _ROTARY_MOVE.search(text or "") is not None


#: Where Repeat puts an edit's copies when it is not a pattern: right after
#: the edit, `copies` times.
REPEAT_HERE = "here"

#: A new Repeat on an edit: after each layer, unshifted. `where` is
#: `REPEAT_HERE` or a `WHERE_CHOICES` key, with a Rule's every / first /
#: last / n; `copies` counts only for `REPEAT_HERE`; the k-th copy is
#: shifted k times (dx, dy, dz) mm.
EDIT_REPEAT_DEFAULTS = {"where": "after_layer", "every": 1, "first": None,
                        "last": None, "n": 0.0, "copies": 1, "dx": 0.0,
                        "dy": 0.0, "dz": 0.0}

#: A Repeat stops writing copies past this many lines, so "every 1 move" on
#: a big edit cannot hang the page.
REPEAT_MAX_LINES = 200000


@dataclass
class Scope:
    """What a transform acts on: "all", "layers" (`first`..`last`, 1-based
    across the file), "group" (`first`..`last`, 1-based scaffold/base
    groups), "piece" (`first`..`last`, 1-based pieces), "lines", "set" or
    "edit".

    A change on a part (all, layers, group, piece) changes the lines the
    edits sitting in that part add, as well as the generated ones: the
    tree's rule that a part holds what is under it. "lines" is exactly
    the generated lines whose keys `first` lists (a text selection);
    "set" is the union of the sub-scopes `first` lists, as dicts (a
    multi-selection: two edits, a layer and some lines).

    "edit" makes the transform an OPERATION ON AN EDIT: `first` is the id of
    the edit it sits under (an Insert, Rule, LineEdit, Transform, or another
    operation), and it acts on everything that edit writes -- its own lines
    and every copy a Repeat under it makes. A geometric op changes those
    lines; "repeat" writes the edit again at a pattern (its params:
    `EDIT_REPEAT_DEFAULTS`). Removing, or switching off, the edit takes its
    operations with it."""
    kind: str = "all"
    first: object = None
    last: object = None

    def to_dict(self):
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(d.get("kind", "all"), d.get("first"), d.get("last"))


@dataclass
class Transform(Change):
    """A transform chip: `op` from `TRANSFORM_KINDS`, its `params`
    (`TRANSFORM_FIELDS`), its `scope`."""
    kind: str = "transform"
    op: str = "offset"
    params: dict = field(default_factory=dict)
    scope: Scope = field(default_factory=Scope)


@dataclass
class Piece:
    """One piece of a stitched file (spec section 8).

    source   "designer" (the document's base), "file" or "snippet"
    text     the piece's g-code for file/snippet, normalised on add
             (mm, absolute) by `normalise_piece_text`
    offset   (dx, dy, dz) mm; rotation deg about the piece's XY centre;
    mirror   "", "x" (flip X) or "y" (flip Y), about the same centre;
    scale    XY factor -- it changes fiber spacing, and the form says so
    layers   optional (first, last) 1-based layer range to include
    drop_frame  drop this piece's own header and footer (lines before its
             first move and after its last)
    lift     mm the join INTO this piece lifts by, 0 = a straight travel.
             In MEW the join lays fiber, and the preview shows it.
    layer_starts  optional 0-based lines of `text` where its layers begin
             (a Move's copy knows them); None: guessed from Z changes."""
    id: str = ""
    source: str = "designer"
    name: str = ""
    text: str = ""
    flavor: str = "klipper"
    offset: tuple = (0.0, 0.0, 0.0)
    rotation: float = 0.0
    mirror: str = ""
    scale: float = 1.0
    layers: object = None
    drop_frame: bool = True
    lift: float = 0.0
    on: bool = True
    layer_starts: object = None

    def to_dict(self):
        d = dict(self.__dict__)
        d["offset"] = list(self.offset)
        if self.layers is not None:
            d["layers"] = list(self.layers)
        if self.layer_starts is not None:
            d["layer_starts"] = list(self.layer_starts)
        else:
            d.pop("layer_starts", None)
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        d["offset"] = tuple(d.get("offset", (0.0, 0.0, 0.0)))
        if d.get("layers") is not None:
            d["layers"] = tuple(d["layers"])
        return cls(**d)

    def is_plain(self):
        """True when the piece form changes nothing."""
        return (tuple(self.offset) == (0.0, 0.0, 0.0) and not self.rotation
                and not self.mirror and self.scale == 1.0
                and self.layers is None)


def change_from_dict(d):
    """The chip a `Change.to_dict()` wrote."""
    d = dict(d)
    kind = d.get("kind")
    if kind == "insert":
        d["anchor"] = Anchor.from_dict(d.get("anchor", {}))
        return Insert(**d)
    if kind == "line_edit":
        d["key"] = tuple(d.get("key", ()))
        d["at"] = tuple(d.get("at", ()))
        return LineEdit(**d)
    if kind == "rule":
        return Rule(**d)
    if kind == "transform":
        d["scope"] = Scope.from_dict(d.get("scope"))
        return Transform(**d)
    raise ValueError("unknown chip kind %r" % (kind,))


# -- word patches ------------------------------------------------------------ #
def _simple_tokens(line):
    """``(tokens, ending)`` when every token after the command is one
    letter + number, each letter once; else None."""
    body = line.rstrip("\r\n")
    toks = body.split()
    if not toks or not re.fullmatch(r"[A-Za-z][-+0-9.]*", toks[0]):
        return None
    letters = []
    for t in toks[1:]:
        if not _WORD.fullmatch(t):
            return None
        letters.append(t[0].upper())
    if len(set(letters)) != len(letters):
        return None
    return toks, line[len(body):]


def apply_word_patch(line, set_, drop):
    """`line` with letter words set and dropped; None when `line` is not a
    simple word line. A letter the line lacks is appended."""
    st = _simple_tokens(line)
    if st is None:
        return None
    toks, ending = st
    have = {t[0].upper() for t in toks[1:]}
    out = [toks[0]]
    for t in toks[1:]:
        L = t[0].upper()
        if L in drop:
            continue
        out.append(t[0] + set_[L] if L in set_ else t)
    out += [L + v for L, v in set_.items() if L not in have]
    return " ".join(out) + ending


def word_patch(old, new):
    """``(set, drop)`` turning `old` into `new` by letter words alone, or
    None (a different command, or a change words cannot express)."""
    a, b = _simple_tokens(old), _simple_tokens(new)
    if a is None or b is None or canon_cmd(a[0][0]) != canon_cmd(b[0][0]) \
            or a[0][0] != b[0][0]:
        return None
    va = {t[0].upper(): t[1:] for t in a[0][1:]}
    vb = {t[0].upper(): t[1:] for t in b[0][1:]}
    set_ = {L: v for L, v in vb.items() if va.get(L) != v}
    drop = [L for L in va if L not in vb]
    if (apply_word_patch(old, set_, drop) or "").rstrip("\r\n") \
            != new.rstrip("\r\n"):
        return None
    return set_, drop


# =========================================================================== #
# 5. Rendering
# =========================================================================== #

@dataclass
class RLine:
    """One rendered line and where it came from.

    origin  "base" (a generator line), "edit" (one a LineEdit changed),
            "transform" (one a transform changed or made), "insert", "rule",
            "option" (the heat-and-wait or the custom line), "join" (a stitch
            travel), "flavor" (the flavor's header/footer)
    chip    the chip (or piece) id that made it, "" for an untouched line
    key     the line key of a generated line, () otherwise
    readonly  rule, option, join, flavor and repeated lines: edit the chip,
              not its output
    uid     identity shared by the export and parse forms of one line
    src     the line's G-code before `machine_render` re-spelled it (no line
            ending), None when the export form was not re-spelled
    pos     incremental export only: the absolute position {letter: mm/deg}
            before this line, so typing a delta can be read back"""
    text: str
    origin: str = "base"
    chip: str = ""
    key: tuple = ()
    readonly: bool = False
    uid: tuple = ()
    src: object = None
    pos: object = None


@dataclass
class Rendered:
    """A render: `text`, `lines` (one `RLine` each, joining to `text`),
    `unplaced` (ids of chips whose anchor is not in this base -- the
    "couldn't carry over" list), `form` ("export" or "parse")."""
    text: str
    lines: list
    unplaced: list
    form: str = "export"


@dataclass
class _Slot:
    """A generated line on its way through the render. `text` may hold more
    than one line once a transform splits it (arcs to chords)."""
    text: str
    key: tuple = ()
    origin: str = "base"
    chip: str = ""
    pre: str = ""             # before any LineEdit, for no-op detection
    deleted: bool = False
    user: bool = False        # verbatim user text: output options skip it
    flavor: str = ""
    readonly: bool = False
    uid: tuple = ()
    piece: int = 0            # 0-based piece index
    src: object = None        # before any operation on an edit: what a
                              # Repeat of that edit copies
    copy_of: object = None    # a Repeat-a-block copy: (the _Slot it
                              # copies, its copy number k, the shift)


DEFAULT_FEED = 600.0          # mm/min until the first F: the consumers' clock


def _arc_len(x0, y0, x1, y1, i, j, cw):
    cx, cy = x0 + i, y0 + j
    r = math.hypot(i, j)
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    d = (a0 - a1) if cw else (a1 - a0)
    while d <= 1e-12:
        d += 2.0 * math.pi
    return r * d


class _LazyWalk:
    """`Document._walk`'s list, worked out on first use. Reads as the list
    does: index, len, truth, iteration."""

    __slots__ = ("_fn", "_v")

    def __init__(self, fn):
        self._fn, self._v = fn, None

    def _get(self):
        if self._v is None:
            self._v = self._fn()
        return self._v

    def __getitem__(self, i):
        return self._get()[i]

    def __len__(self):
        return len(self._get())

    def __iter__(self):
        return iter(self._get())


class _Walker:
    """The nozzle, line by line: position, feed, mode, clock, fiber length.
    Absolute until a G91; X/Y/Z unknown (None) until named, then 0-based for
    arithmetic, matching the parser's assumed start."""

    def __init__(self, comment=";"):
        self.x = self.y = self.z = None
        self.f = DEFAULT_FEED
        self.rel = False
        self.t = 0.0
        self.mm = 0.0
        self.moves = 0
        self.comment = comment

    def step(self, line):
        """Advance over one line; returns (cmd, words, dist, zero_length)."""
        cmd, words = parse_words(line, self.comment)
        c = canon_cmd(cmd) if cmd else ""
        if c == "G90":
            self.rel = False
        elif c == "G91":
            self.rel = True
        if c not in _MOTION:
            return c, words, 0.0, False
        x0, y0, z0 = (self.x or 0.0), (self.y or 0.0), (self.z or 0.0)

        def ax(L, cur):
            if L not in words:
                return cur
            try:
                v = float(words[L])
            except ValueError:
                return cur
            return (cur or 0.0) + v if self.rel else v
        nx, ny, nz = ax("X", self.x), ax("Y", self.y), ax("Z", self.z)
        if "F" in words:
            try:
                self.f = float(words["F"])
            except ValueError:
                pass
        x1, y1, z1 = (nx or 0.0), (ny or 0.0), (nz or 0.0)
        if c in ("G2", "G3") and ("I" in words or "J" in words):
            try:
                i, j = float(words.get("I", 0)), float(words.get("J", 0))
                d = math.hypot(_arc_len(x0, y0, x1, y1, i, j, c == "G2"),
                               z1 - z0)
            except ValueError:
                d = math.dist((x0, y0, z0), (x1, y1, z1))
        else:
            d = math.dist((x0, y0, z0), (x1, y1, z1))
        zero = d <= 1e-12 and not any(L in words for L in ROTARY_WORDS)
        self.x, self.y, self.z = nx, ny, nz
        self.moves += 1
        self.mm += d
        if self.f > 0:
            self.t += d / (self.f / 60.0)
        return c, words, d, zero

    def snapshot(self):
        return {"x": self.x or 0.0, "y": self.y or 0.0, "z": self.z or 0.0,
                "t": self.t, "mm": self.mm, "moves": self.moves,
                "f": self.f}


def _set_words(line, values, precision=4, add=False):
    """`line` with numeric words replaced by `values` {letter: float}.
    `add`: a letter the line does not name yet is appended after the
    command's last word, in `values`' order (never into a comment)."""
    body = line.rstrip("\r\n")
    ending = line[len(body):]
    out = []
    seen = set()
    for t in body.split():
        m = _WORD.fullmatch(t)
        if m and m.group(1).upper() in values and out:
            seen.add(m.group(1).upper())
            out.append(t[0] + fmt_num(values[m.group(1).upper()], precision))
        else:
            out.append(t)
    if add:
        order = list(values)
        for L, v in values.items():
            if L in seen:
                continue
            # before the first named word that comes after it in `values`
            # (X before Y), else before any comment, else at the end
            k = len(out)
            for j, t in enumerate(out):
                m = _WORD.fullmatch(t)
                if t.startswith(";") or t.startswith("(") or (
                        j and m and m.group(1).upper() in order
                        and order.index(m.group(1).upper())
                        > order.index(L)):
                    k = j
                    break
            out.insert(k, L + fmt_num(v, precision))
            seen.add(L)
    return " ".join(out) + ending


def motion_xy_centre(lines, chosen, skip=()):
    """The middle of the XY extent the absolute moves among `lines`
    (0-based indices in `chosen`) end at, where the machine is after
    each, a missing X or Y carried from the move before. None when the
    chosen lines reach no XY point. A Move's turn pivots here by
    default (`Document._move_rotate`)."""
    rel = False
    x = y = None
    lo = hi = None
    for i, t in enumerate(lines):
        if i in skip:
            continue
        cmd, words = parse_words(t)
        cc = canon_cmd(cmd) if cmd else ""
        if cc in ("G90", "G91"):
            rel = cc == "G91"
        if cc not in _MOTION or rel:
            continue
        try:
            x = float(words["X"]) if "X" in words else x
            y = float(words["Y"]) if "Y" in words else y
        except ValueError:
            continue
        if i in chosen and x is not None and y is not None:
            lo = (x, y) if lo is None else (min(lo[0], x), min(lo[1], y))
            hi = (x, y) if hi is None else (max(hi[0], x), max(hi[1], y))
    if lo is None:
        return None
    return ((lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0)


def _shift_line(line, shift, k):
    """A motion line's X/Y/Z moved `k` times `shift` {letter: mm}; any other
    line as it is. Only the letters the line names move, as Repeat a
    block's copies do."""
    if not k or not any(shift.values()):
        return line
    cmd, words = parse_words(line)
    if canon_cmd(cmd or "") not in _MOTION:
        return line
    vals = {}
    for L, d in shift.items():
        if d and L in words:
            try:
                vals[L] = float(words[L]) + k * d
            except ValueError:
                pass
    return _set_words(line, vals) if vals else line


def _put_words(line, values, precision=4):
    """`line` with `values` {letter: float} written: a word the line names
    is replaced, a missing one is added after its last X/Y/Z/I/J/R word (or
    after the command) -- a move the layer shift moves in X must name its X
    even when the file left it modal. A comment is kept as it was."""
    body = line.rstrip("\r\n")
    ending = line[len(body):]
    code, sep, comment = body.partition(";")
    toks = code.split()
    if not toks:
        return line
    head = 2 if re.fullmatch(r"[Nn]\d+", toks[0]) and len(toks) > 1 else 1
    have, at = set(), head
    for j in range(head, len(toks)):
        m = _WORD.fullmatch(toks[j])
        if not m:
            continue
        L = m.group(1).upper()
        if L in values:
            toks[j] = toks[j][0] + fmt_num(values[L], precision)
            have.add(L)
        if L in "XYZIJR":
            at = j + 1
    toks[at:at] = [L + fmt_num(values[L], precision) for L in "XYIJR"
                   if L in values and L not in have]
    out = " ".join(toks)
    if sep:
        out += (" " if code[-1:].isspace() else "") + sep + comment
    return out + ending


#: The layer shift's phase slide tapers to nothing over this many |D| from
#: each pass end. 2 keeps the slide's slope at 1/2, so it never folds a pass
#: back on itself (`Document._layer_shift`).
_PHASE_RAMP = 2.0


def _arc_centre(start, end, centre):
    """`centre` moved onto the perpendicular bisector of `start`-`end`, so
    the arc's two radii agree. A start shifted by a different amount than
    the arc (an arc that opens a layer, its start in the layer below) would
    otherwise leave the controller a centre off its own circle; where the
    two moved together the centre is already on it and nothing changes. A
    full circle (start on end) keeps its centre."""
    (sx, sy), (ex, ey), (cx, cy) = start, end, centre
    ux, uy = ex - sx, ey - sy
    d = math.hypot(ux, uy)
    if d < 1e-9:
        return centre
    bx, by = -uy / d, ux / d
    mx, my = (sx + ex) / 2.0, (sy + ey) / 2.0
    k = (cx - mx) * bx + (cy - my) * by
    return mx + k * bx, my + k * by


def _split_text_lines(text):
    return text.splitlines(keepends=True) or [text]


class Document:
    """A G-code document: a base, its line map, a flavor, output options and
    chips. See the module docstring for the model.

    source   "scaffold" (follows the designer), "calibration" or "file"
    linked   True while it follows the designer; False after `detach()`"""

    def __init__(self, base_text="", line_map=None, flavor="klipper",
                 options=None, source="scaffold", linked=True, changes=(),
                 pieces=()):
        self.base_text = base_text
        self.line_map = line_map
        self.flavor = flavor
        self.options = options if options is not None else OutputOptions()
        self.source = source
        self.linked = bool(linked)
        self.changes = list(changes)
        self.pieces = list(pieces)
        self._next_id = 1
        # array copies: {(piece, copy group): (piece, original group)}. Set
        # by the page from the design each render, never saved: an edit on
        # the original scaffold lands on every copy of it too.
        self.copies = {}
        # the Machine page's feed time unit, "min" or "s": a split-feed
        # flavor's parse form says it (G75/G76) so the reader times it. Set
        # by the page like `copies`, never saved.
        self.feed_unit = "min"

    # -- construction ------------------------------------------------------ #
    @classmethod
    def from_file(cls, text, flavor=None):
        """A detached document over an imported file: flavor detected from
        its syntax when not given, layers found by `LineMap.guess`, output
        options all off (an imported file is never rewritten until asked).
        A file this app saved gets the structure it was saved with
        (`recall_structure`), since its height may have been named once."""
        flavor = flavor or detect_flavor(text)
        raw, text = text, to_internal_text(text, flavor)
        lm = recall_structure(raw, flavor, text) or LineMap.guess(
            text, get_flavor(flavor).comment)
        return cls(text, lm, flavor, OutputOptions(), source="file",
                   linked=False)

    def remember_structure(self, saved_text, rendered=None):
        """Keep this export's structure for the file `saved_text` that Save
        (or Send, or History's copy) writes from it, so opening that file
        again finds its scaffolds and layers (`recall_structure`). The marks
        are read off the render's line keys, which say which group, layer
        and pass each line came from whatever the output options removed.
        Nothing is kept for a structure that was itself guessed."""
        lm = self._map()
        if lm.guessed:
            return None
        r = rendered or self.render()
        links = {int(m["line"]) for m in lm.marks if m["kind"] == "link"}
        gid, groups, marks = {}, [], []
        prev = None
        n = 0
        for rl in r.lines:
            at, n = n, n + max(1, rl.text.count("\n"))
            k = rl.key
            if not k or len(k) < 5 or k[0] == "machine" or k[1] == "header":
                continue
            pid, sec, g, L, P = k[0], k[1], k[2], k[3], k[4]
            if sec == "exit":
                if prev != "exit":
                    marks.append((at, {"kind": "exit"}))
                prev = "exit"
                continue
            if sec != "body":
                continue
            new_g = not isinstance(prev, tuple) or prev[:2] != (pid, g)
            if new_g:
                if (pid, g) not in gid:
                    gid[(pid, g)] = len(groups)
                    src = lm.groups[g] if pid == "base" \
                        and 0 <= g < len(lm.groups) else {}
                    grp = {k2: v for k2, v in src.items()
                           if k2 not in ("name", "layers")}
                    # the name as the Outline says it: a raw "scaffold1"
                    # would read as the designer's own scaffold row
                    grp["name"] = self.group_name(pid, g)
                    grp["layers"] = 0
                    groups.append(grp)
                marks.append((at, {"kind": "group", "group": gid[(pid, g)]}))
            if L >= 0 and (new_g or prev[2] != L):
                marks.append((at, {"kind": "layer", "layer": int(L)}))
                grp = groups[gid[(pid, g)]]
                grp["layers"] = max(grp["layers"], int(L) + 1)
            if P >= 0 and (new_g or prev[2] != L or prev[3] != P):
                marks.append((at, {"kind": "pass", "pass": int(P)}))
            uid = rl.uid or ()
            if pid == "base" and len(uid) == 3 and uid[0] == "s" \
                    and uid[2] in links:
                marks.append((at, {"kind": "link"}))
            prev = (pid, g, L, P)
        return write_structure(r.text, marks, groups, self.flavor,
                               saved_text)

    def rebase(self, base_text, line_map=None):
        """Put a new base under the same chips (the designer regenerated).
        Returns the ids of chips that couldn't carry over."""
        self.base_text = base_text
        self.line_map = line_map
        return self.render().unplaced

    def relink_preview(self, base_text, line_map=None):
        """For a detached document: ``[(chip_id, carries)]`` -- which chips a
        re-link onto this base would carry over."""
        trial = self.copy()
        lost = set(trial.rebase(base_text, line_map))
        return [(c.id, c.id not in lost) for c in self.changes]

    def shift_user_xy(self, dx, dy):
        """G-code zero moved on the plate: every absolute X/Y the user's own
        edits name moves by (`dx`, `dy`) mm, so what they place stays where
        it was on the plate, as the designer's Start X/Y do
        (`gui_buildplate._zero_moved`). Returns how many edits and pieces
        moved.

        Moved: an Insert's or a Rule's absolute moves (a Move-to form's X/Y
        with its lines), a hand edit's X/Y words or replacing line, an
        Offset's pivot, a stitched file's or copy's offset. Left as they
        are: relative stretches (G91) and arc centres (I/J), Scale (about
        zero by definition), an edit typed in an incremental export (its
        `shift` is a delta) and an export-stage line (the zero move, the
        Start/End blocks: written in the file's own terms)."""
        if not (dx or dy):
            return 0
        moved = 0

        def lines_of(lines, flavor):
            comment = get_flavor(flavor or self.flavor).comment or ";"
            out, rel = [], False
            for ln in lines:
                cmd, words = parse_words(ln, comment)
                cc = canon_cmd(cmd) if cmd else ""
                if cc in ("G90", "G91"):
                    rel = cc == "G91"
                vals = {}
                if cc in _MOTION and not rel:
                    for L, d in (("X", dx), ("Y", dy)):
                        if d and L in words:
                            try:
                                vals[L] = float(words[L]) + d
                            except ValueError:
                                pass
                out.append(_put_words(ln, vals) if vals else ln)
            return out

        def command_of(c):
            cmd = dict(c.command or {})
            hit = False
            for k, d in (("x", dx), ("y", dy)):
                if cmd.get(k) not in (None, ""):
                    try:
                        cmd[k] = round(float(cmd[k]) + d, 6)
                        hit = True
                    except (TypeError, ValueError):
                        pass
            return cmd if hit else None

        for c in self.changes:
            was = c.to_dict()
            if isinstance(c, (Insert, Rule)) and not getattr(c, "shift", None):
                if c.command:
                    cmd = command_of(c)
                    if cmd is not None:
                        c.command = cmd
                if isinstance(c, Insert):
                    c.lines = lines_of(c.lines, c.flavor)
                else:
                    c.text = "\n".join(lines_of(c.text.split("\n"),
                                                c.flavor))
            elif isinstance(c, LineEdit) and not c.shift \
                    and c.stage != "export":
                if c.op == "words":
                    for L, d in (("X", dx), ("Y", dy)):
                        if d and L in c.set:
                            try:
                                c.set[L] = fmt_num(float(c.set[L]) + d)
                            except ValueError:
                                pass
                elif c.op == "replace" and c.text:
                    c.text = "\n".join(lines_of(c.text.split("\n"),
                                                c.flavor))
            elif isinstance(c, Transform) and c.op == "offset":
                p = dict(c.params or {})
                for k, d in (("cx", dx), ("cy", dy)):
                    if p.get(k) not in (None, ""):
                        try:
                            p[k] = round(float(p[k]) + d, 6)
                        except (TypeError, ValueError):
                            pass
                c.params = p
            if c.to_dict() != was:
                moved += 1
        for p in self.pieces:
            if p.source in ("file", "snippet"):
                ox, oy, oz = (tuple(p.offset) + (0.0, 0.0, 0.0))[:3]
                p.offset = (round(ox + dx, 6), round(oy + dy, 6), oz)
                moved += 1
        return moved

    def copy(self):
        return Document.from_dict(self.to_dict(with_base=True))

    # -- undo ------------------------------------------------------------- #
    def snapshot(self):
        """A value `restore` takes back: the page's document-level undo. Tk's
        own undo cannot be used, because a re-render after a chip toggle
        would read to the diff as a hand edit."""
        return self.to_dict(with_base=True)

    def restore(self, snap):
        other = Document.from_dict(snap)
        self.__dict__.update(other.__dict__)

    # -- the line map, always present -------------------------------------- #
    def _map(self):
        lm = self.line_map
        if lm is None or not lm.matches(self.base_text):
            lm = LineMap.guess(self.base_text, get_flavor(self.flavor).comment)
            self.line_map = lm
        return lm

    def group_name(self, piece_id, group):
        """Plain name of a group: "base" for the stabilization base,
        "scaffold k", or the piece's name."""
        if piece_id == "base":
            gs = self._map().groups
            name = gs[group]["name"] if 0 <= group < len(gs) else ""
            if name == "stab":
                return "base layers"
            if name == "file":
                return "the file"
            m = re.fullmatch(r"scaffold(\d+)", name or "")
            if m:
                return "scaffold %s" % m.group(1)
            return name or "scaffold %d" % (group + 1)
        for p in self.pieces:
            if p.id == piece_id:
                return p.name or "piece"
        return "piece"

    # -- chips ------------------------------------------------------------- #
    def new_id(self, prefix="c"):
        used = {c.id for c in self.changes} | {p.id for p in self.pieces}
        while True:
            cid = "%s%d" % (prefix, self._next_id)
            self._next_id += 1
            if cid not in used:
                return cid

    def add(self, change):
        """Add a chip (an id is assigned when it has none); returns it."""
        if not change.id:
            change.id = self.new_id()
        if isinstance(change, (Insert, Rule, LineEdit)) \
                and not change.flavor:
            change.flavor = self.flavor
        self.changes.append(change)
        return change

    def get(self, chip_id):
        for c in self.changes:
            if c.id == chip_id:
                return c
        raise KeyError(chip_id)

    def remove(self, chip_id):
        """Take a chip out, and every operation under it."""
        gone = self.lineage(chip_id)
        self.changes = [c for c in self.changes if c.id not in gone]

    def toggle(self, chip_id, on):
        self.get(chip_id).on = bool(on)

    # -- operations on edits (`Scope` kind "edit") ------------------------- #
    def parent_id(self, c):
        """The id of the edit `c` is an operation on, or "" (not one)."""
        sc = getattr(c, "scope", None)
        if isinstance(c, Transform) and sc is not None and sc.kind == "edit":
            return str(sc.first or "")
        return ""

    def children(self, chip_id):
        """The operations directly under `chip_id`, in list order."""
        return [c for c in self.changes if self.parent_id(c) == chip_id]

    def lineage(self, chip_id):
        """`chip_id` and the ids of every operation under it, at any depth:
        the chips whose lines are what that edit writes."""
        out, todo = {chip_id}, [chip_id]
        while todo:
            p = todo.pop()
            for c in self.changes:
                if c.id not in out and self.parent_id(c) == p:
                    out.add(c.id)
                    todo.append(c.id)
        return out

    def add_operation(self, chip_id, op, params=None):
        """A new operation `op` (a `_GEOMETRIC` transform kind) on the edit
        `chip_id`, nested under it; returns it. Repeat starts from
        `EDIT_REPEAT_DEFAULTS`, the others from `TRANSFORM_FIELDS`."""
        self.get(chip_id)
        if op not in _GEOMETRIC:
            raise ValueError("%s works on the whole file, not on one edit"
                             % _TRANSFORM_LABEL.get(op, op))
        if params is None:
            params = dict(EDIT_REPEAT_DEFAULTS) if op == "repeat" else {
                n: d for n, _l, _u, d in TRANSFORM_FIELDS.get(op, ())}
        return self.add(Transform(op=op, params=dict(params),
                                  scope=Scope("edit", chip_id), what=op))

    def _shown(self, c, _depth=0):
        """True when `c` renders: it is on, and so is every edit it sits
        under."""
        if not c.on or _depth > 64:
            return False
        pid = self.parent_id(c)
        if not pid:
            return True
        try:
            return self._shown(self.get(pid), _depth + 1)
        except KeyError:
            return False

    def chips(self):
        """``[(chip, label)]`` for every edit, in order."""
        return [(c, c.label or chip_label(self, c)) for c in self.changes]

    def add_piece(self, piece):
        if not piece.id:
            piece.id = self.new_id("p")
        if not self.pieces and piece.source != "designer" and self.base_text:
            self.pieces.append(Piece(id="base", source="designer",
                                     name="Scaffold designer"))
        self.pieces.append(piece)
        return piece

    def insert_command(self, kind, anchor, **fields):
        """An Insert chip from an Insert form (`INSERT_FORMS`)."""
        lines = make_command(kind, self.flavor, **fields)
        return self.add(Insert(lines=lines, anchor=anchor,
                               command=dict(fields, kind=kind),
                               flavor=self.flavor))

    def rule_from_insert(self, chip_id, where=None, every=1):
        """"A chip turns into a rule in one click": the Insert's text, placed
        at every unit like its anchor (after layer 7 -> after each layer)."""
        c = self.get(chip_id)
        if not isinstance(c, Insert):
            raise TypeError("only an inserted line becomes a rule")
        if where is None:
            unit, side = c.anchor.unit, c.anchor.side
            where = {"layer": side + "_layer", "group": side + "_group",
                     "pass": side + "_layer", "line": side + "_layer",
                     "start": "start", "end": "end",
                     "header": "before_first_print"}.get(unit, "after_layer")
        r = Rule(text="\n".join(_render_insert_lines(c, c.flavor)),
                 where=where, every=int(every), flavor=c.flavor,
                 name=c.label or "", command=copy.deepcopy(c.command),
                 what=c.what, saved=c.saved, label=c.label, on=c.on)
        r.id = c.id
        self.changes[self.changes.index(c)] = r
        return r

    def place(self, chip_id, where):
        """Put an added-lines edit (Insert or Rule) at a new place, keeping
        its id, name, kind and lines. `where` is an `Anchor` (one spot) or a
        dict with a `WHERE_CHOICES` "where" and its every / first / last / n
        (a pattern). Returns the chip now in the list (an Insert becomes a
        Rule, or back, as the place needs)."""
        c = self.get(chip_id)
        if not isinstance(c, (Insert, Rule)):
            raise TypeError("only added lines have a place")
        common = dict(id=c.id, on=c.on, label=c.label, what=c.what,
                      saved=c.saved, typed=c.typed, flavor=c.flavor,
                      command=copy.deepcopy(c.command))
        lines = (_render_insert_lines(c, c.flavor) if isinstance(c, Insert)
                 else _rule_lines(c, c.flavor))
        if isinstance(where, Anchor):
            new = Insert(lines=list(lines), anchor=where, **common)
        else:
            w = dict(where)
            new = Rule(text="\n".join(lines),
                       where=w.get("where", "after_layer"),
                       every=int(w.get("every") or 1), first=w.get("first"),
                       last=w.get("last"), n=float(w.get("n") or 0.0),
                       name=c.label or getattr(c, "name", ""), **common)
        self.changes[self.changes.index(c)] = new
        return new

    def nudge(self, chip_id, delta):
        """Move a one-spot edit (an Insert) `delta` lines down (negative:
        up) past the lines around it. Returns True when it moved."""
        c = self.get(chip_id)
        if not isinstance(c, Insert) or not delta:
            return False
        r = self.render()
        start = next((i for i, rl in enumerate(r.lines) if rl.chip == c.id),
                     None)
        if start is None:
            return False
        was_on, was_anchor = c.on, c.anchor
        c.on = False
        try:
            off = self.render().lines
        finally:
            c.on = was_on
        step = 1 if delta > 0 else -1
        pos = start
        for _ in range(abs(delta)):
            moved = False
            while 0 <= pos + step <= len(off):
                pos += step
                c.anchor = self._anchor_between(off, pos)
                now = next((i for i, rl in enumerate(self.render().lines)
                            if rl.chip == c.id), None)
                if now is not None and now != start:
                    start, moved = now, True
                    break
            if not moved:
                break
        return c.anchor != was_anchor

    def move_to_line(self, chip_id, line_no):
        """Put a one-spot edit (an Insert) so its first line is export line
        `line_no` (1-based, clamped to the file), counted as the file reads
        with the edit in it. Returns the line it landed on, or None (the
        place is not a spot an Insert can hold)."""
        c = self.get(chip_id)
        if not isinstance(c, Insert):
            return None
        was_on = c.on
        c.on = False
        try:
            off = self.render().lines
        finally:
            c.on = was_on
        pos = min(max(int(line_no), 1), len(off) + 1) - 1
        c.anchor = self._anchor_between(off, pos)
        return next((i for i, rl in enumerate(self.render().lines, start=1)
                     if rl.chip == c.id), None)

    def anchor_at(self, line_no, rendered=None):
        """The anchor an Insert form uses for a line going in BEFORE export
        line `line_no` (0-based; len(lines) = the end) -- the caret's place,
        in structural terms."""
        r = rendered or self.render()
        return self._anchor_between(r.lines, line_no)

    def place_insert(self, chip_id, line_no):
        """Re-place an Insert ("couldn't carry over" and dragged back) so it
        sits after export line `line_no` (0-based; -1 = the top)."""
        c = self.get(chip_id)
        r = self.render()
        c.anchor = self._anchor_between(r.lines, line_no + 1)

    # -- render ------------------------------------------------------------ #
    def automatic_rules(self):
        """The rules of the automatic scripts (`options.rig_recipes`), as
        Rule objects with ids "rig:i:j" and `saved` set to their script's
        name. They are NOT in `changes`: they run on every print and are
        never stored in the design."""
        rig = []
        if self.source != "file":
            for i, data in enumerate(self.options.rig_recipes):
                try:
                    recipe = Recipe.from_dict(data)
                except (KeyError, TypeError, ValueError):
                    continue
                for j, rule in enumerate(recipe.rules):
                    rule.id = "rig:%d:%d" % (i, j)
                    rule.saved = recipe.name
                    rig.append(rule)
        return rig

    def render(self, form="export"):
        """Render with the automatic scripts, without storing them in the design."""
        original = self.changes
        self.changes = original + self.automatic_rules()
        try:
            return self._render_impl(form)
        finally:
            self.changes = original

    def _render_impl(self, form="export"):
        """Render the document. `form` "export" (what the page shows and
        Save writes), "parse" (what the parser / converter / player read:
        G-code, the flavor's read-only aliases read as their moves) or
        "internal" (the export before its last step, the flavor's spelling
        and the line numbers: the lines `export_gaps` judges, sharing the
        export's `uid`s)."""
        fl = get_flavor(self.flavor)
        exportish = form in ("export", "internal")
        ending = "\r\n" if "\r\n" in self.base_text else "\n"
        slots, unplaced = self._compose(ending)
        for c in self.changes:
            if not c.on:
                continue
            if self.parent_id(c):
                # an operation on an edit: on the generated lines that edit
                # changed now; on the lines it adds, and every Repeat, with
                # the added lines below
                if not self._shown(c):
                    try:
                        self.get(self.parent_id(c))
                    except KeyError:
                        unplaced.append(c.id)
                elif c.op in _GEOMETRIC and c.op != "repeat":
                    slots = self._apply_transform(slots, c, ending)
                continue
            if isinstance(c, Transform) and c.op in _GEOMETRIC:
                slots = self._apply_transform(slots, c, ending)
            elif isinstance(c, LineEdit) and c.at:
                continue            # on the rendered lines, below
            elif isinstance(c, LineEdit):
                if not self._apply_line_edit(slots, c):
                    unplaced.append(c.id)
                for k in self._copy_keys(c.key):
                    self._apply_line_edit(slots, c, k)
                if c.op == "delete" and c.unit == "layer" and len(c.key) > 3:
                    # a whole layer is removed BY LAYER, not by the lines it
                    # had: after the design changes (more passes) none of
                    # the layer is left behind
                    for pre in [tuple(c.key[:4])] + [
                            k[:4] for k in self._copy_keys(c.key)]:
                        for s in slots:
                            if s.key and tuple(s.key[:4]) == pre \
                                    and not s.deleted:
                                s.deleted = True
                                s.origin, s.chip = "edit", c.id
                    continue
                for extra in (c.more if c.op == "delete" else ()) or ():
                    self._apply_line_edit(slots, c, extra)
                    for k in self._copy_keys(extra):
                        self._apply_line_edit(slots, c, k)
        pre_opt = [s.text for s in slots]
        if exportish:
            self._apply_options(slots)
        index = self._index(slots)
        # the nozzle state per slot, walked only when a rule, a Repeat or an
        # operation on an edit first reads it: a document with none of those
        # (an opened file, a design with no rules) never needs it, and it was
        # half of every render -- 0.6 s of a 120,000-line file's
        walk = _LazyWalk(lambda: self._walk(pre_opt, fl))
        top, bottom = [], []
        before, after = {}, {}

        def put(pos, rl):
            where, i = pos
            if where == "top":
                top.append(rl)
            elif where == "bottom":
                bottom.append(rl)
            elif where == "before":
                before.setdefault(i, []).append(rl)
            else:
                after.setdefault(i, []).append(rl)
        if exportish:
            line = wait_temp_line(self.options.wait_temp, fl)
            if line:
                top.append(RLine(line, "option",
                                 "opt:wait_temp", (), True,
                                 ("opt", "wait_temp")))
        for pl in self._placements(slots, index, walk, fl, ending, unplaced):
            for ln, origin, chip, ro, uid in pl["lines"]:
                if form == "parse":
                    ln = strip_comment(ln, fl.comment).strip()
                    if not ln:
                        continue
                put(pl["pos"], RLine(ln + ending, origin, chip, (), ro, uid))
        out = []
        for ln in fl.header:
            out.append(RLine(ln + ending, "flavor", "flavor", (), True,
                             ("flavor", "header", ln)))
        out += top
        shifted = {tuple(c.key): c.shift for c in self.changes
                   if c.on and isinstance(c, LineEdit) and c.shift
                   and not c.at}
        marks = []
        layer_at = self._layer_marker_lines(slots, fl) if exportish else {}
        for i, s in enumerate(slots):
            for j, t in enumerate(layer_at.get(i, ())):
                out.append(RLine(t + ending, "option", "opt:layer", (), True,
                                 ("opt", "layer", i, j)))
            out += before.get(i, ())
            if s.key and tuple(s.key) in shifted:
                marks.append((len(out), shifted[tuple(s.key)]))
            if not s.deleted:
                text = s.text
                if form == "parse" and s.user:
                    text = strip_comment(text, fl.comment).strip()
                    text = (text + ending) if text else ""
                parts = _split_text_lines(text) if text else []
                for j, part in enumerate(parts):
                    out.append(RLine(part, s.origin, s.chip,
                                     s.key if j == 0 else (),
                                     s.readonly or j > 0 or (
                                         len(parts) > 1),
                                     s.uid + ((j,) if j else ())))
            out += after.get(i, ())
        out += bottom
        for c in self.changes:
            if c.on and isinstance(c, Insert) and c.shift:
                at = [k for k, o in enumerate(out) if o.chip == c.id]
                if at:
                    marks.append((at[-1] + 1, c.shift))
        at_edits = [c for c in self.changes
                    if c.on and isinstance(c, LineEdit) and c.at]
        placed = set()
        out, marks = self._apply_at_edits(out, at_edits, "", placed, form,
                                          fl, ending, marks)
        if marks:
            out = _apply_shifts(out, marks, fl.comment)
        for ln in fl.footer:
            out.append(RLine(ln + ending, "flavor", "flavor", (), True,
                             ("flavor", "footer", ln)))
        for c in self.changes:
            if c.on and isinstance(c, Transform) and c.op not in _GEOMETRIC \
                    and (c.scope is None or c.scope.kind not in (
                        "edit", "lines", "set")):
                out = _text_transform(out, c, fl, ending)
        # the flavor's footer, and lines a text transform made
        out, _m = self._apply_at_edits(out, at_edits, "", placed, form, fl,
                                       ending)
        # every line but the last ends with a newline
        for k in range(len(out) - 1):
            t = out[k].text
            if not t.endswith(("\n", "\r")):
                out[k] = RLine(t + ending, out[k].origin, out[k].chip,
                               out[k].key, out[k].readonly, out[k].uid)
        if form == "parse" and fl.aliases:
            out = [RLine(read_line(o.text, fl), o.origin, o.chip, o.key,
                         o.readonly, o.uid) for o in out]
        if form == "export":
            out = machine_render(out, fl, self.options, ending)
        elif form == "parse" and fl.rotary_feed == "split" and \
                _rotary_feeds(o.text for o in out)[1] and not any(
                canon_cmd(parse_words(o.text, fl.comment)[0] or "")
                in _INTERNAL_ONLY for o in out):
            # the reader times a split file by its time base, which the
            # export leaves to the controller's setting. Only a text that IS
            # split-fed: the flavor says how the controller reads a rate, not
            # how this text was written, and a COMBINED tube (one F, the
            # surface speed -- what the designer emits while split emission
            # is paused) read under G75 is timed by its X travel alone, at
            # half its surface length on the default tube. That clock is what
            # the physics relay lays the jet by: every wall stepped ~12 um a
            # circuit on the 4-axis default in Aerotech.
            out.insert(0, RLine(("G76" if self.feed_unit == "s" else "G75")
                                + ending, "flavor", "flavor", (), True,
                                ("flavor", "time_base")))
        if exportish and self.options.custom_line is not None:
            cl, after_n = self.options.custom_line
            if out and not out[-1].text.endswith(("\n", "\r")):
                o = out[-1]
                out[-1] = RLine(o.text + "\n", o.origin, o.chip, o.key,
                                o.readonly, o.uid)
            out.insert(min(int(after_n), len(out)),
                       RLine(cl + "\n", "option", "opt:custom_line", (), True,
                             ("opt", "custom_line")))
        if self.source != "file":
            def machine_lines(block, name):
                text = render_machine_block(block, self.options.context, fl)
                result = []
                for i, ln in enumerate(text.splitlines(keepends=True)):
                    if form == "parse":
                        ln = strip_comment(ln, fl.comment).strip()
                        if not ln:
                            continue
                        ln = read_line(ln, fl) + ending
                    key = (("machine", "header", -1, -1, -1, i)
                           if name == "start" else ())
                    result.append(RLine(ln, "machine", "machine:" + name,
                                        key, True, ("machine", name, i)))
                return result
            out = machine_lines(self.options.start_block, "start") + out
            out += machine_lines(self.options.end_block, "end")
        out, _m = self._apply_at_edits(out, at_edits, "export", placed, form,
                                       fl, ending)
        if form != "parse":
            # (the parse form has no option, zero-move or block lines)
            unplaced += [c.id for c in at_edits if c.id not in placed]
        if form == "export" and fl.line_numbers:
            out = [RLine("N%d %s" % (k + 1, o.text), o.origin, o.chip, o.key,
                         o.readonly, o.uid) for k, o in enumerate(out)]
        seen = set()
        unplaced = [u for u in unplaced if not (u in seen or seen.add(u))]
        return Rendered("".join(o.text for o in out), out, unplaced, form)

    def _layer_marker_lines(self, slots, fl):
        """{slot index: [comment lines]}: "Mark each layer in the file"
        (`OutputOptions.layer_markers`). A ``;LAYER:n`` before the first
        line of each layer still in the file, counted over the whole file
        from 0 as Cura does, and ``;LAYER_COUNT:N`` before the first. Only
        from the generator's map: a guessed map's layers are not written
        back as though they were known. The line goes before the chips
        placed at the layer's start, so they stay in the layer they were
        put in."""
        if not self.options.layer_markers:
            return {}
        lm = self._map()
        if lm.guessed:
            return {}
        order = []
        last = None
        for i, s in enumerate(slots):
            k = s.key
            if (s.deleted or not k or len(k) < 5 or k[1] != "body"
                    or k[3] < 0):
                continue
            here = (k[0], k[2], k[3])
            if here != last:
                order.append(i)
                last = here
        out = {}
        for n, i in enumerate(order):
            lines = [layer_marker(n, fl.comment)]
            if n == 0:
                lines.insert(0, layer_count_marker(len(order), fl.comment))
            out[i] = lines
        return out

    def export_text(self):
        return self.render("export").text

    def parse_text(self):
        return self.render("parse").text

    def unplaced(self):
        return self.render().unplaced

    # -- render internals -------------------------------------------------- #
    def _compose(self, ending):
        """The generated lines of every piece, joined. With no pieces, the
        base text alone, untouched."""
        unplaced = []
        pieces = [p for p in self.pieces if p.on]
        if not pieces:
            pieces = [Piece(id="base", source="designer")]
        slots = []
        prev_end = None
        for pi, p in enumerate(pieces):
            if p.source == "designer":
                text, lm, pid = self.base_text, self._map(), "base"
            else:
                text, pid = p.text, p.id
                lm = _piece_map(text, p.layer_starts)
            lines = text.splitlines(keepends=True)
            addrs = lm.addresses()
            ps = [_Slot(ln, (pid,) + a.key(), pre=ln, uid=("s", pid, i),
                        piece=pi)
                  for i, (ln, a) in enumerate(zip(lines, addrs))]
            if p.source != "designer" and p.drop_frame:
                ps = [s for s in ps if s.key[1] == "body"]
            if p.layers is not None:
                lo, hi = int(p.layers[0]), int(p.layers[1])
                order = []
                for s in ps:
                    if s.key[1] == "body" and s.key[3] >= 0 \
                            and (s.key[2], s.key[3]) not in order:
                        order.append((s.key[2], s.key[3]))
                keep = {u for n, u in enumerate(order, 1) if lo <= n <= hi}
                ps = [s for s in ps if s.key[1] != "body" or s.key[3] < 0
                      or (s.key[2], s.key[3]) in keep]
            if not p.is_plain():
                _piece_transform(ps, p)
            if pi > 0 and ps:
                slots += self._join(prev_end, ps, p, ending)
            slots += ps
            if pi == len(pieces) - 1:
                break
            w = _Walker()
            for s in slots:
                if not s.deleted:
                    for part in _split_text_lines(s.text):
                        w.step(part)
            prev_end = (w.x, w.y, w.z)
        return slots, unplaced

    def _join(self, prev_end, ps, piece, ending):
        """The travel from the previous piece's last point to this one's
        first. It lays fiber in MEW; the preview shows it."""
        w = _Walker()
        first = None
        for s in ps:
            c, words, _d, _z = w.step(s.text)
            if c in _MOTION and ("X" in words or "Y" in words):
                first = (w.x, w.y, w.z)
                break
        if first is None or prev_end is None or prev_end[0] is None:
            return []
        out = []
        za = prev_end[2] or 0.0
        zb = first[2] if first[2] is not None else za
        if piece.lift:
            out.append("G0 Z%s" % fmt_num(max(za, zb) + float(piece.lift)))
            out.append("G0 X%s Y%s" % (fmt_num(first[0]), fmt_num(first[1])))
            out.append("G0 Z%s" % fmt_num(zb))
        else:
            out.append("G0 X%s Y%s Z%s" % (fmt_num(first[0]),
                                           fmt_num(first[1]), fmt_num(zb)))
        return [_Slot(t + ending, (), "join", piece.id, readonly=True,
                      uid=("join", piece.id, k), piece=ps[0].piece)
                for k, t in enumerate(out)]

    def _index(self, slots):
        """Lookups for anchors and rules over the slot list."""
        key_at = {}
        units = {"group": {}, "layer": {}, "pass": {}}
        layers, groups = [], []
        header_last = None
        first_piece = slots[0].key[0] if slots and slots[0].key else None
        for i, s in enumerate(slots):
            k = s.key
            if not k:
                continue
            key_at[k] = i
            pid, sec, g, L, ps = k[0], k[1], k[2], k[3], k[4]
            if sec == "header" and pid == first_piece:
                header_last = i
            if sec != "body":
                continue
            for unit, u in (("group", (pid, g)), ("layer", (pid, g, L)),
                            ("pass", (pid, g, L, ps))):
                if unit == "layer" and L < 0 or unit == "pass" and ps < 0:
                    continue
                rng = units[unit].get(u)
                units[unit][u] = (i, i) if rng is None else (rng[0], i)
            if (pid, g) not in groups:
                groups.append((pid, g))
            if L >= 0 and (pid, g, L) not in layers:
                layers.append((pid, g, L))
        return {"key": key_at, "units": units, "layers": layers,
                "groups": groups, "header_last": header_last,
                "pieces": list(dict.fromkeys(s.key[0] for s in slots
                                             if s.key))}

    def _resolve(self, anchor, index):
        unit, side, key = anchor.unit, anchor.side, tuple(anchor.key)
        if unit == "start":
            return ("top", None)
        if unit == "end":
            return ("bottom", None)
        if unit == "header":
            h = index["header_last"]
            return ("top", None) if h is None else ("after", h)
        if unit == "line":
            i = index["key"].get(key)
            return None if i is None else (side, i)
        rng = index["units"].get(unit, {}).get(key)
        if rng is None:
            return None
        return ("before", rng[0]) if side == "before" else ("after", rng[1])

    def _apply_line_edit(self, slots, c, key=None):
        key = tuple(c.key) if key is None else tuple(key)
        for s in slots:
            if s.key == key:
                break
        else:
            return False
        if c.op == "delete":
            s.deleted = True
        elif c.op == "replace":
            s.text = c.text + (s.text[len(s.text.rstrip("\r\n")):] or "\n")
            s.user = True
            s.flavor = c.flavor
            conv = convert_line(c.text, c.flavor or self.flavor, self.flavor)
            s.text = conv + s.text[len(c.text):]
        else:
            new = apply_word_patch(s.text, c.set, c.drop)
            if new is None:
                return False
            s.text = new
        s.origin, s.chip = "edit", c.id
        return True

    def _apply_at_edits(self, out, edits, stage, placed, form, fl, ending,
                        marks=None):
        """Apply each `LineEdit` with an `at` of `stage` not yet `placed` to
        the rendered lines `out`: the line whose uid it names is replaced
        or removed. Returns ``(out, marks)``, the shift marks moved to the
        new indices, an incremental edit's own added."""
        todo = {tuple(c.at): c for c in edits
                if c.stage == stage and c.id not in placed}
        if not todo:
            return out, marks
        new, at_new, added = [], [], []
        for o in out:
            at_new.append(len(new))
            c = todo.get(tuple(o.uid))
            if c is None:
                new.append(o)
                continue
            placed.add(c.id)
            if c.shift:
                added.append((len(new), dict(c.shift)))
            if c.op == "delete":
                continue
            body = c.text
            if stage == "":
                body = convert_line(body, c.flavor or self.flavor,
                                    self.flavor)
            if form == "parse":
                body = strip_comment(body, fl.comment).strip()
                if stage == "export" and body:
                    body = read_line(body, fl)
                if not body:
                    continue
            end = o.text[len(o.text.rstrip("\r\n")):] or ending
            new.append(RLine(body + end, "edit", c.id, o.key, False, o.uid,
                             src=o.src, pos=o.pos))
        at_new.append(len(new))
        if marks is not None:
            marks = [(at_new[min(i, len(out))], v) for i, v in marks] + added
        return new, marks

    def _apply_options(self, slots):
        """The output options, on the generator's own lines only: a stitched
        file's lines (a piece other than the designer's, keyed by its own
        id) print as they are, like the file opened alone, so "Don't adjust
        height per layer" never takes that file's layer heights away."""
        o = self.options
        if not (o.travel_as_g1 or o.padded or o.drop_layer_z):
            return
        idx = [i for i, s in enumerate(slots)
               if not s.deleted and not s.user and s.origin != "join"
               and s.key[:1] == ("base",)
               and "\n" not in s.text.rstrip("\r\n")]
        lines = [slots[i].text for i in idx]
        five = is_five_axis_gcode("".join(lines))
        bounds = o.boundaries
        if bounds == AUTO:
            bounds = self._auto_boundaries(slots, idx)
        new = _rewrite_line_list(lines, o.travel_as_g1, o.padded,
                                 o.drop_layer_z, bounds, five_axis=five)
        for i, t in zip(idx, new):
            if t is None:
                slots[i].deleted = True
                slots[i].origin = "option"
            else:
                slots[i].text = t

    def _auto_boundaries(self, slots, idx):
        """``(ordinal, height)`` for the first Z-bearing generated line of
        every scaffold after the first (the stabilization base is not a
        boundary: it inherits scaffold 1's start Z) -- `gui_scaffold.
        _flat_z_boundaries` read off the line map instead of re-derived
        from the design."""
        out = []
        seen = set()
        first = None
        ordinal = -1
        for i in idx:
            found = split_z_word(slots[i].text)
            if found is None:
                continue
            ordinal += 1
            k = slots[i].key
            if not k or k[1] != "body":
                continue
            g = (k[0], k[2])
            if g in seen:
                continue
            seen.add(g)
            if k[0] == "base" and self.group_name("base", k[2]) == \
                    "base layers":
                continue
            if first is None:
                first = g
                continue
            out.append((ordinal, found[2]))
        return tuple(out)

    def boundaries_from_map(self):
        """`_auto_boundaries` for the bare base: what AUTO resolves to
        before any chip."""
        slots, _u = self._compose("\n")
        return self._auto_boundaries(slots, range(len(slots)))

    def _walk(self, texts, fl):
        """Nozzle state AFTER each slot (pre-options text), plus whether the
        slot is a travel and whether it moves."""
        w = _Walker(fl.comment)
        out = []
        for t in texts:
            travel = moved = False
            for part in _split_text_lines(t):
                c, _words, d, zero = w.step(part)
                if c in _MOTION:
                    moved = True
                    travel = travel or (c == "G0" and not zero)
            out.append(dict(w.snapshot(), travel=travel, moved=moved,
                            first_print=False))
        return out

    def _copies_of(self):
        """{(piece, original group): [(piece, copy group), ...]}."""
        out = {}
        for cp, src in (self.copies or {}).items():
            if tuple(cp) != tuple(src):
                out.setdefault(tuple(src), []).append(tuple(cp))
        return out

    def _with_copies(self, slots, sel):
        """`sel` (slot indices) and, for each one on an original scaffold
        that has array copies, the same line of every copy."""
        of = self._copies_of()
        if not of or not sel:
            return sel
        want = set()
        for i in sel:
            k = slots[i].key
            if k and k[1] == "body" and (k[0], k[2]) in of:
                want.add((k[0], k[2], k[1]) + tuple(k[3:]))
        if not want:
            return sel
        src_of = {c: s for s, cs in of.items() for c in cs}
        got = set(sel)
        for i, s in enumerate(slots):
            k = s.key
            if not k or k[1] != "body":
                continue
            src = src_of.get((k[0], k[2]))
            if src is not None and src + (k[1],) + tuple(k[3:]) in want:
                got.add(i)
        return sorted(got)

    def _copy_keys(self, key):
        """A line or unit key on an original scaffold, as it is on each of
        its copies (the group swapped)."""
        key = tuple(key)
        if len(key) < 2:
            return []
        of = self._copies_of()
        if len(key) >= 3 and key[1] == "body":   # a line: (p, body, g, ...)
            return [(c[0], key[1], c[1]) + key[3:]
                    for c in of.get((key[0], key[2]), ())]
        return [(c[0], c[1]) + key[2:]            # a unit: (p, g, ...)
                for c in of.get((key[0], key[1]), ())]

    def _scope_slots(self, slots, scope, index):
        kind = scope.kind if scope else "all"
        if kind in ("lines", "layers", "group"):
            return self._with_copies(
                slots, self._scope_slots_own(slots, scope, index))
        return self._scope_slots_own(slots, scope, index)

    def _scope_slots_own(self, slots, scope, index):
        kind = scope.kind if scope else "all"
        if kind == "all":
            return [i for i, s in enumerate(slots) if s.key]
        if kind == "lines":
            want = {tuple(k) for k in (scope.first or ())}
            return [i for i, s in enumerate(slots) if s.key in want]
        if kind == "set":
            got = set()
            for sub in scope.first or ():
                sub = Scope.from_dict(sub)
                got.update(self._edit_slots(slots, str(sub.first or ""))
                           if sub.kind == "edit" else
                           self._scope_slots(slots, sub, index))
            return sorted(got)
        if kind == "edit":
            return self._edit_slots(slots, str(scope.first or ""))
        lo = int(scope.first or 1)
        hi = int(scope.last if scope.last is not None else lo)
        if kind == "layers":
            want = {u for n, u in enumerate(index["layers"], 1)
                    if lo <= n <= hi}
            return [i for i, s in enumerate(slots) if s.key
                    and s.key[1] == "body"
                    and (s.key[0], s.key[2], s.key[3]) in want]
        if kind == "group":
            want = {u for n, u in enumerate(index["groups"], 1)
                    if lo <= n <= hi}
            return [i for i, s in enumerate(slots) if s.key
                    and s.key[1] == "body" and (s.key[0], s.key[2]) in want]
        if kind == "piece":
            want = {u for n, u in enumerate(index["pieces"], 1)
                    if lo <= n <= hi}
            return [i for i, s in enumerate(slots) if s.key
                    and s.key[0] in want]
        raise ValueError("unknown scope %r" % (kind,))

    def _edit_slots(self, slots, parent):
        """The generated lines the edit `parent` (a LineEdit or a Transform)
        and the operations under it have changed."""
        ids = self.lineage(parent)
        return [i for i, s in enumerate(slots)
                if s.chip in ids and not s.deleted]

    @staticmethod
    def _move_rotate(slots, chosen, p, cid):
        """An offset that also turns about Z (`rz` degrees about the pivot
        `cx`, `cy`, blank: the middle of the chosen moves): every chosen
        absolute move goes to pivot + R(here - pivot) + (dx, dy), its Z
        + dz, an arc's I/J turned with it. A turn needs both of a point's
        coordinates, so a move naming only one of X / Y gets the other
        from where the machine was; a move naming neither stays put, as
        the line before it left the machine."""
        d = (float(p.get("dx", 0)), float(p.get("dy", 0)),
             float(p.get("dz", 0)))
        a = math.radians(float(p.get("rz") or 0.0))
        co, si = math.cos(a), math.sin(a)
        piv = motion_xy_centre([s.text for s in slots], chosen,
                               skip={i for i, s in enumerate(slots)
                                     if s.deleted})
        cx = p.get("cx")
        cy = p.get("cy")
        cx = float(cx) if cx not in (None, "") else (piv or (0.0, 0.0))[0]
        cy = float(cy) if cy not in (None, "") else (piv or (0.0, 0.0))[1]
        rel = False
        x = y = None
        for i, s in enumerate(slots):
            if s.deleted:
                continue
            cmd, words = parse_words(s.text)
            cc = canon_cmd(cmd) if cmd else ""
            if cc in ("G90", "G91"):
                rel = cc == "G91"
            if cc not in _MOTION or rel:
                continue
            try:
                nx = float(words["X"]) if "X" in words else x
                ny = float(words["Y"]) if "Y" in words else y
            except ValueError:
                continue
            x, y = nx, ny
            if i not in chosen:
                continue
            vals = {}
            if ("X" in words or "Y" in words) and x is not None \
                    and y is not None:
                ux, uy = x - cx, y - cy
                vals["X"] = cx + co * ux - si * uy + d[0]
                vals["Y"] = cy + si * ux + co * uy + d[1]
            if cc in ("G2", "G3") and ("I" in words or "J" in words):
                try:
                    ii = float(words.get("I", 0.0))
                    jj = float(words.get("J", 0.0))
                except ValueError:
                    ii = jj = None
                if ii is not None:
                    vals["I"] = co * ii - si * jj
                    vals["J"] = si * ii + co * jj
            if "Z" in words and d[2]:
                vals["Z"] = float(words["Z"]) + d[2]
            if vals:
                s.text = _set_words(s.text, vals, add=True)
                s.origin, s.chip = "transform", cid

    def _apply_transform(self, slots, c, ending):
        index = self._index(slots)
        sel = self._scope_slots(slots, c.scope, index)
        for i in sel:
            if slots[i].src is None:
                slots[i].src = slots[i].text
        p = c.params or {}
        op = c.op
        chosen = set(sel)
        if op == "offset" and float(p.get("rz") or 0.0) % 360.0:
            self._move_rotate(slots, chosen, p, c.id)
        elif op == "offset":
            d = {"X": float(p.get("dx", 0)), "Y": float(p.get("dy", 0)),
                 "Z": float(p.get("dz", 0))}
            rel = False
            for i, s in enumerate(slots):
                cmd, words = parse_words(s.text)
                cc = canon_cmd(cmd) if cmd else ""
                if cc in ("G90", "G91"):
                    rel = cc == "G91"
                if i not in chosen or rel or cc not in _MOTION or s.deleted:
                    continue
                vals = {L: float(words[L]) + d[L] for L in d
                        if L in words and d[L]}
                if vals:
                    s.text = _set_words(s.text, vals)
                    s.origin, s.chip = "transform", c.id
        elif op == "scale":
            k = {"X": float(p.get("px", 100.0)) / 100.0,
                 "Y": float(p.get("py", 100.0)) / 100.0,
                 "Z": float(p.get("pz", 100.0)) / 100.0}
            k["I"], k["J"] = k["X"], k["Y"]
            rel = False
            for i, s in enumerate(slots):
                cmd, words = parse_words(s.text)
                cc = canon_cmd(cmd) if cmd else ""
                if cc in ("G90", "G91"):
                    rel = cc == "G91"
                if i not in chosen or rel or cc not in _MOTION or s.deleted:
                    continue
                vals = {}
                for L, f in k.items():
                    if L in words and f != 1.0:
                        try:
                            vals[L] = float(words[L]) * f
                        except ValueError:
                            pass
                if vals:
                    s.text = _set_words(s.text, vals)
                    s.origin, s.chip = "transform", c.id
        elif op == "layer_shift":
            self._layer_shift(slots, sel, c)
        elif op in ("speed_set", "speed_scale"):
            if op == "speed_set":
                tgt = lambda f: float(p.get("mm_s", 10.0)) * 60.0  # noqa
            else:
                k = float(p.get("percent", 100.0)) / 100.0
                tgt = lambda f: f * k                               # noqa
            orig = out_f = None
            run_end = None      # the last selected move of the current run
            for i, s in enumerate(slots):
                if s.deleted:
                    continue
                cmd, words = parse_words(s.text)
                if canon_cmd(cmd or "") not in _MOTION:
                    continue
                if i not in chosen and run_end is not None:
                    # the file's own feed, back right after the selection,
                    # so a line placed after it keeps its speed too
                    was = orig if orig is not None else DEFAULT_FEED
                    if out_f is not None and abs(out_f - was) > 1e-9:
                        t = slots[run_end].text
                        body = t.rstrip("\r\n")
                        slots[run_end].text = (body + ending + "G1 F"
                                               + fmt_num(was)
                                               + (t[len(body):] or ending))
                        out_f = was
                    run_end = None
                if i in chosen:
                    run_end = i
                if "F" in words:
                    try:
                        orig = float(words["F"])
                    except ValueError:
                        pass
                cur = orig if orig is not None else DEFAULT_FEED
                want = tgt(cur) if i in chosen else cur
                if op == "speed_scale" and "E" in words and i in chosen \
                        and has_rotary_words(s.text):
                    # a split-feed move (F linear, E rotary): both rates
                    # scale, so the surface speed scales and the path holds
                    try:
                        s.text = _set_words(s.text,
                                            {"E": float(words["E"]) * k})
                        s.origin, s.chip = "transform", c.id
                    except ValueError:
                        pass
                if "F" in words:
                    if i in chosen:
                        s.text = _set_words(s.text, {"F": want})
                        s.origin, s.chip = "transform", c.id
                    out_f = want
                elif out_f is None or abs(want - out_f) > 1e-9:
                    if i in chosen or out_f is not None:
                        body = s.text.rstrip("\r\n")
                        s.text = body + " F" + fmt_num(want) + \
                            s.text[len(body):]
                        s.origin, s.chip = "transform", c.id
                        out_f = want
        elif op == "round":
            dec = int(p.get("decimals", 3))
            for i in sel:
                s = slots[i]
                cmd, words = parse_words(s.text)
                if canon_cmd(cmd or "") not in _MOTION:
                    continue
                vals = {}
                for L in "XYZIJ":
                    if L in words:
                        try:
                            vals[L] = round(float(words[L]), dec)
                        except ValueError:
                            pass
                new = _set_words(s.text, vals, precision=dec)
                if new != s.text:
                    s.text, s.origin, s.chip = new, "transform", c.id
        elif op == "arcs_to_chords":
            seg = max(1e-3, float(p.get("max_seg", 0.1)))
            w = _Walker()
            for i, s in enumerate(slots):
                if s.deleted:
                    continue
                x0, y0 = (w.x or 0.0), (w.y or 0.0)
                cmd, words, _d, _z = w.step(s.text)
                if i not in chosen or cmd not in ("G2", "G3"):
                    continue
                try:
                    ii = float(words.get("I", 0))
                    jj = float(words.get("J", 0))
                except ValueError:
                    continue
                x1, y1 = w.x or 0.0, w.y or 0.0
                cx, cy = x0 + ii, y0 + jj
                r = math.hypot(ii, jj)
                a0 = math.atan2(y0 - cy, x0 - cx)
                L = _arc_len(x0, y0, x1, y1, ii, jj, cmd == "G2")
                n = max(1, int(math.ceil(L / seg)))
                sweep = (L / r if r > 0 else 0.0) * (-1 if cmd == "G2" else 1)
                pts = []
                for k in range(1, n + 1):
                    a = a0 + sweep * k / n
                    pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
                pts[-1] = (x1, y1)
                lines = []
                for k, (px, py) in enumerate(pts):
                    t = "G1 X%s Y%s" % (fmt_num(px), fmt_num(py))
                    if k == 0 and "F" in words:
                        t += " F" + words["F"]
                    lines.append(t)
                s.text = ending.join(lines) + ending
                s.origin, s.chip = "transform", c.id
        elif op == "repeat":
            if not sel:
                return slots
            copies = max(0, int(p.get("copies", 1)))
            dx, dy, dz = (float(p.get("dx", 0)), float(p.get("dy", 0)),
                          float(p.get("dz", 0)))
            block = [slots[i] for i in sel if not slots[i].deleted]
            extra = []
            for k in range(1, copies + 1):
                for j, s in enumerate(block):
                    cmd, words = parse_words(s.text)
                    t = s.text
                    if canon_cmd(cmd or "") in _MOTION:
                        vals = {}
                        for L, dv in (("X", dx), ("Y", dy), ("Z", dz)):
                            if L in words and dv:
                                vals[L] = float(words[L]) + k * dv
                        t = _set_words(t, vals)
                    if not t.endswith(("\n", "\r")):
                        t += ending
                    extra.append(_Slot(t, (), "transform", c.id,
                                       readonly=True,
                                       uid=("rep", c.id, k, j),
                                       piece=s.piece,
                                       copy_of=(s, k, {"X": dx, "Y": dy,
                                                       "Z": dz})))
            last = sel[-1]
            if not slots[last].text.endswith(("\n", "\r")):
                slots[last].text += ending
            slots = slots[:last + 1] + extra + slots[last + 1:]
        return slots

    def _link_starts(self):
        """``({("base", group, layer, pass): k}, known)``: where each pass's
        turnaround starts within its bucket, from the generator's "link"
        marks. `known` is False for a map with several passes a layer and
        no link at all -- one written before links were marked, where a
        pass bucket's tail cannot be told from its fibre."""
        lm = self._map()
        addrs = lm.addresses()
        out, multi = {}, False
        for m in lm.marks:
            if m["kind"] == "pass" and int(m.get("pass", 0)) >= 1:
                multi = True
            elif m["kind"] == "link" and 0 <= int(m["line"]) < len(addrs):
                a = addrs[int(m["line"])]
                out[("base", a.group, a.layer, a.pass_)] = a.k
        return out, bool(out) or not multi

    def layer_shift_stats(self, cid):
        """What the layer shift `cid` did in the last render: {"layers",
        "first_f", "last_f", "passes", "wavy_passes", "flat_layers",
        "refused"} -- None before it has rendered."""
        return self.__dict__.setdefault("_shift_stats", {}).get(cid)

    def _layer_shift(self, slots, sel, c):
        """MICROSCALE LAYER SHIFTING: each selected layer's path offset by its
        own amount, so a fibre lands beside the one below instead of on top
        of it -- the vertical stacking the jet's electrostatic AUTOFOCUSING
        onto the last fibre otherwise enforces. Liashenko, Hrynevich &
        Dalton, Adv. Mater. 2020, 32, 2001874 introduced it (tilted,
        branched, textured and overhanging walls, tapered domes); Devlin et
        al., Adv. Mater. Technol. 2024, 9, 2400419 built its "phase" and
        "amplitude" offsets into a g-code generator. Both are ported here,
        not re-derived:

          * the selected layers are counted 0, 1, 2 ... in file order and
            layer n moves `layer_shift_factor(profile, n)` STEPS. Ramp is
            both papers' rule: the first layer is the reference (Devlin's
            ``pha*(n-1)``; Liashenko's "first layer had the same pattern").
          * X / Y step -- the whole layer, rigidly (tilted walls,
            overhangs; Alternate gives Liashenko's branching).
          * Along -- Devlin's phase: "displaces each node in the print
            direction". Each pass slides along ITS OWN travel direction
            (the chord from its start to its end), so a serpentine's
            opposite passes slide opposite ways and negative is always
            backward -- Liashenko's correction of a forward-tilting wall.
          * Amplitude -- Devlin's amplitude: "shifts nodes in the direction
            perpendicular to the print". The pass's offset from its chord
            is scaled so its peak grows by the µm step, plus the % step of
            its own amplitude (Liashenko's 2% per layer).
          * Radius -- the layer scaled about its own bounding-box centre so
            its farthest point moves out by the step (Liashenko's domes,
            "radius varied both linearly and periodically").

        A PASS ENDS WHERE ITS TURNAROUND BEGINS (the generator's "link"
        mark), and both pass ends stay where they are: the amplitude is
        zero at the chord's ends by definition, and the phase slide tapers
        to nothing over 2|D| from each end. The taper's slope is at most
        1/2, so the slide is a monotone map along the chord and can never
        fold a pass back on itself, whatever the pattern. So the
        turnarounds come through the phase and amplitude byte for byte, and
        the X / Y step and radius move them with their layer.

        An ARC keeps its circle: its centre is moved by the same map as its
        end, and its I/J re-measured from where the previous move now ends
        (R scaled with the radius step). Z, F, the header, the exit,
        relative moves and lines the user added are left alone. A file
        with a rotary axis is refused (3-axis only), and one without a
        generator's passes (an opened file, a pre-link map) gets the X / Y
        step and radius only; `layer_shift_stats` says so."""
        p = c.params or {}

        def num(k, d=0.0):
            try:
                return float(p.get(k, d) or 0.0)
            except (TypeError, ValueError):
                return 0.0
        um = 1e-3                       # the fields are µm; the g-code is mm
        dx, dy, along = num("dx") * um, num("dy") * um, num("along") * um
        amp, rad = num("amp") * um, num("radius") * um
        pct = num("amp_pct") / 100.0
        profile = p.get("profile")
        if profile not in LAYER_SHIFT_PROFILES:
            profile = "ramp"
        period = int(num("period", 10)) or 1
        try:
            cycle = parse_shift_cycle(p.get("cycle", ""))
        except ValueError:
            cycle = []
        stats = {"layers": 0, "first_f": 0.0, "last_f": 0.0, "passes": 0,
                 "wavy_passes": 0, "flat_layers": 0, "refused": None}
        self.__dict__.setdefault("_shift_stats", {})[c.id] = stats
        if not any((dx, dy, along, amp, pct, rad)):
            return
        if has_rotary_words("".join(s.text for s in slots if not s.deleted)):
            stats["refused"] = "rotary"
            return
        chosen = set(sel)
        w = _Walker()
        moves = {}              # slot -> (x0, y0, x1, y1, cmd, words)
        for i, s in enumerate(slots):
            if s.deleted:
                continue
            x0, y0 = w.x, w.y
            cmd, words, _d, _z = w.step(s.text)
            k = s.key
            if cmd not in _MOTION or i not in chosen or w.rel or not k \
                    or k[1] != "body" or k[3] < 0 \
                    or None in (x0, y0, w.x, w.y):
                continue
            moves[i] = (x0, y0, w.x, w.y, cmd, words)
        layers = {}
        for i in moves:
            k = slots[i].key
            layers.setdefault((k[0], k[2], k[3]), []).append(i)
        links, links_known = self._link_starts()
        wavy = bool(along or amp or pct)
        mapping = {}            # slot -> (layer map, pass map or None, f)
        for n, (lk, idx) in enumerate(layers.items()):
            f = layer_shift_factor(profile, n, period, cycle)
            if n == 0:
                stats["first_f"] = f
            stats["last_f"] = f
            xs = [moves[i][2] for i in idx]
            ys = [moves[i][3] for i in idx]
            cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
            rm = max(math.hypot(x - cx, y - cy) for x, y in zip(xs, ys))
            kr = 1.0 + f * rad / rm if rad and rm > 1e-9 else 1.0
            lay = (cx, cy, kr, f * dx, f * dy)
            pmap = {}
            if wavy and links_known:
                buckets = {}
                for i in idx:
                    if slots[i].key[4] >= 0:
                        buckets.setdefault(slots[i].key[4], []).append(i)
                for ps, bi in buckets.items():
                    at = links.get(lk + (ps,))
                    own = [i for i in bi if at is None
                           or slots[i].key[5] < at]
                    if not own or any(moves[i][4] in ("G2", "G3")
                                      for i in own):
                        continue
                    sx, sy = moves[own[0]][0], moves[own[0]][1]
                    ex, ey = moves[own[-1]][2], moves[own[-1]][3]
                    L = math.hypot(ex - sx, ey - sy)
                    if L < 1e-9:
                        continue
                    t = ((ex - sx) / L, (ey - sy) / L)
                    W = max(abs(-(moves[i][2] - sx) * t[1]
                                + (moves[i][3] - sy) * t[0]) for i in own)
                    g = 1.0 + f * pct + (f * amp / W if W > 1e-9 else 0.0)
                    info = (sx, sy, t, L, g, f * along)
                    for i in own:
                        pmap[i] = info
                    stats["passes"] += 1
                    stats["wavy_passes"] += W > 1e-6
                if not pmap:
                    stats["flat_layers"] += 1
            for i in idx:
                mapping[i] = (lay, pmap.get(i))
        stats["layers"] = len(layers)

        def moved(i, x, y):
            lay, info = mapping[i]
            if info is not None:
                sx, sy, t, L, g, D = info
                rx, ry = x - sx, y - sy
                u = rx * t[0] + ry * t[1]
                v = -rx * t[1] + ry * t[0]
                if D:
                    ramp = _PHASE_RAMP * abs(D)
                    u += D * min(1.0, max(0.0, min(u, L - u) / ramp))
                v *= g
                x, y = sx + u * t[0] - v * t[1], sy + u * t[1] + v * t[0]
            cx, cy, kr, ox, oy = lay
            return cx + kr * (x - cx) + ox, cy + kr * (y - cy) + oy

        w = _Walker()
        here = None             # where the nozzle now is, shifted
        for i, s in enumerate(slots):
            if s.deleted:
                continue
            x0, y0 = w.x, w.y
            cmd, words, _d, _z = w.step(s.text)
            if cmd not in _MOTION:
                continue
            if i not in mapping:
                if here is None:
                    here = (w.x, w.y)
                elif w.rel:     # a relative move carries the shift on
                    here = (here[0] + (w.x or 0.0) - (x0 or 0.0),
                            here[1] + (w.y or 0.0) - (y0 or 0.0))
                else:           # a word it leaves out stays where it was
                    here = (w.x if "X" in words else here[0],
                            w.y if "Y" in words else here[1])
                continue
            nx, ny = moved(i, w.x, w.y)
            vals = {}
            for j, (L, new, old) in enumerate((("X", nx, w.x),
                                               ("Y", ny, w.y))):
                # a word the line names is compared with itself; a modal
                # one with where the nozzle now really is
                was = old if L in words or here is None else here[j]
                if fmt_num(new) != fmt_num(was):
                    vals[L] = new
            if cmd in ("G2", "G3") and ("I" in words or "J" in words):
                try:
                    ii, jj = float(words.get("I", 0)), float(words.get("J", 0))
                except ValueError:
                    ii = jj = None
                if ii is not None:
                    hx, hy = here if here is not None else (x0, y0)
                    ccx, ccy = _arc_centre((hx, hy), (nx, ny),
                                           moved(i, x0 + ii, y0 + jj))
                    for L, new, old in (("I", ccx - hx, ii),
                                        ("J", ccy - hy, jj)):
                        if fmt_num(new) != fmt_num(old):
                            vals[L] = new
            elif cmd in ("G2", "G3") and "R" in words:
                kr = mapping[i][0][2]
                if kr != 1.0:
                    try:
                        vals["R"] = float(words["R"]) * kr
                    except ValueError:
                        pass
            if vals:
                new = _put_words(s.text, vals)
                if new != s.text:
                    s.text, s.origin, s.chip = new, "transform", c.id
            here = (nx, ny)

    def _rule_positions(self, rule, slots, index, walk):
        """``[(pos, ctx)]`` for every place `rule` fires, in file order."""
        w = rule.where
        out = []
        layer_of = {u: n for n, u in enumerate(index["layers"], 1)}
        group_of = {u: n for n, u in enumerate(index["groups"], 1)}
        piece_of = {u: n for n, u in enumerate(index["pieces"], 1)}

        def ctx_at(i, before):
            st = walk[i - 1] if before and i > 0 else (
                walk[i] if not before else
                {"x": 0.0, "y": 0.0, "z": 0.0, "t": 0.0})
            k = slots[i].key if 0 <= i < len(slots) else ()
            layer = 0
            if k and k[1] == "body" and k[3] >= 0:
                layer = layer_of.get((k[0], k[2], k[3]), 0)
            else:
                prior = [layer_of[(s.key[0], s.key[2], s.key[3])]
                         for s in slots[:i + 1] if s.key and
                         s.key[1] == "body" and s.key[3] >= 0]
                layer = prior[-1] if prior else 0
            return {"layer": layer, "z": fmt_num(st["z"]),
                    "x": fmt_num(st["x"]), "y": fmt_num(st["y"]),
                    "time_s": fmt_num(st["t"], 1),
                    "piece": piece_of.get(k[0], 1) if k else 1}

        def selected(n):
            lo = int(rule.first) if rule.first not in (None, "") else 1
            hi = int(rule.last) if rule.last not in (None, "") else None
            ev = max(1, int(rule.every or 1))
            return n >= lo and (hi is None or n <= hi) and (n - lo) % ev == 0
        if w == "start":
            return [(("top", None), ctx_at(0, True))] if slots else []
        if w == "end":
            n = len(slots) - 1
            return [(("bottom", None), ctx_at(n, False))] if slots else []
        if w == "before_first_print":
            for i, s in enumerate(slots):
                c, _ = parse_words(s.text)
                if canon_cmd(c or "") in ("G1", "G2", "G3"):
                    return [(("before", i), ctx_at(i, True))]
            return []
        if w in ("before_layer", "after_layer"):
            for u in index["layers"]:
                if not selected(layer_of[u]):
                    continue
                a, b = index["units"]["layer"][u]
                if w == "before_layer":
                    out.append((("before", a), ctx_at(a, True)))
                else:
                    out.append((("after", b), ctx_at(b, False)))
            return out
        if w in ("before_group", "after_group"):
            for u in index["groups"]:
                if not selected(group_of[u]):
                    continue
                a, b = index["units"]["group"][u]
                if w == "before_group":
                    out.append((("before", a), ctx_at(a, True)))
                else:
                    out.append((("after", b), ctx_at(b, False)))
            return out
        if w in ("before_travel", "after_travel"):
            for i, st in enumerate(walk):
                if st["travel"] and slots[i].key and not slots[i].deleted:
                    out.append(((w.split("_")[0], i),
                                ctx_at(i, w == "before_travel")))
            return out
        if w in ("every_n_moves", "every_n_mm", "every_n_seconds"):
            n = float(rule.n or 0)
            if n <= 0:
                return []
            field_ = {"every_n_moves": "moves", "every_n_mm": "mm",
                      "every_n_seconds": "t"}[w]
            nxt = n
            for i, st in enumerate(walk):
                if st[field_] + 1e-9 >= nxt and slots[i].key:
                    out.append((("after", i), ctx_at(i, False)))
                    while st[field_] + 1e-9 >= nxt:
                        nxt += n
            return out
        if w == "at_z":
            zt = float(rule.n or 0)
            for i, st in enumerate(walk):
                if st["moved"] and st["z"] + 1e-9 >= zt:
                    return [(("before", i), ctx_at(i, True))]
            return []
        raise ValueError("unknown rule place %r" % (w,))

    # -- added lines, and the operations on edits -------------------------- #
    def _placements(self, slots, index, walk, fl, ending, unplaced):
        """Every added line, chip by chip in list order: an Insert at its
        anchor, a Rule at each place it fires, a Repeat's copies of the edit
        it sits under. Then each geometric operation on an edit changes the
        lines that edit writes, copies included. Returns ``[{"pos", "chip",
        "ctx", "lines": [(text, origin, chip, readonly, uid)]}]``."""
        pls = []
        for c in self.changes:
            if not self._shown(c):
                continue
            if isinstance(c, Insert):
                pos = self._resolve(c.anchor, index)
                if pos is None:
                    unplaced.append(c.id)
                    continue
                lines = list(_render_insert_lines(c, fl))
                pls.append({"pos": pos, "chip": c.id, "ctx": None, "lines": [
                    (ln, "insert", c.id, False, ("ins", c.id, j))
                    for j, ln in enumerate(lines)]})
                # the same spot on every array copy of its scaffold
                for n, k in enumerate(self._copy_keys(c.anchor.key), 1):
                    cp = self._resolve(Anchor(c.anchor.unit, c.anchor.side,
                                              k), index)
                    if cp is not None:
                        pls.append({"pos": cp, "chip": c.id, "ctx": None,
                                    "lines": [
                            (ln, "insert", c.id, True, ("ins", c.id, j, n))
                            for j, ln in enumerate(lines)]})
            elif isinstance(c, Rule):
                rlines = _rule_lines(c, fl)
                for occ, (pos, ctx) in enumerate(
                        self._rule_positions(c, slots, index, walk)):
                    pls.append({"pos": pos, "chip": c.id, "ctx": ctx,
                                "lines": [
                        (fill_placeholders(ln, dict(machine_placeholder_context(
                                                    self.options.context, fl),
                                                    **dict(ctx, n=occ + 1))), "rule",
                         c.id, True, ("rule", c.id, occ, j))
                        for j, ln in enumerate(rlines)]})
            elif self.parent_id(c) and c.op == "repeat":
                pls += self._repeat_copies(c, pls, slots, index, walk, fl)
        pls += self._block_copies(pls, slots)
        for c in self.changes:
            if not isinstance(c, Transform) or c.op not in _GEOMETRIC \
                    or c.op == "repeat" or not self._shown(c):
                continue
            ids, at, everywhere = self._scope_reach(c.scope, slots, index)
            for pl in pls:
                where, i = pl["pos"]
                if pl["chip"] in ids or everywhere or (
                        where in ("before", "after") and i in at):
                    self._op_on_lines(pl, c, walk)
        return pls

    @staticmethod
    def _block_copies(pls, slots):
        """The edits placed at a line that Repeat a block copied, placed
        again at each copy of that line and shifted as the copy is."""
        at = {}
        for pl in pls:
            where, i = pl["pos"]
            if where in ("before", "after"):
                at.setdefault(i, []).append(pl)
        if not at:
            return []
        row_of = {id(s): i for i, s in enumerate(slots)}
        out = []
        for ci, s in enumerate(slots):
            if s.copy_of is None:
                continue
            src, k, shift = s.copy_of
            for pl in at.get(row_of.get(id(src)), ()):
                out.append({"pos": (pl["pos"][0], ci), "chip": pl["chip"],
                            "ctx": pl["ctx"], "lines": [
                    (_shift_line(t, shift, k), origin, chip, True,
                     tuple(uid) + ("blk",) + tuple(s.uid))
                    for t, origin, chip, _ro, uid in pl["lines"]]})
        return out

    def _scope_reach(self, scope, slots, index):
        """``(edit ids, slot indices, everywhere)``: which added lines a
        change reaches -- those an edit in `edit ids` writes, those placed
        at a generated line in `slot indices` (the edits sitting in a part),
        or all of them. Exact "lines" reach no added line."""
        kind = scope.kind if scope else "all"
        if kind == "all":
            return set(), set(), True
        if kind == "edit":
            return self.lineage(str(scope.first or "")), set(), False
        if kind == "lines":
            return set(), set(), False
        if kind == "set":
            ids, at, every = set(), set(), False
            for sub in scope.first or ():
                a, b, e = self._scope_reach(Scope.from_dict(sub), slots,
                                            index)
                ids |= a
                at |= b
                every = every or e
            return ids, at, every
        return set(), set(self._scope_slots(slots, scope, index)), False

    def _edit_base(self, cid):
        """The edit an operation chain starts from: `cid`, or, through any
        geometric operations above it (they change lines, they write none),
        the edit or Repeat they sit under."""
        c = self.get(cid)
        for _ in range(64):
            if not self.parent_id(c) or c.op == "repeat":
                break
            c = self.get(self.parent_id(c))
        return c

    def _source_lines(self, c, slots, fl):
        """``(lines, template)``: what a Repeat of edit `c` copies, as it is
        before any operation on it; `template` when they are a Rule's, with
        placeholders to fill at each copy."""
        if isinstance(c, Insert):
            return list(_render_insert_lines(c, fl)), False
        if isinstance(c, Rule):
            return list(_rule_lines(c, fl)), True
        if self.parent_id(c):                   # a Repeat: what it repeats
            return self._source_lines(
                self._edit_base(self.parent_id(c)), slots, fl)
        out = []
        for i in self._edit_slots(slots, c.id):
            s = slots[i]
            t = s.src if s.src is not None else s.text
            out += [p.rstrip("\r\n") for p in _split_text_lines(t)]
        return out, False

    def _own_positions(self, c, pls, slots):
        """``[(pos, ctx)]``: where edit `c`'s own lines are."""
        if isinstance(c, (Insert, Rule)) or self.parent_id(c):
            return [(pl["pos"], pl["ctx"]) for pl in pls
                    if pl["chip"] == c.id]
        sel = self._edit_slots(slots, c.id)
        return [(("after", sel[-1]), None)] if sel else []

    def _repeat_copies(self, r, pls, slots, index, walk, fl):
        """The placements a Repeat on an edit writes: the edit's lines again
        right after it (`REPEAT_HERE`, `copies` times) or at every place of
        a pattern but its own, the k-th copy shifted k times."""
        p = dict(EDIT_REPEAT_DEFAULTS, **(r.params or {}))
        try:
            base = self._edit_base(self.parent_id(r))
        except KeyError:
            return []
        lines, template = self._source_lines(base, slots, fl)
        if not lines:
            return []
        own = self._own_positions(base, pls, slots)
        w = p.get("where") or REPEAT_HERE
        if w == REPEAT_HERE:
            n = max(0, int(p.get("copies") or 0))
            places = [(pos, ctx, k) for pos, ctx in own
                      for k in range(1, n + 1)]
        else:
            pattern = Rule(where=w, every=int(p.get("every") or 1),
                           first=p.get("first"), last=p.get("last"),
                           n=float(p.get("n") or 0.0))
            mine = {pos for pos, _ctx in own}
            places = []
            for pos, ctx in self._rule_positions(pattern, slots, index, walk):
                if pos not in mine:
                    places.append((pos, ctx, len(places) + 1))
        shift = {L: float(p.get(k) or 0.0)
                 for L, k in (("X", "dx"), ("Y", "dy"), ("Z", "dz"))}
        out, total = [], 0
        for occ, (pos, ctx, k) in enumerate(places):
            total += len(lines)
            if total > REPEAT_MAX_LINES:
                break
            row = []
            for j, ln in enumerate(lines):
                if template and ctx is not None:
                    ln = fill_placeholders(ln, dict(ctx, n=occ + 1))
                row.append((_shift_line(ln, shift, k), "transform", r.id,
                            True, ("rep", r.id, occ, j)))
            out.append({"pos": pos, "chip": r.id, "ctx": ctx, "lines": row})
        return out

    def _op_on_lines(self, pl, c, walk):
        """Geometric operation `c` on one placement's lines, in place. The
        nozzle's state where they sit is a seed line ahead of them, so an
        arc starts where the nozzle is and a speed is told from the feed in
        force; a speed it changes is put back after them, so the design's
        moves that follow keep theirs."""
        where, i = pl["pos"]
        st = None
        if where == "after" and 0 <= i < len(walk):
            st = walk[i]
        elif where == "before" and 0 < i <= len(walk):
            st = walk[i - 1]
        elif where == "bottom" and walk:
            st = walk[-1]
        feed = st["f"] if st else DEFAULT_FEED
        seed = "G1 X%s Y%s Z%s F%s" % (fmt_num(st["x"]), fmt_num(st["y"]),
                                       fmt_num(st["z"]), fmt_num(feed)) \
            if st else "G1 F%s" % fmt_num(feed)
        tmp = [_Slot(seed + "\n")] + [
            _Slot(t + "\n", ("~", "edit", -1, -1, -1, j))
            for j, (t, *_r) in enumerate(pl["lines"])]
        before = [s.text for s in tmp]
        tmp = self._apply_transform(
            tmp, Transform(op=c.op, params=c.params, scope=Scope()), "\n")
        rows = []
        for (t, origin, chip, ro, uid), s, was in zip(pl["lines"], tmp[1:],
                                                      before[1:]):
            if s.text == was:
                rows.append((t, origin, chip, ro, uid))
                continue
            parts = [x.rstrip("\r\n") for x in _split_text_lines(s.text)]
            for k, part in enumerate(parts):
                rows.append((part, "transform", c.id, True,
                             uid + ((k,) if k else ())))
        if c.op in ("speed_set", "speed_scale") and rows:
            def end_feed(texts):
                w = _Walker()
                for t in texts:
                    w.step(t)
                return w.f
            was_f = end_feed(before)
            now_f = end_feed([seed] + [r[0] for r in rows])
            if abs(now_f - was_f) > 1e-9:
                rows.append(("G1 F%s" % fmt_num(was_f), "transform", c.id,
                             True, ("feed", c.id) + tuple(pl["lines"][0][4])))
        pl["lines"] = rows

    # -- edits ------------------------------------------------------------- #
    def record_edit(self, new_text):
        """Turn the page's edited text into chips. `new_text` is the export
        form after the user typed in it. Every change lands as a chip in
        structural terms. A line that is not a generated line of its own (a
        join, the zero move, an option line, a Start/End block line, one
        place a rule fills in) is kept as an edit on that line
        (`LineEdit.at`). Returns an `EditResult`."""
        res = EditResult()
        known = {c.id for c in self.changes}
        try:
            self._record_edit(new_text, res)
        finally:
            self._rec_old, self._rec_respelled = None, False
            for c in self.changes:
                if c.id not in known:
                    c.typed = True
                    if not c.what:
                        c.what = "line" if isinstance(c, LineEdit) \
                            else "custom"
        return res

    def _record_edit(self, new_text, res):
        old = self.render().lines
        self._rec_old = old
        self._rec_respelled = any(o.src is not None for o in old)
        a = [ln.text.rstrip("\r\n") for ln in old]
        b = [ln.rstrip("\r\n") for ln in new_text.splitlines()]
        lo = 0
        while lo < len(a) and lo < len(b) and a[lo] == b[lo]:
            lo += 1
        hi_a, hi_b = len(a), len(b)
        while hi_a > lo and hi_b > lo and a[hi_a - 1] == b[hi_b - 1]:
            hi_a -= 1
            hi_b -= 1
        if lo == hi_a and lo == hi_b:
            return res
        sm = difflib.SequenceMatcher(None, a[lo:hi_a], b[lo:hi_b],
                                     autojunk=False)
        ops = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "equal":
                ops.append((tag, i1 + lo, i2 + lo, j1 + lo, j2 + lo))
        # apply from the bottom so earlier positions stay valid for anchors
        for tag, i1, i2, j1, j2 in reversed(ops):
            n_pair = min(i2 - i1, j2 - j1) if tag == "replace" else 0
            for k in range(n_pair):
                self._edit_line(old[i1 + k], b[j1 + k], res)
            if tag in ("delete", "replace"):
                self._delete_lines(old, old[i1 + n_pair:i2], res)
            if tag in ("insert", "replace") and j2 - j1 > n_pair:
                self._insert_lines(old, i1 + n_pair, b[j1 + n_pair:j2], res)
        return res

    def _at_edit_for(self, uid):
        """The edit made on the rendered line `uid` (`LineEdit.at`)."""
        uid = tuple(uid)
        for c in self.changes:
            if isinstance(c, LineEdit) and c.at and tuple(c.at) == uid:
                return c
        return None

    def _at_stage(self, rl):
        """"export" for a line only the export has (the Start/End blocks,
        the zero move, the custom line, a line the export split in two),
        typed and kept in the export's own spelling; "" for the rest."""
        if rl.origin in ("machine", "origin") \
                or tuple(rl.uid) == ("opt", "custom_line"):
            return "export"
        if rl.src is None and getattr(self, "_rec_respelled", False):
            return "export"
        return ""

    def _at_key(self, rl):
        """The generated line `rl` belongs to, for where its edit sits:
        its own key, else the key of the line it was split from."""
        if rl.key:
            return tuple(rl.key)
        uid = tuple(rl.uid)
        for o in getattr(self, "_rec_old", None) or ():
            if o.key and len(uid) > len(o.uid) \
                    and tuple(o.uid) == uid[:len(o.uid)]:
                return tuple(o.key)
        return ()

    def _at_edit(self, rl, new, res, shift=None):
        """Typing over (`new`) or deleting (`new` None) a line that is not
        a generated line of its own: an edit on its rendered identity, made
        or changed. Typing it back as it was removes the edit."""
        stage = self._at_stage(rl)
        if new is not None and stage == "export":
            fl = get_flavor(self.flavor)
            if fl.line_numbers:
                new = re.sub(r"^\s*N\d+\s*", "", new)
        c = self._at_edit_for(rl.uid)
        if c is None:
            if new is not None and not shift \
                    and new == (rl.src if rl.src is not None
                                else rl.text).rstrip("\r\n"):
                return
            c = self.add(LineEdit(key=self._at_key(rl), at=tuple(rl.uid),
                                  stage=stage, flavor=self.flavor,
                                  op="delete" if new is None else "replace",
                                  text="" if new is None else new))
            _mark(res.added, c.id)
        else:
            c.op = "delete" if new is None else "replace"
            c.text, c.flavor = ("" if new is None else new), self.flavor
            if shift:
                self._add_shift(c, shift)
                shift = None
            if new is not None and not c.shift:
                was, c.on = c.on, False
                try:
                    orig = next((
                        (o.src if o.src is not None and stage == ""
                         else o.text).rstrip("\r\n")
                        for o in self.render().lines
                        if tuple(o.uid) == tuple(c.at)), None)
                finally:
                    c.on = was
                if new == orig:
                    self.remove(c.id)
                    _mark(res.removed, c.id)
                    return
            _mark(res.changed, c.id)
        if shift:
            self._add_shift(c, shift)

    def _rule_line_edit(self, rl, new, res):
        """Typing over one line a rule wrote changes the rule -- every place
        it writes that line -- unless the line fills in a placeholder, which
        typed text would freeze: then the typing is kept at that one place,
        and the rule is left alone for its others. So is a line of an
        automatic script (not in the design) or of a rule no longer there.
        `new` None deletes the line."""
        try:
            c = self.get(rl.chip)
        except KeyError:
            return self._at_edit(rl, new, res)
        if _PLACEHOLDER.search(c.text or "") or len(rl.uid) != 4:
            return self._at_edit(rl, new, res)
        lines = _rule_lines(c, self.flavor)
        j = rl.uid[3]
        if not 0 <= j < len(lines):
            return self._at_edit(rl, new, res)
        if new is None:
            del lines[j]
        else:
            lines[j] = new
        if not lines:
            self.remove(c.id)
            _mark(res.removed, c.id)
            return
        c.text, c.flavor, c.command = "\n".join(lines), self.flavor, None
        _mark(res.changed, c.id)

    def _ins_chip(self, rl):
        c = self.get(rl.chip)
        if c.command is not None or c.flavor != self.flavor:
            c.lines = _render_insert_lines(c, get_flavor(self.flavor))
            c.command = None
            c.flavor = self.flavor
        return c

    # -- typing in the machine's spelling ---------------------------------- #
    def _inbound(self, rl, new):
        """``(internal line, shift)`` for `new`, typed over rendered line
        `rl`. The machine's words become G-code again (a travel the export
        spelled as the move word stays a G0). In an INCREMENTAL export the
        position words typed are deltas: the change from the delta shown is
        the shift, and the line keeps its own G-code positions."""
        if rl.src is None:
            return new, {}
        fl = get_flavor(self.flavor)
        internal = from_machine_line(new, fl)
        shown = from_machine_line(rl.text.rstrip("\r\n"), fl)
        sc, _sw = parse_words(rl.src, fl.comment)
        nc, nw = parse_words(internal, fl.comment)
        oc, ow = parse_words(shown, fl.comment)
        if sc and nc and canon_cmd(sc) == "G0" and canon_cmd(nc) == "G1" \
                and oc == nc:
            internal = _respell(internal, _CMD.match(rl.src).group(2))
            nc = sc
        if rl.pos is None or canon_cmd(nc or "") not in _MOTION \
                or canon_cmd(sc or "") not in _MOTION:
            return internal, {}
        shift = {}
        for L in _POS_WORDS:
            try:
                d0 = float(ow.get(L, 0.0))
                d1 = float(nw.get(L, 0.0))
            except ValueError:
                return internal, {}
            if abs(d1 - d0) > 1e-12:
                shift[L] = d1 - d0
        patch = word_patch(shown, internal)
        line = rl.src
        if patch is not None:
            set_, drop = patch
            line = apply_word_patch(
                rl.src, {L: v for L, v in set_.items() if L not in _POS_WORDS},
                [L for L in drop if L not in _POS_WORDS])
        if canon_cmd(nc) != canon_cmd(sc):
            line = _respell(line, nc)
        return line, shift

    def _delta_of(self, rl):
        """The position words of rendered line `rl`, as shown in an
        incremental export: {letter: delta}."""
        if rl.pos is None or rl.src is None:
            return {}
        fl = get_flavor(self.flavor)
        cmd, words = parse_words(from_machine_line(rl.text, fl), fl.comment)
        if canon_cmd(cmd or "") not in _MOTION:
            return {}
        out = {}
        for L, v in words.items():
            if L in _POS_WORDS:
                try:
                    out[L] = float(v)
                except ValueError:
                    pass
        return out

    def _add_shift(self, c, shift, sign=1.0):
        for L, v in shift.items():
            c.shift[L] = c.shift.get(L, 0.0) + sign * v
            if abs(c.shift[L]) <= 1e-12:
                del c.shift[L]

    def _key_shift(self, key, shift, res):
        """Add `shift` to the edit on line `key`, making one when none is."""
        if not shift:
            return
        prev = self._edit_for(key)
        if prev is None:
            prev = self.add(LineEdit(key=tuple(key), op="words"))
            _mark(res.added, prev.id)
        else:
            _mark(res.changed, prev.id)
        self._add_shift(prev, shift)

    def _edit_line(self, rl, new, res):
        new, shift = self._inbound(rl, new)
        if self._at_edit_for(rl.uid) is not None:
            return self._at_edit(rl, new, res, shift)
        if rl.origin == "rule":
            return self._rule_line_edit(rl, new, res)
        if rl.readonly:
            return self._at_edit(rl, new, res, shift)
        if rl.origin == "insert":
            c = self._ins_chip(rl)
            c.lines[rl.uid[2]] = new
            self._add_shift(c, shift)
            _mark(res.changed, c.id)
            return
        if not rl.key:
            return self._at_edit(rl, new, res, shift)
        old = rl.src if rl.src is not None else rl.text
        if new == old.rstrip("\r\n"):
            return self._key_shift(rl.key, shift, res)
        try:
            return self._edit_base_line(rl, old, new, res)
        finally:
            self._key_shift(rl.key, shift, res)

    def _edit_base_line(self, rl, old, new, res):
        patch = word_patch(old, new)
        prev = self._edit_for(rl.key)
        if patch is not None and (prev is None or prev.op == "words"):
            set_, drop = patch
            if prev is None:
                c = self.add(LineEdit(key=tuple(rl.key), op="words",
                                      set=set_, drop=drop))
                _mark(res.added, c.id)
            else:
                prev.set.update(set_)
                for L in set_:
                    if L in prev.drop:
                        prev.drop.remove(L)
                prev.drop += [L for L in drop if L not in prev.drop]
                for L in drop:
                    prev.set.pop(L, None)
                if self._edit_is_noop(prev):
                    self.remove(prev.id)
                    _mark(res.removed, prev.id)
                else:
                    _mark(res.changed, prev.id)
            return
        if prev is not None:
            prev.op, prev.text, prev.set, prev.drop = "replace", new, {}, []
            prev.flavor = self.flavor
            _mark(res.changed, prev.id)
        else:
            c = self.add(LineEdit(key=tuple(rl.key), op="replace", text=new,
                                  flavor=self.flavor))
            _mark(res.added, c.id)

    def _edit_for(self, key):
        for c in self.changes:
            if isinstance(c, LineEdit) and not c.at \
                    and tuple(c.key) == tuple(key):
                return c
        return None

    def _edit_is_noop(self, c):
        """True when a words edit no longer changes its line."""
        if c.shift:
            return False
        was, c.on = c.on, False
        try:
            lines = self.render().lines
        finally:
            c.on = was
        for rl in lines:
            if rl.key == tuple(c.key):
                t = rl.src if rl.src is not None else rl.text
                return apply_word_patch(t, c.set, c.drop) == t
        return False

    def _blockable(self, rl):
        """True for a plain generated line, which a deletion of a block can
        fold into one chip (not a rule's, an insert's or a read-only one)."""
        return bool(rl.key) and rl.origin not in ("rule", "insert") \
            and not rl.readonly and self._edit_for(rl.key) is None \
            and self._at_edit_for(rl.uid) is None

    def _delete_lines(self, old, rls, res):
        """Delete rendered lines `rls`. Each run of plain generated lines in
        one layer is ONE chip -- the whole layer reads "Removed layer 3" --
        instead of one "removed line" chip per line."""
        run = []

        def flush():
            if len(run) > 1:
                self._delete_block(old, run, res)
            else:
                for rl in run:
                    self._delete_line(rl, res)
            run.clear()
        for rl in rls:
            if self._blockable(rl) and (
                    not run or tuple(run[-1].key[:4]) == tuple(rl.key[:4])):
                run.append(rl)
                continue
            flush()
            if self._blockable(rl):
                run.append(rl)
            else:
                self._delete_line(rl, res)
        flush()

    def _delete_block(self, old, run, res):
        gone = {}
        for rl in run:
            for L, v in self._delta_of(rl).items():
                if v:
                    gone[L] = gone.get(L, 0.0) - v
        keys = {tuple(rl.key) for rl in run}
        pre = tuple(run[0].key[:4])
        whole = pre[3] >= 0 and all(
            tuple(o.key) in keys for o in old
            if o.key and tuple(o.key[:4]) == pre and o.origin != "insert"
            and o.origin != "rule")
        c = self.add(LineEdit(key=tuple(run[0].key), op="delete",
                              more=[] if whole
                              else [tuple(rl.key) for rl in run[1:]],
                              unit="layer" if whole else ""))
        self._add_shift(c, gone)
        _mark(res.added, c.id)

    def _delete_line(self, rl, res):
        # an incremental move deleted: the moves after it lose its delta
        gone = {L: -v for L, v in self._delta_of(rl).items() if v}
        if self._at_edit_for(rl.uid) is not None:
            return self._at_edit(rl, None, res, gone)
        if rl.origin == "rule":
            return self._rule_line_edit(rl, None, res)
        if rl.readonly:
            return self._at_edit(rl, None, res, gone)
        if rl.origin == "insert":
            c = self._ins_chip(rl)
            self._add_shift(c, gone)
            del c.lines[rl.uid[2]]
            if c.lines:
                _mark(res.changed, c.id)
            else:
                self.remove(c.id)
                _mark(res.removed, c.id)
            return
        if not rl.key:
            return self._at_edit(rl, None, res, gone)
        prev = self._edit_for(rl.key)
        if prev is not None:
            prev.op, prev.set, prev.drop, prev.text = "delete", {}, [], ""
            _mark(res.changed, prev.id)
        else:
            prev = self.add(LineEdit(key=tuple(rl.key), op="delete"))
            _mark(res.added, prev.id)
        self._add_shift(prev, gone)

    def _inbound_new(self, old, at, lines):
        """``(internal lines, shift)`` for lines typed before rendered line
        `at`: the machine's words read back, and in an INCREMENTAL export
        each move's deltas made positions from where the machine is there.
        The shift is the net move, which every later move then makes too."""
        if not any(o.src is not None for o in old):
            return list(lines), {}
        fl = get_flavor(self.flavor)
        lines = [from_machine_line(ln, fl) for ln in lines]
        here = next((o.pos for o in old[at:] if o.pos is not None), None)
        if here is None:
            last = next((o for o in reversed(old[:at]) if o.pos is not None),
                        None)
            if last is None:
                return lines, {}
            here = dict(last.pos)
            for L, v in self._delta_of(last).items():
                here[L] = here.get(L, 0.0) + v
        here = dict(here)
        start = dict(here)
        out = []
        for ln in lines:
            cmd, words = parse_words(ln, fl.comment)
            if canon_cmd(cmd or "") in _MOTION:
                vals = {}
                for L, v in words.items():
                    if L in _POS_WORDS:
                        try:
                            here[L] = here.get(L, 0.0) + float(v)
                        except ValueError:
                            continue
                        vals[L] = here[L]
                ln = _set_words(ln, vals) if vals else ln
            out.append(ln)
        shift = {L: here[L] - start.get(L, 0.0) for L in here
                 if abs(here[L] - start.get(L, 0.0)) > 1e-12}
        return out, shift

    def _insert_lines(self, old, at, lines, res):
        """New lines typed before rendered line `at` (== len: at the end)."""
        lines, shift = self._inbound_new(old, at, lines)
        prev = old[at - 1] if at > 0 else None
        nxt = old[at] if at < len(old) else None
        if prev is not None and prev.origin == "insert":
            c = self._ins_chip(prev)
            j = prev.uid[2] + 1
            c.lines[j:j] = list(lines)
            self._add_shift(c, shift)
            _mark(res.changed, c.id)
            return
        if nxt is not None and nxt.origin == "insert":
            c = self._ins_chip(nxt)
            j = nxt.uid[2]
            c.lines[j:j] = list(lines)
            self._add_shift(c, shift)
            _mark(res.changed, c.id)
            return
        c = self.add(Insert(lines=list(lines),
                            anchor=self._anchor_between(old, at),
                            flavor=self.flavor, shift=dict(shift)))
        _mark(res.added, c.id)

    def _anchor_between(self, old, at):
        """The structural anchor for a line typed before rendered line `at`:
        the coarsest boundary that lies exactly there."""
        A = next((old[k] for k in range(at - 1, -1, -1)
                  if old[k].key), None)
        B = next((old[k] for k in range(at, len(old)) if old[k].key), None)
        if A is None:
            return Anchor("start", "after")
        if B is None:
            return Anchor("end", "after")
        ka, kb = A.key, B.key
        if ka[0] == "machine":
            # a line of the Machine page's header block: the block is added
            # after the document is composed, so nothing can anchor to it.
            # A line put there goes after the file's own header instead.
            return Anchor("header", "after")
        if ka[1] == "header" and kb[1] != "header":
            return Anchor("header", "after")
        same_g = ka[0] == kb[0] and ka[2] == kb[2] and kb[1] == "body"
        if ka[1] == "body" and not same_g:
            return Anchor("group", "after", (ka[0], ka[2]))
        if ka[1] == "body" and ka[3] != kb[3] and ka[3] >= 0:
            return Anchor("layer", "after", (ka[0], ka[2], ka[3]))
        if ka[1] == "body" and kb[1] == "body" and ka[3] >= 0 \
                and ka[3] != kb[3] and kb[3] >= 0:
            return Anchor("layer", "before", (kb[0], kb[2], kb[3]))
        if ka[1] == "body" and ka[3] == kb[3] and ka[4] != kb[4] \
                and ka[4] >= 0:
            return Anchor("pass", "after", (ka[0], ka[2], ka[3], ka[4]))
        return Anchor("line", "after", tuple(ka))

    # -- structure --------------------------------------------------------- #
    def outline(self, rendered=None):
        """The Outline: `OutlineNode`s Header -> scaffold/piece -> layer ->
        End, each with its first and last EXPORT line (0-based)."""
        r = rendered or self.render()
        nodes = []
        cur_g = cur_l = None
        opens = None        # the first of the layer comments just passed
        for n, rl in enumerate(r.lines):
            k = rl.key
            if rl.chip == "opt:layer":
                # "Mark each layer": it opens the layer it names
                opens = n if opens is None else opens
                continue
            at, opens = (opens if opens is not None else n), None
            if not k:
                if cur_l is not None:
                    cur_l.last = n
                if cur_g is not None:
                    cur_g.last = n
                elif nodes:
                    nodes[-1].last = n
                continue
            pid, sec, g, L = k[0], k[1], k[2], k[3]
            if sec == "header":
                if not nodes or nodes[-1].kind != "header":
                    nodes.append(OutlineNode("Header", n, n, "header"))
                    cur_g = cur_l = None
                nodes[-1].last = n
                continue
            if sec == "exit":
                if not nodes or nodes[-1].kind != "exit":
                    nodes.append(OutlineNode("End (exit move)", n, n, "exit"))
                    cur_g = cur_l = None
                nodes[-1].last = n
                continue
            if cur_g is None or cur_g.key != (pid, g):
                cur_g = OutlineNode(self.group_name(pid, g).capitalize(), at,
                                    n, "group")
                cur_g.key = (pid, g)
                nodes.append(cur_g)
                cur_l = None
            cur_g.last = n
            if L >= 0:
                if cur_l is None or cur_l.key != (pid, g, L):
                    cur_l = OutlineNode("Layer %d" % (L + 1), at, n, "layer")
                    cur_l.key = (pid, g, L)
                    cur_g.children.append(cur_l)
                cur_l.last = n
        lm = self._map()
        if lm.guessed and any(nd.kind == "group" for nd in nodes):
            for nd in nodes:
                if nd.kind == "group":
                    nd.label += GUESS_LABELS.get(lm.basis,
                                                 GUESS_LABELS["height"])
        return nodes

    def describe_line(self, line_no, rendered=None):
        """Plain words for where export line `line_no` (0-based) sits."""
        r = rendered or self.render()
        if not 0 <= line_no < len(r.lines):
            return ""
        rl = r.lines[line_no]
        if rl.origin in ("rule", "insert") or rl.origin == "transform" \
                and not rl.key:
            try:
                return "Added by: %s" % chip_label(self, self.get(rl.chip))
            except KeyError:
                return "Added by a change"
        if rl.origin == "option":
            return ("Output option: wait for temperature"
                    if rl.chip == "opt:wait_temp"
                    else "Output option: custom line")
        if rl.origin == "join":
            return "The travel joining two pieces (it lays fiber)"
        if rl.origin == "flavor":
            return "The flavor's header or footer"
        where = describe_key(self, rl.key)
        if rl.origin in ("edit", "transform"):
            try:
                where += " -- changed by: %s" % chip_label(self,
                                                           self.get(rl.chip))
            except KeyError:
                pass
        return where

    def explain_line(self, line_no, rendered=None):
        """Explain-this-line: what the line does, then where it sits."""
        r = rendered or self.render()
        if not 0 <= line_no < len(r.lines):
            return ""
        what = explain(r.lines[line_no].text, self.flavor)
        where = self.describe_line(line_no, r)
        return what + ((" (%s.)" % where) if where else "")

    def export_to_parse_line(self, line_no):
        """The parse-form line export line `line_no` is, or None (a line only
        the export form has). For Find in simulation and the preview link."""
        ex, pa = self.render("export"), self.render("parse")
        if not 0 <= line_no < len(ex.lines):
            return None
        at = {rl.uid: n for n, rl in enumerate(pa.lines) if rl.uid}
        return at.get(ex.lines[line_no].uid)

    def parse_to_export_line(self, line_no):
        """The export line parse-form line `line_no` is, or None. For
        clicking a fiber in the preview."""
        ex, pa = self.render("export"), self.render("parse")
        if not 0 <= line_no < len(pa.lines):
            return None
        at = {rl.uid: n for n, rl in enumerate(ex.lines) if rl.uid}
        return at.get(pa.lines[line_no].uid)

    # -- flavor, link ------------------------------------------------------ #
    def set_flavor(self, name):
        """Change the document's flavor. The generated lines read the same in
        every flavor; the user's lines are re-spelled at render from the
        flavor they were written in, so setting the old flavor back returns
        the exact text. Take a `snapshot()` first for undo."""
        self.flavor = get_flavor(name).name if not isinstance(name, Flavor) \
            else name.name

    def detach(self):
        """Stop following the designer: the base is frozen as it is now."""
        self.linked = False

    def relink(self):
        self.linked = True

    # -- persistence ------------------------------------------------------- #
    def to_dict(self, with_base=None):
        """A JSON-able dict. The base text and line map are included for a
        file or detached document (or `with_base=True`); a linked document's
        base comes from the generator."""
        if with_base is None:
            with_base = self.source == "file" or not self.linked
        d = {"version": 1, "flavor": self.flavor,
             "options": self.options.to_dict(), "source": self.source,
             "linked": self.linked, "next_id": self._next_id,
             "changes": [c.to_dict() for c in self.changes],
             "pieces": [p.to_dict() for p in self.pieces]}
        fl = get_flavor(self.flavor)
        if fl.name == self.flavor and (not fl.builtin
                                       or flavor_is_edited(fl)):
            # The generator runs in a fresh process without the GUI's flavor
            # registry. Carry a custom flavor -- or a built-in the lab has
            # edited -- with the document it renders.
            d["flavor_definition"] = fl.to_dict()
        if with_base:
            d["base_text"] = self.base_text
            d["line_map"] = (None if self.line_map is None
                             else self.line_map.to_dict())
        return d

    @classmethod
    def from_dict(cls, d, base_text=None, line_map=None):
        if int(d.get("version", 0)) != 1:
            raise ValueError("unknown document version %r" % d.get("version"))
        definition = d.get("flavor_definition")
        name = d.get("flavor")
        if definition and (name not in FLAVORS or (
                name in SHIPPED_FLAVORS and not FLAVOR_STORE_LOADED)):
            fl = Flavor.from_dict(definition)
            if fl.name == name:
                register_flavor(fl)
        if base_text is None:
            base_text = d.get("base_text", "")
        if line_map is None and d.get("line_map"):
            line_map = LineMap.from_dict(d["line_map"])
        doc = cls(base_text, line_map, d.get("flavor", "klipper"),
                  OutputOptions.from_dict(d.get("options")),
                  d.get("source", "scaffold"), d.get("linked", True),
                  [change_from_dict(c) for c in d.get("changes", ())],
                  [Piece.from_dict(p) for p in d.get("pieces", ())])
        doc._next_id = int(d.get("next_id", 1))
        return doc

    def save(self, path, with_base=None):
        with open(path, "w") as fh:
            json.dump(self.to_dict(with_base), fh, indent=1)

    @classmethod
    def load(cls, path):
        with open(path) as fh:
            return cls.from_dict(json.load(fh))


def _mark(lst, cid):
    if cid not in lst:
        lst.append(cid)


@dataclass
class EditResult:
    """What `record_edit` did: chip ids `added`, `changed`, `removed`."""
    added: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    removed: list = field(default_factory=list)


@dataclass
class OutlineNode:
    label: str
    first: int
    last: int
    kind: str = ""
    children: list = field(default_factory=list)
    key: tuple = ()


def fill_placeholders(text, ctx):
    """`text` with every known [[name]] filled from `ctx`; unknown ones are
    left for `problems()` to report."""
    return _PLACEHOLDER.sub(
        lambda m: str(ctx[m.group(1)]) if m.group(1) in ctx else m.group(0),
        text)


def machine_placeholder_context(context, flavor="klipper"):
    """Add the selected controller's setpoint lines to the print values."""
    fl = get_flavor(flavor)
    ctx = {str(k): ("" if v is NOT_THIS_PRINT else
                    str(v).replace("\r", " ").replace("\n", " | "))
           for k, v in (context or {}).items()}
    ctx.setdefault("nozzle_height", ctx.get("gap", ""))
    for name, template, key in (("pressure_command", fl.pressure, "pressure"),
                                ("voltage_command", fl.hv_level, "voltage")):
        try:
            value = fmt_num(float(ctx.get(key, "")))
        except (ValueError, TypeError):
            value = ""
        ctx[name] = template.replace("{value}", value) if template and value else ""
    # The heat wait is ONE control, the printer's "Wait to print until target
    # temperature is reached" box, on every flavor: ticked (`heat_wait` in
    # the print values) writes the flavor's wait, unticked writes none. So
    # a MEWron still starts without a wait unless the box asks for one.
    ctx["temperature_command"] = (
        wait_temp_line(ctx.get("temperature"), fl).strip()
        if ctx.get("heat_wait", "").strip().lower() in ("1", "true", "yes")
        else "")
    return ctx


#: Heat commands any file may carry whatever its flavor: what a researcher
#: typed into a start block or an Insert (the Printer link's own test).
_ANY_HEAT = re.compile(r"(?:M104|M109|SET_HEATER_TEMPERATURE)\b", re.I)


def _template_word(template):
    """The command word a flavor template writes: ``SET_HV VALUE={value}``
    -> ``SET_HV``; "" for an empty template."""
    for line in str(template or "").splitlines():
        parts = line.strip().split()
        if parts and "{" not in parts[0]:
            return parts[0].upper()
    return ""


def process_setpoints(text, flavor="klipper"):
    """What the file `text` does about the three process settings a MEW
    print needs, read off its command lines (comments are skipped):
    ``{name: {"written": bool, "word": str}}`` for "voltage", "pressure"
    and "temperature", plus "hv_off". `word` is the command the flavor
    writes for it ("" = this flavor has none, which is every built-in for
    voltage and pressure: no firmware source defines them).

    Read off the TEXT, not the settings, so a command a researcher put in
    by hand (an Insert, their own start block line) counts too."""
    fl = get_flavor(flavor)
    words = {"voltage": [_template_word(fl.hv_level),
                         _template_word(fl.hv_on)],
             "pressure": [_template_word(fl.pressure)],
             "temperature": [_template_word(fl.heat_wait)],
             "hv_off": [_template_word(fl.hv_off)]}
    firsts = set()
    heat = False
    for raw in str(text or "").splitlines():
        body = strip_comment(raw, fl.comment).strip()
        if not body:
            continue
        body = re.sub(r"^[Nn]\d+\s+", "", body)
        first = body.split()[0].upper()
        firsts.add(first)
        if _ANY_HEAT.match(body):
            heat = True
    out = {}
    for name, ws in words.items():
        ws = [w for w in ws if w]
        written = any(w in firsts for w in ws)
        if name == "temperature":
            written = written or heat
        out[name] = {"written": written, "word": ws[0] if ws else ""}
    return out


def _join_and(items, last="and"):
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " %s " % last + items[-1]


def process_gap_text(setpoints, flavor="klipper", where="Setup's G-code card"):
    """Plain sentences for what `setpoints` (`process_setpoints`) says the
    file leaves out, with the place each is set; "" when it sets all three.
    `where` names the card that holds the start block and the heat-wait box,
    as the page saying this would send the researcher to it."""
    fl = get_flavor(flavor)
    missing = [n for n in ("voltage", "pressure", "temperature")
               if not setpoints.get(n, {}).get("written")]
    if not missing:
        return ""
    out = ["This file does not set the %s." % _join_and(missing, "or")]
    no_word = [n for n in missing if n != "temperature"
               and not setpoints[n]["word"]]
    has_word = [n for n in missing if n != "temperature"
                and setpoints[n]["word"]]
    if no_word:
        out.append(
            "%s has no %s command. Set %s on the printer before you start, "
            "or add your printer's own command%s under Edit flavors… (on %s)."
            % (fl.label, _join_and(no_word, "or"),
               "it" if len(no_word) == 1 else "them",
               "" if len(no_word) == 1 else "s", where))
    if has_word:
        tags = ["[[%s_command]]" % n for n in has_word]
        one = len(has_word) == 1
        out.append("%s has a %s command%s, but the start block does not use "
                   "%s: add %s to the start block on %s."
                   % (fl.label, _join_and(has_word), "" if one else "s",
                      "it" if one else "them", _join_and(tags), where))
    if "temperature" in missing:
        if fl.heat_wait:
            out.append("Tick “Wait to print until target temperature is "
                       "reached” on %s to write %s, or set the temperature "
                       "on the printer." % (where,
                                            fl.heat_wait.replace("{t}", "<°C>")))
        else:
            out.append("%s has no heat command: set the temperature on the "
                       "printer." % fl.label)
    return " ".join(out)


def start_block_waits(start_block):
    """True when the start block places the heat wait itself
    (``[[temperature_command]]``): the export then writes no wait line of
    its own above it, so a ticked box gives one wait, never two."""
    return "[[temperature_command]]" in str(start_block or "")


def render_machine_block(block, context, flavor="klipper"):
    """Expand a machine block (the header, the end). A setpoint command the
    flavor does not have prints no line; any other blank value keeps its
    line, and a comment says "(not set)" so a missing value is visible. A
    comment naming a value that does not describe this print
    (`NOT_THIS_PRINT`) is left out."""
    fl = get_flavor(flavor)
    ctx = machine_placeholder_context(context, fl)
    na = {str(k) for k, v in (context or {}).items() if v is NOT_THIS_PRINT}
    lines = []
    for line in str(block or "").splitlines():
        names = [m.group(1) for m in _PLACEHOLDER.finditer(line)]
        if any(name in OPTIONAL_PLACEHOLDERS and name in ctx
               and not str(ctx[name]).strip() for name in names):
            continue
        if line.lstrip().startswith(";") and any(n in na for n in names):
            continue
        shown = dict(ctx)
        if line.lstrip().startswith(";"):
            for name in names:
                if name in shown and not str(shown[name]).strip():
                    shown[name] = NOT_SET
        expanded = fill_placeholders(line, shown)
        if not expanded.strip():
            continue
        if expanded.lstrip().startswith(";") and fl.comment != ";":
            expanded = expanded.replace(";", fl.comment, 1)
        lines.append(expanded)
    return "".join(line + "\n" for line in lines)


def strip_export_comments(text, flavor="klipper"):
    """Strip controller comments while keeping commands and blank lines."""
    mark = get_flavor(flavor).comment
    out = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        clean = strip_comment(body, mark).rstrip()
        if get_flavor(flavor).line_numbers and re.fullmatch(r"\s*N\d+\s*", clean, re.I):
            continue
        if clean or not body.strip():
            out.append(clean + line[len(body):])
    if get_flavor(flavor).line_numbers:
        n = 0
        for i, line in enumerate(out):
            if re.match(r"\s*N\d+\s+", line, re.I):
                n += 1
                out[i] = re.sub(r"^(\s*)N\d+", r"\g<1>N%d" % n, line, count=1,
                                flags=re.I)
    return "".join(out)


def _rule_lines(c, fl):
    """A Rule's lines in flavor `fl`: its command written for `fl`, else its
    own text re-spelled from the flavor it was written in."""
    fl = get_flavor(fl)
    if c.command:
        fields = dict(c.command)
        kind = fields.pop("kind")
        try:
            return make_command(kind, fl, **fields)
        except CommandUnavailable:
            pass
    return convert_text(c.text, c.flavor or fl.name, fl.name).splitlines()


def _render_insert_lines(c, fl):
    """An Insert's lines in flavor `fl`."""
    fl = get_flavor(fl)
    if c.command:
        fields = dict(c.command)
        kind = fields.pop("kind")
        try:
            return make_command(kind, fl, **fields)
        except CommandUnavailable:
            pass                    # keep the text it was written as
    return [convert_line(ln, c.flavor or fl.name, fl.name) for ln in c.lines]


def edit_lines(c, fl):
    """The lines an added-lines edit (Insert or Rule) writes in flavor `fl`,
    placeholders unfilled -- what an edit's g-code box shows."""
    if isinstance(c, Insert):
        return list(_render_insert_lines(c, fl))
    if isinstance(c, Rule):
        return list(_rule_lines(c, fl))
    raise TypeError("only added lines have lines of their own")


def describe_key(doc, key):
    """Plain words for a line key: "scaffold 2, layer 3, pass 4"."""
    if not key:
        return ""
    pid, sec, g, L, p = key[0], key[1], key[2], key[3], key[4]
    if sec == "header":
        return "the file header"
    if sec == "exit":
        return "the end-of-print exit move"
    parts = [doc.group_name(pid, g)]
    if L >= 0:
        parts.append("layer %d" % (L + 1))
        parts.append("pass %d" % (p + 1) if p >= 0
                     else "the move into the layer")
    return ", ".join(parts)


def describe_at(doc, c):
    """Plain words for the line an `at` edit is on: "the travel joining
    two pieces", "line 3 of the Start block", "the move to G-code zero"."""
    at = tuple(c.at)
    head = at[0] if at else ""
    if head == "join":
        return "the travel joining two pieces"
    if head == "origin":
        return "the move to G-code zero"
    if at[:2] == ("opt", "wait_temp"):
        return "the heat-and-wait line"
    if at[:2] == ("opt", "custom_line"):
        return "the custom line"
    if head == "machine" and len(at) > 2:
        return "line %d of the %s block" % (
            int(at[2]) + 1, "Start" if at[1] == "start" else "End")
    if head == "flavor":
        return "the flavor's %s" % ("footer" if at[1:2] == ("footer",)
                                    else "first line")
    if head in ("rule", "ins", "rep") and len(at) > 1:
        try:
            other = doc.get(at[1])
            name = (other.label or chip_label(doc, other)).partition(
                " · ")[0]
        except KeyError:
            name = "an automatic script"
        if head == "rule" and len(at) > 2:
            return "%s, place %d" % (name, int(at[2]) + 1)
        return "a copy of %s" % name
    if c.key:
        return describe_key(doc, c.key)
    return "a line"


def delete_phrase(doc, c):
    """What a removal says: "Removed layer 3 of Scaffold 1", "Removed 12
    lines from layer 3 of Scaffold 1", "Removed a line from ...". `c` is an
    op "delete" `LineEdit`."""
    if c.at:
        return "Removed " + describe_at(doc, c)
    k = tuple(c.key)
    n = 1 + len(c.more or ())
    if len(k) < 5:
        return "Removed %d line%s" % (n, "" if n == 1 else "s")
    if k[1] in ("header", "exit"):
        part = describe_key(doc, k)
    else:
        part = doc.group_name(k[0], k[2])
        part = part[:1].upper() + part[1:]
        if k[3] >= 0:
            if c.unit == "layer":
                return "Removed layer %d of %s" % (k[3] + 1, part)
            part = "layer %d of %s" % (k[3] + 1, part)
    return "Removed %s from %s" % (
        "a line" if n == 1 else "%d lines" % n, part)


def anchor_label(doc, a):
    """Plain words for an anchor: "after layer 7 of scaffold 1"."""
    k = tuple(a.key)
    if a.unit == "start":
        return "at the start"
    if a.unit == "end":
        return "at the end"
    if a.unit == "header":
        return "after the header"
    if a.unit == "group":
        return "%s %s" % (a.side, doc.group_name(k[0], k[1]))
    if a.unit == "layer":
        return "%s layer %d of %s" % (a.side, k[2] + 1,
                                      doc.group_name(k[0], k[1]))
    if a.unit == "pass":
        return "%s pass %d of layer %d" % (a.side, k[3] + 1, k[2] + 1)
    if a.unit == "line":
        return "in %s" % describe_key(doc, k)
    return a.unit


def chip_label(doc, c):
    """What a chip says in Changes, e.g. ``G4 S2 · after layer 7``."""
    if isinstance(c, Insert):
        first = (_render_insert_lines(c, doc.flavor) or [""])[0]
        more = " (+%d)" % (len(c.lines) - 1) if len(c.lines) > 1 else ""
        return "%s%s · %s" % (first, more, anchor_label(doc, c.anchor))
    if isinstance(c, LineEdit):
        where = describe_at(doc, c) if c.at else describe_key(doc, c.key)
        if c.op == "delete":
            if c.more or c.unit:
                return delete_phrase(doc, c)
            return "%s: removed" % where
        if c.op == "replace":
            return "%s: -> %s" % (where, c.text)
        bits = ["%s -> %s" % (L, v) for L, v in c.set.items()]
        bits += ["%s removed" % L for L in c.drop]
        return "%s: %s" % (where, ", ".join(bits))
    if isinstance(c, Rule):
        name = c.name or (_rule_lines(c, doc.flavor) or [""])[0]
        where = _WHERE_LABEL.get(c.where, c.where).lower()
        if c.where in ("every_n_moves", "every_n_mm", "every_n_seconds"):
            where = where.replace(" n ", " %s " % fmt_num(c.n))
        elif c.where == "at_z":
            where = "when Z first reaches %s mm" % fmt_num(c.n)
        elif (c.every or 1) > 1:
            where = where.replace("each", "every %s" % _nth(c.every))
        return "%s · %s" % (name, where)
    if isinstance(c, Transform):
        label = _TRANSFORM_LABEL.get(c.op, c.op)
        p = c.params or {}
        if c.op == "offset":
            bits = ["%s %+g mm" % (L, float(p.get(k, 0)))
                    for L, k in (("X", "dx"), ("Y", "dy"), ("Z", "dz"))
                    if float(p.get(k, 0))]
            label = "Offset " + " ".join(bits)
            if float(p.get("rz") or 0.0) % 360.0:
                turn = "otate %+.4g°" % float(p["rz"])
                label = label + ", r" + turn if bits else "R" + turn
        elif c.op == "scale":
            parts = ["%s %g%%" % (L, float(p.get(k, 100)))
                     for L, k in (("X", "px"), ("Y", "py"), ("Z", "pz"))
                     if float(p.get(k, 100)) != 100.0]
            label = "Scale " + " ".join(parts) if parts \
                else "Scale (unchanged)"
        elif c.op == "speed_set":
            label = "Speed %g mm/s" % float(p.get("mm_s", 10))
        elif c.op == "speed_scale":
            label = "Speed at %g%%" % float(p.get("percent", 100))
        elif c.op == "layer_shift":
            bits = _layer_shift_bits(p, 1.0)
            prof = p.get("profile", "ramp")
            label = "Layer shift " + (", ".join(bits) + (
                " per layer" if prof == "ramp" else " per step (%s)" % prof)
                if bits else "(no step yet)")
        sc = c.scope
        if sc and sc.kind == "edit":
            if c.op == "repeat":
                label = "Repeat " + repeat_where_label(p)
            try:
                parent = doc.get(str(sc.first))
                on = (parent.label or chip_label(doc, parent)).partition(
                    " · ")[0]
            except KeyError:
                on = "an edit that is gone"
            return "%s · on %s" % (label, on)
        if sc and sc.kind == "lines":
            n = len(sc.first or ())
            return "%s · %d line%s" % (label, n, "" if n == 1 else "s")
        if sc and sc.kind == "set":
            n = len(sc.first or ())
            return "%s · %d selections" % (label, n)
        if sc and sc.kind != "all":
            lo = sc.first or 1
            hi = sc.last if sc.last is not None else lo
            noun = {"layers": "layers", "group": "scaffolds",
                    "piece": "pieces"}[sc.kind]
            label += " · %s %s" % (noun, lo if lo == hi else
                                   "%s-%s" % (lo, hi))
        return label
    return c.kind


_SHIFT_WORDS = (("dx", "X", "µm"), ("dy", "Y", "µm"), ("along", "along", "µm"),
                ("amp", "amplitude", "µm"), ("amp_pct", "amplitude", "%"),
                ("radius", "radius", "µm"))


def _layer_shift_bits(p, f):
    """A layer shift's non-zero fields, `f` steps of each, in words:
    ``["X +15 µm", "along -18 µm"]``. A length of 1000 µm or more reads in
    mm."""
    out = []
    for k, name, unit in _SHIFT_WORDS:
        try:
            v = float(p.get(k, 0) or 0) * f
        except (TypeError, ValueError):
            continue
        if abs(v) < 1e-12:
            continue
        if unit == "%":
            out.append("%s %+g%%" % (name, round(v, 4)))
        elif abs(v) >= 1000:
            out.append("%s %+g mm" % (name, round(v / 1000.0, 4)))
        else:
            out.append("%s %+g µm" % (name, round(v, 3)))
    return out


def layer_shift_summary(doc, c):
    """What layer shift `c` did in the last render, in a sentence or two
    for its form: how many layers, how far the last one moved, and why
    any part of it did nothing. "" before it has rendered."""
    st = doc.layer_shift_stats(c.id)
    if st is None:
        return ""
    p = c.params or {}
    if not _layer_shift_bits(p, 1.0):
        return "Set a step to move the layers."
    if st["refused"] == "rotary":
        return ("Nothing moves: the layer shift is for flat (3-axis) "
                "prints, and this file turns a rotary axis.")
    if not st["layers"]:
        return "Nothing moves: the selection holds no layer's moves."
    n = st["layers"]
    last = _layer_shift_bits(p, st["last_f"])
    out = "%d layer%s, counted from 0. The last moves %s." % (
        n, "" if n == 1 else "s", ", ".join(last) if last else "nothing")
    wavy = any(float(p.get(k, 0) or 0) for k in ("along", "amp", "amp_pct"))
    if wavy and not st["passes"]:
        out += (" Along and Amplitude found no passes to move -- they need "
                "a generated flat scaffold -- so only X, Y and Radius act.")
    elif wavy and not st["wavy_passes"]:
        out += (" Every pass is a straight line, so Along and Amplitude "
                "change nothing: they move a wavy pattern (the designer's "
                "Wave).")
    elif wavy and st["flat_layers"]:
        out += " %d layer%s had no pass to slide or grow." % (
            st["flat_layers"], "" if st["flat_layers"] == 1 else "s")
    return out


def repeat_where_label(p):
    """Where a Repeat on an edit writes it, in words: "after each layer",
    "every 50 moves", "2 more times right after it", plus its shift."""
    p = dict(EDIT_REPEAT_DEFAULTS, **(p or {}))
    w = p.get("where") or REPEAT_HERE
    if w == REPEAT_HERE:
        n = int(p.get("copies") or 0)
        where = "%d more time%s right after it" % (n, "" if n == 1 else "s")
    else:
        where = _WHERE_LABEL.get(w, w).lower()
        if w in ("every_n_moves", "every_n_mm", "every_n_seconds"):
            where = where.replace(" n ", " %s " % fmt_num(p.get("n") or 0))
        elif w == "at_z":
            where = "when Z first reaches %s mm" % fmt_num(p.get("n") or 0)
        elif int(p.get("every") or 1) > 1:
            where = where.replace("each", "every %s" % _nth(
                int(p.get("every"))))
        lo, hi = p.get("first"), p.get("last")
        if lo not in (None, "") or hi not in (None, ""):
            where += " (%s to %s)" % (lo if lo not in (None, "") else 1,
                                      hi if hi not in (None, "") else "end")
    bits = ["%s %+g" % (L, float(p.get(k) or 0))
            for L, k in (("X", "dx"), ("Y", "dy"), ("Z", "dz"))
            if float(p.get(k) or 0)]
    if bits:
        where += ", shifted " + " ".join(bits) + " mm each time"
    return where


def _nth(n):
    n = int(n)
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(
        n % 10, "th")
    return "%d%s" % (n, suf)


# =========================================================================== #
# 6. Pieces
# =========================================================================== #

_LINE_NUMBER = re.compile(r"^(\s*)[Nn]\d+\s*")
_CHECKSUM = re.compile(r"\*\d+\s*$")
#: one whole line of words written with no spaces, `G1X10Y0F600`: a letter,
#: then a number, at least twice. CAM tools and some controllers emit this.
_PACKED = re.compile(r"^[A-Za-z][-+]?[\d.]+(?:[A-Za-z][-+]?[\d.]+)+$")


def decode_gcode_bytes(data):
    """``(text, note)``: g-code bytes as text, honouring a byte-order mark.

    Windows Notepad saves "Unicode" as UTF-16, which read as UTF-8 is a NUL
    between every character -- no word parses and the file reads as empty.
    ``note`` is a plain sentence when the encoding was not plain UTF-8, else
    None. Anything undecodable is replaced rather than refused."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace"), "read as UTF-16 text"
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", errors="replace"), None
    # UTF-16 without a mark: every other byte of ASCII g-code is NUL
    if len(data) >= 8 and data[1:200:2].count(0) > 0.9 * len(data[1:200:2]):
        return data.decode("utf-16-le", errors="replace"), "read as UTF-16 text"
    return data.decode("utf-8", errors="replace"), None


def tidy_foreign_text(text):
    """``(text, notes)``: g-code from another tool made readable by every
    reader in this app, which splits a line's words on spaces and knows
    nothing of line numbers.

    A leading ``N123`` line number and a trailing ``*45`` checksum are
    dropped, a line whose words are packed with no spaces (``G1X10Y0``) has
    the spaces put in, an inch file is rewritten in mm and a radius-form arc
    is given its centre (`_inches_and_radius_arcs`). Relative moves are left
    to the readers, which handle them. ``notes`` says in plain words what was
    changed; empty means the text came back unchanged."""
    out, numbered, packed = [], 0, 0
    for raw in text.splitlines():
        body, sep, comment = raw.partition(";")
        new = _LINE_NUMBER.sub(r"\1", body)
        new = _CHECKSUM.sub("", new)
        if new != body:
            numbered += 1
        stripped = new.strip()
        if _PACKED.match(stripped):
            new = new.replace(stripped, re.sub(
                r"(?<=[\d.])(?=[A-Za-z])", " ", stripped), 1)
            packed += 1
        out.append(new + sep + comment if sep else new)
    notes = []
    if numbered:
        notes.append("line numbers and checksums removed from %d line(s)"
                     % numbered)
    if packed:
        notes.append("spaces put between the words of %d packed line(s)"
                     % packed)
    out, more = _inches_and_radius_arcs(out)
    notes += more
    if not notes:
        return text, []
    return "\n".join(out) + ("\n" if text.endswith(("\n", "\r")) else ""), notes


_LENGTH_WORDS = ("X", "Y", "Z", "I", "J", "K", "R", "E", "F")
#: every line `_inches_and_radius_arcs` can change carries one of these: a
#: G20 token (`G20`, `g020`) or an R word (`R5`, `R-.5`, as `_WORD` reads one)
_INCH_OR_RADIUS = re.compile(r"[Gg]0*20(?![0-9])|[Rr][-+]?\.?[0-9]")


def _inches_and_radius_arcs(lines):
    """``(lines, notes)``: an inch file (G20) rewritten in mm, and every
    radius-form arc (``G2 X10 Y10 R5``) given its centre as I/J offsets.

    Both are forms every reader here refuses or misreads, and both have one
    right answer: 25.4 mm to the inch for every length and feed word, and
    the centre the controller itself computes from R (grbl's formula: a
    positive R takes the short way round, a negative one the long way). The
    position is tracked through G90/G91, G92 and G28 for the arcs; an arc
    whose chord is longer than its diameter is left for the reader to refuse.
    Lines with neither are returned untouched.

    A text with no ``G20`` token and no ``R`` word anywhere has neither, so
    it comes back at once: the walk below took a second of a 120,000-line
    open, on the window's thread, to change nothing. The test is a superset
    (a comment saying ``R5`` still takes the walk), never a miss."""
    if _INCH_OR_RADIUS.search("\n".join(lines)) is None:
        return list(lines), []
    inch, absolute, motion = False, True, None
    pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    n_inch = n_arc = 0
    out = []
    for line in lines:
        toks = strip_comment(line, ";").split()
        gs = {t[1:].lstrip("0") or "0" for t in toks
              if re.fullmatch(r"[Gg]\d+", t)}
        words = {}
        for t in toks:
            m = _WORD.fullmatch(t)
            if m and m.group(1).upper() not in ("G", "M", "N"):
                try:
                    words[m.group(1).upper()] = float(m.group(2))
                except ValueError:
                    pass
        if "20" in gs:
            inch = True
            n_inch += 1
            line = re.sub(r"\b[Gg]0*20\b", "G21", line, count=1)
        elif "21" in gs:
            inch = False
        if "90" in gs:
            absolute = True
        if "91" in gs:
            absolute = False
        for g in ("0", "1", "2", "3"):
            if g in gs:
                motion = g
        is_move = bool(gs & {"0", "1", "2", "3"}) or (
            not gs and motion is not None
            and any(L in words for L in ("X", "Y", "Z")))
        if inch and (is_move or "92" in gs):
            scaled = {L: v * 25.4 for L, v in words.items()
                      if L in _LENGTH_WORDS}
            if scaled:
                line = _set_words(line, scaled)
                words.update(scaled)
        if "92" in gs:
            for L in pos:
                if L in words:
                    pos[L] = words[L]
            out.append(line)
            continue
        if "28" in gs:
            named = [L for L in pos if L in words]
            for L in (named or list(pos)):
                pos[L] = 0.0
            out.append(line)
            continue
        if not is_move:
            out.append(line)
            continue
        end = {L: ((words[L] if absolute else pos[L] + words[L])
                   if L in words else pos[L]) for L in pos}
        if motion in ("2", "3") and "R" in words and not (
                "I" in words or "J" in words):
            dx, dy = end["X"] - pos["X"], end["Y"] - pos["Y"]
            r = words["R"]
            q = 4.0 * r * r - dx * dx - dy * dy
            d = math.hypot(dx, dy)
            if d > 1e-12 and q >= -1e-9:
                h = -math.sqrt(max(q, 0.0)) / d
                if motion == "3":
                    h = -h
                if r < 0:
                    h = -h
                line = re.sub(r"\s+[Rr][-+]?[\d.]+", "", line, count=1)
                line = _set_words(line, {"I": 0.5 * (dx - dy * h),
                                         "J": 0.5 * (dy + dx * h)}, add=True)
                n_arc += 1
        pos = end
        out.append(line)
    notes = []
    if n_inch:
        notes.append("inches converted to mm")
    if n_arc:
        notes.append("%d radius-form arc(s) given their centre (I/J)" % n_arc)
    return out, notes


def read_foreign_gcode(path):
    """``(text, notes)``: a g-code file from anywhere, decoded and tidied
    (`decode_gcode_bytes`, `tidy_foreign_text`). Raises OSError as open()."""
    with open(path, "rb") as fh:
        text, enc = decode_gcode_bytes(fh.read())
    text, notes = tidy_foreign_text(text)
    return text, ([enc] if enc else []) + notes


def normalise_piece_text(text, flavor="klipper", strip_e=False):
    """A foreign file made ready to stitch: inches -> mm, relative ->
    absolute, line numbers and checksums dropped, optionally its slicer E
    words stripped. Comments are kept. Returns ``(text, notes)``, the notes
    saying in plain words what was changed."""
    fl = get_flavor(flavor)
    notes = []
    out = []
    scale = 1.0
    pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    rel = False
    for raw in text.splitlines():
        line = re.sub(r"^\s*[Nn]\d+\s+", "", raw)
        line = re.sub(r"\*\d+\s*$", "", line)
        cmd, words = parse_words(line, fl.comment)
        c = canon_cmd(cmd) if cmd else ""
        if c == "G20":
            scale = 25.4
            notes.append("inches converted to mm")
            out.append("G21")
            continue
        if c == "G21":
            scale = 1.0
        if c == "G91":
            rel = True
            notes.append("relative moves made absolute")
            continue
        if c == "G90":
            rel = False
        if c in _MOTION:
            vals = {}
            for L in ("X", "Y", "Z"):
                if L in words:
                    v = float(words[L]) * scale
                    pos[L] = pos[L] + v if rel else v
                    vals[L] = pos[L]
            for L in ("I", "J", "F"):
                if L in words and scale != 1.0:
                    vals[L] = float(words[L]) * scale
            line = _set_words(line, vals)
            if strip_e and "E" in words:
                line = re.sub(r"\s+[Ee][-+0-9.]+", "", line)
        out.append(line)
    if strip_e:
        notes.append("slicer E words stripped")
    seen = set()
    notes = [n for n in notes if not (n in seen or seen.add(n))]
    return "\n".join(out) + "\n", notes


def _piece_map(text, starts):
    """A piece's structure: one group, its layers at `starts` (0-based
    lines) when given and in range, else guessed from Z changes."""
    n = len(text.splitlines())
    starts = sorted({int(i) for i in starts or () if 0 <= int(i) < n})
    if not starts:
        return LineMap.guess(text)
    marks = [{"line": 0, "kind": "group", "group": 0}]
    marks += [{"line": i, "kind": "layer", "layer": k}
              for k, i in enumerate(starts)]
    return LineMap(marks, n)


def _piece_transform(slots, p):
    """Offset, rotate, mirror and scale a piece's generated lines in place,
    about the XY centre of its moves. Arcs: I/J turn with the piece and a
    mirror swaps G2/G3."""
    xs, ys = [], []
    w = _Walker()
    for s in slots:
        c, words, _d, _z = w.step(s.text)
        if c in _MOTION and w.x is not None and w.y is not None:
            xs.append(w.x)
            ys.append(w.y)
    if not xs:
        return
    cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
    th = math.radians(float(p.rotation or 0.0))
    ca, sa = math.cos(th), math.sin(th)
    k = float(p.scale or 1.0)
    mx = -1.0 if p.mirror == "x" else 1.0
    my = -1.0 if p.mirror == "y" else 1.0
    dx, dy, dz = (float(v) for v in p.offset)
    cur = {"X": None, "Y": None}

    def xf(x, y):
        x, y = (x - cx) * mx * k, (y - cy) * my * k
        return (cx + x * ca - y * sa + dx, cy + x * sa + y * ca + dy)

    def vec(i, j):
        i, j = i * mx * k, j * my * k
        return (i * ca - j * sa, i * sa + j * ca)
    for s in slots:
        cmd, words = parse_words(s.text)
        c = canon_cmd(cmd) if cmd else ""
        if c not in _MOTION:
            continue
        x = float(words["X"]) if "X" in words else cur["X"]
        y = float(words["Y"]) if "Y" in words else cur["Y"]
        cur["X"], cur["Y"] = x, y
        vals = {}
        if x is not None and y is not None and ("X" in words or "Y" in words):
            nx, ny = xf(x, y)
            vals["X"], vals["Y"] = nx, ny
        if "Z" in words and dz:
            vals["Z"] = float(words["Z"]) + dz
        if "I" in words or "J" in words:
            ni, nj = vec(float(words.get("I", 0)), float(words.get("J", 0)))
            vals["I"], vals["J"] = ni, nj
        t = s.text
        if c in ("G2", "G3") and (mx * my) < 0:
            t = re.sub(r"^(\s*)[Gg]0*([23])\b",
                       lambda m: m.group(1) + ("G3" if m.group(2) == "2"
                                               else "G2"), t)
        new = _set_words(t, vals)
        if vals and ("X" in vals and "X" not in words
                     or "Y" in vals and "Y" not in words):
            body = new.rstrip("\r\n")
            add = "".join(" %s%s" % (L, fmt_num(vals[L])) for L in ("X", "Y")
                          if L not in words)
            new = body + add + new[len(body):]
        if new != s.text:
            s.text = new
            s.origin, s.chip = "transform", p.id


def _text_transform(lines, c, fl, ending):
    """A file-wide spelling transform over rendered lines."""
    op = c.op
    out = []
    if op == "strip_comments":
        for rl in lines:
            t = strip_comment(rl.text, fl.comment).rstrip()
            if not t and rl.text.strip():
                continue
            out.append(_relined(rl, t + _end_of(rl.text, ending)))
        return out
    if op == "strip_line_numbers":
        return [_relined(rl, re.sub(r"^(\s*)[Nn]\d+\s+", r"\1", rl.text))
                for rl in lines]
    if op == "strip_e":
        return [_relined(rl, re.sub(r"\s+[Ee][-+]?[0-9.]+(?=\s|$)", "",
                                    rl.text.rstrip("\r\n"))
                         + _end_of(rl.text, ending)) for rl in lines]
    if op in ("to_relative", "to_absolute"):
        to_rel = op == "to_relative"
        pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
        rel_in = False
        mode_written = False
        for rl in lines:
            cmd, words = parse_words(rl.text, fl.comment)
            cc = canon_cmd(cmd) if cmd else ""
            if cc in ("G90", "G91"):
                rel_in = cc == "G91"
                if not mode_written:
                    out.append(_relined(rl, ("G91" if to_rel else "G90")
                                        + _end_of(rl.text, ending)))
                    mode_written = True
                continue
            if cc in _MOTION:
                if not mode_written:
                    out.append(RLine(("G91" if to_rel else "G90") + ending,
                                     "transform", c.id, (), True,
                                     ("mode", c.id)))
                    mode_written = True
                vals = {}
                for L in ("X", "Y", "Z"):
                    if L in words:
                        v = float(words[L])
                        new_abs = pos[L] + v if rel_in else v
                        vals[L] = (new_abs - pos[L]) if to_rel else new_abs
                        pos[L] = new_abs
                rl = _relined(rl, _set_words(rl.text, vals))
            out.append(rl)
        return out
    return list(lines)


def _relined(rl, text):
    return RLine(text, rl.origin, rl.chip, rl.key, rl.readonly, rl.uid)


def _end_of(text, ending):
    body = text.rstrip("\r\n")
    return text[len(body):] or ""


# =========================================================================== #
# 7. Understanding -- explain, problems, flavor detection and conversion
# =========================================================================== #

class CommandUnavailable(ValueError):
    """An Insert form the flavor has no command for; the message says so in
    plain words."""


#: Insert ▾ forms: (kind, menu label, fields), each field
#: (name, label, unit, default). `make_command` writes the line.
INSERT_FORMS = (
    ("dwell", "Dwell", (("seconds", "Time", "s", 2.0),)),
    ("wait_temp", "Wait for temperature",
     (("celsius", "Temperature", "°C", 90.0),)),
    ("set_speed", "Set speed", (("mm_s", "Speed", "mm/s", 10.0),)),
    ("set_pressure", "Set pressure", (("value", "Pressure", "", 0.0),)),
    ("hv_on", "High voltage on", (("value", "Voltage", "kV", 0.0),)),
    ("hv_off", "High voltage off", ()),
    ("hv_level", "Set high voltage", (("value", "Voltage", "kV", 0.0),)),
    ("message", "Message on printer screen", (("text", "Message", "", ""),)),
    ("line_to", "Line to (a printing move)",
     (("x", "X", "mm", 0.0), ("y", "Y", "mm", 0.0), ("z", "Z", "mm", None),
      ("mm_s", "Speed", "mm/s", None))),
    ("arc_cw", "Arc, clockwise",
     (("x", "End X", "mm", 0.0), ("y", "End Y", "mm", 0.0),
      ("i", "Centre X from start", "mm", 1.0),
      ("j", "Centre Y from start", "mm", 0.0),
      ("mm_s", "Speed", "mm/s", None))),
    ("arc_ccw", "Arc, counter-clockwise",
     (("x", "End X", "mm", 0.0), ("y", "End Y", "mm", 0.0),
      ("i", "Centre X from start", "mm", 1.0),
      ("j", "Centre Y from start", "mm", 0.0),
      ("mm_s", "Speed", "mm/s", None))),
    ("move_to", "Move to", (("x", "X", "mm", None), ("y", "Y", "mm", None),
                            ("z", "Z", "mm", None),
                            ("mm_s", "Speed", "mm/s", None))),
    ("pause", "Pause for operator", ()),
    ("comment", "Comment", (("text", "Comment", "", ""),)),
)


def form_available(kind, flavor="klipper"):
    """``(True, "")`` or ``(False, why)`` -- whether the Insert form `kind`
    has a command in `flavor`, for greying the menu item out."""
    try:
        make_command(kind, flavor, **{f[0]: (f[3] if f[3] is not None else 0)
                                      for k, _l, fs in INSERT_FORMS
                                      if k == kind for f in fs})
    except CommandUnavailable as exc:
        return False, str(exc)
    return True, ""


def make_command(kind, flavor="klipper", **fields):
    """The line(s) an Insert form writes, in `flavor`. Raises
    `CommandUnavailable` (plain words) when the flavor has no such command."""
    fl = get_flavor(flavor)

    def num(name, default=0.0):
        v = fields.get(name, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            raise CommandUnavailable("%s must be a number." % name)
    if kind == "dwell":
        s = num("seconds")
        if not fl.dwell_unit:
            raise CommandUnavailable(
                "%s's dwell unit is not known, so no dwell is written. Type "
                "the line yourself." % fl.label)
        if s < 0:
            raise CommandUnavailable("A dwell cannot be negative.")
        return [dwell_line(s, fl)]
    if kind == "wait_temp":
        line = wait_temp_line(num("celsius"), fl)
        if not line:
            raise CommandUnavailable(
                "%s has no wait-for-temperature command." % fl.label
                if not fl.heat_wait else "The temperature must be above 0.")
        return [line.rstrip("\n")]
    if kind == "set_speed":
        mm_s = num("mm_s")
        if mm_s <= 0:
            raise CommandUnavailable("The speed must be above 0.")
        return ["G1 F%s" % fmt_num(mm_s * 60.0)]
    if kind in ("set_pressure", "hv_on", "hv_off", "hv_level"):
        tmpl = {"set_pressure": fl.pressure, "hv_on": fl.hv_on,
                "hv_off": fl.hv_off, "hv_level": fl.hv_level}[kind]
        if not tmpl:
            what = {"set_pressure": "pressure", "hv_on": "high-voltage-on",
                    "hv_off": "high-voltage-off",
                    "hv_level": "high-voltage level"}[kind]
            raise CommandUnavailable(
                "%s has no standard %s command. Set yours in your own flavor."
                % (fl.label, what))
        return [tmpl.replace("{value}", fmt_num(num("value")))]
    if kind == "message":
        if not fl.message:
            raise CommandUnavailable("%s has no message command." % fl.label)
        return [fl.message.replace("{msg}", str(fields.get("text", "")))]
    if kind in ("line_to", "arc_cw", "arc_ccw"):
        # a printing move: G1 to a point, or G2/G3 about a centre given as
        # I/J from the start (the form every flavor here reads; no R)
        words = []
        letters = ("x", "y", "z") if kind == "line_to" else ("x", "y", "i",
                                                             "j")
        for L in letters:
            v = fields.get(L)
            if v not in (None, ""):
                words.append("%s%s" % (L.upper(), fmt_num(num(L))))
        if kind != "line_to" and not any(
                fields.get(L) not in (None, "") and num(L) != 0
                for L in ("i", "j")):
            raise CommandUnavailable("An arc needs its centre: give Centre "
                                     "X or Y (from the start) other than 0.")
        if fields.get("mm_s") not in (None, ""):
            words.append("F%s" % fmt_num(num("mm_s") * 60.0))
        if not words:
            raise CommandUnavailable("Give at least one of X, Y or Z.")
        return [{"line_to": "G1 ", "arc_cw": "G2 ",
                 "arc_ccw": "G3 "}[kind] + " ".join(words)]
    if kind == "move_to":
        words = []
        for L in ("x", "y", "z"):
            v = fields.get(L)
            if v not in (None, ""):
                words.append("%s%s" % (L.upper(), fmt_num(num(L))))
        if fields.get("mm_s") not in (None, ""):
            words.append("F%s" % fmt_num(num("mm_s") * 60.0))
        if not words:
            raise CommandUnavailable("Give at least one of X, Y or Z.")
        return ["G0 " + " ".join(words)]
    if kind == "pause":
        if not fl.pause:
            raise CommandUnavailable("%s has no pause command." % fl.label)
        return [fl.pause]
    if kind == "comment":
        if not fl.comment:
            raise CommandUnavailable("%s has no comment character." % fl.label)
        return ["%s %s" % (fl.comment, fields.get("text", ""))]
    raise CommandUnavailable("Unknown insert %r." % (kind,))


def dwell_line(seconds, flavor="klipper"):
    """A dwell of `seconds` in `flavor`'s own word and unit."""
    fl = get_flavor(flavor)
    cmd = fl.dwell_cmd or "G4"
    if fl.dwell_unit == "ms":
        return "%s %s%s" % (cmd, fl.dwell_word, fmt_num(seconds * 1000.0, 3))
    return "%s %s%s" % (cmd, fl.dwell_word, fmt_num(seconds, 4))


def dwell_seconds(words, flavor="klipper"):
    """Seconds a G4 with `words` pauses in `flavor`, or None. P is
    milliseconds and S seconds in Klipper, Marlin and RepRapFirmware (S wins
    when both are given); GRBL's P is SECONDS."""
    fl = get_flavor(flavor)
    try:
        if fl.name == "grbl":
            return float(words["P"]) if "P" in words else None
        if "S" in words and fl.name != "klipper":
            return float(words["S"])
        if "P" in words:
            return float(words["P"]) / 1000.0
    except ValueError:
        return None
    return None


def convert_line(line, src, dst):
    """One user line re-spelled from flavor `src` to `dst`. Never moves the
    nozzle: only a dwell, the heat-and-wait, a message and a pause are
    re-spelled, each into `dst`'s own spelling, so a line `make_command`
    wrote converts there and back unchanged. A command `dst` has no
    spelling for is kept unchanged (`problems()` flags it). The document
    never converts twice: a chip keeps the flavor it was typed in, so a
    hand-typed spelling also comes back exactly."""
    fs, fd = get_flavor(src), get_flavor(dst)
    if fs.name == fd.name:
        return line
    body = line.rstrip("\r\n")
    ending = line[len(body):]
    cmd, words = parse_words(body, fs.comment)
    c = canon_cmd(cmd) if cmd else ""
    if c == "G4":
        # always the target's own spelling: keeping a line "already valid"
        # there (Duet reads G4 P as ms too) would break the way back
        s = dwell_seconds(words, fs)
        if s is None:
            return line
        return dwell_line(s, fd) + ending
    if fs.heat_wait and c == canon_cmd(fs.heat_wait.split()[0]) \
            and "S" in words:
        if not fd.heat_wait or fd.heat_wait == fs.heat_wait:
            return line
        return fd.heat_wait.replace("{t}", words["S"]) + ending
    if fs.message and cmd == fs.message.split()[0].upper():
        if not fd.message or fd.message.split()[0] == fs.message.split()[0]:
            return line
        msg = body.split(None, 1)[1] if len(body.split(None, 1)) > 1 else ""
        return fd.message.replace("{msg}", msg) + ending
    if fs.pause and body.strip().upper() == fs.pause.upper():
        if not fd.pause:
            return line
        return fd.pause + ending
    return line


def convert_text(text, src, dst):
    return "".join(convert_line(ln, src, dst)
                   for ln in text.splitlines(keepends=True))


#: Commands a flavor is known to lack, with the source. Only grounded facts;
#: a command not listed is not judged.
_LACKS = {
    "klipper": {"M0": "Klipper has no native M0 (use PAUSE)",
                "M1": "Klipper has no native M1 (use PAUSE)",
                "M226": "M226 is RepRapFirmware's pause",
                "M291": "M291 is RepRapFirmware's message box"},
    "grbl": {"M109": "GRBL has no temperature commands",
             "M104": "GRBL has no temperature commands",
             "M140": "GRBL has no temperature commands",
             "M190": "GRBL has no temperature commands",
             "M117": "GRBL has no message command",
             "PAUSE": "PAUSE is a Klipper macro",
             "M226": "M226 is RepRapFirmware's pause",
             "M291": "M291 is RepRapFirmware's message box"},
    "duet": {"PAUSE": "PAUSE is a Klipper macro"},
    "marlin": {"PAUSE": "PAUSE is a Klipper macro",
               "M291": "M291 is RepRapFirmware's message box"},
}


def detect_flavor(text):
    """The likeliest flavor of a file from its syntax; Klipper (the MEWron
    default) when nothing tells."""
    head = text[:200000]
    low = head.lower()
    if re.search(r"^\s*;\s*flavor\s*:\s*marlin", low, re.M):
        return "marlin"
    if re.search(r"^\s*;\s*flavor\s*:\s*reprap", low, re.M) or \
            "reprapfirmware" in low:
        return "duet"
    if re.search(r"^\s*(if|while|var|set|echo|abort)\b", head, re.M) or \
            re.search(r"^\s*(M98\s+P\"|M291\b|M260\.3\b|M568\b)", head,
                      re.M) or re.search(r"\{[^}]*\}", head):
        return "duet"
    # AeroBasic, before GRBL: its $AO[n] / $DO[n] I/O lines also open on '$'
    if re.search(r"^\s*(LINEAR|INCREMENTAL|ABSOLUTE)\b", head, re.M) or \
            re.search(r"^\s*\$[AD][IO]\[", head, re.M):
        return "aerotech"
    # a lab's own move word (its own flavor's, or a built-in it edited): its
    # moves only draw when read in that flavor
    for fl in FLAVORS.values():
        w = str(fl.move_word or "").strip()
        if w and w.upper() != "G1" and re.search(
                r"^\s*%s\b" % re.escape(w), head, re.M | re.I):
            return fl.name
    if re.search(r"^\s*\$[A-Za-z0-9]", head, re.M):
        return "grbl"
    if re.search(r"^\s*(PAUSE|RESUME|SET_[A-Z_]+|BED_MESH_[A-Z_]+)\b",
                 head, re.M):
        return "klipper"
    return "klipper"


_EXPLAIN_SIMPLE = {
    "G20": "Set units to inches. MEWracle works in mm; the preview converts.",
    "G21": "Set units to millimetres.",
    "G28": "Home the axes.",
    "G90": "Use absolute positions: every X, Y, Z is a place on the machine.",
    "G91": "Use relative positions: every X, Y, Z is a step from where the "
           "nozzle is.",
    "M82": "Extruder in absolute mode (a slicer setting; MEW ignores it).",
    "M83": "Extruder in relative mode (a slicer setting; MEW ignores it).",
    "M0": "Stop and wait for the operator.",
    "M1": "Stop and wait for the operator, if optional stops are on.",
    "M226": "Pause the print here, once the moves before it have finished.",
    "PAUSE": "Pause the print and wait for the operator to resume.",
    "RESUME": "Resume a paused print.",
    "M30": "End of program.",
    "M106": "Turn the part fan on.",
    "M107": "Turn the part fan off.",
}


def explain(line, flavor="klipper"):
    """Plain words for one line, in mm, mm/s and seconds."""
    fl = get_flavor(flavor)
    body = read_line(line.rstrip("\r\n"), fl)
    if not body.strip():
        return "An empty line."
    cmd, words = parse_words(body, fl.comment)
    if not cmd:
        return "A comment. The printer ignores it."
    c = canon_cmd(cmd)

    def mm(L):
        return "%s %s" % (L, fmt_num(float(words[L])))
    if c in _MOTION:
        axes = [mm(L) for L in ("X", "Y", "Z") if L in words]
        rot = ["%s to %s°" % (L, fmt_num(float(words[L])))
               for L in sorted(ROTARY_WORDS) if L in words]
        speed = ""
        if "F" in words:
            f = float(words["F"])
            speed = " at %s mm/s (%s mm/min)" % (fmt_num(f / 60.0, 2),
                                                 fmt_num(f))
        turn = (" and turn %s" % ", ".join(rot)) if rot else ""
        if not axes and not rot:
            return ("Set the speed to %s mm/s (%s mm/min) for the moves that "
                    "follow." % (fmt_num(float(words["F"]) / 60.0, 2),
                                 fmt_num(float(words["F"])))
                    if "F" in words else "A move with no target; it does "
                                         "nothing.")
        where = ", ".join(axes)
        if c == "G0":
            if "Z" in words and axes == [mm("Z")] and not rot:
                return ("Move the nozzle to Z %s mm%s. In continuous-flow MEW "
                        "the jet keeps flowing, so even this lays fiber."
                        % (fmt_num(float(words["Z"])), speed))
            return ("Travel to %s%s%s. In continuous-flow MEW the jet keeps "
                    "flowing, so this travel also lays a fiber."
                    % (where or "the same spot", turn, speed))
        if c == "G1":
            return ("Move in a straight line to %s%s%s. The jet lays fiber "
                    "along it." % (where or "the same spot", turn, speed))
        return ("Move along a %s arc to %s%s. The jet lays fiber along it."
                % ("clockwise" if c == "G2" else "counter-clockwise",
                   where, speed))
    if c == "G4":
        s = dwell_seconds(words, fl)
        if s is None:
            return "Pause (dwell), with no time given."
        return "Wait %s s (dwell). The jet keeps flowing." % fmt_num(s, 3)
    if c == "G92":
        return ("Tell the machine its current position is %s. Nothing moves."
                % ", ".join("%s %s" % (L, v) for L, v in words.items()))
    if c in ("M104", "M109", "M140", "M190") and "S" in words:
        what = {"M104": "Set the nozzle heater to %s °C",
                "M109": "Set the heater to %s °C and wait until it is there",
                "M140": "Set the bed heater to %s °C",
                "M190": "Set the bed heater to %s °C and wait until it is "
                        "there"}[c]
        return what % words["S"] + "."
    if c == "M117":
        msg = body.split(None, 1)[1] if len(body.split(None, 1)) > 1 else ""
        return "Show \"%s\" on the printer's screen." % msg.strip()
    if c == "M291":
        return "Show a message box on the printer (RepRapFirmware)."
    if c in _EXPLAIN_SIMPLE:
        return _EXPLAIN_SIMPLE[c]
    return ("MEWracle doesn't know this command. It goes to the printer "
            "unchanged and isn't drawn in the preview.")


@dataclass
class Problem:
    """One finding for the Problems bar. `line` is 0-based (export form) or
    None for the whole file; `fix` names a safe fix `apply_fix` knows, or ""
    when the fix is the user's to make."""
    line: object
    code: str
    message: str
    fix: str = ""


def _rotary_feeds(texts):
    """`(rotary, split)` for internal G-code lines: whether any move turns a
    rotary axis, and whether the text is SPLIT-FED (Aerotech, `scaffold_gcode`
    rotary_feed="split") -- a rotary text whose moves carry E, which is then
    the rotary axes' feedrate and not slicer extrusion. A combined text never
    has an E; the flavor's own `rotary_feed` does not say which a text is."""
    rotary = split = False
    for t in texts:
        if t[:1] not in "Gg":
            continue
        for w in t.split()[1:]:
            c = w[:1].upper()
            if c in ROTARY_WORDS and _WORD.fullmatch(w):
                rotary = True
            elif c == "E" and _WORD.fullmatch(w):
                split = True
        if rotary and split:
            break
    return rotary, rotary and split


def problems(doc, plate=None, limits=None, skipped_lines=(), cts_mm_s=None):
    """Plain-language findings for the Problems bar, over a `Document` (or
    a plain text, read as Klipper).

    plate          (xmin, xmax, ymin, ymax) mm, to flag moves off the plate
    limits         {"X": (lo, hi), ...} machine travel, mm
    skipped_lines  export lines the preview could not draw
    cts_mm_s       the calibrated critical translation speed, advisory only

    MEWron's zero-length G0 markers are never flagged."""
    if isinstance(doc, str):
        doc = Document.from_file(doc, "klipper")
    fl = get_flavor(doc.flavor)
    r = doc.render()
    # judged as G-code: a re-spelled line by the G-code it was rendered from
    # (`RLine.src`), still at its own line number
    r = Rendered(r.text, [
        RLine(o.src + o.text[len(o.text.rstrip("\r\n")):], o.origin, o.chip,
              o.key, o.readonly, o.uid) if o.src is not None else o
        for o in r.lines], r.unplaced, r.form)
    out = []
    lacks = _LACKS.get(fl.name, {})
    w = _Walker(fl.comment)
    feed_seen = False
    first_move_flagged = False
    rotary, split_feed = _rotary_feeds(o.text for o in r.lines)
    piece_ids = {rl.key[0] for rl in r.lines if rl.key}
    first_piece = r.lines[0].key[0] if r.lines and r.lines[0].key else None
    for n, rl in enumerate(r.lines):
        text = rl.text.rstrip("\r\n")
        cmd, words = parse_words(text, fl.comment)
        c = canon_cmd(cmd) if cmd else ""
        if fl.max_line and len(text) > fl.max_line:
            out.append(Problem(n, "line_too_long",
                               "Line %d is %d characters; %s reads at most %d."
                               % (n + 1, len(text), fl.label, fl.max_line)))
        if "[[" in text and _PLACEHOLDER.search(text):
            hit = _PLACEHOLDER.search(text)
            why = ("%s is only known to a rule, at the layer or move it "
                   "fires on; the header and end blocks are written once, "
                   "before any move." % hit.group(0)
                   if hit.group(1) in RULE_ONLY_PLACEHOLDERS else
                   "%s is not a placeholder MEWracle knows." % hit.group(0))
            out.append(Problem(n, "placeholder",
                               "Line %d has an unfilled placeholder. %s "
                               "Export is refused until it is filled or "
                               "removed." % (n + 1, why)))
        if not c:
            continue
        key = c if c in lacks else (cmd.upper() if cmd.upper() in lacks
                                    else None)
        if key:
            out.append(Problem(n, "flavor_lacks", "Line %d: %s. %s may not "
                               "accept it." % (n + 1, lacks[key], fl.label)))
        if c == "G4" and fl.name == "klipper" and "S" in words \
                and "P" not in words:
            out.append(Problem(n, "flavor_lacks",
                               "Line %d: Klipper's G4 takes P (milliseconds), "
                               "not S." % (n + 1), "dwell_respell"))
        if c == "G4" and "P" in words:
            try:
                p = float(words["P"])
            except ValueError:
                p = None
            if p is not None and fl.dwell_unit == "s" and fl.dwell_word == "P"\
                    and p >= 100:
                out.append(Problem(n, "dwell_unit",
                                   "Line %d: %s reads G4 P as seconds -- this "
                                   "pauses %s s." % (n + 1, fl.label,
                                                     fmt_num(p)),
                                   "dwell_ms_to_s"))
            elif p is not None and fl.dwell_unit == "ms" and 0 < p < 50 \
                    and rl.origin in ("insert", "edit", "base"):
                out.append(Problem(n, "dwell_unit",
                                   "Line %d: %s reads G4 P as milliseconds -- "
                                   "this pauses %s ms. Did you mean %s s?"
                                   % (n + 1, fl.label, fmt_num(p),
                                      fmt_num(p)), "dwell_s_to_ms"))
        if c == "G20":
            out.append(Problem(n, "inches", "Line %d switches to inches."
                               % (n + 1)))
        if c == "G91":
            out.append(Problem(n, "relative", "Line %d switches to relative "
                               "moves." % (n + 1), "to_absolute"))
        if c in ("G28", "G92") and rl.key and rl.key[0] != first_piece \
                and len(piece_ids) > 1:
            out.append(Problem(n, "homing_in_piece",
                               "Line %d: %s inside a stitched piece resets "
                               "the machine's position mid-print."
                               % (n + 1, c)))
        if c in _MOTION:
            if "E" in words and not split_feed:
                out.append(Problem(n, "e_words",
                                   "Line %d carries a slicer E word; MEW has "
                                   "no extruder axis." % (n + 1), "strip_e"))
            if "F" in words:
                feed_seen = True
            elif not feed_seen and not first_move_flagged:
                out.append(Problem(n, "no_feed",
                                   "Line %d moves before any speed (F) is set; "
                                   "the machine will use its own default."
                                   % (n + 1)))
                first_move_flagged = True
            if c in ("G2", "G3") and rotary:
                out.append(Problem(n, "arc_rotary",
                                   "Line %d is an arc in a rotary file; arcs "
                                   "are only meaningful on the flat plate."
                                   % (n + 1)))
            if cts_mm_s and "F" in words and not split_feed:
                try:
                    if float(words["F"]) / 60.0 < float(cts_mm_s):
                        out.append(Problem(n, "below_cts",
                                           "Line %d runs below the calibrated "
                                           "critical speed (advisory)."
                                           % (n + 1)))
                except ValueError:
                    pass
        _c, _w, _d, zero = w.step(text)
        if c in _MOTION and not zero:
            for L, (lo, hi) in (limits or {}).items():
                v = getattr(w, L.lower(), None)
                if v is not None and not (lo - 1e-6 <= v <= hi + 1e-6):
                    out.append(Problem(n, "limits",
                                       "Line %d goes past the machine's %s "
                                       "travel (%s mm)." % (n + 1, L,
                                                            fmt_num(v))))
            if plate and w.x is not None and w.y is not None:
                x0, x1, y0, y1 = plate
                if not (x0 - 1e-6 <= w.x <= x1 + 1e-6
                        and y0 - 1e-6 <= w.y <= y1 + 1e-6):
                    out.append(Problem(n, "off_plate",
                                       "Line %d moves off the build plate."
                                       % (n + 1)))
    for n in skipped_lines:
        out.append(Problem(n, "preview_skipped",
                           "Line %d isn't drawn in the preview." % (n + 1)))
    if r.unplaced:
        out.append(Problem(None, "unplaced",
                           "%d change(s) couldn't carry over to the new "
                           "design. They are kept in Changes."
                           % len(r.unplaced)))
    for c in doc.changes:
        if isinstance(c, Rule) and c.on and not c.text.strip():
            out.append(Problem(None, "empty_rule",
                               "The rule \"%s\" has no g-code yet."
                               % chip_label(doc, c)))
    if fl.name != "klipper" and doc.options.wait_temp is not None \
            and not fl.heat_wait:
        out.append(Problem(None, "flavor_lacks",
                           "%s has no wait-for-temperature command, so the "
                           "Wait option writes nothing." % fl.label))
    return out


def apply_fix(doc, problem):
    """Apply a Problem's safe fix to `doc` as a chip; returns the chip, or
    None when the problem has no fix. Fixes are offered, never automatic."""
    fx = problem.fix
    if fx in ("strip_e", "to_absolute"):
        return doc.add(Transform(op=fx))
    if fx in ("dwell_ms_to_s", "dwell_s_to_ms", "dwell_respell"):
        r = doc.render()
        rl = r.lines[problem.line]
        cmd, words = parse_words(rl.text)
        p = float(words.get("P", words.get("S", 0)))
        secs = {"dwell_ms_to_s": p / 1000.0, "dwell_s_to_ms": p,
                "dwell_respell": p}[fx]
        new = dwell_line(secs, doc.flavor)
        text = "".join(l.text if k != problem.line else
                       new + _end_of(l.text, "\n")
                       for k, l in enumerate(r.lines))
        doc.record_edit(text)
        return None
    return None


# =========================================================================== #
# 8. Recipes -- the user's library of rules
# =========================================================================== #

LIBRARY_VERSION = 1


@dataclass
class Recipe:
    """A SCRIPT: a named, reusable set of edits ("Feeder start: OCTO1 800"),
    rules and transforms ("Scale X 120%"). The class keeps its old name.

    `automatic` is the one switch that makes a script run on EVERY print the
    app writes, at the place its rules say -- "Automatic commands". It lives
    here, in My library, so a script has one definition and one switch, and
    nothing is copied (the library file's old key `use_on_new` is read and
    written too, so an older build still sees it).
    `builtin` marks a starter (read-only: duplicate it to change it)."""
    name: str
    rules: list = field(default_factory=list)
    automatic: bool = False
    builtin: bool = False
    description: str = ""

    @property
    def use_on_new(self):
        return self.automatic

    @use_on_new.setter
    def use_on_new(self, value):
        self.automatic = bool(value)

    def to_dict(self):
        return {"name": self.name, "automatic": self.automatic,
                "use_on_new": self.automatic,
                "description": self.description,
                "rules": [r.to_dict() if hasattr(r, "to_dict") else dict(r)
                          for r in self.rules]}

    @classmethod
    def from_dict(cls, d):
        return cls(d["name"], [change_from_dict(
            dict(r, kind=r.get("kind") if r.get("kind") == "transform"
                 else "rule")) for r in d.get("rules", ())],
                   bool(d.get("automatic", d.get("use_on_new", False))),
                   False, d.get("description", ""))


def _starter(name, desc, **rule):
    return Recipe(name, [Rule(name=name, flavor="klipper", **rule)],
                  builtin=True, description=desc)


#: Read-only starters, written in Klipper and re-spelled for the document's
#: flavor at render. "Pressure step" ships with no command: no built-in
#: flavor has a grounded pressure command, so the user fills theirs in.
STARTER_RECIPES = (
    _starter("Pause after each layer", "Wait 2 s at the end of every layer.",
             text="G4 P2000", where="after_layer"),
    _starter("Layer number on the printer screen",
             "Show the layer number as each layer starts.",
             text="M117 Layer [[layer]]", where="before_layer"),
    _starter("Lift and wait at the end",
             "Raise the nozzle 5 mm and wait 10 s once the print is done.",
             text="G91\nG0 Z5\nG90\nG4 P10000", where="end"),
    _starter("Pressure step every N layers",
             "Your dispenser's pressure command every 5th layer. Fill in the "
             "command -- it depends on your dispenser.",
             text="", where="before_layer", every=5),
    _starter("Re-prime before each scaffold",
             "Wait 3 s before each scaffold so the jet settles.",
             text="G4 P3000", where="before_group"),
)


def load_library(path):
    """The user's recipes from `path` (missing file = none), after the
    starters. Returns a list of `Recipe`."""
    user = []
    try:
        with open(path) as fh:
            d = json.load(fh)
        if int(d.get("version", 0)) == LIBRARY_VERSION:
            user = [Recipe.from_dict(r) for r in d.get("recipes", ())]
    except (OSError, ValueError):
        user = []
    return list(copy.deepcopy(STARTER_RECIPES)) + user


def save_library(path, recipes):
    """Write the user's recipes (starters are never written)."""
    with open(path, "w") as fh:
        json.dump({"version": LIBRARY_VERSION,
                   "recipes": [r.to_dict() for r in recipes
                               if not r.builtin]}, fh, indent=1)


def _rekey(key, old, new):
    key = tuple(key or ())
    return ((new,) + key[1:]) if key and key[0] == old else key


def _rekey_scope(sc, old, new):
    if sc is None:
        return
    if sc.kind == "lines" and isinstance(sc.first, (list, tuple)):
        sc.first = [_rekey(k, old, new) for k in sc.first]
    elif sc.kind == "set" and isinstance(sc.first, (list, tuple)):
        subs = []
        for d in sc.first:
            sub = Scope.from_dict(d) if isinstance(d, dict) else d
            _rekey_scope(sub, old, new)
            subs.append(sub.to_dict() if isinstance(d, dict) else sub)
        sc.first = subs


def adopt_file(design, file_doc, name=""):
    """`design` (a new designer document) takes the opened file `file_doc`
    as its FIRST piece, the design printing after it: how a scaffold is
    added to a file (New scaffold on an opened file). The file's own start
    and end lines are left out (`Piece.drop_frame`) -- the printer's Start
    and End blocks run once, around the whole print -- and every edit made
    on the file comes with it, moved onto the piece's lines. Returns
    ``(piece, notes)``, the notes saying in plain words what changed."""
    notes = []
    text, tidy = normalise_piece_text(file_doc.base_text, "klipper")
    same = text.splitlines() == file_doc.base_text.splitlines()
    if not same:
        notes += tidy
    design._next_id = max(design._next_id, file_doc._next_id)
    pid = design.new_id("p")
    piece = Piece(id=pid, source="file", name=name or "Opened file",
                  text=text if not same else file_doc.base_text,
                  flavor=file_doc.flavor, drop_frame=True)
    pieces = []
    for p in file_doc.pieces or [Piece(id="base", source="designer")]:
        if p.source == "designer":
            if not p.is_plain():
                piece.offset, piece.rotation = p.offset, p.rotation
                piece.mirror, piece.scale = p.mirror, p.scale
                piece.layers = p.layers
            piece.lift, piece.on = p.lift, p.on
            pieces.append(piece)
        else:
            pieces.append(copy.deepcopy(p))
    pieces.append(Piece(id="base", source="designer",
                        name="Scaffold designer"))
    design.pieces = pieces
    dropped = 0
    for c in file_doc.changes:
        c = copy.deepcopy(c)
        if isinstance(c, LineEdit):
            if not same and (c.key or c.at):
                dropped += 1
                continue
            c.key = _rekey(c.key, "base", pid)
            c.more = [_rekey(k, "base", pid) for k in c.more or ()]
            if c.at and len(c.at) > 1 and c.at[0] == "s" \
                    and c.at[1] == "base":
                c.at = ("s", pid) + tuple(c.at[2:])
        elif isinstance(c, Insert):
            if not same and c.anchor.unit == "line":
                dropped += 1
                continue
            if c.anchor.unit in ("group", "layer", "pass", "line"):
                c.anchor.key = _rekey(c.anchor.key, "base", pid)
        elif isinstance(c, Transform):
            if not same and c.scope is not None and c.scope.kind == "lines":
                dropped += 1
                continue
            _rekey_scope(c.scope, "base", pid)
        design.changes.append(c)
    if dropped:
        notes.append("%d edit%s on single lines could not follow the file's "
                     "tidied lines" % (dropped, "" if dropped == 1 else "s"))
    return piece, notes


def recipe_from_chips(doc, name, chip_ids, use_on_new=False, automatic=None):
    """Save rules (inserted lines, as rules) and transforms from a document
    as a recipe. A changed line belongs to its file and is not kept."""
    if automatic is not None:
        use_on_new = automatic
    rules = []
    for cid in chip_ids:
        c = doc.get(cid)
        if isinstance(c, Insert):
            c = Rule(text="\n".join(_render_insert_lines(c, c.flavor)),
                     where="start" if c.anchor.unit == "start" else
                     "end" if c.anchor.unit == "end" else "after_layer",
                     flavor=c.flavor, command=copy.deepcopy(c.command),
                     what=c.what, label=c.label)
        if isinstance(c, (Rule, Transform)):
            r = copy.deepcopy(c)
            r.id, r.on, r.typed, r.saved = "", True, False, name
            rules.append(r)
    return Recipe(name, rules, automatic=bool(use_on_new))


def apply_recipe(doc, recipe):
    """Add a recipe's rules to `doc` as chips; returns their ids."""
    ids = []
    for r in recipe.rules:
        r = copy.deepcopy(r)
        r.id = ""
        if isinstance(r, Rule) and not r.name:
            r.name = recipe.name
        r.saved = recipe.name
        ids.append(doc.add(r).id)
    return ids


def new_document(base_text="", line_map=None, flavor="klipper", options=None,
                 library=(), source="scaffold"):
    """A fresh document with every automatic recipe of `library` added as
    visible chips. The app passes no library: its automatic scripts run
    through `OutputOptions.rig_recipes` at render and show as locked rows."""
    doc = Document(base_text, line_map, flavor, options, source=source)
    for rec in library:
        if rec.automatic:
            apply_recipe(doc, rec)
    return doc


# =========================================================================== #
# 2b. Built-in flavor profiles (data). Sources: the leader notes' primary-
#     source table; anything not in it is marked UNVERIFIED.
# =========================================================================== #

FLAVORS["klipper"] = Flavor(
    "klipper", "Klipper", dwell_word="P", dwell_unit="ms",
    heat_wait="M109 S{t}", message="M117 {msg}", pause="PAUSE",
    sources={"dwell": "klipper3d.org/G-Codes.html (G4 P<ms>)",
             "heat_wait": "klipper3d.org/G-Codes.html (M109 [T] S)",
             "message": "klipper3d.org/G-Codes.html (M117, display_status)",
             "pause": "klipper3d.org/G-Codes.html (PAUSE needs "
                      "[pause_resume])",
             "comment": "klippy/gcode.py _process_commands strips from "
                        "the first ';'"})
FLAVORS["duet"] = Flavor(
    "duet", "RepRapFirmware (Duet)", dwell_word="S", dwell_unit="s",
    heat_wait="M109 S{t}", message="M117 {msg}", pause="M226",
    max_line=255, multi_per_line=True,
    sources={"dwell": "RepRapFirmware GCodes.cpp DoDwell (G4 S s, P ms)",
             "heat_wait": "RepRapFirmware GCodes2.cpp (M109 deprecated but "
                          "handled)",
             "message": "RepRapFirmware GCodes2.cpp (M117 rest of line)",
             "pause": "RepRapFirmware GCodes2.cpp (M226 synchronous pause)",
             "max_line": "RepRapFirmware Configuration.h MaxGCodeLength 256 "
                         "incl. null"})
FLAVORS["marlin"] = Flavor(
    "marlin", "Marlin", dwell_word="P", dwell_unit="ms",
    heat_wait="M109 S{t}", message="M117 {msg}", pause="M0",
    sources={"dwell": "marlinfw.org/docs/gcode/G004.html",
             "heat_wait": "marlinfw.org/docs/gcode/M109.html (waits)",
             "message": "marlinfw.org/docs/gcode/M117.html (needs an LCD)",
             "pause": "marlinfw.org/docs/gcode/M000-M001.html (ignored "
                      "without an LCD or EMERGENCY_PARSER)"})
FLAVORS["grbl"] = Flavor(
    "grbl", "GRBL", dwell_word="P", dwell_unit="s", heat_wait="",
    message="", pause="M0",
    sources={"dwell": "gnea/grbl motion_control.c mc_dwell(float seconds)",
             "pause": "gnea/grbl gcode.c (M0)"})
# Aerotech AeroBasic (A3200 class). Seeded from a lab's own working program
# for its rig (a mandrel winding: HOME Z / HOME A / HOME X Y, INCREMENTAL,
# LINEAR X.. A.. F.. E.., DWELL, $AO/$DO process lines). Left EMPTY because no
# source settles them: the rapid word (a travel is written as LINEAR, which
# MEW draws anyway), the arc words' argument format (arcs go out as short
# LINEAR moves), DWELL's unit (no dwell form), the comment syntax (kept ";"),
# and every process command (the lab keeps its $AO/$DO lines as a header
# script). Units are a controller setting: nothing is written for them.
FLAVORS["aerotech"] = Flavor(
    "aerotech", "Aerotech (AeroBasic)", dwell_word="", dwell_unit="",
    dwell_cmd="DWELL", heat_wait="", message="", pause="",
    move_word="LINEAR", rapid_word="", arc_cw="", arc_ccw="",
    abs_word="ABSOLUTE", inc_word="INCREMENTAL",
    home=("HOME Z", "HOME A", "HOME X Y"), units_word="",
    rotary_feed="split", aliases=("RAPID=G0",),
    sources={"moves": "the lab's working A3200 program (LINEAR, INCREMENTAL, "
                      "HOME, DWELL); help.aerotech.com AeroScript Equivalents "
                      "of AeroBasic Commands (LINEAR, CW/CCW, DWELL, HOME are "
                      "AeroBasic commands)",
             "abs_word": "ABSOLUTE: the EPICS motorBase A3200 driver sends it "
                         "(third-party; pairs with INCREMENTAL)",
             "aliases": "help.aerotech.com AeroScript Equivalents of "
                        "AeroBasic Commands (RAPID: renamed MoveRapid(); "
                        "read only -- its axes are not coordinated, so a "
                        "travel is written LINEAR)",
             "rotary_feed": "Automation1 'G-Code Commands': a move with a "
                            "linear axis runs at F, E governs a rotary-only "
                            "move"})


#: The built-ins exactly as MEWracle ships them. A lab may edit a built-in
#: (`flavor_presets` keeps the edit on its computer); this copy is what
#: "Reset to default" brings back and what an edit is measured against.
SHIPPED_FLAVORS = {k: copy.deepcopy(v) for k, v in FLAVORS.items()}

#: Set by `flavor_presets.load`: this process holds the lab's flavor store, so
#: its registry is the authority and a document's carried definition of a
#: built-in never replaces it. A worker process (no store) takes the carried
#: definition, which is how a lab's edited built-in reaches Generate.
FLAVOR_STORE_LOADED = False


def flavor_is_edited(flavor):
    """True for a built-in a lab has changed from the shipped default."""
    fl = get_flavor(flavor)
    shipped = SHIPPED_FLAVORS.get(fl.name)
    return bool(fl.builtin and shipped is not None and fl != shipped)


def custom_flavor(base="klipper", name="custom", label="Custom", **changes):
    """"Duplicate and edit": a copy of a built-in profile with `changes`,
    not built-in. It is saved with the machine preset (`Flavor.to_dict`).
    `home` may be one string, lines split on ';' (the Machine page's row)."""
    d = get_flavor(base).to_dict()
    d.update(changes, name=name, label=label, builtin=False)
    for k in ("home", "known", "header", "footer", "aliases"):
        if isinstance(d.get(k), str):
            d[k] = _split_list(d[k])
    if d.get("rotary_feed") not in ("combined", "split"):
        d["rotary_feed"] = "combined"
    d["sources"] = {}
    return Flavor.from_dict(d)


#: Built-in labels older builds showed and SAVED (presets, sessions). The lab's
#: printer name was in Klipper's; Klipper is a firmware anyone runs. Every
#: label lookup maps these first, so an old save still finds its flavor.
LEGACY_FLAVOR_LABELS = {"Klipper (MEWron)": "Klipper"}


def flavor_by_label(label):
    """The profile whose `label` (what the Printer & collector page shows) is
    `label`, or Klipper."""
    label = LEGACY_FLAVOR_LABELS.get(label, label)
    for fl in FLAVORS.values():
        if fl.label == label:
            return fl
    return FLAVORS["klipper"]


def register_flavor(fl):
    """Make a custom profile available by name (a loaded machine preset)."""
    FLAVORS[fl.name.lower()] = fl
    return fl


#: The flavor editor's form: (field, label, example), one row per field.
#: `read_as_move` / `read_as_travel` are the two halves of `aliases`.
FLAVOR_FIELDS = (
    ("label", "Name", "My lab's Duet"),
    ("dwell_word", "Dwell word", "P or S"),
    ("dwell_unit", "Dwell unit", "ms or s"),
    ("heat_wait", "Heat and wait", "M109 S{t}"),
    ("message", "Message", "M117 {msg}"),
    ("pause", "Pause", "PAUSE"),
    ("pressure", "Set pressure", "M260.3 P2 S\"{value}\""),
    ("hv_on", "High voltage on", "SET_HV VALUE={value}"),
    ("hv_off", "High voltage off", "SET_HV VALUE=0"),
    ("hv_level", "Set high voltage", "SET_HV VALUE={value}"),
    ("comment", "Comment character", ";"),
    ("max_line", "Longest line (0 = no limit)", "255"),
    ("line_numbers", "Number the lines", "off"),
    # the move spelling: what your controller calls each G-code command.
    # The page reads your words back as G-code, so your edits still draw.
    ("move_word", "Linear move (G1)", "G1, LINEAR or MOVE"),
    ("rapid_word", "Travel move (G0)", "G0, or empty to travel with the "
                                       "linear move"),
    ("arc_cw", "Arc clockwise (G2)", "G2, or empty for short straight moves"),
    ("arc_ccw", "Arc counter-clockwise (G3)", "G3, or empty"),
    ("abs_word", "Absolute positions (G90)", "G90 or ABSOLUTE"),
    ("inc_word", "Incremental positions (G91)", "G91 or INCREMENTAL"),
    ("home", "Home line(s)", "G28, or HOME Z; HOME A; HOME X Y"),
    ("units_word", "Millimetre units (G21)", "G21, or empty when units are "
                                             "a controller setting"),
    ("dwell_cmd", "Dwell command (G4)", "G4 or DWELL"),
    ("rotary_feed", "Rotary feed", "combined (one F) or split (F linear, "
                                   "E rotary)"),
    ("read_as_move", "Also read as a printing move", "MOVEINC; G01"),
    ("read_as_travel", "Also read as a travel", "RAPID"),
    ("known", "Not moves (kept as written)", "$AO; SET_HV"),
)

#: The editor's sections: (title, intro, fields). Every FLAVOR_FIELDS name
#: but "label" is in exactly one.
FLAVOR_GROUPS = (
    ("Moves the preview reads",
     "What your controller calls each command that moves the nozzle. "
     "MEWracle writes these words and reads them back, so the preview "
     "draws what the machine will do.",
     ("move_word", "rapid_word", "arc_cw", "arc_ccw", "abs_word",
      "inc_word", "units_word", "home", "dwell_cmd", "dwell_word",
      "dwell_unit", "rotary_feed")),
    ("Words in files you open",
     "Other words your files use. A printing-move or travel word is read "
     "as that move but never written; a word that is not a move is kept as "
     "written and never asked about.",
     ("read_as_move", "read_as_travel", "known")),
    ("Process commands",
     "What Insert writes for these. Empty means your machine has none and "
     "Insert greys it out. Your own commands can also be saved as scripts "
     "in My library.",
     ("heat_wait", "message", "pause", "pressure", "hv_on", "hv_off",
      "hv_level")),
    ("File",
     "How a saved file is laid out.",
     ("comment", "max_line", "line_numbers")),
)

#: Whole-field tips the editor shows beside a row's example.
FLAVOR_TIPS = {
    "rapid_word": "Leave this empty when your controller's rapid move does "
                  "not travel in a straight line (Aerotech's RAPID moves "
                  "each axis at its own speed). The jet lays fiber through "
                  "a travel, so MEWracle then writes travels as the linear "
                  "move. Put the rapid word under 'Also read as a travel' "
                  "so files you open still read it.",
    "rotary_feed": "What F measures on a rotary move. combined: one F, the "
                   "speed along the surface. split: F is the linear rate "
                   "and E the rotary rate (Aerotech).",
    "known": "Commands your controller accepts that are not moves. They "
             "already go to the machine as written; listing one here only "
             "stops the G-code page asking whether it is a move.",
}


def _split_list(text):
    """A row's "a; b; c" as a list, empty items dropped."""
    return [t.strip() for t in str(text).split(";") if t.strip()]


def flavor_form_values(flavor):
    """``{field: text}`` -- every FLAVOR_FIELDS row as the editor shows it:
    on/off, lists joined with "; "."""
    fl = get_flavor(flavor)
    amap = alias_map(fl)
    out = {}
    for name, _l, _e in FLAVOR_FIELDS:
        if name == "read_as_move":
            v = [w for w, c in amap.items() if c == "G1"]
        elif name == "read_as_travel":
            v = [w for w, c in amap.items() if c == "G0"]
        else:
            v = getattr(fl, name, "")
        if isinstance(v, bool):
            v = "on" if v else "off"
        elif isinstance(v, (tuple, list)):
            v = "; ".join(str(x) for x in v)
        out[name] = str(v)
    return out


def flavor_from_form(values, base="klipper", name="custom", label=None):
    """The flavor the editor's rows describe: `base` with every row in
    `values` ({field: text}) applied, as a saved (not built-in) profile
    called `name`. The inverse of `flavor_form_values`."""
    ch = {}
    for f, v in values.items():
        v = "" if v is None else str(v)
        if f == "max_line":
            try:
                ch[f] = max(0, int(float(v.strip() or 0)))
            except ValueError:
                ch[f] = 0
        elif f == "line_numbers":
            ch[f] = v.strip().lower() in ("on", "yes", "true", "1")
        elif f in ("read_as_move", "read_as_travel"):
            continue
        elif f in ("home", "known"):
            ch[f] = _split_list(v)
        elif f in ("comment", "label"):
            ch[f] = v
        else:
            ch[f] = v.strip()
    if "read_as_move" in values or "read_as_travel" in values:
        al = ["%s=G1" % w for w in _split_list(values.get("read_as_move",
                                                           ""))]
        al += ["%s=G0" % w for w in _split_list(values.get("read_as_travel",
                                                            ""))]
        ch["aliases"] = al
    label = label if label is not None else ch.pop("label", None)
    ch.pop("label", None)
    return custom_flavor(base, name, label or get_flavor(base).label, **ch)


# =========================================================================== #
# 2c. Machine spelling -- the move table, both ways (see `Flavor`)
# =========================================================================== #

#: internal G-code command -> the Flavor field that spells it
_SPELL_FIELDS = (("G1", "move_word"), ("G0", "rapid_word"),
                 ("G2", "arc_cw"), ("G3", "arc_ccw"), ("G90", "abs_word"),
                 ("G91", "inc_word"), ("G21", "units_word"))
#: written into the parse form for the reader, never into an export: the
#: split-feed time base is a controller SETTING (the Machine page's unit)
_INTERNAL_ONLY = frozenset(("G75", "G76"))
#: the position words an incremental export writes as deltas
_POS_WORDS = "XYZ" + "".join(sorted(ROTARY_WORDS))
#: the least number of decimals an incremental delta is rounded to
_INC_DECIMALS = 4
_CMD = re.compile(r"^(\s*)(\S+)(.*)$", re.S)


def spelling(flavor):
    """``{internal command: machine word}`` for `flavor`; "" = it has none."""
    fl = get_flavor(flavor)
    d = {g: str(getattr(fl, f) or "").strip() for g, f in _SPELL_FIELDS}
    home = fl.home[0].split() if fl.home and fl.home[0].split() else []
    d["G28"] = home[0] if home else ""
    d["G4"] = str(fl.dwell_cmd or "").strip()
    return d


def spells_moves(flavor):
    """True when `flavor` spells a G-code command any other way, so the
    export must be translated."""
    return any(w != c for c, w in spelling(flavor).items())


def _respell(line, word):
    m = _CMD.match(line)
    return m.group(1) + word + m.group(3) if m else line


def _decimals(s):
    s = str(s)
    return len(s.split(".", 1)[1]) if "." in s else 0


def _arc_chords(line, start, comment=";"):
    """A G2/G3 from `start` (x, y) as G1 lines, about 5 degrees each. The
    feed rides the first, every other non-plane word the last. An R-form
    arc, or one it cannot read, comes back unchanged."""
    cmd, words = parse_words(line, comment)
    if "R" in words:
        return [line]
    try:
        x0, y0 = float(start[0]), float(start[1])
        x1 = float(words.get("X", x0))
        y1 = float(words.get("Y", y0))
        cx = x0 + float(words.get("I", 0.0))
        cy = y0 + float(words.get("J", 0.0))
    except (TypeError, ValueError):
        return [line]
    r = math.hypot(x0 - cx, y0 - cy)
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    cw = canon_cmd(cmd) == "G2"
    sweep = a1 - a0
    if cw and sweep >= -1e-12:
        sweep -= 2.0 * math.pi
    elif not cw and sweep <= 1e-12:
        sweep += 2.0 * math.pi
    n = max(1, int(math.ceil(abs(sweep) / math.radians(5.0))))
    extra = [L + words[L] for L in words if L not in "XYIJF"]
    out = []
    for k in range(1, n + 1):
        if k == n:
            px, py = x1, y1
        else:
            a = a0 + sweep * k / n
            px, py = cx + r * math.cos(a), cy + r * math.sin(a)
        toks = ["G1", "X" + fmt_num(px), "Y" + fmt_num(py)]
        if k == 1 and "F" in words:
            toks.append("F" + words["F"])
        if k == n:
            toks += extra
        out.append(" ".join(toks))
    return out


def to_machine_line(line, flavor, start=None):
    """One internal G-code line (no ending) in `flavor`'s spelling, as a list:
    [] when the machine has no such command and none is needed (G21, a mode
    word, the internal time base), several lines for an arc the machine
    cannot draw. Only the command word changes; the words after it are kept
    character for character, so `from_machine_line` reads it back exactly."""
    fl = get_flavor(flavor)
    cmd, words = parse_words(line, fl.comment)
    if not cmd:
        return [line]
    c = canon_cmd(cmd)
    if c in _INTERNAL_ONLY:
        return []
    sp = spelling(fl)
    if c not in sp:
        return [line]
    if c == "G4":
        if sp["G4"] in ("", "G4") or not fl.dwell_unit:
            return [line]
        s = dwell_seconds(words, "klipper")
        if s is None:
            return [line]
        v = s * 1000.0 if fl.dwell_unit == "ms" else s
        return [("%s %s%s" % (sp["G4"], fl.dwell_word or "",
                              fmt_num(v, 4))).rstrip()]
    word = sp[c]
    if word == c:
        return [line]
    if word:
        return [_respell(line, word)]
    if c == "G0" and sp["G1"]:
        return [_respell(line, sp["G1"])]
    if c in ("G2", "G3") and sp["G1"] and start is not None:
        return [_respell(p, sp["G1"])
                for p in _arc_chords(line, start, fl.comment)]
    if c in ("G21", "G90", "G91", "G28"):
        return []
    return [line]


def _z_only(line, comment=";"):
    """True for a move line whose only position word is Z."""
    _cmd, words = parse_words(line, comment)
    return "Z" in words and not any(L in words for L in _POS_WORDS
                                    if L != "Z")


def from_machine_line(line, flavor):
    """The machine's spelling of one line read back as internal G-code: the
    inverse of `to_machine_line` for a move, a mode word and a home line.
    Anything else (a DWELL, a $AO line, a line it does not know) is kept
    exactly as typed.

    In a flavor with no travel word every travel goes out as the move word,
    so the way back cannot tell them apart -- except the one travel the
    readers depend on: the Z-only step between plies, which is how
    `parse_gcode` knows a new ply starts (`scaffold_gcode` writes every
    layer change as `G0 Z..`, and no generator writes a Z-only G1). A
    Z-only move word reads back as G0 there. Read as G1, a re-opened
    Aerotech tube is ONE fiber through all its plies."""
    fl = get_flavor(flavor)
    m = _CMD.match(line)
    if not m:
        return line
    tok = m.group(2).upper()
    sp = spelling(fl)
    for c, w in sp.items():
        if c != "G4" and w and w != c and tok == w.upper():
            if c == "G1" and not sp["G0"] and _z_only(line, fl.comment):
                c = "G0"
            return m.group(1) + c + m.group(3)
    return line


def alias_map(flavor):
    """``{WORD: internal command}`` from `flavor`'s read-only aliases."""
    out = {}
    for a in get_flavor(flavor).aliases:
        w, _s, c = str(a).partition("=")
        w, c = w.strip().upper(), canon_cmd(c.strip())
        if w and c in _MOTION:
            out[w] = c
    return out


def read_line(line, flavor):
    """One line as the readers take it: the machine's words
    (`from_machine_line`), then the flavor's read-only aliases."""
    fl = get_flavor(flavor)
    line = from_machine_line(line, fl)
    m = _CMD.match(line)
    if m and fl.aliases:
        c = alias_map(fl).get(m.group(2).upper())
        if c:
            return m.group(1) + c + m.group(3)
    return line


#: The commands that change what the preview, converter and player read
#: (`Flavor`'s docstring): only these are mapped. Every other line goes to
#: the machine as written.
MEANING_COMMANDS = ("G0", "G1", "G2", "G3", "G4", "G20", "G21", "G28",
                    "G90", "G91", "G92")

#: What each meaning command is, in the words a notice uses.
MEANING_NAMES = {"G0": "a travel move", "G1": "a printing move",
                 "G2": "a clockwise arc", "G3": "a counter-clockwise arc",
                 "G4": "a dwell", "G20": "inch units",
                 "G21": "millimetre units", "G28": "homing",
                 "G90": "absolute positions", "G91": "incremental positions",
                 "G92": "set position"}

_AXIS_WORD = re.compile(r"[%s][-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?$"
                        % ("XYZ" + "".join(sorted(ROTARY_WORDS))), re.I)
_GCODE_WORD = re.compile(r"[GMTgmt]\d")
_LINE_NO = re.compile(r"[Nn]\d+$")


def flavor_words(flavor):
    """Every first word `flavor` itself knows, upper case: its spelling, its
    home lines, its process commands, its aliases and its kept words."""
    fl = get_flavor(flavor)
    words = {w.upper() for w in spelling(fl).values() if w}
    for t in (fl.heat_wait, fl.message, fl.pause, fl.pressure, fl.hv_on,
              fl.hv_off, fl.hv_level) + tuple(fl.home) + tuple(fl.header) \
            + tuple(fl.footer):
        toks = str(t or "").split()
        if toks:
            words.add(toks[0].upper())
    words |= set(alias_map(fl))
    words |= {str(k).strip().upper() for k in fl.known if str(k).strip()}
    return words


def unread_moves(text, flavor):
    """``{WORD: [0-based line, ...]}`` -- lines that LOOK like moves the
    preview cannot read: the first word is no G/M/T code and no word
    `flavor` knows, and an axis word (X1.5, A90) follows it. A lab's `MOVE`
    under Klipper, Aerotech's `RAPID`: the converter skips them, so the
    fibre they lay is missing from every preview. A line with no axis word
    (`$AO[1].X = 2.25`, `SET_HV VALUE=5`) is not a move and is never
    listed; it goes to the machine as written."""
    fl = get_flavor(flavor)
    known = flavor_words(fl)
    out = {}
    for n, raw in enumerate(text.splitlines()):
        body = strip_comment(raw, fl.comment) if fl.comment else raw
        toks = body.split()
        if toks and _LINE_NO.match(toks[0]):
            toks = toks[1:]
        if len(toks) < 2:
            continue
        w = toks[0].upper()
        if _GCODE_WORD.match(w) or w in known:
            continue
        if any(_AXIS_WORD.match(t) for t in toks[1:]):
            out.setdefault(toks[0].upper(), []).append(n)
    return out


@dataclass
class Gap:
    """A meaning command `flavor` cannot spell, found by `export_gaps`:
    `cmd` (internal G-code), `lines` (0-based export lines; empty when the
    export drops them) and `message` (plain words)."""
    cmd: str
    lines: list
    message: str


def export_gaps(doc):
    """The meaning commands (`MEANING_COMMANDS`) in `doc`'s export that its
    flavor cannot write: each would reach the machine as G-code it may not
    read, or vanish and change the print. A list of `Gap`, one per command;
    empty when the export says what the document means. Nothing else is
    judged -- a command MEWracle does not read goes out as written."""
    fl = get_flavor(doc.flavor)
    sp = spelling(fl)
    respelled = spells_moves(fl)
    inc = bool(doc.options.incremental and fl.inc_word)
    internal = doc.render("internal").lines
    export = doc.render("export").lines
    at = {}
    for k, rl in enumerate(export):
        at.setdefault(rl.uid, k)
    uses_g91 = any(canon_cmd(parse_words(rl.text, fl.comment)[0] or "")
                   == "G91" for rl in internal)
    found = {}
    for rl in internal:
        cmd, _w = parse_words(rl.text, fl.comment)
        c = canon_cmd(cmd) if cmd else ""
        why = None
        if c == "G4":
            if sp["G4"] in ("", "G4"):
                why = None if sp["G4"] == "G4" else (
                    "%s has no dwell command, so it goes out as G4"
                    % fl.label)
            elif not fl.dwell_unit:
                why = ("%s's dwell unit isn't set, so it goes out as G4"
                       % fl.label)
        elif c == "G0" and not sp["G0"] and not sp["G1"]:
            why = "%s has no move word, so it goes out as G0" % fl.label
        elif c in ("G2", "G3") and not sp[c] and not sp["G1"]:
            why = "%s has no arc or move word, so it goes out as %s" % (
                fl.label, c)
        elif c in ("G90", "G91") and not inc and not sp[c] and (
                c == "G91" or uses_g91):
            why = ("%s has no word for %s, so the line is left out and the "
                   "moves after it are read the other way"
                   % (fl.label, MEANING_NAMES[c]))
        elif c == "G28" and not sp["G28"]:
            why = "%s has no home line, so the line is left out" % fl.label
        elif c in ("G20", "G92") and respelled:
            why = "%s's own words have no %s, so it goes out as %s" % (
                fl.label, MEANING_NAMES[c], c)
        if why is None:
            continue
        g = found.get(c)
        if g is None:
            g = found[c] = Gap(c, [], why)
        if rl.uid in at:
            g.lines.append(at[rl.uid])
    return list(found.values())


def to_internal_text(text, flavor):
    """An imported file in `flavor`'s spelling as internal G-code, for the
    parse form. A line whose translation would not come back exactly is
    kept as it was, so an untouched import exports byte-identical."""
    fl = get_flavor(flavor)
    if not spells_moves(fl):
        return text
    out = []
    for raw in text.splitlines(keepends=True):
        body = raw.rstrip("\r\n")
        t = from_machine_line(body, fl)
        if t != body and to_machine_line(t, fl) != [body]:
            t = body
        out.append(t + raw[len(body):])
    return "".join(out)


def _origin_block(fl, opts, rlines):
    """The start of an incremental export, internal spelling except the
    home lines: home, go to where the machine is at G-code zero, switch to
    incremental. The move carries the file's first F (and E) so it is not
    made at the controller's default rate."""
    rot = set(str(opts.rotary_axes or "").upper())
    lines = []
    for h in fl.home:
        toks = h.split()
        if len(toks) > 1:
            keep = [t for t in toks[1:]
                    if t.upper() not in ROTARY_WORDS or t.upper() in rot]
            if not keep:
                continue
            h = " ".join([toks[0]] + keep)
        if toks:
            lines.append(h)
    feed = {}
    for rl in rlines:
        cmd, words = parse_words(rl.text, fl.comment)
        if canon_cmd(cmd or "") in _MOTION and "F" in words:
            feed = {L: words[L] for L in "FE" if L in words}
            break
    move = " ".join(["G1"] + [L + fmt_num(v) for L, v in opts.origin]
                    + [L + feed[L] for L in "FE" if L in feed])
    if fl.abs_word:
        lines += ["G90", move, "G91"]
    else:
        lines += ["G91", move]
    return lines


def machine_render(rlines, flavor, opts=None, ending="\n"):
    """The export form's last step: `rlines` (internal G-code `RLine`s) in
    `flavor`'s spelling and, with `opts.incremental`, as deltas after the
    origin block. Each line keeps its identity (origin, chip, key, uid) and
    gains `src`, its internal text, so typing can be read back
    (`Document._inbound`). With a G-code flavor, absolute positions and no
    internal-only line it returns `rlines` itself -- byte-identical."""
    fl = get_flavor(flavor)
    inc = bool(opts is not None and opts.incremental and fl.inc_word)
    spell = spells_moves(fl)
    if not (spell or inc or any(
            canon_cmd(parse_command(rl.text, fl.comment) or "")
            in _INTERNAL_ONLY for rl in rlines)):
        return rlines
    out = []
    if inc:
        for k, t in enumerate(_origin_block(fl, opts, rlines)):
            for d in to_machine_line(t, fl):
                out.append(RLine(d + ending, "origin", "origin", (), True,
                                 ("origin", k)))
    pos, rel, emitted = {}, False, {}
    for rl in rlines:
        body = rl.text.rstrip("\r\n")
        end = rl.text[len(body):]
        before = dict(pos)
        cmd, words = parse_words(body, fl.comment)
        c = canon_cmd(cmd) if cmd else ""
        pieces = [body]
        if c in ("G90", "G91"):
            rel = c == "G91"
            if inc:
                pieces = []
        elif c == "G92":
            for L, v in words.items():
                if L in _POS_WORDS:
                    try:
                        pos[L] = float(v)
                        emitted[L] = float(v)
                    except ValueError:
                        pass
        elif c in _MOTION:
            if c in ("G2", "G3") and not spelling(fl)[c] and spell:
                pieces = _arc_chords(body, (pos.get("X", 0.0),
                                            pos.get("Y", 0.0)), fl.comment)
            conv = []
            for p in pieces:
                _pc, pw = parse_words(p, fl.comment)
                new, delta, prec = {}, {}, 0
                for L, v in pw.items():
                    if L not in _POS_WORDS:
                        continue
                    try:
                        fv = float(v)
                    except ValueError:
                        continue
                    new[L] = pos.get(L, 0.0) + fv if rel else fv
                    if inc and not rel:
                        # one precision for every delta: a word's own decimals
                        # are trimmed ("X0"), and rounding the PREVIOUS position
                        # to them turned X1.1806 -> X0 into X-1. `emitted` is
                        # what the machine has summed so far, so the error
                        # cannot accumulate.
                        d = max(_decimals(v), _INC_DECIMALS)
                        prec = max(prec, d)
                        step = round(round(new[L], d) - emitted.get(L, 0.0), d)
                        delta[L] = step
                        emitted[L] = round(emitted.get(L, 0.0) + step, d)
                    elif inc:
                        emitted[L] = emitted.get(L, 0.0) + fv
                if delta:
                    p = _set_words(p, delta, precision=max(prec, 1))
                pos.update(new)
                conv.append(p)
            pieces = conv
        disp = []
        start = (before.get("X", 0.0), before.get("Y", 0.0))
        for p in pieces:
            disp += to_machine_line(p, fl, start)
        for j, d in enumerate(disp):
            last = j == len(disp) - 1
            out.append(RLine(d + (end if last else ending), rl.origin,
                             rl.chip, rl.key if j == 0 else (),
                             rl.readonly or j > 0,
                             rl.uid + ((j,) if j else ()),
                             src=body if j == 0 else None,
                             pos=before if (inc and j == 0) else None))
    return out


def machine_text(text, flavor, opts=None):
    """`machine_render` for a plain text (the off-page Save path)."""
    lines = [RLine(t) for t in text.splitlines(keepends=True)]
    ending = "\r\n" if "\r\n" in text else "\n"
    out = machine_render(lines, flavor, opts, ending)
    return text if out is lines else "".join(o.text for o in out)


def _apply_shifts(out, marks, comment=";"):
    """Add each mark's shift {letter: delta} to the position words of every
    move from its index on: what an incremental edit means -- the line
    typed moved, so does everything after it, as the machine will run it."""
    marks = sorted(marks, key=lambda m: m[0])
    cum, k, rel = {}, 0, False
    res = []
    for i, rl in enumerate(out):
        while k < len(marks) and marks[k][0] <= i:
            for L, v in marks[k][1].items():
                cum[L] = cum.get(L, 0.0) + float(v)
            k += 1
        cmd, words = parse_words(rl.text, comment)
        c = canon_cmd(cmd or "")
        if c in ("G90", "G91"):
            rel = c == "G91"
        if c in _MOTION and not rel and any(abs(v) > 1e-12
                                            for v in cum.values()):
            vals = {}
            for L, v in cum.items():
                if L in words:
                    try:
                        vals[L] = float(words[L]) + v
                    except ValueError:
                        pass
            if vals:
                rl = RLine(_set_words(rl.text, vals), rl.origin, rl.chip,
                           rl.key, rl.readonly, rl.uid)
        res.append(rl)
    return res

