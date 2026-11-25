"""The Pareto reader: feasibility, front and direction from the pinned archive files."""

import csv
import shutil
from pathlib import Path

import pytest

from trid3nt.workflows.pestpp.errors import PestppError
from trid3nt.workflows.pestpp.tradeoff import read_result

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pestpp" / "tradeoff"


def _bound(senses=("min", "min")):
    return {
        "decisions": [{"name": "pump", "lower": -5.0, "upper": 0.0}],
        "objectives": [{"name": "obj_1", "sense": senses[0]},
                       {"name": "obj_2", "sense": senses[1]}],
        "constraints": [{"name": "stream", "sense": "<=", "limit": -1.0}],
    }


def _copy(tmp_path, name="archive"):
    target = tmp_path / name
    shutil.copytree(_FIXTURES / name, target)
    return target


def _rows(path: Path):
    with path.open(newline="") as file:
        return list(csv.DictReader(file))


def _write(path: Path, rows):
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def test_the_archive_yields_every_candidate_with_decisions_objectives_and_slack(tmp_path):
    result = read_result(_copy(tmp_path), run_id="one", manifest_uri="cache", bound=_bound())
    assert result.outcome == "feasible"
    assert [row["id"] for row in result.candidates] == ["r1", "r2"]
    first, second = result.candidates
    assert first["decisions"] == {"pump": -2.0} and second["decisions"] == {"pump": -3.0}
    assert first["objectives"] == {"obj_1": 1.1, "obj_2": 3.9}
    assert first["feasible"] is True and first["rank"] == 1 and first["front"] == 1
    assert first["constraints"][0]["name"] == "stream" and first["constraints"][0]["slack"] == pytest.approx(0.5)
    assert result.objectives[0]["sense"] == "min" and result.objectives[1]["sense"] == "min"


def test_an_empty_feasible_front_is_a_typed_outcome(tmp_path):
    directory = _copy(tmp_path)
    path = directory / "pestpp.pareto.archive.summary.csv"
    rows = _rows(path)
    for row in rows:
        row["is_feasible"] = "0"
    _write(path, rows)
    result = read_result(directory, run_id="one", manifest_uri="cache", bound=_bound())
    assert result.outcome == "empty" and result.candidates == []


def test_a_duplicate_or_missing_member_refuses(tmp_path):
    directory = _copy(tmp_path)
    path = directory / "pestpp.pareto.archive.summary.csv"
    rows = _rows(path)
    _write(path, rows + [{**rows[-1]}])
    with pytest.raises(PestppError) as duplicated:
        read_result(directory, run_id="one", manifest_uri="cache", bound=_bound())
    assert duplicated.value.error_code == "PESTPP_MOU_ARCHIVE"

    directory = _copy(tmp_path / "missing")
    population = directory / "pestpp.archive.dv_pop.csv"
    kept = [row for row in _rows(population) if row["real_name"] != "r2"]
    _write(population, kept)
    with pytest.raises(PestppError) as missing:
        read_result(directory, run_id="one", manifest_uri="cache", bound=_bound())
    assert missing.value.error_code == "PESTPP_MOU_ARCHIVE"


def test_a_dominated_feasible_member_refuses_in_the_stated_directions(tmp_path):
    directory = _copy(tmp_path / "dominated")
    path = directory / "pestpp.pareto.archive.summary.csv"
    rows = _rows(path)
    _write(path, rows + [{**rows[-1], "member": "r3", "obj_1": "9.0", "obj_2": "9.0"}])
    population = directory / "pestpp.archive.dv_pop.csv"
    _write(population, _rows(population) + [{"real_name": "r3", "pump": "-4.0"}])
    observed = directory / "pestpp.archive.obs_pop.csv"
    _write(observed, _rows(observed) + [{"real_name": "r3", "obj_1": "9.0", "obj_2": "9.0", "stream": "-1.0"}])
    with pytest.raises(PestppError) as dominated:
        read_result(directory, run_id="one", manifest_uri="cache", bound=_bound())
    assert dominated.value.error_code == "PESTPP_MOU_DOMINATED"


def test_nondominance_reads_the_stated_sense_per_objective(tmp_path):
    directory = _copy(tmp_path / "direction", name="direction")
    result = read_result(directory, run_id="one", manifest_uri="cache", bound=_bound(("min", "max")))
    assert [row["id"] for row in result.candidates] == ["r1", "r2"]
    with pytest.raises(PestppError) as dominated:
        read_result(directory, run_id="one", manifest_uri="cache", bound=_bound(("min", "min")))
    assert dominated.value.error_code == "PESTPP_MOU_DOMINATED"
