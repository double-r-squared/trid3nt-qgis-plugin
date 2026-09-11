"""Atomic tool ``compute_contours`` - elevation contour LINES from a DEM.

Wraps ``gdal_contour`` into FlatGeobuf ``LineString`` features carrying an
``elev`` attribute in EPSG:4326; a derived interval is never zero or negative.
"""
from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import CACHE_BUCKET, read_through
from trid3nt_server.tools.derive._gdal_runner import (
    read_raster_bytes,
    resolve_gdal_contour,
    run_gdal,
)

__all__ = [
    "compute_contours",
    "ContourComputeError",
]

logger = logging.getLogger("trid3nt_server.tools.derive.compute_contours.compute_contours")


def fetch_dem(**kwargs):
    """Resolve ``fetch_dem`` through ``TOOL_REGISTRY`` at call time. Keyword-only."""
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY["fetch_dem"].fn(**kwargs)




# ``error_code`` is one of GDAL_CONTOUR_UNAVAILABLE, GDAL_CONTOUR_FAILED,
# DEM_DOWNLOAD_FAILED, DEM_READ_FAILED, REPROJECT_FAILED, NO_DEM_INPUT.
class ContourComputeError(RuntimeError):
    """``gdal_contour`` failed or the DEM could not be fetched."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code



_COMPUTE_CONTOURS_METADATA = AtomicToolMetadata(
    name="compute_contours",
    ttl_class="static-30d",
    source_class="contours",
    cacheable=True,
)



def _get_gdal_contour_bin() -> str:
    """Resolve ``gdal_contour``; absent raises
    ``ContourComputeError(GDAL_CONTOUR_UNAVAILABLE)``.
    """
    binary = resolve_gdal_contour()
    if binary is None:
        raise ContourComputeError(
            "GDAL_CONTOUR_UNAVAILABLE",
            "gdal_contour binary not found on PATH; set "
            "TRID3NT_GDAL_CONTOUR_BIN (or TRID3NT_GDALDEM_BIN -- gdal_contour is "
            "resolved next to gdaldem) or install gdal-bin.",
        )
    return binary


def _download_dem_bytes(dem_uri: str, storage_client: object | None = None) -> bytes:
    """Read the DEM bytes from an ``s3://`` URI or a local path; ``storage_client``
    is ignored and a read failure raises ``ContourComputeError``.
    """
    del storage_client
    return read_raster_bytes(
        dem_uri,
        on_error=lambda msg: ContourComputeError("DEM_DOWNLOAD_FAILED", msg),
    )



#: "Nice" contour intervals (metres). Derived intervals snap to the closest of
#: these so the contour layer reads cleanly on a map.
_NICE_INTERVALS_M: tuple[float, ...] = (
    1.0, 2.0, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0, 200.0, 250.0, 500.0, 1000.0,
)

#: Target contour count for an AOI: relief / target gives the raw interval,
#: which then snaps to a nice number, for 10-20 readable lines.
_TARGET_CONTOUR_COUNT: float = 15.0


def _snap_to_nice_interval(raw: float) -> float:
    """Snap a raw interval to the nearest value in ``_NICE_INTERVALS_M``; never 0
    or negative, and a raw interval past the largest snaps to that largest.
    """
    if not math.isfinite(raw) or raw <= 0.0:
        return _NICE_INTERVALS_M[0]
    # A tie takes the smaller, denser interval.
    return min(_NICE_INTERVALS_M, key=lambda nice: (abs(nice - raw), nice))


def _read_dem_relief(dem_path: str) -> tuple[float, float]:
    """``(min, max)`` elevation of the DEM, ignoring nodata; an unreadable raster
    or one with no valid pixels raises ``ContourComputeError(DEM_READ_FAILED)``.
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
    """A contour interval in metres from the DEM relief; flat or degenerate relief
    falls back to the smallest nice interval so the call still yields a layer.
    """
    lo, hi = _read_dem_relief(dem_path)
    relief = hi - lo
    if relief <= 0.0:
        return _NICE_INTERVALS_M[0]
    raw = relief / _TARGET_CONTOUR_COUNT
    return _snap_to_nice_interval(raw)




def _dem_bbox_4326(dem_path: str) -> tuple[float, float, float, float] | None:
    """The DEM extent as ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326;
    best-effort, so any failure returns None rather than raising.
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
    except Exception as exc:  # noqa: BLE001 -- zoom-to is best-effort
        logger.warning(
            "compute_contours: could not derive 4326 bbox for %s (%s) -- no zoom-to",
            dem_path,
            exc,
        )
        return None




def _run_gdal_contour(
    input_path: str,
    output_path: str,
    interval_m: float,
) -> None:
    """Run ``gdal_contour`` into a FlatGeobuf of ``LineString`` contours, each with
    an ``elev`` metre attribute; a missing binary or non-zero exit raises.
    """
    gdal_contour = _get_gdal_contour_bin()

    cmd: list[str] = [
        gdal_contour,
        "-a", "elev",
        "-i", str(interval_m),
        "-f", "FlatGeobuf",
        input_path,
        output_path,
    ]

    logger.info(
        "compute_contours: running gdal_contour input=%s interval_m=%s cmd=%s",
        input_path, interval_m, " ".join(cmd),
    )

    run_gdal(
        cmd, gdal_contour,
        on_unavailable=lambda msg: ContourComputeError("GDAL_CONTOUR_UNAVAILABLE", msg),
        on_failed=lambda msg: ContourComputeError("GDAL_CONTOUR_FAILED", msg),
    )

    logger.info(
        "compute_contours: gdal_contour completed output=%s", output_path
    )


def _reproject_fgb_to_4326(input_path: str, output_path: str) -> None:
    """Reproject a FlatGeobuf contour vector to EPSG:4326, copying an input already
    in it; any failure raises ``ContourComputeError(REPROJECT_FAILED)``.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]

        gdf = gpd.read_file(input_path)
        if gdf.crs is None:
            # gdal_contour carries the DEM CRS through, so a missing CRS means
            # the proj wiring failed and no safe reprojection is possible.
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




def _resolve_dem_uri(
    dem_uri: str | None,
    bbox: tuple[float, float, float, float] | None,
) -> str:
    """The DEM uri to contour: ``dem_uri`` when given, else ``fetch_dem(bbox)``.
    Neither supplied raises ``ContourComputeError(NO_DEM_INPUT)``.
    """
    if dem_uri:
        return dem_uri
    if bbox is None:
        raise ContourComputeError(
            "NO_DEM_INPUT",
            "compute_contours requires either dem_uri or bbox; neither given.",
        )
    dem_layer = fetch_dem(bbox=bbox)
    assert dem_layer.uri is not None, "fetch_dem must return a uri"
    return dem_layer.uri




def _make_fetch_fn(
    dem_uri: str,
    interval_m: float | None,
    storage_client: object | None,
) -> tuple[bytes, float, tuple[float, float, float, float] | None]:
    """``(fgb_bytes, effective_interval_m, dem_bbox_4326)``; the interval rides
    along because a derived one is only known once the DEM has been read.
    """
    dem_bytes = _download_dem_bytes(dem_uri, storage_client)

    in_tmp: str | None = None
    out_tmp: str | None = None
    reproj_tmp: str | None = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as in_f:
            in_tmp = in_f.name
            in_f.write(dem_bytes)

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




@register_tool(
    _COMPUTE_CONTOURS_METADATA,
    # openWorldHint=False: the computation is local GDAL/geopandas, and
    # fetch_dem's external call is that tool's own concern.
)
def compute_contours(
    dem_uri: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    interval_m: float | None = None,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Compute elevation contour LINES (topographic isolines) from a DEM. Wraps ``gdal_contour``.

    Use this when: the user asks for "contour lines"/"topographic contours"/
    "topo map", or wants a line overlay on a hillshade/colored-relief base.
    Do NOT use for: the shaded-relief raster itself (``compute_hillshade``/
    ``compute_colored_relief``); slope/aspect (``compute_slope``/
    ``compute_aspect``); per-zone stats (the code_exec playground).

    Params:
        dem_uri: single-band elevation DEM (typically ``fetch_dem(...).uri``).
            Provide this or ``bbox``.
        bbox: (min_lon, min_lat, max_lon, max_lat) EPSG:4326; used to fetch
            the DEM via ``fetch_dem`` when ``dem_uri`` is omitted.
        interval_m: contour interval, metres. ``None`` (default) derives a
            sensible interval from DEM relief (~10-20 readable contours).

    Returns a FlatGeobuf of ``LineString`` contours in EPSG:4326, each with an
    ``elev`` attribute. Failures raise ContourComputeError.
    """
    effective_bucket = _bucket or CACHE_BUCKET

    resolved_dem_uri = _resolve_dem_uri(dem_uri, bbox)

    # A cache HIT does not invoke _fetch, so the interval and bbox it used are
    # captured here to keep the LayerURI metadata accurate either way.
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

    # A None interval_m derives from the DEM alone, so the None key is stable
    # per DEM and does not need the derived value in it.
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

    # Re-deriving the label on a cache hit would need the DEM again, so a
    # caller-pinned interval names the layer and anything else is "auto".
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
        style={"kind": "reference", "geometry": "line"},
        role="context",
        units="m",
        bbox=dem_bbox,
    )
