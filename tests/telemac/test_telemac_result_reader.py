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
    """A file whose frames do not add up to its header refuses by name too."""
    written = _written(tmp_path)
    whole = Path(written["geo_slf"]).read_bytes()
    cut = tmp_path / "cut.slf"
    # the header states one frame over four nodes and the bytes are 16 short of
    # it, which is the mismatch the reader reports rather than a short series.
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


#: A square ring of sixteen nodes with the middle square left out, so the walk
#: returns two contours. The writer's own numbering of that geometry and ours
#: disagree on which node each ring reaches when - the one shape where handing
#: the walk's IPOBO over is the difference between the ``.cli`` rows and the
#: geometry agreeing and not.
_RING_X = np.array([float(k % 4) for k in range(16)])
_RING_Y = np.array([float(k // 4) for k in range(16)])
_RING_CELLS = np.array(
    [tri for j in range(3) for i in range(3) if (i, j) != (1, 1)
     for tri in ([j * 4 + i, j * 4 + i + 1, j * 4 + i + 5],
                 [j * 4 + i, j * 4 + i + 5, j * 4 + i + 4])])


def test_the_geometry_carries_the_walks_own_numbering(tmp_path):
    """IPOBO in the file is the walk's, not the writer's own rebuild of it.

    The ``.cli`` rows are written in the walk's order, so a geometry numbered by
    any other walk classifies the wrong nodes without saying anything."""
    from serafin import SerafinHeader, SerafinReader

    from trid3nt_server.workflows.mesh.shared.formats.tin_topology import (
        boundary_numbering)

    ours, _ = boundary_numbering(_RING_CELLS, _RING_X.shape[0])
    rebuilt = SerafinHeader(title="RING")
    rebuilt.from_triangulation(
        np.column_stack([_RING_X, _RING_Y]), _RING_CELLS + 1)
    # the two walks disagree on this shape, so the assertion below discriminates.
    assert not np.array_equal(np.asarray(rebuilt.ipobo), ours)

    written = IO.write_telemac_pair(
        tmp_path, x=_RING_X, y=_RING_Y, cells=_RING_CELLS,
        bed=np.zeros(16), roles={"inflow": [0, 1]})
    with SerafinReader(str(written["geo_slf"]), "en") as reader:
        reader.read_header()
        assert np.array_equal(np.asarray(reader.header.ipobo), ours)


def test_the_origin_is_reported_and_the_coordinates_stay_as_stored(tmp_path):
    """A file carrying an origin reads back at its STORED coordinates.

    Every postprocess recovers the origin itself and adds it, so applying it
    here would place the mesh twice as far out as the file puts it."""
    from serafin import SerafinHeader, SerafinWriter

    from trid3nt_server.workflows.mesh.shared.formats.tin_topology import (
        boundary_numbering)

    ipobo, _ = boundary_numbering(_CELLS, 4)
    header = SerafinHeader(title="OFFSET")
    header.from_triangulation(np.column_stack([_X, _Y]), _CELLS + 1, ipobo)
    header.add_variable_str("BOTTOM", "BOTTOM", "M")
    header.set_mesh_origin(100, 200)
    slf = tmp_path / "offset.slf"
    with SerafinWriter(str(slf), "en", overwrite=True) as writer:
        writer.write_header(header)
        writer.write_entire_frame(header, 0.0, np.zeros((1, 4)))

    mesh = R.read_selafin(slf)
    assert (mesh["x_origin"], mesh["y_origin"]) == (100, 200)
    assert mesh["x"].tolist() == _X.tolist()
    assert mesh["y"].tolist() == _Y.tolist()


def test_a_three_dimensional_result_keeps_every_plane(tmp_path):
    """NPLAN planes over the 2D mesh, and ``ikle`` is the PRISM connectivity.

    Reading the 2D connectivity for a 3D file drops every plane but one."""
    from serafin import SerafinHeader, SerafinWriter

    from trid3nt_server.workflows.mesh.shared.formats.tin_topology import (
        boundary_numbering)

    ipobo, _ = boundary_numbering(_CELLS, 4)
    flat = SerafinHeader(title="BASIN")
    flat.from_triangulation(np.column_stack([_X, _Y]), _CELLS + 1, ipobo)
    flat.add_variable_str("ELEVATION Z", "ELEVATION Z", "M")
    header = flat.copy_as_3d(3)
    slf = tmp_path / "basin3d.slf"
    with SerafinWriter(str(slf), "en", overwrite=True) as writer:
        writer.write_header(header)
        writer.write_entire_frame(
            header, 0.0, np.arange(12, dtype=float).reshape(1, -1))

    mesh = R.read_selafin(slf)
    assert (mesh["nplan"], mesh["npoin2"], mesh["nelem2"]) == (3, 4, 2)
    assert mesh["npoin"] == 12 and mesh["nelem"] == 4
    assert mesh["ikle"].shape == (4, 6)
    assert mesh["ikle2"].shape == (2, 3)
    assert mesh["data"]["ELEVATION Z"].shape == (1, 12)


def test_a_frame_that_will_not_read_refuses_by_the_same_name(tmp_path,
                                                             monkeypatch):
    """A header that opens is not a file that reads.

    The library raises where a frame disagrees with the header it was declared
    by; that is unreadable too, and says so by the file's name rather than
    returning a series one frame short."""
    import serafin

    written = _written(tmp_path)
    broken = tmp_path / "half.slf"
    broken.write_bytes(Path(written["geo_slf"]).read_bytes())

    def refuse(self, index):
        raise serafin.SerafinRequestError("frame is not the header's")

    monkeypatch.setattr(serafin.SerafinReader, "read_vars_in_frame", refuse)
    with pytest.raises(R.SelafinReadError) as ei:
        R.read_selafin(broken)
    assert "half.slf" in str(ei.value)


def test_every_image_run_left_on_this_side_is_a_solve():
    """Nothing but an engine run costs a container start.

    The result read and the pair write are this process's own; a third caller
    here is a per-file container start that nobody asked for."""
    server = _REPO / "trid3nt_server"
    callers = {module.relative_to(_REPO).as_posix()
               for module in sorted(server.rglob("*.py"))
               if "run_image_script" in module.read_text()
               and module.name != "image_script.py"}
    assert callers == {
        "trid3nt_server/workflows/mesh/meshers/om2d.py",
        "trid3nt_server/workflows/telemac/authoring/cas_validate.py"}
