"""TRID3NT settings dialog + provider preset table.

Split out of dock.py (2026-07-21 flat->package restructure). Behavior identical.
"""
from __future__ import annotations

from typing import List, Optional

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QWidget,
)

from ..plugin_settings import MODE_LOCAL, MODE_REMOTE, PluginSettings
from ..net.tasks import _ModelListTask, _ProviderConfigTask




# OpenRouter model-extensibility (design 2026-07-19). Static provider preset
# table -- label -> the agent-process ENV a given provider needs (base_url +
# key-env NAME) + a curated TOOL-CAPABLE model shortlist + the num_ctx the
# agent should set so the context-clip guard does not false-trip. The plugin
# CANNOT inject the agent's env (base_url/key/num_ctx live in the agent
# process, set via .env.local + restart); this table is the picker's source
# of truth AND documents exactly which env vars a provider switch requires,
# so the "restart to apply" note is honest rather than hand-wavy. ``models``
# is a curated shortlist only -- the model combo is EDITABLE so the user can
# paste ANY id the provider serves. The agent is tool-heavy (tool_choice=auto
# every round); many free models ignore tools and narrate a fake answer, so
# the shortlist sticks to ids known to honor tool-calling (design "Risks").
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
    """Mode local/remote, URLs, pasted token, MinIO + export API endpoints,
    AOI toggles, and the auto-basemap toggle.

    Item 4 (live-feedback 2026-07-09): the AOI checkboxes (canvas / selected
    polygon) used to live-apply straight from the dock; they now live here
    ONLY, and nothing applies until Save -- every field, line edits and
    checkboxes alike, copies into ``settings`` in the ``accept()`` branch.
    """

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
        # Item B3 (qgis-ux-batch 2026-07-19) + NATE 2026-07-20: the dock's
        # connect + disconnect paths + the live connection state, so BOTH
        # actions live here (off the header button row) and each is enabled
        # only in the state where it applies.
        self._on_disconnect = on_disconnect
        self._on_connect = on_connect
        # Keep-alive refs for the OpenRouter free-model fetch tasks (design
        # 2026-07-19) -- initialised BEFORE _reload_model_choices runs below.
        self._model_list_tasks: List["_ModelListTask"] = []
        self.setWindowTitle("TRID3NT settings")
        form = QFormLayout(self)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems([MODE_LOCAL, MODE_REMOTE])
        self.mode_combo.setCurrentText(settings.mode)
        form.addRow("Mode", self.mode_combo)

        self.local_url_edit = QLineEdit(settings.local_url)
        form.addRow("Local agent URL", self.local_url_edit)

        self.remote_url_edit = QLineEdit(settings.remote_url)
        self.remote_url_edit.setPlaceholderText("wss://<host>/ws")
        form.addRow("Remote agent URL", self.remote_url_edit)

        self.token_edit = QLineEdit(settings.token)
        self.token_edit.setEchoMode(QLineEdit.Password)
        self.token_edit.setPlaceholderText("paste bearer token (remote mode)")
        form.addRow("Remote token", self.token_edit)
        # S2 (NATE 2026-07-20): the "Get token help" label row is REMOVED (the
        # explanation moves to a future Help page -- keep the dialog terse).

        self.minio_edit = QLineEdit(settings.minio_endpoint)
        form.addRow("Local MinIO endpoint", self.minio_edit)

        self.export_api_edit = QLineEdit(settings.export_api)
        form.addRow("Local export API", self.export_api_edit)
        # S3 (NATE 2026-07-20): the big export/anonymous NOTE under "Local export
        # API" is REMOVED (the explanation moves to a future Help page).

        # S7 (NATE 2026-07-20): the AOI selector checkboxes (canvas / selected
        # polygon) are REMOVED -- the canvas-as-AOI path is gone (A2). The AOI is
        # now the explicit Set-AOI rectangle OR the agent geocodes it.

        # S5 (NATE 2026-07-20): the basemap PRESET picker (half width) sits on
        # ONE row with the "Add basemap automatically" toggle to its RIGHT --
        # was two separate rows (auto-basemap row + basemap-combo row).
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

        # OpenRouter model-extensibility (design 2026-07-19): provider + api
        # key + model picker. Only the MODEL rides the user-message live
        # (mirrors show_thinking); PROVIDER (base_url) + api-key are agent
        # process env the plugin cannot inject, so those two persist here and
        # need an agent restart -- the note below is honest about that.
        self.provider_combo = QComboBox()
        for preset_label in PROVIDER_PRESETS:
            self.provider_combo.addItem(preset_label)
        p_idx = self.provider_combo.findText(settings.provider)
        self.provider_combo.setCurrentIndex(p_idx if p_idx >= 0 else 0)
        form.addRow("Provider", self.provider_combo)

        # SECRET: password echo, mirrors token_edit; NEVER logged. This is the
        # agent's TRID3NT_OPENAI_API_KEY (OPENROUTER_API_KEY / OPENAI_API_KEY /
        # GROQ_API_KEY per preset) -- persisted here, applied on agent restart;
        # never sent over the WS (no per-message carrier, and a live key must
        # not leak onto the wire).
        self.provider_key_edit = QLineEdit(settings.openrouter_api_key)
        self.provider_key_edit.setEchoMode(QLineEdit.Password)
        self.provider_key_edit.setPlaceholderText(
            "provider API key (OpenRouter / OpenAI / Groq)"
        )
        form.addRow("Provider API key", self.provider_key_edit)

        # EDITABLE combo pre-filled with the curated tool-capable shortlist for
        # the selected provider -- editable so the user can paste ANY model id.
        # Empty text = the agent's env default (TRID3NT_OPENAI_MODEL); a MODEL
        # switch applies on the NEXT message with no restart.
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self._reload_model_choices(settings.provider)
        self.model_combo.setCurrentText(settings.model_id)
        # Repopulate the shortlist when the provider changes (keeps whatever
        # the user has typed -- only the dropdown items swap).
        self.provider_combo.currentTextChanged.connect(self._reload_model_choices)
        form.addRow("Model id", self.model_combo)

        # S6 (NATE 2026-07-20): "Show model thinking" -> "Show Reasoning",
        # relocated to sit directly UNDER the "Model id" row (with the model
        # controls, not up in the generic section). Still bound to
        # settings.show_thinking on Save.
        self.show_thinking_checkbox = QCheckBox("Show Reasoning")
        self.show_thinking_checkbox.setChecked(settings.show_thinking)
        form.addRow("", self.show_thinking_checkbox)

        # S3 (NATE 2026-07-20): the long provider_note ("On Save ...") is
        # REMOVED -- keep the dialog terse (explanation -> future Help page).

        # S4 (NATE 2026-07-20): Connect/Disconnect collapse to ONE toggle
        # button -- its TEXT + action depend on the connection state at build
        # time. Connected -> "Disconnect from agent" (runs the disconnect
        # teardown); disconnected -> "Connect to agent" (runs the connect path).
        # Both close the dialog WITHOUT saving (an action, not a settings edit,
        # so it does not also push provider config).
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

        # S1 (NATE 2026-07-20): the remote-only rows (Remote agent URL + Remote
        # token) show ONLY in REMOTE mode -- hidden in LOCAL. Toggled live off
        # the mode combo and seeded from the current mode. Hiding a QFormLayout
        # row hides BOTH the field and its label (labelForField).
        self._form = form
        self._remote_rows = [self.remote_url_edit, self.token_edit]
        self.mode_combo.currentTextChanged.connect(
            self._apply_mode_field_visibility
        )
        self._apply_mode_field_visibility(self.mode_combo.currentText())

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _apply_mode_field_visibility(self, mode: str) -> None:
        """S1 (NATE 2026-07-20): remote-only rows visible only in REMOTE mode
        -- hide the field AND its label in LOCAL (labelForField)."""
        show = mode == MODE_REMOTE
        for field in self._remote_rows:
            field.setVisible(show)
            label = self._form.labelForField(field)
            if label is not None:
                label.setVisible(show)

    def accept(self) -> None:
        self._settings.mode = self.mode_combo.currentText()
        self._settings.local_url = self.local_url_edit.text()
        self._settings.remote_url = self.remote_url_edit.text()
        self._settings.token = self.token_edit.text()
        self._settings.minio_endpoint = self.minio_edit.text()
        self._settings.export_api = self.export_api_edit.text()
        # S7 (NATE 2026-07-20): canvas_aoi / selection_aoi checkboxes removed --
        # no writes here (the canvas-as-AOI path is gone, A2).
        self._settings.auto_basemap = self.auto_basemap_checkbox.isChecked()
        self._settings.basemap_preset = self.basemap_combo.currentText()
        self._settings.show_thinking = self.show_thinking_checkbox.isChecked()
        # OpenRouter model-extensibility (design 2026-07-19): persist provider
        # + key + model, then PUSH the live config to the agent so a
        # provider/key switch applies on the NEXT message with no restart
        # (Feature 3). model_id also rides the user-message live.
        self._settings.provider = self.provider_combo.currentText()
        self._settings.openrouter_api_key = self.provider_key_edit.text()
        self._settings.model_id = self.model_combo.currentText()
        self._push_provider_config()
        super().accept()

    def _disconnect_and_close(self) -> None:
        """Item B3 (qgis-ux-batch 2026-07-19): run the dock's disconnect path
        then close the dialog without saving (an action, not a settings edit)."""
        if self._on_disconnect is not None:
            self._on_disconnect()
        self.reject()

    def _connect_and_close(self) -> None:
        """NATE 2026-07-20: run the dock's connect path then close without
        saving (an action, not a settings edit) -- the Connect twin of the
        Disconnect button, both off the header row."""
        if self._on_connect is not None:
            self._on_connect()
        self.reject()

    def _push_provider_config(self) -> None:
        """POST the persisted provider config to the agent OFF-THREAD (Feature
        3, design 2026-07-19) so a dead/asleep agent HTTP listener never freezes
        this dialog on Save. The base_url + num_ctx come from the provider
        preset (the plugin's source of truth for what env a provider needs);
        the api_key is the persisted secret. The honest result lands on the
        DOCK status (this dialog is closing), routed via the parent dock's
        handlers. SECURITY: the api_key rides the POST body but is NEVER logged
        here or in the client helper."""
        preset = PROVIDER_PRESETS.get(self._settings.provider) or {}
        payload = {
            "base_url": preset.get("base_url", ""),
            "api_key": self._settings.openrouter_api_key,
            "model": self._settings.model_id,
            "num_ctx": preset.get("num_ctx", ""),
        }
        dock = self.parent()
        task = _ProviderConfigTask(self._settings.export_api, payload, dock)
        # Own the task on the DOCK (not this closing dialog) so the daemon
        # thread + QObject outlive accept() -- mirrors _case_list_tasks.
        if dock is not None and hasattr(dock, "_provider_config_tasks"):
            dock._provider_config_tasks.append(task)
            task.finished.connect(dock._on_provider_config_finished)
            task.errored.connect(dock._on_provider_config_errored)
        task.start()

    def _reload_model_choices(self, provider: str) -> None:
        """Swap the model combo's dropdown to the curated shortlist for
        ``provider`` WITHOUT clobbering whatever the user has typed (the combo
        is editable -- only the item list changes, the edit text is preserved).
        Bound to the provider combo's ``currentTextChanged`` and called once at
        construction."""
        preset = PROVIDER_PRESETS.get(provider) or {}
        current_text = self.model_combo.currentText()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for model in preset.get("models", []):
            self.model_combo.addItem(model)
        self.model_combo.setCurrentText(current_text)
        self.model_combo.blockSignals(False)
        # Feature 2 (design 2026-07-19): for an OpenRouter preset, fetch the
        # LIVE free + tool-capable model list off the UI thread and swap it in
        # on success; the static shortlist above stays as the honest fallback
        # on any error/timeout. Combo stays editable, so any id is typeable.
        base_url = (preset.get("base_url") or "").lower()
        if "openrouter.ai" in base_url:
            task = _ModelListTask(self._settings.export_api, provider, self)
            self._model_list_tasks.append(task)
            task.finished.connect(self._on_model_list_finished)
            task.errored.connect(self._on_model_list_errored)
            task.start()

    def _on_model_list_finished(self, ids: list, provider: str) -> None:
        """Repopulate the model combo with the LIVE free-model ids (Feature 2).
        Stale-guarded: the user may have switched provider again while the
        fetch was in flight, so only apply for the CURRENT provider. Preserves
        whatever the user has typed (editable combo -- only items swap)."""
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
