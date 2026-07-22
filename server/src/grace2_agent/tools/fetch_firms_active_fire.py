"""``fetch_firms_active_fire`` atomic tool — NASA FIRMS active fire / thermal anomaly fetcher (job-0108).

Wraps the NASA FIRMS (Fire Information for Resource Management System) Web
Service "AREA API" endpoint::

    https://firms.modaps.eosdis.nasa.gov/api/area/csv/<MAP_KEY>/<source>/<bbox>/<days>

The API returns CSV rows of satellite-detected active-fire / thermal-anomaly
pixels (one row per detection) which this tool parses into a FlatGeobuf
``Point`` vector with brightness / FRP / confidence properties — Tier-B vector
ready for the QGIS Server / map surface.

Supported NRT sources (Sprint-12-mega Wave 1.5):
    "VIIRS_SNPP_NRT"   — Suomi NPP VIIRS, 375m, default (recommended for v0.1).
    "VIIRS_NOAA20_NRT" — NOAA-20 (JPSS-1) VIIRS, 375m.
    "MODIS_NRT"        — Terra/Aqua MODIS, 1 km.

``days_back`` is clamped to ``1..10`` per the FIRMS Web Service kickoff spec.
LIVE upstream behaviour as of 2026-06-08 is stricter — the AREA endpoint
rejects ``days_back > 5`` with HTTP 400 "Invalid day range. Expects [1..5]".
The tool accepts the kickoff range (1..10) for forward-compatibility but
surfaces upstream 400s as ``FirmsUpstreamError``. See OQ-0108-DAYS-RANGE.

Historical-date positional (fire-animation demo S2/J2):
    The AREA endpoint accepts an OPTIONAL trailing ``/{YYYY-MM-DD}`` start-date
    segment so a SPECIFIC PAST date works (not just the rolling-window
    ``days_back`` from FIRMS "today"). When ``date`` is supplied the tool builds
    ``.../<source>/<bbox>/<days>/<YYYY-MM-DD>`` and forces ``day_range=1`` so the
    result is exactly that one acquisition day. This is what unblocks the
    co-registered hot-pixel overlay for a recreate-this-animation prompt over a
    past window (e.g. 2026-06-22 for the GOES demo, or 2026-05-15..05-19 day-by-
    day for the JPSS Santa Rosa demo). Backward compatible: ``date=None`` (the
    default) keeps the original rolling-``days_back`` URL byte-identical.

MAP_KEY (FR-AS-11 / OQ surfaced):
    NASA FIRMS requires a free MAP_KEY via the self-serve registration page at
    https://firms.modaps.eosdis.nasa.gov/api/map_key/. The kickoff specifies
    "for v0.1 use 'demo' key (rate-limited)"; live testing on 2026-06-08
    confirmed the FIRMS endpoint rejects ``demo`` / ``DEMO_KEY`` with
    ``"Invalid MAP_KEY."`` (a real production key is required even for the
    rate-limited demo path). This tool therefore reads the key from the
    ``GRACE2_FIRMS_MAP_KEY`` env var, falling back to the literal ``"demo"``
    (which will fail upstream and surface as a typed ``FirmsAuthError`` rather
    than a silent zero-feature result). See OQ-0108-MAP-KEY-AUTH.

FR-TA-2: atomic tool returning ``LayerURI``.
FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical
``(source, bbox, days_back, MAP_KEY)`` calls reuse the cached FlatGeobuf
(``dynamic-1h`` so active fires don't go stale within a session).

Wave 1.5 ``supports_global_query`` opt-out:
    FIRMS requires a bbox — passing ``-180,-90,180,90`` is rejected by the
    AREA endpoint. The metadata advertises ``supports_global_query=False``
    so the orchestrator never routes a "global active fires" request here.
    Built defensively against the parallel job-0114-schema field-add (the
    same pattern used by ``fetch_mrms_qpe`` / ``fetch_nws_alerts_conus``).
"""

from __future__ import annotations

import io
import logging
import math
import os
import tempfile
from datetime import datetime
from typing import Literal, Any

import requests

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_firms_active_fire",
    "FirmsError",
    "FirmsArgError",
    "FirmsAuthError",
    "FirmsMissingKeyError",
    "FirmsUpstreamError",
    "FirmsEmptyError",
    "set_persistence_for_secrets",
    "_build_firms_url",
    "_validate_date",
]

logger = logging.getLogger("grace2_agent.tools.fetch_firms_active_fire")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class FirmsError(RuntimeError):
    """Base class for fetch_firms_active_fire failures."""

    error_code: str = "FIRMS_ERROR"
    retryable: bool = True


class FirmsArgError(FirmsError):
    """Invalid argument (unknown source, bad days_back, bad bbox)."""

    error_code = "FIRMS_ARG_INVALID"
    retryable = False


class FirmsAuthError(FirmsError):
    """FIRMS rejected the MAP_KEY (missing / invalid / over rate limit).

    Fires when a key resolved but the upstream rejected it. The agent surface
    (server.py) treats ``FIRMS_AUTH_ERROR`` as a missing/invalid-credential
    signal and pauses to emit a ``credential-request`` envelope (job:
    AUTH-ERROR -> CREDENTIAL-REQUEST) so the user can paste a valid MAP_KEY,
    which is then read from the vault on retry.
    """

    error_code = "FIRMS_AUTH_ERROR"
    retryable = False


class FirmsMissingKeyError(FirmsError):
    """No FIRMS MAP_KEY resolved via vault, env, or the demo fallback path.

    Distinct from ``FirmsAuthError`` (which fires when a key resolved but the
    upstream rejected it). Reserved for callers that opt out of the
    ``demo``-literal fallback and want a hard pre-network failure when no real
    key is available — the agent surface treats ``FIRMS_MISSING_KEY`` the same
    way it treats ``FIRMS_AUTH_ERROR``: a credential-request prompt, not a
    silent dead-end (data-source fallback norm).
    """

    error_code = "FIRMS_MISSING_KEY"
    retryable = False


class FirmsUpstreamError(FirmsError):
    """FIRMS endpoint returned a non-OK response or network failure."""

    error_code = "FIRMS_UPSTREAM_ERROR"
    retryable = True


class FirmsEmptyError(FirmsError):
    """Endpoint succeeded but returned no detections for the window.

    Note: per the kickoff "Empty response → 0-feature FlatGeobuf" the
    fetcher path emits a valid 0-feature FlatGeobuf rather than raising —
    this error type is reserved for cases where the empty result is
    actively undesirable (e.g. an explicit assertion in tests). The public
    tool surface never raises this.
    """

    error_code = "FIRMS_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

#: NRT sources supported by the FIRMS Web Service AREA endpoint.
_VALID_SOURCES = frozenset({"VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "MODIS_NRT"})

#: FIRMS clamps days_back to this range.
_DAYS_MIN = 1
_DAYS_MAX = 10

#: User-Agent per NASA Earthdata good-neighbour conventions.
_USER_AGENT = (
    "grace-2/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/GRACE-2; agent@grace-2.dev)"
)

#: Request timeout — FIRMS responds fast (<5s for small bbox) but we leave
#: headroom for CONUS-sized queries.
_REQUEST_TIMEOUT = 60.0

#: Columns emitted in the FlatGeobuf properties (drops `latitude`/`longitude`
#: from properties since they ARE the geometry — duplicating them in attrs is
#: noise).
_RETAINED_COLUMNS = (
    "brightness",
    "scan",
    "track",
    "acq_date",
    "acq_time",
    "satellite",
    "instrument",
    "confidence",
    "version",
    "bright_t31",
    "frp",
    "daynight",
)


# ---------------------------------------------------------------------------
# Metadata.
# ---------------------------------------------------------------------------

# Build AtomicToolMetadata DEFENSIVELY against the parallel
# job-0114-schema sibling that adds ``supports_global_query`` to the contract.
# If the schema job lands first, this tool's metadata will carry the field
# (advertising ``False`` — FIRMS requires a bbox). If the schema field hasn't
# landed yet, fall back to construction without it so registration still works.
# Same pattern used by job-0103/0105 (fetch_mrms_qpe, fetch_nws_alerts_conus).
# See OQ-0108-METADATA-FIELD.

def _build_metadata() -> AtomicToolMetadata:
    common = dict(
        name="fetch_firms_active_fire",
        ttl_class="dynamic-1h",
        source_class="firms_active_fire",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:
        logger.debug(
            "AtomicToolMetadata does not (yet) support supports_global_query; "
            "registering fetch_firms_active_fire without it"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# bbox / args helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``FirmsArgError`` if bbox is invalid."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise FirmsArgError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise FirmsArgError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise FirmsArgError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise FirmsArgError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise FirmsArgError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_4dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox to 4dp (~11m) for cache-key stability.

    FIRMS detections have ~375m (VIIRS) or 1km (MODIS) resolution so 4dp is
    well below the actual ground footprint and keeps the cache key stable
    against sub-pixel-jitter input bboxes.
    """
    return tuple(round(v, 4) for v in bbox)  # type: ignore[return-value]


def _bbox_to_firms_str(bbox: tuple[float, float, float, float]) -> str:
    """Format bbox as the comma-string FIRMS expects: ``west,south,east,north``."""
    return f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"


def _validate_date(date: str | None) -> str | None:
    """Validate an optional historical-date string and return it normalized.

    Accepts ``"YYYY-MM-DD"`` (the FIRMS AREA-endpoint trailing start-date
    format). Returns ``None`` for a falsy / blank input. Raises ``FirmsArgError``
    for a malformed date so a typo surfaces as a typed argument error rather than
    a confusing upstream 400.
    """
    if not date or not str(date).strip():
        return None
    s = str(date).strip()
    try:
        parsed = datetime.strptime(s, "%Y-%m-%d")
    except ValueError as exc:
        raise FirmsArgError(
            f"date={date!r} must be 'YYYY-MM-DD' (FIRMS historical start date)"
        ) from exc
    return parsed.strftime("%Y-%m-%d")


def _build_firms_url(
    bbox: tuple[float, float, float, float],
    days_back: int,
    source: str,
    map_key: str,
    date: str | None = None,
) -> str:
    """Build the FIRMS AREA-endpoint CSV URL.

    Rolling window (``date=None``)::

        <base>/<MAP_KEY>/<source>/<west,south,east,north>/<days_back>

    Historical single date (``date="YYYY-MM-DD"``) -- appends the trailing
    start-date segment so the result is exactly that one acquisition day::

        <base>/<MAP_KEY>/<source>/<west,south,east,north>/<days>/<YYYY-MM-DD>

    The historical path is byte-additive: the rolling URL is unchanged when
    ``date`` is omitted.
    """
    url = (
        f"{_FIRMS_BASE}/{map_key}/{source}/"
        f"{_bbox_to_firms_str(bbox)}/{days_back}"
    )
    if date:
        url = f"{url}/{date}"
    return url


# ---------------------------------------------------------------------------
# MAP_KEY resolution (vault-first; generalizes the eBird _resolve_api_key
# pattern). Priority: explicit map_key kwarg -> per-Case secret_ref via the
# vault -> GRACE2_FIRMS_MAP_KEY env -> "demo" literal fallback.
#
# The cache key NEVER includes the raw key (only a short fingerprint), so two
# callers with different valid keys still hit the same cached artifact.
# ---------------------------------------------------------------------------


def _resolve_map_key(
    map_key: str | None = None,
    secret_ref: Any | None = None,
) -> str:
    """Return the FIRMS MAP_KEY, resolving the user's vault key first.

    Priority (mirrors ``fetch_ebird_observations._resolve_api_key``):

    1. Explicit ``map_key`` kwarg (live test path, dev override).
    2. ``secret_ref`` (a ``SecretRecord``) → ``Persistence.get_secret_value``
       — the per-Case vault path threaded by the server at call time. The
       user's FIRMS MAP_KEY (saved via the secrets panel / credential-request
       flow) lives here.
    3. ``GRACE2_FIRMS_MAP_KEY`` env var (local dev convenience).
    4. The literal ``"demo"`` (which the upstream rejects — surfaces as a typed
       ``FirmsAuthError`` rather than a silent no-fires result an LLM could
       narrate as "no fires found"). See OQ-0108-MAP-KEY-AUTH.

    A vault lookup failure (revoked secret, vault unreachable) does NOT crash
    the resolution — it logs and falls through to env / demo so the dispatch
    still produces a typed upstream error the credential-request flow can act
    on, rather than a hard 500.
    """
    # 1. Explicit kwarg.
    if map_key:
        return map_key

    # 2. secret_ref via Persistence.get_secret_value (per-Case vault path).
    if secret_ref is not None:
        try:
            resolved = _materialize_secret(secret_ref)
            if resolved:
                return resolved
        except Exception as exc:  # noqa: BLE001 — fall through to env/demo
            logger.warning(
                "fetch_firms_active_fire: secret_ref lookup failed (%s); "
                "falling back to env/demo",
                exc,
            )

    # 3. Env var fallback.
    env_key = os.environ.get("GRACE2_FIRMS_MAP_KEY")
    if env_key:
        return env_key

    # 4. Demo-literal fallback (rejected upstream -> FirmsAuthError).
    return "demo"


def _materialize_secret(secret_ref: Any) -> str:
    """Bridge ``Persistence.get_secret_value`` (async) into the sync tool body.

    Mirrors ``fetch_ebird_observations._materialize_secret``. The tool body is
    sync (``read_through`` is sync); when invoked from the agent's running event
    loop we cannot call ``asyncio.run`` (it raises "cannot be called from a
    running event loop"), so we run the coroutine on a one-shot worker thread.
    Tests may pass a plain ``str`` (the test-mock shortcut) which is returned
    verbatim.
    """
    # Test-mock shortcut: a bare string is the resolved key.
    if isinstance(secret_ref, str):
        return secret_ref

    persistence = _get_persistence_for_secrets()
    if persistence is None:
        raise FirmsMissingKeyError(
            "Persistence not bound; cannot resolve secret_ref. "
            "Pass map_key=... explicitly in this context."
        )

    coro = persistence.get_secret_value(secret_ref)
    return _run_coro_sync(coro)


# Module-level Persistence binding (parallels the eBird/era5/gtsm setter). The
# agent service binds it at startup so the FIRMS tool can resolve ``secret_ref``
# without importing the MCP client; tests inject a mock via the same setter.
_PERSISTENCE_FOR_SECRETS: Any | None = None


def set_persistence_for_secrets(persistence: Any | None) -> None:
    """Bind the agent-service ``Persistence`` for FIRMS MAP_KEY materialization.

    Called once at startup by the agent service. Tests set it in a fixture and
    reset to ``None`` on teardown.
    """
    global _PERSISTENCE_FOR_SECRETS
    _PERSISTENCE_FOR_SECRETS = persistence


def _get_persistence_for_secrets() -> Any | None:
    return _PERSISTENCE_FOR_SECRETS


def _run_coro_sync(coro: Any) -> Any:
    """Run an ``asyncio`` coroutine and return its result from a sync context.

    Uses ``asyncio.run`` when no loop is running (test/CLI path); falls back to
    a one-shot worker-thread loop when called from inside a running loop
    (agent-runtime path). Both paths close the loop they create. Mirrors
    ``fetch_ebird_observations._run_coro_sync``.
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


# ---------------------------------------------------------------------------
# CSV → FlatGeobuf conversion.
# ---------------------------------------------------------------------------


def _parse_firms_csv_to_fgb(csv_text: str) -> bytes:
    """Parse FIRMS AREA-endpoint CSV into a FlatGeobuf Point layer (EPSG:4326).

    Returns FGB bytes. Empty input (header-only) produces a valid 0-feature
    FlatGeobuf — callers handle the empty case as "no detections this window".

    Args:
        csv_text: the raw CSV body from the FIRMS endpoint. First line is the
            header; rows are one detection each.

    Raises:
        FirmsUpstreamError: malformed CSV (missing required header columns) or
            geopandas/shapely write failure.
    """
    # Lazy import — keeps unit tests that don't touch this path lightweight.
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        import pandas as pd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise FirmsUpstreamError(
            f"geopandas / pandas / shapely not available: {exc}"
        ) from exc

    if not csv_text or not csv_text.strip():
        raise FirmsUpstreamError("FIRMS returned an empty response body")

    try:
        df = pd.read_csv(io.StringIO(csv_text))
    except Exception as exc:  # noqa: BLE001 — pandas raises many error types
        raise FirmsUpstreamError(
            f"FIRMS CSV parse failed: {exc}"
        ) from exc

    # FIRMS CSV always carries latitude / longitude columns. If they're absent
    # the response was an error page or schema-changed payload.
    missing = {"latitude", "longitude"} - set(df.columns)
    if missing:
        raise FirmsUpstreamError(
            f"FIRMS CSV missing required columns {sorted(missing)}; "
            f"got columns={list(df.columns)}"
        )

    # Drop rows with null lat/lon (defensive — FIRMS shouldn't emit these but
    # we guard against schema drift).
    before = len(df)
    df = df.dropna(subset=["latitude", "longitude"]).copy()
    if len(df) < before:
        logger.info(
            "fetch_firms_active_fire: dropped %d null-coord rows", before - len(df)
        )

    # Build Point geometry from lat/lon.
    if df.empty:
        # 0-feature output. Build an explicitly-empty GeoDataFrame with the
        # retained schema so downstream consumers see a stable shape.
        kept = [c for c in _RETAINED_COLUMNS if c in df.columns]
        gdf = gpd.GeoDataFrame(
            df[kept] if kept else df, geometry=[], crs="EPSG:4326"
        )
    else:
        geom = [Point(xy) for xy in zip(df["longitude"], df["latitude"])]
        kept = [c for c in _RETAINED_COLUMNS if c in df.columns]
        gdf = gpd.GeoDataFrame(
            df[kept] if kept else df.drop(columns=["latitude", "longitude"]),
            geometry=geom,
            crs="EPSG:4326",
        )

    # Serialize to FlatGeobuf via a temp file (pyogrio writes FGB in place).
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_firms_"
        ) as tf:
            tmp_path = tf.name
        try:
            gdf.to_file(tmp_path, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise FirmsUpstreamError(
                f"FlatGeobuf write failed: {exc}"
            ) from exc
        with open(tmp_path, "rb") as fh:
            fgb_bytes = fh.read()
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    logger.info(
        "fetch_firms_active_fire: parsed %d detection(s) → %d-byte FlatGeobuf",
        len(gdf),
        len(fgb_bytes),
    )
    return fgb_bytes


# ---------------------------------------------------------------------------
# Network fetch.
# ---------------------------------------------------------------------------


def _fetch_firms_csv(
    bbox: tuple[float, float, float, float],
    days_back: int,
    source: str,
    map_key: str,
    date: str | None = None,
) -> str:
    """Hit the FIRMS AREA-endpoint and return the CSV body as text.

    ``date`` (optional ``"YYYY-MM-DD"``) appends the trailing historical
    start-date segment for a specific past acquisition day.

    Raises:
        FirmsAuthError: FIRMS rejected the MAP_KEY (invalid / rate-limited).
        FirmsUpstreamError: network failure or non-200 status.
    """
    url = _build_firms_url(bbox, days_back, source, map_key, date=date)
    logger.info(
        "fetch_firms_active_fire: requesting %s",
        url.replace(map_key, "***"),  # don't log the key
    )
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_REQUEST_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise FirmsUpstreamError(
            f"FIRMS network failure: {exc}"
        ) from exc

    body = resp.text
    lowered = body.strip().lower()

    # FIRMS auth-failure detection (verified live 2026-06-08):
    # - Invalid MAP_KEY: HTTP 400 + body "Invalid MAP_KEY." (sometimes 200).
    # - Rate-limited:   HTTP 200 + body "exceeded your transaction" wording.
    # Both must surface as FirmsAuthError so the agent surface can prompt the
    # user to set GRACE2_FIRMS_MAP_KEY rather than retry.
    if lowered.startswith("invalid map_key") or "invalid map_key" in lowered:
        raise FirmsAuthError(
            "FIRMS rejected the MAP_KEY. Set GRACE2_FIRMS_MAP_KEY to a valid "
            "key from https://firms.modaps.eosdis.nasa.gov/api/map_key/. "
            "See OQ-0108-MAP-KEY-AUTH for production-mode auth."
        )
    if "exceeded your transaction" in lowered or (
        "rate" in lowered and "limit" in lowered
    ):
        raise FirmsAuthError(
            f"FIRMS reports rate-limit exhaustion for MAP_KEY: {body[:200]}"
        )

    if resp.status_code != 200:
        raise FirmsUpstreamError(
            f"FIRMS returned HTTP {resp.status_code}: {body[:200]}"
        )

    return body


def _fetch_firms_active_fire_bytes(
    bbox: tuple[float, float, float, float],
    days_back: int,
    source: str,
    map_key: str,
    date: str | None = None,
) -> bytes:
    """Fetch FIRMS CSV → FlatGeobuf bytes. The cache-shim fetch_fn."""
    csv_text = _fetch_firms_csv(bbox, days_back, source, map_key, date=date)
    return _parse_firms_csv_to_fgb(csv_text)


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
def fetch_firms_active_fire(
    bbox: tuple[float, float, float, float],
    days_back: int = 1,
    source: Literal[
        "VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "MODIS_NRT"
    ] = "VIIRS_SNPP_NRT",
    # fire-animation demo S2/J2: optional historical start-date "YYYY-MM-DD".
    # When supplied, queries exactly that one acquisition day (day_range forced
    # to 1) so a SPECIFIC PAST date works in addition to the rolling days_back.
    date: str | None = None,
    # job VAULT-READ: per-Case MAP_KEY resolution. ``map_key`` is the explicit
    # override (dev/tests); ``secret_ref`` is the per-Case ``SecretRecord`` the
    # server threads at call time so the user's vault key is resolved first.
    # Both are underscore-free so they survive the schema strip ONLY if exposed;
    # the server injects ``secret_ref`` programmatically (not via the LLM).
    map_key: str | None = None,
    secret_ref: Any | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch NASA FIRMS satellite active-fire detections for a bbox and recent day window.

    **What it does:** Calls the NASA FIRMS Web Service AREA API
    (``firms.modaps.eosdis.nasa.gov/api/area/csv``) to retrieve satellite
    thermal-anomaly / active-fire pixel detections for the last 1–10 days over
    a bounded geographic region. Parses the CSV response into a FlatGeobuf
    Point layer with brightness, FRP (Fire Radiative Power), scan, track,
    acquisition time, satellite, instrument, confidence, and day/night fields.
    Cached ``dynamic-1h``. Requires a free NASA FIRMS MAP_KEY
    (``GRACE2_FIRMS_MAP_KEY`` env var). Does not support global queries
    (``supports_global_query=False``).

    **When to use:**
    - Wildfire situational-awareness: "show active fires in California right now".
    - Near-real-time fire forcing discovery for a wildfire workflow setup.
    - Smoke-source identification overlaid on an air-quality or population layer.
    - Multi-sensor comparison (VIIRS vs MODIS detection density) over a study
      area.

    **When NOT to use:**
    - Historical fire perimeters or burned-area polygons (use
      ``fetch_nifc_fire_perimeters`` for current season or
      ``fetch_mtbs_burn_severity`` for 1984-present archives).
    - Fuel-load / fuel-moisture inputs (LANDFIRE — separate tool, not yet in
      catalog).
    - Fire spread or behavior forecasts (FIRMS is detection-only, not forecast).
    - Global queries without a bbox (FIRMS AREA API requires a bbox).

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
      Required; global queries rejected. Example: ``(-124.0, 32.5, -114.0, 42.0)``
      for California.
    - ``days_back`` (int): 1–10 days back from FIRMS "today". Default 1. Higher
      values accumulate more detections; note FIRMS upstream rejects >5 as of
      2026-06-08 (surfaces as ``FirmsUpstreamError`` — see OQ-0108-DAYS-RANGE).
    - ``source`` (str): ``"VIIRS_SNPP_NRT"`` (default; Suomi NPP, 375m),
      ``"VIIRS_NOAA20_NRT"`` (NOAA-20, 375m), or ``"MODIS_NRT"``
      (Terra/Aqua, 1km).
    - ``date`` (str, optional): a historical acquisition day ``"YYYY-MM-DD"``.
      When given, the tool queries EXACTLY that one day (the FIRMS trailing
      start-date segment, ``day_range`` forced to 1) -- use this to overlay the
      hot pixels for a SPECIFIC PAST date (e.g. recreating a past animation
      window) rather than the rolling ``days_back`` from FIRMS "today". Default
      ``None`` keeps the rolling-window behaviour.

    **Returns:**
    ``LayerURI(layer_type="vector", role="primary", units=None)`` pointing at a
    FlatGeobuf with fields: ``brightness``, ``scan``, ``track``, ``acq_date``,
    ``acq_time``, ``satellite``, ``instrument``, ``confidence``, ``version``,
    ``bright_t31``, ``frp``, ``daynight``. Cached ``dynamic-1h``.

    **Cross-tool dependencies:**
    - Upstream of: wildfire spread-model workflow setups, smoke/population
      impact overlays.
    - Pairs with: ``fetch_nifc_fire_perimeters`` (current perimeters around FIRMS
      detections), ``fetch_mtbs_burn_severity`` (historical context).
    - Auth dependency: set ``GRACE2_FIRMS_MAP_KEY`` env var (register free at
      ``firms.modaps.eosdis.nasa.gov/api/map_key/``).
    """
    # 1. Validate arguments (typed errors, not crashes — invariant: FR-AS-11).
    if source not in _VALID_SOURCES:
        raise FirmsArgError(
            f"unknown source={source!r}; allowed: {sorted(_VALID_SOURCES)}"
        )
    if not isinstance(days_back, int) or not (_DAYS_MIN <= days_back <= _DAYS_MAX):
        raise FirmsArgError(
            f"days_back must be int in [{_DAYS_MIN},{_DAYS_MAX}]; got {days_back!r}"
        )
    _validate_bbox(bbox)
    # Historical-date positional: a single past acquisition day. When supplied,
    # force day_range=1 so the result is exactly that day (the trailing
    # /{YYYY-MM-DD} segment + day_range=1 is the FIRMS single-day idiom).
    q_date = _validate_date(date)
    effective_days_back = 1 if q_date is not None else days_back

    # 2. Quantize bbox to 4dp for cache-key stability.
    q_bbox = _round_bbox_to_4dp(bbox)

    # 3. Resolve MAP_KEY (vault-first) and a key-fingerprint for the cache. We
    #    avoid putting the raw key in the cache-key params so cache hits don't
    #    depend on the secret value — two callers with different valid keys
    #    still hit the same artifact. A short SHA-256 prefix prevents accidental
    #    cross-key blob reuse if FIRMS ever segments responses by key.
    #    job VAULT-READ: the user's per-Case vault key (via ``secret_ref``) wins
    #    over the env var; ``map_key`` kwarg (dev/test) wins over both.
    resolved_map_key = _resolve_map_key(map_key=map_key, secret_ref=secret_ref)
    import hashlib as _hashlib
    key_fingerprint = _hashlib.sha256(
        resolved_map_key.encode("utf-8")
    ).hexdigest()[:8]

    params = {
        "source": source,
        "bbox": list(q_bbox),
        "days_back": effective_days_back,
        # ``date`` is None for the rolling-window path (pruned by the cache
        # canonicalizer), so a rolling call's key is byte-identical to before;
        # a historical call keys on the specific day.
        "date": q_date,
        "key_fp": key_fingerprint,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_firms_active_fire_bytes(
            q_bbox, effective_days_back, source, resolved_map_key, date=q_date
        ),
    )
    assert result.uri is not None, (
        "fetch_firms_active_fire is cacheable; uri must be set by read_through"
    )

    # 4. Build LayerURI.
    source_label = {
        "VIIRS_SNPP_NRT": "VIIRS S-NPP",
        "VIIRS_NOAA20_NRT": "VIIRS NOAA-20",
        "MODIS_NRT": "MODIS Terra/Aqua",
    }[source]

    if q_date is not None:
        layer_id = (
            f"firms-{source.lower()}-{q_bbox[0]:.3f}-{q_bbox[1]:.3f}-{q_date}"
        )
        layer_name = f"NASA FIRMS active fires - {source_label} ({q_date})"
    else:
        layer_id = (
            f"firms-{source.lower()}-{q_bbox[0]:.3f}-{q_bbox[1]:.3f}-"
            f"d{days_back}"
        )
        layer_name = (
            f"NASA FIRMS active fires - {source_label} (last {days_back}d)"
        )

    return LayerURI(
        layer_id=layer_id,
        name=layer_name,
        layer_type="vector",
        uri=result.uri,
        style_preset="firms_active_fire",
        role="primary",
        units=None,
        bbox=q_bbox,
    )
