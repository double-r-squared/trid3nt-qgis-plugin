"""Offscreen harness for remote-daemon (tailnet) endpoint derivation
(LANE P). Run as a SUBPROCESS by ``test_remote_endpoints.TestRemoteEndpointsDock``
-- it needs ``qgis.PyQt`` (a real ``Trid3ntDock``/``QDockWidget``), which the
pure test venv lacks; the test probes the system interpreter and skips
honestly when absent (same convention as ``test_dock_ui`` / ``test_case_bbox``).

Offscreen, no agent, no network -- ``_on_connected`` is invoked directly
(exactly how ``AgentBridge.connected`` delivers a real handshake result to the
dock; see ``ws_bridge.AgentWorker.run``), so every check below exercises the
REAL dock code path (``_effective_http_base`` / ``_effective_data_base`` /
``LayerMaterializer.data_base_override``), not a re-implementation of it.

Checks:
  1. stub-shaped advertised endpoints present -> both effective bases equal
     the advertisement verbatim (trailing slash already stripped upstream),
     and the materializer's ``data_base_override`` picks it up.
  2. no advertised endpoints (old daemon) + a tailnet-shaped ``local_url`` ->
     the http base is WS-host-DERIVED (:8766), never localhost; the data
     base falls back to ``settings.minio_endpoint`` (current behavior).
  3. no advertised endpoints + the DEFAULT ``local_url`` -> the derived http
     base is byte-identical to the old hardcoded default
     (``http://127.0.0.1:8766``) -- the "localhost default unchanged" bar.
  4. REMOTE mode is untouched: ``_effective_http_base`` still routes through
     ``case_export.ws_url_to_http_base(remote_url)`` regardless of any
     advertised value stashed from a prior LOCAL connect.
  5. token passthrough: ``settings.effective_token()`` now rides the pasted
     token in LOCAL mode too (previously always "").

Exits 0 and prints REMOTE-ENDPOINTS-OK; raises (nonzero) on any failed check.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qgis.PyQt.QtCore import QCoreApplication, QSettings  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

# Isolate QSettings so the harness never touches a real QGIS profile.
QSettings.setDefaultFormat(QSettings.IniFormat)
QCoreApplication.setOrganizationName("trid3nt-remote-endpoints-harness")
QCoreApplication.setApplicationName("trid3nt-remote-endpoints-harness")

app = QApplication(sys.argv)

from trid3nt.plugin_settings import MODE_REMOTE, PluginSettings  # noqa: E402
from trid3nt.ui.dock import Trid3ntDock  # noqa: E402


class FakeIface:
    def mapCanvas(self):
        raise RuntimeError("no canvas in this harness -- unused by these checks")

    def activeLayer(self):
        return None

    def addCustomActionForLayerType(self, *a, **k):
        raise RuntimeError("no layer tree in the harness")

    def removeCustomActionForLayerType(self, *a, **k):
        raise RuntimeError("no layer tree in the harness")


def _fail(msg: str) -> None:
    raise AssertionError(msg)


dock = Trid3ntDock(FakeIface())
dock._auto_connect_done_this_show = True  # block showEvent auto-connect

# ---- 1. advertised endpoints win outright ---------------------------------- #
dock.settings.local_url = "ws://127.0.0.1:8765/ws"
dock._on_connected(
    "USER1", True, "http://100.64.0.5:8766/", "http://100.64.0.5:9000/"
)
if dock._effective_http_base() != "http://100.64.0.5:8766":
    _fail(f"advertised http_base not honored: {dock._effective_http_base()!r}")
if dock._effective_data_base() != "http://100.64.0.5:9000":
    _fail(f"advertised data_base not honored: {dock._effective_data_base()!r}")
if dock.materializer.data_base_override != "http://100.64.0.5:9000":
    _fail(
        "materializer.data_base_override not synced: "
        f"{dock.materializer.data_base_override!r}"
    )

# ---- 2. no advertisement + tailnet-shaped local_url -> WS-host derivation -- #
dock.settings.local_url = "ws://100.64.0.7:8765/ws"
dock._on_connected("USER1", True, "", "")
if dock._effective_http_base() != "http://100.64.0.7:8766":
    _fail(f"WS-host fallback wrong: {dock._effective_http_base()!r}")
if dock._effective_data_base() != dock.settings.minio_endpoint:
    _fail(
        "data_base fallback must be settings.minio_endpoint (current "
        f"localhost behavior), got {dock._effective_data_base()!r}"
    )

# ---- 3. no advertisement + DEFAULT local_url -> byte-identical old default - #
dock.settings.local_url = "ws://127.0.0.1:8765/ws"  # DEFAULT_LOCAL_URL
dock._on_connected("USER1", True, "", "")
if dock._effective_http_base() != "http://127.0.0.1:8766":
    _fail(
        "localhost default regressed: "
        f"{dock._effective_http_base()!r} != http://127.0.0.1:8766"
    )

# ---- 4. REMOTE mode is untouched by any stashed advertisement -------------- #
dock.settings.mode = MODE_REMOTE
dock.settings.remote_url = "wss://cloud.example.com/ws"
# A prior LOCAL connect's advertisement must never leak into the remote path.
dock._advertised_http_base = "http://100.64.0.5:8766"
if dock._effective_http_base() != "https://cloud.example.com":
    _fail(f"remote mode leaked a local advertisement: {dock._effective_http_base()!r}")
dock.settings.mode = "local"  # restore for the next check

# ---- 5. token passthrough: LOCAL mode no longer blanks the token ----------- #
fresh_settings = PluginSettings()
fresh_settings.token = "tailnet-shared-secret"
if fresh_settings.mode != "local":
    _fail(f"expected default mode local, got {fresh_settings.mode!r}")
if fresh_settings.effective_token() != "tailnet-shared-secret":
    _fail(
        "LOCAL mode must ride the optional shared token now, got "
        f"{fresh_settings.effective_token()!r}"
    )
# Unset (default OFF) still rides through cleanly as "".
fresh_settings.token = ""
if fresh_settings.effective_token() != "":
    _fail("empty token must stay empty (OFF by default)")

print("REMOTE-ENDPOINTS-OK")
