"""HEC-RAS engine #11 landing tests (contract + postprocess + dispatch gate).

Offline-first: the postprocess runs against a COMPACT solved-HDF fixture
(``muncie_flood_solved/muncie_p04_solved_min.hdf``, ~112 KB) extracted from a real
in-container Muncie solve (2026-08-04) -- the geometry (cells + min-elevation +
facepoints + perimeter + projection) plus the ``Results`` max-water-surface -- so
the depth/rasterize/COG path is exercised with NO engine invocation. The live
canary (a real RasUnsteady solve) is the acceptance run; this is the deterministic
offline gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trid3nt_contracts.hecras_contracts import (
    HECRAS_ARCHETYPES,
    HECRAS_DEPTH_STYLE_PRESET,
    HECRAS_ERROR_CODES,
    HECRASRunArgs,
    HecrasDepthLayerURI,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE = (
    _REPO_ROOT
    / "workers" / "hecras" / "fixtures" / "muncie_flood_solved"
    / "muncie_p04_solved_min.hdf"
)


# --------------------------------------------------------------------------- #
# Contract round-trip
# --------------------------------------------------------------------------- #
def test_runargs_defaults_and_roundtrip():
    args = HECRASRunArgs()
    assert args.archetype == "muncie_riverine_flood"
    assert args.flow_scale == 1.0
    dumped = args.model_dump()
    back = HECRASRunArgs.model_validate(dumped)
    assert back == args


def test_runargs_flow_scale_band_enforced():
    HECRASRunArgs(flow_scale=1.3)  # in-band OK
    with pytest.raises(Exception):
        HECRASRunArgs(flow_scale=99.0)  # above the modelable band
    with pytest.raises(Exception):
        HECRASRunArgs(flow_scale=0.0)


def test_runargs_target_peak_optional():
    a = HECRASRunArgs(target_peak_cfs=42000.0)
    assert a.target_peak_cfs == 42000.0


def test_depth_layer_uri_fields_and_roundtrip():
    layer = HecrasDepthLayerURI(
        layer_id="hecras-depth-peak-x",
        name="Peak flood depth (HEC-RAS 2D)",
        layer_type="raster",
        uri="s3://runs/x/hecras_depth_peak.tif",
        style_preset=HECRAS_DEPTH_STYLE_PRESET,
        depth_max_ft=20.6,
        depth_mean_ft=7.8,
        wet_cell_count=4998,
        wse_max_ft=951.9,
        flow_scale=1.3,
        peak_inflow_cfs=27300.0,
        volume_error_pct=0.0056,
        n_cells=5765,
    )
    back = HecrasDepthLayerURI.model_validate(layer.model_dump())
    assert back.depth_max_ft == 20.6
    assert back.flow_scale == 1.3
    assert back.style_preset == "continuous_flood_depth"


def test_error_codes_and_archetypes_present():
    assert "muncie_riverine_flood" in HECRAS_ARCHETYPES
    assert "muncie_levee_breach" in HECRAS_ARCHETYPES
    for code in ("HECRAS_SOLVE_FAILED", "HECRAS_INPUT_INVALID",
                 "HECRAS_FINISHED_SENTINEL_MISSING", "HECRAS_OUTPUT_EMPTY"):
        assert code in HECRAS_ERROR_CODES


def test_runargs_levee_breach_archetype_and_toggle():
    """The levee-breach archetype + breach_enabled knob round-trip (ADR 0125)."""
    a = HECRASRunArgs(archetype="muncie_levee_breach", breach_enabled=False)
    assert a.archetype == "muncie_levee_breach"
    assert a.breach_enabled is False
    back = HECRASRunArgs.model_validate(a.model_dump())
    assert back == a
    # default archetype keeps breach_enabled True (the levee-fails default)
    assert HECRASRunArgs().breach_enabled is True


def test_depth_layer_uri_dry_levee_held_is_valid():
    """A levee-HELD result is a valid DRY layer: 0 wet cells / 0 depth, breach off."""
    layer = HecrasDepthLayerURI(
        layer_id="hecras-depth-peak-dry",
        name="Peak flood depth (HEC-RAS 2D, 2D Interior Area) -- LEVEE HELD",
        layer_type="raster",
        uri="s3://runs/dry/hecras_depth_peak.tif",
        style_preset=HECRAS_DEPTH_STYLE_PRESET,
        depth_max_ft=0.0,
        depth_mean_ft=0.0,
        wet_cell_count=0,
        flow_scale=1.0,
        n_cells=5765,
        breach_enabled=False,
    )
    back = HecrasDepthLayerURI.model_validate(layer.model_dump())
    assert back.wet_cell_count == 0 and back.depth_max_ft == 0.0
    assert back.breach_enabled is False


# --------------------------------------------------------------------------- #
# Postprocess against the compact solved fixture
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _FIXTURE.exists(), reason="solved fixture absent")
def test_postprocess_depth_and_mesh(fake_s3, monkeypatch):
    monkeypatch.setenv("TRID3NT_RUNS_BUCKET", "test-runs")
    from trid3nt_server.workflows.hecras.postprocess_hecras import postprocess_hecras

    layers, metrics = postprocess_hecras(
        _FIXTURE,
        run_id="pp-test",
        flow_scale=1.0,
        peak_inflow_cfs=21000.0,
        volume_error_pct=0.0058,
        fallback_note="DEMONSTRATION GEOMETRY",
    )
    # primary depth layer + context mesh layer
    assert len(layers) == 2
    depth = layers[0]
    assert isinstance(depth, HecrasDepthLayerURI)
    assert depth.role == "primary"
    assert depth.layer_type == "raster"
    assert depth.style_preset == "continuous_flood_depth"
    assert depth.units == "ft"
    # physical sanity: the baseline Muncie solve floods a large 2D domain
    assert depth.depth_max_ft > 5.0
    assert depth.wet_cell_count and depth.wet_cell_count > 1000
    assert depth.n_cells == 5765
    assert depth.peak_inflow_cfs == pytest.approx(21000.0)
    assert depth.fallback_note and "DEMONSTRATION" in depth.fallback_note
    assert depth.legend is not None and depth.legend.units == "ft"

    mesh = layers[1]
    assert mesh.layer_type == "vector"
    assert mesh.role == "context"
    assert mesh.style_preset == "mesh_grid"
    assert mesh.bbox is None  # a context mesh must not fight the flood camera

    # the COG + mesh objects were uploaded to the runs bucket
    keys = set(fake_s3.store)
    assert any("hecras_depth_peak.tif" in k for k in keys)
    assert any("mesh.geojson" in k for k in keys)

    # the inflow-forcing series scales with the flow multiplier (invariant 1)
    series = metrics["inflow_hydrograph"]
    assert series and max(p["q_cfs"] for p in series) == pytest.approx(21000.0, rel=1e-3)


def _write_dry_2d_hdf(path) -> None:
    """A minimal plan HDF whose 2D area is entirely DRY (levee-holds case).

    Only the two datasets ``_read_depth_per_cell`` reads: a Results max-WSE (all
    <= 0, i.e. dry) + the geometry Cells Minimum Elevation. No engine, no mesh."""
    import h5py
    import numpy as np

    area = "2D Interior Area"
    with h5py.File(path, "w") as f:
        f.create_group(f"Geometry/2D Flow Areas/{area}")
        f[f"Geometry/2D Flow Areas/{area}"].create_dataset(
            "Cells Minimum Elevation", data=np.array([940.0, 941.0, 942.0], dtype=np.float32)
        )
        base = ("Results/Unsteady/Output/Output Blocks/Base Output/Summary Output/"
                f"2D Flow Areas/{area}")
        f.create_group(base)
        # all dry: WSE == 0 for every cell (a dry HEC-RAS cell stores WSE 0)
        f[base].create_dataset(
            "Maximum Water Surface", data=np.array([[0.0, 0.0, 0.0]], dtype=np.float32)
        )


def test_read_depth_per_cell_allow_dry_valid_success(tmp_path):
    """0 wet cells raises HECRAS_OUTPUT_EMPTY by default, but is a valid dry
    success under allow_dry (the levee-holds case -- ADR 0125)."""
    from trid3nt_server.workflows.hecras.postprocess_hecras import (
        PostprocessHecrasError,
        _read_depth_per_cell,
    )

    hdf = tmp_path / "dry.hdf"
    _write_dry_2d_hdf(hdf)

    with pytest.raises(PostprocessHecrasError) as ei:
        _read_depth_per_cell(hdf, allow_dry=False)
    assert ei.value.error_code == "HECRAS_OUTPUT_EMPTY"

    depth, wet, area, stats = _read_depth_per_cell(hdf, allow_dry=True)
    assert area == "2D Interior Area"
    assert stats["wet_cell_count"] == 0
    assert stats["depth_max_ft"] == 0.0
    assert stats["depth_mean_ft"] == 0.0
    assert stats["n_cells"] == 3
    assert not bool(wet.any())


@pytest.mark.skipif(not _FIXTURE.exists(), reason="solved fixture absent")
def test_postprocess_scaled_flow_deepens_and_widens(fake_s3, monkeypatch):
    """The scaled-flow sanity check on the SAME fixture: a higher flow_scale label
    threads through to a proportionally scaled inflow series + peak (the physics
    delta itself is proven by the live canary's two solves)."""
    monkeypatch.setenv("TRID3NT_RUNS_BUCKET", "test-runs")
    from trid3nt_server.workflows.hecras.postprocess_hecras import postprocess_hecras

    _, m10 = postprocess_hecras(_FIXTURE, run_id="s10", flow_scale=1.0, peak_inflow_cfs=21000.0)
    _, m13 = postprocess_hecras(_FIXTURE, run_id="s13", flow_scale=1.3, peak_inflow_cfs=27300.0)
    p10 = max(p["q_cfs"] for p in m10["inflow_hydrograph"])
    p13 = max(p["q_cfs"] for p in m13["inflow_hydrograph"])
    assert p13 == pytest.approx(p10 * 1.3, rel=1e-3)


# --------------------------------------------------------------------------- #
# Dispatch: the Finished-sentinel / correct_end classify_exit gate
# --------------------------------------------------------------------------- #
def test_classify_exit_ok_on_correct_end(tmp_path):
    from trid3nt_server.workflows.hecras.run_hecras import _classify_exit

    (tmp_path / "hecras_metrics.json").write_text(
        '{"correct_end": true, "flow_scale": 1.3, "peak_inflow_cfs": 27300.0, '
        '"volume_accounting": {"Error Percent": 0.0056}}'
    )
    status, code, err, extra = _classify_exit(tmp_path, 0)
    assert status == "ok" and code == 0 and err is None
    assert extra["flow_scale"] == 1.3
    assert extra["volume_error_pct"] == pytest.approx(0.0056)


def test_classify_exit_error_when_sentinel_missing(tmp_path):
    """A clean process exit but NO correct_end (no Finished sentinel / Results) is
    an honest failure, not an empty success."""
    from trid3nt_server.workflows.hecras.run_hecras import _classify_exit

    (tmp_path / "hecras_metrics.json").write_text('{"correct_end": false}')
    status, code, err, _ = _classify_exit(tmp_path, 0)
    assert status == "error" and code == 2 and err


def test_classify_exit_error_on_nonzero_exit(tmp_path):
    from trid3nt_server.workflows.hecras.run_hecras import _classify_exit

    status, code, err, _ = _classify_exit(tmp_path, 1)
    assert status == "error" and code == 1 and err


def test_solver_registered():
    from trid3nt_server.workflows.hecras.run_hecras import (
        HECRAS_SOLVER_NAME,
        HECRAS_LEVEE_BREACH_SOLVER_NAME,
    )
    from trid3nt_server.workflows.solver.solver import (
        SOLVER_WORKFLOW_REGISTRY,
        LOCAL_SOLVER_SPEC_REGISTRY,
    )

    for name in (HECRAS_SOLVER_NAME, HECRAS_LEVEE_BREACH_SOLVER_NAME):
        assert name in SOLVER_WORKFLOW_REGISTRY
        assert name in LOCAL_SOLVER_SPEC_REGISTRY
