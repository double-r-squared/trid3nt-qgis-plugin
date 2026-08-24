"""Tests for the MODFLOW deck-build + submit + postprocess engine seam.

engine-door refactor: the LLM-facing ``run_modflow_job`` wrapper is DELETED
(folded into the ``modflow_contaminant_plume`` template - see
``test_modflow_contaminant_plume.py`` for the single/multi known-answer + the
registration/tier coverage). This file keeps the ENGINE-level coverage that is
unchanged by the fold:

  * Deck layout matches the solver-entrypoint expectations (gwf/ + gwt/ subdir
    layout, package-ref rewrites, manifest model_crs) - the single/archetype
    deck path is untouched by the multi_species branch.
  * ``submit_modflow_run`` typed-error contract.
  * ``postprocess_modflow`` plume-metric math on synthetic arrays.
  * A FULL local-mode deck-build -> mf6 -> completion.json run against a real
    ``mf6`` binary when one is available (skipped otherwise).

No Gemini/Vertex calls anywhere - the cloud path is mocked, the local path
shells out to ``mf6`` directly.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from trid3nt_contracts import new_ulid
from trid3nt_contracts.modflow_contracts import MODFLOWRunArgs

from trid3nt_server.workflows.modflow import run_modflow as rm
from trid3nt_server.workflows.modflow import postprocess_modflow as pp


# --------------------------------------------------------------------------- #
# mf6 binary discovery (for the live local-mode test)
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _find_mf6() -> str | None:
    """Locate a runnable mf6 binary: $TRID3NT_MF6_BIN, PATH, or the job-0220/0221
    download evidence dirs. Returns None if none is found (the live test skips)."""
    env = os.environ.get("TRID3NT_MF6_BIN")
    if env and Path(env).exists():
        return env
    on_path = shutil.which("mf6")
    if on_path:
        return on_path
    for cand in _REPO_ROOT.rglob("mf6.5.0_linux/bin/mf6"):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


_MF6_BIN = _find_mf6()
_HAVE_FLOPY = True
try:  # flopy is required for the deck build + UCN read
    import flopy  # type: ignore[import-not-found]  # noqa: F401
except Exception:  # noqa: BLE001
    _HAVE_FLOPY = False


_SPILL_ARGS = MODFLOWRunArgs(
    spill_location_latlon=(26.64, -81.87),
    contaminant="benzene",
    release_rate_kg_s=0.01,
    duration_days=30.0,
    aquifer_k_ms=1e-4,
    porosity=0.3,
)


# --------------------------------------------------------------------------- #
# Deck staging: gwf/ + gwt/ subdir layout + model_crs (entrypoint contract)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not _HAVE_FLOPY, reason="flopy not installed")
def test_deck_staging_subdir_layout_and_model_crs(tmp_path: Path) -> None:
    """Staged deck matches the entrypoint's gwf/+gwt/ layout + manifest model_crs."""
    staging = rm.build_and_stage_modflow_deck(
        _SPILL_ARGS, workdir=tmp_path, stage_to_gcs=False
    )

    assert staging.model_crs.startswith("EPSG:")
    assert staging.model_crs == "EPSG:32617"

    dests = {i["dest"] for i in staging.manifest_inputs}
    assert "gwf/gwf_model.nam" in dests
    assert "gwf/gwf_model.dis" in dests
    assert "gwt/gwt_model.nam" in dests
    assert "gwt/gwt_model.ucn" not in dests  # output, not input
    assert "gwt/gwt_model.src" in dests
    assert "mfsim.nam" in dests
    assert "mfsim.tdis" in dests
    assert "gwfgwt.exg" in dests
    assert "gwf_model.nam" not in dests
    assert "gwt_model.nam" not in dests

    deck = Path(staging.local_deck_dir)
    assert (deck / "gwf" / "gwf_model.nam").exists()
    assert (deck / "gwt" / "gwt_model.nam").exists()
    assert (deck / "mfsim.nam").exists()
    assert (deck / "gwfgwt.exg").exists()

    import json

    manifest = json.loads((deck / "manifest.json").read_text())
    assert manifest["model_crs"] == staging.model_crs
    assert "mfsim.lst" in manifest["outputs"]
    assert any("ucn" in o for o in manifest["outputs"])


@pytest.mark.skipif(not _HAVE_FLOPY, reason="flopy not installed")
def test_deck_namefile_package_refs_rewritten_to_subdir(tmp_path: Path) -> None:
    """The GWF/GWT namefiles reference package files via the subdir prefix."""
    staging = rm.build_and_stage_modflow_deck(
        _SPILL_ARGS, workdir=tmp_path, stage_to_gcs=False
    )
    deck = Path(staging.local_deck_dir)

    gwf_nam = (deck / "gwf" / "gwf_model.nam").read_text()
    assert "gwf/gwf_model.dis" in gwf_nam
    assert "gwf/gwf_model.npf" in gwf_nam

    gwt_nam = (deck / "gwt" / "gwt_model.nam").read_text()
    assert "gwt/gwt_model.dis" in gwt_nam
    assert "gwt/gwt_model.src" in gwt_nam

    mfsim = (deck / "mfsim.nam").read_text()
    assert "gwf/gwf_model.nam" in mfsim
    assert "gwt/gwt_model.nam" in mfsim
    assert "gwf/gwf_model.ims" in mfsim
    assert "gwt/gwt_model.ims" in mfsim


# --------------------------------------------------------------------------- #
# submit_modflow_run - local-exec dispatch typed-error contract
# --------------------------------------------------------------------------- #


def test_submit_modflow_run_dispatch_failure_is_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local-exec dispatch failure (no runs bucket) surfaces MODFLOW_DISPATCH_FAILED."""
    monkeypatch.setenv("TRID3NT_SOLVER_BACKEND", "local-docker")
    monkeypatch.delenv("TRID3NT_RUNS_BUCKET", raising=False)
    from trid3nt_server.workflows.solver import solver as _solver

    monkeypatch.setattr(_solver, "_RUNS_BUCKET", None, raising=False)
    run_id = new_ulid()
    staging = rm.DeckStaging(
        run_id=run_id,
        manifest_uri=f"s3://bucket/modflow/{run_id}/manifest.json",
        deck_base_uri=f"s3://bucket/modflow/{run_id}/",
        local_deck_dir="/tmp/none",
        model_crs="EPSG:32617",
        gwf_name="gwf_model",
        gwt_name="gwt_model",
        spill_lat=26.64,
        spill_lon=-81.87,
        output_globs=["gwt_model.ucn"],
    )
    with pytest.raises(rm.MODFLOWWorkflowError) as exc:
        rm.submit_modflow_run(staging)
    assert exc.value.error_code == "MODFLOW_DISPATCH_FAILED"


# --------------------------------------------------------------------------- #
# postprocess_modflow plume-metric math on synthetic arrays
# --------------------------------------------------------------------------- #


def test_compute_plume_metrics_counts_above_floor() -> None:
    """max + area computed from a synthetic 2D concentration grid."""
    import numpy as np

    grid = np.zeros((4, 4), dtype="float64")
    grid[1, 1] = 5.0
    grid[1, 2] = 2.0
    grid[2, 2] = 0.0005  # below floor
    max_conc, area_km2 = pp.compute_plume_metrics(grid, cell_area_m2=2500.0)

    assert max_conc == pytest.approx(5.0)
    assert area_km2 == pytest.approx(0.005)


def test_compute_plume_metrics_clamps_negative_max_to_zero() -> None:
    """A numerically-negative dispersion artifact never narrates as < 0."""
    import numpy as np

    grid = np.full((3, 3), -1e-10, dtype="float64")
    max_conc, area_km2 = pp.compute_plume_metrics(grid, cell_area_m2=2500.0)
    assert max_conc == 0.0
    assert area_km2 == 0.0


def test_compute_plume_metrics_handles_nan_and_empty() -> None:
    """NaN-masked cells are ignored; empty grids yield zeros."""
    import numpy as np

    grid = np.array([[np.nan, 3.0], [np.nan, np.nan]], dtype="float64")
    max_conc, area_km2 = pp.compute_plume_metrics(grid, cell_area_m2=10_000.0)
    assert max_conc == pytest.approx(3.0)
    assert area_km2 == pytest.approx(0.01)

    empty = np.zeros((0, 0), dtype="float64")
    assert pp.compute_plume_metrics(empty, cell_area_m2=2500.0) == (0.0, 0.0)


# --------------------------------------------------------------------------- #
# Local-mode deck-build -> mf6 -> completion.json (live) - skipped if no binary
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    _MF6_BIN is None or not _HAVE_FLOPY,
    reason="mf6 binary and/or flopy not available",
)
def test_run_modflow_local_writes_completion(tmp_path: Path) -> None:
    """run_modflow_local reaches Normal termination + writes a completion.json."""
    import json

    rm.set_mf6_binary(_MF6_BIN)
    try:
        staging = rm.build_and_stage_modflow_deck(
            _SPILL_ARGS, workdir=tmp_path, stage_to_gcs=False
        )
        uri = rm.run_modflow_local(staging)
        assert uri.startswith("file://")
        deck = Path(staging.local_deck_dir)
        completion = json.loads((deck / "completion.json").read_text())
        assert completion["status"] == "ok"
        assert completion["exit_code"] == 0
        assert completion["converged"] is True
        assert completion["model_crs"] == "EPSG:32617"
        assert "Normal termination of simulation" in (deck / "mfsim.lst").read_text()
        assert (deck / "gwt_model.ucn").exists()
    finally:
        rm.set_mf6_binary(None)
