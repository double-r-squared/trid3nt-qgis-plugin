# 0177: Category-taxonomy vocabulary audit - "hazard" is not the product identity

## Context

TRID3NT is general geospatial intelligence, not a hazard workbench. The
"hazard" vocabulary sprinkled through the tool taxonomy is web-era legacy. The
worst offender was the top-level category `hazard_modeling`, which was the
primary home for a large grab-bag of tools - many of which answer questions
that are not hazards at all: terrain diagnostics (Landlab Hack's-law scaling,
flow accumulation, HAND wetness, DEM conditioning, lake mapping), closed-form
validation gates (MODFLOW package V&V, SCHISM transport-scheme mixing/mass
conservation, Pelicun closed-form check, ELMFIRE elliptical-spread
verification), and model-vs-observation skill primitives. The category name
also framed the whole product as hazard-only through its description and the
adapter system prompt. The template-capability-naming norm (names = question
class, never place/case/hazard) applied to categories.

Constraint: categories are the LLM-visible retrieval surface and the design is
deliberately restrained (~12 classes for retrieval). The fix is renames + a
small number of honest splits, not a re-architecture. Category text does NOT
feed the docstring-indexed tool-retrieval corpus, so renames cannot break
`retrieve_visible_tools` / template surfacing.

## Decision

Audit the taxonomy to question-class-honest names; 12 categories -> 13.

- **Rename** `hazard_modeling` -> `simulation_modeling` ("Simulation and
  modeling"). The honest question class is "RUN a physics-based process
  simulation" (across flood, drainage, groundwater, seismic, wildfire, waves,
  landscape, tsunami/surge) - not "hazard". Description enumerates domains
  generally; no hazard-as-identity framing.
- **Split out** `model_validation` ("Model validation"): the correctness/
  benchmark question class. Primary members: `modflow_package_validation`,
  `schism_transport_validation`, `pelicun_closed_form_validation`,
  `elmfire_verification_elliptical_replication`. Cross-listed: the V&V analysis
  primitives (`compute_model_residuals`, `compute_skill_metrics`,
  `compute_flood_extent_skill`, `extract_model_at_observations`,
  `read_run_diagnostics`) and their observed-data inputs
  (`fetch_high_water_marks`, `fetch_flood_extent_observation`). Each keeps a
  cross-list back to its engine domain so it still surfaces from the
  simulation/damage/fire lane.
- **Re-home** the pure terrain/hydrology Landlab diagnostics out of the
  simulation lane into their honest data lanes: `landlab_hacks_law_scaling` +
  `landlab_dem_conditioning` -> `terrain_elevation`; `landlab_flow_accumulation`
  + `landlab_lake_mapping` + `landlab_hand_wetness` -> `hydrology` (each cross-
  listed to the sibling lane). The genuine process sims (susceptibility,
  Green-Ampt, landslide ensemble, overland-flow timeseries) stay in
  `simulation_modeling`.
- **Framing sweep.** Adapter `SYSTEM_PROMPT` identity lines rewritten from
  "geospatial hazard-modeling assistant / model natural hazards" to "general
  geospatial intelligence assistant" spanning any domain. `psha.py` "the
  multi-hazard workbench's seismic driver" -> "the platform's seismic driver".
  Board line reframed off "a hazard workbench". Legitimate domain uses of
  "hazard" (a flood IS a hazard; PSHA/NSHM are real seismic-hazard terms; FEMA
  National Risk Index; compound-flood climatology) are left untouched - the ban
  is on framing the PRODUCT as hazard-only, not on honest domain vocabulary.

## Consequences

- No source code keys on the literal category string beyond `categories.py`
  (verified by grep across `server/`); consumers were only tests, all updated.
- No persisted-case impact: `AllowedToolSet.opened_categories` is session-scoped
  in-memory (never persisted in cases), and `_build_from_hot_set` already skips
  unknown/renamed category ids gracefully - a stale `hazard_modeling` in any
  in-flight session degrades to a no-op (tools re-open on the next
  `list_tools_in_category`), never a crash.
- Retrieval unchanged: the template corpus surfacing gate
  (`test_door_dissolution::test_every_template_surfaces_in_top8`) and the
  catalog-surfacing gate both pass unchanged, since category text is not part of
  the tool-retrieval index.
- 13 categories stays within the restrained retrieval design; the two new/renamed
  ids are honest question classes, not an explosion.
