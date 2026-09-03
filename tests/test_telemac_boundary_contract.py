"""The cross-file boundary contract: the ``.cli`` quad and the steering keyword.

A TELEMAC boundary states itself twice, and the engine reads the steering value
only where the code quad says to. These two files therefore have to come from ONE
decision, in ONE numbering, or the disagreement is silent: the number is written,
never read, and the face runs on what its code alone means.

Both halves are pinned here. The NUMBERING is the engine's own rule ported off
``bief/front2.f`` - it starts each contour at the south-westernmost boundary
point, not at the first row of the file - and the KEYWORD is derived from the
quad the boundary file carries rather than from the role's name.
"""

from __future__ import annotations

import sys

import pytest

from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir
from trid3nt_server.workflows.telemac.authoring import author as A

sys.path.insert(0, str(drivers_dir()))
import selafin_cli_driver as D  # noqa: E402


# --------------------------------------------------------------------------- #
# One contour whose SOUTH-WEST corner lies on a liquid face, which is the
# geometry the two numbering rules disagree on.
# --------------------------------------------------------------------------- #
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
    """Row order would number the outflow first: it is the first liquid run met
    walking the file from its first solid row. The engine starts at the domain's
    south-west corner, which sits on the inflow, so the inflow is boundary 1 -
    and a file written to the row-order answer prescribes both values into codes
    that never read them."""
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


# --------------------------------------------------------------------------- #
# The keyword is read off the quad, so the two files cannot disagree.
# --------------------------------------------------------------------------- #
def test_the_keyword_is_read_off_the_quad_the_boundary_file_carries():
    assert D._prescribes(D._ROLE_CODES["outflow"]) == "elevation"
    assert D._prescribes(D._ROLE_CODES["inflow"]) == "flowrate"
    assert D._prescribes(D._ROLE_CODES["wall"]) == "nothing"


_BED = {"bed_top_m": 100.0, "bed_drop_m": 3.0, "reach_length_m": 1000.0,
        "outflow_section": [[0.0, 100.0], [10.0, 97.0],
                            [50.0, 97.0], [60.0, 100.0]]}


def _cas(tmp_path, numbered) -> str:
    A.author_reach(
        tmp_path, sheet={"name": "reach", "inflow_q_m3s": 50.0,
                         "init_depth_m": 2.0, "duration_s": 600.0,
                         "time_step_s": 1.0},
        geometry="mesh.slf", boundary="mesh.cli", results="r2d.slf",
        steering="t2d_river.cas",
        liquid_boundary_order=[role for role, _ in numbered],
        liquid_boundary_prescribes=[what for _, what in numbered],
        bed=_BED, source_utm=(500.0, 0.0))
    return (tmp_path / "t2d_river.cas").read_text()


def test_the_run_prescribes_at_the_number_whose_quad_reads_it(tmp_path):
    """End to end over the domain above: the engine calls the inflow 1 and the
    outflow 2, so the discharge is first and the level second - each one landing
    on the code that consumes it."""
    numbered = _numbered()
    assert numbered == [("inflow", "flowrate"), ("outflow", "elevation")]
    cas = _cas(tmp_path, numbered)
    assert "PRESCRIBED FLOWRATES            = 50.0;0.0" in cas
    assert "PRESCRIBED ELEVATIONS           = 0.0;97.792" in cas
    assert "/  Measured liquid boundaries: 1 inflow=flowrate, " \
           "2 outflow=elevation" in cas


def test_flipping_the_strategy_moves_the_quad_and_the_keyword_together(tmp_path):
    """ONE table decides both files. Swap what the two roles prescribe and the
    boundary file's quads and the steering file's lists move as one - there is no
    second place holding the old answer for them to disagree from."""
    swapped = {**D._ROLE_CODES,
               "inflow": D._ROLE_CODES["outflow"],
               "outflow": D._ROLE_CODES["inflow"]}
    numbered = _numbered(swapped)
    assert numbered == [("inflow", "elevation"), ("outflow", "flowrate")]
    cas = _cas(tmp_path, numbered)
    assert "PRESCRIBED ELEVATIONS           = 97.792;0.0" in cas
    assert "PRESCRIBED FLOWRATES            = 0.0;50.0" in cas


def test_a_boundary_whose_quad_prescribes_nothing_refuses_rather_than_writing(
        tmp_path):
    """A free-exit quad reads neither list, so a value written at its number
    would be a number the engine never looks at - which is exactly the silence
    this contract exists to end."""
    free_exit = {**D._ROLE_CODES, "outflow": (D.KSORT,) * 4}
    with pytest.raises(A.SteeringAuthorError) as exc:
        _cas(tmp_path, _numbered(free_exit))
    assert exc.value.error_code == "TELEMAC_BOUNDARY_PRESCRIBES_NOTHING"
