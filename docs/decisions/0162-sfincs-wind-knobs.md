# ADR 0162 - SFINCS wind timeseries + wind-drag curve knobs

Date: 2026-08-06

Status: accepted

## Context

The ADR 0152 wave triaged all 13 SFINCS CAND-S board rows and folded most of
them, but two "Wind forcing" rows never received a disposition due to an
extraction bug in that wave: `uniform_wind_timeseries_forcing` and
`wind_drag_coefficient_curve_tuning`. Both are S-tier hypotheses under the
triage-first law; this ADR closes them out, completing the SFINCS CAND-S
tier (zero `[CAND-S]` rows remain in the SFINCS section of
`docs/validation/module-coverage-board.md`).

Prior state:

- Constant uniform wind already shipped: `WindForcing.magnitude`/`direction`
  -> `setup_wind_forcing(magnitude=, direction=)` (a two-row bracket spanning
  the whole sim window). The board row asks for a genuine TIME SERIES
  (`sfincs.wnd`: t, magnitude, direction schedule) -- a distinct capability.
- A flat wind-drag override (`wind_drag`, ADR 0152) already wrote a constant
  `cdval: [cd,cd,cd]` curve with `cdnrb: 3`. The board row asks for a CUSTOM
  multi-point breakpoint curve (arbitrary `cdwnd`/`cdval` pairs) -- the
  Vatvani et al. 2012 drag-saturation curve, not a single constant.

Both mechanisms were verified against the live SFINCS v2.3.3 solver image
(`deltares/sfincs-cpu:sfincs-v2.3.3`) and hydromt_sfincs 1.2.2:

1. `SfincsModel.setup_wind_forcing(timeseries=None, magnitude=None,
   direction=None)` already accepts a `timeseries` kwarg: a tabulated CSV
   read via `data_catalog.get_dataframe(timeseries, time_tuple=(tstart,
   tstop), parse_dates=True, index_col=0)` -- absolute-datetime index,
   magnitude in the second column, direction in the third (the column ORDER
   hydromt hardcodes onto `["mag", "dir"]` regardless of header text).
   `SfincsModel.write_forcing` then writes the SAME native `sfincs.wnd`
   ASCII artifact the constant path already produces.
2. `SfincsInput` (hydromt_sfincs.sfincs_input) carries three real attrs for
   the drag curve: `cdnrb` (int, breakpoint count), `cdwnd` (list, wind-speed
   m/s breakpoints), `cdval` (list, drag coefficients) -- confirmed live
   against `SfincsInput.__init__` (defaults `cdnrb=3, cdwnd=[0.0,28.0,50.0],
   cdval=[0.001,0.0025,0.0015]`) and `.write()`/`.read()` (space-joined list
   I/O, e.g. `cdwnd = 0.0 28.0 50.0`). `setup_config` is a full passthrough
   (`SfincsInput.from_dict` does a raw `setattr` per key -- the ADR 0152
   fact), so any resolved list reaches `sfincs.inp` verbatim.

The ACTIVE offline/local path (`run_solver` local-docker / `model_flood_scenario`)
builds the deck IN-AGENT via `sfincs_builder.py` and runs the STOCK
`deltares/sfincs-cpu` container as a pure solver -- no worker/image code is
touched by this landing, so NO image rebuild is needed (WORKER-IMAGE LAW,
same as ADR 0152).

## Decision

Zero new tools, zero new templates. Registry / EXPECTED_TEMPLATES unchanged.

**Row: `uniform_wind_timeseries_forcing` -> LANDED.**

- `WindForcing` (sfincs_builder.py) gains an optional `timeseries` field: an
  ordered list of `(t_s, magnitude_mps, direction_deg)` tuples (`t_s` =
  seconds since sim-start). Precedence in `_emit_surge_forcing_blocks`:
  gridded (`grid_uri`) > schedule (`timeseries`) > constant
  (`magnitude`/`direction`) -- a `None` timeseries leaves the constant path
  byte-identical (test-locked).
- `_write_wind_timeseries_csv` (new helper) materialises the schedule to a
  CSV (absolute timestamps off the module-level `SFINCS_TREF` constant,
  hoisted from a function-local so the wind-schedule emitter can share it),
  staged inside the SAME per-build temp dir `build_sfincs_model` already uses
  for the deck (`_generate_hydromt_yaml_config` / `_emit_surge_forcing_blocks`
  both gained an optional `build_dir` parameter to thread it through --
  following the existing forcing-file-staging pattern the spiderweb `.spw`
  and reclass-table CSV already use). The CSV is a hydromt-internal STAGING
  input (read once during `model.build()`), not a deck output artifact, so it
  is NOT part of the uploaded deck manifest.
- `_build_surge_forcing_members` (sfincs_forcing_autowire.py) threads a new
  `"timeseries"` sub-key through the workflow-facing `wind` dict param on
  `sfincs_flood` / `model_flood_scenario`, so a caller can pass
  `wind={"timeseries": [(t_s, mag, dir), ...]}` end-to-end.

**Row: `wind_drag_coefficient_curve_tuning` -> LANDED.**

- A new `wind_drag_curve` key in `physics_registry['sfincs']`, using a new
  registry value shape: `"type": "float_pairs"` (a sentinel string, not a
  python type object) validated by a dedicated branch in
  `_coerce_and_check` -- an ordered list of `>=2` `(x, y)` pairs, each
  column range-checked (`range` is `((wind_lo, wind_hi), (cd_lo, cd_hi))` =
  `((0.0, 100.0), (0.0, 0.01))`), with `x` (wind speed) required STRICTLY
  INCREASING (a physical breakpoint axis, not just a bounded scalar). This is
  a general-purpose extension any future engine's breakpoint-curve knob can
  reuse.
- `_emit_physics_config` (sfincs_builder.py) writes a present `wind_drag_curve`
  as `cdnrb: len(curve)` / `cdwnd: [...]` / `cdval: [...]` -- the SAME
  `setup_config` passthrough keys the ADR 0152 flat `wind_drag` targets.
  Setting BOTH `wind_drag` and `wind_drag_curve` is ambiguous (both target
  the same three keys), so it raises a typed
  `SFINCSSetupError("WIND_DRAG_CURVE_CONFLICT")` rather than a silent
  last-key-wins YAML overwrite (Invariant 7).
- Surfaced on the `sfincs_advanced_numerical_physics_knobs` template's
  `_KNOB_KEYS` + function signature + docstring (the same fold pattern ADR
  0152 used for `viscosity`/`nuvisc`).
- `numerical_physics.py`'s input-review `SyntheticInput` construction was
  fixed to stringify non-scalar resolved values (`SyntheticInput.value` is
  scalar/str-only; the new tuple-of-pairs value would otherwise fail pydantic
  validation) -- a small, honest fix uncovered by exercising the new knob
  through the review gate, not a design change.

Constant-wind and flat-`wind_drag` paths are byte-identical when the new
fields are unset (test-locked in `test_sfincs_builder_surge_forcing.py`).

## Consequences

- `WindForcing` gains a `timeseries` field; `physics_registry['sfincs']`
  gains `wind_drag_curve`; the numerical-physics template gains one param.
  A run with neither set is byte-identical.
- WORKER-IMAGE LAW: unchanged from ADR 0152 -- the active local path does not
  execute the vendored `_sfincs_build/deck.py` twin, so this landing did NOT
  touch it (the two new rows are entirely in the in-agent build path). The
  vendored twin remains un-mirrored for these two rows; flagged as a residual
  for whenever the ON-HOLD s2z cloud worker resumes.
- The `float_pairs` registry type is a new, reusable primitive for any future
  multi-point breakpoint-curve knob (any engine).

## Evidence

- Offline slice (from repo root, `env -u TRID3NT_CACHE_BUCKET pytest`):
  `test_physics_registry.py` + `test_sfincs_archetype_decks.py` +
  `test_sfincs_autoscale.py` + `test_sfincs_builder_mask_active.py` +
  `test_sfincs_builder_surge_forcing.py` + `test_sfincs_forcing_adapter.py` +
  `test_sfincs_numerical_physics.py` + `test_sfincs_solve_domain_aoi_guard.py`
  + `test_sfincs_spiderweb.py` + `test_set_sfincs_parameters.py` +
  `test_template_hygiene.py` + `test_catalog_surfacing.py` +
  `test_door_dissolution.py` + `test_categories.py` +
  `test_model_flood_scenario_surge_plumbing.py` +
  `test_model_flood_scenario.py` + `test_model_flood_scenario_v2.py` = 268
  passed.
  - One order-dependent flake diagnosed and fixed en route (not a new bug):
    `test_sfincs_autoscale.py`'s env-override tests `importlib.reload()` the
    `sfincs_builder` module, rebinding a NEW `SFINCSSetupError` class in the
    same session; a new conflict test that trusted a top-of-file import of
    that class failed `pytest.raises` when run after the reload. Fixed by
    re-fetching the class off the live module at call time (the same
    defensive pattern `test_sfincs_spiderweb.py` already uses).
- LIVE SMOKE (downtown Chattanooga TN bbox, the `run_sfincs_direct.py`
  canary AOI, stock `deltares/sfincs-cpu:sfincs-v2.3.3`, `run_solver`
  local-docker): a 10 m/s@270deg -> 25 m/s@180deg ramping/veering 2-hour wind
  schedule PLUS a tuned `wind_drag_curve` in ONE `sfincs_flood` call.
  status=ok, depth COG published
  (`s3://trid3nt-runs/01KZC2V0MGNV2CZJT0QWW1ZQXV/overviews/01KZC2W9S8BGWPKV5B8XEMN1BQ.tif`).
  Deck `sfincs.inp` (fetched from
  `cache/static-30d/sfincs_setup/01KZC2TY2Z98VHZKWJ3A3B5KV9/deck/sfincs.inp`)
  carries, byte-for-byte:
  ```
  wndfile              = sfincs.wnd
  cdnrb                = 3
  cdwnd                = 0.0 28.0 50.0
  cdval                = 0.001 0.0025 0.0018
  ```
  The deck's staged `sfincs.wnd` (hydromt's own native write) round-trips the
  schedule exactly:
  ```
     0.0   10.00  270.00
  3600.0   17.50  225.00
  7200.0   25.00  180.00
  ```
  run_id `01KZC2V0MGNV2CZJT0QWW1ZQXV`; new MinIO run prefix + outputs
  confirmed (19 depth frames + peak + `sfincs_map.nc`, 3.65 MB).
- Proof: `docs/proof/templates/sfincs_advanced_numerical_physics_knobs_wind_schedule.png`
  -- wind magnitude (m/s) and wind direction (deg, from) vs time, two
  quantitative panels with legends, rendered through the QGIS plugin's own
  chart-dock interpreter (`qgis-plugin/trid3nt/ui/charts.py:render_spec`),
  composited from two genuine `render_spec()` calls (no re-interpretation of
  the data). SFINCS is a regular grid on this AOI -- no separate mesh
  wireframe deliverable (matches ADR 0152's precedent).
