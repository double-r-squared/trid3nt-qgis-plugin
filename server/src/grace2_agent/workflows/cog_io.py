"""Shared Cloud-Optimized-GeoTIFF write / reproject / CRS-guard / upload helpers.

STEP 1 of the engine-coverage-levers refactor (pure dedupe, NO behavior change).
Five on-box postprocess modules (``postprocess_swmm`` / ``_modflow`` / ``_geoclaw``
/ ``_landlab`` / ``_openquake``) each hand-rolled a near-identical
``_write_*_cog_4326`` / ``_reproject_field_cog_4326`` / ``_upload_cog*`` /
``_cog_bbox_4326`` family. This module is the single implementation; each engine
now calls it through a thin shim and produces BYTE-IDENTICAL output.

CRITICAL design rule (kickoff): every per-engine nuance is a DECLARED PARAMETER,
never flattened. The nuances preserved here, with the engine that needs each:

  - ``mask``: the per-cell mask applied before write. The plume + OpenQuake mask
    cells AT/BELOW a positive floor to NaN (render only the hazard); the MODFLOW
    RIV seepage layer writes AS-IS so the NEGATIVE (gaining) reach values survive
    (a positive-floor mask would wrongly drop every gaining cell); SWMM/GeoClaw
    pass an already-masked grid through. Declared via the ``mask`` callable
    (default: identity / no mask).
  - ``resampling``: warp resampling. SWMM/Landlab use ``nearest`` (preserve the
    NaN dry-mask without smearing); MODFLOW plume uses ``bilinear`` (a smooth
    concentration field). Declared via ``resampling``.
  - ``crs_roundtrip_guard``: the TiTiler-wedge / mistagged-raster guard
    (re-open + assert the CRS tag round-trips + the geographic/projected
    magnitude check). SWMM/GeoClaw/Landlab run it; MODFLOW/OpenQuake historically
    did NOT (they relied on the upstream tag). Declared via ``crs_roundtrip_guard``
    (and ``guard_projected_check`` for the projected-CRS magnitude leg, which only
    SFINCS' on-NetCDF path uses; the 4326 writers only need the geographic leg).
  - ``content_type``: the S3 ``ContentType`` header. SWMM/GeoClaw/Landlab set
    ``image/tiff``; OpenQuake's ``put_object`` set NONE (byte-identical: omit it).
    Declared via ``content_type`` (None -> header omitted).
  - ``gs_backend`` + ``gs_fallback_to_file``: the gs:// branch. SWMM/GeoClaw/
    Landlab use fsspec and RAISE on failure; MODFLOW uses fsspec but FALLS BACK
    to a ``file://`` URI (with a loud ImportError classification); OpenQuake uses
    the ``google.cloud.storage`` client and falls back to ``file://``. Declared
    via ``gs_backend`` ("fsspec" | "gcs_client") + ``gs_fallback_to_file``.
  - ``error_map``: every engine raises its OWN typed error subclass with its OWN
    ``error_code`` per stage. cog_io raises a generic :class:`CogIoError` carrying
    a normalized ``stage`` token; the engine shim catches it and re-raises its
    typed error via the ``error_map`` it passes (stage -> (error_code, message)).
    This is how the byte-identical typed-error contract is preserved without
    flattening five error enums into one.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("grace2_agent.workflows.cog_io")

__all__ = [
    "CogIoError",
    "CogStage",
    "safe_unlink",
    "cog_bbox_4326",
    "write_cog_4326_from_grid",
    "reproject_cog_file_to_4326",
    "upload_cog",
]


# Normalized stage tokens the engine shims map onto their typed error codes.
CogStage = str  # one of: "DEPENDENCY", "WRITE", "REPROJECT", "CRS_MISMATCH", "UPLOAD"


class CogIoError(RuntimeError):
    """A staged COG-IO failure the engine shim re-raises as its typed error.

    ``stage`` is one of the normalized :data:`CogStage` tokens; the shim looks it
    up in its ``error_map`` to recover the engine-specific ``error_code``.
    """

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

    The byte-identical ``_cog_bbox_4326`` shared by SWMM / MODFLOW / OpenQuake
    (and the inline bbox read in Landlab's guard). Degrades to ``None`` on any
    read failure (never raises - a missing zoom-to bbox is not fatal).
    """
    try:
        import rasterio  # type: ignore[import-not-found]

        with rasterio.open(cog_path) as ds:
            b = ds.bounds
            return (float(b.left), float(b.bottom), float(b.right), float(b.top))
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# CRS round-trip guard (the TiTiler-wedge / mistagged-raster guard).
# --------------------------------------------------------------------------- #
def _run_crs_roundtrip_guard(
    cog_path: Path,
    *,
    dst_crs: str,
) -> tuple[float, float, float, float]:
    """Re-open the written COG and assert the CRS tag round-trips.

    The shared guard SWMM/GeoClaw/Landlab run AFTER writing a 4326 COG: the CRS
    tag must read back EXACTLY ``dst_crs``, and (EPSG:4326 being geographic) the
    bounds magnitude must be <= 360 (a |x|>360 implies the tag is wrong and the
    pixels are really projected metres - the classic mistagged-raster bug).
    Raises :class:`CogIoError` with ``stage="CRS_MISMATCH"``. Returns the COG
    bounds tuple (Landlab uses it as the zoom-to bbox).
    """
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


# --------------------------------------------------------------------------- #
# Grid -> EPSG:4326 COG (covers SWMM / MODFLOW / GeoClaw / OpenQuake).
# --------------------------------------------------------------------------- #
def write_cog_4326_from_grid(
    grid: Any,
    *,
    src_crs: str,
    src_transform: Any,
    reproject: bool,
    resampling: Any | None = None,
    mask: Callable[[Any], Any] | None = None,
    crs_roundtrip_guard: bool = False,
    src_suffix: str = "_src.tif",
    dst_suffix: str = "_4326.tif",
) -> Path:
    """Write a 2D ``grid`` to an EPSG:4326 COG, optionally reprojecting.

    Two code paths, selected by ``reproject``:

    - ``reproject=False`` (GeoClaw / OpenQuake): the grid is ALREADY in EPSG:4326
      (``src_crs`` must be ``"EPSG:4326"`` and ``src_transform`` the ``from_bounds``
      affine). The COG is written directly with the 4326 profile - NO warp.
    - ``reproject=True`` (SWMM / MODFLOW): the grid is in a projected CRS
      (``src_crs`` + ``src_transform``). A source GTiff is staged in ``src_crs``,
      then warped to EPSG:4326 via ``calculate_default_transform`` + ``reproject``
      using ``resampling`` (caller declares ``nearest`` vs ``bilinear``).

    ``mask`` (declared per engine) is applied to the float32 array before write
    (e.g. mask-below-floor for the plume / OpenQuake; identity for the seepage /
    already-masked SWMM/GeoClaw grids). ``crs_roundtrip_guard`` runs the
    TiTiler-wedge guard after the write (SWMM/GeoClaw on; MODFLOW/OpenQuake off,
    byte-identical to their pre-refactor behavior).

    Raises :class:`CogIoError` (stage ``DEPENDENCY`` / ``WRITE`` / ``REPROJECT`` /
    ``CRS_MISMATCH``). Returns the staged COG path.
    """
    try:
        import numpy as np  # type: ignore[import-not-found]
        import rasterio  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise CogIoError(
            "DEPENDENCY", message=f"numpy/rasterio unavailable: {exc}"
        ) from exc

    arr = np.asarray(grid, dtype="float32")
    if mask is not None:
        arr = np.asarray(mask(arr), dtype="float32")
    height, width = arr.shape

    dst_crs = "EPSG:4326"

    # --- already-4326 direct-write path (no warp) -------------------------- #
    if not reproject:
        dst_cog = Path(_named_tmp(dst_suffix))
        try:
            profile = {
                "driver": "COG",
                "crs": dst_crs,
                "transform": src_transform,
                "width": width,
                "height": height,
                "count": 1,
                "dtype": "float32",
                "nodata": float("nan"),
                "compress": "LZW",
            }
            with rasterio.open(dst_cog, "w", **profile) as dst:
                dst.write(arr, 1)
        except Exception as exc:  # noqa: BLE001
            safe_unlink(dst_cog)
            raise CogIoError(
                "WRITE", message=f"COG write failed: {exc}"
            ) from exc
        if crs_roundtrip_guard:
            try:
                _run_crs_roundtrip_guard(dst_cog, dst_crs=dst_crs)
            except CogIoError:
                safe_unlink(dst_cog)
                raise
        return dst_cog

    # --- projected -> 4326 warp path --------------------------------------- #
    from rasterio.warp import (  # type: ignore[import-not-found]
        Resampling,
        calculate_default_transform,
    )
    from rasterio.warp import reproject as _warp_reproject

    if resampling is None:
        resampling = Resampling.nearest

    src_tmp = Path(_named_tmp(src_suffix))
    try:
        with rasterio.open(
            src_tmp,
            "w",
            driver="GTiff",
            width=width,
            height=height,
            count=1,
            dtype="float32",
            crs=src_crs,
            transform=src_transform,
            nodata=float("nan"),
        ) as dst:
            dst.write(arr, 1)
    except Exception as exc:  # noqa: BLE001
        safe_unlink(src_tmp)
        raise CogIoError(
            "WRITE",
            message=f"source COG write failed: {exc}",
            details={"src_crs": src_crs},
        ) from exc

    dst_cog = Path(_named_tmp(dst_suffix))
    try:
        with rasterio.open(src_tmp) as src:
            transform, out_w, out_h = calculate_default_transform(
                src.crs, dst_crs, src.width, src.height, *src.bounds
            )
            profile = {
                "driver": "COG",
                "crs": dst_crs,
                "transform": transform,
                "width": out_w,
                "height": out_h,
                "count": 1,
                "dtype": "float32",
                "nodata": float("nan"),
                "compress": "LZW",
            }
            with rasterio.open(dst_cog, "w", **profile) as dst:
                _warp_reproject(
                    source=rasterio.band(src, 1),
                    destination=rasterio.band(dst, 1),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=dst_crs,
                    resampling=resampling,
                )
    except Exception as exc:  # noqa: BLE001
        safe_unlink(dst_cog)
        raise CogIoError(
            "REPROJECT",
            message=f"projected -> EPSG:4326 reprojection failed: {exc}",
            details={"src_crs": src_crs},
        ) from exc
    finally:
        safe_unlink(src_tmp)

    if crs_roundtrip_guard:
        try:
            _run_crs_roundtrip_guard(dst_cog, dst_crs=dst_crs)
        except CogIoError:
            safe_unlink(dst_cog)
            raise
    return dst_cog


# --------------------------------------------------------------------------- #
# Existing-COG-file -> EPSG:4326 COG (Landlab worker field).
# --------------------------------------------------------------------------- #
def reproject_cog_file_to_4326(
    src_cog: Path,
    *,
    resampling: Any | None = None,
    crs_roundtrip_guard: bool = True,
    dst_suffix: str = "_4326.tif",
) -> tuple[Path, tuple[float, float, float, float] | None]:
    """Reproject a metric-CRS COG FILE to EPSG:4326 (the Landlab worker-field path).

    Unlike :func:`write_cog_4326_from_grid`, the SOURCE is an existing single-band
    COG on disk (the Batch worker's field output), not an in-memory array. Warps
    to EPSG:4326 via ``calculate_default_transform`` + ``reproject`` (default
    ``Resampling.nearest`` - preserve the NaN no-data without smearing). When
    ``crs_roundtrip_guard`` is set (the default) the TiTiler-wedge guard runs and
    its bounds become the returned zoom-to bbox; otherwise the bbox is read via
    :func:`cog_bbox_4326`.

    Raises :class:`CogIoError` (stage ``DEPENDENCY`` / ``READ`` / ``REPROJECT`` /
    ``CRS_MISMATCH``). Returns ``(dst_cog_path, bbox_4326)``.
    """
    try:
        import rasterio  # type: ignore[import-not-found]
        from rasterio.warp import (  # type: ignore[import-not-found]
            Resampling,
            calculate_default_transform,
        )
        from rasterio.warp import reproject as _warp_reproject
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

    if resampling is None:
        resampling = Resampling.nearest

    dst_cog = Path(_named_tmp(dst_suffix))
    dst_crs = "EPSG:4326"
    try:
        with rasterio.open(src_cog) as src:
            if src.crs is None:
                raise CogIoError(
                    "READ",
                    message=f"field COG {src_cog} carries no CRS tag",
                    details={"src_cog": str(src_cog)},
                )
            transform, width, height = calculate_default_transform(
                src.crs, dst_crs, src.width, src.height, *src.bounds
            )
            profile = {
                "driver": "COG",
                "crs": dst_crs,
                "transform": transform,
                "width": width,
                "height": height,
                "count": 1,
                "dtype": "float32",
                "nodata": float("nan"),
                "compress": "LZW",
            }
            with rasterio.open(dst_cog, "w", **profile) as dst:
                _warp_reproject(
                    source=rasterio.band(src, 1),
                    destination=rasterio.band(dst, 1),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=dst_crs,
                    resampling=resampling,
                )
    except CogIoError:
        safe_unlink(dst_cog)
        raise
    except Exception as exc:  # noqa: BLE001
        safe_unlink(dst_cog)
        raise CogIoError(
            "REPROJECT",
            message=f"projected-metres -> EPSG:4326 reprojection failed: {exc}",
            details={"src_cog": str(src_cog)},
        ) from exc

    bbox: tuple[float, float, float, float] | None
    if crs_roundtrip_guard:
        try:
            bbox = _run_crs_roundtrip_guard(dst_cog, dst_crs=dst_crs)
        except CogIoError:
            safe_unlink(dst_cog)
            raise
    else:
        bbox = cog_bbox_4326(dst_cog)
    return dst_cog, bbox


def _named_tmp(suffix: str) -> str:
    """A non-deleting NamedTemporaryFile name (the engines all used this idiom)."""
    import tempfile

    return tempfile.NamedTemporaryFile(suffix=suffix, delete=False).name


# --------------------------------------------------------------------------- #
# Scheme-aware upload (covers SWMM / MODFLOW / GeoClaw / Landlab / OpenQuake).
# --------------------------------------------------------------------------- #
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

    Scheme-aware via ``cache.storage_scheme()`` (the job-0291/0292b lesson):

    - ``s3``: upload via boto3 through the solver module's shared S3 client. The
      runs bucket MUST come from ``GRACE2_RUNS_BUCKET`` / the explicit
      ``runs_bucket`` arg (no GCP-named default on AWS) - a missing bucket raises
      ``stage="UPLOAD"``. ``content_type`` is passed as the S3 ``ContentType``
      header (OpenQuake omitted it - pass ``None`` for byte-identical behavior).
    - ``gs``: ``gs_backend`` selects the writer. ``"fsspec"`` uses
      ``fsspec.filesystem("gcs").put`` (SWMM/GeoClaw/Landlab/MODFLOW);
      ``"gcs_client"`` uses ``google.cloud.storage`` (OpenQuake). When
      ``gs_fallback_to_file`` is set, a gs failure degrades to a ``file://`` URI
      (MODFLOW/OpenQuake offline-dev path; MODFLOW additionally classifies a
      missing ``fsspec[gcs]`` loudly); otherwise it RAISES ``stage="UPLOAD"``
      (SWMM/GeoClaw/Landlab - no silent file:// on the cloud path).

    Raises :class:`CogIoError` (stage ``UPLOAD``). Returns the object URI.
    """
    from ..tools.cache import storage_scheme

    scheme = storage_scheme()
    if scheme == "s3":
        bucket = runs_bucket or (os.environ.get("GRACE2_RUNS_BUCKET") or "").strip()
        if not bucket:
            raise CogIoError(
                "UPLOAD",
                message=(
                    "GRACE2_RUNS_BUCKET must be set under "
                    "GRACE2_STORAGE_BACKEND=s3 (no GCP-named default on AWS)"
                ),
                details={"local_cog": str(local_cog)},
            )
        dest = f"s3://{bucket}/{run_id}/{dest_filename}"
        try:
            from ..tools.solver import _get_s3_client

            kwargs: dict[str, Any] = {
                "Bucket": bucket,
                "Key": f"{run_id}/{dest_filename}",
            }
            if content_type is not None:
                kwargs["ContentType"] = content_type
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

    # --- gs / local-dev path ---------------------------------------------- #
    bucket = runs_bucket or os.environ.get(
        "GRACE2_RUNS_BUCKET", runs_bucket_default or ""
    )
    bucket = (bucket or "").strip()
    if not bucket and gs_fallback_to_file:
        return f"file://{local_cog}"
    dest = f"gs://{bucket}/{run_id}/{dest_filename}"

    if gs_backend == "gcs_client":
        try:
            from google.cloud import storage  # type: ignore[import-not-found]

            client = storage.Client()
            b, _, k = dest[len("gs://"):].partition("/")
            client.bucket(b).blob(k).upload_from_filename(str(local_cog))
            return dest
        except Exception as exc:  # noqa: BLE001
            if gs_fallback_to_file:
                return f"file://{local_cog}"
            raise CogIoError(
                "UPLOAD",
                message=f"upload of {local_cog} to {dest} failed: {exc}",
                details={"local_cog": str(local_cog), "dest": dest},
            ) from exc

    # gs_backend == "fsspec"
    try:
        import fsspec  # type: ignore[import-not-found]

        fs = fsspec.filesystem("gcs")
        fs.put(str(local_cog), dest)
    except ImportError as exc:
        if gs_fallback_to_file:
            # job-0241: a missing fsspec[gcs] is a DEPLOY/ENV DEFECT (declared
            # dependency), not a transient GCS error - classify it loudly so the
            # next stale-venv regression is one log line, not a basemap-only map.
            logger.error(
                "%s upload to %s SKIPPED - fsspec[gcs] not importable (%s). "
                "This is a deploy/env defect: fsspec is a declared dependency. "
                "The layer will fall back to file:// and will NOT render. "
                "Fix: pip install -e . in services/agent (installs fsspec[gcs]).",
                log_label,
                dest,
                exc,
            )
            return f"file://{local_cog}"
        raise CogIoError(
            "UPLOAD",
            message=f"upload of {local_cog} to {dest} failed: {exc}",
            details={"local_cog": str(local_cog), "dest": dest},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        if gs_fallback_to_file:
            logger.warning(
                "%s upload to %s failed (%s); using local file:// URI",
                log_label,
                dest,
                exc,
            )
            return f"file://{local_cog}"
        raise CogIoError(
            "UPLOAD",
            message=f"upload of {local_cog} to {dest} failed: {exc}",
            details={"local_cog": str(local_cog), "dest": dest},
        ) from exc
    logger.info("uploaded %s to %s", log_label, dest)
    return dest
