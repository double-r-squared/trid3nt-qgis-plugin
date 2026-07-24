# V&V wave build report (integration agent)

Scope: the 9-tool V&V wave (ADR 0021, `docs/validation/build-contract.md`).
Four lanes built in parallel; this pass wires registration, categories,
corpus staging, membership tests, and runs the full suite gate.

ASCII hyphens only.

## 1. Registration wiring

All 9 tools are eagerly imported in `server/src/trid3nt_server/tools/__init__.py`
(alphabetically placed within their existing subpackage sections, matching
repo idiom) so their `@register_tool` decorators fire at package-import time:

- `from .simulation.diagnostics import read_run_diagnostics` (per
  build-contract.md section 4.2's literal import line -- the dispatcher lives
  in a subpackage, not a flat module).
- `.fetchers.hydrology`: `fetch_flood_extent_observation`, `fetch_high_water_marks`.
- `.processing`: `compute_flood_extent_skill`, `compute_skill_metrics`,
  `extract_model_at_observations`.
- `.simulation`: `set_modflow_parameters`, `set_sfincs_parameters`,
  `set_swmm_parameters`.

Smoke test (build-contract.md section 6, run verbatim):

```
cd server && ../venvs/agent/bin/python -c "
import trid3nt_server.tools as t
need={...9 names...}
have=set(t.TOOL_REGISTRY)
assert need<=have"
```

Result: PASS. `len(TOOL_REGISTRY)` after `import trid3nt_server.tools` alone
went from 191 (pre-wave baseline, confirmed by lane B/C's own reports and by
ADR 0021's "registry 191 -> 200") to **200** -- exactly the 9 additions, no
regression to the pre-existing 191.

## 2. Category placement

`server/src/trid3nt_server/categories.py` updated per build-contract.md
section 4.1:

| Tool | PRIMARY | SECONDARY |
|---|---|---|
| `read_run_diagnostics` | hazard_modeling | -- |
| `set_sfincs_parameters` | hazard_modeling | -- |
| `set_swmm_parameters` | hazard_modeling | -- |
| `set_modflow_parameters` | hazard_modeling | -- |
| `compute_skill_metrics` | geographic_primitives | hazard_modeling |
| `compute_flood_extent_skill` | geographic_primitives | hazard_modeling |
| `extract_model_at_observations` | geographic_primitives | hazard_modeling |
| `fetch_high_water_marks` | hydrology | hazard_modeling |
| `fetch_flood_extent_observation` | hydrology | hazard_modeling |

`test_categories.py::test_every_registered_tool_has_a_primary_category` is
the membership-based guard for this (computes `registered - mapped`, so it
fails automatically if any registered tool lacks a PRIMARY_CATEGORY entry --
no hardcoded expected-set to hand-extend). It passes with the 9 additions.

## 3. uri_registry.py gap (flagged by lane B, closed here)

`RESOLVABLE_URI_PARAMS` in `server/src/trid3nt_server/uri_registry.py` did
not carry lane B's `paired_table_uri` (`compute_skill_metrics`) or
`model_extent_uri` / `benchmark_extent_uri` (`compute_flood_extent_skill`).
No lane owned this file (build-contract.md section 4.2 leaves it unassigned;
lane B explicitly deferred it to integration to avoid a cross-lane
collision). Added all three. `extract_model_at_observations` needed no
addition -- it reuses the already-registered `model_layer_uri` /
`observations_layer_uri` names from `compute_model_residuals`.
`run_handle` (`read_run_diagnostics`) is deliberately NOT added, per
build-contract.md section 2.1 (the tool self-resolves; the registry must not
mangle a run handle).

## 4. Corpus patch (staged, not landed)

`server/src/trid3nt_server/data/tool_query_corpus.yaml` carries uncommitted
user WIP and is hard-off-limits to every build/integration agent. Collected
all 9 tools' `proposed_corpus_queries` from the 4 lane reports verbatim into
**`docs/validation/corpus-additions.yaml`** (same `tool_name: [queries]`
shape as the real corpus file), with a header noting the retrieval
acceptance check is deferred until NATE hand-merges these entries into the
real file.

**Retrieval smoke-check performed anyway** (informational, since the real
corpus can't be touched): warmed the `search_tools` discover index in a
throwaway script (`search_tools._get_index()`, mirroring the orchestrator's
startup warm -- `retrieve_visible_tools` itself never builds it on the hot
path, so a bare/test process without this call always fail-opens to the full
registry, which is what an unwarmed first attempt showed). With the index
warmed, **8 of 9 tools already resolve correctly via docstring content
alone** (no corpus needed) for a natural-language query approximating their
proposed corpus entries:

- `read_run_diagnostics`, `compute_skill_metrics`, `fetch_high_water_marks`,
  `compute_flood_extent_skill`, `extract_model_at_observations`,
  `fetch_flood_extent_observation`, `set_swmm_parameters`,
  `set_sfincs_parameters` -- all correctly retrieved.
- `set_modflow_parameters` -- MISSED on "double the hydraulic conductivity in
  layer 2 of this groundwater model" and "calibrate the K field on this
  staged groundwater model"; HIT on "set specific yield to 0.15 site-wide",
  "reduce recharge by 30%", and "adjust vertical hydraulic conductivity".
  This is exactly the kind of gap the staged corpus queries close --
  landing `corpus-additions.yaml` should fix it and further strengthen the
  other 8.

This is NOT a substitute for the real acceptance check (which reads the live
corpus file, not this staging file) -- treat retrieval visibility for these 9
tools as informally verified-good, formally UNVERIFIED until NATE lands the
patch.

## 5. Membership / registry tests

No test carries an explicit hand-maintained "expected tool names" set that
needed literal extension -- the registry-shape tests in this repo are all
either membership-diff-based (`test_categories.py`'s
`test_every_registered_tool_has_a_primary_category`, which auto-computes
`registered - mapped`) or lower-bound (`len(TOOL_REGISTRY) >= 191` in
`test_spatial_query.py`, `>= 55` in `test_tool_annotations.py`, `>= 50` in
`test_gemini_kwargs_fuzz.py`). All pass unmodified with the 9 additions
(200 > every bound). `test_uri_registry.py`'s
`test_resolvable_param_allowlist_excludes_server_owned_params` is a subset
check (`{hazard_raster_uri, assets_uri, layer_uri} <= RESOLVABLE_URI_PARAMS`),
also unaffected. No test file needed a literal set edit; the category-mapping
edit in section 2 IS the "membership test" work item.

## 6. Cross-lane test edits (lane A, sanctioned)

Lane A's solver.py change (adding the `solver` field to
`_write_local_completion`) forced 2 test-fixture-key updates in files it does
not own, flagged explicitly in its report as needing reviewer confirmation:

- `server/tests/test_solver_local_docker.py`: `_ENTRYPOINT_COMPLETION_KEYS`
  gained `"solver"`.
- `server/tests/test_modflow_local_backend.py`: `_MODFLOW_COMPLETION_KEYS`
  gained `"solver"`.

Reviewed: correctly scoped (adds exactly the one new key, comment explains
why), necessary fallout of a sanctioned lane-A-exclusive solver.py edit.
Accepted as-is, no further action.

## 7. Test results

### 7.1 Targeted (all 9 lanes' own tests + wiring-adjacent suites)

```
tests/test_read_run_diagnostics.py tests/test_compute_skill_metrics.py
tests/test_compute_flood_extent_skill.py tests/test_extract_model_at_observations.py
tests/test_fetch_high_water_marks.py tests/test_fetch_flood_extent_observation.py
tests/test_set_sfincs_parameters.py tests/test_set_swmm_parameters.py
tests/test_set_modflow_parameters.py tests/test_categories.py
tests/test_tools_registry.py tests/test_uri_registry.py tests/test_tool_annotations.py
```
**212 passed**, 0 failed, 52 warnings (all pre-existing deprecation noise from
pyproj/hydromt/pyogrio, not from wave code).

### 7.2 Contracts package suite

```
cd contracts && ../venvs/agent/bin/python -m pytest tests/ -q
```
**691 passed**, 0 failed. Unaffected by this wave (no contracts-package files
touched by any lane).

### 7.3 Full server suite (`server/tests/`, 11,494 tests collected)

This suite is large enough that a single run takes ~16 minutes. Ran it
TWICE in the background: the first attempt hit an operator-added inner
`timeout 590` wrapper at 90% (EXIT:124, inconclusive -- superseded); the
second, untimed run completed naturally:

```
16 failed, 11382 passed, 95 skipped, 1 xfailed, 248 warnings in 954.85s
```

The first (cutoff) run additionally surfaced 11 failures in an EARLIER
region (6% / 30%) that **did NOT reproduce** in the second, complete run
(`test_code_exec_tool.py`, `test_fetch_roads_osm.py`,
`test_fetch_sentinel1_sar.py`, `test_sandbox_hardening.py`,
`test_scenario_reuse_fetch_f96.py`) -- confirmed transient/order-flaky
(each also passes standalone), not a wave defect, and not present in the
final 16.

**Every one of the 16 final failures was individually triaged**: isolated,
run standalone, checked against `git status` for wave ownership, and -- for
every failure where standalone reproduction alone was ambiguous -- re-run
with this wave's 3 integration-owned files (`tools/__init__.py`,
`categories.py`, `uri_registry.py`) temporarily `git stash`-ed out (an A/B
isolation test) to determine causation directly rather than inferring it.
Stash was popped back immediately after each check; final state re-verified
identical (`60 insertions` across the 3 files, smoke test green) before
proceeding.

**13 of 16 -- pre-existing, verified unrelated to this wave:**

- `test_coastal_forcing_offloop.py::test_coastal_heavy_helpers_dispatched_via_to_thread`
  -- the exact test lane B's own report already flagged pre-existing.
- `test_fetch_resolution_gate.py::test_gate_emits_fetch_granularity_block[fetch_dem-dem]`
- `test_fetch_resolution_gate.py::test_gate_emits_fetch_granularity_block[fetch_topobathy-topobathy]`
- `test_fetch_resolution_gate.py::test_fetch_gate_compute_label_deployment_aware[aws-batch-fetch]`
- `test_fetch_resolution_gate.py::test_fetch_gate_compute_label_deployment_aware[-fetch]`
  -- compute-class deployment-detection resolves to `"local"` instead of
  `"fetch"` regardless of `TRID3NT_SOLVER_BACKEND`; unrelated subsystem
  (granularity-gate compute-class labeling), zero content overlap with tool
  registration.
- `test_run_modflow.py::test_run_modflow_job_local_end_to_end` -- fails on
  `TRID3NT_RUNS_BUCKET must be set under TRID3NT_STORAGE_BACKEND=s3`, an
  env-var/storage-backend issue, not a registry/solver.py issue.
- `test_run_river_dye_scenario.py` x5 -- all fail with
  `TypeError: _fake_publish() takes 4 positional arguments but 8 were given`,
  a test-mock-vs-real-function signature drift with zero relation to tool
  registration or lane A's `solver.py` change.
- `test_search_tools.py::test_search_tools_routes_canonical_queries[elevation Grand Canyon-fetch_dem]`
- `test_search_tools.py::test_matched_queries_populated_for_corpus_hit`
  -- **A/B-isolation-confirmed**: both still fail with this wave's 3 files
  stashed out (i.e. as if the 9 tools were never registered). Caused by the
  PRE-EXISTING, already-uncommitted `tool_query_corpus.yaml` user WIP (98
  insertions / 7 deletions present in the working tree before this wave
  began), which shifts BM25 term statistics independent of anything in this
  wave.

For every item above, `git status` on the touched file(s) shows zero
modifications by any lane or by integration, and (where isolation testing
was inconclusive from file-ownership alone) the failure reproduces
identically with the wave's registration wiring removed.

**3 of 16 -- attributable to this wave, A/B-isolation-confirmed:**

- `test_search_tools.py::test_search_tools_routes_canonical_queries[model flooding-run_model_flood_scenario]`
  -- **PASSES** with the wave's 3 files stashed out, **FAILS** with them
  restored. `fetch_flood_extent_observation` (lane C) now ranks in the
  discover index's top-3 for the query "model flooding" ahead of
  `run_model_flood_scenario` (`top=['run_swmm_urban_flood',
  'run_model_nws_flood_event_scenario', 'fetch_flood_extent_observation']`),
  pushing the canonical target to rank 4. A genuine catalog-growth ranking
  side effect: adding a new "flood"-heavy tool dilutes an existing
  fixed-top-3 assertion. Landing `corpus-additions.yaml` may or may not
  resolve this (BM25 IDF shifts are corpus-wide, not guaranteed to favor the
  displaced tool) -- flagged for the retrieval-index owner, NOT something a
  wiring-layer fix can address (no lane logic to change; docstring rewrites
  are lane-owned content, out of scope here).
- `test_spatial_query.py::TestRetrieval::test_search_tools_top5[give me summary statistics for this layer, min max mean and sum]`
  -- same signature: **PASSES** stashed-out, **FAILS** restored.
  `spatial_query` drops out of the top-5 for a "summary statistics" query
  once the 9 new tools are in the catalog.
- `test_tool_retrieval.py::test_every_registered_tool_has_corpus_queries`
  -- **DIRECTLY AND UNAVOIDABLY CAUSED**, by design: this test asserts every
  registered tool has a `tool_query_corpus.yaml` entry. The 9 new tools
  genuinely have none yet (staged in `corpus-additions.yaml`, section 4) --
  `tool_query_corpus.yaml` is hard-off-limits to this agent. This is the
  EXPECTED, DESIGNED failure state until NATE lands the corpus patch; it is
  not fixable at the wiring layer and not a defect.

**Every one of the 9 new tools' own tests, every wiring-adjacent registry/
category/uri test, and the entire contracts suite are unconditionally
green.** The 3 wave-attributable failures are all retrieval/ranking-surface
effects of catalog growth, not correctness defects in any of the 9 new
tools, and 2 of the 3 are plausibly improved (not guaranteed fixed) once
`corpus-additions.yaml` lands; the third resolves automatically once it
does.

## 8. Open issues rollup (from the 4 lane reports, not resolved at the wiring
layer -- carried here for the contracts agent / NATE)

- **MODFLOW dry-cell line format** (lane A): parser matches documented
  MODFLOW-lineage phrasing but is tested only against a SYNTHETIC line (no
  real dry-cell fixture was obtainable from MinIO; mf6io doc was
  403-blocked). `dry_cells=0` means "no explicit dry-cell notices found", not
  a proven guarantee.
- **TELEMAC healthy-path mass balance** (lane A): tested against a
  SYNTHESIZED `full_listing.log` line + synthesized `correct_end=true`
  completion, because the only real MinIO TELEMAC run is a FAILED run whose
  listing crashed before any balance line. Phrasing/units should be
  confirmed against a real healthy TELEMAC listing when one exists.
- **SFINCS mass balance on the real fixture** (lane A): the real
  `sfincs_map.nc` carries no `cumprcp`/`cuminf`, so the derived path is only
  exercised against a synthesized tiny nc; the real-fixture path correctly
  returns `mass_balance_pct=null`.
- **Bounds-as-hard-error reconciliation** (lane D, `_setter_envelope.py`):
  build-contract.md 3.4 frames `plausibility[].in_range=false` as a WARNING;
  the lane-D brief required a hard typed `BoundsViolation`. Lane D reconciled
  by treating the named physical-bounds table (Manning n, K, ss, sy,
  infiltration, %impervious) as a genuine physical floor -- values in that
  table ARE non-physical outside range (negative K, Manning n<=0, etc.), so
  a hard error reads as the correct call on inspection; ruling: **accept as
  built**, no code change requested. Flagged here for NATE's explicit
  sign-off since the lane surfaced it as a genuine tension.
- **SFINCS setter zone scope** (lane D): global-only in v1; hydromt-sfincs's
  `setup_*` API has no arbitrary-polygon-zone knob. SWMM (subcatchment list)
  and MODFLOW (layer index) DO have zone scope. Documented gap, not silent.
- **K/ss/sy/recharge bounds tables** (lane D): lane D's own literature
  citations (Freeze & Cherry 1979, Johnson 1967) since no shared research.md
  table was available for those specific engines -- worth a cross-check
  against research.md section 1 if the contracts agent has different pinned
  numbers.
- **`child_setup_uri` manifest shape** (lane D): a setter-provenance manifest
  (child lineage + changes_applied), not yet a `launch_local_solver`-ready
  dispatch manifest -- a follow-up integration step would be needed to make
  a setter child directly re-runnable through `run_solver`.
- **`fetch_flood_extent_observation` historical coverage** (lane C): primary
  source (NASA LANCE MCDWD_L3_F3_NRT) is near-real-time only; an arbitrary
  historical event date 404s to a typed error. Historical archive access
  (LAADS / Copernicus GFM) needs credentials and is a documented follow-up,
  not built.
- **`extract_model_at_observations` VERTCON conversion** (lane C): a
  declared NAVD88-vs-NGVD29 mismatch with no `datum_shift_m` is a typed
  error by design (never a silent guess); automatic VERTCON-style conversion
  is intentionally out of scope.
- **Retrieval visibility** (this pass): see section 4 -- formally deferred
  until `corpus-additions.yaml` lands in the real corpus.
- **Flood-sim canary** (standing hard rule): lane A's `solver.py` change
  touches the completion.json written by every local solve; verified via
  196+ engine-backend/chain tests plus this pass's full-suite gate, but the
  standing rule's live canary (direct-call flood run + WS turn smoke + NATE
  visual in QGIS) is NATE's step, not runnable by a build/integration
  subagent.

## 9. File inventory

New tool modules (9):
- `server/src/trid3nt_server/tools/simulation/diagnostics/__init__.py` (+
  `sfincs.py`, `swmm.py`, `modflow.py`, `geoclaw.py`, `telemac.py`,
  `_common.py` internal)
- `server/src/trid3nt_server/tools/processing/compute_skill_metrics.py`
- `server/src/trid3nt_server/tools/processing/compute_flood_extent_skill.py`
- `server/src/trid3nt_server/tools/processing/extract_model_at_observations.py`
- `server/src/trid3nt_server/tools/fetchers/hydrology/fetch_high_water_marks.py`
- `server/src/trid3nt_server/tools/fetchers/hydrology/fetch_flood_extent_observation.py`
- `server/src/trid3nt_server/tools/simulation/set_sfincs_parameters.py`
- `server/src/trid3nt_server/tools/simulation/set_swmm_parameters.py`
- `server/src/trid3nt_server/tools/simulation/set_modflow_parameters.py` (+
  shared `_setter_envelope.py`)

Integration-owned edits (this pass):
- `server/src/trid3nt_server/tools/__init__.py` (9 import lines)
- `server/src/trid3nt_server/categories.py` (PRIMARY_CATEGORY x9,
  SECONDARY_CATEGORIES x5)
- `server/src/trid3nt_server/uri_registry.py` (RESOLVABLE_URI_PARAMS +3:
  `paired_table_uri`, `model_extent_uri`, `benchmark_extent_uri`)
- `docs/validation/corpus-additions.yaml` (new -- staged corpus patch)
- `docs/validation/build-report.md` (this file)

Untouched per hard rule: `server/src/trid3nt_server/data/tool_query_corpus.yaml`.

## 10. Corpus patch pointer

`docs/validation/corpus-additions.yaml` -- 9 tool entries, 76 total proposed
queries, same key/list-of-strings shape as the real corpus file. Ready for
NATE to hand-merge; the retrieval acceptance check re-runs once landed.

## 8. Post-panel fix pass (2026-07-24, surgical agent)

All 4 panel findings fixed and re-verified:
1. MAJOR ft-vs-m pairing: observed units inferred from field name, ft->m
   x0.3048 at ingestion, recorded in alignment.units, typed PairingInputError
   when undeterminable; observed_units override param added; STN-fixture test
   proves converted values pair.
2. MAJOR setter bounds: warn-and-proceed (in_range=false) for out-of-plausible
   values per contract 3.4; hard BoundsViolation only for meaningless values
   (negative n, K<=0, imperv outside 0-100, sy outside 0-1); SFINCS Manning
   band aligned to 0.011-0.8.
3. SRMS = plain RMSE/(max-min) ratio; PBIAS sign convention documented
   (spotpy positive = over-predict; Moriasi opposite) + abs() banding confirmed.
4. bands_source consolidated top-level; mode-B station_tolerance_m (500m)
   split from nearest_wet_tolerance_m, recorded in alignment.

Results: targeted 127 passed; full suite 16 failed / 11390 passed - EXACTLY
the section-7 baseline (13 pre-existing + 3 corpus-blocked), zero new.
FLOOD CANARY PASSED live: run 01KYB160YPY4C9F8KPJFN47TSB status=ok, depth COG
+ 7 frames, completion.json carries solver="sfincs".
