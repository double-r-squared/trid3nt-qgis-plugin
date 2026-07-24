# V&V wave build contract (PINNED by the contracts agent, build start)

Authoritative for the 9-tool V&V wave (tool-list.md STATUS block; ADR 0021).
Every lane conforms to this file EXACTLY. Where this contract and a lane's own
judgement disagree, this contract wins; raise a blocker instead of diverging.

ASCII hyphens only. No typographic dashes anywhere in code, tests, or docs.

The 9 tools and their lanes:

- Lane A: `read_run_diagnostics` (folds the 5 per-engine readers into ONE
  dispatcher + internal parser modules).
- Lane B: `compute_skill_metrics`, `compute_flood_extent_skill`.
- Lane C: `extract_model_at_observations`, `fetch_high_water_marks`,
  `fetch_flood_extent_observation`.
- Lane D: `set_sfincs_parameters`, `set_swmm_parameters`,
  `set_modflow_parameters`.
- Integration agent: registration wiring, category membership, corpus patch,
  membership/retrieval tests (see section 4).

---

## 1. RAW-OUTPUT RETENTION (the load-bearing seam)

### 1.1 Finding: raw diagnostics ARE durably retained. No new stashing needed.

Every engine that dispatches through `solver.launch_local_solver` (SFINCS,
SWMM, MODFLOW, GeoClaw, TELEMAC) uploads its glob-expanded `outputs[]` PLUS
`<solver>.stdout` / `<solver>.stderr` to the runs bucket at
`s3://<runs_bucket>/<run_id>/`, and ALWAYS writes `completion.json` there
(`solver._supervise_local_run` -> `_write_local_completion`). That object store
is the durable index and is MinIO locally (`TRID3NT_RUNS_BUCKET=trid3nt-runs`,
`AWS_ENDPOINT_URL` from `.env.local`). The local rundir under
`$TRID3NT_RUNS_DIR` is scratch and may be reaped; the S3 upload is the durable
copy. Verified live against MinIO (141 completion.json, real diagnostics files
per engine). Per-engine retention, all CONFIRMED present in the run prefix:

| Engine  | Diagnostics files retained (in `s3://<runs_bucket>/<run_id>/`)                                  | outputs glob source |
|---------|-----------------------------------------------------------------------------------------------|---------------------|
| SFINCS  | `sfincs_map.nc` (cuminf/cumprcp/zsmax/storage), `sfincs.stdout` (timing, CFL, finished marker) | model_flood_scenario / run_swmm parity |
| SWMM    | `*.rpt` (Runoff + Flow Routing Continuity %, Highest Continuity Errors, Highest Flow Instability Indexes), `swmm.stdout` | `run_swmm.py` outputs `["*.out","*.rpt","*.tif"]` |
| MODFLOW | `mfsim.lst` (convergence/timing), `<model>.lst` e.g. `gwf_model.lst` (PERCENT DISCREPANCY budget + dry cells), `mf6.stdout` | `run_modflow.py` outputs `*.lst`, `mfsim.lst`, `**/*.lst` |
| GeoClaw | `geoclaw.stdout` ("Total mass at initial time" conservation signal), `gauge*.txt`, `fort.q*`/`fort.t*`, `fgmax*` | `run_geoclaw.py` GEOCLAW_OUTPUT_GLOBS |
| TELEMAC | `full_listing.log` (mass balance), `telemac_metrics.json`, `telemac.stdout`; metrics ALSO folded into completion.json extras | `run_telemac.py` |

Therefore `read_run_diagnostics(run_handle)` CAN reach PAST runs' diagnostics
files for every engine. No change is required to retain more files.

### 1.2 The ONE gap: engine identity is NOT in completion.json. Minimal solver.py change (LANE A ONLY).

The `completion.json` verified shape (live MinIO) is:

```json
{
  "run_id": "01KWRSECZGJEYBD44X6T0GRTT9",
  "status": "ok",                       // "ok" | "error" | "cancelled"
  "exit_code": 0,
  "sfincs_stdout_uri": "s3://trid3nt-runs/<run_id>/sfincs.stdout",
  "sfincs_stderr_uri": "s3://trid3nt-runs/<run_id>/sfincs.stderr",
  "output_uris": ["s3://trid3nt-runs/<run_id>/sfincs_map.nc", "..."],
  "started_at": "2026-...Z",
  "finished_at": "2026-...Z",
  "error": null
  // engine extras vary: geoclaw adds "scenario"/"error_code"/"publish_manifest_uri";
  // telemac adds "correct_end"/"npoin"/"wall_s"/... ; modflow adds "converged"/"model_crs".
}
```

There is NO `solver` / `engine` field. The stdout field NAME
(`sfincs_stdout_uri` / `swmm_stdout_uri` / `mf6_stdout_uri` /
`geoclaw_stdout_uri` / `telemac_stdout_uri`) is the only current engine
tell, which is fragile.

REQUIRED LANE-A SOLVER CHANGE (surgical, ~4 lines, `solver.py` ONLY):

1. Add a `solver: str | None = None` keyword param to
   `_write_local_completion(...)`.
2. Insert `"solver": solver,` into the `payload` dict it builds (place it
   after `"exit_code"` and BEFORE `**(extra or {})` so a spec's `extra` can
   never contain/clobber it).
3. In `_supervise_local_run`, pass `solver=run.spec.solver` at the
   `_write_local_completion(...)` call site.

This is forward-only. Legacy completion.json (all committed fixtures, all
existing MinIO runs) lack `solver`, so the reader MUST fall back (section 2.3).
Do NOT rewrite existing completion.json objects. Do NOT touch any other part
of solver.py.

---

## 2. HANDLE RESOLUTION

### 2.1 `read_run_diagnostics(run_handle: str, ...)` accepts, in priority order:

- (a) a bare run_id ULID: 26-char Crockford base32, regex
  `^[0-9A-HJKMNP-TV-Z]{26}$` (matches `trid3nt_contracts.new_ulid`).
- (b) any `s3://` URI UNDER the runs prefix: the run's `output_uri`
  (`s3://<bucket>/<run_id>/`), OR any object URI beneath it (a published COG
  such as `s3://<bucket>/<run_id>/flood_depth_peak.tif`, or a
  `completion.json` URI). Extract `run_id` as the FIRST path segment matching
  the ULID regex above.
- (c) fail typed if no ULID segment is recoverable (see 2.4).

Resolution yields `(runs_bucket, run_id)`. `runs_bucket` = the bucket parsed
from an `s3://` handle, else `solver._get_runs_bucket()`.

`run_handle` MUST NOT be added to `RESOLVABLE_URI_PARAMS` in
`uri_registry.py` (the tool self-resolves; the registry must not mangle a run
handle). Keep the param name `run_handle`. This mirrors `list_run_frames(run_id)`
but is a strict superset (also accepts the s3 URI/prefix the LLM usually holds).

### 2.2 Reaching the artifacts (reuse existing solver seams; no new plumbing):

- completion.json: reuse `solver._try_get_completion_s3(runs_bucket, run_id)`
  (returns the parsed dict or `None`).
- diagnostics bytes: reuse `solver._read_object_bytes(uri)` on each
  `output_uris[]` entry / the `<solver>_stdout_uri` you need.
- S3 client / bucket overrides for tests: reuse `solver.set_s3_client(...)` /
  `solver.set_runs_bucket(...)` (the dict-backed fake used by
  `test_solver_local_docker.py`).

### 2.3 Engine identity recovery (post-fix + legacy fallback):

```
engine = completion.get("solver")                         # section 1.2 fix
if engine is None:                                        # legacy fixtures / old runs
    for key in completion:                                # infer from the stdout field name
        if key.endswith("_stdout_uri"):
            engine = {"sfincs":"sfincs","swmm":"swmm","mf6":"modflow",
                      "geoclaw":"geoclaw","telemac":"telemac"}[key[:-len("_stdout_uri")]]
            break
```

Note `mf6_stdout_uri` -> engine `"modflow"`. If neither path resolves an
engine, raise the typed `DIAGNOSTICS_ENGINE_UNKNOWN` error.

### 2.4 Typed errors (FR-AS-11 convention; class attrs `error_code` + `retryable`):

- `RUN_HANDLE_UNRESOLVED` (retryable=False): no ULID recoverable from
  `run_handle`.
- `DIAGNOSTICS_RUN_NOT_FOUND` (retryable=False): no completion.json at the
  resolved prefix.
- `DIAGNOSTICS_ENGINE_UNKNOWN` (retryable=False): engine identity not
  recoverable.
- `DIAGNOSTICS_ARTIFACT_MISSING` (retryable=True): completion.json present but
  the engine's diagnostics file is absent from `output_uris`/`stdout_uri`.
- `DIAGNOSTICS_PARSE_ERROR` (retryable=False): a diagnostics file was found but
  could not be parsed. NEVER return a fabricated healthy result on parse
  failure; raise.

Honesty floor: a value the engine does not report is `null`, never invented.
A derived value carries `mass_balance_source="derived"`. A parse/IO failure is
a typed exception carrying engine + run_id + the offending file, never a
silent None or a fake `healthy=true`.

---

## 3. ENVELOPE SCHEMAS (exact field names; `null` where an engine does not report)

All four are plain JSON-serializable dicts (or a `LayerURI` subclass carrying
the fields, for the two that also emit a map layer). Every field an engine
does not report is `null`. Lists default to `[]`, never `null`.

### 3.1 (a) Diagnostics envelope -- return of `read_run_diagnostics`

```json
{
  "engine": "sfincs|swmm|modflow|geoclaw|telemac",
  "run_id": "<ULID>",
  "status": "ok|error|cancelled",              // from completion.json
  "healthy": true,                              // HEURISTIC roll-up (see below); null when indeterminate
  "mass_balance_pct": -0.018,                   // signed % continuity/volume error; null if none reported and none derivable
  "mass_balance_source": "reported",            // "reported" | "derived" | null
  "instability": 24,                            // engine instability signal (see per-engine); null if none
  "nonconverged_pct": 0.0,                      // % steps/iterations not converged; null if N/A
  "dry_cells": 0,                               // MODFLOW dry-cell count; null for other engines
  "warnings": ["<verbatim warning line>"],      // [] if none
  "engine_specific": { },                       // engine-namespaced extras (below); {} allowed
  "sources": {
    "completion_json": "<uri-or-path>",
    "diagnostics_files": ["<uri-or-path>"]
  },
  "notes": ["<provenance / derivation note>"]
}
```

`healthy` is a coarse HEURISTIC roll-up, not a gate: `true` when
`status=="ok"` AND `abs(mass_balance_pct)` is within the engine band (SWMM/MODFLOW
< ~1%, or `mass_balance_pct is None` with no other flags) AND no nonconvergence
/ instability flag trips; `false` when a hard flag trips (nonconvergence,
dry-cell blowup, mass balance out of band, `status!="ok"`,
`correct_end==false`); `null` when indeterminate (missing files, unparseable).
The raw fields are authoritative; a `notes[]` entry states the heuristic used.

`engine_specific{}` keys per engine (all values `null` when unreported):

- sfincs: `max_water_depth_m`, `cfl_limiting_pct`, `cfl_limiting_cell`,
  `avg_timestep_s`, `runtime_s`, `cumprcp_m3`, `cuminf_m3`,
  `storage_delta_m3`, `boundary_flux_m3`, `finished` (bool). Top-level
  `mass_balance_pct` is DERIVED (`mass_balance_source="derived"`) from
  cumprcp vs cuminf + net boundary flux + storage delta (SFINCS has no
  explicit continuity field); `instability` = `cfl_limiting_pct`.
- swmm: `runoff_continuity_pct`, `flow_routing_continuity_pct`,
  `max_flow_instability_index` (int), `flooded_nodes`, `surcharged_nodes`,
  `flood_volume`. Top-level `mass_balance_pct` = `flow_routing_continuity_pct`
  (`"reported"`); `instability` = `max_flow_instability_index`.
- modflow: `percent_discrepancy_pct` (max abs over stress periods/steps, from
  the per-model `<model>.lst` budget), `converged` (bool),
  `nonconverged_steps` (int), `dry_cells` (int), `per_model` (list of
  `{model, percent_discrepancy_pct}`). Top-level `mass_balance_pct` =
  `percent_discrepancy_pct` (`"reported"`); `dry_cells` mirrors the field.
  Confirm exact LST field text against `gwf_model.lst` fixture before
  hardcoding (research.md flags the mf6io doc was 403-blocked).
- geoclaw: `mass_initial`, `mass_final`, `mass_ratio` (final/initial, or
  null if final absent), `n_gauges`, `n_frames`. Top-level `mass_balance_pct`
  = `(mass_ratio-1)*100` when both present, `mass_balance_source="derived"`,
  else null.
- telemac: `correct_end` (bool), `npoin`, `nelem`, `wall_s`,
  `listing_mass_balance_pct` (from full_listing.log when parseable). Top-level
  `mass_balance_pct` = `listing_mass_balance_pct` (`"reported"`) or null;
  `healthy` keys off `correct_end`. Prefer the completion.json extras
  (already folded from telemac_metrics.json) over re-parsing where present;
  reuse `postprocess_telemac` parsing helpers rather than duplicating.

### 3.2 (b) Skill-metrics envelope -- return of `compute_skill_metrics`

```json
{
  "variable": "streamflow|stage|head|<generic>",   // "head" preset adds SRMS
  "n": 48,                                           // paired sample count
  "metrics": {
    "NSE": 0.81, "KGE": 0.77, "PBIAS": -4.2, "RSR": 0.44,
    "RMSE": 0.31, "R2": 0.83,
    "peak_error": 0.12, "peak_timing_error": null,   // null unless a time column is present
    "SRMS": null                                     // populated ONLY for variable=="head"; else null
  },
  "bands": {                                         // acceptance bands from research.md section 2.2; null where no codified band
    "NSE": {"satisfactory": ">0.50", "good": "0.65-0.75", "very_good": ">0.75"},
    "PBIAS": {"satisfactory": "<=25", "good": "<=15", "very_good": "<=10"},
    "RSR": {"satisfactory": "<=0.70", "good": "<=0.60", "very_good": "<=0.50"},
    "KGE": null
  },
  "suggested_verdict": "good",                       // very_good|good|satisfactory|unsatisfactory|indeterminate
  "verdict_is_heuristic": true,                      // ALWAYS true (thresholds are heuristics, not gates)
  "caveats": ["KGE has no graded acceptance band; diagnostic only"],
  "units": "m3/s",
  "notes": ["metrics via spotpy.objectivefunctions"]
}
```

Metric math wraps `spotpy.objectivefunctions` (NSE/KGE/PBIAS/RMSE native); do
not hand-roll bespoke formulas where spotpy provides them. RSR = RMSE /
stdev(obs); R2 = Pearson r^2; SRMS = RMSE / (max(obs) - min(obs)); peak_error
and peak_timing_error are simple derived quantities. `verdict_is_heuristic` is
ALWAYS `true`. When `n` is small, add a `caveats[]` entry and set
`suggested_verdict="indeterminate"` rather than a graded verdict.

Input: EITHER a `paired_table_uri` (a lane-C paired table -- see 3.3 format)
OR explicit `observed` + `simulated` arrays (+ optional `time`). The
`variable="head"` preset is the fold of the former standalone
`compute_head_calibration_stats` (#8); reconcile with
`compute_model_residuals` (extend/reuse its sampling; do not duplicate the
observed-vs-simulated pairing math). Lane B owns any edit to
`compute_model_residuals.py`.

### 3.3 (c) Pairing envelope -- return of `extract_model_at_observations` (a `LayerURI` subclass carrying these fields)

```json
{
  "layer_id": "model-obs-pairs-<seed>",
  "name": "Model-obs pairs (<n> points)",
  "layer_type": "vector",
  "uri": "s3://<runs_bucket>/model-obs-pairs-<seed>/paired.fgb",
  "paired_table_uri": "<same as uri>",     // the handle lane B consumes
  "style_preset": "model_obs_pairs",
  "role": "primary",
  "bbox": [w, s, e, n],

  "n_paired": 46,
  "n_dropped": 4,
  "dropped": [
    {"obs_id": "STN-12345", "reason": "outside_footprint"},
    {"obs_id": "STN-22222", "reason": "nodata_sample"}
    // reason in: outside_footprint | nodata_sample | unparseable_value |
    //            no_time_match | crs_reproject_failed
  ],
  "alignment": {
    "spatial": "bilinear_sample_at_point",   // how the model value was located
    "temporal": "none_static",               // "exact" | "nearest_within_tolerance:<sec>" | "none_static"
    "datum": "assumed_match",                // "assumed_match" | "unknown" | "<datum name>"
    "crs": "<model_crs> -> EPSG:4326"
  },
  "columns": ["obs_id", "observed", "simulated", "time"],
  "units_warning": "<always populated>",     // mirror compute_model_residuals honesty
  "notes": ["..."]
}
```

PAIRED-TABLE STORAGE FORMAT (so lane B can consume lane C's output): a
FlatGeobuf point layer, EPSG:4326, ONE feature per paired sample, with the
columns `obs_id` (str), `observed` (float), `simulated` (float), `time`
(ISO8601 str, nullable) PLUS any passthrough observation properties.
Time-series pairs at one station = N features at the same coordinate with
distinct `time`. Lane B reads it with `geopandas.read_file` and uses the
`observed` / `simulated` (and `time` for peak-timing) columns. This is the
`compute_model_residuals` output shape minus the `residual` column, on
purpose, so the two are interoperable.

### 3.4 (d) Setter envelope -- return of `set_sfincs_parameters` / `set_swmm_parameters` / `set_modflow_parameters`

```json
{
  "engine": "sfincs|swmm|modflow",
  "child_setup_uri": "s3://<runs_bucket>/<child>/manifest.json",  // NEW derived deck/setup handle
  "parent_model": "<parent setup_uri / handle the child derives from>",
  "changes_applied": [
    {"param": "manning_land", "scope": "global", "before": 0.04, "after": 0.06, "unit": "s/m^(1/3)"},
    {"param": "qinf", "scope": "global", "before": 0.0, "after": 3.5, "unit": "mm/hr"}
    // scope: "global" | "zone:<id>" | "cell:<range>" | "layer:<k>"
  ],
  "plausibility": [
    {"param": "manning_land", "value": 0.06, "in_range": true, "range": [0.011, 0.8], "note": "overland Manning n physical range (research.md 1.2)"},
    {"param": "qinf", "value": 3.5, "in_range": true, "range": [0.0, 100.0], "note": "uniform infiltration mm/hr"}
  ],
  "notes": ["derived child deck; parent left immutable (A.7 replace-not-reconcile)"]
}
```

Setters are DERIVE-not-mutate: write a CHILD deck/setup (new `child_setup_uri`)
and leave the parent deck immutable. `before`/`after` per changed param.
`plausibility[].in_range=false` is a WARNING carried honestly in the envelope,
NOT a hard error (a user may intentionally set an out-of-range value); a
genuinely invalid param (unknown name, wrong type) IS a typed error. Physical
ranges come from research.md section 1: overland Manning n 0.011-0.8,
imperviousness 0-100, infiltration >= 0, hydraulic conductivity K > 0, etc.
Prefer the package's own param API where it exists (hydromt-sfincs `setup_*`
for SFINCS, swmm-api/PySWMM for the `.inp`, flopy for MODFLOW deck arrays);
pyEMU/PstFrom is the calibration-LOOP machinery (group E, FROZEN) -- do NOT
pull it in for an atomic setter.

---

## 4. PLACEMENT + FILE OWNERSHIP

### 4.1 Category placement (reuse existing categories; NO new "validation" category)

All 9 map cleanly onto existing categories, so a 13th category is NOT
introduced (it would fragment the calibration surface the router already knows
and split it from `compute_model_residuals`). Placement (integration agent
lands these in `categories.py`):

| Tool | PRIMARY_CATEGORY | SECONDARY_CATEGORIES |
|------|------------------|----------------------|
| `read_run_diagnostics` | `hazard_modeling` | -- |
| `set_sfincs_parameters` | `hazard_modeling` | -- |
| `set_swmm_parameters` | `hazard_modeling` | -- |
| `set_modflow_parameters` | `hazard_modeling` | -- |
| `compute_skill_metrics` | `geographic_primitives` | `hazard_modeling` |
| `compute_flood_extent_skill` | `geographic_primitives` | `hazard_modeling` |
| `extract_model_at_observations` | `geographic_primitives` | `hazard_modeling` |
| `fetch_high_water_marks` | `hydrology` | `hazard_modeling` |
| `fetch_flood_extent_observation` | `hydrology` | `hazard_modeling` |

Rationale: A (diagnostics) and D (setters) operate directly ON an engine/run,
so they sit beside `run_solver` in `hazard_modeling`. B (metrics) and the C
pairing primitive are analysis primitives over model+obs, mirroring
`compute_model_residuals` (PRIMARY `geographic_primitives`, SECONDARY
`hazard_modeling`). The two C fetchers are observed flood data, siblings of
`fetch_usgs_nwis_gauges` / `fetch_sentinel1_sar` in `hydrology`.

### 4.2 File ownership (a lane owns ONLY its new files; NEVER edit another lane's files)

Lane A (`read_run_diagnostics` + the solver.py surgical change):
- NEW `server/src/trid3nt_server/tools/simulation/diagnostics/__init__.py`
  (the registered `read_run_diagnostics` dispatcher + handle resolution +
  the DiagnosticsEnvelope builder).
- NEW `.../simulation/diagnostics/sfincs.py`, `swmm.py`, `modflow.py`,
  `geoclaw.py`, `telemac.py` (internal per-engine parsers; NOT registered).
- EDIT `server/src/trid3nt_server/tools/simulation/solver.py` -- ONLY the
  `_write_local_completion` `solver` field addition (section 1.2). solver.py is
  LANE A EXCLUSIVE.
- NEW `server/tests/test_read_run_diagnostics.py`.

Lane B (`compute_skill_metrics`, `compute_flood_extent_skill`):
- NEW `server/src/trid3nt_server/tools/processing/compute_skill_metrics.py`.
- NEW `server/src/trid3nt_server/tools/processing/compute_flood_extent_skill.py`.
- EDIT `server/pyproject.toml` -- add `spotpy` (LANE B EXCLUSIVE; see 5.4).
- EDIT `server/src/trid3nt_server/tools/processing/compute_model_residuals.py`
  -- ONLY if reconciling the head-calibration fold (LANE B EXCLUSIVE).
- NEW `server/tests/test_compute_skill_metrics.py`,
  `server/tests/test_compute_flood_extent_skill.py`.

Lane C (`extract_model_at_observations`, `fetch_high_water_marks`,
`fetch_flood_extent_observation`):
- NEW `server/src/trid3nt_server/tools/processing/extract_model_at_observations.py`.
- NEW `server/src/trid3nt_server/tools/fetchers/hydrology/fetch_high_water_marks.py`.
- NEW `server/src/trid3nt_server/tools/fetchers/hydrology/fetch_flood_extent_observation.py`.
- NEW `server/tests/test_extract_model_at_observations.py`,
  `test_fetch_high_water_marks.py`, `test_fetch_flood_extent_observation.py`.

Lane D (`set_sfincs_parameters`, `set_swmm_parameters`,
`set_modflow_parameters`):
- NEW `server/src/trid3nt_server/tools/simulation/set_sfincs_parameters.py`,
  `set_swmm_parameters.py`, `set_modflow_parameters.py`.
- NEW (optional) `.../simulation/_setter_envelope.py` (shared SetterEnvelope +
  plausibility helper). If created, it is LANE D EXCLUSIVE.
- NEW `server/tests/test_set_sfincs_parameters.py`,
  `test_set_swmm_parameters.py`, `test_set_modflow_parameters.py`.

Integration agent ONLY (NO lane touches these):
- EDIT `server/src/trid3nt_server/tools/__init__.py` (the 9 import lines --
  add `from .simulation.diagnostics import read_run_diagnostics`, the two
  processing tools, the pairing tool, the two hydrology fetchers, the three
  simulation setters).
- EDIT `server/src/trid3nt_server/categories.py` (`PRIMARY_CATEGORY` +
  `SECONDARY_CATEGORIES` per 4.1).
- EDIT `server/src/trid3nt_server/data/tool_query_corpus.yaml` (corpus queries
  -- proposed by the contracts agent in the structured report; the integration
  agent lands them; NO OTHER agent touches this file, it has user WIP).
- Membership + retrieval-visibility tests
  (`retrieve_visible_tools(prompt, None, 8)` per the new-tool retrieval rule).

Registration idiom: each tool file carries its own `@register_tool(...)`
(match `_example_tool_template.py` and the target-category sibling). The
diagnostics `__init__.py` carries the `read_run_diagnostics` decorator so the
integration import fires it. Tool docstrings: lean, routing-first (What / When
to use / When NOT), no markdown tables, no dead refs; Bedrock truncates to
1000 chars so front-load the routing block.

---

## 5. FIXTURE INVENTORY

Offline-first: the committed test suite runs with ZERO network. Lanes COPY
(trimmed where large) the fixtures below into
`server/tests/fixtures/validation/<engine>/` and read them via a local
`_run_dir` seam (see 5.3). ONE manual live API call per NEW fetcher is allowed
to capture its fixture (lane C only); commit the captured bytes and run the
test against them thereafter.

### 5.1 Engine-diagnostics fixtures (lane A) -- concrete source paths NOW

Pull from MinIO (`AWS_ENDPOINT_URL=http://100.92.163.46:9000`, bucket
`trid3nt-runs`) using the repo MinIO env block (5.5). Copy the small files
verbatim; trim/synthesize the large ones.

- SFINCS -- MinIO prefix `s3://trid3nt-runs/01KWRSECZGJEYBD44X6T0GRTT9/`:
  `completion.json` (437 B, copy verbatim), `sfincs.stdout` (3.2 KB, copy
  verbatim -- carries timing + finished marker), `sfincs_map.nc` (1.79 MB --
  DO NOT commit raw; SYNTHESIZE a tiny `sfincs_map.nc` via netCDF4/xarray
  carrying just `cuminf`/`cumprcp`/`zsmax` + storage vars for the derived
  mass-balance path).
- GeoClaw -- MinIO prefix `s3://trid3nt-runs/01KWT8BJ7QET79PTENW5XC8WAT/`:
  `completion.json` (1.7 KB), `gauge00001.txt` (14 KB), `fgmax0001.txt`
  (12 KB) -- copy verbatim; `geoclaw.stdout` (65 KB -- TRIM to the
  "Total mass at initial time: 46341211214.59" line + surrounding gauge/mass
  context, a few KB). DROP the `fort.q*` frames (145 KB each; the parser reads
  stdout + gauge, not fort.q).
- TELEMAC -- MinIO prefix `s3://trid3nt-runs/01KXHE0B8V025C9DRZ0B180HHT/`:
  `completion.json` (1 KB), `telemac_metrics.json` (2.6 KB),
  `full_listing.log` (3.3 KB), `telemac.stdout` (0 B) -- copy all verbatim.
  NOTE this run is a FAILED run (`correct_end=false`) -- keep it as the
  negative fixture; ALSO synthesize/capture a `correct_end=true` completion +
  metrics for the healthy path.
- SWMM -- local `/tmp/swmm-01KY8FQ0ZJPBPXWP8KX7R0KV3F-k36okd9b/mesh.rpt`
  (191 KB -- TRIM to the "Runoff Quantity Continuity", "Flow Routing
  Continuity", "Highest Continuity Errors", "Highest Flow Instability Indexes"
  blocks, a few KB). No SWMM run with completion.json exists in MinIO, so
  SYNTHESIZE a `completion.json` fixture (`swmm_stdout_uri`, status="ok",
  `output_uris=["mesh.rpt"]`) pointing at the trimmed `mesh.rpt` in the fixture
  dir.
- MODFLOW -- local `/tmp/tmpl6jokb5a/`: `gwf_model.lst` (207 KB -- TRIM to the
  VOLUME BUDGET block with `PERCENT DISCREPANCY` + any dry-cell/convergence
  lines) and `mfsim.lst` (18 KB -- TRIM to the convergence/timing block). No
  MODFLOW run with completion.json in MinIO, so SYNTHESIZE a `completion.json`
  (`mf6_stdout_uri`, `converged`, `model_crs`, status="ok",
  `output_uris=["mfsim.lst","gwf_model.lst"]`). Alternate MODFLOW budget
  fixture available at `/tmp/tmp9yhlj3h6/t.lst`.

### 5.2 Lane B / C fixtures

- Lane B `compute_skill_metrics`: a tiny synthesized paired FlatGeobuf (3.3
  format) OR inline observed/simulated arrays in the test -- no engine fixture
  needed. `compute_flood_extent_skill`: two small synthesized wet/dry rasters
  (model extent + benchmark extent) written in the test via rasterio.
- Lane C `extract_model_at_observations`: a small synthesized model raster +
  a small point FGB (reuse the `test_compute_model_residuals.py` `_write_head_
  raster` / obs-geojson helpers as a pattern). `fetch_high_water_marks`:
  ONE live USGS STN flood-event HWM API capture -> commit under
  `server/tests/fixtures/validation/stn/`. `fetch_flood_extent_observation`:
  ONE live Sentinel-1 SAR / catalog capture (or a trimmed derived extent
  raster) -> commit under `server/tests/fixtures/validation/sar/`.

### 5.3 Offline read seam (lane A + any tool that reaches a run)

`read_run_diagnostics` MUST support a private `_run_dir: str | None` param
(mirrors `compute_model_residuals._output_dir`). When `_run_dir` is set, read
`completion.json` and the diagnostics files from that LOCAL directory (the
fixture) -- the offline test path. When `None` (production), resolve the handle
to `(runs_bucket, run_id)` and read via the solver S3 seams (2.2).
Alternative accepted: reuse `solver.set_s3_client(FakeS3)` seeded from the
fixture bytes (the `test_solver_local_docker.py` pattern). Either is fine;
`_run_dir` is preferred for a directory-of-files fixture.

### 5.4 Dependency note (lane B)

The agent venv (`venvs/agent`) has scipy, rasterio, geopandas, netCDF4,
xarray, pyogrio, shapely, numpy, flopy -- but NOT `spotpy`. Lane B adds
`spotpy` to `server/pyproject.toml` AND installs it into `venvs/agent`
(`venvs/agent/bin/pip install -e server` picks it up) so the OFFLINE test
passes. If a `spotpy` import fails at runtime, raise a typed
`SKILL_METRICS_DEPENDENCY_MISSING` error -- never silently skip a metric.

### 5.5 MinIO env block (read-only inventory / fixture pull; NEVER ambient AWS creds)

```
set -a; . /home/nate/Documents/trid3nt-local/.env.local; set +a
# exports AWS_ENDPOINT_URL, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
# AWS_REGION, TRID3NT_RUNS_BUCKET; boto3 client(endpoint_url=$AWS_ENDPOINT_URL).
```

---

## 6. TEST COMMANDS

Interpreter is the agent venv; run from `server/`. There is no pytest config in
`pyproject.toml`, so pass paths explicitly. `pytest-asyncio` is available (the
new tools are sync; async is unused here).

Per-lane (each lane runs ONLY its own new tests during build):

```
cd /home/nate/Documents/trid3nt-local/server
../venvs/agent/bin/python -m pytest tests/test_read_run_diagnostics.py -q          # lane A
../venvs/agent/bin/python -m pytest tests/test_compute_skill_metrics.py \
    tests/test_compute_flood_extent_skill.py -q                                    # lane B
../venvs/agent/bin/python -m pytest tests/test_extract_model_at_observations.py \
    tests/test_fetch_high_water_marks.py tests/test_fetch_flood_extent_observation.py -q   # lane C
../venvs/agent/bin/python -m pytest tests/test_set_sfincs_parameters.py \
    tests/test_set_swmm_parameters.py tests/test_set_modflow_parameters.py -q       # lane D
```

Integration gate (after all lanes land + wiring):

```
cd /home/nate/Documents/trid3nt-local/server
../venvs/agent/bin/python -m pytest tests/ -q            # full suite, ZERO network
# registry sanity: the 9 tools register + import cleanly
../venvs/agent/bin/python -c "import trid3nt_server.tools as t; \
  need={'read_run_diagnostics','compute_skill_metrics','compute_flood_extent_skill',\
'extract_model_at_observations','fetch_high_water_marks','fetch_flood_extent_observation',\
'set_sfincs_parameters','set_swmm_parameters','set_modflow_parameters'}; \
  have=set(t.TOOL_REGISTRY); print('MISSING', need-have); assert need<=have"
```

Every new tool also gets the model-free retrieval check per the standing rule
(integration agent): `retrieve_visible_tools(prompt, None, 8)` returns the tool
for its corpus queries (proposed in the contracts structured report).

Offline invariant: no test may hit the network. The one-live-call-per-fetcher
allowance (5.2) is a DEVELOPMENT-time capture only; the committed test runs
against the captured fixture.
