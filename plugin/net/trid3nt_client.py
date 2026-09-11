"""TRID3NT agent WebSocket client -- pure Python, stdlib only.

No PyQGIS, no PyQt and no third-party package: QGIS's bundled Python ships no
WebSocket library, so this module carries its own RFC 6455 client. ``send_text``
is mutex-guarded; everything else is single-consumer (one reader thread)."""

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
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

# Numeric-finiteness helpers shared with the render path, so the WS-boundary
# sanitizer and the native styling clamps agree on what "a real number" is.
from ..render import formatting

_LOG = logging.getLogger("trid3nt.trid3nt_client")

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
    "s3_to_vsis3",
    "utc_ts",
    "DEFAULT_HTTP_PORT",
    "derive_http_base",
    "resolve_http_base",
    "resolve_data_base",
]

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """26-char Crockford-base32 ULID (48-bit ms timestamp + 80-bit random)."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> (5 * (25 - i))) & 0x1F] for i in range(26))


def utc_ts() -> str:
    """ISO-8601 UTC timestamp with a literal Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_envelope(
    type_: str,
    session_id: str,
    payload: dict,
    case_id: Optional[str] = None,
) -> dict:
    """Build a wire envelope dict."""
    return {
        "type": type_,
        "id": new_ulid(),
        "ts": utc_ts(),
        "session_id": session_id,
        "case_id": case_id,
        "payload": payload,
    }


def build_ws_url(base_url: str, token: Optional[str] = None) -> str:
    """Append the ``?st=<token>`` carrier a broker authenticates on before the
    WebSocket upgrade completes. No-op when ``token`` is falsy."""
    if not token:
        return base_url
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}st={urllib.parse.quote(token, safe='')}"




@dataclass
class LayerEvent:
    """One row of ``session-state.loaded_layers`` (ProjectLayerSummary +
    the additive ``inline_geojson`` the local agent merges in)."""

    layer_id: str
    name: str
    layer_type: str  # "raster" | "vector" | ...
    uri: str
    inline_geojson: Optional[dict] = None
    opacity: Optional[float] = None
    visible: bool = True
    legend: Optional[dict] = None
    raw: dict = field(default_factory=dict)


#: Opacity is the one numeric style field that rides from a wire row into a
#: NATIVE QGIS call (``setOpacity``), so a non-finite value is stripped here at
#: the boundary rather than trusted downstream (the arm64 double-to-int32
#: saturation that smashes the stack). The legend's own numbers never reach Qt
#: from this side: the map loads the ``.qml`` the row carries, and the server
#: resolved the range that document states.


def parse_layer_events(session_state_payload: dict) -> list[LayerEvent]:
    """Parse ``session-state.loaded_layers`` rows into ``LayerEvent``s. A
    malformed row is skipped and a non-finite opacity is dropped -- logged once
    per row, never silently swallowed -- so neither reaches the map."""
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
        dropped: list = []
        raw_opacity = row.get("opacity")
        opacity = raw_opacity if formatting.is_finite_number(raw_opacity) else None
        if raw_opacity is not None and opacity is None:
            dropped.append(f"opacity={raw_opacity!r}")
        legend = row.get("legend") if isinstance(row.get("legend"), dict) else None
        if dropped:
            _LOG.warning(
                "layer %s: dropped non-finite style value(s) at WS boundary: %s",
                layer_id,
                ", ".join(dropped),
            )
        events.append(
            LayerEvent(
                layer_id=layer_id,
                name=str(row.get("name") or layer_id),
                layer_type=str(row.get("layer_type") or "raster"),
                uri=uri if isinstance(uri, str) else "",
                inline_geojson=inline,
                opacity=opacity,
                visible=bool(row.get("visible", True)),
                legend=legend,
                raw=row,
            )
        )
    return events


@dataclass
class PipelineStep:
    """The subset of a pipeline step the dock renders."""

    step_id: str
    name: str
    tool_name: str
    state: str  # pending | running | complete | failed | cancelled
    parent_step_id: Optional[str] = None
    substep_label: Optional[str] = None
    error_message: Optional[str] = None
    # ``role`` discriminates the off-box solver card ("compute", tool_name
    # "<solver>:solve") from a plain tool card ("tool"); the dock routes a
    # compute step to its collapsible sim card instead of a grey row.
    # ``batch_job_id`` is "local-docker:<run_id>" on the local seam;
    # ``batch_status`` mirrors the control plane verbatim; ``duration_ms`` is
    # stamped on the terminal transition only.
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
                # Sim-card fields: all optional on the wire, all
                # default-preserving here.
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
    """Decide which case a fresh connect binds: ``("resume" | "select" |
    "create", case_id_or_None)``. PURE -- no sockets, no Qt."""
    # The ladder: a resumed persisted case wins; else the NEWEST live case
    # (``updated_at`` descending, ISO-8601 Z sorting lexicographically) with
    # tombstoned and malformed rows skipped; else create, the last resort.
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
    A genuine failure RAISES rather than returning ``[]``, so an unreachable
    agent never reads as "no cases exist"; bad rows are still skipped."""
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


# Provider-config POST + live model-list GET, both against the local agent's
# HTTP listener.


class ProviderConfigRequestError(Exception):
    """``post_provider_config`` failed -- transport, HTTP status, or a non-JSON
    body. Carries an honest, user-facing message that NEVER contains the api
    key."""


def post_provider_config(base_url: str, payload: dict, timeout: float = 5.0) -> dict:
    """``POST {base_url}/api/provider-config`` with any subset of ``{base_url,
    api_key, model, num_ctx}`` -> the agent's result dict. SECURITY: the api
    key rides the body, is never logged, and never echoes in a raised message."""
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
    """``GET {base_url}/api/local-models`` -> ``(model_ids, default)``. The
    list is a convenience dropdown, never a whitelist; any fault RAISES so the
    caller can fall back to its static shortlist."""
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




#: Cap on chat-history replay rows: a Case that has chatted for hours must not
#: stall the dock repainting hundreds of bubbles.
CHAT_HISTORY_REPLAY_MAX = 50


def parse_chat_history(session_state_payload: dict) -> list:
    """``session_state.chat_history`` rows -> plain replay dicts. ``user``,
    ``agent`` and ``tool`` rows survive and ``system`` rows are dropped; a bad
    row is skipped, and the newest ``CHAT_HISTORY_REPLAY_MAX`` are kept."""
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
            # The typed tool_card dict is the preferred render source; the
            # content JSON twin is the fallback. Skip only when NEITHER is
            # usable.
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
        if role == "agent":
            # Persisted reasoning, so the dock can replay the collapsible
            # thinking fold. Absent, non-string or blank -> an honest None.
            thinking = row.get("thinking")
            if not isinstance(thinking, str) or not thinking.strip():
                thinking = None
            out.append({"role": role, "content": content, "thinking": thinking})
        else:
            out.append({"role": role, "content": content})
    return out[-CHAT_HISTORY_REPLAY_MAX:]


def parse_charts(session_state_payload: dict) -> list:
    """``session_state.charts`` rows -> the persisted chart payloads, in
    order (oldest first). A row without a usable ``chart_id`` and Vega-Lite
    spec is skipped, never raised on."""
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
    """Everything a ``case-open`` envelope rehydrates: the case, its layers,
    its chat and its charts. ``bbox`` is EPSG:4326 ``[lon_min, lat_min,
    lon_max, lat_max]`` and may be absent on a case that never carried one."""

    case_id: str
    title: str
    layers: list = field(default_factory=list)  # list[LayerEvent]
    bbox: Optional[Tuple[float, float, float, float]] = None
    chat_messages: list = field(default_factory=list)  # list[{"role","content"}]
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
    """Scan a ``case-open`` payload for a bbox in any carrier, checked in
    order: ``payload.bbox``, ``session_state.bbox``, ``session_state.case.
    bbox``. None when nothing usable is found; never raises."""
    # Today's wire shape carries exactly ONE bbox field, so this normally
    # re-finds the same value the case row already gave; it stands so a new
    # server-side carrier is picked up without another client change.
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
    """Parse a ``case-open`` payload into a ``CaseOpenInfo``. None means the
    server could not rehydrate and the caller falls back to the empty state;
    a malformed field degrades to None rather than raising."""
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




def is_auth_failure(text: str) -> bool:
    """True for a REJECTED TOKEN, false for a transport failure. The
    distinction decides policy: a transport failure drives the reconnect
    ladder, an auth failure must stop it rather than loop on a dead token."""
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



#: Minimum seconds between case-list refresh round trips: session-resume is
#: cheap, but a click-happy user must not be able to queue a resume storm.
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



#: Backoff FLOOR (ms): the first reconnect after a drop waits at least this
#: long, which is what keeps a drop from becoming a reconnect storm.
RECONNECT_FLOOR_MS = 1500
#: Backoff CEILING (ms): the doubling ladder caps here.
RECONNECT_MAX_MS = 5000

#: Outbound-queue bound: beyond this the OLDEST frames are dropped first, so
#: the most recent intent is the intent that survives.
OUTBOUND_QUEUE_MAX = 50


def next_backoff(
    base_ms: int,
    rng: Callable[[], float] = random.random,
) -> tuple[int, int]:
    """One rung of the capped-jitter reconnect ladder -> ``(delay_ms,
    next_base_ms)``. The wait jitters within ``[0.5, 1.0) * base``; the caller
    resets the base to ``RECONNECT_FLOOR_MS`` after a successful open."""
    base = max(int(base_ms), 1)
    jitter_factor = 0.5 + 0.5 * rng()
    delay = int(round(base * jitter_factor))
    return delay, min(base * 2, RECONNECT_MAX_MS)




def s3_to_vsis3(uri: str) -> Optional[str]:
    """``s3://bucket/key`` -> the ``/vsis3/bucket/key`` path GDAL reads. The
    endpoint and credentials are GDAL CONFIGURATION, so no host appears here;
    anything that is not an ``s3://`` object uri returns None."""
    if not uri.startswith("s3://"):
        return None
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        return None
    return f"/vsis3/{bucket}/{key}"


#: The local agent's HTTP listener port (tool catalog + /api/* routes). A
#: daemon that advertises no endpoint still binds it, so it is the constant
#: the fallback derivation uses.
DEFAULT_HTTP_PORT = 8766


def derive_http_base(ws_url: str, port: int = DEFAULT_HTTP_PORT) -> str:
    """Fallback HTTP base derived from the WS URL's HOST, so ONE "Server URL"
    setting reaches the same peer's :8766 listener: ``ws://<host>:8765/ws`` ->
    ``http://<host>:8766``, ``wss://<host>/ws`` -> ``https://<host>:8766``."""
    parts = urllib.parse.urlsplit((ws_url or "").strip())
    scheme = "https" if parts.scheme == "wss" else "http"
    host = parts.hostname or "127.0.0.1"
    return f"{scheme}://{host}:{port}"


def resolve_http_base(advertised: Optional[str], ws_url: str) -> str:
    """The effective HTTP base: the server-advertised ``http_base`` when
    present, else the WS-host derivation. EVERY :8766 caller resolves through
    this one function, so they cannot drift out of sync."""
    if advertised:
        return advertised.rstrip("/")
    return derive_http_base(ws_url)


def resolve_data_base(advertised: Optional[str], fallback: str) -> str:
    """The object store's endpoint: the server-advertised ``data_base`` when
    present, else ``fallback``. There is no derivable port here, so the
    caller's own setting is the only honest fallback."""
    if advertised:
        return advertised.rstrip("/")
    return fallback


def qgis_xyz_uri(template: str, zmin: int = 0, zmax: int = 24) -> str:
    """Build the QGIS ``wms`` provider uri for an XYZ tile TEMPLATE."""
    # Encode as LITTLE as possible: QGIS does NOT percent-decode the ``url``
    # component, so a fully-quoted template yields a layer that reports valid
    # yet never issues a tile request. Only the template's own query-string
    # ampersands are escaped, so the provider's ``&``-splitting of uri
    # parameters cannot eat them; scheme, slashes, ``?``, ``=`` and the
    # ``{z}/{x}/{y}`` placeholders stay literal, and an already-encoded query
    # value passes through verbatim exactly as the tile server expects.
    return (
        f"type=xyz&url={template.replace('&', '%26')}"
        f"&zmin={zmin}&zmax={zmax}"
    )




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
    ONE reader thread, but ``send_text`` and ``close`` are mutex-guarded so any
    thread may write. Fragments are joined, pings answered, binary ignored."""

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
        """Receive the next complete TEXT message. ``None`` means ``timeout``
        expired before a NEW frame started, so a caller loop can poll a stop
        flag; a timeout MID-frame is a transport failure and raises."""
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




@dataclass
class AgentEvent:
    """One dispatched server frame, normalized for the UI bridge. ``kind`` is
    the dock's dispatch key and ``next_event`` is the authority on the set;
    ``"raw"`` carries anything this client does not name."""

    kind: str
    data: dict


class AgentClient:
    """Synchronous agent client: handshake, case, chat and the event pump.
    Connect and pump on ONE worker thread; a UI thread may call the outbound
    verbs concurrently, because socket writes are mutex-guarded."""

    def __init__(
        self,
        url: str,
        token: Optional[str] = None,
        session_id: Optional[str] = None,
        connect_timeout: float = 15.0,
        handshake_timeout: float = 20.0,
    ):
        self.base_url = url
        self.token = token or ""
        self.session_id = session_id or new_ulid()
        self.connect_timeout = connect_timeout
        self.handshake_timeout = handshake_timeout
        self.user_id: Optional[str] = None
        self.is_anonymous: Optional[bool] = None
        self.case_id: Optional[str] = None
        self.last_session_state: Optional[dict] = None
        #: The most recent ``case-list`` observed, stashed by BOTH the
        #: handshake drain and the event pump because a server may emit it
        #: either side of session-state. None until one arrives.
        self.last_case_list: Optional[list] = None
        #: The last ``error`` envelope payload seen while draining a handshake
        #: wait (AUTH_REQUIRED before a 1008 close, say). It is folded into the
        #: failure text so a token rejection stays classifiable.
        self.last_handshake_error: Optional[dict] = None
        #: Server-advertised endpoint bases from the last ``auth-ack``. When
        #: present they are the ONLY source of truth for the agent's HTTP base
        #: and the store's endpoint; ``None`` means the caller derives a
        #: fallback instead.
        self.advertised_http_base: Optional[str] = None
        self.advertised_data_base: Optional[str] = None
        #: True between a completed handshake and the next transport loss.
        self.connected = False
        #: Optional credential broker. When set, connect pushes every stored
        #: credential over ``secret-add`` and a prompt-answered key is stored
        #: back for the next connect. None where there is no auth home.
        self.credential_broker = None
        self._ws: Optional[WebSocketConnection] = None
        # Outbound intent queue: pre-serialized frames buffered while
        # disconnected, flushed FIFO after the resume handshake. Bounded,
        # OLDEST dropped first.
        self._outbound_queue: list[str] = []
        self._queue_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------- #

    @property
    def ws_url(self) -> str:
        return build_ws_url(self.base_url, self.token)

    def connect(self) -> str:
        """Open the socket and run the auth + resume handshake -> the resolved
        ``user_id``. Re-callable: the SAME ``session_id`` is reused, so a
        re-dial resumes the session rather than starting a new one."""
        self.connected = False
        self.last_handshake_error = None
        if self._ws is not None:
            self._ws.close()
        self._ws = WebSocketConnection(self.ws_url, connect_timeout=self.connect_timeout)
        self._ws.connect()
        self._send("auth-token", {"token": self.token})
        ack = self._wait_for("auth-ack")
        payload = ack.get("payload") or {}
        user_id = payload.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise HandshakeFailed(f"auth-ack without user_id: {payload!r}")
        self.user_id = user_id
        self.is_anonymous = bool(payload.get("is_anonymous", not self.token))
        # Endpoint advertisement is OPTIONAL and arrives in either of two
        # shapes -- flat on the payload, or nested under ``endpoints`` -- so
        # both are read defensively; absence just means the caller falls back.
        # The store endpoint is GDAL CONFIGURATION, never part of a layer uri:
        # a layer is always ``s3://bucket/key`` and the endpoint decides which
        # host serves it.
        endpoints = payload.get("endpoints")
        if not isinstance(endpoints, dict):
            endpoints = {}
        raw_http_base = endpoints.get("http_base") or payload.get("http_base")
        raw_data_base = endpoints.get("data_base") or payload.get("data_base")
        self.advertised_http_base = (
            raw_http_base.rstrip("/")
            if isinstance(raw_http_base, str) and raw_http_base
            else None
        )
        self.advertised_data_base = (
            raw_data_base.rstrip("/")
            if isinstance(raw_data_base, str) and raw_data_base
            else None
        )
        self._send("session-resume", {"case_id": self.case_id})
        state = self._wait_for("session-state")
        self.last_session_state = state.get("payload") or {}
        # The session-state reply's envelope ``case_id`` is the active case
        # the resume rebound. Adopt it ONLY when this client has no case yet,
        # so a fresh connect keeps the persisted case instead of minting one;
        # a client that already carries a case is the authority on its own.
        resumed = state.get("case_id")
        if self.case_id is None and isinstance(resumed, str) and resumed:
            self.case_id = resumed
        self.connected = True
        self._flush_outbound_queue()
        self._broker_push_on_connect()
        return user_id

    def _broker_push_on_connect(self) -> None:
        """Push every stored credential over ``secret-add``. Best-effort: no
        broker, an empty store or a locked auth DB is a silent no-op, because
        connect must never block on the credential home."""
        broker = self.credential_broker
        if broker is None:
            return
        try:
            broker.push_all(self.push_secret)
        except Exception:  # noqa: BLE001 -- connect-time push must not fail connect
            pass

    def reconnect(self) -> str:
        """Re-dial after a transport loss, same session and case. The caller
        owns the backoff cadence."""
        return self.connect()

    def close(self) -> None:
        self.connected = False
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    # -- protocol verbs ------------------------------------------------------ #

    def create_case(self, title: str, bbox: Optional[list] = None) -> str:
        """Create a fresh case; returns its case_id. BLOCKS until the
        ``case-open`` reply. An optional ``bbox`` (EPSG:4326) seeds the case
        extent, so the very FIRST turn already has the user's AOI."""
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
        """Switch the active case. Does NOT block: the ``case-open``
        rehydration arrives through ``next_event``."""
        # The local stamp updates AT SEND TIME, so the next resume or message
        # re-asserts the same case even if a queued select and a resume race.
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
        ``set-bbox``) WITHOUT blocking; the reply flows through
        ``next_event``. ``args`` is the command's own free-form slot."""
        payload: dict = {"command": command, "args": dict(args) if args else {}}
        if case_id is not None:
            payload["case_id"] = case_id
        self._send(
            "case-command", payload, case_id=case_id, queue_if_closed=True
        )

    def request_case_list_refresh(self) -> bool:
        """Refresh the case list; False when disconnected, because there is
        then nothing to ask and the reconnect resume refreshes anyway. The
        caller debounces."""
        # The protocol has NO list-cases verb: ``case-list`` only ever arrives
        # as a server emission, and the session-resume reply carries one. The
        # redundant ``session-state`` that rides along is harmless, since
        # layer materialization dedups by layer_id.
        if not self.connected:
            return False
        self._send("session-resume", {"case_id": self.case_id})
        return True

    def send_chat(
        self,
        text: str,
        show_thinking: bool = False,
        model_id: str = "",
        aoi_bbox: Optional[Tuple[float, float, float, float]] = None,
        tool_choice_mode: str = "",
        drawn_geometry: Optional[dict] = None,
    ) -> None:
        """Send a user chat message. ``text`` is CLEAN user prose: nothing
        else rides inside it. Every optional field is OMITTED when falsy
        rather than sent null, so a plain message stays byte-identical."""
        payload: dict = {"text": text, "case_id": self.case_id}
        if show_thinking:
            payload["show_thinking"] = True
        if model_id:
            payload["model_id"] = model_id
        if aoi_bbox is not None:
            payload["aoi_bbox"] = [float(v) for v in aoi_bbox]
        if tool_choice_mode == "ask":
            payload["tool_choice_mode"] = "ask"
        # A drawn region rides as ``{"geometry_type": ..., "bbox": [4 floats]}``
        # in EPSG:4326, exactly as ``aoi_bbox`` carries the canvas AOI.
        if drawn_geometry is not None:
            payload["drawn_geometry"] = drawn_geometry
        self._send(
            "user-message",
            payload,
            case_id=self.case_id,
            queue_if_closed=True,
        )

    def send_dev_tool_invoke(
        self, name: str, args: dict, raw_text: str = ""
    ) -> None:
        """Send a ``!run`` direct tool invocation: the named tool runs OUTSIDE
        the LLM loop through the same emission pipeline. ``raw_text`` is the
        original composer line, persisted as the turn's user row."""
        payload: dict = {"name": name, "args": args, "case_id": self.case_id}
        if raw_text:
            payload["raw_text"] = raw_text
        self._send(
            "dev-tool-invoke",
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
        """Answer a ``tool-payload-warning`` gate. ``narrow_scope`` REQUIRES a
        ``revised_args`` dict (possibly empty) while ``proceed`` and ``cancel``
        forbid one; enforced here, so a UI slip cannot emit a rejected shape."""
        if decision == "narrow_scope":
            revised: Optional[dict] = revised_args if isinstance(revised_args, dict) else {}
        else:
            revised = None
        self._send(
            "tool-payload-confirmation",
            {"warning_id": warning_id, "decision": decision, "revised_args": revised},
            queue_if_closed=True,
        )

    def push_secret(self, provider_id: str, key_value: str) -> None:
        """Push one credential VALUE over ``secret-add``, with no reply signal
        and no ``credential-provided``: there is no paused tool to resume. KEY
        HYGIENE: the value rides this envelope only and is never logged."""
        self._send(
            "secret-add",
            {
                "provider": provider_id,
                "case_id": self.case_id,
                "key_value": key_value,
            },
            queue_if_closed=True,
        )

    def submit_credential(
        self, request_id: str, provider_id: str, key_value: str
    ) -> None:
        """Answer a ``credential-request`` with the user's key. KEY HYGIENE:
        the raw key rides the ``secret-add`` envelope ALONE and is never
        logged or stored on ``self``."""
        # ORDER matters and is safe on one socket, because the server consumes
        # envelopes sequentially per connection: the cache write completes
        # before the paused tool's future resolves and re-resolves the key.
        # The retry signal that follows carries NO key material, and
        # ``secret_id`` goes out None because the server re-resolves the
        # credential itself rather than handing back a id to wait for.
        broker = self.credential_broker
        if broker is not None:
            try:
                broker.remember(provider_id, key_value)
            except Exception:  # noqa: BLE001 -- store is best-effort; push still runs
                pass
        self._send(
            "secret-add",
            {
                "provider": provider_id,
                "case_id": self.case_id,
                "key_value": key_value,
            },
            queue_if_closed=True,
        )
        self._send(
            "credential-provided",
            {"request_id": request_id, "secret_id": None, "provided": True},
            queue_if_closed=True,
        )

    def decline_credential(self, request_id: str) -> None:
        """Decline a ``credential-request``: ``provided=False`` and NO
        preceding ``secret-add``. The gate still CLOSES -- the paused tool
        resumes into its original typed auth error, never a dead end."""
        self._send(
            "credential-provided",
            {"request_id": request_id, "secret_id": None, "provided": False},
            queue_if_closed=True,
        )

    def send_tool_choice(
        self,
        request_id: str,
        tool_name: Optional[str] = None,
        free_text: Optional[str] = None,
    ) -> None:
        """Answer a ``tool-candidates`` picker. Exactly one of three shapes:
        ``tool_name`` echoed VERBATIM, ``free_text`` guidance, or both None,
        which tells the server to proceed with its own top pick."""
        self._send(
            "tool-choice",
            {
                "request_id": request_id,
                "tool_name": tool_name,
                "free_text": free_text,
            },
            queue_if_closed=True,
        )

    def send_region_choice(
        self,
        request_id: str,
        choice: str,
        selected_region_id: Optional[str] = None,
        selected_bbox: Optional[list] = None,
    ) -> None:
        """Answer a ``region-choice-request`` gate. ``choice`` is ``"region"``
        or ``"whole_state"``; on a region pick the server re-resolves the bbox
        from ``selected_region_id``, which outranks any bbox sent here."""
        self._send(
            "region-choice-provided",
            {
                "envelope_type": "region-choice-provided",
                "request_id": request_id,
                "choice": choice,
                "selected_region_id": selected_region_id,
                "selected_bbox": selected_bbox,
            },
            case_id=self.case_id,
            queue_if_closed=True,
        )

    def send_spatial_input(
        self,
        request_id: str,
        geometry_type: Optional[str] = None,
        coordinates: Optional[list] = None,
        features: Optional[dict] = None,
        cancelled: bool = False,
    ) -> None:
        """Answer a ``spatial-input-request`` gate. ``geometry_type`` is
        ``"point"`` (``[lon, lat]``), ``"bbox"`` (four coordinates) or
        ``"vector_draw"`` (``features``); ``cancelled`` is the decline path."""
        self._send(
            "spatial-input-response",
            {
                "request_id": request_id,
                "geometry_type": geometry_type,
                "coordinates": coordinates,
                "features": features,
                "cancelled": cancelled,
            },
            case_id=self.case_id,
            queue_if_closed=True,
        )

    # -- event pump ---------------------------------------------------------- #

    def next_event(self, timeout: float = 1.0) -> Optional[AgentEvent]:
        """Receive + normalize one server frame; None on timeout. Raises
        ``ConnectionClosed`` when the socket dies -- the caller, not this
        method, owns reconnect policy."""
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
        # Every envelope the dock acts on gets its OWN kind below. An
        # unrecognized type falls through to ``"raw"`` and SURFACES there --
        # a silently dropped gate envelope leaves the server's paused turn
        # hanging forever.
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
            # Reasoning-channel tokens, keyed by the SAME message_id as the
            # answer chunk that follows, so the dock attaches the thinking
            # block to the right assistant entry.
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
            # The case-open reply is the ONE signal every rebind path shares
            # -- create, select and startup reuse alike -- so the wire stamp
            # follows it unconditionally. Without that, an envelope sent after
            # a rebind carries the previous case_id and the turn persists into
            # the wrong case.
            opened = ((payload.get("session_state") or {}).get("case") or {}).get(
                "case_id"
            )
            if isinstance(opened, str) and opened:
                self.case_id = opened
            return AgentEvent("case-open", payload)
        if etype == "tool-payload-warning":
            return AgentEvent("payload-warning", payload)
        if etype == "code-exec-request":
            # The agent BLOCKS before running sandbox Python until a
            # ``tool-payload-confirmation`` whose ``warning_id`` equals this
            # request's ``code_exec_id`` arrives.
            return AgentEvent("code-exec-request", payload)
        if etype == "credential-request":
            # A keyed tool hit a missing or invalid key and the agent PAUSED
            # it to ask for the credential by name. The pause has a
            # server-side TTL: unanswered, the tool fails.
            return AgentEvent("credential-request", payload)
        if etype == "tool-candidates":
            # The agent ranked several plausible tools and asks which runs.
            # Unanswered, the server's own ``timeout_s`` fail-open proceeds
            # with its top pick, so the user's window to redirect is finite.
            return AgentEvent("tool-candidates", payload)
        if etype == "chart-emission":
            # A live mid-turn chart. Its persisted replay twin rides in the
            # case-open ``session_state.charts``.
            return AgentEvent("chart", payload)
        if etype == "solve-progress":
            return AgentEvent("solve-progress", payload)
        if etype == "tool-io":
            # The raw tool-args sidecar, keyed by pipeline step_id: an
            # input-only frame arrives at dispatch START, the full one on
            # completion.
            return AgentEvent("tool-io", payload)
        if etype == "region-choice-request":
            # A gate WAIT: the server snapped a vague geocode to the whole
            # state and PAUSES the turn until a reply arrives. A
            # ``whole_state`` answer keeps that default, so the gate always
            # has a closing move.
            return AgentEvent("region-choice-request", payload)
        if etype == "spatial-input-request":
            # A gate WAIT: the agent needs a picked geometry and PAUSES the
            # turn until a response arrives. Cancel sends ``cancelled=True``
            # and closes the gate.
            return AgentEvent("spatial-input-request", payload)
        if etype == "code-exec-result":
            # The run OUTCOME after an approved code-exec-request:
            # fire-and-forget, no reply expected.
            return AgentEvent("code-exec-result", payload)
        if etype == "secrets-list":
            # The per-Case secret roster. Raw key values NEVER ride here --
            # only vault_ref records.
            return AgentEvent("secrets-list", payload)
        if etype == "case-list":
            cases = parse_case_list(payload)
            # Stashed like ``last_session_state`` above, so the startup
            # case-reuse decision reads the freshest list either way.
            self.last_case_list = cases
            return AgentEvent("case-list", {"cases": cases, "payload": payload})
        if etype == "loop_exhausted":
            # The agent hit its runaway guard. Surfaced as an ERROR, so the
            # user is told WHY the turn stopped.
            reason = (payload or {}).get("reason", "Agent reached its iteration limit.")
            return AgentEvent("error", {"message": reason, "source": "loop_exhausted"})
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
        """Send an envelope, or buffer it when disconnected. With
        ``queue_if_closed`` a user-intent verb is buffered instead of raising
        and flushed after the next resume; a handshake verb still raises."""
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
        """Drain frames until one of ``etype`` arrives. Non-matching frames
        are DROPPED, except an ``error`` and a ``case-list``, whose payloads
        are stashed so a rejection or a list survives the drain."""
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
            # A ``case-list`` that lands mid-handshake would otherwise be
            # dropped; stash it for the startup case-reuse decision.
            if env.get("type") == "case-list" and isinstance(env.get("payload"), dict):
                self.last_case_list = parse_case_list(env["payload"])
            if env.get("type") == etype:
                return env
