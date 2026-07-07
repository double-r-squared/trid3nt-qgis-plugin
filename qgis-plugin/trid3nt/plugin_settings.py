"""Plugin settings -- QSettings-backed, one namespace.

Kept out of the dock widget so both the bridge and the layer materializer can
read the same values. Uses ``qgis.PyQt`` (Qt5/Qt6-neutral surface, per the
product analysis section 5).
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QSettings

GROUP = "trid3nt"

DEFAULT_LOCAL_URL = "ws://127.0.0.1:8765/ws"
DEFAULT_REMOTE_URL = "wss://"
DEFAULT_MINIO_ENDPOINT = "http://127.0.0.1:9000"
DEFAULT_EXPORT_API = "http://127.0.0.1:8766"

MODE_LOCAL = "local"
MODE_REMOTE = "remote"


class PluginSettings:
    """Read/write view over the plugin's QSettings keys."""

    def __init__(self) -> None:
        self._qs = QSettings()

    # -- raw accessors -------------------------------------------------------- #

    def _get(self, key: str, default: str = "") -> str:
        return str(self._qs.value(f"{GROUP}/{key}", default) or default)

    def _set(self, key: str, value: str) -> None:
        self._qs.setValue(f"{GROUP}/{key}", value)

    # -- typed properties ------------------------------------------------------ #

    @property
    def mode(self) -> str:
        mode = self._get("mode", MODE_LOCAL)
        return mode if mode in (MODE_LOCAL, MODE_REMOTE) else MODE_LOCAL

    @mode.setter
    def mode(self, value: str) -> None:
        self._set("mode", value if value in (MODE_LOCAL, MODE_REMOTE) else MODE_LOCAL)

    @property
    def local_url(self) -> str:
        return self._get("local_url", DEFAULT_LOCAL_URL) or DEFAULT_LOCAL_URL

    @local_url.setter
    def local_url(self, value: str) -> None:
        self._set("local_url", value.strip() or DEFAULT_LOCAL_URL)

    @property
    def remote_url(self) -> str:
        return self._get("remote_url", DEFAULT_REMOTE_URL)

    @remote_url.setter
    def remote_url(self, value: str) -> None:
        self._set("remote_url", value.strip())

    @property
    def token(self) -> str:
        """Pasted bearer token for remote mode. Auth ACQUISITION (Cognito
        sign-in flow) is out of scope for milestone 1 -- paste-only."""
        return self._get("token", "")

    @token.setter
    def token(self, value: str) -> None:
        self._set("token", value.strip())

    @property
    def minio_endpoint(self) -> str:
        return self._get("minio_endpoint", DEFAULT_MINIO_ENDPOINT) or DEFAULT_MINIO_ENDPOINT

    @minio_endpoint.setter
    def minio_endpoint(self, value: str) -> None:
        self._set("minio_endpoint", value.strip() or DEFAULT_MINIO_ENDPOINT)

    @property
    def export_api(self) -> str:
        """The local agent's HTTP listener base URL (tool catalog + the
        /api/export-qgis routes) -- Open-case-in-QGIS uses this."""
        return self._get("export_api", DEFAULT_EXPORT_API) or DEFAULT_EXPORT_API

    @export_api.setter
    def export_api(self, value: str) -> None:
        self._set("export_api", value.strip() or DEFAULT_EXPORT_API)

    @property
    def canvas_aoi(self) -> bool:
        """Milestone 2: "Use map canvas as area of interest" toggle (default
        ON). Stored as "true"/"false" strings (QSettings bool portability)."""
        return self._get("canvas_aoi", "true").lower() != "false"

    @canvas_aoi.setter
    def canvas_aoi(self, value: bool) -> None:
        self._set("canvas_aoi", "true" if value else "false")

    @property
    def anonymous_user_id(self) -> str:
        """Server-assigned anonymous user id, replayed on reconnect so the
        same local User record re-binds (mirrors the web client)."""
        return self._get("anonymous_user_id", "")

    @anonymous_user_id.setter
    def anonymous_user_id(self, value: str) -> None:
        self._set("anonymous_user_id", value)

    # -- derived --------------------------------------------------------------- #

    def effective_url(self) -> str:
        return self.local_url if self.mode == MODE_LOCAL else self.remote_url

    def effective_token(self) -> str:
        return "" if self.mode == MODE_LOCAL else self.token
