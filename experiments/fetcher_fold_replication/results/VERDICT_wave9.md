# Replication-parity VERDICT -- phase-2 wave-9 multi_url + gzip_object (ADR 0055)

LIVE twin-vs-router over Meta HRSL VRT (AWS) + UCSB CHIRPS archive.

| source | verdict | checks |
|---|---|---|
| fetch_hrsl_population | PASS | 17/18 |
| fetch_chirps_precipitation | PASS | 36/36 |

## Per-check detail

### fetch_hrsl_population -- PASS
- [ok] schema.docstring_verbatim -- spec.docstring == inspect.getdoc(twin)
- [ok] caveats.reproduced -- typed empty-caveat present
- [ok] hrsl.values.band_count
- [ok] hrsl.values.dtype
- [ok] hrsl.values.crs
- [ok] hrsl.values.nodata
- [ok] hrsl.values.bounds
- [ok] hrsl.values.min
- [ok] hrsl.values.max
- [ok] hrsl.values.mean
- [ok] hrsl.layer.type
- [ok] hrsl.layer.style_preset
- [ok] hrsl.layer.role
- [ok] hrsl.layer.units
- [ok] error.empty
- [ok] error.upstream
- [ok] error.bad_bbox
- [RP] error.bbox_none: twin='BBOX_REQUIRED' router='HRSL_INPUT_INVALID' -- error_code diverges: twin=BBOX_REQUIRED router=HRSL_INPUT_INVALID

### fetch_chirps_precipitation -- PASS
- [ok] schema.docstring_verbatim -- spec.docstring == inspect.getdoc(twin)
- [ok] caveats.reproduced -- typed empty-caveat present
- [ok] monthly.values.band_count
- [ok] monthly.values.dtype
- [ok] monthly.values.crs
- [ok] monthly.values.nodata
- [ok] monthly.values.bounds
- [ok] monthly.values.min
- [ok] monthly.values.max
- [ok] monthly.values.mean
- [ok] daily.values.band_count
- [ok] daily.values.dtype
- [ok] daily.values.crs
- [ok] daily.values.nodata
- [ok] daily.values.bounds
- [ok] daily.values.min
- [ok] daily.values.max
- [ok] daily.values.mean
- [ok] global.values.band_count
- [ok] global.values.dtype
- [ok] global.values.crs
- [ok] global.values.nodata
- [ok] global.values.bounds
- [ok] global.values.min
- [ok] global.values.max
- [ok] global.values.mean
- [ok] global.layer.type
- [ok] global.layer.style_preset
- [ok] global.layer.role
- [ok] global.layer.units
- [ok] error.not_available
- [ok] error.upstream
- [ok] error.empty
- [ok] error.bad_date
- [ok] error.future
- [ok] error.bad_bbox
