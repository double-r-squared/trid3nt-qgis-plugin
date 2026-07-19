"""Qt harness: SettingsDialog Save -> off-thread provider-config POST payload
shape + OpenRouter provider -> live model-list repopulate (design 2026-07-19).

Runs under the ``qgis.PyQt`` interpreter (offscreen). Spins a recording
``http.server`` stub for the agent's ``/api/provider-config`` (POST) and
``/api/local-models`` (GET) routes, points a PluginSettings ``export_api`` at
it, builds the dialog for an ``openrouter-free`` provider, and:

  * waits for the construction-time live model-list fetch to REPOPULATE the
    editable model combo with the stub's (distinct-from-static) free ids ->
    prints ``MODEL_REPOPULATE_OK``;
  * calls ``accept()`` and waits for the off-thread POST to land, asserting the
    payload = the provider preset's base_url + num_ctx plus the persisted key +
    model id -> prints ``SAVE_PAYLOAD_OK``.

Exits non-zero on any mismatch. The api key is NEVER printed.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

from trid3nt.dock import PROVIDER_PRESETS, SettingsDialog  # noqa: E402
from trid3nt.plugin_settings import PluginSettings  # noqa: E402

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
    settings.openrouter_api_key = _API_KEY
    settings.model_id = "meta-llama/llama-3.3-70b-instruct:free"

    dlg = SettingsDialog(settings, None)

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

    # 2) Save -> off-thread POST with the preset payload shape.
    dlg.accept()
    if not _wait(app, lambda: _Stub.last_post_payload is not None):
        print("SAVE_PAYLOAD_FAIL none")
        return 1
    payload = _Stub.last_post_payload
    preset = PROVIDER_PRESETS["openrouter-free"]
    expected = {
        "base_url": preset["base_url"],
        "api_key": _API_KEY,
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
