# Playground recipe: which farm fields a groundwater plume reaches

Status: LIVE recipe (engine-door refactor, MODFLOW pilot, 2026-07-26).
Replaces the CUT `run_model_contamination_affected_fields` composer.

## Why this is a recipe, not a tool

The old `run_model_contamination_affected_fields` composer bundled two distinct
things: (1) a MODFLOW contaminant plume, and (2) a zonal field-scoring analysis
over that plume. Per the analysis-is-playground norm (atomic tools are DATA
fetchers + irreducible primitives; composed analyses live in the python
playground), the composer is CUT:

- the plume half IS the `modflow_contaminant_plume` template (single OR multi
  species), and
- the zonal field-scoring half is a raster-over-polygon zonal pass composed in
  the playground (the generic pattern lives in
  docs/playbooks/zonal-statistics-recipe.md; both `analyze_affected_fields` and
  `compute_zonal_statistics` were culled to the playground).

The model composes them in `code_exec_request` (the python playground), which is
flexible + auditable and needs no bespoke composer.

## The recipe (select-then-call + compose in the playground)

1. Call the `run_modflow` door, then call the `modflow_contaminant_plume`
   template with the spill point + contaminant (+ release rate + duration). It
   returns `plumes[]`; take `plumes[0].uri` (a concentration COG, mg/L,
   EPSG:4326).

2. Fetch the field boundaries for the AOI with `fetch_field_boundaries`
   (FTW / fiboa FlatGeobuf; each feature carries `crop_name`). Take its
   `LayerURI.uri`.

3. In the python playground (`code_exec_request`), stage the plume COG + the
   fields vector as `layer_refs` and rasterize each field polygon over the plume
   to score it (the per-zone stats pattern in
   docs/playbooks/zonal-statistics-recipe.md). Rank the fields by peak (or mean)
   concentration and keep the crop name.

### Sketch

```python
# after: door -> modflow_contaminant_plume returned `plume_result`
# staged as layer_refs={"plume": plumes[0]["uri"], "fields": fields["uri"]}
import numpy as np
from rasterio.features import rasterize

src, gdf = plume, fields
if src.crs is None or gdf.crs is None:
    raise ValueError("CRS_MISMATCH: plume or fields CRS missing")
if gdf.crs != src.crs:
    gdf = gdf.to_crs(src.crs)
conc = src.read(1).astype("float64")
nod = src.nodata
valid = ~np.isnan(conc) if nod is None else ((conc != nod) & ~np.isnan(conc))

scored = []
for _, row in gdf.iterrows():
    burned = rasterize([(row.geometry, 1)], out_shape=conc.shape,
                       transform=src.transform, fill=0, dtype="uint8")
    px = conc[(burned == 1) & valid]
    if px.size:
        scored.append({"crop_name": row.get("crop_name"),
                       "max_mgl": float(px.max()), "mean_mgl": float(px.mean())})
scored.sort(key=lambda f: f["max_mgl"], reverse=True)
result = {"affected_fields": scored, "worst_field": scored[0] if scored else None}
```

## Honesty notes

- The plume is a demo-aquifer, planning-grade envelope (narrated by the
  `modflow_contaminant_plume` result + the `run_modflow` door fidelity brief),
  not a calibrated site model.
- A plume that never reaches any field yields `affected_fields=[]` with a valid
  zero-headline (never a fabricated hit).
