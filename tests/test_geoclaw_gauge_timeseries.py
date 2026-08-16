"""Offline tests for the ``geoclaw_tsunami_gauge_timeseries`` template (ADR 0123,
hazard-easy-four continuation #3).

Pins the agent-side gauge-timeseries surface in ISOLATION (no solver, no docker,
no MinIO):

1. **Contract round-trip** -- ``GeoClawDepthLayerURI`` carries the OPTIONAL gauge
   scalars (default None, additive). (no IO)
2. **Download-filter widening** -- ``_is_geoclaw_output_key`` accepts fort.* AND
   gaugeNNNNN.txt, rejects unrelated files. (no IO)
3. **Gauge parser (postprocess-vs-fixture)** -- a synthetic gauge file parses to
   the surface-elevation series + typed scalars (co-seismic offset = eta at t0).
4. **Gauge time-series chart builder** -- a deterministic Vega-Lite spec; empty
   series -> None. (no IO)
5. **Tool bbox gate** -- a missing bbox returns a typed error envelope. (async)
"""

from __future__ import annotations

import asyncio
import math

import pytest

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.geoclaw_contracts import GeoClawDepthLayerURI


# ===========================================================================
# (1) Contract round-trip.
# ===========================================================================
def test_depth_layer_carries_optional_gauge_scalars():
    layer = GeoClawDepthLayerURI(
        layer_id="geoclaw-peak-X",
        name="Peak flood depth",
        layer_type="raster",
        uri="s3://runs/X/geoclaw_depth_peak.tif",
        style_preset="continuous_flood_depth",
        role="primary",
        max_depth_m=0.0,
        flooded_area_km2=0.0,
        max_inundation_m=0.0,
        scenario="tsunami",
        gauge_max_surface_elevation_m=1.8,
        gauge_min_surface_elevation_m=-0.6,
        gauge_max_amplitude_m=2.4,
        gauge_coseismic_offset_m=-0.4,
        gauge_max_depth_m=3.1,
    )
    assert isinstance(layer, LayerURI)
    assert layer.gauge_coseismic_offset_m == -0.4
    assert layer.gauge_max_amplitude_m == 2.4
    # default None when not a gauge run (additive).
    plain = GeoClawDepthLayerURI(
        layer_id="geoclaw-peak-Y",
        name="Peak flood depth",
        layer_type="raster",
        uri="s3://runs/Y/geoclaw_depth_peak.tif",
        style_preset="continuous_flood_depth",
        role="primary",
        max_depth_m=1.0,
        flooded_area_km2=0.5,
        max_inundation_m=0.8,
    )
    assert plain.gauge_max_amplitude_m is None


# ===========================================================================
# (2) Download-filter widening.
# ===========================================================================
def test_output_key_filter_accepts_fort_and_gauge():
    from trid3nt_server.workflows.geoclaw.inundation.inundation import (
        _is_geoclaw_output_key,
    )

    assert _is_geoclaw_output_key("fort.q0003")
    assert _is_geoclaw_output_key("fort.t0003")
    assert _is_geoclaw_output_key("gauge00001.txt")
    assert not _is_geoclaw_output_key("setrun.py")
    assert not _is_geoclaw_output_key("gauge_grids.data")  # not a *.txt gauge series


# ===========================================================================
# (3) Gauge parser (postprocess-vs-fixture).
# ===========================================================================
def test_gauge_parser_series_and_scalars(tmp_path):
    from trid3nt_server.workflows.geoclaw.postprocess_geoclaw import (
        parse_geoclaw_gauge_series,
    )

    out = tmp_path / "_output"
    out.mkdir()
    # [level t h hu hv eta]; co-seismic offset eta(t0) = -0.4, then a decaying wave.
    rows = ["# gauge 1", "# Columns: level t h hu hv eta"]
    for i in range(30):
        t = i * 60.0
        eta = -0.4 + 1.0 * math.sin(2 * math.pi * t / 1200.0) * math.exp(-t / 2000.0)
        h = max(0.0, eta + 2.0)
        rows.append(f"   1  {t:.6e}  {h:.6e}  0.0  0.0  {eta:.6e}")
    (out / "gauge00001.txt").write_text("\n".join(rows) + "\n")

    series, scalars = parse_geoclaw_gauge_series(tmp_path)
    assert series is not None
    assert len(series["t"]) == 30
    # co-seismic offset is eta at t0.
    assert abs(scalars["gauge_coseismic_offset_m"] - (-0.4)) < 1e-6
    assert scalars["gauge_max_amplitude_m"] >= 0.0
    assert (
        scalars["gauge_max_surface_elevation_m"]
        >= scalars["gauge_min_surface_elevation_m"]
    )


def test_gauge_parser_no_file_returns_none(tmp_path):
    from trid3nt_server.workflows.geoclaw.postprocess_geoclaw import (
        parse_geoclaw_gauge_series,
    )

    assert parse_geoclaw_gauge_series(tmp_path) == (None, {})


# ===========================================================================
# (4) Gauge time-series chart builder.
# ===========================================================================
def test_gauge_timeseries_chart_spec():
    from trid3nt_contracts.chart_contracts import is_structurally_valid_vega_lite_spec
    from trid3nt_server.workflows.geoclaw.postprocess_geoclaw import (
        build_gauge_timeseries_chart_spec,
    )

    series = {"t": [0.0, 60.0, 120.0], "eta": [-0.4, 0.6, 0.1], "depth": [1.6, 2.6, 2.1]}
    spec = build_gauge_timeseries_chart_spec(series)
    assert is_structurally_valid_vega_lite_spec(spec)
    assert len(spec["data"]["values"]) == 3
    assert spec["encoding"]["y"]["field"] == "eta_m"
    assert build_gauge_timeseries_chart_spec(None) is None
    assert build_gauge_timeseries_chart_spec({"t": []}) is None


# ===========================================================================
# (5) Tool bbox gate.
# ===========================================================================
def test_tool_missing_bbox_returns_typed_error():
    from trid3nt_server.workflows.geoclaw.gauge_timeseries.gauge_timeseries import (
        geoclaw_tsunami_gauge_timeseries,
    )

    out = asyncio.run(geoclaw_tsunami_gauge_timeseries(bbox=None))
    assert isinstance(out, dict)
    assert out["status"] == "error"
    assert out["error_code"] == "GEOCLAW_PARAMS_INCOMPLETE"
