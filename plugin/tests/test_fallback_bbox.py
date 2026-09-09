"""Auto-focus fallback bbox scan over a case-open payload.

No QGIS required except where a case names the bridge; the WS stub needs
``websockets``.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from plugin.net import trid3nt_client as tc  # noqa: E402

# --------------------------------------------------------------------------- #
# ITEM D (live-feedback 2026-07-10): auto-focus fallback bbox scan --
# ``find_fallback_bbox`` covers a case-open payload OUTSIDE the primary
# session_state.case.bbox carrier ``parse_case_open`` already extracts.
# --------------------------------------------------------------------------- #


class TestFindFallbackBbox(unittest.TestCase):
    def test_top_level_payload_bbox(self):
        bbox = tc.find_fallback_bbox({"bbox": [-83, 35, -82, 36]})
        self.assertEqual(bbox, (-83.0, 35.0, -82.0, 36.0))

    def test_session_state_level_bbox(self):
        bbox = tc.find_fallback_bbox(
            {"session_state": {"bbox": [-83, 35, -82, 36]}}
        )
        self.assertEqual(bbox, (-83.0, 35.0, -82.0, 36.0))

    def test_session_state_case_level_bbox(self):
        bbox = tc.find_fallback_bbox(
            {"session_state": {"case": {"bbox": [-83, 35, -82, 36]}}}
        )
        self.assertEqual(bbox, (-83.0, 35.0, -82.0, 36.0))

    def test_precedence_top_level_wins(self):
        # top-level payload.bbox is checked first, even when a DIFFERENT
        # bbox also sits deeper in the payload.
        bbox = tc.find_fallback_bbox(
            {
                "bbox": [-83, 35, -82, 36],
                "session_state": {"case": {"bbox": [-70, 40, -69, 41]}},
            }
        )
        self.assertEqual(bbox, (-83.0, 35.0, -82.0, 36.0))

    def test_absent_or_malformed_yields_none(self):
        for payload in (
            {},
            {"bbox": None},
            {"bbox": [-83, 35, -82]},  # only 3 elements
            {"bbox": "not-a-list"},
            {"session_state": None},
            {"session_state": {"case": "not-a-dict"}},
            "not-a-dict",
            None,
        ):
            self.assertIsNone(tc.find_fallback_bbox(payload), f"payload={payload!r}")

    def test_int_elements_coerced_to_float(self):
        bbox = tc.find_fallback_bbox({"bbox": [-83, 35, -82, 36]})
        self.assertTrue(all(isinstance(v, float) for v in bbox))
