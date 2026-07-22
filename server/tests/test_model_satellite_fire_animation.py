"""Unit tests for ``model_satellite_fire_animation`` (fire-animation demos S5/J5).

Coverage:
- run_model_satellite_fire_animation registered (workflow_dispatch) + the new
  fire-animation tools grew the TOOL_REGISTRY.
- Product routing: GOES products -> fetch_goes_animation; day_fire ->
  fetch_viirs_day_fire.
- Default window per family (GOES ~6.5h, VIIRS ~4d) + discovery floor.
- The workflow STOPS at the bbox/window review gate (confirm=false) and returns
  the AOI bbox + planned frame counts WITHOUT fetching imagery.
- On confirm=true it emits frames in the postprocess_flood SHAPE (distinct
  layer_ids + shared style_preset + an ISO-time NAME token + identical bbox) and
  honesty-floors an empty run.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_contracts.execution import LayerURI
from trid3nt_server.workflows.model_satellite_fire_animation import (
    GOES_PRODUCTS,
    SUPPORTED_PRODUCTS,
    VIIRS_PRODUCTS,
    SatelliteFireAnimationInputError,
    _default_window_for_product,
    _product_to_fetcher,
    model_satellite_fire_animation,
)


# ---- registration / registry growth ---------------------------------------


def test_composer_registered_as_workflow_dispatch():
    assert "run_model_satellite_fire_animation" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["run_model_satellite_fire_animation"]
    assert entry.metadata.source_class == "workflow_dispatch"
    assert entry.metadata.cacheable is False


def test_registry_grew_by_the_new_fire_animation_tools():
    # The four new tools this build adds must all be present.
    for name in (
        "fetch_wfigs_incident",
        "fetch_goes_animation",
        "fetch_viirs_day_fire",
        "run_model_satellite_fire_animation",
    ):
        assert name in TOOL_REGISTRY, f"{name} not registered"


# ---- product routing ------------------------------------------------------


def test_product_routing():
    assert _product_to_fetcher("geocolor") == "fetch_goes_animation"
    assert _product_to_fetcher("fire_temperature") == "fetch_goes_animation"
    assert _product_to_fetcher("day_fire") == "fetch_viirs_day_fire"
    assert set(GOES_PRODUCTS) | set(VIIRS_PRODUCTS) == set(SUPPORTED_PRODUCTS)


def test_product_routing_unknown_raises():
    with pytest.raises(SatelliteFireAnimationInputError):
        _product_to_fetcher("night_microphysics")


# ---- window derivation ----------------------------------------------------


def test_default_window_goes_is_about_six_and_a_half_hours():
    end = datetime(2026, 6, 22, 20, 0, tzinfo=timezone.utc)
    start, e = _default_window_for_product("geocolor", None, end)
    assert e == end
    assert (end - start).total_seconds() == pytest.approx(6.5 * 3600)


def test_default_window_viirs_is_four_days():
    end = datetime(2026, 5, 19, 22, 1, tzinfo=timezone.utc)
    start, e = _default_window_for_product("day_fire", None, end)
    assert (e - start).days == 4


def test_default_window_respects_discovery_floor():
    end = datetime(2026, 6, 22, 20, 0, tzinfo=timezone.utc)
    # Discovery at 18:00Z is LATER than end - 6.5h (13:30Z) so it floors start.
    start, _ = _default_window_for_product("geocolor", "2026-06-22T18:00:00Z", end)
    assert start == datetime(2026, 6, 22, 18, 0, tzinfo=timezone.utc)


# ---- review gate + execute (mocked registry) ------------------------------


_INCIDENT = {
    "incident_name": "Iron",
    "lat": 39.96976,
    "lon": -112.16481,
    "bbox": [-113.346, 39.57, -111.765, 41.115],
    "fire_discovery_datetime": "2026-06-20T00:00:00Z",
    "incident_size_acres": 21935,
    "poo_state": "US-UT",
}


def _fake_wfigs(name, state=None, *a, **k):
    return dict(_INCIDENT)


def _fake_geocode_precise(query, *a, **k):
    """A PRECISE Nominatim geocode (no fallback_reason / not a state snap)."""
    return {
        "name": "Eureka, Juab County, Utah",
        "bbox": [-112.30, 39.90, -112.05, 40.05],
        "latitude": 39.96,
        "longitude": -112.16,
        "source": "nominatim",
    }


def _fake_geocode_coarse(query, *a, **k):
    """A COARSE state-snap geocode (the Santa Rosa Island failure mode)."""
    return {
        "name": "California, United States",
        "bbox": [-124.48, 32.53, -114.13, 42.01],
        "latitude": 37.0,
        "longitude": -119.0,
        "source": "state-bbox-fallback",
        "fallback_reason": (
            "No precise match for 'Santa Rosa Island'; snapped to the full "
            "state of California. Refine the prompt for a smaller area."
        ),
    }


def _run(coro):
    return asyncio.run(coro)


def test_review_gate_stops_without_fetching_frames():
    """confirm=false returns the bbox + planned frame counts; no imagery fetched."""
    fetched_imagery = {"called": False}

    def _fake_peek(product, bbox, start, end):
        return 78 if product == "geocolor" else 12

    def _fake_goes(*a, **k):
        fetched_imagery["called"] = True
        return []

    with patch.dict(
        TOOL_REGISTRY,
        {
            "geocode_location": _reg(_fake_geocode_precise),
            "fetch_wfigs_incident": _reg(_fake_wfigs),
            "fetch_goes_animation": _reg(_fake_goes),
        },
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._peek_frame_count",
        _fake_peek,
    ):
        result = _run(
            model_satellite_fire_animation(
                "Iron",
                products=["geocolor"],
                state="UT",
                end_utc="2026-06-22T20:00:00Z",
            )
        )

    assert result["status"] == "review"
    # A tight WFIGS incident bbox wins over the geocode (additive) bbox.
    assert result["bbox"] == _INCIDENT["bbox"]
    assert result["aoi_source"] == "wfigs-incident"
    assert result["frame_counts"]["geocolor"] == 78
    assert result["start_utc"].endswith("Z")
    assert "presentation_text" in result
    # The review gate must NOT have fetched any imagery.
    assert fetched_imagery["called"] is False


def test_confirm_emits_postprocess_flood_frame_shape():
    """confirm=true returns frames in the postprocess_flood shape (distinct ids,
    shared preset, ISO-time name token, identical bbox)."""
    bbox = tuple(_INCIDENT["bbox"])

    def _frame(ts_iso):
        return LayerURI(
            layer_id=f"goes-anim-geocolor-{ts_iso}",
            name=f"GOES GeoColor {ts_iso} (GOES-18)",
            layer_type="raster",
            uri=f"s3://fake/{ts_iso}.tif",
            style_preset="goes_rgb_animation",
            role="context",
            units=None,
            bbox=bbox,
        )

    frames = [
        _frame("2026-06-22T13:30:00Z"),
        _frame("2026-06-22T13:35:00Z"),
        _frame("2026-06-22T13:40:00Z"),
    ]

    def _fake_goes(*a, **k):
        return frames

    def _fake_peek(product, b, s, e):
        return len(frames)

    with patch.dict(
        TOOL_REGISTRY,
        {
            "geocode_location": _reg(_fake_geocode_precise),
            "fetch_wfigs_incident": _reg(_fake_wfigs),
            "fetch_goes_animation": _reg(_fake_goes),
        },
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._peek_frame_count",
        _fake_peek,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._safe_overlay_firms",
        _async_none,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._safe_overlay_perimeters",
        _async_none,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._publish_layers",
        _async_empty_dict,
    ):
        result = _run(
            model_satellite_fire_animation(
                "Iron",
                products=["geocolor"],
                state="UT",
                start_utc="2026-06-22T13:30:00Z",
                end_utc="2026-06-22T20:00:00Z",
                confirm=True,
                overlay_firms=False,
                overlay_perimeters=False,
            )
        )

    assert result["status"] == "ok"
    assert result["n_frames"] == 3
    layers = result["layers"]
    # distinct layer_ids
    ids = [lyr["layer_id"] for lyr in layers]
    assert len(set(ids)) == len(ids)
    # shared style_preset
    assert {lyr["style_preset"] for lyr in layers} == {"goes_rgb_animation"}
    # ISO-time name token present in every frame name (each its real UTC stamp)
    assert all("2026-06-22T13:" in lyr["name"] for lyr in layers)
    assert any("13:30:00Z" in lyr["name"] for lyr in layers)
    assert any("13:40:00Z" in lyr["name"] for lyr in layers)
    # role context (frames, not the primary peak)
    assert all(lyr["role"] == "context" for lyr in layers)


def test_confirm_empty_run_is_not_ok_honesty_floor():
    """A confirmed run that produced NO imagery frames must NOT read status=ok."""

    def _fake_goes(*a, **k):
        from trid3nt_server.tools.fetch_goes_animation import GOESAnimEmptyError

        raise GOESAnimEmptyError("no frames")

    def _fake_peek(product, b, s, e):
        return 0

    with patch.dict(
        TOOL_REGISTRY,
        {
            "geocode_location": _reg(_fake_geocode_precise),
            "fetch_wfigs_incident": _reg(_fake_wfigs),
            "fetch_goes_animation": _reg(_fake_goes),
        },
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._peek_frame_count",
        _fake_peek,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._safe_overlay_firms",
        _async_none,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._safe_overlay_perimeters",
        _async_none,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._publish_layers",
        _async_empty_dict,
    ):
        result = _run(
            model_satellite_fire_animation(
                "Iron",
                products=["geocolor"],
                state="UT",
                start_utc="2026-06-22T13:30:00Z",
                end_utc="2026-06-22T20:00:00Z",
                confirm=True,
                overlay_firms=False,
                overlay_perimeters=False,
            )
        )

    assert result["status"] == "empty"
    assert result["n_frames"] == 0


def test_confirm_emits_each_published_frame_as_a_loaded_layer():
    """NATE 2026-06-26: the render-blocker fix, re-pinned on the NEW s3 publish
    shape (TiTiler exit / QGIS-native swap). Each frame whose publish returned a
    renderable uri -- now the raw s3:// COG the plugin reads via /vsicurl/ --
    must be EMITTED into session-state loaded_layers via add_loaded_layer. The
    emitted LayerURI carries the PUBLISHED s3:// uri; a frame whose publish
    failed (absent from the publish map) is honestly SKIPPED.
    """
    bbox = tuple(_INCIDENT["bbox"])

    def _frame(ts_iso):
        return LayerURI(
            layer_id=f"goes-anim-geocolor-{ts_iso}",
            name=f"GOES GeoColor {ts_iso} (GOES-18)",
            layer_type="raster",
            uri=f"s3://fake/{ts_iso}.tif",
            style_preset="goes_rgb_animation",
            role="context",
            units=None,
            bbox=bbox,
        )

    frames = [
        _frame("2026-06-22T13:30:00Z"),
        _frame("2026-06-22T13:35:00Z"),
        _frame("2026-06-22T13:40:00Z"),
    ]

    def _fake_goes(*a, **k):
        return frames

    def _fake_peek(product, b, s, e):
        return len(frames)

    # The first two frames publish (the NEW shape: publish_layer echoes the
    # raw s3:// COG uri, post overview-ensure); the third FAILS to publish
    # (absent from the map) -> it must be honestly skipped, never emitted.
    published_map = {
        frames[0].layer_id: f"s3://fake-pub/{frames[0].layer_id}.tif",
        frames[1].layer_id: f"s3://fake-pub/{frames[1].layer_id}.tif",
    }

    async def _fake_publish(layers, pipeline_emitter):
        # Mirror the real _publish_layers contract: {layer_id: published_url}.
        return dict(published_map)

    emitter = _RecordingEmitter()

    with patch.dict(
        TOOL_REGISTRY,
        {
            "geocode_location": _reg(_fake_geocode_precise),
            "fetch_wfigs_incident": _reg(_fake_wfigs),
            "fetch_goes_animation": _reg(_fake_goes),
        },
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._peek_frame_count",
        _fake_peek,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._safe_overlay_firms",
        _async_none,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._safe_overlay_perimeters",
        _async_none,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._publish_layers",
        _fake_publish,
    ):
        result = _run(
            model_satellite_fire_animation(
                "Iron",
                products=["geocolor"],
                state="UT",
                start_utc="2026-06-22T13:30:00Z",
                end_utc="2026-06-22T20:00:00Z",
                confirm=True,
                overlay_firms=False,
                overlay_perimeters=False,
                pipeline_emitter=emitter,  # type: ignore[arg-type]
            )
        )

    assert result["status"] == "ok"
    assert result["n_frames"] == 3
    # Only the TWO frames that PUBLISHED are emitted; the publish-fail frame is
    # honestly skipped (never emitted).
    assert len(emitter.loaded_layers) == 2
    emitted_ids = {lyr.layer_id for lyr in emitter.loaded_layers}
    assert emitted_ids == set(published_map)
    # Every emitted frame carries the PUBLISHED s3:// COG uri (the new shape).
    for lyr in emitter.loaded_layers:
        assert lyr.uri == published_map[lyr.layer_id]
        # The other identity fields are copied through unchanged.
        assert lyr.style_preset == "goes_rgb_animation"
        assert lyr.role == "context"
        assert lyr.bbox == bbox


# ---- GOES blended GeoColor + Fire Temperature animation (NATE 2026-06-22) ---
#
# A GOES fire run FOLDS the co-temporal pair into ONE scrubber: each frame is a
# BLEND of the GeoColor base (true-color + smoke) with the Fire Temperature
# active-fire glow composited in (the CIRA "GeoColor and Fire Temperature" look).
# The default (no products arg) is BOTH GOES products -> ONE blended group via
# fetch_goes_blend_animation. A single product can still be requested (un-blended).


def _blend_frame(step, ts_iso, bbox):
    """A blended GOES frame in the emitted shape: ONE shared layer_id prefix +
    single product-label name with a "step <N>" token + the real ISO valid-time."""
    return LayerURI(
        layer_id=f"goes-fire-blend-{ts_iso}",
        name=(
            f"GOES Fire (GeoColor + Fire Temperature) step {step} {ts_iso} (GOES-18)"
        ),
        layer_type="raster",
        uri=f"s3://fake/blend/{ts_iso}.tif",
        style_preset="goes_rgb_animation",
        role="context",
        units=None,
        bbox=bbox,
    )


# The shared valid-times the blended group animates over.
_GOES_TIMES = (
    "2026-06-22T18:00:00Z",
    "2026-06-22T18:05:00Z",
    "2026-06-22T18:10:00Z",
)


def test_default_goes_run_emits_one_blended_group():
    """A GOES run with NO products arg defaults to BOTH products and FOLDS them
    into ONE blended scrubber group (single layer_id prefix + single product-label
    stem + step tokens) via fetch_goes_blend_animation -- NOT two groups."""
    bbox = tuple(_INCIDENT["bbox"])
    dispatched = {"single_goes": False}

    def _fake_blend(b, satellite="goes-18", *a, **k):
        # The composer dispatches the blend fetcher ONCE for the default pair.
        return [_blend_frame(i, t, bbox) for i, t in enumerate(_GOES_TIMES, start=1)]

    def _fake_single_goes(*a, **k):
        # Must NOT be called when both products are requested (blend wins).
        dispatched["single_goes"] = True
        return []

    def _fake_peek(product, b, s, e):
        return len(_GOES_TIMES)

    with patch.dict(
        TOOL_REGISTRY,
        {
            "geocode_location": _reg(_fake_geocode_precise),
            "fetch_wfigs_incident": _reg(_fake_wfigs),
            "fetch_goes_blend_animation": _reg(_fake_blend),
            "fetch_goes_animation": _reg(_fake_single_goes),
        },
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._peek_frame_count",
        _fake_peek,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._safe_overlay_firms",
        _async_none,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._safe_overlay_perimeters",
        _async_none,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._publish_layers",
        _async_empty_dict,
    ):
        result = _run(
            model_satellite_fire_animation(
                "Iron",
                # NO products arg -> default to BOTH GOES products -> blended.
                state="UT",
                start_utc="2026-06-22T18:00:00Z",
                end_utc="2026-06-22T18:30:00Z",
                confirm=True,
                overlay_firms=False,
                overlay_perimeters=False,
            )
        )

    assert result["status"] == "ok"
    # Both GOES products are still REPORTED (they both fed the blend) ...
    assert set(result["products"]) == {"geocolor", "fire_temperature"}
    # ... but exactly ONE blended group of frames was emitted (not 2x).
    assert result["n_frames"] == len(_GOES_TIMES)
    # The single-product GOES fetcher was NOT used (the blend path won).
    assert dispatched["single_goes"] is False

    layers = result["layers"]
    # ONE group: every frame shares the single blended layer_id prefix + label.
    assert all(lyr["layer_id"].startswith("goes-fire-blend-") for lyr in layers)
    assert all(
        "Fire (GeoColor + Fire Temperature)" in lyr["name"] for lyr in layers
    )
    # No separate GeoColor-only / Fire-Temperature-only groups exist.
    assert not any(lyr["layer_id"].startswith("goes-anim-") for lyr in layers)
    # Each frame carries a "step <N>" monotonic token (the web grouping value)
    # and its real ISO valid-time as the display label.
    import re

    steptimes = {}
    for lyr in layers:
        step = int(re.search(r"step (\d+)", lyr["name"]).group(1))
        iso = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", lyr["name"]).group(1)
        steptimes[step] = iso
    assert set(steptimes) == {1, 2, 3}
    assert set(steptimes.values()) == set(_GOES_TIMES)
    # distinct ids across the group.
    ids = [lyr["layer_id"] for lyr in layers]
    assert len(set(ids)) == len(ids)
    # shared style preset (scrubber-group contract).
    assert {lyr["style_preset"] for lyr in layers} == {"goes_rgb_animation"}


def test_single_goes_product_can_still_be_requested():
    """A way to request just one GOES product is preserved (products=['geocolor'])
    -- a single GOES product dispatches the un-blended fetch_goes_animation."""
    bbox = tuple(_INCIDENT["bbox"])

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

    def _fake_peek(product, b, s, e):
        return 1

    with patch.dict(
        TOOL_REGISTRY,
        {
            "geocode_location": _reg(_fake_geocode_precise),
            "fetch_wfigs_incident": _reg(_fake_wfigs),
            "fetch_goes_animation": _reg(_fake_goes),
        },
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._peek_frame_count",
        _fake_peek,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._safe_overlay_firms",
        _async_none,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._safe_overlay_perimeters",
        _async_none,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._publish_layers",
        _async_empty_dict,
    ):
        result = _run(
            model_satellite_fire_animation(
                "Iron",
                products=["geocolor"],
                state="UT",
                start_utc="2026-06-22T18:00:00Z",
                end_utc="2026-06-22T18:30:00Z",
                confirm=True,
                overlay_firms=False,
                overlay_perimeters=False,
            )
        )

    assert result["status"] == "ok"
    assert result["products"] == ["geocolor"]
    assert result["n_frames"] == 1
    assert all("GeoColor" in lyr["name"] for lyr in result["layers"])
    assert not any("Fire Temperature" in lyr["name"] for lyr in result["layers"])


def test_viirs_run_is_single_polar_product_unchanged():
    """VIIRS/JPSS day_fire stays a SINGLE polar product (not dual-product) -- a
    day_fire run animates one group only."""
    bbox = tuple(_INCIDENT["bbox"])
    dispatched = {"products": []}

    def _fake_viirs(b, sat, product, *a, **k):
        dispatched["products"].append(product)
        return [
            LayerURI(
                layer_id=f"viirs-dayfire-{i}",
                name=f"VIIRS Day Fire step {i} 2026-05-1{i}T20:30:00Z (JPSS)",
                layer_type="raster",
                uri=f"s3://fake/viirs/{i}.tif",
                style_preset="viirs_day_fire_animation",
                role="context",
                units=None,
                bbox=bbox,
            )
            for i in (1, 2)
        ]

    def _fake_peek(product, b, s, e):
        return 2

    with patch.dict(
        TOOL_REGISTRY,
        {
            "geocode_location": _reg(_fake_geocode_precise),
            "fetch_wfigs_incident": _reg(_fake_wfigs),
            "fetch_viirs_day_fire": _reg(_fake_viirs),
        },
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._peek_frame_count",
        _fake_peek,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._safe_overlay_firms",
        _async_none,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._safe_overlay_perimeters",
        _async_none,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._publish_layers",
        _async_empty_dict,
    ):
        result = _run(
            model_satellite_fire_animation(
                "Santa Rosa Island",
                products=["day_fire"],
                start_utc="2026-05-15T00:00:00Z",
                end_utc="2026-05-19T00:00:00Z",
                confirm=True,
                overlay_firms=False,
                overlay_perimeters=False,
            )
        )

    assert result["status"] == "ok"
    # Single polar product -- NOT expanded to two.
    assert result["products"] == ["day_fire"]
    assert dispatched["products"] == ["day_fire"]
    assert all("VIIRS Day Fire" in lyr["name"] for lyr in result["layers"])


def test_default_goes_run_empty_is_not_ok_honesty_floor():
    """A confirmed default (blended) GOES run whose blend fetcher produced NO
    frames must NOT read status=ok -- the honesty floor holds for the blend path
    too."""

    def _fake_blend(*a, **k):
        from trid3nt_server.tools.fetch_goes_animation import GOESAnimEmptyError

        raise GOESAnimEmptyError("no frames")

    def _fake_peek(product, b, s, e):
        return 0

    with patch.dict(
        TOOL_REGISTRY,
        {
            "geocode_location": _reg(_fake_geocode_precise),
            "fetch_wfigs_incident": _reg(_fake_wfigs),
            "fetch_goes_blend_animation": _reg(_fake_blend),
        },
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._peek_frame_count",
        _fake_peek,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._safe_overlay_firms",
        _async_none,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._safe_overlay_perimeters",
        _async_none,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._publish_layers",
        _async_empty_dict,
    ):
        result = _run(
            model_satellite_fire_animation(
                "Iron",
                # default dual-product GOES run
                state="UT",
                start_utc="2026-06-22T18:00:00Z",
                end_utc="2026-06-22T18:30:00Z",
                confirm=True,
                overlay_firms=False,
                overlay_perimeters=False,
            )
        )

    assert result["status"] == "empty"
    assert result["n_frames"] == 0
    # Both products were attempted and both came back empty.
    assert set(result["products"]) == {"geocolor", "fire_temperature"}


# ---- FIX A: coarse-geocode detection --------------------------------------


def test_geocode_is_coarse_detects_state_snap():
    from trid3nt_server.workflows.model_satellite_fire_animation import (
        _geocode_is_coarse,
    )

    # A coarse state-snap (the Santa Rosa Island failure mode) is coarse.
    assert _geocode_is_coarse(_fake_geocode_coarse("x")) is True
    # source-only signal.
    assert _geocode_is_coarse({"source": "state-bbox-fallback"}) is True
    # fallback_reason-only signal.
    assert _geocode_is_coarse({"fallback_reason": "snapped to ..."}) is True
    # None counts as coarse (no geocode -> fall through to data).
    assert _geocode_is_coarse(None) is True
    # A precise Nominatim match is NOT coarse.
    assert _geocode_is_coarse(_fake_geocode_precise("x")) is False


# ---- FIX A: densest-hotspot clustering ------------------------------------


def test_densest_hotspot_bbox_empty_is_none():
    from trid3nt_server.workflows.model_satellite_fire_animation import (
        _densest_hotspot_bbox,
    )

    assert _densest_hotspot_bbox([]) is None


def test_densest_hotspot_bbox_tight_around_densest_cluster():
    """The bbox snaps to the dense cluster, NOT to a far-flung outlier."""
    from trid3nt_server.workflows.model_satellite_fire_animation import (
        _densest_hotspot_bbox,
    )

    # A dense cluster around the Channel Islands (lon ~-120.10, lat ~33.96) ...
    cluster = [
        (-120.11, 33.95),
        (-120.10, 33.96),
        (-120.09, 33.97),
        (-120.10, 33.95),
        (-120.11, 33.96),
    ]
    # ... plus a single far outlier 4 deg away that must NOT widen the AOI.
    points = cluster + [(-116.0, 33.0)]
    bbox = _densest_hotspot_bbox(points, pad_deg=0.1, cell_deg=0.1)
    assert bbox is not None
    min_lon, min_lat, max_lon, max_lat = bbox
    # The cluster is inside.
    assert min_lon <= -120.11 and max_lon >= -120.09
    assert min_lat <= 33.95 and max_lat >= 33.97
    # The outlier at lon -116 is EXCLUDED (AOI stays tight, < ~1 deg wide).
    assert max_lon < -119.0
    assert (max_lon - min_lon) < 1.0
    assert (max_lat - min_lat) < 1.0


# ---- FIX A: FIRMS-localization branch (the Santa Rosa Island fix) ----------


class _RecordingEmitter:
    """Minimal PipelineEmitter stand-in that records map-command emissions.

    NATE 2026-06-26: also records add_loaded_layer so we can lock in that each
    published animation frame is emitted into session-state loaded_layers (the
    step the composer omitted -- frames published to TiTiler but never rendered).
    """

    def __init__(self):
        self.map_commands: list[tuple[str, dict]] = []
        self.loaded_layers: list[LayerURI] = []

    async def add_step(self, name, tool_name):
        return "step-1"

    async def mark_running(self, step_id):
        return None

    async def mark_complete(self, step_id):
        return None

    async def mark_failed(self, step_id, code, msg):
        return None

    async def emit_map_command(self, command, args):
        self.map_commands.append((command, args))

    async def add_loaded_layer(self, layer):
        self.loaded_layers.append(layer)


def test_coarse_geocode_localizes_from_firms_and_emits_aoi_pre_gate():
    """The Santa Rosa Island fix: a COARSE state-snap geocode + NO WFIGS incident
    -> the AOI is derived from FIRMS hot pixels, emitted (snap-to-AOI) BEFORE the
    review gate."""
    from trid3nt_server.tools.fetch_firms_active_fire import FirmsArgError  # noqa: F401

    # The fire's real FIRMS hot pixels (Channel Islands cluster).
    firms_points = [
        (-120.11, 33.95),
        (-120.10, 33.96),
        (-120.09, 33.97),
        (-120.10, 33.95),
    ]

    fetched_imagery = {"called": False}

    def _no_wfigs(name, state=None, *a, **k):
        # WFIGS does not carry this contained fire -> honest typed not-found.
        from trid3nt_server.tools.fetch_wfigs_incident import (
            WFIGSIncidentNotFoundError,
        )

        raise WFIGSIncidentNotFoundError("no match")

    def _fake_firms(bbox, days_back=1, source="VIIRS_NOAA20_NRT", date=None, *a, **k):
        # Return a sentinel "layer"; _read_firms_points is patched to read it.
        return ("FIRMS_LAYER", bbox, date)

    def _fake_read_points(layer):
        return list(firms_points)

    def _fake_peek(product, bbox, start, end):
        return 5

    def _fake_viirs(*a, **k):
        fetched_imagery["called"] = True
        return []

    emitter = _RecordingEmitter()

    with patch.dict(
        TOOL_REGISTRY,
        {
            "geocode_location": _reg(_fake_geocode_coarse),
            "fetch_wfigs_incident": _reg(_no_wfigs),
            "fetch_firms_active_fire": _reg(_fake_firms),
            "fetch_viirs_day_fire": _reg(_fake_viirs),
        },
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._peek_frame_count",
        _fake_peek,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._read_firms_points",
        _fake_read_points,
    ):
        result = _run(
            model_satellite_fire_animation(
                "Santa Rosa Island",
                products=["day_fire"],
                start_utc="2026-05-15T00:00:00Z",
                end_utc="2026-05-19T00:00:00Z",
                pipeline_emitter=emitter,  # type: ignore[arg-type]
            )
        )

    # Stopped at the review gate, AOI derived from the data (not a state snap).
    assert result["status"] == "review"
    assert result["aoi_source"] == "firms-hotspots"
    min_lon, min_lat, max_lon, max_lat = result["bbox"]
    # The AOI is TIGHT around the Channel Islands cluster, NOT the full state of
    # California (the coarse geocode bbox spanned ~10 deg of longitude).
    assert -120.5 < min_lon < -119.5
    assert -120.5 < max_lon < -119.5
    assert 33.5 < min_lat < 34.5
    assert (max_lon - min_lon) < 1.0
    assert (max_lat - min_lat) < 1.0
    # No imagery fetched at the review gate.
    assert fetched_imagery["called"] is False
    # The AOI snap-to was emitted EARLY (a zoom-to map-command fired before the
    # gate returned) and points at the tight derived bbox.
    zooms = [a for (c, a) in emitter.map_commands if c == "zoom-to"]
    assert zooms, "expected an early zoom-to map-command (snap-to-AOI)"
    assert zooms[0]["bbox"] == list(result["bbox"])


def test_wfigs_no_match_does_not_gate_falls_back_to_firms():
    """A WFIGS no-match must NOT stop the run -- it degrades to FIRMS localization
    (with even a coarse geocode), proving WFIGS is additive context, not a gate."""

    def _no_wfigs(name, state=None, *a, **k):
        from trid3nt_server.tools.fetch_wfigs_incident import (
            WFIGSIncidentNotFoundError,
        )

        raise WFIGSIncidentNotFoundError("no match")

    def _fake_firms(bbox, days_back=1, source="VIIRS_NOAA20_NRT", date=None, *a, **k):
        return ("FIRMS_LAYER", bbox, date)

    def _fake_read_points(layer):
        return [(-120.10, 33.96), (-120.11, 33.95), (-120.09, 33.97)]

    def _fake_peek(product, bbox, start, end):
        return 3

    with patch.dict(
        TOOL_REGISTRY,
        {
            "geocode_location": _reg(_fake_geocode_coarse),
            "fetch_wfigs_incident": _reg(_no_wfigs),
            "fetch_firms_active_fire": _reg(_fake_firms),
        },
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._peek_frame_count",
        _fake_peek,
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._read_firms_points",
        _fake_read_points,
    ):
        result = _run(
            model_satellite_fire_animation(
                "Santa Rosa Island",
                products=["day_fire"],
                start_utc="2026-05-15T00:00:00Z",
                end_utc="2026-05-17T00:00:00Z",
            )
        )

    # The run did NOT raise -- it produced a review with a data-derived AOI.
    assert result["status"] == "review"
    assert result["aoi_source"] == "firms-hotspots"
    # incident is None (no authoritative record) but the run still localized.
    assert result["incident"] is None


def test_precise_geocode_is_used_without_firms_localization():
    """A PRECISE geocode (no state-snap) + no WFIGS incident uses the geocode
    bbox directly and does NOT invoke FIRMS localization."""
    firms_called = {"called": False}

    def _no_wfigs(name, state=None, *a, **k):
        from trid3nt_server.tools.fetch_wfigs_incident import (
            WFIGSIncidentNotFoundError,
        )

        raise WFIGSIncidentNotFoundError("no match")

    def _fake_firms(*a, **k):
        firms_called["called"] = True
        return ("FIRMS_LAYER",)

    def _fake_peek(product, bbox, start, end):
        return 7

    with patch.dict(
        TOOL_REGISTRY,
        {
            "geocode_location": _reg(_fake_geocode_precise),
            "fetch_wfigs_incident": _reg(_no_wfigs),
            "fetch_firms_active_fire": _reg(_fake_firms),
        },
    ), patch(
        "trid3nt_server.workflows.model_satellite_fire_animation._peek_frame_count",
        _fake_peek,
    ):
        result = _run(
            model_satellite_fire_animation(
                "Eureka, Utah",
                products=["geocolor"],
                end_utc="2026-06-22T20:00:00Z",
            )
        )

    assert result["status"] == "review"
    assert result["aoi_source"] == "geocode"
    assert result["bbox"] == [-112.30, 39.90, -112.05, 40.05]
    # The precise geocode short-circuits the data-localization path.
    assert firms_called["called"] is False


# ---- helpers --------------------------------------------------------------


def _reg(fn):
    """Wrap a plain callable as a RegisteredTool for patch.dict(TOOL_REGISTRY)."""
    from trid3nt_server.tools import RegisteredTool
    from trid3nt_contracts.tool_registry import AtomicToolMetadata

    return RegisteredTool(
        metadata=AtomicToolMetadata(
            name="_fake", ttl_class="live-no-cache", source_class="workflow_dispatch", cacheable=False
        ),
        fn=fn,
        module="test",
    )


async def _async_none(*a, **k):
    return None


async def _async_empty_dict(*a, **k):
    return {}
