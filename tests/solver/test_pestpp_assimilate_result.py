"""The sequential DA reader: forecast and analysis per cycle, forecast-only cycles, dropped members."""

import csv
import shutil
from pathlib import Path

import pytest

from trid3nt.workflows.pestpp.assimilation import read_result
from trid3nt.workflows.pestpp.errors import PestppError

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pestpp" / "assimilate"


def _bound(weights=None):
    weights = weights or {"0": 1.0, "1": 2.0, "2": 0.0}
    return {
        "cycles": [0, 1, 2],
        "parameters": [
            {"name": "k", "cycle": -1, "transform": "log"},
            {"name": "head_a", "cycle": -1, "transform": "fixed"},
        ],
        "observations": [
            {"name": "obs_a", "cycle": -1, "value": 1.0, "weight": 1.0, "group": "heads",
             "source": "stated", "values": {"0": 1.0, "1": 1.5}, "weights": weights},
            {"name": "obs_b", "cycle": -1, "value": 2.0, "weight": 1.0, "group": "heads",
             "source": "stated", "values": {"0": 2.0}, "weights": {"0": 1.0, "1": 0.0, "2": 0.0}},
            {"name": "head_a", "cycle": -1, "value": 2.0, "weight": 0.0, "group": "state",
             "source": "stated", "weights": {"0": 0.0, "1": 0.0, "2": 0.0}},
        ],
        "state": [{"parameter": "head_a", "observation": "head_a"}],
    }


def _copy(tmp_path):
    target = tmp_path / "complete"
    shutil.copytree(_FIXTURES / "complete", target)
    return target


def _rows(path: Path):
    with path.open(newline="") as file:
        return list(csv.DictReader(file))


def _write(path: Path, rows):
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def test_each_cycle_holds_its_forecast_and_analysis_ensembles(tmp_path):
    result = read_result(_copy(tmp_path), run_id="one", manifest_uri="cache", bound=_bound())
    assert [row["cycle"] for row in result.cycles] == [0, 1, 2]
    first = result.cycles[0]
    assert first["forecast_iteration"] == 0 and first["analysis_iteration"] == 2
    assert first["forecast_only"] is False and first["members"] == ["real_0", "real_1"]
    obs = next(row for row in result.observations if row["name"] == "obs_a" and row["cycle"] == 0)
    assert obs["forecast"]["mean"] == pytest.approx(1.1)
    assert obs["analysis"]["mean"] == pytest.approx(0.85)
    assert obs["forecast"]["values"] == {"real_0": 1.0, "real_1": 1.2}
    assert obs["analysis"]["values"] == {"real_0": 0.8, "real_1": 0.9}
    assert obs["weight"] == 1.0 and obs["observed"] == 1.0


def test_a_cycle_with_no_active_weight_is_forecast_only(tmp_path):
    result = read_result(_copy(tmp_path), run_id="one", manifest_uri="cache", bound=_bound())
    only = result.cycles[2]
    assert only["forecast_only"] is True and only["analysis_iteration"] == 0
    assert result.observations == [row for row in result.observations if row["cycle"] != 2]


def test_the_phi_trace_keeps_every_cycle_and_iteration(tmp_path):
    result = read_result(_copy(tmp_path), run_id="one", manifest_uri="cache", bound=_bound())
    assert [(row["cycle"], row["iteration"]) for row in result.phi_trace] == [
        (0, 0), (0, 2), (1, 0), (1, 1), (2, 0)]
    assert result.cycles[0]["forecast_phi"] == pytest.approx(10.0)
    assert result.cycles[0]["analysis_phi"] == pytest.approx(4.0)


def test_the_parameter_evolution_is_summarised_by_cycle(tmp_path):
    result = read_result(_copy(tmp_path), run_id="one", manifest_uri="cache", bound=_bound())
    k = next(row for row in result.parameters if row["name"] == "k" and row["cycle"] == 0)
    assert k["forecast"]["mean"] == pytest.approx(1.05)
    assert k["analysis"]["mean"] == pytest.approx(0.95)
    assert k["forecast"]["spread"] == pytest.approx(0.05)


def test_the_state_transfer_pairs_analysis_with_the_next_initial_state(tmp_path):
    result = read_result(_copy(tmp_path), run_id="one", manifest_uri="cache", bound=_bound())
    first = next(row for row in result.state if row["cycle"] == 0)
    assert first["parameter"] == "head_a" and first["observation"] == "head_a"
    assert first["analysis"] == pytest.approx(1.95)
    assert first["next_initial"] == pytest.approx(1.95)


def test_a_missing_cycle_output_refuses_by_name(tmp_path):
    directory = _copy(tmp_path)
    (directory / "pestpp.1.0.obs.csv").unlink()
    with pytest.raises(PestppError) as missing:
        read_result(directory, run_id="one", manifest_uri="cache", bound=_bound())
    assert missing.value.error_code == "PESTPP_DA_OUTPUT_MISSING"
    assert "pestpp.1.0.obs.csv" in str(missing.value)


def test_a_dropped_member_refuses(tmp_path):
    directory = _copy(tmp_path)
    path = directory / "pestpp.0.2.obs.csv"
    _write(path, _rows(path)[:1])
    with pytest.raises(PestppError) as dropped:
        read_result(directory, run_id="one", manifest_uri="cache", bound=_bound())
    assert dropped.value.error_code == "PESTPP_DA_MEMBER_DROPPED"


def test_a_non_finite_member_refuses(tmp_path):
    directory = _copy(tmp_path)
    path = directory / "pestpp.0.2.obs.csv"
    rows = _rows(path)
    rows[0]["obs_a"] = "nan"
    _write(path, rows)
    with pytest.raises(PestppError) as refused:
        read_result(directory, run_id="one", manifest_uri="cache", bound=_bound())
    assert refused.value.error_code == "PESTPP_DA_NONFINITE"
