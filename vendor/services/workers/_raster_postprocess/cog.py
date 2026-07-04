"""COG encode tail: orient, face-rasterize, COG+overviews, CRS round-trip verify.

LIFTED verbatim-in-spirit from the agent's
``grace2_agent.workflows.postprocess_flood`` (``_orient_array_for_cog`` /
``_rasterize_face_field`` / ``_finalize_cog`` + the ``_read_crs_from_dataset`` /
``_read_face_coords`` / ``_is_quadtree_output`` readers) and made GPL-free +
agent-import-free.

Every public function here is PURE (numpy / scipy / rasterio / pyproj only) so a
single frame can be encoded in its own subprocess (``concurrent.futures``
ProcessPool, one GDAL dataset per process => bounded peak memory, GDAL-safe).

The CRS / face-coord readers take an OPEN xarray ``ds`` (cheap, read once in the
parent), but the encode functions (:func:`rasterize_face_field`,
:func:`finalize_cog`, :func:`write_field_cog`) take only plain numpy arrays +
floats + the resolved ``crs`` string so they pickle cheaply across processes and
never re-open the NetCDF.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

LOG = logging.getLogger("grace2.worker.raster_postprocess.cog")

#: Default depth/wave no-data threshold (metres). Sub-threshold cells are masked
#: to NaN so a COG is dry/no-data aware. 5 cm matches the agent's
#: ``NODATA_DEPTH_M`` / ``NODATA_WAVE_M`` and the lowest QML colour stop.
NODATA_DEPTH_M: float = 0.05

#: Guard against a degenerate quadtree grid blowing memory (agent parity).
_MAX_GRID_DIM = 8192

#: Minimum output dimension (px) below which we upsample so the COG driver builds
#: real overviews. The SFINCS quadtree on a tiny AOI can produce a sub-256px
#: raster; without this the agent's ``_ensure_raster_has_overviews`` no-op
#: assumption breaks. 768 (the SWAN ``_upsample_for_cog`` value) is deliberately
#: past the rasterio COG driver's 512px overview block-size threshold, so a raster
#: at/under this size is guaranteed overviews after the upsample.
_COG_MIN_DIM_PX = 768


class CogError(RuntimeError):
    """Raised on a COG read / rasterize / write / CRS-verify failure.

    ``error_code`` mirrors the agent's ``PostprocessError`` open-set codes so the
    manifest + completion.json carry the SAME typed surface the agent already
    knows: ``RUN_OUTPUT_EMPTY`` / ``RUN_OUTPUT_UNEXPECTED_SHAPE`` /
    ``COG_WRITE_FAILED`` / ``CRS_TAG_MISMATCH``.
    """

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


# --------------------------------------------------------------------------- #
# CRS + face-coord readers (take an OPEN xarray ds; read once in the parent).
# --------------------------------------------------------------------------- #


def read_crs_from_dataset(ds: Any) -> str:
    """Read the CRS from a SFINCS NetCDF dataset (CF-convention ``crs`` var).

    SFINCS stores the CRS in a DATA VARIABLE named ``crs`` (not ``ds.attrs``).
    Resolution order (lifted from postprocess_flood._read_crs_from_dataset):
      1. ``crs_var.attrs['epsg_code']`` ("EPSG:32617" / bare int)
      2. ``crs_var.attrs['epsg'|'EPSG']`` (bare int; cht placeholder '-' skipped)
      3. ``crs_var.attrs['crs_wkt'|'spatial_ref'|'wkt']`` (parse via pyproj)
      4. the crs VARIABLE VALUE itself (cht quadtree stores the bare int EPSG)
      5. fallback ``ds.attrs.get('crs', 'EPSG:3857')`` (logged warning)
    """
    if "crs" in getattr(ds, "variables", {}):
        crs_var = ds["crs"]
        attrs = crs_var.attrs

        if "epsg_code" in attrs:
            raw = str(attrs["epsg_code"]).strip()
            if raw.upper().startswith("EPSG:"):
                return raw
            try:
                return f"EPSG:{int(raw)}"
            except ValueError:
                pass

        for epsg_key in ("epsg", "EPSG"):
            if epsg_key in attrs:
                try:
                    return f"EPSG:{int(str(attrs[epsg_key]).strip())}"
                except (ValueError, TypeError):
                    pass

        for wkt_key in ("crs_wkt", "spatial_ref", "wkt"):
            if wkt_key in attrs:
                try:
                    import pyproj  # type: ignore

                    return pyproj.CRS.from_wkt(attrs[wkt_key]).to_string()
                except Exception:  # noqa: BLE001
                    pass

        try:
            import numpy as np  # type: ignore

            raw_val = np.asarray(crs_var.values).ravel()
            if raw_val.size >= 1 and np.isfinite(raw_val[0]):
                epsg_int = int(raw_val[0])
                if epsg_int > 0:
                    try:
                        import pyproj  # type: ignore

                        return pyproj.CRS.from_epsg(epsg_int).to_string()
                    except Exception:  # noqa: BLE001
                        return f"EPSG:{epsg_int}"
        except Exception:  # noqa: BLE001
            pass

    fallback = ds.attrs.get("crs", "EPSG:3857")
    if fallback == "EPSG:3857":
        LOG.warning(
            "raster_postprocess: no 'crs' variable in sfincs_map.nc; "
            "falling back to EPSG:3857 — COG CRS tag may not match pixel coords."
        )
    return fallback


def is_quadtree_output(ds: Any) -> bool:
    """Probe whether a SFINCS dataset is a FACE-INDEXED UGRID (quadtree) output.

    Purely structural (dim name OR the face-x variable) so it never imports
    cht_sfincs. Lifted from postprocess_flood._is_quadtree_output.
    """
    try:
        dims = set(getattr(ds, "dims", {}))
        variables = set(getattr(ds, "variables", {}))
    except Exception:  # noqa: BLE001
        return False
    return "nmesh2d_face" in dims or "mesh2d_face_x" in variables


def read_face_coords(ds: Any) -> tuple[Any, Any]:
    """Read per-face centroid coords (UGRID quadtree); GPL-free.

    1. ``mesh2d_face_x``/``_y`` (or ``face_x``/``face_y``) — precomputed centroids.
    2. compute centroids from ``mesh2d_node_x``/``_y`` + ``mesh2d_face_nodes``
       (1-based ``start_index``, fill slots masked) via ``np.nanmean``. Fully
       vectorized — no Python face loop, no cht import.

    Lifted from postprocess_flood._read_face_coords.
    """
    import numpy as np  # type: ignore

    for xk, yk in (("mesh2d_face_x", "mesh2d_face_y"), ("face_x", "face_y")):
        if xk in ds.variables and yk in ds.variables:
            return (
                np.asarray(ds[xk].values, dtype="float64").ravel(),
                np.asarray(ds[yk].values, dtype="float64").ravel(),
            )

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
        if conn.ndim == 1:
            conn = conn[np.newaxis, :]
        idx0 = conn - float(start_index)
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

    raise CogError(
        "RUN_OUTPUT_UNEXPECTED_SHAPE",
        message=(
            "quadtree output carries no face-centroid coordinates "
            "(mesh2d_face_x/_y or face_x/_y) and no node coords + "
            "face-node connectivity to compute them from"
        ),
        details={"variables": list(ds.variables.keys())},
    )


def read_regular_grid_bounds(ds: Any) -> tuple[float, float, float, float] | None:
    """Read the regular-grid (xmin, ymin, xmax, ymax) from the 1D x/y coords.

    Returns ``None`` if the dataset has no usable 1D ``x``/``y`` (the quadtree
    path), so the caller routes through the face rasterizer instead.
    """
    import numpy as np  # type: ignore

    try:
        _x = np.asarray(ds["x"].values)
        _y = np.asarray(ds["y"].values)
        return (
            float(_x.min()),
            float(_y.min()),
            float(_x.max()),
            float(_y.max()),
        )
    except Exception:  # noqa: BLE001
        return None


def orient_array_for_cog(
    arr: Any,
    *,
    x_dim_len: int | None = None,
    y_dim_len: int | None = None,
    y_ascends_along_rows: bool | None = None,
    x_descends_along_cols: bool | None = None,
) -> Any:
    """Apply the rotation + Y-flip + X-flip orientation guards to a 2D array.

    The agent's ``_orient_array_for_cog`` reads the dim names + coord directions
    off the OPEN dataset inline; to keep this picklable for the ProcessPool we
    take the already-probed scalars (computed once in the parent via
    :func:`probe_regular_grid_orientation`). All three guards degrade to identity
    on a None probe. Lifted from postprocess_flood._orient_array_for_cog.
    """
    import numpy as np  # type: ignore

    # Rotation: array is (n_x, n_y) (x-cols in rows) -> transpose to (n_y, n_x).
    if (
        x_dim_len is not None
        and y_dim_len is not None
        and arr.ndim == 2
        and x_dim_len != y_dim_len
        and arr.shape[0] == x_dim_len
        and arr.shape[1] == y_dim_len
    ):
        arr = arr.T

    if y_ascends_along_rows:
        arr = arr[::-1, :]
    if x_descends_along_cols:
        arr = arr[:, ::-1]

    return np.ascontiguousarray(arr)


def probe_regular_grid_orientation(ds: Any) -> dict[str, Any]:
    """Probe the orientation scalars once in the parent (picklable result).

    Returns the kwargs :func:`orient_array_for_cog` consumes. Every probe
    degrades to ``None`` on failure (defensive — a bad read skips the correction
    rather than corrupting the raster).
    """
    out: dict[str, Any] = {
        "x_dim_len": None,
        "y_dim_len": None,
        "y_ascends_along_rows": None,
        "x_descends_along_cols": None,
    }
    try:
        _x_dim = ds["x"].dims[0]
        _y_dim = ds["y"].dims[0]
        out["x_dim_len"] = int(ds.sizes.get(_x_dim, ds["x"].shape[0]))
        out["y_dim_len"] = int(ds.sizes.get(_y_dim, ds["y"].shape[0]))
    except Exception:  # noqa: BLE001
        pass
    try:
        _y_vals = ds["y"].values
        if _y_vals.ndim == 2:
            out["y_ascends_along_rows"] = bool(_y_vals[0, 0] < _y_vals[-1, 0])
        else:
            out["y_ascends_along_rows"] = bool(_y_vals[0] < _y_vals[-1])
    except Exception:  # noqa: BLE001
        pass
    try:
        _x_vals = ds["x"].values
        if _x_vals.ndim == 2:
            out["x_descends_along_cols"] = bool(_x_vals[0, 0] > _x_vals[0, -1])
        else:
            out["x_descends_along_cols"] = bool(_x_vals[0] > _x_vals[-1])
    except Exception:  # noqa: BLE001
        pass
    return out


# --------------------------------------------------------------------------- #
# Face rasterize (scipy griddata) — PURE, picklable.
# --------------------------------------------------------------------------- #


def rasterize_face_field(
    values_1d: Any,
    face_x: Any,
    face_y: Any,
    *,
    crs: str,
    bbox: tuple[float, float, float, float] | None,
    resolution_m: float = 30.0,
) -> tuple[Any, Any]:
    """Grid a per-face scalar UGRID field onto a regular metric raster.

    Nearest-neighbour griddata (preserves per-face value, no smoothing across the
    variable quadtree), a linear-interp convex-hull mask -> NaN outside the mesh,
    output in the FACE (projected/UTM) CRS. Returns ``(arr_2d, transform)``.
    Lifted from postprocess_flood._rasterize_face_field — GPL-free (no cht).
    """
    import numpy as np  # type: ignore
    import rasterio  # type: ignore
    from scipy.interpolate import griddata  # type: ignore

    vals = np.asarray(values_1d, dtype="float64").ravel()
    fx = np.asarray(face_x, dtype="float64").ravel()
    fy = np.asarray(face_y, dtype="float64").ravel()
    if not (vals.shape[0] == fx.shape[0] == fy.shape[0]):
        raise CogError(
            "RUN_OUTPUT_UNEXPECTED_SHAPE",
            message=(
                f"quadtree face field length {vals.shape[0]} != face-coord "
                f"length ({fx.shape[0]}, {fy.shape[0]})"
            ),
            details={"n_values": int(vals.shape[0]), "n_faces": int(fx.shape[0])},
        )

    finite = np.isfinite(fx) & np.isfinite(fy)
    fx, fy, vals = fx[finite], fy[finite], vals[finite]
    if fx.size == 0:
        raise CogError(
            "RUN_OUTPUT_EMPTY",
            message="quadtree output has no finite face centroids",
            details={},
        )

    minx = maxx = miny = maxy = None
    if bbox is not None:
        try:
            from pyproj import Transformer  # type: ignore

            tf = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
            bx0, by0 = tf.transform(float(bbox[0]), float(bbox[1]))
            bx1, by1 = tf.transform(float(bbox[2]), float(bbox[3]))
            minx, maxx = min(bx0, bx1), max(bx0, bx1)
            miny, maxy = min(by0, by1), max(by0, by1)
        except Exception as exc:  # noqa: BLE001
            LOG.warning(
                "raster_postprocess: bbox->%s reproject failed (%s); bounding to "
                "face extent.", crs, exc,
            )
            minx = maxx = miny = maxy = None
    if minx is None:
        minx, maxx = float(fx.min()), float(fx.max())
        miny, maxy = float(fy.min()), float(fy.max())

    res = max(1.0, float(resolution_m))
    width = max(1, int(np.ceil((maxx - minx) / res)))
    height = max(1, int(np.ceil((maxy - miny) / res)))
    if width > _MAX_GRID_DIM or height > _MAX_GRID_DIM:
        scale = max(width / _MAX_GRID_DIM, height / _MAX_GRID_DIM)
        res = res * scale
        width = max(1, int(np.ceil((maxx - minx) / res)))
        height = max(1, int(np.ceil((maxy - miny) / res)))

    xs = minx + (np.arange(width) + 0.5) * res
    ys = maxy - (np.arange(height) + 0.5) * res
    grid_x, grid_y = np.meshgrid(xs, ys)

    arr = griddata((fx, fy), vals, (grid_x, grid_y), method="nearest").astype(
        "float32"
    )
    try:
        hull_mask = griddata(
            (fx, fy), np.ones_like(vals), (grid_x, grid_y), method="linear"
        )
        arr = np.where(np.isfinite(hull_mask), arr, np.nan).astype("float32")
    except Exception:  # noqa: BLE001
        pass

    transform = rasterio.transform.from_bounds(minx, miny, maxx, maxy, width, height)
    return np.ascontiguousarray(arr), transform


def _field_metrics(arr_masked: Any) -> dict[str, Any]:
    """Compute the peak aggregates over a NaN-masked field (agent metric keys)."""
    import numpy as np  # type: ignore

    flooded = arr_masked[~np.isnan(arr_masked)]
    if flooded.size == 0:
        return {
            "max_depth_m": 0.0,
            "mean_depth_m": 0.0,
            "p95_depth_m": 0.0,
            "flooded_cell_count": 0,
        }
    return {
        "max_depth_m": float(np.nanmax(flooded)),
        "mean_depth_m": float(np.nanmean(flooded)),
        "p95_depth_m": float(np.nanpercentile(flooded, 95)),
        "flooded_cell_count": int(flooded.size),
    }


def _maybe_upsample_for_overviews(
    arr: Any, transform: Any
) -> tuple[Any, Any]:
    """Upsample a sub-``_COG_MIN_DIM_PX`` raster so the COG driver builds overviews.

    A tiny quadtree AOI can produce a raster below the COG overview threshold;
    the agent's ``_ensure_raster_has_overviews`` no-op assumption then breaks.
    Nearest-neighbour upsample (preserves the per-cell value / NaN mask) to at
    least ``_COG_MIN_DIM_PX`` on the short side, adjusting the transform pixel
    size to keep the SAME geographic bounds. Mirrors the SWAN ``_upsample_for_cog``
    spirit. Identity when the raster is already large enough.
    """
    import numpy as np  # type: ignore
    import rasterio  # type: ignore

    h, w = arr.shape[-2], arr.shape[-1]
    short = min(h, w)
    if short >= _COG_MIN_DIM_PX:
        return arr, transform
    factor = int(np.ceil(_COG_MIN_DIM_PX / max(short, 1)))
    if factor <= 1:
        return arr, transform
    up = np.kron(arr, np.ones((factor, factor), dtype=arr.dtype))
    # Recompute the transform from the SAME bounds at the new pixel count.
    west = transform.c
    north = transform.f
    new_a = transform.a / factor
    new_e = transform.e / factor
    new_transform = rasterio.Affine(new_a, 0.0, west, 0.0, new_e, north)
    return np.ascontiguousarray(up), new_transform


def finalize_cog(
    arr_masked: Any,
    *,
    crs: str,
    transform: Any,
    out_path: Path,
) -> None:
    """Write the (oriented + masked) 2D array as a CRS-verified COG WITH overviews.

    Differences vs the agent's ``_finalize_cog``:
      * writes overview-bearing COGs (the COG driver builds internal overviews;
        plus a sub-min-dimension upsample guard) so the agent's
        ``_ensure_raster_has_overviews`` becomes a no-op (spike risk #8);
      * writes to a CALLER-CHOSEN ``out_path`` (the deterministic deck key) rather
        than a tempfile, so the worker's ``_expand_outputs`` ``*.tif`` sweep
        uploads it with no new upload code.

    Re-opens the COG to assert the CRS tag round-trips + the geographic/projected
    classification matches the bounds magnitude (the mistagged-raster guard).
    """
    import numpy as np  # type: ignore
    import rasterio  # type: ignore

    arr_masked = np.asarray(arr_masked, dtype="float32")
    arr_masked, transform = _maybe_upsample_for_overviews(arr_masked, transform)

    try:
        with rasterio.open(
            out_path,
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
            overview_resampling="nearest",
        ) as dst:
            dst.write(arr_masked.astype("float32"), 1)
    except Exception as exc:  # noqa: BLE001
        raise CogError(
            "COG_WRITE_FAILED",
            message=f"COG write failed: {exc}",
            details={"out_path": str(out_path)},
        ) from exc

    with rasterio.open(out_path, "r") as verify:
        if str(verify.crs) != str(crs):
            raise CogError(
                "CRS_TAG_MISMATCH",
                message=(
                    f"COG written with crs={crs!r} but rasterio read back "
                    f"{verify.crs!r}"
                ),
                details={"out_path": str(out_path)},
            )
        is_geographic = verify.crs.is_geographic
        bounds_max = max(abs(verify.bounds.left), abs(verify.bounds.right))
        if is_geographic and bounds_max > 360:
            raise CogError(
                "CRS_TAG_MISMATCH",
                message=(
                    f"crs={crs!r} is geographic but bounds.left="
                    f"{verify.bounds.left} implies projected coords (|x|>360)"
                ),
                details={"out_path": str(out_path)},
            )
        if (not is_geographic) and bounds_max < 1000:
            raise CogError(
                "CRS_TAG_MISMATCH",
                message=(
                    f"crs={crs!r} is projected but bounds.left="
                    f"{verify.bounds.left} implies geographic coords (|x|<1000)"
                ),
                details={"out_path": str(out_path)},
            )


def write_field_cog(
    *,
    out_path: Path,
    crs: str,
    nodata_threshold_m: float = NODATA_DEPTH_M,
    # --- quadtree (face-indexed) inputs --- #
    face_values: Any = None,
    face_x: Any = None,
    face_y: Any = None,
    bbox: tuple[float, float, float, float] | None = None,
    resolution_m: float = 30.0,
    # --- regular-grid inputs --- #
    regular_arr: Any = None,
    regular_bounds: tuple[float, float, float, float] | None = None,
    orient_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Encode ONE field (peak or frame) to a COG at ``out_path``; return metrics.

    This is the PICKLABLE per-frame work unit the ProcessPool maps over — it
    takes ONLY plain numpy arrays + floats + the resolved ``crs`` string (NO open
    xarray dataset, NO cht), writes one GDAL dataset, and returns the field
    metrics dict. Two modes:

      * QUADTREE: pass ``face_values`` + ``face_x`` + ``face_y`` (+ optional
        ``bbox`` / ``resolution_m``) — rasterized via :func:`rasterize_face_field`.
      * REGULAR GRID: pass ``regular_arr`` (2D) + ``regular_bounds``
        (xmin,ymin,xmax,ymax) + ``orient_kwargs`` (from
        :func:`probe_regular_grid_orientation`).
    """
    import numpy as np  # type: ignore
    import rasterio  # type: ignore

    if face_values is not None:
        arr, transform = rasterize_face_field(
            face_values, face_x, face_y, crs=crs, bbox=bbox, resolution_m=resolution_m
        )
    elif regular_arr is not None:
        arr = np.asarray(regular_arr, dtype="float32")
        if arr.ndim > 2:
            arr = np.squeeze(arr)
        if arr.ndim != 2:
            raise CogError(
                "RUN_OUTPUT_UNEXPECTED_SHAPE",
                message=f"field array has shape {arr.shape}; expected 2D after squeeze",
                details={"shape": list(arr.shape)},
            )
        arr = orient_array_for_cog(arr, **(orient_kwargs or {}))
        if regular_bounds is not None:
            minx, miny, maxx, maxy = regular_bounds
            transform = rasterio.transform.from_bounds(
                minx, miny, maxx, maxy, arr.shape[-1], arr.shape[-2]
            )
        else:
            transform = rasterio.Affine.identity()
    else:
        raise CogError(
            "RUN_OUTPUT_UNEXPECTED_SHAPE",
            message="write_field_cog needs either face_values or regular_arr",
            details={},
        )

    arr_masked = np.where(arr > nodata_threshold_m, arr, np.nan).astype("float32")
    metrics = _field_metrics(arr_masked)
    finalize_cog(arr_masked, crs=crs, transform=transform, out_path=out_path)
    metrics["crs"] = crs
    metrics["units"] = "meters"
    return metrics


def tmp_cog_path(suffix: str = ".tif") -> Path:
    """A throwaway tmp path (used only by tests / non-deterministic callers)."""
    return Path(tempfile.NamedTemporaryFile(suffix=suffix, delete=False).name)
