"""Atomic tool ``compute_contours`` — elevation contour LINES from a DEM (F35).

This module registers one atomic tool that computes elevation contour lines
(topographic isolines) from a DEM by wrapping GDAL's ``gdal_contour`` command:

    ``compute_contours(dem_uri | bbox, interval_m) → LayerURI(layer_type="vector")``

The result is a vector contour layer — ``LineString`` features each carrying an
``elev`` (elevation, metres) attribute — at a fixed contour INTERVAL. It is
emitted as a FlatGeobuf in EPSG:4326 so the job-0175 inline-GeoJSON vector path
ships the parsed FeatureCollection to the client and the web vector renderer
paints it as a line layer (``style_preset="contours"``). The artifact is stored
under the FR-DC-3 cache shim at:

    ``s3://trid3nt-cache/cache/static-30d/contours/<key>.fgb``

**Sibling of ``compute_hillshade`` / ``compute_slope``.** Like every terrain
``compute_*`` tool, ``gdal_contour`` ships with GDAL (already on the box — the
binary is resolved next to ``gdaldem``); it does NOT need the PyQGIS worker.

**DEM acquisition.** The caller may pass an explicit ``dem_uri`` (typically the
``LayerURI.uri`` returned by ``fetch_dem``) OR a ``bbox`` with no DEM, in which
case the DEM is fetched the SAME way the other terrain tools acquire one — via
``fetch_dem(bbox)`` (the shared 3DEP acquisition path; no reinvention).

**Contour interval.** When ``interval_m`` is ``None`` a sensible interval is
derived from the DEM relief so any AOI yields ~10–20 readable contours: roughly
``(max - min) / 15`` snapped to a "nice" number from
``{1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000}`` metres. The derived
interval is never 0 or negative.

**Cache key** is derived from ``(dem_uri, interval_m)`` — both materially affect
the output geometry (FR-DC-3).

**Implementation flow (cache miss):**

1. Resolve the DEM bytes (from ``dem_uri``, or fetch via ``fetch_dem(bbox)``).
2. Write the DEM to a temp file (``gdal_contour`` requires a file path).
3. Derive the contour interval from the DEM relief if not supplied.
4. ``subprocess.run(["gdal_contour", "-a", "elev", "-i", <interval>,
   "-f", "FlatGeobuf", <input>, <output>])``.
5. Reproject the contour vector to EPSG:4326 (so the inline-GeoJSON path renders
   it) and read the FlatGeobuf bytes.
6. ``read_through`` writes the bytes to the cache bucket.

**Cross-cutting invariants:**

- **Invariant 2 (Deterministic workflows): preserves.** Zero LLM calls.
- **FR-DC-6 (cacheable): honors.** ``cacheable=True``, ``ttl_class="static-30d"``,
  ``source_class="contours"`` — DEM-derived output is stable for the lifetime of
  the cached DEM (same TTL class as the other terrain ``compute_*`` tools).
- **NFR-R-1 (resilience): preserves.** ``subprocess.run`` failures surface as
  ``ContourComputeError`` (typed, never unhandled exception); DEM-acquisition
  errors are let through for the agent FR-AS-11 surface to handle.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import tempfile
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import CACHE_BUCKET, read_through

# Reuse the job-0257 PROJ/GDAL data-dir env fix from compute_hillshade — without
# it the conda-env GDAL binaries cannot find proj.db and reprojection /
# CRS-tagging silently degrade (same failure class hillshade hit live).
from trid3nt_server.tools.processing.compute_hillshade import _download_dem_bytes, _gdaldem_subprocess_env

__all__ = [
    "compute_contours",
    "ContourComputeError",
]

logger = logging.getLogger("trid3nt_server.tools.processing.compute_contours")

# ---------------------------------------------------------------------------
# Error class (mirrors HillshadeComputeError)
# ---------------------------------------------------------------------------


class ContourComputeError(RuntimeError):
    """Raised when ``gdal_contour`` fails or the DEM cannot be fetched.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code surfaced in the
    pipeline strip / function_response envelope (NFR-R-1 typed-error
    requirement).

    Codes:
    - ``GDAL_CONTOUR_UNAVAILABLE`` — ``gdal_contour`` binary not found.
    - ``GDAL_CONTOUR_FAILED`` — ``gdal_contour`` returned non-zero / timed out.
    - ``DEM_DOWNLOAD_FAILED`` — DEM acquisition failed.
    - ``DEM_READ_FAILED`` — the DEM raster could not be read for relief stats.
    - ``REPROJECT_FAILED`` — the contour vector could not be reprojected to 4326.
    - ``NO_DEM_INPUT`` — neither ``dem_uri`` nor ``bbox`` was supplied.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_COMPUTE_CONTOURS_METADATA = AtomicToolMetadata(
    name="compute_contours",
    ttl_class="static-30d",
    source_class="contours",
    cacheable=True,
)

# ---------------------------------------------------------------------------
# gdal_contour binary resolution (mirrors compute_hillshade._get_gdaldem_bin —
# the binary lives next to gdaldem, so resolve gdaldem then swap the name).
# ---------------------------------------------------------------------------

_GDAL_CONTOUR_BIN: str | None = None


def _conda_grace2_gdal_contour() -> str | None:
    """Return the grace2 conda-env gdal_contour path if it exists."""
    candidate = os.path.expanduser("~/miniforge3/envs/grace2/bin/gdal_contour")
    return candidate if os.path.isfile(candidate) else None


def _get_gdal_contour_bin() -> str:
    """Resolve the ``gdal_contour`` binary path, with env-var override support.

    ``gdal_contour`` ships with GDAL and lives in the SAME directory as
    ``gdaldem``. Resolution order mirrors ``compute_hillshade._get_gdaldem_bin``:

    1. ``TRID3NT_GDAL_CONTOUR_BIN`` explicit override.
    2. The sibling of the resolved ``gdaldem`` binary (so a single
       ``TRID3NT_GDALDEM_BIN`` override covers both tools).
    3. ``shutil.which("gdal_contour")`` on PATH.
    4. The known conda-env path from the dev environment.

    Raises ``ContourComputeError`` if not found.
    """
    global _GDAL_CONTOUR_BIN
    if _GDAL_CONTOUR_BIN is not None:
        return _GDAL_CONTOUR_BIN

    import shutil

    # Sibling-of-gdaldem: reuse compute_hillshade's resolver so a single
    # TRID3NT_GDALDEM_BIN override (or the conda-env fallback) covers both.
    sibling: str | None = None
    try:
        from trid3nt_server.tools.processing.compute_hillshade import _get_gdaldem_bin

        gdaldem = _get_gdaldem_bin()
        candidate = os.path.join(
            os.path.dirname(os.path.abspath(gdaldem)), "gdal_contour"
        )
        if os.path.isfile(candidate):
            sibling = candidate
    except Exception:  # noqa: BLE001 — gdaldem missing is fine; try other paths
        sibling = None

    candidate = (
        os.environ.get("TRID3NT_GDAL_CONTOUR_BIN")
        or sibling
        or shutil.which("gdal_contour")
        or _conda_grace2_gdal_contour()
    )
    if candidate is None or not os.path.isfile(candidate):
        raise ContourComputeError(
            "GDAL_CONTOUR_UNAVAILABLE",
            "gdal_contour binary not found on PATH; set "
            "TRID3NT_GDAL_CONTOUR_BIN (or TRID3NT_GDALDEM_BIN — gdal_contour is "
            "resolved next to gdaldem) or install gdal-bin / activate the "
            "grace2 conda env.",
        )
    _GDAL_CONTOUR_BIN = candidate
    return _GDAL_CONTOUR_BIN


# ---------------------------------------------------------------------------
# Contour-interval derivation
# ---------------------------------------------------------------------------

#: "Nice" contour intervals (metres). Derived intervals snap to the closest of
#: these so the contour layer reads cleanly on a map.
_NICE_INTERVALS_M: tuple[float, ...] = (
    1.0, 2.0, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0, 200.0, 250.0, 500.0, 1000.0,
)

#: Target number of contour lines an AOI should produce (relief / target →
#: raw interval, then snapped to a nice number). ~10–20 readable contours.
_TARGET_CONTOUR_COUNT: float = 15.0


def _snap_to_nice_interval(raw: float) -> float:
    """Snap a raw interval to the nearest "nice" value in ``_NICE_INTERVALS_M``.

    Never returns 0 or a negative value: a non-positive / NaN raw interval
    falls back to the smallest nice interval (1 m). A raw interval larger than
    the biggest nice value snaps to that biggest value.
    """
    if not math.isfinite(raw) or raw <= 0.0:
        return _NICE_INTERVALS_M[0]
    # Pick the nice value closest to raw (ties → the smaller, denser interval).
    return min(_NICE_INTERVALS_M, key=lambda nice: (abs(nice - raw), nice))


def _read_dem_relief(dem_path: str) -> tuple[float, float]:
    """Return (min, max) elevation of the DEM, ignoring nodata.

    Raises ``ContourComputeError(DEM_READ_FAILED)`` if the raster cannot be
    read or has no valid pixels.
    """
    try:
        import numpy as np
        import rasterio

        with rasterio.open(dem_path) as src:
            band = src.read(1, masked=True)
        valid = band.compressed() if hasattr(band, "compressed") else np.asarray(band)
        if valid.size == 0:
            raise ContourComputeError(
                "DEM_READ_FAILED",
                f"DEM {dem_path!r} has no valid (non-nodata) pixels for relief.",
            )
        return float(np.min(valid)), float(np.max(valid))
    except ContourComputeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ContourComputeError(
            "DEM_READ_FAILED",
            f"Could not read DEM relief from {dem_path!r}: {exc}",
        ) from exc


def _derive_interval_m(dem_path: str) -> float:
    """Derive a sensible contour interval (metres) from the DEM relief.

    ``relief / _TARGET_CONTOUR_COUNT`` → snapped to a nice number so any AOI
    yields ~10–20 readable contours. Flat / degenerate relief falls back to the
    smallest nice interval (1 m) so the call still produces a valid layer.
    """
    lo, hi = _read_dem_relief(dem_path)
    relief = hi - lo
    if relief <= 0.0:
        return _NICE_INTERVALS_M[0]
    raw = relief / _TARGET_CONTOUR_COUNT
    return _snap_to_nice_interval(raw)


# ---------------------------------------------------------------------------
# DEM bbox extent (for LayerURI.bbox auto-zoom) — in EPSG:4326
# ---------------------------------------------------------------------------


def _dem_bbox_4326(dem_path: str) -> tuple[float, float, float, float] | None:
    """Return the DEM extent as (min_lon, min_lat, max_lon, max_lat) in 4326.

    Reprojects the raster bounds from the DEM's native CRS to EPSG:4326 so the
    pipeline emitter can fly the camera to the contour layer. Returns ``None``
    (no zoom-to) on any failure — best-effort, never raises.
    """
    try:
        import rasterio
        from rasterio.warp import transform_bounds

        with rasterio.open(dem_path) as src:
            b = src.bounds
            if src.crs is None:
                return None
            west, south, east, north = transform_bounds(
                src.crs, "EPSG:4326", b.left, b.bottom, b.right, b.top
            )
        return (float(west), float(south), float(east), float(north))
    except Exception as exc:  # noqa: BLE001 — zoom-to is best-effort
        logger.warning(
            "compute_contours: could not derive 4326 bbox for %s (%s) — no zoom-to",
            dem_path,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# gdal_contour subprocess wrapper
# ---------------------------------------------------------------------------


def _run_gdal_contour(
    input_path: str,
    output_path: str,
    interval_m: float,
) -> None:
    """Run ``gdal_contour`` as a subprocess.

    Produces a FlatGeobuf of ``LineString`` contours, each carrying an ``elev``
    (elevation, metres) attribute, at the given interval.

    Args:
        input_path: local file path to the input DEM GeoTIFF.
        output_path: local file path for the output FlatGeobuf.
        interval_m: contour interval in metres (must be > 0).

    Raises:
        ContourComputeError: if the binary is missing or returns non-zero.
    """
    gdal_contour = _get_gdal_contour_bin()

    cmd: list[str] = [
        gdal_contour,
        "-a", "elev",          # write elevation into the 'elev' attribute
        "-i", str(interval_m),  # contour interval (metres)
        "-f", "FlatGeobuf",    # output driver
        input_path,
        output_path,
    ]

    logger.info(
        "compute_contours: running gdal_contour input=%s interval_m=%s cmd=%s",
        input_path, interval_m, " ".join(cmd),
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=300,  # 5-min ceiling; contours of any reasonable DEM are seconds
            env=_gdaldem_subprocess_env(gdal_contour),  # job-0257 PROJ/GDAL dirs
        )
    except FileNotFoundError as exc:
        raise ContourComputeError(
            "GDAL_CONTOUR_UNAVAILABLE",
            f"gdal_contour binary not executable at {gdal_contour!r}: {exc}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ContourComputeError(
            "GDAL_CONTOUR_FAILED",
            f"gdal_contour timed out after 300 s for input={input_path!r}: {exc}",
        ) from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        raise ContourComputeError(
            "GDAL_CONTOUR_FAILED",
            f"gdal_contour returned exit code {result.returncode}; "
            f"stderr={stderr!r}; stdout={stdout!r}",
        )

    logger.info(
        "compute_contours: gdal_contour completed output=%s", output_path
    )


def _reproject_fgb_to_4326(input_path: str, output_path: str) -> None:
    """Reproject a FlatGeobuf contour vector to EPSG:4326.

    The inline-GeoJSON vector path (job-0175) reads the artifact server-side and
    ships it to MapLibre, which expects WGS84 coordinates — so the contours must
    be in EPSG:4326. If the input is already in 4326, this is effectively a copy.

    Raises ``ContourComputeError(REPROJECT_FAILED)`` on any failure.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]

        gdf = gpd.read_file(input_path)
        if gdf.crs is None:
            # gdal_contour carries the DEM CRS through; a missing CRS means the
            # proj wiring failed. We cannot safely reproject — leave as-is.
            logger.warning(
                "compute_contours: contour vector has no CRS; writing without "
                "reprojection (output may not align on the map)."
            )
        elif str(gdf.crs).upper() not in {"EPSG:4326", "WGS84"}:
            gdf = gdf.to_crs("EPSG:4326")
        gdf.to_file(output_path, driver="FlatGeobuf", engine="pyogrio")
    except ContourComputeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ContourComputeError(
            "REPROJECT_FAILED",
            f"could not reproject contour vector to EPSG:4326: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# DEM resolution (explicit dem_uri OR fetch via bbox — shared fetch_dem path)
# ---------------------------------------------------------------------------


def _resolve_dem_uri(
    dem_uri: str | None,
    bbox: tuple[float, float, float, float] | None,
) -> str:
    """Return a DEM ``uri`` to contour.

    If ``dem_uri`` is supplied it is used directly. Otherwise a ``bbox`` is
    required and the DEM is fetched the SAME way the other terrain tools do —
    via ``fetch_dem(bbox)`` (the shared 3DEP acquisition path; no reinvention).

    Raises ``ContourComputeError(NO_DEM_INPUT)`` if neither is supplied.
    """
    if dem_uri:
        return dem_uri
    if bbox is None:
        raise ContourComputeError(
            "NO_DEM_INPUT",
            "compute_contours requires either dem_uri or bbox; neither given.",
        )
    # Reuse fetch_dem — the shared DEM-acquisition path (do not reinvent).
    from trid3nt_server.tools.fetchers.terrain.fetch_dem import fetch_dem

    dem_layer = fetch_dem(bbox)
    assert dem_layer.uri is not None, "fetch_dem must return a uri"
    return dem_layer.uri


# ---------------------------------------------------------------------------
# Fetch function (cache miss)
# ---------------------------------------------------------------------------


def _make_fetch_fn(
    dem_uri: str,
    interval_m: float | None,
    storage_client: object | None,
) -> tuple[bytes, float, tuple[float, float, float, float] | None]:
    """Produce contour FlatGeobuf bytes for the DEM on cache-miss.

    Returns ``(fgb_bytes, effective_interval_m, dem_bbox_4326)``. The interval
    and bbox are returned alongside the bytes so the LayerURI metadata reflects
    the actual values used (the interval may have been derived from relief).
    """
    dem_bytes = _download_dem_bytes(dem_uri, storage_client)

    in_tmp: str | None = None
    out_tmp: str | None = None
    reproj_tmp: str | None = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as in_f:
            in_tmp = in_f.name
            in_f.write(dem_bytes)

        # Derive the interval from relief if the caller did not pin one.
        effective_interval = (
            float(interval_m)
            if interval_m is not None and float(interval_m) > 0.0
            else _derive_interval_m(in_tmp)
        )

        bbox_4326 = _dem_bbox_4326(in_tmp)

        with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as out_f:
            out_tmp = out_f.name
        os.unlink(out_tmp)  # gdal_contour creates the file fresh

        _run_gdal_contour(in_tmp, out_tmp, effective_interval)

        # Reproject to EPSG:4326 for the inline-GeoJSON vector render path.
        with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as rp_f:
            reproj_tmp = rp_f.name
        os.unlink(reproj_tmp)
        _reproject_fgb_to_4326(out_tmp, reproj_tmp)

        with open(reproj_tmp, "rb") as f:
            fgb_bytes = f.read()
        return fgb_bytes, effective_interval, bbox_4326
    finally:
        for path in (in_tmp, out_tmp, reproj_tmp):
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


@register_tool(
    _COMPUTE_CONTOURS_METADATA,
    # Annotations: readOnlyHint=True (reads input raster; writes cache artifact
    # only via the read-through shim), openWorldHint=False (all computation is
    # local GDAL/geopandas — fetch_dem's external call is its own tool's
    # concern), destructiveHint=False, idempotentHint=True (deterministic
    # transform; same DEM + interval always produce the same contours).
)
def compute_contours(
    dem_uri: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    interval_m: float | None = None,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Compute elevation contour LINES (topographic isolines) from a DEM. Wraps ``gdal_contour``.

    Use this when: the user asks for "contour lines"/"topographic contours"/
    "topo map", or wants a line overlay on a hillshade/colored-relief base.
    Do NOT use for: the shaded-relief raster itself (``compute_hillshade``/
    ``compute_colored_relief``); slope/aspect (``compute_slope``/
    ``compute_aspect``); per-zone stats (``compute_zonal_statistics``).

    Params:
        dem_uri: single-band elevation DEM (typically ``fetch_dem(...).uri``).
            Provide this or ``bbox``.
        bbox: (min_lon, min_lat, max_lon, max_lat) EPSG:4326; used to fetch
            the DEM via ``fetch_dem`` when ``dem_uri`` is omitted.
        interval_m: contour interval, metres. ``None`` (default) derives a
            sensible interval from DEM relief (~10-20 readable contours).

    Returns:
        ``LayerURI`` (vector, ``style_preset="contours"``, ``units="m"``)
        for a FlatGeobuf of ``LineString`` contours (EPSG:4326, each with
        an ``elev`` attribute), cache bucket, TTL 30d.

    Raises:
        ContourComputeError: gdal_contour unavailable/non-zero, DEM
            fetch/read failure, reprojection failure, or neither
            ``dem_uri`` nor ``bbox`` supplied.
    """
    effective_bucket = _bucket or CACHE_BUCKET

    resolved_dem_uri = _resolve_dem_uri(dem_uri, bbox)

    # Capture the interval + bbox the fetch actually used so the LayerURI
    # metadata is accurate even on a cache HIT (where _fetch is not invoked).
    captured: dict[str, Any] = {"interval_m": None, "bbox": None}

    def _fetch() -> bytes:
        fgb_bytes, eff_interval, dem_bbox = _make_fetch_fn(
            dem_uri=resolved_dem_uri,
            interval_m=interval_m,
            storage_client=_storage_client,
        )
        captured["interval_m"] = eff_interval
        captured["bbox"] = dem_bbox
        return fgb_bytes

    # Cache key on (dem_uri, interval_m). When interval_m is None the derived
    # interval depends only on the DEM, so the None key is stable per-DEM.
    params = {
        "dem_uri": resolved_dem_uri,
        "interval_m": interval_m,
    }

    result = read_through(
        metadata=_COMPUTE_CONTOURS_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
    )
    assert result.uri is not None, "compute_contours is cacheable; uri must be set"

    # On a cache hit _fetch did not run; recover the interval for labelling.
    # If the caller pinned an interval, use it; otherwise re-derive a label
    # value lazily would require the DEM — so fall back to "auto" in the name.
    eff_interval = captured["interval_m"]
    dem_bbox = captured["bbox"]

    dem_key = resolved_dem_uri.rstrip("/").rsplit("/", 1)[-1].replace(".tif", "")
    if eff_interval is not None:
        interval_label = (
            f"{int(eff_interval)}" if float(eff_interval).is_integer()
            else f"{eff_interval:g}"
        )
        id_interval = interval_label
        name = f"Contours ({interval_label} m)"
    elif interval_m is not None:
        interval_label = (
            f"{int(interval_m)}" if float(interval_m).is_integer()
            else f"{interval_m:g}"
        )
        id_interval = interval_label
        name = f"Contours ({interval_label} m)"
    else:
        id_interval = "auto"
        name = "Contours (auto interval)"

    layer_id = f"contours-{dem_key}-{id_interval}m"

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="contours",  # thin line style; renderer colors lines generically
        role="context",
        units="m",
        bbox=dem_bbox,
    )
