"""TRID3NT settings dialog + provider preset table.

APPLY-ON-SAVE: nothing a field carries takes effect until Save, where every
field copies into ``settings`` in one place."""
from __future__ import annotations

from typing import List, Optional

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QWidget,
)

from ..plugin_settings import PluginSettings
from ..net.tasks import _ModelListTask, _ProviderConfigTask




# Static provider preset table: label -> the agent-process ENV that provider
# needs (base_url and the key-env NAME), a curated model shortlist, and the
# num_ctx the agent should set so the context-clip guard does not false-trip.
# The plugin CANNOT inject the agent's env, so naming the env vars here is
# what makes the "restart to apply" note honest rather than hand-wavy.
#
# ``models`` is a shortlist only -- the model combo is EDITABLE, so any id the
# provider serves is typeable. The agent is tool-heavy and many free models
# ignore tools and narrate a fake answer instead, so the shortlist sticks to
# ids known to honor tool-calling.
PROVIDER_PRESETS: dict = {
    "local-ollama": {
        "base_url": "http://127.0.0.1:11434/v1",
        "key_env": "",  # not needed for a local ollama seam
        "num_ctx": "24576",
        "models": [
            "qwen3:8b-24k",
            "qwen2.5:7b",
            "llama3.1:8b",
        ],
    },
    "openrouter-free": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "num_ctx": "32768",
        "models": [
            "meta-llama/llama-3.3-70b-instruct:free",
            "qwen/qwen-2.5-72b-instruct:free",
            "mistralai/mistral-small-3.1-24b-instruct:free",
        ],
    },
    "openrouter-paid": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "num_ctx": "65536",
        "models": [
            "deepseek/deepseek-chat",
            "meta-llama/llama-3.3-70b-instruct",
            "qwen/qwen-2.5-72b-instruct",
            "mistralai/mistral-large",
        ],
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "num_ctx": "128000",
        "models": [
            "gpt-4o-mini",
            "gpt-4o",
        ],
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "num_ctx": "32768",
        "models": [
            "llama-3.3-70b-versatile",
            "qwen-2.5-32b",
        ],
    },
}


class SettingsDialog(QDialog):
    """The server URL and token, the basemap, and the model controls. Nothing
    applies until Save: every field, line edits and checkboxes alike, copies
    into ``settings`` in ``accept()``."""

    def __init__(
        self,
        settings: PluginSettings,
        parent: Optional[QWidget] = None,
        on_disconnect=None,
        on_connect=None,
        connected: bool = False,
    ):
        super().__init__(parent)
        self._settings = settings
        # Connect and disconnect both live HERE rather than on the header row,
        # each enabled only in the state where it applies.
        self._on_disconnect = on_disconnect
        self._on_connect = on_connect
        # Keep-alive refs for the live model-list fetch tasks, initialised
        # BEFORE _reload_model_choices runs below.
        self._model_list_tasks: List["_ModelListTask"] = []
        self.setWindowTitle("TRID3NT settings")
        form = QFormLayout(self)

        # "Server" section: the agent runs locally or on a tailnet peer,
        # reached over ws://. One "Server URL" row, plus an optional shared
        # token (OFF by default; only needed when the daemon opts into the
        # TRID3NT_ACCESS_TOKEN gate -- the tailnet itself is the trust boundary).
        self.local_url_edit = QLineEdit(settings.local_url)
        self.local_url_edit.setPlaceholderText("ws://127.0.0.1:8765/ws")
        form.addRow("Server URL", self.local_url_edit)

        self.token_edit = QLineEdit(settings.token)
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("optional shared tailnet token")
        form.addRow("Server token (optional)", self.token_edit)

        # There is NEVER a second data-endpoint field. Pointing this one URL at
        # a tailnet peer is the whole remote-daemon story: the agent's :8766
        # base and the object store's endpoint are DERIVED from the handshake,
        # with a WS-host fallback, never configured by hand.

        from ..render.layers import BASEMAP_PRESETS
        self.basemap_combo = QComboBox()
        for preset_name in BASEMAP_PRESETS:
            self.basemap_combo.addItem(preset_name)
        idx = self.basemap_combo.findText(settings.basemap_preset)
        self.basemap_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.basemap_combo.setMaximumWidth(160)  # ~half width; toggle rides right
        self.auto_basemap_checkbox = QCheckBox("Add basemap automatically")
        self.auto_basemap_checkbox.setChecked(settings.auto_basemap)
        basemap_row = QHBoxLayout()
        basemap_row.addWidget(self.basemap_combo)
        basemap_row.addWidget(self.auto_basemap_checkbox, 1)
        form.addRow("Basemap", basemap_row)

        # Only the MODEL rides the user-message live. Provider and api key are
        # agent-process env the plugin cannot inject, so those two persist here
        # and take effect when the agent restarts.
        self.provider_combo = QComboBox()
        for preset_label in PROVIDER_PRESETS:
            self.provider_combo.addItem(preset_label)
        p_idx = self.provider_combo.findText(settings.provider)
        self.provider_combo.setCurrentIndex(p_idx if p_idx >= 0 else 0)
        form.addRow("Provider", self.provider_combo)

        # SECRET: password echo, NEVER logged, and never sent over the
        # websocket -- there is no per-message carrier, and a live key must not
        # leak onto the wire.
        self.provider_key_edit = QLineEdit(settings.openrouter_api_key)
        self.provider_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.provider_key_edit.setPlaceholderText(
            "provider API key (OpenRouter / OpenAI / Groq)"
        )
        form.addRow("Provider API key", self.provider_key_edit)

        # EDITABLE combo, so any model id is typeable. Empty text means the
        # agent's own env default, and a model switch applies on the NEXT
        # message with no restart.
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self._reload_model_choices(settings.provider)
        self.model_combo.setCurrentText(settings.model_id)
        # Repopulate the shortlist when the provider changes (keeps whatever
        # the user has typed -- only the dropdown items swap).
        self.provider_combo.currentTextChanged.connect(self._reload_model_choices)
        form.addRow("Model id", self.model_combo)

        self.show_thinking_checkbox = QCheckBox("Show Reasoning")
        self.show_thinking_checkbox.setChecked(settings.show_thinking)
        form.addRow("", self.show_thinking_checkbox)

        # "auto" is autonomous tool selection, with a picker only on a
        # measured near-tie; "ask" surfaces every staged selection as a picker
        # card. Consent gates are NEVER mode-dependent.
        self.tool_choice_combo = QComboBox()
        self.tool_choice_combo.addItems(["auto", "ask"])
        self.tool_choice_combo.setCurrentText(settings.tool_choice_mode)
        self.tool_choice_combo.setToolTip(
            "auto: the agent picks tools itself (asks only on a near-tie). "
            "ask: confirm each step's tool from a ranked picker card."
        )
        form.addRow("Tool selection", self.tool_choice_combo)

        # ONE toggle button whose text and action depend on the connection
        # state at build time. Either way it closes the dialog WITHOUT saving:
        # this is an action, not a settings edit, so it must not also push
        # provider config.
        self.conn_toggle_btn = QPushButton()
        if connected:
            self.conn_toggle_btn.setText("Disconnect from agent")
            self.conn_toggle_btn.setToolTip("End the current agent connection")
            self.conn_toggle_btn.setEnabled(on_disconnect is not None)
            self.conn_toggle_btn.clicked.connect(self._disconnect_and_close)
        else:
            self.conn_toggle_btn.setText("Connect to agent")
            self.conn_toggle_btn.setToolTip("Start the agent connection")
            self.conn_toggle_btn.setEnabled(on_connect is not None)
            self.conn_toggle_btn.clicked.connect(self._connect_and_close)
        form.addRow("Connection", self.conn_toggle_btn)

        self._form = form

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def accept(self) -> None:
        self._settings.local_url = self.local_url_edit.text()
        self._settings.token = self.token_edit.text()
        self._settings.auto_basemap = self.auto_basemap_checkbox.isChecked()
        self._settings.basemap_preset = self.basemap_combo.currentText()
        self._settings.show_thinking = self.show_thinking_checkbox.isChecked()
        # The tool-selection mode rides the next user-message; no restart and
        # no push.
        self._settings.tool_choice_mode = self.tool_choice_combo.currentText()
        # Provider, key and model persist, then the live config is PUSHED to
        # the agent, so a provider or key switch applies on the next message
        # with no restart.
        self._settings.provider = self.provider_combo.currentText()
        self._settings.openrouter_api_key = self.provider_key_edit.text()
        self._settings.model_id = self.model_combo.currentText()
        self._push_provider_config()
        super().accept()

    def _disconnect_and_close(self) -> None:
        """Run the dock's disconnect path, then close WITHOUT saving: this is
        an action, not a settings edit."""
        if self._on_disconnect is not None:
            self._on_disconnect()
        self.reject()

    def _connect_and_close(self) -> None:
        """Run the dock's connect path, then close WITHOUT saving: this is an
        action, not a settings edit."""
        if self._on_connect is not None:
            self._on_connect()
        self.reject()

    def _resolve_http_base(self) -> str:
        """The agent's HTTP base for this dialog's two calls. The PARENT dock
        owns the derivation; a standalone dialog with no dock falls back to the
        stored ``export_api``."""
        dock = self.parent()
        effective = getattr(dock, "_effective_http_base", None)
        if callable(effective):
            return effective()
        return self._settings.export_api

    def _push_provider_config(self) -> None:
        """POST the persisted provider config OFF-THREAD, so a dead agent
        never freezes Save. SECURITY: the api key rides the body and is NEVER
        logged."""
        preset = PROVIDER_PRESETS.get(self._settings.provider) or {}
        payload = {
            "base_url": preset.get("base_url", ""),
            "api_key": self._settings.openrouter_api_key,
            "model": self._settings.model_id,
            "num_ctx": preset.get("num_ctx", ""),
        }
        dock = self.parent()
        task = _ProviderConfigTask(self._resolve_http_base(), payload, dock)
        # Own the task on the DOCK, not on this closing dialog, so the daemon
        # thread and its QObject outlive ``accept()``.
        if dock is not None and hasattr(dock, "_provider_config_tasks"):
            dock._provider_config_tasks.append(task)
            task.finished.connect(dock._on_provider_config_finished)
            task.errored.connect(dock._on_provider_config_errored)
        task.start()

    def _reload_model_choices(self, provider: str) -> None:
        """Swap the model combo's dropdown to ``provider``'s shortlist WITHOUT
        clobbering whatever the user has typed: only the item list changes."""
        preset = PROVIDER_PRESETS.get(provider) or {}
        current_text = self.model_combo.currentText()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for model in preset.get("models", []):
            self.model_combo.addItem(model)
        self.model_combo.setCurrentText(current_text)
        self.model_combo.blockSignals(False)
        # For an OpenRouter preset, fetch the LIVE model list off the UI
        # thread and swap it in on success; the static shortlist above is the
        # honest fallback on any error or timeout.
        preset_base_url = (preset.get("base_url") or "").lower()
        if "openrouter.ai" in preset_base_url:
            task = _ModelListTask(self._resolve_http_base(), provider, self)
            self._model_list_tasks.append(task)
            task.finished.connect(self._on_model_list_finished)
            task.errored.connect(self._on_model_list_errored)
            task.start()

    def _on_model_list_finished(self, ids: list, provider: str) -> None:
        """Repopulate the model combo with the LIVE ids. STALE-GUARDED: the
        user may have switched provider mid-fetch, so this applies only for the
        provider still selected."""
        try:
            if self.provider_combo.currentText() != provider or not ids:
                return
            current_text = self.model_combo.currentText()
            self.model_combo.blockSignals(True)
            self.model_combo.clear()
            for mid in ids:
                self.model_combo.addItem(mid)
            self.model_combo.setCurrentText(current_text)
            self.model_combo.blockSignals(False)
        except RuntimeError:
            # Underlying combo was destroyed (dialog closed mid-fetch) -- the
            # static shortlist already shipped, nothing more to do.
            return

    def _on_model_list_errored(self, message: str) -> None:
        # The static PROVIDER_PRESETS shortlist is already in the combo as the
        # honest fallback -- a live-fetch failure is silent by design (no UI to
        # repaint on a possibly-closed dialog).
        return
