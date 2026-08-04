"""Cross-thread worker QObjects for the dock + settings dialog.

Split out of dock.py (2026-07-21 flat->package restructure). Each task runs one
net/case/render round trip OFF the Qt UI thread and emits the result via a
cross-thread signal. They live in ``net/`` so both the dock (``ui/dock.py``) and
the settings dialog (``ui/settings_dialog.py``) import them without a ui<->ui
import cycle. Behavior identical -- this is a move.
"""
from __future__ import annotations

import threading
from typing import Optional

from qgis.PyQt.QtCore import QObject, pyqtSignal

from .trid3nt_client import (
    CaseListRequestError,
    ModelListRequestError,
    ProviderConfigRequestError,
    fetch_case_list,
    fetch_model_list,
    post_provider_config,
)
from ..case import push_layer
from ..render import probe


class _CaseListTask(QObject):
    """GET /api/case-list off the UI thread (items b/c, live-feedback
    2026-07-09) -- the cross-thread signal-emit pattern (one net round trip
    off the Qt UI thread) so a slow/dead agent HTTP listener never freezes the
    Cases dialog.
    """

    finished = pyqtSignal(list)  # list[CaseInfo]
    errored = pyqtSignal(str)    # honest message

    def __init__(self, base_url: str, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._base_url = base_url

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            cases = fetch_case_list(self._base_url)
        except CaseListRequestError as exc:
            self.errored.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 -- surfaced, never silent
            self.errored.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(cases)


class _ProviderConfigTask(QObject):
    """POST /api/provider-config off the UI thread (Feature 3, OpenRouter
    model-extensibility 2026-07-19) -- follows the ``_CaseListTask`` pattern
    (cross-thread signal emit) so a dead/asleep agent HTTP listener never
    freezes the Settings dialog on Save. SECURITY: the payload carries the
    provider api key; this task NEVER logs it, and the client helper
    (``post_provider_config``) is likewise silent."""

    finished = pyqtSignal(dict)  # {"ok", "model", "base_url_host"}
    errored = pyqtSignal(str)    # honest message (never contains the key)

    def __init__(self, base_url: str, payload: dict, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._base_url = base_url
        self._payload = payload

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            result = post_provider_config(self._base_url, self._payload)
        except ProviderConfigRequestError as exc:
            self.errored.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 -- surfaced, never silent
            self.errored.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(result)


class _ModelListTask(QObject):
    """GET /api/local-models off the UI thread (Feature 2, OpenRouter
    free-model dropdown 2026-07-19) -- follows the ``_CaseListTask`` pattern.
    ``finished`` carries ``(model_ids, provider)`` so a stale fetch for a
    since-changed provider is ignored at the call site; ``errored`` is a
    silent fallback to the static shortlist."""

    finished = pyqtSignal(list, str)  # (model_ids, provider)
    errored = pyqtSignal(str)         # honest message

    def __init__(self, base_url: str, provider: str, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._base_url = base_url
        self._provider = provider

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            ids, _default = fetch_model_list(self._base_url)
        except ModelListRequestError as exc:
            self.errored.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 -- surfaced, never silent
            self.errored.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(ids, self._provider)


class _EffectiveModelTask(QObject):
    """GET the agent's EFFECTIVE (env-default) model id off the UI thread, so
    the status strip can show the running model even when the user did not pick
    one in Settings (e.g. NATE's nemotron set via .env.local). Reuses
    ``fetch_model_list`` (its second return value is the agent default). Silent
    on failure -- the label just keeps whatever it had. NATE 2026-07-20."""

    finished = pyqtSignal(str)  # the agent default model id ("" if unknown)

    def __init__(self, base_url: str, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._base_url = base_url

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            _ids, default = fetch_model_list(self._base_url)
        except Exception:  # noqa: BLE001 -- silent: the label keeps its text
            return
        self.finished.emit(str(default or ""))


class _PushLayerTask(QObject):
    """Push the active QGIS layer into a case via ``push_layer.py``, off the
    UI thread (cross-thread signal emit) -- the same off-thread pattern as
    the other tasks here. One task = one export-to-tempfile + upload + register round
    trip (``push_layer.push_active_layer``); the temp file is deleted by
    ``push_exported_file`` whether the ingest POST succeeds or fails.
    """

    finished = pyqtSignal(str, dict)  # layer_name, result
    errored = pyqtSignal(str, str)    # layer_name, message

    def __init__(
        self,
        base_url: str,
        case_id: str,
        layer,
        make_aoi: bool = False,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._base_url = base_url
        self._case_id = case_id
        self._layer = layer
        self._make_aoi = make_aoi
        try:
            self._layer_name = layer.name() or ""
        except Exception:  # noqa: BLE001 -- best-effort label only
            self._layer_name = ""

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            result = push_layer.push_active_layer(
                self._base_url, self._case_id, self._layer, make_aoi=self._make_aoi
            )
        except push_layer.PushLayerRequestError as exc:
            self.errored.emit(self._layer_name, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 -- surfaced, never silent
            self.errored.emit(self._layer_name, f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(self._layer_name, result)


class _ProbePointTask(QObject):
    """POST /api/probe-point off the UI thread, for one map click -- follows
    the ``_PushLayerTask`` off-thread pattern (cross-thread signal
    emit). One task = one round trip (``probe.post_probe_point``); the
    result formatting (``probe.format_probe_result``) runs back on the UI
    thread in the ``finished`` slot, matching every other worker task here.
    """

    finished = pyqtSignal(float, float, dict)  # lon, lat, result
    errored = pyqtSignal(float, float, str)    # lon, lat, message

    def __init__(
        self,
        base_url: str,
        case_id: str,
        lon: float,
        lat: float,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._base_url = base_url
        self._case_id = case_id
        self._lon = lon
        self._lat = lat

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            result = probe.post_probe_point(
                self._base_url, self._case_id, self._lon, self._lat
            )
        except probe.ProbePointRequestError as exc:
            self.errored.emit(self._lon, self._lat, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 -- surfaced, never silent
            self.errored.emit(self._lon, self._lat, f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(self._lon, self._lat, result)

