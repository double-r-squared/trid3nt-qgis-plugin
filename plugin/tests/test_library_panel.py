"""The Library panel's two halves: the listing it reads, and the panel it paints.

The listing and the search hits come off the daemon's library routes; parsing is
defensive (a malformed entry is skipped, never raised on) so one bad row never
costs the panel the rest. The Qt half runs in a harness subprocess."""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
import urllib.parse

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from plugin.net import trid3nt_client as tc  # noqa: E402
from library_bodies import LIBRARY_BODY, SEARCH_BODY  # noqa: E402


class _LibraryStub(http.server.BaseHTTPRequestHandler):
    """Mirrors the agent's two library routes in miniature."""

    status: int = 200
    body: dict = LIBRARY_BODY
    #: Every query string the search route was handed, in order.
    queries: list = []

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/library":
            payload, status = self.body, self.status
        elif parsed.path == "/api/library/search":
            _LibraryStub.queries.append(
                urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
            )
            payload, status = SEARCH_BODY, 200
        else:
            payload, status = {"error": "not found"}, 404
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class TestLibraryFetch(unittest.TestCase):
    def _start(self, status: int = 200, body: dict = None) -> str:
        _LibraryStub.status = status
        _LibraryStub.body = body if body is not None else LIBRARY_BODY
        _LibraryStub.queries = []
        httpd = http.server.HTTPServer(("127.0.0.1", 0), _LibraryStub)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def test_tools_keep_their_subsystem_and_wire_order(self):
        library = tc.fetch_library(self._start(), timeout=10)
        self.assertEqual(
            [name for name, _ in library.subsystems], ["fetchers", "derives"]
        )
        names = [item.name for item in library.subsystems[0][1]]
        self.assertEqual(names, ["fetch_dem", "fetch_landcover"])
        first = library.subsystems[0][1][0]
        self.assertEqual(first.kind, "tool")
        self.assertEqual(first.group, "fetchers")
        self.assertIn("elevation", first.description)
        self.assertIn(("source_class", "elevation"), first.facts)

    def test_rows_sit_under_class_then_kind_with_their_fetcher_first(self):
        library = tc.fetch_library(self._start(), timeout=10)
        self.assertEqual([name for name, _ in library.classes], ["elevation"])
        kinds = library.classes[0][1]
        self.assertEqual([name for name, _ in kinds], ["raster"])
        row = kinds[0][1][0]
        self.assertEqual(row.name, "3dep")
        self.assertEqual(row.kind, "row")
        self.assertEqual(row.group, "elevation / raster")
        self.assertEqual(row.facts[0], ("fetcher", "fetch_dem"))
        self.assertIn(("resolution_m", "10"), row.facts)

    def test_a_malformed_entry_is_skipped_not_raised_on(self):
        body = {
            "subsystems": [
                "not-a-dict",
                {"tools": [{"name": "orphan"}]},
                {"name": "fetchers", "tools": [
                    "junk", {"docstring": "no name"}, {"name": ""},
                    {"name": "fetch_dem"},
                ]},
            ],
            "classes": [{"name": "elevation", "kinds": [
                "junk", {"rows": []}, {"name": "raster", "rows": ["junk"]},
            ]}],
        }
        library = tc.fetch_library(self._start(body=body), timeout=10)
        self.assertEqual([n for n, _ in library.subsystems], ["fetchers"])
        self.assertEqual(
            [i.name for i in library.subsystems[0][1]], ["fetch_dem"]
        )
        self.assertEqual(library.classes[0][1], [("raster", [])])

    def test_search_sends_the_query_and_keeps_the_ranked_order(self):
        base = self._start()
        hits = tc.search_library(base, "where is the water", timeout=10)
        self.assertEqual(_LibraryStub.queries, ["where is the water"])
        self.assertEqual([h.name for h in hits], ["fetch_dem", "3dep"])
        self.assertEqual(hits[0].facts[-1], ("score", "8.400"))
        self.assertEqual(hits[1].kind, "row")

    def test_an_http_fault_raises_an_honest_error(self):
        with self.assertRaises(tc.LibraryRequestError):
            tc.fetch_library(self._start(status=500), timeout=10)
        with self.assertRaises(tc.LibraryRequestError):
            tc.fetch_library("http://127.0.0.1:1", timeout=2)


def _qt_python() -> "str | None":
    """First interpreter that can import qgis.PyQt (same probe as
    test_tool_picker)."""
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


@pytest.mark.qt_harness_shim
class TestLibraryPanelQt(unittest.TestCase):
    """One harness subprocess run covering the panel: the tree, the entry list,
    the detail pane, one-click copy, search and the honest failure line."""

    _proc: "subprocess.CompletedProcess | None" = None

    @classmethod
    def setUpClass(cls):
        py = _qt_python()
        if py is None:
            raise unittest.SkipTest("no interpreter with qgis.PyQt available")
        harness = os.path.join(
            os.path.dirname(__file__), "qt_library_harness.py"
        )
        cls._proc = subprocess.run(
            [py, "-u", harness],
            capture_output=True,
            text=True,
            timeout=180,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )

    def test_harness_green(self):
        proc = self._proc
        self.assertIsNotNone(proc)
        self.assertEqual(
            proc.returncode,
            0,
            "library harness failed (rc="
            f"{proc.returncode})\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
        )
        self.assertIn("LIBRARY-OK", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
