"""Tests for the MODFLOW Wave-3 multi_species agent-side path.

Three layers, mirroring the single-species contamination + archetype tests:

  * ``postprocess_multi_species`` resolves N per-species ``gwt_<species>.ucn``,
    reads each (reusing the single-species concentration reader), and returns one
    ``PlumeLayerURI`` per species  -  exercised on SYNTHETIC ucn files (a fake
    HeadFile-backed dir) AND on a REAL local mf6 run when the binary is available
    (``TRID3NT_MODFLOW_LOCAL=1`` + ``TRID3NT_MF6_BIN``; the gap-closer for the N-GWT
    binary path).

  * ``normalize_species_list`` honesty gates: empty / malformed / duplicate /
    sourceless species lists raise ``MultiSpeciesInputError`` (never fabricate a
    contaminant).

  * The composer ``model_multi_species_scenario`` full chain through a FAKE
    ``run_modflow_multi_species_job`` (DI through the lazy tool import) returning N
    plumes, plus the typed-error surfacing of a failed run dict, plus tool
    registration + category membership.

The composer's solver tool is monkeypatched (no mf6 needed for the chain tests);
the postprocess + the optional real-run test exercise the live binary path.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from trid3nt_contracts.modflow_contracts import (
    MultiSpeciesPlumeResult,
    PlumeLayerURI,
    SpeciesSpec,
)

from trid3nt_server.tools import RegisteredTool, TOOL_REGISTRY
from trid3nt_server.workflows import postprocess_modflow as pp
from trid3nt_server.workflows import model_multi_species_scenario as ms
from trid3nt_server.workflows.model_multi_species_scenario import (
    MultiSpeciesInputError,
    MultiSpeciesResult,
    MultiSpeciesScenarioError,
    model_multi_species_scenario,
    normalize_species_list,
)


# --------------------------------------------------------------------------- #
# mf6 discovery (for the real local-mode postprocess test)
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _find_mf6() -> str | None:
    env = os.environ.get("TRID3NT_MF6_BIN")
    if env and Path(env).exists():
        return env
    on_path = shutil.which("mf6")
    if on_path:
        return on_path
    for cand in (Path("/tmp/mf6bin/mf6"),):
        if cand.exists():
            return str(cand)
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


# --------------------------------------------------------------------------- #
# Registry fakes (DI through the registry seam)
# --------------------------------------------------------------------------- #


def _fake_geocode(query: str, **_: Any) -> dict[str, Any]:
    return {"name": query, "latitude": 26.64, "longitude": -81.87, "source": "fake"}


def _install_fake_tool(name: str, fn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    existing = TOOL_REGISTRY.get(name)
    if existing is None:
        from trid3nt_server.tools.run_modflow_tool import _RUN_MODFLOW_JOB_METADATA

        metadata = _RUN_MODFLOW_JOB_METADATA
    else:
        metadata = existing.metadata
    monkeypatch.setitem(
        TOOL_REGISTRY, name, RegisteredTool(metadata=metadata, fn=fn, module="test")
    )


def _fake_plume(species: str, area: float, conc: float) -> PlumeLayerURI:
    return PlumeLayerURI(
        layer_id=f"plume-concentration-{species.lower()}-TEST",
        name=f"Contaminant Plume - {species} (peak concentration)",
        layer_type="raster",
        uri=f"file:///tmp/plume_{species.lower()}.tif",
        style_preset="continuous_plume_concentration",
        role="primary",
        units="mg/L",
        max_concentration_mgl=conc,
        plume_area_km2=area,
    )


TWO_SPECIES = [
    {"name": "TCE", "release_rate_kg_s": 0.01, "sorption_kd": 0.2, "decay_per_day": 0.01},
    {"name": "cis-DCE", "release_rate_kg_s": 0.0, "decay_per_day": 0.02, "parent": "TCE"},
]


# --------------------------------------------------------------------------- #
# normalize_species_list honesty gates (pure, no emitter / solver)
# --------------------------------------------------------------------------- #


def test_normalize_accepts_dicts_and_specs() -> None:
    specs = normalize_species_list(TWO_SPECIES)
    assert [s.name for s in specs] == ["TCE", "cis-DCE"]
    assert isinstance(specs[0], SpeciesSpec)
    # round-trips SpeciesSpec inputs too.
    again = normalize_species_list(specs)
    assert [s.name for s in again] == ["TCE", "cis-DCE"]


def test_normalize_rejects_none() -> None:
    with pytest.raises(MultiSpeciesInputError, match="non-empty list of species"):
        normalize_species_list(None)


def test_normalize_rejects_empty() -> None:
    with pytest.raises(MultiSpeciesInputError, match="non-empty list of species"):
        normalize_species_list([])


def test_normalize_rejects_malformed_species() -> None:
    with pytest.raises(MultiSpeciesInputError, match="invalid species spec"):
        normalize_species_list([{"release_rate_kg_s": 0.01}])  # no name


def test_normalize_rejects_duplicate_names() -> None:
    with pytest.raises(MultiSpeciesInputError, match="unique"):
        normalize_species_list(
            [
                {"name": "TCE", "release_rate_kg_s": 0.01},
                {"name": "TCE", "release_rate_kg_s": 0.02},
            ]
        )


def test_normalize_rejects_sourceless_list() -> None:
    """A list of pure daughter products (all rates 0) has no source to model."""
    with pytest.raises(MultiSpeciesInputError, match="positive release_rate_kg_s"):
        normalize_species_list(
            [
                {"name": "cis-DCE", "release_rate_kg_s": 0.0, "decay_per_day": 0.02},
                {"name": "VC", "release_rate_kg_s": 0.0, "decay_per_day": 0.03},
            ]
        )


# --------------------------------------------------------------------------- #
# postprocess_multi_species: per-species UCN resolution + label recovery
# --------------------------------------------------------------------------- #


def test_species_label_from_ucn_stem() -> None:
    assert pp._species_label_from_ucn_stem("gwt_tce") == "TCE"
    assert pp._species_label_from_ucn_stem("gwt_cis_dce") == "CIS_DCE"


def test_resolve_species_ucn_excludes_single_species_stem(tmp_path: Path) -> None:
    """The glob must EXCLUDE the single-species gwt_model.ucn so a spill deck is
    never mis-read as a one-species multi run, and find every gwt_<species>.ucn."""
    (tmp_path / "gwt_model.ucn").write_bytes(b"single")
    (tmp_path / "gwt_tce.ucn").write_bytes(b"a")
    (tmp_path / "gwt_cis_dce.ucn").write_bytes(b"b")
    hits = pp._resolve_species_ucn_paths(str(tmp_path))
    names = sorted(p.name for p in hits)
    assert names == ["gwt_cis_dce.ucn", "gwt_tce.ucn"]
    assert "gwt_model.ucn" not in names


def test_resolve_species_ucn_no_files_raises(tmp_path: Path) -> None:
    (tmp_path / "gwt_model.ucn").write_bytes(b"only-single")
    with pytest.raises(pp.PostprocessMODFLOWError) as exc:
        pp._resolve_species_ucn_paths(str(tmp_path))
    assert exc.value.error_code == "PLUME_OUTPUT_READ_FAILED"


def test_postprocess_multi_species_synthetic(monkeypatch, tmp_path: Path) -> None:
    """Two synthetic per-species ucn files -> two PlumeLayerURI, each carrying its
    OWN max_concentration_mgl + plume_area_km2 + the species name in the label.

    The per-species concentration READER + COG write + upload are stubbed so the
    test exercises the multi_species GLOB + ordering + per-species labelling +
    MultiSpeciesPlumeResult assembly without flopy/rasterio.
    """
    import numpy as np

    (tmp_path / "gwt_tce.ucn").write_bytes(b"a")
    (tmp_path / "gwt_cis_dce.ucn").write_bytes(b"b")

    # Distinct per-species grids so the two plumes carry DIFFERENT metrics.
    grids = {
        "gwt_tce.ucn": np.array([[10.0, 0.0], [0.0, 5.0]]),  # 2 cells > floor
        "gwt_cis_dce.ucn": np.array([[2.0, 0.0], [0.0, 0.0]]),  # 1 cell > floor
    }
    monkeypatch.setattr(
        pp, "_read_final_concentration", lambda path: grids[Path(path).name]
    )
    # 100 m cells -> 10000 m^2 / 1e6 = 0.01 km^2 per plume cell.
    monkeypatch.setattr(
        pp,
        "_grid_georegistration_from_deck",
        lambda _d: {"delr": 100.0, "delc": 100.0, "xorigin": 0.0,
                    "yorigin": 0.0, "nrow": 2, "ncol": 2},
    )
    monkeypatch.setattr(pp, "_write_reprojected_cog", lambda *a, **k: tmp_path / "x.tif")
    monkeypatch.setattr(pp, "_cog_bbox_4326", lambda _p: (-81.9, 26.6, -81.8, 26.7))
    monkeypatch.setattr(
        pp, "_upload_cog", lambda cog, rid, bkt, **k: f"s3://runs/{k['cog_filename']}"
    )
    monkeypatch.setattr(pp, "_dispatch_publish_layer", lambda *a, **k: None)

    result = pp.postprocess_multi_species(
        str(tmp_path),
        run_id="RUN1",
        model_crs="EPSG:32617",
        deck_dir=str(tmp_path),
        species_names=["TCE", "cis-DCE"],
    )
    assert isinstance(result, MultiSpeciesPlumeResult)
    assert len(result.plumes) == 2
    by_name = {p.name: p for p in result.plumes}
    tce = next(p for p in result.plumes if "TCE" in p.name)
    dce = next(p for p in result.plumes if "cis-DCE" in p.name)
    # TCE: max 10, 2 plume cells * 0.01 km^2 = 0.02; cis-DCE: max 2, 1 cell = 0.01.
    assert tce.max_concentration_mgl == pytest.approx(10.0)
    assert tce.plume_area_km2 == pytest.approx(0.02)
    assert dce.max_concentration_mgl == pytest.approx(2.0)
    assert dce.plume_area_km2 == pytest.approx(0.01)
    # Distinct layer ids + per-species COG filenames (no collision in the bucket).
    ids = {p.layer_id for p in result.plumes}
    assert len(ids) == 2
    assert all("RUN1" in p.layer_id for p in result.plumes)


@requires_mf6
def test_postprocess_multi_species_real_mf6(monkeypatch, tmp_path: Path) -> None:
    """Author a 2-species deck, run mf6, then postprocess BOTH per-species .ucn into
    two PlumeLayerURI from the REAL concentration output (the binary-path proof).

    Only the COG write + upload + publish are stubbed (no rasterio / S3); the UCN
    read + per-species plume metrics are the real flopy concentration arrays.
    """
    # The agent re-export inserts the worker dir into sys.path lazily (so flopy is
    # only imported when a MODFLOW tool runs) - use it instead of importing the
    # bare ``gwt_adapter`` module (which is not on the path by default).
    from trid3nt_server.workflows.run_modflow import build_modflow_deck

    d = build_modflow_deck(
        workdir=tmp_path,
        archetype="multi_species",
        species=[
            {"name": "TCE", "release_rate_kg_s": 0.01, "sorption_kd": 0.2, "decay_per_day": 0.01},
            {"name": "cis-DCE", "release_rate_kg_s": 0.0, "decay_per_day": 0.02, "parent": "TCE"},
        ],
        spill_location_latlon=(26.64, -81.87),
        contaminant="x",
        release_rate_kg_s=0.0,
        duration_days=15,
        aquifer_k_ms=1e-4,
        porosity=0.3,
        write=True,
    )
    import subprocess

    rc = subprocess.run([_MF6_BIN], cwd=str(tmp_path), capture_output=True, text=True)
    assert "Normal termination of simulation" in (rc.stdout + rc.stderr)

    # Stub only the COG write / upload / publish; the UCN read is REAL.
    monkeypatch.setattr(pp, "_write_reprojected_cog", lambda *a, **k: tmp_path / "x.tif")
    monkeypatch.setattr(pp, "_cog_bbox_4326", lambda _p: (-81.9, 26.6, -81.8, 26.7))
    monkeypatch.setattr(
        pp, "_upload_cog", lambda cog, rid, bkt, **k: f"file:///{k['cog_filename']}"
    )
    monkeypatch.setattr(pp, "_dispatch_publish_layer", lambda *a, **k: None)

    result = pp.postprocess_multi_species(
        str(tmp_path),
        run_id="REAL1",
        model_crs=d.model_crs,
        deck_dir=str(tmp_path),
        species_names=["TCE", "cis-DCE"],
    )
    assert isinstance(result, MultiSpeciesPlumeResult)
    assert len(result.plumes) == 2
    tce = next(p for p in result.plumes if "TCE" in p.name)
    # The sourced parent shows a real plume from the shared flow field.
    assert tce.max_concentration_mgl > 0.0


# --------------------------------------------------------------------------- #
# Composer full chain (fake run-tool DI)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_composer_requires_species(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_tool("geocode_location", _fake_geocode, monkeypatch)
    with pytest.raises(MultiSpeciesInputError):
        await model_multi_species_scenario(
            spill_location_latlon=(26.64, -81.87), species=None
        )


@pytest.mark.asyncio
async def test_composer_requires_exactly_one_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both location AND an explicit point is rejected (and neither too)."""
    _install_fake_tool("geocode_location", _fake_geocode, monkeypatch)
    with pytest.raises(MultiSpeciesInputError, match="exactly one"):
        await model_multi_species_scenario(
            location="Fort Myers",
            spill_location_latlon=(26.64, -81.87),
            species=TWO_SPECIES,
        )


@pytest.mark.asyncio
async def test_composer_full_chain_two_plumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fake run tool returning two plumes -> MultiSpeciesResult with both layers
    + a summary carrying each species' typed scalars (Invariant 1)."""
    _install_fake_tool("geocode_location", _fake_geocode, monkeypatch)

    captured: dict[str, Any] = {}

    async def _fake_run(run_args, **_kw):  # noqa: ANN001
        captured["run_args"] = run_args
        return MultiSpeciesPlumeResult(
            plumes=[
                _fake_plume("TCE", area=1.5, conc=12.0),
                _fake_plume("cis-DCE", area=0.4, conc=2.0),
            ]
        )

    import trid3nt_server.tools.run_modflow_multi_species_tool as tool

    monkeypatch.setattr(tool, "run_modflow_multi_species_job", _fake_run)

    result = await model_multi_species_scenario(
        spill_location_latlon=(26.64, -81.87), species=TWO_SPECIES
    )
    assert isinstance(result, MultiSpeciesResult)
    assert len(result.plume_layers) == 2
    # The run args threaded archetype + species into the engine.
    assert captured["run_args"].archetype == "multi_species"
    assert [s.name for s in captured["run_args"].species] == ["TCE", "cis-DCE"]
    # Summary carries per-species typed scalars (no free-generated numbers).
    sp = {s["name"]: s for s in result.summary["species"]}
    assert sp["TCE"]["max_concentration_mgl"] == pytest.approx(12.0)
    assert sp["cis-DCE"]["plume_area_km2"] == pytest.approx(0.4)
    assert result.summary["n_species"] == 2


@pytest.mark.asyncio
async def test_composer_surfaces_run_error_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed run dict from the tool re-raises as a typed scenario error (the
    honesty floor: a failed run never reads as a successful layer set)."""
    _install_fake_tool("geocode_location", _fake_geocode, monkeypatch)

    async def _err_run(run_args, **_kw):  # noqa: ANN001
        return {
            "status": "error",
            "error_code": "MODFLOW_MULTISPECIES_EMPTY_RESULT",
            "error_message": "no non-trivial plume for any species",
        }

    import trid3nt_server.tools.run_modflow_multi_species_tool as tool

    monkeypatch.setattr(tool, "run_modflow_multi_species_job", _err_run)

    with pytest.raises(MultiSpeciesScenarioError, match="EMPTY_RESULT"):
        await model_multi_species_scenario(
            spill_location_latlon=(26.64, -81.87), species=TWO_SPECIES
        )


@pytest.mark.asyncio
async def test_wrapper_missing_species_returns_user_input_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LLM-facing wrapper maps a missing species list to USER_INPUT_REQUIRED
    (never fabricates a contaminant)."""
    _install_fake_tool("geocode_location", _fake_geocode, monkeypatch)
    out = await ms.run_model_multi_species_scenario(
        spill_location_latlon=[26.64, -81.87], species=None
    )
    assert out["status"] == "error"
    assert out["error_code"] == "USER_INPUT_REQUIRED"


# --------------------------------------------------------------------------- #
# Registration + category
# --------------------------------------------------------------------------- #


def test_composer_registered_uncacheable() -> None:
    import trid3nt_server.tools  # noqa: F401 - fire registration

    entry = TOOL_REGISTRY.get("run_model_multi_species_scenario")
    assert entry is not None
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.source_class == "workflow_dispatch"


def test_composer_in_hazard_modeling_category() -> None:
    from trid3nt_server.categories import tools_for_category

    assert "run_model_multi_species_scenario" in tools_for_category("hazard_modeling")
