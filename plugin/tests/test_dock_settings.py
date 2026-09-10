"""The settings the dock reads and stores."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))


class TestShowThinkingSettings(unittest.TestCase):
    """F9: show_thinking preference in plugin_settings."""

    def _make_settings(self, stored: dict = None):
        import types
        import importlib

        class FakeQSettings:
            store: dict = {}

            def value(self, key, default=None):
                return self.store.get(key, default)

            def setValue(self, key, value):
                self.store[key] = value

        FakeQSettings.store = stored or {}
        qtcore = types.ModuleType("qgis.PyQt.QtCore")
        qtcore.QSettings = FakeQSettings
        pyqt = types.ModuleType("qgis.PyQt")
        pyqt.QtCore = qtcore
        qgis_mod = types.ModuleType("qgis")
        qgis_mod.PyQt = pyqt
        saved = {k: sys.modules.get(k) for k in ("qgis", "qgis.PyQt", "qgis.PyQt.QtCore")}
        sys.modules.update({"qgis": qgis_mod, "qgis.PyQt": pyqt, "qgis.PyQt.QtCore": qtcore})
        try:
            sys.modules.pop("plugin.plugin_settings", None)
            return importlib.import_module("plugin.plugin_settings").PluginSettings()
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_default_is_true(self):
        s = self._make_settings()
        self.assertTrue(s.show_thinking, "show_thinking must default to True")

    def test_explicit_false_stored(self):
        s = self._make_settings({"trid3nt/show_thinking": "false"})
        self.assertFalse(s.show_thinking)

    def test_explicit_true_stored(self):
        s = self._make_settings({"trid3nt/show_thinking": "true"})
        self.assertTrue(s.show_thinking)

    def test_setter_persists(self):
        import types
        import importlib

        class FakeQSettings:
            store: dict = {}

            def value(self, key, default=None):
                return self.store.get(key, default)

            def setValue(self, key, value):
                self.store[key] = value

        FakeQSettings.store = {}
        qtcore = types.ModuleType("qgis.PyQt.QtCore")
        qtcore.QSettings = FakeQSettings
        pyqt = types.ModuleType("qgis.PyQt")
        pyqt.QtCore = qtcore
        qgis_mod = types.ModuleType("qgis")
        qgis_mod.PyQt = pyqt
        saved = {k: sys.modules.get(k) for k in ("qgis", "qgis.PyQt", "qgis.PyQt.QtCore")}
        sys.modules.update({"qgis": qgis_mod, "qgis.PyQt": pyqt, "qgis.PyQt.QtCore": qtcore})
        try:
            sys.modules.pop("plugin.plugin_settings", None)
            ps = importlib.import_module("plugin.plugin_settings")
            s = ps.PluginSettings()
            s.show_thinking = False
            self.assertEqual(FakeQSettings.store.get("trid3nt/show_thinking"), "false")
            s.show_thinking = True
            self.assertEqual(FakeQSettings.store.get("trid3nt/show_thinking"), "true")
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v


class TestAutoBasemapSettings(unittest.TestCase):
    """Item 4: auto_basemap preference in
    plugin_settings -- same shape as TestShowThinkingSettings above."""

    def _make_settings(self, stored: dict = None):
        import types
        import importlib

        class FakeQSettings:
            store: dict = {}

            def value(self, key, default=None):
                return self.store.get(key, default)

            def setValue(self, key, value):
                self.store[key] = value

        FakeQSettings.store = stored or {}
        qtcore = types.ModuleType("qgis.PyQt.QtCore")
        qtcore.QSettings = FakeQSettings
        pyqt = types.ModuleType("qgis.PyQt")
        pyqt.QtCore = qtcore
        qgis_mod = types.ModuleType("qgis")
        qgis_mod.PyQt = pyqt
        saved = {k: sys.modules.get(k) for k in ("qgis", "qgis.PyQt", "qgis.PyQt.QtCore")}
        sys.modules.update({"qgis": qgis_mod, "qgis.PyQt": pyqt, "qgis.PyQt.QtCore": qtcore})
        try:
            sys.modules.pop("plugin.plugin_settings", None)
            return importlib.import_module("plugin.plugin_settings").PluginSettings()
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_default_is_true(self):
        s = self._make_settings()
        self.assertTrue(s.auto_basemap, "auto_basemap must default to True")

    def test_explicit_false_stored(self):
        s = self._make_settings({"trid3nt/auto_basemap": "false"})
        self.assertFalse(s.auto_basemap)

    def test_explicit_true_stored(self):
        s = self._make_settings({"trid3nt/auto_basemap": "true"})
        self.assertTrue(s.auto_basemap)

    def test_setter_persists(self):
        import types
        import importlib

        class FakeQSettings:
            store: dict = {}

            def value(self, key, default=None):
                return self.store.get(key, default)

            def setValue(self, key, value):
                self.store[key] = value

        FakeQSettings.store = {}
        qtcore = types.ModuleType("qgis.PyQt.QtCore")
        qtcore.QSettings = FakeQSettings
        pyqt = types.ModuleType("qgis.PyQt")
        pyqt.QtCore = qtcore
        qgis_mod = types.ModuleType("qgis")
        qgis_mod.PyQt = pyqt
        saved = {k: sys.modules.get(k) for k in ("qgis", "qgis.PyQt", "qgis.PyQt.QtCore")}
        sys.modules.update({"qgis": qgis_mod, "qgis.PyQt": pyqt, "qgis.PyQt.QtCore": qtcore})
        try:
            sys.modules.pop("plugin.plugin_settings", None)
            ps = importlib.import_module("plugin.plugin_settings")
            s = ps.PluginSettings()
            s.auto_basemap = False
            self.assertEqual(FakeQSettings.store.get("trid3nt/auto_basemap"), "false")
            s.auto_basemap = True
            self.assertEqual(FakeQSettings.store.get("trid3nt/auto_basemap"), "true")
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v


class TestProviderModelSettings(unittest.TestCase):
    """OpenRouter model-extensibility: provider / model_id
    / openrouter_api_key round-trip through QSettings -- same FakeQSettings
    idiom as TestShowThinkingSettings / TestAutoBasemapSettings above."""

    def _make_settings(self, stored: dict = None):
        import types
        import importlib

        class FakeQSettings:
            store: dict = {}

            def value(self, key, default=None):
                return self.store.get(key, default)

            def setValue(self, key, value):
                self.store[key] = value

        FakeQSettings.store = stored or {}
        qtcore = types.ModuleType("qgis.PyQt.QtCore")
        qtcore.QSettings = FakeQSettings
        pyqt = types.ModuleType("qgis.PyQt")
        pyqt.QtCore = qtcore
        qgis_mod = types.ModuleType("qgis")
        qgis_mod.PyQt = pyqt
        saved = {k: sys.modules.get(k) for k in ("qgis", "qgis.PyQt", "qgis.PyQt.QtCore")}
        sys.modules.update({"qgis": qgis_mod, "qgis.PyQt": pyqt, "qgis.PyQt.QtCore": qtcore})
        try:
            sys.modules.pop("plugin.plugin_settings", None)
            return importlib.import_module("plugin.plugin_settings").PluginSettings(), FakeQSettings
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_provider_default_is_local_ollama(self):
        s, _ = self._make_settings()
        self.assertEqual(s.provider, "local-ollama")

    def test_provider_stored_value(self):
        s, _ = self._make_settings({"trid3nt/provider": "openrouter-paid"})
        self.assertEqual(s.provider, "openrouter-paid")

    def test_provider_setter_persists(self):
        s, store = self._make_settings()
        s.provider = "groq"
        self.assertEqual(store.store.get("trid3nt/provider"), "groq")

    def test_provider_blank_falls_back_to_default(self):
        s, store = self._make_settings()
        s.provider = "   "
        self.assertEqual(store.store.get("trid3nt/provider"), "local-ollama")

    def test_model_id_default_is_empty(self):
        s, _ = self._make_settings()
        self.assertEqual(s.model_id, "")

    def test_model_id_stored_value(self):
        s, _ = self._make_settings({"trid3nt/model_id": "deepseek/deepseek-chat"})
        self.assertEqual(s.model_id, "deepseek/deepseek-chat")

    def test_model_id_setter_persists_stripped(self):
        s, store = self._make_settings()
        s.model_id = "  meta-llama/llama-3.3-70b-instruct  "
        self.assertEqual(
            store.store.get("trid3nt/model_id"),
            "meta-llama/llama-3.3-70b-instruct",
        )

    def test_api_key_default_is_empty(self):
        s, _ = self._make_settings()
        self.assertEqual(s.openrouter_api_key, "")

    def test_api_key_round_trip(self):
        s, store = self._make_settings()
        s.openrouter_api_key = "sk-or-v1-SECRET"
        self.assertEqual(store.store.get("trid3nt/openrouter_api_key"), "sk-or-v1-SECRET")
        s2, _ = self._make_settings({"trid3nt/openrouter_api_key": "sk-or-v1-SECRET"})
        self.assertEqual(s2.openrouter_api_key, "sk-or-v1-SECRET")
