"""``merge_features`` atomic tool -- union/dissolve selected vector features into one.

QGIS-plugin-wrapping backlog (DigitizingTools ``DtMerge``). The plugin's core
logic is a pure GEOS unary-union of a feature set: take the geometries of the
selected features, combine them into one geometry, keep one survivor feature's
attributes, drop the rest. The "which feature survives / keeps PK" choice is the
only UI -- it collapses to the ``keep_id`` parameter.

GPL-cleanliness: this is a **clean-room reimplementation** of the standard GEOS
``unary_union`` operation using shapely/geopandas (already first-party deps). No
GPL plugin source was copied -- a dissolve/union is a commodity GEOS algorithm
(QGIS native ``native:dissolve`` does the same), and reimplementing it from the
documented behaviour is GPL-clean per the survey's license note.

Operation:
    1. Read ``layer_uri`` (FlatGeobuf, GeoJSON, Shapefile, GeoParquet) with
       geopandas/pyogrio.
    2. Select the features named by ``feature_ids`` (positional row indices) --
       ``None`` selects ALL features (merge the whole layer).
    3. ``shapely.unary_union`` the selected geometries into one geometry.
    4. Emit ONE output feature carrying that merged geometry. Its attributes are
       copied from the "keeper": the feature at positional index ``keep_id`` when
       given, else the first selected feature.
    5. Promote the merged geometry to MULTI- form (MultiPolygon / MultiLineString
       / MultiPoint) so a mixed single/multi union round-trips through a
       single-geometry-type FlatGeobuf without a type-mismatch write error.
    6. Write the single-feature result as FlatGeobuf bytes; ``read_through``
       caches it.

Cache key: SHA-256 of (layer_uri, feature_ids, keep_id) under
``cache/static-30d/merge_features/<hash>.fgb``.

Cross-cutting invariants:
- Invariant 2 (Deterministic workflows): preserves -- zero LLM calls.
- FR-DC-6 (cacheable): honors -- cacheable=True, ttl_class="static-30d".
- NFR-R-1 (resilience): preserves -- all failures surface as ``MergeFeaturesError``.
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
    "merge_features",
    "MergeFeaturesError",
]

logger = logging.getLogger("trid3nt_server.tools.processing.merge_features")


# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class MergeFeaturesError(RuntimeError):
    """Raised when merging fails or inputs are unreadable / unrecognised.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code surfaced in the
    pipeline strip (NFR-R-1 typed-error requirement).

    Codes:
    - ``GEOPANDAS_UNAVAILABLE`` -- geopandas / pyogrio / shapely missing.
    - ``VECTOR_OPEN_FAILED`` -- could not read ``layer_uri``.
    - ``UNKNOWN_VECTOR_URI`` -- ``layer_uri`` not an s3:// URI and not a readable file.
    - ``DOWNLOAD_FAILED`` -- S3 download failed.
    - ``LAYER_EMPTY`` -- the source layer has zero features.
    - ``INVALID_FEATURE_IDS`` -- ``feature_ids`` / ``keep_id`` out of range or
      not integer indices.
    - ``NOTHING_TO_MERGE`` -- fewer than 1 selected feature.
    - ``EMPTY_RESULT`` -- the union produced an empty geometry.
    - ``WRITE_FAILED`` -- FlatGeobuf write failed.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_MERGE_FEATURES_METADATA = AtomicToolMetadata(
    name="merge_features",
    ttl_class="static-30d",
    source_class="merge_features",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Layer URI download helper (supports s3:// and local file paths)
# ---------------------------------------------------------------------------


def _resolve_layer_to_local_path(uri: str, suffix: str) -> tuple[str, bool]:
    """Resolve a layer URI to a local file path.

    Returns ``(path, is_temp)`` -- caller deletes the path iff ``is_temp`` is True.
    For ``s3://`` URIs: downloads bytes to a temp file via the shared boto3 reader
    (NOT s3fs -- instance-role lesson, job-0289). For local paths: returns the
    path unchanged. Raises ``MergeFeaturesError`` on any failure.
    """
    if uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

        try:
            data = read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise MergeFeaturesError(
                "DOWNLOAD_FAILED",
                f"S3 download failed for {uri!r}: {exc}",
            ) from exc
        with tempfile.NamedTemporaryFile(
            suffix=suffix, delete=False, prefix="trid3nt_merge_"
        ) as tmp:
            tmp.write(data)
            return tmp.name, True

    if os.path.isfile(uri):
        return uri, False

    raise MergeFeaturesError(
        "UNKNOWN_VECTOR_URI",
        f"layer URI {uri!r} is not an s3:// URI and is not a readable local file.",
    )


def _infer_suffix(uri: str) -> str:
    """Pick a temp-file suffix matching the URI extension so pyogrio auto-detects
    the driver. Falls back to ``.fgb`` (FlatGeobuf)."""
    lower = uri.lower()
    for ext in (".fgb", ".geojson", ".json", ".shp", ".gpkg", ".parquet"):
        if lower.endswith(ext):
            return ext
    return ".fgb"


# ---------------------------------------------------------------------------
# Selection helper
# ---------------------------------------------------------------------------


def _select_indices(n_features: int, feature_ids: list[int] | None) -> list[int]:
    """Resolve ``feature_ids`` (positional indices) to a validated index list.

    ``None`` selects every feature. Out-of-range / non-integer ids raise
    ``INVALID_FEATURE_IDS``.
    """
    if feature_ids is None:
        return list(range(n_features))

    if not isinstance(feature_ids, (list, tuple)):
        raise MergeFeaturesError(
            "INVALID_FEATURE_IDS",
            f"feature_ids must be a list of integer indices or None; got "
            f"{type(feature_ids).__name__}.",
        )

    out: list[int] = []
    for fid in feature_ids:
        try:
            idx = int(fid)
        except (TypeError, ValueError) as exc:
            raise MergeFeaturesError(
                "INVALID_FEATURE_IDS",
                f"feature_ids entry {fid!r} is not an integer index.",
            ) from exc
        if idx < 0 or idx >= n_features:
            raise MergeFeaturesError(
                "INVALID_FEATURE_IDS",
                f"feature_ids index {idx} out of range [0, {n_features}).",
            )
        out.append(idx)
    # De-dup while preserving order (a feature merged with itself is a no-op).
    seen: set[int] = set()
    deduped: list[int] = []
    for idx in out:
        if idx not in seen:
            seen.add(idx)
            deduped.append(idx)
    return deduped


# ---------------------------------------------------------------------------
# Multi-promotion helper
# ---------------------------------------------------------------------------


def _promote_to_multi(geom: Any) -> Any:
    """Promote a single-part geometry to its MULTI- form so the output layer can
    declare a homogeneous Multi* geometry type (FlatGeobuf is single-type).

    A ``unary_union`` of adjacent polygons may collapse to a single Polygon or
    stay a MultiPolygon depending on adjacency; forcing MULTI keeps the write
    schema stable regardless.
    """
    from shapely.geometry import (
        MultiLineString,
        MultiPoint,
        MultiPolygon,
    )

    gtype = geom.geom_type
    if gtype == "Polygon":
        return MultiPolygon([geom])
    if gtype == "LineString":
        return MultiLineString([geom])
    if gtype == "Point":
        return MultiPoint([geom])
    # Already Multi* or a GeometryCollection -- pass through unchanged.
    return geom


# ---------------------------------------------------------------------------
# Core merge function -- operates on a local path, returns FlatGeobuf bytes
# ---------------------------------------------------------------------------


def _merge_locally(
    layer_path: str,
    feature_ids: list[int] | None,
    keep_id: int | None,
) -> bytes:
    """Perform the merge locally and return single-feature FlatGeobuf bytes."""
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.ops import unary_union  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MergeFeaturesError(
            "GEOPANDAS_UNAVAILABLE",
            f"geopandas / shapely not available: {exc}",
        ) from exc

    try:
        gdf = gpd.read_file(layer_path, engine="pyogrio")
    except Exception as exc:  # noqa: BLE001
        raise MergeFeaturesError(
            "VECTOR_OPEN_FAILED",
            f"could not read vector source {layer_path!r}: {exc}",
        ) from exc

    if gdf.empty:
        raise MergeFeaturesError(
            "LAYER_EMPTY",
            f"vector source {layer_path!r} has zero features (nothing to merge).",
        )

    n = len(gdf)
    indices = _select_indices(n, feature_ids)
    if not indices:
        raise MergeFeaturesError(
            "NOTHING_TO_MERGE",
            "no features selected to merge (feature_ids resolved to empty set).",
        )

    # Resolve the keeper (whose attributes survive). Default: first selected.
    if keep_id is None:
        keeper_idx = indices[0]
    else:
        try:
            keeper_idx = int(keep_id)
        except (TypeError, ValueError) as exc:
            raise MergeFeaturesError(
                "INVALID_FEATURE_IDS",
                f"keep_id {keep_id!r} is not an integer index.",
            ) from exc
        if keeper_idx < 0 or keeper_idx >= n:
            raise MergeFeaturesError(
                "INVALID_FEATURE_IDS",
                f"keep_id index {keeper_idx} out of range [0, {n}).",
            )
        if keeper_idx not in indices:
            # The keeper must be among the merged features (its geometry
            # participates in the union); fold it in.
            indices = [keeper_idx, *indices]

    # iloc by positional index -> the selected sub-frame.
    selected = gdf.iloc[indices]
    geoms = [g for g in selected.geometry.tolist() if g is not None and not g.is_empty]
    if not geoms:
        raise MergeFeaturesError(
            "NOTHING_TO_MERGE",
            "selected features carry no non-empty geometries.",
        )

    merged = unary_union(geoms)
    if merged is None or merged.is_empty:
        raise MergeFeaturesError(
            "EMPTY_RESULT",
            "the union of the selected features produced an empty geometry.",
        )
    merged = _promote_to_multi(merged)

    # Build a one-row GeoDataFrame: keeper attributes + merged geometry.
    keeper_row = gdf.iloc[[keeper_idx]].copy()
    keeper_row = keeper_row.set_geometry(
        gpd.GeoSeries([merged], index=keeper_row.index, crs=gdf.crs)
    )

    logger.info(
        "merge_features: merged %d feature(s) (keeper idx=%d) -> %s",
        len(indices),
        keeper_idx,
        merged.geom_type,
    )

    tmp_out: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_merge_out_"
        ) as out_f:
            tmp_out = out_f.name
        keeper_row.to_file(tmp_out, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_out, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001
        raise MergeFeaturesError(
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
    _MERGE_FEATURES_METADATA,
    # readOnlyHint=True (reads input vector; writes a cache artifact only),
    # openWorldHint=False (local shapely/GEOS, no external API),
    # destructiveHint=False (emits a NEW layer; the source is untouched),
    # idempotentHint=True (deterministic union of the same inputs).
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def merge_features(
    layer_uri: str,
    feature_ids: list[int] | None = None,
    keep_id: int | None = None,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Merge (dissolve) selected vector features into ONE combined feature.

    Use this when: dissolving several adjacent polygons into one (combine
    parcels into a single AOI, merge fragmented flood-zone polygons) --
    "merge these into one", "dissolve the selected polygons". Unions
    geometries via ``shapely.unary_union``; output keeps ONE feature's
    attributes (the "keeper"). Do NOT use for: differencing one polygon
    out of another (``cut_features_with_polygon``); dissolve-by-attribute
    into multiple groups (``qgis_process`` ``native:dissolve`` with a
    FIELD); clipping to a mask (``clip_vector_to_polygon``).

    Params:
        layer_uri: source vector layer.
        feature_ids: optional 0-based indices to merge (driver read
            order). ``None`` merges all features.
        keep_id: optional index of the feature whose attributes the
            merged output keeps; defaults to the first selected feature.

    Returns:
        ``LayerURI`` for a single-feature FlatGeobuf (cache bucket,
        vector, role="context"; geometry promoted to MULTI- form).

    Raises:
        MergeFeaturesError: GEOPANDAS_UNAVAILABLE, VECTOR_OPEN_FAILED,
            UNKNOWN_VECTOR_URI, DOWNLOAD_FAILED, LAYER_EMPTY,
            INVALID_FEATURE_IDS, NOTHING_TO_MERGE, EMPTY_RESULT,
            WRITE_FAILED.
    """
    effective_bucket = _bucket or CACHE_BUCKET

    layer_path: str | None = None
    layer_is_temp = False

    def _fetch() -> bytes:
        nonlocal layer_path, layer_is_temp
        try:
            layer_path, layer_is_temp = _resolve_layer_to_local_path(
                layer_uri, suffix=_infer_suffix(layer_uri)
            )
            return _merge_locally(layer_path, feature_ids, keep_id)
        finally:
            if layer_is_temp and layer_path is not None:
                try:
                    os.unlink(layer_path)
                except OSError:
                    pass

    params: dict[str, Any] = {
        "layer_uri": layer_uri,
        "feature_ids": list(feature_ids) if feature_ids is not None else None,
        "keep_id": keep_id,
    }

    result = read_through(
        metadata=_MERGE_FEATURES_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
    )
    assert result.uri is not None, "merge_features is cacheable; uri must be set"

    base = layer_uri.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0][:24]
    layer_id = f"merged-{base}"

    return LayerURI(
        layer_id=layer_id,
        name="Merged features",
        layer_type="vector",
        uri=result.uri,
        style_preset="affected_buildings",  # generic vector preset; caller may override
        role="context",
        units=None,
        bbox=None,
    )
