# ADR 0108 -- pysheds terrain-hydrology primitives + reach-selection fix

Status: accepted (2026-08-04, NATE hydrology pick, slotted before the HEC-RAS landing)

## Context

pysheds (MIT) is the terrain-hydrology primitive layer NATE picked from the
hydrology-integration set. Two registered tools already deliver the D8 core --
`delineate_watershed` (catchment upstream of a snapped pour point -> watershed
polygon) and `extract_stream_network` (accumulation-thresholded channel lines)
-- sharing `processing/_hydrology_common.py` (the typed error hierarchy, the
pysheds import seam, DEM staging/conditioning, bbox clamp, GeoJSON writer). This
ADR formalizes that landing and lands the one targeted consumer fix it enables:
the ADR 0104 Bug-1 reach-selection residual.

## Decision

### 1. pysheds dependency + numba latency (playground)

pysheds is NOT a new top-level dependency: `pfdf>=3.0.4` (already vendored in
`server/wheels/`) HARD-pins `pysheds==0.4` as its own requirement, so pysheds is
importable in the agent venv transitively. A second explicit pin would be
redundant and could only drift; the pyproject carries a comment documenting the
direct import + transitive pin instead.

pysheds is numba-based. Measured in the agent venv (fresh process, real DEM):

- `import pysheds.grid` -- ~2.2 s, paid LAZILY on the FIRST tool call (the tool
  modules import only numpy/contracts at module scope; `_import_pysheds` is
  called inside `_condition_dem`), NOT at daemon boot.
- numba JIT first-call penalty on the D8 chain -- negligible (<0.1 s; the
  fill/flowdir/accumulation/catchment routines are effectively not JIT-bound at
  our AOI sizes). No warm-up step is warranted; the honest first-call latency is
  the one-time ~2.2 s import, documented rather than hidden.

pysheds 0.4's `snap_to_mask` / `extract_river_network` are NEP-50-incompatible
with our numpy, so pour-point snapping and channel vectorization are pure-numpy
in-module; `nodata_out` is passed as numpy-typed scalars for the same reason.

### 2. The primitive contract (already registered)

`delineate_watershed(pour_point, bbox?, dem_uri?, snap_threshold=100)`:
`fetch_copernicus_dem` (or a `dem_uri` override) -> pysheds condition (fill_pits
-> fill_depressions -> resolve_flats) -> flowdir -> accumulation -> snap the pour
point to the nearest cell with `>= snap_threshold` upslope cells -> catchment ->
polygonize. Returns a `WatershedLayerURI` (polygon GeoJSON + `area_km2`,
`cell_count`, requested/snapped pour points, honest `notes`). Typed gates:
`HYDROLOGY_AOI_TOO_LARGE` (bbox over the 0.3-deg CPU clamp),
`HYDROLOGY_INPUT_INVALID` (bad pour point/bbox/URI, or pour point outside the
bbox), `HYDROLOGY_EMPTY_WATERSHED` (pour point off the flow grid),
`HYDROLOGY_DEPENDENCY_MISSING`, `HYDROLOGY_UPSTREAM_ERROR`. Honest truncation
note when the catchment touches the AOI edge (area is a lower bound -- enlarge
the bbox). `supports_global_query=False`. Offline-tested against a synthetic
valley DEM via the `dem_uri` override (8 tests, `test_hydrology_primitives.py`).

`extract_stream_network` is the sibling channel-line tool (same plumbing,
`accumulation_threshold`).

Fidelity line (ladder doctrine): pysheds outputs are SCREENING-grade terrain
hydrology; real flood depths route to the flood templates (SFINCS / HEC-RAS /
TELEMAC), never a HAND raster.

### 3. Reach-selection consumer fix (resolves the ADR 0104 Bug-1 residual)

ADR 0104 characterized but did NOT fix: the TELEMAC release-seeded reach search
(`services/workers/telemac/telemac_river_dye_build.py`) snapped to the NHDFlowline
geometrically NEAREST the seed, so a seed near a confluence
(Longview = Columbia x Cowlitz) landed on a 292 m order-3 tributary stub
(COMID 24521434). `_named_flowline_seed` only disambiguated when the LLM supplied
a `river_name`.

Fix -- the honest minimal ranking (NOT a routing engine): a NAME-FREE mainstem
re-seed. When `cfg.river_name` is absent, `_mainstem_flowline_seed` queries
NHDPlus_HR layer 3 within a ~0.05-deg envelope of the seed and prefers the
highest-`streamorde` channel (tie-broken by `totdasqkm` upstream drainage, then
proximity), re-seeding onto it ONLY when it STRICTLY outranks the nearest
flowline AND its nearest vertex is within 6 km (bounded so a genuine small-creek
study is never yanked onto a distant river). Fail-OPEN to the raw
position-snap on any error, mirroring `_named_flowline_seed`. The pure decision
seam `resolve_centerline_seed` is untouched (its 17 offline tests still pass).

Live proof: from BOTH the Longview city center (raw snap = COMID 24521434, the
exact 292 m stub ADR 0104 named) and the Columbia release point, the re-seed now
resolves COMID 24520446 -- the Columbia River mainstem (order 11, ~589,834 km2
drainage) vs the Cowlitz stub.

### 4. Watershed-as-AOI composition

The delineated watershed polygon IS an AOI source today: the LLM composes
`delineate_watershed` -> `clip_raster_to_polygon(polygon_uri=<watershed.uri>)`
(or drives a subsequent fetch off `WatershedLayerURI.bbox`). Proven live
(DEM fetched for the basin bbox, then clipped to the watershed polygon). A TRUE
domain-clip that masks a SFINCS solve to the watershed needs the worker
`include_mask` seam (ADR 0099 QUEUED row) -- NOT built here; the polygon->clip
and polygon->bbox composition paths cover the flexible playground case today.

## Consequences

- TELEMAC worker code changed (`telemac_river_dye_build.py`) -> worker image
  rebuild on the next deploy. Each name-free river_dye run now pays one extra
  fail-open NHDPlus_HR query (~1-2 s) to disambiguate the mainstem.
- Registry UNCHANGED (the pysheds tools were already registered; the reach fix
  adds/removes no tool). No flood seam re-point -> no flood canary mandated.
- HAND (Height Above Nearest Drainage) screening raster: DEFERRED, not built
  (see DELETION_LEDGER / open issues). pysheds 0.4 exposes `compute_hand`; a
  future cheap add would need the raster-COG write/publish/style path + the
  screening-grade fidelity label. The HEC-RAS landing is the natural consumer.
