"""Atomic tool ``compute_cross_section`` - sample raster value(s) along a line.

Stations sample the NEAREST CELL, so a finer ``n_stations`` adds nothing a coarse
raster lacks; no vertical datum is reconciled, so an overlay needs a shared one.
"""
from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.emission.charts import build_chart_payload

__all__ = [
    "compute_cross_section",
    "CrossSectionError",
]

logger = logging.getLogger("trid3nt_server.tools.processing.compute_cross_section.compute_cross_section")

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
# Error type (typed-error surface)
# ---------------------------------------------------------------------------


# ``error_code`` is one of LINE_INVALID, NO_LAYERS, TOO_MANY_LAYERS,
# LAYER_OPEN_FAILED, DOWNLOAD_FAILED, LINE_REPROJECT_FAILED, LINE_OUTSIDE_RASTER.
class CrossSectionError(RuntimeError):
    """No profile could be produced."""

    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

# Never cached: every call mints a fresh chart_id, and a cached envelope would
# hand the panel a stale one. The raster read is already cached upstream.
_METADATA = AtomicToolMetadata(
    name="compute_cross_section",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    supports_global_query=False,
)


# ---------------------------------------------------------------------------
# Line resolution
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
    """Resolve ``line`` to ``[lon, lat]`` vertices from a GeoJSON LineString,
    Feature, FeatureCollection (its FIRST) or a bare vertex list; else LINE_INVALID.
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

    # A zero-length segment carries no profile information and breaks
    # arc-length interpolation, so consecutive duplicates go.
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
# Raster open helper
# ---------------------------------------------------------------------------


def _open_raster_source(layer_uri: str) -> tuple[Any, bool]:
    """``(bytes-or-path, is_memory)`` for opening ``layer_uri``: staged bytes for
    an ``s3://`` URI, the path itself for a local file.
    """
    # boto3 stages the s3:// bytes because GDAL's own /vsis3/ credential chain
    # does not resolve the instance role in this environment.
    if layer_uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

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
    """``(station lon/lat list, cumulative geodesic distance_m list)``: planar
    interpolation sets WHERE they land, the geodesic sum their DISTANCE labels.
    """
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
# Per-layer sampling
# ---------------------------------------------------------------------------


def _sample_layer(
    layer_uri: str,
    stations_4326: list[tuple[float, float]],
) -> tuple[list[float | None], str | None, int]:
    """Sample one raster at ``stations_4326``, returning ``(values, units,
    n_valid)``; a nodata or out-of-bounds read is None, never a filled value.
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
        # src.sample takes coordinates in the raster's own CRS.
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
        for arr in src.sample(coords, indexes=1):
            v = float(arr[0])
            # v != v is True only for NaN, so the test stays NaN-safe.
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
    """A short label for a layer: the basename without its extension."""
    base = layer_uri.rstrip("/").rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base or layer_uri


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
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
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Sample raster value(s) along a line and chart the cross-section profile.

    Use when the user wants a section view, long profile or transect ALONG a
    line: elevation across a valley, flood depth along a road. The only chart
    keyed on DISTANCE - a distribution or time series is ``generate_chart``.
    Pass ``extra_layer_uris`` (up to 3) to overlay surfaces on the same axis.

    Params:
        layer_uri: primary raster to profile (DEM, depth, head, bathymetry).
        line: GeoJSON LineString / Feature / FeatureCollection or
            ``[[lon,lat],...]`` in EPSG:4326, at least 2 vertices; a drawn line
            from ``request_spatial_input(mode="vector_draw")`` feeds straight in.
        n_stations: sample count along the line, default 200, clamped [2, 2000].
        extra_layer_uris: up to 3 more rasters on the same stations; a station
            no layer covers reads null rather than a filled value.

    Returns a Vega-Lite line chart, x=distance_m, one line per layer.
    """
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

    rows: list[dict[str, Any]] = []
    per_layer_units: list[str | None] = []
    per_layer_label: list[str] = []
    per_layer_valid: list[int] = []
    layer_value_extent: list[tuple[float, float] | None] = []

    for layer_idx, uri in enumerate(layer_uris):
        values, units, n_valid = _sample_layer(uri, stations)
        label = _layer_label(uri)
        # Identical basenames would collapse the colour legend.
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

    distinct_units = {u for u in per_layer_units if u}
    multi_layer = len(layer_uris) > 1
    units_match = len(distinct_units) <= 1
    y_title = next(iter(distinct_units)) if distinct_units else "value"

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
    """The Vega-Lite v5 line-chart spec: one line, or a shared y-axis coloured by
    layer, or two independent y scales when the layers' units differ.
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

    # Differing units take two independent y scales: the first layer on the
    # left axis, the rest on the right, both sharing the x-axis.
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
    """One caption line carrying the computed profile numbers."""
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
