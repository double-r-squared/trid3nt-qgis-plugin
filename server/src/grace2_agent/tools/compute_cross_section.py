"""Atomic tool ``compute_cross_section`` -- sample raster value(s) along a line.

The "draw-a-line, see-a-profile" capability (cross-section / transect / long
profile). Given a polyline and one or more height/depth rasters already in the
Case, it samples each raster at N evenly-spaced stations along the line and
returns the resulting (distance, value) series as a **Vega-Lite v5 line chart**
the web client renders inline (the same chart-emission chat-card path as
``generate_time_series``). x = cumulative geodesic distance along the line in
metres; y = elevation or depth in the raster's native units; one coloured line
per sampled layer (DESIGN CALL B = multi-layer overlay).

This is the canonical hydraulics/terrain "section view" -- ground vs water
surface (freeboard / inundation depth), DEM vs bathymetry (bank-to-channel),
head surface vs land surface (MODFLOW seepage), pre vs post event. A single
terrain profile is commodity; overlaying N surfaces on one shared distance axis
is the differentiator (per ``reports/design/spike_cross_section_profile_tool.md``).

DATA FLOW (the spike's happy path, steps 1-6)
---------------------------------------------
1. Resolve ``line`` to a shapely LineString in EPSG:4326 (accepts a GeoJSON
   LineString, a list of ``[lon, lat]`` vertices, or a FeatureCollection -- the
   last lets the agent feed the drawn ``barriers`` FC from
   ``request_spatial_input`` straight through, OR pass a self-constructed line
   inline with zero user draw).
2. Open each ``layer_uri`` with rasterio (s3:// staged via ``read_object_bytes_s3``
   + ``MemoryFile`` -- the same /vsis3/-credential workaround documented in
   ``clip_raster_to_polygon._get_source_crs``).
3. Interpolate N stations at equal arc-length along the line in lon/lat
   (``line.interpolate``), and compute the cumulative GEODESIC distance from the
   start vertex (``pyproj.Geod``) so the x-axis is metres on the ground, not
   degrees.
4. For each layer: reproject the station coordinates into the raster CRS
   (``rasterio.warp.transform``) and read all stations in ONE vectorized
   ``src.sample(...)`` call; nodata / out-of-bounds -> ``None`` (surfaced
   honestly, never silently dropped -- the honesty floor).
5. Concatenate to a series ``[{distance_m, value, lon, lat, layer}, ...]`` and
   build the Vega-Lite line spec (``color`` encoding on ``layer`` when >1 layer;
   single-y when units match, dual-axis when they differ).
6. Wrap with ``build_chart_payload(...)`` -> a ChartEmissionPayload dict
   (``envelope_type="chart-emission"``) the agent loop emits + persists +
   summarizes for narration, EXACTLY like the four ``chart_tools`` charts.

DETERMINISM (Invariant 2): zero LLM calls, zero randomness -- the profile is a
pure function of (line, layers, n_stations). The numbers the agent narrates are
reproducible from the inputs.

CACHING: ``cacheable=False`` / ``ttl_class="live-no-cache"`` -- the result is a
fresh chart-emission envelope minting a new ``chart_id`` per call (the same
in-process emit pattern as ``chart_tools``; caching would re-use a stale
chart_id). The expensive part -- the raster read -- is already cached upstream
by the fetcher that produced the layer.

LIMITATIONS (honest -- this is a sampler, not a hydraulic solver):
- Stations are sampled by nearest-cell (``src.sample``); for a coarse raster a
  finer ``n_stations`` does not add information the grid does not carry.
- Multi-layer overlay caps at ``_MAX_LAYERS`` layers; layers need not co-cover
  the line -- uncovered stations read ``None`` and the line breaks there.
- No vertical datum reconciliation across layers: if two layers use different
  vertical datums the overlay is only meaningful when they share one (the caller
  owns datum hygiene, same as ``compute_zonal_statistics``).
"""

from __future__ import annotations

import logging
from typing import Any

from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .chart_tools import build_chart_payload

__all__ = [
    "compute_cross_section",
    "CrossSectionError",
]

logger = logging.getLogger("grace2_agent.tools.compute_cross_section")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default number of stations sampled along the line. ~200 is dense enough for a
#: smooth profile while keeping the inline chart data well under chart_tools'
#: _MAX_ROWS row cap (single-layer) and under it for a few overlaid layers.
_DEFAULT_N_STATIONS = 200

#: Hard floor / ceiling on n_stations (a degenerate request must not produce a
#: one-point "profile" or an enormous inline payload).
_MIN_N_STATIONS = 2
_MAX_N_STATIONS = 2000

#: Multi-layer overlay cap (DESIGN CALL B bound). 3-4 surfaces is the readable
#: ceiling for one shared distance axis (ground + water + bathymetry).
_MAX_LAYERS = 4

#: Vega-Lite v5 schema (build_chart_payload sets this too; declared for clarity).
_VEGA_LITE_V5_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"


# ---------------------------------------------------------------------------
# Error type (NFR-R-1 typed-error surface)
# ---------------------------------------------------------------------------


class CrossSectionError(RuntimeError):
    """Raised when ``compute_cross_section`` cannot produce a profile.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code consumed by
    ``summarize_tool_result`` (FR-AS-11 retry surface):

    - ``LINE_INVALID``        -- ``line`` is not a usable LineString (wrong shape,
      < 2 distinct vertices, non-numeric coordinates).
    - ``NO_LAYERS``           -- no ``layer_uri`` (and no ``extra_layer_uris``)
      was supplied.
    - ``TOO_MANY_LAYERS``     -- more than ``_MAX_LAYERS`` layers requested.
    - ``LAYER_OPEN_FAILED``   -- a raster could not be opened with rasterio.
    - ``DOWNLOAD_FAILED``     -- an s3:// download for a layer failed.
    - ``LINE_REPROJECT_FAILED``-- reprojecting the line into a raster CRS failed.
    - ``LINE_OUTSIDE_RASTER`` -- every station fell on nodata / outside EVERY
      layer (the profile would be entirely null -- surfaced, not faked).
    """

    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    # Deterministic in-process emit tool -> never touches the cache shim
    # (mirrors chart_tools' chart_id-per-call rationale and the analytic
    # compute_wave_nomograph live-no-cache choice).
    name="compute_cross_section",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    supports_global_query=False,
)


# ---------------------------------------------------------------------------
# Line resolution -- accept GeoJSON LineString / [lon,lat] list / FeatureCollection
# ---------------------------------------------------------------------------


def _coords_from_geojson_geometry(geom: dict[str, Any]) -> list[list[float]]:
    """Pull a LineString's coordinate list out of a GeoJSON geometry dict."""
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype != "LineString" or not isinstance(coords, list):
        raise CrossSectionError(
            "LINE_INVALID",
            f"line geometry must be a LineString; got type={gtype!r}.",
        )
    return coords  # type: ignore[return-value]


def _resolve_line_coords(line: Any) -> list[list[float]]:
    """Resolve ``line`` to a list of ``[lon, lat]`` vertices.

    Accepts (in priority order):
      1. A GeoJSON ``LineString`` geometry dict ``{"type": "LineString",
         "coordinates": [[lon,lat], ...]}``.
      2. A GeoJSON ``Feature`` wrapping a LineString.
      3. A GeoJSON ``FeatureCollection`` -- the FIRST LineString feature is used
         (so the drawn ``barriers`` FC from ``request_spatial_input`` feeds
         straight through; extra barrier lines are ignored for v1).
      4. A bare list of ``[lon, lat]`` vertices (the agent-derived inline path).

    Raises ``CrossSectionError(LINE_INVALID)`` on any unusable input.
    """
    if line is None:
        raise CrossSectionError("LINE_INVALID", "line is required (got None).")

    coords: list[Any] | None = None

    if isinstance(line, dict):
        gtype = line.get("type")
        if gtype == "LineString":
            coords = _coords_from_geojson_geometry(line)
        elif gtype == "Feature":
            geom = line.get("geometry")
            if not isinstance(geom, dict):
                raise CrossSectionError(
                    "LINE_INVALID", "line Feature carried no geometry dict."
                )
            coords = _coords_from_geojson_geometry(geom)
        elif gtype == "FeatureCollection":
            feats = line.get("features")
            if not isinstance(feats, list) or not feats:
                raise CrossSectionError(
                    "LINE_INVALID",
                    "line FeatureCollection carried no features.",
                )
            for feat in feats:
                geom = feat.get("geometry") if isinstance(feat, dict) else None
                if isinstance(geom, dict) and geom.get("type") == "LineString":
                    coords = _coords_from_geojson_geometry(geom)
                    break
            if coords is None:
                raise CrossSectionError(
                    "LINE_INVALID",
                    "line FeatureCollection contained no LineString feature.",
                )
        else:
            raise CrossSectionError(
                "LINE_INVALID",
                f"line dict has unsupported GeoJSON type={gtype!r}; expected "
                "LineString, Feature, or FeatureCollection.",
            )
    elif isinstance(line, (list, tuple)):
        coords = list(line)
    else:
        raise CrossSectionError(
            "LINE_INVALID",
            f"line must be a GeoJSON LineString/Feature/FeatureCollection or a "
            f"list of [lon, lat] vertices; got {type(line).__name__}.",
        )

    # Validate + coerce each vertex to a float [lon, lat] pair.
    cleaned: list[list[float]] = []
    for i, pt in enumerate(coords):
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            raise CrossSectionError(
                "LINE_INVALID",
                f"line vertex[{i}] must be a [lon, lat] pair; got {pt!r}.",
            )
        try:
            lon = float(pt[0])
            lat = float(pt[1])
        except (TypeError, ValueError) as exc:
            raise CrossSectionError(
                "LINE_INVALID",
                f"line vertex[{i}] has non-numeric coordinates: {pt!r}.",
            ) from exc
        cleaned.append([lon, lat])

    # Drop consecutive duplicate vertices (a zero-length segment carries no
    # profile information and breaks arc-length interpolation).
    deduped: list[list[float]] = []
    for pt in cleaned:
        if not deduped or deduped[-1] != pt:
            deduped.append(pt)

    if len(deduped) < 2:
        raise CrossSectionError(
            "LINE_INVALID",
            "line needs at least 2 distinct vertices to form a profile; got "
            f"{len(deduped)} after de-duplication.",
        )
    return deduped


# ---------------------------------------------------------------------------
# Raster open helper (mirrors clip_raster_to_polygon._get_source_crs staging)
# ---------------------------------------------------------------------------


def _open_raster_source(layer_uri: str) -> tuple[Any, bool]:
    """Return (bytes-or-path, is_memory) for opening ``layer_uri`` with rasterio.

    For ``s3://`` URIs the bytes are staged via the shared boto3 reader (GDAL's
    /vsis3/ credential chain does not resolve the EC2 instance role in this env;
    boto3 does -- the documented clip_raster_to_polygon workaround). Returns the
    raw bytes for an s3:// URI (open via MemoryFile) or the local path for a file.
    """
    if layer_uri.startswith("s3://"):
        from .cache import read_object_bytes_s3

        try:
            return read_object_bytes_s3(layer_uri), True
        except Exception as exc:  # noqa: BLE001
            raise CrossSectionError(
                "DOWNLOAD_FAILED",
                f"S3 download failed for {layer_uri!r}: {exc}",
                retryable=True,
            ) from exc
    import os

    if os.path.isfile(layer_uri):
        return layer_uri, False
    raise CrossSectionError(
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
    """Return (station lon/lat list, cumulative geodesic distance_m list).

    Stations are equally spaced by PLANAR arc-length along the densified line in
    lon/lat (``shapely.LineString.interpolate``), then the cumulative GEODESIC
    distance from the start vertex is computed with ``pyproj.Geod`` so the x-axis
    is metres on the ground (a 1-degree step is ~111 km near the equator but
    shrinks toward the poles; planar interpolation only sets WHERE the stations
    land, geodesic sets their DISTANCE labels).
    """
    from shapely.geometry import LineString

    geom = LineString(coords)
    total_len = geom.length
    stations: list[tuple[float, float]] = []
    for i in range(n_stations):
        frac = i / (n_stations - 1) if n_stations > 1 else 0.0
        pt = geom.interpolate(frac * total_len)
        stations.append((pt.x, pt.y))

    # Cumulative geodesic distance: sum the geodesic length of each prefix
    # segment. WGS84 ellipsoid.
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
# Per-layer sampling
# ---------------------------------------------------------------------------


def _sample_layer(
    layer_uri: str,
    stations_4326: list[tuple[float, float]],
) -> tuple[list[float | None], str | None, int]:
    """Sample one raster at ``stations_4326`` (lon/lat). Return (values, units, n_valid).

    Reprojects the stations into the raster CRS, reads band 1 at every station in
    one vectorized ``src.sample`` call, and maps nodata / out-of-bounds reads to
    ``None`` (honesty floor -- never a fabricated value). ``units`` is read from
    the raster's band-1 units tag when present.
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
        # Reproject stations into the raster CRS so src.sample reads the right
        # cells (src.sample takes coordinates in the raster's own CRS).
        if raster_crs is not None and raster_crs.to_epsg() != 4326:
            try:
                xs, ys = transform("EPSG:4326", raster_crs, xs, ys)
            except Exception as exc:  # noqa: BLE001
                raise CrossSectionError(
                    "LINE_REPROJECT_FAILED",
                    f"reprojecting stations into {raster_crs} for {layer_uri!r} "
                    f"failed: {exc}",
                ) from exc

        coords = list(zip(xs, ys))
        values: list[float | None] = []
        n_valid = 0
        # src.sample is a generator yielding a band-array per coordinate.
        for arr in src.sample(coords, indexes=1):
            v = float(arr[0])
            # NaN-safe nodata test: ``v != v`` is True only for NaN.
            is_nan = v != v
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
    except CrossSectionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CrossSectionError(
            "LAYER_OPEN_FAILED",
            f"rasterio could not sample {layer_uri!r}: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Layer label helper
# ---------------------------------------------------------------------------


def _layer_label(layer_uri: str) -> str:
    """A short human label for a layer (the basename without extension)."""
    base = layer_uri.rstrip("/").rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base or layer_uri


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # readOnlyHint=True (samples input rasters; emits a chart, no side effects),
    # openWorldHint=False (local GDAL read, no external API), destructiveHint=False,
    # idempotentHint=True (deterministic: same line+layers -> same profile).
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def compute_cross_section(
    layer_uri: str,
    line: Any,
    n_stations: int = _DEFAULT_N_STATIONS,
    extra_layer_uris: list[str] | None = None,
    *,
    _created_turn_id: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Sample raster value(s) along a line and chart the cross-section profile.

    Use this when the user wants the "section view" / "long profile" / "transect"
    of a surface ALONG a drawn or derived line -- "show me the elevation profile
    across this valley", "plot the flood depth along the road", "draw a section
    of the ground and water surface". This is the only chart keyed on DISTANCE
    along a line (vs generate_time_series = over TIME, generate_histogram =
    DISTRIBUTION of values).

    DESIGN CALL B (multi-layer overlay): pass ``extra_layer_uris`` to overlay
    several surfaces on the SAME line and the SAME distance axis -- ground vs
    water surface (freeboard / inundation depth), DEM vs bathymetry, head vs land
    surface (MODFLOW seepage), pre vs post event. Each layer is a coloured line
    in one chart. Cap of 4 layers.

    The line input (any of):
        - A user-DRAWN line: call ``request_spatial_input(mode="vector_draw")``,
          have the user draw a single LineString, then pass the returned
          ``barriers`` FeatureCollection (or its first LineString) as ``line`` --
          the FIRST LineString in an FC is used.
        - An agent-DERIVED line: construct two endpoints (or a perpendicular to a
          river / valley axis) yourself and pass them inline as a GeoJSON
          LineString or a ``[[lon,lat], [lon,lat], ...]`` list -- no user draw
          needed.

    Do NOT use this for: a value distribution (use generate_histogram); a value
    over time (use generate_time_series); a single number for the whole line
    (use compute_zonal_statistics with a buffered line); rendering the line on
    the map (use publish_layer).

    Parameters:
        layer_uri: s3:// URI or local path of the PRIMARY raster (DEM, flood
            depth, head surface, bathymetry COG, ...) to profile.
        line: the section line, as a GeoJSON LineString / Feature /
            FeatureCollection, or a list of ``[lon, lat]`` vertices (EPSG:4326,
            lon-first). At least 2 distinct vertices.
        n_stations: number of evenly-spaced stations sampled along the line
            (default 200; clamped to [2, 2000]).
        extra_layer_uris: OPTIONAL up to 3 additional raster URIs to overlay on
            the same line (multi-layer profile). Sampled over the SAME stations;
            uncovered stations read null and that layer's line breaks there.

    Returns:
        A ChartEmissionPayload dict (``envelope_type="chart-emission"``) carrying
        a Vega-Lite v5 line chart (x = distance_m, y = value, one coloured line
        per layer when >1), a title, and a caption with the profile drop / range.
        The agent loop emits this as a chart-emission envelope and feeds a compact
        summary back for narration.

    Raises:
        CrossSectionError: typed error_code (LINE_INVALID, NO_LAYERS,
            TOO_MANY_LAYERS, LAYER_OPEN_FAILED, DOWNLOAD_FAILED,
            LINE_REPROJECT_FAILED, LINE_OUTSIDE_RASTER).
    """
    # ---- validate inputs ---------------------------------------------------
    if not isinstance(layer_uri, str) or not layer_uri.strip():
        raise CrossSectionError(
            "NO_LAYERS", f"layer_uri must be a non-empty URI string; got {layer_uri!r}."
        )
    layer_uris: list[str] = [layer_uri.strip()]
    if extra_layer_uris:
        if not isinstance(extra_layer_uris, (list, tuple)):
            raise CrossSectionError(
                "NO_LAYERS",
                f"extra_layer_uris must be a list of URI strings; got "
                f"{type(extra_layer_uris).__name__}.",
            )
        for u in extra_layer_uris:
            if isinstance(u, str) and u.strip():
                layer_uris.append(u.strip())
    if len(layer_uris) > _MAX_LAYERS:
        raise CrossSectionError(
            "TOO_MANY_LAYERS",
            f"at most {_MAX_LAYERS} layers may be overlaid; got {len(layer_uris)}.",
        )

    try:
        n = int(n_stations)
    except (TypeError, ValueError):
        n = _DEFAULT_N_STATIONS
    n = max(_MIN_N_STATIONS, min(_MAX_N_STATIONS, n))

    coords = _resolve_line_coords(line)
    stations, distances = _interpolate_stations(coords, n)

    # ---- sample every layer over the SAME stations -------------------------
    rows: list[dict[str, Any]] = []
    per_layer_units: list[str | None] = []
    per_layer_label: list[str] = []
    per_layer_valid: list[int] = []
    layer_value_extent: list[tuple[float, float] | None] = []

    for layer_idx, uri in enumerate(layer_uris):
        values, units, n_valid = _sample_layer(uri, stations)
        label = _layer_label(uri)
        # Disambiguate identical basenames so the color legend stays 1:1.
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
                    "value": values[station_idx],
                    "lon": round(float(lon), 6),
                    "lat": round(float(lat), 6),
                    "layer": label,
                }
            )

    total_valid = sum(per_layer_valid)
    if total_valid == 0:
        raise CrossSectionError(
            "LINE_OUTSIDE_RASTER",
            "every station fell on nodata or outside ALL supplied rasters -- the "
            "line does not cross any layer's covered extent. Check the line "
            "location and the layer CRS/coverage; do not fabricate a profile.",
            retryable=False,
        )

    # ---- units handling: single-y when units match, dual-axis otherwise ----
    distinct_units = {u for u in per_layer_units if u}
    multi_layer = len(layer_uris) > 1
    units_match = len(distinct_units) <= 1
    y_title = next(iter(distinct_units)) if distinct_units else "value"

    # ---- build the Vega-Lite line spec -------------------------------------
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
        f"Cross-section profile ({len(layer_uris)} layers)"
        if multi_layer
        else f"Cross-section profile -- {per_layer_label[0]}"
    )

    logger.info(
        "compute_cross_section layers=%d stations=%d length=%.1fm valid=%d/%d",
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
    """Build the Vega-Lite v5 line-chart spec for the profile.

    Same line-chart shape as ``generate_time_series`` with a distance x-axis.
    Single-layer: one line. Multi-layer with matching units: one shared y-axis,
    a ``color`` encoding on ``layer``. Multi-layer with DIFFERING units: a
    dual-axis ``layer`` (two independent y scales) -- the spike's units fallback.
    """
    x_enc = {
        "field": "distance_m",
        "type": "quantitative",
        "title": "distance along line (m)",
    }
    tooltip = [
        {"field": "distance_m", "type": "quantitative", "title": "distance (m)", "format": ".1f"},
        {"field": "value", "type": "quantitative", "title": y_title},
        {"field": "layer", "type": "nominal", "title": "layer"},
        {"field": "lon", "type": "quantitative", "title": "lon", "format": ".5f"},
        {"field": "lat", "type": "quantitative", "title": "lat", "format": ".5f"},
    ]

    if not multi_layer:
        return {
            "$schema": _VEGA_LITE_V5_SCHEMA,
            "title": "Cross-section profile",
            "data": {"values": rows},
            "mark": {"type": "line", "tooltip": True, "point": False},
            "encoding": {
                "x": x_enc,
                "y": {"field": "value", "type": "quantitative", "title": y_title},
                "tooltip": tooltip,
            },
            "width": "container",
        }

    if units_match:
        # One shared y-axis; color distinguishes the overlaid layers.
        return {
            "$schema": _VEGA_LITE_V5_SCHEMA,
            "title": "Cross-section profile",
            "data": {"values": rows},
            "mark": {"type": "line", "tooltip": True, "point": False},
            "encoding": {
                "x": x_enc,
                "y": {"field": "value", "type": "quantitative", "title": y_title},
                "color": {"field": "layer", "type": "nominal", "title": "layer"},
                "tooltip": tooltip,
            },
            "width": "container",
        }

    # Differing units -> dual independent y scales via a layered spec. Split the
    # first layer (left axis) from the rest (right axis); both share the x-axis.
    primary_label = per_layer_label[0]
    primary_units = per_layer_units[0] or "value"
    secondary_units = next((u for u in per_layer_units[1:] if u), "value")
    primary_rows = [r for r in rows if r["layer"] == primary_label]
    secondary_rows = [r for r in rows if r["layer"] != primary_label]
    return {
        "$schema": _VEGA_LITE_V5_SCHEMA,
        "title": "Cross-section profile",
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
                        "field": "value",
                        "type": "quantitative",
                        "title": f"{primary_label} ({primary_units})",
                        "axis": {"titleColor": "#5fa8ff"},
                    },
                    "color": {"field": "layer", "type": "nominal", "title": "layer"},
                    "tooltip": tooltip,
                },
            },
            {
                "data": {"values": secondary_rows},
                "mark": {"type": "line", "tooltip": True, "strokeDash": [4, 2]},
                "encoding": {
                    "x": x_enc,
                    "y": {
                        "field": "value",
                        "type": "quantitative",
                        "title": secondary_units,
                        "axis": {"titleColor": "#ff9f5f"},
                    },
                    "color": {"field": "layer", "type": "nominal", "title": "layer"},
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
    """One-line caption carrying the computed profile numbers (determinism boundary)."""
    parts = [f"{total_len_m:.0f} m line", f"{n_stations} stations"]
    for i, label in enumerate(per_layer_label):
        extent = layer_value_extent[i] if i < len(layer_value_extent) else None
        if extent is not None:
            lo, hi = extent
            unit = per_layer_units[i] or ""
            drop = hi - lo
            unit_suffix = f" {unit}" if unit else ""
            parts.append(
                f"{label}: {lo:.3g}..{hi:.3g}{unit_suffix} (range {drop:.3g})"
            )
        else:
            parts.append(f"{label}: no data along line")
    return " | ".join(parts)
