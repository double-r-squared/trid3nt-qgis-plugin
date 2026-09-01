"""FR-AS-10 / FR-WC-16: the AGENT consuming a drawn FeatureCollection.

Proves the full agent-side wire for the spatial-input gate:

1. PURE PARSE (``trid3nt_server.gates.spatial_input``): a role-tagged drawn
   ``FeatureCollection`` (aoi polygon + points) splits into the AOI bbox and the
   point list, and EVERY malformed shape degrades to a TYPED
   ``SpatialInputParseError`` (never a silent success / fabricated geometry --
   the honesty floor).

2. RESPONSE -> RESULT (``_spatial_response_to_result``): a
   ``spatial-input-response`` (vector_draw / point / bbox / cancel / timeout /
   malformed) maps to the typed result the LLM reads.

3. PAUSE/RESUME REGISTRY + INBOUND RESOLVE (``server`` spatial-input gate): the
   ``_PENDING_SPATIAL_INPUTS`` registry + ``_resolve_pending_spatial_input``
   mirror the region-choice gate (cross-session refusal, unknown-id no-op), and
   ``_emit_spatial_input_and_wait`` round-trips a drawn reply.

4. TOOL SENTINEL (``tools/spatial_input_tool``): ``request_spatial_input``
   returns the sentinel the turn loop intercepts, and rejects an unknown mode
   with a typed error.
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import numpy as np
import pytest

from trid3nt_server import server
from trid3nt_server.gates.cards.spatial_input import (
    _spatial_response_to_result,
)
from trid3nt_server.server import (
    SessionState,
    _emit_spatial_input_and_wait,
    _resolve_pending_spatial_input,
)
from trid3nt_server.gates.spatial_input import (
    ParsedSpatialInput,
    SpatialInputParseError,
    parse_spatial_input_features,
    split_features_by_role,
)
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.ws import (
    AGENT_TO_CLIENT_PAYLOADS,
    CLIENT_TO_AGENT_PAYLOADS,
    SpatialInputRequestPayload,
    SpatialInputResponsePayload,
)


@pytest.fixture(autouse=True)
def _cap_gate_waits(monkeypatch):
    """LANE C: cap every user-decision gate wait so a headless run never hangs
    on the F6 24h local-lane lift (``_gate_wait_timeout``). Production leaves
    ``TRID3NT_GATE_WAIT_CAP_S`` unset -> byte-identical behavior. Happy-path
    resolvers answer within milliseconds; the emit/await timeout test tightens
    the cap so it hits the honest None-return path fast."""
    monkeypatch.setenv("TRID3NT_GATE_WAIT_CAP_S", "5")


# =========================================================================== #
# Geometry fixtures.
# =========================================================================== #


def _aoi_feature() -> dict[str, Any]:
    """A drawn rectangle AOI (role=='aoi')."""
    return {
        "type": "Feature",
        "properties": {"role": "aoi"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [-85.31, 35.04],
                    [-85.29, 35.04],
                    [-85.29, 35.06],
                    [-85.31, 35.06],
                    [-85.31, 35.04],
                ]
            ],
        },
    }


def _line_feature() -> dict[str, Any]:
    """A drawn NEUTRAL section line (role=='line')."""
    return {
        "type": "Feature",
        "properties": {"role": "line"},
        "geometry": {
            "type": "LineString",
            "coordinates": [[-85.305, 35.045], [-85.305, 35.055]],
        },
    }


def _point_feature() -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"role": "point"},
        "geometry": {"type": "Point", "coordinates": [-85.300, 35.050]},
    }


def _full_drawn_fc() -> dict[str, Any]:
    """A complete drawn FeatureCollection: AOI + section line + point."""
    return {
        "type": "FeatureCollection",
        "features": [_aoi_feature(), _line_feature(), _point_feature()],
    }


# =========================================================================== #
# 1. PURE PARSE - the role split.
# =========================================================================== #


def test_split_features_by_role_buckets_all_roles():
    buckets = split_features_by_role(_full_drawn_fc())
    assert len(buckets["aoi"]) == 1
    assert len(buckets["line"]) == 1
    assert len(buckets["point"]) == 1


def test_parse_full_drawn_fc_produces_engine_inputs():
    parsed = parse_spatial_input_features(_full_drawn_fc())
    assert isinstance(parsed, ParsedSpatialInput)
    # the neutral section line rides through as bare vertices.
    assert parsed.n_lines == 1
    assert parsed.line_coords == [[-85.305, 35.045], [-85.305, 35.055]]
    # AOI bbox derived from the polygon ring.
    assert parsed.aoi_bbox is not None
    assert math.isclose(parsed.aoi_bbox[0], -85.31)
    assert math.isclose(parsed.aoi_bbox[1], 35.04)
    assert math.isclose(parsed.aoi_bbox[2], -85.29)
    assert math.isclose(parsed.aoi_bbox[3], 35.06)
    # one drawn point.
    assert parsed.points == [[-85.300, 35.050]]


# --- malformed -> TYPED error (honesty floor; never a silent success) ------- #


def test_not_a_feature_collection_raises():
    with pytest.raises(SpatialInputParseError) as ei:
        parse_spatial_input_features({"type": "Polygon", "coordinates": []})
    assert ei.value.error_code == "SPATIAL_INPUT_NOT_FEATURECOLLECTION"


def test_features_not_a_list_raises():
    with pytest.raises(SpatialInputParseError) as ei:
        parse_spatial_input_features({"type": "FeatureCollection", "features": {}})
    assert ei.value.error_code == "SPATIAL_INPUT_NO_FEATURES"


def test_unknown_role_raises():
    feat = _line_feature()
    feat["properties"]["role"] = "river"  # not a canonical role
    with pytest.raises(SpatialInputParseError) as ei:
        parse_spatial_input_features(
            {"type": "FeatureCollection", "features": [feat]}
        )
    assert ei.value.error_code == "SPATIAL_INPUT_BAD_ROLE"


def test_point_wrong_geometry_raises():
    feat = _point_feature()
    feat["geometry"] = {"type": "LineString", "coordinates": [[1, 2], [3, 4]]}
    with pytest.raises(SpatialInputParseError) as ei:
        parse_spatial_input_features(
            {"type": "FeatureCollection", "features": [feat]}
        )
    assert ei.value.error_code == "SPATIAL_INPUT_POINT_NOT_POINT"


# =========================================================================== #
# 2. spatial-input-response -> the LLM-facing result.
# =========================================================================== #


def test_response_vector_draw_carries_the_drawn_roles():
    resp = SpatialInputResponsePayload(
        request_id=new_ulid(),
        geometry_type="vector_draw",
        features=_full_drawn_fc(),
    )
    result = _spatial_response_to_result(resp)
    assert result["status"] == "ok"
    assert result["geometry_type"] == "vector_draw"
    assert result["n_aoi"] == 1
    assert result["n_lines"] == 1
    assert result["points"] == [[-85.300, 35.050]]
    assert "aoi_bbox" in result and len(result["aoi_bbox"]) == 4
    assert result["line"] == [[-85.305, 35.045], [-85.305, 35.055]]


def test_response_point_and_bbox():
    pt = SpatialInputResponsePayload(
        request_id=new_ulid(), geometry_type="point", coordinates=[-85.3, 35.05]
    )
    r = _spatial_response_to_result(pt)
    assert r["status"] == "ok" and r["geometry_type"] == "point"
    assert r["coordinates"] == [-85.3, 35.05]

    bb = SpatialInputResponsePayload(
        request_id=new_ulid(),
        geometry_type="bbox",
        coordinates=[-85.31, 35.04, -85.29, 35.06],
    )
    rb = _spatial_response_to_result(bb)
    assert rb["status"] == "ok" and rb["geometry_type"] == "bbox"


def test_response_cancelled_is_not_a_success():
    resp = SpatialInputResponsePayload(request_id=new_ulid(), cancelled=True)
    r = _spatial_response_to_result(resp)
    assert r["status"] == "cancelled"
    assert "aoi_bbox" not in r and "points" not in r


def test_response_timeout_is_typed_error():
    """No reply (None) -> a typed timeout error, never a fabricated success."""
    r = _spatial_response_to_result(None)
    assert r["status"] == "error"
    assert r["error_code"] == "SPATIAL_INPUT_TIMEOUT"


def test_response_malformed_features_rejected_at_contract_boundary():
    """The contract validator REJECTS an unknown role at construction - the
    first line of the honesty floor (a malformed draw never even reaches the
    result mapper; the inbound handler returns TOOL_PARAMS_INVALID)."""
    bad = _full_drawn_fc()
    bad["features"][1]["properties"]["role"] = "levee"  # not a canonical role
    with pytest.raises(Exception) as ei:  # pydantic ValidationError
        SpatialInputResponsePayload(
            request_id=new_ulid(),
            geometry_type="vector_draw",
            features=bad,
        )
    assert "role" in str(ei.value)


def test_response_malformed_features_is_typed_error_second_layer():
    """SECOND layer of the honesty floor: if a malformed FeatureCollection ever
    reaches the result mapper (e.g. contract validation bypassed), it degrades to
    a TYPED error, never a silent success / fabricated geometry."""
    bad = _full_drawn_fc()
    bad["features"][1]["properties"]["role"] = "levee"  # not a canonical role
    # model_construct bypasses validation -> simulate a malformed FC arriving.
    resp = SpatialInputResponsePayload.model_construct(
        request_id=new_ulid(),
        geometry_type="vector_draw",
        coordinates=None,
        features=bad,
        cancelled=False,
    )
    r = _spatial_response_to_result(resp)
    assert r["status"] == "error"
    assert r["error_code"] == "SPATIAL_INPUT_BAD_ROLE"
    assert "aoi_bbox" not in r


# =========================================================================== #
# 3. Pending-future registry + inbound resolve + emit/await round-trip.
# =========================================================================== #


def test_registration_in_ws_routing_registries():
    assert "spatial-input-request" in {
        p.MESSAGE_TYPE for p in AGENT_TO_CLIENT_PAYLOADS.values()
    } or "spatial-input-request" in AGENT_TO_CLIENT_PAYLOADS
    assert "spatial-input-response" in CLIENT_TO_AGENT_PAYLOADS


def test_resolve_unknown_request_id_is_noop():
    state = SessionState(session_id=new_ulid())
    resp = SpatialInputResponsePayload(request_id=new_ulid(), cancelled=True)
    assert _resolve_pending_spatial_input(state.session_id, resp) is False


def test_resolve_cross_session_refused():
    """A response from a non-owner session is refused (mirrors region-choice)."""
    async def _run() -> tuple[bool, bool]:
        owner = new_ulid()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        req_id = new_ulid()
        server._register_pending_spatial_input(owner, req_id, fut)
        try:
            resp = SpatialInputResponsePayload(request_id=req_id, cancelled=True)
            refused = _resolve_pending_spatial_input("some-other-session", resp)
            accepted = _resolve_pending_spatial_input(owner, resp)
            return refused, accepted
        finally:
            server._pop_pending_spatial_input(req_id)

    refused, accepted = asyncio.run(_run())
    assert refused is False, "cross-session response must be refused"
    assert accepted is True, "owner-session response must resolve the future"


class _MockWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        self.sent.append(json.loads(raw) if isinstance(raw, str) else raw)


def test_emit_and_wait_round_trips_a_drawn_reply():
    """_emit_spatial_input_and_wait emits the request, then resolves on the
    matching spatial-input-response (mirrors the region-choice emit/await)."""
    async def _run() -> SpatialInputResponsePayload | None:
        ws = _MockWebSocket()
        state = SessionState(session_id=new_ulid())
        payload = SpatialInputRequestPayload(
            request_id=new_ulid(),
            mode="vector_draw",
            title="Draw the flood walls",
            description="Outline the AOI and place walls / flap gates.",
            default_timeout_seconds=5,
        )
        handler = asyncio.create_task(
            _emit_spatial_input_and_wait(ws, state, payload)
        )
        # Wait for the request emission + the pending future registration.
        for _ in range(200):
            await asyncio.sleep(0)
            if any(
                e["type"] == "spatial-input-request" for e in ws.sent
            ) and server._PENDING_SPATIAL_INPUTS:
                break
        reqs = [e for e in ws.sent if e["type"] == "spatial-input-request"]
        assert reqs, "a spatial-input-request must be emitted"
        request_id = reqs[0]["payload"]["request_id"]
        assert reqs[0]["payload"]["mode"] == "vector_draw"
        reply = SpatialInputResponsePayload(
            request_id=request_id,
            geometry_type="vector_draw",
            features=_full_drawn_fc(),
        )
        assert _resolve_pending_spatial_input(state.session_id, reply)
        return await handler

    resp = asyncio.run(_run())
    assert resp is not None and resp.geometry_type == "vector_draw"
    # the round-tripped reply parses into the drawn roles.
    result = _spatial_response_to_result(resp)
    assert result["status"] == "ok"
    assert result["n_aoi"] == 1 and result["n_lines"] == 1


def test_emit_and_wait_timeout_returns_none(monkeypatch):
    """No reply within the window -> None (caller surfaces a typed timeout)."""
    # The F6 local-lane gate ignores the payload timeout and would wait 24h; the
    # LANE C cap forces the honest timeout quickly (tight override of the autouse
    # 5s net) so this asserts the real None-return path without hanging.
    monkeypatch.setenv("TRID3NT_GATE_WAIT_CAP_S", "0.05")

    async def _run() -> SpatialInputResponsePayload | None:
        ws = _MockWebSocket()
        state = SessionState(session_id=new_ulid())
        payload = SpatialInputRequestPayload(
            request_id=new_ulid(),
            mode="bbox",
            title="Pick",
            description="Drag a box.",
            default_timeout_seconds=0,  # immediate timeout
        )
        return await _emit_spatial_input_and_wait(ws, state, payload)

    assert asyncio.run(_run()) is None


# =========================================================================== #
# 4. request_spatial_input catalog tool — sentinel + invalid-mode typed error.
# =========================================================================== #


def test_request_spatial_input_tool_returns_sentinel():
    from trid3nt_server.tools.meta.spatial_input_tool.spatial_input_tool import (
        SPATIAL_INPUT_SENTINEL_KEY,
        request_spatial_input,
    )

    out = asyncio.run(
        request_spatial_input(
            mode="vector_draw", title="Draw", description="Outline the AOI."
        )
    )
    assert out.get(SPATIAL_INPUT_SENTINEL_KEY) is True
    assert out["mode"] == "vector_draw"
    # the server sentinel key matches the tool's (lock-step).
    assert SPATIAL_INPUT_SENTINEL_KEY == server.SPATIAL_INPUT_SENTINEL_KEY


def test_request_spatial_input_tool_rejects_bad_mode():
    from trid3nt_server.tools.meta.spatial_input_tool.spatial_input_tool import (
        SPATIAL_INPUT_SENTINEL_KEY,
        request_spatial_input,
    )

    out = asyncio.run(request_spatial_input(mode="freehand"))
    assert out["status"] == "error"
    assert out["error_code"] == "SPATIAL_INPUT_PARAMS_INVALID"
    assert SPATIAL_INPUT_SENTINEL_KEY not in out


