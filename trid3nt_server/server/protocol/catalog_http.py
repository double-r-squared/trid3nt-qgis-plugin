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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from trid3nt_server.adapters import model_discovery

logger = logging.getLogger("trid3nt_server.server.protocol.catalog_http")

__all__ = [
    "build_catalog_payload",
    "render_catalog_page",
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


# Self-contained catalog page. Inline CSS + JS + embedded data -- NO external
# assets (a strict-CSP / offline viewer must render it unchanged). The data is
# embedded as a JSON <script> block the inline JS reads once; facets and search
# are derived from it entirely client-side.
_CATALOG_PAGE_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TRID3NT tool catalog</title>
<style>
:root{color-scheme:light dark;--bg:#fff;--fg:#1a1a1a;--muted:#666;--card:#f6f7f9;--border:#dcdfe4;--badge:#e6ebf2;--accent:#2d6cdf}
@media(prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e6e8eb;--muted:#9aa2ad;--card:#1d2027;--border:#2c313a;--badge:#252b36;--accent:#5b8cf0}}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--border);padding:16px 20px;z-index:2}
h1{margin:0 0 4px;font-size:20px}
.sub{color:var(--muted);font-size:13px}
.controls{margin-top:12px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
#q{flex:1 1 260px;min-width:200px;padding:8px 10px;border:1px solid var(--border);border-radius:8px;background:var(--card);color:var(--fg);font:inherit}
select{padding:7px 8px;border:1px solid var(--border);border-radius:8px;background:var(--card);color:var(--fg);font:inherit}
main{padding:16px 20px;max-width:1100px;margin:0 auto}
.tool{border:1px solid var(--border);background:var(--card);border-radius:10px;padding:14px 16px;margin:0 0 12px}
.tool h2{margin:0;font:600 15px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}
.badges{margin:6px 0 8px;display:flex;flex-wrap:wrap;gap:6px}
.badge{font-size:11px;padding:2px 8px;border-radius:999px;background:var(--badge);color:var(--muted);white-space:nowrap}
.badge.eng{color:var(--accent)}
pre.doc{margin:0;white-space:pre-wrap;word-break:break-word;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--fg)}
.sq{margin-top:8px;font-size:12px;color:var(--muted)}
.sq b{color:var(--fg);font-weight:600}
.empty{color:var(--muted);padding:40px 0;text-align:center}
</style></head>
<body>
<header>
  <h1>TRID3NT tool catalog</h1>
  <div class="sub">The agent's-eye view: every registered tool with the exact docstring the model routes on. <span id="count"></span></div>
  <div class="controls">
    <input id="q" type="search" placeholder="Search name + docstring..." autocomplete="off">
    <select id="f-engine"><option value="">engine: all</option></select>
    <select id="f-tier"><option value="">tier: all</option></select>
    <select id="f-source"><option value="">source_class: all</option></select>
  </div>
</header>
<main id="list"></main>
<script id="catalog-data" type="application/json">__DATA__</script>
<script>
(function(){
  var data=JSON.parse(document.getElementById("catalog-data").textContent);
  var tools=data.tools||[];
  var list=document.getElementById("list");
  var q=document.getElementById("q");
  var fEngine=document.getElementById("f-engine"),fTier=document.getElementById("f-tier"),fSource=document.getElementById("f-source");
  function opts(sel,vals){vals.forEach(function(v){var o=document.createElement("option");o.value=v;o.textContent=v;sel.appendChild(o);});}
  function uniq(key){var s={};tools.forEach(function(t){if(t[key])s[t[key]]=1;});return Object.keys(s).sort();}
  opts(fEngine,uniq("engine"));opts(fTier,uniq("tier"));opts(fSource,uniq("source_class"));
  function esc(x){return (x==null?"":String(x)).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
  function badges(t){var b=[];if(t.engine)b.push('<span class="badge eng">'+esc(t.engine)+'</span>');
    b.push('<span class="badge">tier: '+esc(t.tier)+'</span>');
    if(t.source_class)b.push('<span class="badge">'+esc(t.source_class)+'</span>');
    if(t.supports_global_query)b.push('<span class="badge">global</span>');
    if(t.annotations&&t.annotations.read_only_hint)b.push('<span class="badge">read-only</span>');
    return b.join("");}
  function render(){
    var term=q.value.trim().toLowerCase();
    var e=fEngine.value,ti=fTier.value,so=fSource.value;
    var html="",n=0;
    tools.forEach(function(t){
      if(e&&t.engine!==e)return;if(ti&&t.tier!==ti)return;if(so&&t.source_class!==so)return;
      if(term&&(t.name+" "+t.docstring).toLowerCase().indexOf(term)<0)return;
      n++;
      var sq=(t.sample_queries&&t.sample_queries.length)?'<div class="sq"><b>e.g.</b> '+t.sample_queries.map(esc).join(" &middot; ")+'</div>':"";
      html+='<div class="tool"><h2>'+esc(t.name)+'</h2><div class="badges">'+badges(t)+'</div><pre class="doc">'+esc(t.docstring)+'</pre>'+sq+'</div>';
    });
    list.innerHTML=n?html:'<div class="empty">No tools match.</div>';
    document.getElementById("count").textContent=n+" of "+tools.length+" shown";
  }
  q.addEventListener("input",render);fEngine.addEventListener("change",render);
  fTier.addEventListener("change",render);fSource.addEventListener("change",render);
  render();
})();
</script>
</body></html>"""


def render_catalog_page(payload: dict[str, Any] | None = None) -> bytes:
    """Render the self-contained HTML catalog page as UTF-8 bytes; the payload
    is embedded as inline JSON with ``</`` escaped, so a docstring containing it
    cannot break out of the script block."""
    data = payload if payload is not None else build_catalog_payload()
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    safe = raw.replace("</", "<\\/")
    return _CATALOG_PAGE_TEMPLATE.replace("__DATA__", safe).encode("utf-8")





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


def _case_list_route_enabled() -> bool:
    """The route is served: this build has ONE fixed local user to resolve to."""
    return True


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
#
# Served whenever the agent runs the local single-user seam.


class _IngestLayerBadRequest(Exception):
    """Malformed /api/ingest-layer(-file) request."""


def _ingest_layer_route_enabled() -> bool:
    """The routes are served (mirrors ``_case_list_route_enabled``)."""
    return True


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
# detected frame sequence, on the case at one point. Served whenever the agent
# runs the local single-user seam.


class _ProbePointBadRequest(Exception):
    """Malformed /api/probe-point request."""


def _probe_point_route_enabled() -> bool:
    """The route is served (mirrors ``_case_list_route_enabled``)."""
    return True


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


def _format_response(
    status: int,
    body: bytes,
    *,
    content_type: str = "application/json; charset=utf-8",
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


async def _handle_http(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Handle one HTTP request. The protocol implementation is deliberately
    minimal: an unknown path is 404, an unknown method 405, and a body is read
    to Content-Length or end-of-stream so a stray POST cannot hang."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    except asyncio.TimeoutError:
        writer.close()
        return
    if not request_line:
        writer.close()
        return
    try:
        method, path, _version = request_line.decode("ascii", "replace").split()
    except ValueError:
        body = _format_response(400, b'{"error":"bad request line"}')
        writer.write(body)
        await writer.drain()
        writer.close()
        return

    # Drain headers; the ones we consume are Content-Length (so a POST body
    # can be read) and Host (so /plugin-repo/plugins.xml can build a download_url
    # that matches the host:port the client actually dialed -- e.g. a
    # tailnet client's daemon-host address, not a hardcoded 127.0.0.1). The
    # socket must be advanced past the rest before we close so the client
    # sees our response cleanly.
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
        writer.write(_format_response(204, b""))
        await writer.drain()
        writer.close()
        return

    proxy_path, _, proxy_qs = path.partition("?")

    
    if method == "POST" and proxy_path == "/api/ingest-layer-file":
        # Bidirectional layer push, half 1: stage the client's raw upload bytes
        # to object storage.
        if not _ingest_layer_route_enabled():
            writer.write(_format_response(404, b'{"error":"not found"}'))
            await writer.drain()
            writer.close()
            return
        from trid3nt_server.inputs.user_layer import MAX_INGEST_BYTES

        if content_length <= 0:
            writer.write(
                _format_response(400, b'{"error":"missing or empty request body"}')
            )
            await writer.drain()
            writer.close()
            return
        if content_length > MAX_INGEST_BYTES:
            # Reject BEFORE reading the oversized body into memory.
            writer.write(
                _format_response(
                    413,
                    json.dumps(
                        {
                            "error": f"upload is {content_length} bytes, exceeds "
                            f"the {MAX_INGEST_BYTES}-byte cap"
                        },
                        separators=(",", ":"),
                    ).encode("utf-8"),
                )
            )
            await writer.drain()
            writer.close()
            return
        from trid3nt_server.inputs.user_layer import ImportLayerError, ObjectTooLargeError

        try:
            filename = _parse_ingest_layer_filename(proxy_qs)
            raw_body = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=120.0
            )
            s3_uri = await asyncio.to_thread(
                _upload_layer_file_fn(), filename, raw_body
            )
            writer.write(
                _format_response(
                    200,
                    json.dumps({"s3_uri": s3_uri}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except _IngestLayerBadRequest as exc:
            writer.write(
                _format_response(
                    400,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            writer.write(
                _format_response(400, b'{"error":"upload body read failed"}')
            )
        except ObjectTooLargeError as exc:
            writer.write(
                _format_response(
                    413,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except ImportLayerError as exc:
            writer.write(
                _format_response(
                    400,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("ingest-layer-file upload failed")
            writer.write(_format_response(500, b'{"error":"layer upload failed"}'))
        await writer.drain()
        writer.close()
        return

    if method == "POST" and proxy_path == "/api/ingest-layer":
        # Bidirectional layer push, half 2: register an already-uploaded
        # object onto the case (see the module section above for the
        # request/response contract).
        if not _ingest_layer_route_enabled():
            writer.write(_format_response(404, b'{"error":"not found"}'))
            await writer.drain()
            writer.close()
            return
        raw_body = b""
        if content_length > 0:
            try:
                raw_body = await asyncio.wait_for(
                    reader.readexactly(content_length), timeout=30.0
                )
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                raw_body = b""
        from trid3nt_server.inputs.user_layer import CaseNotFoundError, ImportLayerError, ObjectNotFoundError

        try:
            body = await _handle_ingest_layer_post(raw_body)
            writer.write(_format_response(200, body))
        except _IngestLayerBadRequest as exc:
            writer.write(
                _format_response(
                    400,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except (CaseNotFoundError, ObjectNotFoundError) as exc:
            writer.write(
                _format_response(
                    404,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except ImportLayerError as exc:
            # INVALID_INPUT / OBJECT_TOO_LARGE / UNREADABLE_LAYER / other typed
            # core errors -- the request was well-formed HTTP but ingestion
            # cannot succeed.
            writer.write(
                _format_response(
                    400,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("ingest-layer run failed")
            writer.write(_format_response(500, b'{"error":"layer ingest failed"}'))
        await writer.drain()
        writer.close()
        return

    if method == "POST" and proxy_path == "/api/probe-point":
        # Deterministic map-click point probe.
        if not _probe_point_route_enabled():
            writer.write(_format_response(404, b'{"error":"not found"}'))
            await writer.drain()
            writer.close()
            return
        raw_body = b""
        if content_length > 0:
            try:
                raw_body = await asyncio.wait_for(
                    reader.readexactly(content_length), timeout=30.0
                )
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                raw_body = b""
        from trid3nt_server.tools.derive.probe_point.probe_point import (
            ProbePointCaseNotFoundError,
            ProbePointInputError,
        )
        from trid3nt_server.inputs.user_input import UserInputError

        try:
            body = await _handle_probe_point_post(raw_body)
            writer.write(_format_response(200, body))
        except _ProbePointBadRequest as exc:
            writer.write(
                _format_response(
                    400,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except ProbePointCaseNotFoundError as exc:
            writer.write(
                _format_response(
                    404,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except (ProbePointInputError, UserInputError) as exc:
            writer.write(
                _format_response(
                    400,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("probe-point run failed")
            writer.write(_format_response(500, b'{"error":"probe point failed"}'))
        await writer.drain()
        writer.close()
        return

    if method == "POST" and proxy_path == "/api/provider-config":
        # The live provider config: a provider, model or key switch takes effect
        # on the NEXT turn with no restart, because the adapter reads the env per
        # call. SECURITY: the api_key rides the body and is written to the env,
        # never logged or echoed - only the base URL host and effective model
        # return. Runs in a thread, because the coherence gate may make a short
        # blocking probe that must never sit on the event loop.
        if not model_discovery._local_models_route_enabled():
            writer.write(_format_response(404, b'{"error":"not found"}'))
            await writer.drain()
            writer.close()
            return
        raw_body = b""
        if content_length > 0:
            try:
                raw_body = await asyncio.wait_for(
                    reader.readexactly(content_length), timeout=30.0
                )
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                raw_body = b""
        try:
            body = await asyncio.to_thread(
                model_discovery.apply_provider_config, raw_body)
            writer.write(_format_response(200, body))
        except model_discovery.ProviderConfigBadRequest as exc:
            writer.write(
                _format_response(
                    400,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except Exception:  # noqa: BLE001 -- NEVER surface the body/key in logs
            # A generic static message + no request context: the traceback
            # references the raw body variable by name only, never its value.
            logger.exception("provider-config update failed")
            writer.write(
                _format_response(500, b'{"error":"provider config update failed"}')
            )
        await writer.drain()
        writer.close()
        return

    if method != "GET":
        writer.write(
            _format_response(405, b'{"error":"method not allowed"}')
        )
        await writer.drain()
        writer.close()
        return

    if path == "/api/tool-catalog":
        try:
            payload = build_catalog_payload()
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            writer.write(_format_response(200, body))
        except Exception:  # noqa: BLE001
            logger.exception("tool-catalog payload build failed")
            writer.write(
                _format_response(500, b'{"error":"catalog build failed"}')
            )
    elif proxy_path == "/catalog":
        # Self-contained HTML catalog page (the agent's-eye view). Inline
        # CSS + JS + embedded data -- no external assets.
        try:
            body = render_catalog_page()
            writer.write(
                _format_response(
                    200, body, content_type="text/html; charset=utf-8"
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("catalog page render failed")
            writer.write(
                _format_response(
                    500,
                    b"<!doctype html><p>catalog render failed</p>",
                    content_type="text/html; charset=utf-8",
                )
            )
    elif path == "/api/telemetry/summary":
        try:
            from trid3nt_server.telemetry import build_telemetry_summary

            summary = await build_telemetry_summary()
            body = json.dumps(summary, separators=(",", ":")).encode("utf-8")
            writer.write(_format_response(200, body))
        except Exception:  # noqa: BLE001
            logger.exception("telemetry summary build failed")
            writer.write(
                _format_response(500, b'{"error":"telemetry summary failed"}')
            )
    elif proxy_path == "/api/case-list":
        # The cold case list, for a client with no WebSocket session yet.
        if not _case_list_route_enabled():
            writer.write(_format_response(404, b'{"error":"not found"}'))
        else:
            try:
                payload = await build_case_list_payload()
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                writer.write(_format_response(200, body))
            except _CaseListPersistenceUnavailable as exc:
                writer.write(
                    _format_response(
                        503,
                        json.dumps(
                            {"error": str(exc)}, separators=(",", ":")
                        ).encode("utf-8"),
                    )
                )
            except Exception:  # noqa: BLE001
                logger.exception("case-list build failed")
                writer.write(
                    _format_response(500, b'{"error":"case list failed"}')
                )
    elif proxy_path == "/api/local-models":
        # The installed local models, for a client's model picker. The route is
        # absent, like any unknown path, unless the local provider is active,
        # and the upstream fetch runs off the event loop.
        if not model_discovery._local_models_route_enabled():
            writer.write(_format_response(404, b'{"error":"not found"}'))
        else:
            try:
                body = await asyncio.to_thread(model_discovery._fetch_local_models)
                writer.write(_format_response(200, body))
            except model_discovery._LocalModelsUpstreamError as exc:
                writer.write(
                    _format_response(
                        502,
                        json.dumps(
                            {"error": str(exc)}, separators=(",", ":")
                        ).encode("utf-8"),
                    )
                )
            except Exception:  # noqa: BLE001
                logger.exception("local-models listing failed")
                writer.write(
                    _format_response(500, b'{"error":"local models failed"}')
                )
    elif proxy_path == "/api/building-detail":
        # Click-to-enrich: the building footprint inline
        # GeoJSON is now SLIM (id-only props). The popup fetches the full tag
        # bag on demand by (osm_type, osm_id) here. Cold/box-off friendly + off
        # the event loop (S3 + Overpass run via asyncio.to_thread).
        try:
            body = await _handle_building_detail(proxy_qs)
            writer.write(_format_response(200, body))
        except _BuildingDetailNotFound as exc:
            writer.write(
                _format_response(
                    404,
                    json.dumps(
                        {"error": "building detail not found", "detail": str(exc)},
                        separators=(",", ":"),
                    ).encode("utf-8"),
                )
            )
        except _BuildingDetailBadRequest as exc:
            writer.write(
                _format_response(
                    400,
                    json.dumps(
                        {"error": "bad request", "detail": str(exc)},
                        separators=(",", ":"),
                    ).encode("utf-8"),
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("building-detail lookup failed")
            writer.write(
                _format_response(500, b'{"error":"building detail failed"}')
            )
    elif path == "/api/version":
        # Daemon git sha + active model provider -- the version indicator the
        # removed plugin-settings Update section wanted. Cheap: one
        # `git rev-parse` subprocess, off the
        # event loop.
        try:
            from trid3nt_server import plugin_repo

            payload = await asyncio.to_thread(plugin_repo.build_version_payload)
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            writer.write(_format_response(200, body))
        except Exception:  # noqa: BLE001
            logger.exception("version payload build failed")
            writer.write(_format_response(500, b'{"error":"version lookup failed"}'))
    elif proxy_path == "/plugin-repo/plugins.xml":
        # QGIS custom plugin repository index. The
        # packaged plugins.xml carries a HOST_SENTINEL; the download_url host
        # is filled from the REQUEST's own Host header so a tailnet client's
        # "Add repository" URL (http://<daemon-host>:8766/plugin-repo/plugins.xml)
        # round-trips to a reachable zip URL without a hardcoded host.
        from trid3nt_server import plugin_repo

        try:
            host = host_header or (
                f"127.0.0.1:{os.environ.get('TRID3NT_AGENT_HTTP_PORT', DEFAULT_HTTP_PORT)}"
            )
            body = await asyncio.to_thread(plugin_repo.render_plugins_xml, host)
            writer.write(
                _format_response(200, body, content_type="text/xml; charset=utf-8")
            )
        except plugin_repo.PluginRepoBuildError as exc:
            writer.write(
                _format_response(
                    503,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("plugins.xml serve failed")
            writer.write(
                _format_response(500, b'{"error":"plugin repo index failed"}')
            )
    elif proxy_path == "/plugin-repo/trid3nt.zip":
        # THE zip Plugin Manager / Install-from-ZIP downloads -- every
        # plugins.xml download_url now points here. Fixed name (must match
        # plugin_repo.FRESH_ZIP_URL_PATH), built on demand straight from
        # plugin/ and mtime-cached. No deploy-time
        # package_plugin_repo() step required. ?v=<version> (already
        # stripped into proxy_qs above) is a pure cache-busting hint.
        from trid3nt_server import plugin_repo

        try:
            data, _version, zip_filename = await asyncio.to_thread(
                plugin_repo.build_fresh_zip
            )
            writer.write(
                _format_response(
                    200,
                    data,
                    content_type="application/zip",
                    extra_headers={
                        "Content-Disposition": f'attachment; filename="{zip_filename}"'
                    },
                )
            )
        except plugin_repo.PluginRepoBuildError as exc:
            writer.write(
                _format_response(
                    503,
                    json.dumps({"error": str(exc)}, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("plugin zip (fresh) serve failed")
            writer.write(_format_response(500, b'{"error":"plugin zip failed"}'))
    elif proxy_path.startswith("/plugin-repo/") and proxy_path.endswith(".zip"):
        # The versioned zip built by package_plugin_repo() -- kept as a
        # manual-QA / fallback path; served straight from the packaged
        # directory (deploy-time artifact). Not what plugins.xml advertises
        # anymore (see the /plugin-repo/trid3nt.zip branch above).
        from trid3nt_server import plugin_repo

        filename = proxy_path[len("/plugin-repo/") :]
        try:
            zip_path = await asyncio.to_thread(plugin_repo.served_zip_path, filename)
            data = await asyncio.to_thread(zip_path.read_bytes)
            writer.write(
                _format_response(
                    200,
                    data,
                    content_type="application/zip",
                    extra_headers={
                        "Content-Disposition": f'attachment; filename="{zip_path.name}"'
                    },
                )
            )
        except FileNotFoundError:
            writer.write(_format_response(404, b'{"error":"not found"}'))
        except Exception:  # noqa: BLE001
            logger.exception("plugin zip serve failed")
            writer.write(_format_response(500, b'{"error":"plugin zip failed"}'))
    else:
        writer.write(_format_response(404, b'{"error":"not found"}'))
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
