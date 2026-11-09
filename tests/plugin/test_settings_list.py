"""The Settings list reads the daemon's settings over ``GET /api/settings``; the plugin never opens the user's file."""

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

BODY = {"settings": [
    {"name": "FILL_MAX_DISTANCE", "family": "bed", "type": "INTEGER", "value": 250, "level": "user",
     "default": 100, "unit": "px", "bounds": [1.0, 1e12], "choices": None, "help": "h", "source": "s"},
    {"nameless": True},
    None,
]}


class _Stub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        found = self.path == "/api/settings"
        raw = json.dumps(BODY if found else {"error": "not found"}).encode("utf-8")
        self.send_response(200 if found else 404)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class TestFetchSettings(unittest.TestCase):
    def _start(self) -> str:
        httpd = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def test_a_row_without_a_name_is_dropped_and_the_value_and_level_ride_through(self):
        rows = tc.fetch_settings(self._start(), timeout=10)
        self.assertEqual([r["name"] for r in rows], ["FILL_MAX_DISTANCE"])
        self.assertEqual((rows[0]["value"], rows[0]["level"]), (250, "user"))

    def test_an_unreachable_agent_raises_the_honest_error(self):
        with self.assertRaises(tc.SettingsRequestError):
            tc.fetch_settings("http://127.0.0.1:1", timeout=2)


class TestTypedSetting(unittest.TestCase):
    def test_a_list_setting_takes_its_values_comma_separated(self):
        row = {"type": "REAL", "value": [2.0, 98.0]}
        self.assertEqual(tc.typed_setting(row, "[5, 95]"), [5.0, 95.0])
        self.assertEqual(tc.typed_setting(row, "1.5,99"), [1.5, 99.0])
        self.assertIsNone(tc.typed_setting(row, "five"))
        self.assertEqual(tc.typed_setting({"type": "REAL", "value": 1.0}, "7"), 7.0)
        self.assertEqual(tc.typed_setting({"type": "STRING", "value": "viridis"}, " magma "), "magma")

    def test_a_logical_setting_takes_on_or_off_and_nothing_else(self):
        row = {"type": "LOGICAL", "value": False}
        self.assertIs(tc.typed_setting(row, "on"), True)
        self.assertIs(tc.typed_setting(row, " False "), False)
        self.assertIsNone(tc.typed_setting(row, "maybe"))
