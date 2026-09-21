"""HTTP catalog endpoint: the read-only discovery surface.

It serves the tool catalog as JSON on its own listener beside the WebSocket
server, beside the case, layer, probe and plugin-repository routes and the
telemetry summary the telemetry module aggregates. Every catalog facet derives
from the tool registry, and the endpoints are unauthenticated with open CORS."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from trid3nt_server.adapters import model_discovery

logger = logging.getLogger("trid3nt_server.server.protocol.catalog_http")

__all__ = [
    "build_catalog_payload",
    "load_query_corpus",
    "serve_catalog_http",
    "build_case_list_payload",
    "DEFAULT_HTTP_PORT",
]


DEFAULT_HTTP_PORT = 8766

# Module-level cache: loaded once on the first request and retained until the
# process restarts; a discovery endpoint needs no hot-reload semantics.
_CORPUS_CACHE: dict[str, list[str]] | None = None
_PAYLOAD_CACHE: dict[str, Any] | None = None


def load_query_corpus(path: Path | None = None) -> dict[str, list[str]]:
    """The example-query corpus as ``tool_name -> [query]``, read through the
    search tools that own it and cached for the life of the process: a discovery
    endpoint needs no hot-reload semantics."""
    global _CORPUS_CACHE
    if _CORPUS_CACHE is None:
        from trid3nt_server.tools.search.search_tools.search_tools import (
            _load_corpus,
        )

        _CORPUS_CACHE = _load_corpus(path)
        logger.info("catalog_http: loaded %d tool query entries",
                    len(_CORPUS_CACHE))
    return _CORPUS_CACHE


def _facet_str(value: Any) -> str | None:
    """Coerce a metadata facet (enum / str / None) to a plain string or None."""
    if value is None:
        return None
    s = str(value)
    return s or None


def _credential_facts(tool_name: str) -> dict[str, Any] | None:
    """The credential a tool's source row declares, as wire facts (or None)."""
    from trid3nt_server.credentials.resolver import credential_for_tool

    credential = credential_for_tool(tool_name)
    if credential is None:
        return None
    return {
        "name": credential.name,
        "label": credential.label,
        "signup_url": credential.signup_url,
        "env_var": credential.env_var,
    }


def build_catalog_payload(
    *,
    corpus: dict[str, list[str]] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Assemble the flat ``/api/tool-catalog`` payload: a thin reader over the
    registry listing every tool with its REAL docstring, the exact text the
    model routes on, and the metadata facets the tool already carries."""
    from trid3nt_server.tools import TOOL_REGISTRY

    global _PAYLOAD_CACHE
    if use_cache and _PAYLOAD_CACHE is not None:
        return _PAYLOAD_CACHE

    corpus_map = corpus if corpus is not None else load_query_corpus()

    tools_out: list[dict[str, Any]] = []
    for name in sorted(TOOL_REGISTRY.keys()):
        entry = TOOL_REGISTRY[name]
        meta = entry.metadata
        # A declared tool renders TWO views of its docstring; this page is a
        # CHOOSE-the-tool surface, so it takes the routing one. Everything else
        # has only its written docstring.
        doc_full = (getattr(entry.fn, "routing_doc", None)
                    or entry.fn.__doc__ or "").strip()
        # Cap to 3 sample queries -- the page shows 2-3; sending all 5-10 wastes
        # bandwidth on a discovery surface.
        sample_queries = list(corpus_map.get(name, []))[:3]
        tools_out.append(
            {
                "name": name,
                "docstring": doc_full,
                "engine": _facet_str(getattr(meta, "engine", None)),
                "tier": _facet_str(getattr(meta, "tier", "general")) or "general",
                "source_class": _facet_str(meta.source_class),
                "supports_global_query": bool(meta.supports_global_query),
                "cacheable": bool(meta.cacheable),
                "ttl_class": str(meta.ttl_class),
                "annotations": {
                    "read_only_hint": bool(meta.read_only_hint),
                    "open_world_hint": bool(meta.open_world_hint),
                    "destructive_hint": bool(meta.destructive_hint),
                    "idempotent_hint": bool(meta.idempotent_hint),
                },
                "sample_queries": sample_queries,
                # The key this source needs, verbatim off its row, so the
                # plugin's keys form has one row per credential and no table of
                # its own. NO key material: a name, a label, a signup url and
                # the env var the daemon falls back to.
                "credential": _credential_facts(name),
            }
        )

    payload = {
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "tool_count": len(tools_out),
        "tools": tools_out,
    }
    if use_cache:
        _PAYLOAD_CACHE = payload
    return payload


# Building click-to-enrich detail endpoint.
#
# The building footprint inline GeoJSON now carries ID-only props (osm_id /
# osm_type / a composite fid). The full tag bag (building / height / levels /
# name / addr:*) is persisted in a per-AOI sidecar next to the .fgb
# (cache/static-30d/buildings/<key>.tags.json) keyed by fid. This endpoint reads
# that sidecar for a clicked (osm_type, osm_id); if no sidecar carries the fid it
# falls back to a LIVE Overpass-by-id query. Non-blocking: S3 + Overpass run via
# asyncio.to_thread so the agent's WS heartbeat is never starved.


class _BuildingDetailNotFound(Exception):
    """No tag bag found for the requested building (sidecar miss + live miss)."""


class _BuildingDetailBadRequest(Exception):
    """Malformed /api/building-detail request (missing/invalid osm_type|osm_id)."""


def _parse_building_detail_qs(query_string: str) -> tuple[str, str]:
    """Parse and validate the OSM element kind and id from the query string;
    anything malformed raises, so the handler emits a typed 400 rather than a
    fabricated success."""
    from urllib.parse import parse_qs

    params = parse_qs(query_string, keep_blank_values=False)
    osm_type_raw = (params.get("osm_type") or [""])[0].strip().lower()
    osm_id_raw = (params.get("osm_id") or [""])[0].strip()
    if osm_type_raw not in ("way", "relation", "node"):
        raise _BuildingDetailBadRequest(
            f"osm_type must be way|relation|node, got {osm_type_raw!r}"
        )
    if not osm_id_raw or not osm_id_raw.isdigit():
        raise _BuildingDetailBadRequest(
            f"osm_id must be a positive integer, got {osm_id_raw!r}"
        )
    return osm_type_raw, osm_id_raw


async def _handle_building_detail(query_string: str) -> bytes:
    """Resolve the ``{fid, tags}`` body for the building-detail route; malformed
    input raises a 400 and tags found in neither the sidecar nor the live read
    raise a 404. Both reads run off the event loop."""
    from trid3nt_server.tools.fetchers.socioeconomic.fetch_buildings import hooks

    osm_type, osm_id = _parse_building_detail_qs(query_string)
    fid = hooks.building_fid(osm_type, osm_id)

    tags = await asyncio.to_thread(hooks.tags_from_sidecars, fid)
    if tags is None:
        # Sidecar miss (cold box, evicted, or never written) -> live by-id.
        tags = await asyncio.to_thread(hooks.tags_from_overpass, osm_type, osm_id)
    if tags is None:
        raise _BuildingDetailNotFound(
            f"no tags for {osm_type}/{osm_id} (sidecar + live Overpass both empty)"
        )
    return json.dumps(
        {"fid": fid, "tags": tags}, separators=(",", ":")
    ).encode("utf-8")








# The cold case list: the same rows the WS session emits, over plain HTTP, so a
# client can populate its dialog BEFORE a WebSocket connection exists.
#
# The WS path scopes rows to the handshake's user. A cold caller has no
# handshake, and this build collapses every connection onto one fixed local
# user id, so it resolves the identical id anyway.


class _CaseListPersistenceUnavailable(Exception):
    """Persistence is unbound; the case list cannot be sourced (-> 503)."""


#: The case-list row the client reads: the envelope serializes every Case field,
#: and these four are what a left-rail row renders. A case with no bbox carries
#: an honest None.
_CASE_ROW_FIELDS = ("case_id", "title", "updated_at", "bbox")


async def build_case_list_payload() -> dict[str, Any]:
    """Assemble the case-list payload newest-first, through the same
    persistence call the WS path makes; unbound persistence raises, so the route
    answers an honest 503 rather than a fabricated empty list."""
    from trid3nt_server.credentials.auth_handshake import LOCAL_SINGLE_USER_ID
    from trid3nt_server.server import get_persistence

    persistence = get_persistence()
    if persistence is None:
        raise _CaseListPersistenceUnavailable("persistence unavailable")

    from trid3nt_contracts.case import CaseListEnvelopePayload

    cases = await persistence.list_cases_for_user(LOCAL_SINGLE_USER_ID)
    serialized = CaseListEnvelopePayload(cases=cases).model_dump(mode="json")
    rows = [{k: row.get(k) for k in _CASE_ROW_FIELDS}
            for row in serialized["cases"]]
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return {"cases": rows}


# /api/ingest-layer(-file) -- bidirectional layer push (QGIS plugin -> case).
#
# The reverse seam of layer materialization: the plugin's "Push layer" button
# sends the user's ACTIVE QGIS layer (vector or raster) into the current
# case as a first-class input layer. Two routes, ONE upload flow:
#
#   POST /api/ingest-layer-file?filename=<name>  -- raw request-body upload.
#     The QGIS Python runtime has no boto3 (stdlib-only), so the plugin
#     cannot PUT to MinIO directly; it streams the exported file's bytes
#     here (Content-Type: application/octet-stream, NOT multipart/form-data
#     -- this codebase has no multipart parser anywhere and a raw-body PUT is
#     the simplest correct shape for a single-file upload) and the agent does
#     the actual object-store write. Returns {"s3_uri": "s3://..."}.
#
#   POST /api/ingest-layer {"case_id", "name", "kind", "s3_uri",
#     "crs_authid"?, "make_aoi"?}  -- registers an ALREADY-uploaded object
#     (normally the s3_uri from the call above) onto the case. Runs the
#     ingest_user_layer core (inputs/user_layer.py): validates the object
#     exists + is within the size cap, converts/validates the artifact,
#     merges it into the case's durable loaded_layer_summaries, and
#     best-effort-pins the AOI when make_aoi is true.


class _IngestLayerBadRequest(Exception):
    """Malformed /api/ingest-layer(-file) request."""


def _ingest_layer_fn():
    """Lazy-import seam for the ingest core (heavy geo deps load on first
    call, not at listener start; monkeypatchable in tests)."""
    from trid3nt_server.inputs.user_layer import ingest_user_layer

    return ingest_user_layer


def _upload_layer_file_fn():
    """Lazy-import seam for the staging-upload helper (monkeypatchable)."""
    from trid3nt_server.inputs.user_layer import upload_layer_file

    return upload_layer_file


async def _handle_ingest_layer_post(raw_body: bytes) -> bytes:
    """Resolve the JSON body for the ingest-layer route: malformed input raises
    a 400, and the ingest's own typed errors propagate for the dispatcher to map
    to honest 4xx bodies."""
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body.strip() else None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _IngestLayerBadRequest(f"body must be JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise _IngestLayerBadRequest(
            'body must be a JSON object like {"case_id": "...", "name": "...", '
            '"kind": "vector"|"raster", "s3_uri": "s3://..."}'
        )
    case_id = payload.get("case_id")
    name = payload.get("name")
    kind = payload.get("kind")
    s3_uri = payload.get("s3_uri")
    if not isinstance(case_id, str) or not case_id.strip():
        raise _IngestLayerBadRequest("missing or empty `case_id`")
    if not isinstance(kind, str) or kind not in ("vector", "raster"):
        raise _IngestLayerBadRequest(
            f'`kind` must be "vector" or "raster", got {kind!r}'
        )
    if not isinstance(s3_uri, str) or not s3_uri.strip():
        raise _IngestLayerBadRequest("missing or empty `s3_uri`")
    crs_authid = payload.get("crs_authid")
    if crs_authid is not None and not isinstance(crs_authid, str):
        raise _IngestLayerBadRequest("`crs_authid` must be a string when given")
    make_aoi = bool(payload.get("make_aoi", False))

    result = await _ingest_layer_fn()(
        case_id=case_id.strip(),
        name=name.strip() if isinstance(name, str) else "",
        kind=kind,
        s3_uri=s3_uri.strip(),
        crs_authid=crs_authid,
        make_aoi=make_aoi,
    )
    return json.dumps(result, separators=(",", ":")).encode("utf-8")


def _parse_ingest_layer_filename(query_string: str) -> str:
    """Extract + validate the ``filename`` query param for the upload route."""
    from urllib.parse import parse_qs

    params = parse_qs(query_string, keep_blank_values=False)
    filename = (params.get("filename") or [""])[0].strip()
    if not filename:
        raise _IngestLayerBadRequest("missing `filename` query param")
    return filename


# The deterministic map-click point probe: samples every raster layer, and any
# detected frame sequence, on the case at one point.


class _ProbePointBadRequest(Exception):
    """Malformed /api/probe-point request."""


def _probe_point_fn():
    """Lazy-import seam for the probe tool (heavy geo deps load on first
    call, not at listener start; monkeypatchable in tests)."""
    from trid3nt_server.tools.derive.probe_point.probe_point import probe_point

    return probe_point


async def _handle_probe_point_post(raw_body: bytes) -> bytes:
    """Resolve the JSON body for the probe-point route: malformed input raises a
    400, and the probe's own typed errors propagate for the dispatcher to map to
    honest 4xx bodies."""
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body.strip() else None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ProbePointBadRequest(f"body must be JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise _ProbePointBadRequest(
            'body must be a JSON object like {"case_id": "...", "lon": -85.4, '
            '"lat": 30.1}'
        )
    case_id = payload.get("case_id")
    lon = payload.get("lon")
    lat = payload.get("lat")
    if not isinstance(case_id, str) or not case_id.strip():
        raise _ProbePointBadRequest("missing or empty `case_id`")
    if lon is None or lat is None:
        raise _ProbePointBadRequest("`lon` and `lat` are both required")

    result = await _probe_point_fn()(point=(lon, lat), case_id=case_id.strip())
    return json.dumps(result, separators=(",", ":")).encode("utf-8")





_HTTP_VERSION = b"HTTP/1.1"
_CRLF = b"\r\n"
_JSON = "application/json; charset=utf-8"


class _HttpError(Exception):
    """The status and message one route answers a rejected request with."""

    def __init__(self, status: int, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class _Request:
    """One parsed request: the path with its query split off, the body already
    read to Content-Length, and the Host the client dialled."""

    method: str
    path: str
    query: str
    body: bytes
    host: str


@dataclass(frozen=True)
class _Reply:
    """What a route answers with: the bytes, their type, and any header a
    download needs beyond the common set."""

    body: bytes
    content_type: str = _JSON
    headers: dict[str, str] | None = None


@dataclass(frozen=True)
class _Route:
    """One entry of the route table. ``failure`` is what an unhandled fault
    answers with, so no route hand-writes a 500. A POST route reads its body
    first: ``body_required`` refuses an empty one, ``body_cap`` names the byte
    cap to reject at BEFORE the body is read into memory."""

    handler: Any
    failure: str
    body_required: bool = False
    body_cap: Any = None
    body_timeout: float = 30.0


def _json_reply(payload: Any) -> _Reply:
    """A JSON body with no whitespace, the one shape every data route answers in."""
    return _Reply(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _json_error(status: int, message: str, detail: str = "") -> bytes:
    """The one error body: a message, and a detail only where the route has one."""
    payload: dict[str, Any] = {"error": message}
    if detail:
        payload["detail"] = detail
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _format_response(
    status: int,
    body: bytes,
    *,
    content_type: str = _JSON,
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    """Assemble a minimal HTTP/1.1 response."""
    reason = {
        200: "OK",
        204: "No Content",
        400: "Bad Request",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        413: "Payload Too Large",
        500: "Internal Server Error",
        502: "Bad Gateway",
        503: "Service Unavailable",
    }.get(status, "OK")
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        # CORS -- see module docstring. POST is scoped to the ingest/probe routes.
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Cache-Control": "no-cache",
        "Connection": "close",
    }
    if extra_headers:
        headers.update(extra_headers)
    header_lines = (
        _HTTP_VERSION
        + b" "
        + str(status).encode()
        + b" "
        + reason.encode()
        + _CRLF
    )
    for k, v in headers.items():
        header_lines += f"{k}: {v}".encode() + _CRLF
    return header_lines + _CRLF + body


async def _route_tool_catalog(_req: _Request) -> _Reply:
    """``GET /api/tool-catalog``: every registered tool with its routing
    docstring and the credential its source row declares."""
    return _json_reply(build_catalog_payload())


async def _route_telemetry_summary(_req: _Request) -> _Reply:
    """``GET /api/telemetry/summary``: the routing-quality summary the telemetry
    module aggregates over its own sink."""
    from trid3nt_server.telemetry import build_telemetry_summary

    return _json_reply(await build_telemetry_summary())


async def _route_case_list(_req: _Request) -> _Reply:
    """``GET /api/case-list``: the cold case list, for a client with no
    WebSocket session yet."""
    try:
        return _json_reply(await build_case_list_payload())
    except _CaseListPersistenceUnavailable as exc:
        raise _HttpError(503, str(exc)) from exc


async def _route_local_models(_req: _Request) -> _Reply:
    """``GET /api/local-models``: the installed local models, for a client's
    model picker. Absent, like any unknown path, unless the local provider is
    active; the upstream fetch runs off the event loop."""
    if not model_discovery._local_models_route_enabled():
        raise _HttpError(404, "not found")
    try:
        body = await asyncio.to_thread(model_discovery._fetch_local_models)
    except model_discovery._LocalModelsUpstreamError as exc:
        raise _HttpError(502, str(exc)) from exc
    return _Reply(body)


async def _route_building_detail(req: _Request) -> _Reply:
    """``GET /api/building-detail``: the full tag bag for one clicked footprint,
    read off the per-AOI sidecar or live Overpass, both off the event loop."""
    try:
        return _Reply(await _handle_building_detail(req.query))
    except _BuildingDetailNotFound as exc:
        raise _HttpError(404, "building detail not found", str(exc)) from exc
    except _BuildingDetailBadRequest as exc:
        raise _HttpError(400, "bad request", str(exc)) from exc


async def _route_version(_req: _Request) -> _Reply:
    """``GET /api/version``: the daemon's git sha and active model provider."""
    from trid3nt_server import plugin_repo

    return _json_reply(await asyncio.to_thread(plugin_repo.build_version_payload))


async def _route_plugins_xml(req: _Request) -> _Reply:
    """``GET /plugin-repo/plugins.xml``: the QGIS custom repository index. The
    download_url host is filled from the REQUEST's own Host header, so a tailnet
    client's "Add repository" URL round-trips to a reachable zip URL."""
    from trid3nt_server import plugin_repo

    host = req.host or (
        f"127.0.0.1:{os.environ.get('TRID3NT_AGENT_HTTP_PORT', DEFAULT_HTTP_PORT)}"
    )
    try:
        body = await asyncio.to_thread(plugin_repo.render_plugins_xml, host)
    except plugin_repo.PluginRepoBuildError as exc:
        raise _HttpError(503, str(exc)) from exc
    return _Reply(body, content_type="text/xml; charset=utf-8")


async def _route_fresh_zip(_req: _Request) -> _Reply:
    """``GET /plugin-repo/trid3nt.zip``: THE zip every plugins.xml download_url
    points at, built on demand straight from ``plugin/`` and mtime-cached."""
    from trid3nt_server import plugin_repo

    try:
        data, _version, zip_filename = await asyncio.to_thread(
            plugin_repo.build_fresh_zip
        )
    except plugin_repo.PluginRepoBuildError as exc:
        raise _HttpError(503, str(exc)) from exc
    return _Reply(
        data,
        content_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_filename}"'},
    )


async def _route_packaged_zip(req: _Request) -> _Reply:
    """``GET /plugin-repo/<name>.zip``: the versioned zip ``package_plugin_repo``
    built, kept as the manual-QA fallback path plugins.xml no longer advertises."""
    from trid3nt_server import plugin_repo

    filename = req.path[len("/plugin-repo/"):]
    try:
        zip_path = await asyncio.to_thread(plugin_repo.served_zip_path, filename)
        data = await asyncio.to_thread(zip_path.read_bytes)
    except FileNotFoundError as exc:
        raise _HttpError(404, "not found") from exc
    return _Reply(
        data,
        content_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_path.name}"'},
    )


async def _route_ingest_layer_file(req: _Request) -> _Reply:
    """``POST /api/ingest-layer-file``: stage the client's raw upload bytes to
    object storage. The QGIS Python runtime is stdlib-only, so the plugin cannot
    PUT to the store itself and streams the exported file's bytes here."""
    from trid3nt_server.inputs.user_layer import ImportLayerError, ObjectTooLargeError

    try:
        filename = _parse_ingest_layer_filename(req.query)
        s3_uri = await asyncio.to_thread(
            _upload_layer_file_fn(), filename, req.body
        )
    except _IngestLayerBadRequest as exc:
        raise _HttpError(400, str(exc)) from exc
    except ObjectTooLargeError as exc:
        raise _HttpError(413, str(exc)) from exc
    except ImportLayerError as exc:
        raise _HttpError(400, str(exc)) from exc
    return _json_reply({"s3_uri": s3_uri})


async def _route_ingest_layer(req: _Request) -> _Reply:
    """``POST /api/ingest-layer``: register an already-uploaded object onto the
    case, through the ingest core, which merges it into the case's durable
    layer summaries and best-effort-pins the AOI."""
    from trid3nt_server.inputs.user_layer import (
        CaseNotFoundError,
        ImportLayerError,
        ObjectNotFoundError,
    )

    try:
        return _Reply(await _handle_ingest_layer_post(req.body))
    except _IngestLayerBadRequest as exc:
        raise _HttpError(400, str(exc)) from exc
    except (CaseNotFoundError, ObjectNotFoundError) as exc:
        raise _HttpError(404, str(exc)) from exc
    except ImportLayerError as exc:
        # The request was well-formed HTTP but ingestion cannot succeed.
        raise _HttpError(400, str(exc)) from exc


async def _route_probe_point(req: _Request) -> _Reply:
    """``POST /api/probe-point``: the deterministic map-click probe, sampling
    every raster layer and any detected frame sequence on the case at one point."""
    from trid3nt_server.inputs.user_input import UserInputError
    from trid3nt_server.tools.derive.probe_point.probe_point import (
        ProbePointCaseNotFoundError,
        ProbePointInputError,
    )

    try:
        return _Reply(await _handle_probe_point_post(req.body))
    except _ProbePointBadRequest as exc:
        raise _HttpError(400, str(exc)) from exc
    except ProbePointCaseNotFoundError as exc:
        raise _HttpError(404, str(exc)) from exc
    except (ProbePointInputError, UserInputError) as exc:
        raise _HttpError(400, str(exc)) from exc


async def _route_provider_config(req: _Request) -> _Reply:
    """``POST /api/provider-config``: a provider, model or key switch that takes
    effect on the NEXT turn with no restart, because the adapter reads the env
    per call. The api_key rides the body into the env and is never logged or
    echoed; the coherence gate's short blocking probe runs off the event loop."""
    if not model_discovery._local_models_route_enabled():
        raise _HttpError(404, "not found")
    try:
        body = await asyncio.to_thread(
            model_discovery.apply_provider_config, req.body)
    except model_discovery.ProviderConfigBadRequest as exc:
        raise _HttpError(400, str(exc)) from exc
    return _Reply(body)


def _max_ingest_bytes() -> int:
    from trid3nt_server.inputs.user_layer import MAX_INGEST_BYTES

    return MAX_INGEST_BYTES


#: (method, path) -> the route that answers it. An exact miss on the path falls
#: through to the packaged-zip prefix and then to 404; a hit on the path under
#: another method is 405.
_ROUTES: dict[tuple[str, str], _Route] = {
    ("GET", "/api/tool-catalog"): _Route(
        _route_tool_catalog, "catalog build failed"),
    ("GET", "/api/telemetry/summary"): _Route(
        _route_telemetry_summary, "telemetry summary failed"),
    ("GET", "/api/case-list"): _Route(
        _route_case_list, "case list failed"),
    ("GET", "/api/local-models"): _Route(
        _route_local_models, "local models failed"),
    ("GET", "/api/building-detail"): _Route(
        _route_building_detail, "building detail failed"),
    ("GET", "/api/version"): _Route(
        _route_version, "version lookup failed"),
    ("GET", "/plugin-repo/plugins.xml"): _Route(
        _route_plugins_xml, "plugin repo index failed"),
    ("GET", "/plugin-repo/trid3nt.zip"): _Route(
        _route_fresh_zip, "plugin zip failed"),
    ("POST", "/api/ingest-layer-file"): _Route(
        _route_ingest_layer_file, "layer upload failed",
        body_required=True, body_cap=_max_ingest_bytes, body_timeout=120.0),
    ("POST", "/api/ingest-layer"): _Route(
        _route_ingest_layer, "layer ingest failed"),
    ("POST", "/api/probe-point"): _Route(
        _route_probe_point, "probe point failed"),
    ("POST", "/api/provider-config"): _Route(
        _route_provider_config, "provider config update failed"),
}


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[_Request, _Route | None] | _Reply | None:
    """Parse one request and find its route, or answer it outright.

    A parse fault, an unknown route and a body the route refuses each come back
    as the reply to write; ``None`` means the peer left nothing to answer."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    except asyncio.TimeoutError:
        return None
    if not request_line:
        return None
    try:
        method, raw_path, _version = request_line.decode("ascii", "replace").split()
    except ValueError:
        raise _HttpError(400, "bad request line") from None

    # The headers consumed are Content-Length, so a POST body can be read, and
    # Host, so plugins.xml builds a download_url matching the host:port the
    # client actually dialled. The socket must be advanced past the rest before
    # the close so the client sees the response cleanly.
    content_length = 0
    host_header = ""
    while True:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        except asyncio.TimeoutError:
            break
        if not line or line == b"\r\n" or line == b"\n":
            break
        name, _, value = line.decode("latin-1", "replace").partition(":")
        header_name = name.strip().lower()
        if header_name == "content-length":
            try:
                content_length = int(value.strip())
            except ValueError:
                content_length = 0
        elif header_name == "host":
            host_header = value.strip()

    if method == "OPTIONS":
        # CORS preflight.
        return _Reply(b"", content_type=_JSON)

    path, _, query = raw_path.partition("?")
    route = _ROUTES.get((method, path))
    if route is None and method == "GET" and path.startswith("/plugin-repo/") \
            and path.endswith(".zip"):
        route = _Route(_route_packaged_zip, "plugin zip failed")
    if route is None:
        # A GET nobody serves is an unknown path; anything else is the method.
        if method != "GET":
            raise _HttpError(405, "method not allowed")
        raise _HttpError(404, "not found")

    body = b""
    if content_length > 0:
        cap = route.body_cap() if route.body_cap is not None else None
        if cap is not None and content_length > cap:
            # Reject BEFORE reading the oversized body into memory.
            raise _HttpError(
                413, f"upload is {content_length} bytes, exceeds the {cap}-byte cap")
        try:
            body = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=route.body_timeout)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            if route.body_required:
                raise _HttpError(400, "upload body read failed") from exc
            body = b""
    if route.body_required and not body:
        raise _HttpError(400, "missing or empty request body")
    return _Request(method, path, query, body, host_header), route


async def _handle_http(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Handle one HTTP request off the route table: an unknown path is 404, a
    known path under the wrong method 405, and every fault a route does not name
    itself answers with that route's one failure message."""
    status = 200
    reply: _Reply | None = None
    failure = "request failed"
    try:
        parsed = await _read_request(reader)
        if parsed is None:
            writer.close()
            return
        if isinstance(parsed, _Reply):
            status, reply = 204, parsed
        else:
            request, route = parsed
            failure = route.failure
            reply = await route.handler(request)
    except _HttpError as exc:
        status = exc.status
        reply = _Reply(_json_error(exc.status, exc.message, exc.detail))
    except Exception:  # noqa: BLE001 -- one honest 500 per route, never a traceback
        logger.exception("%s", failure)
        status = 500
        reply = _Reply(_json_error(500, failure))
    writer.write(
        _format_response(
            status,
            reply.body,
            content_type=reply.content_type,
            extra_headers=reply.headers,
        )
    )
    await writer.drain()
    writer.close()




async def serve_catalog_http(
    host: str = "127.0.0.1",
    port: int | None = None,
) -> asyncio.AbstractServer:
    """Start the catalog HTTP listener and return the server handle; the port
    comes from ``TRID3NT_AGENT_HTTP_PORT`` when not passed. It runs on the same
    loop and process as the WebSocket server."""
    if port is None:
        try:
            port = int(os.environ.get("TRID3NT_AGENT_HTTP_PORT", DEFAULT_HTTP_PORT))
        except ValueError:
            port = DEFAULT_HTTP_PORT
    server = await asyncio.start_server(_handle_http, host, port)
    logger.info(
        "tool-catalog HTTP server listening host=%s port=%d", host, port
    )
    return server
