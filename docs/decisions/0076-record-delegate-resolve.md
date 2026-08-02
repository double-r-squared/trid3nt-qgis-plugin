# 0076 - Record-return output shape + socketed delegate_resolve: wfigs folded, HRRR/cluster-B re-scoped

Context: ADR 0075 named two small, precise mechanisms as the unblock for its
STOP-RULEd residuals: `hooks.delegate_resolve` (a socketed pre-cache-key resolve)
for the HRRR-Zarr pair, and the `output.layer_type: "record"` dict-return shape for
the four Cluster-B "JSON-record" fetchers (wfigs / fault / population / lehd) plus the
ledger's landcover sidecar row. This wave BUILDS both mechanisms (strict no-op for the
61 priors), folds the one fetcher that genuinely fits each, and RE-SCOPES the rest with
sharper residuals discovered by reading every twin end-to-end -- three of the four
Cluster-B fetchers turn out NOT to be record fetchers at all.

Decision (2026-08-01):

## MECHANISM 1 - RECORD-RETURN OUTPUT SHAPE (built, no-op for the 61 priors)

A source whose result is a bare structured JSON dict (a discovery record, a summary),
NOT a renderable LayerURI, declares `shape: record` + `output.layer_type: record`
(+ `ext: json`) + a `hooks.record`. New pieces:

1. `SourceShape += "record"` and `OutputSpec.layer_type += "record"` (contracts).
   The cross-field validator pins `shape=record <-> layer_type=record` both ways so a
   raster/vector shape can never declare a record output and vice-versa.
2. `HookSpec.record`: `(spec, params, bodies) -> dict | None`. PURE: the router owns
   the transport (fetches the `hooks.build_request` plan(s) through the shared client)
   and the cache; the hook shapes the fetched body/bodies into the result dict.
   Returning `None` for a plan's body signals "no usable record here, try the next
   plan" -- the `executors/record.py` executor walks the build plans IN ORDER and
   stops at the first non-None dict (the wfigs Current->YearToDate short-circuit); if
   EVERY plan yields None it raises the source's typed empty/not-found error. A record
   spec with no `build_request` (a pure dict builder) calls the hook once with `[]`.
3. `route()` becomes `-> LayerURI | dict`: for a record spec it caches the JSON bytes
   via `read_through` and returns the parsed dict envelope. The HONESTY FLOOR is
   intact: the record hook raises the typed input/empty/upstream errors via the shared
   factories (so `read_through` never writes a fabricated-success sentinel), and the
   returned dict is exactly what the twin returned -- a record hook may SHAPE the dict,
   never fabricate success.
4. `registration._validate_hooks`: a record spec MUST declare `hooks.record`.

STRICT no-op for every prior spec (none are `shape: record`; `layer_type` was
raster|vector). The record executor is only reached on the new shape.

### Folded: fetch_wfigs_incident (proof-by-migration for the record shape)

The cleanest record fetcher: resolve a NAMED NIFC/WFIGS wildland-fire incident to an
authoritative point + padded AOI bbox + discovery record. Folded to `source.yaml` +
`wfigs_incident.build_request` (the token-OR `UPPER(IncidentName) LIKE` builder + the
ordered 2-endpoint plan set Current->YearToDate + the bespoke state-code / pad-degree
input validation) + `wfigs_incident.record` (best-feature-by-size selection over ONE
feed, returning None to advance to the next endpoint -- the twin's per-feed
short-circuit; + bbox-from-point + epoch->ISO discovery record). error_prefix
`WFIGS_INCIDENT`, input_error_suffix `INPUT_INVALID`, empty_error_suffix `NOT_FOUND`
reproduce all three twin codes byte-identically (INPUT_INVALID / UPSTREAM_ERROR /
NOT_FOUND). Value coverage -> `test_router_wfigs_incident.py` (21 tests: pure helpers,
the 2-plan ordered build, the end-to-end record dict, the Current->YTD short-circuit +
Current-hit-skips-YTD, the typed not-found after both feeds miss, bad-state INPUT).
Twin + `test_fetch_wfigs_incident.py` DELETED. Consumers: NONE functional -- the
satellite/frame-animation playbook references it by name (registry seam), and the
`agent/tools/__init__.py` twin import became a fold comment (auto-registered by
`register_specs_from_tree`). Catalog `n_specs` 61 -> 62. Retrieval unshifted (7/7
corpus phrasings top-8, model-free `retrieve_visible_tools`).

Non-gating divergences (wfigs): (a) the twin registered NO payload estimator (no
warning ever); the router's mandatory `payload_estimate` seam gets a tiny clipped
`bbox_area` (floor 0.02, ceil 0.2 MB) that likewise never warns -- behaviour-identical
(no #154 warning) on a tiny record. (b) the typed not-found MESSAGE text differs (the
twin counted `total_features` across feeds); the CODE (`WFIGS_INCIDENT_NOT_FOUND`) +
`retryable=False` are identical (the LayerURI-cosmetics divergence class).

## MECHANISM 2 - hooks.delegate_resolve (built, no-op for the 61 priors)

The delegate sibling of the chained-resolution `resolve_build`/`resolve_parse` (which
resolve over the router's http transport): a source whose cycle/key resolution walks a
LIBRARY socket names `hooks.delegate_resolve` -- `(spec, params, *, timeout_s) -> dict`.

- `library_delegate.resolve`: runs the hook under the SAME constraints as `invoke` (the
  declared `ingest.delegate.timeout_s`, telemetry marks it library-owned, an unmapped
  library exception -> a retryable source-stamped `*_UPSTREAM_ERROR`; a non-dict return
  is rejected as upstream).
- `route()` runs it AFTER type/gate + `delegate_validate` and BEFORE `read_through`, and
  MERGES its dict return into `params` so the resolved cycle enters the cache key (a
  `cycle=None` request would otherwise compute a non-deterministic key -- the ADR 0064e
  pre-cache-resolve precedent, over a socket instead of http).
- `registration._validate_hooks`: `delegate_resolve` requires `delegate` (each is
  meaningless without the other).

STRICT no-op (no prior spec declares it). Proven by `test_router_delegate_resolve.py`
(7 tests: the no-op path, the merge dict, the upstream backstop on an unmapped library
error, the non-dict rejection, the route()-level pre-cache-key merge, and the
registration pairing gate) over a stub raster delegate + stub resolve hook.

## STOP-RULED with sharper residuals

### fetch_hrrr_forecast + fetch_hrrr_smoke (delegate_resolve now BUILT; read hook live-blocked)

`hooks.delegate_resolve` -- the sole missing mechanism ADR 0075 named for the HRRR pair
-- is now BUILT. The remaining work is the delegate READ hook: `fsspec.get_mapper` ->
`xarray.open_zarr` -> `rioxarray` LCC->EPSG:4326 reproject + clip (+ the forecast's
`10m_wind_speed` hypot(u,v) over the two components), plus the `_resolve_cycle`
`fs.exists` backward cycle walk as the resolve hook. This is an ENTIRELY live-S3 zarr
data path: there is NO offline fixture for the zarr stores, so its byte-parity is
UNPROVABLE offline. Folding it now would delete two WORKING twins on an unprovable
parity -- a direct violation of the offline-first / value-identical bar. STOP-RULED with
the twins INTACT; deferred to a live-drive that opens the real S3 zarr (the mechanism it
needed is done, the fold is a mechanical live-parity finish). HRRR feeds no flood seam
(grep) -> no canary implicated.

### fetch_fault_sources (record shape built; needs an emptiness-switch + two-tier cache)

NOT a pure record: the twin returns a `FaultSourcesResult` (a LayerURI SUBCLASS -- a
renderable fault-trace vector with a categorical legend, role=context) on the NON-empty
path and a bare dict ONLY on empty. That is an EMPTINESS-DRIVEN output switch
(vector-envelope OR record) the record shape alone does not express; PLUS a two-tier
cache (a constant-key whole-world 10.6 MB GEM GAF GeoJSON + an AOI-keyed filtered vector
entry) `read_through`'s single key does not express; PLUS `FaultSourcesResult` (with its
legend) must move into `execution.py`'s `LAYER_RESULT_MODELS` (the HighWaterMarksLayerURI
precedent) since a spec-driven surface has no coded twin. The record shape is now a
satisfied prerequisite; the remaining named seams are the emptiness-switch + the two-tier
cache directive + the result-model migration. `resolve_fault_sources` already tolerates
both return shapes (`.faults`/`.note` off dict-or-object) so its re-point is trivial.

### fetch_population (premise REFUTED: returns a LayerURI, ACS leg half-built)

The record premise is refuted by the twin: it ALWAYS returns a `LayerURI` -- the
WorldPop leg a raster, the `acs_*` leg a `LayerURI(layer_type=vector)` pointing at a
GeoJSON. The "record" lives only INSIDE the ACS leg's cached bytes, and that leg is
explicitly HALF-BUILT (`geometry: None` "follow-up", a heuristic 15-state CONUS FIPS
table, a 9-country ISO3 table). It is a raster-vs-vector VARIANT dispatch by dataset
prefix, NOT a record. The record shape does not apply. The real fold is the WorldPop
raster leg + DROP the half-built ACS branch (a NATE flag-not-copy call) -- the
variant-dispatch wave, unchanged from ADR 0075. `compute_exposure_summary` reads only
`.uri` as a raster (WorldPop), so a registry-seam re-point is value-identical once folded.

### fetch_lehd_jobs (premise REFUTED: returns a vector choropleth, not a record)

Also refuted: the twin returns a `LayerURI(vector)` tract-choropleth FlatGeobuf -- a
TIGERweb tract-geometry leg LEFT-JOINed to a LODES WAC bulk gzip-CSV values leg on the
11-digit GEOID -- NOT a record summary. It needs the gzip-CSV-values-join-to-vector seam
(the ADR 0071 "join values-hook wave": a `gzip_object` values leg + a GEOID join into
the tract geometry), not the record shape. STOP unchanged.

### fetch_usgs_nwis_gauges (derived-output + parse-fallback; not record/delegate_resolve; canary NOT owed)

Returns a `LayerURI(vector)` in BOTH modes; the derived-mode switch (instant-overlay vs
discharge-hydrograph, chosen by window-presence) flips style_preset / units / layer_id /
COLUMNS -- all vector -- and the instant mode carries an IV-WaterML-JSON -> Site-RDB
cross-parser fallback. Neither is the record shape nor `delegate_resolve`; it needs a
derived-output selector + a parse-fallback chain (the station derived-output wave). Left
ENTIRELY untouched -> no flood-consumer seam re-pointed (`sfincs_forcing_autowire` /
`flood.py` / `sfincs_forcing_adapter` still resolve the twin) -> NO flood canary owed
(the ADR 0075 posture: a canary is owed only when the flood seam is re-pointed).

Consequence: the router now carries a record-return output shape (`route() -> LayerURI |
dict`, honesty floor intact) proven by the wfigs fold, and a socketed pre-cache
`delegate_resolve` proven by a mechanism test. The four Cluster-B "record" residuals
re-scope sharply: wfigs was the ONLY pure record fetcher; fault is an emptiness-switch
hybrid (record shape now a prerequisite); population + lehd are LayerURI fetchers whose
record premise is refuted (variant-dispatch / join-values waves); nwis is a derived-output
+ parse-fallback vector fold. Extends the tier-3 hook contract (ADR 0056/0061/0063/0071/
0073/0074/0075) with `hooks.record` + `hooks.delegate_resolve` + `output.layer_type:
record`. Supersedes the ADR 0075 fetch_wfigs_incident STOP-RULE.
