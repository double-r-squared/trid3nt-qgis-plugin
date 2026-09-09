"""Plugin settings -- QSettings-backed, one namespace.

A value either rides the wire per turn (model id, thinking, tool-choice mode) or
is agent-process ENV the plugin cannot inject: provider and its API key persist
here and take effect only when the agent restarts."""

from __future__ import annotations

from qgis.PyQt.QtCore import QSettings

GROUP = "trid3nt"

DEFAULT_LOCAL_URL = "ws://127.0.0.1:8765/ws"
DEFAULT_MINIO_ENDPOINT = "http://127.0.0.1:9000"
DEFAULT_EXPORT_API = "http://127.0.0.1:8766"

#: The bundled local stack's object-store credentials (``scripts/start_minio.sh``
#: provisions this user). They are defaults, not secrets: a store standing
#: anywhere but this box is reached by overriding these two keys in the profile.
DEFAULT_STORE_ACCESS_KEY = "trid3nt"
DEFAULT_STORE_SECRET_KEY = "trid3nt-local-dev"
DEFAULT_STORE_REGION = "us-east-1"

#: The product is LOCAL-only: the agent runs on this box or a tailnet peer,
#: reached over ws:// (the tailnet itself is the trust boundary). ``MODE_LOCAL``
#: and the read-only ``mode`` property exist ONLY as a migration seam, so a
#: config persisted with ``mode=remote`` loads without a crash and degrades to
#: the sole local behavior.
MODE_LOCAL = "local"


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
        """Always ``MODE_LOCAL``. A stored ``mode=remote`` reads as local
        rather than crashing; the stored key is otherwise inert."""
        return MODE_LOCAL

    @property
    def local_url(self) -> str:
        return self._get("local_url", DEFAULT_LOCAL_URL) or DEFAULT_LOCAL_URL

    @local_url.setter
    def local_url(self, value: str) -> None:
        self._set("local_url", value.strip() or DEFAULT_LOCAL_URL)

    @property
    def token(self) -> str:
        """The optional shared tailnet token; empty means OFF. Pasted verbatim
        into the connect handshake and never expires -- a static shared secret
        is either accepted or rejected."""
        return self._get("token", "")

    @token.setter
    def token(self, value: str) -> None:
        self._set("token", value.strip())

    @property
    def minio_endpoint(self) -> str:
        """The object store's endpoint FALLBACK, read only when a connect
        handshake advertised no ``data_base``. Not a settings-dialog field."""
        return self._get("minio_endpoint", DEFAULT_MINIO_ENDPOINT) or DEFAULT_MINIO_ENDPOINT

    @minio_endpoint.setter
    def minio_endpoint(self, value: str) -> None:
        self._set("minio_endpoint", value.strip() or DEFAULT_MINIO_ENDPOINT)

    @property
    def store_access_key(self) -> str:
        """Object-store access key GDAL signs ``/vsis3`` reads with."""
        return self._get("store_access_key", DEFAULT_STORE_ACCESS_KEY) or DEFAULT_STORE_ACCESS_KEY

    @store_access_key.setter
    def store_access_key(self, value: str) -> None:
        self._set("store_access_key", value.strip() or DEFAULT_STORE_ACCESS_KEY)

    @property
    def store_secret_key(self) -> str:
        """Object-store secret key GDAL signs ``/vsis3`` reads with."""
        return self._get("store_secret_key", DEFAULT_STORE_SECRET_KEY) or DEFAULT_STORE_SECRET_KEY

    @store_secret_key.setter
    def store_secret_key(self, value: str) -> None:
        self._set("store_secret_key", value.strip() or DEFAULT_STORE_SECRET_KEY)

    @property
    def store_region(self) -> str:
        """Region the signature is scoped to; MinIO accepts any single value."""
        return self._get("store_region", DEFAULT_STORE_REGION) or DEFAULT_STORE_REGION

    @store_region.setter
    def store_region(self, value: str) -> None:
        self._set("store_region", value.strip() or DEFAULT_STORE_REGION)

    @property
    def export_api(self) -> str:
        """The :8766 base of LAST resort, for a caller with no live
        connection. Not a settings-dialog field: a connected session derives
        its effective base from the handshake instead."""
        return self._get("export_api", DEFAULT_EXPORT_API) or DEFAULT_EXPORT_API

    @export_api.setter
    def export_api(self, value: str) -> None:
        self._set("export_api", value.strip() or DEFAULT_EXPORT_API)

    @property
    def canvas_aoi(self) -> bool:
        """Whether the map canvas extent is offered as the AOI (default ON)."""
        return self._get("canvas_aoi", "true").lower() != "false"

    @canvas_aoi.setter
    def canvas_aoi(self, value: bool) -> None:
        self._set("canvas_aoi", "true" if value else "false")

    @property
    def selection_aoi(self) -> bool:
        """Whether a selected polygon overrides the canvas extent as the AOI
        (default OFF -- an explicit, opt-in override)."""
        return self._get("selection_aoi", "false").lower() == "true"

    @selection_aoi.setter
    def selection_aoi(self, value: bool) -> None:
        self._set("selection_aoi", "true" if value else "false")

    @property
    def basemap_preset(self) -> str:
        return self._get("basemap_preset", "OpenStreetMap")

    @basemap_preset.setter
    def basemap_preset(self, value: str) -> None:
        self._set("basemap_preset", str(value))

    @property
    def auto_basemap(self) -> bool:
        """Add an OpenStreetMap basemap when a case opens (default ON), so the
        canvas is never left white behind the case's own layers."""
        return self._get("auto_basemap", "true").lower() != "false"

    @auto_basemap.setter
    def auto_basemap(self, value: bool) -> None:
        self._set("auto_basemap", "true" if value else "false")

    @property
    def show_thinking(self) -> bool:
        """Ride ``show_thinking`` on the user-message payload (default ON) so
        the server forwards the model's reasoning channel."""
        return self._get("show_thinking", "true").lower() != "false"

    @show_thinking.setter
    def show_thinking(self, value: bool) -> None:
        self._set("show_thinking", "true" if value else "false")

    @property
    def tool_choice_mode(self) -> str:
        """``"auto"`` (default, no picker cards) or ``"ask"`` (every staged
        selection surfaces as a picker). Reads filter to that closed vocabulary,
        so a hand-edited value degrades to ``"auto"``."""
        value = self._get("tool_choice_mode", "auto")
        return value if value in ("auto", "ask") else "auto"

    @tool_choice_mode.setter
    def tool_choice_mode(self, value: str) -> None:
        self._set("tool_choice_mode", value if value in ("auto", "ask") else "auto")

    @property
    def provider(self) -> str:
        """The selected LLM provider preset label, one key of the settings
        dialog's preset table; empty degrades to the local ollama seam."""
        return self._get("provider", "local-ollama") or "local-ollama"

    @provider.setter
    def provider(self, value: str) -> None:
        self._set("provider", str(value).strip() or "local-ollama")

    @property
    def model_id(self) -> str:
        """The per-turn model id ridden on the user-message payload. Empty
        means the agent's own env default, so an unset picker changes nothing;
        switching model within a provider is live."""
        return self._get("model_id", "")

    @model_id.setter
    def model_id(self, value: str) -> None:
        self._set("model_id", value.strip())

    @property
    def openrouter_api_key(self) -> str:
        """The provider API key. NEVER logged and never sent over the
        websocket: no per-message carrier exists, and a live key on the wire
        would be a security hole."""
        return self._get("openrouter_api_key", "")

    @openrouter_api_key.setter
    def openrouter_api_key(self, value: str) -> None:
        self._set("openrouter_api_key", value.strip())

    # -- derived --------------------------------------------------------------- #

    def effective_url(self) -> str:
        return self.local_url

    def effective_token(self) -> str:
        """The optional shared tailnet token (``""`` = OFF, the default)."""
        return self.token
