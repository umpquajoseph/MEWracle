#!/usr/bin/env python3
"""gui_args.py -- Args mixin: turns the scaffold/g-code input fields into a
validated run -- the numeric/hex field validators, the CLI-arg builders for both
worker scripts, the per-scaffold spec + scaffolds-json handoff, and the
fiber-param lists forwarded to the player."""
import math

from gui_common import *


def _not_a_number(value, name):
    """The refusal for a field that does not parse. A decimal comma -- what a
    European keyboard types for 0.5 -- gets told what to type instead, since
    the printer's g-code only knows the point."""
    s = str(value).strip()
    if s.count(",") == 1:
        try:
            float(s.replace(",", "."))
        except ValueError:
            pass
        else:
            return (f"{name} must be a number written with a decimal point: "
                    f"{s.replace(',', '.')}, not {s}.")
    if not s:
        return f"{name} is empty; it needs a number."
    return f"{name} must be a number."


class _ArgsMixin:
    @staticmethod
    def _pos_float(value, name, allow_zero=False):
        try:
            v = float(value)
        except ValueError:
            return None, _not_a_number(value, name)
        if not math.isfinite(v):
            return None, f"{name} must be a finite number."
        if v < 0 or (v == 0 and not allow_zero):
            return None, f"{name} must be {'non-negative' if allow_zero else 'positive'}."
        return v, None

    @staticmethod
    def _int_at_least(value, name, minimum):
        try:
            v = int(value)
        except ValueError:
            s = str(value).strip()
            return None, (f"{name} is empty; it needs a whole number." if not s
                          else f"{name} must be a whole number.")
        if v < minimum:
            return None, f"{name} must be at least {minimum}."
        return v, None

    @classmethod
    def _faces_error(cls, value):
        """None, or why `value` is not a usable tube facet count: a whole
        number from 3 to the converter's `MAX_FACES`, which a run past it
        refuses after minutes of meshing rather than before."""
        import gcode_to_fibers
        n, e = cls._int_at_least(value, "Tube facets", 3)
        if e:
            return e
        if n > gcode_to_fibers.MAX_FACES:
            return ("Tube facets is %d; the most is %d. A tube of %d facets "
                    "is already round to the eye, and each facet more adds "
                    "triangles to every fiber." % (n, gcode_to_fibers.MAX_FACES,
                                                   gcode_to_fibers.MAX_FACES))
        return None

    @staticmethod
    def _any_float(value, name):
        try:
            v = float(value)
        except ValueError:
            return None, _not_a_number(value, name)
        if not math.isfinite(v):
            return None, f"{name} must be a finite number."
        return v, None

    @staticmethod
    def _valid_hex(value, name):
        """None if `value` is a #rrggbb hex colour (the # is optional) or blank
        (blank -> the viewer uses its default), else an error string."""
        s = value.strip().lstrip("#")
        if not s:
            return None
        if len(s) != 6 or any(c not in "0123456789abcdefABCDEF" for c in s):
            return f"{name} must be a hex color like #ff0000."
        return None

    def _validate_gcode(self):
        inp = self.in_var.get().strip()
        if not inp:
            return "Please choose an input G-code file."
        if not os.path.isfile(inp):
            return f"Input file not found:\n{inp}"
        # Frozen there is no loose worker .py -- the exe re-launches itself as
        # `MEWracle.exe worker:convert ...` (worker_cmd), so skip the source-only
        # script-exists pre-flight; WORKER still points at a non-existent path.
        if not getattr(sys, "frozen", False) and not os.path.isfile(WORKER):
            return f"Cannot find the worker script:\n{WORKER}"
        dia, e = self._pos_float(self.diameter.get(), "Fiber diameter")
        if e:
            return e
        # This field is in mm while the Design page's is in um, so "20" meant
        # as 20 um arrives here as a 20 mm tube, built metres high
        if dia > 2.0:
            return ("Fiber diameter is %g mm here. MEW fibers are micrometres "
                    "across: for %g um type %g (this field is in mm)."
                    % (dia, dia, dia / 1000.0))
        e = self._faces_error(self.faces.get())
        if e:
            return e
        if self._sag_solver_value(self.sag_mode) != "off":
            _, e = self._pos_float(self.sag_mult.get(), "Sag multiplier",
                                   allow_zero=True)
            if e:
                return e
        return self._validate_plate()

    def _validate_one_scaffold(self, v, label=""):
        """Validate one scaffold's field vars; return an error string or None.
        `label` (e.g. 'Scaffold 2: ') prefixes the message so the user knows
        which panel is at fault in a multi-scaffold run."""
        for name, key in (("Width X", "width"), ("Length Y", "height"),
                          ("Fiber spacing", "stepover"),
                          ("Fiber diameter", "diameter"),
                          ("Print speed", "speed")):
            _, e = self._pos_float(v[key].get(), label + name)
            if e:
                return e
        _, e = self._int_at_least(v["layers"].get(), label + "Layers", 1)
        if e:
            return e
        if v["angle_pattern_on"].get():
            self._sync_angle_pattern(v)
            angles, e = self._parse_angle_pattern(v["angle_pattern"].get())
            if e:
                return f"{label}Layer angles: {e}."
            if not angles:
                return (f"{label}Layer angles is on but lists no angles. Add "
                        f"at least one, or turn it off.")
            steps, es = self._parse_layer_value_list(
                v["angle_stepover_pattern"].get())
            if es:
                return f"{label}Per-layer spacing: {es}."
            if steps and any(x is not None and x <= 0 for x in steps):
                return (f"{label}Per-layer spacing values must be positive.")
            _offs, eo = self._parse_layer_value_list(
                v["angle_offset_pattern"].get())
            if eo:
                return f"{label}Per-layer period offset: {eo}."
        else:
            try:
                self._rotation_value(v["rotation"])
            except (ValueError, IndexError):
                return f"{label}Layer rotation must be a number (e.g. 90, 60, 36)."
        for name, key in (("Start X", "start_x"), ("Start Y", "start_y"),
                          ("Start Z", "start_z"), ("Start angle", "start_angle"),
                          ("Footprint angle", "footprint_angle")):
            val, e = self._any_float(v[key].get(), label + name)
            if e:
                return e
            # Start Z is a height above the collector's surface: below zero
            # the nozzle is driven into the plate or the rod
            if key == "start_z" and val < 0:
                return (f"{label}Start Z is {val:g} mm, below the collector "
                        f"surface: the nozzle would be driven into it. Use 0 "
                        f"to print on the surface, or a positive height.")
        if v["arc_radius_mult"].get().strip():
            _, e = self._pos_float(v["arc_radius_mult"].get(),
                                   label + "Turnaround radius (x spacing)",
                                   allow_zero=True)
            if e:
                return e
        _, e = self._int_at_least(v["arc_segments"].get(),
                                  label + "Arc segments", 2)
        if e:
            return e
        if v["stepover_end"].get().strip():
            _, e = self._pos_float(v["stepover_end"].get(),
                                   label + "Spacing where it ends")
            if e:
                return e
            _, e = self._any_float(v["grade_angle"].get(),
                                   label + "Grade direction")
            if e:
                return e
        if v["layer_overrides"].get().strip():
            import scaffold_gcode
            try:
                scaffold_gcode.parse_layer_overrides(
                    v["layer_overrides"].get(), int(v["layers"].get()))
            except ValueError as exc:
                return f"{label}Layer parameters: {exc}."
        if v["loop_speed"].get().strip():
            _, e = self._pos_float(v["loop_speed"].get(), label + "Loop speed")
            if e:
                return e
        if v["link_loops_on"].get():
            _, e = self._pos_float(v["link_loop_angle"].get(),
                                   label + "Linking-loop min turn angle",
                                   allow_zero=True)
            if e:
                return e
            _, e = self._pos_float(v["link_loop_dist"].get(),
                                   label + "Linking-loop coincidence distance",
                                   allow_zero=True)
            if e:
                return e
        if v["perimeter_travel"].get():
            _, e = self._pos_float(v["perimeter_travel_offset"].get(),
                                   label + "Travel distance from the part",
                                   allow_zero=True)
            if e:
                return e
        if v["linear_on"].get():
            self._sync_linear_pattern(v)
            pairs, e = self._parse_linear_pattern(v["linear_pattern"].get())
            if e:
                return f"{label}Wave: {e}."
            if not pairs:
                return (f"{label}Wave is on but lists no points. Add at "
                        f"least one, or turn it off.")
            _, e = self._pos_float(v["linear_wavelength"].get(),
                                   label + "Wave wavelength")
            if e:
                return e
            if v["linear_spacing"].get().strip():
                _, e = self._pos_float(v["linear_spacing"].get(),
                                       label + "Wave period spacing",
                                       allow_zero=True)
                if e:
                    return e
        shape = v["shape"].get()
        if shape in ("circle", "ellipse", "polygon"):
            _, e = self._int_at_least(v["shape_sides"].get(),
                                      label + "Shape sides", 3)
            if e:
                return e
        elif shape == "custom":
            pairs, e = self._parse_linear_pattern(v["shape_polygon"].get())
            if e:
                return f"{label}Custom shape: {e}."
            if not pairs or len(pairs) < 3:
                return (f"{label}Custom shape needs at least 3 points: draw an "
                        f"outline in the Footprint card, or pick another "
                        f"shape.")
            import scaffold_gcode  # lazy: keep GUI start-up independent of it
            norm = scaffold_gcode._normalize_polygon(
                [(float(a), float(p)) for a, p in pairs])
            if not scaffold_gcode._polygon_is_convex(norm):
                return (f"{label}Custom shape must be convex for now (concave "
                        f"outlines aren't supported yet).")
        e = self._valid_hex(v["color"].get(), label + "Viewer color")
        if e:
            return e
        return None

    def _deposition_by_layer(self):
        """True when the Export "Deposition order" control is set to interleave by
        layer height across scaffolds (vs the default, by scaffold number). Absent
        in a partial build -> False (the original by-scaffold order)."""
        var = getattr(self, "s_deposition_order", None)
        return bool(var is not None and var.get() == "layer")

    def _expanded_specs_and_sources(self):
        """The full expanded scaffold list AND its parallel source tags, built
        together so they can never fall out of step.

        EVERY scaffold is expanded into its own rectangular array (a scaffold whose
        array box is off contributes a single copy); the concatenation is emitted as
        one continuous fiber. `sources[j]` is part j's 0-based ORIGINAL-scaffold
        index, for the player groups and viewer colours.

        Order follows the Export "Deposition order" control: "scaffold" (default)
        is all of scaffold 1's copies, then scaffold 2's; "layer" is a STABLE sort
        of the whole list by each copy's start_z, so copies from different
        scaffolds interleave by height and ties keep the by-scaffold order."""
        specs, sources = [], []
        for i in range(self._scaffold_count()):
            arr = self._array_specs(i)
            parts = arr if arr is not None else [self._scaffold_spec(i)]
            specs.extend(parts)
            sources.extend([i] * len(parts))
        if self._deposition_by_layer():
            order = sorted(range(len(specs)),
                           key=lambda j: specs[j].get("start_z", 0.0))
            specs = [specs[j] for j in order]
            sources = [sources[j] for j in order]
        return specs, sources

    def _array_copy_counts(self):
        """Per-scaffold number of rectangular-array copies -- nx*ny*nz when its box
        is on, else 1. Cheap: reads only the count fields (never the full spec), so
        it drives the by-scaffold source tags without building any geometry."""
        counts = []
        for i in range(self._scaffold_count()):
            v = self.scaf_vars[i]
            if v["array_on"].get():
                nx = max(1, int(float(v["array_nx"].get())))
                ny = max(1, int(float(v["array_ny"].get())))
                nz = max(1, int(float(v["array_nz"].get())))
                counts.append(nx * ny * nz)
            else:
                counts.append(1)
        return counts

    def _array_sources(self):
        """The 0-based ORIGINAL-scaffold index of each part in the expanded stack,
        parallel to the g-code's scaffold1..scaffoldK order, so the player groups
        and viewer colours can tag every array copy to the scaffold it came from.
        Same order as _expanded_scaffold_specs.

        By scaffold number this is the cheap count-only path (each scaffold's
        copies are contiguous), so it also works for a minimal host that never
        builds a full spec. By layer height it must follow the Z-sorted combined
        list, so it comes from the shared builder."""
        if self._deposition_by_layer():
            return self._expanded_specs_and_sources()[1]
        sources = []
        for i, c in enumerate(self._array_copy_counts()):
            sources.extend([i] * c)
        return sources

    def _expanded_scaffold_specs(self):
        """The full scaffold spec list for the JSON path (see
        _expanded_specs_and_sources). Part j's source scaffold is
        _array_sources()[j] (same order)."""
        return self._expanded_specs_and_sources()[0]

    def _validate_scaffold(self):
        # Frozen: no loose .py; the exe re-launches as `worker:scaffold` (see the
        # matching guard in _validate_gcode). Skip the source-only script check.
        if not getattr(sys, "frozen", False) and not os.path.isfile(SCAFFOLD_WORKER):
            return f"Cannot find the worker script:\n{SCAFFOLD_WORKER}"
        n = self._scaffold_count()
        for i in range(n):
            label = "" if n == 1 else f"Scaffold {i + 1}: "
            e = self._validate_one_scaffold(self.scaf_vars[i], label)
            if e:
                return e
        # every scaffold can now be arrayed independently, so validate the array
        # fields of ALL scaffolds whose box is on (they all tile at Generate time)
        for i in range(n):
            av = self.scaf_vars[i]
            if not av["array_on"].get():
                continue
            label = f"Scaffold {i + 1} array "
            for name, key in (("columns (X)", "array_nx"),
                              ("rows (Y)", "array_ny"),
                              ("layers (Z)", "array_nz")):
                _, e = self._int_at_least(av[key].get(), label + name, 1)
                if e:
                    return e
            for name, key in (("X spacing", "array_dx"),
                              ("Y spacing", "array_dy"),
                              ("Z spacing", "array_dz"),
                              ("travel offset", "array_travel")):
                _, e = self._pos_float(av[key].get(), label + name,
                                       allow_zero=True)
                if e:
                    return e
        if self.stab_on.get():
            for name, var in (("Stabilization width", self.stab_width),
                              ("Stabilization height", self.stab_height),
                              ("Stabilization stepover", self.stab_stepover)):
                _, e = self._pos_float(var.get(), name)
                if e:
                    return e
            _, e = self._int_at_least(self.stab_layers.get(),
                                      "Stabilization layers", 1)
            if e:
                return e
            for name, var in (("Stabilization start X", self.stab_start_x),
                              ("Stabilization start Y", self.stab_start_y)):
                _, e = self._any_float(var.get(), name)
                if e:
                    return e
            if self.stab_arc_radius_mult.get().strip():
                _, e = self._pos_float(self.stab_arc_radius_mult.get(),
                                       "Stabilization turnaround radius (x spacing)",
                                       allow_zero=True)
                if e:
                    return e
        # sent only when the move is on, blank meaning the 5 mm default
        # (`_build_args_scaffold`)
        if self.s_exit_move.get():
            _, e = self._pos_float(self.s_exit_margin.get().strip() or "5",
                                   "Exit distance", allow_zero=True)
            if e:
                return e
        if self.s_make_stl.get():
            e = self._faces_error(self.s_faces.get())
            if e:
                return e
            if self._sag_solver_value(self.s_sag_mode) != "off":
                _, e = self._pos_float(self.s_sag_mult.get(), "Sag multiplier",
                                       allow_zero=True)
                if e:
                    return e
                if self.s_sag_reset.get():
                    _, e = self._int_at_least(self.s_sag_reset_layers.get(),
                                              "Reset sagging every N layers", 1)
                    if e:
                        return e
        # Corner resolution. Validated OUTSIDE the make-STL branch: it shapes the
        # deposited centreline, so it reaches the .depo cache, the simulation and
        # the live preview even when no mesh is built.
        if self._fill_per_tau_active():
            e = self._validate_fill_per_tau()
            if e:
                return e
        # A runaway size (a stepover of 0.0001 for 0.1, 100000 layers for 100)
        # is refused here with the generator's own estimate, before a worker
        # spends an hour writing gigabytes the user never meant to ask for
        if self.s_collector.get() == "plate":
            e = self._program_size_error()
            if e:
                return e
        # the plate datum sets the print start Z (baked into the g-code), so its
        # fields are validated whether or not an STL is rendered
        return self._validate_plate()

    def _program_size_error(self):
        """The generator's size refusal for the scaffolds as entered, or None.
        Anything that cannot be built into a spec here is left to the worker,
        which reports it in its own words."""
        import scaffold_gcode
        try:
            specs = [scaffold_gcode._scaffold_from_spec(s)
                     for s in self._expanded_scaffold_specs()]
            scaffold_gcode._check_program_size(specs)
        except ValueError as exc:
            msg = str(exc)
            return msg[:1].upper() + msg[1:] + "."
        except Exception:  # noqa: BLE001 -- not this check's call
            return None
        return None

    def _fill_per_tau_active(self):
        """True when the Corner-resolution field bites: the physics deposition
        transform is what builds the transient windows it refines. False in a build
        with no sim mixin (the field then simply never reaches a worker)."""
        v = getattr(self, "sim_physics_deposit", None)
        return bool(v is not None and v.get())

    def _validate_fill_per_tau(self):
        """None, or the error message. Capped as well as floored: the refine grid
        is built at `tau/fill_per_tau` and only THEN thinned to the point cap, so a
        mistyped 6000 allocates the huge grid before anything trims it -- a freeze,
        not a slow render. 96 is past the point the crest angle stops moving
        visibly, so nothing useful is refused."""
        n, e = self._int_at_least(self.s_fill_per_tau.get(),
                                  "Corner resolution", 1)
        if e:
            return e
        if n > 96:
            return ("Corner resolution is capped at 96 samples per lag time "
                    f"(got {n}). Above ~24 the corner stops visibly changing "
                    "and the grid gets expensive to build.")
        return None

    def _fill_per_tau_args(self):
        """`--fill-per-tau N` for mew_export / the player, or [] when the physics
        deposition is off (no transform -> nothing to refine, and the plain worker
        would reject the flag). Emitted explicitly whenever it applies so the
        worker's own default can never diverge from the field."""
        if not self._fill_per_tau_active():
            return []
        return ["--fill-per-tau", (self.s_fill_per_tau.get().strip() or "6")]

    def _auto_corner_grid_args(self, fiber_diameter):
        """`--auto-corner-grid D` for mew_export -- the 4-AXIS corner sampler.

        Gated on the same switch as `_fill_per_tau_args`, and for the same reason:
        with the physics deposition off there is no relay to refine, and the plain
        worker would reject the flag.

        Still NOT part of `_sim_play_args`. The live-adjust march is
        4-axis, so `gui_mandrel._fiber_params_mandrel` calls this and the tier
        DOES reach the player. `_sim_play_args` is still the wrong home because it
        is shared with FLAT runs, where the march seeds from a `.depo` built
        without a corner grid -- refining there would make a re-derived layer
        disagree with the cached ones beside it, and would cost
        `test_mew_incremental`'s live == batch bit-identity. `gcode_player` guards
        the same rule independently by ignoring the flag with no frame.
        """
        if not self._fill_per_tau_active():
            return []
        cq = getattr(self, "s_curve_quality", None)
        tier = (cq.get().strip().lower() if cq is not None else "") or "standard"
        return ["--auto-corner-grid", repr(float(fiber_diameter)),
                "--curve-quality", tier]

    def _flat_auto_corner_diameter(self, box=None):
        """The fiber diameter (mm) a PLATE scaffold's corner grid is sized on, or
        None when "Auto corner resolution" is unticked (or absent from this build).

        The finest diameter among the scaffolds this run prints, because the
        tolerance is a fraction of it and one grid serves the whole file: sizing
        on a coarser scaffold would hold the fine one to a looser bound than its
        own Curve-quality tier promises. A blank or mistyped diameter is skipped;
        the run's own validation reports it."""
        # `box` is the var that decides: the export's tickbox by default, the
        # preview's own (`pv_auto_corner_grid`) for the live picture.
        if box is None:
            box = getattr(self, "s_auto_corner_grid", None)
        if box is None or not box.get():
            return None
        ds = []
        for v in self.scaf_vars[:self._scaffold_count()]:
            try:
                d = float(v["diameter"].get())
            except (ValueError, TypeError):
                continue
            if d > 0.0:
                ds.append(d)
        return min(ds) if ds else None

    def _flat_auto_corner_args(self):
        """`--auto-corner-grid D --curve-quality T` for a PLATE scaffold run, or []
        when the box is unticked. Sent to BOTH the export (`_build_args_scaffold`)
        and the player (`_fiber_params_scaffold`): the live-adjust march re-lays
        layers beside the ones the `.depo` cached, so it must refine on the grid
        the export used or a re-derived layer disagrees with its neighbours. The
        mandrel and surface builders override both methods and send their own."""
        d = self._flat_auto_corner_diameter()
        return [] if d is None else self._auto_corner_grid_args(d)

    def _rotation_value(self, combo=None):
        """Numeric rotation from a rotation combobox (accepts '90' or
        '90 (square)'); defaults to scaffold 1's."""
        combo = combo if combo is not None else self.s_rotation
        return float(combo.get().strip().split()[0])

    def _sag_reset_arg(self):
        """--sag-reset-layers for a worker: the period, or "0" (= never reset) when
        the tickbox is off. Always emitted explicitly so the worker's own default of
        20 can never silently re-enable a reset the user unticked."""
        if not self.s_sag_reset.get():
            return "0"
        return self.s_sag_reset_layers.get().strip() or "0"

    def _gen_is_preview(self):
        """True when the Generate quality is a preview (wireframe/tubes): the run
        writes the sag + .depo cache but SKIPS the STL mesh build (--no-mesh). The
        exact mesh is built later on demand (export / viewing Exact)."""
        return RENDER_LBL2VAL.get(self.gen_render.get(), "exact") != "exact"

    def _build_args_gcode(self):
        # `--progress` rides on EVERY export argv (see _build_args_scaffold).
        src = self.in_var.get().strip()
        args = self._export_prefix("worker:convert", "converter", src) + [
                src, "--progress",
                "-d", self.diameter.get().strip(),
                "-f", self.faces.get().strip()]
        out = self.out_var.get().strip()
        if out:
            args += ["-o", out]
        args += self._mesh_tol_args_for(getattr(self, "mesh_tol", None))
        args += self._sag_solver_args(self.sag_mode, self.sag_mult)
        # Travel deposition is a property of the JET, not of one tab's geometry and
        # not of a checkbox: in MEW the jet never stops, so the travel IS deposited
        # and is always solved with the rest of the print. The user chooses only
        # whether it reaches the EXPORTED mesh -- the viewer's Show/Hide travel
        # fiber handles the rest, off the same .depo either way.
        args.append("--render-travel")
        if bool(getattr(self, "s_travel_in_stl", None)
                and self.s_travel_in_stl.get()):
            args.append("--travel-in-stl")
        if self._gen_is_preview():          # sag + .depo only; skip the mesh build
            args.append("--no-mesh")
        args += self._plate_args()
        return args

    def _build_args_scaffold(self):
        # `--progress` makes the worker report how far through its work it is, so
        # the bottom bar can run 0-100 % instead of bouncing (gui_run._on_progress).
        # It changes no output, and goes on the FRONT of the argv so it survives the
        # mew_export wrapper, which forwards what it does not recognise verbatim.
        args = self._export_prefix("worker:scaffold", "scaffold") + [
                "--progress", "-o", self.s_gcode_out.get().strip()]
        # Add the placement datum to Start X/Y for every scaffold and the
        # stabilization scaffold. The modern shell measures from the plate.
        cdx, cdy = self._scaffold_origin_xy()
        n = self._scaffold_count()
        any_array = any(self.scaf_vars[i]["array_on"].get() for i in range(n))
        # per-layer parameters travel only in the JSON spec
        any_layers = any(self.scaf_vars[i]["layer_overrides"].get().strip()
                         for i in range(n))
        if n > 1 or any_array or any_layers:
            # JSON path: EVERY scaffold, each expanded into its OWN rectangular
            # array. The combined list is one continuous fiber, so all the arrays
            # end up in the same result. -d / --arc-segments are the STL step's
            # global default (scaffold 1's); each spec carries its own
            # fiber_diameter in the JSON, so every copy renders at ITS scaffold's
            # tube diameter.
            specs = self._expanded_scaffold_specs()
            json_path = self._write_scaffolds_json(specs)
            args += ["--scaffolds-json", json_path,
                     "-d", self.scaf_vars[0]["diameter"].get().strip(),
                     "--arc-segments",
                     self.scaf_vars[0]["arc_segments"].get().strip()]
            if any_array:
                # inter-scaffold travel clearance: one global value for the run, so
                # take the largest array's -- enough room to clear every arrayed one.
                offs = [float(self.scaf_vars[i]["array_travel"].get() or 0)
                        for i in range(n) if self.scaf_vars[i]["array_on"].get()]
                args += ["--travel-offset", repr(max(offs))]
        elif n == 1:
            # single scaffold: the original flat flags, byte-for-byte
            v = self.scaf_vars[0]
            args += ["--width", v["width"].get().strip(),
                     "--height", v["height"].get().strip(),
                     "--stepover", v["stepover"].get().strip(),
                     "--rotation", repr(self._rotation_value(v["rotation"])),
                     "-d", v["diameter"].get().strip(),
                     "--layers", v["layers"].get().strip(),
                     "--speed", v["speed"].get().strip(),
                     "--start-x", repr(float(v["start_x"].get()) + cdx),
                     "--start-y", repr(float(v["start_y"].get()) + cdy),
                     "--start-z", v["start_z"].get().strip(),
                     "--start-angle", v["start_angle"].get().strip(),
                     "--arc-segments", v["arc_segments"].get().strip()]
            if v["arc_radius_mult"].get().strip():
                args += ["--arc-radius-mult", v["arc_radius_mult"].get().strip()]
            if v["loop_speed"].get().strip():
                args += ["--loop-speed", v["loop_speed"].get().strip()]
            if v["link_loops_on"].get():
                args += ["--link-loops", v["link_loops_mode"].get(),
                         "--link-loop-angle", v["link_loop_angle"].get().strip(),
                         "--link-loop-dist", v["link_loop_dist"].get().strip()]
                # The UI and the CLI agree in sense: emit the flag when TICKED.
                if v["link_loop_travel"].get():
                    args += ["--link-loop-travel"]
            if v["perimeter_travel"].get():
                args += ["--perimeter-travel", "--perimeter-travel-offset",
                         v["perimeter_travel_offset"].get().strip()]
            if v["stepover_end"].get().strip():   # blank: byte-identical
                args += ["--stepover-end", v["stepover_end"].get().strip(),
                         "--grade-angle",
                         v["grade_angle"].get().strip() or "0"]
            if v["angle_pattern_on"].get():
                # overrides --rotation / --start-angle in the worker
                self._sync_angle_pattern(v)
                args += ["--angle-pattern", v["angle_pattern"].get().strip()]
                # joined --opt=value form: a period-offset list can lead with a
                # negative value, which argparse would otherwise read as an option
                # flag (the same negative-safe trick the origin uses).
                sp = v["angle_stepover_pattern"].get().strip()
                if sp:
                    args += [f"--angle-stepover-pattern={sp}"]
                of = v["angle_offset_pattern"].get().strip()
                if of:
                    args += [f"--angle-offset-pattern={of}"]
            if v["linear_on"].get():
                self._sync_linear_pattern(v)
                args += ["--linear-pattern", v["linear_pattern"].get().strip(),
                         "--linear-wavelength",
                         v["linear_wavelength"].get().strip()]
                sp = v["linear_spacing"].get().strip()
                if sp:
                    args += ["--linear-spacing", sp]
            # footprint shape (omitted for rectangle -> byte-identical default).
            # --width/--height above already carry the bounding box the GUI keeps
            # synced for every shape.
            shape = v["shape"].get()
            if shape != "rectangle":
                args += ["--shape", shape]
                if shape in ("circle", "ellipse", "polygon"):
                    args += ["--shape-sides", v["shape_sides"].get().strip()]
                if shape == "custom":
                    self._sync_shape_pattern(v)
                    args += ["--footprint-polygon",
                             v["shape_polygon"].get().strip()]
            # footprint rotation. Omitted at 0 so an unrotated scaffold stays
            # byte-identical; a nonzero angle spins the whole footprint.
            fa = v.get("footprint_angle")
            if fa is not None:
                try:
                    fav = float(fa.get())
                except Exception:  # noqa: BLE001 -- validated in _validate_one_scaffold;
                    fav = 0.0      # 0.0 fallback is belt-and-braces, never reached
                if fav:
                    args += ["--footprint-angle", repr(fav)]
        # G2/G3 true arcs is a global machine capability; the worker applies the
        # flag to every scaffold (and the stabilization base) in either mode.
        if self.s_g2g3.get():
            args.append("--g2g3-arcs")
        # NB: "Don't adjust height per layer" (Machine page) deliberately sends
        # NOTHING here. It is a USER-FACING SPELLING, applied by
        # `gui_scaffold._rewrite_gcode_lines` to the g-code text view and
        # Save-g-code-as only. The worker keeps emitting the full stacked ladder
        # so the sag solve, the .depo cache, the preview, the viewer and the
        # player all predict the real print -- at one commanded Z the fibers
        # still sag and stack. `scaffold_gcode`'s
        # `--flat-layers` still exists and still flattens the GEOMETRY; it is a
        # CLI-only flag now, and sending it from here is what made the checkbox
        # collapse the STL along with the file.
        if not self.s_exit_move.get():
            args.append("--no-exit-move")
        else:
            # Only when the move is ON: --exit-margin with --no-exit-move is
            # accepted but meaningless, and sending it anyway puts a number in
            # the run log that had no effect.
            args += ["--exit-margin", self.s_exit_margin.get().strip() or "5"]
        if self.stab_on.get():
            args.append("--stabilization")
            args += ["--stab-width", self.stab_width.get().strip(),
                     "--stab-height", self.stab_height.get().strip(),
                     "--stab-stepover", self.stab_stepover.get().strip(),
                     "--stab-layers", self.stab_layers.get().strip(),
                     "--stab-start-x", repr(float(self.stab_start_x.get()) + cdx),
                     "--stab-start-y", repr(float(self.stab_start_y.get()) + cdy)]
            if self.stab_arc_radius_mult.get().strip():
                args += ["--stab-arc-radius-mult",
                         self.stab_arc_radius_mult.get().strip()]
        if self.s_make_stl.get():
            args += ["--stl", self.s_stl_out.get().strip(),
                     "-f", self.s_faces.get().strip()]
            args += self._mesh_tol_args()
            args += self._sag_solver_args(self.s_sag_mode, self.s_sag_mult,
                                          with_reset=True)
            # The jet deposits while travelling, always. Consumed by mew_export
            # when physics deposition is on (it then also carries the lag state
            # THROUGH the travel), else forwarded by scaffold_gcode to the
            # converter, which lays the travel on its commanded line. Same flag
            # name either way. --travel-in-stl is the user's choice on top: it
            # decides only whether the solved travel is tubed into the STL.
            args.append("--render-travel")
            if self.s_travel_in_stl.get():
                args.append("--travel-in-stl")
            # deposition order across scaffolds: sag in g-code order so a
            # by-scaffold-number stack rests each scaffold on the last. Gated (see
            # the helper), so it is omitted for every normal print.
            args += self._layer_by_deposition_arg()
            if self._gen_is_preview():      # sag + .depo only; skip the mesh build
                args.append("--no-mesh")
        # "Auto corner resolution": the .depo is built whether or not an STL is,
        # so this rides every run, like Corner resolution does.
        args += self._flat_auto_corner_args()
        # the plate datum + machine bounds go to the worker on EVERY scaffold run:
        # the print start Z is baked into the g-code even when no STL is made.
        args += self._plate_args()
        return args

    def _scaffold_spec(self, i):
        """Scaffold i's fields as a JSON-ready dict for --scaffolds-json. Every
        value is already validated, so the float/int conversions are safe.
        Blank arc-radius / loop-speed are omitted, matching the single-scaffold
        flags (the worker then inherits the 1.5x default / the print speed)."""
        v = self.scaf_vars[i]
        # Use the same placement datum as the single and stabilization paths.
        cdx, cdy = self._scaffold_origin_xy()
        spec = {
            "width": float(v["width"].get()),
            "height": float(v["height"].get()),
            "stepover": float(v["stepover"].get()),
            "rotation": self._rotation_value(v["rotation"]),
            "fiber_diameter": float(v["diameter"].get()),
            "layers": int(v["layers"].get()),
            "speed": float(v["speed"].get()),
            "start_x": float(v["start_x"].get()) + cdx,
            "start_y": float(v["start_y"].get()) + cdy,
            "start_z": float(v["start_z"].get()),
            "start_angle": float(v["start_angle"].get()),
            "arc_segments": int(v["arc_segments"].get()),
        }
        fa = v.get("footprint_angle")
        if fa is not None:
            try:
                fav = float(fa.get())
            except Exception:  # noqa: BLE001 -- validated in _validate_one_scaffold;
                fav = 0.0      # 0.0 fallback is belt-and-braces, never reached
            if fav:                          # omit the default so 0 stays byte-identical
                spec["footprint_angle"] = fav
        if v["arc_radius_mult"].get().strip():
            spec["arc_radius_mult"] = float(v["arc_radius_mult"].get())
        if v["loop_speed"].get().strip():
            spec["loop_speed"] = float(v["loop_speed"].get())
        if v["link_loops_on"].get():
            spec["link_loops"] = v["link_loops_mode"].get()
            spec["link_loop_angle"] = float(v["link_loop_angle"].get())
            spec["link_loop_dist"] = float(v["link_loop_dist"].get())
            spec["link_loop_travel"] = bool(v["link_loop_travel"].get())
        if v["perimeter_travel"].get():      # omitted when off: byte-identical
            spec["perimeter_travel"] = True
            spec["perimeter_travel_offset"] = float(
                v["perimeter_travel_offset"].get())
        if v["angle_pattern_on"].get():
            self._sync_angle_pattern(v)
            angles, _e = self._parse_angle_pattern(v["angle_pattern"].get())
            if angles:                       # overrides this scaffold's rotation
                spec["angle_pattern"] = angles
                # OPTIONAL per-layer stepover / period offset (aligned to the
                # angles; blank slots -> None -> the scaffold default). Only sent
                # when they carry a real value, so a plain angle pattern is inert.
                steps, _es = self._parse_layer_value_list(
                    v["angle_stepover_pattern"].get())
                offs, _eo = self._parse_layer_value_list(
                    v["angle_offset_pattern"].get())
                if steps and any(x is not None for x in steps):
                    spec["angle_stepovers"] = steps
                if offs and any(x is not None for x in offs):
                    spec["angle_offsets"] = offs
        if v["stepover_end"].get().strip():  # blank stays byte-identical
            spec["stepover_end"] = float(v["stepover_end"].get())
            spec["grade_angle"] = float(v["grade_angle"].get() or 0)
        lo = v["layer_overrides"].get().strip()
        if lo:                               # blank stays byte-identical
            spec["layer_overrides"] = json.loads(lo)
        if v["linear_on"].get():
            self._sync_linear_pattern(v)
            pairs, _e = self._parse_linear_pattern(v["linear_pattern"].get())
            if pairs:
                spec["linear_pattern"] = [[a, p] for (a, p) in pairs]
                spec["linear_wavelength"] = float(v["linear_wavelength"].get())
                sp = v["linear_spacing"].get().strip()
                if sp:
                    spec["linear_spacing"] = float(sp)
        # footprint shape. Rectangle is left implicit (the worker default). For
        # circle/polygon the bbox is square (height := width); for custom it is the
        # drawing's bbox, so width/height stay the footprint bounding box the
        # inter-scaffold / exit routing relies on, whatever the GUI fields hold.
        shape = v["shape"].get()
        if shape != "rectangle":
            spec["shape"] = shape
            if shape in ("circle", "ellipse", "polygon"):
                spec["shape_sides"] = int(float(v["shape_sides"].get()))
            if shape in ("circle", "polygon"):
                spec["height"] = spec["width"]
            if shape == "custom":
                self._sync_shape_pattern(v)
                pairs, _e = self._parse_linear_pattern(v["shape_polygon"].get())
                pairs = pairs or []
                spec["footprint_polygon"] = [[a, p] for (a, p) in pairs]
                if pairs:
                    xs = [p[0] for p in pairs]
                    ys = [p[1] for p in pairs]
                    spec["width"] = max(xs) - min(xs)
                    spec["height"] = max(ys) - min(ys)
        return spec

    def _write_scaffolds_json(self, specs):
        """Write the given scaffold specs beside the output g-code and return the
        path. Tracked in self._scaffolds_json_tmp so _on_finished/on_close delete
        it -- it's a transient handoff to the worker, not an output. `specs` is
        the explicit list to write (the multi-scaffold sub-tabs, or the
        rectangular-array tiling of one)."""
        base = os.path.splitext(self.s_gcode_out.get().strip())[0]
        path = base + ".scaffolds.json"
        with open(path, "w") as fh:
            json.dump(specs, fh, indent=2)
        self._scaffolds_json_tmp = path
        return path

    def _fiber_params_gcode(self):
        """Fiber params (player CLI args) matching the G-code tab's STL run, so
        the player's deposition view rebuilds the same sagged mesh."""
        a = ["--diameter", self.diameter.get().strip(),
             "--faces", self.faces.get().strip()]
        a += self._sag_solver_args(self.sag_mode, self.sag_mult)
        # this panel's own "Mesh detail", for the same reason as the scaffold
        # tab's (see _mesh_tol_args): the exact view must match the STL it wrote
        a += self._mesh_tol_args_for(getattr(self, "mesh_tol", None))
        a += self._render_travel_arg()
        return a

    def _mesh_tol_args(self):
        """--mesh-tolerance from the Export panel's "Mesh detail" combo.

        Sent to the converter worker AND to the player: it decides how many of the
        sag solver's samples become tube rings, and the Full-mesh view is the one
        that claims to BE the STL, so sending it to only one of them made
        build_deposition_mesh's "identical to the STL" promise false.

        NOT a cache key: the `.depo` always stores the FULL sampling and the
        thinning happens on the way into the tube, so -- unlike --sag-approx /
        --render-travel -- changing it cannot make the player's cache disagree
        with the geometry on disk. It is frozen into the run's fiber params at
        Generate, so editing the combo afterwards cannot make the viewer disagree
        with the STL already written."""
        return self._mesh_tol_args_for(getattr(self, "s_mesh_tol", None))

    @staticmethod
    def _mesh_tol_args_for(var):
        """--mesh-tolerance from a Mesh-detail combo var (either panel's)."""
        val = MESH_DETAIL_LBL2VAL.get(var.get() if var is not None else None)
        return ["--mesh-tolerance", val] if val is not None else []

    def _sag_solver_args(self, mode_var, mult_var, with_reset=False):
        """The sag-solver flags for a worker OR the player, from one Sag-solver
        combo. Both consume the same spelling, which is what keeps the player's
        `.depo` cache key in step with the geometry the export just wrote.

          off         -> --no-sag
          exact       -> --sag-multiplier ...
          approximate -> --sag-approx + the same

        `with_reset` adds --sag-reset-layers, which only the scaffold side sends
        (the G-code-import panel has no reset control). Byte-identical argv at the
        "Exact" default.

        The Physics page's Fiber sagging law switch overrides the combo: off is
        --no-sag for every panel, the same as choosing Off here."""
        mode = self._sag_solver_value(mode_var)
        law = getattr(self, "sim_sag_law", None)
        if mode == "off" or (law is not None and not law.get()):
            return ["--no-sag"]
        a = ["--sag-approx"] if mode == "approximate" else []
        a += ["--sag-multiplier", mult_var.get().strip()]
        if with_reset:
            a += ["--sag-reset-layers", self._sag_reset_arg()]
            # the designer's Sag resolution rides the same side as the reset,
            # and to the player too: it is a .depo cache key. Nothing at Standard.
            a += self._sag_resolution_args_for(
                getattr(self, "s_sag_resolution", None))
        return a

    @staticmethod
    def _sag_resolution_args_for(var):
        """--sag-resolution from a Sag-resolution combo var holding a LABEL, or []
        at Standard (or an unknown label), so a default run's argv is unchanged."""
        val = SAG_RESOLUTION_LBL2VAL.get(var.get() if var is not None else None)
        return [] if val in (None, "1") else ["--sag-resolution", val]

    def _render_travel_arg(self):
        """--render-travel for the PLAYER: a .depo cache key, not a mode the player
        implements. The export writes travel geometry into the cached fibers WITHOUT
        changing the fiber count (merge_travels preserves it), so the player cannot
        tell a travel cache from a plain one by any count guard -- it has to be told.
        Omitting this would let the locked sim reject the very cache the run just
        wrote and silently rebuild from the commanded path.

        Unconditional, since the run always asks for travel fiber. Whether it is
        SHOWN is the viewer's own toggle and whether it reached the STL is
        --travel-in-stl; neither is a property of the cache. `--travel-gcode-line`
        rides along here as the same panel's other travel choice."""
        return ["--render-travel"] + (
            ["--travel-gcode-line"]
            if bool(getattr(self, "s_travel_gcode_line", None)
                    and self.s_travel_gcode_line.get()) else [])

    def _layer_by_deposition_arg(self):
        """--layer-by-deposition for the converter (and a .depo cache key, like
        --render-travel), gated so it can only ever CHANGE an interleaved
        by-scaffold-number stack -- never a normal print.

        "By layer height" already emits the g-code in ascending Z, so the default
        by-height sag matches it exactly and no flag is sent. "By scaffold number"
        needs the flag ONLY when the emission order departs from height order (a
        later-emitted copy sitting below an earlier one). For every monotonic-Z
        print the flag is omitted, so the sag solve stays byte-for-byte the
        default by-height path."""
        if self._deposition_by_layer():
            return []                       # height-sorted g-code; by-height matches
        # only a genuine multi-body stack (>1 scaffold, or one arrayed in Z) can
        # interleave in Z; short-circuit before building any geometry otherwise (so
        # a single-scaffold host needs no _array_specs at all).
        n = self._scaffold_count()
        any_array = any(self.scaf_vars[i]["array_on"].get() for i in range(n))
        if n <= 1 and not any_array:
            return []
        zs = [s.get("start_z", 0.0) for s in self._expanded_scaffold_specs()]
        departs = any(zs[j + 1] < zs[j] - 1e-9 for j in range(len(zs) - 1))
        return ["--layer-by-deposition"] if departs else []

    def _fiber_params_scaffold(self):
        """Fiber params matching the scaffold tab. The fiber diameter is always
        meaningful (it's the written line width), so deposition works even when
        this run made no STL; the sag/faces options only apply when it did.

        The fallback --diameter / --arc-segments are scaffold 1's (the STL step's
        -d); the per-part --scaffold-groups below override them per scaffold."""
        a = ["--diameter", self.scaf_vars[0]["diameter"].get().strip()]
        # "Mesh detail" rides along even with the STL step OFF, unlike faces/sag:
        # there is still an STL in that case -- the on-demand --from-depo build --
        # and it reads its multiplier back out of these frozen params, so leaving
        # the flag out here is what would let the two disagree.
        a += self._mesh_tol_args()
        if self.s_make_stl.get():
            a += ["--faces", self.s_faces.get().strip()]
            a += self._sag_solver_args(self.s_sag_mode, self.s_sag_mult,
                                       with_reset=True)
        # arc-approximation fidelity, so the player's deposition overlay matches
        # the STL when g2/g3 arcs are used (the drawn path stays smooth regardless)
        a += ["--arc-segments", self.scaf_vars[0]["arc_segments"].get().strip()]
        a += self._render_travel_arg()      # .depo cache key (see the helper)
        # --layer-by-deposition is deliberately NOT sent to the player: its live
        # march already re-lays in g-code (deposition) order via _SagState, which
        # is what the flag bakes into the locked .depo, so it reproduces either
        # mode with no flag -- unlike --render-travel, which the live march cannot
        # reproduce. It rides only on the converter build; a toggle forces a
        # regenerate.
        # per-part groups for the player, matching the g-code order the worker
        # emits: the stabilization base, then each EXPANDED scaffold copy at ITS
        # source scaffold's layers / diameter / colour. The stab base inherits
        # scaffold 1's diameter AND colour (no own fields), matching scaffold_gcode,
        # which prints it as scaffold 1's lead-in; a blank colour field is omitted,
        # so the player falls back to its solid colour.
        sources = self._array_sources()
        groups = []
        if self.stab_on.get():
            g = {"name": "stab",
                 "layers": int(float(self.stab_layers.get())),
                 "fiber_diameter": float(self.scaf_vars[0]["diameter"].get())}
            if self.scaf_vars[0]["color"].get().strip():
                g["color"] = self.scaf_vars[0]["color"].get().strip()
            groups.append(g)
        for j, src in enumerate(sources):
            v = self.scaf_vars[src]
            g = {"name": f"scaffold{j + 1}",
                 "layers": int(float(v["layers"].get())),
                 "fiber_diameter": float(v["diameter"].get())}
            if v["color"].get().strip():
                g["color"] = v["color"].get().strip()
            groups.append(g)
        a += ["--scaffold-groups", json.dumps(groups)]
        # the same corner grid the export was built on (see the helper)
        a += self._flat_auto_corner_args()
        return a
