"""Cross-thread worker QObjects for the dock + settings dialog.

Each task runs exactly ONE round trip off the Qt UI thread and emits its result
via a cross-thread signal, so a slow or dead agent never freezes a dialog."""
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
    """GET /api/case-list."""

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
    """POST /api/provider-config. SECURITY: the payload carries the provider
    api key and this task NEVER logs it."""

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
    """GET /api/local-models. ``finished`` carries ``(model_ids, provider)``
    so a stale fetch for a since-changed provider is ignored at the call
    site."""

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
    """GET the agent's EFFECTIVE (env-default) model id, so the status strip
    can name the running model when the user picked none. Silent on failure:
    the label keeps whatever text it had."""

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
    """Push the active QGIS layer into a case: one export-to-tempfile, upload
    and register round trip. The temp file is deleted whether the ingest POST
    succeeds or fails."""

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
    """POST /api/probe-point for one map click. Only the round trip runs off
    the UI thread; the result is formatted back in the ``finished`` slot."""

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

