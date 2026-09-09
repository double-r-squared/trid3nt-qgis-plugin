# Hygiene manifest -- `trid3nt_server/tools/fetchers`

Read-only census. Every file tracked under the scope was opened end to end by the agent that
wrote its row; the greps below are guards run after the read, never the census itself.

Scope listing: `git ls-files trid3nt_server/tools/fetchers` -> 405 files (206 `.py`, 199 `.yaml`).
`find` returns 162 more paths; all are untracked `__pycache__/*.pyc` and are out of scope.

Pure LOC is `scripts/loc_report.py`: total - blank - comment - docstring. YAML never counts as LOC,
so the YAML tables carry no LOC column and give physical lines instead.

Docstring limits: function/class <= 3 physical lines, module <= 5, LLM-facing <= 1000 chars in front.
Disallowed-class codes: `hist` history, `spec` spec notation, `attrib` person attribution or memory
filename, `rationale` why-essay, `arch` architecture / neighbour reference, `narrative` usage
narrative, `examples` worked examples, `rollcall` per-field roll-call of a typed model.

## A. Python modules (206)

| path | pure LOC | docstrings: count / lines / over-limit | disallowed classes | comment blocks > 6 lines | history markers | dead or moved references | fate | notes |
|---|---|---|---|---|---|---|---|---|
| `trid3nt_server/tools/fetchers/__init__.py` | 0 (loc_report yields -1: a blank line inside a docstring-only module is subtracted twice) | 1 / 4 / 0 | - | 0 | 0 | none | KEEP | 4-line module docstring, inside the 5-line cap, but it names three neighbour modules (`_fetch_common`, `_public_s3`, `us_states`) -- a neighbour reference. loc_report returns -1 pure LOC here because the blank line inside a docstring-only module is subtracted twice. |
| `trid3nt_server/tools/fetchers/_fetch_common.py` | 76 | 8 / 61 / 6 | hist, spec | L25 (10L) constraint -- re-raise contract; carries A.6 / server.py M1 spec notation + 'mapping not yet landed' | 0 | `server.py` (no such module; error mapping lives under `trid3nt_server/server/`) | TRIM | 6 docstrings over the physical-line cap; spec notation in the docstrings/comments; fold/twin history. |
| `trid3nt_server/tools/fetchers/_public_s3.py` | 17 | 4 / 20 / 1 | - | 0 | 1 -- the 2026-07-06 local tool sweep: fetch_glm_lightning, fetch_hrrr_forec | none | TRIM | Module docstring names 'the 2026-07-06 local tool sweep' -- dated history. |
| `trid3nt_server/tools/fetchers/_router/__init__.py` | 1 | 1 / 15 / 1 | spec | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap; spec notation in the docstrings/comments. |
| `trid3nt_server/tools/fetchers/_router/emit_on_fetch.py` | 116 | 5 / 56 / 4 | examples | 0 | 1 -- Design facts (settled semantics, docs/IDEAS.md 2026-08-13): | none | TRIM | Module docstring cites 'docs/IDEAS.md 2026-08-13' as a Design-facts source -- memory/date reference. |
| `trid3nt_server/tools/fetchers/_router/errors.py` | 47 | 10 / 49 / 5 | hist, spec, examples | 0 | 0 | none | TRIM | 5 docstrings over the physical-line cap; spec notation in the docstrings/comments; fold/twin history. |
| `trid3nt_server/tools/fetchers/_router/executors/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/_router/executors/animation_frames.py` | 65 | 2 / 28 / 1 | - | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/_router/executors/chained_resolution.py` | 89 | 6 / 47 / 4 | hist | 0 | 0 | none | TRIM | 4 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/_router/executors/dataretrieval_delegate.py` | 289 | 9 / 45 / 4 | hist, spec | L119 (8L) narration -- 'Reproduces fetch_usgs_water_quality' / 'the twin's' -- fold history banner; L285 (8L) narration -- same fold-history banner shape for nldi_navigate | 2 -- Water Data OGC API migration churn. A spec opts in with ``ingest.deleg | none | TRIM | 4 docstrings over the physical-line cap; spec notation in the docstrings/comments; fold/twin history. |
| `trid3nt_server/tools/fetchers/_router/executors/http_json.py` | 136 | 9 / 75 / 8 | hist | 0 | 0 | none | TRIM | 8 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/_router/executors/library_delegate.py` | 70 | 5 / 56 / 5 | hist, spec | L139 (9L) constraint -- which typed errors must survive un-clobbered; in-body, carries 'Broadened from the original' | 1 -- routed by the legacy ``ingest.delegate.library == 'dataretrieval'`` se | none | TRIM | 5 docstrings over the physical-line cap; spec notation in the docstrings/comments; fold/twin history. |
| `trid3nt_server/tools/fetchers/_router/executors/overpass_sidecar.py` | 50 | 4 / 28 / 2 | hist | 0 | 0 | none | TRIM | 2 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/_router/executors/raster_cog.py` | 1251 | 26 / 192 / 20 | hist, spec | L367 (9L) constraint -- single-URL opener returns all-NaN on a multi-tile VRT; L562 (11L) constraint -- source-CRS windowing rule; tail is fold narration ('STRICT no-op for every prior spec'); L703 (7L) constraint -- gzip stream is not byte-servable; trailing '., chirps_precipitation.' is a broken fragment; L837 (11L) constraint -- GRIB driver needs a real path, so bytes land in a tempfile; L982 (10L) constraint -- griddap server-side subset + the honest-no-data markers; L1142 (9L) constraint -- DEFLATE member is not byte-windowable; trailing '..' fragment; L1301 (11L) constraint -- coverage returns class integers, not palette indices; L1399 (12L) constraint -- uint8 first-valid mosaic; a 404 tile is a coverage gap, all-nodata is EMPTY; L1669 (10L) constraint -- a fully-transparent export is valid, never a typed EMPTY | 0 | none | TRIM | 20 docstrings over the physical-line cap; spec notation in the docstrings/comments; fold/twin history. |
| `trid3nt_server/tools/fetchers/_router/executors/record.py` | 32 | 2 / 28 / 2 | - | 0 | 0 | none | TRIM | 2 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/_router/executors/stac_raster.py` | 546 | 29 / 135 / 13 | hist | 0 | 1 -- A multi-band asset described only by the legacy ``eo:bands`` list reso | none | TRIM | 13 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/_router/executors/station_timeseries.py` | 402 | 16 / 68 / 8 | spec | L288 (8L) narration -- 'STRICT no-op for every timeseries spec' / 'the audit's HYBRID hook' -- fold history | 1 -- ``"2022-09-28 00:00" -> "2022-09-28T00:00Z"`` (space -> ``T`` + ``Z``  | none | TRIM | 8 docstrings over the physical-line cap; spec notation in the docstrings/comments. |
| `trid3nt_server/tools/fetchers/_router/executors/vector_fgb.py` | 321 | 11 / 68 / 8 | hist, spec | L44 (10L) constraint -- where_clauses rule contract; header carries 'phase-2 wave-2'; L76 (17L) constraint -- column_map rule-field reference table; header carries 'phase-2 wave-2' | 0 | none | TRIM | 8 docstrings over the physical-line cap; spec notation in the docstrings/comments; fold/twin history. |
| `trid3nt_server/tools/fetchers/_router/executors/vector_ogr.py` | 197 | 9 / 64 / 5 | - | 0 | 0 | none | TRIM | 5 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/_router/hooks/__init__.py` | 65 | 10 / 100 / 6 | hist, spec | 0 | 0 | none | TRIM | 6 docstrings over the physical-line cap; spec notation in the docstrings/comments; fold/twin history. |
| `trid3nt_server/tools/fetchers/_router/hooks/cds.py` | 527 | 11 / 55 / 4 | hist, spec | 0 | 0 | none | TRIM | 4 docstrings over the physical-line cap; spec notation in the docstrings/comments; fold/twin history. |
| `trid3nt_server/tools/fetchers/_router/hooks/goes_animation.py` | 247 | 11 / 52 / 5 | - | 0 | 0 | none | TRIM | 5 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/_router/hooks/goes_archive.py` | 183 | 8 / 43 / 3 | hist | 0 | 0 | none | TRIM | 3 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/_router/hooks/hrrr.py` | 220 | 9 / 55 / 6 | hist | 0 | 1 -- The ``(level, s3_var)`` used to PROBE the cycle for ``variable``. | none | TRIM | 6 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/_router/hooks/hyriver.py` | 83 | 9 / 37 / 2 | - | 0 | 0 | none | TRIM | 2 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/_router/hooks/osm.py` | 91 | 6 / 39 / 3 | - | 0 | 0 | none | TRIM | 3 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/_router/hooks/pfdf_raster.py` | 115 | 6 / 39 / 4 | hist | 0 | 0 | none | TRIM | 4 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/_router/hooks/topobathy.py` | 1260 | 43 / 262 / 22 | hist, spec, attrib, examples | L837 (15L) constraint -- why the 3DEP land leg is waterline-masked under force_bathy_base, with the measured Chignik evidence; L1880 (7L) constraint -- which params suppress the coverage claim and why include_regional_fine is not one; in-body | 1 -- # (NATE resolution doctrine, 2026-08-11 -- the 0221 blockiness was CUD | none | TRIM | Largest hook module. `_COVERAGE_COMPLETE` is defined at line 1638 but used at 1497/1501. Line 1314 comment carries 'NATE resolution doctrine, 2026-08-11' -- person attribution, an explicitly disallowed class. |
| `trid3nt_server/tools/fetchers/_router/hooks/topobathy_class.py` | 189 | 6 / 94 / 5 | - | 0 | 0 | none | TRIM | 5 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/_router/registration.py` | 241 | 15 / 91 / 10 | hist, spec | L63 (7L) constraint -- catalog-arm flag semantics; names the MISSING experiments/catalog_surfacing/DESIGN.md; L127 (7L) constraint -- schema_optional reproduces the twin's Optional handling; in-body, 'wave-2' x3; L313 (8L) constraint -- tier resolution order (internal wins over catalog arm); in-body | 2 -- Promotion registration (data-router fold, phase-2 wave 1 -- the first  | `experiments/catalog_surfacing/DESIGN.md` (absent); `agent/tools/__init__.py` (absent -- the caller is `trid3nt_server/tools/__init__.py`); module docstring names `sfincs_forcing_autowire`, which no spec declares | TRIM | Promotion seam. Module docstring opens 'data-router fold, phase-2 wave 1 -- the first real cut'. |
| `trid3nt_server/tools/fetchers/_router/router.py` | 689 | 17 / 114 / 12 | hist, spec | L535 (13L) constraint -- dispatch precedence; tail is fold narration ('No-op for every prior spec'); in-body; L973 (7L) constraint -- envelope hook may ADD fields, never flip status or re-point the layer; in-body | 3 -- #: The canonical confirm gate for a heavy raster FETCHER (ADR 0273). A | `_router/transforms/join.py` (absent; the raise is a DELIBERATE refusal, not a stale link) | TRIM | The engine. Docstrings carry ADR 0273 / wave-7 / wave-2 / 'the twin' / VERDICT #1 / phase-2 throughout. |
| `trid3nt_server/tools/fetchers/_router/shape_classifier.py` | 88 | 4 / 75 / 4 | hist, attrib, examples | 0 | 5 -- Unified response-shape classifier (the NATE shape principle, one place | none | TRIM | Module docstring names 'the NATE shape principle' twice (person attribution) plus migration history. |
| `trid3nt_server/tools/fetchers/_router/spec.py` | 67 | 7 / 36 / 5 | hist | 0 | 0 | none | TRIM | 5 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/_router/transforms/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/_router/transforms/fan_out.py` | 73 | 4 / 25 / 3 | hist | 0 | 0 | none | TRIM | 3 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/_router/transforms/tiled_mosaic.py` | 145 | 6 / 27 / 3 | - | 0 | 0 | none | TRIM | 3 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/_router/transport/__init__.py` | 37 | 1 / 8 / 1 | spec | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap; spec notation in the docstrings/comments. |
| `trid3nt_server/tools/fetchers/_router/transport/client.py` | 183 | 9 / 52 / 5 | - | 0 | 0 | none | TRIM | `get_bytes`/`post_bytes` annotate `dict[str, Any]` without importing `Any`; safe only under `from __future__ import annotations`. |
| `trid3nt_server/tools/fetchers/_router/transport/errors.py` | 54 | 7 / 42 / 4 | hist, spec | 0 | 1 -- migration is additive/behavior-identical, never narrower than before. | none | TRIM | 4 docstrings over the physical-line cap; spec notation in the docstrings/comments; fold/twin history. |
| `trid3nt_server/tools/fetchers/_router/transport/opener.py` | 61 | 5 / 32 / 3 | - | 0 | 0 | none | TRIM | 3 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/_router/transport/range_file.py` | 125 | 6 / 36 / 2 | - | 0 | 0 | none | TRIM | 2 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/_router/transport/staged.py` | 18 | 4 / 25 / 3 | - | 0 | 0 | none | TRIM | 3 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/_router/transport/zip_object.py` | 9 | 2 / 24 / 2 | - | 0 | 0 | none | TRIM | 2 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/climate/__init__.py` | 0 | 1 / 1 / 0 | - | 0 | 0 | none | KEEP | Clean against the standard. |
| `trid3nt_server/tools/fetchers/climate/fetch_chirps_precipitation/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/climate/fetch_climate_normals/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/climate/fetch_climate_normals/hooks.py` | 149 | 7 / 19 / 1 | hist | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/climate/fetch_era5_reanalysis/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/climate/fetch_gridmet/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/climate/fetch_modis_lst/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/climate/fetch_us_drought_monitor/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/climate/lookup_precip_return_period/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/climate/lookup_precip_return_period/lookup_precip_return_period.py` | 350 | 10 / 141 / 9 | hist, spec, rationale, arch, examples, narrative | L54 (25L) narration -- dated live-probe log + kickoff inference + Tier 3 / §F.1.1 -- history, spec notation, dates; L229 (28L) constraint -- 'WHY THIS EXISTS' rationale essay wrapping a real constraint (Atlas 2 has no point CSV; the bundled parameterization is deterministic) | 3 -- # Access pattern tier -- LIVE-VERIFIED matches kickoff inference (2026 | `data_fetch.py` (absent); `server.py` (absent) | TRIM | The other coded `@register_tool` fetcher. Same unused-import set. Two module-level comment blocks (25L and 28L) are the largest narration in scope. |
| `trid3nt_server/tools/fetchers/hazard/__init__.py` | 0 | 1 / 2 / 0 | - | 0 | 0 | none | KEEP | Clean against the standard. |
| `trid3nt_server/tools/fetchers/hazard/fetch_epa_frs_facilities/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hazard/fetch_epa_frs_facilities/hooks.py` | 157 | 4 / 19 / 1 | hist, spec | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap; spec notation in the docstrings/comments; fold/twin history. |
| `trid3nt_server/tools/fetchers/hazard/fetch_fault_sources/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hazard/fetch_fault_sources/hooks.py` | 189 | 10 / 42 / 4 | hist | 0 | 0 | none | TRIM | 4 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/hazard/fetch_fema_nfhl_zones/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hazard/fetch_fema_nfhl_zones/hooks.py` | 163 | 7 / 51 / 1 | - | L81 (8L) constraint -- the measured service pacing that justifies the tile width and delay | 0 | none | TRIM | 1 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/hazard/fetch_firms_active_fire/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hazard/fetch_firms_active_fire/hooks.py` | 104 | 6 / 43 / 6 | hist | 0 | 0 | none | TRIM | 6 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/hazard/fetch_hifld_critical_infrastructure/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hazard/fetch_hifld_transmission_lines/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hazard/fetch_landfire_fuels/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hazard/fetch_mtbs_burn_severity/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hazard/fetch_nifc_fire_perimeters/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hazard/fetch_openfema_disasters/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hazard/fetch_openfema_disasters/hooks.py` | 349 | 7 / 33 / 3 | hist | 0 | 0 | `transforms/join.py` (absent) | TRIM | 3 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/hazard/fetch_tsunami_events/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hazard/fetch_tsunami_events/hooks.py` | 178 | 3 / 10 / 1 | - | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/hazard/fetch_usace_dams/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hazard/fetch_usace_dams/hooks.py` | 190 | 5 / 22 / 1 | hist, spec | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap; spec notation in the docstrings/comments; fold/twin history. |
| `trid3nt_server/tools/fetchers/hazard/fetch_usace_levees/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hazard/fetch_usfs_canopy_fuels/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hazard/fetch_usgs_earthquakes/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hazard/fetch_usgs_earthquakes/hooks.py` | 205 | 5 / 23 / 3 | - | 0 | 0 | none | TRIM | 3 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/hazard/fetch_usgs_volcano_alerts/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hazard/fetch_usgs_volcano_alerts/hooks.py` | 168 | 3 / 10 / 1 | - | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/hazard/fetch_wfigs_incident/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hazard/fetch_wfigs_incident/hooks.py` | 168 | 10 / 27 / 1 | hist | 0 | 1 -- The proof-by-migration for the record-return output shape. The source  | none | TRIM | Module docstring opens 'The proof-by-migration for the record-return output shape' -- fold history. |
| `trid3nt_server/tools/fetchers/hydrology/__init__.py` | 0 | 1 / 1 / 0 | - | 0 | 0 | none | KEEP | Clean against the standard. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_aquifer_thickness/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_aquifer_transmissivity/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_flood_extent_observation/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_flood_extent_observation/hooks.py` | 131 | 6 / 24 / 2 | - | 0 | 0 | none | TRIM | 2 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_groundwater_recharge/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_high_water_marks/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_high_water_marks/hooks.py` | 249 | 6 / 32 / 2 | hist | 0 | 0 | none | TRIM | 2 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_jrc_global_surface_water/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_jrc_global_surface_water/hooks.py` | 58 | 5 / 20 / 2 | hist | 0 | 0 | none | TRIM | 2 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_lter_records/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_lter_records/hooks.py` | 262 | 13 / 51 / 3 | examples | 0 | 0 | none | TRIM | 3 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nhd_area_water/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nhd_waterbodies/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nhdplus_hr_flowlines/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nhdplus_nldi_navigate/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_noaa_nwm_streamflow/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_noaa_nwm_streamflow/hooks.py` | 434 | 20 / 92 / 7 | hist | L466 (7L) constraint -- datetime64[ns].item() degrades to int -- the us round-trip; in-body, carries 'silently skipped this whole block before' | 1 -- #: USGS NLDI base (used to discover COMIDs + geometries for the bbox s | none | TRIM | 7 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nwi_wetlands/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nws_river_forecast/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nws_river_forecast/hooks.py` | 263 | 5 / 15 / 1 | - | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_opera_dswx/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_river_geometry/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_river_geometry/hooks.py` | 85 | 4 / 20 / 3 | - | 0 | 0 | none | TRIM | 3 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_usgs_groundwater_levels/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_usgs_groundwater_levels/hooks.py` | 203 | 6 / 17 / 1 | hist | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_usgs_nwis_gauges/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_usgs_nwis_gauges/hooks.py` | 289 | 8 / 41 / 3 | hist | 0 | 0 | none | TRIM | 3 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_usgs_water_quality/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_water_table_depth/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/imagery/__init__.py` | 0 | 1 / 1 / 0 | - | 0 | 0 | none | KEEP | Clean against the standard. |
| `trid3nt_server/tools/fetchers/imagery/_goes_archive_core.py` | 685 | 32 / 276 / 26 | hist, spec, examples | L155 (38L) constraint -- the fire-detection threshold derivation (Matson & Dozier, MODIS C5 heritage); names 'the kickoff'; L891 (7L) constraint -- per-band valid DN range differs 14-bit vs 12-bit; in-body | 0 | none | TRIM | `read_through` imported at line 18 and unused. `_PRODUCT_LABEL` (line 257) superseded by `_PRODUCT_LABELS`. Lines 303-309 are an empty 'AtomicToolMetadata' section header. |
| `trid3nt_server/tools/fetchers/imagery/_goes_common.py` | 141 | 10 / 68 / 7 | hist, spec | L104 (16L) constraint -- bucket token glues the digits (noaa-goes18, not noaa-goes-18); tail is GOES-swap history with dates | 4 -- # East/West -> bird mapping (current as of the 2025-04-07 NOAA GOES-Ea | none | TRIM | GOES East/West swap dates in the block are a real data-vintage constraint (which bucket still gains frames), not narration -- keep the fact, drop the schedule history. |
| `trid3nt_server/tools/fetchers/imagery/_satellite_slider.py` | 467 | 17 / 143 / 10 | - | L146 (7L) constraint -- sector envelopes are approximate; LIVE-VERIFY at the limb; L699 (20L) constraint -- why a masked alpha overlay and not a max blend, and how the fire mask is formed | 1 -- # Constants (confirmed from the SLIDER-cli source + live probes 2026-0 | none | TRIM | 10 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/imagery/fetch_goes_active_fire/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/imagery/fetch_goes_animation/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/imagery/fetch_goes_archive_animation/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/imagery/fetch_goes_blend_animation/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/imagery/fetch_goes_satellite/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/imagery/fetch_goes_satellite/hooks.py` | 307 | 11 / 49 / 3 | hist | 0 | 0 | none | TRIM | 3 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/imagery/fetch_landsat_imagery/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/imagery/fetch_naip/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/imagery/fetch_sentinel1_sar/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/imagery/fetch_sentinel2_truecolor/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/imagery/fetch_slider_timestamps/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/imagery/fetch_slider_timestamps/hooks.py` | 59 | 4 / 19 / 2 | hist | 0 | 0 | none | TRIM | 2 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/imagery/fetch_viirs_day_fire/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/imagery/fetch_viirs_day_fire/hooks.py` | 183 | 8 / 21 / 1 | - | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/ocean/__init__.py` | 0 | 1 / 1 / 0 | - | 0 | 0 | none | KEEP | Clean against the standard. |
| `trid3nt_server/tools/fetchers/ocean/fetch_bluetopo/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/ocean/fetch_bluetopo/hooks.py` | 258 | 14 / 69 / 6 | spec | 0 | 0 | none | TRIM | 6 docstrings over the physical-line cap; spec notation in the docstrings/comments. |
| `trid3nt_server/tools/fetchers/ocean/fetch_greatlakes_bathymetry/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/ocean/fetch_gtsm_tide_surge/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_coops_currents/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_coops_tides/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_slr_confidence/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_slr_marsh/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_slr_scenarios/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_sst/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/ocean/fetch_osm_breakwaters/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/ocean/fetch_osm_breakwaters/hooks.py` | 64 | 5 / 19 / 2 | - | 0 | 0 | none | TRIM | 2 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/ocean/fetch_osm_coastline/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/ocean/fetch_osm_coastline/hooks.py` | 32 | 3 / 21 / 3 | - | 0 | 0 | none | TRIM | 3 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/ocean/fetch_topobathy/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/socioeconomic/__init__.py` | 0 | 1 / 1 / 0 | - | 0 | 0 | none | KEEP | Clean against the standard. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_administrative_boundaries/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_administrative_boundaries/hooks.py` | 68 | 3 / 11 / 1 | - | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_buildings/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_buildings/hooks.py` | 34 | 3 / 17 / 2 | - | 0 | 0 | none | TRIM | 2 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_field_boundaries/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_field_boundaries/hooks.py` | 142 | 4 / 32 / 3 | hist | 0 | 0 | none | TRIM | 3 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_ghsl_population/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_hrsl_population/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_overpass_pois/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_overpass_pois/hooks.py` | 113 | 7 / 27 / 3 | - | 0 | 0 | none | TRIM | 3 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_population/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_population/hooks.py` | 145 | 6 / 50 / 5 | hist, attrib | 0 | 1 -- NATE flag-not-copy): it was half-built (geometry=None follow-up + heur | none | TRIM | Module docstring carries 'NATE flag-not-copy' -- person attribution. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_roads_osm/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_roads_osm/hooks.py` | 62 | 4 / 14 / 2 | - | 0 | 0 | none | TRIM | 2 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_usace_nsi/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_usace_nsi/hooks.py` | 100 | 4 / 20 / 1 | hist | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/socioeconomic/geocode_location/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/socioeconomic/geocode_location/geocode_location.py` | 421 | 13 / 221 / 11 | hist, spec, attrib, arch, examples, narrative | L56 (12L) narration -- 'NATE directive 2026-06-17' -- person attribution + dated history + 'v2 -- NOT now'; L180 (12L) narration -- 'an earlier F71 attempt ... It was REVERTED' -- pure history + spec notation; in-body; L219 (12L) constraint -- the 2-letter false-match guard ('in'->IN, 'or'->OR); in-body; L243 (7L) constraint -- offline backstop provenance + the round-outward rule; L407 (21L) constraint -- the result-class preference + minimum-AOI-floor contract; L790 (7L) constraint -- the state-snap fallback branch and what it must not swallow; in-body | 1 -- # State-snap fallback (NATE directive 2026-06-17): a vague/regional qu | `server.py` (absent; the emission seam is `server/dispatch/emitter.py`) | TRIM | One of the two remaining coded `@register_tool` fetchers. LLM docstring is ~110 lines / ~4700 chars against a 1000-char front budget. Unused imports: io, tempfile, time, Callable, LayerURI, FetchError. |
| `trid3nt_server/tools/fetchers/soil/__init__.py` | 0 | 1 / 1 / 0 | - | 0 | 0 | none | KEEP | Clean against the standard. |
| `trid3nt_server/tools/fetchers/soil/fetch_gcn250_curve_numbers/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/soil/fetch_snotel_snow/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/soil/fetch_snotel_snow/hooks.py` | 157 | 6 / 19 / 1 | - | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/soil/fetch_soilgrids/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/soil/fetch_statsgo_soils/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/terrain/__init__.py` | 0 | 1 / 1 / 0 | - | 0 | 0 | none | KEEP | Clean against the standard. |
| `trid3nt_server/tools/fetchers/terrain/fetch_3dep_extra/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/terrain/fetch_copernicus_dem/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/terrain/fetch_dem/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/terrain/fetch_dem/hooks.py` | 277 | 15 / 136 / 11 | hist | 0 | 0 | none | TRIM | 11 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/terrain/fetch_esri_landcover_10m/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/terrain/fetch_landcover/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/terrain/fetch_landcover/hooks.py` | 87 | 3 / 29 / 2 | hist | 0 | 0 | none | TRIM | 2 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/us_states.py` | 52 | 3 / 37 / 3 | - | 0 | 0 | none | TRIM | 3 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/weather/__init__.py` | 0 | 1 / 1 / 0 | - | 0 | 0 | none | KEEP | Clean against the standard. |
| `trid3nt_server/tools/fetchers/weather/fetch_airnow_air_quality/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/weather/fetch_airnow_air_quality/hooks.py` | 130 | 4 / 20 / 1 | hist | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/weather/fetch_aorc_precip/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/weather/fetch_aorc_precip/hooks.py` | 126 | 4 / 42 / 4 | - | 0 | 0 | none | TRIM | 4 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/weather/fetch_asos_metar/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/weather/fetch_asos_metar/hooks.py` | 223 | 6 / 19 / 1 | hist | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/weather/fetch_glm_lightning/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/weather/fetch_glm_lightning/hooks.py` | 305 | 17 / 68 / 6 | hist | 0 | 0 | none | TRIM | 6 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/weather/fetch_hrrr_forecast/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/weather/fetch_hrrr_smoke/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/weather/fetch_mrms_qpe/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/weather/fetch_mrms_qpe/hooks.py` | 103 | 6 / 18 / 1 | hist | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/weather/fetch_nldas2_forcing/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/weather/fetch_nldas2_forcing/hooks.py` | 98 | 5 / 27 / 2 | - | 0 | 0 | none | TRIM | 2 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/weather/fetch_nws_alerts_conus/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/weather/fetch_nws_alerts_conus/hooks.py` | 180 | 7 / 18 / 1 | hist | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/weather/fetch_nws_event/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/weather/fetch_nws_event/hooks.py` | 86 | 4 / 25 / 2 | hist | 0 | 0 | none | TRIM | 2 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/weather/fetch_openaq_measurements/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/weather/fetch_openaq_measurements/hooks.py` | 176 | 7 / 26 / 1 | hist | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/weather/fetch_raws_weather/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/weather/fetch_raws_weather/hooks.py` | 218 | 8 / 22 / 1 | hist | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap; fold/twin history. |
| `trid3nt_server/tools/fetchers/weather/fetch_storm_events_db/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/weather/fetch_storm_events_db/hooks.py` | 191 | 6 / 18 / 1 | - | 0 | 0 | none | TRIM | 1 docstrings over the physical-line cap. |
| `trid3nt_server/tools/fetchers/weather/fetch_storm_tracks/__init__.py` | 0 | 0 / 0 / 0 | - | 0 | 0 | none | KEEP | 0-byte package marker; nothing to trim. |
| `trid3nt_server/tools/fetchers/weather/fetch_storm_tracks/hooks.py` | 739 | 22 / 64 / 2 | hist | 0 | 0 | none | TRIM | 2 docstrings over the physical-line cap; fold/twin history. |

## B. `source.yaml` spec declarations (99)

These are declarations, not code, so pure LOC is `n/a`. The `docstring:` block is the LLM-facing
surface and is measured against the 1000-char front budget; the `#` header is a comment block.

| path | lines | LLM docstring chars (budget 1000) | disallowed classes in the docstring | header comment block | history markers | dead or moved references | fate | notes |
|---|---|---|---|---|---|---|---|---|
| `trid3nt_server/tools/fetchers/climate/fetch_chirps_precipitation/source.yaml` | 136 | 3455 (OVER) | arch, examples, narrative | 4L narration | 2 -- # Router source spec (fetcher-fold wave-9, gzip_object whole | deleted twin: `fetch_chirps_precipitation.py` | TRIM | Header banner names the deleted twin; docstring is 3455 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/climate/fetch_climate_normals/source.yaml` | 134 | 3706 (OVER) | arch, narrative, rollcall | 4L narration | 3 -- # Router source spec (keyed/misc-leftovers wave, chained_res | deleted twin: `fetch_climate_normals.py` | TRIM | Header banner names the deleted twin; docstring is 3706 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/climate/fetch_era5_reanalysis/source.yaml` | 196 | 4884 (OVER) | spec, arch, narrative | 4L narration | 3 -- # Router source spec (ADR 0085) -- Copernicus ERA5 global re | deleted twin: `fetch_era5_reanalysis.py` | TRIM | Header banner names the deleted twin; docstring is 4884 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/climate/fetch_gridmet/source.yaml` | 181 | 3615 (OVER) | spec, arch, examples, narrative | 3L narration | 7 -- # Twin: fetch_gridmet/fetch_gridmet.py (SPEC-EXPRESSIBLE, ra | deleted twin: `fetch_gridmet/fetch_gridmet.py` | TRIM | Header banner names the deleted twin; docstring is 3615 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/climate/fetch_modis_lst/source.yaml` | 154 | 2944 (OVER) | spec, narrative | 2L constraint | 0 | none | TRIM | Header comment carries wave/adr spec notation; docstring is 2944 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/climate/fetch_us_drought_monitor/source.yaml` | 133 | 3347 (OVER) | arch, narrative, rollcall | 5L narration | 3 -- # Router source spec (data-router fold, phase-2 wave-2 ArcGI | deleted twin: `fetch_us_drought_monitor.py` | TRIM | Header banner names the deleted twin; docstring is 3347 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hazard/fetch_epa_frs_facilities/source.yaml` | 91 | 2745 (OVER) | narrative | 0 | 0 | none | TRIM | Docstring is 2745 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hazard/fetch_fault_sources/source.yaml` | 94 | 2939 (OVER) | narrative | 5L narration | 2 -- # Router source spec (finisher-mechanisms wave, ADR 0081). | deleted twin: `fetch_fault_sources.py` | TRIM | Header banner names the deleted twin; docstring is 2939 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hazard/fetch_fema_nfhl_zones/source.yaml` | 164 | 4780 (OVER) | spec, arch, examples, narrative | 0 | 0 | none | TRIM | Docstring is 4780 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hazard/fetch_firms_active_fire/source.yaml` | 147 | 3050 (OVER) | arch, examples, narrative | 6L narration | 2 -- # Router source spec (quick-folds wave, keyed CSV http_json, | deleted twin: `fetch_firms_active_fire.py` | TRIM | Header banner names the deleted twin; docstring is 3050 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hazard/fetch_hifld_critical_infrastructure/source.yaml` | 135 | 2346 (OVER) | narrative | 3L narration | 6 -- # Twin: fetch_hifld_critical_infrastructure.py (SPEC-EXPRESS | deleted twin: `fetch_hifld_critical_infrastructure.py` | TRIM | header comment cites a bare 'VERDICT.md' with no path; no such file in the tree. |
| `trid3nt_server/tools/fetchers/hazard/fetch_hifld_transmission_lines/source.yaml` | 117 | 2785 (OVER) | narrative | 3L narration | 4 -- # Router source spec (data-router fold, phase-2 wave-2 ArcGI | deleted twin: `fetch_hifld_transmission_lines.py` | TRIM | Header banner names the deleted twin; docstring is 2785 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hazard/fetch_landfire_fuels/source.yaml` | 149 | 3496 (OVER) | spec, arch, examples, narrative | 3L narration | 2 -- # Router source spec (fetcher-fold wave-7, imageserver_expor | deleted twin: `fetch_landfire_fuels.py` | TRIM | Header banner names the deleted twin; docstring is 3496 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hazard/fetch_mtbs_burn_severity/source.yaml` | 109 | 2433 (OVER) | arch, examples, narrative | 3L narration | 3 -- # Router source spec (data-router fold, phase-2 wave-2 ArcGI | deleted twin: `fetch_mtbs_burn_severity.py` | TRIM | Header banner names the deleted twin; docstring is 2433 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hazard/fetch_nifc_fire_perimeters/source.yaml` | 105 | 2375 (OVER) | spec, arch, examples, narrative | 4L narration | 2 -- # Router source spec (data-router fold, phase-2 wave-2 ArcGI | deleted twin: `fetch_nifc_fire_perimeters.py` | TRIM | Header banner names the deleted twin; docstring is 2375 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hazard/fetch_openfema_disasters/source.yaml` | 175 | 5357 (OVER) | arch, examples, narrative | 3L narration | 2 -- # Router source spec (data-router fold, chained-resolution m | deleted twin: `fetch_openfema_disasters.py` | TRIM | Header banner names the deleted twin; docstring is 5357 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hazard/fetch_tsunami_events/source.yaml` | 140 | 5734 (OVER) | arch, examples, narrative | 4L narration | 2 -- # Router source spec (data-router fold, phase-2 wave-10 tier | deleted twin: `fetch_tsunami_events.py` | TRIM | Header banner names the deleted twin; docstring is 5734 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hazard/fetch_usace_dams/source.yaml` | 223 | 5859 (OVER) | spec, arch, examples, narrative | 0 | 0 | none | TRIM | Docstring is 5859 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hazard/fetch_usace_levees/source.yaml` | 141 | 2850 (OVER) | spec, arch, examples, narrative | 5L narration | 2 -- # Router source spec (data-router fold, phase-2 wave-6 VECTO | deleted twin: `fetch_usace_levees.py` | TRIM | Header banner names the deleted twin; docstring is 2850 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hazard/fetch_usfs_canopy_fuels/source.yaml` | 156 | 4317 (OVER) | spec, arch, narrative | 3L narration | 2 -- # Router source spec (fetcher-fold wave-7, imageserver_expor | deleted twin: `fetch_usfs_canopy_fuels.py` | TRIM | Header banner names the deleted twin; docstring is 4317 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hazard/fetch_usgs_earthquakes/source.yaml` | 134 | 5802 (OVER) | arch, examples, narrative | 4L narration | 2 -- # Router source spec (data-router fold, phase-2 wave-10 tier | deleted twin: `fetch_usgs_earthquakes.py` | TRIM | Header banner names the deleted twin; docstring is 5802 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hazard/fetch_usgs_volcano_alerts/source.yaml` | 117 | 5273 (OVER) | arch, examples, narrative | 4L narration | 2 -- # Router source spec (data-router fold, phase-2 wave-10 tier | deleted twin: `fetch_usgs_volcano_alerts.py` | TRIM | Header banner names the deleted twin; docstring is 5273 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hazard/fetch_wfigs_incident/source.yaml` | 102 | 2727 (OVER) | arch, examples, narrative | 0 | 1 -- # not a bulk payload; the twin registered no estimator. A ti | none | TRIM | Docstring is 2727 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_aquifer_thickness/source.yaml` | 135 | 3191 (OVER) | arch, narrative | 4L narration | 1 -- # conductivity. See scripts/stage_zell_sanford_groundwater.p | none | TRIM | Header comment carries wave/adr spec notation; docstring is 3191 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_aquifer_transmissivity/source.yaml` | 140 | 3278 (OVER) | arch, narrative | 8L narration (fold/wave/ADR banner + deleted-twin recap) | 2 -- # at read time. ADR 0298 Decision 7 parked this spec (Normal | none | TRIM | header comment carries 'ADR 0298 Decision 7 parked this spec' plus a NATE attribution. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_flood_extent_observation/source.yaml` | 116 | 2457 (OVER) | spec, arch, narrative | 5L narration | 2 -- # Router source spec (landcover + flood-extent wave, ADR 008 | deleted twin: `fetch_flood_extent_observation.py` | TRIM | Carries an INLINE corpus: block duplicating the sibling corpus.yaml byte-for-byte. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_groundwater_recharge/source.yaml` | 140 | 3301 (OVER) | arch, narrative | 4L narration | 0 | none | TRIM | Header comment carries wave/adr spec notation; docstring is 3301 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_high_water_marks/source.yaml` | 109 | 2868 (OVER) | spec, arch, examples, narrative | 0 | 0 | none | TRIM | Docstring is 2868 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_jrc_global_surface_water/source.yaml` | 152 | 2950 (OVER) | spec, narrative | 4L constraint | 0 | none | TRIM | Header comment carries wave/adr spec notation; docstring is 2950 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_lter_records/source.yaml` | 126 | 2844 (OVER) | arch, examples, narrative | 5L narration | 2 -- # Router source spec (ADR 0203) -- LTER / EDI environmental  | none | TRIM | Header comment carries wave/adr spec notation; docstring is 2844 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nhd_area_water/source.yaml` | 115 | 1965 (OVER) | narrative | 4L constraint | 1 -- # a river rather than its centreline. The TELEMAC reach mesh | none | TRIM | Header comment carries wave/adr spec notation; docstring is 1965 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nhd_waterbodies/source.yaml` | 122 | 2717 (OVER) | arch, narrative | 5L narration | 2 -- # Router source spec (data-router fold, phase-2 wave-2 ArcGI | deleted twin: `fetch_nhd_waterbodies.py` | TRIM | line 118 'Resilience (feedback_data_source_fallback_norm):' is a MEMORY FILENAME -- an explicitly disallowed class. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nhdplus_hr_flowlines/source.yaml` | 136 | 2561 (OVER) | examples, narrative, rollcall | 4L constraint | 1 -- # family used to run this query from inside the solver conta | none | TRIM | Header comment carries wave/adr spec notation; docstring is 2561 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nhdplus_nldi_navigate/source.yaml` | 165 | 5017 (OVER) | arch, examples, narrative | 5L narration | 2 -- # Router source spec (fetcher fold phase-2 wave-3, ADR 0040) | deleted twin: `fetch_nhdplus_nldi_navigate.py` | TRIM | Header banner names the deleted twin; docstring is 5017 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_noaa_nwm_streamflow/source.yaml` | 188 | 4541 (OVER) | arch, examples, narrative, rollcall | 13L narration (fold/wave/ADR banner + deleted-twin recap) | 6 -- # Router source spec (FETCHER FINALE ENDGAME, ADR 0112): the | deleted twin: `fetch_noaa_nwm_streamflow.py` | TRIM | Carries an INLINE corpus: block that has DRIFTED from the sibling corpus.yaml. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nwi_wetlands/source.yaml` | 84 | 2577 (OVER) | arch, narrative | 0 | 0 | none | TRIM | Docstring is 2577 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nws_river_forecast/source.yaml` | 117 | 2808 (OVER) | examples | 2L narration | 2 -- # Router source spec (data-router fold, chained-resolution m | deleted twin: `fetch_nws_river_forecast.py` | TRIM | Header banner names the deleted twin; docstring is 2808 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_opera_dswx/source.yaml` | 140 | 1756 (OVER) | narrative | 7L narration (fold/wave/ADR banner + deleted-twin recap) | 0 | none | TRIM | Header comment carries wave/adr spec notation; docstring is 1756 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_river_geometry/source.yaml` | 130 | 3619 (OVER) | hist, spec, arch, examples, narrative, rollcall | 0 | 0 | none | TRIM | docstring twice states a leg 'was removed in ADR 0074' -- history plus spec notation inside the LLM-facing surface. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_usgs_groundwater_levels/source.yaml` | 164 | 5175 (OVER) | arch, examples, narrative | 5L narration | 2 -- # Router source spec (keyed/misc-leftovers wave, chained_res | deleted twin: `fetch_usgs_groundwater_levels.py` | TRIM | Header banner names the deleted twin; docstring is 5175 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_usgs_nwis_gauges/source.yaml` | 204 | 6849 (OVER) | arch, examples, narrative, rollcall | 4L narration | 4 -- # Router source spec (ADR 0085) -- USGS NWIS / Water Service | deleted twin: `fetch_usgs_nwis_gauges.py` | TRIM | Header banner names the deleted twin; docstring is 6849 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_usgs_water_quality/source.yaml` | 199 | 5265 (OVER) | arch, examples, narrative | 5L narration | 2 -- # Router source spec (fetcher fold phase-2 wave-3, ADR 0040) | deleted twin: `fetch_usgs_water_quality.py` | TRIM | Header banner names the deleted twin; docstring is 5265 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/hydrology/fetch_water_table_depth/source.yaml` | 142 | 3414 (OVER) | arch, narrative | 5L narration | 0 | none | TRIM | Header comment carries wave/adr spec notation; docstring is 3414 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/imagery/fetch_goes_active_fire/source.yaml` | 123 | 2886 (OVER) | arch, narrative | 0 | 1 -- # netcdf_cf_object per-frame mode config (ADR 0088): the spl | none | TRIM | Docstring is 2886 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/imagery/fetch_goes_animation/source.yaml` | 137 | 3873 (OVER) | arch, examples, narrative | 0 | 1 -- # The twin registered no payload estimator (a list-return to | none | TRIM | Docstring is 3873 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/imagery/fetch_goes_archive_animation/source.yaml` | 188 | 6261 (OVER) | arch, narrative | 0 | 2 -- # netcdf_cf_object per-frame mode config (ADR 0088): the ban | none | TRIM | Docstring is 6261 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/imagery/fetch_goes_blend_animation/source.yaml` | 75 | 665 | hist | 0 | 0 | none | DELETE candidate -- self-declared DEPRECATED alias of fetch_goes_animation | docstring line 10 self-declares 'DEPRECATED alias of ``fetch_goes_animation`` with band="blend"' yet the tool keeps a full 9-phrasing corpus.yaml. Strongest DELETE candidate in scope. |
| `trid3nt_server/tools/fetchers/imagery/fetch_goes_satellite/source.yaml` | 83 | 3721 (OVER) | spec, arch, narrative | 7L narration (fold/wave/ADR banner + deleted-twin recap) | 3 -- # Router source spec (FETCHER FINALE WAVE 2, ADR 0111): the  | deleted twin: `fetch_goes_satellite.py` | TRIM | Carries an INLINE corpus: block duplicating the sibling corpus.yaml byte-for-byte. |
| `trid3nt_server/tools/fetchers/imagery/fetch_landsat_imagery/source.yaml` | 264 | 3566 (OVER) | hist, spec, examples, narrative | 5L constraint | 0 | none | TRIM | Header comment carries wave/adr spec notation; docstring is 3566 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/imagery/fetch_naip/source.yaml` | 109 | 1659 (OVER) | spec, narrative | 4L constraint | 0 | none | TRIM | Header comment carries wave/adr spec notation; docstring is 1659 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/imagery/fetch_sentinel1_sar/source.yaml` | 166 | 3073 (OVER) | spec, narrative | 5L constraint | 0 | none | TRIM | Header comment carries wave/adr spec notation; docstring is 3073 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/imagery/fetch_sentinel2_truecolor/source.yaml` | 148 | 2489 (OVER) | spec, narrative | 5L constraint | 0 | none | TRIM | Header comment carries wave/adr spec notation; docstring is 2489 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/imagery/fetch_slider_timestamps/source.yaml` | 99 | 1870 (OVER) | examples, narrative | 0 | 1 -- # bulk payload; the twin registered no estimator (cacheable= | none | TRIM | Docstring is 1870 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/imagery/fetch_viirs_day_fire/source.yaml` | 128 | 3510 (OVER) | arch, examples, narrative | 0 | 0 | none | TRIM | Docstring is 3510 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/ocean/fetch_bluetopo/source.yaml` | 164 | 3904 (OVER) | arch, narrative | 0 | 0 | none | TRIM | Docstring is 3904 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/ocean/fetch_greatlakes_bathymetry/source.yaml` | 140 | 2332 (OVER) | narrative | 0 | 0 | none | TRIM | Docstring is 2332 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/ocean/fetch_greatlakes_water_level/source.yaml` | 135 | 2563 (OVER) | narrative | 0 | 0 | none | TRIM | Docstring is 2563 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/ocean/fetch_gtsm_tide_surge/source.yaml` | 139 | 3753 (OVER) | spec, arch, examples, narrative, rollcall | 4L narration | 4 -- # Router source spec (ADR 0085) -- Deltares GTSM v3.0 global | deleted twin: `fetch_gtsm_tide_surge.py` | TRIM | Header banner names the deleted twin; docstring is 3753 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_coops_currents/source.yaml` | 158 | 2963 (OVER) | arch, narrative | 7L narration (fold/wave/ADR banner + deleted-twin recap) | 6 -- # Router source spec (data-router fold, phase-2 wave-4 stati | deleted twin: `fetch_noaa_coops_currents.py` | TRIM | Header banner names the deleted twin; docstring is 2963 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_coops_tides/source.yaml` | 188 | 5144 (OVER) | arch, examples, narrative | 3L narration | 3 -- # Twin: fetch_noaa_coops_tides.py (SPEC-EXPRESSIBLE, station | deleted twin: `fetch_noaa_coops_tides.py` | TRIM | Header banner names the deleted twin; docstring is 5144 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_slr_confidence/source.yaml` | 118 | 1935 (OVER) | hist, arch, narrative | 5L narration | 3 -- # Router source spec (raster-stragglers wave, mapserver_expo | deleted twin: `_noaa_slr_raster.py`, `fetch_noaa_slr_confidence.py` | TRIM | Header banner names the deleted twin; docstring is 1935 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_slr_marsh/source.yaml` | 127 | 1866 (OVER) | hist, arch, narrative | 5L narration | 3 -- # Router source spec (raster-stragglers wave, mapserver_expo | deleted twin: `_noaa_slr_raster.py`, `fetch_noaa_slr_marsh.py` | TRIM | Header banner names the deleted twin; docstring is 1866 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_slr_scenarios/source.yaml` | 164 | 3961 (OVER) | hist, arch, examples, narrative, rollcall | 5L narration | 2 -- # Router source spec (data-router fold, phase-2 wave-6 VECTO | deleted twin: `fetch_noaa_slr_scenarios.py` | TRIM | docstring calls fetch_noaa_slr_marsh 'a separate tool (not yet implemented)'; it IS implemented in this same scope. STALE. |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_sst/source.yaml` | 123 | 2415 (OVER) | spec, narrative | 7L narration (fold/wave/ADR banner + deleted-twin recap) | 3 -- # Router source spec (quick-folds wave, raster-cog griddap a | deleted twin: `fetch_noaa_sst.py` | TRIM | Header banner names the deleted twin; docstring is 2415 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/ocean/fetch_osm_breakwaters/source.yaml` | 82 | 2539 (OVER) | arch, narrative | 0 | 0 | none | TRIM | Docstring is 2539 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/ocean/fetch_osm_coastline/source.yaml` | 74 | 2166 (OVER) | arch, narrative | 0 | 0 | none | TRIM | Docstring is 2166 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/ocean/fetch_topobathy/source.yaml` | 256 | 8171 (OVER) | hist, spec, arch, narrative | 0 | 2 -- # ADR 0273 -- declared confirm gate: the heavy raster fetch  | none | TRIM | Docstring is 8171 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_administrative_boundaries/source.yaml` | 80 | 2218 (OVER) | arch, examples, narrative | 0 | 0 | none | TRIM | Docstring is 2218 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_buildings/source.yaml` | 91 | 2469 (OVER) | spec, narrative | 3L narration | 1 -- # Router source spec (trigger wave, ADR 0084) -- OSM buildin | none | TRIM | docstring still sells `source="msft"` as a real fallback; caveat line 62 says the msft leg is dead and resolves to OSM. Internally contradictory. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_field_boundaries/source.yaml` | 110 | 3069 (OVER) | spec, arch, narrative | 0 | 0 | none | TRIM | Docstring is 3069 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_ghsl_population/source.yaml` | 122 | 2838 (OVER) | arch, examples, narrative | 0 | 0 | none | TRIM | Docstring is 2838 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_hrsl_population/source.yaml` | 113 | 2510 (OVER) | arch, examples, narrative | 4L narration | 2 -- # Router source spec (fetcher-fold wave-9, multi_url VRT fan | deleted twin: `fetch_hrsl_population.py` | TRIM | Header banner names the deleted twin; docstring is 2510 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_overpass_pois/source.yaml` | 115 | 4724 (OVER) | spec, arch, examples, narrative, rollcall | 0 | 0 | none | TRIM | Docstring is 4724 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_population/source.yaml` | 122 | 2224 (OVER) | arch, narrative | 7L narration (fold/wave/ADR banner + deleted-twin recap) | 3 -- # Router source spec (fetcher-fold, ADR 0092): WorldPop whol | deleted twin: `fetch_population.py` | TRIM | Header banner names the deleted twin; docstring is 2224 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_roads_osm/source.yaml` | 95 | 3114 (OVER) | spec, arch, narrative | 0 | 0 | none | TRIM | Docstring is 3114 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_usace_nsi/source.yaml` | 187 | 6003 (OVER) | spec, arch, examples, narrative, rollcall | 5L narration | 2 -- # Router source spec (data-router fold, tier-3 hooks, ADR 00 | deleted twin: `fetch_usace_nsi.py` | TRIM | docstring points readers at `estimate_payload_mb`, a function of the deleted twin; the estimate is now the spec's payload_estimate block. STALE. |
| `trid3nt_server/tools/fetchers/soil/fetch_gcn250_curve_numbers/source.yaml` | 132 | 3302 (OVER) | spec, arch, narrative | 5L narration | 2 -- # Router source spec (fetcher-fold wave-8, direct_window + s | deleted twin: `fetch_gcn250_curve_numbers.py` | TRIM | Header banner names the deleted twin; docstring is 3302 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/soil/fetch_snotel_snow/source.yaml` | 108 | 4324 (OVER) | arch, narrative, rollcall | 3L narration | 2 -- # Router source spec (batched-snapshot fold, ADR 0065). | deleted twin: `fetch_snotel_snow.py` | TRIM | Header banner names the deleted twin; docstring is 4324 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/soil/fetch_soilgrids/source.yaml` | 164 | 3081 (OVER) | spec, narrative | 6L narration | 3 -- # Router source spec (raster-modes wave, projected_vrt_windo | deleted twin: `fetch_soilgrids.py` | TRIM | Header banner names the deleted twin; docstring is 3081 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/soil/fetch_statsgo_soils/source.yaml` | 99 | 4598 (OVER) | spec, arch, narrative | 0 | 0 | none | TRIM | docstring documents a `timeout_s` parameter the spec's `params:` block does not declare; also 'SSURGO (no atomic tool yet; add in a future job)'. STALE + spec notation. |
| `trid3nt_server/tools/fetchers/terrain/fetch_3dep_extra/source.yaml` | 117 | 4666 (OVER) | arch, narrative | 0 | 0 | none | TRIM | docstring documents a `timeout_s` parameter the spec does not declare, and names `SUPPORTED_RESOLUTIONS`, a constant of the deleted twin. STALE. |
| `trid3nt_server/tools/fetchers/terrain/fetch_copernicus_dem/source.yaml` | 94 | 699 | - | 6L constraint | 1 -- # corpus re-homed onto fetch_dem (wave-11): this internal se | none | TRIM | internal_only seam; corpus deliberately re-homed onto fetch_dem, so it correctly has no corpus.yaml. |
| `trid3nt_server/tools/fetchers/terrain/fetch_dem/source.yaml` | 166 | 4618 (OVER) | arch, narrative | 0 | 2 -- # ADR 0273 -- declared confirm gate: the heavy raster fetch  | none | TRIM | Docstring is 4618 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/terrain/fetch_esri_landcover_10m/source.yaml` | 163 | 3322 (OVER) | spec, narrative | 4L constraint | 3 -- # year out of [2017,2023] -> ESRI_LANDCOVER_YEAR_INVALID (tw | none | TRIM | Header comment carries wave/adr spec notation; docstring is 3322 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/terrain/fetch_landcover/source.yaml` | 154 | 3606 (OVER) | spec, arch, examples, narrative | 6L narration | 3 -- # Router source spec (landcover + flood-extent wave, ADR 008 | deleted twin: `fetch_landcover.py`, `flood.py` | TRIM | cites '§F.1.1'; says extract_landcover_class works 'once it lands' (it landed -- tools/processing/extract_landcover_class); the ESA WorldCover branch is documented as 'forward-looking' and raising. Carries an INLINE corpus: block that has DRIFTED from the sibling corpus.yaml. |
| `trid3nt_server/tools/fetchers/weather/fetch_airnow_air_quality/source.yaml` | 132 | 4926 (OVER) | spec, arch, examples, narrative | 3L narration | 2 -- # Router source spec (keyed http_json fold, ADR 0065). | deleted twin: `fetch_airnow_air_quality.py` | TRIM | Header banner names the deleted twin; docstring is 4926 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/weather/fetch_aorc_precip/source.yaml` | 129 | 3105 (OVER) | arch, narrative | 5L narration | 1 -- # Router source spec (ADR 0203) -- NOAA AORC v1.1 hourly pre | none | TRIM | Header comment carries wave/adr spec notation; docstring is 3105 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/weather/fetch_asos_metar/source.yaml` | 122 | 4750 (OVER) | spec, arch, examples, narrative, rollcall | 3L narration | 2 -- # Router source spec (station-observations fold, ADR 0065). | deleted twin: `fetch_asos_metar.py` | TRIM | docstring names `_MAX_STATIONS` (100), a constant of the deleted twin. STALE. |
| `trid3nt_server/tools/fetchers/weather/fetch_glm_lightning/source.yaml` | 130 | 3051 (OVER) | arch, examples, narrative | 7L narration (fold/wave/ADR banner + deleted-twin recap) | 4 -- # Router source spec (fetcher-fold, ADR 0092): GOES GLM grou | deleted twin: `fetch_glm_lightning.py` | TRIM | Header banner names the deleted twin; docstring is 3051 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/weather/fetch_hrrr_forecast/source.yaml` | 182 | 5693 (OVER) | spec, arch, examples, narrative | 0 | 0 | none | TRIM | Docstring is 5693 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/weather/fetch_hrrr_smoke/source.yaml` | 184 | 6004 (OVER) | spec, arch, examples, narrative | 0 | 0 | none | TRIM | docstring names 'EPA AirNow (future tool)' and 'NASA MODIS AOD (future)'; fetch_airnow_air_quality exists in this scope. STALE. |
| `trid3nt_server/tools/fetchers/weather/fetch_mrms_qpe/source.yaml` | 127 | 4240 (OVER) | arch, examples, narrative | 3L narration | 1 -- # S3-listed key resolve phase, ADR 0069). Twin: fetch_mrms_q | deleted twin: `fetch_mrms_qpe.py` | TRIM | docstring ends 'Payload estimate: estimate_payload_mb(bbox, accumulation)' -- deleted-twin function. Also 'Case 3' x4 (spec notation). |
| `trid3nt_server/tools/fetchers/weather/fetch_nldas2_forcing/source.yaml` | 136 | 2699 (OVER) | arch, narrative | 7L narration (fold/wave/ADR banner + deleted-twin recap) | 0 | none | TRIM | Header comment carries wave/adr spec notation; docstring is 2699 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/weather/fetch_nws_alerts_conus/source.yaml` | 123 | 3967 (OVER) | spec, arch, examples, narrative, rollcall | 2L narration | 2 -- # Router source spec (data-router fold, chained-resolution m | deleted twin: `fetch_nws_alerts_conus.py` | TRIM | Header banner names the deleted twin; docstring is 3967 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/weather/fetch_nws_event/source.yaml` | 115 | 2749 (OVER) | spec, arch, narrative | 4L narration | 2 -- # Router source spec (data-router fold, tier-3 hooks, ADR 00 | deleted twin: `fetch_nws_event.py` | TRIM | Header banner names the deleted twin; docstring is 2749 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/weather/fetch_openaq_measurements/source.yaml` | 126 | 4667 (OVER) | arch, examples, narrative | 3L narration | 2 -- # Router source spec (keyed chained paging+enrich fold, ADR  | deleted twin: `fetch_openaq_measurements.py` | TRIM | Header banner names the deleted twin; docstring is 4667 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/weather/fetch_raws_weather/source.yaml` | 132 | 5403 (OVER) | spec, arch, narrative, rollcall | 3L narration | 2 -- # Router source spec (station-observations fold, ADR 0065). | deleted twin: `fetch_raws_weather.py` | TRIM | docstring names `_MAX_STATIONS` (50) and `_MAX_DATE_RANGE_DAYS` (14), constants of the deleted twin. STALE. |
| `trid3nt_server/tools/fetchers/weather/fetch_storm_events_db/source.yaml` | 117 | 3453 (OVER) | arch, examples, narrative | 3L narration | 2 -- # Router source spec (data-router fold, chained-resolution r | deleted twin: `fetch_storm_events_db.py` | TRIM | Header banner names the deleted twin; docstring is 3453 chars of routing narrative against a 1000-char front budget. |
| `trid3nt_server/tools/fetchers/weather/fetch_storm_tracks/source.yaml` | 201 | 5391 (OVER) | spec, arch, narrative, rollcall | 7L narration (fold/wave/ADR banner + deleted-twin recap) | 4 -- # Router source spec (FETCHER FINALE WAVE 2, ADR 0111): the  | deleted twin: `fetch_storm_tracks.py` | TRIM | Carries an INLINE corpus: block duplicating the sibling corpus.yaml byte-for-byte. |

## C. `corpus.yaml` retrieval phrasings (100)

Doc-shaped rows: these files carry no code and no docstring, only the natural-language phrasings
the retrieval index is built from.

| path | lines | what it is | stale claims | fate |
|---|---|---|---|---|
| `trid3nt_server/tools/fetchers/climate/fetch_chirps_precipitation/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_chirps_precipitation` | none found | KEEP |
| `trid3nt_server/tools/fetchers/climate/fetch_climate_normals/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_climate_normals` | none found | KEEP |
| `trid3nt_server/tools/fetchers/climate/fetch_era5_reanalysis/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_era5_reanalysis` | none found | KEEP |
| `trid3nt_server/tools/fetchers/climate/fetch_gridmet/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_gridmet` | none found | KEEP |
| `trid3nt_server/tools/fetchers/climate/fetch_modis_lst/corpus.yaml` | 16 | retrieval phrasings keyed `fetch_modis_lst` | 4 comment lines carry 'cleanup wave phase 2 (2026-08-25)' -- dated wave history in a phrasings file | TRIM |
| `trid3nt_server/tools/fetchers/climate/fetch_us_drought_monitor/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_us_drought_monitor` | none found | KEEP |
| `trid3nt_server/tools/fetchers/climate/lookup_precip_return_period/corpus.yaml` | 8 | retrieval phrasings keyed `lookup_precip_return_period` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hazard/fetch_epa_frs_facilities/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_epa_frs_facilities` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hazard/fetch_fault_sources/corpus.yaml` | 7 | retrieval phrasings keyed `fetch_fault_sources` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hazard/fetch_fema_nfhl_zones/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_fema_nfhl_zones` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hazard/fetch_firms_active_fire/corpus.yaml` | 10 | retrieval phrasings keyed `fetch_firms_active_fire` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hazard/fetch_hifld_critical_infrastructure/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_hifld_critical_infrastructure` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hazard/fetch_hifld_transmission_lines/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_hifld_transmission_lines` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hazard/fetch_landfire_fuels/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_landfire_fuels` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hazard/fetch_mtbs_burn_severity/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_mtbs_burn_severity` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hazard/fetch_nifc_fire_perimeters/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_nifc_fire_perimeters` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hazard/fetch_openfema_disasters/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_openfema_disasters` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hazard/fetch_tsunami_events/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_tsunami_events` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hazard/fetch_usace_dams/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_usace_dams` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hazard/fetch_usace_levees/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_usace_levees` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hazard/fetch_usfs_canopy_fuels/corpus.yaml` | 7 | retrieval phrasings keyed `fetch_usfs_canopy_fuels` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hazard/fetch_usgs_earthquakes/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_usgs_earthquakes` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hazard/fetch_usgs_volcano_alerts/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_usgs_volcano_alerts` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hazard/fetch_wfigs_incident/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_wfigs_incident` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_aquifer_thickness/corpus.yaml` | 11 | retrieval phrasings keyed `fetch_aquifer_thickness` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_aquifer_transmissivity/corpus.yaml` | 11 | retrieval phrasings keyed `fetch_aquifer_transmissivity` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_flood_extent_observation/corpus.yaml` | 7 | retrieval phrasings keyed `fetch_flood_extent_observation` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_groundwater_recharge/corpus.yaml` | 11 | retrieval phrasings keyed `fetch_groundwater_recharge` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_high_water_marks/corpus.yaml` | 7 | retrieval phrasings keyed `fetch_high_water_marks` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_jrc_global_surface_water/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_jrc_global_surface_water` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_lter_records/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_lter_records` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nhd_area_water/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_nhd_area_water` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nhd_waterbodies/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_nhd_waterbodies` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nhdplus_hr_flowlines/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_nhdplus_hr_flowlines` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nhdplus_nldi_navigate/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_nhdplus_nldi_navigate` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_noaa_nwm_streamflow/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_noaa_nwm_streamflow` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nwi_wetlands/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_nwi_wetlands` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_nws_river_forecast/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_nws_river_forecast` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_opera_dswx/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_opera_dswx` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_river_geometry/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_river_geometry` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_usgs_groundwater_levels/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_usgs_groundwater_levels` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_usgs_nwis_gauges/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_usgs_nwis_gauges` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_usgs_water_quality/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_usgs_water_quality` | none found | KEEP |
| `trid3nt_server/tools/fetchers/hydrology/fetch_water_table_depth/corpus.yaml` | 13 | retrieval phrasings keyed `fetch_water_table_depth` | none found | KEEP |
| `trid3nt_server/tools/fetchers/imagery/fetch_goes_active_fire/corpus.yaml` | 7 | retrieval phrasings keyed `fetch_goes_active_fire` | none found | KEEP |
| `trid3nt_server/tools/fetchers/imagery/fetch_goes_animation/corpus.yaml` | 12 | retrieval phrasings keyed `fetch_goes_animation` | none found | KEEP |
| `trid3nt_server/tools/fetchers/imagery/fetch_goes_archive_animation/corpus.yaml` | 11 | retrieval phrasings keyed `fetch_goes_archive_animation` | none found | KEEP |
| `trid3nt_server/tools/fetchers/imagery/fetch_goes_blend_animation/corpus.yaml` | 10 | retrieval phrasings keyed `fetch_goes_blend_animation` | 9 phrasings still indexed for a tool its own spec calls DEPRECATED | DELETE candidate -- follows the spec's fate |
| `trid3nt_server/tools/fetchers/imagery/fetch_goes_satellite/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_goes_satellite` | none found | KEEP |
| `trid3nt_server/tools/fetchers/imagery/fetch_landsat_imagery/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_landsat_imagery` | none found | KEEP |
| `trid3nt_server/tools/fetchers/imagery/fetch_naip/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_naip` | none found | KEEP |
| `trid3nt_server/tools/fetchers/imagery/fetch_sentinel1_sar/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_sentinel1_sar` | none found | KEEP |
| `trid3nt_server/tools/fetchers/imagery/fetch_sentinel2_truecolor/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_sentinel2_truecolor` | none found | KEEP |
| `trid3nt_server/tools/fetchers/imagery/fetch_slider_timestamps/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_slider_timestamps` | none found | KEEP |
| `trid3nt_server/tools/fetchers/imagery/fetch_viirs_day_fire/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_viirs_day_fire` | none found | KEEP |
| `trid3nt_server/tools/fetchers/ocean/fetch_bluetopo/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_bluetopo` | none found | KEEP |
| `trid3nt_server/tools/fetchers/ocean/fetch_greatlakes_bathymetry/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_greatlakes_bathymetry` | none found | KEEP |
| `trid3nt_server/tools/fetchers/ocean/fetch_greatlakes_water_level/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_greatlakes_water_level` | none found | KEEP |
| `trid3nt_server/tools/fetchers/ocean/fetch_gtsm_tide_surge/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_gtsm_tide_surge` | none found | KEEP |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_coops_currents/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_noaa_coops_currents` | none found | KEEP |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_coops_tides/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_noaa_coops_tides` | none found | KEEP |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_slr_confidence/corpus.yaml` | 7 | retrieval phrasings keyed `fetch_noaa_slr_confidence` | none found | KEEP |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_slr_marsh/corpus.yaml` | 7 | retrieval phrasings keyed `fetch_noaa_slr_marsh` | none found | KEEP |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_slr_scenarios/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_noaa_slr_scenarios` | none found | KEEP |
| `trid3nt_server/tools/fetchers/ocean/fetch_noaa_sst/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_noaa_sst` | none found | KEEP |
| `trid3nt_server/tools/fetchers/ocean/fetch_osm_breakwaters/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_osm_breakwaters` | none found | KEEP |
| `trid3nt_server/tools/fetchers/ocean/fetch_osm_coastline/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_osm_coastline` | none found | KEEP |
| `trid3nt_server/tools/fetchers/ocean/fetch_topobathy/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_topobathy` | none found | KEEP |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_administrative_boundaries/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_administrative_boundaries` | none found | KEEP |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_buildings/corpus.yaml` | 11 | retrieval phrasings keyed `fetch_buildings` | none found | KEEP |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_field_boundaries/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_field_boundaries` | none found | KEEP |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_ghsl_population/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_ghsl_population` | none found | KEEP |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_hrsl_population/corpus.yaml` | 7 | retrieval phrasings keyed `fetch_hrsl_population` | none found | KEEP |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_overpass_pois/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_overpass_pois` | none found | KEEP |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_population/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_population` | none found | KEEP |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_roads_osm/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_roads_osm` | none found | KEEP |
| `trid3nt_server/tools/fetchers/socioeconomic/fetch_usace_nsi/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_usace_nsi` | none found | KEEP |
| `trid3nt_server/tools/fetchers/socioeconomic/geocode_location/corpus.yaml` | 8 | retrieval phrasings keyed `geocode_location` | none found | KEEP |
| `trid3nt_server/tools/fetchers/soil/fetch_gcn250_curve_numbers/corpus.yaml` | 7 | retrieval phrasings keyed `fetch_gcn250_curve_numbers` | none found | KEEP |
| `trid3nt_server/tools/fetchers/soil/fetch_snotel_snow/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_snotel_snow` | none found | KEEP |
| `trid3nt_server/tools/fetchers/soil/fetch_soilgrids/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_soilgrids` | none found | KEEP |
| `trid3nt_server/tools/fetchers/soil/fetch_statsgo_soils/corpus.yaml` | 7 | retrieval phrasings keyed `fetch_statsgo_soils` | none found | KEEP |
| `trid3nt_server/tools/fetchers/terrain/fetch_3dep_extra/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_3dep_extra` | none found | KEEP |
| `trid3nt_server/tools/fetchers/terrain/fetch_dem/corpus.yaml` | 17 | retrieval phrasings keyed `fetch_dem` | none found | KEEP |
| `trid3nt_server/tools/fetchers/terrain/fetch_esri_landcover_10m/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_esri_landcover_10m` | none found | KEEP |
| `trid3nt_server/tools/fetchers/terrain/fetch_landcover/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_landcover` | none found | KEEP |
| `trid3nt_server/tools/fetchers/weather/fetch_airnow_air_quality/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_airnow_air_quality` | none found | KEEP |
| `trid3nt_server/tools/fetchers/weather/fetch_aorc_precip/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_aorc_precip` | none found | KEEP |
| `trid3nt_server/tools/fetchers/weather/fetch_asos_metar/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_asos_metar` | none found | KEEP |
| `trid3nt_server/tools/fetchers/weather/fetch_glm_lightning/corpus.yaml` | 12 | retrieval phrasings keyed `fetch_glm_lightning` | none found | KEEP |
| `trid3nt_server/tools/fetchers/weather/fetch_hrrr_forecast/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_hrrr_forecast` | none found | KEEP |
| `trid3nt_server/tools/fetchers/weather/fetch_hrrr_smoke/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_hrrr_smoke` | none found | KEEP |
| `trid3nt_server/tools/fetchers/weather/fetch_mrms_qpe/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_mrms_qpe` | none found | KEEP |
| `trid3nt_server/tools/fetchers/weather/fetch_nldas2_forcing/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_nldas2_forcing` | none found | KEEP |
| `trid3nt_server/tools/fetchers/weather/fetch_nws_alerts_conus/corpus.yaml` | 7 | retrieval phrasings keyed `fetch_nws_alerts_conus` | none found | KEEP |
| `trid3nt_server/tools/fetchers/weather/fetch_nws_event/corpus.yaml` | 7 | retrieval phrasings keyed `fetch_nws_event` | none found | KEEP |
| `trid3nt_server/tools/fetchers/weather/fetch_openaq_measurements/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_openaq_measurements` | none found | KEEP |
| `trid3nt_server/tools/fetchers/weather/fetch_raws_weather/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_raws_weather` | none found | KEEP |
| `trid3nt_server/tools/fetchers/weather/fetch_storm_events_db/corpus.yaml` | 9 | retrieval phrasings keyed `fetch_storm_events_db` | none found | KEEP |
| `trid3nt_server/tools/fetchers/weather/fetch_storm_tracks/corpus.yaml` | 8 | retrieval phrasings keyed `fetch_storm_tracks` | none found | KEEP |

## D. `docs/decisions/`

Not applicable: the scope `trid3nt_server/tools/fetchers` contains no `docs/decisions` entries and
no `.md` file of any kind. Every tracked file in scope is `.py` or `.yaml`, so the DOCS CENSUS
decision table (number / title / BINDING or SUPERSEDED-by or DEAD-because / fate) has no rows here.

## Scope-wide findings

1. 54 of the 99 `source.yaml` files name a DELETED twin module in their header comment
   (`# Twin: fetch_<name>.py -- FOLDED ...`). The twins were removed at the fetcher endgame; the
   comment is the single largest class of dead reference in scope.
2. 67 of the 99 `source.yaml` files carry spec notation in the header banner (`ADR NNNN`,
   `wave-N`, `phase-2`, `FETCHER FINALE`), an explicitly disallowed class.
3. 97 of the 99 `source.yaml` docstrings exceed the 1000-char LLM front budget (median 3301,
   max 8171 at `ocean/fetch_topobathy`). The overrun is usage narrative, worked examples and
   cross-tool architecture, all disallowed classes.
4. 94 of the 104 non-empty `.py` files hold at least one docstring over the physical-line cap;
   `imagery/_goes_archive_core.py` (26), `_router/hooks/topobathy.py` (22) and
   `_router/executors/raster_cog.py` (20) lead.
5. 102 of the 206 `.py` files are 0-byte `__init__.py` package markers.
6. Person attribution survives in 4 places: `_router/hooks/topobathy.py:1314` ('NATE resolution
   doctrine, 2026-08-11'), `socioeconomic/geocode_location/geocode_location.py:56` ('NATE directive
   2026-06-17'), `socioeconomic/fetch_population/hooks.py` ('NATE flag-not-copy') and
   `_router/shape_classifier.py` ('the NATE shape principle', twice).
7. One memory filename survives: `hydrology/fetch_nhd_waterbodies/source.yaml:118`
   ('feedback_data_source_fallback_norm').
8. Missing paths named from inside the scope: `experiments/catalog_surfacing/DESIGN.md`,
   `agent/tools/__init__.py`, `data_fetch.py`, `server.py`, a bare `VERDICT.md`, and
   `_router/transforms/join.py`. The join.py reference in `router.py` is a DELIBERATE refusal
   message and is not a defect; the one in `fetch_openfema_disasters/hooks.py` is a stale link.
9. Five `source.yaml` files carry an inline `corpus:` block duplicating the sibling `corpus.yaml`;
   two of the five have DRIFTED (`terrain/fetch_landcover`, `hydrology/fetch_noaa_nwm_streamflow`).
10. `ocean/fetch_greatlakes_water_level` is the only fetcher directory in scope with no
    `__init__.py`. `terrain/fetch_copernicus_dem` is the only one with no `corpus.yaml`, and that
    is correct: it is the `tier="internal"` seam behind `fetch_dem`, deliberately out of the index.
11. Stale factual claims inside LLM-facing docstrings: `fetch_noaa_slr_scenarios` calls
    `fetch_noaa_slr_marsh` 'not yet implemented' (it is, in this scope); `fetch_hrrr_smoke` calls
    EPA AirNow a 'future tool' (`fetch_airnow_air_quality` exists); `fetch_landcover` says
    `extract_landcover_class` works 'once it lands' (it landed); `fetch_statsgo_soils` and
    `fetch_3dep_extra` document a `timeout_s` parameter their specs do not declare;
    `fetch_asos_metar`, `fetch_raws_weather`, `fetch_3dep_extra`, `fetch_usace_nsi` and
    `fetch_mrms_qpe` name constants and functions that lived only on the deleted twins.
12. `imagery/fetch_goes_blend_animation` self-declares DEPRECATED in its own docstring while
    keeping a full 9-phrasing `corpus.yaml`. It is the one DELETE candidate in scope.

### `# docstring-exempt:` candidates (10, at the STOP ceiling)

| target | reason a longer docstring is a genuine contract |
|---|---|
| `_router/executors/raster_cog.py` -- the per-access-mode module banners | each names the physical reason a mode exists (a gzip stream is not byte-servable; a DEFLATE ZIP member cannot be byte-windowed; GRIB needs a real path) -- a constraint no signature carries |
| `_router/hooks/topobathy.py::_waterline_mask` region | the waterline mask is a refusal rule with measured evidence; cutting it re-opens the flat-ocean failure |
| `_router/router.py::_dispatch` | the executor precedence order is the contract between spec and engine and is not inferable from the signature |
| `_router/registration.py::register_specs_from_tree` | states the skip-one-broken-spec startup rule that a caller must rely on |
| `_router/transport/errors.py::classify_status` | the status -> typed-error mapping is the honesty floor's input-output contract |
| `imagery/_goes_archive_core.py` fire-threshold block | the 320 K / 10 K thresholds are defensible only with their Matson & Dozier / MODIS C5 derivation |
| `hazard/fetch_fema_nfhl_zones/hooks.py` pacing block | the tile width and delay are the measured limits of the upstream service |
| `socioeconomic/geocode_location/geocode_location.py` sub-locality block | the result-class preference and minimum-AOI floor are a two-part refusal contract |
| `climate/lookup_precip_return_period/...py` Atlas-2 block | a bundled parameterization must state its provenance or it reads as fabricated data |
| `_router/emit_on_fetch.py` module docstring | the role="context" nesting semantics are settled behaviour a caller depends on |

files listed: 405 / files read: 405 / rows written: 405
