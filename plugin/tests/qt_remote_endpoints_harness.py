"""Offscreen harness for remote-daemon endpoint derivation.

Run as a SUBPROCESS by its wrapper, which needs a real dock widget and skips
honestly when absent. ``_on_connected`` is invoked directly, exactly as the
bridge delivers a handshake, so the real dock code runs. Pointing at a remote
store is an endpoint VALUE: the same ``s3://`` uri resolves against any host."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from qgis.PyQt.QtCore import QCoreApplication, QSettings  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

# Isolate QSettings so the harness never touches a real QGIS profile.
QSettings.setDefaultFormat(QSettings.IniFormat)
QCoreApplication.setOrganizationName("trid3nt-remote-endpoints-harness")
QCoreApplication.setApplicationName("trid3nt-remote-endpoints-harness")

app = QApplication(sys.argv)

from plugin.plugin_settings import PluginSettings  # noqa: E402
from plugin.ui.dock import Trid3ntDock  # noqa: E402


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
from osgeo import gdal  # noqa: E402

if gdal.GetConfigOption("AWS_S3_ENDPOINT") != "100.64.0.5:9000":
    _fail(
        "GDAL /vsis3 endpoint not pointed at the advertised store: "
        f"{gdal.GetConfigOption('AWS_S3_ENDPOINT')!r}"
    )
if gdal.GetConfigOption("AWS_VIRTUAL_HOSTING") != "FALSE":
    _fail("path-style addressing not set for the store")
if gdal.GetConfigOption("GDAL_PAM_ENABLED") != "NO":
    _fail("PAM still enabled -- a read would write .aux.xml into the store")

# ---- 2. no advertisement + tailnet-shaped local_url -> WS-host derivation -- #
dock.settings.local_url = "ws://100.64.0.7:8765/ws"
dock._on_connected("USER1", True, "", "")
if dock._effective_http_base() != "http://100.64.0.7:8766":
    _fail(f"WS-host fallback wrong: {dock._effective_http_base()!r}")
if dock._effective_data_base() != dock.settings.minio_endpoint:
    _fail(
        "data_base fallback must be settings.minio_endpoint, got "
        f"{dock._effective_data_base()!r}"
    )
if gdal.GetConfigOption("AWS_S3_ENDPOINT") != "127.0.0.1:9000":
    _fail(
        "the fallback endpoint did not reach GDAL: "
        f"{gdal.GetConfigOption('AWS_S3_ENDPOINT')!r}"
    )

# ---- 3. no advertisement + DEFAULT local_url -> byte-identical old default - #
dock.settings.local_url = "ws://127.0.0.1:8765/ws"  # DEFAULT_LOCAL_URL
dock._on_connected("USER1", True, "", "")
if dock._effective_http_base() != "http://127.0.0.1:8766":
    _fail(
        "localhost default regressed: "
        f"{dock._effective_http_base()!r} != http://127.0.0.1:8766"
    )

# ---- 4. token passthrough: the optional shared tailnet token rides through - #
fresh_settings = PluginSettings()
fresh_settings.token = "tailnet-shared-secret"
if fresh_settings.mode != "local":
    _fail(f"expected mode local (migration seam), got {fresh_settings.mode!r}")
if fresh_settings.effective_token() != "tailnet-shared-secret":
    _fail(
        "the optional shared token must ride effective_token(), got "
        f"{fresh_settings.effective_token()!r}"
    )
# Unset (default OFF) still rides through cleanly as "".
fresh_settings.token = ""
if fresh_settings.effective_token() != "":
    _fail("empty token must stay empty (OFF by default)")

print("REMOTE-ENDPOINTS-OK")
