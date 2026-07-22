"""STEP-3 Landlab registry quantities: secondary fields + physics merge.

Covers:
  - build_landlab_build_spec merges validated advanced_physics (flow_director /
    overland_alpha / mannings_n) and is byte-identical when None;
  - an invalid physics key raises LANDLAB_PHYSICS_INVALID;
  - publish_landlab_quantities reads the secondary COGs (rasterio patched) and
    routes them through the shared executor (cog_io patched), producing one
    raster layer per supplied token mapped to the right OutputQuantitySpec;
  - the new style presets resolve.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from trid3nt_server.workflows import postprocess_landlab as pl
from trid3nt_server.workflows.run_landlab import (
    LandlabWorkflowError,
    build_landlab_build_spec,
)
from trid3nt_contracts.landlab_contracts import LandlabRunArgs

_BBOX = (-122.1, 46.0, -122.0, 46.1)


def _args(**kw) -> LandlabRunArgs:
    return LandlabRunArgs(bbox=_BBOX, **kw)


# --------------------------------------------------------------------------- #
# physics merge into build_spec
# --------------------------------------------------------------------------- #
def test_build_spec_physics_none_is_byte_identical() -> None:
    base = build_landlab_build_spec(_args())
    assert "flow_director" not in base
    assert "overland_alpha" not in base and "mannings_n" not in base


def test_build_spec_merges_validated_physics() -> None:
    spec = build_landlab_build_spec(
        _args(advanced_physics={"flow_director": "Dinf", "mannings_n": 0.05,
                                "overland_alpha": 0.6})
    )
    assert spec["flow_director"] == "Dinf"
    assert spec["mannings_n"] == 0.05
    assert spec["overland_alpha"] == 0.6


def test_build_spec_invalid_physics_raises_typed() -> None:
    with pytest.raises(LandlabWorkflowError) as ei:
        build_landlab_build_spec(_args(advanced_physics={"bogus_key": 1.0}))
    assert ei.value.error_code == "LANDLAB_PHYSICS_INVALID"


def test_build_spec_out_of_range_physics_raises_typed() -> None:
    # mannings_n range is (0.01, 0.2) in the registry.
    with pytest.raises(LandlabWorkflowError) as ei:
        build_landlab_build_spec(_args(advanced_physics={"mannings_n": 5.0}))
    assert ei.value.error_code == "LANDLAB_PHYSICS_INVALID"


# --------------------------------------------------------------------------- #
# publish_landlab_quantities executor wiring
# --------------------------------------------------------------------------- #
def test_publish_landlab_quantities_emits_one_layer_per_token() -> None:
    import numpy as np

    captured = {}

    def _registrar(manifest, *, run_id, bbox=None):
        captured["manifest"] = manifest
        return manifest

    grid = np.array([[0.1, 0.2], [0.3, 0.4]], dtype="float64")

    secondary = {
        "drainage_area": "/tmp/da.tif",
        "slope": "/tmp/slope.tif",
        "factor_of_safety": "/tmp/fos.tif",
    }

    with (
        patch.object(pl, "_read_cog_grid_and_georef",
                     return_value=(grid, "EPSG:32610", "T")),
        patch("trid3nt_server.workflows.publish_quantities.cog_io.write_cog_4326_from_grid",
              return_value=Path("/tmp/fake.tif")),
        patch("trid3nt_server.workflows.publish_quantities.cog_io.cog_bbox_4326",
              return_value=(-1.0, 2.0, 3.0, 4.0)),
        patch("trid3nt_server.workflows.publish_quantities.cog_io.safe_unlink",
              return_value=None),
        patch.object(pl, "_upload_cog_to_runs_bucket",
                     side_effect=lambda c, r, b=None, *, dest_filename: f"s3://runs/{r}/{dest_filename}"),
    ):
        pl.publish_landlab_quantities(
            secondary, run_id="R1", register_manifest_layers=_registrar,
        )

    manifest = captured["manifest"]
    names = sorted(layer.name for layer in manifest.layers)
    assert names == ["Drainage area", "Factor of safety", "Topographic slope"]
    # each is a context raster.
    assert all(layer.role == "context" for layer in manifest.layers)
    assert all(layer.layer_type == "raster" for layer in manifest.layers)


def test_publish_landlab_quantities_empty_returns_none() -> None:
    assert pl.publish_landlab_quantities(
        {}, run_id="R", register_manifest_layers=lambda *a, **k: None,
    ) is None


def test_landlab_step3_style_presets_resolve() -> None:
    from trid3nt_server.tools.publish_layer import _TITILER_STYLE_REGISTRY
    from trid3nt_contracts.output_quantities import get_output_registry

    for spec in get_output_registry("landlab"):
        if spec.default_on:
            assert spec.style_preset in _TITILER_STYLE_REGISTRY
