"""The cross-file boundary contract: the ``.cli`` quad and the steering keyword.

A boundary states itself twice and the engine reads the steering value only where
the code quad says to, so the two files must come from ONE decision in ONE
numbering or the disagreement is silent. The NUMBERING is the engine's own rule -
each contour starts south-westernmost - and the KEYWORD follows the quad."""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.mesh import topology as T
from trid3nt_server.workflows.mesh.shared import selafin_io as D
from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries


# One contour whose SOUTH-WEST corner lies on a liquid face, which is the
# geometry the two numbering rules disagree on.
#: A rectangle walked counter-clockwise from its own south-west corner. Ranks
#: 11, 0, 1 are the west face and rank 0 IS the corner, so the run the engine
#: opens first is the one that straddles row 0.
_XY = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0),
       (3.0, 1.0), (3.0, 2.0), (3.0, 3.0),
       (2.0, 3.0), (1.0, 3.0), (0.0, 3.0), (0.0, 2.0), (0.0, 1.0)]
_ROLE_OF_RANK = {0: "inflow", 1: "inflow", 11: "inflow",
                 4: "outflow", 5: "outflow"}


def _domain(table=None):
    """``(x, y, bnodes, codes, contour_lengths)`` for that rectangle."""
    codes_for = table or D._ROLE_CODES
    x = [p[0] for p in _XY]
    y = [p[1] for p in _XY]
    bnodes = list(range(len(_XY)))
    codes = [codes_for[_ROLE_OF_RANK.get(k, "wall")] for k in bnodes]
    return x, y, bnodes, codes, [len(_XY)]


def _numbered(table=None):
    """``[(role, prescribes), ...]`` in the order the ENGINE will number them."""
    x, y, bnodes, codes, lengths = _domain(table)
    runs = D._liquid_boundaries(x, y, bnodes, codes, lengths)
    numliq = D._numliq(runs, D._successors(lengths), len(bnodes))
    rows = [[k for k in bnodes if numliq[k] == n] for n in range(1, len(runs) + 1)]
    return [(D._joined([_ROLE_OF_RANK[k] for k in here]),
             D._joined([D._prescribes(codes[k]) for k in here])) for here in rows]


def test_the_engine_numbers_from_its_own_south_west_corner_not_from_row_order():
    """Row order would number the outflow first; the engine starts at the south-west
    corner, which sits on the inflow. A file written to the row-order answer
    prescribes both values into codes that never read them."""
    assert [role for role, _ in _numbered()] == ["inflow", "outflow"]


def test_a_liquid_run_that_straddles_the_first_row_is_ONE_boundary():
    """Ranks 11, 0, 1 are one face. Counted as two, the run would state a value
    for a boundary the engine does not have and drop one it does."""
    x, y, bnodes, codes, lengths = _domain()
    runs = D._liquid_boundaries(x, y, bnodes, codes, lengths)
    assert runs == [[11, 1], [4, 5]]


def test_a_lone_liquid_point_between_two_solid_ones_refuses_as_the_engine_does():
    x, y, bnodes, codes, lengths = _domain()
    codes[8] = D._ROLE_CODES["outflow"]
    with pytest.raises(ValueError, match="lone liquid point"):
        D._liquid_boundaries(x, y, bnodes, codes, lengths)


def test_the_keyword_is_read_off_the_quad_the_boundary_file_carries():
    assert D._prescribes(D._ROLE_CODES["outflow"]) == "elevation"
    assert D._prescribes(D._ROLE_CODES["inflow"]) == "flowrate"
    assert D._prescribes(D._ROLE_CODES["wall"]) == "nothing"


#: What the accepted mesh MEASURED, as the boundaries composite reads it. The
#: stage is a normal depth the assembler derived; here it is a number, because
#: what is under test is which list it lands in.
_MEASURED = {"inflow_q_m3s": 50.0, "outflow_stage_m": 97.792}


def _lists(numbered):
    """The three PRESCRIBED lists the composite writes for that walk."""
    slots, _files = T2D.COMPOSITES["boundaries"].expand(Boundaries(
        measured={**_MEASURED,
                  "liquid_boundary_order": [role for role, _ in numbered],
                  "liquid_boundary_prescribes": [what for _, what in numbered]},
        tracers=[0.0]))
    return slots


def test_the_run_prescribes_at_the_number_whose_quad_reads_it():
    """End to end over the domain above: the engine calls the inflow 1 and the
    outflow 2, so the discharge is first and the level second - each one landing
    on the code that consumes it."""
    numbered = _numbered()
    assert numbered == [("inflow", "flowrate"), ("outflow", "elevation")]
    slots = _lists(numbered)
    assert slots["PRESCRIBED_FLOWRATES"] == [50.0, 0.0]
    assert slots["PRESCRIBED_ELEVATIONS"] == [0.0, 97.792]
    assert slots["PRESCRIBED_TRACERS_VALUES"] == [0.0, 0.0]


def test_a_closed_body_writes_no_prescribed_list_at_all():
    """A body whose edge names no run prescribes nothing anywhere. An EMPTY list
    is not that statement: it is a keyword with nothing after it, which DAMOCLES
    reads as the next line's business."""
    slots = _lists([])
    assert not {"PRESCRIBED_FLOWRATES", "PRESCRIBED_ELEVATIONS",
                "PRESCRIBED_TRACERS_VALUES"} & set(slots)


def test_flipping_the_strategy_moves_the_quad_and_the_keyword_together():
    """ONE table decides both files. Swap what the two roles prescribe and the
    boundary file's quads and the steering file's lists move as one - there is no
    second place holding the old answer for them to disagree from."""
    swapped = {**D._ROLE_CODES,
               "inflow": D._ROLE_CODES["outflow"],
               "outflow": D._ROLE_CODES["inflow"]}
    numbered = _numbered(swapped)
    assert numbered == [("inflow", "elevation"), ("outflow", "flowrate")]
    slots = _lists(numbered)
    assert slots["PRESCRIBED_ELEVATIONS"] == [97.792, 0.0]
    assert slots["PRESCRIBED_FLOWRATES"] == [0.0, 50.0]


def test_a_boundary_whose_quad_prescribes_nothing_refuses_rather_than_writing():
    """An OUTFLOW means a prescribed level, so an all-KSORT quad under that name is the
    two files describing different boundaries: a value written at its number is one
    the engine never looks at."""
    mislabelled = {**D._ROLE_CODES, "outflow": (D.KSORT,) * 4}
    with pytest.raises(ValueError, match="prescribes 'nothing'"):
        _lists(_numbered(mislabelled))


def test_the_free_exit_role_prescribes_nothing_as_a_stated_choice():
    """The same "nothing", under the role that DECLARES it.

    A free exit is a boundary condition - the water leaves at the level and velocity
    the interior brings to it - so the deck writes a placeholder rather than refusing."""
    assert D._prescribes(D._ROLE_CODES[D.FREE_EXIT_ROLE]) == "nothing"
    assert D._ROLE_CODES[D.FREE_EXIT_ROLE] == (D.KSORT,) * 4
    assert D.FREE_EXIT_ROLE == T.FREE_EXIT_ROLE
    slots = _lists([("inflow", "flowrate"), (D.FREE_EXIT_ROLE, "nothing")])
    assert slots["PRESCRIBED_FLOWRATES"] == [50.0, 0.0]
    assert slots["PRESCRIBED_ELEVATIONS"] == [0.0, 0.0]


def test_a_pinched_boundary_refuses_by_name(tmp_path):
    """A domain pinched to a point has no IPOBO permutation, and the walk that
    decides it is the daemon's own - nothing shells out to learn it."""
    import numpy as np

    from trid3nt_server.workflows.mesh.meshers import MeshToolError

    x = np.array([0.0, 1.0, 0.0, 2.0, 1.0, 2.0])
    y = np.array([0.0, 1.0, 2.0, 0.0, 1.0, 2.0])
    cells = np.array([[0, 1, 2], [1, 3, 4], [1, 4, 5]])
    with pytest.raises(MeshToolError) as excinfo:
        D.write_telemac_pair(tmp_path, x=x, y=y, cells=cells, bed=np.zeros(6))
    assert excinfo.value.error_code == "MESH_BOUNDARY_PINCHED"
    assert "pinched" in str(excinfo.value)
