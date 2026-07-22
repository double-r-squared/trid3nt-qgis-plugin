"""``fetch_movebank_tracks`` atomic tool — Movebank animal tracking Tier-2 fetcher (job-0130).

Wraps the Movebank REST API (https://www.movebank.org/movebank/service/direct-read)
to return per-individual animal-tracking trajectories as FlatGeobuf linestrings (one
line feature per ``individual_local_identifier``, vertices ordered by timestamp) OR
points (one feature per telemetry fix). The tool is Movebank's "data SOURCE not
plugin" path called out in ``docs/srs/E-qgis-plugins-inventory.md`` and the Tier-2
keyed-endpoint pattern in §F.3 / Appendix H.6 (per-Case keys, vault-scoped).

API surface (verified 2026-06-08 against https://www.movebank.org/movebank/help/):

    https://www.movebank.org/movebank/service/direct-read?entity_type=event
        &study_id=<study_id>
        &attributes=individual_local_identifier,timestamp,location_lat,location_long,sensor_type_id
        &sensor_type_id=<id>            # optional
        &timestamp_start=YYYYMMDDhhmmssSSS  # optional
        &timestamp_end=YYYYMMDDhhmmssSSS    # optional

Authentication: HTTP Basic — every request carries a ``movebank`` username +
password pair. Studies vary in their data-licence: some are publicly readable with
ANY authenticated account; others demand "Data Use Statement" acceptance per
study. The error surface distinguishes auth failures (401), licence-acceptance
failures (403, "License not accepted"), and bad-study errors.

Response shape: CSV with a header row + one row per fix. Empty studies (or any
no-data response with bbox=outside-coverage) yield a header-only CSV; we serialize
that as a 0-feature FlatGeobuf so downstream readers see a well-formed file.

Cache: static-30d (historic tracks are immutable; live deployments still cycle
the bucket through the 30-day lifecycle policy). Cache key on
``(study_id, bbox-quantized, username, sensor_type_id, time_range, max_records,
geometry_type)``. Username is part of the key because the visible study set
depends on which Movebank account approved which study's Data Use Statement.

The job-0086 codified lesson (URL/render consistency != geographic correctness)
applies: every emitted feature MUST lie inside the requested bbox after the
client-side spatial filter (Movebank does not bbox-filter server-side). The
live test asserts ≥1 feature lies geographically inside the test bbox.

FR-TA-2 atomic tool; FR-CE-8 / FR-DC-3/4 routed through ``read_through``.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Literal

import httpx

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_movebank_tracks",
    "MovebankError",
    "MovebankInputError",
    "MovebankAuthError",
    "MovebankLicenseError",
    "MovebankUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.fetch_movebank_tracks")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class MovebankError(RuntimeError):
    """Base class for fetch_movebank_tracks failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "MOVEBANK_ERROR"
    retryable: bool = True


class MovebankInputError(MovebankError):
    """Bad inputs (malformed bbox, missing credentials, unknown study)."""

    error_code = "MOVEBANK_INPUT_ERROR"
    retryable = False


class MovebankAuthError(MovebankError):
    """Movebank rejected the supplied credentials (HTTP 401)."""

    error_code = "MOVEBANK_AUTH_ERROR"
    retryable = False


class MovebankLicenseError(MovebankError):
    """The Movebank account has not accepted this study's Data Use Statement (HTTP 403)."""

    error_code = "MOVEBANK_LICENSE_ERROR"
    retryable = False


class MovebankUpstreamError(MovebankError):
    """Movebank API returned 5xx or the network call failed."""

    error_code = "MOVEBANK_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_MOVEBANK_URL = "https://www.movebank.org/movebank/service/direct-read"

# Per-request timeout — Movebank studies can be large; pad generously.
_TIMEOUT_S = 120.0

# Cap on records — defensive guard against a multi-million-row pull. The audit
# notes large multi-year studies ~50MB which still fits well under this cap.
_MAX_RECORDS_HARD_CAP = 1_000_000

# User-Agent per Movebank usage etiquette (mirrors the GBIF pattern).
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

# Attributes we pull. ``individual_local_identifier`` keys the per-animal
# aggregation; ``timestamp`` orders the vertices; ``location_long/lat`` provide
# the geometry; ``sensor_type_id`` (numeric) goes into the output property. We
# explicitly DO NOT request individual ``id`` fields (numeric DB-internal ids)
# to keep the output schema stable across Movebank schema migrations.
_REQUEST_ATTRIBUTES = (
    "individual_local_identifier,timestamp,location_lat,location_long,sensor_type_id"
)

GeometryType = Literal["linestring", "point"]


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_movebank_tracks",
    ttl_class="static-30d",
    source_class="movebank",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# bbox + parameter validation.
# ---------------------------------------------------------------------------


def _validate_bbox(
    bbox: tuple[float, float, float, float] | None,
) -> None:
    """Raise ``MovebankInputError`` if bbox is invalid. ``None`` is allowed."""
    if bbox is None:
        return
    if len(bbox) != 4:
        raise MovebankInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise MovebankInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise MovebankInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise MovebankInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if west >= east or south >= north:
        raise MovebankInputError(
            f"bbox is degenerate (west < east, south < north required): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    """Round bbox coords to 6dp (~0.1m) for cache-key stability."""
    if bbox is None:
        return None
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _validate_geometry_type(geometry_type: str) -> GeometryType:
    if geometry_type not in ("linestring", "point"):
        raise MovebankInputError(
            f"geometry_type must be 'linestring' or 'point'; got {geometry_type!r}"
        )
    return geometry_type  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Credential resolution (Tier-2 secret handling, Appendix H.6).
# ---------------------------------------------------------------------------


def _resolve_credentials(
    username: str | None,
    password: str | None,
    secret_ref: Any | None,
) -> tuple[str, str]:
    """Resolve a (username, password) pair from the three accepted sources.

    Priority (most specific to most general):

    1. Explicit ``username`` + ``password`` kwargs (test fixtures, scripts).
    2. ``secret_ref`` (a ``SecretRecord``) — looked up via
       ``Persistence.get_secret_value``. The vault payload is interpreted as
       either ``"username:password"`` (colon-separated) OR a JSON object
       ``{"username": "...", "password": "..."}``. The username field on the
       explicit kwarg, if also passed, overrides the vault-resolved one (lets
       the LLM disambiguate which Movebank account a multi-account user wants).
    3. ``TRID3NT_MOVEBANK_USER`` + ``TRID3NT_MOVEBANK_PASSWORD`` env vars (local
       dev / CI live-test gate).

    Raises ``MovebankInputError`` on missing creds. The Persistence lookup
    happens via a local import (avoids a heavy import at module load and keeps
    the unit-test surface mockable).
    """
    # 1. Explicit kwargs win.
    if username and password:
        return username, password

    # 2. secret_ref via Persistence.get_secret_value (async; we run on the
    # caller's thread since this tool runs synchronously inside an ADK
    # FunctionTool body that the agent invokes via threadpool).
    resolved_user = username
    resolved_pass = password
    if secret_ref is not None:
        try:
            import asyncio
            import json

            from ..persistence import Persistence  # local — avoids cycle
        except Exception as exc:  # noqa: BLE001
            raise MovebankInputError(
                f"secret_ref provided but Persistence machinery is unavailable: {exc}"
            ) from exc

        # Persistence.get_secret_value is a coroutine; run it synchronously.
        # We need an MCP-less helper that only touches Secret Manager —
        # Persistence.get_secret_value is an instance method but doesn't
        # actually touch the MCP transport, so we instantiate a minimal
        # Persistence with a no-op MCP client.
        class _NoOpMCP:
            async def call_tool(
                self, name: str, arguments: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                raise RuntimeError(
                    "_NoOpMCP.call_tool should not be invoked from credential resolution"
                )

        persistence = Persistence(_NoOpMCP())
        try:
            payload = asyncio.run(persistence.get_secret_value(secret_ref))
        except RuntimeError as exc:
            # If we're already inside a running event loop (pytest-asyncio etc),
            # asyncio.run will refuse; fall back to a fresh loop in a thread.
            if "asyncio.run() cannot be called" in str(exc):
                import concurrent.futures

                def _bg() -> str:
                    new_loop = asyncio.new_event_loop()
                    try:
                        return new_loop.run_until_complete(
                            persistence.get_secret_value(secret_ref)
                        )
                    finally:
                        new_loop.close()

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    payload = pool.submit(_bg).result()
            else:
                raise

        # Interpret payload: colon-separated OR JSON object.
        parsed_user: str | None = None
        parsed_pass: str | None = None
        payload_str = payload.strip()
        if payload_str.startswith("{"):
            try:
                obj = json.loads(payload_str)
            except json.JSONDecodeError as exc:
                raise MovebankInputError(
                    f"secret_ref vault payload is not valid JSON: {exc}"
                ) from exc
            parsed_user = obj.get("username") or obj.get("user")
            parsed_pass = obj.get("password") or obj.get("pass")
        elif ":" in payload_str:
            parsed_user, _, parsed_pass = payload_str.partition(":")
        else:
            # Assume the payload is the password alone; username must come
            # from the explicit kwarg or env.
            parsed_pass = payload_str

        if resolved_user is None:
            resolved_user = parsed_user
        if resolved_pass is None:
            resolved_pass = parsed_pass

    # 3. env-var fallback.
    if resolved_user is None:
        resolved_user = os.environ.get("TRID3NT_MOVEBANK_USER")
    if resolved_pass is None:
        resolved_pass = os.environ.get("TRID3NT_MOVEBANK_PASSWORD")

    if not resolved_user or not resolved_pass:
        raise MovebankInputError(
            "Movebank credentials missing: pass (username + password) OR "
            "secret_ref OR set TRID3NT_MOVEBANK_USER + TRID3NT_MOVEBANK_PASSWORD"
        )

    return resolved_user, resolved_pass


# ---------------------------------------------------------------------------
# CSV fetch.
# ---------------------------------------------------------------------------


def _format_movebank_timestamp(dt: datetime) -> str:
    """Movebank wants timestamps in ``YYYYMMDDhhmmssSSS`` (UTC, millisecond)."""
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y%m%d%H%M%S") + f"{utc.microsecond // 1000:03d}"


def _fetch_movebank_events_csv(
    study_id: int,
    username: str,
    password: str,
    *,
    sensor_type_id: int | None,
    time_range: tuple[datetime, datetime] | None,
    client: httpx.Client | None = None,
) -> str:
    """Issue the direct-read CSV request and return the body text.

    Raises:
        ``MovebankAuthError`` on 401.
        ``MovebankLicenseError`` on 403 (Data Use Statement not accepted).
        ``MovebankUpstreamError`` on 5xx / network failure / non-CSV body.
    """
    params: dict[str, Any] = {
        "entity_type": "event",
        "study_id": study_id,
        "attributes": _REQUEST_ATTRIBUTES,
    }
    if sensor_type_id is not None:
        params["sensor_type_id"] = sensor_type_id
    if time_range is not None:
        params["timestamp_start"] = _format_movebank_timestamp(time_range[0])
        params["timestamp_end"] = _format_movebank_timestamp(time_range[1])

    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            timeout=_TIMEOUT_S,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        )
    try:
        try:
            resp = client.get(
                _MOVEBANK_URL, params=params, auth=(username, password)
            )
        except httpx.RequestError as exc:
            raise MovebankUpstreamError(
                f"Movebank direct-read network failure (study_id={study_id}): {exc}"
            ) from exc

        if resp.status_code == 401:
            raise MovebankAuthError(
                f"Movebank rejected credentials for study_id={study_id} "
                f"(HTTP 401). Check username/password."
            )
        if resp.status_code == 403:
            # Movebank returns 403 for "License not accepted" — the account
            # must visit movebank.org and accept the study's Data Use Statement.
            raise MovebankLicenseError(
                f"Movebank account has not accepted the Data Use Statement for "
                f"study_id={study_id} (HTTP 403). Log into movebank.org and "
                f"accept the licence before retrying."
            )
        if resp.status_code >= 500:
            raise MovebankUpstreamError(
                f"Movebank direct-read returned {resp.status_code} "
                f"(study_id={study_id})"
            )
        if resp.status_code >= 400:
            raise MovebankInputError(
                f"Movebank direct-read returned {resp.status_code} "
                f"(study_id={study_id}): {resp.text[:200]}"
            )

        body = resp.text
        # Defensive: Movebank's "License Terms" response is HTML, not CSV. If
        # the body doesn't look like CSV (no comma in the first line, no
        # known column), reject it.
        first_line = body.splitlines()[0] if body else ""
        if "<html" in body[:200].lower() or "License Terms" in body[:500]:
            raise MovebankLicenseError(
                f"Movebank returned an HTML licence-acceptance page for "
                f"study_id={study_id}; account must accept the Data Use Statement."
            )
        if first_line and "," not in first_line and "individual" not in first_line:
            raise MovebankUpstreamError(
                f"Movebank direct-read body does not look like CSV "
                f"(study_id={study_id}): first 200 chars: {body[:200]!r}"
            )

        return body
    finally:
        if owns_client:
            client.close()


# ---------------------------------------------------------------------------
# CSV -> records.
# ---------------------------------------------------------------------------


def _parse_movebank_csv(
    body: str,
) -> list[dict[str, Any]]:
    """Parse the direct-read CSV into a list of event dicts.

    Returns one dict per row with normalized keys (``individual_id``,
    ``timestamp_iso``, ``lon``, ``lat``, ``sensor_type_id``). Skips rows
    missing coordinates or timestamp. Movebank column names vary slightly
    between studies (some carry ``location-lat`` with hyphens, others
    ``location_lat``); we accept both.
    """
    if not body.strip():
        return []
    reader = csv.DictReader(io.StringIO(body))
    out: list[dict[str, Any]] = []
    skipped_missing = 0
    for row in reader:
        # Tolerate hyphen-vs-underscore column variants.
        ind = (
            row.get("individual_local_identifier")
            or row.get("individual-local-identifier")
            or row.get("tag_local_identifier")
            or row.get("tag-local-identifier")
            or ""
        )
        ts = row.get("timestamp") or ""
        lon_raw = (
            row.get("location_long")
            or row.get("location-long")
            or row.get("location_lon")
        )
        lat_raw = row.get("location_lat") or row.get("location-lat")
        sensor_raw = (
            row.get("sensor_type_id")
            or row.get("sensor-type-id")
            or row.get("sensor_type")
        )

        if lon_raw is None or lat_raw is None or lon_raw == "" or lat_raw == "":
            skipped_missing += 1
            continue
        try:
            lon_f = float(lon_raw)
            lat_f = float(lat_raw)
        except (TypeError, ValueError):
            skipped_missing += 1
            continue
        if not (math.isfinite(lon_f) and math.isfinite(lat_f)):
            skipped_missing += 1
            continue
        try:
            sensor_val = int(sensor_raw) if sensor_raw else None
        except (TypeError, ValueError):
            sensor_val = None
        out.append(
            {
                "individual_id": str(ind),
                "timestamp_iso": ts.strip(),
                "lon": lon_f,
                "lat": lat_f,
                "sensor_type_id": sensor_val,
            }
        )

    if skipped_missing:
        logger.info(
            "fetch_movebank_tracks: skipped %d rows with missing/invalid coords",
            skipped_missing,
        )
    return out


# ---------------------------------------------------------------------------
# FlatGeobuf serialization.
# ---------------------------------------------------------------------------


def _records_to_flatgeobuf_bytes(
    records: list[dict[str, Any]],
    bbox: tuple[float, float, float, float] | None,
    geometry_type: GeometryType,
    study_id: int,
) -> bytes:
    """Convert event records to a FlatGeobuf.

    geometry_type="point":
        One feature per record. Properties: individual_id, timestamp,
        sensor_type_id, study_id.

    geometry_type="linestring":
        Group records by individual_id, sort each group by timestamp_iso (ISO
        strings sort chronologically), build a LineString per individual that
        has ≥2 points. Properties: individual_id, n_points, first_timestamp,
        last_timestamp, study_id.

    Bbox filter (client-side per audit; Movebank doesn't reliably bbox-filter):
        - For points: each emitted point lies within bbox.
        - For linestrings: ONLY individuals whose ENTIRE track lies within
          bbox produce a linestring (a conservative interpretation that keeps
          the geographic-correctness gate strict — a track that exits/enters
          the bbox is dropped rather than truncated). This is one OQ-worth
          surfacing: see OQ-0130-MOVEBANK-LINESTRING-BBOX-CLIPPING.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        import pandas as pd  # type: ignore[import-not-found]
        from shapely.geometry import LineString, Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MovebankUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    if bbox is not None:
        west, south, east, north = bbox

        def _in_bbox(lon: float, lat: float) -> bool:
            return west <= lon <= east and south <= lat <= north
    else:
        def _in_bbox(lon: float, lat: float) -> bool:  # noqa: ARG001
            return True

    rows: list[dict[str, Any]] = []
    geoms: list[Any] = []
    skipped_outside_bbox = 0

    if geometry_type == "point":
        for rec in records:
            if not _in_bbox(rec["lon"], rec["lat"]):
                skipped_outside_bbox += 1
                continue
            rows.append(
                {
                    "individual_id": rec["individual_id"],
                    "timestamp": rec["timestamp_iso"],
                    "sensor_type_id": rec["sensor_type_id"],
                    "study_id": study_id,
                }
            )
            geoms.append(Point(rec["lon"], rec["lat"]))
    else:
        # Group by individual; sort by timestamp.
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for rec in records:
            grouped[rec["individual_id"]].append(rec)

        for ind, recs in grouped.items():
            recs_sorted = sorted(recs, key=lambda r: r["timestamp_iso"])
            # Bbox filter per linestring: ALL vertices must lie inside bbox.
            # If any vertex is outside, drop the whole individual.
            if bbox is not None and not all(
                _in_bbox(r["lon"], r["lat"]) for r in recs_sorted
            ):
                skipped_outside_bbox += len(recs_sorted)
                continue
            if len(recs_sorted) < 2:
                # A 1-point "track" is not a LineString — drop.
                continue
            coords = [(r["lon"], r["lat"]) for r in recs_sorted]
            rows.append(
                {
                    "individual_id": ind,
                    "n_points": len(recs_sorted),
                    "first_timestamp": recs_sorted[0]["timestamp_iso"],
                    "last_timestamp": recs_sorted[-1]["timestamp_iso"],
                    "study_id": study_id,
                }
            )
            geoms.append(LineString(coords))

    if skipped_outside_bbox:
        logger.warning(
            "fetch_movebank_tracks: filtered %d record(s) outside bbox %s",
            skipped_outside_bbox,
            bbox,
        )

    if not rows:
        # Empty result — build an empty FlatGeobuf with the right schema.
        if geometry_type == "point":
            empty_df = pd.DataFrame(
                columns=[
                    "individual_id",
                    "timestamp",
                    "sensor_type_id",
                    "study_id",
                ]
            )
        else:
            empty_df = pd.DataFrame(
                columns=[
                    "individual_id",
                    "n_points",
                    "first_timestamp",
                    "last_timestamp",
                    "study_id",
                ]
            )
        gdf = gpd.GeoDataFrame(empty_df, geometry=[], crs="EPSG:4326")
    else:
        df = pd.DataFrame(rows)
        gdf = gpd.GeoDataFrame(df, geometry=geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_movebank_"
        ) as fgb_f:
            tmp_fgb = fgb_f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise MovebankUpstreamError(
                f"FlatGeobuf write failed: {exc}"
            ) from exc
        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()
        logger.info(
            "fetch_movebank_tracks: FlatGeobuf serialized %d %s feature(s) = %d bytes",
            len(rows),
            geometry_type,
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


def _fetch_movebank_bytes(
    study_id: int,
    bbox: tuple[float, float, float, float] | None,
    username: str,
    password: str,
    sensor_type_id: int | None,
    time_range: tuple[datetime, datetime] | None,
    max_records: int,
    geometry_type: GeometryType,
) -> bytes:
    """Pipeline: pull Movebank CSV → parse → bbox-filter → serialize to FlatGeobuf."""
    body = _fetch_movebank_events_csv(
        study_id=study_id,
        username=username,
        password=password,
        sensor_type_id=sensor_type_id,
        time_range=time_range,
    )
    records = _parse_movebank_csv(body)
    if len(records) > max_records:
        logger.info(
            "fetch_movebank_tracks: truncating %d records to max_records=%d",
            len(records),
            max_records,
        )
        records = records[:max_records]
    return _records_to_flatgeobuf_bytes(
        records=records,
        bbox=bbox,
        geometry_type=geometry_type,
        study_id=study_id,
    )


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
def fetch_movebank_tracks(
    study_id: int,
    bbox: tuple[float, float, float, float] | None = None,
    username: str | None = None,
    password: str | None = None,
    secret_ref: Any | None = None,
    sensor_type_id: int | None = None,
    time_range: tuple[datetime, datetime] | None = None,
    max_records: int = 500_000,
    geometry_type: GeometryType = "linestring",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Movebank Tier-2 animal-tracking trajectory fetcher.

    Use this when: the agent needs animal-tracking telemetry (bird migration,
    mammal movement, marine megafauna) for ecological or hazard overlay — e.g.
    overlaying sandhill crane migration corridors on a wildfire footprint,
    plotting elephant movement against flood-risk surfaces, or visualizing
    sea turtle tracks against coastal storm surge. Returns either FlatGeobuf
    LineStrings (one per individual, vertices ordered by timestamp; default)
    or Points (one per telemetry fix).

    Do NOT use this for: occurrence points without tracking continuity (use
    ``fetch_gbif_occurrences`` or ``fetch_inaturalist_observations`` —
    Movebank tracks are temporally ordered movement traces, not static
    sighting points), live-streaming telemetry (Movebank's API is
    near-real-time but caches batches; for sub-minute live feeds use the
    publisher's own SCADA), or species-range polygons (use IUCN Red List
    range maps instead).

    Wraps the Movebank REST API (https://www.movebank.org/movebank/service/direct-read).
    Authentication is **always required** — Movebank rejects unauthenticated
    requests. Most studies further require accepting per-study Data Use
    Statements on movebank.org BEFORE the API serves data; on first access
    of a new study the tool surfaces ``MovebankLicenseError`` with the
    licence-acceptance hint.

    Credentials are resolved in priority order:
    1. Explicit ``username`` + ``password`` kwargs.
    2. ``secret_ref`` (a ``SecretRecord``) — vault payload may be
       ``"user:pass"`` (colon-separated) OR a JSON ``{"username": "...",
       "password": "..."}``.
    3. ``TRID3NT_MOVEBANK_USER`` + ``TRID3NT_MOVEBANK_PASSWORD`` env vars
       (local dev / CI live-test gate).

    Bbox filtering happens **client-side** after the fetch — Movebank does not
    reliably bbox-filter server-side, so the full study record set is pulled
    then trimmed. For linestrings the filter is conservative: ALL vertices of
    an individual's track must lie within the bbox or the individual is
    dropped (avoids truncated/broken tracks). See
    OQ-0130-MOVEBANK-LINESTRING-BBOX-CLIPPING for the alternative considered.

    Params:
        study_id: Movebank ``study_id`` (int). Example: ``1259686571`` is the
            "Sandhill Crane: Bismarck-Hettinger-Mandan" public study.
        bbox: optional ``(west, south, east, north)`` in EPSG:4326. ``None``
            returns the entire study record set.
        username: explicit Movebank account username. See credential
            resolution priority above.
        password: explicit Movebank account password.
        secret_ref: a ``SecretRecord`` whose vault payload carries the
            credentials. Looked up via ``Persistence.get_secret_value``.
        sensor_type_id: optional Movebank ``sensor_type_id`` int (e.g.
            653 = "GPS"). ``None`` returns every sensor type.
        time_range: optional ``(start, end)`` ``datetime`` pair filtering on
            the event timestamp. Inclusive on both ends. Timezone-aware
            datetimes are converted to UTC; naive datetimes assumed UTC.
        max_records: cap on records pulled from Movebank before serialization
            (default 500_000, hard cap 1_000_000). Beyond this the result
            FlatGeobuf is truncated, NOT an error.
        geometry_type: ``"linestring"`` (default — one feature per individual)
            or ``"point"`` (one feature per telemetry fix).

    Returns:
        A ``LayerURI`` pointing at a FlatGeobuf in the cache bucket:
        ``s3://trid3nt-cache/cache/static-30d/movebank/<key>.fgb``
        containing the tracks/points clipped to the requested bbox (if any),
        in EPSG:4326. ``layer_type="vector"``, ``role="context"``,
        ``units=None``, ``style_preset="movebank_tracks"``.

    Output schema:
        geometry_type="linestring":
            individual_id    (str)
            n_points         (int)
            first_timestamp  (str ISO-8601)
            last_timestamp   (str ISO-8601)
            study_id         (int)
        geometry_type="point":
            individual_id    (str)
            timestamp        (str ISO-8601)
            sensor_type_id   (int | null)
            study_id         (int)

    FR-CE-8: Routed through ``read_through`` so identical
    ``(study_id, bbox, username, sensor_type_id, time_range, max_records,
    geometry_type)`` calls reuse the cached FlatGeobuf. Cache key includes the
    username because Movebank access varies per account licence acceptance.

    Errors (FR-AS-11 typed surface):
        MovebankInputError       — bad bbox, missing credentials, bad params (retryable=False)
        MovebankAuthError        — Movebank rejected credentials (401, retryable=False)
        MovebankLicenseError     — account has not accepted study's Data Use Statement (403, retryable=False)
        MovebankUpstreamError    — 5xx / network / malformed response (retryable=True)
    """
    # ---- Input validation ----
    if not isinstance(study_id, int):
        raise MovebankInputError(
            f"study_id must be int; got {type(study_id).__name__}"
        )
    if study_id <= 0:
        raise MovebankInputError(f"study_id must be > 0; got {study_id}")

    _validate_bbox(bbox)
    gtype = _validate_geometry_type(geometry_type)

    if sensor_type_id is not None and (
        not isinstance(sensor_type_id, int) or sensor_type_id <= 0
    ):
        raise MovebankInputError(
            f"sensor_type_id must be a positive int or None; got {sensor_type_id!r}"
        )

    if not isinstance(max_records, int):
        raise MovebankInputError(
            f"max_records must be int; got {type(max_records).__name__}"
        )
    if max_records <= 0:
        raise MovebankInputError(f"max_records must be > 0; got {max_records}")
    if max_records > _MAX_RECORDS_HARD_CAP:
        raise MovebankInputError(
            f"max_records exceeds hard cap {_MAX_RECORDS_HARD_CAP}; got {max_records}"
        )

    if time_range is not None:
        if len(time_range) != 2:
            raise MovebankInputError(
                f"time_range must be (start, end); got {time_range!r}"
            )
        ts_start, ts_end = time_range
        if not isinstance(ts_start, datetime) or not isinstance(ts_end, datetime):
            raise MovebankInputError(
                f"time_range entries must be datetime; got {type(ts_start).__name__}, "
                f"{type(ts_end).__name__}"
            )
        if ts_start > ts_end:
            raise MovebankInputError(
                f"time_range start must be <= end; got {time_range!r}"
            )

    # ---- Credentials ----
    resolved_user, resolved_pass = _resolve_credentials(
        username=username, password=password, secret_ref=secret_ref
    )

    # ---- Cache-key params (resolved + quantized) ----
    q_bbox = _round_bbox_to_6dp(bbox)
    cache_params: dict[str, Any] = {
        "study_id": study_id,
        "username": resolved_user,
        "geometry_type": gtype,
        "max_records": max_records,
    }
    if q_bbox is not None:
        cache_params["bbox"] = list(q_bbox)
    if sensor_type_id is not None:
        cache_params["sensor_type_id"] = sensor_type_id
    if time_range is not None:
        cache_params["time_range"] = [
            time_range[0].isoformat(),
            time_range[1].isoformat(),
        ]

    result = read_through(
        metadata=_METADATA,
        params=cache_params,
        ext="fgb",
        fetch_fn=lambda: _fetch_movebank_bytes(
            study_id=study_id,
            bbox=q_bbox,
            username=resolved_user,
            password=resolved_pass,
            sensor_type_id=sensor_type_id,
            time_range=time_range,
            max_records=max_records,
            geometry_type=gtype,
        ),
    )
    assert result.uri is not None, (
        "fetch_movebank_tracks is cacheable; uri must be set by read_through"
    )

    layer_label = "Movebank Tracks" if gtype == "linestring" else "Movebank Points"
    return LayerURI(
        layer_id=f"movebank-{study_id}-{gtype}",
        name=f"{layer_label} — study {study_id}",
        layer_type="vector",
        uri=result.uri,
        style_preset="movebank_tracks",
        role="context",
        units=None,
    )
