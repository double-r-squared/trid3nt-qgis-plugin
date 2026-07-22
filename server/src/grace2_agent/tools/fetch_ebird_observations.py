"""``fetch_ebird_observations`` atomic tool — Cornell Lab eBird Tier-2 fetcher (job-0128).

Wraps the Cornell Lab of Ornithology eBird API v2
(``https://api.ebird.org/v2/``) to return recent species sightings clipped to
a bbox as a FlatGeobuf. This is one of the Tier-2 conservation/biodiversity
fetchers for sprint-12 (see ``project_conservation_tool_stubs`` memo: the
Wave-2 Tier-2 substrate complements the three Tier-1 fetchers landed in
Wave 1 — GBIF, iNaturalist, WDPA).

API surface (verified 2026-06-08):

    Recent obs (geo):
        https://api.ebird.org/v2/data/obs/geo/recent/{species_code}
            ?lat={lat}&lng={lon}&dist={km}&back={days_back}&fmt=json
    Headers:
        X-eBirdApiToken: <api_key>

The geo endpoint returns recent observations within a ``dist`` (km) radius
around ``(lat, lng)`` over the last ``back`` days. eBird does NOT expose a
bbox query — we tile the requested bbox into ~50 km circles and dedupe by
``subId`` (eBird's stable submission identifier; one ``subId`` may carry
multiple species but each (subId, speciesCode) pair is unique). Sprint-13
will replace the row/col grid with a proper hex-tile cover; the v0.1 grid
intentionally overlaps so we don't miss thin slivers.

eBird API requires an API key (free, registration at
``https://ebird.org/api/keygen``). The agent resolves the key in this order:

1. Explicit ``api_key`` kwarg (live test path, dev override).
2. ``secret_ref`` ``SecretRecord`` → ``Persistence.get_secret_value()`` (the
   production per-Case path landed by Wave 2 sibling job-0124).
3. ``GRACE2_EBIRD_API_KEY`` env var (local dev convenience).

If none of the three resolve a key, the tool raises ``EBirdMissingKeyError``
(retryable=False) and the agent surface routes a "needs a key" message to
the user (per FR-AS-11 typed-error envelope).

FR-TA-2 atomic tool. FR-CE-8 / FR-DC-3/4: routed through ``read_through``
with ``ttl_class="dynamic-1h"`` — eBird updates rapidly as new checklists
arrive; an hourly window balances freshness against API quota (eBird
recommends sub-minute polling only for "rare bird alerts" use cases).

Cache key composition (per audit.md): SHA-256 of (species_code, bbox
rounded-6dp, days_back). The cache shim already factors in the dynamic-1h
``ttl_bucket_vintage`` so two calls inside the same hour reuse the cached
FlatGeobuf and a top-of-hour crossing forces a refresh.

The cache key intentionally does NOT include the api_key — the underlying
observations don't vary by caller. Per-user keying would defeat the cache.

Output FlatGeobuf schema:
    Geometry: Point (one feature per (subId, speciesCode) sighting)
    Properties:
        subId         (str)   — eBird submission id (stable, dedup key)
        obsDt         (str)   — observation datetime (eBird format: "YYYY-MM-DD HH:MM")
        locName       (str)   — eBird locality name ("Pine Island NWR", etc.)
        howMany       (int)   — observed count; null if "X" (presence-only)
        comName       (str)   — common name ("Bewick's Wren")
        sciName       (str)   — scientific name ("Thryomanes bewickii")
        speciesCode   (str)   — eBird 6-character species code (echo of request)

CRS: EPSG:4326 (eBird coordinates are WGS84 decimal degrees).

Tier-2 vs Tier-1 distinction (Decision F + project_conservation_tool_stubs):
- Tier-1 (GBIF / iNaturalist / WDPA): open APIs, no key.
- Tier-2 (eBird / IUCN Red List / Movebank): require API key; the agent
  resolves via ``secret_ref`` at invocation time.

Geographic-correctness gate (job-0086 codified lesson): every emitted point
must lie within the requested bbox. eBird's radius queries return points
along the circle's perimeter; the corner circles in our tile-cover overlap
the bbox edge by design, so a real-edge sighting may also land slightly
outside the requested bbox. We hard-filter on the requested bbox before
serialization so the contract is clean.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any

import httpx

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = ["fetch_ebird_observations"]

logger = logging.getLogger("grace2_agent.tools.fetch_ebird_observations")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class EBirdError(RuntimeError):
    """Base class for fetch_ebird_observations failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "EBIRD_ERROR"
    retryable: bool = True


class EBirdInputError(EBirdError):
    """Bad inputs (unknown species code, malformed bbox, etc.)."""

    error_code = "EBIRD_INPUT_ERROR"
    retryable = False


class EBirdUpstreamError(EBirdError):
    """eBird API returned 5xx or the network call failed."""

    error_code = "EBIRD_UPSTREAM_ERROR"
    retryable = True


class EBirdMissingKeyError(EBirdError):
    """No API key resolved via any of the three lookup paths.

    Raised BEFORE any network call. The agent surface uses this to prompt
    the user to add an eBird key via the secrets panel (sprint-12 Case-UX).
    """

    error_code = "EBIRD_MISSING_KEY"
    retryable = False


class EBirdAuthError(EBirdError):
    """eBird API returned 401/403 — key is invalid, revoked, or rate-limited.

    Distinct from ``EBirdMissingKeyError`` (which fires pre-network when no
    key resolves at all). This fires when a key resolved but the API
    rejected it — typically a revoked or rate-limited key.
    """

    error_code = "EBIRD_AUTH_ERROR"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_EBIRD_GEO_RECENT_URL_TMPL = (
    "https://api.ebird.org/v2/data/obs/geo/recent/{species_code}"
)

# Per-tile radius. eBird's max is 50 km; we use the max so the tile grid
# covers the bbox in the fewest possible API calls.
_TILE_RADIUS_KM = 50.0

# Per-request timeout. eBird normally responds within a few hundred ms; we
# pad generously for slow networks.
_TIMEOUT_S = 30.0

# Hard cap on tile count per call. A 1000 km × 1000 km bbox would tile into
# ~400 50-km circles; we cap at 200 tiles so a runaway caller asking for a
# whole-continent bbox doesn't burn through their eBird quota in one shot.
_MAX_TILES_HARD_CAP = 200

# User-Agent per eBird usage guidelines.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

# Default ``days_back`` per audit.md.
_DEFAULT_DAYS_BACK = 30

# eBird API caps days_back at 30. Audit.md explicitly notes this is the
# upper bound; we validate accordingly.
_DAYS_BACK_MAX = 30


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_ebird_observations",
    ttl_class="dynamic-1h",
    source_class="ebird",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# bbox helpers (mirror fetch_gbif_occurrences for consistency).
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``EBirdInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise EBirdInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise EBirdInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise EBirdInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise EBirdInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if west >= east or south >= north:
        raise EBirdInputError(
            f"bbox is degenerate (west < east, south < north required): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coords to 6dp (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _validate_species_code(species_code: str) -> None:
    """eBird species codes are short alphanumeric tokens (e.g. ``bewwre``).

    The 6-character lowercase-letters form is the most common, but eBird
    has historical 4-letter codes (``amro``) and some longer 7-character
    codes for subspecies. We accept any non-empty alphanumeric string of
    reasonable length — eBird itself returns 404 on an unknown code, which
    surfaces as ``EBirdInputError`` from the search path.
    """
    if not isinstance(species_code, str):
        raise EBirdInputError(
            f"species_code must be a str; got {type(species_code).__name__}"
        )
    sc = species_code.strip()
    if not sc:
        raise EBirdInputError("species_code must be non-empty")
    if len(sc) > 16:
        raise EBirdInputError(
            f"species_code too long (max 16 chars); got {sc!r}"
        )
    if not all(c.isalnum() for c in sc):
        raise EBirdInputError(
            f"species_code must be alphanumeric; got {sc!r}"
        )


def _validate_days_back(days_back: int) -> None:
    if not isinstance(days_back, int):
        raise EBirdInputError(
            f"days_back must be int; got {type(days_back).__name__}"
        )
    if days_back < 1:
        raise EBirdInputError(f"days_back must be >= 1; got {days_back}")
    if days_back > _DAYS_BACK_MAX:
        raise EBirdInputError(
            f"days_back exceeds eBird API max ({_DAYS_BACK_MAX}); got {days_back}"
        )


# ---------------------------------------------------------------------------
# Tile-cover: bbox → list of (lat, lng) circle centers.
# ---------------------------------------------------------------------------


def _bbox_to_tile_centers(
    bbox: tuple[float, float, float, float],
    radius_km: float = _TILE_RADIUS_KM,
) -> list[tuple[float, float]]:
    """Compute the set of circle centers that cover the bbox.

    eBird supports radius queries (``dist`` km) only; there is no bbox
    parameter. We tile the bbox into a grid of overlapping circles whose
    radius is ``radius_km`` and whose center-to-center spacing is
    ``radius_km`` (so adjacent circles overlap by ~50% — guarantees coverage
    of the full bbox interior including any sliver between tiles).

    The grid is in geographic coordinates: lon spacing varies with cos(lat)
    so the metric spacing is honored at the bbox's central latitude.
    Sprint-13 will replace this with a proper hex-tile cover (which packs
    more efficiently); the v0.1 row/col grid intentionally over-covers.

    Returns a list of ``(lat, lng)`` tuples — eBird's parameter order is
    ``(lat, lng)``, not ``(lng, lat)``.
    """
    west, south, east, north = bbox
    if radius_km <= 0:
        raise EBirdInputError(f"radius_km must be > 0; got {radius_km}")

    # Use the bbox center latitude to convert km → degrees lon. For
    # small-to-moderate bboxes this is accurate enough; for very tall
    # bboxes the cover slightly over-tiles toward the poles, which is
    # safe (more API calls, but no missed area).
    center_lat = 0.5 * (south + north)

    # 1 degree of latitude is ~110.574 km everywhere.
    deg_per_km_lat = 1.0 / 110.574
    # 1 degree of longitude is ~111.320 km × cos(lat) at latitude `lat`.
    deg_per_km_lon = 1.0 / (111.320 * max(0.01, math.cos(math.radians(center_lat))))

    step_deg_lat = radius_km * deg_per_km_lat
    step_deg_lon = radius_km * deg_per_km_lon

    # Number of rows / cols needed. We always emit at least 1 tile.
    n_rows = max(1, math.ceil((north - south) / step_deg_lat))
    n_cols = max(1, math.ceil((east - west) / step_deg_lon))

    # If the cover would exceed the hard cap, fall back to a single tile
    # at the bbox center — the caller is asking for too big an area.
    if n_rows * n_cols > _MAX_TILES_HARD_CAP:
        raise EBirdInputError(
            f"bbox tile cover would require {n_rows * n_cols} tiles "
            f"(max {_MAX_TILES_HARD_CAP}); request a smaller bbox"
        )

    centers: list[tuple[float, float]] = []
    for r in range(n_rows):
        # Distribute centers evenly inside the bbox so the first and last
        # are near the bbox edges (not exactly on; we use linspace-style
        # division).
        if n_rows == 1:
            lat = 0.5 * (south + north)
        else:
            lat = south + (r + 0.5) * (north - south) / n_rows
        for c in range(n_cols):
            if n_cols == 1:
                lng = 0.5 * (west + east)
            else:
                lng = west + (c + 0.5) * (east - west) / n_cols
            centers.append((lat, lng))
    return centers


# ---------------------------------------------------------------------------
# API-key resolution (FR-AS-11 + §F.3 per-Case secret path).
# ---------------------------------------------------------------------------


def _resolve_api_key(
    api_key: str | None,
    secret_ref: Any | None,
) -> str:
    """Return the live eBird API key from one of three lookup paths.

    Priority (per audit.md):

    1. Explicit ``api_key`` kwarg.
    2. ``secret_ref`` (a ``SecretRecord``) → ``Persistence.get_secret_value``
       (the per-Case path landed by Wave 2 sibling job-0124).
    3. ``GRACE2_EBIRD_API_KEY`` env var (dev convenience).

    Raises:
        ``EBirdMissingKeyError`` if none of the three paths produce a key.
    """
    # 1. Explicit kwarg.
    if api_key:
        return api_key

    # 2. secret_ref via Persistence.get_secret_value (sync wrapping of the
    #    async coroutine — the tool body is sync because read_through is
    #    sync; tier-2 fetcher convention is to bridge via asyncio.run when
    #    no event loop is running, else await directly).
    if secret_ref is not None:
        try:
            return _materialize_secret(secret_ref)
        except Exception as exc:  # noqa: BLE001 — surface as missing-key
            raise EBirdMissingKeyError(
                f"secret_ref lookup failed: {exc}"
            ) from exc

    # 3. Env var fallback.
    env_key = os.environ.get("GRACE2_EBIRD_API_KEY")
    if env_key:
        return env_key

    raise EBirdMissingKeyError(
        "no eBird API key available: pass api_key=..., secret_ref=..., "
        "or set the GRACE2_EBIRD_API_KEY env var. Register at "
        "https://ebird.org/api/keygen."
    )


def _materialize_secret(secret_ref: Any) -> str:
    """Bridge ``Persistence.get_secret_value`` (async) into a sync caller.

    The tool body is sync (cache.read_through is sync). When invoked from
    the agent's async event loop we cannot call ``asyncio.run`` (it would
    raise "cannot be called from a running event loop"); we use
    ``asyncio.new_event_loop`` on a worker thread in that case. Tests
    pass a synchronous mock that already returns the string, bypassing
    this path entirely.

    Lazy import of Persistence avoids a startup-time dep on MCP.
    """
    # Test-mock shortcut: if the caller passes something that quacks like a
    # ``str`` already (rare — but the test surface uses this to inject a
    # known key without standing up Persistence), accept it.
    if isinstance(secret_ref, str):
        return secret_ref

    from ..persistence import Persistence  # local — avoid top-level cycles

    persistence = _get_persistence_for_secrets()
    if persistence is None:
        raise EBirdMissingKeyError(
            "Persistence not bound; cannot resolve secret_ref. "
            "Pass api_key=... explicitly in this context."
        )

    coro = persistence.get_secret_value(secret_ref)
    return _run_coro_sync(coro)


# Module-level Persistence binding. The agent service sets this at startup
# via ``set_persistence_for_secrets`` so Tier-2 fetchers can resolve
# ``secret_ref`` without each tool importing the MCP client. Tests inject
# a mock via the same setter.
_PERSISTENCE_FOR_SECRETS: Any | None = None


def set_persistence_for_secrets(persistence: Any | None) -> None:
    """Bind the agent-service ``Persistence`` for secret materialization.

    Called once at startup by the agent service (parallels
    ``passthroughs.set_mcp_client``). Tests call this in a fixture and
    reset to ``None`` on teardown.
    """
    global _PERSISTENCE_FOR_SECRETS
    _PERSISTENCE_FOR_SECRETS = persistence


def _get_persistence_for_secrets() -> Any | None:
    return _PERSISTENCE_FOR_SECRETS


def _run_coro_sync(coro: Any) -> Any:
    """Run an ``asyncio`` coroutine and return its result from sync context.

    Uses ``asyncio.run`` when no event loop is running (test path, CLI
    path); falls back to a one-shot worker-thread loop when called from
    within a running loop (agent-runtime path). Both paths close the loop
    they create.
    """
    import asyncio
    import threading

    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False

    if not running:
        return asyncio.run(coro)

    # Already inside a loop — spin up a worker thread with its own loop.
    result_box: dict[str, Any] = {}
    error_box: dict[str, BaseException] = {}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            result_box["value"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001
            error_box["err"] = exc
        finally:
            loop.close()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if "err" in error_box:
        raise error_box["err"]
    return result_box["value"]


# ---------------------------------------------------------------------------
# Paginated eBird tile fetch.
# ---------------------------------------------------------------------------


def _fetch_one_tile(
    species_code: str,
    lat: float,
    lng: float,
    days_back: int,
    api_key: str,
    *,
    client: httpx.Client,
) -> list[dict[str, Any]]:
    """Fetch one tile's worth of recent observations from eBird.

    Returns the raw list of observation dicts. The eBird endpoint returns
    a single JSON array (no pagination).

    Raises:
        ``EBirdAuthError`` on 401/403 (bad/revoked key).
        ``EBirdInputError`` on 404 (unknown species code) or other 4xx.
        ``EBirdUpstreamError`` on 5xx / network failure.
    """
    url = _EBIRD_GEO_RECENT_URL_TMPL.format(species_code=species_code)
    params = {
        "lat": f"{lat:.6f}",
        "lng": f"{lng:.6f}",
        "dist": int(_TILE_RADIUS_KM),  # eBird wants integer km
        "back": days_back,
        "fmt": "json",
    }
    headers = {
        "X-eBirdApiToken": api_key,
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    try:
        resp = client.get(url, params=params, headers=headers)
    except httpx.RequestError as exc:
        raise EBirdUpstreamError(
            f"eBird network failure for tile lat={lat:.4f} lng={lng:.4f}: {exc}"
        ) from exc

    if resp.status_code in (401, 403):
        raise EBirdAuthError(
            f"eBird API rejected the key (status {resp.status_code}): "
            f"{resp.text[:200]}"
        )
    if resp.status_code == 404:
        raise EBirdInputError(
            f"eBird API returned 404 for species_code={species_code!r}: "
            f"{resp.text[:200]}"
        )
    if resp.status_code >= 500:
        raise EBirdUpstreamError(
            f"eBird API returned {resp.status_code} for tile "
            f"lat={lat:.4f} lng={lng:.4f}: {resp.text[:200]}"
        )
    if resp.status_code >= 400:
        raise EBirdInputError(
            f"eBird API returned {resp.status_code} for tile "
            f"lat={lat:.4f} lng={lng:.4f}: {resp.text[:200]}"
        )

    try:
        payload = resp.json()
    except ValueError as exc:
        raise EBirdUpstreamError(
            f"eBird returned non-JSON for tile lat={lat:.4f} lng={lng:.4f}: {exc}"
        ) from exc

    if not isinstance(payload, list):
        raise EBirdUpstreamError(
            f"eBird payload is not a list; got {type(payload).__name__}"
        )
    return payload


def _fetch_all_tiles(
    species_code: str,
    bbox: tuple[float, float, float, float],
    days_back: int,
    api_key: str,
) -> list[dict[str, Any]]:
    """Walk every tile center in the bbox cover and accumulate dedup'd records.

    Dedup key: ``subId`` (each eBird submission is unique; one submission
    may report many species but each (subId, speciesCode) pair is unique,
    and we're querying for a single species_code, so subId alone dedupes).

    Returns the list of de-duplicated observation dicts.
    """
    centers = _bbox_to_tile_centers(bbox)
    logger.info(
        "fetch_ebird_observations: tile-cover species=%s bbox=%s n_tiles=%d",
        species_code,
        bbox,
        len(centers),
    )

    seen_sub_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []

    with httpx.Client(
        timeout=_TIMEOUT_S,
        follow_redirects=True,
    ) as client:
        for idx, (lat, lng) in enumerate(centers):
            records = _fetch_one_tile(
                species_code=species_code,
                lat=lat,
                lng=lng,
                days_back=days_back,
                api_key=api_key,
                client=client,
            )
            new_count = 0
            for rec in records:
                sub_id = rec.get("subId")
                if not sub_id or not isinstance(sub_id, str):
                    # Defensive: a record without subId can still be useful.
                    # Synthesize a key from coords + timestamp.
                    sub_id = (
                        f"_anon::{rec.get('lat')}::{rec.get('lng')}::"
                        f"{rec.get('obsDt')}"
                    )
                if sub_id in seen_sub_ids:
                    continue
                seen_sub_ids.add(sub_id)
                deduped.append(rec)
                new_count += 1
            logger.info(
                "fetch_ebird_observations: tile %d/%d returned=%d new=%d "
                "total_deduped=%d",
                idx + 1,
                len(centers),
                len(records),
                new_count,
                len(deduped),
            )

    return deduped


# ---------------------------------------------------------------------------
# FlatGeobuf serialization.
# ---------------------------------------------------------------------------


def _records_to_flatgeobuf_bytes(
    records: list[dict[str, Any]],
    bbox: tuple[float, float, float, float],
    species_code: str,
) -> bytes:
    """Convert eBird observation dicts to a FlatGeobuf with the documented schema.

    Geographic-correctness gate (job-0086 codified lesson): every emitted
    point must lie WITHIN the requested bbox. eBird's radius queries return
    points along the circle perimeter; the corner circles in our tile cover
    overlap the bbox edge by design, so a real-edge sighting may also land
    slightly outside the requested bbox. We hard-filter so the contract is
    clean.

    Returns FlatGeobuf bytes (empty FlatGeobuf if no records survive).
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        import pandas as pd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise EBirdUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    west, south, east, north = bbox

    rows: list[dict[str, Any]] = []
    geoms: list[Any] = []
    skipped_missing_coords = 0
    skipped_outside_bbox = 0

    for rec in records:
        lng = rec.get("lng")
        lat = rec.get("lat")
        if lng is None or lat is None:
            skipped_missing_coords += 1
            continue
        try:
            lng_f = float(lng)
            lat_f = float(lat)
        except (TypeError, ValueError):
            skipped_missing_coords += 1
            continue
        if not (math.isfinite(lng_f) and math.isfinite(lat_f)):
            skipped_missing_coords += 1
            continue
        if not (west <= lng_f <= east and south <= lat_f <= north):
            skipped_outside_bbox += 1
            continue

        # eBird ``howMany`` is either an int or the string "X" (presence-only).
        how_many_raw = rec.get("howMany")
        try:
            how_many_int: int | None = (
                int(how_many_raw) if how_many_raw is not None else None
            )
        except (TypeError, ValueError):
            how_many_int = None

        rows.append({
            "subId": rec.get("subId") or "",
            "obsDt": rec.get("obsDt") or "",
            "locName": rec.get("locName") or "",
            "howMany": how_many_int,
            "comName": rec.get("comName") or "",
            "sciName": rec.get("sciName") or "",
            "speciesCode": rec.get("speciesCode") or species_code,
        })
        geoms.append(Point(lng_f, lat_f))

    if skipped_missing_coords:
        logger.info(
            "fetch_ebird_observations: skipped %d records with missing/invalid coords",
            skipped_missing_coords,
        )
    if skipped_outside_bbox:
        logger.info(
            "fetch_ebird_observations: filtered %d records outside requested bbox %s",
            skipped_outside_bbox,
            bbox,
        )

    if not rows:
        empty_df = pd.DataFrame(
            columns=[
                "subId",
                "obsDt",
                "locName",
                "howMany",
                "comName",
                "sciName",
                "speciesCode",
            ]
        )
        gdf = gpd.GeoDataFrame(empty_df, geometry=[], crs="EPSG:4326")
    else:
        df = pd.DataFrame(rows)
        gdf = gpd.GeoDataFrame(df, geometry=geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_ebird_"
        ) as fgb_f:
            tmp_fgb = fgb_f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise EBirdUpstreamError(
                f"FlatGeobuf write failed: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_ebird_observations: FlatGeobuf serialized %d feature(s) = %d bytes",
            len(rows),
            len(fgb_bytes),
        )
        return fgb_bytes
    finally:
        if tmp_fgb:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Fetch function (passed to read_through).
# ---------------------------------------------------------------------------


def _fetch_ebird_bytes(
    species_code: str,
    bbox: tuple[float, float, float, float],
    days_back: int,
    api_key: str,
) -> bytes:
    """Pipeline: tile-cover eBird → dedup → geographic-correctness filter → FlatGeobuf."""
    records = _fetch_all_tiles(
        species_code=species_code,
        bbox=bbox,
        days_back=days_back,
        api_key=api_key,
    )
    return _records_to_flatgeobuf_bytes(records, bbox, species_code)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    supports_global_query=False,
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls Cornell Lab eBird API external endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_ebird_observations(
    species_code: str,
    bbox: tuple[float, float, float, float],
    days_back: int = _DEFAULT_DAYS_BACK,
    api_key: str | None = None,
    secret_ref: Any | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Cornell Lab eBird Tier-2 recent-observations fetcher.

    Use this when: the agent needs recent bird sightings for ecological
    analysis or display — e.g. mapping Bewick's Wren observations over a
    wildfire footprint, overlaying recent waterfowl sightings on a flooded
    refuge, or summarizing avian biodiversity within a habitat-restoration
    bbox. Returns one FlatGeobuf point feature per (eBird subId, speciesCode)
    sighting reported in the last ``days_back`` days inside the requested
    bbox. eBird is the world's largest citizen-science bird database
    (>1B observations to date) — the right tool for "what birds were seen
    here recently".

    Do NOT use this for: HISTORICAL (>30 days back) sightings — eBird's API
    caps at 30 days; use GBIF or eBird's bulk-download for longer windows.
    Hot-spot checklists per location (different endpoint:
    ``data/obs/{regionCode}/recent/hotspot``). Rare-bird alerts (eBird has a
    different endpoint with sub-hour latency; we use the geo/recent endpoint
    deliberately for the dynamic-1h cache). Tier-1 GBIF queries (use
    ``fetch_gbif_occurrences`` for keyless species-occurrence fetch).
    Protected-area POLYGONS (use ``fetch_wdpa_protected_areas``). Tracking
    data (Movebank — different tool).

    eBird requires a free API key (registration at
    ``https://ebird.org/api/keygen``). The tool resolves the key in this
    order: (1) explicit ``api_key=`` kwarg, (2) ``secret_ref=`` per-Case
    secret via ``Persistence.get_secret_value`` (production path),
    (3) ``GRACE2_EBIRD_API_KEY`` env var (dev convenience). If none of the
    three resolve a key, raises ``EBirdMissingKeyError`` BEFORE any network
    call — the agent surface uses this to route a "needs a key" message
    via the secrets panel.

    eBird does NOT expose a bbox query — only radius queries around a
    ``(lat, lng)`` point. We tile the bbox into ~50 km circles (eBird's max
    radius) and dedupe by ``subId``. Per audit.md, sprint-13 will replace
    the row/col grid with a proper hex-tile cover; the v0.1 grid intentionally
    overlaps tiles so we don't miss slivers between rows/cols.

    Params:
        species_code: eBird 4-7 character species code (e.g. ``"bewwre"`` for
            Bewick's Wren, ``"amrob"`` for American Robin, ``"laggul"`` for
            Laughing Gull). Codes are at
            ``https://ebird.org/science/use-ebird-data/ebird-taxonomy``.
        bbox: ``(west, south, east, north)`` in EPSG:4326 (WGS84 decimal degrees).
        days_back: how many days of recent observations to return (1-30;
            default 30). eBird's API caps this at 30.
        api_key: optional explicit API key — highest-priority resolution path.
        secret_ref: optional ``SecretRecord`` (from per-Case secrets panel)
            — resolved via ``Persistence.get_secret_value`` at invocation time.

    Returns:
        A ``LayerURI`` pointing at a FlatGeobuf in the cache bucket:
        ``s3://trid3nt-cache/cache/dynamic-1h/ebird/<key>.fgb``
        containing the observation points clipped to the requested bbox.
        ``layer_type="vector"``, ``role="context"``, ``units=None``.

    Raises:
        ``EBirdMissingKeyError``: no API key resolved from any of the three paths.
        ``EBirdAuthError``: API rejected the key (revoked / rate-limited).
        ``EBirdInputError``: bad bbox, days_back, species_code, or unknown species.
        ``EBirdUpstreamError``: eBird API 5xx / network failure (retryable).

    FR-CE-8: Routed through ``read_through`` with ``ttl_class="dynamic-1h"``
    so identical ``(species_code, bbox, days_back)`` calls inside the same
    hour reuse the cached FlatGeobuf and a top-of-hour crossing forces a
    refresh. The cache key intentionally does NOT include the api_key —
    the underlying observations don't vary by caller.
    """
    # ---- Input validation ----
    _validate_bbox(bbox)
    _validate_species_code(species_code)
    _validate_days_back(days_back)

    species_code_clean = species_code.strip().lower()

    # ---- API-key resolution (pre-network; cheap fail) ----
    resolved_key = _resolve_api_key(api_key=api_key, secret_ref=secret_ref)

    # ---- Cache-key params (quantized; key omits api_key by design) ----
    q_bbox = _round_bbox_to_6dp(bbox)
    params: dict[str, Any] = {
        "species_code": species_code_clean,
        "bbox": list(q_bbox),
        "days_back": days_back,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_ebird_bytes(
            species_code=species_code_clean,
            bbox=q_bbox,
            days_back=days_back,
            api_key=resolved_key,
        ),
    )
    assert result.uri is not None, (
        "fetch_ebird_observations is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=f"ebird-{species_code_clean}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}",
        name=f"eBird Recent Sightings — {species_code_clean}",
        layer_type="vector",
        uri=result.uri,
        style_preset="ebird_observations",
        role="context",
        units=None,
    )
