"""Qt bridge for the pure-python connection layer.

The WebSocket lives on a QThread-hosted worker; the dock only ever touches Qt
signals (queued across threads, so slots run on the UI thread). Outbound sends
(``send_chat`` / ``cancel`` / ``confirm_payload``) are safe to call from the
UI thread directly: ``WebSocketConnection.send_text`` is mutex-guarded and a
chat-sized ``sendall`` does not block meaningfully; while disconnected they
buffer in the client's bounded outbound queue and flush on resume.

QGIS freeze rule (product analysis section 7): the socket NEVER blocks the UI
thread -- connect, handshake, the receive loop, AND the reconnect backoff all
run on the worker.

Milestone 2 reconnect policy (mirrors the web client, ws.ts):

* The FIRST connect is fail-fast: a dead port / bad URL / rejected upgrade at
  the moment the user presses Connect surfaces immediately as ``failed``
  (milestone 1 behaviour preserved -- no silent retry against a stack that
  was never up).
* AFTER a successful first connect, any transport loss enters the
  capped-jitter reconnect ladder (floor 1.5 s doubling to 5 s, jitter in
  [0.5, 1.0) x base -- ``trid3nt_client.next_backoff``), emitting
  ``reconnecting`` per attempt. Each re-dial reuses the SAME session_id +
  sticky anonymous_user_id and sends ``session-resume`` with the current
  case_id so the server re-binds the Case and replays its layers; queued
  outbound intent flushes FIFO. ``resumed`` fires when the wire is back.
* ``stop()`` exits the ladder immediately (the backoff sleep polls the stop
  flag).

Milestone 3 token-expiry policy: a failure that classifies as AUTH
(``trid3nt_client.is_auth_failure`` -- the broker's pre-upgrade 401/403 on a
dead ``?st=`` token, or an in-band AUTH_REQUIRED error) emits
``auth_expired`` and STOPS -- first connect and reconnect ladder alike.
Retrying a dead token forever is exactly the silent-reconnect-loop UX this
kills; the dock tells the user to paste a fresh token instead.
"""

from __future__ import annotations

import time
import traceback
from typing import Optional, Tuple

from qgis.PyQt.QtCore import QObject, QThread, pyqtSignal

from .trid3nt_client import (
    RECONNECT_FLOOR_MS,
    AgentClient,
    ConnectionClosed,
    WebSocketError,
    choose_startup_case,
    is_auth_failure,
    next_backoff,
)


class AgentWorker(QObject):
    """Runs connect + handshake + case create + the receive/reconnect loop."""

    # CRASH FIX (found live in QGIS 3.40.6): this signal was named ``event``,
    # which SHADOWS the C++ virtual ``QObject.event()``. The first QEvent Qt
    # delivered to the object (the ChildAdded from ``QThread(self)`` in
    # AgentBridge.start) made PyQt call the attribute as the reimplemented
    # event handler -> "TypeError: native Qt signal is not callable" -> qFatal
    # abort of the whole QGIS process. NEVER name a pyqtSignal after a
    # QObject virtual (event / eventFilter / timerEvent / childEvent / ...).
    # user_id, is_anonymous, advertised_http_base ("" if none), advertised_data_base ("" if none)
    connected = pyqtSignal(str, bool, str, str)
    case_ready = pyqtSignal(str)       # case_id
    agent_event = pyqtSignal(str, object)  # AgentEvent.kind, AgentEvent.data
    failed = pyqtSignal(str)           # terminal setup failure (human-readable)
    closed = pyqtSignal(str)           # loop ended for good (reason)
    reconnecting = pyqtSignal(str)     # transport lost; entering backoff (reason)
    resumed = pyqtSignal()             # reconnect handshake done, queue flushed
    auth_expired = pyqtSignal(str)     # token rejected -- paste a fresh one

    def __init__(
        self,
        url: str,
        token: str = "",
        anonymous_user_id: Optional[str] = None,
        case_title: str = "QGIS session",
        case_bbox: Optional[list] = None,
        reuse_case: bool = False,
    ):
        super().__init__()
        self._url = url
        self._token = token
        self._anonymous_user_id = anonymous_user_id or None
        self._case_title = case_title
        self._case_bbox = case_bbox
        self._reuse_case = reuse_case
        self._stop = False
        self.client: Optional[AgentClient] = None

    # Runs on the worker thread (wired to QThread.started).
    def run(self) -> None:
        self.client = AgentClient(
            self._url,
            token=self._token,
            anonymous_user_id=self._anonymous_user_id,
        )
        # First connect: fail-fast (see module docstring).
        try:
            user_id = self.client.connect()
            self.connected.emit(
                user_id,
                bool(self.client.is_anonymous),
                self.client.advertised_http_base or "",
                self.client.advertised_data_base or "",
            )
            case_id = self._bind_startup_case()
            self.case_ready.emit(case_id)
        except Exception as exc:  # noqa: BLE001 -- surfaced verbatim, never silent
            text = self._failure_text(exc)
            if is_auth_failure(text):
                self.auth_expired.emit(text)
            else:
                self.failed.emit(text)
            self._close_client()
            return

        backoff_ms = RECONNECT_FLOOR_MS
        reason = "stopped"
        try:
            while not self._stop:
                # -- receive until stop or transport loss -------------------- #
                try:
                    while not self._stop:
                        ev = self.client.next_event(timeout=1.0)
                        if ev is not None:
                            self.agent_event.emit(ev.kind, ev.data)
                    break  # stop requested
                except ConnectionClosed as exc:
                    if self._stop:
                        break
                    self.reconnecting.emit(str(exc))

                # -- capped-jitter reconnect ladder --------------------------- #
                while not self._stop:
                    delay_ms, backoff_ms = next_backoff(backoff_ms)
                    if not self._sleep_interruptible(delay_ms / 1000.0):
                        break  # stop requested mid-backoff
                    try:
                        self.client.reconnect()
                    except (WebSocketError, OSError) as exc:
                        text = self._failure_text(exc)
                        if is_auth_failure(text):
                            # A dead token cannot be fixed by retrying --
                            # exit the ladder honestly instead of looping.
                            self.auth_expired.emit(text)
                            reason = "auth-expired"
                            self._stop = True
                            break
                        self.reconnecting.emit(text)
                        continue
                    backoff_ms = RECONNECT_FLOOR_MS  # reset on successful open
                    self.resumed.emit()
                    break
        except Exception as exc:  # noqa: BLE001 -- anything else is terminal
            reason = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            self._close_client()
            self.closed.emit(reason)

    def _bind_startup_case(self) -> str:
        """Bind the fresh connection to a case; returns its case_id.

        ``reuse_case=False`` (remote mode): milestone 1 behavior, unchanged
        -- create a fresh case.

        ``reuse_case=True`` (local mode, live-feedback 2026-07-09): never
        mint a fresh case while the user already has one -- the old always-
        create regrew case clutter on every dock-show. Decision ladder
        (``choose_startup_case``, pure + unit-tested):

          1. the resume handshake rebound a persisted active case -> keep it;
          2. else the user HAS cases -> select the NEWEST live one;
          3. else (zero cases) -> create, the only remaining path.

        Both reuse rungs send a ``case-command select`` (even for the
        resumed case): the server's full ``case-open`` rehydration then
        flows through the normal event pump and rebinds the dock with the
        authoritative title + persisted layers + bbox zoom -- the dock is
        never left caseless (``case_ready`` fires with the target id either
        way; the case-open refines it moments later).

        The live server emits the ``case-list`` envelope right AFTER the
        session-state the connect handshake consumed (the stub emits it
        before, which the handshake drain stashes), so when neither a
        resumed case nor a stashed list exists yet we pump events briefly
        -- forwarding them to the dock as usual -- until the list lands.
        A no-show inside the window falls through to an honest create.
        """
        if self._reuse_case:
            if self.client.case_id is None and self.client.last_case_list is None:
                deadline = time.monotonic() + 5.0
                while (
                    not self._stop
                    and self.client.last_case_list is None
                    and time.monotonic() < deadline
                ):
                    ev = self.client.next_event(timeout=0.5)
                    if ev is not None:
                        self.agent_event.emit(ev.kind, ev.data)
            action, target = choose_startup_case(
                self.client.case_id, self.client.last_case_list or []
            )
            if action in ("resume", "select") and target:
                self.client.select_case(target)
                return target
        return self.client.create_case(self._case_title, bbox=self._case_bbox)

    def _sleep_interruptible(self, seconds: float) -> bool:
        """Sleep in small slices, polling the stop flag. False = stopped."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stop:
                return False
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        return not self._stop

    def stop(self) -> None:
        """Thread-safe: just flips the poll flag the run loop checks."""
        self._stop = True

    def _failure_text(self, exc: Exception) -> str:
        """The exception, plus any error envelope the handshake drained
        (e.g. AUTH_REQUIRED before a 1008 close) -- one classifiable line."""
        text = f"{type(exc).__name__}: {exc}"
        err = getattr(self.client, "last_handshake_error", None)
        if isinstance(err, dict):
            code = str(err.get("error_code") or "").strip()
            message = str(err.get("message") or "").strip()
            detail = " ".join(part for part in (code, message) if part)
            if detail:
                text += f" [{detail}]"
        return text

    def _close_client(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:  # noqa: BLE001
                pass

    # -- UI-thread-safe outbound verbs (socket writes are mutex-guarded; ---- #
    # -- while disconnected they buffer in the client's bounded queue) ------ #

    def send_chat(
        self,
        text: str,
        show_thinking: bool = False,
        model_id: str = "",
        aoi_bbox: Optional[Tuple[float, float, float, float]] = None,
        tool_choice_mode: str = "",
    ) -> None:
        if self.client is not None:
            self.client.send_chat(
                text,
                show_thinking=show_thinking,
                model_id=model_id,
                aoi_bbox=aoi_bbox,
                tool_choice_mode=tool_choice_mode,
            )

    def cancel(self) -> None:
        if self.client is not None:
            self.client.cancel()

    def select_case(self, case_id: str) -> None:
        if self.client is not None:
            self.client.select_case(case_id)

    def case_command(
        self,
        command: str,
        case_id: Optional[str] = None,
        args: Optional[dict] = None,
    ) -> None:
        if self.client is not None:
            self.client.case_command(command, case_id=case_id, args=args)

    def refresh_case_list(self) -> bool:
        if self.client is not None:
            return self.client.request_case_list_refresh()
        return False

    def confirm_payload(
        self,
        warning_id: str,
        decision: str = "proceed",
        revised_args: Optional[dict] = None,
    ) -> None:
        if self.client is not None:
            self.client.confirm_payload(warning_id, decision, revised_args)

    def submit_credential(
        self, request_id: str, provider_id: str, key_value: str
    ) -> None:
        # LANE K: the raw key passes straight through to the client's
        # secret-add + credential-provided pair -- never logged, never stored.
        if self.client is not None:
            self.client.submit_credential(request_id, provider_id, key_value)

    def decline_credential(self, request_id: str) -> None:
        if self.client is not None:
            self.client.decline_credential(request_id)

    def send_tool_choice(
        self,
        request_id: str,
        tool_name: Optional[str] = None,
        free_text: Optional[str] = None,
    ) -> None:
        # ADR 0018 picker reply -- one tool-choice envelope (pick / guidance
        # / let-agent-decide), buffered while disconnected like every
        # user-intent verb.
        if self.client is not None:
            self.client.send_tool_choice(
                request_id, tool_name=tool_name, free_text=free_text
            )


class AgentBridge(QObject):
    """Owns the QThread + worker pair; the dock talks only to this."""

    # ``agent_event``, NOT ``event`` -- see the AgentWorker signal block for
    # the QObject.event() shadowing crash this name avoids.
    connected = pyqtSignal(str, bool)
    case_ready = pyqtSignal(str)
    agent_event = pyqtSignal(str, object)
    failed = pyqtSignal(str)
    closed = pyqtSignal(str)
    reconnecting = pyqtSignal(str)
    resumed = pyqtSignal()
    auth_expired = pyqtSignal(str)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._thread: Optional[QThread] = None
        self._worker: Optional[AgentWorker] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(
        self,
        url: str,
        token: str = "",
        anonymous_user_id: Optional[str] = None,
        case_title: str = "QGIS session",
        case_bbox: Optional[list] = None,
        reuse_case: bool = False,
    ) -> None:
        self.stop()
        self._worker = AgentWorker(
            url,
            token=token,
            anonymous_user_id=anonymous_user_id,
            case_title=case_title,
            case_bbox=case_bbox,
            reuse_case=reuse_case,
        )
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.connected.connect(self.connected)
        self._worker.case_ready.connect(self.case_ready)
        self._worker.agent_event.connect(self.agent_event)
        self._worker.failed.connect(self.failed)
        self._worker.closed.connect(self.closed)
        self._worker.reconnecting.connect(self.reconnecting)
        self._worker.resumed.connect(self.resumed)
        self._worker.auth_expired.connect(self.auth_expired)
        # Whichever way the run loop exits, wind the thread down. (The
        # first-connect auth path emits auth_expired and returns without a
        # closed emission, so it must quit the thread too.)
        self._worker.failed.connect(self._thread.quit)
        self._worker.closed.connect(self._thread.quit)
        self._worker.auth_expired.connect(self._thread.quit)
        self._thread.start()

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
        self._worker = None
        self._thread = None

    # -- outbound ------------------------------------------------------------ #

    def send_chat(
        self,
        text: str,
        show_thinking: bool = False,
        model_id: str = "",
        aoi_bbox: Optional[Tuple[float, float, float, float]] = None,
        tool_choice_mode: str = "",
    ) -> None:
        if self._worker is not None:
            self._worker.send_chat(
                text,
                show_thinking=show_thinking,
                model_id=model_id,
                aoi_bbox=aoi_bbox,
                tool_choice_mode=tool_choice_mode,
            )

    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def select_case(self, case_id: str) -> None:
        if self._worker is not None:
            self._worker.select_case(case_id)

    def case_command(
        self,
        command: str,
        case_id: Optional[str] = None,
        args: Optional[dict] = None,
    ) -> None:
        if self._worker is not None:
            self._worker.case_command(command, case_id=case_id, args=args)

    def refresh_case_list(self) -> bool:
        if self._worker is not None:
            return self._worker.refresh_case_list()
        return False

    def confirm_payload(
        self,
        warning_id: str,
        decision: str = "proceed",
        revised_args: Optional[dict] = None,
    ) -> None:
        if self._worker is not None:
            self._worker.confirm_payload(warning_id, decision, revised_args)

    def submit_credential(
        self, request_id: str, provider_id: str, key_value: str
    ) -> None:
        # LANE K: pass-through to the worker's client (mutex-guarded socket
        # write; buffers while disconnected like every user-intent verb).
        # The key is never logged or stored on the bridge.
        if self._worker is not None:
            self._worker.submit_credential(request_id, provider_id, key_value)

    def decline_credential(self, request_id: str) -> None:
        if self._worker is not None:
            self._worker.decline_credential(request_id)

    def send_tool_choice(
        self,
        request_id: str,
        tool_name: Optional[str] = None,
        free_text: Optional[str] = None,
    ) -> None:
        # ADR 0018 picker reply: pass-through to the worker's client
        # (mutex-guarded socket write; buffers while disconnected).
        if self._worker is not None:
            self._worker.send_tool_choice(
                request_id, tool_name=tool_name, free_text=free_text
            )