"""The trade-off control states several objectives with their senses, decisions, and the search settings."""

import csv

import pytest

from trid3nt.workflows.pestpp.authoring.control import Parameter, write_tradeoff_control
from trid3nt.workflows.pestpp.errors import PestppError


def _write(tmp_path, **overrides):
    values = {
        "parameters": [
            Parameter("well_1", -2.0, -5.0, 0.0, "well_rates", transform="none", change_limit="relative"),
        ],
        "objectives": [
            {"name": "obj_1", "sense": "min", "source": "declared objective"},
            {"name": "obj_2", "sense": "max", "source": "declared objective"},
        ],
        "constraints": [
            {"name": "stream", "sense": "<=", "limit": -5.0, "source": "upstream limit"},
        ],
        "templates": [("wells.tpl", "wells.wel")],
        "instructions": [("reads.ins", "reads.txt")],
        "command": "python /opt/modflow_trial.py",
        "generations": 3,
        "population": 12,
        "generator": "de",
        "seed": 7,
    }
    return write_tradeoff_control(tmp_path, **{**values, **overrides})


def test_tradeoff_control_states_objectives_senses_and_the_search(tmp_path):
    path = _write(tmp_path)
    text = path.read_text()
    for line in ("opt_dec_var_groups well_rates", "mou_objectives obj_1,obj_2",
                 "mou_population_size 12", "mou_generator de", "rand_seed 7", "noptmax 3"):
        assert line in text.splitlines()
    tables = {section: filename for section, filename in zip(text.splitlines(), text.splitlines()[1:])
              if section.endswith(" external")}
    with (tmp_path / tables["* parameter data external"]).open() as file:
        decision, = csv.DictReader(file)
    assert (decision["parlbnd"], decision["parubnd"], decision["partrans"]) == ("-5.0", "0.0", "none")
    with (tmp_path / tables["* observation data external"]).open() as file:
        rows = {row["obsnme"]: row for row in csv.DictReader(file)}
    assert rows["obj_1"]["obgnme"] == "less_than"
    assert rows["obj_2"]["obgnme"] == "greater_than"
    assert rows["stream"]["obgnme"] == "less_than"
    assert all(float(row["weight"]) == 1 for row in rows.values())
    assert (float(rows["obj_1"]["obsval"]), float(rows["stream"]["obsval"])) == (0.0, -5.0)


@pytest.mark.parametrize(("overrides", "code", "name"), [
    ({"objectives": [{"name": "obj_1", "source": "source"}]}, "PESTPP_MOU_SENSE", "obj_1"),
    ({"objectives": [{"name": "obj_1", "sense": "min"}]}, "PESTPP_MOU_SOURCE_UNSTATED", "obj_1"),
    ({"parameters": [Parameter("well_1", -2.0, None, 0.0, "wells", transform="none")]},
     "PESTPP_MOU_BOUNDS_UNSTATED", "well_1"),
    ({"constraints": [{"name": "stream", "sense": "<=", "source": "source"}]},
     "PESTPP_MOU_LIMIT_UNSTATED", "stream"),
    ({"constraints": [{"name": "obj_1", "sense": "<=", "limit": 1.0, "source": "source"}]},
     "PESTPP_MOU_NAME", "obj_1"),
    ({"objectives": [{"name": "obj_1", "sense": "min", "source": "source"}]},
     "PESTPP_MOU_OBJECTIVES", "two"),
    ({"generations": 0}, "PESTPP_MOU_ITERATIONS", "generations"),
    ({"population": 0}, "PESTPP_MOU_POPULATION", "population"),
    ({"generator": "tournament"}, "PESTPP_MOU_GENERATOR", "tournament"),
    ({"seed": "7"}, "PESTPP_MOU_SEED", "seed"),
])
def test_tradeoff_control_names_missing_choices(tmp_path, overrides, code, name):
    with pytest.raises(PestppError, match=name) as refused:
        _write(tmp_path, **overrides)
    assert refused.value.error_code == code
