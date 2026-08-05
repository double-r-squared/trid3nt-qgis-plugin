# 0150: GeoClaw AMR mesh as a first-class per-run product (raw grid emission)

Date: 2026-08-05
Status: landed

## Context

NATE's directive: make the GeoClaw AMR mesh a first-class per-run PRODUCT, honestly
reflecting the raw grid. GeoClaw's adaptive mesh existed ONLY inside the `fort.q`
per-patch headers (each patch: level, mx, my, xlow, ylow, dx, dy). The inundation
templates emitted depth/eta rasters but never surfaced the mesh itself, so a user
could not see WHERE the solver actually refined - the very thing an
`amr_regions` run is about. The `amr_regions_mesh.png` proof (ADR 0148) drew an
ABSTRACTION (black L4 patch boundaries + light-grey L3 families); NATE wants the
raw grid instead: the actual cell edges, one colour, where refinement is
self-evident because the grid gets DENSER in refined areas.

## Decision

- **Emit the mesh as a per-run layer in the SHARED agent postprocess seam.** New
  functions in `postprocess_geoclaw.py`: `build_geoclaw_mesh_geojson` (patches ->
  grid-line `FeatureCollection`), `make_geoclaw_mesh_layer_uri` (upload +
  LayerURI), and `build_geoclaw_mesh_layer` (discover the peak-relevant frame,
  parse, build, upload). `model_geoclaw_inundation` - the ONE composer every
  inundation template rides (amr_regions / regional_manning / gauge_timeseries) -
  calls it once and surfaces the layer via the reusable `publish_input_layer`
  context seam. One seam, not per-template copies.

- **The mesh IS the raw grid.** Each AMR patch becomes one `MultiLineString`
  feature holding its actual cell-edge grid lines in EPSG:4326 (fort.q
  `xlow`/`ylow` are lon/lat under GeoClaw's spherical `coordinate_system=2`). All
  levels land in ONE FeatureCollection; a finer patch simply draws a denser grid.
  No per-level colour/weight coding - refinement is grid density, unabstracted.

- **Honest decimation, stated in a property.** A patch with <= 2500 cells emits
  EVERY cell edge (a faithful full grid). A larger/finer patch (where every edge
  would be megabytes) emits its boundary plus interior lines sampled to ~40 per
  side. Each feature carries `decimated` + `sample_stride_x/y`; the
  FeatureCollection `metadata` foreign member carries the policy + per-level
  histogram + counts. Payload is measured (`estimate`-style) and warned above a
  soft cap; per-patch decimation keeps it bounded.

- **Envelope = the hecras mesh row + crs_authid.** The row mirrors
  `make_hecras_mesh_layer_uri`: `layer_type="vector"`, `style_preset="mesh_grid"`,
  `role="context"`, `bbox=None` (the mesh must not fight the flood camera), plus
  `crs_authid="EPSG:4326"` (ADR 0118). Grid lines are a LineString collection, so
  the renderable QGIS type is a VECTOR (QgsVectorLayer draws the black grid) - a
  `layer_type="mesh"` row would route the plugin to MDAL, which cannot load
  LineStrings. The row still rides the mesh-preview protocol (mesh_grid preset +
  context role + crs_authid); "mesh" here is the semantic product, "vector" the
  faithful renderer.

- **No worker rebuild (WORKER-IMAGE LAW, ADR 0148).** The mesh is built AGENT-SIDE
  from the downloaded `fort.q` frames; no container code (`services/workers/*`)
  changed, so `trid3nt-local/geoclaw:latest` was NOT rebuilt. The live GeoClaw
  path is confirmed the agent-side fallback (completion.json carries no
  `publish_manifest_uri`), so the shared composer's mesh call executes on every
  run. Follow-up (not this ADR): the dormant worker-manifest fast path does not
  yet carry the mesh - if it is ever activated for GeoClaw, the worker postprocess
  must build the mesh too.

## Consequence

- **Live re-smoke** (Crescent City deck, ADR 0148 params: amr_levels=4, one L4
  window over the harbour, through the EXISTING image). Depth solve healthy:
  max_depth 0.997 m, flooded 0.075 km2. Emitted `mesh.geojson`: 75 AMR patches
  (per-level L1:1 L2:4 L3:64 L4:6, max level 4), 4786 grid lines, 9572 vertices,
  0.27 MB, CRS EPSG:4326, built from the final frame (frame 5). Refined-density
  check: ALL 1016 finest (L4) grid vertices fall over the user AMR window
  (frac_near_window = 1.0) - the dense grid sits exactly where the user asked.
  Zero patches needed decimation at this AOI/resolution.

- **Proofs** (`docs/proof/templates/`, regenerated from the SAME run):
  `amr_regions_mesh.png` = the RAW UNIFIED MESH (Clawpack-gallery style: actual
  black thin grid lines, ONE colour, visibly denser over the refined window; the
  ONLY overlay is the yellow dashed user window + white AOI box over Esri; caption
  strip states "yellow dashed = your AMR window; the mesh is the solver's response
  to it"). This SUPERSEDES the ADR 0148 abstract patch-family mesh proof.
  `amr_regions.png` = the sea-surface anomaly (eta) snapshot (the approved ADR 0148
  raster style: a full-AOI wave field on the diverging blue-white-red ramp;
  offshore drawdown -> shoreline run-up; white AOI box + yellow window + red gauge
  dot). `amr_regions_depth.png` = the peak-inundation depth product map. All from
  the same re-smoke run; no annotation boxes, no suptitle, captions in the strip.

- **Determinism rule for frame-snapshot proofs (standing norm).** A snapshot proof
  PINS its presentation so the same physics always renders the same way (a re-smoke
  reads as the same experiment, never a new one), never auto-selects. Two pins,
  BOTH stated in the caption strip: (1) a fixed frame criterion -- `amr_regions.png`
  pins the frame nearest `t=900 s`; (2) a fixed symmetric colour scale --
  `amr_regions.png` pins `+-0.5 m` (out-of-range values clip into the end colours,
  standard). `scripts/proof_geoclaw_amr_mesh.py` carries the pins as
  `ETA_SNAPSHOT_T_S` / `ETA_VLIM_M` constants. This applies to any future
  frame-snapshot proof.

- **Tests.** `test_geoclaw_postprocess_amr_flatten.py` gains a synthetic
  multi-patch structure test (per-level patches present, honest decimation flag +
  stride on a large patch, correct CRS, valid LineString FeatureCollection) and an
  end-to-end `build_geoclaw_mesh_layer` test off a synthetic fort.q (asserts the
  vector / mesh_grid / context / crs_authid / s3-key envelope). Offline geoclaw
  slice green (55 server + 31 contract cases).

- **Render script.** `scripts/proof_geoclaw_amr_mesh.py` renders both proofs from
  one run prefix (the SWAN proof harness pattern: Esri basemap, 3857 overlay,
  caption strip).
