"""Atomic tool ``compute_zonal_statistics`` — hazard-analysis primitive (job-0083, FR-TA-2, FR-CE-8, FR-DC).

This module registers one atomic tool that computes zonal statistics: aggregating
values from a raster within zones defined by either a raster mask or vector polygons.

    ``compute_zonal_statistics(value_raster_uri, zone_input_uri, statistics, ...) → dict``

**Zone input auto-detection:**

- Extension ``.tif`` / ``.tiff`` → raster zone (opens via ``rasterio``).
- Extension ``.fgb``, ``.geojson``, ``.gpkg``, ``.shp``, ``.json`` → vector zone.
- For any other extension, rasterio is tried first; on failure, vector reading is tried.

**Raster zone path:**

Non-zero pixels are "in zone". When ``zone_threshold`` is provided, pixels where
``zone_value >= zone_threshold`` are "in zone" (useful for flood-depth thresholds).

**Vector zone path:**

Each polygon feature is one zone. The tool rasterizes each feature using
``rasterio.features.rasterize`` onto a grid matching the value raster, then
computes per-polygon stats. A whole-area aggregate (union of all zones) is also
computed. Zone IDs default to the feature's ``id`` property if present, else the
sequential feature index.

**No rasterstats dependency:**

We roll our own aggregation with ``rasterio`` + ``numpy``. The venv does not include
``rasterstats`` (not in pyproject.toml; ``rasterstats`` is an acceptable addition if
the orchestrator wants cleaner code — see Open Questions in report).

**Cache:** result dict serialized as JSON in the cache bucket at
    ``cache/dynamic-1h/zonal_statistics/<key>.json``

**Cross-cutting invariants:**

- **Invariant 2 (Deterministic workflows): preserves.** Pure rasterio + numpy
  pipeline, no LLM calls, deterministic given inputs.
- **FR-DC-6 (cacheable): honors.** ``cacheable=True``, ``ttl_class="dynamic-1h"``,
  ``source_class="zonal_statistics"``.
- **Claims carry provenance (Invariant 7): preserves.** Result dict carries
  ``value_raster``, ``zone_input``, ``computed_at`` ISO timestamp.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Literal

import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.transform import from_bounds

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import CACHE_BUCKET, read_through

__all__ = [
    "compute_zonal_statistics",
    "ZonalStatisticsError",
]

logger = logging.getLogger("trid3nt_server.tools.compute_zonal_statistics")

# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class ZonalStatisticsError(RuntimeError):
    """Raised when zonal statistics computation fails.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code surfaced in the
    pipeline strip (NFR-R-1 typed-error requirement).

    Codes:
    - ``RASTER_OPEN_FAILED`` — value or zone raster could not be opened.
    - ``VECTOR_OPEN_FAILED`` — zone vector file could not be opened.
    - ``ZONE_RASTER_CRS_MISMATCH`` — raster CRS mismatch requiring reprojection
      (not supported in this version; caller must reproject before calling).
    - ``DOWNLOAD_FAILED`` — GCS download for a URI failed.
    - ``NO_VALID_PIXELS`` — the masked zone contains no valid pixels to aggregate.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_COMPUTE_ZONAL_STATS_METADATA = AtomicToolMetadata(
    name="compute_zonal_statistics",
    ttl_class="dynamic-1h",
    source_class="zonal_statistics",
    cacheable=True,
)

# ---------------------------------------------------------------------------
# Statistic computation helpers
# ---------------------------------------------------------------------------

_SUPPORTED_STATS = Literal[
    "count",
    "sum",
    "mean",
    "min",
    "max",
    "std",
    "median",
    "percentile_25",
    "percentile_75",
    "percentile_95",
]


def _compute_stats(
    values: np.ndarray,
    stats: list[str],
) -> dict[str, float | int | None]:
    """Compute the requested statistics over a 1-D array of valid pixel values.

    ``values`` is a 1-D float32/float64 array of VALID (non-nodata) pixels.
    Returns a dict mapping stat name → value (or None if values is empty).
    """
    if values.size == 0:
        return {s: None for s in stats}

    result: dict[str, float | int | None] = {}
    for stat in stats:
        if stat == "count":
            result[stat] = int(values.size)
        elif stat == "sum":
            result[stat] = float(np.sum(values))
        elif stat == "mean":
            result[stat] = float(np.mean(values))
        elif stat == "min":
            result[stat] = float(np.min(values))
        elif stat == "max":
            result[stat] = float(np.max(values))
        elif stat == "std":
            result[stat] = float(np.std(values))
        elif stat == "median":
            result[stat] = float(np.median(values))
        elif stat == "percentile_25":
            result[stat] = float(np.percentile(values, 25))
        elif stat == "percentile_75":
            result[stat] = float(np.percentile(values, 75))
        elif stat == "percentile_95":
            result[stat] = float(np.percentile(values, 95))
        else:
            result[stat] = None  # unknown stat: return None defensively
    return result


# ---------------------------------------------------------------------------
# Zone type detection
# ---------------------------------------------------------------------------

_RASTER_EXTENSIONS = {".tif", ".tiff", ".img", ".vrt", ".nc"}
_VECTOR_EXTENSIONS = {".fgb", ".geojson", ".gpkg", ".shp", ".json", ".gml", ".kml"}


def _detect_zone_type(uri: str) -> str:
    """Return ``"raster"`` or ``"vector"`` for the given URI / path.

    Detection order:
    1. Extension lookup (fast, deterministic for known formats).
    2. Try rasterio.open() — if it succeeds, raster.
    3. Else: vector.
    """
    ext = os.path.splitext(uri.split("?")[0].rstrip("/"))[-1].lower()
    if ext in _RASTER_EXTENSIONS:
        return "raster"
    if ext in _VECTOR_EXTENSIONS:
        return "vector"

    # Fallback: try rasterio
    try:
        with rasterio.open(uri):
            return "raster"
    except Exception:  # noqa: BLE001
        return "vector"


# ---------------------------------------------------------------------------
# Object-store download helper (S3-only; GCP decommissioned)
# ---------------------------------------------------------------------------


def _download_uri_bytes(uri: str, storage_client: object | None = None) -> bytes:
    """Download bytes from an ``s3://`` URI or read a local path.

    GCP is decommissioned: object-store reads route through boto3 (S3).
    ``storage_client`` is retained for backward-compatible call signatures
    but is ignored.
    """
    del storage_client  # GCP decommissioned — S3/local only.
    # sprint-14-aws (job-0290b): s3:// staging via the shared boto3 reader.
    if uri.startswith("s3://"):
        from .cache import read_object_bytes_s3
        try:
            return read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise ZonalStatisticsError(
                "DOWNLOAD_FAILED",
                f"S3 download failed for {uri!r}: {exc}",
            ) from exc
    try:
        with open(uri, "rb") as f:
            return f.read()
    except OSError as exc:
        raise ZonalStatisticsError(
            "DOWNLOAD_FAILED",
            f"Could not read local path {uri!r}: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Raster zone aggregation
# ---------------------------------------------------------------------------


def _zonal_stats_raster_zone(
    value_path: str,
    zone_path: str,
    stats: list[str],
    zone_threshold: float | None,
    nodata_value: float | None,
) -> dict[str, Any]:
    """Compute stats for a raster-zone input.

    The zone raster is reprojected/resampled to match the value raster's grid
    and CRS if they differ (different dimensions, transform, or CRS). This
    handles the common case where the zone mask was created at a different
    resolution or extent than the value raster.

    Returns the ``aggregate`` dict; ``by_zone`` is empty for raster-zone calls
    (single binary mask = single zone).
    """
    try:
        with rasterio.open(value_path) as val_src:
            val_data = val_src.read(1).astype(np.float64)
            val_nodata = nodata_value if nodata_value is not None else val_src.nodata
            val_transform = val_src.transform
            val_crs = val_src.crs
            val_height = val_src.height
            val_width = val_src.width
            val_units = (
                val_src.tags().get("units")
                or (val_src.units[0] if val_src.units else None)
            )
    except Exception as exc:  # noqa: BLE001
        raise ZonalStatisticsError(
            "RASTER_OPEN_FAILED",
            f"Could not open value raster {value_path!r}: {exc}",
        ) from exc

    try:
        with rasterio.open(zone_path) as zone_src:
            zone_raw = zone_src.read(1).astype(np.float64)
            zone_transform = zone_src.transform
            zone_crs = zone_src.crs
            zone_height = zone_src.height
            zone_width = zone_src.width
    except Exception as exc:  # noqa: BLE001
        raise ZonalStatisticsError(
            "RASTER_OPEN_FAILED",
            f"Could not open zone raster {zone_path!r}: {exc}",
        ) from exc

    # Reproject/resample zone raster onto the value raster grid if they differ.
    grids_match = (
        zone_height == val_height
        and zone_width == val_width
        and zone_transform == val_transform
        and zone_crs == val_crs
    )
    if not grids_match:
        logger.info(
            "compute_zonal_statistics: zone raster grid differs from value raster "
            "(zone %dx%d vs value %dx%d); reprojecting zone to value grid.",
            zone_width,
            zone_height,
            val_width,
            val_height,
        )
        try:
            from rasterio.warp import reproject, Resampling

            zone_aligned = np.zeros((val_height, val_width), dtype=np.float64)
            with rasterio.open(zone_path) as zone_src:
                reproject(
                    source=zone_src.read(1).astype(np.float64),
                    destination=zone_aligned,
                    src_transform=zone_transform,
                    src_crs=zone_crs,
                    dst_transform=val_transform,
                    dst_crs=val_crs,
                    resampling=Resampling.nearest,
                )
            zone_data = zone_aligned
        except Exception as exc:  # noqa: BLE001
            raise ZonalStatisticsError(
                "RASTER_OPEN_FAILED",
                f"Could not reproject zone raster to value raster grid: {exc}",
            ) from exc
    else:
        zone_data = zone_raw

    # Build the zone mask: True = in zone.
    if zone_threshold is not None:
        zone_mask = zone_data >= zone_threshold
    else:
        zone_mask = zone_data != 0

    # Mask out nodata in the value raster.
    if val_nodata is not None:
        # Handle NaN nodata (common for float rasters).
        import math
        if isinstance(val_nodata, float) and math.isnan(val_nodata):
            valid_mask = ~np.isnan(val_data)
        else:
            valid_mask = val_data != val_nodata
    else:
        valid_mask = np.ones_like(val_data, dtype=bool)

    # Also mask out NaN regardless (belt + suspenders).
    valid_mask = valid_mask & ~np.isnan(val_data)

    # Combined mask: in-zone AND valid.
    combined = zone_mask & valid_mask
    pixel_values = val_data[combined]

    aggregate = _compute_stats(pixel_values, stats)

    logger.info(
        "compute_zonal_statistics raster-zone: in-zone pixels=%d stats=%s",
        int(np.sum(combined)),
        list(stats),
    )

    return {
        "by_zone": {},
        "aggregate": aggregate,
        "units": val_units,
    }


# ---------------------------------------------------------------------------
# Vector zone aggregation
# ---------------------------------------------------------------------------


def _zonal_stats_vector_zone(
    value_path: str,
    zone_path: str,
    stats: list[str],
    nodata_value: float | None,
) -> dict[str, Any]:
    """Compute per-polygon stats for a vector-zone input.

    Uses ``rasterio.features.rasterize`` to burn each polygon to a mask
    matching the value raster grid, then aggregates. For large vector files
    this is feature-by-feature (memory-efficient).

    ``by_zone`` keys are the feature's ``id`` field if present, else the
    sequential zero-based index stringified.

    Returns ``by_zone`` (per-polygon) and ``aggregate`` (all in-zone pixels).
    """
    try:
        with rasterio.open(value_path) as val_src:
            val_data = val_src.read(1).astype(np.float64)
            val_nodata = nodata_value if nodata_value is not None else val_src.nodata
            val_transform = val_src.transform
            val_shape = (val_src.height, val_src.width)
            val_units = (
                val_src.tags().get("units")
                or (val_src.units[0] if val_src.units else None)
            )
    except Exception as exc:  # noqa: BLE001
        raise ZonalStatisticsError(
            "RASTER_OPEN_FAILED",
            f"Could not open value raster {value_path!r}: {exc}",
        ) from exc

    # Nodata mask for value raster: True = valid.
    import math as _math
    if val_nodata is not None and not (isinstance(val_nodata, float) and _math.isnan(val_nodata)):
        val_valid = (val_data != val_nodata) & ~np.isnan(val_data)
    else:
        # nodata is NaN or None: just mask NaN pixels.
        val_valid = ~np.isnan(val_data)

    # Read vector features.
    try:
        features = _read_vector_features(zone_path)
    except ZonalStatisticsError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ZonalStatisticsError(
            "VECTOR_OPEN_FAILED",
            f"Could not open zone vector {zone_path!r}: {exc}",
        ) from exc

    by_zone: dict[str, dict[str, float | int | None]] = {}
    all_in_zone_pixels: list[np.ndarray] = []

    for feat_idx, (zone_id, geometry) in enumerate(features):
        # Rasterize this single polygon onto the value raster grid.
        try:
            burned = rasterize(
                [(geometry, 1)],
                out_shape=val_shape,
                transform=val_transform,
                fill=0,
                dtype=np.uint8,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "compute_zonal_statistics: rasterize failed for zone %s (idx=%d): %s",
                zone_id,
                feat_idx,
                exc,
            )
            by_zone[str(zone_id)] = {s: None for s in stats}
            continue

        zone_mask = burned == 1
        combined = zone_mask & val_valid
        pixel_values = val_data[combined]

        by_zone[str(zone_id)] = _compute_stats(pixel_values, stats)
        if pixel_values.size > 0:
            all_in_zone_pixels.append(pixel_values)

    # Aggregate across all zones.
    if all_in_zone_pixels:
        all_pixels = np.concatenate(all_in_zone_pixels)
    else:
        all_pixels = np.array([], dtype=np.float64)

    aggregate = _compute_stats(all_pixels, stats)

    logger.info(
        "compute_zonal_statistics vector-zone: zones=%d total_in_zone_pixels=%d",
        len(by_zone),
        int(all_pixels.size),
    )

    return {
        "by_zone": by_zone,
        "aggregate": aggregate,
        "units": val_units,
    }


def _read_vector_features(path: str) -> list[tuple[str | int, Any]]:
    """Read vector features from a local file path.

    Returns a list of ``(zone_id, geometry_dict)`` tuples where
    ``geometry_dict`` is a GeoJSON-style dict suitable for
    ``rasterio.features.rasterize``.

    Supports GeoJSON and FlatGeobuf natively via rasterio's vector reading
    path; any OGR-readable format works.

    The ``id`` property of each feature is used as the zone ID when available;
    otherwise the sequential feature index is used.
    """
    # Try reading as a JSON/GeoJSON file first (common for synthetic + test cases).
    if path.endswith(".geojson") or path.endswith(".json"):
        try:
            return _read_geojson_features(path)
        except Exception as exc:  # noqa: BLE001
            raise ZonalStatisticsError(
                "VECTOR_OPEN_FAILED",
                f"Could not parse GeoJSON {path!r}: {exc}",
            ) from exc

    # Fall back to rasterio Dataset (supports .fgb, .gpkg, .shp, etc.).
    return _read_ogr_features(path)


def _read_geojson_features(path: str) -> list[tuple[str | int, Any]]:
    """Read features from a GeoJSON file."""
    with open(path) as f:
        fc = json.load(f)

    features_out = []
    feats = fc.get("features", [fc]) if fc.get("type") == "FeatureCollection" else [fc]

    for idx, feat in enumerate(feats):
        geom = feat.get("geometry") if feat.get("type") == "Feature" else feat
        props = feat.get("properties") or {}
        zone_id = props.get("id", idx)
        features_out.append((zone_id, geom))

    return features_out


def _read_ogr_features(path: str) -> list[tuple[str | int, Any]]:
    """Read features from an OGR-compatible vector file via rasterio."""
    # Use rasterio's built-in vector support (GDAL/OGR).
    import rasterio.features

    try:
        with rasterio.open(path) as src:
            # rasterio can open vector files in some configurations; if not,
            # fall through to the shapefile/gpkg manual path.
            pass
    except Exception:  # noqa: BLE001
        pass

    # Use GDAL via osgeo if available, else raise.
    try:
        from osgeo import ogr  # type: ignore[import-not-found]
        ds = ogr.Open(path)
        if ds is None:
            raise ZonalStatisticsError(
                "VECTOR_OPEN_FAILED",
                f"GDAL/OGR could not open {path!r}",
            )
        layer = ds.GetLayer(0)
        features_out = []
        for idx in range(layer.GetFeatureCount()):
            feat = layer.GetFeature(idx)
            geom_json = feat.GetGeometryRef().ExportToJson()
            geom = json.loads(geom_json)
            props = {
                feat.GetFieldDefnRef(i).GetName(): feat.GetField(i)
                for i in range(feat.GetFieldCount())
            }
            zone_id = props.get("id", idx)
            features_out.append((zone_id, geom))
        return features_out
    except ImportError:
        pass

    # Last resort: try fiona if available.
    try:
        import fiona  # type: ignore[import-not-found]
        features_out = []
        with fiona.open(path) as src:
            for idx, feat in enumerate(src):
                props = dict(feat.get("properties") or {})
                zone_id = props.get("id", idx)
                features_out.append((zone_id, feat["geometry"]))
        return features_out
    except ImportError:
        pass

    raise ZonalStatisticsError(
        "VECTOR_OPEN_FAILED",
        f"No vector reading backend available for {path!r}. "
        "Install osgeo.ogr (GDAL) or fiona to read non-GeoJSON vector formats.",
    )


# ---------------------------------------------------------------------------
# Cache-key derivation
# ---------------------------------------------------------------------------


def _derive_cache_key(
    value_raster_uri: str,
    zone_input_uri: str,
    statistics: list[str],
    zone_threshold: float | None,
    nodata_value: float | None,
) -> str:
    """Derive a stable 32-hex-char SHA-256 cache key for this call.

    Independent of the TTL shim's ``compute_cache_key`` because this tool
    caches a computed result (dict) rather than a downloaded artifact —
    the key encodes ALL parameters that affect the output.
    """
    payload = json.dumps(
        {
            "value_raster_uri": value_raster_uri,
            "zone_input_uri": zone_input_uri,
            "statistics": sorted(statistics),
            "zone_threshold": zone_threshold,
            "nodata_value": nodata_value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


@register_tool(
    _COMPUTE_ZONAL_STATS_METADATA,
    # Annotations: readOnlyHint=True (reads input raster/vector; writes cache
    # artifact only via the read-through shim), openWorldHint=False (all
    # computation is local GDAL/numpy; no external API calls),
    # destructiveHint=False, idempotentHint=True (deterministic transform;
    # same inputs always produce the same output pixels).
)
def compute_zonal_statistics(
    value_raster_uri: str,
    zone_input_uri: str,
    statistics: list[
        Literal[
            "count",
            "sum",
            "mean",
            "min",
            "max",
            "std",
            "median",
            "percentile_25",
            "percentile_75",
            "percentile_95",
        ]
    ] = ["count", "sum", "mean", "max"],
    zone_threshold: float | None = None,
    nodata_value: float | None = None,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Compute zonal statistics — aggregate values from a raster within zones.

    Common uses:
        - Population in flood zone: value=population_raster, zone=flood_depth_raster, zone_threshold=0.5 (>=0.5m)
        - Mean elevation in watershed: value=DEM, zone=watershed_polygon
        - Building footprint area exposed: value=buildings_raster, zone=hazard_raster
        - Max wind in damage assessment area: value=wind_raster, zone=admin_boundary

    Use this whenever the user asks "how much" / "how many" / "what's the average"
    of one quantity within a zone defined by another. For hazard exposure:
    value=hazard intensity, zone=admin/asset/exposure layer. For impact:
    value=population/buildings/assets, zone=hazard threshold.

    Do NOT use this for: rendering or visualization (use compute_colored_relief);
    creating a slope/hillshade derivative (use compute_slope/compute_hillshade);
    anything that needs spatial outputs rather than statistical summaries.

    Parameters:
        value_raster_uri: gs:// URI or local path of a single-band raster
            whose values are aggregated (e.g. flood depth COG, population
            density raster, building value raster).
        zone_input_uri: gs:// URI or local path of either:
            - a raster (non-zero = in zone, or apply zone_threshold);
            - a vector file (FlatGeobuf/GeoJSON/GPKG polygons; each feature = one zone).
            Auto-detected by file extension.
        statistics: list of summary statistics to compute. Supported values:
            count, sum, mean, min, max, std, median,
            percentile_25, percentile_75, percentile_95.
            Defaults to [count, sum, mean, max].
        zone_threshold: for raster zone inputs only — treat pixels where
            zone_value >= zone_threshold as "in zone". Useful for flood-depth
            thresholds (e.g. 0.5m). If None, non-zero = in zone.
        nodata_value: explicit nodata value for the value raster. Overrides
            the raster's internal nodata metadata. Pass None to use the
            raster's own nodata tag.

    Returns:
        dict with structure:
            {
              "by_zone": {<zone_id>: {<stat>: value, ...}, ...},  # per-zone (vector zones only; empty for raster zones)
              "aggregate": {<stat>: value, ...},                  # whole-area aggregate
              "value_raster": str,                                # provenance: value raster URI
              "zone_input": str,                                  # provenance: zone input URI
              "computed_at": str,                                 # ISO 8601 timestamp
              "units": str | None,                               # propagated from value raster tags if present
            }

    LLM guidance:
        - For hazard exposure questions ("how many people are in the flood zone"),
          set value=population raster, zone=flood extent, zone_threshold=0 or 0.5m.
        - For impact severity, set value=hazard intensity, zone=admin boundary polygon.
        - "by_zone" is only populated when zone_input is a vector; check its keys
          to see per-polygon breakdown vs the aggregate.

    FR-CE-8: Results are cached via read_through. TTL is dynamic-1h because
    both rasters are external and may update within a session.

    Cross-tool dependencies:
        Upstream (consumes):
        - ``fetch_dem`` / ``compute_slope`` / ``compute_aspect`` / ``compute_hillshade`` /
          ``compute_colored_relief`` / ``compute_impervious_surface`` — any of these
          produce a ``LayerURI`` suitable as ``value_raster_uri``.
        - ``postprocess_flood`` (via ``run_model_flood_scenario``) — flood-depth
          COG ``LayerURI`` is a primary ``value_raster_uri`` for exposure analysis.
        - ``fetch_wdpa_protected_areas`` / ``fetch_gbif_occurrences`` /
          ``fetch_administrative_boundaries`` — supply the ``zone_input_uri``
          polygon layer for per-protected-area or per-admin-unit aggregation.
        - ``clip_raster_to_polygon`` / ``clip_raster_to_bbox`` — trim inputs to a
          study area before passing to this tool.
        Downstream (feeds):
        - ``run_model_flood_habitat_scenario`` — calls this to compute flood
          impact metrics within WDPA protected-area polygons.
        - Agent narration and ``AssessmentEnvelope`` ``impact_metrics`` fields
          consume the returned ``aggregate`` dict for headline numbers.

    Raises:
        ZonalStatisticsError: with a typed error_code if raster/vector reading
            fails, GCS download fails, or inputs are incompatible.
    """
    effective_bucket = _bucket or CACHE_BUCKET

    # Derive cache key from all parameters that affect the output.
    cache_key = _derive_cache_key(
        value_raster_uri,
        zone_input_uri,
        list(statistics),
        zone_threshold,
        nodata_value,
    )

    # Params dict used by the read_through shim for its own key derivation.
    # We pass the same hash as the source_id to ensure the path is deterministic.
    params = {
        "cache_key": cache_key,
    }

    def _fetch() -> bytes:
        computed_at = datetime.now(timezone.utc).isoformat()

        # Download both rasters/vectors to temp files if they are GCS URIs.
        with tempfile.TemporaryDirectory() as tmpdir:
            value_local = _materialize_uri(
                value_raster_uri, tmpdir, "value", _storage_client
            )
            zone_local = _materialize_uri(
                zone_input_uri, tmpdir, "zone", _storage_client
            )

            zone_type = _detect_zone_type(zone_local)
            logger.info(
                "compute_zonal_statistics: zone_type=%s value=%s zone=%s",
                zone_type,
                value_raster_uri,
                zone_input_uri,
            )

            if zone_type == "raster":
                partial = _zonal_stats_raster_zone(
                    value_local,
                    zone_local,
                    list(statistics),
                    zone_threshold,
                    nodata_value,
                )
            else:
                partial = _zonal_stats_vector_zone(
                    value_local,
                    zone_local,
                    list(statistics),
                    nodata_value,
                )

        result: dict[str, Any] = {
            "by_zone": partial["by_zone"],
            "aggregate": partial["aggregate"],
            "value_raster": value_raster_uri,
            "zone_input": zone_input_uri,
            "computed_at": computed_at,
            "units": partial["units"],
        }
        return json.dumps(result, default=str).encode("utf-8")

    rt = read_through(
        metadata=_COMPUTE_ZONAL_STATS_METADATA,
        params=params,
        ext="json",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
        source_id=f"zonal_statistics:{cache_key}",
    )

    return json.loads(rt.data.decode("utf-8"))


def _materialize_uri(
    uri: str,
    tmpdir: str,
    label: str,
    storage_client: object | None,
) -> str:
    """Return a local file path for the given URI.

    If the URI is already a local path (does not start with ``gs://``), return
    it directly without copying. Otherwise download to a temp file.
    """
    # sprint-14-aws (job-0290b): s3:// staging — download to a temp file.
    if uri.startswith("s3://"):
        import tempfile as _tf
        from .cache import read_object_bytes_s3
        _name = uri.rstrip("/").rsplit("/", 1)[-1] or "object.bin"
        _sfx = ("." + _name.rsplit(".", 1)[-1]) if "." in _name else ".bin"
        with _tf.NamedTemporaryFile(suffix=_sfx, delete=False, prefix="trid3nt_zonal_") as _f:
            _f.write(read_object_bytes_s3(uri))
            return _f.name
    if not uri.startswith("gs://"):
        # Local path — use directly (test / dev convenience).
        return uri

    # Determine a safe filename from the URI's last path component.
    name = uri.rstrip("/").rsplit("/", 1)[-1]
    if not name:
        name = f"{label}.bin"
    local_path = os.path.join(tmpdir, f"{label}_{name}")

    data = _download_uri_bytes(uri, storage_client)
    with open(local_path, "wb") as f:
        f.write(data)
    return local_path
