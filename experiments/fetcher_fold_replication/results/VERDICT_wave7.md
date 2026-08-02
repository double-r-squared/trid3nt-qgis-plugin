# Replication-parity VERDICT -- phase-2 wave-7 RASTER imageserver_export (ADR 0053)

LIVE twin-vs-router over the same LANDFIRE LF2022 ImageServer exportImage.

| source | verdict | checks |
|---|---|---|
| fetch_landfire_fuels | PASS | 32/32 |
| fetch_usfs_canopy_fuels | PASS | 32/32 |

## Per-check detail

### fetch_landfire_fuels -- PASS
- [ok] schema.docstring_verbatim -- spec.docstring == inspect.getdoc(twin)
- [ok] caveats.reproduced -- typed empty-caveat present
- [ok] fbfm40.values.band_count
- [ok] fbfm40.values.dtype
- [ok] fbfm40.values.crs
- [ok] fbfm40.values.nodata
- [ok] fbfm40.values.bounds
- [ok] fbfm40.values.min
- [ok] fbfm40.values.max
- [ok] fbfm40.values.mean
- [ok] fbfm40.layer.type
- [ok] fbfm40.layer.style_preset
- [ok] fbfm40.layer.role
- [ok] fbfm40.layer.units
- [ok] fbfm40.layer.bbox_present
- [ok] cbh.values.band_count
- [ok] cbh.values.dtype
- [ok] cbh.values.crs
- [ok] cbh.values.nodata
- [ok] cbh.values.bounds
- [ok] cbh.values.min
- [ok] cbh.values.max
- [ok] cbh.values.mean
- [ok] cbh.layer.type
- [ok] cbh.layer.style_preset
- [ok] cbh.layer.role
- [ok] cbh.layer.units
- [ok] cbh.layer.bbox_present
- [ok] error.empty
- [ok] error.upstream
- [ok] error.bad_bbox
- [ok] error.bad_layer

### fetch_usfs_canopy_fuels -- PASS
- [ok] schema.docstring_verbatim -- spec.docstring == inspect.getdoc(twin)
- [ok] caveats.reproduced -- typed empty-caveat present
- [ok] cbh.values.band_count
- [ok] cbh.values.dtype
- [ok] cbh.values.crs
- [ok] cbh.values.nodata
- [ok] cbh.values.bounds
- [ok] cbh.values.min
- [ok] cbh.values.max
- [ok] cbh.values.mean
- [ok] cbh.layer.type
- [ok] cbh.layer.style_preset
- [ok] cbh.layer.role
- [ok] cbh.layer.units
- [ok] cbh.layer.bbox_present
- [ok] cbd.values.band_count
- [ok] cbd.values.dtype
- [ok] cbd.values.crs
- [ok] cbd.values.nodata
- [ok] cbd.values.bounds
- [ok] cbd.values.min
- [ok] cbd.values.max
- [ok] cbd.values.mean
- [ok] cbd.layer.type
- [ok] cbd.layer.style_preset
- [ok] cbd.layer.role
- [ok] cbd.layer.units
- [ok] cbd.layer.bbox_present
- [ok] error.empty
- [ok] error.upstream
- [ok] error.bad_bbox
- [ok] error.bad_layer
