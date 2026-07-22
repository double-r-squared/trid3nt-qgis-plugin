"""TRID3NT agent WebSocket client -- pure Python, stdlib only.

This module is the plugin's CONNECTION LAYER. Hard rules:

  * NO PyQGIS / PyQt imports -- it must be importable and unit-testable with
    any plain CPython (the tests run it under the trid3nt-local agent venv,
    outside QGIS entirely).
  * stdlib only. QGIS's bundled Python does NOT reliably ship a WebSocket
    library: on Debian/Ubuntu ``python3-qgis`` depends on neither
    ``websockets`` nor ``websocket-client``, and ``python3-pyqt5.qtwebsockets``
    is a separate package QGIS does not require. Shipping our own minimal
    RFC 6455 client (~200 lines) removes the dependency gamble on every
    platform and keeps the plugin zip pure-python (QGIS plugin repository
    no-binaries rule).

Protocol (mirrors the web client's ``ws.ts`` (separate repo) + ``scripts/tool_routing_bench.py``):

  envelope   {"type", "id" (ULID), "ts" (ISO-8601 Z), "session_id",
              "case_id", "payload"}
  handshake  send ``auth-token`` -> expect ``auth-ack``;
             send ``session-resume`` -> drain until ``session-state``
  case       send ``case-command`` {command: "create", args: {title}} ->
             drain until ``case-open``; case_id at
             payload.session_state.case.case_id
  chat       send ``user-message`` {text, case_id}; the reply streams as
             ``agent-message-chunk`` / ``pipeline-state`` / ``session-state``
             (layers ride on ``loaded_layers``) and terminates with
             ``turn-complete``.
  remote     token rides BOTH as the ``?st=<token>`` query param (the cloud
             broker's pre-upgrade carrier) AND inside the ``auth-token``
             envelope. Local mode sends an empty token (anonymous).

Threading: ``WebSocketConnection.send_text`` is mutex-guarded so a UI thread
may send while a worker thread blocks in ``recv``. Everything else is
single-consumer (one reader thread).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import socket
import ssl as ssl_module
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

__all__ = [
    "AgentClient",
    "AgentEvent",
    "CaseInfo",
    "CaseListRequestError",
    "CaseOpenInfo",
    "ModelListRequestError",
    "ProviderConfigRequestError",
    "fetch_model_list",
    "post_provider_config",
    "ConnectionClosed",
    "Debouncer",
    "HandshakeFailed",
    "LayerEvent",
    "OUTBOUND_QUEUE_MAX",
    "PipelineStep",
    "RECONNECT_FLOOR_MS",
    "RECONNECT_MAX_MS",
    "REFRESH_DEBOUNCE_S",
    "CHAT_HISTORY_REPLAY_MAX",
    "WebSocketConnection",
    "WebSocketError",
    "build_ws_url",
    "choose_startup_case",
    "fetch_case_list",
    "find_fallback_bbox",
    "is_auth_failure",
    "make_envelope",
    "new_ulid",
    "next_backoff",
    "parse_case_list",
    "parse_case_open",
    "parse_chat_history",
    "parse_layer_events",
    "parse_pipeline_steps",
    "qgis_xyz_uri",
    "s3_to_http",
    "utc_ts",
]

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# --------------------------------------------------------------------------- #
# ULID + envelope helpers
# --------------------------------------------------------------------------- #

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """26-char Crockford-base32 ULID (48-bit ms timestamp + 80-bit random)."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> (5 * (25 - i))) & 0x1F] for i in range(26))


def utc_ts() -> str:
    """ISO-8601 UTC timestamp with a literal Z suffix (contract A.1)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_envelope(
    type_: str,
    session_id: str,
    payload: dict,
    case_id: Optional[str] = None,
) -> dict:
    """Build a wire envelope dict (contract A.1)."""
    return {
        "type": type_,
        "id": new_ulid(),
        "ts": utc_ts(),
        "session_id": session_id,
        "case_id": case_id,
        "payload": payload,
    }


def build_ws_url(base_url: str, token: Optional[str] = None) -> str:
    """Append the ``?st=<token>`` carrier the cloud broker authenticates on.

    The broker reads the token from the query string BEFORE the WebSocket
    upgrade completes (subprotocol-based carriers were stripped by CloudFront;
    see the per-user-isolation deploy notes). No-op when ``token`` is falsy
    (local anonymous mode).
    """
    if not token:
        return base_url
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}st={urllib.parse.quote(token, safe='')}"


# --------------------------------------------------------------------------- #
# Layer + pipeline event parsing (pure)
# --------------------------------------------------------------------------- #


@dataclass
class LayerEvent:
    """One row of ``session-state.loaded_layers`` (ProjectLayerSummary +
    the additive ``inline_geojson`` the local agent merges in)."""

    layer_id: str
    name: str
    layer_type: str  # "raster" | "vector" | ...
    uri: str
    wms_url: Optional[str] = None
    style_preset: Optional[str] = None
    inline_geojson: Optional[dict] = None
    opacity: Optional[float] = None
    visible: bool = True
    legend: Optional[dict] = None
    raw: dict = field(default_factory=dict)

    @property
    def tile_template(self) -> Optional[str]:
        """The XYZ tile TEMPLATE for a raster layer, or None.

        The local agent publishes rasters with a ready TiTiler template
        (contains ``{z}/{x}/{y}``) in ``uri`` (see Map.tsx buildWmsTileUrl
        pass-through); ``wms_url`` is checked as a fallback carrier.
        """
        if "{z}" in (self.uri or ""):
            return self.uri
        if self.wms_url and "{z}" in self.wms_url:
            return self.wms_url
        return None


def parse_layer_events(session_state_payload: dict) -> list[LayerEvent]:
    """Parse ``session-state.loaded_layers`` rows into ``LayerEvent``s.

    Defensive: malformed rows are skipped, never raised on -- a bad layer row
    must not take down the chat stream.
    """
    events: list[LayerEvent] = []
    rows = session_state_payload.get("loaded_layers") or []
    if not isinstance(rows, list):
        return events
    for row in rows:
        if not isinstance(row, dict):
            continue
        layer_id = row.get("layer_id")
        uri = row.get("uri")
        if not isinstance(layer_id, str) or not layer_id:
            continue
        inline = row.get("inline_geojson")
        if not isinstance(inline, dict):
            inline = None
        events.append(
            LayerEvent(
                layer_id=layer_id,
                name=str(row.get("name") or layer_id),
                layer_type=str(row.get("layer_type") or "raster"),
                uri=uri if isinstance(uri, str) else "",
                wms_url=row.get("wms_url") if isinstance(row.get("wms_url"), str) else None,
                style_preset=row.get("style_preset")
                if isinstance(row.get("style_preset"), str)
                else None,
                inline_geojson=inline,
                opacity=row.get("opacity") if isinstance(row.get("opacity"), (int, float)) else None,
                visible=bool(row.get("visible", True)),
                legend=row.get("legend") if isinstance(row.get("legend"), dict) else None,
                raw=row,
            )
        )
    return events


@dataclass
class PipelineStep:
    """Subset of PipelineStepSummary (contract D.6) the dock renders."""

    step_id: str
    name: str
    tool_name: str
    state: str  # pending | running | complete | failed | cancelled
    parent_step_id: Optional[str] = None
    substep_label: Optional[str] = None
    error_message: Optional[str] = None
    # Item R4 (live-feedback 2026-07-18): the two-card sim observability
    # fields (contract ws.PipelineStep, task-149). ``role`` discriminates the
    # off-box solver card ("compute", minted by
    # pipeline_emitter.mint_dispatch_and_sim_cards with tool_name
    # "<solver>:solve") from a plain tool card ("tool"); the dock routes
    # compute steps to the collapsible SimCard instead of grey rows.
    # ``batch_job_id`` is "local-docker:<run_id>" on the local seam;
    # ``batch_status`` mirrors the control plane verbatim; ``duration_ms``
    # is stamped on the terminal transition only.
    role: str = "tool"
    batch_job_id: Optional[str] = None
    batch_status: Optional[str] = None
    progress_percent: Optional[int] = None
    duration_ms: Optional[int] = None


def parse_pipeline_steps(pipeline_state_payload: dict) -> list[PipelineStep]:
    """Parse ``pipeline-state.steps`` into ``PipelineStep``s (defensive)."""
    steps: list[PipelineStep] = []
    rows = pipeline_state_payload.get("steps") or []
    if not isinstance(rows, list):
        return steps
    for row in rows:
        if not isinstance(row, dict):
            continue
        step_id = row.get("step_id")
        if not isinstance(step_id, str) or not step_id:
            continue
        steps.append(
            PipelineStep(
                step_id=step_id,
                name=str(row.get("name") or ""),
                tool_name=str(row.get("tool_name") or row.get("name") or ""),
                state=str(row.get("state") or "pending"),
                parent_step_id=row.get("parent_step_id")
                if isinstance(row.get("parent_step_id"), str)
                else None,
                substep_label=row.get("substep_label")
                if isinstance(row.get("substep_label"), str)
                else None,
                error_message=row.get("error_message")
                if isinstance(row.get("error_message"), str)
                else None,
                # Item R4 (live-feedback 2026-07-18): sim-card fields; all
                # optional on the wire, all default-preserving here.
                role=str(row.get("role") or "tool"),
                batch_job_id=row.get("batch_job_id")
                if isinstance(row.get("batch_job_id"), str)
                else None,
                batch_status=row.get("batch_status")
                if isinstance(row.get("batch_status"), str)
                else None,
                progress_percent=row.get("progress_percent")
                if isinstance(row.get("progress_percent"), int)
                else None,
                duration_ms=row.get("duration_ms")
                if isinstance(row.get("duration_ms"), int)
                else None,
            )
        )
    return steps


# --------------------------------------------------------------------------- #
# Case-list parsing (pure)
# --------------------------------------------------------------------------- #


@dataclass
class CaseInfo:
    """One row of the ``case-list`` envelope (subset of ``CaseSummary`` the
    dock's case picker renders)."""

    case_id: str
    title: str
    status: str = "active"
    updated_at: str = ""
    bbox: Optional[list] = None  # [lon_min, lat_min, lon_max, lat_max]
    raw: dict = field(default_factory=dict)


def parse_case_list(payload: dict) -> list[CaseInfo]:
    """Parse ``case-list.cases`` rows into ``CaseInfo``s (defensive: bad rows
    are skipped, never raised on)."""
    cases: list[CaseInfo] = []
    rows = payload.get("cases") or []
    if not isinstance(rows, list):
        return cases
    for row in rows:
        if not isinstance(row, dict):
            continue
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            continue
        bbox = row.get("bbox")
        if not (
            isinstance(bbox, list)
            and len(bbox) == 4
            and all(isinstance(v, (int, float)) for v in bbox)
        ):
            bbox = None
        cases.append(
            CaseInfo(
                case_id=case_id,
                title=str(row.get("title") or case_id),
                status=str(row.get("status") or "active"),
                updated_at=str(row.get("updated_at") or ""),
                bbox=bbox,
                raw=row,
            )
        )
    return cases


def choose_startup_case(
    resumed_case_id: Optional[str],
    cases: list,
) -> Tuple[str, Optional[str]]:
    """Decide which case a fresh LOCAL connect should bind (PURE -- no
    sockets, no Qt, so the startup decision is unit-testable).

    Live-feedback 2026-07-09: ``connect_agent`` used to CREATE a fresh
    "QGIS session ..." case on every dock-show, regrowing exactly the case
    clutter the user just purged (157 junk cases). The decision ladder:

      ("resume", id)   the session-resume handshake already rebound a
                       persisted active case -- keep it.
      ("select", id)   no resumed case, but the user HAS cases -- reuse the
                       NEWEST live one (``updated_at`` desc; ISO-8601 Z
                       strings sort lexicographically). Tombstoned
                       (deleted/archived) and malformed rows are skipped.
      ("create", None) zero usable cases -- only then is a fresh case
                       created (or explicitly via the New case button).
    """
    if isinstance(resumed_case_id, str) and resumed_case_id:
        return ("resume", resumed_case_id)
    candidates = []
    for case in cases or []:
        case_id = getattr(case, "case_id", None)
        if not isinstance(case_id, str) or not case_id:
            continue
        status = getattr(case, "status", "") or ""
        if status in ("deleted", "archived"):
            continue
        candidates.append(case)
    if candidates:
        newest = max(
            candidates, key=lambda c: str(getattr(c, "updated_at", "") or "")
        )
        return ("select", newest.case_id)
    return ("create", None)


class CaseListRequestError(Exception):
    """``fetch_case_list`` failed -- transport, HTTP status, or a non-JSON
    body. Carries an honest, user-facing message."""


def fetch_case_list(base_url: str, timeout: float = 5.0) -> list:
    """``GET {base_url}/api/case-list`` -- the COLD case list, no WS session.

    Live-feedback 2026-07-09: the QGIS dock previously could not show ANY
    cases until the user pressed Connect, because ``case-list`` only ever
    arrived as a WS envelope. The local agent's HTTP listener
    (``tool_catalog_http.py``) mirrors that same envelope's data over plain
    HTTP for the local single-user seam, so the dock's Cases dialog can
    populate BEFORE a connection exists. Plain ``urllib`` (stdlib only, same
    posture as ``case_export.py``'s HTTP calls) -- no WebSocket involved.

    Returns ``[]`` (never raises) is NOT the contract here: a genuine failure
    (agent HTTP listener down, route absent, bad body) raises
    ``CaseListRequestError`` with an honest message so the caller can show
    it -- never a silently-empty dialog that looks like "no cases exist".
    Row-level parsing stays defensive (``parse_case_list`` skips malformed
    rows; a partially-bad payload still yields the good rows).
    """
    url = f"{base_url.rstrip('/')}/api/case-list"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            if isinstance(body, dict):
                detail = str(body.get("error") or "")
        except Exception:  # noqa: BLE001 -- body may be anything
            pass
        raise CaseListRequestError(
            detail or f"case list request failed (HTTP {exc.code})"
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise CaseListRequestError(
            f"agent HTTP API unreachable at {url} ({exc})"
        ) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaseListRequestError(
            f"case list API returned non-JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise CaseListRequestError("case list API returned a non-object body")
    return parse_case_list(payload)


# --------------------------------------------------------------------------- #
# OpenRouter model-extensibility (design 2026-07-19) -- provider-config POST +
# live model-list GET, both against the local agent's HTTP listener.
# --------------------------------------------------------------------------- #


class ProviderConfigRequestError(Exception):
    """``post_provider_config`` failed -- transport, HTTP status, or a non-JSON
    body. Carries an honest, user-facing message that NEVER contains the api
    key."""


def post_provider_config(base_url: str, payload: dict, timeout: float = 5.0) -> dict:
    """``POST {base_url}/api/provider-config`` -- push the LIVE provider config
    to the agent so a provider/model/key switch applies on the NEXT message
    with no agent restart (the agent's OpenAI adapter reads ``GRACE2_OPENAI_*``
    from ``os.environ`` at call time). ``payload`` = ``{base_url, api_key,
    model, num_ctx}`` (any subset). Plain ``urllib`` (stdlib only, same posture
    as ``fetch_case_list``) -- no WebSocket involved.

    Returns the agent's ``{"ok", "model", "base_url_host"}`` result dict, or
    raises ``ProviderConfigRequestError`` with an honest message on any fault.
    SECURITY: the api_key rides the POST body but is NEVER logged here, and a
    raised message never echoes it (the agent likewise scrubs it).
    """
    url = f"{base_url.rstrip('/')}/api/provider-config"
    raw = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            parsed = json.loads(exc.read().decode("utf-8", "replace"))
            if isinstance(parsed, dict):
                detail = str(parsed.get("error") or "")
        except Exception:  # noqa: BLE001 -- body may be anything
            pass
        raise ProviderConfigRequestError(
            detail or f"provider-config request failed (HTTP {exc.code})"
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ProviderConfigRequestError(
            f"agent HTTP API unreachable at {url} ({exc})"
        ) from exc
    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderConfigRequestError(
            f"provider-config returned non-JSON: {exc}"
        ) from exc
    if not isinstance(result, dict):
        raise ProviderConfigRequestError(
            "provider-config returned a non-object body"
        )
    return result


class ModelListRequestError(Exception):
    """``fetch_model_list`` failed -- transport, HTTP status, or a non-JSON
    body. Carries an honest, user-facing message."""


def fetch_model_list(
    base_url: str, timeout: float = 8.0
) -> Tuple[list, Optional[str]]:
    """``GET {base_url}/api/local-models`` -> ``(model_ids, default)``.

    For an OpenRouter provider the agent returns the FREE + tool-capable model
    ids (design 2026-07-19); for local Ollama it returns the installed models.
    The plugin's model combo stays EDITABLE either way, so any id is still
    typeable -- this list is a convenience dropdown, not a whitelist. Plain
    ``urllib`` (stdlib only). Raises ``ModelListRequestError`` on any fault so
    the caller can fall back to its static shortlist.
    """
    url = f"{base_url.rstrip('/')}/api/local-models"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            parsed = json.loads(exc.read().decode("utf-8", "replace"))
            if isinstance(parsed, dict):
                detail = str(parsed.get("error") or "")
        except Exception:  # noqa: BLE001 -- body may be anything
            pass
        raise ModelListRequestError(
            detail or f"model list request failed (HTTP {exc.code})"
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ModelListRequestError(
            f"agent HTTP API unreachable at {url} ({exc})"
        ) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelListRequestError(
            f"model list returned non-JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ModelListRequestError("model list returned a non-object body")
    ids: list = []
    models = payload.get("models")
    if isinstance(models, list):
        for m in models:
            if isinstance(m, dict):
                mid = m.get("id")
                if isinstance(mid, str) and mid.strip():
                    ids.append(mid.strip())
    default = payload.get("default")
    if not isinstance(default, str) or not default.strip():
        default = None
    return ids, default


# --------------------------------------------------------------------------- #
# Case-open parsing (pure) -- the select/rebind rehydration
# --------------------------------------------------------------------------- #


#: Cap on chat-history replay rows (ITEM B dock snappiness -- a Case that has
#: chatted for hours must not stall the dock repainting hundreds of bubbles).
CHAT_HISTORY_REPLAY_MAX = 50


def parse_chat_history(session_state_payload: dict) -> list:
    """Parse ``session_state.chat_history`` rows (``CaseChatMessage`` --
    contracts ``case.py``) into plain ``{"role", "content"}`` dicts for the
    dock's case-open chat replay.

    ``user``/``agent`` rows surface as the plain CONVERSATION (user bubbles +
    assistant bubbles, no thinking blocks). Item H (qgis-ux-batch 2026-07-19):
    ``role == "tool"`` rows are ALSO surfaced now (they were dropped, so a
    reopened case lost its whole tool-call chain -- the dock lagged the web
    client, which already replays ``tool_card`` on reopen). A tool row carries
    the typed ``tool_card`` dict (the contract-blessed ``ToolCardRecord``
    payload -- name/state/args/response) plus the ``content`` JSON twin; the
    dock's ``_replay_chat_history`` renders it as a collapsed tool-card row.
    ``system`` rows stay dropped. Defensive: a missing/non-list ``chat_history``,
    a non-dict row, or a row with a missing/non-string ``role``/``content`` (and,
    for tool rows, no usable ``tool_card`` either) is skipped, never raised on --
    a bad persisted row must not break a case switch. Capped to the most recent
    ``CHAT_HISTORY_REPLAY_MAX`` rows (persisted order is oldest-first, so the cap
    keeps the TAIL -- the most recent conversation).
    """
    rows = session_state_payload.get("chat_history") or []
    if not isinstance(rows, list):
        return []
    out: list = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        role = row.get("role")
        content = row.get("content")
        if role == "tool":
            # Carry the typed tool_card dict (preferred render source) plus the
            # content JSON twin; skip only when NEITHER is usable.
            tool_card = row.get("tool_card")
            if not isinstance(tool_card, dict) and not (
                isinstance(content, str) and content
            ):
                continue
            out.append(
                {"role": "tool", "tool_card": tool_card, "content": content}
            )
            continue
        if role not in ("user", "agent"):
            continue
        if not isinstance(content, str) or not content:
            continue
        out.append({"role": role, "content": content})
    return out[-CHAT_HISTORY_REPLAY_MAX:]


def parse_charts(session_state_payload: dict) -> list:
    """Parse ``session_state.charts`` rows (persisted ``ChartEmissionPayload``
    dicts -- contracts ``chart_contracts.py``) for the dock's Charts panel
    (OpenQuake result parity, live-feedback 2026-07-13).

    The server hydrates the session document's append-only ``charts`` array
    into every ``case-open`` rehydration (oldest-first, job-0294b), so the
    envelope the plugin already receives carries them -- no extra fetch.
    Each row is the exact payload a live ``chart-emission`` frame carries:
    ``chart_id`` + ``title`` + ``caption`` + the Vega-Lite ``vega_lite_spec``.
    Defensive like ``parse_chat_history``: a missing/non-list field, a
    non-dict row, or a row without a usable chart_id/spec is skipped, never
    raised on -- a bad persisted chart must not break a case switch. Order
    is preserved (the panel shows the newest = last).
    """
    rows = session_state_payload.get("charts") or []
    if not isinstance(rows, list):
        return []
    out: list = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        chart_id = row.get("chart_id")
        spec = row.get("vega_lite_spec")
        if not isinstance(chart_id, str) or not chart_id:
            continue
        if not isinstance(spec, dict) or not spec:
            continue
        out.append(row)
    return out


@dataclass
class CaseOpenInfo:
    """The rehydration a ``case-open`` envelope carries (select response).

    ``session_state.case`` is the CaseSummary; ``loaded_layers`` rides in the
    same session_state so the client can repaint the reopened Case's layers.
    ``bbox`` (EPSG:4326 ``[lon_min, lat_min, lon_max, lat_max]``) lets the
    dock zoom the canvas to the case instead of leaving it wherever it was
    (the "canvas is just white" fix) -- may be absent/None on cases that
    predate the #170 AOI-first bbox seeding. ``chat_messages`` (ITEM B) is
    the same session_state's persisted ``chat_history``, defensively parsed
    via ``parse_chat_history``, so the dock can replay the opened Case's
    conversation instead of leaving the previous Case's bubbles on screen.
    """

    case_id: str
    title: str
    layers: list = field(default_factory=list)  # list[LayerEvent]
    bbox: Optional[Tuple[float, float, float, float]] = None
    chat_messages: list = field(default_factory=list)  # list[{"role","content"}]
    # OpenQuake result parity (live-feedback 2026-07-13): the persisted
    # ``session_state.charts`` replay set (ChartEmissionPayload dicts,
    # oldest-first) -- feeds the dock's Charts panel on case open.
    charts: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _coerce_bbox(raw) -> Optional[Tuple[float, float, float, float]]:
    """A candidate ``[lon_min, lat_min, lon_max, lat_max]`` value -> a clean
    float 4-tuple, or None when it is not a well-formed EPSG:4326 bbox.
    Never raises."""
    if (
        isinstance(raw, (list, tuple))
        and len(raw) == 4
        and all(isinstance(v, (int, float)) for v in raw)
    ):
        return (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
    return None


def find_fallback_bbox(payload: dict) -> Optional[Tuple[float, float, float, float]]:
    """ITEM D (live-feedback 2026-07-10): scan a ``case-open`` payload for a
    bbox OUTSIDE the primary ``session_state.case.bbox`` carrier ``parse_
    case_open`` already extracts.

    Today's wire contract (``CaseOpenEnvelopePayload`` / ``CaseSessionState``
    / ``CaseSummary``) has exactly ONE bbox field, so in practice this only
    re-finds what ``parse_case_open`` already found -- it exists so a
    raster-only OLD case (no vector layers to fall back to, per the "canvas
    is just white" fix's other rung) still gets a shot at SOME bbox before
    the dock gives up and says so honestly, and so a future server-side
    bbox carrier (e.g. per-layer or top-level) is picked up without another
    client change. Checked in order: ``payload.bbox``, ``payload.
    session_state.bbox``, ``payload.session_state.case.bbox``. Defensive:
    never raises, returns None when nothing usable is found.
    """
    if not isinstance(payload, dict):
        return None
    direct = _coerce_bbox(payload.get("bbox"))
    if direct is not None:
        return direct
    session_state = payload.get("session_state")
    if isinstance(session_state, dict):
        direct = _coerce_bbox(session_state.get("bbox"))
        if direct is not None:
            return direct
        case = session_state.get("case")
        if isinstance(case, dict):
            direct = _coerce_bbox(case.get("bbox"))
            if direct is not None:
                return direct
    return None


def parse_case_open(payload: dict) -> Optional[CaseOpenInfo]:
    """Parse a ``case-open`` payload into a ``CaseOpenInfo``.

    Returns None when the server could not rehydrate -- per
    ``CaseOpenEnvelopePayload`` semantics ``session_state`` is None (or the
    ``case`` row is missing) and the client falls back to the empty state.
    Defensive: never raises on a malformed payload -- a missing/malformed
    ``bbox`` yields ``None`` on the field, never a crash.
    """
    if not isinstance(payload, dict):
        return None
    session_state = payload.get("session_state")
    if not isinstance(session_state, dict):
        return None
    case = session_state.get("case")
    if not isinstance(case, dict):
        return None
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        return None
    bbox = _coerce_bbox(case.get("bbox"))
    return CaseOpenInfo(
        case_id=case_id,
        title=str(case.get("title") or case_id),
        layers=parse_layer_events(session_state),
        bbox=bbox,
        chat_messages=parse_chat_history(session_state),
        charts=parse_charts(session_state),
        raw=payload,
    )


# --------------------------------------------------------------------------- #
# Auth-failure classification (pure) -- milestone 3 token-expiry UX
# --------------------------------------------------------------------------- #


def is_auth_failure(text: str) -> bool:
    """Classify a connection failure as an AUTH failure (dead/expired token)
    vs a transport failure.

    The remote broker validates the ``?st=`` token BEFORE the WebSocket
    upgrade, so a dead token surfaces as ``HandshakeFailed("upgrade rejected:
    HTTP/1.1 401/403 ...")``. An in-band rejection surfaces as an ``error``
    envelope with ``error_code=AUTH_REQUIRED`` followed by a policy-violation
    close (1008). Transport failures (connection refused, read timeout, a
    mid-stream drop) must NOT classify as auth -- those drive the reconnect
    ladder; an auth failure must STOP the ladder instead (retrying a dead
    token is a silent reconnect loop, the exact UX this exists to kill).
    """
    low = (text or "").lower()
    if not low:
        return False
    if "upgrade rejected" in low and (" 401" in low or " 403" in low):
        return True
    markers = (
        "auth_required",
        "auth-ack without user_id",
        "unauthorized",
        "token expired",
        "invalid token",
        "code=1008",
    )
    return any(marker in low for marker in markers)


# --------------------------------------------------------------------------- #
# Refresh debounce (pure) -- milestone 3 case-list refresh
# --------------------------------------------------------------------------- #

#: Minimum seconds between case-list refresh round trips (session-resume is
#: cheap -- the web uses it as a ~25s keepalive -- but a click-happy user
#: should not be able to queue a resume storm).
REFRESH_DEBOUNCE_S = 2.0


class Debouncer:
    """Min-interval debounce: ``allow()`` returns True (and stamps the clock)
    when the action may fire now, False while inside the suppress window.
    Clock injectable for tests."""

    def __init__(
        self,
        interval_s: float = REFRESH_DEBOUNCE_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.interval_s = interval_s
        self._clock = clock
        self._last: Optional[float] = None

    def allow(self) -> bool:
        now = self._clock()
        if self._last is not None and (now - self._last) < self.interval_s:
            return False
        self._last = now
        return True


# --------------------------------------------------------------------------- #
# Reconnect backoff (pure) -- mirrors the web client (ws.ts)
# --------------------------------------------------------------------------- #

#: Backoff FLOOR (ms): the first reconnect after a drop waits at least this
#: long (web BUG 1b raised it from 500 to 1500 to stop reconnect storms).
RECONNECT_FLOOR_MS = 1500
#: Backoff CEILING (ms): the doubling ladder caps here.
RECONNECT_MAX_MS = 5000

#: Outbound-queue bound (web ws.ts sendOrQueue MAX_QUEUE): beyond this the
#: OLDEST frames are dropped first (keep the most recent intent).
OUTBOUND_QUEUE_MAX = 50


def next_backoff(
    base_ms: int,
    rng: Callable[[], float] = random.random,
) -> tuple[int, int]:
    """One rung of the web client's capped-jitter reconnect ladder.

    Returns ``(delay_ms, next_base_ms)``: the actual wait is jittered within
    ``[0.5, 1.0) * base`` (up to 50 percent earlier, never later) and the base
    DOUBLES toward ``RECONNECT_MAX_MS``. Reset the base to
    ``RECONNECT_FLOOR_MS`` after a successful open (as the web does).
    """
    base = max(int(base_ms), 1)
    jitter_factor = 0.5 + 0.5 * rng()
    delay = int(round(base * jitter_factor))
    return delay, min(base * 2, RECONNECT_MAX_MS)


# --------------------------------------------------------------------------- #
# URI helpers (pure)
# --------------------------------------------------------------------------- #


def s3_to_http(uri: str, endpoint: str) -> Optional[str]:
    """Translate ``s3://bucket/key`` to the MinIO path-style http form.

    ``endpoint`` is e.g. ``http://127.0.0.1:9000``. Returns None for
    non-s3 uris.
    """
    if not uri.startswith("s3://"):
        return None
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        return None
    return f"{endpoint.rstrip('/')}/{bucket}/{key}"


def qgis_xyz_uri(template: str, zmin: int = 0, zmax: int = 24) -> str:
    """Build the QGIS ``wms`` provider uri for an XYZ tile TEMPLATE.

    Encode as LITTLE as possible: the installed QGIS build does NOT
    percent-decode the ``url`` component, so a fully-quoted template
    produces a layer that reports valid yet never issues a single tile
    request (proven 2026-07-10 with a request-logging stub server; the
    prior full-quote version painted nothing). Only the template's own
    query-string ampersands are escaped (``%26``) so the provider's
    ``&``-splitting of uri parameters cannot eat them; scheme, slashes,
    ``?``, ``=`` and the ``{z}/{x}/{y}`` placeholders stay literal, and
    already-encoded query values (TiTiler ``url=s3%3A%2F%2F...``) pass
    through verbatim exactly as the tile server expects.
    """
    return (
        f"type=xyz&url={template.replace('&', '%26')}"
        f"&zmin={zmin}&zmax={zmax}"
    )


# --------------------------------------------------------------------------- #
# Minimal RFC 6455 client (stdlib sockets)
# --------------------------------------------------------------------------- #


class WebSocketError(Exception):
    """Base class for connection-layer errors."""


class HandshakeFailed(WebSocketError):
    """The HTTP upgrade or the agent auth handshake failed."""


class ConnectionClosed(WebSocketError):
    """The peer closed the WebSocket (or the TCP stream died)."""

    def __init__(self, code: Optional[int] = None, reason: str = ""):
        self.code = code
        self.reason = reason
        super().__init__(f"connection closed (code={code} reason={reason!r})")


_OP_CONT, _OP_TEXT, _OP_BINARY = 0x0, 0x1, 0x2
_OP_CLOSE, _OP_PING, _OP_PONG = 0x8, 0x9, 0xA


class WebSocketConnection:
    """Blocking RFC 6455 client over a stdlib socket (``ws://`` and ``wss://``).

    Single reader thread; ``send_text``/``close`` are mutex-guarded so any
    thread may write. Handles fragmentation, replies to pings, ignores
    binary frames.
    """

    def __init__(
        self,
        url: str,
        connect_timeout: float = 15.0,
        frame_timeout: float = 30.0,
        max_message_bytes: int = 128 * 1024 * 1024,
    ):
        self.url = url
        self.connect_timeout = connect_timeout
        self.frame_timeout = frame_timeout
        self.max_message_bytes = max_message_bytes
        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._recv_buf = b""
        self._closed = False

    # -- lifecycle ---------------------------------------------------------- #

    def connect(self) -> None:
        parts = urllib.parse.urlsplit(self.url)
        if parts.scheme not in ("ws", "wss"):
            raise HandshakeFailed(f"unsupported scheme: {parts.scheme!r}")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        resource = parts.path or "/"
        if parts.query:
            resource += "?" + parts.query

        raw = socket.create_connection((host, port), timeout=self.connect_timeout)
        if parts.scheme == "wss":
            ctx = ssl_module.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=host)
        self._sock = raw

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_header = host if port in (80, 443) else f"{host}:{port}"
        request = (
            f"GET {resource} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: trid3nt-qgis-plugin/0.3\r\n"
            "\r\n"
        )
        raw.sendall(request.encode("ascii"))

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = raw.recv(4096)
            if not chunk:
                raise HandshakeFailed("server closed during HTTP upgrade")
            response += chunk
            if len(response) > 65536:
                raise HandshakeFailed("oversized upgrade response")
        head, _, rest = response.partition(b"\r\n\r\n")
        self._recv_buf = rest  # frames may already have arrived
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " 101" not in status_line:
            raise HandshakeFailed(f"upgrade rejected: {status_line}")
        accept_expected = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        accept_got = None
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.decode("latin-1", "replace").partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                accept_got = value.strip()
        if accept_got != accept_expected:
            raise HandshakeFailed("bad Sec-WebSocket-Accept")

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self._sock is None or self._closed:
            return
        self._closed = True
        try:
            payload = struct.pack("!H", code) + reason.encode("utf-8")[:120]
            self._send_frame(_OP_CLOSE, payload)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None

    # -- send --------------------------------------------------------------- #

    def send_text(self, text: str) -> None:
        self._send_frame(_OP_TEXT, text.encode("utf-8"))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise ConnectionClosed(reason="socket not connected")
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            try:
                sock.sendall(header + mask + masked)
            except OSError as exc:
                raise ConnectionClosed(reason=f"send failed: {exc}") from exc

    # -- receive ------------------------------------------------------------ #

    def recv(self, timeout: Optional[float] = None) -> Optional[str]:
        """Receive the next complete TEXT message.

        Returns ``None`` if ``timeout`` expires while waiting for a NEW frame
        (so a caller loop can poll a stop flag). Once a frame header has
        started arriving the read switches to ``frame_timeout`` -- a timeout
        mid-frame is a real transport failure and raises ``ConnectionClosed``.
        """
        fragments: list[bytes] = []
        while True:
            frame = self._recv_frame(timeout if not fragments else self.frame_timeout)
            if frame is None:
                if fragments:
                    raise ConnectionClosed(reason="timeout mid-fragmented-message")
                return None
            fin, opcode, payload = frame
            if opcode == _OP_PING:
                try:
                    self._send_frame(_OP_PONG, payload)
                except ConnectionClosed:
                    pass
                continue
            if opcode == _OP_PONG:
                continue
            if opcode == _OP_CLOSE:
                code: Optional[int] = None
                reason = ""
                if len(payload) >= 2:
                    code = struct.unpack("!H", payload[:2])[0]
                    reason = payload[2:].decode("utf-8", "replace")
                try:
                    self._send_frame(_OP_CLOSE, payload[:2])
                except ConnectionClosed:
                    pass
                self.close()
                raise ConnectionClosed(code=code, reason=reason)
            if opcode == _OP_BINARY:
                # The agent protocol is JSON-text-only; skip binary frames
                # (and their continuations, which carry opcode 0).
                while not fin:
                    nxt = self._recv_frame(self.frame_timeout)
                    if nxt is None:
                        raise ConnectionClosed(reason="timeout in binary continuation")
                    fin = nxt[0]
                continue
            if opcode in (_OP_TEXT, _OP_CONT):
                if opcode == _OP_TEXT and fragments:
                    raise WebSocketError("unexpected new TEXT frame mid-message")
                if opcode == _OP_CONT and not fragments:
                    # Stray continuation (e.g. tail of a skipped message).
                    continue
                fragments.append(payload)
                if sum(len(f) for f in fragments) > self.max_message_bytes:
                    raise WebSocketError("message exceeds max_message_bytes")
                if fin:
                    return b"".join(fragments).decode("utf-8", "replace")
                continue
            # Unknown opcode: skip.
            continue

    def _recv_frame(self, timeout: Optional[float]) -> Optional[tuple[bool, int, bytes]]:
        """Read one frame. None = timeout before the frame started."""
        header = self._read_exact(2, timeout, allow_timeout=True)
        if header is None:
            return None
        b0, b1 = header
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            ext = self._read_exact(2, self.frame_timeout)
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = self._read_exact(8, self.frame_timeout)
            length = struct.unpack("!Q", ext)[0]
        if length > self.max_message_bytes:
            raise WebSocketError(f"frame too large: {length} bytes")
        mask = self._read_exact(4, self.frame_timeout) if masked else b""
        payload = self._read_exact(length, self.frame_timeout) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def _read_exact(
        self,
        n: int,
        timeout: Optional[float],
        allow_timeout: bool = False,
    ) -> Optional[bytes]:
        sock = self._sock
        if sock is None:
            raise ConnectionClosed(reason="socket not connected")
        deadline = None if timeout is None else time.monotonic() + timeout
        while len(self._recv_buf) < n:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if allow_timeout and not self._recv_buf:
                        return None
                    raise ConnectionClosed(reason="read timeout")
                sock.settimeout(remaining)
            else:
                sock.settimeout(None)
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                if allow_timeout and not self._recv_buf:
                    return None
                raise ConnectionClosed(reason="read timeout")
            except OSError as exc:
                raise ConnectionClosed(reason=f"recv failed: {exc}") from exc
            if not chunk:
                raise ConnectionClosed(reason="TCP stream ended")
            self._recv_buf += chunk
        out, self._recv_buf = self._recv_buf[:n], self._recv_buf[n:]
        return out


# --------------------------------------------------------------------------- #
# Agent protocol client
# --------------------------------------------------------------------------- #


@dataclass
class AgentEvent:
    """One dispatched server frame, normalized for the UI bridge.

    ``kind`` values the dock consumes in milestone 1:

      chunk           {"message_id", "delta", "done"}
      pipeline        {"pipeline_id", "steps": [PipelineStep, ...]}
      session-state   {"payload": <raw>, "layers": [LayerEvent, ...]}
      error           raw error payload (error_code, message, ...)
      turn-complete   raw payload
      case-open       raw payload
      payload-warning raw payload (the dock renders the gate card; see gate.py)
      code-exec-request raw payload (the code-exec HARD confirm gate,
                      contracts sandbox_contracts.py -- the dock renders the
                      approval card; the reply rides the EXISTING
                      tool-payload-confirmation with warning_id ==
                      code_exec_id. Live-feedback 2026-07-21)
      case-list       {"cases": [CaseInfo, ...], "payload": <raw>}
      chart           raw ChartEmissionPayload (live chart-emission frame;
                      the dock's Charts panel renders it -- 2026-07-13)
      solve-progress  raw SolveProgressPayload (live big-sim telemetry tick;
                      the dock's SimCard consumes it -- item R4, 2026-07-18)
      tool-io         raw ToolIoPayload (raw-args sidecar keyed by step_id;
                      the dock's tool chips read a short arg summary from it
                      -- item R2, 2026-07-18)
      raw             {"type": <envelope type>, "payload": <raw>}
    """

    kind: str
    data: dict


class AgentClient:
    """Synchronous TRID3NT agent client (handshake + case + chat + events).

    Intended use from the plugin: construct + ``connect()`` + ``create_case``
    on a worker thread, then loop ``next_event`` on that same thread while the
    UI thread calls ``send_chat``/``cancel`` (socket writes are mutex-guarded).
    """

    def __init__(
        self,
        url: str,
        token: Optional[str] = None,
        anonymous_user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        connect_timeout: float = 15.0,
        handshake_timeout: float = 20.0,
    ):
        self.base_url = url
        self.token = token or ""
        self.anonymous_user_id = anonymous_user_id
        self.session_id = session_id or new_ulid()
        self.connect_timeout = connect_timeout
        self.handshake_timeout = handshake_timeout
        self.user_id: Optional[str] = None
        self.is_anonymous: Optional[bool] = None
        self.case_id: Optional[str] = None
        self.last_session_state: Optional[dict] = None
        #: The most recent ``case-list`` observed -- stashed by BOTH the
        #: handshake drain (``_wait_for``; the stub server emits it before
        #: session-state) and the event pump (``next_event``; the live
        #: server emits it after). None until one arrives. The startup
        #: case-reuse decision (``choose_startup_case``) reads this.
        self.last_case_list: Optional[list] = None
        #: The last ``error`` envelope payload seen while draining a handshake
        #: wait (e.g. AUTH_REQUIRED before a 1008 close) -- the bridge folds it
        #: into the failure text so token expiry is classifiable.
        self.last_handshake_error: Optional[dict] = None
        #: True between a completed handshake and the next transport loss.
        self.connected = False
        self._ws: Optional[WebSocketConnection] = None
        # Outbound intent queue (web sendOrQueue): pre-serialized frames
        # buffered while disconnected, flushed FIFO after the resume
        # handshake. Bounded; OLDEST dropped first.
        self._outbound_queue: list[str] = []
        self._queue_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------- #

    @property
    def ws_url(self) -> str:
        return build_ws_url(self.base_url, self.token)

    def connect(self) -> str:
        """Open the socket and run the auth + resume handshake.

        Returns the resolved ``user_id``. Raises ``HandshakeFailed`` /
        ``ConnectionClosed`` on any failure.

        RECONNECT semantics (milestone 2): the SAME ``session_id`` is reused,
        the sticky ``anonymous_user_id`` re-binds the same local User record,
        and ``session-resume`` carries the current ``case_id`` so the server
        RE-BINDS its active-Case pointer and replays that Case's layers
        (contract ``SessionResumePayload.case_id``, job-CASE-AUTHORITY).
        Queued outbound frames are flushed FIFO once the handshake completes.
        """
        self.connected = False
        self.last_handshake_error = None
        if self._ws is not None:
            self._ws.close()
        self._ws = WebSocketConnection(self.ws_url, connect_timeout=self.connect_timeout)
        self._ws.connect()
        self._send(
            "auth-token",
            {"token": self.token, "anonymous_user_id": self.anonymous_user_id},
        )
        ack = self._wait_for("auth-ack")
        payload = ack.get("payload") or {}
        user_id = payload.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise HandshakeFailed(f"auth-ack without user_id: {payload!r}")
        self.user_id = user_id
        self.is_anonymous = bool(payload.get("is_anonymous", not self.token))
        if self.is_anonymous:
            # Sticky-anonymous: replay the server-assigned id on the next
            # connect so the SAME local User record re-binds (web mirror).
            self.anonymous_user_id = user_id

        self._send("session-resume", {"case_id": self.case_id})
        state = self._wait_for("session-state")
        self.last_session_state = state.get("payload") or {}
        # Startup case reuse (live-feedback 2026-07-09): the server stamps
        # the session-state reply's envelope ``case_id`` with the active
        # case the resume rebound (its persisted ``last_active_case_id``).
        # Adopt it when this client has no case yet, so the connect flow can
        # KEEP the persisted case instead of minting a new one. A client
        # that already carries a case (reconnect) keeps its own stamp -- the
        # client is the authority there (job-CASE-AUTHORITY).
        resumed = state.get("case_id")
        if self.case_id is None and isinstance(resumed, str) and resumed:
            self.case_id = resumed
        self.connected = True
        self._flush_outbound_queue()
        return user_id

    def reconnect(self) -> str:
        """Re-dial after a transport loss (same session + case; see
        ``connect``). The caller owns the backoff cadence."""
        return self.connect()

    def close(self) -> None:
        self.connected = False
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    # -- protocol verbs ------------------------------------------------------ #

    def create_case(self, title: str, bbox: Optional[list] = None) -> str:
        """Create a fresh case; returns its case_id.

        ``bbox`` (optional) is the #170 AOI-first extent
        ``[lon_min, lat_min, lon_max, lat_max]`` (EPSG:4326): the agent seeds
        ``CaseSummary.bbox`` + ``state.case_bbox`` from ``args.bbox`` so the
        FIRST turn's ``_turn_case_bbox`` returns the user's extent (exact web
        mirror: useCases.ts createCase includes ``bbox`` only when supplied).
        """
        args: dict = {"title": title}
        if bbox is not None:
            args["bbox"] = list(bbox)
        self._send("case-command", {"command": "create", "args": args})
        deadline = time.monotonic() + self.handshake_timeout
        while True:
            env = self._wait_for("case-open", deadline=deadline)
            payload = env.get("payload") or {}
            session_state = payload.get("session_state") or {}
            case = session_state.get("case") or {}
            case_id = case.get("case_id")
            if isinstance(case_id, str) and case_id:
                self.case_id = case_id
                return case_id
            # A case-open without a case_id (e.g. a null rehydration) --
            # keep draining until the deadline.

    def select_case(self, case_id: str) -> None:
        """Switch the active case (``case-command select``).

        Exact web mirror (ws.ts ``sendCaseCommand``): the local ``case_id``
        stamp updates AT SEND TIME so the very next ``session-resume`` /
        ``user-message`` re-asserts the same case even if a queued select and
        the resume race; the frame itself sendOrQueues (a select tapped
        mid-reconnect must not be silently dropped -- LANE CASE-WEB). The
        server replies with a full ``case-open`` rehydration (CaseSummary +
        loaded_layers + chat history) which arrives through ``next_event``.
        """
        self.case_id = case_id
        self._send(
            "case-command",
            {"command": "select", "case_id": case_id, "args": {}},
            case_id=case_id,
            queue_if_closed=True,
        )

    def case_command(
        self,
        command: str,
        case_id: Optional[str] = None,
        args: Optional[dict] = None,
    ) -> None:
        """Send a generic ``case-command`` (``create`` / ``delete`` /
        ``set-bbox`` / ...) WITHOUT blocking on the reply -- unlike
        ``create_case`` (used only during the initial connect handshake), the
        reply here flows through the normal ``next_event`` pump like
        ``select_case``'s does (a ``create`` reply arrives as a ``case-open``
        the dock rebinds on; a ``delete`` reply arrives as a fresh
        ``case-list``).

        ``args`` (per-case-bbox 2026-07-19): the free-form
        ``CaseCommandEnvelopePayload.args`` slot -- ``set-bbox`` rides its
        edited AOI here as ``{"bbox": [w, s, e, n]}`` (EPSG:4326), the same
        carrier ``create`` already uses for ``title`` / ``bbox``. Defaults to
        an empty dict so every existing caller (create with no args, delete)
        is byte-identical to before.

        Mirrors ``select_case``'s envelope shape and queue-if-closed
        behaviour: a New/Delete/set-bbox tapped mid-reconnect must not be
        silently dropped.
        """
        payload: dict = {"command": command, "args": dict(args) if args else {}}
        if case_id is not None:
            payload["case_id"] = case_id
        self._send(
            "case-command", payload, case_id=case_id, queue_if_closed=True
        )

    def request_case_list_refresh(self) -> bool:
        """Refresh the case list via one ``session-resume`` round trip.

        DOCUMENTED TRADEOFF (milestone 3 item 3): the protocol has NO
        dedicated list-cases request verb -- ``case-list`` only ever arrives
        as a server emission. The server's session-resume handler replies
        with ``session-state`` + ``case-list`` and is ALREADY the web
        client's ~25s keepalive (the server logs it at DEBUG), so a resume
        round trip IS the cheap refresh. Cost: a redundant ``session-state``
        frame rides along -- harmless, layer materialization dedups by
        layer_id. Returns False when disconnected (nothing to ask; the
        reconnect resume will refresh anyway). The caller debounces.
        """
        if not self.connected:
            return False
        self._send("session-resume", {"case_id": self.case_id})
        return True

    def send_chat(
        self, text: str, show_thinking: bool = False, model_id: str = ""
    ) -> None:
        """Send a user chat message.

        :param text: The message text.
        :param show_thinking: F9 (live-feedback 2026-07-09) - when True, ride
            ``show_thinking=True`` on the payload so the local model's reasoning
            channel is forwarded as ``agent-thinking-chunk`` envelopes. Only
            meaningful locally; cloud agents ignore the field.
        :param model_id: OpenRouter model-extensibility (design 2026-07-19) -
            when truthy, ride ``model_id`` on the payload so the server's
            ``resolve_selected_model`` picks THIS model for the turn (any
            openai/OpenRouter model id passes verbatim). Empty = the agent's
            env default (``GRACE2_OPENAI_MODEL``). Mirrors the ``show_thinking``
            add exactly: a LIVE per-turn switch, no agent restart. (Provider
            base_url/api_key are agent-process env, NOT sent here.)
        """
        payload: dict = {"text": text, "case_id": self.case_id}
        if show_thinking:
            payload["show_thinking"] = True
        if model_id:
            payload["model_id"] = model_id
        self._send(
            "user-message",
            payload,
            case_id=self.case_id,
            queue_if_closed=True,
        )

    def cancel(self, reason: str = "user-cancel") -> None:
        self._send(
            "cancel", {"reason": reason}, case_id=self.case_id, queue_if_closed=True
        )

    def confirm_payload(
        self,
        warning_id: str,
        decision: str = "proceed",
        revised_args: Optional[dict] = None,
    ) -> None:
        """Answer a ``tool-payload-warning`` gate (milestone 2 gate card).

        Contract cross-rule (``PayloadConfirmationEnvelopePayload``):
        ``narrow_scope`` REQUIRES a ``revised_args`` dict (may be empty);
        ``proceed`` / ``cancel`` MUST send ``revised_args = None``. Enforced
        here so a UI slip can never emit an envelope the agent rejects.
        """
        if decision == "narrow_scope":
            revised: Optional[dict] = revised_args if isinstance(revised_args, dict) else {}
        else:
            revised = None
        self._send(
            "tool-payload-confirmation",
            {"warning_id": warning_id, "decision": decision, "revised_args": revised},
            queue_if_closed=True,
        )

    # -- event pump ---------------------------------------------------------- #

    def next_event(self, timeout: float = 1.0) -> Optional[AgentEvent]:
        """Receive + normalize one server frame; None on timeout.

        Raises ``ConnectionClosed`` when the socket dies -- the caller owns
        reconnect policy (milestone 1: surface as disconnected, no auto
        reconnect loop).
        """
        raw = self._recv(timeout)
        if raw is None:
            return None
        try:
            env = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return AgentEvent("raw", {"type": "non-json", "payload": {"text": raw[:200]}})
        if not isinstance(env, dict):
            return AgentEvent("raw", {"type": "non-object", "payload": {}})
        etype = env.get("type") or ""
        payload = env.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        if etype == "agent-message-chunk":
            return AgentEvent(
                "chunk",
                {
                    "message_id": payload.get("message_id"),
                    "delta": payload.get("delta") or payload.get("text") or "",
                    "done": bool(payload.get("done")),
                },
            )
        if etype == "agent-thinking-chunk":
            # F9 (live-feedback 2026-07-09): local model reasoning-channel
            # tokens. Same payload shape as agent-message-chunk. Keyed by the
            # same message_id as the subsequent answer chunk so the dock can
            # attach the thinking block to the right assistant entry.
            return AgentEvent(
                "thinking-chunk",
                {
                    "message_id": payload.get("message_id"),
                    "delta": payload.get("delta") or payload.get("text") or "",
                    "done": bool(payload.get("done")),
                },
            )
        if etype == "pipeline-state":
            return AgentEvent(
                "pipeline",
                {
                    "pipeline_id": payload.get("pipeline_id"),
                    "steps": parse_pipeline_steps(payload),
                },
            )
        if etype == "session-state":
            self.last_session_state = payload
            return AgentEvent(
                "session-state",
                {"payload": payload, "layers": parse_layer_events(payload)},
            )
        if etype == "error":
            return AgentEvent("error", payload)
        if etype == "turn-complete":
            return AgentEvent("turn-complete", payload)
        if etype == "case-open":
            # F34 (live-proven 2026-07-10): adopt the server's authoritative
            # rebind into the wire stamp. select_case stamps at send time,
            # but the generic case_command("create") path did NOT - so every
            # envelope after a New-case rebind (including user-message) kept
            # carrying the PREVIOUS case_id and the turn ran/persisted into
            # the wrong case. The case-open reply is the one signal every
            # rebind path shares (create, select, startup reuse), so the
            # stamp follows it unconditionally.
            opened = ((payload.get("session_state") or {}).get("case") or {}).get(
                "case_id"
            )
            if isinstance(opened, str) and opened:
                self.case_id = opened
            return AgentEvent("case-open", payload)
        if etype == "tool-payload-warning":
            return AgentEvent("payload-warning", payload)
        if etype == "code-exec-request":
            # Code-exec approval gate (live-feedback 2026-07-21): the agent
            # emits this BEFORE running sandbox Python and BLOCKS on its
            # confirm seam until a ``tool-payload-confirmation`` whose
            # ``warning_id == code_exec_id`` arrives (contracts
            # sandbox_contracts.py / server._gate_on_code_exec). Previously
            # fell through to "raw" and was dropped by the dock -- the agent
            # then waited forever and the turn "just stopped". The dock now
            # renders the approval card (ui/cards.CodeExecCard).
            return AgentEvent("code-exec-request", payload)
        if etype == "chart-emission":
            # OpenQuake result parity (live-feedback 2026-07-13): a live
            # mid-turn chart (ChartEmissionPayload -- Vega-Lite spec + title
            # + caption). Previously fell through to "raw" and was dropped
            # by the dock; the persisted replay twin rides in the case-open
            # ``session_state.charts`` (``parse_charts``).
            return AgentEvent("chart", payload)
        if etype == "solve-progress":
            # Item R4 (live-feedback 2026-07-18): live big-sim telemetry tick
            # (contract ws.SolveProgressPayload -- run_id / solver /
            # grid_resolution_m / active_cell_count / vcpus / elapsed_seconds
            # / eta_seconds / phase, emitted every ~10 s by
            # workflows.solve_progress.drive_live_solve_progress). Previously
            # fell through to "raw" and was dropped; the dock's SimCard now
            # consumes it.
            return AgentEvent("solve-progress", payload)
        if etype == "tool-io":
            # Item R2 (live-feedback 2026-07-18): the raw tool-args sidecar
            # keyed by pipeline step_id (contract ws.ToolIoPayload; the server
            # emits an input-only frame at dispatch START, then the full one
            # on completion). The dock's tool chip rows render a short arg
            # summary from ``raw_args``.
            return AgentEvent("tool-io", payload)
        if etype == "case-list":
            cases = parse_case_list(payload)
            # Mirror of the last_session_state stash above: the startup
            # case-reuse decision reads the freshest list either way.
            self.last_case_list = cases
            return AgentEvent("case-list", {"cases": cases, "payload": payload})
        return AgentEvent("raw", {"type": etype, "payload": payload})

    def run_forever(
        self,
        on_event: Callable[[AgentEvent], None],
        should_stop: Callable[[], bool],
        poll_seconds: float = 1.0,
    ) -> None:
        """Pump events to ``on_event`` until ``should_stop()`` or the socket
        closes (``ConnectionClosed`` propagates to the caller)."""
        while not should_stop():
            event = self.next_event(timeout=poll_seconds)
            if event is not None:
                on_event(event)

    # -- internals ------------------------------------------------------------ #

    def _send(
        self,
        type_: str,
        payload: dict,
        case_id: Optional[str] = None,
        queue_if_closed: bool = False,
    ) -> None:
        """Send an envelope, or buffer it when disconnected.

        ``queue_if_closed`` mirrors the web's ``sendOrQueue``: user-intent
        verbs (chat / cancel / gate confirmations) issued while the socket is
        down are pre-serialized and buffered (bounded ``OUTBOUND_QUEUE_MAX``,
        OLDEST dropped first) instead of raising, then flushed FIFO after the
        next resume handshake. Handshake verbs keep the raise-on-closed
        behaviour -- queueing an auth-token would be nonsense.
        """
        env = make_envelope(type_, self.session_id, payload, case_id=case_id)
        raw = json.dumps(env)
        if queue_if_closed and (not self.connected or self._ws is None):
            self._enqueue(raw)
            return
        if self._ws is None:
            raise ConnectionClosed(reason="not connected")
        try:
            self._ws.send_text(raw)
        except ConnectionClosed:
            self.connected = False
            if queue_if_closed:
                # The transport died under the send: keep the user's intent.
                self._enqueue(raw)
                return
            raise

    def _enqueue(self, raw: str) -> None:
        with self._queue_lock:
            self._outbound_queue.append(raw)
            if len(self._outbound_queue) > OUTBOUND_QUEUE_MAX:
                del self._outbound_queue[: len(self._outbound_queue) - OUTBOUND_QUEUE_MAX]

    def _flush_outbound_queue(self) -> None:
        """FIFO-flush buffered intent frames after a resume handshake. If the
        socket dies mid-flush the unsent remainder is re-buffered (frame
        included) and the failure propagates to the reconnect loop."""
        with self._queue_lock:
            pending, self._outbound_queue = self._outbound_queue, []
        for i, raw in enumerate(pending):
            try:
                if self._ws is None:
                    raise ConnectionClosed(reason="not connected")
                self._ws.send_text(raw)
            except ConnectionClosed:
                self.connected = False
                with self._queue_lock:
                    self._outbound_queue = pending[i:] + self._outbound_queue
                raise

    @property
    def queued_outbound(self) -> int:
        with self._queue_lock:
            return len(self._outbound_queue)

    def _recv(self, timeout: float) -> Optional[str]:
        if self._ws is None:
            raise ConnectionClosed(reason="not connected")
        try:
            return self._ws.recv(timeout=timeout)
        except ConnectionClosed:
            self.connected = False
            raise

    def _wait_for(self, etype: str, deadline: Optional[float] = None) -> dict:
        """Drain frames until one of ``etype`` arrives (handshake helper).

        Non-matching frames are dropped, mirroring the reference driver
        (tool_routing_bench.do_handshake / create_case) -- EXCEPT ``error``
        envelopes, whose payload is stashed on ``last_handshake_error`` so a
        rejection that closes the socket (AUTH_REQUIRED then 1008) stays
        classifiable after the ``ConnectionClosed`` surfaces.
        """
        if deadline is None:
            deadline = time.monotonic() + self.handshake_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HandshakeFailed(f"timed out waiting for {etype!r}")
            raw = self._recv(timeout=min(remaining, 5.0))
            if raw is None:
                continue
            try:
                env = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(env, dict):
                continue
            if env.get("type") == "error" and isinstance(env.get("payload"), dict):
                self.last_handshake_error = env["payload"]
            # A ``case-list`` drained during the handshake wait (the stub
            # server interleaves it BEFORE session-state) would otherwise be
            # dropped -- stash it for the startup case-reuse decision.
            if env.get("type") == "case-list" and isinstance(env.get("payload"), dict):
                self.last_case_list = parse_case_list(env["payload"])
            if env.get("type") == etype:
                return env
