"""SFINCS run-output postprocessing.

``postprocess_flood(run_outputs_uri) → list[LayerURI]`` reads the SFINCS run's
raw output (NetCDF ``sfincs_map.nc`` carrying water depth time-series, plus
any auxiliary flux/water-level products HydroMT-SFINCS emits), extracts the
peak flood depth field, converts it to a Cloud-Optimized GeoTIFF, uploads to
GCS, and returns a typed ``LayerURI`` pointing at the COG.

Output format set is fixed by FR-CE-4 + FR-QS-3: rasters COG; vectors
FlatGeobuf/GeoParquet -- produced identically by engine, consumed identically
by QGIS Server + web. The postprocess output here is one COG (flood depth at
peak); future workflows may emit additional layers (flood velocity,
arrival-time COG, affected-buildings FlatGeobuf, …) -- extend the return list
when those land.

Style preset: ``continuous_flood_depth`` (a new preset name for the M5
substrate). The actual QML file lives in ``styles/`` (FROZEN under this job
per the kickoff), so the style_preset string here references a name that the
engine's styles follow-up job will author.

Tier separation (Invariant 5): the COG is written under
``s3://trid3nt-runs/<run_id>/`` (the runs bucket).
The agent service doesn't re-render -- QGIS Server picks up the URI from the
AssessmentEnvelope's ``ResultLayer`` and serves WMS/WMTS tiles.

This module is workflow-internal -- not registered as an atomic tool.
``model_flood_scenario`` calls it after ``wait_for_completion`` returns a
COMPLETE ``RunResult``.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trid3nt_contracts.execution import LayerURI

# STEP 1 (engine-coverage-levers): the frame machinery was lifted OUT of this
# module into ``frames.py`` (single source of truth for the time-stepped
# animation contract). ``MAX_FLOOD_FRAMES`` + ``_select_frame_time_indices`` are
# imported back here and RE-EXPORTED so every existing
# ``from .postprocess_flood import MAX_FLOOD_FRAMES, _select_frame_time_indices``
# (postprocess_swmm / _swan / _waves / _geoclaw + the agent tests) resolves to
# the SAME objects (byte-identical output guarantee).
from trid3nt_server.agent.workflows.shared.frames import (  # noqa: F401 - re-exported for backward compatibility
    MAX_FLOOD_FRAMES,
    _select_frame_time_indices,
)
from trid3nt_server.agent.workflows.shared.cog_io import (  # noqa: F401 - cross-engine seams, re-exported
    FLOOD_DEPTH_STYLE_PRESET,
    NODATA_DEPTH_M,
    RUNS_BUCKET_DEFAULT,
    _read_crs_from_dataset,
)

__all__ = [
    "PostprocessError",
    "postprocess_flood",
    "FLOOD_DEPTH_STYLE_PRESET",
    "NODATA_DEPTH_M",
    "RUNS_BUCKET_DEFAULT",
    "MAX_FLOOD_FRAMES",
]

logger = logging.getLogger("trid3nt_server.agent.workflows.sfincs.postprocess_sfincs")

# ``RUNS_BUCKET_DEFAULT`` / ``NODATA_DEPTH_M`` / ``FLOOD_DEPTH_STYLE_PRESET`` /
# ``_read_crs_from_dataset`` (shared/cog_io.py) and ``MAX_FLOOD_FRAMES`` /
# ``_select_frame_time_indices`` (shared/frames.py) are the CROSS-ENGINE seams --
# imported + re-exported above so this module's own callers/tests resolve to the
# SAME objects. Sibling engines import them straight from ``shared/``.


class PostprocessError(RuntimeError):
    """Raised by ``postprocess_flood`` on read / extraction / upload failures.

    Carries ``error_code`` matching the open-set A.6 surface so the agent
    emitter can render a typed error frame. Codes used here:

    - ``RUN_OUTPUT_READ_FAILED`` -- could not read the raw solver output
      (network, missing blob, malformed NetCDF).
    - ``RUN_OUTPUT_EMPTY`` -- output exists but contains no depth field /
      no timesteps (defensive; surfaces alongside the typed envelope
      so the user understands why the layer is missing).
    - ``RUN_OUTPUT_UNEXPECTED_SHAPE`` -- the extracted depth array has extra
      singleton dims that do not collapse to 2D after squeeze; indicates an
      unexpected HydroMT-SFINCS output shape variant.
    - ``COG_WRITE_FAILED`` -- rasterio could not write the COG (encoder
      error, disk full).
    - ``COG_UPLOAD_FAILED`` -- the GCS upload of the staged COG failed.
    - ``CRS_TAG_MISMATCH`` - belt-and-suspenders guard: the CRS tag written to
      the COG does not match what rasterio reads back, OR the tag's
      geographic/projected classification is inconsistent with the actual
      coordinate magnitudes (geographic → |x| ≤ 360; projected → |x| > 1000).
      Raised before the COG is uploaded to the runs bucket so a mistagged
      raster never lands in production.
    """

    error_code: str = "POSTPROCESS_FAILED"

    def __init__(
        self,
        error_code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


def _resolve_run_output_to_local(run_outputs_uri: str) -> Path:
    """Download (if gs:// / s3://) or resolve (if local) the run output to a
    local NetCDF.

    HydroMT-SFINCS standard output is ``sfincs_map.nc``; if ``run_outputs_uri``
    points at a directory or prefix we look for that filename inside it. If it
    points at a single file we use that.

    ``s3://`` run outputs (the local-docker solver
    backend's runs prefix) download via **boto3** through the solver module's
    shared S3 client seam - boto3 NOT s3fs (instance-role lesson).
    """
    if run_outputs_uri.startswith("s3://"):
        from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client

        tmpdir = Path(tempfile.mkdtemp(prefix="sfincs-output-"))
        local_target = tmpdir / "sfincs_map.nc"
        source = (
            run_outputs_uri
            if run_outputs_uri.endswith(".nc")
            else run_outputs_uri.rstrip("/") + "/sfincs_map.nc"
        )
        bucket_name, _, obj_key = source[len("s3://"):].partition("/")
        try:
            import shutil as _shutil

            resp = _get_s3_client().get_object(Bucket=bucket_name, Key=obj_key)
            with local_target.open("wb") as fh:
                _shutil.copyfileobj(resp["Body"], fh)
        except Exception as exc:  # noqa: BLE001
            raise PostprocessError(
                "RUN_OUTPUT_READ_FAILED",
                message=f"could not fetch run output {source}: {exc}",
                details={"run_outputs_uri": run_outputs_uri},
            ) from exc
        return local_target
    p = Path(run_outputs_uri.replace("file://", ""))
    if p.is_dir():
        candidate = p / "sfincs_map.nc"
        if candidate.exists():
            return candidate
    if p.exists():
        return p
    raise PostprocessError(
        "RUN_OUTPUT_READ_FAILED",
        message=f"run output not found at {run_outputs_uri}",
        details={"run_outputs_uri": run_outputs_uri},
    )




def _orient_array_for_cog(arr: Any, ds: Any) -> Any:
    """Apply the rotation + Y-flip + X-flip orientation guards to a 2D depth array.

    Centralizes the per-cell orientation corrections so they apply IDENTICALLY
    to every per-frame depth array. Pure geometry -- no masking, no I/O. Takes
    the squeezed 2D ``arr`` (rotation is decided from ``ds`` dim names below)
    and the open dataset; returns the correctly-oriented array
    (``(y_rows, x_cols)``, row 0 = north, col 0 = west).

    The rotation guard reads the ``x``/``y`` dim names from ``ds`` (not array
    shapes) so square grids are handled correctly. Y/X flips read the
    coordinate direction. All three degrade to identity on probe failure
    (defensive -- a bad coordinate read must never corrupt the raster, only
    skip the correction).
    """
    import numpy as np  # type: ignore[import-not-found]

    # --- Rotation fix ---
    # SFINCS netCDF convention: ds["x"].dims = ("m",), ds["y"].dims = ("n",)
    # where m=x-cols, n=y-rows. If the depth array's last two dims are
    # (x_dim, y_dim) instead of (y_dim, x_dim), transpose to (y_rows, x_cols).
    # We re-derive the dim ordering from the *array* shape vs the ds coord
    # lengths, since a single frame slice may not carry the original dim names.
    try:
        _x_dim = ds["x"].dims[0]  # e.g. "m"
        _y_dim = ds["y"].dims[0]  # e.g. "n"
        _n_x = int(ds.sizes.get(_x_dim, ds["x"].shape[0]))
        _n_y = int(ds.sizes.get(_y_dim, ds["y"].shape[0]))
        # If the array is (n_x, n_y) -- x-cols in rows -- transpose to (n_y, n_x).
        if (
            arr.ndim == 2
            and _n_x != _n_y
            and arr.shape[0] == _n_x
            and arr.shape[1] == _n_y
        ):
            logger.info(
                "postprocess_flood: transposing depth array shape %s — x-dim (%s,n=%d) "
                "is in rows; expected (y_rows=%d, x_cols=%d). Rotation fix (job-0071).",
                arr.shape, _x_dim, _n_x, _n_y, _n_x,
            )
            arr = arr.T
    except Exception:  # noqa: BLE001 -- dim inspection failure falls through to identity
        pass

    # --- Y-orientation guard ---
    # SFINCS often emits y ascending along rows (row 0 = south). COG transforms
    # declare row 0 = north, so flip rows when y ascends.
    try:
        _y_vals = ds["y"].values
        if _y_vals.ndim == 2:
            y_ascends_along_rows = bool(_y_vals[0, 0] < _y_vals[-1, 0])
        else:
            y_ascends_along_rows = bool(_y_vals[0] < _y_vals[-1])
        if y_ascends_along_rows:
            arr = arr[::-1, :]
    except Exception:  # noqa: BLE001 -- defensive; bad y → identity, no harm
        logger.warning("postprocess_flood: y-orientation probe failed; not flipping")

    # --- X-orientation guard (belt-and-suspenders) ---
    # Curvilinear grids can have x descending along columns (col 0 = east). COG
    # from_bounds always produces west-to-east, so flip cols when x descends.
    try:
        _x_vals = ds["x"].values
        if _x_vals.ndim == 2:
            x_descends_along_cols = bool(_x_vals[0, 0] > _x_vals[0, -1])
        else:
            x_descends_along_cols = bool(_x_vals[0] > _x_vals[-1])
        if x_descends_along_cols:
            arr = arr[:, ::-1]
    except Exception:  # noqa: BLE001 -- defensive; bad x → identity, no harm
        logger.warning("postprocess_flood: x-orientation probe failed; not flipping")

    return np.ascontiguousarray(arr)


def _is_quadtree_output(ds: Any) -> bool:
    """Probe whether a SFINCS dataset is a FACE-INDEXED UGRID (quadtree) output.

    The cht_sfincs quadtree solve writes a UGRID ``sfincs_map.nc`` whose fields
    live on ``nmesh2d_face`` (one scalar per quadtree face) with per-face
    coordinates ``mesh2d_face_x`` / ``mesh2d_face_y`` -- NOT the regular
    ``(n, m)`` grid + 1D ``x``/``y`` coords the legacy ``_write_verified_cog``
    ``from_bounds`` path assumes. ``_write_verified_cog`` branches on this probe
    so a face-indexed field routes through ``_rasterize_face_field`` (P1) instead
    of failing on the missing regular-grid coords. Probe is purely structural
    (dim name OR the face-x variable) so it never imports cht_sfincs.
    """
    try:
        dims = set(getattr(ds, "dims", {}))
        variables = set(getattr(ds, "variables", {}))
    except Exception:  # noqa: BLE001
        return False
    return "nmesh2d_face" in dims or "mesh2d_face_x" in variables


def _read_face_coords(ds: Any) -> tuple[Any, Any]:
    """Read the per-face centroid coordinates (UGRID quadtree output).

    Returns ``(face_x, face_y)`` 1D numpy arrays in the deck's projected CRS
    (UTM metres). Resolution order:

    1. ``mesh2d_face_x`` / ``mesh2d_face_y`` (or ``face_x`` / ``face_y``) --
       the canonical pre-computed per-face centroid coords. Fast path.
    2. **Compute from the UGRID node coords + connectivity.** The REAL
       cht_sfincs quadtree ``sfincs_map.nc`` does NOT carry ``mesh2d_face_x/_y``;
       it carries ``mesh2d_node_x`` / ``mesh2d_node_y`` (per-node coords) plus
       ``mesh2d_face_nodes`` (each face -> its corner node indices, 1-based via
       the ``start_index`` attr, fill in unused slots for non-quad faces). We
       compute each face centroid as the mean of its REAL corner-node coords:
       convert connectivity to 0-based, mask invalid/fill slots (non-finite,
       ``< start_index``, or ``>= n_nodes``) to NaN, gather node coords, and
       ``np.nanmean`` over the per-face node axis. Fully vectorized
       (n_faces x max_nodes index array, no Python face loop).

    Raised as ``RUN_OUTPUT_UNEXPECTED_SHAPE`` when neither the centroid coords
    NOR the node-coords+connectivity are present so a malformed quadtree output
    surfaces a typed error rather than a silent grayscale.
    """
    import numpy as np  # type: ignore[import-not-found]

    # --- Fast path: explicit per-face centroid coords. --- #
    for xk, yk in (("mesh2d_face_x", "mesh2d_face_y"), ("face_x", "face_y")):
        if xk in ds.variables and yk in ds.variables:
            return (
                np.asarray(ds[xk].values, dtype="float64").ravel(),
                np.asarray(ds[yk].values, dtype="float64").ravel(),
            )

    # --- Compute centroids from node coords + face->node connectivity. --- #
    node_x = node_y = conn = None
    for nxk, nyk in (("mesh2d_node_x", "mesh2d_node_y"), ("node_x", "node_y")):
        if nxk in ds.variables and nyk in ds.variables:
            node_x = np.asarray(ds[nxk].values, dtype="float64").ravel()
            node_y = np.asarray(ds[nyk].values, dtype="float64").ravel()
            break
    for ck in ("mesh2d_face_nodes", "face_nodes"):
        if ck in ds.variables:
            conn_var = ds[ck]
            conn = np.asarray(conn_var.values, dtype="float64")
            start_index = int(conn_var.attrs.get("start_index", 1))
            break

    if node_x is not None and node_y is not None and conn is not None:
        n_nodes = node_x.shape[0]
        if conn.ndim == 1:  # single-face edge case → (1, k)
            conn = conn[np.newaxis, :]
        # Convert 1-based (start_index) connectivity to 0-based int indices.
        idx0 = conn - float(start_index)
        # Mask invalid / fill slots: non-finite, negative (below start_index),
        # or out of range (>= n_nodes). Those slots contribute nothing to the
        # centroid mean so triangles/pentagons average only their real corners.
        valid = np.isfinite(idx0) & (idx0 >= 0.0) & (idx0 < float(n_nodes))
        safe = np.where(valid, idx0, 0.0).astype(np.intp)
        gx = np.where(valid, node_x[safe], np.nan)
        gy = np.where(valid, node_y[safe], np.nan)
        with np.errstate(invalid="ignore"):
            face_x = np.nanmean(gx, axis=1)
            face_y = np.nanmean(gy, axis=1)
        return (
            np.ascontiguousarray(face_x, dtype="float64"),
            np.ascontiguousarray(face_y, dtype="float64"),
        )

    raise PostprocessError(
        "RUN_OUTPUT_UNEXPECTED_SHAPE",
        message=(
            "quadtree output carries no face-centroid coordinates "
            "(mesh2d_face_x/_y or face_x/_y) and no node coords + "
            "face-node connectivity to compute them from"
        ),
        details={"variables": list(ds.variables.keys())},
    )


def _rasterize_face_field(
    values_1d: Any,
    face_x: Any,
    face_y: Any,
    *,
    crs: str,
    bbox: tuple[float, float, float, float] | None,
    resolution_m: float = 30.0,
) -> tuple[Any, Any]:
    """Grid a per-face scalar UGRID field onto a regular raster.

    The quadtree solve emits one scalar per face (``values_1d``) at the face
    centroids (``face_x``/``face_y`` in the deck's PROJECTED CRS -- UTM metres).
    To produce a COG the agent's TiTiler fast-path can serve, we interpolate the
    scattered per-face values onto a regular metric grid via
    ``scipy.interpolate.griddata`` (nearest-neighbour -- preserves the per-face
    value, no smoothing across the variable-size quadtree, and never invents
    intermediate magnitudes), at ``resolution_m`` metres.

    The output raster is authored in the SAME projected CRS as the face coords
    (UTM); the COG carries that CRS tag so MapLibre/TiTiler reproject on the fly.
    ``bbox`` (EPSG:4326) is reprojected to the face CRS to bound the output grid
    when supplied; otherwise the face-coordinate extent is used.

    Returns ``(arr_2d, transform)`` -- a float32 2D array (row 0 = north) and the
    rasterio Affine. NaN fills cells with no nearby face (outside the convex hull
    of the mesh) so the dry/no-data mask downstream stays honest.

    Never imports the GPL cht packages -- pure numpy + scipy + the face vars off
    the NetCDF (the 1.2GB cht deck-builder stays in the worker image).
    """
    import numpy as np  # type: ignore[import-not-found]
    import rasterio  # type: ignore[import-not-found]
    from scipy.interpolate import griddata  # type: ignore[import-not-found]

    vals = np.asarray(values_1d, dtype="float64").ravel()
    fx = np.asarray(face_x, dtype="float64").ravel()
    fy = np.asarray(face_y, dtype="float64").ravel()
    if not (vals.shape[0] == fx.shape[0] == fy.shape[0]):
        raise PostprocessError(
            "RUN_OUTPUT_UNEXPECTED_SHAPE",
            message=(
                f"quadtree face field length {vals.shape[0]} != face-coord "
                f"length ({fx.shape[0]}, {fy.shape[0]})"
            ),
            details={"n_values": int(vals.shape[0]), "n_faces": int(fx.shape[0])},
        )

    # Drop non-finite faces (defensive -- a NaN centroid would poison the grid).
    finite = np.isfinite(fx) & np.isfinite(fy)
    fx, fy, vals = fx[finite], fy[finite], vals[finite]
    if fx.size == 0:
        raise PostprocessError(
            "RUN_OUTPUT_EMPTY",
            message="quadtree output has no finite face centroids",
            details={},
        )

    # --- output grid extent in the FACE (projected) CRS ---
    # Reproject the AOI bbox (EPSG:4326) into the face CRS when supplied; else
    # bound to the face-coordinate extent. The face CRS is UTM metres so the
    # resolution is directly metres-per-pixel.
    minx = maxx = miny = maxy = None
    if bbox is not None:
        try:
            from pyproj import Transformer  # type: ignore[import-not-found]

            tf = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
            bx0, by0 = tf.transform(float(bbox[0]), float(bbox[1]))
            bx1, by1 = tf.transform(float(bbox[2]), float(bbox[3]))
            minx, maxx = min(bx0, bx1), max(bx0, bx1)
            miny, maxy = min(by0, by1), max(by0, by1)
        except Exception as exc:  # noqa: BLE001 -- fall back to the face extent
            logger.warning(
                "postprocess_flood: bbox->%s reproject for the quadtree raster "
                "grid failed (%s); bounding to the face extent instead.",
                crs, exc,
            )
            minx = maxx = miny = maxy = None
    if minx is None:
        minx, maxx = float(fx.min()), float(fx.max())
        miny, maxy = float(fy.min()), float(fy.max())

    res = max(1.0, float(resolution_m))
    width = max(1, int(np.ceil((maxx - minx) / res)))
    height = max(1, int(np.ceil((maxy - miny) / res)))
    # Guard against a degenerate / pathological grid blowing memory.
    _MAX_DIM = 8192
    if width > _MAX_DIM or height > _MAX_DIM:
        scale = max(width / _MAX_DIM, height / _MAX_DIM)
        res = res * scale
        width = max(1, int(np.ceil((maxx - minx) / res)))
        height = max(1, int(np.ceil((maxy - miny) / res)))

    # Pixel centres (row 0 = north → descending y).
    xs = minx + (np.arange(width) + 0.5) * res
    ys = maxy - (np.arange(height) + 0.5) * res
    grid_x, grid_y = np.meshgrid(xs, ys)

    arr = griddata(
        (fx, fy), vals, (grid_x, grid_y), method="nearest"
    ).astype("float32")
    # Mask grid cells outside the mesh's convex hull (no nearby face) to NaN so
    # nearest-neighbour does not stretch the edge value across empty space.
    try:
        hull_mask = griddata(
            (fx, fy), np.ones_like(vals), (grid_x, grid_y), method="linear"
        )
        arr = np.where(np.isfinite(hull_mask), arr, np.nan).astype("float32")
    except Exception:  # noqa: BLE001 -- convex-hull mask is best-effort
        pass

    transform = rasterio.transform.from_bounds(
        minx, miny, maxx, maxy, width, height
    )
    return np.ascontiguousarray(arr), transform


def _write_verified_cog(
    arr_2d: Any,
    *,
    ds: Any,
    netcdf_path: Path,
    face_values: Any = None,
    bbox: tuple[float, float, float, float] | None = None,
    resolution_m: float = 30.0,
    nodata_threshold_m: float = NODATA_DEPTH_M,
) -> tuple[Path, dict[str, Any]]:
    """Orient, mask, write, and CRS-verify a single 2D field as a COG.

    The reusable per-frame COG writer (flood-animation Phase 1; now quadtree
    aware, P1). Two input modes:

    - **Regular grid** (default): ``arr_2d`` is a 2D ``(n, m)`` array; the
      rotation/Y-flip/X-flip orientation guards (``_orient_array_for_cog``) are
      applied and the transform comes from the 1D ``x``/``y`` coords via
      ``rasterio.transform.from_bounds`` (legacy path, byte-identical when
      ``face_values is None`` and the dataset is NOT face-indexed).
    - **Quadtree / face-indexed UGRID**: when the dataset is face-indexed
      (``_is_quadtree_output``) OR ``face_values`` (a 1D per-face array) is
      supplied, the field is rasterized via ``_rasterize_face_field`` onto a
      regular metric grid in the deck's projected (UTM) CRS -- no ``from_bounds``
      regular-grid assumption. This is what fixes BOTH depth and waves on the
      true quadtree path (the legacy path would raise on the missing ``x``/``y``
      regular coords).

    Sub-threshold values (< ``nodata_threshold_m``) are masked to NaN so the COG
    is dry/no-data aware. Returns ``(tmp_cog_path, metrics_summary)``
    with the field aggregates (max/mean/p95/flooded_cell_count) + ``crs`` +
    ``units``.
    """
    import numpy as np  # type: ignore[import-not-found]
    import rasterio  # type: ignore[import-not-found]

    crs = _read_crs_from_dataset(ds)

    # --- Quadtree / face-indexed branch (P1) -------------------------------- #
    # Route a per-face scalar field through the UGRID rasterizer. Triggered when
    # an explicit ``face_values`` 1D array is passed OR the dataset is face
    # indexed (then ``arr_2d`` IS the 1D per-face field). Reads face geometry
    # straight off the NetCDF (mesh2d_face_x/_y) -- never imports cht_sfincs.
    face_indexed = _is_quadtree_output(ds)
    face_field = face_values if face_values is not None else (
        arr_2d if face_indexed else None
    )
    if face_indexed or face_values is not None:
        face_x, face_y = _read_face_coords(ds)
        arr, transform = _rasterize_face_field(
            face_field,
            face_x,
            face_y,
            crs=crs,
            bbox=bbox,
            resolution_m=resolution_m,
        )
        arr_masked = np.where(arr > nodata_threshold_m, arr, np.nan)
        flooded = arr_masked[~np.isnan(arr_masked)]
        if flooded.size == 0:
            metrics_summary: dict[str, Any] = {
                "max_depth_m": 0.0,
                "mean_depth_m": 0.0,
                "p95_depth_m": 0.0,
                "flooded_cell_count": 0,
            }
        else:
            metrics_summary = {
                "max_depth_m": float(np.nanmax(flooded)),
                "mean_depth_m": float(np.nanmean(flooded)),
                "p95_depth_m": float(np.nanpercentile(flooded, 95)),
                "flooded_cell_count": int(flooded.size),
            }
        return _finalize_cog(
            arr_masked, crs=crs, transform=transform, netcdf_path=netcdf_path,
            metrics_summary=metrics_summary,
        )

    # --- Regular-grid branch (legacy, byte-identical) ----------------------- #
    arr = np.asarray(arr_2d, dtype="float32")
    if arr.ndim > 2:
        arr = np.squeeze(arr)
    if arr.ndim != 2:
        raise PostprocessError(
            "RUN_OUTPUT_UNEXPECTED_SHAPE",
            message=(
                f"depth array has shape {arr.shape}; expected 2D after squeeze"
            ),
            details={"netcdf_path": str(netcdf_path), "shape": list(arr.shape)},
        )

    arr = _orient_array_for_cog(arr, ds)

    # Mask sub-threshold depths to NaN so the COG is dry-cell-aware.
    arr_masked = np.where(arr > nodata_threshold_m, arr, np.nan)
    flooded = arr_masked[~np.isnan(arr_masked)]
    if flooded.size == 0:
        metrics_summary = {
            "max_depth_m": 0.0,
            "mean_depth_m": 0.0,
            "p95_depth_m": 0.0,
            "flooded_cell_count": 0,
        }
    else:
        metrics_summary = {
            "max_depth_m": float(np.nanmax(flooded)),
            "mean_depth_m": float(np.nanmean(flooded)),
            "p95_depth_m": float(np.nanpercentile(flooded, 95)),
            "flooded_cell_count": int(flooded.size),
        }

    # CRS + transform from the dataset (CF-convention 'crs' variable; fix).
    try:
        _x = ds["x"].values
        _y = ds["y"].values
        transform = rasterio.transform.from_bounds(
            float(_x.min()), float(_y.min()), float(_x.max()), float(_y.max()),
            arr.shape[-1], arr.shape[-2],
        )
    except Exception:  # noqa: BLE001
        transform = rasterio.Affine.identity()

    return _finalize_cog(
        arr_masked, crs=crs, transform=transform, netcdf_path=netcdf_path,
        metrics_summary=metrics_summary,
    )


def _finalize_cog(
    arr_masked: Any,
    *,
    crs: str,
    transform: Any,
    netcdf_path: Path,
    metrics_summary: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """Write the (already oriented + masked) 2D array as a CRS-verified COG.

    Shared tail of ``_write_verified_cog`` (both the regular-grid and the
    quadtree-rasterized branches feed identical bytes here): write the LZW COG to
    a tmp path, then re-open it to assert the CRS tag round-trips + the
    geographic/projected classification is consistent with the bounds magnitudes
    (the TiTiler-wedge / mistagged-raster guards). Returns
    ``(tmp_cog_path, metrics_summary)`` with ``crs`` + ``units`` attached.
    """
    import numpy as np  # type: ignore[import-not-found]
    import rasterio  # type: ignore[import-not-found]

    arr_masked = np.asarray(arr_masked, dtype="float32")
    tmp_cog = Path(tempfile.NamedTemporaryFile(suffix=".tif", delete=False).name)
    try:
        with rasterio.open(
            tmp_cog,
            "w",
            driver="COG",
            width=arr_masked.shape[-1],
            height=arr_masked.shape[-2],
            count=1,
            dtype="float32",
            crs=crs,
            transform=transform,
            nodata=float("nan"),
            compress="LZW",
        ) as dst:
            dst.write(arr_masked.astype("float32"), 1)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessError(
            "COG_WRITE_FAILED",
            message=f"COG write failed: {exc}",
            details={"netcdf_path": str(netcdf_path)},
        ) from exc

    # --- CRS_TAG_MISMATCH guard (research-workflow) ---
    # Re-open the COG and verify the CRS tag round-trips BEFORE any upload. This
    # is also the per-frame VALID-COG assertion (a frame that can't be re-opened
    # or carries a bad CRS tag raises here, never reaching the runs bucket).
    with rasterio.open(tmp_cog, "r") as verify:
        if str(verify.crs) != str(crs):
            raise PostprocessError(
                "CRS_TAG_MISMATCH",
                message=(
                    f"COG written with crs={crs!r} but rasterio read back "
                    f"{verify.crs!r}"
                ),
                details={"netcdf_path": str(netcdf_path)},
            )
        is_geographic = verify.crs.is_geographic
        bounds_max = max(abs(verify.bounds.left), abs(verify.bounds.right))
        if is_geographic and bounds_max > 360:
            raise PostprocessError(
                "CRS_TAG_MISMATCH",
                message=(
                    f"crs={crs!r} is geographic but bounds.left="
                    f"{verify.bounds.left} implies projected coords (|x|>360)"
                ),
                details={"netcdf_path": str(netcdf_path)},
            )
        if (not is_geographic) and bounds_max < 1000:
            raise PostprocessError(
                "CRS_TAG_MISMATCH",
                message=(
                    f"crs={crs!r} is projected but bounds.left="
                    f"{verify.bounds.left} implies geographic coords (|x|<1000)"
                ),
                details={"netcdf_path": str(netcdf_path)},
            )

    metrics_summary["crs"] = crs
    metrics_summary["units"] = "meters"
    return tmp_cog, metrics_summary


def _collapse_running_max(field: Any) -> Any:
    """Collapse a SFINCS running-max field (``hmax`` / ``zsmax``) to a 2D peak.

    SFINCS writes its max fields with a leading ``timemax`` axis whose length is
    ``ceil(tstop-tstart / dtmaxout)``. When ``dtmaxout >= sim-length`` that axis
    is size 1 (a single global max, squeezes away cleanly). But when ``dtmaxout``
    is set FINER than the sim window -- which the flood-animation deck does
    (``dtmaxout = max(600, total/24)`` in sfincs_builder, giving ~24 blocks over a
    24h sim) -- SFINCS emits a SEQUENCE of running-max snapshots: ``hmax`` arrives
    as ``(timemax=24, n, m)``. The representative PEAK is the max OVER those
    blocks, so we reduce any ``timemax``/``time`` leading axis here. Without this
    the peak array stays 3D and ``_write_verified_cog``'s squeeze raises
    ``RUN_OUTPUT_UNEXPECTED_SHAPE`` -- sinking BOTH the peak layer and every
    animation frame (the whole flood layer set vanishes on an otherwise-good
    solve). Any non-spatial reduce dim present on the field is collapsed; the
    spatial ``n``/``m`` dims are left intact.
    """
    reduce_dims = [d for d in getattr(field, "dims", ()) if d in ("timemax", "time")]
    for d in reduce_dims:
        field = field.max(dim=d)
    return field


def _select_peak_depth(ds: Any) -> Any:
    """Select the PEAK (max-over-time) depth field from a SFINCS dataset.

    Fallback order: ``hmax`` (max water depth, direct) → ``zsmax - zb`` (max
    water-level minus bed) → ``zs.max(time) - zb`` (max of the time series).
    Returns an xarray DataArray (NOT yet a numpy array). Raises
    ``RUN_OUTPUT_EMPTY`` when no depth field is present.

    ``hmax`` / ``zsmax`` carry a leading ``timemax`` axis that is size 1 when
    ``dtmaxout >= sim-length`` but size N when the deck sets a finer
    ``dtmaxout`` (the animation deck does -- ~24 running-max blocks). We collapse
    that axis to a true global 2D peak via ``_collapse_running_max`` so the COG
    writer always receives a 2D field.
    """
    if "hmax" in ds.variables:
        return _collapse_running_max(ds["hmax"])
    if "zsmax" in ds.variables and "zb" in ds.variables:
        return _collapse_running_max(ds["zsmax"]) - ds["zb"]
    if "zs" in ds.variables and "zb" in ds.variables:
        return (ds["zs"].max(dim="time") - ds["zb"]).clip(min=0.0)
    raise PostprocessError(
        "RUN_OUTPUT_EMPTY",
        message=(
            f"sfincs_map.nc carries neither hmax nor zsmax/zs+zb; "
            "no depth field to extract."
        ),
        details={"variables": list(ds.variables.keys())},
    )


# ``_select_frame_time_indices`` now lives in ``frames.py`` (STEP 1
# extract-in-place) and is imported + re-exported at the top of this module. The
# SFINCS frame loop below calls it unchanged.


def _extract_peak_depth_geotiff(netcdf_path: Path) -> tuple[Path, dict[str, Any]]:
    """Read sfincs_map.nc, compute the per-cell peak depth, write a COG to a tmp path.

    SFINCS publishes ``zsmax`` (max water-level) and ``zs`` (water-level time
    series); the depth at peak is ``zsmax - zb`` (water-level minus bed-level).
    HydroMT-SFINCS variants emit ``hmax`` (max water depth) directly. We try
    ``hmax`` first; fall back to computing it from ``zsmax`` - ``zb``.

    Returns the path to the staged COG and a metadata dict (max/mean/p95
    depth, units, crs string) the AssessmentEnvelope's FloodMetrics consumes.
    """
    try:
        import xarray as xr  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessError(
            "RUN_OUTPUT_READ_FAILED",
            message=f"xarray/rasterio/numpy not available: {exc}",
            details={"netcdf_path": str(netcdf_path)},
        ) from exc

    try:
        ds = xr.open_dataset(str(netcdf_path))
    except Exception as exc:  # noqa: BLE001
        raise PostprocessError(
            "RUN_OUTPUT_READ_FAILED",
            message=f"xarray could not open {netcdf_path}: {exc}",
            details={"netcdf_path": str(netcdf_path)},
        ) from exc

    try:
        depth = _select_peak_depth(ds)
        return _write_verified_cog(depth.values, ds=ds, netcdf_path=netcdf_path)
    finally:
        try:
            ds.close()
        except Exception:  # noqa: BLE001
            pass


def _extract_depth_frames(
    netcdf_path: Path,
) -> tuple[Path, dict[str, Any], list[Path], list[str]]:
    """Extract the PEAK depth COG AND (when time-varying output exists) N per-frame
    depth COGs from a SFINCS map output -- the engine-agnostic flood-animation core.

    Returns ``(peak_cog, peak_metrics, frame_cogs, frame_labels)``:

    - ``peak_cog`` -- the representative max-depth COG (always produced; identical
      to the legacy ``_extract_peak_depth_geotiff`` output). Drives FloodMetrics
      + the habitat/Pelicun/honesty-floor consumers (regression-safe).
    - ``peak_metrics`` -- PEAK aggregates (max/mean/p95/flooded_cell_count) +
      crs/units. Computed over the PEAK field, NOT a single frame.
    - ``frame_cogs`` -- up to ``MAX_FLOOD_FRAMES`` per-timestep depth COGs in
      ASCENDING time order, evenly subsampled (first + last always kept). EMPTY
      when the dataset has no usable time-varying water level (only hmax/zsmax,
      or a single zs timestep) → caller emits ONLY the peak layer (full
      backward-compat). Each frame COG is orientation-corrected + CRS-verified
      by ``_write_verified_cog`` (the per-frame VALID-COG guard).
    - ``frame_labels`` -- parallel short labels (e.g. ``"step 1"``) for provenance;
      the AUTHORITATIVE web grouping token lives in the LayerURI NAME the caller
      assigns ("Flood depth step N"), NOT here.

    The per-frame path REQUIRES ``zs(time,n,m)`` + ``zb(n,m)`` with a time dim of
    length > 1 -- which only exists once ``dtout`` is set in the SFINCS deck
    (sfincs_builder). Without time-varying output the function degrades cleanly
    to the single-max behavior.
    """
    try:
        import xarray as xr  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessError(
            "RUN_OUTPUT_READ_FAILED",
            message=f"xarray/rasterio/numpy not available: {exc}",
            details={"netcdf_path": str(netcdf_path)},
        ) from exc

    try:
        ds = xr.open_dataset(str(netcdf_path))
    except Exception as exc:  # noqa: BLE001
        raise PostprocessError(
            "RUN_OUTPUT_READ_FAILED",
            message=f"xarray could not open {netcdf_path}: {exc}",
            details={"netcdf_path": str(netcdf_path)},
        ) from exc

    frame_cogs: list[Path] = []
    frame_labels: list[str] = []
    try:
        # Peak COG + metrics ALWAYS (the representative + FloodMetrics source).
        peak_field = _select_peak_depth(ds)
        peak_cog, peak_metrics = _write_verified_cog(
            peak_field.values, ds=ds, netcdf_path=netcdf_path
        )

        # Per-frame path: only when zs(time,...)+zb carry a real time dim > 1.
        has_timeseries = (
            "zs" in ds.variables
            and "zb" in ds.variables
            and "time" in ds["zs"].dims
        )
        if has_timeseries:
            n_steps = int(ds.sizes.get("time", ds["zs"].sizes.get("time", 0)))
            if n_steps > 1:
                zb = ds["zb"]
                indices = _select_frame_time_indices(n_steps)
                for frame_no, t_idx in enumerate(indices, start=1):
                    depth_t = (ds["zs"].isel(time=t_idx) - zb).clip(min=0.0)
                    try:
                        frame_cog, _frame_metrics = _write_verified_cog(
                            depth_t.values, ds=ds, netcdf_path=netcdf_path
                        )
                    except PostprocessError:
                        # A single corrupt frame must not sink the whole animation
                        # OR the peak layer. Clean up partial frames and degrade to
                        # the peak-only path (honest: better one good layer than a
                        # broken group). Re-raise only the peak-write failures above.
                        logger.warning(
                            "postprocess_flood: frame %d (t=%d) COG write/verify "
                            "failed; degrading to peak-only (no animation group).",
                            frame_no, t_idx,
                        )
                        for p in frame_cogs:
                            try:
                                p.unlink(missing_ok=True)
                            except Exception:  # noqa: BLE001
                                pass
                        frame_cogs = []
                        frame_labels = []
                        break
                    frame_cogs.append(frame_cog)
                    frame_labels.append(f"step {frame_no}")
                # A 1-frame "group" can never form on the web (needs >= 2 distinct
                # values); drop it so we never publish a lone styled frame row.
                if len(frame_cogs) < 2:
                    for p in frame_cogs:
                        try:
                            p.unlink(missing_ok=True)
                        except Exception:  # noqa: BLE001
                            pass
                    frame_cogs = []
                    frame_labels = []

        return peak_cog, peak_metrics, frame_cogs, frame_labels
    finally:
        try:
            ds.close()
        except Exception:  # noqa: BLE001
            pass


def _upload_cog_to_runs_bucket(
    local_cog: Path,
    run_id: str,
    runs_bucket: str | None = None,
    *,
    dest_filename: str = "flood_depth_peak.tif",
) -> str:
    """Upload the staged COG to
    ``s3://<runs_bucket>/<run_id>/<dest_filename>``.

    ``dest_filename`` defaults to ``flood_depth_peak.tif`` (the peak layer,
    byte-identical key to the pre-animation path). Per-frame callers pass a
    DISTINCT name (e.g. ``flood_depth_frame_03.tif``) so each frame COG lands at
    its own object key → its own ``url=`` in the TiTiler tile template → its own
    ``_layer_identity_key`` (no dedup collision; the sequential group keeps all
    its members).

    GCP is decommissioned (GCP-teardown): the upload always goes via
    **boto3** and the runs bucket MUST come from
    ``TRID3NT_RUNS_BUCKET`` / the explicit ``runs_bucket`` arg.
    """
    bucket = runs_bucket or (os.environ.get("TRID3NT_RUNS_BUCKET") or "").strip()
    if not bucket:
        raise PostprocessError(
            "COG_UPLOAD_FAILED",
            message=(
                "TRID3NT_RUNS_BUCKET must be set (S3-only; no GCP-named default)"
            ),
            details={"local_cog": str(local_cog)},
        )
    dest = f"s3://{bucket}/{run_id}/{dest_filename}"
    try:
        from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client

        with local_cog.open("rb") as fh:
            _get_s3_client().put_object(
                Bucket=bucket,
                Key=f"{run_id}/{dest_filename}",
                Body=fh,
                ContentType="image/tiff",
            )
    except Exception as exc:  # noqa: BLE001
        raise PostprocessError(
            "COG_UPLOAD_FAILED",
            message=f"upload of {local_cog} to {dest} failed: {exc}",
            details={"local_cog": str(local_cog), "dest": dest},
        ) from exc
    logger.info("uploaded flood-depth COG to %s (boto3)", dest)
    return dest


def postprocess_flood(
    run_outputs_uri: str,
    *,
    run_id: str,
    runs_bucket: str | None = None,
) -> tuple[list[LayerURI], dict[str, Any]]:
    """Convert a SFINCS run's NetCDF output into a flood-depth COG ``LayerURI``.

    Use this when the workflow has a SUCCEEDED ``RunResult`` and needs to
    materialize the renderable layers for the AssessmentEnvelope. v0.1 returns
    a single-element layer list (flood depth at peak); future products
    (velocity, arrival time) extend the list.

    Args:
        run_outputs_uri: the ``gs://`` URI of the SFINCS run output (the
            ``RunResult.output_uri`` from ``wait_for_completion``; may be a
            directory containing ``sfincs_map.nc`` or the NetCDF directly).
        run_id: the run identifier the COG is keyed under in the runs bucket.
        runs_bucket: optional override for the runs bucket name.

    Returns:
        A tuple ``(layers, metrics)`` where ``layers`` is a list of
        ``LayerURI`` and ``metrics`` is a dict carrying the PEAK aggregates
        (``max_depth_m``, ``mean_depth_m``, ``p95_depth_m``,
        ``flooded_cell_count``) for the workflow to populate ``FloodMetrics``.

        ``layers[0]`` is ALWAYS the representative peak flood-depth COG
        (``layer_id=flood-depth-peak-{run_id}``, name ``"Peak flood depth"``,
        role ``"primary"``) -- the regression-safe single layer the habitat /
        Pelicun / honesty-floor / wrapper-return consumers read. ``layers[1:]``
        (present ONLY when the SFINCS output carries time-varying water level)
        are up to ``MAX_FLOOD_FRAMES`` per-timestep depth COGs named
        ``"Flood depth step N"`` (N = 1..k, contiguous, 1-based) with role
        ``"context"`` -- the web ``parseFrameToken`` recognizes the ``step N``
        token and ``detectSequentialGroups`` collapses them into ONE bottom-
        center-scrubber temporal group (engine-agnostic flood animation,
        Phase 1). Each frame COG lands at a DISTINCT runs-bucket key so its
        ``url=`` (hence ``_layer_identity_key``) is distinct → no dedup collapse.

    Raises:
        PostprocessError: any step of the read → COG-write → upload chain
            failed; ``error_code`` identifies the stage.
    """
    netcdf_path = _resolve_run_output_to_local(run_outputs_uri)
    peak_cog, metrics, frame_cogs, frame_labels = _extract_depth_frames(netcdf_path)

    # --- Peak (representative) layer -- ALWAYS layers[0], unchanged contract. ---
    try:
        peak_uri = _upload_cog_to_runs_bucket(peak_cog, run_id, runs_bucket)
    finally:
        try:
            peak_cog.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    layers: list[LayerURI] = [
        LayerURI(
            # job (flood-duplicate-layer fix): a clear human-readable name --
            # "Peak flood depth" -- so the LayerPanel row matches the
            # white->blue->green ``continuous_flood_depth`` styling. The
            # style_preset MUST stay set (FLOOD_DEPTH_STYLE_PRESET); a layer that
            # reaches publish_layer / the map with NO preset falls through to the
            # raw COG and TiTiler renders it in matplotlib viridis (the redundant
            # unstyled-duplicate symptom).
            layer_id=f"flood-depth-peak-{run_id}",
            name="Peak flood depth",
            layer_type="raster",
            uri=peak_uri,
            style_preset=FLOOD_DEPTH_STYLE_PRESET,
            role="primary",
            units="meters",
        )
    ]

    # --- Per-frame layers (time-stepped animation, engine-agnostic). ---
    # Each frame uploads to a DISTINCT key flood_depth_frame_{NN:02d}.tif so its
    # TiTiler url= (→ _layer_identity_key) is unique and the dedup keeps every
    # frame. Names carry the EXACT web token ("Flood depth step N") so the panel
    # forms the sequential group. role="context" (LayerURI.role is a closed
    # Literal["primary","context","input"]; frames are NOT the primary peak
    # layer, so they ride as context -- the grouping key on the web side is the
    # NAME token + style_preset + bbox-signature, never the role).
    for frame_no, (frame_cog, _label) in enumerate(
        zip(frame_cogs, frame_labels), start=1
    ):
        try:
            frame_uri = _upload_cog_to_runs_bucket(
                frame_cog,
                run_id,
                runs_bucket,
                dest_filename=f"flood_depth_frame_{frame_no:02d}.tif",
            )
        finally:
            try:
                frame_cog.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        layers.append(
            LayerURI(
                layer_id=f"flood-depth-frame-{frame_no:02d}-{run_id}",
                name=f"Flood depth step {frame_no}",
                layer_type="raster",
                uri=frame_uri,
                style_preset=FLOOD_DEPTH_STYLE_PRESET,
                role="context",
                units="meters",
            )
        )

    if len(layers) > 1:
        logger.info(
            "postprocess_flood: emitted peak layer + %d time-step frames "
            "(animation group) for run_id=%s",
            len(layers) - 1,
            run_id,
        )
    return layers, metrics
