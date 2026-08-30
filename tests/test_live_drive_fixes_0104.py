"""Regression tests from the live-drive bug-fix wave whose subject is still here.

The wave covered six defects. Four of them lived in the SWMM legs and in the
per-process TELEMAC build scripts, both of which left the tree; what remains is
the pair whose code is still in the server:

- the worker's degenerate-reach metrics surfacing as the typed, retryable gate;
- the object-exists guard that keeps a failed upload from leaving a dangling
  layer handle behind.

Offline-first: no live daemon, no network.
"""

from __future__ import annotations

import pytest


def test_server_maps_reach_degenerate_metrics_to_typed_gate():
    """The worker's TELEMAC_REACH_DEGENERATE metrics surface as the typed,
    retryable server error with .suggestions."""
    from trid3nt_server.workflows.telemac.steps import TelemacReachDegenerateError
    from trid3nt_server.workflows.telemac.steps.solve import raise_if_reach_degenerate

    raise_if_reach_degenerate({"error_code": "SOMETHING_ELSE"})  # no-op

    with pytest.raises(TelemacReachDegenerateError) as ei:
        raise_if_reach_degenerate({
            "error_code": "TELEMAC_REACH_DEGENERATE",
            "reach_length_m": 292.0,
            "degenerate_channel_width_m": 500.0,
        })
    assert ei.value.retryable is True
    assert ei.value.error_code == "TELEMAC_REACH_DEGENERATE"
    assert ei.value.suggestions  # rides the tool-retry loop


def test_s3_object_exists_guard():
    from trid3nt_server.workflows.telemac.steps.products import _s3_object_exists

    class _PresentS3:
        def head_object(self, **kw):
            return {"ContentLength": 10}

    class _AbsentS3:
        def head_object(self, **kw):
            raise RuntimeError("NoSuchKey")

    assert _s3_object_exists(_PresentS3(), "b", "k") is True
    assert _s3_object_exists(_AbsentS3(), "b", "k") is False
