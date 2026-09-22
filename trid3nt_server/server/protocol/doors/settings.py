"""The SETTINGS door: the models a client can pick, the provider switch, and
the credentials the source rows declare.

The model routes answer only while the local provider is active; the catalog
route is the keys form's one reader, so it carries the credential a source row
declares and nothing else - a name, a label, a signup url and the env var the
daemon falls back to, never key material."""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from trid3nt_server.adapters import model_discovery
from trid3nt_server.server.protocol.doors.transport import (
    HttpError, failure, json_reply, raw_reply,
)


def build_credential_catalog() -> dict[str, Any]:
    """The ``/api/tool-catalog`` payload: every tool whose source row declares a
    credential, with that credential's own facts verbatim off the row, so the
    keys form has one row per credential and no table of its own."""
    from trid3nt_server.credentials.resolver import credential_for_tool
    from trid3nt_server.tools import TOOL_REGISTRY

    tools: list[dict[str, Any]] = []
    for name in sorted(TOOL_REGISTRY.keys()):
        credential = credential_for_tool(name)
        if credential is None:
            continue
        tools.append({
            "name": name,
            "credential": {
                "name": credential.name,
                "label": credential.label,
                "signup_url": credential.signup_url,
                "env_var": credential.env_var,
            },
        })
    return {"tools": tools}


@failure("catalog build failed")
async def _tool_catalog(_request: web.Request) -> web.Response:
    """``GET /api/tool-catalog``: the credential every keyed source declares."""
    return json_reply(build_credential_catalog())


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
    app.router.add_get("/api/tool-catalog", _tool_catalog, allow_head=False)
    app.router.add_get("/api/local-models", _local_models, allow_head=False)
    app.router.add_post("/api/provider-config", _provider_config)
