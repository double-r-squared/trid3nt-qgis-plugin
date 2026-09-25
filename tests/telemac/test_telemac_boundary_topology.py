"""The boundary numbering TELEMAC's steering author reads off the walk.

Offline: the bundle is measured from the roles and the walk's own numbering, and
refuses a numbering that states what fewer boundaries prescribe than it numbers.
"""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.telemac.authoring import topology as T


def test_the_walk_carries_the_roles_and_the_measured_order():
    read = T.boundary_topology(roles={"inflow": [1, 2], "outflow": [7]},
                               liquid_boundary_order=["outflow", "inflow"],
                               liquid_boundary_prescribes=["elevation", "flowrate"])
    assert read["roles"] == {"inflow": [1, 2], "outflow": [7]}
    assert read["liquid_boundary_order"] == ["outflow", "inflow"]
    assert read["liquid_boundary_prescribes"] == ["elevation", "flowrate"]


def test_a_walk_that_states_no_prescription_per_boundary_refuses():
    """It was numbered by the superseded row-order rule, and a steering file
    authored against it prescribes into codes that never read it."""
    with pytest.raises(ValueError, match="rebuild the mesh"):
        T.boundary_topology(roles={"outflow": [7]},
                            liquid_boundary_order=["outflow"],
                            liquid_boundary_prescribes=[])


def test_a_walk_naming_no_liquid_boundary_states_the_closed_basin():
    """A closed basin is an ANSWER. The walk is measured for every mesh, and the
    reader says the boundary is solid wall rather than leaving the caller to read
    an absence."""
    read = T.boundary_topology(roles={}, liquid_boundary_order=[],
                               liquid_boundary_prescribes=[])
    assert read["roles"] == {} and read["liquid_boundary_order"] == []
    assert "names no liquid boundary" in read["states"]


def test_the_walk_states_the_numbering_a_steering_author_reads():
    read = T.boundary_topology(roles={"open": [1]},
                               liquid_boundary_order=["open"],
                               liquid_boundary_prescribes=["elevation"])
    assert read["states"] == "1 liquid boundary, numbered 1=open"


def test_an_empty_role_is_not_a_role():
    """A role naming no node is dropped rather than counted: nothing carries it."""
    read = T.boundary_topology(roles={"inflow": []},
                               liquid_boundary_order=["inflow"],
                               liquid_boundary_prescribes=["flowrate"])
    assert read["roles"] == {}
