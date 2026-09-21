"""The keys form's two halves: the rows it reads, and the store it writes.

The rows come off the daemon's tool catalog, reduced to one entry per
credential NAME with no key material; the store is QgsAuthManager, which the
broker reaches by name and answers for by config id, never by value.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from plugin.net import trid3nt_client as tc  # noqa: E402
from plugin.net.auth_broker import AuthBroker  # noqa: E402

#: A tool-catalog body in miniature: two rows sharing one credential, one row
#: with its own, and a public row that declares none.
CATALOG_BODY = {
    "tool_count": 4,
    "tools": [
        {"name": "fetch_era5_reanalysis", "credential": {
            "name": "ecmwf_cds", "label": "Copernicus Climate Data Store",
            "signup_url": "https://cds.climate.copernicus.eu/how-to-api",
            "env_var": "TRID3NT_COPERNICUS_CDS_API_KEY"}},
        {"name": "fetch_gtsm_tide_surge", "credential": {
            "name": "ecmwf_cds", "label": "Copernicus Climate Data Store",
            "signup_url": "https://cds.climate.copernicus.eu/how-to-api",
            "env_var": "TRID3NT_COPERNICUS_CDS_API_KEY"}},
        {"name": "fetch_airnow_air_quality", "credential": {
            "name": "airnow", "label": "EPA AirNow",
            "signup_url": None, "env_var": "TRID3NT_AIRNOW_API_KEY"}},
        {"name": "fetch_usgs_water_gauges", "credential": None},
    ],
}


class _CatalogStub(http.server.BaseHTTPRequestHandler):
    """Mirrors the agent's ``GET /api/tool-catalog`` route in miniature."""

    status: int = 200
    body: dict = CATALOG_BODY

    def do_GET(self):  # noqa: N802
        raw = json.dumps(
            self.body if self.path == "/api/tool-catalog"
            else {"error": "not found"}
        ).encode("utf-8")
        self.send_response(self.status if self.path == "/api/tool-catalog" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class TestFetchKeyedSources(unittest.TestCase):
    def _start(self, status: int = 200, body: dict = None) -> str:
        _CatalogStub.status = status
        _CatalogStub.body = body if body is not None else CATALOG_BODY
        httpd = http.server.HTTPServer(("127.0.0.1", 0), _CatalogStub)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def test_one_row_per_credential_not_per_source(self):
        rows = tc.fetch_keyed_sources(self._start(), timeout=10)
        self.assertEqual([r["name"] for r in rows], ["airnow", "ecmwf_cds"])
        self.assertEqual(rows[1]["label"], "Copernicus Climate Data Store")
        self.assertEqual(rows[0]["env_var"], "TRID3NT_AIRNOW_API_KEY")

    def test_absent_signup_url_stays_none(self):
        rows = tc.fetch_keyed_sources(self._start(), timeout=10)
        self.assertIsNone(rows[0]["signup_url"])

    def test_a_catalog_with_no_keyed_row_is_empty(self):
        body = {"tool_count": 1, "tools": [{"name": "fetch_dem"}]}
        self.assertEqual(tc.fetch_keyed_sources(self._start(body=body)), [])

    def test_unreachable_agent_raises_honest_error(self):
        with self.assertRaises(tc.KeyedSourcesRequestError):
            tc.fetch_keyed_sources("http://127.0.0.1:1", timeout=2)


class _FakeConfig:
    """A QgsAuthMethodConfig stand-in: a name, a method and one config map."""

    def __init__(self, name: str = "", config: dict = None):
        self._name = name
        self._method = ""
        self._config = dict(config or {})

    def name(self):
        return self._name

    def setName(self, name):  # noqa: N802
        self._name = name

    def setMethod(self, method):  # noqa: N802
        self._method = method

    def setConfig(self, key, value):  # noqa: N802
        self._config[key] = value

    def config(self, key):
        return self._config.get(key, "")


class _FakeAuthManager:
    """A QgsAuthManager stand-in over an in-memory config table."""

    def __init__(self):
        self.configs: dict = {}
        self._next = 0

    def availableAuthMethodConfigs(self):  # noqa: N802
        return dict(self.configs)

    def storeAuthenticationConfig(self, cfg):  # noqa: N802
        for cfg_id, existing in self.configs.items():
            if existing.name() == cfg.name():
                self.configs[cfg_id] = cfg
                return True
        self._next += 1
        self.configs[f"cfg{self._next:03d}"] = cfg
        return True

    def loadAuthenticationConfig(self, cfg_id, cfg, full):  # noqa: N802
        stored = self.configs.get(cfg_id)
        if stored is None:
            return False
        cfg.setName(stored.name())
        cfg.setConfig("key", stored.config("key"))
        return True


class TestBrokerOverAFakeAuthManager(unittest.TestCase):
    def setUp(self):
        from plugin.net.auth_broker import QgsAuthManagerStore

        self.am = _FakeAuthManager()
        self.store = QgsAuthManagerStore(self.am)
        self.broker = AuthBroker(self.store)
        # The real QgsAuthMethodConfig is imported lazily inside the store; the
        # fake stands in for it for the duration of this test.
        import types

        qgis_core = types.ModuleType("qgis.core")
        qgis_core.QgsAuthMethodConfig = _FakeConfig
        self._saved = sys.modules.get("qgis.core")
        sys.modules["qgis.core"] = qgis_core
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            sys.modules.pop("qgis.core", None)
        else:
            sys.modules["qgis.core"] = self._saved

    def test_remember_then_name_and_config_id(self):
        self.assertTrue(self.broker.remember("airnow", "AIRNOW-KEY"))
        self.assertEqual(self.broker.stored_names(), frozenset({"airnow"}))
        self.assertIsNotNone(self.broker.config_id("airnow"))

    def test_config_id_is_none_for_an_unstored_credential(self):
        self.assertIsNone(self.broker.config_id("firms"))

    def test_push_all_carries_every_stored_key_once(self):
        self.broker.remember("airnow", "A")
        self.broker.remember("firms", "F")
        pushed: list = []
        self.assertEqual(self.broker.push_all(
            lambda name, value: pushed.append((name, value))), 2)
        self.assertEqual(sorted(pushed), [("airnow", "A"), ("firms", "F")])

    def test_a_replacement_key_overwrites_rather_than_duplicates(self):
        self.broker.remember("airnow", "OLD")
        self.broker.remember("airnow", "NEW")
        self.assertEqual(len(self.am.configs), 1)
        self.assertEqual(self.store.providers(), {"airnow": "NEW"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
