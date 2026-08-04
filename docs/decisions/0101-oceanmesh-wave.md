# ADR 0101 -- Oceanmesh wave (leg 1 bank fallback, leg 2 coastal_tin, leg 3 RiverMapper)

Status: accepted (2026-08-04)
Spec: docs/specs/mesh-layer-extraction.md (SIGNED, NATE 2026-08-03) -- the
OCEANMESH WAVE section + the oceanmesh-wave leg-1 section at the bottom.
Follows: ADR 0098 (M1 EXTRACT), 0099 (M2 GENERALIZE), 0100 (M3 HECRAS). Runs
right after M3, before M4 (NATE pull-forward 2026-08-04). Three legs.

## Context

NATE doctrine (2026-08-04): the data-source-fallback norm extends to MESH-GEOMETRY
sources -- any SILENT substitution of banks / terrain / shoreline inputs is
outlawed. And the census's no-graded-sizing gap is closed by adopting the
OceanMesh2D tried-and-true sizing functions rather than hand-building grading.

## Leg 1 -- EXPLICIT TELEMAC bank fallback (BUILT + LIVE-PROVEN)

Before: `telemac_river_dye_build.py` silently fell back real-NHDArea-banks ->
constant-width ribbon whenever no NHDArea polygon covered the reach (empty fetch
/ too-little-sampled-water / fetch error). That is an inexplicit mesh-source
fallback -- outlawed.

Decision -- an explicit `bank_source` param + a typed gate (the DEM_FALLBACK_GATE
pattern, ADR 0091):

- **`bank_source`** threaded end-to-end: the `telemac_river_dye` template
  (`nhd_area` default | `constant_ribbon`) -> `model_river_dye_release_scenario`
  -> the worker manifest `reach.bank_source` -> `ReachConfig.bank_source`
  (default flipped `"auto"` -> `"nhd_area"`; legacy spellings `auto`/`constant`
  still map). `_normalize_bank_source` collapses synonyms to the closed set on
  the server; the worker holds the same legacy map (belt-and-suspenders).
- **The gate**: on the default `nhd_area` path, when the worker cannot produce
  real banks (no NHDArea polygons OR sampling saw too little water OR a fetch
  error), it raises `BanksUnavailableError` -> `main()` writes
  `telemac_metrics.json` with `error_code="TELEMAC_BANKS_UNAVAILABLE"` +
  `bank_source_retry="constant_ribbon"` + `assumed_channel_width_m` and exits 3.
  NO ribbon is built. The server (`preview_telemac_mesh` at the approve-mesh gate
  AND the full-solve dispatch) reads the worker metrics via `_read_run_metrics`
  and `_raise_if_banks_unavailable` -> the typed, RETRYABLE
  `TelemacBanksUnavailableError` (subclass of `TelemacDyeScenarioError`, carries
  `.suggestions` naming `bank_source="constant_ribbon"` + the width). The
  template RE-RAISES it (not swallowed to a dict) so `summarize_tool_result`
  surfaces `.suggestions` and it rides the tool-retry loop -- the user approves
  the ribbon substitution conversationally.
- **Provenance** (feeds structured provenance): the worker records the OUTPUT
  `bank_source` (`nhd_area` = real sampled banks | `constant_ribbon` = assumed
  width) in `telemac_metrics.json`; it rides the mesh-gate stats
  (`preview_telemac_mesh` return + the approve-mesh card reason), the tin
  preview-gate envelope tool_args, and the result envelope (the published
  layer's `fallback_note` states which banks were used + the assumed-width caveat
  for the ribbon).
- **`constant_ribbon`** works exactly as before, now LABELED as an assumption.

Live proof (mesh_only direct-worker drives, rebuilt telemac image):
| drive | bank_source | result |
| --- | --- | --- |
| Columbia R. (river_name reseed) | nhd_area | exit 0, `bank_source="nhd_area"`, real banks frac=1.00 width 488/687/910 m, 3981 nodes, domain_mode=water-polygon |
| Snake R. Twin Falls | constant_ribbon | exit 0, `bank_source="constant_ribbon"`, ribbon 825 nodes, domain_mode=ribbon |
| Snake R. + forced-empty NHDArea | nhd_area | exit 3, `TELEMAC_BANKS_UNAVAILABLE`, retry=constant_ribbon, assumed 60 m -- the gate text |

Forced-empty seam: `TRID3NT_TELEMAC_FORCE_BANKS_EMPTY` env in
`fetch_bank_polygons` (test-only, live path untouched when unset). Note: the
un-forced Snake/Columbia-without-river_name reaches ALSO gated ("too little
water" on a mis-snapped flowline) -- i.e. the pre-existing silent-ribbon masked a
sampling miss the gate now surfaces honestly. TELEMAC worker image REBUILT
(code-only change; size unchanged 3.55 GB, base layers cached). Retrieval
re-proof after the docstring change: 7/7 river_dye corpus phrasings keep
`telemac_river_dye` top-8 (model-free `retrieve_visible_tools(q, None, 8)`).

## Leg 2 -- coastal_tin via oceanmesh (BUILT: generator + validation; NO solver consumes it yet)

- **A new dedicated mesh worker `services/workers/mesh/`** (the honest home): the
  `oceanmesh` package is GPL-3 AND carries pybind11 C++ extensions
  (delaunay_class / HamiltonJacobi / fast_geometry) linking CGAL -> GMP/MPFR, so
  -- exactly like gmsh in the TELEMAC image -- it must never enter the server
  venv, and riding an existing worker would drag CGAL in or fail the source
  build. A slim, single-purpose multi-stage `python:3.11-slim` image is the
  honest home. oceanmesh is NOT on PyPI (verified: `No matching distribution`) --
  installed from the canonical `CHLNDDEV/oceanmesh` repo pinned `@v1.0.0`.

  **IMAGE SIZE (container-hygiene hard rule, `docker history`), amd64, ~1.33 GB
  uncompressed:**
  | layer | size |
  | --- | --- |
  | oceanmesh venv closure (numpy<2 / scipy / geopandas / rasterio / fiona / shapely / matplotlib / scikit-fmm / oceanmesh + its CGAL-linked pybind11 extensions) | 874 MB |
  | Debian trixie base + python | ~140 MB |
  | runtime libs (libgmp10 + libmpfr6 + libexpat1) | ~1.7 MB |
  | worker code | 33 kB |

  Hygiene: MULTI-STAGE -- the C++ toolchain (build-essential, cmake, git,
  libcgal-dev/libgmp-dev/libmpfr-dev/libeigen3-dev + libgdal/geos/proj -dev,
  several hundred MB) lives ONLY in the build stage and NEVER reaches runtime;
  `.dockerignore` (tests/pyc); slim runtime (only the 3 dlopen'd shared libs --
  fiona's wheel dlopen()s libexpat, the documented modflow/rasterio lesson). The
  venv closure is the dominant cost; a `--no-deps` minimal trim is a
  characterized follow-up once the consuming coastal-solver wave pins the exact
  oceanmesh call surface (the same posture as the HEC-RAS ras-commander closure).
- **The server component `agent/mesh/coastal_tin.py`** (thin, M1/M2 paradigm,
  offline-suite-safe -- no heavy import server-side): `CoastalTinSpec` composes
  the OceanMesh2D sizing-function SPEC (edge bounds + distance/feature-size/
  wavelength/slope + gradation) + inputs; `run_coastal_tin_worker` stages the
  manifest, dispatches the worker (local-docker volume-mount envelope), and reads
  the mesh GeoJSON + stats back -- the caller publishes the GeoJSON through the
  shared `mesh_preview` `style_preset="mesh_grid"` contract.
- **Shoreline source**: a vector polygon shapefile supplied per-run (GSHHG L1 is
  the keyless source oceanmesh's README + tests use). The validation case uses
  oceanmesh's OWN bundled fixtures (no network). NO new registered shoreline
  source.yaml this wave (registry unchanged); a NOAA/GSHHS source spec is a
  characterized follow-up when a fetch-time shoreline is needed.
- **Sizing functions**: distance / feature-size (medial axis) / wavelength (M2
  tide) / slope (bathymetric-gradient), composed with `compute_minimum` and
  graded with `enforce_mesh_gradation` (User Guide Eqs. 3-11); quality read back
  with `simp_qual` (equilateral quality q_E, Eq. 1) + the q_E - 3*sigma > 0.75
  control-limit floor (Eq. 2).
- **Validation (paper-first)**: replicated the OceanMesh2D workflow on a US
  domain -- **Galveston Bay, TX** -- using oceanmesh's OWN shipped test fixtures
  (the intermediate-resolution GSHHG shoreline `GSHHS_i_L1.shp` + the `galv_sub`
  DEM, downloaded from the pinned `CHLNDDEV/oceanmesh@v1.0.0` `tests/`), with
  feature-size + slope (bathymetric-gradient) sizing + gradation 0.15 + the
  standard cleanup pass. WHAT WAS COMPARED = the mesh QUALITY (the User Guide's
  ACTUAL acceptance gate), computed as the scale-invariant equilateral quality
  q_E = 4*sqrt(3)*A/sum(edge^2) (Guide Eq. 1) in LOCAL METRIC coordinates --
  NOTE oceanmesh's own `simp_qual` is a SCALE-DEPENDENT radius ratio (0.577 for a
  unit equilateral), NOT the paper's q_E, so q_E is computed directly:

  | metric | published (OceanMesh2D User Guide) | our Galveston TIN |
  | --- | --- | --- |
  | mean q_E | 0.956 (Jamaica Bay), 0.958-0.967 (other worked cases) | **0.9572** |
  | Eq. 2 termination q_bar - 3*sigma > 0.75 | the acceptance criterion | **0.807 -> PASS** |
  | % elements q_E > 0.5 | -- | 99.9% |
  | mesh | -- | 12,159 vertices / 21,667 elements |

  Vertex/element COUNTS are NOT directly comparable (different domain + the
  intermediate-res GSHHG vs the paper's high-res PostSandyNCEI), so the
  comparison is on QUALITY + the gradation bounds -- which MATCH the published
  0.956-0.967 range and pass the guide's Eq. 2 gate. `sum(simp_vol)` = 0.457
  deg^2 tracks the meshed water area (the package's own sanity assertion).
- **Spot-check deliverables** (honest renders, screenshot-proof norm): the
  validation-case TIN + a fresh US coastal AOI TIN (**Mexico Beach, FL** -- the
  SFINCS validation AOI; feature-size sizing on GSHHG_i; 858 verts / 1323 elems,
  mean q_E 0.9515, q_bar-3sigma 0.778 PASS, 0 non-finite, 11.8 s) rendered under
  `scratchpad/oceanmesh_proofs/galveston_tin.png` + `mexicobeach_tin.png` --
  both show the OceanMesh2D graded sizing (fine along the shoreline/estuary,
  coarsening offshore).

NO solver consumes coastal_tin yet -- future SCHISM / TELEMAC-coastal work. This
wave lands the generator + validation only (per the signed spec).

## Leg 3 -- RiverMapper characterization (report-only, no build)

schism-dev's **pyDEM** (github.com/schism-dev/RiverMeshTools; standalone mirror
wzhengui/pydem) extracts thalwegs (1D stream centerlines) from raw DEM tiles via
a Priority-Flood-fill -> D8 -> flow-accumulation -> threshold pipeline; output =
a thalweg-polyline shapefile. **RiverMapper** (same repo) converts an APPROXIMATE
centerline (pyDEM output, NHDFlowline, NWM streamlines, or hand-drawn) + DEM tiles
into 2D-resolvable **river arcs** (bank-line arcs + cross-channel subdivision
arcs) in SMS `.map` format (+ optional OCSMesh polygon export) by walking
perpendicular transects to locate the true banks within a DEM-informed width
window. Notably it can also consume **NHDArea polygons directly instead of a DEM**
for cleaner small-channel delineation (Pearl River example, tested for
STOFS-3D-Atlantic).

- License: repo LICENSE = Apache-2.0 (subpackage metadata declares MIT) -- both
  permissive, non-copyleft, no blocking constraint (carry attribution).
- Maturity: peer-reviewed (Ye et al. 2023, Env. Model. & Software 166:105731),
  production-used (STOFS-3D-Atlantic on AWS), last commit 2026-06-26, small but
  real (docs, tutorials, sample tarballs). Serial code path suits reach-scale
  AOIs; MPI is skippable.
- Dependencies: pure-Python pip installs (GDAL/rasterio/shapely/geopandas/rtree/
  scikit-learn), no SCHISM/Fortran/SMS runtime -- runnable in an isolated worker
  (the only friction = a GDAL native install, same class as our rasterio stack).

RECOMMENDATION: **Adopt as the third explicit `bank_source="dem_derived"` for
TELEMAC -- but as its OWN small wave (river-hydraulics track), NOT folded into
leg 1.** Fit is strong: RiverMapper is purpose-built for "approximate centerline +
DEM -> real bank arcs", a materially better fallback than an assumed-width ribbon
for exactly the NHDArea-empty gap leg 1 now gates on (NHDFlowline exists for
nearly every reach where NHDArea is empty). Effort class **M** (a new GDAL-native
worker + a bank-arc <-> TELEMAC contract translation + canonical-case V&V). It
also doubles as the river-arc feed for future compound-flood meshing. Simpler
alternative (pysheds / GRASS r.stream DEM-threshold) is S-effort but crude and
carries no compound-flood on-ramp -- prefer RiverMapper if that roadmap is real.
See the DELETION_LEDGER QUEUED row.

## Consequences

- Leg 1: NEW typed errors (`BanksUnavailableError` worker /
  `TelemacBanksUnavailableError` server, retryable, `.suggestions`); no registry
  / coded-tool / spec-served change (a behaviour change + a new param on an
  existing template). The silent NHDArea->ribbon fallback is DELETED as behaviour
  (DELETION_LEDGER). Offline baseline UNCHANGED (the documented 9 failures); the 5
  river_dye baseline members fail identically in KIND -- the 3 `_fake_publish`
  stale-mock TypeErrors moved 8->9 positional args (a legit new provenance arg on
  `_publish_peak_layer`, a pre-existing broken mock, not a behaviour regression);
  the 2 env/geocode failures unchanged.
- Leg 2: NEW worker `services/workers/mesh/` + NEW server component
  `agent/mesh/coastal_tin.py`; registry / coded-tool / spec-served UNCHANGED (no
  registered tool -- generator + validation only). No flood-seam touch -> flood
  canary NOT mandated.
- Leg 3: no code; a DELETION_LEDGER QUEUED row + this ADR section.
