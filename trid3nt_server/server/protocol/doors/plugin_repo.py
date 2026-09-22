"""The PLUGIN REPO door: the QGIS custom repository a client adds, and the
version the daemon answers with.

Three routes over the builder that owns the zip: the repository index, THE zip
its download_url points at, and the versioned zip kept as the manual-QA
fallback. The index's download_url host is the REQUEST's own Host, so a tailnet
client's "Add repository" URL round-trips to a reachable zip URL."""

from __future__ import annotations

import asyncio

from aiohttp import web

from trid3nt_server import plugin_repo
from trid3nt_server.server.protocol.doors.transport import (
    HttpError, failure, http_port, json_reply,
)


@failure("version lookup failed")
async def _version(_request: web.Request) -> web.Response:
    """``GET /api/version``: the daemon's git sha and active model provider."""
    return json_reply(await asyncio.to_thread(plugin_repo.build_version_payload))


@failure("plugin repo index failed")
async def _plugins_xml(request: web.Request) -> web.Response:
    """``GET /plugin-repo/plugins.xml``: the QGIS custom repository index."""
    # The Host HEADER, not ``request.host``: aiohttp answers that with the
    # machine's own name when the client sent none, and a download_url no
    # client can dial is worse than the loopback default.
    host = request.headers.get("Host", "") or f"127.0.0.1:{http_port()}"
    try:
        body = await asyncio.to_thread(plugin_repo.render_plugins_xml, host)
    except plugin_repo.PluginRepoBuildError as exc:
        raise HttpError(503, str(exc)) from exc
    return web.Response(body=body, content_type="text/xml", charset="utf-8")


@failure("plugin zip failed")
async def _fresh_zip(_request: web.Request) -> web.Response:
    """``GET /plugin-repo/trid3nt.zip``: THE zip every plugins.xml download_url
    points at, built on demand straight from ``plugin/`` and mtime-cached."""
    try:
        data, _version, zip_filename = await asyncio.to_thread(
            plugin_repo.build_fresh_zip)
    except plugin_repo.PluginRepoBuildError as exc:
        raise HttpError(503, str(exc)) from exc
    return _zip_reply(data, zip_filename)


@failure("plugin zip failed")
async def _packaged_zip(request: web.Request) -> web.Response:
    """``GET /plugin-repo/<name>.zip``: the versioned zip ``package_plugin_repo``
    built, kept as the manual-QA fallback path plugins.xml no longer advertises."""
    filename = request.match_info["filename"]
    if not filename.endswith(".zip"):
        raise HttpError(404, "not found")
    try:
        zip_path = await asyncio.to_thread(plugin_repo.served_zip_path, filename)
        data = await asyncio.to_thread(zip_path.read_bytes)
    except FileNotFoundError as exc:
        raise HttpError(404, "not found") from exc
    return _zip_reply(data, zip_path.name)


def _zip_reply(data: bytes, filename: str) -> web.Response:
    """A zip the client saves under the name the builder gave it."""
    return web.Response(
        body=data, content_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def add_routes(app: web.Application) -> None:
    """Register the plugin repo's routes; the two exact paths are registered
    before the fallback, which would otherwise match them."""
    app.router.add_get("/api/version", _version, allow_head=False)
    app.router.add_get("/plugin-repo/plugins.xml", _plugins_xml, allow_head=False)
    app.router.add_get("/plugin-repo/trid3nt.zip", _fresh_zip, allow_head=False)
    app.router.add_get("/plugin-repo/{filename}", _packaged_zip, allow_head=False)
