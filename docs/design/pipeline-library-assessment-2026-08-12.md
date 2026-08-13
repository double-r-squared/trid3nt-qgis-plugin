# Architecture walkthrough: the GeoClaw earthquake-to-tsunami chain vs the pipeline-library vision

Date: 2026-08-12. Written for NATE's review after ADR 0229 landed the
complete Chignik chain. Purpose: assess how close the CURRENT composer
code already is to the intended library abstraction (OpenCV-style: plain
well-named functions - load / gate / simulate / plot - composed in
ordinary Python; explicit args; target_resolution_m passed and inherited,
never hardcoded). The design itself is DEFERRED - this is the shared
reference for that discussion. See docs/IDEAS.md for the parked design
state (universal target_resolution_m, settings value|native|auto,
inheritance by argument).

## The chain in execution order (stage -> file:line -> verdict)

1. RESOLVE the earthquake - `agent/workflows/geoclaw/earthquake_source.py:221`
   `resolve_earthquake_source(region, min_magnitude, ...)`: geocode ->
   USGS ComCat -> pick event.
   VERDICT: already library-shaped. Standalone, typed, reusable.

2. LOAD the slip model - `agent/workflows/geoclaw/finite_fault.py:298`
   `fetch_finite_fault_model(event)`: ComCat finite-fault product ->
   parse_fsp -> N-subfault table -> CSVFault text, provenance
   basis=measured_inversion. Ladder: measured -> derived (loud) -> typed
   error.
   VERDICT: library-shaped, same reason.

3. COMPOSE + domain - `agent/workflows/geoclaw/inundation/inundation.py:163`
   (tool entry) -> `model_geoclaw_inundation:959`;
   `run_geoclaw.py:plan_geoclaw_domain:365` sizes the rupture-enclosing box.
   VERDICT: procedural and staged (the shape is right); composer-private.

4. LOAD the bathymetry - `inundation.py:_fetch_topo_for_geoclaw:736`
   (docstring IS the fallback doctrine: seamless topobathy -> land DEM
   fallback -> honest typed error) -> `_router/hooks/topobathy.py:
   read_topobathy:1413` -> `_select_and_merge:1197` ->
   `_mask_land_leg_ocean_fill:874` (the ADR 0229 deep-water rung: mask
   the 3DEP flat ocean fill so ETOPO's real column shows through).
   VERDICT: SPLIT. The router internals are true library machinery (all
   specs ride them). But `_fetch_topo_for_geoclaw` is composer-PRIVATE,
   and SCHISM carries its own `_fetch_bathymetry_cog` doing ~80% the
   same job. In the library these merge into one
   `load_topobathy(bbox, target_resolution_m=..., force_bathy_base=...)`.

5. AUTHOR the deck - `run_geoclaw.py:stage_geoclaw_manifest:1130`;
   `services/workers/geoclaw/setrun_builder.py:render_maketopo_dtopo:1023`
   + `render_setrun_py:1425`; the flat guard
   `entrypoint.py:_validate_offshore_bathymetry:390` (the honest refusal
   that ADR 0229 satisfied with real data rather than deleted).
   VERDICT: rightly per-engine (deck formats are engine truth); behind
   the strict parser.

6. SIMULATE - `agent/adapters/.../solver.py:run_solver:1576` ->
   `_run_solver_local_docker:1235` -> `wait_for_completion:1714`.
   VERDICT: ALREADY the generic verb - the same run_solver serves
   landlab/MODFLOW/GeoClaw. The gap is noise, not shape: ~20 lines of
   card-minting/emitter-binding boilerplate surround every dispatch in
   every composer (see flow_accumulation.py:366-390 for the same
   pattern); in the library that plumbing lives INSIDE simulate().

7. POSTPROCESS - `agent/workflows/geoclaw/postprocess_geoclaw.py:1231`
   (fgmax, frame rasterization, deformation COG, gauge series, AMR mesh
   geojson).
   VERDICT: per-engine by nature; fine.

8. PLOT - `inundation.py`: `publish_raster_input_cog@1082` (the 0227
   bathy spot-check layer), `_publish_peak_layer:1708`,
   `_emit_deformation_layer:1675`, `_maybe_emit_gauge_chart:1758`,
   `publish_input_layer@1626` (mesh).
   VERDICT: the messiest stage vs the vision - FIVE publish/emit entry
   points where the library wants one `plot(thing, role=...)`. This seam's
   ambiguity is what produced the modflow never-plotted-its-map bug; it is
   where extraction pays most.

## Overall assessment

The architecture is roughly 70% of the vision already: procedural staged
composers, explicit knobs on signatures (target_resolution_m at
flow_accumulation.py:138), one generic simulate seam, gates as composable
calls (gate_input_review), honest typed fallbacks everywhere. What is
missing is not structure but VOCABULARY: the load/plot verbs exist as
~40 per-engine private functions instead of one shared library, and the
observability plumbing is written longhand at every call site instead of
living inside the verbs.

Consequence: the refactor is EXTRACTION + CONSOLIDATION, not invention -
it can proceed incrementally (engine by engine) with no big-bang rewrite,
and the universal target_resolution_m rollout naturally rides it, because
the extracted load_* functions are exactly where the knob and its
argument-inheritance land.
