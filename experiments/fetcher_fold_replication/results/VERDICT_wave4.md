# Replication-parity VERDICT -- phase-2 wave-4 station family (ADR 0045)

LIVE twin(urllib) vs router(httpx, `emit: snapshot` + `coops_currents`
transform) over the same real CO-OPS endpoints. Twin behavior is the
contract. Gating value check = station-id SET (observed scalars drift a
timestep between calls, recorded as info.*).

| source | verdict | checks |
|---|---|---|
| fetch_noaa_coops_currents | PASS | 28/28 |

## Per-check detail

### fetch_noaa_coops_currents -- PASS
- [ok] schema.docstring_verbatim -- spec.docstring == inspect.getdoc(twin)
- [ok] obs.values.n
- [ok] obs.values.geom
- [ok] obs.values.crs
- [ok] obs.schema.columns
- [ok] obs.values.station_id_set
- [ok] info.obs.speed_spotcheck -- non-gating: observed speeds may drift 1 timestep between twin/router calls
- [ok] layer.type
- [ok] layer.style_preset
- [ok] layer.role
- [ok] layer.units
- [ok] layer.bbox_present
- [ok] pred.values.n
- [ok] pred.values.geom
- [ok] pred.values.crs
- [ok] pred.schema.columns
- [ok] pred.values.station_id_set
- [ok] info.pred.speed_spotcheck -- non-gating: observed speeds may drift 1 timestep between twin/router calls
- [ok] layer.type
- [ok] layer.style_preset
- [ok] layer.role
- [ok] layer.units
- [ok] layer.bbox_present
- [ok] caveats.reproduced -- typed COOPS_CURRENTS_EMPTY caveat present
- [ok] error.empty
- [ok] error.upstream
- [ok] error.bad_bbox
- [ok] error.bad_product
