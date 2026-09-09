"""Selection AOI: the pure precedence math over selection and canvas.

No QGIS required except where a case names the bridge; the WS stub needs
``websockets``.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from plugin.case import aoi  # noqa: E402

# --------------------------------------------------------------------------- #
# Selection AOI (pure math)
# --------------------------------------------------------------------------- #


class TestSelectionAoi(unittest.TestCase):
    def test_choose_aoi_precedence(self):
        sel = (-82.60, 35.55, -82.55, 35.60)
        canvas = (-83.0, 35.0, -82.0, 36.0)
        # selection wins when preferred and resolved
        self.assertEqual(aoi.choose_aoi(sel, canvas, True), (sel, "selection"))
        # no selection resolved -> canvas
        self.assertEqual(aoi.choose_aoi(None, canvas, True), (canvas, "canvas"))
        # selection present but toggle off -> canvas
        self.assertEqual(aoi.choose_aoi(sel, canvas, False), (canvas, "canvas"))
        # nothing -> honest (None, None)
        self.assertEqual(aoi.choose_aoi(None, None, True), (None, None))
        # a TOO-LARGE selection is still chosen (the guard rejects it later
        # with an honest "selection ... too large" -- never a silent canvas
        # fallback the user did not ask for)
        wide_sel = (-90.0, 30.0, -80.0, 40.0)
        chosen, source = aoi.choose_aoi(wide_sel, canvas, True)
        self.assertEqual((chosen, source), (wide_sel, "selection"))
        self.assertFalse(aoi.bbox_within_guard(chosen))

    def test_selection_status_text(self):
        sel = (-82.62, 35.55, -82.50, 35.64)
        self.assertEqual(
            aoi.aoi_status_text(sel, True, source="selection"),
            "AOI: selection 0.12 x 0.09 deg",
        )
        wide = (-85.0, 35.0, -80.0, 36.0)
        status = aoi.aoi_status_text(wide, True, source="selection")
        self.assertIn("selection", status)
        self.assertIn("too large", status)
        # (2026-07-22): the per-message in-text context line is GONE
        # -- the AOI rides the structured ``aoi_bbox`` user-message payload
        # field for every source (see test_client structured-AOI tests).
        self.assertFalse(hasattr(aoi, "attach_aoi_to_text"))

    def test_selection_bbox_transform_reuses_extent_math(self):
        # a 3857 selection rect (what boundingBoxOfSelected returns for a
        # web-mercator layer) inverts through the same pure math
        bbox = aoi.extent_to_bbox4326(
            -9196000.0, 4238000.0, -9185000.0, 4249000.0, "EPSG:3857"
        )
        self.assertIsNotNone(bbox)
        self.assertTrue(aoi.bbox_within_guard(bbox))
        # a degenerate rect (single-point selection) is an honest None
        self.assertIsNone(
            aoi.extent_to_bbox4326(-82.5, 35.5, -82.5, 35.5, "EPSG:4326")
        )
