#!/usr/bin/env python3
"""viewer_sidebar_spec.py -- MODERN-UI ONLY: the layout tokens shared by the
TWO renderers of a viewer's control sidebar, so the minimized and maximized
states stay visually identical.

The same viewer (locked sim / live sim / STL) draws its left sidebar two ways:

  * MAXIMIZED -- native fullscreen + the classic windowed launch -- is VTK
    actors, drawn in the viewer CHILD by gui_viewer_shell.ViewerChrome.
  * MINIMIZED -- embedded in a modern-shell page -- is real ttk widgets, drawn
    GUI-side by gui_embed.ViewerPanel._build_rail.

Both are fed the SAME control spec (viewer_embed.EmbedChrome: sections, buttons,
readout, footers), so their CONTENT already matches; only the pixel rendering
diverges. The two live in different processes/toolkits and cannot import each
other -- gui_viewer_shell imports vtk at module scope, and gui_embed must stay
vtk-free (it runs in the GUI process). This module is the neutral ground: it has
ZERO imports (no vtk, no tkinter), so both sides can pull the shared numbers from
one place and the width can never drift apart again.

What genuinely SHARES cleanly is a pixel measure (a width, a row height); font
POINT sizes do not translate 1:1 between VTK's SetFontSize and a ttk named font,
so those stay per-renderer (the parity test in test_viewer_embed.py guards the
button LOOK instead).

The rail's LAYOUT DATA lives here too -- which readout rows the table has, which
controls go on which tab, how the key legend splits into pairs -- so the ttk rail
and the fullscreen VTK rail (gui_viewer_shell.RailChrome) group and order the
same spec identically. Keep this module free of vtk and tkinter (stdlib only).
"""

import re

# The FLOOR (minimum) width both renderers start from. 240 px is the width the
# minimized ttk rail was validated at: it fits the player's widest readout line,
# "v(act/set) NN.N/NN.N mm/s xN.NN", at the DEFAULT (standard-DPI) font.
# It is a FLOOR, not a hard width: each renderer AUTOFITS upward from here to its
# own measured content (the ttk rail measures its widgets, the VTK ViewerChrome
# its text actors), so a display-scaled machine -- where the ttk named fonts grow
# in pixels but VTK's SetFontSize does not -- never clips a label. At standard DPI
# both measure ~240 and stay here, so the two states are the same width in the
# common case and diverge only when one toolkit's fonts scale and the other's do
# not, which is exactly when sizing each to its own content prevents the clip.
SIDEBAR_W = 240

# The button surface height. The VTK card draws this height directly; the ttk
# card button derives its vertical padding to land on the same ~24 px surface, so
# a button reads the same size in both states.
BUTTON_ROW_H = 24

# The rail groups its sections into tabs. A section not named here is its own
# tab under its own name (the micrograph sliders arrive as "Micrograph").
TAB_OF_SECTION = {"Scaffold": "Scene", "Printer": "Scene",
                  "Deviation": "Simulation"}
# transport roles, in bar order (see viewer_embed.EmbedChrome.button's `role`)
TRANSPORT_ROLES = ("restart", "play", "slower", "faster")
# what an empty readout value reads before a viewer fills it
EMPTY_VALUE = "—"

# The readout table's rows, drawn at rail build so the table is there before a
# viewer sends anything. A row the child does not send (no physics: the Process
# rows) reads EMPTY_VALUE. `sample` is the widest value the row is expected to
# show; each rail's width is measured once with these in place.
READOUT_ROWS = (
    {"g": "Print", "k": "Time", "sample": "00m 00s / 00m 00s"},
    {"g": "Print", "k": "Line", "sample": "000000"},
    {"g": "Print", "k": "Move", "sample": "G1 X000.0 Y-000.0"},
    # the mandrel form is the wider one: X, Z and a many-turn A, read as whole
    # turns plus the angle within the turn (gcode_player.format_rotary)
    {"g": "Print", "k": "Position",
     "sample": "X -000.0 Z 00.00 A 000 turns + 000.0°"},
    {"g": "Process", "k": "Voltage", "knob": "voltage", "sample": "00.0 kV"},
    {"g": "Process", "k": "Pressure", "knob": "pressure", "sample": "00.0 psi"},
    {"g": "Process", "k": "Temperature", "knob": "temperature", "sample": "000 °C"},
    {"g": "Process", "k": "Speed", "knob": "speed",
     "sample": "000 / 000 mm/min"},
    {"g": "", "k": "Diameter", "sample": "000 µm"},
    {"g": "", "k": "CTS", "sample": "000 mm/min · v/CTS 0.00"},
    {"g": "", "k": "Jet lag", "sample": "00°"},
)


# The g-code listing (the Simulation sidebar's "G-code listing" switch): a window
# of the file with the current line lit, drawn IN PLACE OF these readout rows by
# both renderers. The child sends LISTING_ROWS lines (LISTING_ROWS // 2 either
# side of the current one); a line is cut to LISTING_COLS characters, its
# number included.
LISTING_REPLACES = (("Print", "Line"), ("Print", "Move"))
LISTING_ROWS = 11
LISTING_COLS = 30


def readout_knobs(controls):
    """The knob names ("voltage", ...) that have -/+ buttons in this spec, i.e.
    the readout rows that get steppers beside their value."""
    return {c["role"].split(":")[1] for c in controls
            if (c.get("role") or "").startswith("knob:")
            and c["role"].count(":") == 2}


def rail_tabs(controls, has_rows):
    """The rail's tabs: {tab name: [control spec, ...]} in first-seen order.

    Transport roles are drawn on the transport bar, not a tab. Knob roles are
    drawn beside the readout rows when there IS a table (has_rows); otherwise
    they are ordinary buttons on a "Live edit" tab."""
    tabs = {}
    for spec in controls:
        role = spec.get("role") or ""
        if role in TRANSPORT_ROLES or (has_rows and role.startswith("knob:")):
            continue
        if role.startswith("knob:"):
            section = "Live edit"
        else:
            section = spec.get("section") or "Controls"
        tabs.setdefault(TAB_OF_SECTION.get(section, section), []).append(spec)
    return tabs


def legend_rows(footers):
    """The footer legend ("Keys" / "Mouse" blocks of 'key  action' lines) as
    [(section, [(key, action), ...])]. Pairs are split on runs of 2+ spaces, so
    a line holding two pairs ("< >  step line   - +  speed") yields both; a line
    with no pair ("Live from the printer") yields (line, "")."""
    out = []
    for f in footers or []:
        if f.get("section"):
            out.append((f["section"], []))
        text = f.get("text") or ""
        if not text:
            continue
        if not out:
            out.append(("", []))
        for line in text.splitlines():
            toks = [t for t in re.split(r"\s{2,}", line.strip()) if t]
            if len(toks) < 2:
                if toks:
                    out[-1][1].append((toks[0], ""))
                continue
            for i in range(0, len(toks) - 1, 2):
                out[-1][1].append((toks[i], toks[i + 1]))
            if len(toks) % 2:
                out[-1][1].append((toks[-1], ""))
    return [(sec, rows) for sec, rows in out if rows]
