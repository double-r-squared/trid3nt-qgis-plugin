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

Structured-AOI additions (ADR 0017 mechanism 2, 2026-07-22):

* every ``user-message`` payload is validated the way the live server's
  ``UserMessagePayload`` (``extra="forbid"``) would: unknown keys or a
  malformed ``aoi_bbox`` (anything but a 4-number EPSG:4326
  ``[min_lon, min_lat, max_lon, max_lat]`` list, or null) get an ``error``
  envelope with ``error_code=TOOL_PARAMS_INVALID`` and NO turn-complete --
  a contract regression fails LOUDLY offline instead of 400ing live.
  Violations are recorded on ``protocol_violations``; each present
  ``aoi_bbox`` value is recorded on ``user_message_aoi_bboxes``.

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

#: The exact ``UserMessagePayload`` field surface (contracts ws.py). The live
#: server is ``extra="forbid"`` -- any other key is a 400 there, so the stub
#: rejects it too (ADR 0017 structured-AOI landing; ``tool_choice_mode`` is
#: the ADR 0018 auto/ask carrier).
USER_MESSAGE_ALLOWED_KEYS = frozenset(
    {
        "text",
        "research_mode",
        "model_id",
        "case_id",
        "show_thinking",
        "aoi_bbox",
        "tool_choice_mode",
    }
)


def _aoi_bbox_problem(value: Any) -> Optional[str]:
    """Why ``value`` is not a contract-legal ``aoi_bbox`` (None = it is legal).

    Mirrors the ``UserMessagePayload._validate_aoi_bbox`` rules: null, or a
    list of exactly 4 finite numbers in EPSG:4326
    ``[min_lon, min_lat, max_lon, max_lat]`` order.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        return f"aoi_bbox must be a list or null, got {type(value).__name__}"
    if len(value) != 4:
        return f"aoi_bbox must have exactly 4 elements, got {len(value)}"
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        return f"aoi_bbox elements must all be numbers: {value!r}"
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in value)
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        return f"aoi_bbox longitudes out of range [-180, 180]: {value!r}"
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        return f"aoi_bbox latitudes out of range [-90, 90]: {value!r}"
    if min_lon > max_lon or min_lat > max_lat:
        return f"aoi_bbox min > max (order is [min_lon, min_lat, max_lon, max_lat]): {value!r}"
    return None

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

STUB_TOOL_CANDIDATES_REQUEST_ID = "01STUBTOOLPICKAAAAAAAAAAAA"

# A tool-candidates payload field-for-field the ToolCandidatesPayload contract
# (contracts ws.py, ADR 0018 auto/ask modes). Candidates arrive ranked
# best-first; ``reason`` is the closed enum ("ambiguity" = AUTO-mode measured
# near-tie / "ask_mode" = the user's ASK mode surfacing every staged
# selection); ``timeout_s`` is the SERVER's fail-open window -- unanswered,
# the live server proceeds with its own top pick (simulated here by the
# "which-tool-timeout" trigger, which emits the request and immediately moves
# the turn on without waiting). The reply is ONE ``tool-choice`` envelope
# (request_id echo + tool_name XOR free_text, or both None = let the agent
# decide), handled in the tool-choice branch below.
TOOL_CANDIDATES_ROW: dict[str, Any] = {
    "request_id": STUB_TOOL_CANDIDATES_REQUEST_ID,
    "stage_label": "Data step",
    "candidates": [
        {
            "tool_name": "spatial_query",
            "summary": "Query/summarize features of a loaded layer",
            "score": 0.62,
        },
        {
            "tool_name": "assess_building_damage",
            "summary": "Estimate structural damage over an AOI",
            "score": 0.61,
        },
        {
            "tool_name": "fetch_landcover",
            "summary": "Fetch NLCD landcover for an AOI",
            "score": 0.44,
        },
    ],
    "reason": "ambiguity",
    "timeout_s": 60.0,
}

STUB_CREDENTIAL_REQUEST_ID = "01STUBCREDREQAAAAAAAAAAAAA"

# A credential-request payload field-for-field the
# CredentialRequestEnvelopePayload contract (contracts .../secrets.py). The
# reply is TWO envelopes in Decision-F order: secret-add (the ONLY transport
# for the raw key; the live server vault-writes it 0600 and answers with a
# refreshed secrets-list) THEN credential-provided (request_id echo +
# provided=True) -- or credential-provided provided=False alone on Skip (the
# live server then re-raises the tool's original typed error). LANE K
# 2026-07-22: this envelope previously had ZERO plugin handling (the exact
# code-exec gap), so the agent's paused keyed tool waited out its TTL.
CREDENTIAL_REQUEST_ROW: dict[str, Any] = {
    "envelope_type": "credential-request",
    "request_id": STUB_CREDENTIAL_REQUEST_ID,
    "provider_id": "firms",
    "provider_label": "NASA FIRMS",
    "signup_url": "https://firms.modaps.eosdis.nasa.gov/api/map_key/",
    "secret_key_name": "FIRMS_MAP_KEY",
    "message": (
        "I need a NASA FIRMS map key to fetch the active-fire detections "
        "for this Case."
    ),
    "tool_name": "fetch_active_fires",
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
        self.secret_adds: list[dict] = []  # secret-add payloads (LANE K)
        self.credential_replies: list[dict] = []  # credential-provided payloads
        self.tool_choices: list[dict] = []  # tool-choice payloads (ADR 0018)
        #: ADR 0018 auto/ask carrier: every ``tool_choice_mode`` value PRESENT
        #: on a user-message payload (omitted keys are not recorded -- the
        #: default-auto send stays byte-identical, mirroring aoi_bbox).
        self.user_message_tool_choice_modes: list = []
        self.selects: list[Optional[str]] = []  # case-command select case_ids
        #: ADR 0017 structured AOI: every ``aoi_bbox`` value PRESENT on a
        #: user-message payload (omitted keys are not recorded).
        self.user_message_aoi_bboxes: list = []
        #: user-message contract violations (unknown key / malformed
        #: aoi_bbox) -- tests assert this stays empty.
        self.protocol_violations: list[str] = []
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
                payload = env.get("payload") or {}
                # ADR 0017 structured-AOI contract gate: mimic the live
                # server's extra="forbid" UserMessagePayload validation so a
                # plugin regression fails LOUDLY offline (error envelope, NO
                # turn-complete) instead of 400ing against the live agent.
                unknown_keys = sorted(set(payload) - USER_MESSAGE_ALLOWED_KEYS)
                problem: Optional[str] = None
                if unknown_keys:
                    problem = (
                        "user-message payload carries unknown key(s) "
                        f"{unknown_keys} (live server is extra=forbid)"
                    )
                elif "aoi_bbox" in payload:
                    problem = _aoi_bbox_problem(payload["aoi_bbox"])
                if problem is not None:
                    self.protocol_violations.append(problem)
                    await send(
                        "error",
                        {"error_code": "TOOL_PARAMS_INVALID", "message": problem},
                        case_id=case_id,
                    )
                    continue
                if "aoi_bbox" in payload:
                    self.user_message_aoi_bboxes.append(payload["aoi_bbox"])
                if "tool_choice_mode" in payload:
                    self.user_message_tool_choice_modes.append(
                        payload["tool_choice_mode"]
                    )
                text = str(payload.get("text") or "")
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
                if "need-key" in text:
                    # Credential-request pause (LANE K): a keyed tool hit a
                    # missing key; the agent pauses the tool and BLOCKS until
                    # the credential-provided reply arrives -- handled in the
                    # credential-provided branch below. (The live server's
                    # 300s gate TTL is not simulated; the pause is indefinite
                    # within a test run.)
                    self._pending_gate_case = case_id
                    await send(
                        "credential-request",
                        CREDENTIAL_REQUEST_ROW,
                        case_id=case_id,
                    )
                    continue
                if "which-tool-timeout" in text:
                    # ADR 0018 fail-open twin: the live server emits the
                    # picker, waits timeout_s WITHOUT a tool-choice, then
                    # proceeds with its own top pick. Simulated with zero
                    # wait: the request goes out and the turn IMMEDIATELY
                    # moves on -- the client sees a subsequent turn event
                    # with the card unanswered (the dock folds it to "agent
                    # proceeded").
                    await send(
                        "tool-candidates", TOOL_CANDIDATES_ROW, case_id=case_id
                    )
                    await send(
                        "agent-message-chunk",
                        {
                            "message_id": "m-pick",
                            "delta": "No answer -- proceeding with spatial_query.",
                            "done": True,
                        },
                        case_id=case_id,
                    )
                    await send("turn-complete", {}, case_id=case_id)
                    continue
                if "which-tool" in text:
                    # ADR 0018 picker pause: the agent surfaces the ranked
                    # candidates and BLOCKS until the tool-choice reply
                    # arrives -- handled in the tool-choice branch below.
                    # (The live server's timeout_s fail-open is not simulated
                    # on this trigger; the pause is indefinite within a test
                    # run -- the timeout path is the trigger above.)
                    self._pending_gate_case = case_id
                    await send(
                        "tool-candidates", TOOL_CANDIDATES_ROW, case_id=case_id
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

            elif etype == "secret-add":
                # LANE K (contract SecretAddEnvelopePayload): the ONLY
                # envelope that ever carries the raw key (Decision F). The
                # live server vault-writes key_value and answers with a
                # refreshed secrets-list whose SecretRecord rows carry
                # vault_ref, NEVER the raw value -- mirrored here so a
                # client that echoed the key back would fail the round trip.
                payload = env.get("payload") or {}
                self.secret_adds.append(payload)
                await send(
                    "secrets-list",
                    {
                        "secrets": [
                            {
                                "schema_version": "v1",
                                "secret_id": "01STUBSECRETRECAAAAAAAAAAA",
                                "provider": payload.get("provider"),
                                "case_id": payload.get("case_id"),
                                "vault_ref": "file-vault://stub/secret",
                                "label": payload.get("label"),
                                "added_at": "2026-07-22T00:00:00Z",
                                "last_used_at": None,
                                "is_active": True,
                            }
                        ]
                    },
                    case_id=env.get("case_id"),
                )
            elif etype == "credential-provided":
                # LANE K (contract CredentialProvidedEnvelopePayload): the
                # retry signal that resolves the agent's paused-tool future.
                # provided=True -> the live server re-resolves the freshly
                # vault-written key and retries the tool ONCE; provided=False
                # -> it re-raises the original typed error and the agent
                # narrates honestly. NO key material rides here.
                payload = env.get("payload") or {}
                self.credential_replies.append(payload)
                gate_case = getattr(self, "_pending_gate_case", None)
                if payload.get("provided"):
                    await send(
                        "agent-message-chunk",
                        {
                            "message_id": "m-cred",
                            "delta": "Key accepted -- fire detections fetched.",
                            "done": True,
                        },
                        case_id=gate_case,
                    )
                    await send("turn-complete", {}, case_id=gate_case)
                else:
                    await send(
                        "agent-message-chunk",
                        {
                            "message_id": "m-cred",
                            "delta": (
                                "No key provided -- the FIRMS fetch failed "
                                "with its original auth error."
                            ),
                            "done": True,
                        },
                        case_id=gate_case,
                    )
                    await send("turn-complete", {}, case_id=gate_case)
            elif etype == "tool-choice":
                # ADR 0018 (contract ToolChoicePayload): the picker reply --
                # request_id echo + exactly one of three shapes. The live
                # server resumes its paused selection with the verbatim pick,
                # feeds free text back into the selection step, or proceeds
                # with its own top pick on both-None; the narration here names
                # which path ran so round-trip tests can assert it.
                payload = env.get("payload") or {}
                self.tool_choices.append(payload)
                gate_case = getattr(self, "_pending_gate_case", None)
                tool_name = payload.get("tool_name")
                free_text = payload.get("free_text")
                if tool_name:
                    delta = f"Running {tool_name}."
                elif free_text:
                    delta = "Taking your guidance: " + str(free_text)
                else:
                    delta = "Agent decided: spatial_query."
                await send(
                    "agent-message-chunk",
                    {"message_id": "m-pick", "delta": delta, "done": True},
                    case_id=gate_case,
                )
                await send("turn-complete", {}, case_id=gate_case)
            elif etype == "cancel":
                await send("turn-complete", {"cancelled": True}, case_id=env.get("case_id"))
