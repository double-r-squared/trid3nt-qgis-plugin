"""The LAYER door: the map's own round trips - push a layer in, probe a point.

Two routes carry ONE upload flow. The QGIS Python runtime is stdlib-only, so
the plugin cannot PUT to the object store itself: it streams the exported
file's bytes to the file route (a raw body, not multipart - there is no
multipart parser in this codebase and a single file needs none) and the daemon
writes the object; the second route then registers that object onto the case
through the ingest core. The probe samples every raster layer, and any detected
frame sequence, on the case at one point."""

from __future__ import annotations

import asyncio
import json

from aiohttp import web

from trid3nt_server.server.protocol.doors.transport import (
    HttpError, failure, json_reply, raw_reply,
)


class _BadRequest(Exception):
    """Malformed request to one of this door's routes."""


def _ingest_layer_fn():
    """Lazy-import seam for the ingest core (heavy geo deps load on first
    call, not at listener start; monkeypatchable in tests)."""
    from trid3nt_server.inputs.user_layer import ingest_user_layer

    return ingest_user_layer


def _upload_layer_file_fn():
    """Lazy-import seam for the staging-upload helper (monkeypatchable)."""
    from trid3nt_server.inputs.user_layer import upload_layer_file

    return upload_layer_file


def _probe_point_fn():
    """Lazy-import seam for the probe tool (heavy geo deps load on first
    call, not at listener start; monkeypatchable in tests)."""
    from trid3nt_server.tools.derive.probe_point.probe_point import probe_point

    return probe_point


def _max_ingest_bytes() -> int:
    from trid3nt_server.inputs.user_layer import MAX_INGEST_BYTES

    return MAX_INGEST_BYTES


def _json_object(raw_body: bytes, shape: str) -> dict:
    """The request's JSON object, or a 400 naming the shape the route takes;
    a body that is not an object never reaches a handler as one."""
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body.strip() else None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _BadRequest(f"body must be JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise _BadRequest(f"body must be a JSON object like {shape}")
    return payload


async def _handle_ingest_layer_post(raw_body: bytes) -> bytes:
    """Resolve the JSON body for the ingest-layer route: malformed input raises
    a 400, and the ingest's own typed errors propagate for the door to map to
    honest 4xx bodies."""
    payload = _json_object(
        raw_body,
        '{"case_id": "...", "name": "...", "kind": "vector"|"raster", '
        '"s3_uri": "s3://..."}',
    )
    case_id = payload.get("case_id")
    name = payload.get("name")
    kind = payload.get("kind")
    s3_uri = payload.get("s3_uri")
    if not isinstance(case_id, str) or not case_id.strip():
        raise _BadRequest("missing or empty `case_id`")
    if not isinstance(kind, str) or kind not in ("vector", "raster"):
        raise _BadRequest(f'`kind` must be "vector" or "raster", got {kind!r}')
    if not isinstance(s3_uri, str) or not s3_uri.strip():
        raise _BadRequest("missing or empty `s3_uri`")
    crs_authid = payload.get("crs_authid")
    if crs_authid is not None and not isinstance(crs_authid, str):
        raise _BadRequest("`crs_authid` must be a string when given")

    result = await _ingest_layer_fn()(
        case_id=case_id.strip(),
        name=name.strip() if isinstance(name, str) else "",
        kind=kind,
        s3_uri=s3_uri.strip(),
        crs_authid=crs_authid,
        make_aoi=bool(payload.get("make_aoi", False)),
    )
    return json.dumps(result, separators=(",", ":")).encode("utf-8")


async def _handle_probe_point_post(raw_body: bytes) -> bytes:
    """Resolve the JSON body for the probe-point route: malformed input raises a
    400, and the probe's own typed errors propagate for the door to map to
    honest 4xx bodies."""
    payload = _json_object(
        raw_body, '{"case_id": "...", "lon": -85.4, "lat": 30.1}')
    case_id = payload.get("case_id")
    lon = payload.get("lon")
    lat = payload.get("lat")
    if not isinstance(case_id, str) or not case_id.strip():
        raise _BadRequest("missing or empty `case_id`")
    if lon is None or lat is None:
        raise _BadRequest("`lon` and `lat` are both required")

    result = await _probe_point_fn()(point=(lon, lat), case_id=case_id.strip())
    return json.dumps(result, separators=(",", ":")).encode("utf-8")


async def _upload_body(request: web.Request) -> bytes:
    """The upload's bytes, refused at the cap BEFORE the body is read into
    memory and refused empty, because the route has nothing to stage without
    them."""
    cap = _max_ingest_bytes()
    declared = request.content_length or 0
    if declared > cap:
        raise HttpError(
            413, f"upload is {declared} bytes, exceeds the {cap}-byte cap")
    try:
        body = await request.read()
    except Exception as exc:  # noqa: BLE001 -- a half-sent upload is the client's
        raise HttpError(400, "upload body read failed") from exc
    if not body:
        raise HttpError(400, "missing or empty request body")
    return body


@failure("layer upload failed")
async def _ingest_layer_file(request: web.Request) -> web.Response:
    """``POST /api/ingest-layer-file``: stage the client's raw upload bytes to
    object storage."""
    from trid3nt_server.inputs.user_layer import ImportLayerError, ObjectTooLargeError

    body = await _upload_body(request)
    filename = request.query.get("filename", "").strip()
    if not filename:
        raise HttpError(400, "missing `filename` query param")
    try:
        s3_uri = await asyncio.to_thread(_upload_layer_file_fn(), filename, body)
    except ObjectTooLargeError as exc:
        raise HttpError(413, str(exc)) from exc
    except ImportLayerError as exc:
        raise HttpError(400, str(exc)) from exc
    return json_reply({"s3_uri": s3_uri})


@failure("layer ingest failed")
async def _ingest_layer(request: web.Request) -> web.Response:
    """``POST /api/ingest-layer``: register an already-uploaded object onto the
    case, through the ingest core, which merges it into the case's durable
    layer summaries and best-effort-pins the AOI."""
    from trid3nt_server.inputs.user_layer import (
        CaseNotFoundError,
        ImportLayerError,
        ObjectNotFoundError,
    )

    try:
        return raw_reply(await _handle_ingest_layer_post(await request.read()))
    except _BadRequest as exc:
        raise HttpError(400, str(exc)) from exc
    except (CaseNotFoundError, ObjectNotFoundError) as exc:
        raise HttpError(404, str(exc)) from exc
    except ImportLayerError as exc:
        # The request was well-formed HTTP but ingestion cannot succeed.
        raise HttpError(400, str(exc)) from exc


@failure("probe point failed")
async def _probe_point(request: web.Request) -> web.Response:
    """``POST /api/probe-point``: the deterministic map-click probe, sampling
    every raster layer and any detected frame sequence on the case at one point."""
    from trid3nt_server.inputs.user_input import UserInputError
    from trid3nt_server.tools.derive.probe_point.probe_point import (
        ProbePointCaseNotFoundError,
        ProbePointInputError,
    )

    try:
        return raw_reply(await _handle_probe_point_post(await request.read()))
    except _BadRequest as exc:
        raise HttpError(400, str(exc)) from exc
    except ProbePointCaseNotFoundError as exc:
        raise HttpError(404, str(exc)) from exc
    except (ProbePointInputError, UserInputError) as exc:
        raise HttpError(400, str(exc)) from exc


def add_routes(app: web.Application) -> None:
    """Register the layer door's routes on the door's one app."""
    app.router.add_post("/api/ingest-layer-file", _ingest_layer_file)
    app.router.add_post("/api/ingest-layer", _ingest_layer)
    app.router.add_post("/api/probe-point", _probe_point)
