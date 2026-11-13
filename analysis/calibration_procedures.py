#!/usr/bin/env python3
"""calibration_procedures.py -- the operator procedure for every calibrated quantity,
as data.

WHAT THIS IS. One `Procedure` per tab on the Calibration page: what the tab
calibrates, whether it owns a print, the steps an operator follows from cold rig to an
applied number, and what to measure. The GUI renders these; it does not author them.

WHO IT IS WRITTEN FOR. Someone who has never opened this software and does not know
the physics. Second person, one action per step, on-screen names quoted exactly as
they are labelled, units on every number. No model internals, no file names, no jargon
without a plain gloss.

THIS MODULE IS THE ONE PLACE THE OPERATOR PROCEDURE LIVES. `LAG_CIRCLE_PROTOCOL.md`
points here and keeps developer notes only. When a step changes, change it HERE --
a second copy in markdown is how the app and the runbook drift apart.

EVERY STEP IS SOURCED. Each procedure carries a `# source:` comment naming the
file:line it was written from. Nothing here is invented lab practice: where no source
in this repo says how to measure something, the step says so in words rather than
guessing, and those gaps are the ones to close first.

HEADLESS. Imports nothing but `dataclasses` -- no Tk, no VTK, no engine module.
"""
from __future__ import annotations

from dataclasses import dataclass


#: What a step says when this repo documents no measurement method. Grep for it;
#: every occurrence is a real gap, not a placeholder to be filled in from memory.
UNDOCUMENTED = ("How to measure this is not written down yet. Report it "
                "on the project's GitHub issues.")


# --------------------------------------------------------------------------- #
# The four groups every procedure is told in, and the headings the Calibration page's
# "Instructions" disclosure renders them under.
#
# They are the four things an operator does, in the order they do them: set the
# software up, make the print, take the measurement, put the number back in. The page
# numbers the steps 1..N across all four groups, so an operator citing "step 11" means
# the eleventh step of the whole procedure.
#
# A tab that owns no print still has all four groups. "Make the print" then says what
# to print by hand and at what settings.
# --------------------------------------------------------------------------- #
SETUP = "Set up the software"
PRINT = "Make the print"
MEASURE = "Measure"
APPLY = "Enter and apply (Measurements)"

#: the group headings, in render order.
GROUP_ORDER = (SETUP, PRINT, MEASURE, APPLY)


@dataclass(frozen=True)
class Procedure:
    """One calibrated quantity, print to applied number.

    `key`             -- the stable id; also the tab's order key in PROCEDURES.
    `title`           -- the tab's heading.
    `tab`             -- the SHORT label the page's tab strip shows; the title is far
                         too wide for a tab.
    `calibrates`      -- one short paragraph: what gets written and why it matters.
    `print_protocol`  -- "cts" | "circle" | "diam" | "visc" | "sag" | "tube" | None.
                         Set means the tab
                         owns a print the shared bottom bar's Generate produces; None
                         means the operator brings their own print. "diam" (the
                         activation-energy temperature ladder) and "visc" (the
                         viscosity plate, ONE slide at ONE temperature) run the same
                         generator -- the plate is a one-rung ladder -- but are
                         separate protocols over separate variables. The calibration
                         runbook derives both from one ladder, but the
                         two are recalibrated on different clocks -- viscosity weekly
                         as the melt ages, activation energy once per polymer batch --
                         so a viscosity run must not disturb the ladder the Ea tab has
                         designed.
    `groups`          -- the operator steps, in the four groups of GROUP_ORDER. Steps
                         carry NO numbers of their own; the page numbers them 1..N
                         across the whole procedure.
    `settings_notes`  -- section id -> (step number, one-line summary), one entry per
                         settings sub-section the tab shows. The ids are the keys of
                         SECTION_HEADERS[key].
    `measure`         -- what to measure, how, and in what units.
    `shared_cards`    -- which of the page's shared print sections (the ids of
                         `SHARED_CARDS`) this tab shows. The page hides the rest, so
                         a tab never shows a card that does not apply to it and no
                         step has to say "ignore" it. Left out, all of them.

    `steps` is a derived flatten of `groups`, in group order, so every existing caller
    reads the same tuple it always did. A property, not a field: a second stored copy
    of the same prose is exactly what this module exists to prevent.
    """

    key: str
    title: str
    tab: str
    calibrates: str
    print_protocol: str | None
    groups: tuple[tuple[str, tuple[str, ...]], ...]
    settings_notes: dict[str, tuple[int, str]]
    measure: str
    shared_cards: frozenset[str] | None = None

    def __post_init__(self):
        if self.shared_cards is None:
            object.__setattr__(self, "shared_cards", SHARED_CARDS)

    @property
    def steps(self) -> tuple[str, ...]:
        """Every step, in order, with the group boundaries dropped."""
        return tuple(s for _, ss in self.groups for s in ss)


# --------------------------------------------------------------------------- #
# The settings sub-sections each tab shows, id -> the text on screen above it.
#
# Six of them are the shared print-design sections every tab carries -- in
# the Calibration settings, Stabilization scaffold, Export and Calibration presets
# cards or on the preview's header -- so they are on screen on ALL SEVEN
# tabs even though only the two print-owning tabs use them; the five fit-only tabs
# say so in a step. The rig + material profile's own box is NOT one of them: it
# lives on the Physics page as "Physics presets". Fusion is the exception: the page
# hides those cards on its tab (`gui_calibration._CAL_FIT_ONLY`), so it names none of
# them and has no "ignore the print settings" step. Where a group of fields carries no
# heading of its own, the header text is the FIRST FIELD LABEL in that group.
# --------------------------------------------------------------------------- #
# source: gui_sweeps.py:628-652, :654-795, :818-895, :897-930;
_SHARED_PRINT_HEADERS = {
    "plate_feed_limit": "Plate & feed limit",
    "stabilization_base": "Base",
    "preview_options": "Preview",
    # the LABEL changed, the id did not: this box saves the calibration
    # PRINT this page designs. The rig + material profile has its own box
    # on the Physics page, "Physics presets", which is not a section of
    # any tab here.
    "experiment_presets": "Presets",
    "export_options": "Export",
    "target_slide": "Slides",
}

#: every shared section a tab may show; `Procedure.shared_cards` picks from these.
SHARED_CARDS = frozenset(_SHARED_PRINT_HEADERS)

#: the shared sections a print on a MANDREL has no use for: a tube has no plate,
#: no base pad and is one print.
_PLATE_ONLY = frozenset({"plate_feed_limit", "stabilization_base", "target_slide"})
MANDREL_CARDS = SHARED_CARDS - _PLATE_ONLY


def _shared_headers(cards):
    """The shared section headers of a tab that shows `cards`."""
    return {k: v for k, v in _SHARED_PRINT_HEADERS.items() if k in cards}


SECTION_HEADERS: dict[str, dict[str, str]] = {
    "cts": dict(_SHARED_PRINT_HEADERS,
                cts_feeds="Speed ladder",
                measured_cts="Measured CTS (mm/min)",),
    "cts4": dict(_shared_headers(MANDREL_CARDS),
                 cts4_feeds="Speed ladder",
                 measured_cts="Measured CTS (mm/min)",),
    "viscosity": dict(_SHARED_PRINT_HEADERS,
                      visc_conditions="Temperature",
                      fit_inputs="Pressure (psi)",),
    "activation_energy": dict(_SHARED_PRINT_HEADERS,
                              diam_ladder="Temperature ladder",
                              temperature_rows="Temperature (°C)",),
    "lag3": dict(_SHARED_PRINT_HEADERS,
                 conditions="Conditions",
                 specimens="Specimens",
                 specimen_size="Specimen size",
                 no_nesting="No nesting — one separate circle per specimen",
                 spacing_lead_in="Spacing, lead-in & base speed",
                 printed_gap="Printed gap (mm)",
                 measured_rows="Deposited R (mm)",),
    "lag4": dict(_shared_headers(MANDREL_CARDS),
                 tube_bands="Tube design",
                 gap_rows="Winding angle (°)",),
    "sag": dict(_SHARED_PRINT_HEADERS,
                sag_ladder="Spans (mm)",
                fit_inputs="Span (mm)",),
    # fusion shows no print tiles at all, so it names none of the shared ones
    "fusion": dict(fit_inputs="Layers N",),
}


# The two sentences every fit-only tab needs, written once so the five of them
# cannot drift apart.
_NO_PRINT_HERE = (
    "This type designs no print, so “Parameters” and “Base” stay "
    "greyed and the window shows your own design."
)
_SHARED_NOTE = "Belongs to the types that design a print. Nothing on this type uses it."

# The same six shared sections, seen from a tab that DOES design a print. Written
# for the activation-energy LADDER; the viscosity plate overrides the three whose
# wording assumes more than one slide.
_LADDER_SHARED = {
    "plate_feed_limit": "Layout margin and an optional printer feed ceiling. "
                        "The defaults are fine here.",
    "stabilization_base": "A settling pad printed before the lines, so the jet "
                          "is steady.",
    "preview_options": "What the on-screen preview draws. It changes no g-code.",
    "export_options": "The measurement-CSV template for this ladder.",
    "target_slide": "Every rung is on screen at once. Slide 1 is the first "
                    "temperature. “Save g-code as…” writes one file each.",
    "experiment_presets": "Saves this ladder design so you can run it again "
                          "unchanged.",
}


# --------------------------------------------------------------------------- #
# The procedures, in tab order.
# --------------------------------------------------------------------------- #

# source: the CTS calibration procedure written for the accompanying paper
#   (the descending ladder, 600 down to 240 mm/min in 60 mm/min steps at 3 lines
#   per speed; the slowest speed whose lines are all straight; one wobbling line
#   disqualifies that speed; move the top and bottom rungs if every line or no
#   line wobbles; the resolution is the step, refined by a finer ladder centred
#   on the first result; read by eye); mew_sweeps.py:9-17 (generate_cts, feed
#   stepping DOWN); LAG_CIRCLE_PROTOCOL.md:43-67 (CTS measured on the same rig in
#   the same session); gui_sweeps `_sw_build_protocol_cts` (the form's speeds are
#   mm/min) and gui_calibration `_build_fit_cts_card` (the box is mm/min, Apply
#   converts to the Physics page's mm/s, mew_physics.py:385); gui_shell.py:459,
#   :494.
_CTS = Procedure(
    key="cts",
    title="Critical translation speed (CTS) on a plate",
    tab="CTS (plate)",
    calibrates=(
        "This writes the critical translation speed: the collector speed at "
        "which the jet stops coiling and starts laying a straight line. Every "
        "other speed in the model is measured against it. Get it wrong and the "
        "jet lag and the lag amplitude are wrong too. It drifts with melt age "
        "and room temperature, so measure it again in the same session as any "
        "calibration that uses it."
    ),
    print_protocol="cts",
    groups=(
        (SETUP, (
            "On Setup > Physics, set “Inner diameter, D (mm)” and "
            "“Needle length, L (mm)” on the “Material and printer "
            "parameters” card, then “Temperature (°C)”, “Pressure (psi)”, "
            "“Voltage (kV)” and “Nozzle–collector gap (mm)” on the “Process "
            "parameters” card. The CTS you measure holds only at those "
            "settings.",
            "Set “Fastest speed (mm/min)”, “Slowest speed (mm/min)”, “Speed "
            "step (mm/min)” and “Lines per speed”. The defaults, 600 mm/min down "
            "to 240 mm/min in steps of 60 mm/min with 3 lines each, suit a first print.",
        )),
        (PRINT, (
            "Press “Generate”, then “Save g-code as…” and pick a folder.",
            "Print each file on its own clean glass slide.",
        )),
        (MEASURE, (
            "Look at the lines by eye or under low magnification, speed by "
            "speed.",
            "Find the slowest speed whose lines are all straight. That is the "
            "CTS, in mm/min. One wobbling line rules that whole speed out.",
            "If every line wobbles, raise both speeds. If none wobble, lower "
            "both. Then print again.",
            "The answer is only as fine as “Speed step (mm/min)”. For a finer "
            "answer, print again with a smaller step centred on the first "
            "result.",
        )),
        (APPLY, (
            "Type the CTS into “Measured CTS (mm/min)”.",
            "Press “Check” and confirm the speed it echoes is the one you read "
            "off the slide.",
            "Press “Apply” on the ribbon. It writes the CTS to the Physics "
            "page.",
            "Apply keeps it: it is saved in the physics preset “My calibration”, which loads at every start. Save this ladder under “Presets” here to print it again.",
        )),
    ),
    settings_notes={
        "cts_feeds": (2, "The ladder itself: the speeds, the step, the lines "
                         "per speed and how the lines are laid out."),
        "plate_feed_limit": (4, "Layout margin and an optional printer speed "
                                "ceiling. The defaults are fine here."),
        "stabilization_base": (4, "A settling pad printed before the test "
                                  "lines, so the jet is steady."),
        "preview_options": (4, "What the on-screen preview draws. It changes "
                               "no g-code."),
        "export_options": (4, "An optional measurement-CSV template, off by "
                              "default. The g-code already records every "
                              "line's speed."),
        "target_slide": (3, "Every slide is on screen at once. "
                            "“Save g-code as…” writes one file each."),
        "experiment_presets": (12, "Saves this ladder so you can print it "
                                   "again unchanged."),
        "measured_cts": (9, "The one number this type needs: the speed you read "
                            "off the slide, in mm/min."),
    },
    measure=(
        "The slowest speed, in mm/min, at which every line printed at that "
        "speed is straight. Read it off the slide by eye or under low "
        "magnification. No scale calibration is needed: the answer is a speed "
        "you commanded, not a length you measured."
    ),
)


# source: the accompanying paper's mandrel calibration procedure (CTS is
#   measured on the mandrel with the same descending ladder, printed as lines
#   along the mandrel axis) and its CTS procedure (the ladder and the reading,
#   as for _CTS above); mew_sweeps `CtsMandrelParams` /
#   `generate_cts_mandrel` (each speed a group of axial lines, fastest first);
#   gui_calibration `_apply_fit_cts` (Apply writes `m_cts`, never `cal.cts_ref`);
#   gui_pages `_phys_mandrel_lag` (the Jet lag card's "Mandrel CTS");
#   gui_mandrel.py:534 (the Scaffold designer's "Mandrel diameter").
_CTS4 = Procedure(
    key="cts4",
    title="Critical translation speed (CTS) on a mandrel",
    tab="CTS (mandrel)",
    calibrates=(
        "This writes the critical translation speed measured on the mandrel, "
        "for mandrel prints only. Plate prints keep their own. It is the ladder "
        "of the “CTS (plate)” type, printed as short lines along the mandrel "
        "axis. The “Jet lag (mandrel)” type and every mandrel print use it; left "
        "unmeasured, they use the flat CTS."
    ),
    print_protocol="cts4",
    groups=(
        (SETUP, (
            "Open the Scaffold designer, switch the collector to the "
            "mandrel, and set “Mandrel diameter (mm)” to the mandrel you will "
            "print on.",
            "Set “Temperature (°C)”, “Pressure (psi)”, “Voltage (kV)” and "
            "“Nozzle–collector gap (mm)” on the “Process parameters” card "
            "(Setup > Physics). The CTS you measure holds only at those "
            "settings.",
            "Set “Fastest speed (mm/min)”, “Slowest speed (mm/min)”, “Speed "
            "step (mm/min)” and “Lines per speed”, as on a slide.",
            "Check the ladder on the mandrel in the preview. Each speed is a "
            "group of short lines along the mandrel, labelled with its speed.",
        )),
        (PRINT, (
            "Press “Generate”, then “Save g-code as…”.",
            "Print it on the mandrel. The fastest speed is printed first.",
        )),
        (MEASURE, (
            "Look along the mandrel by eye or under low magnification, "
            "starting from the fastest speed.",
            "Find the slowest speed whose lines are all straight. That is the "
            "mandrel CTS, in mm/min. One wobbling line rules that whole speed "
            "out.",
            "If every line wobbles, raise both speeds. If none wobble, lower "
            "both. Then print again.",
        )),
        (APPLY, (
            "Type the CTS into “Measured CTS (mm/min)”.",
            "Press “Check” and confirm the speed it echoes is the one you read "
            "off the mandrel.",
            "Press “Apply” on the ribbon. It writes the mandrel CTS, shown on "
            "the Physics page's Jet lag card. The flat CTS is not changed.",
            "You may save this ladder under “Presets” here.",
        )),
    ),
    settings_notes={
        "cts4_feeds": (3, "The ladder, how its lines are laid out, and where "
                          "it sits on the mandrel."),
        "preview_options": (4, "What the on-screen preview draws. It changes "
                               "no g-code."),
        "export_options": (5, "An optional measurement-CSV template, off by "
                              "default. The g-code already records every "
                              "line's speed."),
        "experiment_presets": (13, "Saves this ladder so you can print it "
                                   "again unchanged."),
        "measured_cts": (10, "The one number this type needs: the speed you "
                             "read off the mandrel, in mm/min."),
    },
    measure=(
        "The slowest speed, in mm/min, at which every line printed at that "
        "speed along the mandrel is straight. Read it by eye or under low "
        "magnification."
    ),
    shared_cards=MANDREL_CARDS,
)


# source: gui_calibration.py:635-639 (the viscosity blurb: one fiber at a known
#   pressure/temperature/speed, d proportional to 1/sqrt(eta));
#   mew_calibration.py:490-497 (fit_viscosity_ref -- the anchor scaled by
#   (d_model/d_measured)^2, folding r, L, Ea and T_ref through the model's own flow
#   law); mew_physics.py:300 (`viscosity_ref`, Pa*s at temperature_ref, and that it
#   must be RE-ANCHORED whenever the needle bore or length changes);
#   gui_calibration.py:645-649 (the four field labels).
#   The measurement method: ImageJ on microscope images
#   calibrated on a stage micrometer.
_VISCOSITY = Procedure(
    key="viscosity",
    title="Viscosity",
    tab="Viscosity",
    calibrates=(
        "This writes the melt viscosity anchor: how thick the melt is at one "
        "reference temperature. It sets the size of every fiber diameter the "
        "model predicts. Thinner melt, thinner fiber. It is one slide at one "
        "temperature, and it is the calibration to repeat most often: the melt "
        "ages in the barrel, so re-run it weekly."
    ),
    print_protocol="visc",
    groups=(
        (SETUP, (
            "On Setup > Physics, set your printer’s “Inner diameter, D "
            "(mm)” and “Needle length, L (mm)” on the “Material and printer "
            "parameters” card, then “Pressure (psi)”, “Voltage (kV)” and "
            "“Nozzle–collector gap (mm)” on the “Process parameters” card. "
            "The anchor you measure holds only at those settings.",
            "Set “Print temperature (°C)” to the temperature you want the "
            "anchor at, and “Print feed (mm/min)” to any speed above the CTS. "
            "The line under the heading says whether it clears.",
            "Set “Lines on the slide” to how many fibers you want to measure.",
        )),
        (PRINT, (
            "Press “Generate”, then “Save g-code as…” and pick a folder. This "
            "type lays one slide.",
            "Set the nozzle to your print temperature, hold the pressure and "
            "the speed fixed, and print the slide.",
        )),
        (MEASURE, (
            "Measure the diameter of one deposited fiber, in {fiber_d}. One method is "
            "to use ImageJ on microscope images of the fibers, calibrated on a "
            "stage micrometer.",
        )),
        (APPLY, (
            "Under “Enter your measurements”, check that “Pressure (psi)”, "
            "“Temperature (°C)” and “Print speed” match what you printed "
            "at. They are filled in from the plate you designed.",
            "Type the fiber diameter in {fiber_d} into “Measured fiber Ø ({fiber_d})”.",
            "Press “Compute”. “Measurements” shows the melt viscosity anchor in Pa·s.",
            "Press “Apply” on the ribbon.",
            "The new anchor shows on the Physics page. Apply keeps it: it is saved in the physics preset “My calibration”, which loads at every start.",
        )),
    ),
    settings_notes={
        "visc_conditions": (2, "The one temperature this plate is printed at, "
                               "the one feed, and how many lines the slide "
                               "gets."),
        "fit_inputs": (7, "The pressure, temperature and speed you printed at, "
                          "plus one cell per line you measured. They are "
                          "averaged."),
        "plate_feed_limit": (4, _LADDER_SHARED["plate_feed_limit"]),
        "stabilization_base": (4, _LADDER_SHARED["stabilization_base"]),
        "preview_options": (4, _LADDER_SHARED["preview_options"]),
        "experiment_presets": (4, "Saves this plate design so you can run it "
                                  "again unchanged."),
        "export_options": (4, "An optional measurement-CSV template for this "
                              "plate, off by default."),
        "target_slide": (4, "This type designs only one slide."),
    },
    measure=(
        "Every line’s diameter, in {fiber_d}, from one slide printed at a pressure "
        "(psi), a nozzle temperature (°C) and a print speed you know."
    ),
)


# source: gui_calibration.py:677-681 (the Ea blurb: fixed pressure and speed, several
#   temperatures, Ea from ln(d^2) vs 1/T, "Fill at least two rows");
#   gui_calibration.py:660-673 (five (T, diameter) rows and their headings);
#   mew_calibration.py:507-514 (fit_activation_energy -- least squares on ln(d^2)
#   against 1/T_kelvin, slope = -Ea/R; pressure and speed need not be known because
#   they only shift the discarded intercept, and it raises on fewer than two DISTINCT
#   temperatures); mew_physics.py:317 (`activation_energy`, J/mol, neat PCL 80-120 C).
#   No source in this repo states how the fiber diameter is measured.
_ACTIVATION_ENERGY = Procedure(
    key="activation_energy",
    title="Activation energy",
    tab="Activation energy",
    calibrates=(
        "This writes the flow activation energy: how fast the melt thins as it "
        "gets hotter. It is what lets the model predict a fiber diameter at a "
        "temperature you have not printed at."
    ),
    print_protocol="diam",
    groups=(
        (SETUP, (
            "Open Calibrate and pick “Activation energy” from the “Type” menu.",
            "Plan two to five nozzle temperatures across the range you print "
            "in. Two different temperatures is the minimum the fit accepts.",
            "Pick one operating pressure and one print speed, and keep both for "
            "every temperature.",
        )),
        (PRINT, (
            "Type your temperatures into “Temperatures (°C)”, hottest first. "
            "Every temperature's slide is on screen at once. Press “Save g-code "
            "as…” in the bottom row for one file per slide, print slide 1 at "
            "the first temperature, then each next file at the next.",
            "Let the melt settle at each new temperature before you print that "
            "row.",
            "Keep the pressure and the print speed the same for every "
            "temperature. You do not need to know their values, although "
            "recording them may be useful for future reference.",
        )),
        (MEASURE, (
            "Measure the fiber diameter, in {fiber_d}, for every repetition line from "
            "each temperature's print. One method is to use ImageJ on "
            "microscope images calibrated on a stage micrometer.",
        )),
        (APPLY, (
            "Type each temperature in °C into a “Temperature (°C)” cell, with "
            "that slide’s line diameters in {fiber_d} in the “Fiber Ø ({fiber_d})” cells "
            "beside it. Blank cells are skipped.",
            "Leave unused rows blank.",
            "Press “Compute”. “Measurements” shows the activation energy in J/mol and "
            "in kJ/mol.",
            "You may check the kJ/mol figure against the measured range for "
            "plain PCL, about 31 to 41 kJ/mol.",
            "Press “Apply” on the ribbon.",
            "Apply keeps it: it is saved in the physics preset “My calibration”, which loads at every start.",
        )),
    ),
    settings_notes={
        "diam_ladder": (2, "The temperatures to print at, the one feed to hold "
                           "for all of them, and how many lines each gets. One "
                           "temperature, one slide."),
        "temperature_rows": (8, "One row per temperature you printed, and one "
                                "cell per line on that temperature’s slide. "
                                "Each row’s cells are averaged."),
        "plate_feed_limit": (4, _LADDER_SHARED["plate_feed_limit"]),
        "stabilization_base": (4, _LADDER_SHARED["stabilization_base"]),
        "preview_options": (4, _LADDER_SHARED["preview_options"]),
        "experiment_presets": (4, _LADDER_SHARED["experiment_presets"]),
        "export_options": (4, _LADDER_SHARED["export_options"]),
        "target_slide": (4, _LADDER_SHARED["target_slide"]),
    },
    measure=(
        "Every repetition line’s diameter, in {fiber_d}, at each of several nozzle "
        "temperatures in °C, all printed at the same pressure and speed."
    ),
)


# source: LAG_CIRCLE_PROTOCOL.md:31-197 (the runbook this module replaces);
#   mew_circle_fit.py:1-20 (THE PROCEDURE THE USER FOLLOWS);
#   `git show c6331b7:gui_sweeps.py`:135-141 (the retired "lag circles" chip, whose
#   values ARE the defaults quoted below: eps {2, 3, 4, 6}, 2 replicates, nested
#   layout, lag/radius 0.25, prior amplitude A, fixed radius off. The tab now OPENS at
#   these values, so the steps state them rather than naming a preset);
#   gui_sweeps.py:660-795 (the section headers); gui_sweeps.py:1025-1030 (the
#   measurement-CSV template); mew_circle_fit.py:305-316 (fit_amplitude -- the gap is
#   REQUIRED); mew_circle_fit.py:407-418 (rows_from_csv -- rows printed at different
#   gaps must be fitted separately); mew_physics.py:535 and mew_physics.py:548;
#   gui_calibration.py:1131, :1152-1165 (the labels).
_LAG3 = Procedure(
    key="lag3",
    title="Jet lag on a plate",
    tab="Jet lag (plate)",
    calibrates=(
        "This writes the jet-lag amplitude A, and the gap it was measured at. "
        "A sets how far behind the nozzle the fiber actually lands on a flat "
        "plate, and every plate print uses it. It only holds at the gap you "
        "printed at, which is why the gap is stored beside it."
    ),
    print_protocol="circle",
    groups=(
        (SETUP, (
            "Measure the critical translation speed first, on this printer, in this "
            "session, on the “CTS (plate)” type. A wrong value there gives a wrong A.",
            "On Setup > Physics, open “Process parameters” and set "
            "“Nozzle–collector gap (mm)” to the gap you will print at.",
            "Open Calibrate and pick “Jet lag (plate)” from the “Type” menu.",
            "Read the “Conditions” line. Check that the gap it prints is the "
            "gap you just set.",
            "Leave the “Specimens” table as it opens: speed ratios of 2, 3, 4 "
            "and 6, two repeats each. A speed ratio is the print speed divided "
            "by the critical translation speed.",
            "Note the radius shown on each row. Those are the circles you will "
            "measure.",
            "Leave “Specimen size” as it opens: “Lag / radius ratio” 0.25, "
            "“Prior amplitude A” 2.8629, “Fixed radius” 0 mm.",
            "Do not raise the “Lag / radius ratio”. At 0.25 this type can still "
            "catch a critical translation speed that is 5 % low. At 0.45 it "
            "cannot.",
            "Do not add rows below a speed ratio of 2. They cost precision "
            "instead of buying it.",
            "Leave “No nesting — one separate circle per specimen” unticked. "
            "Nesting fits the whole plate onto two slides.",
            "Leave “Spacing, lead-in & base speed”, “Plate & feed limit”, "
            "“Base” and the bottom row’s display boxes at their defaults.",
            "Open “Export” and tick “Also write the measurement-CSV "
            "template”.",
        )),
        (PRINT, (
            "Press “Generate” in the bottom row.",
            "Press “Save g-code as…” and pick a folder. You get one g-code "
            "file per slide, plus one file whose "
            "name ends in _measure.csv.",
            "Print each g-code file on its own clean glass slide. At a 3.5 mm "
            "gap the defaults give two slides and eight circles.",
        )),
        (MEASURE, (
            "Calibrate the scale of whatever you image with. A 0.3 % scale "
            "error is 50 µm on a 17 mm radius and lands straight on A.",
            "Image every printed circle.",
            "Fit a radius, in mm, to the deposited fiber of each circle.",
            "Leave out the first 90° of arc after the fiber enters the circle. "
            "The CSV names that arc in its exclude_deg and entry_deg columns.",
            "Leave out the straight run-up into the circle. It sits outside the "
            "circle and reads as a radius up to 230 µm too large.",
            "On a nested plate, keep the measurement window on the ring you are "
            "measuring. Its neighbour is only 1.2 to 3.6 mm away.",
            "Write each fitted radius into that circle's measured_r_mm column "
            "in the CSV. Leave blank any circle you did not measure.",
        )),
        (APPLY, (
            "Press “Load CSV…” on this type and pick your filled-in file. It "
            "fills one row per measured circle and fills “Printed gap (mm)”.",
            "Check the rows against what you measured. Press “Add row” if one "
            "is missing.",
            "Press “Compute”. “Measurements” shows A for each specimen, the mean, the "
            "spread, and the trend across the speed ratios.",
            "Read that trend against the floor printed beside it. Inside the "
            "floor means your critical translation speed and the model agree.",
            "Go back and measure the critical translation speed again if the "
            "trend sits above the floor. It cannot tell you which of the "
            "two is wrong.",
            "Check two things if the fit refuses to run: that each deposited "
            "radius is smaller than its commanded radius, and that your image "
            "scale is right.",
            "Press “Apply” on the ribbon. It writes A and the gap together.",
            "Apply keeps it: it is saved in the physics preset “My calibration”, which loads at every start. It holds for this gap: to keep another gap's value too, save "
            "a copy under “Physics presets” on the Physics page with the gap "
            "in the name. Save the plate design under “Presets” on this type.",
            "Run the whole procedure again at any second gap you print at. A "
            "does not carry from one gap to another.",
        )),
    ),
    settings_notes={
        "conditions": (4, "A read-only line: the gap, the radii and the insets "
                          "this plate will use."),
        "specimens": (5, "One row per circle: its speed ratio, how many repeats "
                         "of it, and the radius it will be given."),
        "specimen_size": (7, "The sizing dial and the starting A the sizing "
                             "assumes. Leave both alone."),
        "no_nesting": (10, "Off packs the repeats as rings about one centre and "
                           "saves slides."),
        "spacing_lead_in": (11, "Gaps between circles, the straight run-in, the "
                                "arc sampling and the base speed."),
        "plate_feed_limit": (11, "Layout margin and an optional printer feed "
                                 "ceiling."),
        "stabilization_base": (11, "A settling pad printed before the circles, "
                                   "so the jet is steady."),
        "preview_options": (11, "What the on-screen preview draws. It changes "
                                "no g-code."),
        "export_options": (12, "The measurement-CSV template. This type reads "
                               "that file back in step 23."),
        "target_slide": (14, "Every slide is on screen at once. "
                             "“Save g-code as…” writes one file each."),
        "printed_gap": (23, "The gap the plate was printed at. A loaded CSV "
                            "fills this for you."),
        "measured_rows": (23, "One row per circle you measured, with its "
                              "commanded and deposited radius."),
        "experiment_presets": (30, "Saves this plate design so you can print it "
                                   "again unchanged."),
    },
    measure=(
        "Each deposited circle's radius, in mm, fitted to the deposited fiber "
        "over the arc the CSV leaves in: 270° of it, with the first 90° after "
        "the entry point excluded. You need to resolve a few tens of microns "
        "over a 20-35 mm field. A calibrated flatbed scan, a stitched "
        "micrograph or an optical comparator all do it, with the scale "
        "calibrated."
    ),
)


# source: the accompanying paper's mandrel jet-lag procedure: one tube, bands
#   of parallel lines at 20, 45 and 70 deg plus 0 and 90 as checks, 20 mm bands, neighbours printed back and forth so every
#   other gap is narrower; "the deviation is half the difference between a
#   measured gap and the programmed spacing"; CTS measured on the mandrel with
#   the same descending ladder. The spacing default is 1 mm, not the paper's 2;
#   the 0/90 check bands are short and fixed
#   (mew_sweeps.CHECK_BAND_LEN). mew_sweeps.generate_lag_tube (the
#   print, and why one line per band is laid twice); mew_lag_fit.py:9-30 (the
#   fit, D = (wide - narrow)/4, the spacing cancels); gui_calibration
#   `_build_fit_mandrel_lag` (the table; the fit reads the design, the gap and
#   the CTS from where each lives, so there is nothing else to type);
#   gui_tube_map (the "Where to measure" strip under the preview);
#   gui_mandrel.py:534 (the Scaffold designer's labels).
_LAG4 = Procedure(
    key="lag4",
    title="Jet lag on a mandrel",
    tab="Jet lag (mandrel)",
    calibrates=(
        "This writes the two mandrel lag radii, R_ax along the mandrel and "
        "R_cir around it, and the speed they were measured at, for mandrel "
        "prints only. This type prints one calibration tube: bands of parallel "
        "lines at several winding angles, each line printed the opposite way "
        "to its neighbor."
    ),
    print_protocol="tube",
    groups=(
        (SETUP, (
            "Open the Scaffold designer, switch the collector to the "
            "mandrel, and set “Mandrel diameter (mm)” to the mandrel you will "
            "print on.",
            "On Setup > Physics, open “Process parameters” and set "
            "“Nozzle–collector gap (mm)” to the nozzle-to-mandrel gap you "
            "will print at.",
            "Measure the CTS on the mandrel first, on the “CTS (mandrel)” type. "
            "If you skip it, the flat CTS is used and the two radii absorb "
            "the difference.",
            "On this type, set “Print speed (mm/min)” to the speed you print "
            "tubes at. The radii hold at that speed.",
            "Check the design under “Tube design”. By default it prints 13 mm "
            "bands at 20°, 45° and 70° and a short 90° check band, lines 1 mm "
            "apart. To change the bands, edit “Winding angles (°)”.",
        )),
        (PRINT, (
            "Press “Generate”, then “Save g-code as…”.",
            "Print it on the mandrel. The bands come in the order of "
            "“Winding angles (°)”, starting at the near end.",
        )),
        (MEASURE, (
            "Calibrate the scale of whatever you measure with.",
            "Image the printed fibers. In the middle of each band, measure "
            "across the lines: one wide gap and one narrow gap between "
            "neighboring lines, in mm. ImageJ is one way to do this.",
            "Measure the 90° check band the same way. Its gaps should stay "
            "close to the programmed spacing, without the wide and narrow "
            "pattern of the other bands.",
        )),
        (APPLY, (
            "Type each band's wide and narrow gaps beside its angle in the "
            "table.",
            "Press “Compute”. It shows the two lag radii, and how far each "
            "wide and narrow pair averages from its programmed spacing. More "
            "than 2 % off means the image scale is likely wrong, and the "
            "radii carry about the same error.",
            "Press “Apply” on the ribbon. It writes the two radii and the "
            "speed they were measured at, for mandrel prints only. Refit them "
            "if you change the print speed a lot.",
            "Apply keeps it: it is saved in the physics preset “My calibration”, which loads at every start. Save this tube's design under “Presets” to print it again.",
        )),
    ),
    settings_notes={
        "tube_bands": (5, "The tube: winding angles, line spacing, lines per "
                          "band, band length and print speed."),
        "gap_rows": (11, "One row per band: its angle, then its wide and "
                         "narrow gap."),
        "preview_options": (9, "What the preview draws, including where to "
                               "measure. It changes no g-code."),
        "experiment_presets": (14, "Saves this tube's design so you can "
                                   "print it again unchanged."),
        "export_options": (6, "The measurement-CSV template for this tube: "
                              "one row per band."),
    },
    shared_cards=MANDREL_CARDS,
    measure=(
        "One wide and one narrow gap, in mm, between neighbouring lines at "
        "each winding angle, measured across the lines in the middle of the "
        "band."
    ),
)


# source: the sag calibration procedure drafted for the accompanying paper
#   (the print and the span list -- parallel walls with two bridging
#   fibers across them, and that span is the wall CENTERLINE pitch rather than the
#   clear gap); the operator steps below are that procedure's, taken
#   verbatim; the physics is the paper's sag relaxation (pin a
#   support on each obstacle the free droop meets, re-solve);
#   mew_sweeps.generate_sag (the generator this tab's Generate runs);
#   gui_calibration `_build_fit_sag` (the fit grid); mew_calibration
#   `fit_sag_ladder`; mew_physics.py:269 (`sag_coeff`, the magnitude c);
#   mew_physics.py:270 (`sag_span_exp`, the exponent p, default 2.0).
#
#   THE LADDER FIT: log(droop) against log(span) over the whole
#   row, whose slope is `sag_span_exp` and whose intercept is `sag_coeff`
#   (mew_calibration.fit_sag_ladder; the card is gui_calibration `_build_fit_sag`).
#   What that build settled, and why the card looks the way it does:
#     * NO "Fiber \xd8 (mm)" and NO "Temperature (\xb0C)" on the card. One ladder is
#       one fiber at one temperature, so both are constant across every point in the
#       regression and fold into the intercept -- mew_physics.py:1038-1041 says the
#       same of the diameter from the law's side, and at the shipped sag_diam_exp = 0
#       and sag_thermal_visc_exp = 0 both factors are exactly 1. Neither is MEASURED
#       anywhere in these steps either, so a field for them would be a number the
#       operator has to invent.
#     * The droop is fitted as handed over. Step 10 reads the bridging fiber's
#       centerline at the wall and again at the midspan, which is what the solver
#       subtracts: rest heights are CENTERLINE heights (gcode_to_fibers.py:3225) and
#       the droop comes off the chord between two of them (:5961-5965). Nothing to
#       correct.
#     * `c` is mm^(1-p) once p is fitted, which is what the result strip says; the
#       Physics page's field still declares "1/mm", true only at the shipped p = 2.
#     * p is REFUSED on fewer than 3 usable spans or less than a 3x span range
#       (mew_calibration.SAG_MIN_SPANS / SAG_MIN_SPAN_RATIO): the Physics page's
#       exponent is kept, the magnitude alone is fitted, the strip says which
#       threshold failed and Apply leaves `sag_span_exp` alone. Step 15 is that.
#     * Readings of one span -- the mirrored pass, the second bridging fiber -- are
#       cells on that span's row and collapse to one point by GEOMETRIC mean, the
#       log-space collapse the log-log regression asks for. Both are optional.
#   STILL OWED: `sag_coeff` is only valid at the ladder's fiber diameter, so
#   `sag_diam_ref` (mew_physics.py:272) must be set to it before `sag_diam_exp` is
#   turned on. This procedure never measures a fiber diameter, so the card does not
#   write it and must not pretend to: that pivot belongs to the mixed-diameter sweep
#   that would fit `sag_diam_exp` in the first place (SAG_EXPONENT_EXPERIMENT.md).
_SAG = Procedure(
    key="sag",
    title="Sagging",
    tab="Sagging",
    calibrates=(
        "This writes the fiber sag law: how far a fiber sags when it bridges "
        "a pore instead of resting on something, and how fast that sag grows "
        "as the pore gets wider. It sets how tall and how open the printed "
        "scaffold really is. The values that ship were set by eye and by "
        "theory, not fitted to measured sag. This type prints its own test "
        "piece: a row of thin walls whose spacing steps wider along the plate, "
        "with two fibers laid across the whole row so they sag into every gap "
        "at once."
    ),
    print_protocol="sag",
    groups=(
        (SETUP, (
            "Open Calibrate and pick “Sagging” from the “Type” menu.",
            "On the Physics page, set the process settings and critical "
            "translation speed to the values you print scaffolds at. Set print "
            "speed under the sag calibration settings so you are printing at "
            "your desired speed ratio. This calibration holds only at those "
            "values.",
            "Check the span list under “Spans (mm)”.",
            "Set “Wall height (layers)”. Taller walls let wider spans "
            "sag without touching the collector.",
        )),
        (PRINT, (
            "Press “Generate”, then “Save g-code as…”.",
            "Print it on a plate you can lift off and stand on its edge under a "
            "microscope.",
            "You may optionally record the speed, nozzle temperature, voltage, "
            "nozzle-collector distance and pressure for future reference.",
        )),
        (MEASURE, (
            "Image the spans however is convenient. A microscope or a side-on "
            "high-zoom camera both work. Backlight the plate so the fiber reads "
            "bright against a dark background.",
            "Check that the span walls sit on one horizontal line to ensure the "
            "image isn't angled.",
            "Measure the midspan depth for the bridging fiber over each span. "
            "One method is to use ImageJ and set the scale as the distance "
            "between the two span walls' midpoints. Measure at the centerline "
            "of the bridging fiber, one point at the span wall, and one point "
            "at the midspan.",
            "If a fiber has bottomed out on the collector it should not be "
            "counted and the next calibration print should use a higher wall "
            "fiber count.",
            "Optionally, turn the plate around and repeat for the second "
            "bridging fiber.",
        )),
        (APPLY, (
            "Enter measured values into “Measurements”.",
            "Press “Compute”. “Measurements” shows the span exponent, the "
            "magnitude coefficient, and how well the power law fits.",
            "If it could not fit the exponent, you have fewer than three usable "
            "spans. Reprint with taller walls, or keep the exponent from the "
            "Physics page and fit the magnitude alone.",
            "Press “Apply” on the ribbon.",
            "Apply keeps it: it is saved in the physics preset “My calibration”, which loads at every start.",
        )),
    ),
    settings_notes={
        "sag_ladder": (3, "The spans, the walls that make them, the one feed "
                          "the row is printed at, and the two fibers laid "
                          "across it."),
        "fit_inputs": (13, "Every span you measured and the sag you measured "
                           "over it."),
        "plate_feed_limit": (5, "Layout margin and an optional printer feed "
                                "ceiling. The defaults are fine here."),
        "stabilization_base": (5, "A settling pad printed before the walls, so "
                                  "the jet is steady."),
        "preview_options": (5, "What the on-screen preview draws. It changes "
                               "no g-code."),
        "export_options": (5, "The measurement-CSV template for this ladder, "
                              "one row per span per fiber."),
        "target_slide": (5, "This ladder is a single slide."),
        "experiment_presets": (5, "Saves this ladder design so you can run it "
                                  "again unchanged."),
    },
    measure=(
        "The midspan depth of the bridging fiber over each span, in mm, read "
        "side-on at the fiber's CENTERLINE: one point where it crosses the span "
        "wall and one at the midspan. The span it belongs to comes from the "
        "span list, so no scale bar is needed. A span whose fiber has bottomed "
        "out on the collector is not counted."
    ),
)


# source: the accompanying paper's fusion model and its calibration -- fusion depends on
#   layer time, which the law does not carry, so phi is fitted on the scaffold
#   itself against the height the same stacks reach unfused, and holds for repeats
#   of that design only. No calibration print (print_protocol=None).
#   mew_calibration.fit_fusion_rows (the height law h = d*[1 + (N-1)*(1 - phi)],
#   junction and wall rows fitted separately -- the two phis are not
#   interchangeable); gui_calibration._build_fit_fusion (the field labels);
#   mew_physics `fusion_overlap` / `fusion_wall_overlap`.
#   The measurement steps are the operator procedure as written for this tab,
#   split only where a step ran over 40 words.
_FUSION = Procedure(
    key="fusion",
    title="Fusion",
    tab="Fusion",
    calibrates=(
        "This writes the fusion sink: how far a fiber sinks into the "
        "one it lands on, at crossovers and optionally along walls. It is why a "
        "real stack is shorter than the layer count times the fiber diameter. "
        "Fit it on the scaffold you want to show, because fusion changes with "
        "the time between layers."
    ),
    print_protocol=None,
    groups=(
        (SETUP, (
            "Open Calibrate and pick “Fusion” from the “Type” menu.",
        )),
        (PRINT, (
            "Print the scaffold you want to model, from the Scaffold designer "
            "page or your own g-code.",
            "The sink you fit holds only for this design at the settings you "
            "print it at. A different pore size, footprint or speed is a new "
            "calibration.",
            "You may optionally record the speed, nozzle temperature, voltage, "
            "nozzle-collector distance and pressure for future reference.",
        )),
        (MEASURE, (
            "Measure the fiber diameter in {fiber_d} on this same print. One method is "
            "to use ImageJ on microscope images of the fibers, calibrated on a "
            "stage micrometer.",
            "Image the scaffold side-on so you can see the fiber walls and "
            "junctions clearly. You may have to cut the scaffold in half to get "
            "a clear view of the interior.",
            "Measure the height of at least 5 junctions, the peaks where fibers "
            "cross, from the base to the top of the peak.",
            "One method is to use ImageJ and set the scale as the distance "
            "between two neighboring junction centers, which is your pore "
            "spacing.",
            "Optionally, measure the height of at least 5 wall positions midway "
            "between junctions. Only measure where the upper fibers visibly "
            "rest on the ones below. Where a fiber still sags, the height "
            "reads sag, not fusion.",
            "Count the fibers stacked at each feature. A junction in a 0/90 "
            "scaffold holds every layer. A wall holds only the layers that run "
            "along it, which is half the layers in a 0/90 scaffold.",
        )),
        (APPLY, (
            "Type the fiber diameter in {fiber_d} into “Measured fiber Ø”. Type each "
            "junction's fiber count and height in mm into a row of “Layers N” "
            "and “Height (mm)”. Press “Add junction row” for more rows.",
            "Press “Compute”. “Measurements” shows the junction sink φ, a fraction "
            "of the fiber diameter between 0 and 1.",
            "Press “Apply” on the ribbon and pick “Apply junction sink”. It fills "
            "in “Junction sink φ” on the Physics page’s “Fusion” card.",
            "If you measured walls, enter each wall's fiber count and height in "
            "the wall table, press “Compute”, then pick “Apply straight-span sink” under “Apply”. "
            "It fills in “Straight-span sink φ” on the Physics page’s “Fusion” "
            "card.",
            "Apply keeps it: it is saved in the physics preset “My calibration”, which loads at every start. The sink holds only for this design: to keep it beside "
            "others, save a copy under “Physics presets” on the Physics page, "
            "named after the scaffold.",
        )),
    ),
    settings_notes={
        "fit_inputs": (11, "The fiber diameter, then the fiber count and height "
                           "of each junction and wall you measured."),
    },
    measure=(
        "The fiber diameter in {fiber_d} on the scaffold you printed. Then, imaged "
        "side-on, the height in mm and fiber count of at least 5 junctions and, "
        "optionally, at least 5 wall positions where the upper fibers rest on "
        "the ones below."
    ),
)


#: The tabs, in the order the Calibration page renders them. Insertion order IS
#: tab order.
PROCEDURES: dict[str, Procedure] = {
    p.key: p for p in (
        _CTS,
        _CTS4,
        _VISCOSITY,
        _ACTIVATION_ENERGY,
        _LAG3,
        _LAG4,
        _SAG,
        _FUSION,
    )
}
