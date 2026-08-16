"""HEC-RAS 2D flow-area geometry: read a geometry HDF's mesh -> a preview layer.

Mesh-layer wave M3 (SIGNED spec: docs/specs/mesh-layer-extraction.md; the write
seam the spec named as ``hecras_geometry``). This module delivers the READ/preview
half. The WRITE half lives in
``workers/hecras2025/hecras_geometry_writer.py``. See below for why
that write path is unblocked.

WHY A FROM-SCRATCH 2D FLOW AREA IS VIABLE. The original constraint was that a
from-scratch 2D flow area is INSUFFICIENT because "nothing on the Linux stack
computes the subgrid property tables" -- each cell carries a terrain-sampled
volume<->elevation table and each face an area/WP<->elevation table, ``RasUnsteady``
reads them in ``Subroutine READBathymetry`` and cannot run without them, and
``RasGeomPreprocess`` does NOT rebuild the 2D subgrid tables (it rebuilds only the
1D cross-section conveyance). At the time those tables were only authorable by the
Windows RASMapperLib DLLs. Two results removed exactly that premise:

  * ``MeshPropertyTables.ComputeFrom`` (the HEC-RAS 2025 beta compute
    path) computes the cell/face subgrid tables HEADLESS ON LINUX under substituted
    open-source GDAL/HDF5. So the tables ARE now Linux-computable.
  * The PRODUCTION 6.x solver CONSUMES 2D
    subgrid tables written by an EXTERNAL (h5py) writer and reproduces the Muncie
    baseline (dWSE 0.008 ft); the 2025-computed VALUES match the 6.x GUI ground
    truth (cell-volume corr 0.99988). A fresh-group geometry
    writer solved a WRITER-AUTHORED /Geometry/2D Flow Areas group to the
    baseline bit-identically (dWSE 0.00000 ft, product path).

  So a writer-authored 2D flow area (topology from the 2025 ``Mesh``, subgrid
  tables from ``ComputeFrom``) is NO LONGER dead code -- the Linux stack computes
  the tables and the 6.x solver consumes them.

WHAT REMAINS GATED: a genuinely-NEW-AOI PURE-2D deck also needs
the plan-skeleton / boundary forcing stanzas (``.pNN``/``.bNN``/``.xNN`` 2D-BC-line
or precipitation blocks), which the in-repo combined-1D/2D Muncie reference does not
carry; those await a pure-2D reference deck (ledgered). The reader/preview half
(this module) + the writer discharge the geometry link; the deck skeleton is the
next link.

WHAT THIS MODULE DOES (the tractable half, reusable by the engine wave + proofs):
read a HEC-RAS geometry/plan HDF's 2D flow-area cell mesh and emit it as a
mesh-preview ``FeatureCollection`` (cell polygons + the domain perimeter),
reprojected from the model projection to EPSG:4326 -- the same publishable
``style_preset="mesh_grid"`` / ``role="context"`` preview the M1/M2 mesh_preview
component gives every other paradigm. This is what renders the "modeled domain"
spot-check layer and, later, what the engine-landing wave's postprocess reuses.

Dependency-light + lazy: ``h5py`` and ``pyproj`` are imported INSIDE the reader
(not at module import) so the mesh package stays importable in the offline suite
with no new hard dependency; the reader is only invoked against a real HEC-RAS
HDF (engine wave / proof render), where those libs are present.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("trid3nt_server.mesh.hecras_geometry")

__all__ = [
    "read_2d_flow_area_cells",
    "HECRAS_2D_AREAS_GROUP",
]

#: HDF group holding the 2D flow areas in a HEC-RAS 6.x geometry/plan HDF.
HECRAS_2D_AREAS_GROUP = "Geometry/2D Flow Areas"

#: Per-cell facepoint index padding value in "Cells FacePoint Indexes".
_FACEPOINT_PAD = -1


def _model_crs_wkt(hdf) -> str | None:
    """The model projection WKT from the HDF root ``Projection`` attr (or None)."""
    proj = hdf.attrs.get("Projection")
    if proj is None:
        return None
    return proj.decode() if isinstance(proj, bytes) else str(proj)


def read_2d_flow_area_cells(
    hdf_path: str,
    *,
    area_name: str | None = None,
    max_cells: int = 20000,
) -> tuple[dict, dict]:
    """Read a HEC-RAS 2D flow area's cell mesh into an EPSG:4326 FeatureCollection.

    Reads the RASMapper-built mesh topology directly from the geometry/plan HDF
    (offline-first: the layout was mapped against HEC's shipped Muncie fixture,
    never guessed). Each 2D cell is a polygon whose corners are the cell's
    facepoints (``Cells FacePoint Indexes`` -> ``FacePoints Coordinate``); the
    domain boundary is the ``Perimeter`` ring. Coordinates are reprojected from
    the model projection (root ``Projection`` WKT) to EPSG:4326.

    Args:
        hdf_path: path to a HEC-RAS 6.x geometry or plan HDF.
        area_name: which 2D flow area to read; defaults to the first (and, for
            Muncie, only) area under ``Geometry/2D Flow Areas``.
        max_cells: honest cap. If the area has more cells, a WARNING is logged
            and the polygon features are truncated to the first ``max_cells``
            (the perimeter + the true ``n_cells`` stat stay exact -- nothing is
            silently misreported).

    Returns:
        ``(feature_collection, stats)``. The FeatureCollection carries one
        ``Polygon`` per cell (``role="cell"``) plus the domain ``Polygon``
        (``role="perimeter"``), all EPSG:4326. ``stats`` carries ``area_name``,
        ``n_cells``, ``n_facepoints``, ``model_crs``, ``rendered_cells``,
        ``truncated``, and the 4326 ``bbox``.

    Raises:
        ValueError: no 2D flow area present, or the model has no projection to
            reproject from (a HEC-RAS 2D model is always projected).
    """
    import h5py  # lazy: keeps the mesh package offline-suite-safe
    import numpy as np
    from pyproj import CRS, Transformer

    with h5py.File(hdf_path, "r") as f:
        if HECRAS_2D_AREAS_GROUP not in f:
            raise ValueError(
                f"{hdf_path}: no '{HECRAS_2D_AREAS_GROUP}' group -- not a 2D HEC-RAS model"
            )
        areas = f[HECRAS_2D_AREAS_GROUP]
        area_keys = [k for k in areas if isinstance(areas[k], h5py.Group)]
        if not area_keys:
            raise ValueError(f"{hdf_path}: no 2D flow area sub-group present")
        name = area_name or area_keys[0]
        if name not in area_keys:
            raise ValueError(
                f"{hdf_path}: 2D flow area {name!r} not found (have {area_keys})"
            )
        area = areas[name]

        fp_idx = area["Cells FacePoint Indexes"][()]  # (n_cells, max_sides), -1 pad
        fp_xy = area["FacePoints Coordinate"][()]  # (n_facepoints, 2) model CRS
        perimeter = area["Perimeter"][()] if "Perimeter" in area else None

        wkt = _model_crs_wkt(f)
        if not wkt:
            raise ValueError(
                f"{hdf_path}: no model 'Projection' attr -- cannot reproject the 2D mesh"
            )

    n_cells = int(fp_idx.shape[0])
    transformer = Transformer.from_crs(CRS.from_wkt(wkt), CRS.from_epsg(4326), always_xy=True)

    # Reproject every facepoint once, then index per cell (fast + exact).
    lon, lat = transformer.transform(fp_xy[:, 0], fp_xy[:, 1])
    fp_ll = np.column_stack([lon, lat])

    rendered = min(n_cells, max_cells)
    truncated = rendered < n_cells
    if truncated:
        logger.warning(
            "read_2d_flow_area_cells: area %r has %d cells > max_cells=%d; "
            "rendering the first %d (perimeter + n_cells stat stay exact)",
            name, n_cells, max_cells, rendered,
        )

    features: list[dict] = []
    for c in range(rendered):
        idxs = [int(i) for i in fp_idx[c] if int(i) != _FACEPOINT_PAD]
        if len(idxs) < 3:
            continue
        ring = [[float(fp_ll[i, 0]), float(fp_ll[i, 1])] for i in idxs]
        ring.append(ring[0])  # close
        features.append(
            {
                "type": "Feature",
                "properties": {"role": "cell", "cell_id": c},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )

    if perimeter is not None and len(perimeter) >= 3:
        plon, plat = transformer.transform(perimeter[:, 0], perimeter[:, 1])
        pring = [[float(x), float(y)] for x, y in zip(plon, plat)]
        if pring[0] != pring[-1]:
            pring.append(pring[0])
        features.append(
            {
                "type": "Feature",
                "properties": {"role": "perimeter", "area": name},
                "geometry": {"type": "Polygon", "coordinates": [pring]},
            }
        )

    bbox = [
        float(fp_ll[:, 0].min()),
        float(fp_ll[:, 1].min()),
        float(fp_ll[:, 0].max()),
        float(fp_ll[:, 1].max()),
    ]
    stats = {
        "kind": "hecras-2d-flow-area",
        "area_name": name,
        "n_cells": n_cells,
        "n_facepoints": int(fp_xy.shape[0]),
        "model_crs": wkt.split('"')[1] if '"' in wkt else "(wkt)",
        "rendered_cells": rendered,
        "truncated": truncated,
        "bbox": bbox,
    }
    fc = {"type": "FeatureCollection", "features": features}
    return fc, stats
