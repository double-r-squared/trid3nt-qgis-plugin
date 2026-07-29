# Playground recipe: frame animations, fire-AOI localization, news-event ingest

Status: LIVE recipe (shared-workflows cull phase A, 2026-07-29).
Replaces the CUT `run_model_goes_fire_animation` and `run_model_news_event_ingest`
composers; captures the FIRMS densest-hotspot AOI-localization snippet the
`run_model_satellite_fire_animation` composer still wraps (that composer is
retained for now -- its cull is blocked only on a FIRMS-map-key live drive, not
on any missing primitive).

## Why these are recipes, not tools

Per the analysis-is-playground norm (atomic tools are DATA fetchers + irreducible
primitives; composed analyses live in the python playground), a satellite/GOES
frame animation and a news-event ingest are STRAIGHT-LINE compositions over
already-registered primitives -- no bespoke composer earns its keep. Each
composer below was a fan-out + an honesty-floor summary; the playground
reproduces both. Live replication (2026-07-29) confirmed the fired-tool set
equals the composer's and the same layers publish.

The three retained primitives that unblock the frame-animation pattern:

- `fetch_slider_timestamps` (P1, newly registered) -- the SLIDER availability +
  cadence index: which frames EXIST for a (sat, sector, product), so a requested
  window snaps to real frames instead of guessed ones.
- `fetch_goes_animation` / `fetch_goes_blend_animation` / `fetch_viirs_day_fire`
  -- the per-frame imagery fetchers; each returns an ORDERED `list[LayerURI]`
  whose names already carry the scrubber NAME-TOKEN
  (`"GOES <Product> step <N> <ISO> (<SAT>)"`).
- `fetch_firms_active_fire` -- FIRMS hot pixels for the data-driven fire AOI.

## Recipe A -- GOES/VIIRS frame animation (replaces run_model_goes_fire_animation)

1. Resolve the AOI: `geocode_location("<place>")` -> bbox (or a canvas AOI).
2. Availability + snap: call `fetch_slider_timestamps(sat, sector, product)`
   (e.g. `("goes-19", "conus", "geocolor")`). It returns `timestamps_int`
   (ascending), `count`, `cadence_seconds`, `earliest_iso` / `latest_iso`. Take
   the last N frames of `timestamps_int` and build `start_utc` / `end_utc` from
   them -- that is the "snap the window to available frames" step.
3. Fetch frames: `fetch_goes_animation(bbox, band, satellite, sector, start_utc,
   end_utc)` (or `fetch_goes_blend_animation` for the CIRA GeoColor+FireTemp
   blend, or `fetch_viirs_day_fire` for the JPSS polar Day-Fire product). Returns
   an ordered `list[LayerURI]`; publish/emit each.
4. Scrubber: the plugin's `render/temporal.py group_frame_layers` auto-groups the
   frames by their shared name-token stem (>= 2 members with strictly-increasing
   step values) so QGIS's Temporal Controller plays them. NO web/plugin change
   is needed -- just emit the fetcher's frame names verbatim.

The honesty floor is inherited from the fetchers: an animation with zero
non-empty frames never reads `status=ok`.

## Recipe B -- data-driven fire AOI (FIRMS densest-hotspot clustering)

When the AOI should be the ACTIVE fire rather than a named place: fetch FIRMS hot
pixels over a broad region, then cluster to the densest hotspot. This ~15-line
snippet is the whole of the former `_densest_hotspot_bbox` -- run it in
`code_exec_request`:

```python
import math
def densest_hotspot_bbox(points, pad_deg=0.1, cell_deg=0.1):
    """(lon,lat) hot pixels -> a TIGHT AOI bbox around the densest cluster."""
    if not points:
        return None  # no fire detected -> keep the region bbox + honesty-floor
    cell = lambda lo, la: (int(math.floor(lo / cell_deg)), int(math.floor(la / cell_deg)))
    counts = {}
    for lo, la in points:
        c = cell(lo, la); counts[c] = counts.get(c, 0) + 1
    bc, br = min(counts, key=lambda c: (-counts[c], c[0], c[1]))  # densest; deterministic tie-break
    neigh = {(bc + dc, br + dr) for dc in (-1, 0, 1) for dr in (-1, 0, 1)}  # 3x3 so a boundary-straddling fire is whole
    cluster = [(lo, la) for (lo, la) in points if cell(lo, la) in neigh] or points
    lons = [p[0] for p in cluster]; lats = [p[1] for p in cluster]
    pad = max(0.01, float(pad_deg))
    return (round(max(-180.0, min(lons) - pad), 6), round(max(-90.0, min(lats) - pad), 6),
            round(min(180.0, max(lons) + pad), 6), round(min(90.0, max(lats) + pad), 6))
```

Feed it from `fetch_firms_active_fire(bbox=region, days_back=N)`: read the
returned FIRMS layer with geopandas, pool `(geom.x, geom.y)` for each point, then
`densest_hotspot_bbox(points)`.

AOI precedence (highest wins): explicit user bbox / canvas AOI > FIRMS densest
hotspot > WFIGS incident bbox (`fetch_wfigs_incident`) > geocoded place. Then run
Recipe A over the chosen AOI and overlay FIRMS + `fetch_nifc_perimeters` (or the
active-fire perimeter fetcher) as context layers.

## Recipe C -- news-event ingest for review (replaces run_model_news_event_ingest)

The review-gated ingest is a conversational loop, not tool plumbing: the agent
fetches sources, derives params, narrates them, and waits for the user's go
before dispatching any solver (Invariant 9 across turns).

1. Per source, dispatch by type: `web_fetch(url, extract="main_text")` for
   article URLs; `fetch_nws_event(area)` for an NWS state/county; and
   `fetch_storm_events_db(year, state)` for a Storm-Events entry. Extract the
   text for each (title + body for URLs; layer name for the structured sources).
2. `aggregate_claims_across_sources(sources=[{"url","text","fetched_at"}, ...],
   claim_targets=["location","date","scale","contaminant","casualties"])` ->
   best-supported value per target with source-agreement confidence + provenance.
3. `geocode_location(derived_location_value)` -> bbox for the review card.
4. Narrate the derived params + provenance + confidence and STOP; only after the
   user approves does a downstream solver (e.g. the `run_modflow` door) run.

Live replication (2026-07-29) fired exactly
`web_fetch` + `fetch_nws_event` + `fetch_storm_events_db` ->
`aggregate_claims_across_sources` -> `geocode_location` and produced the same
derived-param + geocoded-bbox envelope.
