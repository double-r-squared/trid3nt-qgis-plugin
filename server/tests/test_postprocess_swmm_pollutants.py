"""SWMM water-quality POSTPROCESS tests (sprint-WQ).

Runs ``postprocess_swmm_pollutants`` against the PINNED Phase-1 in-image smoke
fixture (``fixtures/swmm_wq/wq_smoke.{out,rpt}`` — a 2-subcatchment TSS + E.coli
buildup/washoff run) and asserts the typed WQ readout the agent narrates:

  - the cumulative OUTFALL LOAD parsed from the ``.rpt`` Outfall Loading Summary
    (TSS in kg; E.coli's ``#/L`` count reported as LOG10 and converted to a raw
    count — NEVER mislabeled as kg);
  - the outfall POLLUTOGRAPH series (concentration vs minutes, native units) with
    a first-flush peak;
  - the supply-limited ``washoff_mass_fraction`` in (0, 1] (washed <= built);
  - the Quality Routing Continuity error carried through as the WQ mass-balance
    readout.

The concentration-COG upload is patched (offline — no runs bucket), exactly like
``test_postprocess_swmm``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("swmm_api")
pytest.importorskip("pyswmm")
rasterio = pytest.importorskip("rasterio")

from grace2_agent.workflows.postprocess_swmm import (  # noqa: E402
    postprocess_swmm_pollutants,
    read_outfall_loading,
    read_runoff_quality_built_washed,
)

_FIX = Path(__file__).parent / "fixtures" / "swmm_wq"


def _fake_upload(local_cog, run_id, runs_bucket=None, *, dest_filename="c.tif"):  # noqa: ANN001
    return f"gs://test-runs/{run_id}/{dest_filename}"


def _stub_run_build():
    from rasterio.transform import from_origin

    run = SimpleNamespace(
        out_path=str(_FIX / "wq_smoke.out"),
        rpt_path=str(_FIX / "wq_smoke.rpt"),
    )
    # The smoke deck is 2 cells (S_0_0, S_0_1) => a (1, 2) grid.
    build = SimpleNamespace(
        grid_shape=(1, 2),
        crs="EPSG:32616",
        transform=list(from_origin(500000.0, 4600000.0, 10.0, 10.0))[:6],
        pollutants=[("TSS", "MG/L"), ("E_coli", "#/L")],
    )
    return run, build


def test_outfall_loading_parse():
    """The .rpt Outfall Loading Summary yields the RAW per-pollutant columns."""
    loads = read_outfall_loading(str(_FIX / "wq_smoke.rpt"), 2)
    assert loads is not None and len(loads) == 2
    # TSS load ~0.004 kg; E.coli reported as LogN ~7.64 (raw column value).
    assert abs(loads[0] - 0.004) < 1e-3
    assert 7.0 < loads[1] < 8.0


def test_runoff_quality_built_washed_count_conversion():
    """A count pollutant's built/washed are converted from LOG10 to raw counts."""
    bw = read_runoff_quality_built_washed(str(_FIX / "wq_smoke.rpt"), 1, is_count=True)
    assert bw is not None
    built, washed = bw
    # LogN Surface Runoff 9.000 -> 10^9 washed; built >= washed (supply-limited).
    assert washed == pytest.approx(1e9, rel=1e-3)
    assert built >= washed


def test_postprocess_pollutants_metrics_and_layers():
    run, build = _stub_run_build()
    with patch(
        "grace2_agent.workflows.postprocess_swmm._upload_cog_to_runs_bucket",
        side_effect=_fake_upload,
    ):
        layers, series, metrics = postprocess_swmm_pollutants(
            run, build, run_id="smoke", runs_bucket=None
        )

    # --- metrics ---
    assert set(metrics) == {"TSS", "E_coli"}
    tss = metrics["TSS"]
    assert tss["outfall_load_units"] == "kg"
    assert tss["outfall_load"] == pytest.approx(0.004, abs=1e-3)
    assert tss["peak_outfall_conc"] > 1.0  # mg/L first-flush crest
    assert 0.0 < tss["washoff_mass_fraction"] <= 1.0
    assert tss["wq_continuity_error_pct"] is not None
    assert tss["pollutant_units"] == "mg/L"

    eco = metrics["E_coli"]
    assert eco["outfall_load_units"] == "counts"  # NOT kg — a count, honestly
    # 10^7.64 ~ 4.4e7 organisms (never a raw 7.64 kg mislabel).
    assert eco["outfall_load"] == pytest.approx(10 ** 7.64, rel=0.05)
    assert eco["peak_outfall_conc"] > 100.0  # #/L
    assert eco["pollutant_units"] == "#/L"

    # --- pollutograph series (time-varying first flush) ---
    assert len(series["TSS"]) > 2
    xs = [m for m, _ in series["TSS"]]
    assert xs == sorted(xs)  # ascending minutes
    assert max(c for _, c in series["TSS"]) == pytest.approx(
        tss["peak_outfall_conc"], rel=1e-6
    )

    # --- layers: one SWMMPollutantLayerURI per pollutant, role=context ---
    from grace2_contracts.swmm_contracts import SWMMPollutantLayerURI

    assert len(layers) == 2
    by_name = {l.pollutant_name: l for l in layers}
    assert set(by_name) == {"TSS", "E_coli"}
    for lyr in layers:
        assert isinstance(lyr, SWMMPollutantLayerURI)
        assert lyr.role == "context"  # depth stays primary; WQ is additive
        assert lyr.style_preset == "continuous_concentration"
        assert lyr.uri.startswith("gs://test-runs/")
        assert lyr.outfall_load >= 0.0
    assert by_name["E_coli"].outfall_load_units == "counts"
    assert by_name["TSS"].outfall_load_units == "kg"


def test_no_pollutants_returns_empty():
    """A build with no authored pollutants yields empty layers/series/metrics."""
    run, build = _stub_run_build()
    build.pollutants = []
    layers, series, metrics = postprocess_swmm_pollutants(
        run, build, run_id="none", runs_bucket=None
    )
    assert layers == [] and series == {} and metrics == {}
