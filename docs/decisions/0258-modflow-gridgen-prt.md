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
