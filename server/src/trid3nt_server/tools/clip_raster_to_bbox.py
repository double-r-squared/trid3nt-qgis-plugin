"""Atomic tool ``clip_raster_to_bbox`` — clip a raster to a bounding box (job-0085, FR-CE-8, FR-DC).

This module registers one atomic tool that clips a raster to a bounding box,
optionally reprojecting the output:

    ``clip_raster_to_bbox(raster_uri, bbox, bbox_crs, target_crs) → LayerURI``

The result is a clipped GeoTIFF stored under the FR-DC-3 cache shim at:

    ``s3://trid3nt-cache/cache/static-30d/clip_raster/<key>.tif``

**Path selection** (via rasterio CRS comparison):

- If ``target_crs`` is None AND ``bbox_crs`` matches the source raster CRS:
  ``gdal_translate -projwin`` — fast no-reprojection path.
- Otherwise: ``gdalwarp -te <bbox> -te_srs <bbox_crs> [-t_srs <target_crs>]``
  — full reprojection-capable path.

**Cache key** is derived from ``(raster_uri, bbox_rounded_6dp, bbox_crs,
target_crs)`` — all parameters materially affect the output pixels.

**Implementation flow (cache miss):**

1. Detect source CRS with ``rasterio.open(raster_uri).crs``.
2. Choose gdal_translate (fast) or gdalwarp (reproject) based on CRS comparison
   and target_crs.
3. Download source bytes from GCS (``gs://``) or read local file.
4. Write to a temp input file.
5. Run the selected GDAL subprocess.
6. Read output bytes; clean up.
7. ``read_through`` writes bytes to the cache bucket.

**Cross-cutting invariants:**

- **Invariant 2 (Deterministic workflows): preserves.** Zero LLM calls.
- **FR-DC-6 (cacheable): honors.** ``cacheable=True``, ``ttl_class="static-30d"``,
  ``source_class="clip_raster"`` — clip of a static raster is stable.
- **NFR-R-1 (resilience): preserves.** GDAL failures surface as
  ``ClipRasterError`` (typed, never unhandled exception).
"""

from __future__ import annotations
from typing import Any

import logging
import os
import subprocess
import tempfile

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import CACHE_BUCKET, read_through

__all__ = [
    "clip_raster_to_bbox",
    "ClipRasterError",
]

logger = logging.getLogger("trid3nt_server.tools.clip_raster_to_bbox")

# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class ClipRasterError(RuntimeError):
    """Raised when clipping fails or the raster cannot be fetched/opened.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code surfaced in the
    pipeline strip (NFR-R-1 typed-error requirement).

    Codes:
    - ``GDAL_TRANSLATE_UNAVAILABLE`` — ``gdal_translate`` binary not found on PATH.
    - ``GDALWARP_UNAVAILABLE`` — ``gdalwarp`` binary not found on PATH.
    - ``GDAL_TRANSLATE_FAILED`` — ``gdal_translate`` returned non-zero.
    - ``GDALWARP_FAILED`` — ``gdalwarp`` returned non-zero.
    - ``RASTER_OPEN_FAILED`` — could not open raster_uri with rasterio.
    - ``RASTER_DOWNLOAD_FAILED`` — GCS download for the raster URI failed.
    - ``UNKNOWN_RASTER_URI`` — raster_uri not a gs:// URI and not a readable file.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_CLIP_RASTER_METADATA = AtomicToolMetadata(
    name="clip_raster_to_bbox",
    ttl_class="static-30d",
    source_class="clip_raster",
    cacheable=True,
)

# ---------------------------------------------------------------------------
# GDAL binary resolution
# ---------------------------------------------------------------------------

# The ``gdal_translate`` and ``gdalwarp`` binaries are expected on PATH. In the
# dev environment they live in the ``grace2`` conda env
# (``~/miniforge3/envs/grace2/bin/gdal_translate``). In the agent container
# they will be installed alongside GDAL. Override via env vars
# ``TRID3NT_GDAL_TRANSLATE_BIN`` and ``TRID3NT_GDALWARP_BIN``.

_GDAL_TRANSLATE_BIN: str | None = None
_GDALWARP_BIN: str | None = None


def _get_gdal_translate_bin() -> str:
    """Resolve the ``gdal_translate`` binary path, with env-var override support."""
    global _GDAL_TRANSLATE_BIN
    if _GDAL_TRANSLATE_BIN is not None:
        return _GDAL_TRANSLATE_BIN

    import shutil

    candidate = (
        os.environ.get("TRID3NT_GDAL_TRANSLATE_BIN")
        or shutil.which("gdal_translate")
        or _conda_grace2_bin("gdal_translate")
    )
    if candidate is None or not os.path.isfile(candidate):
        raise ClipRasterError(
            "GDAL_TRANSLATE_UNAVAILABLE",
            "gdal_translate binary not found on PATH; set TRID3NT_GDAL_TRANSLATE_BIN "
            "or install gdal-bin / activate the grace2 conda env.",
        )
    _GDAL_TRANSLATE_BIN = candidate
    return _GDAL_TRANSLATE_BIN


def _get_gdalwarp_bin() -> str:
    """Resolve the ``gdalwarp`` binary path, with env-var override support."""
    global _GDALWARP_BIN
    if _GDALWARP_BIN is not None:
        return _GDALWARP_BIN

    import shutil

    candidate = (
        os.environ.get("TRID3NT_GDALWARP_BIN")
        or shutil.which("gdalwarp")
        or _conda_grace2_bin("gdalwarp")
    )
    if candidate is None or not os.path.isfile(candidate):
        raise ClipRasterError(
            "GDALWARP_UNAVAILABLE",
            "gdalwarp binary not found on PATH; set TRID3NT_GDALWARP_BIN "
            "or install gdal-bin / activate the grace2 conda env.",
        )
    _GDALWARP_BIN = candidate
    return _GDALWARP_BIN


def _conda_grace2_bin(name: str) -> str | None:
    """Return the grace2 conda-env binary path if it exists."""
    candidate = os.path.expanduser(f"~/miniforge3/envs/grace2/bin/{name}")
    return candidate if os.path.isfile(candidate) else None


# ---------------------------------------------------------------------------
# Source CRS detection
# ---------------------------------------------------------------------------


def _get_source_crs(raster_uri: str) -> object:
    """Open the raster with rasterio and return its CRS.

    For ``s3://`` URIs the bytes are staged via the shared boto3 reader and
    opened in-memory. For local paths, opens directly.

    Raises:
        ClipRasterError: if the URI is unrecognised or rasterio cannot open it.
    """
    try:
        import rasterio  # type: ignore[import-not-found]

        # sprint-14-aws (job-0293b): s3:// header-read via GDAL /vsis3/ —
        # mirrors the /vsigs/ style; the EC2 instance-role creds resolve
        # through GDAL's AWS credential chain.
        if raster_uri.startswith("s3://"):
            # sprint-14-aws (job-0293c): GDAL's /vsis3/ credential chain does
            # not resolve the EC2 instance role in this env (boto3 does) —
            # observed live: "does not exist" on an existing object. Stage the
            # bytes via the shared boto3 reader and open in-memory.
            from rasterio.io import MemoryFile
            from .cache import read_object_bytes_s3
            with MemoryFile(read_object_bytes_s3(raster_uri)) as mf:
                with mf.open() as src:
                    return src.crs
        elif os.path.isfile(raster_uri):
            with rasterio.open(raster_uri) as src:
                return src.crs
        else:
            raise ClipRasterError(
                "UNKNOWN_RASTER_URI",
                f"raster_uri {raster_uri!r} is not an s3:// URI and is not a "
                "readable local file. Provide an s3:// URI or an absolute local path.",
            )
    except ClipRasterError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ClipRasterError(
            "RASTER_OPEN_FAILED",
            f"rasterio could not open {raster_uri!r}: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Raster download helper
# ---------------------------------------------------------------------------


def _download_raster_bytes(raster_uri: str, storage_client: object | None = None) -> bytes:
    """Download the raster bytes from an ``s3://`` URI or read from local path.

    GCP is decommissioned: object-store reads route through boto3 (S3).
    ``storage_client`` is retained for backward-compatible call signatures
    but is ignored.

    Raises:
        ClipRasterError: on any failure so callers get a typed error.
    """
    del storage_client  # GCP decommissioned — S3/local only.
    # sprint-14-aws (job-0290b): s3:// staging via the shared boto3 reader.
    if raster_uri.startswith("s3://"):
        from .cache import read_object_bytes_s3
        try:
            return read_object_bytes_s3(raster_uri)
        except Exception as exc:  # noqa: BLE001
            raise ClipRasterError(
                "RASTER_DOWNLOAD_FAILED",
                f"S3 download failed for {raster_uri!r}: {exc}",
            ) from exc
    # Local path — read directly (test / dev convenience).
    if not os.path.isfile(raster_uri):
        raise ClipRasterError(
            "UNKNOWN_RASTER_URI",
            f"raster_uri {raster_uri!r} is not an s3:// URI and is not a "
            "readable local file. Provide an s3:// URI or an absolute local path.",
        )
    try:
        with open(raster_uri, "rb") as f:
            return f.read()
    except OSError as exc:
        raise ClipRasterError(
            "RASTER_DOWNLOAD_FAILED",
            f"Could not read local raster path {raster_uri!r}: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# GDAL subprocess wrappers
# ---------------------------------------------------------------------------



def _run_gdal_translate_clip_with_srs(
    input_path: str,
    output_path: str,
    bbox: tuple[float, float, float, float],
    bbox_crs: str,
) -> None:
    """Run ``gdal_translate -projwin`` to clip without reprojection.

    This path is only taken when ``bbox_crs`` already matches the source raster
    CRS (verified by the caller via rasterio CRS comparison). Therefore the
    bbox coordinates are in the native CRS and we do NOT pass ``-projwin_srs``,
    which would require a PROJ database lookup for the EPSG code and fails in
    environments where the conda GDAL's PROJ database is not accessible.

    ``-projwin`` expects upper-left / lower-right corners: ulx uly lrx lry
    (west, north, east, south).

    Args:
        input_path: local file path to the input raster GeoTIFF.
        output_path: local file path for the output clipped GeoTIFF.
        bbox: (west, south, east, north) in ``bbox_crs`` (= source CRS).
        bbox_crs: CRS string — logged for diagnostics only; NOT passed to GDAL
            because the bbox is already in the source native CRS.

    Raises:
        ClipRasterError: if the binary is missing or returns non-zero.
    """
    gdal_translate = _get_gdal_translate_bin()
    west, south, east, north = bbox

    # -projwin ulx uly lrx lry (= west north east south)
    # No -projwin_srs: coords are in native CRS; avoids PROJ DB lookup which
    # fails when the conda-env GDAL can't open its proj.db from subprocess context.
    cmd: list[str] = [
        gdal_translate,
        "-of", "GTiff",
        "-projwin", str(west), str(north), str(east), str(south),
        input_path,
        output_path,
    ]

    logger.info(
        "clip_raster_to_bbox: gdal_translate (native CRS) bbox=%s bbox_crs=%s cmd=%s",
        bbox,
        bbox_crs,
        " ".join(cmd),
    )

    _run_subprocess(cmd, "gdal_translate", "GDAL_TRANSLATE_FAILED", "GDAL_TRANSLATE_UNAVAILABLE")


def _run_gdalwarp_clip(
    input_path: str,
    output_path: str,
    bbox: tuple[float, float, float, float],
    bbox_crs: str,
    target_crs: str | None,
) -> None:
    """Run ``gdalwarp -te -te_srs [-t_srs]`` to clip with optional reprojection.

    Args:
        input_path: local file path to the input raster GeoTIFF.
        output_path: local file path for the output clipped GeoTIFF.
        bbox: (west, south, east, north) in ``bbox_crs``.
        bbox_crs: CRS string the bbox is expressed in.
        target_crs: if provided, reproject output to this CRS; else preserve source CRS.

    Raises:
        ClipRasterError: if the binary is missing or returns non-zero.
    """
    gdalwarp = _get_gdalwarp_bin()
    west, south, east, north = bbox

    cmd: list[str] = [
        gdalwarp,
        "-of", "GTiff",
        "-te", str(west), str(south), str(east), str(north),
        "-te_srs", bbox_crs,
    ]
    if target_crs is not None:
        cmd.extend(["-t_srs", target_crs])

    cmd.extend([input_path, output_path])

    logger.info(
        "clip_raster_to_bbox: gdalwarp bbox=%s bbox_crs=%s target_crs=%s cmd=%s",
        bbox,
        bbox_crs,
        target_crs,
        " ".join(cmd),
    )

    _run_subprocess(cmd, "gdalwarp", "GDALWARP_FAILED", "GDALWARP_UNAVAILABLE")


def _gdal_subprocess_env() -> dict[str, str]:
    """Build an environment dict suitable for running GDAL subprocesses.

    The conda-env GDAL binaries (``gdal_translate``, ``gdalwarp``) link against
    the conda PROJ library, but ``~/miniforge3/envs/grace2/share/proj/proj.db``
    may have a schema version mismatch relative to the PROJ headers that were
    used to build the binary. When that happens, PROJ fails to open the DB and
    EPSG lookups fail (``-projwin_srs``, ``-te_srs``, ``-t_srs`` all use PROJ).

    Resolution priority (TRID3NT_PROJ_LIB env var → system path → no override):
    - ``TRID3NT_PROJ_LIB`` — explicit operator override (e.g. in CI or containers
      where PROJ data is installed at a non-default location).
    - ``/usr/share/proj`` — system PROJ data on Debian/Ubuntu; present on this
      dev machine and in the Cloud Run agent containers.
    - Fallback: do not set PROJ_LIB so the binary resolves its own data path.

    Note: ``PROJ_DATA`` is the modern key (PROJ 9+); ``PROJ_LIB`` is the legacy
    key still honoured by GDAL 3.x. We set both for maximum compatibility.
    """
    env = dict(os.environ)
    # Prefer explicit override
    override = os.environ.get("TRID3NT_PROJ_LIB")
    if override:
        env["PROJ_LIB"] = override
        env["PROJ_DATA"] = override
        return env
    # Try system path
    system_proj = "/usr/share/proj"
    if os.path.isdir(system_proj):
        env["PROJ_LIB"] = system_proj
        env["PROJ_DATA"] = system_proj
    return env


def _run_subprocess(
    cmd: list[str],
    bin_name: str,
    fail_code: str,
    unavailable_code: str,
) -> None:
    """Run a GDAL subprocess; raise ClipRasterError on failure.

    Args:
        cmd: the full command list to run.
        bin_name: human-readable binary name for error messages.
        fail_code: error_code to use when the process returns non-zero.
        unavailable_code: error_code to use when the binary cannot be found.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=300,
            env=_gdal_subprocess_env(),
        )
    except FileNotFoundError as exc:
        raise ClipRasterError(
            unavailable_code,
            f"{bin_name} binary not executable at {cmd[0]!r}: {exc}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClipRasterError(
            fail_code,
            f"{bin_name} timed out after 300 s: {exc}",
        ) from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        raise ClipRasterError(
            fail_code,
            f"{bin_name} returned exit code {result.returncode}; "
            f"stderr={stderr!r}; stdout={stdout!r}",
        )

    logger.info("clip_raster_to_bbox: %s completed output=%s", bin_name, cmd[-1])


# ---------------------------------------------------------------------------
# BBox rounding helper (FR-DC-3: quantize to 6 decimal places)
# ---------------------------------------------------------------------------


def _round_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places for stable cache key derivation."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


@register_tool(
    _CLIP_RASTER_METADATA,
    # Annotations: readOnlyHint=True (reads input raster/vector; writes cache
    # artifact only via the read-through shim), openWorldHint=False (all
    # computation is local GDAL/numpy; no external API calls),
    # destructiveHint=False, idempotentHint=True (deterministic transform;
    # same inputs always produce the same output pixels).
)
def clip_raster_to_bbox(
    raster_uri: str,
    bbox: tuple[float, float, float, float],
    bbox_crs: str = "EPSG:4326",
    target_crs: str | None = None,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Clip a raster to a bounding box, optionally reprojecting.

    Wraps ``gdal_translate -projwin`` (fast path, same CRS) or ``gdalwarp -te
    -te_srs`` (reprojection path) to trim a GCS-hosted raster to a bounding box.
    Returns a new ``LayerURI`` pointing at the clipped raster in the FR-DC cache.
    Cached for 30 days.

    When to use:
        - A fetched raster is larger than the analysis area (national DEM clipped
          to city/county extent, global wind field clipped to storm bbox).
        - Before passing a large raster to ``compute_slope``, ``compute_hillshade``,
          ``compute_colored_relief``, or ``compute_zonal_statistics`` to reduce
          compute and transfer cost.
        - When you need both clipping and CRS reprojection in a single operation.

    When NOT to use:
        - Vector clipping (use ``clip_vector_to_polygon``).
        - Clipping to an irregular polygon boundary (use ``clip_raster_to_polygon``).
        - Reprojection without spatial clipping (use a dedicated gdalwarp call).
        - Per-pixel statistics over the full raster (clip first, then pass to
          ``compute_zonal_statistics``).

    Params:
        raster_uri: source raster URI — ``gs://`` GCS path or absolute local
            file path. Must be a GeoTIFF or any GDAL-readable raster format.
        bbox: (west, south, east, north) bounding box in ``bbox_crs``. The
            output raster will cover this extent (or the intersection with the
            source raster if the bbox is larger than the source).
        bbox_crs: CRS the bbox is expressed in (default ``"EPSG:4326"`` /
            WGS84 lat/lon). Pass the source raster CRS to use the fast
            ``gdal_translate -projwin`` path.
        target_crs: if provided (e.g. ``"EPSG:3857"``), reproject the output
            to this CRS; else preserve the source raster CRS.

    Returns:
        A ``LayerURI`` pointing at a clipped GeoTIFF in the cache bucket:
        ``s3://trid3nt-cache/cache/static-30d/clip_raster/<key>.tif``.

    LLM guidance:
        - bbox_crs default "EPSG:4326" matches user-facing lat/lon inputs;
          pass the user's bbox coordinates directly without conversion.
        - When target_crs is provided alongside bbox_crs "EPSG:4326", both
          clipping and reprojection happen in one gdalwarp pass.
        - Cache key includes all four parameters (bbox rounded to 6dp); a
          1-meter change in bbox extent forces a new clip.

    FR-CE-8: Results are routed through ``read_through`` so repeat calls with
    the same ``(raster_uri, bbox, bbox_crs, target_crs)`` quadruple return the
    cached clip without re-running GDAL. TTL is 30 days.

    Cross-tool dependencies:
        Upstream (consumes):
        - ``fetch_dem`` / ``fetch_landcover`` / ``compute_slope`` /
          ``compute_hillshade`` / ``compute_colored_relief`` /
          ``compute_impervious_surface`` — any of these produce a
          ``LayerURI`` suitable as ``raster_uri``.
        Downstream (feeds):
        - ``compute_zonal_statistics`` — pass the clipped ``LayerURI`` as
          ``value_raster_uri`` or ``zone_input_uri`` for cheaper aggregation.
        - ``compute_slope`` / ``compute_hillshade`` / ``compute_colored_relief`` —
          process only the clipped area.
        - ``publish_layer`` — clip to display extent before publishing.

    Raises:
        ClipRasterError: if GDAL binaries are unavailable, return non-zero,
            the raster cannot be opened/downloaded, or the URI is unrecognised.
    """
    effective_bucket = _bucket or CACHE_BUCKET

    # Round bbox coordinates for stable cache key derivation (FR-DC-3).
    bbox_rounded = _round_bbox(bbox)

    # Detect source CRS to decide which GDAL path to take.
    # This uses rasterio.open().crs on a /vsigs/ virtual path (for gs://) —
    # reads only the header, no full download needed.
    try:
        source_crs = _get_source_crs(raster_uri)
    except ClipRasterError:
        raise

    # Decide which path to use:
    # - gdal_translate (fast): no reprojection requested AND bbox_crs matches source CRS.
    # - gdalwarp (general): reprojection requested OR bbox_crs differs from source CRS.
    try:
        from rasterio.crs import CRS  # type: ignore[import-not-found]

        bbox_crs_obj = CRS.from_user_input(bbox_crs)
        crs_match = (source_crs == bbox_crs_obj)
    except Exception as exc:  # noqa: BLE001
        # If CRS comparison fails, fall back to gdalwarp which handles mismatches.
        logger.warning(
            "clip_raster_to_bbox: CRS comparison failed (%s); falling back to gdalwarp", exc
        )
        crs_match = False

    use_gdal_translate = (target_crs is None) and crs_match

    def _fetch() -> bytes:
        # 1. Download the raster bytes.
        raster_bytes = _download_raster_bytes(raster_uri, _storage_client)

        in_tmp: str | None = None
        out_tmp: str | None = None
        try:
            # 2. Write to a temp input file.
            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as in_f:
                in_tmp = in_f.name
                in_f.write(raster_bytes)

            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as out_f:
                out_tmp = out_f.name
            # Remove output placeholder so GDAL creates it fresh.
            os.unlink(out_tmp)

            # 3. Run the appropriate GDAL command.
            if use_gdal_translate:
                _run_gdal_translate_clip_with_srs(in_tmp, out_tmp, bbox_rounded, bbox_crs)
            else:
                _run_gdalwarp_clip(in_tmp, out_tmp, bbox_rounded, bbox_crs, target_crs)

            # 4. Read output bytes.
            with open(out_tmp, "rb") as f:
                return f.read()
        finally:
            for path in (in_tmp, out_tmp):
                if path is not None:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    # Cache key on (raster_uri, bbox_rounded_6dp, bbox_crs, target_crs).
    # None values are omitted by _canonicalize_params (cache.py rule).
    params: dict[str, object] = {
        "raster_uri": raster_uri,
        "bbox": list(bbox_rounded),  # JSON-serializable
        "bbox_crs": bbox_crs,
        "target_crs": target_crs,
    }

    result = read_through(
        metadata=_CLIP_RASTER_METADATA,
        params=params,
        ext="tif",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
    )
    assert result.uri is not None, "clip_raster_to_bbox is cacheable; uri must be set"

    # Build a stable layer_id from the raster URI hash component.
    raster_key = raster_uri.rstrip("/").rsplit("/", 1)[-1].replace(".tif", "")
    crs_suffix = ""
    if target_crs:
        # Compact CRS label: "EPSG:3857" → "3857"
        crs_suffix = "-" + target_crs.replace("EPSG:", "epsg").replace(":", "-")

    layer_id = f"clip-{raster_key}{crs_suffix}"

    crs_label = target_crs or bbox_crs
    name = f"Clipped raster [{crs_label}]"

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="raster",
        uri=result.uri,
        style_preset="continuous_dem",  # default; caller can override at the map layer
        role="context",
        units=None,
        bbox=None,
    )
