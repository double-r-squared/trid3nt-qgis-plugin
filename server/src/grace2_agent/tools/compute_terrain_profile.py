"""``compute_terrain_profile`` atomic tool -- elevation profile along a line.

QGIS-plugin-wrapping backlog (the QGIS "Profile tool" / "Terrain Profile" plugin
family). The plugin's numerical kernel is tiny: walk a polyline, sample one or
more DEM rasters at evenly-spaced stations, and plot distance-vs-elevation. The
plugin's weight is the interactive canvas draw + the Qt/matplotlib dock; the
COMPUTE is a small ``rasterio`` sampling loop. Per the survey, the right move is
to REIMPLEMENT the kernel in rasterio/shapely/pyproj (which the agent already
has) rather than wrap the heavy-UI GPL plugin.

This tool is framed in the user's TERRAIN/ELEVATION vocabulary ("terrain
profile", "elevation profile", "long profile of the ground") and samples DEM /
COG elevation layers (``fetch_dem`` / ``fetch_topobathy`` / ``fetch_3dep_extra``
outputs). It is a sibling of ``compute_cross_section`` (the generic
distance-vs-VALUE section view): same chart-emission output shape, same geodesic
x-axis, same multi-raster overlay -- but scoped to TERRAIN elevation so the
Bedrock LLM routes elevation-profile phrasing here.

CRS CORRECTNESS (the live bug this guards against): the line arrives in EPSG:4326
(lon/lat) but a DEM COG is often in a projected CRS (e.g. UTM). Sampling lon/lat
coordinates directly against a UTM raster silently reads the wrong cells (or all
nodata). This tool ALWAYS reprojects the station coordinates into each raster's
own CRS (``rasterio.warp.transform``) BEFORE ``src.sample`` -- per-raster, since
overlaid DEMs may differ in CRS.

DATA FLOW
---------
1. Resolve ``line`` to a shapely LineString in EPSG:4326 (accepts a GeoJSON
   LineString / Feature / FeatureCollection, or a bare ``[[lon,lat], ...]`` list
   -- a user-drawn line via ``request_spatial_input`` OR an agent-derived line).
2. Interpolate N evenly-spaced stations by planar arc-length and compute the
   cumulative GEODESIC distance (``pyproj.Geod``, WGS84) so x is metres on the
   ground, not degrees.
3. For each DEM ``layer_uri``: stage s3:// bytes via ``read_object_bytes_s3`` +
   ``rasterio.MemoryFile`` (the documented /vsis3/-credential workaround),
   reproject the stations into the raster CRS, and read all stations in one
   vectorized ``src.sample`` call. nodata / out-of-bounds -> ``None`` (honesty
   floor, never fabricated).
4. Build a Vega-Lite v5 line spec (x=distance_m, y=elevation, one coloured line
   per DEM) and wrap via ``chart_tools.build_chart_payload`` -> a
   ChartEmissionPayload dict the agent loop emits + persists + summarizes.

DETERMINISM (Invariant 2): zero LLM calls, zero randomness.

CACHING: ``cacheable=False`` / ``ttl_class="live-no-cache"`` -- like
``compute_cross_section`` and ``chart_tools``, this mints a fresh ``chart_id``
per call; the expensive raster read is already cached upstream by the fetcher.

GPL-cleanliness: **clean-room reimplementation** of a commodity line-sampling
kernel in rasterio/shapely/pyproj. No GPL plugin source was copied; the
sampling-along-a-line behaviour is re-derived from the documented plugin
behaviour (and mirrors the first-party ``compute_cross_section``).
"""

from __future__ import annotations

import logging
from typing import Any

from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .chart_tools import build_chart_payload

__all__ = [
    "compute_terrain_profile",
    "TerrainProfileError",
]

logger = logging.getLogger("grace2_agent.tools.compute_terrain_profile")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_N_STATIONS = 200
_MIN_N_STATIONS = 2
_MAX_N_STATIONS = 2000
_MAX_LAYERS = 4
_VEGA_LITE_V5_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"


# ---------------------------------------------------------------------------
# Error type (NFR-R-1 typed-error surface)
# ---------------------------------------------------------------------------


class TerrainProfileError(RuntimeError):
    """Raised when ``compute_terrain_profile`` cannot produce a profile.

    Codes:
    - ``LINE_INVALID``         -- ``line`` is not a usable LineString.
    - ``NO_LAYERS``            -- no DEM ``layer_uri`` supplied.
    - ``TOO_MANY_LAYERS``      -- more than ``_MAX_LAYERS`` DEMs requested.
    - ``LAYER_OPEN_FAILED``    -- a raster could not be opened with rasterio.
    - ``DOWNLOAD_FAILED``      -- an s3:// download for a layer failed.
    - ``LINE_REPROJECT_FAILED``-- reprojecting the line into a raster CRS failed.
    - ``LINE_OUTSIDE_RASTER``  -- every station fell on nodata / outside EVERY
      DEM (the profile would be entirely null -- surfaced, not faked).
    """

    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="compute_terrain_profile",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    supports_global_query=False,
)


# ---------------------------------------------------------------------------
# Line resolution -- GeoJSON LineString / [lon,lat] list / FeatureCollection
# ---------------------------------------------------------------------------


def _coords_from_geojson_geometry(geom: dict[str, Any]) -> list[list[float]]:
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype != "LineString" or not isinstance(coords, list):
        raise TerrainProfileError(
            "LINE_INVALID",
            f"line geometry must be a LineString; got type={gtype!r}.",
        )
    return coords  # type: ignore[return-value]


def _resolve_line_coords(line: Any) -> list[list[float]]:
    """Resolve ``line`` to a list of ``[lon, lat]`` vertices (EPSG:4326).

    Accepts a GeoJSON LineString geometry, a Feature wrapping a LineString, a
    FeatureCollection (FIRST LineString used -- so a drawn FC feeds through), or
    a bare list of ``[lon, lat]`` vertices. Raises ``LINE_INVALID`` on any
    unusable input.
    """
    if line is None:
        raise TerrainProfileError("LINE_INVALID", "line is required (got None).")

    coords: list[Any] | None = None

    if isinstance(line, dict):
        gtype = line.get("type")
        if gtype == "LineString":
            coords = _coords_from_geojson_geometry(line)
        elif gtype == "Feature":
            geom = line.get("geometry")
            if not isinstance(geom, dict):
                raise TerrainProfileError(
                    "LINE_INVALID", "line Feature carried no geometry dict."
                )
            coords = _coords_from_geojson_geometry(geom)
        elif gtype == "FeatureCollection":
            feats = line.get("features")
            if not isinstance(feats, list) or not feats:
                raise TerrainProfileError(
                    "LINE_INVALID", "line FeatureCollection carried no features."
                )
            for feat in feats:
                geom = feat.get("geometry") if isinstance(feat, dict) else None
                if isinstance(geom, dict) and geom.get("type") == "LineString":
                    coords = _coords_from_geojson_geometry(geom)
                    break
            if coords is None:
                raise TerrainProfileError(
                    "LINE_INVALID",
                    "line FeatureCollection contained no LineString feature.",
                )
        else:
            raise TerrainProfileError(
                "LINE_INVALID",
                f"line dict has unsupported GeoJSON type={gtype!r}; expected "
                "LineString, Feature, or FeatureCollection.",
            )
    elif isinstance(line, (list, tuple)):
        coords = list(line)
    else:
        raise TerrainProfileError(
            "LINE_INVALID",
            f"line must be a GeoJSON LineString/Feature/FeatureCollection or a "
            f"list of [lon, lat] vertices; got {type(line).__name__}.",
        )

    cleaned: list[list[float]] = []
    for i, pt in enumerate(coords):
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            raise TerrainProfileError(
                "LINE_INVALID",
                f"line vertex[{i}] must be a [lon, lat] pair; got {pt!r}.",
            )
        try:
            lon = float(pt[0])
            lat = float(pt[1])
        except (TypeError, ValueError) as exc:
            raise TerrainProfileError(
                "LINE_INVALID",
                f"line vertex[{i}] has non-numeric coordinates: {pt!r}.",
            ) from exc
        cleaned.append([lon, lat])

    deduped: list[list[float]] = []
    for pt in cleaned:
        if not deduped or deduped[-1] != pt:
            deduped.append(pt)

    if len(deduped) < 2:
        raise TerrainProfileError(
            "LINE_INVALID",
            "line needs at least 2 distinct vertices to form a profile; got "
            f"{len(deduped)} after de-duplication.",
        )
    return deduped


# ---------------------------------------------------------------------------
# Raster open helper (s3 staging + MemoryFile)
# ---------------------------------------------------------------------------


def _open_raster_source(layer_uri: str) -> tuple[Any, bool]:
    """Return (bytes-or-path, is_memory) for opening ``layer_uri`` with rasterio.

    For ``s3://`` URIs the bytes are staged via the shared boto3 reader (GDAL's
    /vsis3/ credential chain does not resolve the EC2 instance role in this env;
    boto3 does). Returns raw bytes for s3:// (open via MemoryFile) or the local
    path for a file.
    """
    if layer_uri.startswith("s3://"):
        from .cache import read_object_bytes_s3

        try:
            return read_object_bytes_s3(layer_uri), True
        except Exception as exc:  # noqa: BLE001
            raise TerrainProfileError(
                "DOWNLOAD_FAILED",
                f"S3 download failed for {layer_uri!r}: {exc}",
                retryable=True,
            ) from exc
    import os

    if os.path.isfile(layer_uri):
        return layer_uri, False
    raise TerrainProfileError(
        "LAYER_OPEN_FAILED",
        f"layer_uri {layer_uri!r} is not an s3:// URI and is not a readable "
        "local file.",
    )


# ---------------------------------------------------------------------------
# Station interpolation + geodesic distance
# ---------------------------------------------------------------------------


def _interpolate_stations(
    coords: list[list[float]], n_stations: int
) -> tuple[list[tuple[float, float]], list[float]]:
    """Return (station lon/lat list, cumulative geodesic distance_m list)."""
    from shapely.geometry import LineString

    geom = LineString(coords)
    total_len = geom.length
    stations: list[tuple[float, float]] = []
    for i in range(n_stations):
        frac = i / (n_stations - 1) if n_stations > 1 else 0.0
        pt = geom.interpolate(frac * total_len)
        stations.append((pt.x, pt.y))

    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    distances: list[float] = [0.0]
    for j in range(1, len(stations)):
        lon0, lat0 = stations[j - 1]
        lon1, lat1 = stations[j]
        _, _, seg_m = geod.inv(lon0, lat0, lon1, lat1)
        distances.append(distances[-1] + float(seg_m))
    return stations, distances


# ---------------------------------------------------------------------------
# Per-layer sampling (CRS-correct: reproject stations into the raster CRS)
# ---------------------------------------------------------------------------


def _sample_layer(
    layer_uri: str,
    stations_4326: list[tuple[float, float]],
) -> tuple[list[float | None], str | None, int]:
    """Sample one DEM at ``stations_4326`` (lon/lat). Return (values, units, n_valid).

    Reprojects the stations into the raster's OWN CRS (the CRS-correctness guard:
    a lon/lat line must NOT be sampled directly against a UTM DEM), reads band 1
    at every station in one vectorized ``src.sample`` call, and maps nodata /
    out-of-bounds reads to ``None`` (honesty floor).
    """
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.warp import transform

    source, is_memory = _open_raster_source(layer_uri)

    def _read(src) -> tuple[list[float | None], str | None, int]:  # type: ignore[no-untyped-def]
        raster_crs = src.crs
        nodata = src.nodata
        units = None
        try:
            band_units = src.units
            if band_units and band_units[0]:
                units = str(band_units[0])
        except Exception:  # noqa: BLE001 -- units are best-effort metadata
            units = None

        xs = [lon for lon, _ in stations_4326]
        ys = [lat for _, lat in stations_4326]
        # CRS-correctness: reproject the EPSG:4326 stations into the raster's CRS
        # so src.sample reads the right cells (src.sample expects coordinates in
        # the raster's own CRS). This is the guard against the lon/lat-vs-UTM bug.
        if raster_crs is not None and raster_crs.to_epsg() != 4326:
            try:
                xs, ys = transform("EPSG:4326", raster_crs, xs, ys)
            except Exception as exc:  # noqa: BLE001
                raise TerrainProfileError(
                    "LINE_REPROJECT_FAILED",
                    f"reprojecting stations into {raster_crs} for {layer_uri!r} "
                    f"failed: {exc}",
                ) from exc

        coords = list(zip(xs, ys))
        # Out-of-bounds guard (honesty floor): a raster WITHOUT a nodata value
        # returns 0.0 for coordinates outside its grid -- indistinguishable from
        # a real reading. So flag stations whose pixel (row, col) falls outside
        # the raster window as None explicitly, independent of nodata. (When a
        # nodata value IS set, src.sample already returns it for out-of-bounds.)
        height = src.height
        width = src.width
        in_bounds: list[bool] = []
        for x, y in coords:
            try:
                row, col = src.index(x, y)
            except Exception:  # noqa: BLE001
                in_bounds.append(False)
                continue
            in_bounds.append(0 <= row < height and 0 <= col < width)

        values: list[float | None] = []
        n_valid = 0
        for station_idx, arr in enumerate(src.sample(coords, indexes=1)):
            if not in_bounds[station_idx]:
                values.append(None)
                continue
            v = float(arr[0])
            is_nan = v != v  # NaN-safe
            is_nodata = nodata is not None and v == nodata
            if is_nan or is_nodata:
                values.append(None)
            else:
                values.append(v)
                n_valid += 1
        return values, units, n_valid

    try:
        if is_memory:
            with MemoryFile(source) as mf:
                with mf.open() as src:
                    return _read(src)
        else:
            with rasterio.open(source) as src:
                return _read(src)
    except TerrainProfileError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TerrainProfileError(
            "LAYER_OPEN_FAILED",
            f"rasterio could not sample {layer_uri!r}: {exc}",
        ) from exc


def _layer_label(layer_uri: str) -> str:
    base = layer_uri.rstrip("/").rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base or layer_uri


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # readOnlyHint=True (samples input DEMs; emits a chart, no side effects),
    # openWorldHint=False (local GDAL read), destructiveHint=False,
    # idempotentHint=True (deterministic: same line+DEMs -> same profile).
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def compute_terrain_profile(
    layer_uri: str,
    line: Any,
    n_samples: int = _DEFAULT_N_STATIONS,
    extra_layer_uris: list[str] | None = None,
    *,
    _created_turn_id: str | None = None,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Sample DEM elevation along a line and chart the terrain (long) profile.

    Use this when the user wants the ELEVATION / TERRAIN PROFILE of the ground
    along a drawn or derived line -- "show the elevation profile across this
    ridge", "plot the terrain profile along the road", "what does the ground
    look like along this transect", "long profile of the valley floor". The
    x-axis is distance-along-the-line in metres (geodesic); the y-axis is
    elevation in the DEM's native units.

    Multi-DEM overlay: pass ``extra_layer_uris`` to overlay several elevation
    surfaces on the SAME line and distance axis (e.g. bare-earth DEM vs
    topo-bathy, pre- vs post-event terrain). Cap of 4 DEMs.

    CRS correctness: the line is EPSG:4326 (lon/lat); each DEM may be in a
    projected CRS (UTM, etc.). The tool reprojects the sample stations into each
    raster's CRS before sampling, so a lon/lat line over a UTM DEM samples the
    correct cells (NOT all nodata).

    The line input (any of):
        - A user-DRAWN line: ``request_spatial_input(mode="vector_draw")``, then
          pass the returned FeatureCollection (FIRST LineString used).
        - An agent-DERIVED line: construct endpoints and pass a GeoJSON
          LineString or a ``[[lon,lat], [lon,lat], ...]`` list.

    Do NOT use this for: a non-terrain value section view across a flood/head
    raster (use ``compute_cross_section``, the general distance-vs-value tool);
    a value distribution (``generate_histogram``); a value over time
    (``generate_time_series``); a single number for the line
    (``compute_zonal_statistics`` over a buffered line).

    Parameters:
        layer_uri: ``s3://`` URI or local path of the PRIMARY DEM / elevation COG
            (``fetch_dem`` / ``fetch_topobathy`` / ``fetch_3dep_extra`` output).
        line: the profile line -- a GeoJSON LineString / Feature /
            FeatureCollection, or a list of ``[lon, lat]`` vertices (EPSG:4326,
            lon-first). At least 2 distinct vertices.
        n_samples: number of evenly-spaced sample stations along the line
            (default 200; clamped to [2, 2000]).
        extra_layer_uris: OPTIONAL up to 3 additional DEM URIs to overlay on the
            same line. Sampled over the SAME stations; uncovered stations read
            null and that surface's line breaks there.

    Returns:
        A ChartEmissionPayload dict (``envelope_type="chart-emission"``) carrying
        a Vega-Lite v5 line chart (x = distance_m, y = elevation, one coloured
        line per DEM), a title, and a caption with the elevation range / relief.
        The agent loop emits this as a chart-emission envelope.

    Raises:
        TerrainProfileError: typed ``error_code`` (LINE_INVALID, NO_LAYERS,
            TOO_MANY_LAYERS, LAYER_OPEN_FAILED, DOWNLOAD_FAILED,
            LINE_REPROJECT_FAILED, LINE_OUTSIDE_RASTER).
    """
    # ---- validate inputs ---------------------------------------------------
    if not isinstance(layer_uri, str) or not layer_uri.strip():
        raise TerrainProfileError(
            "NO_LAYERS",
            f"layer_uri must be a non-empty DEM URI string; got {layer_uri!r}.",
        )
    layer_uris: list[str] = [layer_uri.strip()]
    if extra_layer_uris:
        if not isinstance(extra_layer_uris, (list, tuple)):
            raise TerrainProfileError(
                "NO_LAYERS",
                f"extra_layer_uris must be a list of URI strings; got "
                f"{type(extra_layer_uris).__name__}.",
            )
        for u in extra_layer_uris:
            if isinstance(u, str) and u.strip():
                layer_uris.append(u.strip())
    if len(layer_uris) > _MAX_LAYERS:
        raise TerrainProfileError(
            "TOO_MANY_LAYERS",
            f"at most {_MAX_LAYERS} DEMs may be overlaid; got {len(layer_uris)}.",
        )

    try:
        n = int(n_samples)
    except (TypeError, ValueError):
        n = _DEFAULT_N_STATIONS
    n = max(_MIN_N_STATIONS, min(_MAX_N_STATIONS, n))

    coords = _resolve_line_coords(line)
    stations, distances = _interpolate_stations(coords, n)

    # ---- sample every DEM over the SAME stations ---------------------------
    rows: list[dict[str, Any]] = []
    per_layer_units: list[str | None] = []
    per_layer_label: list[str] = []
    per_layer_valid: list[int] = []
    layer_value_extent: list[tuple[float, float] | None] = []

    for layer_idx, uri in enumerate(layer_uris):
        values, units, n_valid = _sample_layer(uri, stations)
        label = _layer_label(uri)
        if label in per_layer_label:
            label = f"{label} ({layer_idx + 1})"
        per_layer_label.append(label)
        per_layer_units.append(units)
        per_layer_valid.append(n_valid)

        finite = [v for v in values if v is not None]
        layer_value_extent.append((min(finite), max(finite)) if finite else None)

        for station_idx, (dist_m, (lon, lat)) in enumerate(zip(distances, stations)):
            rows.append(
                {
                    "distance_m": round(float(dist_m), 3),
                    "elevation": values[station_idx],
                    "lon": round(float(lon), 6),
                    "lat": round(float(lat), 6),
                    "surface": label,
                }
            )

    total_valid = sum(per_layer_valid)
    if total_valid == 0:
        raise TerrainProfileError(
            "LINE_OUTSIDE_RASTER",
            "every station fell on nodata or outside ALL supplied DEMs -- the "
            "line does not cross any DEM's covered extent. Check the line "
            "location and the DEM CRS/coverage; do not fabricate a profile.",
        )

    distinct_units = {u for u in per_layer_units if u}
    multi_layer = len(layer_uris) > 1
    units_match = len(distinct_units) <= 1
    y_title = next(iter(distinct_units)) if distinct_units else "elevation (m)"

    spec = _build_profile_spec(
        rows=rows,
        multi_layer=multi_layer,
        units_match=units_match,
        y_title=y_title,
        per_layer_label=per_layer_label,
        per_layer_units=per_layer_units,
    )

    total_len_m = distances[-1] if distances else 0.0
    caption = _build_caption(
        total_len_m=total_len_m,
        n_stations=n,
        per_layer_label=per_layer_label,
        layer_value_extent=layer_value_extent,
        per_layer_units=per_layer_units,
    )
    title = (
        f"Terrain profile ({len(layer_uris)} surfaces)"
        if multi_layer
        else f"Terrain profile -- {per_layer_label[0]}"
    )

    logger.info(
        "compute_terrain_profile dems=%d stations=%d length=%.1fm valid=%d/%d",
        len(layer_uris),
        n,
        total_len_m,
        total_valid,
        len(layer_uris) * n,
    )

    return build_chart_payload(
        vega_lite_spec=spec,
        title=title,
        caption=caption,
        source_layer_uri=layer_uris[0],
        created_turn_id=_created_turn_id,
    )


# ---------------------------------------------------------------------------
# Vega-Lite spec + caption builders
# ---------------------------------------------------------------------------


def _build_profile_spec(
    *,
    rows: list[dict[str, Any]],
    multi_layer: bool,
    units_match: bool,
    y_title: str,
    per_layer_label: list[str],
    per_layer_units: list[str | None],
) -> dict[str, Any]:
    """Build the Vega-Lite v5 line-chart spec for the terrain profile.

    Single-DEM: one line. Multi-DEM with matching units: one shared y-axis with a
    ``color`` encoding on ``surface``. Multi-DEM with differing units: dual
    independent y scales via a layered spec.
    """
    x_enc = {
        "field": "distance_m",
        "type": "quantitative",
        "title": "distance along line (m)",
    }
    tooltip = [
        {"field": "distance_m", "type": "quantitative", "title": "distance (m)", "format": ".1f"},
        {"field": "elevation", "type": "quantitative", "title": y_title},
        {"field": "surface", "type": "nominal", "title": "surface"},
        {"field": "lon", "type": "quantitative", "title": "lon", "format": ".5f"},
        {"field": "lat", "type": "quantitative", "title": "lat", "format": ".5f"},
    ]

    if not multi_layer:
        return {
            "$schema": _VEGA_LITE_V5_SCHEMA,
            "title": "Terrain profile",
            "data": {"values": rows},
            "mark": {"type": "line", "tooltip": True, "point": False},
            "encoding": {
                "x": x_enc,
                "y": {"field": "elevation", "type": "quantitative", "title": y_title},
                "tooltip": tooltip,
            },
            "width": "container",
        }

    if units_match:
        return {
            "$schema": _VEGA_LITE_V5_SCHEMA,
            "title": "Terrain profile",
            "data": {"values": rows},
            "mark": {"type": "line", "tooltip": True, "point": False},
            "encoding": {
                "x": x_enc,
                "y": {"field": "elevation", "type": "quantitative", "title": y_title},
                "color": {"field": "surface", "type": "nominal", "title": "surface"},
                "tooltip": tooltip,
            },
            "width": "container",
        }

    primary_label = per_layer_label[0]
    primary_units = per_layer_units[0] or "elevation"
    secondary_units = next((u for u in per_layer_units[1:] if u), "elevation")
    primary_rows = [r for r in rows if r["surface"] == primary_label]
    secondary_rows = [r for r in rows if r["surface"] != primary_label]
    return {
        "$schema": _VEGA_LITE_V5_SCHEMA,
        "title": "Terrain profile",
        "data": {"values": rows},
        "width": "container",
        "encoding": {"x": x_enc},
        "layer": [
            {
                "data": {"values": primary_rows},
                "mark": {"type": "line", "tooltip": True},
                "encoding": {
                    "x": x_enc,
                    "y": {
                        "field": "elevation",
                        "type": "quantitative",
                        "title": f"{primary_label} ({primary_units})",
                        "axis": {"titleColor": "#5fa8ff"},
                    },
                    "color": {"field": "surface", "type": "nominal", "title": "surface"},
                    "tooltip": tooltip,
                },
            },
            {
                "data": {"values": secondary_rows},
                "mark": {"type": "line", "tooltip": True, "strokeDash": [4, 2]},
                "encoding": {
                    "x": x_enc,
                    "y": {
                        "field": "elevation",
                        "type": "quantitative",
                        "title": secondary_units,
                        "axis": {"titleColor": "#ff9f5f"},
                    },
                    "color": {"field": "surface", "type": "nominal", "title": "surface"},
                    "tooltip": tooltip,
                },
            },
        ],
        "resolve": {"scale": {"y": "independent"}},
    }


def _build_caption(
    *,
    total_len_m: float,
    n_stations: int,
    per_layer_label: list[str],
    layer_value_extent: list[tuple[float, float] | None],
    per_layer_units: list[str | None],
) -> str:
    """One-line caption carrying the computed elevation range / relief."""
    parts = [f"{total_len_m:.0f} m line", f"{n_stations} samples"]
    for i, label in enumerate(per_layer_label):
        extent = layer_value_extent[i] if i < len(layer_value_extent) else None
        if extent is not None:
            lo, hi = extent
            unit = per_layer_units[i] or "m"
            relief = hi - lo
            parts.append(
                f"{label}: {lo:.4g}..{hi:.4g} {unit} (relief {relief:.4g})"
            )
        else:
            parts.append(f"{label}: no data along line")
    return " | ".join(parts)
