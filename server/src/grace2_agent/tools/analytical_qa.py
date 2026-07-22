"""Atomic tools for conversational analytical Q&A (job-0224, sprint-13 Stage 1).

Three tools that answer "how much / how many / what's the average" questions
directly on already-fetched layer artifacts (raster COGs or vector FGB/GeoJSON).
They are the foundation of the sprint-13 conversational data analysis layer.

    summarize_layer_statistics(layer_uri) → dict
        Raster: min / max / mean / sum / count + 10-bin histogram.
        Vector: feature_count + per-numeric-attribute summary dict.

    count_features_above_threshold(layer_uri, property, threshold) → dict
        Count of vector features where property >= threshold, plus total.

    aggregate_property_within_zone(value_layer_uri, zone_layer_uri,
                                   property, agg) → dict
        Aggregate a vector property (sum|mean|max) for features whose
        centroid falls within any polygon in the zone layer.

All three tools read GCS via the same rasterio/geopandas helpers used by
compute_zonal_statistics.  ttl_class="dynamic-1h" (deterministic per-layer;
same inputs = same result within the hour cache window).

Cross-cutting invariants preserved:
- Invariant 2 (Deterministic workflows): pure rasterio/numpy/geopandas,
  no LLM calls.
- FR-DC-6 (cacheable): cacheable=True, ttl_class="dynamic-1h".
- Invariant 7 (Claims carry provenance): every result dict carries
  layer_uri + computed_at.
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

from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import CACHE_BUCKET, read_through

__all__ = [
    "summarize_layer_statistics",
    "count_features_above_threshold",
    "aggregate_property_within_zone",
    "AnalyticalQAError",
]

logger = logging.getLogger("grace2_agent.tools.analytical_qa")

# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class AnalyticalQAError(RuntimeError):
    """Raised when an analytical Q&A tool fails.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code (NFR-R-1 typed-error
    requirement):

    - ``LAYER_OPEN_FAILED``     — raster or vector layer could not be opened.
    - ``DOWNLOAD_FAILED``       — GCS download for a gs:// URI failed.
    - ``PROPERTY_NOT_FOUND``    — the named property/attribute is absent.
    - ``NO_FEATURES``           — vector layer contains zero features.
    - ``TYPE_ERROR``            — property is non-numeric when numeric needed.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_SUMMARIZE_META = AtomicToolMetadata(
    name="summarize_layer_statistics",
    ttl_class="dynamic-1h",
    source_class="analytical_qa",
    cacheable=True,
)

_COUNT_ABOVE_META = AtomicToolMetadata(
    name="count_features_above_threshold",
    ttl_class="dynamic-1h",
    source_class="analytical_qa",
    cacheable=True,
)

_AGG_ZONE_META = AtomicToolMetadata(
    name="aggregate_property_within_zone",
    ttl_class="dynamic-1h",
    source_class="analytical_qa",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# GCS download / local-path helper (mirrors compute_zonal_statistics pattern)
# ---------------------------------------------------------------------------


def _download_uri_bytes(uri: str, storage_client: object | None = None) -> bytes:
    """Download bytes from an ``s3://`` URI or read a local path.

    GCP is decommissioned: object-store reads route through boto3 (S3).
    ``storage_client`` is retained for backward-compatible call signatures
    but is ignored.
    """
    del storage_client  # GCP decommissioned — S3/local only.
    # sprint-14-aws (job-0293b): s3:// staging via the shared boto3 reader
    # (NOT s3fs — instance-role lesson, job-0289).
    if uri.startswith("s3://"):
        from .cache import read_object_bytes_s3

        try:
            return read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise AnalyticalQAError(
                "DOWNLOAD_FAILED",
                f"S3 download failed for {uri!r}: {exc}",
            ) from exc
    try:
        with open(uri, "rb") as f:
            return f.read()
    except OSError as exc:
        raise AnalyticalQAError(
            "DOWNLOAD_FAILED",
            f"Could not read local path {uri!r}: {exc}",
        ) from exc


def _materialize_uri(
    uri: str,
    tmpdir: str,
    label: str,
    storage_client: object | None = None,
) -> str:
    """Return a local file path for the given URI."""
    # sprint-14-aws (job-0293b): s3:// URIs are staged via the shared reader.
    if uri.startswith("s3://"):
        name = uri.rstrip("/").rsplit("/", 1)[-1] or f"{label}.bin"
        local_path = os.path.join(tmpdir, f"{label}_{name}")
        data = _download_uri_bytes(uri, storage_client)
        with open(local_path, "wb") as f:
            f.write(data)
        return local_path
    return uri


# ---------------------------------------------------------------------------
# Layer-type detection
# ---------------------------------------------------------------------------

_RASTER_EXTS = {".tif", ".tiff", ".img", ".vrt", ".nc"}
_VECTOR_EXTS = {".fgb", ".geojson", ".gpkg", ".shp", ".json", ".gml", ".kml"}


def _layer_type(uri: str) -> str:
    """Return ``"raster"`` or ``"vector"`` by file extension, or by probing."""
    ext = os.path.splitext(uri.split("?")[0].rstrip("/"))[-1].lower()
    if ext in _RASTER_EXTS:
        return "raster"
    if ext in _VECTOR_EXTS:
        return "vector"
    try:
        import rasterio
        with rasterio.open(uri):
            return "raster"
    except Exception:  # noqa: BLE001
        return "vector"


# ---------------------------------------------------------------------------
# Cache-key helpers
# ---------------------------------------------------------------------------


def _sha256_key(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Raster summarization helpers
# ---------------------------------------------------------------------------


def _summarize_raster(local_path: str) -> dict[str, Any]:
    """Open a single-band raster and compute summary statistics + histogram."""
    try:
        import rasterio
    except ImportError as exc:
        raise AnalyticalQAError(
            "LAYER_OPEN_FAILED", "rasterio not available"
        ) from exc

    try:
        with rasterio.open(local_path) as src:
            data = src.read(1).astype(np.float64)
            nodata = src.nodata
            units = (
                src.tags().get("units")
                or (src.units[0] if src.units else None)
            )
    except Exception as exc:  # noqa: BLE001
        raise AnalyticalQAError(
            "LAYER_OPEN_FAILED",
            f"Could not open raster {local_path!r}: {exc}",
        ) from exc

    # Build valid-pixel mask.
    import math
    if nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)):
        valid = (data != nodata) & ~np.isnan(data)
    else:
        valid = ~np.isnan(data)

    pixels = data[valid]
    count = int(pixels.size)

    if count == 0:
        return {
            "layer_type": "raster",
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "sum": None,
            "distribution": [],
            "units": units,
        }

    mn = float(np.min(pixels))
    mx = float(np.max(pixels))
    mu = float(np.mean(pixels))
    total = float(np.sum(pixels))

    # 10-bin histogram over the valid-pixel range.
    hist, bin_edges = np.histogram(pixels, bins=10)
    distribution = [
        {
            "bin_start": float(bin_edges[i]),
            "bin_end": float(bin_edges[i + 1]),
            "count": int(hist[i]),
        }
        for i in range(len(hist))
    ]

    return {
        "layer_type": "raster",
        "count": count,
        "min": mn,
        "max": mx,
        "mean": mu,
        "sum": total,
        "distribution": distribution,
        "units": units,
    }


# ---------------------------------------------------------------------------
# Vector summarization helpers
# ---------------------------------------------------------------------------


def _read_geodataframe(local_path: str):  # type: ignore[return]
    """Read a vector file into a GeoDataFrame."""
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        return gpd.read_file(local_path)
    except Exception as exc:  # noqa: BLE001
        raise AnalyticalQAError(
            "LAYER_OPEN_FAILED",
            f"Could not open vector layer {local_path!r}: {exc}",
        ) from exc


def _summarize_vector(local_path: str) -> dict[str, Any]:
    """Read a vector layer and compute per-attribute numeric summaries."""
    gdf = _read_geodataframe(local_path)
    feature_count = len(gdf)

    attribute_summary: dict[str, Any] = {}
    for col in gdf.columns:
        if col in ("geometry",):
            continue
        series = gdf[col]
        if not np.issubdtype(series.dtype, np.number):
            continue
        vals = series.dropna().values.astype(np.float64)
        if vals.size == 0:
            attribute_summary[col] = {
                "count": 0,
                "min": None,
                "max": None,
                "mean": None,
                "sum": None,
            }
        else:
            attribute_summary[col] = {
                "count": int(vals.size),
                "min": float(np.min(vals)),
                "max": float(np.max(vals)),
                "mean": float(np.mean(vals)),
                "sum": float(np.sum(vals)),
            }

    return {
        "layer_type": "vector",
        "feature_count": feature_count,
        "attribute_summary": attribute_summary,
    }


# ---------------------------------------------------------------------------
# Tool 1: summarize_layer_statistics
# ---------------------------------------------------------------------------


@register_tool(
    _SUMMARIZE_META,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def summarize_layer_statistics(
    layer_uri: str,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Summarize statistics for a raster or vector layer.

    Use this when the user asks a quantitative question about an already-fetched
    layer: "What's the range of flood depths?", "How many buildings are in the
    dataset?", "What are the statistics for this layer?"

    For raster layers returns: count (valid pixels), min, max, mean, sum,
    10-bin histogram (distribution), and units (if encoded in raster tags).

    For vector layers returns: feature_count and attribute_summary — a dict
    keyed by numeric attribute name, each entry having count/min/max/mean/sum.

    Do NOT use this for: rendering (use compute_colored_relief / publish_layer);
    aggregating within a zone (use compute_zonal_statistics or
    aggregate_property_within_zone); counting features above a threshold (use
    count_features_above_threshold).

    Parameters:
        layer_uri: the layer's ``layer_id`` HANDLE from a prior tool result
            (PREFERRED — the server resolves handles to exact storage URIs;
            never construct gs:// paths), or a gs:// URI copied verbatim.
            Supports raster (GeoTIFF / COG) and vector (GeoJSON, FlatGeobuf,
            GeoPackage).

    Returns:
        For raster:
            {
              "layer_type": "raster",
              "count": int,        # valid (non-nodata) pixel count
              "min": float,
              "max": float,
              "mean": float,
              "sum": float,
              "distribution": [{"bin_start":…, "bin_end":…, "count":…}, …],  # 10 bins
              "units": str | None,
              "layer_uri": str,    # provenance
              "computed_at": str,  # ISO 8601
            }
        For vector:
            {
              "layer_type": "vector",
              "feature_count": int,
              "attribute_summary": {
                "<attr>": {"count":…, "min":…, "max":…, "mean":…, "sum":…},
                …
              },
              "layer_uri": str,
              "computed_at": str,
            }

    LLM guidance:
        - For raster flood / DEM / population layers, check "mean" and "max"
          for exposure context.
        - For vector damage / structure layers, inspect attribute_summary keys
          for monetary / area / damage-state attributes.

    Raises:
        AnalyticalQAError: with typed error_code on failure.
    """
    effective_bucket = _bucket or CACHE_BUCKET
    cache_key = _sha256_key({"layer_uri": layer_uri, "tool": "summarize_layer_statistics"})

    def _fetch() -> bytes:
        computed_at = datetime.now(timezone.utc).isoformat()
        with tempfile.TemporaryDirectory() as tmpdir:
            local = _materialize_uri(layer_uri, tmpdir, "layer", _storage_client)
            ltype = _layer_type(local)
            if ltype == "raster":
                stats = _summarize_raster(local)
            else:
                stats = _summarize_vector(local)
        stats["layer_uri"] = layer_uri
        stats["computed_at"] = computed_at
        return json.dumps(stats, default=str).encode("utf-8")

    rt = read_through(
        metadata=_SUMMARIZE_META,
        params={"cache_key": cache_key},
        ext="json",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
        source_id=f"analytical_qa:summarize:{cache_key}",
    )
    return json.loads(rt.data.decode("utf-8"))


# ---------------------------------------------------------------------------
# Tool 2: count_features_above_threshold
# ---------------------------------------------------------------------------


@register_tool(
    _COUNT_ABOVE_META,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def count_features_above_threshold(
    layer_uri: str,
    property: str,
    threshold: float,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Count vector features where a numeric property meets or exceeds a threshold.

    Use this for questions like "How many buildings have a damage ratio above 0.5?",
    "How many structures have ground_elev_m below 2?", "Count features where
    pop_density >= 1000".

    Works on vector layers (GeoJSON / FlatGeobuf / GeoPackage). For raster
    pixel-counting use compute_zonal_statistics with a threshold-based zone.

    Parameters:
        layer_uri: the layer's ``layer_id`` HANDLE from a prior tool result
            (PREFERRED; never construct gs:// paths), or a verbatim gs:// URI
            of a vector layer.
        property: name of the numeric attribute to threshold.
        threshold: minimum value (inclusive) to count. Features where
            property >= threshold are included.

    Returns:
        {
          "count": int,        # features where property >= threshold
          "total": int,        # total feature count (including null/non-numeric)
          "property": str,     # echo of the property arg
          "threshold": float,  # echo of the threshold arg
          "layer_uri": str,    # provenance
          "computed_at": str,  # ISO 8601
        }

    LLM guidance:
        - Useful after a Pelicun damage run to count critically damaged structures.
        - Combine with aggregate_property_within_zone for spatial refinement.
        - Returns count=0 and total=N when no features meet the threshold — not
          an error.

    Raises:
        AnalyticalQAError: PROPERTY_NOT_FOUND if the attribute is absent;
            LAYER_OPEN_FAILED if the file cannot be read.
    """
    effective_bucket = _bucket or CACHE_BUCKET
    cache_key = _sha256_key({
        "layer_uri": layer_uri,
        "property": property,
        "threshold": threshold,
        "tool": "count_features_above_threshold",
    })

    def _fetch() -> bytes:
        computed_at = datetime.now(timezone.utc).isoformat()
        with tempfile.TemporaryDirectory() as tmpdir:
            local = _materialize_uri(layer_uri, tmpdir, "layer", _storage_client)
            gdf = _read_geodataframe(local)

        total = len(gdf)
        if property not in gdf.columns:
            raise AnalyticalQAError(
                "PROPERTY_NOT_FOUND",
                f"Property {property!r} not found in layer {layer_uri!r}. "
                f"Available columns: {sorted(str(c) for c in gdf.columns if c != 'geometry')}",
            )

        series = gdf[property]
        # Convert to numeric; non-convertible values become NaN.
        try:
            import pandas as pd
            numeric = pd.to_numeric(series, errors="coerce")
        except Exception:  # noqa: BLE001
            numeric = series

        count = int((numeric >= threshold).sum())

        result = {
            "count": count,
            "total": total,
            "property": property,
            "threshold": float(threshold),
            "layer_uri": layer_uri,
            "computed_at": computed_at,
        }
        return json.dumps(result, default=str).encode("utf-8")

    rt = read_through(
        metadata=_COUNT_ABOVE_META,
        params={"cache_key": cache_key},
        ext="json",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
        source_id=f"analytical_qa:count_above:{cache_key}",
    )
    return json.loads(rt.data.decode("utf-8"))


# ---------------------------------------------------------------------------
# Tool 3: aggregate_property_within_zone
# ---------------------------------------------------------------------------


@register_tool(
    _AGG_ZONE_META,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def aggregate_property_within_zone(
    value_layer_uri: str,
    zone_layer_uri: str,
    property: str,
    agg: Literal["sum", "mean", "max"] = "sum",
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Aggregate a vector property for features spatially within a zone polygon layer.

    Use this for spatially filtered aggregation: "What is the total damage cost
    within the flood zone?", "What's the mean building value inside the county?",
    "Max structure replacement cost within the WDPA protected area?"

    Features whose centroid falls within any polygon in zone_layer_uri are
    included in the aggregation. Requires geopandas.

    Parameters:
        value_layer_uri: layer_id HANDLE from a prior tool result (PREFERRED;
            never construct gs:// paths) or verbatim gs:// URI of the vector
            layer whose ``property`` attribute is aggregated (e.g. USACE NSI
            structure inventory, Pelicun damage output, GBIF occurrences).
        zone_layer_uri: layer_id HANDLE (PREFERRED) or verbatim gs:// URI of
            a vector polygon layer defining the zone(s) to aggregate within
            (e.g. county boundary, WDPA protected area, FEMA flood zone
            polygon).
        property: name of the numeric attribute in value_layer_uri to aggregate.
        agg: aggregation function — "sum" (default), "mean", or "max".

    Returns:
        {
          "value": float,            # aggregated result (sum / mean / max)
          "agg": str,                # echo of the agg arg
          "n_features": int,         # count of features within the zone
          "total_features": int,     # total feature count before spatial filter
          "property": str,           # echo of the property arg
          "value_layer_uri": str,    # provenance
          "zone_layer_uri": str,     # provenance
          "computed_at": str,        # ISO 8601
        }

    LLM guidance:
        - Useful after run_model_flood_habitat_scenario to aggregate species
          count within WDPA polygons, or after run_pelicun_damage_assessment
          to aggregate damage within an admin boundary.
        - n_features=0 means no value-layer features intersect the zone — not
          an error; the value will be 0 for sum, None for mean/max.
        - For raster-based aggregation use compute_zonal_statistics instead.

    Raises:
        AnalyticalQAError: PROPERTY_NOT_FOUND, LAYER_OPEN_FAILED.
    """
    effective_bucket = _bucket or CACHE_BUCKET
    cache_key = _sha256_key({
        "value_layer_uri": value_layer_uri,
        "zone_layer_uri": zone_layer_uri,
        "property": property,
        "agg": agg,
        "tool": "aggregate_property_within_zone",
    })

    def _fetch() -> bytes:
        computed_at = datetime.now(timezone.utc).isoformat()

        try:
            import geopandas as gpd  # type: ignore[import-not-found]
        except ImportError as exc:
            raise AnalyticalQAError(
                "LAYER_OPEN_FAILED", "geopandas not available"
            ) from exc

        with tempfile.TemporaryDirectory() as tmpdir:
            value_local = _materialize_uri(value_layer_uri, tmpdir, "value", _storage_client)
            zone_local = _materialize_uri(zone_layer_uri, tmpdir, "zone", _storage_client)

            try:
                value_gdf = gpd.read_file(value_local)
            except Exception as exc:  # noqa: BLE001
                raise AnalyticalQAError(
                    "LAYER_OPEN_FAILED",
                    f"Could not open value layer {value_layer_uri!r}: {exc}",
                ) from exc

            try:
                zone_gdf = gpd.read_file(zone_local)
            except Exception as exc:  # noqa: BLE001
                raise AnalyticalQAError(
                    "LAYER_OPEN_FAILED",
                    f"Could not open zone layer {zone_layer_uri!r}: {exc}",
                ) from exc

            total_features = len(value_gdf)

            if property not in value_gdf.columns:
                raise AnalyticalQAError(
                    "PROPERTY_NOT_FOUND",
                    f"Property {property!r} not found in value layer {value_layer_uri!r}. "
                    f"Available columns: {sorted(str(c) for c in value_gdf.columns if c != 'geometry')}",
                )

            # Align CRS: reproject value_gdf to zone CRS if needed.
            if value_gdf.crs is not None and zone_gdf.crs is not None:
                if value_gdf.crs != zone_gdf.crs:
                    value_gdf = value_gdf.to_crs(zone_gdf.crs)
            elif value_gdf.crs is None and zone_gdf.crs is not None:
                # Assume value layer is already in the same CRS; set it to avoid
                # geopandas CRS mismatch warnings.
                value_gdf = value_gdf.set_crs(zone_gdf.crs)

            # Spatial join: keep value features whose centroid is within any zone.
            value_centroids = value_gdf.copy()
            value_centroids["geometry"] = value_gdf.geometry.centroid
            joined = gpd.sjoin(value_centroids, zone_gdf[["geometry"]], how="inner", predicate="within")

            n_features = len(joined)

            # Deduplicate if a centroid falls in multiple zone polygons.
            joined = joined[~joined.index.duplicated(keep="first")]
            n_features_unique = len(joined)

            # Aggregate the property.
            import pandas as pd
            numeric = pd.to_numeric(joined[property], errors="coerce").dropna()

            if numeric.empty:
                agg_value: float | None = 0.0 if agg == "sum" else None
            elif agg == "sum":
                agg_value = float(numeric.sum())
            elif agg == "mean":
                agg_value = float(numeric.mean())
            elif agg == "max":
                agg_value = float(numeric.max())
            else:
                agg_value = float(numeric.sum())  # fallback

        result = {
            "value": agg_value,
            "agg": agg,
            "n_features": n_features_unique,
            "total_features": total_features,
            "property": property,
            "value_layer_uri": value_layer_uri,
            "zone_layer_uri": zone_layer_uri,
            "computed_at": computed_at,
        }
        return json.dumps(result, default=str).encode("utf-8")

    rt = read_through(
        metadata=_AGG_ZONE_META,
        params={"cache_key": cache_key},
        ext="json",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
        source_id=f"analytical_qa:agg_zone:{cache_key}",
    )
    return json.loads(rt.data.decode("utf-8"))
