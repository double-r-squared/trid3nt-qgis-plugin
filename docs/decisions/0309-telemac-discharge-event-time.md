# 0309 - telemac_do_sag / telemac_river_dye: event_time + cycle-pinned discharge provenance

Decision: both TELEMAC river templates declare `Param("event_time")`
(`door=QUESTION`, optional, `derived_when_absent`) - the storm/event moment
("during last Tuesday's storm") the shared `resolve_carrier_discharge` reads
the NOAA National Water Model carrier-discharge cycle at. Accepts an ISO date
or datetime (`coerce_event_time`, the outfall-coordinate precedent: a
malformed value REFUSES typed at wire-arg normalization, never silently reads
the latest cycle instead). Unset reads the most recent published cycle; the
NWM PDS bucket retains only ~30 days, so a deeper request is a documented
typed-refusal gap, not a new source added this wave.

`event_time` threads to `fetch_noaa_nwm_streamflow`'s existing `valid_time`
param (already declared; no fetcher change). `resolve_carrier_discharge` and
`_nwm_nearest_streamflow` now return the RESOLVED cycle (`reference_time` +
`product`) alongside the discharge value, and every downstream record pins
it, never the bare request word:

- the carrier-discharge `note` (e.g. `"... NOAA National Water Model, nearest
  reach to the seed, analysis_assim @ 2026-08-24T01:00Z)"`) - "latest" never
  appears unpinned;
- the `discharge_m3s` `SyntheticInput` provenance row on BOTH templates'
  published layer (river_dye already carried one; `publish_do_products`
  gained a `carrier_discharge` param and now carries one too - a gap this
  wave closed, not a pre-existing behavior);
- the run's persisted `metrics.json` (`_physical_answer` on both templates
  now reads the row back: `discharge_m3s` / `discharge_note`);
- a new context "station" layer (`Input: NWM discharge station (<product> @
  <cycle>)`) - `_nwm_nearest_streamflow` fetches with `visualize=False`
  (suppressing the generic auto-emission, which only knows the REQUESTED
  time) and `resolve_carrier_discharge` publishes this one instead, through
  the existing `publish_input_layer` seam (the bed-bathymetry / oil-slick
  precedent, `products.py`). This layer's `.name` is what a proof-panel
  caption renders verbatim.

An out-of-retention `event_time` is a `_nwm_nearest_streamflow` miss like any
other; `resolve_carrier_discharge`'s typed refusal names the ~30-day bound
when `event_time` was set, so the retention gap reads as a documented limit
rather than a generic "no coverage" message.

## Consequence

An explicit `discharge_m3s` still short-circuits the fetch entirely (never
calls the NWM lookup, never surfaces a station layer) - `event_time` only
governs the fetched path. A user-revised discharge at the `user_gated` review
gate (do_sag) clears `reference_time`/`product` on the revised row: a
hand-typed value is no longer the fetched cycle it started from, and carrying
the stale cycle forward would misdescribe it.

No change to `fetch_noaa_nwm_streamflow`'s `source.yaml` - `valid_time` /
`product` / `forecast_hour` were already declared; this wave is a consumer.

## Bug found + fixed during live verification

The live A/B (a `latest` run vs an explicit `event_time` 5 days back) surfaced
a real correctness bug in `nwm_streamflow._load_streamflow_by_feature`: the
downloaded netCDF's `time` coordinate is `datetime64[ns]`, and `.item(0)` on
that dtype degrades to a plain Python `int` (nanoseconds since epoch - Python's
`datetime` has no ns resolution). The old code guarded with
`hasattr(t0, "astype")`, which an `int` never has, so the guard silently
skipped the whole conversion on EVERY call and fell through to the
`datetime.now()` fallback - a historical `event_time` request reported "now"
as its resolved cycle, not the cycle it actually read. Fixed by round-tripping
through `numpy.datetime64(t0, "us")` (the precision Python's `datetime` always
supports) regardless of the source array's dtype. Confirmed by direct
inspection of a real downloaded NWM file (`time` coord present, dtype
`datetime64[ns]`) and pinned with a network-free regression test
(`test_load_streamflow_reads_the_real_nc_time_coordinate`,
`tests/test_router_nwm_streamflow.py`) that round-trips a real xarray Dataset
through `netcdf4` to reproduce the exact dtype. Out of `source.yaml`'s scope
but squarely load-bearing for THIS wave's honesty claim, so fixed here rather
than deferred.
