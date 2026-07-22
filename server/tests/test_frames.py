"""Unit tests for the engine-agnostic frame machinery (STEP 1 extract).

Pins the contract the SFINCS/SWMM/GeoClaw/SWAN postprocess modules + the web
``parseFrameToken`` / ``detectSequentialGroups`` scrubber rely on:

  - ``select_frame_time_indices`` even-subsample (endpoints kept, <= cap, distinct
    increasing) - the SAME object the legacy ``_select_frame_time_indices`` alias
    points to.
  - the web frame-token naming (``peak_layer_name`` / ``frame_name`` ``step N`` /
    ``peak_layer_id`` ``-peak-`` token / ``frame_layer_id`` ``-frame-NN-`` token).
  - ``emit_timeseries_layers`` corrupt-frame-degrades-to-peak + "< 2 never groups".
"""

from __future__ import annotations

import re

import pytest

from trid3nt_server.workflows import frames
from trid3nt_server.workflows.frames import (
    MAX_FLOOD_FRAMES,
    EmittedFrame,
    emit_timeseries_layers,
    frame_dest_filename,
    frame_layer_id,
    frame_name,
    peak_layer_id,
    peak_layer_name,
    select_frame_time_indices,
)

# The THIRD FRAME_PATTERNS regex in web/src/LayerPanel.tsx (the step token).
_WEB_STEP_TOKEN_RE = re.compile(r"\b(?:step|frame|idx|index)\s*\+?(\d{1,4})\b", re.I)


# --------------------------------------------------------------------------- #
# select_frame_time_indices (+ the legacy private alias is the SAME object)
# --------------------------------------------------------------------------- #
def test_legacy_private_alias_is_same_object() -> None:
    assert frames._select_frame_time_indices is select_frame_time_indices


def test_under_cap_returns_every_step() -> None:
    idx = select_frame_time_indices(5)
    assert idx == [0, 1, 2, 3, 4]


def test_zero_or_negative_returns_empty() -> None:
    assert select_frame_time_indices(0) == []
    assert select_frame_time_indices(-3) == []


def test_over_cap_subsamples_evenly_endpoints_kept(caplog) -> None:
    import logging

    n = MAX_FLOOD_FRAMES * 3
    with caplog.at_level(
        logging.INFO, logger="trid3nt_server.workflows.postprocess_flood"
    ):
        idx = select_frame_time_indices(n)
    assert len(idx) <= MAX_FLOOD_FRAMES
    assert idx[0] == 0 and idx[-1] == n - 1  # endpoints always kept
    assert idx == sorted(idx) and len(set(idx)) == len(idx)  # distinct increasing
    # The cap-firing log is emitted under the postprocess_flood logger (pinned).
    assert any("exceed MAX_FLOOD_FRAMES" in r.getMessage() for r in caplog.records)


def test_exactly_at_cap_no_subsample() -> None:
    idx = select_frame_time_indices(MAX_FLOOD_FRAMES)
    assert len(idx) == MAX_FLOOD_FRAMES
    assert idx == list(range(MAX_FLOOD_FRAMES))


# --------------------------------------------------------------------------- #
# Web frame-token naming contract
# --------------------------------------------------------------------------- #
def test_peak_layer_name_lowercases_after_peak() -> None:
    assert peak_layer_name("Flood depth") == "Peak flood depth"
    assert peak_layer_name("Wave height") == "Peak wave height"


def test_frame_name_matches_web_step_token() -> None:
    for q in ("Flood depth", "Wave height"):
        for n in (1, 7, 144):
            name = frame_name(n, q)
            assert name == f"{q} step {n}"
            m = _WEB_STEP_TOKEN_RE.search(name)
            assert m is not None and int(m.group(1)) == n


def test_peak_layer_id_carries_load_bearing_peak_token() -> None:
    lid = peak_layer_id("flood-depth", "run-xyz")
    assert lid == "flood-depth-peak-run-xyz"
    assert "-peak-" in lid  # web isPeakLayer keys on this token


def test_frame_layer_id_zero_pads_and_carries_frame_token() -> None:
    assert frame_layer_id("swmm-depth", 3, "run-abc") == "swmm-depth-frame-03-run-abc"
    assert frame_layer_id("swmm-depth", 12, "run-abc") == "swmm-depth-frame-12-run-abc"
    assert "-frame-" in frame_layer_id("x", 1, "r")


def test_frame_dest_filename_zero_pads() -> None:
    assert frame_dest_filename("flood_depth", 4) == "flood_depth_frame_04.tif"
    assert frame_dest_filename("hazard", 10, ".tif") == "hazard_frame_10.tif"


# --------------------------------------------------------------------------- #
# emit_timeseries_layers guards
# --------------------------------------------------------------------------- #
def _frame_writer_ok(frame_no: int, raw_idx: int) -> EmittedFrame:
    return EmittedFrame(
        frame_no=frame_no, uri=f"s3://b/{frame_no}.tif", bbox=None, metrics={}
    )


def test_emit_timeseries_happy_path() -> None:
    frames_out = emit_timeseries_layers(5, write_frame=_frame_writer_ok)
    assert [f.frame_no for f in frames_out] == [1, 2, 3, 4, 5]
    assert all(isinstance(f, EmittedFrame) for f in frames_out)


def test_emit_timeseries_single_frame_never_groups() -> None:
    # n_steps == 1 -> one kept index -> < 2 frames -> dropped to [].
    cleaned = []
    out = emit_timeseries_layers(
        1, write_frame=_frame_writer_ok, cleanup=lambda: cleaned.append(True)
    )
    assert out == []
    assert cleaned == [True]  # cleanup invoked on the < 2 drop


def test_emit_timeseries_corrupt_frame_degrades_to_peak_only() -> None:
    degraded: list[Exception] = []
    cleaned: list[bool] = []

    def _writer(frame_no: int, raw_idx: int) -> EmittedFrame:
        if frame_no == 3:
            raise RuntimeError("corrupt frame 3")
        return _frame_writer_ok(frame_no, raw_idx)

    out = emit_timeseries_layers(
        6,
        write_frame=_writer,
        on_degrade=degraded.append,
        cleanup=lambda: cleaned.append(True),
    )
    # A single corrupt frame abandons the WHOLE animation (peak still stands at
    # the caller) - never re-raised.
    assert out == []
    assert len(degraded) == 1 and isinstance(degraded[0], RuntimeError)
    assert cleaned == [True]


def test_emit_timeseries_never_raises_on_writer_error() -> None:
    def _boom(frame_no: int, raw_idx: int) -> EmittedFrame:
        raise ValueError("kaboom")

    # Must NOT propagate - the peak layer must survive a bad frame set.
    assert emit_timeseries_layers(4, write_frame=_boom) == []
