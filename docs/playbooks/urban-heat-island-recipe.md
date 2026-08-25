# Playground recipe: urban heat island (surface temperature by land cover)

Status: LIVE recipe (cleanup wave phase 2, 2026-08-25). Replaces the DEMOTED
`compute_urban_heat_island` atomic tool (docs/decisions/0313 window,
docs/DELETION_LEDGER.md). Both maps the question needs are already registered
fetchers; the analysis between them is per-class arithmetic over two staged
rasters, so it lives in the python playground (`code_exec_request`), not a
bespoke tool.

## Why this is a recipe, not a tool

Per the analysis-is-playground norm (atomic tools are DATA fetchers +
irreducible primitives; composed analyses live in the playground), the demoted
tool was `fetch_modis_lst` + `fetch_esri_landcover_10m` + a zonal mean, which
is doctrine's "fetch X + fetch Y + zonal + summarize" shape with land-cover
classes as the zones.

The EMIT gate that protects most of `processing/` does not hold here, and that
is the whole reason this one demotes while its five siblings do not. The tool's
map product was the MODIS LST resampled onto the 10 m land-cover grid, painted
with `style_preset="land_surface_temp_c"` - which the tool's own source called
"the fetch_modis_lst paint". `fetch_modis_lst` paints that layer itself, at its
NATIVE resolution, and since emission became automatic (ADR 0313) it reaches
the map without anyone asking. So the recipe loses no layer. It loses the
upsample, and losing it is an honesty gain: a ~1 km LST resampled to a 10 m
grid reads far more precise than the measurement is.

## The recipe

1. Fetch the two layers. Both land on the map on their own:

   ```
   fetch_modis_lst(bbox=<aoi>)              -> LST COG (deg C), style land_surface_temp_c
   fetch_esri_landcover_10m(bbox=<aoi>)     -> 10 m class COG (Esri/IO 9-class)
   ```

2. Stage them into the playground as handles and compute the per-class table +
   the delta:

   ```python
   # layer_refs={"lst": <lst_uri>, "lc": <landcover_uri>}
   import numpy as np
   from rasterio.warp import Resampling, reproject

   lc_arr = lc.read(1)
   # Put the LST on the land-cover grid (the classes are the zones, so the
   # class grid is the reference; nearest keeps class edges honest).
   lst_on_lc = np.full(lc_arr.shape, np.nan, dtype="float32")
   reproject(source=lst.read(1), destination=lst_on_lc,
             src_transform=lst.transform, src_crs=lst.crs,
             dst_transform=lc.transform, dst_crs=lc.crs,
             src_nodata=lst.nodata, dst_nodata=np.nan,
             resampling=Resampling.bilinear)

   BUILT = 7                       # Esri/IO "Built area"
   VEG = (2, 4, 5, 11)             # Trees, Flooded vegetation, Crops, Rangeland
   LABELS = {1: "Water", 2: "Trees", 4: "Flooded vegetation", 5: "Crops",
             7: "Built area", 8: "Bare ground", 9: "Snow/ice",
             10: "Clouds", 11: "Rangeland"}

   per_class, total = [], int(np.count_nonzero(lc_arr > 0))
   for code in np.unique(lc_arr[lc_arr > 0]):
       vals = lst_on_lc[(lc_arr == code) & np.isfinite(lst_on_lc)]
       if vals.size == 0:
           continue
       per_class.append({"class_code": int(code), "label": LABELS.get(int(code)),
                         "mean_lst_c": round(float(vals.mean()), 2),
                         "min_lst_c": round(float(vals.min()), 2),
                         "max_lst_c": round(float(vals.max()), 2),
                         "pixel_count": int(vals.size),
                         "area_share": round(vals.size / total, 4)})

   def _mean(codes):
       v = lst_on_lc[np.isin(lc_arr, codes) & np.isfinite(lst_on_lc)]
       return float(v.mean()) if v.size else None

   built, veg = _mean([BUILT]), _mean(list(VEG))
   result = {"per_class_lst_c": sorted(per_class, key=lambda r: -r["mean_lst_c"]),
             "built_mean_lst_c": built, "vegetation_mean_lst_c": veg,
             "uhi_delta_c": (round(built - veg, 2)
                             if built is not None and veg is not None else None)}
   ```

3. Narrate `uhi_delta_c` against the two sides that produced it. When either
   side is absent the delta is `None` - say so; do not substitute a different
   pair of classes to manufacture a number.

## Honest LOSSES vs the demoted tool (and the workarounds)

1. **The 10 m-resampled LST COG.** The tool wrote its resampled array back out
   as a styled COG; the playground cannot write to the object store. The LST
   layer on the map is now `fetch_modis_lst`'s own, at its native resolution.
   As above, this is a fidelity gain dressed as a loss.
2. **The typed `uhi_delta_c is None` honesty note.** The tool emitted a
   specific sentence naming which side was missing. The recipe's `None` carries
   the same fact but the narration is the model's job - step 3 states the rule.
3. **The AOI size clamp** (`_MAX_AOI_DEG = 1.0`, `_MAX_GRID_PX = 4096`). The
   fetchers still clamp their own pixel budgets, so the failure mode is a
   fetcher refusal rather than an in-tool one; a continent-sized AOI refuses
   earlier, not later.
