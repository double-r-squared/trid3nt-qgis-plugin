"""Unit tests for ``model_goes_fire_animation`` (fire-demo Track A, UNATTENDED).

This is the no-confirm-gate GOES sibling of model_satellite_fire_animation. The
distinguishing behavior under test:

- run_model_goes_fire_animation is registered (workflow_dispatch).
- ``_resolve_default_window``: end-only / start-only / neither / discovery floor.
- ``_snap_window_to_available`` (the CORE unattended fix): a requested window
  that already lines up is kept; a slightly-off window is SNAPPED to the nearest
  available frames; an empty index returns None (-> honesty floor).
- The workflow PROCEEDS to fetch+publish WITHOUT a review gate (no status="review"
  branch exists) and AUTO-SNAPS a window that misses the available frames.
- It emits frames in the postprocess_flood FRAME SHAPE (distinct layer_ids +
  shared style_preset + a "step N <ISO>" name token + identical bbox).
- Honesty floor: NOTHING available (empty index) raises GOES_FIRE_ANIM_EMPTY;
  available frames that all fetch empty also raise it.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from grace2_agent.tools import TOOL_REGISTRY, RegisteredTool
from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata
from grace2_agent.workflows.model_goes_fire_animation import (
    DEFAULT_GOES_WINDOW,
    GOES_FIRE_PRODUCTS,
    GOESFireAnimEmptyError,
    GOESFireAnimInputError,
    model_goes_fire_animation,
    _resolve_default_window,
    _snap_window_to_available,
)

_BBOX = (-113.346, 39.57, -111.765, 41.115)


def _run(coro):
    return asyncio.run(coro)


def _reg(fn):
    """Wrap a plain callable as a RegisteredTool for patch.dict(TOOL_REGISTRY)."""
    return RegisteredTool(
        metadata=AtomicToolMetadata(
            name="_fake",
            ttl_class="live-no-cache",
            source_class="workflow_dispatch",
            cacheable=False,
        ),
        fn=fn,
        module="test",
    )


async def _async_empty_dict(*a, **k):
    return {}


async def _async_none(*a, **k):
    return None


# ---- timestamp helpers (build a SLIDER-style ascending int list) ------------


def _ts(dt: datetime) -> int:
    return int(dt.strftime("%Y%m%d%H%M%S"))


def _five_min_series(start: datetime, n: int) -> list[int]:
    return [_ts(start + timedelta(minutes=5 * i)) for i in range(n)]


# ---- registration -----------------------------------------------------------


def test_composer_registered_as_workflow_dispatch():
    assert "run_model_goes_fire_animation" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["run_model_goes_fire_animation"]
    assert entry.metadata.source_class == "workflow_dispatch"
    assert entry.metadata.cacheable is False


def test_products_are_goes_only():
    assert set(GOES_FIRE_PRODUCTS) == {"geocolor", "fire_temperature"}


# ---- window resolution ------------------------------------------------------


def test_default_window_end_only_is_six_and_a_half_hours():
    end = datetime(2026, 6, 22, 20, 0, tzinfo=timezone.utc)
    start, e = _resolve_default_window(None, end)
    assert e == end
    assert (end - start) == DEFAULT_GOES_WINDOW


def test_default_window_start_only_extends_forward():
    start_in = datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    s, e = _resolve_default_window(start_in, None)
    assert s == start_in
    assert (e - s) == DEFAULT_GOES_WINDOW


def test_default_window_both_given_is_verbatim():
    s_in = datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    e_in = datetime(2026, 6, 22, 20, 0, tzinfo=timezone.utc)
    s, e = _resolve_default_window(s_in, e_in)
    assert (s, e) == (s_in, e_in)


def test_default_window_respects_discovery_floor():
    end = datetime(2026, 6, 22, 20, 0, tzinfo=timezone.utc)
    # Discovery 18:00Z is LATER than end - 6.5h (13:30Z) -> floors start.
    s, _ = _resolve_default_window(None, end, "2026-06-22T18:00:00Z")
    assert s == datetime(2026, 6, 22, 18, 0, tzinfo=timezone.utc)


# ---- the CORE: auto-snap to available frames --------------------------------


def test_snap_empty_index_returns_none():
    """Nothing available -> None (the caller honesty-floors)."""
    start = datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    end = datetime(2026, 6, 22, 20, 0, tzinfo=timezone.utc)
    assert _snap_window_to_available([], start, end) is None


def test_snap_window_already_aligned_is_kept_as_is():
    """A requested window that lines up with real data is kept unchanged."""
    base = datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    ts = _five_min_series(base, 12)  # 13:30..14:25
    start = datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    end = datetime(2026, 6, 22, 14, 30, tzinfo=timezone.utc)
    snapped = _snap_window_to_available(ts, start, end)
    assert snapped is not None
    snap_start, snap_end, in_window = snapped
    # No snap needed: window unchanged, all 12 frames inside.
    assert snap_start == start and snap_end == end
    assert len(in_window) == 12


def test_snap_off_window_snaps_to_nearest_available():
    """A requested window with NO frames inside is SNAPPED to the nearest
    available frames (the unattended fix -- no parking to re-pick)."""
    # Available data is a single day's afternoon.
    base = datetime(2026, 6, 22, 18, 0, tzinfo=timezone.utc)
    ts = _five_min_series(base, 24)  # 18:00..19:55
    # User asked for the MORNING of the SAME day (no frames there yet).
    start = datetime(2026, 6, 22, 6, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    snapped = _snap_window_to_available(ts, start, end)
    assert snapped is not None
    snap_start, snap_end, in_window = snapped
    # The window moved FORWARD onto the real data (the morning request snapped to
    # the afternoon frames -- the nearest available is 18:00Z).
    assert snap_start == base  # nearest available frame becomes the new start
    assert (snap_end - snap_start) == DEFAULT_GOES_WINDOW
    assert in_window  # real frames now selected
    assert all(snap_start <= _to_dt(t) <= snap_end for t in in_window)


def test_snap_far_window_falls_back_to_most_recent_available():
    """A requested window FAR outside the tolerance falls back to the most recent
    window of available data rather than parking / erroring."""
    # Available data is ~6 months in the past relative to the requested window.
    base = datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)
    ts = _five_min_series(base, 12)
    start = datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    end = datetime(2026, 6, 22, 20, 0, tzinfo=timezone.utc)
    snapped = _snap_window_to_available(ts, start, end)
    assert snapped is not None
    snap_start, snap_end, in_window = snapped
    # Snapped to the most-recent available frame's window (Jan, not June).
    assert snap_end == _to_dt(ts[-1])
    assert in_window


def _to_dt(t: int) -> datetime:
    s = f"{int(t):014d}"
    return datetime(
        int(s[0:4]), int(s[4:6]), int(s[6:8]),
        int(s[8:10]), int(s[10:12]), int(s[12:14]),
        tzinfo=timezone.utc,
    )


# ---- input validation -------------------------------------------------------


def test_bad_bbox_raises_input_error():
    with pytest.raises(GOESFireAnimInputError):
        _run(model_goes_fire_animation((1.0, 2.0)))  # type: ignore[arg-type]


def test_unknown_product_raises_input_error():
    with pytest.raises(GOESFireAnimInputError):
        _run(model_goes_fire_animation(_BBOX, products=["day_fire"]))


def test_unknown_satellite_raises_input_error():
    with pytest.raises(GOESFireAnimInputError):
        _run(model_goes_fire_animation(_BBOX, satellite="goes-99"))


# ---- the workflow: proceeds WITHOUT a review gate + emits frame shape -------


def _blend_frame(step, ts_iso, bbox):
    """A blended GOES frame in the emitted shape: ONE shared layer_id prefix +
    single product-label name with a "step <N>" token + the real ISO valid-time."""
    return LayerURI(
        layer_id=f"goes-fire-blend-{ts_iso}",
        name=f"GOES Fire (GeoColor + Fire Temperature) step {step} {ts_iso} (GOES-18)",
        layer_type="raster",
        uri=f"s3://fake/blend/{ts_iso}.tif",
        style_preset="goes_rgb_animation",
        role="context",
        units=None,
        bbox=bbox,
    )


_GOES_TIMES = (
    "2026-06-22T18:00:00Z",
    "2026-06-22T18:05:00Z",
    "2026-06-22T18:10:00Z",
)


def _patch_slider(ts: list[int]):
    """Patch the workflow's SLIDER reader so the auto-snap sees ``ts`` (no net)."""
    return patch(
        "grace2_agent.workflows.model_goes_fire_animation._read_slider_timestamps",
        new=_make_async_reader(ts),
    )


def _make_async_reader(ts: list[int]):
    async def _reader(product, satellite, sector):
        return list(ts)

    return _reader


def test_default_run_proceeds_unattended_and_emits_blended_group():
    """The default (both products) GOES run PROCEEDS to fetch+publish with NO
    review gate, and emits ONE blended scrubber group in the frame shape."""
    bbox = _BBOX
    available = _five_min_series(
        datetime(2026, 6, 22, 18, 0, tzinfo=timezone.utc), 24
    )

    def _fake_blend(b, satellite="goes-18", *a, **k):
        return [_blend_frame(i, t, bbox) for i, t in enumerate(_GOES_TIMES, start=1)]

    with patch.dict(
        TOOL_REGISTRY,
        {"fetch_goes_blend_animation": _reg(_fake_blend)},
    ), _patch_slider(available), patch(
        "grace2_agent.workflows.model_goes_fire_animation._safe_overlay_firms",
        _async_none,
    ), patch(
        "grace2_agent.workflows.model_goes_fire_animation._publish_layers",
        _async_empty_dict,
    ):
        result = _run(
            model_goes_fire_animation(
                bbox,
                start_utc="2026-06-22T18:00:00Z",
                end_utc="2026-06-22T18:30:00Z",
                overlay_firms=False,
            )
        )

    # No review gate exists -- it ran straight through to ok.
    assert result["status"] == "ok"
    assert "review" not in {result["status"]}
    assert result["n_frames"] == len(_GOES_TIMES)
    assert set(result["products"]) == {"geocolor", "fire_temperature"}

    layers = result["layers"]
    # ONE blended group: shared layer_id prefix + shared product label.
    assert all(lyr["layer_id"].startswith("goes-fire-blend-") for lyr in layers)
    assert all("Fire (GeoColor + Fire Temperature)" in lyr["name"] for lyr in layers)
    # Frame-contract: distinct ids + shared style_preset + step token + ISO time.
    ids = [lyr["layer_id"] for lyr in layers]
    assert len(set(ids)) == len(ids)
    assert {lyr["style_preset"] for lyr in layers} == {"goes_rgb_animation"}
    assert all(lyr["role"] == "context" for lyr in layers)
    steptimes = {}
    for lyr in layers:
        step = int(re.search(r"step (\d+)", lyr["name"]).group(1))
        iso = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", lyr["name"]).group(1)
        steptimes[step] = iso
    assert set(steptimes) == {1, 2, 3}
    assert set(steptimes.values()) == set(_GOES_TIMES)


def test_off_window_request_is_auto_snapped_not_parked():
    """A requested window that misses the available frames is AUTO-SNAPPED and the
    run still produces frames -- it NEVER parks asking the user to re-pick."""
    bbox = _BBOX
    # Real data sits in the afternoon ...
    available = _five_min_series(
        datetime(2026, 6, 22, 18, 0, tzinfo=timezone.utc), 24
    )
    seen_window = {}

    def _fake_blend(b, satellite, sector, start_iso, end_iso, *a, **k):
        seen_window["start"] = start_iso
        seen_window["end"] = end_iso
        return [_blend_frame(i, t, bbox) for i, t in enumerate(_GOES_TIMES, start=1)]

    with patch.dict(
        TOOL_REGISTRY,
        {"fetch_goes_blend_animation": _reg(_fake_blend)},
    ), _patch_slider(available), patch(
        "grace2_agent.workflows.model_goes_fire_animation._safe_overlay_firms",
        _async_none,
    ), patch(
        "grace2_agent.workflows.model_goes_fire_animation._publish_layers",
        _async_empty_dict,
    ):
        result = _run(
            model_goes_fire_animation(
                bbox,
                # User asked for the MORNING (no frames there) -> must snap fwd.
                start_utc="2026-06-22T06:00:00Z",
                end_utc="2026-06-22T12:00:00Z",
                overlay_firms=False,
            )
        )

    assert result["status"] == "ok"
    assert result["snapped"] is True
    # The fetcher was called with the SNAPPED window (afternoon), not the
    # requested morning window.
    assert seen_window["start"].startswith("2026-06-22T18:00")
    # The requested window is still reported for transparency.
    assert result["requested_start_utc"].startswith("2026-06-22T06:00")
    assert result["n_frames"] == len(_GOES_TIMES)


def test_single_product_emits_un_blended_group():
    """A single GOES product dispatches the un-blended fetch_goes_animation."""
    bbox = _BBOX
    available = _five_min_series(
        datetime(2026, 6, 22, 18, 0, tzinfo=timezone.utc), 12
    )

    def _fake_goes(b, band, *a, **k):
        assert band == "geocolor"
        return [
            LayerURI(
                layer_id=f"goes-anim-geocolor-{_GOES_TIMES[0]}",
                name=f"GOES GeoColor step 1 {_GOES_TIMES[0]} (GOES-18)",
                layer_type="raster",
                uri=f"s3://fake/geocolor/{_GOES_TIMES[0]}.tif",
                style_preset="goes_rgb_animation",
                role="context",
                units=None,
                bbox=bbox,
            )
        ]

    with patch.dict(
        TOOL_REGISTRY,
        {"fetch_goes_animation": _reg(_fake_goes)},
    ), _patch_slider(available), patch(
        "grace2_agent.workflows.model_goes_fire_animation._safe_overlay_firms",
        _async_none,
    ), patch(
        "grace2_agent.workflows.model_goes_fire_animation._publish_layers",
        _async_empty_dict,
    ):
        result = _run(
            model_goes_fire_animation(
                bbox,
                products=["geocolor"],
                start_utc="2026-06-22T18:00:00Z",
                end_utc="2026-06-22T18:30:00Z",
                overlay_firms=False,
            )
        )

    assert result["status"] == "ok"
    assert result["products"] == ["geocolor"]
    assert result["n_frames"] == 1
    assert all("GeoColor" in lyr["name"] for lyr in result["layers"])
    assert not any(
        "Fire (GeoColor + Fire Temperature)" in lyr["name"] for lyr in result["layers"]
    )


def test_firms_overlay_is_co_registered_static_layer():
    """The FIRMS overlay is added as a co-registered (same bbox) static layer."""
    bbox = _BBOX
    available = _five_min_series(
        datetime(2026, 6, 22, 18, 0, tzinfo=timezone.utc), 12
    )

    def _fake_blend(*a, **k):
        return [_blend_frame(i, t, bbox) for i, t in enumerate(_GOES_TIMES, start=1)]

    firms_layer = LayerURI(
        layer_id="firms-viirs-static",
        name="NASA FIRMS active fires - VIIRS NOAA-20 (2026-06-22)",
        layer_type="vector",
        uri="s3://fake/firms.fgb",
        style_preset="firms_active_fire",
        role="primary",
        units=None,
        bbox=bbox,
    )

    def _fake_firms(b, days_back=1, source="VIIRS_NOAA20_NRT", date=None, *a, **k):
        # The overlay is fetched with the historical-date positional (a past day).
        assert date is not None
        return firms_layer

    with patch.dict(
        TOOL_REGISTRY,
        {
            "fetch_goes_blend_animation": _reg(_fake_blend),
            "fetch_firms_active_fire": _reg(_fake_firms),
        },
    ), _patch_slider(available), patch(
        "grace2_agent.workflows.model_goes_fire_animation._publish_layers",
        _async_empty_dict,
    ):
        result = _run(
            model_goes_fire_animation(
                bbox,
                start_utc="2026-06-22T18:00:00Z",
                end_utc="2026-06-22T18:30:00Z",
                overlay_firms=True,
            )
        )

    assert result["status"] == "ok"
    assert result["n_overlays"] == 1
    firms = [lyr for lyr in result["layers"] if lyr["layer_id"] == "firms-viirs-static"]
    assert firms, "FIRMS overlay layer missing"
    assert firms[0]["layer_type"] == "vector"


# ---- honesty floor ----------------------------------------------------------


def test_empty_index_raises_honesty_floor():
    """NOTHING available (empty SLIDER index) raises GOES_FIRE_ANIM_EMPTY."""
    with patch.dict(
        TOOL_REGISTRY, {"fetch_goes_blend_animation": _reg(lambda *a, **k: [])}
    ), _patch_slider([]):
        with pytest.raises(GOESFireAnimEmptyError) as ei:
            _run(
                model_goes_fire_animation(
                    _BBOX,
                    start_utc="2026-06-22T18:00:00Z",
                    end_utc="2026-06-22T18:30:00Z",
                    overlay_firms=False,
                )
            )
    assert ei.value.error_code == "GOES_FIRE_ANIM_EMPTY"


def test_available_but_all_frames_empty_raises_honesty_floor():
    """Timestamps existed but every fetched frame was empty/off-grid -> empty."""
    available = _five_min_series(
        datetime(2026, 6, 22, 18, 0, tzinfo=timezone.utc), 12
    )

    def _fake_blend(*a, **k):
        from grace2_agent.tools.fetch_goes_animation import GOESAnimEmptyError

        raise GOESAnimEmptyError("every frame empty over AOI")

    with patch.dict(
        TOOL_REGISTRY, {"fetch_goes_blend_animation": _reg(_fake_blend)}
    ), _patch_slider(available), patch(
        "grace2_agent.workflows.model_goes_fire_animation._safe_overlay_firms",
        _async_none,
    ), patch(
        "grace2_agent.workflows.model_goes_fire_animation._publish_layers",
        _async_empty_dict,
    ):
        with pytest.raises(GOESFireAnimEmptyError):
            _run(
                model_goes_fire_animation(
                    _BBOX,
                    start_utc="2026-06-22T18:00:00Z",
                    end_utc="2026-06-22T18:30:00Z",
                    overlay_firms=False,
                )
            )
