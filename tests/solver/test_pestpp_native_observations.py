"""Native MODFLOW OBS routing and time-ordered PEST CSV instructions."""

from pathlib import Path

import pytest

from trid3nt.workflows.modflow.authoring.observations import NativeObservation, author_native_observations
from trid3nt.workflows.modflow.errors import ModflowError
from trid3nt.workflows.pestpp.authoring.modflow import observe_csv


def _deck(root: Path):
    (root / "flow.nam").write_text(
        "BEGIN PACKAGES\n  WEL6 flow.wel Wells_A\n  SFR6 flow.sfr River\n  OBS6 original.obs\nEND PACKAGES\n")
    (root / "flow.wel").write_text("BEGIN OPTIONS\n  BOUNDNAMES\nEND OPTIONS\n")
    (root / "flow.sfr").write_text("BEGIN OPTIONS\n  OBS6 FILEIN old-stream.obs\nEND OPTIONS\n")
    original = "BEGIN CONTINUOUS FILEOUT existing.csv\n  previous head 1 2 3\nEND CONTINUOUS\n"
    (root / "original.obs").write_text(original)
    (root / "old-stream.obs").write_text(original)


def test_generic_observations_use_native_ids_and_preserve_existing_inputs(tmp_path):
    _deck(tmp_path)
    forecasts = [
        NativeObservation("h", "head", (1, 2, 3)),
        NativeObservation("face", "flow-ja-face", (1, 2, 3), second=(1, 2, 4)),
        NativeObservation("pumping", "wel", ("north well",), package="Wells_A"),
        NativeObservation("exchange", "sfr", ("HEADWATER",), package="River"),
        NativeObservation("reach", "upstream-flow", (1,), package="River"),
        NativeObservation("another_well", "wel", (1, 2, 3), package="flow.wel"),
    ]
    files = author_native_observations(tmp_path, "flow.nam", forecasts, stem="pest_forecasts")
    assert list(files.values()) == [("h", "face"), ("pumping", "another_well"), ("exchange", "reach")]
    assert "previous head 1 2 3" in (tmp_path / "original.obs").read_text()
    assert "face flow-ja-face 1 2 3 1 2 4" in (tmp_path / "original.obs").read_text()
    assert "exchange sfr HEADWATER" in (tmp_path / "old-stream.obs").read_text()
    assert "reach upstream-flow 1" in (tmp_path / "old-stream.obs").read_text()
    assert "OBS6 FILEIN pest_forecasts_2.obs" in (tmp_path / "flow.wel").read_text()
    assert "another_well wel 1 2 3" in (tmp_path / "pest_forecasts_2.obs").read_text()
    assert "pumping wel 'north well'" in (tmp_path / "pest_forecasts_2.obs").read_text()
    assert (tmp_path / "flow.nam").read_text().upper().count("OBS6") == 1


def test_missing_package_refuses_before_changing_deck(tmp_path):
    _deck(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ModflowError, match="no_well") as refused:
        author_native_observations(tmp_path, "flow.nam", [
            NativeObservation("h", "head", (1, 2, 3)),
            NativeObservation("q", "wel", (1, 2, 3), package="no_well")], stem="pest_forecasts")
    assert refused.value.error_code == "MODFLOW_OBSERVATION_UNPLACED"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_one_csv_writer_addresses_different_rows_and_sparse_columns(tmp_path):
    instruction, output = observe_csv(tmp_path, "forecast.csv", ("h", "q", "exchange"), {
        "initial_head": ("h", 1),
        "later_exchange": ("exchange", 4),
        "later_head": ("h", 4),
    })
    assert output == "forecast.csv"
    assert (tmp_path / instruction).read_text().splitlines() == [
        "pif ~", "l2 ~,~ !initial_head!", "l3 ~,~ !later_head! ~,~ ~,~ !later_exchange!",
    ]


def test_csv_writer_names_an_unplaced_output(tmp_path):
    with pytest.raises(ModflowError, match="missing") as refused:
        observe_csv(tmp_path, "forecast.csv", ("h",), {"constraint": ("missing", 1)})
    assert refused.value.error_code == "MODFLOW_OBSERVATION_UNPLACED"


def test_opt_deck_links_a_single_well_period_to_native_forecasts(tmp_path):
    from trid3nt.workflows.pestpp.authoring.optimize import author_optimize

    _deck(tmp_path)
    nam = tmp_path / "flow.nam"
    nam.write_text(nam.read_text().replace("BEGIN PACKAGES\n", "BEGIN PACKAGES\nDIS6 grid.dis\n"))
    (tmp_path / "mfsim.nam").write_text(
        "BEGIN TIMING\nTDIS6 clock.tdis\nEND TIMING\nBEGIN MODELS\nGWF6 flow.nam aquifer\nGWT6 solute.nam tracer\nEND MODELS\n")
    (tmp_path / "solute.nam").write_text("BEGIN PACKAGES\nEND PACKAGES\n")
    (tmp_path / "clock.tdis").write_text(
        "BEGIN PERIODDATA\n1 1 1\n2 2 1\n3 1 1\nEND PERIODDATA\n")
    (tmp_path / "flow.wel").write_text(
        "BEGIN OPTIONS\nBOUNDNAMES\nEND OPTIONS\n"
        "BEGIN PERIOD 1\n 1 2 3 -2 well\nEND PERIOD\n"
        "BEGIN PERIOD 2\n 1 2 3 -3 well\nEND PERIOD\n")
    selection = dict(period=2, step=2, units="m3/day", source="published model")
    control, bound = author_optimize(
        tmp_path, model="aquifer", iterations=3,
        decisions=[dict(name="pump", package="Wells_A", period=2, cellid=[1, 2, 3],
                        lower=-5.0, upper=0.0, source="published bounds")],
        objective=dict(selection, name="pump_flux", type="wel", id=[1, 2, 3], package="Wells_A", sense="min"),
        constraints=[dict(selection, name="exchange", type="sfr", id=["HEADWATER"], package="River",
                          sense="<=", limit=-1.0),
                     dict(selection, name="solute", type="concentration", id=[1, 2, 3], model="tracer",
                          units="kg/m3", sense="<=", limit=0.4)])
    assert "opt_obj_func pump_flux" in control.read_text()
    assert "l4 ~,~ !exchange!" in (tmp_path / "opt_observations_2.csv.ins").read_text()
    assert "OBS6 opt_observations_model2_1.obs" in (tmp_path / "solute.nam").read_text()
    assert "solute concentration 1 2 3" in (tmp_path / "opt_observations_model2_1.obs").read_text()
    template = (tmp_path / "flow.wel.tpl").read_text()
    assert "1 2 3 ~pump" in template
    assert "BEGIN PERIOD 3\n 1 2 3 -3 well" in template
    assert bound["decisions"][0]["start"] == -3


def test_opt_result_reads_signed_constraints_and_preserves_infeasibility(tmp_path):
    from trid3nt.workflows.pestpp.optimization import read_result

    trace = tmp_path / "pestpp.slp.iobj.csv"
    trace.write_text("iteration,objective_function\n1,-4\n")
    (tmp_path / "pestpp.par").write_text("single point\npump -4 1 0\n")
    (tmp_path / "pestpp.res").write_text(
        "Model outputs\n Name Group Measured Modelled Residual Weight\n"
        " pump_flux objective 0 -4 4 0\n exchange less_than -2 -3 1 1\n")
    bound = {
        "decisions": [{"name": "pump", "lower": -5, "upper": 0}],
        "objective": {"name": "pump_flux", "sense": "min"},
        "constraints": [{"name": "exchange", "sense": "<=", "limit": -2}],
    }
    result = read_result(tmp_path, run_id="one", manifest_uri="cache", bound=bound, outcome="feasible")
    assert result.outcome == "feasible"
    assert result.objective["simulated"] == -4
    assert result.constraints[0]["slack"] == 1
    assert result.decisions[0]["value"] == -4
    (tmp_path / "pestpp.res").unlink()
    result = read_result(tmp_path, run_id="two", manifest_uri="cache", bound=bound, outcome="infeasible")
    assert result.outcome == "infeasible" and result.decisions == [] and result.constraints == []
