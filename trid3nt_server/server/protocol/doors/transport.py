"""The HTTP transport every door registers on: aiohttp, one app, one port.

A door module names its routes and answers them; this module owns everything
below that - the error a route raises, the JSON body every route answers in,
the failure message an unhandled fault falls back to, the CORS headers, and the
listener itself. Nothing here knows what any route is about."""

from __future__ import annotations

import functools
import json
import logging
import os
from typing import Any

from aiohttp import web

logger = logging.getLogger("trid3nt_server.server.protocol.doors")

DEFAULT_HTTP_PORT = 8766

#: Every response carries these: the doors are unauthenticated with open CORS,
#: and nothing they answer is cacheable.
_COMMON_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Cache-Control": "no-cache",
}


class HttpError(Exception):
    """The status and message one route answers a rejected request with."""

    def __init__(self, status: int, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


def json_reply(payload: Any) -> web.Response:
    """A JSON body with no whitespace, the one shape every data route answers in."""
    return raw_reply(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def raw_reply(body: bytes) -> web.Response:
    """A JSON body a route already serialized itself."""
    return web.Response(body=body, content_type="application/json",
                        charset="utf-8")


def _error_body(message: str, detail: str = "") -> bytes:
    """The one error body: a message, and a detail only where the route has one."""
    payload: dict[str, Any] = {"error": message}
    if detail:
        payload["detail"] = detail
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def failure(message: str):
    """Name what an unhandled fault in this route answers with, so no route
    hand-writes a 500 and no traceback reaches the client."""
    def wrap(handler):
        @functools.wraps(handler)
        async def run(request: web.Request) -> web.StreamResponse:
            try:
                return await handler(request)
            except (HttpError, web.HTTPException):
                raise
            except Exception:  # noqa: BLE001 -- one honest 500 per route
                logger.exception("%s", message)
                raise HttpError(500, message) from None
        return run
    return wrap


@web.middleware
async def errors(request: web.Request, handler) -> web.StreamResponse:
    """Turn every refusal into the door's one JSON error body and put the CORS
    headers on it. A path nobody serves is 404 under GET and 405 under any
    other method, which is what a client sending the wrong verb has done."""
    try:
        response = await handler(request)
    except HttpError as exc:
        response = web.Response(
            status=exc.status, body=_error_body(exc.message, exc.detail),
            content_type="application/json", charset="utf-8")
    except (web.HTTPNotFound, web.HTTPMethodNotAllowed):
        # A GET nobody serves is an unknown path; anything else is the method.
        if request.method == "GET":
            response = web.Response(
                status=404, body=_error_body("not found"),
                content_type="application/json", charset="utf-8")
        else:
            response = web.Response(
                status=405, body=_error_body("method not allowed"),
                content_type="application/json", charset="utf-8")
    response.headers.update(_COMMON_HEADERS)
    return response


async def preflight(_request: web.Request) -> web.Response:
    """The CORS preflight every path answers with, before any route is named."""
    return web.Response(status=204)


def http_port() -> int:
    """The port the doors listen on: ``TRID3NT_AGENT_HTTP_PORT`` or the default."""
    try:
        return int(os.environ.get("TRID3NT_AGENT_HTTP_PORT", DEFAULT_HTTP_PORT))
    except ValueError:
        return DEFAULT_HTTP_PORT
