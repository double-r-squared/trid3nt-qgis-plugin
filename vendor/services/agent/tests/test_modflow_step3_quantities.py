"""STEP-3 MODFLOW registry quantities: concentration animation + head + physics.

Covers:
  - the deck-builder advanced_physics wiring (GwtMst sorption/decay + GwtDsp
    dispersivity) is byte-identical when physics is None / {}, and applies the
    resolved overrides when given (gated on flopy);
  - the OC saverecord flips to ALL concentration steps;
  - ``publish_modflow_quantities`` routes the new readers through the shared
    executor: a concentration TimeseriesField (peak + frames) + a head
    RasterField, registered via the ONE registrar (cog_io patched, no rasterio);
  - the new style presets resolve.
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from grace2_agent.workflows import postprocess_modflow as pm

_HAS_FLOPY = importlib.util.find_spec("flopy") is not None


# --------------------------------------------------------------------------- #
# deck-builder advanced_physics wiring (gated on flopy)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _HAS_FLOPY, reason="flopy not installed")
def test_physics_off_is_default_conservative_tracer() -> None:
    """advanced_physics None/{} -> NO sorption, NO decay (byte-identical)."""
    import flopy

    from grace2_agent.workflows.run_modflow import build_modflow_deck

    d = tempfile.mkdtemp()
    build_modflow_deck(
        (30.0, -87.0), "TCE", 0.1, 10.0, 1e-4, 0.3, workdir=d, write=True,
        advanced_physics=None,
    )
    sim = flopy.mf6.MFSimulation.load(sim_ws=d, verbosity_level=0)
    mst = sim.get_model("gwt_model").get_package("mst")
    # sorption / decay are absent (None) on a conservative tracer.
    assert mst.sorption.get_data() in (None, "")
    assert mst.first_order_decay.get_data() in (None, False)


@pytest.mark.skipif(not _HAS_FLOPY, reason="flopy not installed")
def test_physics_overrides_apply_sorption_decay_dispersivity() -> None:
    import flopy

    from grace2_agent.workflows.run_modflow import build_modflow_deck

    d = tempfile.mkdtemp()
    build_modflow_deck(
        (30.0, -87.0), "TCE", 0.1, 10.0, 1e-4, 0.3, workdir=d, write=True,
        advanced_physics={
            "sorption_kd": 5.0,
            "bulk_density": 1700.0,
            "decay_rate_per_day": 0.02,
            "long_dispersivity_m": 25.0,
            "trans_dispersivity_m": 2.5,
        },
    )
    sim = flopy.mf6.MFSimulation.load(sim_ws=d, verbosity_level=0)
    gwt = sim.get_model("gwt_model")
    mst = gwt.get_package("mst")
    dsp = gwt.get_package("dsp")
    assert str(mst.sorption.get_data()).lower() == "linear"
    assert float(mst.distcoef.get_data().flat[0]) == 5.0
    assert float(mst.bulk_density.get_data().flat[0]) == 1700.0
    assert bool(mst.first_order_decay.get_data()) is True
    assert float(mst.decay.get_data().flat[0]) == 0.02
    assert float(dsp.alh.get_data().flat[0]) == 25.0
    assert float(dsp.ath1.get_data().flat[0]) == 2.5


@pytest.mark.skipif(not _HAS_FLOPY, reason="flopy not installed")
def test_oc_saves_all_concentration_steps() -> None:
    import flopy

    from grace2_agent.workflows.run_modflow import build_modflow_deck

    d = tempfile.mkdtemp()
    build_modflow_deck(
        (30.0, -87.0), "TCE", 0.1, 10.0, 1e-4, 0.3, workdir=d, write=True,
    )
    sim = flopy.mf6.MFSimulation.load(sim_ws=d, verbosity_level=0)
    oc = sim.get_model("gwt_model").get_package("oc")
    save = oc.saverecord.get_data()[0]
    settings = {(str(r["rtype"]).lower(), str(r["ocsetting"]).lower()) for r in save}
    assert ("concentration", "all") in settings


def test_physics_invalid_key_raises_typed_error() -> None:
    """An out-of-registry physics key surfaces MODFLOW_PHYSICS_INVALID."""
    from grace2_contracts.modflow_contracts import MODFLOWRunArgs
    from grace2_agent.workflows.run_modflow import (
        MODFLOWWorkflowError,
        build_and_stage_modflow_deck,
    )

    args = MODFLOWRunArgs(
        spill_location_latlon=(30.0, -87.0),
        contaminant="TCE",
        release_rate_kg_s=0.1,
        duration_days=10.0,
        advanced_physics={"not_a_real_key": 1.0},
    )
    with pytest.raises(MODFLOWWorkflowError) as ei:
        build_and_stage_modflow_deck(args, stage_to_gcs=False)
    assert ei.value.error_code == "MODFLOW_PHYSICS_INVALID"


# --------------------------------------------------------------------------- #
# publish_modflow_quantities executor wiring (cog_io patched, no rasterio/flopy)
# --------------------------------------------------------------------------- #
def _fake_grid(v: float):
    return [[v, v], [v, v]]


def test_publish_modflow_quantities_emits_timeseries_and_head() -> None:
    captured = {}

    def _registrar(manifest, *, run_id, bbox=None):
        captured["manifest"] = manifest
        return manifest

    conc_grids = [_fake_grid(1.0), _fake_grid(2.0), _fake_grid(3.0)]
    geo = {"xorigin": 0.0, "yorigin": 0.0, "delr": 50.0, "delc": 50.0,
           "nrow": 2, "ncol": 2}

    with (
        patch.object(pm, "_grid_georegistration_from_deck", return_value=geo),
        patch.object(pm, "_resolve_ucn_path", return_value=Path("/tmp/x.ucn")),
        patch.object(pm, "_resolve_gwf_hds_path", return_value=Path("/tmp/x.hds")),
        patch.object(pm, "_read_concentration_steps",
                     return_value=(conc_grids, conc_grids[-1])),
        patch.object(pm, "_read_head_grid", return_value=__import__("numpy").array(
            [[10.0, 11.0], [12.0, 13.0]])),
        patch("grace2_agent.workflows.publish_quantities.cog_io.write_cog_4326_from_grid",
              return_value=Path("/tmp/fake.tif")),
        patch("grace2_agent.workflows.publish_quantities.cog_io.cog_bbox_4326",
              return_value=(-1.0, 2.0, 3.0, 4.0)),
        patch("grace2_agent.workflows.publish_quantities.cog_io.safe_unlink",
              return_value=None),
        patch.object(pm, "_upload_cog",
                     side_effect=lambda c, r, b, *, cog_filename: f"s3://runs/{r}/{cog_filename}"),
    ):
        pm.publish_modflow_quantities(
            "file:///tmp/run", run_id="R1", model_crs="EPSG:32617",
            register_manifest_layers=_registrar,
        )

    manifest = captured["manifest"]
    names = [layer.name for layer in manifest.layers]
    # concentration animation: peak + 3 frames; head: 1 raster.
    assert "Peak plume concentration" in names
    assert "Plume concentration step 1" in names
    assert "Plume concentration step 3" in names
    assert "Water table (head)" in names
    # head metrics bubbled up.
    assert "max_head_m" in manifest.metrics
    assert manifest.metrics.get("max_concentration_mgl") is not None
    # the provenance rows (plume-concentration / river-seepage) are NOT published.
    stems = [layer.layer_id_stem for layer in manifest.layers]
    assert not any(s.startswith("river-seepage") for s in stems)


def test_modflow_step3_style_presets_resolve() -> None:
    from grace2_agent.tools.publish_layer import _TITILER_STYLE_REGISTRY

    assert pm.HEAD_STYLE_PRESET in _TITILER_STYLE_REGISTRY
