"""OpenQuake epistemic logic-tree + multi-PoE/UHS fold (board rows folded into
``openquake_psha`` knobs).

Pure (no engine, no I/O) coverage of the deck-rendering + build-spec + chart
wiring for the three folded capabilities:

  - ``logic_tree="single"``      -> byte-identical classical single-branch deck.
  - ``logic_tree="source_models"`` -> competing weighted source models + 2 GMPEs
                                    + 5/50/95 quantile output (GEM LogicTreeCase1).
  - ``logic_tree="gr_uncertainty"`` -> a/b + Mmax epistemic branches on a
                                    two-source model + 2 GMPEs per TRT
                                    (GEM LogicTreeCase2, 324-realization mechanism).
  - ``secondary_poe`` / ``uniform_hazard_spectra`` -> multi-return-period + UHS.

The engine-run validation (that each deck runs on ``oq`` and exports the
mean/quantile/UHS CSVs) lives in the live smoke; here we lock the deck shape.
"""

from __future__ import annotations

from trid3nt_contracts.openquake_contracts import OpenQuakeRunArgs
from trid3nt_server.workflows.openquake.psha.psha import assemble_build_spec
from trid3nt_server.tools.processing.charts_common import (
    build_hazard_quantile_band_chart,
)
from workers.openquake.job_ini import render_openquake_deck

BBOX = (-122.30, 37.70, -122.10, 37.90)
_BASE = {"bbox": list(BBOX), "imt": "PGA", "poe": 0.1, "site_grid_spacing_km": 20.0}


def test_single_mode_deck_is_classical_byte_identical() -> None:
    deck = render_openquake_deck(dict(_BASE))
    # single branch trees, no quantiles, single poe, no extra files.
    assert deck.extra_files == {}
    assert "quantiles =\n" in deck.job_ini
    assert "individual_rlzs" not in deck.job_ini
    assert "poes = 0.1\n" in deck.job_ini
    assert deck.source_model_logic_tree_xml.count("<logicTreeBranch branchID=") == 1


def test_source_models_mode_competing_models_and_quantiles() -> None:
    deck = render_openquake_deck({**_BASE, "logic_tree": "source_models"})
    # two competing source-model files + the competing-source logic tree.
    assert set(deck.extra_files) == {"source_model_1.xml", "source_model_2.xml"}
    assert deck.source_model_logic_tree_xml.count('uncertaintyType="sourceModel"') == 1
    assert deck.source_model_logic_tree_xml.count("<logicTreeBranch branchID=") == 2
    # 2 competing GMPEs on active shallow crust.
    assert "BooreAtkinson2008" in deck.gmpe_logic_tree_xml
    assert "ChiouYoungs2008" in deck.gmpe_logic_tree_xml
    # 5/50/95 quantile spread requested + individual realizations.
    assert "quantiles = 0.05 0.5 0.95" in deck.job_ini
    assert "individual_rlzs = true" in deck.job_ini


def test_gr_uncertainty_mode_ab_and_mmax_branches_two_sources() -> None:
    deck = render_openquake_deck({**_BASE, "logic_tree": "gr_uncertainty"})
    smlt = deck.source_model_logic_tree_xml
    assert 'uncertaintyType="abGRAbsolute"' in smlt
    assert 'uncertaintyType="maxMagGRAbsolute"' in smlt
    assert 'applyToSources="first"' in smlt and 'applyToSources="second"' in smlt
    # two-source model across two tectonic regions.
    assert deck.source_model_xml.count("<areaSource") == 2
    assert "Stable Continental Crust" in deck.source_model_xml
    # GMPE tree carries both TRTs (-> the 4-GMPE-combination x 81 SM = 324 tree).
    assert deck.gmpe_logic_tree_xml.count('uncertaintyType="gmpeModel"') == 2
    assert "quantiles = 0.05 0.5 0.95" in deck.job_ini


def test_assemble_build_spec_threads_knobs() -> None:
    sm = assemble_build_spec(OpenQuakeRunArgs(bbox=BBOX, logic_tree="source_models"))
    assert sm["logic_tree"] == "source_models"
    row3 = assemble_build_spec(
        OpenQuakeRunArgs(bbox=BBOX, secondary_poe=0.02, uniform_hazard_spectra=True)
    )
    assert row3["poes"] == [0.1, 0.02]
    assert row3["uniform_hazard_spectra"] is True
    # single mode leaves the classical spec free of the epistemic keys.
    single = assemble_build_spec(OpenQuakeRunArgs(bbox=BBOX))
    assert "logic_tree" not in single and "poes" not in single


def test_multipoe_and_uhs_deck() -> None:
    deck = render_openquake_deck(
        {**_BASE, "poes": [0.1, 0.02], "uniform_hazard_spectra": True}
    )
    assert "poes = 0.1 0.02" in deck.job_ini
    assert "uniform_hazard_spectra = true" in deck.job_ini
    assert 'SA(1.0)' in deck.job_ini  # the UHS SA-period ladder injected


def test_quantile_band_chart_is_four_line_series() -> None:
    imls = [0.05, 0.1, 0.2, 0.4]
    chart = build_hazard_quantile_band_chart(
        imls_g=imls,
        mean_poe=[0.9, 0.6, 0.3, 0.1],
        q05_poe=[0.8, 0.5, 0.2, 0.05],
        q50_poe=[0.88, 0.58, 0.28, 0.09],
        q95_poe=[0.95, 0.7, 0.4, 0.15],
        imt="PGA",
        investigation_time_years=50.0,
        n_realizations=324,
        logic_tree_label="gr_uncertainty",
    )
    assert chart is not None
    spec = chart["vega_lite_spec"]
    # a single-view line spec grouped by a color field (the dock renders it).
    assert spec["mark"]["type"] == "line"
    assert spec["encoding"]["color"]["field"] == "series"
    series = {r["series"] for r in spec["data"]["values"]}
    assert series == {"q05", "q50 (median)", "mean", "q95"}
