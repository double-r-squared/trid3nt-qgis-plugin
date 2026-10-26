"""The counts the executable fixes and the friction regions a bare run states.

Offline. CLAW_NUM_EQN is the module's: three for shallow water, five for Boussinesq, a contradicting statement refusing by
name. FRICTION_VARIABLE_FRICTION with FRICTION_FRICTION_REGIONS writes the friction.data Clawpack's own writer makes from the
Ike example's two regions and reads back through the staged check; a Manning law a run states, regions without the switch,
and too few aux slots each refuse by name.
"""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.workflows.geoclaw.authoring.rundata import serialize
from trid3nt_server.workflows.geoclaw.authoring.staged_check import check_staged_run
from trid3nt_server.workflows.geoclaw.errors import GeoclawError
from trid3nt_server.workflows.geoclaw.modules import GEO, GEOB
from trid3nt_server.workflows.geoclaw.templates.storm_surge import storm_surge
from trid3nt_server.workflows.geoclaw.templates.tsunami_inundation import tsunami_inundation
from trid3nt_server.workflows.runtime.sheet import fill

_STATED = dict(CLAW_TFINAL=10.0, CLAW_NUM_OUTPUT_TIMES=2)
_AUX = ["center", "capacity", "yleft", "center", "center", "center", "center"]
_INF = float("inf")
#: examples/storm-surge/ike/setrun.py: the domain-wide region and the La-Tex shelf.
_IKE = [[[-99.0, 8.0], [-70.0, 32.0], [_INF, 0.0, -_INF], [0.030, 0.022]],
        [[-98, 25.25], [-90, 30], [_INF, -10.0, -200.0, -_INF], [0.030, 0.012, 0.022]]]


def _numerics(template, base=GEO):
    """The template's keywords over ``base``, its composites left out: a body that asserts only values."""
    return type("Numerics", (base,), {k: v for k, v in template.STEERING.ASSERTED.items() if k not in base.COMPOSITES})


def _refused(code, **over):
    with pytest.raises(GeoclawError) as caught:
        fill(over.pop("body", GEO), **{**_STATED, **over})
    assert caught.value.error_code == f"GEOCLAW_{code}"
    return str(caught.value)


def test_each_module_states_the_equation_count_its_executable_runs():
    assert fill(GEO, **_STATED).stated()["CLAW_NUM_EQN"] == 3
    assert fill(GEOB, **_STATED).stated()["CLAW_NUM_EQN"] == 5
    assert fill(GEO, CLAW_NUM_EQN=3, **_STATED).stated()["CLAW_NUM_EQN"] == 3


def test_a_template_rebased_onto_boussinesq_writes_five_equations(tmp_path):
    for template in (tsunami_inundation, storm_surge):
        assert "CLAW_NUM_EQN" not in template.STEERING.ASSERTED
    serialize(fill(_numerics(tsunami_inundation, GEOB), **_STATED), tmp_path / "b")
    serialize(fill(_numerics(tsunami_inundation), **_STATED), tmp_path / "s")

    assert "5                    =: num_eqn" in (tmp_path / "b" / "claw.data").read_text()
    assert "3                    =: num_eqn" in (tmp_path / "s" / "claw.data").read_text()


@pytest.mark.parametrize("body, value", [(GEO, 5), (GEOB, 3)])
def test_a_count_the_executable_cannot_run_refuses_by_name(body, value):
    message = _refused("NUM_EQN_FIXED", body=body, CLAW_NUM_EQN=value)

    assert body.MODULE in message and str(value) in message


def _friction(**over):
    return fill(GEO, CLAW_NUM_AUX=7, AMR_AUX_TYPE=_AUX, FRICTION_VARIABLE_FRICTION=True, FRICTION_FRICTION_REGIONS=_IKE,
                **{**_STATED, **over})


def test_the_ike_regions_write_the_friction_data_clawpacks_writer_makes_and_read_back(tmp_path):
    from clawpack.clawutil.data import ClawRunData

    sheet = _friction()
    written = serialize(sheet, tmp_path / "ours")
    expected = ClawRunData("geoclaw", 2).friction_data
    expected.variable_friction = True
    expected.friction_regions = [[list(a), list(b), list(c), list(d)] for a, b, c, d in _IKE]
    expected.write(str(tmp_path / "friction.data"))

    assert (tmp_path / "ours" / "friction.data").read_text() == (tmp_path / "friction.data").read_text()
    assert check_staged_run(tmp_path / "ours", sheet=sheet, written=written["written"])["checked"]


def test_a_regions_row_that_the_file_does_not_carry_fails_the_staged_check(tmp_path):
    sheet = _friction()
    written = serialize(sheet, tmp_path)
    path = tmp_path / "friction.data"
    path.write_text(path.read_text().replace("0.03 0.022 ", "0.04 0.022 "))

    with pytest.raises(GeoclawError) as caught:
        check_staged_run(tmp_path, sheet=sheet, written=written["written"])
    assert caught.value.error_code == "GEOCLAW_STAGED_MISMATCH"


def test_a_manning_law_a_run_states_beside_variable_friction_refuses_but_a_templates_yields(tmp_path):
    assert "GEO_MANNING_COEFFICIENT" in _refused("FRICTION_LAW_UNUSED", CLAW_NUM_AUX=7, AMR_AUX_TYPE=_AUX,
                                                  FRICTION_VARIABLE_FRICTION=True, FRICTION_FRICTION_REGIONS=_IKE,
                                                  GEO_MANNING_COEFFICIENT=[0.025])
    sheet = fill(_numerics(storm_surge), **_STATED, FRICTION_VARIABLE_FRICTION=True, FRICTION_FRICTION_REGIONS=_IKE)

    assert sheet.stated()["FRICTION_VARIABLE_FRICTION"] is True and sheet.filled["GEO_MANNING_COEFFICIENT"].provenance.origin.value == "template"


def test_regions_without_the_switch_and_too_few_aux_slots_refuse():
    assert "FRICTION_VARIABLE_FRICTION" in _refused("FRICTION_REGIONS_UNUSED", FRICTION_FRICTION_REGIONS=_IKE)
    assert "at least 4" in _refused("FRICTION_AUX", CLAW_NUM_AUX=3, FRICTION_VARIABLE_FRICTION=True)
    assert "unstated" in _refused("FRICTION_AUX", FRICTION_VARIABLE_FRICTION=True)
    assert "at least 6" in _refused("FRICTION_AUX", CLAW_NUM_AUX=5, FRICTION_FRICTION_INDEX=5, FRICTION_VARIABLE_FRICTION=True)
    assert _friction().stated()["CLAW_NUM_AUX"] == 7
