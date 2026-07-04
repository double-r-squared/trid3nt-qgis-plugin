"""COASTAL/WAVE animation-cadence ("looks like rain" fix) tests.

A coastal surge+SnapWave flood animation rendered at HOURLY frames (the legacy
``dtout = duration/24`` deck cadence + the 24-frame postprocess cap) reads like a
slowly-filling bathtub: waves move in seconds-to-minutes so an hourly snapshot of
a rising surge hides the wave motion regardless of the wave model. The fix: for
COASTAL / quadtree / wave runs, output map frames at a FINE minute-scale interval
over a focused window; the PLUVIAL path stays hourly (byte-identical).

These tests pin the three load-bearing pieces:

(a) a COASTAL run resolves a FINE output interval (minutes) and the >24-frame
    capability (``MAX_FLOOD_FRAMES`` cap raised from 24 -> 144 so a fine-cadence
    run emits all its frames);
(b) the deck ``dtout``/``dtmaxout`` (regular grid) AND the quadtree deck-build
    ``output_dt`` reflect the requested interval, floored at 60 s;
(c) a PLUVIAL run resolves ``None`` -> the legacy hourly cadence, <=24 frames,
    and the deck YAML forcing/cadence is BYTE-IDENTICAL to the pre-cadence deck.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from grace2_agent.workflows.model_flood_scenario import (
    _COASTAL_OUTPUT_INTERVAL_MIN_DEFAULT,
    _estimate_frame_count,
    _resolve_output_interval_min,
)
from grace2_agent.workflows.postprocess_flood import (
    MAX_FLOOD_FRAMES,
    _select_frame_time_indices,
)
from grace2_agent.workflows.sfincs_builder import (
    BuildOptions,
    ForcingSpec,
    _generate_hydromt_yaml_config,
)

_BBOX = (-82.0, 26.5, -81.8, 26.7)  # Fort Myers-ish coastal AOI


def _pluvial_forcing(sim_hours: float) -> ForcingSpec:
    return ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=12.1,
        duration_hours=sim_hours,
        return_period_years=100,
        provenance={"vintage_volume": "NOAA Atlas 14 Volume 1"},
    )


def _yaml(options: BuildOptions, sim_hours: float) -> str:
    return _generate_hydromt_yaml_config(
        bbox=_BBOX,
        options=options,
        dem_local_path="/tmp/dem.tif",
        landcover_local_path="/tmp/lc.tif",
        river_local_path=None,
        forcing=_pluvial_forcing(sim_hours),
        mapping_csv_path="/tmp/manning.csv",
    )


# --------------------------------------------------------------------------- #
# (a) COASTAL resolves a FINE interval + the cap is lifted past 24
# --------------------------------------------------------------------------- #


def test_coastal_resolves_fine_interval_default() -> None:
    """A coastal run with no explicit interval -> the FINE minute-scale default."""
    resolved = _resolve_output_interval_min(
        is_coastal=True, output_interval_min=None, duration_hr=24.0
    )
    assert resolved is not None
    assert resolved == _COASTAL_OUTPUT_INTERVAL_MIN_DEFAULT
    # FINE means well under the hourly (60 min) stride.
    assert resolved < 60.0


def test_coastal_explicit_interval_honored() -> None:
    """An explicit coastal interval overrides the default (floored at 1 min)."""
    assert (
        _resolve_output_interval_min(
            is_coastal=True, output_interval_min=10, duration_hr=24.0
        )
        == 10.0
    )
    # Sub-floor request is clamped UP to 1 min (the deck re-floors at 60 s).
    assert (
        _resolve_output_interval_min(
            is_coastal=True, output_interval_min=0.1, duration_hr=24.0
        )
        == 1.0
    )


def test_max_flood_frames_cap_lifted_past_24() -> None:
    """The hard 24-frame cap is raised so a fine-cadence run emits all its frames."""
    assert MAX_FLOOD_FRAMES > 24
    assert MAX_FLOOD_FRAMES == 144


def test_coastal_fine_cadence_yields_more_than_24_frames() -> None:
    """A fine coastal cadence over a focused window can produce >24 frames now."""
    # 5-min stride over a 6 h focused window = 72 raw frames -> all kept (<=144).
    frames = _estimate_frame_count(output_interval_min=5.0, duration_hr=6.0)
    assert frames > 24
    assert frames <= MAX_FLOOD_FRAMES
    # The frame-index subsampler also keeps >24 frames when the raw count fits.
    idx = _select_frame_time_indices(72)
    assert len(idx) == 72  # no subsample below the cap
    assert idx[0] == 0 and idx[-1] == 71  # endpoints always kept


def test_postprocess_subsamples_and_logs_when_over_cap(caplog) -> None:
    """A run that STILL exceeds the cap is subsampled EVENLY (never silently)."""
    import logging

    with caplog.at_level(logging.INFO, logger="grace2_agent.workflows.postprocess_flood"):
        idx = _select_frame_time_indices(MAX_FLOOD_FRAMES * 3)
    assert len(idx) <= MAX_FLOOD_FRAMES
    assert idx[0] == 0 and idx[-1] == MAX_FLOOD_FRAMES * 3 - 1  # endpoints kept
    # The cap is LOGGED (kickoff: never silently truncate).
    assert any(
        "exceed MAX_FLOOD_FRAMES" in rec.getMessage() for rec in caplog.records
    )


# --------------------------------------------------------------------------- #
# (b) deck dtout / quadtree output_dt reflect the requested interval + floor
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "interval_min, expected_dtout",
    [
        (5.0, 300),    # 5 min -> 300 s
        (10.0, 600),   # 10 min -> 600 s
        (1.0, 60),     # 1 min -> 60 s (the wave floor)
        (0.5, 60),     # 30 s requested -> floored at 60 s
    ],
)
def test_coastal_deck_dtout_reflects_interval(
    interval_min: float, expected_dtout: int
) -> None:
    """A coastal BuildOptions.output_interval_min drives dtout/dtmaxout (seconds),
    floored at 60 s (waves justify sub-10-min output)."""
    options = BuildOptions(
        grid_resolution_m=30.0,
        simulation_hours=24.0,
        output_interval_min=interval_min,
    )
    yaml_text = _yaml(options, 24.0)
    assert f"dtout: {expected_dtout}" in yaml_text, (
        f"dtout must be {expected_dtout} for output_interval_min={interval_min}; "
        f"yaml=\n{yaml_text}"
    )
    assert f"dtmaxout: {expected_dtout}" in yaml_text


def test_quadtree_deckbuild_output_dt_reflects_interval(monkeypatch) -> None:
    """The quadtree+SnapWave deck-build output_dt follows the fine cadence too."""
    from grace2_agent.tools import solver as solver_mod
    from grace2_agent.workflows.model_flood_scenario import (
        _compose_and_upload_deckbuild_spec,
    )

    class _FakeS3:
        def __init__(self) -> None:
            self.objects: dict[tuple[str, str], bytes] = {}

        def put_object(self, Bucket, Key, Body, **_kw):  # noqa: N803
            data = Body.read() if hasattr(Body, "read") else bytes(Body)
            self.objects[(Bucket, Key)] = data
            return {}

    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("GRACE2_CACHE_BUCKET", "deck-cache-bucket")
    s3 = _FakeS3()
    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: s3)

    class _FakeModelSetup:
        parameters = {"crs": "EPSG:3857", "forcing_provenance": {}}

    class _FakeForcingSpec:
        provenance: dict[str, Any] = {}

    # The workflow maps a 5-min coastal interval -> output_dt_s = 300.0.
    build_spec_uri = _compose_and_upload_deckbuild_spec(
        bbox=_BBOX,
        topobathy_uri="s3://topo-bucket/topobathy.tif",
        bathymetry_present=True,
        model_setup=_FakeModelSetup(),
        forcing_spec=_FakeForcingSpec(),
        surge_forcing=None,
        grid_resolution_m=30.0,
        duration_hr=6.0,
        output_dt_s=300.0,
        is_coastal=True,
        return_period_yr=100,
    )
    s3_bucket, _, key = build_spec_uri[len("s3://"):].partition("/")
    composed = json.loads(s3.objects[(s3_bucket, key)])
    assert composed["output"]["output_dt"] == 300.0


# --------------------------------------------------------------------------- #
# (c) PLUVIAL is UNCHANGED  -  None interval, hourly cadence, <=24 frames,
#     and the deck YAML is byte-identical to the pre-cadence deck.
# --------------------------------------------------------------------------- #


def test_pluvial_resolves_none_interval() -> None:
    """The pluvial path ALWAYS resolves None (legacy hourly), even if a stray
    interval was passed -> the pluvial deck is never touched."""
    assert (
        _resolve_output_interval_min(
            is_coastal=False, output_interval_min=None, duration_hr=24.0
        )
        is None
    )
    # Even a stray explicit value is ignored for a non-coastal run (regression
    # guard: pluvial stays byte-identical).
    assert (
        _resolve_output_interval_min(
            is_coastal=False, output_interval_min=5, duration_hr=24.0
        )
        is None
    )


def test_pluvial_frame_count_stays_hourly_and_bounded() -> None:
    """Pluvial -> ~1 frame/hour, never more than the legacy 24 over a 1-day sim."""
    assert _estimate_frame_count(output_interval_min=None, duration_hr=24.0) == 24
    assert _estimate_frame_count(output_interval_min=None, duration_hr=12.0) == 12


@pytest.mark.parametrize(
    "sim_hours, expected_dtout",
    [
        (24.0, max(600, int(24 * 3600 / 24))),  # 3600 s
        (48.0, max(600, int(48 * 3600 / 24))),  # 7200 s
        (2.0, max(600, int(2 * 3600 / 24))),    # floors at 600 s
    ],
)
def test_pluvial_deck_dtout_byte_identical(
    sim_hours: float, expected_dtout: int
) -> None:
    """With output_interval_min=None (the pluvial default) the deck dtout is the
    legacy max(600, total/24)  -  byte-identical to the pre-cadence deck."""
    # The default BuildOptions has output_interval_min=None.
    default_opts = BuildOptions(grid_resolution_m=30.0, simulation_hours=sim_hours)
    assert default_opts.output_interval_min is None
    yaml_default = _yaml(default_opts, sim_hours)
    assert f"dtout: {expected_dtout}" in yaml_default
    assert f"dtmaxout: {expected_dtout}" in yaml_default

    # Passing output_interval_min=None EXPLICITLY produces the SAME YAML string
    # (byte-identical forcing/cadence) as the implicit default.
    explicit_none = BuildOptions(
        grid_resolution_m=30.0, simulation_hours=sim_hours, output_interval_min=None
    )
    assert _yaml(explicit_none, sim_hours) == yaml_default
