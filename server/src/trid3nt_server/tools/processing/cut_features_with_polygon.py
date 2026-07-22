"""``cut_features_with_polygon`` atomic tool -- per-feature difference by a cutter.

QGIS-plugin-wrapping backlog (DigitizingTools ``DtCutWithPolygon``). The plugin's
core logic is a pure GEOS difference applied IN PLACE: for each cutter polygon and
each intersecting target feature, ``newGeom = targetGeom.difference(cutterGeom)``;
if the result is empty/zero-area, optionally delete the feature, else replace its
geometry while PRESERVING its attributes/IDs. The "feature fully consumed ->
delete?" QMessageBox collapses to the ``delete_emptied`` bool parameter.

The delta over QGIS native ``native:difference`` (Processing) is exactly the
in-place attribute/ID preservation -- the target's columns (including any
precomputed area/length) ride through unchanged on the surviving (cut) geometry,
which the native overlay does not preserve cleanly. That delta is why this is a
Class-B hand-written tool rather than a Processing pass-through.

GPL-cleanliness: **clean-room reimplementation** of the standard GEOS
``difference`` op via shapely/geopandas. No GPL plugin source copied -- a polygon
difference/erase is a commodity GEOS algorithm; the only non-trivial behaviour
(keep attributes, delete-on-empty policy) is re-derived from the documented
behaviour, not lifted from source.

Operation:
    1. Read ``target_uri`` and ``cutter_uri`` with geopandas/pyogrio.
    2. Reproject the cutter to the target CRS if mismatched (the target CRS is
       authoritative so its attribute columns stay coordinate-consistent).
    3. Optionally filter the cutter to ``cutter_feature_ids`` (positional indices)
       and dissolve all selected cutters into ONE mask geometry (unary_union).
    4. For each target feature: ``new = geom.difference(cutter_mask)``. Attributes
       are preserved (the row is kept; only its geometry changes).
    5. If the difference is empty/zero-area: drop the feature when
       ``delete_emptied`` is True (default), else keep it with its (empty)
       geometry -- honest, never silently fabricated.
    6. Promote surviving geometries to MULTI- form (a difference can split one
       polygon into several parts) so the FlatGeobuf schema stays homogeneous.
    7. Write the cut layer as FlatGeobuf bytes; ``read_through`` caches it.

Cache key: SHA-256 of (target_uri, cutter_uri, cutter_feature_ids, delete_emptied)
under ``cache/static-30d/cut_features_polygon/<hash>.fgb``.

Cross-cutting invariants:
- Invariant 2 (Deterministic workflows): preserves -- zero LLM calls.
- FR-DC-6 (cacheable): honors -- cacheable=True, ttl_class="static-30d".
- NFR-R-1 (resilience): preserves -- all failures surface as ``CutFeaturesError``.
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
    "cut_features_with_polygon",
    "CutFeaturesError",
]

logger = logging.getLogger("trid3nt_server.tools.processing.cut_features_with_polygon")


# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class CutFeaturesError(RuntimeError):
    """Raised when cutting fails or inputs are unreadable / unrecognised.

    Codes:
    - ``GEOPANDAS_UNAVAILABLE`` -- geopandas / pyogrio / shapely missing.
    - ``TARGET_OPEN_FAILED`` -- could not read ``target_uri``.
    - ``CUTTER_OPEN_FAILED`` -- could not read ``cutter_uri``.
    - ``UNKNOWN_TARGET_URI`` -- ``target_uri`` not an s3:// URI and not a file.
    - ``UNKNOWN_CUTTER_URI`` -- ``cutter_uri`` not an s3:// URI and not a file.
    - ``DOWNLOAD_FAILED`` -- S3 download failed.
    - ``TARGET_EMPTY`` -- target layer has zero features.
    - ``CUTTER_EMPTY`` -- cutter layer (after filtering) has zero features.
    - ``INVALID_FEATURE_IDS`` -- ``cutter_feature_ids`` out of range / not ints.
    - ``ALL_FEATURES_CONSUMED`` -- the cut emptied every target feature and
      ``delete_emptied`` is True (the result would be an empty layer).
    - ``WRITE_FAILED`` -- FlatGeobuf write failed.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_CUT_FEATURES_METADATA = AtomicToolMetadata(
    name="cut_features_with_polygon",
    ttl_class="static-30d",
    source_class="cut_features_polygon",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Layer URI download helper
# ---------------------------------------------------------------------------


def _resolve_layer_to_local_path(
    uri: str, suffix: str, *, not_found_code: str
) -> tuple[str, bool]:
    """Resolve a layer URI to a local file path. Returns ``(path, is_temp)``."""
    if uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

        try:
            data = read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise CutFeaturesError(
                "DOWNLOAD_FAILED",
                f"S3 download failed for {uri!r}: {exc}",
            ) from exc
        with tempfile.NamedTemporaryFile(
            suffix=suffix, delete=False, prefix="trid3nt_cut_"
        ) as tmp:
            tmp.write(data)
            return tmp.name, True

    if os.path.isfile(uri):
        return uri, False

    raise CutFeaturesError(
        not_found_code,
        f"layer URI {uri!r} is not an s3:// URI and is not a readable local file.",
    )


def _infer_suffix(uri: str) -> str:
    lower = uri.lower()
    for ext in (".fgb", ".geojson", ".json", ".shp", ".gpkg", ".parquet"):
        if lower.endswith(ext):
            return ext
    return ".fgb"


# ---------------------------------------------------------------------------
# Selection helper (positional indices)
# ---------------------------------------------------------------------------


def _select_indices(n_features: int, feature_ids: list[int] | None) -> list[int]:
    if feature_ids is None:
        return list(range(n_features))
    if not isinstance(feature_ids, (list, tuple)):
        raise CutFeaturesError(
            "INVALID_FEATURE_IDS",
            f"cutter_feature_ids must be a list of integer indices or None; got "
            f"{type(feature_ids).__name__}.",
        )
    out: list[int] = []
    for fid in feature_ids:
        try:
            idx = int(fid)
        except (TypeError, ValueError) as exc:
            raise CutFeaturesError(
                "INVALID_FEATURE_IDS",
                f"cutter_feature_ids entry {fid!r} is not an integer index.",
            ) from exc
        if idx < 0 or idx >= n_features:
            raise CutFeaturesError(
                "INVALID_FEATURE_IDS",
                f"cutter_feature_ids index {idx} out of range [0, {n_features}).",
            )
        out.append(idx)
    return out


def _promote_to_multi(geom: Any) -> Any:
    """Promote a single-part geometry to its MULTI- form (a difference can split
    one polygon into several parts; force MULTI so the write schema stays stable)."""
    from shapely.geometry import MultiLineString, MultiPoint, MultiPolygon

    gtype = geom.geom_type
    if gtype == "Polygon":
        return MultiPolygon([geom])
    if gtype == "LineString":
        return MultiLineString([geom])
    if gtype == "Point":
        return MultiPoint([geom])
    return geom


# ---------------------------------------------------------------------------
# Core cut function
# ---------------------------------------------------------------------------


def _cut_locally(
    target_path: str,
    cutter_path: str,
    cutter_feature_ids: list[int] | None,
    delete_emptied: bool,
) -> bytes:
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.ops import unary_union  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CutFeaturesError(
            "GEOPANDAS_UNAVAILABLE",
            f"geopandas / shapely not available: {exc}",
        ) from exc

    # --- read cutter ---
    try:
        cutter_gdf = gpd.read_file(cutter_path, engine="pyogrio")
    except Exception as exc:  # noqa: BLE001
        raise CutFeaturesError(
            "CUTTER_OPEN_FAILED",
            f"could not read cutter source {cutter_path!r}: {exc}",
        ) from exc
    if cutter_gdf.empty:
        raise CutFeaturesError(
            "CUTTER_EMPTY",
            f"cutter source {cutter_path!r} has zero features.",
        )

    cutter_indices = _select_indices(len(cutter_gdf), cutter_feature_ids)
    cutter_sel = cutter_gdf.iloc[cutter_indices]
    if cutter_sel.empty:
        raise CutFeaturesError(
            "CUTTER_EMPTY",
            "cutter_feature_ids selected zero features.",
        )

    # --- read target ---
    try:
        target_gdf = gpd.read_file(target_path, engine="pyogrio")
    except Exception as exc:  # noqa: BLE001
        raise CutFeaturesError(
            "TARGET_OPEN_FAILED",
            f"could not read target source {target_path!r}: {exc}",
        ) from exc
    if target_gdf.empty:
        raise CutFeaturesError(
            "TARGET_EMPTY",
            f"target source {target_path!r} has zero features (nothing to cut).",
        )

    target_crs = target_gdf.crs
    cutter_crs = cutter_sel.crs

    # --- reproject cutter to target CRS if mismatched (target authoritative) ---
    if cutter_crs is not None and target_crs is not None and cutter_crs != target_crs:
        logger.info(
            "cut_features_with_polygon: reprojecting cutter %s -> %s to match target",
            cutter_crs,
            target_crs,
        )
        cutter_sel = cutter_sel.to_crs(target_crs)

    cutter_geoms = [
        g for g in cutter_sel.geometry.tolist() if g is not None and not g.is_empty
    ]
    if not cutter_geoms:
        raise CutFeaturesError(
            "CUTTER_EMPTY",
            "selected cutter features carry no non-empty geometries.",
        )
    cutter_mask = unary_union(cutter_geoms)

    # --- per-feature difference, preserving attributes ---
    new_geoms: list[Any] = []
    keep_rows: list[int] = []
    n_emptied = 0
    for pos, geom in enumerate(target_gdf.geometry.tolist()):
        if geom is None or geom.is_empty:
            # No geometry to cut -- keep as-is (honest passthrough).
            new_geoms.append(geom)
            keep_rows.append(pos)
            continue
        if not geom.intersects(cutter_mask):
            # Untouched by the cutter -- geometry unchanged.
            new_geoms.append(geom)
            keep_rows.append(pos)
            continue
        diff = geom.difference(cutter_mask)
        if diff is None or diff.is_empty:
            n_emptied += 1
            if delete_emptied:
                continue  # drop this feature
            # keep with the (empty) geometry -- honest, not fabricated
            new_geoms.append(diff)
            keep_rows.append(pos)
            continue
        new_geoms.append(_promote_to_multi(diff))
        keep_rows.append(pos)

    if not keep_rows:
        raise CutFeaturesError(
            "ALL_FEATURES_CONSUMED",
            "the cutter polygon fully consumed every target feature and "
            "delete_emptied is True -- the result would be empty.",
        )

    out_gdf = target_gdf.iloc[keep_rows].copy()
    out_gdf = out_gdf.set_geometry(
        gpd.GeoSeries(new_geoms, index=out_gdf.index, crs=target_crs)
    )

    logger.info(
        "cut_features_with_polygon: %d/%d target feature(s) survive (emptied=%d, "
        "delete_emptied=%s)",
        len(keep_rows),
        len(target_gdf),
        n_emptied,
        delete_emptied,
    )

    # FlatGeobuf's packed R-tree rejects NULL geometries; when delete_emptied is
    # False an emptied feature carries an empty geometry, so disable the spatial
    # index for that case (the layer is small and unindexed-FGB still round-trips).
    has_empty = any(g is None or g.is_empty for g in new_geoms)
    write_kwargs: dict[str, Any] = {"driver": "FlatGeobuf", "engine": "pyogrio"}
    if has_empty:
        write_kwargs["SPATIAL_INDEX"] = "NO"

    tmp_out: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_cut_out_"
        ) as out_f:
            tmp_out = out_f.name
        out_gdf.to_file(tmp_out, **write_kwargs)
        with open(tmp_out, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001
        raise CutFeaturesError(
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
    _CUT_FEATURES_METADATA,
    # readOnlyHint=True (reads inputs; writes a cache artifact only),
    # openWorldHint=False (local GEOS), destructiveHint=False (emits a NEW layer),
    # idempotentHint=True (deterministic difference of the same inputs).
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def cut_features_with_polygon(
    target_uri: str,
    cutter_uri: str,
    cutter_feature_ids: list[int] | None = None,
    delete_emptied: bool = True,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    **_extra_ignored: Any,
) -> LayerURI:
    """Erase (difference) a cutter polygon out of each target feature, in place.

    For every target feature, subtracts the cutter polygon geometry
    (``geom.difference(cutter)``) while PRESERVING the target feature's
    attributes. This is the in-place-attribute-preserving erase: unlike a generic
    overlay difference, each surviving feature keeps its original columns on the
    cut-down geometry. Returns a FlatGeobuf ``LayerURI``, cached for 30 days.

    When to use:
        - Punch a hole / remove an area from polygons (e.g. erase a water body
          from land parcels, remove a no-build buffer from developable area,
          subtract a protected zone from an analysis polygon).
        - Trim features back from an obstacle while keeping their attributes.
        - "cut this polygon out of those features", "erase the lake from the
          parcels", "remove the overlap region".

    When NOT to use:
        - Keeping the INSIDE of a mask instead of removing it (use
          ``clip_vector_to_polygon``).
        - Merging features together (use ``merge_features``).
        - Splitting a single feature along a line (a future split tool /
          ``qgis_process`` ``native:splitwithlines``).

    Params:
        target_uri: vector layer to cut -- ``s3://`` or absolute local path
            (FlatGeobuf / GeoJSON / Shapefile / GeoPackage / GeoParquet). Its
            features keep their attributes; only their geometries are trimmed.
        cutter_uri: polygon layer whose geometry is subtracted from the target.
            ``s3://`` or local path. Multiple cutter polygons are dissolved into
            one mask before cutting. Reprojected to the target CRS if needed.
        cutter_feature_ids: optional 0-based positional indices selecting WHICH
            cutter features to use, indexing the cutter layer AS READ by the
            driver (FlatGeobuf and similar formats reorder by a spatial index, so
            this is the deterministic on-disk read order). ``None`` (default)
            uses all cutter features.
        delete_emptied: when ``True`` (default), target features that the cut
            reduces to nothing are DROPPED from the output. When ``False`` they
            are kept with an empty geometry (honest, never fabricated).

    Returns:
        A ``LayerURI`` pointing at the cut FlatGeobuf in the cache bucket.
        ``layer_type="vector"``, ``role="context"``. Surviving geometries are
        promoted to MULTI- form (a cut can split one polygon into parts).

    Raises:
        CutFeaturesError: typed ``error_code`` (GEOPANDAS_UNAVAILABLE,
            TARGET_OPEN_FAILED, CUTTER_OPEN_FAILED, UNKNOWN_TARGET_URI,
            UNKNOWN_CUTTER_URI, DOWNLOAD_FAILED, TARGET_EMPTY, CUTTER_EMPTY,
            INVALID_FEATURE_IDS, ALL_FEATURES_CONSUMED, WRITE_FAILED).
    """
    effective_bucket = _bucket or CACHE_BUCKET

    target_path: str | None = None
    cutter_path: str | None = None
    target_is_temp = False
    cutter_is_temp = False

    def _fetch() -> bytes:
        nonlocal target_path, cutter_path, target_is_temp, cutter_is_temp
        try:
            target_path, target_is_temp = _resolve_layer_to_local_path(
                target_uri,
                suffix=_infer_suffix(target_uri),
                not_found_code="UNKNOWN_TARGET_URI",
            )
            cutter_path, cutter_is_temp = _resolve_layer_to_local_path(
                cutter_uri,
                suffix=_infer_suffix(cutter_uri),
                not_found_code="UNKNOWN_CUTTER_URI",
            )
            return _cut_locally(
                target_path, cutter_path, cutter_feature_ids, delete_emptied
            )
        finally:
            if target_is_temp and target_path is not None:
                try:
                    os.unlink(target_path)
                except OSError:
                    pass
            if cutter_is_temp and cutter_path is not None:
                try:
                    os.unlink(cutter_path)
                except OSError:
                    pass

    params: dict[str, Any] = {
        "target_uri": target_uri,
        "cutter_uri": cutter_uri,
        "cutter_feature_ids": (
            list(cutter_feature_ids) if cutter_feature_ids is not None else None
        ),
        "delete_emptied": delete_emptied,
    }

    result = read_through(
        metadata=_CUT_FEATURES_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
    )
    assert result.uri is not None, "cut_features_with_polygon is cacheable; uri must be set"

    tgt_key = target_uri.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0][:24]
    cut_key = cutter_uri.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0][:24]
    layer_id = f"cut-{tgt_key}-by-{cut_key}"

    return LayerURI(
        layer_id=layer_id,
        name="Cut features",
        layer_type="vector",
        uri=result.uri,
        style_preset="affected_buildings",
        role="context",
        units=None,
        bbox=None,
    )
