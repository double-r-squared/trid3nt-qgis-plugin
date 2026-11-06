"""The SETTINGS door: the models a client can pick, the provider switch and the changeable settings.

The model routes answer only while the local provider is active. The credentials the
keys form offers ride the library listing, which already lists what is here."""

from __future__ import annotations

import asyncio

from aiohttp import web

from trid3nt_server.model.adapters import model_discovery
from trid3nt_server.settings import listing
from trid3nt_server.server.protocol.http.transport import (
    HttpError, failure, json_reply, raw_reply,
)


@failure("local models failed")
async def _local_models(_request: web.Request) -> web.Response:
    """``GET /api/local-models``: the installed local models, for a client's model picker. Absent unless the local provider is active; the upstream fetch runs off the event loop."""
    if not model_discovery._local_models_route_enabled():
        raise HttpError(404, "not found")
    try:
        body = await asyncio.to_thread(model_discovery._fetch_local_models)
    except model_discovery._LocalModelsUpstreamError as exc:
        raise HttpError(502, str(exc)) from exc
    return raw_reply(body)


@failure("provider config update failed")
async def _provider_config(request: web.Request) -> web.Response:
    """``POST /api/provider-config``: a provider, model or key switch that takes effect on the NEXT
    turn with no restart, because the adapter reads the env per call. The api_key is never logged or echoed; the coherence probe runs off the event loop."""
    if not model_discovery._local_models_route_enabled():
        raise HttpError(404, "not found")
    raw = await request.read()
    try:
        body = await asyncio.to_thread(model_discovery.apply_provider_config, raw)
    except model_discovery.ProviderConfigBadRequest as exc:
        raise HttpError(400, str(exc)) from exc
    return raw_reply(body)


@failure("settings listing failed")
async def _settings(_request: web.Request) -> web.Response:
    """``GET /api/settings``: every changeable setting with the value the daemon holds, its level (default, user), default, range, unit and source, and ``retrieval_backend``, the dense backend the tool-retrieval index was built with (null while the index is cold or built lexical-only). The file is read on a worker thread."""
    from trid3nt_server.tools.search.search_tools import search_tools

    index = search_tools._INDEX
    backend = getattr(index, "backend_name", None) if index is not None else None
    return json_reply({"settings": await asyncio.to_thread(listing), "retrieval_backend": backend})


def add_routes(app: web.Application) -> None:
    """Register the settings door's routes on the door's one app."""
    app.router.add_get("/api/local-models", _local_models, allow_head=False)
    app.router.add_get("/api/settings", _settings, allow_head=False)
    app.router.add_post("/api/provider-config", _provider_config)
