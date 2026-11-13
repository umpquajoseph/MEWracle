"""One place that scales this repo's absolute wall-clock test budgets.

Several suites assert a real duration. Those numbers were measured on the
reference desktop, and they are the point: a budget that drifts with the machine cannot
catch a regression, so the desktop must keep seeing them EXACTLY.

A cloud VM is a different machine, not merely a slower one -- 4 vCPU and no GPU,
so VTK renders through Mesa's llvmpipe, where one frame of the scaffold preview
measures 4.5-7.0 s against ~10 ms on a GPU. Widening the assert for everyone
would silently retire the desktop measurement, which is why
this is a scale factor instead.

USAGE

    import perf_budget
    assert dt < perf_budget.seconds(3.0)        # a duration ceiling
    FPS_FLOOR = perf_budget.rate(45.0)          # a throughput floor

`seconds()` multiplies and `rate()` divides, so both move in the direction that
grants slack. On the desktop FACTOR is exactly 1.0 and both are identities.

THE FACTOR: `MEW_PERF_FACTOR` in the environment wins, always (set it to `1` to
force desktop-strict budgets in cloud and see the raw numbers); otherwise a
cloud session (`CLAUDE_CODE_REMOTE=true`) gets CLOUD_FACTOR; otherwise 1.0.

CLOUD_FACTOR is measured, not guessed. Worst observed overrun on the reference
VM (Ubuntu 24.04, 4 vCPU, llvmpipe):

    test_gui_preview  latency budget   17.50 s / 3.0 s  = 5.83x
    test_gui_preview  sag toggle       10.57 s / 3.5 s  = 3.02x
    test_viewer_embed locked-player    45.0 fps / 28.9  = 1.56x

8.0 clears the worst by ~37%. Re-measure it rather than nudging it: the spread
between 1.56x and 5.83x is the software rasteriser, so a suite that newly needs
a bigger factor is usually rendering more, not running slower.

WHAT THIS DOES NOT DO. It does not make a cloud timing comparable to a desktop
one -- only an A/B between two runs on the SAME host is meaningful. Scaling
buys the suite the right to run; it does not turn it into a benchmark.
"""

import os as _os, sys as _sys
# A developer script: the program's folders sit beside this one, and
# app_paths puts all of them on sys.path.
_sys.path.insert(0, _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "engine"))
import app_paths  # noqa: E402,F401

import os

CLOUD_FACTOR = 8.0


def _resolve():
    raw = (os.environ.get("MEW_PERF_FACTOR") or "").strip()
    if raw:
        try:
            val = float(raw)
        except ValueError:
            val = 0.0
        if val > 0:
            return val
        # A malformed or non-positive override is a typo, not a request for
        # zero-length budgets -- fall through rather than make every assert fail.
    if os.environ.get("CLAUDE_CODE_REMOTE") == "true":
        return CLOUD_FACTOR
    return 1.0


FACTOR = _resolve()


def seconds(budget):
    """Scale a duration CEILING (bigger = more slack)."""
    return budget * FACTOR


def rate(floor):
    """Scale a throughput FLOOR (smaller = more slack)."""
    return floor / FACTOR


def note():
    """One line naming the factor, for a suite to print when it is not 1.0."""
    if FACTOR == 1.0:
        return ""
    return ("perf_budget: wall-clock budgets scaled x%g (%s); these are NOT "
            "comparable to desktop timings."
            % (FACTOR,
               "MEW_PERF_FACTOR" if (os.environ.get("MEW_PERF_FACTOR") or "").strip()
               else "cloud session"))
