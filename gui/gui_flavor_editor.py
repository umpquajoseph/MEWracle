#!/usr/bin/env python3
"""gui_flavor_editor.py -- the g-code flavor editor: ONE window, opened from
the Machine page ("Edit flavors...") and from the G-code page (Syntax >
Edit flavors...), so a lab sees and changes its flavors the same way in both.

What a flavor is lives in `mew_gcode.Flavor`; the store is `flavor_presets`.
This window only shows them: a list of every flavor (the five built-ins,
then the lab's own; New / Duplicate / Rename / Delete, Import / Export share
one as a file, Open folder shows where they are kept), and the selected one's
rows in the sections `mew_gcode.FLAVOR_GROUPS` names.

A built-in is edited in place like any flavor, except its Name (presets and
files on every computer know it by it). The store keeps the edit apart from
the shipped flavor, so the list marks it "edited", each changed row shows the
shipped word beside it, and Reset to default (in Delete's place, two presses)
brings the shipped flavor back. New starts from plain G-code with no process
commands (`flavor_presets.new`); Duplicate copies the selected one. Rename
puts the cursor in the Name row; Enter or Save renames.

Edits are explicit: Save writes them, Revert drops them, and nothing else --
switching flavor, Use, Close -- may lose them silently: each says so on the
window's status line first (the G-code page's "no popups" rule, applied here
too).

The opener passes `on_use(label)`: what "Use this flavor" does for it (set
the Machine page's flavor, or the G-code document's). After any change the
window calls `app._flavors_changed(renamed, deleted, edited)`, which every flavor
menu, the machine preset's carried copy and the open document follow."""

from gui_common import *
import flavor_presets as fp
import gui_fonts
import gui_widgets
import mew_gcode
from gui_tips import add_tip

#: rows that pick from a fixed list, with the list
_CHOICES = {"dwell_unit": ("ms", "s", ""), "rotary_feed": ("combined", "split"),
            "line_numbers": ("off", "on")}
_LABELS = {f: (lab, ex) for f, lab, ex in mew_gcode.FLAVOR_FIELDS}


class FlavorEditor:
    """The flavor editor window. One per app: `open` reuses it."""

    @classmethod
    def open(cls, app, select=None, on_use=None, use_text=None):
        ed = getattr(app, "_flavor_editor", None)
        if ed is not None and ed.alive():
            ed.set_use(on_use, use_text)
            if select:
                ed.select_label(select)
            ed.top.deiconify()
            ed.top.lift()
            return ed
        ed = cls(app, select, on_use, use_text)
        app._flavor_editor = ed
        return ed

    def __init__(self, app, select=None, on_use=None, use_text=None):
        self.app = app
        self.directory = app._flavors_dir()
        self.on_use = on_use
        self.current = None              # the selected flavor's name
        self._loading = False
        self._close_armed = False
        self._delete_armed = False
        top = self.top = tk.Toplevel(app.root)
        top.title("G-code flavors")
        top.transient(app.root)
        gui_fonts.size_dialog(top, 1000, 700, 760, 480)
        top.protocol("WM_DELETE_WINDOW", self.close)
        outer = ttk.Frame(top, padding=gui_fonts.SP["l"])
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)
        ttk.Label(outer, text="G-code flavors", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w")

        # -- the list ---------------------------------------------------- #
        left = ttk.Frame(outer)
        left.grid(row=1, column=0, sticky="nsw", padx=(0, gui_fonts.SP["l"]),
                  pady=(gui_fonts.SP["s"], 0))
        left.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse",
                                 height=18)
        self.tree.column("#0", width=gui_fonts.dpi_px(left, 250))
        self.tree.grid(row=0, column=0, columnspan=2, sticky="nsw")
        self.tree.tag_configure("builtin", foreground="#7a7a7a")
        self.tree.bind("<<TreeviewSelect>>", self._picked)
        b = ttk.Button(left, text="New", command=self.new)
        b.grid(row=1, column=0, sticky="ew", pady=(gui_fonts.SP["s"], 0))
        add_tip(b, "Make a new flavor from scratch: plain G-code moves (G1, "
                   "G0, G2/G3, G90/G91, G28, G21, G4) and no pause, heat, "
                   "message, pressure or voltage commands until you type "
                   "your controller's.")
        b = ttk.Button(left, text="Duplicate", command=self.duplicate)
        b.grid(row=1, column=1, sticky="ew", padx=(gui_fonts.SP["xs"], 0),
               pady=(gui_fonts.SP["s"], 0))
        add_tip(b, "Make a copy of the selected flavor to change, keeping "
                   "the original: \"Duplicate Aerotech and edit\".")
        self.rename_btn = ttk.Button(left, text="Rename",
                                     command=self.rename)
        self.rename_btn.grid(row=2, column=0, sticky="ew",
                             pady=(gui_fonts.SP["xs"], 0))
        add_tip(self.rename_btn, "Rename the selected flavor of yours: type "
                                 "the new name, then press Enter or Save. "
                                 "Printer presets and g-code that used the "
                                 "old name follow it. A built-in keeps its "
                                 "name.")
        self.delete_btn = ttk.Button(left, text="Delete", command=self.delete)
        self.delete_btn.grid(row=2, column=1, sticky="ew",
                             padx=(gui_fonts.SP["xs"], 0),
                             pady=(gui_fonts.SP["xs"], 0))
        add_tip(self.delete_btn, "Delete the selected flavor of yours. A "
                                 "printer preset that used it still carries "
                                 "a copy and brings it back when loaded. "
                                 "On a built-in this is Reset to default: "
                                 "it brings back the words MEWracle ships.")
        b = ttk.Button(left, text="Import\u2026", command=self.import_files)
        b.grid(row=3, column=0, sticky="ew", pady=(gui_fonts.SP["xs"], 0))
        add_tip(b, "Add flavors from files another lab shared. A name you "
                   "already have is kept as \"Name (2)\": an import never "
                   "replaces one of yours.")
        self.export_btn = ttk.Button(left, text="Export\u2026",
                                     command=self.export)
        self.export_btn.grid(row=3, column=1, sticky="ew",
                             padx=(gui_fonts.SP["xs"], 0),
                             pady=(gui_fonts.SP["xs"], 0))
        add_tip(self.export_btn, "Save the selected flavor as a file to "
                                 "share with another printer or lab (a "
                                 "built-in once you have changed it).")
        b = ttk.Button(left, text="Open folder", command=self.open_folder)
        b.grid(row=4, column=0, columnspan=2, sticky="ew",
               pady=(gui_fonts.SP["xs"], 0))
        add_tip(b, "Show the folder your flavors are saved in, one file "
                   "each.")

        # -- the selected flavor's rows ----------------------------------- #
        right = ttk.Frame(outer)
        right.grid(row=1, column=1, sticky="nsew", pady=(gui_fonts.SP["s"], 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        self.banner_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.banner_var, style="Hint.TLabel",
                  wraplength=640, justify="left").grid(row=0, column=0,
                                                       sticky="w")
        sf = VScrollFrame(right)
        sf.grid(row=1, column=0, sticky="nsew", pady=(gui_fonts.SP["s"], 0))
        body = sf.body
        body.columnconfigure(1, weight=1)
        self.vars, self.widgets, self.hints = {}, {}, {}
        r = 0
        self._row(body, r, "label")
        r += 1
        for title, intro, names in mew_gcode.FLAVOR_GROUPS:
            gui_widgets.section_header(body, title).grid(
                row=r, column=0, columnspan=3, sticky="w",
                pady=(gui_fonts.SP["l"], 0))
            ttk.Label(body, text=intro, style="Hint.TLabel", wraplength=600,
                      justify="left").grid(row=r + 1, column=0, columnspan=3,
                                           sticky="w",
                                           pady=(0, gui_fonts.SP["xs"]))
            r += 2
            for name in names:
                self._row(body, r, name)
                r += 1

        # -- the buttons --------------------------------------------------- #
        bar = ttk.Frame(outer)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew",
                 pady=(gui_fonts.SP["m"], 0))
        bar.columnconfigure(2, weight=1)
        self.save_btn = ttk.Button(bar, text="Save", command=self.save)
        self.save_btn.grid(row=0, column=0)
        add_tip(self.save_btn, "Save your changes to this flavor (a new "
                               "Name renames it).")
        self.revert_btn = ttk.Button(bar, text="Revert", command=self.revert)
        self.revert_btn.grid(row=0, column=1, padx=(gui_fonts.SP["xs"], 0))
        add_tip(self.revert_btn, "Drop the changes you have not saved.")
        self.status_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.status_var, wraplength=420,
                  justify="left").grid(row=0, column=2, sticky="w",
                                       padx=gui_fonts.SP["m"])
        self.use_btn = ttk.Button(bar, text="Use this flavor",
                                  command=self.use)
        self.use_btn.grid(row=0, column=3)
        ttk.Button(bar, text="Close", command=self.close).grid(
            row=0, column=4, padx=(gui_fonts.SP["xs"], 0))
        self.set_use(on_use, use_text)
        self.refresh(select or None)

    # ------------------------------------------------------------------ #
    def _row(self, body, r, name):
        label, example = _LABELS[name]
        var = tk.StringVar(value="")
        if name in _CHOICES:
            w = ttk.Combobox(body, textvariable=var, values=_CHOICES[name],
                             state="readonly", width=12)
        else:
            w = ttk.Entry(body, textvariable=var, width=34)
        ttk.Label(body, text=label).grid(row=r, column=0, sticky="w",
                                         padx=(0, gui_fonts.SP["m"]),
                                         pady=2)
        w.grid(row=r, column=1, sticky="w", pady=2)
        hint = "e.g. " + example if name not in _CHOICES else ""
        hl = ttk.Label(body, text=hint, style="Hint.TLabel")
        hl.grid(row=r, column=2, sticky="w", padx=(gui_fonts.SP["s"], 0))
        self.hints[name] = (hl, hint)
        tip = mew_gcode.FLAVOR_TIPS.get(name) or (
            "%s in your controller's own words, e.g. %s. Empty means it "
            "has none." % (label, example))
        add_tip(w, tip)
        var.trace_add("write", lambda *_a: self._edited())
        if name == "label":
            w.bind("<Return>", lambda _e: self.save())
            w.bind("<KP_Enter>", lambda _e: self.save())
        self.vars[name] = var
        self.widgets[name] = w

    def alive(self):
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False

    def set_use(self, on_use, use_text=None):
        self.on_use = on_use
        self.use_btn.configure(text=use_text or "Use this flavor")
        if on_use is None:
            self.use_btn.grid_remove()
        else:
            self.use_btn.grid()

    def flavor(self):
        return mew_gcode.FLAVORS.get(self.current or "")

    # -- the list ------------------------------------------------------- #
    def refresh(self, select_label=None):
        """Rebuild the list; select `select_label`, else the current one."""
        keep = select_label or (self.flavor().label if self.flavor() else None)
        self.tree.delete(*self.tree.get_children())
        for f in fp.builtin_flavors():
            self.tree.insert("", "end", iid=f.name, text=f.label + (
                "  · built in, edited" if fp.is_edited(f) else
                "  · built in"), tags=("builtin",))
        for f in fp.saved_flavors():
            self.tree.insert("", "end", iid=f.name, text=f.label)
        target = fp.by_label(keep) if keep else None
        self._show((target or mew_gcode.FLAVORS["klipper"]).name)

    def select_label(self, label):
        f = fp.by_label(label)
        if f is not None and f.name != self.current:
            if self.dirty():
                self._say("You have unsaved changes to %s. Save or Revert "
                          "them first." % self.flavor().label)
                return False
            self._show(f.name)
        return True

    def _picked(self, _e=None):
        sel = self.tree.selection()
        if not sel or sel[0] == self.current:
            return
        if self.dirty():
            self._say("You have unsaved changes to %s. Save or Revert them "
                      "first." % self.flavor().label)
            self._loading = True
            try:
                self.tree.selection_set(self.current)
            finally:
                self._loading = False
            return
        self._show(sel[0])

    def _show(self, name):
        if name not in mew_gcode.FLAVORS:      # deleted from outside
            name = "klipper"
        self.current = name
        self._delete_armed = False
        self.delete_btn.configure(text="Delete")
        self._loading = True
        try:
            if self.tree.exists(name):
                self.tree.selection_set(name)
                self.tree.see(name)
            vals = self.saved_values()
            for f, var in self.vars.items():
                var.set(vals.get(f, ""))
        finally:
            self._loading = False
        try:
            self.widgets["label"].selection_clear()   # a Rename's highlight
        except tk.TclError:
            pass
        fl = self.flavor()
        bi = fl.builtin
        for f, w in self.widgets.items():
            if isinstance(w, ttk.Combobox):
                w.configure(state="readonly")
            else:
                w.configure(state="readonly" if bi and f == "label"
                            else "normal")
        self.rename_btn.configure(state="disabled" if bi else "normal")
        self.export_btn.configure(state="normal" if not bi
                                  or fp.is_edited(fl) else "disabled")
        self._paint_shipped()
        self._close_armed = False
        self._edited()

    def _paint_shipped(self):
        """The built-in's banner, Reset button and per-row shipped words."""
        fl = self.flavor()
        ship = fp.shipped(fl.name) if fl.builtin else None
        ship_vals = mew_gcode.flavor_form_values(ship) if ship else {}
        now = self.saved_values()
        changed = [f for f in self.vars
                   if ship and f != "label" and now.get(f) != ship_vals.get(f)]
        for f, (hl, hint) in self.hints.items():
            if f in changed:
                hl.configure(text="shipped: %s" % (ship_vals.get(f)
                                                   or "(empty)"))
            else:
                hl.configure(text=hint)
        if fl.builtin:
            self.delete_btn.configure(
                text="Reset to default",
                state="normal" if changed else "disabled")
            if changed:
                self.banner_var.set(
                    "%s ships with MEWracle; you have changed %d row%s "
                    "(the shipped words are beside them). Your version is "
                    "kept on this computer through restarts and updates. "
                    "Reset to default brings back the shipped one."
                    % (fl.label, len(changed),
                       "" if len(changed) == 1 else "s"))
            else:
                self.banner_var.set(
                    "%s ships with MEWracle. Change any row and Save: your "
                    "version is kept on this computer through restarts and "
                    "updates, and Reset to default always brings back the "
                    "shipped one. Its name stays." % fl.label)
        else:
            self.delete_btn.configure(text="Delete", state="normal")
            self.banner_var.set(
                "Your flavor. Change any row, then Save. Every word here is "
                "how MEWracle writes that command for your controller and "
                "reads it back.")

    # -- edits ---------------------------------------------------------- #
    def saved_values(self):
        return mew_gcode.flavor_form_values(self.flavor())

    def values(self):
        return {f: v.get() for f, v in self.vars.items()}

    def dirty(self):
        fl = self.flavor()
        if fl is None:
            return False
        return self.values() != self.saved_values()

    def _edited(self):
        if self._loading:
            return
        d = self.dirty()
        self.save_btn.configure(state="normal" if d else "disabled")
        self.revert_btn.configure(state="normal" if d else "disabled")
        self._close_armed = False
        if d:
            self._say("Not saved yet.")
        elif self.status_var.get() == "Not saved yet.":
            self._say("")

    def _say(self, text):
        self.status_var.set(text)

    def revert(self):
        if self.current:
            self._show(self.current)
            self._say("Changes dropped.")

    def save(self):
        """Write the rows to the selected flavor (a built-in's as the lab's
        edit of it; a new Name renames one of the lab's own)."""
        fl = self.flavor()
        if fl is None or not self.dirty():
            return False
        vals = self.values()
        label = fl.label if fl.builtin else vals.get("label", "").strip()
        if not label:
            self._say("A flavor needs a name.")
            return False
        if fp.label_taken(label, but=fl.name):
            self._say("Another flavor is already called %s." % label)
            return False
        if not vals.get("move_word", "").strip():
            self._say("A flavor needs its linear move word (G1 for G-code): "
                      "without one no move can be written.")
            return False
        old = fl.label
        new = mew_gcode.flavor_from_form(vals, base=fl.name, name=fl.name,
                                         label=label)
        try:
            fp.save(self.directory, new)
        except (OSError, ValueError) as exc:
            self._say("Not saved: %s" % exc)
            return False
        self.refresh(label)
        self._say("Renamed %s to %s." % (old, label) if old != label
                  else "Saved %s." % label)
        self.app._flavors_changed(renamed={old: label} if old != label
                                  else None, edited=fl.name)
        return True

    def _focus_name(self):
        w = self.widgets["label"]
        try:
            w.focus_set()
            w.select_range(0, "end")
            w.icursor("end")
        except tk.TclError:
            pass

    def rename(self):
        """Put the cursor in the Name row; Enter or Save renames."""
        fl = self.flavor()
        if fl is None:
            return False
        if fl.builtin:
            self._say("%s is built in and keeps its name: presets and files "
                      "on every computer know it by it. Duplicate it to "
                      "make one with a name of your own." % fl.label)
            return False
        self._focus_name()
        self._say("Type the new name for %s, then press Enter or Save."
                  % fl.label)
        return True

    def new(self):
        """A new flavor of yours from scratch (`flavor_presets.new`)."""
        if self.dirty():
            self._say("Save or Revert your changes to %s first."
                      % self.flavor().label)
            return None
        try:
            fl = fp.new(self.directory)
        except (OSError, ValueError) as exc:
            self._say("Not made: %s" % exc)
            return None
        self.refresh(fl.label)
        self._focus_name()
        self._say("Made %s: plain G-code moves, no process commands. Name "
                  "it, type your controller's words, then Save." % fl.label)
        self.app._flavors_changed()
        return fl

    def duplicate(self):
        fl = self.flavor()
        if fl is None:
            return None
        if self.dirty():
            self._say("Save or Revert your changes to %s first." % fl.label)
            return None
        try:
            new = fp.duplicate(self.directory, fl.name)
        except (OSError, ValueError) as exc:
            self._say("Not duplicated: %s" % exc)
            return None
        self.refresh(new.label)
        self._say("Made %s. Change its rows, then Save." % new.label)
        self.app._flavors_changed()
        return new

    def delete(self):
        """Two presses: the first asks on the button itself. On a built-in
        this is Reset to default."""
        fl = self.flavor()
        if fl is None:
            return False
        if fl.builtin:
            return self.reset()
        if not self._delete_armed:
            self._delete_armed = True
            self.delete_btn.configure(text="Delete: sure?")
            self._say("Press Delete again to delete %s." % fl.label)
            return False
        label = fl.label
        try:
            fp.delete(self.directory, fl.name)
        except (OSError, ValueError) as exc:
            self._say("Not deleted: %s" % exc)
            return False
        self.current = None
        self.refresh(mew_gcode.FLAVORS["klipper"].label)
        self._say("Deleted %s." % label)
        self.app._flavors_changed(deleted=label)
        return True

    def reset(self):
        """The selected built-in back to what MEWracle ships. Two presses,
        like Delete."""
        fl = self.flavor()
        if fl is None or not fl.builtin or not fp.is_edited(fl):
            return False
        if self.dirty():
            self._say("Save or Revert your changes to %s first." % fl.label)
            return False
        if not self._delete_armed:
            self._delete_armed = True
            self.delete_btn.configure(text="Reset: sure?")
            self._say("Press Reset again to bring back %s as MEWracle ships "
                      "it. Your changes to it are dropped." % fl.label)
            return False
        try:
            fp.reset(self.directory, fl.name)
        except (OSError, ValueError) as exc:
            self._say("Not reset: %s" % exc)
            return False
        self.refresh(fl.label)
        self._say("%s is back to the shipped default." % fl.label)
        self.app._flavors_changed(edited=fl.name)
        return True

    def import_files(self, paths=None):
        """Add the flavors in `paths` (asked for when None); returns them."""
        if self.dirty():
            self._say("Save or Revert your changes to %s first."
                      % self.flavor().label)
            return []
        if paths is None:
            paths = filedialog.askopenfilenames(
                parent=self.top, title="Import g-code flavors",
                filetypes=[("Flavor files", "*.json"), ("All files", "*.*")])
        added, bad = [], []
        for path in paths or ():
            try:
                added.append(fp.import_file(self.directory, path))
            except (OSError, ValueError) as exc:
                bad.append(str(exc))
        if added:
            self.refresh(added[-1].label)
            self.app._flavors_changed()
        msg = ("Imported %s." % ", ".join(f.label for f in added)
               if added else "")
        if bad:
            msg = (msg + " " if msg else "") + "Skipped: " + " ".join(bad)
        self._say(msg or "Nothing imported.")
        return added

    def export(self, path=None):
        """Write the selected flavor -- yours, or a built-in you changed --
        to `path` (asked for when None)."""
        fl = self.flavor()
        if fl is None or (fl.builtin and not fp.is_edited(fl)):
            return False
        if self.dirty():
            self._say("Save or Revert your changes to %s first." % fl.label)
            return False
        if path is None:
            import gui_prefs
            path = filedialog.asksaveasfilename(
                **gui_prefs.save_dir_kw(), parent=self.top,
                title="Export g-code flavor", defaultextension=".json",
                initialfile=fl.name[len("user-"):] + ".json"
                if fl.name.startswith("user-") else
                fl.name + "-edited.json" if fl.builtin else
                fl.name + ".json",
                filetypes=[("Flavor files", "*.json"), ("All files", "*.*")])
        if not path:
            return False
        try:
            fp.export(fl, path)
        except (OSError, ValueError) as exc:
            self._say("Not exported: %s" % exc)
            return False
        self._say("Exported %s to %s." % (fl.label, os.path.basename(path)))
        return True

    def open_folder(self):
        os.makedirs(self.directory, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(self.directory)  # noqa: S606 -- a folder
            else:
                subprocess.Popen(["open" if sys.platform == "darwin"
                                  else "xdg-open", self.directory])
        except OSError as exc:
            self._say("Couldn't open %s: %s" % (self.directory, exc))

    def use(self):
        fl = self.flavor()
        if fl is None or self.on_use is None:
            return
        if self.dirty():
            self._say("Save or Revert your changes to %s first." % fl.label)
            return
        self.on_use(fl.label)
        self._say("Now using %s." % fl.label)

    def close(self):
        if self.dirty() and not self._close_armed:
            self._close_armed = True
            self._say("You have unsaved changes to %s. Save them, or press "
                      "Close again to drop them." % self.flavor().label)
            return
        self.top.destroy()
        self.app._flavor_editor = None
