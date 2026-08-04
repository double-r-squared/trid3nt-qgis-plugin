"""HEC-RAS 2D flow-area geometry: read a geometry HDF's mesh -> a preview layer.

Mesh-layer wave M3 (SIGNED spec: docs/specs/mesh-layer-extraction.md; the write
seam the spec named as ``hecras_geometry``). The spec's WRITE stage envisioned a
"thin per-solver terminal writer -- cells + breaklines to the geometry HDF". M3
proved, empirically and offline-first, that the WRITE direction is BLOCKED and
the READ direction is the tractable, reusable half. This module delivers the
read/preview half; the write blocker is characterized below (ADR 0100).

WHY THE WRITER IS STOPPED (empirical, host-proven 2026-08-03 on HEC's shipped
Muncie test project, White River, Muncie IN, from the official Linux_RAS_v66.zip):

  * HEC-RAS 2D hydraulics are SUBGRID: each cell carries a terrain-sampled
    volume<->elevation table ("Cells Volume Elevation Values") and each face an
    area/wetted-perimeter<->elevation table ("Faces Area Elevation Values").
    ``RasUnsteady`` READs these in ``Subroutine READBathymetry`` and CANNOT run
    without them.
  * ``RasGeomPreprocess`` does NOT build the 2D subgrid tables. Stripping them
    from the Muncie HDF and re-running the preprocessor left them ABSENT (the
    preprocessor only rebuilt the 1D cross-section conveyance tables); the
    subsequent ``RasUnsteady`` then failed with
    ``object 'Cells Volume Elevation Info' doesn't exist``. Those tables are
    authored by RASMapper (the Windows RASMapperLib DLLs), exactly the
    "headless 2D mesh authoring is the real frontier" caveat the ras-commander
    feasibility report named (ras-commander's own ``GeomMesh`` needs those DLLs).

  So a from-scratch 2D flow area carrying only perimeter + cell points + mesh
  topology is INSUFFICIENT: nothing on the Linux stack computes the subgrid
  property tables, so the deck never solves. Replicating RASMapper's terrain
  subgrid sampling into HEC's undocumented internal table format is the genuine
  blocker, and building a topology-only writer that no Linux engine can consume
  would be dead code.

TEMPLATE-FIRST FALLBACK (the HEC-RAS engine-landing wave's path, already proven
by the M3 Muncie gate): reuse a RASMapper-built 2D flow-area geometry HDF as a
template and reparameterize terrain association + Manning's n + forcing/BCs
(ras-commander ``RasGeo``/``RasUnsteady`` ASCII editors). The Muncie replication
gate demonstrates the reparameterize-and-solve spine end to end.

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

logger = logging.getLogger("trid3nt_server.agent.mesh.hecras_geometry")

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
