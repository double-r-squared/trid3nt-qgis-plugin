"""Offline unit coverage for the ELMFIRE CAND-S sensitivity-sweep surface.

Fast (no solver, no index): the namelist knob-extension, the generic sweep chart
spec, the shared sensitivity contract, and the three templates' registration.
ASCII only.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _deck_builder():
    path = REPO / "services" / "workers" / "elmfire" / "deck_builder.py"
    spec = importlib.util.spec_from_file_location("elmfire_db_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GRID = {"epsg": 5070, "cellsize_m": 30.0, "xll": 0.0, "yll": 0.0, "nx": 10, "ny": 10}
IGN = [{"x": 150.0, "y": 150.0, "t_ign_s": 0.0}]
WX = {"lh_pct": 30.0, "lw_pct": 60.0, "ws_mph_20ft": 15.0, "wd_deg": 270.0,
      "m1_pct": 3.0, "m10_pct": 4.0, "m100_pct": 5.0}


def test_namelist_base_unchanged_without_extras():
    """render_namelist with no extras is byte-identical to the pre-extension deck."""
    db = _deck_builder()
    nl = db.render_namelist(GRID, IGN, WX, duration_s=3600.0)
    # No injected knobs leak in.
    assert "MAX_LOW" not in nl
    assert "WIND_FLUCTUATIONS" not in nl
    assert "FOLIAR_MOISTURE_CONTENT" not in nl
    # The base tutorial-01 keys are present.
    assert "WSMFEFF_LOW_MULT = 0.011364" in nl
    assert "DUMP_TIME_OF_ARRIVAL = .TRUE." in nl


def test_namelist_extras_inject_into_correct_groups():
    """simulator/outputs/inputs extras land verbatim in their namelist groups."""
    db = _deck_builder()
    nl = db.render_namelist(
        GRID, IGN, WX, duration_s=3600.0,
        simulator_extra={"MAX_LOW": "8.0000", "WIND_FLUCTUATIONS": ".TRUE."},
        outputs_extra={"DUMP_CROWN_FIRE_AREA": ".TRUE."},
        inputs_extra={"FOLIAR_MOISTURE_CONTENT": "90.0000"},
    )
    assert "MAX_LOW = 8.0000" in nl
    assert "WIND_FLUCTUATIONS = .TRUE." in nl
    assert "DUMP_CROWN_FIRE_AREA = .TRUE." in nl
    assert "FOLIAR_MOISTURE_CONTENT = 90.0000" in nl
    # &SIMULATOR extras sit within that group (after WSMFEFF_LOW_MULT, before &MISC).
    sim = nl.split("&SIMULATOR", 1)[1].split("&MISCELLANEOUS", 1)[0]
    assert "MAX_LOW = 8.0000" in sim and "WIND_FLUCTUATIONS = .TRUE." in sim
    # &INPUTS extra sits in the &INPUTS group (before &OUTPUTS), not &SIMULATOR.
    inputs = nl.split("&INPUTS", 1)[1].split("&OUTPUTS", 1)[0]
    assert "FOLIAR_MOISTURE_CONTENT = 90.0000" in inputs
    assert "FOLIAR_MOISTURE_CONTENT" not in sim


def _sweep_common():
    import trid3nt_server.agent.workflows.elmfire.sensitivity._sensitivity_common as m
    return m


def test_sweep_chart_spec_shapes():
    m = _sweep_common()
    assert m.build_sweep_chart_spec([], x_title="x", y_title="y") is None
    sweep = [{"x": 3.0, "y": 2.7}, {"x": 7.5, "y": 4.6}, {"x": 12.0, "y": 4.6}]
    spec = m.build_sweep_chart_spec(
        sweep, x_title="cap", y_title="ltw", reference_y=4.6, identity_diagonal=True
    )
    layers = spec["layer"]
    # base line + identity diagonal + reference rule = 3 layers.
    assert len(layers) == 3
    marks = [ly["mark"]["type"] for ly in layers]
    assert marks.count("line") == 2 and marks.count("rule") == 1
    # the identity diagonal spans the swept x-range.
    diag = [ly for ly in layers if ly["mark"].get("strokeDash") == [4, 4]][0]
    xs = [pt["x"] for pt in diag["data"]["values"]]
    assert xs == [3.0, 12.0]


def test_sensitivity_layer_uri_contract():
    from trid3nt_contracts.elmfire_contracts import ElmfireSensitivityLayerURI

    layer = ElmfireSensitivityLayerURI(
        layer_id="fire-x", name="n", layer_type="raster", uri="s3://b/k.tif",
        style_preset="continuous_fire_arrival_hr", role="primary",
        bbox=(-1.0, -1.0, 1.0, 1.0), burned_area_km2=0.5, fire_arrival_max_hr=1.0,
        duration_hours=0.75,
        swept_param="max_low_cap", swept_units="ratio",
        response_metric="length_to_width_ratio", response_units="ratio",
        sweep=[{"x": 3.0, "y": 2.7}], summary={"natural_ltw": 4.6},
    )
    assert layer.swept_param == "max_low_cap"
    assert layer.sweep[0]["x"] == 3.0
    assert layer.summary["natural_ltw"] == 4.6


@pytest.mark.parametrize(
    "name",
    [
        "elmfire_length_to_width_ceiling_sensitivity",
        "elmfire_wind_fluctuation_randomization",
        "elmfire_live_fuel_moisture_sensitivity",
    ],
)
def test_templates_registered(name):
    import trid3nt_server.main as _main

    _main._import_tools_registry()
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY[name]
    assert callable(entry.fn)
    assert entry.metadata.tier == "template"
    assert entry.metadata.engine == "elmfire"
    assert entry.metadata.cacheable is False
