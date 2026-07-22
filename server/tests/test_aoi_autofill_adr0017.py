"""ADR 0017 (structured-AOI slice, Lane S) — dispatch-time bbox auto-fill.

The canvas AOI arrives as a structured ``aoi_bbox`` field on the user-message
payload (interface contract with the client lane: ``[min_lon, min_lat,
max_lon, max_lat]`` EPSG:4326, ``None`` when absent). The server stores it as
the session's active AOI, and at dispatch a tool call whose signature
REQUIRES a bbox-like param the model OMITTED gets it auto-filled with
precedence: explicit arg > active AOI > case bbox. Explicit args are never
overridden.

Covers:
1. the pure ``autofill_missing_bbox`` helper (precedence, no-override,
   required-only, validation fallbacks);
2. ``_set_active_aoi_from_payload`` (set / clear / malformed-ignore);
3. the REAL dispatch seam (``_invoke_tool_via_emitter``) filling a dummy
   tool's required bbox from the session's active AOI.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from trid3nt_contracts import new_ulid
from trid3nt_server.tool_arg_normalizer import autofill_missing_bbox

ACTIVE_AOI = [-91.7, 41.9, -91.6, 42.0]
CASE_BBOX = [-92.0, 41.5, -91.0, 42.5]
EXPLICIT = [-80.0, 25.0, -79.5, 25.5]


# --------------------------------------------------------------------------- #
# 1. The pure helper
# --------------------------------------------------------------------------- #


def _tool_required_bbox(bbox: list[float], detail: str = "x") -> dict:
    return {"bbox": bbox}


def _tool_optional_bbox(bbox: list[float] | None = None) -> dict:
    return {"bbox": bbox}


def _tool_required_aoi_bbox(aoi_bbox: list[float]) -> dict:
    return {"aoi_bbox": aoi_bbox}


def _tool_no_bbox(location_query: str) -> dict:
    return {"q": location_query}


class TestAutofillHelper:
    def test_omitted_required_bbox_fills_from_active_aoi(self) -> None:
        out = autofill_missing_bbox(
            "fetch_dem", {}, _tool_required_bbox,
            active_aoi=ACTIVE_AOI, case_bbox=CASE_BBOX,
        )
        assert out["bbox"] == ACTIVE_AOI

    def test_case_bbox_fills_when_no_active_aoi(self) -> None:
        out = autofill_missing_bbox(
            "fetch_dem", {}, _tool_required_bbox,
            active_aoi=None, case_bbox=CASE_BBOX,
        )
        assert out["bbox"] == CASE_BBOX

    def test_explicit_arg_is_never_overridden(self) -> None:
        params = {"bbox": EXPLICIT}
        out = autofill_missing_bbox(
            "fetch_dem", params, _tool_required_bbox,
            active_aoi=ACTIVE_AOI, case_bbox=CASE_BBOX,
        )
        assert out["bbox"] == EXPLICIT
        assert out is params  # untouched — not even copied

    def test_optional_bbox_param_is_not_filled(self) -> None:
        """An optional bbox means the tool owns 'absent' semantics."""
        out = autofill_missing_bbox(
            "fetch_nws_alerts_conus", {}, _tool_optional_bbox,
            active_aoi=ACTIVE_AOI, case_bbox=CASE_BBOX,
        )
        assert "bbox" not in out

    def test_aoi_bbox_param_name_also_fills(self) -> None:
        out = autofill_missing_bbox(
            "run_swmm_urban_flood", {}, _tool_required_aoi_bbox,
            active_aoi=ACTIVE_AOI, case_bbox=None,
        )
        assert out["aoi_bbox"] == ACTIVE_AOI

    def test_tool_without_bbox_param_untouched(self) -> None:
        params = {"location_query": "Cedar Rapids"}
        out = autofill_missing_bbox(
            "geocode_location", params, _tool_no_bbox,
            active_aoi=ACTIVE_AOI, case_bbox=CASE_BBOX,
        )
        assert out is params

    def test_invalid_active_aoi_falls_through_to_case_bbox(self) -> None:
        # Degenerate (min == max) and non-finite candidates are skipped.
        out = autofill_missing_bbox(
            "fetch_dem", {}, _tool_required_bbox,
            active_aoi=[-91.7, 41.9, -91.7, 42.0], case_bbox=CASE_BBOX,
        )
        assert out["bbox"] == CASE_BBOX

    def test_no_sources_leaves_params_unchanged(self) -> None:
        params: dict[str, Any] = {}
        out = autofill_missing_bbox(
            "fetch_dem", params, _tool_required_bbox,
            active_aoi=None, case_bbox=None,
        )
        assert out is params

    def test_explicit_none_counts_as_omitted(self) -> None:
        """The model sometimes sends bbox=null — treat it as omitted."""
        out = autofill_missing_bbox(
            "fetch_dem", {"bbox": None}, _tool_required_bbox,
            active_aoi=ACTIVE_AOI, case_bbox=None,
        )
        assert out["bbox"] == ACTIVE_AOI

    def test_string_form_candidate_coerces(self) -> None:
        out = autofill_missing_bbox(
            "fetch_dem", {}, _tool_required_bbox,
            active_aoi="-91.7, 41.9, -91.6, 42.0", case_bbox=None,
        )
        assert out["bbox"] == ACTIVE_AOI

    def test_logs_one_line_when_it_fires(self, caplog) -> None:
        with caplog.at_level("INFO", logger="trid3nt_server.tool_arg_normalizer"):
            autofill_missing_bbox(
                "fetch_dem", {}, _tool_required_bbox,
                active_aoi=ACTIVE_AOI, case_bbox=None,
            )
        hits = [r for r in caplog.records if "aoi-autofill" in r.getMessage()]
        assert len(hits) == 1
        assert "active-aoi" in hits[0].getMessage()


# --------------------------------------------------------------------------- #
# 2. The session-state seam (_set_active_aoi_from_payload)
# --------------------------------------------------------------------------- #


class TestActiveAoiPayloadSeam:
    def _state(self):
        from trid3nt_server.server import SessionState

        return SessionState(session_id=new_ulid())

    def test_valid_bbox_sets_active_aoi(self) -> None:
        from trid3nt_server.server import _set_active_aoi_from_payload

        state = self._state()
        _set_active_aoi_from_payload(state, ACTIVE_AOI)
        assert state.active_aoi_bbox == ACTIVE_AOI

    def test_explicit_null_clears(self) -> None:
        from trid3nt_server.server import _set_active_aoi_from_payload

        state = self._state()
        _set_active_aoi_from_payload(state, ACTIVE_AOI)
        _set_active_aoi_from_payload(state, None)
        assert state.active_aoi_bbox is None

    @pytest.mark.parametrize(
        "malformed",
        [
            [1, 2, 3],  # wrong arity
            "not a bbox",
            [float("nan"), 0, 1, 1],
            [-91.6, 41.9, -91.7, 42.0],  # min > max
            {"min_lon": -91.7},
        ],
    )
    def test_malformed_value_is_ignored_never_clobbers(self, malformed) -> None:
        from trid3nt_server.server import _set_active_aoi_from_payload

        state = self._state()
        _set_active_aoi_from_payload(state, ACTIVE_AOI)
        _set_active_aoi_from_payload(state, malformed)
        assert state.active_aoi_bbox == ACTIVE_AOI


# --------------------------------------------------------------------------- #
# 3. The real dispatch seam
# --------------------------------------------------------------------------- #


class MockWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        self.sent.append(json.loads(raw))


@pytest.fixture()
def _dummy_bbox_tool():
    """Register a dummy fetch tool whose bbox is REQUIRED."""
    from trid3nt_contracts.tool_registry import AtomicToolMetadata
    from trid3nt_server.tools import TOOL_REGISTRY, RegisteredTool

    captured: dict[str, Any] = {}

    def fetch_needs_bbox(bbox: list[float], detail: str = "std") -> dict:
        captured["bbox"] = bbox
        return {"ok": True, "n": 1}

    saved = dict(TOOL_REGISTRY)
    TOOL_REGISTRY["fetch_needs_bbox_t"] = RegisteredTool(
        fn=fetch_needs_bbox,
        metadata=AtomicToolMetadata(
            name="fetch_needs_bbox_t",
            ttl_class="live-no-cache",
            source_class="api_fetch",
            cacheable=False,
        ),
        module=__name__,
    )
    try:
        yield captured
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(saved)


def test_dispatch_seam_fills_omitted_bbox_from_active_aoi(_dummy_bbox_tool) -> None:
    from trid3nt_server.server import SessionState, _invoke_tool_via_emitter

    async def run() -> None:
        ws = MockWebSocket()
        state = SessionState(session_id=new_ulid())
        state.active_aoi_bbox = list(ACTIVE_AOI)
        await _invoke_tool_via_emitter(ws, state, "fetch_needs_bbox_t", {})
        assert _dummy_bbox_tool["bbox"] == ACTIVE_AOI

    asyncio.run(run())


def test_dispatch_seam_never_overrides_explicit_bbox(_dummy_bbox_tool) -> None:
    from trid3nt_server.server import SessionState, _invoke_tool_via_emitter

    async def run() -> None:
        ws = MockWebSocket()
        state = SessionState(session_id=new_ulid())
        state.active_aoi_bbox = list(ACTIVE_AOI)
        await _invoke_tool_via_emitter(
            ws, state, "fetch_needs_bbox_t", {"bbox": list(EXPLICIT)}
        )
        assert _dummy_bbox_tool["bbox"] == EXPLICIT

    asyncio.run(run())
