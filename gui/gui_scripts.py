#!/usr/bin/env python3
"""gui_scripts.py -- SCRIPTS: one store, one switch, one window.

A script is a `mew_gcode.Recipe`: a name and the rules (g-code at a place)
it writes. There is ONE store of them, My library (the user's recipes.json),
and everything that touches a script reads and writes that:

  * the G-code page's library panel (Use here, Save to library),
  * this window (`ScriptLibrary`): New, edit, duplicate, delete -- opened from
    the Machine page's G-code card OR the G-code page, and never moves you
    between pages,
  * the Machine page's "Automatic commands" list, which is only a VIEW of the
    scripts whose `automatic` switch is on.

`automatic` is a flag on the script, in the library; nothing is copied, so
there is nothing to keep in step. An automatic script runs in every g-code the
app writes, at the place its rules say (`OutputOptions.rig_recipes`, filled
from `App._machine_recipes()` each time the options are built), and the
G-code tree shows it as a locked row tagged "automatic".

The three ways to make one:
  1. one-off: an edit in the file (+ New edit, or typing), which stays there;
  2. save it: Save to library on that edit (or Save on its row);
  3. script first: New, here, writes it without any file open.

The window follows the flavor editor's rules (gui_flavor_editor): explicit
Save / Revert, and nothing that would lose an edit does so without saying so
on the status line first."""

import copy
import json
import os

from gui_common import *
import gui_fonts
import gui_form
import mew_gcode
from gui_tips import add_tip

#: the "when it runs" choices that repeat per layer or per scaffold: they take
#: an Every and a From/To
_PER_PART = ("before_layer", "after_layer", "before_group", "after_group")
#: the choices whose one number is not "every": label, unit
_NUMBERED = {"every_n_moves": ("Every", "moves"),
             "every_n_mm": ("Every", "mm of fiber"),
             "every_n_seconds": ("Every", "seconds"),
             "at_z": ("Z height", "mm")}
_WHERE_LABEL = dict(mew_gcode.WHERE_CHOICES)
_WHERE_KEY = {lab: k for k, lab in mew_gcode.WHERE_CHOICES}


def _ordinal(n):
    n = int(n)
    if 10 <= n % 100 <= 20:
        return "%dth" % n
    return "%d%s" % (n, {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def _num(v):
    try:
        return "%g" % float(v)
    except (TypeError, ValueError):
        return ""


def when_words(rule):
    """Where a rule fires, in plain words: "After every 5th layer"."""
    lab = _WHERE_LABEL.get(rule.where, rule.where)
    if rule.where in _PER_PART:
        every = int(rule.every or 1)
        if every > 1:
            lab = lab.replace("each", "every " + _ordinal(every))
        part = "layers" if "layer" in rule.where else "scaffolds"
        if rule.first not in (None, ""):
            last = rule.last if rule.last not in (None, "") else rule.first
            lab += (" (%s %s)" % (part, _num(rule.first))
                    if str(last) == str(rule.first) else
                    " (%s %s\u2013%s)" % (part, _num(rule.first), _num(last)))
        return lab
    if rule.where == "at_z":
        return "When Z first reaches %s mm" % _num(rule.n)
    if rule.where in _NUMBERED:
        return lab.replace("N", _num(rule.n) or "N")
    return lab


def script_when(rec):
    """A script's "when", for a list: its first rule's, plus a count."""
    rules = [r for r in rec.rules if isinstance(r, mew_gcode.Rule)]
    if not rules:
        return "Changes existing lines"
    more = len(rec.rules) - 1
    return when_words(rules[0]) + (" +%d more" % more if more > 0 else "")


def script_first_line(rec, flavor="klipper"):
    for r in rec.rules:
        if isinstance(r, mew_gcode.Rule):
            try:
                lines = mew_gcode.edit_lines(r, flavor)
            except Exception:                             # noqa: BLE001
                lines = (r.text or "").splitlines()
            first = next((ln.strip() for ln in lines if ln.strip()), "")
            return first if len(first) <= 44 else first[:43] + "\u2026"
    return ""


def placeholder_help():
    """The Placeholders dialog's text: what each name stands for, and where
    it works."""
    out = ["Write [[name]] in a header, an end block or a script; it is "
           "filled in when the g-code is written.", "",
           "Anywhere (header, end block, scripts):"]
    out += ["  [[%s]]  \u2014  %s" % kd for kd in mew_gcode.BLOCK_PLACEHOLDERS]
    out += ["", "Scripts only (the value at the layer or move the script "
                "runs on; the header is written before any move, so it has "
                "no such value):"]
    out += ["  [[%s]]  \u2014  %s" % kd for kd in mew_gcode.RULE_PLACEHOLDERS]
    return "\n".join(out)


def placeholder_problems(text, rule_ok=False):
    """Sentences about the [[names]] in `text` that will not fill, or []."""
    known = {k for k, _d in mew_gcode.PLACEHOLDERS}
    out, seen = [], set()
    for m in mew_gcode._PLACEHOLDER.finditer(text or ""):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        if name not in known:
            out.append("[[%s]] is not a placeholder MEWracle knows." % name)
        elif name in mew_gcode.RULE_ONLY_PLACEHOLDERS and not rule_ok:
            out.append("[[%s]] only works in a script, where it is the value "
                       "at the layer or move the script runs on; this block "
                       "is written once, before any move.%s" % (
                           name, " For the nozzle height use [[nozzle_height]]."
                           if name == "z" else ""))
    return out


# =========================================================================== #
# the App side
# =========================================================================== #
class _ScriptsMixin:
    """Mixed into the modern app: the store's automatic view, the window's
    opener, and the Machine card's Automatic commands list."""

    # -- the store ---------------------------------------------------------- #
    def _scripts_all(self):
        """Every script, starters first (a fresh copy)."""
        fn = getattr(self, "_gce_library", None)
        return fn() if fn is not None else []

    def _scripts_write_quiet(self, lib):
        """Write My library without refreshing anything: for a caller that is
        itself inside a render (the legacy import, the old-line migration)."""
        path = self._gce_recipes_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            mew_gcode.save_library(path, lib)
        except OSError:
            pass
        self._gce_lib_cache = copy.deepcopy(lib)

    def _automatic_import_legacy(self):
        """A machine preset from before the unification carries its automatic
        rules as a JSON copy in `machine_gcode_rules`. Bring each into My
        library as an automatic script (an existing script of that name just
        gets its switch turned on), then empty the copy: from here on there
        is one definition."""
        var = getattr(self, "machine_gcode_rules", None)
        if var is None:
            return
        raw = (var.get() or "").strip()
        if raw in ("", "[]"):
            return
        try:
            data = json.loads(raw)
        except ValueError:
            return
        var.set("[]")
        if not isinstance(data, list) or not hasattr(self, "_gce_library"):
            return
        lib = self._gce_library()
        by_name = {r.name: r for r in lib if not r.builtin}
        changed = False
        for d in data:
            try:
                rec = mew_gcode.Recipe.from_dict(d)
            except (KeyError, TypeError, ValueError):
                continue
            if rec.name in by_name:
                if not by_name[rec.name].automatic:
                    by_name[rec.name].automatic = True
                    changed = True
                continue
            rec.automatic = True
            lib.append(rec)
            by_name[rec.name] = rec
            changed = True
        if changed:
            self._scripts_write_quiet(lib)

    def _machine_recipes(self, *, shown=True):
        """The automatic scripts, as dicts (what `OutputOptions.rig_recipes`
        carries into a render and into Print History). `shown` notes them as
        what the g-code now reflects (`_scripts_changed` compares against it);
        a caller that only looks passes False."""
        self._automatic_import_legacy()
        out = [r.to_dict() for r in self._scripts_all()
               if r.automatic and not r.builtin]
        if shown:
            self._automatic_shown = json.dumps(out, sort_keys=True)
        return out

    def _automatic_scripts(self):
        return [r for r in self._scripts_all() if r.automatic and not r.builtin]

    def _machine_add_recipe(self, recipe):
        """A script that must run on every print (the old numbered line, once
        migrated): into My library, switched on."""
        if not hasattr(self, "_gce_library"):
            return
        recipe = copy.deepcopy(recipe)
        recipe.automatic = True
        lib = [r for r in self._gce_library()
               if r.builtin or r.name != recipe.name]
        lib.append(recipe)
        self._scripts_write_quiet(lib)
        self._scripts_changed()

    def _automatic_set(self, name, on):
        """Turn a script of mine's automatic switch on or off."""
        lib = self._scripts_all()
        hit = next((r for r in lib if not r.builtin and r.name == name), None)
        if hit is None or hit.automatic == bool(on):
            return
        hit.automatic = bool(on)
        self._gce_write_library(lib)

    def _scripts_changed(self):
        """My library changed: every view of it follows."""
        self._automatic_refresh_card()
        win = getattr(self, "_script_lib", None)
        if win is not None and win.alive():
            win.refresh()
        if getattr(self, "_machine_migrating", False) \
                or getattr(self, "_loading_preset", False):
            return
        # The g-code is spelled again only when the automatic scripts
        # changed. Any other library write (Save, Remove, a rename) leaves it
        # alone, so an open edit form stays open.
        now = json.dumps(self._machine_recipes(shown=False), sort_keys=True)
        if now == getattr(self, "_automatic_shown", None):
            return
        if getattr(self, "_gce_fs", None) is not None:
            # An edit form is open on the G-code page (its Run automatically
            # box is what changed). Spelling the g-code again now closed the
            # form under the click; it waits for the form to close instead
            # (`_gce_edit_finish` / `_gce_edit_lost` run it).
            self._automatic_respell_pending = True
            return
        getattr(self, "_refresh_gcode_display_on_toggle", lambda: None)()

    def _automatic_respell_if_pending(self):
        """The automatic scripts changed while an edit form was open: spell
        the g-code with them now that it has closed."""
        if self.__dict__.pop("_automatic_respell_pending", False):
            self._scripts_changed()

    # -- opening things, without taking anyone off their page ---------------- #
    def open_script_library(self, select=None, new=False):
        """The My library window: over whatever page is showing."""
        return ScriptLibrary.open(self, select=select, new=new)

    def _open_machine_gcode_card(self):
        """The Machine page's G-code card: the header, the end block and the
        automatic commands."""
        if hasattr(self, "_nav_select"):
            self._nav_select("machine")
        if hasattr(self, "_setup_go"):
            self.root.after_idle(lambda: self._setup_go("gcode", "gcode_rules"))

    # -- the Machine card's Automatic commands list --------------------------- #
    def _build_automatic_section(self, parent, F):
        F.section("Automatic commands", first=False)
        F.intro("Scripts that run on every print you make, each at the place "
                "it says. They are the scripts in My library with Run "
                "automatically ticked; add one there, or press New script. "
                "On the G-code page they show as locked rows in the tree.",
                wrap=560)
        host = ttk.Frame(parent)
        lb = tk.Listbox(host, height=5, width=58, exportselection=False)
        gui_form.exempt(lb, "list of automatic scripts")
        lb.grid(row=0, column=0, rowspan=5, sticky="nsew")
        hint = gui_form.mark(ttk.Label(
            host, text="None yet. Press New script, or tick Run automatically "
                       "on a script in My library.", style="Hint.TLabel",
            wraplength=400), "live")
        hint.grid(row=0, column=0, sticky="nw")
        pad = (gui_fonts.SP["m"], 0)

        def button(row, text, cmd, tip):
            b = gui_form.mark(ttk.Button(host, text=text, command=cmd),
                              "button")
            b.grid(row=row, column=1, sticky="ew", padx=pad,
                   pady=(0 if row == 0 else gui_fonts.SP["xs"], 0))
            add_tip(b, tip)
            return b
        self._machine_rules_edit = button(
            0, "Edit\u2026", self._automatic_edit_selected,
            "Open the selected script to change what it writes and where.")
        self._machine_rules_remove = button(
            1, "Stop running", self._automatic_stop_selected,
            "Untick Run automatically on the selected script. It stays in "
            "My library.")
        button(2, "New script\u2026", lambda: self.open_script_library(new=True),
               "Write a new script. Tick Run automatically on it to have it "
               "run on every print.")
        self._machine_rules_library = button(
            3, "My library\u2026", self.open_script_library,
            "Every script you have saved, and the examples, in a window "
            "over this page.")
        self._machine_rules_list = lb
        self._machine_rules_hint = hint
        lb.bind("<<ListboxSelect>>", lambda _e: self._automatic_selection())
        lb.bind("<Double-Button-1>",
                lambda _e: self._automatic_edit_selected())
        F.slot(None, host, "The scripts that run on every print.", sticky="w")
        self._automatic_names = []
        self._automatic_refresh_card()

    def _automatic_refresh_card(self):
        box = getattr(self, "_machine_rules_list", None)
        if box is None:
            return
        try:
            if not box.winfo_exists():
                return
        except tk.TclError:
            return
        mine = self._automatic_scripts()
        names = [r.name for r in mine]
        rows = ["%s \u2014 %s" % (r.name, script_when(r)) for r in mine]
        if names != getattr(self, "_automatic_names", None) or \
                rows != list(box.get(0, "end")):
            box.delete(0, "end")
            for text in rows:
                box.insert("end", text)
            self._automatic_names = names
        # Edit and Stop running act on a listed script, so with none they
        # are not shown: greyed beside the empty-state hint they read as
        # loose text run into it (vireo, item 4)
        per_row = [getattr(self, a, None) for a in
                   ("_machine_rules_edit", "_machine_rules_remove")]
        if mine:
            self._machine_rules_hint.grid_remove()
            box.grid()
            for b in per_row:
                if b is not None:
                    b.grid()
        else:
            box.grid_remove()
            self._machine_rules_hint.grid()
            for b in per_row:
                if b is not None:
                    b.grid_remove()
        self._automatic_selection()

    def _automatic_selected_name(self):
        box = getattr(self, "_machine_rules_list", None)
        sel = box.curselection() if box is not None else ()
        names = getattr(self, "_automatic_names", [])
        return names[sel[0]] if sel and sel[0] < len(names) else None

    def _automatic_selection(self):
        on = "normal" if self._automatic_selected_name() else "disabled"
        for attr in ("_machine_rules_edit", "_machine_rules_remove"):
            b = getattr(self, attr, None)
            if b is not None:
                b.configure(state=on)

    def _automatic_edit_selected(self):
        name = self._automatic_selected_name()
        if name:
            self.open_script_library(select=name)

    def _automatic_stop_selected(self):
        name = self._automatic_selected_name()
        if name:
            self._automatic_set(name, False)

    def _build_header_notes_row(self, F):
        F.entry("Print notes", self.gcode_header_notes,
                "Free text for this print. It fills [[notes]] in the header "
                "(the default header has a Notes line).", width="path")

    def _header_problems(self):
        """Sentences about names in the start and end blocks that will not
        fill, for the card's live warning."""
        out = []
        for var in (self.machine_gcode_start, self.machine_gcode_end):
            for p in placeholder_problems(var.get()):
                if p not in out:
                    out.append(p)
        return out

    def _header_process_gaps(self, where="this card"):
        """What the start and end blocks, with the heat-wait box, leave the
        printer to do by hand (voltage, pressure, temperature), as the
        sentences `mew_gcode.process_gap_text` writes; "" when none. Read off
        the blocks as they will be written, so a command the lab typed into
        its own block counts."""
        try:
            fl = self._machine_flavor_name()
            ctx = self._machine_gcode_context(
                use_gce=hasattr(self, "gce_scaf_vars"))
            text = "".join(mew_gcode.render_machine_block(b, ctx, fl)
                           for b in self._machine_blocks())
            wait = getattr(self, "s_wait_for_temp", None)
            if wait is not None and wait.get():
                text += mew_gcode.wait_temp_line(ctx.get("temperature"), fl)
            return mew_gcode.process_gap_text(
                mew_gcode.process_setpoints(text, fl), fl, where=where)
        except Exception:                                 # noqa: BLE001
            return ""

    def _header_preview_text(self):
        """The start block as the header will read it, with this design's
        values."""
        if self.machine_omit_blocks.get():
            return ("(Leave out the start and end blocks is ticked: the "
                    "g-code begins at its first move.)")
        try:
            ctx = self._machine_gcode_context(
                use_gce=hasattr(self, "gce_scaf_vars"))
            return mew_gcode.render_machine_block(
                self.machine_gcode_start.get(), ctx,
                self._machine_flavor_name()).rstrip("\n")
        except Exception as exc:                          # noqa: BLE001
            return "(the header could not be shown: %s)" % exc


# =========================================================================== #
# the window
# =========================================================================== #
class ScriptLibrary:
    """My library, as a window. One per app: `open` reuses it."""

    @classmethod
    def open(cls, app, select=None, new=False):
        win = getattr(app, "_script_lib", None)
        if win is None or not win.alive():
            win = cls(app)
            app._script_lib = win
        win.top.deiconify()
        win.top.lift()
        if new:
            win.new()
        elif select:
            win.select_name(select)
        return win

    # ------------------------------------------------------------------ #
    def __init__(self, app):
        self.app = app
        self.current = None           # name of the saved script shown, or None
        self.rec = None               # the Recipe shown (a copy, may be a draft)
        self.draft = False
        self.dirty = False
        self._loading = False
        self._delete_armed = False
        self._close_armed = False
        SP = gui_fonts.SP
        top = self.top = tk.Toplevel(app.root)
        top.title("My library")
        top.transient(app.root)
        gui_fonts.size_dialog(top, 920, 640, 720, 460)
        top.protocol("WM_DELETE_WINDOW", self.close)
        outer = ttk.Frame(top, padding=SP["l"])
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(2, weight=1)
        ttk.Label(outer, text="My library", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            outer, style="Hint.TLabel", wraplength=860, justify="left",
            text="Scripts: g-code you write once and use again. Use one in "
                 "a file, or tick Run automatically and it is written into "
                 "every print."
        ).grid(row=1, column=0, columnspan=2, sticky="w",
               pady=(SP["xs"], SP["s"]))

        # -- the list ---------------------------------------------------- #
        left = ttk.Frame(outer)
        left.grid(row=2, column=0, sticky="nsw", padx=(0, SP["l"]))
        left.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse",
                                 height=16)
        self.tree.column("#0", width=gui_fonts.dpi_px(left, 280))
        self.tree.grid(row=0, column=0, columnspan=2, sticky="nsw")
        self.tree.tag_configure("builtin", foreground="#7a7a7a")
        self.tree.tag_configure("auto", foreground="#2e7d4f")
        self.tree.tag_configure("head", foreground="#7a7a7a")
        self.tree.bind("<<TreeviewSelect>>", self._picked)
        b = ttk.Button(left, text="New script", command=self.new)
        b.grid(row=1, column=0, sticky="ew", pady=(SP["s"], 0))
        add_tip(b, "Write a new script from scratch. It is saved here, in "
                   "My library, and is not put into any file until you say "
                   "Use in this g-code.")
        b = ttk.Button(left, text="Duplicate", command=self.duplicate)
        b.grid(row=1, column=1, sticky="ew", padx=(SP["xs"], 0),
               pady=(SP["s"], 0))
        add_tip(b, "Make your own copy of the selected script (an example "
                   "too) to change.")
        self.legend = ttk.Label(left, style="Hint.TLabel", wraplength=280,
                                justify="left",
                                text="Green: runs on every print.")
        self.legend.grid(row=2, column=0, columnspan=2, sticky="w",
                         pady=(SP["s"], 0))

        # -- the selected script ------------------------------------------ #
        right = ttk.Frame(outer)
        right.grid(row=2, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        right.rowconfigure(5, weight=1)
        self.banner = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.banner, style="Hint.TLabel",
                  wraplength=560, justify="left").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, SP["xs"]))
        self.v_name = tk.StringVar()
        self.v_desc = tk.StringVar()
        self.v_when = tk.StringVar()
        self.v_every = tk.StringVar()
        self.v_first = tk.StringVar()
        self.v_last = tk.StringVar()
        self.v_n = tk.StringVar()
        self.v_auto = tk.BooleanVar(value=False)
        self.w = {}
        self._field(right, 1, "Name", ttk.Entry(right, textvariable=self.v_name,
                                                width=40), "name",
                    "What this script is called, here and in the G-code "
                    "tree.")
        self._field(right, 2, "What it is for",
                    ttk.Entry(right, textvariable=self.v_desc, width=60),
                    "desc", "Optional: a reminder of why you wrote it.")
        self._field(right, 3, "When it runs",
                    ttk.Combobox(right, textvariable=self.v_when,
                                 values=[lab for _k, lab in
                                         mew_gcode.WHERE_CHOICES],
                                 state="readonly", width=34),
                    "when", "Where in the print its g-code is written. A "
                            "layer or scaffold choice repeats at each one.")
        # the numbers a choice needs, in one row that rebuilds itself
        self.nums = ttk.Frame(right)
        self.nums.grid(row=4, column=1, columnspan=2, sticky="w",
                       pady=(0, SP["xs"]))
        self.l_every = ttk.Label(self.nums, text="Every")
        self.e_every = ttk.Entry(self.nums, textvariable=self.v_every, width=6)
        self.l_from = ttk.Label(self.nums, text="from")
        self.e_first = ttk.Entry(self.nums, textvariable=self.v_first, width=6)
        self.l_to = ttk.Label(self.nums, text="to")
        self.e_last = ttk.Entry(self.nums, textvariable=self.v_last, width=6)
        self.l_n = ttk.Label(self.nums, text="")
        self.e_n = ttk.Entry(self.nums, textvariable=self.v_n, width=8)
        self.l_unit = ttk.Label(self.nums, text="", style="Hint.TLabel")
        self.l_per = ttk.Label(self.nums, text="", style="Hint.TLabel")
        for e, tip in ((self.e_every, "Run on every Nth one: 1 is each, 5 is "
                                     "every fifth."),
                       (self.e_first, "Optional: start at this one (1 is the "
                                     "first). Blank is from the start."),
                       (self.e_last, "Optional: stop after this one. Blank "
                                    "is to the end."),
                       (self.e_n, "The number this choice uses.")):
            add_tip(e, tip)
        ttk.Label(right, text="G-code").grid(row=5, column=0, sticky="nw",
                                             padx=(0, SP["m"]), pady=2)
        tf = ttk.Frame(right)
        tf.grid(row=5, column=1, columnspan=2, sticky="nsew")
        tf.columnconfigure(0, weight=1)
        tf.rowconfigure(0, weight=1)
        self.text = tk.Text(tf, width=60, height=9, wrap="none", undo=True)
        gui_form.exempt(self.text, "editable multiline script g-code")
        self.text.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=sb.set)
        self.text.bind("<<Modified>>", self._text_modified, add="+")
        self.w["text"] = self.text
        add_tip(self.text, "One command per line, written in your "
                           "controller's language (the Printer & collector page's flavor). "
                           "[[layer]], [[z]] and the other placeholders "
                           "fill in as it runs.")
        pl = ttk.Frame(right)
        pl.grid(row=6, column=1, columnspan=2, sticky="ew", pady=(SP["xs"], 0))
        pl.columnconfigure(0, weight=1)
        self.problem = tk.StringVar(value="")
        ttk.Label(pl, textvariable=self.problem, wraplength=470,
                  justify="left", foreground="#c0392b").grid(
            row=0, column=0, sticky="w")
        b = ttk.Button(pl, text="Placeholders\u2026", command=self._show_ph)
        b.grid(row=0, column=1, sticky="e", padx=(SP["m"], 0))
        add_tip(b, "Every [[name]] you can write, and where each works.")
        ck = ttk.Checkbutton(right, variable=self.v_auto,
                             text="Run automatically on every print",
                             command=self._edited)
        ck.grid(row=7, column=0, columnspan=3, sticky="w", pady=(SP["m"], 0))
        self.w["auto"] = ck
        add_tip(ck, "On: this script is written into every g-code the app "
                    "makes, at the place above. It shows in the G-code tree "
                    "as a locked row, and in Printer & collector > G-code > Automatic "
                    "commands. Off: it is only here, for you to use in a "
                    "file when you want it.")
        self.used = ttk.Label(right, style="Hint.TLabel", wraplength=560,
                              justify="left")
        self.used.grid(row=8, column=0, columnspan=3, sticky="w",
                       pady=(SP["xs"], 0))

        # -- the buttons --------------------------------------------------- #
        bar = ttk.Frame(outer)
        bar.grid(row=3, column=0, columnspan=2, sticky="ew",
                 pady=(SP["m"], 0))
        bar.columnconfigure(3, weight=1)
        self.save_btn = ttk.Button(bar, text="Save", style="Primary.TButton",
                                   command=self.save)
        self.save_btn.grid(row=0, column=0)
        add_tip(self.save_btn, "Save your changes to My library.")
        self.revert_btn = ttk.Button(bar, text="Revert", command=self.revert)
        self.revert_btn.grid(row=0, column=1, padx=(SP["xs"], 0))
        add_tip(self.revert_btn, "Drop the changes you have not saved.")
        self.delete_btn = ttk.Button(bar, text="Delete", command=self.delete)
        self.delete_btn.grid(row=0, column=2, padx=(SP["xs"], 0))
        add_tip(self.delete_btn, "Take this script out of My library (press "
                                 "twice). Edits already in a file stay there.")
        self.status = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.status, wraplength=380,
                  justify="left").grid(row=0, column=3, sticky="w",
                                       padx=SP["m"])
        self.use_btn = ttk.Button(bar, text="Use in this g-code",
                                  command=self.use_here)
        self.use_btn.grid(row=0, column=4)
        add_tip(self.use_btn, "Add the saved script to the g-code open on "
                              "the G-code page. It opens there so you can "
                              "choose where it goes.")
        b = ttk.Button(bar, text="Header and end\u2026",
                       command=self.to_machine)
        b.grid(row=0, column=5, padx=(SP["xs"], 0))
        add_tip(b, "The Printer & collector page's G-code card: the header, the end "
                   "block and the automatic commands.")
        ttk.Button(bar, text="Close", command=self.close).grid(
            row=0, column=6, padx=(SP["xs"], 0))
        for var in (self.v_name, self.v_desc, self.v_when, self.v_every,
                    self.v_first, self.v_last, self.v_n):
            var.trace_add("write", lambda *_a: self._edited())
        self.v_when.trace_add("write", lambda *_a: self._layout_nums())
        top.bind("<Escape>", lambda _e: self.close())
        self.refresh(select=None)

    # ------------------------------------------------------------------ #
    def _field(self, parent, r, label, widget, key, tip):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w",
                                           padx=(0, gui_fonts.SP["m"]), pady=2)
        widget.grid(row=r, column=1, columnspan=2, sticky="w", pady=2)
        add_tip(widget, tip)
        self.w[key] = widget

    def alive(self):
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False

    def _say(self, text):
        self.status.set(text)

    def _flavor(self):
        return getattr(self.app, "_machine_flavor_name", lambda: "klipper")()

    # -- the list ------------------------------------------------------------ #
    def refresh(self, select=-1):
        """Rebuild the list from My library; keep the selection (or go to
        `select`). With unsaved edits open, the form is left as it is."""
        lib = self.app._scripts_all()
        self._lib = lib
        keep = self.current if select == -1 else select
        tree = self.tree
        self._selecting = True
        try:
            tree.delete(*tree.get_children(""))
            mine = [r for r in lib if not r.builtin]
            ex = [r for r in lib if r.builtin]
            tree.insert("", "end", iid="h:mine", text="My scripts", open=True,
                        tags=("head",))
            if self.draft:
                tree.insert("h:mine", "end", iid="s:#draft",
                            text=(self.v_name.get() or "New script")
                            + "  (not saved)")
            for r in mine:
                tree.insert("h:mine", "end", iid="s:" + r.name, text=r.name,
                            tags=("auto",) if r.automatic else ())
            if not mine and not self.draft:
                tree.insert("h:mine", "end", iid="h:none",
                            text="Nothing yet. Press New script.",
                            tags=("head",))
            tree.insert("", "end", iid="h:ex", text="Examples", open=True,
                        tags=("head",))
            for r in ex:
                tree.insert("h:ex", "end", iid="x:" + r.name, text=r.name,
                            tags=("builtin",))
        finally:
            self._selecting = False
        used = {}
        m = self.app._gce_model_now() if hasattr(self.app, "_gce_model_now")             else None
        for c in (m.changes if m is not None else ()):
            if c.saved:
                used[c.saved] = used.get(c.saved, 0) + 1
        self._used = used
        if self.draft:
            self._select_iid("s:#draft")
        elif keep and tree.exists("s:" + keep):
            self._select_iid("s:" + keep,
                             force=not (self.dirty and keep == self.current))
        elif keep and tree.exists("x:" + keep):
            self._select_iid("x:" + keep)
        elif mine:
            self._select_iid("s:" + mine[0].name)
        elif ex:
            self._select_iid("x:" + ex[0].name)
        self._buttons()

    def _select_iid(self, iid, force=True):
        self._selecting = True
        try:
            self.tree.selection_set(iid)
            self.tree.see(iid)
        finally:
            self._selecting = False
        if force:
            self._show_iid(iid)

    def select_name(self, name):
        if self.dirty and name != self.current:
            self._say("You have unsaved changes: Save or Revert first.")
            return
        if self.draft:
            self.draft = False
        self.refresh(select=name)

    def _picked(self, _e=None):
        if getattr(self, "_selecting", False):
            return
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid == self._shown_iid:
            return                       # our own selection, delivered late
        if iid.startswith("h:"):
            self._restore_selection()
            return
        if (self.dirty or self.draft) and iid != self._shown_iid:
            if self.draft and not self.dirty and \
                    not (self.v_name.get().strip() or self.text.get("1.0", "end-1c").strip()):
                self.draft = False        # an untouched draft just goes
                self.refresh(select=None)
                self._select_iid(iid)
                return
            self._say("You have unsaved changes: Save or Revert first.")
            self._restore_selection()
            return
        self._show_iid(iid)

    _shown_iid = None

    def _restore_selection(self):
        if self._shown_iid and self.tree.exists(self._shown_iid):
            self._select_iid(self._shown_iid, force=False)

    def _show_iid(self, iid):
        self._shown_iid = iid
        if iid == "s:#draft":
            return
        name = iid[2:]
        rec = next((r for r in self._lib
                    if r.name == name and r.builtin == iid.startswith("x:")),
                   None)
        if rec is not None:
            self.load(rec)

    # -- loading and reading the form ----------------------------------------- #
    def _rules(self, rec):
        return [r for r in rec.rules if isinstance(r, mew_gcode.Rule)]

    def _simple(self, rec):
        return len(rec.rules) == 1 and isinstance(rec.rules[0], mew_gcode.Rule)

    def load(self, rec):
        """Show a script in the form."""
        self._loading = True
        try:
            self.rec = copy.deepcopy(rec)
            self.current = None if rec.builtin else rec.name
            self.draft = False
            self.v_name.set(rec.name)
            self.v_desc.set(rec.description)
            self.v_auto.set(bool(rec.automatic))
            self.problem.set("")
            rules = self._rules(rec)
            r = rules[0] if rules else None
            self.v_when.set(_WHERE_LABEL.get(r.where, "") if r else "")
            self.v_every.set(_num(r.every) if r and r.every else "1")
            self.v_first.set(_num(r.first) if r and r.first not in (None, "")
                             else "")
            self.v_last.set(_num(r.last) if r and r.last not in (None, "")
                            else "")
            self.v_n.set(_num(r.n) if r and r.n else "")
            if r is not None:
                try:
                    lines = mew_gcode.edit_lines(r, self._flavor())
                except Exception:                         # noqa: BLE001
                    lines = (r.text or "").splitlines()
                body = "\n".join(lines)
            else:
                body = ""
            self._body0 = body
            self._set_text(body)
            self._layout_nums()
            simple = self._simple(rec)
            locked = rec.builtin
            self.banner.set(
                "An example. Duplicate it to change it, or tick Run "
                "automatically on your copy." if locked else
                "" if simple else
                "This script has %d parts, so only its name, notes and "
                "switch can be changed here. To change a part, press Use in "
                "this g-code, change it in the tree, and save it again." %
                len(rec.rules))
            for key in ("name", "desc"):
                self.w[key].configure(state="disabled" if locked else "normal")
            edit_parts = "disabled" if (locked or not simple) else "normal"
            self.w["when"].configure(
                state="disabled" if edit_parts == "disabled" else "readonly")
            for e in (self.e_every, self.e_first, self.e_last, self.e_n):
                e.configure(state=edit_parts)
            self.text.configure(state=edit_parts)
            self.w["auto"].configure(state="disabled" if locked else "normal")
            n = self._used.get(rec.name, 0) if not locked else 0
            self.used.configure(
                text=("In this g-code %d time%s. Changing the script here "
                      "does not change those copies." %
                      (n, "" if n == 1 else "s")) if n else "")
        finally:
            self._loading = False
        self.dirty = False
        self._delete_armed = False
        self.delete_btn.configure(text="Delete")
        self._say("")
        self._buttons()

    def _set_text(self, body):
        st = str(self.text.cget("state"))
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", body)
        self.text.edit_modified(False)
        self.text.edit_reset()
        self.text.configure(state=st)

    def _layout_nums(self):
        for w in (self.l_every, self.e_every, self.l_from, self.e_first,
                  self.l_to, self.e_last, self.l_n, self.e_n, self.l_unit,
                  self.l_per):
            w.grid_forget()
        where = _WHERE_KEY.get(self.v_when.get(), "")
        c = 0
        pad = (gui_fonts.SP["xs"], gui_fonts.SP["s"])
        if where in _PER_PART:
            part = "layer" if "layer" in where else "scaffold or piece"
            self.l_every.configure(text="Every")
            self.l_per.configure(text=part)
            for w, p in ((self.l_every, (0, 4)), (self.e_every, (0, 4)),
                         (self.l_per, (0, 14)), (self.l_from, (0, 4)),
                         (self.e_first, (0, 4)), (self.l_to, (0, 4)),
                         (self.e_last, (0, 4))):
                w.grid(row=0, column=c, padx=p)
                c += 1
            self.l_from.configure(text="from #")
            self.l_to.configure(text="to #")
        elif where in _NUMBERED:
            lab, unit = _NUMBERED[where]
            self.l_n.configure(text=lab)
            self.l_unit.configure(text=unit)
            for w, p in ((self.l_n, (0, 4)), (self.e_n, (0, 4)),
                         (self.l_unit, (0, 0))):
                w.grid(row=0, column=c, padx=p)
                c += 1

    def _text_modified(self, _e=None):
        if self.text.edit_modified():
            self.text.edit_modified(False)
            if not self._loading:
                self._edited()

    def _edited(self):
        if self._loading or self.rec is None:
            return
        if self.rec.builtin:
            return
        self.dirty = True
        self._delete_armed = False
        self.delete_btn.configure(text="Delete")
        probs = placeholder_problems(self.text.get("1.0", "end-1c"),
                                     rule_ok=True)
        self.problem.set(" ".join(probs))
        self._say("Unsaved changes." if not self.draft else
                  "Not saved yet: press Save to keep it in My library.")
        if self.draft and str(self.tree.selection()) :
            iid = "s:#draft"
            if self.tree.exists(iid):
                self.tree.item(iid, text=(self.v_name.get() or "New script")
                               + "  (not saved)")
        self._buttons()

    def _buttons(self):
        rec = self.rec
        locked = rec is not None and rec.builtin
        saved = rec is not None and not locked and not self.draft
        self.save_btn.configure(
            state="normal" if (self.dirty and not locked) else "disabled")
        self.revert_btn.configure(
            state="normal" if (self.dirty or self.draft) else "disabled")
        self.delete_btn.configure(
            state="normal" if (saved or self.draft) else "disabled")
        can_use = saved and hasattr(self.app, "_gce_use_recipe") \
            and self.app._gce_model_now() is not None
        self.use_btn.configure(state="normal" if can_use else "disabled")

    def _show_ph(self):
        messagebox.showinfo("Placeholders", placeholder_help(),
                            parent=self.top)

    # -- building a Recipe from the form --------------------------------------- #
    def collect(self):
        """The Recipe the form describes, or raise ValueError with a
        sentence."""
        eng = mew_gcode
        name = self.v_name.get().strip()
        if not name:
            raise ValueError("Give the script a name.")
        base = copy.deepcopy(self.rec)
        rec = eng.Recipe(name, base.rules, bool(self.v_auto.get()), False,
                         self.v_desc.get().strip())
        if not self._simple(base):
            if not rec.rules:
                raise ValueError("This script has nothing in it.")
            return rec
        r = copy.deepcopy(base.rules[0])
        where = _WHERE_KEY.get(self.v_when.get())
        if not where:
            raise ValueError("Choose when it runs.")
        body = self.text.get("1.0", "end-1c").rstrip("\n")
        if not body.strip() and not r.command:
            raise ValueError("Write the g-code it should send.")
        bad = placeholder_problems(body, rule_ok=True)
        if bad:
            raise ValueError(bad[0])
        r.where = where
        r.name = name
        if body != self._body0:
            r.text, r.command, r.flavor = body, None, self._flavor()

        def whole(var, label, minimum=1, required=False):
            raw = var.get().strip()
            if not raw:
                if required:
                    raise ValueError("%s is empty." % label)
                return None
            try:
                v = int(float(raw))
            except ValueError:
                raise ValueError("%s must be a whole number, not \u201c%s\u201d."
                                 % (label, raw))
            if v < minimum:
                raise ValueError("%s must be %d or more." % (label, minimum))
            return v
        if where in _PER_PART:
            r.every = whole(self.v_every, "Every", 1) or 1
            r.first = whole(self.v_first, "From #", 1)
            r.last = whole(self.v_last, "To #", 1)
            if r.first and r.last and r.last < r.first:
                raise ValueError("To # is before From #.")
            r.n = 0.0
        elif where in _NUMBERED:
            raw = self.v_n.get().strip()
            try:
                r.n = float(raw)
            except ValueError:
                raise ValueError("%s must be a number." % _NUMBERED[where][0])
            if where != "at_z" and r.n <= 0:
                raise ValueError("%s must be more than 0." % _NUMBERED[where][0])
            r.every, r.first, r.last = 1, None, None
        else:
            r.every, r.first, r.last, r.n = 1, None, None, 0.0
        rec.rules = [r]
        return rec

    # -- the buttons ------------------------------------------------------------- #
    def save(self):
        if self.rec is None or self.rec.builtin:
            return
        try:
            rec = self.collect()
        except ValueError as exc:
            self._say(str(exc))
            return
        old = self.current
        lib = self.app._scripts_all()
        clash = any((not r.builtin) and r.name == rec.name and r.name != old
                    for r in lib)
        if clash:
            self._say("Another script is already called \u201c%s\u201d." %
                      rec.name)
            return
        lib = [r for r in lib if r.builtin or r.name not in (rec.name, old)]
        lib.append(rec)
        m = self.app._gce_model_now() if hasattr(self.app, "_gce_model_now") \
            else None
        if old and old != rec.name and m is not None:
            for c in m.changes:             # its copies in the file follow
                if c.saved == old:
                    c.saved = rec.name
        self.draft = False
        self.dirty = False
        self.current = rec.name
        self.app._gce_write_library(lib)     # -> _scripts_changed -> refresh
        self.refresh(select=rec.name)
        self._say("Saved. %s" % (
            "It runs on every print from now on." if rec.automatic else ""))

    def revert(self):
        if self.draft:
            self.draft = False
            self.dirty = False
            self.rec = None
            self.refresh(select=None)
            self._say("")
            return
        if self.rec is not None:
            was = next((r for r in self._lib if not r.builtin and
                        r.name == self.current), None)
            if was is not None:
                self.load(was)
                self._say("Back to what is saved.")

    def new(self):
        if self.dirty and not self.draft:
            self._say("You have unsaved changes: Save or Revert first.")
            return
        rule = mew_gcode.Rule(name="", text="", where="after_layer",
                              flavor=self._flavor())
        self.load(mew_gcode.Recipe("", [rule]))
        self.draft = True
        self.refresh(select=None)
        self.banner.set("A new script. Name it, say when it runs and write "
                        "its g-code, then Save.")
        self._say("Not saved yet.")
        self.w["name"].focus_set()
        self._buttons()

    def duplicate(self):
        if self.rec is None:
            return
        if self.dirty:
            self._say("You have unsaved changes: Save or Revert first.")
            return
        src = self.rec
        lib = self.app._scripts_all()
        taken = {r.name for r in lib if not r.builtin}
        name = "%s (copy)" % src.name
        n = 2
        while name in taken:
            name = "%s (copy %d)" % (src.name, n)
            n += 1
        dup = copy.deepcopy(src)
        dup.name, dup.builtin, dup.automatic = name, False, False
        for r in dup.rules:
            if isinstance(r, mew_gcode.Rule):
                r.name, r.saved = name, name
        lib.append(dup)
        self.app._gce_write_library(lib)
        self.refresh(select=name)
        self._say("Copied. Change it and Save.")

    def delete(self):
        if self.draft:
            self.revert()
            return
        if self.rec is None or self.rec.builtin or not self.current:
            return
        if not self._delete_armed:
            self._delete_armed = True
            self.delete_btn.configure(text="Sure? Delete")
            self._say("Press again to take it out of My library. Edits "
                      "already in a file stay there.%s" % (
                          " It stops running on every print."
                          if self.rec.automatic else ""))
            return
        name = self.current
        lib = [r for r in self.app._scripts_all()
               if r.builtin or r.name != name]
        m = self.app._gce_model_now() if hasattr(self.app, "_gce_model_now") \
            else None
        for c in (m.changes if m is not None else ()):
            if c.saved == name:
                c.saved = ""
        self.current = None
        self.dirty = False
        self._delete_armed = False
        self.delete_btn.configure(text="Delete")
        self.app._gce_write_library(lib)
        self.refresh(select=None)
        self._say("\u201c%s\u201d is out of My library." % name)

    def use_here(self):
        if self.dirty or self.draft or self.rec is None or self.rec.builtin:
            self._say("Save it first, then use it.")
            return
        rec = next((r for r in self.app._scripts_all()
                    if not r.builtin and r.name == self.current), None)
        if rec is None:
            return
        app = self.app
        if hasattr(app, "_nav_select"):
            app._nav_select("gcode")
        self.close(force=True)
        app.root.after_idle(lambda: app._gce_use_recipe(rec))

    def to_machine(self):
        self.close(force=True)
        self.app._open_machine_gcode_card()

    def select_label(self, name):
        self.select_name(name)

    def close(self, force=False):
        if (self.dirty or self.draft and (self.v_name.get().strip()
                                          or self.text.get("1.0", "end-1c").strip())) \
                and not force and not self._close_armed:
            self._close_armed = True
            self._say("Unsaved changes. Press Close again to throw them away.")
            return
        self._close_armed = False
        self.dirty = False
        self.draft = False
        try:
            self.top.destroy()
        except tk.TclError:
            pass
        if getattr(self.app, "_script_lib", None) is self:
            self.app._script_lib = None
