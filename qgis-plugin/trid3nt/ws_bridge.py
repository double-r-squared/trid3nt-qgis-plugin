"""Qt bridge for the pure-python connection layer.

The WebSocket lives on a QThread-hosted worker; the dock only ever touches Qt
signals (queued across threads, so slots run on the UI thread). Outbound sends
(``send_chat`` / ``cancel``) are safe to call from the UI thread directly:
``WebSocketConnection.send_text`` is mutex-guarded and a chat-sized
``sendall`` does not block meaningfully.

QGIS freeze rule (product analysis section 7): the socket NEVER blocks the UI
thread -- connect, handshake, and the receive loop all run on the worker.
"""

from __future__ import annotations

import traceback
from typing import Optional

from qgis.PyQt.QtCore import QObject, QThread, pyqtSignal

from .trid3nt_client import AgentClient, ConnectionClosed


class AgentWorker(QObject):
    """Runs connect + handshake + case create + the receive loop."""

    connected = pyqtSignal(str, bool)  # user_id, is_anonymous
    case_ready = pyqtSignal(str)       # case_id
    event = pyqtSignal(str, object)    # AgentEvent.kind, AgentEvent.data
    failed = pyqtSignal(str)           # terminal setup failure (human-readable)
    closed = pyqtSignal(str)           # socket ended (reason)

    def __init__(
        self,
        url: str,
        token: str = "",
        anonymous_user_id: Optional[str] = None,
        case_title: str = "QGIS session",
    ):
        super().__init__()
        self._url = url
        self._token = token
        self._anonymous_user_id = anonymous_user_id or None
        self._case_title = case_title
        self._stop = False
        self.client: Optional[AgentClient] = None

    # Runs on the worker thread (wired to QThread.started).
    def run(self) -> None:
        try:
            self.client = AgentClient(
                self._url,
                token=self._token,
                anonymous_user_id=self._anonymous_user_id,
            )
            user_id = self.client.connect()
            self.connected.emit(user_id, bool(self.client.is_anonymous))
            case_id = self.client.create_case(self._case_title)
            self.case_ready.emit(case_id)
        except Exception as exc:  # noqa: BLE001 -- surfaced verbatim, never silent
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            self._close_client()
            return

        reason = "stopped"
        try:
            while not self._stop:
                ev = self.client.next_event(timeout=1.0)
                if ev is not None:
                    self.event.emit(ev.kind, ev.data)
        except ConnectionClosed as exc:
            reason = str(exc)
        except Exception as exc:  # noqa: BLE001
            reason = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            self._close_client()
            self.closed.emit(reason)

    def stop(self) -> None:
        """Thread-safe: just flips the poll flag the run loop checks."""
        self._stop = True

    def _close_client(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:  # noqa: BLE001
                pass

    # -- UI-thread-safe outbound verbs (socket writes are mutex-guarded) ---- #

    def send_chat(self, text: str) -> None:
        if self.client is not None:
            self.client.send_chat(text)

    def cancel(self) -> None:
        if self.client is not None:
            self.client.cancel()

    def confirm_payload(self, warning_id: str, decision: str = "proceed") -> None:
        if self.client is not None:
            self.client.confirm_payload(warning_id, decision)


class AgentBridge(QObject):
    """Owns the QThread + worker pair; the dock talks only to this."""

    connected = pyqtSignal(str, bool)
    case_ready = pyqtSignal(str)
    event = pyqtSignal(str, object)
    failed = pyqtSignal(str)
    closed = pyqtSignal(str)

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
    ) -> None:
        self.stop()
        self._worker = AgentWorker(
            url,
            token=token,
            anonymous_user_id=anonymous_user_id,
            case_title=case_title,
        )
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.connected.connect(self.connected)
        self._worker.case_ready.connect(self.case_ready)
        self._worker.event.connect(self.event)
        self._worker.failed.connect(self.failed)
        self._worker.closed.connect(self.closed)
        # Whichever way the run loop exits, wind the thread down.
        self._worker.failed.connect(self._thread.quit)
        self._worker.closed.connect(self._thread.quit)
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

    def send_chat(self, text: str) -> None:
        if self._worker is not None:
            self._worker.send_chat(text)

    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def confirm_payload(self, warning_id: str, decision: str = "proceed") -> None:
        if self._worker is not None:
            self._worker.confirm_payload(warning_id, decision)
