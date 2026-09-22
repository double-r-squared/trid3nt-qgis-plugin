"""The TELEMETRY door: the routing-quality summary, over the analytics that
aggregate it.

One route and no analysis of its own - the telemetry module owns the sink it
reads and the summary it builds."""

from __future__ import annotations

from aiohttp import web

from trid3nt_server.server.protocol.doors.transport import failure, json_reply


@failure("telemetry summary failed")
async def _summary(_request: web.Request) -> web.Response:
    """``GET /api/telemetry/summary``: the routing-quality summary the telemetry
    module aggregates over its own sink."""
    from trid3nt_server.telemetry import build_telemetry_summary

    return json_reply(await build_telemetry_summary())


def add_routes(app: web.Application) -> None:
    """Register the telemetry door's route on the door's one app."""
    app.router.add_get("/api/telemetry/summary", _summary, allow_head=False)
