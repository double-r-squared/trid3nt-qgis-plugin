"""Offer-to-add card + wave-picker tests (LANE P, 2026-07-22 -- SRS Sec F.1.2
Mode 2, ADR 0018 auto/ask modes).

The server's ``mode2_classifier`` flags a fetched ``.gov``/``.edu``/``.mil``/
``.int`` page carrying structured-data signals and emits a LIGHT, fire-and-
forget ``mode2-candidate`` envelope (a raw dict, NOT a ``trid3nt_contracts``
model -- ``server/src/trid3nt_server/mode2_classifier.py``); the plugin
surfaces it as the offer-to-add card (``ui/cards.Mode2CandidateCard``). The
reply -- either decision -- rides the contract's ``catalog-addition-response``
shape (``ws.CatalogAdditionResponsePayload``), with the light candidate's own
``candidate_id`` ULID standing in for the reply's ``request_id`` (see
``ui/gate.py``'s module docstring for the light -> heavy bridge; the heavier
``offer-catalog-addition`` review envelope answers on the identical shape
with its own real ``request_id``).

Coverage here (offline -- pure parse logic + stub_server round trips; the Qt
card itself, and the wave-picker "Step N" fold sequence, are covered at the
qt harness level, ``qt_mode2_offer_harness.py``, run as a subprocess by
``TestMode2OfferQt`` below):

* ``gate.parse_mode2_candidate`` / ``gate.parse_offer_catalog_addition``
  field mapping + malformed-envelope honesty, ``gate.mode2_reason_lines``,
  ``gate.resolve_mode2_decision``'s light->heavy request_id bridge, and
  ``gate.mode2_decision_chip``'s exact chip strings.
* the wire round trip against the stub's fire-and-forget mode2-candidate
  side effect: BOTH decisions land as the exact
  ``CatalogAdditionResponsePayload`` shape on ``self.server.
  catalog_addition_responses`` -- the turn completes regardless of whether/
  how the client answers (mirrors the live server's side-effect emission,
  never a pause).
* the wave sequence on the wire: two DISTINCT ``tool-candidates`` requests
  land within one turn (``STUB_WAVE_STEP1_REQUEST_ID`` /
  ``STUB_WAVE_STEP2_REQUEST_ID``, different ``stage_label``s), unanswered --
  the client-side fold + "Step N" behavior is Qt-harness coverage.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from trid3nt.net import trid3nt_client as tc  # noqa: E402
from trid3nt.ui import gate  # noqa: E402
from stub_server import (  # noqa: E402
    MODE2_CANDIDATE_ROW,
    STUB_MODE2_CANDIDATE_ID,
    STUB_WAVE_STEP1_REQUEST_ID,
    STUB_WAVE_STEP2_REQUEST_ID,
    StubAgentServer,
)


class TestMode2CandidateParsing(unittest.TestCase):
    def test_parse_fields(self):
        req = gate.parse_mode2_candidate(MODE2_CANDIDATE_ROW)
        self.assertEqual(req.kind, "light")
        self.assertEqual(req.request_id, STUB_MODE2_CANDIDATE_ID)
        self.assertEqual(req.url, "https://waterdata.usgs.gov/nwis/rt")
        self.assertEqual(req.domain, "waterdata.usgs.gov")
        self.assertEqual(req.display_host, "waterdata.usgs.gov")
        self.assertEqual(req.title, "USGS National Water Information System")
        # "Why it was flagged" -- the structured-data pattern signals +
        # confidence, human-labeled, never invented.
        self.assertIn(
            "REST API endpoint pattern detected", gate.mode2_reason_lines(req)
        )
        self.assertIn(
            "offers a downloadable data file (CSV/GeoJSON/...)",
            gate.mode2_reason_lines(req),
        )
        self.assertIn("classifier confidence 0.70", gate.mode2_reason_lines(req))

    def test_parse_bare_candidate_dict(self):
        """The envelope-vs-bare-candidate defensive fallback."""
        req = gate.parse_mode2_candidate(MODE2_CANDIDATE_ROW["candidate"])
        self.assertEqual(req.request_id, STUB_MODE2_CANDIDATE_ID)
        self.assertEqual(req.url, "https://waterdata.usgs.gov/nwis/rt")

    def test_parse_malformed_is_none(self):
        # No candidate_id -> nothing to correlate a decision against.
        self.assertIsNone(gate.parse_mode2_candidate({"candidate": {}}))
        self.assertIsNone(
            gate.parse_mode2_candidate({"candidate": {"candidate_id": "x"}})
        )  # no url
        self.assertIsNone(gate.parse_mode2_candidate("not-a-dict"))
        self.assertIsNone(gate.parse_mode2_candidate(None))

    def test_parse_defensive_patterns(self):
        """Junk/absent pattern rows degrade honestly -- no crash, no lines
        invented."""
        req = gate.parse_mode2_candidate(
            {
                "candidate": {
                    "candidate_id": "01X",
                    "url": "https://example.gov/data",
                    "detected_patterns": [1, None, "json-ld", "unknown-pattern"],
                    "confidence": "high",
                }
            }
        )
        self.assertEqual(
            gate.mode2_reason_lines(req),
            ["JSON-LD structured data found on the page", "unknown-pattern"],
        )

    def test_display_host_falls_back_to_url(self):
        req = gate.parse_mode2_candidate(
            {"candidate": {"candidate_id": "01X", "url": "https://example.edu/x"}}
        )
        self.assertEqual(req.domain, "")
        self.assertEqual(req.display_host, "example.edu")


class TestOfferCatalogAdditionParsing(unittest.TestCase):
    """The HEAVIER contract envelope (ws.OfferCatalogAdditionPayload) -- not
    yet emitted by the live server, but parsed defensively so the plugin is
    ready the day it is."""

    def test_parse_fields(self):
        req = gate.parse_offer_catalog_addition(
            {
                "request_id": "01HEAVYREQAAAAAAAAAAAAAAAA",
                "url": "https://data.example.gov/api",
                "discovered_via": "web-research",
                "probe_findings": {
                    "tls_cert_org": "U.S. Department of Example",
                    "access_tier_inferred": 2,
                    "stac_root_found": True,
                    "ogc_capabilities_found": False,
                    "license_observed": "CC0",
                },
                "suggested_catalog_entry": {"name": "Example flood gauges"},
                "ttl_seconds": 600,
            }
        )
        self.assertEqual(req.kind, "heavy")
        self.assertEqual(req.request_id, "01HEAVYREQAAAAAAAAAAAAAAAA")
        self.assertEqual(req.domain, "data.example.gov")
        self.assertEqual(req.title, "Example flood gauges")
        self.assertIn(
            "TLS cert org: U.S. Department of Example", req.reasons
        )
        self.assertIn("inferred access tier 2", req.reasons)
        self.assertIn("STAC root found", req.reasons)
        self.assertNotIn("OGC GetCapabilities found", req.reasons)
        self.assertIn("license: CC0", req.reasons)

    def test_parse_malformed_is_none(self):
        self.assertIsNone(gate.parse_offer_catalog_addition({"url": "x"}))
        self.assertIsNone(
            gate.parse_offer_catalog_addition({"request_id": "01X"})
        )  # no url
        self.assertIsNone(gate.parse_offer_catalog_addition(None))


class TestMode2DecisionResolution(unittest.TestCase):
    def test_light_candidate_bridges_candidate_id_to_request_id(self):
        req = gate.parse_mode2_candidate(MODE2_CANDIDATE_ROW)
        wire = gate.resolve_mode2_decision(req, add=True)
        self.assertEqual(
            wire,
            {
                "request_id": STUB_MODE2_CANDIDATE_ID,
                "decision": "accept",
                "edited_catalog_entry": None,
                "reject_reason": None,
                "cancelled": False,
            },
        )
        reject_wire = gate.resolve_mode2_decision(req, add=False)
        self.assertEqual(reject_wire["decision"], "reject")
        self.assertEqual(reject_wire["request_id"], STUB_MODE2_CANDIDATE_ID)

    def test_heavy_offer_uses_its_own_request_id(self):
        req = gate.parse_offer_catalog_addition(
            {"request_id": "01HEAVYREQAAAAAAAAAAAAAAAA", "url": "https://x.gov/y"}
        )
        wire = gate.resolve_mode2_decision(req, add=True)
        self.assertEqual(wire["request_id"], "01HEAVYREQAAAAAAAAAAAAAAAA")

    def test_chip_strings(self):
        req = gate.parse_mode2_candidate(MODE2_CANDIDATE_ROW)
        self.assertEqual(
            gate.mode2_decision_chip(req, add=True),
            "added waterdata.usgs.gov to the catalog",
        )
        self.assertEqual(gate.mode2_decision_chip(req, add=False), "dismissed")


class TestMode2RoundTrip(unittest.TestCase):
    """The wire round trip against the stub's fire-and-forget mode2-candidate
    side effect -- mirrors TestToolChoiceRoundTrip for the offer-to-add
    seam."""

    def setUp(self):
        self.server = StubAgentServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client = tc.AgentClient(self.server.url)
        self.addCleanup(self.client.close)
        self.client.connect()
        self.client.create_case("mode2 offer test")

    def _await_kind(self, kind, deadline_s=10.0):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            ev = self.client.next_event(timeout=1.0)
            if ev is not None and ev.kind == kind:
                return ev
        self.fail(f"no {kind!r} event within {deadline_s}s")

    def _await_catalog_addition_response(self, deadline_s=10.0):
        """catalog-addition-response has no server reply envelope to await
        (fire-and-forget by design) -- poll the stub's recorded list
        instead of a fixed sleep."""
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            if self.server.catalog_addition_responses:
                return
            time.sleep(0.05)
        self.fail("no catalog-addition-response recorded by the stub")

    def test_request_classified_not_raw(self):
        self.client.send_chat("please mode2-candidate check this page")
        ev = self._await_kind("mode2-candidate")
        self.assertEqual(
            ev.data["candidate"]["candidate_id"], STUB_MODE2_CANDIDATE_ID
        )
        self.assertEqual(ev.data["candidate"]["domain"], "waterdata.usgs.gov")
        # Fire-and-forget: the turn completes regardless (no gate).
        self._await_kind("turn-complete")

    def test_add_round_trip(self):
        self.client.send_chat("please mode2-candidate check this page")
        ev = self._await_kind("mode2-candidate")
        self._await_kind("turn-complete")
        req = gate.parse_mode2_candidate(ev.data)
        wire = gate.resolve_mode2_decision(req, add=True)
        self.client.respond_catalog_addition(
            wire["request_id"], wire["decision"],
            edited_catalog_entry=wire["edited_catalog_entry"],
            reject_reason=wire["reject_reason"],
        )
        self._await_catalog_addition_response()
        self.assertEqual(
            self.server.catalog_addition_responses,
            [
                {
                    "request_id": STUB_MODE2_CANDIDATE_ID,
                    "decision": "accept",
                    "edited_catalog_entry": None,
                    "reject_reason": None,
                    "cancelled": False,
                }
            ],
        )

    def test_dismiss_round_trip(self):
        self.client.send_chat("please mode2-candidate check this page")
        ev = self._await_kind("mode2-candidate")
        self._await_kind("turn-complete")
        req = gate.parse_mode2_candidate(ev.data)
        wire = gate.resolve_mode2_decision(req, add=False)
        self.client.respond_catalog_addition(
            wire["request_id"], wire["decision"],
            edited_catalog_entry=wire["edited_catalog_entry"],
            reject_reason=wire["reject_reason"],
        )
        self._await_catalog_addition_response()
        self.assertEqual(
            self.server.catalog_addition_responses,
            [
                {
                    "request_id": STUB_MODE2_CANDIDATE_ID,
                    "decision": "reject",
                    "edited_catalog_entry": None,
                    "reject_reason": None,
                    "cancelled": False,
                }
            ],
        )

    def test_wave_sequence_two_candidates_one_turn(self):
        """Two DISTINCT tool-candidates requests land within ONE turn -- the
        wire half of the wave-picker UX (the client-side fold/step-N is the
        Qt harness's job, TestMode2OfferQt)."""
        self.client.send_chat("wave-picks please")
        ev1 = self._await_kind("tool-candidates")
        ev2 = self._await_kind("tool-candidates")
        self.assertEqual(ev1.data["request_id"], STUB_WAVE_STEP1_REQUEST_ID)
        self.assertEqual(ev1.data["stage_label"], "Data step")
        self.assertEqual(ev2.data["request_id"], STUB_WAVE_STEP2_REQUEST_ID)
        self.assertEqual(ev2.data["stage_label"], "Analysis step")
        self._await_kind("turn-complete")
        self.assertEqual(self.server.tool_choices, [])


def _qt_python() -> "str | None":
    """First interpreter that can import qgis.PyQt (same probe as
    test_tool_picker._qt_python)."""
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


class TestMode2OfferQt(unittest.TestCase):
    """One harness subprocess run covering: the offer-to-add card render/
    decide/lock/fold + malformed-envelope honesty, and the wave-picker
    two-cards-one-turn fold + 'Step N' title/chip."""

    _proc: "subprocess.CompletedProcess | None" = None

    @classmethod
    def setUpClass(cls):
        py = _qt_python()
        if py is None:
            raise unittest.SkipTest("no interpreter with qgis.PyQt available")
        harness = os.path.join(
            os.path.dirname(__file__), "qt_mode2_offer_harness.py"
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
        self.assertIsNotNone(proc, "harness did not run")
        self.assertEqual(
            proc.returncode, 0,
            f"harness failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertIn("MODE2-OFFER-OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
