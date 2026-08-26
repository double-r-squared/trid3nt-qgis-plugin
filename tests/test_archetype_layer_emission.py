"""Regression: the shared ``_run_archetype`` seam must LOAD its headline layer.

The MODFLOW archetype composers (capture_zone, wellhead_protection, drawdown,
mine_dewatering, subsidence, ...) all run their solve through the shared
``_run_archetype`` helper and return a typed ``*Result`` that the thin workflow
tool serializes to a dict. The server dispatch's ``add_loaded_layer`` gate fires
ONLY on a bare-``LayerURI`` return, so a dict-returning composer must load its
own headline layer -- otherwise the run succeeds, produces a real FGB/COG, and
the Case receives ZERO layers with no error (an honesty-floor hole).

``_run_archetype`` used to rely on ``_maybe_emit(pipeline_emitter, ...)`` to load
the layer via ``emit_tool_call``'s gate, but the thin tools pass
``pipeline_emitter=None``, so nothing loaded. These tests pin the fix: the
returned LayerURI is added to ``current_emitter().loaded_layers``, and the
dict-shaped return is (correctly) NOT auto-loaded by ``emit_tool_call``.

Fully offline: ``run_modflow_archetype_job`` is mocked -- no mf6, no S3.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from trid3nt_contracts import new_ulid
from trid3nt_contracts.modflow_contracts import CaptureZoneLayerURI
from trid3nt_server.emission.pipeline_emitter import (
    _CURRENT_EMITTER,
    PipelineEmitter,
)
from trid3nt_server.workflows.modflow.sustainable_yield.sustainable_yield import (
    _run_archetype,
)


class _Sink:
    async def __call__(self, text: str) -> None:
        import json

        json.loads(text)  # assert the wire payload is valid JSON


def _fake_capture_layer() -> CaptureZoneLayerURI:
    return CaptureZoneLayerURI(
        layer_id="capture-zone-" + new_ulid(),
        name="Capture zone",
        uri="s3://trid3nt-runs/" + new_ulid() + "/capture_zone_4326.fgb",
        style_preset="capture_zone",
        layer_type="vector",
        capture_zone_area_km2=5.6,
        travel_time_years=[5.0, 10.0, 25.0],
        isochrone_areas_km2={"5": 0.1, "10": 0.25, "25": 0.65},
        particle_count=48,
    )


@pytest.mark.asyncio
async def test_run_archetype_loads_headline_layer():
    """A typed layer returned by the archetype run tool is added to the
    emitter's loaded_layers -- even when pipeline_emitter is None (the thin-tool
    path). This is the regression that would have caught the ZERO-layers bug."""
    emitter = PipelineEmitter(session_id=new_ulid(), sink=_Sink())
    layer = _fake_capture_layer()

    async def _fake_job(run_args, *, compute_class):  # noqa: ANN001
        return layer

    token = _CURRENT_EMITTER.set(emitter)
    try:
        with patch(
            "trid3nt_server.workflows.modflow."
            "run_modflow_archetype_tool.run_modflow_archetype_job",
            _fake_job,
        ):
            out = await _run_archetype(
                run_args=object(),
                compute_class="standard",
                pipeline_emitter=None,  # the thin-tool path -- the broken case
                tool_label="Model wellhead protection area",
                expected_type=CaptureZoneLayerURI,
                error_code="WELLHEAD_PROTECTION_RUN_FAILED",
                scenario_error=RuntimeError,
            )
    finally:
        _CURRENT_EMITTER.reset(token)

    assert out is layer
    assert len(emitter.loaded_layers) == 1
    assert emitter.loaded_layers[0].uri == layer.uri


@pytest.mark.asyncio
async def test_run_archetype_raises_and_loads_nothing_on_error_dict():
    """A non-typed (error dict) archetype return raises the scenario error and
    loads NO layer -- the honesty floor is preserved."""
    emitter = PipelineEmitter(session_id=new_ulid(), sink=_Sink())

    async def _fake_job(run_args, *, compute_class):  # noqa: ANN001
        return {"status": "error", "error_code": "MODFLOW_ARCHETYPE_EMPTY_RESULT",
                "error_message": "no non-trivial result"}

    token = _CURRENT_EMITTER.set(emitter)
    try:
        with patch(
            "trid3nt_server.workflows.modflow."
            "run_modflow_archetype_tool.run_modflow_archetype_job",
            _fake_job,
        ):
            with pytest.raises(RuntimeError, match="MODFLOW_ARCHETYPE_EMPTY_RESULT"):
                await _run_archetype(
                    run_args=object(),
                    compute_class="standard",
                    pipeline_emitter=None,
                    tool_label="Model wellhead protection area",
                    expected_type=CaptureZoneLayerURI,
                    error_code="WELLHEAD_PROTECTION_RUN_FAILED",
                    scenario_error=RuntimeError,
                )
    finally:
        _CURRENT_EMITTER.reset(token)

    assert len(emitter.loaded_layers) == 0


@pytest.mark.asyncio
async def test_composer_dict_return_is_not_auto_loaded_by_dispatch():
    """emit_tool_call's add_loaded_layer gate fires ONLY on a bare LayerURI, so a
    composer's dict-shaped return is NOT auto-loaded -- this is WHY _run_archetype
    must self-load the headline layer (documents the root cause)."""
    emitter = PipelineEmitter(session_id=new_ulid(), sink=_Sink())
    layer = _fake_capture_layer()
    composer_dict = {
        "schema_version": "v1",
        "capture_zone_layer": layer.model_dump(mode="json"),
        "summary": {},
    }

    async def _invoke():
        return composer_dict

    out = await emitter.emit_tool_call(
        name="modflow_wellhead_protection",
        tool_name="modflow_wellhead_protection",
        invoke=_invoke,
    )
    assert out is composer_dict
    assert len(emitter.loaded_layers) == 0
