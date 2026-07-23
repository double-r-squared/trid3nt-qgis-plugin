"""``fetch_iucn_red_list_range`` atomic tool — IUCN Red List range info Tier-2 fetcher.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any

import httpx

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.secrets import SecretRecord
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_iucn_red_list_range",
    "IUCNError",
    "IUCNInputError",
    "IUCNAuthError",
    "IUCNNotFoundError",
    "IUCNUpstreamError",
    "estimate_payload_mb",
    "set_persistence_for_secrets",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.biodiversity.fetch_iucn_red_list_range")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class IUCNError(RuntimeError):
    """Base class for fetch_iucn_red_list_range failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the
    agent surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "IUCN_ERROR"
    retryable: bool = True


class IUCNInputError(IUCNError):
    """Caller passed an invalid argument (empty species name, bad region)."""

    error_code = "IUCN_INPUT_INVALID"
    retryable = False


class IUCNAuthError(IUCNError):
    """No api_key / secret_ref / env var available, or the key was rejected.

    Never includes the offending key value in its message — only a hint at
    which resolution path was tried.
    """

    error_code = "IUCN_AUTH_ERROR"
    retryable = False


class IUCNNotFoundError(IUCNError):
    """Species lookup returned no result (empty ``result`` list).

    Not an exception in some sibling tools' contracts (e.g. fetch_gbif returns
    an empty FlatGeobuf for unknown species) — but per the kickoff IUCN's
    contract is "info or nothing", and an empty FlatGeobuf for a species the
    user explicitly named would suppress the "species not in database" signal.
    We return an empty single-feature FlatGeobuf (with ``category="DD"``-style
    placeholder + ``is_placeholder_geometry=True``) so callers can distinguish
    "unknown species" from "no data" by reading the empty payload field set.
    """

    error_code = "IUCN_NOT_FOUND"
    retryable = False


class IUCNUpstreamError(IUCNError):
    """IUCN API returned 5xx, network call failed, or response was malformed."""

    error_code = "IUCN_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_IUCN_BASE = "https://apiv3.iucnredlist.org/api/v3"

# Per-request timeout. The Red List API is generally fast (≤2s); we pad for
# upstream congestion.
_TIMEOUT_S = 30.0

# User-Agent per IUCN API courtesy guidelines (their docs ask for a
# contact-bearing UA).
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

# Env var fallback for the API key. Matches the kickoff verbatim.
_API_KEY_ENV_VAR = "TRID3NT_IUCN_RED_LIST_API_KEY"


# ---------------------------------------------------------------------------
# Module-level Persistence binding (job credential-pipeline-generic). The agent
# service binds the SAME ``Persistence`` here at startup (parallels eBird /
# FIRMS / era5 / gtsm) so a tool dispatched with a per-Case ``secret_ref`` can
# materialize the user's vault key WITHOUT the caller threading a sync
# ``persistence=`` resolver. The server only injects ``secret_ref`` (not a
# persistence object), so without this seam an IUCN secret_ref would dead-end.
# ---------------------------------------------------------------------------
_PERSISTENCE_FOR_SECRETS: Any | None = None


def set_persistence_for_secrets(persistence: Any | None) -> None:
    """Bind the agent-service ``Persistence`` for IUCN secret materialization.

    Called once at startup by the agent service (mirrors
    ``fetch_ebird_observations.set_persistence_for_secrets``). Tests set it in
    a fixture and reset to ``None`` on teardown.
    """
    global _PERSISTENCE_FOR_SECRETS
    _PERSISTENCE_FOR_SECRETS = persistence


def _get_persistence_for_secrets() -> Any | None:
    return _PERSISTENCE_FOR_SECRETS


def _run_coro_sync(coro: Any) -> Any:
    """Run an ``asyncio`` coroutine and return its result from a sync context.

    Uses ``asyncio.run`` when no loop is running (test / CLI path); falls back
    to a one-shot worker-thread loop when called from inside a running loop
    (agent-runtime path). Mirrors ``fetch_ebird_observations._run_coro_sync``.
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

# Maximum length of species_name we will pass through to the URL. IUCN
# binomial names rarely exceed ~60 chars; cap at 200 defensively to limit
# malformed-input attack surface.
_MAX_SPECIES_NAME_LEN = 200

# Default placeholder polygon half-extent (degrees) for the OQ-RANGE-SPATIAL
# stub geometry. ~1° ≈ 110 km — a coarse stand-in until real polygons land.
_PLACEHOLDER_HALF_DEGREES = 1.0

# Default placeholder centroid (lat, lon) when we have no region hint. We
# pick (0, 0) — the Gulf of Guinea null island — so the placeholder geometry
# is obviously *placeholder* on any visual inspection. The
# ``is_placeholder_geometry`` property is the authoritative signal.
_PLACEHOLDER_CENTROID = (0.0, 0.0)


# Estimated payload MB per the kickoff (FR-DC-9 Wave 1.5 payload estimator).
# IUCN species info is single-record JSON → tiny FlatGeobuf (<10 KB). Even
# when the Spatial Data ingest lands a typical multi-polygon range may be
# ~0.5 MB; we report the kickoff-specified upper bound so the
# tool-payload-warning system is calibrated for the future swap-in.
_ESTIMATED_PAYLOAD_MB = 0.5


def estimate_payload_mb(**args: Any) -> float:
    """FR-DC-9 / Wave-1.5 payload estimator hook (called by chat-warning gate).

    Returns the kickoff-specified ~0.5 MB upper bound per species range. The
    signature accepts ``**args`` to match the Wave-1.5 estimator convention
    (the chat-warning gate passes the tool's kwargs unchanged).
    """
    return _ESTIMATED_PAYLOAD_MB


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


_METADATA = AtomicToolMetadata(
    name="fetch_iucn_red_list_range",
    ttl_class="static-30d",
    source_class="iucn_red_list",
    cacheable=True,
    supports_global_query=True,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def _validate_species_name(name: str) -> str:
    """Clean + validate a species name; returns the normalized form.

    Trims whitespace, lower-cases (IUCN's URL routing is case-insensitive but
    cache key stability benefits from a single canonical form), and enforces
    a sanity bound on the length.
    """
    if not isinstance(name, str):
        raise IUCNInputError(
            f"species_name must be str; got {type(name).__name__}"
        )
    stripped = name.strip()
    if not stripped:
        raise IUCNInputError("species_name must be a non-empty string")
    if len(stripped) > _MAX_SPECIES_NAME_LEN:
        raise IUCNInputError(
            f"species_name exceeds maximum length {_MAX_SPECIES_NAME_LEN}: "
            f"got {len(stripped)} chars"
        )
    # IUCN URL routing tolerates spaces, but URL-encoding is what httpx does
    # for us — we just normalize whitespace runs to a single space.
    return " ".join(stripped.split())


def _validate_region(region: str) -> str:
    """Validate the IUCN region key.

    IUCN's documented regional assessment keys (per
    https://apiv3.iucnredlist.org/api/v3/region/list?token=...) include
    "global", "europe", "mediterranean", "northeastern_africa", "pan-africa",
    "northern_africa", "central_africa", "eastern_africa", "western_africa",
    "southern_africa", "persian_gulf", "arabian_sea". We accept any
    lowercase-letter / hyphen / underscore string — IUCN returns a clean 404
    for unknown regions, which we propagate as IUCNNotFoundError.
    """
    if not isinstance(region, str):
        raise IUCNInputError(
            f"region must be str; got {type(region).__name__}"
        )
    stripped = region.strip()
    if not stripped:
        raise IUCNInputError("region must be a non-empty string")
    # Defensive char-set check (no path separators, no spaces). Letters,
    # digits, hyphen, underscore only.
    if not all(c.isalnum() or c in "-_" for c in stripped):
        raise IUCNInputError(
            f"region contains illegal characters; expected [a-z0-9_-]+, got {stripped!r}"
        )
    return stripped.lower()


# ---------------------------------------------------------------------------
# API key resolution (the three-path waterfall).
# ---------------------------------------------------------------------------


def _resolve_api_key(
    api_key: str | None,
    secret_ref: SecretRecord | None,
    *,
    persistence: Any | None = None,
) -> str:
    """Resolve the IUCN API key via the documented three-path waterfall.

    Returns:
        The raw key value (never logged).

    Raises:
        ``IUCNAuthError`` if none of the three paths yields a non-empty key.
    """
    # Path 1: explicit ``api_key=`` argument. Highest precedence — caller
    # knows what they're doing.
    if api_key is not None:
        if not isinstance(api_key, str):
            raise IUCNAuthError(
                f"api_key must be str; got {type(api_key).__name__}"
            )
        if not api_key.strip():
            raise IUCNAuthError("api_key was provided but is empty")
        return api_key.strip()

    # Path 2: ``secret_ref=`` SecretRecord → Persistence.get_secret_value.
    # The Persistence object is the consumer-injected dependency; v0.1
    # invocation typically goes through the agent-service-bound singleton.
    if secret_ref is not None:
        if not isinstance(secret_ref, SecretRecord):
            raise IUCNAuthError(
                f"secret_ref must be SecretRecord; got {type(secret_ref).__name__}"
            )
        if secret_ref.provider != "iucn_red_list":
            raise IUCNAuthError(
                f"secret_ref provider mismatch: expected 'iucn_red_list', "
                f"got {secret_ref.provider!r}"
            )
        # job credential-pipeline-generic: the server injects ``secret_ref``
        # alone (not a ``persistence=`` resolver), so fall back to the
        # module-level Persistence seam the agent binds at startup — the same
        # vault->env pattern eBird / FIRMS / era5 / gtsm use.
        resolver = persistence if persistence is not None else (
            _get_persistence_for_secrets()
        )
        if resolver is None:
            # No persistence binding anywhere — surface the misuse so callers
            # wire the seam (rather than silently dead-ending on a vault key).
            raise IUCNAuthError(
                "secret_ref was provided but no Persistence resolver is bound; "
                "either pass api_key= directly or bind the agent's "
                "set_persistence_for_secrets seam"
            )
        try:
            # ``Persistence.get_secret_value`` is async; the tool body is sync.
            # Mocks may pass a sync resolver that returns the string directly.
            # If we get an awaitable (the production async Persistence), run it
            # synchronously on a worker-thread loop (mirrors eBird/FIRMS) rather
            # than rejecting it — that is exactly the agent-runtime path.
            result = resolver.get_secret_value(secret_ref)
            if hasattr(result, "__await__"):
                result = _run_coro_sync(result)
            if not isinstance(result, str) or not result.strip():
                raise IUCNAuthError(
                    "secret_ref resolved to an empty/non-string value"
                )
            return result.strip()
        except IUCNAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise IUCNAuthError(
                f"secret_ref resolution failed: {exc}"
            ) from exc

    # Path 3: env var fallback (local dev).
    env_key = os.environ.get(_API_KEY_ENV_VAR)
    if env_key and env_key.strip():
        return env_key.strip()

    raise IUCNAuthError(
        "no IUCN Red List API key resolved; pass api_key=, secret_ref=, "
        f"or set ${_API_KEY_ENV_VAR}"
    )


# ---------------------------------------------------------------------------
# IUCN API call.
# ---------------------------------------------------------------------------


def _fetch_iucn_species_payload(
    species_name: str,
    region: str,
    api_key: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Call the IUCN Red List API for one species + region.

    Endpoint selection:
        ``region == "global"`` → ``/species/{species_name}``
        otherwise              → ``/species/region/{species_name}/{region}``

    Returns the parsed JSON payload (with ``result`` list).

    Raises:
        ``IUCNAuthError`` on 401/403 (key rejected).
        ``IUCNUpstreamError`` on 5xx, network, parse failure.
        ``IUCNInputError`` on 4xx other than 401/403 (e.g. malformed name).
    """
    if region == "global":
        path = f"/species/{species_name}"
    else:
        path = f"/species/region/{species_name}/{region}"
    url = _IUCN_BASE + path

    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            timeout=_TIMEOUT_S,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        )
    try:
        try:
            resp = client.get(url, params={"token": api_key})
        except httpx.RequestError as exc:
            raise IUCNUpstreamError(
                f"IUCN network failure for species={species_name!r} "
                f"region={region!r}: {exc}"
            ) from exc

        if resp.status_code in (401, 403):
            raise IUCNAuthError(
                f"IUCN Red List API rejected the key (HTTP {resp.status_code}). "
                "Check the token at https://apiv3.iucnredlist.org/api/v3/token"
            )
        if resp.status_code >= 500:
            raise IUCNUpstreamError(
                f"IUCN returned {resp.status_code} for species={species_name!r} "
                f"region={region!r}"
            )
        if resp.status_code >= 400:
            # 4xx other than auth — typically malformed species name. IUCN
            # rarely 4xx; treat as input error.
            raise IUCNInputError(
                f"IUCN returned {resp.status_code} for species={species_name!r} "
                f"region={region!r}: {resp.text[:200]}"
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise IUCNUpstreamError(
                f"IUCN returned non-JSON for species={species_name!r} "
                f"region={region!r}: {exc}"
            ) from exc

        if not isinstance(payload, dict):
            raise IUCNUpstreamError(
                f"IUCN payload is not an object: {type(payload).__name__}"
            )

        # IUCN sometimes returns ``{"message": "Token not valid!"}`` with a
        # 200 OK when the token is bad. Treat that as auth failure.
        if "message" in payload and "result" not in payload:
            msg = str(payload.get("message", ""))
            if "token" in msg.lower():
                raise IUCNAuthError(
                    f"IUCN signaled token rejection in body: {msg!r}"
                )
            raise IUCNUpstreamError(
                f"IUCN returned an unexpected message-only payload: {msg!r}"
            )

        return payload
    finally:
        if owns_client:
            client.close()


# ---------------------------------------------------------------------------
# FlatGeobuf serialization.
# ---------------------------------------------------------------------------


def _placeholder_polygon(
    centroid: tuple[float, float],
    *,
    half_degrees: float = _PLACEHOLDER_HALF_DEGREES,
) -> Any:
    """Build a small WGS84 square polygon centred on ``centroid``.

    Lazy-imported shapely so test environments without shapely can still
    import the module.
    """
    from shapely.geometry import Polygon  # type: ignore[import-not-found]

    lat, lon = centroid
    # Clamp to WGS84 to avoid antimeridian / pole issues.
    west = max(-180.0, lon - half_degrees)
    east = min(180.0, lon + half_degrees)
    south = max(-90.0, lat - half_degrees)
    north = min(90.0, lat + half_degrees)
    return Polygon([
        (west, south),
        (east, south),
        (east, north),
        (west, north),
        (west, south),
    ])


def _payload_to_flatgeobuf_bytes(
    payload: dict[str, Any],
    species_name: str,
    region: str,
) -> tuple[bytes, bool]:
    """Convert an IUCN /species response into a FlatGeobuf (1 feature).

    Returns ``(fgb_bytes, found)`` where ``found`` is False if IUCN returned
    an empty ``result`` list (unknown species) — the caller decides whether
    to surface that as ``IUCNNotFoundError`` or as a "data-deficient"
    placeholder.

    The geometry is the OQ-0129-RANGE-SPATIAL placeholder square. The
    ``is_placeholder_geometry`` property is True at v0.1 for every feature;
    the Spatial Data swap-in will flip it.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        import pandas as pd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise IUCNUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    result = payload.get("result") or []
    if not isinstance(result, list):
        raise IUCNUpstreamError(
            f"IUCN 'result' is not a list: {type(result).__name__}"
        )

    # Build the property dict. Per the kickoff: even when IUCN returns no
    # match we still emit a single-feature FlatGeobuf carrying the
    # "data-deficient / not found" sentinel so downstream consumers see a
    # well-formed file rather than a schema mismatch.
    if result:
        rec = result[0] if isinstance(result[0], dict) else {}
        found = True
    else:
        rec = {}
        found = False

    def _g(k: str, default: Any = None) -> Any:
        v = rec.get(k)
        return v if v is not None else default

    def _f(k: str) -> float | None:
        v = rec.get(k)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    row: dict[str, Any] = {
        "taxonid": _g("taxonid"),
        "scientific_name": _g("scientific_name", species_name),
        "common_name": _g("main_common_name", ""),
        "kingdom": _g("kingdom", ""),
        "phylum": _g("phylum", ""),
        "class_name": _g("class", ""),  # 'class' is a Python keyword; use 'class_name' in FGB
        "order_name": _g("order_name", ""),
        "family": _g("family", ""),
        "category": _g("category", "DD" if not found else ""),
        "criteria": _g("criteria", ""),
        "population_trend": _g("population_trend", ""),
        "marine_system": bool(_g("marine_system", False)),
        "freshwater_system": bool(_g("freshwater_system", False)),
        "terrestrial_system": bool(_g("terrestrial_system", False)),
        "elevation_lower": _f("elevation_lower"),
        "elevation_upper": _f("elevation_upper"),
        "depth_lower": _f("depth_lower"),
        "depth_upper": _f("depth_upper"),
        "published_year": _g("published_year"),
        "assessment_date": _g("assessment_date", ""),
        "region": region,
        "is_placeholder_geometry": True,  # OQ-0129-RANGE-SPATIAL
    }

    geom = _placeholder_polygon(_PLACEHOLDER_CENTROID)
    gdf = gpd.GeoDataFrame(pd.DataFrame([row]), geometry=[geom], crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_iucn_"
        ) as fgb_f:
            tmp_fgb = fgb_f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise IUCNUpstreamError(
                f"FlatGeobuf write failed: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_iucn_red_list_range: serialized 1 feature "
            "(found=%s, category=%r) = %d bytes",
            found,
            row.get("category"),
            len(fgb_bytes),
        )
        return fgb_bytes, found
    finally:
        if tmp_fgb:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Fetch function (passed to read_through).
# ---------------------------------------------------------------------------


def _fetch_iucn_bytes(
    species_name: str,
    region: str,
    api_key: str,
) -> bytes:
    """Pipeline: call IUCN → validate response → serialize to FlatGeobuf."""
    payload = _fetch_iucn_species_payload(species_name, region, api_key)
    fgb_bytes, _found = _payload_to_flatgeobuf_bytes(
        payload, species_name=species_name, region=region
    )
    return fgb_bytes


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
def fetch_iucn_red_list_range(
    species_name: str,
    region: str = "global",
    api_key: str | None = None,
    secret_ref: SecretRecord | None = None,
    *,
    persistence: Any | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """IUCN Red List species range info Tier-2 fetcher.

    Use this when: the agent needs an authoritative species threat-status +
    range overlay for a Case 1 conservation analysis — e.g. cross-referencing
    a flood model footprint with the IUCN Red List range of a vulnerable
    mammal, or building a "species of concern within X km" overlay layer.
    Returns a single-feature FlatGeobuf carrying the IUCN assessment payload
    (category, criteria, population trend, habitat systems, elevation bounds)
    keyed on a placeholder square polygon — see ``OQ-0129-RANGE-SPATIAL``
    for the v0.2 Spatial Data swap-in plan.

    Do NOT use this for: GBIF/iNaturalist OCCURRENCE POINTS (use
    ``fetch_gbif_occurrences`` or ``fetch_inaturalist_observations``),
    actual range POLYGONS pre-v0.2 (the geometry here is a placeholder
    square — read ``is_placeholder_geometry``), live tracking data
    (Movebank — different tool), national checklist queries (IUCN's
    ``species/country`` endpoint is out of scope for v0.1), or
    bulk-corpus pulls (the Red List API is per-species; for bulk use the
    IUCN Red List Spatial Data zips out-of-band).

    Wraps the IUCN Red List API v3
    (``https://apiv3.iucnredlist.org/api/v3``). Tier-2 keyed — requires an
    IUCN Red List API key (free for research, sign up at
    ``https://apiv3.iucnredlist.org/api/v3/token``). The key resolves via
    one of three paths (waterfall):

    1. ``api_key="<str>"`` — explicit (CLI / direct invocation).
    2. ``secret_ref=<SecretRecord>`` — looked up via
       ``Persistence.get_secret_value(secret_ref)`` (per-Case keyed path,
       per job-0124).
    3. ``TRID3NT_IUCN_RED_LIST_API_KEY`` env var — local dev fallback.

    If none of the three resolve a non-empty key, ``IUCNAuthError`` is
    raised BEFORE any network call.

    Params:
        species_name: scientific binomial (e.g. ``"Puma concolor"``,
            ``"Panthera tigris"``). Case-insensitive; whitespace is
            normalized. Required.
        region: IUCN region key. Defaults to ``"global"`` (uses the global
            assessment endpoint). Other documented values include
            ``"europe"``, ``"mediterranean"``, ``"pan-africa"``, etc.
            See ``https://apiv3.iucnredlist.org/api/v3/region/list``.
        api_key: explicit IUCN API token (path 1).
        secret_ref: ``SecretRecord`` with ``provider="iucn_red_list"``
            (path 2). When passed without a ``persistence=`` resolver the
            tool raises rather than silently falling back to the env var.
        persistence: optional ``Persistence`` (or duck-typed equivalent
            with a sync ``get_secret_value(secret_ref) -> str`` method) for
            path 2 secret resolution. The production agent runtime binds
            this via the tool-binding seam; tests pass a mock.

    Returns:
        A ``LayerURI`` pointing at a FlatGeobuf in the cache bucket:
        ``s3://trid3nt-cache/cache/static-30d/iucn_red_list/<key>.fgb``
        carrying the species assessment. ``layer_type="vector"``,
        ``role="context"``, ``units=None``. The single feature carries the
        ``is_placeholder_geometry=True`` property; downstream consumers
        should treat the geometry as a sentinel until the Spatial Data
        ingest lands.

    Raises:
        ``IUCNInputError``: bad species name or region.
        ``IUCNAuthError``: no key resolved, or IUCN rejected the key.
        ``IUCNNotFoundError``: never raised at v0.1 — unknown species
            instead returns an FGB carrying ``category="DD"`` with the
            data-deficient sentinel populated. Reserved for v0.2 hardening.
        ``IUCNUpstreamError``: network / 5xx / parse failure (retryable).

    FR-CE-8: routed through ``read_through`` so identical
    ``(species_name_lower, region)`` calls within the 30-day window reuse
    the cached FlatGeobuf. The api_key value itself is NEVER part of the
    cache key.
    """
    # ---- Input validation ----
    norm_name = _validate_species_name(species_name)
    norm_region = _validate_region(region)

    # ---- API key resolution (auth gate BEFORE any network call) ----
    resolved_key = _resolve_api_key(
        api_key=api_key, secret_ref=secret_ref, persistence=persistence
    )

    # ---- Cache-key params: scientific name lowercased + region. The api_key
    # is NEVER part of the cache key (two callers with different keys for
    # the same species hit the same public response).
    cache_params: dict[str, Any] = {
        "species_name": norm_name.lower(),
        "region": norm_region,
    }

    result = read_through(
        metadata=_METADATA,
        params=cache_params,
        ext="fgb",
        fetch_fn=lambda: _fetch_iucn_bytes(
            species_name=norm_name,
            region=norm_region,
            api_key=resolved_key,
        ),
    )
    assert result.uri is not None, (
        "fetch_iucn_red_list_range is cacheable; uri must be set by read_through"
    )

    # Stable layer_id derived from the cache-key inputs (not the key itself).
    safe_name = "-".join(norm_name.lower().split())
    layer_id = f"iucn-{safe_name}-{norm_region}"
    display_name = (
        f"IUCN Red List — {norm_name}" + (f" ({norm_region})" if norm_region != "global" else "")
    )

    return LayerURI(
        layer_id=layer_id,
        name=display_name,
        layer_type="vector",
        uri=result.uri,
        style_preset="iucn_red_list_range",
        role="context",
        units=None,
    )
