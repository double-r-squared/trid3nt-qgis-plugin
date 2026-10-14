"""The mesh front lift: the recipe's build field and a ragged cell set.

The two changes that make one front serve a second mesher family: a recipe may
carry the backend CONSTRUCTOR's own kwargs (checked against the mesher's
declared roster, recorded only when declared so existing content keys stand),
and a mesh's cells may vary in vertex count - measured edge over edge, given a
UGRID face, digested canonically. A uniform triangle or quad mesh is untouched
end to end."""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.render.mesh_display import write_2dm
from trid3nt_server.tools.mesh.meshers import (
    Mesh,
    MeshToolError,
    _MESHERS,
    register_mesher,
)
from trid3nt_server.tools.mesh.recipe import (
    MeshRecipe,
    build_recipe,
    recipe_from_plan_value,
    recipe_plan_value,
)
from trid3nt_server.tools.mesh.session import (
    MeshSession,
    _probes,
    mesh_digest,
    replay_recipe,
)
from trid3nt_server.tools.mesh.step import mesh_key
from trid3nt_server.tools.mesh.tool import MeshTool

_AOI = (-83.5, 35.0, -83.4, 35.09)


def _reg_recipe(**over):
    return MeshTool.build_mesh(mesher="reg_grid", kind="structured_grid",
                               extent=_AOI, resolution_m=2000.0, **over)


def _ragged(triangles: int = 3) -> Mesh:
    """A fan of triangles around a hub, capped by one polygon of any arity."""
    hub = 0
    ring = np.arange(1, triangles + 2)
    pts = np.column_stack([
        np.concatenate([[0.0], np.cos(np.linspace(0, 2 * np.pi, triangles + 1,
                                                  endpoint=False))]),
        np.concatenate([[0.0], np.sin(np.linspace(0, 2 * np.pi, triangles + 1,
                                                  endpoint=False))]),
    ])
    cells = [np.array([hub, int(r), int(ring[(i + 1) % len(ring)])])
             for i, r in enumerate(ring)]
    return Mesh(points=pts, cells=cells, crs_authid="EPSG:32617",
                bed=np.linspace(10.0, 12.0, pts.shape[0]),
                meta={"utm_epsg": 32617,
                      "lonlat_bbox": (-83.5, 35.0, -83.4, 35.09)})


def _mixed_ragged() -> Mesh:
    """Triangles beside a pentagon: no single vertex count, so no min angle."""
    pts = np.array([[0.0, 0.0], [1, 0], [2, 0], [3, 0], [4, 0], [4, 1],
                    [2, 2], [0, 1]], dtype=float)
    cells = [np.array([0, 1, 7]), np.array([1, 2, 6, 7]),
             np.array([2, 3, 4, 5, 6])]
    return Mesh(points=pts, cells=cells, crs_authid="EPSG:32617",
                meta={"utm_epsg": 32617,
                      "lonlat_bbox": (-83.5, 35.0, -83.4, 35.09)})


class _StubMesher:
    @staticmethod
    def build(recipe):
        raise AssertionError("the stub never builds")


def _stub(name: str, **over):
    """A registered stub mesher; the caller pops it when done."""
    return register_mesher(name, _StubMesher.build,
                           kinds=over.pop("kinds", ("voronoi",)), **over)


def test_build_kwargs_on_a_constructor_that_takes_none_refuse_by_name():
    with pytest.raises(MeshToolError) as excinfo:
        MeshTool.build_mesh(mesher="om2d", kind="unstructured_tri",
                            extent=_AOI, build={"maximum_area": 0.5})
    assert excinfo.value.error_code == "MESH_BUILD_UNKNOWN_PARAM"
    assert "maximum_area" in str(excinfo.value)


def test_build_kwargs_are_checked_against_the_declared_roster():
    mesher = _stub("test_flopy_stub2",
                   build_params=("maximum_area", "angle"))
    try:
        recipe = MeshTool.build_mesh(
            mesher=mesher.name, extent=_AOI,
            build={"maximum_area": 0.5, "angle": 33.0})
        assert dict(recipe.build) == {"maximum_area": 0.5, "angle": 33.0}
        with pytest.raises(MeshToolError) as excinfo:
            MeshTool.build_mesh(mesher=mesher.name, extent=_AOI,
                                build={"bogus": 1})
        assert excinfo.value.error_code == "MESH_BUILD_UNKNOWN_PARAM"
        assert "bogus" in str(excinfo.value)
    finally:
        _MESHERS.pop(mesher.name, None)


def test_an_unrecordable_build_value_refuses():
    mesher = _stub("test_flopy_stub_bad",
                   build_params=("maximum_area",))
    try:
        with pytest.raises(MeshToolError) as excinfo:
            build_recipe(mesher=mesher.name, extent=_AOI,
                         build={"maximum_area": object()})
        assert excinfo.value.error_code == "MESH_RECIPE_UNSERIALIZABLE"
    finally:
        _MESHERS.pop(mesher.name, None)


def test_an_undeclared_build_is_absent_from_json_plan_and_content_key():
    """A recipe without a build is byte-identical to one written before the
    field existed, so its content key stands."""
    recipe = _reg_recipe()
    assert "build" not in recipe.to_json()
    plan = recipe_plan_value(recipe)
    assert "build" not in plan
    legacy = {"mesher": recipe.mesher, "kind": recipe.kind,
              "extent": list(recipe.extent),
              "resolution_m": recipe.resolution_m, "ops": []}
    assert mesh_key(plan) == mesh_key(legacy)


def test_a_declared_build_flows_json_plan_and_key_round_trip():
    mesher = _stub("test_flopy_stub3", kinds=("quadtree",),
                   build_params=("delr", "delc"))
    try:
        recipe = MeshTool.build_mesh(mesher=mesher.name, extent=_AOI,
                                     build={"delr": [100.0], "delc": [80.0]})
        doc = recipe.to_json()
        assert doc["build"] == {"delr": [100.0], "delc": [80.0]}
        assert MeshRecipe.from_json(doc).to_json() == doc
        plan = recipe_plan_value(recipe)
        assert plan["build"] == {"delr": [100.0], "delc": [80.0]}
        again = recipe_from_plan_value(plan)
        assert again.to_json() == doc
        other = recipe_from_plan_value({**plan, "build": {"delr": [50.0]}})
        assert mesh_key(recipe_plan_value(other)) != mesh_key(plan)
    finally:
        _MESHERS.pop(mesher.name, None)


def test_set_params_still_refuses_build_as_an_agnostic_param():
    with pytest.raises(MeshToolError) as excinfo:
        _reg_recipe().with_params(build={"x": 1})
    assert excinfo.value.error_code == "MESH_RECIPE_UNKNOWN_PARAM"


def test_ragged_cells_count_and_probe_edge_over_edge():
    mesh = _mixed_ragged()
    assert mesh.ragged and mesh.element_count == 3
    assert mesh.nodes_per_cell == 0
    facts = _probes(mesh, _reg_recipe())
    assert facts["element_count"] == 3
    assert facts["edge_length_m"]["min"] > 0
    assert "min_angle_deg" not in facts, (
        "a mixed-vertex mesh states no comparable minimum angle")
    assert facts["boundary_edges"] > 0


def test_an_all_triangle_ragged_mesh_still_reports_its_min_angle():
    facts = _probes(_ragged(4), _reg_recipe())
    assert facts["min_angle_deg"] > 0
    assert facts["element_count"] == 5


def test_a_uniform_mesh_probes_exactly_as_before():
    pts = np.array([[500000.0, 3880000.0], [500100.0, 3880000.0],
                    [500000.0, 3880100.0], [500100.0, 3880100.0]])
    cells = np.array([[0, 1, 2], [1, 3, 2]])
    mesh = Mesh(points=pts, cells=cells, crs_authid="EPSG:32616",
                meta={"utm_epsg": 32616,
                      "lonlat_bbox": (-83.5, 35.0, -83.4, 35.09)})
    facts = _probes(mesh, _reg_recipe())
    assert facts["min_angle_deg"] > 0
    assert facts["boundary_edges"] == 4
    assert facts["boundary_loops"] == 1


def test_a_ragged_mesh_display_face_is_ugrid_and_a_uniform_one_keeps_2dm(tmp_path):
    ragged = MeshSession(_reg_recipe(), workdir=tmp_path / "ragged")
    ragged._mesh = _mixed_ragged()
    face, _ = ragged._display_face()
    assert face.name == "mesh.ugrid"
    import netCDF4

    with netCDF4.Dataset(face) as ds:
        assert ds.variables["Mesh2D"].getncattr("cf_role") == "topology_data"
        assert ds.variables["Mesh2D_face_nodes"].shape == (5, 3)

    uniform = MeshSession(_reg_recipe(), workdir=tmp_path / "uniform")
    uniform._mesh = Mesh(
        points=np.array([[0.0, 0.0], [1, 0], [0, 1]]),
        cells=np.array([[0, 1, 2]]), crs_authid="EPSG:32616",
        meta={"utm_epsg": 32616,
              "lonlat_bbox": (-83.5, 35.0, -83.4, 35.09)})
    face, _ = uniform._display_face()
    assert face.name == "mesh.2dm"
    assert face.read_text().startswith("MESH2D")


def test_a_ragged_mesh_digest_is_canonical_and_permutation_sensitive():
    mesh = _mixed_ragged()
    same = _mixed_ragged()
    assert mesh_digest(mesh) == mesh_digest(same)
    swapped = _mixed_ragged()
    object.__setattr__(swapped, "cells", [swapped.cells[2], swapped.cells[1],
                                          swapped.cells[0]])
    assert mesh_digest(mesh) != mesh_digest(swapped)
    # A uniform mesh still digests over its .2dm face, byte for byte.
    uniform = Mesh(points=np.array([[0.0, 0.0], [1, 0], [0, 1]]),
                   cells=np.array([[0, 1, 2]]), crs_authid="EPSG:32616")
    assert mesh_digest(uniform) == (
        "sha256:" + __import__("hashlib").sha256(
            write_2dm(uniform).encode()).hexdigest())


def test_a_ragged_mesh_replays_from_its_journal(tmp_path):
    """The journal records the recipe; a replay REBUILDS through the mesher,
    so a ragged mesh that a mesher produced comes back the same. Here the
    mesher is stubbed to return the ragged mesh verbatim."""
    recipe = _reg_recipe()
    session = MeshSession(recipe, workdir=tmp_path)
    ragged = _mixed_ragged()
    session._mesh = ragged
    session._journal()
    lines = session.recipe_path.read_text()
    assert "recipe" in lines
    # A hand-edit onto a ragged mesh still refuses to replay.
    session.regen_note = "edited"
    session._events.append({"event": "adopt", "source": "hand.2dm",
                            "digest": "sha256:0"})
    session._journal()
    with pytest.raises(MeshToolError) as excinfo:
        replay_recipe(session.recipe_path)
    assert excinfo.value.error_code == "MESH_RECIPE_NOT_REPLAYABLE"
