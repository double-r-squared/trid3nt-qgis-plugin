"""``fetch_usace_nsi`` atomic tool — USACE National Structure Inventory (job A6).

Wraps the U.S. Army Corps of Engineers (USACE) National Structure Inventory
(NSI) REST API to return per-building structure inventory features — point
geometries with HAZUS occupancy classification (``occtype``), structure
replacement value (``val_struct``), content value (``val_cont``), square
footage, number of stories, year built, FEMA flood zone, population residing
at AM/PM, and ground elevation. NSI is the authoritative U.S.-wide structure
inventory used by FEMA / USACE / FEMA HAZUS for flood loss estimation.

This is the **preferred** building+occupancy+replacement-value source for
``run_pelicun_damage_assessment`` / ``run_pelicun_with_buildings`` because every
NSI structure already carries the HAZUS ``occtype`` (e.g. ``RES1``, ``COM1``)
plus a real per-structure ``val_struct`` (USD), removing both the
``compute_building_density`` default-RES1 proxy AND the
``_REPLACEMENT_VALUE_DEFAULTS_USD`` per-class fallback. See "Cross-tool
dependency" below.

Endpoint (verified live 2026-06-09 against the USACE NSI API):

    POST https://nsi.sec.usace.army.mil/nsiapi/structures?fmt=fc
    Content-Type: application/json
    Body: a GeoJSON FeatureCollection with a single Polygon feature
        defining the area of interest (the API rejects GET ?bbox= with a
        500; the POST polygon path is the documented contract).

Response: a GeoJSON FeatureCollection of Point features (one per structure)
with ~40 NSI properties per record. Documented schema at
``https://www.hec.usace.army.mil/confluence/nsi/technicalreferences/2022``.

Properties preserved on the output FlatGeobuf (HAZUS + downstream Pelicun
consumers care about these; the rest of the NSI columns are dropped):

    ``fd_id``           NSI persistent feature ID
    ``occtype``         HAZUS occupancy class (RES1, RES3A, COM1, ...)
    ``st_damcat``       Structure damage category (RES, COM, IND, AGR, ...)
    ``bldgtype``        Building construction type (W, S, C, M, ...)
    ``found_type``      Foundation type (S, B, C, P, ...)
    ``found_ht``        Foundation height above ground (ft)
    ``num_story``       Number of stories
    ``sqft``            Building square footage
    ``med_yr_blt``      Median year built (Census block tract)
    ``val_struct``      Structure replacement value (USD)
    ``val_cont``        Content replacement value (USD)
    ``val_vehic``       Vehicle replacement value (USD)
    ``firmzone``        FEMA FIRM flood zone (e.g. AE, X, VE, or None)
    ``cbfips``          Census block FIPS code
    ``ground_elv``      Ground elevation (ft)
    ``ground_elv_m``    Ground elevation (m)
    ``pop2amu65``       AM population <65
    ``pop2amo65``       AM population >=65
    ``pop2pmu65``       PM population <65
    ``pop2pmo65``       PM population >=65
    ``students``        Student count (schools / EDU class)
    ``source``          NSI source tier (E=Estimated, P=Parcel, etc.)

Cache: ``static-30d``, ``source_class="usace_nsi"``. The NSI is rebuilt by
USACE on a ~quarterly cadence (the underlying parcel + Census + Microsoft
Buildings inputs change slowly), so a 30-day TTL is consistent with the
update cadence and matches HRSL / GCN250 / WDPA static-30d siblings.

``supports_global_query=False``: NSI only covers the United States and the
service rejects requests larger than ~1-2 degrees (a full-CONUS POST is
rejected with a 500). Always bbox-scoped.

Cross-tool dependency (codified in job A6):
``run_pelicun_with_buildings`` should prefer NSI when available. Replaces the
following Pelicun input limitations:
    - ``compute_building_density`` (Microsoft Buildings raster) emits a single
      ``component_type="RES1"`` per cell — NSI carries the real occupancy.
    - ``_REPLACEMENT_VALUE_DEFAULTS_USD`` (HAZUS-MH 4.2 class defaults) is a
      coarse per-class average — NSI carries the per-structure ``val_struct``.
This swap is documented in ``run_pelicun_damage_assessment``'s "Assets
convention" docstring as the v0.2 preferred path.

FR-TA-2 / FR-AS-3 / FR-CE-8 / FR-DC-3/4 invariants honored.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from typing import Any

import httpx

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_usace_nsi",
    "USACE_NSIError",
    "USACE_NSIInputError",
    "USACE_NSIUpstreamError",
    "USACE_NSIEmptyError",
    "estimate_payload_mb",
    "_build_nsi_polygon_body",
    "_bbox_to_polygon_feature",
    "_validate_bbox",
    "_round_bbox_to_6dp",
    "_fetch_nsi_geojson",
    "_geojson_to_fgb",
    "_fetch_nsi_bytes",
    "NSI_BBOX_MAX_SPAN_DEG",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.socioeconomic.fetch_usace_nsi")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class USACE_NSIError(RuntimeError):
    """Base class for ``fetch_usace_nsi`` failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "USACE_NSI_ERROR"
    retryable: bool = True


class USACE_NSIInputError(USACE_NSIError):
    """Caller passed an invalid bbox or oversized envelope.

    Not retryable — input validation failures are deterministic given the same
    inputs.
    """

    error_code = "USACE_NSI_INPUT_INVALID"
    retryable = False


class USACE_NSIUpstreamError(USACE_NSIError):
    """USACE NSI REST query failed (network, HTTP, or parse error).

    Retryable — NSI's API occasionally returns 5xx for transient backend
    issues; the agent's FR-AS-11 retry loop may surface the same query later.
    """

    error_code = "USACE_NSI_UPSTREAM_ERROR"
    retryable = True


class USACE_NSIEmptyError(USACE_NSIError):
    """NSI returned an empty FeatureCollection — informational, not retryable.

    NOT raised by the tool body (we serialize an empty FGB instead — a bbox
    over open water or remote wilderness with no NSI structures is
    LEGITIMATE), but kept available for future strict-mode opt-in.
    """

    error_code = "USACE_NSI_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_NSI_BASE = "https://nsi.sec.usace.army.mil/nsiapi/structures"

# Maximum bbox span (degrees) per axis. NSI's API rejects very large
# envelopes with a 500. ~1 degree corresponds to roughly a county-sized
# area; users wanting a whole state should iterate over sub-bboxes.
NSI_BBOX_MAX_SPAN_DEG: float = 1.0

# Properties preserved from each NSI feature. The full NSI schema has ~40
# columns; we keep the subset that the Pelicun consumer + LLM narration cares
# about. Anything not in this list is dropped from the FlatGeobuf row.
_PRESERVED_PROPERTIES: tuple[str, ...] = (
    "fd_id",
    "occtype",
    "st_damcat",
    "bldgtype",
    "found_type",
    "found_ht",
    "num_story",
    "sqft",
    "med_yr_blt",
    "val_struct",
    "val_cont",
    "val_vehic",
    "firmzone",
    "cbfips",
    "ground_elv",
    "ground_elv_m",
    "pop2amu65",
    "pop2amo65",
    "pop2pmu65",
    "pop2pmo65",
    "students",
    "source",
)

# Pelicun / Wave 2 contract: the consumer reads ``component_type`` (HAZUS
# occupancy class). NSI's ``occtype`` IS that vocabulary, so we duplicate it
# onto a ``component_type`` column so the downstream Pelicun tool's
# ``"component_type" in gdf.columns`` branch fires without remapping.
_PELICUN_COMPONENT_TYPE_COL = "component_type"
# Pelicun also looks for a ``replacement_value`` (USD) per asset; NSI's
# ``val_struct`` is the canonical value. We duplicate so the existing Pelicun
# branch (``rv = asset.get("replacement_value")``) picks it up unchanged.
_PELICUN_REPLACEMENT_VALUE_COL = "replacement_value"

# User-Agent — NSI's API is unauthenticated but identifying clients is polite
# (and useful when USACE asks who's hammering their cluster).
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

# Request timeout. NSI for a 1-degree bbox typically returns in a few seconds;
# the larger upper-bound queries can take ~30 s.
_HTTP_TIMEOUT_S = 60.0

# Estimated payload MB per the FR-DC-9 / Wave-1.5 estimator hook. Empirically
# the Fort Myers ~0.02-degree bbox returns ~50-100 KB; a 1-degree bbox can
# push 20-50 MB in dense urban areas. We report a conservative upper bound for
# the typical 0.1-degree city-scale query.
_ESTIMATED_PAYLOAD_MB_PER_DEG2: float = 50.0


def estimate_payload_mb(**args: Any) -> float:
    """FR-DC-9 / Wave-1.5 payload estimator hook (called by chat-warning gate).

    Estimates output size from the bbox area (square degrees), assuming a
    typical CONUS structure density. NSI for urban CONUS bboxes can run
    ~20-50 MB per square degree of dense area; lighter rural areas closer to
    ~1 MB. We use the dense-urban upper bound so the warning gate
    conservatively trips for queries that could blow past the 25 MB chat
    threshold.

    Signature matches the Wave-1.5 estimator convention (kwargs from the
    tool call site).
    """
    bbox = args.get("bbox")
    if bbox is None or len(bbox) != 4:
        # Conservative default for unknown bbox.
        return 10.0
    min_lon, min_lat, max_lon, max_lat = bbox
    span_deg2 = max(0.0, (max_lon - min_lon) * (max_lat - min_lat))
    return float(span_deg2 * _ESTIMATED_PAYLOAD_MB_PER_DEG2)


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
#
# ``supports_global_query=False`` (Wave 1.5 schema amendment, job-0114): NSI
# only covers the United States and rejects requests > ~1-2 degrees. The
# catalog/discovery layer must always supply a bbox.
# ---------------------------------------------------------------------------


_METADATA = AtomicToolMetadata(
    name="fetch_usace_nsi",
    ttl_class="static-30d",
    source_class="usace_nsi",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``USACE_NSIInputError`` if bbox is invalid or oversized."""
    if len(bbox) != 4:
        raise USACE_NSIInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise USACE_NSIInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise USACE_NSIInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise USACE_NSIInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise USACE_NSIInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    lon_span = max_lon - min_lon
    lat_span = max_lat - min_lat
    if lon_span > NSI_BBOX_MAX_SPAN_DEG or lat_span > NSI_BBOX_MAX_SPAN_DEG:
        raise USACE_NSIInputError(
            f"bbox span exceeds {NSI_BBOX_MAX_SPAN_DEG} degrees per axis "
            f"(lon_span={lon_span:.4f}, lat_span={lat_span:.4f}); NSI rejects "
            "oversized queries — split into tiles and call once per tile."
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _bbox_to_polygon_feature(
    bbox: tuple[float, float, float, float],
) -> dict[str, Any]:
    """Build a GeoJSON Polygon Feature wrapping the bbox.

    NSI's documented contract is POST of a FeatureCollection of polygon(s).
    The polygon is a CCW closed ring around the bbox.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    ring = [
        [min_lon, min_lat],
        [max_lon, min_lat],
        [max_lon, max_lat],
        [min_lon, max_lat],
        [min_lon, min_lat],
    ]
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {},
    }


def _build_nsi_polygon_body(
    bbox: tuple[float, float, float, float],
) -> dict[str, Any]:
    """Build the NSI POST body — a FeatureCollection with a single Polygon."""
    return {
        "type": "FeatureCollection",
        "features": [_bbox_to_polygon_feature(bbox)],
    }


# ---------------------------------------------------------------------------
# NSI HTTP fetch.
# ---------------------------------------------------------------------------


def _fetch_nsi_geojson(
    url: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """POST the NSI query and return parsed GeoJSON.

    Raises:
        ``USACE_NSIUpstreamError``: network / 5xx / non-JSON / non-FeatureCollection
        response.
    """
    logger.info("fetch_usace_nsi: POST %s", url)
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.post(
                url,
                params={"fmt": "fc"},
                json=body,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        raise USACE_NSIUpstreamError(
            f"NSI request failed url={url}: {exc}"
        ) from exc

    if resp.status_code >= 400:
        raise USACE_NSIUpstreamError(
            f"NSI returned HTTP {resp.status_code} url={url}: {resp.text[:500]!r}"
        )

    try:
        parsed = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise USACE_NSIUpstreamError(
            f"NSI returned non-JSON url={url}: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise USACE_NSIUpstreamError(
            f"NSI response is not a JSON object url={url}: type={type(parsed).__name__!r}"
        )

    # NSI may surface errors as {"message": "..."} in a 200/4xx body.
    if "message" in parsed and parsed.get("type") != "FeatureCollection":
        raise USACE_NSIUpstreamError(
            f"NSI returned error message url={url}: {parsed.get('message')!r}"
        )

    if parsed.get("type") != "FeatureCollection":
        raise USACE_NSIUpstreamError(
            f"NSI response is not a GeoJSON FeatureCollection url={url}: "
            f"type={parsed.get('type')!r}"
        )

    return parsed


# ---------------------------------------------------------------------------
# GeoJSON -> FlatGeobuf conversion.
# ---------------------------------------------------------------------------


def _geojson_to_fgb(geojson: dict[str, Any]) -> bytes:
    """Convert an NSI GeoJSON FeatureCollection to FlatGeobuf bytes.

    Preserves ``_PRESERVED_PROPERTIES`` and adds a ``component_type`` +
    ``replacement_value`` column for direct Pelicun consumption (NSI's
    ``occtype`` is the HAZUS occupancy class; ``val_struct`` is the structure
    USD value). Features without a point geometry are dropped. Always emits a
    valid FlatGeobuf — an empty input yields a header-only FGB.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise USACE_NSIUpstreamError(
            f"geopandas not available for FlatGeobuf conversion: {exc}"
        ) from exc

    features = geojson.get("features", []) or []

    cleaned: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if geom is None:
            continue
        props = feat.get("properties") or {}
        row_props: dict[str, Any] = {}
        for key in _PRESERVED_PROPERTIES:
            v = props.get(key)
            # Coerce non-scalar values to JSON strings — FlatGeobuf needs
            # scalar column types per field.
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
            row_props[key] = v
        # Pelicun-consumer convenience columns: duplicate occtype +
        # val_struct under the names Pelicun's
        # run_pelicun_damage_assessment branch reads
        # (``"component_type" in gdf.columns`` / ``asset.get("replacement_value")``).
        occtype = props.get("occtype")
        if isinstance(occtype, str) and occtype:
            row_props[_PELICUN_COMPONENT_TYPE_COL] = occtype
        else:
            row_props[_PELICUN_COMPONENT_TYPE_COL] = None
        val_struct = props.get("val_struct")
        if isinstance(val_struct, (int, float)) and math.isfinite(val_struct):
            row_props[_PELICUN_REPLACEMENT_VALUE_COL] = float(val_struct)
        else:
            row_props[_PELICUN_REPLACEMENT_VALUE_COL] = None
        cleaned.append({
            "type": "Feature",
            "properties": row_props,
            "geometry": geom,
        })

    all_columns = list(_PRESERVED_PROPERTIES) + [
        _PELICUN_COMPONENT_TYPE_COL,
        _PELICUN_REPLACEMENT_VALUE_COL,
    ]
    if not cleaned:
        gdf = gpd.GeoDataFrame(
            {k: [] for k in all_columns},
            geometry=gpd.GeoSeries([], crs="EPSG:4326"),
            crs="EPSG:4326",
        )
    else:
        gdf = gpd.GeoDataFrame.from_features(cleaned, crs="EPSG:4326")
        gdf = gdf.dropna(subset=["geometry"]).copy()

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_nsi_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise USACE_NSIUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} features: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_usace_nsi: FlatGeobuf = %d bytes (%d feature(s))",
            len(fgb_bytes),
            len(gdf),
        )
        return fgb_bytes
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# End-to-end fetcher (body build → HTTP → GeoJSON → FGB bytes).
# ---------------------------------------------------------------------------


def _fetch_nsi_bytes(
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Build body, POST to NSI, convert response to FlatGeobuf bytes."""
    body = _build_nsi_polygon_body(bbox)
    geojson = _fetch_nsi_geojson(_NSI_BASE, body)
    return _geojson_to_fgb(geojson)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public API endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_usace_nsi(
    bbox: tuple[float, float, float, float],
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """USACE National Structure Inventory as a point FlatGeobuf — buildings +
    HAZUS occupancy class + per-structure replacement value (USD).

    What it does: queries the U.S. Army Corps of Engineers (USACE) National
    Structure Inventory (NSI) REST API for every documented building inside
    a bbox and returns a FlatGeobuf of point features (one per structure),
    each carrying its HAZUS occupancy class (``occtype``), structure
    replacement value (``val_struct``, USD), content value (``val_cont``),
    square footage, year built, foundation type/height, FEMA FIRM flood
    zone, AM/PM population, and ground elevation. NSI is the authoritative
    U.S.-wide structure inventory used by FEMA / USACE / HAZUS for flood
    loss estimation.

    When to use:
        - User asks for the buildings or "structure inventory" inside an
          area (e.g. "show me every building in Fort Myers Beach", "what
          buildings are in the floodplain?").
        - As the asset layer for ``run_pelicun_damage_assessment`` /
          ``run_pelicun_with_buildings`` — NSI carries the HAZUS occupancy
          class AND per-structure replacement value, removing both the
          ``compute_building_density`` default-RES1 proxy AND the
          ``_REPLACEMENT_VALUE_DEFAULTS_USD`` per-class fallback. This is
          the **preferred** Pelicun asset substrate inside CONUS.
        - User asks for exposed value or repair cost estimates that need a
          real ``val_struct`` per structure.
        - Counting buildings by occupancy class (RES1 / COM1 / IND1 / EDU1
          / etc.) in a small area.

    When NOT to use:
        - Outside the United States — NSI only covers CONUS + AK + HI + the
          U.S. territories. Use ``compute_building_density`` (Microsoft
          Global ML Building Footprints) for international queries instead.
        - For bbox spans larger than ~1 degree per axis — NSI rejects
          oversized queries with a 500. Split into tiles and call once per
          tile, or use ``compute_building_density`` for the regional
          aggregate.
        - For aggregate building density rasters — ``compute_building_density``
          is the right primitive when you want a raster of building counts
          per cell, not point features.
        - For HRSL population grids — ``fetch_hrsl_population`` is the
          dedicated tool when you only need persons-per-cell, not per-
          structure attributes.

    Parameters:
        bbox: REQUIRED ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
            Valid range: ``min_lon`` in [-180, 180], ``max_lat`` in [-90, 90],
            min < max on each axis. Span on each axis must be ≤ 1.0 degree
            (NSI server-side cap; oversized envelopes return 500). Example:
            ``(-81.880, 26.620, -81.860, 26.660)`` for a Fort Myers Beach
            neighborhood (~0.02 deg, ~50-200 structures).

    Returns:
        A ``LayerURI`` pointing at a FlatGeobuf of NSI Point features (one
        per structure) at
        ``s3://trid3nt-cache/cache/static-30d/usace_nsi/<key>.fgb``.
        ``layer_type="vector"``, ``role="primary"``, ``units=None``,
        ``style_preset="usace_nsi"``. Each feature carries:

        - ``fd_id`` (int) — NSI persistent feature ID.
        - ``occtype`` (str) — HAZUS occupancy class (``RES1``, ``RES3A``,
          ``COM1``, ``IND1``, ``EDU1``, ...). Downstream Pelicun reads this.
        - ``component_type`` (str) — duplicate of ``occtype`` under the
          column name Pelicun's ``run_pelicun_damage_assessment`` looks for.
        - ``st_damcat`` (str) — Structure damage category (RES, COM, IND).
        - ``bldgtype`` (str) — Construction type (W=wood, S=steel, C=concrete,
          M=masonry).
        - ``found_type`` (str) — Foundation type (S=slab, B=basement,
          C=crawlspace, P=pier).
        - ``found_ht`` (float) — Foundation height above ground (ft).
        - ``num_story`` (int) — Number of stories.
        - ``sqft`` (float) — Building square footage.
        - ``med_yr_blt`` (int) — Median year built (Census block).
        - ``val_struct`` (float) — Structure replacement value (USD).
        - ``replacement_value`` (float) — duplicate of ``val_struct`` under
          the column name Pelicun reads.
        - ``val_cont`` (float) — Content replacement value (USD).
        - ``val_vehic`` (float) — Vehicle replacement value (USD).
        - ``firmzone`` (str|None) — FEMA FIRM flood zone (``AE``, ``X``,
          ``VE``, or None).
        - ``cbfips`` (str) — Census block FIPS code.
        - ``ground_elv`` / ``ground_elv_m`` (float) — Ground elevation
          (ft / m).
        - ``pop2amu65`` / ``pop2amo65`` / ``pop2pmu65`` / ``pop2pmo65``
          (int) — Population AM/PM, under/over 65.
        - ``students`` (int) — Student count (schools).
        - ``source`` (str) — NSI source tier (E=Estimated, P=Parcel, ...).

    Cross-tool dependencies:
        - Consumed by ``run_pelicun_damage_assessment`` (and the composer
          ``run_pelicun_with_buildings``): pass the returned ``LayerURI.uri``
          as ``assets_uri``. Because every NSI feature already carries
          ``component_type`` and ``replacement_value`` (added by this tool),
          the Pelicun damage loop uses real HAZUS occupancy + structure
          values rather than the ``"RES1"`` / class-default fallback. This
          is the **preferred** Pelicun asset substrate when the bbox is
          inside CONUS — the composer should prefer NSI when available and
          fall back to ``compute_building_density`` only outside the US.
        - Complementary to ``fetch_hrsl_population`` (population grid),
          ``fetch_administrative_boundaries`` (admin polygons for zonal
          aggregation), and ``fetch_roads_osm`` (road network) for full
          exposure-context layers.
        - Pair with ``run_model_flood_scenario`` output: use its flood
          depth COG as ``hazard_raster_uri`` in
          ``run_pelicun_damage_assessment(hazard_raster_uri=..., assets_uri=<this>)``.

    Cache: ``static-30d`` (the NSI is rebuilt by USACE on a ~quarterly
    cadence; a 30-day TTL matches the update rhythm). Cache key:
    SHA-256 of (bbox-rounded-6dp, "static-30d" vintage).

    External-API resilience (NFR-R-1): the NSI cluster occasionally returns
    5xx for transient backend issues. Network failure / non-2xx / malformed
    JSON / non-FeatureCollection responses raise
    ``USACE_NSIUpstreamError(retryable=True)`` so the agent's FR-AS-11
    surface decides whether to retry, clarify, or fall back.

    Source-tier: FR-HEP-2 Tier 1 (USACE-issued, authoritative U.S. structure
    inventory used by FEMA / HAZUS). Claims derived from this tool should
    be marked ``source_authority_tier=1`` in any ``ClaimSet`` aggregation.

    Payload estimation (FR-DC-9 / Wave-1.5 hook): see ``estimate_payload_mb``
    — bbox-area-driven, with the dense-urban upper bound (~50 MB / deg²).
    Typical city-scale ~0.1-degree queries land in single-digit MB; the
    full 1-degree max can push past 25 MB and trip the chat-warning gate.
    """
    if bbox is None:
        raise USACE_NSIInputError(
            "bbox is required — NSI does not support a global / CONUS sweep "
            "and the API rejects requests larger than ~1 degree per axis."
        )
    _validate_bbox(bbox)
    q_bbox = _round_bbox_to_6dp(bbox)

    params = {
        "bbox": list(q_bbox),
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_nsi_bytes(q_bbox),
    )
    assert result.uri is not None, (
        "fetch_usace_nsi is cacheable; uri must be set by read_through"
    )

    name = (
        f"USACE NSI Structures — bbox "
        f"({q_bbox[0]:.3f},{q_bbox[1]:.3f},{q_bbox[2]:.3f},{q_bbox[3]:.3f})"
    )
    layer_id = (
        f"usace-nsi-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-"
        f"{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
    )

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="usace_nsi",
        role="primary",
        units=None,
    )
