#!/usr/bin/env python3
"""test_ribbon_fit.py -- the G-code page's ribbon stays inside its window
(`gui_gcode_cad.RibbonFit`), driven in a real, mapped Tk app at the window
sizes a user has: wider than the ribbon, 1680x1000 (capture_ui's), 1280x720,
and the shell's minimum (`gui_shell.SHELL_MIN_W/H`).

Comparators (none of them written by RibbonFit):
  * Tk's own packer: what a tool's `winfo_ismapped` / `winfo_width` /
    `winfo_rootx` say on screen, and the ribbon's `winfo_reqwidth` at each
    level, which the fitter's arithmetic must equal to the pixel;
  * the tools as built: each ToolButton's construction width
    (`_tool_width` of its label) and `ribbon_group`'s gap, for the look
    that must not change wherever the ribbon already fits;
  * the tools' own state (`set_why`, `configure(state=)`, `set_on`) and
    what a click on them does (`_gce_panel_open`).

Cases, each with its RED proof (the defect injected in memory by a probe script that does not ship,
the case seen failing; the
clean run is the GREEN control):
  every_tool_reachable   RED: `RibbonFit.fit` a no-op (the ribbon as built
                         before: Presets, Save, Copy all unmapped at 1680).
                         A tool counts as reachable when it is drawn whole
                         in the window, or listed in More's menu while
                         More is.
  no_room_wasted         RED: `fit` folding three tools more than it must.
                         The same defect as folding whole groups, as it did
                         until 2026-10-31: 212 px empty at 1080 with all of
                         Inspect (344 px) in More.
  wide_is_as_built       RED: `widths` never letting level 0 fit (always
                         compact).
  widens_back            RED: `fit` never widening (`lv = cur`), and
                         `HYST = 0` (widens with no room to spare).
  popup_reads_state      RED: `_popup` drawing every entry "normal";
                         `anchor` returning its widget (the tutorial
                         points at a tool that is off the ribbon).
  collector_swap_refits  RED: `_gce_c_mode_sync` not calling `fit` AND a
                         ToolButton's label change not refitting (the
                         3-axis set comes back at the 4-axis level, 1361
                         px in 1280). Either refit alone keeps it green:
                         the Collector tool's label changes on the swap.
  display_row_fits       the action bar's display row (`OneRow.fit`, via
                         `_gce_disp_fit`): every toggle and every bar
                         control reachable at each size, the whole row
                         again when wide, a More entry on its toggle's own
                         variable, and a refit when Update preview joins.
                         RED: `_gce_disp_fit` a no-op (five toggles
                         clipped at 1280, all but one at 1080); the
                         Update-preview refit removed (Collector and Reset
                         view clipped at 1680). Clipped, not shrunk: the
                         squeezed strip cuts a toggle that keeps its own
                         width, hence `_on_screen` walks the ancestors.

    xvfb-run -a -s "-screen 0 1920x1200x24" python tests/test_ribbon_fit.py

SKIPS with no Tk display. Bounded: every wait has a deadline."""
import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import sys

try:
    import tkinter as tk
except Exception:  # noqa: BLE001
    print("SKIP: tkinter unavailable")
    sys.exit(0)

import test_gcode_editor as E

check, pump, _M = E.check, E.pump, E._M

#: the sizes a user has (W, H); the last is the shell's minimum
SIZES = ((1680, 1000), (1280, 720), (1080, 640))
WIDE = (2200, 1000)


def _app():
    return _M["app"]


def _size(w, h):
    root = _M["root"]
    root.geometry(f"{w}x{h}+0+0")
    pump(0.4, lambda: root.winfo_width() == w)
    pump(0.2)


def _mode(mode):
    _app().s_collector.set(mode)
    pump(0.4)


def _tools(w):
    from gui_gcode_cad import ToolButton
    out = []
    for c in w.winfo_children():
        if isinstance(c, ToolButton):
            out.append(c)
        else:
            out.extend(_tools(c))
    return out


def _on_screen(t):
    """Drawn whole inside the window (Tk's own word for it): at its asked
    width, and inside every ancestor, since a squeezed parent clips a child
    that keeps its own full width."""
    try:
        if not (t.winfo_ismapped() and t.winfo_width() >= t.winfo_reqwidth()):
            return False
        x0, x1 = t.winfo_rootx(), t.winfo_rootx() + t.winfo_width()
        a = t.master
        while a is not None:
            if not (a.winfo_rootx() <= x0
                    and x1 <= a.winfo_rootx() + a.winfo_width()):
                return False
            a = a.master
        return True
    except tk.TclError:
        return False


def _unreachable():
    """The live ribbon tools a user cannot reach: neither drawn whole on
    screen nor listed in More's menu while More is."""
    fit = _app()._gce_ribfit
    bad = []
    folded = fit.folded()
    labels = []
    if folded:
        m = fit._popup()
        labels = [m.entrycget(i, "label") for i in range(m.index("end") + 1)
                  if m.type(i) != "separator"]
    for box in fit.groups():
        for t in fit.tools(box):
            lab = " ".join(t.cget("text").split())
            if t._fold_info is not None:
                if not (_on_screen(fit.more) and lab in labels):
                    bad.append(lab)
            elif not _on_screen(t):
                bad.append(lab)
    return bad


# ---------------------------------------------------------------------- #
def every_tool_reachable():
    for w, h in SIZES:
        for mode in ("plate", "mandrel"):
            _mode(mode)
            _size(w, h)
            bad = _unreachable()
            check(f"every tool reachable at {w}x{h}, {mode}", not bad,
                  f"cannot reach: {bad} (ribbon asks "
                  f"{_app()._gce_ribbon.winfo_reqwidth()} px)")


def no_room_wasted():
    """As many tools show as fit: the slack left of the ribbon is less than
    what the first tool in More would take, measured against Tk's own
    `winfo_reqwidth`."""
    app = _app()
    fit, rib = app._gce_ribfit, app._gce_ribbon
    for mode in ("plate", "mandrel"):
        _mode(mode)
        for w, h in SIZES:
            _size(w, h)
            folded = fit.folded()
            if not folded:
                continue
            box = folded[0]
            mine = fit.tools(box)
            shown = [t for t in mine if t._fold_info is None]
            first = mine[len(shown)]
            step = fit._box_w(box, True, shown + [first]) - (
                fit._box_w(box, True, shown) if shown else 0)
            slack = rib.winfo_width() - rib.winfo_reqwidth()
            check(f"the first tool in More does not fit at {w}x{h}, {mode}",
                  slack < step + fit.HYST,
                  f"{slack} px spare, {first.cget('text')!r} takes {step}")


def wide_is_as_built():
    from gui_gcode_cad import GROUP_PADX
    app = _app()
    fit = app._gce_ribfit
    for mode in ("plate", "mandrel"):
        _mode(mode)
        _size(*WIDE)
        check(f"level 0 when the ribbon fits, {mode}", fit.level == 0,
              f"level {fit.level}, ribbon {fit.widths()[0]} px in "
              f"{app._gce_ribbon.winfo_width()}")
        off = [" ".join(t.cget("text").split())
               for b in fit.groups() for t in fit.tools(b)
               if t.winfo_width() != t._w_full]
        check(f"every tool at its built width, {mode}", not off, str(off))
        gaps = [b._rg["title"] for b in fit.groups()
                if fit._padx(b) != 2 * GROUP_PADX or b._rg["folded"]]
        check(f"every group at its built gap, unfolded, {mode}", not gaps,
              str(gaps))
        check(f"no More, {mode}", not fit.more_box.winfo_manager())


def widens_back():
    app = _app()
    fit, rib = app._gce_ribfit, app._gce_ribbon
    _mode("plate")
    _size(1080, 640)
    low = fit.level
    _size(*WIDE)
    check("widening the window brings every group back", fit.level == 0
          and low >= 2, f"level {low} at 1080, {fit.level} at {WIDE[0]}")
    # the edge: widening back takes HYST pixels to spare
    _size(1080, 640)
    lv = fit.level
    edge = fit.widths()[lv - 1]          # the next level up, exactly
    root = _M["root"]
    pad = root.winfo_width() - rib.winfo_width()
    _size(edge + pad + fit.HYST - 1, 640)
    held = fit.level
    _size(edge + pad + fit.HYST, 640)
    check("at the edge it holds, past HYST it widens",
          held == lv and fit.level == lv - 1 and fit.HYST > 0,
          f"HYST {fit.HYST}: level {held} at the edge, {fit.level} past it, "
          f"from {lv}")


def popup_reads_state():
    app = _app()
    fit = app._gce_ribfit
    _mode("plate")
    _size(1080, 640)
    folded = [b._rg["title"] for b in fit.folded()]
    check("FILE is folded into More at the minimum window",
          "File" in folded and _on_screen(fit.more)
          and fit.more.cget("text") == "More", f"folded {folded}")
    m = fit._popup()
    heads = [m.entrycget(i, "label") for i in range(m.index("end") + 1)
             if m.type(i) == "command" and m.entrycget(i, "label").isupper()]
    check("More lists each folded group under its caption, in order",
          heads == [t.upper() for t in folded], f"{heads} vs {folded}")
    save = app._gce_ptools["presets"]
    was = save.cget("state")
    save.configure(state="disabled")
    m = fit._popup()
    ent = {m.entrycget(i, "label"): i for i in range(m.index("end") + 1)
           if m.type(i) != "separator"}
    check("a greyed tool is a greyed entry",
          m.entrycget(ent["Presets"], "state") == "disabled",
          m.entrycget(ent["Presets"], "state"))
    check("a live tool is a live entry",
          m.entrycget(ent["Syntax"], "state") == "normal")
    save.configure(state=was)
    if getattr(app, "_gce_form", None) is not None:
        app._gce_host_close()
        pump(0.2)
    # the popup was built while Presets was greyed: read it again
    m = fit._popup()
    ent = {m.entrycget(i, "label"): i for i in range(m.index("end") + 1)
           if m.type(i) != "separator"}
    m.invoke(ent["Presets"])
    pump(0.4)
    check("an entry does what its tool does (Presets opens its panel)",
          getattr(app, "_gce_panel_key", None) == "presets"
          and app._gce_form is not None)
    m = fit._popup()
    ent = {m.entrycget(i, "label"): i for i in range(m.index("end") + 1)
           if m.type(i) != "separator"}
    check("a pressed tool is a ticked entry",
          m.type(ent["Presets"]) == "checkbutton", m.type(ent["Presets"]))
    app._gce_host_close()
    pump(0.3)
    check("the tutorial's anchor for a folded tool is More",
          fit.anchor(save) is fit.more)
    _size(*WIDE)
    check("and the tool itself once the window is wide again",
          fit.anchor(save) is save)


def collector_swap_refits():
    app = _app()
    rib = app._gce_ribbon
    # fitted fresh on the narrower 4-axis set (compact, nothing folded),
    # then swapped: the 3-axis set at that level is too wide for 1280
    _mode("mandrel")
    _size(*WIDE)
    _size(1280, 720)
    for mode in ("plate", "mandrel", "plate"):
        _mode(mode)
        check(f"fits after the swap to {mode} at 1280",
              rib.winfo_reqwidth() <= rib.winfo_width() and
              not _unreachable(),
              f"ribbon {rib.winfo_reqwidth()} px in {rib.winfo_width()}, "
              f"level {app._gce_ribfit.level}")


def _bar_unreachable():
    """The action bar's display items a user cannot reach: neither drawn
    whole on screen nor listed in the row's More menu while More is; and
    any other bar control not drawn whole."""
    app = _app()
    flow = app._gce_flow
    bad = []
    labels = []
    if flow.keep is not None:
        m = flow.fold_menu()
        labels = [m.entrycget(i, "label") for i in range(m.index("end") + 1)]
    folded = flow.folded_items()
    for it in flow._shown():
        lab = str(it.widget.cget("text"))
        if it in folded:
            if not (_on_screen(flow._fold_btn) and lab in labels):
                bad.append(lab)
        elif not _on_screen(it.widget):
            bad.append(lab)
    for w in app._shell_bar.pack_slaves():
        if w is not app._gce_disp and w.winfo_ismapped() \
                and not _on_screen(w):
            bad.append(w.winfo_class())
    if app._gce_pv_btn.winfo_ismapped() and not _on_screen(app._gce_pv_btn):
        bad.append("Update preview")
    return bad


def display_row_fits():
    app = _app()
    flow = app._gce_flow
    _mode("plate")
    for w, h in SIZES + (WIDE,):
        _size(w, h)
        bad = _bar_unreachable()
        check(f"every display item reachable at {w}x{h}", not bad,
              f"cannot reach: {bad} (keep {flow.keep}, bar asks "
              f"{app._shell_bar.winfo_reqwidth()} px in "
              f"{app._shell_bar.winfo_width()})")
    check("the whole row, no More, once the window is wide again",
          flow.keep is None and not (flow._fold_btn is not None
                                     and flow._fold_btn.winfo_ismapped()),
          f"keep {flow.keep}")
    _size(1080, 640)
    m = flow.fold_menu()
    ent = {m.entrycget(i, "label"): i for i in range(m.index("end") + 1)}
    cb = next(it.widget for it in flow.folded_items()
              if it.widget.winfo_class() == "TCheckbutton")
    var = str(cb.cget("variable"))
    was = app.root.globalgetvar(var)
    m.invoke(ent[str(cb.cget("text"))])
    pump(0.2)
    now = app.root.globalgetvar(var)
    m.invoke(ent[str(cb.cget("text"))])
    pump(0.2)
    check("a More entry flips its toggle's own variable",
          str(now) != str(was)
          and str(app.root.globalgetvar(var)) == str(was),
          f"{cb.cget('text')}: {was} -> {now}")
    # Update preview joins the strip for a large text; the row refits
    _size(1680, 1000)
    lc = app.gce.line_count
    app.gce.line_count = lambda: 10 ** 9
    try:
        app._gce_pv_schedule()
        pump(0.4)
        bad = _bar_unreachable()
        check("Update preview on the bar refits the row at 1680",
              app._gce_pv_btn.winfo_ismapped() and not bad,
              f"cannot reach: {bad} (keep {flow.keep})")
    finally:
        app.gce.line_count = lc
        app._gce_pv_btn.pack_forget()
        app._gce_disp_fit()
        pump(0.3)


CASES = (every_tool_reachable, no_room_wasted, wide_is_as_built, widens_back,
         popup_reads_state, collector_swap_refits, display_row_fits)


def run(names=None):
    root = _M["root"]
    root.deiconify()
    for case in CASES:
        if names and case.__name__ not in names:
            continue
        print(f"[{case.__name__}]")
        try:
            case()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(f"{case.__name__} raised", False, repr(exc))


def main():
    try:
        root = tk.Tk()
        root.destroy()
    except tk.TclError:
        print("SKIP: no display")
        return 0
    if not E.boot():
        print("FAIL  boot")
        return 1
    run()
    app, root = _M["app"], _M["root"]
    for aid in root.tk.call("after", "info"):
        try:
            root.tk.call("after", "cancel", aid)
        except tk.TclError:
            pass
    app._gce_stop_regen()
    root.destroy()
    fails = E._FAILS
    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
