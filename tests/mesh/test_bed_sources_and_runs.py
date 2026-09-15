"""The bed slot at the mesh op, and boundary runs as what prescribes a role.

Offline: rasters are written to disk here, and what is proved is which source
painted which node and what a stated depth and a set of runs impose.
"""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.inputs.boundary import BoundaryRun
from trid3nt_server.inputs.point import Point
from trid3nt_server.workflows.mesh.meshers import Mesh, MeshToolError
from trid3nt_server.workflows.mesh.shared import primitives as P


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


def test_the_bed_is_one_source_and_two_are_merged_before_the_op(tmp_path):
    """A channel survey where it was flown and the surface elsewhere is ONE bed,
    composed by the merge derive; the op takes the layer that came out of it."""
    from trid3nt_contracts.execution import LayerURI
    from trid3nt_server.tools.derive.derive_merge_rasters.derive_merge_rasters import (
        derive_merge_rasters,
    )

    survey = np.full((5, 5), np.nan, dtype="float32")
    survey[:, :2] = -9.0

    def _layer(path: str) -> LayerURI:
        return LayerURI(layer_id="x", name=path, layer_type="raster", uri=path,
                        vertical_datum="NAVD88")

    merged = derive_merge_rasters(
        primary=_layer(_raster(tmp_path / "survey.tif", survey, nodata=np.nan)),
        fallback=_layer(_raster(tmp_path / "dem.tif",
                                np.full((5, 5), 3.0), nodata=-9999.0)),
        _output_dir=str(tmp_path))
    bedded = P.set_bed(_lattice_mesh(), merged)
    painted = sorted({round(float(v), 1) for v in bedded.bed})
    assert painted == [-9.0, 3.0]
    assert merged.primary_fraction > 0.0 and merged.fallback_fraction > 0.0
    assert bedded.meta["bed_sources"][0].endswith("merged_bed.tif") or \
        "merged_bed" in bedded.meta["bed_sources"][0]


def test_a_domain_no_source_covers_refuses_by_name(tmp_path):
    """Filling the holes with the mean of what WAS covered is a bed nobody
    measured, so the op says how many nodes have nothing."""
    empty = _raster(tmp_path / "hole.tif",
                    np.full((5, 5), np.nan, dtype="float32"), nodata=np.nan)
    with pytest.raises(MeshToolError) as excinfo:
        P.set_bed(_lattice_mesh(), empty)
    assert excinfo.value.error_code == "MESH_BED_UNPAINTED"
    assert "9 of 9 nodes" in str(excinfo.value)


def test_a_layer_of_soundings_goes_through_the_derive_at_the_meshs_own_scale(
        monkeypatch, tmp_path):
    """A slot calls a derive BY NAME, and asks it for the scale the ELEMENTS
    were sized for: a surface finer than them buys nothing."""
    from trid3nt_server.inputs.bed import SURVEY_DERIVE
    from trid3nt_server.tools import TOOL_REGISTRY

    asked: dict = {}
    surface = _raster(tmp_path / "surface.tif", np.full((5, 5), -4.0))

    class _Entry:
        @staticmethod
        def fn(**kwargs):
            asked.update(kwargs)
            return surface

    monkeypatch.setitem(TOOL_REGISTRY, SURVEY_DERIVE, _Entry)
    soundings = {"type": "FeatureCollection", "features": []}
    bedded = P.set_bed(_lattice_mesh(), soundings)
    assert asked["points"] is soundings
    # the lattice's own edges are about 900 m on the ground
    assert 500.0 < asked["resolution_m"] < 1500.0
    assert list(np.unique(bedded.bed)) == [-4.0]
    assert SURVEY_DERIVE in bedded.meta["bed_source"]


def test_a_tree_without_the_soundings_derive_says_which_one_is_missing(
        monkeypatch):
    from trid3nt_server.inputs.bed import SURVEY_DERIVE
    from trid3nt_server.tools import TOOL_REGISTRY

    monkeypatch.delitem(TOOL_REGISTRY, SURVEY_DERIVE, raising=False)
    with pytest.raises(MeshToolError) as excinfo:
        P.set_bed(_lattice_mesh(),
                  {"type": "FeatureCollection", "features": []})
    assert excinfo.value.error_code == "MESH_BED_SURVEY_UNINTERPOLATED"
    assert SURVEY_DERIVE in str(excinfo.value)


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
