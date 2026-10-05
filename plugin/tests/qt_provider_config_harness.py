"""Qt harness: the settings dialog's save payload, model list and key entry.

Runs offscreen under the ``qgis.PyQt`` interpreter against a recording
``http.server`` stub for the agent routes. The construction-time fetch
repopulates the model combo from the stub's ids, the off-thread POST carries the
preset and model but never a key, and the ONE key entry stores its key in the
credential store and pushes it over ``secret-add``. The key is NEVER printed."""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from qgis.PyQt.QtWidgets import QApplication, QLineEdit, QWidget  # noqa: E402

from plugin.ui.settings_dialog import (  # noqa: E402
    LANGUAGE_MODEL_CREDENTIAL,
    PROVIDER_PRESETS,
    SettingsDialog,
)
from plugin.plugin_settings import PluginSettings  # noqa: E402

_API_KEY = "sk-or-HARNESS-SECRET"
# Deliberately DISTINCT from the static openrouter-free shortlist so a match
# proves the LIVE fetch repopulated the combo (not the static fallback).
_LIVE_IDS = ["zzz/live-model-a:free", "zzz/live-model-b:free"]


class _Stub(http.server.BaseHTTPRequestHandler):
    last_post_payload = None

    def _json(self, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        if self.path != "/api/provider-config":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            _Stub.last_post_payload = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            _Stub.last_post_payload = None
        self._json(200, {"ok": True, "model": "m", "base_url_host": "openrouter.ai"})

    def do_GET(self):  # noqa: N802
        if self.path == "/api/library":
            self._json(200, {"credentials": [
                {"name": "airnow", "label": "EPA AirNow", "env_var": "X"}]})
            return
        if self.path != "/api/local-models":
            self._json(404, {"error": "not found"})
            return
        self._json(
            200,
            {
                "models": [{"id": i, "label": f"{i} (free)"} for i in _LIVE_IDS],
                "default": _LIVE_IDS[0],
            },
        )

    def log_message(self, *a):
        pass


def _wait(app, predicate, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


class _Broker:
    """The credential store's stand-in: names and values in memory."""

    def __init__(self):
        self.stored: dict = {}

    def stored_names(self):
        return frozenset(self.stored)

    def remember(self, name, value):
        self.stored[name] = value
        return True


class _Bridge:
    running = True

    def __init__(self):
        self.pushed: list = []

    def push_secret(self, name, value):
        self.pushed.append((name, value))


class _Dock(QWidget):
    def __init__(self, base):
        super().__init__()
        self.bridge = _Bridge()
        self._base = base

    def _effective_http_base(self):
        return self._base


def main() -> int:
    app = QApplication(sys.argv)

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    # Isolate QSettings so the harness never touches a real profile.
    from qgis.PyQt.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.IniFormat)
    QApplication.setOrganizationName("trid3nt-test")
    QApplication.setApplicationName("harness")

    settings = PluginSettings()
    settings.export_api = base
    settings.provider = "openrouter-free"
    settings.model_id = "meta-llama/llama-3.3-70b-instruct:free"

    dock = _Dock(base)
    dlg = SettingsDialog(settings, dock)
    dlg._broker = _Broker()

    # 1) The construction-time live fetch must repopulate the combo with the
    #    stub's DISTINCT free ids.
    def _repopulated():
        items = [dlg.model_combo.itemText(i) for i in range(dlg.model_combo.count())]
        return _LIVE_IDS[0] in items

    if not _wait(app, _repopulated):
        items = [dlg.model_combo.itemText(i) for i in range(dlg.model_combo.count())]
        print("MODEL_REPOPULATE_FAIL", items)
        return 1
    # The user's typed model id is preserved across the repopulate.
    if dlg.model_combo.currentText() != "meta-llama/llama-3.3-70b-instruct:free":
        print("MODEL_PRESERVE_FAIL", dlg.model_combo.currentText())
        return 1
    print("MODEL_REPOPULATE_OK")

    # 2) ONE key entry replaces the list of asked-for keys: it names the
    #    language model first, gains the daemon's declared credentials, and the
    #    dialog carries no other key field.
    if not _wait(app, lambda: dlg.key_for_combo.count() == 2):
        print("KEY_ENTRY_FAIL choices", dlg.key_for_combo.count())
        return 1
    choices = [dlg.key_for_combo.itemData(i) for i in range(dlg.key_for_combo.count())]
    secret_fields = [e for e in dlg.findChildren(QLineEdit)
                     if e.echoMode() == QLineEdit.EchoMode.Password]
    if (choices != [LANGUAGE_MODEL_CREDENTIAL, "airnow"]
            or secret_fields != [dlg.token_edit, dlg.key_edit]
            or hasattr(dlg, "provider_key_edit") or hasattr(dlg, "keys_group")
            or hasattr(settings, "openrouter_api_key")):
        print("KEY_ENTRY_FAIL", choices, len(secret_fields))
        return 1
    print("ONE_KEY_ENTRY_OK")
    dlg.key_for_combo.setCurrentIndex(0)
    dlg.key_edit.setText(_API_KEY)

    # 3) Save -> off-thread POST with the preset payload shape, and the key to
    #    the store and over secret-add.
    dlg.accept()
    if (dlg._broker.stored != {LANGUAGE_MODEL_CREDENTIAL: _API_KEY}
            or dock.bridge.pushed != [(LANGUAGE_MODEL_CREDENTIAL, _API_KEY)]):
        print("KEY_SAVE_FAIL", sorted(dlg._broker.stored), len(dock.bridge.pushed))
        return 1
    print("KEY_SAVE_OK")
    if not _wait(app, lambda: _Stub.last_post_payload is not None):
        print("SAVE_PAYLOAD_FAIL none")
        return 1
    payload = _Stub.last_post_payload
    preset = PROVIDER_PRESETS["openrouter-free"]
    expected = {
        "base_url": preset["base_url"],
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "num_ctx": preset["num_ctx"],
    }
    if payload != expected:
        # Never print the key -- redact before surfacing a mismatch.
        redacted = dict(payload or {})
        if "api_key" in redacted:
            redacted["api_key"] = "<redacted:%s>" % (
                "present" if redacted["api_key"] else "empty"
            )
        print("SAVE_PAYLOAD_FAIL", redacted)
        return 1
    print("SAVE_PAYLOAD_OK")

    httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
