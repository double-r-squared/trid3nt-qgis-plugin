# Replication-parity VERDICT -- phase-2 wave-6 VECTOR/ZIP family (ADR 0052)

LIVE twin-vs-router over the same real NOAA OCM SLR + USACE NLD endpoints.
slr = declarative fan-out; levees = endpoint_by_param + properties_by_param.

| source | verdict | checks |
|---|---|---|
| fetch_epa_ejscreen | PASS | 30/30 |

## Per-check detail

### fetch_epa_ejscreen -- PASS
- [ok] schema.docstring_verbatim -- spec.docstring == inspect.getdoc(twin)
- [ok] pm25.values.n
- [ok] pm25.values.geom
- [ok] pm25.values.crs
- [ok] pm25.schema.columns
- [ok] pm25.values.bg_id_set
- [ok] pm25.values.value_col
- [ok] pm25.values.indicator_echo
- [ok] layer.type
- [ok] layer.style_preset
- [ok] layer.role
- [ok] layer.units
- [ok] layer.bbox_present
- [ok] diesel.values.n
- [ok] diesel.values.geom
- [ok] diesel.values.crs
- [ok] diesel.schema.columns
- [ok] diesel.values.bg_id_set
- [ok] diesel.values.value_col
- [ok] diesel.values.indicator_echo
- [ok] layer.type
- [ok] layer.style_preset
- [ok] layer.role
- [ok] layer.units
- [ok] layer.bbox_present
- [ok] empty.values.n
- [ok] empty.schema.columns
- [ok] error.upstream
- [ok] error.bad_bbox
- [ok] error.bad_indicator
