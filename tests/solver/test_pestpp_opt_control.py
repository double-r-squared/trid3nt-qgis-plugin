"""The deterministic OPT control uses forecast rows with their correct roles."""

import csv

import pytest

from trid3nt.workflows.pestpp.authoring.control import Parameter, write_opt_control
from trid3nt.workflows.pestpp.errors import PestppError


def _write(tmp_path, **overrides):
    values = {
        "parameters": [
            Parameter("well_1", -2.0, -5.0, 0.0, "well_rates", transform="none", change_limit="relative"),
        ],
        "objective": {"name": "forecast", "sense": "max", "source": "declared objective"},
        "constraints": [
            {"name": "upstream", "sense": "<=", "limit": -5.0, "source": "upstream limit"},
            {"name": "downstream", "sense": ">=", "limit": 4.0, "source": "downstream limit"},
        ],
        "templates": [("wells.tpl", "wells.wel")],
        "instructions": [("reads.ins", "reads.txt")],
        "command": "python /opt/modflow_trial.py",
        "iterations": 2,
    }
    return write_opt_control(tmp_path, **{**values, **overrides})


def test_opt_control_writes_roles_bounds_and_signed_limits(tmp_path):
    path = _write(tmp_path)
    text = path.read_text()
    for line in ("opt_dec_var_groups well_rates", "opt_obj_func forecast",
                 "opt_direction max", "opt_risk 0.5", "noptmax 2"):
        assert line in text.splitlines()
    tables = {section: filename for section, filename in zip(text.splitlines(), text.splitlines()[1:])
              if section.endswith(" external")}
    with (tmp_path / tables["* parameter data external"]).open() as file:
        decision, = csv.DictReader(file)
    assert (decision["parlbnd"], decision["parubnd"], decision["partrans"]) == ("-5.0", "0.0", "none")
    with (tmp_path / tables["* observation data external"]).open() as file:
        rows = {row["obsnme"]: row for row in csv.DictReader(file)}
    assert float(rows["forecast"]["weight"]) == 0
    assert rows["upstream"]["obgnme"] == "less_than"
    assert rows["downstream"]["obgnme"] == "greater_than"
    assert (float(rows["upstream"]["obsval"]), float(rows["downstream"]["obsval"])) == (-5, 4)


@pytest.mark.parametrize(("overrides", "code", "name"), [
    ({"objective": {"name": "forecast", "sense": "max"}}, "PESTPP_OPT_SOURCE_UNSTATED", "forecast"),
    ({"constraints": [{"name": "stream", "sense": "<=", "source": "source"}]},
     "PESTPP_OPT_LIMIT_UNSTATED", "stream"),
    ({"parameters": [Parameter("well_1", -2.0, None, 0.0, "wells", transform="none")]},
     "PESTPP_OPT_BOUNDS_UNSTATED", "well_1"),
])
def test_opt_control_names_missing_choices(tmp_path, overrides, code, name):
    with pytest.raises(PestppError, match=name) as refused:
        _write(tmp_path, **overrides)
    assert refused.value.error_code == code
