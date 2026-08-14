# ADR 0258 - MODFLOW gridgen in the worker image; DISV quad-refined backward PRT proven through-image

Date: 2026-08-14
Status: accepted

## Context

ADR 0246 sweep-2 adjudicated the MODFLOW PRT cluster and left two rows on an
image-variant STOP: `prt_backward_capture_zone_quadrefined` and
`prt_backward_lateral_boundary_injection_wells`. PRT itself was confirmed native
(mf6 6.7.0, `flopy.mf6.ModflowPrt` OK; backward capture-zone + transient PRT
already live on a STRUCTURED grid via the wellhead track, ADR 0215). The wall was
the DISV quad-refinement: `which gridgen` = none in the modflow worker image, so
`flopy.utils.gridgen.Gridgen` imported but could not build a refined DISV grid at
runtime. The forward row (`prt_forward_transient_flow_pathlines`) was SUBSUMED by
ADR 0215.

The STOP-recipe was: SHA-pin the USGS gridgen binary into the image, author a
refined DISV, run backward ModflowPrt on it, worker-image rebuild + through-image
smoke.

## Decision

Executed the image half of the recipe and proved the physics through the rebuilt
image (local-first; the product-side port is the scoped follow-on).

### Image (`trid3nt-local/modflow:adr0258`, ~1.16 GB)

- Converted `services/workers/modflow/Dockerfile` to a two-stage build. A
  `binaries` stage carries the curl/unzip download toolchain and fetches the
  SHA-pinned mf6 6.7.0 zip (existing pin) + the USGS gridgen 1.0.02 dedicated
  release `linux.zip` (SHA-256 `d45bc3378f6bc5767f5a5ab7f102c5cc4624d24bad772422762e33d9aca09302`);
  the runtime stage COPYs only the resolved binaries, so curl/unzip no longer
  ship in the runtime image (container hygiene).
- gridgen 1.0.02 is dynamically linked against libstdc++.so.6 + libgcc_s.so.1 --
  added `libstdc++6` + `libgcc-s1` to the runtime apt layer.
- flopy 3.10's `Gridgen.build()` requires the optional deps geopandas + pyshp
  (`shapefile`) -- it constructs the refined-grid intersection through geopandas
  geometry ops and reads gridgen's output quadtree shapefile via pyshp. Added
  `geopandas>=1,<2` + `shapely>=2,<3` + `pyshp>=2.3,<3` to the image venv (the
  server venvs/agent already ships geopandas 1.1.4 + shapely 2.1.2 + pyshp 3.x).
- triangle NOT installed: the quad-refined DISV path uses gridgen polygon
  refinement only, no Delaunay triangulation. Neither open row needs it.
- Provenance checked IN-IMAGE (build-time smoke + fresh `docker run`):
  `mf6: 6.7.0 02/05/2026`, `GRIDGEN Version 1.0.02`, gridgen libs resolve,
  `gwt_adapter.gridgen_available()` True.

### Through-image smoke (ex-prt-mp7-p02 backward pattern on gridgen DISV)

A self-contained flopy driver (persisted at
`docs/proof/templates/modflow_capture_zone_disv_quadrefined_prt_smoke.py`) run
inside the image: gridgen refines a 300 m polygon around a pumping well (3 levels)
on a 40x40 / 100 m base grid -> DISV; mf6 solves the GWF field; head+budget are
time-reversed (`HeadFile.reverse()` / `CellBudgetFile.reverse()`); a separate
PRT DISV sim (`ModflowPrtdisv` + mip + prp ring of 24 particles + oc + fmi
reversed + ems) forward-tracks through the reversed field -> backward capture
zone.

DISCRIMINATING result (coarse control vs gridgen quad-refined, same physics):

| metric              | coarse    | refined   |
|---------------------|-----------|-----------|
| ncpl                | 1600      | 4135      |
| min cell edge       | 100 m     | 12.5 m    |
| particles tracked   | 24        | 24        |
| GWF head_min        | 48.00 m   | 47.28 m   |

The refined grid RESOLVES the pumping cone of depression (head_min 47.28 m, below
the 48 m downgradient boundary) that the coarse 100 m grid SMEARS (head_min pinned
at the boundary minimum, cone invisible). Proof render (DISV mesh wireframe +
refinement ring + backward pathline polylines over the head field, refined vs
coarse): `docs/proof/templates/modflow_capture_zone_disv_quadrefined_prt.png`.

## Consequence

- Board: `prt_backward_capture_zone_quadrefined` STOP-RECIPE -> IMAGE-UNBLOCKED /
  PROTOTYPE-PROVEN; `prt_backward_lateral_boundary_injection_wells` STOP-RECIPE ->
  BUILD (image-unblocked). Two durable in-image facts for the orchestrator: the
  modflow image NOW carries gridgen 1.0.02 (any DISV/DISU quad-refined grid path
  is unblocked), and flopy 3.10 gridgen needs geopandas + pyshp (now baked).
- No production code changed (Dockerfile + docs/proofs only) -> the four-slice
  test law does not fire; no registry pin moves (no template registered this
  turn); the live modflow canary is untouched (local-exec still runs the host
  mf6 binary on the structured PRT path).
- REMAINING to land the template (scoped follow-on, no image gap): the product
  `_build_prt_capture_zone_deck` / `_run_prt_capture_zone` in
  `services/workers/modflow/gwt_adapter.py` are structured-DIS only -- port to
  DISV (`ModflowGwfdisv`/`ModflowPrtdisv` from `get_gridprops_disv`,
  `VertexGrid.intersect` for well + release cells, DISV-aware pathline georef +
  capture-zone polygon on unstructured cells + COG rasterization from DISV),
  surface a `grid_type='disv_quadrefined'` knob on capture_zone, then full 6-point
  registration + 4-slice law + live canary. Host-provision the gridgen binary on
  the box (`TRID3NT_GRIDGEN_BIN`, mirroring `TRID3NT_MF6_BIN`) for the live
  local-exec deck-build path (deck build runs agent-side in venvs/agent).

Image-only landing (binary + through-image physics proof); no production code
changed.

## Product port (2026-08-14 append) -- `grid_type='disv_quadrefined'` knob LIVE

The scoped follow-on named in the Consequence is DONE. The DISV capture zone is a
`grid_type` KNOB on the existing `modflow_capture_zone` / `modflow_wellhead_protection`
template -- registry UNCHANGED at 255, no new tool.

### Code

- `services/workers/modflow/gwt_adapter.py`
  - `_build_disv_capture_zone_deck` (new): gridgen refines a `DISV_REFINE_HALF_WIDTH_M`
    (300 m) square around the well through `DISV_REFINE_LEVEL` (3) quadtree levels on
    the 41x41 / 100 m base grid -> `ModflowGwfdisv` (via `_build_disv_gridprops` +
    `_build_gwf_disv`). CHD perimeter (planar georeferenced OR demo west->east) and the
    WEL are placed on cell2d indices (`gwf.modelgrid.intersect`), not (row, col). The
    `get_gridprops_disv()` dict + the well cell2d ride the in-memory `DeckManifest`
    (`disv_gridprops` / `disv_well_cell2d` / `disv_ncpl` / `disv_min_cell_edge_m`).
  - `_build_prt_capture_zone_deck` gained a `grid_type` param + an early DISV branch
    (single-well STEADY only; transient / multi-well / NHD-RIV on DISV raise a typed
    ValueError -- honest STOP, never a silently-degraded structured run). The old
    ARBITRARY drawn `refine_regions` path (ADR 0099 M2, whose CHD/WEL were never ported
    to DISV) now raises directing to the knob (the gridgen binary being present would
    otherwise build a DISV grid under structured boundary code = a WRONG deck).
  - `build_and_run_prt_from_gwf` gained a `ModflowPrtdisv` leg: when the deck carries
    DISV gridprops it rebuilds the identical VertexGrid and intersects each release-ring
    point to its own (possibly finer) cell2d. postprocess is grid-agnostic (it reads
    x/y from the trk.csv), so the hull + pathline georef are unchanged.
  - `GRIDGEN_EXE` is now `os.environ.get("TRID3NT_GRIDGEN_BIN", "gridgen")`, mirroring
    the `TRID3NT_MF6_BIN` pattern (agent-side deck build runs in venvs/agent where
    gridgen is not on PATH; the image ships it on PATH).
- `grid_type` threads: composer (`model_capture_zone_scenario` / `modflow_capture_zone`)
  -> `MODFLOWRunArgs.grid_type` (Literal['structured','disv_quadrefined']) -> the
  staged GWF build (`build_and_stage_modflow_deck`) AND the PRT-phase deck
  reconstruction (`run_modflow_archetype_tool`) -- both must build the SAME grid.

### Host gridgen provisioning (`TRID3NT_GRIDGEN_BIN`)

The SHA-pinned USGS gridgen 1.0.02 linux binary
(`MODFLOW-ORG/gridgen` v1.0.02 `linux.zip`, SHA-256
`d45bc3378f6bc5767f5a5ab7f102c5cc4624d24bad772422762e33d9aca09302` -- the same pin
the image uses) is installed to `bin/gridgen` (mode 755) and `TRID3NT_GRIDGEN_BIN=
.../bin/gridgen` is added to `.env.local`, mirroring `TRID3NT_MF6_BIN`. In-place
probe: `GRIDGEN Version 1.0.02`, libstdc++/libgcc resolve, `gridgen_available()` True.

### Product-path E2E (both grid_types, same well/AOI)

Direct-call through the PRODUCT code (`build_modflow_deck` -> mf6 ->
`build_and_run_prt_from_gwf` -> `postprocess_capture_zone`) at a real High Plains /
Ogallala setting (near Garden City, KS), 24 particles, tiers [1, 5, 10] yr:

| metric               | structured | disv_quadrefined |
|----------------------|-----------|------------------|
| ncpl                 | 1681      | 4216             |
| min cell edge        | 100 m     | 12.5 m           |
| well-cell head       | 48.99 m   | 48.04 m          |
| pathlines            | 24        | 24               |
| capture hull         | 1.572 km2 | 1.666 km2        |

DISCRIMINANT (ADR 0258): the refined 12.5 m cell at the pumping node RESOLVES a
~0.95 m deeper cone-of-depression drawdown (well-cell head 48.04 m) that the 100 m
cell SMEARS by averaging over its footprint (48.99 m). (With a strong regional
gradient the GLOBAL head_min is pinned at the downgradient boundary corner for both
grids, so the cone discriminant is read AT the well cell -- the direct, honest
metric.) Both grid_types publish the hull polygon + the pathline fan as its own
context layer. Proof (filled head cells + mesh wireframe + pathline polylines +
hull polygon, structured vs DISV):
`docs/proof/templates/modflow_capture_zone_grid_type_knob_prt.png`.

### Bundled: pathline layer surfacing (NATE catch)

Independent of DISV, the backtracked PRT pathlines now surface as their OWN vector
layer (role='context', "Pathlines: backward PRT (N particles)") on EVERY capture_zone
run -- `CaptureZoneLayerURI` gained a `pathlines_layer` field, `postprocess_capture_zone`
writes a separate `capture_zone_pathlines_4326.fgb` (the LineStrings moved OUT of the
hull FGB), and the composer publishes it via `publish_input_layer` beside the polygon
(input-parity doctrine: the hull alone hid which trajectories delineated it). Offline
pins guard both-published + emitter-drop-not-silent.

### `prt_backward_lateral_boundary_injection_wells`

Still BUILD (image-unblocked): now that the DISV+PRT product surface exists, it is an
additional archetype/knob (irregular active-domain + lateral IWEL boundary) on this
surface, sequenced after this knob.
