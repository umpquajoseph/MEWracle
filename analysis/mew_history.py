#!/usr/bin/env python3
"""mew_history.py -- headless Print History engine.

Records, stores, diffs and exports "runs" -- one per Generate. NO Tk/VTK import: pure
JSON/CSV I/O plus an OPTIONAL reportlab PDF run sheet (guarded by HAS_REPORTLAB). The
GUI half lives in gui_history.py; this module is the testable core.

Storage layout under base_dir (default <HERE>/history, git-ignored):
  runs.jsonl              append-only, one JSON record per FINISHED run (oldest first
                          on disk; load_runs() returns newest first)
  saved.json             {"saved": [record + note/tags/operator/saved_ts/gcode_copy]}
  thumbs/<id>.png        persistent fibre thumbnail (runs that produced an STL)
  saved_files/<id>.gcode optional archived g-code for a promoted run

A run record is a plain dict. The parameter snapshot is grouped by the four preset
kinds whose union partitions the persist map (gui_presets._keys_for_kind), and
recording, diffing and export all iterate them in PARAM_GROUPS order.
"""
import re
import csv
import datetime
import importlib.util
import json
import os
import secrets
import shutil

# The four preset kinds, in display order, with a human heading for each.
PARAM_GROUPS = ("scaffold", "machine", "calibration", "sweep")

# The HEADLINE parameters -- the small, curated set that belongs on a run sheet
# (process + CTS + the defining scaffold geometry). Everything else lives in the full
# snapshot for Reproduce; it never bloats the sheet. Each entry is (persist_key,
# label), looked up across ALL four param groups. Missing keys are skipped, so a
# g-code-import run simply shows fewer rows.
#
# A headline key normally mirrors a GUI field, so a captured run fills it in.
# MANUAL_HEADLINE_KEYS (below) is the exception: keys with NO capture field, e.g. an
# environmental reading the operator jots down. They have no default and stay blank
# until typed, so the edit dialog always offers them even on an auto-recorded run.
HEADLINE_SECTIONS = (
    ("Process", (
        ("s.speed", "Print speed"),
        ("cal.cts_ref", "Critical translation speed (CTS)"),
        ("sim.pressure", "Applied pressure"),
        ("sim.voltage", "Voltage"),
        ("sim.standoff", "Nozzle–collector gap"),
        ("sim.temperature", "Temperature"),
        ("env.humidity", "Relative humidity (%RH)"),
        ("cal.noz_inner_diameter", "Needle inner diameter"),
    )),
    ("Scaffold geometry", (
        ("s.layers", "Layers"),
        ("s.width", "Footprint width"),
        ("s.height", "Footprint height"),
        ("s.diameter", "Fiber diameter (layer height)"),
        ("s.rotation", "Rotation / pattern"),
    )),
)

# Each headline value's stored unit, and the display quantity that may convert
# it (gui_units' Settings choice: speed, pressure, fiber diameter). Without a
# unit the sheet read "Print speed 800" beside "CTS 8" -- mm/min against mm/s.
HEADLINE_UNITS = {
    "s.speed": ("speed", "mm/min"),
    "cal.cts_ref": ("speed", "mm/s"),
    "sim.pressure": ("pressure", "psi"),
    "sim.voltage": (None, "kV"),
    "sim.standoff": (None, "mm"),
    "sim.temperature": (None, "°C"),
    "cal.noz_inner_diameter": (None, "mm"),
    "s.width": (None, "mm"),
    "s.height": (None, "mm"),
    "s.diameter": ("fiber_d", "mm"),
}

# Angles, in degrees (direction page §5). The layer rotation is stored as text
# with its pattern name, "90 (square)", so the sign goes after the number.
DEGREE_KEYS = frozenset({"s.rotation"})


def _with_unit(key, v):
    """A headline value as the sheet prints it: in the display unit the user
    chose on the Settings panel, with that unit. Non-numbers pass through."""
    quantity, unit = HEADLINE_UNITS.get(key, (None, ""))
    text = _fmt(v)
    if key in DEGREE_KEYS:
        # stored humanised ("90 (square)"), so the unit goes after its number
        return re.sub(r"^(-?\d+(?:\.\d+)?)(?![\d.°])", r"\g<1>°", text, count=1)
    if not unit or text == "—":
        return text
    try:
        x = float(str(v).strip())
    except ValueError:
        return text
    if quantity:
        try:
            import gui_units                     # Tk-free at import time
            shown = gui_units.unit(quantity)
            x *= gui_units.factor(quantity, unit, shown)
            unit = shown
        except Exception:  # noqa: BLE001 -- no prefs: keep the stored unit
            pass
    return "%s %s" % (_fmt(x), unit)


# Headline keys that have NO GUI capture field -- manual annotations the operator
# enters by hand (blank on every auto-recorded run). The edit dialog always offers
# these, and set_param adds them on demand. A manual key is ungrouped (no preset
# kind owns it), so it lands in the "scaffold" catch-all group -- the same place
# gui_history._hist_group_params routes an unowned key.
MANUAL_HEADLINE_KEYS = frozenset({"env.humidity"})

# The group a manual (unowned) headline key is stored under -- the scaffold
# catch-all, matching gui_history._hist_group_params' default.
_MANUAL_GROUP = "scaffold"


def _fmt(v):
    """Compact display of a persisted value: ints stay ints, floats trim to 4 sig
    figs, blanks read as an em-dash, pre-humanised strings ('90 (square)') pass
    through untouched."""
    s = str(v).strip()
    if s == "":
        return "—"
    try:
        f = float(s)
    except ValueError:
        return s
    return str(int(f)) if f == int(f) else f"{f:.4g}"


def _flat_params(record):
    """The record's grouped params flattened to one {key: value} dict."""
    flat = {}
    for group in PARAM_GROUPS:
        flat.update(record.get("params", {}).get(group) or {})
    # a run recorded when the needle was a bore RADIUS reads as its diameter
    try:
        import mew_calibration
        flat = mew_calibration.migrate_inner_diameter(flat)
    except ImportError:
        pass
    return flat


def headline_rows(record):
    """The curated headline parameters as [(section, [(label, value)])], skipping
    any section whose keys are all absent."""
    flat = _flat_params(record)
    out = []
    for section, keys in HEADLINE_SECTIONS:
        rows = [(label, _with_unit(key, flat[key]))
                for key, label in keys if key in flat]
        if rows:
            out.append((section, rows))
    return out


def process_headline_keys():
    """The Process headline parameters as [(key, label)] -- the numeric process
    settings the saved-log graph can trend over time (speed, CTS, pressure, voltage,
    gap, temperature, humidity, needle inner diameter). Sourced from HEADLINE_SECTIONS so the
    dropdown and the graph stay in lockstep with the curated set."""
    return list(dict(HEADLINE_SECTIONS).get("Process", ()))


def series_points(records, key):
    """A run-identified time series for one headline parameter across `records`:
    [{"t": datetime, "v": float, "id": run_id, "title": title}], sorted ascending by
    each run's timestamp (`ts`). A run is skipped when its value for `key` is
    blank/absent/non-numeric or its `ts` won't parse. Keeping the run id/title on each
    point lets the saved-log graph map a clicked point back to its run."""
    out = []
    for rec in records:
        raw = _flat_params(rec).get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            val = float(str(raw).strip())
        except ValueError:
            continue
        try:
            ts = datetime.datetime.fromisoformat(rec.get("ts", ""))
        except (ValueError, TypeError):
            continue
        out.append({"t": ts, "v": val, "id": rec.get("id", ""),
                    "title": rec.get("title", "")})
    out.sort(key=lambda p: p["t"])
    return out


def series_for_key(records, key):
    """A time series for one headline parameter across `records`: a list of
    (datetime, float) sorted ascending by each run's timestamp (`ts`), invalid runs
    skipped. The `(t, v)` projection of `series_points` -- which the graph uses when
    it also needs each point's run identity (click-to-identify)."""
    return [(p["t"], p["v"]) for p in series_points(records, key)]


def set_param(record, key, value, add_if_missing=False):
    """Set an EXISTING persisted key in whichever group owns it (used by the edit
    dialog to correct a headline value). Returns True if the key was set.

    When `add_if_missing` is True and the key is absent from every group, it is
    ADDED to the _MANUAL_GROUP (the scaffold catch-all) -- this is how a manual
    headline field (env.humidity) gets recorded on a run that never captured one.
    The default (False) leaves the old contract intact: an unknown key is a no-op
    returning False."""
    params = record.setdefault("params", {})
    for group in PARAM_GROUPS:
        if key in (params.get(group) or {}):
            params[group][key] = value
            return True
    if add_if_missing:
        params.setdefault(_MANUAL_GROUP, {})[key] = value
        return True
    return False


def new_manual_record(params=None, title="", mode="manual", result="", note="",
                      now=None):
    """Build a MANUALLY-entered run record -- the same shape a captured run has -- so
    a print done outside the app can be logged by hand. Every field is optional: blank
    params are dropped (so they simply yield fewer headline rows), and an all-blank
    entry still records cleanly. `params` is the grouped {group: {key: value}} dict;
    missing groups fill empty. The caller appends the returned dict to a HistoryStore.
    `now` defaults to the wall clock (injectable for tests)."""
    base = now or datetime.datetime.now()
    grouped = {}
    for g in PARAM_GROUPS:
        src = (params or {}).get(g) or {}
        grouped[g] = {k: v for k, v in src.items() if str(v).strip() != ""}
    rec = {
        "id": base.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2),
        "ts": base.isoformat(timespec="seconds"),
        "mode": (str(mode).strip() or "manual"),
        "params": grouped,
        "argv": [],
        "title": str(title or "").strip(),
        "status": "manual",   # provenance flag; the Result column shows result-or-this
        "manual": True,
    }
    if str(result or "").strip():
        rec["result"] = str(result).strip()
    if str(note or "").strip():
        rec["note"] = str(note).strip()
    return rec


_ILLEGAL_FILE = set('<>:"/\\|?*')


def _safe_filename(name):
    """A filesystem-safe stem from a user title: illegal/control chars -> '_',
    trailing dots/spaces trimmed (Windows), capped at 80 chars. '' when blank."""
    name = (name or "").strip()
    if not name:
        return ""
    out = "".join("_" if (c in _ILLEGAL_FILE or ord(c) < 32) else c for c in name)
    return out.strip(" .")[:80]


def _unique_filename(dirpath, base, ext):
    """`base+ext`, or `base_2+ext`, `base_3+ext`, ... if that already exists."""
    cand = base + ext
    i = 2
    while os.path.exists(os.path.join(dirpath, cand)):
        cand = f"{base}_{i}{ext}"
        i += 1
    return cand

# Optional PDF backend. reportlab is OPTIONAL and imported LAZILY (only inside
# build_run_sheet_pdf) so importing this module -- which happens at APP STARTUP --
# never pays reportlab's ~0.2 s import. find_spec is a cheap availability probe that
# does NOT import the package: enough for the GUI to grey the PDF button. CSV export
# needs none of this.
HAS_REPORTLAB = importlib.util.find_spec("reportlab") is not None


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
class HistoryStore:
    """Read/write the per-user history under `base_dir`. Dirs are created lazily,
    so constructing a store never touches the filesystem (tests pass a tmp dir)."""

    RUN_CAP = 500        # keep at most this many runs in runs.jsonl (FIFO trim)
    RETENTION_DAYS = 30  # unlogged runs older than this are auto-pruned; the saved
                         # log (saved.json) is curated and NEVER expires

    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.runs_path = os.path.join(base_dir, "runs.jsonl")
        self.saved_path = os.path.join(base_dir, "saved.json")
        self.thumbs_dir = os.path.join(base_dir, "thumbs")
        self.saved_files_dir = os.path.join(base_dir, "saved_files")
        self.config_path = os.path.join(base_dir, "config.json")

    def _ensure(self):
        os.makedirs(self.base_dir, exist_ok=True)

    # ---- small persisted config (config.json) ----------------------------- #
    # A tiny per-user settings blob for the page itself -- deliberately NOT in the
    # GUI preset persist map (this is a UI preference, not a scaffold parameter, so
    # it stays clear of the persist-map parity guard). Currently just the auto-capture
    # toggle. A missing / corrupt file reads as the defaults.
    def _load_config(self):
        try:
            with open(self.config_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def capture_enabled(self):
        """Whether every Generate is auto-recorded (default True)."""
        return bool(self._load_config().get("capture_enabled", True))

    def set_capture_enabled(self, on):
        """Persist the auto-capture toggle (leaves any other config keys intact)."""
        self._ensure()
        cfg = self._load_config()
        cfg["capture_enabled"] = bool(on)
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)

    # ---- broad history (runs.jsonl) --------------------------------------- #
    def append(self, record, now=None):
        """Append one finished-run record, then prune (age + FIFO cap)."""
        self._ensure()
        with open(self.runs_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        self.prune(now)

    def prune(self, now=None):
        """Drop unlogged runs older than RETENTION_DAYS, then any beyond RUN_CAP
        (newest kept). Unparseable lines / timestamps are KEPT -- a parse hiccup must
        never silently lose history. The saved log (saved.json) is separate and is
        NEVER pruned. `now` defaults to the wall clock (injectable for tests)."""
        try:
            with open(self.runs_path, encoding="utf-8") as fh:
                lines = [ln for ln in fh if ln.strip()]
        except OSError:
            return
        cutoff = None
        if self.RETENTION_DAYS:
            base = now or datetime.datetime.now()
            cutoff = base - datetime.timedelta(days=self.RETENTION_DAYS)
        kept = []
        for ln in lines:
            if cutoff is not None:
                try:
                    ts = datetime.datetime.fromisoformat(json.loads(ln).get("ts", ""))
                    if ts < cutoff:
                        continue                      # too old -> drop
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass                              # undateable -> keep
            kept.append(ln if ln.endswith("\n") else ln + "\n")
        if self.RUN_CAP and self.RUN_CAP > 0 and len(kept) > self.RUN_CAP:
            kept = kept[-self.RUN_CAP:]
        if len(kept) != len(lines):
            with open(self.runs_path, "w", encoding="utf-8") as fh:
                fh.writelines(kept)

    def delete_run(self, run_id):
        """Remove a single run from the broad history (runs.jsonl)."""
        try:
            with open(self.runs_path, encoding="utf-8") as fh:
                lines = [ln for ln in fh if ln.strip()]
        except OSError:
            return
        out = []
        for ln in lines:
            try:
                if json.loads(ln).get("id") == run_id:
                    continue
            except json.JSONDecodeError:
                pass
            out.append(ln if ln.endswith("\n") else ln + "\n")
        with open(self.runs_path, "w", encoding="utf-8") as fh:
            fh.writelines(out)

    def load_runs(self):
        """All recorded runs, NEWEST FIRST. Corrupt lines are skipped so one bad
        write can never blank the whole history."""
        out = []
        try:
            with open(self.runs_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        out.reverse()
        return out

    def get_run(self, run_id):
        for r in self.load_runs():
            if r.get("id") == run_id:
                return r
        return None

    # ---- saved log (saved.json) ------------------------------------------- #
    def load_saved(self):
        try:
            with open(self.saved_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return []
        return data.get("saved", []) if isinstance(data, dict) else []

    def _write_saved(self, saved):
        self._ensure()
        with open(self.saved_path, "w", encoding="utf-8") as fh:
            json.dump({"saved": saved}, fh, indent=2)

    def promote(self, run_id, note="", tags=None, operator="", gcode_src=None,
                saved_ts="", title=None, result=None):
        """Copy a run out of the broad history into the curated saved log, with a
        user title/note/tags/operator/result. The FULL record is deep-copied so the
        saved entry survives a later FIFO trim of runs.jsonl. When `gcode_src` is a
        real file it is archived under saved_files/<id>.gcode (the opt-in "keep
        g-code copy"); otherwise gcode_copy stays None (reproduce regenerates it).

        `gcode_src` may instead be a LIST of files: a calibration print leaves
        the app as one file per slide (one per rung of a temperature ladder),
        and its one entry keeps them all, under saved_files/<base>/ with their
        own names, listed in `gcode_copies`. `gcode_copy` then names the
        first, so a reader that knows only one file still finds one."""
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        entry = dict(run)
        entry["params"] = {k: dict(v) for k, v in (run.get("params") or {}).items()}
        entry["note"] = note
        entry["tags"] = list(tags or [])
        entry["operator"] = operator
        entry["saved_ts"] = saved_ts
        if title is not None:
            entry["title"] = title
        if result is not None:
            entry["result"] = result
        entry["gcode_copy"] = None
        entry["gcode_copies"] = []
        if isinstance(gcode_src, (list, tuple)):
            srcs = [p for p in gcode_src if p and os.path.isfile(p)]
            if srcs:
                base = _safe_filename(title) or run_id
                sub = _unique_filename(self.saved_files_dir, base, "")
                folder = os.path.join(self.saved_files_dir, sub)
                try:
                    os.makedirs(folder, exist_ok=True)
                    for src in srcs:
                        shutil.copy2(src, os.path.join(
                            folder, os.path.basename(src)))
                        entry["gcode_copies"].append(os.path.join(
                            "saved_files", sub, os.path.basename(src)))
                except OSError:
                    entry["gcode_copies"] = []
                if entry["gcode_copies"]:
                    entry["gcode_copy"] = entry["gcode_copies"][0]
        elif gcode_src and os.path.isfile(gcode_src):
            os.makedirs(self.saved_files_dir, exist_ok=True)
            # name the archived g-code after the TITLE (sanitised); fall back to the
            # run id when there is no title, and uniquify to avoid clobbering another
            # saved run that shares the title
            base = _safe_filename(title) or run_id
            fname = _unique_filename(self.saved_files_dir, base, ".gcode")
            try:
                shutil.copy2(gcode_src, os.path.join(self.saved_files_dir, fname))
                entry["gcode_copy"] = os.path.join("saved_files", fname)
            except OSError:
                entry["gcode_copy"] = None
        saved = [s for s in self.load_saved() if s.get("id") != run_id]
        saved.append(entry)
        self._write_saved(saved)
        return entry

    def annotate(self, run_id, note=None, tags=None, operator=None):
        """Edit a saved entry's note/tags/operator in place (None = leave as-is)."""
        saved = self.load_saved()
        for s in saved:
            if s.get("id") == run_id:
                if note is not None:
                    s["note"] = note
                if tags is not None:
                    s["tags"] = list(tags)
                if operator is not None:
                    s["operator"] = operator
                self._write_saved(saved)
                return
        raise KeyError(run_id)

    def update_saved(self, entry):
        """Replace the saved entry with the same id wholesale (used by the edit
        dialog after it has changed the note AND/OR headline parameter values)."""
        rid = entry.get("id")
        saved = [s for s in self.load_saved() if s.get("id") != rid]
        saved.append(entry)
        self._write_saved(saved)

    def delete_saved(self, run_id):
        """Drop a saved entry (and its archived g-code copy, if any)."""
        saved = self.load_saved()
        for s in saved:
            if s.get("id") != run_id:
                continue
            for rel in kept_files(s):
                try:
                    os.remove(os.path.join(self.base_dir, rel))
                except OSError:
                    pass
            if s.get("gcode_copies"):
                try:
                    os.rmdir(os.path.dirname(os.path.join(
                        self.base_dir, s["gcode_copies"][0])))
                except OSError:
                    pass
        self._write_saved([s for s in saved if s.get("id") != run_id])


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #
def diff_runs(a, b):
    """Compare two run records' grouped params. Returns:
        {"changed": [(group, key, value_a, value_b)],
         "added":   [(group, key, value_b)],   # in b, not a
         "removed": [(group, key, value_a)]}   # in a, not b
    Values are compared as strings (persist snapshots are widget .get() strings)."""
    pa = a.get("params") or {}
    pb = b.get("params") or {}
    changed, added, removed = [], [], []
    for group in PARAM_GROUPS:
        ga = pa.get(group) or {}
        gb = pb.get(group) or {}
        for key in sorted(set(ga) | set(gb)):
            in_a, in_b = key in ga, key in gb
            if in_a and in_b:
                if str(ga[key]) != str(gb[key]):
                    changed.append((group, key, ga[key], gb[key]))
            elif in_b:
                added.append((group, key, gb[key]))
            else:
                removed.append((group, key, ga[key]))
    return {"changed": changed, "added": added, "removed": removed}


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def export_run_csv(record, out_path):
    """Write a single run's HEADLINE summary as a long-format CSV: header
    [section, parameter, value], then the meta fields, the headline parameters,
    and the note. The full parameter set is NOT dumped -- it stays in the record
    for Reproduce; the sheet is a summary. Stdlib only."""
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["section", "parameter", "value"])
        for label, val in (("id", record.get("id", "")),
                           ("title", record.get("title", "")),
                           ("date", record.get("ts", "")),
                           ("made in", mode_label(record)),
                           ("result", _result(record)),
                           ("operator", record.get("operator", "")),
                           ("build", record.get("app_version", ""))):
            if val not in (None, ""):
                w.writerow(["meta", label, val])
        for section, rows in headline_rows(record):
            for label, val in rows:
                w.writerow([section, label, val])
        if record.get("note"):
            w.writerow(["notes", "note", record["note"]])


_STATUS_WORDS = {"ok": "Done", "failed": "Failed", "cancelled": "Cancelled"}

# what made the run, in the words of the top row; older records carry only
# `mode` (the run() dispatch word), which cannot tell the G-code window from
# the Scaffold designer -- both build through "scaffold"
_SOURCE_WORDS = {"gcode_window": "G-code window",
                 "scaffold": "Scaffold designer",
                 "gcode": "G-code window",
                 "calibration": "Calibrate",
                 "manual": "Added by hand"}
_COLLECTOR_WORDS = {"plate": "plate", "mandrel": "mandrel"}


def kept_files(entry):
    """The g-code files a saved entry keeps, relative to the store: every
    slide of a calibration print, or the one file of any other run."""
    if entry.get("gcode_copies"):
        return list(entry["gcode_copies"])
    return [entry["gcode_copy"]] if entry.get("gcode_copy") else []


def _result(record):
    """The RESULT to display: the user's edited `result` if present, else the
    auto-captured `status` in words (Done / Failed / Cancelled)."""
    if record.get("result"):
        return record["result"]
    st = record.get("status", "")
    return _STATUS_WORDS.get(st, st)


def mode_label(record):
    """Where a run came from, as a person reads it: "G-code window, mandrel".
    A record from before `source` existed falls back to its `mode` word."""
    src = record.get("source") or record.get("mode", "")
    words = _SOURCE_WORDS.get(src, src)
    coll = _COLLECTOR_WORDS.get(record.get("collector", ""))
    return "%s, %s" % (words, coll) if words and coll else words


def _param_table_style():
    # No header row (the section heading names the table) -> every row is a plain
    # label/value pair, uniform. Label column tinted so the two columns read apart.
    from reportlab.lib import colors            # lazy: only when building a PDF
    from reportlab.platypus import TableStyle
    return TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f2f4")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbbbbb")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ])


def build_run_sheet_pdf(record, out_path, thumb_path=None):
    """Render a compact 1-2 page run sheet: the scaffold THUMBNAIL at the top (only
    when one exists -- i.e. the run generated a scaffold), a one-line header, the
    curated HEADLINE parameters (process + geometry), and the note. The full
    parameter set is deliberately NOT dumped (that is what Reproduce is for).
    Requires reportlab -- raises RuntimeError with a plain-language hint when it is
    not installed (the GUI greys the PDF button)."""
    if not HAS_REPORTLAB:
        raise RuntimeError(
            "reportlab not installed -- run 'pip install reportlab' to export PDF "
            "run sheets. (CSV export needs no extra libraries.)")
    # lazy imports: keep reportlab off the app-startup path (see HAS_REPORTLAB)
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (SimpleDocTemplate, Table, Paragraph, Spacer,
                                     Image)
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(out_path, pagesize=LETTER, topMargin=16 * mm,
                            bottomMargin=16 * mm,
                            title="MEWracle run sheet " + str(record.get("id", "")))
    flow = []
    # the preview at the TOP -- only when the run produced a scaffold thumbnail
    if thumb_path and os.path.isfile(thumb_path):
        try:
            flow.append(Image(thumb_path, width=64 * mm, height=64 * mm,
                              hAlign="CENTER"))
            flow.append(Spacer(1, 3 * mm))
        except Exception:  # noqa: BLE001 -- a bad PNG must not sink the whole sheet
            pass
    flow.append(Paragraph("MEWracle Run Sheet", styles["Title"]))
    title = record.get("title") or record.get("id", "run")
    meta = "  ·  ".join(x for x in (
        title, record.get("ts", "").replace("T", "  "), mode_label(record),
        _result(record),
        ("op: " + record["operator"]) if record.get("operator") else "",
        ("build " + record["app_version"]) if record.get("app_version") else "",
    ) if x)
    flow.append(Paragraph(meta, styles["Normal"]))
    if record.get("tags"):
        flow.append(Paragraph("tags: " + ", ".join(record["tags"]), styles["Normal"]))
    flow.append(Spacer(1, 5 * mm))
    for section, rows in headline_rows(record):
        flow.append(Paragraph(section, styles["Heading2"]))
        flow.append(Table([[label, val] for label, val in rows],
                          colWidths=[85 * mm, 70 * mm], style=_param_table_style()))
        flow.append(Spacer(1, 3 * mm))
    if record.get("note"):
        flow.append(Paragraph("Notes", styles["Heading2"]))
        flow.append(Paragraph(record["note"].replace("\n", "<br/>"),
                              styles["BodyText"]))
    doc.build(flow)
