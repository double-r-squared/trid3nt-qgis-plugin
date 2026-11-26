"""The sequential DA authoring: ordered cycle windows, the three cycle tables, state linkage."""

import csv
from pathlib import Path

import pytest

from trid3nt.workflows.pestpp.authoring.assimilate import author_assimilate
from trid3nt.workflows.pestpp.errors import PestppError


def _deck(tmp_path: Path) -> Path:
    (tmp_path / "mfsim.nam").write_text(
        "BEGIN MODELS\n  GWF6 gwf.nam gwf\nEND MODELS\n"
        "BEGIN TIMING\n  TDIS6 gwf.tdis\nEND TIMING\n")
    (tmp_path / "gwf.nam").write_text(
        "BEGIN PACKAGES\n  DIS6 gwf.dis\n  IC6 gwf.ic\n  NPF6 gwf.npf\nEND PACKAGES\n")
    (tmp_path / "gwf.tdis").write_text(
        "BEGIN OPTIONS\nEND OPTIONS\nBEGIN DIMENSIONS\n  NPER 3\nEND DIMENSIONS\n"
        "BEGIN PERIODDATA\n  31 1 1.0\n  29 1 1.0\n  30 1 1.0\nEND PERIODDATA\n")
    for name in ("gwf.dis", "gwf.ic", "gwf.npf"):
        (tmp_path / name).write_text("")
    for name in ("k.dat.tpl", "heads.out.tpl", "wel.dat.tpl", "gwf.tdis.tpl"):
        (tmp_path / name).write_text("ptf ~\n")
    (tmp_path / "heads.ins").write_text("pif ~\n")
    for name in ("k.dat", "heads.out", "wel.dat"):
        (tmp_path / name).write_text("")
    return tmp_path


def _cycles():
    return [
        {"id": 0, "period": 1, "length": 31.0},
        {"id": 1, "period": 2, "length": 29.0},
        {"id": 2, "period": 3, "length": 30.0},
    ]


def _parameters():
    return [
        {"name": "k", "template": "k.dat.tpl", "model_file": "k.dat", "value": 1.0,
         "lower": 0.1, "upper": 10.0, "group": "npf_k", "transform": "log", "cycle": -1,
         "source": "stated multiplier bounds"},
        {"name": "wel_0", "template": "wel.dat.tpl", "model_file": "wel.dat", "value": -10.0,
         "lower": -50.0, "upper": 0.0, "group": "wel", "transform": "none", "cycle": 0,
         "source": "stated rate bounds"},
        {"name": "head_a", "template": "heads.out.tpl", "model_file": "heads.out", "value": 2.0,
         "lower": 1.9, "upper": 2.1, "group": "head_state", "transform": "fixed", "cycle": -1, "state": True,
         "source": "the deck's own initial head"},
        {"name": "perlen", "template": "gwf.tdis.tpl", "model_file": "gwf.tdis", "value": 31.0,
         "lower": 1.0, "upper": 366.0, "group": "clock", "transform": "fixed", "cycle": -1,
         "source": "the TDIS clock's stated period lengths"},
    ]


def _observations():
    return [
        {"name": "obs_a", "instruction": "heads.ins", "model_file": "heads.out", "group": "heads",
         "value": 1.0, "weight": 1.0, "cycle": -1, "source": "stated target",
         "values": {"0": 1.0, "1": 1.5}, "weights": {"0": 1.0, "1": 2.0, "2": 0.0}},
        {"name": "head_a", "instruction": "heads.ins", "model_file": "heads.out", "group": "state",
         "value": 2.0, "weight": 0.0, "cycle": -1, "source": "the deck's own initial head",
         "values": {"0": 2.0, "1": 1.9, "2": 1.8},
         "weights": {"0": 0.0, "1": 0.0, "2": 0.0}},
        {"name": "obs_b", "instruction": "heads.ins", "model_file": "heads.out", "group": "heads",
         "value": 2.0, "weight": 1.0, "cycle": -1, "source": "stated target"},
    ]


def _write(tmp_path: Path, **overrides):
    values = {"cycles": _cycles(), "parameters": _parameters(), "observations": _observations(),
              "options": {"noptmax": 1, "da_num_reals": 5, "ies_no_noise": True}}
    values.update(overrides)
    return author_assimilate(_deck(tmp_path), model="gwf", **values)


def _table(path: Path):
    with path.open(newline="") as file:
        return list(csv.reader(file))


def test_the_three_cycle_tables_hold_names_down_the_first_column_and_cycle_ids_across(tmp_path):
    bound = _write(tmp_path)
    parameter_table = _table(tmp_path / "par_cycle_tbl.csv")
    assert parameter_table[0] == ["", "0", "1", "2"]
    assert parameter_table[1] == ["perlen", "31.0", "29.0", "30.0"]
    observation_table = _table(tmp_path / "obs_cycle_tbl.csv")
    assert observation_table[0] == ["", "0", "1", "2"]
    rows = {row[0]: row for row in observation_table[1:]}
    assert rows["obs_a"] == ["obs_a", "1.0", "1.5", ""]
    assert rows["head_a"] == ["head_a", "2.0", "1.9", "1.8"]
    weight_table = _table(tmp_path / "weight_cycle_tbl.csv")
    weights = {row[0]: row for row in weight_table[1:]}
    assert weights["obs_a"] == ["obs_a", "1.0", "2.0", "0.0"]
    assert bound["cycle_tables"] == {"parameters": "par_cycle_tbl.csv",
                                     "observations": "obs_cycle_tbl.csv", "weights": "weight_cycle_tbl.csv"}


def test_the_control_states_the_cycle_tables_and_the_da_options(tmp_path):
    _write(tmp_path)
    lines = (tmp_path / "pestpp.pst").read_text().splitlines()
    for line in ("da_parameter_cycle_table par_cycle_tbl.csv",
                 "da_observation_cycle_table obs_cycle_tbl.csv",
                 "da_weight_cycle_table weight_cycle_tbl.csv",
                 "noptmax 1", "da_num_reals 5", "ies_no_noise true"):
        assert line in lines
    tables = {line: lines[index + 1] for index, line in enumerate(lines)
              if line.endswith(" external")}
    parameters = _table(tmp_path / tables["* parameter data external"])
    assert parameters[0][-1] == "cycle"
    assert {row[0]: row[-1] for row in parameters[1:]}["wel_0"] == "0"
    observations = _table(tmp_path / tables["* observation data external"])
    assert observations[0][-2:] == ["cycle", "state_par_link"]
    templates = _table(tmp_path / tables["* model input external"])
    assert templates[0] == ["pest_file", "model_file", "cycle"]


def test_state_linkage_is_by_shared_name_or_the_explicit_link(tmp_path):
    bound = _write(tmp_path, observations=[
        {**_observations()[0]},
        {"name": "sim_head_a", "instruction": "heads.ins", "model_file": "heads.out", "group": "state",
         "value": 2.0, "weight": 0.0, "cycle": -1, "source": "stated", "state_par_link": "head_a"},
        {**_observations()[2]},
    ])
    assert bound["state"] == [{"parameter": "head_a", "observation": "sim_head_a"}]
    observations = _table(tmp_path / "study_observations.csv")
    row = next(row for row in observations[1:] if row[0] == "sim_head_a")
    assert row[-1] == "head_a"


def test_one_observation_may_read_a_different_instruction_in_each_cycle(tmp_path):
    for cycle in (0, 1, 2):
        (tmp_path / f"heads_{cycle}.ins").write_text("pif ~\n")
    observations = [_observations()[0], _observations()[1], _observations()[2]]
    observations[0] = {**observations[0], "instruction": None,
                       "instructions": {cycle: f"heads_{cycle}.ins" for cycle in (0, 1, 2)}}
    _write(tmp_path, observations=observations, command="python forward_run.py")
    lines = (tmp_path / "pestpp.pst").read_text().splitlines()
    assert "python forward_run.py" in lines
    table = _table(tmp_path / "study_instructions.csv")
    rows = {tuple(row) for row in table[1:]}
    assert {("heads_0.ins", "heads.out", "0"), ("heads_1.ins", "heads.out", "1"),
            ("heads_2.ins", "heads.out", "2")} <= rows


def test_an_unlinked_state_parameter_refuses_by_name(tmp_path):
    with pytest.raises(PestppError, match="head_a") as refused:
        _write(tmp_path, observations=[_observations()[0], _observations()[2]])
    assert refused.value.error_code == "PESTPP_DA_STATE_UNLINKED"


def test_a_state_link_to_a_parameter_that_is_not_fixed_or_absent_refuses(tmp_path):
    observations = [*_observations()[:2],
                    {"name": "obs_b", "instruction": "heads.ins", "model_file": "heads.out",
                     "group": "heads", "value": 2.0, "weight": 1.0, "cycle": -1,
                     "source": "stated", "state_par_link": "missing"}]
    with pytest.raises(PestppError, match="missing") as refused:
        _write(tmp_path, observations=observations)
    assert refused.value.error_code == "PESTPP_DA_STATE_UNLINKED"


@pytest.mark.parametrize(("overrides", "code", "name"), [
    ({"cycles": [{"id": 1, "period": 2}, {"id": 0, "period": 1}]}, "PESTPP_DA_CYCLE_ORDER", "ordered"),
    ({"cycles": [{"id": 0, "period": 1}, {"id": 0, "period": 2}]}, "PESTPP_DA_CYCLE_ORDER", "ordered"),
    ({"cycles": [{"id": 0, "period": 1}, {"id": 1, "period": 1}]}, "PESTPP_DA_CYCLE_ORDER", "period"),
    ({"cycles": [{"id": 0, "period": 9}]}, "PESTPP_DA_CYCLE_CLOCK", "9"),
    ({"cycles": [{"id": 0, "period": 1, "length": 99.0}]}, "PESTPP_DA_CYCLE_CLOCK", "99"),
    ({"observations": [{**_observations()[0], "values": {"9": 1.0}}]},
     "PESTPP_DA_CYCLE_TABLE", "9"),
    ({"observations": [{**_observations()[0], "weights": {"9": 1.0}}]},
     "PESTPP_DA_CYCLE_TABLE", "9"),
    ({"parameters": [{**_parameters()[0], "source": ""}]},
     "PESTPP_DA_PARAMETER_SOURCE", "k"),
    ({"parameters": [{**_parameters()[0], "lower": None}]},
     "PESTPP_DA_PARAMETER_BOUNDS", "k"),
    ({"observations": [{**_observations()[0], "weight": None}]},
     "PESTPP_DA_OBSERVATION_WEIGHT", "obs_a"),
    ({"observations": [{**_observations()[0], "source": ""}]},
     "PESTPP_DA_OBSERVATION_SOURCE", "obs_a"),
    ({"options": {"not_a_pestpp_option": 1}}, "PESTPP_OPTION_UNKNOWN", "not_a_pestpp_option"),
    ({"options": {"save_binary": True}}, "PESTPP_OPTION_LOCKED", "save_binary"),
])
def test_authoring_refuses_a_missing_or_mismatched_choice_by_name(tmp_path, overrides, code, name):
    with pytest.raises(PestppError, match=name) as refused:
        _write(tmp_path, **overrides)
    assert refused.value.error_code == code


def test_a_cycle_must_name_a_one_based_existing_stress_period(tmp_path):
    with pytest.raises(PestppError) as refused:
        _write(tmp_path, cycles=[{"id": 0, "period": 0}])
    assert refused.value.error_code == "PESTPP_DA_CYCLE_CLOCK"
