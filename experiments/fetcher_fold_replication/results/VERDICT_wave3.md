# Replication-parity VERDICT -- phase-2 wave-3 USGS water-data family (ADR 0040)

LIVE twin(urllib+bespoke) vs router(dataretrieval) over the same real USGS
endpoints. Twin behavior is the contract.

| source | verdict | checks |
|---|---|---|
| fetch_usgs_water_quality | PASS | 18/18 |
| fetch_nhdplus_nldi_navigate | PASS | 20/20 |

## Per-check detail

### fetch_usgs_water_quality -- PASS
- [ok] schema.docstring_verbatim -- spec.docstring == inspect.getdoc(twin)
- [ok] values.n
- [ok] values.geom
- [ok] values.crs
- [ok] schema.columns
- [ok] values.site_id_set
- [ok] values.value_spotcheck -- sorted latest-per-site numeric values
- [ok] layer.type
- [ok] layer.style_preset
- [ok] layer.role
- [ok] layer.units
- [ok] layer.bbox_present
- [ok] caveats.reproduced -- typed WQP_NO_SITES empty caveat present
- [ok] error.empty
- [ok] error.bad_characteristic
- [ok] error.upstream
- [ok] error.bad_bbox
- [ok] error.bbox_too_large

### fetch_nhdplus_nldi_navigate -- PASS
- [ok] schema.docstring_verbatim -- spec.docstring == inspect.getdoc(twin)
- [ok] values.n
- [ok] values.geom
- [ok] values.crs
- [ok] schema.columns
- [ok] values.comid_set
- [ok] layer.type
- [ok] layer.style_preset
- [ok] layer.role
- [ok] layer.units
- [ok] layer.bbox_present
- [ok] caveats.reproduced -- typed NHDPLUS_NLDI_EMPTY caveat present
- [ok] values.seed_snap_n -- seed_point snap->navigate feature count parity
- [ok] error.empty
- [ok] error.upstream
- [ok] error.both_seeds
- [ok] error.neither_seed
- [ok] error.bad_direction
- [ok] error.bad_distance
- [ok] error.seed_outside_conus
