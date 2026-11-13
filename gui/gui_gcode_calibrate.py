#!/usr/bin/env python3
"""gui_gcode_calibrate.py -- MODERN-UI ONLY: Calibrate, in the G-code CAD window.

Calibrate is Design with two differences: the ribbon's Parameters opens the
calibration's own form, and the print starts from a calibration's default
design (the CTS ladder, the temperature ladder, the lag circles...) picked
with Type. Everything else -- the tree, the picture, the bottom row, Add and
Change selection, Inspect, File -- is Design's and works the same way. It is
a MODE of the CAD window (`tab_gcode`), entered through
`_nav_select("calibration")` and left through `_nav_select("gcode")`.

THE ENGINE IS UNCHANGED. `gui_sweeps` still generates the prints and
`gui_calibration` owns the forms and fits. This module is their page:

  * the DOCUMENT. Every slide of the print is in ONE document, laid side by
    side as the picture draws them (`calibration_layout`): slide 1 where the
    engine put it, each next one shifted in X/Y to its place in the grid. It is
    built as a Design's linked document is: the layout is the base, and the
    Export card's options spell it. The Design's document, undo stack and
    hidden rows wait in `_cal_stash` and come back untouched
    (`_cal_unload_doc`). It reuses the FILE source, because every branch that
    follows the designer already skips a file. Edits, undo steps and hidden
    rows are kept per type in `_cal_docs` and ride onto a rebuilt print
    through rebase.
  * SAVE writes one file per slide, each at its own origin: an unedited slide
    is the engine's bytes as they always were, an edited one is its lines of
    the document shifted back (`_sw_slide_text`). GENERATE builds the whole
    layout (`_sw_selected_slide_gcode`), so the Simulation shows what the
    picture shows.
  * the TREE. The line map is read off each slide's own moves, never the
    generators (their output does not change for it): a part per specimen
    value (repeats of one value are its lines), the base with its layers, the
    slide's end. A Slide row is added over each slide's parts
    (`_gce_anatomy_rows`); it selects, moves and hides them as one.
  * the PICTURE is Design's, built from Design's options: the same 3-D tubes,
    jet lag (with the coiling wave below CTS), sagging and bottom row. Added on
    top: one label per specimen value, and a plate under every slide past the
    first (`_cal_draw_extras`). For colouring only, a slide is one scaffold
    (`_view_layer_table`): its passes share its colour, kept apart from
    Design's own scaffold colours. A tube is drawn by `build_rod_preview`, because
    its rod is described by the calibration, not by a designed tube.
  * the FORMS. `CardBox` stands where the page's `ModuleGrid` stood, so
    `gui_calibration`'s builders run once and unchanged, and the ribbon shows
    one card at a time in the floating box. Fit forms are built ONCE because the
    numbers an operator typed must survive a switch of type.
  * the RIBBON. Collector, Design's Create, Pattern and Modify hide; Add,
    Change selection, Inspect and File stay, as Design's; Type, then Create
    (Stabilization scaffold) and Modify (Parameters) with Design's icons,
    then Instructions, Measurements and Apply show. Compute stays in Measurements.
    Apply is on the ribbon, and presses the card's own button so a fit's
    refusals have one home.

Fusion has no print. It shows the current Design, its real document, with no
swap, so Generate and Save there are Design's (`_cal_print_on` is False)."""

import math
import os
import tkinter as tk
from tkinter import ttk

import numpy as np

import gui_tips

_DRAW = ("G1", "G01", "G2", "G02", "G3", "G03")
_TRAVEL = ("G0", "G00")
#: the word after a specimen's badge in the tree, by print
_VALUE_UNIT = {"cts": " mm/min", "cts4": " mm/min"}
#: the parts whose layers are built up, and so get Layer rows
_BUILT_UP = ("Base", "Walls")


class _Moves:
    """One slide's motion lines, read once: `rows[k]` is (line, cmd, x, y, z)
    after that line runs, and `runs` the printing runs, each (first travel
    line before it, first line, last line, centre, extent)."""

    def __init__(self, eng, text):
        self.n = 0
        self.rows = []
        self.runs = []
        x = y = z = 0.0
        travel = None
        run = None
        for i, line in enumerate(text.splitlines()):
            self.n = i + 1
            cmd, words = eng.parse_words(line)
            if cmd not in _DRAW and cmd not in _TRAVEL:
                continue
            try:
                x = float(words.get("X", x))
                y = float(words.get("Y", y))
                z = float(words.get("Z", z))
            except ValueError:
                continue
            self.rows.append((i, cmd, x, y, z))
            if cmd in _TRAVEL:
                run = None
                if travel is None:
                    travel = i
                continue
            if run is None:
                run = dict(travel=i if travel is None else travel, first=i,
                           last=i, lo=[x, y], hi=[x, y], start=None)
                self.runs.append(run)
                travel = None
            run["last"] = i
            run["lo"] = [min(run["lo"][0], x), min(run["lo"][1], y)]
            run["hi"] = [max(run["hi"][0], x), max(run["hi"][1], y)]


def _flat_starts(mv, specimens):
    """The line each plate specimen starts on: the printing run, in order,
    that sits on it. A run off every specimen (the base, a connector) joins
    the row before it. A specimen no run sits on ends the matching, and what
    is left stays with the last row found: the tree never claims a line it
    cannot place.

    Lines joined by linking loops (the CTS ladder's) are one unbroken run, so
    the straight segment that IS each line is matched instead
    (`_segment_starts`) whenever that places more specimens."""
    runs = _run_starts(mv, specimens)
    segs = _segment_starts(mv, specimens)
    return segs if len(segs) > len(runs) else runs


def _segment_starts(mv, specimens):
    """The line each straight-line specimen starts on, read segment by
    segment: the move whose middle is the specimen's centre and which is at
    least half its length. Reached by a travel, it starts at the first travel
    before it, as a run does; reached by a loop from the line before, at its
    own line, so the loop is the row before's."""
    out = []
    rows = mv.rows
    k = 1
    for s in specimens:
        cx, cy = s.center
        size = float(s.size)
        tol = max(0.02, 0.01 * size)
        found = None
        while k < len(rows):
            i = k
            k += 1
            _ln, cmd, x, y, _z = rows[i]
            if cmd not in _DRAW:
                continue
            px, py = rows[i - 1][2], rows[i - 1][3]
            if math.hypot(x - px, y - py) < 0.5 * size:
                continue
            if abs(0.5 * (x + px) - cx) <= tol \
                    and abs(0.5 * (y + py) - cy) <= tol:
                found = i
                break
        if found is None:
            break
        j = found - 1
        if rows[j][1] in _TRAVEL:
            while j > 0 and rows[j - 1][1] in _TRAVEL:
                j -= 1
            out.append(rows[j][0])
        else:
            out.append(rows[found][0])
    return out


def _run_starts(mv, specimens):
    """`_flat_starts` over whole printing runs."""
    out = []
    k = 0
    for s in specimens:
        cx, cy = s.center
        tol = max(0.02, 0.5 * float(s.size))
        while k < len(mv.runs):
            r = mv.runs[k]
            k += 1
            mx, my = (0.5 * (r["lo"][0] + r["hi"][0]),
                      0.5 * (r["lo"][1] + r["hi"][1]))
            if abs(mx - cx) <= tol and abs(my - cy) <= tol:
                out.append(r["travel"])
                break
        else:
            break
    return out


def _ring_starts(mv, specimens):
    """The line each circle starts on. Rings nest round one centre and are
    laid in one unbroken run, so a run says nothing: a ring starts where the
    head first sits on its radius, 5% of it either way."""
    out = []
    k = 0
    for s in specimens:
        cx, cy = s.center
        r = 0.5 * float(s.size)
        while k < len(mv.rows):
            i, cmd, x, y, _z = mv.rows[k]
            k += 1
            if cmd in _DRAW and abs(np.hypot(x - cx, y - cy) - r) <= 0.05 * r:
                out.append(i)
                break
        else:
            break
    return out


def _rotary_starts(mv, specimens):
    """The line each band or rung of a tube starts on: X is the distance
    along the rod, so a specimen starts where the head reaches its interval."""
    out = []
    k = 0
    for s in specimens:
        lo = float(s.center[0]) - 0.5 * float(s.size)
        hi = float(s.center[0]) + 0.5 * float(s.size)
        while k < len(mv.rows):
            i, cmd, x, _y, _z = mv.rows[k]
            k += 1
            if cmd in _DRAW and lo - 1e-6 <= x <= hi + 1e-6:
                out.append(i)
                break
        else:
            break
    return out


def _sag_groups(mv, has_base):
    """(start line, name) of the sag ladder's parts. Its specimens are GAPS,
    which share walls, so no line belongs to one gap: the parts are the base,
    the walls (they start where Z falls back to the slide) and the fibres laid
    over them, which the generator lays last, at the top Z."""
    top = max(r[4] for r in mv.rows)
    above = [r for r in mv.rows if r[4] == top]
    fibres = [r for r in above if r[1] in _DRAW]
    out = []
    z = mv.rows[0][4]
    fall = None
    for r in mv.rows:
        if r[4] < z:
            fall = r[0]
            break
        z = r[4]
    if has_base and fall is not None:
        out.append((fall, "Walls"))
    else:
        out.append((mv.rows[0][0], "Walls"))
    first = above[0][0] if above else None
    for j, row in enumerate(fibres):
        start = first if j == 0 else row[0] - 1
        out.append((start, "Fiber " + "ABCDEFGH"[min(j, 7)]))
    return out


def _slide_parts(mv, specimens, kind, has_base):
    """One slide's parts, in print order: ``[(name, [start line, ...])]``.
    A specimen value printed several times over (the CTS ladder's lines at
    one speed) is ONE part, each repeat a line of it. The lead (the base, or
    a tube's lead-in) comes first."""
    rotary = kind in ("tube", "cts4")
    if kind == "sag":
        starts = list(_sag_groups(mv, has_base))
    else:
        find = (_rotary_starts if rotary else
                _ring_starts if kind == "circle" else _flat_starts)
        unit = _VALUE_UNIT.get(kind, "")
        starts = [(a, s.value_text + unit)
                  for a, s in zip(find(mv, specimens), specimens)]
    head = mv.rows[0][0]
    if not starts or starts[0][0] > head:
        starts.insert(0, (head, "Base" if has_base and not rotary
                          else "Lead-in"))
    parts = []
    for a, name in starts:
        if parts and parts[-1][0] == name and name not in _BUILT_UP:
            parts[-1][1].append(a)
        else:
            parts.append((name, [a]))
    return parts


def _slide_summary(specimens, unit):
    """What a slide holds, for its row: its one value, or its first and
    last."""
    seen = list(dict.fromkeys(s.value_text for s in specimens))
    if not seen:
        return ""
    if len(seen) == 1:
        return seen[0] + unit
    return "%s – %s%s" % (seen[0], seen[-1], unit)


def tile_offsets(n, w, h):
    """(dx, dy) of each of `n` slides laid in a near-square grid, reading
    left to right, then down, a gap of 14% of the plate between them."""
    cols = max(1, int(math.ceil(math.sqrt(n))))
    gap = 0.14 * max(w, h)
    return [((i % cols) * (w + gap), -(i // cols) * (h + gap))
            for i in range(n)]


def shift_text(eng, text, dx, dy, dz=0.0):
    """`text` with every move's X, Y and Z words shifted by (dx, dy, dz): a
    slide put at its place in the layout and on the g-code origin, or
    (negated) back at its own origin. Arc centres (I, J) are relative to the
    move, so they stay."""
    if not (dx or dy or dz):
        return text
    out = []
    for line in text.splitlines(keepends=True):
        cmd, words = eng.parse_words(line)
        if cmd in _DRAW or cmd in _TRAVEL:
            vals = {}
            try:
                if dx and "X" in words:
                    vals["X"] = float(words["X"]) + dx
                if dy and "Y" in words:
                    vals["Y"] = float(words["Y"]) + dy
                if dz and "Z" in words:
                    vals["Z"] = float(words["Z"]) + dz
            except ValueError:
                vals = {}
            if vals:
                line = eng._set_words(line, vals)
        out.append(line)
    return "".join(out)


class Layout:
    """Every slide of a calibration print in one text (`text`), with its line
    map (`lm`). `slides[k]` is slide k's dict: `first` / `last` its lines
    (0-based, inclusive), `groups` its parts' group indices, `offset` where
    it sits, `label`. `names[g]` is group g's row and `layer_names[(g, L)]`
    a line's; `raws` the engine's own bytes, slide by slide."""

    def __init__(self, text, lm, slides, names, layer_names, raws):
        self.text = text
        self.lm = lm
        self.slides = slides
        self.names = names
        self.layer_names = layer_names
        self.raws = raws

    def slide_of_group(self, g):
        for k, s in enumerate(self.slides):
            if g in s["groups"]:
                return k
        return None


def calibration_layout(eng, raws, specimens, kind, has_base, offsets,
                       rebase=(0.0, 0.0, 0.0)):
    """The `Layout` of a print whose slides are `raws` (the engine's bytes,
    one per slide), `specimens[k]` slide k's, each shifted by `offsets[k]`
    and then all of them by `rebase`, the g-code origin's re-base. The
    offsets stay the picture's (labels, extra plates); `rebase` is undone
    by the picture's own origin shift, as a design's is."""
    rx, ry, rz = rebase
    unit = _VALUE_UNIT.get(kind, "")
    texts, marks, groups, names, layer_names, slides = [], [], [], [], {}, []
    at = 0
    for k, raw in enumerate(raws):
        dx, dy = offsets[k]
        text = shift_text(eng, raw, dx + rx, dy + ry, rz)
        if text and not text.endswith(("\n", "\r")) and k + 1 < len(raws):
            text += "\n"
        texts.append(text)
        mv = _Moves(eng, raw)
        n = len(text.splitlines())
        mine = []
        if not mv.rows:
            parts, exit_at = [("Slide %d" % (k + 1), [0])], None
        else:
            parts = _slide_parts(mv, specimens[k], kind, has_base)
            draws = [r[0] for r in mv.rows if r[1] in _DRAW]
            exit_at = next((r[0] for r in mv.rows if r[0] > draws[-1]),
                           None) if draws else None
        if k:
            # a later slide's opening lines (its units and mode) are its first
            # part's: the document has one Header, the first slide's
            parts[0][1][0] = 0
        if exit_at is not None:
            parts.append(("End", [exit_at]))
        bounds = [p[1][0] for p in parts[1:]] + [n]
        for (name, starts), b in zip(parts, bounds):
            g = len(groups)
            mine.append(g)
            names.append(name)
            groups.append({"name": "stab" if name == "Base" else name,
                           "layers": 0})
            marks.append({"line": at + starts[0], "kind": "group",
                          "group": g})
            if name in _BUILT_UP:
                rows = [r for r in mv.rows if starts[0] <= r[0] < b]
                zs = [r[4] for r in rows]
                if len(set(zs)) > 1:
                    for j, r in enumerate(rows):
                        if j and zs[j] == zs[j - 1]:
                            continue
                        if j + 1 < len(rows) and zs[j + 1] != zs[j]:
                            continue            # one line at a Z is a hop
                        L = groups[g]["layers"]
                        marks.append({"line": at + r[0], "kind": "layer",
                                      "layer": L})
                        groups[g]["layers"] += 1
            elif len(starts) > 1:
                for L, a in enumerate(starts):
                    marks.append({"line": at + a, "kind": "layer",
                                  "layer": L})
                    layer_names[(g, L)] = "Line %d" % (L + 1)
                groups[g]["layers"] = len(starts)
        slides.append(dict(first=at, last=at + n - 1, groups=mine,
                           offset=(dx, dy),
                           label="Slide %d" % (k + 1),
                           summary=_slide_summary(specimens[k], unit)))
        at += n
    text = "".join(texts)
    lay = Layout(text, eng.LineMap(marks, at, groups), slides, names,
                 layer_names, list(raws))
    lay.rebase = (rx, ry, rz)
    return lay


def build_rod_preview(gcode_path, text, radius, surface_z, rod, fiber_diameter,
                      physics=None, lift=0.0, lay_travels=False):
    """A calibration tube for the page's picture: `_sw_extract_tube`'s reading
    (its rotary frame, its relay, no drape, `lift` raising the fibre onto the
    bare rod), carrying the print times and the keys the page reads off
    `build_mandrel_preview`, which cannot draw it: that one resolves the rod
    from a tube DESIGN, and this print has none. `lay_travels` lays the travels
    into the fibre, as `build_mandrel_preview` always does."""
    import gcode_to_fibers
    import gui_preview
    import mew_frame
    with open(gcode_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    frame = mew_frame.CylinderFrame(float(radius),
                                    surface_z=float(surface_z))
    unrolled, ptimes, travels = gcode_to_fibers.parse_gcode(
        gcode_path, mode="g1", with_times=True, with_travels=True, frame=frame)
    commanded = [mew_frame.wrap_polyline(frame, c) for c in
                 gui_preview.commanded_paths(gcode_to_fibers, unrolled, ptimes,
                                             travels)]
    laid = unrolled
    if physics is not None:
        try:
            import mew_deposition
            transform = mew_deposition.make_transform(
                physics["model"], pressure=physics["pressure"],
                voltage=physics["voltage"],
                temperature=physics["temperature"],
                zeta=physics.get("zeta", 1.0), coil=physics.get("coil", False),
                coil_wave_amp=physics.get("coil_wave_amp", 0.0),
                coil_wave_len=physics.get("coil_wave_len", 0.3),
                max_accel=physics.get("max_accel") or None,
                square_corner_velocity=physics.get(
                    "square_corner_velocity", 6.0),
                rotary_accel=physics.get("rotary_accel", 0.0),
                corner_chord=mew_deposition.auto_corner_grid(
                    float(physics.get("fiber_diameter") or fiber_diameter)),
                simulate_travel=lay_travels)
            laid, ptimes = (
                transform(unrolled, ptimes, travels, frame=frame) if lay_travels
                else transform(unrolled, ptimes, frame=frame))
        except Exception:  # noqa: BLE001 -- the commanded path, as the engine
            laid = unrolled
    if lay_travels and laid is unrolled:
        laid, ptimes = gcode_to_fibers.merge_travels(unrolled, ptimes, travels)

    def raised(p):
        q = np.array(p, dtype=np.float64, copy=True)
        q[:, 2] += lift
        return q
    pairs = [mew_frame.wrap_path(frame, raised(p), times=t)
             for p, t in zip(laid, ptimes) if len(p) >= 2]
    paths = [w for w, _t in pairs]
    point_times = [t for _w, t in pairs]
    if not paths:
        raise ValueError("no deposition moves")
    x0, x1 = float(rod[0]), float(rod[1])
    r = float(radius)
    pts = np.concatenate(paths, axis=0)
    lo = np.minimum(pts.min(axis=0), np.array([x0, -r, -r]))
    hi = np.maximum(pts.max(axis=0), np.array([x1, r, r]))
    meta = {
        "bbox": (tuple(lo), tuple(hi)),
        "fiber_diameter": float(fiber_diameter),
        "n_paths": len(paths),
        "n_pts": sum(len(p) for p in paths),
        "radius": r,
        "length": x1 - x0,
        "axial_overhang": 0.0,
        "collector_bounds": (x0, x1),
        "print_start_x": x0,
        "family": "calibration",
        "physics": physics is not None and laid is not unrolled,
        "gcode_paths": commanded,
        "ptimes": (point_times if all(t is not None for t in point_times)
                   else None),
        "frame": frame,
    }
    return paths, meta


class CardBox:
    """`ModuleGrid`'s place in the CAD window: what `gui_calibration` asks of
    the grid (`add`, `card`, `opened`, `sync`), with each card a frame shown in
    the page's floating box by its ribbon button. A card's `grid` and
    `grid_remove` say whether the current type HAS that card."""

    class Card(ttk.Frame):
        def __init__(self, parent, key, summary=None, info=None):
            super().__init__(parent)
            self.key = key
            self.available = True
            self.columnconfigure(0, weight=1)
            head = ttk.Frame(self)
            head.grid(row=0, column=0, sticky="ew", pady=(0, 2))
            head.columnconfigure(0, weight=1)
            if summary is not None:
                import gui_form
                gui_form.mark(ttk.Label(head, textvariable=summary,
                                        style="Hint.TLabel"), "live").grid(
                    row=0, column=0, sticky="w")
            self.info_dot = None
            if info:
                self.info_dot = gui_tips.info_dot(head, info)
                self.info_dot.grid(row=0, column=1, sticky="e")
            self.body = ttk.Frame(self)
            self.body.grid(row=1, column=0, sticky="ew")
            self.body.columnconfigure(0, weight=1)

        def grid(self, *a, **k):
            if a or k:
                return super().grid(*a, **k)
            self.available = True

        def grid_remove(self):
            self.available = False

    #: the cards the Settings window also borrows (gui_settings), which must
    #: therefore be children of the root rather than of the box
    LENT = ("quality", "export")

    def __init__(self, parent, on_open=None, inner=None, room=None):
        self.parent = parent
        #: the width the window can give the box, or None (`_fit_wraps`)
        self.room = room
        #: the box's scrolled interior. A card that only ever shows in the
        #: box is ITS child, because Tk clips a widget by its parent, not by
        #: where it is gridded: a root child gridded into the box painted its
        #: lower rows (Compute, the result) over the bottom row whenever the
        #: card was taller than the window left for it.
        self.inner = inner if inner is not None else parent
        self.on_open = on_open
        self.cards = {}
        self.shown = None

    def add(self, key, title, status_var=None, summary=None, info=None,
            wide=False):
        parent = self.parent if key in self.LENT else self.inner
        card = self.Card(parent, key, summary=summary, info=info)
        self.cards[key] = card
        return card

    def card(self, key):
        return self.cards.get(key)

    def opened(self):
        return self.shown

    def sync(self):
        pass

    def show(self, key, body, row=0):
        """Put card `key` in the box's `body`. A child of the box, gridded
        into a descendant of it, so it outlives the box closing and what was
        typed in it stays."""
        card = self.cards[key]
        ttk.Frame.grid(card, in_=body, row=row, column=0, sticky="ew")
        card.lift()
        self.shown = key
        if not getattr(card, "_fit_wraps", False):
            card._fit_wraps = True
            card.bind("<Configure>", lambda _e, c=card: self._fit_wraps(c),
                      add="+")
        self._fit_wraps(card)
        if self.on_open is not None:
            self.on_open(key)

    #: the box's padding and scrollbar, between `room` and the card's width
    _BOX_CHROME = 44

    def _fit_wraps(self, card):
        """Cap every wrapping label in `card` to the width the card has.

        The forms were written for the old page and wrap at fixed widths
        (430 to 660 px). In the G-code window's box the card can be ~400 px
        wide, and a line such as the lag plate's "Conditions" ran off the
        right edge, cut mid-word. Each label keeps its own wrap as the
        ceiling, so a wide window looks as it did.

        Capped by the ROOM the window gives the box, never by the card's own
        width: the box is as wide as its content asks (up to the room), so a
        cap read off the card shrank the content, which shrank the box, which
        shrank the cap, down to the floor."""
        try:
            room = self.room() if self.room is not None else None
            if not room:
                return
            width = int(room) - self._BOX_CHROME
            if width < 50:
                return
            x0 = card.winfo_rootx()
            stack = list(card.winfo_children())
            while stack:
                w = stack.pop()
                stack.extend(w.winfo_children())
                try:
                    wrap = int(float(str(w.cget("wraplength"))))
                except (tk.TclError, ValueError):
                    continue
                if wrap <= 0 and not hasattr(w, "_wrap_ceiling"):
                    continue
                ceiling = getattr(w, "_wrap_ceiling", None)
                if ceiling is None:
                    ceiling = w._wrap_ceiling = wrap
                room = width - (w.winfo_rootx() - x0) - 12
                new = max(160, min(ceiling, room))
                if new != wrap:
                    w.configure(wraplength=new)
        except tk.TclError:
            pass

    def hide(self):
        key, self.shown = self.shown, None
        if key is None:
            return
        try:
            ttk.Frame.grid_forget(self.cards[key])
        except tk.TclError:
            pass
        if self.on_open is not None:
            self.on_open(None)


class _Picture:
    """`SweepPreviewPanel`'s place for `gui_sweeps`, which still reports to a
    panel: it says a print is ready, and the CAD panel draws it. The
    engine's own preview toggles are built into `options_host`, which is
    never shown: the bottom row is Design's."""

    def __init__(self, app):
        self.app = app
        self.options_host = None        # the engine's toggles, never shown
        self.title_row = None           # where the section's dot goes
        self.tube_map = None            # the "Where to measure" strip
        self._tube_bands = None

    def _panel(self):
        return getattr(self.app, "_gce_pv_panel", None)

    def show_options(self):
        pass

    def set_status(self, text, error=False):
        panel = self._panel()
        if panel is not None and self.app._cal_print_on():
            panel.set_status(text, error=error)

    def apply_state(self, state, subtitle):
        self.app._cal_take_state(state, subtitle)
        self._tube_bands = state.get("tube_map")
        self.sync_tube_map()
        return True

    def sync_visibility(self):
        self.app._cal_sync_visibility()

    def _render_soon(self):
        panel = self._panel()
        if panel is not None:
            panel._render_soon()

    def _update_display_mode(self, force=False):
        self._render_soon()

    def retheme(self):
        self.app._cal_draw_extras()

    def set_depo_color(self, rgb):
        pass                            # the fibre wears the design's colour

    def sync_tube_map(self):
        """The strip shows exactly when there is a tube to draw and the
        Instructions box is open: its Measure steps are what it illustrates."""
        strip = self.tube_map
        if strip is None:
            return
        app = self.app
        if not self._tube_bands or not app._sw_tube_map_wanted():
            strip.grid_remove()
            return
        import gui_modern_theme
        pal = (gui_modern_theme.DARK if app.dark_mode.get()
               else gui_modern_theme.LIGHT)
        strip.show(self._tube_bands, app._sw_player_color() or "#cc4033",
                   app._sw_preview_lag_on(),
                   {k: pal[k] for k in ("bg", "card", "fg", "hint", "border")})
        strip.grid()


class _GcodeCalibrateMixin:
    """The CAD window's Calibrate mode; see the module docstring."""

    def _cal_procedures(self):
        """Use the modern Physics label in the shared procedure text."""
        from dataclasses import replace

        return {
            key: replace(proc, groups=tuple(
                (heading, tuple(step.replace("Nozzle–collector gap",
                                             "Nozzle–collector gap")
                                for step in steps))
                for heading, steps in proc.groups))
            for key, proc in super()._cal_procedures().items()
        }
    #: tells `gui_calibration` the ribbon carries Apply
    _cal_apply_on_ribbon = True

    _cal_on = False             # the mode: the ribbon
    _cal_stash = None           # the Design's document while a print is up
    _cal_key = None             # the type whose document is on screen
    _cal_docs = {}              # type -> its document's edits, shelved
    _cal_lay = None             # the Layout on screen
    _cal_sig = None             # what that layout was built from
    _cal_state = None           # the engine's last state for the picture

    # ------------------------------------------------------------------ #
    # what state are we in
    # ------------------------------------------------------------------ #
    def _cal_active(self):
        """True while the CAD window is in Calibrate."""
        return bool(self._cal_on)

    def _cal_type(self):
        var = getattr(self, "_cal_tab_var", None)
        return var.get() if var is not None else None

    def _cal_proc(self):
        return self._cal_procedures().get(self._cal_type())

    def _cal_fit_only(self):
        return self._cal_type() in self._CAL_FIT_ONLY

    def _cal_print_on(self):
        """A calibration print is the page's document. False in Design, and
        in Fusion, which has no print and shows the Design."""
        return self._cal_on and not self._cal_fit_only()

    # ------------------------------------------------------------------ #
    # the top row
    # ------------------------------------------------------------------ #
    def _nav_select(self, page_id):
        if page_id == "calibration":
            self._cal_enter()
            return super()._nav_select("gcode")
        if page_id == "gcode":
            self._cal_leave()
        try:
            if self.status.get() == self._CAL_GUIDE:
                # Design says its own line on arrival (_gce_say_arrival)
                self.status.set("")
        except (tk.TclError, AttributeError):
            pass
        return super()._nav_select(page_id)

    def _nav_tab(self):
        tab = super()._nav_tab()
        # only the CAD page is Calibrate's: the Scaffold designer, reached
        # from Home with Calibrate still on, is Design's
        pid = self._nav_pid_of.get(self.nb.select())
        return "calibrate" if self._cal_on and pid == "gcode" else tab

    def _cal_nav_refresh(self):
        refresh = getattr(self, "_nav_refresh", None)
        if callable(refresh):
            refresh()

    def _active_mode(self):
        if not getattr(self, "_gce_dispatch", None) \
                and self._cal_print_on() and self._gce_page_shown():
            return "calibration"
        return super()._active_mode()

    def _gce_on_page(self):
        """The bottom row's Save writes THE TEXT on this page. A calibration
        print's Save writes every slide, so the shell's route takes it."""
        return not self._cal_print_on() and super()._gce_on_page()

    # ------------------------------------------------------------------ #
    # building
    # ------------------------------------------------------------------ #
    def _build_gcode_editor_page(self, page):
        super()._build_gcode_editor_page(page)
        if getattr(self, "_gce_pv_panel", None) is None \
                or not self._init_calibration_vars():
            return
        self._cal_picture = _Picture(self)
        self._cal_slide_note = _ToolNote()
        self._cal_apply_btns = {}
        self._cal_docs = {}
        # the engine builds its preview toggles into a row that is never
        # shown: the bottom row is Design's
        from gui_gcode_cad import OneRow
        held = ttk.Frame(page)
        self._cal_picture.options_host = OneRow(held)
        self._cal_picture.title_row = ttk.Frame(held)
        self._sw_mount(self._cal_picture)
        box = self._gce_formbox.body
        # Quality and Export are children of the ROOT: Tk grids a widget
        # only into a descendant of its parent, and the Settings window
        # (gui_settings) hosts those two from the root. Every other card is a
        # child of the box's scrolled interior, so the box clips it
        # (`CardBox.inner`). They stay unmanaged until `show` grids one in.
        self._cal_boxes = CardBox(
            self.root, on_open=self._cal_on_card_open, inner=box,
            room=lambda: getattr(self, "_gce_form_room", None))
        forms = ttk.Frame(box)          # the engine's labels: read, not shown
        self._cal_build_cards(forms, boxes=self._cal_boxes)
        import gui_tube_map
        self._cal_picture.tube_map = gui_tube_map.TubeMeasureMap(
            self._cal_boxes.card("instructions").body)
        self._cal_picture.tube_map.grid(row=1, column=0, sticky="ew",
                                        pady=(6, 0))
        self._cal_picture.tube_map.grid_remove()
        self._cal_build_ribbon()
        self._sw_warn_var.trace_add("write", lambda *_a: self._cal_warnings())
        # the saved spelling the page shows carries the machine's custom line,
        # which is no reason for the engine to rebuild
        for var in (getattr(self, "custom_gcode", None),
                    getattr(self, "custom_gcode_after", None)):
            if var is not None:
                var.trace_add("write", lambda *_a: self._cal_respell())

    def _cal_build_ribbon(self):
        """Type; Stabilization scaffold and Parameters, in Design's Create and
        Modify groups with Design's icons; Instructions, Measurements and
        Apply. In a frame the mode packs where Design's groups were."""
        from gui_gcode_cad import ribbon_group
        tool = self._gce_tool
        lead = ttk.Frame(self._gce_ribbon, style="Bar.TFrame")
        self._cal_groups = lead
        t = self._cal_tools = {}
        pick = ribbon_group(lead, "Calibrate")
        t["type"] = tool(
            pick, "add", "cell", "Type\n" + self._cal_type_short(),
            "What you are calibrating. Each type is one number the physics "
            "model needs, with its own test print, measurements and result.",
            menu=self._cal_type_menu)
        # one width for every type, so the ribbon does not shift when the
        # type changes; measured in the font, so the longest name fits at
        # any display scaling (a fixed 118 px cut "Activation energy")
        from gui_gcode_cad import _tool_width
        w = max(_tool_width("Type\n" + p.tab)
                for p in self._cal_procedures().values())
        tk.Frame.configure(t["type"], width=w)
        t["type"]._w_full = w
        # Design's groups, icons and order for the tools both modes have:
        # Stabilization scaffold under Create, Parameters under Modify
        cre = ribbon_group(lead, "Create")
        t["base"] = tool(
            cre, "add", "base", "Stabilization\nscaffold",
            "The small dense pad printed first, so the jet is steady before "
            "the specimens start.",
            command=lambda: self._gce_panel_open("base"))
        mod = ribbon_group(lead, "Modify")
        t["parameters"] = tool(
            mod, "change", "params", "Parameters",
            "The test print's own settings, for the type you picked.",
            command=lambda: self._gce_panel_open("parameters"))
        bench = ribbon_group(lead, "Measure")
        t["instructions"] = tool(
            bench, "change", "output", "Instructions",
            "The procedure for this type, step by step.",
            command=lambda: self._gce_panel_open("instructions"))
        t["measurements"] = tool(
            bench, "change", "measure", "Measurements",
            "What you carry back from the bench. Type your readings, then "
            "press Check or Compute to see the result. Nothing is written "
            "until you press Apply.",
            command=lambda: self._gce_panel_open("measurements"))
        t["apply"] = tool(
            bench, "change", "apply", "Apply",
            "Write the computed result into the Physics page.",
            command=self._cal_apply_pressed)
        self._GCE_PANEL_TITLES = dict(
            self._GCE_PANEL_TITLES, parameters="Parameters", base="Stabilization scaffold",
            instructions="Instructions", measurements="Measurements")
        ptools = getattr(self, "_gce_ptools", None)
        if ptools is not None:
            for key in ("parameters", "base", "instructions", "measurements"):
                ptools[key] = t[key]
        self._cal_type_shown(self._cal_type())

    # ------------------------------------------------------------------ #
    # the ribbon's own tools
    # ------------------------------------------------------------------ #
    def _cal_type_short(self):
        proc = self._cal_proc()
        return proc.tab if proc is not None else ""

    def _cal_type_menu(self):
        m = tk.Menu(self._cal_tools["type"], tearoff=0)
        procs = self._cal_procedures()
        for key in self._cal_proc_order:
            m.add_radiobutton(label=procs[key].tab, value=key,
                              variable=self._cal_tab_var)
        return m

    def _cal_apply_pressed(self):
        """Apply on the ribbon: the card's own button, so a fit's refusal
        reads where it always did, in Measurements, opened to show it. A type
        that writes two values asks which."""
        btns = self._cal_apply_btns.get(self._cal_type()) or []
        if not btns:
            return
        if getattr(self, "_gce_panel_key", None) != "measurements":
            self._gce_panel_open("measurements")
        if len(btns) == 1:
            self._cal_apply_invoke(btns[0])
            return
        tool = self._cal_tools["apply"]
        m = tk.Menu(tool, tearoff=0)
        for b in btns:
            m.add_command(label=str(b.cget("text")),
                          command=lambda b=b: self._cal_apply_invoke(b))
        m.tk_popup(tool.winfo_rootx(),
                   tool.winfo_rooty() + tool.winfo_height())

    def _cal_apply_invoke(self, button):
        """Press a card's Apply, and when it wrote nothing say so in the
        status line as well: the refusal in Measurements is easy to miss from
        the ribbon, and a status still reading the last success would claim
        the value went in."""
        before = getattr(self, "_cal_applied_count", 0)
        self._fit_last_refusal = ""
        button.invoke()
        if getattr(self, "_cal_applied_count", 0) != before:
            return
        why = getattr(self, "_fit_last_refusal", "") or (
            "Press Compute in Measurements and check the result.")
        self.status.set("Nothing was applied. " + why)

    def _gce_panel_parameters(self, body):
        self._cal_boxes.show("settings", body)

    def _gce_panel_base(self, body):
        self._cal_boxes.show("stabilization", body)

    def _gce_panel_instructions(self, body):
        self._cal_boxes.show("instructions", body)

    def _gce_panel_measurements(self, body):
        self._cal_boxes.show("fit", body)

    def _gce_panel_quality(self, body):
        if not self._cal_print_on():
            return super()._gce_panel_quality(body)
        self._gce_build_quality_row(body)
        self._cal_boxes.show("quality", body, row=1)

    def _gce_panel_export(self, body):
        if not self._cal_print_on():
            return super()._gce_panel_export(body)
        self._cal_boxes.show("export", body)

    def _gce_panel_presets(self, body):
        if not self._cal_print_on():
            return super()._gce_panel_presets(body)
        self._cal_boxes.show("presets", body)

    def _gce_panel_closed(self, e, host):
        if e.widget is host and getattr(self, "_cal_boxes", None) is not None:
            self._cal_boxes.hide()
        super()._gce_panel_closed(e, host)

    def _cal_type_shown(self, key):
        """`_cal_show_tab` has just shown type `key`: the ribbon, and the
        document, follow it."""
        tools = getattr(self, "_cal_tools", None)
        if tools is None:
            return
        fit_only = key in self._CAL_FIT_ONLY
        tools["type"]._text.configure(text="Type\n" + self._cal_type_short())
        none = "Fusion has no test print: you measure your own scaffold."
        shows = self._cal_shared_cards(key)
        base_on = shows is None or "stabilization_base" in shows
        tools["parameters"].set_why(none if fit_only else None)
        tools["base"].set_why(none if fit_only else None if base_on else
                              "This print has no base.")
        panel_key = getattr(self, "_gce_panel_key", None)
        if self._cal_on:
            if panel_key in ("parameters", "base", "quality", "export",
                             "presets") and fit_only:
                self._gce_host_close()
            self._cal_sync_doc()

    def _cal_warnings(self):
        """The engine's warnings: a specimen dropped off the plate must not
        hide behind a closed box."""
        text = self._sw_warn_var.get()
        if self._cal_print_on() and text:
            self._gce_notice_show(text, [], kind="calibrate")
        else:
            self._gce_notice_hide_if("calibrate")

    # ------------------------------------------------------------------ #
    # the mode
    # ------------------------------------------------------------------ #
    def _cal_enter(self):
        if self._cal_on or getattr(self, "_cal_tools", None) is None:
            return
        self._gce_edit_finish(keep=True)
        self._gce_host_close()
        # Design's Parameters table floats over the picture on its own,
        # not in the form host: left open, it showed Design's scaffolds in
        # Calibrate, under Measurements
        close = getattr(self, "_gce_params_close", None)
        if callable(close) and self._gce_params_shown():
            close()
        if getattr(self, "_gce_wc", None) is not None:
            self._gce_toggle_whatchanged()
        self._cal_on = True
        # Calibrate and Design share one tab, so moving between them is no
        # tab change and nothing records which one Save and Generate target
        # from Simulation. Say it here (and back in `_cal_leave`).
        if self._gce_page_shown():
            self._last_input_mode = "calibration"
        self._cal_ribbon(True)
        self._cal_sync_doc()
        self._cal_nav_refresh()
        self._sw_on_panel_mapped()
        self.status.set(self._CAL_GUIDE)
        # The first visit of a session opens Instructions, so the page shows
        # its procedure, not only a picture and twenty tools. Reported by a
        # blind tester: no "start here"; the status line was the only
        # guidance. Later visits keep whatever the user left open.
        if not getattr(self, "_cal_shown_once", False):
            self._cal_shown_once = True
            try:
                self.root.after_idle(self._cal_open_instructions)
            except (tk.TclError, AttributeError):
                pass

    def _cal_open_instructions(self):
        if self._cal_on and getattr(self, "_gce_panel_key", None) is None:
            self._gce_panel_open("instructions")

    #: the line Calibrate opens on. Leaving Calibrate for any page takes it
    #: down again if nothing has replaced it: reported, other pages still
    #: said "Calibrate is optional…" after the user had moved on
    _CAL_GUIDE = (
        "Calibrate is optional: until you apply your own measurement, the "
        "app uses the lab's measured values on the Physics page. "
        "Instructions lists the steps for this Type: Generate and print "
        "it, measure it, type what you measured under Measurements, then "
        "press Apply.")

    def _cal_leave(self):
        if not self._cal_on:
            return
        self._gce_host_close()
        self._cal_on = False
        # Save from Simulation after a Design Generate offered "Save sweep
        # g-code as" cts_sweep.gcode: the run target was still Calibrate's
        if getattr(self, "_last_input_mode", None) == "calibration":
            self._last_input_mode = "gcode"
        self._cal_unload_doc()
        self._gce_notice_hide_if("calibrate")
        self._cal_ribbon(False)
        self._cal_nav_refresh()
        self._gce_set_link_label()
        pending = getattr(self, "_cal_take_later", None)
        self._cal_take_later = None
        if pending is not None:
            super()._gce_take_design(*pending)
        # Calibrate's lines (its intro, "type what you measure under
        # Measurements") are not Design's
        self._gce_say_arrival()

    def _cal_ribbon(self, on):
        rib = self._gce_ribbon
        lead = self._gce_c_lead
        sets = getattr(self, "_gce_c_sets", {})
        if on:
            # relative to `lead`: RibbonFit may have folded any later group
            self._cal_groups.pack(side="left", fill="y", before=lead)
            for w in [lead] + list(sets.values()):
                w.pack_forget()
            self._gce_syntax_btn.set_why(
                "A calibration print is written in the Printer & collector page's syntax.")
            self._gce_wc_btn.set_why("A calibration print has no design to "
                                     "compare with.")
        else:
            lead.pack(side="left", fill="y", before=self._cal_groups)
            self._cal_groups.pack_forget()
            self._gce_syntax_btn.set_why(None)
            self._gce_wc_btn.set_why(None)
            super()._gce_c_mode_sync()
        self._gce_ribfit.fit()
        rib.update_idletasks()

    def _gce_c_mode_sync(self):
        if not self._cal_on:
            super()._gce_c_mode_sync()

    # ------------------------------------------------------------------ #
    # the document
    # ------------------------------------------------------------------ #
    def _cal_sync_doc(self):
        """The document follows the type: a print, or the Design's for
        Fusion."""
        if not self._cal_on:
            return
        if self._cal_fit_only():
            self._cal_unload_doc()
            self._gce_set_link_label()
            return
        self._sw_on_panel_mapped()

    def _cal_take_state(self, state, _subtitle):
        """The engine has a print to show."""
        self._cal_state = state
        if not self._cal_print_on() or self._cal_restoring:
            # mid-Undo the values are being put back one by one, and a
            # spelling one respells at once from the result still standing
            return
        res = self._sw_result
        kind = self.sw_vars["kind"].get()
        if res is None or {"visc": "diam"}.get(kind, kind) != res.kind:
            return                      # the last type's, still standing
        if self._cal_stash is not None and self._sw_state.get("dirty"):
            # built from values already changed again: a newer build is on
            # its way. A slow print (the sag ladder) built before an Undo
            # landed after the Redo and put the undone print up.
            return
        sig = (self._cal_type(), tuple(res.gcode_texts),
               self._cal_offsets(res), bool(self.sw_vars["stab_on"].get()),
               self._cal_undated(res, res.gcode_texts[0]),
               self._custom_gcode_spec(), self._sw_rebase_shift(),
               self._sw_feeds(res.gcode_texts[0]))
        if sig != self._cal_sig or self._cal_stash is None:
            self._cal_load_doc(self._cal_layout(res), sig)
            self._cal_sig = sig
        self._cal_draw_extras()

    def _cal_undated(self, res, text):
        """`_sw_user_gcode(text)` with the header's date and time left
        blank, for the signature: each build stamps its own minute
        (`_sw_gcode_context`), and the same print built a minute later is
        not a new print. Counted as one, it reloaded the page and dropped
        its undo history."""
        held = getattr(self, "_sw_stamp", None)
        self._sw_stamp = (res, "", "")
        try:
            return self._sw_user_gcode(text)
        finally:
            self._sw_stamp = held

    def _cal_offsets(self, res):
        """Where each slide sits in the layout: a grid, slide 1 where the
        engine put it. A tube is one print, on its rod."""
        plate = getattr(self, "_sw_plate", None)
        if res.n_slides < 2 or not isinstance(plate, tuple):
            return tuple((0.0, 0.0) for _ in res.gcode_texts)
        _ox, _oy, w, h = plate
        return tuple(tile_offsets(res.n_slides, w, h))

    def _cal_layout(self, res):
        return calibration_layout(
            self._gce_engine(), [self._sw_feeds(t) for t in res.gcode_texts],
            [p.specimens for p in res.prints], res.kind,
            bool(self.sw_vars["stab_on"].get()), self._cal_offsets(res),
            self._sw_rebase_shift())

    def _cal_respell(self):
        if self._cal_print_on() and self._sw_result is not None:
            self._sw_show_all()

    def _refresh_gcode_display_on_toggle(self):
        """An Export option flipped: the print's text is spelled again, its
        edits kept."""
        if self._cal_print_on():
            if not getattr(self, "_loading_preset", False):
                self._cal_respell()
            return
        super()._refresh_gcode_display_on_toggle()

    def _cal_load_doc(self, lay, sig=None):
        """Put the print `lay` (built from `sig`) on the page.

        A change to this type's own parameters is ONE undo step, as a
        redesign is in Design (`_gce_take_design`): the text's step carries
        the document before and after, and the sw.* values that change set
        (`_cal_on_step`). A type switch, a first load and a rebuild nothing
        on this page caused (the plate, the physics) make no step; the
        last of those starts the type's history again, as before."""
        eng = self._gce_engine()
        if self._cal_stash is None:
            self._cal_take_later = None
            self._cal_stash = dict(
                doc=self._gce_doc, model=self._gce_model_now(),
                text=self.gce.get_text(), undo=list(self.gce._undo),
                redo=list(self.gce._redo), hidden=set(self._gce_hidden),
                key=self._gw_key())
        vals = self._cal_sw_values()
        seen = self._cal_vals_seen
        same = (self._cal_key is not None and self._cal_key == self._cal_type()
                and self._cal_lay is not None and seen is not None)
        delta = ({k: (seen.get(k), v) for k, v in vals.items()
                  if seen.get(k) != v} if same else {})
        before = None
        if delta:
            before = dict(model=self._gce_snap(), lay=self._cal_lay,
                          sig=self._cal_sig, raw=self._gce_doc.get("raw"),
                          base=self._gce_doc.get("base"))
        self._cal_vals_seen = vals
        self._cal_park()
        self._cal_lay = lay
        self._cal_key = key = self._cal_type()
        slot = self._cal_docs.get(key)
        undo, redo, hidden = [], [], set()
        if slot is not None and (slot["model"].changes or before is not None
                                 or slot["model"].base_text == lay.text):
            # this type's history: its edits, its steps, its hidden rows
            undo, redo, hidden = slot["undo"], slot["redo"], slot["hidden"]
        if slot is not None and slot["model"].changes:
            # edits you made: they ride onto the print as it is now
            self._gce_model = m = slot["model"]
            m.options = self._cal_options()
            if m.base_text != lay.text:
                typed = self._gce_typed_chips()
                for c in typed:
                    m.remove(c.id)
                lost = m.rebase(lay.text, lay.lm)
                if before is None:
                    undo, redo = [], []
                self._cal_rebuilt_note(len(typed), len(lost))
            else:
                m.line_map = lay.lm
        elif slot is not None and slot["model"].base_text == lay.text:
            # the same print as when this type was left: its document back
            self._gce_model = m = slot["model"]
            m.options = self._cal_options()
            m.line_map = lay.lm
        else:
            self._gce_model = eng.Document(
                lay.text, lay.lm, self.gce_flavor_name(), self._gce_options(),
                source="calibration", linked=False)
        self._gce_hidden = hidden
        base = self._gce_render().text
        self._gce_doc = dict(
            source=self._GCE_FILE, raw=lay.text, base=base, path=None,
            sig=None, mode="g1", diameter=self._sw_diam, stale=False,
            error=None)
        self.gce._undo[:] = undo
        self.gce._redo[:] = redo
        if before is not None:
            n0 = len(self.gce._undo)
            self.gce.set_text(base, undoable=True)
            if len(self.gce._undo) > n0:
                after = dict(model=self._gce_snap(record=False), lay=lay,
                             sig=sig, raw=lay.text, base=base)
                before["vals"] = {k: o for k, (o, _n) in delta.items()}
                after["vals"] = {k: n for k, (_o, n) in delta.items()}
                self.gce.attach_meta((before["model"], after["model"],
                                      None, None, before, after))
        else:
            self.gce.set_text(base, undoable=False)
            self.gce._undo[:] = undo
            self.gce._redo[:] = redo
        self._gce_render_changes()
        self._gce_set_link_label()
        self._gce_refresh_controls()
        self._gce_pv_schedule(now=True)

    #: the sw.* values the print on screen was built from (`_cal_load_doc`)
    _cal_vals_seen = None
    _cal_restoring = False      # `_cal_restore` is putting values back
    #: sw.* keys that pick WHICH print, not what it is: never an undo step
    _CAL_NOT_STEPS = ("kind", "cal_tab")

    def _cal_sw_values(self):
        return {k: v.get() for k, v in self.sw_vars.items()
                if k not in self._CAL_NOT_STEPS}

    def _gce_on_step(self, meta, forward):
        """Undo/Redo of a step made while a print is up. The document stays
        the print's (the shared step handler re-labels a non-file document
        as detached, which a print never is); a parameter step also puts
        back the values it changed (`_cal_restore`)."""
        super()._gce_on_step(meta, forward)
        if self._cal_stash is None:
            return
        self._gce_doc["source"] = self._GCE_FILE
        if len(meta) >= 6 and meta[4 + bool(forward)] is not None:
            self._cal_restore(meta[4 + bool(forward)])

    def _cal_restore(self, st):
        """A parameter step's side: its layout and signature first, so the
        rebuild the restored values start finds the print already showing
        (`_cal_take_state` loads nothing when the signature matches), then
        the values themselves. The text is the step's own."""
        self._cal_lay = st["lay"]
        self._cal_sig = st["sig"]
        self._gce_doc.update(raw=st["raw"], base=st["base"])
        seen = dict(self._cal_vals_seen or {})
        seen.update(st["vals"])
        self._cal_vals_seen = seen
        self._cal_restoring = True
        try:
            for k, val in st["vals"].items():
                var = self.sw_vars.get(k)
                if var is not None and var.get() != val:
                    var.set(val)
        finally:
            self._cal_restoring = False

    def _gce_options(self):
        """A calibration print is spelled by the Calibration page's Export
        card, the same rule `_sw_user_gcode` writes it by."""
        if self._cal_stash is None:
            return super()._gce_options()
        return self._cal_options()

    def _cal_options(self):
        """The whole layout's options: every Machine-page setting a saved
        slide carries (`_sw_output_options`), so the page's text, Send and
        Generate have the same header, end, scripts and spelling as Save."""
        return self._sw_output_options(filename=self._gw_send_name())

    def _cal_shelve(self):
        """The print on screen goes on the shelf with its edits, undo steps
        and hidden rows, to be found again by its type."""
        if self._cal_key is None or self._cal_stash is None:
            return
        self._gce_edit_finish(keep=True)
        self._gce_record_edits()
        m = self._gce_model_now()
        if m is not None:
            self._cal_docs[self._cal_key] = dict(
                model=m, undo=list(self.gce._undo),
                redo=list(self.gce._redo), hidden=set(self._gce_hidden))

    def _cal_park(self):
        self._cal_shelve()
        self._cal_key = None

    def _cal_edited_model(self):
        """The print's document when you have edited it, on the print as the
        engine builds it now and on the live options; else None."""
        if self._cal_print_on() and self._cal_key == self._cal_type():
            self._cal_shelve()
        slot = self._cal_docs.get(self._cal_type())
        lay = self._cal_lay
        if slot is None or not slot["model"].changes or lay is None:
            return None
        m = slot["model"]
        if m.base_text != lay.text:
            m.rebase(lay.text, lay.lm)
        m.options = self._cal_options()
        return m

    def _cal_bare_parse(self, m, rendered=False):
        """`m`'s parse form with no machine frame: the print and your edits,
        no start or end block and no automatic script. The text `rendered`
        returns the `Rendered`."""
        opts, m.options = m.options, self._gce_engine().OutputOptions()
        try:
            r = m.render("parse")
        finally:
            m.options = opts
        return r if rendered else r.text

    def _cal_slide_lines(self, m, index):
        """Slide `index`'s lines of the edited document `m`, in the form the
        parser reads, at the slide's own origin. None when the slide has no
        edits: it is then the engine's bytes."""
        lay = self._cal_lay
        # the slide's own lines only: Save frames each slide with the
        # machine's start and end blocks and scripts (`_sw_user_gcode`), so
        # the layout's frame must not ride along as well
        r = self._cal_bare_parse(m, rendered=True)
        last = len(lay.slides) - 1
        out = []
        cur = 0
        for rl in r.lines:
            if rl.origin == "flavor":
                continue                # the flavor's frame is the Export's
            k = tuple(rl.key or ())
            if len(k) >= 4:
                if k[0] != "base":
                    cur = last          # a piece prints after everything
                elif k[1] == "header":
                    cur = 0
                elif k[2] >= 0:
                    cur = lay.slide_of_group(k[2]) or 0
            if cur == index:
                out.append(rl.text)
        text = "".join(out)
        s = lay.slides[index]
        base = "".join(lay.text.splitlines(keepends=True)[
            s["first"]:s["last"] + 1])
        if text.strip() == base.strip():
            return None
        dx, dy = s["offset"]
        rx, ry, rz = getattr(lay, "rebase", (0.0, 0.0, 0.0))
        # back in plate coordinates, as the engine wrote the slide: Save
        # puts it on the g-code origin (`_sw_slide_text`)
        return shift_text(self._gce_engine(), text, -dx - rx, -dy - ry, -rz)

    def _sw_selected_slide_gcode(self):
        """Generate builds, and the Simulation plays, the whole print as the
        picture lays it out, with its edits."""
        res = getattr(self, "_sw_result", None)
        if not self._cal_print_on() or res is None or self._cal_lay is None:
            return super()._sw_selected_slide_gcode()
        import gui_common
        m = self._cal_edited_model()
        # the print's own lines: whatever leaves the app from this file is
        # framed by the export (`_gcode_save_text`), so it is not framed here
        text = self._cal_lay.text if m is None else self._cal_bare_parse(m)
        path = os.path.join(gui_common.SESSION_DIR, "sweep_print.gcode")
        try:
            with open(path, "w", newline="") as fh:
                fh.write(text)
        except OSError as exc:
            self.status.set(f"Could not prepare the g-code: {exc}")
            return None
        return path

    def _sw_export_gcode(self, folder, base, write_csv):
        """Save g-code: the print on screen is shelved first, so what you
        typed on it is in what is written."""
        if self._cal_print_on():
            self._cal_shelve()
        return super()._sw_export_gcode(folder, base, write_csv)

    def _sw_slide_text(self, index, raw, filename=None):
        """A slide you edited saves its lines of the document, at its own
        origin and in the printer's spelling; one you did not saves as
        it always did."""
        m = self._cal_edited_model()
        lay = self._cal_lay
        if m is None or lay is None or index >= len(lay.slides) \
                or lay.raws[index] != self._sw_feeds(raw):
            return super()._sw_slide_text(index, raw, filename)
        text = self._cal_slide_lines(m, index)
        return super()._sw_slide_text(index, raw if text is None else text,
                                      filename)

    def _cal_rebuilt_note(self, typed, lost):
        """The print was rebuilt under its edits."""
        said = []
        if typed:
            said.append("%d typed edit%s dropped" % (typed, "s" * (typed != 1)))
        if lost:
            said.append("%d edit%s could not find %s place" % (
                len(lost), "s" * (len(lost) != 1),
                "their" if len(lost) != 1 else "its"))
        if said:
            self._gce_notice_show(
                "The calibration print was rebuilt: " + "; ".join(said)
                + ".", [("OK", lambda: self._gce_notice_hide_if(
                    "calibrate-rebuilt"))], kind="calibrate-rebuilt")

    def _cal_unload_doc(self):
        st = self._cal_stash
        if st is None:
            return
        self._cal_park()
        self._cal_stash = None
        self._cal_sig = None
        self._cal_clear_extras()
        self._gce_doc, self._gce_model = st["doc"], st["model"]
        self._gce_hidden = st["hidden"]
        self.gce.set_text(st["text"], undoable=False)
        self.gce._undo[:] = st["undo"]
        self.gce._redo[:] = st["redo"]
        held = self.__dict__.pop("_cal_persist_later", None)
        if held is not None:
            # a preset or a reproduced run set the Design's document while
            # the print was up (`_gce_persist_written` held it): written
            # again now, onto the Design's own
            self.gce_document.set(held)
        self._gce_render_changes()
        self._gce_refresh_controls()
        self._gce_pv_schedule(now=True)
        self._gce_on_show()

    def _gce_take_design(self, raw, sig):
        """A design regenerated while a print is up: it waits for Design,
        since the document on screen is not the one it belongs to."""
        if self._cal_stash is not None:
            self._cal_take_later = (raw, sig)
            return
        super()._gce_take_design(raw, sig)

    def _gce_sync_persist(self):
        if self._cal_stash is None:
            super()._gce_sync_persist()

    def _gw_key(self):
        """Nothing here is the Design's work, so nothing here is unsaved
        work: the fingerprint stays the one Design left."""
        st = self._cal_stash
        return st["key"] if st is not None else super()._gw_key()

    def _gw_send_name(self):
        """Send stores a calibration print under its own name (the one Save
        offers), not the Design's "scaffold.gcode"; a typed name wins."""
        if not self._cal_print_on():
            return super()._gw_send_name()
        typed = self.ml_upload_name.get().strip()             if hasattr(self, "ml_upload_name") else ""
        name = self._sw_default_name() + ".gcode"
        if typed and hasattr(self, "_ml_sanitize_filename"):
            return self._ml_sanitize_filename(typed, name)
        return name

    def _gce_send_upload(self, text, say):
        """A temperature ladder goes up as the files Save writes, one per
        rung, and none of them starts: the operator sets each rung's
        temperature first. Any other print is one file, as Design's."""
        rungs = (self._sw_rung_files(self._gw_send_name())
                 if self._cal_print_on() else None)
        if not rungs:
            return super()._gce_send_upload(text, say)
        self._gw_offer_start = None
        if not self._sw_rung_confirm(rungs, self.ml_host.get().strip()):
            say("Not sent.")
            return False
        note = self._sw_rung_note(rungs, self.ml_host.get().strip())
        if not self._ml_upload_files(rungs, os.path.dirname(
                self._gce_scratch("send.gcode")), note):
            say(self.ml_conn_status.get())
            return False
        self._gw_sending = "ladder"   # the outcome comes back to this notice
        say(note)
        return True

    def _gw_save(self):
        """Ctrl+S and File > Save: every slide, as Save g-code does."""
        if self._cal_print_on():
            self.save_gcode_as()
            return True
        return super()._gw_save()

    def _gce_file_menu(self):
        m = super()._gce_file_menu()
        if self._cal_on:
            m.entryconfigure(0, label="Save g-code")
            m.entryconfigure(1, label="Open file in Design…")
        return m

    def _gce_open_file(self):
        """A file you open is a Design document."""
        if self._cal_on:
            self._nav_select("gcode")
        super()._gce_open_file()

    def _gw_label_refresh(self):
        """The saved/unsaved mark is about the Design, which is waiting."""
        if self._cal_stash is None:
            super()._gw_label_refresh()
        elif getattr(self, "gce_work_var", None) is not None:
            self.gce_work_var.set("")

    def _gce_set_link_label(self):
        if not self._cal_print_on() or self._cal_stash is None:
            super()._gce_set_link_label()
            return
        lay = self._cal_lay
        label = "● Calibration print: " + self._cal_type_short()
        if lay is not None and len(lay.slides) > 1:
            label += " · %d slides" % len(lay.slides)
        self.gce_link_var.set(label)
        self._gce_relink_btn.pack_forget()

    def _summary_target(self, mode):
        if self._cal_print_on():
            return "Calibrate", self._cal_type_short()
        return super()._summary_target(mode)

    # ------------------------------------------------------------------ #
    # the tree
    # ------------------------------------------------------------------ #
    def _gce_design_group_label(self, iid, node, label):
        lay = self._cal_lay
        if self._cal_stash is None or lay is None:
            return super()._gce_design_group_label(iid, node, label)
        if node.key[0] != "base":
            return label
        g = int(node.key[1])
        return lay.names[g] if 0 <= g < len(lay.names) else label

    def _gce_design_layer_label(self, lid, node, label):
        lay = self._cal_lay
        if self._cal_stash is None or lay is None:
            return label
        k = tuple(node.key)
        if k[0] != "base":
            return label
        return lay.layer_names.get((int(k[1]), int(k[2])), label)

    def _gce_c_label(self):
        """Design's Collector row, sized from the calibration's own slide or
        rod: the one under the picture, not Design's."""
        if self._cal_stash is None:
            return super()._gce_c_label()
        rod = self._sw_tube_geometry()
        if rod is not None:
            return ("Collector · mandrel", "Ø %g" % round(2 * rod["radius"], 3))
        plate = getattr(self, "_sw_plate", None)
        if isinstance(plate, tuple) and len(plate) == 4:
            return ("Collector · slide", "%g×%g" % (round(plate[2], 3),
                                                   round(plate[3], 3)))
        return ("Collector", "")

    def _gce_design_open(self, iid):
        """The slide is sized on Parameters, so the row opens that."""
        if self._cal_stash is not None and iid == "d:collector":
            self._gce_panel_open("parameters")
            return True
        return super()._gce_design_open(iid)

    def _gce_design_eye(self, iid):
        super()._gce_design_eye(iid)
        if self._cal_stash is not None and iid == "d:collector":
            self._cal_sync_visibility()

    def _gce_design_rows_into(self, iid, node, parent, info, order):
        if self._cal_stash is None:
            super()._gce_design_rows_into(iid, node, parent, info, order)

    def _gce_anatomy_rows(self, m):
        """Design's rows, with a Slide row over each slide's parts."""
        rows, info = super()._gce_anatomy_rows(m)
        lay = self._cal_lay
        if self._cal_stash is None or lay is None:
            return rows, info
        slide_of = {}
        for iid, nf in info.items():
            if nf.get("kind") == "group" and nf["key"][0] == "base":
                k = lay.slide_of_group(int(nf["key"][1]))
                if k is not None:
                    slide_of[iid] = k
        if not slide_of:
            return rows, info
        out, made = [], set()
        for row in rows:
            iid, par = row[0], row[1]
            k = slide_of.get(iid)
            if k is not None and not par:
                sid = "n:s:%d" % k
                if sid not in made:
                    made.add(sid)
                    mine = [i for i, s in slide_of.items() if s == k]
                    ords = [info[i]["ord"] for i in mine]
                    first = min(info[i]["first"] for i in mine)
                    last = max(info[i]["last"] for i in mine)
                    s = lay.slides[k]
                    lab = s["label"] + (" · " + s["summary"]
                                        if s["summary"] else "")
                    info[sid] = dict(kind="slide", key=(k,), first=first,
                                     last=last, label=s["label"].lower(),
                                     ord=None, ords=(min(ords), max(ords)))
                    out.append((sid, "", lab, (), ("%d–%d" % (first, last),
                                                   "")))
                row = (iid, sid) + tuple(row[2:])
            out.append(row)
        return self._cal_rehome(m, out, info), info

    def _cal_rehome(self, m, rows, info):
        """An edit of a whole slide (a Move of its Slide row) sits under the
        Slide row, not under Whole file, with the edits made on it."""
        eng = self._gce_engine()
        by_ords = {nf["ords"]: iid for iid, nf in info.items()
                   if nf.get("kind") == "slide"}
        home, moved = {}, {}
        for iid, par, *_rest in rows:
            nf = info.get(iid) or {}
            if par in moved:
                moved[iid] = moved[par]
            elif par == "n:file" and nf.get("kind") == "edit":
                try:
                    sc = getattr(m.get(nf["cid"]), "scope", None)
                except KeyError:
                    sc = None
                if sc is not None and sc.kind == "group":
                    sid = by_ords.get((sc.first, sc.last))
                    if sid is not None:
                        moved[iid] = sid
                        home[iid] = sid
        if not moved:
            return rows
        keep = [r for r in rows if r[0] not in moved]
        for sid in dict.fromkeys(home.values()):
            # its parts are the slide's, not "places" it was copied to
            sub = [(r[0], sid, r[2], r[3], ("", r[4][1])) if r[0] in home
                   else r for r in rows if moved.get(r[0]) == sid]
            at = next(k for k, r in enumerate(keep) if r[0] == sid) + 1
            keep[at:at] = sub
        return keep

    def _gce_node_scope(self, info):
        """A Slide row covers its parts."""
        if info.get("kind") == "slide":
            a, b = info["ords"]
            return self._gce_engine().Scope("group", a, b)
        return super()._gce_node_scope(info)

    # ------------------------------------------------------------------ #
    # the picture
    # ------------------------------------------------------------------ #
    def _gce_preview_text(self):
        """The engine's bytes, laid out. A flat print has its layer Z taken
        out, one Z word left: the solver stacks by deposition order, and the
        ladder's Z steps on top of that stand a 20-layer wall 0.34 mm tall
        where 20 fibers of 0.011 mm reach 0.23. A tube's Z is its ply
        radius."""
        if self._cal_stash is None:
            return super()._gce_preview_text()
        if self._cal_edited():
            return self._cal_flat(self._gce_parse_text())
        return self._cal_flat(self._gce_doc["raw"])

    def _cal_edited(self):
        """The print on screen has edits, or typing not yet recorded: what
        it draws is the document, not the engine's bytes."""
        m = self._gce_model_now()
        return (self._cal_stash is not None and m is not None
                and (bool(m.changes) or self._gce_edited()))

    def _cal_flat(self, text):
        """`text` as the picture reads it (see `_gce_preview_text`)."""
        if self._sw_tube_geometry() is not None:
            return text
        return self._rewrite_gcode_lines(text, False, False,
                                         drop_layer_z=True, boundaries=())

    def _gce_pv_source(self):
        """Past the document's parse form: the document holds the text as
        shown, and its respelled travels would draw as fiber."""
        if self._cal_stash is None:
            return super()._gce_pv_source()
        if self._cal_edited():
            inner = super()._gce_pv_source()
            tube = self._sw_tube_geometry() is not None
            flat = self._rewrite_gcode_lines

            def run():
                out = inner()
                text, rest = out if isinstance(out, tuple) else (out, None)
                if not tube:
                    text = flat(text, False, False, drop_layer_z=True,
                                boundaries=())
                return text if rest is None else (text, rest)
            return run
        text = self._gce_preview_text()
        return lambda: text

    def _gce_pv_options(self):
        """Design's: the same jet lag, coiling wave, sagging and travels. The
        machine plate's off-plate check is the engine's job here, slide by
        slide; the sag ladder's walls need the exact drape."""
        opts = super()._gce_pv_options()
        if self._cal_stash is None:
            return opts
        # the document sits on the g-code origin (`calibration_layout`'s
        # rebase); the picture, its labels and its slides are in plate
        # coordinates, so the picture undoes exactly that re-base
        lay = self._cal_lay
        rx, ry, rz = getattr(lay, "rebase", (0.0, 0.0, 0.0))
        shift = (-rx, -ry, -rz) if (rx or ry or rz) else None
        opts = dict(opts, plate=None, preview_shift=shift,
                    physics=self._cal_physics(opts.get("physics"), read=False))
        if self.sw_vars["kind"].get() == "sag" and opts.get("sag"):
            opts["sag"] = dict(opts["sag"], exact=True)
        return opts

    def _view_layer_table(self, nodes, clock):
        """For colouring, a slide is one scaffold: its base, passes and end
        share the slide's colour, and the Color tool names it "Slide k". The
        tree keeps its own rows. A piece added from the library counts as a
        scaffold of its own, after the slides."""
        rows = super()._view_layer_table(nodes, clock)
        lay = self._cal_lay
        if self._cal_stash is None or lay is None:
            return rows
        keys = []
        for nd in nodes:
            if nd.kind == "group" and tuple(nd.key) not in keys:
                keys.append(tuple(nd.key))
        remap, extra = {}, len(lay.slides)
        for i, key in enumerate(keys):
            k = lay.slide_of_group(int(key[1])) if key[0] == "base" else None
            if k is None:
                extra += 1
                remap[i + 1] = (extra, None)
            else:
                remap[i + 1] = (k + 1, lay.slides[k]["label"])
        for r in rows:
            s, name = remap.get(r["scaffold"], (r["scaffold"], None))
            r["scaffold"] = s
            if name is not None:
                r["name"] = name
        return rows

    def _view_scaffold_colors(self, n, fallback):
        """A slide's colour starts as Design's scaffold colour at its place;
        one chosen here is the calibration's, kept apart from the design."""
        out = super()._view_scaffold_colors(n, fallback)
        if self._cal_stash is None:
            return out
        own = self._gce_prefs().get("cal_color_scaf") or {}
        for i in range(n):
            rgb = self._view_rgb(own[str(i)]) if str(i) in own else None
            if rgb is not None:
                out[i] = rgb
        return out

    def _view_set_scaffold(self, i, hx):
        """Recolouring a slide never recolours Design's scaffold."""
        if self._cal_stash is None:
            super()._view_set_scaffold(i, hx)
            return
        own = dict(self._gce_prefs().get("cal_color_scaf") or {})
        own[str(i)] = hx
        self._gce_prefs_set(cal_color_scaf=own)
        self._view_scaf_refresh()
        self._view_note_color([self._view_rgb(hx)])

    def _gce_pv_arc(self, options):
        res = getattr(self, "_sw_result", None)
        if self._cal_stash is None or res is None:
            return super()._gce_pv_arc(options)
        return res.arc_segments

    def _sw_player_color(self):
        """The first slide's colour, as the Color tool set it: the
        calibration's own pick, else Design's first scaffold's."""
        own = (self._gce_prefs().get("cal_color_scaf") or {}).get("0")
        return (own or self.gce_scaf_vars[0]["color"].get()).strip()

    def _sw_diam_mult_value(self):
        """1: Calibrate draws Design's picture, which has no drawing-only
        diameter multiplier, so the Simulation and the STL do not either.
        A preset's old `sw.diam_mult` is kept, and read by nothing here."""
        return 1.0

    def _gce_pv_subtitle(self):
        if self._cal_stash is not None:
            return "Every slide of the print, drawn from this text."
        return super()._gce_pv_subtitle()

    def _gce_pv_is_tube(self):
        if self._cal_stash is not None:
            return self._sw_tube_geometry() is not None
        return super()._gce_pv_is_tube()

    #: what the calibration print sets of the jet; the rest is Design's
    _CAL_JET_KEYS = ("model", "pressure", "voltage", "temperature", "zeta",
                     "fiber_diameter")

    def _cal_physics(self, design=None, read=True):
        """Design's jet, gated by Design's Jet lag box, printed at the
        calibration's own gap, pressure, voltage and temperature (the
        mandrel's values folded in for a tube). Design's own sim_* values
        are a different print's."""
        if read:
            try:
                design = self._pv_snapshot_options(0).get("physics")
            except (ValueError, tk.TclError):
                design = None
        if design is None:
            return None
        cal = self._sw_physics_dict(self.sw_vars["kind"].get())
        if cal is None:
            return design
        phys = dict(design)
        for k in self._CAL_JET_KEYS:
            if k in cal:
                phys[k] = cal[k]
        return phys

    def _gce_tube_snapshot(self):
        if self._cal_stash is None:
            return self._mandrel_snapshot()
        geo = self._sw_tube_geometry()
        physics = self._cal_physics()
        diam = float(self._sw_diam)

        def build(text, path):
            return build_rod_preview(path, text, geo["radius"],
                                     geo["surface_z"], geo["rod"], diam,
                                     physics=physics, lift=0.5 * diam,
                                     lay_travels=True)
        return {"build": build}

    def _gce_pv_apply(self, paths, meta, err):
        super()._gce_pv_apply(paths, meta, err)
        if self._cal_stash is not None and not err:
            self._cal_draw_extras()

    def _cal_collector_on(self):
        """The one Collector box on screen. Which var it is follows Design's
        collector mode, not the calibration's, so a 4-axis type read
        pv_show_collector while the box showed pv_show_plate."""
        var = self._gce_c_eye_var()
        return True if var is None else bool(var.get())

    def _pv_plate_wanted(self):
        if self._cal_stash is not None:
            return (self._sw_tube_geometry() is None
                    and self._cal_collector_on())
        return super()._pv_plate_wanted()

    def _gce_rod_wanted(self):
        if self._cal_stash is not None:
            return (self._sw_tube_geometry() is not None
                    and self._cal_collector_on())
        return super()._gce_rod_wanted()

    def _pv_on_show_plate(self, *_a):
        super()._pv_on_show_plate(*_a)
        self._cal_sync_visibility()

    def _pv_on_show_collector(self, *_a):
        super()._pv_on_show_collector(*_a)
        self._cal_sync_visibility()

    def _cal_scene(self):
        """The CAD panel, once its scene exists, with the lists the extras
        keep on it."""
        panel = self._gce_pv_panel
        if panel is None or panel._ren is None:
            return None
        if getattr(panel, "_cal_labels", None) is None:
            panel._cal_labels = []
            panel._cal_plates = []
        return panel

    def _cal_label_spots(self):
        """(text, (x, y, z)) of each specimen value on each slide: one label,
        beside the lines that print it."""
        res = getattr(self, "_sw_result", None)
        lay = self._cal_lay
        state = self._cal_state or {}
        if res is None or lay is None:
            return []
        if self._sw_tube_geometry() is not None:
            return list(state.get("labels") or ())
        z = 1.0         # clear of the solid plate drawn under each slide
        out = []
        for k, meta in enumerate(res.prints):
            dx, dy = lay.slides[k]["offset"] if k < len(lay.slides) \
                else (0.0, 0.0)
            if res.kind not in ("cts", "diam"):
                # one label per specimen: a mirrored sag gap or a repeated
                # ring would average to the middle of the slide. A ring is
                # labelled on its own circle, since a stack shares one centre.
                for s in meta.specimens:
                    x, y = float(s.center[0]), float(s.center[1])
                    if res.kind == "circle":
                        x += 0.5 * float(s.size)
                    out.append((s.value_text, (x + dx, y + dy, z)))
                continue
            by = {}
            for s in meta.specimens:
                by.setdefault(s.value_text, []).append(s)
            for text, ss in by.items():
                # repeats of one value are stacked lines: one label, left
                x = min(float(s.center[0]) - 0.5 * float(s.size)
                        for s in ss) - 1.0
                y = float(np.mean([s.center[1] for s in ss]))
                out.append((text, (x + dx, y + dy, z)))
        return out

    def _cal_draw_extras(self):
        """The labels, and a plate under every slide past the first, over the
        print just drawn."""
        panel = self._cal_scene()
        if panel is None or self._cal_stash is None:
            return
        import gui_preview
        vtk = gui_preview._VTK
        self._cal_clear_extras()
        make = getattr(vtk, "vtkBillboardTextActor3D", None)
        for text, pos in self._cal_label_spots() if make else ():
            a = make()
            a.SetInput(str(text))
            a.SetPosition(*pos)
            tp = a.GetTextProperty()
            tp.SetFontSize(13)
            tp.SetColor(*self._cal_label_rgb())
            tp.SetJustificationToRight()
            tp.SetVerticalJustificationToCentered()
            panel._ren.AddActor(a)
            panel._cal_labels.append(a)
        plate = getattr(panel, "_plate_actor", None)
        lay = self._cal_lay
        if plate is not None and lay is not None and len(lay.slides) > 1:
            src = panel._plate_src
            for s in lay.slides[1:]:
                dx, dy = s["offset"]
                c = vtk.vtkCubeSource()
                c.SetXLength(src.GetXLength())
                c.SetYLength(src.GetYLength())
                c.SetZLength(src.GetZLength())
                cx, cy, cz = src.GetCenter()
                c.SetCenter(cx + dx, cy + dy, cz)
                mp = vtk.vtkPolyDataMapper()
                mp.SetInputConnection(c.GetOutputPort())
                act = vtk.vtkActor()
                act.SetMapper(mp)
                act.SetProperty(plate.GetProperty())
                panel._ren.AddActor(act)
                panel._cal_plates.append(act)
        self._cal_sync_visibility()

    def _cal_label_rgb(self):
        dark = bool(getattr(self, "dark_mode", tk.BooleanVar(value=False)).get())
        return (0.92, 0.93, 0.95) if dark else (0.15, 0.16, 0.18)

    def _cal_clear_extras(self):
        panel = self._gce_pv_panel
        if panel is None or getattr(panel, "_cal_labels", None) is None:
            return
        for a in panel._cal_labels + panel._cal_plates:
            panel._ren.RemoveActor(a)
        panel._cal_labels = []
        panel._cal_plates = []

    def _cal_sync_visibility(self):
        """The extras follow the Collector box (cheap: no rebuild)."""
        panel = self._cal_scene()
        if panel is None or self._cal_stash is None:
            return
        on = 1 if self._pv_plate_wanted() else 0
        for a in panel._cal_plates:
            a.SetVisibility(on)
        holder = getattr(self, "_gce_pv_holder", None)
        if holder is not None:
            holder.set_plate_visible(self._pv_plate_wanted())
            holder.set_collector_visible(self._gce_rod_wanted())
        panel._render_soon()


class _ToolNote:
    """`_TipNote`'s two-method contract for the one section that has no
    widget of its own any more: the slide pager, which every slide on screen
    at once replaced."""

    def __init__(self):
        self._text = ""

    def set_info_text(self, text, title=None):
        self._text = text

    def info_text(self):
        return self._text
