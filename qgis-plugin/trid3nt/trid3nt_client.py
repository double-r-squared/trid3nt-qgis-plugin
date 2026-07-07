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

Protocol (mirrors ``vendor/web/src/ws.ts`` + ``scripts/tool_routing_bench.py``):

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
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

__all__ = [
    "AgentClient",
    "AgentEvent",
    "CaseInfo",
    "ConnectionClosed",
    "HandshakeFailed",
    "LayerEvent",
    "OUTBOUND_QUEUE_MAX",
    "PipelineStep",
    "RECONNECT_FLOOR_MS",
    "RECONNECT_MAX_MS",
    "WebSocketConnection",
    "WebSocketError",
    "build_ws_url",
    "make_envelope",
    "new_ulid",
    "next_backoff",
    "parse_case_list",
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

    The template's own query string (TiTiler ``url=``/``rescale=``/
    ``colormap=`` params) contains ``&`` and ``=`` characters that would
    confuse the QGIS uri parser, so the whole template is percent-encoded
    (QGIS decodes the ``url`` component). ``{z}/{x}/{y}`` placeholders are
    encoded too -- QGIS accepts them either way.
    """
    return (
        f"type=xyz&url={urllib.parse.quote(template, safe='')}"
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
            "User-Agent: trid3nt-qgis-plugin/0.1\r\n"
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
      case-list       {"cases": [CaseInfo, ...], "payload": <raw>}
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

    def send_chat(self, text: str) -> None:
        self._send(
            "user-message",
            {"text": text, "case_id": self.case_id},
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
            return AgentEvent("case-open", payload)
        if etype == "tool-payload-warning":
            return AgentEvent("payload-warning", payload)
        if etype == "case-list":
            return AgentEvent(
                "case-list", {"cases": parse_case_list(payload), "payload": payload}
            )
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
        (tool_routing_bench.do_handshake / create_case).
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
            if isinstance(env, dict) and env.get("type") == etype:
                return env
