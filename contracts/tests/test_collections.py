"""Round-trip + negative tests for the five MongoDB collections (Appendix D)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from grace2_contracts.collections import (
    ARTICLES_VECTOR_INDEX,
    EMBEDDING_DIMENSIONS_DEFAULT,
    EMBEDDING_MODEL_DEFAULT,
    EVENTS_VECTOR_INDEX,
    MONGO_DUMP_KWARGS,
    RUNS_VECTOR_INDEX,
    SESSIONS_TTL,
    VECTOR_INDEXES,
    ArticleDocument,
    ChatMessage,
    EventDocument,
    MapView,
    PipelineSnapshot,
    PipelineStepSummary,
    ProjectDocument,
    ProjectLayerSummary,
    RunDocument,
    SessionDocument,
    ToolCallSummary,
    UserSpatialInput,
)
from grace2_contracts.common import new_ulid
from grace2_contracts.event import (
    ClaimSet,
    EventLocation,
    EventProvenance,
    HurricaneIntensity,
    IntensityIndicators,
    NumericClaim,
)


def _project_doc() -> ProjectDocument:
    return ProjectDocument(
        id=new_ulid(),
        session_id=new_ulid(),
        qgs_uri="gs://trid3nt/projects/01HX/project_01HX.qgs",
        name="Hurricane Ian / Fort Myers / SFINCS depth",
        bbox=(-82.5, 26.4, -81.7, 26.9),
        hazard_types=["flood"],
        layers=[
            ProjectLayerSummary(
                layer_id="run-01HX-flood-depth",
                name="Flood depth (m)",
                layer_type="raster",
                uri="gs://trid3nt/runs/01HX/depth.cog.tif",
                style_preset="flood_depth_blue",
                visible=True,
                role="primary",
                temporal=True,
            )
        ],
        created_at="2026-06-05T12:00:00Z",
        updated_at="2026-06-05T12:30:00Z",
    )


def _doc_roundtrip_idempotent(doc) -> dict:
    dumped_a = doc.model_dump(**MONGO_DUMP_KWARGS)
    text_a = json.dumps(dumped_a, sort_keys=True)
    cls = type(doc)
    doc_b = cls.model_validate(json.loads(text_a))
    dumped_b = doc_b.model_dump(**MONGO_DUMP_KWARGS)
    text_b = json.dumps(dumped_b, sort_keys=True)
    assert text_a == text_b
    return dumped_a


def test_project_doc_roundtrip_and_id_aliasing() -> None:
    doc = _project_doc()
    dumped = _doc_roundtrip_idempotent(doc)
    # Mongo dump uses _id, not id
    assert "_id" in dumped and "id" not in dumped


def test_run_doc_status_supports_cancelled() -> None:
    """Invariant 8: cancelled is a distinct terminal RunDocument.status."""
    doc = RunDocument(
        id=new_ulid(),
        project_id=new_ulid(),
        session_id=new_ulid(),
        status="cancelled",
        cancelled_at="2026-06-05T12:00:00Z",
        cancellation_reason="user-requested",
        run_type="modeled",
        hazard_type="flood",
        workflow_name="run_storm_surge_flood",
        bbox=(-82.5, 26.4, -81.7, 26.9),
        assessment=None,
    )
    _doc_roundtrip_idempotent(doc)


def test_run_doc_has_no_cost_field() -> None:
    """Invariant 9: no cost field on runs (D.7)."""
    doc = RunDocument(
        id=new_ulid(),
        project_id=new_ulid(),
        session_id=new_ulid(),
        status="pending",
        run_type="modeled",
        hazard_type="flood",
        workflow_name="run_storm_surge_flood",
        bbox=(-82.5, 26.4, -81.7, 26.9),
        assessment=None,
    )
    dumped = doc.model_dump(**MONGO_DUMP_KWARGS)
    assert not any("cost" in k.lower() for k in dumped.keys())
    # ... and the model refuses a cost field via extra=forbid
    with pytest.raises(ValidationError):
        RunDocument.model_validate({**dumped, "cost_usd": 4.20})


def test_run_doc_with_user_spatial_input() -> None:
    doc = RunDocument(
        id=new_ulid(),
        project_id=new_ulid(),
        session_id=new_ulid(),
        status="complete",
        started_at="2026-06-05T12:00:00Z",
        completed_at="2026-06-05T12:30:00Z",
        run_type="modeled",
        hazard_type="flood",
        workflow_name="run_storm_surge_flood",
        bbox=(-82.5, 26.4, -81.7, 26.9),
        user_spatial_inputs=[
            UserSpatialInput(
                request_id=new_ulid(),
                geometry_type="point",
                coordinates=[-82.0, 26.5],
                prompt_title="Pick the impact center",
                submitted_at="2026-06-05T12:01:00Z",
            )
        ],
    )
    _doc_roundtrip_idempotent(doc)


def test_article_doc_roundtrip() -> None:
    doc = ArticleDocument(
        id=new_ulid(),
        url="https://example.com/ian-coverage",
        url_hash="0" * 64,
        title="Hurricane Ian roars ashore",
        publisher="AP",
        text="text",
        text_length=4,
        fetched_at="2026-06-05T12:00:00Z",
        extraction_status="extracted",
        extracted_event_ids=[new_ulid()],
        last_processed_at="2026-06-05T12:05:00Z",
        embedding_model=EMBEDDING_MODEL_DEFAULT,
    )
    _doc_roundtrip_idempotent(doc)


def test_event_doc_is_event_metadata_shape() -> None:
    article_id = new_ulid()
    doc = EventDocument(
        event_id=new_ulid(),
        event_type="hurricane",
        confidence=0.92,
        location=EventLocation(
            bbox=(-82.6, 26.4, -81.7, 27.0),
            place_name="Fort Myers, FL",
        ),
        time_range={"start": "2022-09-28T00:00:00Z", "end": "2022-09-30T00:00:00Z"},
        time_classification="past",
        intensity=IntensityIndicators(
            hurricane=HurricaneIntensity(
                saffir_simpson=ClaimSet(
                    claims=[
                        NumericClaim(
                            value=4.0,
                            unit="category",
                            source_type="agency",
                            source_id="a",
                            source_url="https://example.com",
                            reporting_time="2026-06-05T12:00:00Z",
                        )
                    ],
                    consensus_value=4.0,
                )
            )
        ),
        provenance=EventProvenance(article_ids=[article_id], primary_article_id=article_id),
        extracted_at="2026-06-05T12:00:00Z",
        extractor_version="hep-extractor-v0.1.0",
    )
    dumped_a = doc.model_dump(mode="json")
    text_a = json.dumps(dumped_a, sort_keys=True)
    doc_b = EventDocument.model_validate(json.loads(text_a))
    dumped_b = doc_b.model_dump(mode="json")
    text_b = json.dumps(dumped_b, sort_keys=True)
    assert text_a == text_b


def test_session_doc_with_pipeline_history_cancelled() -> None:
    doc = SessionDocument(
        id=new_ulid(),
        client_fingerprint="opaque-cookie",
        created_at="2026-06-05T12:00:00Z",
        last_active_at="2026-06-05T12:30:00Z",
        expires_at="2026-06-05T13:30:00Z",
        chat_history=[
            ChatMessage(
                message_id=new_ulid(),
                role="user",
                content="model the flood",
                created_at="2026-06-05T12:00:00Z",
            ),
            ChatMessage(
                message_id=new_ulid(),
                role="agent",
                content="okay, running the solver",
                tool_calls=[
                    ToolCallSummary(
                        call_id=new_ulid(),
                        tool_name="run_solver",
                        state="cancelled",
                        started_at="2026-06-05T12:01:00Z",
                        completed_at="2026-06-05T12:02:00Z",
                    )
                ],
                created_at="2026-06-05T12:01:00Z",
            ),
        ],
        pipeline_history=[
            PipelineSnapshot(
                pipeline_id=new_ulid(),
                started_at="2026-06-05T12:01:00Z",
                completed_at="2026-06-05T12:02:00Z",
                final_state="cancelled",
                steps=[
                    PipelineStepSummary(
                        step_id=new_ulid(),
                        name="run_solver",
                        tool_name="run_solver",
                        state="cancelled",
                        started_at="2026-06-05T12:01:00Z",
                        completed_at="2026-06-05T12:02:00Z",
                    )
                ],
            )
        ],
        map_view=MapView(
            center=(-82.0, 26.5),
            zoom=8.0,
            bbox=(-82.5, 26.4, -81.7, 26.9),
        ),
    )
    _doc_roundtrip_idempotent(doc)


# --- Vector index + TTL configs (D.6, D.8) --------------------------------- #


def test_vector_indexes_cover_runs_articles_events() -> None:
    assert set(VECTOR_INDEXES.keys()) == {"runs", "articles", "events"}
    for spec in VECTOR_INDEXES.values():
        assert spec["type"] == "vectorSearch"
        # The default dim is the documented constant; OQ-7 surfaces the
        # recall-vs-cost check before infra locks Atlas.
        vector_field = next(f for f in spec["fields"] if f["type"] == "vector")
        assert vector_field["numDimensions"] == EMBEDDING_DIMENSIONS_DEFAULT
        assert vector_field["similarity"] == "cosine"
        assert vector_field["path"] == "embedding"


def test_embedding_dimension_default_is_768_oq7() -> None:
    """OQ-7: SRS Decision L default (text-embedding-005, 768 dims)."""
    assert EMBEDDING_DIMENSIONS_DEFAULT == 768
    assert EMBEDDING_MODEL_DEFAULT == "text-embedding-005"


def test_runs_vector_index_filter_paths_are_high_cardinality() -> None:
    """The runs index filters on hazard_type and run_type — both denormalized
    onto the document so the index doesn't have to traverse the embedded
    AssessmentEnvelope."""
    filter_paths = [
        f["path"] for f in RUNS_VECTOR_INDEX["fields"] if f["type"] == "filter"
    ]
    assert "hazard_type" in filter_paths
    assert "run_type" in filter_paths


def test_articles_vector_index_filters_on_extraction_status() -> None:
    filter_paths = [
        f["path"] for f in ARTICLES_VECTOR_INDEX["fields"] if f["type"] == "filter"
    ]
    assert "extraction_status" in filter_paths


def test_events_vector_index_filters_on_event_type_and_time_classification() -> None:
    filter_paths = [
        f["path"] for f in EVENTS_VECTOR_INDEX["fields"] if f["type"] == "filter"
    ]
    assert "event_type" in filter_paths
    assert "time_classification" in filter_paths


def test_sessions_ttl_config() -> None:
    assert SESSIONS_TTL["collection"] == "sessions"
    assert SESSIONS_TTL["field"] == "expires_at"
    assert SESSIONS_TTL["expire_after_seconds"] == 30 * 24 * 60 * 60


# --- D.6 PipelineStepSummary extended fields (job-0030) -------------------- #
# Closes job-0026 OQ-W-26-PIPELINE-STEP-FIELDS.


def test_pipeline_step_summary_carries_new_optional_fields_roundtrip() -> None:
    """Extended D.6 fields (progress_percent / error_code / error_message)
    populate and JSON-roundtrip on a single step."""
    step = PipelineStepSummary(
        step_id=new_ulid(),
        name="run_sfincs_solver",
        tool_name="run_solver",
        state="running",
        started_at="2026-06-06T12:00:00Z",
        progress_percent=42,
    )
    dumped_a = step.model_dump(mode="json")
    assert dumped_a["progress_percent"] == 42
    assert dumped_a["error_code"] is None
    assert dumped_a["error_message"] is None

    text_a = json.dumps(dumped_a, sort_keys=True)
    step_b = PipelineStepSummary.model_validate(json.loads(text_a))
    dumped_b = step_b.model_dump(mode="json")
    text_b = json.dumps(dumped_b, sort_keys=True)
    assert text_a == text_b

    # All three new fields default to None (preserves backward field set).
    minimal = PipelineStepSummary(
        step_id=new_ulid(),
        name="step",
        tool_name="t",
        state="pending",
    )
    assert minimal.progress_percent is None
    assert minimal.error_code is None
    assert minimal.error_message is None


@pytest.mark.parametrize("good", [0, 1, 42, 99, 100])
def test_pipeline_step_summary_progress_percent_accepts_0_to_100(good: int) -> None:
    """Field(ge=0, le=100) accepts the inclusive endpoints + interior."""
    step = PipelineStepSummary(
        step_id=new_ulid(),
        name="step",
        tool_name="t",
        state="running",
        progress_percent=good,
    )
    assert step.progress_percent == good


@pytest.mark.parametrize("bad", [-1, 101, 200, 1_000_000])
def test_pipeline_step_summary_progress_percent_rejects_out_of_range(
    bad: int,
) -> None:
    """Field(ge=0, le=100) rejects anything outside [0, 100]."""
    with pytest.raises(ValidationError):
        PipelineStepSummary(
            step_id=new_ulid(),
            name="step",
            tool_name="t",
            state="running",
            progress_percent=bad,
        )


@pytest.mark.parametrize(
    "code",
    [
        "SFINCS_TIMEOUT",
        "DEM_SOURCE_UNAVAILABLE",
        "RATE_LIMITED",
        "A",
        "X_1",
        "FOO_BAR_BAZ_42",
    ],
)
def test_pipeline_step_summary_error_code_accepts_screaming_snake(code: str) -> None:
    """Appendix A.6: SCREAMING_SNAKE_CASE is the wire convention."""
    step = PipelineStepSummary(
        step_id=new_ulid(),
        name="step",
        tool_name="t",
        state="failed",
        error_code=code,
    )
    assert step.error_code == code


@pytest.mark.parametrize(
    "bad",
    [
        "camelCase",
        "snake_case",
        "kebab-case",
        "lower_UPPER",
        "_LEADING_UNDERSCORE",
        "TRAILING_",
        "DOUBLE__UNDERSCORE",
        "1_LEADING_DIGIT",
        "WITH SPACE",
        "",
    ],
)
def test_pipeline_step_summary_error_code_rejects_non_screaming_snake(bad: str) -> None:
    """Non-SCREAMING_SNAKE shapes are rejected by the field validator."""
    with pytest.raises(ValidationError):
        PipelineStepSummary(
            step_id=new_ulid(),
            name="step",
            tool_name="t",
            state="failed",
            error_code=bad,
        )


def test_pipeline_step_summary_error_message_512_char_cap() -> None:
    """error_message is capped at 512 chars to discourage stack-trace leakage."""
    # 512 is accepted
    step = PipelineStepSummary(
        step_id=new_ulid(),
        name="step",
        tool_name="t",
        state="failed",
        error_code="SFINCS_TIMEOUT",
        error_message="x" * 512,
    )
    assert step.error_message is not None
    assert len(step.error_message) == 512

    # 513 is rejected
    with pytest.raises(ValidationError):
        PipelineStepSummary(
            step_id=new_ulid(),
            name="step",
            tool_name="t",
            state="failed",
            error_code="SFINCS_TIMEOUT",
            error_message="x" * 513,
        )


# --- D.6 PipelineStepSummary.duration_ms (job-0264) ------------------------- #
# ELEVATED tool-timer requirement: authoritative wall-clock duration stamped
# on the terminal transition. Optional, ge=0, defaults None.


def test_pipeline_step_summary_duration_ms_defaults_none() -> None:
    """duration_ms defaults to None and JSON-serializes (backward-compatible)."""
    step = PipelineStepSummary(
        step_id=new_ulid(),
        name="step",
        tool_name="t",
        state="pending",
    )
    assert step.duration_ms is None
    assert step.model_dump(mode="json")["duration_ms"] is None


def test_pipeline_step_summary_duration_ms_roundtrips() -> None:
    """A populated duration_ms survives a JSON round-trip unchanged."""
    step = PipelineStepSummary(
        step_id=new_ulid(),
        name="run_sfincs_solver",
        tool_name="run_solver",
        state="complete",
        started_at="2026-06-10T12:00:00Z",
        completed_at="2026-06-10T12:02:34Z",
        duration_ms=154_000,
    )
    dumped_a = step.model_dump(mode="json")
    assert dumped_a["duration_ms"] == 154_000
    text_a = json.dumps(dumped_a, sort_keys=True)
    step_b = PipelineStepSummary.model_validate(json.loads(text_a))
    assert step_b.duration_ms == 154_000
    text_b = json.dumps(step_b.model_dump(mode="json"), sort_keys=True)
    assert text_a == text_b


@pytest.mark.parametrize("good", [0, 1, 250, 154_000, 12_600_000])
def test_pipeline_step_summary_duration_ms_accepts_non_negative(good: int) -> None:
    """Field(ge=0) accepts zero (sub-ms tool) through long-running solvers."""
    step = PipelineStepSummary(
        step_id=new_ulid(),
        name="step",
        tool_name="t",
        state="complete",
        duration_ms=good,
    )
    assert step.duration_ms == good


@pytest.mark.parametrize("bad", [-1, -250, -1_000_000])
def test_pipeline_step_summary_duration_ms_rejects_negative(bad: int) -> None:
    """Field(ge=0) rejects a negative duration (clock-skew never reaches here)."""
    with pytest.raises(ValidationError):
        PipelineStepSummary(
            step_id=new_ulid(),
            name="step",
            tool_name="t",
            state="complete",
            duration_ms=bad,
        )


# --- D.6 PipelineStepSummary two-card sim observability (task-149) ----------- #
# Mirror the ws.PipelineStep card-kind discriminator + Batch binding so a
# persisted/replayed snapshot and a cold-case rehydration carry the off-box
# solver card across a reconnect. role defaults "tool", ids default None.


def test_pipeline_step_summary_role_defaults_to_tool_back_compat() -> None:
    """task-149: a minimally-built summary is an on-box tool card with no Batch
    binding — proving the persisted snapshot stays byte-identical for old steps.
    """
    step = PipelineStepSummary(
        step_id=new_ulid(),
        name="step",
        tool_name="t",
        state="pending",
    )
    assert step.role == "tool"
    assert step.batch_job_id is None
    assert step.batch_status is None
    dumped = step.model_dump(mode="json")
    assert dumped["role"] == "tool"
    assert dumped["batch_job_id"] is None
    assert dumped["batch_status"] is None


def test_pipeline_step_summary_compute_card_roundtrips() -> None:
    """task-149: a ``role="compute"`` Batch-bound solver card persists the
    jobId + last DescribeJobs status and survives a JSON round-trip unchanged.
    """
    step = PipelineStepSummary(
        step_id=new_ulid(),
        name="run_sfincs_solver",
        tool_name="run_solver",
        state="running",
        role="compute",
        batch_job_id="a1b2c3d4-0000-1111-2222-333344445555",
        batch_status="RUNNING",
    )
    dumped_a = step.model_dump(mode="json")
    assert dumped_a["role"] == "compute"
    assert dumped_a["batch_job_id"] == "a1b2c3d4-0000-1111-2222-333344445555"
    assert dumped_a["batch_status"] == "RUNNING"
    text_a = json.dumps(dumped_a, sort_keys=True)
    step_b = PipelineStepSummary.model_validate(json.loads(text_a))
    assert step_b.role == "compute"
    assert step_b.batch_job_id == "a1b2c3d4-0000-1111-2222-333344445555"
    text_b = json.dumps(step_b.model_dump(mode="json"), sort_keys=True)
    assert text_a == text_b


def test_pipeline_step_summary_rejects_unknown_role() -> None:
    """task-149: role is a closed Literal — only ``tool``/``compute``."""
    with pytest.raises(ValidationError):
        PipelineStepSummary(
            step_id=new_ulid(),
            name="step",
            tool_name="t",
            state="running",
            role="solver",  # type: ignore[arg-type]
        )


# --- D.2 ProjectLayerSummary: job-0072 new optional fields ------------------ #
# Closes OQ-62-LAYERURI-URI-FIELD, OQ-W-65-STYLE-PRESET, OQ-0068-ZIDX.


def test_project_layer_summary_new_optional_fields_default_to_none() -> None:
    """All three new optional fields (wms_url, opacity, z_index) default to None."""
    layer = ProjectLayerSummary(
        layer_id="run-01HX-flood-depth",
        name="Flood depth (m)",
        layer_type="raster",
        uri="gs://trid3nt/runs/01HX/depth.cog.tif",
        style_preset="flood_depth_blue",
        visible=True,
        role="primary",
        temporal=False,
    )
    assert layer.wms_url is None
    assert layer.opacity is None
    assert layer.z_index is None


def test_project_layer_summary_new_optional_fields_roundtrip_non_default() -> None:
    """Non-None values for wms_url, opacity, z_index round-trip through JSON."""
    layer = ProjectLayerSummary(
        layer_id="run-01HX-flood-depth",
        name="Flood depth (m)",
        layer_type="raster",
        uri="gs://trid3nt/runs/01HX/depth.cog.tif",
        style_preset="flood_depth_blue",
        visible=True,
        role="primary",
        temporal=False,
        wms_url="https://qgis.example.com/wms?MAP=01HX.qgs&LAYERS=flood_depth",
        opacity=0.75,
        z_index=10,
    )
    assert layer.wms_url == "https://qgis.example.com/wms?MAP=01HX.qgs&LAYERS=flood_depth"
    assert layer.opacity == 0.75
    assert layer.z_index == 10

    dumped = layer.model_dump(mode="json")
    assert dumped["wms_url"] == layer.wms_url
    assert dumped["opacity"] == 0.75
    assert dumped["z_index"] == 10

    # Idempotent JSON round-trip.
    import json
    text_a = json.dumps(dumped, sort_keys=True)
    layer_b = ProjectLayerSummary.model_validate(json.loads(text_a))
    dumped_b = layer_b.model_dump(mode="json")
    text_b = json.dumps(dumped_b, sort_keys=True)
    assert text_a == text_b


def test_project_layer_summary_backward_compat_missing_new_fields() -> None:
    """Documents written before job-0072 (without wms_url/opacity/z_index) still parse."""
    import json

    old_doc = {
        "layer_id": "run-01HX-flood-depth",
        "name": "Flood depth (m)",
        "layer_type": "raster",
        "uri": "gs://trid3nt/runs/01HX/depth.cog.tif",
        "style_preset": "flood_depth_blue",
        "visible": True,
        "role": "primary",
        "temporal": False,
        # no wms_url, no opacity, no z_index
    }
    layer = ProjectLayerSummary.model_validate(old_doc)
    assert layer.wms_url is None
    assert layer.opacity is None
    assert layer.z_index is None

    # Re-serialized form includes the new fields as null.
    dumped = layer.model_dump(mode="json")
    assert dumped["wms_url"] is None
    assert dumped["opacity"] is None
    assert dumped["z_index"] is None

    # Re-parsing the serialized form is also stable.
    layer_b = ProjectLayerSummary.model_validate(json.loads(json.dumps(dumped)))
    assert layer_b.wms_url is None
    assert layer_b.opacity is None
    assert layer_b.z_index is None
