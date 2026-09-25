"""The staged run directory, checked clause by clause before it leaves the daemon.

Each clause gets a directory that violates it and nothing else, so a clause that
stopped reading its artifact is a red test rather than a solve that dies in its
first second inside the image.
"""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.workflows.telemac.authoring import selafin_io as IO
from trid3nt_server.workflows.runtime.levers import BOX_CORES
from trid3nt_server.workflows.telemac.authoring import staged_check as C
from trid3nt_server.workflows.telemac.errors import TelemacError

#: A unit square split in two: one contour of four boundary nodes, two of them
#: an inflow face, which is what makes a prescribed list readable at all.
_X = np.array([0.0, 1.0, 1.0, 0.0])
_Y = np.array([0.0, 0.0, 1.0, 1.0])
_CELLS = np.array([[0, 1, 2], [0, 2, 3]])

_DECK = "t2d_square.cas"
_TABLE = "square_boundaries.txt"

#: A table whose one column spans the whole window, opening when the run opens.
_SERIES = "#measured\nT Q(1)\ns m3/s\n0.000 2.0\n600.000 2.0\n"


def _pair(rundir, bed=None):
    IO.write_telemac_pair(
        rundir, x=_X, y=_Y, cells=_CELLS,
        bed=np.array([1.0, 2.0, 3.0, 4.0]) if bed is None else bed,
        roles={"inflow": [0, 3]}, title="SQUARE")


def _deck(**over):
    stated = {"GEOMETRY FILE": "'mesh.slf'",
              "BOUNDARY CONDITIONS FILE": "'mesh.cli'",
              "RESULTS FILE": "'r2d_square.slf'",
              "PRESCRIBED FLOWRATES": "2.2;0.0",
              "TIME STEP": "1.0",
              "NUMBER OF TIME STEPS": "600",
              "DURATION": "600.0"}
    stated.update(over)
    return "".join(f"{keyword} = {value}\n"
                   for keyword, value in stated.items()
                   if value is not None)


def _staged(tmp_path, bed=None, **over):
    """A run directory a solve would take, less whatever the clause breaks."""
    _pair(tmp_path, bed=bed)
    (tmp_path / _DECK).write_text("/// the deck\n" + _deck(**over))
    return tmp_path


def _check(rundir, *, duration_s=600.0, cores=1, inputs=()):
    return C.check_staged_run(
        rundir, steering=_DECK, inputs=list(inputs),
        written_by_the_engine=["r2d_square.slf"],
        duration_s=duration_s, cores=cores)


def _refusal(rundir, **kwargs):
    with pytest.raises(TelemacError) as caught:
        _check(rundir, **kwargs)
    return caught.value


def test_a_whole_staged_run_passes_every_clause(tmp_path):
    read = _check(_staged(tmp_path))

    assert read["boundary_rows"] == 4 and read["liquid_faces"] == 2
    assert read["decks"] == [_DECK]


def test_a_file_the_steering_names_and_nobody_staged_refuses_by_name(tmp_path):
    rundir = _staged(tmp_path, **{"SOURCES FILE": "'square_sources.txt'"})

    refusal = _refusal(rundir)

    assert refusal.error_code == "TELEMAC_STAGED_FILE_MISSING"
    assert "SOURCES FILE" in str(refusal) and "square_sources.txt" in str(refusal)


def test_a_name_staged_in_another_spelling_is_named_as_that(tmp_path):
    rundir = _staged(tmp_path, **{"GEOMETRY FILE": "'MESH.SLF'"})

    refusal = _refusal(rundir)

    assert refusal.error_code == "TELEMAC_STAGED_FILE_MISSING"
    assert "another spelling" in str(refusal) and "mesh.slf" in str(refusal)


def test_a_boundary_file_of_another_length_refuses_against_the_walk(tmp_path):
    rundir = _staged(tmp_path)
    rows = (rundir / "mesh.cli").read_text().splitlines()
    (rundir / "mesh.cli").write_text("\n".join(rows[:-1]) + "\n")

    refusal = _refusal(rundir)

    assert refusal.error_code == "TELEMAC_STAGED_BOUNDARY_MISMATCH"
    assert "3 row" in str(refusal) and "4 boundary node" in str(refusal)


def test_a_boundary_file_numbered_against_another_walk_refuses(tmp_path):
    rundir = _staged(tmp_path)
    rows = (rundir / "mesh.cli").read_text().splitlines()
    rows[0], rows[1] = rows[1], rows[0]
    (rundir / "mesh.cli").write_text("\n".join(rows) + "\n")

    refusal = _refusal(rundir)

    assert refusal.error_code == "TELEMAC_STAGED_BOUNDARY_MISMATCH"
    assert "row 1" in str(refusal)


def test_a_liquid_face_the_deck_prescribes_nothing_at_refuses(tmp_path):
    rundir = _staged(tmp_path, **{"PRESCRIBED FLOWRATES": "2.2"})

    refusal = _refusal(rundir)

    assert refusal.error_code == "TELEMAC_STAGED_BOUNDARY_UNPRESCRIBED"
    assert "PRESCRIBED FLOWRATES" in str(refusal)


def test_a_series_that_stops_short_of_the_window_refuses(tmp_path):
    rundir = _staged(tmp_path, **{"LIQUID BOUNDARIES FILE": f"'{_TABLE}'"})
    (rundir / _TABLE).write_text(_SERIES.replace("600.000", "60.000"))

    refusal = _refusal(rundir)

    assert refusal.error_code == "TELEMAC_STAGED_SERIES_SHORT"
    assert _TABLE in str(refusal) and "600" in str(refusal)


def test_a_series_over_the_whole_window_passes(tmp_path):
    rundir = _staged(tmp_path, **{"LIQUID BOUNDARIES FILE": f"'{_TABLE}'"})
    (rundir / _TABLE).write_text(_SERIES)

    assert _check(rundir)["liquid_faces"] == 2


def test_a_node_the_bed_never_painted_refuses(tmp_path):
    rundir = _staged(tmp_path, bed=np.array([1.0, 2.0, np.nan, 4.0]))

    refusal = _refusal(rundir)

    assert refusal.error_code == "TELEMAC_STAGED_BED_UNPAINTED"
    assert "1 of 4 node" in str(refusal)


def test_a_clock_whose_steps_do_not_make_the_window_refuses(tmp_path):
    rundir = _staged(tmp_path, **{"NUMBER OF TIME STEPS": "100"})

    refusal = _refusal(rundir)

    assert refusal.error_code == "TELEMAC_STAGED_CLOCK_INCONSISTENT"
    assert "100" in str(refusal) and "600" in str(refusal)


def test_a_partition_past_the_box_refuses_rather_than_being_cut_to_fit(tmp_path):
    rundir = _staged(tmp_path,
                     **{"PARALLEL PROCESSORS": str(BOX_CORES + 1)})

    refusal = _refusal(rundir)

    assert refusal.error_code == "TELEMAC_STAGED_CORES_OVER_BOX"
    assert str(BOX_CORES + 1) in str(refusal) and str(BOX_CORES) in str(refusal)


def test_a_mesh_still_in_the_store_is_read_through_its_staged_name(tmp_path, monkeypatch):
    """A file the launcher will stage is checked as the object it is now."""
    rundir = _staged(tmp_path)
    store = tmp_path / "store"
    store.mkdir()
    for name in ("mesh.slf", "mesh.cli"):
        (store / name).write_bytes((rundir / name).read_bytes())
        (rundir / name).unlink()
    monkeypatch.setattr(
        "trid3nt_server.tools.cache.read_object_bytes_s3",
        lambda uri: (store / uri.rsplit("/", 1)[-1]).read_bytes())

    read = _check(rundir, inputs=[{"gs_uri": f"s3://bucket/{name}", "dest": name}
                                  for name in ("mesh.slf", "mesh.cli")])

    assert read["boundary_rows"] == 4


@pytest.mark.asyncio
async def test_a_run_staged_against_a_clause_refuses_before_the_manifest(
        tmp_path, monkeypatch):
    """The clauses read on the real path: the staging seam calls them itself."""
    from trid3nt_server.workflows.telemac.authoring import staging

    rundir = _staged(tmp_path, **{"SOURCES FILE": "'square_sources.txt'"})
    written: list[str] = []
    monkeypatch.setattr(staging, "_upload_authored", lambda *a, **k: [])
    monkeypatch.setattr(staging, "_write_manifest",
                        lambda *a, **k: written.append("manifest") or "s3://m")

    with pytest.raises(TelemacError) as caught:
        await staging.stage_run(
            rundir, "run-tag", module="telemac2d", steering=_DECK,
            results=["r2d_square.slf"], outputs=[], mesh_inputs=[], prefix="p",
            sheet={}, server_facts={"duration_s": 600.0},
            result_basename="r2d_square")

    assert caught.value.error_code == "TELEMAC_STAGED_FILE_MISSING"
    assert written == []


def test_a_supplied_mesh_with_no_pair_is_taken_and_the_run_refuses_by_name(tmp_path):
    """The guarantee the artifact stopped stating, standing where it is measured.

    A mesh carrying no pair is taken by the door - a mesh is not unsolvable in
    the abstract - and the run authored on it refuses here, naming the steering
    keyword and the file nobody staged."""
    from trid3nt_server.mesh.artifact import MeshArtifact
    from trid3nt_server.mesh.tool import resolve_mesh
    from trid3nt_server.workflows.runtime.accepts import Accepts

    bare = MeshArtifact(
        mesh_id="01BARE", name="square", mode="om2d",
        display_uri="s3://m/01BARE/mesh.2dm", crs_authid="EPSG:32617",
        has_bathymetry=True, node_count=4, element_count=2,
        bbox=(0.0, 0.0, 1.0, 1.0), engine_files={},
        provenance={"recipe": {"mesher": "om2d", "kind": "unstructured_tri"}})
    taken = resolve_mesh(explicit=bare, accepts=Accepts(mesh=("unstructured_tri",)))
    assert taken.artifact is bare and not bare.engine_files

    (tmp_path / _DECK).write_text("/// the deck\n" + _deck())
    refusal = _refusal(tmp_path)
    assert refusal.error_code == "TELEMAC_STAGED_FILE_MISSING"
    assert "GEOMETRY FILE" in str(refusal) and "mesh.slf" in str(refusal)
