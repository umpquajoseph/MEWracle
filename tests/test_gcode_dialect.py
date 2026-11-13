"""Machine spelling and incremental origin -- the G-code page in a
controller's own language.

A flavor may spell moves its own way (Aerotech's `LINEAR`, a lab's `MOVE`).
The document stays G-code inside; `mew_gcode.machine_render` re-spells the
export form and `Document.record_edit` reads typing back. With the Machine
page's "Incremental coordinates" the export homes, moves to the machine's
position at G-code zero, switches to incremental and writes deltas.

COMPARATORS, none written by the code under test:

  1. **A lab's working program.** `AEROTECH_SAMPLE` is the head and tail of a
     real A3200 mandrel winding that ran (home, INCREMENTAL, the move to the
     lab's zero, LINEAR X.. A.. passes, the $AO/$DO process lines). Imported
     and exported untouched it must come back byte for byte; its moves must
     reach the parse form as G-code the converter reads.
  2. **Invariants**: the deltas of an incremental export summed from the
     origin give back every absolute position; a typed delta survives
     verbatim and moves every later move by its change (what the controller
     will do); a Klipper export with nothing to translate is the input object
     itself.
  3. **The converter**: `gcode_to_fibers.parse_gcode` on the parse form of a
     translated file draws the same fibres as on the plain G-code.

RED against the pre-change engine (HEAD copies of mew_gcode.py and
scaffold_gcode.py first on the path): the import, edit, incremental, custom
flavor, arc and fixed-feed cases fail -- the import kept `LINEAR` in the parse
form, so the converter drew nothing.
"""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import json
import os
import sys
import tempfile

import gcode_to_fibers as gf
import mew_gcode as g
import scaffold_gcode as sg

_PASS, _FAIL = [], []


def check(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       ("   " + detail) if detail else ""))


AEROTECH_SAMPLE = (
    "$AO[1].X = 2.250000\n$AO[3].X = 1\n$DO[4].X = 1\n\n"
    "HOME Z\nHOME A\nHOME X Y\n\nINCREMENTAL\n"
    "LINEAR X35 Y31 Z87.5 F4.5 E25\n\nDWELL 100\n\nDWELL 100\n\n"
    "LINEAR X20.0336 A1092.8571\nLINEAR X-20.0336 A1092.8572\n"
    "LINEAR X20.0336 A1092.8571\nLINEAR X-20.0336 A1092.8571\n"
    "LINEAR X-5.0000\n\n$AO[3].X = 0\n$AO[1].X = 0\n$DO[4].X = 0\n")

#: a designer-shaped absolute mandrel file (internal G-code, split feeds)
BASE = ("G21\nG90\nG75\nG0 X0 A0 Z0.5 F600\nG1 X10 A360 F4.5 E25\n"
        "G1 X0 A720\nG1 X10 A1080\n")
ORIGIN = (("X", 35.0), ("Y", 31.0), ("Z", 87.5))


def _parse_paths(text):
    fd, path = tempfile.mkstemp(suffix=".gcode")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    try:
        return gf.parse_gcode(path)
    finally:
        os.remove(path)


def _inc_doc():
    d = g.Document(BASE, flavor="aerotech")
    d.options = g.OutputOptions(incremental=True, origin=ORIGIN,
                                rotary_axes="A")
    return d


def _moves(text):
    """Absolute X/A targets of every G0/G1 in an internal text."""
    out = []
    for ln in text.splitlines():
        cmd, w = g.parse_words(ln)
        if g.canon_cmd(cmd or "") in ("G0", "G1"):
            out.append((float(w.get("X", "nan")), float(w.get("A", "nan"))))
    return out


def test_a_lab_program_round_trips():
    raw = AEROTECH_SAMPLE
    check("a $AO/LINEAR program reads as Aerotech",
          g.detect_flavor(raw) == "aerotech", g.detect_flavor(raw))
    check("...a GRBL $H file still reads as GRBL",
          g.detect_flavor("$H\nG1 X1 F100\n") == "grbl")
    doc = g.Document.from_file(raw)
    check("imported untouched, the export is byte-identical",
          doc.render().text == raw)
    parse = doc.parse_text()
    check("the parse form carries G-code moves, not LINEAR",
          "G1 X20.0336 A1092.8571" in parse and "LINEAR" not in parse
          and "G91" in parse and "G28 Z" in parse, parse[:200])
    check("...and leaves the process lines and DWELL as typed",
          "$AO[1].X = 2.250000" in parse and "DWELL 100" in parse)
    check("the converter draws the imported passes", len(_parse_paths(
        parse)) > 0)


def test_typing_in_the_machines_words():
    doc = g.Document.from_file(AEROTECH_SAMPLE)
    lines = doc.render().text.splitlines()
    k = lines.index("LINEAR X20.0336 A1092.8571")
    lines[k] = "LINEAR X21 A1092.8571"
    res = doc.record_edit("\n".join(lines) + "\n")
    check("a typed LINEAR line is recorded",
          len(doc.changes) == 1, str(res.added))
    check("...shows as typed", doc.render().text.splitlines()[k]
          == "LINEAR X21 A1092.8571")
    check("...and reaches the parse form as G1",
          "G1 X21 A1092.8571" in doc.parse_text())
    # a travel the export spells as LINEAR keeps its G0 when edited
    d = g.Document("G21\nG90\nG0 X1 A0\nG1 X2 A10\n", flavor="aerotech")
    ex = d.render().text.splitlines()
    j = ex.index("LINEAR X1 A0")
    ex[j] = "LINEAR X1.5 A0"
    d.record_edit("\n".join(ex) + "\n")
    check("an edited travel stays a G0 inside (the parser's travel)",
          "G0 X1.5 A0" in d.parse_text(), d.parse_text())


def test_klipper_is_untouched():
    text = "G21\nG90\nG0 X1 Y1 Z0.02 F600\nG1 X5 Y1\nG2 X6 Y2 I0 J1\n"
    rl = [g.RLine(t) for t in text.splitlines(keepends=True)]
    check("a Klipper export with nothing to translate is the same object",
          g.machine_render(rl, "klipper", g.OutputOptions()) is rl)
    check("...and the off-page Save path returns the text unchanged",
          g.apply_output_options(text, g.OutputOptions(), "klipper") == text)


def test_incremental_export():
    d = _inc_doc()
    ex = d.render().text.splitlines()
    head = ["HOME Z", "HOME A", "HOME X Y", "ABSOLUTE",
            "LINEAR X35 Y31 Z87.5 F600", "INCREMENTAL"]
    check("homes, goes to the machine's zero, switches to incremental",
          ex[:6] == head, str(ex[:6]))
    check("the internal time base and G21/G90 are not written",
          not any(t in ex for t in ("G75", "G21", "G90")), str(ex))
    pos = {"X": 0.0, "A": 0.0}
    got = []
    for ln in ex[6:]:
        cmd, w = g.parse_words(ln)
        if cmd == "LINEAR":
            for L in "XA":
                if L in w:
                    pos[L] += float(w[L])
            got.append((pos["X"], pos["A"]))
    want = _moves(BASE)
    check("the deltas summed from zero give back every absolute target",
          all(abs(a - b) < 1e-9 and abs(c - e) < 1e-9
              for (a, c), (b, e) in zip(got, want)) and len(got) == len(want),
          "%s vs %s" % (got, want))
    check("the off-page Save path writes the same text",
          g.apply_output_options(BASE, d.options, "aerotech")
          == d.render().text)
    # a 3-axis machine is not told to home an A it does not have
    d.options = g.OutputOptions(incremental=True, origin=ORIGIN)
    check("a 3-axis machine's home has no HOME A",
          "HOME A" not in d.render().text.splitlines())


def test_incremental_edit_moves_what_follows():
    d = _inc_doc()
    ex = d.render().text.splitlines()
    j = ex.index("LINEAR X-10 A360")
    ex[j] = "LINEAR X-8 A360"
    res = d.record_edit("\n".join(ex) + "\n")
    after = d.render().text.splitlines()
    check("the typed delta survives verbatim", after[j] == "LINEAR X-8 A360"
          and bool(res.added or res.changed), str(after[j:j + 2]))
    check("...the next delta is unchanged", after[j + 1] == "LINEAR X10 A360")
    want = [(0, 0), (10, 360), (2, 720), (12, 1080)]
    check("...and every later move shifts by the change, as the machine "
          "runs it", _moves(d.parse_text()) == want,
          str(_moves(d.parse_text())))
    # deleting a delta takes it out of everything after
    d = _inc_doc()
    ex = d.render().text.splitlines()
    j = ex.index("LINEAR X-10 A360")
    del ex[j]
    d.record_edit("\n".join(ex) + "\n")
    check("a deleted incremental move: the moves after keep their deltas",
          _moves(d.parse_text()) == [(0, 0), (10, 360), (20, 720)],
          str(_moves(d.parse_text())))
    # inserting one adds its delta to everything after
    d = _inc_doc()
    ex = d.render().text.splitlines()
    j = ex.index("LINEAR X-10 A360")
    ex[j:j] = ["LINEAR X1 A0"]
    d.record_edit("\n".join(ex) + "\n")
    check("an inserted incremental move becomes a position, and the rest "
          "follow it", _moves(d.parse_text()) == [
              (0, 0), (10, 360), (11, 360), (1, 720), (11, 1080)],
          str(_moves(d.parse_text())))
    check("a line of the move to G-code zero takes typing as an edit, "
          "kept as typed", _keeps_origin_edit())


def _keeps_origin_edit():
    d = _inc_doc()
    ex = d.render().text.splitlines()
    ex[4] = "LINEAR X36 Y31 Z87.5 F600"
    typed = "\n".join(ex) + "\n"
    d.record_edit(typed)
    d2 = g.Document.from_dict(json.loads(json.dumps(d.to_dict(with_base=True))))
    return (d.render().text == typed and d2.render().text == typed
            and len(d.changes) == 1 and d.changes[0].at[0] == "origin")


def test_a_labs_own_words():
    fl = g.register_flavor(g.custom_flavor(
        "klipper", "custom", "Custom", move_word="MOVE", rapid_word="",
        abs_word="ABS", inc_word="INC", units_word="", home="HOME_ALL"))
    try:
        d = g.Document("G21\nG90\nG1 X1 Y1 F600\nG1 X5 Y1\n", flavor=fl.name)
        ex = d.render().text
        check("a Custom flavor's move word is written",
              ex == "ABS\nMOVE X1 Y1 F600\nMOVE X5 Y1\n", repr(ex))
        lines = ex.splitlines()
        lines[2] = "MOVE X6 Y1"
        d.record_edit("\n".join(lines) + "\n")
        check("...an edit typed with it draws",
              "G1 X6 Y1" in d.parse_text(), d.parse_text())
        check("...and a file written with it is recognised",
              g.detect_flavor("MOVE X1 Y2\nMOVE X3 Y4\n") == "custom")
        check("home lines typed as one row split on ';'",
              g.custom_flavor("klipper", home="HOME Z; HOME X Y").home
              == ("HOME Z", "HOME X Y"))
    finally:
        g.FLAVORS.pop("custom", None)


def test_an_arc_the_machine_cannot_draw():
    d = g.Document("G21\nG90\nG1 X10 Y0 F600\nG2 X0 Y-10 I-10 J0\n",
                   flavor="aerotech")
    ex = [ln for ln in d.render().text.splitlines() if ln]
    chords = ex[2:]
    end = g.parse_words(chords[-1])[1]
    check("an arc goes out as short LINEAR moves",
          len(chords) > 5 and all(c.startswith("LINEAR ") for c in chords),
          str(chords[:3]))
    check("...ending exactly where the arc ends",
          end.get("X") == "0" and end.get("Y") == "-10", str(end))
    r = [((float(g.parse_words(c)[1]["X"])) ** 2
          + (float(g.parse_words(c)[1]["Y"])) ** 2) ** 0.5 for c in chords]
    check("...every chord end on the arc's circle",
          all(abs(x - 10.0) < 1e-3 for x in r), str(r[:3]))


def test_the_time_base_is_a_setting():
    d = g.Document("G21\nG90\nG0 X0 A0 F600\nG1 X1 A10 F4.5 E25\n",
                   flavor="aerotech")
    check("a split file's parse form tells the reader its time base",
          d.parse_text().startswith("G75"))
    d.feed_unit = "s"
    check("...per second when the Machine page says so",
          d.parse_text().startswith("G76"))
    check("...and the export writes none",
          "G75" not in d.render().text and "G76" not in d.render().text)


def test_a_combined_tube_gets_no_time_base():
    """The Aerotech FLAVOR is split-fed; a tube written COMBINED (one F, the
    surface speed) is not, and its parse form must not say G75. Read under
    G75 each move is timed by its X travel alone (0.90 s where the surface
    path takes 1.72 s on the 4-axis default), and the physics relay laid every
    wall ~12 um further round each circuit: walls that step instead of stack
    (2026-10-28). RED before: the header went in on the flavor alone."""
    import gcode_to_fibers as g2f
    import mew_frame
    import tempfile
    text = sg.generate_mandrel_gcode(3.0, 12.0, 58.5, 0.02181, 4, 800.0,
                                     eta=13, rm=2)
    d = g.Document(text, flavor="aerotech")
    pt = d.parse_text()
    check("a combined tube's parse form carries no G75/G76",
          "G75" not in pt and "G76" not in pt, pt[:40])
    with tempfile.NamedTemporaryFile("w", suffix=".gcode", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(pt)
        path = fh.name
    try:
        frame = mew_frame.CylinderFrame(1.5)
        _p, ta = g2f.parse_gcode(path, with_times=True, frame=frame)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        _p, tk = g2f.parse_gcode(path, with_times=True, frame=frame)
    finally:
        os.unlink(path)
    check("...so it is timed exactly as the Klipper text is",
          abs(float(ta[-1][-1]) - float(tk[-1][-1])) < 1e-9,
          "%r vs %r" % (float(ta[-1][-1]), float(tk[-1][-1])))
    split = sg.generate_mandrel_gcode(3.0, 12.0, 58.5, 0.02181, 4, 800.0,
                                      eta=13, rm=2, rotary_feed="split")
    split = "\n".join(ln for ln in split.splitlines()
                      if ln.strip() not in ("G75", "G76")) + "\n"
    check("...while a split one's still does",
          g.Document(split, flavor="aerotech").parse_text().startswith("G75"))


def test_fixed_feeds():
    design = sg.mandrel_design(1.5, 20.0, 3.24)
    text = sg.generate_mandrel_gcode(1.5, 3.24, 20.0, 0.02, 4, 600.0,
                                     design=design, rotary_feed="split",
                                     fixed_feed=(4.5, 25.0))
    fs, es = set(), set()
    for ln in text.splitlines():
        cmd, w = g.parse_words(ln)
        if cmd in ("G0", "G1"):        # F is modal from the approach G0
            fs.update([w["F"]] if "F" in w else [])
            es.update([w["E"]] if "E" in w else [])
    check("fixed feeds: every F and E is the one typed",
          fs == {"4.5"} and es == {"25"}, "%s %s" % (fs, es))
    try:
        sg.generate_mandrel_gcode(1.5, 3.24, 20.0, 0.02, 4, 600.0,
                                  design=design, fixed_feed=(4.5, 25.0))
        ok = False
    except ValueError:
        ok = True
    check("...refused on a combined-feed file", ok)
    auto = sg.generate_mandrel_gcode(1.5, 3.24, 20.0, 0.02, 4, 600.0,
                                     design=design, rotary_feed="split")
    check("...and auto still works the rates out from the speed",
          "E25 " not in auto + " ")


def test_scale_speed_scales_both_rates():
    d = g.Document("G21\nG90\nG1 X10 A360 F4 E20\nG1 X0 A720\n",
                   flavor="aerotech")
    d.add(g.Transform(op="speed_scale", params={"percent": 50.0}))
    p = d.parse_text()
    check("Scale speed on a split file scales F and E together",
          "F2 E10" in p, p)


def test_a_reopened_tube_keeps_its_plies():
    """The tube writes each ply change as `G0 Z..` (`scaffold_gcode`), and
    that G0 is how `parse_gcode` starts a new ply. Aerotech has no travel
    word, so the export writes it LINEAR; read back as G1, the re-opened
    file is ONE fiber through every ply (the preview then drapes it on
    itself). RED before: 4 plies came back as 1 fiber."""
    import mew_frame
    text = sg.generate_mandrel_gcode(3.0, 12.0, 58.5, 0.02181, 4, 800.0,
                                     eta=13, rm=2)
    ex = g.Document(text, flavor="aerotech").render().text
    back = g.Document.from_file(ex, "aerotech")
    frame = mew_frame.CylinderFrame(1.5)

    def fibres(t):
        fd, path = tempfile.mkstemp(suffix=".gcode")
        with os.fdopen(fd, "w") as fh:
            fh.write(t)
        try:
            return len(gf.parse_gcode(path, frame=frame))
        finally:
            os.remove(path)
    n0, n1 = fibres(text), fibres(back.parse_text())
    check("a re-opened Aerotech tube has as many plies as the design",
          n0 == n1 and n0 > 1, "%d vs %d" % (n0, n1))
    check("...and its export is still byte-identical",
          back.render().text == ex)
    check("a Z-only LINEAR typed in reads as a ply step",
          g.from_machine_line("LINEAR Z0.5", "aerotech") == "G0 Z0.5")
    check("...a LINEAR with X in it is still a printing move",
          g.from_machine_line("LINEAR X1 Z0.5", "aerotech") == "G1 X1 Z0.5")
    check("...and a G-code flavor's G1 Z is left alone",
          g.from_machine_line("G1 Z0.5", "klipper") == "G1 Z0.5")


def test_words_read_but_never_written():
    """A flavor's read-only aliases: the parse form reads them as their
    move, the text and the export keep the word. RED before: the converter
    skipped `RAPID`, so the travel's fibre was missing."""
    raw = ("INCREMENTAL\nLINEAR X1 A10 F4.5 E25\nRAPID X2 A0\n"
           "LINEAR X1 A10\n")
    d = g.Document.from_file(raw, "aerotech")
    check("an Aerotech RAPID is read as a travel",
          "G0 X2 A0" in d.parse_text(), d.parse_text())
    check("...and goes back out as written", d.render().text == raw)
    fl = g.custom_flavor("klipper", "labx", "Lab X",
                         aliases=["MOVEINC=G1", "junk=Q9"])
    check("an alias that names no move is ignored",
          g.alias_map(fl) == {"MOVEINC": "G1"}, str(g.alias_map(fl)))
    check("explain reads an alias as its move",
          g.explain("RAPID X2", "aerotech").startswith("Travel"))


def test_lines_that_look_like_moves():
    """`unread_moves`: a first word nobody knows, followed by an axis word.
    Process lines and macros (no axis word) are never listed."""
    text = ("G1 X1 Y1\nMOVE X2 Y2\nmove x3 y3\n$AO[1].X = 2.25\n"
            "SET_HV VALUE=5\nN10 MOVE X4\nPAUSE\n; MOVE X5 in a comment\n"
            "SET_KINEMATIC_POSITION X=10\n")
    got = g.unread_moves(text, "klipper")
    check("MOVE lines are found, any case, after a line number",
          got == {"MOVE": [1, 2, 5]}, str(got))
    fl = g.custom_flavor("klipper", "labk", "Lab K", known=["MOVE"])
    check("...and a kept word is never asked about",
          g.unread_moves(text, fl) == {}, str(g.unread_moves(text, fl)))
    check("Aerotech's own words are never listed",
          g.unread_moves(AEROTECH_SAMPLE + "RAPID X1\n", "aerotech") == {},
          str(g.unread_moves(AEROTECH_SAMPLE, "aerotech")))


def test_meaning_the_flavor_cannot_write():
    """`export_gaps`: only the commands the readers act on, and only when
    the export would lose or mis-spell them. A command MEWracle does not
    read is never a gap."""
    base = "G21\nG90\nG1 X1 Y1 F600\nG4 P500\nM109 S90\nG92 X0\n"
    gaps = {x.cmd: x for x in g.export_gaps(
        g.Document(base, flavor="aerotech"))}
    check("Aerotech: the dwell (no unit) and G92 are gaps, M109 is not",
          set(gaps) == {"G4", "G92"}, str(sorted(gaps)))
    ex = g.Document(base, flavor="aerotech").render().text.splitlines()
    check("...each gap points at its line in the export",
          ex[gaps["G4"].lines[0]] == "G4 P500"
          and ex[gaps["G92"].lines[0]] == "G92 X0", str(ex))
    check("Klipper writes all of it", g.export_gaps(
        g.Document(base, flavor="klipper")) == [])
    fl = g.register_flavor(g.custom_flavor("klipper", "noinc", "No inc",
                                           inc_word="", abs_word=""))
    try:
        gp = g.export_gaps(g.Document("G90\nG1 X1\nG91\nG1 X1\n",
                                      flavor=fl.name))
        check("a dropped G91 (and the G90 that pairs with it) is a gap",
              sorted(x.cmd for x in gp) == ["G90", "G91"]
              and all(not x.lines for x in gp), str(gp))
        check("...a lone G90 with nothing to pair with is not",
              g.export_gaps(g.Document("G90\nG1 X1\n", flavor=fl.name))
              == [])
    finally:
        g.FLAVORS.pop("noinc", None)


def test_the_editor_rows_round_trip():
    """`flavor_form_values` / `flavor_from_form`: what the editor shows
    rebuilds the same profile, for every built-in."""
    for name in ("klipper", "duet", "marlin", "grbl", "aerotech"):
        fl = g.get_flavor(name)
        back = g.flavor_from_form(g.flavor_form_values(fl), name, "copy",
                                  fl.label + " copy")
        same = all(getattr(back, f) == getattr(fl, f) for f in (
            "move_word", "rapid_word", "home", "dwell_unit", "dwell_word",
            "max_line", "line_numbers", "aliases", "known", "heat_wait"))
        check("%s survives the editor's rows" % name, same and
              not back.builtin and back.name == "copy")
    names = {f for _t, _i, fs in g.FLAVOR_GROUPS for f in fs}
    check("every editor row is in exactly one section",
          names == {f for f, _l, _e in g.FLAVOR_FIELDS} - {"label"}
          and sum(len(fs) for _t, _i, fs in g.FLAVOR_GROUPS) == len(names))
    check("a flavor saved by a newer version still loads",
          g.Flavor.from_dict(dict(g.get_flavor("duet").to_dict(),
                                  future_field=1)).label
          == g.get_flavor("duet").label)


def main():
    test_a_lab_program_round_trips()
    test_typing_in_the_machines_words()
    test_klipper_is_untouched()
    test_incremental_export()
    test_incremental_edit_moves_what_follows()
    test_a_labs_own_words()
    test_an_arc_the_machine_cannot_draw()
    test_the_time_base_is_a_setting()
    test_a_combined_tube_gets_no_time_base()
    test_fixed_feeds()
    test_scale_speed_scales_both_rates()
    test_a_reopened_tube_keeps_its_plies()
    test_words_read_but_never_written()
    test_lines_that_look_like_moves()
    test_meaning_the_flavor_cannot_write()
    test_the_editor_rows_round_trip()
    print("\n%d passed, %d failed" % (len(_PASS), len(_FAIL)))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
