"""Qt bridge for the pure-python connection layer.

The socket NEVER blocks the UI thread: connect, handshake, the receive loop and
the reconnect backoff all run on the QThread-hosted worker. Outbound verbs are
safe to call from the UI thread and buffer while disconnected."""

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

    # NEVER name a pyqtSignal after a QObject virtual (event / eventFilter /
    # timerEvent / childEvent / ...). A signal named ``event`` shadows the C++
    # virtual ``QObject.event()``, so the first QEvent Qt delivers -- the
    # ChildAdded from ``QThread(self)`` in AgentBridge.start -- makes PyQt call
    # the attribute as the reimplemented handler: "native Qt signal is not
    # callable", then a qFatal abort of the whole QGIS process.
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
        case_title: str = "QGIS session",
        case_bbox: Optional[list] = None,
        reuse_case: bool = False,
    ):
        super().__init__()
        self._url = url
        self._token = token
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
        )
        # QgsAuthManager credential broker: connect-time push of stored keys +
        # prompt-store. Best-effort -- a locked / unprovisioned auth DB is a
        # silent no-op (the daemon's env fallback covers it).
        try:
            from .auth_broker import AuthBroker

            self.client.credential_broker = AuthBroker()
        except Exception:  # noqa: BLE001 -- broker is optional, never fatal
            pass
        # The FIRST connect is fail-fast: a dead port, a bad URL or a rejected
        # upgrade at the moment the user presses Connect surfaces immediately
        # rather than retrying silently against a stack that was never up. An
        # AUTH-classified failure STOPS rather than retrying, here and in the
        # ladder alike -- a rejected token cannot be fixed by looping on it.
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
                # Each re-dial reuses the SAME session_id and resumes with the
                # current case_id, so the server re-binds the Case and replays
                # its layers; queued outbound intent flushes FIFO. ``stop()``
                # exits the ladder because the backoff sleep polls the flag.
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
        """Bind the fresh connection to a case; returns its case_id. Under
        ``reuse_case`` a fresh case is minted only when the user has none, and
        every reuse rung still sends a select so case-open rehydration runs."""
        if self._reuse_case:
            # The live server emits ``case-list`` right AFTER the session-state
            # the connect handshake consumed, so with neither a resumed case
            # nor a stashed list we pump events briefly -- forwarding them to
            # the dock as usual -- until the list lands. A no-show inside the
            # window falls through to an honest create.
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

    # -- UI-thread-safe outbound verbs. Socket writes are mutex-guarded and -- #
    # -- buffer in the client's bounded queue while disconnected, so a paused - #
    # -- turn never hangs. A raw key passes through and is never logged here. - #

    def send_chat(
        self,
        text: str,
        show_thinking: bool = False,
        model_id: str = "",
        aoi_bbox: Optional[Tuple[float, float, float, float]] = None,
        tool_choice_mode: str = "",
        drawn_geometry: Optional[dict] = None,
    ) -> None:
        if self.client is not None:
            self.client.send_chat(
                text,
                show_thinking=show_thinking,
                model_id=model_id,
                aoi_bbox=aoi_bbox,
                tool_choice_mode=tool_choice_mode,
                drawn_geometry=drawn_geometry,
            )

    def send_dev_tool_invoke(
        self, name: str, args: dict, raw_text: str = ""
    ) -> None:
        if self.client is not None:
            self.client.send_dev_tool_invoke(name, args, raw_text=raw_text)

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
        if self.client is not None:
            self.client.send_tool_choice(
                request_id, tool_name=tool_name, free_text=free_text
            )

    def send_region_choice(
        self,
        request_id: str,
        choice: str,
        selected_region_id: Optional[str] = None,
        selected_bbox: Optional[list] = None,
    ) -> None:
        if self.client is not None:
            self.client.send_region_choice(
                request_id,
                choice,
                selected_region_id=selected_region_id,
                selected_bbox=selected_bbox,
            )

    def send_spatial_input(
        self,
        request_id: str,
        geometry_type: Optional[str] = None,
        coordinates: Optional[list] = None,
        features: Optional[dict] = None,
        name: Optional[str] = None,
        cancelled: bool = False,
    ) -> None:
        if self.client is not None:
            self.client.send_spatial_input(
                request_id,
                geometry_type=geometry_type,
                coordinates=coordinates,
                features=features,
                name=name,
                cancelled=cancelled,
            )


class AgentBridge(QObject):
    """Owns the QThread + worker pair; the dock talks only to this."""

    # ``agent_event``, NOT ``event``: naming a signal after a QObject virtual
    # aborts the QGIS process (see the AgentWorker signal block).
    # ``connected`` carries (user_id, is_anonymous, http_base, data_base) --
    # the signature MUST match the worker's 4-arg signal it forwards at
    # start(); a narrower signature here silently DROPS the advertised
    # endpoints and every remote (tailnet) client falls back to localhost
    # layer fetches.
    connected = pyqtSignal(str, bool, str, str)
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
        case_title: str = "QGIS session",
        case_bbox: Optional[list] = None,
        reuse_case: bool = False,
    ) -> None:
        self.stop()
        self._worker = AgentWorker(
            url,
            token=token,
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

    # -- outbound: pass-through to the worker's client. Socket writes are ----- #
    # -- mutex-guarded and buffer while disconnected; the bridge stores ------- #
    # -- nothing of what passes through it. ---------------------------------- #

    def send_chat(
        self,
        text: str,
        show_thinking: bool = False,
        model_id: str = "",
        aoi_bbox: Optional[Tuple[float, float, float, float]] = None,
        tool_choice_mode: str = "",
        drawn_geometry: Optional[dict] = None,
    ) -> None:
        if self._worker is not None:
            self._worker.send_chat(
                text,
                show_thinking=show_thinking,
                model_id=model_id,
                aoi_bbox=aoi_bbox,
                tool_choice_mode=tool_choice_mode,
                drawn_geometry=drawn_geometry,
            )

    def send_dev_tool_invoke(
        self, name: str, args: dict, raw_text: str = ""
    ) -> None:
        if self._worker is not None:
            self._worker.send_dev_tool_invoke(name, args, raw_text=raw_text)

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
        if self._worker is not None:
            self._worker.send_tool_choice(
                request_id, tool_name=tool_name, free_text=free_text
            )

    def send_region_choice(
        self,
        request_id: str,
        choice: str,
        selected_region_id: Optional[str] = None,
        selected_bbox: Optional[list] = None,
    ) -> None:
        if self._worker is not None:
            self._worker.send_region_choice(
                request_id,
                choice,
                selected_region_id=selected_region_id,
                selected_bbox=selected_bbox,
            )

    def send_spatial_input(
        self,
        request_id: str,
        geometry_type: Optional[str] = None,
        coordinates: Optional[list] = None,
        features: Optional[dict] = None,
        name: Optional[str] = None,
        cancelled: bool = False,
    ) -> None:
        if self._worker is not None:
            self._worker.send_spatial_input(
                request_id,
                geometry_type=geometry_type,
                coordinates=coordinates,
                features=features,
                name=name,
                cancelled=cancelled,
            )