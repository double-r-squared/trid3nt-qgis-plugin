"""Stub TRID3NT agent WS server for connection-layer tests.

Runs a real ``websockets`` (>=13, asyncio) server on a background thread with
its own event loop, speaking just enough of the envelope protocol
(auth-token/auth-ack, session-resume/session-state, case-command/case-open,
user-message -> chunk + pipeline-state + session-state-with-layers +
turn-complete) to exercise ``trid3nt_client.AgentClient`` end to end.

Requires the ``websockets`` package (present in the trid3nt-local agent venv).
The plugin itself never imports this -- test-only.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Optional

import websockets

STUB_USER_ID = "01STUBUSERAAAAAAAAAAAAAAAA"
STUB_CASE_ID = "01STUBCASEAAAAAAAAAAAAAAAA"

# A raster row exactly as the local agent publishes it: the display ``uri`` is
# a ready TiTiler XYZ template (contains {z}/{x}/{y}) with style params.
RASTER_LAYER_ROW: dict[str, Any] = {
    "layer_id": "01STUBRASTERAAAAAAAAAAAAAA",
    "name": "DEM Asheville",
    "layer_type": "raster",
    "uri": (
        "http://127.0.0.1:8080/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
        "?url=s3%3A%2F%2Ftrid3nt-runs%2Fdem%2Fasheville.tif"
        "&rescale=600,2100&colormap_name=terrain"
    ),
    "style_preset": "dem_hillshade",
    "visible": True,
    "role": "primary",
    "temporal": False,
    "opacity": 1.0,
}

# A vector row with the additive inline_geojson merge (job-0175 shape).
VECTOR_LAYER_ROW: dict[str, Any] = {
    "layer_id": "01STUBVECTORAAAAAAAAAAAAAA",
    "name": "Buildings",
    "layer_type": "vector",
    "uri": "s3://trid3nt-runs/vectors/buildings.fgb",
    "style_preset": "buildings",
    "visible": True,
    "role": "context",
    "temporal": False,
    "inline_geojson": {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[-82.55, 35.59], [-82.55, 35.60], [-82.54, 35.60], [-82.54, 35.59], [-82.55, 35.59]]
                    ],
                },
                "properties": {"height_m": 12.5, "pad": "x" * 70000},
            }
        ],
    },
}

# An s3-only vector row (no inline geojson) -- exercises the MinIO translation
# path client-side.
S3_VECTOR_LAYER_ROW: dict[str, Any] = {
    "layer_id": "01STUBS3VECTORAAAAAAAAAAAA",
    "name": "Rivers",
    "layer_type": "vector",
    "uri": "s3://trid3nt-runs/vectors/rivers.geojson",
    "style_preset": "rivers",
    "visible": True,
    "role": "context",
    "temporal": False,
}


class StubAgentServer:
    """Threaded stub agent. ``start()`` binds an ephemeral port; ``stop()``
    tears the loop down. Records every received envelope + upgrade path."""

    def __init__(self) -> None:
        self.port: Optional[int] = None
        self.received: list[dict] = []
        self.paths: list[str] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stop_event: Optional[asyncio.Event] = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    # -- lifecycle ----------------------------------------------------------- #

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("stub server failed to start")

    def stop(self) -> None:
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        async with websockets.serve(self._handler, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop_event.wait()

    # -- protocol ------------------------------------------------------------ #

    async def _handler(self, ws) -> None:
        try:
            self.paths.append(ws.request.path)
        except AttributeError:  # older websockets API fallback
            self.paths.append(getattr(ws, "path", ""))

        async def send(type_: str, payload: dict, case_id: Optional[str] = None) -> None:
            await ws.send(
                json.dumps(
                    {
                        "type": type_,
                        "id": "01STUBENVELOPEAAAAAAAAAAAA",
                        "ts": "2026-07-07T00:00:00Z",
                        "session_id": self._session_id,
                        "case_id": case_id,
                        "payload": payload,
                    }
                )
            )

        self._session_id = "01STUBSESSIONAAAAAAAAAAAAA"
        async for raw in ws:
            try:
                env = json.loads(raw)
            except json.JSONDecodeError:
                continue
            self.received.append(env)
            etype = env.get("type")
            self._session_id = env.get("session_id") or self._session_id

            if etype == "auth-token":
                token = (env.get("payload") or {}).get("token") or ""
                await send(
                    "auth-ack",
                    {"user_id": STUB_USER_ID, "is_anonymous": token == ""},
                )
            elif etype == "session-resume":
                # Real server interleaves housekeeping before session-state;
                # emit a case-list first so the client's drain is exercised.
                await send("case-list", {"cases": []})
                await send(
                    "session-state",
                    {"chat_history": [], "loaded_layers": [], "pipeline_history": []},
                )
            elif etype == "case-command":
                payload = env.get("payload") or {}
                if payload.get("command") == "create":
                    await send(
                        "case-open",
                        {
                            "session_state": {
                                "case": {
                                    "case_id": STUB_CASE_ID,
                                    "title": (payload.get("args") or {}).get("title"),
                                },
                                "loaded_layers": [],
                            }
                        },
                    )
            elif etype == "user-message":
                case_id = env.get("case_id")
                await send(
                    "pipeline-state",
                    {
                        "pipeline_id": "01STUBPIPELINEAAAAAAAAAAAA",
                        "steps": [
                            {
                                "step_id": "s1",
                                "name": "fetch_elevation",
                                "tool_name": "fetch_elevation",
                                "state": "running",
                            }
                        ],
                    },
                    case_id=case_id,
                )
                await send(
                    "agent-message-chunk",
                    {"message_id": "m1", "delta": "Here is the DEM ", "done": False},
                    case_id=case_id,
                )
                await send(
                    "agent-message-chunk",
                    {"message_id": "m1", "delta": "you asked for.", "done": True},
                    case_id=case_id,
                )
                await send(
                    "pipeline-state",
                    {
                        "pipeline_id": "01STUBPIPELINEAAAAAAAAAAAA",
                        "steps": [
                            {
                                "step_id": "s1",
                                "name": "fetch_elevation",
                                "tool_name": "fetch_elevation",
                                "state": "complete",
                            }
                        ],
                    },
                    case_id=case_id,
                )
                # The >64 KiB inline_geojson pad forces the 64-bit-length
                # frame path in the client's frame decoder.
                await send(
                    "session-state",
                    {
                        "chat_history": [],
                        "loaded_layers": [
                            RASTER_LAYER_ROW,
                            VECTOR_LAYER_ROW,
                            S3_VECTOR_LAYER_ROW,
                        ],
                        "pipeline_history": [],
                    },
                    case_id=case_id,
                )
                await send("turn-complete", {}, case_id=case_id)
            elif etype == "cancel":
                await send("turn-complete", {"cancelled": True}, case_id=env.get("case_id"))
