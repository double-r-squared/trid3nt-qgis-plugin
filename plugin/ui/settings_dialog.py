"""TRID3NT settings dialog, the provider preset table, and the one key entry.

APPLY-ON-SAVE: nothing a field carries takes effect until Save, where every
field copies into ``settings`` in one place. A key is the one exception to that
store: it goes to QgsAuthManager, never to QSettings."""
from __future__ import annotations

from typing import List, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QWidget,
)

from ..plugin_settings import PluginSettings
from ..net.auth_broker import LANGUAGE_MODEL_CREDENTIAL, AuthBroker, key_entries
from ..net.tasks import _KeyedSourcesTask, _ModelListTask, _ProviderConfigTask, _SettingsTask


#: What a Keys entry says and the colour it is drawn in: red the provider refused it, green stored, grey no key.
_KEY_STATE_WORDS = {"rejected": "rejected", "stored": "stored", "none": "no key"}
_KEY_STATE_COLOURS = {"rejected": "#c0392b", "stored": "#1e8449", "none": "#7f8c8d"}

# Provider presets: label -> base_url, a model shortlist and the num_ctx the agent should set.
# The model combo is EDITABLE; the shortlist sticks to ids known to honor tool-calling.
PROVIDER_PRESETS: dict = {
    "local-ollama": {
        "base_url": "http://127.0.0.1:11434/v1",
        "num_ctx": "24576",
        "models": [
            "qwen3:8b-24k",
            "qwen2.5:7b",
            "llama3.1:8b",
        ],
    },
    "openrouter-free": {
        "base_url": "https://openrouter.ai/api/v1",
        "num_ctx": "32768",
        "models": [
            "meta-llama/llama-3.3-70b-instruct:free",
            "qwen/qwen-2.5-72b-instruct:free",
            "mistralai/mistral-small-3.1-24b-instruct:free",
        ],
    },
    "openrouter-paid": {
        "base_url": "https://openrouter.ai/api/v1",
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
        "num_ctx": "128000",
        "models": [
            "gpt-4o-mini",
            "gpt-4o",
        ],
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "num_ctx": "32768",
        "models": [
            "llama-3.3-70b-versatile",
            "qwen-2.5-32b",
        ],
    },
}


class SettingsDialog(QDialog):
    """The server URL and token, the basemap, the model controls and the key."""

    def __init__(
        self,
        settings: PluginSettings,
        parent: Optional[QWidget] = None,
        on_disconnect=None,
        on_connect=None,
        connected: bool = False,
        on_charts=None,
        on_library=None,
        on_set_setting=None,
    ):
        super().__init__(parent)
        self._settings = settings
        self._on_set_setting = on_set_setting
        self._setting_rows: dict = {}
        self._settings_task: Optional["_SettingsTask"] = None
        # Keep-alive refs for model-list fetch tasks; set BEFORE _reload_model_choices runs.
        self._model_list_tasks: List["_ModelListTask"] = []
        self._keys_task: Optional["_KeyedSourcesTask"] = None
        self._broker = AuthBroker()
        self.setWindowTitle("TRID3NT settings")
        form = QFormLayout(self)

        # The server token is REQUIRED: the daemon refuses connections that present none.
        self.local_url_edit = QLineEdit(settings.local_url)
        self.local_url_edit.setPlaceholderText("ws://127.0.0.1:8765/ws")
        form.addRow("Server URL", self.local_url_edit)

        self.token_edit = QLineEdit(settings.token)
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText(
            "required -- the daemon printed it at ~/.trid3nt/access_token"
        )
        form.addRow("Server token", self.token_edit)

        # NEVER a second data-endpoint field: the :8766 base and the store endpoint are DERIVED from the handshake.

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

        # Only the MODEL rides the user-message live; the provider is pushed on Save.
        self.provider_combo = QComboBox()
        for preset_label in PROVIDER_PRESETS:
            self.provider_combo.addItem(preset_label)
        p_idx = self.provider_combo.findText(settings.provider)
        self.provider_combo.setCurrentIndex(p_idx if p_idx >= 0 else 0)
        form.addRow("Provider", self.provider_combo)

        # An empty model means the agent's env default; a switch applies on the NEXT message.
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self._reload_model_choices(settings.provider)
        self.model_combo.setCurrentText(settings.model_id)
        self.provider_combo.currentTextChanged.connect(self._reload_model_choices)
        form.addRow("Model id", self.model_combo)

        self.show_thinking_checkbox = QCheckBox("Show Reasoning")
        self.show_thinking_checkbox.setChecked(settings.show_thinking)
        form.addRow("", self.show_thinking_checkbox)

        # "auto" is autonomous selection with a picker only on a near-tie; "ask" picks every staged selection. Consent gates are NEVER mode-dependent.
        self.tool_choice_combo = QComboBox()
        self.tool_choice_combo.addItems(["auto", "ask"])
        self.tool_choice_combo.setCurrentText(settings.tool_choice_mode)
        self.tool_choice_combo.setToolTip(
            "auto: the agent picks tools itself (asks only on a near-tie). "
            "ask: confirm each step's tool from a ranked picker card."
        )
        form.addRow("Tool selection", self.tool_choice_combo)

        # One connect/disconnect toggle; it closes the dialog WITHOUT saving (an action, not a settings edit).
        self.conn_toggle_btn = QPushButton()
        if connected:
            self.conn_toggle_btn.setText("Disconnect from agent")
            self.conn_toggle_btn.setToolTip("End the current agent connection")
            self.conn_toggle_btn.setEnabled(on_disconnect is not None)
            self.conn_toggle_btn.clicked.connect(
                lambda: self._act_and_close(on_disconnect))
        else:
            self.conn_toggle_btn.setText("Connect to agent")
            self.conn_toggle_btn.setToolTip("Start the agent connection")
            self.conn_toggle_btn.setEnabled(on_connect is not None)
            self.conn_toggle_btn.clicked.connect(
                lambda: self._act_and_close(on_connect))
        form.addRow("Connection", self.conn_toggle_btn)

        # Each entry is an action and closes the dialog WITHOUT saving.
        self.charts_entry_btn = QPushButton("Display charts")
        self.charts_entry_btn.setEnabled(on_charts is not None)
        self.charts_entry_btn.clicked.connect(lambda: self._act_and_close(on_charts))
        self.library_entry_btn = QPushButton("Search library")
        self.library_entry_btn.setEnabled(on_library is not None)
        self.library_entry_btn.clicked.connect(lambda: self._act_and_close(on_library))
        views_row = QHBoxLayout()
        views_row.addWidget(self.charts_entry_btn)
        views_row.addWidget(self.library_entry_btn)
        form.addRow("Views", views_row)

        # One scroll list of every keyed source: refused keys first, stored keys next, sources with no key
        # last. A stored key is reported as stored and NEVER read back.
        self._declared: dict = {}
        self.keys_list = QListWidget()
        self.keys_list.setMaximumHeight(150)
        self.keys_list.currentItemChanged.connect(self._show_key_entry)
        form.addRow("Keys", self.keys_list)
        self.key_state_lbl = QLabel("")
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_set_btn = QPushButton("Set key")
        self.key_set_btn.clicked.connect(self._set_key)
        self.key_remove_btn = QPushButton("Remove")
        self.key_remove_btn.clicked.connect(self._remove_key)
        self._key_detail = QWidget()
        detail = QHBoxLayout(self._key_detail)
        detail.setContentsMargins(0, 0, 0, 0)
        detail.addWidget(self.key_state_lbl)
        detail.addWidget(self.key_edit, 1)
        detail.addWidget(self.key_set_btn)
        detail.addWidget(self.key_remove_btn)
        self._key_detail.setVisible(False)
        form.addRow("", self._key_detail)
        self._refresh_keys()
        self._load_keyed_sources()

        # One scroll list of every changeable setting, read off the daemon. Set and Reset ask the agent to run set_config, which opens its card in the chat.
        self.config_list = QListWidget()
        self.config_list.setMaximumHeight(150)
        self.config_list.currentItemChanged.connect(self._show_setting)
        form.addRow("Settings", self.config_list)
        self.config_help_lbl = QLabel("")
        self.config_help_lbl.setWordWrap(True)
        self.config_edit = QLineEdit()
        self.config_set_btn = QPushButton("Set")
        self.config_set_btn.clicked.connect(self._set_setting)
        self.config_reset_btn = QPushButton("Reset")
        self.config_reset_btn.clicked.connect(self._reset_setting)
        self._config_detail = QWidget()
        config_detail = QHBoxLayout(self._config_detail)
        config_detail.setContentsMargins(0, 0, 0, 0)
        config_detail.addWidget(self.config_help_lbl, 2)
        config_detail.addWidget(self.config_edit, 1)
        config_detail.addWidget(self.config_set_btn)
        config_detail.addWidget(self.config_reset_btn)
        self._config_detail.setVisible(False)
        form.addRow("", self._config_detail)
        self._load_settings()

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
        # The tool-selection mode rides the next user-message; no push.
        self._settings.tool_choice_mode = self.tool_choice_combo.currentText()
        # Provider and model persist, then the live config is PUSHED to the agent.
        self._settings.provider = self.provider_combo.currentText()
        self._settings.model_id = self.model_combo.currentText()
        self._push_provider_config()
        self._set_key()
        super().accept()

    def _load_settings(self) -> None:
        """Read the daemon's settings off-thread so a dead agent never freezes the dialog."""
        task = _SettingsTask(self._resolve_http_base(), self)
        self._settings_task = task
        task.finished.connect(self._on_settings)
        task.errored.connect(self._on_settings_errored)
        task.start()

    def _on_settings(self, rows: list) -> None:
        try:
            self._setting_rows = {row["name"]: row for row in rows}
            self.config_list.blockSignals(True)
            self.config_list.clear()
            for row in rows:
                item = QListWidgetItem(f"{row['family']}: {row['name']} = {row['value']} ({row['level']})")
                item.setData(Qt.ItemDataRole.UserRole, row["name"])
                item.setToolTip(f"{row['help']}\nDefault {row['default']}. Source: {row['source']}")
                if row["level"] != "default":
                    item.setForeground(QColor(_KEY_STATE_COLOURS["stored"]))
                self.config_list.addItem(item)
            self.config_list.blockSignals(False)
        except RuntimeError:
            return  # the dialog closed mid-fetch

    def _on_settings_errored(self, message: str) -> None:
        try:
            self.config_list.setToolTip(f"Settings are not listed: {message}")
        except RuntimeError:
            return

    def _selected_setting(self) -> Optional[dict]:
        item = self.config_list.currentItem()
        return self._setting_rows.get(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None

    def _show_setting(self, item, *_previous) -> None:
        """Click a setting: what it decides, its range and the field its value is typed in."""
        row = self._selected_setting()
        self._config_detail.setVisible(row is not None)
        if row is None:
            return
        bounds = row.get("bounds")
        range_text = f" ({bounds[0]:g} to {bounds[1]:g} {row['unit']})" if bounds and bounds[1] < 1e11 else (
            f" (at least {bounds[0]:g} {row['unit']})" if bounds else (f" ({row['unit']})" if row["unit"] else ""))
        choices = f" One of: {', '.join(str(c) for c in row['choices'])}." if row.get("choices") else ""
        self.config_help_lbl.setText(f"{row['help']}{range_text}{choices}")
        self.config_edit.setText(str(row["value"]))
        self.config_reset_btn.setEnabled(row["level"] != "default")

    def _typed_setting(self, row: dict, text: str):
        """The typed text as the setting's own type, or ``None`` where it does not parse (the agent refuses a value out of range on its own card)."""
        try:
            if row["type"] == "INTEGER":
                return int(text)
            if row["type"] == "REAL":
                return float(text)
        except ValueError:
            return None
        return text.strip() or None

    def _set_setting(self) -> None:
        row = self._selected_setting()
        value = self._typed_setting(row, self.config_edit.text()) if row else None
        if row is None or value is None or self._on_set_setting is None:
            return
        self._act_and_close(lambda: self._on_set_setting(row["name"], value, False))

    def _reset_setting(self) -> None:
        row = self._selected_setting()
        if row is None or self._on_set_setting is None:
            return
        self._act_and_close(lambda: self._on_set_setting(row["name"], None, True))

    def _load_keyed_sources(self) -> None:
        """Add the credentials the daemon's rows declare to the entry's choices, off-thread so
        a dead agent never freezes the dialog."""
        task = _KeyedSourcesTask(self._resolve_http_base(), self)
        self._keys_task = task
        task.finished.connect(self._on_keyed_sources)
        task.errored.connect(self._on_keyed_sources_errored)
        task.start()

    def _on_keyed_sources(self, rows: list) -> None:
        try:
            self._declared = {row["name"]: row for row in rows}
            self._refresh_keys()
        except RuntimeError:
            return  # the dialog closed mid-fetch

    def _on_keyed_sources_errored(self, message: str) -> None:
        try:
            self.keys_list.setToolTip(f"Data-source keys are not listed: {message}")
        except RuntimeError:
            return

    def _refresh_keys(self) -> None:
        """Rebuild the list from what is stored, refused and asked for, keeping the selected entry."""
        keep = self._selected_key()
        entries = key_entries({name: row["label"] for name, row in self._declared.items()}, self._settings.asked_keys,
                              self._broker.stored_names(), self._settings.rejected_keys)
        self.keys_list.blockSignals(True)
        self.keys_list.clear()
        for entry in entries:
            word = _KEY_STATE_WORDS[entry["state"]]
            item = QListWidgetItem(f"{entry['label']} - {word}")
            item.setData(Qt.ItemDataRole.UserRole, entry["name"])
            item.setData(Qt.ItemDataRole.UserRole + 1, entry["state"])
            item.setForeground(QColor(_KEY_STATE_COLOURS[entry["state"]]))
            declared = self._declared.get(entry["name"])
            if declared is not None:
                item.setToolTip(f"a development run also reads {declared['env_var']}")
            self.keys_list.addItem(item)
            if entry["name"] == keep:
                self.keys_list.setCurrentItem(item)
        self.keys_list.blockSignals(False)
        self._show_key_entry(self.keys_list.currentItem())

    def _selected_key(self) -> Optional[str]:
        item = self.keys_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _show_key_entry(self, item, *_previous) -> None:
        """Click an entry: the key field, and Remove where a key is stored."""
        self.key_edit.clear()
        self._key_detail.setVisible(item is not None)
        if item is None:
            return
        state = item.data(Qt.ItemDataRole.UserRole + 1)
        self.key_state_lbl.setText(_KEY_STATE_WORDS[state])
        self.key_edit.setPlaceholderText("type to replace" if state != "none" else "paste the key")
        self.key_remove_btn.setVisible(state != "none")

    def _set_key(self) -> None:
        """Store the typed key in QgsAuthManager and push it to the agent now; every later connect
        pushes it again. SECURITY: the value goes to the auth manager and ``secret-add`` only."""
        value = self.key_edit.text().strip()
        name = self._selected_key()
        if not value or name is None:
            return
        self._broker.remember(name, value)
        self._settings.clear_key_rejection(name)
        self.key_edit.clear()
        bridge = getattr(self.parent(), "bridge", None)
        if bridge is not None and bridge.running:
            bridge.push_secret(name, value)
        self._refresh_keys()

    def _remove_key(self) -> None:
        """Delete the stored key from QgsAuthManager; the agent drops its copy when the session ends."""
        name = self._selected_key()
        if name is None:
            return
        self._broker.forget(name)
        self._settings.clear_key_rejection(name)
        self._refresh_keys()

    def _act_and_close(self, action) -> None:
        """Run one of the dock's actions, then close WITHOUT saving: an action is not a
        settings edit."""
        if action is not None:
            action()
        self.reject()

    def _resolve_http_base(self) -> str:
        """The agent's HTTP base for this dialog's two calls."""
        dock = self.parent()
        effective = getattr(dock, "_effective_http_base", None)
        if callable(effective):
            return effective()
        return self._settings.export_api

    def _push_provider_config(self) -> None:
        """POST the persisted provider config OFF-THREAD, so a dead agent never freezes Save.
        The key never rides it: keys go over ``secret-add``."""
        preset = PROVIDER_PRESETS.get(self._settings.provider) or {}
        payload = {
            "base_url": preset.get("base_url", ""),
            "model": self._settings.model_id,
            "num_ctx": preset.get("num_ctx", ""),
        }
        dock = self.parent()
        task = _ProviderConfigTask(self._resolve_http_base(), payload, dock)
        # Own the task on the DOCK so its thread outlives ``accept()``.
        if dock is not None and hasattr(dock, "_provider_config_tasks"):
            dock._provider_config_tasks.append(task)
            task.finished.connect(dock._on_provider_config_finished)
            task.errored.connect(dock._on_provider_config_errored)
        task.start()

    def _reload_model_choices(self, provider: str) -> None:
        """Swap the model combo's dropdown to ``provider``'s shortlist WITHOUT clobbering
        whatever the user has typed: only the item list changes."""
        preset = PROVIDER_PRESETS.get(provider) or {}
        current_text = self.model_combo.currentText()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for model in preset.get("models", []):
            self.model_combo.addItem(model)
        self.model_combo.setCurrentText(current_text)
        self.model_combo.blockSignals(False)
        # For OpenRouter, fetch the LIVE list off-thread; the static shortlist is the fallback.
        preset_base_url = (preset.get("base_url") or "").lower()
        if "openrouter.ai" in preset_base_url:
            task = _ModelListTask(self._resolve_http_base(), provider, self)
            self._model_list_tasks.append(task)
            task.finished.connect(self._on_model_list_finished)
            task.errored.connect(self._on_model_list_errored)
            task.start()

    def _on_model_list_finished(self, ids: list, provider: str) -> None:
        """Repopulate the model combo with the LIVE ids."""
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
            # Combo destroyed (dialog closed mid-fetch).
            return

    def _on_model_list_errored(self, message: str) -> None:
        # A live-fetch failure is silent by design: the static shortlist is already in the combo.
        return
