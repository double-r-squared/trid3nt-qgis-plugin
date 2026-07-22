"""Unit tests for ``PipelineEmitter`` (job-0035, M4 real envelope emission).

Coverage maps to the kickoff's acceptance criteria #5:

1. ``test_happy_path_state_transitions`` — pending → running → complete emits
   3 ``pipeline-state`` envelopes carrying the full snapshot each time
   (replace-not-reconcile per Appendix A.7).
2. ``test_replace_not_reconcile_full_snapshot`` — multi-step pipeline emits
   the FULL list of steps on every transition; there is NO merge / delta
   helper on the emitter (structurally enforced).
3. ``test_error_path_failed_step_carries_code_and_message`` — ``mark_failed``
   populates ``error_code`` (SCREAMING_SNAKE_CASE) + ``error_message``
   (truncated to 512 chars) per D.6 / job-0030.
4. ``test_loaded_layers_accumulation_via_layer_uri_return`` — when a tool
   returns a ``LayerURI``, ``loaded_layers`` grows in the next ``session-state``
   emission (FR-AS-7 / A.4 ``session-state``).
5. ``test_current_pipeline_set_and_cleared`` — ``current_pipeline`` is non-null
   in ``session-state`` while a pipeline is running and ``None`` after close
   (cross-envelope visibility predicate from job-0026).
6. ``test_cancel_propagation_emits_cancelled_state`` — the M1 cancel chain
   (``asyncio.CancelledError`` inside ``emit_tool_call``) flips the step to
   ``cancelled`` (yellow chip, distinct from ``failed`` per Invariant 8).
7. ``test_loaded_layers_dedup_by_uri`` — re-fetching the same layer replaces
   in place (TENTATIVE policy per kickoff Open Questions).
8. ``test_no_merge_helper_exists`` — defensive: scans the class for any
   ``merge``/``apply_delta``/``update_partial`` method that would break A.7.

These are async tests; the sink is a sync capture closure wrapped in an
``async def`` so the emitter can ``await`` it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from grace2_contracts import new_ulid
from grace2_contracts.execution import LayerURI

from grace2_agent.pipeline_emitter import (
    EMITTER_ERROR_CODES,
    PipelineEmitter,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


class _CapturingSink:
    """Captures every envelope JSON the emitter pushes. Tests assert on the
    parsed dicts."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def __call__(self, text: str) -> None:
        self.frames.append(json.loads(text))


@pytest.fixture()
def session_id() -> str:
    # Crockford-base32 ULID per grace2_contracts.ULIDStr.
    return new_ulid()


@pytest.fixture()
def sink() -> _CapturingSink:
    return _CapturingSink()


@pytest.fixture()
def emitter(session_id: str, sink: _CapturingSink) -> PipelineEmitter:
    return PipelineEmitter(session_id=session_id, sink=sink)


def _pipeline_frames(sink: _CapturingSink) -> list[dict[str, Any]]:
    return [f for f in sink.frames if f["type"] == "pipeline-state"]


def _session_frames(sink: _CapturingSink) -> list[dict[str, Any]]:
    return [f for f in sink.frames if f["type"] == "session-state"]


# --------------------------------------------------------------------------- #
# 1. Happy-path state transitions
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_happy_path_state_transitions(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """A single step's pending → running → complete cycle emits exactly 3
    ``pipeline-state`` envelopes, each carrying the FULL snapshot."""
    step_id = await emitter.add_step(name="Geocode", tool_name="geocode_location")
    await emitter.mark_running(step_id)
    await emitter.mark_complete(step_id)

    frames = _pipeline_frames(sink)
    assert len(frames) == 3, frames

    states = [f["payload"]["steps"][0]["state"] for f in frames]
    assert states == ["pending", "running", "complete"]

    # Pipeline id is stable across the three frames.
    pids = {f["payload"]["pipeline_id"] for f in frames}
    assert len(pids) == 1


# --------------------------------------------------------------------------- #
# 2. Replace-not-reconcile (Appendix A.7)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_replace_not_reconcile_full_snapshot(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """Every emission carries the complete steps list. Adding a second step
    to an already-running pipeline must emit a frame with BOTH steps; the
    M1 client replaces its view wholesale."""
    s1 = await emitter.add_step(name="Geocode", tool_name="geocode_location")
    await emitter.mark_running(s1)
    await emitter.mark_complete(s1)

    s2 = await emitter.add_step(name="Fetch DEM", tool_name="fetch_dem")

    frames = _pipeline_frames(sink)
    last = frames[-1]
    assert [step["step_id"] for step in last["payload"]["steps"]] == [s1, s2], last
    # First step still carries its `complete` terminal state in the
    # last frame — replace-not-reconcile.
    assert last["payload"]["steps"][0]["state"] == "complete"
    assert last["payload"]["steps"][1]["state"] == "pending"


# --------------------------------------------------------------------------- #
# 3. Error path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_error_path_failed_step_carries_code_and_message(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """``mark_failed`` populates the D.6 failure fields. Truncation to 512
    chars is enforced. SCREAMING_SNAKE_CASE shape is checked by the
    PipelineStepSummary validator (job-0030)."""
    step_id = await emitter.add_step(name="Fetch DEM", tool_name="fetch_dem")
    await emitter.mark_running(step_id)

    long_message = "x" * 1000  # > 512 cap
    await emitter.mark_failed(
        step_id,
        error_code="UPSTREAM_API_ERROR",
        error_message=long_message,
    )

    last = _pipeline_frames(sink)[-1]
    step = last["payload"]["steps"][0]
    assert step["state"] == "failed"
    # The wire ``pipeline-state`` payload (Appendix A.4) carries
    # ``progress_percent`` but NOT ``error_code``/``error_message`` — those
    # live on the persisted ``PipelineStepSummary`` (D.6) which surfaces via
    # session-state.current_pipeline. Verify both shapes:
    snap = emitter.current_snapshot()
    assert snap is not None
    failed = snap.steps[0]
    assert failed.error_code == "UPSTREAM_API_ERROR"
    assert failed.error_message is not None
    assert len(failed.error_message) == 512  # truncated
    assert EMITTER_ERROR_CODES.known("UPSTREAM_API_ERROR")


@pytest.mark.asyncio
async def test_mark_failed_rejects_malformed_error_code(
    emitter: PipelineEmitter,
) -> None:
    """The D.6 ``_validate_error_code_shape`` regex (job-0030) rejects
    lowercase / kebab-case codes at serialization time. ``mark_failed``
    flows through ``current_snapshot``/``PipelineStepSummary`` so the
    rejection lands eventually — but at the wire envelope shape
    (``PipelineStep``) the regex is NOT enforced (open-set on the wire).
    Verify the persistence-snapshot raises while the wire emission proceeds.
    """
    step_id = await emitter.add_step(name="X", tool_name="x")
    await emitter.mark_running(step_id)
    # Use lowercase code — would fail PipelineStepSummary regex.
    await emitter.mark_failed(
        step_id, error_code="upstream_api_error", error_message="oops"
    )
    with pytest.raises(Exception):
        emitter.current_snapshot()  # PipelineStepSummary regex fires here


# --------------------------------------------------------------------------- #
# 4. loaded_layers accumulation
# --------------------------------------------------------------------------- #


def _make_layer(uri: str, layer_id: str = "L1") -> LayerURI:
    return LayerURI(
        layer_id=layer_id,
        name="Demo DEM",
        layer_type="raster",
        uri=uri,
        style_preset="dem-default",
    )


@pytest.mark.asyncio
async def test_loaded_layers_accumulation_via_layer_uri_return(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """Calling ``add_loaded_layer`` directly OR returning a ``LayerURI`` from
    ``emit_tool_call`` should both grow ``loaded_layers`` and emit a fresh
    session-state envelope (A.7)."""
    layer = _make_layer("gs://b/dem.tif", layer_id="dem_1")
    await emitter.add_loaded_layer(layer)

    sess_frames = _session_frames(sink)
    assert len(sess_frames) == 1
    layers = sess_frames[-1]["payload"]["loaded_layers"]
    assert len(layers) == 1
    assert layers[0]["uri"] == "gs://b/dem.tif"
    assert layers[0]["layer_type"] == "raster"

    # Second distinct layer → both present in next emission.
    layer2 = _make_layer("gs://b/pop.fgb", layer_id="pop_1")
    await emitter.add_loaded_layer(layer2)
    sess_frames = _session_frames(sink)
    assert len(sess_frames) == 2
    uris = [layer["uri"] for layer in sess_frames[-1]["payload"]["loaded_layers"]]
    assert uris == ["gs://b/dem.tif", "gs://b/pop.fgb"]


@pytest.mark.asyncio
async def test_emit_tool_call_layer_uri_return_funnels_to_loaded_layers(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """End-to-end: a tool that returns a ``LayerURI`` from inside
    ``emit_tool_call`` causes a ``session-state`` envelope to be emitted.

    STUCK-RUNNING-CARD FIX: the terminal ``pipeline-state(complete)`` frame is
    now emitted BEFORE the ``session-state`` side-effect of
    ``add_loaded_layer``. Previously add_loaded_layer ran first and its
    session-state snapshot captured the step while STILL "running" — that
    snapshot could land at/after the terminal frame and leave the tool card
    stuck "running" forever.

    job-0254: ``emit_tool_call`` routes the returned ``LayerURI`` through the
    ``layer_uri_emit`` seam before ``add_loaded_layer``. A renderable raster
    carries a WMS ``http(s)`` URL (post-publish, the realistic shape), which the
    seam passes through — so the funnel still fires. (The raster-with-raw-
    ``gs://`` drop path is covered by ``test_emit_tool_call_drops_raster_gs_uri``
    below and in ``test_layer_uri_emit.py``.)"""
    layer = LayerURI(
        layer_id="dem_1",
        name="Demo DEM",
        layer_type="raster",
        uri="https://qgis.run.app/wms?LAYERS=dem_1",
        style_preset="dem-default",
    )

    def fake_tool() -> LayerURI:
        return layer

    result = await emitter.emit_tool_call(
        name="Fetch DEM", tool_name="fetch_dem", invoke=fake_tool
    )
    assert result is layer
    assert any(f["type"] == "session-state" for f in sink.frames)
    # Frame ordering: pending, running, complete (terminal frame FIRST),
    # then session-state (add_loaded_layer side-effect).
    types = [f["type"] for f in sink.frames]
    assert types == [
        "pipeline-state",  # pending
        "pipeline-state",  # running
        "pipeline-state",  # complete (terminal frame emitted BEFORE the layer)
        "session-state",  # add_loaded_layer side-effect
    ]


@pytest.mark.asyncio
async def test_emit_tool_call_layer_uri_terminal_frame_before_session_state(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """STUCK-RUNNING-CARD REGRESSION GUARD (compute_hillshade et al.).

    For a ``compute_*`` tool that returns a renderable raster ``LayerURI``, the
    terminal ``pipeline-state`` frame with ``state="complete"`` MUST be emitted
    BEFORE the ``session-state`` frame that ``add_loaded_layer`` produces, and
    the FINAL emitted pipeline-state for the step MUST be ``"complete"`` (never
    left at ``"running"``). This is the bug NATE hit: the hillshade layer
    renders but the card stays stuck "Computing hillshade..." forever because a
    running-snapshot session-state arrived after the terminal frame.
    """
    # compute_hillshade publishes a WMS-URL raster (post-publish shape) so the
    # layer_uri_emit seam passes it through to add_loaded_layer.
    layer = LayerURI(
        layer_id="hillshade_1",
        name="Hillshade",
        layer_type="raster",
        uri="https://qgis.run.app/wms?LAYERS=hillshade_1",
        style_preset="hillshade",
    )

    def compute_hillshade() -> LayerURI:
        return layer

    result = await emitter.emit_tool_call(
        name="Computing hillshade", tool_name="compute_hillshade",
        invoke=compute_hillshade,
    )
    assert result is layer

    # The terminal complete frame is emitted strictly BEFORE the session-state.
    types = [f["type"] for f in sink.frames]
    complete_idx = next(
        i for i, f in enumerate(sink.frames)
        if f["type"] == "pipeline-state"
        and f["payload"]["steps"][-1]["state"] == "complete"
    )
    session_idx = types.index("session-state")
    assert complete_idx < session_idx, (
        f"terminal complete frame (idx {complete_idx}) must precede the "
        f"session-state frame (idx {session_idx}); ordering={types}"
    )

    # The FINAL pipeline-state for the step is "complete" — never stuck running.
    last_pipeline = _pipeline_frames(sink)[-1]
    assert last_pipeline["payload"]["steps"][-1]["state"] == "complete"

    # Belt-and-suspenders: the session-state snapshot itself captures the step
    # as terminal (complete), not "running" — that running-snapshot is the exact
    # frame that used to clobber the card back to running.
    last_session = _session_frames(sink)[-1]
    current = last_session["payload"]["current_pipeline"]
    assert current is not None
    assert current["steps"][-1]["state"] == "complete"

    # The layer still reached the accumulator (render path intact).
    assert len(emitter.loaded_layers) == 1
    assert emitter.loaded_layers[0].layer_id == "hillshade_1"


@pytest.mark.asyncio
async def test_emit_tool_call_drops_raster_gs_uri(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """job-0254 §1+§2 integration: a tool that returns a renderable raster
    ``LayerURI`` with a raw ``gs://`` uri (the publish-failure degraded path)
    is DROPPED by the ``layer_uri_emit`` seam — NO ``session-state`` is
    emitted (no broken layer row), the step still completes, and the tool
    result is returned UNCHANGED so narration/retry can act on it."""
    leaked = _make_layer("gs://b/flood_depth_peak.tif", layer_id="flood_1")

    def fake_tool() -> LayerURI:
        return leaked

    result = await emitter.emit_tool_call(
        name="Flood scenario", tool_name="run_model_flood_scenario", invoke=fake_tool
    )
    # The LLM-visible tool result is unchanged (retry contract preserved).
    assert result is leaked
    # No layer reached the accumulator; no session-state was emitted.
    assert emitter.loaded_layers == []
    assert not any(f["type"] == "session-state" for f in sink.frames)
    # The step still completes cleanly (the drop is not a tool failure).
    types = [f["type"] for f in sink.frames]
    assert types == [
        "pipeline-state",  # pending
        "pipeline-state",  # running
        "pipeline-state",  # complete (no session-state in between)
    ]


# --------------------------------------------------------------------------- #
# 5. current_pipeline set + cleared
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_current_pipeline_set_and_cleared(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """``session-state.current_pipeline`` is non-null while a pipeline is
    running (cross-envelope predicate (b) from job-0026), and is ``None``
    after ``close_pipeline``."""
    step_id = await emitter.add_step(name="Geocode", tool_name="geocode_location")
    await emitter.mark_running(step_id)
    await emitter.emit_session_state()

    last = _session_frames(sink)[-1]
    assert last["payload"]["current_pipeline"] is not None
    assert last["payload"]["current_pipeline"]["pipeline_id"] == emitter.pipeline_id

    await emitter.mark_complete(step_id)
    emitter.close_pipeline()
    await emitter.emit_session_state()

    last = _session_frames(sink)[-1]
    assert last["payload"]["current_pipeline"] is None
    assert emitter.pipeline_id is None


# --------------------------------------------------------------------------- #
# 6. Cancel propagation (Invariant 8)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cancel_propagation_emits_cancelled_state(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """``asyncio.CancelledError`` inside the wrapped tool flips the step to
    ``cancelled`` (distinct from failed) and re-raises. Honors Invariant 8."""

    async def cancelling_tool() -> Any:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await emitter.emit_tool_call(
            name="Long fetch", tool_name="fetch_dem", invoke=cancelling_tool
        )
    frames = _pipeline_frames(sink)
    last_state = frames[-1]["payload"]["steps"][-1]["state"]
    assert last_state == "cancelled"


@pytest.mark.asyncio
async def test_error_classifier_buckets_known_exception_types(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """Exception classification feeds the open-set A.6 error-code registry.
    ConnectionError → UPSTREAM_API_ERROR (covers job-0033 fetcher failures)."""

    def boom() -> None:
        raise ConnectionError("upstream 503")

    with pytest.raises(ConnectionError):
        await emitter.emit_tool_call(
            name="Fetch", tool_name="fetch_dem", invoke=boom
        )
    snap = emitter.current_snapshot()
    assert snap is not None
    failed = snap.steps[-1]
    assert failed.state == "failed"
    assert failed.error_code == "UPSTREAM_API_ERROR"


# --------------------------------------------------------------------------- #
# 7. loaded_layers dedup
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_loaded_layers_dedup_by_uri(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """Dedup policy: by ``uri`` (TENTATIVE per kickoff). Re-fetching the same
    layer with refreshed metadata replaces in place rather than duplicating."""
    layer = _make_layer("gs://b/dem.tif", layer_id="dem_1")
    await emitter.add_loaded_layer(layer)

    # Same uri, different style_preset on a re-fetch
    refreshed = LayerURI(
        layer_id="dem_1",
        name="Demo DEM (refreshed)",
        layer_type="raster",
        uri="gs://b/dem.tif",
        style_preset="dem-bluescale",
    )
    await emitter.add_loaded_layer(refreshed)

    layers = emitter.loaded_layers
    assert len(layers) == 1
    assert layers[0].style_preset == "dem-bluescale"
    assert layers[0].name == "Demo DEM (refreshed)"


# --------------------------------------------------------------------------- #
# 8. No merge helper (structural A.7 enforcement)
# --------------------------------------------------------------------------- #


def test_no_merge_helper_exists() -> None:
    """A.7 replace-not-reconcile is structurally enforced: the emitter must
    expose no merge / apply_delta / update_partial method. A future PR that
    accidentally adds one will fail this test."""
    forbidden = {"merge", "apply_delta", "update_partial", "reconcile"}
    methods = {name for name in dir(PipelineEmitter) if not name.startswith("_")}
    overlap = methods & forbidden
    assert not overlap, (
        f"PipelineEmitter exposes forbidden helper(s) {sorted(overlap)} — "
        "Appendix A.7 mandates replace-not-reconcile, structurally enforced "
        "by NOT shipping a merge-style API. Remove or rename."
    )


# --------------------------------------------------------------------------- #
# 9. Vector inline-GeoJSON (job-0175)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_vector_layer_inlines_geojson_into_session_state(
    emitter: PipelineEmitter, sink: _CapturingSink, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a tool returns a vector LayerURI, the emitter reads bytes from
    GCS, parses, and embeds the result on the wire as ``inline_geojson``."""
    fake_fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
                "properties": {"event": "Flood Warning"},
            }
        ],
    }

    async def fake_reader(uri: str):
        assert uri == "gs://b/alerts.fgb"
        return fake_fc

    monkeypatch.setattr(
        "grace2_agent.pipeline_emitter._read_vector_uri_as_geojson", fake_reader,
    )

    vector_layer = LayerURI(
        layer_id="nws-conus-all",
        name="NWS Alerts CONUS",
        layer_type="vector",
        uri="gs://b/alerts.fgb",
        style_preset="nws_alerts",
    )
    await emitter.add_loaded_layer(vector_layer)

    sess_frames = _session_frames(sink)
    assert len(sess_frames) == 1
    layers = sess_frames[-1]["payload"]["loaded_layers"]
    assert len(layers) == 1
    assert "inline_geojson" in layers[0]
    assert layers[0]["inline_geojson"]["type"] == "FeatureCollection"
    assert len(layers[0]["inline_geojson"]["features"]) == 1
    assert layers[0]["uri"] == "gs://b/alerts.fgb"


@pytest.mark.asyncio
async def test_vector_layer_inline_geojson_failure_is_non_fatal(
    emitter: PipelineEmitter, sink: _CapturingSink, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the GCS read / parse fails, the layer still lands; the wire
    payload omits ``inline_geojson``."""

    async def boom(uri: str):
        raise RuntimeError("simulated GCS failure")

    monkeypatch.setattr(
        "grace2_agent.pipeline_emitter._read_vector_uri_as_geojson", boom,
    )

    vector_layer = LayerURI(
        layer_id="nws-fail",
        name="NWS Alerts (broken)",
        layer_type="vector",
        uri="gs://b/missing.fgb",
        style_preset="nws_alerts",
    )
    await emitter.add_loaded_layer(vector_layer)

    sess_frames = _session_frames(sink)
    assert len(sess_frames) == 1
    layers = sess_frames[-1]["payload"]["loaded_layers"]
    assert len(layers) == 1
    assert "inline_geojson" not in layers[0]


@pytest.mark.asyncio
async def test_raster_layer_does_not_trigger_inline_path(
    emitter: PipelineEmitter, sink: _CapturingSink, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raster layers don't pass through the inline path."""
    calls: list[str] = []

    async def fake_reader(uri: str):
        calls.append(uri)
        return None

    monkeypatch.setattr(
        "grace2_agent.pipeline_emitter._read_vector_uri_as_geojson", fake_reader,
    )

    raster_layer = LayerURI(
        layer_id="dem_1",
        name="Demo DEM",
        layer_type="raster",
        uri="gs://b/dem.tif",
        style_preset="dem-default",
    )
    await emitter.add_loaded_layer(raster_layer)

    assert calls == []
    sess_frames = _session_frames(sink)
    layers = sess_frames[-1]["payload"]["loaded_layers"]
    assert "inline_geojson" not in layers[0]


@pytest.mark.asyncio
async def test_reset_loaded_layers_clears_inline_table(
    emitter: PipelineEmitter, sink: _CapturingSink, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``reset_loaded_layers`` flushes the inline-GeoJSON side-table."""

    async def fake_reader(uri: str):
        return {"type": "FeatureCollection", "features": []}

    monkeypatch.setattr(
        "grace2_agent.pipeline_emitter._read_vector_uri_as_geojson", fake_reader,
    )

    vector_layer = LayerURI(
        layer_id="a",
        name="A",
        layer_type="vector",
        uri="gs://b/a.fgb",
        style_preset="nws_alerts",
    )
    await emitter.add_loaded_layer(vector_layer)
    emitter.reset_loaded_layers([])
    assert emitter.loaded_layers == []
    await emitter.emit_session_state()
    last = _session_frames(sink)[-1]
    assert last["payload"]["loaded_layers"] == []


# --------------------------------------------------------------------------- #
# duration_ms stamping (job-0264, ELEVATED tool-timer requirement)
# --------------------------------------------------------------------------- #


def _stub_clock(emitter: PipelineEmitter, instants: list) -> None:
    """Patch the emitter's ``_now_fn`` to return ``instants`` in order, then
    repeat the last instant forever (so any extra clock reads don't IndexError).
    Pass timezone-aware UTC ``datetime`` objects."""
    seq = list(instants)
    idx = {"i": 0}

    def _fn():
        i = idx["i"]
        if i < len(seq):
            idx["i"] = i + 1
            return seq[i]
        return seq[-1]

    emitter._now_fn = staticmethod(_fn)  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_complete_stamps_authoritative_duration_ms(
    session_id: str, sink: _CapturingSink
) -> None:
    """On the complete transition the step carries duration_ms = the
    wall-clock elapsed time between mark_running and mark_complete."""
    from datetime import datetime, timezone

    emitter = PipelineEmitter(session_id=session_id, sink=sink)
    # add_step (start_pipeline + step), mark_running (started_at),
    # mark_complete (completed_at). Clock reads: pipeline_started, started_at,
    # completed_at — give a 2m34s gap between running and complete.
    t0 = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
    t_run = datetime(2026, 6, 10, 12, 0, 1, tzinfo=timezone.utc)
    t_done = datetime(2026, 6, 10, 12, 2, 35, tzinfo=timezone.utc)  # +154s from t_run
    _stub_clock(emitter, [t0, t_run, t_done])

    step_id = await emitter.add_step(name="run_sfincs", tool_name="run_solver")
    await emitter.mark_running(step_id)
    await emitter.mark_complete(step_id)

    last = _pipeline_frames(sink)[-1]["payload"]["steps"][-1]
    assert last["state"] == "complete"
    assert last["duration_ms"] == 154_000


@pytest.mark.asyncio
async def test_failed_stamps_duration_ms(
    session_id: str, sink: _CapturingSink
) -> None:
    """A failed step also carries the elapsed-before-failure duration_ms."""
    from datetime import datetime, timezone

    emitter = PipelineEmitter(session_id=session_id, sink=sink)
    t0 = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
    t_run = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
    t_fail = datetime(2026, 6, 10, 12, 0, 5, 500_000, tzinfo=timezone.utc)  # +5.5s
    _stub_clock(emitter, [t0, t_run, t_fail])

    step_id = await emitter.add_step(name="fetch_dem", tool_name="fetch_dem")
    await emitter.mark_running(step_id)
    await emitter.mark_failed(step_id, error_code="UPSTREAM_API_ERROR", error_message="503")

    last = _pipeline_frames(sink)[-1]["payload"]["steps"][-1]
    assert last["state"] == "failed"
    assert last["duration_ms"] == 5_500


@pytest.mark.asyncio
async def test_cancelled_stamps_duration_ms(
    session_id: str, sink: _CapturingSink
) -> None:
    """A cancelled step carries the elapsed-before-cancel duration_ms so the
    yellow card locks rather than ticking forever."""
    from datetime import datetime, timezone

    emitter = PipelineEmitter(session_id=session_id, sink=sink)
    t0 = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
    t_run = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
    t_cancel = datetime(2026, 6, 10, 12, 0, 12, tzinfo=timezone.utc)  # +12s
    _stub_clock(emitter, [t0, t_run, t_cancel])

    step_id = await emitter.add_step(name="long_fetch", tool_name="fetch_dem")
    await emitter.mark_running(step_id)
    await emitter.mark_cancelled(step_id)

    last = _pipeline_frames(sink)[-1]["payload"]["steps"][-1]
    assert last["state"] == "cancelled"
    assert last["duration_ms"] == 12_000


@pytest.mark.asyncio
async def test_pending_and_running_have_no_duration_ms(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """duration_ms is None while pending and running — only the terminal
    transition stamps it. The cosmetic client ticker fills the gap."""
    step_id = await emitter.add_step(name="run_sfincs", tool_name="run_solver")
    pending = _pipeline_frames(sink)[-1]["payload"]["steps"][-1]
    assert pending["state"] == "pending"
    assert pending["duration_ms"] is None

    await emitter.mark_running(step_id)
    running = _pipeline_frames(sink)[-1]["payload"]["steps"][-1]
    assert running["state"] == "running"
    assert running["duration_ms"] is None


@pytest.mark.asyncio
async def test_zero_duration_for_subsecond_tool(
    session_id: str, sink: _CapturingSink
) -> None:
    """A tool that completes within the same instant reports duration_ms == 0
    (honest, not None) — the contract is ge=0."""
    from datetime import datetime, timezone

    emitter = PipelineEmitter(session_id=session_id, sink=sink)
    t = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
    _stub_clock(emitter, [t, t, t])

    step_id = await emitter.add_step(name="geocode", tool_name="geocode_location")
    await emitter.mark_running(step_id)
    await emitter.mark_complete(step_id)

    last = _pipeline_frames(sink)[-1]["payload"]["steps"][-1]
    assert last["duration_ms"] == 0


@pytest.mark.asyncio
async def test_emit_tool_call_stamps_duration_end_to_end(
    session_id: str, sink: _CapturingSink
) -> None:
    """The full emit_tool_call wrapper stamps a non-negative duration_ms on
    the terminal complete frame (integration of the seam server.py drives)."""
    emitter = PipelineEmitter(session_id=session_id, sink=sink)

    async def tool() -> str:
        return "ok"

    await emitter.emit_tool_call(name="fetch_dem", tool_name="fetch_dem", invoke=tool)
    last = _pipeline_frames(sink)[-1]["payload"]["steps"][-1]
    assert last["state"] == "complete"
    assert last["duration_ms"] is not None
    assert last["duration_ms"] >= 0


# --------------------------------------------------------------------------- #
# job-0254 §3 — byte-identical emission when SIGNED_URLS is absent
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_emit_byte_identical_with_seam_for_passing_layers(
    session_id: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For a PASSING layer (WMS raster), routing through the seam in
    ``emit_tool_call`` produces a ``session-state`` payload byte-identical to
    calling ``add_loaded_layer`` directly (pre-seam path). SIGNED_URLS absent
    must be a true no-op on the wire."""
    monkeypatch.delenv("SIGNED_URLS", raising=False)

    layer = LayerURI(
        layer_id="dem_1",
        name="Demo DEM",
        layer_type="raster",
        uri="https://qgis.run.app/wms?LAYERS=dem_1",
        style_preset="dem-default",
    )

    # Path A: seam-routed (through emit_tool_call's isinstance gate).
    sink_seam = _CapturingSink()
    em_seam = PipelineEmitter(session_id=session_id, sink=sink_seam)
    await em_seam.emit_tool_call(
        name="Fetch DEM", tool_name="fetch_dem", invoke=lambda: layer
    )
    seam_session = [f for f in sink_seam.frames if f["type"] == "session-state"]
    assert seam_session, "seam path emitted no session-state"
    seam_loaded = seam_session[-1]["payload"]["loaded_layers"]

    # Path B: direct add_loaded_layer (bypasses the seam entirely).
    sink_direct = _CapturingSink()
    em_direct = PipelineEmitter(session_id=session_id, sink=sink_direct)
    await em_direct.add_loaded_layer(layer)
    direct_session = [
        f for f in sink_direct.frames if f["type"] == "session-state"
    ]
    direct_loaded = direct_session[-1]["payload"]["loaded_layers"]

    # The loaded_layers wire dicts are byte-identical (seam is a no-op for a
    # passing layer when SIGNED_URLS is absent).
    assert seam_loaded == direct_loaded
    assert seam_loaded[0]["uri"] == "https://qgis.run.app/wms?LAYERS=dem_1"


@pytest.mark.asyncio
async def test_emit_byte_identical_under_signed_urls_true(
    session_id: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGNED_URLS=true is dormant: the emitted ``session-state.loaded_layers``
    payload is identical to SIGNED_URLS absent (only a WARNING is logged)."""
    layer = LayerURI(
        layer_id="dem_2",
        name="Demo DEM 2",
        layer_type="raster",
        uri="https://qgis.run.app/wms?LAYERS=dem_2",
        style_preset="dem-default",
    )

    monkeypatch.delenv("SIGNED_URLS", raising=False)
    sink_absent = _CapturingSink()
    em_absent = PipelineEmitter(session_id=session_id, sink=sink_absent)
    await em_absent.emit_tool_call(
        name="Fetch DEM", tool_name="fetch_dem", invoke=lambda: layer
    )
    absent_loaded = [
        f for f in sink_absent.frames if f["type"] == "session-state"
    ][-1]["payload"]["loaded_layers"]

    monkeypatch.setenv("SIGNED_URLS", "true")
    sink_true = _CapturingSink()
    em_true = PipelineEmitter(session_id=session_id, sink=sink_true)
    await em_true.emit_tool_call(
        name="Fetch DEM", tool_name="fetch_dem", invoke=lambda: layer
    )
    true_loaded = [
        f for f in sink_true.frames if f["type"] == "session-state"
    ][-1]["payload"]["loaded_layers"]

    assert absent_loaded == true_loaded


# --------------------------------------------------------------------------- #
# Terminal-on-RETURN (terminal-pipeline-card hardening) — a tool/workflow that
# FAILS or is CANCELLED yet RETURNS (the solver poll path) must flip the card
# to failed/cancelled, NOT green. Kills NATE's "silent green on a dead solve" +
# "card spins forever then mislabels success" symptom.
# --------------------------------------------------------------------------- #


def _run_result(status: str, **kw: Any):
    """Build a RunResult with the given terminal status (duck-typed shape)."""
    from grace2_contracts.execution import RunResult

    return RunResult(
        run_id=new_ulid(),
        handle_id=new_ulid(),
        status=status,  # type: ignore[arg-type]
        **kw,
    )


@pytest.mark.asyncio
async def test_emit_tool_call_failed_runresult_marks_card_failed(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """A tool that RETURNS a RunResult(status='failed') -> the card is FAILED
    (red), never complete (green). The result is returned unchanged so the
    LLM/retry loop can act."""
    rr = _run_result("failed", error_code="SOLVER_FAILED", error_message="diverged")

    result = await emitter.emit_tool_call(
        name="Flood scenario", tool_name="run_model_flood_scenario", invoke=lambda: rr
    )
    assert result is rr
    last = _pipeline_frames(sink)[-1]
    step = last["payload"]["steps"][0]
    assert step["state"] == "failed", step
    # No 'complete' frame was ever emitted for this step.
    states = [f["payload"]["steps"][0]["state"] for f in _pipeline_frames(sink)]
    assert "complete" not in states, states


@pytest.mark.asyncio
async def test_emit_tool_call_timeout_runresult_marks_card_failed(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """SOLVER_TIMEOUT RunResult (the wait_for_completion local-docker timeout
    branch) RETURNS rather than raises -> card FAILED, carries the code."""
    rr = _run_result(
        "failed", error_code="SOLVER_TIMEOUT", error_message="exceeded budget"
    )
    await emitter.emit_tool_call(
        name="Flood scenario", tool_name="run_model_flood_scenario", invoke=lambda: rr
    )
    last = _pipeline_frames(sink)[-1]
    assert last["payload"]["steps"][0]["state"] == "failed"


@pytest.mark.asyncio
async def test_emit_tool_call_cancelled_runresult_marks_card_cancelled(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """A RunResult(status='cancelled') (supervisor wrote a cancelled
    completion.json, wait_for_completion RETURNED it) -> card CANCELLED
    (yellow), distinct from failed per Invariant 8."""
    rr = _run_result("cancelled", cancellation_reason="user stop")
    await emitter.emit_tool_call(
        name="Flood scenario", tool_name="run_model_flood_scenario", invoke=lambda: rr
    )
    last = _pipeline_frames(sink)[-1]
    assert last["payload"]["steps"][0]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_emit_tool_call_complete_runresult_marks_card_complete(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """REGRESSION GUARD: a healthy RunResult(status='complete') still marks the
    card complete (green). The detector must NEVER mislabel a good run."""
    rr = _run_result("complete", output_uri="s3://bucket/run/out/")
    await emitter.emit_tool_call(
        name="Flood scenario", tool_name="run_model_flood_scenario", invoke=lambda: rr
    )
    last = _pipeline_frames(sink)[-1]
    assert last["payload"]["steps"][0]["state"] == "complete"


@pytest.mark.asyncio
async def test_emit_tool_call_failed_envelope_dict_marks_card_failed(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """The flood composer returns a typed failed AssessmentEnvelope whose
    ``workflow_name`` carries the ``:FAILED:<CODE>`` honesty anchor. As a dict
    (model_dump) it must flip the card to FAILED — the silent-green mislabel for
    the flood path (Gap 2)."""
    env_dict = {
        "envelope_type": "modeled",
        "hazard_type": "flood",
        "workflow_name": "model_flood_scenario:FAILED:SOLVER_TIMEOUT",
        "layers": [],
    }
    await emitter.emit_tool_call(
        name="Flood scenario",
        tool_name="run_model_flood_scenario",
        invoke=lambda: env_dict,
    )
    last = _pipeline_frames(sink)[-1]
    step = last["payload"]["steps"][0]
    assert step["state"] == "failed", step


@pytest.mark.asyncio
async def test_emit_tool_call_cancelled_envelope_dict_marks_card_cancelled(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """A failed flood envelope tagged :FAILED:CANCELLED maps to the CANCELLED
    (yellow) card, not failed — honest cancel surfacing."""
    env_dict = {
        "envelope_type": "modeled",
        "workflow_name": "model_flood_scenario:FAILED:CANCELLED",
        "layers": [],
    }
    await emitter.emit_tool_call(
        name="Flood scenario",
        tool_name="run_model_flood_scenario",
        invoke=lambda: env_dict,
    )
    last = _pipeline_frames(sink)[-1]
    assert last["payload"]["steps"][0]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_emit_tool_call_error_status_dict_marks_card_failed(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """The MODFLOW tool returns a raw {'status': 'error', ...} dict on the
    killed/timed-out solver path. It must flip the card to FAILED."""
    err = {
        "status": "error",
        "error_code": "SOLVER_FAILED",
        "error_message": "MODFLOW did not complete",
    }
    await emitter.emit_tool_call(
        name="MODFLOW run", tool_name="run_modflow_job", invoke=lambda: err
    )
    last = _pipeline_frames(sink)[-1]
    step = last["payload"]["steps"][0]
    assert step["state"] == "failed", step


@pytest.mark.asyncio
async def test_emit_tool_call_ok_dict_marks_card_complete(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """REGRESSION GUARD: a normal tool returning a dict WITHOUT a failure
    status/anchor still completes (green). No false-positive failures."""
    ok = {"status": "ok", "value": 42, "rows": [1, 2, 3]}
    await emitter.emit_tool_call(
        name="Zonal stats", tool_name="compute_zonal_statistics", invoke=lambda: ok
    )
    last = _pipeline_frames(sink)[-1]
    assert last["payload"]["steps"][0]["state"] == "complete"


@pytest.mark.asyncio
async def test_emit_tool_call_plain_dict_no_status_marks_card_complete(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """REGRESSION GUARD: a plain result dict with no ``status`` field at all is
    treated as success (the conservative default)."""
    res = {"count": 7, "table": [{"a": 1}]}
    await emitter.emit_tool_call(
        name="Aggregate", tool_name="aggregate_claims", invoke=lambda: res
    )
    last = _pipeline_frames(sink)[-1]
    assert last["payload"]["steps"][0]["state"] == "complete"


# Unit-level coverage of the classifier itself.


def test_classify_tool_return_recognizes_all_failed_shapes() -> None:
    from grace2_agent.pipeline_emitter import _classify_tool_return

    # RunResult shapes.
    assert _classify_tool_return(_run_result("failed", error_code="X"))[0] == "failed"
    assert _classify_tool_return(_run_result("cancelled"))[0] == "cancelled"
    assert _classify_tool_return(_run_result("complete")) is None
    # Dict shapes.
    assert _classify_tool_return({"status": "error"})[0] == "failed"
    assert _classify_tool_return({"status": "cancelled"})[0] == "cancelled"
    assert _classify_tool_return({"status": "ok"}) is None
    # Failed-envelope dict via :FAILED: anchor.
    fenv = {"workflow_name": "x:FAILED:SOLVER_FAILED", "layers": []}
    cls = _classify_tool_return(fenv)
    assert cls is not None and cls[0] == "failed" and cls[1] == "SOLVER_FAILED"
    # Non-failure shapes -> None (conservative).
    assert _classify_tool_return({"foo": "bar"}) is None
    assert _classify_tool_return("a string") is None
    assert _classify_tool_return(None) is None
    assert _classify_tool_return(42) is None


@pytest.mark.asyncio
async def test_update_current_progress_targets_running_step(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """``update_current_progress`` bumps the running step without a step_id;
    no-op (no crash) when nothing is running."""
    # No running step yet -> best-effort no-op (no frame, no raise).
    await emitter.update_current_progress(10)
    assert _pipeline_frames(sink) == []

    step_id = await emitter.add_step(name="Build", tool_name="build_sfincs_model")
    await emitter.mark_running(step_id)
    await emitter.update_current_progress(33)
    last = _pipeline_frames(sink)[-1]
    assert last["payload"]["steps"][0]["progress_percent"] == 33


# --------------------------------------------------------------------------- #
# tool-io sidecar (tool-card-expand-output spec)
# --------------------------------------------------------------------------- #


def _tool_io_frames(sink: _CapturingSink) -> list[dict[str, Any]]:
    return [f for f in sink.frames if f["type"] == "tool-io"]


@pytest.mark.asyncio
async def test_emit_tool_io_carries_args_and_response(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """``emit_tool_io`` emits a ``tool-io`` envelope carrying the json-dumped
    raw args + function_response keyed by the dispatch's step_id."""
    step_id = await emitter.add_step(name="Geocode", tool_name="geocode_location")
    await emitter.emit_tool_io(
        step_id=step_id,
        tool_name="geocode_location",
        raw_args={"location_name": "Boulder, CO"},
        function_response={"status": "ok", "bbox": [-105.3, 39.9, -105.1, 40.1]},
        is_error=False,
    )
    frames = _tool_io_frames(sink)
    assert len(frames) == 1, frames
    p = frames[0]["payload"]
    assert p["step_id"] == step_id
    assert p["tool_name"] == "geocode_location"
    assert p["is_error"] is False
    assert p["args_truncated"] is False
    assert p["response_truncated"] is False
    # Round-trips back to the original objects (pretty-printed JSON strings).
    assert json.loads(p["raw_args"]) == {"location_name": "Boulder, CO"}
    assert json.loads(p["function_response"]) == {
        "status": "ok",
        "bbox": [-105.3, 39.9, -105.1, 40.1],
    }


@pytest.mark.asyncio
async def test_emit_tool_io_error_flag(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """A typed-error function_response is flagged ``is_error`` so the web styles
    the response block red without re-parsing the JSON."""
    step_id = await emitter.add_step(name="Fetch fires", tool_name="fetch_firms_active_fire")
    await emitter.emit_tool_io(
        step_id=step_id,
        tool_name="fetch_firms_active_fire",
        raw_args={"bbox": [0, 0, 1, 1]},
        function_response={
            "status": "error",
            "error_code": "UPSTREAM_API_ERROR",
            "message": "FIRMS returned 503",
        },
        is_error=True,
    )
    p = _tool_io_frames(sink)[-1]["payload"]
    assert p["is_error"] is True
    assert json.loads(p["function_response"])["error_code"] == "UPSTREAM_API_ERROR"


@pytest.mark.asyncio
async def test_emit_tool_io_truncates_large_payload(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """A function_response over the per-field byte cap is truncated, the flag is
    set, and the ORIGINAL byte length is reported (honest 'truncated, N bytes')."""
    from grace2_contracts.ws import ToolIoPayload

    big = {"rows": ["x" * 1000 for _ in range(200)]}  # well over the 32KB cap
    step_id = await emitter.add_step(name="Query", tool_name="mongo_query")
    await emitter.emit_tool_io(
        step_id=step_id,
        tool_name="mongo_query",
        raw_args={"q": "find"},
        function_response=big,
        is_error=False,
    )
    p = _tool_io_frames(sink)[-1]["payload"]
    assert p["response_truncated"] is True
    # The shipped string is bounded by the cap; the reported size is the original.
    assert len(p["function_response"].encode("utf-8")) <= ToolIoPayload.MAX_FIELD_BYTES
    assert p["response_bytes"] > ToolIoPayload.MAX_FIELD_BYTES
    # Small args are NOT truncated.
    assert p["args_truncated"] is False


@pytest.mark.asyncio
async def test_emit_tool_io_non_serializable_degrades_to_str(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """A non-JSON-serializable value degrades to its str() rather than raising;
    the envelope is still emitted."""

    class _Weird:
        def __repr__(self) -> str:
            return "<Weird object>"

    step_id = await emitter.add_step(name="Tool", tool_name="some_tool")
    await emitter.emit_tool_io(
        step_id=step_id,
        tool_name="some_tool",
        raw_args={"obj": _Weird()},
        function_response=_Weird(),
        is_error=False,
    )
    frames = _tool_io_frames(sink)
    assert len(frames) == 1
    p = frames[0]["payload"]
    # default=str renders the repr inside the JSON; non-serializable never raises.
    assert "Weird" in p["raw_args"]
    assert "Weird" in p["function_response"]


# --------------------------------------------------------------------------- #
# J-B-part-i: terminal-state survives a dead/cycling socket + rebind replay
# --------------------------------------------------------------------------- #


class _ClosingSink:
    """A sink that raises ConnectionClosedError on send (simulates a dead WS).

    Mirrors how ``websocket.send`` blows up on a closed/cycling socket — the
    exact failure that previously aborted a terminal pipeline-state emit and
    LOST the red/green card."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, text: str) -> None:
        from websockets.exceptions import ConnectionClosedError

        self.calls += 1
        raise ConnectionClosedError(None, None)


@pytest.mark.asyncio
async def test_mark_failed_survives_dead_socket(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """J-B-part-i: ``mark_failed`` whose TERMINAL send raises
    ConnectionClosedError still flips the step to ``failed`` and does NOT let
    the exception escape (the red card must record even on a dead socket)."""
    step_id = await emitter.add_step(name="Solve", tool_name="run_solver")
    await emitter.mark_running(step_id)

    # Swap in a dead-socket sink ONLY for the terminal emit so add_step /
    # mark_running succeed normally first.
    emitter._sink = _ClosingSink()

    # No exception escapes despite the underlying ConnectionClosedError.
    await emitter.mark_failed(
        step_id, error_code="SOLVER_DISPATCH_FAILED", error_message="off-box crash"
    )

    # The state transition itself completed: the step is terminal + carries the
    # error code/message even though the wire send failed.
    snap = emitter.current_snapshot()
    assert snap is not None
    assert snap.steps[0].state == "failed"
    assert snap.steps[0].error_code == "SOLVER_DISPATCH_FAILED"
    assert snap.steps[0].error_message == "off-box crash"
    # The terminal snapshot was stashed for replay-on-rebind.
    assert emitter._last_terminal_pipeline_payload is not None


@pytest.mark.asyncio
async def test_mark_complete_survives_dead_socket(
    emitter: PipelineEmitter,
) -> None:
    """J-B-part-i: ``mark_complete`` on a dead socket still flips to
    ``complete`` (green card) without raising."""
    step_id = await emitter.add_step(name="Fetch", tool_name="fetch_dem")
    await emitter.mark_running(step_id)
    emitter._sink = _ClosingSink()

    await emitter.mark_complete(step_id)

    snap = emitter.current_snapshot()
    assert snap is not None
    assert snap.steps[0].state == "complete"


@pytest.mark.asyncio
async def test_terminal_emit_propagates_non_connection_errors(
    emitter: PipelineEmitter,
) -> None:
    """J-B-part-i: the best-effort swallow is NARROW — a REAL logic error from
    the sink (not a connection-closed) still propagates; we only swallow the
    connection-closed class so genuine bugs are never hidden."""

    async def _broken_sink(text: str) -> None:
        raise ValueError("serialization bug, not a dead socket")

    step_id = await emitter.add_step(name="Solve", tool_name="run_solver")
    await emitter.mark_running(step_id)
    emitter._sink = _broken_sink

    with pytest.raises(ValueError, match="serialization bug"):
        await emitter.mark_complete(step_id)

    # The state still flipped before the emit attempt (terminal recorded).
    assert emitter._steps[step_id].state == "complete"


@pytest.mark.asyncio
async def test_rebind_sink_replays_last_terminal_pipeline_state(
    emitter: PipelineEmitter,
) -> None:
    """J-B-part-i: after a terminal transition went out on a (now dead) socket,
    ``rebind_sink`` REPLAYS the last terminal pipeline-state onto the NEW sink so
    the RENDERED/terminal card stays surfaced across a WS blip."""
    step_id = await emitter.add_step(name="Solve", tool_name="run_solver")
    await emitter.mark_running(step_id)

    # Terminal emit goes out on a dead socket (best-effort drop).
    emitter._sink = _ClosingSink()
    await emitter.mark_complete(step_id)

    # A NEW socket connects: rebind. The replay is scheduled as a task, so let
    # the loop run it.
    new_sink = _CapturingSink()
    emitter.rebind_sink(new_sink)
    await asyncio.sleep(0)  # let the scheduled replay task run
    await asyncio.sleep(0)

    replayed = _pipeline_frames(new_sink)
    assert len(replayed) == 1, replayed
    payload = replayed[0]["payload"]
    assert payload["pipeline_id"] == emitter.pipeline_id
    assert payload["steps"][0]["state"] == "complete"


@pytest.mark.asyncio
async def test_rebind_sink_replays_full_live_snapshot_for_open_pipeline(
    emitter: PipelineEmitter,
) -> None:
    """J-B-part-i (FIX 2): an OPEN pipeline that has NOT reached a terminal
    transition still replays its FULL live snapshot on ``rebind_sink`` -- this
    is exactly the fix for the dropped SETUP/dispatch running card. Every step
    in its CURRENT (running) state must reappear on the new sink, not nothing."""
    step_id = await emitter.add_step(name="Solve", tool_name="run_solver")
    await emitter.mark_running(step_id)  # running, NOT terminal

    new_sink = _CapturingSink()
    emitter.rebind_sink(new_sink)
    await asyncio.sleep(0)  # let the scheduled snapshot replay task run
    await asyncio.sleep(0)

    replayed = _pipeline_frames(new_sink)
    assert len(replayed) == 1, replayed
    payload = replayed[0]["payload"]
    assert payload["pipeline_id"] == emitter.pipeline_id
    assert [s["step_id"] for s in payload["steps"]] == [step_id]
    assert payload["steps"][0]["state"] == "running"


@pytest.mark.asyncio
async def test_rebind_sink_no_replay_with_no_open_pipeline(
    emitter: PipelineEmitter,
) -> None:
    """J-B-part-i: with NO open pipeline AND no terminal stash, ``rebind_sink``
    replays nothing (there is no card to repaint)."""
    new_sink = _CapturingSink()
    emitter.rebind_sink(new_sink)
    await asyncio.sleep(0)

    assert _pipeline_frames(new_sink) == []


@pytest.mark.asyncio
async def test_running_emit_swallows_connection_closed_but_records_state(
    emitter: PipelineEmitter,
) -> None:
    """FIX 1: a NON-terminal (running) ``_emit_pipeline_state`` whose underlying
    send raises ConnectionClosedError is swallowed (does NOT propagate) and the
    step's running state is still recorded -- symmetric with the terminal path.

    This is the dropped SETUP/dispatch card: the running frame is lost on the
    dead launch socket, but the in-memory step state survives so a later rebind
    can replay it in full."""
    step_id = await emitter.add_step(name="Setup", tool_name="fetch_dem")

    # Swap in a dead-socket sink ONLY for the running emit (add_step already
    # emitted its pending frame on the live sink).
    closing = _ClosingSink()
    emitter._sink = closing

    # mark_running drives a NON-terminal _emit_pipeline_state. Pre-FIX this
    # raised; post-FIX it is swallowed.
    await emitter.mark_running(step_id)

    # The send was attempted (and swallowed) ...
    assert closing.calls >= 1
    # ... and the running state is still recorded in memory.
    assert emitter._steps[step_id].state == "running"
    snap = emitter.current_snapshot()
    assert snap is not None
    assert snap.steps[0].state == "running"


@pytest.mark.asyncio
async def test_running_emit_propagates_non_connection_errors(
    emitter: PipelineEmitter,
) -> None:
    """FIX 1: the running-path swallow is NARROW -- a REAL logic error from the
    sink (not a connection-closed) on a non-terminal emit still propagates, so
    genuine serialization/logic bugs are never hidden."""

    async def _broken_sink(text: str) -> None:
        raise ValueError("serialization bug, not a dead socket")

    step_id = await emitter.add_step(name="Setup", tool_name="fetch_dem")
    emitter._sink = _broken_sink

    with pytest.raises(ValueError, match="serialization bug"):
        await emitter.mark_running(step_id)


@pytest.mark.asyncio
async def test_rebind_sink_open_pipeline_replays_all_steps_mixed_states(
    emitter: PipelineEmitter,
) -> None:
    """FIX 2 (full-snapshot replay): after several SETUP/dispatch frames were
    dropped on a dead launch socket, ``rebind_sink`` on the still-OPEN pipeline
    replays a SINGLE full snapshot carrying ALL step_ids in their CURRENT state
    (a mix of complete + running) -- not just the last terminal card."""
    # Three steps: a completed setup child, a completed dispatch, a running sim.
    setup_id = await emitter.add_step(name="Fetch DEM", tool_name="fetch_dem")
    await emitter.mark_running(setup_id)
    await emitter.mark_complete(setup_id)

    dispatch_id = await emitter.add_step(name="Dispatch", tool_name="run_solver")
    await emitter.mark_running(dispatch_id)
    await emitter.mark_complete(dispatch_id)

    sim_id = await emitter.add_step(name="Sim", tool_name="wait_for_completion")

    # Simulate the launch socket dying for the sim's running frame (FIX 1 path).
    emitter._sink = _ClosingSink()
    await emitter.mark_running(sim_id)  # running frame dropped, state recorded

    # A NEW socket connects: rebind replays the FULL live snapshot.
    new_sink = _CapturingSink()
    emitter.rebind_sink(new_sink)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    replayed = _pipeline_frames(new_sink)
    assert len(replayed) == 1, replayed
    payload = replayed[0]["payload"]
    assert payload["pipeline_id"] == emitter.pipeline_id

    by_id = {s["step_id"]: s for s in payload["steps"]}
    # ALL three cards are present (not just the last terminal one) ...
    assert set(by_id) == {setup_id, dispatch_id, sim_id}
    # ... each in its CURRENT state.
    assert by_id[setup_id]["state"] == "complete"
    assert by_id[dispatch_id]["state"] == "complete"
    assert by_id[sim_id]["state"] == "running"


# --------------------------------------------------------------------------- #
# Two-card sim observability (task-149)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_add_compute_step_yields_role_compute_bound_to_jobid(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """``add_compute_step`` mints a ``role="compute"`` step bound to the Batch
    jobId, lands it running, and carries the discriminator on the wire."""
    step_id = await emitter.add_compute_step(
        name="sfincs solve",
        tool_name="sfincs:solve",
        batch_job_id="batch-job-xyz",
        batch_status="SUBMITTED",
    )

    frames = _pipeline_frames(sink)
    # The last frame is the mark_running emit; its step carries the compute card.
    step = frames[-1]["payload"]["steps"][-1]
    assert step["step_id"] == step_id
    assert step["role"] == "compute"
    assert step["batch_job_id"] == "batch-job-xyz"
    assert step["batch_status"] == "SUBMITTED"
    assert step["state"] == "running"
    assert step["started_at"] is not None  # mark_running stamped it


@pytest.mark.asyncio
async def test_update_compute_status_patches_batch_status(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """``update_compute_status`` patches ``batch_status`` + re-emits; an
    identical status is a no-op (no duplicate frame), and the ``state`` is
    untouched (terminal transitions own that)."""
    step_id = await emitter.add_compute_step(
        name="sfincs solve",
        tool_name="sfincs:solve",
        batch_job_id="batch-job-xyz",
        batch_status="SUBMITTED",
    )
    n_before = len(_pipeline_frames(sink))

    await emitter.update_compute_status(step_id, "RUNNING")
    frames = _pipeline_frames(sink)
    assert len(frames) == n_before + 1  # one new frame
    step = frames[-1]["payload"]["steps"][-1]
    assert step["batch_status"] == "RUNNING"
    assert step["state"] == "running"  # unchanged

    # Identical status -> no-op (no new frame).
    await emitter.update_compute_status(step_id, "RUNNING")
    assert len(_pipeline_frames(sink)) == n_before + 1

    # Unknown step id -> best-effort no-op (never raises).
    await emitter.update_compute_status("does-not-exist", "RUNNING")
    assert len(_pipeline_frames(sink)) == n_before + 1


@pytest.mark.asyncio
async def test_tool_card_role_defaults_to_tool_backcompat(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """A plain ``add_step`` card carries the default ``role="tool"`` with both
    Batch ids ``None`` (back-compat — byte-identical on the wire)."""
    step_id = await emitter.add_step(name="Geocode", tool_name="geocode_location")
    step = _pipeline_frames(sink)[-1]["payload"]["steps"][-1]
    assert step["role"] == "tool"
    assert step["batch_job_id"] is None
    assert step["batch_status"] is None
    # The persisted summary mirrors the same defaults.
    summary = emitter._to_summary(step_id)
    assert summary.role == "tool"
    assert summary.batch_job_id is None
    assert summary.batch_status is None


@pytest.mark.asyncio
async def test_mint_dispatch_and_sim_cards_emits_two_cards(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """``mint_dispatch_and_sim_cards`` mints a complete Dispatch tool card + a
    running compute card bound to the handle's jobId."""
    from grace2_agent.pipeline_emitter import mint_dispatch_and_sim_cards

    handle = type(
        "H",
        (),
        {"workflows_execution_id": "batch-job-777", "workflow_name": "aws-batch", "solver": "sfincs"},
    )()
    sim_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver="sfincs", handle=handle, compute_class="large"
    )
    assert sim_id is not None

    steps = _pipeline_frames(sink)[-1]["payload"]["steps"]
    # Two cards: a complete tool dispatch + a running compute sim bound to jobId.
    dispatch = [s for s in steps if s["role"] == "tool"]
    compute = [s for s in steps if s["role"] == "compute"]
    assert len(dispatch) == 1 and dispatch[0]["state"] == "complete"
    assert len(compute) == 1
    assert compute[0]["step_id"] == sim_id
    assert compute[0]["batch_job_id"] == "batch-job-777"
    assert compute[0]["state"] == "running"


@pytest.mark.asyncio
async def test_mint_dispatch_and_sim_cards_none_emitter_is_noop() -> None:
    """``emitter is None`` (direct/smoke call) returns ``None`` and emits
    nothing — the two cards are an observability affordance, never required."""
    from grace2_agent.pipeline_emitter import mint_dispatch_and_sim_cards

    handle = type("H", (), {"workflows_execution_id": "j", "solver": "sfincs"})()
    sim_id = await mint_dispatch_and_sim_cards(
        emitter=None, solver="sfincs", handle=handle
    )
    assert sim_id is None


@pytest.mark.asyncio
async def test_route_sim_terminal_marks_complete_and_failed(
    session_id: str,
) -> None:
    """``route_sim_terminal`` drives the compute card green on a complete
    RunResult and red on a non-complete one (carrying its error_code)."""
    from grace2_agent.pipeline_emitter import route_sim_terminal

    # complete -> green
    sink_ok = _CapturingSink()
    em_ok = PipelineEmitter(session_id=session_id, sink=sink_ok)
    sim_ok = await em_ok.add_compute_step(
        name="sfincs solve", tool_name="sfincs:solve", batch_job_id="j1"
    )
    rr_ok = type("R", (), {"status": "complete", "error_code": None, "error_message": None})()
    await route_sim_terminal(em_ok, sim_ok, run_result=rr_ok)
    assert _pipeline_frames(sink_ok)[-1]["payload"]["steps"][-1]["state"] == "complete"

    # non-complete -> red, error_code surfaced
    sink_bad = _CapturingSink()
    em_bad = PipelineEmitter(session_id=session_id, sink=sink_bad)
    sim_bad = await em_bad.add_compute_step(
        name="sfincs solve", tool_name="sfincs:solve", batch_job_id="j2"
    )
    rr_bad = type(
        "R",
        (),
        {"status": "failed", "error_code": "SOLVER_TIMEOUT", "error_message": "ran out of budget"},
    )()
    await route_sim_terminal(em_bad, sim_bad, run_result=rr_bad)
    step = _pipeline_frames(sink_bad)[-1]["payload"]["steps"][-1]
    assert step["state"] == "failed"

    # cancel (run_result is None) -> yellow
    sink_cx = _CapturingSink()
    em_cx = PipelineEmitter(session_id=session_id, sink=sink_cx)
    sim_cx = await em_cx.add_compute_step(
        name="sfincs solve", tool_name="sfincs:solve", batch_job_id="j3"
    )
    await route_sim_terminal(em_cx, sim_cx, run_result=None)
    assert _pipeline_frames(sink_cx)[-1]["payload"]["steps"][-1]["state"] == "cancelled"


# --------------------------------------------------------------------------- #
# 9b. Compaction card (Part A -- compaction UX)
# --------------------------------------------------------------------------- #
#
# Wire-shape / lifecycle coverage against a FAKE (no-persist-hook) emitter --
# see tests/test_compaction_card_persistence.py for the persistence-row-shape
# + full dispatch-loop integration coverage.


class TestCompactionCard:
    @pytest.mark.asyncio
    async def test_mint_compaction_card_is_a_plain_running_tool_card(
        self, emitter: PipelineEmitter, sink: _CapturingSink
    ) -> None:
        """The running card is role="tool" (NEVER "compute" -- there is no
        Batch job bound to a local compaction pass), tool_name
        "context:compact", state running, labeled COMPACTING_LABEL."""
        from grace2_agent.context_budget import COMPACTING_LABEL
        from grace2_agent.pipeline_emitter import mint_compaction_card

        step_id = await mint_compaction_card(emitter=emitter)
        assert step_id is not None

        step = _pipeline_frames(sink)[-1]["payload"]["steps"][-1]
        assert step["step_id"] == step_id
        assert step["role"] == "tool"
        assert step["batch_job_id"] is None
        assert step["batch_status"] is None
        assert step["tool_name"] == "context:compact"
        assert step["name"] == COMPACTING_LABEL
        assert step["state"] == "running"
        assert step["started_at"] is not None

    @pytest.mark.asyncio
    async def test_complete_compaction_card_renames_and_completes(
        self, emitter: PipelineEmitter, sink: _CapturingSink
    ) -> None:
        from grace2_agent.context_budget import compaction_complete_label
        from grace2_agent.pipeline_emitter import (
            complete_compaction_card,
            mint_compaction_card,
        )

        step_id = await mint_compaction_card(emitter=emitter)
        await complete_compaction_card(
            emitter=emitter, step_id=step_id, before_tokens=5000, after_tokens=1200
        )

        step = _pipeline_frames(sink)[-1]["payload"]["steps"][-1]
        assert step["step_id"] == step_id
        assert step["state"] == "complete"
        assert step["name"] == compaction_complete_label(5000, 1200)
        assert step["name"] == "Conversation compacted (5k -> 1k tokens)"
        assert step["role"] == "tool"  # unchanged by the rename
        assert step["tool_name"] == "context:compact"  # unchanged by the rename

    @pytest.mark.asyncio
    async def test_mint_compaction_card_none_emitter_is_noop(self) -> None:
        from grace2_agent.pipeline_emitter import mint_compaction_card

        assert await mint_compaction_card(emitter=None) is None

    @pytest.mark.asyncio
    async def test_complete_compaction_card_none_step_id_is_noop(
        self, emitter: PipelineEmitter, sink: _CapturingSink
    ) -> None:
        """No mint (or a failed mint) -> ``step_id`` is None -> the terminal
        call must never raise and must emit nothing."""
        from grace2_agent.pipeline_emitter import complete_compaction_card

        n_before = len(sink.frames)
        await complete_compaction_card(
            emitter=emitter, step_id=None, before_tokens=100, after_tokens=50
        )
        assert len(sink.frames) == n_before

    def test_rename_step_unknown_id_is_noop(self, emitter: PipelineEmitter) -> None:
        # Must not raise for an id the emitter never minted.
        emitter.rename_step("does-not-exist", name="whatever")

    def test_compaction_complete_label_rounds_and_floors_at_1k(self) -> None:
        from grace2_agent.context_budget import compaction_complete_label

        assert compaction_complete_label(12800, 3900) == "Conversation compacted (13k -> 4k tokens)"
        assert compaction_complete_label(400, 100) == "Conversation compacted (1k -> 1k tokens)"
        assert compaction_complete_label(0, 0) == "Conversation compacted (0k -> 0k tokens)"


# --------------------------------------------------------------------------- #
# 10. Off-loop densify (WS-30s drop-cycle fix)
# --------------------------------------------------------------------------- #
#
# Root cause: ``_read_vector_uri_as_geojson`` read the object off-loop (good) but
# ran the CPU-heavy ``densify_if_needed`` BACK ON the asyncio loop after the
# executor returned. On session-resume the active-case layers are re-inlined +
# re-densified on EVERY ~30s reconnect (observed live: buildings 39509 -> 4000
# each reconnect), so that densify blocked the WS keepalive and fed the 30s drop
# cycle. The densify now runs inside the SAME ``run_in_executor`` thread as the
# read. These tests pin: (a) a large FC still gets densified/capped via the
# off-loop path, (b) the URI-keyed density-meta side-table is still populated +
# bounded, and (c) the densify is actually off-loop (a concurrent loop task is
# not starved while it runs).


def _make_dense_fc(n: int) -> dict[str, Any]:
    """A FeatureCollection with ``n`` small square polygons on a grid.

    Distinct, non-empty geometries so the simplify/cap path runs for real (the
    largest-area cap keeps the first ``MAX_INLINE_FEATURES``).
    """
    feats = []
    for i in range(n):
        x = (i % 100) * 0.001
        y = (i // 100) * 0.001
        feats.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [x, y], [x + 0.0008, y], [x + 0.0008, y + 0.0008],
                        [x, y + 0.0008], [x, y],
                    ]],
                },
                "properties": {"idx": i},
            }
        )
    return {"type": "FeatureCollection", "features": feats}


@pytest.mark.asyncio
async def test_dense_vector_is_densified_via_off_loop_read(
    tmp_path: Any,
) -> None:
    """A FeatureCollection above the dense threshold is read AND densified/capped
    through the off-loop path, and the URI-keyed density-meta side-table is
    populated (so the wire layer can be honestly tagged on re-inline)."""
    from grace2_agent.pipeline_emitter import (
        _LAST_DENSITY_META_BY_URI,
        _read_vector_uri_as_geojson,
    )
    from grace2_agent.tools.vector_tiles import (
        DENSE_VECTOR_THRESHOLD,
        MAX_INLINE_FEATURES,
    )

    n = MAX_INLINE_FEATURES + 1500  # comfortably above threshold AND the cap
    assert n > DENSE_VECTOR_THRESHOLD
    fc = _make_dense_fc(n)

    path = tmp_path / "buildings.geojson"
    path.write_text(json.dumps(fc))
    uri = str(path)

    _LAST_DENSITY_META_BY_URI.pop(uri, None)

    out = await _read_vector_uri_as_geojson(uri)

    assert out is not None
    assert out["type"] == "FeatureCollection"
    # Capped to the inline cap -> fewer features than the original dense FC.
    assert len(out["features"]) == MAX_INLINE_FEATURES
    assert len(out["features"]) < n
    # The density-meta side-table is populated for this URI and records the
    # honest original/emitted counts (this is what add_loaded_layer /
    # reinline_vector_layers lift onto the wire layer).
    meta = _LAST_DENSITY_META_BY_URI.get(uri)
    assert meta is not None
    assert meta.original_feature_count == n
    assert meta.emitted_feature_count == MAX_INLINE_FEATURES
    assert meta.capped is True


@pytest.mark.asyncio
async def test_small_vector_is_not_densified_off_loop(tmp_path: Any) -> None:
    """Below the threshold the FC is returned unchanged and no density-meta is
    recorded (byte-for-byte the prior inline behavior, just off-loop)."""
    from grace2_agent.pipeline_emitter import (
        _LAST_DENSITY_META_BY_URI,
        _read_vector_uri_as_geojson,
    )
    from grace2_agent.tools.vector_tiles import DENSE_VECTOR_THRESHOLD

    n = max(1, DENSE_VECTOR_THRESHOLD // 2)
    fc = _make_dense_fc(n)
    path = tmp_path / "small.geojson"
    path.write_text(json.dumps(fc))
    uri = str(path)
    _LAST_DENSITY_META_BY_URI.pop(uri, None)

    out = await _read_vector_uri_as_geojson(uri)

    assert out is not None
    assert len(out["features"]) == n
    assert _LAST_DENSITY_META_BY_URI.get(uri) is None


@pytest.mark.asyncio
async def test_densify_runs_in_executor_not_on_loop(tmp_path: Any) -> None:
    """The densify must NOT run on the asyncio loop: a concurrent loop task must
    keep ticking while a dense FC is being read + densified.

    This is the WS-keepalive proxy. We instrument ``densify_if_needed`` to block
    for a beat (simulating the real CPU cost) and concurrently run a tight
    ``asyncio.sleep(0)`` heartbeat. If the densify ran ON the loop, the heartbeat
    would be starved for the whole block; because it runs in an executor thread,
    the heartbeat keeps advancing.
    """
    import time

    from grace2_agent import pipeline_emitter as pe
    from grace2_agent.tools.vector_tiles import MAX_INLINE_FEATURES

    fc = _make_dense_fc(MAX_INLINE_FEATURES + 200)
    path = tmp_path / "blocking.geojson"
    path.write_text(json.dumps(fc))
    uri = str(path)
    pe._LAST_DENSITY_META_BY_URI.pop(uri, None)

    from grace2_agent.tools import vector_tiles as vt

    real = vt.densify_if_needed
    started = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_densify(*args: Any, **kwargs: Any):
        # Mark the moment the densify body actually runs (in whatever thread),
        # then block this thread for a beat.
        loop.call_soon_threadsafe(started.set)
        time.sleep(0.25)
        return real(*args, **kwargs)

    heartbeats = 0

    async def heartbeat() -> None:
        nonlocal heartbeats
        await started.wait()
        # Spin the loop while the densify thread is blocked. If the densify ran
        # on the loop, control would never return here until it finished.
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.20:
            heartbeats += 1
            await asyncio.sleep(0)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(vt, "densify_if_needed", slow_densify)
    try:
        hb_task = asyncio.create_task(heartbeat())
        out = await _read_vector_uri_as_geojson_for_test(uri)
        await hb_task
    finally:
        monkey.undo()

    # The densify produced a real capped result via the off-loop path...
    assert out is not None
    assert len(out["features"]) == MAX_INLINE_FEATURES
    # ...and the loop kept ticking thousands of times while the densify thread
    # was blocked -> the densify did NOT run on the loop.
    assert heartbeats > 100, heartbeats


@pytest.mark.asyncio
async def test_densified_result_is_cached_across_reads(tmp_path: Any) -> None:
    """A second read of the SAME vector uri is served from cache -- the densify
    does NOT run again.

    This is the reconnect-storm fix: a session-resume replay re-reads the
    active-case vector layers on every ~30s reconnect; without the cache each
    reconnect re-densified tens of thousands of features, which pegged the shared
    box and fed the reconnect storm. The cache makes the repeat read an O(1) hit.
    """
    from grace2_agent import pipeline_emitter as pe
    from grace2_agent.tools import vector_tiles as vt
    from grace2_agent.tools.vector_tiles import MAX_INLINE_FEATURES

    fc = _make_dense_fc(MAX_INLINE_FEATURES + 800)
    path = tmp_path / "cached.geojson"
    path.write_text(json.dumps(fc))
    uri = str(path)

    # Isolate: drop any prior cache entry for this uri.
    pe._DENSIFIED_FC_CACHE_BY_URI.pop(pe._densified_cache_key(uri), None)

    # First read: cache miss -> densify runs, output is capped + cached.
    out1 = await pe._read_vector_uri_as_geojson(uri)
    assert out1 is not None
    assert len(out1["features"]) == MAX_INLINE_FEATURES
    assert pe._densified_cache_key(uri) in pe._DENSIFIED_FC_CACHE_BY_URI

    # Second read: cache hit -> densify_if_needed must NOT be called again.
    calls = {"n": 0}
    real = vt.densify_if_needed

    def counting_densify(*args: Any, **kwargs: Any):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(vt, "densify_if_needed", counting_densify)
    try:
        out2 = await pe._read_vector_uri_as_geojson(uri)
    finally:
        monkey.undo()

    assert calls["n"] == 0  # served from cache, no re-densify
    assert out2 is out1  # identical cached object -- byte-for-byte the same FC
    assert len(out2["features"]) == MAX_INLINE_FEATURES


@pytest.mark.asyncio
async def test_densified_cache_is_fifo_bounded() -> None:
    """The densified-FC cache FIFO-evicts past the cap so the always-on agent
    process can never grow it without limit."""
    from grace2_agent import pipeline_emitter as pe

    cap = pe._MAX_DENSIFIED_FC_CACHE_ENTRIES
    pe._DENSIFIED_FC_CACHE_BY_URI.clear()
    try:
        for i in range(cap + 5):
            pe._store_densified_fc(
                f"cachekey-{i}", {"type": "FeatureCollection", "features": []}
            )
        assert len(pe._DENSIFIED_FC_CACHE_BY_URI) == cap
        # Oldest keys evicted; newest survive.
        assert "cachekey-0" not in pe._DENSIFIED_FC_CACHE_BY_URI
        assert f"cachekey-{cap + 4}" in pe._DENSIFIED_FC_CACHE_BY_URI
    finally:
        pe._DENSIFIED_FC_CACHE_BY_URI.clear()


# ``_read_vector_uri_as_geojson`` imports ``densify_if_needed`` from the
# ``vector_tiles`` module at call time (``from .tools.vector_tiles import
# densify_if_needed``), so monkeypatching ``vector_tiles.densify_if_needed`` is
# what the off-loop thread picks up. This thin wrapper just re-exports the
# function under test so the patch site above is unambiguous.
async def _read_vector_uri_as_geojson_for_test(uri: str) -> Any:
    from grace2_agent.pipeline_emitter import _read_vector_uri_as_geojson

    return await _read_vector_uri_as_geojson(uri)


# --------------------------------------------------------------------------- #
# DATA-DRIVEN LEGEND carry-over (the render KEY reaches the wire)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_legend_on_layer_uri_flows_to_session_state(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """A ``LayerURI`` that carries a ``legend`` (composer / Pelicun path) emits it
    on the ``ProjectLayerSummary`` in the next session-state envelope."""
    from grace2_contracts.execution import LegendClass, LegendKey

    layer = LayerURI(
        layer_id="pelicun_1",
        name="Pelicun damage",
        layer_type="vector",
        uri="s3://b/pelicun.fgb",
        style_preset="pelicun_damage_state",
        legend=LegendKey(
            kind="categorical",
            value_field="ds_mean",
            vmin=0.0,
            vmax=4.0,
            classes=[LegendClass(value_min=1.5, value_max=2.5, color="#fee08b", label="DS2 Moderate")],
            units="damage_state",
        ),
    )
    await emitter.add_loaded_layer(layer)

    summary = _session_frames(sink)[-1]["payload"]["loaded_layers"][-1]
    assert summary["legend"] is not None
    assert summary["legend"]["value_field"] == "ds_mean"
    assert summary["legend"]["kind"] == "categorical"
    assert summary["legend"]["vmin"] == 0.0 and summary["legend"]["vmax"] == 4.0


@pytest.mark.asyncio
async def test_legend_lifted_from_publish_stash_by_uri(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """The atomic publish_layer wrap-site rebuilds a LayerURI WITHOUT a legend; the
    emitter lifts the legend publish_layer stashed by display uri onto the
    summary (the continuous-raster path)."""
    from grace2_agent.tools.publish_layer import _stash_legend_for_uri
    from grace2_contracts.execution import LegendKey

    tile_uri = "https://cf.example/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=s3%3A%2F%2Fb%2Fx.tif&rescale=0,3&colormap_name=ylgnbu"
    _stash_legend_for_uri(
        tile_uri,
        LegendKey(kind="continuous", colormap="ylgnbu", vmin=0.0, vmax=3.0, label="Flood depth"),
    )
    # The wrap-site rebuilds the LayerURI from the bare string -> no legend on it.
    layer = LayerURI(
        layer_id="flood_1",
        name="flood_1",
        layer_type="raster",
        uri=tile_uri,
        style_preset="continuous_flood_depth",
    )
    assert layer.legend is None  # the wrap-site cannot set it
    await emitter.add_loaded_layer(layer)

    summary = _session_frames(sink)[-1]["payload"]["loaded_layers"][-1]
    assert summary["legend"] is not None
    assert summary["legend"]["kind"] == "continuous"
    assert summary["legend"]["colormap"] == "ylgnbu"
    assert summary["legend"]["vmin"] == 0.0 and summary["legend"]["vmax"] == 3.0


@pytest.mark.asyncio
async def test_legacy_layer_without_legend_emits_null_legend(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """BACKWARD-COMPAT: a layer with no legend (and nothing stashed for its uri)
    carries ``legend=None`` -> the web legacy style_preset path renders it as
    before."""
    layer = _make_layer("gs://b/legacy-dem.tif", layer_id="legacy_dem_1")
    await emitter.add_loaded_layer(layer)

    summary = _session_frames(sink)[-1]["payload"]["loaded_layers"][-1]
    assert summary["legend"] is None
