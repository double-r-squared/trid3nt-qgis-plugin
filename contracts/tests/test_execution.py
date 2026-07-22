"""Round-trip + invariant tests for solver-execution shapes (FR-TA-2)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from grace2_contracts.common import new_ulid
from grace2_contracts.envelope import ResultLayer, TemporalConfig
from grace2_contracts.execution import (
    ExecutionHandle,
    LayerURI,
    LegendClass,
    LegendKey,
    ModelSetup,
    RunResult,
)
from grace2_contracts.ws import LoadLayerArgs, MapTemporal


def test_model_setup_roundtrip() -> None:
    ms = ModelSetup(
        setup_id=new_ulid(),
        solver="sfincs",
        setup_uri="gs://trid3nt/setups/01HX/",
        grid_resolution_m=10.0,
        bbox=(-82.5, 26.4, -81.7, 26.9),
        parameters={"manning": 0.04},
        created_at="2026-06-05T12:00:00Z",
    )
    a = ms.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = ModelSetup.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)


def test_execution_handle_pins_workflows_execution_id_invariant_8() -> None:
    """Invariant 8: the handle carries the Cloud Workflows execution identifier as
    a first-class field. agent calls Workflows `terminate` with it on cancel."""
    handle = ExecutionHandle(
        handle_id=new_ulid(),
        run_id=new_ulid(),
        solver="sfincs",
        compute_class="standard",
        workflows_execution_id="projects/trid3nt/locations/us-central1/workflows/sfincs-run/executions/01HX",
        workflow_name="sfincs-run",
        workflow_location="us-central1",
        submitted_at="2026-06-05T12:00:00Z",
    )
    dumped = handle.model_dump(mode="json")
    assert "workflows_execution_id" in dumped
    assert dumped["workflows_execution_id"].startswith("projects/")
    # The handle must not silently accept a workflows_execution_id rename
    with pytest.raises(ValidationError):
        ExecutionHandle.model_validate({**dumped, "wf_id": dumped["workflows_execution_id"]})


def test_run_result_status_supports_cancelled() -> None:
    """Invariant 8: cancelled is distinct from failed."""
    rr = RunResult(
        run_id=new_ulid(),
        handle_id=new_ulid(),
        status="cancelled",
        cancellation_reason="user-requested",
        started_at="2026-06-05T12:00:00Z",
        completed_at="2026-06-05T12:01:00Z",
    )
    a = rr.model_dump(mode="json")
    again = RunResult.model_validate(a).model_dump(mode="json")
    assert a == again


def test_layer_uri_maps_field_for_field_onto_load_layer_args() -> None:
    """The visualization seam: LayerURI -> map-command load-layer with no
    translation beyond plumbing the WMS URL."""
    layer = LayerURI(
        layer_id="run-01HX-flood-depth",
        name="Flood depth (m)",
        layer_type="raster",
        uri="gs://trid3nt/runs/01HX/depth.cog.tif",
        style_preset="flood_depth_blue",
        temporal=TemporalConfig(
            start="2022-09-28T00:00:00Z",
            end="2022-09-30T00:00:00Z",
            step_seconds=3600,
        ),
        role="primary",
        units="meters",
    )
    args = LoadLayerArgs(
        layer_id=layer.layer_id,
        wms_url="https://qgis.example.com/wms?MAP=01HX.qgs",
        style_preset=layer.style_preset,
        temporal=MapTemporal(
            start=layer.temporal.start,
            end=layer.temporal.end,
            step_seconds=layer.temporal.step_seconds,
        ),
    )
    assert args.layer_id == layer.layer_id
    assert args.style_preset == layer.style_preset
    assert args.temporal is not None
    assert args.temporal.step_seconds == layer.temporal.step_seconds


# --------------------------------------------------------------------------- #
# LegendKey -- the data-driven render key (the colormap that comes from the data)
# --------------------------------------------------------------------------- #


def test_legend_key_constructs_continuous_from_real_data_range() -> None:
    """A continuous legend: named ramp (semantic) + the REAL data range
    (vmin/vmax = the percentile read), the canonical raster/graduated-vector
    shape."""
    legend = LegendKey(
        kind="continuous",
        colormap="reds",
        vmin=0.12,  # the real p2 the producer computed (NOT a hardcoded 0)
        vmax=3.47,  # the real p98 (NOT a hardcoded 3)
        units="meters",
        label="Flood depth",
    )
    assert legend.kind == "continuous"
    assert legend.colormap == "reds"
    assert legend.vmin == 0.12 and legend.vmax == 3.47
    assert legend.classes is None
    # explicit-stops form of colormap also validates
    stops = LegendKey(
        kind="continuous",
        colormap=[(0.0, "#ffffff"), (0.5, "#ff8800"), (1.0, "#000000")],
        vmin=0.0,
        vmax=4.0,
    )
    assert isinstance(stops.colormap, list)
    assert stops.colormap[0] == (0.0, "#ffffff")


def test_legend_key_constructs_categorical_with_classes() -> None:
    """A categorical legend: discrete class swatches (NLCD / damage states),
    optionally driven by a VECTOR feature property via ``value_field``."""
    legend = LegendKey(
        kind="categorical",
        value_field="ds_mean",  # the GeoJSON property the choropleth colors by
        classes=[
            LegendClass(value=0, color="#1a9641", label="None"),
            LegendClass(value_min=0.5, value_max=1.5, color="#fdae61", label="Slight"),
            LegendClass(value="D4", color="#730000", label="Exceptional"),
        ],
        units=None,
    )
    assert legend.kind == "categorical"
    assert legend.value_field == "ds_mean"
    assert legend.classes is not None and len(legend.classes) == 3
    # both class-addressing forms are accepted (single value OR a numeric bin)
    assert legend.classes[0].value == 0
    assert legend.classes[1].value_min == 0.5 and legend.classes[1].value_max == 1.5
    assert legend.classes[2].value == "D4"
    # categorical keys carry no continuous range
    assert legend.colormap is None and legend.vmin is None and legend.vmax is None


def test_layer_uri_carries_legend_and_round_trips() -> None:
    """``LayerURI`` carries the data-driven legend; it survives a JSON
    round-trip byte-for-byte."""
    layer = LayerURI(
        layer_id="run-01HX-flood-depth",
        name="Flood depth (m)",
        layer_type="raster",
        uri="s3://trid3nt/runs/01HX/depth.cog.tif",
        style_preset="flood_depth_blue",
        role="primary",
        units="meters",
        legend=LegendKey(
            kind="continuous",
            colormap="reds",
            vmin=0.12,
            vmax=3.47,
            units="meters",
            label="Flood depth",
        ),
    )
    a = layer.model_dump(mode="json")
    assert a["legend"]["kind"] == "continuous"
    assert a["legend"]["vmin"] == 0.12 and a["legend"]["vmax"] == 3.47
    text_a = json.dumps(a, sort_keys=True)
    b = LayerURI.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)


def test_layer_uri_legend_is_optional_backward_compat() -> None:
    """Additive + optional: a ``LayerURI`` without a legend is unchanged
    (legend defaults to None => legacy ``style_preset`` rendering)."""
    layer = LayerURI(
        layer_id="legacy",
        name="Legacy raster",
        layer_type="raster",
        uri="s3://trid3nt/runs/legacy/depth.cog.tif",
        style_preset="flood_depth_blue",
    )
    assert layer.legend is None
    dumped = layer.model_dump(mode="json")
    assert dumped["legend"] is None
    again = LayerURI.model_validate(dumped).model_dump(mode="json")
    assert dumped == again


def test_result_layer_mirrors_legend_and_round_trips() -> None:
    """``ResultLayer`` (envelope.py) mirrors the legend; the forward-ref to
    ``execution.LegendKey`` resolves and the categorical shape round-trips."""
    result = ResultLayer(
        layer_id="run-01HX-damage",
        name="Damage state",
        layer_type="vector",
        uri="s3://trid3nt/runs/01HX/damage.fgb",
        style_preset="pelicun_damage_state",
        role="primary",
        legend=LegendKey(
            kind="categorical",
            value_field="ds_mean",
            classes=[
                LegendClass(value=0, color="#1a9641", label="None"),
                LegendClass(value=4, color="#d7191c", label="Complete"),
            ],
        ),
    )
    assert result.legend is not None
    assert result.legend.value_field == "ds_mean"
    a = result.model_dump(mode="json")
    again = ResultLayer.model_validate(a).model_dump(mode="json")
    assert a == again
