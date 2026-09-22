"""The one app the doors are served on, beside the WebSocket server.

Each door registers its own routes here and owns nothing of the transport; the
app is served on the same loop and process as the WebSocket server, on the port
the environment names."""

from __future__ import annotations

import logging

from aiohttp import web

from trid3nt_server.server.protocol.doors import (
    cases, layers, library, plugin_repo, settings, telemetry, transport,
)

logger = logging.getLogger("trid3nt_server.server.protocol.doors")

#: The doors, in the order their routes are registered. The plugin repo goes
#: last because its fallback route matches any name under its own prefix.
_DOORS = (library, cases, layers, settings, telemetry, plugin_repo)


def build_app() -> web.Application:
    """The aiohttp app every door has registered on, capped at the largest body
    any route accepts so an oversized upload is refused by the route that names
    the cap rather than by the transport."""
    app = web.Application(
        middlewares=[transport.errors],
        client_max_size=layers._max_ingest_bytes(),
    )
    for door in _DOORS:
        door.add_routes(app)
    app.router.add_route("OPTIONS", "/{path:.*}", transport.preflight)
    return app


async def serve_doors(host: str = "127.0.0.1",
                      port: int | None = None) -> web.AppRunner:
    """Start the door listener and return its runner; the port comes from
    ``TRID3NT_AGENT_HTTP_PORT`` when not passed."""
    if port is None:
        port = transport.http_port()
    runner = web.AppRunner(build_app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    logger.info("door HTTP server listening host=%s port=%d", host, port)
    return runner
