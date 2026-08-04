# ADR 0111 -- fetch_storm_tracks + fetch_goes_satellite folds (FETCHER FINALE WAVE 2)

Status: accepted (2026-08-04)
Follows: ADR 0110 (the fetch-time provenance channel + the fetch_topobathy fold),
0097 (the cross-sibling dispatch seam + the FetchError delegate passthrough), 0090
(the DEM+STORM_TRACKS STOP naming the binary-secondary-enrichment blocker), 0088
(the goes_satellite STOP naming the single-band raster-cog / most-recent / em-dash
residual). FETCHER FINALE WAVE 2: the two remaining coded data-fetchers before nwm
BOTH fold. Campaign coded-data-fetcher counter 3 -> 1.

## Context

Two never-folded coded twins remained after WAVE 1 (topobathy) besides nwm:
`fetch_storm_tracks` (ADR 0090 STOP) and `fetch_goes_satellite` (ADR 0088 STOP).
Both STOPs named a precise residual. This wave builds the minimal general thing for
each and lands the fold, reusing the ADR 0110 provenance channel (free for both) and
the ADR 0097 FetchError delegate passthrough.

## Decision 1 -- fetch_storm_tracks fold (the binary-secondary-enrichment design choice)

ADR 0090's decisive blocker: the tool is ONE registered name whose ACTIVE mode is a
resolve-then-fetch chain whose SECOND round retrieves a BINARY zip-shapefile (the NHC
`forecastTrack.zipFile`) that must be `extractall`-ed to a tempdir, read via
geopandas, and reprojected -- I/O inside what `chained_resolution`'s `enrich_merge`
hook requires be PURE (ADR 0056), which no router phase carries.

DESIGN CHOICE (judged honestly per the kickoff): NOT a new phase type in the
chained-resolution machinery, but **option (b): a `library_delegate` VECTOR spec whose
delegate hook does BOTH rounds**. Rationale (NATE simplicity doctrine): the
`library_delegate` delegate is the ALREADY-sanctioned socket impurity (ADR 0074), and
the topobathy fold (ADR 0110) is the exact precedent -- a whole-tool delegate carrying
heterogeneous binary tile I/O. Expressing the active mode's second round as more
delegate I/O adds ZERO new executor machinery, whereas a new `binary-enrich`
chained-resolution phase would be new machinery for one source (the ADR 0056 bar).
ADR 0090 characterized the whole-tool-delegate option as a "topobathy-style rejection
(LOC delta ~0)" -- but topobathy WAS folded that way in ADR 0110 (the coded twin dies,
a spec takes the name, the counter drops), so the precedent now clearly favours it.

`fetch_storm_tracks` folds onto `source.yaml` (`shape: vector-fgb`,
`ingest.access: library_delegate`) + four hooks in `hooks/storm_tracks.py`:

- `storm_tracks.validate` (delegate_validate) -- the historical-mode bbox-required
  gate (bbox is REQUIRED for historical, OPTIONAL for active -- a conditional the
  declarative `required` flag cannot express) + geometry / storm_name shape, raised
  pre-cache as `StormTracksInputError`.
- `storm_tracks.resolve` (pre_resolve) -- canonicalize `storm_name` (upper) and
  resolve the historical season window (default = the last 3 seasons) BEFORE
  read_through so both enter the cache key (a default-year request is deterministic
  per day and refreshes yearly, the twin's contract).
- `storm_tracks.read` (delegate) -- branch on `active_only` and OWN both rounds:
  HISTORICAL subsets IBTrACS v04r01 (basin CSV -> storm-wise FULL-track selection ->
  line/point features, the movebank-proven grouping); ACTIVE resolves NHC
  CurrentStorms.json then, per storm, fetches the `forecastTrack.zipFile` binary,
  extracts `*_pts.shp` to a tempdir, reads it via geopandas, and reprojects to
  EPSG:4326. Returns GeoJSON features for the shared `vector_fgb` serializer
  (`library_delegate.execute` -> `features_to_fgb_bytes`), and RECORDS the fetch-time
  mode provenance (which mode + which storms) via the ADR 0110 channel.
- `storm_tracks.envelope` -- the twin's `storm-tracks-{seed}` layer_id +
  `Storm tracks - <mode> (<scope>)` name (seed recomputed deterministically from the
  validated params) plus the mode / storm-attribution provenance replayed from the
  channel.

`StormTracksLayerURI` (fields `mode` / `storm_count` / `storm_names`) joins
`LAYER_RESULT_MODELS`. The `StormTracks*Error` classes move to `hooks/storm_tracks.py`
with base `FetchError` so `library_delegate.invoke`'s `except FetchError: raise`
passthrough preserves the pinned codes (`STORM_TRACKS_NO_STORMS`,
`STORM_TRACKS_NO_ACTIVE_STORMS`, `STORM_TRACKS_INPUT_ERROR`,
`STORM_TRACKS_UPSTREAM_ERROR`). Output stamps stay twin-identical
(`style_preset="storm_tracks"`, `role="primary"`, `units="kt / mb"`, the extent bbox
via `output.bbox_from_features {pad: 0.5}`).

CONSUMER: only `sfincs_forcing_autowire` imports it directly; re-pointed to the
registry closure (`TOOL_REGISTRY["fetch_storm_tracks"].fn`, keyword-only). Import-only,
no flood seam touched (grep-verified), so NO flood canary mandated.

## Decision 2 -- fetch_goes_satellite fold (the most-recent design choice)

ADR 0088's residual: a THIRD GOES access surface distinct from the archive per-frame
builder -- a SINGLE `LayerURI` (raster-cog shape), a SINGLE-band float32 PHYSICAL-units
COG, most-recent-frame semantics (no window; a 15-minute `valid_time` cache rounding),
a CONUS-sector pre-gate, and a non-ASCII em-dash LayerURI name.

DESIGN CHOICE (characterized first): NOT a new `raster_cog` access mode. The existing
`library_delegate` raster path (`ingest.access: library_delegate`, dem/topobathy
precedent) expresses the single-band float32 read cleanly -- the delegate returns
`(array, transform, crs)` and the shared `array_to_cog_bytes` writes the DEFLATE
float32 NaN-nodata COG (twin-format-identical). The two genuinely-declarative-defeating
pieces are handled by the EXISTING hook points, not a new mode: the 15-min `valid_time`
rounding is a `pre_resolve` hook (it must enter the cache key, so it runs BEFORE
read_through -- the dem `coarsen` precedent), and the CONUS pre-gate + bbox-required +
band/satellite checks are the `delegate_validate` hook. `pre_resolve` + `direct_window`
alone could NOT express most-recent semantics (the object key is resolved INSIDE the
fetch, and the twin's netCDF CF-scale + subdataset reproject is bespoke), so the
`library_delegate` delegate is the right seam.

`fetch_goes_satellite` folds onto `source.yaml` (`shape: raster-cog`,
`ingest.access: library_delegate`) + four hooks in `hooks/goes_satellite.py`:

- `goes_satellite.validate` (delegate_validate) -- bbox-required (`BBOX_REQUIRED`),
  band + satellite normalization (`GOES_INPUT_INVALID` on unknown), and the
  CONUS-sector honest fast-reject (`GOES_EMPTY` before the S3 round-trip -- the twin
  defined `_CONUS_SECTOR_BBOX` but never used it; the fold turns it into a real
  fast-reject, same code, earlier).
- `goes_satellite.resolve` (pre_resolve) -- round `valid_time` DOWN to the nearest 15
  minutes and normalize the satellite token INTO the cache key (the twin's exact
  caching semantics; the DEFAULT-res request's cache key is `{bbox, band, satellite,
  valid_time}`, byte-identical to the twin).
- `goes_satellite.read` (delegate) -- list the most-recent MCMIPC key, download the
  netCDF, apply the per-band CF scale/offset/`_FillValue`, reproject to EPSG:4326 over
  the bbox, return `(array, transform, crs)`, and RECORD the scan provenance
  (satellite + scan-time -- the scan-time is UNRECOVERABLE from the COG on a cache hit,
  the exact durability the channel provides).
- `goes_satellite.envelope` -- the twin's `goes-{sat}-{band}-{lon}-{lat}` layer_id and
  the `GOES Satellite <U+2014> <label> (<SAT>)` name. The display EM-DASH is preserved
  BYTE-IDENTICAL for parity (the source file stays ASCII via a `—` escape); it is
  the ONE preserved non-ASCII display string (the source file stays ASCII via a
  ``—`` unicode escape). FOLLOW-UP (one line): consider replacing the em-dash with
  an ASCII hyphen in a future display-string sweep (never silently this wave -- a fold
  does not change display strings).

`GOESSatelliteLayerURI` (fields `satellite` / `band` / `scan_time`) joins
`LAYER_RESULT_MODELS`. Per-band units ride `normalize.units_by_param`
(visible -> reflectance, ir_window / water_vapor -> K); `emit_bbox: false` (the twin's
LayerURI carries no bbox).

### The shared-substrate relocation (goes_satellite is NOT a leaf twin)

`fetch_goes_satellite.py` was a SHARED SUBSTRATE: `_normalize_satellite`, the satellite
maps, and the S3 list/download primitives were imported by `_goes_archive_core`,
`hooks/goes_archive`, `hooks/goes_animation`, and `hooks/glm`. Deleting the twin
required relocating them. NEW leaf substrate `imagery/_goes_common.py` (no registered
tool, no router/read_through/slider dependency) holds the satellite-identifier
normalizer + maps + errors + the `?list-type=2` S3 lister + the netCDF downloader +
`_doy_hour` + `_KEY_START_TIME_RE` + `_PRODUCT_PREFIX`; all four consumers re-point
there. This mirrors the ADR 0088 relocation of the archive-animation twin's body into
`_goes_archive_core`. `GOESError` base moves to `FetchError` (still a `RuntimeError`,
`isinstance` holds) so the delegate's pinned codes survive `library_delegate.invoke`.

## Consequences

- Both twins DELETED (`fetch_storm_tracks.py` ~1,251 LOC; `fetch_goes_satellite.py`
  ~854 LOC). New: `hooks/storm_tracks.py`, `hooks/goes_satellite.py`,
  `imagery/_goes_common.py` (+ the relocated substrate). Two result models added to
  `contracts/execution.py` `LAYER_RESULT_MODELS`.
- Registry UNCHANGED at 173 (2 twins died, 2 specs took their names). spec-served
  92 -> 94. Campaign coded-data-fetcher counter 3 -> 1 (only nwm remains). coded tools
  83 -> 81, coded fetchers 5 -> 3.
- Offline baseline preserved at EXACTLY 9 by SET. `test_catalog_surfacing` spec-count
  pins updated (n_specs 92 -> 94; declarable delta -91 -> -93; pool index 91 -> 93).
  Retrieval unshifted (both docstrings carried byte-verbatim into the specs; corpus
  queries top-8 for both). Daemon boot clean.
- Tests migrated: `test_router_storm_tracks.py` + `test_router_goes_satellite.py`; the
  three sibling tests that imported the twins' shared internals re-pointed
  (`test_router_goes_animation`, `test_router_goes_archive` -> `_goes_common`;
  `test_sfincs_spiderweb` -> `hooks/storm_tracks._parse_ibtracs_csv`).
- NO flood-seam edit (the autowire re-point is import-only, grep-verified), so no flood
  canary was mandated.
- The DELETION_LEDGER `fetch_storm_tracks fold` (ADR 0090 chain) + both
  `fetch_goes_satellite fold` rows (ADR 0078 / 0088 chain) resolve to DELETED.

## Open issue (inherited by the last finale wave)

`nwm` (`fetch_noaa_nwm_streamflow`) is the LAST coded data-fetcher (counter 1). It
inherits the provenance channel (ADR 0110), the FetchError delegate passthrough (ADR
0097), and the whole-tool `library_delegate` precedent proven here for a multi-source
composite. The em-dash display-string follow-up (goes name) is queued for a future
display-string sweep, not a fold concern.
