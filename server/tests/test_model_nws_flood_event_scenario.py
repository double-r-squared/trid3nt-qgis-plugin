"""Unit + integration tests for model_nws_flood_event_scenario (Case 3 — job-0229).

The Case 3 composer chains a live NWS active flood warning → MRMS observed
precip → SFINCS inundation into a 3-layer accumulation contract. These tests
exercise the deterministic surface (alert filtering, severity selection,
polygon-bbox extraction, candidate narrowing) directly, plus the full mocked
chain (mocked NWS GeoJSON + mocked MRMS fetch + mocked SFINCS workflow) and the
graceful no-warning degrade path.

Test plan (kickoff acceptance):

1. ``test_select_flood_warning_filters_to_flood_event_set`` — only Flood
   Warning / Flash Flood Warning features survive; watches + non-flood events
   are dropped.
2. ``test_select_flood_warning_picks_highest_severity`` — given mixed
   severities, the highest-severity polygon-bearing warning is selected.
3. ``test_select_flood_warning_index_selects_nth`` — ``warning_index`` selects
   the n-th warning in severity order; out-of-range → None.
4. ``test_select_flood_warning_skips_null_geometry`` — alerts with NULL
   geometry are not selectable (cannot anchor a model bbox).
5. ``test_extract_polygon_bbox_polygon_and_multipolygon`` — bbox extraction
   over Polygon + MultiPolygon coordinate rings.
6. ``test_narrow_candidates_by_state_ugc`` / ``..._by_bbox`` — candidate
   narrowing via UGC geocode + bbox intersection.
7. ``test_full_chain_returns_three_layer_contract`` — full mocked chain
   (NWS → MRMS → SFINCS) returns the {warning_polygon_layer, mrms_precip_layer,
   flood_depth_layer} contract with correct chain ordering.
8. ``test_chain_ordering_mrms_uses_warning_bbox`` — MRMS is fetched over the
   SELECTED warning polygon's bbox; SFINCS receives that bbox + the MRMS uri.
9. ``test_no_flood_warning_degrades_structured`` — no active flood warning →
   structured no-op listing what WAS active (not an exception).
10. ``test_nws_fetch_failure_degrades`` — upstream NWS failure → degrade (no
    loop/retry).
11. ``test_mrms_failure_degrades_with_selected_warning`` — MRMS fetch fails
    after a warning is selected → degrade but surface the warning.
12. ``test_wrapper_registered_workflow_dispatch`` — the LLM wrapper is
    registered with the workflow_dispatch / uncacheable metadata.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from grace2_agent.workflows.model_nws_flood_event_scenario import (
    Case3Error,
    FLOOD_WARNING_EVENT_TYPES,
    extract_polygon_bbox,
    model_nws_flood_event_scenario,
    select_flood_warning,
)
from grace2_agent.workflows.model_nws_flood_event_scenario import (
    _narrow_candidates,
    _accumulation_hours,
)
from grace2_contracts import new_ulid
from grace2_contracts.envelope import (
    AssessmentEnvelope,
    FloodMetrics,
    FloodPayload,
    Provenance,
    ResultLayer,
)
from grace2_contracts.execution import LayerURI


# --------------------------------------------------------------------------- #
# GeoJSON feature fixtures
# --------------------------------------------------------------------------- #

# Idaho-ish polygon (non-Florida geography for Case 3).
_IDAHO_RING = [
    [-116.30, 43.55],
    [-116.10, 43.55],
    [-116.10, 43.70],
    [-116.30, 43.70],
    [-116.30, 43.55],
]


def _feature(
    event: str,
    *,
    severity: str = "Severe",
    geometry: dict[str, Any] | None = "default",
    onset: str = "2026-06-09T12:00:00Z",
    area_desc: str = "Ada County, ID",
    ugc: list[str] | None = None,
    alert_id: str | None = None,
) -> dict[str, Any]:
    if geometry == "default":
        geometry = {"type": "Polygon", "coordinates": [_IDAHO_RING]}
    props: dict[str, Any] = {
        "event": event,
        "severity": severity,
        "headline": f"{event} for {area_desc}",
        "areaDesc": area_desc,
        "onset": onset,
        "expires": "2026-06-09T18:00:00Z",
        "senderName": "NWS Boise ID",
        "id": alert_id or f"urn:oid:{new_ulid()}",
    }
    if ugc is not None:
        props["geocode"] = {"UGC": ugc, "SAME": []}
    return {"type": "Feature", "properties": props, "geometry": geometry}


def _geojson(features: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


# --------------------------------------------------------------------------- #
# 1. Alert filtering — only the flood-warning event set survives
# --------------------------------------------------------------------------- #


def test_select_flood_warning_filters_to_flood_event_set() -> None:
    features = [
        _feature("Flood Warning"),
        _feature("Flash Flood Warning"),
        _feature("Flood Watch"),  # advisory — dropped
        _feature("Tornado Warning"),  # non-flood — dropped
        _feature("Heat Advisory"),  # non-flood — dropped
    ]
    selected, sorted_warnings = select_flood_warning(features)
    assert selected is not None
    assert selected["properties"]["event"] in FLOOD_WARNING_EVENT_TYPES
    # Only the two WARNING events qualify.
    assert len(sorted_warnings) == 2
    events = {f["properties"]["event"] for f in sorted_warnings}
    assert events == {"Flood Warning", "Flash Flood Warning"}


# --------------------------------------------------------------------------- #
# 2. Severity selection
# --------------------------------------------------------------------------- #


def test_select_flood_warning_picks_highest_severity() -> None:
    features = [
        _feature("Flood Warning", severity="Minor", alert_id="minor"),
        _feature("Flash Flood Warning", severity="Extreme", alert_id="extreme"),
        _feature("Flood Warning", severity="Moderate", alert_id="moderate"),
    ]
    selected, sorted_warnings = select_flood_warning(features)
    assert selected is not None
    assert selected["properties"]["id"] == "extreme"
    # Severity-sorted order: Extreme, Moderate, Minor.
    order = [f["properties"]["severity"] for f in sorted_warnings]
    assert order == ["Extreme", "Moderate", "Minor"]


def test_severity_recency_tiebreak() -> None:
    """Same severity → most-recent onset wins."""
    features = [
        _feature("Flood Warning", severity="Severe", onset="2026-06-09T08:00:00Z", alert_id="old"),
        _feature("Flood Warning", severity="Severe", onset="2026-06-09T14:00:00Z", alert_id="new"),
    ]
    selected, _ = select_flood_warning(features)
    assert selected is not None
    assert selected["properties"]["id"] == "new"


# --------------------------------------------------------------------------- #
# 3. warning_index selection
# --------------------------------------------------------------------------- #


def test_select_flood_warning_index_selects_nth() -> None:
    features = [
        _feature("Flood Warning", severity="Extreme", alert_id="0"),
        _feature("Flood Warning", severity="Severe", alert_id="1"),
        _feature("Flood Warning", severity="Minor", alert_id="2"),
    ]
    sel0, _ = select_flood_warning(features, warning_index=0)
    sel1, _ = select_flood_warning(features, warning_index=1)
    sel2, _ = select_flood_warning(features, warning_index=2)
    assert sel0["properties"]["id"] == "0"
    assert sel1["properties"]["id"] == "1"
    assert sel2["properties"]["id"] == "2"


def test_select_flood_warning_index_out_of_range_returns_none() -> None:
    features = [_feature("Flood Warning")]
    selected, sorted_warnings = select_flood_warning(features, warning_index=5)
    assert selected is None
    assert len(sorted_warnings) == 1  # the list is still returned for enumeration


def test_select_flood_warning_bad_index_type_raises() -> None:
    features = [_feature("Flood Warning")]
    with pytest.raises(Case3Error) as exc:
        select_flood_warning(features, warning_index="first")  # type: ignore[arg-type]
    assert exc.value.error_code == "CASE3_BAD_WARNING_INDEX"


# --------------------------------------------------------------------------- #
# 4. NULL-geometry alerts are not selectable
# --------------------------------------------------------------------------- #


def test_select_flood_warning_skips_null_geometry() -> None:
    features = [
        _feature("Flood Warning", severity="Extreme", geometry=None, alert_id="nullgeom"),
        _feature("Flood Warning", severity="Minor", alert_id="haspoly"),
    ]
    selected, sorted_warnings = select_flood_warning(features)
    assert selected is not None
    # The Extreme one has NULL geometry → skipped; the Minor polygon survives.
    assert selected["properties"]["id"] == "haspoly"
    assert len(sorted_warnings) == 1


def test_select_flood_warning_empty_features_returns_none() -> None:
    selected, sorted_warnings = select_flood_warning([])
    assert selected is None
    assert sorted_warnings == []


# --------------------------------------------------------------------------- #
# 5. Polygon bbox extraction
# --------------------------------------------------------------------------- #


def test_extract_polygon_bbox_polygon() -> None:
    feat = _feature("Flood Warning")
    bbox = extract_polygon_bbox(feat)
    assert bbox == pytest.approx((-116.30, 43.55, -116.10, 43.70))


def test_extract_polygon_bbox_multipolygon() -> None:
    feat = _feature(
        "Flood Warning",
        geometry={
            "type": "MultiPolygon",
            "coordinates": [
                [[[-116.5, 43.4], [-116.4, 43.4], [-116.4, 43.5], [-116.5, 43.4]]],
                [[[-116.1, 43.7], [-116.0, 43.7], [-116.0, 43.8], [-116.1, 43.7]]],
            ],
        },
    )
    bbox = extract_polygon_bbox(feat)
    # Union over both polygons.
    assert bbox == pytest.approx((-116.5, 43.4, -116.0, 43.8))


def test_extract_polygon_bbox_no_geometry_raises() -> None:
    feat = _feature("Flood Warning", geometry=None)
    with pytest.raises(Case3Error) as exc:
        extract_polygon_bbox(feat)
    assert exc.value.error_code == "CASE3_NO_GEOMETRY"


def test_extract_polygon_bbox_point_geometry_raises() -> None:
    feat = _feature("Flood Warning", geometry={"type": "Point", "coordinates": [-116.2, 43.6]})
    with pytest.raises(Case3Error) as exc:
        extract_polygon_bbox(feat)
    assert exc.value.error_code == "CASE3_NO_GEOMETRY"


# --------------------------------------------------------------------------- #
# 6. Candidate narrowing
# --------------------------------------------------------------------------- #


def test_narrow_candidates_by_state_ugc() -> None:
    features = [
        _feature("Flood Warning", ugc=["IDC001"], area_desc="Ada County, ID", alert_id="id"),
        _feature("Flood Warning", ugc=["FLC021"], area_desc="Lee County, FL", alert_id="fl"),
    ]
    narrowed = _narrow_candidates(features, bbox=None, state="ID")
    assert len(narrowed) == 1
    assert narrowed[0]["properties"]["id"] == "id"


def test_narrow_candidates_by_state_areadesc_fallback() -> None:
    # No UGC geocode — falls back to areaDesc substring.
    features = [
        _feature("Flood Warning", area_desc="Boise, ID", alert_id="id"),
        _feature("Flood Warning", area_desc="Houston, TX", alert_id="tx"),
    ]
    narrowed = _narrow_candidates(features, bbox=None, state="ID")
    assert [f["properties"]["id"] for f in narrowed] == ["id"]


def test_narrow_candidates_by_bbox() -> None:
    features = [
        # Idaho polygon — intersects the query bbox.
        _feature("Flood Warning", alert_id="inside"),
        # Florida polygon — outside.
        _feature(
            "Flood Warning",
            geometry={"type": "Polygon", "coordinates": [[[-81.9, 26.5], [-81.8, 26.5], [-81.8, 26.6], [-81.9, 26.5]]]},
            alert_id="outside",
        ),
    ]
    narrowed = _narrow_candidates(
        features, bbox=(-117.0, 43.0, -116.0, 44.0), state=None
    )
    assert [f["properties"]["id"] for f in narrowed] == ["inside"]


def test_narrow_candidates_none_returns_all() -> None:
    features = [_feature("Flood Warning"), _feature("Tornado Warning")]
    assert _narrow_candidates(features, bbox=None, state=None) == features


def test_accumulation_hours_parsing() -> None:
    assert _accumulation_hours("24h") == 24
    assert _accumulation_hours("6h") == 6
    assert _accumulation_hours("01H") == 1
    assert _accumulation_hours("72h") == 72
    assert _accumulation_hours("garbage") == 24  # defensive default


# --------------------------------------------------------------------------- #
# Mock helpers for the full chain
# --------------------------------------------------------------------------- #


def _mock_alerts_layer() -> LayerURI:
    return LayerURI(
        layer_id="nws-conus-actual-Flash_Flood_Warning-Flood_Warning",
        name="NWS Active Alerts — CONUS (flood)",
        layer_type="vector",
        uri="gs://test-cache/cache/dynamic-1h/nws_alerts_conus/abc.fgb",
        style_preset="nws_alerts",
        role="primary",
        units=None,
    )


def _mock_mrms_layer() -> LayerURI:
    return LayerURI(
        layer_id="mrms-qpe-24H-test",
        name="MRMS QPE 24H (Pass2 gauge-corrected, mm; valid_time=latest)",
        layer_type="raster",
        uri="gs://test-cache/cache/dynamic-1h/mrms_qpe/def.tif",
        style_preset="precipitation_mm",
        role="primary",
        units="mm",
        bbox=(-116.30, 43.55, -116.10, 43.70),
    )


def _success_envelope(bbox: tuple[float, float, float, float]) -> AssessmentEnvelope:
    run_id = new_ulid()
    return AssessmentEnvelope(
        envelope_id=new_ulid(),
        project_id=new_ulid(),
        session_id=new_ulid(),
        envelope_type="modeled",
        hazard_type="flood",
        workflow_name="model_flood_scenario",
        bbox=bbox,
        crs="EPSG:4326",
        forcing=None,
        layers=[
            ResultLayer(
                layer_id=f"flood-depth-peak-{run_id}",
                name="Flood Depth (peak)",
                layer_type="raster",
                uri=f"https://qgis.example/wms?LAYERS=flood-depth-peak-{run_id}",
                style_preset="continuous_flood_depth",
                role="primary",
                units="meters",
            )
        ],
        provenance=Provenance(data_sources=[]),
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        solver_run_ids=[run_id],
        flood=FloodPayload(
            metrics=FloodMetrics(
                flooded_area_km2=12.5,
                max_depth_m=1.8,
                mean_depth_m=0.4,
                p95_depth_m=1.2,
                solver_version="sfincs-v2.3.3",
                grid_resolution_m=30.0,
                simulation_duration_hours=24,
            )
        ),
    )


def _failed_envelope(bbox: tuple[float, float, float, float], code: str) -> AssessmentEnvelope:
    now = datetime.now(timezone.utc)
    return AssessmentEnvelope(
        envelope_id=new_ulid(),
        project_id=new_ulid(),
        session_id=new_ulid(),
        envelope_type="modeled",
        hazard_type="flood",
        workflow_name="model_flood_scenario",
        bbox=bbox,
        crs="EPSG:4326",
        forcing=None,
        layers=[],
        provenance=Provenance(data_sources=[]),
        created_at=now,
        completed_at=now,
        solver_run_ids=[],
        flood=FloodPayload(
            metrics=FloodMetrics(
                flooded_area_km2=0.0,
                max_depth_m=0.0,
                mean_depth_m=0.0,
                p95_depth_m=0.0,
                solver_version=f"failed:{code}",
                grid_resolution_m=30.0,
                simulation_duration_hours=24,
            )
        ),
    )


_MOD = "grace2_agent.workflows.model_nws_flood_event_scenario"


# --------------------------------------------------------------------------- #
# 7. Full chain — 3-layer accumulation contract + chain ordering
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_full_chain_returns_three_layer_contract() -> None:
    features = [
        _feature("Flood Warning", severity="Severe", alert_id="picked"),
        _feature("Flood Watch", severity="Extreme"),  # watch — never selected
    ]

    captured: dict[str, Any] = {}

    async def _mock_flood(**kwargs: Any) -> AssessmentEnvelope:
        captured["flood_kwargs"] = kwargs
        return _success_envelope(kwargs["bbox"])

    def _mock_mrms(*, bbox: Any, accumulation: str, **_: Any) -> LayerURI:
        captured["mrms_bbox"] = bbox
        captured["mrms_accumulation"] = accumulation
        return _mock_mrms_layer()

    with (
        patch(f"{_MOD}.fetch_nws_alerts_conus", return_value=_mock_alerts_layer()),
        patch(f"{_MOD}._fetch_nws_conus_geojson", return_value=_geojson(features)),
        patch(f"{_MOD}.fetch_mrms_qpe", side_effect=_mock_mrms),
        patch(f"{_MOD}.model_flood_scenario", side_effect=_mock_flood),
    ):
        result = await model_nws_flood_event_scenario(accumulation="24h")

    assert result["status"] == "ok"
    # The 3-layer accumulation contract.
    assert result["warning_polygon_layer"] is not None
    assert result["mrms_precip_layer"] is not None
    assert result["flood_depth_layer"] is not None
    # Warning polygon is a vector layer rendered as context.
    assert result["warning_polygon_layer"]["layer_type"] == "vector"
    assert result["warning_polygon_layer"]["role"] == "context"
    # MRMS precip is a raster in mm.
    assert result["mrms_precip_layer"]["units"] == "mm"
    # Flood depth carries the warning bbox for zoom-to.
    assert result["flood_depth_layer"]["layer_type"] == "raster"
    # The selected warning is the Flood Warning, not the Watch.
    assert result["selected_warning"]["event"] == "Flood Warning"
    assert result["selected_warning"]["id"] == "picked"
    # Envelope metrics flow through for narration.
    assert result["flood_envelope"]["flood"]["metrics"]["max_depth_m"] == 1.8


@pytest.mark.asyncio
async def test_chain_ordering_mrms_uses_warning_bbox() -> None:
    """MRMS is fetched over the selected warning's polygon bbox; SFINCS gets that
    bbox + the MRMS uri (chain ordering)."""
    features = [_feature("Flash Flood Warning", severity="Extreme")]
    captured: dict[str, Any] = {}

    async def _mock_flood(**kwargs: Any) -> AssessmentEnvelope:
        captured["flood_kwargs"] = kwargs
        return _success_envelope(kwargs["bbox"])

    def _mock_mrms(*, bbox: Any, accumulation: str, **_: Any) -> LayerURI:
        captured["mrms_bbox"] = bbox
        return _mock_mrms_layer()

    with (
        patch(f"{_MOD}.fetch_nws_alerts_conus", return_value=_mock_alerts_layer()),
        patch(f"{_MOD}._fetch_nws_conus_geojson", return_value=_geojson(features)),
        patch(f"{_MOD}.fetch_mrms_qpe", side_effect=_mock_mrms),
        patch(f"{_MOD}.model_flood_scenario", side_effect=_mock_flood),
    ):
        result = await model_nws_flood_event_scenario(accumulation="24h")

    expected_bbox = (-116.30, 43.55, -116.10, 43.70)
    # MRMS fetched over the warning polygon bbox.
    assert tuple(captured["mrms_bbox"]) == pytest.approx(expected_bbox)
    # SFINCS receives the SAME warning bbox + the MRMS uri as forcing.
    fk = captured["flood_kwargs"]
    assert tuple(fk["bbox"]) == pytest.approx(expected_bbox)
    assert fk["forcing_raster_uri"] == _mock_mrms_layer().uri
    # duration_hr derived from accumulation "24h" → 24.
    assert fk["duration_hr"] == 24
    assert tuple(result["warning_bbox"]) == pytest.approx(expected_bbox)


@pytest.mark.asyncio
async def test_warning_index_threads_through_to_selection() -> None:
    features = [
        _feature("Flood Warning", severity="Extreme", alert_id="0"),
        _feature("Flood Warning", severity="Minor", alert_id="1"),
    ]

    async def _mock_flood(**kwargs: Any) -> AssessmentEnvelope:
        return _success_envelope(kwargs["bbox"])

    with (
        patch(f"{_MOD}.fetch_nws_alerts_conus", return_value=_mock_alerts_layer()),
        patch(f"{_MOD}._fetch_nws_conus_geojson", return_value=_geojson(features)),
        patch(f"{_MOD}.fetch_mrms_qpe", return_value=_mock_mrms_layer()),
        patch(f"{_MOD}.model_flood_scenario", side_effect=_mock_flood),
    ):
        result = await model_nws_flood_event_scenario(warning_index=1)

    assert result["status"] == "ok"
    assert result["selected_warning"]["id"] == "1"
    assert result["flood_warning_count"] == 2


# --------------------------------------------------------------------------- #
# 8. Failed SFINCS → 3-layer contract with flood_depth_layer None
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_failed_sfincs_returns_ok_with_null_flood_layer() -> None:
    features = [_feature("Flood Warning", severity="Severe")]

    async def _mock_flood(**kwargs: Any) -> AssessmentEnvelope:
        return _failed_envelope(kwargs["bbox"], "LULC_MAPPING_MISMATCH")

    with (
        patch(f"{_MOD}.fetch_nws_alerts_conus", return_value=_mock_alerts_layer()),
        patch(f"{_MOD}._fetch_nws_conus_geojson", return_value=_geojson(features)),
        patch(f"{_MOD}.fetch_mrms_qpe", return_value=_mock_mrms_layer()),
        patch(f"{_MOD}.model_flood_scenario", side_effect=_mock_flood),
    ):
        result = await model_nws_flood_event_scenario()

    # Status is still ok (warning + precip rendered); flood layer is None and
    # the summary narrates the failure honestly (Invariant 7).
    assert result["status"] == "ok"
    assert result["warning_polygon_layer"] is not None
    assert result["mrms_precip_layer"] is not None
    assert result["flood_depth_layer"] is None
    assert "did not complete" in result["summary_text"]
    assert "LULC_MAPPING_MISMATCH" in result["summary_text"]


# --------------------------------------------------------------------------- #
# 9. No-warning degrade path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_flood_warning_degrades_structured() -> None:
    # Only non-flood alerts are active.
    features = [
        _feature("Tornado Warning", severity="Extreme"),
        _feature("Heat Advisory", severity="Minor"),
        _feature("Heat Advisory", severity="Minor"),
        _feature("Flood Watch", severity="Moderate"),  # watch, not warning
    ]

    mrms_called = {"count": 0}

    def _mrms_spy(**_: Any) -> LayerURI:
        mrms_called["count"] += 1
        return _mock_mrms_layer()

    with (
        patch(f"{_MOD}.fetch_nws_alerts_conus", return_value=_mock_alerts_layer()),
        patch(f"{_MOD}._fetch_nws_conus_geojson", return_value=_geojson(features)),
        patch(f"{_MOD}.fetch_mrms_qpe", side_effect=_mrms_spy),
        patch(f"{_MOD}.model_flood_scenario") as mock_flood,
    ):
        result = await model_nws_flood_event_scenario(state="ID")

    # Structured no-op — never raised.
    assert result["status"] == "no_active_flood_warning"
    assert result["reason_code"] == "NO_ACTIVE_FLOOD_WARNING"
    assert result["mrms_precip_layer"] is None
    assert result["flood_depth_layer"] is None
    # The degrade lists what WAS active.
    assert result["active_event_counts"]["Tornado Warning"] == 1
    assert result["active_event_counts"]["Heat Advisory"] == 2
    assert result["flood_warning_count"] == 0
    # The MRMS + SFINCS steps were NEVER reached (no fabricated flood).
    assert mrms_called["count"] == 0
    mock_flood.assert_not_called()
    # The warning-polygon layer is still surfaced so the user sees active alerts.
    assert result["warning_polygon_layer"] is not None
    assert "No active Flood Warning" in result["summary_text"]


@pytest.mark.asyncio
async def test_no_alerts_at_all_degrades_quiet() -> None:
    with (
        patch(f"{_MOD}.fetch_nws_alerts_conus", return_value=_mock_alerts_layer()),
        patch(f"{_MOD}._fetch_nws_conus_geojson", return_value=_geojson([])),
        patch(f"{_MOD}.fetch_mrms_qpe") as mock_mrms,
        patch(f"{_MOD}.model_flood_scenario") as mock_flood,
    ):
        result = await model_nws_flood_event_scenario(state="ID")

    assert result["status"] == "no_active_flood_warning"
    assert result["active_event_counts"] == {}
    assert "weather is currently quiet" in result["summary_text"]
    mock_mrms.assert_not_called()
    mock_flood.assert_not_called()


# --------------------------------------------------------------------------- #
# 10. NWS upstream failure degrades (no loop/retry)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_nws_fetch_failure_degrades() -> None:
    from grace2_agent.tools.fetch_nws_alerts_conus import NWSConusUpstreamError

    geojson_calls = {"count": 0}

    def _raise(*_a: Any, **_k: Any) -> Any:
        geojson_calls["count"] += 1
        raise NWSConusUpstreamError("NWS returned HTTP 503")

    with (
        patch(f"{_MOD}.fetch_nws_alerts_conus", return_value=_mock_alerts_layer()),
        patch(f"{_MOD}._fetch_nws_conus_geojson", side_effect=_raise),
        patch(f"{_MOD}.fetch_mrms_qpe") as mock_mrms,
        patch(f"{_MOD}.model_flood_scenario") as mock_flood,
    ):
        result = await model_nws_flood_event_scenario()

    assert result["status"] == "no_active_flood_warning"
    assert result["reason_code"] == "NWS_CONUS_UPSTREAM_ERROR"
    # The composer did NOT loop/retry the NWS API (kickoff: do not loop/retry).
    assert geojson_calls["count"] == 1
    mock_mrms.assert_not_called()
    mock_flood.assert_not_called()


# --------------------------------------------------------------------------- #
# 11. MRMS fetch failure after a warning is selected
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_mrms_failure_degrades_with_selected_warning() -> None:
    from grace2_agent.tools.fetch_mrms_qpe import MRMSQPEUpstreamError

    features = [_feature("Flood Warning", severity="Severe", alert_id="picked")]

    with (
        patch(f"{_MOD}.fetch_nws_alerts_conus", return_value=_mock_alerts_layer()),
        patch(f"{_MOD}._fetch_nws_conus_geojson", return_value=_geojson(features)),
        patch(f"{_MOD}.fetch_mrms_qpe", side_effect=MRMSQPEUpstreamError("S3 down")),
        patch(f"{_MOD}.model_flood_scenario") as mock_flood,
    ):
        result = await model_nws_flood_event_scenario()

    assert result["status"] == "mrms_fetch_failed"
    # A warning WAS selected and surfaced even though precip failed.
    assert result["selected_warning"]["id"] == "picked"
    assert result["warning_polygon_layer"] is not None
    assert result["mrms_precip_layer"] is None
    assert result["flood_depth_layer"] is None
    # SFINCS never ran (no precip forcing available).
    mock_flood.assert_not_called()


# --------------------------------------------------------------------------- #
# 12. LLM wrapper registration
# --------------------------------------------------------------------------- #


def test_wrapper_registered_workflow_dispatch() -> None:
    # Importing the workflows package fires the @register_tool decorators.
    import grace2_agent.workflows  # noqa: F401
    from grace2_agent.tools import TOOL_REGISTRY

    assert "run_model_nws_flood_event_scenario" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["run_model_nws_flood_event_scenario"].metadata
    assert meta.source_class == "workflow_dispatch"
    assert meta.cacheable is False
    assert meta.ttl_class == "live-no-cache"


@pytest.mark.asyncio
async def test_wrapper_forwards_to_composer() -> None:
    """The registered wrapper forwards verbatim to the composer body."""
    from grace2_agent.workflows.model_nws_flood_event_scenario import (
        run_model_nws_flood_event_scenario,
    )

    features = [_feature("Flood Warning", severity="Severe")]

    async def _mock_flood(**kwargs: Any) -> AssessmentEnvelope:
        return _success_envelope(kwargs["bbox"])

    with (
        patch(f"{_MOD}.fetch_nws_alerts_conus", return_value=_mock_alerts_layer()),
        patch(f"{_MOD}._fetch_nws_conus_geojson", return_value=_geojson(features)),
        patch(f"{_MOD}.fetch_mrms_qpe", return_value=_mock_mrms_layer()),
        patch(f"{_MOD}.model_flood_scenario", side_effect=_mock_flood),
    ):
        result = await run_model_nws_flood_event_scenario(
            state="ID", accumulation="24h"
        )

    assert result["status"] == "ok"
    assert result["flood_depth_layer"] is not None
