"""The SETTINGS door: the models a client can pick and the provider switch.

Both routes answer only while the local provider is active. The credentials the
keys form offers ride the library listing, which already lists what is here."""

from __future__ import annotations

import asyncio

from aiohttp import web

from trid3nt_server.model.adapters import model_discovery
from trid3nt_server.server.protocol.http.transport import (
    HttpError, failure, json_reply, raw_reply,
)


@failure("local models failed")
async def _local_models(_request: web.Request) -> web.Response:
    """``GET /api/local-models``: the installed local models, for a client's
    model picker. Absent, like any unknown path, unless the local provider is
    active; the upstream fetch runs off the event loop."""
    if not model_discovery._local_models_route_enabled():
        raise HttpError(404, "not found")
    try:
        body = await asyncio.to_thread(model_discovery._fetch_local_models)
    except model_discovery._LocalModelsUpstreamError as exc:
        raise HttpError(502, str(exc)) from exc
    return raw_reply(body)


@failure("provider config update failed")
async def _provider_config(request: web.Request) -> web.Response:
    """``POST /api/provider-config``: a provider, model or key switch that takes
    effect on the NEXT turn with no restart, because the adapter reads the env
    per call. The api_key rides the body into the env and is never logged or
    echoed; the coherence gate's short blocking probe runs off the event loop."""
    if not model_discovery._local_models_route_enabled():
        raise HttpError(404, "not found")
    raw = await request.read()
    try:
        body = await asyncio.to_thread(model_discovery.apply_provider_config, raw)
    except model_discovery.ProviderConfigBadRequest as exc:
        raise HttpError(400, str(exc)) from exc
    return raw_reply(body)


def add_routes(app: web.Application) -> None:
    """Register the settings door's routes on the door's one app."""
    app.router.add_get("/api/local-models", _local_models, allow_head=False)
    app.router.add_post("/api/provider-config", _provider_config)
