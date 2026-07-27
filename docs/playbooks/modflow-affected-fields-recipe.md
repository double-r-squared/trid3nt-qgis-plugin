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
- the zonal field-scoring half is the registered `analyze_affected_fields`
  primitive (or `compute_zonal_statistics` for a generic raster-over-polygon
  summary).

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

3. In the python playground (`code_exec_request`), call the registered
   `analyze_affected_fields(plume_layer_uri=<plumes[0].uri>,
   fields_layer_uri=<fields uri>, threshold_mgl=<optional>, rank_by="peak")`.
   It returns the ranked affected fields (`field_id`, `crop_name`,
   `max_concentration_mgl`, `mean_concentration_mgl`, `area_km2`) plus the
   `worst_field` + a `headline`. For a generic raster-over-polygon summary with
   no plume semantics, call `compute_zonal_statistics` directly instead.

### Sketch

```python
# after: door -> modflow_contaminant_plume returned `plume_result`
plume_uri = plume_result["plumes"][0]["uri"]
fields = fetch_field_boundaries(bbox=aoi_bbox)          # or a place clip
affected = analyze_affected_fields(
    plume_layer_uri=plume_uri,
    fields_layer_uri=fields["uri"],
    rank_by="peak",
)
# affected["affected_fields"] is the ranked readout; affected["worst_field"] the headline
```

## Honesty notes

- The plume is a demo-aquifer, planning-grade envelope (narrated by the
  `modflow_contaminant_plume` result + the `run_modflow` door fidelity brief),
  not a calibrated site model.
- A plume that never reaches any field yields `affected_fields=[]` with a valid
  zero-headline (never a fabricated hit).
