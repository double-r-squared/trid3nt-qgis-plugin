"""Atomic tool ``compute_slope`` — terrain slope raster from DEM (job-0081, FR-CE-8, FR-DC).

This module registers one atomic tool that computes a slope raster from a DEM
by wrapping GDAL's ``gdaldem slope`` command:

    ``compute_slope(dem_uri, output_unit, algorithm) → LayerURI``

The result is a single-band GeoTIFF (units: degrees or percent rise/run) in the
same CRS and grid as the input DEM, stored under the FR-DC-3 cache shim at:

    ``gs://grace-2-hazard-prod-cache/cache/static-30d/slope/<key>.tif``

**Cache key** is derived from ``(dem_uri, output_unit, algorithm)`` — all three
parameters materially affect the output pixels, so all three participate in
cache-key derivation (FR-DC-3).

**Implementation flow (cache miss):**

1. Download the DEM bytes from GCS via ``google-cloud-storage``.
2. Write to a temp file (``gdaldem`` requires a file path, not stdin).
3. ``subprocess.run(["gdaldem", "slope", <input>, <output>, *flags])`` where:
   - ``-p`` is added when ``output_unit="percent"`` (percent rise/run).
   - ``-alg ZevenbergenThorne`` is added when ``algorithm="ZevenbergenThorne"``.
   - Horn is the GDAL default (no flag needed).
4. Read the output temp file, clean up.
5. ``read_through`` writes the bytes to the cache bucket.

**Cross-cutting invariants:**

- **Invariant 2 (Deterministic workflows): preserves.** Zero LLM calls.
- **FR-DC-6 (cacheable): honors.** ``cacheable=True``, ``ttl_class="static-30d"``,
  ``source_class="slope"`` — DEM-derived output is stable for the lifetime of
  the cached DEM.
- **NFR-R-1 (resilience): preserves.** ``subprocess.run`` failures surface as
  ``SlopeComputeError`` (typed, never unhandled exception); GCS download
  errors are let through for the agent FR-AS-11 surface to handle.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from typing import Literal, Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import CACHE_BUCKET, read_through

# job-0269: the job-0257 PROJ/GDAL data-dir env fix (without it, conda-env
# gdaldem silently degrades the output CRS to LOCAL_CS — same failure class
# hillshade hit live; slope was never wired).
# job-0271: + COG conversion (flat gdaldem GTiffs render too slowly via WMS).
from .compute_hillshade import _gdaldem_subprocess_env, _translate_to_cog

__all__ = [
    "compute_slope",
    "SlopeComputeError",
]

logger = logging.getLogger("grace2_agent.tools.compute_slope")

# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class SlopeComputeError(RuntimeError):
    """Raised when ``gdaldem slope`` fails or the DEM cannot be fetched.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code surfaced in the
    pipeline strip (NFR-R-1 typed-error requirement).

    Codes:
    - ``GDALDEM_UNAVAILABLE`` — ``gdaldem`` binary not found on PATH.
    - ``GDALDEM_FAILED`` — ``gdaldem slope`` returned non-zero.
    - ``DEM_DOWNLOAD_FAILED`` — GCS download for the DEM URI failed.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_COMPUTE_SLOPE_METADATA = AtomicToolMetadata(
    name="compute_slope",
    ttl_class="static-30d",
    source_class="slope",
    cacheable=True,
)

# ---------------------------------------------------------------------------
# gdaldem binary resolution
# ---------------------------------------------------------------------------

# The ``gdaldem`` binary is expected on PATH. In the dev environment it lives
# in the ``grace2`` conda env (``~/miniforge3/envs/grace2/bin/gdaldem``).
# In the agent container it will be installed alongside GDAL. Override via
# ``GRACE2_GDALDEM_BIN`` env var for environments where the binary is not on
# the default PATH.

_GDALDEM_BIN: str | None = None


def _get_gdaldem_bin() -> str:
    """Resolve the ``gdaldem`` binary path, with env-var override support.

    Checks ``GRACE2_GDALDEM_BIN`` first, then PATH (via ``shutil.which``),
    then the known conda-env path from the dev environment. Raises
    ``SlopeComputeError`` if not found.
    """
    global _GDALDEM_BIN
    if _GDALDEM_BIN is not None:
        return _GDALDEM_BIN

    import shutil

    candidate = (
        os.environ.get("GRACE2_GDALDEM_BIN")
        or shutil.which("gdaldem")
        or _conda_grace2_gdaldem()
    )
    if candidate is None or not os.path.isfile(candidate):
        raise SlopeComputeError(
            "GDALDEM_UNAVAILABLE",
            "gdaldem binary not found on PATH; set GRACE2_GDALDEM_BIN "
            "or install gdal-bin / activate the grace2 conda env.",
        )
    _GDALDEM_BIN = candidate
    return _GDALDEM_BIN


def _conda_grace2_gdaldem() -> str | None:
    """Return the grace2 conda-env gdaldem path if it exists."""
    candidate = os.path.expanduser("~/miniforge3/envs/grace2/bin/gdaldem")
    return candidate if os.path.isfile(candidate) else None


# ---------------------------------------------------------------------------
# GCS download helper
# ---------------------------------------------------------------------------


def _download_dem_bytes(dem_uri: str, storage_client: object | None = None) -> bytes:
    """Download the DEM bytes from an ``s3://`` URI or a local path.

    GCP is decommissioned: object-store reads route through boto3 (S3).
    ``storage_client`` is retained for backward-compatible call signatures
    but is ignored.

    Raises ``SlopeComputeError`` on any failure so callers get a typed error.
    """
    del storage_client  # GCP decommissioned — S3/local only.
    # sprint-14-aws (job-0290b): s3:// staging via the shared boto3 reader.
    if dem_uri.startswith("s3://"):
        from .cache import read_object_bytes_s3
        try:
            return read_object_bytes_s3(dem_uri)
        except Exception as exc:  # noqa: BLE001
            raise SlopeComputeError(
                "DEM_DOWNLOAD_FAILED",
                f"S3 download failed for {dem_uri!r}: {exc}",
            ) from exc
    # Local path — read directly (test / dev convenience).
    try:
        with open(dem_uri, "rb") as f:
            return f.read()
    except OSError as exc:
        raise SlopeComputeError(
            "DEM_DOWNLOAD_FAILED",
            f"Could not read local DEM path {dem_uri!r}: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# gdaldem slope subprocess wrapper
# ---------------------------------------------------------------------------


def _run_gdaldem_slope(
    input_path: str,
    output_path: str,
    output_unit: Literal["degrees", "percent"],
    algorithm: Literal["Horn", "ZevenbergenThorne"],
) -> None:
    """Run ``gdaldem slope`` as a subprocess.

    Args:
        input_path: local file path to the input DEM GeoTIFF.
        output_path: local file path for the output slope GeoTIFF.
        output_unit: ``"degrees"`` (default GDAL) or ``"percent"`` (adds ``-p``).
        algorithm: ``"Horn"`` (default) or ``"ZevenbergenThorne"`` (adds ``-alg ZevenbergenThorne``).

    Raises:
        SlopeComputeError: if the binary is missing or returns non-zero.
    """
    gdaldem = _get_gdaldem_bin()

    cmd: list[str] = [gdaldem, "slope", input_path, output_path]
    if output_unit == "percent":
        cmd.append("-p")
    if algorithm == "ZevenbergenThorne":
        cmd.extend(["-alg", "ZevenbergenThorne"])

    logger.info(
        "compute_slope: running gdaldem slope input=%s output_unit=%s algorithm=%s cmd=%s",
        input_path,
        output_unit,
        algorithm,
        " ".join(cmd),
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=300,  # 5-min ceiling; slope of any reasonable DEM completes in seconds
            env=_gdaldem_subprocess_env(gdaldem),  # job-0257 PROJ/GDAL dirs
        )
    except FileNotFoundError as exc:
        raise SlopeComputeError(
            "GDALDEM_UNAVAILABLE",
            f"gdaldem binary not executable at {gdaldem!r}: {exc}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SlopeComputeError(
            "GDALDEM_FAILED",
            f"gdaldem slope timed out after 300 s for input={input_path!r}: {exc}",
        ) from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        raise SlopeComputeError(
            "GDALDEM_FAILED",
            f"gdaldem slope returned exit code {result.returncode}; "
            f"stderr={stderr!r}; stdout={stdout!r}",
        )

    logger.info(
        "compute_slope: gdaldem slope completed output=%s", output_path
    )


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


@register_tool(
    _COMPUTE_SLOPE_METADATA,
    # Annotations: readOnlyHint=True (reads input raster/vector; writes cache
    # artifact only via the read-through shim), openWorldHint=False (all
    # computation is local GDAL/numpy; no external API calls),
    # destructiveHint=False, idempotentHint=True (deterministic transform;
    # same inputs always produce the same output pixels).
)
def compute_slope(
    dem_uri: str,
    output_unit: Literal["degrees", "percent"] = "degrees",
    algorithm: Literal["Horn", "ZevenbergenThorne"] = "Horn",
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Compute terrain slope from a DEM. Wraps ``gdaldem slope``.

    Applies GDAL's slope algorithm to a single-band elevation GeoTIFF and returns
    a Float32 raster of slope angle (degrees) or percent grade, in the same CRS
    and grid as the input. Cached for 30 days (static-30d TTL class).

    When to use:
        - Landslide susceptibility, mass-movement hazard, or erosion risk mapping.
        - Urban planning, evacuation routing, or accessibility analysis requiring
          terrain steepness.
        - Engineering site assessment or road-grade / construction percent-slope.
        - Input to ``compute_zonal_statistics`` to aggregate slope by zone (e.g.
          mean slope per watershed or flood-depth zone).

    When NOT to use:
        - Hillshade / terrain shading visualization (use ``compute_hillshade``).
        - Colored elevation basemap (use ``compute_colored_relief``).
        - Aspect / flow-direction analysis (use ``compute_aspect``).
        - Bathymetry or sub-aqueous terrain.
        - Dynamic or time-varying slope (output is a static single-time raster).

    Params:
        dem_uri: ``gs://`` URI of a DEM GeoTIFF (typically from ``fetch_dem``).
            Must be a single-band raster with elevation values in meters.
        output_unit: ``"degrees"`` (default) — slope angle 0°–90° (0=flat,
            90=vertical); best for cartographic display and comparison.
            ``"percent"`` — percent rise/run × 100; best for road-grade /
            engineering / construction contexts.
        algorithm: ``"Horn"`` (default) — 3×3 Horn gradient, generally
            accurate for most terrain. ``"ZevenbergenThorne"`` — alternative
            gradient estimator that is smoother on rough / noisy DEMs;
            preferred when the user mentions rough terrain or noisy DEMs.

    Returns:
        A ``LayerURI`` pointing at a slope GeoTIFF in the cache bucket:
        ``gs://grace-2-hazard-prod-cache/cache/static-30d/slope/<key>.tif``.
        The output is a single-band Float32 GeoTIFF in the same CRS and grid
        as the input DEM. Units are degrees (0–90) or percent (0+).

    LLM guidance:
        - Default to ``output_unit="degrees"``. Pick ``"percent"`` when the
          user mentions road grade, engineering design, construction, or
          percent slope.
        - Default to ``algorithm="Horn"``. Pick ``"ZevenbergenThorne"`` if
          the user mentions rough terrain, noisy DEM, or smoother results.

    FR-CE-8: Results are routed through ``read_through`` so repeat calls with
    the same ``(dem_uri, output_unit, algorithm)`` triple return the cached
    slope raster without re-running gdaldem. TTL is 30 days (DEM-derived
    outputs are stable over that window).

    Cross-tool dependencies:
        Upstream (consumes):
        - ``fetch_dem`` — primary source of ``dem_uri``; pass ``LayerURI.uri``
          (gs:// COG) directly as ``dem_uri``.
        Downstream (feeds):
        - ``compute_zonal_statistics`` — pass the returned ``LayerURI`` as
          ``value_raster_uri`` to aggregate slope values by zone.
        - ``publish_layer`` — pass the returned ``LayerURI`` as ``layer_uri``
          to display the slope raster on the map.
        - ``clip_raster_to_polygon`` / ``clip_raster_to_bbox`` — trim the
          slope layer to a study-area boundary before further analysis.

    Raises:
        SlopeComputeError: if gdaldem is unavailable, returns non-zero, or
            the DEM GCS download fails. Error carries ``error_code`` for the
            pipeline strip.
    """
    effective_bucket = _bucket or CACHE_BUCKET

    def _fetch() -> bytes:
        # 1. Download the DEM.
        dem_bytes = _download_dem_bytes(dem_uri, _storage_client)

        # 2. Write to a temp input file.
        in_tmp: str | None = None
        out_tmp: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as in_f:
                in_tmp = in_f.name
                in_f.write(dem_bytes)

            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as out_f:
                out_tmp = out_f.name
            # Remove the output placeholder so gdaldem creates it fresh
            # (gdaldem errors if the output already exists on some GDAL builds).
            os.unlink(out_tmp)

            # 3. Run gdaldem slope.
            _run_gdaldem_slope(in_tmp, out_tmp, output_unit, algorithm)

            # 4. job-0271: return real COG bytes — see _translate_to_cog.
            return _translate_to_cog(out_tmp, _get_gdaldem_bin())
        finally:
            for path in (in_tmp, out_tmp):
                if path is not None:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    # Cache key on (dem_uri, output_unit, algorithm).
    params = {
        "dem_uri": dem_uri,
        "output_unit": output_unit,
        "algorithm": algorithm,
    }

    result = read_through(
        metadata=_COMPUTE_SLOPE_METADATA,
        params=params,
        ext="tif",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
    )
    assert result.uri is not None, "compute_slope is cacheable; uri must be set"

    # Build a stable layer_id from the DEM URI + parameters.
    # Use only the last component of the path (the hash) to keep IDs concise.
    dem_key = dem_uri.rstrip("/").rsplit("/", 1)[-1].replace(".tif", "")
    layer_id = f"slope-{dem_key}-{output_unit}-{algorithm}"

    unit_label = "°" if output_unit == "degrees" else "%"
    return LayerURI(
        layer_id=layer_id,
        name=f"Slope ({output_unit}, {algorithm}) [{unit_label}]",
        layer_type="raster",
        uri=result.uri,
        style_preset="slope_angle_deg",  # tools-backlog #3: slope-angle ylorrd ramp (deg). Backend colormap here; the Orchestrator wires the frontend legend (NATE 2026-06-24).
        role="context",
        units=output_unit,
    )
