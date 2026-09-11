"""Canvas AOI: the pure bbox math the dock hands the server.

No QGIS required - Qt widgets are excluded.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from plugin.case import aoi  # noqa: E402
from plugin.net import trid3nt_client as tc  # noqa: E402
from stub_server import StubAgentServer  # noqa: E402



class TestAoi(unittest.TestCase):
    def test_merc_to_lonlat(self):
        lon, lat = aoi.merc_to_lonlat(0.0, 0.0)
        self.assertAlmostEqual(lon, 0.0)
        self.assertAlmostEqual(lat, 0.0)
        lon, lat = aoi.merc_to_lonlat(20037508.342789244, 0.0)
        self.assertAlmostEqual(lon, 180.0, places=6)
        # Asheville-ish sanity point
        lon, lat = aoi.merc_to_lonlat(-9190000.0, 4241000.0)
        self.assertAlmostEqual(lon, -82.556, places=2)
        self.assertAlmostEqual(lat, 35.566, places=2)

    def test_extent_to_bbox4326(self):
        # 4326 passthrough
        self.assertEqual(
            aoi.extent_to_bbox4326(-82.6, 35.5, -82.5, 35.6, "EPSG:4326"),
            (-82.6, 35.5, -82.5, 35.6),
        )
        # 3857 conversion (web-mercator canvas, the QGIS default with OSM tiles)
        bbox = aoi.extent_to_bbox4326(
            -9196000.0, 4238000.0, -9185000.0, 4249000.0, "EPSG:3857"
        )
        self.assertIsNotNone(bbox)
        self.assertAlmostEqual(bbox[0], -82.610, places=2)
        self.assertAlmostEqual(bbox[2], -82.512, places=2)
        self.assertTrue(35.4 < bbox[1] < bbox[3] < 35.8)
        # unknown CRS -> None (caller falls back to QgsCoordinateTransform)
        self.assertIsNone(aoi.extent_to_bbox4326(0, 0, 1000, 1000, "EPSG:26917"))
        # degenerate / non-finite -> None, never a fabricated bbox
        self.assertIsNone(aoi.extent_to_bbox4326(1, 1, 1, 1, "EPSG:4326"))
        self.assertIsNone(
            aoi.extent_to_bbox4326(float("nan"), 0, 1, 1, "EPSG:4326")
        )

    def test_two_deg_guard(self):
        small = (-82.6, 35.5, -82.5, 35.6)
        self.assertTrue(aoi.bbox_within_guard(small))
        # exactly 2.0 deg is still allowed ("exceeds" the guard means >)
        edge = (-83.0, 35.0, -81.0, 37.0)
        self.assertTrue(aoi.bbox_within_guard(edge))
        wide = (-85.0, 35.0, -80.0, 36.0)  # 5 deg lon
        self.assertFalse(aoi.bbox_within_guard(wide))
        tall = (-82.0, 30.0, -81.0, 36.0)  # 6 deg lat
        self.assertFalse(aoi.bbox_within_guard(tall))

    def test_format_bbox_and_status_text(self):
        bbox = (-82.62, 35.55, -82.50, 35.64)
        # The in-text AOI prose injector
        # (attach_aoi_to_text) is GONE -- the AOI rides the structured
        # ``aoi_bbox`` user-message field (see test_client). format_bbox
        # remains: it renders the one-time "Case AOI set to ..." note.
        self.assertFalse(hasattr(aoi, "attach_aoi_to_text"))
        self.assertEqual(
            aoi.format_bbox(bbox),
            "[-82.620000, 35.550000, -82.500000, 35.640000]",
        )
        # status line formats
        self.assertEqual(
            aoi.aoi_status_text(bbox, True), "AOI: canvas 0.12 x 0.09 deg"
        )
        self.assertEqual(aoi.aoi_status_text(bbox, False), "AOI: off")
        self.assertIn("unavailable", aoi.aoi_status_text(None, True))
        wide = (-85.0, 35.0, -80.0, 36.0)
        status = aoi.aoi_status_text(wide, True)
        self.assertIn("too large", status)
        self.assertIn("sent without AOI", status)

    def test_create_case_sends_aoi_first_bbox(self):
        """The #170 AOI-first mirror: args.bbox on case-command create."""
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        client = tc.AgentClient(server.url)
        self.addCleanup(client.close)
        client.connect()
        client.create_case("aoi case", bbox=[-82.6, 35.5, -82.5, 35.6])
        create = [e for e in server.received if e["type"] == "case-command"][0]
        self.assertEqual(create["payload"]["command"], "create")
        self.assertEqual(
            create["payload"]["args"]["bbox"], [-82.6, 35.5, -82.5, 35.6]
        )
        # no bbox -> args carries no bbox key (byte-identical legacy path)
        client2 = tc.AgentClient(server.url)
        self.addCleanup(client2.close)
        client2.connect()
        client2.create_case("no aoi case")
        create2 = [e for e in server.received if e["type"] == "case-command"][-1]
        self.assertNotIn("bbox", create2["payload"]["args"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
