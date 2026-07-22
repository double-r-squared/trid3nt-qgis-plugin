"""Temporal-animation tests -- frame grouping + range assignment (pure
python, no QGIS required), plus a REAL-QGIS stamping harness run in a
subprocess against the system interpreter (skips honestly when absent),
matching the ``TestQtBridgeStart`` tiering.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trid3nt.render import temporal  # noqa: E402


UTC = timezone.utc
HOUR = timedelta(hours=1)


class TestParseFrameToken(unittest.TestCase):
    def test_step_frame_idx_index(self):
        for name, value in (
            ("Flood depth step 4", 4),
            ("Flood depth frame 02", 2),
            ("Flood depth idx 3", 3),
            ("Flood depth index 12", 12),
        ):
            token = temporal.parse_frame_token(name)
            self.assertIsNotNone(token, name)
            self.assertEqual(token["value"], value)
            self.assertEqual(token["stem"], "flood depth")
            self.assertEqual(token["label"], f"step {value}")

    def test_underscore_names_group_like_spaced_names(self):
        """Exported GeoTIFF stems are _safe_filename outputs -- the plugin
        adaptation over the agent port."""
        token = temporal.parse_frame_token("Flood_depth_step_1")
        self.assertIsNotNone(token)
        self.assertEqual(token["value"], 1)
        self.assertEqual(token["stem"], "flood depth")

    def test_forecast_lead_hour(self):
        token = temporal.parse_frame_token("HRRR reflectivity F+03h")
        self.assertEqual(token["value"], 3)
        self.assertEqual(token["label"], "F+03h")
        self.assertEqual(token["stem"], "hrrr reflectivity")

    def test_hour_token(self):
        token = temporal.parse_frame_token("Depth hr 6")
        self.assertEqual((token["value"], token["label"]), (6, "hr 6"))

    def test_t_plus_token(self):
        token = temporal.parse_frame_token("Surge t+2")
        self.assertEqual((token["value"], token["label"]), (2, "t+2"))

    def test_hash_token(self):
        token = temporal.parse_frame_token("GLM flashes #3")
        self.assertEqual((token["value"], token["label"]), (3, "#3"))

    def test_day_token(self):
        token = temporal.parse_frame_token("Plume day 1")
        self.assertEqual((token["value"], token["label"]), (1, "day 1"))

    def test_iso_valid_time_becomes_label_and_leaves_stem(self):
        token = temporal.parse_frame_token(
            "GOES ABI frame 3 2026-06-22T18:05:00Z"
        )
        self.assertEqual(token["value"], 3)
        self.assertEqual(token["label"], "2026-06-22 18:05Z")
        self.assertEqual(token["stem"], "goes abi")

    def test_non_frame_names_do_not_match(self):
        for name in (
            "NLCD_Land_Cover_2021",
            "USGS_3DEP_DEM_30m",
            "Peak_flood_depth_2",  # dedup suffix, not a frame token
            "",
        ):
            self.assertIsNone(temporal.parse_frame_token(name), name)


class TestGroupFrameLayers(unittest.TestCase):
    def test_export_stem_group(self):
        names = [f"Flood_depth_step_{i}" for i in (3, 1, 2)] + [
            "Peak_flood_depth",
            "USGS_3DEP_DEM_30m",
        ]
        groups = temporal.group_frame_layers(names)
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group.stem, "flood depth")
        self.assertEqual(
            [m.name for m in group.members],
            ["Flood_depth_step_1", "Flood_depth_step_2", "Flood_depth_step_3"],
        )

    def test_single_member_is_not_a_group(self):
        self.assertEqual(temporal.group_frame_layers(["Flood depth step 1"]), [])

    def test_duplicate_values_reject_the_group(self):
        names = ["Depth step 1", "Depth step 1", "Depth step 2"]
        self.assertEqual(temporal.group_frame_layers(names), [])

    def test_groups_ordered_largest_first(self):
        names = (
            [f"Flood depth step {i}" for i in range(1, 4)]
            + [f"GOES frame {i}" for i in range(1, 6)]
        )
        groups = temporal.group_frame_layers(names)
        self.assertEqual([g.stem for g in groups], ["goes", "flood depth"])


class TestAssignFrameRanges(unittest.TestCase):
    def _group(self, names):
        groups = temporal.group_frame_layers(names)
        self.assertEqual(len(groups), 1)
        return groups[0]

    def test_synthetic_ranges_are_contiguous_one_hour_steps(self):
        base = datetime(2026, 7, 7, tzinfo=UTC)
        group = self._group([f"Flood_depth_step_{i}" for i in range(1, 8)])
        ranges = temporal.assign_frame_ranges(group, base=base)
        self.assertEqual(len(ranges), 7)
        for n, (name, begin, end) in enumerate(ranges, start=1):
            self.assertEqual(name, f"Flood_depth_step_{n}")
            self.assertEqual(begin, base + (n - 1) * HOUR)
            self.assertEqual(end, base + n * HOUR)
        for (_, _, prev_end), (_, next_begin, _) in zip(ranges, ranges[1:]):
            self.assertEqual(prev_end, next_begin)  # contiguous

    def test_sparse_values_never_overlap(self):
        base = datetime(2026, 7, 7, tzinfo=UTC)
        group = self._group(["Depth step 1", "Depth step 3"])
        ranges = temporal.assign_frame_ranges(group, base=base)
        self.assertEqual(ranges[0][2], base + 1 * HOUR)
        self.assertEqual(ranges[1][1], base + 2 * HOUR)
        self.assertLessEqual(ranges[0][2], ranges[1][1])

    def test_default_base_is_today_midnight_utc(self):
        base = temporal.default_base_time()
        now = datetime.now(UTC)
        self.assertEqual(
            (base.year, base.month, base.day), (now.year, now.month, now.day)
        )
        self.assertEqual((base.hour, base.minute, base.second), (0, 0, 0))
        self.assertEqual(base.tzinfo, UTC)

    def test_iso_labels_win_when_all_parse(self):
        group = self._group(
            [
                "GOES ABI frame 1 2026-06-22T18:00:00Z",
                "GOES ABI frame 2 2026-06-22T18:10:00Z",
                "GOES ABI frame 3 2026-06-22T18:20:00Z",
            ]
        )
        ranges = temporal.assign_frame_ranges(group)
        t0 = datetime(2026, 6, 22, 18, 0, tzinfo=UTC)
        ten = timedelta(minutes=10)
        self.assertEqual([r[1] for r in ranges], [t0, t0 + ten, t0 + 2 * ten])
        # end = next begin; the last frame reuses the preceding interval.
        self.assertEqual([r[2] for r in ranges], [t0 + ten, t0 + 2 * ten, t0 + 3 * ten])

    def test_non_monotonic_iso_falls_back_to_synthetic(self):
        base = datetime(2026, 7, 7, tzinfo=UTC)
        group = self._group(
            [
                "GOES frame 1 2026-06-22T18:20:00Z",
                "GOES frame 2 2026-06-22T18:00:00Z",
            ]
        )
        ranges = temporal.assign_frame_ranges(group, base=base)
        self.assertEqual(ranges[0][1], base)
        self.assertEqual(ranges[1][2], base + 2 * HOUR)


class TestQtTemporalStamp(unittest.TestCase):
    """Runs ``layers.stamp_temporal`` against REAL QgsRasterLayers in a
    subprocess (the system interpreter with qgis.core); skips honestly when
    no such interpreter exists -- the same tier as TestQtBridgeStart."""

    @staticmethod
    def _qgis_python():
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
                    [py, "-c", "import qgis.core"],
                    capture_output=True,
                    timeout=60,
                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if probe.returncode == 0:
                return py
        return None

    def test_stamp_temporal_on_real_raster_layers(self):
        py = self._qgis_python()
        if py is None:
            self.skipTest("no interpreter with qgis.core available")
        harness = os.path.join(os.path.dirname(__file__), "qt_temporal_harness.py")
        proc = subprocess.run(
            [py, "-u", harness],
            capture_output=True,
            timeout=300,
            text=True,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
        self.assertEqual(
            proc.returncode,
            0,
            "qt temporal harness died (rc="
            f"{proc.returncode})\nstdout: {proc.stdout}\nstderr: {proc.stderr}",
        )
        self.assertIn("QT-TEMPORAL-OK", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
