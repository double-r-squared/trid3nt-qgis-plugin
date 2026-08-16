"""Offline unit tests for the coastal_tidal_surge engine template (ADR 0259).

No solver / no network: registration shape + arg-guard rejection paths + the
series-type classifier only. The physics-through-the-image proof lives in the
substrate build-time smoke + the live E2E; this is the offline-suite guard that
the tool is registered as an engine template and rejects ill-posed args before any
dispatch.
"""
from __future__ import annotations

import asyncio


def test_coastal_tidal_surge_registered_as_engine_template():
    from trid3nt_server.agent.tools import TOOL_REGISTRY
    entry = TOOL_REGISTRY.get("coastal_tidal_surge")
    assert entry is not None, "coastal_tidal_surge must be registered"
    m = entry.metadata
    assert m.engine == "telemac" and m.tier == "template"
    assert m.cacheable is False and m.ttl_class == "live-no-cache"
    assert m.source_class == "workflow_dispatch"
    specs = {r.param for r in (m.resolution_specs or ())}
    assert "target_resolution_m" in specs


def test_coastal_solver_registered():
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        LOCAL_SOLVER_SPEC_REGISTRY,
        SOLVER_WORKFLOW_REGISTRY,
    )
    assert "telemac_coastal" in SOLVER_WORKFLOW_REGISTRY
    assert "telemac_coastal" in LOCAL_SOLVER_SPEC_REGISTRY


def test_tool_rejects_invalid_bbox():
    from trid3nt_server.agent.workflows.telemac.coastal_tidal_surge.coastal_tidal_surge import (
        coastal_tidal_surge,
    )
    out = asyncio.run(coastal_tidal_surge(bbox=[1.0, 2.0]))  # too few numbers
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "COASTAL_PARAMS_INVALID"


def test_series_type_classification_from_prompt():
    from trid3nt_server.agent.workflows.telemac.coastal_tidal_surge.coastal_tidal_surge import (
        _classify_series_type,
    )
    assert _classify_series_type("the astronomical tide prediction", None) == "prediction"
    assert _classify_series_type("observed hurricane surge record", None) == "observed"
    assert _classify_series_type("map the storm surge inland", None) == "observed"
    assert _classify_series_type("anything", "prediction") == "prediction"  # explicit wins


def test_coastal_layer_contract_carries_typed_scalars():
    from trid3nt_contracts.telemac_contracts import (
        TELEMAC_COASTAL_DEPTH_STYLE_PRESET,
        TelemacCoastalLayerURI,
    )
    layer = TelemacCoastalLayerURI(
        layer_id="telemac-coastal-depth-x", name="Peak inundation depth (coast)",
        layer_type="raster", uri="s3://bucket/coastal_depth_max.tif",
        style_preset=TELEMAC_COASTAL_DEPTH_STYLE_PRESET, role="primary", units="m",
        peak_depth_m=9.33, flooded_land_km2=14.51, series_type="observed",
        sl_peak_m=2.645)
    assert layer.peak_depth_m == 9.33 and layer.flooded_land_km2 == 14.51
    assert layer.style_preset == "continuous_coastal_inundation_depth"
