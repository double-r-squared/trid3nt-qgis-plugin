"""STEP-3 MODFLOW registry quantities: deck-builder physics + style presets.

Covers:
  - the deck-builder advanced_physics wiring (GwtMst sorption/decay + GwtDsp
    dispersivity) is byte-identical when physics is None / {}, and applies the
    resolved overrides when given (gated on flopy);
  - the new style presets resolve.

The `publish_modflow_quantities` executor path (the dormant `output_quantities`
scaffold half for MODFLOW) was DEAD CODE (never called by a composer) and is
DELETED in ADR 0284; the transport family's concentration/temperature animation
is now the emit-on-solve seam, pinned by `tests/test_modflow_outputs_seam.py`.
"""

from __future__ import annotations

import importlib.util
import tempfile

import pytest

from trid3nt_server.workflows.modflow import postprocess_modflow as pm

_HAS_FLOPY = importlib.util.find_spec("flopy") is not None


# --------------------------------------------------------------------------- #
# deck-builder advanced_physics wiring (gated on flopy)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _HAS_FLOPY, reason="flopy not installed")
def test_physics_off_is_default_conservative_tracer() -> None:
    """advanced_physics None/{} -> NO sorption, NO decay (byte-identical)."""
    import flopy

    from trid3nt_server.workflows.modflow.run_modflow import build_modflow_deck

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

    from trid3nt_server.workflows.modflow.run_modflow import build_modflow_deck

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

    from trid3nt_server.workflows.modflow.run_modflow import build_modflow_deck

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
    from trid3nt_contracts.modflow_contracts import MODFLOWRunArgs
    from trid3nt_server.workflows.modflow.run_modflow import (
        MODFLOWWorkflowError,
        build_and_stage_modflow_deck,
    )

    args = MODFLOWRunArgs(
        spill_location_latlon=(30.0, -87.0),
        contaminant="TCE",
        release_rate_kg_s=0.1,
        duration_days=10.0,
        aquifer_k_ms=1e-4,
        porosity=0.3,
        advanced_physics={"not_a_real_key": 1.0},
    )
    with pytest.raises(MODFLOWWorkflowError) as ei:
        build_and_stage_modflow_deck(args, stage_to_gcs=False)
    assert ei.value.error_code == "MODFLOW_PHYSICS_INVALID"


# --------------------------------------------------------------------------- #
# The concentration-animation + water-table publish path (``publish_modflow_
# quantities``) was DEAD CODE -- defined + unit-tested but NEVER called by any
# composer (grep-to-zero). It is DELETED for MODFLOW scope in ADR 0284: the
# transport family's concentration animation is now the emit-on-solve seam
# (postprocess_multi_species / postprocess_gwe_thermal write outputs.json; the
# composers read it back frames_only). The seam producer + fork are pinned by
# ``tests/test_modflow_outputs_seam.py``. The OUTPUT_QUANTITIES modflow registry
# specs remain (the scaffold; DELETION_LEDGER row 18) until the full scaffold
# deletion. This module keeps the still-live deck-builder physics wiring + the
# style-preset resolution checks below.
# --------------------------------------------------------------------------- #


def test_modflow_step3_style_presets_resolve() -> None:
    from trid3nt_server.emission.publish import _QGIS_STYLE_REGISTRY

    assert pm.HEAD_STYLE_PRESET in _QGIS_STYLE_REGISTRY
