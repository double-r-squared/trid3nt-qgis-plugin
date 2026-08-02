# Hygiene Sweep Plan (branch refactor/engine-doors)

Scope lane. This is a PLAN, not an execution. Conservative doctrine: extraction +
verified-dead removal only, no rewrites, no compat shims. Scope = `server/src`
(tests excluded except where a touched hunk sits in a test file). Executor uses
`git mv` for moves; orchestrator commits.

Package root: `server/src/trid3nt_server`. `server.py` lives at the package root
(NOT under `agent/`), 15439 lines.

---

## 1. server.py excavation map

### 1a. Structural regions (line ranges -> role -> disposition)

| Lines | Region | Role | Disposition |
|------|--------|------|-------------|
| 1-270 | imports, module constants, env-flag readers | platform config | STAYS |
| 273-560 | tool-retrieval / routing / stage-label helpers | dispatch support | STAYS |
| 558-1590 | pending-* registries (tool-choice, catalog-offer, confirmation, credential) + typed cancel/timeout errors | gate state machine | STAYS (registries), see 1b (builders keyed off these) |
| 1622-1888 | bbox coercion, AOI zoom, pending region/spatial registries | spatial gate state | STAYS |
| 1888-2350 | persistence singleton + init + migrations + session-active-case | platform lifecycle | STAYS |
| 2349-2760 | `_LiveTurn`, live-turn registry, `SessionState` | transport core | STAYS |
| 2760-3388 | envelope send helpers, heartbeat, turn-complete/abort, cache-status, tool-candidates | transport | STAYS |
| 3388-5543 | `_stream_gemini_reply` (turn loop, ~2150 lines) | turn loop | STAYS |
| 5543-7260 | session resume, auth handshake, case list/open/command, autoname | case lifecycle | STAYS |
| 7261-8150 | AOI pinning, reuse, chat/tool-card persistence | persistence | STAYS |
| **8152-11224** | **card / confirm-envelope builders + gate orchestration** | **gate surface** | **see 1b** |
| 11225-12684 | spatial-input handler, redact, emitter-step id, sync-offload guard, `_invoke_tool_via_emitter` | dispatch | STAYS |
| 12684-13560 | auto-publish, case-layer persistence, impact/chart/mode2 emit | persistence/emit | STAYS |
| 13556-14400 | invoke-directive parse, dispatch-and-persist, secrets/lesson/layer handlers | dispatch | STAYS |
| 14303-15248 | session-connection registry, reap, `_make_handler`, `run_server` | transport | STAYS |
| 15390-15439 | `__all__` | exports | EDIT (drop extracted names + strip provenance, see 1b/2) |

### 1b. Card / confirm-envelope builders -> `agent/gates/cards/`

The 8152-11224 band splits cleanly into PURE builders (no websocket / no state /
no await on the wire -> extract) and TRANSPORT-COUPLED orchestration (emit + wait
for a WS response -> STAYS). The pure builders are keyed off `params: dict` and
return a `trid3nt_contracts` envelope/payload dataclass. The pending-* registries
they gate against stay in server.py; builders never touch them (clean cut).

EXTRACT (pure, no I/O) -> new package `agent/gates/cards/`:

| Def | Lines | Family | Notes |
|-----|-------|--------|-------|
| `_get_warning_threshold_mb` | 8152 | payload_warning | env reader |
| `_get_hard_cap_mb` | 8168 | payload_warning | env reader |
| `_resolve_payload_estimator` | 8184 | payload_warning | registry lookup, pure |
| `_clamp_fetch_resolution` | 8880 | solver_confirm | pure clamp |
| `_build_fetch_resolution_envelope` | 8891-9043 | solver_confirm | async, PURE arithmetic (no DEM read) |
| `_build_flood_run_settings_envelope` | 9044-9273 | solver_confirm | async |
| `_clamp_swmm_resolution_to_cap` | 9244 | solver_confirm | pure clamp |
| `_build_psha_confirm_envelope` | 9274-9369 | solver_confirm | pure sync |
| `_build_fire_confirm_envelope` | 9370-9469 | solver_confirm | pure sync |
| `_build_geoclaw_confirm_envelope` | 9470-9555 | solver_confirm | pure sync |
| `_gate_memory_key` | 9556-9585 | solver_confirm | pure key derivation |
| `_build_credential_request_payload` | 10543-10615 | credential | pure |
| `_region_admin_level_for` | 10616-10629 | region_choice | pure |
| `_build_region_candidates` | 10630-10728 | region_choice | pure (may do sync TIGER read; verify before move) |
| `_build_region_choice_request_payload` | 10729-10808 | region_choice | pure |
| `_build_spatial_input_request_payload` | 10978-11040 | spatial_input | pure |
| `_spatial_response_to_result` | 11107-11224 | spatial_input | pure conversion |

Heavier async builders that read a DEM / fetch (still extractable, phase 2):
`_build_telemac_mesh_envelope` (8500-8636), `_build_swmm_granularity_envelope`
(8637-8890). Extract with the solver_confirm family but flag the extra deps
(rasterio / fetchers).

STAYS in server.py (transport-coupled: emit a frame + await a WS response, or
handle a live turn):
`_maybe_gate_on_payload_warning` (8213), `_gate_on_code_exec` (8374),
`_gate_on_solver_confirm` (9586), `_gate_with_turn_memory` (10141),
`_maybe_handle_credential_error` (10361), `_emit_credential_request_and_wait`
(10464), `_emit_region_choice_and_wait` (10809), `_maybe_handle_region_choice`
(10862), `_emit_spatial_input_and_wait` (11041), `_handle_request_spatial_input`
(11225), `_local_compute_lane` (8768 -- keep here, see deps).

Recommended layout (modularity is the goal): package `agent/gates/cards/` with
`solver_confirm.py`, `credential.py`, `region_choice.py`, `spatial_input.py`,
`payload_warning.py`, `__init__.py` re-exporting the public builder names.
Conservative fallback: a single `agent/gates/cards.py`. `agent/gates/` already
exists (circuit_breaker, context_budget, spatial_input, tool_gating, etc.).

Shared-helper dependencies the extracted builders reference:
- `new_ulid` -- from `trid3nt_contracts` (re-import in cards, trivial).
- `coerce_bbox_value` -- from `.agent.tool_arg_normalizer` (re-import).
- `_local_compute_lane` (server.py:8768) -- called by psha/fire/geoclaw. Either
  move it into cards (it is a pure env reader, gate-scoped) or import it from
  server. Moving it into `cards/` is cleaner; if left in server.py, cards imports
  it (server.py already imports the gate helpers, so no cycle if cards does not
  import server at module load -- keep `_local_compute_lane` in cards to avoid a
  cards->server import edge).

Test import surface (NO compat shim -> update the test imports as touched hunks):
tests import `_build_*` directly from `trid3nt_server.server`:
- `test_solver_confirm_gate.py`, `test_fetch_resolution_gate.py`,
  `test_payload_warning_flow.py`, `test_granularity_gate.py`,
  `test_credential_pipeline.py`, `test_region_choice_picker.py`,
  `test_combined_run_settings_gate.py`, `test_code_exec_tool.py`,
  `test_model_fire_spread_chain.py` (imports `_build_fire_confirm_envelope`).
Update these imports to `trid3nt_server.agent.gates.cards...`. `__all__`
(15390-15439) currently exports `_maybe_gate_on_payload_warning` and
`_build_credential_request_payload`; drop the extracted-name exports and let the
gate functions (which STAY) keep theirs.

### 1c. DEAD web-era regions -- honest finding

There is NO large dead web-era block in server.py to excavate. The web/cloud
vocabulary that appears is descriptive of LIVE or explicitly-dormant seams, not
removable dead code -- per-term evidence:

- `titiler` (11 hits): all in comments describing the LIVE publish_layer style
  path (viridis fallback preset) and the QGIS-native swap (e.g. 12575 "TiTiler
  exit (QGIS-native swap): publish_layer now returns..."). publish_layer still
  carries titiler style params. NOT dead; provenance to compress only.
- `dynamodb` (7 hits): all comments; DynamoDB is still a supported persistence
  backend alongside the file backend (1959 "DynamoDB or the file backend"). NOT
  dead.
- `vertex` (9 hits): the Vertex/GCP path is the explicitly-retained dormant
  reversible seam (CLAUDE.md). Comments like 2683 "GCP is decommissioned: the
  Vertex-only..." carry provenance to compress; the seam itself is NOT removed
  this wave.
- `aws_batch` (7), `gcp/GCP` (9), `cloud run` (1): comment references inside
  live dispatch-phrase branches (e.g. `_build_psha_confirm_envelope` picks
  local-vs-Batch wording via `_local_compute_lane()`); NOT dead code.

ONE concrete in-file dead find: DUPLICATE `_ensure_emitter` definition.
server.py:10210 defines `_ensure_emitter` with a docstring and NO body (a no-op
returning None); server.py:10217 immediately redefines the same name with the
real body. Python binds the name to the second def; the first (10210-10216) is
unreachable dead code. Remove the first stub def. (Confirmed: two consecutive
`def _ensure_emitter` at 10210 and 10217, identical signature.)

### 1d. STAYS (transport / dispatch / turn loop)

Everything in 1a marked STAYS. The turn loop (`_stream_gemini_reply`,
3388-5543), the WS handler (`_make_handler`, 14423), `run_server` (15248), the
SessionState/live-turn machinery, and all persistence/emit handlers are
out-of-scope for extraction this wave.

---

## 2. Archaeology rules + estimate

### 2a. Provenance pattern counts (server/src, *.py)

| Pattern | Raw hits |
|---------|---------|
| `job-[0-9]` | 1063 |
| `sprint-[0-9]` | 194 |
| `[Ww]ave-[0-9]` | 99 |
| `OQ-[0-9]` | 161 |
| `\bF1[0-9]\b` (F1x fix refs) | 15 |
| `BK-[0-9]` | 29 |
| `RISK-[0-9]` | 4 |
| `CLB-` | 0 |
| `\bland(ed\|s in)\b` | 76 |
| commit-sha-shaped narration | ~10 |
| `NATE 20xx` dated notes | 147 |
| `task-[0-9]` | 77 |
| `#1[0-9][0-9]` issue nums | 83 |
| `Lane [A-Z]` | 14 |
| `FIRE-[0-9]` | 42 |
| `VAULT-` | 11 |

Unique provenance-bearing lines (core marker set) in server/src: 1503 across 220
files. In server.py alone: 294. Adding the extended vocab (NATE-dated, task-,
#NNN, Lane, FIRE-, VAULT-) pushes the unique touched-line count to roughly
1600-1800 across ~220 files. Top concentrations: server.py (294),
sfincs_builder.py (69), main.py (63), sfincs/flood/flood.py (57),
simulation/solver/solver.py (49), fetch_landcover.py (37),
emission/pipeline_emitter.py (36), adapters/adapter.py (35), persistence.py (33).

### 2b. Calibration: 30-hit sample classified (strip / keep / rewrite)

Three-way outcome per the doctrine. Dominant outcome is REWRITE = strip the
marker token in place, keep the surrounding constraint / code-descriptive text
(compressed, ASCII hyphens). Pure DELETE-line is for marker-only narrative.
KEEP-verbatim is rare (only lines with no marker, or an `ADR NNNN` pointer).

STRIP-MARKER-KEEP-LINE (rewrite) -- the ~80% case:
- `main.py:74  # job-0033: register the 4 data-fetch atomic tools (FROZEN __init__.py).`
  -> `# register the 4 data-fetch atomic tools.` (drop `job-0033:` + `(FROZEN ...)`)
- `server.py:295  # job-0233: the code_exec_request confirm gate validity window (seconds).`
  -> `# code_exec_request confirm gate validity window (seconds).`
- `solver.py:1573 # job-0164: absorb LLM-invented kwargs (centralized at server.py via`
  -> keep `absorb LLM-invented kwargs` (this is a CONSTRAINT: why `**args` exists), drop `job-0164:`.
- `layer_uri_emit.py:41 ... the LLM-visible tool result stays truthful so the job-0177`
  -> keep the honesty-floor constraint sentence, drop `job-0177`.
- `sfincs_builder.py:1079 (load-bearing for load_manning_mapping + the OQ-4 section-4 validation gate)`
  -> keep `load-bearing for load_manning_mapping + the validation gate`, drop `OQ-4 section-4`.
- `fetch_landfire_fuels.py:350 # Empty-raster gate (codified lesson job-0086, geographic-correctness):`
  -> `# Empty-raster gate (geographic-correctness):` (CONSTRAINT kept, lesson-id dropped).
- `compute_colored_relief.py:70 # gdaldem binary resolution (job-0269 - mirrors compute_slope/_aspect)`
  -> `# gdaldem binary resolution.` (also fix the em-dash to ASCII if any clause kept).
- `server.py:1872 # job-0115: app-level Persistence singleton (Wave 1.5).`
  -> `# app-level Persistence singleton.`
- `run_modflow_multi_species_tool.py:1 """...MODFLOW Wave-3 N-species engine.`
  -> `"""...MODFLOW N-species engine.` (drop `Wave-3`).
- `server.py:549 # ADR 0018 -- pending tool-choice registry (mirrors the job-0243 session-...)`
  -> keep `# ADR 0018 -- pending tool-choice registry.` (ADR pointer STAYS; drop the `job-0243` mirror clause).

DELETE-LINE / DELETE-CLAUSE (marker-only fix-history / roadmap) -- the ~15% case:
- `main.py:174 job-0034 DI seam: completes the wire-up promised by job-0032's ...`
  -> the narrative ("completes the wire-up promised by job-0032") is fix-history; delete, keep at most `DI seam` if the code below needs the label.
- `solver.py:1819 Surfaced as OQ-41-ERROR-CODE-REGISTRY - when sprint-08 lands more ...`
  -> pure roadmap narrative; delete.
- `solver.py:1468 semantics as the Cloud Workflows poll (job-0291).`
  -> dead-cloud reference + marker; delete the clause.
- `sfincs_builder.py:2161 pandas >= 3.0 (the old job-0055 blocker that forced river inflow OFF).`
  -> delete the "old blocker" war-story clause; keep `pandas >= 3.0` only if it is a live version constraint (verify).
- `compute_movement_trajectory.py:615 ...of-thousands of fixes). The signature accepts **args per the Wave-1.5`
  -> delete the "tens-of-thousands of fixes" war-story; keep the `**args` constraint sentence.

KEEP-VERBATIM (rare): lines already marker-free that state a constraint; `ADR
NNNN` cross-references (the ADR-lite pointer pattern NATE wants). Do not touch.

NATE-dated notes (`NATE 20xx-xx-xx`): the date-stamp + attribution is provenance
and dies; the CONSTRAINT or DESIGN sentence it introduces is kept-compressed, or
(if it is a multi-line LIVE architectural-choice narrative) offloaded to ONE
ADR-lite note per subsystem in `docs/decisions/` (context/decision/consequence,
supersede-never-rewrite numbering; next id 0027+). Do NOT mint one note per
comment -- batch related blocks per subsystem (e.g. one note for the confirm-gate
card family, one for session durability). Canonical calibration: a 35-line "JOB B
session durability" block collapses to ~4 constraint lines + optionally one
ADR-lite note.

Dead-cloud tokens embedded in provenance lines (`gs://`, `/vsigs/`, "Cloud
Workflows", "Cloud Run") are a SEPARATE judgment: `/vsigs/` may be a live GDAL
path; `gs://`/"Cloud Workflows" are likely dead. Where the whole clause is dead
cloud, delete it; where uncertain (a judgment call that cannot be defended),
leave the line and list it -- never guess-delete.

### 2c. Estimate

Archaeology touch: ~1600-1800 provenance-bearing lines across ~220 files in
server/src. Expected split by the calibration above: ~80% rewrite-in-place
(strip token, keep compressed constraint), ~15% delete-line/clause, ~5%
keep/ADR-offload. Estimated 6-12 ADR-lite notes total (batched per subsystem:
confirm-gate cards, session durability, credential vault, sfincs builder, solver
supervisor, persistence). server.py alone is ~294 lines of provenance touch.

---

## 3. Dead-code candidates beyond server.py

Method: AST-extract every module-level `def`/`class` in the platform root +
`agent/` non-tool modules (tools/ and workflows/ subtrees excluded -- engine-owned,
huge), then word-boundary reference-count each name across `server/src`,
cross-checked against `server/tests`, `scripts`, `experiments`, `qgis-plugin`,
`bin`, and string/getattr usage. Scout-then-act: uncertain = NOT a candidate.
(pyflakes is available but only reports unused imports/locals, not dead
module-level symbols; grep import-chain was used instead.)

Scan flagged 6 zero-reference-in-src symbols; verification reclassified 2 as
LIVE (test-driven), 4 remain candidates:

CANDIDATES (per-item evidence):
- `agent/lessons.py:536 _estimate_tokens` -- zero references in server/src OR
  server/tests. Companion `_CHARS_PER_TOKEN` IS used (lessons.py:586, tests), but
  its arithmetic was inlined at 586; `_estimate_tokens` itself is orphaned. STRONG
  dead candidate.
- `tool_catalog_http.py:156 _reset_caches_for_tests` -- docstring "ONLY for
  tests" but zero references in server/tests or anywhere. Orphaned test-support
  helper (its test appears deleted). Candidate.
- `sandbox/sandbox_runner.py:112 _is_local_mode` -- returns constant `True`, zero
  callers/tests anywhere. Its own docstring claims "callers/tests that reference
  it still resolve" -- that claim is STALE (grep finds none). GCP-decommission
  vestige. Candidate; flag the stale self-justifying docstring.
- `credentials/secrets_handler.py:146 _default_ssm_client` -- zero constructors
  found. Its docstring claims `Persistence.get_secret_value` still constructs it
  for `aws-ssm://` refs, but persistence.py only MENTIONS `aws-ssm://` in a
  comment (1925), no construction. Self-described "legacy-compat seam" (a compat
  shim, which doctrine bans) already orphaned. Candidate, but removal is COUPLED
  to persistence.py's `aws-ssm://` branch (docstring says "Remove together with")
  -- verify that branch is also dead before removing; if the branch still routes,
  leave and list.

CONFIRMED LIVE (keep -- test-driven, not dead):
- `agent/gates/context_budget.py:447 _reset_num_ctx_cache_for_tests` -- imported +
  called in test_context_budget.py.
- `sandbox/sandbox_hardening.py:397 jail_available` -- called in
  test_sandbox_hardening.py.

Plus the in-file dead find from 1c: `server.py:10210 _ensure_emitter` (bodyless
stub shadowed by the 10217 redefinition).

Note: the scan excluded tools/ and workflows/ subtrees by design (engine-owned,
large, out of this lane's conservative cut). A follow-up wave can extend the same
reference-count method there.

---

## 4. Renames spec

Two LLM-registered tool identities rename; HTTP routes UNCHANGED (verified).

### 4a. `export_case_to_qgis` -> `open_case_in_qgis`

Registered identity (MUST rename -- LLM-facing name, retrieval, category, docs):
- `agent/tools/meta/export_case_to_qgis/export_case_to_qgis.py:160` -- `name="export_case_to_qgis"` in the tool decorator/metadata.
- `agent/tools/meta/export_case_to_qgis/corpus.yaml:1` -- top-level retrieval key `export_case_to_qgis:`.
- `agent/categories.py:324` -- `"export_case_to_qgis": "geographic_primitives"` map key (+ comment 327).
- module docstring / user-facing docstring mentions in export_case_to_qgis.py (1, 660 "TRID3NT export_case_to_qgis", log tags).
- tests referencing the registered string: `test_export_case_to_qgis.py:470 TOOL_REGISTRY.get("export_case_to_qgis")`, `test_tool_candidates_waves.py:150`, retrieval-corpus tests.

Python symbol + module dir (RECOMMEND for zero-legacy naming consistency, but
LARGER blast radius -- flag as a decision): renaming the function `export_case_to_qgis`
and the dir `agent/tools/meta/export_case_to_qgis/` (via `git mv`) forces updating
~15 internal seam import sites that reach into the module
(`export_case_to_qgis._unwrap_tile_template` / `._mesh_entry_for_layer` /
`._resolve_mesh_crs` / `._strip_query` / `._layers_from_case` / `._s3_client`):
publish_layer.py (35, 2049, 2055), query_point_hazard.py (10, 168, 198, 230,
233), compose_case_report.py (4, 147, 209, 258), probe_point.py (18),
modflow_mesh.py (3, 42, 44, 69, 76), postprocess_telemac.py (17, 375),
model_river_dye_release_scenario.py (22, 28, 751, 1254), river_dye.py (13),
tool_catalog_http.py (1511, 1519, 1603, 2223, 2233), __init__.py (469), plus test
imports (test_export_case_to_qgis.py, test_export_case_to_qgis_mesh.py,
test_export_qgis_http_route.py, test_query_point_hazard.py,
test_publish_layer_titiler_base_sprint14aws.py) and qgis-plugin COMMENT-only refs
(case_export.py:54, render/layers.py:18/468, tests headless_*). Recommendation:
do the full symbol+dir rename for zero-legacy consistency; if scoped down, at
minimum rename the registered identity + corpus + category + tests and leave a
plan note that the symbol/dir still reads `export_case_to_qgis`.

### 4b. `import_user_layer` -> `register_case_layer`

Registered identity (MUST rename):
- `agent/tools/meta/import_user_layer/import_user_layer.py:723` -- `name="import_user_layer"` in `_IMPORT_USER_LAYER_METADATA`.
- `agent/tools/meta/import_user_layer/corpus.yaml:1` -- retrieval key `import_user_layer:`.
- `agent/categories.py:329` -- `"import_user_layer": "geographic_primitives"`.
- module + wrapper docstrings (1, 15, 638) and log tags.
- tests: `test_import_user_layer.py` (imports `import_user_layer as iul`, calls `iul.import_user_layer(...)`), `test_ingest_layer_http_route.py`.

Do NOT rename the CORE functions `ingest_user_layer` / `upload_layer_file` --
those are the HTTP-route entry points (called by tool_catalog_http.py:1770,
1777), separate from the LLM tool `import_user_layer`. Same symbol+dir-rename
decision as 4a applies (recommend full rename for consistency; flag the internal
import-path surface via __init__.py:470 and the route module).

### 4c. HTTP routes UNCHANGED (verified)

The plugin calls ROUTE strings, never tool NAMES. Verified: qgis-plugin POSTs
`/api/export-qgis` (+ `/api/export-qgis/file`), `/api/ingest-layer`,
`/api/ingest-layer-file` (headless_mesh_proof.py, test_push_layer.py,
test_milestone3.py). The only plugin reference to a tool NAME is a comment
(case_export.py:54). Therefore the route strings in tool_catalog_http.py stay
byte-identical; only the imported function symbols/registered names change. State
this in the PR: "routes unchanged; plugin decoupled from tool names."

---

## 5. Riders

### 5a. Stale `clip_vector_to_polygon` refs (7 refs, 6 files)

`clip_vector_to_polygon` was RETIRED (cull pass 2, per
test_s3_port_job0293b.py:298 "RETIRED by cull pass 2"). The tool no longer
exists; these are stale comment/docstring references to a dead tool:
- `agent/tools/processing/clip_raster_to_bbox/clip_raster_to_bbox.py:479`
- `agent/tools/processing/compute_layer_bounds/compute_layer_bounds.py:116`
- `agent/tools/processing/clip_raster_to_polygon/clip_raster_to_polygon.py:482`
- `agent/tools/processing/spatial_query/spatial_query.py:107`
- `agent/tools/meta/import_user_layer/import_user_layer.py:328`
- `agent/tools/search/search_tools/search_tools.py:1211` and `:1415`
All 7 are comments/docstrings (no live code path). Action: remove the stale
reference, or rewrite to name a LIVE tool (`clip_raster_to_polygon` /
`clip_raster_to_bbox`) where the sentence still needs an example. (Test refs in
server/tests are provenance/history and out of the src scope; note but do not
sweep this wave unless a touched hunk sits there.)

### 5b. pyproject `manning_mapping.csv` glob fix

`server/pyproject.toml:247` package-data glob reads
`"agent/workflows/manning_mapping.csv"` but the file actually lives at
`server/src/trid3nt_server/agent/workflows/sfincs/manning_mapping.csv`. The glob
misses it, so the wheel omits the CSV and every SFINCS run breaks (the comment at
241 documents exactly this failure mode). Fix: change the glob to
`"agent/workflows/sfincs/manning_mapping.csv"` (or a recursive
`"agent/workflows/**/manning_mapping.csv"`). Verify no other stale
`agent/workflows/*.csv` globs point at the pre-move location.

### 5c. `cases/` package -> `agent/cases/`

Malpasset observation support is agent-side; the package belongs under `agent/`.
Move `server/src/trid3nt_server/cases/` (`__init__.py`, `malpasset_obs.py`) to
`server/src/trid3nt_server/agent/cases/` via `git mv`. It is NOT imported anywhere
in server/src (grep-verified: zero src importers). Update the external importers:
- `server/tests/test_malpasset_obs.py:16` -- `from trid3nt_server.cases import malpasset_obs`
- `scripts/run_l2_malpasset.py:168, 317, 543` -- `from trid3nt_server.cases.malpasset_obs import ...`
Retarget to `trid3nt_server.agent.cases.malpasset_obs`. No compat shim. (These
are the "touched hunk" test/script updates the move forces.)

---

## Verification gates for the executor (per doctrine)

- After extraction: full pytest with `--timeout=300`; baseline = EXACTLY 10
  failures (coastal x1, fetch_resolution x4, river_dye x5). Any NEW failure = a
  regression in the cut.
- After renames: `retrieve_visible_tools(prompt, None, 8)` model-free check that
  the new tool names (`open_case_in_qgis`, `register_case_layer`) are retrievable
  from their corpus keys.
- After the cases/ move + manning glob: import-smoke + a SFINCS/Malpasset
  direct-call to confirm package data + imports resolve.
- Big landing canary: direct-call flood run (status=ok + depth COG + envelope) +
  WS turn smoke + NATE visual in QGIS.
