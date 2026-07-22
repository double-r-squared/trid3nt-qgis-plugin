"""Neutral-line round-trip: a USER-DRAWN elevation/section LineString through
``request_spatial_input`` (purpose="line") -> server parse -> the LLM result
that feeds ``compute_terrain_profile(line=...)``.

This is the AGENT-side half of FIX 2: the request_spatial_input vector_draw
surface used to FORCE every drawn LineString to ``role=="barrier"`` and reject
anything missing a wall/flap_gate tag, so a neutral elevation line could never
round-trip. The neutral-line path is ADDITIVE — a drawn ``role=="line"``
LineString parses to plain coordinates and is surfaced as the result's
``line`` / ``linestring`` fields, while the existing barrier flow stays exactly
as it was (covered by test_spatial_input_barriers.py — those tests still pass).

Coverage:
1. PURE PARSE: a ``role=="line"`` LineString -> ``ParsedSpatialInput.line_coords``
   (+ ``n_lines``), with NO barrier_type required; malformed lines raise typed
   ``SpatialInputParseError`` (honesty floor).
2. RESPONSE -> RESULT: ``_spatial_response_to_result`` on a vector_draw reply
   carrying a neutral line adds ``line`` + ``linestring`` to the result; the
   barrier flow result is unchanged (no ``line`` keys when no line is drawn).
3. CONSUMPTION: the surfaced ``line`` / ``linestring`` resolve via
   ``compute_terrain_profile._resolve_line_coords`` (the tool consumes it).
4. TOOL: ``request_spatial_input(purpose="line")`` rides ``purpose`` back in the
   sentinel; an invalid purpose -> a typed param error.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from grace2_agent.server import _spatial_response_to_result
from grace2_agent.spatial_input import (
    SpatialInputParseError,
    parse_spatial_input_features,
    split_features_by_role,
)
from grace2_contracts.common import new_ulid
from grace2_contracts.ws import SpatialInputResponsePayload


# --------------------------------------------------------------------------- #
# Fixtures.
# --------------------------------------------------------------------------- #


def _line_feature() -> dict[str, Any]:
    """A drawn NEUTRAL elevation/section line (role=='line', NO barrier_type)."""
    return {
        "type": "Feature",
        "properties": {"role": "line"},
        "geometry": {
            "type": "LineString",
            "coordinates": [[-85.31, 35.04], [-85.30, 35.05], [-85.29, 35.06]],
        },
    }


def _line_fc() -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": [_line_feature()]}


# --------------------------------------------------------------------------- #
# 1. PURE PARSE — role=="line" -> plain coords, no barrier tagging.
# --------------------------------------------------------------------------- #


def test_split_features_by_role_buckets_line_role():
    buckets = split_features_by_role(_line_fc())
    assert len(buckets["line"]) == 1
    # the line is NOT bucketed as a barrier (would have demanded a tag).
    assert len(buckets["barrier"]) == 0


def test_parse_neutral_line_produces_line_coords_no_barriers():
    parsed = parse_spatial_input_features(_line_fc())
    assert parsed.line_coords == [[-85.31, 35.04], [-85.30, 35.05], [-85.29, 35.06]]
    assert parsed.n_lines == 1
    # a neutral line is NEVER a barrier — barriers stays None (a plain run).
    assert parsed.barriers is None
    assert parsed.n_walls == 0 and parsed.n_flap_gates == 0


def test_parse_line_needs_no_barrier_type():
    """The crux of the fix: a line with NO barrier_type parses cleanly (the old
    barrier-only path would have raised SPATIAL_INPUT_BAD_BARRIER_TYPE)."""
    feat = _line_feature()
    assert "barrier_type" not in feat["properties"]
    parsed = parse_spatial_input_features(
        {"type": "FeatureCollection", "features": [feat]}
    )
    assert parsed.line_coords is not None


def test_parse_line_too_short_raises_typed():
    feat = _line_feature()
    feat["geometry"]["coordinates"] = [[-85.31, 35.04]]  # only 1 position
    with pytest.raises(SpatialInputParseError) as ei:
        parse_spatial_input_features(
            {"type": "FeatureCollection", "features": [feat]}
        )
    assert ei.value.error_code == "SPATIAL_INPUT_LINE_TOO_SHORT"


def test_parse_line_not_linestring_raises_typed():
    feat = _line_feature()
    feat["geometry"] = {"type": "Point", "coordinates": [-85.3, 35.05]}
    with pytest.raises(SpatialInputParseError) as ei:
        parse_spatial_input_features(
            {"type": "FeatureCollection", "features": [feat]}
        )
    assert ei.value.error_code == "SPATIAL_INPUT_LINE_NOT_LINESTRING"


def test_line_and_aoi_coexist():
    """A neutral line can ride alongside an AOI (no role collision)."""
    aoi = {
        "type": "Feature",
        "properties": {"role": "aoi"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[-85.32, 35.03], [-85.28, 35.03], [-85.28, 35.07], [-85.32, 35.07], [-85.32, 35.03]]
            ],
        },
    }
    parsed = parse_spatial_input_features(
        {"type": "FeatureCollection", "features": [aoi, _line_feature()]}
    )
    assert parsed.aoi_bbox is not None
    assert parsed.line_coords is not None
    assert parsed.n_lines == 1


# --------------------------------------------------------------------------- #
# 2. spatial-input-response -> result surfaces the line geometry.
# --------------------------------------------------------------------------- #


def test_response_neutral_line_carries_line_and_linestring():
    resp = SpatialInputResponsePayload(
        request_id=new_ulid(),
        geometry_type="vector_draw",
        features=_line_fc(),
    )
    result = _spatial_response_to_result(resp)
    assert result["status"] == "ok"
    assert result["geometry_type"] == "vector_draw"
    assert result["n_lines"] == 1
    # the bare vertex list...
    assert result["line"] == [[-85.31, 35.04], [-85.30, 35.05], [-85.29, 35.06]]
    # ...and the GeoJSON LineString, both present.
    assert result["linestring"]["type"] == "LineString"
    assert result["linestring"]["coordinates"] == result["line"]
    # a neutral-line draw is NOT a barrier run.
    assert "barriers" not in result


def test_response_barrier_flow_has_no_line_keys():
    """NO-REGRESSION: a pure barrier vector_draw response carries NO line keys
    (the neutral-line path is strictly additive)."""
    barrier_fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"role": "barrier", "barrier_type": "wall"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-85.305, 35.045], [-85.305, 35.055]],
                },
            }
        ],
    }
    resp = SpatialInputResponsePayload(
        request_id=new_ulid(), geometry_type="vector_draw", features=barrier_fc
    )
    result = _spatial_response_to_result(resp)
    assert result["status"] == "ok"
    assert result["n_walls"] == 1
    assert result["n_lines"] == 0
    assert "line" not in result and "linestring" not in result
    assert "barriers" in result  # the barrier flow is unchanged


# --------------------------------------------------------------------------- #
# 3. The surfaced line geometry resolves in compute_terrain_profile.
# --------------------------------------------------------------------------- #


def test_surfaced_line_feeds_compute_terrain_profile():
    from grace2_agent.tools.compute_terrain_profile import _resolve_line_coords

    resp = SpatialInputResponsePayload(
        request_id=new_ulid(),
        geometry_type="vector_draw",
        features=_line_fc(),
    )
    result = _spatial_response_to_result(resp)
    # both the bare list and the GeoJSON LineString resolve in the tool.
    assert _resolve_line_coords(result["line"]) == [
        [-85.31, 35.04],
        [-85.30, 35.05],
        [-85.29, 35.06],
    ]
    assert _resolve_line_coords(result["linestring"]) == [
        [-85.31, 35.04],
        [-85.30, 35.05],
        [-85.29, 35.06],
    ]


# --------------------------------------------------------------------------- #
# 4. request_spatial_input tool carries `purpose` through the sentinel.
# --------------------------------------------------------------------------- #


def test_tool_rides_purpose_line_in_sentinel():
    from grace2_agent.tools.spatial_input_tool import (
        SPATIAL_INPUT_SENTINEL_KEY,
        request_spatial_input,
    )

    out = asyncio.run(
        request_spatial_input(
            mode="vector_draw",
            purpose="line",
            title="Draw the profile line",
            description="Draw a line for the elevation profile.",
        )
    )
    assert out.get(SPATIAL_INPUT_SENTINEL_KEY) is True
    assert out["mode"] == "vector_draw"
    assert out["purpose"] == "line"


def test_tool_default_purpose_is_barrier():
    """NO-REGRESSION: omitting `purpose` defaults to "barrier" (the SWMM flow)."""
    from grace2_agent.tools.spatial_input_tool import request_spatial_input

    out = asyncio.run(
        request_spatial_input(mode="vector_draw", title="Draw", description="x")
    )
    assert out["purpose"] == "barrier"


def test_tool_rejects_bad_purpose():
    from grace2_agent.tools.spatial_input_tool import (
        SPATIAL_INPUT_SENTINEL_KEY,
        request_spatial_input,
    )

    out = asyncio.run(
        request_spatial_input(mode="vector_draw", purpose="freehand")
    )
    assert out["status"] == "error"
    assert out["error_code"] == "SPATIAL_INPUT_PARAMS_INVALID"
    assert SPATIAL_INPUT_SENTINEL_KEY not in out


def test_tool_rides_purpose_aoi_in_sentinel():
    """purpose='aoi' is accepted and the emitted sentinel carries purpose='aoi'.

    This is the fix for the live bug (session 01KX41AFRWWCGTKBYPVYYWHT70) where
    the model correctly called request_spatial_input(purpose='aoi') to ask the
    user to draw an area of interest but got SPATIAL_INPUT_PARAMS_INVALID
    (aoi was not in _VALID_PURPOSES).  The model then retried with purpose='barrier'
    which is semantically wrong for an AOI selection request.
    """
    from grace2_agent.tools.spatial_input_tool import (
        SPATIAL_INPUT_SENTINEL_KEY,
        request_spatial_input,
    )

    out = asyncio.run(
        request_spatial_input(
            mode="vector_draw",
            purpose="aoi",
            title="Draw the study area",
            description="Draw a rectangle or polygon over the region to analyse.",
        )
    )
    # Must succeed (sentinel, not an error dict).
    assert out.get(SPATIAL_INPUT_SENTINEL_KEY) is True, (
        f"expected sentinel, got: {out}"
    )
    assert out["mode"] == "vector_draw"
    assert out["purpose"] == "aoi"
    # Must NOT return an error.
    assert "status" not in out or out.get("status") != "error"


def test_tool_aoi_purpose_does_not_emit_barrier_sentinel():
    """purpose='aoi' sentinel must differ from purpose='barrier'.

    Regression guard: a model using purpose='aoi' must not accidentally produce
    the same wire payload as the SWMM barrier flow.
    """
    from grace2_agent.tools.spatial_input_tool import (
        SPATIAL_INPUT_SENTINEL_KEY,
        request_spatial_input,
    )

    aoi_out = asyncio.run(
        request_spatial_input(mode="vector_draw", purpose="aoi", title="t", description="d")
    )
    barrier_out = asyncio.run(
        request_spatial_input(mode="vector_draw", purpose="barrier", title="t", description="d")
    )
    # Both produce a valid sentinel.
    assert aoi_out.get(SPATIAL_INPUT_SENTINEL_KEY) is True
    assert barrier_out.get(SPATIAL_INPUT_SENTINEL_KEY) is True
    # But the purpose field differs so the server routes them to different UI paths.
    assert aoi_out["purpose"] == "aoi"
    assert barrier_out["purpose"] == "barrier"
