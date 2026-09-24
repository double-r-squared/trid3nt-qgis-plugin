"""The rule a structure face is cut under, and the one number that cuts it.

Measured on a live acceptance arm that would not solve: a lone solid node inside
the designated liquid stretch stopped ARTEMIS in FRONT2.
"""

from __future__ import annotations

import numpy as np


# -- the structure: ONE number ------------------------------------------------ #

def test_the_footprint_and_the_solid_faces_are_cut_at_the_same_width():
    """The mesher removes water inside HALF the declared width of the centreline,
    and the deck calls solid exactly what stands on what it removed. Two numbers
    here would let the deck stamp a face on water the cut left behind."""
    from trid3nt_server.tools import TOOL_REGISTRY

    plan = TOOL_REGISTRY["artemis_harbor_agitation"].fn.workflow.plan
    steps = {step.label: step for step in plan.declared()}
    footprint = steps["footprint"]
    # A ref refuses to compare itself at plan-construction time, which is what
    # keeps a description from being read as a value; the NAME is the statement.
    assert footprint.kwargs["width_m"].name == "barrier_width_m"
    assert steps["settled"].kwargs["structure_width_m"].name == "barrier_width_m"


def test_a_boundary_node_is_on_the_structure_when_it_stands_on_the_punched_outline():
    """The water inside the footprint was removed, so a boundary node is on the cut when
    it stands at the half-width, to the precision a relaxation places a node on a
    locked outline. An equality at exactly 10.000 m would halve one population."""
    from trid3nt_server.workflows.telemac.authoring.walked_boundary import _nodes_near

    # one 200 m centreline segment, cut 20 m wide on a 5 m mesh: the outline runs
    # at 10 m and a node sits on it to within an edge.
    band = 20.0 / 2.0 + 5.0
    segments = [[0.0, 0.0, 200.0, 0.0]]
    points = np.array([[100.0, 10.4],     # on the outline, an edge's slack out
                       [100.0, -9.6],     # on it, the other face
                       [100.0, 40.0],     # open water well off the cut
                       [400.0, 0.0]])     # past the end of the structure
    assert _nodes_near(segments, points, [0, 1, 2, 3], band) == [0, 1]
    assert _nodes_near(segments, points, [0, 1, 2, 3], 10.0) == [1]


def test_a_lone_node_between_two_of_another_kind_is_not_a_face():
    """front2.f refuses "a solid point between two liquid points" and the reverse
    by name, so the walk settles them: a role is a RUN, and a single node whose
    two neighbours agree with each other is theirs."""
    from trid3nt_server.workflows.telemac.authoring.walked_boundary import _settled_walk

    walk = [10, 11, 12, 13, 14, 15, 16, 17]
    structure, liquid = _settled_walk(
        walk, structure={10, 11, 13, 14}, liquid={12, 15, 16, 17})
    assert structure == [10, 11, 12, 13, 14]      # the hole at 12 closes
    assert liquid == [15, 16, 17]
    # and a lone structure node inside a liquid run goes the other way.
    structure, liquid = _settled_walk(
        walk, structure={13}, liquid={10, 11, 12, 14, 15, 16, 17})
    assert structure == []


def test_the_thermocline_is_stated_below_the_water_top_not_the_datum():
    """The hook has only the node's elevation, so a free surface the run opened
    above the datum has to enter the depth the thermocline is placed at."""
    from trid3nt_server.workflows.telemac.modules.telemac3d import _condi_thermocline

    assert "DPTH=0.2910D0-Z(I3)" in _condi_thermocline(
        8.0, 18.0, 6.0, 1.5, 0.291)
