#!/usr/bin/env python3
"""gui_lawedit.py -- the "edit this law" panel on a Physics module card.

MODERN UI ONLY, and plain functions rather than a mixin: `_phys_card` calls
`build(app, parent, law_key)` and that is the whole seam. Nothing here goes into
`ModernApp`'s MRO, so the classic developer build is untouched.

**What the panel is for.** Every fittable hook in this build is
`anchor * PROD (input/ref)^exp`. A user whose polymer wants a logarithm, a
threshold, a saturating term or a coefficient nobody anticipated had no way in.
`mew_lawlang` is the way in; this is its front end.

**Where the state lives.** Two `cal_vars` entries -- `mew_calibration`'s
`LAW_SOURCE_KEY` and `LAW_CONSTS_KEY` -- each holding JSON for EVERY law at once.
One var per key, not per law: `_calibration_persist_keys` is `list(cal_vars)`, so
a var per law would put `mew_lawlang.LAWS`' membership into the persist map, and
adding a fifth editable law later would then read as a dropped control.
Riding `cal_vars` also means the persist map, the
Physics preset kind and the workers' profile file all pick these up with no
further plumbing.

**Apply is explicit, AND applying an untouched box changes nothing.** Typing
does not change the model. A half-written expression is a syntax error for most
of the time it is being typed, and this repo's own rule is that a broken law must
fail loudly rather than silently revert -- so it must not reach the engine until
the user says so. `Check` compiles it and evaluates it at the live operating
point first, and prints the number.

The second half was the reported defect. Pressing Apply without editing a
character stored an override and swapped the card's typeset `depth = c span^p`
for a machine-rendered version of the source. `apply` now asks first whether the
text is the text WE seeded -- a comparison that needs no calibration and is made
on the parsed shape, so re-indenting is not an edit either. `mew_lawlang.
is_shipped` is the second opinion behind it, not the first, because it re-folds
against constants that come back empty whenever a rig field will not parse.

**What the box starts with, and why it is not `spec.shipped`.** The shipped
string carries a guard per optional hook (`... if sag_diam_exp != 0 else 1`), and
every one of those exponents ships at 0 -- so it reads as a four-factor product
where the equation typeset on the card says `depth = c span^p`, and CTS reads as
five factors where what runs is the bare `CTS_ref`. The box seeds from
`mew_lawlang.active_source` instead: the same law with the terms that are exactly
1 folded away. Reset restores that, and `is_shipped` accepts it as "no override".

It also seeds in the CARD'S OWN ALPHABET. `mew_lawlang.SYMBOLS` names each
calibration constant as its equation prints it, so the box under
`depth = c span^p` reads `c * span**p` rather than
`sag_coeff * span**sag_span_exp`. Two alphabets, one on the card and one in the
box, is what made Apply look like vandalism even on the runs where it stored
nothing.

**One reference table, the same on every card, and NOTHING dead in it.** Not
`spec.args` per law, and not the whole `CALIBRATION_FIELDS` registry either --
`mew_lawlang.constant_names` reads it out of the law strings, so a knob no
equation on the page names (`stable_window`, the retired
`contact_damping_zeta`) cannot appear. See `vocabulary_rows`.

**The panel body holds ONE line of prose.** Everything else -- the law's physics
note, the open-source stance, what Apply and Check do -- is in the header's info
dot. The body used to print four paragraphs, on a page where the card above had
already spent three of its own; nine cards of that is what made the Physics page
unreadable. The dot is one click and holds as much as it likes.

The guard-laden original is NOT shown. It was, in a collapsed disclosure, for
about an hour; the reaction was "I will never want the full shipped law as a
visible box". A hook that is off is reachable by fitting its exponent on the
card, at which point the fold puts its term back in the box on its own -- which
is the same information without a wall of `else 1` beside the law.

That fold is against the calibration AS IT STANDS, so the box re-seeds every
time it becomes readable: on the disclosure's own open callback (it is born
SHUT, so that is the usual route) and on `<Map>`, which covers the card being
opened with the panel already open from a previous visit. A stale form applied
after a hook was fitted would freeze that hook out of the model, because an
override ignores the card's exponent fields entirely.

**The panel is not the platform's extension point, and says so.** An expression
lives in the user's own profile, where nobody else can review, test or cite it.
This is where a form is TRIED; `mew_physics.py` is where one is published. The
card carries that sentence rather than leaving a reader to assume an in-app law
editor is how the open-source build is meant to be extended.
"""

import dataclasses
import json
import tkinter as tk
from tkinter import ttk

import gui_fonts
import gui_form
import gui_math
import gui_tips
import gui_widgets

try:
    import mew_calibration
    import mew_lawlang
    import mew_physics
    _HAVE = True
except ImportError:                                      # pragma: no cover
    _HAVE = False

PSI_TO_PA = 6894.757293168361      # same constant gui_sim reads sim_pressure with

# A law symbol as a HEADER reads in prose, not in markup: `gui_math` turns "eta"
# into a drawn glyph on the Canvas, but a Disclosure title is a plain tk.Label.
_SYMBOL_TEXT = {"eta": "η", "phi": "φ"}


class _ThemedText(tk.Text):
    """A `tk.Text` that follows the shell palette.

    `_ThemeMixin._recolor_classic` walks past every `tk.Text` on purpose -- the
    ones it knows about are the worker-log and Moonraker consoles, which are
    always dark by design. This one is a FIELD the user types into, so it has to
    take the palette itself; without that it renders as white paper on a dark
    page, which is what the first version of this panel did."""

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        gui_widgets.register(self)          # the dark/light toggle reaches us
        self.retheme()

    def retheme(self):
        p = gui_widgets.palette()
        fg = p.get("fg", "#000000")
        self.configure(background=p.get("field", "#ffffff"), foreground=fg,
                       insertbackground=fg,
                       selectbackground=p.get("select_bg", "#2f6fd6"),
                       selectforeground=p.get("select_fg", "#ffffff"),
                       highlightbackground=p.get("hairline",
                                                 p.get("border", "#d9dce1")),
                       highlightcolor=p.get("select_bg", "#2f6fd6"))


# --------------------------------------------------------------------------- #
# the two JSON vars
# --------------------------------------------------------------------------- #
def _decode(app, key):
    var = getattr(app, "cal_vars", {}).get(key)
    if var is None:
        return {}
    try:
        data = json.loads(var.get() or "{}")
    except (ValueError, tk.TclError):
        return {}
    return data if isinstance(data, dict) else {}


def _encode(app, key, data):
    var = getattr(app, "cal_vars", {}).get(key)
    if var is None:
        return
    # `{}` is written as the EMPTY STRING, not as "{}": a blank profile entry is
    # what every profile without edited laws has, so clearing every law must leave the
    # var indistinguishable from one that was never touched.
    var.set(json.dumps(data, sort_keys=True) if data else "")


def symbol(key):
    """What law `key` PRODUCES, in the spelling a card typesets it -- `depth`, not
    `sag`. The registry key is an internal id and must never reach the page."""
    if not _HAVE:
        return key
    spec = mew_lawlang.LAWS.get(key)
    return (spec.symbol if spec and spec.symbol else key)


def law_source(app, key):
    """The user's expression for `key`, or "" when the shipped law stands."""
    return str(_decode(app, mew_calibration.LAW_SOURCE_KEY).get(key, "") or "")


def law_consts(app, key):
    got = _decode(app, mew_calibration.LAW_CONSTS_KEY).get(key)
    return dict(got) if isinstance(got, dict) else {}


def _store(app, key, source, consts):
    src = _decode(app, mew_calibration.LAW_SOURCE_KEY)
    con = _decode(app, mew_calibration.LAW_CONSTS_KEY)
    if source:
        src[key] = source
    else:
        src.pop(key, None)
    if consts:
        con[key] = consts
    else:
        con.pop(key, None)
    _encode(app, mew_calibration.LAW_SOURCE_KEY, src)
    _encode(app, mew_calibration.LAW_CONSTS_KEY, con)


# --------------------------------------------------------------------------- #
# the form that is actually running
# --------------------------------------------------------------------------- #
def constants(app):
    """`MEWModel._law_constants()` for the rig as currently edited, or `{}`.

    Only the calibration half of the scope: that is all `active_source` folds
    against, and it is exactly the part that decides which hooks are off.

    The GAP is read separately and is allowed to fail. `sim_standoff` does not
    exist yet while the Physics page is being BUILT -- the sim vars are created
    after the cards -- and a missing gap must not cost us the whole fold, because
    no shipped law guards a term on the working distance. Letting it take the
    model default here is what stops every card seeding with the raw shipped
    string on a cold start."""
    try:
        material, nozzle, cal = app._calibration_models()
    except (AttributeError, TypeError, ValueError, tk.TclError):
        return {}
    gap = 4.0                               # mew_physics.MEWModel's own default
    try:
        gap = float(app.sim_standoff.get()) or gap
    except (AttributeError, TypeError, ValueError, tk.TclError):
        pass
    try:
        return mew_physics.MEWModel(material=material, nozzle=nozzle,
                                    working_distance=gap,
                                    calibration=cal)._law_constants()
    except (ValueError, ZeroDivisionError, OverflowError, TypeError):
        return {}


def seed_source(app, key):
    """What the box shows when there is no override: the shipped law with this
    calibration's off hooks folded away, so the text matches the equation
    typeset above it."""
    try:
        return mew_lawlang.active_source(key, constants(app))
    except mew_lawlang.LawError:                          # pragma: no cover
        return mew_lawlang.LAWS[key].shipped


# --------------------------------------------------------------------------- #
# the live operating point a Check is evaluated at
# --------------------------------------------------------------------------- #
SPAN_PROBE_MM = 0.5        # a representative unsupported span, for Check + table


def operating_point(app):
    """`(model, point)` -- the rig as the fields currently read it, and EVERY
    live quantity at it.

    One function, not one per card. The reference table prints these numbers and
    Check evaluates at them, so a user comparing the table to the verdict line is
    reading the same operating point rather than two that happen to agree.

    Returns (None, None) when the fields cannot be read; the caller says so
    rather than inventing a value."""
    try:
        material, nozzle, cal = app._calibration_models()
        gap = float(app.sim_standoff.get())
        volt = float(app.sim_voltage.get())
        pressure = float(app.sim_pressure.get()) * PSI_TO_PA
        temp = float(app.sim_temperature.get())
    except (AttributeError, TypeError, ValueError, tk.TclError):
        return None, None
    model = mew_physics.MEWModel(material=material, nozzle=nozzle,
                                 working_distance=gap, calibration=cal)
    speed = 800.0
    try:                       # the scaffold being designed, when there is one
        speed = float(app.scaf_vars[0]["speed"].get()) / 60.0 or 800.0
    except (AttributeError, KeyError, IndexError, TypeError, ValueError,
            tk.TclError):
        pass
    model.set_context(voltage=volt, pressure=pressure, temperature=temp,
                      gap=gap, speed=speed)
    try:
        eta = model.viscosity(temp)
        flow = model.flow_rate(pressure, temp)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None, None
    try:
        diam = model.fiber_diameter(flow, speed)
    except (ValueError, ZeroDivisionError):
        diam = 0.02
    try:
        cts = model.critical_speed(volt, gap, flow, eta)
    except (ValueError, ZeroDivisionError, OverflowError):
        cts = None
    point = {"temperature": temp, "pressure": pressure, "voltage": volt,
             "gap": gap, "speed": speed, "eta": eta, "Q": flow, "d": diam,
             "visc_ratio": 1.0, "span": SPAN_PROBE_MM,
             "phi": float(cal.fusion_overlap)}
    if cts is not None:
        point["CTS"] = cts
    point.update(_mandrel_point(app, cal, gap, speed))
    return model, point


def _mandrel_point(app, cal, gap, speed):
    """`R_ax`, `theta` and `r_m` at the Scaffold designer's speed on its mandrel,
    for the R_cir law's Check -- or `{}` when the mandrel fields will not read.

    R_ax is the one a rotary lay runs on (`mew_lag_fit.predict_radii`, with the
    mandrel CTS and any calibrated amplitude folded in), the same number the Jet
    lag card's in-use line prints."""
    try:
        import mew_lag_fit
        r_m = 0.5 * float(app.m_diameter.get())
        extras = app._mandrel_cal_extras()
        mcal = dataclasses.replace(cal, **extras)
        cts = float(extras.get("mandrel_cts") or mcal.cts_ref)
        R_ax = mew_lag_fit.predict_radii(radius=r_m, gap=gap, speed=speed,
                                         cts=cts, calibration=mcal)[0]
    except Exception:  # noqa: BLE001 -- a probe, never a blocker
        return {}
    if not r_m > 0.0:
        return {}
    import math
    return {"R_ax": R_ax, "theta": math.atan2(R_ax, gap), "r_m": r_m}


def _probe(app, key):
    """`(model, args)` -- the operating point above, narrowed to the arguments
    law `key` is actually handed. Everything else it may name is derived inside
    `MEWModel._law_scope`, which is the same path a real run takes."""
    model, point = operating_point(app)
    if model is None:
        return None, None
    spec = mew_lawlang.LAWS.get(key)
    if spec is None:
        return None, None
    args = {a: point[a] for a, _d in spec.args if a in point}
    return model, args


# --------------------------------------------------------------------------- #
# the vocabulary table -- ONE table, IDENTICAL on every card
# --------------------------------------------------------------------------- #
# Every card used to print its own list: `spec.args` plus the process values. So
# the Material card offered `eta` and the Fiber-diameter card did not, although
# both laws are evaluated by the same engine at the same operating point. The
# difference was real -- `eta` was not one of the diameter law's arguments -- but
# it was a difference in PLUMBING, and reading it off two cards it looked like a
# statement about the physics. `MEWModel._law_scope` now derives the quantities a
# law is not handed, so the list is the same everywhere and the table is what
# says so.
#
# It is a table of VALUES, not a glossary. The number beside the name is what
# that name evaluates to at the rig as the fields currently read it, which is the
# same operating point Check reports from -- so "what is `eta` here" and "what
# will my law see" are one question with one answer on screen.
def vocabulary_rows(app):
    """`[(name, what it is, value now), ...]` -- quantities first, then every
    calibration constant under the symbol its card prints.

    Not law-specific by design. An extra name in the list costs a user one line
    of reading; a MISSING one costs them a compile error and a guess."""
    _model, point = operating_point(app)
    point = point or {}
    consts = constants(app)
    rows = [(name, desc, _fmt(point.get(name)))
            for name, desc in mew_lawlang.quantity_rows()]

    # the constants, in the order the Physics page lays them out, so a user
    # scanning the page and a user scanning this table walk the same sequence --
    # and ONLY the ones an equation on that page names (`constant_names`).
    want = mew_lawlang.constant_names()
    for fs in mew_calibration.CALIBRATION_FIELDS:
        name = mew_lawlang.ATTR_SYMBOL.get(fs.attr, fs.attr)
        if name not in want:
            continue
        want.discard(name)
        desc = fs.label.rstrip(":")
        if fs.scale != 1.0:
            # the law's symbol is the dataclass quantity, not the shown one:
            # r is HALF the needle's inner diameter
            desc = "half the %s" % (desc[:1].lower() + desc[1:])
        if fs.unit:
            desc += ", %s" % fs.unit
        if name != fs.attr:
            desc += "  (%s)" % fs.attr
        rows.append((name, desc, _fmt(consts.get(name))))
    for name in sorted(want):                        # no FieldSpec behind it
        rows.append((name, mew_lawlang.NO_FIELD_DESC.get(
            name, "calibration constant"), _fmt(consts.get(name))))
    for name, val in sorted(mew_lawlang.CONSTANTS.items()):
        rows.append((name, "mathematical constant", _fmt(val)))
    return rows


def _fmt(val):
    """One table cell. SIX significant figures, not the full repr: the entries
    hold a fitted 228.16090808099523 and a derived 0.004979614906302322, and
    printed verbatim either one is a column of noise the eye cannot compare
    down. The model still sees every digit -- this is the only place trimmed."""
    if val is None:
        return "—"                     # an optional whose hook is not fitted
    try:
        f = float(val)
    except (TypeError, ValueError):
        return str(val)
    out = "%.6g" % f
    if "e" in out and 1e-6 <= abs(f) < 1e7:
        out = ("%.8f" % f).rstrip("0").rstrip(".")
    return out


def _vocabulary_table(app, parent):
    """Draw the table into `parent`. Returns a callable that re-reads the values,
    so a card re-opened after the rig was edited shows the new numbers rather
    than the ones that were true when the page was built."""
    # NOTHING stretches, and the whole table is `sticky="w"`. An opened module
    # body is as wide as the window, so a stretching middle column pushed
    # "Value now" a thousand pixels away from the name it belongs to and the eye
    # had to cross the page to pair them. Three fixed columns sitting together
    # is the point: name, meaning, number, read in one movement.
    cols = ("name", "what", "value")
    # the tree's master is `holder`, not `parent`: `parent` is the disclosure
    # body and is grid-managed, and packing a child into it raises.
    holder = ttk.Frame(parent)
    tree = ttk.Treeview(holder, columns=cols, show="headings", height=14)
    for cid, text, width, anchor in (
            ("name", "Name", 120, "w"),
            ("what", "What it is", 330, "w"),
            ("value", "Value now", 100, "w")):
        tree.heading(cid, text=text, anchor=anchor)
        tree.column(cid, width=width, minwidth=width, anchor=anchor,
                    stretch=False)
    holder.grid(row=0, column=0, sticky="w")
    bar = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=bar.set)
    tree.pack(side="left")
    bar.pack(side="left", fill="y")
    gui_widgets.scroll_guard(tree)

    def refresh():
        try:
            tree.delete(*tree.get_children())
            for row in vocabulary_rows(app):
                tree.insert("", "end", values=row)
        except tk.TclError:
            pass
    refresh()
    refresh.tree = tree          # the handle test_ui_shell reads the rows from

    # REFERENCE, kept as text: this panel is the vocabulary you type from, and
    # a list you have to hover to read is a list nobody writes from. Tagged
    # "reference" so the one-pattern guard knows it is not a parameter's help.
    note = gui_form.mark(ttk.Label(
        parent, style="Hint.TLabel", justify="left", wraplength=560,
        text="Functions: " + ", ".join(sorted(mew_lawlang.FUNCTIONS))
             + ".\nOperators: + - * / ** %, comparisons, and "
               "`a if condition else b`."), "reference")
    note.grid(row=1, column=0, sticky="w", pady=(gui_fonts.SP["s"], 0))
    return refresh


# --------------------------------------------------------------------------- #
# the panel
# --------------------------------------------------------------------------- #
def build(app, parent, key, strip=None, shipped_markup=None, named=False,
          on_applied=None):
    """Put the editor for law `key` into `parent`.

    `strip` is the card's `gui_math.MathStrip`; when given, the typeset law
    follows what the user applied, and `shipped_markup` is what Reset puts back.
    `on_applied` replaces that for a card that typesets MORE than one law -- the
    strip is a single multi-row drawing, so re-rendering it from one expression
    would wipe the other row; the card owns the recomposition and is called here.

    `named` puts the law's own symbol in the panel header. A card with one law
    does not repeat itself; the Material card carries two, and "Try a different
    form" twice over would say nothing about which.

    Returns the Disclosure, or None when the physics modules are absent (the same
    silent no-op every physics builder in this shell uses)."""
    if not _HAVE or key not in mew_lawlang.LAWS:
        return None
    spec = mew_lawlang.LAWS[key]
    header = "Try a different form"
    if named and spec.symbol:
        header += " for %s" % _SYMBOL_TEXT.get(spec.symbol, spec.symbol)
    # EVERYTHING that is not a control lives in the info dot. The panel used to
    # print four paragraphs above the box -- the result, the arguments, the
    # law's physics note and the open-source stance -- on a page where the card
    # above had already spent three paragraphs of its own. Nine cards of that is
    # what made the Physics page unreadable. The dot is one click and holds as
    # much as it likes; the body holds one line and the controls.
    disc = gui_widgets.Disclosure(
        parent, header, open=False,
        info=("Replace the default law with your own expression, using the "
              "names in the table inside and any coefficients you name.\n\n"
              "The change is for your printer only: it is saved with your "
              "presets, not in the code. The default law is the one MEWracle "
              "reports and the published model describes. A form other users "
              "should have belongs in mew_physics.py as a contribution, where "
              "it can be reviewed, tested and cited. Use this box to find out "
              "whether a form is worth contributing.\n\n"
              "The box starts from the law as it runs at your calibration: the "
              "default form without the terms you have not fitted, so it reads "
              "like the equation above it, in the same letters. Fit a term on "
              "the card and it reappears here. Applying the text unchanged "
              "does nothing.\n\n"
              "Nothing reaches the model until you press Apply. Check compiles "
              "the expression and evaluates it at your current process "
              "settings, so you see the number first.\n\n"
              "A law that will not compile shows as an error wherever it is "
              "used. It never falls back to the default law, so a wrong "
              "expression cannot pass for one that has no effect.\n\n"
              + (spec.note or "")))
    body = disc.body
    # the panel's rows through the shared builder: one intro line, then each
    # control on the card's label and field edges
    F = gui_form.Form(body)

    # -- the expression ---------------------------------------------------- #
    # One line, and it is the one a user needs to write the expression: what
    # number to produce, and which names change per call. Everything else is in
    # the dot above and in the table below.
    F.intro("Gives %s. Uses %s and anything in the table below."
            % (spec.result, ", ".join(a for a, _d in spec.args)))
    # width in CHARACTERS, and sticky="w" rather than "ew": an opened module body
    # is as wide as the page, and a field stretched to 1400 px reads as a document
    # rather than as one line of maths. 72 is the same measure as the wraplength
    # 640 the prose on this card uses.
    text = _ThemedText(body, height=4, width=72, wrap="word", font="MewMono",
                       undo=True, relief="flat", highlightthickness=1)
    F.slot("Expression", text,
           "Your law, over the names in the table below and your own "
           "coefficients. It must produce %s." % spec.result)
    gui_widgets.scroll_guard(text)

    seeded = [""]                       # the last text WE put in the box

    def seed(force=False):
        """Put the no-override form in the box. `force` is Reset; otherwise this
        only writes when the box still holds what we last seeded, so it can be
        re-run on open without ever eating something the user typed."""
        cur = text.get("1.0", "end").strip()
        if not force and cur and cur != seeded[0]:
            return
        fresh = seed_source(app, key)
        if not force and cur == fresh:
            seeded[0] = fresh
            return
        text.delete("1.0", "end")
        text.insert("1.0", fresh)
        seeded[0] = fresh

    have = law_source(app, key)
    if have:
        text.insert("1.0", have)
    else:
        seed(force=True)

    # Re-seed whenever the box becomes VISIBLE, and BOTH routes are wired: the
    # disclosure's open callback (it is born shut now, so opening it is the
    # usual route) and `<Map>`, which fires when the module CARD is opened with
    # the panel already open from a previous visit. Either alone leaves one of
    # the two orders unseeded.
    #
    # Re-seeding matters twice over. The fold is against the calibration as it
    # stands, so a hook fitted on the card after this panel was built would
    # otherwise leave a stale form in the box -- and applying that stale form
    # would quietly freeze the newly fitted hook out of the model. It also
    # covers the cold start, where `sim_standoff` did not exist yet.
    text.bind("<Map>", lambda _e: (seed(), _refresh_ref()), add="+")
    disc._command = lambda opened: (seed(), _refresh_ref()) if opened else None

    # `refresh_table` is defined further down, after the reference panel is
    # built; this indirection is only so the two `<Map>` handlers above can be
    # written once, beside the seeding they belong with.
    _ref = []

    def _refresh_ref():
        for fn in _ref:
            fn()


    # -- the user's own coefficients --------------------------------------- #
    coef_tip = ("Names you choose and their values. Use them in the "
                "expression like any name in the table.")
    # the coefficient lines and their Add button share ONE field cell, so an
    # empty list does not leave the label alone on a row of its own
    coef = ttk.Frame(body)
    rows = ttk.Frame(coef)
    rows.pack(anchor="w")
    F.slot("Your coefficients", coef, coef_tip)
    entries = []            # [(name_var, value_var, the row frame)]

    def add_row(name="", value=""):
        nv, vv = tk.StringVar(value=name), tk.StringVar(value=value)
        line = ttk.Frame(rows)
        line.pack(anchor="w", pady=1)
        gui_form.mark(ttk.Entry(line, textvariable=nv, width=14),
                      "field").pack(side="left")
        gui_form.mark(ttk.Label(line, text="="), "sublabel").pack(
            side="left", padx=gui_fonts.SP["xs"])
        gui_form.mark(ttk.Entry(line, textvariable=vv, width=12),
                      "field").pack(side="left")
        rec = [nv, vv, line]

        def drop():
            line.destroy()
            if rec in entries:
                entries.remove(rec)
        ttk.Button(line, text="✕", width=3, command=drop).pack(
            side="left", padx=(gui_fonts.SP["s"], 0))
        entries.append(rec)
        return rec

    for nm, val in sorted(law_consts(app, key).items()):
        add_row(nm, mew_calibration.fmt_value(val))
    ttk.Button(coef, text="Add a coefficient",
               command=lambda: add_row()).pack(anchor="w")

    # -- the variable reference: ONE table, the same on every card ---------- #
    ref = gui_widgets.Disclosure(body, "Names you can use")
    F.slot(None, ref, sticky="ew")
    _ref.append(_vocabulary_table(app, ref.body))
    disc._law_ref_tree = _ref[0].tree

    # -- the verdict line --------------------------------------------------- #
    # what Check / Apply found -- a computed result, so it stays text
    verdict = F.live(None, text="", wrap=640).field

    def read_consts():
        out, bad = {}, []
        for nv, vv, _line in entries:
            nm = nv.get().strip()
            if not nm:
                continue
            if not nm.isidentifier() or nm in mew_lawlang.FUNCTIONS \
                    or nm in mew_lawlang.CONSTANTS:
                bad.append(nm)
                continue
            try:
                out[nm] = float(vv.get())
            except (TypeError, ValueError):
                bad.append(nm)
        return out, bad

    def check(quiet=False):
        """Compile + evaluate at the live operating point. Returns the source on
        success, None otherwise, and writes the verdict line either way."""
        src = text.get("1.0", "end").strip()
        consts, bad = read_consts()
        if bad:
            verdict.configure(text="Coefficient %s needs a plain name and a "
                                   "number." % ", ".join(bad))
            return None
        model, args = _probe(app, key)
        if model is None:
            verdict.configure(
                text="Cannot read the current process settings, so the "
                     "expression cannot be evaluated. It is still applied as "
                     "typed.")
            return src
        cal = dataclasses.replace(
            model.calibration, law_source={key: src},
            law_consts={key: consts})
        probe = mew_physics.MEWModel(
            material=model.material, nozzle=model.nozzle,
            working_distance=model.working_distance, calibration=cal)
        probe.context = dict(model.context)
        try:
            fn = probe._law(key)
            value = fn(probe._law_scope(key, **args))
        except mew_lawlang.LawError as exc:
            verdict.configure(text="✕  %s" % exc)
            return None
        at = ", ".join("%s = %s" % (k, mew_calibration.fmt_value(v))
                       for k, v in sorted(args.items()))
        if not quiet:
            verdict.configure(
                text="✓  %s = %s\n     at %s"
                     % (spec.result, mew_calibration.fmt_value(value), at))
        return src

    def retypeset(src):
        if on_applied is not None:
            on_applied()
            return
        if strip is None:
            return
        # the LHS is the law's own symbol, so an edited card still reads
        # `depth = ...` rather than `sag = ...` -- the key is an internal id
        markup = (gui_math.from_expression(src, spec.symbol or key) if src
                  else None) or shipped_markup
        if markup:
            try:
                strip.label.set_source(markup)
            except tk.TclError:
                pass

    def apply():
        src = check()
        if src is None:
            return
        consts, _bad = read_consts()
        # Handing back the shipped law is the same as not overriding it: store ""
        # so the profile stays indistinguishable from an untouched one, the card
        # keeps the equation it was typeset with, and every folded-away hook
        # stays live.
        #
        # The FIRST test is the decisive one: did the user hand back the exact
        # text we put in the box? That is what "I changed nothing" means, and it
        # needs no calibration to decide. `is_shipped` alone was not enough --
        # it re-folds the shipped law against `constants(app)`, which comes back
        # EMPTY whenever the rig fields will not parse, and then an untouched
        # Apply stored an override and rewrote the card's equation in front of
        # the user. Comparison is on the parsed shape, so re-indenting the box
        # is not an edit either.
        override = src
        if (mew_lawlang.normalize(src) == mew_lawlang.normalize(seeded[0])
                or mew_lawlang.is_shipped(key, src, constants(app))):
            override = ""
        _store(app, key, override, consts)
        retypeset(override)
        if hasattr(app, "_refresh_module_summaries"):
            app._refresh_module_summaries()
        if hasattr(app, "status"):
            # Say WHICH of the two things happened. "law applied" after an
            # untouched Apply reads as "I have just changed your physics",
            # which is the opposite of what was stored.
            app.status.set("%s: %s" % (
                spec.title,
                "your own law is now in use." if override
                else "unchanged. The default law still runs."))

    def reset():
        seed(force=True)
        for _nv, _vv, line in list(entries):
            line.destroy()
        del entries[:]
        _store(app, key, "", {})
        retypeset("")
        verdict.configure(text="Back to the default law.")
        if hasattr(app, "_refresh_module_summaries"):
            app._refresh_module_summaries()

    btns = ttk.Frame(body)
    F.slot(None, btns)
    ttk.Button(btns, text="Check", command=check).pack(side="left")
    ttk.Button(btns, text="Apply", command=apply).pack(
        side="left", padx=(gui_fonts.SP["s"], 0))
    ttk.Button(btns, text="Reset law to default", command=reset).pack(
        side="left", padx=(gui_fonts.SP["s"], 0))

    disc._law_key = key                 # the handle test_ui_shell asserts on
    disc._law_text = text
    disc._law_apply = apply
    disc._law_reset = reset
    disc._law_check = check
    disc._law_add_const = add_row
    return disc


def summary(app, key):
    """One phrase for the module tile: whether this law is the shipped one."""
    if not _HAVE:
        return ""
    src = law_source(app, key)
    if not src:
        return "default law"
    n = len(law_consts(app, key))
    return "your own law" + (" · %d coefficient%s" % (n, "" if n == 1 else "s")
                             if n else "")
