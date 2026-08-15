"""movebank_tracks hooks (movebank finish wave, keyed http_json CSV/0073/0077):
Movebank animal-tracking direct-read, keyed with COMPOSITE Basic-Auth creds.

Movebank rejects unauthenticated requests, so ``build_request`` resolves a
(username, password) pair -- explicit kwargs -> a ``user:pass`` secret_ref blob ->
``TRID3NT_MOVEBANK_USER`` + ``TRID3NT_MOVEBANK_PASSWORD`` env -- and emits ONE GET
carrying a ``Authorization: Basic <b64>`` header (the resolver blob path; the key is
NEVER registered, the missing-creds MOVEBANK_INPUT_ERROR is the parity surface). The
body is CSV (not JSON), so ``parse_response`` parses the direct-read CSV into per-fix
records, applies the ``max_records`` cap, then shapes them by ``geometry_type``:
``point`` = one Point per fix (individual_id/timestamp/sensor_type_id/study_id);
``linestring`` = one LineString per individual (vertices timestamp-ordered) with the
n_points/first_timestamp/last_timestamp/study_id schema, dropping any individual whose
track is not ENTIRELY inside the bbox (the conservative clip). A 200 licence-terms HTML
body raises MOVEBANK_LICENSE_ERROR at parse. ``classify_status`` splits the transport
status the executor would collapse: 401 -> MOVEBANK_AUTH_ERROR, 403 ->
MOVEBANK_LICENSE_ERROR, other 4xx -> MOVEBANK_INPUT_ERROR, 5xx -> the default upstream.
"""

from __future__ import annotations

import base64
import csv
import io
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import RouterError, router_input_error, router_upstream_error

__all__ = ["build_request", "parse_response", "classify_status"]

_MOVEBANK_URL = "https://www.movebank.org/movebank/service/direct-read"
_REQUEST_ATTRIBUTES = (
    "individual_local_identifier,timestamp,location_lat,location_long,sensor_type_id"
)
_USER_ENV = "TRID3NT_MOVEBANK_USER"
_PASS_ENV = "TRID3NT_MOVEBANK_PASSWORD"


def _resolve_credentials(sc: str, params: dict[str, Any]) -> tuple[str, str]:
    """Resolve (username, password): kwargs -> ``user:pass`` secret_ref blob -> env.

    Raises a credential-shaped MOVEBANK_INPUT_ERROR (the twin's missing-creds code)
    BEFORE any network call when none of the three paths yield a full pair. The key
    is never registered; this typed error is the offline parity surface.
    """
    user = params.get("username")
    pw = params.get("password")
    if user and pw:
        return str(user), str(pw)
    secret_ref = params.get("secret_ref")
    if isinstance(secret_ref, str) and secret_ref:
        blob = secret_ref.strip()
        if ":" in blob:
            b_user, _, b_pass = blob.partition(":")
            user = user or b_user
            pw = pw or b_pass
        else:
            # A password-only blob; the username must come from a kwarg / env.
            pw = pw or blob
    if not user:
        user = os.environ.get(_USER_ENV)
    if not pw:
        pw = os.environ.get(_PASS_ENV)
    if not user or not pw:
        raise router_input_error(
            sc,
            "Movebank credentials missing: pass (username + password) OR a "
            "'user:pass' secret_ref OR set TRID3NT_MOVEBANK_USER + "
            "TRID3NT_MOVEBANK_PASSWORD",
        )
    return str(user), str(pw)


def _basic_auth_header(user: str, pw: str) -> str:
    token = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _format_movebank_timestamp(iso: str) -> str:
    """Movebank wants ``YYYYMMDDhhmmssSSS`` (UTC, millisecond) from an ISO string."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    utc = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return utc.strftime("%Y%m%d%H%M%S") + f"{utc.microsecond // 1000:03d}"


@_hooks.register_hook("movebank_tracks.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Resolve creds, emit ONE Basic-Auth direct-read CSV GET (raises MISSING pre-network)."""
    sc = spec.error_code_prefix
    user, pw = _resolve_credentials(sc, params)
    q: dict[str, Any] = {
        "entity_type": "event",
        "study_id": int(params["study_id"]),
        "attributes": _REQUEST_ATTRIBUTES,
    }
    if params.get("sensor_type_id") is not None:
        q["sensor_type_id"] = int(params["sensor_type_id"])
    tr = params.get("time_range")
    if tr:
        q["timestamp_start"] = _format_movebank_timestamp(str(tr[0]))
        q["timestamp_end"] = _format_movebank_timestamp(str(tr[1]))
    headers = {
        "Authorization": _basic_auth_header(user, pw),
        "User-Agent": spec.auth.user_agent,
    }
    return [_hooks.RequestPlan(url=_MOVEBANK_URL, params=q, headers=headers)]


def _parse_movebank_csv(body: str) -> list[dict[str, Any]]:
    """Direct-read CSV -> per-fix records (hyphen/underscore column tolerant)."""
    if not body.strip():
        return []
    reader = csv.DictReader(io.StringIO(body))
    out: list[dict[str, Any]] = []
    for row in reader:
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
        if lon_raw in (None, "") or lat_raw in (None, ""):
            continue
        try:
            lon_f = float(lon_raw)
            lat_f = float(lat_raw)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lon_f) and math.isfinite(lat_f)):
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
    return out


@_hooks.register_hook("movebank_tracks.parse_response")
def parse_response(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """CSV -> per-geometry_type GeoJSON features with the conservative bbox clip."""
    sc = spec.error_code_prefix
    raw = bodies[0] if bodies else b""
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise router_upstream_error(sc, f"Movebank body not UTF-8: {exc}")
    # A 200 licence-terms HTML page (not CSV) -> the licence-acceptance typed error.
    if "<html" in body[:200].lower() or "License Terms" in body[:500]:
        raise router_input_error(
            sc,
            "Movebank returned an HTML licence-acceptance page; the account must "
            "accept the study's Data Use Statement at movebank.org.",
            "LICENSE_ERROR",
        )
    first_line = body.splitlines()[0] if body else ""
    if first_line and "," not in first_line and "individual" not in first_line:
        raise router_upstream_error(
            sc, f"Movebank direct-read body does not look like CSV: {body[:200]!r}"
        )

    records = _parse_movebank_csv(body)
    max_records = int(params.get("max_records", 500_000))
    if len(records) > max_records:
        records = records[:max_records]

    geometry_type = str(params.get("geometry_type", "linestring"))
    study_id = int(params["study_id"])
    bbox = params.get("bbox")
    if bbox is not None:
        west, south, east, north = (float(v) for v in bbox)

        def _in_bbox(lon: float, lat: float) -> bool:
            return west <= lon <= east and south <= lat <= north
    else:

        def _in_bbox(lon: float, lat: float) -> bool:  # noqa: ARG001
            return True

    feats: list[dict[str, Any]] = []
    if geometry_type == "point":
        for rec in records:
            if not _in_bbox(rec["lon"], rec["lat"]):
                continue
            feats.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [rec["lon"], rec["lat"]]},
                    "properties": {
                        "individual_id": rec["individual_id"],
                        "timestamp": rec["timestamp_iso"],
                        "sensor_type_id": rec["sensor_type_id"],
                        "study_id": study_id,
                    },
                }
            )
    else:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for rec in records:
            grouped[rec["individual_id"]].append(rec)
        for ind, recs in grouped.items():
            recs_sorted = sorted(recs, key=lambda r: r["timestamp_iso"])
            # Conservative clip: ALL vertices must be in-bbox or the track is dropped.
            if bbox is not None and not all(
                _in_bbox(r["lon"], r["lat"]) for r in recs_sorted
            ):
                continue
            if len(recs_sorted) < 2:
                continue
            coords = [[r["lon"], r["lat"]] for r in recs_sorted]
            feats.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": coords},
                    "properties": {
                        "individual_id": ind,
                        "n_points": len(recs_sorted),
                        "first_timestamp": recs_sorted[0]["timestamp_iso"],
                        "last_timestamp": recs_sorted[-1]["timestamp_iso"],
                        "study_id": study_id,
                    },
                }
            )
    return feats


@_hooks.register_hook("movebank_tracks.classify_status")
def classify_status(
    spec: SourceSpec, status: int | None, body: str | None
) -> RouterError | None:
    """401 -> AUTH_ERROR, 403 -> LICENSE_ERROR, other 4xx -> INPUT_ERROR, else default."""
    sc = spec.error_code_prefix
    if status == 401:
        return router_input_error(
            sc, "Movebank rejected the credentials (HTTP 401)", "AUTH_ERROR"
        )
    if status == 403:
        return router_input_error(
            sc,
            "Movebank account has not accepted the study's Data Use Statement "
            "(HTTP 403); accept the licence at movebank.org before retrying.",
            "LICENSE_ERROR",
        )
    if status is not None and 400 <= status < 500:
        return router_input_error(
            sc, f"Movebank direct-read returned {status}: {(body or '')[:200]}"
        )
    return None
