# MEWracle — CODE MAP

> **What this is:** the exhaustive *feature → file → line* index for the codebase, plus a
> task-oriented "where do I change X" lookup. It is the middle tier between `CLAUDE.md`
> (orientation, loaded every session) and the module docstrings (the contracts).
> **Read this first when locating code** — it saves reading the big files.
>
> **Line numbers were re-anchored against `main` 285bce8 on 2026-09-24.** They drift as code
> changes, so trust the symbol written beside a pointer over its number. A pointer with no
> symbol beside it points at a region — prefer `` `symbol` (`:NN`) `` when adding one.
>
> **Spelling (2026-09-15): the user-facing word is "fiber", never "fibre".** Every label, tooltip,
> card title, info blurb and law description in the desktop GUI, in `mew_calibration`'s field
> registry, in `calibration_procedures` and in Lite's mirrored tables now says *fiber*; the Lite
> label-parity guards in `test_lite_params.py` hold the two sides together. **Code was NOT renamed**
> and must not be renamed casually: `straight_fibre` (a `JetState` field five modules read),
> `fibreD` (MEWracle Lite's HTTP payload key), `_FIBRE*` module constants, `lite_web/fibres.js` and
> ~110 `test_*fibre*` names still carry the other spelling. That sweep is its own branch with its
> own test run; the rename done here was bounded by a word boundary on both sides for exactly that
> reason.

---

## 0. The 30-second model

One build (`CLAUDE.md` has the full rules). Two pipelines, both ending in an STL,
both run as **subprocesses** the GUI launches (so an OOM mesh dies alone):

1. **Parameters → g-code → STL**: `engine/scaffold_gcode.py` emits serpentine g-code, chains into the converter.
2. **G-code → STL**: `engine/gcode_to_fibers.py` parses g-code → contacts → sag → tube mesh → STL (+ `.depo` cache).

The GUI (`gui/gcode_to_fibers_gui.py` + `gui/gui_*.py`) is one tkinter `App` of mixins. Results are viewed in the
**unified Viewer page** (§2.12), which embeds `viewers/gcode_player.py` — STL, deviation and micrograph are in-scene
modes of it. `viewers/stl_viewer.py` is no longer a user-facing viewer (§2.8): it survives as the offscreen render
backend (`--thumbnail`, `--shots`) and the shared VTK actor library the player and micrograph viewer import.

---

## 1. Module inventory

The codebase groups into these clusters.

### Physics cluster — the jet model and everything that consumes it
`physics/mew_physics.py`, `physics/mew_field.py`, `physics/mew_kinematics.py`, `physics/mew_coil_wave.py`, `physics/mew_deposition.py`,
`physics/mew_export.py`, `physics/mew_live.py`, `physics/mew_incremental.py`, `physics/mew_calibration.py`, `physics/mew_target.py`,
`gui/gui_sim.py`, `gui/gui_calibration.py`, `gui/gui_fiber_target.py` — with tests `test_mew_physics.py`, `test_mew_coil_wave.py`,
`test_mew_field.py`, `test_mew_kinematics.py`, `test_mew_deposition.py`, `test_mew_live.py`,
`test_mew_incremental.py`, `test_mew_calibration.py`, `test_dynamic_diameter.py`,
`test_physics_diameter.py`, `test_sim_diameter_wiring.py`, `test_sim_elevation_wiring.py`,
`test_jet_source.py` — plus the head mesh `machine_models/MEWron_0.2/Printhead STL.stl` (Lite serves its own lighter `lite/printhead.stl`).
Details in §2.11.

### G-code document engine (§2.22)
`engine/mew_gcode.py` (no Tk) — the text the G-code page shows and Save writes,
rendered from the generator's output + its line map + the user's chips —
with test `test_mew_gcode.py`. Saved flavors: `engine/flavor_presets.py` (no Tk, the
store) with `test_flavor_presets.py`. Details in §2.22.

### G-code page (§2.23)
`gui/gui_gcode_editor.py` (the page + the `GcodeEditor` widget) and
`gui/gui_gcode_changes.py` (the document, its edits: My library, the Anatomy,
the one edit form, pieces, persistence), `gui/gui_gcode_collector.py` (the
collector + 4-axis design tools) — modern UI only, with tests
`test_gcode_editor.py` + `test_gcode_collector.py` + `test_ribbon_fit.py`
(the ribbon at small windows).
`gui/gui_scripts.py` — **scripts**: My library's one store, the *automatic* switch
on a script (`Recipe.automatic`, "Run automatically on every print"), the
**My library window** (`ScriptLibrary`: New, edit, duplicate, delete; opened
from the Machine page's G-code card AND from the G-code page, over whichever
page is showing), and the Machine card's **Automatic commands** list (a view of
the automatic scripts). `_machine_recipes()` fills `OutputOptions.rig_recipes`
from the library at every render; the G-code tree shows each as a locked
`a:rig:i:j` row (kind `"auto"`). There is no second copy: the old
`machine_gcode_rules` JSON is only imported once (`_automatic_import_legacy`).
Header placeholders: `mew_gcode.BLOCK_PLACEHOLDERS` (header, end, scripts) vs
`RULE_PLACEHOLDERS` (`[[layer]]`, `[[z]]`… scripts only); the Machine card
previews the real header and flags a name that will not fill.
`gui/gui_flavor_editor.py` — the ONE flavor editor window, opened from the Machine
page and the page's Syntax menu (ribbon), with test `test_flavor_editor.py`.
Details in §2.23.

### Modern UI shell (§2.12)
`mewracle_gui.py`, `gui/gui_splash.py`, `gui/gui_shell.py`, `gui/gui_home.py`, `gui/gui_fonts.py`, `gui/gui_tips.py`,
`gui/gui_summary.py`, `gui/gui_tour.py`, `assets/tips/` (the tip pictures and their generator),
`gui/gui_modern_theme.py`, `gui/gui_widgets.py`, `gui/gui_pages.py`, `gui/gui_preview.py`, `engine/preview_sag.py`,
`gui/gui_viewer_shell.py`, `viewers/viewer_embed.py`, `gui/gui_embed.py`, `viewers/viewer_sidebar_spec.py`,
`viewers/view_cube.py`, with tests `test_ui_shell.py`, `test_gui_preview.py`, `test_preview_sag.py`,
`test_viewer_embed.py`, `test_view_cube.py` (+ the `MEWracle.bat` / `MEWracle Developer.bat`
launchers). `gui/gui_preview.py` and `engine/preview_sag.py` both import the physics/converter modules
(the touchdown relay; `gcode_to_fibers.sag_depth`).

### Machine-link page (§2.18)
`machine/moonraker_link.py` (the stdlib Klipper/Moonraker client + poller + fake-rig harness) and
`gui/gui_machine_link.py` (the page mixin), with tests `test_moonraker_link.py`,
`test_gui_machine_link.py`, and `test_machine_live.py` (the `gcode_player --machine-live`
child). The live machine view is a MODE of `gcode_player`, not a file of its own.

### Sag solvers — three engines, deliberately not one
`gcode_to_fibers.apply_sag` (EXACT, the reference), `engine/sag_approx.py` (the Export panel's
"Approximate": exact physics, analytic pair contacts, ~5× faster) and `engine/preview_sag.py` (the
scaffold preview's own drape, ~1000× faster and only ever drawn on screen). `tools/sag_bench.py` is
the harness that judges a candidate against the exact solver — `compare` for accuracy + the
time budget, `invariants` for phase-through and junction-shape, both with red proofs. Tests
`test_sag_approx.py`, `test_preview_sag.py`, `test_sag_reset.py`. **`engine/sag_approx.py` is shared
verbatim with MEWracle Lite; `engine/preview_sag.py` is not, and the two must not be merged** — see
§2.4 and the `sag_approx` docstring.

### Core (build-agnostic) modules
`gui/gui_common.py`, `gui/gui_theme.py`, `machine/machine_model.py`, `machine/machine_setup.py`, `engine/array_travel.py`,
`engine/gcode_exit.py`, `machine/bed_calibrator.py`, and the geometry/generator tests (`test_machine_model.py`,
`test_machine_setup.py`,
`test_build_plate.py`, `test_footprint_shapes.py`, `test_scaffold_gcode.py`,
`test_layer_rotation.py`, `test_link_loop_sag.py`, `test_vtk_helpers.py`,
`test_depo_render.py`).

### The GUI mixin-hook pattern (worth knowing before you edit `gui/gui_core.py`)
Every physics addition to the GUI funnels through a **no-op hook on `_CoreMixin`** that the
physics mixin overrides — `_build_sim_tab`/`_sim_persist_entries`/`_sim_play_args`/… overridden
by `gui_sim`, `_build_calibration_tab`/`_calibration_persist_entries`/… by `gui_calibration`,
and the analysis / sweeps / history / positioning equivalents by their pages. That indirection
existed so the old physics-free build could drop the mixins and still run. **It no longer
serves that purpose**, but it does still document the mixin contract and every override site,
and collapsing it is a real refactor (touching `gui_core`, every mixin, and `test_ui_shell`'s
persist-map parity guard) — not a cleanup to do in passing.

### Tooling
- `.bat` launchers: `MEWracle.bat` (launch GUI), `Install.bat`/`Setup.bat`/`update.bat`.

---

## 2. Subsystem index

### 2.1 GUI — assembly & tabs
The `App` is composed in `gui/gcode_to_fibers_gui.py:41` from mixins; `_CoreMixin.__init__`
(`gui/gui_core.py:163`) is the real constructor and builds the outer notebook `self.nb`
(`gui/gui_core.py:239`).

- **`gui/gcode_to_fibers_gui.py`** — `class App(...)` MRO (`:41`; the physics mixins lead so their hooks
  beat `_CoreMixin`'s no-ops, and `_CoreMixin` is last), `WINDOW_TITLE` override (`:44`),
  `_fit_to_screen` (`:47`), `main` (`:65`).
- **`gui/gui_mandrel.py` + `gui/gui_mandrel_preview.py`** — the **4-axis half of the Scaffold designer**.
  Since 2026-09-01 there is NO Mandrel page: the mandrel is the `"mandrel"` value of that page's
  `s_collector` toggle (`gui_scaffold._build_collector_toggle` / `_apply_collector_mode`), and
  `_active_mode()` reads the toggle rather than a notebook frame (`gui/gui_core.py`, and the modern
  shell inherits it). Sections show and hide with `grid_remove()` via `_collector_register`, the
  same mechanism the pill rail uses for scaffolds 2-5. **What the two collectors SHARE is one
  variable each**, not a copy: layers / speed / fibre Ø (`scaf_vars[0]`), the sag solver
  (`s_sag_mode`; `s_sag_mult` too, though it has no box any more — see below), tube facets
  (`s_faces`), the viewer colour, the Preview options,
  the Export block and the Presets — the mandrel's old `m.layers` / `m.speed` /
  `m.fiber_diameter` / `m.diam_override` / `m.sag_mode` / `m.sag_mult` / `m.faces` keys are GONE
  from the persist map (an older preset's copies are ignored on load, and `s.collector` defaults
  to `"plate"`, which is what such a preset always was). The **fibre-diameter override is the
  3-axis one** — `sim_diam_override` on the Physics page, live only while physics-deposit is on
  (Joseph, 2026-09-01) — so scaffold 1 has TWO diameter entries over one var and
  `gui_sim._apply_diam_mode` greys both (`diameter_entry` + `diameter_entry_tube`). What stays
  mandrel-owned is collector diameter, requested length, the PATTERN FAMILY and its knobs, the
  closure ladder (η, Rm), the speed policy, and the optional `S`/`R_ax`/`R_cir` placement radii
  — those last three deliberately, because `physics/mew_calibration.py:314` excludes them from the
  Physics field registry so they sit beside diameter and winding angle.
  **The PATTERN FAMILY (2026-09-01) is row 0 of the tube grid and decides which rows below it
  exist.** `MANDREL_FAMILY_CHOICES` (`gui/gui_mandrel.py:64`) maps the engine token to the human
  label; `m.family` persists the TOKEN and `_m_family_label` is the combobox's unpersisted
  display mirror, so re-wording a label cannot invalidate a saved preset. New persist keys:
  `m.family`, `m.cell_w`, `m.cell_h`, `m.cell_fit`, `m.rib_angle`, `m.cell_amplitude` (every
  pre-existing `m.*` key is kept). Rows are registered with `_mandrel_field(..., families=)` and
  swapped by `_apply_mandrel_family` — `grid_remove()`, the same idiom as `_collector_register`,
  so the two switches compose: `zigzag` owns winding angle ω + η + Rm, the four
  `mew_unit_cell` families own cell width W / cell height V / cell fit, `reentrant` adds the rib
  angle θ and the others the wave amplitude. Selecting `chevron` backfills a positive amplitude,
  because the engine refuses a zero-amplitude chevron by name; selecting **`custom`**
  backfills `MANDREL_DEFAULT_CELL` (a honeycomb) for the same reason — the engine refuses an
  empty drawing by name.
  **The DRAWN family owns a fourth section** (2026-09-04), `_build_mandrel_cell_editor_fields`:
  a `tk.Canvas` graph editor with three click modes (add/move node, join nodes, remove), a
  snap grid in CELL FRACTIONS, and the four neighbouring tiles drawn as ghosts so the weld
  across u = 1 and v = 1 is visible while drawing. Persist keys `m.cell_graph` +
  `m.cell_grid`; the edit MODE is deliberately not persisted (a click posture, not a design
  input, and `test_ui_shell` asserts its absence). Two contracts. **The canvas is a VIEW** —
  `m_cell_graph` is the only state, so a click, a drag, a typed edit and a preset load
  cannot disagree, the rule `gui_scaffold`'s stamp/stale trio enforces for the 3-axis
  editors. **And the validator is the ENGINE** — `_cell_validate` routes through
  `mew_unit_cell.preview_route`, the same tiler and Euler walk the worker runs, exactly as
  `_shape_is_convex` calls `scaffold_gcode._polygon_is_convex`, so the page cannot accept a
  cell the generator would refuse. `_cell_speed_note` adds the bend-tightness line and is
  the one number here that is ADVISORY: `machine_model.RotaryAxis` carries no motion limits,
  so it applies the Machine page's `sim_max_accel` (the X gantry's) to the surface path and
  says so in the line. It is read whether or not the accel-planner checkbox is on — that box
  governs whether the EXPORT is re-timed, and a cell is no less cornered for it being off.
  Beware: `_mandrel_cell_fits` and `_mandrel_build_cell_ladder` each call `cell_design`
  SEPARATELY, and `_mandrel_cell_ladder`'s cache key must list the drawing — all three
  shipped wrong once and the symptom was a resolved table of em dashes and a ladder showing
  the previous cell's rows.
  **The page is OUTCOME-FIRST since 2026-09-02**. The main
  column asks only for what you WANT — pattern, tube length, and either a **pore size in µm**
  (diamond helix) or the cell size (a cell family); the winding angle, the closure ladder, the
  two search bounds, the speed policy and the placement radii are in a collapsed **Advanced**
  group, which is what `_build_mandrel_closure_fields` now builds (the modern shell's Disclosure
  is titled "Advanced", the classic UI's third notebook tab likewise). Four new persist keys:
  `m.design_mode` (`pore` | `angle`), `m.pore_target` (µm), `m.length_tol` (%), `m.eta_cap`
  (blank = the solver's ceiling); `gui_presets._migrate_legacy_mandrel_mode` loads a preset that
  pins `m.angle` and names no mode in `angle` mode, which is what it always meant. Rows are now
  registered on TWO axes — `_mandrel_field(..., families=, modes=)` — and
  `_apply_mandrel_visibility` ANDs them in one pass, so a row constrained on both cannot flicker
  between two handlers. In `pore` mode `_mandrel_pore_design` calls
  `mew_tube.solve_tube_for_pore` (17.6 ms, cached on its own inputs) inside the existing 350 ms
  debounce, writes ω/η/Rm into the greyed Advanced fields through `_mandrel_write_solved`
  (which suppresses the debounce, or the write would resolve forever), and the argv carries the
  RESOLVED ω/η/Rm and the RESOLVED `--tube-length` — with η pinned `mandrel_design` returns the
  design directly (`engine/scaffold_gcode.py:1994`), so the export reproduces the picture and the CLI's
  out-of-sync note never fires. **`turn` mode (the DEFAULT since 2026-10-28) builds ω, length
  AND pore exactly**: `_mandrel_turn_design` -> `mew_tube.solve_turned_tube` (even N nearest the
  pore) -> `TurnedTubeDesign`, whose passes end in a pure-A turn onto the next pivot
  (`scaffold_gcode._zigzag_layer`); argv `--tube-pivots N` in place of `--tube-eta`, ladder pin
  `m.turn_pick`. The Tube card has no mode switch; Advanced's "Closure" radio picks `turn` or
  the two length-closing modes. Guard `test_mandrel_gcode.test_an_end_turn_tube_is_exactly_
  what_was_asked`.
  **`angle` mode resolves η/Rm too since 2026-10-28**
  (`_mandrel_angle_design` -> `mew_tube.solve_tube_options`: `solve_tube`'s nearest closing
  length, so the CLI's own answer at the default 5 %, plus one ladder row per OTHER pore size
  inside `m.length_tol`, pinned in `m.angle_pick`) and sends them the same way; Advanced's "Set η
  and Rm myself" (`m.closure_override`) is the old typed page, argv byte-identical to before. One
  entry point, `_mandrel_solved_design`, returns the page-resolved design or None when typed.
  Both ladders dropped their η and N columns: pore shows ω / Length / Pore, angle Length / Pore.
  A length that closes nowhere inside the tolerance raises `gui_mandrel._LengthGap` (one line,
  the nearest lengths from `mew_tube.nearest_closing_lengths`); the Tube box then draws those
  lengths as `Use` cells of its table (`_mandrel_render_gap`), the preview line reads
  `_MANDREL_GAP_STATUS`, and the G-code page shows NO banner for it (`_gce_check_design` asks
  `_mandrel_length_gap`). The last good tube stays drawn. Guard:
  `test_gui_mandrel.a_length_that_does_not_close_offers_the_nearest_that_do`.
  **The resolved panel is ONE table skeleton for both families**: rows are what you asked for,
  columns are Requested plus one Resolved column (helix) or two, Scaled and Fitted (cells).
  The cell-fit combobox is GONE — both fits are computed by `_mandrel_cell_fits` and the two
  `Use scaled` / `Use fitted` buttons are the only writers of `m.cell_fit`; the helix gets an
  alternatives ladder whose `Use` writes the rung's ω/η/Rm and flips to `angle` mode. Both carry
  the same chips and the same **true printed span** row (`length + 2*params["axial_overhang"]`,
  read off the design object, never re-derived). The sentence form in `m_cell_readout` is still
  maintained for both outcomes — it is where an engine refusal reaches the user verbatim — but
  is DISPLAYED only when the table is not. Every number is read off the `TubeDesign` /
  `TubeCellDesign`, and the A angle off the emitted g-code.
  `_build_args_mandrel` keeps the zigzag ANGLE-mode argv byte-identical (it does not even name
  `--tube-family`, whose CLI default is zigzag) and emits `--tube-family` + the cell flags
  otherwise; `_validate_mandrel` resolves the design so `UnitCellError` and `TubeDesignError`
  reach the user verbatim — a pore under the fibre floor is a message, never a clamped field —
  reach the user verbatim — a pore under the fibre floor is a message, never a clamped field —
  and EVERY INTEGER >= 1 is a legal layer count for EVERY family (`layers` counts plies
  everywhere, as it does on the 3-axis page; the nozzle steps out one diameter PER PLY, so an
  odd count just ends the circuit half way — 2026-09-03, was even-only while the step was per
  circuit).
  `build_mandrel_preview(family=...)` swaps ONLY the emitter; `cell_design` and `a_span_deg` are
  its two new helpers.
  **The preview draws the DEPOSITED path, not the commanded one, when "Physics-deposited
  scaffold" is on (2026-09-01).** `build_mandrel_preview(physics=...)` takes the same options
  dict the 3-axis preview does -- built by the one builder, `gui_preview._pv_snapshot_options`,
  reached through `_MandrelMixin._mandrel_physics_options`, which adds only the three
  mandrel-only placement radii `_calibration_args` folds into the export's profile -- parses with
  `with_times=True` and runs `mew_deposition.make_transform(...)(paths, ptimes, frame=frame)`.
  **The `frame=` is the 4-axis part**: it is what makes the transform build the mandrel
  `ContactPlacement` (`mew_mandrel.placement_for_frame`, gap = the model's working distance)
  instead of the flat branch, and it is the same call the export makes at
  `engine/gcode_to_fibers.py:8180`. Before this the 4-axis preview never called the transform at all, so
  every brick/re-entrant/chevron corner was razor sharp while the exported STL was lag-shaped.
  Physics off -> the parse, the drape call and the output are byte-identical to before, and the
  status line gains a `"physics · "` prefix exactly as the 3-axis one does.
  **That build is ASYNCHRONOUS since 2026-09-01.** It used to run inline on the
  Tk thread, so the whole window was dead for the length of it.
  `_refresh_mandrel_preview` now only SNAPSHOTS the Tk vars
  (`_mandrel_snapshot`, plain values, physics dict included — `_pv_snapshot_options`
  builds a fresh `MEWModel` per call, so nothing shared crosses the thread) and
  posts them to `gui_preview`'s worker under `PV_MANDREL`;
  `_mandrel_apply_result` applies on the Tk thread, drops a result whose
  generation id is stale, refreshes the resolved readout from the design the
  build actually used, and leaves the shared panel alone if the collector toggle
  has since flipped. Paired best-of-2 on one host, 10 plies, sag on, worst Tk
  heartbeat gap BEFORE → AFTER — zigzag (7 123 pts) 0.148 → 0.039 s, brick
  (37 749) 0.682 → 0.070 s, re-entrant (91 447) 1.314 → 0.101 s, re-entrant with
  physics (259 866) 7.963 → 0.268 s. **Edit-to-applied is unchanged** (brick
  1.03 → 1.10 s, the physics case 8.41 → 7.80 s): the build costs what it always
  did, it just no longer costs the window. What is left on the Tk thread is the
  ONE offscreen render of the finished tube (0.30 s of a 0.43 s gap at 260 k
  points; the `vtkTubeFilter` is only 0.17 s, so moving it to the worker would
  buy nothing), which is a render-window call and belongs there. Nothing cancels
  a running drape — the generation id discards it on arrival instead. Guarded by
  `test_gui_mandrel.a_slow_tube_build_never_freezes_the_window`, RED against
  702873f at 82 % of the rebuild frozen and GREEN at 6 %.
  `_install_mandrel_preview_traces` traces the Physics-page knobs (deposit, standoff, P/V/T,
  kinematics + accel/SCV, coil-wave, `s_fill_per_tau`, `pv_show_sag`, the three placement radii)
  and every `cal_vars` entry, since on a cylinder the placement reads the rig profile directly;
  the 3-axis list in `gui_preview._install_preview_traces` covers neither the coil-wave nor the
  calibration. **One preview panel serves both modes**: the page
  builds `MandrelPreviewPanel` always, and the collector rod is an additive actor
  toggled by `set_collector_visible`. **Its visibility has THREE conditions, not one**
  (`gui_preview._pv_rod_wanted`): the "Show the mandrel tube" checkbox, the collector toggle, and --
  since 2026-09-04 -- `_mandrel_rod_pending`, which withholds the rod from the moment the collector is
  switched to the mandrel until THAT switch's tube lands in `_mandrel_apply_result`. The rebuild is
  asynchronous and the shared panel keeps drawing the previous geometry meanwhile, so revealing the rod
  on the switch itself wrapped it around the plate scaffold for the length of the build (Joseph, on the
  running app). An ordinary field edit does not re-arm it -- the rod would blink out on every keystroke
  -- and an errored build does not clear it. Five cases in `test_gui_mandrel.py`, two RED-proven. `_MandrelMixin.run` swaps `_last_input_mode` alongside
  `_mandrel_dispatch`, because `_active_mode` echoes the remembered mode from a NON-INPUT page
  (Home is where the app boots) and the tube otherwise fell through to the g-code branch.
  Guards: `test_ui_shell.py` (mode/persist/argv/colour/section swap, plus the family row swap and
  the from-any-page Generate target — five of its cases are RED-proven, see their docstrings),
  `test_gui_mandrel.py` (real preview plus VTK actor-space collector-axis/span guard, the
  re-entrant preview, the zigzag argv + preview-g-code identity frozen against c20d6bb, and four
  physics-relay cases -- commanded-vs-laid, the rounded brick corner, the engaged
  `ContactPlacement`, and the trace list -- each carrying its own RED control), and
  `test_mandrel_sag.py` (approximate pass-through). The outcome-first redesign adds four cases to
  `test_gui_mandrel.py` (pore-mode argv vs the frozen angle-mode one, both cell fits + the two
  Use buttons, the helix table/chips/ladder, and an impossible pore as a message) and one to
  `test_ui_shell.py` (the legacy-preset migration); the inverse solver itself is
  `test_mandrel_inverse.py`. Capture presets:
  `.claude/visual/presets/mandrel_reentrant.json` and the physics twins
  `mandrel_brick_physics.json` / `mandrel_brick_commanded.json` (`tools/capture_ui.py --preset ...
  --pages scaffold --generate`). **The twins carry a `calibration_values` block**, because
  `sim.physics_deposit` belongs to the CALIBRATION preset kind and `capture_ui --preset` applies
  exactly one kind -- a scaffold-kind preset naming it changes nothing, which is how two
  pixel-identical captures were first produced. The engine is `engine/mew_unit_cell.py`.
- **`gui/gui_core.py`** `_CoreMixin` — the mixin no-op hooks (`:17-161`, from `_build_sim_tab` to
  `_preview_persist_entries`), `__init__` (`:163`), `_build_widgets` (`:228`, tab order: Machine and
  Collector `:252` → Calibration `:265` → Parameters→STL `:270` → viewer tabs `:280` → G-code→STL
  `:286` → Generate/Cancel strip `:304`), G-code→STL tab `_build_gcode_tab` (`:369`), `_active_mode`
  (`:500`, what Generate targets), `_on_tab_changed` (`:546`), `_refresh_result_controls` (`:562`).
  The String Art tab and its `_build_stringart_tab` / `_build_stringart_stub` glue (`:291`, `:438`,
  `:460`) are dead: they only run when `stringart_tab.py` exists, and it was deleted 2026-07-01 (§2.10).
- **`gui/gui_scaffold.py`** `_ScaffoldMixin` — `_build_scaffold_tab` (`:87`), scaffold-1 `s_*` vars
  `_init_scaffold_vars` (`:94`), the collector toggle `_build_collector_toggle` (`:248`) /
  `_apply_collector_mode` (`:337`), `_build_scaffold_container` (`:392`: `s_count` spinbox `:412`,
  `scaf_nb` per-scaffold notebook `:422`, `global_nb` Stab/Export/Presets `:461`),
  `_build_stabilization_tab` (`:486`), `_build_export_tab` (`:534`), `_build_quality_tab` (`:750`),
  the Save-g-code-as rewrite (thin wrappers since 2026-10-23: `_rewrite_gcode_lines` and
  friends alias `mew_gcode`, `_gcode_output_options` builds its `OutputOptions`, §2.22), `_new_scaffold_vars` (`:1175`),
  `_build_scaffold_core_fields` (`:1251`), **shape/footprint editor** `_build_scaffold_shape_fields`
  (`:1444`, custom-draw canvas `_build_shape_canvas` `:1698`), per-scaffold array
  `_build_scaffold_array_fields` (`:1380`), `_array_specs` (`:2063`, tiles active scaffold into a
  grid), `_sync_scaffold_count` (`:2193`, shows first N tabs), `_prefill_scaffold` (`:2206`, seeds +
  stacks Z), linking loops `_build_scaffold_link_loop_fields` (`:2100`), **angle-pattern editor**
  `_toggle_angle_pattern` (`:2316`), **linear-pattern editor + preview** `_build_linear_preview`
  (`:2549`) / `_redraw_linear_preview` (`:2812`).
- **`gui/gui_args.py`** `_ArgsMixin` — validators `_pos_float` (`:13`) … `_valid_hex` (`:45`), `_validate_gcode`
  (`:55`), `_validate_one_scaffold` (`:79`), `_validate_scaffold` (`:263`), **CLI builders**
  `_build_args_gcode` (`:430`) / `_build_args_scaffold` (`:455`, every print goes out as
  `--scaffolds-json` `:470`), `_expanded_specs_and_sources` (`:198`, arrays and deposition order),
  `_scaffold_spec` (`:614`), `_write_scaffolds_json` (`:702`), player fiber-params
  `_fiber_params_gcode` (`:715`) / `_fiber_params_scaffold` (`:811`).
- **`gui/gui_presets.py`** `_PresetsMixin` — two stores (scaffold + machine) over one generic `kind` path:
  `_init_pstore` (`:154`), preset tabs `_build_one_presets_tab` (`:232`), compare table
  `_build_preset_table` (`:307`), snapshot/apply `_snapshot_for_kind` (`:705`) /
  `_apply_preset_for_kind` (`:711`), save/load/rename/delete/import/export (`:757`-1131, from
  `_preset_load` to `_preset_import`), the legacy-preset migrations `_migrate_legacy_*` (`:1356`-1532),
  **the persist map `_persist_map` (`:1160`)** — namespaces `g.` `s.`/`s2.`-`s5.`
  `plate.`/`origin.` `machine.`/`slide.`/`bed.` `sa.`; `_apply_default_preset` (`:883`).
- **`gui/gui_saved_choices.py`** — every CLOSED-LIST setting a preset stores is saved by its KEY,
  never its menu label: the registry of each one's keys, labels and old labels. `to_saved` runs in
  `_preset_data_for_kind`, `from_saved` in `_migrate_saved_choices` (the last step of
  `_migrate_preset_data`); a value naming no choice is left at its default and named by
  `_apply_preset_for_kind` ("Loaded with a problem: …"). A new menu setting that a preset saves
  goes in its `_KEYS` / `_SCAFFOLD_KEYS`. The tube placement keeps its key in the var itself
  (`gui_mandrel.PLACEMENTS`, `placement_menu_var`).
  **The scaffold and machine kinds keep ONE DEFAULT PER COLLECTOR** (2026-09-04): a 3-axis machine
  cannot print a 4-axis design, so "Set as default" on those two opens `_ask_axis_mode` — a two-button
  "Set as default for:" chooser — and writes only that mode's pointer. `AXIS_MODES` is the authority
  for the modes and their key suffixes; the mode ids ARE `s_collector`'s values, and the 3-axis suffix
  is EMPTY so its key stays the legacy `default` / `machine_default` and an existing install boots off
  exactly the key it already wrote. `calibration` and `sweep` describe the rig, not the collector, so
  they keep one pointer and no dialogue. Boot always applies `BOOT_AXIS_MODE` (`"plate"`) — there is no
  remembered mode; the 4-axis pair loads from `_collector_mode_chosen`, wired to the Collector
  radiobuttons' `command` in `gui_scaffold._build_collector_toggle` and NOT to a trace on `s_collector`
  (applying a preset sets that var, so a trace re-enters the load it is inside). Lite has no mandrel
  collector, so `lite_web/presets.js` keeps one default per kind — an absent mode, not a parity gap.
  Guarded by **`test_axis_defaults.py`** (15 cases, 14 measured RED injections).
- **`gui/gui_run.py`** `_RunMixin` — `run` (`:27`, Popen + reader thread), `_assign_output_paths` (`:138`),
  `_read_output` (`:177`), `cancel` (`:187`), `_poll_output` (`:196`, the `after(120ms)` pump),
  `_on_gcode_ready` (`:254`), `_on_finished` (`:269`), Save-as `save_result_as`
  (`:480`)/`_export_as` (`:518`), `on_close` (`:546`, rmtree scratch). Marker `GCODE_READY_MARKER`
  (`:12`).
- **`gui/gui_viewers.py`** `_ViewerMixin` — `_build_viewer_tabs` (`:15`, Simulation/STL/G-code-text/Log),
  `_sync_result_buttons` (`:309`, the enable authority), `view_stl` (`:398`), `_build_exact_stl`
  (`:507`, off-thread `--from-depo` build), `_viewer_color_args` (`:614`), **`play_gcode` (`:646`,
  launches the player)**, `view_gcode` (`:776`). Render combos `sim_render`/`stl_render` (`:48`/`:81`).

- **`gui/gui_common.py`** — `from gui_common import *` substrate: paths `WORKER`/`SCAFFOLD_WORKER`/
  `VIEWER`/`PLAYER` (`:38`), `PRESETS_DIR`/`MACHINE_PRESETS_DIR` (`:91`/`:94`),
  `CALIBRATION_PRESETS_DIR` (`:103`) / `SWEEP_PRESETS_DIR` (`:107`) — **the constant, the kind id, the
  folder and the on-screen label are four different names and two of them do not match**: kind
  `"calibration"` is the PHYSICS profile in `presets/physics/`, labelled "Physics presets"; kind
  `"sweep"` is the Calibration page's print designs in `presets/calibration/`, labelled "Calibration
  presets" — one kind `"sweep:<tab>"` and one subfolder per Calibration tab (§2.15). The id → folder → label table at the top of `gui/gui_presets.py` is the authority — read it
  before touching either constant. `migrate_preset_folders` (`:150`, wrapped by
  `migrate_preset_folders_once` `:271`, which `gui_presets._init_pstore:154` calls) is the one-time move
  that made that swap safe on an existing install: it lifts pre-2026-09-03 physics profiles out of
  `presets/calibration/` into `presets/physics/`, classifying by CONTENT — a preset whose `data` holds
  any `cal.`/`sim.` key is a physics profile (`_is_physics_preset` `:135`), an all-`sw.` one is not.
  All-physics → the folder is renamed; mixed → only those files move; unreadable, ill-shaped or
  name-colliding files are left in place. **Nothing is ever deleted**, and it no-ops entirely once
  `presets/physics/` exists. Guarded by `test_presets_stores.py`.
  `SESSION_DIR` (`:300`),
  `prepare_session_dir` (`:332`, scratch lifecycle + dead-pid sweep), `_field` (`:440`),
  `_parse_length_mm` (`:447`, accepts inches), `enable_dpi_awareness` (`:473`), `class VScrollFrame`
  (`:496`), `RENDER_CHOICES`/`RENDER_LBL2VAL` (`:377`), `__all__` (`:568`).
- **`gui/gui_theme.py`** `_ThemeMixin` — themed **by widget class** so new tabs/widgets are
  covered free: `_apply_theme` (`:71`), `_style_clam` (`:121`, configures every ttk class),
  `_set_classic_options` (`:186`), `_recolor_classic` (`:212`, structural canvases by master-is-VScrollFrame),
  `_theme_preset_table` (`:225`). Pref file `gui_prefs.json`.
- **`gui/gui_sim.py`** `_SimMixin` — see **2.11**.

### 2.2 Scaffold generation
- **`engine/scaffold_gcode.py`** — `main` (`:5994`),
  `generate_multi_scaffold_gcode` (`:1688`), `generate_scaffold_gcode` (`:1843`), `_emit_one_scaffold`
  (`:1052`, the per-scaffold layer loop), `emit_scaffold_layers` (`:1530`), `_validate_scaffold_params`
  (`:1570`). **Footprint shapes:** `footprint_outline` (`:231`), `_polygon_is_convex` (`:269`),
  `_clip_segment_to_polygon` (`:318`), `_clip_segment_to_box` (`:175`, fast rect path), `_SHAPES`
  (`:213`). **Passes/serpentine:** `_layer_passes` (`:440`), `_orient_passes` (`:518`), `_flatten_passes`
  (`:640`). **Turnarounds:** `_turn_circle` (`:763`), `_turn_points` (`:819`). **Linking loops:**
  `_link_loop_circle` (`:849`), `_link_loop_points` (`:930`), `_LINK_LOOP_MAX_REACH` (`:846`, the
  reach cap — a centre distance, never a radius, and never a branch), `_lead_in_chord` (`:1008`) +
  `_LEAD_IN_MIN_STRAIGHT` (`:964`), `_LOOP_GRAZE_SIN` (`:62`). **Patterns:**
  `parse_angle_pattern` (`:101`), `parse_linear_pattern` (`:144`), `_wave_pass` (`:560`). **Misc:**
  `translate_gcode` (`:72`), `estimate_print_time` (`:5871`), `_scaffold_from_spec` (`:1906`).
  - **A hopped layer change gets TWO loops**, because in continuous-flow MEW both ends of the hop are
    corners on laid fibre: the DEPARTURE (old layer → hop, at `prev_exit`, emitted before the `G0` so
    it lies at the old layer's Z) and the ARRIVAL (hop → new layer, at the entry). Both are drawn by
    the one nested emitter `_emit_link_loop` (`:1186`), so feed, arc form and bbox growth cannot
    drift apart. The departure is drawn exactly when the hop is at least `link_loop_dist` long —
    the same condition that makes `_lead_in_chord` return the hop's own heading — so a shared corner
    (a square at rotation 90, gap 0) never grows a second loop on top of the arrival's.
  - **The loop's lead-in heading** is `_lead_in_chord`: the chord of the last `link_loop_dist` mm of
    the path the head actually travelled, so it is the previous layer's exit heading when the two
    free ends coincide and the layer-change TRAVEL's own heading once the hop is longer than the
    look-back — one formula, no threshold between two shapes. `link_loop_travel` (default on) is the
    opt-out and restores the pre-2026-08-06 coincidence gate byte-identically. Two things that look
    like oversights and are not: the around-the-part router (`:1331`, the reason at `:1376`) gets **no** loop because it
    arrives pointing INWARD (85 of 86 routed arrivals admit no outboard circle), and the cue has a
    forced singularity where a hop retraces the fibre just laid — `_LEAD_IN_MIN_STRAIGHT` gives it
    width so two opposite lead-ins are never adjacent. Guards: `test_linking_loop_leg1.py` 6a–6f.
- **Mandrel mode — tubes on a rotating collector**. A **SIBLING
  emitter in the same file**, not a mode inside `_emit_one_scaffold`: `generate_mandrel_gcode`
  (`:2708`), `mandrel_design` (`:2365`, a thin front for `mew_tube.solve_tube`), `_zigzag_layer`
  (`:2323`, McColl Eq 6.1), `MANDREL_STRATEGIES` (`:2215`) / `MANDREL_SPEED_MODES` (`:2223`),
  and the `--mandrel` CLI branch which **returns before any planar flag is read**.
  **Both tube emitters are one-block calls of `generate_mandrel_blocks_gcode` (`:2551`)**: a list
  of `TubeBlock`s (`:2440`; design, layers, optional step and speed) laid outward, each block's
  sheet read through `_TubeSheet` (`:2466`); CLI `--tube-blocks` takes a JSON file or the JSON
  itself (`_cli_tube_blocks`, `:6768`; entries resolved by `tube_blocks_from_entries`, `:6703`, at
  the base flags' tube length). Per-layer settings group into blocks in `tube_layer_blocks`
  (`:6666`), which the GUI's layer table feeds. **Copies side by side** (`copies` /
  `--tube-copies`, `copy_gap` / `--tube-copy-gap`, pitch `tube_copy_pitch` `:2786`): each copy is a
  scaffold group; the first is laid from its FAR end so it finishes facing the gap, and from the
  third on each join crosses the copy before (`tube_copy_crossings`, flagged as a CLI NOTE, not
  refused); an exit longer than the gap is refused. **A layer is ONE WHOLE SHEET** (2026-10-28): a whole winding circuit or cell
  traversal, stepped by `--tube-layer-step` (default the fibre diameter). From 2026-09-03 to
  then a layer was a ply, half a sheet. Blocks join with NO turn (the next starts at the A the
  last stopped at), share one length (another is refused), and a custom cell starting inside the
  tube is restarted on the near end. Guard: `test_mandrel_blocks.py`. Emits `X` + the
  rotary `A` (degrees, unwrapped) and `Z` measured OUTWARD FROM THE BARE MANDREL SURFACE, which is
  the datum `mew_frame.CylinderFrame(radius)` assumes with no argument. Four things not to undo:
  **one `G1` per length iteration is EXACT** (a constant-angle helix is a straight line in `(u, v)`,
  so X–A linear interpolation traces it — do not "densify" it into chords); **the layer change is a
  `G0 Z`**, because the converter's per-scaffold grouping asserts one path per layer in g-code order
  and a path only breaks on a travel — fold `Z` into the layer's first `G1` instead and the whole
  tube becomes one path AND that pass ramps, landing progressively higher along its length;
  **layers repeat identically** (McColl p.4, "All proceeding layers follow the same pattern" — there
  is no per-layer offset to invent, the closing length IS the registration); and **`--stl` chains
  through the cylindrical converter**, including its frame-aware `--sag-approx` path. Strategy 3.2, the
  infinity loop that keeps `A` in range, is NOT built — `A` grows without bound, McColl's own caveat
  about 3.1. Guard: `test_mandrel_gcode.py`, whose comparator is a round trip through
  `parse_gcode(frame=CylinderFrame(...))` plus its own RED control.
- **Split rotary feeds (Aerotech F + E)** — both mandrel emitters take `rotary_feed="split"`
  (`--rotary-feed split`, the designer's "Rotary feed" combo, persisted as `m.rotary_feed`) and
  `time_base` (`--feed-time-base min|s`, `m.feed_time_base`, written as a `G75`/`G76` header).
  `_SplitFeed` gives every move F = S·|du|/L (linear) and E = S·|dA|/L (rotary, deg) from the one
  surface speed S, so each move lasts L/S at radius R0 + (Z − surface_z). "combined" is the
  default and byte-identical. The readers follow: `file_uses_extrusion` no longer reads E as
  extrusion in a file with a rotary word, `gcode_to_fibers.rotary_split_seconds` is Aerotech's
  timing rule (F over the linear axes when any moves, else E), shared by `parse_gcode` and
  `gcode_player.parse_path` (whose `seg_feeds` stay the SURFACE speed), and `print_path_stats`
  and `mew_gcode.problems` know the dialect. Guard: `test_mandrel_split_feed.py`.
  **Since `613f674d` split is not a designer choice:** it follows the Machine page's flavor
  (`mew_gcode.Flavor.rotary_feed`, `gui_mandrel._mandrel_rotary_feed`) and the time unit is the
  Machine page's "Feed time unit" (`origin.feed_time_base`). The mandrel card's "Feeds" is auto
  (the rates above) or fixed (`m.fixed_f`/`m.fixed_e` -> `--fixed-feed F E`, written as typed).
  `G75`/`G76` stay in the scratch/parse form for the readers and are never exported.
- **Asking for a PORE instead of a winding angle** — `mew_tube.solve_tube_for_pore` is the inverse of
  `solve_tube`: given a radius, a tube length and a target pore AREA it searches ω × η (Rm bracketed
  exactly as `solve_tube` does, never scanned), ranks on the pore SIDE `sqrt(Eq 5.3)` quantised into
  1 % bands so half a percent of pore cannot buy 4 % of length, floors candidates at Eq 5.1's LA and
  2 × Eq 5.2's HT against the fibre, and returns `(TubeDesign, [alternatives])` or REFUSES naming the
  numbers it computed. It changes no generator surface — the page resolves ω/η/Rm and sends today's
  flags. ~19 ms for the default 121 ω × 120 η, inside the preview's 350 ms debounce. Guard:
  `test_mandrel_inverse.py`, whose comparator is an independent enumeration over `TubeDesign` objects.
  Its winding-angle sibling `solve_tube_options` (ω fixed) is `solve_tube`'s pick plus the nearest
  (η, Rm) per other pivot count, fibre-floored and thinned evenly; guard
  `test_gui_mandrel.angle_mode_resolves_the_closure_unless_overridden`.
  `nearest_closing_lengths` (the bracketing pair when `solve_tube` refuses) is guarded by
  `test_mew_tube.test_the_nearest_closing_lengths_bracket_the_request`, brute force over
  `valid_lengths`.
- **Unit-cell tube families** — `generate_mandrel_cell_gcode` (`:2170`) is the SECOND mandrel
  emitter, a sibling of the zigzag one rather than a mode inside it, driven by
  `mew_unit_cell` (below). Selected by `--tube-family`, which **defaults to `zigzag` and
  dispatches to the untouched McColl emitter**, so an argv that does not name it is
  byte-identical (`test_mandrel_unit_cell.py` asserts that against
  `generate_mandrel_gcode`'s own output). The cell flags are `--cell-width` / `--cell-height`
  (the requested cell on the unwrapped plane, McCosker's longitudinal and radial spacing),
  `--cell-fit`, `--rib-angle`, `--cell-amplitude`, plus `--cell-n-around` / `--cell-n-along`
  (2026-09-02) — the designer page's count-ladder pin, which REPLACES the rounding
  `design_tube_cells` does on `circumference / cell_h` and `target_length / cell_w`. Both
  default to `None` and the page sends them only when a ladder row is pinned, so an argv that
  names neither is byte-identical to the pre-ladder one. A cell family ignores
  `--winding-angle` / `--tube-eta` / `--tube-rm` entirely, because there is no helix to close. Both branches share
  the file write, the `[GCODE-READY]` line and the `--stl` chain. **`layers` counts PLIES here
  exactly as it does in the zigzag emitter and on the 3-axis page** — any whole number from 1,
  and the tube is one radius PER PLY, a fibre diameter apart, so 10 layers is 10 diameters deep
  at the junctions exactly as 10 flat layers are (2026-09-03; it was EVEN-only and
  `layers // 2` traversals while the step was per traversal, which bought half the wall).
  longitudinals stack where they cross — the same pairing a zigzag circuit's two helix families
  make. **Until 2026-09-01 this emitter ran one traversal PER LAYER**, so every non-diamond
  family built a wall twice as tall as the zigzag did for the same field value.
  Two caveats, stated rather than hidden. A re-entrant honeycomb has no crossings, so its
  traversal is physically ONE ply thick except along the retraced struts — the count is a
  convention shared with the 3-axis page, not a wall-thickness measurement. And retraced
  members, laid twice within a single traversal, stand two diameters proud of their
  neighbours; that is the geometry McCosker's doubled struts have, not an emitter artefact.
- **`engine/mew_unit_cell.py`** — **literature cell families for 4-axis tubes**; stdlib only, no
  numpy/VTK/Tk and **no g-code strings** (it returns geometry; `scaffold_gcode` serialises it).
  `MANDREL_FAMILIES` = `reentrant` / `brick-closed` / `brick-open` / `chevron` / `custom`;
  the legacy McColl diamond is NOT here, it stays `MANDREL_STRATEGIES`.
  **`custom` is the one family whose geometry is DRAWN, not named** (2026-09-04): it
  arrives as a `parse_cell_graph` string — nodes `u,v` in the UNIT SQUARE plus 1-based
  edge pairs `i-j`, a `:free` suffix marking a member the route may retrace cheaply —
  and `_custom_members` tiles it through the same `_route` every other family uses, so a
  drawn cell is a fifth producer of `_Member`s, not a second engine. **The cell is a
  TORUS TILE**: coordinates are absolute in u and modulo the circumference in v, so a
  node on u = 1 welds to the next cell and one on v = 1 to the next ring by plain
  coordinate identity, and authoring in the unit square needs no wrap syntax. The price
  is `_custom_duplicate` — the same member drawn on BOTH boundaries is one member
  authored twice, which the tiling would otherwise lay twice on every cell. `format_cell_graph`
  is the inverse the editor writes back through, and `preview_route` routes a THREE-CELL
  tile at the real `n_around` for live validation: measured, the full tube is superlinear
  (599 ms at 320 cells, 2.1 s at 600) while three cells is 1.7 ms and reproduces the
  retraced fraction exactly and the under-CTS score to 0.2 points — `reversals` is the one
  figure that does NOT transfer and callers report it per cell. `design_tube_cells`
  (`:1293`) resolves a requested cell onto a circumference and routes one layer;
  `TubeCellDesign` (`:125`) carries the resolved size, what was `requested`, and the route
  diagnostics. Read the route with `tube_cell_route` (`:164`) and the pattern with
  `tube_cell_members` / `tube_cell_retraced` (`:176`/`:187`) — the latter two are what a
  preview draws. Every cell definition cites the FIGURE it came from, in the module docstring;
  the figures were rasterised and read, not inferred from the PDF text layer. Four things not
  to undo. **The router retraces, it never restarts** (`_eulerize` `:1065`, `_hierholzer`
  `:1118`): a MEW jet does not stop, so a `G0` inside a layer is a dragged fibre, and doubling
  members is what McCosker actually did. **The matching cost is `length × a per-role penalty`,
  not length** — on a square re-entrant cell the strut is LONGER than the rib (h = 0.789 W
  against l = 0.577 W), so a plain minimum-length Chinese postman doubles the load-bearing
  ribs of an auxetic scaffold; the penalty is what reproduces McCosker's doubled struts.
  **Node identity is seam-aware** (`_node_key`), v modulo the circumference, or the ring that
  crosses v = 0 comes back disconnected. **`amplitude = 0` short-circuits to the straight
  two-point ring**, so the wavy and straight families are byte-identical there rather than
  merely close. Two known limits, both documented in the docstring: `FIT_MODES` (`:65`) has
  no "no change" (Devlin's third option cannot close a circumference), and `design_tube_cells`
  reproduces Devlin Fig 1A's SCALED numbers exactly but not its FITTED ones (`round` vs a
  `ceil` that would fit both; their Supporting Information C is not held). Guards:
  `test_mew_unit_cell.py` (geometry, with a RED continuity control),
  `test_mandrel_unit_cell.py` (the g-code round trip, with a RED control of its own) and,
  for the drawn family, **`test_custom_cell.py`** — whose comparator is a DIFFERENT tiler:
  a drawn brick must reproduce `_brick_members`' interior member for member, differing by
  exactly `n_around` members and exactly `period_v` (the one end ring a torus tile cannot
  have). It carries three RED controls: a node 1e-6 off the v = 1 boundary and one off
  u = 1 must each disconnect the tiling. A single nudge on the HONEYCOMB does not — it has
  two ring welds — and that non-control is asserted as its own fact, because it was
  written as the control first and proved nothing.
  **A CROSSING IS NOT A JUNCTION, and the page repairs that rather than lecturing about
  it** (2026-09-05). The tiler reads a GRAPH: two members whose lines cross on screen
  share a point, not a node, and the Euler walk turns only at nodes — so a chevron laid
  over a longitudinal tiles into two interleaved patterns that never touch and is
  refused, while looking entirely printable. `cell_components` (`:711`) reports the
  pieces without tiling anything (the edges plus `_weld_pairs`' boundary twins);
  `cell_connect_fault` (`:746`) names WHY — `"axial"` / `"ring"` for a boundary node
  with no twin on the opposite edge, `"pieces"` for a drawing in parts — where
  `_hierholzer` can only report the SIZE of the piece its walk landed in
  ("covered 18 of 108 traversals"), having no idea what it missed. `splice_cell_crossings`
  (`:885`) is the repair, behind the page's **Join crossings** button: it puts a node
  where two pieces cross, splitting both members through it. **The geometry never moves** —
  every spliced edge is a sub-segment of one that was already drawn, so the picture is
  identical and only the topology changes. It is MINIMAL in two ways that are both
  load-bearing: one crossing is spliced at a time with the pieces recounted after each,
  so a second crossing between two already-joined pieces is left alone; and a crossing
  INSIDE one piece is never touched, because a self-crossing is the designer's and a node
  there would add a turn nobody asked for. The best case adds ZERO nodes — where an apex
  already sits on the member it must join, the repair is to route through the node that
  is already there. Two things it cannot do, and says so instead: a missing boundary twin
  is a node only the user can place, and two patterns that never cross have no junction
  to add. **`cell_connect_fault` is a diagnosis, never a second gate** — the page always
  shows `design_tube_cells`' verdict and uses this only to explain one, and it has one
  documented blind spot, the winding-parity case (two diagonals stepping only (±1, ±1)
  pass every test here and still split the tiling into odd and even halves; the engine
  refuses, this returns None). `test_custom_cell.py` carries both halves with RED controls:
  removing the connected-drawing guard makes the splice touch a self-crossing cell, and a
  0.2 % drift in the splice point breaks eight checks including the two GUI ones.
- **`engine/array_travel.py`** — routes inter-scaffold G0 hops *around* laid scaffolds via a
  visibility graph + Dijkstra over exclusion-zone corners. `plan_array_travel` (`:116`),
  `plan_exit_travel` (`:157`), `_route` (`:174`), `_visible` (`:87`). Returns `[]`/fallback when boxed in.
- **`engine/perimeter_travel.py`** — PERIMETER TRAVEL (scaffold Parameters, "Travel"): every automatic
  flat-plate G0 (layer change, between scaffolds, lead-in, exit) runs along a lane = the convex
  hull of what a scaffold has laid, offset outward by `offset + fibre/2`, never closer.
  Pure geometry: `lane_polygon`, `boundary_walk` (single lane, shorter way round), `route_between`
  (visibility graph + Dijkstra over lanes), `plan`. Wired in `scaffold_gcode._emit_one_scaffold`
  (`_perimeter_hop`), `generate_multi_scaffold_gcode` (`laid_lanes`, exit), flags
  `--perimeter-travel[-offset]`. Off = byte-identical. On a tube the same switch is
  `--tube-turn-out` / `TubeBlock.turn_out`: an end-turn helix runs out past the end, turns, comes back
  (`_TubeSheet.moves`, `tube_overhang`).
- **`gui/gui_cell_hover.py`** — the unit-cell hover picture on the 4-axis Pattern selector (ribbon menu,
  Parameters-table combobox, designer combobox): one cell blue, neighbours grey, cut from the
  `mew_unit_cell` tilers. `segments` is pure; `attach_menu` / `attach_combobox` wire it.
- **`engine/gcode_exit.py`** — picks the end-of-print parking point (run nozzle off the part so
  trailing polymer misses it). `plate_exit_point` (`:109`, the one the generator uses), `outward_edge_exit`
  (`:72`), `nearest_edge_exit` (`:46`). `DEFAULT_MARGIN=5.0`.

### 2.3 G-code parsing
In `engine/gcode_to_fibers.py`: `parse_gcode` (`:552`, modes auto/e/g1, modal state, MEWron continuous-flow),
`file_uses_extrusion` (`:364`), `arc_xy_points` (`:288`, G2/G3 discretisation), `NO_SELF_CROSS_MARKER`
(`:384`), `DEFAULT_FEED=600.0` (`:38`).
- **`merge_travels(paths, ptimes, travels)`** — splices each travel onto the FRONT of the fibre it
  leads into, dropping the shared junction vertex. Fibre count/order preserved (never a new path:
  the grouping asserts `sum(layers) == len(paths)`). Used by `--render-travel` for the plain
  (physics-off) converter, and by `mew_deposition.make_transform`'s diameter lookup. Layer clustering
  is safe — `_layer_ids` keys off each fibre's MEDIAN Z, which a short travel cannot move.
  ⚠ A **pure-Z-hop travel** (a serpentine whose layer ends where the next begins — no XY motion)
  correctly renders as nothing: after sag both endpoints land at one Z and `_dedupe` (`:1173`, called
  by the sag and deposition paths, e.g. `:4458`) collapses it. Verified: such a scaffold's triangle count is unchanged,
  while a real XY travel (the lag-sweep plates) adds ~23% more triangles. Don't "fix" that.
- **`--render-travel`** (converter CLI, default off) — parse travels and deposit them; see §2.11 and
  the "I want to change X" table for the whole GUI→worker chain.
- **`with_travels=True`** (opt-in, default off → byte-identical) additionally reports the travel (G0)
  geometry the parser otherwise discards, as a third list `travels` **aligned to `paths`**:
  `travels[i]` = the G0 chain leading INTO fibre `i` as `(xyz, times)`, or None. `paths`/`ptimes` are
  untouched — travels ride ALONGSIDE, never interleaved, because callers index fibres positionally
  (the paper's `analyze.py:59` does `dep[s['idx']]`) and an extra entry would shift every index.
  Consecutive G0s coalesce into ONE chain sharing its endpoints EXACTLY with the fibres either side,
  so fibre+travel+fibre is one continuous polyline in space and time. Helpers `take_travel` /
  `travel_step` / `pending_tv` around `flush` (`:~206`). Consumed by `mew_deposition`'s
  `simulate_travel` (§2.11). The TRAILING travel (after the last fibre: the end-of-print exit) is one extra entry, `travels[len(paths)]` (`trailing_travel`), spliced onto the END of the last fibre by `merge_travels` and laid by the relay; a pure Z lift at its end is trimmed.
- G0 **speed**: there is no rapid rate — `advance` (`:~300`) uses the one sticky modal `feed` for
  travels and deposition alike. This is CORRECT for the rig, not an approximation: stock Klipper
  registers `G0` as a literal alias of `cmd_G1` (verified in `klippy/extras/gcode_move.py`) and the
  rig's `printer.cfg` (LDO Voron 0.2, verified 2026-07-16) adds no G0 override. Caveat: the F word is
  only parsed when `with_times` (`:5515`), so `feed` never updates without it.

- **Corner resolution — `fill_per_tau`** `(GUI field, 2026-08-31)`. The physics deposition's refine
  grid: output samples per relaxation time inside a corner/start transient. It exists because the grid
  is uniform in **time** while the ray tracker's contact runs FASTER than the nozzle through a turn
  (`integrate_contact_path_ray`, V_JCP exceeds the steady speed there), so the same time step buys less
  arc exactly at the **turnaround crest** — the tightest bend on the path — and at the shipped 6 that
  crest is drawn as a few long chords and reads faceted. Measured on 10x10 / stepover 1 / 500 mm-min,
  H 3.5 mm, arc radius 0: worst laid turn **38.4 / 22.6 / 11.5 deg at 6 / 12 / 24**, with the point
  count and the deposition time doubling each step. Default **6.0 everywhere**, so an untouched export
  is byte-identical (hash-checked). GUI: `s_fill_per_tau` in the **Export** panel under Mesh detail
  (`gui/gui_scaffold.py` `_build_export_tab`), greyed with `sim_physics_deposit` via `_apply_diam_mode`
  (`gui/gui_sim.py`), validated + capped at 96 by `_validate_fill_per_tau` (`gui/gui_args.py` — the grid is
  built BEFORE the point cap thins it, so a mistyped 6000 is a freeze, not a slow render), emitted by
  `_fill_per_tau_args` into `_sim_play_args`, persisted as `s.fill_per_tau` (`gui/gui_presets.py`, scaffold
  kind by subtraction). **One field, four consumers, on purpose**: `mew_export --fill-per-tau` bakes the
  `.depo`, `gcode_player --fill-per-tau` hands it to `mew_incremental` for the live re-lay, and
  `gui_preview` reads it into the same `make_transform` — a value reaching only some of them would leave
  an edited layer sampled unlike the untouched ones beside it, which is what `test_mew_incremental`'s
  bit-identity guard forbids. It must also be in the preview's `_pv_all_dirty` trace list, or the
  preview adopts it only on the next unrelated edit. Guards: `test_mew_deposition`
  (`test_corner_resolution_*`) and `test_ui_shell` (argv, preset round-trip, preview-dirty), all
  red-proved.

- **Curve quality — the adaptive corner grid** `(2026-09-02)`. `fill_per_tau` above spends its samples
  uniformly in **time** inside each corner window, clamped to the next corner. On a tube the corners sit
  closer together than the lag, so every window is clamped and the fill degenerates into ONE uniform time
  grid — measured on the GUI-default re-entrant tube at `fill_per_tau` 6: **186 samples per second
  everywhere, correlation with interval duration 1.000 and with the turn angle 0.011.** A 120° junction was
  sampled exactly like a straight run, so the drawn chevron cut **31.7 µm (1.6 fibre diameters)** off the
  curve while its marched **vertices** sat 1.71 µm from it — a sampling defect, not an integration one,
  which is what makes refining safe. It is not mandrel-specific, only diluted on a plate.
  **The rule.** `mew_deposition._refine_corner_grid` marches, measures each vertex with `decimate_keep`'s
  own error model (`_span_max_dist` + `radius * theta`, imported not restated), predicts the split from
  the h² sagitta law, and re-marches. Three things keep it cheap and honest: the tracker's sub-step count
  follows `dt/tau` instead of being 16 per OUTPUT sample (`mew_physics.RAY_STEPS_PER_TAU` — its own
  calibration always meant 96 steps per relaxation time, and the fixed form multiplied an integration that
  had already converged); a **progress test** (`_REFINE_MIN_PROGRESS`) stops the loop when a pass does not
  cut the worst deviation, because near CTS the lag collapses and the laid path keeps the commanded path's
  genuine corners — a corner reads as a large deviation at every sampling and refining it converges only
  linearly (without the test the app's own boot state took **63.9 s against 6.2 s**, and 3.7 s with it);
  and the ceiling is per unit path LENGTH (`_REFINE_POINTS_PER_D`), never the corner count, that metric
  being what failed here.
  **REJECTED, do not rebuild** (recorded at the constants): allocating the samples from a closed form
  `n = C*sqrt(theta*L/tol)` instead of measuring — worse on every axis, and worse again as a seed, because
  pre-densifying shrinks the sagitta the trigger reads and the loop then stops early.
  **The field.** ONE user dial, `s_curve_quality` (Export panel, above Corner resolution), in the unit the
  rest of the pipeline already uses — fibre diameters of allowed error — with the integration budget that
  makes each tolerance mean something, since below ~0.1 d the tracker's own first-order error dominates.
  `mew_deposition.CURVE_QUALITY` is the ONE tier table; `auto_corner_grid(d, tier)` the ONE reader.
  Measured on the five 4-axis families at 10 plies against `fill_per_tau` 96:

  | tier | chord error | relay time | points |
  |---|---|---|---|
  | (none, what this replaces) | 0.22–1.58 d | ×1 | — |
  | draft — 0.25 Ø | 0.21–0.45 d | ×1.1–1.9 | +0–12 % |
  | **standard — 0.1 Ø (default)** | 0.11–0.16 d | ×2.0–3.0 | +0–37 % |
  | fine — 0.03 Ø | 0.05–0.11 d | ×2.7–4.2 | +24–141 % |

  **The rotary gate lives in the callers, not the engine**: `gui_mandrel_preview.build_mandrel_preview`
  passes it unconditionally (it is only ever called with a cylinder), `gui_mandrel._build_args_mandrel`
  emits `--auto-corner-grid D --curve-quality TIER` via `gui_args._auto_corner_grid_args` (physics-gated,
  and deliberately NOT in the shared `_sim_play_args` — `gcode_player` eats those too and the live march
  has no rotary frame), and `mew_export` turns the pair into the triple. Flat is byte-identical **by
  construction** (`corner_chord=None`), which is what keeps `test_mandrel_placement`'s `b24df78` digest and
  every hash-checked flat export green. Persisted as `s.curve_quality`; the var holds the ENGINE TOKEN and
  the combobox a label, re-synced by `_sync_curve_quality_label` after a preset load. Guards:
  `test_mandrel_placement` (`test_the_auto_corner_grid_*`, each printing its own RED control in the same
  run, including the near-CTS grind), `test_mew_deposition` (the refiner flags a starved arc and passes a
  dense one; off is byte-identical; the ceiling holds), `test_gui_mandrel` (the argv, physics-gated),
  `test_ui_shell` (persist parity).
  **Plate opt-in — "Auto corner resolution"** `(2026-10-20)`, `s_auto_corner_grid` (Quality card, under
  Corner resolution; persisted as `s.auto_corner_grid`, default ON). Ticked, a FLAT scaffold run gets the
  same grid: `gui_args._flat_auto_corner_args` (sized on the finest scaffold diameter) rides BOTH
  `_build_args_scaffold` (export) and `_fiber_params_scaffold` (player), and `_pv_snapshot_options` hands
  the preview `physics["corner_chord"]` at the DRAFT tier whatever Curve quality says (zig-zag: 2.4 s vs
  4.7 s at Standard; slight crest corners, 4.2 kinks/mm vs 0.035, visible only in close-up). The preview drapes
  the SEED grid and carries Z onto the drawn fibre, the export's sag/drawn split (`build_preview_paths`);
  draping the refined grid took a 1 mm zig-zag linear pattern from 2.7 s to 34 s, nearly all of it the
  drape's self-crossing search. `mew_incremental` no longer gates the grid on a rotary frame; the
  caller must send it to export and player together. The player's mesh rebuild now passes it too.
  Measured on a 0.3 mm sine linear pattern: crest chord error 1.06 d at `fill_per_tau` 6, 0.20 d with this
  on. Guard: `test_ui_shell::auto_corner_resolution_reaches_all_three`. Unmeasured: a flat live-adjust
  re-lay sags the refined fibre directly while the batch `.depo` used the sag/drawn split.

  **The sag / drawn-curve split** `(2026-09-03)`. The grid exists to smooth what is DRAWN, and the sag
  solver resamples the centreline on its own contact-sweep step either way — so it is handed the SEED grid
  and its Z is carried back onto the refined fibre. `mew_deposition._lay_one` keeps the pre-refinement grid
  (refining is add-only, so it stays a strict subset), `laid_fibers` reports it as `info["sag_times"]`, and
  `make_transform` publishes it as the transform's own `sag_grid` attribute — an ATTRIBUTE and not a fourth
  tuple element because six callers unpack that seam positionally (`gui_preview`, `gui_mandrel_preview`,
  `gui_sweeps`, `sag_bench`, `harness_deviation`, the diagnostics). `gcode_to_fibers.split_for_sag` selects
  the subset (a selection, never an interpolation — it falls back to the whole fibre if the grid is not a
  subset) and `carry_sag_onto_drawn` merges the two back: the union of their time grids, **XY from the
  fibre that was drawn, Z from the fibre that was sagged**. Used by `main`'s sag branch and by
  `build_mandrel_preview`; `sag_grid` is None with the corner grid off, so every flat run is the single
  call it always was. Measured on a 10-ply re-entrant tube at Standard: the solver sees **43 088 → 31 433**
  points, the drawn chord is **unchanged at 0.109 d**, and the output is 168 038 → 162 734. In the preview,
  17 094 → 12 458 into the drape for the same drawn fibre.
  **The union is the rule, not caution**: keeping the SAG solver's own sampling and only re-assigning its
  XY — the obvious cheaper merge — leaves the drawn chord at **0.395 d against the union's 0.109**, barely
  better than not refining at all, because that sampling is span-driven and puts no sample at a corner
  vertex. The Z that results is the seed grid's, and that is a smaller move than the tier choice already
  makes: 13.5 µm at worst against the 13.2 µm that switching Standard → Draft moves it by anyway (control:
  carrying a solve onto itself reads 0.0000 µm). Guard, with the rejected merge as its RED control:
  `test_mandrel_placement.test_the_sag_solve_runs_on_the_seed_grid_and_the_drawn_fibre_does_not_move`.

  **Not yet folded into it**, and the next candidates: `--arc-segments`
  (a count per half-turn), `mew_frame.densify`'s fixed 2° wrap step and `gcode_player`'s
  `_SMOOTH_ARC_STEP_DEG` (both scale-blind — 0.23 µm of chord on a 3 mm mandrel, 3.8 µm on a 25 mm one),
  and `mew_unit_cell.WAVE_CHORDS_PER_PERIOD`.

### 2.4 Sag solver

> **The "Sag multiplier" box is GONE (2026-09-15).** It scaled `sag_coeff` — a calibrated per-rig
> coefficient printed as `c` on the Physics page's Fiber sagging card — from the Scaffold designer's
> Quality card, so the number shown beside the law was not the number the export used. The *variable*
> survives, pinned at `1.0`: `s_sag_mult` / `sag_mult` are still in the persist map, still in every
> saved preset, and `--sag-multiplier` is still a worker/player argument, so nothing on disk or on the
> wire changed. What went is the two entries (`gui_scaffold._build_quality_tab`, `gui_core`'s G-code
> panel) and `mult` as a name a user law may use — `mew_lawlang.LAWS["sag"]` no longer declares it and
> `_law_scope` no longer passes it, so an OVERRIDDEN sag law ignores `--sag-multiplier` while the
> shipped path still honours it. Scale the droop by editing `c`. Guard:
> `test_ui_shell.sag_solver_greys_the_sag_knobs`.

In `engine/gcode_to_fibers.py`: `sag_depth` (`:1825`, parabolic droop law), constants `SAG_COEFF=0.01` (`:1699`),
`SAG_SPAN_EXP`/`SAG_DIAM_EXP` (`:1709`/`:1710`), `SAG_STEP_D`/`CREST_RADIUS_D` (`:1700`/`:1701`).
**`class _SagState` (`:5465`)** — the persistent per-layer stepper shared by batch + live march:
`_solve_layer` (`:5693`, sags a layer without committing), `_commit_layer` (`:5601`), `add_layer`
(`:6334`), `commit_solved` (`:6370`), `reuse_layer` (`:5675`). `apply_sag` (`:6412`, batch driver over
`_SagState`), `loop_entry_flags` (`:6381`), `_sag_applicable` (`:4459`). `_SAG_LAW` seam (`set_sag_law`
`:1850` / `_sag_law` `:1859`).
- **Periodic sag reset** `(GUI knob, OPT-IN — off by default, 20 prefilled):` every Nth layer is laid STRAIGHT and
  becomes the datum the layers above sag onto, so droop re-accumulates from flat instead of sitting at
  its saturated depth — the observed "the fibres go straight again every so many layers" behaviour. A
  phenomenological PERIOD, not a mechanism (same class of knob as `sag_multiplier`; nothing claims a
  cause). `_SagState.maybe_reset` (`engine/gcode_to_fibers.py:5633`) clears `all_lower`/`prev_layer` and
  re-datums `bed_center` onto the top SURFACE of the structure below (its highest centreline + the
  radius it supports at; `bed_at` adds this layer's own radius on top, as it does on layer 0) — geometrically
  "make this layer layer 0 again", so the flat layer falls out of the empty indices with NO special-cased
  droop. Called from `add_layer` AND `reuse_layer` (a replayed/warm-started layer must rebuild the same
  state a cold solve had) and from `mew_incremental._sag_layer` on BOTH its paths (solve + locked-cache),
  so the live march resets on the same layers as the export. Constant `_DEFAULT_SAG_RESET_LAYERS = 0`
  (`:1725`). **One shared default, 0 = never, on EVERY surface** — the `apply_sag` keyword,
  `gcode_to_fibers --sag-reset-layers`, `scaffold_gcode --sag-reset-layers` (forwarded into `gtf_argv`;
  a literal 0 there, since it imports the converter only inside `main()`), `gcode_player
  --sag-reset-layers`, and the GUI tickbox. So the feature is strictly opt-in and an un-asked-for run is
  byte-for-byte the old solve. The shared default is **load-bearing, not tidiness**: the period is part
  of the `.depo` key, so a surface defaulting differently would reject a default-built cache and the
  player would silently fall back to the mew_live re-derive (= live-vs-locked drift). The GUI prefills 20
  as the typical period for when it IS ticked. Cache key: `_depo_cache_meta` (`:7297`) carries it, so a
  cache at another period — or written before the feature — is rejected (shape test). GUI: `s_sag_reset`
  (tickbox) + `s_sag_reset_layers` in the **Quality** card (where the Sag multiplier used to be)
  (`gui/gui_scaffold.py:834`, in `_build_quality_tab`; greying via `_toggle_scaffold_sag` `:2929`), emitted by `_sag_reset_arg`
  (`gui/gui_args.py:416`, always explicit so the worker's 20-default can't re-enable an unticked reset),
  persisted as `s.sag_reset`/`s.sag_reset_layers` (`gui/gui_presets.py:1225`, scaffold kind by subtraction).
  It lives on Export, NOT on a scaffold's Geometry tab, because the solve is global — layers are
  Z-clusters across every stacked scaffold, so "every N layers" cannot be resolved per-scaffold. The
  preview drape mirrors it (`preview_sag.drape_sag` clears its committed structure and re-datums), clustering with the exact solver's
  own `_layer_ids` so it bands on the same layers. **Measured** (not assumed): droop does NOT grow without
  bound, it SATURATES — interior waviness ramps over ~7 layers to `sag_depth(stepover)` and then locks, so
  a reset reads as a repeating band (flat → ramp → saturate); peak heights are BIT-IDENTICAL with the reset
  on or off (it re-datums exactly onto the peaks), so it changes waviness only, never scaffold height.
  Guarded by **`test_sag_reset.py`** (13 tests).
- **Sag resolution** `(2026-10-07)` — the solve's OUTPUT sampling density, a module seam like
  `set_sag_law`: `set_sag_resolution(q)` / `sag_resolution()` (`engine/gcode_to_fibers.py`, after `_sag_law`).
  q divides `SAG_STEP_D` and `CREST_STEP_D` and lifts `SAG_MAX_SPAN_SAMPLES` / `CREST_MAX_SAMPLES` /
  `_ROUND_CRESTS_MAX_SHIFTS` (and the world engine's collector-curvature step) by q; `sag_approx` reads
  the same factor. 1 = byte-identical. NOT cosmetic at 1: `_round_crests` works on the samples, so the
  shoulder beside a support sits up to 0.905 d low on the default sag ladder vs the 10x solve (3x:
  0.109 d) — the "kink on a ladder span's flank". It does not touch a landing (see Tangent touchdown
  below). `.depo` key: `_depo_cache_meta` appends q
  only when != 1 (existing caches stay valid). CLI `--sag-resolution` on the converter, `scaffold_gcode`
  (`_sag_resolution_argv`, forwarded only off 1) and the player (set in `main` before any cache read).
  GUI: "Sag resolution" combo — designer `s_sag_resolution` (label, `SAG_RESOLUTION_CHOICES` in
  `gui_common`), emitted by `_sag_solver_args(with_reset=True)` → `_sag_resolution_args_for`, so every
  designer build AND player launch gets it; Calibration page `sw.sag_resolution` (value) in
  `_sw_quality_args`. Persisted `s.sag_resolution` / `sw.sag_resolution`. Guarded by
  **`test_sag_resolution.py`** (7 tests).
- **Tangent touchdown** `(2026-10-07; converged 2026-10-08)` — `_tangent_touchdowns`
  (`engine/gcode_to_fibers.py:4767`), called in `_solve_layer` on the FREE droop, after `floor_arc` and before
  `_round_crests` / the rest clamp; `sag_approx` makes the same call. Where a span's droop reaches the
  floor (the bed, a fibre lying along it, the flank of anything below), `_relax_pins` (`:4694`) runs
  paper V6 §2.3.4's relaxation to convergence: each round pins the deepest penetration of every sub-span
  and redraws by the installed law; a pin the merged droop clears is released (reachable under p != 2
  and on a curved collector, whose chord term varies along the fibre). Each sub-span's depth is read
  once and kept while its ends do not move: per sample, every round, it cost a curved collector 14x. Under span^2 it is quickhull, so every stretch is an eq. 5 span and the fibre lands
  tangentially (L = sqrt(h/4c)) to within a sample. Never pinned on: the loop-entry / visit-seam
  windows, and each crossing support's own round top, `_support_caps` (`:4666`, R / sin theta along
  the fibre, sin floored at `_CAP_MIN_SIN` = 0.15), so a bridging fibre still hangs from its support and
  the §2.4.4 midspan stays c * span^p. The floor-peak pass pins a peak only where the CONVERGED droop
  rests on it (pinned, or within `eps_z`), not where the free droop of the whole span would reach it.
  The clamp's corner was 50-78 deg on the default sag ladder at every q; now ~1/q.
  `.depo` generation 9. Guarded by **`test_sag_touchdown.py`** (7 tests, RED proof in its docstring).
- **Same-path chords** `(2026-10-08)` — `_same_path` (`engine/gcode_to_fibers.py:4063`), applied inside
  `_crossing_contacts` (chart engine, flat and mandrel; the lower segment is unshifted by the query
  piece's period shift first), `_WorldFibre.crossings` (compared at surface feet via `settle`) and
  `sag_approx`'s own crossing finder. A lower segment whose BOTH ends lie on this fibre's own XY path
  (to `_SAME_PATH_TOL` = 1 nm) is a chord of the same path, not a crossing: the sag ladder's lane
  strands, each laid on the previous sweep's path, read 9521 of 11116 "crossings" from those chords,
  each a support at a spot the sampling chose. Guarded by **`test_sag_same_path.py`** (3 tests, flat +
  mandrel, RED proof in its docstring). The world-engine variant has no unit case; the five-axis
  matrix is its only exercise.
- **Physical (tensioned-thread) sag law** `(OPT-IN — major feature, 89754bb 2026-06-26):`
  the default droop is the geometric `sag_depth` heuristic; with `--physical-sag` the physics build swaps
  in `mew_physics.bridge_sag_depth` via `make_sag_law` → installed into the `_SAG_LAW`
  seam. Reduced-order molten-thread parabola `depth = c·span²` (`c = sag_coeff`, mm⁻¹) — NO first-principles
  mechanism (the earlier static-tension `w·span²/8T` reading is RETRACTED: Nguyen [S41] disproves static
  tension; span² is grounded as the stretching-regime catenary [S44]/[S32]). Diameter exponent defaults to 0,
  so the droop is diameter-INDEPENDENT and `sag_diam_ref` is inert (folded into `c`); it re-enters only as the
  opt-in hook `(d/sag_diam_ref)^q`. All tunables in the `Calibration` `sag_*` block, `sag_coeff = 0.341588 mm⁻¹`
  (= old 0.004 / d_ref 0.01171). Sets descent MAGNITUDE only; the rest-on-top envelope geometry
  is unchanged, so the geometric default stays byte-identical. Cache meta flags it as `phys_sag`.
- **Sag solver: exact / approximate / off** `(GUI dropdown, 2026-07-31 — REPLACED the "Disable
  sagging (straight bars)" checkbox):` one combo per input panel, `SAG_SOLVER_CHOICES` in
  `gui/gui_common.py` (label ↔ value, the `RENDER_CHOICES` idiom). Sites, all six:
  the controls (`gui/gui_scaffold.py` `_build_export_tab` — Export disclosure, `s_sag_mode`;
  `gui/gui_core.py` `_build_gcode_tab` — G-code-import page, `sag_mode`), the greying
  (`_toggle_scaffold_sag` / `_toggle_sag`, via `_sag_solver_value`), the argv
  (`gui_args._sag_solver_args` — ONE emitter for both the worker and the player, which is what
  keeps the `.depo` cache key in step), the persist map (`g.sag_mode`/`s.sag_mode`, with
  `_migrate_legacy_no_sag` translating a pre-dropdown preset's boolean), and the engine
  (`gcode_to_fibers.apply_sag_approx` → `sag_approx.drape_sag`, behind `--sag-approx`;
  `scaffold_gcode` forwards it into `gtf_argv`).
  **The seam is in `gcode_to_fibers.main`, NOT `mew_export`** — `mew_export` only runs with
  physics-deposit ON, so a seam there would have silently given an exact solve with physics off.
  `apply_sag_approx` evaluates `_fusion_law`/`_FUSION_BAND` at the same diameter `_SagState` uses
  and lets the droop law default to the installed `_SAG_LAW`, so both solvers run the same physics.
  **Cache meta slot 9 (`sag_approx`)** — nothing else in the meta distinguishes the two solvers'
  output, so without it the player rides exact geometry while the STL is approximate. The
  live-adjust seed (`_load_live_locked`) passes `sag_approx=False` unconditionally: that march
  re-derives with the exact `_SagState`, so an approximate cache is geometry it cannot reproduce.
  Guarded by `test_sag_approx.py` + `tools/sag_bench.py compare|invariants` + three
  `test_ui_shell.py` cases (argv matrix, greying, preset migration).
- **Layer-spanning fibres — and the LIMITATION this fix leaves behind** `(2026-08-02):`
  `_layer_ids` (`:2047`) and `_layer_ids_deposition` (`:2061`) take ONE representative Z per
  fibre — its **median** — so a fibre walking through several layers is assigned whole to the
  layer nearest its middle and the stack collapses into a single plane. Nothing then has a layer
  below to rest on, every crossing becomes an in-layer self-intersection, and the solve explodes.
  **Measured** on the 30×30/0.5/×10 default whose 10 fibres had been welded into one:
  `_layer_ids` reported **1 layer instead of 10**, points 666,893 → 3,428,274, drape 3.2 s →
  47.9 s, and `/solve/convert` at exact sag went past the 120 s guard as a 504.
  Fixed by `_split_layer_spanning` (`:2108`) + `_rejoin_split` (`:2368`): cut each fibre into
  Z-bands of at most `layer_tol` BEFORE clustering, solve them as the separate layers they are,
  concatenate each fibre's bands on output so the one-entry-per-input-path contract is unchanged.
  The band test is **cumulative against the band's anchor, not per-step**, so a spiral climbing
  1 µm per point bands correctly instead of never triggering.
  **Byte-identical where it does not apply, by construction:** the helper returns `None` when no
  fibre spans, and the caller then runs its original code verbatim. Every scaffold this program
  generates is in that class — measured across ten configurations, Z span per fibre exactly
  **0.0000**.
  **TWO SEAMS, deliberately.** Both engine entry points split for themselves — `apply_sag`
  (`:6412`) and `apply_sag_approx` (`:6733`), the latter at the seam rather than inside
  `sag_approx` so the two solvers keep taking identical geometry. **Lite's
  `lite_core._drape` does a SECOND one** (`lite/lite_core.py`, at the `sag_approx.drape_sag`
  call) because that path deliberately bypasses `apply_sag_approx` — its fusion sink, wall band
  and droop law come from the request's calibration profile rather than from the engine's module
  globals. Same helpers, same `0.5 * diameter` tolerance `drape_sag` itself uses, so both solvers
  band identically. That is not duplication to collapse; deleting either one re-opens the defect
  on that path only.
  **THE LIMITATION — three cases this does NOT cover.** (1) **The incremental warm-start is
  excluded**: `frozen`/`reuse_sagged` are indexed per input fibre and the player guarantees the
  frozen set is a deposition-time prefix, neither of which survives re-indexing onto bands — so
  `gcode_player --live-adjust` on a layer-spanning import still collapses, while the export of the
  same file is correct. (2) **`--render-travel` changes output**: its laid travels give each fibre
  a one-layer-height tail (span 0.0200 at the 0.02 default), which used to survive only because
  the *median* is robust to a few travel points; those points now band into the layer above.
  Intended, but a real behaviour change on that flag, not a no-op. (3) **The upstream cause is
  untouched**: the Scaffold page's `travel_as_g1` export option (default ON, desktop
  `gui/gui_scaffold.py:614`, Lite `lite_params.py:391`) still erases the G0/G1 distinction the
  converter needs, so a re-imported scaffold is still ONE fibre — now correctly banded, but its
  entry/exit travels are still drawn as deposited fibre.

- **Layer-change seam (flat bed, 2026-10-05)** -- `seam_tail_z` / `seam_ramp` (`engine/gcode_to_fibers.py`),
  the previous layer's tails in `_SagState.prev_tails`, mirrored in `engine/sag_approx.py`; the STL's
  `_drop_travel_spans(keep_xy=)` keeps a hop of <= 2 diameters. Guard: `test_layer_change_continuity.py`.
  **KNOWN, LEFT ON PURPOSE:** on polygons whose layer rotation puts a pass through a corner (decagon
  at 36 deg), exit and entry differ by ~0.017 mm, so the g-code steps back 162 deg then forward 162 deg
  (net heading 0): a sub-diameter spur at the tip, drawn as a visible flat cap where two overlapping
  tubes meet. The tubes are connected, so it is cosmetic. If it hurts an FEA mesh or a render, the
  clean fix is in `engine/scaffold_gcode.py` (make the layer's exit and the next entry coincide), not in the
  mesher. Not handled: curved frames/mandrel (seam off), a sag-reset layer (laid straight, keeps its
  break), a long travel hop (omitted from the STL unless `--travel-in-stl`).

### 2.5 Crossover / contact detection
In `engine/gcode_to_fibers.py`: **`class _SegIndex` (`:2483`)** — spatial-hash segment index: `add_fiber`
(`:2845`, the settled-aware twin at `:4938`), `rest_heights` (`:3251`; `floor` is the bed-UNDERSIDE rest level — `bed_center` plus a radius, so the
fibre's underside lands on the bare surface; a per-point array when the layer is laid at a per-point
diameter, see `_solve_layer`'s `bed_at`),
`point_pairs` (`:3180`), `_build` (`:3066`; the second index at `:4959`). `_seg_hits_xy` (`:2379`),
`_crossing_contacts` (`:4109`, drops same-path chords via `_same_path` `:4063`), `_self_intersections` (`:4017`), `_self_contact_heights` (`:4166`),
`_round_crests` (`:4598`), `_hash_cell` (`:3532`, cell size from diameter).
- **Fiber fusion / squishing** `(OPT-IN — major feature, f35ef11 + 47df7e9 2026-06-26):`
  crossover-localised junction compaction — fibres that touch at a crossover partially melt-fuse and
  squish DOWN instead of stacking a full diameter. `_FUSION_LAW` seam (`set_fusion_law` `:1887` / `_fusion_law`
  `:1903`, controls sink depth from diameter), applied by `_sink_fused_contacts` (`:1934`): sinks fibre-on-fibre
  rest heights at the `resolve_supports` + `floor_arc` twins AND — on the self-crossing path — both the lower
  strand's `z_lo` rest and the `_self_contact_heights` lift. Net effect: a turnaround/loop **tip** fuses at the
  inter-layer junction UNDER it as well as the self-cross on top, landing on the grid datum instead of
  sticking up a diameter (47df7e9 fixed the missing twin-sink that left tips stacked). Never touches the
  spans. Cache meta flag `phys_fuse`; output byte-identical with fusion off.
- **Wall (straight-span) vs junction fusion** `(OPT-IN sub-feature — 2026-07-16):`
  a contact is not one thing. Two fibres CROSSING share melt over ~`d/sin θ` — a square crossing is a
  **point** contact one fibre wide; a **collinear** pair (the classic rotation=0 wall, and equally layer N+2
  drooping onto the parallel layer N once the rotation series comes back around — N+2 at rot=90, N+3 at
  rot=60, N+5 at rot=36) is a **line** contact running the whole span. Same physics, very different contact
  length, so a rig may calibrate the two to different magnitudes. `Calibration.fusion_wall_overlap`
  (**None → reuse `fusion_overlap`**, i.e. one magnitude everywhere = byte-identical, which is why it is the
  default) + `MEWModel.wall_sink` / `fusion_wall_band`; the law gains a `wall` flag
  (`law(diameter, wall=False)` — the default keeps an old one-arg call site on the junction magnitude).
  Seam: `set_fusion_law(law, wall_band)`; `wall_band=None` ⇒ the solver never computes an angle
  (`angular` gate in `_solve_layer` keys on the two MAGNITUDES differing, not on a band existing).
  Discriminator = `|sin θ|` between the fibre's tangent and the support's direction, blended by
  `_wall_weight` (smoothstep over a **5°–25°** band, dormant `fusion_wall_angle_lo/hi`). **Two non-obvious
  requirements, both measured, both load-bearing** — get either wrong and the geometry cracks:
  (1) the angle is read over a **~1-diameter arc window** (`_tangent_at(window=)`), because a raw
  per-segment tangent swings 23°→0° across a 2.4 µm step at a resampled loop corner and drops a 7.6 µm
  cliff into a continuous centreline — physically, contact melt is smeared over ~1 fibre width, so the angle
  is only meaningful at that scale; (2) each **candidate** contact is sunk by its own angle **BEFORE** the max
  (`_SegIndex.rest_heights(pair_sink=…)`), never the winner of a geometric max after — otherwise two
  candidates tie at equal height, take different sinks, and the floor steps by `(φ_j − φ_w)·d` the instant the
  argmax flips (measured **1.04d** on a rot=45 scaffold). (2) is also the correct physics: a fibre rests on the
  highest **post-fusion** contact — it cannot sink 0.9d into a crossing fibre when a collinear one beneath
  yields 0.05d. Because `max()` of continuous candidates is continuous, that cliff class is gone by
  construction. Measured selectivity is **exact**: a pure wall answers only to φ_wall (φ_junction=0.9 leaves it
  at 140.0 µm, unchanged) and a pure junction tower only to φ_junction. GUI: the **"Fusion"** section
  (renamed from "Junction fusion") on the Physics page — `cal.fusion_overlap` ("Junction sink φ") plus
  `cal.fusion_wall_overlap` ("Straight-span sink φ") behind the tickbox
  **"Different fusion magnitude for straight spans"** (`cal.fusion_wall_overlap.on`), via the registry's new
  `FieldSpec.gate_label`/`gate_key` (§2.11). Unticked ⇒ the field reads BLANK whatever it remembers
  (sentinel folded in `_calibration_profile_snapshot`, the `_sag_reset_arg` idiom) ⇒ None ⇒ inherit.
  **The embedded PREVIEW tracks the split too** (2026-07-16): a bare heightmap knows how high the structure
  below is but not WHICH fibre put it there. (2026-07-31: the raster is HISTORICAL --
  the rebuilt `preview_sag` has no heightmap at all; each contact's wall/junction angle
  reads off its own segment PAIR, same `_wall_weight` band, sinks applied per-candidate
  before the max exactly as `rest_heights(pair_sink=)` argues.) Plumbed as
  `_pv_fusion_law() -> (law, band)` → `sag["fuse_law"]`/`sag["fuse_band"]` → `drape_sag(fuse_wall=, band=)`.
  Guarded by `test_mew_fusion.py` (33 tests: both byte-identity gates — unset AND set-equal — selectivity both
  ways, the fuse-before-max mechanism built directly on the index, and the 1-diameter tangent window) +
  `test_preview_sag.py` (16: the drape's own split gates + it TRACKING the exact solver on both archetypes) +
  `test_preset_diameter.py` (63: the field AND its tickbox ride the Physics preset, a real JSON save/load
  round-trip restores both and re-enables the gated entry, and an OLD preset falls back to inherit rather than
  carrying a stale value over). NB `fit_fusion_overlap` fits
  **whichever φ you measured**: a straight stacked WALL fits `fusion_wall_overlap`, a crossover peak tower
  fits `fusion_overlap` — before the split its wall reading was the only φ and got applied at crossings, which
  is exactly the conflation this undoes. The Calibration page's **Fusion** card (2026-09-24) takes both at
  once off the operator's OWN scaffold, no print generated: measured d, then junction rows and optional wall
  rows of (layers, height), each table fitted by `fit_fusion_rows` (least squares on the same law). Two Apply
  buttons, one per φ; the wall one writes the value WITH its tickbox. The tab hides every print tile and the
  preview (`gui_calibration._CAL_FIT_ONLY`).

### 2.6 Tube mesh + STL writer + `.depo` cache
The mesh/output core of `engine/gcode_to_fibers.py` — this section is its home.
- **Tube sweep:** `tube_mesh` (`:1309`, vectorised RMF-frame swept tube; scalar OR per-vertex radius
  `rad_b` for dynamic diameter; optional exact `with_normals`), `_tangents` (`:1219`), `_rmf_frames`
  (`:1237`, rotation-minimizing, no twist), `_dedupe_idx` (`:1181`). Plate primitives `box_mesh` (`:1617`),
  `disk_prism_mesh` (`:1644`).
- **Deposition mesh (for the player's reveal):** `build_deposition_mesh` (`:6932`), `_tube_fibers`
  (`:7074`, per-fiber **block cache** at offset-0 for O(1) live re-lay + dynamic-diameter radius),
  `_resample_by_time` (`:6837`).
- **`.depo` cache** (the contract with the player/viewers/incremental): `write_depo_cache` (`:7310`),
  `load_depo_cache` (`:7382`, rejects a mismatched-mode/older cache), `load_depo_diam_by_time` (`:7541`),
  `load_depo_pdia` (`:7559`), `load_depo_groups` (`:7594`), `_load_depo_raw` (`:7612`, for `--from-depo`),
  `_depo_cache_meta` (`:7297`, 6-float meta incl. `transformed`/`phys_sag`), `DEPO_CACHE_EXT=".depo.npz"`
  (`:7214`). **Faces/resolution-independent** — stores sagged centrelines, viewer tubes at preview fidelity.
- **Mesh-sampling decimation** (`--mesh-tolerance`, default 1.0, on the way INTO the tube loop only):
  `decimate_for_mesh` → `decimate_keep` → `_span_max_dist` + `_span_max_turn`, tolerance from
  `facet_error`. GUI: **Export → "Mesh detail"** on both the scaffold designer (`gui/gui_scaffold.py`,
  `s.mesh_tol`) and the g-code→STL panel (`gui/gui_core.py`, `g.mesh_tol`); labels in
  `gui_common.MESH_DETAIL_CHOICES`, argv in `gui_args._mesh_tol_args_for`. The sag solver samples for
  the SOLVE (uniform interior points + crest detail per drooping span), which is finer than a
  `faces`-gon tube can express — a 30×30 print goes 690k sagged points → 16.5M tris → 824 MB.
  **TWO tests, not one:** a merge is accepted only when `centreline deviation + r*turn <= tol`, where
  the tolerance is the tube's OWN faceting error `r*(1−cos(π/faces))` (0.27 µm at r=8 µm, 12 faces).
  The turn term exists because a tube on a bend moves further than its centreline — without it the
  measured SURFACE deviation ran to 1.7 µm and every worst vertex sat on a turnaround loop, which is
  modelled physics here (jet-lag corner rounding). Straight runs are unaffected, which is where the
  reduction comes from: 824 → 471 MB. (The "viewer open 3.88 → 1.77 s" once claimed here was
  `stl_viewer`, which the app no longer opens; this flag is **not** on the unified Viewer's path at
  all — that reads the `.depo`. See `gui/gui_run.py`'s `stl_render`/`sim_render` note.)
  **Not Douglas–Peucker** (RDP's recursion cost 4.0 s on a 288k-point ellipse, more than the mesh it
  saved): bottom-up, vectorised, each pass validating proposed drops against the ORIGINAL points so
  error cannot accumulate — measured against `lite_codec.pd_mask`/`dp_mask`, which overshoot their own
  tolerance by 1.5× and run 9× slower respectively. `--mesh-tolerance 0` / "Full" → the previous
  byte-identical STL. **The `.depo` cache is written BEFORE this**, so the player, deviation view and
  analysis engine keep the full sampling. The centreline bound is proven; the SURFACE number is
  measured separately (`.perf/surfdev.py`: mean 0.095, p99 0.431, max 1.738 µm, the max unattributed)
  and must not be restated as a bound. Guarded by `test_mesh_decimate.py`.
- **STL writer:** `write_binary_stl` (`:7643`, vectorised, chunked 1M tris, the `_STL_REC` dtype `:7600`).
  The phase is **memory-locality bound, not disk bound** — 850 MB of raw bytes writes in 0.76 s here,
  while the same 16.5M-triangle mesh takes 3.3 s tube-ordered and 8.6 s with randomised vertex
  indices. Do not add `truncate()` preallocation: measured 2.5× SLOWER (`.perf/writetest.py`). **Do
  not delete the degenerate-facet zero-fill** either: it looks dead (|n| == 0 only when n is already
  zero) but a cross product can give NEGATIVE zero and that line normalises it — removing it changes
  the bytes. It is behind an `.any()` guard, so a real mesh never pays for it.
- **Viewer read path:** `stl_viewer._binary_stl_polydata` reads a binary STL with numpy (one
  structured array, cells from two `arange()`s, 1M-record chunks) and DECLINES anything uncertain —
  ASCII, truncated, a header count that disagrees with the file length — falling back to
  `vtkSTLReader`. 824 MB opens in 3.18 s against 3.88 s. `gcode_player._toggle_overlay` routes
  through it too: it used to use a bare `vtkSTLReader` with point-welding ON, 2.72 s vs 0.43 s on a
  409 MB mesh. Guarded by `test_stl_fastread.py`.
- **Orchestration:** `main` (`:7757`) — `[GCODE-READY]` marker (`:8076`) → parse/`--from-depo` →
  `_FIBER_TRANSFORM` seam (`:8166`) → per-scaffold grouping (`:8272`) → `apply_sag` (`:6412`) →
  `write_depo_cache` (`:7310`) → tube loop (`:8621`) → `write_binary_stl` (`:7643`) + per-scaffold
  siblings → `_write_build_plate` (the plate top sits AT the deposition datum, full stop — a
  bottom-layer fibre rests its UNDERSIDE there, so the datum IS the contact plane. It used to drop
  datum−diameter on every export but the dynamic-diameter one, compensating for a bottom layer
  whose centreline sat on the datum). CLI: `-d`/`-f`/`--no-mesh`/`--from-depo`/`--no-sag`/
  `--sag-multiplier`/`--scaffold-groups`/`--plate-*`.

### 2.7 Machine model / build plate / kinematics
- **`machine/machine_model.py`** — pure-geometry multi-body printer model (no VTK). `class
  MachineProfile` (`:113`, methods): `from_dict` (`:243`, new role-keyed + legacy axis-keyed schema),
  `contact_alignment` (`:1243`, bed-slide / who-closes-the-gap), `axis_side` (`:1205`). **Module-level
  placement functions** (take `profile` 1st — NOT methods): `place(profile, tip, gx, gy, gz)` (`:1542`,
  per-frame part offsets) and **`place_scene(profile, tip, head, …)` (`:1598`, the player's entry
  point** — origin re-base, collector registration split, motion-limit clamp). `find_tip` (`:1478`), `find_bed_top_face` (`:1493`, bed-datum auto-detect). Chains
  `CHAINS` (`:76`: mewron / xyz_head / xy_bed_z_head / custom). Tests: `test_machine_model.py`,
  `test_player_scene.py`.
- **`gui/gui_buildplate.py`** `_BuildPlateMixin` — the "Build plate" tab. Since 2026-09-16 it is
  `_init_machine_vars` (every var, no widgets) plus SECTION builders that both UIs lay
  out (`_build_plate_fields` / `_build_origin_fields` / `_build_machine_stl_fields` /
  `_build_machine_calibration_fields` / `_build_rotary_*`; the modern layout is §2.12
  U12). `_build_buildplate_tab`
  (`:144`), `_build_machine_model_group` (`:551`, kinematics combo + STL rows + motion-limit rows),
  motion-limit rows `_machine_limit_row` (`:1584`) / `_refresh_motion_limits` (`:1622`), custom-chain
  table `_build_machine_custom` (`:1638`), bed calibrator hook `_calibrate_bed`
  (`:1763`)/`_poll_bed_datum` (`:2129`), derived geometry `_plate_top_z` (`:2159`)/`_origin_offset` (`:2494`),
  `_machine_persist_keys` (`:2557`), **CLI builders** `_plate_args` (`:2635`)/`_scaffold_origin_xy` (`:2661`)/
  `_viewer_origin_arg` (`:2671`)/**`_machine_args` (`:2679`, writes the role-keyed `machine.json`)**.
  `_KINEMATICS` (`:34`).
- **The ROTARY (4-axis) MOUNT** — `machine_model.RotaryAxis` carries the joint
  (`word`/`role`/`radius`/`length`/`stl`/`axis_height`); `role` accepts a moving body
  OR **`"frame"`** (a static stage), and `place_scene` gives a frame-mounted stage
  **no collector motion at all**. The Machine page's "Rotary collector (4-axis)"
  group (`gui_buildplate._build_rotary_group`) writes ONLY the mount into the
  profile — the rod's diameter/length are the JOB's, and `gcode_player._build_machine`
  installs them ONTO the profile's joint rather than replacing it, then **drops the
  joint entirely on a flat job** so a plate print on a 4-axis rig is the scene it
  always was. The axis' XY is the existing `slide_offset`; only its HEIGHT above the
  mount body is new, and the player folds `axis_height + R0` into the bed contact so
  the nozzle stands one standoff above the ROD SURFACE. Tests: **`test_rotary_mount.py`**.
- **THE HOLDER IS THE BED** (2026-09-03, Joseph: *"the correct solution is to
  import the mandrel holder into the bed slot and we can calibrate the actual
  cylinder position based off the holder"*). A 4-axis rig has no build plate, so
  the body that carries the print IS the mandrel holder and it goes in the
  **`parts["bed"]` slot** — one part, one datum, one Calibrate button,
  `machine.json` unchanged. `gui_buildplate._sync_collector_words` renames the
  two rows when the rotary tick flips, because what changes is what the numbers
  MEAN: "Bed datum / Top Z / corner X / corner Y" becomes **"Mandrel axis datum /
  Axis Z / Axis X / Axis Y"**, the mandrel AXIS in the holder STL's own frame.
  Consequences: `_compute_registration` needs NO rotary branch (it already seats
  the g-code origin at datum + slide offset, and for a tube that origin is a
  point on the axis); the player's bed contact uses `radius` alone when a manual
  datum is set and `axis_height + radius` when it is not (`axis_height` is the
  auto-detect FALLBACK, and the run log says which of the two reasons made it
  inert); and `place_scene` now freezes the bed **body** as well as the build
  group for `role == "frame"`, or a static stage's holder sank down the Z rails
  with its rod hanging above it. The separate `rotary.stl` field is gone —
  `gui_presets._migrate_legacy_holder_stl` moves an old one into `machine.z_stl`
  unless a bed is already named. Tests: **`test_rotary_mount.py`**
  (`test_the_manual_datum_is_the_axis_on_a_rotary_rig` measures the bed's own
  placement with the datum and without, and pins the absolute
  tip − axis − standoff − R0; `test_a_frame_mounted_holder_does_not_ride_the_z_rails`
  carries the bed-mounted control), `test_ui_shell.py` (the migration).
- **WHERE THE ROD IS, AND WHERE A TUBE READS ZERO** (2026-09-03). The same group
  carries the rod's **length** and its **near-end X** (`rotary_rod_length` /
  `rotary_rod_x`) — the rotary counterparts of the plate's size and Corner X/Y, so
  they travel to the WORKERS (`--start-x`, `--mandrel-start`/`--mandrel-length`) and
  not into `machine.json`. The DIAMETER stays on the 4-axis designer: it is in the
  winding maths and the other two are not. Unticked, the rod is still derived from
  the print and every default is unchanged.
  The **G-code zero** section (was "G-code origin") gains the 4-axis choice, "Zero along the rod / radially": `rotary_origin_axial`
  (rod near end / far end / mid-length) x `rotary_origin_radial` (rod surface /
  rod axis), both COLLECTOR-origin datums like the plate's face/corner — under the
  World origin the zero is fixed and the ROD moves by its near-end X. There is no
  lateral ladder: a tube program emits no Y word, so the datum is the mandrel axis
  by construction.
  **Custom zero** (both collectors): "Custom" on the Corner dropdown or on "Zero
  along the mandrel" opens `bed_calibrator --pick zero` on the collector drawn from
  its size (`--zero-plate W,H,T` / `--zero-disc D,T` / `--zero-rod D,L`); the pick
  is one "z,x,y" in the collector frame, `origin_custom` / `rotary_origin_custom`
  (persist `origin.custom` / `rotary.origin_custom`, machine preset). Read in
  `gui_buildplate._origin_datum` and `gui_mandrel._mandrel_origin_u` (+ the G-code
  page's `gui_gcode_twodesigns` override); the rest is `gui_buildplate`'s
  `_zero_*` helpers + `_calibrate_zero`. Header Zero line says "custom";
  unpicked Custom is refused at Generate (`_zero_custom_error`).
  ONE DEFINITION each: `gui_mandrel_preview.axial_layout` (rod interval +
  `print_start_x`, read by the preview, the argv and the player alike) and
  `gui_mandrel._mandrel_origin_u` / `_mandrel_origin_offset`, which overrides
  `_origin_offset` for the duration of a tube dispatch so `gui_run`'s frozen origin
  arg — and therefore the player's undo — is right for both collectors from one
  call site. The offset is **AXIAL ONLY**; the radial datum rides
  **`--mandrel-surface-z`** (the g-code Z of the bare rod) into the generator's
  `start_z`, the converter's `CylinderFrame(surface_z=)` and the player's, because
  a Z re-base would be undone twice — once by the frame unrolling the file and once
  by the player putting the scene back in machine coordinates.
  **WHERE ON THE ROD the print goes** replaced the end margin: `place` (offset
  from the left end / centred / offset from the right end) + one offset, resolved
  in `axial_layout`, with `clearance` (near, far) out the other side -- negative
  when the print hangs off, which is how the page refuses it by name. On a
  DERIVED rod all three placements coincide, because the rod is however long the
  print needs. `gui_presets._migrate_legacy_end_margin` carries an old
  `m.end_margin` onto the left rung. Tests: **`test_rotary_mount.py`**
  (layout + both datums, each against its own control), `test_gui_mandrel.py`
  (the argv), `test_mandrel_collector_length.py` (designer rod == viewer rod).
- **THE FIVE-AXIS COLLECTOR TABLE ON THE MACHINE PAGE** (2026-09-09, slice 2 of
  the real-five-axis task). A pair is **two rotary BODIES**, so each joint gets
  its own STL slot and its own Calibrate button — `_build_rotary_joint_rows`
  (`gui/gui_buildplate.py:992`, the modern card) or `_build_rotary_joint_word_rows` +
  `_build_rotary_joint_stl_rows` (the classic `_build_rotary_group`), writing `stl` + `pivot` per entry of
  the `"rotaries"` list (`_rotary_pair_joints`). Five things worth knowing:
  * **The pivot's only source is the pick.** No Entry, anywhere: the picked
    triple is a READ-OUT in the same slot the mandrel's "axis located" line
    uses (`_sync_pair_axis_note`), and `Calibrate…` is the only writer. It is
    stored as ONE `"z,x,y"` string per joint (`rotary.j1_pivot` /
    `rotary.j2_pivot`), because the picker writes one triple and three keys is
    three chances to half-fill or transpose it. Blank IS uncalibrated, and an
    unpicked joint writes **no `pivot` key at all** rather than `[0,0,0]`.
  * **The order flips once, in `_rotary_pair_joints`.** `bed_calibrator` writes
    `z,x,y`; `RotaryAxis.pivot` is `x,y,z`.
  * **The frame is that joint's own STL frame**, verbatim — the convention
    `bed_top_z`/`bed_corner` already use for the bed. `machine_model` does every
    conversion (theta, slice 1: every part STL is loaded into the machine frame
    by ONE rigid shift shared by all parts).
  * **`bed_calibrator --pick joint`** is the third pick mode: the axis line and
    the 0.5 mm marker, no rod. Its line is padded by a quarter of the part's
    DIAGONAL, because a spin plate is thin along its own axis.
  * **What a pair stops offering.** The bed slot goes back to saying **bed** —
    on a pair it holds whatever is under the table — so `_COLLECTOR_WORDS`'
    third column is a copy of its first. `slide_offset` is emitted `[0,0]` and
    its row is ungridded, joint 1's pick being the single source of the
    collector XY. And the kind selector is two **radios outside the greyed
    body**, not a readonly combobox inside it, which is what made a user
    conclude the 4-axis mode had been deleted.
  Tests: **`test_gui_buildplate.py`** (34 cases, comparator
  `MachineProfile.from_dict`, eleven verified RED injections), `test_ui_shell.py`.
- **THE PAIR'S ROTARY LIMITS** (2026-09-18, slice 1 of the five-axis shelve).
  Five fields in the rotary group, built by `_build_rotary_limits_rows`
  (`gui/gui_buildplate.py:1027`), vars in `_rotary_vars_make`:
  **spin rate, spin acceleration, tilt rate, tilt acceleration** (deg/s,
  deg/s^2) and the **maximum feed** (mm/min). 0 means not limited.

  * **The list is `rotary_limits.RIG_LIMIT_FIELDS`**, and it is the ONE place
    the persist keys (`rotary.max_*`), the `App` attribute names and the
    shipped defaults are written. `_machine_persist_keys` takes its keys from
    there, `gui_surface._surface_limit_args` sends them
    (`--surface-max-spin`, `--surface-max-spin-accel`, `--surface-max-tilt-rate`,
    `--surface-max-tilt-accel`, `--surface-max-feed`), and
    `test_five_axis_matrix.export_argv` generates every case with
    `rotary_limits.default_argv()` -- so the suite's files are the files a user
    gets by pressing Generate and touching nothing.
  * **Shipped: 720 deg/s^2 on BOTH accelerations, everything else 0.** 720 is
    the C axis's measured limit (memory `rotary-accel-limit-720`); the A axis
    has never been measured and the tooltip says so. No rate and no maximum
    feed is known for either joint and nobody is to go looking (the shelve
    spec), so those three are fields for the day a number exists.
  * **WHY THIS PAGE AND NOT THE CURVED-COLLECTOR ONE**, where oak wrote them
    (`collector-pole-zone`): preset ownership follows the tab a field lives on
    (`gui/gui_presets.py` header), so on the designer they rode the SCAFFOLD preset
    and could not reach `presets/machine/5_Axis_PhD_Printer.json` at all. A rig
    limit retyped per design is a rig limit that is eventually wrong. The
    designer shows them read-only instead
    (`gui_surface._surface_limits_readout`).
  * **The FEED FLOOR stayed on the designer** (`sf.min_speed`; since 285bce8 it is
    sent only while "Slow the feed…" is ticked, §2.21): it is how far
    the jet may be slowed before it coils, which follows the material and the
    process, not the machine. Blank resolves to
    `mew_physics.MEWModel.min_print_speed` -- CTS x `CTS_FLOOR_MARGIN` -- which
    the matrix calls too. **It binds every move, TRAVELS INCLUDED** (2026-09-18,
    Joseph: *"since the real jet is continuous, i see no reason travel fibers
    should be treated differently"*). A travel used to be exempt from it and
    from feed compensation both, so the library dome's nine layer hops crossed
    the collector at 11 mm/min against a 528 floor and drew a knot at every
    layer boundary. The floor is defined in the COLLECTOR's frame -- how far
    the contact point walks, `scaffold_gcode._surface_arcs`, not the nozzle's
    speed and not what F divides -- and `collector_checks.check_surface_speed`
    is the guard (the matrix's `surface` column).
  Tests: `test_gui_buildplate.py`, `test_ui_shell.py`, `test_rotary_mount.py`,
  `test_five_axis_gcode.py`, `test_five_axis_matrix.py`.
- **THE PAIR'S TWO CALIBRATED DATUMS** (2026-09-10). Joseph, on what every
  printing mode needs and five axes had neither of: *"1) where the collector
  (the thing being printed on) is located relative to whatever is holding it
  (bed for 3 axis, mandrel mount for 4 axis, C axis for 5 axis). 2) where you
  are zeroing your nozzle relative to the collector, the origin of the gcode."*
  Both live on **joint 1** beside its pivot, both are one `"z,x,y"` triple, both
  are calibrator-written, and blank means uncalibrated.

  | | 3-axis | 4-axis | 5-axis |
  |---|---|---|---|
  | 1. collector on its holder | bed top face + corner | mandrel axis in the holder's frame | **`rotary.j1_seat`** |
  | 2. nozzle zero vs collector | plate face + anchor corner | rod near end + rod surface | **`rotary.home`** |

  * **THE SEAT IS NOT THE PIVOT**, and that is the whole reason it exists. A
    pivot is a point on the axis of ROTATION; a collector STANDS on a face.
    `_relative_pivots` used the pivot as the collector's origin, so on a dome
    the CENTRE OF CURVATURE sat on the table and the rim hung
    `mew_frame.DepositionFrame.mount_axial` above it — 3.125 mm measured on
    Joseph's rig with an R=10 dome spanning 0–12 mm, "a few centimetres" on a
    bigger one. `machine_model.collector_origin` is the seat walked back up
    joint 1's axis by that number; `collector_datum` still answers its own
    question (where the spin axis is) and is no longer the collector's seat.
  * **THE COLLECTOR ANSWERS `mount_axial`, never the machine page.** It is the
    minimum of `x(u)` over the span, plus `surface_x` — one rule for every
    shape, since a dome's meridian runs downhill from the pole (minimum at
    `u1`) and a cone's uphill from its base (`u0`). `None` on a plane and a
    cylinder: a rod is gripped at its ENDS and its placement is
    `RotaryAxis.seat`, untouched.
  * **THE ZERO COMES OFF IN `rotary_transform`, LAST**, so what that function
    returns is EXPORTED coordinates. There because the generator
    (`scaffold_gcode._five_axis_point`), the parser
    (`gcode_to_fibers.FiveAxisReader`) and the renderer
    (`gcode_player._collector_pose`) all arrive there — a file cannot be
    written from one zero and drawn against another. AFTER the rotation, not
    before: you jog to a spot with the rotaries at zero and datum there, and
    turning a joint afterwards does not move it.
  * **RE-SEATING IS A RIGID MOVE.** Shifting the seat by D shifts every
    commanded point by `R@D` — same length at every pose, different direction.
    Without a home the shift is `(R - I)@D`, which is what
    `test_five_axis_gcode` uses as its control.
  * **A HOMED FILE DROPS ITS OPENING `G92`.** That G92 datums the file to its
    own first print point and existed because nothing knew where the machine
    starts; keeping it would throw the pick away and put the nozzle back inside
    the collector at t=0. Its removal is also why a homed file finally opens
    with a visible lead-in travel.
  * **BOTH PICKS ARE MADE AGAINST THE DRAWN COLLECTOR.**
    `bed_calibrator --pick seat` stands the part on the marker;
    `--pick home` stands it on the already-picked seat and moves the marker
    against it, because the zero *"often is at the edge of the collector or off
    the collector entirely so the travel fiber does not intersect an important
    part of the print"*. The five collector words come off the Surface
    designer live (`gui_buildplate._collector_calibrator_args`) and go through
    `scaffold_gcode.surface_frame`, the one builder; the aim comes from
    `machine_model.collector_mount_rotation`, the same swing the kinematics
    mount by. With no seat picked the home window draws nothing and says so.
  * **Neither pick set → byte-identical**, four-axis included.
  Tests: **`test_machine_model.py`** (`collector_origin`, the exported zero,
  the mandrel no-op — seven verified RED injections),
  **`test_five_axis_gcode.py`** (the re-base, the rigid seat move, the G92),
  **`test_gui_buildplate.py`** (nine seat cases, six verified RED injections),
  `test_ui_shell.py`. Measured on the developers' rig.
- **`machine/bed_calibrator.py`** — standalone VTK datum picker subprocess; orbit + press **P** to
  set bed datum, writes `z,x,y` to `--out`. `main` (`:221`), the pick handler `_on_key` (`:669`). Launched by
  `gui_buildplate._calibrate_bed`.

### 2.8 VTK viewers
- **`viewers/stl_viewer.py`** — **the offscreen render backend + the shared VTK actor library.
  Its interactive window is DEPRECATED** (Joseph, 2026-08-04): the unified Viewer page
  (§2.12) is the current viewer, and the modern shell has no route to this one — its
  "View STL" button is built on `gui/gui_shell.py`'s never-gridded `_legacy_holder` (`:764`).
  The classic developer UI still opens it (`gui/gui_viewers.py:90` → `view_stl` →
  `_launch_stl_viewer` `:440`); that path is left working but is no longer the
  documented way to look at a result. **Do not add features to `view()`.** What IS
  current, and load-bearing for four other subsystems:
  - **Offscreen renders, no window ever** — `render_thumbnail` / `--thumbnail` (`:1493`,
    Print History, §2.17) and `render_shots` / `--shots` (`:1584`, §2.19, the geometry
    oracle the Stop gate runs). Both tested by `test_thumbnail.py`.
  - **Actor + reader library** — `_load_centerlines` (reads `.depo`), `_centerline_actor`
    (wireframe/`tube:N`), `_stl_actor`, `_render_sides`, `_travel_actor` (G0 hops as wires),
    `_binary_stl_polydata` (the numpy fast read, §2.6, `test_stl_fastread.py`),
    `_print_time_seconds`, `_RAINBOW`/`_SOLID_COLOR`. Imported by `viewers/gcode_player.py`
    (lazily, `viewers/gcode_player.py:3999`-5466), `viewers/micrograph_viewer.py` (`:35`); the
    colours are mirrored by `gui/gui_common.py:351`.
  - **Deviation LUT helpers** — see the Deviation entry in §2.14; the unified Viewer
    colours through them, so they outlive the `--deviation` window mode.
  - `parse_viewer_argv` (unit-tested, `test_stl_viewer_args.py`), `main`.
  - **Toggle overlays** (`view`, top-left stacked buttons): one Hide/Show per scaffold part + a
    `__color__` solid/colour button (existing), plus **a master `__scaffold__` Hide/Show-scaffold
    button** (flips every deposited-fibre actor at once, incl. the single-merged one with no per-part
    toggle) **and a `gcode lines` toggle** drawing the commanded deposition (G1/G2/G3) toolpath as thin
    gold wires (`gcode_player.DEPOSIT_COLOR`, off by default) so you can drop the mesh and read the raw
    toolpath alone. These are the STL VIEWER'S OWN toggles: the playback sim has a `G-code lines` switch
    of its own (which owns the gold G1 lines AND the grey G0 travels, 2026-09-18) but no scaffold switch
    any more — `run()` shows its deposition mesh unconditionally, so the exported-STL overlay only ever
    redrew what was already on screen and it was removed. The lines come from `_deposit_segments` (tiny local
    G1/G2/G3 parser, travels excluded — they keep their own toggle) + `_arc_xy` (local copy of
    `gcode_to_fibers.arc_xy_points`, 22.5°/chord). Both need the source g-code, like the travels.
- **`viewers/vtk_helpers.py`** — shared actor/camera helpers for BOTH viewers. `origin_marker`
  (`:18`), `fit_clipping_range` (`:114`, the per-frame clip fix, bounds-explicit),
  `install_cad_camera` (`:247`, cursor-pivot orbit/zoom, observer priority `NAV=0.3`),
  `set_projection` (`:172`) + `projection_toggle` (`:227`, the sidebar row). Test:
  `test_vtk_helpers.py`.
  - **The projection toggle preserves apparent size, and that is the whole mechanism.** The two
    projections express zoom with DIFFERENT quantities, so a bare `SetParallelProjection` jumps the
    image: orthographic zoom is the **parallel scale** (half-height in world mm shown at every depth),
    perspective zoom is the **camera distance** (half-height at the focal point is `d*tan(th/2)`).
    Equating them gives `d = scale / tan(th/2)` going out and `scale = d*tan(th/2)` coming back, so the
    focal plane keeps its on-screen size and only the depth cue changes. Direction and view-up are never
    touched, so an in-progress orbit and the ViewCube stay in step. `projection_toggle` reads the CAMERA
    for both its label and its switch state — nothing caches a mode a re-frame could contradict.
  - **Orthographic stays the default, and the reason is the zoom.** `install_cad_camera._zoom` drives
    parallel zoom by shrinking the parallel scale, which has no floor; perspective falls back to
    `cam.Dolly()` and stops against the near-clip clamp. So perspective is a toggle, never a default.
    The **offscreen shot renderers** (`viewers/stl_viewer.py` `:1517`, `:1719`) stay orthographic too — they are
    deterministic capture paths — as does the **ViewCube**, which owns its own camera (`viewers/view_cube.py:284`).
  - **The pivot depth read is `_depth`, NOT `renderer.GetZ` (VTK 9.6 fix, 2026-07-15).** `vtkRenderer::GetZ`
    is **broken on VTK 9.6** for a renderer whose viewport is INSET: it bounds-checks the pixel against the
    viewport's SIZE instead of its extent in the window. The themed viewers confine the scene to the right of
    the 240-px sidebar, so the scene spans `x=[240, W]` while GetZ only answers for `x <= W-240` — blinding
    exactly the **rightmost sidebar-width (240 px) strip** of the view, where it returns a bogus `1.0`.
    `_pivot`'s `0 < z < 1` guard reads that as "no model here" and silently drops cursor-anchored orbit back
    to the focal plane there, in the windowed + native-fullscreen viewers. That strip is a FIXED 240 px, so it
    is the right 60% of a 640-px test window but ~24% of the player's 1240-px default (measured 18–23 px of
    pivot slide inside it before the fix; 0.00 px after). **The depth buffer was never the problem**: measured, it is byte-identical
    on 9.3 and 9.6 (`window.GetZbufferData` spans the test sphere at x=[239..554] on both) and the colour
    buffer matches too — only the READER truncates (9.3 GetZ → [239..554]; 9.6 GetZ → [239..400]). So this is
    NOT the U9 `PreserveDepthBufferOn` fix regressing — that still works on 9.6 (an overlay with
    PreserveDepthBuffer **off** still loses the depth on both versions, which is why that guard stays).
    `window.GetZbufferDataAtPoint` returns the correct value on 9.3 and 9.6 alike, so `_depth` is a straight
    fix, not a version shim, and **no `vtk` upper pin is needed**. Guards:
    `test_pivot_orbits_about_model_past_viewport_width` (behavioural — orbits at a pixel past viewport-width
    and asserts the model point stays pinned under the cursor; verified to FAIL on 9.6 with the old read and
    PASS on 9.3, i.e. it reproduces the real bug) and `test_chrome_stack_keeps_pivot_depth` (now reads through
    the window, since GetZ was the wrong instrument for the invariant it guards). `gui/gui_preview.py:1501` still
    calls `GetZ` and is SAFE — its preview renderer has no sidebar, so its viewport is the full window.
- **`viewers/view_cube.py`** `(2026-07-15)` — the **ViewCube**: a Fusion-style labelled cube in the
  **upper-right** of EVERY viewer window (sim / STL / micrograph) that co-rotates with the camera; click a
  **face / edge / corner** (26 regions) to snap the view. Carries its own RGB axis triad at the cube's
  `(-,-,-)` corner, which is why it **REPLACED the bottom-right `vtkOrientationMarkerWidget` + `vtkAxesActor`**
  the STL + micrograph viewers used to carry (`stl_viewer` / `micrograph_viewer` — two gizmos on one screen
  otherwise; the player never had one, it has the in-scene machine-origin `origin_marker`, which is NOT this
  and stays).
  - **Scene-side, NOT chrome-side — the load-bearing decision.** The obvious home is `ViewerChrome`; that is
    wrong. Embedded there IS no `ViewerChrome` (the child builds `viewer_embed.EmbedChrome`, which draws
    nothing and only exports a control spec to the ttk rail), so a chrome-owned cube would show in native
    fullscreen and **vanish in the embedded panel — the default UI**. Instead the cube is a renderer in the
    child's own window, exactly like the marker it replaced: embedded / native-FS / classic-windowed are all
    the SAME window (U9 flips it offscreen↔borderless without rebuilding the scene), so ONE construction
    covers all three. Only the show/hide **toggle** is chrome-side — one `chrome.button` in the viewer's
    `"View"` section, which fans out to rail + VTK sidebar + fullscreen for free.
  - **The pick is analytic, NOT a VTK widget.** `vtkCameraOrientationWidget` looks like the shortcut and is a
    trap: `vtkSliderWidget`'s pick geometry did not survive the offscreen→native-FS flip (which is why the
    chrome sliders already hand-hit-test). `ray_box` + `cube_region` slab-test the cube in cube space from a
    `DisplayToWorld` ray — **pure matrix math, never `GetZ`/`vtkPicker`** (those read the GL depth buffer and
    need a rendered frame). Side effect: the whole classification is pure functions, so the 26-region
    round-trip is unit-tested with **no window at all**.
  - **Two old scars respected:** the gizmo renderer sits at **layer 1** (so `ViewerChrome`'s hardcoded
    `SetNumberOfLayers(2)` and `viewer_embed._teardown_fs_chrome`'s max-aware recount both stay correct with no
    edit), and it **clears depth** — safe ONLY because its viewport is a small corner box, so the clear is
    scissored there and cannot wipe `install_cad_camera`'s `GetZ` orbit pivot (the full-window version of that
    clear is the U9 bug `PreserveDepthBufferOn` fixed). Hiding sets prop visibility and **never detaches the
    renderer** (churning it re-orphans the layer). Its click observer is priority **9.0** — under the chrome's
    buttons (10), over nav (0.3) — and aborts, so a cube click never also orbits.
  - Labels are `vtkTextActor3D` + `apply_font` (real Helvetica, honouring the sidebar theme's one-typeface
    invariant) at ONE `_fit_scale` sized so the widest ("BOTTOM") fits — **not** `vtkAnnotatedCubeActor`, whose
    `vtkVectorText` is neither Helvetica nor fitted (word labels overflowed ~1.5× the face). Viewport is a
    **fixed `VIEW_PX` box recomputed per render**, not a fraction — the marker's fraction ballooned in
    fullscreen on a big monitor. Default **visible**, session-only (no persistence → no persist-map impact).
    Test: **`test_view_cube.py`** (26-region round-trip, clamp, layer/depth, pick+snap, toggle) +
    `test_viewer_embed.py`'s two child-process guards.

### 2.9 Player / playback sim
`viewers/gcode_player.py` — VTK animator. `main` (`:7614`), `class Player` (`:1324`), `__init__` (`:1355`),
`run` (`:7169`), per-frame `_on_tick` (`:6960`) / `_apply_time` (`:7054`).
- **Clock/timeline:** `_cursor` (`:7042`), `_retime_kinematic` (`:2808`, resample onto `mew_kinematics`),
  `_to_clock`/`_from_clock` (`:2901`/`:2912`), `_reveal_time` (`:3551`, mesh reveal) vs `_lines_reveal_time`
  (`:3574`, commanded-lines reveal at `t-lag`).
- **G-code line ↔ frame map:** `parse_path` records `seg_line` (1-based SOURCE g-code line per segment, arcs
  share their one line; survives `_retime_kinematic` via the resample index). Pure resolvers `line_to_time` /
  `time_to_line` (module level, headless-tested by `test_gcode_lineref.py`). Drives three things: (a) the sidebar
  **readout** — `_refresh_hud` adds the executing line #, its command text (`_cursor_line`) and the live nozzle
  X/Y/Z; (b) **line-by-line arrow scrub** — `_skip` steps the playhead to the prev/next line boundary
  (`_line_step_times`, built in `__init__`) instead of the old fixed 2% jump, and pauses; (c) **"find in
  simulation"** — `--goto-line N` (`_goto_frame`, applied in `run()` after the mesh exists) opens PAUSED at that
  line's frame with head/jet/deposition revealed up to it. **GUI side** (`gui_viewers._make_gcode_textview`,
  shared by the classic tab and the modern shell's `gui_shell` tab): the embedded **G-code text** view now has a
  synced **line-number gutter** and a **"Find in simulation"** button — clicking a line records it
  (`_on_gcode_line_click`), and the button (`_find_in_sim`) relaunches the player via `_player_args(static=True)` +
  `--goto-line`, so in the modern shell it lands in the embedded unified Viewer. Guarded by `test_gcode_gutter.py`.
- **Locked deposition (rides `.depo`):** `_find_depo_cache` (`:5693`), `_build_deposition` (`:5785`),
  `_reveal_deposition` (`:6175`), `_install_depo_mesh` (`:6029`). Preview decimation `_decimate_paths`
  (`:390`)/`_tube_via_filter` (`:444`). **`--depo <path>`** (the GUI's `last_depo`, passed by
  `gui_viewers._player_args`) is checked FIRST by `_find_depo_cache` — without it a **g-code import at a
  preview quality** (no STL; cache next to `-o` in the session folder, g-code elsewhere) found no cache and
  the locked sim silently fell back to the COMMANDED (square) path while the live re-lay rounded. Guarded by
  `test_locked_sim_depo.py` (export→argv→resolution→rounded-geometry chain).
- **Per-vertex colour arrays MUST reach the chunk mappers as 4-component uint8 RGBA.** `_install_depo_mesh`
  shares ONE colour array across ~200 chunk polydata, but `vtkMapper::MapScalars` runs PER MAPPER — sharing
  the object shares no work. A 3-component array (or a scalar+LUT under `SetColorModeToMapScalars`) makes every
  chunk convert the WHOLE mesh's colours into its own RGBA copy and upload its own VBO. So the scaffold colour is
  padded with an opaque alpha and the deviation LUT is applied ONCE at bake time (`_apply_depo_color` then uses
  `SetColorModeToDirectScalars` for both) — **no vertex moved**, framebuffers identical to the pixel.
  The CONTROLLED measurement is same-process, same 11,956,632-cell fixture, one CLI flag different:
  the upload went **6.21 s / 7,321 MB → 1.15 s / 1,750 MB**. (A 30×30×10 "Full mesh" open also went 51.3 s →
  11.5 s, but treat that as indicative only: the 51.3 s came from an earlier session and peaked at 10,435 MB
  against ~5.5 GB free, so an unknown part of it was paging, not mapper cost.) Guarded by
  `test_depo_color_upload.py`. The measuring instruments (`render_cost.py`, `viewer_frame.py`) live in the
  **gitignored** `.perf/`, so they do not travel.
- **One "Mesh detail" drives the STL *and* the Full-mesh view** (Joseph, 2026-08-04). `--mesh-tolerance` now
  reaches `gcode_player` too (`fiber_opts["mesh_tolerance"]` → `_tube_fibers(mesh_tolerance=…)`), so
  `build_deposition_mesh`'s "identical to the STL" promise is finally TRUE — it used to be false in the worse
  direction, the exported artifact thinned and the preview not. Three invariants: it decimates **before** the
  dedupe (that is where `main()` does it — deduping first diverges on any fibre with a coincident point) and
  **before** `_resample_by_time` (so the time fill can restore a span too coarse to reveal smoothly);
  `mesh_tolerance=0` is byte-identical, keeping the exporter's own call and the "Full (largest file)" choice
  untouched; and the GUI freezes it next to `--faces`, which it must travel with, since the tolerance is
  computed FROM the facet count. The decimation is solved ONCE — `write_depo_decimation` stores the kept
  indices in a **`.depo.dec.npz` companion** (`DEPO_DECIM_EXT`) keyed on multiplier + facets + fibre count +
  total points, and `load_depo_decimation` refuses anything else rather than reusing the wrong sampling; a run
  that does not decimate deletes a stale companion. Guarded by `test_mesh_tol_parity.py`, which compares the
  player against `decimate_for_mesh` itself rather than a re-derivation.
  Four ways the two sides could still be sampled apart, all closed 2026-08-04 and each proven RED first:
  **(1)** the companion is dropped by any run that RE-SOLVES, not only by one that does not decimate
  (`drop_depo_decimation`, called before the `--no-mesh` return) — a preview rewrites the `.depo` and no field
  in the key can see a geometry change that left the fibre count and point count alone, which is exactly what a
  different sag multiplier or reset period does. **(2)** The tolerance comes from the EXPORT's facet count, not
  the tube's: the player floors its tube at `DEPO_FACES` so crossovers don't poke, and `facet_error` falls as
  facets rise, so thinning at the floored count kept rings the STL dropped — hence `_tube_fibers(mesh_faces=…)`
  and `stl_faces` in the player. **(3)** `--no-sag` is not thinned on either side; it is the render-what-was-
  commanded reference and `main()` always skipped it, but the player did not (measured: 6194 → 1574 vertices at
  `--arc-segments 256`, against a byte-identical STL). **(4)** The on-demand `--from-depo` STL
  (`gui_viewers._build_exact_stl`) is built at the run's own multiplier via `_fiber_mesh_tol`; sending no flag
  took the CONVERTER's default of 1.0, so "Full (largest file)" silently exported a decimated STL. Because that
  build reads the multiplier back out of the frozen fiber params, `_fiber_params_scaffold` sends
  `--mesh-tolerance` even with the STL step OFF — the one case where the on-demand build is the only STL.
- **In-scene deviation (embedded, U2c):** the "Show deviation" button asks the GUI to compute a sidecar and
  pushes it down (`_on_deviation_msg`). In the **locked sim** it is a **colour scheme baked onto the deposition
  mesh** so it rides the SAME progressive reveal (play/scrub/restart show deviation per print stage): a per-vertex
  scalar from `_deviation_vertex_scalar` (interpolates the sidecar's per-centreline-point deviation over PRINT
  TIME — agnostic to tube/wireframe/exact + preview thinning + cache-vs-fresh mesh), carried through
  `_install_depo_mesh` (named point-data array `deviation`) and switched on by `_apply_depo_color`
  (deviation LUT > per-scaffold RGB > solid, via `SetActiveScalars`). `_apply_deviation` (re)builds the mesh with
  the scalar via `_ensure_depo_deviation`; `_clear_deviation` reverts the colour. **Locked-sim-only** — the
  live-edit sim re-lays geometry incrementally with no matching per-print sidecar, so the button is hidden there
  (`_build_buttons` gates on `not live_adjust`) and a stray GUI push is ignored (`_apply_deviation` early-returns).
  Colour-key = `stl_viewer._deviation_scalar_bar`.
- **View section — "Snap top-down" (`_snap_top`):** a camera preset in the sidebar's `"View"` group
  (`_build_buttons`), the micrograph viewer's snap ported into the unified sim viewer after the viewers merged.
  Points the camera straight down `-Z` (`+Y` up), then reuses `_frame_camera` to re-fit the zoom + clipping on
  the scaffold footprint — so it lands centred and filling the view under the player's parallel projection.
  Fans out to the ttk rail / VTK-maximized / native-fullscreen chrome for free (one `chrome.button`). Guarded by
  `test_viewer_embed.py::player_view_snap_top`. The section also carries the **"Hide/Show view cube"** toggle
  (`_toggle_view_cube`) for the upper-right **ViewCube** (`viewers/view_cube.py`, §2.8) — built in `_build_scene` AFTER the
  chrome but BEFORE `_build_buttons`, so its first hello label is honest (the same backwards-toggle rule the plate
  follows). The cube's TOP face reproduces `_snap_top` exactly (+Y up), and it reuses `_frame_camera` as its
  `reframe`. Guards: `test_viewer_embed.py::player_view_cube_toggle` / `player_view_cube_snaps_on_click`.
  The section ALSO carries the **camera-projection switch** ("Perspective view" / "Orthographic view",
  `vtk_helpers.projection_toggle`), in the sim player AND the STL viewer (`viewers/stl_viewer.py`, whose `"View"`
  section is now unconditional — the projection row is always there, the ViewCube row still needs the gizmo).
  Guard: `test_viewer_embed.py::projection_switch_through_real_session`, which drives it through a REAL
  `EmbedSession` — the switch state comes back from the child's live camera, so the switch moving is evidence
  the camera moved. **Proven RED** by deleting the `view.append` row: "no Perspective row in the hello spec".
- **Live time-march (`--live-adjust`):** `_init_live_relay` (builds `mew_incremental.IncrementalDeposition`),
  `_note_param_change` (O(1) schedule append), `_advance_deposition`, `_rebuild_live_geometry`,
  live knob keybinds `_bump`/`_bump_print_speed`.
  **4-AXIS SINCE 2026-09-04 — a rotary collector marches like any other print.** It used to degrade to the
  locked sim, because the march was flat in two places at once: the parse took no `frame=`, so a tube collapsed
  onto its own axis (265 points not 529, zero circumferential extent), and `mew_incremental` called
  `mew_deposition.laid_fibers` with neither `frame` nor `placement`, so a corrected parse would still have been
  laid flat. Both are supplied now — `_init_live_relay` parses with the scene's own frame and builds the contact
  `placement` exactly as `mew_deposition.make_transform` does (a `CylinderFrame` carrying this run's nozzle gap).
  Three consequences worth knowing before you touch it:
  - **`mew_incremental` works in the UNROLLED (u, v, h) surface frame throughout**, like the batch solver, and
    wraps to world once on the way OUT — `_present` / `_present_rec`, the march's twin of the converter's single
    "the wrap". The wrap DENSIFIES, so `mew_frame.wrap_path(parallel=…)` carries the per-point diameter across it;
    the tube and jet index that positionally, so an unwrapped `ppd` beside a wrapped path misaligns silently.
  - **A tube's `.depo` cannot seed the march.** The converter writes it AFTER the wrap, so it is world-space
    while the march is surface-frame; `_load_live_locked` refuses it outright, and unwrapping is not available
    (the wrap densified, and `to_world` has discarded `v`'s winding number).
  - **So `_use_export_relay` admits a mandrel without a seed**, on the merits rather than for reproducibility:
    constant-param layers take `mew_deposition.laid_fibers` — the engine that builds the STL — and only a layer
    straddling a mid-fibre edit takes `mew_live`, which now carries the placement itself.
  Guards: `test_mandrel_live.py` (the live relay against `laid_fibers` on one tube, controls both ways) and
  `test_player_scene.py::test_live_adjust_marches_a_rotary_collector` (the launch path only).
  **The Curve-quality tier reaches the march too** (`--auto-corner-grid` / `--curve-quality`, emitted by
  `gui_mandrel._fiber_params_mandrel`, resolved through `mew_deposition.auto_corner_grid` so one tier means one
  thing everywhere). Applied only under a rotary frame — a flat march seeds from a `.depo` built without a
  corner grid, so refining a re-derived layer there would disagree with the cached layers beside it; the player
  and `mew_incremental` each drop the flag independently. It deliberately does NOT carry the converter's
  `split_for_sag` / `carry_sag_onto_drawn` seed-grid split: that split is a SPEED optimisation, priced at up to
  13.5 µm of Z by the comment that defines it, so the march sags the refined fibre directly.
  **The march is NOT coarser than the exported tube — an earlier note here said it was, and that was wrong.**
  Measured on a 3-layer helix, sharpest drawn turn: relay out 6.79°, after `apply_sag` 60.75°, after the wrap
  (= the exported STL) 60.34°, the march 60.47°. The two agree; the 60° is the SAG's Z stepping, not lost
  resolution and not mandrel-specific — in XY alone the same fibres read 6.79° before and after the sag, and a
  flat scaffold behaves the same way (17.58° → 75.99° in 3D, 17.58° → 17.58° in XY).
  **What the tier is worth depends entirely on the geometry, and a helix is the wrong thing to measure it on.**
  On a plain 45° helix it is nearly inert — sharpest drawn turn 6.79° at draft *and* standard, 2.43° only at
  fine. On a **re-entrant unit cell** (6 × 5, rib 25°), which is what sharp-turn tube work actually looks like,
  it is the difference between a smooth fibre and a faceted one — 99th-percentile drawn turn, live march:

  | tier | F600 (10 mm/s) | F3000 (50 mm/s) |
  |---|---|---|
  | off | 47.58° | 50.62° |
  | draft | 46.77° | 41.69° |
  | standard | 25.52° | 17.50° |
  | fine | 19.01° | 9.77° |

  Speed is the other half: the relay's grid is uniform in TIME, so printing the same path faster buys fewer
  samples per mm (4008 points at F3000 against 8265 at F600, tier off) — which is why a high-speed cell looked
  coarse. The corner grid is speed-aware refinement, and at `fine` it more than compensates (10967 points).
  **One real gap remains, and it is not the corner grid:** the live tube is built at
  `max(fiber_opts["faces"], DEPO_FACES=12)` facets, and `_fiber_params_mandrel` only emits `--faces` when the
  STL step is ticked — so with "make STL" off, a user who set 32 facets still gets 12 in the sim.
  - **"Live edit" sidebar section (`_build_buttons`, live-adjust only):** an on-screen equivalent of the
    v/V, f/F, h/H, `[`/`]` keybinds — an increase/decrease button pair per knob (Voltage ±0.5 kV, Pressure
    ±1 psi, Temperature ±5 °C, Print speed ×1.25 / ÷1.25) plus **Reset params**, so the sim is fully drivable
    without the keyboard. Each button calls the SAME `_bump`/`_bump_print_speed`/`_reset_params` the keybind
    does with the same delta (button labels are static, not toggles). Added as its own `groups` entry after
    Playback, so it fans out to the ttk rail / VTK-maximized / native-fullscreen chrome for free; the empty
    list in the locked sim is skipped by the section guard. Guarded by `test_viewer_embed.py::live_embed_knobs`
    (asserts "Reset params" is exported in the live hello).
  - **Locked-cache seed (`_load_live_locked`):** loads the SAME `.depo` the locked sim rides and passes it as
    `IncrementalDeposition(locked=…)`. While **unedited** the march commits those cached fibres verbatim (live ==
    locked byte-for-byte) instead of re-deriving via mew_live — whose centreline drifts a sub-diameter from
    mew_deposition's and so makes the live crossovers inconsistent vs locked. None (no cache / params mismatch) →
    re-derive all, as before. See `mew_incremental._layer_locked_clean`.
  - **Edited region uses the EXPORT relay (crossover-crest symmetry):** a layer laid AFTER an edit must re-derive,
    but a CONSTANT-param one (no breakpoint strictly inside it) re-derives via `mew_deposition.laid_fibers` (the
    `.depo`'s own path → symmetric crests) NOT `mew_live` — whose denser sampling drives the sag's crest passes
    (`_round_crests` k_max, rest-clamp) to clamp one crest side into a plateau and leave the other shallow.
    `mew_live` is kept only for a layer straddling a mid-fibre edit. `laid_fibers`/`diameter_by_time` gained a
    `speed_scale` (folded into the lag like `mew_live._tau_at`) + `entry_vs`/`exit_vs` overrides for the per-layer
    call. See `mew_incremental._use_export_relay`/`_relay_layer_export`.
- **Physics jet:** `_recompute_physics` (`:3534`), `_jet_contact` (`:3449`), `_jet_arc_points` (`:3747`,
  a catenary with its vertex at the touchdown — `_catenary_a` at `:1030` solves its one parameter from
  the tip→contact geometry, so the shape carries NO tuning constant since 2026-08), `_jet_radii`
  (`:3657`, Taylor-cone profile), `_set_jet_geometry` (`:3696`, points + radii + the NOZZLE-EXIT PLANE
  that cuts the cone's top face flat — a render cut, the centreline is untouched),
  `_place_head_and_jet` (`:3841`).
- **Machine model:** `_build_machine` (`:4665`), `_place_machine` (`:5113`) → `machine_model.place_scene`.
- **Camera:** `_frame_camera` (`:1978`), `_framing_bounds` (`:1954`).
- Knobs: `DEPO_CHUNK_TRIS=100_000`, `JET_SIDES=16`, `RELAY_DEBOUNCE=0.35`, `PHASE4_LAYER_CAP=1`.
-

### 2.10 String art — **REMOVED 2026-07-01**   *(tombstone; kept so section numbers stay stable)*
The optional image → string-art g-code tab (`stringart_improved.py`, `stringart_tab.py`,
`requirements-stringart.txt`) was deleted at the user's request via its designed removal path:
the GUI's guarded import in `gui_common` stops offering the tab, the installers' `Test-Path`
guards skip the extras, and the modern shell's `StringArtTab is not None` guards hide its page
and Home card. **Those guards are still in the code** — they are the removal path, so leave them
be unless you are deliberately ripping the seam out. Nothing else references string art.

### 2.11 Physics cluster
- **`physics/mew_mandrel.py`** — Neuhaus 2025 Eqs 1–5 (the cylindrical placement deviation)
  **plus the Q10 composition seam**. `ContactPlacement.resolve(du, dv, lag, speed)`
  answers a travel direction in the UNROLLED frame with
  `(trail_mm, off_u, off_v)`: the ALONG-track lag for that heading and the lateral
  PRE-OFFSET that is Eq 5's deviation as a signed vector.
  `mew_deposition.laid_fibers(placement=...)` consumes it, and
  `placement_for_frame(frame)` returns **None** for anything flat, which is the
  off-switch — a `None` placement takes no branch and 3-axis output is
  byte-identical (pinned to a digest captured before the change,
  `test_mandrel_placement.py`). The one thing to understand before touching it:
  Eq 1 and the contact tracker place the contact on the **same** ellipse by two
  different rules, so they compose as a decomposition about the direction of
  travel and NEVER as a sum — adding both doubles the deviation.
  The sign comes from the cylinder's two mirror
  symmetries, so reversing the mandrel mirrors the deviation and Neuhaus's
  alternating line spacing falls out rather than being coded. `mew_physics` was
  not touched: `integrate_contact_path_2nd` still takes one scalar tau.
  **The CONTACT ARC is capped at the tangency bound** (2026-09-01): a taut jet cannot touch farther round
  than the tangent point, so `R_cir ≤ r·arccos(r/(r+H))`. `tangency_arc` + `cap_circumferential_lag` are
  the one helper, called from BOTH places a circumferential radius is formed (`lag_radii_from_jet` and
  `ContactPlacement.resolve`); `deviation()` stays UNCLAMPED so the Fig 3 fixture keeps comparing against
  an unmodified port, and past the bound it turns over by design. The clamp is never silent — it lands in
  `info["lag_arc_saturated"]` and one converter log line naming the arc and the slowest speed that
  reached it. Only R_cir; R_ax is untouched. Inert below ~200 mm/s on a 3 mm mandrel at a 3.5 mm gap, and
  on the whole Fig 3 fixture.
  **ONE placement per scaffold, at the BARE radius** (2026-09-01): Eqs 1–5 are a
  single-layer model, so re-running them on each grown wall is a composition rule
  no mandrel measurement covers — and it leans the wall ≈5.5 µm/layer. The
  deposition branch therefore takes every layer at h = 0 and `at_surface_radius`
  is reached only through the off-by-default hook
  `Calibration.mandrel_per_layer_registration` → `ContactPlacement.
  per_layer_registration` (code, or `mew_export --mandrel-per-layer`). All five
  per-layer terms move together — a partial freeze leans the other way. The
  jet's SPEED still uses the wall radius (`_metric_path`); that is kinematics.
- **`physics/mew_lag_fit.py`** — **the micrograph calibration**: one printed tube → the two mandrel lag
  radii → the per-rig amplitude `k`. Input is rows of (winding angle, WIDE gap, NARROW gap) read off the
  micrograph viewer; because the lines alternate direction the gaps alternate `s ± 2D`, so
  `D = (wide − narrow)/4` and the spacing cancels (it is kept as a scale CHECK). Numpy only — nothing
  shipped imports scipy, so the 2-parameter least squares and its seed scan are written out. `S` is HELD
  at `r + 0.75H` (degenerate with the amplitude) and the tangency cap is NOT applied while fitting
  (`deviation()` is the comparator). The anisotropy IS bounded to `R_cir < R_ax`, the model's own
  invariant, so a mis-transcribed row reads as one instead of as a calibration. Surfaced as the Physics
  page's Guided-fit section (`gui_calibration._build_fit_mandrel_lag`); Apply writes `k` to the
  calibration profile and the two radii to the Mandrel page. Comparator: `test_mew_lag_fit.py` reproduces
  `run_stage2.py`'s published scipy fit of Neuhaus's own images to −0.00%
- **`five_axis/mew_frame.py`** — the deposition FRAME (`PlanarFrame` = the no-op base class,
  `CylinderFrame` = a rotating mandrel). `parse_gcode(frame=...)` reads the 4th
  axis through it and returns points already UNROLLED, which is the frame the
  placement and the sag solver both work in.
Dependency order: `mew_field` + `mew_kinematics` + `mew_coil_wave` (pure leaves) →
`mew_physics` (hub) → `mew_deposition` + `mew_live` + `mew_calibration` → `mew_export`
(atop deposition + calibration) + `mew_incremental` (atop live).
- **`physics/mew_coil_wave.py`** — the shared **below-CTS coiling-WAVE indicator** reshape:
  `coil_wave_reshape(xyz, times, amp, wavelength)` adds a fixed lateral in-XY sine
  (`amp·sin(2π·s/λ)`, Z kept; resampled ≥24 pts/λ; times interpolated by commanded
  arc-length so the reveal still tracks the head; tapered to 0 over the first/last
  half-λ so serpentine passes meet with no lateral seam). A DELIBERATELY simple visual
  marker that CTS was reached — distinct from the physical sewing-machine `coil` model
  (`_coil_excursion`, dormant): amplitude/wavelength are USER knobs, not derived, so it
  conserves neither arc length nor a coil clock. One module so the batch
  (`mew_deposition`) and live (`mew_live`) engines apply a byte-identical wave and can't
  drift. Pure numpy; imported by both engines (and thus `mew_incremental`); nothing
  imports it back. Wired through: `laid_fibers`/`make_transform` `coil_wave_amp/len`
  (below-CTS branch → locked sim, `.depo`, STL, preview), `relay_fibers`/`_relay_one`
  (per-fibre gate mirroring `laid_fibers` → live sim byte-identical to locked; the
  gate reads the schedule's **per-interval** speed scale, length-weighted, so a live
  print-speed cut that crosses CTS PARTWAY through the fibre being laid still waves
  it — reading only the fibre-start scale left the actively-laid fibre commanded, the
  "physics stops below CTS in the live sim" bug; a constant/unedited schedule ⇒ every
  interval equals the start scale ⇒ byte-identical to the locked path),
  `IncrementalDeposition` (both re-derive branches). CLI: `mew_export --coil-wave-amp/
  --coil-wave-len` + `gcode_player --coil-wave-amp/--coil-wave-len`. GUI: `sim_coil_wave`
  (BoolVar, default ON) + `sim_coil_wave_amp` (0.1) / `sim_coil_wave_len` (0.3) on the
  Physics page (`gui_sim`), persisted as `sim.coil_wave*` (physics preset kind, in
  `_PHYSICS_PROCESS_KEYS`), forwarded by `_sim_play_args` (toggle-off ⇒ amp 0, the
  `_sag_reset_arg` idiom), read by the preview's `_pv_snapshot_options` (gated on
  `sim_physics_deposit`, like the export). Guarded by **`test_mew_coil_wave.py`**
  (13 tests: reshape geometry, disabled/degenerate passthrough, below-CTS waves +
  above-CTS no-op through both engines, live == locked, and a live mid-fibre speed
  cut below CTS waves the actively-laid fibre). amp ≤ 0 = off ⇒ byte-identical
  to the straight passthrough (the `coil`/`coil_wave` defaults keep every existing test
  green).
- **`physics/mew_physics.py`** — `class MEWModel` (`:539`), `solve` (`:1322`, the one entry → `JetState`),
  `fiber_diameter` (`:850`, `d=√(4Q/πv)`), `flow_rate` (`:815`, Hagen-Poiseuille), `critical_speed`
  (`:890`, CTS), `lag_angle` (`:946`), `bridge_sag_depth` (`:1019`, the grounded physical droop law) +
  `make_sag_law` (`:1106`, wraps it into the `_SAG_LAW` seam), cone-jet `jet_radius`/`jet_speed_at`
  (`:1238`/`:1274`), `build_field` (`:1304`). **`class Calibration` (`:112`, the single fit seam)**.
  Integrators `integrate_contact_path` (`:1376`, 1st-order) / `integrate_contact_path_2nd` (`:1429`, used;
  ω pinned to 2ζ/τ so the steady trail = v·τ = lag_distance for ANY ζ — the 2nd-order model reduces to
  Hrynevich's steady lag, ζ tunes only corner transients). Bessel/coil machinery (`invert_j0_unit` `:1729`+).
  `class JetState` (`:498`, `straight_fibre` trust flag `:526`).
  **Lag law (LAG-POWER-FORM, 2026-08-22 — this REPLACED LAG-REANCHOR-HRYNEVICH's saturating
  exponential):** `lag_distance = H·tan(θ(ε))` with `θ = θ_cts + arctan(A·(1 − ε^−b))`, so with the floor
  pinned to 0 the lag is `H·A·(1 − ε^−b)` — Hrynevich 2020 Eq.4 (`L = 10.02·(1 − SR^−0.304)` mm at gap
  3.5 mm) with the **gap divided out**. `A = 10.02/3.5 = 2.8629` is stored as its angle
  `lag_ceiling_angle_deg = 70.745620335°` (the digits are load-bearing) and `lag_saturation_exp = 0.304`
  is Eq.4's own exponent, so agreement is **exact at every speed ratio**, not a fit to a window.
  **The per-rig lag inputs are CTS and `lag_amplitude` (k, default 1)** — k multiplies the whole law in
  LAG space (`lag = k·H·(base + (A − base)·(1 − ε^−b))`), applied inside `lag_angle` so the returned
  chord angle is the angle actually laid, and carried to the ray tracker by `MEWModel.lag_ray_constants()`
  — which exists so k cannot reach one and not the other (`mew_deposition` and `mew_live` call it rather
  than repeating the expression). k = 1 is Hrynevich's own amplitude and is byte-identical. **The rule:
  k carries the per-rig AMPLITUDE; A and b stay literature constants unless someone is fitting the curve
  SHAPE from their own lag sweep** — moving both for one fit double-counts. On a mandrel k is Eq 2's k in
  `R_ax = k·L(H, ε)`. **At 3.5 mm this model IS Eq.4** — the arctan/tan pair is inert and
  nothing downstream reads the raw angle (`mew_deposition` / `mew_live` consume `.lag_distance` only) —
  so the SHAPE is borrowed and only the FACTORISATION is ours. Write it that way. The lag still
  **saturates**, at `A·H = 2.863·H`, so it cannot diverge —
  the ceiling was raised (from `tan(48.94°) = 1.148·H`), not removed, and it is Hrynevich's own asymptote
  rather than one we fitted. **Why it changed:** the old cap flatlined `mew_mandrel.deviation` at 107 µm
  above 50 mm/s against a measured 135/148/155 (23.0% RMS vs 0.7%), and it was a SHAPE error no refit
  removes. **The two Calibration fields were RENAMED, not revalued** — a stale profile carrying
  `lag_angle_max_deg = 48.94` would otherwise be applied as `A = 1.148` and silently reinstate the cap.
  See `mew_physics` docstring pt 4 and its Calibration lag_* block.
- **`physics/mew_field.py`** — `class AnalyticField` (`:117`, uniform gap + image-charge sources), `Field`
  protocol (`:59`), `PointCharge` (`:78`). Default = uniform V/H; tip/repulsion sources off.
- **`physics/mew_kinematics.py`** — `plan_trajectory` (`:269`, the entry), `class Trajectory` (`:158`),
  `speed_at`, the hook diameter/lag consumers read, and `resample`. `MachineConfig`
  (`:58`)/`MACHINE` (`:78`). OFF when `max_accel` None/≤0 (byte-identical naive timing).
  The 4-axis ROTARY limit (`--rotary-accel`, deg/s², 0 = none): `plan_trajectory(seg_accel=)`,
  built by `axis_seg_accels` (`:467`, the player, off the file's X/A words) or
  `chart_seg_accels` (`:495`, the export and live re-lay, off the mandrel chart).
- **`physics/mew_deposition.py`** — the locked/`.depo` (batch) deposition path. `laid_fibers` (`:889`, reshape
  all fibres to the touchdown path), `_lay_one`, `diameter_by_time`, **`make_transform` (wraps into the
  `_FIBER_TRANSFORM` seam)**, coil `_assemble_coil_offsets`, `junction_speeds`. Flags
  `coil`/`dynamic_diameter`/`max_accel` all default off. `laid_fibers`/`diameter_by_time` also take
  `speed_scale` (1.0 default, byte-identical; folded into the lag/diameter like `mew_live._tau_at`) +
  `entry_vs`/`exit_vs` overrides, so the live march's edited-region branch can relay one layer in isolation
  and still match the whole-set export.
  **CONTINUOUS JET — `travels=` + `simulate_travel=` (opt-in, default off → byte-identical, proven
  against pristine HEAD on a real trial plate at max_accel 0 AND 2000, worst delta 0.000e+00).** Off,
  every fibre is laid in ISOLATION: the tracker ignites at rest under the nozzle
  (`physics/mew_physics.py` `integrate_contact_path_2nd` `contact[0]=points[0]`, `w=0`) so lag builds from zero
  at every path start, and G0s deposit nothing. That is wrong for MEW, where the jet NEVER stops: it
  trails through the travel and enters the next fibre already carrying lag state (magnitude AND
  direction). On, feed `travels` from `parse_gcode(with_travels=True)` (§2.3) and the tracker integrates
  continuously across the whole program. Pieces: `_program_segments` (program-order fibre/travel list),
  `_segs_contiguous` (exact endpoint+clock identity → the jet re-ignites at rest wherever the head
  cannot have arrived continuously), `_chain_effective_speeds` (with kinematics ON, plans each whole
  contiguous CHAIN as ONE polyline so `plan_trajectory` applies its own junction-deviation/centripetal
  corner rules at fibre↔travel junctions — planning per segment would fake a stop at every junction and
  blunt the entry state the mode exists to model), `_end_pin_state` (below-CTS passthrough still advances
  the state: lag is 0 there, so the contact rides the nozzle). Seams it rides:
  `integrate_contact_path_2nd(init=, with_state=)` and `_lay_one(init=, settle=, with_state=)`.
  Three invariants, all guarded by `test_mew_travel.py`:
  (1) `out_paths` stays **index-aligned** to `paths`; laid travels go in `info["travel_paths"]` /
  `["travel_ptimes"]` (aligned, entry i = travel INTO fibre i, or None).
  (2) The **settle tail moves**: only the LAST segment of a chain settles — mid-program the head travels
  on rather than stopping, so that material lands on the TRAVEL fibre. A fibre therefore ends ~one lag
  distance SHORT of its last commanded point. This is what makes the touchdown continuous across a G0
  instead of teleporting (measured gap on a real plate: 0.000e+00 mm over all 29 transitions).
  (3) The **LEADING travel IS deposited** (changed 2026-07-21) — the origin→first-part approach is real
  fibre in continuous MEW (the jet is already flowing on it), so `merge_travels` / `_program_segments` no
  longer skip index 0. It ignites at rest, so the first fibre now inherits the lag that approach built.
  **Caveat:** its start is the parser's ASSUMED origin (0,0,0); the g-code does not record the head's true
  pre-program position (the rig's `SET_SLIDE_XY` macro actually parks it at X3 Y3), so the very first few
  mm of the drawn approach can be slightly off. For generator output the assumed origin IS the g-code
  frame, so the approach otherwise renders as the datum-to-part move seen on the rig.
  **Scope finding (2026-07-16, for the paper's jet-lag campaign):** the inherited entry state decays as
  the critically-damped transient, so it reshapes only the entry region. Measured on the real CTS-480
  plates with the paper's own tail-free leg estimator: the LEFT leg (~6.3 τ downstream of entry) changes
  by exactly 0.000°, the RIGHT leg (~1.3 τ downstream) by up to ~3°, whole-square ≲1° — against a 3–9°
  unexplained rotation deficit. It explains the entry/exit TAILS, not the rotation.
- **`physics/mew_export.py`** — `main` (`:38`): build `MEWModel` (via `mew_calibration.build_model`),
  `set_fiber_transform` (called at `:259`, defined `engine/gcode_to_fibers.py:162`) + optional `set_sag_law` (called at `:266`, defined `engine/gcode_to_fibers.py:1850`), dispatch to
  `scaffold_gcode.main`/`gcode_to_fibers.main`, restore seams. CLI
  `--standoff/--voltage/--pressure/--temperature/--calibration/--coil/--dynamic-diameter/--physical-sag/--physics-deposit/--max-accel/--square-corner-velocity`.
- **`physics/mew_calibration.py`** — the per-rig CALIBRATION PROFILE seam (turns `Calibration`/`Material`/`Nozzle`
  into a user-editable, shareable object). `CALIBRATION_FIELDS` (the registry that drives the GUI — one
  `FieldSpec` per knob, defaults pulled LIVE from the dataclasses, required/advanced groups, descriptions),
  `default_profile`/`models_from_profile`/`load_profile`, **`build_model` (the workers' entry: profile →
  `MEWModel`; empty profile == factory defaults)**, and the guided-fit estimators (`fit_viscosity_ref`,
  `fit_activation_energy`, `fit_sag_coeff`, `fit_fusion_overlap`/`fit_fusion_rows`, `cts_from_diameter`,
  `coil_radius_from_peak_to_peak` — all reuse the real `mew_physics` laws). Excludes the dormant
  viscous-creep block + the gap (gap = `sim_standoff`, passed as `--standoff`). Guarded by `test_mew_calibration.py`.
  **Gated fields** (2026-07-16, first user = `cal.fusion_wall_overlap`): `FieldSpec.gate_label` non-empty ⇒
  `_render_cal_fields` puts the field behind its own tickbox (`gate_key` = `<key>.on`, a GUI-only BooleanVar
  living in `cal_vars`, so it persists / kinds / reaches BOTH shells like a real field while
  `models_from_profile` skips it as an unknown key — no dataclass field stands behind it, and
  `FieldSpec.default` would raise if one had to). Unticked ⇒ `_calibration_profile_snapshot` emits BLANK for
  the field whatever the entry remembers ⇒ `_coerce` → `_SKIP` ⇒ the dataclass default (= "inherit"). That
  sentinel is folded GUI-side, the `_sag_reset_arg` idiom: the worker's own default can never silently
  re-enable a split the user unticked, and unticking never loses the typed value. Requires `optional=True`
  (blank has to mean something). `_sync_cal_gates` greys the entry and is re-run from
  `_after_calibration_preset_applied` — a preset carries its tickbox, and a bulk preset set never fires the
  tickbox's own `command`.
- **`physics/mew_live.py`** — the live (real-time) deposition path. `relay_fibers` (`:249`, time-varying-τ
  re-lay), `class ParamSchedule` (`:96`, `set_at` `:112`), `Params` (`:93`, incl. `speed_scale`),
  `diameter_schedule` (`:841`). Below-CTS coil gated per sub-interval. **Recurrently drifts vs
  `mew_deposition` — diff them first** on "locked-wrong/live-right" bugs.
- **`physics/mew_incremental.py`** — Phase-4 time-march (drives `--live-adjust`). `class IncrementalDeposition`:
  `advance_to` (lay layers complete by t), `set_params` (O(1) live edit), `_truncate_to` (scrub-back),
  `_sag_layer` (via `_SagState`), `_freeze_tip_up_to`, `committed_fibers`, `tip_fibers`. Relay branch:
  `_relay_layer` → `_use_export_relay` (locked seed + constant-param layer → EXPORT relay
  `_relay_layer_export` for symmetric crests; else `mew_live`), shared `_repr_diams`. Accumulated == batch
  `apply_sag` (guarded by `test_mew_incremental.py` — run it after any `apply_sag`/`_SagState` change).
- **`gui/gui_sim.py`** `_SimMixin` — the Printhead-sim panel. `_build_sim_controls` (`:38`, 4 MEW knobs +
  5 checkboxes), `_build_sim_tab` (`:333`), `_build_sim_extra` (`:340`, the real-time-adjust launcher),
  `_solve_jet` (`:457`), `_refresh_scaffold_phys_diam` (`:576`, writes predicted `d` into the scaffold's
  `diameter` var), **arg builders** `_export_prefix` (`:645`), `_sim_play_args` (`:664`, → player AND
  export), `_live_diam_args` (`:716`), `_sim_cone_args` (`:725`, `--no-cone`, player-only). Sim vars
  `sim_standoff`/`sim_voltage`/`sim_pressure`(psi)/`sim_temperature`/`sim_taylor_cone`(default on)/etc.
  `_sim_persist_entries` (`:396`). `PSI_TO_PA=6894.76`. `_solve_jet_with_calibration` builds the readout
  model from the Calibration tab (`_calibration_models` hook); `_sim_play_args` appends `_calibration_args`.
- **`gui/gui_calibration.py`** `_CalibrationMixin` — the calibration UI; its module docstring carries the
  card → field → fit table. In the **classic (developer) build** it is the **"Calibration"** top-level tab
  (`_build_calibration_tab` `:142`), a sub-notebook of **Calibrate** (the card stack `_cal_build_cards`
  `:1519`, one card per calibrated quantity, the same stack the modern Calibration page builds — §2.15),
  **Rig & material** (`_build_calibration_params` `:177` — registry-driven fields, short Required list +
  collapsible Advanced via `_toggle_calibration_advanced` `:305`) and **Physics presets**
  (`_build_calibration_presets_tab` `:449` — the third, independent preset kind, id `"calibration"`, over
  `CALIBRATION_PRESETS_DIR` = `presets/physics/`). In the **modern shell** the fields are a section of the
  Physics page (`gui_pages._build_calibration_section`, §2.12) and the cards are the Calibration page.
  Either way a no-op degrade to "no tab/section" when the physics modules are missing (same posture as
  `gui_sim`). The three rig protocols `physics/mew_physics.py` cites by bare name (`LAG_CALIBRATION_EXPERIMENT.md`,
  `SAG_EXPONENT_EXPERIMENT.md`, `ZETA_FALSIFICATION_EXPERIMENT.md`) are **not in this repo**: they
  are kept with the accompanying paper.
  Worker/readout hooks (override the `_CoreMixin` no-ops): `_calibration_args` (writes
  `SESSION_DIR/calibration.json`, returns `--calibration <path>`), `_calibration_models` (in-process readout model),
  `_calibration_model` (a `MEWModel` for the guided fits, gap from `sim_standoff`), `_calibration_profile_snapshot`,
  `_calibration_persist_entries`/`_persist_keys`/`_grid_labels`, `_after_calibration_preset_applied` (refreshes the
  Printhead-sim readout). `cal_vars` = one StringVar per registry field. `PSI_TO_PA=6894.76` (matches `gui_sim`).

#### 2.11a User-replaced laws — `physics/mew_lawlang.py` + `gui/gui_lawedit.py`
A user can replace six scalar laws — `viscosity`, `flow`, `diameter`, `cts`, `sag`, `fusion` — with
their own expression. `viscosity` → `flow` → `diameter` is a CHAIN through this seam, so replacing
the melt viscosity moves the flow rate and the fibre diameter with it; `eta` is passed INTO the flow
law rather than recomputed there, so the two cannot disagree. A shear-thinning (Cross/Carreau)
viscosity in place of Arrhenius is the substitution the seam exists for — a change of FORM no
exponent hook could express. **`physics/mew_lawlang.py`** is the language: `ast.parse` for the tree, then `_compile`
walks it against a **node-type whitelist** and returns a tree of closures. **No `eval`, no
`compile` of user text** — Lite serves the same engine over HTTP, so an expression can arrive
from the network. `LAWS` (a `LawSpec` each) carries the law's arguments, its result, and the
**shipped form transcribed into the language**; `test_law_lang.py` evaluates that transcription
against the `mew_physics` method over a sweep, so the registry cannot drift from the code.
**`lag_angle` is deliberately absent** and a test asserts it stays absent — the tracker
integrates the law's closed-form INVERSE (`physics/mew_physics.py:1543`), so a typed form would leave
the STL following one law and the card showing another.

`active_source(key, constants)` is the form the EDITOR seeds from: the shipped string with
every hook whose exponent is 0 folded out, so the text matches the equation typeset on the
card. Two rewrites only — `a if C else b` where C is decidable from calibration constants
alone, and `x ** n` where n is exactly 0. `x ** 1` is **not** folded (it is 1 in the exponent,
not in the value, and folding it would delete a coefficient name the user can edit).
`is_shipped` accepts **both** spellings, on the PARSED shape (`normalize`), so re-indenting the
box is not an edit; without that, opening the panel and pressing Apply while changing nothing would
store the folded text as an override and freeze every folded-away hook out of the model
permanently. It is the second opinion, not the first: `gui_lawedit.apply` compares against the text
the panel actually SEEDED, because `is_shipped` re-folds against constants that come back empty
whenever a rig field will not parse — which is how an untouched Apply came to rewrite a card's
typeset equation in front of the user (`test_ui_shell.physics_law_editor_seeds_the_form_that_is_running`
block 5). The guard-laden `shipped` string is **not shown anywhere in the
UI** — a hook that is off is reached by fitting its exponent on the card, at which point the fold
puts its term back in the box on its own.

**One vocabulary, the SAME on every card.** `QUANTITIES` is it; every law argument must be in
it, `CONTEXT_NAMES` is built from it, and `test_law_lang.one_quantity_has_one_name_everywhere`
fails on a second spelling. `speed` is never `v`, `voltage` is never `V`, and the absolute
viscosity `eta` (Pa·s) is never spelled like the dimensionless ratio `visc_ratio`. That case also
CALLS every override site through a law naming all of its own arguments, so the keywords
`mew_physics` passes cannot drift from the registry.

The reference table lists **only what an equation on the page NAMES**: `constant_names()` reads the
set out of the shipped law strings plus a hand-listed `LAG_CONSTANTS` (the lag card is typeset but not
editable, so it has no `shipped` to read). Built from the whole `CALIBRATION_FIELDS` registry instead,
it offered `stable_window` (a regime LABEL in the readout) and the retired `contact_damping_zeta` as
things to write into a law. `NO_FIELD_DESC` covers the one constant with no `FieldSpec` behind it
(`sag_diam_ref`). Guard: `test_law_lang.the_reference_table_carries_no_dead_constants`.

Since **2026-09-15** every quantity is usable in EVERY law, which is what lets the editor show one
table instead of a per-card list. `_law` admits all of `QUANTITIES` at compile time and
`MEWModel._law_scope` **derives** the ones a law is not handed (`eta`, `visc_ratio`, `Q`, `d`,
`CTS`) from the process context, memoised per context and cleared by `set_context`. It derives
only names the expression MENTIONS (`compile_law` records them on `run.uses`), so `c * span**p`
— evaluated once per span — pays nothing. `DERIVED_BY` maps each derived name to every law that
would have to run to produce it; named inside one of those it is a cycle and stays absent, which
reads as "no value at this operating point". A `_deriving` set catches the cycles a USER law can
invent that the table cannot know about. `mult` was removed from the sag law entirely with the GUI
box that set it — see §2.3.

**`SYMBOLS`: the constants are named as the CARD prints them.** `c` is `sag_coeff`, `p` is
`sag_span_exp`, `Ea` is `activation_energy`, and `eta_ref` / `T_ref` / `r` / `L` / `phi_junction` /
`phi_wall` / `phi_max` / `CTS_ref` likewise; the shipped strings are written in those symbols, so the
box under `depth = c span^p` seeds `c * span**p`. The **jet-lag four** are in there too (`k`, `b`,
`theta_ceiling`, `theta_CTS`) although that law is not editable — the card typesets them, so the
table has to answer to the same letters or the card and the table describe one rig in two alphabets.
`LawSpec.args` descriptions are **overwritten from `QUANTITIES`** at import, so one quantity cannot be
worded two ways again. `_law_constants` puts the value under BOTH spellings and
`_law_names` admits both, so nothing written against a field name breaks. Only constants a card
actually typesets get a symbol — inventing one for `sag_diam_exp` would be notation nobody writes. `LawSpec.symbol` is what the law produces
(`d`, `CTS`, `depth`, `sink`, `eta`, `Q`) — the typeset line and a two-law card's panel headers use
it, so a registry key never reaches the page. Constants: `pi`, `e`, `R` (molar gas constant) and
`T_abs` = 273.15, so kelvin is `temperature + T_abs`; the shipped Hagen-Poiseuille carries **no**
unit factors, because with the bore in mm, pressure in Pa and eta in Pa·s the conversions cancel
exactly (the drift guard checks that against `mew_physics`, which does it the long way round).

Storage: `Calibration.law_source` / `law_consts`, both `default_factory=dict` and **empty by
default** — empty means the shipped method runs and `mew_lawlang` is never imported, which
matters because `bridge_sag_depth` is called once per span. They ride the profile JSON under
`mew_calibration.LAW_SOURCE_KEY` / `LAW_CONSTS_KEY`, **not** as `FieldSpec`s (`CALIBRATION_FIELDS`
is a flat float table `lite/lite_params.py` mirrors key-for-key), and are listed in
`DORMANT_CALIBRATION_ATTRS`. **A broken expression RAISES where it is used** — a silent fall
back to the shipped law would make "my expression is wrong" and "my expression has no effect"
look identical. Two dict fields make the frozen `Calibration` **unhashable**.

**`gui/gui_lawedit.py`** is the front end, MODERN UI ONLY and plain functions (no mixin, so the
classic build and `ModernApp`'s MRO are untouched): `build(app, parent, key, ...)` hangs a
**"Try a different form"** disclosure on `card._law_host`. The **Fiber diameter** card typesets
TWO laws — `d = sqrt(4Q/pi v)` over `Q = pi r^4 dP / (8 eta L)` — and carries two panels
(`_phys_law_editors_multi`), each headed by its symbol; that card's strip is one multi-row drawing,
so an `on_applied` callback recomposes EVERY row rather than letting one editor redraw the strip
from its own expression and wipe the other. **The flow law moved there from Material and machine
parameters on 2026-09-15** (`LawSpec.title` is the card, and `flow`'s says Fiber diameter): its
constants r and L are still material fields, but the chain that sets the fibre thickness is
Q-then-d, and splitting it sent a reader asking "what sets my fibre thickness" to the wrong card for
half the answer. Material keeps eta(T) and one panel.
`constants()` tolerates a missing `sim_standoff` (the sim vars are created after the
Physics cards; without that fallback every card seeds with the raw shipped string on a cold
start). `_ThemedText` takes the palette itself because `_recolor_classic` skips every
`tk.Text` by design. The panel is **born SHUT** (`open=False`) and its body holds ONE line of prose — the law's
physics note, the open-source stance and what Apply/Check do all live in the header's info dot, because
nine cards each printing four paragraphs is what made the page unreadable. It re-seeds on BOTH the
disclosure's open callback and `<Map>`; either alone leaves one of the two open orders unseeded.
Check evaluates at the live operating point; **Apply is explicit**, and applying an
UNTOUCHED box stores nothing and leaves the card's typeset equation exactly as it was — the status
line says which of the two happened. `operating_point(app)` is the one place the live quantities
are read; `_probe` narrows it to a law's own arguments and the rest is derived inside `_law_scope`,
the same path a real run takes. `vocabulary_rows(app)` builds the **"Names you can use"** table:
every quantity, then every constant an equation names (`mew_lawlang.constant_names`) under its card
symbol with its field name beside it, then the maths constants — name / what it is / **value now**,
IDENTICAL on every card and re-read whenever the panel becomes visible. The three columns are FIXED
width and nothing stretches: an opened module body is as wide as the window, and a stretching middle
column put the value a thousand pixels from the name it belongs to. It replaced a per-card list built from `spec.args`, where `eta` appeared on the
Material card and not on Fiber diameter — a difference in plumbing that read as a statement about
the physics.
Lite has **no** law keys, so a profile carrying one loads into Lite with the laws dropped —
a known parity gap.

#### 2.11b Fiber target — `physics/mew_target.py` + `gui/gui_fiber_target.py`
The inverse of the forward model: type a fibre diameter (and optionally a jet lag) and get the
process settings that make it. **`physics/mew_target.py`** is pure (no Tk) and holds NO physics of its
own. `TargetModel` builds one `MEWModel` per needle bore (`dataclasses.replace(nozzle,
inner_radius=)`) and log-bisects `MEWModel.solve` for each knob (`pressure_for`, `speed_for`,
`temperature_for`, `speed_for_lag`, the joint `lag_and_diameter`). Bisection rather than a closed
form is deliberate: a `mew_lawlang` user law replaces the closed form, and the inversion must follow
it. Every answer is put back through `solve` before it is returned, so a solved point gives the
target by construction (`TOLERANCE`). `curve` / `grid` feed the map. `NEEDLE_GAUGES` are NOMINAL
regular-wall bores, not measured ones. `Ranges.why_outside` names the knob a setting breaks.

**`gui/gui_fiber_target.py`** is the panel, ONE class (`FiberTargetPanel`) drawn in two places: the
Physics page card `target` (`_ft_build_physics_card`, called from `gui/gui_pages.py` after the Fiber
diameter card; summary `_summary_phys_target`) and the G-code window's ribbon tool Modify ▸ Fiber
target, on the 3-axis set (`gui/gui_gcode_design.py`) and the 4-axis set (`gui/gui_gcode_collector.py`),
opening `_ft_open_cad` in the floating edit box. Solve for moves one knob (pressure, speed, or
temperature); the other two are HELD at the value typed in their Hold field, or where they are now
when it is blank (`_ft_holds`, `_ft_solve`). A click on a chart line types its speed and
temperature into those fields and solves the pressure, so no hidden picked state exists. The map: a
speed × pressure field of the resulting diameter for the chosen needle, diverging around the
target, hatched where the jet coils (below CTS) or where the printer can't go. Temperature is greyed
until viscosity AND Ea are no longer "default" (`_cal_origin`). Apply writes `sim_pressure` (psi),
temperature / bore if changed, and speed to the chosen scaffolds of the G-code window's own design;
Put back restores them. Every scaffold in scope gets the answer's speed. On a mandrel each
scaffold is a tube and its Speed row is the deposition-surface speed the laws take, so the panel
solves it unchanged; fixed Feeds on a split-feed flavor offer no answer and the omega speed policy
adds a note (`_ft_mandrel_speed`). The G-code window's undo step carries the process settings a
physics-driven diameter came from (`gui_gcode_design._PHYS_VARS`), so Undo of an Apply restores
both. "Out of reach" is judged against the printer's **Process limits**
(pressure, print speed, melt temperature), rows on Setup > Printer model under the acceleration
limits (`_build_machine_process_limits`, called from `gui_pages._build_machine_grid`). Persist keys
`machine.range_<knob>_<min|max>` join the persist map (`gui_presets`) and the MACHINE preset's keys
(`gui_buildplate._machine_persist_keys`, via `_ft_machine_keys`). The panel names them and its
Change… opens the card scrolled to them. The map sizes itself to the room left in its scrolling
host (`_TargetMap.fit`), so it is on screen when the panel opens at 1366x768. Mixed in by
`ModernApp` only.

**The G-code window's diameter follows the physics** (`gui_sim._phys_page_attach` /
`_refresh_phys_diam_of`, called from both `gce_scaf_vars` creation points in
`gui/gui_gcode_twodesigns.py`). Before, a pressure change moved the designer's diameter but the
window's own design kept the old value under a greyed "From physics" field. `_manual_page_diam`
stashes the window's typed diameters across a physics on/off toggle, as `_manual_diam` does for the
designer's.

### 2.12 Modern UI shell
The DEFAULT UI since the 2026-07 restructure: one top row wrapped around the SAME
App — same Tk vars, validators, arg-builders, presets, workers. The classic UI is untouched
and remains the "developer build" (`gui/gcode_to_fibers_gui.py`, or
`mewracle_gui.py --classic`); `MEWracle.bat` launches the modern UI.

Since the 2026-07-01 DEEP REDESIGN (U6): **no inner notebooks anywhere in the modern UI**
— every page is one scrolling surface built from overline section headers + a contiguous
stack of collapsible `Disclosure` rows; the scaffolds are a numbered, wrapping `PillRail` (a
selector, not tabs). The classic UI still uses the notebooks.

Since **2026-09-14 (U10)** the **Scaffold designer** is the exception and the intended
direction for the rest: its sections are a two-column **`ModuleGrid` of tiles** that open
over the whole form pane, so the page reads as a set of swappable modules rather than one
long column.

Since **2026-09-16 (U12)** the **Machine & collector** page is a module grid too
(`gui_pages._build_machine_grid`, two columns), under **the Scaffold designer's own
3/4/5-axis toggle — the same `s_collector` variable**, drawn a second time by
`_finish_machine_page` (it waits for the scaffold page, which creates the var).
The classic tab's "a rotary stage is installed" tick and kind radios are **not
shown**: `_machine_mode_sync` sets `rotary_on` / `rotary_kind` from the mode after
every mode change and after every machine-preset load (the `_apply_preset_for_kind`
override), so the toggle wins over a preset's saved stage and a 5-axis rig loaded
for a flat print writes a plain 3-axis profile. No persist key moved.

Since the setup-flow rework it is six cards (G-code was added later), in the
order a printer is set up, and "Stage rides" is gone (`_ROTARY_ROLE = "bed"`):

| card (key) | 3 axis | 4 axis | 5 axis |
|---|---|---|---|
| Instructions (`setup`) | the checklist, `_render_setup_tile` `gui/gui_pages.py:1904` | same | same |
| Machine (`machine`) | `_build_machine_stl_fields` `gui/gui_buildplate.py:567` (travel-range rows built into an ungridded frame: not editable here, still persisted) | + `_build_rotary_holder_hint`, `_build_rotary_mount_fields` (letter) | + `_build_rotary_joint_rows` `:992` (body, letter, turns about) |
| Calibrate (`calibrate`) | nozzle tip + bed top face, each Pick in 3D… with "Enter by hand" collapsed | tip + mandrel axis | tip + joint 1/2 axis |
| Collector (`collector`) | `_build_plate_fields` `:347` + `_build_bed_offset_row` `:720`; G-code zero face/corner | `_build_rotary_rod_row` `:1158` + offset; zero along the rod / radially | `_build_rotary_seat_row` `:978`; `_build_rotary_home_row` `:1085` |
| Presets (`presets`, wide) | unchanged | | |

**Statuses live only in the Instructions checklist.** `machine/machine_setup.py` (no Tk)
turns the page's vars, keyed by persist key, into four steps of items with one
state each (`setup_steps` `:101`); `gui_pages._refresh_machine_summaries` `:1986`
reads it once per coalesced trace and hands it to `_render_setup_tile` (the list,
each item a `gui_widgets.StatusChip` `:662` coloured by state that opens its card
and focuses its row via `_setup_go`; one six-symbol key above the list; a
one-line Optional/Required note under each step) and `_render_setup_statuses`
(the Calibrate card's bed-face info text). A card's tile summary is
`card_summary`: the item names, never a status. The exported-Z options moved to
Scaffold → Export. The acceleration limits (`_build_machine_motion_limits`, once on
Physics → Visualisation) are back on the Machine card, Rotary accel on 4 axis only.

The same section builders are what the classic `_build_buildplate_tab` stacks, so a
field added to one appears in both UIs. Two things to know: `_collector_register`
now takes a **tuple** of modes for a row two modes share (never two calls), and the
modern page sets **`_rotary_rows_by_mode`** so `_sync_rotary_enable` stops gridding
the kind-dependent rows — two managers on one frame disagree as soon as their traces
fire in the other order. Tests: **`test_machine_grid.py`** (page) + **`test_machine_setup.py`**
(the status engine, headless).

Since **2026-09-15 (U11, Phase 3)** the **Physics** page is the second, at
**`columns=3`** — there is no preview panel on it, so the whole window width is the
form pane, and a card's BODY gets that width whatever the column count is. One card
per physical LAW rather than per field group, each printing its law **typeset by
`gui_math`** in a tinted strip above its parameters and carrying its own
**Calibrate…** jump into that quantity's `calibration_procedures` card:

| card | fields | jumps to |
|---|---|---|
| Process parameters | nozzle–collector gap, voltage, pressure, temperature (`gui_sim`) | — |
| Critical translation speed | `cts_ref` only | `cts` |
| Material and machine parameters | Nozzle (measured) + Material (melt) + Jet (the Taylor cone switch from `gui_sim._sim_cone_field`, its knee height, the base named as the bore radius), the one card keeping its section headers; typesets eta(T) only | `viscosity`, `activation_energy` |
| Jet lag | the six `lag_*`; typesets `lag = H A (1 - eps^-b)` — the PUBLISHED distance form, not a chord angle led by k | `lag3`, `lag4` |
| Fiber sagging | `sag_coeff` (c), `sag_span_exp` (p) | `sag` |
| Fiber fusion | the ON/OFF (`gui_sim`) above `fusion_overlap` / wall / clamp | `fusion` |
| Fiber diameter | NO parameters — BOTH laws (d over Q), the override toggle, the live readout (`gui_sim`) | — |
| Visualisation | coiling wave + amp/len (the cone moved to the Material card's Jet section) | — |
| Advanced | the mandrel per-ply switch, Reset (no registry fields — see `_PHYS_RETIRED`) | — |
| Physics presets | the one WIDE tile, the `calibration` kind's only box | — |

The cards' pictures (CTS, Material, Jet lag, Sagging, Fusion, Diameter,
Visualization, and Process) are 3D renders with dimension lines, one PNG per
card and theme in `assets/physics/`, made at BUILD time by
`assets/physics/make_physics_figures.py` (VTK offscreen + a matplotlib overlay;
rerun it after changing a figure, then commit the PNGs). Every picture is a row
of identical panels (`Panels`: one box size, title, equation, stage and caption
always in the same place; a panel's drawing is centred in its stage by
measurement). `gui/gui_physics_diagrams.py` only shows them: beside a fields column
of at least `FIELDS_WIDTH`, every card at the one scale that fits `FULL_WIDTH`
(three panels), dropping under the fields below `MIN_SHARE` of it. `_phys_card` mounts them, and `gui/gui_shell.py` mounts
the Process one. The modern-only plain field labels, units and hover help are
`_phys_fields`'s `labels` / `units` and `_PHYS_DESCS`; the Fusion card's are in
`gui/gui_fusion_input.py` (`LABELS`, `GATE_LABEL`, `DESCS`). The registry labels stay
for the classic UI and Lite. `gui/gui_fusion_input.py` displays the existing junction and wall
coefficients or a measured overlap in µm/mm at a measured fiber diameter. Its
conversion evaluates the built-in model's diameter and temperature hooks and
clamp; custom fusion laws do not offer distance entry because they need not have
an inverse. Out-of-range distances are refused rather than silently clipped.

`gui/gui_speed_ratio.py` keeps each scaffold's canonical speed in mm/min and its
`× CTS` representation together. Plate and surface modes use the entered plate
CTS; mandrel mode uses the entered mandrel CTS when present, else the plate CTS.
The Scaffold designer and the G-code Parameters tables show the selected CTS.
Per-layer ratios are stored separately from engine-facing layer overrides, which
remain mm/min. Presets retain the ratio mode and values, and CTS changes update
the corresponding speed and per-layer overrides.

Three things about it that are easy to get wrong:

- **`_cal_host` is gone** and `_sim_host` is now the Process card's inner frame.
  `_populate_sim_controls(frame, hosts=None)` splits into four groups
  (`_sim_process_fields` / `_sim_diameter_fields` / `_sim_fusion_switch` /
  `_sim_visual_fields`), each `(parent, row) -> next row`: the classic panel passes
  no `hosts` and stacks all four in one grid exactly as before, the shell passes a
  dict of card bodies. `sim_physical_fusion` therefore moved into
  `_sim_ensure_switch_vars`, because its widget is now built on the calibration
  side, which runs FIRST.
- **The law strip is `gui_math`'s markup, not a pre-spelled string.** Each `law=`
  argument to `_phys_card` is a small TeX subset (`\frac`, `_{}`, `^{}`,
  `\sqrt`, `\left[ \right]`, `\eta`…) and `gui_math` owns the italics, the
  raised exponents, the division bars and the operator spacing. Before
  2026-09-15 each card spelled its own equation as a `MewMono` line mixing real
  Unicode superscripts with ASCII carets; it read as a code comment sitting in
  the prose blurb. Do not hand-spell a new one.
- **Never give a sim host `columnconfigure(weight=...)`.** `_field` grids its label
  `sticky="e"` in column 0, so a weighted column 0 right-aligns every label against
  the far edge of a 1400 px pane. It happened twice in one session (the
  coiling-wave fields, then all four process knobs).

**16 off-by-default fitting hooks are NOT rendered on this page** —
`_ModernPagesMixin._PHYS_HIDDEN_HOOKS`: the 8 CTS power-law terms,
`field_tip_strength`, and sag's and fusion's diameter/thermal hooks. Every one is
a zero exponent (or the reference a zero exponent pivots on), so its law asserts
nothing with it left alone, which is also why none has literature backing to show
a user. They stay in `CALIBRATION_FIELDS`, in `Calibration`, in the laws, in
`lite_params` and in the classic developer UI's "Rig & material" tab, so **no
persist key changed and no parity guard moved**. Deliberately NOT a `group ==
"advanced"` filter: `sag_span_exp` (2.0, the viscous catenary), every `lag_*`
(Hrynevich 2020 Eq. 4) and `fusion_max_overlap` are all `advanced` and all fitted
or grounded, so they render.
`test_ui_shell.physics_page_present` pins the set against its own literal list —
derived from the source constant it was tautological and passed with 15 of the 16
un-hidden.

**One RETIRED knob is not rendered either, and it is a SEPARATE set on purpose**
— `_ModernPagesMixin._PHYS_RETIRED = {"contact_damping_zeta"}`. A hidden hook is
off but live: set its exponent and the law moves. ζ reaches **no geometry at
all**. It damped `integrate_contact_path_2nd`, the second-order contact filter
that `integrate_contact_path_ray` replaced on 2026-08-31; the shipped tracker
(`integrate_contact_path_ray`, `physics/mew_physics.py:1528`) takes `lag_ceiling` / `lag_floor` / `saturation_exp` and
has no damping term, and `mew_deposition.laid_fibers` says in its own docstring
that ζ is "accepted, ignored, reaching no geometry", kept only so old profiles
and `mew_export --zeta` keep loading. Same treatment as the hooks and for the
same reason — not rendered here, untouched in the registry, `Calibration`, every
saved preset, `lite_params` and the classic UI, so no persist key moved. The
Advanced card therefore has **no registry fields at all** today; its
`_phys_fields("Contact dynamics")` call stays so a future contact-solver
parameter lands there. `test_ui_shell` pins this set too, and both its
assertions were shown RED (un-retire the set; leak the field past the filter).

Every other modern page is still the U6 disclosure stack. Lite still draws
its scaffold page as disclosures too — a real parity gap, deliberately left for a Lite
change rather than taken as a side effect of this one (`capture_lite.SCAFFOLD_DISC_ORDER`
still asserts the shared ORDER, which is unchanged).

- **`mewracle_gui.py`** — the modern entry point + `--classic` dispatcher. `class
  ModernApp(_ShellMixin, _PreviewMixin, _ModernPagesMixin, _HomeMixin, _ModernThemeMixin,
  App)` — the shell layers lead the MRO so their overrides win (`_PreviewMixin` before
  `_ModernPagesMixin` so its `_show_scaffold_frame` chains); `App` brings the classic
  mixin stack unchanged. `_open_boot_splash()` runs at IMPORT time, above the heavy
  imports, and only when the file is launched as the app (not imported, not a frozen
  worker relaunch); `main()` then builds on the splash's Tk root.
- **`gui/gui_splash.py`** `BootSplash` — the boot card (logo, stage text, stepped bar) on
  screen ~0.8 s after launch, where the withdrawn main window takes ~10 s warm. An
  import hook (`_ImportPump`) redraws it during the module imports and is removed before
  the page build, so no full `update()` ever runs mid-build. Stdlib + tkinter only.
- **`gui/gui_legacy.py`** `_LegacyMixin` — More's **Legacy UI**: the old sidebar over the same pages, for the session. `_gce_text_only` (gui_gcode_editor) puts the CAD page away but for its text; Calibration still opens the CAD window. Guarded by `test_ui_shell.legacy_ui_round_trip`.
- **`gui/gui_shell.py`** `_ShellMixin` — overrides `_build_widgets`. Since 2026-10-31 **one top
  row** replaced the left sidebar: `NAV_TABS` is Home | Setup (Machine, Physics) | Calibrate |
  Design (CAD page, Scaffold designer) | Simulation | More (Print history, Machine link, Log).
  `_build_top_row` draws row 0 (`_nav_row`: wordmark, `_nav_btns`, `_nav_gear`, `_nav_context`,
  which the CAD page fills with undo, redo, Tutorial and Help) and row 1 (`_nav_strip`, the
  `SegmentedBar` of a two-page tab, hidden on the CAD page). `_finish_top_row` builds
  `_page_frames` / `_page_labels` / `_nav_tab_of` / `_nav_bars` / `_nav_more_menu`, `_nav_open(tab)`
  opens a tab at its last page, `_nav_refresh` lights it. Off the CAD page the row carries
  `gui_summary`'s design line. The bar's **Send to printer** sits beside Save g-code and greys
  with it (`_SaveGcodeButton.follower`); `send_to_printer` sends the CAD page's own text there
  (`_gce_send`), else the last result, and with no printer connected it opens Machine link.
  `_generate` with no CAD text, off the page, opens Design. The row drives a
  top-level **`gui_pagestack.PageStack`** (`gui/gui_pagestack.py:78`) — one area, every page placed and mapped for
  the life of the app, a switch is one `lift()`. It was a hidden-tab `ttk.Notebook` until
  2026-09-14; a notebook shows a page by UNMAPPING the old one, and that remap set off the
  relayout cascade that made a switch fill in from the bottom over 398–983 ms (§2.19a).
  `self.nb` keeps the notebook API — `add` / `select` / `tabs` / `tab` / `index` /
  `nametowidget` / `<<NotebookTabChanged>>` — so ALL frame-identity glue works unmodified
  (`_active_mode`, `gui_run._on_finished`'s auto-jump, `view_gcode`'s jump, the
  row's highlight). `goto_page` (`:479`) is what user controls call and carries NO paint
  fence, measured. Pages mount the EXISTING
  builders; the viewer tabs are rebuilt as four pages with identical widget names; the
  Machine page is a module grid under the 3/4/5-axis toggle (U12, below — no
  `machine_nb`). Other overrides: `_build_sim_tab` (Printhead-sim panel promoted to its
  own page), `_active_mode` (Home/sim/calibration are non-input),
  `_refresh_result_controls` (super() + a Generate label that NAMES its target),
  `_last_input_mode = "gcode"` launch default. Action bar = the classic bottom strip
  relocated (same widget names: `run_btn`/`cancel_btn`/`progress`/save
  buttons/`status`) under a hairline, filled-accent Generate. Keyboard: Ctrl+G generate,
  Esc cancel, Ctrl+D dark, Ctrl+1..6 the row's tabs.
- **`gui/gui_pages.py`** `_ModernPagesMixin` (U6) — the notebook-free page layouts:
  `_build_scaffold_container`, `_build_calibration_tab` (Required visible; Advanced /
  Guided fit / Presets disclosures; Advanced open-state writes back to
  `cal_show_advanced`), `_scaffold_selector_index`/`_scaffold_selector_show` (the
  pill-rail implementations of the scaf_nb glue contract), `_apply_all_toggles`
  (super() + `_sync_disclosures`).
  - **The Scaffold designer is a MODULE GRID since 2026-09-14 (U10)**, not a column
    of disclosures: title → collector toggle → count + `PillRail` → a
    `gui_widgets.ModuleGrid` of two-column tiles, in reading order collector /
    curved collector / geometry / preview options / stabilization / angle pattern /
    linking loops / footprint / array / linear / advanced / custom cell / export,
    then **Presets as the one full-width tile**. Clicking a tile REPLACES the grid
    with that module's fields across the whole pane. **Every field builder is the
    classic one, called with the arguments it always took** — only its parent
    changed, which is what keeps `_persist_map` key-identical.
  - A **per-scaffold** module is ONE card for all the scaffolds, its body holding
    one stacked frame per scaffold (`_scaf_module`; more are added as the count
    grows, `_scaf_slot_built`); `_show_scaffold_frame` raises frame `i`
    **and re-points that card's badge at `scaf_vars[i]`'s status var**. Geometry is
    the one card carrying all three collector geometries, gridded exclusively in
    the same body (`_build_geometry_frame`), because the card shows whichever
    collector is live and three separate cards would leave two of them empty.
  - **The Design summary** sits under the tiles on the plate and the mandrel
    (hidden on a curved surface): `gui_widgets.DesignSummary`, mounted with
    `ModuleGrid.add_footer` so an opened module covers it, filled by
    `gui_pages._ds_refresh` / `_ds_fill`. **It never waits for the preview.**
    Diameter, stepover and layers are the page's vars; the CTS verdict is taken
    when the preview snapshots an edit (`state["cts_now"]`, in `_pv_kick` /
    `_refresh_mandrel_preview`); the flat plate's print time and fiber length come
    from a quick job on its own thread (`_ds_quick` / `_ds_submit` ->
    `gui_preview.design_stats` -> `scaffold_gcode.print_path_stats`, off the same
    `preview_gcode` the preview builds from), which keeps running with the preview
    disabled. Only the plate check (`meta["off_plate"]`) and the tube's time and
    length (`build_mandrel_preview` meta) wait for a build. The one CTS comparison
    is `gui_preview.cts_check`; **both previews REFUSE to build below CTS**
    (physics on), in red, with `gui_preview.BELOW_CTS_STATUS`. `_ds_submit` paints
    before starting its thread: Tk calls made while the generator holds the GIL
    cost 760 ms on a 30x30 mm / 0.2 mm / 30-layer design.
    Units are the Settings panel's (`gui_units` readout-only quantities `pore`,
    `fiber_len`, `volume`, `print_time`, via `gui_units.readout`).
  - Measured module counts per collector: **10 on the plate, 6 on the mandrel, 4 on
    a curved surface** (`capture_ui` only ever walks the boot collector).
- **`gui/gui_widgets.py`** (U6) — the flat typographic primitives: `section_header` (tracked
  overline), `hairline`, `Disclosure` (chevron + title + live On/Off-or-value badge;
  body hidden via `grid_remove` so widget identity survives; `.sync()` re-settles after
  preset loads; optional `command` mirrors the open state to a var), `PillRail`
  (`.index()`/`.select(i)`/`.show_n(n)` with clamp), `ActionRow` (Home's flat task row),
  `scroll_guard` (stops Treeview wheel events double-scrolling the page). Composite
  internals are plain tk, rethemed by `set_palette(pal)` from `_apply_theme`.
  - **`ModuleCard` / `ModuleGrid`** (U10, 2026-09-14) — the module-grid primitive.
    A card is Disclosure's sibling and identical where they overlap (`status_var`
    drives the badge, `.body` holds the fields and keeps its identity for the life
    of the page); it adds a one-line summary slot and puts `.body` in the grid's
    shared detail host instead of under the tile. **That placement is a
    measurement, not a preference**: `gui_shell._scaf_init_sash` seats the sash at
    40%, so the form pane is 582 px, 534 inside its `PAGE_MARGIN`, and a two-column
    card is 261 — narrower than one label+entry pair in
    `_build_scaffold_geometry_fields`, which grids labels at columns 0 and 2. An
    in-place expansion clips its own labels.
    **The grid REFLOWS rather than holding fixed positions**, and that is what the
    collector modes need: `_apply_collector_mode` (`gui/gui_scaffold.py:337`) hides a
    section by calling `grid_remove()` on the widget it was handed, which in a
    fixed two-column grid leaves a hole. `ModuleCard.grid`/`.grid_remove` route
    into `ModuleGrid.relayout()`, so **that hook needed no change at all**.
    **A card must never measure its own width.** The summary label's
    `wraplength` comes from `ModuleGrid._on_tiles_configure` — the tiles frame,
    whose width is pinned by the canvas — pushed down via
    `ModuleCard.set_summary_wrap`. Bound to the card instead (as it was until
    2026-09-15) it is self-feeding, because the card's requested width includes
    that label: the card gets two stable widths and Tk alternates between them
    forever, so `update_idletasks` never returns and the window never finishes
    laying out. It converged on the Scaffold page only by luck — two columns in a
    fixed-width sash pane, where a card can never be granted the width it asks
    for — and diverged the moment the Physics page put three columns in a
    width-tracking scroll canvas. A card
    registered under TWO collector modes would not work and is not a gap —
    `_apply_collector_mode` walks every `(widget, mode)` pair, so one listed twice
    is shown by one pass and hidden by the next; a module for every collector is
    simply not registered. Tests: **`test_module_grid.py`** (9, the primitive in
    isolation, comparator = Tk's own geometry manager) and
    **`test_module_grid_no_drop.py`** (the frozen 497-key `_persist_map` baseline —
    the only guard that can see a preset key deleted outright, which
    `test_ui_shell.persist_parity` cannot, both its sides being built from the same
    function).
- **`gui/gui_preview.py`** (U7, 2026-07-02) — the **embedded live scaffold preview**: the
  scaffold page is now form (left, scrolling) + a fixed `PreviewPanel` (right) built by
  `gui_shell`'s scaffold-page block via the `_build_scaffold_preview` hook. Per-scaffold
  auto-updating 3D view: Tk-var traces → 350 ms debounce → `_pv_snapshot_spec` (reuses
  `_scaffold_spec` verbatim, start zeroed, traces suppressed) + `_pv_snapshot_options`
  (physics iff the sim panel's physics-deposit toggle; model via `_calibration_models`,
  mew_export's exact wiring) → one worker thread runs `build_preview_paths` (in-process
  generate → parse(with_times) → optional `mew_deposition.make_transform` relay →
  `decimate_paths` cap) → optional **approximate sag** (`preview_sag` seam, below) →
  results queue → Tk-thread apply. **That worker serves BOTH collectors since
  2026-09-01**: the 4-axis build submits to the same mailbox under the key
  `PV_MANDREL` (`gui/gui_preview.py:111`), so a slow tube family delays the picture
  instead of freezing the window — `_pv_worker_loop` and `_pv_apply_result`
  dispatch on that key, and the mandrel half owns the other end
  (`gui_mandrel._mandrel_snapshot` / `_refresh_mandrel_preview` /
  `_mandrel_apply_result`). The scaffold page is now **drag-resizable** (form and
  `PreviewPanel` share a `ttk.PanedWindow`, sash seated ~40% by `gui_shell._scaf_init_sash`),
  and a **"Preview options"** disclosure (above "Per-layer angle pattern", built in
  `gui_pages`, filled after the sim tab so its physics checkbox binds `sim_physics_deposit`)
  toggles 3-D tubes / **"Sagging (approximate)"** / physics path / colour / **"Studio
  light"**. The tube + sag
  toggles now **persist into the scaffold preset** (`pv.show_tubes` / `pv.show_sag` via
  `_PreviewMixin._preview_persist_entries`, no kind prefix → scaffold kind), so a saved
  design restores its preview state (`test_ui_shell.preview_options_preset`). The EXACT sag
  stays out (profiled 12.8 s at 30×30/0.5/×10); measured edit→applied physics ON 0.7 s,
  sag+physics ON 1.2 s (budgets 3 / 3.5 s). Rendering: OFFSCREEN vtkRenderWindow → PPM →
  Tk PhotoImage (a direct HWND embed is erased by ttk.Notebook repaints — bisected in the
  module docstring); tubes vs centrelines chosen ONLY by the "Draw fibres as 3-D tubes"
  checkbox (`_update_display_mode`, no zoom auto-switch — tubes stay tubes at every scale);
  **lighting** is `PreviewPanel._apply_studio_light`, PORTED from
  `gcode_player._apply_studio_light` and carrying the part that matters — it clears the light
  list FIRST, because vtkRenderer creates its automatic headlight during any Render that
  finds the list empty and that light then sits in the list as an ordinary member, so
  Add/RemoveLightsFromRenderer alone leaves studio mode with a stray SIXTH light (a first
  A/B hit exactly that: 6 lights, not 5). Ambient is deliberately NOT raised — the kit is
  DIRECTIONAL, so the away-facing facet still falls dark and the pores stay open zoomed out,
  which is what the old ambient 0.55 floor got wrong. `pv.studio_light` persists; the
  checkbox is UNREGISTERED so it shows in 3-, 4- and 5-axis alike, and its trace is
  `_pv_relight` — a renderer property, so no rebuild and no restyle, unlike its neighbours;
  controls left-orbit / right Z-turntable / middle-pan / wheel zoom-to-cursor
  (`install_cad_camera` math, pivot = scaffold centre). No origin triad (scaffold alone).
  Degrades to a text label if vtk fails. **No density cap** (the old `PREVIEW_EST_CAP`
  pre-flight refusal + display-decimation ceiling were removed by request — full-res
  always; `decimate_paths`/`PREVIEW_MAX_PTS` survive only as an opt-in `max_pts=` helper).
  Test: **`test_gui_preview.py`** (17 self-running tests incl.
  the latency budgets, the sag toggle, staleness, debounce-coalescing, pill-rail states).
  **Click-to-select** (2026-10-25): `PreviewPanel.pick` / `pick_at` /
  `set_layer_highlight` / `on_pick` / `pick_outline`, over the no-Tk
  **`viewers/preview_pick.py`** (pixel -> seen fiber via the depth buffer -> print time ->
  line via the player's line clock -> layer via `mew_gcode.Document.outline`).
  `on_pick` None (the default, every page but the G-code page) leaves Button-1 the
  plain orbit. Test: **`test_preview_pick.py`** (shown off-screen; comparators
  `vtkCellPicker`, the player's parse, the render keys; 4 RED proofs in its docstring).
- **`engine/preview_sag.py`** (rebuilt 2026-07-31, session 13 -- ground-up, blind to its
  predecessor) -- the **fast approximate "drape" sag engine** behind the preview's
  "Sagging (approximate)" toggle (`gui_preview.preview_sag` and `lite_core._drape`
  both call `drape_sag`). THE PHYSICS IS THE EXACT SOLVER'S (crossing supports,
  span^2/d droop, droop-reachable floor peaks, crest rounding, rest-on-round-cross-
  section, self-crossing stacking, visit-seam continuity at a layer change --
  PLANAR only, `drape_sag`'s `seam`, 2026-09-23; before it the drape had no seam
  or loop-entry rule and every layer change climbed a diameter --, a FLAT bed's
  floor read as the exact contact cap off `G`, each cell's top strand as two
  solved segments (`_segment_floor`, 2026-09-23; before it `H + d`, a one-cell
  top-hat that wrinkled every fibre laid beside an earlier strand), fusion w/ wall blend,
  periodic reset), with laws IMPORTED from `gcode_to_fibers`; what changed is the
  CONTACT ALGORITHM: analytic closest-approach between commanded-centreline segment
  pairs on a coarse hash (no heightmap, no raster, no resolution knob), heights
  interpolated from each lower fibre's solved (arc, z) profile. Measured (2026-07-31):
  mean |dz| 0.25-0.35 um vs `apply_sag` across default/rot60/circle/wavy + fusion/
  reset/multiplier/loops variants; 1.7 s parsed / 3.2 s physics-relaid at the
  30x30/0.5/x10 target (exact: 7-11 s). Departures are in its docstring; the
  turnaround-balloon fringe carries a bounded ~1 d residual on a few hundred points
  (printed interior: zero). Judged by **`tools/sag_bench.py`**: `compare` (accuracy + the
  5 s budget incl. the relaid path) and `invariants` (no phase-through at any
  commanded crossing, zero interior surface penetration, junction-shape consistency
  -- each proven RED). Pure numpy; imports the shared `gcode_to_fibers` and nothing
  imports it back. Test: **`test_preview_sag.py`** (27).
  - **The PERIODIC (4-axis) path's cost lives in ONE place** and it is not the
    raster: a fibre's self-support heights are a fixed point, and each pass of it
    re-solves the whole fibre. On the 88 210-point physics-laid re-entrant tube
    that loop ran **84 passes on the later fibres** (the commanded tube settles at
    the seed, 1 pass). Three shape fixes, all byte-identical output, took the case
    from 76.6 s to 27.1 s (2026-09-01):
    the floor `max(f, S)` dilates as `max(dilate(f), dilate(S))` so the dense term
    is computed once per fibre, not once per pass (`_MetricDilator.sparse_plan` /
    `sparse`); the support POSITIONS are invariant across passes so the sparse
    plan is cached; and the loop is swept only over the pairs that can reach a
    support (`_support_closure_pairs` -- 38.9% of them).
  - **Preview latency was chased again on 2026-09-04 and is CLOSED**. The two fixes that landed are
    `mew_frame.densify` (vectorised, bit-identical, 4-axis only) and
    `gui_preview.PREVIEW_SWITCH_INTERVAL` (the Tk event loop was HALVING the
    preview worker through GIL contention -- 2.91 s asleep vs 6.37 s running,
    same build); the *Mandrel default* preset went 9.55 s -> 4.13 s
    edit-to-applied and the 3-axis plate preview 5.13 s -> 2.81 s, no accuracy
    traded. It also records four things measured and REJECTED, so they are not
    retried: coarsening the drape `cell` (fades with length, and is not
    monotonically faster), pruning the periodic self-crossing candidates
    (correct and bit-identical on branch `selfcross-perf`, but 3-6% for a second
    code path through `_SegIndex`), `sag_depth`'s exponent, and the already-done
    2026-09-01 work above. Tubes to ~20 mm are now under 5 s; 30 mm is
    borderline and 50 mm is not, and re-entrant is RELAY-bound, not sag-bound.
  - **`gcode_to_fibers._self_contact_apply` is swept by DEPENDENCY LEVEL**
    (`_self_pair_plan` `engine/gcode_to_fibers.py:4054`, `_SelfPairs` `engine/gcode_to_fibers.py:4018`), which is what took the same
    case **28.7 s -> 15.7 s** (2026-09-01, best of 3, alternating processes against
    an ac695b8 checkout; 15/15 fixtures byte-identical). A pair lifts a point onto
    an EARLIER segment (`k + 1 < q` strictly), so the pairs are a DAG: level the
    points by longest lift-chain and a whole level evaluates in one vectorised
    step, total work one pass over the pairs whatever the depth. Measured depth is
    **3** on the re-entrant tube, 2 on the brick tubes. The plan is cached on the
    `_SelfPairs` tuple subclass because `preview_sag` sweeps one fibre's 556 484
    pairs ~40 times while its supports converge; `_support_closure_pairs` returns
    `pairs.subset(...)` so the reduced set carries a plan of its own. The scalar
    sweep survives as `_self_contact_sweep` (`engine/gcode_to_fibers.py:4347`) -- the fallback when the
    level relaxation does not settle in 64 rounds, and the comparator
    **`test_self_contact_levels.py`** (18) checks the levelled sweep against, with
    its own RED control (a dropped recurrence moves the fibre 1.1 diameters) and
    its own identity control (reordering the pairs within a point changes nothing).
    Shared with the exact solver, so gated by the five sag suites.
  - **What remains on that case** is `preview_sag` itself (the metric dilator's
    sparse scatter, `_span_rebuild`, the heightmap `np.maximum.at` -- 3.8 s of a
    16.0 s profile) plus `_SegIndex.rest_heights` (2.4 s, 9 calls): the periodic
    anchor floors query a growing index once per fibre, and the candidate set is
    **33.9 M pairs across the 9 calls, 55% of them in reach**. That quadratic
    growth, not the arithmetic, is the next shape to fix.
- **`viewers/viewer_embed.py` + `gui/gui_embed.py`** (U8, 2026-07-02) — the **embedded viewers**
: the Simulation page split into **Locked simulation** /
  **Live simulation** pages and the STL viewer embedded, all still OUT-OF-PROCESS (the
  crash protection): the child renders offscreen and streams frames through a
  shared-memory double buffer (`--embed name:WxH`); input forwards down stdin into a
  `vtkGenericRenderWindowInteractor` (install_cad_camera / key maps run unchanged);
  the sidebar chrome exports through `viewer_embed.EmbedChrome` (ViewerChrome's exact
  interface) and renders here as a themed ttk control rail (wordmark, gated readout,
  sectioned centred card buttons, keys/mouse legend, print time — the **universal viewer
  sidebar theme**, documented below) + a Tk transport scrubber + ⛶ fullscreen
  (see U9 below) + ✕ close. `gui_embed`: `EmbedSession`
  (shm owner, Popen, reader thread, 8 ms Tk pump, latest-only frames), `EmbedViewport`
  (PIL `ImageTk.paste` blit — 6.5 ms/frame; PPM fallback), `ViewerPanel` (states
  idle/starting/running/crashed(+stderr tail+Relaunch)), `_EmbedMixin` (reroutes
  `gui_viewers`' `_spawn_player` seam — the `_spawn_stl_viewer`/`_spawn_deviation_viewer`
  overrides were deleted 2026-08-04, unreachable since U2 folded both into the unified
  Viewer — relaunches running viewers
  on a fresh run, on_close teardown, theme forwarding). **Fps guards** (user report:
  ~30 fps stutter — fixed to 60): publish cap must sit BELOW the 16 ms tick (0.012 s),
  `timeBeginPeriod(1)` beats the 15.6 ms Windows timer quantization,
  `w2i.ShouldRerenderOff()`, single numpy copy into shm, and NEVER lazy-import numpy in
  the loop (Windows DLL-load deadlock against the blocked raw-fd stdin reader — see
  `EmbedHost.__init__`). Test: **`test_viewer_embed.py`** (12 tests: protocol round-trips
  for all three viewers, the >=45 fps floor, scrub/keys, crash contract, the U9
  native-fullscreen round-trip, no-orphaned-layer, and move-coalescing guards, plus the
  **sidebar-theme parity guards** — width equality, VTK button centring, ttk card style).
- **U9 — native fullscreen + pan-lag + orbit-pivot fixes** (2026-07-02, follow-up to U8). Three shipped pieces (the `scratch/spike_*.py`
  files cited below were gitignored developer spikes and are not in the repo):
  - **Native borderless fullscreen (Task A).** ⛶ no longer opens a Tk Toplevel; it sends
    `{"t":"native_fs","on":true,x,y,w,h}` and the CHILD flips its SAME render window
    offscreen↔onscreen-borderless at the app's monitor (`MonitorFromPoint`) — GPU-direct,
    vsynced, ZERO blits. The child uses the **platform (Win32) interactor from launch**
    (`EmbedHost.make_interactor`) even while offscreen, so synthesized events drive it in
    the panel AND `iren.ProcessEvents()` pumps real OS input once native (one interactor,
    observers installed once; verified `scratch/spike_native_fs.py`). Native chrome is a
    real `gui_viewer_shell.ViewerChrome` built from the exported control spec;
    `_make_borderless` strips the Win32 caption VTK keeps on an already-realized window
    (`SetBorders(0)` only applies at creation). Esc/F11/⛶ exit deferred out of the pump;
    child replies on every transition so the panel button stays truthful; publishing
    suspends while native. **The fullscreen window overhangs the monitor bottom by 1px
    (`_FS_OVERHANG`, 2026-07-08): a borderless window that EXACTLY covers the monitor +
    holds the foreground is promoted by DWM into independent-flip (direct-flip)
    presentation, and that mode transition costs one black frame — the "black flash" (at
    launch for the continuously-presenting sims; on first interaction for the tick-less
    STL viewer, which DWM only promotes once it presents again). Overhanging by a pixel
    keeps the window in DWM's normal COMPOSITED path — no transition, no flash. Diagnosed
    live via a foreground/present timeline probe (`MEWRACLE_FS_LOG`, since removed): both
    windows were confirmed foreground at entry, so it was NOT a focus/activation race.** **`_teardown_fs_chrome` sizes `NumberOfLayers` to the highest
    remaining renderer — a hard `SetNumberOfLayers(1)` orphaned the STL viewer's
    orientation-marker renderer (layer 1) and logged `vtkRendererCollection: Invalid layer
    for renderer` as a popup on exit** (`scratch/spike_fs_teardown3.py`). `EmbedHost`
    routes VTK output to stderr (`SetDisplayModeToAlwaysStdErr`) so no VTK message ever
    pops a window over a viewer.
  - **Fullscreen transport = the CHILD's own bottom bar (`ViewerChrome.transport`), not a
    VTK slider and not a GUI-side strip.** The `vtkSliderWidget` pick geometry does not
    survive the offscreen→native flip (`ComputeInteractionState` returns Outside
    regardless of renderer origin — `scratch/spike_fs_slider*.py`). A GUI-side topmost
    borderless ttk strip was shipped first and is **fundamentally unreliable** (diagnosed
    live 2026-07-08): once the monitor-covering fullscreen window holds the foreground,
    DWM gives it the display directly (independent flip / fullscreen presentation) and
    stops compositing other processes' windows over it — the strip kept `WS_EX_TOPMOST`,
    z-order, and `WindowFromPoint` pixel ownership yet never reached the screen. So the
    timeline is drawn INSIDE the fullscreen window: `gcode_player._mount_fs_transport`
    (via the `EmbedHost.fs_enter_hook` seam) mounts `gui_viewer_shell.ViewerChrome
    .transport()` — a full-width bottom bar (play/pause card + track/knob + right-aligned
    time) using the sidebar sliders' manual hit-testing (no VTK picking), scrubbing via
    `_fs_scrub` in-process; `EmbedHost.loop` calls `_fs_chrome.refresh_labels()` per tick
    so the knob/time follow the playing clock. Guard:
    `test_viewer_embed.fullscreen_transport_bar`. The embedded PANEL keeps its ttk
    transport (`_build_transport`; `_scales` syncs it from the "state" stream).
  - **Orbit-about-cursor depth fix (Tasks A + user #2).** The cad-camera pivot reads
    `renderer.GetZ`; `ViewerChrome`'s layer-1 overlay was CLEARING the depth buffer, so
    orbit/zoom silently fell back to the focal plane (hit in fullscreen; latent in the
    themed windowed viewers) — fixed with `PreserveDepthBufferOn()` on the overlay
    (guard: `test_vtk_helpers.test_chrome_stack_keeps_pivot_depth`). The **embedded
    scaffold preview** (`gui/gui_preview.py`) had the same class of bug for a different reason
    — its orbit pivoted on the scaffold BBOX CENTRE, never the cursor — now anchored via
    `PreviewPanel._cursor_pivot` (GetZ under the cursor on press, focal-plane fallback;
    guard: `test_gui_preview.app_orbit_under_cursor`).
  - **Pan-lag coalescing (Task B).** Tk fires `B1-Motion` at 125–200 Hz and the GUI
    forwarded each; the child rendered per backlogged move (rubber-banding on dense
    scenes). `viewer_embed._coalesce_moves` collapses runs of consecutive same-button/
    modifier moves to the newest in the command drain (a click/modifier-change breaks the
    run); `EmbedViewport._mouse` adds a 5 ms send throttle that flushes the held move on
    release. Measured: a 30-move backlog drops from 30 renders/10.7 ms to 1 render/0.7 ms
    (`scratch/spike_coalesce_bench.py`). Guards: `test_viewer_embed` coalescing unit +
    move-flood.
  - **Docked panel (2026-10-21, Windows).** Streaming capped a maximised panel
    (~1900x1150) near 20 fps: the Tk paste alone measured ~33 ms/frame, the child's
    render+readback ~30 ms. After the first streamed frame `ViewerPanel._maybe_dock`
    sends `{"t":"dock","hwnd":viewport.winfo_id(),w,h}`; `EmbedHost._dock` makes the
    SAME render window a `WS_CHILD` of that HWND and draws onscreen (measured ~95
    renders/s vs 17 fps streamed on one scaffold). Input is the platform interactor's
    own (`ProcessEvents`, as native fullscreen); fullscreen un-parents it and its exit
    RE-DOCKS. Streaming stays the fallback: hwnd 0, a failed dock, non-Windows, or
    `MEWRACLE_EMBED_BLIT=1`. Cost accepted: cross-process parenting couples input
    queues, so a hung child can stall GUI input. `capture_ui`'s PrintWindow still sees
    the docked view. Guard: `test_viewer_embed.docked_roundtrip`.
  - **Deferred — C/D/E (potential future perf wins, NOT built; user judged current perf
    fine 2026-07-02).** If more embedded-panel fps is ever wanted: **(C)** adaptive
    resolution — render ~0.7 scale while playing/dragging, full res at rest (paused
    screenshots stay full-res); **(D)** event-driven frame pump — the reader thread nudges
    Tk via `event_generate` on frame arrival instead of the fixed 8 ms `after` poll;
    **(E)** cheaper blit — write frames in a raw mode PIL's `paste` handles without
    per-pixel conversion (BGRX/RGBX) or `Tk_PhotoPutBlock` via ctypes (~half the 6.5 ms
    panel paste). **(F)** interaction LOD (wireframe/low-facet actors during a drag) for
    truly geometry-bound dense scenes.
- **Universal viewer sidebar theme** (`viewers/viewer_sidebar_spec.py` + `gui/gui_viewer_shell.py` +
  `gui/gui_embed.py`, 2026-07-03) — **THE GENERAL PRACTICE for all viewer chrome.** The viewers
  (Locked sim, Live sim, STL, Micrograph, plus the deviation recolour) wear ONE sidebar theme, drawn TWO
  ways from ONE control spec, so they look identical to each other and across the minimized
  ↔ maximized states. **Since the UI merge (U2) these are no longer separate Results pages** — they are the
  layers/modes of the ONE unified **"Viewer"** page (§2.12), but each still builds its chrome the same way.
  - **The spec (single source).** Each viewer builds its sidebar ONCE through the
    `ViewerChrome` interface — `section()` / `button(label_fn, cb)` / `set_hud(actor)` /
    `footer_section()` / `footer_label()` (players: `gcode_player._build_hud` +
    `_build_buttons`; STL: `stl_viewer.view`). Embedded, `viewer_embed.EmbedChrome`
    CAPTURES those same calls and exports the rows in the `hello` message; the two
    renderers below consume the identical rows, so content never diverges.
  - **Maximized renderer = VTK.** `gui_viewer_shell.ViewerChrome` draws the sidebar as VTK
    2D actors for the classic WINDOWED launch. Bordered card buttons with
    **centre-justified labels (both axes)**; the "READOUT" header (`_hud_header`) is built
    ONLY inside `set_hud`, so a viewer with no readout omits it.
  - **Native fullscreen = `gui_viewer_shell.RailChrome`** (a `ViewerChrome` subclass,
    2026-10-04), built by `viewer_embed._build_fs_chrome` from the child's `EmbedChrome`.
    It draws the EMBEDDED rail's layout in VTK: title + Paused/Playing chip, the readout
    table card (knob −/+ steppers + Reset), the tabs (`SegmentedBar` look) over one card
    as tall as the tallest tab, switches for `toggle=` buttons, and the key legend in a
    card at the bottom (the page keeps it behind its toolbar's Controls popup). The
    transport roles leave the sidebar for the bottom bar (restart / play / − speed + /
    track / time, the panel's order). Grouping comes from the SAME functions the ttk rail
    uses — `viewer_sidebar_spec.rail_tabs` / `readout_knobs` / `legend_rows` /
    `READOUT_ROWS` — so the two states cannot group a spec differently. Pixel measures are
    ported from the ttk rail (`_R_*` constants). The rail's tab crosses the boundary
    both ways in the `native_fs` messages (`tab`). Guard:
    `test_viewer_embed.fullscreen_rail_parity`.
  - **Minimized renderer = ttk.** `gui_embed.ViewerPanel._build_rail` draws the embedded
    page rail as real ttk widgets from the `hello` spec — wordmark, gated READOUT block,
    sectioned **`ViewerBtn.TButton`** cards (bordered, centred), footers. Wrapped in the
    shared `VScrollFrame` (`gui_common`) so a tall rail (the live sim's long key legend)
    SCROLLS instead of clipping — the bar auto-hides when it fits and `_retheme_rail_canvas`
    keeps the scroll canvas the sidebar tone across theme toggles.
  - **Slider + number controls (2026-07-03, for the micrograph viewer).** Beyond
    `button()`, the chrome exports two VALUE control types from ONE definition:
    `chrome.slider(label, value_fn, lo, hi, on_set, fmt)` and
    `chrome.number(label, value_fn, on_set, lo, hi, fmt)`. A rail edit sends
    `{"t":"ctl","id","value"}` → `EmbedChrome.apply_value` → the child's `on_set`; the host
    diffs `current_values()` and pushes `{"t":"values"}` back so a programmatic change
    (Reset / scale-from-2D / a fullscreen drag) re-syncs the widgets. **Maximized:**
    `ViewerChrome.slider` draws a themed track+knob (press jumps, `MouseMove` scrubs,
    `LeftButtonRelease` clears the grab); a `number` falls back to a slider (no text entry in
    a VTK overlay). **Minimized:** `_build_rail_slider` draws a `ttk.Scale` **with a small
    type-in entry** — drag to scrub OR type an exact value (Return/focus-out, clamped to
    `[min,max]`) so a known target needs no fine-scrubbing; the typed value updates the child
    state at once, so the maximized slider (reading that same state) shows it on maximize —
    no typing needed in fullscreen. `_build_rail_number` is a bare entry. Buttons ride
    parallel id lists (`_button_ids`/`_buttons_by_id`) so dispatch never misaligns once value
    controls intersperse.
  - **Shared invariants — keep these true for EVERY viewer:**
    - **Width is one constant:** `viewer_sidebar_spec.SIDEBAR_W` (240 px), imported by BOTH
      `gui_viewer_shell.SIDEBAR_WIDTH_PX` and `gui_embed.RAIL_W` (import-free module → never
      drags vtk into the GUI process, and the width can't drift). A viewer must not hardcode
      its own width.
    - **Buttons are centred bordered cards, NO colour swatch** — pass no `swatch=` to
      `chrome.button` (the STL per-scaffold/overlay chips were removed 2026-07-03; the
      maximized viewer drew them inside the card, the minimized beside it — both gone).
    - **A READOUT block appears only if the viewer registers one** — `set_hud` sets
      `has_readout` in the hello; the sims do, the STL viewer does not (its estimate lives in
      the INFO footer), so its READOUT header is omitted in BOTH states.
    - **Typeface is Helvetica throughout** — VTK via `apply_font`, ttk via the `Mew*` ramp
      (the rail readout/keys use `MewBody`, not mono, to match the VTK face).
  - **Parity guards** (`test_viewer_embed.py`, 14): width equality across the three modules,
    VTK button label centred, ttk rail using the centred card style (not flat `Nav.TButton`),
    plus `value_controls_unit` (slider/number export + `apply_value`/`current_values` dispatch
    + the button-interspersed regression) and `fullscreen_chrome_slider_drag` (press/scrub/
    release). `test_ui_shell.embedded_viewer_panels` asserts the five panels
    (locked/live/stl/**micrograph**/**deviation**) and that a built rail is wrapped in a
    `VScrollFrame`.
  - **Adding/theming a NEW viewer:** build its chrome through the same `ViewerChrome` calls
    (no `swatch=`; call `set_hud` only if it has a live readout; use `slider()`/`number()` for
    value controls) and it inherits the whole theme in both states for free. The **micrograph
    viewer** (§2.13) is the reference consumer of the slider/typing controls. Do NOT restyle
    inside the viewer — the two shared renderers own the look; a per-viewer override is the
    thing that breaks the sync.
- **`gui/gui_home.py`** `_HomeMixin` — the Home page (overhauled 2026-07-21): a guided
  **"Get started"** path of four `gui_widgets.StepRow`s walking the row (Setup → Calibrate →
  Design → Simulation) whose recommended step is highlighted live by `_refresh_home` (the
  first tab not yet opened, "Start here" or "Next", then Simulation "Ready" on a result;
  every step is always clickable), a compact
  feature-gated **"More tools"** list (`ActionRow`s: physics, g-code import, the Scaffold
  designer, Machine link), and a one-line keyboard footer. Deliberately NOT the old
  sidebar-mirror, preset quick-load strip, prose quick-start, or font-report footer.
  Under the welcome line, a **"Take the tour"** link (`start_tour`, below).
- **`gui/gui_tour.py`** `_TourMixin` — the guided tour: `STEPS` (page id, anchor resolver,
  title, text), eight steps along the top row. Each goes to its page, rings its anchor with
  four borderless windows OUTSIDE it in screen coords (`_tour_ring`; not
  `gui_finder.ring`, whose in-master band landed over card titles on a scrolling grid),
  and places a withdrawn-then-shown callout beside it (`_tour_reposition`, re-run on the
  ROOT's own `<Configure>` only). Never auto-starts, persists nothing, starts no worker.
- **`gui/gui_summary.py`** `_SummaryMixin` — the top row's **design line**
  (`_build_design_summary`, packed into `_nav_row`; a narrow row loses the name and then the
  state whole, `_summary_fit`): the
  Generate target, the design's detail in its tooltip (reuses `_summary_geometry`), and the result
  state — Not generated yet / Up to date / Changed since Generate. `_summary_note_run`
  (called first in the shell's `run`) records a signature: `_snapshot_for_kind` of the
  mode's key groups (`_MODE_GROUPS`) minus display-only `pv.*`/`.color` keys. A
  result is recognised when `last_gcode`/`last_depo`/`last_stl` change path or mtime
  after the worker exits; a 1 s tick (~1.5 ms) compares. Persists nothing. Probe:
  `.../cottonwood/probe_summary.py`.
- **`gui/gui_fonts.py`** — the Helvetica type ramp as NAMED fonts (`MewBody`/`MewTitle`/
  `MewSection`/`MewHint`/`MewNav`/`MewOverline`/`MewMono`), family resolved from
  Helvetica Neue → Helvetica → the bundled Liberation Sans (`fonts/`, SIL OFL)
  → Arial; why the bundled face must be a hinted .ttf is in its docstring; the 4-px
  spacing scale (`SP`, `PAD` — the modern `self.pad`; classic keeps 6/4 —
  `GAP_SECTION`/`GAP_HEADER`/`PAGE_MARGIN`/`INDENT`).
- **`gui/gui_modern_theme.py`** `_ModernThemeMixin` — overrides `_apply_theme` only: clam in
  BOTH modes, modern light/dark palettes (+ `hover`/`hairline` keys), reuses `gui_theme`'s
  by-class engine pieces (`_style_clam` etc. — `gui/gui_theme.py` itself untouched/shared),
  class-wide FLAT restyle (fields w/ focus ring, de-boxed `TLabelframe`, quiet Treeview
  headings, card-surface buttons, filled-accent `Primary.TButton`) + the shell styles
  (`Nav.TButton`, `Bar.TFrame`, `Shell.TNotebook`, `SectionHeader.TLabel`,
  `Pill/PillSel.TButton`, `Hairline.TFrame`, `ViewerBtn.TButton` — the viewer rail's
  bordered centred card button). Same `dark_mode` var + `gui_prefs.json` as
  classic; ends by calling `gui_widgets.set_palette`.
- **`gui/gui_tips.py`** — `Tooltip`/`add_tip`/`info_dot` + the `GLOSSARY` dict (palette-aware
  via `set_tip_palette`). **Pictures in tips:** `pic(name, text)` returns a `TipText` (a
  str naming `assets/tips/<name>.png`); the hover tip and the ⓘ popup draw it above the
  words (`tip_image` caches one PhotoImage per interpreter). The PNGs are rendered from
  the ENGINE by `assets/tips/make_tip_images.py` (scaffold_gcode → parse_gcode,
  `laid_fibers`, `apply_sag`) — re-run it after changing any of those; `*.png` is
  gitignored repo-wide with `!assets/tips/*.png` excepted, and `MEWracle.spec` bundles the
  folder. Photographed by `.../cottonwood/probe_tips.py`.
  `gui_widgets.Disclosure(tip=)` is the dot-free alternative to `info=` on a header.
- **`test_ui_shell.py`** — self-running, 14 tests: classic smoke (developer UI untouched),
  modern build/page sweep, **persist-map parity (the no-drop guard)**, widget presence,
  `_active_mode` matrix, Generate labelling, hidden-tab-bar check, theme round-trip,
  toggles, **no-inner-notebooks guarantee**, **pill-rail show-N/clamp matrix**,
  **disclosure preset re-sync**, **embedded viewer panels
  (locked/live/stl/micrograph/deviation)**, **Analyze page removed, deviation still answered**.
- **The rule:** a new FIELD needs zero shell work (pages mount the builders — put it in
  the right group builder and it lands in both UIs); a new PAGE needs one registry entry
  in `_ShellMixin._build_widgets` + a place in `NAV_TABS`; a new secondary GROUP on a modern
  page = a `Disclosure` in the stack (bind its enable var for the badge + re-sync).

### 2.13 Micrograph overlay viewer

- **`viewers/micrograph_viewer.py`** — a VTK viewer in the SAME shape as `stl_viewer` (embedded
  panel + native fullscreen via `viewer_embed.EmbedHost`), imported STANDALONE. Textures a real top-down micrograph on a `vtkPlaneSource`
  (`load_image_rgb` → `image_texture` → `image_plane_actor`) on the XY plane; the generated
  scaffold (reused `stl_viewer._load_centerlines`/`_centerline_actor`/`_stl_actor`) is drawn
  FIXED on top. Only the IMAGE moves: `_ImageState` holds per-axis scale (mm/px), rotation
  (Z), origin + Z and opacity, rebuilt as a `vtkTransform` (Scale→RotateZ→Translate). On open
  the image AUTO-FITS to the scaffold footprint (`_fit_placement`); a pinned `--scale-x/-y`
  (from the 2-D setter) overrides. Controls are ALL `chrome.slider(...)` (opacity ×2, origin
  X/Y/Z, scale X/Y, rotation) + buttons (Snap top-down, Fit to scaffold, Reset) so the rail
  and the VTK fullscreen sidebar render identically; a live HUD (`set_hud`) shows the
  placement/scale readout. Keys: arrows nudge XY (Shift ×10); Z is set by its slider. CLI:
  `viewers/micrograph_viewer.py <stl|.depo> --image P [--scale-x/-y --origin X,Y --rot --z --render
  --embed]` (`parse_micro_argv`, both spaced/joined forms). Reuses `vtk_helpers`.
- **`gui/gui_micrograph.py`** `_MicrographMixin` — **STALE ENTRY (corrected 2026-07-21):**
  since the UI merge (U2f) this is NO LONGER a launcher page. It is a thin helper mixin
  exposing only `_an_micro_load` (PIL/PNG file dialog) and `_an_micro_scale_window` (the
  optional locked-2D matplotlib window: click two points on a known length → pin a real
  mm/px); the micrograph is now an in-scene LAYER of the ONE unified Viewer, pushed down
  via `_unified_import_image`/`_unified_scale_image` → `panel.send_child(t="micrograph")`
  (`gui_shell._build_viewer_pages`'s top row, `gui_embed._push_micrograph_overlay`). The
  old `_build_micrograph_launcher` / `_show_micrograph` / separate "Micrograph" Results
  page are GONE. The standalone `viewers/micrograph_viewer.py` worker is unchanged.
- **Sidebar chrome** rides the §2.12 slider/number control types + the manual-typing rail
  entry. Guarded by `test_micrograph_viewer.py` (5) + `test_viewer_embed.py` (14) + the
  `test_ui_shell` panel/page checks.

### 2.14 Deviation (the Analyze page was removed 2026-09-24)

The Analyze page (fidelity table, Experiment Designer, Validation workbench) is **gone**:
the Calibration page (§2.15) replaced its experiments and fit loop, and nobody used the
fidelity table or held-out validation from the UI. What survives is the Viewer's
**"Show deviation"** colouring and the headless engine behind it.

- **`analysis/mew_analysis.py`** — the headless engine (pure numpy; imports
  `gcode_to_fibers`/`mew_physics`/`mew_calibration`, never Tk/VTK). **`deviation_sidecar(
  gcode, depo, out)`** is the Viewer's one call: it builds the PREDICTED bundle from the
  `.depo` and the COMMANDED bundle from the g-code (`bundle_from_gcode(with_travels=)`,
  taking the G0 travels exactly when the `.depo` is stamped `render_travel` — every
  travel-aware physics export is, asked or not), pairs them by fibre index, and writes the
  sidecar. `_deviation_by_fiber` computes per-vertex cross-track deviation in the
  print-time frame via `_deviation_crosstrack` — which first fits and removes a per-fibre
  transport delay `tau_hat` (kills the `lag*(1-cos Δheading)` corner phantom) and computes
  on a canonical one-diameter arc-length grid with a fixed `_DEV_TANGENT_DIAMS` tangent
  window (sampling-invariant), with an arc-length fallback for time-less bundles. It
  refuses (`DepoMismatch`) only a travel `.depo` paired with a travel-FREE commanded
  bundle. `write_deviation_sidecar` stores `dev`/`splits`/`vmax` + a p99 `vcap` colour cap
  floored at the float32 storage noise (`_DEV_NOISE_ULP_K·2⁻²⁴·max|coord|`, so a
  physics-off cache paints all-blue). The fidelity metrics (`compare_geometry`,
  `per_layer_metrics`), parity/fit (`parity_for_measurements`, the fit targets) and the
  sweep/grid/sensitivity primitives are still here, UNWIRED, for scripts and the paper.
  Guarded by `test_mew_analysis.py`.
- **`gui/gui_deviation.py`** `_DeviationMixin` — answers the Viewer's child→parent
  `deviation` request (`_req_viewer_deviation`, registered by
  `gui_embed._viewer_request_handlers` only when the method exists): runs
  `deviation_sidecar` on the active print (`last_gcode`/`last_depo`) off the Tk thread and
  pushes the sidecar (or an `error=` reason) DOWN with `panel.send_child(t="deviation")`.
  Caches the last sidecar keyed on the `.depo`'s path + mtime (the session folder reuses
  file names); `_invalidate_deviation` drops it on a new Generate (via
  `gui_embed._invalidate_analysis`).
- **`analysis/analysis_plots.py`** — optional-matplotlib figure builders; now used by Print
  history's saved-log graph (§2.17). `test_analysis_plots.py`.
- **In the Viewer** (`viewers/gcode_player.py`): `_toggle_deviation` → request;
  `_on_deviation_msg` applies the sidecar, or sets `_dev_error`, which `_refresh_hud`
  keeps on the HUD (the rail's status line) until the next attempt — a one-shot
  `hud.SetInput` was overwritten by the next tick, so a failure used to look like nothing.
  The colouring code lives in `viewers/stl_viewer.py` (§2.8) as a library: `_deviation_lut`,
  `_deviation_scalar_bar`, `_load_deviation` (sidecar↔`.depo` alignment check).
  Locked sim, embedded Viewer only; the classic UI's windowed player has no deviation.
  Guarded by `test_viewer_embed.py` (the request/`send_child` round trip) +
  `test_stl_viewer_args.py` + `test_ui_shell.py` (`analyze_page_is_gone`).

### 2.15 Calibrate

**Since 2026-10-31 Calibrate is a MODE of the G-code CAD window, not a page**
(`gui/gui_gcode_calibrate.py`, `_GcodeCalibrateMixin`, whose docstring is the
authority): `_nav_select("calibration")` lands on `tab_gcode`. Calibrate is
Design with the calibration's Parameters and a default print. Type, then Create
(Stabilization scaffold) and Modify (Parameters) with Design's groups, icons and
order, then Instructions, Measurements and Apply replace Collector, Create,
Pattern and Modify on the ribbon; Add, Change selection, Inspect and File stay,
as Design's (2026-11-13). Parameters has no Appearance rows since 2026-11-13:
the fibre's colour is the ribbon's Color tool (`_sw_player_color` reads its
first-slide pick), and `_sw_diam_mult_value` is 1, as in Design.

**CTS linking loops (2026-11-13)** -- "Linking loops" on CTS (3-axis)'s
Parameters joins each line to the next with the designer's turnaround loop
(`mew_sweeps._line_move` over `scaffold_gcode._turn_points` / `_turn_circle`):
`sw.cts_loops_on` (on, as Design's linking loops are), `sw.cts_loop_arc`
("Turnaround radius", x line spacing, 1.5) and `sw.cts_loop_speed` ("Loop
speed", mm/min, blank = the line before). `CtsParams.loop_arc` 0 is the
engine default and writes the old bytes. The lines are pulled in by
`_loop_reach` so the loops stay inside the margin. Looped lines are one
printing run, so the tree matches each line's own segment
(`gui_gcode_calibrate._segment_starts`).

**A Calibrate parameter change is one undo step (2026-11-13)**, as a redesign
is in Design: `_cal_load_doc` keeps the type's history on a same-type rebuild
whose sw.* values changed (`_cal_vals_seen`) and attaches, to the text's step,
the document before/after plus the changed values with their layout and
signature (meta slots 4/5; Design's slots 2/3 stay None). Undo/Redo runs
`_cal_restore`: layout and signature first, then the values, with
`_cal_take_state` held off (`_cal_restoring`); the rebuild they start finds
the signature equal and loads nothing. `_cal_take_state` also skips a result
while an edit is pending (`_sw_state["dirty"]`), and the signature leaves out
the header's date and time (`_cal_undated`): each build stamps its minute,
and a print rebuilt across a minute read as new and lost its history. A type
left and found again on the same print gets its document and steps back.
Since 2026-11-02 every slide is on screen at once in ONE editable document
(`calibration_layout`, slides tiled on their own plates), the Design waits in
`_cal_stash`, edits are kept per type in `_cal_docs`, and Save writes each slide
at its own origin (`_sw_slide_text`). The tree is Header, then a "Slide k" row
per slide over its passes (Base, each value, End); for COLOURING only a slide is
one scaffold (`_view_layer_table`), and its colours are kept apart from
Design's (`cal_color_scaf`). The picture is Design's, with the calibration's own
jet (`_cal_physics`). `CardBox` shows `gui_calibration`'s forms in the floating
box. "Card", "tab" and
"page" below are the old page's words: the forms, `_cal_cards`, `_cal_tab_var` and
the procedure keys are the same objects, the tile grid and tab strip are gone
from the modern UI, and the rest stands. The move was proved byte-identical.

**One card per calibrated quantity**, each walking the operator print → measure →
enter → Compute → Apply. Since 2026-09-03 this ONE page replaces two half-jobs:
the Experiment-sweeps page (prints no fit consumed) and the Physics page's
Guided-fit disclosure (fits with no prints — five of the six named no protocol).
The CLASSIC developer UI hosts the same card stack (`_cal_build_cards`) beside the
rig/material fields, minus the print knobs (it carries no `_SweepsMixin`).

**Export + Quality parity with the Scaffold designer (2026-10-07)** -- the Export card (`_sw_build_output`) and a Quality card (`_sw_build_quality`) carry the designer's rows that a calibration print can use, on the page's OWN `sw.*` keys (`exit_move`/`exit_margin`, `g2g3`, `travel_as_g1`, `gcode_convention`, `travel_in_stl`, `travel_gcode_line`, `faces`, `sag_mode` (default Exact), `sag_reset`/`sag_reset_layers`, `mesh_tol`), plus the machine's shared "Don't adjust height per layer" (`no_layer_z`). Arcs and the exit change the FILE (`mew_sweeps` `g2g3` / `exit_margin`, `SweepResult.arc_segments`); the rest reach the converter AND the player via `_sw_quality_args` (they are the `.depo` cache key) and "Save g-code as" / the g-code view via `_sw_user_gcode` -> `_rewrite_gcode_lines` (`_last_gcode_is_calibration` routes `gui_scaffold._gcode_emit_opts` here). Left out on purpose: Also render STL, deposition order, per-scaffold Z, and "Add printhead elevation to Z" (a G-code-zero datum the calibration export does not re-base onto). Guards: `test_sweep_export_options.py` (engine) + three `test_gui_calibration.py` cases.

**Fiber diameter is the Scaffold designer's, plus a visualization multiplier (2026-10-05)** -- `gui_sweeps._sw_diam_mm` reads the designer's field again (a page-local override was tried and removed: the speed ladders print each rung at its own diameter, and a blanket value erased that). The multiplier's row left Calibrate on 2026-11-13 and the CAD window reads it as 1; the key `sw.diam_mult` persists. The Appearance block carried it ("Fiber diameter multiplier", default 1): it scales every drawn fiber's own diameter and nothing else. It reaches the preview as `meta['tube_diameter']` (`_sw_on_diam_mult`, no rebuild) and the Viewer and STL as `--diameter-multiplier`, which sets `gcode_to_fibers.TUBE_RADIUS_MULT`, read only inside `tube_mesh` -- downstream of the sag solve, the contact radii and the `.depo` cache.
The viewer's Diameter readout is ONE number, `Player._fiber_diameter_um` (the jet's live value when physics drives the diameter, the typed one otherwise); guard `test_hud_diameter.py`.

Three modules, one concern each:

- **`analysis/calibration_procedures.py`** — the procedure REGISTRY. `Procedure(key, title,
  tab, calibrates, print_protocol, groups, settings_notes, measure)` and an
  insertion-ordered `PROCEDURES` dict of the seven keys (`cts`, `viscosity`,
  `activation_energy`, `lag3`, `lag4`, `sag`, `fusion`). Its order IS the tab
  order and its prose is the ONLY procedure text in the app — the page hardcodes
  none, and a tab the registry has no procedure for does not exist. Three fields
  exist for the tabbed page (2026-09-03): **`tab`** is the short strip label
  (`title` is the card heading and far too wide for a tab); **`groups`** holds
  the steps in FOUR groups, in order — `SETUP` ("Set up the software"), `PRINT`
  ("Make the print"), `MEASURE` ("Measure"), `APPLY` ("Enter and apply (Fit results card)"), listed
  in `GROUP_ORDER`, and a tab that owns no generator still has all four, its
  "Make the print" saying what to print by hand; and **`settings_notes`** maps a
  section id to `(step number, one-line summary)`, which is what every
  Process-settings ⓘ on the page opens with. The ids are `SECTION_HEADERS[key]`'s,
  one per settings box that tab shows, and the page renders each by exactly one
  dot in BOTH directions
  (`test_gui_calibration.settings_notes_match_the_info_dots`) — so an inserted
  step cannot leave a dot quoting the old number. Step counts are
  25/14/14/31/23/15/15. **`steps` is a derived property**, the flatten of
  `groups`: a second STORED copy of the prose is exactly what this module exists
  to prevent. `test_calibration_procedures.py` (28).
- **`gui/gui_calibration.py`** — the FORMS. `_cal_build_cards`, `_cal_build_card`,
  the per-key fit
  builders, and `_CARD_FITS` mapping key → builder.

  **A MODULE GRID under the tab strip** (2026-09-16, the Scaffold designer's
  `gui_widgets.ModuleGrid`): six cards in reading order — **Calibration
  settings**, **Stabilization scaffold**, **Export**, **Instructions**, **Fit
  results**, **Calibration presets** (`app._cal_grid`). Three are PER TAB:
  `_cal_build_card` builds one frame per tab into each of Calibration settings
  (`_cal_fill_process`: the print knobs via `_sw_build_protocol`, or a fit-only
  tab's `_CARD_CONDITIONS`, empty since the lag4 boxes went), Instructions (the tab title, what it writes, then
  `_cal_fill_instructions` — **words only**, the registry's four groups over
  steps numbered 1..N, wrapped to `_INSTR_WRAP`) and Fit results (the "Enter your
  measurements" header + the fit form). `_cal_cards[key]` is that
  `{"settings", "instructions", "fit"}` dict, and `_cal_show_tab` grids exactly
  one tab's three. The other three cards are the shared print design, one widget
  set on every tab. Tile summaries: `_cal_refresh_summaries` /
  `_cal_print_summary` and `_sw_refresh_card_summaries`. The classic UI (no
  `_SweepsMixin`) gets the three per-tab cards alone. `modules_in_order` and
  `instructions_are_words_only` guard it.

  **The ⓘ on every settings sub-section names its step.** `_cal_section_disc`
  (`gui/gui_calibration.py:2051`) builds a header plus a dot whose text `_cal_note_text` opens
  with `"Step N of the Instructions: <summary>"`, straight from `settings_notes`
  — never typed in this file, so the number a dot quotes and the number the
  Instructions render are the same number by construction. Six sections are
  **ONE set of widgets shown on every tab**: `plate_feed_limit` (the foot of
  Calibration settings), `stabilization_base` and `export_options` (their cards'
  own ⓘ), `target_slide` (inside Export), `experiment_presets` (the presets
  card's ⓘ) and `preview_options` (an ⓘ beside the preview's PREVIEW title) —
  their step lines **retargeted per tab** by `_cal_place_shared` — which is why
  `gui_tips.info_dot` grew `set_info_text()`/`info_text()`. Two copies is not an
  option: they bind the same `sw.*` vars, and the second `Entry` built on a var
  is the one the user types into while the first goes stale on screen.

  **One preset box per kind, and which page hosts which** (Joseph, 2026-09-03).
  The rig + material profile (kind `"calibration"`) is saved where its fields
  are: the Physics page's **"Physics presets"**
  (`gui_pages._build_calibration_section`), and in the classic UI the Calibration
  notebook's sub-tab of the same name. This page's own box is the PRINT design
  (kind `"sweep"`), labelled **"Calibration presets"**. **Since 2026-09-25 it is
  PER TAB**: each print tab is its own kind `"sweep:<tab>"` (tab = the
  `calibration_procedures` key) in `presets/calibration/<tab>/`, with pointer
  `"sweep_default:<tab>"`, holding only `gui_sweeps._sweep_tab_keys(tab)` — the
  protocol's own `sw.<prefix>*` keys (`_SW_PROTOCOL_PREFIXES`), the shared cards
  the tab shows (`_SW_SHARED_KEYS`) and the rod rows on a mandrel tab. The page
  state (`kind`, `cal_tab`, `write_csv`, the preview toggles and colour) rides no
  preset. `_build_sweep_presets_tab` builds one shared-UI box per tab and
  `_sw_show_presets_tab` (called from `_cal_show_tab`) grids the showing one; Fusion
  prints nothing and has none. Bare `"sweep"` is still the whole `sw.*` KEY GROUP
  (the scaffold kind subtracts it; Print History snapshots it) with no store. A
  loose `.json` in `presets/calibration/` is a pre-split whole-page preset:
  `gui_presets._migrate_legacy_sweep_presets` (run from `_load_presets`) sends it,
  through `_migrate_preset_data`, to ONE tab (`sw.cal_tab`, else `sw.kind`'s tab) with
  that tab's keys, moves the original to `_legacy/`, and carries the old
  `sweep_default` over. `test_presets_stores.py` guards all of it. The folders were swapped
  to match the labels on the same day — kind `"calibration"` → `presets/physics/`,
  kind `"sweep"` → `presets/calibration/` — while the two IDS stayed put, so
  reading either id as a folder or a label is wrong in both directions (the table
  atop `gui/gui_presets.py`; the one-time move is `gui_common.migrate_preset_folders`).
  Building one kind twice
  is not two views of one store — `_init_pstore` overwrites `_pstores[kind]` and
  `_build_preset_table` overwrites `st["table"]`, so the first box keeps a dead
  table and Load applies whatever the survivor has selected.
  `test_presets_stores.py` guards it. Two cards are
  new here: `_build_fit_cts` (no fit — the operator reads CTS off the print, and
  the entry is labelled mm/s because the sweep's feeds are mm/min) and
  `_build_fit_lag_circles` (typed rows OR "Load CSV…" → `mew_circle_fit.fit_amplitude`;
  Apply writes `cal.lag_amplitude` AND `cal.lag_amplitude_gap` together).

  **TABS, not a stack** (2026-09-03 — seven cards on one scrolling page was one
  unreadable page). `_cal_build_cards` draws a `gui_widgets.PillRail` of the
  seven `tab` labels (that widget — the same one the scaffold selector is — took
  `labels=` for this; a ttk.Notebook CLIPS its strip rather than wrapping, and
  seven labelled tabs do not fit across this pane). A labelled rail is
  **equal-width and flush left**, which the first shape was neither: every column
  joins one `uniform` group and every pill is `sticky="ew"` (without that, "CTS"
  sat centred 35 px inside a column sized by "Lag radii (4-axis)" while the
  second row started at 0), and the pills-per-row is MEASURED from the width the
  rail is given (`_fit_per_row` on `<Configure>`), not passed in -- so the rail
  is gridded `sticky="ew"`. Seven pills at the widest label are 122 px each and
  want 878 px; the form pane offers 735, so they wrap 5 + 2 with both rows at the
  margin. `tab_pills_are_one_equal_flush_row` measures it. Every card is
  built once and only the selected tab's frames are gridded, so a tab
  switch is a show/hide and typed fit inputs survive it. `_cal_show_tab` is the
  one entry point; the selection lives in **`sw.cal_tab`**, so the page reopens
  where it was left (the classic UI has no sw.* keys and gets a local var).
  **What Generate targets**: a tab that owns a print protocol writes
  `sw_vars["kind"]`; a tab that does NOT has nothing to point it at and leaves
  the bar alone, so `_cal_target_text` states in words, on every tab, which
  print Generate will build and which tab it came from. That is the alternative
  to silently building the print from a tab you are not looking at. The old
  per-card protocol radio is gone — selecting the tab is now that statement.

  **The lag3 fit rows are an editable list** (`_fit_circ_add_row` /
  `_fit_circ_remove_row` / `_fit_circ_regrid`): Add row, a per-row remove, and
  **no hidden overflow**. The card used to offer a fixed eight rows plus
  `_fit_circ_extra`, which a longer CSV spilled into — those rows were fitted
  but could not be seen or corrected. `_fill_fit_circle_rows` now leaves exactly
  as many rows as the file has, and `_fit_circle_rows` reads every row on
  screen and nothing else. The re-fill loops are COUNTED, not
  `while len(...) != n`: proving the guards red with remove stubbed out spun a
  `while` until the timeout killed the run.

  **One set of calibration vars.** `_init_calibration_vars` is IDEMPOTENT, and
  that is load-bearing. Two builders call it — the Physics page's Core/Advanced
  section (`gui/gui_pages.py:957`) and this page (`gui/gui_calibration.py:153`) — and it used to rebuild
  `cal_vars` each time. A Tk `StringVar` unsets its Tcl variable in `__del__`
  and an `Entry` holds only the variable's NAME, so dropping the first set left
  36 of the Physics page's 42 fields bound to variables that no longer existed:
  they rendered EMPTY, and a card's Apply wrote the second set, which nothing
  visible was bound to. That was Joseph's 2026-09-03 report ("the CTS entry in
  the Physics section is empty and didn't get updated"), and
  `test_gui_calibration.apply_reaches_the_physics_page` — which reads the
  WIDGET, not the var — is what pins it.
- **`gui/gui_sweeps.py`** — the PRINT ENGINE behind the cards; it owns no page and,
  since the mode, no picture. See below.

The **jet-lag squares are hidden from the UI**: `mew_sweeps.generate_lag` and
`test_mew_sweeps.py` are untouched, but no field reaches them, and a saved
preset carrying `kind="lag"` degrades to the CTS sweep rather than crashing.

- **`analysis/mew_sweeps.py`** — the headless engine, a byte-identical port of the two
  data-folder generators (verified against their outputs at defaults, 2026-07-06):
  `generate_cts(CtsParams)` (Hrynevich straight-line ladder: descending feeds ×
  reps, ONE boustrophedon column per slide, spill = a new print file, never a
  second column) and `generate_lag(LagParams)` (open squares sized
  `safety×lag(eps, gap)` from Hrynevich Eq.4, arranged by a selectable LAYOUT
  (shelf auto-fit / columns-by-ε / rows-by-ε / uniform grid) + emit ORDER
  (size/slowest/fastest/position) + optional per-square manual PLACEMENT pins,
  `array_travel`-routed G0 hops, per-eps rep overrides, diag
  tail) → `SweepResult` (per-slide g-code texts, measurement-CSV template
  (CRLF, the scripts' schema), per-print bbox + ON-SLIDE verdicts, per-specimen
  `Specimen` label descriptors, `oversize` list, warnings). Both prepend the
  program's own stabilization base (`scaffold_gcode.emit_scaffold_layers`).
  Engine additions: the ORIGIN OFFSET (`origin_x/y` shifts base + specimens +
  the on-slide check); the **never-erase** contract (`_pack_into_prints` returns
  `(prints, oversize)` — a normal excess auto-splits onto more slides, a single
  specimen bigger than the usable plate is flagged in `oversize`, never dropped
  silently or raised); a machine feed-ceiling check (`max_feed` →
  `_feed_ceiling_warnings`, the runbook's repeated caveat); and `extract_moves`
  (deposition via the real `parse_gcode` mode "g1", `with_times=` for the physics
  relay; travels = the G0 hop runs from a modal tracker — continuous-flow MEW
  lays fibre on every move, so the preview must show them). The **placement
  controls**: the LAYOUT packers -- `columns`/`rows` via
  one `_dense_pack` (a real 2-D shelf, size-descending FFD, vertical/horizontal;
  a size-ASCENDING order or a one-eps-per-line pack wastes the perpendicular
  dimension and costs a slide -- the bug this replaced), `grid` via `_pack_grid`
  (per-slide-max uniform matrix), all never-erase like `_pack_into_prints`; the
  ORDER policy (`_apply_order`, supersedes the legacy `speed_order`); and the
  manual PLACEMENT pins (`placements={label: (slide, x_abs, y_abs)}` pin a
  square, the rest auto-lay; an orphan label is ignored — a deliberate
  slicer-"locked object" behaviour, so a pin survives an ε being removed and
  re-added). Pins are applied AFTER the emit ORDER and only the pinned slides are
  re-sorted within themselves, so a `slowest`/`fastest` order cannot move a pinned
  square off its assigned slide. Defaults (`shelf`/`size`,
  no pins) stay byte-identical; a `lag_layout_efficiency` test guards that
  columns/rows never use more slides than the shelf. `test_mew_sweeps.py` (52).
- **`generate_sag(SagParams)`** — the FIFTH experiment (2026-09-18), the Sag
  tab's own print (`print_protocol="sag"`, so the tab no longer says "bring your
  own"). A row of one-fibre-wide walls running in Y whose CENTRELINE PITCH steps
  along X (`spans`, mirrored by default: 7 spans -> 14 gaps, 15 walls), crossed
  by TWO bridging fibres running in X one layer above the wall tops, each set
  `fibre_inset` in from a wall end. One slide, ~22 x 3 mm against the plate's
  front edge, laid right of the shared stabilization base. Emit order is walls
  layer-by-layer (a serpentine per layer, the X hop between walls at a wall END,
  sweep direction flipped each layer) then fibre A then fibre B — the inset is
  what keeps the connectors' beads out of the measured stretch. `span - diam` is
  the clear gap; the CSV is one row per gap per fibre with `depth_mm` /
  `bottomed_out` blank. Defaults come from the paper repo's `Done/Section 2.4.4
  — sag calibration procedure` (2026-09-18) and are TENTATIVE — unprinted, and
  20 layers of 15 um fibre is only 0.285 mm of headroom under a 2 mm span, which
  the card says on screen and `warnings` repeats. The FIT is still the
  single-span `fit_sag_coeff`; the procedure's log-log span EXPONENT and its
  droop table are not built. GUI: `sw.sag_*` (gui_sweeps `_sw_build_protocol_sag`
  + `_sw_update_sag_readout`), `_PROTOCOL_NAMES["sag"]`, SECTION_HEADERS
  `sag_ladder`. `test_gui_calibration.py` + `test_calibration_procedures.py`.
- **`generate_lag_tube(LagTubeParams)`** — the SIXTH experiment (2026-09-25), the
  "Lag radii (4-axis)" tab's own print (`print_protocol="tube"`): paper section
  2.4.6's calibration tube. A lead-in helix, then one band per winding angle
  (20/45/70 + the 90 check by default, 13 mm bands 1 mm apart, 1 mm spacing: 48 mm,
  sized to the shipped 4-axis machine's 50 mm rod; the paper's 0 check is optional): N parallel lines in alternating
  directions joined along the band edge. `tube_band_layout` is the ONE layout
  (generator, fit card, summary): `lines` None = the even count closing once
  round (`_tube_band_count`, spacing widened, never narrowed); `lines` given =
  exactly that many at exactly the spacing, warned when they overlap. An even
  band leaves by retracing line 0; 90 deg is rings. Fitted bands are
  `band_len` (13 mm; a shallow band reads LOW until ~8 lag lengths,
  `SETTLE_LAG_LENGTHS`); the 0/90 CHECK bands are a fixed `CHECK_BAND_LEN`.
  The tube is NEVER shrunk onto the rod: too long, it warns with the length it
  needs. `lag_tube_length` is the closed-form length the page places it with.
  `tube_mid_gaps` reads a band's crossings at its middle and the gaps across the
  lines (print order from line 0, the retrace dropped). GUI: `sw.tube_*`
  (`_sw_build_protocol_tube`: Winding angles, Line spacing, Lines per band, Band
  length, Gap between bands, Print speed (mm/min), Lead-in turns + the rod rows
  + a read-only Mandrel CTS line from `m_cts`), `_sw_tube_params`,
  `_sw_tube_place`, `_sw_extract_tube` (parse through the rotary frame -> relay
  WITH the frame -> wrap; also returns the unrolled `depo_uv`). **"Where to
  measure"** is `gui_tube_map.TubeMeasureMap`, under the steps in the
  Instructions box (`state["tube_map"]`, built in `_sw_show_tube` by
  `gui_tube_map.band_view`): the bands unrolled, and a close-up of one band's
  middle with a wide and a narrow gap bracketed. The lag4 fit card
  (`_build_fit_mandrel_lag`) is the gap table only, 0/90 rows marked as checks;
  `_fit_lag_design` reads the mandrel, gap, speed and CTS from where each lives.
  NO CTS ladder on the tube: the mandrel CTS is the CTS (4-axis) tab's.
- **`generate_cts_mandrel(CtsMandrelParams)`** (kind `"cts4"`, 2026-09-25) — the
  **CTS (4-axis)** tab's print (its own tab and protocol since the Collector switch
  on the CTS tab went): the slide ladder and reading, each speed a short meander of
  AXIAL lines along the top of the rod, speeds stepping along the axis, the run
  between them through the gap (odd reps) or along the underside (even). Its own
  `sw.cts4_*` keys (`gui_sweeps._sw_build_protocol_cts4`); an old one-tab preset is
  split by `gui_presets._migrate_legacy_cts_collector`. Rides the lag tube's rotary
  plumbing (`_SW_ROTARY_KINDS`); the preview draws it ON THE ROD, as the lag
  tube's (`_sw_show_tube`). Both CTS tabs take "Measured CTS" in mm/min and
  convert on Apply; this one writes `m_cts`, never `cal.cts_ref`.
- **Shared print cards per tab** (2026-09-25): `calibration_procedures.Procedure.
  shared_cards` lists the shared sections a tab shows; `gui_calibration.
  _cal_show_shared` hides the rest. The two mandrel tabs take `MANDREL_CARDS` (no
  Plate & feed limit, Stabilization scaffold or Target slide).
- **MANDREL-ONLY CALIBRATION** (2026-09-25). A mandrel Apply never touches the flat
  profile: the lag tab writes `m_lag_axial/_circ/_speed`, CTS (4-axis) `m_cts`
  (`gui_mandrel._MANDREL_CAL_VARS`, folded into a 4-axis run's profile by
  `_mandrel_cal_extras`). A ROTARY lay runs on `mew_lag_fit.mandrel_model` --
  amplitude R_ax / L(eps_cal), CTS the mandrel's -- in `make_transform` and the
  player's live march; a flat lay never does. The Physics page's Jet lag card
  shows and edits the four values and what is in use (`gui_pages._phys_mandrel_lag`).
  `test_lag_tube.py` (19) + `test_gui_calibration.py` + `test_calibration_procedures.py`.
- **`generate_circles(CircleParams)` + `analysis/mew_circle_fit.py`** — the THIRD
  experiment (2026-09-03): circles that calibrate the flat lag AMPLITUDE `k`,
  the 3-axis analogue of the mandrel's micrograph inversion (§2.11). Protocol:
  `PHYSICS_MODEL_ADDENDUM.md` §A.4a in the paper repo.
  Four things to keep:
  1. **`mew_circle_fit` owns the law, both ways.** The shipped ray rule's steady
     state on a circle is `R*sqrt(1-rho^2) = k*H*A*(1-(rho*eps)^-b)` — the
     contact runs a SHORTER circle than the nozzle, so it holds the lag of its
     own reduced speed. CTS cancels, `k` is linear, so the inversion is closed
     form. Cao Eq 11 is its `L/R -> 0` limit, **not** the same relation. The
     generator sizes through `commanded_radius`, with `mew_circle_fit`'s
     constants, never this module's `_A`/`_B` (they agree to ten digits and not
     to the last bit, and the squares' byte-identity is why `_A`/`_B` stay).
  2. **Nothing may cross a measured arc.** Continuous-flow MEW lays fibre on
     travels, so this is the measurement, not cosmetics, and it is what each
     layout's geometry is FOR. `chain`: every circle in a row is tangent to the
     same straight line, so that line is the run-up, the connector and the next
     lap's run-up at once. `nested` (the default): a ring is entered along its
     own tangent, whose start is clamped inside the ring just finished, so the
     connector between rings is exactly RADIAL — and the head leaves a stack by
     RETRACING the way it came, because the innermost ring's exit is inside
     everything and any straight line out crosses it. `test_circle_sweep.py`
     checks every emitted move against every circle in both layouts and carries
     the RED control for that check.
  2b. **Nesting is what sets the default lag/radius ratio.** The ε-flatness
     check has a floor — run the ray rule's own circles through the retired
     filter's inversion and the recovered k trends +18.9 %/decade at 0.45,
     against +10.5 % for a CTS believed 5 % low, so the check is blind there.
     The floor falls to +5.7 % at 0.25, which is unaffordable flat (6 slides)
     and 2 nested. `mew_circle_fit.ambiguity_trend` computes it; `fit_amplitude` reports
     the observed trend against it.
  3. **The stabilization base is what limits the plate**, not the circles: its
     real bbox is 19.9 mm with the turnaround balloons, and a 24 mm circle plus
     a 20 mm base does not fit in 42.8 mm of usable height. `_pack_circles`
     therefore tries four base placements per slide and keeps the best.
  4. **`Calibration.lag_amplitude_gap`** records the gap `k` was fitted at, and
     since 2026-09-03 the lag3 card's Apply WRITES it alongside `k` (it was
     write-only before). **As of 285bce8 nothing in `physics/mew_physics.py` reads it**:
     the field is declared at `physics/mew_physics.py:222` and the law uses
     `lag_amplitude` with the gap divided out (`:210`), so it is a record of the
     fit, not an input. The `gui_calibration` docstring's "read by mew_physics
     (:548)" predates that.
  5. **The specimen TABLE** (2026-09-03). `CircleParams.specimens` is a list of
     `CircleSpec(eps, reps, radius_mm)`; `None` (the default) keeps the
     `eps`/`reps`/`reps_eps` path byte for byte, a list is instead the whole
     truth and those three are ignored. `circle_table(p)` returns the rows the
     generator will lay with every radius RESOLVED, so feeding it back as
     `specimens` is a fixed point — which is what lets the GUI show the radius
     before it is printed and edit it in place. `specs_to_str` /
     `specs_from_str` are the one-string form (`"2:2, 3:2, 4:2:12.5, 6:2"`)
     the page persists as **`sw.circ_specimens`**; blank maps to `None`, never
     to an empty list, which the engine rightly refuses. `specs_from_str`
     RAISES rather than skipping a row it cannot read — a silently dropped row
     is a specimen that quietly stops being printed. The lag3 print block draws
     it as a table (`_sw_circ_refresh_table` / `_sw_circ_read_rows` /
     `_sw_circ_add_specimen` / `_sw_circ_remove_specimen` /
     `_sw_circ_reset_table`), replacing three fields that could only say the
     same thing about every specimen. A radius cell is an override when its
     text differs from what the page last wrote there **or** when a sticky
     per-row flag says it already was: `circle_table` echoes an override back
     into its own cell, so the text comparison alone handed the ring back to
     the sizing rule one refresh later (measured: pinning ring 1 to 12.5 mm and
     pressing Add row rewrote the key from `"2:2:12.5, 3:5, 4:2, 6:2"` to
     `"2:2, 3:5, 4:2, 6:2, 7:2"`). "Reset to law sizing" clears the key.

  The **lag3 card** is `fit_amplitude`'s GUI home (typed rows or "Load CSV…"), and its
  Apply writes both profile values; `python analysis/mew_circle_fit.py <plate>_measure.csv`
  remains the equivalent command line.
  `test_circle_sweep.py` (28) + `test_mew_sweeps.py`.
- **`gui/gui_sweeps.py`** — `_SweepsMixin` (the print ENGINE + the field builders the
  cards mount: `_sw_build_protocol` (cts / circle knobs), `_sw_add_print_cards`
  (Plate & feed limit incl. machine max-feed, the Stabilization scaffold card,
  the Export card = CSV opt-in + the designer's file options + Target slide
  pager, and the Quality card), `_sw_add_presets_card`,
  `_sw_build_warnings` (the red readout under the grid), and every generator
  knob as a field (`sw.*` persist keys via `_sweeps_persist_entries`; no-op
  default in `gui_core`). **`_sw_build_preview_options`** puts the display
  toggles on the preview panel's header row, the Scaffold designer's
  `_OptionFlow` shape (Plate / Deposited / G-code path / Travels / Labels / 3-D
  tubes / **Physics** = `sim_physics_deposit` + a colour swatch), and
  `_sw_subtitle` keeps the header to one short line. The preview shows
  **EVERY slide at once, tiled** in a near-square grid (`_sw_show_all` /
  `_sw_tile` — each slide's plate, base, specimens, travels and labels offset
  into its own cell), so an overflow to N plates is visible in one view. (In the
  modern UI the pager is gone: Calibrate shows every slide in one document,
  above.) 350 ms debounce → one worker (which relays the physics touchdown path
  via `mew_deposition` when the toggle is on) → latest-only apply. **The page has
  NO in-form action buttons — it drives the shared modern-shell bottom bar like
  every other design page** (`gui_shell` makes `"sweeps"` a real input mode):
  **Generate** (`_sw_generate`) builds the TARGET slide's STL/`.depo` off-thread
  (like `_build_exact_stl`, via the SHARED `_export_prefix`) and feeds
  last_gcode/last_stl/last_depo → **Save STL as…**, **Save g-code as…** and the
  Simulation page lights up; **Save g-code as…** is routed to
  `_sw_export_gcode_dialog` (batch-writes every slide + the optional CSV, since a
  sweep is N files). **`SweepPreviewPanel`** (subclasses `gui_preview.PreviewPanel`:
  adds tiled PLATE face+edge actors + an accent-edge actor for the target plate,
  the TRAVEL strand actor, and floating **billboard ε/feed labels** per specimen;
  toggles are pure restyles via `sync_visibility`). The jet-lag section adds the
  **Layout** + **Print order** dropdowns (`_sw_combo_row`). (The jet-lag SQUARES
  and their per-square Positioning-page pins were removed on 2026-09-03 with the
  page merge: `sw.lag_placements` no longer exists, `mew_sweeps.generate_lag` is
  unreachable from the UI, and a stale preset's `"lag"` kind degrades to the CTS
  sweep. The Positioning page itself went on 2026-10-31, §2.16.) Snapshot robustness (`_sw_snapshot_params`): off/auto fields
  (Fixed side, Side per ε, Origin) read blank as 0 (`Fzero`); required blanks
  raise a **named** error (`_SW_FIELD_LABELS`), not `float('')`. A **preset load**
  first resets the active experiment-kind's field group (`circ_*` / `cts_*`) to
  factory defaults, THEN applies the preset dict — so a preset is a clean recipe,
  not a partial overlay on stale edits. (The nine named "Protocol preset" chips
  were deleted on 2026-09-03; their values survive as the field defaults, which
  `cards_open_at_the_protocol_defaults` pins.)
  **Generate** rebuilds the target slide synchronously first if the debounced
  preview is dirty/pending (never exports a stale slide), and its off-thread build
  registers as `self.proc` so the shared bottom-bar **Cancel** can kill it.
  `test_gui_calibration.py` (35) + the `test_ui_shell` presence guard
  (calibration is an input mode) + the parity test's sanctioned `sw.` carve-out.

  **Two path sets, two toggles.** `SweepPreviewPanel` draws the DEPOSITED fibre
  (`self._actor`, `sw_show_depo`) and the COMMANDED toolpath (the inherited
  `_gc_actor`, `sw_show_gcode`, via a `_gcode_enabled` override) independently.
  Before 2026-09-03 the box named "Show g-code" was bound to the fibre actor and
  the overlay was dead, because the page's meta never carried `gcode_paths`;
  `_sw_extract` now snapshots the commanded path before the touchdown relay and
  `_sw_show_all` puts it in `meta["gcode_paths"]`.

  **Nothing the app already owns is a field here.** `_sw_gap` ← `sim_standoff`,
  `_sw_resolve_cts` ← the calibration anchor, `_sw_plate_size` / `_sw_origin` ←
  the Build-plate page's `plate_x/y` + `plate_corner_x/y`, `_sw_diam_mm` ← the
  scaffold designer's diameter (the PHYSICS-PREDICTED one when physics-deposit is
  on). Each is read at snapshot time. The page-local `plate_w/h`, `origin_x/y`
  and `diam` copies, the nine `_PRESETS` protocol chips (whose values survive as
  the field DEFAULTS) and the orphaned `cts_*_dx/dy` placement offsets are gone.
  The experiment kind now dispatches through **one** `mew_sweeps.generate(kind,
  params)`: three call sites (the debounced worker, the synchronous Generate
  path, Positioning) and two of them had been two-way `if lag else cts`, so a
  third kind fell through to the CTS generator. `GENERATORS` maps a kind to a
  function NAME, resolved at call time — holding the object froze it at import
  and made the page suite's worker stand-in invisible.

### 2.16 Positioning page — **REMOVED 2026-10-31**   *(tombstone; kept so section numbers stay stable)*

`gui_positioning.py` and its `pos.*` persist keys left with the top row (§2.12). A scaffold's
place is its `start_x/y` field on the Scaffold designer and the Move tool on the CAD page
(§2.23). A preset that still holds `pos.*` keys loads, and they are ignored.

### 2.17 Print History page

- **What:** a lab-notebook layer. Every Generate is auto-recorded; the good runs
  get promoted into a curated saved log; a run opens a **detail sheet** (double-click
  / Preview) showing its thumbnail + **headline** parameters + notes; it reproduces
  (re-arms the fields), diffs against another, is edited (saved runs), and exports as
  a compact CSV or **1-2 page PDF run sheet**.
- **`analysis/mew_history.py`** — the headless engine (NO Tk/VTK), unit-tested by
  `test_mew_history.py` (29). `HistoryStore(base_dir)` over a git-ignored
  `history/` dir: `runs.jsonl` (append-only, `load_runs()` newest-first, corrupt
  lines skipped; `prune(now)` drops UNLOGGED runs older than `RETENTION_DAYS=30`
  then any beyond `RUN_CAP=500` — undateable lines are kept, never lost;
  `delete_run(id)` removes one), `saved.json` (`promote`/`annotate`/`update_saved`/
  `delete_saved` — a FULL deep copy so a promoted run survives a broad-history trim
  and is NEVER auto-pruned; opt-in `gcode_src` archives into `saved_files/<name>.gcode`
  where `<name>` is the sanitised TITLE, or the run id when untitled, uniquified),
  `thumbs/<id>.png` (kept even past prune — a saved run may still reference it). **`HEADLINE_SECTIONS`** is the
  curated set that reaches a sheet (Process: speed/CTS/pressure/voltage/gap/temp/
  **humidity**/nozzle; Scaffold geometry: layers/W/H/fibre-diam/rotation) — `headline_rows(rec)`
  pulls them across all groups; `set_param(rec,key,val,add_if_missing=False)` edits one (the full 400+
  snapshot stays for Reproduce and is NEVER dumped to a sheet). **`MANUAL_HEADLINE_KEYS`**
  ({`env.humidity`}) are headline keys with NO capture field — manual lab annotations
  (blank on every auto-recorded run); the edit dialog (saved-log runs) always offers
  them and `set_param(...,add_if_missing=True)` lands them in the scaffold catch-all
  group. `diff_runs(a,b)` →
  `{changed/added/removed}` over the four `PARAM_GROUPS` (full, not headline).
  `export_run_csv` (stdlib, headline+meta+note) + `build_run_sheet_pdf` (OPTIONAL
  reportlab; thumbnail at TOP only if it exists, then headline tables + note;
  `HAS_REPORTLAB` guard raises a plain-language `RuntimeError` when absent — the
  GUI greys the button). **`new_manual_record(params,title,mode,result,note,now)`**
  builds a MANUAL run record (same shape a captured run has, `mode="manual"`,
  `status="manual"`, `manual=True`) so a print done outside the app can be logged by
  hand — blank params are dropped (fewer headline rows) and an all-blank entry still
  records. **`config.json`** holds a tiny per-user page preference blob, deliberately
  OUTSIDE the GUI preset persist map (a UI preference, not a scaffold parameter):
  `capture_enabled()` / `set_capture_enabled(on)` (default on; missing/corrupt →
  default) back the auto-record toggle.
- **`gui/gui_history.py`** — `_HistoryMixin` (composed into `App` in
  `gui/gcode_to_fibers_gui.py` alongside `_DeviationMixin`). **Capture** rides the ONE
  Generate choke point: `_history_on_run_start(mode,args)` snapshots the fields via
  `_snapshot_for_kind` for each of scaffold/machine/calibration/sweep (the same
  grouping the preset machinery uses — the four kinds partition the persist map);
  `_history_on_run_finish(status,exit_code)` finalizes status + output basenames
  and, when the run produced an STL, fires a fire-and-forget
  `worker_cmd("worker:viewer", stl, "--thumbnail", ...)` subprocess into the
  persistent `history/thumbs/` (session outputs are swept on close, so the thumb
  must render now). Both are called from `gui/gui_run.py` (`run()` / `_on_finished`)
  behind `hasattr` guards, so the capture is UI-agnostic (fires in classic + modern). **Latency discipline:** the start hook
  runs AFTER `progress.start()`, the git build-hash lookup is done ONCE in a
  background thread (a `git` spawn can stall seconds on Windows/Defender), and
  `reportlab` imports lazily inside `build_run_sheet_pdf` (off app startup —
  `HAS_REPORTLAB` is a `find_spec` probe) — so capture adds ~2 ms to Generate.
  A run's **Title** is blank until the user names it on save; **Result** (the list
  column, renaming `status`) shows the user's edited value or the auto
  ok/failed/cancelled. **Reproduce** = `_apply_preset_for_kind`
  per kind (the preset-load path — `_loading_preset` muting + per-kind re-sync come
  free). The tab: a segmented **History ⇄ Saved log** toggle over two STACKED tree
  frames — **NOT an inner ttk.Notebook** (`test_ui_shell.no_inner_notebooks` forbids
  any notebook but `self.nb`) — a lean list bar (**New** / Preview / Reproduce /
  Diff (2) / Save to log… / Delete / Refresh) and NO side preview panel, plus a
  top-bar **Auto-record new prints** checkbox beside the view toggle. The **Saved-log
  view is split in half vertically** (`_hist_make_saved_pane`, `uniform` grid rows —
  History view stays a plain full list): the runs list on top, a **time-series graph
  panel** below (`_hist_build_graph_panel`) — a Process-variable dropdown
  (`mew_history.process_headline_keys`) + **Graph** button over an embedded figure.
  `_hist_graph_selected` pulls `mew_history.series_points(selected, key)` (→
  time-sorted run-identified points — `{t,v,id,title}`, blank/non-numeric/undateable
  runs skipped; `series_for_key` is the `(t,v)` projection) into
  `analysis_plots.history_series_figure`, embedded via `FigureCanvasTkAgg` (the
  optional-matplotlib plumbing — `_HAVE_MPL_TK`; absent → a hint, list
  still works). **Click a point** → `_hist_graph_click` finds the nearest point by
  pixel distance and names its run (on-plot callout + status line). The saved view has
  a visible **"Sort by" dropdown** above the list (`_HIST_SORT_MODES` →
  `_hist_apply_saved_sort`: Most recent / Oldest first / Title / Result; default
  most-recent, re-applied on every `_hist_refresh`); the runs trees ALSO have
  click-to-sort column headers (`_hist_sort_column`, toggles asc/desc). **New**
  (`_hist_new_manual`) opens an all-optional manual-entry dialog (title/mode/result/
  note + the headline params); `_hist_create_manual` drops blanks, distributes the
  params into the four `PARAM_GROUPS` via `_hist_group_params` (the app's own
  `_keys_for_kind` ownership, scaffold catch-all fallback), appends, and selects the
  new row. The checkbox gates `_history_on_run_start` (off → no `_pending_history`,
  so finish is a clean no-op) and persists via the store's `config.json`. **Delete**
  works on the SHOWING list — `delete_run` for unlogged history, `delete_saved` for
  the log. Double-click (or Preview) opens
  `_hist_open_detail` — a scrolled sheet (thumbnail + headline params + notes) whose
  own bar carries **Edit** / Reproduce / Export CSV / Export PDF.
  **Edit** applies to SAVED runs only (`_hist_edit` → `update_saved`; on a
  broad-history run it offers to promote first) and edits the note/tags/operator +
  the headline values. In-tab/in-dialog buttons are correct here (a Results page) — the shared-bottom-bar rule binds only Generate pages.
- **Thumbnail** rides a physics-free `stl_viewer.render_thumbnail` + `--thumbnail`
  CLI (shared file — offscreen fibres-only render, camera along the ViewCube
  front-face top-right corner `(1,-1,1)`; `test_thumbnail.py`).
- **Mount:** `gui/gui_shell.py` registers `("history","Results","🗃 Print history",…)`
  right after the Viewer pages (no-op-when-absent posture). **No `_persist_map` fields**
  → the persist-map parity guard is untouched (the view toggle is session-only; the
  auto-record toggle persists in `history/config.json`, NOT the preset map, so it
  stays clear of the parity guard). `test_gui_history.py` (9): capture
  round-trip (ok/failed/no-pending), the capture toggle gates recording, reproduce
  applies a snapshot, the tab builds + the detail sheet opens, a manual `New` entry
  appends + drops blanks + groups params, an edit updates a saved run's note +
  headline value, and the end-to-end capture→real-PNG thumbnail.

### 2.18 Machine link page (live printer link)
The live digital-twin link: connect to a real MEWron's **Klipper/Moonraker** over
ethernet to watch the machine move, send prints, set temperatures, jog, browse/
download/delete stored g-code, and stream the console — a NON-input page in a new
entry under the top row's **More** menu.

- **`machine/moonraker_link.py`** — headless, **stdlib-only** (`urllib`, no `requests`/
  websocket; the repo had zero network code before this). `MoonrakerClient`
  (`query`/`gcode`/`upload`(+start)/`print_*`/`emergency_stop`/`gcode_store`/
  `proc_stats` + `discover_heaters`/`discover_sensors`/`heater_limits` +
  `list_files`/`download_file`(raw bytes)/`delete_file`; optional `X-Api-Key`). `LinkSnapshot` (one telemetry frame; slow fields carried forward) +
  `LinkPoller` (daemon thread, tiered fast/slow/safety cadences, capped-backoff
  reconnect, `queue.Queue` handoff). `TelemetryRecorder` (append-only CSV).
  **`FakeMoonraker`** — a stdlib `ThreadingHTTPServer` rig (scripted moving toolhead,
  recorded scripts + uploads, api-key/flaky modes) that backs the tests AND runs
  standalone via **`python tests/test_moonraker_link.py --serve`** so the GUI can hit
  `localhost` with no rig. Multipart is hand-rolled (`cgi` gone in 3.13). Frame note:
  `motion_report.live_position` is in Klipper g-code coords (same frame the export
  commands), so the live view reuses the sim's `--origin`/`--machine`; `homing_origin`
  is surfaced for rig-day alignment. **HV/pressure are NOT Klipper objects on this
  rig — the poller is discovery-driven, so they appear only if ever added to the cfg.**
  Test: `test_moonraker_link.py` (14).
- **`viewers/gcode_player.py --machine-live`** (in the player, §2.9) — the live 3-D
  view is a MODE of the player, not a new viewer: renders ONLY the machine bodies
  (+plate), moving from `{"t":"livepos",x,y,z,vx,vy,vz,state,progress,spd}` messages
  fed over the embed channel; NO g-code/deposition/jet/clock. `__init__` forces
  physics + live-adjust off and skips the parse/jet/relay via a degenerate origin path
  (keeps the `n_pts>=2` guards happy); `_run_machine_live`/`_tick_machine_live`/
  `_place_machine_live` dead-reckon from `live_velocity` (capped `_LIVE_EXTRAP_CAP`
  0.5 s) into `machine_model.place_scene`; View-only chrome (Snap top-down / view cube
  / plate) + a 6-line HUD (state+progress / X / Y / Z / v); `_frame_camera` frames the
  visible machine (`ComputeVisiblePropBounds`). `--machine-live` requires `--machine`;
  the g-code positional is now optional. Reuses `_build_machine`/chrome/ViewCube/embed/
  native-FS wholesale. Test: `test_machine_live.py` (4, reuses `test_viewer_embed`'s
  child harness).
- **The live scaffold reveal** (`--machine-live <gcode> --reveal-scaffold
  --reveal-gcode`) — given the file the printer is ACTUALLY running, the same mode
  lays the scaffold under the live nozzle. It is an ordinary player scene (real parse,
  real `.depo` mesh, real jet, `_apply_time` unmodified) with exactly two swaps: the
  bodies are placed from the live feed instead of the toolpath head, and `self.t` comes
  from the printer's **file cursor** instead of a clock.
  - **The cursor is `virtual_sdcard.file_position`** → `line_byte_starts`/`byte_to_line`
    → `line_to_time(seg_line, …)` (`_advance_live_cursor`). Klipper advances that byte
    offset by `len(line.encode()) + 1` per dispatched line, so the map is its own
    arithmetic inverted, and the line number is a **join key, not a clock** — `t` lands
    in the player's own time base, so the two clocks never need reconciling. Three
    earlier cursors each failed on the rig while passing offline: nearest-XY projection
    (ambiguous across stacked rotated layers → ratchets to EOF), `print_duration` (a
    different clock, different origin and rate), `progress` (a byte fraction).
  - **Residual lead.** `file_position` is the READER, ahead of the toolhead by
    Klipper's `buffer_time_high` plus one whole move. `_track_nozzle` shaves that: the
    playhead is a **clock at 1× wall time** that folds in `_project_head`'s match of
    the rendered head against the path — searched over the WHOLE backward
    `_live_lead_max` bracket, gated only on travel direction — as a
    **measurement** at `_LIVE_TRIM_GAIN`, rate-limited by `_LIVE_SLEW_FWD/BACK`. A
    complementary filter: wall time supplies the high frequencies (which is what
    animates a long pass, since `file_position` does not move for its whole duration),
    the projection the low ones, and a wrong match nudges instead of teleporting.
    **The lead is never modelled** — `t_read` is only a hard ceiling and the bracket
    that makes the match unambiguous — which is why a 10× swing in the reader's lead
    moves the worst playhead error by 0.000 s.
  - **The bracket is DERIVED PER FILE** (`_derive_live_bracket` + `_layer_duration`,
    2026-08-11), not a constant. It must be wider than the reader's lead (else the
    FLOOR `t_read − bracket` overtakes the nozzle and the clamp drags the reveal
    into the turnaround the head has not reached) and narrower than a layer (else
    the projection resolves to an earlier, near-identical one). The lead scales with
    the longest MOVE and the ambiguity risk with the LAYER, so a flat 6 s could not
    serve both: on the `scaffold reveal bug` preset real Klipper's lead peaked at
    **7.05 s** against that 6 s bracket, and the reveal entered a linking loop 678 ms
    early with 17 playhead jumps past the 3× rate limit. `_LIVE_LEAD_FLOOR` (6.0 s)
    is the old value and the derivation may only **widen** past it, so every existing
    fixture keeps the bracket it had. The clamp now runs **before** the rate limit,
    so a bracket violation is walked to rather than jumped to. Swept over 7 footprints
    × 4 layer rotations in `test_machine_live_footprints.py` (brackets 8.1–11.3 s;
    0 ms pre-reveal, 0 teleports everywhere), whose `flat_bracket_cursor` re-runs the
    old design as the red half rather than remembering its numbers.
    - Until **2026-08-08** this was `_retard_to_nozzle`, a per-frame nearest-point
      search inside a ±`dt*4` window centred on the playhead's OWN previous value.
      That feedback is what made a slip permanent: the playhead ended pinned at the
      bracket floor, frozen on 85.5% of frames and 4.32 s from the true nozzle, which
      is the "each pass pops in when its move ends" symptom. Fixed measurements and
      the rejected alternative (collapsing the lead to one damped scalar — refused,
      the virtual MEWron's lead varies 3.1× within a run) are in the comment block
      above `_LIVE_LEAD_FLOOR` (`viewers/gcode_player.py:6542`; the bracket is now derived per
      file by `_derive_live_bracket`). Guarded by `test_machine_live_replay.py`, whose
      acceptance metric is `t_true` — **not `layer_error`, which compares the playhead
      to the reader and so scores a cursor that copies `t_read` a perfect zero.**
  - **Clear scaffold** — `{"t":"reveal", clear:true}` → `Player._clear_scaffold`
    (`gui_machine_link._ml_clear_scaffold`). Unlike the checkbox, which hides a mesh it
    keeps built, this REMOVES the actors and discards the mesh (next enable re-solves
    the sag) and calls `_reset_reveal`.
  - **The ratchet only runs while a print is live** (`_live_tracking`, set/cleared in
    `_advance_live_cursor`). The idle view places the WHOLE path at `t = total`; a
    high-water mark taken from that pass used to pin the reveal at end-of-file for the
    session — the "whole scaffold appears at once" bug.
  - **Frames:** nozzle/bodies stay on RAW `live_position`; only FILE-frame reveal
    geometry takes the work offset **H** (`moonraker_link.work_offset` = `gcode_move.
    position - gcode_position`, the commanded pair — NOT `homing_origin`, which reads
    ~0 on this G92-zeroed rig). `_bed_actors` (origin triad, plate/scaffold STL) take
    the plain bed delta; `_reveal_actors` + `depo_actors` + the jet contact take
    `_overlay_delta` (delta + H + a 0.05 mm `_LIVE_OVERLAY_LIFT` that kills the Z-fight
    with the bed's top face — the "path fades when I zoom in").
  - **Not printing shows the WHOLE path** (`t = total`), so placement is always
    checkable; `printing`/`paused` reveal progressively. Revealing only while printing
    read on the rig as "I can't see the g-code at all".
  - Test: `test_machine_live_reveal.py` (15) — incl. `real_gui_embed_path`, which
    drives the actual ModernApp page against `FakeMoonraker` with a REAL embedded
    child. That path being untested is why three attempts shipped broken.
    **`every_pass_reveals_progressively` is left FAILING on purpose** (see the
    `KNOWN-UNSOUND` block): the playhead steps rather than sweeps, and weakening the
    test would hide it. Note also that these replays feed an idealised machine
    (position exactly on the commanded path, on the player's own timeline) — they
    cannot reproduce the rig, and fixes have verified green here and failed on
    hardware. Replay a recorded `TelemetryRecorder` CSV instead.
- **`gcode_player._LiveFeed`** — the pure TRUST layer under `--machine-live` (no VTK/
  Tk, injectable clock; `Player` only delegates). `live_position` is a coordinate in a
  frame Klipper REDEFINES: a `G28` calls `set_position` before each endstop-seeking
  move and at the trigger, so the sample steps outside the travel volume while nothing
  moves — drawn as travel that was 647.9 mm at 231-449 mm/s over one homing burst (the
  printhead yank; the bed through the head is its 175 mm Z leg). `_LiveFeed` HOLDS on a
  `hold` flag from the page, re-seats on a feed gap > `MAX_SEG` (interpolating across a
  dropout renders a 7.9 mm frame at 0.5 s / 67.9 mm at 2 s — the 'first jog after idle'
  yank; `EXTRAP_CAP` cannot catch it, that guards only PAST the newest sample), and
  closes the gap at a bounded `CATCHUP_SPEED` (80 mm/s). Rate-limited, NOT a timed
  glide: a fixed-duration blend renders distance/duration, so a 160 mm re-seat over
  0.35 s draws the same yank, and it cannot converge on a still-moving target.
  Gate + travel-volume detector live in `gui_machine_link._ml_motion_gate`;
  `MoonrakerClient.axis_limits()` supplies the volume. Test:
  `test_machine_link_yank.py` (6, incl. the real ModernApp page + `FakeMoonraker`
  replaying the captured homing, with the un-gated baseline asserted so it cannot pass
  vacuously).
- **`gui/gui_machine_link.py`** `_MachineLinkMixin` — the page: a `PanedWindow` with a
  left control stack (`VScrollFrame`) + the embedded `machine` `ViewerPanel` (the same
  chrome the Viewer page uses; ⛶ native fullscreen for free). Sections: Connection card
  (host/port/api-key + Connect/Disconnect + always-visible **E-stop** + a **Door unlock**
  button firing the rig's custom `DOOR_UNLOCK` Klipper macro, GPIO21), Status &
  progress (abs read-only + **editable work (rel) X/Y/Z boxes** — type a value + Enter
  to move that axis to it via an absolute G1 in the gcode frame, `_ml_move_rel_script`;
  live telemetry never clobbers a focused box; moves blocked mid-print; + pause/resume/
  cancel — see **pause/resume** below), Send a print (upload; +start behind a confirm; a
  **Name / Rename** box stores it on the printer under a chosen `.gcode` filename,
  sanitized via `_ml_sanitize_filename`, blank = the generated basename), **G-code
  files** (a paged Treeview of the printer's stored files — `list_files` on connect /
  after upload / after delete; **Prev/Next** page x of y at `FILES_PER_PAGE`=10;
  Download… saves the raw bytes, Delete behind a confirm; a stale list that outlives
  its session is dropped),
  Temperatures (rows built from discovery, setpoints clamped to `max_temp`, + a
  dependency-free `_TempGraph` tk.Canvas strip chart of temperature/°C + heater PWM/%
  since connect — snapshots carry `powers` from a `power` query field; the x-axis is
  **wall-clock** time (e.g. `11:47am`), the clock resets on each connect, and the chart
  follows the modern light/dark theme via `gui_widgets.register`/`palette` + a `retheme`),
  Jog & home,
  Overrides (M220/M221), Console (tail + entry), Telemetry recording (CSV), Safety
  (proc_stats throttle flags). Connect spawns a probe thread → `LinkPoller`; a
  `root.after` pump (`_ml_pump`, the gui_run thread→Queue→after pattern) drains snapshots
  → updates the readouts, feeds the recorder, and forwards `livepos` DOWN to the panel
  (`send_child`). **Nothing blocks Tk** (connect/gcode/upload/estop all on short
  threads). Pure g-code builders (`_ml_jog_script`/`_ml_home_script`/`_ml_temp_script`/
  `_ml_override_script`/`_ml_door_unlock_script`/`_ml_move_rel_script`) keep the emitted
  strings unit-testable. **Safety:** nothing runs
  on connect; upload-only default; Upload&start / Cancel behind a confirm; E-stop is one
  click (no confirm); temps clamped. Test: `test_gui_machine_link.py` (15).
- **Pause / resume** (`gui_machine_link`, the "print control" section) — the one pair
  that is NOT a thin POST, because Moonraker's `/printer/print/{pause,resume}` run the
  **g-code commands named `PAUSE` / `RESUME`**, whatever those are on that machine
  (usually `[gcode_macro]` wrappers). Two symptoms were reported off mewron04
  (2026-07-24) — every Pause press raised Z 2 mm, Resume did nothing — and since
  nothing in the page moves Z, both belong to the rig's own commands. Three guards:
  (a) the lift is the rig's PAUSE macro and nothing lowers it — the pause
  records the work-frame Z (`_ml_read_work_z`, read *before* the macro can move it) and
  the resume puts the head back with an absolute `G1 Z`, because on a MEW rig the
  nozzle-collector distance IS the process; (b) that macro **re-fires on every press**,
  so each extra click climbed another 2 mm — `_ml_update_print_buttons` greys Pause the
  moment the printer reports paused (Pause/Resume follow the JOB, not the connection;
  Cancel stays connection-gated as a safety control) and `_ml_pause` refuses anyway,
  since the button is up to one poll behind; (c) a resume can **fail silently** —
  Klipper's `RESUME` declines with a console line and an HTTP 200 whenever
  `pause_resume.is_paused` is false (exactly where a job paused by a bare `M25` sits),
  as does a wrapper macro whose guard never fires. So `_ml_resume_worker` **escalates**
  (`_ml_resume_attempts`: `RESUME` / raw `M24` / the renamed builtin discovered from
  `/printer/gcode/help` by `_base_print_commands`, ordered by the observed
  `is_paused`) and **verifies** each attempt against a fresh `print_stats.state` read
  (`_ml_wait_state`) instead of trusting the 200; every attempt is logged to the console
  mirror and the verdict goes to a `sticky` status line (`_ml_say`, which the 4 Hz
  snapshot handler would otherwise overwrite within 250 ms). `pause_resume.is_paused`
  rides the snapshot (`LinkPoller(pause_resume=…)`, enabled only after
  `MoonrakerClient.has_object` confirms it — **Klipper fails a whole combined query on
  one unknown object**), and a pause Klipper does not own reads `state paused (sd)`.
  Tests: `test_gui_machine_link.py` (the three shapes, incl. the +2 mm-per-press
  reproduction) + `test_moonraker_link.py` (the fake rig models all of them:
  `pause_resume` / `resume_macro_broken` / `base_commands`).
- **Wiring** (mirrors §2.15/§2.16): `mewracle_gui` MRO adds `_MachineLinkMixin`;
  `gui_shell` registers the page in the **Print** section via the tab-count-delta trick
  (like Print history) and joins `_noninput_frames`; `gui_embed` supplies the `machine` panel
  role via `_make_embed_panel`; `gui_home` "More tools" card.
  **No persist-map keys** — host/port/api-key live in a per-user `machine_link_prefs.json`
  (the `gui_prefs.json` pattern, gitignored), so `test_ui_shell`'s persist-parity guard
  is UNCHANGED (only its embed-panel enumeration grew `{unified}` → `{unified, machine}`).

---

### 2.19 Visual inspection (looking at the app and at a result)

Two offscreen, window-free ways to turn what the program renders into PNGs an
agent can `Read` back. They exist because this project's defects are
overwhelmingly ones a person catches **on sight** — a viewer showing the wrong
thing, geometry through the bed, a first layer floating — and reasoning about
why the code *ought* to work has repeatedly failed to catch them.

- **`tools/capture_ui.py`** — renders every modern-shell page via Win32
  `PrintWindow(PW_RENDERFULLCONTENT)` (works while occluded, never steals
  focus). Asserts no-raise / not-blank / consecutive-pages-differ and exits
  non-zero, so it runs as a check
  **with `--generate --shots .claude/visual/shots`** from
  `autonomy_gate.SUITE_ARGS` — args live in that side table, not in the suite
  name, so the gate's `(repo / suite).exists()` check and dedupe keep working on
  a plain filename.
  - `--generate` presses Generate and pumps the event loop until the worker
    exits, so pages capture with a real result instead of the boot state
    (`run_generate`). Bounded by a wall clock computed once, before the loop.
  - `silence_dialogs` redirects every `tkinter.messagebox` entry point into a
    list first. **This is not optional**: an unattended run has nobody to
    dismiss a modal, so one validation error would block the loop forever.
    It patches the shared module object, which is why it covers every caller.
  - `--shots DIR` chains the renderer below **as a subprocess** — this process
    already owns a live Tk main window and does not stand a VTK one up beside it.
  - Embedded viewers capture fine (they blit offscreen into a Tk widget rather
    than parenting an HWND). **Not covered:** the unified Viewer's canvas, which
    only populates once "Open viewer" is pressed, and the standalone
    `gcode_player` / `stl_viewer` windows, which are separate processes.
  - **It renders on Linux too** since 2026-08-07 — Pillow X11 grab, cropped to
    the window. With no Pillow or no `DISPLAY` it prints "NOTHING WAS RENDERED"
    and returns 2 in a cloud session, 0 elsewhere, so on a fork it stays quiet
    instead of failing a suite that was never going to run.

- **`stl_viewer.render_shots` / `--shots DIR`** — the geometry oracle, and the
  better answer for anything about the part rather than the chrome. Renders
  the `.depo` centrelines (STL fallback) from `_SHOT_VIEWS`, and writes
  `shots.json`. Three decisions worth keeping:
  - **Elevations exaggerate Z, top/hero do not.** A 10 mm scaffold 0.06 mm tall
    is ~170:1; framed honestly it is a hairline and the defect is sub-pixel.
    The factor is drawn **on the image** (`_SHOT_Z_FILL`, `_SHOT_Z_MAX`) — the
    same bargain the Lite viewer's minimum-fibre-width makes. Never exaggerate
    silently.
  - **The plate shows only in elevations.** There it is the datum; at true
    scale from above it is a 76 mm backdrop that swallows a 10 mm part, which
    is why `render_thumbnail` draws no plate either.
  - **Clearance is measured from the fibre UNDERSIDE** (`centre − radius`), not
    the centreline — that is what "resting on the plate" means for a tube.
    Seated reads `0.00`; the centreline figure is kept beside it so the two are
    never confused. This is the machine-checkable half; the images are the
    other half, and something still has to LOOK.

Both are guarded by `test_thumbnail.py` (11 tests: every view written, the
exaggerate/true-scale split, signed seated/floating/sunk clearance, and the
`.depo.npz`-path stem fix).

#### 2.19a Swap without a staged fill — `gui/gui_pagestack.py` + `gui/gui_paint.py`

Two mechanisms, and **which one applies depends on the SIZE of the swap.** Read
both module docstrings before touching either; they were measured with a ~45 fps screen camera in its own process (never a
thread: the GIL starves it through exactly the interval under investigation).

| | mechanism | where it is wired |
|---|---|---|
| a **page** switch | never unmap, and only the top page follows the stack's size: `gui_pagestack.PageStack`, every page placed, switch = `lift()`. `gui_curtain.hold` covers it while it paints | `gui/gui_shell.py:79`, `goto_page:479` |
| **anything a click changes** | `gui_curtain.watch`: an execution trace on `grid` / `pack` / `place` / `raise` raises the same cover for the first layout change after a click, with no call at the site. The cover is a native layered popup holding a copy of the window's own pixels, removed once Tk is idle with no WM_PAINT left | `gui/gui_curtain.py`, `gui_shell._build_widgets` |
| a **card**-sized swap | `gui_paint.paint_fence` holds `WS_EX_COMPOSITED` across the swap and blits once | `gui_widgets.ModuleGrid.open/close`, `gui_pages._show_scaffold_frame`, `gui_calibration._cal_show_tab` |

- **The page defect was never paint batching.** A `ttk.Notebook` shows a page by
  unmapping the old one and mapping the new one, and the remap sets off a
  relayout cascade — `ttk.PanedWindow` re-dividing from its weights (the
  scaffold form column resettling 809→798 px), `VScrollFrame`'s canvas
  re-issuing body width then scrollregion, `PreviewPanel` re-rendering — all
  delivered as `<Configure>` events AFTER the switching call returns, with
  Windows painting ~1200 child HWNDs in between. Photographed: first-change
  spread 398–983 ms. With the stack, the same tail carries **zero** Configure
  events and zero Python callbacks (`whats_left.py`).
- **The fence is a LOSS on a page.** Three arms alternated in one process
  (`fence_ab.py`): spread 315/355/584 ms fence-off against 432/558/703 ms
  fence-on, worst with `invalidate=True`. Do not re-add it to `goto_page`.
- **Eager mapping is deliberate.** Nothing calls `root.geometry()`, so the
  toplevel sizes from what its contents request and a notebook requests the max
  over all pages; mapping lazily would start the window at Home's size and grow
  it on the first heavy visit.
- **Instrument controls are mandatory** and are hard failures in
  `camlib.controls`: a no-op must photograph as an empty timeline, and a
  synthetic bottom-up fill must come back with a clearly NEGATIVE slope. Without
  the second, "the page arrives whole" and "the camera cannot see stages" are the
  same picture. **Never read the fitted slope's magnitude** — the shapes are
  steps and a linear fit turns a step into a fake gradient.
- `PrintWindow(PW_RENDERFULLCONTENT)`, which `capture_ui.grab` uses, **hangs on a
  composited window**. That is why `paint_fence` always clears the style.

---

### 2.19 MEWracle Lite — the browser tier

The same engine, unmodified, behind an HTTP endpoint, with a three.js client.
`lite/` adds **no engine logic**, so there is no porting step: an engine change
flows into the next container build. What it adds is a thin handler, a wire
format, and a web UI.

| File | What it is |
|------|-----------|
| `lite/lite_core.py` | The whole request surface with **no HTTP in it** — `route(method, path, headers, body, query) -> Response`. Validation, the argv build, the subprocess solve, the in-process drape, the wire encode, the token-bucket rate limiter. **`_gate` runs FIRST in `route()`**, so a route added below cannot forget to protect itself — the only way to open a path to the public is to name it in `_gate`. An unauthenticated GET is answered with `login.html` at 200, **not a 401**: that is what keeps `app.js`, `params.js` and `index.html` off the wire entirely, so there is no view-source path to the parameter registry. POSTs get a 401 in the `{"error": ...}` shape `app.js` already parses. Sign-in attempts use `LOGIN_LIMITER`, separate from and tighter than the solve bucket, because the solve bucket is sized NOT to throttle a person iterating on a design. Both adapters share it, so behaviour cannot diverge between hosts. `solve()` runs the engine as a SUBPROCESS for the same reason the desktop does (an OOM dies alone) plus one the desktop does not have: `_FIBER_TRANSFORM` / sag-law / fusion-law are module globals and a warm Lambda container serves many requests. |
| `lite/lite_web/live.js` + `LiveSession` in `lite_core.py` | **SHELVED on Lite since 2026-08-04 -- `lite_params.LIVE_EDIT_ENABLED = False`, `route()` refuses `/live/*` with 404, the rail draws no button.** Everything below is still an accurate description of the code, and the code still works on a SINGLE-PROCESS host; what it cannot do is run on Lambda, which keeps no session affinity, so the in-process `_LIVE` dict is missing on whichever warm environment answers next. The 409-`{reopen}` contract in the last sentence is exactly what turned that into an endless re-open loop. **Live edit -- the desktop's `gcode_player --live-adjust`, as four endpoints.** `/live/open` runs the base solve ONCE and keeps a `mew_incremental.IncrementalDeposition` in process memory; `/live/params` is the O(1) `ParamSchedule` append plus the jet solve, together, because the desktop does both on one frame; `/live/advance` marches to the playhead (`LIVE_LAYER_CAP` = the player's `PHASE4_LAYER_CAP` = one layer per call) and returns **only the fibres that changed**, which the browser accumulates; `/live/close` drops it. **The obvious design -- post the schedule, take the whole print back -- was built and rejected**: it reproduced the geometry exactly and the FEEL not at all, because every knob press paid the engine subprocess, a march over every layer, the drape and the encode (4.5 s on the default scaffold, against 0.01-0.03 s now). Two things that batch call got wrong and the session must not: the march has to reach the playhead BEFORE a breakpoint is appended (else `_freeze_tip_up_to` has nothing to freeze and the straddling layer re-derives its own past), and the **grounded sag law has to be installed around each march** -- `build_argv` forces `--physical-sag` and `_SagState` reads it from a module global, which is why the player installs it at launch. Installed and REMOVED per march here, because unlike the player this process is shared. A session is memory, so it is capped and expiring, and every endpoint answers an unknown one with 409 `{reopen}` -- the client owns the schedule and replays it. |
| `lite/lite_server.py` | Transport adapter: stdlib `http.server`. Localhost, Cloud Run, Fly, a lab box. `--dev` enables `/debug/shot`. |
| `lite/lite_lambda.py` | Transport adapter: AWS Function URL payload v2. **Lambda invokes a handler through the Runtime Interface Client — it does not speak HTTP to the container**, so a long-running web server would time out every invocation. Binary bodies go back base64 with `isBase64Encoded`; over the 6 MB buffered cap it fails loudly naming RESPONSE_STREAM. **Cookies are lifted in both directions**: payload v2 puts them in their own `cookies` array rather than in the header block, so a request the browser definitely sent a cookie with can arrive with no `headers["cookie"]`, and a `Set-Cookie` left in the response headers may never reach the browser — which would present as a login that succeeds and then does nothing. |
| `lite/lite_auth.py` | **The password gate.** Stdlib only, no AWS calls, no database: two environment variables (`MEWLITE_USERS` = salted PBKDF2 lines, `MEWLITE_SECRET` = the ticket signing key). **Off until `MEWLITE_USERS` is set**, and `enabled()` keys on the VARIABLE BEING SET rather than on the table parsing — so a typo'd table locks everyone out instead of letting anyone in. The session ticket is `base64(user\|expiry\|HMAC(secret, user\|expiry\|userkey))` where `userkey` digests the user's own stored line, which is what makes three revocations work with no server-side session store: remove the user, change one password, or rotate the secret (everyone). Signed, never encrypted — it carries only what the holder already knows. |
| `lite/lite_users.py` | **The only way an account is created.** A local operator tool (never COPYed into the image) that read-modify-writes the live function's environment via the AWS CLI. `add` / `passwd` / `remove` / `rotate-secret` / `status` / `list`, plus `--local` to print the variables for `lite_server.py`. It exists mainly to avoid one trap: `update-function-configuration --environment` REPLACES the whole block, so hand-typing it to add a user silently drops `MEWLITE_SOLVE_TIMEOUT` and the symptom appears much later as convert solves timing out at the default. It always reads first, names what it preserves, and verifies the write-back. |
| `lite/lite_web/login.html` | The page an unauthenticated visitor receives **instead of the app**. Deliberately self-contained — one file, no imports, no requests — because pulling in `app.css` would defeat the point. CSS transcribed from `app.css`'s tokens (zero radius, no focus ring, no transitions) and dark mode read from the same `mewlite.dark` key `ui.js:52` writes. Inputs go to 16px under 480 px wide: iOS Safari force-zooms a focused field below that, and this is the one page a phone user must type into. |
| `lite/lite_params.py` | **The one parameter registry** (126 declared, 274 after the per-scaffold clones). `Field` says which CLI flag a knob maps to and what a legal value is; the physics stays in the engine. `build_argv` / `validate` / `calibration_profile` read it, and `emit_js()` GENERATES `lite_web/params.js`. **Three LAYOUT TABLES decide where each field appears — `_SCAFFOLD_LAYOUT`, `_MACHINE_LAYOUT`, `_PHYSICS_LAYOUT` — and every one is TRANSCRIBED from the desktop page it mirrors, never invented.** Each raises on a field of its page that the table forgot, and `_check_layout_covers` raises on the reverse (a table naming one field too many silently truncates the page). Every page that was laid out from imagination instead turned out to have diverged. `Field.widget="radio"` marks the two controls the desktop draws as `ttk.Radiobutton` pairs. |
| `lite/lite_codec.py` | The `.mwl1` wire format: decimate → quantise → delta+zigzag varint → zlib, columnar. **v2 names its streams in the header** (`x y z t r gcode`) instead of locating the g-code by `3 + has_t`, which broke the moment the per-point radius stream landed between them. Lossy by design — a DISPLAY payload; anything measuring fidelity must use undecimated geometry server-side. |
| `lite/lite_web/fibres.js` | **The fibre renderer: instanced ray-traced capsules.** One box per segment that strictly contains its capsule, back-face rasterised, with the fragment shader solving the real ray/capsule intersection and writing the exact surface depth and normal. **Do not go back to `LineSegments2`**: its `worldUnits` path writes the segment AXIS depth (`LineMaterial.js:215-216`) and the deliberately-overlapping capsules z-fight — that is the "random holes" bug. Depth is three's logarithmic encoding, reproduced exactly so the plate and grid agree — **except under the orthographic camera, where `clip.w` is 1 for every fragment and the log form would flatten the scene onto one depth**; there it writes `0.5 * ndc.z + 0.5`, three's own fallback. The ortho ray branch also moves the ray ORIGIN to the eye plane: the proxy box is back-face rasterised, so the fragment sits BEHIND the capsule and a ray starting there discards everything. **No backticks in the GLSL template literals** — one ends the shader mid-comment. |
| `lite/lite_web/viewer.js` | **ORTHOGRAPHIC** camera, matching the desktop's `ParallelProjectionOn()`; framing is by frustum half-height (`_halfH`), not eye distance, because a dolly does nothing to a parallel picture — and `camera.zoom` is reset on every reframe, since OrbitControls dollies ortho THROUGH zoom. Near/far from the scene radius, the build plate (from `gui_viewer_shell.viewport_bg`), and the **minimum screen-space width** floor with its honesty readout. Declines to draw when the page has no layout — otherwise `clientHeight` is 0 and the floor computes a nonsense width. **`mouseButtons` and `touches` must be handed back TOGETHER**: `_installOrbit` fires on any `pointerdown` with `button === 0`, which a finger also carries, so leaving `touches.ONE` at OrbitControls' default ROTATE put two rotators on the camera — measured at **3.24×** a mouse drag of the same length, because the second divides both axes by element HEIGHT where the desktop divides azimuth by width. `touches.TWO` stays `DOLLY_PAN`: the pinch is the phone's only wheel, and `_installOrbit` stands down as soon as a second pointer lands so a zoom does not also spin. |
| `lite/lite_web/pick.js` | **What is under the cursor, in millimetres** — the geometry behind orbiting/turntabling about the fibre you press on (`viewer._pivotAt`). The desktop reads the DEPTH BUFFER for this (`vtk_helpers.install_cad_camera._pivot`); WebGL2 cannot `readPixels` a depth attachment, and the colour-encoded alternative would need a second copy of the capsule ray-trace, so this is an **analytic ray/capsule test** — the same call `viewers/view_cube.py` makes. `capIntersect` is `fibres.js:207-230` transcribed and must stay shared. Three things it must honour or it is silently wrong: the **drawn** radius (`uRadiusScale`, often 50× the true 20 µm), the **reveal** (a segment past the playhead is not drawn and the straddling one is trimmed), and a **`minRadius` floor** — `uRadiusScale` is one scale for the whole mesh, so a thin dynamic-diameter segment still draws under the 1.6 px minimum (0.61 px measured), and a fibre you can see must be one you can press. **Not `THREE.Raycaster`**: `FibreMesh`'s `position` is a unit-box template with the geometry in custom instance attributes. DOM-free and three-free so `node` runs it (`test_lite_server.py`); the browser half is `capture_lite.phase_pivot`. **`nearestSegmentPoint` is the THIRD tier**, and it exists because Lite's first tier is narrower than the desktop's: the desktop reads the depth buffer and hits everything drawn, so its `_focal_plane` fallback (`viewers/vtk_helpers.py:276`) almost never runs, while Lite tests fibres and the plate top only and fell through constantly. Pivoting on the plane through `controls.target` puts the pivot at the wrong DEPTH, and an ortho rotation about it moves geometry by that offset times the angle — in pixels, so zoom multiplies it. Measured on a 372-px canvas, one 60-px drag, the fibre under the finger: **0.5 px on a hit, 109.4 px on a miss, 172.1 px at 8× zoom**; with the tier, 0.9 px. Same rule, better plane — still the point under the finger, still on the ray. Pressing the bare PLATE still swings the part (12.5 px at 1×, 4.2 at 8×) and is left alone: you grabbed the plate, so the plate stays, which is what the desktop does. |
| `lite/lite_web/viewcube.js` | **The Fusion-style ViewCube**, ported from `viewers/view_cube.py`: a labelled cube upper-right that co-rotates with the camera; click a face/edge/corner to snap to one of **26** directions, with the RGB axis triad ON the cube. `rayBox`/`cubeRegion`/`snapViewUp`/`viewportRect` are `viewers/view_cube.py:184-257` transcribed, constants included (`EDGE_FRAC 0.62`, `VIEW_PX 128`, `MARGIN_PX 8`) — pure functions, so the 26-region round trip unit-tests under bare `node`, exactly as the desktop's does with no window. Three things to preserve: the pick is an **analytic ray/slab test** (a `THREE.Raycaster` set up from the MAIN camera silently tests the wrong frustum, since the cube is a second scissored viewport with its own camera); **`LABEL_LIFT` is load-bearing** — the desktop found faces winning the depth test and swallowing labels below it, size-dependently, so eyeballing one size proves nothing; and each face is oriented by an explicit **(right, up, normal) basis**, never a rotate-plus-roll-angle, which shipped "RIGHT" upside down once here. The desktop's scene-side-vs-chrome-side hazard does NOT apply — Lite has one canvas. Show/hide is a rail toggle; `capture_lite.phase_viewcube` is the browser half. **`VIEW_PX` stays absolute and `viewportRect`'s defaults stay put** (`test_lite_server.py` asserts both against the desktop) — the phone cap lives at the ONE call site, `ViewCube.rect`, as `min(VIEW_PX, 0.20 · min(w, h))`. A cap can only shrink, so it cannot resurrect the ballooning a viewport FRACTION caused on the desktop; it never binds on the 1126×766 desktop canvas, and on a 372-px phone canvas it takes the cube from a third of the width to 74 px. Both `render` and `pick` go through `rect`, so the drawn and clickable cubes cannot disagree. |
| `lite/lite_web/stl.js` | Client-side tube sweep + binary STL. A faithful port of `gcode_to_fibers.tube_mesh` including the sequential **double-reflection RMF** (a naive Frenet frame gives a differently twisted tube). Chunked into a `Blob`; a 30×30×10 at 12 faces is ~390 MB. |
| `lite/lite_web/ui.js` | Shell primitives: nav, theme, 500 ms tooltips, click-to-open info dots + the glossary, and the form renderer that turns `params.js` into pages. `fieldValue` and `migrateLegacyIds` are shared with `presets.js` so a saved preset coerces exactly as Generate does. |
| `lite/lite_web/presets.js` | **The named-preset stores — a transcription of `gui/gui_presets.py`, not a browser reinvention.** THREE stores, keyed off `Field.page` the way `_keys_for_kind` keys off the tab: scaffold, machine, and calibration (= the WHOLE Physics page, because `_calibration_persist_keys` claims the sim.* process knobs too). Each gets a "Presets" disclosure as the LAST row of its page and **all eight** desktop buttons in the desktop's order and grouping. The grid IS the list — grouped for scaffolds (a name-only header row per preset, a child row per scaffold), flat for the other two, default marked `★ default`, double-click to load. A preset **fully determines its kind**: a key it omits comes back as the field's factory default. Export writes the same `{"name","data"}` ENVELOPE the desktop's does, slugged by the same rule; Import keeps a clashing name under "(2)". **The two are not interchangeable** — `data` is keyed by Lite's registry ids where the desktop's is keyed by its persist map, so a file crossing tiers imports as "reset everything to factory" rather than as an error. Two deliberate browser substitutions: `askyesno`/`askstring` → `confirm()`/`prompt()`, and `showinfo`/`showerror` → the status line (a native `alert()` blocks `capture_lite`'s CDP driver). **The four-button "Load / Save as… / Delete / Reset to defaults" panel this replaced was reverted once (`dfef920`) for being written from a docstring instead of from `gui/gui_presets.py`.** |
| `lite/lite_web/fit.js` | **The Physics page's "Guided fit"** + the "Reset calibration to defaults" button that sits between Core and Advanced (the desktop has since moved to one "Reset to defaults" per law card, `gui/gui_pages.py:891`). Five sections transcribed from the desktop's `_build_fit_*` builders (`gui/gui_calibration.py:647`-2370, which now number seven), verbatim titles/blurbs/labels. The maths is SERVER-side (`/fit/{viscosity,ea,sag,fusion}`): two of the four need a real `MEWModel`, and the result line is formatted in Python so the `%g` is the desktop's. The CTS section keeps its "CTS sweep" button wired to the fallback the DESKTOP itself takes when the sweep designer is absent (`_goto_experiment_designer`), because Lite does not have that page **yet** — a gap, not a decision. |
| `lite/lite_web/app.js` / `history.js` / `mwl1.js` | Orchestration; Print History in localStorage; the decoder mirroring `lite_codec`. `generate(preload)` takes an optional baked blob so the **default scaffold can fill the viewer on load** without a solve — the preload runs the SAME decode/tube/rail/readout code the real path does, deliberately instead of extracting an `applyDepo()`. A preload files no Print-history entry, takes no thumbnail, does not leave the home page, and never shows the visitor an error. |
| `lite/lite_web/app.css` (`@media (max-width: 900px)`) + `#vtools` in `index.html` + `setImmersive` in `app.js` | **The phone layout.** The viewer canvas was 20 % of a 390×844 iPhone (340×195) because eight bands above and below it each kept their desktop height; it is now 69 %, and 100 % in full screen. Four things not to undo. **Full screen is `body.immersive`, a CSS class, NOT the Fullscreen API** — iPhone Safari has only ever offered fullscreen on a `<video>`, so an implementation resting on `requestFullscreen()` does nothing on the target device; the native call is made ON TOP where a browser has one, on `documentElement` rather than `#canvasWrap` because the scrub bar is a sibling and an element outside the fullscreen subtree is not rendered. **The rail is a DRAWER, not a band** — the 120-px band both ate a third of the viewer and clipped its own controls mid-row. **Everything is behind the breakpoint or inert** (`100dvh`, `env(safe-area-inset-*)`, `touch-action`), which is how "the desktop does not change" is kept, and it is *measured* rather than asserted: `capture_lite.phase_mobile` reads an identical canvas, draw buffer, rail, ViewCube rect and every band at 1600×1000. **`.sb-nav` needs `flex-basis: 0` in landscape** — flex assigns items to lines by base size and only shrinks within a line, so `min-width: 0` alone leaves the nav strip on its own row. |
| `lite/bake_default.py` | **The baked default scaffold.** Solves `LP.defaults()` through `lite_core.solve` at IMAGE BUILD time into `lite_web/default-<sha>.mwl1` + a `default.json` sidecar (both gitignored). The payload crosses the wire either way, so baking removes the solve, not the bytes: 4.5 s and ~12 GB-s of Lambda per visitor become one static GET of the same 462 KB. The hash of `LP.defaults()` rides the FILENAME so the blob can be `immutable` without a redeploy ever serving a scaffold that disagrees with the form. `--check` is the staleness probe; `test_lite_preload.py` drives it in both directions. |
| `lite/Dockerfile` | Two-stage: AWS base image for Lambda, a `server` stage for everything else. The COPY list is **checked, not eyeballed** — a test compares it against the real import closure, because a missing module builds a fine image that dies on its first solve. The `RUN python bake_default.py` at the end of the `engine` stage is also the first thing that exercises the engine, so a broken solve path fails the BUILD rather than the first request. |

**Which page's control governs which pipeline.** The Scaffold designer and the
G-code import page own SEPARATE sag controls and tube-facet counts, because the
desktop does (`self.faces` / `self.sag_mult` / `self.no_sag` on its g-code tab,
`s_faces` / `s_sag_mult` / `s_sag_mode` on the designer). Anything reading a sag
knob goes through `lite_params.sag_mode_for` / `sag_multiplier_for` /
`sag_reset_for`, which take the target — read the bare field and an import
silently obeys the other page. `render_travel` is the one that stays shared: the
desktop applies its Export-panel travel toggle to an import too.

Guarded by `test_lite_params.py` (registry ↔ `params.js` parity, every default
read back off the engine's own argparse, **every calibration label + unit
checked character-for-character against `mew_calibration.CALIBRATION_FIELDS`**
with one named allowance for the ASCII-vs-glyph pairs tkinter forces on the
desktop, and — since 2026-08-01 — the Scaffold / G-code / Machine / Physics-
process pages' labels and every dropdown CAPTION against transcription tables
carrying the desktop `file:line` per entry), `test_lite_codec.py` (round-trip error against the quantisation step,
both simplifiers against their stated bounds, the named-stream change), and
`test_lite_server.py` — the HTTP surface, a **41-case feature matrix**, a
**differential matrix** that requires each knob to change the payload (a knob
that reaches the engine and does nothing passes an existence check), the
**guided-fit round trips** (each measurement synthesised from a known model,
then the model's own constant recovered), and node-driven parity for the two
shipped JS files against their Python counterparts.

### 2.20 The virtual MEWron — not in this release   *(tombstone; kept so section numbers stay stable)*

### 2.21 Five-axis — curved collectors

Doubly-curved collectors — domes, bowls, cones, tori — with **two rotary axes
carrying the COLLECTOR** and the printhead translating only, so the nozzle stays
perpendicular to the local surface at a constant working distance. Built
2026-09-08 as eleven slices; `five-axis-v1`. The precedent is Terranova 2024,
whose platform is this layout and whose g-code is `X Y Z U V F`.

**Every five-axis number is a PREDICTION.** There are no validation trials, by
decision. The paper states an extrapolation with its limits.

**STEP IMPORT IS PAUSED IN THE UI (2026-09-17), and only in the UI.** Joseph
shelved arbitrary collectors so the paper could be written: the Curved-collector
page offers dome, bowl, cone and torus and says nothing about STEP — no file
row, no Browse button, no up-axis chooser, no import status line.

| | where |
|---|---|
| the variable, and the ONE place it is read | `gui_surface.STEP_IMPORT_ENV` / `step_import_enabled()` (`gui/gui_surface.py`, just under the module docstring). `MEWRACLE_STEP_IMPORT=1` (or `true`/`yes`/`on`) before launch brings the picker back exactly as it was |
| its three callers | `_build_surface_import_fields` (the widgets are not BUILT, not merely hidden), `_surface_persist_entries` (`sf.file` / `sf.up_axis` / `sf.heading` leave the preset map, so an old `presets/defaults.json` carrying a path is never applied), `_surface_file_path` |
| why `_surface_file_path` is the safe seam | it is the ONE predicate every consumer asks — the frame builder, `_surface_check`, the readout, `_build_args_surface`, the preview, `gui_viewers`. Answering `None` there turns the import off everywhere at once, so no path from a preset, a scratch state or code can put a design on a solid whose picker is off screen |
| what is UNTOUCHED | `five_axis/mew_cad.py`, `five_axis/mew_surface.py`, `five_axis/mew_project.py`, both CLIs' `--surface-file` / `--surface-up`, `test_mew_cad.py`, and `test_five_axis_matrix.py`'s three import cases, which still run in both switch states. `cadquery-ocp` stays optional and lazily imported (`mew_cad._occ`) |
| the two-way proof | Built the real `App` in both states and asserted each line in both. Unset: 62 widgets under `surface_nb`, none of them STEP. Set: 70, and `Collector file:` / `Browse…` / `Clear` / `File's up axis:` / `Heading (deg):` are back |

`test_ui_shell.py`'s persist-map parity passes in BOTH states because the
classic and the modern shell build the one `_persist_map`, so a key dropped by
the switch is dropped from both sides and the two stay key-identical.

**THE FOOTPRINT ROWS ARE PAUSED THE SAME WAY.** `gui_surface.footprint_fields_enabled()`
(`gui/gui_surface.py:135`, env `MEWRACLE_SURFACE_FOOTPRINT=1`) gates the "Footprint
shape / sides / Custom outline / Footprint angle" rows: unset,
`_build_surface_footprint_fields` (`:1050`) builds none of them, and the two
readers every consumer asks, `_surface_footprint_shape` (`:1325`) and
`_surface_custom_polygon` (`:1335`), answer `"rectangle"` and `None`, so a
preset's `sf.fp_*` keys cannot steer the output from off screen. The engine's
`--surface-shape` / `--surface-footprint-polygon` are untouched.

**THE FEED SLOWDOWN IS A CHECKBOX, OFF (2026-09-24, 285bce8).** "Slow the feed to keep
the nozzle perpendicular" (`sf_slow_feed`, persisted `sf.slow_feed`, default off).
Off, `_surface_feed_floor` (`gui/gui_surface.py:1912`) returns `None`, no
`--surface-min-speed` is sent (the engine's 0, which never slows) and the rotary
limits spend perpendicularity instead; `_surface_check` skips the floor
validation. On, the preview re-times each interval by the speed the planner held
(`generate_surface_gcode(surface_speeds=)`, `engine/scaffold_gcode.py:5499`), so it draws
the lag the file lays.

**EVERY TRAVEL IS ROUTED OVER THE COLLECTOR (2026-09-18).** A travel is one
`G0` and the controller interpolates every word linearly, rotaries included,
so a straight travel swings the collector under a nozzle moving in a straight
line and nothing used to ask whether the two met: every lead-in dived 0.19 to
3.81 mm into the part and the block's layer hops 0.72 mm. Deposition is
untouched by all of it — 2586 fibre moves, bit-identical in the collector
frame, on all 15 matrix cases.

| | where |
|---|---|
| the route | `scaffold_gcode._travel_waypoints` — samples the travel at `TRAVEL_STEP_MM` / `TRAVEL_STEP_DEG`, poses each sample through `machine_model.rotary_transforms`, asks `frame.outermost_along` how far it must rise along the nozzle axis, and lifts the stretches that would enter to the UPPER HULL of that requirement. **The rotary path is not touched**: each waypoint carries the words the straight move had at that fraction, and each piece keeps the move's own `F`, so the joints pass the same poses in the same order and turn slower through a lifted piece, never faster |
| the lead-in's lateral half | `scaffold_gcode._leadin_approach` — reads the design's footprint off the layers' own chart coordinates and brings the approach in from OUTSIDE it, widening until `_footprint_fibre_mm` says no fibre is laid across the footprint on the way in. MEW's jet never stops, so a travel over the scaffold is fibre under the scaffold |
| **what a travel asks the frame, and it is not what a fibre asks** | `mew_frame.DepositionFrame.outermost_along`. A fibre asks about the PRINTED band (`covers`, `u0..u1`); a travel has to clear the physical collector, and a collector does not end where the pattern does. `RevolutionFrame` overrides it to march the whole meridian **only where the meridian CLOSES** — convex and curved, so the dome's ball and the torus's tube, which is what the part would be if it ran further. A STRAIGHT meridian (cone) has no end, and extending it invented part at u = 102 mm and answered with a 100 mm plunge; a CONCAVE one (bowl) has its solid on the outside of the sphere, and extending it fills the room rather than thickening the cup (one sample asked for a 34.4 mm rise). Both keep the span. `self.k1 > 0` is the test |
| the one library shape still not at 0 mm | the BOWL, 20 of 615 lead-in samples 2.44 mm inside — all of them 6 mm of meridian arc PAST the band, where the frame says there is no cup and `test_five_axis_matrix`'s oracle assumes 5 mm of wall (`BOWL_WALL_MM`). Nobody has told this code where a collector physically ends: ops task `20260918-043100` |
| how a check reads a routed travel | a RUN of consecutive `G0`s is ONE travel. `collector_checks.layer_of_moves` opens a layer on the first `G0` of a run; `check_geometry` drops the intermediate waypoints from the `k*d` and tilt sample set, because a waypoint in the air stands on no layer. Comparisons between two files are about the `G1`s, with travel runs counted beside them and never differenced (`collector_zoo.travel_runs`) — the R10 cap takes 9 lead-in waypoints where the library dome takes 7, because one is a bounded solid with a rim and a base and the other a surface of revolution |
| the RED control | `collector_zoo.straighten` — drops every `G0` followed by another `G0` and puts the straight travel back. Both suites use it. On the plate it reads 3.812 mm inside, which is slice 0's own measurement of that case on `c4be0c6` |
| what routing costs | about +8 s on the library dome's generate, +2.4 s on the imported block. The matrix runs 248 s against a 300 s budget |

| Concern | 3/4-axis | 5-axis |
|---|---|---|
| surface | `PlanarFrame` / `CylinderFrame` | **`mew_frame.RevolutionFrame`** — a profile curve spun about machine X. One class is dome, bowl, cone and torus |
| profile | — | `LineProfile` (straight meridian → developable) / `ArcProfile` (circular meridian) |
| pattern → path | closed-form helix (`mew_tube`) | **`five_axis/mew_project.py`** — geodesic projection of a flat pattern by Fermi coordinates, through three surface queries only (point, normal, geodesic step) |
| speed (imported collector) | — | `mew_project.parallel(frame=)` shards `march_many` rows and the chart query over spawned workers that rebuild the frame from `CadFrame.worker_recipe`; `project_patterns` batches every distinct layer pattern. Only the generator's surface branch opens the block. Guard: `test_mew_cad` sharded == in-process. |
| flat pattern | `_layer_passes` + `_flatten_passes`, clipped to the footprint at every layer's own angle | **the SAME two**, via `mew_project.scaffold_pattern` + `scaffold_gcode.SurfacePattern` (2026-09-11). Footprint shape / sides / custom outline / footprint angle all reach it. The passes come back AXIS-ALIGNED and the layer's angle is handed to `project_pattern` instead, because `_rib_jobs` groups rows by exact float equality of `q` |
| layer entry | four candidates (`flip` x `vflip` x reversal), nearest the layer below's exit | **the same four**, `SurfacePattern.ENTRIES`, chosen in `surface_layers` |
| generator | `generate_mandrel_gcode` | **`scaffold_gcode.generate_surface_gcode`**, fed by `surface_frame` + `surface_layers` |
| parser | `parse_gcode(frame=)` | **`gcode_to_fibers.FiveAxisReader`**, `parse_gcode(five_axis=reader)` |
| lag / placement | `lag_radii_from_jet` | **`mew_mandrel.CurvedPlacement`** — one semi-axis per principal direction |
| metric | `metric_v(h)`, one scalar | **`frame.metric(u, v, h)` → `(a, b, c)`**, the chart's first fundamental form FACTORED |
| designer | `gui/gui_mandrel.py` | **`gui/gui_surface.py`** — a THIRD value of `s_collector`, not a peer page |
| player / viewer | `_mandrel_body` | `gcode_player` rotary PAIR + `stl_viewer.revolution_actor` |

**Five contracts, each of which cost something to learn** — and one defect worth keeping named, because the shape of it recurs.

0. **A LAYER ROTATION IS NOT A RIGID TURN OF ONE PATTERN.** Until 2026-09-11 `surface_layers` built one `length x width` rectangle and turned it per layer, so a 90-degree layer's passes reached `length / 2` where the layer below reached `width / 2` — **1.00 mm of straight overrun at each end** at the shipped 6 x 4 defaults, visible in the g-code as a pass carrying on for a few mm before its turnaround, and it also dropped the passes the flat generator lays outside the turned rectangle. The flat generator never had this: it re-derives hatch lines at each angle against a FIXED footprint. `footprint_angle` is the one that IS rigid (`engine/scaffold_gcode.py:1083` says what re-clipping cost it), so the two rotations are different operations and neither may be implemented as the other. Measured both ways in `test_a_rotated_layer_ends_on_the_shared_footprint` and its RED control.

1. **`metric` returns `(a, b, c)` FACTORED, never `(E, F, G)`.** A step
   `(du, dv)` lands at `(a*du, b*(dv - c*du))`. Factored so no call site takes a
   square root: `sqrt(m*m)` is not `m` in float64 and the cylinder's digests
   would move. `c` is the chart's SHEAR, `rho'*phi`, and it is zero only where
   `rho' = 0` — a cylinder, and nothing else. Dropping it costs **145% of a
   step on a dome** and **nine gating suites stay green**, because not one of
   them builds a curved frame.
2. **Euler's theorem is asked at the point being laid, in the SURFACE's
   frame.** `chord_drop_at(span, eu, ev, u, h)` takes the direction as the
   surface sees it and `_curvatures_here(u, h)` the curvature there.
   `normal_curvature`/`chord_drop` survive with **no production caller**: they
   take no `u` and read `principal_curvatures(h)`, which on a `RevolutionFrame`
   is the COLLECTOR-midpoint value. Reaching for them again costs +33%/−20% on
   a 15° cone and −91%/+96% on a torus.
3. **A rotary word is never wrapped.** Terranova's `V` accumulates past
   ±27 000° monotonically; `U` is a bounded tilt. Normalising either into a
   principal range, or comparing two readings modulo 360, unwinds a print.
4. **The pole is refused, by a PATH test.** `mew_project` caps each march step
   at `rho - pole_guard`; `rho` is 1-Lipschitz in world position and the march
   is unit-speed, so no INTERIOR point of a step can enter the disc. A sample
   test instead admits crossings at random and returns a folded `(u, v)`.
5. **Flat and cylinder stay byte-identical**, and on a bare cylinder that is
   exact rather than approximate: `CylinderFrame.metric` returns `a = 1.0` and
   `c = 0.0` EXACTLY, so the affected columns telescope as `x + (y - x)`, which
   float64 returns as `y`.

**Two traps that are latent rather than live.**

- **A dome's `rho` is NEGATIVE across its whole span.** `surface_frame` builds
  dome and bowl as one circle traversed opposite ways, `ArcProfile.t0` defaults
  to 0 and `convex=True` sets `sigma = -1`, so a dome is `rho = -R*sin(s/R)`
  (−0.01 … −4.93 at R = 5) while a bowl is positive. Self-consistent — a
  surface of revolution is symmetric, so a negative `rho` mirrors onto the same
  surface — but every derived scalar that reads as a LENGTH is negative for the
  default shape. `period_v_at(u_mid)` returns −29.28 for a dome.
- **`RevolutionFrame` does not bound itself to its own `u0`/`u1`.** A pattern
  centred at `u = 13` on a collector spanning 2–8 projects happily and returns a
  path spanning 10–16. `gui_surface` refuses it; the engine does not.

**WHERE THE COLLECTOR SPIN IS DECIDED** — the `C` axis whip, and the three
passes that route it. All in `engine/scaffold_gcode.py`.

| what | where | note |
|---|---|---|
| the feasible arc pair, closed form | `_perp_band_interval` | carries the maths; `None` means EVERY `C` is feasible (the pole, or a flat stretch). **The pick between the two arcs costs BOTH joints** — they are the two IK branches, half a turn apart with opposite tilt, and pricing them on `C` alone bought 3.7° of spin for a 93.5° tilt flip that dragged a layer hop over the apex |
| greedy least-motion pick | `_perp_band_c` | a thin clamp over the interval — the two must not drift apart |
| normal into the collector frame | `_collector_normal` | uses `machine_model.collector_mount`, the same one `rotary_solutions` applies |
| pass 1+2+3 driver | `_five_axis_layer` | greedy walk → smooth → lean; **tolerance 0 reaches none of it and stays byte-identical** |
| pass 2, smooth-and-project | `_route_band_c` | minimises CURVATURE: cuts the peak 343 → 89.7 °/s, cannot cut the total |
| pass 3, least total travel | `_route_min_travel_c` | DP over ABSOLUTE `C` under a rate cap; both IK arcs, every winding and the pole are ONE feasible set here |
| the cap, and when pass 3 is taken | `_peak_rate`, `ROUTE_LEAN_MIN_GAIN`, `ROUTE_LEAN_MAX_PEAK_RISE`, `ROUTE_LEAN_MAX_POINTS` | pass 2's OWN peak — no axis limit exists to read — **except when the solved floor sits ≥10% under it** |
| the SOLVED cap, when pass 2 fails | `_route_minimax_cap`, `_route_cap_feasible`, `_dilate`, `ROUTE_MINIMAX_TRIGGER`/`ROUTE_MINIMAX_SLACK`/`ROUTE_MINIMAX_MIN_DROP` | bisects the least holdable peak over the full two-arc set; fires where the greedy walk snapped branch and smoothing then achieved nothing (1357.8 → 618.4 °/s) |
| the shared feasible set | `_route_grid` | one `(point, C)` boolean both whole-path solvers read, so they cannot disagree about what is feasible |
| what the rotaries cost, and what a wider band would save | `surface_spin_report`, `_spin_stats`, `_hold_threshold_deg`, `_straight_runs` | printed by every five-axis Generate; each candidate is a pass's own **angular miss distance** `d/R`, and the saving beside it is re-solved, never extrapolated |
| the band's error curve | `_band_sinusoid`, `_band_err_deg`, `_capped_envelope` | `p(C) = K + R cos(C - psi)` |

Three things that are easy to get wrong here. **Least-total and least-peak
oppose each other** — the travel optimum hugs the feasible boundary and peaks at
742 °/s, and smoothing it changes nothing because it is already a fixed point of
smooth-and-project. **Timing is collector ARC** (`ProjectedPath.arc_length` /
speed), never Cartesian XYZ, which overstates the whip by up to 7×. And
**unwrap, never wrap**: the spin word accumulates across revolutions.

A fourth, added 2026-09-14 after Joseph's dome R10 / 60° preset: **`C` is the
azimuth of the contact point about the collector's spin axis**, so a straight
pass missing that axis by `d` sweeps it ~180° at a peak rate of `step/d` —
a coordinate singularity, not a routing defect (measured within 2% of `0.5/d`
across fifteen passes). Orientation is a SQUARE 2-in-2 system, `C` and `A`
against a direction on a sphere, so X/Y/Z cannot help and `A` cannot stand in.
What IS avoidable is that a pass can be served by ONE `C` for its whole length
at a constant error of `d/R`, so the tolerance field is a **step, not a dial**
— 2.0° → 3.0° took that preset's worst pass from 93° of `C` to 5.7°, which is
why the page default moved.

Gating suite is `test_five_axis_gcode.py` (65 checks), whose `_c_walk` drives
all three passes from one function so a comparison is the routing and nothing
else. It carries the RED controls: the greedy walk as pass 2's control, and a
cone as pass 3's (a cone has no slack, so declining is the pass condition).

**Three things that do NOT exist yet**, and each is the visible edge of the build:

- **No sagged deposition MESH for a curved collector.** `build_deposition_mesh`
  takes no `frame`, so it re-parses with no reader and the word guard refuses.
  What the player draws on a dome is the deposited TOOLPATH, **without the sag
  solve**. This is what stands between the five-axis build and a digital twin.
- ~~**No UI for a rotary PAIR.**~~ **RESOLVED 2026-09-09** — see "THE FIVE-AXIS
  COLLECTOR TABLE ON THE MACHINE PAGE" in §2.7. The page writes a two-joint
  `rotaries` list with an `stl` and a picked `pivot` per joint.
- **Live-adjust degrades to locked** on a curved collector: `_init_live_relay`
  (`viewers/gcode_player.py:3071`) parses with `parse_gcode(frame=)`, which raises on a
  `RevolutionFrame`, and it now says so on stderr (the GUI's Log page).

**Terranova's files are a comparator for the CONVENTION, not the physics.**
Their per-segment `F` divides the 5-vector with degrees in quadrature and the
implied segment time is a per-file CONSTANT (60 / 100 / 499 ms, 83–99% of moves
within 1%). Their path is uniformly TIME-sampled and `F` is that sampling's
arithmetic shadow — **their surface speed is not held either**, so our feed
checkbox has no external comparator. Their validation collector measures as an
extrusion of a single convex arc (R ≈ 18.03 mm, max residual 0.033 mm), and
**nobody has found the concave region** the published convex-vs-concave delta
rests on; that is an open task, together with their **unchecked
`U`/`V` sign convention**.

### 2.22 G-code document engine

**Machine G-code card:** `gui_pages._build_machine_grid` places G-code between
Collector and Presets; `gui_buildplate._build_machine_gcode_fields` owns the
flavor, comment stripping, start/end blocks, automatic commands and coordinate
settings. They ride machine presets via `gui_presets._persist_map` and
`gui_buildplate._machine_persist_keys`. `machine_setup.setup_steps` includes
the card in Instructions. The default start block and placeholder expansion
live in `mew_gcode`; current print values come from
`gui_buildplate._machine_gcode_context`. `Document.render` applies machine
rules in both export and parse forms and carries a custom flavor to the worker.
`gui_gcode_changes._gce_library` migrates older automatic recipes and
`_gce_migrate_custom_line` moves a numbered custom line to the nearest
structural boundary. The Design tree's Header form holds notes, and the G-code
window's Hide/Show header button folds the start block in the text view. The one-file
strip override starts from the machine setting. Comment metadata stays out
of band; comments here are for people, not for reconstructing the print.

**`engine/mew_gcode.py`** — no Tk. The G-code page's ONE text is not stored: it is
RENDERED from the base (generator output or an imported file), its **line map**
(where each line sits: header / group / layer / pass / exit) and the user's
**chips**, each recorded in structural terms ("after layer 7", "layer 3, pass 4:
F → 900"), so a redesign re-places them and lists what can't carry over.
Two forms of one render: **export** (output options + flavor; what Save/Copy/Send
write) and **parse** (travels still G0, no output options, user comments
stripped; what the parser / converter / player read). The module docstring is
the model.

- **Machine spelling + incremental origin**: a `Flavor`
  carries a move table (`move_word`, `rapid_word`, `arc_cw/ccw`, `abs_word`, `inc_word`,
  `home`, `units_word`, `dwell_cmd`, `rotary_feed`); built-ins are G-code's identity except
  `FLAVORS["aerotech"]`. The document is G-code inside. `machine_render` (section 2c) is the
  export form's last step: re-spells each line (`to_machine_line`), chords an arc the
  machine has no word for, and with `OutputOptions.incremental` prepends the origin block
  (home, move to `origin`, switch) and writes deltas. Each re-spelled `RLine` keeps `src`
  (its G-code) and `pos` (absolute before it). `record_edit` reads typing back:
  `Document._inbound` / `_inbound_new` (`from_machine_line`); an incremental edit becomes a
  `shift` on the LineEdit/Insert, applied to that line and every later move in both forms
  (`_apply_shifts`). `from_file` translates an import in (`to_internal_text`, exact round
  trip). The off-page Save path runs the same pass (`machine_text` in
  `apply_output_options`). GUI: `gui_buildplate._build_incremental_rows` /
  `_origin_output_fields` (persist `origin.incremental`, `origin.machine_x/y/z/r1/r2`,
  `origin.feed_time_base`). Guard: `test_gcode_dialect.py`.

- The moved user-facing rewrite, byte-identical to the pre-engine code:
  `split_z_word` `:63`, `is_five_axis_gcode` `:80`, `_rewrite_line_list` `:115`
  (per-line form the document needs), `rewrite_gcode_lines` `:165`,
  `wait_temp_line` `:222`, `insert_line_after` `:245`, `OutputOptions` `:263`
  (`boundaries=AUTO` reads scaffold boundaries off the line map),
  `apply_output_options` `:316`. `gui_scaffold` keeps the old names as aliases.
- Structure: `Address` `:578`, `LineMap` `:597` (`guess` `:730` for an
  opened file with no recalled structure: its `;LAYER:n` comments
  (`layer_starts_from_comments` `:784`), else its motion
  (`layer_starts_from_motion` `:877`: new heights, and fiber crossings in a
  file that names its height once); `basis` says which, and the Outline
  shows it. `guess_from_z` `:700` is the old Z-only guess; `sha1` pairs a
  map with its text). `OutputOptions.layer_markers` = Setup's "Mark each
  layer in the file", written by `Document._layer_marker_lines`.
- Chips: `Insert` `:675` (+ `Anchor` `:652`), `LineEdit` `:690` (word patch /
  replace / delete on one line key), `Rule` `:740` (`WHERE_CHOICES`,
  `PLACEHOLDERS`), `Transform` `:814` (+ `Scope` `:796`), `Piece` `:824`.
- `Document` `:1093`: `render` `:1263` (transforms + line edits in chip order →
  options on generated lines only `_apply_options` `:1533` → inserts/rules at
  anchors → file-wide transforms → legacy custom line → flavor framing),
  `record_edit` `:1858` (typed text → chips; the render then IS the typed text),
  `_anchor_between` `:2024` (the coarsest boundary at the caret), `rebase`
  `:1123`, `outline`, `describe_line`, `explain_line`, `export_to_parse_line`,
  `snapshot`/`restore` (document-level undo), `to_dict`/`save`/`load`.
- Flavors: `Flavor` `:342` holds ONLY sourced syntax (`sources` per field;
  pressure / HV empty in every built-in); `make_command` (+ `INSERT_FORMS`,
  `form_available`), `convert_line` (never moves the nozzle), `explain`,
  `problems` + `apply_fix`, `detect_flavor`, `custom_flavor` /
  `register_flavor`.
- **What a flavor MEANS** (2026-10-28). Only the commands that change what
  the preview draws are mapped (`MEANING_COMMANDS`: G0-G4, G20/21, G28,
  G90-G92); every other word goes to the machine as written, unasked.
  `Flavor.aliases` ("WORD=G1" / "WORD=G0") are READ-ONLY: applied in the
  parse form only (`read_line`, `alias_map`), so the file keeps the word.
  `Flavor.known` = the lab's own words that are not moves (never asked
  about). `unread_moves(text, flavor)` finds lines whose unknown first word
  carries axis words; `export_gaps(doc)` lists the meaning commands the
  flavor can't write (`Gap`: a re-spelled dwell with no unit, G0/G2/G3 with
  no move word, G90/G91 dropped, G28 with no home line, G20/G92 in a
  re-spelling flavor). `from_machine_line` reads a Z-only linear move as G0
  when the flavor has no rapid word: an Aerotech file reopened keeps its
  plies (`parse_gcode` splits plies on G0 Z). Editor rows: `FLAVOR_FIELDS`,
  `FLAVOR_GROUPS`, `FLAVOR_TIPS`, `flavor_form_values` / `flavor_from_form`.
  Guard: `test_gcode_dialect.py`.
- **Saved flavors** — `engine/flavor_presets.py` (no Tk): one JSON per flavor in
  `gui_common.FLAVOR_PRESETS_DIR` (`presets/flavor/`). `name` is a stable id,
  the label is unique and renamable (`rename`; the old label stays in the file
  as `former_labels`, which `by_label` resolves). `new` starts from plain
  G-code with no process commands. Built-ins are editable: the edit is a diff
  from `mew_gcode.SHIPPED_FLAVORS` in `presets/flavor/edited_defaults/`,
  `reset` deletes it, `is_edited` tells; an edited built-in rides a document
  to the worker as `flavor_definition` (`FLAVOR_STORE_LOADED` keeps the GUI's
  registry authoritative). `snapshot` is the copy a machine preset carries
  (`origin.gcode_flavor_custom`); `adopt` reads it back (this computer's own
  flavor wins by id, then label; the old single Custom slot's rows become a
  saved "Custom"). `export` / `import_file` share one as a file (an import
  always gets a fresh id and a unique label). Guard: `test_flavor_presets.py`.
- Pieces: `normalise_piece_text` (inches → mm, relative → absolute),
  `_piece_transform`; a join travel between pieces lays fiber.
- Recipes: `Recipe`, `STARTER_RECIPES`, `load_library` / `save_library`,
  `apply_recipe`, `new_document` (adds "use on every new print").
- **Layer shift** (microscale layer shifting, Liashenko 2020 / Devlin 2024 —
  the Scale/move menu's "Layer shift"): `Document._layer_shift`. Each selected
  layer n moves `layer_shift_factor(profile, n)` steps (Ramp / Constant /
  Alternate / Wave / Cycle, counted from 0) of X / Y (rigid), Along (each
  pass slides along its OWN print direction: the phase), Amplitude (µm at the
  peak, or % of its own: across the print) and Radius (about the layer's
  centre). Pass ends are pinned, so turnarounds are untouched under Along /
  Amplitude; the pass ends where the generator's **`link` mark** says its
  turnaround begins (`_link_starts`). Arcs keep their circle (`_arc_centre`).
  Fields µm in the chip, mm in the g-code. 3-axis only (`has_rotary_words`);
  an opened file has no passes, so only X / Y / Radius act —
  `layer_shift_summary` says so in the form. Tables: `TRANSFORM_FIELDS`,
  `TRANSFORM_CHOICES` (the Profile combo), `TRANSFORM_TIPS`.

**The generator side** (`engine/scaffold_gcode.py`): `_mark` / `record_line_map`
(module-level recorder, `None` = off, byte-identical output) marks group /
layer / pass / link / exit in `generate_multi_scaffold_gcode`, `_emit_one_scaffold`,
both mandrel emitters, `_append_mandrel_exit` and `generate_surface_gcode`.
`_write_gcode_output` serves all three `-o` writes: `--line-map PATH` writes the
map beside the g-code (never in it); `--gcode-doc DOC.json` writes the
document's PARSE form to `-o`, so the STL, `.depo` and player see the edits.
`mew_export` passes both through untouched (`allow_abbrev=False`).

### 2.23 G-code page (Design)

The modern shell's **G-code** page: an editor of the exact file Save writes,
following the Scaffold designer live, with the scaffold preview beside it. It
replaced G-code import and the Results "G-code text" page. **Modern UI only**;
the classic UI keeps its G-code tab. Two mixins, in MRO order
`_GcodeChangesMixin, _GcodeEditorMixin` (`mewracle_gui.ModernApp`): the
changes half overrides the editor half's hooks, each chaining to `super()`.

**`gui/gui_gcode_editor.py` — the page and the plain editor.**
- `GcodeEditor` (:104) — the text widget: gutter, read-only lines, Find (Ctrl+F
  returns "break"), Go to line (Ctrl+L), undo steps `[p, old, new, meta]` with
  `attach_meta` and an `on_step(meta, forward)` callback run BEFORE the text
  changes (the changes half restores the document snapshot there).
- `_GcodeEditorMixin` (:908): `_build_gcode_editor_page` (:920), the Output menu
  (:1097), side column (:1129), preview (:1226).
- Live regeneration: `_gce_design_argv` (:1334) runs Generate's own worker
  prefix into the page's scratch (`session/editor/`); `_gce_take_design` (:1498).
- `_gce_open_file` / `_gce_load_file` (:1591/:1608) — Open file…, with
  Convert-to-STL folded in.
- Flavor (:1711-1760): the page box follows the Machine page's
  `origin.gcode_flavor` until a flavor is picked on the page.
- `run` (:2144) — Ctrl+G Generate from the page; `_gce_run_text_file` (:2175)
  runs the page's PARSE text as a file; `save_gcode_as` (:2221) writes the
  page's text exactly; `_find_in_sim` (:2258) maps the caret through
  `_gce_parse_line`.

**`gui/gui_gcode_cad.py` + `gui/gui_ribbon_icons.py` — the ribbon.** `ToolButton`
takes an icon by NAME (`gui_ribbon_icons.ICONS`; any other string is drawn as
a text glyph). Icons are drawn in code on one 24-unit grid, in the palette's
`fg` with `st_info` as the one accent, and redrawn on retheme; a new tool
adds a `_name(p)` function there. `python gui/gui_ribbon_icons.py out.png` draws
the whole set in both themes.
`RibbonFit` keeps the ribbon inside the window: as built where it fits, then
every tool at its label's width, then the rightmost TOOLS fold, one by one,
into one More dropdown at the right end (each group's under its caption, the
tools' live state). A group stays on the ribbon as far as it fits, and goes
with its hairline when its last tool does. Widths come from the packing arithmetic, never a trial
layout, and equal Tk's reqwidth at every level; call `fit()` after anything
that swaps groups. A pointer at a tool (the tutorial) asks `anchor(w)`.
The action bar's display row (`OneRow`, toggles + Reset view) folds the same
way: `_gce_disp_fit` hands `OneRow.fit` what the bar leaves it, and the
items that do not fit go, from the right, behind its own "More…" link.
Call `_gce_disp_fit()` after packing anything new into the strip.
Guarded by `test_ribbon_fit.py` (8 cases).

The page has no title row. The ribbon's **Designer** group
(`_build_gce_source_tools`) holds the way to the Scaffold designer and, in one
slot, Update from designer or Back to this design (`_gce_src_tool`). The tree's
title bar says what is off the usual, a file or a frozen copy and unsaved work
(`_gce_head_refresh`, from `gce_link_var` and `gce_work_var`); on this page's
own saved design it is blank. The print's time, length and volume
(`gce_stats_var`) are in that bar's tooltip. The Quality panel opens with
**Build quality** (`gen_render`), which the shell's bottom row no longer shows;
Generate reads the var on every page.

**`gui/gui_gcode_changes.py` — the document and its edits** (`mew_gcode.Document`).
The designer's g-code is the baseline; every change is an EDIT on top, and a
typed edit and one made with + New edit are the same object in the same form.
Design-tree feature rows use `_gce_design_can_delete` / `_gce_design_delete`
for both the ribbon and Delete key; saved edits opened from My library carry a
rename back to the library on Done. `_gce_hl_clock_of` reads canonical g-code
and maps its line numbers to the selected flavor's displayed text. The G-code
overlay has resize grips, and a Design preview refresh borrows the shared
progress bar when Generate is not using it. Modern scaffold placement starts
from the plate corner; changing the g-code zero rebases the output while the
Design preview maps the result back into the fixed physical plate frame.
The Quality panel's **Preview at export quality** runs the selected export
solver in a separate worker and displays its cached centerlines and diameters;
an edit cancels that view and returns to the fast live preview. The live view
honours the tube-facet count, sag Off, and periodic sag reset. Simulation reads
the generated cache and uses the generated tube-facet count in tube mode.
- `_gce_options` (:92) — the designer's output options (AUTO boundaries when
  the line map matches); a file gets all-off.
- `_gce_snap` (:150) — before any change: an open form is kept first, typing
  not yet recorded is recorded.
- `_gce_design_argv` (:191) adds `--line-map`; `_gce_take_design` (:214)
  rebases the document; `_gce_load_file` (:291) builds `Document.from_file`.
- `_gce_record_edits` (:326) — typing becomes typed edits. **Recorded against
  the UNSYNCED model**: an output option flipped a moment ago has not reached
  the text yet, and reading its re-spelled lines as typing made 12 bogus chips
  per flip.
- `_gce_pv_source` / `_gce_pv_adopt` (:386 / :417) — the preview WORKER
  records the typing on a copy; the Tk thread takes the result when the text
  and the document key are unchanged. With no picture to draw the worker
  still runs (`_gce_pv_kick(draw=False)` in the editor).
- Header: `_gce_detach` (:540), `_gce_relink` (:556, a two-step inline confirm
  listing what carries over).
- The left column, two quadrants (`_build_gce_quadrants` :613): My library
  (`_gce_render_library` :1135 — this file's unsaved edits as kept rows, the
  saved ones, examples; `_gce_save_id` :1283, `_gce_use_recipe` :1311,
  `_gce_edit_recipe` :1349, a saved row's Edit: the one edit form) over the Anatomy (`_build_gce_anatomy` :653, a
  Treeview: `_gce_anatomy_rows` :792, each edit under `_gce_home` :887,
  diff-synced by `_gce_tree_sync` :950; drag an edit onto a part,
  `_gce_move_edit` :1062).
- The one edit form, in the library's place (`_gce_host_open` :1410):
  `_gce_new_edit` (:1537), `_gce_open_edit` (:1562), `_gce_edit_form` (:1590),
  live apply `_gce_edit_apply` (:2006) through `_gce_edit_build` (:2030), and
  `_gce_edit_finish` (:2230 — Done is ONE undo step via
  `GcodeEditor.begin_group`/`end_group`, Cancel restores the snapshot). Plain
  forms (a stitched piece, `_gce_open_piece` :2309) use `_gce_form_open`
  (:1495). Every form's widgets are `gui_revert.skip`ped: mapping them never
  rescans the app for revert arrows.
- Problems (:2450, hidden), What changed (:2525).
- Off the page: `_build_args_scaffold` (:2656) appends `--gcode-doc` to the
  designer's Generate when edits are on (never to the page's own regen,
  `_gce_regen_building`); `save_gcode_as` (:2676) saves the export text when
  the disk file is the document's parse form, else says so.
- Export card badge `_gce_build_badge` (:2715, wired in `gui_pages`).
- Persistence: `gce.document` (:2772), a JSON StringVar in the SCAFFOLD preset;
  a linked document stores edits only and is restored on the next regen
  (`_gce_restore_pending` :2828), a detached copy stores its base too.

**`gui/gui_gcode_collector.py` — the collector and 4-axis design on the page**
(`_GcodeCollectorMixin`, before `_GcodePanelsMixin` in the MRO; module
docstring is the contract). The ribbon leads with a Collector group, then
swaps whole frames: the 3-axis groups (`gui_gcode_design`) or the 4-axis
ones (New scaffold / Family / Custom cell / Parameters / Advanced); 5 axis
keeps the 3-axis set, greyed. `s_collector` is the designer's, shared; the
panel's 3/4/5 bar calls `_collector_mode_chosen` (axis defaults, no confirm).

**On a mandrel the page holds SEVERAL TUBES, one per page scaffold**
(2026-10-30, the 4-axis parity salvage). The page owns them: each page
scaffold's `m_*` tube vars (`TUBE_KEYS`) and its tube state live in
`gce_scaf_vars[i]`, swapped in by `gui_gcode_twodesigns._gce_tube_scope(i)`
(new scaffolds start from `_TUBE_STATE_INIT`); the designer keeps ONE tube,
and its Update links copy scaffold 1. `_gce_tube_place_beside(k, gap=5)`
places a new scaffold (a copy of the last) beside the last one; placement is
the user's — Position on rod / Offset / Start out from rod, per scaffold.
Every tube goes to `scaffold_gcode` as one `--tube-scaffolds` JSON list
(`generate_mandrel_scaffolds_gcode`, §2.22). A tube that hangs off the rod is
drawn hanging off, never refused (Joseph: no banner for it). **Copies are
retired**: a preset carrying `copies` opens as that many scaffolds.
- `_TubeView` runs every lookup and call of the designer's `gui_mandrel`
  builders inside `_gce_tube_scope(i)` — the Custom-cell panel
  (`_gce_m_cell_open(i)`) and Advanced (`_build_mandrel_rig_fields`, the rig
  only) are built over it. Pattern and Tube panels are gone: every Tube field
  is a row of the Parameters table.
- The Parameters table (`gui/gui_gcode_params.py`, module docstring): one column
  per scaffold, `TUBE_PARAM_ROWS` (kinds family / cell_edit / pos / rib /
  choice / pick / out / …; `_gcp_tube_info(c)` works out each column's
  applies / editable / resolved / error in scope), so pore size, Requested /
  Resolved, end turn and the pore-size choices are cells; per-layer view is
  `TUBE_LAYER_ROWS` over that scaffold's `m_layer_overrides`: pattern, its
  size, rib angle, wave amplitude, speed, step out, and Position on mandrel
  / Offset, which `gui_mandrel._mandrel_layer_shift` turns into a block's
  `shift` along the rod by the tube's own placement rule
  (`TubeBlock.shift`, `tube_shift_range`). The table scrolls both ways.
  Every menu in it is a `gui_widgets.FitCombobox`, as wide as its longest
  choice in its own font: never a hand-picked character `width`.
- The tree: one group per scaffold, its `d:m<i>:pattern|cell` rows (eye,
  double-click to the form), and, as on the plate, a layer that differs
  gets ONE `d:m<i>:L<k>` "Layer parameters" row inside its own branch --
  never a block row under the scaffold (they listed every layer twice,
  Joseph 2026-10-31). A group opens the Parameters column, a layer row
  the layer table.
- A bad value: the table marks the cell and the preview's status line says
  "Not redrawn: …" — no banner.
- `scaffold_gcode`: when a tube's exit would land on another tube, the
  emitter lays the last tube from its other end (`_last_far`).
Guarded by `test_gcode_collector.py` +
`test_gcode_params.py` + `test_mandrel_blocks.py`
(`test_an_exit_onto_another_tube_turns_the_last_one_round`).

**No scaffold-count limit** (2026-10-30): `SCAFFOLD_SPIN_MAX` (gui_common)
is the spinbox's top only. The designer grows slots on demand
(`gui_scaffold._scaf_grow(n)` → `_new_scaffold_vars` + `_scaf_slot_built`;
the modern shell adds a frame to every per-scaffold card and a pill to the
rail, `PillRail.add_pill`, which wraps at the page's width via `bound=`);
the page grows with `_gce_ensure_scaffolds(n)`.

**Subsection — Design view and STL export.** `gui/gui_gcode_subsection.py`
owns the ribbon, edit form, tree eyes, pore conversion, preview and the
`gce.subsections` preset/Print History key. Sections use the print-order
layer table (`gui_gcode_view` / `view_tools.layer_table`), including the
stabilization scaffold. Preview placement uses the Move gimbal.
**The Design view clips fibre CENTRELINES, never a mesh**: `mew_subsection.
section_pieces` / `clip_polyline` (numpy; a sphere by its own equation, a
bounding-box prefilter first; about 0.15 s for a million points) cut the
preview's paths with their print clock and radii, and `_gce_pv_clip` (the hook
`_gce_pv_show` calls after the tree eyes' cut) hands the pieces to the panel as
ordinary fibres. So an enabled section shows ONLY the fibres inside it, and
colours, tubes, the line clock and **click-to-select** work on them as on the
whole print. While the tool is open the whole print stays up, dimmed grey in
place (one opaque, clickable actor), with the cut drawn coloured over it
(`_subsection_overlay`); while dragging the outline moves and the fibres inside
it are re-cut each tick as plain coloured lines (`_subsection_drag_cut`,
throttled to twice its own cost); tubes, times and colours return on release.
**A box section is also cut flat through the tube body** (`_subsection_gpu_cut`):
the export slices the tube MESH at the box, the preview slices centrelines, so a
fibre stayed round past the box and ended square. For one enabled box the
centrelines are kept 3 radii longer than the box and the actor is clipped at the
box's own planes on the GPU (`_subsection_apply_planes`; VTK draws at most six),
the faces where the planes slice the tubes drawn as a second actor (`_subsection_caps`: each
fibre the box touches is tubed and cut with the export's own `ClosedClipper`, and only the
faces lying on a plane kept; flat in the fibre colour, so a layer-coloured view caps in the base colour). Other
shapes, a mandrel, several enabled sections and a layer selection keep the
centreline cut with capped tubes (`meta['cap_tubes']`). Playback shows the full print (`_sub_full`). A hidden
gimbal is not drawn (`MoveGimbal._apply` sets `SetDraw`): its layer-1 renderer
clears depth even when empty, which broke every click-to-select after the Move
or Subsection tool closed. **Shapes:** box, circle, sphere, and a **custom
outline** drawn in the Footprint tool's own editor (`gui_scaffold.
_build_shape_editor`: Add/Remove/Clear, Zoom −/+/Fit, Grid, the vertex table),
shown only for that shape. The outline is `outline_mm` (millimetres about the
centre); a section saved with the old unit-square `outline` still cuts
(`mew_subsection.outline_points`). The form greys what a shape drives (a
sphere's Y and Z follow its diameter, a drawn outline sets X and Y) and shows
the first/last layer rows only for a layer selection. `engine/mew_subsection.py`
builds convex clipping volumes and closes each clipped
fibre. `gcode_to_fibers --subsection ... --from-depo ...` clips the existing
full-print solve in a worker, after the export mesh's decimation and travel
rules. Layer selection uses the original g-code clock where available and
cuts and caps a continuous fibre at the boundaries of selected layer times.
Mandrel pore counts use the winding cell's axial and circumferential sizes;
a narrow layer-selected region keeps the side of the mandrel at its centre.
Five-axis collector sections are disabled. Export writes one named STL per
enabled section (each validated first); without one the normal full-print
export is unchanged. The preview's cut is by centreline, so a fibre whose
centre lies within one fibre radius outside the boundary is not drawn there
though the export's cut tube includes it.

**`gui/gui_gcode_move.py` + `gui/move_gimbal.py` — the Move tool** (`_GcodeMoveMixin`,
before `_GcodeChangesMixin` in the MRO; module docstrings are the contract).
Ribbon **Move** (key M, icon `translate`) opens the ordinary offset edit form
(`_gce_tool_move` :61 → `_gce_tool_change("offset")`); the old "Scale / move"
menu is now **Transform**. The box's dx/dy/dz, **rz (degrees about Z) and
its pivot cx/cy** ARE the edit (`mew_gcode` `Document._move_rotate`: p' =
pivot + R(p − pivot) + d, arcs' I/J turned, a missing X or Y filled from the
modal position; rz = 0 keeps the old add-to-the-named-words path byte for
byte). The pivot is filled with `mew_gcode.motion_xy_centre` of the moving
lines (`_gce_mv_centre`) after an apply, never while one is pending: a
retarget calls `_gce_edit_vis` BEFORE it applies the new scope. The gimbal
sits at pivot + (dx, dy); its blue ring turns (`PreviewPanel._gimbal_turn`),
hidden on a 4-axis tube. The gimbal on the picture only writes the box: `_gce_mv_drag` (:226) sets the vars silently while
dragging and flushes once on release. The selection's lit fibers ride a
GHOST offset (`PreviewPanel.set_highlight_offset`, `gui_preview` :1751) —
the picture never rebuilds while a form is open — computed in the
`_gce_hl_refresh` override from the params the clock's text was built with
(`_gce_mv_seen`: shift, turn, pivot) and the box's now; the ghost is the
actor turned about Z through a pivot, then shifted (`_hl_pose`). `_gce_mv_lines` (:181) reads the lines from the
SCOPE, since a zero offset changes none. Picking: with Move open (or Ctrl/
Shift held) a click selects the fiber's layer, a double-click its scaffold,
a modifier toggles it into the tree selection (`_gce_on_pick` :255); a
changed selection retargets the open move (`_gce_sel_show` :304).
`move_gimbal.MoveGimbal` (:198) is a layer-1 overlay renderer with its own
camera synced on StartEvent: three arrows, three plane squares (XY/YZ/ZX,
coloured by their normal), a free ball; `hit_handle` (:114) and
`handle_point` (:152) are pure (VTK-free) and carry the hit order. The
overlay clears depth over its viewport, so every depth read goes through
`PreviewPanel._depth_render` (:2349, gimbal hidden). The panel routes a
press to the cube or the gimbal first (`_overlay_press` :2253,
`_gimbal_drag` :2279). **ViewCube:** `want_view_cube` set by
`gui_gcode_editor._build_gce_preview` (:1663); `_make_view_cube` (:2233)
builds it with `interactor=None` (`view_cube.ViewCube` then skips its own
press observer) and the panel's Tk press forwards to `pick`.
**One edit, several homes:** a move over a `set` scope is filed under each
part it covers (`gui_gcode_changes._gce_homes` :1441); the extra rows are
aliases `e:<cid>@<parent>` (`_gce_row_cid` :1465 maps any row back), summed
"N places". **Make a copy** (a check in the box, `_gce_kind_rows` adds
it): Done keeps the originals and appends the moved lines as a frozen
stitched piece (`Piece(source="snippet")`, named "scaffold K · copy"), its
layers re-stacked from the collector up (`_gce_mv_restack`: the first at its
scaffold's first-layer Z, each next one its own step above) and listed in
the tree through `Piece.layer_starts` (`mew_gcode._piece_map`; None:
guessed from Z changes). The piece is added after a record + flush and the
editor text re-synced, so the base finish (remove=True) neither reads it
as typing nor splits the undo step. Like any piece it prints after the
design's End. On a 4-axis tube X runs along the rod; Y/Z still write the
file's Y/Z words, which a tube's wrapped picture cannot show as a ghost.

**Machine page:** `gui_buildplate._build_gcode_flavor_field` — the flavor box
(built-ins + saved, by label) and **Edit flavors…**; persisted as
`origin.gcode_flavor` (the label) / `origin.gcode_flavor_custom` (a saved
flavor's carried copy, machine kind). `_machine_flavor_set` carries the copy,
`_machine_flavor_copy_loaded` adopts a loaded one, `_flavors_changed` is the
one fan-out after any save / rename / delete / reset (Machine box, carried
copy, saved printer presets naming a renamed flavor via
`_machine_presets_follow_rename`, then `_gce_flavors_changed` on the page).
Both flavor vars normalise an old or former label on write
(`CURRENT_FLAVOR_LABEL`, `_gce_flavor_current`).

**Flavors on the page** (`gui_gcode_changes`): Syntax > Edit flavors… opens
the same `gui_flavor_editor.FlavorEditor` (Use = this text). `_gce_check_unread`
(on open and on a flavor change) shows a notice for words that look like
moves: Read as printing moves / Read as travels (an alias, saved into the
text's flavor, a built-in as the lab's edit of it) / Not a move. `save_gcode_as` first runs
`_gce_export_gaps`; a gap pauses Save with a notice (Save anyway / Edit
flavors). Nothing else ever pauses Save.

**Work safety (`gui/gui_gcode_work.py`, `_GcodeWorkMixin`; its docstring is the contract).** Ctrl+S saves the page's design + `gce.document` as a Design preset (first time asks a name; later Overwrite / Save as new / Cancel; built-ins never overwritten); Ctrl+O opens a file; an unsaved prompt (Save / Don't save / Cancel) guards quit, Open file and preset load; autosave (atomic, 4 s, `MEWRACLE_AUTOSAVE_DIR`) with a restore/discard offer at the next launch; page-wide Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z through `_gce_undo` (a design change waiting on typed edits is undone first); job stats (time, fibre length, volume) in the title row, stale-guarded; Ctrl+C / Ctrl+V / Ctrl+K and the tree's right-click menu (no ribbon buttons); a Send tool uploads the exact bytes Save writes. `gui_machine_link` refuses Upload / Upload & start when the page's results are out of date (`_ml_stale_reason`) and offers `_ml_send_page_version`. Dialogs go through `_gw_dialog`, which never blocks when the app is not interactive (`ModernApp.GW_INTERACTIVE`, set only by `main()`). Not undoable: Export option flips, eye visibility, flavor (not probed).

**Not done:** the preview is a second preview instance, not the designer's (spec §10.1).

Guarded by `test_gcode_editor.py` (the page, each case RED-proven) +
`test_flavor_editor.py` (the editor from both doors, the carried copy, the
unread-move notice, the Save gap) +
`test_ui_shell.py` (persist parity; `gce.` is a sanctioned modern-only prefix)
+ `test_gcode_gutter.py` + `test_mew_gcode.py` (the engine) +
`test_gcode_collector.py` (the collector and 4-axis tools).

---

## 3. "I want to change X" → where to go

| Task | Go to |
|------|-------|
| Change the plate's **Fiber spacing** or **Grade the spacing** (Design > Parameters) | `gui/gui_gcode_params.py` module docstring + `_gcp_grade_*` / `_gcp_draw_sketch` / `sync_layer_grade`. Fiber spacing IS `stepover` (centre to centre); no row shows the pore. The grade is a VIEW: across the layer = `stepover_end` + `grade_angle` (generator `_graded_offsets`, unchanged); Up through the layers = design var `stepover_top`, which writes each layer's `stepover` override (`attach_layer_grade` is hooked where `gui_gcode_twodesigns` makes a page scaffold). Ticking sets `stepover_end` equal to the spacing, which the generator lays byte-identical to an even layer. The tutorial's grade steps call `_gt_grade_layers`. |
| Add or change a five-axis / curved-collector anything — a shape, the projection, the metric, the emitter, the designer mode, the player | §2.21 first: it names the five contracts and the three things that do not exist yet. |
| Change how a fiber sags / droop law (geometric) | `engine/gcode_to_fibers.py` `sag_depth` `:1825` + `_SagState._solve_layer` `:5693` |
| Change the periodic sag RESET (straight every N layers) | `engine/gcode_to_fibers.py` `_SagState.maybe_reset` `:5633` + `_DEFAULT_SAG_RESET_LAYERS` `:1725`; GUI `gui/gui_scaffold.py:834` (Quality card); mirror in `preview_sag.drape_sag` `:610` |
| Tune the PHYSICAL (tensioned-thread) sag law | `physics/mew_physics.py` `bridge_sag_depth` `:1019` + `Calibration` `sag_*` block; `--physical-sag` |
| Deposit TRAVEL (G0) moves / carry jet state across paths | ENGINE: `gcode_to_fibers.parse_gcode(with_travels=)` §2.3 + `mew_deposition.laid_fibers(travels=, simulate_travel=)` §2.11. EXPORT/GUI: the ONE user flag `--render-travel` — GUI checkbox `s_render_travel` (`gui/gui_scaffold.py`, Export panel, persist `s.render_travel`) drives **THREE independent paths, all of which must be wired or the box looks broken**: (1) `gui_args._build_args_scaffold` → `mew_export --render-travel` (physics on: `make_transform(simulate_travel=True)` + `set_fiber_transform(t, with_travels=True)`) **or** `scaffold_gcode --render-travel` → `gcode_to_fibers --render-travel` (physics off: `merge_travels`); (2) `gui_args._build_args_gcode` — the SAME box governs an imported g-code (travel deposition is a jet property, not one tab's geometry); (3) **`gui/gui_preview.py`** — the live preview parses + relays IN-PROCESS and never runs the converter, so it needs `options["render_travel"]` (`_pv_snapshot_options`) + a `_pv_all_dirty` trace, threaded separately. Missing (3) was a real shipped bug (the box ticked, the preview never changed); guarded by `test_scaffold_preview_honours_render_travel`. Either way the travel is MERGED into the fibre it leads into, never appended — the grouping asserts `sum(layers) == len(paths)`. ⚠ A plain single scaffold has **NOTHING to render** (its layer changes are in-place Z-hops, 0.000 mm XY) — the converter now says so out loud. Travel shows on a stabilization base / multi-scaffold / imported g-code. **`.depo` CACHE KEY**: `render_travel` is the 9th field of `_depo_cache_meta` — it MUST be, because `merge_travels` preserves the fibre count, so a travel-rendered cache is indistinguishable from a plain one by any count guard (the player's `if len(paths) != n` seed guard cannot catch it). The GUI passes `--render-travel` to the player purely as this key (`gui_args._render_travel_arg`); `_load_live_locked` passes `render_travel=False` UNCONDITIONALLY because the live march parses without travels, so a travel cache is geometry it cannot reproduce and must be rejected rather than seeded from (cost: a travel-rendered print loses the live locked-seed, so live may drift from locked — the honest trade). Guarded by `test_depo_cache_keys_on_render_travel`. NOT wired: the player's LIVE-adjust re-lay (`mew_live`) does not lay travels; locked mode rides the `.depo`, so it shows travel fine. The player's "Show travels" button is an unrelated pre-existing commanded-G0 OVERLAY (`viewers/gcode_player.py:1376`). Tests `test_mew_travel.py` (21) |
| Change the DEPOSITION ORDER across scaffolds (by scaffold number vs by layer height) | Export control `s_deposition_order` (`gui/gui_scaffold.py`, radios "By scaffold number" | "By layer height across scaffolds", persist `s.deposition_order`, trace → `_invalidate_order_result` in `gui/gui_viewers.py` which clears last_stl/depo/**gcode**). GENERATOR: one builder `gui_args._expanded_specs_and_sources` produces the expanded specs AND their `_array_sources` in lockstep; "by layer" is a STABLE sort of the whole list by each copy's `start_z` (so `_expanded_scaffold_specs` / `_array_sources` stay parallel through the sort), emitting the g-code in height order. VIEWER: for the by-scaffold order the emitted g-code's deposition order departs from height order, so the converter must sag in g-code order, not by global Z — `apply_sag(layer_by_deposition=True)` swaps `_layer_ids` for `_layer_ids_deposition` (`engine/gcode_to_fibers.py`), which layers in emission order so a scaffold printed later rests ON TOP of the last (the sandwich fix). The flag is **gated** in `gui_args._layer_by_deposition_arg`: passed ONLY for a genuine multi-body stack whose start_z is non-monotonic in emission order → every normal print (single scaffold, plain ascending stack, stabilization base, and the by-layer order itself) omits it and stays BYTE-IDENTICAL by-height. Wiring: GUI `_build_args_scaffold` → `scaffold_gcode --layer-by-deposition` → forwarded into `gtf_argv`. Deliberately **NOT** sent to the player (unlike `--render-travel`): the player's live march re-lays in g-code order via `_SagState` already, so it reproduces either mode with no flag; a toggle forces a regenerate. Guarded by `test_deposition_order.py` (byte-identity on ascending Z + block-order layering + the orange-on-red-crown apply_sag payoff + the GUI ordering/gating seam). |
| Tune fiber FUSION / squishing at crossovers | `engine/gcode_to_fibers.py` `_fusion_law` `:1903` / `_sink_fused_contacts` `:1934`; `set_fusion_law` `:1887` |
| Change Fiber target (settings for a target diameter / lag, its map, Apply) | `physics/mew_target.py` (the inversion; never put physics here, bisect `MEWModel.solve`) + `gui/gui_fiber_target.py` (the panel, used by the Physics card and the G-code ribbon) — §2.11b |
| Tune the jet-LAG law / re-fit to new lag data | `physics/mew_physics.py` `lag_angle` `:946` + `Calibration` lag_* block (`:190`-211); see LAG-REANCHOR-HRYNEVICH |
| Fix missed/extra fiber contacts / rest heights | `engine/gcode_to_fibers.py` `_SegIndex` `:2483`, `rest_heights` `:3251` |
| Change tube shape / facets / STL output | `engine/gcode_to_fibers.py` `tube_mesh` `:1309`, `write_binary_stl` `:7643` |
| `.depo` cache format / a stale cache reused | `engine/gcode_to_fibers.py` `write_depo_cache` `:7310`/`load_depo_cache` `:7382` + meta `_depo_cache_meta` `:7297` |
| Change the G-code page's picture tools -- the one bottom play bar (Play / layer slider / time / speed), Colour by scaffold / layer / speed, Fit, Measure | `viewers/view_tools.py` (VTK + numpy: the print-time window shader, ramps, `layer_table`, `measure_numbers`, `ViewTools`) + `gui/gui_gcode_view.py` (`_GcodeViewMixin`: the bar's dressing, Colour panel, measure box, keys I Z C K and the arrows) + `gui/gui_gcode_jet.py` (`_GcodeJetMixin`: builds the bar, `PlayButton`, the jet clock, `SPEEDS`) + the hooks in `gui/gui_preview.py` (`_view()`, `pick(zread=)`, `_depth_render`, `_fire_pick`) and `gui/gui_gcode_editor.py` (`_gce_pv_aux`, `_gce_hl_geometry` window clip) -- §2.23. The window is two shader uniforms on per-point print-time arrays, never a rebuild; the eyes still cut by rebuild. ONE bar: the slider is the layers laid, 0 (the empty collector) to n, showing layers 1..k; Play carries on with layer k + 1, and moving the slider drops the jet. The Simulation sidebar's "G-code listing" switch (`gcode_player._toggle_listing`, `viewer_sidebar_spec.LISTING_*`, drawn by `gui_embed._set_listing` and `gui_viewer_shell._refresh_listing`) replaces the Line / Move rows with a followed window of the file. `test_gcode_view.py` exists; its RED proofs are incomplete. |
| Change Settings (the gear): General, G-code output, Quality, "Make these my defaults", the preview's own quality knobs | `gui/gui_settings.py` (module docstring: two scopes, App and This design). The This-design sections are the Scaffold designer's Export / Quality cards built through `gui_gcode_panels._gce_panel_export` / `_gce_panel_quality`; the live preview's `pv_curve_quality` / `pv_fill_per_tau` / `pv_auto_corner_grid` / `pv_tube_sides` are made in `gui_preview._pv_ensure_option_vars` and read in `_pv_snapshot_options` and `gui_gcode_editor._gce_pv_options`; defaults are `design_defaults` in `gui_prefs.json`, applied by `_apply_design_defaults` after the boot default presets (`gui_presets._apply_default_preset`). Run `test_ui_shell.py` + `test_ribbon_fit.py` after |
| Change the G-code page's collector or 4-axis tools — the Collector panel / tree row, Tube / Custom cell / Advanced, the Presets kind bar | `gui/gui_gcode_collector.py` (module docstring) + `gui/gui_gcode_panels.py` — §2.23. Run `test_gcode_collector.py` + `test_gcode_editor.py` + `test_ui_shell.py` (+ `test_gui_mandrel.py` for a `gui_mandrel` builder) |
| Change the G-code page's Move tool — the gimbal, its hit order, the ghost offset, picking layers/scaffolds on the picture, the page's ViewCube, a move filed under several parts | `gui/gui_gcode_move.py` + `gui/move_gimbal.py` (docstrings) + `gui/gui_preview.py` (`show_gimbal`, `_overlay_press`, `_depth_render`, `set_highlight_offset`) — §2.23. Run `test_move_gimbal.py` + `test_gcode_editor.py` + `test_gui_preview.py` + `test_view_cube.py` + `test_ui_shell.py` |
| Change the G-code page — the edit form, My library, the Anatomy, Detach/relink, what Save or Generate writes from it, `gce.document` | `gui/gui_gcode_changes.py` (the document half) or `gui/gui_gcode_editor.py` (the text, the Output menu, Open file, Find in simulation) — §2.23; the text it renders is `mew_gcode` §2.22. Run `test_gcode_editor.py` + `test_ui_shell.py` + `test_mew_gcode.py` |
| Fix how g-code is interpreted | `engine/gcode_to_fibers.py` `parse_gcode` `:552` |
| The G-code page's text: a chip kind, anchoring, a rule place, a transform, a flavor profile, Problems | `engine/mew_gcode.py` (§2.22); the structure it anchors to is `scaffold_gcode._mark` |
| Saved flavors, the flavor editor, what a flavor maps (aliases, unread moves, export gaps) | `engine/flavor_presets.py` (store), `gui/gui_flavor_editor.py` (window), `mew_gcode` "What a flavor MEANS" (§2.22), the page's notices in `gui_gcode_changes` (§2.23). Run `test_gcode_dialect.py` + `test_flavor_presets.py` + `test_flavor_editor.py` + `test_ui_shell.py` |
| The user-facing output options (travels as G1, G0X, one layer Z, M109, the custom line) | `mew_gcode.apply_output_options` / `_rewrite_line_list`; GUI gates in `gui_scaffold._gcode_output_options` |
| Change scaffold geometry / add a generation param | `engine/scaffold_gcode.py` (`generate_*`, `_emit_one_scaffold` `:1052`) + wire GUI |
| New footprint shape | `engine/scaffold_gcode.py` `footprint_outline` `:231`, `_SHAPES` `:213` |
| Linking loops / turnarounds | `engine/scaffold_gcode.py` `_link_loop_circle` `:849` / `_link_loop_points` `:930`, `_lead_in_chord` `:1008` / `_turn_circle` `:763` / `_turn_points` `:819` |
| Inter-scaffold travel / array routing | `engine/array_travel.py` `plan_array_travel` `:116` |
| End-of-print exit move | `engine/gcode_exit.py` `plate_exit_point` `:109`; its switch: the G-code window's Parameters box, `gui/gui_gcode_params.py` `_gcp_build_exit` `:863` (the App's `s_exit_move`/`s_exit_margin`, also Settings > Export) |
| **Add a GUI input field** | var+widget in the owning tab builder (`gui_core` g-code tab / `gui_scaffold` core/export/stab) → CLI in `gui_args._build_args_*` (+ `_scaffold_spec` if per-scaffold/multi) → validate in `gui_args._validate_*` → persist in `gui_presets._persist_map` `:1160` (+ `_new_scaffold_vars`/`_prefill_scaffold`/`s2.`-`s5.` loop for per-scaffold) |
| Add a GUI tab | top-level: `gui_core._build_widgets` `:228` (+ teach `_active_mode`); global sub-tab: `gui_scaffold._build_scaffold_tab` `:87`. **Modern UI:** a new field needs NOTHING extra (pages mount the same builders); a new PAGE needs a registry entry in `gui_shell._build_widgets` |
| Modern UI: top row/pages/action bar/theme/fonts | `gui/gui_shell.py` / `gui/gui_modern_theme.py` / `gui/gui_fonts.py` / `gui/gui_home.py` (§2.12); run `test_ui_shell.py` after |
| Embedded scaffold preview (designer page, right panel) | `gui/gui_preview.py` (§2.12): pipeline `build_preview_paths`, panel `PreviewPanel`, wiring `_PreviewMixin`; run `test_gui_preview.py` after |
| Embedded viewers: native fullscreen / transport / pan-lag | `viewers/viewer_embed.py` (`EmbedHost` native_fs `_enter/_exit_native_fs`, `_coalesce_moves`) + `gui/gui_embed.py` (`ViewerPanel` `toggle_fullscreen`, `EmbedViewport._mouse`); the fullscreen timeline is the CHILD's `ViewerChrome.transport` (`gui/gui_viewer_shell.py`, mounted by `gcode_player._mount_fs_transport`); §2.12 U9; run `test_viewer_embed.py` + `scratch/verify_gui_embed.py` after |
| Viewer orbits about screen centre, not the cursor | the cad-camera pivot reads `renderer.GetZ` — a layer>0 overlay must `PreserveDepthBufferOn` (`gui_viewer_shell.ViewerChrome`); the preview anchors via `PreviewPanel._cursor_pivot`; run `test_vtk_helpers.py` + `test_gui_preview.py` |
| Preview "Sagging (approximate)" drape engine | `engine/preview_sag.py` `drape_sag` (§2.12) via the `gui_preview.preview_sag` seam; run `test_preview_sag.py` after |
| Add/fix a preset field | `gui_presets._persist_map` `:1160`; `_keys_for_kind` routes by `_machine_persist_keys` (`gui_buildplate:2557`) / `_calibration_persist_keys` (`gui_calibration`) → scaffold / machine / calibration. **Lite mirrors this in `lite/lite_web/presets.js` `kindOfField`, keyed off `Field.page`** — a new Lite page needs a line there or its fields silently ride the scaffold preset |
| Lite: a preset button, store or dialog | `lite/lite_web/presets.js` (§2.19) — read `gui/gui_presets.py` end to end first |
| Lite: a Guided-fit section or a new `/fit/` estimator | `lite/lite_web/fit.js` `SECTIONS` + `lite/lite_core.run_fit` / `FIT_FIELDS`; the estimator itself belongs in `physics/mew_calibration.py`, never in the browser; guard it with a round trip in `test_lite_server.test_guided_fit_endpoints_round_trip` |
| Add/change a calibration parameter | `mew_calibration.CALIBRATION_FIELDS` (registry; the GUI auto-builds the field) — defaults stay in `mew_physics.Calibration`/`Material`/`Nozzle`; nothing else to wire |
| Print History: what a run records / diff / CSV+PDF export | ENGINE `analysis/mew_history.py` (`HistoryStore`, `diff_runs`, `export_run_csv`, `build_run_sheet_pdf`) §2.17; CAPTURE `gui_history._history_on_run_start/finish` (called from `gui_run.run()`/`_on_finished` via `hasattr`); TAB `gui_history._build_history_tab`; run `test_mew_history.py` + `test_gui_history.py` after |
| Print History: the run thumbnail (camera / no-plate) | `stl_viewer.render_thumbnail` + `--thumbnail` CLI (§2.17, shared file, physics-free); run `test_thumbnail.py` after |
| LOOK at a generated result without opening a window | `stl_viewer.render_shots` + `--shots DIR` CLI (§2.19): offscreen renders from the `.depo` (STL fallback) at `front`/`right`/`top`/`hero`, plus a `shots.json` carrying the **underside clearance** to the plate. Elevations exaggerate Z (labelled on the image); top/hero are true scale and hide the plate. Run `test_thumbnail.py` after |
| LOOK at the running GUI with a real result on screen | `tools/capture_ui.py --generate [--shots DIR]` (§2.19): presses Generate, pumps the loop until the worker exits, then captures every page. Modals are redirected to a list first — an unattended modal hangs forever. The Stop gate runs exactly this form via `autonomy_gate.SUITE_ARGS`; the no-flag form still works and still exits 0 |
| Add a guided-fit estimator | `mew_calibration` (pure fit fn, reuse `mew_physics` laws) + a section in `gui_calibration._build_calibration_fit` + a `test_mew_calibration` round-trip |
| Change the subprocess run/log/cancel | `gui/gui_run.py` `run` `:27`/`_poll_output` `:196` |
| Add a viewer control / launch flag | `gui/gui_viewers.py` `_build_viewer_tabs` `:15` → `_launch_stl_viewer` `:439` / `play_gcode` `:646` |
| Theming (new widget not themed) | usually automatic; `gui_theme._style_clam` `:121` (ttk-by-class) |
| Player: animation/reveal/camera | `viewers/gcode_player.py` `_on_tick` `:6960`/`_apply_time` `:7054`/`_reveal_time` `:3551`/`_frame_camera` `:1978` |
| Player: locked vs live deposition drift | diff `_build_deposition` `:5785` (locked) vs `_advance_deposition` `:3262`/`mew_live`/`mew_deposition` |
| Printer model / motion limits / bed slide | `machine/machine_model.py` `place_scene` `:1598`/`contact_alignment` `:1243` + `gui_buildplate._machine_args` `:2679` |
| Machine page setup checklist (a step, an item, its status or Go target) | `machine_setup.setup_steps` `:101` + `gui_pages._render_setup_tile` `:1904` / `_setup_focus` in `_build_machine_grid` `:1511` |
| Bed datum wrong | `machine_model.find_bed_top_face` `:1493` or `machine/bed_calibrator.py` (manual P-pick) |
| Origin/datum re-base (scene shifted) | `gui_buildplate._origin_offset` `:2494` ↔ player `place_scene` origin (mirror images) |
| Add/fit a physics law | `physics/mew_physics.py` `Calibration` `:112` + a `MEWModel` method; test the scaling law |
| Physics jet visualization | `viewers/gcode_player.py` `_recompute_physics` `:3534`/`_jet_radii` `:3657` + `mew_physics` |
| Physics-deposited export / dynamic diameter | `gui_sim._export_prefix` `:645`/`_sim_play_args` `:664` → `mew_export` → `_FIBER_TRANSFORM` seam |
| Calibrate the jet model to a rig (tab/profile/fit) | `gui_calibration._build_calibration_tab` → `mew_calibration` (registry + `build_model` + fit fns); worker arg `--calibration <profile.json>` |
| Accel-limited kinematics | `mew_kinematics.plan_trajectory` `:250`; wired via player `_retime_kinematic` `:2808` |
| Calibrate a physics value (print → measure → fit → apply), or design a CTS / lag-circle / temperature-ladder calibration print | `analysis/calibration_procedures.py` (the procedure registry — card order + all operator prose) + `gui/gui_calibration.py` (`_build_calibration_page`, `_cal_build_card`, the per-key fits) + `gui/gui_sweeps.py` (`_SweepsMixin` print engine, `SweepPreviewPanel`) + `analysis/mew_sweeps.py` (`generate(kind, params)`, `extract_moves`; three protocols — `"cts"`, `"circle"` and `"diam"`, the temperature ladder the Viscosity and Activation Ea tabs SHARE) + `analysis/mew_circle_fit.py` (`fit_k`) (§2.15); run `test_gui_calibration.py` + `test_calibration_procedures.py` + `test_mew_sweeps.py` + `test_circle_sweep.py` + `test_diam_ladder.py` + `test_ui_shell.py` after |
| Fit the flat lag amplitude k from printed circles | `analysis/mew_circle_fit.py` (`contact_radius` / `amplitude_from_radius` / `commanded_radius` / `fit_k`) (§2.15); the gap it was fitted at is `Calibration.lag_amplitude_gap`; run `test_circle_sweep.py` after |

---

## 4. Test index (the suites live in `tests/`; each self-runs as `python tests/test_X.py`, prints PASS/FAIL, exits non-zero on fail)

**G-code page:** `test_gcode_editor.py` (11 cases, ~70 s, a real
withdrawn modern app): the linked text equals the Save seam's, an output
flip adds no chip, Insert anchors at the caret, all chips off gives the
designer's output, Undo restores the chip set, Generate carries
`--gcode-doc` and the page's own regen never does, the preset round trip,
the flavor follows the Machine page, Esc in a form never reaches the app,
Save writes the page's text. Every case RED-proven by an in-memory
injection. `test_gcode_collector.py` (11
cases, same harness): the ribbon follows the collector, a click loads the
axis defaults, 5 axis cannot be picked, the plate rows bind the machine's
vars, Tube binds the page's design, winding angle reaches the argv, a
collector switch or a Tube edit regenerates the text, closed panels hand
the handles back, the Collector tree row, the Presets Machine kind. The Move tool: `test_move_gimbal.py`
(5 cases, headless + one VTK offscreen: the hit order, a square beats a
crossing arrow, a drag keeps the grabbed point under the cursor; RED via
`--red <arm>`), and `test_gcode_editor.py`'s `move_tool_moves_selection`.

**Saved flavors:** `test_flavor_presets.py` (23, headless, ~1 s: the store
read back from disk, a duplicated Aerotech exports byte for byte as
Aerotech, a machine preset's carried copy recreated elsewhere, the old
Custom slot adopted) + `test_flavor_editor.py` (6 cases, a real withdrawn
modern app: one editor from both doors, duplicate/edit/save/rename/delete,
unsaved edits never lost, the carried copy, the unread-move notice, the Save
gap).

**Layer shift:** `test_layer_shift.py` (~20 s): the papers' definitions on
the real generator's wavy and straight scaffolds (X step, phase along each
pass's own direction, amplitude in µm and %, radius, profiles, scope), arcs
keep their circle, pass ends and turnarounds byte-identical, no fold-back at
a 0.6 mm slide, rotary refused, opened file / pre-link map reduced to X / Y.
`--red` runs its five injections and fails if one stays green. GUI case:
`test_gcode_editor.tool_layer_shift`.

**G-code document engine:** `test_mew_gcode.py` (15 groups, ~45 s; name
filters as argv): the moved rewrite == the pre-move `gui_scaffold` loaded from
git at `ae783efb` (16k option combinations, every `test_flat_layer_z` /
`test_gui_preview` fixture); the line map leaves the g-code byte-identical and
matches the emitter's structure; WYSIWYG round trip; edits carried over a
redesign by structure; rules, transforms, flavors (convert-and-back, motion
identical in every flavor), pieces, persistence, `--gcode-doc`. RED proofs in
its docstring.

**Physics:** `test_mew_physics.py` (61, scaling laws), `test_mew_field.py`
(13, field properties), `test_mew_kinematics.py` (28, planner laws), `test_mew_deposition.py` (40,
touchdown geometry incl. converter-seam end-to-end + coil + `speed_scale` fold), `test_mew_live.py` (34,
forward-accum re-lay), `test_mew_incremental.py` (25, **incremental==batch bit-identity** + O(1) edit +
scrub + locked-seed ride + edited-region EXPORT-relay routing),
`test_mew_calibration.py` (27, profile↔models round-trip + registry drift guard + guided-fit round-trips),
`test_law_lang.py` (15, the user-replaced-law seam: the `shipped` transcriptions vs the `mew_physics` methods,
the whitelist refusing `__import__` and friends, a broken law raising rather than reverting, and the folded
`active_source` agreeing with `shipped` at both a factory and a fitted calibration),
`test_mew_travel.py` (15, **continuous jet / travel deposition**: OFF byte-identity incl. passing travels
without the flag, parser travel capture + exact shared junction vertices + G0-runs-at-sticky-feed,
touchdown continuity across G0, index alignment, cruise lag still exactly v·τ, settle-tail relocation,
leading travel now deposited (origin→first-part approach, ignites at rest, matches a monolithic glued
reference), missing-travel re-ignites at rest, no faked stop with kinematics on).
`test_mew_unit_cell.py` (109, **the unit-cell geometry**, no g-code in it at all: per family the
tiling is connected, closes the circumference to 1e-9 and merges its seam; the route covers every
member exactly once plus its retraces and nothing else, with a RED control that perturbs one point;
the re-entrant retrace is exactly the interior strut set — McCosker's own doubled struts, checked as
a SET — alongside the control showing a length-only postman would have doubled the ribs instead; the
closed brick's retrace is one ring's worth; unfold strains against hand-computed values and their
inverse; both fit modes; `amplitude = 0` identical to the straight family; 12 refusals),
`test_custom_cell.py` (79, **the DRAWN cell family**: its comparator is a different tiler — a drawn
brick must reproduce `_brick_members`' interior member for member, differing by exactly `n_around`
members and exactly `period_v`, the one end ring a torus tile cannot have. Three RED controls, all
verified red: a node 1e-6 off the v = 1 boundary and one off u = 1 must each disconnect the tiling,
and the coverage check must reject a moved route point. A single nudge on the HONEYCOMB does NOT
disconnect it — two ring welds — and that non-control is asserted as its own fact, because it was
written as the control first and proved nothing. Also: the four built families' route figures
asserted against numbers captured on `main` BEFORE this family existed; parse/format a fixed point;
the three-cell tile reproducing the full route's retraced fraction exactly and its under-CTS score
inside 1 point, with `reversals` explicitly NOT transferring; `preview_route` inside a
`perf_budget` ceiling; 14 refusals),
`test_mandrel_unit_cell.py` (43, **the unit-cell emitter**: every family round-trips through
`parse_gcode(frame=CylinderFrame(...))` to its routed layer within 4.8e-7 mm, with its own RED
control; one path per layer at one height; each layer closes on a whole circumference and the next
starts there, with half a layer as the RED control; A on every G1 and unwrapping across layers; the
repo's g-code rules; layers counting whole sheets; and `--tube-family zigzag` byte-identical to `generate_mandrel_gcode`. Also MEASURES what the
default 4-decimal formatting costs — 4.311e-5 mm, against a 5.0e-5 mm quantisation bound),
`test_mandrel_blocks.py` (45, **layer blocks and copies**: winding x4 / re-entrant x4 / winding x4 parses to 12
layers, each its own block's sheet rebuilt from the design and `tube_cell_route`, with a same-shape
wrong-sheet RED control; heights by each block's step; joins with no turn; per-block speed; length,
rod, layer, step and speed refusals; a custom cell restarted on the near end; the `--tube-blocks`
CLI writing the call's own program, inline JSON too; 3 copies side by side, the first the u-mirror
of the sheet and the same cell circuit by segment multiset, each with its RED control; the exit-gap
refusal; `tube_layer_blocks` against hand-written groupings),
`test_mandrel_split_feed.py` (43, **Aerotech split feeds**: every G1 of a zigzag, omega, offset-datum,
re-entrant and brick tube lasts L/S under Aerotech's documented rule and under a slower-axis
reading; the split file parses to the combined file's fibres and per-segment clock in
`parse_gcode` and `parse_path`; G75/G76; no F0/E0; the estimate and the G-code page's checks.
RED: 11 cases fail against the pre-change readers, and a 10 % wrong F is caught),
`test_mew_mandrel.py` (49, **term A, the Neuhaus 2025 placement deviation**: 18 Fig 3 inset
cases against the printed figure, the ported reference fixture to 1e-9, two degenerate controls
that must swing (flat limit, zero lag), the three silent traps of `mew_mandrel`'s docstring, the
OFF bar at shipped defaults, and the tangency-bound turnover table -- capped D monotone,
uncapped D peaks then falls)

`test_mandrel_placement.py` (26, **the Q10 contact placement**: flat byte-identity vs a digest
captured BEFORE the change, cross-track == `mew_mandrel.deviation` to 2e-16, the two mirror
symmetries, the flat limit, the whole g-code→parser→CylinderFrame→deposition chain, and a
no-op-placement CONTROL that must lay nothing),
`test_mew_lag_fit.py` (27, **the micrograph calibration**: reproduces `run_stage2.py`'s published scipy
inversion of Neuhaus's own images to −0.00% (R_ax 0.8827 / R_cir 0.6989, RMS 1.50 vs 1.53 µm) and so
recovers her jet-camera radii to +4.5% / +10.9%; her three-angle subset alone lands within 3%. Plus every
control: a known pair round-trips, the 0/90° nulls provably do not move the answer, an injected 5% scale
error is reported and lands on `k` 8.6× harder than on the shape, gaps read the wrong way round RAISE, and
a MIS-TRANSCRIBED row pins the anisotropy at its ceiling with the RMS note firing instead of returning a
ratio no cylinder can produce),
`test_dynamic_diameter.py`, `test_physics_diameter.py`, `test_sim_diameter_wiring.py`,
`test_sim_elevation_wiring.py`, `test_jet_source.py`,
`test_ui_shell.py` (17, modern-shell smoke + **persist-map parity** vs the classic App + the four embedded
viewer panels incl. **micrograph** — run after any `gui_shell`/`gui_home`/`gui_modern_theme`/`gui_fonts`
change AND after adding a persisted GUI field),
`test_viewer_embed.py` (25, embedded-viewer protocol: sidebar parity + **slider/number export + `ctl`/
`values` dispatch** + fullscreen slider drag + the two **ViewCube** child-process guards — the toggle ships
honest in the hello and flips both ways, and a forwarded click SNAPS the view in the embedded panel),
`test_view_cube.py` (37, the **ViewCube** (§2.8): the 26 face/edge/corner directions round-trip through the
analytic pick with NO window, the tiny-window viewport clamp, one-scale label fitting, layer/depth (the
orbit pivot survives outside the gizmo box), pick→snap, and hide-props-never-the-renderer),
`test_micrograph_viewer.py` (5, the VTK micrograph-overlay
viewer: CLI, image→texture, transform, `.depo` scaffold, end-to-end embed),
`test_mew_sweeps.py` (38, the Sweep Designer engine: ladder/packing/routing/origin-offset/extract_moves +
specimen labels + never-erase oversize + feed-ceiling + with_times + layout modes/order/manual placement +
the space-efficiency guard + per-ε-rep unique labels + dense-pack clears the base band + pin-after-order +
oversize-message axis/dedup),
`test_diam_ladder.py` (9, **the melt temperature ladder** -- the print the Viscosity and
Activation Ea tabs SHARE, one slide per rung. The only suite in the repo whose comparator is
OUTSIDE it: the research-paper repo's realised trial file
`data/Trials/Fiber diameter 1/master gcode all temps F700.gcode`, which the generator
reproduces 397 lines byte-for-byte with only the two departures this module already documents
(no `M30`, feed on the base pad's opening `G0`). It SKIPS loudly, never passes, when the paper
repo is not mounted. Plus: every rung is the same slide; no `M104`/`M109`/`;` is ever emitted;
slide 1 is the anchor rung whatever order the ladder is typed in (RED-proved against BOTH an
ascending and a descending sort); one CSV row per laid line; oversize named not dropped and
never spilled onto a second slide; a sub-20 mm line warns; and a round trip through
`gcode_to_fibers.parse_gcode` recovers 5 specimen polylines at the commanded pitch. All nine
cases were run against an injected defect and the observed message is quoted in each docstring),
`test_circle_sweep.py` (22, **the flat lag-amplitude calibration**: the closed-form circle steady state
against `integrate_contact_path_ray` marching a real circle, to 0.40 µm over 12 geometries, with `k -> 0`
returning the commanded radius exactly and a real `k` moving it 0.3–1.7 mm; the inversion round-trips over
gap × eps × k; wrong-way and sub-CTS inputs RAISE; a planted amplitude comes back exactly and a believed
CTS 15% low shows as a +29.5%/decade trend — the flatness check's RED control. Plus the layout claim as
geometry: no emitted move crosses a measured arc, with its own RED control (displace one circle onto the
toolpath and the check must fire), the chord tolerance, per-circle feed on the lead-in, no `;`/`M30`,
never-erase, and CTS + lag g-code byte-identical to `0ea7371`. And the WHOLE chain — g-code text →
`parse_gcode` → `mew_deposition` → measured radius → `amplitude_from_radius` — returns a planted k = 1 to
0.69%, with the chord tolerance as its own RED control: a 20x coarser arc moves the recovered k from 1.007
to 1.037, because the lap is a POLYLINE whose mean radius sits below the commanded circle and the fit
inverts an ideal one. And the flatness check's own FLOOR: the placement rule trends on its own
(+18.9 %/decade at the shipped lag/radius, against +10.5 % for a CTS believed 5 % low), so `fit_k` reports
the observed trend against `ambiguity_trend` and the suite holds the case where a 5 % CTS error does NOT
clear the floor — a limitation pinned as a test, not a pass),
`test_gui_calibration.py` (24, the Calibration page on the real ModernApp: mount as an INPUT mode +
"Generate" label, all seven cards mount in the registry's order and each Apply writes ITS cal.* key, the
lag-circle card recovers a planted k from typed rows AND from its own exported CSV, Apply writes the gap
beside k, "Show g-code" and "Show deposited" drive different actors, the plate follows the Build-plate page
and the diameter the Scaffold page, no square widget or sw.lag_* key survives, the cards open at the retired
chips' values, bottom-bar dispatch, debounced rebuild, never-erase (an unfittable ring is NAMED in oversize,
not dropped), batch g-code export + optional-CSV is byte-identical to the engine, Generate freshens a
debounce-stale slide, the build honours Cancel). Seven of its cases carry verified RED injections.

`test_moonraker_link.py` (14, the **Machine link** engine §2.18: info/query round-trips, g-code-script +
upload encoding — byte-identity, start flag, newlines/spaces — print-control + estop verbs, the api-key 401
gate, heater/sensor/limit discovery, the poller — moving position, temps, console delta, cadence — and its
reconnect-after-flaky recovery, the CSV recorder; all against the in-file `FakeMoonraker`, which also runs
via `--serve`), `test_machine_live.py` (4, the `gcode_player --machine-live` child: CLI contract needs
`--machine`; hello has_transport=False/has_readout=True + a non-blank frame; a `livepos` message moves the
machine + updates the readout; reuses `test_viewer_embed`'s `Child` harness), `test_gui_machine_link.py`
(11, the page on the real ModernApp: mount + non-input + no-persist-leak, the pure jog/home/temp(clamped)/
override/door-unlock/move-rel script builders, the upload-rename sanitizer + effective name, the
temperature/PWM graph (reset + relative-clock sampling + axis autoscale + wall-clock format + theme
recolour), connect→command→disconnect vs `FakeMoonraker` (incl. live graph samples + editing a work-coord
box to move an axis with the G92 offset honoured + the mid-print move gate), upload byte-identity + rename
+ confirm gate, the g-code-file browser (list-on-connect + paging + confirmed delete + clear-on-disconnect),
prefs round-trip). `test_moonraker_link.py` also covers file list/upload-appears/download-round-trip/
delete/404 (`test_file_management`).

**Core:** `test_machine_model.py` (864, multi-body model), `test_player_scene.py`
(player scene with real STLs), **`test_gui_buildplate.py`** (15 — the Machine
page describing a five-axis TABLE: `stl` + picked `pivot` per joint through
`MachineProfile.from_dict`, the z,x,y→x,y,z flip, an unpicked joint writing no
pivot, `slide_offset` [0,0], the pair's bed slot saying bed, the kind radios
legible with no stage, the joint picker's flags, the persist keys. It is the
only suite that reads what the PAGE writes — `test_machine_model.py` and
`test_rotary_mount.py` both build their `rotaries` list by hand, so both stay
green whatever the page emits, including nothing. Five verified RED injections
in its docstring), `test_build_plate.py`, `test_footprint_shapes.py`, `test_scaffold_gcode.py`
(linking loops), `test_layer_rotation.py`, `test_link_loop_sag.py`, `test_vtk_helpers.py`,
`test_stl_viewer_args.py`, `test_depo_render.py` (deposition facet fidelity ≥ STL).

**Visual (§2.19):** `test_thumbnail.py` (11 — both offscreen render modes:
`--thumbnail`, and `--shots`' view set, Z-exaggeration split, signed underside
clearance, `.depo.npz` stem). `tools/capture_ui.py` is itself a check, run with
`--generate --shots .claude/visual/shots`.

> After touching `apply_sag`/`_SagState` in `engine/gcode_to_fibers.py`, run `test_mew_incremental.py`
> (guards the live-march == batch bit-identity). After any `mew_*.py` change run the matching suite.

#### 4a. The suites, by group

`ls tests/test_*.py` is the complete list. Tests **self-run — no pytest** (it isn't
installed). Each prints `PASS/FAIL` per case and exits non-zero on any failure.

```bat
rem  physics core
python tests/test_mew_physics.py
python tests/test_mew_deposition.py
python tests/test_mew_incremental.py
python tests/test_mew_fusion.py
python tests/test_sag_touchdown.py   rem  a droop lands TANGENTIALLY where the law puts it
python tests/test_sag_approx.py      rem  the APPROXIMATE sag solver's contracts

rem  viewers
python tests/test_viewer_embed.py    rem  embedded-viewer protocol, incl. slider/number

rem  pages + engines
python tests/test_gui_calibration.py rem  the Calibration page; needs a display
python tests/test_moonraker_link.py  rem  Machine-link engine (client/poller/fake rig)
python tests/test_machine_live.py    rem  gcode_player --machine-live child
python tests/test_lite_server.py     rem  Lite's HTTP surface, the 41-case feature matrix, the Lambda adapter
python tests/test_lite_auth.py       rem  the password gate: the closed door, forged tickets, revocation
python tests/test_ui_shell.py        rem  modern shell + persist-map parity (the no-drop check)
python tests/test_scenario_grid.py   rem  the scenario table: 3 collector modes x generate/reload/preview, 6 shared rules; add a row or a rule here, not a suite
python tests/run_suites.py --gate     rem  the 14-suite check (GATE_SUITES in run_suites.py), ~2 min

rem  five-axis collectors -- THE MATRIX first; it is the gate for any five-axis change
python tests/test_five_axis_matrix.py      rem  4 library shapes + both sag settings + the 2 imports + 3 synthetic, under 5 min
python tests/test_five_axis_matrix.py --slow    rem  + the imported block's full exact-sag pipeline (~65 s more)
python tests/test_five_axis_matrix.py --reuse   rem  re-judge the last run's files; do not regenerate
python tools/collector_checks.py rotary FILE.gcode   rem  per-move C/A deg, deg/s, deg/s^2 on any five-axis file
```

**Whole set:** `run_suites.py --gate` once a feature is finished, `run_suites.py` at a
major checkpoint, `run_suites.py --overnight` for the slow five-axis matrix. Each logs to
`.claude/test_ledger/runs.jsonl`. The set was cut from 139 suites to 29 on 2026-10-30, and a
section of this file may still name a removed one.

**Wall clock.** `run_suites.py` bounds every suite (`--timeout`, default 900 s). Run
a suite on its own under a timeout too (memory `test-runs-must-be-bounded`).

**In a cloud session** (Ubuntu, no GPU) every Tk/VTK suite needs a display —
`CLAUDE.md` carries the `xvfb-run` invocation and the `tools/perf_budget.py` scaling
rules, because a wrongly-run cloud suite reports a false number rather than
failing.

**Windows Codex Tcl note:** Python 3.13's Tcl runtime under the user's
`AppData\Local\Programs\Python\Python313\tcl\tcl8.6` is valid
(Tk 8.6.15 starts in host context). A sandboxed shell may instead report
`Can't find a usable init.tcl`; run Tk/VTK suites in the approved host context
before attempting a Tcl repair or reinstall.

---

#### 4b. Open reds

`run_suites.py` and `--gate` are expected to be green apart from these. A red in
the default run is either new or listed here; there is no other known-red list.

- `test_five_axis_matrix.py` (`--overnight`): 7 lead-in rotary cases. Do not tune
  the limiter to clear it; the deciding number is on the machine.
