"""Stub TRID3NT agent WS server for connection-layer tests.

Runs a real ``websockets`` (>=13, asyncio) server on a background thread with
its own event loop, speaking just enough of the envelope protocol
(auth-token/auth-ack, session-resume/session-state, case-command/case-open,
user-message -> chunk + pipeline-state + session-state-with-layers +
turn-complete) to exercise ``trid3nt_client.AgentClient`` end to end.

Milestone 2 additions:

* a user-message whose text contains ``"simulate"`` pauses behind a
  ``tool-payload-warning`` (granularity + time_scale enrichments, job-0127 /
  #154 shapes) and only proceeds when the matching
  ``tool-payload-confirmation`` arrives (decision recorded; cancel ->
  cancelled turn-complete). ``"simulate-hardcap"`` emits the hard-cap variant
  (no "proceed" in options).
* ``session-resume`` now answers with a populated ``case-list`` (CaseSummary
  rows) and echoes the resumed ``case_id`` back on the session-state.
* a user-message whose text contains ``"drop-connection"`` closes the socket
  server-side WITHOUT a turn-complete (reconnect tests); the server keeps
  accepting new connections.

Milestone 3 additions:

* ``case-command select`` answers with the server's full ``case-open``
  rehydration (CaseSummary + loaded_layers) for a known ``CASE_LIST_ROWS``
  id, or ``session_state: None`` for an unknown id (the real server's
  could-not-rehydrate shape). Selected ids are recorded on ``selects``.
* an ``auth-token`` whose token is ``EXPIRED_TOKEN`` is REJECTED the way the
  live agent rejects a dead token: an ``error`` envelope with
  ``error_code=AUTH_REQUIRED`` then a 1008 (policy violation) close.

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

#: An auth-token carrying this value is rejected AUTH_REQUIRED + close 1008.
EXPIRED_TOKEN = "stub-expired-token"

# A raster row exactly as the local agent publishes it POST TiTiler->QGIS
# swap: ``uri`` is the raw s3 COG and the explicit ``legend`` (LegendKey:
# colormap + vmin/vmax) drives the plugin's own QGIS-native renderer.
RASTER_LAYER_ROW: dict[str, Any] = {
    "layer_id": "01STUBRASTERAAAAAAAAAAAAAA",
    "name": "DEM Asheville",
    "layer_type": "raster",
    "uri": "s3://trid3nt-runs/dem/asheville.tif",
    "style_preset": "dem_hillshade",
    "visible": True,
    "role": "primary",
    "temporal": False,
    "opacity": 1.0,
    "legend": {
        "kind": "continuous",
        "colormap": "viridis",
        "vmin": 600.0,
        "vmax": 2100.0,
        "units": "m",
        "label": "Elevation",
    },
}

# A LEGACY raster row (old persisted cases, pre-swap): the ``uri`` is still a
# TiTiler XYZ tile TEMPLATE whose percent-encoded ``url=`` param carries the
# s3 COG and whose query string carries the styling. The plugin MUST keep
# rendering these forever (unwrap -> same gdal path) -- back-compat coverage.
LEGACY_RASTER_LAYER_ROW: dict[str, Any] = {
    "layer_id": "01STUBLEGACYRASTERAAAAAAAA",
    "name": "Flood depth (legacy)",
    "layer_type": "raster",
    "uri": (
        "http://127.0.0.1:8080/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
        "?url=s3%3A%2F%2Ftrid3nt-runs%2Fflood%2Fdepth.tif"
        "&rescale=0,3&colormap_name=ylgnbu"
    ),
    "style_preset": "continuous_flood_depth",
    "visible": True,
    "role": "primary",
    "temporal": False,
    "opacity": 0.8,
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

STUB_WARNING_ID = "01STUBWARNINGAAAAAAAAAAAAA"

# A tool-payload-warning payload with the #154 granularity + time_scale
# enrichments -- field-for-field the PayloadWarningEnvelopePayload contract
# (contracts .../payload_warning.py).
PAYLOAD_WARNING_ROW: dict[str, Any] = {
    "envelope_type": "tool-payload-warning",
    "warning_id": STUB_WARNING_ID,
    "tool_name": "run_sfincs_simulation",
    "tool_args": {"bbox": [-82.6, 35.55, -82.5, 35.65], "grid_resolution_m": 30.0},
    "estimated_mb": 48.0,
    "threshold_mb": 25.0,
    "recommendation": "Consider a coarser grid for this AOI.",
    "alternative_args": {"grid_resolution_m": 60.0},
    "options": ["proceed", "cancel", "narrow_scope"],
    "ttl_seconds": 300,
    "granularity": {
        "engine": "sfincs",
        "resolution_param": "grid_resolution_m",
        "suggested_resolution_m": 30.0,
        "resolution_choices": [10.0, 30.0, 60.0, 120.0],
        "estimated_active_cells": 46000,
        "estimated_solve_seconds": 70.0,
        "vcpus": 8,
        "compute_class": "local",
        "cell_cap": 2000000,
        "coarsened": False,
        "reason": "30 m keeps the AOI under the cell cap.",
        "spot_label": None,
    },
    "time_scale": {
        "cadence_param": "output_interval_min",
        "suggested_interval_min": 5.0,
        "interval_choices": [5.0, 10.0, 15.0],
        "duration_param": "duration_hr",
        "suggested_duration_hr": 6.0,
        "estimated_frame_count": 72,
        "max_frames": 144,
        "min_interval_min": 1.0,
        "is_coastal": True,
        "reason": "Coastal surge: 5-min frames animate the wave roll-in.",
    },
}

# The hard-cap variant: "proceed" is OMITTED from options (contract: the agent
# removes it above HARD_CAP_MB_DEFAULT).
PAYLOAD_WARNING_HARDCAP_ROW: dict[str, Any] = dict(
    PAYLOAD_WARNING_ROW,
    warning_id="01STUBWARNINGHARDCAPAAAAAA",
    estimated_mb=400.0,
    options=["cancel", "narrow_scope"],
)

STUB_CODE_EXEC_ID = "01STUBCODEEXECAAAAAAAAAAAA"

# A code-exec-request payload field-for-field the CodeExecRequestPayload
# contract (contracts .../sandbox_contracts.py). The confirmation reply rides
# the EXISTING tool-payload-confirmation envelope with warning_id ==
# code_exec_id (the server's shared confirm-gate seam, _gate_on_code_exec --
# no new client verb). Live-feedback 2026-07-21: this envelope previously had
# ZERO plugin handling, so the agent blocked on the gate forever.
CODE_EXEC_REQUEST_ROW: dict[str, Any] = {
    "envelope_type": "code-exec-request",
    "code_exec_id": STUB_CODE_EXEC_ID,
    "python_code": (
        "import numpy as np\n"
        "depth = layer_handles['depth'].read(1)\n"
        "result = float(np.percentile(depth[depth > 0], 95))\n"
    ),
    "layer_refs": {"depth": "s3://trid3nt-runs/flood/depth.tif"},
    "rationale": "Compute the 95th-percentile flood depth over the AOI.",
}

# case-list rows (CaseSummary subset the plugin's case picker consumes).
CASE_LIST_ROWS: list[dict[str, Any]] = [
    {
        "schema_version": "v1",
        "case_id": "01STUBCASELISTAAAAAAAAAAAA",
        "title": "Asheville flood",
        "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-07-06T12:00:00Z",
        "status": "active",
        "bbox": [-82.6, 35.55, -82.5, 35.65],
    },
    {
        "schema_version": "v1",
        "case_id": "01STUBCASELISTBBBBBBBBBBBB",
        "title": "Tampa surge",
        "created_at": "2026-06-20T00:00:00Z",
        "updated_at": "2026-06-21T09:30:00Z",
        "status": "archived",
        "bbox": None,
    },
]


class StubAgentServer:
    """Threaded stub agent. ``start()`` binds an ephemeral port; ``stop()``
    tears the loop down. Records every received envelope + upgrade path."""

    def __init__(self) -> None:
        self.port: Optional[int] = None
        self.received: list[dict] = []
        self.paths: list[str] = []
        self.connection_count = 0
        self.resume_case_ids: list[Optional[str]] = []  # session-resume payloads
        self.confirmations: list[dict] = []  # tool-payload-confirmation payloads
        self.selects: list[Optional[str]] = []  # case-command select case_ids
        #: When set, a BARE session-resume (payload case_id None) answers with
        #: THIS case_id stamped on the session-state envelope -- the real
        #: server's persisted ``last_active_case_id`` rebind (startup case
        #: reuse, live-feedback 2026-07-09). A client-stamped resume still
        #: echoes the client's id (job-CASE-AUTHORITY).
        self.resume_rebind_case_id: Optional[str] = None
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
        self.connection_count += 1
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
                if token == EXPIRED_TOKEN:
                    # The live agent's dead-token path: an in-band error
                    # envelope, then a policy-violation close (1008). No
                    # auth-ack ever arrives.
                    await send(
                        "error",
                        {
                            "error_code": "AUTH_REQUIRED",
                            "message": "token expired or invalid",
                        },
                    )
                    await ws.close(code=1008, reason="auth required")
                    return
                await send(
                    "auth-ack",
                    {"user_id": STUB_USER_ID, "is_anonymous": token == ""},
                )
            elif etype == "session-resume":
                # Real server interleaves housekeeping before session-state;
                # emit a case-list first so the client's drain is exercised.
                resume_case_id = (env.get("payload") or {}).get("case_id")
                self.resume_case_ids.append(resume_case_id)
                # Persisted last_active_case_id rebind on a BARE resume
                # (startup case reuse) -- the client's own stamp wins.
                if resume_case_id is None and self.resume_rebind_case_id:
                    resume_case_id = self.resume_rebind_case_id
                await send("case-list", {"cases": CASE_LIST_ROWS})
                await send(
                    "session-state",
                    {"chat_history": [], "loaded_layers": [], "pipeline_history": []},
                    case_id=resume_case_id,
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
                elif payload.get("command") == "select":
                    # The real server's _emit_case_open: full rehydration
                    # (CaseSummary + persisted loaded_layers) for a known
                    # Case; session_state=None when it cannot rehydrate.
                    sel_id = payload.get("case_id")
                    self.selects.append(sel_id)
                    row = next(
                        (r for r in CASE_LIST_ROWS if r["case_id"] == sel_id),
                        None,
                    )
                    if row is None:
                        await send("case-open", {"session_state": None})
                    else:
                        await send(
                            "case-open",
                            {
                                "session_state": {
                                    "case": dict(row),
                                    "loaded_layers": [RASTER_LAYER_ROW],
                                    "chat_history": [],
                                    "pipeline_history": [],
                                }
                            },
                            case_id=sel_id,
                        )
            elif etype == "user-message":
                case_id = env.get("case_id")
                text = str((env.get("payload") or {}).get("text") or "")
                if "drop-connection" in text:
                    # Simulate a transport loss mid-turn: close server-side,
                    # NO turn-complete. The serve loop keeps accepting.
                    # (1006 is reserved/unsendable; 1011 = server error.)
                    await ws.close(code=1011, reason="stub drop")
                    return
                if "run-code" in text:
                    # Code-exec HARD confirm gate (live-feedback 2026-07-21):
                    # the agent emits the request and BLOCKS until the matching
                    # confirmation (warning_id == code_exec_id) arrives --
                    # handled in the tool-payload-confirmation branch below.
                    self._pending_gate_case = case_id
                    await send(
                        "code-exec-request", CODE_EXEC_REQUEST_ROW, case_id=case_id
                    )
                    continue
                if "simulate" in text:
                    # Gate the "solve" behind a payload warning; the turn
                    # continues only on the matching confirmation (handled in
                    # the tool-payload-confirmation branch below).
                    row = (
                        PAYLOAD_WARNING_HARDCAP_ROW
                        if "hardcap" in text
                        else PAYLOAD_WARNING_ROW
                    )
                    self._pending_gate_case = case_id
                    await send("tool-payload-warning", row, case_id=case_id)
                    continue
                # F9 (live-feedback 2026-07-09): when the user-message carries
                # show_thinking=True or the text contains "think", emit two
                # agent-thinking-chunk deltas before the answer.
                show_thinking = bool((env.get("payload") or {}).get("show_thinking"))
                if show_thinking or "think" in text:
                    await send(
                        "agent-thinking-chunk",
                        {"message_id": "m1", "delta": "Considering the request... ", "done": False},
                        case_id=case_id,
                    )
                    await send(
                        "agent-thinking-chunk",
                        {"message_id": "m1", "delta": "Fetching DEM.", "done": True},
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
                            LEGACY_RASTER_LAYER_ROW,
                            VECTOR_LAYER_ROW,
                            S3_VECTOR_LAYER_ROW,
                        ],
                        "pipeline_history": [],
                    },
                    case_id=case_id,
                )
                await send("turn-complete", {}, case_id=case_id)
            elif etype == "tool-payload-confirmation":
                payload = env.get("payload") or {}
                self.confirmations.append(payload)
                gate_case = getattr(self, "_pending_gate_case", None)
                decision = payload.get("decision")
                if payload.get("warning_id") == STUB_CODE_EXEC_ID:
                    # The code-exec confirm reply (warning_id == code_exec_id;
                    # the live server FAIL-CLOSES anything != "proceed").
                    if decision == "proceed":
                        await send(
                            "agent-message-chunk",
                            {
                                "message_id": "m-code",
                                "delta": "Code executed.",
                                "done": True,
                            },
                            case_id=gate_case,
                        )
                        await send("turn-complete", {}, case_id=gate_case)
                    else:
                        await send(
                            "turn-complete", {"cancelled": True}, case_id=gate_case
                        )
                elif decision == "cancel":
                    await send(
                        "turn-complete", {"cancelled": True}, case_id=gate_case
                    )
                else:
                    revised = payload.get("revised_args") or {}
                    resolution = revised.get("grid_resolution_m", 30.0)
                    await send(
                        "agent-message-chunk",
                        {
                            "message_id": "m-gate",
                            "delta": f"Starting the run at {resolution:g} m.",
                            "done": True,
                        },
                        case_id=gate_case,
                    )
                    await send("turn-complete", {}, case_id=gate_case)

            elif etype == "cancel":
                await send("turn-complete", {"cancelled": True}, case_id=env.get("case_id"))
