# Generic data router - the fetcher fold (FOR NATE REVIEW)

NATE's vision (2026-07-28, greenlit as the next campaign): "a generic router
for data similar to the catalog structure where the endpoint is defined and
the piping adapts to the data being ingested... reusable since most of the
data is similar." Target: the 71,836-line fetcher family (38% of the agent
surface, 1/3 of the package). "Redundancy that doesn't pay off" goes; the
routing worry becomes a measured gate, not a fear.

## The architecture

1. SOURCE SPECS (data, not code): one YAML per data source -
   endpoint(s) + auth mode, request-param schema (bbox/time/product knobs),
   response format + shape (COG / vector / station-timeseries / tiles),
   normalization directives (CRS, units, quantity stamp, datum), cache
   TTL class, payload estimate, honest caveats, fallback chain,
   supports_global_query, AND the corpus phrasings - co-located, exactly
   like tools today. Adding a source = adding a YAML. Zero registry cost,
   zero routing cost (phrasings carry the routing, as they always did).
2. THE ROUTER (one engine, reusable piping): resolve spec -> build request
   (shared param validation, granularity gate integration) -> fetch w/
   retry/fallback per the data-source norm -> ADAPTIVE INGESTION keyed on
   declared shape (raster->COG pipeline, vector->FGB pipeline,
   timeseries->station-FGB w/ time_series_csv, tiles->assembly) -> stamp
   (CRS/units/quantity/datum) -> cache -> publish/emit envelope. All the
   seams every fetcher duplicates today (_fetch_common, cache read-through,
   payload estimates, typed upstream errors) exist ONCE.
3. RETRIEVAL: source specs index into the SAME corpus machinery (tree-walk
   loader). Either surfaced per-source as virtual tools (registration
   decoupled from code - the tier mechanism exists) or via
   search_data_catalog + fetch_from_catalog as the consumption pair -
   decided by the bench (whichever routes better).

## The derisking gates (why this cannot silently degrade the product)

- PHASE 1 (build + pilot): router + spec schema + 5 representative sources
  spanning the shapes: one COG raster (fetch_gridmet), one vector API
  (fetch_usgs_nwis_gauges or wdpa), one station-timeseries
  (fetch_noaa_coops_tides), one tiled/imagery, one awkward case picked by
  the audit. Hand-written twins STAY during the pilot.
- BENCH GATE (the routing worry, measured): retrieval_probe + canonical
  routing checks - catalog-routed phrasings must rank >= the hand-written
  baseline for the pilot sources. Deterministic, NATE-methodology rules
  (sign-off on inputs, no LLM judging). Fail -> we learned cheaply,
  architecture adjusts before any cut.
- REPLICATION GATE per source (cull doctrine): same request -> envelope
  compared vs the hand-written fetcher (values, layer output, caveats,
  error behavior incl. upstream-failure paths). A fetcher dies ONLY when
  its spec passes both gates.
- PHASE 2: family-by-family migration (USGS family, NOAA family, satellite
  family...) with per-source proofs; each family landing removes its
  hand-written twins same-commit (clean-as-you-go).
- PHASE 3: the residual set - fetchers with genuinely bespoke logic
  (Overpass query construction, multi-endpoint stitching, animation
  assembly) STAY AS CODE, honestly classified by the phase-1 audit.
  Expect 60-80% foldable; even 60% = ~40k lines out.

## Consumer compatibility

Workflows/templates import ~a dozen fetchers directly (nested use). The
router exposes the same callable seam per source (registry-resolved), so
nested consumers migrate mechanically; envelope shapes unchanged.

## Sequencing

After the in-flight SFINCS remediation lands: phase-1 audit (classify all
~100 fetchers by shape + bespoke-ness + telemetry usage ranking) -> NATE
reviews the classification -> build pilot -> gates -> fan out. THEN the
tool/workflow extensions (template growth) follow, per NATE's ordering.
