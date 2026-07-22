"""``fetch_field_boundaries`` atomic tool — agricultural FIELD-BOUNDARY vectors
from Fields of The World (FTW) / fiboa PUBLISHED datasets (NATE 2026-06-17).

This is the v1 PUBLISHED-VECTOR fetcher. It serves field-boundary polygons that
already exist as cloud-native GeoParquet on Source Cooperative — it does NOT run
on-demand boundary inference from satellite imagery (that is a separate
follow-on tool; see "Coverage" below). Where the published benchmark has no
coverage for the requested bbox, the tool raises an HONEST typed error
(``FIELDS_NO_COVERAGE``) per ``feedback_data_source_fallback_norm`` — it never
fabricates polygons.

--------------------------------------------------------------------------------
ACCESS PATTERN (researched + LIVE-PROBED 2026-06-17)
--------------------------------------------------------------------------------
The fiboa organization on Source Cooperative publishes one GeoParquet file per
region, served over public HTTPS at:

    https://data.source.coop/<account>/<repo>/<file>.parquet

with NO authentication. ``data.source.coop`` is an S3-compatible object store
front; every object responds with ``Accept-Ranges: bytes``, so HTTP range
requests work.

The country files are large (US-USDA: 4.3 GB / 16.2M parcels; Japan: 4.9 GB /
29.4M parcels). Downloading them whole is a non-starter. The reliable bbox-
filtered path is **GeoParquet 1.1 row-group pruning over HTTP range requests**:

    fs = fsspec.filesystem("https", headers={"User-Agent": ...})
    with fs.open(url) as f:
        gdf = geopandas.read_parquet(f, bbox=<bbox-in-FILE-CRS>, columns=[...])

geopandas + pyarrow read only the row groups whose ``bbox`` covering-column
statistics intersect the query bbox, pulling a few MB instead of gigabytes.

TWO hard-won lessons from the live probe (both encoded below):

1. COVERING METADATA IS REQUIRED. Only GeoParquet **1.1.0** files that declare a
   ``covering`` bbox column AND are split into many row groups can be pruned.
   The older 1.0.0 single-row-group files (e.g. Denmark, 420 MB, one row group,
   no covering) cannot be pruned — a ``bbox=`` read would pull the entire file.
   Each registry entry therefore declares ``pushdown`` explicitly, and the
   runtime re-confirms the ``covering`` key is present in the file's ``geo``
   metadata before trusting pushdown. Non-pushdown datasets are only read in
   full when their (small) file size makes that acceptable.

2. THE COVERING BBOX IS IN THE FILE'S NATIVE CRS, NOT WGS84. The US-USDA file
   is NAD83 / Albers Equal Area (meters); its ``bbox`` covering stats are in
   Albers meters. Passing a WGS84 ``(lon, lat)`` bbox returns ZERO rows (the
   probe's first failure). The query bbox MUST be reprojected from WGS84 into
   the file's CRS before it is handed to ``read_parquet(bbox=...)``, and the
   returned geometry MUST be reprojected back to WGS84 for the inline-GeoJSON
   layer. Both transforms are done below.

LIVE PROBE RESULTS (2026-06-17, real Source Cooperative endpoints):
  - US-USDA, Ames-Iowa bbox (-93.70,42.00,-93.60,42.08): 247 field polygons,
    result bounds inside the requested AOI, ~bytes-pruned (not 4.3 GB).
  - Japan, bbox (140.30,35.70,140.40,35.78): 10,226 field polygons, result
    bounds inside the AOI.
  - WGS84 bbox passed without CRS reprojection: 0 rows (confirms lesson #2).

--------------------------------------------------------------------------------
COVERAGE (the benchmark regions — NOT global yet)
--------------------------------------------------------------------------------
The published FTW/fiboa corpus covers specific countries/regions, not the whole
planet. This tool registers the datasets it has VERIFIED work end-to-end. A bbox
that falls outside every registered region raises ``FIELDS_NO_COVERAGE`` — the
honest "we have no published boundaries here" signal. The future ON-DEMAND
GLOBAL INFERENCE tool (run an FTW model over Sentinel-2 for an arbitrary AOI)
is a separate job and is deliberately NOT built here.

--------------------------------------------------------------------------------
OUTPUT / RENDER
--------------------------------------------------------------------------------
Returns a ``LayerURI(layer_type="vector")`` pointing at a FlatGeobuf in the
read-through cache. The agent's ``pipeline_emitter`` reads the FGB and ships it
to the client as inline GeoJSON (the Wave 4.9 vector path), exactly like
``fetch_roads_osm`` and ``fetch_wdpa_protected_areas`` — do NOT call
``publish_layer`` on the result.

FR-TA-2 atomic tool; FR-CE-8 / FR-DC-3 read-through cache (identical
``(bbox, dataset)`` calls reuse the cached FlatGeobuf within the 30-day TTL).
Pattern reference: ``fetch_roads_osm.py`` (inline-GeoJSON vector, clip-to-bbox),
``fetch_wdpa_protected_areas.py`` (typed-error surface, cache-key).
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass
from typing import Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_field_boundaries",
    "FieldsError",
    "FieldsInputError",
    "FieldsNoCoverageError",
    "FieldsUpstreamError",
    "FTW_DATASETS",
    "FieldDataset",
]

logger = logging.getLogger("grace2_agent.tools.fetch_field_boundaries")


# ---------------------------------------------------------------------------
# Typed-error surface (FR-AS-11). ``error_code`` maps to the WebSocket A.6
# error frame; ``retryable`` guides retry logic.
# ---------------------------------------------------------------------------


class FieldsError(RuntimeError):
    """Base class for fetch_field_boundaries failures."""

    error_code: str = "FIELDS_ERROR"
    retryable: bool = True


class FieldsInputError(FieldsError):
    """The caller passed an invalid argument (bad bbox, unknown dataset)."""

    error_code = "FIELDS_INPUT_INVALID"
    retryable = False


class FieldsNoCoverageError(FieldsError):
    """No PUBLISHED field-boundary dataset covers the requested bbox.

    This is the HONEST typed error per ``feedback_data_source_fallback_norm``:
    the FTW/fiboa benchmark is regional, not global, and on-demand inference is
    a separate (not-yet-built) tool. We refuse to fabricate boundaries. Not
    retryable — retrying the same out-of-coverage bbox cannot succeed.
    """

    error_code = "FIELDS_NO_COVERAGE"
    retryable = False


class FieldsUpstreamError(FieldsError):
    """The Source Cooperative GeoParquet read failed (network / parse / I/O)."""

    error_code = "FIELDS_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Dataset registry.
#
# Each entry is a VERIFIED-WORKING published FTW/fiboa GeoParquet on Source
# Cooperative. URLs + formats were live-probed 2026-06-17. We register only
# datasets we have confirmed exist and read correctly — the ms-buildings-abfs
# lesson (never stub against an unverified endpoint).
#
# ``coverage`` is the WGS84 (lon/lat) extent of the dataset (from its STAC
# collection.json spatial extent). ``pushdown`` is True iff the file is
# GeoParquet 1.1 with a ``covering`` bbox column + multiple row groups (so a
# bbox read prunes via HTTP range requests instead of pulling the whole file).
# The runtime re-confirms ``covering`` is actually present before trusting it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldDataset:
    """One published FTW/fiboa field-boundary dataset."""

    key: str
    label: str
    url: str
    #: WGS84 (min_lon, min_lat, max_lon, max_lat) coverage extent.
    coverage: tuple[float, float, float, float]
    #: True iff the file supports GeoParquet 1.1 row-group bbox pushdown.
    pushdown: bool
    license: str
    #: Property column carrying the human crop/land-type label, if any. Kept in
    #: the output FlatGeobuf so the client can label fields. None = none mapped.
    crop_field: str | None = None


#: Verified FTW/fiboa datasets. Ordered most-specific-first within a region so
#: the bbox→dataset match picks the best fit.
FTW_DATASETS: tuple[FieldDataset, ...] = (
    # CONUS — USDA Crop Sequence Boundaries (the GRACE-2 headline coverage).
    # GeoParquet 1.1.0, 648 row groups, 16.2M parcels, NAD83/Albers (projected).
    FieldDataset(
        key="us_usda_cropland",
        label="US Cropland Field Boundaries (USDA CSB)",
        url="https://data.source.coop/fiboa/us-usda-cropland/us_usda_cropland.parquet",
        coverage=(-124.736342, 24.521208, -66.945392, 49.382808),
        pushdown=True,
        license="USDA CSB (public, attribution)",
        crop_field="crop:name",
    ),
    # Japan — fiboa, GeoParquet 1.1.0, 1177 row groups, 29.4M parcels.
    FieldDataset(
        key="japan",
        label="Japan Field Boundaries (fiboa)",
        url="https://data.source.coop/fiboa/japan/japan.parquet",
        coverage=(122.946031194, 24.046770215, 145.802669174, 45.520014956),
        pushdown=True,
        license="CC-BY-4.0",
        crop_field="land_type_en",
    ),
    # Denmark — fiboa, GeoParquet 1.0.0, SINGLE row group, 0.61M parcels,
    # 420 MB. NO covering column → cannot prune; read in full (small enough).
    FieldDataset(
        key="denmark",
        label="Denmark Field Boundaries (fiboa)",
        url="https://data.source.coop/fiboa/denmark/denmark.parquet",
        coverage=(8.02765145891927, 54.44421901416275, 15.573199436854463, 57.75163614760113),
        pushdown=False,
        license="CC-0",
        crop_field="crop_name",
    ),
)


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "grace-2/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/GRACE-2; agent@grace-2.dev)"
)

#: Per-call cap on returned features. A bbox that pulls more than this is almost
#: certainly an unintentional whole-region query; we cap (and warn) so a single
#: tool call can't ship a 100 MB inline-GeoJSON to the client.
_MAX_FEATURES = 50_000

_METADATA = AtomicToolMetadata(
    name="fetch_field_boundaries",
    ttl_class="static-30d",
    source_class="ftw_field_boundaries",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Validate + coerce a WGS84 bbox; raise ``FieldsInputError`` if invalid."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise FieldsInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    try:
        vals = tuple(float(v) for v in bbox)
    except (TypeError, ValueError) as exc:
        raise FieldsInputError(f"bbox values must be numeric: {bbox!r}") from exc
    min_lon, min_lat, max_lon, max_lat = vals
    if not all(math.isfinite(v) for v in vals):
        raise FieldsInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise FieldsInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise FieldsInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise FieldsInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    return vals  # type: ignore[return-value]


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coords to 6 dp (~0.1 m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _bbox_intersects(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    """True iff two WGS84 (min_lon,min_lat,max_lon,max_lat) bboxes overlap."""
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _select_dataset(
    bbox: tuple[float, float, float, float],
    dataset: str | None,
) -> FieldDataset:
    """Pick the FTW dataset covering ``bbox`` (or honor explicit ``dataset``).

    Raises:
        ``FieldsInputError``: ``dataset`` names an unknown key.
        ``FieldsNoCoverageError``: no registered dataset overlaps ``bbox``.
    """
    if dataset is not None:
        for ds in FTW_DATASETS:
            if ds.key == dataset:
                if not _bbox_intersects(bbox, ds.coverage):
                    raise FieldsNoCoverageError(
                        f"dataset {dataset!r} (coverage {ds.coverage}) does not "
                        f"intersect bbox {bbox}; pick a bbox inside the dataset "
                        f"or omit `dataset` to auto-select."
                    )
                return ds
        raise FieldsInputError(
            f"unknown dataset {dataset!r}; valid keys: "
            f"{[d.key for d in FTW_DATASETS]}"
        )

    matches = [ds for ds in FTW_DATASETS if _bbox_intersects(bbox, ds.coverage)]
    if not matches:
        raise FieldsNoCoverageError(
            "no published Fields of The World / fiboa dataset covers bbox "
            f"{bbox}. Published coverage is regional (currently: "
            f"{', '.join(d.label for d in FTW_DATASETS)}). On-demand global "
            "field-boundary inference from satellite imagery is a separate "
            "future tool and is not available yet."
        )
    # Prefer a pushdown-capable dataset when several overlap (cheaper read).
    matches.sort(key=lambda d: (not d.pushdown,))
    return matches[0]


# ---------------------------------------------------------------------------
# GeoParquet read (CRS-aware bbox pushdown over HTTP range requests).
# ---------------------------------------------------------------------------


def _open_parquet_fs(url: str):
    """Open an fsspec file handle for a Source Cooperative GeoParquet URL."""
    import fsspec  # type: ignore[import-not-found]

    fs = fsspec.filesystem("https", headers={"User-Agent": _USER_AGENT})
    return fs.open(url)


def _file_crs(parquet_file: Any) -> Any:
    """Return the file's geometry CRS (pyproj CRS) — defaults to OGC:CRS84.

    GeoParquet stores the CRS as PROJJSON under the primary column's ``crs``
    key in the ``geo`` file metadata. A missing ``crs`` means OGC:CRS84
    (WGS84 lon/lat) per the GeoParquet spec.
    """
    import pyproj

    geo_raw = parquet_file.schema_arrow.metadata.get(b"geo")
    if not geo_raw:
        return pyproj.CRS.from_user_input("OGC:CRS84")
    geo = json.loads(geo_raw)
    prim = geo.get("primary_column")
    col = (geo.get("columns") or {}).get(prim, {})
    crs = col.get("crs")
    if crs is None:
        return pyproj.CRS.from_user_input("OGC:CRS84")
    if isinstance(crs, dict):
        return pyproj.CRS.from_json_dict(crs)
    return pyproj.CRS.from_user_input(crs)


def _has_covering(parquet_file: Any) -> bool:
    """True iff the GeoParquet declares a ``covering`` bbox column (1.1)."""
    geo_raw = parquet_file.schema_arrow.metadata.get(b"geo")
    if not geo_raw:
        return False
    geo = json.loads(geo_raw)
    prim = geo.get("primary_column")
    col = (geo.get("columns") or {}).get(prim, {})
    return bool(col.get("covering"))


def _read_fields_gdf(
    ds: FieldDataset,
    bbox: tuple[float, float, float, float],
):
    """Read field-boundary polygons for ``bbox`` from dataset ``ds``.

    CRS-aware bbox pushdown: reproject the WGS84 query bbox into the file's CRS,
    pass it to ``read_parquet(bbox=...)`` (which prunes row groups via HTTP
    range requests when the file carries a covering column), clip to the exact
    requested bbox, and reproject the result back to WGS84.

    Returns a WGS84 GeoDataFrame (possibly empty).
    """
    import geopandas as gpd  # type: ignore[import-not-found]
    import pyarrow.parquet as pq  # type: ignore[import-not-found]
    import pyproj
    from shapely.geometry import box  # type: ignore[import-not-found]

    columns = ["geometry"]
    if ds.crop_field:
        columns.append(ds.crop_field)

    try:
        fh = _open_parquet_fs(ds.url)
    except Exception as exc:  # noqa: BLE001
        raise FieldsUpstreamError(
            f"could not open {ds.url}: {exc}"
        ) from exc

    try:
        # Inspect file CRS + covering up front (footer-only range read).
        try:
            pf = pq.ParquetFile(fh)
            file_crs = _file_crs(pf)
            covering = _has_covering(pf)
            # Reset position for the subsequent full read.
            try:
                fh.seek(0)
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            raise FieldsUpstreamError(
                f"could not read GeoParquet metadata for {ds.key}: {exc}"
            ) from exc

        wgs84 = pyproj.CRS.from_epsg(4326)
        # Reproject the WGS84 query bbox into the file's CRS so the covering
        # stats (which are in the file CRS) match. If the file is already
        # CRS84/EPSG:4326 the transform is a no-op.
        if file_crs.equals(wgs84) or file_crs.to_epsg() == 4326:
            pushdown_bbox = bbox
            same_crs = True
        else:
            same_crs = False
            qb = gpd.GeoSeries([box(*bbox)], crs="EPSG:4326").to_crs(file_crs)
            pushdown_bbox = tuple(float(v) for v in qb.total_bounds)

        read_kwargs: dict[str, Any] = {"columns": columns}
        # Only request bbox pushdown when the file actually supports it; passing
        # bbox to a non-covering file would error or pull the whole file.
        use_pushdown = ds.pushdown and covering
        if use_pushdown:
            read_kwargs["bbox"] = pushdown_bbox

        try:
            gdf = gpd.read_parquet(fh, **read_kwargs)
        except Exception as exc:  # noqa: BLE001
            raise FieldsUpstreamError(
                f"GeoParquet read failed for {ds.key}: {exc}"
            ) from exc
    finally:
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass

    if gdf.crs is None:
        gdf = gdf.set_crs(file_crs, allow_override=True)

    # Reproject to WGS84 for clipping + output.
    if not same_crs:
        gdf = gdf.to_crs("EPSG:4326")

    # Clip to the EXACT requested bbox (pushdown is row-group-level, so edge
    # row groups bring in fields outside the AOI; a non-pushdown read brings
    # in the whole region). Clipping keeps the rendered fields inside the AOI.
    from shapely.geometry import box as _box  # type: ignore[import-not-found]

    clip_geom = _box(*bbox)
    gdf = gdf[gdf.geometry.intersects(clip_geom)].copy()
    if len(gdf) > 0:
        gdf["geometry"] = gdf.geometry.intersection(clip_geom)
        gdf = gdf[~gdf.geometry.is_empty].copy()

    if len(gdf) > _MAX_FEATURES:
        logger.warning(
            "fetch_field_boundaries: %d fields in bbox for %s exceeds cap %d; "
            "truncating (narrow the bbox for the full set)",
            len(gdf),
            ds.key,
            _MAX_FEATURES,
        )
        gdf = gdf.iloc[:_MAX_FEATURES].copy()

    # Normalize the crop/label column to a stable output name.
    if ds.crop_field and ds.crop_field in gdf.columns:
        gdf = gdf.rename(columns={ds.crop_field: "crop_name"})
    elif "crop_name" not in gdf.columns:
        gdf["crop_name"] = None

    keep = ["geometry", "crop_name"]
    gdf = gdf[[c for c in keep if c in gdf.columns]]
    return gdf


def _gdf_to_flatgeobuf_bytes(gdf: Any) -> bytes:
    """Serialize a WGS84 GeoDataFrame to FlatGeobuf bytes via pyogrio.

    An empty GeoDataFrame still produces valid (empty) FlatGeobuf bytes so the
    cache write succeeds; we never write a sentinel (poisons future reads).
    """
    import geopandas as gpd  # type: ignore[import-not-found]

    if gdf is None or len(gdf) == 0:
        import pandas as pd  # type: ignore[import-not-found]

        empty = pd.DataFrame({"crop_name": pd.Series(dtype="object")})
        gdf = gpd.GeoDataFrame(
            empty, geometry=gpd.GeoSeries([], crs="EPSG:4326"), crs="EPSG:4326"
        )

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_ftw_fields_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise FieldsUpstreamError(f"FlatGeobuf write failed: {exc}") from exc
        with open(tmp_fgb, "rb") as f:
            return f.read()
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


def _fetch_fields_bytes(
    ds: FieldDataset,
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Cache miss-path fetcher passed to ``read_through``."""
    gdf = _read_fields_gdf(ds, bbox)
    logger.info(
        "fetch_field_boundaries: %d field(s) for dataset=%s bbox=%s",
        len(gdf),
        ds.key,
        bbox,
    )
    return _gdf_to_flatgeobuf_bytes(gdf)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # readOnlyHint=True, openWorldHint=True (public Source Cooperative HTTPS),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_field_boundaries(
    bbox: tuple[float, float, float, float],
    dataset: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Agricultural FIELD BOUNDARIES (Fields of The World / fiboa) for an AOI.

    **What it does:** Fetches published agricultural field-boundary polygons —
    individual farm parcels / fields — for the requested bbox from the Fields of
    The World (FTW) / fiboa open datasets on Source Cooperative, clips them to
    the exact AOI, and returns them as a vector layer that renders inline on the
    map automatically (like roads / protected areas). NO API key required. The
    resulting vector renders inline — do NOT call ``publish_layer`` on it.

    **Coverage (IMPORTANT — regional, not global yet):** This serves the
    PUBLISHED FTW/fiboa benchmark, which covers specific regions, currently:
    the contiguous United States (USDA Crop Sequence Boundaries), Japan, and
    Denmark. A bbox outside every covered region returns a structured
    ``FIELDS_NO_COVERAGE`` error — there are simply no published boundaries
    there. On-demand field-boundary INFERENCE from satellite imagery for an
    arbitrary AOI (running an FTW model anywhere on Earth) is a SEPARATE future
    tool and is not available through this one.

    **When to use:**
    - User wants to see farm fields / agricultural parcels / field boundaries
      over an AOI in the US, Japan, or Denmark.
    - A hazard analysis needs cropland parcel geometry as context (e.g. which
      fields a flood footprint covers, agricultural exposure mapping).
    - Pairs with flood / drought / fire layers to quantify agricultural impact.

    **When NOT to use:**
    - Land-cover CLASSIFICATION rasters (use ``fetch_landcover`` / NLCD) — this
      returns vector PARCEL boundaries, not a per-pixel crop-type raster.
    - Cadastral / legal property parcels — these are agricultural FIELD units,
      not legal land-ownership parcels (use a county assessor source for those).
    - Areas outside the covered regions — the tool will honestly report no
      coverage rather than guess; do not retry the same out-of-coverage bbox.
    - Administrative boundaries (use ``fetch_administrative_boundaries``).

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
      Keep it small (a county or smaller); a continent-sized bbox returns far
      too many parcels. Example (Ames, Iowa cropland):
      ``(-93.70, 42.00, -93.60, 42.08)``.
    - ``dataset`` (str | None): force a specific source key
      (``"us_usda_cropland"``, ``"japan"``, ``"denmark"``). Default ``None``
      auto-selects the dataset whose coverage contains the bbox.

    **Returns:** A ``LayerURI`` (``layer_type="vector"``, ``role="context"``,
    ``units=None``) pointing at a FlatGeobuf of field polygons. Each feature
    carries a ``crop_name`` property (the source crop / land-type label where
    the dataset provides one). An AOI with coverage but no fields (e.g. urban /
    water) returns a valid 0-feature layer, not an error.

    **Cross-tool dependencies:**
    - Typically layered over flood / fire / drought hazard rasters as context.
    - Pairs with ``compute_zonal_statistics`` to summarize a hazard over fields.
    - Use ``fetch_administrative_boundaries`` first if you need a county/AOI
      bbox to query within.

    FR-CE-8: ``read_through`` with ``ttl_class="static-30d"``; cache key is
    SHA-256 over ``(bbox-6dp, dataset_key)``.
    """
    vbbox = _validate_bbox(bbox)
    q_bbox = _round_bbox_to_6dp(vbbox)

    # Selection raises FieldsNoCoverageError / FieldsInputError as appropriate.
    ds = _select_dataset(q_bbox, dataset)

    params = {
        "bbox": list(q_bbox),
        "dataset": ds.key,
    }
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_fields_bytes(ds, q_bbox),
    )
    assert result.uri is not None, (
        "fetch_field_boundaries is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=f"ftw-fields-{ds.key}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}",
        name=f"Field Boundaries — {ds.label}",
        layer_type="vector",
        uri=result.uri,
        style_preset="field_boundaries",
        role="context",
        units=None,
        bbox=q_bbox,
    )
