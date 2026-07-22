"""``fetch_inaturalist_observations`` atomic tool — iNaturalist Tier-1 citizen-science fetcher (job-0088).

Wraps the iNaturalist API v1 (https://api.inaturalist.org/v1/observations) to
fetch vetted citizen-science occurrence points for a given taxon over a WGS84
bounding box, clipped optionally by an observation-date lookback window. Results
are paginated, serialized to FlatGeobuf points with species/date/observer/photo
properties, and routed through ``read_through`` (FR-DC-3 / FR-CE-8 shim) so the
30-day cache absorbs the API calls.

iNaturalist is Tier-1 free (no API key required); the public ``per_page`` cap is
200. ``quality_grade='research'`` returns only community-vetted observations
suitable for ecological analysis. The tool also accepts a *scientific or common
name string* as ``taxon_id`` and resolves it to an integer ID via the iNat taxa
endpoint (``/v1/taxa?q=...``).

FR-TA-2 atomic tool, returns ``LayerURI`` (vector, role="context", units=None).
FR-CE-8 / FR-DC-3 / FR-DC-4: identical ``(taxon, bbox, quality_grade, days_back,
max_records)`` calls reuse the cached FlatGeobuf within the 30-day window.

Pattern reference: ``fetch_administrative_boundaries.py`` (job-0084).

URL conventions (verified 2026-06-08):
    observations: https://api.inaturalist.org/v1/observations
    taxa search:  https://api.inaturalist.org/v1/taxa

The codified job-0086 lesson (URL/render consistency != geographic correctness)
applies here: the FlatGeobuf carries WGS84 point geometry direct from the iNat
``geojson`` field; the live verification asserts that returned points actually
fall **inside** the requested bbox (geographic-correctness check), not merely
that bytes round-trip.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_inaturalist_observations",
    "INatError",
    "INatInputError",
    "INatUpstreamError",
]

logger = logging.getLogger("grace2_agent.tools.fetch_inaturalist_observations")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class INatError(RuntimeError):
    """Base class for iNaturalist fetch failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the
    agent surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "INAT_ERROR"
    retryable: bool = True


class INatInputError(INatError):
    """Caller passed an invalid argument (bad bbox, unknown taxon name, ...)."""

    error_code = "INAT_INPUT_INVALID"
    retryable = False


class INatUpstreamError(INatError):
    """iNaturalist API call failed (network / HTTP / parse / rate-limit)."""

    error_code = "INAT_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_INAT_BASE = "https://api.inaturalist.org/v1"
_OBSERVATIONS_URL = f"{_INAT_BASE}/observations"
_TAXA_URL = f"{_INAT_BASE}/taxa"

# iNaturalist's public per-page cap (server-enforced; values above silently clamp).
_PER_PAGE = 200

# Default HTTP timeout (seconds). 30 per audit.md.
_HTTP_TIMEOUT = 30.0

# Hard cap on records returned by this tool in a single call. Per audit.md
# default is 5000; caller may override via ``max_records``.
_DEFAULT_MAX_RECORDS = 5000

# Polite User-Agent per iNat usage guidelines.
_USER_AGENT = (
    "grace-2/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/GRACE-2; agent@grace-2.dev)"
)

# Accepted quality grades.
_VALID_QUALITY_GRADES = frozenset({"research", "needs_id", "casual", "any"})

# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_inaturalist_observations",
    ttl_class="static-30d",
    source_class="inaturalist",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# bbox validation.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``INatInputError`` if bbox is invalid."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise INatInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise INatInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise INatInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise INatInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise INatInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Taxon-name resolution.
# ---------------------------------------------------------------------------


def _resolve_taxon_id(
    name: str,
    *,
    client: httpx.Client | None = None,
) -> int:
    """Resolve a taxon name (scientific or common) to an integer iNat taxon_id.

    Calls ``/v1/taxa?q={name}&per_page=1`` and returns the top hit's ``id``.

    Raises:
        ``INatInputError``: empty/blank name or no results returned.
        ``INatUpstreamError``: HTTP/parse failure.
    """
    if not name or not name.strip():
        raise INatInputError("taxon name must be a non-empty string")

    params = {"q": name.strip(), "per_page": 1}
    own_client = False
    if client is None:
        client = httpx.Client(timeout=_HTTP_TIMEOUT, headers={"User-Agent": _USER_AGENT})
        own_client = True
    try:
        try:
            resp = client.get(_TAXA_URL, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise INatUpstreamError(
                f"iNat taxa lookup failed for name={name!r}: {exc}"
            ) from exc
        try:
            payload = resp.json()
        except ValueError as exc:
            raise INatUpstreamError(
                f"iNat taxa lookup returned non-JSON for name={name!r}: {exc}"
            ) from exc
        results = payload.get("results") or []
        if not results:
            raise INatInputError(
                f"iNat taxa lookup returned no results for name={name!r}"
            )
        try:
            return int(results[0]["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise INatUpstreamError(
                f"iNat taxa top-hit missing/invalid id for name={name!r}: {exc}"
            ) from exc
    finally:
        if own_client:
            client.close()


def _coerce_taxon_id(
    taxon_id: int | str,
    *,
    client: httpx.Client | None = None,
) -> int:
    """Return an integer iNat taxon_id from ``taxon_id`` (int, digit-string, or name).

    - ``int`` → returned as-is.
    - ``str`` of digits → coerced via ``int()``.
    - other ``str`` → routed through ``_resolve_taxon_id``.
    """
    if isinstance(taxon_id, bool):  # bool is a subclass of int; reject explicitly
        raise INatInputError(f"taxon_id must be int or str, got bool: {taxon_id!r}")
    if isinstance(taxon_id, int):
        if taxon_id <= 0:
            raise INatInputError(f"taxon_id integer must be positive; got {taxon_id!r}")
        return taxon_id
    if isinstance(taxon_id, str):
        stripped = taxon_id.strip()
        if not stripped:
            raise INatInputError("taxon_id string must be non-empty")
        if stripped.isdigit():
            return int(stripped)
        return _resolve_taxon_id(stripped, client=client)
    raise INatInputError(
        f"taxon_id must be int or str; got {type(taxon_id).__name__}: {taxon_id!r}"
    )


# ---------------------------------------------------------------------------
# Observation fetch + pagination.
# ---------------------------------------------------------------------------


def _build_observations_params(
    taxon_id_int: int,
    bbox: tuple[float, float, float, float],
    quality_grade: str,
    days_back: int | None,
    page: int,
) -> dict[str, Any]:
    """Build the query-params dict for ``/v1/observations``."""
    min_lon, min_lat, max_lon, max_lat = bbox
    params: dict[str, Any] = {
        "taxon_id": taxon_id_int,
        "swlat": min_lat,
        "swlng": min_lon,
        "nelat": max_lat,
        "nelng": max_lon,
        "quality_grade": quality_grade,
        "per_page": _PER_PAGE,
        "page": page,
        # Geo points only — observations without coords are not useful here.
        "geo": "true",
    }
    if days_back is not None:
        # iNat accepts &d1=YYYY-MM-DD as "observed on or after"
        d1 = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        params["d1"] = d1
    return params


def _extract_observation_record(obs: dict[str, Any]) -> dict[str, Any] | None:
    """Project the iNat observation dict to the FlatGeobuf record schema.

    Returns ``None`` if the observation lacks a usable geographic point.

    Output fields (audit.md):
        ``id``, ``observed_on``, ``user_login``, ``photo_url``,
        ``species_guess``, ``place_guess``

    Plus geometry: ``(lon, lat)`` in EPSG:4326.
    """
    geojson = obs.get("geojson") or {}
    coords = geojson.get("coordinates")
    if (
        not isinstance(coords, (list, tuple))
        or len(coords) < 2
        or coords[0] is None
        or coords[1] is None
    ):
        return None
    try:
        lon = float(coords[0])
        lat = float(coords[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None

    # First photo URL (medium-size square preferred when present).
    photo_url: str | None = None
    photos = obs.get("photos") or []
    if isinstance(photos, list) and photos:
        first = photos[0] or {}
        if isinstance(first, dict):
            photo_url = first.get("url") or first.get("medium_url") or None

    user_login: str | None = None
    user = obs.get("user") or {}
    if isinstance(user, dict):
        user_login = user.get("login") or user.get("login_exact") or None

    return {
        "id": obs.get("id"),
        "observed_on": obs.get("observed_on"),
        "user_login": user_login,
        "photo_url": photo_url,
        "species_guess": obs.get("species_guess"),
        "place_guess": obs.get("place_guess"),
        "lon": lon,
        "lat": lat,
    }


def _fetch_observation_records(
    taxon_id_int: int,
    bbox: tuple[float, float, float, float],
    quality_grade: str,
    days_back: int | None,
    max_records: int,
    *,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Page through ``/v1/observations`` until exhausted or ``max_records`` reached.

    Raises:
        ``INatUpstreamError``: HTTP / parse / pagination failure.
    """
    own_client = False
    if client is None:
        client = httpx.Client(timeout=_HTTP_TIMEOUT, headers={"User-Agent": _USER_AGENT})
        own_client = True

    records: list[dict[str, Any]] = []
    try:
        page = 1
        total_results: int | None = None
        # Hard upper-bound on page iterations to defend against a misbehaving
        # upstream that never decreases (defensive belt-and-braces; the
        # per_page=200 cap means even 5000 records take <=25 pages).
        max_pages = max(1, (max_records + _PER_PAGE - 1) // _PER_PAGE) + 2
        while True:
            if page > max_pages:
                logger.warning(
                    "fetch_inaturalist_observations: page cap %d reached; stopping",
                    max_pages,
                )
                break
            params = _build_observations_params(
                taxon_id_int, bbox, quality_grade, days_back, page
            )
            logger.info(
                "fetch_inaturalist_observations: GET %s page=%d", _OBSERVATIONS_URL, page
            )
            try:
                resp = client.get(_OBSERVATIONS_URL, params=params)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise INatUpstreamError(
                    f"iNat observations call failed page={page}: {exc}"
                ) from exc
            try:
                payload = resp.json()
            except ValueError as exc:
                raise INatUpstreamError(
                    f"iNat observations non-JSON response page={page}: {exc}"
                ) from exc

            results = payload.get("results") or []
            if not isinstance(results, list):
                raise INatUpstreamError(
                    f"iNat observations 'results' is not a list page={page}: "
                    f"{type(results).__name__}"
                )

            if total_results is None:
                # Cache the upstream-reported total_results on the first page so
                # we know when to stop. If the field is missing, treat as
                # "stop when an empty page returns".
                tr = payload.get("total_results")
                total_results = int(tr) if isinstance(tr, int) else None

            for obs in results:
                if not isinstance(obs, dict):
                    continue
                rec = _extract_observation_record(obs)
                if rec is not None:
                    records.append(rec)
                if len(records) >= max_records:
                    break

            if len(records) >= max_records:
                break
            if not results:
                # Empty page → no more data.
                break
            if total_results is not None and (page * _PER_PAGE) >= total_results:
                break

            page += 1
    finally:
        if own_client:
            client.close()

    return records


# ---------------------------------------------------------------------------
# FlatGeobuf serialization.
# ---------------------------------------------------------------------------


def _records_to_flatgeobuf_bytes(records: list[dict[str, Any]]) -> bytes:
    """Serialize ``records`` to FlatGeobuf bytes via geopandas / pyogrio.

    Each record contributes one ``Point(lon, lat)`` feature with the audit.md
    property schema. CRS is EPSG:4326.

    Empty ``records`` still produces a valid (empty) FlatGeobuf so the cache
    write succeeds — downstream callers can identify the empty case by
    decoding the FGB; we never write a sentinel.

    Raises:
        ``INatUpstreamError``: serialization failure.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise INatUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    if records:
        geometries = [Point(r["lon"], r["lat"]) for r in records]
        attrs = [
            {
                "id": r["id"],
                "observed_on": r.get("observed_on"),
                "user_login": r.get("user_login"),
                "photo_url": r.get("photo_url"),
                "species_guess": r.get("species_guess"),
                "place_guess": r.get("place_guess"),
            }
            for r in records
        ]
        gdf = gpd.GeoDataFrame(attrs, geometry=geometries, crs="EPSG:4326")
    else:
        # Empty GeoDataFrame with the same schema, EPSG:4326. We pin the
        # column types to str/object so pyogrio doesn't infer divergent
        # dtypes on the empty case.
        import pandas as pd  # type: ignore[import-not-found]

        empty_df = pd.DataFrame(
            {
                "id": pd.Series(dtype="Int64"),
                "observed_on": pd.Series(dtype="object"),
                "user_login": pd.Series(dtype="object"),
                "photo_url": pd.Series(dtype="object"),
                "species_guess": pd.Series(dtype="object"),
                "place_guess": pd.Series(dtype="object"),
            }
        )
        gdf = gpd.GeoDataFrame(empty_df, geometry=gpd.GeoSeries([], crs="EPSG:4326"), crs="EPSG:4326")

    import os
    import tempfile

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False, prefix="grace2_inat_") as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001 — translate to typed error
            raise INatUpstreamError(
                f"FlatGeobuf write failed: {exc}"
            ) from exc
        with open(tmp_fgb, "rb") as f:
            return f.read()
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Cache-key + fetch wrapper.
# ---------------------------------------------------------------------------


def _fetch_inat_bytes(
    taxon_id_int: int,
    bbox: tuple[float, float, float, float],
    quality_grade: str,
    days_back: int | None,
    max_records: int,
) -> bytes:
    """The miss-path fetcher passed to ``read_through``.

    Resolves taxon_id (if a name was passed it was already resolved upstream so
    we receive only an int here), pages through observations, serializes to
    FlatGeobuf, returns bytes.
    """
    records = _fetch_observation_records(
        taxon_id_int=taxon_id_int,
        bbox=bbox,
        quality_grade=quality_grade,
        days_back=days_back,
        max_records=max_records,
    )
    logger.info(
        "fetch_inaturalist_observations: fetched %d record(s) for taxon_id=%d bbox=%s",
        len(records),
        taxon_id_int,
        bbox,
    )
    return _records_to_flatgeobuf_bytes(records)


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
def fetch_inaturalist_observations(
    taxon_id: int | str,
    bbox: tuple[float, float, float, float],
    quality_grade: str = "research",
    days_back: int | None = None,
    max_records: int = _DEFAULT_MAX_RECORDS,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch iNaturalist citizen-science observation points for a taxon over a bbox.

    **What it does:** Pages through ``api.inaturalist.org/v1/observations`` for
    a given taxon and bounding box, optionally restricted by quality grade and
    observation-date lookback window. Returns a FlatGeobuf Point layer with
    species name, observation date, observer login, and photo URL per feature.
    Accepts taxon as an integer ID or a scientific/common name (resolved via
    ``/v1/taxa``). No API key required. Cached ``static-30d``.

    **When to use:**
    - Agent needs recent, community-vetted species sightings for a study area
      (e.g. manatee distribution in coastal Florida over the last 30 days).
    - Conservation analysis requires observations with photo evidence and
      observer metadata (iNat provides more detail than GBIF for recent sightings).
    - ``days_back`` filter enables near-real-time species-response monitoring
      after a disturbance event (e.g. "bird sightings in the week after the fire").

    **When NOT to use:**
    - Complete species distribution modeling requiring museum specimens or eDNA
      records (use ``fetch_gbif_occurrences`` for broader source pool).
    - Threatened-species taxa with location-obfuscated coordinates (iNat policy
      randomly offsets exact locations; returned points may not match field truth).
    - Species status assessments (use IUCN Red List).
    - Protected-area boundaries (use ``fetch_wdpa_protected_areas``).

    **Parameters:**
    - ``taxon_id`` (int or str): integer iNat taxon ID (e.g. ``43616`` for West
      Indian manatee) OR scientific/common name string resolved via ``/v1/taxa``.
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
      Example: ``(-87.5, 24.5, -80.0, 27.5)`` for Florida coastal waters.
    - ``quality_grade`` (str): ``"research"`` (default; community-vetted),
      ``"needs_id"``, ``"casual"``, or ``"any"``.
    - ``days_back`` (int or None): restrict to last N days from today. ``None``
      returns all-time observations. Valid positive integers only.
    - ``max_records`` (int): per-call cap; default 5000.

    **Returns:**
    ``LayerURI(layer_type="vector", role="context", units=None)`` pointing at a
    FlatGeobuf with fields: ``id`` (int), ``observed_on`` (str), ``user_login``
    (str), ``photo_url`` (str), ``species_guess`` (str), ``place_guess`` (str).

    **Cross-tool dependencies:**
    - Related: ``fetch_gbif_occurrences`` (broader multi-source occurrence data).
    - Pairs with: ``fetch_wdpa_protected_areas`` (inside/outside protected area),
      ``fetch_mtbs_burn_severity`` (post-fire species presence).
    - Name resolution uses ``/v1/taxa`` internally; same taxon_id integer and
      name string collapse to the same cache entry.
    """
    # 1. Input validation.
    _validate_bbox(bbox)
    if quality_grade not in _VALID_QUALITY_GRADES:
        raise INatInputError(
            f"unknown quality_grade={quality_grade!r}; allowed: "
            f"{sorted(_VALID_QUALITY_GRADES)}"
        )
    if days_back is not None:
        if isinstance(days_back, bool) or not isinstance(days_back, int):
            raise INatInputError(
                f"days_back must be int or None; got {type(days_back).__name__}"
            )
        if days_back <= 0:
            raise INatInputError(f"days_back must be positive; got {days_back!r}")
    if isinstance(max_records, bool) or not isinstance(max_records, int):
        raise INatInputError(
            f"max_records must be int; got {type(max_records).__name__}"
        )
    if max_records <= 0:
        raise INatInputError(f"max_records must be positive; got {max_records!r}")

    # 2. Resolve taxon to int BEFORE cache-key computation so name + id
    #    collapse onto the same key (audit.md cache-key spec).
    taxon_id_int = _coerce_taxon_id(taxon_id)

    # 3. Quantize bbox to 6dp for cache-key stability.
    q_bbox = _round_bbox_to_6dp(bbox)

    # 4. read_through with the resolved taxon_id_int.
    params = {
        "taxon_id": taxon_id_int,
        "bbox": list(q_bbox),
        "quality_grade": quality_grade,
        "days_back": days_back,
        "max_records": max_records,
    }
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_inat_bytes(
            taxon_id_int=taxon_id_int,
            bbox=q_bbox,
            quality_grade=quality_grade,
            days_back=days_back,
            max_records=max_records,
        ),
    )
    assert result.uri is not None, (
        "fetch_inaturalist_observations is cacheable; uri must be set by read_through"
    )

    # 5. LayerURI shape per audit.md: vector / context / units=None.
    label = f"iNat Observations — taxon={taxon_id_int}"
    if quality_grade != "research":
        label += f" ({quality_grade})"
    return LayerURI(
        layer_id=f"inat-{taxon_id_int}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}",
        name=label,
        layer_type="vector",
        uri=result.uri,
        style_preset="inaturalist_observations",
        role="context",
        units=None,
        bbox=q_bbox,
    )
