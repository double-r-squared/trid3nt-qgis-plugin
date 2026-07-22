"""``fill_gaps`` atomic tool -- emit sliver gaps enclosed between adjacent polygons.

QGIS-plugin-wrapping backlog (DigitizingTools ``DtFillGap`` + ``dtCombineSelectedPolygons``
+ ``dtExtractRings``). The plugin's core logic is pure geometry: union all the
input polygons, then harvest the INTERIOR RINGS of the combined geometry -- any
interior ring of the union is a gap/sliver fully enclosed by the inputs. Each such
ring becomes a new polygon feature. The interactive "fill THIS gap" single-click
pick is just a filter over the same enclosed-ring set; the batch "fill all gaps"
mode is fully headless and is the natural agent default.

This is genuine topology cleanup for digitized / ML-derived polygons -- slivers
between adjacent parcels, courtyards between buildings, holes between FTW field
boundaries.

GPL-cleanliness: **clean-room reimplementation** using shapely
(``unary_union`` + ``Polygon.interiors``). No GPL plugin source copied -- interior-
ring extraction from a union is a commodity GEOS pattern, re-derived from the
documented behaviour described in the survey (the ``dtExtractRings`` nugget is
``poly.interiors`` -> ``Polygon(ring)``).

Operation:
    1. Read each ``layer_uri`` with geopandas/pyogrio.
    2. Reproject all sources to the FIRST layer's CRS (the common-CRS
       harmonization the multi-layer variant needs).
    3. Optionally filter the first layer to ``feature_ids`` (positional indices).
    4. Collect every polygon geometry, ``unary_union`` them into one combined
       geometry.
    5. Harvest the interior rings of the union (per Polygon part). Each interior
       ring, re-polygonized, is an enclosed gap.
    6. Optionally drop gaps larger than ``max_gap_area`` (in the working CRS's
       area units) so only true slivers are emitted -- ``None`` keeps all.
    7. Write the gap polygons as a NEW FlatGeobuf layer; ``read_through`` caches
       it. (Empty -> typed NO_GAPS_FOUND, never a fabricated layer.)

Cache key: SHA-256 of (layer_uris, feature_ids, max_gap_area) under
``cache/static-30d/fill_gaps/<hash>.fgb``.

Cross-cutting invariants:
- Invariant 2 (Deterministic workflows): preserves -- zero LLM calls.
- FR-DC-6 (cacheable): honors -- cacheable=True, ttl_class="static-30d".
- NFR-R-1 (resilience): preserves -- all failures surface as ``FillGapsError``.
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
    "fill_gaps",
    "FillGapsError",
]

logger = logging.getLogger("trid3nt_server.tools.processing.fill_gaps")


# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class FillGapsError(RuntimeError):
    """Raised when gap extraction fails or inputs are unreadable / unrecognised.

    Codes:
    - ``GEOPANDAS_UNAVAILABLE`` -- geopandas / pyogrio / shapely missing.
    - ``VECTOR_OPEN_FAILED`` -- could not read a ``layer_uri``.
    - ``UNKNOWN_VECTOR_URI`` -- a layer_uri not an s3:// URI and not a readable file.
    - ``DOWNLOAD_FAILED`` -- S3 download failed.
    - ``NO_LAYERS`` -- no layer_uri supplied.
    - ``LAYER_EMPTY`` -- the (filtered) sources carry zero polygons.
    - ``NOT_POLYGONS`` -- the sources contain no polygon geometries (gaps are a
      polygon-only concept).
    - ``INVALID_FEATURE_IDS`` -- ``feature_ids`` out of range / not integers.
    - ``NO_GAPS_FOUND`` -- the union had no enclosed interior rings (no slivers).
    - ``WRITE_FAILED`` -- FlatGeobuf write failed.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_FILL_GAPS_METADATA = AtomicToolMetadata(
    name="fill_gaps",
    ttl_class="static-30d",
    source_class="fill_gaps",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Layer URI download helper
# ---------------------------------------------------------------------------


def _resolve_layer_to_local_path(uri: str, suffix: str) -> tuple[str, bool]:
    if uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

        try:
            data = read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise FillGapsError(
                "DOWNLOAD_FAILED",
                f"S3 download failed for {uri!r}: {exc}",
            ) from exc
        with tempfile.NamedTemporaryFile(
            suffix=suffix, delete=False, prefix="trid3nt_fillgap_"
        ) as tmp:
            tmp.write(data)
            return tmp.name, True

    if os.path.isfile(uri):
        return uri, False

    raise FillGapsError(
        "UNKNOWN_VECTOR_URI",
        f"layer URI {uri!r} is not an s3:// URI and is not a readable local file.",
    )


def _infer_suffix(uri: str) -> str:
    lower = uri.lower()
    for ext in (".fgb", ".geojson", ".json", ".shp", ".gpkg", ".parquet"):
        if lower.endswith(ext):
            return ext
    return ".fgb"


def _select_indices(n_features: int, feature_ids: list[int] | None) -> list[int]:
    if feature_ids is None:
        return list(range(n_features))
    if not isinstance(feature_ids, (list, tuple)):
        raise FillGapsError(
            "INVALID_FEATURE_IDS",
            f"feature_ids must be a list of integer indices or None; got "
            f"{type(feature_ids).__name__}.",
        )
    out: list[int] = []
    for fid in feature_ids:
        try:
            idx = int(fid)
        except (TypeError, ValueError) as exc:
            raise FillGapsError(
                "INVALID_FEATURE_IDS",
                f"feature_ids entry {fid!r} is not an integer index.",
            ) from exc
        if idx < 0 or idx >= n_features:
            raise FillGapsError(
                "INVALID_FEATURE_IDS",
                f"feature_ids index {idx} out of range [0, {n_features}).",
            )
        out.append(idx)
    return out


# ---------------------------------------------------------------------------
# Interior-ring harvest -- the dtExtractRings nugget, clean-room
# ---------------------------------------------------------------------------


def _extract_enclosed_gaps(union_geom: Any) -> list[Any]:
    """Return the interior rings of a (multi)polygon union as filled gap polygons.

    Each interior ring of the combined geometry is a void fully enclosed by the
    inputs -- a gap. We re-polygonize each ring into its own Polygon. This is the
    ``dtExtractRings`` behaviour (``poly[1:]`` interior rings -> new polygons),
    reimplemented over ``shapely.Polygon.interiors``.
    """
    from shapely.geometry import Polygon

    gaps: list[Any] = []

    def _harvest(poly: Any) -> None:
        for ring in poly.interiors:
            gap = Polygon(ring)
            if not gap.is_empty and gap.area > 0:
                gaps.append(gap)

    gtype = union_geom.geom_type
    if gtype == "Polygon":
        _harvest(union_geom)
    elif gtype in ("MultiPolygon", "GeometryCollection"):
        for part in union_geom.geoms:
            if part.geom_type == "Polygon":
                _harvest(part)
    return gaps


# ---------------------------------------------------------------------------
# Core fill-gaps function
# ---------------------------------------------------------------------------


def _fill_gaps_locally(
    layer_paths: list[str],
    feature_ids: list[int] | None,
    max_gap_area: float | None,
) -> bytes:
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.ops import unary_union  # type: ignore[import-not-found]
    except ImportError as exc:
        raise FillGapsError(
            "GEOPANDAS_UNAVAILABLE",
            f"geopandas / shapely not available: {exc}",
        ) from exc

    frames = []
    base_crs = None
    for li, path in enumerate(layer_paths):
        try:
            gdf = gpd.read_file(path, engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise FillGapsError(
                "VECTOR_OPEN_FAILED",
                f"could not read vector source {path!r}: {exc}",
            ) from exc
        if gdf.empty:
            continue
        if li == 0:
            base_crs = gdf.crs
            # feature_ids only filters the FIRST layer (the primary selection).
            indices = _select_indices(len(gdf), feature_ids)
            gdf = gdf.iloc[indices]
        elif base_crs is not None and gdf.crs is not None and gdf.crs != base_crs:
            # Harmonize every other source to the first layer's CRS.
            gdf = gdf.to_crs(base_crs)
        frames.append(gdf)

    if not frames:
        raise FillGapsError(
            "LAYER_EMPTY",
            "the supplied layer(s) carry zero features after filtering.",
        )

    # Collect polygon geometries only (gaps are a polygon-only concept).
    polys: list[Any] = []
    for gdf in frames:
        for g in gdf.geometry.tolist():
            if g is None or g.is_empty:
                continue
            if g.geom_type == "Polygon":
                polys.append(g)
            elif g.geom_type == "MultiPolygon":
                polys.extend(list(g.geoms))

    if not polys:
        raise FillGapsError(
            "NOT_POLYGONS",
            "no polygon geometries found in the source(s); gap-filling requires "
            "polygons (a gap is an enclosed void between adjacent polygons).",
        )

    union_geom = unary_union(polys)
    gaps = _extract_enclosed_gaps(union_geom)

    if max_gap_area is not None:
        try:
            cap = float(max_gap_area)
            gaps = [g for g in gaps if g.area <= cap]
        except (TypeError, ValueError):
            logger.warning(
                "fill_gaps: ignoring non-numeric max_gap_area=%r", max_gap_area
            )

    if not gaps:
        raise FillGapsError(
            "NO_GAPS_FOUND",
            "no enclosed gaps (interior rings) were found in the union of the "
            "supplied polygons -- there are no slivers to fill.",
        )

    out_gdf = gpd.GeoDataFrame(
        {"gap_index": list(range(len(gaps))), "gap_area": [g.area for g in gaps]},
        geometry=gaps,
        crs=base_crs,
    )

    logger.info(
        "fill_gaps: %d enclosed gap(s) emitted from %d polygon(s) across %d layer(s)",
        len(gaps),
        len(polys),
        len(frames),
    )

    tmp_out: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_fillgap_out_"
        ) as out_f:
            tmp_out = out_f.name
        out_gdf.to_file(tmp_out, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_out, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001
        raise FillGapsError(
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
    _FILL_GAPS_METADATA,
    # readOnlyHint=True (reads inputs; writes a cache artifact only),
    # openWorldHint=False (local GEOS), destructiveHint=False (emits a NEW layer),
    # idempotentHint=True (deterministic interior-ring harvest of the same inputs).
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def fill_gaps(
    layer_uri: str,
    extra_layer_uris: list[str] | None = None,
    feature_ids: list[int] | None = None,
    max_gap_area: float | None = None,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    **_extra_ignored: Any,
) -> LayerURI:
    """Find sliver gaps enclosed between adjacent polygons and emit them as polygons.

    Unions the input polygons and harvests the INTERIOR RINGS of the combined
    geometry -- each enclosed void (a sliver/gap fully surrounded by the inputs)
    becomes a new polygon feature. This is topology cleanup for digitized or
    ML-derived polygon coverages. Returns a FlatGeobuf ``LayerURI`` of the gap
    polygons, cached for 30 days.

    When to use:
        - Find slivers between adjacent parcels / field boundaries / building
          footprints that should tile without gaps (e.g. FTW ag-field boundaries,
          OSM building footprints).
        - Generate the "missing" polygons to backfill a coverage (courtyards,
          interstitial voids).
        - "find the gaps between these polygons", "fill the slivers", "what
          areas are uncovered between the parcels".

    When NOT to use:
        - Extracting donut HOLES of individual features (that is interior-ring
          extraction of single features -- a future ``fill_rings`` tool).
        - Merging polygons together (use ``merge_features``).
        - Removing an area (use ``cut_features_with_polygon``).

    Note: only voids FULLY ENCLOSED by the inputs are detected -- a gap open to
    the outside boundary is not an interior ring and is not returned.

    Params:
        layer_uri: primary polygon layer -- ``s3://`` or absolute local path
            (FlatGeobuf / GeoJSON / Shapefile / GeoPackage / GeoParquet).
        extra_layer_uris: optional additional polygon layers to union together
            with the primary before harvesting gaps (multi-layer slivers across
            adjacent datasets). Reprojected to the primary layer's CRS.
        feature_ids: optional 0-based positional indices selecting WHICH features
            of the PRIMARY layer to use, indexing the layer AS READ by the driver
            (FlatGeobuf and similar formats reorder by a spatial index, so this is
            the deterministic on-disk read order). ``None`` (default) uses all
            features.
        max_gap_area: optional maximum gap area (in the primary layer CRS's area
            units) -- gaps larger than this are dropped so only true slivers are
            emitted. ``None`` (default) keeps every enclosed gap.

    Returns:
        A ``LayerURI`` pointing at a FlatGeobuf of gap polygons (each with a
        ``gap_index`` and ``gap_area`` attribute) in the cache bucket.
        ``layer_type="vector"``, ``role="context"``.

    Raises:
        FillGapsError: typed ``error_code`` (GEOPANDAS_UNAVAILABLE,
            VECTOR_OPEN_FAILED, UNKNOWN_VECTOR_URI, DOWNLOAD_FAILED, NO_LAYERS,
            LAYER_EMPTY, NOT_POLYGONS, INVALID_FEATURE_IDS, NO_GAPS_FOUND,
            WRITE_FAILED).
    """
    if not isinstance(layer_uri, str) or not layer_uri.strip():
        raise FillGapsError(
            "NO_LAYERS", f"layer_uri must be a non-empty URI string; got {layer_uri!r}."
        )

    effective_bucket = _bucket or CACHE_BUCKET

    uris: list[str] = [layer_uri.strip()]
    if extra_layer_uris:
        if not isinstance(extra_layer_uris, (list, tuple)):
            raise FillGapsError(
                "NO_LAYERS",
                f"extra_layer_uris must be a list of URI strings; got "
                f"{type(extra_layer_uris).__name__}.",
            )
        for u in extra_layer_uris:
            if isinstance(u, str) and u.strip():
                uris.append(u.strip())

    resolved: list[tuple[str, bool]] = []

    def _fetch() -> bytes:
        try:
            for u in uris:
                resolved.append(_resolve_layer_to_local_path(u, suffix=_infer_suffix(u)))
            paths = [p for p, _ in resolved]
            return _fill_gaps_locally(paths, feature_ids, max_gap_area)
        finally:
            for path, is_temp in resolved:
                if is_temp:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    params: dict[str, Any] = {
        "layer_uris": uris,
        "feature_ids": list(feature_ids) if feature_ids is not None else None,
        "max_gap_area": max_gap_area,
    }

    result = read_through(
        metadata=_FILL_GAPS_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
    )
    assert result.uri is not None, "fill_gaps is cacheable; uri must be set"

    base = layer_uri.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0][:24]
    layer_id = f"gaps-{base}"

    return LayerURI(
        layer_id=layer_id,
        name="Gap polygons",
        layer_type="vector",
        uri=result.uri,
        style_preset="affected_buildings",
        role="context",
        units=None,
        bbox=None,
    )
