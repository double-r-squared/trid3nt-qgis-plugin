# Scope census -- hydro and earth-surface systems

Ruling: `docs/IDEAS.md` "SCOPE RULED: HYDRO AND EARTH-SURFACE SYSTEMS" (NATE 2026-09-08).
Census lenses: fetcher specs + hooks, non-fetcher tools/helpers, extension boundary.
Method: LOC by `wc -l`; consumers by `grep -rl` across the tree plus `corpus.yaml` rows and
`docs/validation/code-graph/graph.json`; live_use from
`data/persistence/trid3nt_dev/tool_call_telemetry.json` (2616 dispatch records) -- NOT
`data/telemetry/tool_calls.jsonl`, which logs retrieval-pool visibility only and cannot
answer "was this called". Read-only pass at HEAD; nothing in this document has been moved.

Registry today: **174 registered tools** = 108 `source.yaml` fetcher specs (tree-walked at
`trid3nt_server/tools/fetchers/_router/registration.py:353`, called once at
`trid3nt_server/tools/__init__.py:510`) + 66 `@register_tool` decorators. Corroborated by the
live telemetry tail (`full_registry_size: 174`, ts 2026-09-07T07:07:39Z).

---

## 1. Headline -- fate table

Counts are fetcher specs plus non-fetcher tools. "product py" excludes tests; "yaml" is
`source.yaml` + `corpus.yaml`.

| fate | fetcher specs | non-fetcher tools | product py LOC | test LOC | yaml LOC | total LOC |
|---|---|---|---|---|---|---|
| **IN** | 83 | 64 | 142,336 | ~109,167 | 12,377 | ~263,880 |
| **SCOPE-ATTIC** (ruled) | 7 | 2 | 2,728 | ~1,197 | 966 | ~4,891 |
| **BORDERLINE** (unruled) | 18 | 0 | 2,300 | 847 dedicated (+ sections in 9 mixed files) | 2,521 | ~5,668+ |
| **totals** | 108 | 66 | 147,364 | 111,211 | 15,846 (fetchers) | -- |

Product-py totals are `trid3nt_server/**` + `contracts/trid3nt_contracts/**`; test totals are
`tests/**` + `contracts/tests/**`. The IN rows are residuals, not independently summed.

### Projected tree after the ruled move (SCOPE-ATTIC only)

| measure | before | after | delta |
|---|---|---|---|
| registered tools | 174 | **165** | -9 |
| fetcher `source.yaml` specs | 108 | **101** | -7 |
| `@register_tool` decorators | 66 | **64** | -2 |
| product py (server + contracts pkg) | 147,364 | 144,636 | -2,728 |
| fetcher yaml | 15,846 | 14,898 | -948 |
| test py | 111,211 | ~110,014 | -1,197 |
| processing tool dirs | 35 | 33 | -2 |
| fetcher hook modules in `_router/hooks/` | see 4a | -6 modules | -1,217 |

### If the borderline set is also ruled out (upper bound, not a recommendation)

| measure | after ruled move | after borderline too |
|---|---|---|
| registered tools | 165 | **147** |
| fetcher specs | 101 | **83** |
| product py | 144,636 | 142,336 |
| fetcher yaml | 14,898 | 12,377 |

Note: 9 of the 18 borderline specs are generic-executor-only (zero dedicated py); the other 9
carry 2,300 LOC of dedicated hooks. `fetch_overpass_pois` is the exception that cannot move
cheaply -- see section 4.

---

## 2. SCOPE-ATTIC list, ordered by LOC

Ruled by NATE: the 7 biodiversity fetchers + the 2 movement-ecology tools. Every row's
consumers were grepped tree-wide; "in-scope consumers: none" is the condition for the row to
stay here rather than fall back to BORDERLINE.

| # | item | product py | yaml | in-scope consumers | other consumers (plumbing/registry/tests) | rider tests (LOC) | live_use |
|---|---|---|---|---|---|---|---|
| 1 | `compute_movement_trajectory` (`trid3nt_server/tools/processing/compute_movement_trajectory/`) | 773 | 9 | **none** | `tools/__init__.py:543` (import); `compute_home_range_kde` | `tests/test_compute_movement_trajectory.py` (457) | 0 |
| 2 | `compute_home_range_kde` (`trid3nt_server/tools/processing/compute_home_range_kde/`) | 738 | 9 | **none** | `tools/__init__.py:538` (import) | `tests/test_compute_home_range_kde.py` (344) | 0 |
| 3 | `fetch_movebank_tracks` (`fetchers/biodiversity/fetch_movebank_tracks/`, hook `_router/hooks/movebank_tracks.py`) | 278 | 201 | **none** | rows 1-2 (both attic-bound); `credentials/credential_registry.py` | `tests/test_router_movebank.py` (169) | 2 |
| 4 | `fetch_wdpa_protected_areas` (hook `wdpa_protected_areas.py`) | 210 | 91 | **none** | `search_tools.py`, `server/dispatch/emitter.py`, `adapters/adapter.py` SYSTEM_PROMPT example | `tests/test_router_arcgis_odd.py:118-150` (~35 of 251) | 11 |
| 5 | `fetch_inaturalist_observations` (hook `inaturalist_observations.py`) | 199 | 114 | **none** | `scripts/tool_sweep.py:100-102` | `tests/test_router_chained.py:206-241` (~36 of 554) | 4 |
| 6 | `fetch_gbif_occurrences` (hook `gbif_occurrences.py`) | 197 | 102 | **none** | -- | `tests/test_router_chained.py:136-205` (~70 of 554) | 9 |
| 7 | `fetch_ebird_observations` (hook `ebird_observations.py`) | 168 | 140 | **none** | `credential_registry.py`, `contracts/tests/test_ws.py`, `scripts/gen_tool_support_page.py`, `scripts/tool_sweep.py` | `tests/test_router_keyed_misc.py:134-167` (~34 of 219) | 1 |
| 8 | `fetch_iucn_red_list_range` (hook `iucn_red_list_range.py`) | 165 | 157 | **none** | `credential_registry.py`, `scripts/gen_tool_support_page.py`, `scripts/tool_sweep.py` | `tests/test_router_keyed_misc.py:168-197` (~30 of 219) | 1 |
| 9 | `fetch_mobi` (generic raster-cog executor, **no hook module**) | 0 | 143 | **none** | `server/dispatch/emitter.py:260` (`_ALWAYS_OFFLOAD_SYNC_TOOLS` row) | `tests/test_router_keyed_misc.py:81-102` (~22 of 219) | 4 |
| | **total** | **2,728** | **966** | | | **~1,197** | 32 |

Rider-test classes:
- **owned, move whole (970 LOC):** `test_compute_home_range_kde.py` (344),
  `test_compute_movement_trajectory.py` (457), `test_router_movebank.py` (169).
- **mixed, sections extracted (~227 LOC, ESTIMATED from the line ranges above):**
  `test_router_keyed_misc.py`, `test_router_chained.py`, `test_router_arcgis_odd.py` --
  each also covers in-scope specs and must be split, not moved.
  `test_router_arcgis_odd.py:24` imports `wdpa_protected_areas` at module scope, so the split
  is a hard blocker: the file fails at import the moment the hook leaves.
- **incidental, do NOT move -- re-anchor to an in-scope stand-in (15 files):**
  `test_credential_pipeline.py` (9 hits), `test_multi_turn_loop.py` (9),
  `test_scenario_reuse_fetch_f96.py` (8), `test_dispatch_guards_stage3.py` (6),
  `test_gemini_kwargs_fuzz.py` (6), `test_thought_signature.py` (5),
  `test_search_tools.py` (4), `test_gemini_schema_compliance.py` (2),
  `test_system_prompt.py` (2), `test_tools_registry.py` (2),
  `test_tool_retry_on_failure.py` (2), `test_telemetry.py` (1),
  `test_duplicate_flood_layer_fix.py` (1), `contracts/tests/test_ws.py` (1),
  plus the non-suite live drivers `tests/eval_routing_live.py` (3) and
  `tests/live_evidence_job_0169.py` (6).
  Two are retrieval FIXTURES needing a re-baseline, not a rename:
  `tests/test_search_tools.py:107` (recall pair `("national parks polygons",
  "fetch_wdpa_protected_areas")`, assertions `:313-315`) and
  `tests/test_system_prompt.py:143` (`assert "fetch_wdpa_protected_areas" in SYSTEM_PROMPT`).
  Suggested stand-ins: `fetch_firms_active_fire` for every credential/auth test;
  `fetch_fema_nfhl_zones` or `fetch_nwi_wetlands` for every vector-polygon dispatch/reuse test.

---

## 3. BORDERLINE list

The ruling's test: **an in-scope consumer KEEPS the source.** Applied per row. "in-scope
consumer" excludes registry tables, dispatch/emitter plumbing, credential rows, tests, and
`scripts/tool_sweep.py`-class fixtures -- those exist for every tool and prove nothing.

| # | spec | hook LOC | yaml | live_use | in-scope consumer that would keep it | recommendation |
|---|---|---|---|---|---|---|
| 1 | `fetch_population` (worldpop.py) | 252 | 132 | 20 | **`compute_exposure_summary`** (flood consequence, explicitly IN) + `fetch_from_catalog` catalog row + `public_data_source_catalog.yaml` | **KEEP -- IN** |
| 2 | `fetch_hrsl_population` | 0 | 120 | 4 | **`compute_building_density`** (explicitly IN) | **KEEP -- IN** |
| 3 | `fetch_fault_sources` (fault_sources.py) | 283 | 101 | 2 | **`model_debris_flow`** (post-fire hydrology, explicitly IN) + `contracts/execution.py`, `contracts/source_spec.py` | **KEEP -- IN.** Strongest borderline keep: seismic geometry consumed by a hydro hazard tool, not by a seismic-hazard tool. |
| 4 | `fetch_ghsl_population` | 0 | 132 | 2 | none (emitter row only) | **KEEP -- IN by group coherence** with rows 1-2: it is the same population class, and splitting the population trio leaves a fallback ladder with a hole. See Q1. |
| 5 | `fetch_field_boundaries` (field_boundaries.py) | 222 | 119 | 4 | none found; but it is the AOI substrate for the plume/FOTW ag-field validation case | **KEEP -- IN, consumer-pending.** Re-check at the MODFLOW-GWT wave; attic if that case does not land. |
| 6 | `fetch_overpass_pois` (shares `_router/hooks/overpass.py`, 733 LOC, with 7 IN specs) | 0 movable | 125 | 20 | none | **KEEP -- IN.** Attic-ing it saves 125 yaml lines and zero py, and would leave a partial theme in a shared hook. Not worth a cut. |
| 7 | `fetch_usgs_earthquakes` (usgs_earthquakes.py) | 257 | 143 | 26 | none (bench/sweep fixtures only) | **SCOPE-ATTIC candidate.** Highest live_use of any borderline row -- a demand signal, not a scope argument. See Q1. |
| 8 | `fetch_usgs_volcano_alerts` (usgs_volcano.py) | 205 | 126 | 11 | none | **SCOPE-ATTIC candidate** (with row 7 as the seismic/volcano group). |
| 9 | `fetch_openfema_disasters` (openfema_disasters.py) | 457 | 184 | 3 | none | **KEEP -- IN.** Disaster declarations are the event index for flood/fire cases; largest single borderline hook, and the theme is water-hazard-adjacent by use. See Q1. |
| 10 | `fetch_epa_frs_facilities` (epa_frs_facilities.py) | 205 | 100 | 2 | **none at all** -- not even a test beyond `test_router_arcgis_odd.py` | **SCOPE-ATTIC candidate.** Cleanest cut on the board. |
| 11 | `fetch_hifld_critical_infrastructure` | 0 | 143 | 10 | none | **SCOPE-ATTIC candidate** (infrastructure group with row 12). |
| 12 | `fetch_hifld_transmission_lines` | 0 | 125 | 1 | none | **SCOPE-ATTIC candidate.** |
| 13 | `fetch_cdc_svi` | 0 | 134 | 3 | none | **SCOPE-ATTIC candidate** (demographic group with 14-16). |
| 14 | `fetch_census_acs` | 0 | 161 | 2 | none | **SCOPE-ATTIC candidate** (demographic group). |
| 15 | `fetch_epa_ejscreen` | 0 | 176 | 3 | none | **SCOPE-ATTIC candidate** (demographic group). |
| 16 | `fetch_lehd_jobs` | 0 | 224 | 2 | none | **SCOPE-ATTIC candidate** (demographic group). |
| 17 | `fetch_airnow_air_quality` (airnow_air_quality.py) | 173 | 141 | 4 | none | **SCOPE-ATTIC candidate** (air-quality group with 18) -- unless the smoke story claims it; `fetch_hrrr_smoke` is IN and air quality is its natural observation counterpart. See Q1. |
| 18 | `fetch_openaq_measurements` (openaq_measurements.py) | 246 | 135 | 5 | none | **SCOPE-ATTIC candidate** (air-quality group). |

Dedicated borderline rider tests (LOC): `test_router_fault_sources.py` 189,
`test_router_field_boundaries.py` 86, `test_router_lehd_jobs.py` 284,
`test_router_population.py` 288 = 847. Nine further files carry borderline sections mixed with
in-scope ones and would need splitting: `test_router_hooks.py` (426, earthquakes + volcano),
`test_router_overpass.py` (406, pois + roads + buildings), `test_router_stations.py` (287,
airnow + openaq + coops), `test_router_promotion.py` (336, hifld + cdc_svi + census_acs),
`test_router_fanout_routing.py` (359, ejscreen), `test_router_zip_multifile.py` (249, ghsl),
`test_router_arcgis_odd.py` (251, epa_frs), `test_router_chained.py` (554, openfema),
`test_catalog_surfacing.py` (fault_sources + openfema + census_acs).

**Non-fetcher borderline carried forward, unresolved at this lens:** `docs/proof/templates/`
holds 19 OpenQuake and Pelicun (seismic hazard / damage) proof artifacts with no in-scope
consumer tool. These are engine-level, not tool-level; they belong to an engines census pass.
See Q3.

Recommendation summary: **6 KEEP-IN, 12 SCOPE-ATTIC candidates** grouped as demographic (4),
infrastructure (3), seismic/volcano (2), air quality (2), EPA FRS (1). If all 12 go, the tree
loses a further 1,086 py LOC, 1,608 yaml lines, and 12 registered tools.

---

## 4. Couplings the move must cut, and the residue to ledger

### 4a. Product-code leaks (must be cut for the ruled move -- 8 sites)

| # | site | what it is | cut |
|---|---|---|---|
| L1 | `trid3nt_server/tools/fetchers/_router/hooks/__init__.py:204,205,224,240,241,258` | 6 explicit imports of the biodiversity hook modules. **This is the leak that makes the boundary dirty**: hooks live in a shared directory, not beside their spec, so a "self-contained fetcher package" is a lie today. | 6 lines (or 0 under co-location, Q4/option A) |
| L2 | `trid3nt_server/tools/__init__.py:538,543` | the 2 processing-tool import lines | 2 lines |
| L3 | `trid3nt_server/credentials/credential_registry.py:131-142,155-165,166-176` (PROVIDERS rows `ebird`/`movebank`/`iucn_red_list`), `:186,189,190` (TOOL_PROVIDER), `:209,218,221` (TOOL_AUTH_ERROR_CODES), `:19-23` (docstring) | hardcoded tool->provider, provider->signup-URL, and auth-error-code tables. **Deepest coupling: the credential registry is a closed list**, so no extension can bring its own provider. | rows out; the 3 rows move into the extension MANIFEST as `credentials.yaml` |
| L4 | `contracts/trid3nt_contracts/secrets.py:111-113` (`ebird`, `iucn_red_list`, `movebank` in the provider-id allowlist); illustrative eBird prose at `:171,312,335,343,347` | a CONTRACTS-level closed provider enum. A contracts change means the plugin ships in lockstep. | 3 ids; re-anchor the prose to FIRMS or AirNow |
| L5 | `trid3nt_server/scenario_reuse.py:133-136` (`_FETCH_TOOL_KIND` rows wdpa/gbif/inaturalist/ebird) + matching `_FETCHED_KIND_MARKERS` rows | fetched-layer reuse recognition keyed by tool name. Safe by construction: the map is explicitly conservative (`scenario_reuse.py:122-131` -- an absent tool simply gets no hint). | 4 rows + markers |
| L6 | `trid3nt_server/adapters/adapter.py:478-481` (the "show me protected areas in Big Cypress" precursor->tool worked example inside SYSTEM_PROMPT), `:466-467` ("WDPA", "GBIF", "iNaturalist", "eBird" in the name-the-source list) | **the system prompt teaches routing with a WDPA example.** Cutting it is a routing behavior change, not a cleanup. Asserted by `tests/test_system_prompt.py:117,122,142-143`. | cut and re-anchor to an in-scope pair (e.g. "show me flood zones in Cape Coral" -> `fetch_fema_nfhl_zones`), with a before/after top-k table |
| L7 | `trid3nt_server/server/dispatch/emitter.py:260` (`"fetch_mobi"` in `_ALWAYS_OFFLOAD_SYNC_TOOLS`), `:847` (a WDPA comment example) | offload allowlist row + comment | 1 row, 1 comment re-worded |
| L8 | `tools/search/search_tools/search_tools.py:979,1061`; `tools/fetchers/_router/shape_classifier.py:24`; `tools/fetchers/imagery/_pc_stac.py:3`; `main.py:89,123-125`; `tools/__init__.py:402-407` | comments/docstrings that explain a SHARED mechanism using a biodiversity example. `_pc_stac.py:3` frames a Planetary-Computer signing module (IN) as "the conservation tool set". Under the comments-are-constraints norm these are live explanations, not history -- they must not name a tool that no longer exists. | re-word to in-scope examples |

### 4b. What is NOT coupled (measured, not assumed)

- **Registration is a tree walk.** `register_specs_from_tree()`
  (`_router/registration.py:353`) rglobs `fetchers/**/source.yaml` via `_router/spec.py:117`.
  **Deleting a fetcher directory de-registers it with no edit anywhere** -- no import line, no
  table row, no name list (`trid3nt_server/tools/README.md:10`).
- **Corpus/retrieval index is built in memory** from the live registry plus rglobbed
  `corpus.yaml` (`server/protocol/catalog_http.py:162`,
  `tools/search/search_tools/search_tools.py:456`). No persisted BM25/dense artifact to rebuild.
  The residual monolith `trid3nt_server/tools/tool_query_corpus.yaml` is 11 lines and carries no
  row for any of the 9 subject tools.
- **Styles:** `emission/presets.py` is fully generic; style lives inline in each `source.yaml`.
  Zero theme rows.
- **Contracts types / JSON schemas:** `contracts/schemas/*.json` are envelope/WS/document
  shapes. The 2 movement tools return generic dict/LayerURI, not typed contract classes, so
  there is no contracts-level split to make.
- **SysML:** `docs/model/*.sysml` (6 seams) -- zero hits for any of the 9 names.
- **Workflows / engine templates:** `trid3nt_server/workflows/**` -- zero hits.
- **Public catalog:** `public_data_source_catalog.yaml` -- zero hits.
- **Plugin:** the only hit is `plugin/tests/test_credential.py:239,245,253,262-263` using
  `"ebird"` as a fake provider key. No dock card names a biodiversity tool.
- **Hook orphan check:** each of the 6 biodiversity hook modules is named by exactly ONE
  `source.yaml` -- its own. No hook is shared across the attic/in-scope boundary, and no
  in-scope spec names a biodiversity hook. The genuinely shared hooks (`cds.py` 680 for
  era5+gtsm, `overpass.py` 733 for 8 specs, `goes_animation.py`, `goes_archive.py`, `hrrr.py`)
  are all IN or IN-anchored and stay.
- **Router core stays whole:** `_router/router.py` (1104), `registration.py` (446), `spec.py`
  (130), `shape_classifier.py` (206), `errors.py` (139), `emit_on_fetch.py` (208), all
  `executors/*`, `transforms/*`, `transport/*`, `_fetch_common.py`, `_public_s3.py`,
  `us_states.py`. Domain-agnostic; nothing orphans.
- **Cross-package edge:** `movement_ecology` DEPENDS on `biodiversity`
  (`fetch_movebank_tracks` is its only track source, and `compute_home_range_kde` imports a
  `compute_movement_trajectory` helper). The two packages must move in the SAME wave and both
  MANIFESTs must name the edge.

### 4c. Tooling and docs residue

| site | what | action |
|---|---|---|
| `scripts/gen_tool_support_page.py:52-59,60-63` | `KEY_EARMARKS` rows for ebird + iucn | cut rows; page regenerates |
| `scripts/tool_sweep.py:100-102` | sweep arg fixtures for ebird/inat/iucn | cut rows |
| `docs/site/tool-support.md` | generated | regenerate |
| `trid3nt_server/tools/README.md:26` | names `biodiversity` in the fetcher-subfolder table | cut the word |
| `docs/authoring/writing-a-tool.md:32` | lists `biodiversity` among the domain folders an author picks | cut the word |
| `docs/decisions/0041,0051,0059,0061,0071,0077,0090,0225` | multi-topic ADRs citing these tools | LEAVE (history); the MANIFEST cites them by number |
| `docs/validation/code-graph/graph.json`, `docs/validation/lean-sweep-inventory.md` | generated | regenerate |
| `docs/reports/ab-2026-07-07/*`, `docs/reports/tool-*`, `docs/specs/*` | frozen evidence dumps | LEAVE |
| `docs/DELETION_LEDGER.md` | 9 existing mentions | ADD the move rows (section 5c) |

---

## 5. Attic layout, MANIFEST, ledger row

### 5a. Two attics, two contracts

`~/Documents/trid3nt-attic` = REPLACED work; its README says "Nothing here is on the import
path, is built, or is tested." The scope attic is the **opposite**: intact, tested at move
time, re-mountable. Its README must say so in the first paragraph or someone will treat it as
dead. Proposed path, mirroring the existing convention: **`~/Documents/trid3nt-scope-attic`**.

```
~/Documents/trid3nt-scope-attic/
  README.md                                  # MOUNTABLE, not dead. States the contract.
  extensions/
    biodiversity/
      MANIFEST.md
      credentials.yaml                       # the 3 provider rows lifted from L3/L4
      trid3nt_server/tools/fetchers/biodiversity/    # mirrored repo paths, 7 spec dirs
        fetch_ebird_observations/{source.yaml,corpus.yaml,__init__.py,hooks.py}
        ... x7
      tests/{test_router_movebank.py,test_biodiversity_hooks.py}
    movement_ecology/
      MANIFEST.md
      trid3nt_server/tools/processing/compute_home_range_kde/
      trid3nt_server/tools/processing/compute_movement_trajectory/
      tests/{test_compute_home_range_kde.py,test_compute_movement_trajectory.py}
```

Paths mirror repo-relative locations (same convention as `trid3nt-attic`), so a re-mount is a
directory copy, not a reconstruction.

### 5b. Per-package `MANIFEST.md` shape

```markdown
# Extension: biodiversity

MOVED OUT: 2026-09-XX, commit <sha>, under the scope ruling
(docs/IDEAS.md "SCOPE RULED: HYDRO AND EARTH-SURFACE SYSTEMS", NATE 2026-09-08).
STATUS: sound, tests green at move time. Returnable.

## Re-mount contract
1. Copy `trid3nt_server/` and `tests/` from this package over the repo root
   (paths already mirror).
2. Registration lines to add: NONE (fetchers are tree-walked from
   fetchers/**/source.yaml; hooks are co-located hooks.py).
3. Credential rows to restore: see credentials.yaml -- 3 provider rows into
   trid3nt_server/credentials/credential_registry.py and 3 ids into
   contracts/trid3nt_contracts/secrets.py PROVIDER_IDS.
4. Expected registry size after re-mount: 165 -> 172.

## Contents
| tool | spec | hook LOC | tests | live_use at move |
| fetch_gbif_occurrences | source.yaml + corpus.yaml | 197 | test_biodiversity_hooks.py::gbif | 9 |
... (7 rows)

## Provides / requires
Requires: _router executors vector_fgb, raster_cog, http_json, chained_resolution;
the credential pipeline.
Provides: 7 fetch tools. Consumed by: movement_ecology (fetch_movebank_tracks).

## ADRs
0041, 0051, 0059, 0061, 0071, 0077, 0090, 0225 (left in the repo; cited, not moved).

## Why it left
Out of scope, not broken. Live dispatches at move time: 32 across 9 tools.
```

### 5c. Ledger row shape

`docs/DELETION_LEDGER.md` is the existing register and its columns fit, but the move needs a
THIRD status beside QUEUED / CONDITION-MET / DELETED, because nothing is deleted:

| Candidate | Scope | Condition to delete | Status | Source |
|---|---|---|---|---|
| biodiversity extension (7 fetch specs + 6 hook modules + 3 credential providers) | `trid3nt_server/tools/fetchers/biodiversity/`, `_router/hooks/{gbif_occurrences,inaturalist_observations,wdpa_protected_areas,ebird_observations,iucn_red_list_range,movebank_tracks}.py`, `credentials/credential_registry.py`, `contracts/trid3nt_contracts/secrets.py` | NOT a deletion -- OUT OF SCOPE under the 2026-09-08 ruling. Moved intact with tests to `~/Documents/trid3nt-scope-attic/extensions/biodiversity/`. Returns as an extension when a cross-discipline question earns it. | **SCOPE-ATTIC(<sha>)** | `docs/IDEAS.md` scope ruling; `docs/validation/scope-census.md` |
| movement_ecology extension (`compute_home_range_kde`, `compute_movement_trajectory`) | `trid3nt_server/tools/processing/` | as above; depends on `biodiversity/fetch_movebank_tracks` -- returns only with it | **SCOPE-ATTIC(<sha>)** | as above |

Plus one residue row per re-anchored seam (the SYSTEM_PROMPT worked example, the
`test_search_tools.py` recall fixture, the contracts provider enum) so the re-anchoring is
auditable rather than buried in a diff.

---

## 6. The move wave -- stages and verification

Standing norms this wave sits under: continuous deploy as work lands green; path-scoped commits
while agents are in flight; every landing removes what it supersedes; a fresh-eyes
spec-conformance table at close.

**Stage 0 -- BASELINE (read-only, one evidence commit).**
Record `len(get_registered_tools())` (expect **174**), the fetcher spec count (108), the
`@register_tool` count (66), the retrieval top-k for the 3 fixtures that name an atticked tool,
and an offline suite run (expect **exactly zero failures**, the standing baseline).

**Stage 1 -- STRUCTURE (no behavior change, in-scope only).**
Only if Q4 lands on option A: co-locate every single-spec hook module beside its spec as
`hooks.py` and teach `_router/hooks/__init__.py` to walk `fetchers/**/hooks.py`. Leave the
genuinely shared modules (`cds.py`, `overpass.py`, `dem_3dep.py`, `topobathy*.py`,
`goes_*.py`, `hrrr.py`) where they are.
GATE: registered count still 174; suite zero; no `source.yaml` edited.

**Stage 2 -- RE-ANCHOR (in-scope only, still nothing moved).**
Cut the 15 incidental test files and the 2 retrieval fixtures over to in-scope stand-ins;
re-anchor the SYSTEM_PROMPT worked example (L6) with a before/after top-k table; re-word the L8
comments; split the 3 mixed router test files.
GATE: registered count still 174; suite zero.
**This is the only stage verifiable without the move having happened -- do it first,
deliberately.**

**Stage 3 -- MOVE.**
`git mv` the 2 packages into `~/Documents/trid3nt-scope-attic/extensions/*` with mirrored paths;
write both MANIFESTs; cut L1 (6 lines, or 0 under option A), L2, L3, L4, L5, L7; cut the
`scripts/` fixture rows and the two README/doc words; add the ledger rows.
GATE: registered count **165**; spec files 101; `@register_tool` 64; suite zero.

**Stage 4 -- REGENERATE.**
`docs/site/tool-support.md`; `scripts/code_graph.py` -> `docs/validation/code-graph/graph.json`;
`docs/validation/module-coverage-board.md` if it carries these rows.

**Stage 5 -- PROVE THE RE-MOUNT ONCE, THEN REVERT.**
On `movement_ecology` (smaller, and the one with a stated dependency): copy its two dirs back,
add only the import lines its MANIFEST names, run its two test files, confirm the registered
count returns to 167, then `git checkout` the tree back. Attach the transcript to the wave
report. **If the re-mount needs an edit the MANIFEST did not name, the boundary is not clean
and Stage 3 is not done.**

### Verification checklist -- every row is a command, not a claim

| # | check | expected |
|---|---|---|
| V1 | `len(get_registered_tools())` before / after | 174 -> 165 |
| V2 | `find trid3nt_server/tools/fetchers -name source.yaml \| wc -l` | 108 -> 101 |
| V3 | `grep -rc '@register_tool' trid3nt_server/tools --include=*.py` | 66 -> 64 |
| V4 | offline suite, 5 slices incl. contracts | exactly zero failures, both sides |
| V5 | retrieval fixtures re-baselined | `test_search_tools.py` recall pairs name only in-scope tools; top-k recorded in the wave report |
| V6 | **sweep guard -- no in-scope module references an atticked one:** `grep -rnE "fetch_(ebird_observations\|gbif_occurrences\|inaturalist_observations\|iucn_red_list_range\|mobi\|movebank_tracks\|wdpa_protected_areas)\|compute_(home_range_kde\|movement_trajectory)" trid3nt_server/ contracts/ plugin/ scripts/ tests/ --include=*.py --include=*.yaml` | ZERO hits |
| V7 | system prompt free of the theme: `python -c 'from trid3nt_server.adapters.adapter import SYSTEM_PROMPT; print(SYSTEM_PROMPT)' \| grep -icE "wdpa\|gbif\|inaturalist\|ebird"` | 0 |
| V8 | credential surface closed cleanly -- ids in `contracts/secrets.py` == `credential_registry.PROVIDERS` == providers reachable from a live TOOL_PROVIDER row | consistent, 3 fewer |
| V9 | re-mount rehearsal (Stage 5) | MANIFEST steps suffice; count returns to 167; tests pass; reverted |
| V10 | flagship canary unaffected | authored mesh -> `.supplied()` -> solve -> full packet (layers + composite + charts + GIF), green |

V6 is the sweep guard the paradigm-wave law asks for. V9 is the only check that proves
"returnable extension" rather than asserting it.

---

## 7. DESIGN questions for NATE

**Q1. The borderline groups -- which of the 12 candidates go?**
Section 3 splits the 18 borderline specs into 6 KEEP-IN (population trio, fault sources, field
boundaries, POIs, openfema) and 12 attic candidates in 5 groups: demographic (cdc_svi,
census_acs, ejscreen, lehd_jobs), infrastructure (hifld x2, epa_frs), seismic/volcano
(earthquakes, volcano_alerts), air quality (airnow, openaq).
*Recommendation:* attic **demographic (4) + infrastructure (3)** now -- zero in-scope consumers,
1,086 py LOC and 7 tools out. **Hold seismic/volcano and air quality**: earthquakes has the
highest live_use on the board (26) and volcano 11, which says users ask; and air quality is the
observation counterpart to `fetch_hrrr_smoke`, which is IN. Ruling them out is cheap later
(they are group-clean); ruling them back in after a move costs a re-mount.

**Q2. Does the attic keep its own copy of the tests, or do the tests only live in git history?**
*Recommendation:* **the attic keeps them.** The scope attic's whole contract is "sound, intact,
returnable", and a package whose tests must be archaeologically recovered from a commit is not
returnable -- it is deleted with extra steps. Cost is ~1,197 LOC of duplicated-then-removed test
code, which is the price of the guarantee. The mixed-file sections (~227 LOC) get extracted into
a new `tests/test_biodiversity_hooks.py` inside the package, not left half in the repo.

**Q3. Do `docs/proof/templates/` artifacts of atticked themes move, or stay frozen in the repo?**
19 OpenQuake/Pelicun (seismic) proof artifacts currently sit there with no in-scope consumer.
*Recommendation:* **proofs stay frozen in the repo, and the MANIFEST cites them by path.**
Proofs are dated evidence of what the system did on a given day -- moving them rewrites the
evidence record, and the frozen-evidence rule already covers `docs/reports/*`. The MANIFEST's
"Contents" table gains a `proofs (frozen, not moved)` row so a re-mount knows where to look.
Note this question is only live if Q1 rules the seismic group out; the ruled 9 have no proof
artifacts at all.

**Q4. Hook co-location (option A) or the 6-line registration list (option B)?**
Today hooks live in the shared `_router/hooks/` and are imported by name from
`hooks/__init__.py` -- so a "self-contained fetcher package" still requires editing shared code
on both the move and the re-mount.
*Recommendation:* **option A -- co-locate hooks as `fetchers/<group>/<spec>/hooks.py` and
tree-walk them**, matching how `source.yaml` and `corpus.yaml` already register. It makes the
fetcher re-mount edit count exactly ZERO, makes the entire borderline set free to move later at
no marginal shared-code cost, and is the design the ruling's standalone-fetcher-subsystem note
implies. Blast radius is one shared loader, gated by "registered count still 174" at Stage 1.
Option B is the smaller wave but leaves the closed list in place for every future extension.

**Q5. The extension re-mount contract -- what exactly does it promise?**
*Recommendation:* the MANIFEST promises **(a)** mirrored paths, so re-mount is `cp -r` over the
repo root; **(b)** an explicit, exhaustive list of registration lines to add (target: zero for
fetchers under Q4/A, one per processing tool); **(c)** a `credentials.yaml` naming every
provider row and contracts id to restore; **(d)** a stated expected registry size after
re-mount; **(e)** a named `Requires:` list of shared subsystems (router executors, credential
pipeline) that must still exist. And the contract is only real if Stage 5 rehearses it once, on
`movement_ecology`, with the transcript attached -- a MANIFEST that has never been executed is a
hypothesis.

**Q6 (secondary, flagged because it changes runtime behavior).** L6 cuts the SYSTEM_PROMPT's
only worked precursor->tool example and replaces it with a flood-zone pair. That will shift
retrieval for nearby asks.
*Recommendation:* land it in Stage 2 with a before/after top-k table over the routing bench, so
the shift is measured rather than discovered in a live session. If the table shows regression on
in-scope asks, the example choice -- not the cut -- is what gets revised.

**Q7 (secondary).** The ledger needs a third status token; `SCOPE-ATTIC(<sha>)` proposed
alongside QUEUED / CONDITION-MET / DELETED, and the scope-attic path confirmed as
`~/Documents/trid3nt-scope-attic`. *Recommendation:* both as proposed.
