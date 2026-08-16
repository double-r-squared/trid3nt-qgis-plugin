"""Degenerate-numeric clamp tests -- the QGIS 4 / Qt6 / macOS arm64 dock-open
SIGBUS fix (v0.3.8).

Root cause: a non-finite (NaN/inf) or zero-span range reaching a NATIVE Qt
double-to-string call has its precision computed as a non-finite double and
cast to a C int; on arm64 that saturates to INT_MAX, and ``qt_doubleToAscii``
overruns the stack. These are the pure-python guards that keep any such value
away from the native boundary -- the crash is untestable off-arm64, so we test
the CLAMP (that no path can produce an unbounded precision or a degenerate
range) instead.

Covers ``render.formatting`` (the shared clamp seam) and ``ui.charts._as_float``
(the chart-series finiteness drop). Both are qgis-free -> this venv runs them.
"""

from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from plugin.render import formatting as F  # noqa: E402
from plugin.ui import charts  # noqa: E402

NAN = float("nan")
INF = float("inf")


class TestIsFiniteNumber(unittest.TestCase):
    def test_accepts_real_numbers(self):
        for v in (0, -1, 3.14, 1e300, -2.5):
            self.assertTrue(F.is_finite_number(v), v)

    def test_rejects_non_finite_and_non_numeric(self):
        for v in (NAN, INF, -INF, None, "3", True, False, [1]):
            self.assertFalse(F.is_finite_number(v), v)


class TestClampDecimals(unittest.TestCase):
    def test_in_band_passthrough(self):
        self.assertEqual(F.clamp_decimals(0), 0)
        self.assertEqual(F.clamp_decimals(6), 6)
        self.assertEqual(F.clamp_decimals(12), 12)

    def test_truncates_float_toward_zero(self):
        self.assertEqual(F.clamp_decimals(3.9), 3)

    def test_over_ceiling_clamped_to_max(self):
        self.assertEqual(F.clamp_decimals(999), F.MAX_DECIMALS)
        # The exact register value from the crash log -- must NOT pass through.
        self.assertEqual(F.clamp_decimals(0x7FFFFFFF), F.MAX_DECIMALS)

    def test_below_floor_clamped_to_min(self):
        self.assertEqual(F.clamp_decimals(-4), F.MIN_DECIMALS)

    def test_non_finite_falls_back_to_default(self):
        for bad in (NAN, INF, -INF, None, "x", True):
            self.assertEqual(F.clamp_decimals(bad), F.DEFAULT_DECIMALS, bad)

    def test_custom_default_also_bounded(self):
        # A caller-supplied default that is itself absurd is still bounded.
        self.assertEqual(F.clamp_decimals(NAN, default=10_000), F.MAX_DECIMALS)


class TestSaneRange(unittest.TestCase):
    def test_valid_increasing_passthrough(self):
        self.assertEqual(F.sane_range(0.0, 10.0), (0.0, 10.0))
        self.assertEqual(F.sane_range(-5, 5), (-5.0, 5.0))

    def test_nan_defeats_naive_guard_but_not_this_one(self):
        # nan <= nan is False -- the exact bug this replaces. Must NOT pass.
        self.assertEqual(F.sane_range(NAN, NAN), (0.0, 1.0))
        self.assertEqual(F.sane_range(0.0, NAN), (0.0, 1.0))
        self.assertEqual(F.sane_range(NAN, 10.0), (0.0, 1.0))

    def test_inf_bounds_rejected(self):
        self.assertEqual(F.sane_range(0.0, INF), (0.0, 1.0))
        self.assertEqual(F.sane_range(-INF, 0.0), (0.0, 1.0))
        self.assertEqual(F.sane_range(-INF, INF), (0.0, 1.0))

    def test_degenerate_and_inverted_span_rejected(self):
        self.assertEqual(F.sane_range(5.0, 5.0), (0.0, 1.0))  # zero span
        self.assertEqual(F.sane_range(10.0, 1.0), (0.0, 1.0))  # inverted

    def test_custom_default(self):
        self.assertEqual(F.sane_range(NAN, NAN, default=(0.0, 100.0)),
                         (0.0, 100.0))

    def test_is_sane_range_predicate(self):
        self.assertTrue(F.is_sane_range(0.0, 1.0))
        self.assertFalse(F.is_sane_range(NAN, NAN))
        self.assertFalse(F.is_sane_range(0.0, INF))
        self.assertFalse(F.is_sane_range(1.0, 1.0))


class TestDecimalsForRange(unittest.TestCase):
    def test_never_exceeds_band_across_extreme_spans(self):
        spans = [
            (0.0, 1e-30), (0.0, 1e-9), (0.0, 1.0), (0.0, 10.0),
            (0.0, 1e6), (0.0, 1e30), (-1e12, 1e12),
        ]
        for lo, hi in spans:
            d = F.decimals_for_range(lo, hi)
            self.assertGreaterEqual(d, F.MIN_DECIMALS, (lo, hi))
            self.assertLessEqual(d, F.MAX_DECIMALS, (lo, hi))

    def test_tiny_span_wants_more_decimals_still_bounded(self):
        # 1e-9 span -> raw 1 - floor(-9) = 10, inside the band.
        self.assertEqual(F.decimals_for_range(0.0, 1e-9), 10)

    def test_huge_span_clamped_to_floor(self):
        self.assertEqual(F.decimals_for_range(0.0, 1e30), F.MIN_DECIMALS)

    def test_degenerate_and_non_finite_use_default(self):
        for lo, hi in [(5.0, 5.0), (NAN, NAN), (0.0, INF), (10.0, 1.0)]:
            self.assertEqual(F.decimals_for_range(lo, hi), F.DEFAULT_DECIMALS,
                             (lo, hi))


class TestFormatNumber(unittest.TestCase):
    def test_finite_compact_default(self):
        self.assertEqual(F.format_number(3.5), "3.5")
        self.assertEqual(F.format_number(0), "0")

    def test_fixed_decimals_clamped(self):
        self.assertEqual(F.format_number(math.pi, decimals=2), "3.14")
        # An absurd decimals request is clamped, not passed to the formatter.
        out = F.format_number(1.0, decimals=0x7FFFFFFF)
        self.assertEqual(out, "1." + "0" * F.MAX_DECIMALS)

    def test_non_finite_uses_fallback_not_nan_string(self):
        self.assertEqual(F.format_number(NAN), "n/a")
        self.assertEqual(F.format_number(INF), "n/a")
        self.assertEqual(F.format_number(-INF, fallback="--"), "--")


class TestChartAsFloat(unittest.TestCase):
    """The chart-series finiteness drop -- a NaN/inf in a persisted spec must
    not poison the matplotlib auto-range into a degenerate extent."""

    def test_finite_numbers_pass(self):
        self.assertEqual(charts._as_float(3), 3.0)
        self.assertEqual(charts._as_float(-2.5), -2.5)
        self.assertEqual(charts._as_float(0), 0.0)

    def test_non_finite_dropped(self):
        self.assertIsNone(charts._as_float(NAN))
        self.assertIsNone(charts._as_float(INF))
        self.assertIsNone(charts._as_float(-INF))

    def test_bool_and_non_numeric_dropped(self):
        self.assertIsNone(charts._as_float(True))
        self.assertIsNone(charts._as_float("5"))
        self.assertIsNone(charts._as_float(None))


if __name__ == "__main__":
    unittest.main()
