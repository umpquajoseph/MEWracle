#!/usr/bin/env python3
"""test_mew_gcode -- the G-code document engine (`mew_gcode.py`).

Self-running, no pytest: prints OK/FAIL per check, exits non-zero on any FAIL.

COMPARATORS, by section:

  1. THE MOVE. `gui_scaffold`'s user-facing rewrite moved into the engine.
     The comparator is the PRE-MOVE code itself: `gui_scaffold.py` as it stood
     at `PRE_MOVE` (main, before the engine existed) is loaded from git into
     its own module, and its `_gcode_for_display_export` is run beside the
     wrapper on every fixture of `gcode_doc_fixtures.py` and
     `test_gui_preview.py`, across every combination of the options, byte for
     byte. Without git (a release tree) the section SKIPs and says so; the two
     suites above still pin the behaviour through the wrappers.

  2-15. THE ENGINE. Invariants needing no reference: the line map leaves the
     g-code byte-identical and its marks sit where the emitter turns a layer;
     the render IS the typed text; a redesign re-places edits by structure;
     a flavor conversion and its inverse give back the same text, and the
     motion words are identical in every flavor.

RED PROOFS (2026-10-23, each injection monkeypatched in, its test went RED;
with no injection every test was green):
  section 1: boundary lines dropped; the M109 line reformatted; the custom
     line inserted one line late.
  sections 2-15: a layer mark one line late; anchors recorded by line, not
     structure; a word patch that reorders the line; Duet dwell written in ms
     under S; [[n]] counting from 0; a transform's scope ignored;
     --gcode-doc writing the export form instead of the parse form.
  one edit model: `nudge` a no-op; typing over a rule's line
     refused as read-only; `_apply_transform` ignoring "scale"; `place`
     dropping the command; `move_to_line` returning before the move.
     Each went RED in its own check.
"""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import itertools
import os
import subprocess
import sys
import types

import mew_gcode as G
import scaffold_gcode as sg

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRE_MOVE = "ae783efb"          # main before mew_gcode existed

FAILURES = []


def check(cond, name, detail=""):
    print("  %-4s %s%s" % ("OK" if cond else "FAIL", name,
                           "" if cond else "  -- " + detail))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _fixtures():
    """(name, text, boundaries) -- every source text the two gating suites
    feed the rewrite, plus line-ending variants of them."""
    import gcode_doc_fixtures as tf
    out = []
    flat = tf._flat()
    out.append(("flat", flat, ()))
    out.append(("flat lifted", tf._flat(start_z=0.5), ()))
    out.append(("flat g2g3", tf._flat(g2g3=True), ()))
    for zs in ((0.0, 0.06), (0.0, 0.03, 0.06), (0.0, 0.0), (0.0, 0.5)):
        src, b = tf._scaffolds(*zs)
        out.append(("stack %r" % (zs,), src, b))
        out.append(("stack %r, wrong boundaries" % (zs,), src,
                    tuple((o + 1, z) for o, z in b)))
    out.append(("tube", sg.generate_mandrel_gcode(3, 4.7124, 45, 0.02, 4,
                                                  600), ()))
    try:
        out.append(("five-axis A/C", tf._five_axis(), ()))
        out.append(("five-axis U/V", tf._five_axis("U", "V"), ()))
    except Exception as exc:  # noqa: BLE001 -- reported, not hidden
        print("  NOTE five-axis fixtures unavailable: %r" % (exc,))
    out.append(("imported no Z", "G21\nG90\nM83\nG1 X1 Y1 F600\nG1 X2 Y2\n",
                ()))
    # test_gui_preview's sources
    out.append(("preview spelling",
                "G90\nM83\nG0 Z0.2\nG1 X1 Y0 F60\nG0 X2 Y2\nG2 X3 Y3 I0 J1\n",
                ()))
    out.append(("preview header", "G21\nG90\nG0 Z0.2\nG1 X1 Y0 F60\n", ()))
    more = []
    for name, text, b in out:
        more.append((name + " CRLF", text.replace("\n", "\r\n"), b))
        more.append((name + " no final newline", text.rstrip("\n"), b))
    return out + more


class _Var(object):
    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v

    def set(self, v):
        self.v = v


def _load_pre_move():
    """`gui_scaffold.py` at PRE_MOVE as a module, or None without git."""
    try:
        src = subprocess.run(
            ["git", "show", "%s:gui_scaffold.py" % PRE_MOVE], cwd=HERE,
            capture_output=True, timeout=60, check=True).stdout.decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        print("  SKIP no git history for %s (%r)" % (PRE_MOVE, exc))
        return None
    mod = types.ModuleType("gui_scaffold_pre_move")
    mod.__file__ = os.path.join(HERE, "gui_scaffold_pre_move.py")
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    return mod


def _fake(mixin, state, boundaries):
    """An object with just the attributes the export path reads."""
    class Fake(mixin):
        def _flat_z_boundaries(self):          # the GUI's own reading of the
            return list(boundaries)            # design; identical in both

        def _sw_emit_opts(self):
            return state["sw"]
    f = Fake.__new__(Fake)
    f.s_travel_as_g1 = _Var(state["g1"])
    f.s_gcode_convention = _Var("G0X" if state["pad"] else "GX")
    f.no_layer_z = _Var(state["dropz"])
    f.no_scaffold_z = _Var(state["per_scaffold"])
    f.s_wait_for_temp = _Var(state["wait"] is not None)
    f.sim_temperature = _Var(state["wait"] or "")
    f.custom_gcode = _Var(state["custom"][0])
    f.custom_gcode_after = _Var(state["custom"][1])
    f.s_collector = _Var(state["collector"])
    f._last_gcode_is_scaffold = state["scaffold"]
    f._last_gcode_is_calibration = state["calibration"]
    return f


# --------------------------------------------------------------------------- #
# 1. the move
# --------------------------------------------------------------------------- #
def the_moved_rewrite_is_byte_identical_to_the_pre_move_code():
    pre = _load_pre_move()
    if pre is None:
        return
    import gui_scaffold
    old_m, new_m = pre._ScaffoldMixin, gui_scaffold._ScaffoldMixin
    # Two crossed blocks instead of one full product (which ran 8 min): the
    # spelling/Z axes crossed with each other and with every source gate, and
    # the two header/insert axes crossed with each other and with two
    # spelling states. Every value of every axis meets every other axis it
    # interacts with in the code under test.
    # No "surface" collector: this release offers the plate and the mandrel
    # only, and a saved state naming the curved collector opens on the plate
    # (gui_scaffold._apply_collector_mode), so the page can never hand the
    # export "surface". A five-axis FILE is still declined by the rewrite
    # itself, which the five-axis fixtures check on every state.
    spell = dict(g1=(False, True), pad=(False, True), dropz=(False, True),
                 per_scaffold=(False, True),
                 collector=("plate", "mandrel"))
    extra = dict(wait=(None, "140", "92.5", "hot", "-3"),
                 custom=(("", "2"), ("OCTO1 -500", "0"), ("OCTO1 -500", "2"),
                         ("OCTO1 -500", "99"), ("OCTO1 -500", "x")))
    gates = ((True, False, (False, False, False)),
             (False, False, (False, False, False)),
             (True, True, (False, False, False)),
             (True, True, (True, True, True)))
    states = []
    for sc, cal, sw in gates:
        base = dict(scaffold=sc, calibration=cal, sw=sw)
        for combo in itertools.product(*spell.values()):
            states.append(dict(base, wait="140", custom=("OCTO1 -500", "2"),
                               **dict(zip(spell, combo))))
        for g1, pad in ((False, False), (True, True)):
            for combo in itertools.product(*extra.values()):
                states.append(dict(base, g1=g1, pad=pad, dropz=True,
                                   per_scaffold=False, collector="plate",
                                   **dict(zip(extra, combo))))
    n = mism = 0
    first_bad = ""
    for name, text, bnds in _fixtures():
        for state in states:
            old = _fake(old_m, state, bnds)
            new = _fake(new_m, state, bnds)
            n += 1
            a = old._gcode_for_display_export(text)
            b = new._gcode_for_display_export(text)
            ok = (a == b and old._gcode_export_active()
                  == new._gcode_export_active())
            if not ok:
                mism += 1
                if not first_bad:
                    first_bad = "%s %r" % (name, state)
    check(n > 5000, "control: the grid is large (%d cases)" % n)
    check(mism == 0, "export text and export-active agree with the pre-move "
          "code on every case", "%d mismatches, first: %s" % (mism, first_bad))
    # the three pure functions, directly, on every fixture
    bad = []
    for name, text, bnds in _fixtures():
        for g1, pad, dz in itertools.product((False, True), repeat=3):
            if (pre._ScaffoldMixin._rewrite_gcode_lines(
                    text, g1, pad, drop_layer_z=dz, boundaries=bnds)
                    != G.rewrite_gcode_lines(text, g1, pad, drop_layer_z=dz,
                                             boundaries=bnds)):
                bad.append((name, g1, pad, dz))
        if (pre._ScaffoldMixin._is_five_axis_gcode(text)
                != G.is_five_axis_gcode(text)):
            bad.append((name, "five-axis guard"))
        for ln in text.splitlines():
            if pre._ScaffoldMixin._split_z_word(ln) != G.split_z_word(ln):
                bad.append((name, "split_z_word", ln))
                break
    check(not bad, "rewrite, guard and Z split agree line for line",
          repr(bad[:3]))
    check(pre._ROTARY_WORDS == G.ROTARY_WORDS == gui_scaffold._ROTARY_WORDS,
          "the rotary alphabet is unchanged")


def a_document_with_no_chips_renders_the_export_text():
    """`Document.render()` with no chips == `apply_output_options` -- the
    page's text IS what Save writes."""
    import gcode_doc_fixtures as tf
    src, b = tf._scaffolds(0.0, 0.06)
    for opts in (G.OutputOptions(),
                 G.OutputOptions(True, True, True, b, "140",
                                 ("OCTO1 -500", 2))):
        doc = G.Document(src, options=opts)
        r = doc.render()
        check(r.text == G.apply_output_options(src, opts),
              "no chips: render == apply_output_options (%s)"
              % ("all on" if opts.active() else "all off"))
        check("".join(ln.text for ln in r.lines) == r.text,
              "the rendered lines join to the text")
    check(G.Document(src).render("parse").text == src,
          "no chips: the parse form is the base text")


# --------------------------------------------------------------------------- #
# helpers: a generated design with its line map
# --------------------------------------------------------------------------- #
_SC = dict(width=5, height=5, stepover=0.5, rotation=90.0,
           fiber_diameter=0.02, layers=4, speed=600, start_angle=0.0,
           arc_radius=None, arc_segments=8, start_x=0.0, start_y=0.0,
           start_z=0.0)


def _design(layers=4, stepover=0.5, n=2, stab=True, stacked=True, **kw):
    """(text, LineMap) for n scaffolds (+ a stabilization base), recorded by
    the generator exactly as `--line-map` records it."""
    specs = []
    for k in range(n):
        sp = dict(_SC, layers=layers, stepover=stepover, **kw)
        if stacked:
            sp["start_z"] = k * layers * sp["fiber_diameter"]
        else:
            sp["start_x"] = 10.0 * k
        specs.append(sp)
    stabd = (dict(width=6, height=6, stepover=1.0, fiber_diameter=0.02,
                  layers=2, arc_radius=None, start_x=-0.5, start_y=-0.5)
             if stab else None)
    with sg.record_line_map() as marks:
        text = sg.generate_multi_scaffold_gcode(specs, stabilization=stabd,
                                                plate_extent=(-50, 50,
                                                              -50, 50))
    groups = ([{"name": "stab", "layers": 2}] if stab else []) + [
        {"name": "scaffold%d" % (k + 1), "layers": layers} for k in range(n)]
    return text, G.LineMap(marks, len(text.splitlines()), groups,
                           sha1=G.text_sha1(text))


def _lines_of(doc, pred):
    r = doc.render()
    return [n for n, rl in enumerate(r.lines) if pred(rl)], r


def _layer_span(r, piece, group, layer):
    idx = [n for n, rl in enumerate(r.lines) if rl.key and rl.key[0] == piece
           and rl.key[1] == "body" and rl.key[2] == group
           and rl.key[3] == layer]
    return (idx[0], idx[-1]) if idx else None


# --------------------------------------------------------------------------- #
# 2. the generator's line map
# --------------------------------------------------------------------------- #
def the_line_map_changes_no_output_and_matches_the_structure():
    """Off by default the generator is byte-identical (comparator: the same
    call with no recording); the map's structure is checked against what
    the flat emitter guarantees (invariant): every layer has exactly one
    Z-bearing line, in its entry; every pass line is a G1/G2/G3; the exit is
    travel only."""
    for stab in (False, True):
        for kw in ({}, {"g2g3": True}, {"link_loops": "manual"}):
            text, lm = _design(stab=stab, **kw)
            specs = [dict(_SC, **kw), dict(_SC, start_z=0.08, **kw)]
            stabd = (dict(width=6, height=6, stepover=1.0,
                          fiber_diameter=0.02, layers=2, arc_radius=None,
                          start_x=-0.5, start_y=-0.5) if stab else None)
            plain = sg.generate_multi_scaffold_gcode(
                specs, stabilization=stabd, plate_extent=(-50, 50, -50, 50))
            check(plain == text, "recording leaves the g-code byte-identical"
                  " (stab=%s %r)" % (stab, kw))
            addrs = lm.addresses()
            lines = text.splitlines()
            check(len(addrs) == len(lines), "one address per line")
            bad = []
            per_layer = {}
            for ln, a in zip(lines, addrs):
                if a.section == "body" and a.layer >= 0 and G.split_z_word(ln):
                    per_layer.setdefault((a.group, a.layer), []).append(a)
                if a.pass_ >= 0 and not ln.startswith(("G1 ", "G2 ", "G3 ")):
                    bad.append(("pass line not a deposition move", ln))
                if a.section == "exit" and not ln.startswith("G0 "):
                    bad.append(("exit line not a travel", ln))
            want = (2 if stab else 0) + 8
            check(len(per_layer) == want and all(
                len(v) == 1 and v[0].pass_ == -1 for v in per_layer.values()),
                "each layer's one Z line sits in its entry (%d layers)" % want,
                "%d layers, %r" % (len(per_layer), list(per_layer.items())[:2]))
            check(not bad, "pass and exit lines are what they claim",
                  repr(bad[:2]))
            check(any(a.section == "exit" for a in addrs),
                  "the exit is mapped")


def the_line_map_flag_writes_beside_the_gcode_never_in_it():
    """`scaffold_gcode --line-map` through the real CLI: the -o file is
    byte-identical with and without it, and the map loads and matches."""
    import tempfile
    d = tempfile.mkdtemp(prefix="mewgcode_")
    a, b, m = (os.path.join(d, n) for n in ("a.gcode", "b.gcode", "m.json"))
    common = ["--width", "5", "--height", "5", "--stepover", "0.5",
              "--layers", "3", "--stabilization", "--no-exit-move"]
    with open(os.devnull, "w") as dn:
        so = sys.stdout
        sys.stdout = dn
        try:
            r1 = sg.main(common + ["-o", a])
            r2 = sg.main(common + ["-o", b, "--line-map", m])
        finally:
            sys.stdout = so
            sg._LINE_MARKS = None
    ta, tb = open(a).read(), open(b).read()
    check(r1 == 0 and r2 == 0, "both runs succeed")
    check(ta == tb, "the g-code is byte-identical with --line-map")
    lm = G.LineMap.load(m)
    check(lm.matches(tb), "the map names the file it describes (sha1)")
    check([g["name"] for g in lm.groups] == ["stab", "scaffold1"],
          "groups follow --scaffold-groups order", repr(lm.groups))
    check(not any(";" in ln for ln in tb.splitlines()),
          "no comment reached the g-code")


# --------------------------------------------------------------------------- #
# 3. rendering, round trips and anchored edits
# --------------------------------------------------------------------------- #
def every_fixture_renders_identically_through_the_document():
    """The document's full render path with no chips == apply_output_options
    on every fixture and option set; AUTO boundaries off the line map ==
    the ordinal boundaries the GUI computes for the same stack."""
    import gcode_doc_fixtures as tf
    bad = []
    for name, text, bnds in _fixtures():
        for opts in (G.OutputOptions(),
                     G.OutputOptions(True, False, True, bnds),
                     G.OutputOptions(False, True, True, bnds, "92.5",
                                     ("OCTO1 -500", 3)),
                     G.OutputOptions(True, True, False, (), "140",
                                     ("OCTO1 -500", 99))):
            want = G.apply_output_options(text, opts)
            for form_doc in (G.Document(text, options=opts),
                             G.Document.from_file(text)):
                form_doc.options = opts
                got = form_doc.render()
                if got.text != want or "".join(
                        l.text for l in got.lines) != got.text:
                    bad.append((name, opts.to_dict()))
    check(not bad, "document render == apply_output_options everywhere",
          "%d bad, first %r" % (len(bad), bad[:1]))
    for zs in ((0.0, 0.06), (0.0, 0.03, 0.06), (0.0, 0.5)):
        src, bnds = tf._scaffolds(*zs)
        with sg.record_line_map() as marks:
            again, _b = tf._scaffolds(*zs)
        lm = G.LineMap(marks, len(again.splitlines()),
                       [{"name": "scaffold%d" % (k + 1)}
                        for k in range(len(zs))], sha1=G.text_sha1(again))
        doc = G.Document(src, lm, options=G.OutputOptions(
            drop_layer_z=True, boundaries=G.AUTO))
        check(doc.render().text == G.apply_output_options(
            src, G.OutputOptions(drop_layer_z=True, boundaries=bnds)),
            "AUTO boundaries == the GUI's ordinals for stack %r" % (zs,),
            "%r vs %r" % (doc.boundaries_from_map(), bnds))


def typed_text_round_trips_through_chips():
    """WYSIWYG invariant: after `record_edit(T)`, the render IS T -- for an
    insert between layers, a word change, a deletion, a mid-pass insert and
    a replaced command -- and with every chip unticked the render is the
    plain export again (chips are reversible)."""
    text, lm = _design()
    opts = G.OutputOptions(True, False, False)
    doc = G.Document(text, lm, options=opts)
    plain = doc.render()
    lines = [l.text for l in plain.lines]
    span = _layer_span(plain, "base", 1, 1)       # scaffold 1, layer 2
    edits = []
    t = list(lines)
    t.insert(span[1] + 1, "G4 P2000\n")          # after layer 2
    edits.append(("insert after a layer", "".join(t)))
    t = list(lines)
    k = next(n for n in range(span[0], span[1]) if plain.lines[n].key[4] == 1)
    t[k] = t[k].rstrip("\n") + " F900\n"         # a word change
    edits.append(("word change", "".join(t)))
    t = list(lines)
    del t[k]
    edits.append(("delete a generated line", "".join(t)))
    t = list(lines)
    t.insert(k + 1, "M117 mid pass\n")
    edits.append(("insert mid-pass", "".join(t)))
    t = list(lines)
    t[k] = "M400\n"
    edits.append(("replace with another command", "".join(t)))
    t = list(lines)
    t.insert(0, "OCTO1 800\n")
    t.append("G4 P5000\n")
    edits.append(("insert at start and end", "".join(t)))
    for label, typed in edits:
        d = G.Document(text, lm, options=opts)
        res = d.record_edit(typed)
        check(d.render().text == typed, "round trip: %s" % label,
              "chips %r" % (res.added,))
        check(d.record_edit(d.render().text).added == [],
              "re-recording the render adds nothing (%s)" % label)
        for c in d.changes:
            d.toggle(c.id, False)
        check(d.render().text == plain.text,
              "all chips off -> the plain export (%s)" % label)
    d = G.Document(text, lm, options=opts)
    d.record_edit(edits[1][1])
    d.record_edit(plain.text)
    check(d.changes == [], "changing a word back removes its chip",
          repr([c.to_dict() for c in d.changes]))


def edits_carry_over_a_redesign_by_structure():
    """The spec's comparator, by construction: edits made on a 4-layer,
    0.5 mm design are re-placed on a 2-layer, 0.4 mm redesign at the same
    structural places; the one anchored to the vanished layer 3 is listed
    as couldn't-carry-over with its text kept. (A line typed after a
    scaffold's LAST layer anchors to the scaffold, so it survives.)"""
    text, lm = _design(layers=4, stepover=0.5)
    doc = G.Document(text, lm)
    r = doc.render()
    lines = [l.text for l in r.lines]
    s2 = _layer_span(r, "base", 1, 1)
    s4 = _layer_span(r, "base", 2, 2)            # scaffold 2, layer 3
    k = next(n for n in range(s2[0], s2[1] + 1) if r.lines[n].key[4] == 2)
    t = list(lines)
    t.insert(s4[1] + 1, "G4 P1000\n")            # after scaffold 2 layer 3
    t[k] = t[k].rstrip("\n") + " F900\n"         # scaffold 1 layer 2 pass 3
    t.insert(s2[1] + 1, "M117 after L2\n")       # after scaffold 1 layer 2
    t.insert(0, "OCTO1 800\n")
    doc.record_edit("".join(t))
    labels = sorted(G.chip_label(doc, c) for c in doc.changes)
    check(len(doc.changes) == 4, "four chips recorded", repr(labels))
    check(any("after layer 2 of scaffold 1" in s for s in labels),
          "the insert is anchored to the layer, not a line", repr(labels))
    new_text, new_lm = _design(layers=2, stepover=0.4)
    check(new_text != text, "control: the redesign changes the g-code")
    lost = doc.rebase(new_text, new_lm)
    r2 = doc.render()
    gone = [doc.get(i) for i in lost]
    check(len(gone) == 1 and isinstance(gone[0], G.Insert)
          and gone[0].lines == ["G4 P1000"],
          "the edit anchored to the vanished layer 3 is listed, text kept",
          repr([c.to_dict() for c in gone]))
    s2n = _layer_span(r2, "base", 1, 1)
    check(r2.lines[s2n[1] + 1].text == "M117 after L2\n",
          "the insert lands right after layer 2 of the new design")
    kn = [n for n, rl in enumerate(r2.lines)
          if rl.key == r.lines[k].key]
    check(len(kn) == 1 and r2.lines[kn[0]].text.rstrip().endswith("F900"),
          "the speed edit lands on the same pass of the new design")
    check(r2.lines[0].text == "OCTO1 800\n", "the start line stays first")
    check("G4 P1000" not in r2.text, "the lost edit is not exported")
    doc.place_insert(gone[0].id, s2n[0] - 1)
    check(doc.unplaced() == [] and "G4 P1000" in doc.render().text,
          "a lost edit is re-placed where it is dropped")


# --------------------------------------------------------------------------- #
# 4. rules, transforms, flavors, pieces
# --------------------------------------------------------------------------- #
def rules_fire_at_every_structural_place():
    text, lm = _design(n=1, stab=False, layers=4)
    doc = G.Document(text, lm)
    rule = doc.add(G.Rule(text="M117 L[[layer]] Z[[z]] #[[n]]",
                          where="after_layer", name="Layer note"))
    r = doc.render()
    hits = [n for n, rl in enumerate(r.lines) if rl.chip == rule.id]
    check(len(hits) == 4, "after each layer: four lines", repr(hits))
    ok = True
    for occ, n in enumerate(hits):
        prev = r.lines[n - 1]
        nxt = r.lines[n + 1] if n + 1 < len(r.lines) else None
        ok &= prev.key[3] == occ and (nxt is None or not nxt.key
                                      or nxt.key[3] != occ)
        ok &= r.lines[n].text.startswith("M117 L%d Z" % (occ + 1))
        ok &= r.lines[n].text.rstrip().endswith("#%d" % (occ + 1))
    check(ok, "each sits after its layer's last line, placeholders filled")
    check(all(r.lines[n].readonly for n in hits), "rule lines are read-only")
    typed = "".join(l.text for k, l in enumerate(r.lines) if k != hits[0])
    doc.record_edit(typed)
    now = doc.render()
    check(now.text == typed
          and sum(1 for rl in now.lines if rl.chip == rule.id) == 3
          and rule.text == "M117 L[[layer]] Z[[z]] #[[n]]",
          "deleting one place of a rule's line keeps the deletion there, "
          "the rule writing its other three")
    doc.remove(next(c.id for c in doc.changes
                    if isinstance(c, G.LineEdit) and c.at))
    rule.every, rule.text = 2, "G4 P500"
    check(sum(1 for rl in doc.render().lines if rl.chip == rule.id) == 2,
          "every 2nd layer: two lines")
    rule.where, rule.n = "at_z", 0.04
    hits = [n for n, rl in enumerate(doc.render().lines)
            if rl.chip == rule.id]
    check(len(hits) == 1, "when Z first reaches 0.04 mm: once")
    rule.where, rule.text = "start", "M117 hi ; greet"
    p = doc.render("parse")
    check(p.lines[0].text == "M117 hi\n", "the parse form strips comments",
          repr(p.lines[0].text))
    check(doc.render().lines[0].text == "M117 hi ; greet\n",
          "the export keeps the user's comment")
    rule.where, rule.text = "before_travel", "M400"
    n_travel = sum(1 for ln in text.splitlines() if ln.startswith("G0 "))
    got = sum(1 for rl in doc.render().lines if rl.chip == rule.id)
    check(0 < got <= n_travel, "before each travel (%d of %d G0s)"
          % (got, n_travel))


def transforms_move_only_their_scope():
    text, lm = _design(n=2, stab=False, stacked=False)
    doc = G.Document(text, lm)
    base = doc.render()
    t = doc.add(G.Transform(op="offset", params={"dx": 5.0},
                            scope=G.Scope("group", 2)))
    r = doc.render()
    moved = same = 0
    ok = True
    for a, b in zip(base.lines, r.lines):
        ca, wa = G.parse_words(a.text)
        cb, wb = G.parse_words(b.text)
        if a.key and a.key[1] == "body" and a.key[2] == 1 and "X" in wa:
            ok &= abs(float(wb["X"]) - float(wa["X"]) - 5.0) < 1e-9
            moved += 1
        elif a.text == b.text:
            same += 1
        else:
            ok = False
    check(ok and moved > 10, "offset X+5 on scaffold 2 moves exactly its "
          "lines (%d moved, %d untouched)" % (moved, same))
    doc.toggle(t.id, False)
    check(doc.render().text == base.text, "unticked: byte-identical")
    doc.remove(t.id)
    sp = doc.add(G.Transform(op="speed_scale", params={"percent": 150},
                             scope=G.Scope("layers", 2, 3)))
    w = G._Walker()
    feeds = {}
    for rl in doc.render().lines:
        w.step(rl.text)
        if rl.key and rl.key[1] == "body" and G.canon_cmd(
                G.parse_words(rl.text)[0] or "") in ("G1", "G2", "G3"):
            n = doc._index(doc._compose("\n")[0])["layers"].index(
                (rl.key[0], rl.key[2], rl.key[3])) + 1
            feeds.setdefault(n, set()).add(round(w.f, 6))
    check(feeds.get(2) == {900.0} and feeds.get(3) == {900.0}
          and feeds.get(1) == {600.0} and feeds.get(4) == {600.0},
          "layers 2-3 at 150%, the rest unchanged", repr(feeds))
    doc.remove(sp.id)
    gtext, glm = _design(n=1, stab=False, g2g3=True)
    gd = G.Document(gtext, glm)
    gd.add(G.Transform(op="arcs_to_chords", params={"max_seg": 0.05}))
    out = gd.render().text
    check("G2 " not in out and "G3 " not in out, "arcs became segments")
    wa, wb = G._Walker(), G._Walker()
    for ln in gtext.splitlines():
        wa.step(ln)
    for ln in out.splitlines():
        wb.step(ln)
    check(abs(wa.x - wb.x) < 1e-6 and abs(wa.y - wb.y) < 1e-6
          and abs(wa.mm - wb.mm) / wa.mm < 0.01,
          "same end point, path length within 1%%")


def one_edit_model_places_moves_and_rewrites():
    """The page's one-form model: an added-lines edit moves between one
    spot and a pattern keeping its name and command; nudge steps it one
    line; scale multiplies the moves; typing over a rule's line edits the
    rule."""
    text, lm = _design(n=1, stab=False)
    doc = G.Document(text, lm)
    base = doc.render()
    c = doc.insert_command("dwell", doc.anchor_at(40), seconds=2.0)
    c.what, c.label = "dwell", "My pause"

    def at(cid):
        return [i for i, rl in enumerate(doc.render().lines)
                if rl.chip == cid]
    a0 = at(c.id)[0]
    steps = []
    for d in (1, 1, -1):
        doc.nudge(c.id, d)
        steps.append(at(c.id)[0])
    check(steps == [a0 + 1, a0 + 2, a0 + 1], "nudge steps one line each way",
          "%d -> %r" % (a0, steps))
    # move_to_line counts 1-based, the way the page's Line box reads
    landed = [doc.move_to_line(c.id, t) for t in (a0 + 30, 5)]
    check(landed == [a0 + 30, 5] and at(c.id)[0] == 4,
          "move_to_line puts the edit's first line on the typed line",
          "%r, now 0-based %d" % (landed, at(c.id)[0]))
    rule = doc.place(c.id, {"where": "after_layer"})
    n_layers = len(doc._index(doc._compose("\n")[0])["layers"])
    check(isinstance(rule, G.Rule) and rule.id == c.id
          and rule.label == "My pause" and rule.command == c.command
          and len(at(c.id)) == n_layers,
          "one spot -> after each layer keeps id, name and command",
          "%d places for %d layers" % (len(at(c.id)), n_layers))
    doc.set_flavor("duet")
    first = doc.render().lines[at(c.id)[0]].text.strip()
    check(first == "G4 S2", "the rule's command re-spells for Duet", first)
    doc.set_flavor("klipper")
    ins = doc.place(c.id, doc.anchor_at(10))
    check(isinstance(ins, G.Insert) and len(at(c.id)) == 1
          and ins.command == c.command, "and back to one spot")
    doc.remove(c.id)
    check(doc.render().text == base.text, "removed: byte-identical")
    t = doc.add(G.Transform(op="scale", params={"px": 120.0}))
    pairs = []
    for a, b in zip(base.lines, doc.render().lines):
        ca, wa = G.parse_words(a.text)
        cb, wb = G.parse_words(b.text)
        if G.canon_cmd(ca or "") in ("G0", "G1") and "X" in wa:
            pairs.append((float(wa["X"]), float(wb.get("X", "nan")),
                          wa.get("Y") == wb.get("Y")))
    check(pairs and all(abs(xb - 1.2 * xa) < 1e-3 and y
                        for xa, xb, y in pairs),
          "scale X 120%%: every X is 1.2x the original, Y untouched "
          "(%d moves)" % len(pairs), repr(pairs[:3]))
    doc.remove(t.id)
    r = doc.add(G.Rule(text="M400\nM117 hi", where="after_layer"))
    rr = doc.render()
    lines = rr.text.split("\n")
    hit = [i for i, rl in enumerate(rr.lines) if rl.chip == r.id
           and rl.text.strip() == "M117 hi"]
    lines[hit[0]] = "M117 bye"
    res = doc.record_edit("\n".join(lines))
    now = doc.render()
    check(doc.get(r.id).text == "M400\nM117 bye"
          and sum(1 for rl in now.lines if rl.text.strip() == "M117 bye")
          == len(hit), "typing over one line a rule wrote edits the rule "
          "everywhere", repr(res.added))
    p = doc.add(G.Rule(text="M117 L[[layer]]", where="after_layer"))
    rr = doc.render()
    lines = rr.text.split("\n")
    i = next(i for i, rl in enumerate(rr.lines) if rl.chip == p.id)
    lines[i] = "M117 frozen"
    typed = "\n".join(lines)
    doc.record_edit(typed)
    now = doc.render()
    check(doc.get(p.id).text == "M117 L[[layer]]" and now.text == typed
          and sum(1 for rl in now.lines if rl.chip == p.id) >= 1,
          "…a line that fills in a placeholder keeps the typing at that "
          "one place, the rule filling in its others")


def flavors_respell_and_never_move_the_nozzle():
    """Invariant: converting a user line to another flavor and back returns
    it, the dwell's seconds never change, and every motion line of the
    parse form is identical in every flavor."""
    fl = ["klipper", "duet", "marlin", "grbl"]
    bad = []
    for src in fl:
        for kind, kw in (("dwell", {"seconds": 2.5}), ("pause", {}),
                         ("message", {"text": "hello"}),
                         ("wait_temp", {"celsius": 90})):
            try:
                ln = G.make_command(kind, src, **kw)[0]
            except G.CommandUnavailable:
                continue
            for dst in fl:
                there = G.convert_line(ln, src, dst)
                back = G.convert_line(there, dst, src)
                if back != ln:
                    bad.append((kind, src, dst, ln, there, back))
                if kind == "dwell":
                    s1 = G.dwell_seconds(G.parse_words(ln)[1], src)
                    s2 = G.dwell_seconds(G.parse_words(there)[1], dst)
                    if abs(s1 - s2) > 1e-9:
                        bad.append(("seconds", src, dst, s1, s2))
    check(not bad, "canonical lines convert and convert back", repr(bad[:3]))
    check(G.make_command("dwell", "klipper", seconds=2) == ["G4 P2000"]
          and G.make_command("dwell", "duet", seconds=2) == ["G4 S2"]
          and G.make_command("dwell", "grbl", seconds=2) == ["G4 P2"],
          "dwell: Klipper P ms, Duet S s, GRBL P s")
    ok, why = G.form_available("wait_temp", "grbl")
    check(not ok and "GRBL" in why, "GRBL has no wait-for-temperature")
    ok, why = G.form_available("set_pressure", "klipper")
    check(not ok, "no built-in flavor invents a pressure command")
    text, lm = _design(n=1, stab=False)
    doc = G.Document(text, lm)
    doc.insert_command("dwell", G.Anchor("layer", "after",
                                         ("base", 0, 1)), seconds=2)
    doc.record_edit("G4 P1500\n" + doc.render().text)
    k_text = doc.render().text
    motion = None
    for f in fl + ["klipper"]:
        doc.set_flavor(f)
        p = [l for l in doc.render("parse").text.splitlines()
             if G.canon_cmd(G.parse_words(l)[0] or "") in G._MOTION]
        motion = motion or p
        if p != motion:
            bad.append(("motion differs", f))
    check(not bad, "the parse form's motion is the same in every flavor")
    check(doc.render().text == k_text,
          "back to Klipper: the exact text typed")
    doc.set_flavor("duet")
    check("G4 S2\n" in doc.render().text and "G4 S1.5\n" in
          doc.render().text, "in Duet both dwells read in seconds")
    gr = G.Document("G21\nG90\nG4 P2000\nG1 X1 F600\n", flavor="grbl",
                    source="file", linked=False)
    probs = [p for p in G.problems(gr) if p.code == "dwell_unit"]
    check(len(probs) == 1 and "2000 s" in probs[0].message,
          "GRBL G4 P2000 is flagged as 2000 s", repr(probs))
    G.apply_fix(gr, probs[0])
    check("G4 P2\n" in gr.render().text, "the fix writes 2 s in GRBL")


def pieces_stitch_a_foreign_file_beside_the_design():
    text, lm = _design(n=1, stab=False)
    doc = G.Document(text, lm)
    foreign = ("; from another lab\nG20\nG91\nG1 X0.1 Y0 F10\nG1 X0 Y0.1\n"
               "M2\n")
    norm, notes = G.normalise_piece_text(foreign)
    check("G20" not in norm and "G91" not in norm and
          "inches converted to mm" in notes, "normalised to mm, absolute",
          norm)
    w = G._Walker()
    for ln in norm.splitlines():
        w.step(ln)
    check(abs(w.x - 2.54) < 1e-9 and abs(w.y - 2.54) < 1e-9,
          "positions survive normalisation")
    p = doc.add_piece(G.Piece(source="file", name="other lab", text=norm,
                              offset=(20.0, 0.0, 0.0), lift=1.0))
    r = doc.render()
    joins = [rl for rl in r.lines if rl.origin == "join"]
    check(len(joins) == 3 and joins[0].text.startswith("G0 Z"),
          "a lifted join travel between the pieces", repr(joins))
    piece_lines = [rl for rl in r.lines if rl.key and rl.key[0] == p.id]
    check(piece_lines and all(not rl.text.startswith(("G21", ";"))
                              for rl in piece_lines),
          "the file's own header is dropped")
    check(r.text.startswith(text.splitlines(keepends=True)[0]),
          "the design comes first")
    xs = [float(G.parse_words(rl.text)[1]["X"]) for rl in piece_lines
          if "X" in G.parse_words(rl.text)[1]]
    check(xs and min(xs) >= 20.0 - 1e-9, "the piece sits 20 mm over")
    probs = G.problems(doc)
    check(not [q for q in probs if q.code == "homing_in_piece"],
          "no homing in the piece")


def a_document_survives_save_and_load():
    text, lm = _design()
    doc = G.Document(text, lm, flavor="duet",
                     options=G.OutputOptions(True, True, True, G.AUTO))
    doc.add(G.Rule(text="M117 [[layer]]", where="before_layer"))
    doc.add(G.Transform(op="offset", params={"dy": 1},
                        scope=G.Scope("layers", 2, 2)))
    doc.record_edit("G4 S1\n" + doc.render().text)
    again = G.Document.from_dict(json_roundtrip(doc.to_dict()), text, lm)
    check(again.render().text == doc.render().text,
          "to_dict -> JSON -> from_dict renders the same text")
    snap = doc.snapshot()
    doc.remove(doc.changes[0].id)
    doc.restore(snap)
    check(doc.render().text == again.render().text, "snapshot/restore undoes")


def json_roundtrip(d):
    import json
    return json.loads(json.dumps(d))


def generate_with_a_document_writes_its_parse_form():
    """`scaffold_gcode --gcode-doc`: the -o file is the document's parse
    form over the freshly generated base, so the STL and player see it."""
    import tempfile
    d = tempfile.mkdtemp(prefix="mewgcode_")
    base, out, m, dj = (os.path.join(d, n) for n in
                        ("base.gcode", "out.gcode", "m.json", "doc.json"))
    common = ["--width", "5", "--height", "5", "--stepover", "0.5",
              "--layers", "3", "--no-exit-move"]
    with open(os.devnull, "w") as dn:
        so = sys.stdout
        sys.stdout = dn
        try:
            sg.main(common + ["-o", base, "--line-map", m])
            doc = G.Document(open(base).read(), G.LineMap.load(m),
                             options=G.OutputOptions(True, True, False))
            doc.add(G.Rule(text="G4 P100 ; settle", where="after_layer"))
            doc.save(dj)
            rc = sg.main(common + ["-o", out, "--gcode-doc", dj])
        finally:
            sys.stdout = so
            sg._LINE_MARKS = None
    got = open(out).read()
    check(rc == 0 and got == doc.parse_text(),
          "the written file is the parse form")
    check(got.count("G4 P100\n") == 3 and "G0 " in got and "G01" not in got,
          "rule lines in, comments out, travels still G0, no padding")


def explain_speaks_plain_units():
    check(G.explain("G1 X10 Y5 F600").startswith(
        "Move in a straight line to X 10, Y 5 at 10 mm/s (600 mm/min)"),
        "a G1 in mm and mm/s")
    check("2 s" in G.explain("G4 S2", "duet") and "2 s" in
          G.explain("G4 P2000", "klipper") and "2000 s" in
          G.explain("G4 P2000", "grbl"), "dwell units follow the flavor")
    check("doesn't know" in G.explain("FROB 1"), "unknown commands say so")
    text, lm = _design()
    doc = G.Document(text, lm)
    r = doc.render()
    n = next(i for i, rl in enumerate(r.lines) if rl.key
             and rl.key[2] == 2 and rl.key[3] == 1 and rl.key[4] == 0)
    check("scaffold 2, layer 2, pass 1" in doc.explain_line(n),
          "explain-this-line says where", doc.explain_line(n))
    out = doc.outline()
    check([nd.label for nd in out][:2] == ["Header", "Base layers"]
          and len(out[2].children) == 4, "outline: header, base, scaffolds",
          repr([(nd.label, len(nd.children)) for nd in out]))


def recipes_and_new_documents():
    import tempfile
    lib_path = os.path.join(tempfile.mkdtemp(prefix="mewgcode_"), "lib.json")
    lib = G.load_library(lib_path)
    check(len(lib) == len(G.STARTER_RECIPES), "a missing library = starters")
    text, lm = _design(n=1, stab=False)
    doc = G.Document(text, lm)
    c = doc.add(G.Insert(lines=["OCTO1 800"], anchor=G.Anchor("start")))
    rec = G.recipe_from_chips(doc, "Feeder start", [c.id], use_on_new=True)
    G.save_library(lib_path, lib + [rec])
    lib2 = G.load_library(lib_path)
    check(lib2[-1].name == "Feeder start" and lib2[-1].use_on_new,
          "a saved recipe loads back")
    fresh = G.new_document(text, lm, library=lib2)
    check(fresh.render().text.startswith("OCTO1 800\n")
          and len(fresh.changes) == 1, "use on every new print: a chip, "
          "already there")
    pause = next(r for r in lib2 if r.name == "Pause after each layer")
    G.apply_recipe(fresh, pause)
    fresh.set_flavor("duet")
    check(fresh.render().text.count("G4 S2\n") == 4,
          "a starter written in Klipper reads in Duet's units")


TESTS = [
    ("the moved rewrite is byte-identical to the pre-move code",
     the_moved_rewrite_is_byte_identical_to_the_pre_move_code),
    ("a document with no chips renders the export text",
     a_document_with_no_chips_renders_the_export_text),
    ("the line map changes no output and matches the structure",
     the_line_map_changes_no_output_and_matches_the_structure),
    ("the --line-map flag writes beside the g-code, never in it",
     the_line_map_flag_writes_beside_the_gcode_never_in_it),
    ("every fixture renders identically through the document",
     every_fixture_renders_identically_through_the_document),
    ("typed text round-trips through chips",
     typed_text_round_trips_through_chips),
    ("edits carry over a redesign by structure",
     edits_carry_over_a_redesign_by_structure),
    ("rules fire at every structural place",
     rules_fire_at_every_structural_place),
    ("transforms move only their scope", transforms_move_only_their_scope),
    ("flavors respell and never move the nozzle",
     flavors_respell_and_never_move_the_nozzle),
    ("pieces stitch a foreign file beside the design",
     pieces_stitch_a_foreign_file_beside_the_design),
    ("a document survives save and load", a_document_survives_save_and_load),
    ("generate with a document writes its parse form",
     generate_with_a_document_writes_its_parse_form),
    ("explain speaks plain units", explain_speaks_plain_units),
    ("recipes and new documents", recipes_and_new_documents),
    ("one edit model: place, nudge, scale, rule lines",
     one_edit_model_places_moves_and_rewrites),
]


def main():
    print("mew_gcode (the G-code document engine):")
    only = sys.argv[1:]                 # optional name filters, for iterating
    for name, fn in TESTS:
        if only and not any(o in name for o in only):
            continue
        print(" %s" % name)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 -- a crash is a FAIL, named
            import traceback
            traceback.print_exc()
            check(False, name + " raised", repr(exc))
    if FAILURES:
        print("\n%d check(s) FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
    else:
        print("\nall checks passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
