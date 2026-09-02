"""Offline tests for postprocess_telemac_wse (max FREE-SURFACE / WSE raster).

Covers the free-surface / depth variable pickers, the WET-MASK discipline (dry
terrain must NOT leak into the water-surface raster), the mesh-CRS COG write +
quantity tag, and the returned contract scalars. The fields come from the
``telemac_result`` fixture -- no docker / TELEMAC / S3 / case data.
"""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.workflows.telemac import postprocess_telemac as P


VARS = ["VELOCITY U", "WATER DEPTH", "FREE SURFACE", "BOTTOM"]


def _malpasset_like(telemac_result):
    """7 nodes: 5 wet (peak FS <= 12), 1 node dry-with-HIGH-terrain at f0 (FS=25),
    1 far node never wet (FS=30). If the wet mask works, wse_max == 12 (the dry
    FS=25/30 terrain values are excluded); if it leaks, wse_max would be 25 or 30.
    """
    # x, y in local metres (mesh frame).
    x = [5000.0, 5100.0, 5000.0, 5100.0, 5050.0, 5050.0, 7000.0]
    y = [4000.0, 4000.0, 4100.0, 4100.0, 4050.0, 3950.0, 4000.0]
    ikle = [[0, 1, 4], [1, 3, 4], [2, 3, 4], [0, 2, 4], [0, 4, 5], [1, 4, 5]]
    times = [0.0, 60.0]
    depth = {
        # node:            n1    n2    n3    n4    n5    n6    n7(far)
        0: np.array([0.0, 0.0, 4.0, 2.0, 3.0, 2.5, 0.0]),   # frame 0
        1: np.array([3.0, 2.0, 1.0, 0.0, 2.0, 1.5, 0.0]),   # frame 1
    }
    fs = {
        0: np.array([25.0, 10.0, 12.0, 11.0, 12.0, 11.5, 30.0]),  # n1 dry-high(25), n7 dry(30)
        1: np.array([12.0, 11.0, 9.0, 8.0, 11.0, 10.5, 30.0]),
    }
    bottom = np.array([25.0, 8.0, 8.0, 9.0, 9.0, 9.0, 30.0])
    return telemac_result(varnames=VARS, x=x, y=y, ikle=ikle, times=times, data={
        "VELOCITY U": [np.zeros(7), np.zeros(7)],
        "WATER DEPTH": [depth[0], depth[1]],
        "FREE SURFACE": [fs[0], fs[1]],
        "BOTTOM": [bottom, bottom],
    })


def test_pick_named_var():
    assert P._pick_named_var(VARS, P._WSE_VAR_KEYS, "S") == "FREE SURFACE"
    assert P._pick_named_var(VARS, P._DEPTH_VAR_KEYS, "H") == "WATER DEPTH"
    # French deck names.
    fr = ["SURFACE LIBRE", "HAUTEUR D'EAU"]
    assert P._pick_named_var(fr, P._WSE_VAR_KEYS, "S") == "SURFACE LIBRE"
    assert P._pick_named_var(fr, P._DEPTH_VAR_KEYS, "H") == "HAUTEUR D'EAU"
    assert P._pick_named_var(["VELOCITY U", "BOTTOM"], P._WSE_VAR_KEYS, "S") is None


def test_wse_wet_mask_excludes_dry_terrain(tmp_path, telemac_result):
    slf = tmp_path / "wse.slf"
    _malpasset_like(telemac_result)
    layers, metrics = P.postprocess_telemac_wse(
        slf,
        run_id="TESTWSE0000000000000000AA",
        mesh_epsg=32632,
        reach_name="malpasset",
        vertical_datum="NGF",
        _output_dir=str(tmp_path),
    )
    wse = layers[0]
    # THE masking assertion: dry-terrain FS (25 at n1 frame0, 30 at the never-wet
    # far node) must be excluded -> the peak WATER surface is 12, not 25/30.
    assert metrics["wse_max_m"] == pytest.approx(12.0)
    assert wse.wse_max_m == pytest.approx(12.0)
    # the never-wet far node is dropped; 6 near-cluster nodes were wet.
    assert metrics["n_wet_nodes"] == 6
    assert wse.quantity == "water_surface_elevation"
    assert wse.mesh_epsg == 32632
    assert wse.vertical_datum == "NGF"
    assert wse.n_frames == 2


def test_wse_cog_crs_and_quantity_tag(tmp_path, telemac_result):
    import rasterio

    slf = tmp_path / "wse.slf"
    _malpasset_like(telemac_result)
    layers, _ = P.postprocess_telemac_wse(
        slf, run_id="TESTWSE0000000000000000BB", mesh_epsg=32632,
        vertical_datum="NGF", _output_dir=str(tmp_path),
    )
    with rasterio.open(layers[0].uri) as src:
        assert src.crs is not None and src.crs.to_epsg() == 32632
        tags = dict(src.tags())
        tags.update(src.tags(1))
        # the quantity tag drives extract_model_at_observations' model-quantity
        # resolution (elevation family) for a like-for-like WSE pairing.
        assert tags.get("quantity") == "water_surface_elevation"
        # projected metric bounds (not lon/lat degrees): pixels are metres.
        assert abs(src.bounds.left) > 360


def test_wse_depth_mode(tmp_path, telemac_result):
    slf = tmp_path / "wse.slf"
    _malpasset_like(telemac_result)
    layers, metrics = P.postprocess_telemac_wse(
        slf, run_id="TESTWSE0000000000000000CC", mesh_epsg=32632,
        quantity="depth", _output_dir=str(tmp_path),
    )
    assert metrics["quantity"] == "water_depth"
    assert layers[0].quantity == "water_depth"
    # max depth over the wet frames (n3 frame0 depth 4.0 is the deepest).
    assert metrics["wse_max_m"] == pytest.approx(4.0)


def test_a_depth_field_renders_dry_ground_dry_and_nodata_only_off_the_domain(
        tmp_path, telemac_result):
    """n7 never went wet. As NODATA it punches a hole through the map and reads
    as a broken raster; its own zero depth is the run's answer for that node."""
    slf = tmp_path / "wse.slf"
    _malpasset_like(telemac_result)
    _layers, metrics = P.postprocess_telemac_wse(
        slf, run_id="TESTWSE0000000000000000EE", mesh_epsg=32632,
        quantity="depth", _output_dir=str(tmp_path),
    )
    assert metrics["wse_min_m"] == 0.0
    # the wet-node COUNT is unmoved by the dry zeros - 6 of the 7 nodes went wet.
    assert metrics["n_wet_nodes"] == 6
    assert "dry renders DRY" in metrics["honesty_label"]


def test_the_p99_depth_is_measured_beside_the_maximum_over_the_wet_nodes(
        tmp_path, telemac_result):
    """One pit ponding to its rim sets the maximum; the percentile is the field."""
    slf = tmp_path / "wse.slf"
    _malpasset_like(telemac_result)
    _layers, metrics = P.postprocess_telemac_wse(
        slf, run_id="TESTWSE0000000000000000FF", mesh_epsg=32632,
        quantity="depth", _output_dir=str(tmp_path),
    )
    # per-node peaks over the six wet nodes: 3, 2, 4, 2, 3, 2.5.
    assert metrics["wse_p99_m"] == pytest.approx(
        float(np.percentile([3.0, 2.0, 4.0, 2.0, 3.0, 2.5], 99)), abs=1e-4)
    assert metrics["wse_p99_m"] < metrics["wse_max_m"]


def test_a_free_surface_field_keeps_a_never_wet_node_nodata(tmp_path, telemac_result):
    """An ELEVATION has no dry floor: a dry node's free surface IS its bed, so
    filling it in would paint terrain as a water surface."""
    slf = tmp_path / "wse.slf"
    _malpasset_like(telemac_result)
    _layers, metrics = P.postprocess_telemac_wse(
        slf, run_id="TESTWSE00000000000000000G", mesh_epsg=32632,
        _output_dir=str(tmp_path),
    )
    assert metrics["n_wet_nodes"] == 6
    # the dry n1/n7 terrain elevations (25 / 30) never enter the range.
    assert metrics["wse_max_m"] == pytest.approx(12.0)
    assert metrics["wse_min_m"] > 0.0


def _everywhere_dry(telemac_result):
    """A CORRECT solve over a catchment that shed nothing: real frames, a real
    depth variable, and zero water at every node of every one of them."""
    return telemac_result(
        varnames=VARS, x=[0.0, 100.0, 0.0], y=[0.0, 0.0, 100.0],
        ikle=[[0, 1, 2]], times=[0.0, 600.0], data={
            "VELOCITY U": [np.zeros(3), np.zeros(3)],
            "WATER DEPTH": [np.zeros(3), np.zeros(3)],       # everywhere dry
            "FREE SURFACE": [np.array([5.0, 6.0, 7.0])] * 2,
            "BOTTOM": [np.array([5.0, 6.0, 7.0])] * 2,
        })


def test_wse_empty_dry_solve_raises(tmp_path, telemac_result):
    """An ELEVATION read of a dry solve has nothing to report but bed: every
    node's free surface IS its terrain, so the refusal stands on this path."""
    slf = tmp_path / "dry.slf"
    _everywhere_dry(telemac_result)
    with pytest.raises(P.PostprocessTelemacError) as ei:
        P.postprocess_telemac_wse(
            slf, run_id="TESTWSE0000000000000000DD", mesh_epsg=32632,
            _output_dir=str(tmp_path),
        )
    assert ei.value.error_code == "TELEMAC_OUTPUT_EMPTY"


def test_a_wholly_dry_depth_field_completes_stating_the_dryness(
        tmp_path, telemac_result):
    """DRY IS A VALID ANSWER: the solve reached its end and measured no water, so
    the depth field is zero at full extent and the layer says so in numbers. A
    refusal here would report a correct run as a broken one."""
    slf = tmp_path / "dry.slf"
    _everywhere_dry(telemac_result)
    layers, metrics = P.postprocess_telemac_wse(
        slf, run_id="TESTWSE0000000000000DRY1", mesh_epsg=32632,
        quantity="depth", _output_dir=str(tmp_path),
    )
    assert metrics["n_wet_nodes"] == 0
    assert metrics["wse_max_m"] == 0.0
    assert metrics["wse_min_m"] == 0.0
    assert metrics["wse_p99_m"] == 0.0
    assert metrics["n_frames"] == 2
    assert "MEASURED DRY" in metrics["honesty_label"]
    assert layers[0].quantity == "water_depth"
    assert layers[0].wse_max_m == 0.0
    # A ramp with no range paints every cell its MIDDLE colour, which renders a
    # dry catchment as a full basin of water. The floor keeps zero at the bottom.
    assert layers[0].legend.vmin == 0.0
    assert layers[0].legend.vmax == pytest.approx(P.TELEMAC_WSE_WET_DEPTH_M)
    assert "colour ramp spans" in metrics["honesty_label"]


def test_a_depth_read_with_no_variable_or_no_frames_still_refuses(
        tmp_path, telemac_result):
    """TRULY empty output is still empty: no depth variable and no time steps are
    failures of the run, not measurements of a dry one."""
    slf = tmp_path / "empty.slf"
    telemac_result(varnames=["VELOCITY U", "BOTTOM"], x=[0.0, 100.0, 0.0],
                   y=[0.0, 0.0, 100.0], ikle=[[0, 1, 2]], times=[0.0], data={
                       "VELOCITY U": [np.zeros(3)],
                       "BOTTOM": [np.array([5.0, 6.0, 7.0])],
                   })
    with pytest.raises(P.PostprocessTelemacError) as ei:
        P.postprocess_telemac_wse(
            slf, run_id="TESTWSE0000000000000DRY2", mesh_epsg=32632,
            quantity="depth", _output_dir=str(tmp_path),
        )
    assert ei.value.error_code == "TELEMAC_OUTPUT_EMPTY"

    noframes = tmp_path / "noframes.slf"
    empty = np.zeros((0, 3))
    telemac_result(varnames=VARS, x=[0.0, 100.0, 0.0], y=[0.0, 0.0, 100.0],
                   ikle=[[0, 1, 2]], times=[],
                   data={name: [empty] for name in VARS})
    with pytest.raises(P.PostprocessTelemacError) as ei:
        P.postprocess_telemac_wse(
            noframes, run_id="TESTWSE0000000000000DRY3", mesh_epsg=32632,
            quantity="depth", _output_dir=str(tmp_path),
        )
    assert ei.value.error_code == "TELEMAC_OUTPUT_EMPTY"
