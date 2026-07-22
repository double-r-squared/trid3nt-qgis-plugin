"""SWMM quasi-2D computational-mesh -> clickable ``mesh_grid`` vector layer.

NATE task #156: when an urban-flood deck is built we auto-emit the SWMM
quasi-2D uniform quad-cell mesh as a context vector layer so the user can SEE
the true mesh structure (where the cells actually are) over the AOI - the same
grid the solver runs on, not a smoothed raster.

The mesh is recovered from the AUTHORITATIVE source: the staged ``.inp`` deck's
STORAGE node names (``S_<row>_<col>``) via
:func:`swmm_mesh_builder._active_cells_from_deck`. Each active cell becomes a
``Polygon`` feature whose corners are reprojected from the build's source CRS to
EPSG:4326 (lon/lat) with the build's rasterio affine.

Layering:
  * ``style_preset="mesh_grid"`` - the web render is already deployed.
  * ``role="context"`` - the mesh is a default-visible backdrop, not the primary
    result (that is the flood-depth raster).
  * ``bbox=None`` - the mesh must NOT emit a competing ``zoom-to`` (the AOI
    camera owns the view; see ``model_urban_flood_swmm`` zoom-on-area-first).

Decimation (NO silent caps): a fine large-AOI mesh can exceed the inline-GeoJSON
ceiling. When the active-cell count exceeds ``cap`` we aggregate into uniform
super-cells (block size ``b``) so the output stays <= ``cap`` features. We log a
WARNING with the decimation parameters - the structure is preserved (the cells
just get bigger), nothing is silently dropped.

This module is dependency-light at the geometry layer:
:func:`mesh_cells_to_feature_collection` needs only ``rasterio.Affine`` +
``pyproj`` (no swmm-api, no DEM), so it is unit-testable in isolation. The
deck-reading wrappers (:func:`swmm_mesh_to_geojson`,
:func:`make_swmm_mesh_layer_uri`) are SYNC compute - the caller wraps them in
``asyncio.to_thread`` (never run sync compute on the event loop).
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any, Iterable

from trid3nt_contracts.execution import LayerURI

logger = logging.getLogger("trid3nt_server.workflows.mesh_layer")

__all__ = [
    "mesh_cells_to_feature_collection",
    "swmm_mesh_to_geojson",
    "make_swmm_mesh_layer_uri",
    "make_sfincs_mesh_layer_uri",
]


def mesh_cells_to_feature_collection(
    active_cells: Iterable[tuple[int, int]],
    transform: list[float],
    crs: str,
    resolution_m: float,
    grid_shape: tuple[int, int],
    *,
    cap: int = 6000,
) -> tuple[dict, dict]:
    """Turn active mesh cells into a EPSG:4326 ``FeatureCollection`` of quad cells.

    Pure geometry: no swmm-api, no DEM, no I/O. Needs only ``rasterio.Affine``
    (to map pixel (col,row) -> source-CRS (x,y)) and ``pyproj`` (to reproject
    each corner to lon/lat). Unit-testable in isolation.

    Args:
        active_cells: iterable of ``(row, col)`` int pairs - the active mesh
            cells (e.g. recovered from STORAGE node names).
        transform: the build's rasterio affine as a 6-tuple ``(a, b, c, d, e,
            f)`` mapping pixel ``(col, row) -> (x, y)`` in the source CRS. Row 0
            is north so ``e`` (``transform[4]``) is negative.
        crs: source CRS string (e.g. ``"EPSG:32617"``).
        resolution_m: the native cell size in meters (one pixel side).
        grid_shape: ``(nrows, ncols)`` - the full grid extent (provenance/clamp).
        cap: max feature count. When the active-cell count exceeds this we
            aggregate into super-cells (see below) instead of dropping cells.

    Returns:
        ``(feature_collection_dict, meta)`` where ``meta`` is
        ``{n_cells, n_active, decimated, block, effective_resolution_m, cap}``.
        Empty ``active_cells`` -> empty FC + ``meta["n_cells"] == 0``.

    Decimation (no silent caps): if ``n_active > cap`` we aggregate cells into
    super-cells of block size ``b = ceil(sqrt(n_active / cap))`` (``b >= 2``).
    A super-cell keyed ``(row // b, col // b)`` is present iff ANY underlying
    active cell is present, so the resulting count is ``<= cap`` by construction.
    Each decimated feature also carries ``block`` and ``decimated`` properties.
    """
    from rasterio import Affine
    from pyproj import Transformer

    cells = list(active_cells)
    n_active = len(cells)

    # grid_shape is provenance here (the full extent); clamp/validate is left to
    # the build. Reference it so a malformed shape is not silently ignored.
    nrows, ncols = int(grid_shape[0]), int(grid_shape[1])

    if n_active == 0:
        return (
            {"type": "FeatureCollection", "features": []},
            {
                "n_cells": 0,
                "n_active": 0,
                "decimated": False,
                "block": 1,
                "effective_resolution_m": float(resolution_m),
                "cap": cap,
            },
        )

    # --- Decimation: choose the block size so output stays <= cap. -----------
    if n_active > cap:
        block = max(2, math.ceil(math.sqrt(n_active / cap)))
        decimated = True
    else:
        block = 1
        decimated = False

    # Super-cell keys: a super-cell is present iff ANY underlying cell is active.
    # The key (r0, c0) is the block ORIGIN in pixel space (top-left corner).
    block_keys: dict[tuple[int, int], None] = {}
    for (r, c) in cells:
        r0 = (int(r) // block) * block
        c0 = (int(c) // block) * block
        block_keys[(r0, c0)] = None

    if decimated:
        logger.warning(
            "mesh_layer: DECIMATING mesh - n_active=%d > cap=%d; aggregating "
            "into %dx%d super-cells -> output_cells=%d (structure preserved, no "
            "cells dropped; effective cell size %.0f m). grid_shape=(%d,%d)",
            n_active,
            cap,
            block,
            block,
            len(block_keys),
            resolution_m * block,
            nrows,
            ncols,
        )

    affine = Affine(*transform[:6])
    transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)

    cell_size_m = round(resolution_m * block, 2)
    resolution_label = f"{resolution_m * block:.0f} m"

    features: list[dict] = []
    for (r0, c0) in block_keys:
        # Pixel corners: top-left (c0, r0) .. bottom-right (c0+block, r0+block).
        # affine maps (col, row) -> (x, y) in the source CRS.
        x_left, y_top = affine * (c0, r0)
        x_right, y_bot = affine * (c0 + block, r0 + block)

        # Closed ring in source CRS: TL, TR, BR, BL, TL.
        src_ring = [
            (x_left, y_top),
            (x_right, y_top),
            (x_right, y_bot),
            (x_left, y_bot),
            (x_left, y_top),
        ]
        ring = [list(transformer.transform(x, y)) for (x, y) in src_ring]

        props: dict[str, Any] = {
            "cell_size_m": cell_size_m,
            "resolution_label": resolution_label,
            "row": int(r0),
            "col": int(c0),
        }
        if decimated:
            props["block"] = block
            props["decimated"] = True

        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": props,
            }
        )

    fc = {"type": "FeatureCollection", "features": features}
    meta = {
        "n_cells": len(features),
        "n_active": n_active,
        "decimated": decimated,
        "block": block,
        "effective_resolution_m": float(resolution_m * block),
        "cap": cap,
    }
    return fc, meta


def swmm_mesh_to_geojson(build: Any, *, cap: int = 6000) -> tuple[dict, dict]:
    """Recover the active cells from a ``BuildResult`` deck and build the mesh FC.

    Re-reads the staged ``.inp`` via
    :func:`swmm_mesh_builder._active_cells_from_deck` (the AUTHORITATIVE active-
    cell source - matches what a worker re-loading a staged deck sees) and feeds
    them to :func:`mesh_cells_to_feature_collection` with the build's
    georegistration (``transform`` / ``crs`` / ``resolution_m`` / ``grid_shape``).

    SYNC compute (deck read) - the caller wraps in ``asyncio.to_thread``.
    """
    from .swmm_mesh_builder import _active_cells_from_deck

    active_cells = _active_cells_from_deck(build)
    return mesh_cells_to_feature_collection(
        active_cells,
        list(build.transform),
        str(build.crs),
        float(build.resolution_m),
        tuple(build.grid_shape),
        cap=cap,
    )


def make_swmm_mesh_layer_uri(
    build: Any,
    *,
    run_id: str,
    runs_bucket: str | None = None,
    cap: int = 6000,
) -> LayerURI | None:
    """Build the mesh ``FeatureCollection`` + UPLOAD it to S3, return a LayerURI.

    DURABILITY FIX (NATE high-pri, shipped bug): the mesh ``.geojson`` used to be
    written to the deck STAGING dir (``/tmp/swmm-<run>/...``), which the composer
    DELETES on deck cleanup. On any session-state re-emit/reconnect the emitter
    re-reads the LayerURI ``uri`` via ``_read_vector_uri_as_geojson`` and hit
    'No such file or directory' -> the mesh layer could not re-inline -> it
    VANISHED + spammed a warning storm. We now UPLOAD ``mesh.geojson`` to the
    DURABLE runs bucket at
    ``s3://<runs_bucket>/<run_id>/mesh.geojson`` (the SAME convention every other
    run artifact uses, and the SAME s3:// path
    :func:`make_sfincs_mesh_layer_uri` relies on) and set ``LayerURI.uri`` to that
    s3:// path. ``pipeline_emitter.add_loaded_layer`` then inlines from s3://
    (boto3 instance-role GET, off the event loop) on EVERY re-read - durable like
    every other vector.

    Returns ``None`` (best-effort, never fatal) when there are zero features to
    render OR the S3 upload fails (a put failure -> the mesh is simply absent,
    never breaks the solve). The returned ``LayerURI`` carries
    ``style_preset="mesh_grid"``, ``role="context"`` and ``bbox=None`` (the mesh
    must not fight the AOI camera).

    SYNC compute + boto3 upload - the caller wraps it in ``asyncio.to_thread``
    (never run sync boto3 on the asyncio event loop).
    """
    try:
        fc, meta = swmm_mesh_to_geojson(build, cap=cap)
    except Exception as exc:  # noqa: BLE001 - best-effort; mesh emit is non-fatal
        logger.warning(
            "make_swmm_mesh_layer_uri: mesh build failed (non-fatal): %s", exc
        )
        return None

    if meta.get("n_cells", 0) <= 0:
        logger.info(
            "make_swmm_mesh_layer_uri: no active cells -> no mesh layer "
            "(run_id=%s)",
            run_id,
        )
        return None

    # Upload to the DURABLE runs bucket (NOT the soon-to-be-deleted deck dir).
    # Reuse the shared solver S3 seam (_get_s3_client / _get_runs_bucket) so the
    # mesh rides the EXACT same boto3 instance-role + bucket convention as every
    # other run artifact (postprocess COGs, completion.json, the SFINCS mesh).
    try:
        from ..tools.solver import _get_runs_bucket, _get_s3_client

        bucket = runs_bucket or _get_runs_bucket()
        key = f"{run_id}/mesh.geojson"
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(fc).encode("utf-8"),
            ContentType="application/geo+json",
        )
        s3_uri = f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001 - best-effort; S3 put failure is non-fatal
        logger.warning(
            "make_swmm_mesh_layer_uri: mesh.geojson S3 upload failed (non-fatal, "
            "mesh absent; run_id=%s): %s",
            run_id,
            exc,
        )
        return None

    eff_res = meta["effective_resolution_m"]
    return LayerURI(
        layer_id=f"swmm-mesh-{run_id}",
        name=f"Computational mesh ({eff_res:.0f} m cells)",
        layer_type="vector",
        uri=s3_uri,
        style_preset="mesh_grid",
        role="context",
        bbox=None,
    )


def make_sfincs_mesh_layer_uri(
    mesh_uri: str,
    *,
    run_id: str,
    n_cells: int | None = None,
) -> LayerURI | None:
    """THIN constructor over an ALREADY-BUILT SFINCS quadtree ``mesh.geojson``.

    NATE task #160 (coastal North Star): the cht_sfincs worker authors the
    VARIABLE-SIZE quadtree mesh and writes an ALREADY-EPSG:4326
    ``mesh.geojson`` to ``s3://<runs_bucket>/<run_id>/mesh.geojson``. Unlike the
    SWMM helper above, this function builds NO geometry, does NO reproject, and
    writes NO file - the worker already did all of that (the quadtree has
    per-face cell sizes/levels, so the SWMM row/col + single-affine
    :func:`mesh_cells_to_feature_collection` path is WRONG for it and must NOT
    be reused). We only construct a ``LayerURI`` over the worker's output and let
    the existing emitter inline the s3:// ``.geojson`` via
    ``pipeline_emitter.add_loaded_layer`` -> ``_read_vector_uri_as_geojson``
    (boto3 instance-role GET, already offloaded off the event loop).

    The returned ``LayerURI`` carries ``style_preset="mesh_grid"``,
    ``role="context"`` and ``bbox=None`` (the mesh must not fight the AOI
    camera - the regular flood camera owns the view). When ``n_cells`` is given
    the name reads ``Computational mesh (quadtree, N cells)``.

    BEST-EFFORT: a blank/falsy ``mesh_uri`` returns ``None`` (the caller's
    best-effort emit simply skips). This is pure dict work - NO asyncio.to_thread
    is needed; the s3 read happens later inside ``add_loaded_layer``.
    """
    if not mesh_uri or not str(mesh_uri).strip():
        return None

    if n_cells is not None and n_cells > 0:
        name = f"Computational mesh (quadtree, {n_cells} cells)"
    else:
        name = "Computational mesh (quadtree)"

    return LayerURI(
        layer_id=f"sfincs-mesh-{run_id}",
        name=name,
        layer_type="vector",
        uri=str(mesh_uri),
        style_preset="mesh_grid",
        role="context",
        bbox=None,
    )
