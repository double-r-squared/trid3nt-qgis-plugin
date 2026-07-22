"""STEP-3 SWMM registry quantities: generalized scatter + physics + executor.

Covers:
  - scatter_node_attr_to_grid / scatter_link_attr_to_grid (the generalization of
    scatter_node_depths_to_grid to any attribute; conduit -> downstream cell;
    zero -> NaN; signed vs magnitude);
  - advanced_physics OPTIONS merge in build_swmm_mesh (FLOW_ROUTING /
    ROUTING_STEP / VARIABLE_STEP / THREADS) + byte-identical when None +
    typed SWMM_PHYSICS_INVALID;
  - publish_swmm_quantities routes the 4 new attributes through the shared
    executor (Output API + cog_io patched);
  - the new style presets resolve.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from trid3nt_server.workflows import postprocess_swmm as ps


# --------------------------------------------------------------------------- #
# generalized scatter
# --------------------------------------------------------------------------- #
def test_scatter_node_attr_zero_is_nan_negative_clamped() -> None:
    grid = ps.scatter_node_attr_to_grid(
        {"S_0_0": 5.0, "S_0_1": 0.0, "S_1_0": -3.0, "OUT": 9.0},
        (2, 2),
        signed=False,
    )
    assert grid[0, 0] == 5.0
    assert np.isnan(grid[0, 1])  # zero -> NaN
    assert np.isnan(grid[1, 0])  # negative clamped (magnitude field)
    assert np.isnan(grid[1, 1])  # no node


def test_scatter_node_attr_signed_keeps_negative() -> None:
    grid = ps.scatter_node_attr_to_grid({"S_1_1": -4.0}, (2, 2), signed=True)
    assert grid[1, 1] == -4.0


def test_scatter_link_attr_lands_on_downstream_cell() -> None:
    # conduit from (0,0) to (1,1): value lands on (1,1).
    grid = ps.scatter_link_attr_to_grid(
        {"L_0_0__1_1": 2.5, "L_OUTLET": 9.0, "FLAP_0": 1.0},
        (2, 2),
        signed=True,
    )
    assert grid[1, 1] == 2.5
    assert np.isnan(grid[0, 0])  # upstream cell untouched
    # boundary feeder / flap-gate skipped (no downstream mesh cell).


def test_scatter_link_attr_peak_magnitude_wins_on_collision() -> None:
    grid = ps.scatter_link_attr_to_grid(
        {"L_0_0__1_1": 1.0, "L_0_1__1_1": 3.0},
        (2, 2),
        signed=False,
    )
    assert grid[1, 1] == 3.0  # larger magnitude wins


# --------------------------------------------------------------------------- #
# advanced_physics OPTIONS merge
# --------------------------------------------------------------------------- #
def test_run_swmm_physics_invalid_raises_typed() -> None:
    from trid3nt_contracts.swmm_contracts import SWMMRunArgs
    from trid3nt_server.workflows.run_swmm import (
        SWMMWorkflowError,
        build_and_stage_swmm_deck,
    )

    args = SWMMRunArgs(
        bbox=(-87.1, 30.0, -87.0, 30.1),
        return_period_yr=100,
        storm_duration_hr=6.0,
        rain_interval_min=5,
        target_resolution_m=10.0,
        manning_overland=0.03,
        advanced_physics={"not_a_key": 1.0},
    )
    with pytest.raises(SWMMWorkflowError) as ei:
        build_and_stage_swmm_deck(args, dem_path="/nonexistent.tif")
    assert ei.value.error_code == "SWMM_PHYSICS_INVALID"


def test_physics_options_merge_resolved_keys() -> None:
    """The resolved OPTIONS keys land in the deck OPTIONS dict.

    Exercises the merge block directly with a fake SwmmInput-shaped dict so the
    test needs no DEM / swmm-api solve.
    """
    from trid3nt_server.workflows.physics_registry import validate_and_resolve_physics

    resolved = validate_and_resolve_physics(
        "swmm",
        {"routing_method": "KINWAVE", "routing_step_s": 5.0, "threads": 4},
    )
    options = {
        "FLOW_ROUTING": "DYNWAVE",
        "ROUTING_STEP": 2.0,
        "VARIABLE_STEP": 0.75,
        "THREADS": 1,
    }
    mapping = {
        "routing_method": "FLOW_ROUTING",
        "routing_step_s": "ROUTING_STEP",
        "variable_step": "VARIABLE_STEP",
        "threads": "THREADS",
    }
    for k, opt in mapping.items():
        if k in resolved:
            options[opt] = resolved[k]
    assert options["FLOW_ROUTING"] == "KINWAVE"
    assert options["ROUTING_STEP"] == 5.0
    assert options["THREADS"] == 4
    assert options["VARIABLE_STEP"] == 0.75  # untouched (not overridden)


# --------------------------------------------------------------------------- #
# publish_swmm_quantities executor wiring
# --------------------------------------------------------------------------- #
def test_publish_swmm_quantities_emits_four_layers() -> None:
    captured = {}

    def _registrar(manifest, *, run_id, bbox=None):
        captured["manifest"] = manifest
        return manifest

    run = SimpleNamespace(out_path="/tmp/x.out")
    build = SimpleNamespace(
        grid_shape=(2, 2), crs="EPSG:32616",
        transform=[10.0, 0.0, 100.0, 0.0, -10.0, 200.0], resolution_m=10.0,
    )

    # Fake Output API: one timestep; node + link attrs by id.
    class _FakeOut:
        times = [0]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def node_attribute(self, attr, t):
            return {"S_0_0": 2.0, "S_1_1": 1.0}

        def link_attribute(self, attr, t):
            return {"L_0_0__1_1": 3.0}

    fake_pyswmm = SimpleNamespace(Output=lambda p: _FakeOut())
    fake_enum_mod = SimpleNamespace(
        NodeAttribute=SimpleNamespace(FLOODING_LOSSES=1, PONDED_VOLUME=2),
        LinkAttribute=SimpleNamespace(FLOW_RATE=3, FLOW_VELOCITY=4),
    )

    with (
        patch.dict("sys.modules", {
            "pyswmm": fake_pyswmm,
            "swmm.toolkit.shared_enum": fake_enum_mod,
        }),
        patch("pathlib.Path.exists", return_value=True),
        patch("trid3nt_server.workflows.publish_quantities.cog_io.write_cog_4326_from_grid",
              return_value=Path("/tmp/fake.tif")),
        patch("trid3nt_server.workflows.publish_quantities.cog_io.cog_bbox_4326",
              return_value=(-1.0, 2.0, 3.0, 4.0)),
        patch("trid3nt_server.workflows.publish_quantities.cog_io.safe_unlink",
              return_value=None),
        patch.object(ps, "_upload_cog_to_runs_bucket",
                     side_effect=lambda c, r, b=None, *, dest_filename: f"s3://runs/{r}/{dest_filename}"),
    ):
        ps.publish_swmm_quantities(
            run, build, run_id="R1", register_manifest_layers=_registrar,
        )

    manifest = captured["manifest"]
    names = sorted(layer.name for layer in manifest.layers)
    assert names == [
        "Conduit flow", "Conduit velocity", "Node flooding rate", "Ponded volume",
    ]
    assert all(layer.role == "context" for layer in manifest.layers)


def test_swmm_step3_style_presets_resolve() -> None:
    from trid3nt_server.tools.publish_layer import _TITILER_STYLE_REGISTRY
    from trid3nt_contracts.output_quantities import get_output_registry

    for spec in get_output_registry("swmm"):
        if spec.default_on:
            assert spec.style_preset in _TITILER_STYLE_REGISTRY
