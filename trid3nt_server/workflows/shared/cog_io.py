"""Shared Cloud-Optimized-GeoTIFF write / reproject / CRS-guard / upload helpers.

Every per-caller nuance is a DECLARED PARAMETER, never flattened; a failure raises
a staged :class:`CogIoError` for the caller to map onto its own error codes.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("trid3nt_server.workflows.shared.cog_io")

__all__ = [
    "CogIoError",
    "CogStage",
    "safe_unlink",
    "cog_bbox_4326",
    "write_cog_4326_from_grid",
    "reproject_cog_file_to_4326",
    "upload_cog",
    "RUNS_BUCKET_DEFAULT",
    "NODATA_DEPTH_M",
    "_read_crs_from_dataset",
]


# Normalized stage tokens the engine shims map onto their typed error codes.
CogStage = str  # one of: "DEPENDENCY", "WRITE", "REPROJECT", "CRS_MISMATCH", "UPLOAD"


#: Default runs bucket -- the local MinIO runs bucket (env override:
#: TRID3NT_RUNS_BUCKET). Cross-engine seam: every on-box postprocess uploads its
#: display COGs under ``s3://<RUNS_BUCKET_DEFAULT>/<run_id>/``.
RUNS_BUCKET_DEFAULT: str = "trid3nt-runs"

#: Minimum depth threshold below which cells are masked to NaN (treated as dry).
#: 5 cm is the physically meaningful wet-cell threshold -- matches the
#: ``flooded_cell_count`` reporting convention. Shared by every depth COG writer
#: (SFINCS / GeoClaw / SWMM).
NODATA_DEPTH_M: float = 0.05


def _read_crs_from_dataset(ds: Any) -> str:
    """Read the CRS off a netCDF dataset that carries it in a ``crs`` DATA VARIABLE
    rather than in ``ds.attrs``. Falls back to ``ds.attrs`` and finally EPSG:3857,
    logging a warning whenever it does, so a mismatch is never silent."""
    # The known encodings, tried in this order: the ``epsg_code`` attr (already
    # "EPSG:nnnnn"); a bare numeric ``epsg`` / ``EPSG`` attr; the CF canonical
    # ``crs_wkt``; the ``spatial_ref`` / ``wkt`` OGC variants some GDAL writers
    # use; and the crs VARIABLE VALUE itself, which a quadtree writer stores as
    # the bare int EPSG code with a placeholder attr.
    if "crs" in ds.variables:
        crs_var = ds["crs"]
        attrs = crs_var.attrs

        if "epsg_code" in attrs:
            # SFINCS emits e.g. "EPSG:32617" -- may occasionally be bare int.
            raw = str(attrs["epsg_code"]).strip()
            if raw.upper().startswith("EPSG:"):
                return raw  # already canonical
            try:
                return f"EPSG:{int(raw)}"
            except ValueError:
                pass  # fall through to next key

        for epsg_key in ("epsg", "EPSG"):
            if epsg_key in attrs:
                # cht_sfincs writes attrs={'EPSG':'-'} (a placeholder) -- int()
                # raises and we fall through to the variable value below.
                try:
                    return f"EPSG:{int(str(attrs[epsg_key]).strip())}"
                except (ValueError, TypeError):
                    pass  # placeholder / non-numeric -- fall through

        for wkt_key in ("crs_wkt", "spatial_ref", "wkt"):
            if wkt_key in attrs:
                try:
                    import pyproj  # optional; rasterio ships pyproj
                    return pyproj.CRS.from_wkt(attrs[wkt_key]).to_string()
                except Exception:  # noqa: BLE001
                    pass  # malformed WKT -- fall through

        # cht_sfincs quadtree: the crs VARIABLE VALUE is the bare int EPSG code
        # (e.g. 32616), not an attr. Read it as a scalar and validate via pyproj.
        try:
            import numpy as np  # type: ignore[import-not-found]

            raw_val = np.asarray(crs_var.values).ravel()
            if raw_val.size >= 1 and np.isfinite(raw_val[0]):
                epsg_int = int(raw_val[0])
                if epsg_int > 0:
                    try:
                        import pyproj  # validate it is a real authority code

                        return pyproj.CRS.from_epsg(epsg_int).to_string()
                    except Exception:  # noqa: BLE001
                        return f"EPSG:{epsg_int}"
        except Exception:  # noqa: BLE001
            pass  # non-numeric variable value -- fall through to attrs fallback

    # Fallback: old .attrs encoding or bare dataset without a crs variable.
    fallback = ds.attrs.get("crs", "EPSG:3857")
    if fallback == "EPSG:3857":
        logger.warning(
            "cog_io: no 'crs' variable found in the netCDF dataset; falling back "
            "to EPSG:3857 — COG CRS tag may not match pixel coords."
        )
    return fallback


class CogIoError(RuntimeError):
    """A staged COG-IO failure the caller re-raises as its own typed error.
    ``stage`` is one of the normalized :data:`CogStage` tokens, which the caller
    maps onto its own error code."""

    def __init__(
        self,
        stage: CogStage,
        *,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


def safe_unlink(p: Path) -> None:
    """Best-effort ``unlink(missing_ok=True)`` (never raises). The shared
    ``_safe_unlink`` every engine duplicated."""
    try:
        p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def cog_bbox_4326(cog_path: Path) -> tuple[float, float, float, float] | None:
    """Return the COG's ``(min_lon, min_lat, max_lon, max_lat)`` for zoom-to.
    Degrades to ``None`` on any read failure and never raises: a missing zoom-to
    bbox is not fatal."""
    try:
        import rasterio  # type: ignore[import-not-found]

        with rasterio.open(cog_path) as ds:
            b = ds.bounds
            return (float(b.left), float(b.bottom), float(b.right), float(b.top))
    except Exception:  # noqa: BLE001
        return None


def _run_crs_roundtrip_guard(
    cog_path: Path,
    *,
    dst_crs: str,
) -> tuple[float, float, float, float]:
    """Re-open the written COG and assert the CRS tag round-trips, returning its
    bounds. Raises :class:`CogIoError` with ``stage="CRS_MISMATCH"``."""
    # The tag must read back EXACTLY ``dst_crs``, and for a geographic CRS the
    # bounds magnitude must be <= 360: a larger one means the tag is wrong and the
    # pixels are really projected metres.
    import rasterio  # type: ignore[import-not-found]

    with rasterio.open(cog_path, "r") as verify:
        if str(verify.crs) != dst_crs:
            raise CogIoError(
                "CRS_MISMATCH",
                message=(
                    f"COG written with crs={dst_crs!r} but rasterio read back "
                    f"{verify.crs!r}"
                ),
            )
        bounds_max = max(abs(verify.bounds.left), abs(verify.bounds.right))
        if bounds_max > 360:
            raise CogIoError(
                "CRS_MISMATCH",
                message=(
                    f"COG tagged {dst_crs} (geographic) but bounds.left="
                    f"{verify.bounds.left} implies projected coords (|x|>360)"
                ),
            )
        b = verify.bounds
        return (float(b.left), float(b.bottom), float(b.right), float(b.top))


DST_CRS = "EPSG:4326"


def _named_tmp(suffix: str) -> str:
    """A non-deleting NamedTemporaryFile name (the engines all used this idiom)."""
    import tempfile

    return tempfile.NamedTemporaryFile(suffix=suffix, delete=False).name


def _write_4326_cog(
    arr: Any,
    *,
    src_crs: Any,
    src_transform: Any,
    reproject: bool,
    resampling: Any,
    src_nodata: float,
    dst_suffix: str,
) -> Path:
    """Write a float32 2D array to an EPSG:4326 COG, warping when asked.
    NaN is the destination no-data whatever the source tags."""
    # The destination grid is ``calculate_default_transform``'s and is handed to
    # rioxarray explicitly: rioxarray would otherwise re-derive the source affine
    # from its coordinate arrays, perturbing the pixel size in the last float bit
    # and changing the written bytes.
    import numpy as np  # type: ignore[import-not-found]
    import rasterio  # type: ignore[import-not-found]
    import rioxarray  # type: ignore[import-not-found]  # noqa: F401  (.rio accessor)
    import xarray as xr  # type: ignore[import-not-found]

    da = (
        xr.DataArray(np.asarray(arr, dtype="float32"), dims=("y", "x"))
        .rio.write_crs(src_crs)
        .rio.write_transform(src_transform)
        .rio.write_nodata(src_nodata)
    )
    if reproject:
        from rasterio.warp import (  # type: ignore[import-not-found]
            calculate_default_transform,
        )

        height, width = da.shape
        transform, out_w, out_h = calculate_default_transform(
            src_crs,
            DST_CRS,
            width,
            height,
            *rasterio.transform.array_bounds(height, width, src_transform),
        )
        da = da.rio.reproject(
            DST_CRS,
            transform=transform,
            shape=(out_h, out_w),
            resampling=resampling,
            nodata=float("nan"),
        )
    dst_cog = Path(_named_tmp(dst_suffix))
    da.rio.write_nodata(float("nan")).rio.to_raster(
        dst_cog, driver="COG", dtype="float32", compress="LZW"
    )
    return dst_cog


def write_cog_4326_from_grid(
    grid: Any,
    *,
    src_crs: str,
    src_transform: Any,
    reproject: bool,
    resampling: Any | None = None,
    mask: Callable[[Any], Any] | None = None,
    crs_roundtrip_guard: bool = False,
    dst_suffix: str = "_4326.tif",
) -> Path:
    """Write a 2D ``grid`` to an EPSG:4326 COG. ``reproject=False`` requires the grid
    to be ALREADY in 4326 and does NO warp, ``reproject=True`` warps from ``src_crs``
    using ``resampling``, and ``mask`` runs before write. Raises :class:`CogIoError`."""
    try:
        import numpy as np  # type: ignore[import-not-found]
        from rasterio.warp import Resampling  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise CogIoError(
            "DEPENDENCY", message=f"numpy/rasterio unavailable: {exc}"
        ) from exc

    arr = np.asarray(grid, dtype="float32")
    if mask is not None:
        arr = np.asarray(mask(arr), dtype="float32")

    try:
        dst_cog = _write_4326_cog(
            arr,
            src_crs=src_crs,
            src_transform=src_transform,
            reproject=reproject,
            resampling=Resampling.nearest if resampling is None else resampling,
            src_nodata=float("nan"),
            dst_suffix=dst_suffix,
        )
    except Exception as exc:  # noqa: BLE001
        if reproject:
            raise CogIoError(
                "REPROJECT",
                message=f"projected -> EPSG:4326 reprojection failed: {exc}",
                details={"src_crs": src_crs},
            ) from exc
        raise CogIoError("WRITE", message=f"COG write failed: {exc}") from exc

    if crs_roundtrip_guard:
        try:
            _run_crs_roundtrip_guard(dst_cog, dst_crs=DST_CRS)
        except CogIoError:
            safe_unlink(dst_cog)
            raise
    return dst_cog


def reproject_cog_file_to_4326(
    src_cog: Path,
    *,
    resampling: Any | None = None,
    crs_roundtrip_guard: bool = True,
    dst_suffix: str = "_4326.tif",
) -> tuple[Path, tuple[float, float, float, float] | None]:
    """Reproject an existing single-band metric-CRS COG FILE to EPSG:4326.
    Default resampling is ``nearest``, preserving NaN no-data without smearing; raises
    :class:`CogIoError` and returns ``(dst_cog_path, bbox_4326)``."""
    try:
        import rasterio  # type: ignore[import-not-found]
        from rasterio.warp import Resampling  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise CogIoError(
            "DEPENDENCY", message=f"rasterio unavailable for COG reproject: {exc}"
        ) from exc

    if not src_cog.exists():
        raise CogIoError(
            "READ",
            message=f"field COG not found at {src_cog}",
            details={"src_cog": str(src_cog)},
        )

    with rasterio.open(src_cog) as src:
        if src.crs is None:
            raise CogIoError(
                "READ",
                message=f"field COG {src_cog} carries no CRS tag",
                details={"src_cog": str(src_cog)},
            )
        band, src_crs, src_transform = src.read(1), src.crs, src.transform
        src_nodata = float("nan") if src.nodata is None else float(src.nodata)

    try:
        dst_cog = _write_4326_cog(
            band,
            src_crs=src_crs,
            src_transform=src_transform,
            reproject=True,
            resampling=Resampling.nearest if resampling is None else resampling,
            src_nodata=src_nodata,
            dst_suffix=dst_suffix,
        )
    except Exception as exc:  # noqa: BLE001
        raise CogIoError(
            "REPROJECT",
            message=f"projected-metres -> EPSG:4326 reprojection failed: {exc}",
            details={"src_cog": str(src_cog)},
        ) from exc

    bbox: tuple[float, float, float, float] | None
    if crs_roundtrip_guard:
        try:
            bbox = _run_crs_roundtrip_guard(dst_cog, dst_crs=DST_CRS)
        except CogIoError:
            safe_unlink(dst_cog)
            raise
    else:
        bbox = cog_bbox_4326(dst_cog)
    return dst_cog, bbox


def upload_cog(
    local_cog: Path,
    run_id: str,
    runs_bucket: str | None,
    *,
    dest_filename: str,
    content_type: str | None = "image/tiff",
    gs_backend: str = "fsspec",
    gs_fallback_to_file: bool = False,
    runs_bucket_default: str | None = None,
    log_label: str = "COG",
) -> str:
    """Upload a COG to ``{scheme}://<runs_bucket>/<run_id>/<dest_filename>``.
    On ``s3`` the bucket MUST come from ``TRID3NT_RUNS_BUCKET`` or ``runs_bucket``;
    every failure raises :class:`CogIoError` with ``stage="UPLOAD"``."""
    from trid3nt_server.tools.cache import storage_scheme

    scheme = storage_scheme()
    if scheme == "s3":
        bucket = runs_bucket or (os.environ.get("TRID3NT_RUNS_BUCKET") or "").strip()
        if not bucket:
            raise CogIoError(
                "UPLOAD",
                message=(
                    "TRID3NT_RUNS_BUCKET must be set under "
                    "TRID3NT_STORAGE_BACKEND=s3 (no GCP-named default on AWS)"
                ),
                details={"local_cog": str(local_cog)},
            )
        dest = f"s3://{bucket}/{run_id}/{dest_filename}"
        try:
            from trid3nt_server.workflows.solver.solver import _get_s3_client

            kwargs: dict[str, Any] = {
                "Bucket": bucket,
                "Key": f"{run_id}/{dest_filename}",
            }
            if content_type is not None:
                kwargs["ContentType"] = content_type
            # ``content_type=None`` omits the ContentType header entirely.
            with local_cog.open("rb") as fh:
                kwargs["Body"] = fh
                _get_s3_client().put_object(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise CogIoError(
                "UPLOAD",
                message=f"upload of {local_cog} to {dest} failed: {exc}",
                details={"local_cog": str(local_cog), "dest": dest},
            ) from exc
        logger.info("uploaded %s to %s (boto3)", log_label, dest)
        return dest

    # --- non-s3 scheme: there is no non-s3 backend ------------------------- #
    # ``storage_scheme()`` resolves to "s3" in production, so a non-s3 scheme is
    # only reachable when a caller forces one. No client is ever constructed:
    # ``gs_fallback_to_file`` degrades to a ``file://`` URI, otherwise this raises
    # the typed ``CogIoError`` naming the absent backend. ``gs_backend`` is
    # accepted for signature compatibility and selects nothing.
    if gs_fallback_to_file:
        return f"file://{local_cog}"
    raise CogIoError(
        "UPLOAD",
        message=(
            f"cloud upload for scheme={scheme!r} is not available on the "
            "local build (the gs:// backend was removed with the GCP "
            "decommission); set gs_fallback_to_file=True for a local file:// "
            "URI or TRID3NT_STORAGE_BACKEND=s3 for a real upload"
        ),
        details={"local_cog": str(local_cog), "scheme": scheme},
    )
