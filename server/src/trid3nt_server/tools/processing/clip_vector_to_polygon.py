"""``clip_vector_to_polygon`` atomic tool — clip a vector to an arbitrary polygon (job-0107).

Sibling to ``clip_raster_to_polygon`` (job-0106) and ``clip_raster_to_bbox``
(job-0085). The "in [place]" geographic-clipping pattern for vector layers
— e.g. clip nationwide GBIF panther occurrences (points) or NWS alerts
(polygons) to a TIGER FL state polygon.

Operation:
    1. Read ``polygon_uri`` (FlatGeobuf, GeoJSON, Shapefile) with geopandas/pyogrio.
    2. Apply ``feature_filter`` (column→value matching) if the polygon source has
       multiple features (e.g. TIGER state file with all 50 states; filter by
       ``{"STUSPS": "FL"}``).
    3. Dissolve to a single geometry (``unary_union``) if more than one polygon
       remains after filtering.
    4. Read ``vector_uri`` (FlatGeobuf, GeoJSON, Shapefile, GeoParquet) — the
       layer to clip.
    5. Reproject polygon to vector CRS if mismatched (vector CRS is authoritative
       so attribute joins / lookups against the vector remain coordinate-free).
    6. Clip based on geometry type:
       - Points: ``gpd.sjoin(vector, mask, predicate='intersects')`` if
         ``keep_partial`` else ``predicate='within'`` (points have no
         partial-overlap distinction; ``within`` strictly excludes border-on
         points, ``intersects`` keeps them — we use ``within`` for
         ``keep_partial=False`` semantics: "fully contained").
       - Lines: if ``keep_partial`` keep features that intersect (entire
         original geometry preserved); else ``within`` (entire line inside).
       - Polygons: if ``keep_partial`` keep features whose geometry intersects
         (entire original geometry preserved per the audit.md spec — no
         in-place geometry truncation; that would silently change attributes
         like ``area_m2`` that callers may have computed upstream); else
         ``within`` (entire polygon inside).
    7. Write the clipped result as FlatGeobuf bytes; ``read_through`` caches it.

Cache key: SHA-256 of (vector_uri, polygon_uri, feature_filter, keep_partial)
under ``cache/static-30d/clip_vector_polygon/<hash>.fgb`` — TTL-class
``static-30d`` because both vector and polygon sources are themselves
static-30d cached layers.

Cross-cutting invariants:
- Invariant 2 (Deterministic workflows): preserves — zero LLM calls.
- FR-DC-6 (cacheable): honors — cacheable=True, ttl_class="static-30d".
- NFR-R-1 (resilience): preserves — all failures surface as ``ClipVectorError``.

FR-TA-2 / FR-TA-3 / FR-CE-8 / FR-DC-3/4.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import CACHE_BUCKET, read_through

__all__ = [
    "clip_vector_to_polygon",
    "ClipVectorError",
]

logger = logging.getLogger("trid3nt_server.tools.processing.clip_vector_to_polygon")


# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class ClipVectorError(RuntimeError):
    """Raised when clipping fails or inputs are unreadable / unrecognised.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code surfaced in the
    pipeline strip (NFR-R-1 typed-error requirement).

    Codes:
    - ``GEOPANDAS_UNAVAILABLE`` — geopandas / pyogrio / shapely missing.
    - ``VECTOR_OPEN_FAILED`` — could not read vector_uri.
    - ``POLYGON_OPEN_FAILED`` — could not read polygon_uri.
    - ``POLYGON_FILTER_EMPTY`` — feature_filter excluded every feature.
    - ``POLYGON_EMPTY`` — polygon source has zero features.
    - ``UNKNOWN_VECTOR_URI`` — vector_uri not a gs:// URI and not a readable file.
    - ``UNKNOWN_POLYGON_URI`` — polygon_uri not a gs:// URI and not a readable file.
    - ``DOWNLOAD_FAILED`` — GCS download failed.
    - ``UNSUPPORTED_GEOMETRY`` — vector has mixed / unrecognised geometry types.
    - ``CLIP_EMPTY`` — clip succeeded but produced zero features.
    - ``WRITE_FAILED`` — FlatGeobuf write failed.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_CLIP_VECTOR_METADATA = AtomicToolMetadata(
    name="clip_vector_to_polygon",
    ttl_class="static-30d",
    source_class="clip_vector_polygon",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Layer URI download helper (supports gs:// and local file paths)
# ---------------------------------------------------------------------------


def _resolve_layer_to_local_path(
    uri: str,
    storage_client: object | None,
    suffix: str,
    *,
    not_found_code: str,
) -> tuple[str, bool]:
    """Resolve a layer URI to a local file path.

    Returns ``(path, is_temp)`` — caller deletes the path iff ``is_temp`` is True.

    For ``s3://`` URIs: downloads bytes to a temp file. For local paths: returns
    the path unchanged. GCP is decommissioned, so ``storage_client`` is ignored.
    Raises ``ClipVectorError`` on any failure with the given ``not_found_code``
    for unknown URIs and ``DOWNLOAD_FAILED`` for object-store errors.
    """
    del storage_client  # GCP decommissioned — S3/local only.
    # sprint-14-aws (job-0293b): s3:// staging via the shared boto3 reader
    # (NOT s3fs — instance-role lesson, job-0289). Same return shape:
    # (temp path, is_temp=True); caller owns cleanup.
    if uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

        try:
            data = read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise ClipVectorError(
                "DOWNLOAD_FAILED",
                f"S3 download failed for {uri!r}: {exc}",
            ) from exc
        with tempfile.NamedTemporaryFile(
            suffix=suffix, delete=False, prefix="trid3nt_clipvec_"
        ) as tmp:
            tmp.write(data)
            return tmp.name, True

    if os.path.isfile(uri):
        return uri, False

    raise ClipVectorError(
        not_found_code,
        f"layer URI {uri!r} is not an s3:// URI and is not a readable local file.",
    )


def _infer_suffix(uri: str) -> str:
    """Pick a temp-file suffix matching the URI extension so geopandas/pyogrio
    auto-detects the driver. Falls back to ``.fgb`` (FlatGeobuf) — the format
    every other vector-emitting tool in this codebase produces."""
    lower = uri.lower()
    for ext in (".fgb", ".geojson", ".json", ".shp", ".gpkg", ".parquet"):
        if lower.endswith(ext):
            return ext
    return ".fgb"


# ---------------------------------------------------------------------------
# feature_filter helper
# ---------------------------------------------------------------------------


def _apply_feature_filter(gdf: Any, feature_filter: dict | None) -> Any:
    """Apply column→value equality filter to a GeoDataFrame.

    Multiple keys are AND-combined. Missing columns raise
    ``POLYGON_FILTER_EMPTY`` because the filter cannot match. A filter that
    matches no rows raises ``POLYGON_FILTER_EMPTY`` too.

    Examples:
        ``{"STUSPS": "FL"}`` → keep rows where ``row["STUSPS"] == "FL"``
        ``{"STUSPS": "FL", "FUNCSTAT": "A"}`` → both must match
    """
    if not feature_filter:
        return gdf

    missing = [k for k in feature_filter if k not in gdf.columns]
    if missing:
        raise ClipVectorError(
            "POLYGON_FILTER_EMPTY",
            f"feature_filter keys {missing!r} not present in polygon columns "
            f"{list(gdf.columns)!r}",
        )

    mask = None
    for k, v in feature_filter.items():
        col_mask = gdf[k] == v
        mask = col_mask if mask is None else (mask & col_mask)

    filtered = gdf[mask]
    if filtered.empty:
        raise ClipVectorError(
            "POLYGON_FILTER_EMPTY",
            f"feature_filter {feature_filter!r} matched zero polygon features",
        )
    return filtered


# ---------------------------------------------------------------------------
# Geometry-type classification
# ---------------------------------------------------------------------------


def _classify_geometry(gdf: Any) -> str:
    """Return one of ``"point"``, ``"line"``, ``"polygon"`` based on the dominant
    geometry type of ``gdf``. Raises ``UNSUPPORTED_GEOMETRY`` if the layer is
    empty or contains unrecognised types.
    """
    if gdf.empty:
        raise ClipVectorError(
            "UNSUPPORTED_GEOMETRY",
            "vector layer has zero features; cannot classify geometry type",
        )

    # geom_type returns one of: Point, MultiPoint, LineString, MultiLineString,
    # Polygon, MultiPolygon, GeometryCollection. We collapse to coarse buckets.
    types = set(gdf.geom_type.unique())
    point_types = {"Point", "MultiPoint"}
    line_types = {"LineString", "MultiLineString"}
    polygon_types = {"Polygon", "MultiPolygon"}

    if types <= point_types:
        return "point"
    if types <= line_types:
        return "line"
    if types <= polygon_types:
        return "polygon"

    raise ClipVectorError(
        "UNSUPPORTED_GEOMETRY",
        f"vector layer has mixed or unrecognised geometry types: {sorted(types)!r}",
    )


# ---------------------------------------------------------------------------
# Core clip function — operates on local paths, returns FlatGeobuf bytes
# ---------------------------------------------------------------------------


def _clip_vector_locally(
    vector_path: str,
    polygon_path: str,
    feature_filter: dict | None,
    keep_partial: bool,
) -> bytes:
    """Perform the clip operation locally and return FlatGeobuf bytes.

    Caller is responsible for resolving ``vector_uri`` / ``polygon_uri`` to
    local paths before invoking.

    Raises ``ClipVectorError`` on any failure.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.ops import unary_union  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ClipVectorError(
            "GEOPANDAS_UNAVAILABLE",
            f"geopandas / shapely not available: {exc}",
        ) from exc

    # 1. Read polygon source.
    try:
        poly_gdf = gpd.read_file(polygon_path, engine="pyogrio")
    except Exception as exc:  # noqa: BLE001
        raise ClipVectorError(
            "POLYGON_OPEN_FAILED",
            f"could not read polygon source {polygon_path!r}: {exc}",
        ) from exc

    if poly_gdf.empty:
        raise ClipVectorError(
            "POLYGON_EMPTY",
            f"polygon source {polygon_path!r} has zero features",
        )

    # 2. Apply feature_filter.
    poly_gdf = _apply_feature_filter(poly_gdf, feature_filter)

    # 3. Dissolve to single geometry.
    if len(poly_gdf) > 1:
        clip_geom = unary_union(poly_gdf.geometry.tolist())
    else:
        clip_geom = poly_gdf.geometry.iloc[0]

    poly_crs = poly_gdf.crs

    # 4. Read vector source.
    try:
        vec_gdf = gpd.read_file(vector_path, engine="pyogrio")
    except Exception as exc:  # noqa: BLE001
        raise ClipVectorError(
            "VECTOR_OPEN_FAILED",
            f"could not read vector source {vector_path!r}: {exc}",
        ) from exc

    if vec_gdf.empty:
        raise ClipVectorError(
            "CLIP_EMPTY",
            f"vector source {vector_path!r} has zero features (nothing to clip)",
        )

    vec_crs = vec_gdf.crs

    # 5. Reproject polygon to vector CRS if mismatched.
    # The vector CRS is authoritative (we preserve vector attributes including
    # any precomputed area/length columns that are CRS-dependent).
    if poly_crs is not None and vec_crs is not None and poly_crs != vec_crs:
        logger.info(
            "clip_vector_to_polygon: reprojecting polygon %s → %s to match vector CRS",
            poly_crs,
            vec_crs,
        )
        poly_reproj = poly_gdf.to_crs(vec_crs)
        if len(poly_reproj) > 1:
            clip_geom = unary_union(poly_reproj.geometry.tolist())
        else:
            clip_geom = poly_reproj.geometry.iloc[0]
    elif vec_crs is None and poly_crs is not None:
        # Vector has no CRS — assume it's in polygon CRS and tag it (best effort;
        # logged as a warning since this is unusual for our pipeline).
        logger.warning(
            "clip_vector_to_polygon: vector layer has no CRS; assuming polygon CRS %s",
            poly_crs,
        )
        vec_gdf = vec_gdf.set_crs(poly_crs, allow_override=True)

    # 6. Classify geometry and clip.
    geom_kind = _classify_geometry(vec_gdf)
    logger.info(
        "clip_vector_to_polygon: vector kind=%s n_features=%d keep_partial=%s",
        geom_kind,
        len(vec_gdf),
        keep_partial,
    )

    try:
        if keep_partial:
            # All three kinds: keep features whose geometry intersects the mask.
            # No in-place truncation — preserves original attributes/lengths/areas.
            clipped = vec_gdf[vec_gdf.intersects(clip_geom)].copy()
        else:
            # Strict containment: feature must be entirely within the mask.
            clipped = vec_gdf[vec_gdf.within(clip_geom)].copy()
    except Exception as exc:  # noqa: BLE001
        raise ClipVectorError(
            "VECTOR_OPEN_FAILED",
            f"geopandas spatial predicate failed: {exc}",
        ) from exc

    if clipped.empty:
        raise ClipVectorError(
            "CLIP_EMPTY",
            f"clip produced zero features (geom_kind={geom_kind!r}, "
            f"keep_partial={keep_partial}, polygon_features={len(poly_gdf)}, "
            f"vector_features={len(vec_gdf)})",
        )

    logger.info(
        "clip_vector_to_polygon: %d feature(s) after clip", len(clipped)
    )

    # 7. Write FlatGeobuf bytes via temp file.
    tmp_out: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_clipvec_out_"
        ) as out_f:
            tmp_out = out_f.name

        clipped.to_file(tmp_out, driver="FlatGeobuf", engine="pyogrio")

        with open(tmp_out, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001
        raise ClipVectorError(
            "WRITE_FAILED",
            f"FlatGeobuf write failed: {exc}",
        ) from exc
    finally:
        if tmp_out is not None:
            try:
                os.unlink(tmp_out)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Registered atomic tool
# ---------------------------------------------------------------------------


@register_tool(
    _CLIP_VECTOR_METADATA,
    # Annotations: readOnlyHint=True (reads input raster/vector; writes cache
    # artifact only via the read-through shim), openWorldHint=False (all
    # computation is local GDAL/numpy; no external API calls),
    # destructiveHint=False, idempotentHint=True (deterministic transform;
    # same inputs always produce the same output pixels).
)
def clip_vector_to_polygon(
    vector_uri: str,
    polygon_uri: str,
    feature_filter: dict | None = None,
    keep_partial: bool = True,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Clip a vector (points / lines / polygons) to an arbitrary polygon mask.

    Use this when: trimming a nationwide/regional vector layer to a named
    place ("X in [state/county/protected area]") before display or
    statistics. Do NOT use for: clipping rasters (``clip_raster_to_polygon``);
    attribute-enriching spatial joins (``qgis_process`` sjoin); reprojection
    without filtering (``qgis_process`` reproject).

    Params:
        vector_uri: source vector (FlatGeobuf/GeoJSON/SHP/GPKG/GeoParquet);
            features must share one geometry kind.
        polygon_uri: polygon mask source; if it has multiple features (e.g.
            all 50 states), use ``feature_filter`` to select one, else all
            polygons dissolve into a single mask.
        feature_filter: optional column->value equality filter on the
            polygon source, e.g. ``{"STUSPS": "FL"}``. Zero matches raises
            ``ClipVectorError(POLYGON_FILTER_EMPTY)``.
        keep_partial: ``True`` (default) keeps features that partially
            intersect the mask (geometry untouched). ``False`` requires
            full containment (``gpd.within``) for strict-membership intent.

    Returns:
        ``LayerURI`` for the clipped FlatGeobuf (cache bucket, TTL 30d;
        ``layer_type="vector"``, ``role="context"``).

    Raises:
        ClipVectorError: unreadable layer, zero-match filter, unrecognised
            URI, mixed geometry types, or zero-feature clip result.
    """
    effective_bucket = _bucket or CACHE_BUCKET

    # Resolve both URIs to local paths up-front so the inner _clip_vector_locally
    # works against geopandas/pyogrio file readers.
    vector_path: str | None = None
    polygon_path: str | None = None
    vector_is_temp = False
    polygon_is_temp = False

    def _fetch() -> bytes:
        nonlocal vector_path, polygon_path, vector_is_temp, polygon_is_temp
        try:
            vector_path, vector_is_temp = _resolve_layer_to_local_path(
                vector_uri,
                _storage_client,
                suffix=_infer_suffix(vector_uri),
                not_found_code="UNKNOWN_VECTOR_URI",
            )
            polygon_path, polygon_is_temp = _resolve_layer_to_local_path(
                polygon_uri,
                _storage_client,
                suffix=_infer_suffix(polygon_uri),
                not_found_code="UNKNOWN_POLYGON_URI",
            )

            return _clip_vector_locally(
                vector_path=vector_path,
                polygon_path=polygon_path,
                feature_filter=feature_filter,
                keep_partial=keep_partial,
            )
        finally:
            if vector_is_temp and vector_path is not None:
                try:
                    os.unlink(vector_path)
                except OSError:
                    pass
            if polygon_is_temp and polygon_path is not None:
                try:
                    os.unlink(polygon_path)
                except OSError:
                    pass

    # Cache key on (vector_uri, polygon_uri, feature_filter, keep_partial).
    # None / empty values are omitted by _canonicalize_params (cache.py rule).
    params: dict[str, Any] = {
        "vector_uri": vector_uri,
        "polygon_uri": polygon_uri,
        "feature_filter": feature_filter or None,
        "keep_partial": keep_partial,
    }

    result = read_through(
        metadata=_CLIP_VECTOR_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
    )
    assert result.uri is not None, "clip_vector_to_polygon is cacheable; uri must be set"

    # Build a stable layer_id from the vector + polygon URI hash components.
    vec_key = vector_uri.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0][:24]
    poly_key = polygon_uri.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0][:24]
    layer_id = f"clipvec-{vec_key}-in-{poly_key}"

    name_suffix = ""
    if feature_filter:
        # Render a short filter label like "STUSPS=FL"
        try:
            label = ",".join(f"{k}={v}" for k, v in feature_filter.items())
            if len(label) > 40:
                label = label[:37] + "..."
            name_suffix = f" [{label}]"
        except Exception:  # noqa: BLE001
            pass

    return LayerURI(
        layer_id=layer_id,
        name=f"Clipped vector{name_suffix}",
        layer_type="vector",
        uri=result.uri,
        style_preset="affected_buildings",  # generic vector preset; caller may override
        role="context",
        units=None,
        bbox=None,
    )
