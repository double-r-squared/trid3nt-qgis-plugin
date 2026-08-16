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
  - ``gs_fallback_to_file``: the non-s3-scheme branch. GCP is decommissioned
    (no gs:// backend exists); SWMM/GeoClaw/Landlab RAISE ``stage="UPLOAD"``
    on that path, while MODFLOW/OpenQuake set ``gs_fallback_to_file=True``
    and degrade straight to a ``file://`` URI. ``gs_backend`` is kept on the
    signature for caller compatibility but no longer selects a writer.
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
    "FLOOD_DEPTH_STYLE_PRESET",
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
#: ``flooded_cell_count`` reporting convention and the lowest QML colour stop
#: (``continuous_flood_depth.qml`` alpha=0 at 0.05 m). Shared by every depth COG
#: writer (SFINCS / GeoClaw / SWMM).
NODATA_DEPTH_M: float = 0.05

#: QML style preset name attached to a postprocessed flood-depth COG. Shared by
#: the depth-producing engines (SFINCS / SWMM) so the web + QGIS render depth
#: identically.
FLOOD_DEPTH_STYLE_PRESET: str = "continuous_flood_depth"


def _read_crs_from_dataset(ds: Any) -> str:
    """Read CRS from a SFINCS-family netCDF dataset; CF-convention compliant.

    SFINCS (and the sibling engines that emit SFINCS-shaped ``sfincs_map.nc``)
    store the CRS in a **data variable** named ``crs``, not in ``ds.attrs``. The
    variable carries EPSG information either in its attributes (CF conventions)
    OR -- for the cht_sfincs quadtree writer -- as the variable's SCALAR VALUE
    (the bare int EPSG code, e.g. ``32616``, with a useless ``attrs={'EPSG':'-'}``).
    We try the known encodings in order:

    1. ``crs_var.attrs["epsg_code"]`` -- SFINCS emits ``"EPSG:32617"`` (string
       already prefixed); strip any accidental whitespace and return as-is.
    2. ``crs_var.attrs["epsg"]`` / ``["EPSG"]`` -- a bare int EPSG attr (when it
       is a usable number, not the cht placeholder ``"-"``).
    3. ``crs_var.attrs["crs_wkt"]`` -- CF canonical WKT string; parse via
       pyproj and return the EPSG authority string.
    4. ``crs_var.attrs["spatial_ref"]`` / ``["wkt"]`` -- OGC WKT variants used by
       some GDAL writers; parse via pyproj.
    5. The crs VARIABLE VALUE itself -- the cht_sfincs quadtree writer stores the
       bare int EPSG code (32616) AS the variable value, not in an attr; read it
       as ``int(crs_var.values)`` -> ``"EPSG:32616"``.
    6. Fallback: ``ds.attrs.get("crs", "EPSG:3857")`` -- retained for any dataset
       that does not carry the ``crs`` variable.

    A logged warning is emitted whenever the fallback fires so the mismatch is
    visible in the pipeline-strip log rather than silently using EPSG:3857.
    """
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

    Scheme-aware via ``cache.storage_scheme()`` (the lesson):

    - ``s3``: upload via boto3 through the solver module's shared S3 client. The
      runs bucket MUST come from ``TRID3NT_RUNS_BUCKET`` / the explicit
      ``runs_bucket`` arg (no GCP-named default on AWS) - a missing bucket raises
      ``stage="UPLOAD"``. ``content_type`` is passed as the S3 ``ContentType``
      header (OpenQuake omitted it - pass ``None`` for byte-identical behavior).
    - any other scheme (only reachable via a forced ``storage_scheme()`` in
      tests -- GCP is decommissioned, there is no live gs:// path): no cloud
      client is ever constructed. When ``gs_fallback_to_file`` is set this
      degrades straight to a ``file://`` URI (MODFLOW/OpenQuake offline-dev
      path); otherwise it RAISES ``stage="UPLOAD"`` naming the backend as
      absent (SWMM/GeoClaw/Landlab - no silent file:// on the cloud path).
      ``gs_backend`` is accepted for caller-signature compatibility only.

    Raises :class:`CogIoError` (stage ``UPLOAD``). Returns the object URI.
    """
    from trid3nt_server.data.cache import storage_scheme

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
            from trid3nt_server.data.simulation.solver.solver import _get_s3_client

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

    # --- non-s3 scheme: GCP is decommissioned, no gs:// backend ------------ #
    # ``storage_scheme()`` always resolves to "s3" in production; a non-s3
    # scheme is only reachable in tests that force it. Honest handling: no
    # gs client is ever constructed. ``gs_fallback_to_file`` (unchanged
    # per-engine semantics: OpenQuake/MODFLOW opt in, SWMM/GeoClaw/Landlab
    # don't) degrades straight to a ``file://`` URI; otherwise this raises
    # the typed ``CogIoError`` naming the absent backend. ``gs_backend`` is
    # accepted for caller-signature compatibility but no longer selects
    # anything -- both "fsspec" and "gcs_client" hit this same honest path.
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
