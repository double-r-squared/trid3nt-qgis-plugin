"""The result is opened by the daemon itself, and nothing shells into the image.

Offline: the pair is written and read back in this process, so the numbering, the
header and the arrays every downstream read consumes are exercised without the
engine. That the ENGINE accepts what is written here is proved by a solve.
"""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.workflows.mesh.shared import selafin_io as IO
from trid3nt_server.workflows.telemac.modules import outputs as R

#: Every module on this side that reaches a solved result. None may run a
#: container to do it: the image solves and does nothing else.
_READERS = (
    "trid3nt_server/workflows/mesh/shared/selafin_io.py",
    "trid3nt_server/workflows/telemac/modules/outputs.py",
    "trid3nt_server/workflows/telemac/authoring/assembler.py",
)

_REPO = Path(__file__).resolve().parents[2]

#: A unit square split in two, walked as one contour of four boundary nodes.
_X = np.array([0.0, 1.0, 1.0, 0.0])
_Y = np.array([0.0, 0.0, 1.0, 1.0])
_CELLS = np.array([[0, 1, 2], [0, 2, 3]])


def _written(tmp_path, bed=None):
    """The geometry pair for that square -> the written paths and stats."""
    return IO.write_telemac_pair(
        tmp_path, x=_X, y=_Y, cells=_CELLS,
        bed=np.array([1.0, 2.0, 3.0, 4.0]) if bed is None else bed,
        roles={"inflow": [0, 3]}, title="SQUARE")


def test_the_pair_is_written_here_and_read_back_here(tmp_path):
    written = _written(tmp_path)
    mesh = R.read_selafin(written["geo_slf"])

    # the variable NAME is the module's own, with no unit glued to it.
    assert mesh["varnames"] == ["BOTTOM"]
    assert mesh["varunits"] == ["M"]
    assert mesh["npoin"] == 4 and mesh["nelem"] == 2
    assert mesh["data"]["BOTTOM"].shape == (1, 4)
    assert mesh["data"]["BOTTOM"][0].tolist() == [1.0, 2.0, 3.0, 4.0]
    assert np.array_equal(mesh["ikle"], _CELLS)
    assert mesh["x"].tolist() == _X.tolist()
    # the origin is REPORTED, never applied: the coordinates stay as the file
    # stores them, because every postprocess adds the origin it recovered itself.
    assert (mesh["x_origin"], mesh["y_origin"]) == (0, 0)


def test_the_boundary_file_is_numbered_by_the_geometrys_own_ipobo(tmp_path):
    """The two files are one artifact: row k of the ``.cli`` is IPOBO position k."""
    written = _written(tmp_path)
    rows = [line.split() for line in written["cli"].read_text().splitlines()]
    assert len(rows) == written["stats"]["nptfr"] == 4
    assert [int(row[-1]) for row in rows] == [1, 2, 3, 4]
    quads = {int(row[-2]) - 1: (int(row[0]), int(row[1])) for row in rows}
    assert quads[0] == (IO.KSORT, IO.KENT) and quads[3] == (IO.KSORT, IO.KENT)
    assert quads[1] == (IO.KLOG, IO.KLOG)
    assert written["stats"]["liquid_boundary_prescribes"] == ["flowrate"]


def test_a_refusal_names_the_file(tmp_path):
    slf = tmp_path / "truncated.slf"
    slf.write_bytes(b"")
    with pytest.raises(R.SelafinReadError) as ei:
        R.read_selafin(slf)
    assert "truncated.slf" in str(ei.value)


@pytest.mark.parametrize("module", _READERS)
def test_no_reader_on_this_side_starts_a_container(module):
    """The engine image solves; a read that wakes one costs a container per file."""
    tree = ast.parse((_REPO / module).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not {name for name in imported if "image_script" in name}
    assert "docker" not in (_REPO / module).read_text()
