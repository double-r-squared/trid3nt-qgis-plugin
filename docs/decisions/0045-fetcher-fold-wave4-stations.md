# 0045 - fetcher-fold phase-2 wave-4 (station family; CO-OPS currents snapshot)

Context: phase-2 fans the fold out family-by-family. This wave targets the
STATION-TIMESERIES family (audit `docs/specs/fetcher-fold-audit.md`), proven by
the folded fetch_noaa_coops_tides pilot's `station_timeseries` executor. Reading
all six candidate twins IN FULL (the recipe's "read each twin fully; HYBRID/
BESPOKE is a prior, not a verdict") found the audit's SPEC-EXPRESSIBLE labels
optimistic: the pilot executor is coops-tides-shaped (single-catalog discover ->
per-station JSON loop -> ONE point per station carrying rollups + inline
`time_series_csv`, fixed water-level columns), and every sibling diverges in a
DISTINCT ingestion mode. Only the literal sibling (currents) reuses the proven
catalog + per-station machinery; the rest each need a wholly new mode.

Decision (NATE authority via the wave-4 kickoff; both gates held per folded source):

1. FOLD (1): fetch_noaa_coops_currents (audit HYBRID). Spec-served under the twin
   name (source.yaml + a new `emit: snapshot` station mode + the `coops_currents`
   named transform = the audit's `_parse_predictions` flood/ebb/slack hook); twin
   `git rm`-ed. It is the coops-tides sibling: SAME station catalog + per-station
   datagetter, but emits ONE snapshot row per station (latest observed / prediction
   nearest now) instead of the full series. LIVE twin-vs-router parity 28/28 over
   real SF Bay stations (`drivers_wave4.py`): station-id SET identical for both
   products, schema/geom/crs/layer/every error path parity.

2. ROUTER EXTENSIONS (all declarative `ingest.per_station.*`; STRICT no-op for
   every prior spec -- proven: the folded coops-tides source is byte-identical, no
   `emit` key -> the unchanged rollup path):
   - `per_station.emit: snapshot` selects `execute_snapshot` (one row/station) over
     the rollup+`time_series_csv` path; absent -> the timeseries path unchanged.
   - `per_station.snapshot.{transform, columns, window, request_by_product}`:
     `transform` names the registered selector (`coops_currents`); `columns` is the
     emitted point-FGB schema; `window` is the now-relative per-product date window
     (observed 2d lookback / predictions 2d lookahead, no date params); and
     `request_by_product` adds product-conditional request params (predictions ->
     `interval=MAX_SLACK`). `error_prefix: COOPS_CURRENTS` + default suffixes
     reproduce the twin's INPUT_ERROR / UPSTREAM_ERROR / EMPTY A.6 codes.

3. DEFER (5, per-source STOP-RULE evidence -- each needs a wholly new ingestion
   mode / auth subsystem, its own wave; never guessed):
   - fetch_asos_metar + fetch_raws_weather (audit SPEC): MULTI-STATE IEM GeoJSON
     discovery (a 51-entry per-state bbox-overlap table, not the single catalog the
     station mode has) -> ONE point per OBSERVATION ROW (flat obs, not the
     per-station rollup) with (asos) a single bulk-CSV request / (raws) a
     per-station-per-DAY JSON loop + SHEF field rename. A new "station-observations"
     mode + a multi-state-discovery mode; cohesive future wave.
   - fetch_snotel_snow (audit SPEC): single catalog + client-side network re-pin ->
     ONE batched multi-station data call (not per-station loop) -> latest-non-null-
     per-element snapshot with off-season-zero preservation; plus TWO router-emission
     gaps -- LayerURI.bbox = STATION EXTENT (side-channel, router emits request bbox)
     and a degrade-to-locations fallback (data-source-fallback norm) on DATA failure.
     A batched-snapshot mode + two new emission/fallback capabilities.
   - fetch_airnow_air_quality + fetch_openaq_measurements (audit SPEC endpoint,
     bespoke AUTH): 3-path API-key auth (kwarg -> per-Case `secret_ref` via
     `Persistence.get_secret_value` -> env) with credential-card-shaped MISSING_KEY /
     AUTH_ERROR typed errors -- an auth subsystem the router has NO execution path
     for (AuthSpec.mode=api_key_env is declarative only; no secret_ref/Persistence/
     credential-shaped-error machinery). openaq additionally: paginated locations +
     per-location latest + sensor->parameter join. Auth handshake beyond declarative
     reach -> defer per the recipe.

4. RIDER (campaign hygiene): the experiment's twin-A/B machinery
   (`experiments/fetcher_fold_replication/run.py` + `drivers.py`) imported the
   wave-1/2 PILOT twins, DELETED at promotion (ADR 0038/0039) -- so those imports
   no longer resolve. Both retired; the per-wave self-contained drivers
   (`drivers_wave3.py`, `drivers_wave4.py`) + `harness.py` + `results/` verdicts stay
   as the record. A dated `README.md` states the harness's job is now spec-vs-live-
   twin at fold time only.

Consequence: the CO-OPS currents twin is gone as code (spec-served); the station
`emit: snapshot` mode now exists for future snapshot folds. Gates: replication-parity
LIVE twin-vs-router 28/28 (`drivers_wave4.py`); router + promotion suites green
(currents added to `test_router_promotion` + the migrated `coops_currents_select`
transform tests; twin test file deleted); offline FAILED set unchanged (9:
fetch_resolution x4 + river_dye x5); registry unchanged with fetch_noaa_coops_currents
spec-served under `_router._promoted`, every name accounted (no dupes); retrieval
index unshifted (7/7 currents corpus phrasings top-8, docstring byte-verbatim);
daemon import clean. No flood canary: currents is NOT a sfincs/flood consumer (audit;
only coops-TIDES is in `sfincs_forcing_autowire`) -- grep confirms nothing imports
it directly beyond the retired `__init__` eager import + the name-keyed `categories`
dicts (which survive). Flagged non-gating divergences (twin defects/inherent, NOT
copied): (a) observed-currents scalar speed can drift one 6-min timestep between the
twin and router calls (recorded as `info.*`; the gating value check is the
deterministic station-id SET); (b) the declarative `per_station` payload estimator
uses /1024 (twin /1000) and no 50-station cap -- calibrated safely under the 25 MB
warn floor (currents FGBs are always KB), same class as the ADR 0040 NLDI estimator
note. Related: 0036 (router core), 0038 (pilot promotion), 0039 (wave-2 ArcGIS),
0040 (wave-3 USGS dataretrieval).
