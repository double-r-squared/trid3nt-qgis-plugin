# ADR 0226 - GeoClaw Okada seafloor-deformation product + real-event tsunami source

Status: Accepted
Date: 2026-08-11

## Context

ADR 0185 STOPPED the Okada-dtopo front (module-coverage-board rows #18/#19,
"dtopo sources") behind a recipe: the tsunami CONSUMPTION path already existed
(`geoclaw_inundation(tsunami_dtopo_uri=)` threads a dtopo; the worker's
`render_maketopo_dtopo` synthesizes a single-subfault Okada dtopo from
`source_magnitude` + the user-gated fault geometry), but two things were missing to
make "given an earthquake, what seafloor deformation does Okada predict and what
tsunami does it drive" a first-class QUESTION CLASS:

1. the seafloor-DEFORMATION field itself was never surfaced as a product -- the
   worker wrote `dtopo.tt3` purely as solver input and discarded it, so the direct
   answer to "what deformation" was invisible; and
2. the Okada source could only be driven by hand-typed `source_lonlat` +
   `source_magnitude`, never pinned to a NAMED real earthquake.

## Decision

Land the Okada front as COMPOSITION + a run PRODUCT on the existing GeoClaw
inundation surface (NOT a new template, NOT a new standalone primitive):

### 1. Seafloor-deformation PRODUCT (the "what deformation" answer)

- Worker (`services/workers/geoclaw/setrun_builder.render_maketopo_dtopo`): after
  writing `dtopo.tt3`, the generated `maketopo.py` ALSO writes the final-time
  vertical deformation dZ as an ESRI-ASCII grid `deformation_dz.asc` (EPSG:4326,
  north-first, over the Okada source box) and prints its dZ min/max. Pure text write
  (no gdal) from `fault.dtopo.dZ[-1]` -- verified shape `(ntime, ny, nx)` indexed
  `[lat, lon]` against clawpack `dtopotools`.
- The deformation artifacts (`deformation_dz.asc` + `dtopo.tt3` + `maketopo.py`)
  are added to BOTH the worker `DEFAULT_OUTPUT_GLOBS` AND -- load-bearing -- the
  composer's `GEOCLAW_OUTPUT_GLOBS` (`run_geoclaw.py`), which is the AUTHORITATIVE
  manifest `outputs` list the worker honors over its own default. (The first live
  smoke uploaded no deformation because only the worker default carried it; the
  manifest list overrode it.)
- Agent-side postprocess (`postprocess_geoclaw.build_geoclaw_deformation_layer`):
  reads `deformation_dz.asc`, writes a SIGNED EPSG:4326 COG, uploads it, and returns
  a `LayerURI` (name "Seafloor deformation (Okada)", role context, units meters,
  bbox = the offshore Okada source box) + `{max_uplift_m, max_subsidence_m}`. The
  composer emits it through the render chokepoint (`publish_layer`) and stamps the
  modeled coseismic extremes onto the peak layer's `source_note` (a determinism-
  boundary provenance string). Best-effort: absent on dam_break / surge / a staged
  dtopo -> the depth answer stands unchanged.
- Style: a new `diverging_seafloor_deformation` preset (`-5,5`, `rdbu`) in
  `publish_layer._TITILER_STYLE_REGISTRY` -- a diverging ramp centered on 0 so the
  dipole reads blue=subsidence / white=0 / red=uplift, matching the signed
  river-seepage / bed-evolution precedent.

  The live path is AGENT-SIDE `postprocess_geoclaw` (the worker fast-path module
  `_geoclaw_postprocess` is NOT in the geoclaw image -- the Dockerfile copies only
  `services/workers/geoclaw/` -- so `read_publish_manifest` returns None and the
  agent postprocess runs). The deformation product therefore lives agent-side; the
  worker fast-path was deliberately left untouched (enabling it for geoclaw is an
  unrelated architecture change with regression risk).

### 2. Real-event tsunami source (`earthquake_source`)

- `server/.../geoclaw/earthquake_source.py`: `resolve_earthquake_source(region,
  min_magnitude, start/end)` reuses `fetch_usgs_earthquakes` (offline-first repo
  driver -- never a bespoke FDSN call): geocode the seismic region -> a search bbox,
  query FDSN, read the produced FGB, and pick the LARGEST-Mw event (`select_largest_event`,
  a pure selector; a magnitude tie breaks toward the shallower = more tsunamigenic).
- `geoclaw_inundation` gains `earthquake_source` (+ `earthquake_min_magnitude`,
  `earthquake_start_date/end_date`). When set it forces scenario "tsunami" and drives
  the Okada source from the REAL catalog epicenter -> `source_lonlat`, focal depth ->
  `fault_depth_km`, Mw -> `source_magnitude`. Threaded into
  `geoclaw_tsunami_gauge_timeseries` via the shared `model_geoclaw_inundation`.

### Provenance / honesty (the source-parameter story)

Epicenter, focal DEPTH, and Mw are REAL USGS ComCat values (`basis="fetched"`). The
fault MECHANISM (strike/dip/rake) is NOT in the FDSN summary feed, so it is DERIVED
-- a shallow subduction-interface THRUST assumption (dip 15 deg, rake 90 deg), strike
left to the worker's synthetic default -- surfaced as a LOUD `basis="derived"`
provenance entry ("the Okada seafloor deformation is MODELED, not an observed field")
unless the user supplies `fault_*`. The deformation raster's `fallback_note` and the
peak `source_note` both label it modeled.

## Consequence

- +0 registered tools / templates: a knob (`earthquake_source`) + a run PRODUCT
  (deformation raster) on the existing `geoclaw_inundation`, not a new door. Registry
  pin + EXPECTED_TEMPLATES UNCHANGED. The four offline slices reproduce the exact-SIX
  baseline from repo root.
- No strict-parser bump: the deformation emission is scenario-gated (tsunami
  synthetic Okada), not a new build_spec field -- `geoclaw-spec-5` unchanged. The
  worker image was rebuilt for the `maketopo.py` deformation-write + glob change,
  provenance-checked (deformation writer + numpy import baked in), and live-smoked.
- `earthquake_min_magnitude` is a magnitude floor, NOT a resolution knob -- the 0225
  declared-resolution sweep passes without a ResolutionSpec.

## Not done / future folds

- #18 `multi_subfault_dtopo_from_finite_fault_model` -- a paradigm-B
  `fetch_finite_fault_model` (USGS ComCat `.fsp` / SRCMOD / NOAA SIFT unit sources ->
  a normalized N-subfault table) + a `dtopotools.Fault` of N subfaults. DEFERRED
  behind the US-cases PAPER-FIRST gate: it replicates published finite-fault slip
  models, so it needs NATE-verified citations BEFORE build (the 1964 Alaska / Prince
  William Sound anchor). The single-subfault Okada + real-event source landed here is
  its prerequisite front.
- V&V against the NGDC/NCEI runup catalog (a calibrated hindcast) -- this landing is
  planning-grade (MODELED Okada from catalog Mw + an assumed interface mechanism),
  never a validated tide-gauge hindcast.
- A dedicated `deformation_only` cheap mode (author the dtopo + emit the deformation
  raster WITHOUT the run-up solve) if an independent "just show me the Okada field"
  question class appears.
