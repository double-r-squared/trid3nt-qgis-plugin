"""The FORM and DRAW cards' pure logic (declarative campaign, wave 2).

Offline, no Qt: the parse/resolve helpers in ``ui/gate.py`` are where the wire
meets the card, and they are what a headless test can hold to the contract. The
widgets themselves are covered at the qt harness level.

Coverage:

* ``parse_param_sheet``: field mapping, the advanced fold, and honest degrade -
  an absent or malformed sheet is None, so the gate card falls back to its
  provenance text instead of rendering a broken grid.
* ``resolve_param_sheet_edits``: only rows that actually MOVED become
  ``revised_args`` (re-sending an untouched value would re-stamp it as
  user-supplied), numbers parse back to numbers, and an unparseable numeric edit
  is dropped rather than sent as a string the server would refuse.
* ``SpatialInputRequest.supported`` / ``draw_kind``: the polygon + polyline
  purposes are drawable now; the TAGGED barrier surface still degrades honestly.
* ``resolve_spatial_input_features``: the drawn shape's wire reply, including the
  closed polygon ring the contract validator and the server parser read.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from plugin.ui import gate  # noqa: E402


_DEFAULT_ROWS = [
    {"name": "outfall", "value": [-124.1, 40.5], "desc": "Where it enters",
     "door": "user", "basis": "user", "source_badge": "you supplied this"},
    {"name": "water_temp_c", "value": 20.0, "units": "C",
     "desc": "Water temperature", "door": "scenario", "basis": "default_demo",
     "source_badge": "labeled default", "bounds": [0.0, 40.0]},
    {"name": "sim_seconds", "value": 3600.0, "units": "s",
     "desc": "Simulated time", "door": "constant", "basis": "default_demo",
     "source_badge": "labeled default", "bounds": [60.0, 86400.0],
     "advanced": True},
]


def _sheet_payload(**over) -> dict:
    rows = over.pop("rows", _DEFAULT_ROWS)
    payload = {
        "warning_id": "01J000000000000000000000AA",
        "tool_name": "telemac_do_sag",
        "options": ["proceed", "narrow_scope", "cancel"],
        "recommendation": "review the inputs",
        "param_sheet": {"workflow": "telemac_do_sag",
                        "title": "Review the DO-sag inputs", "rows": rows},
    }
    payload.update(over)
    return payload


class ParamSheetParseTests(unittest.TestCase):
    def test_rows_and_the_advanced_fold(self) -> None:
        sheet = gate.parse_param_sheet(_sheet_payload())
        self.assertIsNotNone(sheet)
        self.assertEqual(sheet.workflow, "telemac_do_sag")
        self.assertEqual(sheet.title, "Review the DO-sag inputs")
        self.assertEqual([r.name for r in sheet.basic], ["outfall", "water_temp_c"])
        self.assertEqual([r.name for r in sheet.advanced], ["sim_seconds"])

    def test_a_row_carries_its_declaration(self) -> None:
        sheet = gate.parse_param_sheet(_sheet_payload())
        row = sheet.rows[1]
        self.assertEqual(row.label, "water_temp_c (C)")
        self.assertEqual(row.bounds, (0.0, 40.0))
        self.assertEqual(row.source_badge, "labeled default")
        self.assertTrue(row.editable)
        self.assertTrue(row.is_numeric)

    def test_a_list_value_displays_without_none(self) -> None:
        sheet = gate.parse_param_sheet(_sheet_payload())
        self.assertEqual(sheet.rows[0].display(), "-124.1, 40.5")
        self.assertFalse(sheet.rows[0].is_numeric)

    def test_a_missing_or_malformed_sheet_degrades_to_none(self) -> None:
        """The gate card then renders its provenance text - a worse form, never
        a broken one."""
        self.assertIsNone(gate.parse_param_sheet({"warning_id": "x"}))
        self.assertIsNone(gate.parse_param_sheet(_sheet_payload(param_sheet=[])))
        self.assertIsNone(gate.parse_param_sheet(_sheet_payload(rows=[])))
        self.assertIsNone(gate.parse_param_sheet(_sheet_payload(rows=[{"value": 1}])))
        self.assertIsNone(gate.parse_param_sheet(None))

    def test_inverted_bounds_are_dropped_not_rendered(self) -> None:
        sheet = gate.parse_param_sheet(_sheet_payload(rows=[
            {"name": "a", "value": 1.0, "door": "scenario", "basis": "default_demo",
             "bounds": ["nonsense", 2.0]}]))
        self.assertIsNone(sheet.rows[0].bounds)


class ParamSheetEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = gate.parse_param_sheet(_sheet_payload()).rows

    def test_only_moved_rows_become_revisions(self) -> None:
        """Re-sending an untouched value would re-stamp it user-supplied."""
        revised = gate.resolve_param_sheet_edits(self.rows, {
            "water_temp_c": "24", "sim_seconds": "3600",
            "outfall": "-124.1, 40.5",
        })
        self.assertEqual(revised, {"water_temp_c": 24.0})

    def test_a_numeric_edit_travels_as_a_number(self) -> None:
        revised = gate.resolve_param_sheet_edits(self.rows, {"sim_seconds": "7200"})
        self.assertEqual(revised, {"sim_seconds": 7200.0})
        self.assertIsInstance(revised["sim_seconds"], float)

    def test_an_unparseable_numeric_edit_is_dropped(self) -> None:
        """A string where the server declared a number is refused there; dropping
        it here keeps the rest of the sheet submittable."""
        self.assertEqual(
            gate.resolve_param_sheet_edits(self.rows, {"water_temp_c": "warm"}), {})

    def test_a_text_edit_travels_verbatim(self) -> None:
        rows = gate.parse_param_sheet(_sheet_payload(rows=[
            {"name": "bank_source", "value": "nhd_area", "door": "constant",
             "basis": "default_demo"}])).rows
        self.assertEqual(
            gate.resolve_param_sheet_edits(rows, {"bank_source": "constant_ribbon"}),
            {"bank_source": "constant_ribbon"})

    def test_an_unknown_row_is_never_invented(self) -> None:
        self.assertEqual(
            gate.resolve_param_sheet_edits(self.rows, {"ghost": "1"}), {})

    def test_the_summary_names_the_edits(self) -> None:
        sheet = gate.parse_param_sheet(_sheet_payload())
        self.assertIn("3 rows", gate.param_sheet_summary(sheet, {}))
        self.assertIn("water_temp_c",
                      gate.param_sheet_summary(sheet, {"water_temp_c": 24.0}))


def _draw_request(mode: str, purpose: str = "barrier") -> gate.SpatialInputRequest:
    return gate.parse_spatial_input_request({
        "request_id": "01J000000000000000000000BB",
        "mode": mode, "purpose": purpose,
        "title": "Draw outfall", "description": "Click where it enters",
    })


class DrawKindTests(unittest.TestCase):
    def test_the_shape_purposes_are_drawable(self) -> None:
        for purpose, kind in (("aoi", "polygon"), ("line", "polyline")):
            request = _draw_request("vector_draw", purpose)
            self.assertTrue(request.supported, purpose)
            self.assertEqual(request.draw_kind, kind)

    def test_the_tagged_barrier_surface_still_degrades_honestly(self) -> None:
        """Per-segment wall / flap-gate tagging has no plugin affordance; the card
        says so and Cancel closes the gate."""
        request = _draw_request("vector_draw", "barrier")
        self.assertFalse(request.supported)
        self.assertEqual(request.draw_kind, "")

    def test_the_pick_modes_are_unchanged(self) -> None:
        for mode in ("point", "bbox"):
            self.assertTrue(_draw_request(mode).supported)
            self.assertEqual(_draw_request(mode).draw_kind, "")


class DrawReplyTests(unittest.TestCase):
    def test_a_polygon_reply_closes_its_ring(self) -> None:
        wire = gate.resolve_spatial_input_features(
            "rid", "polygon", [[-124.2, 40.4], [-124.0, 40.4], [-124.0, 40.6]])
        ring = wire["features"]["features"][0]["geometry"]["coordinates"][0]
        self.assertEqual(ring[0], ring[-1])
        self.assertEqual(len(ring), 4)
        self.assertEqual(wire["features"]["features"][0]["properties"]["role"],
                         "aoi")
        self.assertEqual(wire["geometry_type"], "vector_draw")
        self.assertIsNone(wire["coordinates"])
        self.assertFalse(wire["cancelled"])

    def test_an_already_closed_ring_is_not_closed_twice(self) -> None:
        ring_in = [[-124.2, 40.4], [-124.0, 40.4], [-124.0, 40.6], [-124.2, 40.4]]
        wire = gate.resolve_spatial_input_features("rid", "polygon", ring_in)
        ring = wire["features"]["features"][0]["geometry"]["coordinates"][0]
        self.assertEqual(len(ring), 4)

    def test_a_polyline_reply_is_a_plain_untagged_linestring(self) -> None:
        wire = gate.resolve_spatial_input_features(
            "rid", "polyline", [[-124.2, 40.4], [-124.0, 40.6]])
        feature = wire["features"]["features"][0]
        self.assertEqual(feature["geometry"]["type"], "LineString")
        self.assertEqual(feature["properties"], {"role": "line"})

    def test_the_vertex_gate_matches_the_geometry(self) -> None:
        self.assertFalse(gate.spatial_input_vertices_ready("polygon", [[0, 0], [1, 1]]))
        self.assertTrue(
            gate.spatial_input_vertices_ready("polygon", [[0, 0], [1, 1], [2, 2]]))
        self.assertFalse(gate.spatial_input_vertices_ready("polyline", [[0, 0]]))
        self.assertTrue(gate.spatial_input_vertices_ready("polyline", [[0, 0], [1, 1]]))

    def test_the_chip_names_what_was_drawn(self) -> None:
        request = _draw_request("vector_draw", "aoi")
        wire = gate.resolve_spatial_input_features(
            "rid", "polygon", [[-124.2, 40.4], [-124.0, 40.4], [-124.0, 40.6]])
        self.assertIn("polygon", gate.spatial_input_summary(request, wire))
        self.assertIn("4 vertices", gate.spatial_input_summary(request, wire))


if __name__ == "__main__":
    unittest.main()
