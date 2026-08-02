# 0040 - fetcher-fold phase-2 wave-3 (USGS water-data family, dataretrieval-delegated)

Context: phase-2 fans the fold out family-by-family. This wave targets the USGS
water-data family with a NEW recipe element NATE approved: the router executor
DELEGATES to the official USGS `dataretrieval` client (PyPI, agency-maintained)
instead of raw HTTP + our bespoke RDB/GeoJSON/CSV parsers, so the ongoing NWIS ->
Water Data OGC API migration churn is absorbed upstream (decay-avoidance, not just
line-count). In-scope per the audit: fetch_usgs_nwis_gauges, fetch_usgs_water_quality,
fetch_usgs_groundwater_levels; plus a coverage-verify on fetch_nhdplus_nldi_navigate.
Out of scope: fetch_high_water_marks (STN, not covered by dataretrieval).

Decision (NATE authority via the wave-3 kickoff; both gates held per folded source):

1. FOLD (2): fetch_usgs_water_quality (WQP via `wqp.what_sites` + `wqp.get_results`,
   latest-numeric-per-site left-join) and fetch_nhdplus_nldi_navigate (NLDI via
   `nldi.get_features` point-snap + `nldi.get_flowlines` navigate). Both spec-served
   under the twin name (source.yaml + `ingest.delegate: {library: dataretrieval,
   service}`); twins `git rm`-ed. Probe-proven parity: WQP station set 5/5 identical,
   NLDI comid-navigate 16/16 byte-identical + seed_point snap COMID identical.

2. DEFER (2, evidence in open_issues -- STOP RULE, never guess a mapping):
   - fetch_usgs_groundwater_levels: dataretrieval.waterdata wraps the OGC
     `field-measurements` (full history) + `monitoring-locations` collections, but
     NOT the `latest-field-measurements` collection the twin depends on. The twin
     returns 1701 records over 1545 (mlid,pcode) keys for a Kansas bbox; reducing
     `field-measurements` to latest-per-`field_measurements_series_id` yields 1545
     (value multiset differs), and latest-per-(mlid,pcode) matches the keyset (1545)
     but not the twin's 1701 record cardinality. The server collection's series/latest
     granularity is unreproducible without guessing -> unclosable count+value parity.
   - fetch_usgs_nwis_gauges: BESPOKE (audit). `nwis.get_iv` returns NO coordinates
     (a mandatory second `get_info` join) and a wide format with dated-suffix series
     columns (`00065_begins 2014`) that make the twin's latest-per-parameter merge
     unreproducible on multi-series sites; plus dual-mode output (instant style/units
     vs window hydrograph), IV->Site fallback, the bbox-area gate, and it is the
     `sfincs_forcing_autowire` flood consumer. Multiple parity risks -> defer.

3. NLDI verdict: coverage COMPLETE (comid-navigate byte-clean; seed_point snap via
   `get_features(lat,long)` returns the identical COMID as the twin's
   `/comid/position`), so folded as the family's 4th source.

4. Dependency: `dataretrieval==1.2.0` pinned in `server/pyproject.toml` core
   `dependencies` (exact pin: the OGC-API surface + tabular schemas evolve).

5. Router extensions (all declarative, STRICT no-op for every prior spec):
   `ingest.delegate` dispatch in `select_executor` (before the shape dispatch);
   `point` ParamType (2-float [lon,lat], nldi seed_point); `ParamSpec.aliases`
   (wqp characteristic alias-or-passthrough) + `ParamSpec.schema_optional` (a
   None-default param annotated `X|None` so the adapter keeps it OUT of required,
   reproducing the twins' `required=[]`); `NormalizeSpec.units_from_param` (wqp
   LayerURI.units = resolved characteristic); a delegated-spec `pre_validate` hook
   in `router.route` so source-specific INPUT errors (wqp bbox-required, nldi
   seed/comid mutual-exclusion + CONUS + comid gate) raise BEFORE read_through --
   indistinguishable from the twin, offline-testable.

Consequence: the USGS WQP + NLDI twins are gone as code (spec-served); the
dataretrieval-delegating executor now exists for future USGS folds. Gates:
replication-parity LIVE twin-vs-router 2/2 across the full contract-4.2 edge matrix
(WQP 18/18, NLDI 20/20; `experiments/fetcher_fold_replication/drivers_wave3.py`);
router + promotion suites green (139 tests); registry 196 local (200 daemon)
unchanged with both names spec-served under `_router._promoted`; retrieval index
unshifted (WQP 8/8, NLDI 7/7 corpus phrasings top-8, docstrings byte-verbatim);
daemon import clean; both live-proven (WQP 5 Point sites w/ Nitrate values; NLDI 16
comid-navigate + 11 seed-snap flowlines). No flood canary: neither WQP nor NLDI is
imported by any sfincs/flood consumer (nwis_gauges, which is, was deferred). Flagged
divergence (non-gating, NLDI): the twin's payload estimator scaled with
distance/direction (UT=50/km, DM=5/km); NLDI carries no bbox, so the declarative
estimator is a flat ~0.5 MB (calibrated safely under the 25 MB warn floor -- NLDI
flowline FGBs are always KB-few MB) losing that scaling. Related: 0036 (router core),
0038 (pilot promotion), 0039 (wave-2 ArcGIS family).
