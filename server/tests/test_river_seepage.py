"""Tests for the MODFLOW river-seepage extension (sprint-17 J9).

Coverage:
  * ``compute_seepage_metrics`` signed-budget math on synthetic grids (pure,
    always runs).
  * ``postprocess_river_seepage`` end-to-end against a REAL mf6-generated GWF
    cbc RIV budget (gated on an mf6 binary + flopy): the cbc reader scatters the
    RIV leakage onto the grid and the diverging COG carries BOTH gaining
    (negative) and losing (positive) reach cells. Skips cleanly otherwise.
  * The ``model_river_seepage_scenario`` composer arg-assembly with EVERY
    registry tool MOCKED (geocode / fetch_river_geometry / run_river_seepage_job)
    so no run_solver / boto3 / network is touched — the composer only assembles
    args + threads typed results.

No LLM calls anywhere; the live cbc path shells out to mf6 directly.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from trid3nt_contracts.modflow_contracts import PlumeLayerURI, SeepageLayerURI

from trid3nt_server.workflows import postprocess_modflow as pp


# --------------------------------------------------------------------------- #
# mf6 binary + flopy discovery (for the live cbc test)
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _find_mf6() -> str | None:
    env = os.environ.get("TRID3NT_MF6_BIN")
    if env and Path(env).exists():
        return env
    on_path = shutil.which("mf6")
    if on_path:
        return on_path
    for cand in (
        Path.home() / "AGRI-SENTINEL" / ".modflow" / "bin" / "mf6",
        Path.home() / ".local" / "bin" / "mf6",
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    for cand in _REPO_ROOT.rglob("mf6.5.0_linux/bin/mf6"):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


_MF6_BIN = _find_mf6()
_HAVE_FLOPY = True
try:
    import flopy  # type: ignore[import-not-found]  # noqa: F401
except Exception:  # noqa: BLE001
    _HAVE_FLOPY = False


# --------------------------------------------------------------------------- #
# Pure seepage-metric math (always runs)
# --------------------------------------------------------------------------- #


def test_compute_seepage_metrics_signs_and_magnitudes() -> None:
    import numpy as np

    nan = float("nan")
    # A 2x3 grid: one gaining (negative), one losing (positive), rest NaN.
    grid = np.array([[nan, 5.0, nan], [-3.0, nan, 2.0]], dtype="float64")
    total, gaining, losing, cells = pp.compute_seepage_metrics(grid)
    # total = 5 - 3 + 2 = 4 (net losing/recharging)
    assert total == pytest.approx(4.0)
    # gaining magnitude = |-3| = 3 ; losing magnitude = 5 + 2 = 7
    assert gaining == pytest.approx(3.0)
    assert losing == pytest.approx(7.0)
    assert cells == 3


def test_compute_seepage_metrics_all_nan_is_zero() -> None:
    import numpy as np

    grid = np.full((4, 4), np.nan, dtype="float64")
    assert pp.compute_seepage_metrics(grid) == (0.0, 0.0, 0.0, 0)


def test_compute_seepage_metrics_empty() -> None:
    import numpy as np

    assert pp.compute_seepage_metrics(np.array([])) == (0.0, 0.0, 0.0, 0)


def test_compute_seepage_metrics_pure_gaining() -> None:
    import numpy as np

    nan = float("nan")
    grid = np.array([[-2.0, -4.0], [nan, -1.0]], dtype="float64")
    total, gaining, losing, cells = pp.compute_seepage_metrics(grid)
    assert total == pytest.approx(-7.0)  # net gaining (out of aquifer)
    assert gaining == pytest.approx(7.0)
    assert losing == pytest.approx(0.0)
    assert cells == 3


# --------------------------------------------------------------------------- #
# Live cbc -> diverging seepage COG (gated on mf6 + flopy)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    _MF6_BIN is None or not _HAVE_FLOPY,
    reason="no mf6 binary / flopy — live RIV cbc postprocess needs both",
)
def test_postprocess_river_seepage_from_real_cbc(tmp_path, monkeypatch) -> None:
    """Build a real RIV deck, run mf6, and prove the seepage postprocess reads
    the GWF cbc RIV budget into a diverging COG carrying both gaining + losing
    reach cells (publish off, gs/file fallback)."""
    import flopy  # type: ignore[import-not-found]
    import numpy as np
    import rasterio

    # Use the production re-export seam (handles the worker-dir sys.path).
    from trid3nt_server.workflows.run_modflow import build_modflow_deck

    lat0, lon0 = 26.64, -81.87
    poly = [(-81.878, lat0), (-81.872, lat0), (-81.866, lat0), (-81.862, lat0)]
    deck = build_modflow_deck(
        spill_location_latlon=(lat0, lon0),
        contaminant="TCE",
        release_rate_kg_s=0.01,
        duration_days=30,
        aquifer_k_ms=1e-4,
        porosity=0.3,
        workdir=tmp_path,
        river_polyline_lonlat=poly,
        along_river_source=True,
    )
    assert deck.river_coupled and deck.river_cell_count > 0

    sim = flopy.mf6.MFSimulation.load(
        sim_ws=str(tmp_path), exe_name=_MF6_BIN, verbosity_level=0
    )
    ok, buf = sim.run_simulation(silent=True)
    assert ok, "".join(buf[-15:])
    assert "Normal termination of simulation" in (tmp_path / "mfsim.lst").read_text()

    # Force the file:// upload fallback (no runs bucket) so the test is offline.
    monkeypatch.setattr(pp, "_dispatch_publish_layer", lambda *a, **k: None)

    def _file_upload(local_cog, run_id, runs_bucket, *, cog_filename="x.tif"):
        return f"file://{local_cog}"

    monkeypatch.setattr(pp, "_upload_cog", _file_upload)

    seepage = pp.postprocess_river_seepage(
        str(tmp_path),
        run_id="TESTRUN",
        model_crs=deck.model_crs,
        deck_dir=str(tmp_path),
        publish=False,
    )
    assert isinstance(seepage, SeepageLayerURI)
    assert seepage.layer_type == "raster"
    assert seepage.style_preset == "diverging_river_seepage"
    assert seepage.units == "m^3/day"
    assert seepage.river_cell_count == deck.river_cell_count
    # A real river exchanges water with the aquifer.
    assert (seepage.gaining_m3_day + seepage.losing_m3_day) > 1.0
    assert seepage.bbox is not None

    # The diverging COG must carry BOTH signs (gaining negative + losing positive).
    cog_path = seepage.uri.replace("file://", "")
    with rasterio.open(cog_path) as ds:
        assert str(ds.crs) == "EPSG:4326"
        arr = ds.read(1)
        finite = arr[np.isfinite(arr)]
        assert finite.size > 0
        assert finite.min() < 0.0, "expected gaining (negative) reach cells"
        assert finite.max() > 0.0, "expected losing (positive) reach cells"


# --------------------------------------------------------------------------- #
# Composer arg-assembly (registry tools MOCKED — no solver/network)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_composer_assembles_args_and_threads_result(monkeypatch) -> None:
    """``model_river_seepage_scenario`` geocodes -> fetches river -> calls the
    river-seepage tool with the assembled args, and threads the typed seepage
    result into the RiverSeepageResult — every tool mocked (no run_solver)."""
    from trid3nt_server.workflows import model_river_seepage_scenario as mod
    from trid3nt_contracts.execution import LayerURI

    calls: dict[str, Any] = {}

    def _fake_geocode(location):
        calls["geocode"] = location
        return {"latitude": 26.64, "longitude": -81.87}

    def _fake_fetch_river(*, bbox):
        calls["fetch_river_bbox"] = bbox
        return LayerURI(
            layer_id="river-1",
            name="River",
            layer_type="vector",
            uri="s3://bucket/river.fgb",
            style_preset="river_lines",
            role="input",
        )

    async def _fake_run(**kwargs):
        calls["run_kwargs"] = kwargs
        return SeepageLayerURI(
            layer_id="river-seepage-RUN1",
            name="River Seepage",
            layer_type="raster",
            uri="s3://bucket/RUN1/river_seepage_4326.tif",
            style_preset="diverging_river_seepage",
            role="primary",
            units="m^3/day",
            total_leakage_m3_day=-2.5,
            gaining_m3_day=418.97,
            losing_m3_day=418.97,
            river_cell_count=32,
        )

    fake_registry = {
        "geocode_location": type("E", (), {"fn": staticmethod(_fake_geocode)}),
        "fetch_river_geometry": type("E", (), {"fn": staticmethod(_fake_fetch_river)}),
        "run_river_seepage_job": type("E", (), {"fn": staticmethod(_fake_run)}),
    }
    monkeypatch.setattr(mod, "TOOL_REGISTRY", fake_registry)

    result = await mod.model_river_seepage_scenario(
        location="Fort Myers, FL",
        contaminant="TCE",
        release_rate_kg_s=0.02,
        duration_days=45.0,
    )

    # Geocode + river fetch happened with a bbox built around the geocoded point.
    assert calls["geocode"] == "Fort Myers, FL"
    bbox = calls["fetch_river_bbox"]
    assert bbox[0] < -81.87 < bbox[2] and bbox[1] < 26.64 < bbox[3]

    # The river-seepage tool was called with the assembled args (river uri +
    # along-river source + the forcing fields), NOT a run_solver call.
    rk = calls["run_kwargs"]
    assert rk["river_geometry_uri"] == "s3://bucket/river.fgb"
    assert rk["spill_location_latlon"] == (26.64, -81.87)
    assert rk["contaminant"] == "TCE"
    assert rk["release_rate_kg_s"] == 0.02
    assert rk["duration_days"] == 45.0
    assert rk["along_river_source"] is True

    # The typed seepage layer is threaded through into the result + summary.
    assert isinstance(result.seepage_layer, SeepageLayerURI)
    assert result.seepage_layer.river_cell_count == 32
    assert result.summary["gaining_m3_day"] == pytest.approx(418.97)
    assert result.summary["losing_m3_day"] == pytest.approx(418.97)
    assert result.summary["location_name"] == "Fort Myers, FL"
    assert result.derived_params["river_geometry_uri"] == "s3://bucket/river.fgb"


@pytest.mark.asyncio
async def test_composer_errors_when_no_river_found(monkeypatch) -> None:
    """A fetch_river_geometry result without a URI -> a typed scenario error
    (no silent fall-through to a riverless run)."""
    from trid3nt_server.workflows import model_river_seepage_scenario as mod

    def _fake_geocode(location):
        return {"latitude": 26.64, "longitude": -81.87}

    def _fake_fetch_river(*, bbox):
        return {"status": "error", "error_message": "no river"}

    fake_registry = {
        "geocode_location": type("E", (), {"fn": staticmethod(_fake_geocode)}),
        "fetch_river_geometry": type("E", (), {"fn": staticmethod(_fake_fetch_river)}),
    }
    monkeypatch.setattr(mod, "TOOL_REGISTRY", fake_registry)

    with pytest.raises(mod.RiverSeepageScenarioError):
        await mod.model_river_seepage_scenario(location="Nowhere, NV")


@pytest.mark.asyncio
async def test_composer_requires_exactly_one_location_form() -> None:
    from trid3nt_server.workflows import model_river_seepage_scenario as mod

    with pytest.raises(mod.RiverSeepageScenarioInputError):
        await mod.model_river_seepage_scenario()  # neither
    with pytest.raises(mod.RiverSeepageScenarioInputError):
        await mod.model_river_seepage_scenario(
            location="X", spill_location_latlon=(1.0, 2.0)
        )  # both
