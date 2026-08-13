# ADR 0112 -- fetch_noaa_nwm_streamflow fold (FETCHER FINALE ENDGAME -- the last coded data-fetcher)

Status: accepted (2026-08-04)
Follows: ADR 0069 (the multi-source-COMPOSITE STOP that named the exact blocker resolved
here), 0110 (the fetch-time provenance channel + the whole-tool library_delegate fold
pattern, topobathy), 0111 (the storm_tracks / goes_satellite folds -- the whole-tool
library_delegate proven a THIRD time for multi-round I/O + a join, and its open issue naming
nwm as the last coded data-fetcher), 0097 (the FetchError delegate passthrough), 0074 (the
delegate as the ONE sanctioned socket-owning impurity), 0056 (the "no new machinery for one
source" bar). FETCHER FINALE ENDGAME -- the universal-ingest campaign completes.

## Context

`fetch_noaa_nwm_streamflow` was the LAST coded data-fetcher in the fetchers package
(campaign coded-data-fetcher counter 1). ADR 0069 STOP-RULED its fold on a precise blocker:
it is a MULTI-SOURCE COMPOSITE -- resolve the NWM S3 channel_rt key -> download the
whole-object netCDF -> an xarray `{feature_id: streamflow}` LOOKUP DICT + an NLDI 5x5-grid
spatial sample (25 point-snap requests) -> COMIDs + per-reach NLDI geometry (up to 500
requests) + a `feature_id` JOIN -> a point FlatGeobuf -- and "no router MODE fetches-a-lookup
-dict + spatially-samples-a-2nd-API + joins". This wave resolves that STOP and lands the
fold, completing the arc.

## Decision -- the fold (the design choice, judged honestly)

DESIGN CHOICE: **option (a), the whole-tool `library_delegate` VECTOR spec** (the
now-FOURTH-proven precedent: topobathy ADR 0110, storm_tracks + goes_satellite ADR 0111).
NOT option (b), genuinely new composite machinery.

The 0069 STOP asked for a router MODE that fetches-a-dict + samples-a-2nd-API + joins. The
finale's insight (the topobathy/storm_tracks verdict, ADR 0074): the delegate hook is the
ONE sanctioned socket-owning impurity, so ALL of that composite -- the S3 whole-object read,
the netCDF parse, the 25-probe NLDI sample, the up-to-500 geometry fetches, AND the
in-process join -- is expressed as ORDINARY delegate I/O the delegate OWNS, exactly as the
twin did. The join is plain in-process computation (a dict lookup per discovered COMID);
each NLDI probe is an INDEPENDENT best-effort request (a failed probe returns None and the
sample continues -- the twin's contract), so NO transport-layer coalescing / retry semantics
the delegate cannot reach are needed. The delegate shape hits NO wall. A new
`composite-join` executor phase would be new machinery for ONE source -- the ADR 0056 bar,
which the whole-tool delegate clears at LOC ~0 of new executor code. Characterized honestly:
option (b) was considered and rejected because the request fan-out is sequential best-effort,
NOT a coalesced/retried transport pattern.

`fetch_noaa_nwm_streamflow` folds onto `source.yaml` (`shape: vector-fgb`,
`ingest.access: library_delegate`) + three hooks in `hooks/nwm_streamflow.py`:

- `nwm_streamflow.validate` (delegate_validate) -- the twin's own CONUS-intersect gate over
  its more-generous NHDPlus domain envelope `(-130, 20, -60, 55)` (DISTINCT from the generic
  gridmet-bounds `conus_only` gate, so it stays in the hook, not the gate), the `short_range`
  cross-param rule (forecast_hour >= 1), and the `valid_time` ISO parse -- shape checks the
  declarative param surface cannot express, raised pre-cache / pre-network as
  `NWMStreamflowInputError`. The router's generic param validation already stamps
  `NWM_STREAMFLOW_INPUT_ERROR` for bbox shape/range/degenerate + the `product` enum + the
  `forecast_hour` int range [0, 18] (via `error_prefix=NWM_STREAMFLOW`), so those codes are
  identical either path.
- `nwm_streamflow.read` (delegate) -- OWN every network round: resolve the NWM cycle key,
  download the channel_rt netCDF, parse the `{feature_id: streamflow}` lookup, sample the
  5x5 NLDI grid for bbox COMIDs, fetch each reach's LineString geometry, and JOIN streamflow
  + geometry -> point GeoJSON features (props `feature_id` / `streamflow_cms` / `valid_time`
  / `product`, the SFINCS-adapter + telemac river_dye consumed shape) for the shared
  `vector_fgb` serializer, and RECORD the fetch-time provenance (the resolved NWM reference
  time + reach count + NLDI COMID-discovery count) via the ADR 0110 channel.
- `nwm_streamflow.envelope` -- the twin's EXACT `nwm-streamflow-{product}-{seed}` layer_id
  (seed = sha256 of `{bbox-4dp-tag}-{product}-{valid_time-or-latest}-{forecast_hour}`,
  recomputed deterministically from the validated params) + the
  `NWM streamflow -- <product> (<latest|valid_time>[ +fNNN])` name, plus the reference-time /
  reach-count / NLDI-sample provenance replayed from the channel (a pre-channel cache object
  -> the declared defaults hold, byte-identical to the twin's own cache-hit shape, which lost
  the resolved cycle because `fetch_fn` did not re-run).

`NWMStreamflowLayerURI` (fields `product` / `reference_time` / `reach_count` /
`nldi_comids_discovered`) joins `contracts/execution.py` `LAYER_RESULT_MODELS`. The twin
returned a plain `LayerURI`; the subclass exists so the envelope hook can override the twin's
layer_id / name AND surface the fetch-time provenance (the `reference_time` in particular is
UNRECOVERABLE from the produced FGB's per-feature `valid_time` on a cache hit). The
`NWMStreamflow*Error` classes move to `hooks/nwm_streamflow.py` with base `FetchError` so
`library_delegate.invoke`'s `except FetchError: raise` passthrough preserves the pinned codes
(`NWM_STREAMFLOW_INPUT_ERROR`, `_UPSTREAM_ERROR`, `_NOT_AVAILABLE`, `_EMPTY`) through the
delegate wrapper. Output stamps stay twin-identical (`style_preset="nwm_streamflow"`,
`role="primary"`, `units="m^3/s"`, `layer_type="vector"`).

The EDGE MATRIX is preserved EXACTLY, each typed + honest: out-of-CONUS ->
`NWM_STREAMFLOW_INPUT_ERROR` (validate hook); no-reaches-found -> `NWM_STREAMFLOW_EMPTY`
(the delegate raises a typed error before returning features -- never a fabricated
header-only FGB); NWM-object-missing/stale -> `NWM_STREAMFLOW_NOT_AVAILABLE`; NLDI-unavailable
-> the twin's exact best-effort behaviour is preserved (each failed probe is swallowed to
None; if EVERY probe fails the AOI collapses to `NWM_STREAMFLOW_EMPTY` -- the twin's contract,
not "fixed" in a parity fold). Cacheable semantics twin-identical: the cache key is the
validated `{bbox-6dp, product, valid_time-when-supplied, forecast_hour}` under `dynamic-1h`,
so a `valid_time=None` (latest) request refreshes hourly via the TTL bucket and the delegate
resolves the actual cycle INSIDE the fetch (no pre_resolve -- the twin cached under LATEST,
never the resolved cycle). Payload estimate: `per_feature` (~100 reaches/deg^2, 0.1 KB each).
Docstring carried VERBATIM into the spec (retrieval unshifted). Metadata SPEC-IDENTITY
(source_class=nwm_streamflow, dynamic-1h, cacheable, supports_global_query=False).

## Consequences

- Twin `fetch_noaa_nwm_streamflow.py` DELETED (~869 LOC). New: `hooks/nwm_streamflow.py`
  (the delegate body relocated) + `source.yaml`. One result model added to
  `contracts/execution.py` `LAYER_RESULT_MODELS` (`NWMStreamflowLayerURI`).
- Registry UNCHANGED at 177 (one twin died, one spec took its name). spec-served 94 -> 95.
  coded tools 81 -> 80; coded fetchers (package) 3 -> 2 -- the 2 that remain
  (`geocode_location`, `lookup_precip_return_period`) are NON-data-source lookups/primitives
  (a name->coord geocode + a scalar Atlas-14 design-storm depth), NOT layer-producing data
  ingests. THE CAMPAIGN COMPLETES: coded DATA-fetchers in the fetchers package = 0; every
  data source is spec-served through the router (campaign coded-data-fetcher counter 1 -> 0,
  the arc 98 -> 0, with the permanent dispositions: fetch_nexrad_reflectivity RELOCATED to
  tools/display/ as show_nexrad_radar (ADR 0095, a WMS-URL overlay, fetches nothing);
  fetch_cama_flood_discharge DELETED (ADR 0095, US-only doctrine + NWM covers US)).
- Offline baseline preserved at EXACTLY 9 by SET (`test_fetch_resolution_gate` x4 +
  `test_run_river_dye_scenario` x5). The 5 river_dye members stay identical-in-kind (their
  NWM discharge resolution goes through the name-preserved `TOOL_REGISTRY` closure, ADR 0102
  seam-1, unbroken; the failures are the pre-existing stale `_fake_publish` mock-signature
  TypeErrors + the broken-input-validation tests, captured before / diffed after).
- `test_catalog_surfacing` spec-count pins updated (n_specs 94 -> 95; arm-ON declarable
  delta -93 -> -94; pool index 93 -> 94). Retrieval unshifted (docstring carried byte-verbatim
  into the spec; the 8 corpus queries surface `fetch_noaa_nwm_streamflow` at rank 1 top-8).
  Daemon boot clean (registry 177).
- Tests migrated: `test_fetch_noaa_nwm_streamflow.py` -> `test_router_nwm_streamflow.py`
  (pure hook/helper tests offline + end-to-end drives through the promoted router closure
  with the delegate's network leaves monkeypatched + `fake_s3`, incl. the provenance
  cache-hit replay proof). 24 passed.
- CONSUMERS: `sfincs_forcing_autowire` re-pointed from the deleted direct import
  (`fetch_noaa_nwm_streamflow(bbox)`) to the registry closure
  (`TOOL_REGISTRY["fetch_noaa_nwm_streamflow"].fn(bbox=bbox)`, keyword-only, envelope
  unchanged). IMPORT-ONLY, grep-verified NO flood-seam file (flood.py / inundation.py /
  wave_field.py) touched, so NO flood canary was mandated. `telemac river_dye`'s ADR 0102
  discharge seam already resolved through `TOOL_REGISTRY["fetch_noaa_nwm_streamflow"].fn` --
  verified unbroken.
- The DELETION_LEDGER `fetch_noaa_nwm_streamflow fold` row resolves to DELETED (the 0069
  STOP chain closes -- the LAST fetcher-fold row).

## Open issues

None -- the universal-ingest / fetcher-fold campaign is complete (coded data-fetchers = 0).
The pre-existing `goes_satellite` display-string em-dash follow-up (ADR 0111, queued for a
future display-string sweep) is unrelated to this fold.
