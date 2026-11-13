#!/usr/bin/env python3
"""gui_saved_choices.py -- every closed-list setting is SAVED by its key.

A menu's label is wording, and wording changes. A setting saved as the label
its menu showed broke on every rewording: the old value matched no choice,
and the reader fell back to its default without a word (a tube saved
"Centred on the rod" printed at the left end under a row that still said
centred). This registry is the one place that knows, for each closed-list
setting a preset stores, its keys, its labels and the labels older builds
saved.

THE BOUNDARY, NOT THE VARS. Many of these settings keep their LABEL in the
Tk var the menu shows (the Sag solver's "Approximate (fast)"), and dozens of
readers map it to a key at the point of use. Rather than move each var and
every reader, a preset is translated where it crosses the disk:

  * `to_saved` -- a save (`_preset_data_for_kind`) writes the KEY;
  * `from_saved` -- a load (`gui_presets._migrate_saved_choices`, the last
    step of `_migrate_preset_data`) turns a key, today's label or an old
    label into what the var holds; anything else is UNREADABLE and is
    named (`problem`) and left at the field's default by
    `_apply_preset_for_kind`, never silently read as some other choice.

A setting whose var already holds a key (the collector, the pattern, the
tube's placement since 2026-11-13) is listed too, so an unreadable saved
value is named the same way.

Pure Python: the label tables are read from the modules that own them, on
first use."""

import re



def _entries():
    """{name: entry}. An entry is a dict: `what` (the field's name, for a
    message), `pairs` ((key, label), ...), `legacy` {old label: key}, `mem`
    "label" when the var holds the label, "key" when it holds the key.
    `pairs` may be a callable for a list that grows (the printer flavors)."""
    import gui_common as C
    import gui_buildplate as B
    import gui_mandrel as M

    def lab(choices):          # (label, key) lists, as gui_common keeps them
        return tuple((k, l) for l, k in choices)

    def flavors():
        import mew_gcode
        return tuple((f.name, f.label) for f in mew_gcode.FLAVORS.values())

    def flavor_legacy():
        import mew_gcode
        import flavor_presets
        out = {}
        for old, new in mew_gcode.LEGACY_FLAVOR_LABELS.items():
            f = flavor_presets.by_label(new)
            if f is not None:
                out[old] = f.name
        # a label a saved flavor showed before a Rename (`flavor_presets`'s
        # rename memory): a preset saved under it still loads that flavor
        for name, olds in flavor_presets._FORMER.items():
            f = mew_gcode.FLAVORS.get(name)
            if f is not None and not f.builtin:
                for o in olds:
                    out.setdefault(o, name)
        return out

    E = {
        "sag_mode": dict(what="Sagging", mem="label",
                         pairs=lab(C.SAG_SOLVER_CHOICES), legacy={}),
        "mesh_tol": dict(what="Mesh detail", mem="label",
                         pairs=lab(C.MESH_DETAIL_CHOICES), legacy={}),
        "sag_resolution": dict(what="Sag resolution", mem="label",
                               pairs=lab(C.SAG_RESOLUTION_CHOICES),
                               legacy={}),
        "kinematics": dict(what="Kinematics", mem="label",
                           pairs=lab(B._KINEMATICS),
                           legacy={old: B._KINEMATICS_KEY[old]
                                   for old in B._KINEMATICS_LEGACY}),
        "feed_time_base": dict(what="Feed time unit", mem="label",
                               pairs=tuple((B._FEED_TIME_UNIT[l], l)
                                           for l in B.FEED_TIME_LABELS),
                               legacy={}),
        "anchor": dict(what="Zero corner", mem="label",
                       pairs=(("bottom-left", "Bottom-left"),
                              ("bottom-right", "Bottom-right"),
                              ("top-left", "Top-left"),
                              ("top-right", "Top-right"),
                              ("center", "Center"), ("custom", "Custom")),
                       legacy={"Centre": "center"}),
        "face": dict(what="Zero on face", mem="label",
                     pairs=(("top", "Top"), ("bottom", "Bottom")), legacy={}),
        "rotary_kind": dict(what="Rotary stage", mem="label",
                            pairs=lab(B._ROTARY_KINDS), legacy={}),
        "origin_axial": dict(
            what="Mandrel zero along the axis", mem="label",
            pairs=lab(B._ROTARY_ORIGIN_AXIAL),
            legacy={k: v for k, v in B._ROTARY_ORIGIN_AXIAL_KEY.items()
                    if k not in B._ROTARY_ORIGIN_AXIAL_LABELS}),
        "origin_radial": dict(
            what="Mandrel zero across it", mem="label",
            pairs=lab(B._ROTARY_ORIGIN_RADIAL),
            legacy={k: v for k, v in B._ROTARY_ORIGIN_RADIAL_KEY.items()
                    if k not in B._ROTARY_ORIGIN_RADIAL_LABELS}),
        "flavor": dict(what="G-code flavor", mem="label", pairs=flavors,
                       legacy=flavor_legacy),
        "speed_unit": dict(what="Print speed unit", mem="label",
                           pairs=(("units", "Standard units"),
                                  ("cts", "× CTS")), legacy={}),
        # a var that holds the key: only an unreadable value to name
        "collector": dict(what="Collector", mem="key",
                          pairs=(("plate", "Plate (3-axis)"),
                                 ("mandrel", "Mandrel (4-axis)")),
                          # a build that had a third collector: opens flat
                          # (`gui_scaffold._apply_collector_mode`)
                          legacy={"surface": "plate"}),
        "place": dict(what="Position on mandrel", mem="key",
                      pairs=tuple((k, l) for k, l, _s in M.PLACEMENTS),
                      legacy=dict(M._SAVED_PLACEMENT_LABELS)),
        "family": dict(what="Pattern", mem="key",
                       pairs=tuple(M.MANDREL_FAMILY_CHOICES),
                       legacy={l: k for k, l in M.MANDREL_FAMILY_CHOICES}),
        "design_mode": dict(what="Closure", mem="key",
                            pairs=tuple((k, k) for k in
                                        M.MANDREL_DESIGN_MODES), legacy={}),
        "cell_fit": dict(what="Cell fit", mem="key",
                         pairs=(("scale", "Scaled"), ("fit", "Fitted")),
                         legacy={}),
        "feed_mode": dict(what="Feeds", mem="key",
                          pairs=(("auto", "auto"), ("fixed", "fixed")),
                          legacy={}),
        "speed_mode": dict(what="Speed held", mem="key",
                           pairs=(("surface", "surface"), ("omega", "omega")),
                           legacy={}),
        "parent": dict(what="Moves with", mem="key",
                       pairs=(("frame", "frame"), ("gantry", "gantry")),
                       legacy={}),
        "plate_shape": dict(what="Plate shape", mem="key",
                            pairs=(("rectangular", "rectangular"),
                                   ("circular", "circular")), legacy={}),
        "deposition_order": dict(what="Deposition order", mem="key",
                                 pairs=(("scaffold", "scaffold"),
                                        ("layer", "layer")), legacy={}),
        "link_loops_mode": dict(what="Linking loops", mem="key",
                                pairs=(("auto", "auto"),
                                       ("manual", "manual")), legacy={}),
        "gcode_convention": dict(what="Move words", mem="key",
                                 pairs=(("GX", "GX"), ("G0X", "G0X")),
                                 legacy={}),
        "rotary_word": dict(what="Rotary axis letter", mem="key",
                            pairs=(("A", "A"), ("B", "B"), ("C", "C")),
                            legacy={}),
    }
    return E


#: persisted key (a scaffold's s<n>. / gce.s<n>. prefix taken off) -> entry
_KEYS = {
    "g.sag_mode": "sag_mode", "s.sag_mode": "sag_mode",
    "g.mesh_tol": "mesh_tol", "s.mesh_tol": "mesh_tol",
    "s.sag_resolution": "sag_resolution",
    "machine.kinematics": "kinematics",
    "origin.feed_time_base": "feed_time_base",
    "origin.anchor": "anchor", "origin.face": "face",
    "rotary.kind": "rotary_kind",
    "rotary.origin_axial": "origin_axial",
    "rotary.origin_radial": "origin_radial",
    "origin.gcode_flavor": "flavor",
    "s.collector": "collector",
    "m.place": "place", "m.family": "family",
    "m.design_mode": "design_mode", "m.cell_fit": "cell_fit",
    "m.feed_mode": "feed_mode", "m.speed_mode": "speed_mode",
    "machine.bed_parent": "parent", "machine.head_parent": "parent",
    "plate.shape": "plate_shape", "s.deposition_order": "deposition_order",
    "s.gcode_convention": "gcode_convention", "rotary.word": "rotary_word",
}
#: per-scaffold keys, under any scaffold prefix (s., s2., gce.s3.)
_SCAFFOLD_KEYS = {"speed_unit": "speed_unit",
                  "link_loops_mode": "link_loops_mode",
                  "m_place": "place", "m_family": "family",
                  "m_design_mode": "design_mode", "m_cell_fit": "cell_fit"}
_SCAFFOLD_RE = re.compile(r"^(?:s\d*|gce\.s\d+)\.(\w+)$")

_CACHE = {}


def entry(pkey):
    """The registry entry for persisted key `pkey`, or None."""
    name = _KEYS.get(pkey)
    if name is None:
        m = _SCAFFOLD_RE.match(pkey)
        name = _SCAFFOLD_KEYS.get(m.group(1)) if m else None
    if name is None:
        return None
    if not _CACHE:
        _CACHE.update(_entries())
    e = _CACHE[name]
    pairs = e["pairs"]() if callable(e["pairs"]) else e["pairs"]
    legacy = e["legacy"]() if callable(e["legacy"]) else e["legacy"]
    return dict(e, pairs=pairs, legacy=legacy)


def _key_of(e, value):
    """The key `value` names in entry `e` -- a key, today's label or an
    old one, case and spaces aside -- or None."""
    text = str(value if value is not None else "").strip()
    for k, l in e["pairs"]:
        if text == k or text == l:
            return k
    low = text.lower()
    for k, l in e["pairs"]:
        if low == str(k).lower() or low == str(l).lower():
            return k
    for old, k in e["legacy"].items():
        if low == str(old).lower():
            return k
    return None


def to_saved(pkey, value):
    """What a preset stores for `pkey`: its key. A value that names no
    choice is stored as it is (it was already unreadable)."""
    e = entry(pkey)
    if e is None or isinstance(value, bool):
        return value
    k = _key_of(e, value)
    return value if k is None else k


def from_saved(pkey, value):
    """`(value for the var, problem)`: what the var holds for a saved key
    or label (today's label, or the key, as the setting keeps it), or
    `(value, sentence)` when it names no choice. A blank is left alone."""
    e = entry(pkey)
    if e is None or isinstance(value, bool) or value is None \
            or str(value).strip() == "":
        return value, None
    k = _key_of(e, value)
    if k is None:
        return value, problem(e, value)
    if e["mem"] == "key":
        return k, None
    return dict(e["pairs"])[k], None


def problem(e, value):
    """A sentence naming an unreadable saved value."""
    shown = [l for _k, l in e["pairs"]]
    return ("%s was saved as “%s”, which is not one of its "
            "choices (%s); it was left at its default."
            % (e["what"], value, ", ".join(shown)))
