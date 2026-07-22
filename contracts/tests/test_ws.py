"""Round-trip + negative tests for the Appendix A WebSocket protocol (ws.py).

Every message type listed in ``ws.ALL_PAYLOADS`` (Appendix A.3, A.4, A.4b) is
exercised: a real instance is built, dumped to JSON via the Envelope, parsed
back, and re-dumped — both passes must be byte-identical (idempotent).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from trid3nt_contracts import ws
from trid3nt_contracts.chart_contracts import ChartEmissionPayload
from trid3nt_contracts.common import GraceModel, new_ulid
from trid3nt_contracts.region_choice import (
    RegionCandidate,
    RegionChoiceProvidedEnvelopePayload,
    RegionChoiceRequestEnvelopePayload,
)
from trid3nt_contracts.sandbox_contracts import (
    CodeExecRequestPayload,
    CodeExecResultPayload,
)

# Re-export the secrets payloads onto ``ws`` so the inline lambdas below stay
# tidy; the per-module accessors are also still exposed for direct import.
ws.SecretAddEnvelopePayload = ws.SecretAddEnvelopePayload if hasattr(ws, "SecretAddEnvelopePayload") else __import__(
    "trid3nt_contracts.secrets", fromlist=["SecretAddEnvelopePayload"]
).SecretAddEnvelopePayload
ws.SecretRevokeEnvelopePayload = ws.SecretRevokeEnvelopePayload if hasattr(ws, "SecretRevokeEnvelopePayload") else __import__(
    "trid3nt_contracts.secrets", fromlist=["SecretRevokeEnvelopePayload"]
).SecretRevokeEnvelopePayload
ws.SecretsListEnvelopePayload = ws.SecretsListEnvelopePayload if hasattr(ws, "SecretsListEnvelopePayload") else __import__(
    "trid3nt_contracts.secrets", fromlist=["SecretsListEnvelopePayload"]
).SecretsListEnvelopePayload
ws.CredentialRequestEnvelopePayload = ws.CredentialRequestEnvelopePayload if hasattr(ws, "CredentialRequestEnvelopePayload") else __import__(
    "trid3nt_contracts.secrets", fromlist=["CredentialRequestEnvelopePayload"]
).CredentialRequestEnvelopePayload
ws.CredentialProvidedEnvelopePayload = ws.CredentialProvidedEnvelopePayload if hasattr(ws, "CredentialProvidedEnvelopePayload") else __import__(
    "trid3nt_contracts.secrets", fromlist=["CredentialProvidedEnvelopePayload"]
).CredentialProvidedEnvelopePayload


def _wrap(payload: GraceModel, session_id: str) -> ws.Envelope:
    msg_type = getattr(payload, "MESSAGE_TYPE")
    return ws.Envelope[type(payload)](
        type=msg_type,
        session_id=session_id,
        payload=payload,
    )


def _roundtrip_idempotent(envelope: ws.Envelope) -> dict[str, Any]:
    """Serialize -> JSON text -> dict -> re-validate -> serialize. Both passes
    must match byte-for-byte.
    """
    dumped_a = envelope.model_dump(mode="json")
    text_a = json.dumps(dumped_a, sort_keys=True)
    # Real JSON round-trip via text
    loaded = json.loads(text_a)
    envelope_b = type(envelope).model_validate(loaded)
    dumped_b = envelope_b.model_dump(mode="json")
    text_b = json.dumps(dumped_b, sort_keys=True)
    assert text_a == text_b, "JSON round-trip not idempotent"
    return dumped_a


# --------------------------------------------------------------------------- #
# Client -> Agent (A.3)
# --------------------------------------------------------------------------- #


def test_user_message_default_research_mode(session_id: str) -> None:
    """A.3 user-message with the FR-WC-15 research_mode amendment, default value."""
    payload = ws.UserMessagePayload(text="Model the flooding from Hurricane Ian in Fort Myers")
    dumped = _roundtrip_idempotent(_wrap(payload, session_id))
    assert dumped["type"] == "user-message"
    assert dumped["payload"]["research_mode"] == "research"


def test_user_message_deep_research_mode(session_id: str) -> None:
    payload = ws.UserMessagePayload(
        text="Run a deep sweep on the 2024 atmospheric river sequence",
        research_mode="deep_research",
    )
    dumped = _roundtrip_idempotent(_wrap(payload, session_id))
    assert dumped["payload"]["research_mode"] == "deep_research"


def test_user_message_unknown_research_mode_rejected() -> None:
    with pytest.raises(ValidationError):
        ws.UserMessagePayload(text="hi", research_mode="extra_deep")  # type: ignore[arg-type]


def test_cancel_message(session_id: str) -> None:
    payload = ws.CancelPayload(reason="user-requested")
    dumped = _roundtrip_idempotent(_wrap(payload, session_id))
    assert dumped["type"] == "cancel"


def test_confirm_response(session_id: str) -> None:
    payload = ws.ConfirmResponsePayload(request_id=new_ulid(), approved=True)
    _roundtrip_idempotent(_wrap(payload, session_id))


def test_session_resume_empty_payload(session_id: str) -> None:
    payload = ws.SessionResumePayload()
    dumped = _roundtrip_idempotent(_wrap(payload, session_id))
    # payload object is always present, never null. job-CASE-AUTHORITY adds an
    # OPTIONAL case_id (default None) — an older/empty resume carries it as null.
    assert dumped["payload"] == {"case_id": None}


def test_session_resume_carries_client_case_id(session_id: str) -> None:
    """job-CASE-AUTHORITY: the resume stamps the client's CURRENT Case so the
    server re-binds its active-Case pointer to it on reconnect (the SNAP fix).
    """
    case_id = new_ulid()
    payload = ws.SessionResumePayload(case_id=case_id)
    dumped = _roundtrip_idempotent(_wrap(payload, session_id))
    assert dumped["type"] == "session-resume"
    assert dumped["payload"]["case_id"] == case_id
    # parses back to the same value (str | None field).
    reparsed = ws.SessionResumePayload.model_validate(dumped["payload"])
    assert reparsed.case_id == case_id


def test_session_resume_case_id_defaults_none() -> None:
    """Older client (no stamp) leaves case_id None — server keeps its pointer."""
    assert ws.SessionResumePayload().case_id is None


def test_user_message_carries_client_case_id(session_id: str) -> None:
    """job-CASE-AUTHORITY: user-message stamps the client's CURRENT Case so the
    turn binds to it (e.g. a 'resize bbox' runs in the user's actual Case), not
    a stale server pointer. Same field name/shape as SessionResumePayload.
    """
    case_id = new_ulid()
    payload = ws.UserMessagePayload(text="resize the bbox a bit larger", case_id=case_id)
    dumped = _roundtrip_idempotent(_wrap(payload, session_id))
    assert dumped["type"] == "user-message"
    assert dumped["payload"]["case_id"] == case_id
    reparsed = ws.UserMessagePayload.model_validate(dumped["payload"])
    assert reparsed.case_id == case_id


def test_user_message_case_id_defaults_none() -> None:
    """Older client (no stamp) leaves case_id None — server falls back to its
    own active-Case pointer (prior behavior preserved)."""
    assert ws.UserMessagePayload(text="hi").case_id is None


def test_case_id_field_identical_shape_on_both_payloads() -> None:
    """PINNED CONTRACT: case_id is the SAME name + shape (str | None, default
    None, optional) on BOTH SessionResumePayload and UserMessagePayload."""
    sr_field = ws.SessionResumePayload.model_fields["case_id"]
    um_field = ws.UserMessagePayload.model_fields["case_id"]
    assert sr_field.annotation == um_field.annotation
    assert sr_field.default is None and um_field.default is None
    assert not sr_field.is_required() and not um_field.is_required()


# --------------------------------------------------------------------------- #
# Client -> Agent user-input responses (A.4b)
# --------------------------------------------------------------------------- #


def test_spatial_input_response_point(session_id: str) -> None:
    payload = ws.SpatialInputResponsePayload(
        request_id=new_ulid(),
        geometry_type="point",
        coordinates=[-82.0, 26.5],
    )
    _roundtrip_idempotent(_wrap(payload, session_id))


def test_spatial_input_response_cancelled(session_id: str) -> None:
    payload = ws.SpatialInputResponsePayload(
        request_id=new_ulid(),
        cancelled=True,
    )
    dumped = _roundtrip_idempotent(_wrap(payload, session_id))
    assert dumped["payload"]["cancelled"] is True


# --- FR-WC-16 urban vector-draw (vector_draw mode) -------------------------- #


def _vector_draw_feature_collection() -> dict[str, Any]:
    """A drawn FeatureCollection carrying an AOI polygon + a wall + a flap gate
    + a single point — exercising every ``role`` and per-segment barrier tag."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-85.31, 35.04],
                            [-85.30, 35.04],
                            [-85.30, 35.05],
                            [-85.31, 35.05],
                            [-85.31, 35.04],
                        ]
                    ],
                },
                "properties": {"role": "aoi"},
            },
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-85.305, 35.041], [-85.305, 35.048]],
                },
                "properties": {"role": "barrier", "barrier_type": "wall"},
            },
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-85.308, 35.043], [-85.302, 35.043]],
                },
                "properties": {
                    "role": "barrier",
                    "barrier_type": "flap_gate",
                    "flap_direction": "out",
                    "protected_side": "left",
                },
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-85.306, 35.045]},
                "properties": {"role": "point"},
            },
        ],
    }


def test_spatial_input_response_vector_draw_roundtrips(session_id: str) -> None:
    """FR-WC-16: a vector_draw reply carrying a role-tagged FeatureCollection
    with per-segment barrier tags + flap direction serializes/deserializes
    cleanly through the envelope."""
    fc = _vector_draw_feature_collection()
    payload = ws.SpatialInputResponsePayload(
        request_id=new_ulid(),
        geometry_type="vector_draw",
        features=fc,
    )
    dumped = _roundtrip_idempotent(_wrap(payload, session_id))
    out = dumped["payload"]
    assert out["geometry_type"] == "vector_draw"
    assert out["coordinates"] is None
    roles = [f["properties"]["role"] for f in out["features"]["features"]]
    assert roles == ["aoi", "barrier", "barrier", "point"]
    # The two barrier segments preserve their distinct tags + flap direction.
    barriers = [
        f for f in out["features"]["features"] if f["properties"]["role"] == "barrier"
    ]
    tags = [b["properties"]["barrier_type"] for b in barriers]
    assert tags == ["wall", "flap_gate"]
    flap = next(b for b in barriers if b["properties"]["barrier_type"] == "flap_gate")
    assert flap["properties"]["flap_direction"] == "out"
    assert flap["properties"]["protected_side"] == "left"


def test_spatial_input_response_barriers_feed_swmm_contract(session_id: str) -> None:
    """The ``role == "barrier"`` subset of a vector_draw reply is field-for-field
    the tagged-LineString FeatureCollection SWMMRunArgs.barriers accepts — i.e.
    the drawn result round-trips straight into the urban engine seam."""
    from trid3nt_contracts.swmm_contracts import SWMMRunArgs

    fc = _vector_draw_feature_collection()
    barrier_fc = {
        "type": "FeatureCollection",
        "features": [
            f for f in fc["features"] if f["properties"].get("role") == "barrier"
        ],
    }
    # Construct the urban-engine args directly from the drawn barriers; the
    # swmm_contracts validator must accept them without translation.
    args = SWMMRunArgs(bbox=(-85.31, 35.04, -85.30, 35.05), barriers=barrier_fc)
    assert args.barriers is not None
    assert {
        feat["properties"]["barrier_type"] for feat in args.barriers["features"]
    } == {"wall", "flap_gate"}


def test_spatial_input_response_bad_role_rejected() -> None:
    """An unknown ``role`` is a defect — the structural validator rejects it."""
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-85.3, 35.0]},
                "properties": {"role": "landmark"},  # not in {aoi, barrier, point}
            }
        ],
    }
    with pytest.raises(ValidationError):
        ws.SpatialInputResponsePayload(
            request_id=new_ulid(), geometry_type="vector_draw", features=fc
        )


def test_spatial_input_response_bad_barrier_type_rejected() -> None:
    """A barrier LineString tagged with an unknown barrier_type is rejected."""
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-85.3, 35.0], [-85.2, 35.0]],
                },
                "properties": {"role": "barrier", "barrier_type": "moat"},
            }
        ],
    }
    with pytest.raises(ValidationError):
        ws.SpatialInputResponsePayload(
            request_id=new_ulid(), geometry_type="vector_draw", features=fc
        )


def test_spatial_input_response_barrier_must_be_linestring_rejected() -> None:
    """A ``role == "barrier"`` feature with a non-LineString geometry is rejected."""
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-85.3, 35.0]},
                "properties": {"role": "barrier", "barrier_type": "wall"},
            }
        ],
    }
    with pytest.raises(ValidationError):
        ws.SpatialInputResponsePayload(
            request_id=new_ulid(), geometry_type="vector_draw", features=fc
        )


def test_disambiguation_response(session_id: str) -> None:
    payload = ws.DisambiguationResponsePayload(request_id=new_ulid(), candidate_id="cand-1")
    _roundtrip_idempotent(_wrap(payload, session_id))


def test_clarification_response(session_id: str) -> None:
    payload = ws.ClarificationResponsePayload(request_id=new_ulid(), option_id="opt-a")
    _roundtrip_idempotent(_wrap(payload, session_id))


# --------------------------------------------------------------------------- #
# Agent -> Client (A.4)
# --------------------------------------------------------------------------- #


def test_agent_message_chunk(session_id: str) -> None:
    payload = ws.AgentMessageChunkPayload(message_id=new_ulid(), delta="The peak depth is ", done=False)
    _roundtrip_idempotent(_wrap(payload, session_id))


def test_tool_call_start(session_id: str) -> None:
    payload = ws.ToolCallStartPayload(
        call_id=new_ulid(),
        step_id=new_ulid(),
        tool_name="run_storm_surge_flood",
        tool_category="workflow",
        params={"location": "Fort Myers, FL"},
    )
    _roundtrip_idempotent(_wrap(payload, session_id))


def test_tool_call_progress(session_id: str) -> None:
    payload = ws.ToolCallProgressPayload(call_id=new_ulid(), percent=42, status="running solver")
    _roundtrip_idempotent(_wrap(payload, session_id))


def test_tool_call_progress_percent_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        ws.ToolCallProgressPayload(call_id=new_ulid(), percent=200)


def test_tool_call_complete_metrics_carried_as_dict(session_id: str) -> None:
    payload = ws.ToolCallCompletePayload(
        call_id=new_ulid(),
        result_summary="Peak depth 3.2 m over 18 km^2",
        result_uri="gs://trid3nt/runs/01HX/result.cog.tif",
        metrics={"flooded_area_km2": 18.4, "max_depth_m": 3.2},
    )
    dumped = _roundtrip_idempotent(_wrap(payload, session_id))
    # Invariant 1: numbers cited by the narrative live in `metrics`, not free text
    assert "flooded_area_km2" in dumped["payload"]["metrics"]


def test_tool_call_failed(session_id: str) -> None:
    payload = ws.ToolCallFailedPayload(
        call_id=new_ulid(),
        error_code="DEM_SOURCE_UNAVAILABLE",
        message="USGS 3DEP timed out",
        retryable=True,
    )
    _roundtrip_idempotent(_wrap(payload, session_id))


def test_pipeline_state_cancelled_is_distinct_terminal(session_id: str) -> None:
    """Invariant 8: cancelled must be a distinct PipelineStepState, not failed."""
    payload = ws.PipelineStatePayload(
        pipeline_id=new_ulid(),
        steps=[
            ws.PipelineStep(step_id=new_ulid(), name="fetch DEM", tool_name="fetch_dem", state="complete"),
            ws.PipelineStep(step_id=new_ulid(), name="run solver", tool_name="run_solver", state="cancelled"),
        ],
    )
    dumped = _roundtrip_idempotent(_wrap(payload, session_id))
    states = [s["state"] for s in dumped["payload"]["steps"]]
    assert "cancelled" in states and "failed" not in states


def test_pipeline_state_invalid_step_state_rejected() -> None:
    with pytest.raises(ValidationError):
        ws.PipelineStep(step_id=new_ulid(), name="x", tool_name="x", state="aborted")  # type: ignore[arg-type]


def test_pipeline_step_duration_ms_wire_roundtrip(session_id: str) -> None:
    """job-0264: duration_ms is carried on the pipeline-state wire shape.

    Optional + ge=0; defaults to None for non-terminal steps and round-trips
    through the envelope serialization unchanged when populated.
    """
    payload = ws.PipelineStatePayload(
        pipeline_id=new_ulid(),
        steps=[
            ws.PipelineStep(step_id=new_ulid(), name="fetch DEM", tool_name="fetch_dem", state="running"),
            ws.PipelineStep(
                step_id=new_ulid(),
                name="run solver",
                tool_name="run_solver",
                state="complete",
                duration_ms=154_000,
            ),
        ],
    )
    dumped = _roundtrip_idempotent(_wrap(payload, session_id))
    steps = dumped["payload"]["steps"]
    assert steps[0]["duration_ms"] is None
    assert steps[1]["duration_ms"] == 154_000


def test_pipeline_step_duration_ms_rejects_negative() -> None:
    """job-0264: Field(ge=0) rejects negative durations on the wire shape."""
    with pytest.raises(ValidationError):
        ws.PipelineStep(
            step_id=new_ulid(), name="x", tool_name="x", state="complete", duration_ms=-1
        )


# --- two-card sim observability (task-149) ---------------------------------- #


def test_pipeline_step_role_defaults_to_tool_back_compat() -> None:
    """task-149: a minimally-built PipelineStep is an on-box tool card with no
    Batch binding — proving the new fields keep every existing payload identical.
    """
    step = ws.PipelineStep(
        step_id=new_ulid(), name="fetch DEM", tool_name="fetch_dem", state="running"
    )
    assert step.role == "tool"
    assert step.batch_job_id is None
    assert step.batch_status is None


def test_pipeline_step_compute_card_carries_batch_binding(session_id: str) -> None:
    """task-149: a ``role="compute"`` off-box solver card carries the Batch
    jobId + last DescribeJobs status, and round-trips through the envelope.
    """
    payload = ws.PipelineStatePayload(
        pipeline_id=new_ulid(),
        steps=[
            ws.PipelineStep(
                step_id=new_ulid(), name="fetch DEM", tool_name="fetch_dem", state="complete"
            ),
            ws.PipelineStep(
                step_id=new_ulid(),
                name="run SFINCS",
                tool_name="run_solver",
                state="running",
                role="compute",
                batch_job_id="a1b2c3d4-0000-1111-2222-333344445555",
                batch_status="RUNNING",
            ),
        ],
    )
    dumped = _roundtrip_idempotent(_wrap(payload, session_id))
    steps = dumped["payload"]["steps"]
    assert steps[0]["role"] == "tool"
    assert steps[0]["batch_job_id"] is None
    assert steps[1]["role"] == "compute"
    assert steps[1]["batch_job_id"] == "a1b2c3d4-0000-1111-2222-333344445555"
    assert steps[1]["batch_status"] == "RUNNING"


def test_pipeline_step_rejects_unknown_role() -> None:
    """task-149: role is a closed Literal — only ``tool``/``compute``."""
    with pytest.raises(ValidationError):
        ws.PipelineStep(
            step_id=new_ulid(),
            name="x",
            tool_name="x",
            state="running",
            role="solver",  # type: ignore[arg-type]
        )


def test_solve_progress_phase_defaults_none_and_carries_batch_status() -> None:
    """task-149: SolveProgressPayload.phase defaults None (back-compat) and,
    when populated, carries the DescribeJobs status verbatim.
    """
    minimal = ws.SolveProgressPayload(run_id=new_ulid(), solver="sfincs", elapsed_seconds=1.0)
    assert minimal.phase is None
    populated = ws.SolveProgressPayload(
        run_id=new_ulid(), solver="sfincs", elapsed_seconds=42.5, phase="STARTING"
    )
    assert populated.phase == "STARTING"


# --- nested sub-step timeline (task-168) ------------------------------------ #


def test_pipeline_step_substep_fields_default_none_back_compat() -> None:
    """task-168: a minimally-built PipelineStep carries no parent link and no
    breadcrumb, proving the four new fields keep every existing payload identical.
    """
    step = ws.PipelineStep(
        step_id=new_ulid(), name="fetch DEM", tool_name="fetch_dem", state="running"
    )
    assert step.parent_step_id is None
    assert step.substep_label is None
    assert step.substep_index is None
    assert step.substep_total is None


def test_pipeline_step_child_carries_parent_step_id(session_id: str) -> None:
    """task-168: a CHILD step carries parent_step_id (nested, never top-level)
    while the PARENT carries the live breadcrumb trio; both round-trip."""
    parent_id = new_ulid()
    payload = ws.PipelineStatePayload(
        pipeline_id=new_ulid(),
        steps=[
            ws.PipelineStep(
                step_id=parent_id,
                name="Model coastal flood",
                tool_name="model_flood_scenario",
                state="running",
                substep_label="fetch_topobathy",
                substep_index=2,
                substep_total=7,
            ),
            ws.PipelineStep(
                step_id=new_ulid(),
                name="fetch_topobathy",
                tool_name="fetch_topobathy",
                state="running",
                parent_step_id=parent_id,
            ),
        ],
    )
    dumped = _roundtrip_idempotent(_wrap(payload, session_id))
    steps = dumped["payload"]["steps"]
    # Parent: top-level, breadcrumb populated.
    assert steps[0]["parent_step_id"] is None
    assert steps[0]["substep_label"] == "fetch_topobathy"
    assert steps[0]["substep_index"] == 2
    assert steps[0]["substep_total"] == 7
    # Child: nested under the parent, no breadcrumb of its own.
    assert steps[1]["parent_step_id"] == parent_id
    assert steps[1]["substep_label"] is None
    assert steps[1]["substep_index"] is None


def test_pipeline_step_substep_total_none_for_unknown_plan(session_id: str) -> None:
    """task-168: substep_total may be None when the planned child count is
    unknown -- the breadcrumb then shows just the label + index."""
    payload = ws.PipelineStatePayload(
        pipeline_id=new_ulid(),
        steps=[
            ws.PipelineStep(
                step_id=new_ulid(),
                name="Model coastal flood",
                tool_name="model_flood_scenario",
                state="running",
                substep_label="run_solver",
                substep_index=1,
                substep_total=None,
            ),
        ],
    )
    dumped = _roundtrip_idempotent(_wrap(payload, session_id))
    step = dumped["payload"]["steps"][0]
    assert step["substep_label"] == "run_solver"
    assert step["substep_index"] == 1
    assert step["substep_total"] is None


def test_pipeline_step_substep_index_rejects_non_positive() -> None:
    """task-168: substep_index/total are 1-based (ge=1) -- 0 / negative reject."""
    with pytest.raises(ValidationError):
        ws.PipelineStep(
            step_id=new_ulid(),
            name="x",
            tool_name="x",
            state="running",
            substep_index=0,
        )
    with pytest.raises(ValidationError):
        ws.PipelineStep(
            step_id=new_ulid(),
            name="x",
            tool_name="x",
            state="running",
            substep_total=0,
        )


# --- map-command and the per-command args models ---------------------------- #


def test_map_command_load_layer_args_roundtrip(session_id: str) -> None:
    args = ws.LoadLayerArgs(
        layer_id="run-01HX-flood-depth",
        wms_url="https://qgis.example.com/wms?MAP=01HX.qgs",
        style_preset="flood_depth_blue",
        temporal=ws.MapTemporal(
            start="2026-06-05T00:00:00Z", end="2026-06-05T06:00:00Z", step_seconds=300
        ),
    )
    payload = ws.MapCommandPayload(command="load-layer", args=args.model_dump(mode="json"))
    dumped = _roundtrip_idempotent(_wrap(payload, session_id))
    # The internal command discriminator survives the round-trip
    assert dumped["payload"]["command"] == "load-layer"
    # The args dict re-validates as LoadLayerArgs (the consumer's contract)
    re_parsed = ws.LoadLayerArgs.model_validate(dumped["payload"]["args"])
    assert re_parsed.layer_id == "run-01HX-flood-depth"


def test_map_command_zoom_to_bbox_args(session_id: str) -> None:
    args = ws.ZoomToArgs(bbox=(-82.5, 26.4, -81.7, 26.9))
    payload = ws.MapCommandPayload(command="zoom-to", args=args.model_dump(mode="json"))
    _roundtrip_idempotent(_wrap(payload, session_id))


def test_map_command_set_layer_opacity_clamped() -> None:
    with pytest.raises(ValidationError):
        ws.SetLayerOpacityArgs(layer_id="x", opacity=1.5)


def test_map_command_args_registry_covers_every_command() -> None:
    """The internal command vocabulary must match the registered args models."""
    from typing import get_args as _get_args
    declared = set(_get_args(ws.MapCommand))
    registered = set(ws.MAP_COMMAND_ARGS.keys())
    assert declared == registered, (declared, registered)


# --- the rest of A.4 messages ---------------------------------------------- #


def test_confirmation_request_has_no_cost_field(session_id: str) -> None:
    """Invariant 9: no cost field anywhere on confirmation messages."""
    payload = ws.ConfirmationRequestPayload(
        request_id=new_ulid(),
        title="Run SFINCS for Hurricane Ian",
        description="This will execute the storm-surge solver.",
        estimated_duration_seconds=600,
    )
    dumped = _roundtrip_idempotent(_wrap(payload, session_id))
    payload_keys = set(dumped["payload"].keys())
    assert not any("cost" in k.lower() for k in payload_keys)
    # And the model itself rejects an attempt to add one
    with pytest.raises(ValidationError):
        ws.ConfirmationRequestPayload.model_validate(
            {
                "request_id": new_ulid(),
                "title": "x",
                "description": "x",
                "estimated_cost_usd": 4.20,
            }
        )


def test_session_state_payload(session_id: str) -> None:
    payload = ws.SessionStatePayload(
        chat_history=[{"role": "user", "content": "hi"}],
        loaded_layers=[],
        pipeline_history=[],
        map_view={"center": [-82.0, 26.5], "zoom": 8.0, "bbox": [-82.5, 26.4, -81.7, 26.9]},
    )
    _roundtrip_idempotent(_wrap(payload, session_id))


def test_error_payload_uses_a6_codes(session_id: str) -> None:
    payload = ws.ErrorPayload(error_code="RATE_LIMITED", message="slow down", retry_after_seconds=30)
    _roundtrip_idempotent(_wrap(payload, session_id))


def test_error_payload_unknown_code_rejected() -> None:
    with pytest.raises(ValidationError):
        ws.ErrorPayload(error_code="totally_made_up", message="x")  # type: ignore[arg-type]


def test_location_resolved(session_id: str) -> None:
    payload = ws.LocationResolvedPayload(
        resolved_id=new_ulid(),
        label="Fort Myers, FL",
        bbox=(-82.0, 26.5, -81.8, 26.7),
        granularity="city",
        source="geocoding",
    )
    _roundtrip_idempotent(_wrap(payload, session_id))


def test_spatial_input_request(session_id: str) -> None:
    payload = ws.SpatialInputRequestPayload(
        request_id=new_ulid(),
        mode="point",
        title="Pick a location",
        description="Where should the model be centered?",
        suggested_view=ws.SuggestedView(bbox=(-82.5, 26.4, -81.7, 26.9), zoom=10.0),
    )
    _roundtrip_idempotent(_wrap(payload, session_id))


def test_spatial_input_request_vector_draw_mode(session_id: str) -> None:
    """FR-WC-16: the request may ask the client to open the vector-draw surface."""
    payload = ws.SpatialInputRequestPayload(
        request_id=new_ulid(),
        mode="vector_draw",
        title="Draw the AOI and any barriers",
        description="Draw the study area; add walls (red) and flap gates (green).",
        suggested_view=ws.SuggestedView(bbox=(-85.31, 35.04, -85.30, 35.05), zoom=15.0),
    )
    dumped = _roundtrip_idempotent(_wrap(payload, session_id))
    assert dumped["payload"]["mode"] == "vector_draw"


def test_spatial_input_request_unknown_mode_rejected() -> None:
    with pytest.raises(ValidationError):
        ws.SpatialInputRequestPayload(
            request_id=new_ulid(),
            mode="polygon",  # type: ignore[arg-type]  # not in the closed enum
            title="x",
            description="d",
        )


def test_disambiguation_request(session_id: str) -> None:
    payload = ws.DisambiguationRequestPayload(
        request_id=new_ulid(),
        title="Which Springfield?",
        description="Multiple matches found.",
        candidates=[
            ws.DisambiguationCandidate(id="a", label="Springfield, IL", bbox=(-89.7, 39.7, -89.6, 39.9)),
            ws.DisambiguationCandidate(id="b", label="Springfield, MA", bbox=(-72.7, 42.0, -72.4, 42.2)),
        ],
    )
    _roundtrip_idempotent(_wrap(payload, session_id))


def test_clarification_request_requires_2_to_4_options(session_id: str) -> None:
    with pytest.raises(ValidationError):
        ws.ClarificationRequestPayload(
            request_id=new_ulid(),
            question="x?",
            options=[ws.ClarificationOption(id="a", label="A", description="A path")],  # only one
        )


def test_clarification_request_ok(session_id: str) -> None:
    payload = ws.ClarificationRequestPayload(
        request_id=new_ulid(),
        question="Model the storm surge or the pluvial flooding?",
        options=[
            ws.ClarificationOption(id="surge", label="Storm surge", description="SFINCS with surge BC"),
            ws.ClarificationOption(id="pluvial", label="Pluvial", description="SFINCS with rainfall BC"),
        ],
    )
    _roundtrip_idempotent(_wrap(payload, session_id))


# --------------------------------------------------------------------------- #
# Envelope-level + registry coverage
# --------------------------------------------------------------------------- #


def test_secrets_payloads_registered_in_ws_dicts() -> None:
    """job-0115 — OQ-0100-WS-REGISTRY-WIRING resolved by splatting the §F.3
    per-Case secrets payloads into the ws.py routing dicts.

    Mirrors the per-module ``SECRET_*_PAYLOADS`` dicts the secrets module
    already exposes; this test guards against a future refactor accidentally
    dropping the wire-up.
    """
    assert "secret-add" in ws.CLIENT_TO_AGENT_PAYLOADS
    assert "secret-revoke" in ws.CLIENT_TO_AGENT_PAYLOADS
    assert "secrets-list" in ws.AGENT_TO_CLIENT_PAYLOADS
    # Credential-request flow (§F.3 amendment): request is agent->client, the
    # retry signal is client->agent (the key itself rides the secret-add path).
    assert "credential-request" in ws.AGENT_TO_CLIENT_PAYLOADS
    assert "credential-provided" in ws.CLIENT_TO_AGENT_PAYLOADS
    # And in the aggregated registry the smoke factory test consumes
    for t in (
        "secret-add",
        "secret-revoke",
        "secrets-list",
        "credential-request",
        "credential-provided",
    ):
        assert t in ws.ALL_PAYLOADS, f"{t} missing from ws.ALL_PAYLOADS"


def test_envelope_payload_always_an_object(session_id: str) -> None:
    """A.1: payload is always an object, never null/string/list."""
    payload = ws.SessionResumePayload()
    env = _wrap(payload, session_id)
    dumped = env.model_dump(mode="json")
    assert isinstance(dumped["payload"], dict)


def test_every_a3_a4_a4b_payload_round_trips(session_id: str) -> None:
    """Smoke: every payload class in ws.ALL_PAYLOADS must construct & round-trip
    via its minimal arguments. This catches an accidental drop from the registry.
    """
    minimal_factories = {
        "user-message": lambda: ws.UserMessagePayload(text="hi"),
        "cancel": lambda: ws.CancelPayload(),
        "confirm-response": lambda: ws.ConfirmResponsePayload(request_id=new_ulid(), approved=True),
        "session-resume": lambda: ws.SessionResumePayload(),
        "spatial-input-response": lambda: ws.SpatialInputResponsePayload(request_id=new_ulid(), cancelled=True),
        "disambiguation-response": lambda: ws.DisambiguationResponsePayload(request_id=new_ulid(), cancelled=True),
        "clarification-response": lambda: ws.ClarificationResponsePayload(request_id=new_ulid(), cancelled=True),
        "agent-message-chunk": lambda: ws.AgentMessageChunkPayload(message_id=new_ulid(), delta="x"),
            "agent-thinking-chunk": lambda: ws.AgentThinkingChunkPayload(message_id=new_ulid(), delta="x"),
        "tool-call-start": lambda: ws.ToolCallStartPayload(
            call_id=new_ulid(), step_id=new_ulid(), tool_name="t", tool_category="workflow"
        ),
        "tool-call-progress": lambda: ws.ToolCallProgressPayload(call_id=new_ulid()),
        "tool-call-complete": lambda: ws.ToolCallCompletePayload(call_id=new_ulid(), result_summary="ok"),
        "tool-call-failed": lambda: ws.ToolCallFailedPayload(
            call_id=new_ulid(), error_code="GENERIC", message="x"
        ),
        "pipeline-state": lambda: ws.PipelineStatePayload(pipeline_id=new_ulid()),
        "map-command": lambda: ws.MapCommandPayload(command="invalidate-tiles", args={}),
        "confirmation-request": lambda: ws.ConfirmationRequestPayload(
            request_id=new_ulid(), title="x", description="x"
        ),
        "session-state": lambda: ws.SessionStatePayload(),
        "error": lambda: ws.ErrorPayload(error_code="INTERNAL_ERROR", message="x"),
        "location-resolved": lambda: ws.LocationResolvedPayload(
            resolved_id=new_ulid(),
            label="x",
            bbox=(-1.0, -1.0, 1.0, 1.0),
            granularity="city",
            source="geocoding",
        ),
        "spatial-input-request": lambda: ws.SpatialInputRequestPayload(
            request_id=new_ulid(), mode="point", title="t", description="d"
        ),
        "disambiguation-request": lambda: ws.DisambiguationRequestPayload(
            request_id=new_ulid(),
            title="t",
            description="d",
            candidates=[ws.DisambiguationCandidate(id="a", label="A", bbox=(-1.0, -1.0, 1.0, 1.0))],
        ),
        "clarification-request": lambda: ws.ClarificationRequestPayload(
            request_id=new_ulid(),
            question="q?",
            options=[
                ws.ClarificationOption(id="a", label="A", description="a"),
                ws.ClarificationOption(id="b", label="B", description="b"),
            ],
        ),
        # sprint-08 — FR-FR-1 + §F.1.2 Mode 2
        "recovery-choice": lambda: ws.RecoveryChoicePayload(
            request_id=new_ulid(),
            failed_step_id=new_ulid(),
            error_code="UPSTREAM_API_ERROR",
            error_message="x",
            context="x",
            options=["deny", "retry", "chat"],
        ),
        "recovery-choice-response": lambda: ws.RecoveryChoiceResponsePayload(
            request_id=new_ulid(), choice="retry"
        ),
        "offer-catalog-addition": lambda: ws.OfferCatalogAdditionPayload(
            request_id=new_ulid(),
            url="https://example.gov/data/foo",
            discovered_via="user-query",
            probe_findings=ws.ProbeFindings(),
            suggested_catalog_entry=ws.SuggestedCatalogEntry(),
        ),
        "catalog-addition-response": lambda: ws.CatalogAdditionResponsePayload(
            request_id=new_ulid(), decision="reject"
        ),
        # job-0115 — §F.3 per-Case secrets envelopes (OQ-0100-WS-REGISTRY-WIRING)
        "secret-add": lambda: ws.SecretAddEnvelopePayload(
            provider="ebird", case_id=new_ulid(), key_value="x"
        ),
        "secret-revoke": lambda: ws.SecretRevokeEnvelopePayload(secret_id=new_ulid()),
        "secrets-list": lambda: ws.SecretsListEnvelopePayload(),
        # §F.3 amendment — just-in-time credential-request flow
        "credential-request": lambda: ws.CredentialRequestEnvelopePayload(
            request_id=new_ulid(),
            provider_id="ebird",
            provider_label="eBird",
            signup_url="https://ebird.org/api/keygen",
            secret_key_name="EBIRD_API_KEY",
            message="I need an eBird API key to fetch observations for this Case.",
            tool_name="fetch_ebird_observations",
        ),
        "credential-provided": lambda: ws.CredentialProvidedEnvelopePayload(
            request_id=new_ulid(), secret_id=new_ulid()
        ),
        # job-0127 — tool payload-warning envelopes (Wave 2)
        "tool-payload-warning": lambda: ws.PayloadWarningEnvelopePayload(
            warning_id=new_ulid(),
            tool_name="fetch_dem",
            tool_args={},
            estimated_mb=50.0,
            threshold_mb=25.0,
            recommendation="narrow",
        ),
        "tool-payload-confirmation": lambda: ws.PayloadConfirmationEnvelopePayload(
            warning_id=new_ulid(),
            decision="proceed",
        ),
        # job-0223 — chart-emission envelope (sprint-13 conversational analysis)
        "chart-emission": lambda: ChartEmissionPayload(
            chart_id=new_ulid(),
            vega_lite_spec={
                "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                "mark": "bar",
                "encoding": {"x": {"field": "a"}, "y": {"field": "b"}},
            },
            title="Damage distribution",
        ),
        # job-0233 — python-sandbox code-exec envelopes (sprint-13 Stage 2)
        "code-exec-request": lambda: CodeExecRequestPayload(
            code_exec_id=new_ulid(),
            python_code="result = dem.read(1).mean()",
            layer_refs={"dem": "gs://bucket/dem.tif"},
            rationale="mean elevation",
        ),
        "code-exec-result": lambda: CodeExecResultPayload(
            code_exec_id=new_ulid(),
            status="ok",
            stdout_tail="done\n",
            stderr_tail="",
            result={"kind": "json", "value": 42},
            truncated=False,
            duration_s=0.5,
        ),
        # region-disambiguation picker (state-bbox-fallback narrowing). Request
        # is agent->client (whole-state default + candidate counties); provided
        # is client->agent (the user's pick). Mirrors the credential flow.
        "region-choice-request": lambda: RegionChoiceRequestEnvelopePayload(
            request_id=new_ulid(),
            state_name="Florida",
            state_code="FL",
            state_bbox=(-87.634896, 24.396308, -79.974306, 31.000888),
            candidates=[
                RegionCandidate(
                    region_id="county-12071",
                    name="Lee County",
                    bbox=(-82.331, 26.317, -81.564, 26.795),
                )
            ],
            message=(
                "Snapped to the whole state of Florida; pick a county to "
                "narrow the area."
            ),
        ),
        "region-choice-provided": lambda: RegionChoiceProvidedEnvelopePayload(
            request_id=new_ulid(),
            choice="region",
            selected_region_id="county-12071",
            selected_bbox=(-82.331, 26.317, -81.564, 26.795),
        ),
        # solve-progress — LIVE big-sim telemetry (tool-accuracy panel, 2026-06-17)
        "solve-progress": lambda: ws.SolveProgressPayload(
            run_id=new_ulid(),
            solver="sfincs",
            grid_resolution_m=30.0,
            active_cell_count=100_000,
            vcpus=8,
            elapsed_seconds=42.5,
            eta_seconds=300.0,
        ),
        # tool-io — raw args + function_response sidecar for the tool-card
        # expander (tool-card-expand-output spec).
        "tool-io": lambda: ws.ToolIoPayload(
            step_id=new_ulid(),
            tool_name="geocode_location",
            raw_args='{"location_name": "Boulder, CO"}',
            function_response='{"status": "ok"}',
            is_error=False,
            args_truncated=False,
            response_truncated=False,
            args_bytes=32,
            response_bytes=18,
        ),
    }
    # Every payload registered in ws.ALL_PAYLOADS must have a minimal factory
    # (i.e., the test covers the full inventory).
    assert set(minimal_factories.keys()) == set(ws.ALL_PAYLOADS.keys())
    for msg_type, factory in minimal_factories.items():
        payload = factory()
        env = _wrap(payload, session_id)
        _roundtrip_idempotent(env)
