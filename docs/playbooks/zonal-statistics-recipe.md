# Playground recipe: zonal statistics (raster values within zones)

Status: LIVE recipe (processing-wave cull, 2026-07-29). Replaces the DEMOTED
`compute_zonal_statistics` atomic tool (docs/decisions/0043). Aggregating a
value raster within zones (a vector polygon set OR a threshold mask) is a
straight-line rasterio + numpy composition over already-staged Case layers, so
it lives in the python playground (`code_exec_request`), not a bespoke tool.

## Why this is a recipe, not a tool

Per the analysis-is-playground norm (atomic tools are DATA fetchers + irreducible
primitives; composed analyses live in the playground), a zonal summary is the
doctrine's literal "fetch X + fetch Y + zonal + summarize" example. Live
replication (2026-07-29) confirmed the playground path reproduces
`compute_zonal_statistics` EXACTLY: the per-zone and aggregate `count/sum/mean/
min/max/median/percentile` values matched to floating-point tolerance for both a
vector-polygon zone and a raster threshold zone, and the CRS-missing honesty
floor raised in BOTH the tool and the recipe.

## The recipe (stage layers, then compute in the playground)

Load the value raster and the zone (vector or raster) as Case layers first so
they arrive in `code_exec_request` as pre-opened handles: `layer_refs=
{"value": <value_raster_uri>, "zone": <zone_uri>}` exposes `value` (a rasterio
dataset) and `zone` (a GeoDataFrame for a vector, a rasterio dataset for a
raster), plus `value_uri` / `zone_uri` path aliases.

### Vector-polygon zones (one zone per feature)

```python
import numpy as np
from rasterio.features import rasterize

src, gdf = value, zone
# --- CRS honesty floor (see LOSSES below) ---
if src.crs is None or gdf.crs is None:
    raise ValueError("CRS_MISMATCH: value raster or zone vector CRS is missing")
if gdf.crs != src.crs:
    gdf = gdf.to_crs(src.crs)            # reproject zones onto the raster grid

band = src.read(1).astype("float64")
nod = src.nodata
valid = ~np.isnan(band) if nod is None else ((band != nod) & ~np.isnan(band))

def stats(v):
    if v.size == 0:
        return {"count": 0, "sum": None, "mean": None, "max": None}
    return {"count": int(v.size), "sum": float(v.sum()),
            "mean": float(v.mean()), "max": float(v.max())}

by_zone, pooled = {}, []
has_id = "id" in gdf.columns
for i, row in gdf.iterrows():
    zid = row["id"] if has_id else i
    burned = rasterize([(row.geometry, 1)], out_shape=band.shape,
                       transform=src.transform, fill=0, dtype="uint8")
    px = band[(burned == 1) & valid]
    by_zone[str(zid)] = stats(px)
    if px.size:
        pooled.append(px)
result = {"by_zone": by_zone,
          "aggregate": stats(np.concatenate(pooled)) if pooled else stats(np.array([]))}
```

### Raster threshold zones ("pixels >= T are in-zone")

```python
import numpy as np
src, zsrc = value, zone            # both rasterio datasets
band = src.read(1).astype("float64")
zb = zsrc.read(1).astype("float64")     # zone already on the value grid (stage aligned)
nod = src.nodata
valid = ~np.isnan(band) if nod is None else ((band != nod) & ~np.isnan(band))
px = band[(zb >= 0.5) & valid]          # 0.5 = your zone_threshold
result = {"count": int(px.size), "sum": float(px.sum()), "mean": float(px.mean())}
```

Population/building exposure ("how many people/structures in the zone") does NOT
belong here: call the registered `compute_exposure_summary` (it fetches
population + buildings and populates the Case session store `compose_case_report`
reads). Route pure vector-in-vector aggregations to `spatial_query` (DuckDB
spatial SQL). This recipe is for a raster value aggregated within a zone.

## Honest LOSSES vs the retired tool (and the workarounds)

The tool did three things the playground cannot, by design. NATE accepted these
on demotion:

1. **Arbitrary `s3://` self-staging.** The tool called `read_object_bytes_s3`
   to pull ANY `s3://` URI into its own tempdir. The sandbox is egress-denied, so
   the playground cannot fetch arbitrary object-store URIs itself.
   *Workaround:* stage both inputs as Case layers FIRST via the normal fetch/
   compute tools (`fetch_dem`, `fetch_population`, `clip_raster_to_polygon`,
   `fetch_administrative_boundaries`, ...), then pass their URIs in `layer_refs`
   -- the sandbox pre-fetches them off-loop.

2. **1h read-through cache.** The tool cached its result JSON for an hour, so a
   repeated identical query was free. The playground recomputes every run.
   *Workaround:* none needed at current volumes (a zonal pass over a staged
   raster is milliseconds-to-seconds); if a heavy zonal is re-run in a loop,
   compute it once and reuse the `result` in the same turn.

3. **Codified `CRSMismatchError` honesty floor.** The tool raised a typed error
   when a CRS was missing/unreconcilable rather than silently burning zones in
   the wrong place. An ad-hoc snippet can skip that check and mis-place zones.
   *Workaround:* KEEP the `if src.crs is None or gdf.crs is None: raise ...`
   guard (shown above) at the top of every zonal snippet, and always
   `to_crs(src.crs)` before rasterizing. This reproduces the floor exactly.
