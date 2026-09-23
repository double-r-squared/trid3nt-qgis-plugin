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

from trid3nt_server.workflows.mesh.meshers import MeshToolError
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


def test_a_result_whose_frames_are_cut_short_refuses_by_name(tmp_path):
    """A header that opens is not a file that reads: the short read refuses too."""
    written = _written(tmp_path)
    whole = Path(written["geo_slf"]).read_bytes()
    cut = tmp_path / "cut.slf"
    # the header lands whole and the single frame is severed mid-record.
    cut.write_bytes(whole[:-16])

    with pytest.raises(R.SelafinReadError) as ei:
        R.read_selafin(cut)
    assert "cut.slf" in str(ei.value)


def test_a_pair_that_cannot_be_written_refuses_by_name(tmp_path):
    """No in-image writer is left to fall back to, so the failure is named."""
    missing = tmp_path / "no" / "such" / "rundir"

    with pytest.raises(MeshToolError) as ei:
        IO.write_telemac_pair(missing, x=_X, y=_Y, cells=_CELLS,
                              bed=np.zeros(4), roles={"inflow": [0, 3]})
    assert ei.value.error_code == "MESH_PAIR_WRITE_FAILED"
    assert "mesh.slf" in str(ei.value) and "mesh.cli" in str(ei.value)


def test_no_reader_on_this_side_parses_the_format():
    """One reader of the format's fields, and it is the library's, not ours.

    The byte layout is the engine's to know. A second parser was wrong about it
    twice - it refused a truncated result the engine reads, and it glued the
    record's unit onto every variable name - so every consumer reaches the
    fields through ``read_selafin`` and nothing else opens them.
    """
    server = _REPO / "trid3nt_server"
    opens_format, hand_rolls = set(), set()
    for module in sorted(server.rglob("*.py")):
        text = module.read_text()
        tree = ast.parse(text)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        rel = module.relative_to(_REPO).as_posix()
        if "serafin" in imported:
            opens_format.add(rel)
        if "struct" in imported and "selafin" in text.lower():
            hand_rolls.add(rel)

    assert opens_format == {"trid3nt_server/workflows/mesh/shared/selafin_io.py"}
    assert not hand_rolls


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
