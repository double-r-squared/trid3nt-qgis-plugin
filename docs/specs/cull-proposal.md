# Cull proposal - post engine-door-refactor registry sweep

Status: PROPOSAL ONLY. NATE decides. Nothing is deleted by this document.
Date: 2026-07-27. Branch: refactor/engine-doors.
Authority: docs/specs/engine-door-refactor.md step 4 ("Cull redundant tools in
the sweep") + the INTEGRATE-lane kickoff item 5. Scope: a redundancy
assessment across the FULL live registry (212 tools, confirmed via
`trid3nt_server.main._import_tools_registry()`), not just the 10 engines
touched this wave.

ASCII hyphens only. Every recommendation below is per-tool evidence pulled
from the live code (docstrings, call sites, grep), not speculation.

---

## 1. CULL candidates (concrete, evidence-backed)

### 1.1 `fetch_copernicus_dem` - self-declared deprecated alias

File: `tools/fetchers/terrain/fetch_copernicus_dem/fetch_copernicus_dem.py:499`.
Docstring literally reads: "DEPRECATED alias of `fetch_dem` with
`source=\"copernicus\"`. Retained as a thin registered delegate for backward
compatibility (existing cases + the routing bench)." The function body is one
line: `return _copernicus_dem_impl(bbox)`, itself a call-through into the
`fetch_dem` machinery. `fetch_dem` already exposes `source="copernicus"` as a
first-class mode.

Evidence it is not load-bearing beyond back-compat: no engine template, door,
or composer in `workflows/` calls `fetch_copernicus_dem` (grep across
`workflows/` and `tools/` finds zero non-registration references besides its
own module and the routing-bench fixture).

RECOMMENDATION: CULL (retire the registered name; `fetch_dem(source=
"copernicus")` is the one true path). Low risk - the tool's own docstring
already tells callers to prefer `fetch_dem`.

### 1.2 `fetch_goes_blend_animation` - self-declared deprecated alias

File: `tools/fetchers/imagery/fetch_goes_animation/fetch_goes_animation.py:810`.
Docstring: "DEPRECATED alias of `fetch_goes_animation` with `band=\"blend\"`.
Retained as a thin registered delegate for backward compatibility." Same
shape as 1.1 - a zero-logic delegate into a sibling registered tool's existing
parameter surface.

RECOMMENDATION: CULL (retire the registered name; `fetch_goes_animation(band=
"blend")` is the one true path).

### 1.3 `pelicun_damage_with_buildings` - composed pipeline, not an atomic primitive

File: `workflows/pelicun/damage_with_buildings/damage_with_buildings.py:391`.
Docstring: "Two-step composition: `compute_building_density(bbox,
cell_size_m)` -> `density_cog_to_point_fgb` (COG-to-point conversion) ->
`pelicun_damage_assessment(hazard_raster_uri, assets_uri=<points_fgb>)`."
This is a registered TEMPLATE (engine=pelicun, tier=template) whose entire
body is a 3-step call chain of a fetch + a format conversion + a sibling
registered tool. Under the "analysis is playground, not tools" hard rule
("atomic tools = DATA fetchers + irreducible primitives ONLY, never composed
analyses"), this is the same shape as the MODFLOW pilot's CUT precedent
(`run_model_contamination_affected_fields`, cut because its zonal-scoring
half was a composition better done in the playground).

This exact tension is already flagged as OPEN-A in
`docs/specs/engine-rollout-contract.md` section 8, where NATE's contract PIN
was "KEEP SEPARATE" (two templates) on the grounds that the INVENTORY
ACQUISITION pathway (explicit vector assets vs auto-fetched building-density
grid) is a genuine functional difference, not a mere convenience knob. That
pin was made at the PELICUN slice's kickoff, before this full-registry sweep.
Re-flagging here because the redundancy assessment surfaces the identical
composed-analysis pattern the analysis-is-playground norm targets, and the
contract's own "documented alternative" (fold `assets_uri` OR `bbox` as one
knob on `pelicun_damage_assessment`) remains on the table.

RECOMMENDATION: NO NEW ACTION - OPEN-A stays open, NATE already has the
decision framed with both options. Not re-litigating; just consolidating the
evidence in one place since this doc's job is the full-registry pass.

---

## 2. Reviewed and CONFIRMED NOT redundant (evidence recorded so this is not
re-litigated on the next sweep)

### 2.1 `search_spatial_functions` overlap candidates - the GDAL/QGIS-Processing wraps

Registered tools `compute_hillshade`, `compute_slope`, `compute_aspect`,
`compute_colored_relief`, `compute_contours` each wrap a single `gdaldem` /
`gdal_contour` command that is ALSO reachable as a generic QGIS Processing
algorithm via `list_qgis_algorithms` / `describe_qgis_algorithm` /
`qgis_process` (`gdal:hillshade`, `gdal:slope`, `gdal:aspect`,
`gdal:colorrelief`, `gdal:contour`). On the surface this is the exact
"tool duplicating discoverable QGIS Processing coverage" pattern the kickoff
asked to check.

Evidence AGAINST culling, read from each module's own docstring: every one
of these five tools is wired through the `read_through` FR-DC-3 cache shim
(`cache/static-30d/<name>/<key>.tif`, key derived from the exact params that
affect output pixels), returns a `LayerURI` with a baked `style_preset` the
web renderer consumes directly, and carries a typed-error surface -- none of
which the generic `qgis_process` dispatch path provides (no caching, no
style_preset wiring, no typed errors, and the LLM would have to know the
`gdal:*` algorithm id + its exact parameter dictionary shape). This is the
same coexistence the "Tool integration paradigm A vs B" ADR already blesses
(Paradigm A discoverable QGIS Processing + Paradigm B hand-written wraps are
BOTH legitimate, not mutually exclusive) -- these five earn Class-B status by
adding caching + rendering-pipeline integration, not by accident.

`merge_features` (dissolve) and `cut_features_with_polygon` (erase/difference)
overlap `native:dissolve` / `native:difference` even more directly, and their
own docstrings pre-empt the question: `cut_features_with_polygon.py:9-13`
states verbatim "The delta over QGIS native `native:difference` (Processing)
is exactly the in-place attribute/ID preservation ... That delta is why this
is a Class-B hand-written tool rather than a Processing pass-through."
`merge_features.py` carries the parallel GPL-cleanliness note (clean-room
GEOS reimplementation, not a wrap of the GPL DigitizingTools plugin).

RECOMMENDATION: KEEP all seven. Evidence is self-documented and sound; no
action.

### 2.2 `fetch_landcover` vs `fetch_esri_landcover_10m` - looks like a source-mode
split that was NOT folded (cf. `fetch_dem(source=)` / `fetch_copernicus_dem`)

Both are landcover fetchers; superficially resembles the copernicus-DEM
pattern (1.1) where a second source should have been folded into the first
as a `source=` mode. Evidence against folding: the two have INCOMPATIBLE
return contracts -- `fetch_landcover` returns a `dict` (LayerURI plus an
`nlcd_vintage_year` sidecar that SFINCS setup consumes for Manning's-roughness
vintage validation) and is US-only (NLCD/WorldCover via MRLC WCS);
`fetch_esri_landcover_10m` returns a bare `LayerURI` (global 10 m Impact
Observatory schema, Microsoft Planetary Computer STAC, auto-tiling for large
bboxes). `fetch_esri_landcover_10m`'s own docstring: "Use this (not
`fetch_landcover`, which serves US-only NLCD as a dict) when the AOI is
OUTSIDE CONUS." Folding would require a discriminated-union return type or a
breaking change to the SFINCS consumer of the sidecar field -- not a
same-shape convenience fold like `source="copernicus"` was.

RECOMMENDATION: KEEP both. No action; different contracts, not redundant.

### 2.3 Dead-composer sweep - none found

Checked: every `workflows/**/*.py` and `tools/simulation/**/*.py` module
whose file stem does NOT match a currently-registered tool name (78
candidates, mechanically enumerated from the 255 folder-per-tool files under
`workflows/` + `tools/`). Cross-referenced each against the rest of the tree
for an import/reference. Result: ALL 78 have >=1 live reference elsewhere
(postprocessors imported by their engine's template/door pair, `_common.py`
/ `physics_registry.py` / `cog_io.py`-style shared internals imported across
multiple engines, and -- the ones that look most like refactor debris --
the OLD pre-rename composer bodies, e.g.
`workflows/swmm/model_urban_flood_swmm/model_urban_flood_swmm.py`,
`workflows/telemac/model_river_dye_release_scenario/...`,
`workflows/geoclaw/model_dambreak_geoclaw_scenario/...`,
`workflows/openquake/model_seismic_hazard_scenario/...`,
`workflows/elmfire/model_fire_spread_scenario/...`,
`workflows/swan/model_wave_scenario/...`,
`workflows/landlab/model_landslide_scenario/...` -- each of these is
DIRECTLY IMPORTED by its engine's new folder-per-template module (e.g.
`workflows/swmm/urban_flood/urban_flood.py:44` imports from
`workflows.swmm.model_urban_flood_swmm.model_urban_flood_swmm`) as the
internal, no-longer-separately-registered engine composition body the
template wraps. This mirrors the MODFLOW pilot precedent (templates call the
shared `run_modflow_archetype_job` internally) -- by design, not orphaned.

RECOMMENDATION: no dead code found this pass. NOTE (not a cull item, a
structural observation for a future job if NATE wants it): the OLD-named
folders above are now pure internal-implementation modules with no registered
`@register_tool` of their own; a future tidiness pass COULD rename them to
drop the pre-refactor `model_*_scenario` naming (e.g.
`workflows/swmm/model_urban_flood_swmm/` -> `workflows/swmm/urban_flood/
_engine.py` or similar) so the tree does not read as "two composers per
engine" at a glance. Purely cosmetic; zero functional redundancy.

### 2.4 `analyze_affected_fields` - carried-forward open flag, not a new finding

Stays registered tier=general (RISK-6 in `docs/specs/modflow-pilot-contract.md`
section "Risks"): whether it too should become a playground recipe (per
analysis-is-playground) is EXPLICITLY deferred there as a separate decision
from the `run_model_contamination_affected_fields` cut. Restating here only
so the full-registry sweep's cull list is complete; not re-deciding it.

### 2.5 `model_debris_flow` - engine-ambiguous by name, confirmed standalone

Already resolved in `docs/specs/engine-rollout-contract.md` section 3.2
(OPEN-B): uses the vendored `pfdf` library (Staley/Gartner/Cannon empirical
models), imports no physics solver, not folded under any door. No new
evidence changes that call.

---

## 3. Data-catalog overlap (`search_data_catalog` / `fetch_from_catalog`)

`search_data_catalog` searches a curated YAML catalog
(`TRID3NT_CATALOG_YAML=public_data_source_catalog.yaml`) and returns entries
resolved through the generic `ogc_adapter` (`fetch_ogc_layer`); it is the
Paradigm-A "discoverable, no hand-wrap" surface described in the
tool-integration-paradigm decision. The ~100 hand-written `fetch_*` tools in
the registry are the Paradigm-B surface for sources that need bespoke parsing,
auth, or a non-OGC access pattern (STAC item search, agency-specific REST
APIs, FTP/HTTP archive layouts, credentialed endpoints). A full line-by-line
cross-reference of every hand-written fetcher's source against the catalog
YAML's entries (to find a fetcher that is a PURE duplicate of a catalog entry
already reachable generically) was NOT completed in this pass -- it is a
non-trivial audit (catalog entries are keyed by provider/topic, not by
registered-tool name, and several fetchers pre-date the catalog surface
entirely) and the kickoff's time budget for this lane does not cover it.

RECOMMENDATION: flag as a FOLLOW-UP audit, not attempted here. No cull
candidates asserted in this category this pass (asserting one without doing
the cross-reference would violate the "per-tool evidence" bar this doc holds
itself to elsewhere).

---

## 4. Engine-door refactor itself - net registry effect (context, not new)

For completeness, the wave's own net effect on registry size (already
reported per-lane in the INTEGRATE kickoff, restated here since a cull
proposal should be read against the current shape): 212 registered tools
total; 10 engine doors (tier=door) + 21 engine templates (tier=template,
pool-excluded) replaced 19 old top-level engine-tool registrations (14 MODFLOW
+ `run_model_flood_scenario` + `run_swmm_urban_flood` (thin wrapper already
folded pre-wave) + `run_geoclaw_inundation` + `run_swan_waves` +
`run_landlab_susceptibility` + `run_seismic_hazard_psha` + `model_fire_spread`
+ `run_pelicun_damage_assessment` + `run_pelicun_with_buildings`) plus 6
now-deleted thin `run_<engine>_tool` wrapper shims. This restructure is
DONE and verified (section elsewhere in the INTEGRATE report); it is not
itself a new cull item, just the baseline this proposal's section 1-3
candidates sit on top of.

---

## Summary table

| # | tool(s) | finding | recommendation |
|---|---------|---------|-----------------|
| 1.1 | `fetch_copernicus_dem` | self-declared deprecated alias of `fetch_dem(source="copernicus")` | CULL |
| 1.2 | `fetch_goes_blend_animation` | self-declared deprecated alias of `fetch_goes_animation(band="blend")` | CULL |
| 1.3 | `pelicun_damage_with_buildings` | composed pipeline (fetch+convert+call-sibling); OPEN-A already frames the fold alternative | NO NEW ACTION - OPEN-A stays open for NATE |
| 2.1 | `compute_hillshade`/`slope`/`aspect`/`colored_relief`/`contours`, `merge_features`, `cut_features_with_polygon` | overlap QGIS Processing natives but earn Class-B status via caching + style_preset + typed errors (self-documented) | KEEP |
| 2.2 | `fetch_landcover` vs `fetch_esri_landcover_10m` | different return contracts (dict+sidecar vs bare LayerURI), US vs global | KEEP |
| 2.3 | dead-composer sweep (78 candidates checked) | zero orphaned; all referenced | KEEP (no action); optional cosmetic rename noted |
| 2.4 | `analyze_affected_fields` | carried-forward RISK-6, not new | NO NEW ACTION |
| 2.5 | `model_debris_flow` | carried-forward OPEN-B, not new | NO NEW ACTION |
| 3 | ~100 hand-written fetchers vs catalog YAML | cross-reference not completed (out of budget) | FOLLOW-UP AUDIT |

PROPOSAL ONLY. Nothing in this document has been deleted, renamed, or
otherwise changed in the codebase. NATE decides which (if any) of section 1's
two concrete CULL candidates to action, and whether to open the section 3
follow-up audit as its own job.
