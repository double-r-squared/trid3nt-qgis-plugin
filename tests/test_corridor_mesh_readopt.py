"""A hand-edited corridor stays solvable, and the bundle a solve adopts round-trips.

Offline. Two seams meet here:

  1. THE EDIT. A corridor's solve does not read its SELAFIN - it rebuilds one from
     the topology bundle with the bed fitted to the reach at authoring time - so a
     hand-edit that dropped that bundle left a session able to ACCEPT a mesh the
     deck then refused to stage, after the user had already approved it. The edit
     now rewrites the bundle from the adopted nodes and cells, and a mesher that
     cannot rewrite what its solve needs refuses at the edit instead.

  2. THE GUARD. The boundary walk the edit runs terminates only on a boundary that
     is a permutation of its own nodes, so the two layer shapes QGIS hands back
     that are not - a collapsed face and a duplicated one - are refused by name
     before it starts. Both are driven under a timeout, because the failure a
     bounded walk prevents is a turn that never comes back rather than a wrong
     answer.

  3. THE BUNDLE. The worker's half of the hand-off carries 23 keys and every one of
     them is read on the solve path, so the round trip is pinned key by key, along
     with the refusal a bundle disagreeing with its own node count raises and the
     absent-file answer that lets a run mesh for itself.

The corridor build itself runs a triangulator in a container, so the built mesh is
supplied here rather than built; everything downstream of it is the real code.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.emission.mesh_display import write_2dm_arrays
from trid3nt_server.workflows.mesh.meshers import Mesh, MeshToolError, get_mesher
from trid3nt_server.workflows.mesh.session import MeshSession
from trid3nt_server.workflows.mesh.tool import tool

from workers.telemac._staged_mesh import (
    STAGED_MESH_FILENAME,
    StagedMeshUnusableError,
    staged_mesh_bundle,
    write_mesh_bundle,
)

_WIDTH_M = 60.0
_UTM = "EPSG:32611"

#: Every key the worker reads off a staged bundle on the solve path.
_BUNDLE_KEYS = (
    "X", "Y", "ikle", "ring", "ipob", "lihbor", "liubor", "livbor", "litbor",
    "cls", "in_nodes", "out_nodes", "boundary_rings", "centerline",
    "npoin", "nptfr", "n_in", "n_out", "n_islands", "domain_mode",
    "water_coverage_frac", "banks_ok", "smooth_tries",
)


# --------------------------------------------------------------------------- #
# A corridor, as the box builds one.
# --------------------------------------------------------------------------- #
def _ribbon(nx: int = 7, ny: int = 3, step: float = 20.0):
    """A straight meshed channel: nodes on a lattice, split into triangles."""
    xs = 500000.0 + step * np.arange(nx, dtype=float)
    ys = 4100000.0 + step * np.arange(ny, dtype=float)
    points = np.array([[x, y] for y in ys for x in xs], dtype=float)
    cells = []
    for j in range(ny - 1):
        for i in range(nx - 1):
            a = j * nx + i
            cells.append([a, a + 1, a + nx])
            cells.append([a + 1, a + nx + 1, a + nx])
    return points, np.asarray(cells, dtype=np.int64)


def _built_bundle(points, cells) -> dict:
    """The mesh dict the corridor build hands its bundle writer.

    The end caps are the inflow and the outflow, which is what a reach solve
    forces discharge in at and out of; everything else on the boundary is bank.
    """
    from trid3nt_server.workflows.mesh.meshers.corridor_tin import (
        _bundle_dict, _boundary_rings, _oriented,
    )

    oriented = _oriented(points, cells)
    rings = _boundary_rings(oriented)
    ring = np.concatenate(rings)
    x = points[ring, 0]
    roles = np.array(["wall"] * ring.size, dtype=object)
    roles[x <= points[:, 0].min() + 1e-6] = "inflow"
    roles[x >= points[:, 0].max() - 1e-6] = "outflow"
    built = {"centerline": np.array([[500000.0, 4100020.0], [500120.0, 4100020.0]]),
             "domain_mode": "water-polygon", "water_coverage_frac": 0.91,
             "banks_ok": True, "smooth_tries": 2}
    return _bundle_dict(points, oriented, rings, ring, roles, built)


def _corridor_mesh(tmp_path: Path) -> Mesh:
    """A built corridor with its three staged files on disk."""
    points, cells = _ribbon()
    mesh = _built_bundle(points, cells)
    rundir = tmp_path / "build"
    rundir.mkdir(parents=True, exist_ok=True)
    write_mesh_bundle(mesh, str(rundir / STAGED_MESH_FILENAME))
    (rundir / "river.slf").write_bytes(b"SELAFIN")
    (rundir / "river.cli").write_text("")
    return Mesh(
        points=points, cells=np.asarray(mesh["ikle"], dtype=np.int64),
        crs_authid=_UTM, bed=None,
        meta={
            "utm_epsg": 32611,
            "lonlat_bbox": (-117.0, 37.0, -116.99, 37.01),
            "domain": {"reach": {"slug": "eel"}, "seed": {"lon": -117.0, "lat": 37.0}},
            "files": {"slf_uri": str(rundir / "river.slf"),
                      "cli_uri": str(rundir / "river.cli"),
                      "topology_uri": str(rundir / STAGED_MESH_FILENAME)},
            "probes": {"domain_mode": "water-polygon", "island_count": 0,
                       "water_coverage_frac": 0.91, "inflow_nodes": mesh["n_in"],
                       "outflow_nodes": mesh["n_out"]},
            "artifact": {"engine_compat": ["telemac"],
                         "provenance": {"extent_km": 0.12, "width_m": _WIDTH_M,
                                        "bank_source": "nhd_area",
                                        "mesh_size_m": 20.0}},
        })


def _edited_layer(mesh: Mesh, path: Path, *, nudge_m: float = 1.0) -> Path:
    """The mesh as a human moved one interior node in QGIS."""
    points = np.array(mesh.points, dtype=float, copy=True)
    points[len(points) // 2, 1] += nudge_m
    path.write_text(write_2dm_arrays(points, np.asarray(mesh.cells),
                                     np.zeros(points.shape[0])))
    return path


def _session(tmp_path: Path, mesh: Mesh) -> MeshSession:
    """A corridor session over an ALREADY BUILT mesh.

    The build runs a triangulator in its box, so the session is handed what that
    box produced rather than running it; every step the test exercises after this
    is the shipped path.
    """
    declaration = tool.build_mesh(
        mesher="corridor_tin", kind="unstructured_tri",
        domain=dict(mesh.meta["domain"]), extent_km=0.12, width_m=_WIDTH_M,
        banks="nhd_area", refine={"edge_length": 20.0})
    session = MeshSession(declaration, workdir=tmp_path / "session")
    session._mesh = mesh
    return session


# --------------------------------------------------------------------------- #
# 1. The edit rewrites what the solve is staged from.
# --------------------------------------------------------------------------- #
def test_a_hand_edited_corridor_regenerates_the_topology_a_solve_adopts(tmp_path):
    before = _corridor_mesh(tmp_path)
    layer = _edited_layer(before, tmp_path / "edited.2dm")

    after = get_mesher("corridor_tin").action("apply_layer_edits").apply(
        before, layer=str(layer))

    files = dict(after.meta["files"])
    assert sorted(files) == ["cli_uri", "slf_uri", "topology_uri"]
    for uri in files.values():
        assert Path(uri).is_file(), uri
    # Every file is the EDITED mesh's, not a carried copy of the pre-edit one.
    assert set(files.values()).isdisjoint(set(dict(before.meta["files"]).values()))

    rewritten = staged_mesh_bundle(str(Path(files["topology_uri"]).parent))
    assert rewritten["npoin"] == after.node_count
    assert rewritten["ikle"].shape[0] == after.element_count
    # The end caps survived a nudge that touched neither of them.
    built = staged_mesh_bundle(str(Path(before.meta["files"]["topology_uri"]).parent))
    assert rewritten["n_in"] == built["n_in"]
    assert rewritten["n_out"] == built["n_out"]
    assert after.meta["probes"]["boundary_role_carry_m"]["max"] <= 1.0001


def test_the_rewritten_cli_ranks_every_boundary_node_in_ipobo_order(tmp_path):
    before = _corridor_mesh(tmp_path)
    after = get_mesher("corridor_tin").action("apply_layer_edits").apply(
        before, layer=str(_edited_layer(before, tmp_path / "edited.2dm")))

    bundle = staged_mesh_bundle(str(Path(after.meta["files"]["topology_uri"]).parent))
    rows = Path(after.meta["files"]["cli_uri"]).read_text().splitlines()
    assert len(rows) == int(bundle["nptfr"])
    ranks = [int(r.split()[-3]) for r in rows]
    assert ranks == list(range(1, len(rows) + 1))
    assert [int(r.split()[-4]) for r in rows] == [int(n) + 1 for n in bundle["ring"]]


def test_a_hand_edit_that_cuts_an_end_cap_off_refuses_at_the_edit(tmp_path):
    """A corridor with no inflow is not a reach a solve can force discharge into."""
    before = _corridor_mesh(tmp_path)
    points = np.array(before.points, dtype=float, copy=True)
    keep = points[:, 0] > points[:, 0].min() + 1e-6
    index = {old: new for new, old in enumerate(np.where(keep)[0])}
    cells = np.asarray([[index[int(n)] for n in tri]
                        for tri in np.asarray(before.cells)
                        if all(keep[int(n)] for n in tri)], dtype=np.int64)
    layer = tmp_path / "cut.2dm"
    layer.write_text(write_2dm_arrays(points[keep], cells,
                                      np.zeros(int(keep.sum()))))

    with pytest.raises(MeshToolError) as excinfo:
        get_mesher("corridor_tin").action("apply_layer_edits").apply(
            before, layer=str(layer))
    assert excinfo.value.error_code == "MESH_CORRIDOR_NO_FLOW_BOUNDARY"


def test_a_mesh_realized_from_an_authoring_bundle_offers_no_hand_edit_at_all():
    """The engine re-realizes those cells; a .2dm is not the inputs it reads."""
    assert "apply_layer_edits" not in get_mesher("hecras_rog").actions


def test_a_bundle_carrying_mesh_refuses_the_hand_edit_at_the_gate(tmp_path):
    """The refusal lands at the EDIT, never at the deck the user already approved."""
    layer = tmp_path / "edited.2dm"
    points, cells = _ribbon(3, 2)
    layer.write_text(write_2dm_arrays(points, cells, np.zeros(points.shape[0])))
    bundled = Mesh(points=points, cells=cells, crs_authid=_UTM,
                   bed=np.zeros(points.shape[0]),
                   meta={"bundle": {"seeds": "/staged/seeds.geojson"}})

    with pytest.raises(MeshToolError) as excinfo:
        get_mesher("coastal_edge").action("apply_layer_edits").apply(
            bundled, layer=str(layer))
    assert excinfo.value.error_code == "MESH_EDIT_NOT_STAGEABLE"


# --------------------------------------------------------------------------- #
# 2. The layer shapes a hand-edit leaves that no boundary walk can follow.
# --------------------------------------------------------------------------- #
#: How long a bounded edit is given to come back. The refusals below are reached
#: without touching the network or a container, so any wait at all is the walk
#: failing to terminate rather than the machine being slow.
_EDIT_TIMEOUT_S = 20.0


def _refusal_from_edit(session: MeshSession, layer: Path) -> BaseException | None:
    """Apply the hand-edit off the calling thread -> what it raised, if anything.

    The edit runs a boundary walk, and an unbounded walk parks the turn that
    demanded it rather than answering wrongly. Driving it on its own thread is what
    turns that into a failing assertion instead of a suite that never ends.
    """
    raised: list[BaseException | None] = []

    def _apply() -> None:
        try:
            session.edit("apply_layer_edits", layer=str(layer))
            raised.append(None)
        except BaseException as exc:  # noqa: BLE001 -- the refusal is the result
            raised.append(exc)

    worker = threading.Thread(target=_apply, daemon=True)
    worker.start()
    worker.join(_EDIT_TIMEOUT_S)
    assert not worker.is_alive(), (
        f"the hand-edit did not return within {_EDIT_TIMEOUT_S:.0f}s: the "
        "boundary walk is unbounded")
    return raised[0]


def _write_layer(path: Path, points, cells) -> Path:
    path.write_text(write_2dm_arrays(points, cells, np.zeros(points.shape[0])))
    return path


def test_a_hand_edit_that_collapses_a_face_refuses_instead_of_walking_forever(
        tmp_path):
    """A triangle whose third vertex repeats its second is QGIS's own output.

    Dragging a vertex onto its neighbour leaves the layer a triangle with two
    identical corners. Its collapsed edge is then walked twice by one face, which
    leaves the boundary a cycle that never returns to where the walk began.
    """
    built = _corridor_mesh(tmp_path)
    session = _session(tmp_path, built)
    points = np.asarray(session.mesh.points, dtype=float)
    cells = np.array(session.mesh.cells, dtype=np.int64, copy=True)
    # The second-row, first triangle: its collapse is the one that leaves a walk
    # with somewhere to go and nowhere to close.
    cells[12] = (cells[12][0], cells[12][1], cells[12][1])
    layer = _write_layer(tmp_path / "collapsed.2dm", points, cells)

    raised = _refusal_from_edit(session, layer)

    assert isinstance(raised, MeshToolError), raised
    assert raised.error_code == "MESH_CORRIDOR_NON_MANIFOLD"


def test_a_hand_edit_that_duplicates_a_face_refuses_by_name(tmp_path):
    """One triangle listed twice: a copy-paste in the layer, not a new element.

    The duplicate lands a third face on each of its edges, so the boundary edge it
    covered stops being one and the walk runs off the end of a ring that no longer
    closes.
    """
    built = _corridor_mesh(tmp_path)
    session = _session(tmp_path, built)
    points = np.asarray(session.mesh.points, dtype=float)
    cells = np.asarray(session.mesh.cells, dtype=np.int64)
    layer = _write_layer(tmp_path / "duplicated.2dm", points,
                         np.vstack([cells, cells[0][None, :]]))

    raised = _refusal_from_edit(session, layer)

    assert isinstance(raised, MeshToolError), raised
    assert raised.error_code == "MESH_CORRIDOR_NON_MANIFOLD"


# --------------------------------------------------------------------------- #
# 3. Accept, then stage the deck: the repro end to end.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_hand_edited_corridor_is_accepted_and_the_deck_stages_it(
        tmp_path, monkeypatch):
    from trid3nt_server.workflows.telemac import release_layer as rel_mod
    from trid3nt_server.workflows.telemac.steps import deck as deck_mod

    async def _river(**_kw):
        return {"inputs": [{"gs_uri": "s3://c/c.geojson",
                            "dest": "river_centerline.geojson"}],
                "provenance": {"seed_lon": -117.0, "seed_lat": 37.0,
                               "seed_rung": "position-named-flowline",
                               "centerline_sha256": "0" * 64,
                               "centerline_comids": [1],
                               "bed_source": "cop-dem-glo-30"}}

    async def _publish(*_a, **_kw):
        return False

    monkeypatch.setattr(deck_mod, "resolve_reach_river", _river)
    monkeypatch.setattr(rel_mod, "publish_release_point", _publish)

    built = _corridor_mesh(tmp_path)
    session = _session(tmp_path, built)
    session.edit("apply_layer_edits",
                 layer=str(_edited_layer(built, tmp_path / "edited.2dm")))
    art = session.accept()

    assert art.topology_uri and Path(art.topology_uri).is_file()
    assert art.engine_compat == ["telemac"]

    out = await deck_mod.write_reach_deck(
        reach={"name": "Eel River", "slug": "eel"},
        seed={"lon": -117.0, "lat": 37.0, "source": "flowline"},
        mesh={"artifact": art, "mesh_id": art.mesh_id, "slf_uri": art.slf_uri,
              "cli_uri": art.cli_uri, "topology_uri": art.topology_uri},
        carrier_discharge={"m3s": 12.0}, substance="dye")
    assert {"gs_uri": art.topology_uri, "dest": "river_mesh.npz"} in out["inputs"]


def test_the_hand_edit_is_recorded_as_the_non_replayable_edit_it_is(tmp_path):
    built = _corridor_mesh(tmp_path)
    session = _session(tmp_path, built)
    session.edit("apply_layer_edits",
                 layer=str(_edited_layer(built, tmp_path / "edited.2dm")))

    line = [json.loads(ln) for ln in
            session.recipe_path.read_text().splitlines() if ln.strip()][-1]
    assert line["edit"] == "apply_layer_edits"
    assert line["layer"].startswith("sha256:")
    assert line["replayable"] is False


def test_a_bed_less_corridor_is_telemac_compatible_because_the_deck_fits_the_bed(
        tmp_path):
    from trid3nt_server.workflows.mesh.artifact import mesh_compatible_with_engine

    session = _session(tmp_path, _corridor_mesh(tmp_path))
    art = session.accept()

    assert art.has_bathymetry is False
    ok, reason = mesh_compatible_with_engine(art, "telemac")
    assert ok, reason
    # Without the accepted topology the same bed-less mesh is NOT solvable.
    art.topology_uri = None
    assert mesh_compatible_with_engine(art, "telemac")[0] is False


# --------------------------------------------------------------------------- #
# 4. The worker's half of the hand-off.
# --------------------------------------------------------------------------- #
def test_the_staged_bundle_round_trips_every_key_the_solve_reads(tmp_path):
    points, cells = _ribbon()
    mesh = _built_bundle(points, cells)
    write_mesh_bundle(mesh, str(tmp_path / STAGED_MESH_FILENAME))

    read = staged_mesh_bundle(str(tmp_path))
    assert sorted(read) == sorted(_BUNDLE_KEYS)
    for key in ("X", "Y", "ikle", "ring", "ipob", "lihbor", "liubor", "livbor",
                "litbor", "centerline"):
        assert np.array_equal(np.asarray(read[key]), np.asarray(mesh[key])), key
    assert list(read["cls"]) == list(mesh["cls"])
    assert read["in_nodes"] == mesh["in_nodes"]
    assert read["out_nodes"] == mesh["out_nodes"]
    assert [list(r) for r in read["boundary_rings"]] == \
        [list(r) for r in mesh["boundary_rings"]]
    for key in ("npoin", "nptfr", "n_in", "n_out", "n_islands", "domain_mode",
                "water_coverage_frac", "banks_ok", "smooth_tries"):
        assert read[key] == mesh[key], key


def test_a_bundle_disagreeing_with_its_own_node_count_refuses(tmp_path):
    points, cells = _ribbon()
    mesh = _built_bundle(points, cells)
    mesh["npoin"] = int(mesh["npoin"]) + 1
    write_mesh_bundle(mesh, str(tmp_path / STAGED_MESH_FILENAME))

    with pytest.raises(StagedMeshUnusableError) as excinfo:
        staged_mesh_bundle(str(tmp_path))
    assert "not a mesh this run can solve on" in str(excinfo.value)


def test_no_staged_bundle_reads_as_none_so_the_run_meshes_for_itself(tmp_path):
    assert staged_mesh_bundle(str(tmp_path)) is None
