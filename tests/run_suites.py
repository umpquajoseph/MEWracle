"""Run every `tests/test_*.py` suite as its own process, in parallel, and
keep an append-only ledger of what happened.

Suites self-run (no pytest): each prints PASS/FAIL and exits non-zero on a
failure. This runner only schedules them, bounds them and records them.
From the repo root:

    ..\\.venv\\Scripts\\python.exe tests\\run_suites.py           conservative defaults
    python tests/run_suites.py --workers 6 --gui-workers 2
    python tests/run_suites.py --only gui sag --exclude matrix
    python tests/run_suites.py --gate                           the ~2 min check, run once when a feature is finished
    python tests/run_suites.py --rerun-red                      reds of the last run, ONE at a time
    python tests/run_suites.py --list                           what would run, GUI flag included

WHERE. The suites live in `tests/`, beside this file; the program they test is
the folder above. Each suite runs with the repo root as its working directory,
as it did when the suites sat at the root, and each puts the root on `sys.path`
itself, so `python tests/test_foo.py` also works on its own.

LIMITS. `--workers` caps every running suite; `--gui-workers` caps the subset
that opens Tk or VTK windows (a plain text scan, see `is_gui`). A parallel run
once died with Windows error 1158 (out of window handles), and this PC has
frozen under unbounded test runs, so both defaults are low. The scan
reads import lines, so a suite that only names a GUI file does not count.

TIMEOUT. `--timeout` seconds per suite (default 900). On expiry the whole
process tree is killed (`taskkill /T /F` on Windows), because suites spawn
workers that would otherwise outlive the suite and keep eating the machine.

WINDOWS. On Windows the children start minimised, without taking focus, at
below-normal priority, so a run does not cover the screen. `--visible` turns that off.

ENCODING. Children get PYTHONIOENCODING=utf-8 and PYTHONUTF8=1: a suite
printing U+2715 crashed on a cp1252 stdout.

LEDGER. `.claude/test_ledger/runs.jsonl`, one JSON line per suite per run,
opened in append mode only and never rewritten. `tail` (last ~40 lines of
output) is stored for a non-zero exit or a timeout. Timings taken under
parallel load are NOT comparable with a one-at-a-time run; compare suites only
within a run, or between runs with the same `workers`.

The interpreter used for the children is the one running this script, unless
`--python` says otherwise.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # tests/: the suites
REPO = ROOT.parent                              # the program they test

#: `--gate`: the check for a finished feature or reorganization, about 2 min. Red here means something broke,
#: so nothing known-red, no hash pins and no timing budgets go in this list.
GATE_SUITES = (
    "test_ui_shell.py",
    "test_scenario_grid.py",
    "test_scaffold_gcode.py",
    "test_mandrel_gcode.py",
    "test_machine_model.py",
    "test_mew_physics.py",
    "test_mew_incremental.py",
    "test_gcode_to_fibers.py",
    "test_sag_approx.py",
    "test_five_axis_frame.py",
    "test_five_axis_metric.py",
    "test_sag_touchdown.py",
    "test_mew_fusion.py",
    "test_mew_gcode.py",
)
#: Too slow for a branch-end run. `--overnight` runs them; the default run skips them.
OVERNIGHT_SUITES = ()
LEDGER = REPO / ".claude" / "test_ledger" / "runs.jsonl"
TAIL_LINES = 40

# Suites that really open a Tk or VTK window. A wider match held the whole run at the
# GUI cap, and three GUI suites at once crashed natively on this PC.
_GUI_RE = re.compile(
    r"^\s*(?:import|from)\s+(?:tkinter|vtk\w*|capture_ui|mewracle_gui|gcode_to_fibers_gui"
    r"|gcode_player|stl_viewer|view_cube|viewer_embed|test_viewer_embed|gui_\w+)\b", re.M
)


def is_gui(path: Path) -> bool:
    try:
        return bool(_GUI_RE.search(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return True


def git(*args) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=30)
        return r.stdout.strip()
    except Exception:
        return ""


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, timeout=60)
    else:
        try:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            proc.kill()
    try:
        proc.wait(timeout=30)
    except Exception:
        pass


def tail_of(path: Path, n: int = TAIL_LINES) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def append_ledger(rec: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:   # append only, never rewrite
        f.write(json.dumps(rec) + "\n")


def last_run_reds() -> list:
    """Suite names that were red (non-zero exit or timeout) in the last run_id."""
    if not LEDGER.exists():
        return []
    recs = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            recs.append(json.loads(line))
        except ValueError:
            pass
    if not recs:
        return []
    rid = recs[-1]["run_id"]
    return [r["suite"] for r in recs if r["run_id"] == rid and (r["exit_code"] != 0 or r["timed_out"])]


def discover(root: Path, only, exclude) -> list:
    names = sorted(p.name for p in root.glob("test_*.py"))
    if only:
        names = [n for n in names if any(o.lower() in n.lower() for o in only)]
    if exclude:
        names = [n for n in names if not any(x.lower() in n.lower() for x in exclude)]
    return names


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workers", type=int, default=None,
                    help="max suites running at once (default 6)")
    ap.add_argument("--gui-workers", type=int, default=None,
                    help="max GUI/VTK suites running at once (default 2)")
    ap.add_argument("--overnight", action="store_true", help="run only OVERNIGHT_SUITES")
    ap.add_argument("--gate", action="store_true",
                    help="run only GATE_SUITES (the ~2 min check for a finished feature)")
    ap.add_argument("--timeout", type=float, default=900.0, help="seconds per suite (default 900)")
    ap.add_argument("--only", nargs="+", default=[], metavar="SUBSTR", help="keep suites whose name contains any")
    ap.add_argument("--exclude", nargs="+", default=[], metavar="SUBSTR", help="drop suites whose name contains any")
    ap.add_argument("--rerun-red", action="store_true",
                    help="rerun the reds of the last ledger run, one at a time")
    ap.add_argument("--python", default=sys.executable, help="interpreter for the children")
    ap.add_argument("--visible", action="store_true",
                    help="let suite windows open normally (default: minimised, unfocused, low priority)")
    ap.add_argument("--list", action="store_true", help="list the suites and their GUI flag, then exit")
    ap.add_argument("--root", default=str(ROOT), help="directory holding the test_*.py files")
    ap.add_argument("--ledger", default=None, help="override the ledger path (controls use this)")
    args = ap.parse_args()

    global LEDGER
    if args.ledger:
        LEDGER = Path(args.ledger)
    root = Path(args.root).resolve()

    names = discover(root, args.only, args.exclude)
    if args.gate:
        have = set(discover(root, [], []))
        missing = [n for n in GATE_SUITES if n not in have]
        if missing:
            print("gate suites missing from", root, ":", ", ".join(missing))
            return 2
        names = list(GATE_SUITES)
    if args.overnight:
        names = list(OVERNIGHT_SUITES)
    elif not (args.gate or args.only or args.rerun_red):
        names = [n for n in names if n not in OVERNIGHT_SUITES]
    workers = max(1, args.workers or 6)
    gui_workers = max(1, args.gui_workers or 2)
    rerun = False
    if args.rerun_red:
        reds = last_run_reds()
        names = [n for n in names if n in reds]
        workers = gui_workers = 1
        rerun = True
        if not names:
            print("no red suites in the last ledger run")
            return 0
    gui = {n: is_gui(root / n) for n in names}
    if args.list:
        for n in names:
            print(("GUI  " if gui[n] else "     ") + n)
        print(f"{len(names)} suites, {sum(gui.values())} GUI")
        return 0
    if not names:
        print("no suites matched")
        return 1

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    commit, branch = git("rev-parse", "HEAD"), git("rev-parse", "--abbrev-ref", "HEAD")
    run_id = uuid.uuid4().hex[:8]
    outdir = Path(tempfile.mkdtemp(prefix="run_suites_"))
    creation, startup = 0, None
    if os.name == "nt":
        creation = subprocess.CREATE_NEW_PROCESS_GROUP
        if not args.visible:
            # Windows the suites open start minimised and never take focus, and the
            # suites run at below-normal priority, so a run stays out of the way.
            creation |= subprocess.BELOW_NORMAL_PRIORITY_CLASS
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 7            # SW_SHOWMINNOACTIVE
    print(f"run {run_id}  commit {commit[:8]}  branch {branch}  {len(names)} suites  "
          f"workers={workers} gui_workers={gui_workers} timeout={args.timeout:.0f}s", flush=True)

    # Longest first would need history; GUI first keeps the tight limit busy early.
    pending = sorted(names, key=lambda n: (not gui[n], n))
    running = {}          # name -> (Popen, start, outfile handle, outpath)
    results = []
    t_wall = time.time()

    def finish(name, timed_out):
        proc, t0, fh, outp = running.pop(name)
        secs = time.time() - t0
        fh.close()
        code = proc.returncode if not timed_out else -1
        red = timed_out or code != 0
        rec = {
            "run_id": run_id, "commit": commit, "branch": branch, "suite": name,
            "seconds": round(secs, 2), "exit_code": code, "timed_out": timed_out,
            "workers": workers, "gui_workers": gui_workers, "gui": gui[name],
            "rerun": rerun, "timestamp": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "tail": tail_of(outp) if red else "",
        }
        append_ledger(rec)
        results.append(rec)
        status = "TIMEOUT" if timed_out else ("ok" if not red else f"RED({code})")
        print(f"  {status:9s} {secs:7.1f}s  {name}   [{len(results)}/{len(names)}]", flush=True)

    while pending or running:
        n_gui = sum(1 for k in running if gui[k])
        i = 0
        while len(running) < workers and i < len(pending):
            name = pending[i]
            if gui[name] and n_gui >= gui_workers:
                i += 1
                continue
            pending.pop(i)
            outp = outdir / (name + ".out")
            fh = open(outp, "wb")
            proc = subprocess.Popen([args.python, "-u", str(root / name)], cwd=root.parent, env=env,
                                    stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                    creationflags=creation, startupinfo=startup)
            running[name] = (proc, time.time(), fh, outp)
            if gui[name]:
                n_gui += 1
        for name in list(running):
            proc, t0, _, _ = running[name]
            if proc.poll() is not None:
                finish(name, False)
            elif time.time() - t0 > args.timeout:
                kill_tree(proc)
                finish(name, True)
        time.sleep(0.25)

    wall = time.time() - t_wall
    red = [r for r in results if r["exit_code"] != 0 or r["timed_out"]]
    tos = [r for r in results if r["timed_out"]]
    passed = len(results) - len(red)
    print("\n" + "=" * 60)
    print(f"passed {passed}   red {len(red) - len(tos)}   timed out {len(tos)}   total {len(results)}")
    print(f"wall clock {wall:.1f}s   sum of suite seconds {sum(r['seconds'] for r in results):.1f}s")
    print("slowest 15:")
    for r in sorted(results, key=lambda r: -r["seconds"])[:15]:
        print(f"  {r['seconds']:8.1f}s  {r['suite']}")
    if red:
        print("red:")
        for r in sorted(red, key=lambda r: r["suite"]):
            print(f"  {'TIMEOUT' if r['timed_out'] else 'exit ' + str(r['exit_code']):9s} {r['suite']}")
    print(f"outputs kept in {outdir}")
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())
