# Worker-side unit test: the shared post-success run-scratch reaper
# (docs/decisions/0233-runs-retention.md).
#
# Pure logic + a mocked delete_fn -- no S3/MinIO, no rasterio, no clawpack.
# Covers: the right keys deleted, keepers untouched, and a delete failure is
# recorded but never raised (retention hygiene must never fail a run).

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from workers._raster_postprocess.retention import (  # noqa: E402
    match_scratch_keys,
    reap_run_scratch,
)
from workers._geoclaw_postprocess import (  # noqa: E402
    GEOCLAW_SCRATCH_KEEP_PATTERNS,
    GEOCLAW_SCRATCH_PATTERNS,
)


# ---------------------------------------------------------------------------
# match_scratch_keys -- pure matching, no I/O.
# ---------------------------------------------------------------------------

_GEOCLAW_RUN_KEYS = [
    "_output/fort.q0000",
    "_output/fort.q0001",
    "_output/fort.t0000",
    "_output/fort.t0001",
    "_output/fort.b0000",
    "_output/fort.a0000",
    "_output/fgmax0001.txt",
    "_output/fgmax_grids.data",
    "_output/gauge00001.txt",
    "_output/gauge00002.txt",
    "geoclaw_depth_peak.tif",
    "geoclaw_depth_frame_01.tif",
    "publish_manifest.json",
    "deck_manifest.json",
]


def test_match_scratch_keys_geoclaw_reaps_fort_and_data_txt_keeps_gauges() -> None:
    matched = match_scratch_keys(
        _GEOCLAW_RUN_KEYS,
        GEOCLAW_SCRATCH_PATTERNS,
        keep_patterns=GEOCLAW_SCRATCH_KEEP_PATTERNS,
    )
    assert set(matched) == {
        "_output/fort.q0000",
        "_output/fort.q0001",
        "_output/fort.t0000",
        "_output/fort.t0001",
        "_output/fort.b0000",
        "_output/fort.a0000",
        "_output/fgmax0001.txt",
        "_output/fgmax_grids.data",
    }
    # Gauges and every publishable artifact survive.
    for keeper in (
        "_output/gauge00001.txt",
        "_output/gauge00002.txt",
        "geoclaw_depth_peak.tif",
        "geoclaw_depth_frame_01.tif",
        "publish_manifest.json",
        "deck_manifest.json",
    ):
        assert keeper not in matched


def test_match_scratch_keys_never_matches_publishable_extensions() -> None:
    keepers = [
        "geoclaw_depth_peak.tif",
        "publish_manifest.json",
        "some_layer.fgb",
        "chart.png",
    ]
    matched = match_scratch_keys(
        keepers, GEOCLAW_SCRATCH_PATTERNS, keep_patterns=GEOCLAW_SCRATCH_KEEP_PATTERNS
    )
    assert matched == []


def test_match_scratch_keys_dedupes_and_preserves_order() -> None:
    keys = ["_output/fort.q0000", "_output/fort.q0001", "_output/fort.q0000"]
    matched = match_scratch_keys(keys, GEOCLAW_SCRATCH_PATTERNS)
    assert matched == ["_output/fort.q0000", "_output/fort.q0001"]


# ---------------------------------------------------------------------------
# reap_run_scratch -- the delete side-effect, mocked.
# ---------------------------------------------------------------------------


def test_reap_run_scratch_deletes_only_matched_keys() -> None:
    deleted_calls: list[str] = []

    def delete_fn(rel: str) -> None:
        deleted_calls.append(rel)

    result = reap_run_scratch(
        delete_fn,
        "run-123",
        _GEOCLAW_RUN_KEYS,
        GEOCLAW_SCRATCH_PATTERNS,
        keep_patterns=GEOCLAW_SCRATCH_KEEP_PATTERNS,
    )
    assert set(result["deleted"]) == {
        "_output/fort.q0000",
        "_output/fort.q0001",
        "_output/fort.t0000",
        "_output/fort.t0001",
        "_output/fort.b0000",
        "_output/fort.a0000",
        "_output/fgmax0001.txt",
        "_output/fgmax_grids.data",
    }
    assert result["errors"] == []
    assert set(deleted_calls) == set(result["deleted"])
    # Keepers were never even offered to delete_fn.
    assert "_output/gauge00001.txt" not in deleted_calls
    assert "geoclaw_depth_peak.tif" not in deleted_calls
    assert "publish_manifest.json" not in deleted_calls


def test_reap_run_scratch_keepers_untouched_when_no_matches() -> None:
    deleted_calls: list[str] = []
    keys = ["publish_manifest.json", "geoclaw_depth_peak.tif", "_output/gauge00001.txt"]

    result = reap_run_scratch(
        lambda rel: deleted_calls.append(rel),
        "run-456",
        keys,
        GEOCLAW_SCRATCH_PATTERNS,
        keep_patterns=GEOCLAW_SCRATCH_KEEP_PATTERNS,
    )
    assert result == {"deleted": [], "errors": []}
    assert deleted_calls == []


def test_reap_run_scratch_delete_failure_is_recorded_not_raised() -> None:
    def flaky_delete(rel: str) -> None:
        if rel == "_output/fort.q0001":
            raise RuntimeError("simulated S3 delete_object failure")

    result = reap_run_scratch(
        flaky_delete,
        "run-789",
        ["_output/fort.q0000", "_output/fort.q0001", "_output/fort.q0002"],
        ["_output/fort.q*"],
    )
    assert result["deleted"] == ["_output/fort.q0000", "_output/fort.q0002"]
    assert result["errors"] == ["_output/fort.q0001"]


def test_reap_run_scratch_empty_patterns_deletes_nothing() -> None:
    deleted_calls: list[str] = []
    result = reap_run_scratch(
        lambda rel: deleted_calls.append(rel),
        "run-000",
        _GEOCLAW_RUN_KEYS,
        (),
    )
    assert result == {"deleted": [], "errors": []}
    assert deleted_calls == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
