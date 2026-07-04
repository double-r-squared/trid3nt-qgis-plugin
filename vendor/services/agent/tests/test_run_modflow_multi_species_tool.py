"""Tests for ``run_modflow_multi_species_job`` (the Wave-3 N-species engine tool).

Covers the engine-surface tool's contract WITHOUT a real mf6 run for the unit
cases (the build + run are monkeypatched), plus an optional REAL local-mode
end-to-end (deck build -> mf6 -> N per-species plumes) when the binary is present:

  * the no-species guard returns a typed error (never a fabricated run);
  * ``build_multi_species_staging`` threads the species list + archetype into the
    adapter (the staging seam that ``build_and_stage_modflow_deck`` does NOT cover);
  * the empty-result honesty floor: a run whose every species plume is at/below the
    detection floor returns a typed empty-result error;
  * the full LOCAL chain (real mf6) returns a ``MultiSpeciesPlumeResult``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from grace2_contracts.modflow_contracts import (
    MODFLOWRunArgs,
    MultiSpeciesPlumeResult,
    PlumeLayerURI,
)

from grace2_agent.tools import run_modflow_multi_species_tool as tool


_REPO_ROOT = Path(__file__).resolve().parents[4]


def _find_mf6() -> str | None:
    env = os.environ.get("GRACE2_MF6_BIN")
    if env and Path(env).exists():
        return env
    on_path = shutil.which("mf6")
    if on_path:
        return on_path
    if Path("/tmp/mf6bin/mf6").exists():
        return "/tmp/mf6bin/mf6"
    for cand in _REPO_ROOT.rglob("mf6.5.0_linux/bin/mf6"):
        return str(cand)
    return None


_MF6_BIN = _find_mf6()
try:
    import flopy  # type: ignore[import-not-found]  # noqa: F401

    _HAVE_FLOPY = True
except Exception:  # noqa: BLE001
    _HAVE_FLOPY = False

requires_mf6 = pytest.mark.skipif(
    _MF6_BIN is None or not _HAVE_FLOPY,
    reason="real mf6 multi_species run needs a runnable mf6 + flopy",
)


def _run_args(species: Any) -> MODFLOWRunArgs:
    return MODFLOWRunArgs(
        spill_location_latlon=(26.64, -81.87),
        contaminant="TCE",
        release_rate_kg_s=0.01,
        duration_days=15.0,
        archetype="multi_species",
        species=species,
    )


TWO_SPECIES = [
    {"name": "TCE", "release_rate_kg_s": 0.01, "sorption_kd": 0.2, "decay_per_day": 0.01},
    {"name": "cis-DCE", "release_rate_kg_s": 0.0, "decay_per_day": 0.02, "parent": "TCE"},
]


def _plume(species: str, conc: float) -> PlumeLayerURI:
    return PlumeLayerURI(
        layer_id=f"plume-concentration-{species.lower()}-T",
        name=f"Contaminant Plume - {species} (peak concentration)",
        layer_type="raster",
        uri=f"file:///tmp/{species.lower()}.tif",
        style_preset="continuous_plume_concentration",
        role="primary",
        units="mg/L",
        max_concentration_mgl=conc,
        plume_area_km2=conc * 0.1,
    )


@pytest.mark.asyncio
async def test_no_species_returns_typed_error() -> None:
    """An args with archetype=multi_species but no species -> typed error, no run."""
    args = MODFLOWRunArgs(
        spill_location_latlon=(26.64, -81.87),
        contaminant="TCE",
        release_rate_kg_s=0.01,
        duration_days=15.0,
        archetype="multi_species",
        species=None,
    )
    out = await tool.run_modflow_multi_species_job(args)
    assert isinstance(out, dict)
    assert out["error_code"] == "MODFLOW_MULTISPECIES_NO_SPECIES"


@pytest.mark.asyncio
async def test_empty_result_honesty_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """When every species plume is at/below the detection floor, the tool returns a
    typed empty-result error (no layers read as a successful modeled set)."""
    # Stub build + run so no mf6 is needed; postprocess returns all-empty plumes.
    monkeypatch.setattr(
        tool,
        "build_multi_species_staging",
        lambda run_args, **k: _FakeStaging(),
    )
    monkeypatch.setattr(tool, "is_local_mode", lambda: True)
    monkeypatch.setattr(tool, "run_modflow_local", lambda staging: "file:///tmp/run")
    monkeypatch.setattr(
        tool,
        "postprocess_multi_species",
        lambda *a, **k: MultiSpeciesPlumeResult(
            plumes=[_plume("TCE", 0.0), _plume("cis-DCE", 0.0)]
        ),
    )
    out = await tool.run_modflow_multi_species_job(_run_args(TWO_SPECIES))
    assert isinstance(out, dict)
    assert out["error_code"] == "MODFLOW_MULTISPECIES_EMPTY_RESULT"


@pytest.mark.asyncio
async def test_non_empty_result_returns_plumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tool, "build_multi_species_staging", lambda run_args, **k: _FakeStaging()
    )
    monkeypatch.setattr(tool, "is_local_mode", lambda: True)
    monkeypatch.setattr(tool, "run_modflow_local", lambda staging: "file:///tmp/run")
    monkeypatch.setattr(
        tool,
        "postprocess_multi_species",
        lambda *a, **k: MultiSpeciesPlumeResult(
            plumes=[_plume("TCE", 12.0), _plume("cis-DCE", 0.0)]
        ),
    )
    out = await tool.run_modflow_multi_species_job(_run_args(TWO_SPECIES))
    assert isinstance(out, MultiSpeciesPlumeResult)
    assert len(out.plumes) == 2


class _FakeStaging:
    run_id = "TESTRUN"
    model_crs = "EPSG:32617"
    local_deck_dir = "/tmp/does-not-matter"


@requires_mf6
@pytest.mark.asyncio
async def test_build_staging_threads_species_and_real_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """build_multi_species_staging writes a 2-GWT deck (the species ARE threaded),
    then the full local chain runs mf6 and returns two real plumes."""
    monkeypatch.setenv("GRACE2_MODFLOW_LOCAL", "1")
    monkeypatch.setenv("GRACE2_MF6_BIN", _MF6_BIN or "mf6")
    # Stub only COG write / upload / publish so we exercise the REAL deck + mf6 + UCN.
    from grace2_agent.workflows import postprocess_modflow as pp

    monkeypatch.setattr(pp, "_write_reprojected_cog", lambda *a, **k: tmp_path / "x.tif")
    monkeypatch.setattr(pp, "_cog_bbox_4326", lambda _p: (-81.9, 26.6, -81.8, 26.7))
    monkeypatch.setattr(
        pp, "_upload_cog", lambda cog, rid, bkt, **k: f"file:///{k['cog_filename']}"
    )
    monkeypatch.setattr(pp, "_dispatch_publish_layer", lambda *a, **k: None)

    # build the deck under tmp_path so the 2-GWT proof is inspectable.
    staging = tool.build_multi_species_staging(
        _run_args([
            {"name": "TCE", "release_rate_kg_s": 0.01, "decay_per_day": 0.01},
            {"name": "cis-DCE", "release_rate_kg_s": 0.002, "parent": "TCE"},
        ]),
        workdir=str(tmp_path),
    )
    deck = Path(staging.local_deck_dir)
    # The species WERE threaded: two distinct GWT models on disk.
    assert (deck / "gwt_tce.mst").is_file()
    assert (deck / "gwt_cis_dce.mst").is_file()
    assert staging.archetype == "multi_species"

    out = await tool.run_modflow_multi_species_job(
        _run_args([
            {"name": "TCE", "release_rate_kg_s": 0.01, "decay_per_day": 0.01},
            {"name": "cis-DCE", "release_rate_kg_s": 0.002, "parent": "TCE"},
        ])
    )
    assert isinstance(out, MultiSpeciesPlumeResult), out
    assert len(out.plumes) == 2
    tce = next(p for p in out.plumes if "TCE" in p.name)
    assert tce.max_concentration_mgl > 0.0
