# ADR 0227 - Bathymetry-consuming templates surface their fetched topobathy as a Case input layer

Status: Accepted
Date: 2026-08-12

## Context

The flood templates already surface their fetched INPUT data as individual Case
layers so a user can spot-check, in QGIS, exactly which data served a run:
`model_flood_scenario` (SFINCS) publishes its fetched DEM/topobathy + NLCD
landcover as `role="input"` raster layers and its NHDPlus rivers as a
`role="input"` vector, via the shared `publish_input_layer` seam that the ADR
0208 wave established for mesh previews and the surge-track overlay.

The bathymetry-consuming coastal templates did NOT do the same. They fetched a
topo/bathy COG, sampled it onto their mesh nodes, and then DISCARDED the fetched
object -- the run's most load-bearing terrain input was invisible on the map.
The spot-check value is precisely seeing WHICH data (CUDEM 1/9" nearshore
composite, ETOPO 2022 shelf base, or a 3DEP land-DEM fallback) and at what native
resolution actually fed the solve.

Affected consumers:

- `schism_pahm_surge` and `schism tidal_hydro` coastal_tin both call the shared
  `_fetch_bathymetry_cog` helper (defined in `tidal_hydro.py`), which downloaded
  the fetched `s3://` COG to a temp file for `rasterio` sampling and returned only
  `(local_path, source_label)` -- the `s3://` object URI was thrown away.
- `geoclaw_inundation` fetches its seamless land+bathy DEM via
  `_fetch_topo_for_geoclaw`, reprojects it to EPSG:4326, stages it for the worker,
  and likewise never surfaced it.
- `sfincs_flood` ALREADY surfaces its DEM/bathy (the `continuous_dem` raster-input
  round-trip at `model_flood_scenario`) -- the cited precedent, left unchanged.

## Decision

Surface the fetched bathymetry the SAME way the flood DEM path does: after a
successful fetch, publish the EXISTING COG object as a `role="context"` Case input
layer with the `continuous_dem` elevation ramp and the provenance in the layer
name.

### 1. One reusable raster-input seam

Added `publish_raster_input_cog(emitter, *, cog_uri, layer_id, name,
style_preset, role="context", fallback_note=None)` to
`emission/layer_uri_emit.py` -- the raster twin of `publish_input_layer` for a COG
that is not yet registered with the render bridge. It rounds the `s3://` COG
through `publish_layer` (which registers the layer's style and returns a plugin-
renderable uri -- the same round-trip the SFINCS DEM-input path does inline), then
builds a `role` LayerURI and hands it to `publish_input_layer`. It rides the object
ALREADY in the runs bucket / cache (NO re-upload; the ADR 0208 r2d pattern) and is
BEST-EFFORT: it NEVER raises -- every failure (no emitter, a falsy uri, a
`PublishLayerError`, the emit guardrail dropping it) is swallowed with a WARNING and
returns `False`, so a failure to surface an input can never fail the solve.

### 2. Style preset: reuse, never invent

The surfaced layer uses `continuous_dem` -- the SAME elevation/hypsometric ramp the
`fetch_topobathy` hook stamps (`topobathy.py._STYLE_PRESET`) and the SFINCS DEM-
input path uses. No bespoke bathymetry ramp was invented; `continuous_dem` resolves
to the "Elevation" QGIS style and renders a topobathy COG as a hypsometric surface.

### 3. Provenance in the layer name

The layer name carries source + native resolution -- the spot-check fact:

- surge / tidal: `Input: bathymetry (topobathy, native CUDEM 1/9", ETOPO shelf base)`
  (or the explicit-coarsened `~<N> m [<basis>]` cell when a resolution was declared).
- geoclaw: `Input: bathymetry (topobathy (CUDEM 1/9" + ETOPO 2022 seamless))` on the
  primary path, `... (3DEP 10 m DEM (land-only fallback))` on the fallback, or
  `... (user-supplied topo/bathy DEM)` when a `dem_uri` was passed.

`_fetch_bathymetry_cog` now returns `(local_path, source_label, cog_s3_uri)` and
`_fetch_topo_for_geoclaw` returns `(s3_uri, source_label)` so each composer has both
the object to ride and the honest source string.

## Consequences

- A user re-seeding any of the three templates gets a `continuous_dem` bathymetry
  input layer under the primary result in QGIS, provenance-named, for spot-checking.
- No new upload cost: the surfaced object is the same COG the fetch already staged.
- The offline-first honesty gate (0217 track-overlay lesson): a unit test asserts a
  valid input LayerURI actually reaches the emitter (`role="context"`,
  `continuous_dem`, provenance name, the rode-through uri) so the emission can never
  silently drop, plus a composer-level test that `schism_pahm_surge` invokes the
  seam after a real fetch. Best-effort failure paths (`PublishLayerError`, no
  emitter, falsy uri) are pinned as non-fatal.
- `sfincs_flood` is the precedent, not a duplicate: it keeps its own inline DEM/
  landcover round-trip. Only the bathymetry-consuming templates that lacked the
  behavior adopted the new seam.

## Rejected alternatives

- A bathymetry-specific colormap preset: rejected -- `continuous_dem` already IS the
  elevation ramp; a new preset would be invented surface with no fidelity gain.
- Re-uploading the COG under a dedicated input key: rejected -- the object already
  lives in the runs bucket; publishing the existing object is the ADR 0208 rule.
- Surfacing as `role="input"` (the flood default): chose `role="context"` to match
  the sibling schism mesh-preview and surge-track overlays on these same templates
  (a terrain backdrop, not a competing answer); both render non-intrusively beneath
  the primary result with no competing zoom-to.
