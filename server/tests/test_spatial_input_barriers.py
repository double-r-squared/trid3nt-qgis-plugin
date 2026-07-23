"""FR-AS-10 / FR-WC-16: the AGENT consuming a drawn FeatureCollection.

Proves the FULL agent-side wire for the urban vector-draw flow:

1. PURE PARSE (``trid3nt_server.spatial_input``): a role-tagged drawn
   ``FeatureCollection`` (aoi polygon + barrier polylines tagged wall/flap_gate
   + points) splits into engine-ready inputs — the clean ``barriers``
   FeatureCollection, the AOI bbox, the point list — and EVERY malformed shape
   degrades to a TYPED ``SpatialInputParseError`` (never a silent success /
   fabricated geometry — the honesty floor).

2. RESPONSE -> RESULT (``server._spatial_response_to_result``): a
   ``spatial-input-response`` (vector_draw / point / bbox / cancel / timeout /
   malformed) maps to the typed result the LLM reads, carrying the engine-ready
   ``barriers`` on the vector_draw path.

3. PARSED BARRIERS FEED THE EXISTING ENGINE (NO re-architecture): the parsed
   ``barriers`` FeatureCollection (a) validates field-for-field against
   ``swmm_contracts.SWMMRunArgs.barriers``, and (b) drives
   ``build_swmm_mesh(barriers=...)`` so a RED ``wall`` OMITS the overland conduit
   and a GREEN ``flap_gate`` becomes a one-way SWMM orifice
   (``has_flap_gate=True``) — exactly the wall=omit / flap_gate=one-way seam.

4. PAUSE/RESUME REGISTRY + INBOUND RESOLVE (``server`` spatial-input gate): the
   ``_PENDING_SPATIAL_INPUTS`` registry + ``_resolve_pending_spatial_input``
   mirror the region-choice gate (cross-session refusal, unknown-id no-op), and
   ``_emit_spatial_input_and_wait`` round-trips a drawn reply.

5. TOOL SENTINEL (``tools/spatial_input_tool``): ``request_spatial_input``
   returns the sentinel the turn loop intercepts, and rejects an unknown mode
   with a typed error.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from trid3nt_server import server
from trid3nt_server.server import (
    SessionState,
    _emit_spatial_input_and_wait,
    _resolve_pending_spatial_input,
    _spatial_response_to_result,
)
from trid3nt_server.spatial_input import (
    ParsedSpatialInput,
    SpatialInputParseError,
    barriers_feature_collection,
    parse_spatial_input_features,
    split_features_by_role,
)
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.swmm_contracts import SWMMRunArgs
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


def _wall_feature() -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"role": "barrier", "barrier_type": "wall"},
        "geometry": {
            "type": "LineString",
            "coordinates": [[-85.305, 35.045], [-85.305, 35.055]],
        },
    }


def _flap_feature() -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {
            "role": "barrier",
            "barrier_type": "flap_gate",
            "protected_side": "left",
            "flap_direction": "out",
        },
        "geometry": {
            "type": "LineString",
            "coordinates": [[-85.300, 35.048], [-85.298, 35.048]],
        },
    }


def _point_feature() -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"role": "point"},
        "geometry": {"type": "Point", "coordinates": [-85.300, 35.050]},
    }


def _full_drawn_fc() -> dict[str, Any]:
    """A complete drawn FeatureCollection: AOI + wall + flap_gate + point."""
    return {
        "type": "FeatureCollection",
        "features": [
            _aoi_feature(),
            _wall_feature(),
            _flap_feature(),
            _point_feature(),
        ],
    }


# =========================================================================== #
# 1. PURE PARSE — role split + engine-ready barriers.
# =========================================================================== #


def test_split_features_by_role_buckets_all_roles():
    buckets = split_features_by_role(_full_drawn_fc())
    assert len(buckets["aoi"]) == 1
    assert len(buckets["barrier"]) == 2
    assert len(buckets["point"]) == 1


def test_parse_full_drawn_fc_produces_engine_inputs():
    parsed = parse_spatial_input_features(_full_drawn_fc())
    assert isinstance(parsed, ParsedSpatialInput)
    # barriers FC is engine-shaped: every feature is a LineString with
    # barrier_type and NO role property (the engine FC has no role field).
    assert parsed.barriers is not None
    assert parsed.barriers["type"] == "FeatureCollection"
    assert len(parsed.barriers["features"]) == 2
    for feat in parsed.barriers["features"]:
        assert feat["geometry"]["type"] == "LineString"
        assert feat["properties"]["barrier_type"] in ("wall", "flap_gate")
        assert "role" not in feat["properties"], "role must be stripped for the engine"
    assert parsed.n_walls == 1
    assert parsed.n_flap_gates == 1
    # flap_direction + protected_side ride through to the engine seam.
    flap = next(
        f
        for f in parsed.barriers["features"]
        if f["properties"]["barrier_type"] == "flap_gate"
    )
    assert flap["properties"]["protected_side"] == "left"
    assert flap["properties"]["flap_direction"] == "out"
    # AOI bbox derived from the polygon ring.
    assert parsed.aoi_bbox is not None
    assert math.isclose(parsed.aoi_bbox[0], -85.31)
    assert math.isclose(parsed.aoi_bbox[1], 35.04)
    assert math.isclose(parsed.aoi_bbox[2], -85.29)
    assert math.isclose(parsed.aoi_bbox[3], 35.06)
    # one drawn point.
    assert parsed.points == [[-85.300, 35.050]]


def test_parse_barriers_only_no_aoi_no_points():
    fc = {"type": "FeatureCollection", "features": [_wall_feature()]}
    parsed = parse_spatial_input_features(fc)
    assert parsed.barriers is not None
    assert parsed.n_walls == 1
    assert parsed.n_flap_gates == 0
    assert parsed.aoi_bbox is None
    assert parsed.points == []


def test_parse_empty_barriers_returns_none_not_empty_fc():
    """No barriers drawn -> barriers is None (a plain run), never an empty FC."""
    fc = {"type": "FeatureCollection", "features": [_aoi_feature()]}
    parsed = parse_spatial_input_features(fc)
    assert parsed.barriers is None, "empty barriers must be None for a plain run"
    assert parsed.aoi_bbox is not None


def test_barriers_feature_collection_numeric_flap_bearing():
    """A numeric flap bearing (degrees) rides through as a valid flap_direction."""
    feat = _flap_feature()
    feat["properties"]["flap_direction"] = 270.0
    fc, n_walls, n_flap = barriers_feature_collection([feat])
    assert n_flap == 1 and n_walls == 0
    assert fc["features"][0]["properties"]["flap_direction"] == 270.0


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
    feat = _wall_feature()
    feat["properties"]["role"] = "river"  # not in {aoi, barrier, point}
    with pytest.raises(SpatialInputParseError) as ei:
        parse_spatial_input_features(
            {"type": "FeatureCollection", "features": [feat]}
        )
    assert ei.value.error_code == "SPATIAL_INPUT_BAD_ROLE"


def test_barrier_not_linestring_raises():
    feat = _wall_feature()
    feat["geometry"] = {"type": "Point", "coordinates": [-85.3, 35.05]}
    with pytest.raises(SpatialInputParseError) as ei:
        parse_spatial_input_features(
            {"type": "FeatureCollection", "features": [feat]}
        )
    assert ei.value.error_code == "SPATIAL_INPUT_BARRIER_NOT_LINESTRING"


def test_barrier_too_short_raises():
    feat = _wall_feature()
    feat["geometry"]["coordinates"] = [[-85.3, 35.05]]  # only 1 position
    with pytest.raises(SpatialInputParseError) as ei:
        parse_spatial_input_features(
            {"type": "FeatureCollection", "features": [feat]}
        )
    assert ei.value.error_code == "SPATIAL_INPUT_BARRIER_TOO_SHORT"


def test_bad_barrier_type_raises():
    feat = _wall_feature()
    feat["properties"]["barrier_type"] = "levee"  # not in {wall, flap_gate}
    with pytest.raises(SpatialInputParseError) as ei:
        parse_spatial_input_features(
            {"type": "FeatureCollection", "features": [feat]}
        )
    assert ei.value.error_code == "SPATIAL_INPUT_BAD_BARRIER_TYPE"


def test_bad_protected_side_raises():
    feat = _flap_feature()
    feat["properties"]["protected_side"] = "up"  # not in {left, right}
    with pytest.raises(SpatialInputParseError) as ei:
        parse_spatial_input_features(
            {"type": "FeatureCollection", "features": [feat]}
        )
    assert ei.value.error_code == "SPATIAL_INPUT_BAD_PROTECTED_SIDE"


def test_bad_flap_direction_raises():
    feat = _flap_feature()
    feat["properties"]["flap_direction"] = "sideways"  # not in/out, not numeric
    with pytest.raises(SpatialInputParseError) as ei:
        parse_spatial_input_features(
            {"type": "FeatureCollection", "features": [feat]}
        )
    assert ei.value.error_code == "SPATIAL_INPUT_BAD_FLAP_DIRECTION"


def test_point_wrong_geometry_raises():
    feat = _point_feature()
    feat["geometry"] = {"type": "LineString", "coordinates": [[1, 2], [3, 4]]}
    with pytest.raises(SpatialInputParseError) as ei:
        parse_spatial_input_features(
            {"type": "FeatureCollection", "features": [feat]}
        )
    assert ei.value.error_code == "SPATIAL_INPUT_POINT_NOT_POINT"


# =========================================================================== #
# 2. Parsed barriers VALIDATE against the SWMM engine contract.
# =========================================================================== #


def test_parsed_barriers_construct_swmm_run_args():
    """The role=='barrier' subset round-trips into SWMMRunArgs(barriers=...)
    UNCHANGED (the structural validator accepts it field-for-field)."""
    parsed = parse_spatial_input_features(_full_drawn_fc())
    # bbox from the drawn AOI; barriers from the drawn walls/flap-gates.
    args = SWMMRunArgs(
        bbox=tuple(parsed.aoi_bbox),  # type: ignore[arg-type]
        barriers=parsed.barriers,
    )
    assert args.barriers is not None
    assert len(args.barriers["features"]) == 2
    tags = {f["properties"]["barrier_type"] for f in args.barriers["features"]}
    assert tags == {"wall", "flap_gate"}


# =========================================================================== #
# 3. spatial-input-response -> the LLM-facing result.
# =========================================================================== #


def test_response_vector_draw_carries_engine_barriers():
    resp = SpatialInputResponsePayload(
        request_id=new_ulid(),
        geometry_type="vector_draw",
        features=_full_drawn_fc(),
    )
    result = _spatial_response_to_result(resp)
    assert result["status"] == "ok"
    assert result["geometry_type"] == "vector_draw"
    assert result["n_walls"] == 1
    assert result["n_flap_gates"] == 1
    assert result["n_aoi"] == 1
    assert result["points"] == [[-85.300, 35.050]]
    assert "aoi_bbox" in result and len(result["aoi_bbox"]) == 4
    # the engine-ready barriers FC is on the result — pass straight to the tool.
    assert "barriers" in result
    SWMMRunArgs(bbox=tuple(result["aoi_bbox"]), barriers=result["barriers"])


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
    assert "barriers" not in r and "aoi_bbox" not in r


def test_response_timeout_is_typed_error():
    """No reply (None) -> a typed timeout error, never a fabricated success."""
    r = _spatial_response_to_result(None)
    assert r["status"] == "error"
    assert r["error_code"] == "SPATIAL_INPUT_TIMEOUT"


def test_response_malformed_features_rejected_at_contract_boundary():
    """The contract validator REJECTS a bad barrier_type at construction — the
    first line of the honesty floor (a malformed draw never even reaches the
    result mapper; the inbound handler returns TOOL_PARAMS_INVALID)."""
    bad = _full_drawn_fc()
    bad["features"][1]["properties"]["barrier_type"] = "levee"  # invalid tag
    with pytest.raises(Exception) as ei:  # pydantic ValidationError
        SpatialInputResponsePayload(
            request_id=new_ulid(),
            geometry_type="vector_draw",
            features=bad,
        )
    assert "barrier_type" in str(ei.value)


def test_response_malformed_features_is_typed_error_second_layer():
    """SECOND layer of the honesty floor: if a malformed FeatureCollection ever
    reaches the result mapper (e.g. contract validation bypassed), it degrades to
    a TYPED error, never a silent success / fabricated barriers."""
    bad = _full_drawn_fc()
    bad["features"][1]["properties"]["barrier_type"] = "levee"  # invalid tag
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
    assert r["error_code"] == "SPATIAL_INPUT_BAD_BARRIER_TYPE"
    assert "barriers" not in r


# =========================================================================== #
# 4. Pending-future registry + inbound resolve + emit/await round-trip.
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
    # the round-tripped reply parses into engine barriers.
    result = _spatial_response_to_result(resp)
    assert result["status"] == "ok"
    assert result["n_walls"] == 1 and result["n_flap_gates"] == 1


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
# 5. request_spatial_input catalog tool — sentinel + invalid-mode typed error.
# =========================================================================== #


def test_request_spatial_input_tool_returns_sentinel():
    from trid3nt_server.tools.meta.spatial_input_tool import (
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
    from trid3nt_server.tools.meta.spatial_input_tool import (
        SPATIAL_INPUT_SENTINEL_KEY,
        request_spatial_input,
    )

    out = asyncio.run(request_spatial_input(mode="freehand"))
    assert out["status"] == "error"
    assert out["error_code"] == "SPATIAL_INPUT_PARAMS_INVALID"
    assert SPATIAL_INPUT_SENTINEL_KEY not in out


# =========================================================================== #
# 6. END-TO-END: drawn barriers -> build_swmm_mesh wall=omit / flap=one-way.
# =========================================================================== #

# These reuse the proven synthetic-AOI harness from test_swmm_mesh_builder so the
# drawn FeatureCollection drives the REAL engine (no re-architecture).
swmm_api = pytest.importorskip("swmm_api")
pyswmm = pytest.importorskip("pyswmm")

from trid3nt_server.workflows import swmm_mesh_builder as mb  # noqa: E402
from trid3nt_server.workflows.swmm_mesh_builder import build_swmm_mesh  # noqa: E402

_N = 20
_CELL = 10.0
_EPSG = 32616
_OX, _OY = 500000.0, 4000000.0


def _build_dem_array() -> np.ndarray:
    ii, jj = np.meshgrid(np.arange(_N), np.arange(_N), indexing="ij")
    plane = 30.0 - 0.02 * _CELL * (ii + jj)
    ci = cj = (_N - 1) / 2.0
    r2 = (ii - ci) ** 2 + (jj - cj) ** 2
    pit = 2.0 * np.exp(-r2 / (2.0 * 3.0**2))
    return (plane - pit).astype("float64")


def _write_dem(path: Path) -> None:
    import rasterio
    from rasterio.crs import CRS
    from rasterio.transform import from_origin

    dem = _build_dem_array()
    transform = from_origin(_OX, _OY, _CELL, _CELL)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": _N,
        "width": _N,
        "crs": CRS.from_epsg(_EPSG),
        "transform": transform,
        "nodata": -9999.0,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(dem.astype("float32"), 1)


def _cell_to_lonlat(i: int, j: int) -> tuple[float, float]:
    from rasterio.transform import from_origin, xy
    from rasterio.warp import transform as warp_transform

    transform = from_origin(_OX, _OY, _CELL, _CELL)
    x, y = xy(transform, i, j)
    lons, lats = warp_transform(f"EPSG:{_EPSG}", "EPSG:4326", [x], [y])
    return lons[0], lats[0]


def _drawn_fc_for_synthetic_grid() -> dict[str, Any]:
    """A drawn FeatureCollection whose barriers land on the SAME edges the
    proven mesh-builder fixture uses (wall (8,9)-(9,9); flap (3,3)-(4,3)),
    tagged with FR-WC-16 ``role`` so the agent parser drives it."""
    a_lon, a_lat = _cell_to_lonlat(8, 9)
    b_lon, b_lat = _cell_to_lonlat(9, 9)
    f_lon, f_lat = _cell_to_lonlat(3, 3)
    g_lon, g_lat = _cell_to_lonlat(4, 3)
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"role": "barrier", "barrier_type": "wall"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[a_lon, a_lat], [b_lon, b_lat]],
                },
            },
            {
                "type": "Feature",
                "properties": {
                    "role": "barrier",
                    "barrier_type": "flap_gate",
                    "protected_side": "left",
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[f_lon, f_lat], [g_lon, g_lat]],
                },
            },
        ],
    }


def test_drawn_barriers_drive_engine_wall_omit_and_flap_oneway(tmp_path: Path):
    """The FULL agent path: a drawn role-tagged FeatureCollection -> agent parse
    -> SWMMRunArgs.barriers -> build_swmm_mesh, asserting the RED wall OMITS its
    overland conduit and the GREEN flap is a one-way orifice (has_flap_gate)."""
    dem_path = tmp_path / "dem.tif"
    _write_dem(dem_path)

    # AGENT-SIDE STEP: parse the drawn FC exactly as the server does on the
    # spatial-input-response, then hand the parsed barriers to the engine.
    parsed = parse_spatial_input_features(_drawn_fc_for_synthetic_grid())
    assert parsed.n_walls == 1 and parsed.n_flap_gates == 1
    # round-trips through the engine contract first (the same path the tool uses).
    run_args = SWMMRunArgs(
        bbox=(_cell_to_lonlat(0, 0)[0], _cell_to_lonlat(_N - 1, _N - 1)[1],
              _cell_to_lonlat(_N - 1, _N - 1)[0], _cell_to_lonlat(0, 0)[1]),
        barriers=parsed.barriers,
    )

    out_inp = tmp_path / "mesh.inp"
    build = build_swmm_mesh(
        dem_path=str(dem_path),
        out_inp_path=str(out_inp),
        total_rain_depth_mm=120.0,
        storm_duration_hr=1.0,
        rain_interval_min=5,
        target_resolution_m=10.0,
        building_footprints=None,
        building_representation="drop",
        infiltration_method="none",
        barriers=run_args.barriers,  # the parsed-from-drawing barriers
        enable_autoscale=False,
    )

    from swmm_api import SwmmInput
    from swmm_api.input_file.section_labels import CONDUITS, ORIFICES

    inp = SwmmInput.read_file(build.inp_path)
    conduits = inp[CONDUITS]
    orifices = inp[ORIFICES]

    # GREEN flap = a one-way ORIFICE with has_flap_gate=True.
    assert build.n_flap_gates >= 1, "drawn flap_gate must snap to >= 1 orifice"
    flap_with_gate = [
        o for o in orifices.values() if getattr(o, "has_flap_gate", False)
    ]
    assert len(flap_with_gate) == build.n_flap_gates

    # RED wall = an OMITTED overland conduit between the walled cells (8,9)-(9,9).
    walled = {mb._cell_node(8, 9), mb._cell_node(9, 9)}
    between = [
        c
        for c in conduits.values()
        if {getattr(c, "from_node", None), getattr(c, "to_node", None)} == walled
    ]
    assert between == [], "drawn wall must OMIT the overland conduit between cells"
    assert build.n_walls >= 1
