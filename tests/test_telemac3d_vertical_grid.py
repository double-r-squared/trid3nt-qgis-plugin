"""The TELEMAC-3D vertical discretisation: the plan, the refusal, the keyword pair.

Offline: plane arithmetic and a composite expansion, no engine and no network.

A uniform sigma spreads the levels evenly, so the near-surface layer over the
deepest column is depth/(NPLAN-1). A deep lake against a metres-thick thermocline
therefore gets a ONE-NODE epilimnion, and the declared initial condition is
unrepresentable however it is written. What is pinned here is that the planner
reproduces the transform ``condim.f`` actually applies - a declared dz computed by
any other formula is fiction - that it spends the least stretch that reaches the
target, and that a column no admissible grid can hold REFUSES by name instead of
solving on a distorted one.
"""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.workflows.telemac.modules.telemac3d import (
    T3D,
    VerticalGrid,
    VerticalGridUnresolved,
    _sigma_planes,
    _STRETCH_BOTTOM,
    _STRETCH_MAX_GROWTH,
    _STRETCH_SURFACE_MAX,
    plan_vertical_grid,
)


def test_sigma_planes_reproduce_the_condim_gotm_stretch():
    """The planner must be the SAME transform condim.f applies, recomputed here
    straight from the dictionary's own formula."""
    nplan, dl, du = 13, 1.0, 2.5
    s = np.arange(nplan) / (nplan - 1)
    expect = (np.tanh((dl + du) * s - dl) + np.tanh(dl)) / (np.tanh(dl) + np.tanh(du))

    assert np.allclose(_sigma_planes(nplan, dl, du), expect)
    assert _sigma_planes(nplan, dl, du)[0] == pytest.approx(0.0)
    assert _sigma_planes(nplan, dl, du)[-1] == pytest.approx(1.0)
    # both coefficients off is the uniform sigma of MESH TRANSFORMATION 1
    assert np.allclose(_sigma_planes(nplan, 0.0, 0.0), s)


def test_a_deep_lake_zooms_the_surface_and_declares_the_achieved_layer():
    """402 m under 13 uniform planes is a 33.5 m near-surface layer against an 8 m
    thermocline - a one-node epilimnion. The zoom must bring it under 4 m."""
    plan = plan_vertical_grid(13, 402.0, 8.0)

    assert plan["mesh_transformation"] == 4
    assert plan["dz_uniform_m"] == pytest.approx(33.5, abs=0.05)
    assert plan["dz_surface_m"] <= 4.0 + 1e-3
    assert plan["layer_growth_ratio"] <= _STRETCH_MAX_GROWTH
    dl, du = plan["mesh_stretching_coefficients"]
    assert dl == pytest.approx(_STRETCH_BOTTOM)
    assert 0.0 < du < _STRETCH_SURFACE_MAX
    # the DECLARED number is the one the transform actually produces
    z = _sigma_planes(13, dl, du)
    assert (z[-1] - z[-2]) * 402.0 == pytest.approx(plan["dz_surface_m"], abs=1e-2)
    assert "near-surface layer" in plan["label"]


def test_the_thermocline_thickness_is_sized_off_the_achieved_layer():
    """A STEP profile's column heat anomaly is O(dz) wrong and reads as physics, so
    the initial condition's thickness is derived from the grid it lands on."""
    plan = plan_vertical_grid(13, 402.0, 8.0)
    assert plan["thermocline_delta_m"] == pytest.approx(2.0 * plan["dz_surface_m"])


def test_a_shallow_basin_keeps_uniform_sigma():
    """Zooming a column the uniform grid already resolves only starves the interior."""
    plan = plan_vertical_grid(13, 20.0, 8.0)

    assert plan["mesh_transformation"] == 1
    assert plan["mesh_stretching_coefficients"] is None
    assert plan["dz_surface_m"] == pytest.approx(20.0 / 12.0, abs=1e-3)


@pytest.mark.parametrize("nplan,depth,thermocline", [
    (7, 402.0, 8.0),      # too few planes: the stretch would need a 3.5x growth
    (13, 402.0, 1.0),     # a metre-thin thermocline under 402 m of water
])
def test_an_unresolvable_column_refuses_rather_than_solving(nplan, depth,
                                                            thermocline):
    with pytest.raises(VerticalGridUnresolved) as excinfo:
        plan_vertical_grid(nplan, depth, thermocline)

    assert excinfo.value.error_code == "TELEMAC3D_VERTICAL_UNRESOLVED"
    message = str(excinfo.value)
    assert "Raise nplan to at least" in message
    assert f"{thermocline:g} m thermocline" in message


def test_the_refusal_names_an_nplan_that_actually_works():
    """Advice nobody checked is advice that sends the caller round again."""
    with pytest.raises(VerticalGridUnresolved) as excinfo:
        plan_vertical_grid(7, 402.0, 8.0)
    need = int(str(excinfo.value).split("Raise nplan to at least ")[1]
               .split()[0].rstrip(","))

    assert plan_vertical_grid(need, 402.0, 8.0)["dz_surface_m"] <= 4.0 + 1e-3


def test_a_zoomed_grid_states_the_dictionary_verified_keyword_pair():
    slots, files = T3D.COMPOSITES["vertical_grid"].expand(
        VerticalGrid(levels=13, max_depth_m=402.0, thermocline_depth_m=8.0))

    assert slots["MESH_TRANSFORMATION"] == 4
    assert len(slots["MESH_STRETCHING_COEFFICIENTS"]) == 2
    assert files == {}


def test_a_uniform_grid_states_nothing_and_lets_the_dictionary_answer():
    """Transformation 1 is the dictionary's own default, so writing it would be the
    wrapper asserting a value - which is the one thing a wrapper never does."""
    slots, files = T3D.COMPOSITES["vertical_grid"].expand(
        VerticalGrid(levels=13, max_depth_m=20.0, thermocline_depth_m=8.0))

    assert slots == {} and files == {}
