"""D3 coverage: a re-run's flood animation frames SUPERSEDE the prior run's
same-step frames instead of accumulating.

Live symptom (case 01KVH4MZ9JF7GGHQ88D5PSWZVH, 50 layers): re-running a scenario
appends a SECOND full "Flood depth step N" frame series under fresh run_ids. The
frames are DISTINCT COGs (per-run S3 keys + run-id-suffixed layer_ids), so the
COG-identity dedup (``_layer_identity_key``) never collapses run B's step N
against run A's step N -> [step1, step1, step2, step2, ...].

Fix (``pipeline_emitter._frame_series_key`` + ``add_loaded_layer`` dedup, and a
matching prune in ``server._persist_case_loaded_layers``): animation frames key
on (role="context" + "Flood depth step N"), so step N of run B replaces step N
of run A. Engine-agnostic (SWMM + SFINCS share the name token). Peak / vector /
basemap layers keep COG-identity dedup unchanged.
"""

from __future__ import annotations

import pytest

from grace2_contracts import new_ulid
from grace2_contracts.execution import LayerURI

from grace2_agent.pipeline_emitter import (
    PipelineEmitter,
    _frame_series_key,
)


class _Sink:
    async def __call__(self, text: str) -> None:  # noqa: D401 — swallow frames
        return None


def _frame_layer(frame_no: int, run_id: str, *, engine: str = "swmm") -> LayerURI:
    """A per-frame depth LayerURI as postprocess_swmm / postprocess_flood emit:
    run-id-suffixed layer_id, stable "Flood depth step N" name, role=context,
    a per-run COG behind a TiTiler display template (the live wire shape)."""
    prefix = "swmm-depth-frame" if engine == "swmm" else "flood-depth-frame"
    fname = "swmm_depth_frame" if engine == "swmm" else "flood_depth_frame"
    cog = f"s3://grace2-hazard-runs/{run_id}/{fname}_{frame_no:02d}.tif"
    return LayerURI(
        layer_id=f"{prefix}-{frame_no:02d}-{run_id}",
        name=f"Flood depth step {frame_no}",
        layer_type="raster",
        uri=(
            "https://titiler.example/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
            f"?url={cog}&rescale=0,2&colormap_name=blues"
        ),
        style_preset="continuous_flood_depth",
        role="context",
    )


def _peak_layer(run_id: str) -> LayerURI:
    cog = f"s3://grace2-hazard-runs/{run_id}/swmm_depth_peak.tif"
    return LayerURI(
        layer_id=f"swmm-depth-peak-{run_id}",
        name="Peak flood depth",
        layer_type="raster",
        uri=(
            "https://titiler.example/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
            f"?url={cog}&rescale=0,3&colormap_name=blues"
        ),
        style_preset="continuous_flood_depth",
        role="primary",
    )


# --------------------------------------------------------------------------- #
# 1. The series-key helper
# --------------------------------------------------------------------------- #


def test_frame_series_key_matches_flood_frames_only() -> None:
    frame = _frame_layer(3, "RUNX")
    summary_frame = _frame_summary(frame)
    assert _frame_series_key(summary_frame) == "flood-frame::Flood depth step 3"

    peak = _peak_layer("RUNX")
    assert _frame_series_key(_frame_summary(peak)) is None


def test_frame_series_key_is_run_independent() -> None:
    """Run A step 5 and run B step 5 share a series key (the whole point)."""
    a = _frame_summary(_frame_layer(5, "RUN_A"))
    b = _frame_summary(_frame_layer(5, "RUN_B"))
    assert _frame_series_key(a) == _frame_series_key(b)


def test_swmm_and_sfincs_frames_share_series_key() -> None:
    swmm = _frame_summary(_frame_layer(2, "RUN_S", engine="swmm"))
    sfincs = _frame_summary(_frame_layer(2, "RUN_F", engine="sfincs"))
    assert _frame_series_key(swmm) == _frame_series_key(sfincs)


def _frame_summary(layer: LayerURI):
    """Build the ProjectLayerSummary the emitter would, for the key helper."""
    from grace2_contracts.collections import ProjectLayerSummary

    return ProjectLayerSummary(
        layer_id=layer.layer_id,
        name=layer.name,
        layer_type=layer.layer_type,
        uri=layer.uri,
        style_preset=layer.style_preset,
        visible=True,
        role=layer.role,
        temporal=layer.temporal is not None,
    )


# --------------------------------------------------------------------------- #
# 2. The emitter de-accumulation (the live symptom)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rerun_frames_supersede_prior_run_in_emitter() -> None:
    emitter = PipelineEmitter(session_id=new_ulid(), sink=_Sink())
    run_a, run_b = "01RUNAAA", "01RUNBBB"

    # Run A: 3 frames + a peak.
    for n in (1, 2, 3):
        await emitter.add_loaded_layer(_frame_layer(n, run_a))
    await emitter.add_loaded_layer(_peak_layer(run_a))

    # Re-run (Run B): SAME 3 steps + a peak — distinct COGs / layer_ids.
    for n in (1, 2, 3):
        await emitter.add_loaded_layer(_frame_layer(n, run_b))
    await emitter.add_loaded_layer(_peak_layer(run_b))

    layers = emitter.loaded_layers
    frame_layers = [l for l in layers if l.name.startswith("Flood depth step")]
    # Exactly 3 frames (one per step) — NOT 6. The re-run superseded, not piled.
    assert len(frame_layers) == 3, [(l.layer_id, l.name) for l in layers]
    # Each surviving frame is the NEWEST run's (run B layer_id).
    for l in frame_layers:
        assert l.layer_id.endswith(run_b), l.layer_id
    # Names cover steps 1..3 exactly once.
    assert sorted(l.name for l in frame_layers) == [
        "Flood depth step 1",
        "Flood depth step 2",
        "Flood depth step 3",
    ]


@pytest.mark.asyncio
async def test_peak_is_not_collapsed_by_a_frame() -> None:
    """A frame must never supersede the peak (role=primary) and vice-versa."""
    emitter = PipelineEmitter(session_id=new_ulid(), sink=_Sink())
    await emitter.add_loaded_layer(_peak_layer("01RUNAAA"))
    await emitter.add_loaded_layer(_frame_layer(1, "01RUNAAA"))
    await emitter.add_loaded_layer(_frame_layer(2, "01RUNAAA"))
    layers = emitter.loaded_layers
    # Peak + 2 frames = 3 distinct rows.
    assert len(layers) == 3
    assert any(l.role == "primary" and l.name == "Peak flood depth" for l in layers)


@pytest.mark.asyncio
async def test_distinct_steps_coexist_within_a_run() -> None:
    emitter = PipelineEmitter(session_id=new_ulid(), sink=_Sink())
    for n in (1, 2, 3, 4, 5):
        await emitter.add_loaded_layer(_frame_layer(n, "01RUNAAA"))
    assert len(emitter.loaded_layers) == 5


@pytest.mark.asyncio
async def test_non_frame_layers_keep_cog_identity_dedup() -> None:
    """The pre-existing COG-identity dedup (job duplicate-flood-layer) still
    collapses two display URLs of the SAME peak COG to one row."""
    emitter = PipelineEmitter(session_id=new_ulid(), sink=_Sink())
    cog = "s3://grace2-hazard-runs/RUN/swmm_depth_peak.tif"
    a = LayerURI(
        layer_id="peak-a",
        name="Peak flood depth",
        layer_type="raster",
        uri=f"https://t/cog/tiles/{{z}}/{{x}}/{{y}}.png?url={cog}&colormap_name=blues",
        style_preset="continuous_flood_depth",
        role="primary",
    )
    b = LayerURI(
        layer_id="peak-b",
        name="Peak flood depth",
        layer_type="raster",
        uri=f"https://t/cog/tiles/{{z}}/{{x}}/{{y}}.png?url={cog}&colormap_name=viridis",
        style_preset="continuous_flood_depth",
        role="primary",
    )
    await emitter.add_loaded_layer(a)
    await emitter.add_loaded_layer(b)
    assert len(emitter.loaded_layers) == 1


# --------------------------------------------------------------------------- #
# 3. Persist-side prune (server._persist_case_loaded_layers logic)
# --------------------------------------------------------------------------- #


def test_persist_side_frame_series_regex_matches_token() -> None:
    """The server reuses the same _FLOOD_FRAME_NAME_RE token so the persisted
    prune keys identically to the emitter."""
    from grace2_agent.server import _FLOOD_FRAME_NAME_RE

    assert _FLOOD_FRAME_NAME_RE.match("Flood depth step 7")
    assert _FLOOD_FRAME_NAME_RE.match("Flood depth step 12")
    assert not _FLOOD_FRAME_NAME_RE.match("Peak flood depth")
    assert not _FLOOD_FRAME_NAME_RE.match("Flood depth step")  # no number
