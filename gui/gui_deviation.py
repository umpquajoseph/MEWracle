#!/usr/bin/env python3
"""gui_deviation.py -- the GUI half of the Viewer's "Show deviation".

The Viewer (gcode_player, embedded) cannot run the analysis itself, so its
"Show deviation" button sends a child->parent `deviation` request
(gui_embed._handle_viewer_request). This mixin answers it: pair the active
print's g-code with its `.depo` (`mew_analysis.deviation_sidecar`), off the Tk
thread, and push the sidecar back DOWN with `panel.send_child(t="deviation")`.
A failure is pushed down too, with a one-line reason the Viewer shows until the
next attempt.

Degrades like the other
physics mixins: without `mew_analysis` the handler is never registered and the
Viewer's request goes unanswered.

Part of the gcode_to_fibers_gui split; see gui_common.py.
"""
import os
import threading

from gui_common import *

try:
    import mew_analysis
    _HAVE_ANALYSIS = True
except Exception:  # noqa: BLE001
    _HAVE_ANALYSIS = False


def _depo_key(gcode, depo):
    """What a sidecar was computed from. The session folder reuses its file
    names on every Generate, so the path alone would match a stale print; the
    .depo's mtime tells two prints apart."""
    try:
        return (gcode, depo, os.path.getmtime(depo))
    except OSError:
        return None


class _DeviationMixin:
    def _dev_init(self):
        if not hasattr(self, "_dev_waiters"):
            self._dev_waiters = []      # viewer panels awaiting a sidecar
            self._dev_busy = False
            self._dev_cache = None      # (key, sidecar path) of the last success

    if _HAVE_ANALYSIS:
        def _req_viewer_deviation(self, panel, msg):
            """Child->parent 'deviation' request from the unified Viewer. Registered
            via gui_embed._viewer_request_handlers (only when this method exists)."""
            self._dev_init()
            gco = getattr(self, "last_gcode", None)
            depo = getattr(self, "last_depo", None)
            if not (gco and os.path.isfile(gco)):
                panel.send_child(t="deviation", sidecar="",
                                 error="no g-code for this print")
                return
            if not (depo and os.path.isfile(depo)):
                panel.send_child(t="deviation", sidecar="",
                                 error="no .depo cache for this print")
                return
            key = _depo_key(gco, depo)
            cache = self._dev_cache
            if cache and cache[0] == key and os.path.isfile(cache[1]):
                panel.send_child(t="deviation", sidecar=cache[1])
                return
            if panel not in self._dev_waiters:
                self._dev_waiters.append(panel)
            if not self._dev_busy:
                self._dev_run(gco, depo, key)

    def _dev_run(self, gco, depo, key):
        """Run the analysis on a worker thread. The worker only fills `box`; the
        Tk thread polls it, because a worker's root.after needs the main thread
        to be inside mainloop and raises otherwise."""
        self._dev_busy = True
        out = os.path.join(SESSION_DIR, "deviation.npz")
        box = {}

        def work():
            try:
                vmax = mew_analysis.deviation_sidecar(gco, depo, out)
                box["res"] = {"ok": True, "sidecar": out, "vmax": vmax}
            except mew_analysis.DepoMismatch as exc:
                box["res"] = {"ok": False, "err": str(exc)}
            except Exception as exc:  # noqa: BLE001
                box["res"] = {"ok": False, "err": f"analysis failed: {exc}"}

        def poll():
            if "res" in box:
                self._dev_finish(box["res"], key)
            else:
                self.root.after(100, poll)
        threading.Thread(target=work, daemon=True).start()
        self.root.after(100, poll)

    def _dev_finish(self, res, key):
        self._dev_busy = False
        if res["ok"]:
            self._dev_cache = (key, res["sidecar"])
            side, err = res["sidecar"], ""
            try:
                self.append_log(f"[deviation] max {res['vmax'] * 1000:.0f} µm\n")
            except Exception:  # noqa: BLE001
                pass
        else:
            side, err = "", res["err"]
            try:
                self.append_log(f"[deviation unavailable] {err}\n")
            except Exception:  # noqa: BLE001
                pass
        for p in list(self._dev_waiters):
            try:
                p.send_child(t="deviation", sidecar=side, error=err)
            except Exception:  # noqa: BLE001
                pass
        self._dev_waiters = []

    def _invalidate_deviation(self):
        """A new successful run makes the cached sidecar stale. The mtime key
        already refuses it; clearing it here as well keeps a same-second
        regenerate from ever matching."""
        self._dev_init()
        self._dev_cache = None
