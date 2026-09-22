"""The HTTP doors: the backend the plugin's panels and dialogs read.

One aiohttp app beside the WebSocket server, its routes registered by the
module that owns their subject - the library, the case list, the layer round
trips, the settings surfaces, the plugin repository and the telemetry summary.
Every route is unauthenticated with open CORS."""

from trid3nt_server.server.protocol.doors.app import build_app, serve_doors
from trid3nt_server.server.protocol.doors.transport import DEFAULT_HTTP_PORT

__all__ = ["build_app", "serve_doors", "DEFAULT_HTTP_PORT"]
