"""The bed slot at the mesh op, and boundary runs as what prescribes a role.

Offline: rasters are written to disk here, and what is proved is which source
painted which node and what a stated depth and a set of runs impose.
"""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.inputs.boundary import BoundaryRun
from trid3nt_server.inputs.point import Point
from trid3nt_server.tools.mesh.meshers import Mesh, MeshToolError
from trid3nt_server.tools.mesh.shared import primitives as P


def _lattice_mesh() -> Mesh:
    """A 3x3 lon/lat node lattice, two triangles per square, one boundary loop."""
    xy = np.array([[x, y] for y in (36.12, 36.13, 36.14)
                   for x in (-75.78, -75.77, -75.76)])
    cells = []
    for row in range(2):
        for col in range(2):
            a = row * 3 + col
            cells += [[a, a + 1, a + 4], [a, a + 4, a + 3]]
    return Mesh(points=xy, cells=np.asarray(cells, dtype=np.int64),
                crs_authid="EPSG:4326")


def _raster(path, values, *, west=-75.79, south=36.11, cell=0.01, nodata=None):
    """A small EPSG:4326 raster carrying ``values`` row by row, north-up."""
    import rasterio
    from rasterio.transform import from_origin

    array = np.asarray(values, dtype="float32")
    north = south + cell * array.shape[0]
    with rasterio.open(
            path, "w", driver="GTiff", height=array.shape[0],
            width=array.shape[1], count=1, dtype="float32", crs="EPSG:4326",
            nodata=nodata, transform=from_origin(west, north, cell, cell)) as dst:
        dst.write(array, 1)
    return str(path)


def test_a_stated_depth_is_a_flat_bed_below_the_free_surface():
    """A pond the user says is two metres deep needs no survey at all."""
    bedded = P.set_bed(_lattice_mesh(), 2.0)
    assert list(np.unique(bedded.bed)) == [-2.0]
    assert "stated depth 2 m" in bedded.meta["bed_source"]
    assert bedded.meta["bed_sources"] == [
        "stated depth 2 m below the free surface"]


def _written_under(monkeypatch, tmp_path):
    """Every raster the bed slot writes lands under the test's own directory."""
    from trid3nt_server.tools.derive import _hydrology_common as common

    real = common.write_cog
    monkeypatch.setattr(common, "write_cog", lambda band, **kw: real(
        band, **{**kw, "output_dir": str(tmp_path)}))


def _laid(layers):
    """The bed slot's own lay of an ordered list -> the one layer it hands on."""
    from trid3nt_server.inputs.bed import bed

    return bed(layers, frame="NAVD88").source


def test_the_bed_is_one_source_and_two_are_laid_by_the_slot_before_the_op(
        tmp_path, monkeypatch):
    """A channel survey where it was flown and the surface elsewhere is ONE bed,
    laid by the bed slot from the ordered list it was handed; the op takes that layer."""
    from trid3nt_contracts.execution import LayerURI

    _written_under(monkeypatch, tmp_path)

    survey = np.full((5, 5), np.nan, dtype="float32")
    survey[:, :2] = -9.0

    def _layer(path: str) -> LayerURI:
        return LayerURI(layer_id="x", name=path, layer_type="raster", uri=path,
                        vertical_datum="NAVD88")

    laid = _laid(
        [_layer(_raster(tmp_path / "survey.tif", survey, nodata=np.nan)),
         _layer(_raster(tmp_path / "dem.tif", np.full((5, 5), 3.0),
                        nodata=-9999.0))])
    bedded = P.set_bed(_lattice_mesh(), laid)
    painted = sorted({round(float(v), 1) for v in bedded.bed})
    assert painted == [-9.0, 3.0]
    assert all(share > 0.0 for _label, share in laid.coverage[:2])
    assert "bed_laid" in bedded.meta["bed_sources"][0]


def test_the_journal_states_the_share_of_nodes_each_input_painted_off_band_2(
        tmp_path, monkeypatch):
    """The per-node shares of a laid bed are read off its own band 2 and named by
    the surfaces it lists, so the journal says which painted what."""
    from trid3nt_server.inputs import run_notes
    from trid3nt_contracts.execution import LayerURI

    _written_under(monkeypatch, tmp_path)

    said: list[str] = []
    monkeypatch.setattr(run_notes, "journal_note", said.append)
    survey = np.full((5, 5), np.nan, dtype="float32")
    survey[:, :2] = -9.0

    def _layer(path: str, name: str) -> LayerURI:
        return LayerURI(layer_id=name, name=name, layer_type="raster", uri=path,
                        vertical_datum="NAVD88")

    laid = _laid(
        [_layer(_raster(tmp_path / "survey.tif", survey, nodata=np.nan), "survey"),
         _layer(_raster(tmp_path / "dem.tif", np.full((5, 5), 3.0),
                        nodata=-9999.0), "dem")])
    P.set_bed(_lattice_mesh(), laid)
    shares = [line for line in said if line.startswith("bed: ")]
    assert len(shares) == 1
    assert "survey reached 33.3% of them" in shares[0]
    assert "dem reached 66.7% of them" in shares[0]


def test_a_domain_no_source_covers_refuses_by_name(tmp_path):
    """Filling the holes with the mean of what WAS covered is a bed nobody
    measured, so the op says how many nodes have nothing."""
    empty = _raster(tmp_path / "hole.tif",
                    np.full((5, 5), np.nan, dtype="float32"), nodata=np.nan)
    with pytest.raises(MeshToolError) as excinfo:
        P.set_bed(_lattice_mesh(), empty)
    assert excinfo.value.error_code == "MESH_BED_UNPAINTED"
    assert "9 of 9 nodes" in str(excinfo.value)


def test_a_node_no_row_reached_refuses_with_the_feedback_and_never_a_zero(
        tmp_path, monkeypatch):
    """A node standing on a cell nothing measured has no elevation, and a number
    put there on its behalf is a bed nobody surveyed: the refusal says how many,
    the box they stand in, and what the surface itself states about its own
    coverage."""
    from trid3nt_contracts.execution import LayerURI

    _written_under(monkeypatch, tmp_path)
    half = np.full((5, 5), np.nan, dtype="float32")
    half[:, :2] = -9.0
    laid = _laid(
        [LayerURI(layer_id="x", name="survey", layer_type="raster",
                  uri=_raster(tmp_path / "half.tif", half, nodata=np.nan),
                  vertical_datum="NAVD88"),
         LayerURI(layer_id="y", name="nothing", layer_type="raster",
                  uri=_raster(tmp_path / "none.tif",
                              np.full((5, 5), np.nan, dtype="float32"), nodata=np.nan),
                  vertical_datum="NAVD88")])
    with pytest.raises(MeshToolError) as excinfo:
        P.set_bed(_lattice_mesh(), laid)
    said = str(excinfo.value)
    assert excinfo.value.error_code == "MESH_BED_UNPAINTED"
    assert "of 9 nodes have no elevation" in said
    assert "-75.7" in said and "36.1" in said
    assert "Over the grid survey" in said and "FILL_MAX_DISTANCE" in said
    assert "0.0" not in said.split("nodes have no elevation")[0]


def test_boundary_runs_prescribe_the_roles_their_types_name():
    inflow = BoundaryRun(Point(-75.78, 36.12), Point(-75.78, 36.14),
                         type="inflow")
    roled = P.set_boundary_roles(_lattice_mesh(), runs=[inflow])
    assert set(roled.meta["boundary_roles"]["inflow"]) == {0, 3, 6}


def test_a_closed_body_states_no_run_and_the_mesh_has_only_walls():
    mesh = _lattice_mesh()
    assert P.set_boundary_roles(mesh, runs=()) is mesh
    walls = [BoundaryRun(Point(-75.78, 36.12), Point(-75.78, 36.14))]
    assert P.set_boundary_roles(mesh, runs=walls) is mesh


def test_runs_and_named_faces_are_the_same_statement():
    """A producer's runs and a template's named face both land as faces of the
    role they name, so the two can be declared side by side."""
    east = {"type": "LineString",
            "coordinates": [[-75.76, 36.12], [-75.76, 36.14]]}
    runs = [BoundaryRun(Point(-75.78, 36.12), Point(-75.78, 36.14),
                        type="inflow")]
    roled = P.set_boundary_roles(_lattice_mesh(), runs=runs, outflow=east)
    assert set(roled.meta["boundary_roles"]["inflow"]) == {0, 3, 6}
    assert set(roled.meta["boundary_roles"]["outflow"]) == {2, 5, 8}


def test_the_nodes_a_source_may_enter_the_water_at_are_the_interior_ones():
    """The mesh's own edge is where boundary conditions are imposed, and a point
    source placed there is one the solver reads as outside its own domain."""
    import numpy as np

    from trid3nt_server.tools.mesh.shared.nodes import interior_nodes

    # a 3x3 lattice cut into triangles: the middle node is the only inside
    nodes = [[i, j] for i in range(3) for j in range(3)]
    cells = []
    for i in range(2):
        for j in range(2):
            a = i * 3 + j
            cells += [[a, a + 3, a + 1], [a + 1, a + 3, a + 4]]
    mask = interior_nodes(np.asarray(cells, dtype=np.int64), len(nodes))
    assert mask.tolist() == [n == 4 for n in range(9)]
