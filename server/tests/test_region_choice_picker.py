"""Tests for the agent-side region-disambiguation picker.

The picker layers an INTERACTIVE narrowing choice on top of the job-0346
state-bbox-fallback: when ``geocode_location`` snaps a vague/regional query to
the WHOLE state bbox (``source == "state-bbox-fallback"``), the agent surfaces a
``region-choice-request`` (whole-state default + candidate counties), PAUSES the
turn, and on ``region-choice-provided`` either narrows the geocode bbox to the
picked region or keeps the whole-state bbox.

This MIRRORS the credential-request pause/resume seam (test_credential_pipeline)
exactly. Covered here:

1. State-snap geocode result triggers a region-choice-request carrying the
   state's counties + the whole-state default (default_action="use_whole_state").
2. A PRECISE geocode (source != state-bbox-fallback) does NOT trigger it.
3. region-choice-provided(choice="region") narrows the geocode bbox to the
   picked region's bbox (re-resolved by region_id).
4. region-choice-provided(choice="whole_state") keeps the state bbox unchanged.
5. The request + provided envelopes (de)serialize, and the contracts are wired
   into the ws.py routing registries.
6. The region-set builder turns TIGER FlatGeobuf features into per-county
   {region_id, name, bbox, admin_level} candidates (mocked fetch).
"""

from __future__ import annotations

import asyncio
import json
from io import BytesIO
from typing import Any
from unittest.mock import patch

import pytest

from trid3nt_server import server
from trid3nt_server.server import (
    SessionState,
    _build_region_candidates,
    _build_region_choice_request_payload,
    _maybe_handle_region_choice,
    _resolve_pending_region_choice,
)
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.region_choice import (
    RegionCandidate,
    RegionChoiceProvidedEnvelopePayload,
    RegionChoiceRequestEnvelopePayload,
)
from trid3nt_contracts.ws import (
    AGENT_TO_CLIENT_PAYLOADS,
    ALL_PAYLOADS,
    CLIENT_TO_AGENT_PAYLOADS,
)


# --------------------------------------------------------------------------- #
# MockWebSocket — collects wire envelopes for assertion (mirrors credential tests).
# --------------------------------------------------------------------------- #


class MockWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            self.sent.append(json.loads(raw))
        else:
            self.sent.append(raw)


# A non-None sentinel for ``state.emitter`` — _maybe_handle_region_choice only
# checks ``state.emitter is not None`` (an interactive surface is bound); it
# never calls into the emitter. The real PipelineEmitter is exercised elsewhere.
_EMITTER_SENTINEL = object()


def _florida_state_snap_result() -> dict:
    """A geocode_location state-snap result for 'south Florida' (job-0346 shape)."""
    return {
        "name": "Florida, United States",
        "bbox": [-87.634896, 24.396308, -79.974306, 31.000888],
        "latitude": 27.7,
        "longitude": -83.8,
        "source": "state-bbox-fallback",
        "query": "protected areas in south Florida",
        "osm_type": None,
        "osm_id": None,
        "place_id": None,
        "fallback_reason": (
            "No precise match for 'protected areas in south Florida'; "
            "snapped to the full state of Florida. Refine the prompt for a "
            "smaller area."
        ),
        "state_bbox_source": "census-offline",
    }


def _florida_counties() -> list[RegionCandidate]:
    """Two sample Florida counties as the mocked candidate set."""
    return [
        RegionCandidate(
            region_id="county-12071",
            name="Lee County",
            bbox=(-82.331, 26.317, -81.564, 26.795),
            admin_level="county",
        ),
        RegionCandidate(
            region_id="county-12086",
            name="Miami-Dade County",
            bbox=(-80.873, 25.137, -80.118, 25.979),
            admin_level="county",
        ),
    ]


async def _drive_region_choice(
    geocode_result: dict,
    reply: RegionChoiceProvidedEnvelopePayload | None,
    *,
    candidates: list[RegionCandidate] | None = None,
) -> MockWebSocket:
    """Drive _maybe_handle_region_choice with a mocked candidate set + reply.

    Spawns the handler as a task, waits for the request envelope + pending
    future, then resolves it with ``reply`` (or lets it run to completion when
    ``reply is None`` — e.g. the precise-geocode no-op path).
    """
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    state.emitter = _EMITTER_SENTINEL  # type: ignore[assignment]

    cand = _florida_counties() if candidates is None else candidates
    with patch.object(server, "_build_region_candidates", return_value=cand):
        handler = asyncio.create_task(
            _maybe_handle_region_choice(ws, state, geocode_result)
        )
        if reply is not None:
            # Wait for the request to be emitted + the pending future registered.
            for _ in range(100):
                await asyncio.sleep(0)
                req = [
                    e for e in ws.sent if e["type"] == "region-choice-request"
                ]
                if req and server._PENDING_REGION_CHOICES:
                    break
            req = [e for e in ws.sent if e["type"] == "region-choice-request"]
            assert req, "region-choice-request must be emitted for a state snap"
            request_id = req[0]["payload"]["request_id"]
            # Echo the real request_id into the reply.
            reply = reply.model_copy(update={"request_id": request_id})
            assert _resolve_pending_region_choice(state.session_id, reply)
        await handler
    return ws


# =========================================================================== #
# 1. State-snap result TRIGGERS a region-choice-request with counties + default
# =========================================================================== #


def test_state_snap_triggers_region_choice_request_with_counties():
    geocode_result = _florida_state_snap_result()

    async def _run() -> MockWebSocket:
        # Keep whole_state so the handler completes deterministically; we only
        # assert the REQUEST shape here.
        reply = RegionChoiceProvidedEnvelopePayload(
            request_id=new_ulid(), choice="whole_state"
        )
        return await _drive_region_choice(geocode_result, reply)

    ws = asyncio.run(_run())
    reqs = [e for e in ws.sent if e["type"] == "region-choice-request"]
    assert len(reqs) == 1, "exactly one region-choice-request for a state snap"
    payload = reqs[0]["payload"]
    assert payload["envelope_type"] == "region-choice-request"
    assert payload["state_name"] == "Florida"
    assert payload["state_code"] == "FL"
    # whole-state default is the honest, already-resolved automated answer.
    assert payload["default_action"] == "use_whole_state"
    assert tuple(payload["state_bbox"]) == tuple(geocode_result["bbox"])
    # The state's counties are the candidate set (default granularity).
    names = {c["name"] for c in payload["candidates"]}
    assert names == {"Lee County", "Miami-Dade County"}
    assert all(c["admin_level"] == "county" for c in payload["candidates"])
    # Honest narration: names the whole-state snap + the narrower offer.
    msg = payload["message"].lower()
    assert "whole state" in msg and "florida" in msg


# =========================================================================== #
# 2. A PRECISE geocode does NOT trigger the picker
# =========================================================================== #


def test_precise_geocode_does_not_trigger_region_choice():
    precise = {
        "name": "Fort Myers, Lee County, Florida, United States",
        "bbox": [-81.95, 26.55, -81.80, 26.70],
        "latitude": 26.64,
        "longitude": -81.87,
        "source": "nominatim",
        "query": "Fort Myers, FL",
    }

    async def _run() -> tuple[MockWebSocket, dict]:
        ws = MockWebSocket()
        state = SessionState(session_id=new_ulid())
        state.emitter = _EMITTER_SENTINEL  # type: ignore[assignment]
        # If the builder were called, it would be a bug — patch it to explode.
        with patch.object(
            server,
            "_build_region_candidates",
            side_effect=AssertionError("must NOT build candidates for a precise geocode"),
        ):
            await _maybe_handle_region_choice(ws, state, precise)
        return ws, precise

    ws, result = asyncio.run(_run())
    assert not [e for e in ws.sent if e["type"] == "region-choice-request"], (
        "a precise (non-fallback) geocode must NOT emit a region-choice-request"
    )
    # The result is untouched.
    assert result["bbox"] == [-81.95, 26.55, -81.80, 26.70]
    assert result["source"] == "nominatim"
    assert "region_choice" not in result
    # And nothing is left pending.
    assert not server._PENDING_REGION_CHOICES


# =========================================================================== #
# 3. region-choice-provided(region) narrows the geocode bbox
# =========================================================================== #


def test_region_choice_provided_region_narrows_bbox():
    geocode_result = _florida_state_snap_result()

    async def _run() -> dict:
        reply = RegionChoiceProvidedEnvelopePayload(
            request_id=new_ulid(),
            choice="region",
            selected_region_id="county-12071",
            # Send a DIFFERENT bbox than the candidate's to prove the agent
            # re-resolves authoritatively by region_id (not the client bbox).
            selected_bbox=(-1.0, -1.0, 1.0, 1.0),
        )
        await _drive_region_choice(geocode_result, reply)
        return geocode_result

    result = asyncio.run(_run())
    # bbox narrowed to Lee County's bbox (re-resolved by region_id, NOT the
    # tampered client bbox).
    assert result["bbox"] == [-82.331, 26.317, -81.564, 26.795]
    assert result["region_choice"] == "region"
    assert result["selected_region_id"] == "county-12071"
    assert result["region_name"] == "Lee County"
    # No longer a whole-state snap — source flips so a re-trigger won't re-offer.
    assert result["source"] == "region-choice-narrowed"
    # Centroid recomputed for the narrowed extent.
    assert result["longitude"] == pytest.approx((-82.331 + -81.564) / 2.0)
    assert result["latitude"] == pytest.approx((26.317 + 26.795) / 2.0)


def test_region_choice_provided_region_falls_back_to_bbox_when_id_unknown():
    """An unknown region_id with an echoed bbox narrows to the echoed bbox."""
    geocode_result = _florida_state_snap_result()

    async def _run() -> dict:
        reply = RegionChoiceProvidedEnvelopePayload(
            request_id=new_ulid(),
            choice="region",
            selected_region_id="county-99999",  # not in the candidate set
            selected_bbox=(-82.0, 26.0, -81.0, 27.0),
        )
        await _drive_region_choice(geocode_result, reply)
        return geocode_result

    result = asyncio.run(_run())
    assert result["bbox"] == [-82.0, 26.0, -81.0, 27.0]
    assert result["region_choice"] == "region"
    assert result["source"] == "region-choice-narrowed"


# =========================================================================== #
# 4. region-choice-provided(whole_state) keeps the state bbox
# =========================================================================== #


def test_region_choice_provided_whole_state_keeps_state_bbox():
    geocode_result = _florida_state_snap_result()
    original_bbox = list(geocode_result["bbox"])

    async def _run() -> dict:
        reply = RegionChoiceProvidedEnvelopePayload(
            request_id=new_ulid(), choice="whole_state"
        )
        await _drive_region_choice(geocode_result, reply)
        return geocode_result

    result = asyncio.run(_run())
    # Whole-state default kept verbatim.
    assert result["bbox"] == original_bbox
    assert result["source"] == "state-bbox-fallback"
    assert result["region_choice"] == "whole_state"
    assert "selected_region_id" not in result


def test_region_choice_timeout_keeps_state_bbox():
    """No client answers (headless): the state bbox stays the result, unchanged."""
    geocode_result = _florida_state_snap_result()
    original_bbox = list(geocode_result["bbox"])

    async def _run() -> dict:
        ws = MockWebSocket()
        state = SessionState(session_id=new_ulid())
        state.emitter = _EMITTER_SENTINEL  # type: ignore[assignment]
        with patch.object(
            server, "_build_region_candidates", return_value=_florida_counties()
        ), patch.object(server, "CODE_EXEC_CONFIRM_TIMEOUT_SECONDS", 0.01):
            await _maybe_handle_region_choice(ws, state, geocode_result)
        return geocode_result

    result = asyncio.run(_run())
    assert result["bbox"] == original_bbox
    assert result["source"] == "state-bbox-fallback"
    assert result["region_choice"] == "whole_state"
    # Pending future is cleaned up after the timeout.
    assert not server._PENDING_REGION_CHOICES


# =========================================================================== #
# 5. Envelope (de)serialization + ws.py registry wiring
# =========================================================================== #


def test_envelopes_roundtrip_and_are_registered():
    req = RegionChoiceRequestEnvelopePayload(
        request_id=new_ulid(),
        state_name="Texas",
        state_code="TX",
        state_bbox=(-106.65, 25.84, -93.51, 36.50),
        candidates=[
            RegionCandidate(
                region_id="county-48201",
                name="Harris County",
                bbox=(-95.96, 29.49, -94.91, 30.17),
            )
        ],
        message="Snapped to the whole state of Texas; pick a county to narrow.",
    )
    req2 = RegionChoiceRequestEnvelopePayload.model_validate_json(
        req.model_dump_json()
    )
    assert req2 == req
    assert req2.default_action == "use_whole_state"
    assert req2.candidates[0].admin_level == "county"

    prov = RegionChoiceProvidedEnvelopePayload(
        request_id=req.request_id,
        choice="region",
        selected_region_id="county-48201",
        selected_bbox=(-95.96, 29.49, -94.91, 30.17),
    )
    prov2 = RegionChoiceProvidedEnvelopePayload.model_validate_json(
        prov.model_dump_json()
    )
    assert prov2 == prov

    # whole_state reply carries no region id/bbox.
    ws_reply = RegionChoiceProvidedEnvelopePayload(
        request_id=req.request_id, choice="whole_state"
    )
    assert ws_reply.selected_region_id is None
    assert ws_reply.selected_bbox is None

    # Registry wiring (mirrors the credential envelopes): request is A.4
    # (agent->client), provided is A.3 (client->agent).
    assert AGENT_TO_CLIENT_PAYLOADS["region-choice-request"] is (
        RegionChoiceRequestEnvelopePayload
    )
    assert CLIENT_TO_AGENT_PAYLOADS["region-choice-provided"] is (
        RegionChoiceProvidedEnvelopePayload
    )
    assert "region-choice-request" in ALL_PAYLOADS
    assert "region-choice-provided" in ALL_PAYLOADS


def test_request_payload_built_from_state_snap_result():
    """_build_region_choice_request_payload derives state name/code/bbox + prompt."""
    geocode_result = _florida_state_snap_result()
    with patch.object(
        server, "_build_region_candidates", return_value=_florida_counties()
    ):
        payload = _build_region_choice_request_payload(
            request_id=new_ulid(), geocode_result=geocode_result
        )
    assert payload is not None
    assert payload.state_name == "Florida"
    assert payload.state_code == "FL"
    assert payload.state_bbox == tuple(geocode_result["bbox"])
    assert len(payload.candidates) == 2
    assert payload.default_action == "use_whole_state"

    # A result whose name does not resolve to a US state yields None (no
    # picker) — the builder cannot scope counties without a state.
    unresolvable = {
        "name": "Fort Myers, FL",
        "bbox": [-81.95, 26.55, -81.80, 26.70],
    }
    assert (
        _build_region_choice_request_payload(
            request_id=new_ulid(), geocode_result=unresolvable
        )
        is None
    )

    # A result missing a bbox also yields None (cannot build a request).
    no_bbox = {"name": "Florida, United States"}
    assert (
        _build_region_choice_request_payload(
            request_id=new_ulid(), geocode_result=no_bbox
        )
        is None
    )


# =========================================================================== #
# 6. Region-set builder: TIGER FlatGeobuf features -> per-county candidates
# =========================================================================== #


def _make_tiger_county_fgb_bytes() -> bytes:
    """Build a tiny FlatGeobuf mimicking TIGER county fields (GEOID/NAMELSAD)."""
    import geopandas as gpd
    from shapely.geometry import box as shapely_box

    gdf = gpd.GeoDataFrame(
        {
            "GEOID": ["12071", "12086"],
            "NAMELSAD": ["Lee County", "Miami-Dade County"],
            "NAME": ["Lee", "Miami-Dade"],
        },
        geometry=[
            shapely_box(-82.331, 26.317, -81.564, 26.795),
            shapely_box(-80.873, 25.137, -80.118, 25.979),
        ],
        crs="EPSG:4326",
    )
    buf = BytesIO()
    gdf.to_file(buf, driver="FlatGeobuf", engine="pyogrio")
    return buf.getvalue()


def test_build_region_candidates_from_tiger_features():
    fgb = _make_tiger_county_fgb_bytes()
    with patch(
        "trid3nt_server.tools.fetch_administrative_boundaries._fetch_admin_boundaries_bytes",
        return_value=fgb,
    ):
        candidates = _build_region_candidates(
            (-87.634896, 24.396308, -79.974306, 31.000888), "county"
        )
    assert len(candidates) == 2
    by_name = {c.name: c for c in candidates}
    assert set(by_name) == {"Lee County", "Miami-Dade County"}
    lee = by_name["Lee County"]
    assert lee.region_id == "county-12071"
    assert lee.admin_level == "county"
    # total_bounds of the county polygon.
    assert lee.bbox == pytest.approx((-82.331, 26.317, -81.564, 26.795))


def test_build_region_candidates_degrades_to_empty_on_fetch_failure():
    """A TIGER fetch failure yields an EMPTY candidate set (honest degrade)."""
    with patch(
        "trid3nt_server.tools.fetch_administrative_boundaries._fetch_admin_boundaries_bytes",
        side_effect=RuntimeError("TIGER download failed"),
    ):
        candidates = _build_region_candidates(
            (-87.6, 24.4, -80.0, 31.0), "county"
        )
    assert candidates == []
