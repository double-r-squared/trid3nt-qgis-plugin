"""Client-helper + dock-wiring tests for the OpenRouter model-extensibility
seam (design 2026-07-19).

Feature 3 -- ``post_provider_config`` POSTs the live provider config to the
agent's ``/api/provider-config`` route (base_url/api_key/model/num_ctx) so a
provider switch applies with no restart. Feature 2 -- ``fetch_model_list`` GETs
the agent's ``/api/local-models`` route (the free + tool-capable list on
OpenRouter).

The pure-python tests here use an ``http.server`` stub that mirrors the real
agent routes in miniature (same posture as ``test_milestone3``'s
``_CaseListStub``) -- no Qt, no live agent, no live network. The Qt dock-wiring
harness (Save -> off-thread POST payload shape; OpenRouter provider -> live
model-list repopulate) runs in a subprocess under the ``qgis.PyQt`` interpreter
and skips honestly when absent.

SECURITY: a dedicated test asserts the api key never appears in a raised error
message.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from trid3nt.net import trid3nt_client as tc  # noqa: E402


# --------------------------------------------------------------------------- #
# Recording stub: POST /api/provider-config + GET /api/local-models, in
# miniature (mirrors the server's tool_catalog_http.py).
# --------------------------------------------------------------------------- #


class _ProviderStub(http.server.BaseHTTPRequestHandler):
    # POST config
    post_status: int = 200
    post_body: dict = {"ok": True, "model": "m", "base_url_host": "openrouter.ai"}
    last_post_payload: dict | None = None
    # GET local-models
    get_status: int = 200
    get_body: dict = {"models": [], "default": None}

    def _json(self, status: int, payload) -> None:
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
            _ProviderStub.last_post_payload = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            _ProviderStub.last_post_payload = None
        if self.post_status == 200:
            self._json(200, self.post_body)
        else:
            self._json(self.post_status, {"error": "provider config update failed"})

    def do_GET(self):  # noqa: N802
        if self.path != "/api/local-models":
            self._json(404, {"error": "not found"})
            return
        self._json(self.get_status, self.get_body)

    def log_message(self, *args):  # silence
        pass


class _ProviderStubBase(unittest.TestCase):
    def _start(self) -> str:
        httpd = http.server.HTTPServer(("127.0.0.1", 0), _ProviderStub)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        _ProviderStub.last_post_payload = None
        return f"http://127.0.0.1:{httpd.server_address[1]}"


# --------------------------------------------------------------------------- #
# post_provider_config
# --------------------------------------------------------------------------- #


class TestPostProviderConfig(_ProviderStubBase):
    def test_happy_path_sends_full_payload_and_returns_result(self):
        _ProviderStub.post_status = 200
        _ProviderStub.post_body = {
            "ok": True,
            "model": "meta-llama/llama-3.3-70b-instruct:free",
            "base_url_host": "openrouter.ai",
        }
        base = self._start()
        payload = {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-SECRET",
            "model": "meta-llama/llama-3.3-70b-instruct:free",
            "num_ctx": "32768",
        }
        result = tc.post_provider_config(base, payload, timeout=10)
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["base_url_host"], "openrouter.ai")
        # The agent received the FULL config, field-for-field.
        self.assertEqual(_ProviderStub.last_post_payload, payload)

    def test_http_error_raises_honest_error_without_key(self):
        _ProviderStub.post_status = 500
        base = self._start()
        with self.assertRaises(tc.ProviderConfigRequestError) as ctx:
            tc.post_provider_config(
                base, {"api_key": "sk-or-SECRET-KEY"}, timeout=10
            )
        # SECURITY: the raised message NEVER contains the api key.
        self.assertNotIn("sk-or-SECRET-KEY", str(ctx.exception))

    def test_unreachable_agent_raises_honest_error(self):
        with self.assertRaises(tc.ProviderConfigRequestError) as ctx:
            tc.post_provider_config(
                "http://127.0.0.1:1", {"model": "m"}, timeout=2
            )
        self.assertIn("unreachable", str(ctx.exception))


# --------------------------------------------------------------------------- #
# fetch_model_list
# --------------------------------------------------------------------------- #


class TestFetchModelList(_ProviderStubBase):
    def test_happy_path_returns_ids_and_default(self):
        _ProviderStub.get_status = 200
        _ProviderStub.get_body = {
            "models": [
                {"id": "meta-llama/llama-3.3-70b-instruct:free", "label": "a"},
                {"id": "qwen/qwen-2.5-72b-instruct:free", "label": "b"},
                {"label": "no-id -- skipped"},
                "not-a-dict",
            ],
            "default": "qwen/qwen-2.5-72b-instruct:free",
        }
        base = self._start()
        ids, default = tc.fetch_model_list(base, timeout=10)
        self.assertEqual(
            ids,
            [
                "meta-llama/llama-3.3-70b-instruct:free",
                "qwen/qwen-2.5-72b-instruct:free",
            ],
        )
        self.assertEqual(default, "qwen/qwen-2.5-72b-instruct:free")

    def test_null_default_when_absent(self):
        _ProviderStub.get_status = 200
        _ProviderStub.get_body = {"models": [{"id": "x", "label": "x"}]}
        base = self._start()
        ids, default = tc.fetch_model_list(base, timeout=10)
        self.assertEqual(ids, ["x"])
        self.assertIsNone(default)

    def test_route_absent_404_raises(self):
        _ProviderStub.get_status = 404
        base = self._start()
        with self.assertRaises(tc.ModelListRequestError):
            tc.fetch_model_list(base, timeout=10)

    def test_unreachable_raises(self):
        with self.assertRaises(tc.ModelListRequestError):
            tc.fetch_model_list("http://127.0.0.1:1", timeout=2)


# --------------------------------------------------------------------------- #
# Qt dock wiring (subprocess under the qgis.PyQt interpreter) -- Save POST
# payload shape + OpenRouter provider live model-list repopulate.
# --------------------------------------------------------------------------- #


def _qt_python() -> str | None:
    candidates = []
    which = shutil.which("python3")
    if which:
        candidates.append(which)
    candidates.append("/usr/bin/python3")
    for py in dict.fromkeys(candidates):
        if not os.path.exists(py):
            continue
        try:
            probe = subprocess.run(
                [py, "-c", "from qgis.PyQt.QtCore import QCoreApplication"],
                capture_output=True,
                timeout=60,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return py
    return None


class TestDockProviderConfigWiring(unittest.TestCase):
    def test_save_posts_preset_payload_and_repopulates_models(self):
        py = _qt_python()
        if py is None:
            self.skipTest("no interpreter with qgis.PyQt")
        harness = os.path.join(
            os.path.dirname(__file__), "qt_provider_config_harness.py"
        )
        proc = subprocess.run(
            [py, harness],
            capture_output=True,
            timeout=120,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, msg=f"harness failed:\n{out}\n{err}")
        self.assertIn("SAVE_PAYLOAD_OK", out, msg=out)
        self.assertIn("MODEL_REPOPULATE_OK", out, msg=out)
        # SECURITY: the api key must never be printed by the harness.
        self.assertNotIn("sk-or-HARNESS-SECRET", out)


if __name__ == "__main__":
    unittest.main()
