"""Tests for the MODFLOW agent integration (job-0227, sprint-13 Stage 2).

Coverage (kickoff acceptance):

  * Deck layout matches the solver-entrypoint expectations — the staged deck
    uses the ``gwf/`` + ``gwt/`` subdir layout, the model namefiles + package
    references are rewritten into those subdirs, the simulation roots stay flat,
    and the manifest carries the OQ-MOD-3 ``model_crs`` field.
  * ``ExecutionHandle`` shape from the Cloud Workflows submit path (mocked).
  * ``postprocess_modflow`` plume-metric math on synthetic concentration arrays.
  * ``run_modflow_job`` registration presence + FR-DC-6 metadata + hazard
    category membership.
  * A FULL local-mode end-to-end run against the downloaded ``mf6`` binary when
    one is available (skipped otherwise) — the same path as the live-evidence
    harness.

No Gemini/Vertex calls anywhere — the cloud path is mocked, the local path
shells out to ``mf6`` directly.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from grace2_contracts import new_ulid
from grace2_contracts.modflow_contracts import MODFLOWRunArgs, PlumeLayerURI

from grace2_agent.workflows import run_modflow as rm
from grace2_agent.workflows import postprocess_modflow as pp


# --------------------------------------------------------------------------- #
# mf6 binary discovery (for the live local-mode test)
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _find_mf6() -> str | None:
    """Locate a runnable mf6 binary: $GRACE2_MF6_BIN, PATH, or the job-0220/0221
    download evidence dirs. Returns None if none is found (the live test skips)."""
    env = os.environ.get("GRACE2_MF6_BIN")
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
)


# --------------------------------------------------------------------------- #
# Registration presence
# --------------------------------------------------------------------------- #


def test_run_modflow_job_registered_uncacheable() -> None:
    """run_modflow_job is in TOOL_REGISTRY with FR-DC-6 workflow_dispatch shape."""
    # Importing the tools package (eager imports) registers the tool.
    import grace2_agent.tools as tools_pkg
    import grace2_agent.tools.run_modflow_tool  # noqa: F401 — fire @register_tool

    entry = tools_pkg.TOOL_REGISTRY.get("run_modflow_job")
    assert entry is not None, "run_modflow_job not registered"
    md = entry.metadata
    assert md.ttl_class == "live-no-cache"
    assert md.source_class == "workflow_dispatch"
    assert md.cacheable is False


def test_run_modflow_job_in_hazard_modeling_category() -> None:
    """run_modflow_job is primary-categorised under hazard_modeling."""
    from grace2_agent.categories import PRIMARY_CATEGORY, tools_for_category

    assert PRIMARY_CATEGORY.get("run_modflow_job") == "hazard_modeling"
    assert "run_modflow_job" in tools_for_category("hazard_modeling")


# --------------------------------------------------------------------------- #
# Deck staging: gwf/ + gwt/ subdir layout + model_crs (entrypoint contract)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not _HAVE_FLOPY, reason="flopy not installed")
def test_deck_staging_subdir_layout_and_model_crs(tmp_path: Path) -> None:
    """Staged deck matches the entrypoint's gwf/+gwt/ layout + manifest model_crs.

    The CRITICAL Stage-1 handoff: the entrypoint reconstructs ``inputs[].dest``
    and echoes ``manifest["model_crs"]`` into completion.json for the
    postprocess reprojection. We assert:
      - gwf_model.* land under gwf/, gwt_model.* under gwt/;
      - mfsim.* + the GWF-GWT exchange stay at the deck root (flat);
      - manifest.model_crs is a populated EPSG string;
      - manifest.outputs capture the concentration + list files.
    """
    staging = rm.build_and_stage_modflow_deck(
        _SPILL_ARGS, workdir=tmp_path, stage_to_gcs=False
    )

    # model_crs populated (OQ-MOD-3 handoff field).
    assert staging.model_crs.startswith("EPSG:")
    # Fort Myers → UTM 17N.
    assert staging.model_crs == "EPSG:32617"

    dests = {i["dest"] for i in staging.manifest_inputs}
    # GWF + GWT files live in subdirs; namefiles included.
    assert "gwf/gwf_model.nam" in dests
    assert "gwf/gwf_model.dis" in dests
    assert "gwt/gwt_model.nam" in dests
    assert "gwt/gwt_model.ucn" not in dests  # output, not input
    assert "gwt/gwt_model.src" in dests
    # Simulation roots stay flat.
    assert "mfsim.nam" in dests
    assert "mfsim.tdis" in dests
    assert "gwfgwt.exg" in dests
    # No file is double-prefixed or left flat when it should be in a subdir.
    assert "gwf_model.nam" not in dests
    assert "gwt_model.nam" not in dests

    # On-disk deck mirrors the dest layout.
    deck = Path(staging.local_deck_dir)
    assert (deck / "gwf" / "gwf_model.nam").exists()
    assert (deck / "gwt" / "gwt_model.nam").exists()
    assert (deck / "mfsim.nam").exists()
    assert (deck / "gwfgwt.exg").exists()

    # The manifest.json file the entrypoint reads carries model_crs + outputs.
    import json

    manifest = json.loads((deck / "manifest.json").read_text())
    assert manifest["model_crs"] == staging.model_crs
    assert "mfsim.lst" in manifest["outputs"]
    assert any("ucn" in o for o in manifest["outputs"])


@pytest.mark.skipif(not _HAVE_FLOPY, reason="flopy not installed")
def test_deck_namefile_package_refs_rewritten_to_subdir(tmp_path: Path) -> None:
    """The GWF/GWT namefiles reference package files via the subdir prefix.

    This is the fix for the trap that flopy writes package refs relative to the
    sim CWD (bare ``gwf_model.dis``); once the namefile lives in ``gwf/`` the
    bare ref would not resolve. We assert the namefile package lines carry the
    ``gwf/`` / ``gwt/`` prefix, and mfsim.nam references the subdir namefiles.
    """
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
# submit_modflow_run — local-exec dispatch (GCP Cloud Workflows decommissioned)
# --------------------------------------------------------------------------- #
#
# The GCP Cloud Workflows submit path (a fake ``executions_v1.ExecutionsClient``
# via ``rm.set_workflows_client``) is removed with the backend. ``submit_modflow
# _run`` now unconditionally routes through ``tools.solver.launch_local_solver``
# (local-exec). The happy-path local-exec submit is covered end-to-end in
# test_modflow_local_backend.py (with a fake S3 client + mf6 shim); here we keep
# the typed-error contract guard.


def test_submit_modflow_run_dispatch_failure_is_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local-exec dispatch failure (no runs bucket → staging fails) surfaces
    MODFLOW_DISPATCH_FAILED — the typed contract the dispatch carries."""
    # No GRACE2_RUNS_BUCKET → launch_local_solver raises SolverDispatchError,
    # which submit_modflow_run wraps as MODFLOW_DISPATCH_FAILED.
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    monkeypatch.delenv("GRACE2_RUNS_BUCKET", raising=False)
    from grace2_agent.tools import solver as _solver

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

    # 4x4 grid; cell area 2500 m² (50 m cells).
    grid = np.zeros((4, 4), dtype="float64")
    grid[1, 1] = 5.0  # plume peak
    grid[1, 2] = 2.0  # plume
    grid[2, 2] = 0.0005  # below floor (0.001) → NOT plume
    max_conc, area_km2 = pp.compute_plume_metrics(grid, cell_area_m2=2500.0)

    assert max_conc == pytest.approx(5.0)
    # Two cells above floor × 2500 m² = 5000 m² = 0.005 km².
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
    assert area_km2 == pytest.approx(0.01)  # 1 cell × 10000 m² = 0.01 km²

    empty = np.zeros((0, 0), dtype="float64")
    assert pp.compute_plume_metrics(empty, cell_area_m2=2500.0) == (0.0, 0.0)


# --------------------------------------------------------------------------- #
# Local-mode FULL end-to-end (live mf6 run) — skipped if no binary/flopy
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    _MF6_BIN is None or not _HAVE_FLOPY,
    reason="mf6 binary and/or flopy not available",
)
@pytest.mark.asyncio
async def test_run_modflow_job_local_end_to_end(monkeypatch: Any) -> None:
    """GRACE2_MODFLOW_LOCAL=1: MODFLOWRunArgs -> deck -> mf6 -> PlumeLayerURI.

    The same chain as the live-evidence harness, exercised through the atomic
    tool. publish is skipped (no QGIS Server / GCS in tests) via the local
    ``file://`` URI guard in _dispatch_publish_layer.
    """
    from grace2_agent.tools.run_modflow_tool import run_modflow_job

    monkeypatch.setenv("GRACE2_MODFLOW_LOCAL", "1")
    monkeypatch.setenv("GRACE2_MF6_BIN", _MF6_BIN)  # type: ignore[arg-type]

    result = await run_modflow_job(
        spill_location_latlon=(26.64, -81.87),
        contaminant="benzene",
        release_rate_kg_s=0.01,
        duration_days=30.0,
    )

    assert isinstance(result, PlumeLayerURI), f"got {result!r}"
    assert result.layer_type == "raster"
    assert result.units == "mg/L"
    assert result.style_preset == "continuous_plume_concentration"
    # Acceptance: non-zero peak concentration + plume area > 0.
    assert result.max_concentration_mgl > 0.0
    assert result.plume_area_km2 > 0.0
    # Reprojected to EPSG:4326 — bbox near Fort Myers (lon ~ -81.87, lat ~ 26.64).
    assert result.bbox is not None
    min_lon, min_lat, max_lon, max_lat = result.bbox
    assert -82.5 < min_lon < -81.0
    assert 26.0 < min_lat < 27.5


@pytest.mark.skipif(
    _MF6_BIN is None or not _HAVE_FLOPY,
    reason="mf6 binary and/or flopy not available",
)
def test_run_modflow_local_writes_completion(tmp_path: Path) -> None:
    """run_modflow_local reaches Normal termination + writes a completion.json.

    Asserts the local completion.json mirrors the cloud entrypoint's schema
    (status/exit_code/converged/model_crs) so postprocess reads it identically.
    """
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
        # The list file proves Normal termination.
        assert "Normal termination of simulation" in (deck / "mfsim.lst").read_text()
        # The concentration output was produced.
        assert (deck / "gwt_model.ucn").exists()
    finally:
        rm.set_mf6_binary(None)


def test_run_modflow_job_rejects_incomplete_params() -> None:
    """Missing required params surface a typed error dict (no exception)."""
    import asyncio

    from grace2_agent.tools.run_modflow_tool import run_modflow_job

    result = asyncio.run(run_modflow_job(contaminant="benzene"))
    assert isinstance(result, dict)
    assert result["status"] == "error"
    assert result["error_code"] == "MODFLOW_PARAMS_INCOMPLETE"


# --------------------------------------------------------------------------- #
# job-0317: Bedrock Claude passes spill_location_latlon as a STRING, not a
# JSON array. The previous ``tuple(float(v) for v in ...)`` iterated the
# string's CHARACTERS -> float('.') -> MODFLOW_PARAMS_INVALID (non-retryable).
# These tests prove the string forms now coerce, and a genuinely-bad value
# still returns a clean typed error.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "latlon_str,expected_lat,expected_lon",
    [
        ("40.8088861,-96.7077751", 40.8088861, -96.7077751),  # the live form
        ("40.81, -96.71", 40.81, -96.71),
        ("[40.81, -96.71]", 40.81, -96.71),
        ("(40.81, -96.71)", 40.81, -96.71),
        ("40.81 -96.71", 40.81, -96.71),
    ],
)
def test_run_modflow_job_coerces_string_latlon(
    monkeypatch: Any,
    latlon_str: str,
    expected_lat: float,
    expected_lon: float,
) -> None:
    """A STRING spill_location_latlon reaches the deck-build stage as (lat, lon).

    mf6-independent: we stub ``build_and_stage_modflow_deck`` to capture the
    coerced ``MODFLOWRunArgs`` and raise a sentinel, proving the string got
    past the coercion (no character-iteration crash) and was parsed correctly.
    """
    import asyncio

    import grace2_agent.tools.run_modflow_tool as rmt

    captured: dict[str, Any] = {}

    class _Sentinel(Exception):
        pass

    def _fake_build_and_stage(run_args: MODFLOWRunArgs) -> Any:
        captured["run_args"] = run_args
        raise _Sentinel("reached deck build")

    monkeypatch.setattr(rmt, "build_and_stage_modflow_deck", _fake_build_and_stage)

    result = asyncio.run(
        rmt.run_modflow_job(
            spill_location_latlon=latlon_str,
            contaminant="benzene",
            release_rate_kg_s=0.01,
            duration_days=30.0,
        )
    )

    # The string was coerced (NOT a MODFLOW_PARAMS_INVALID char-iteration crash):
    # the run reached the deck-build stage where our sentinel fired and was
    # caught by the defensive catch-all -> MODFLOW_INTERNAL_ERROR.
    assert "run_args" in captured, "deck build was never reached — coercion failed"
    run_args = captured["run_args"]
    lat, lon = run_args.spill_location_latlon
    assert lat == pytest.approx(expected_lat)
    assert lon == pytest.approx(expected_lon)
    assert isinstance(result, dict)
    assert result["status"] == "error"
    assert result["error_code"] == "MODFLOW_INTERNAL_ERROR"
    assert result["error_code"] != "MODFLOW_PARAMS_INVALID"


def test_run_modflow_job_accepts_real_list_latlon(monkeypatch: Any) -> None:
    """A genuine 2-list still passes through coercion unchanged."""
    import asyncio

    import grace2_agent.tools.run_modflow_tool as rmt

    captured: dict[str, Any] = {}

    class _Sentinel(Exception):
        pass

    def _fake_build_and_stage(run_args: MODFLOWRunArgs) -> Any:
        captured["run_args"] = run_args
        raise _Sentinel("reached deck build")

    monkeypatch.setattr(rmt, "build_and_stage_modflow_deck", _fake_build_and_stage)

    asyncio.run(
        rmt.run_modflow_job(
            spill_location_latlon=[40.81, -96.71],
            contaminant="benzene",
            release_rate_kg_s=0.01,
            duration_days=30.0,
        )
    )
    lat, lon = captured["run_args"].spill_location_latlon
    assert (lat, lon) == pytest.approx((40.81, -96.71))


@pytest.mark.parametrize(
    "bad_latlon",
    [
        "not-a-coordinate",
        "40.81",  # only one number
        "40.81, -96.71, 12.0",  # three numbers
        "40.81,abc",  # one non-numeric part
        [40.81],  # wrong element count
        [40.81, -96.71, 12.0],  # three elements
    ],
)
def test_run_modflow_job_rejects_bad_latlon_typed(bad_latlon: Any) -> None:
    """A genuinely-bad latlon returns MODFLOW_PARAMS_INVALID (no exception)."""
    import asyncio

    from grace2_agent.tools.run_modflow_tool import run_modflow_job

    result = asyncio.run(
        run_modflow_job(
            spill_location_latlon=bad_latlon,
            contaminant="benzene",
            release_rate_kg_s=0.01,
            duration_days=30.0,
        )
    )
    assert isinstance(result, dict)
    assert result["status"] == "error"
    assert result["error_code"] == "MODFLOW_PARAMS_INVALID"
    assert "spill_location_latlon" in result["error_message"]
