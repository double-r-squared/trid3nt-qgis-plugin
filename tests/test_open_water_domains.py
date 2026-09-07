"""The two rules a water domain is built under, and the one number on a structure.

Both were measured on live acceptance arms that would not solve: a lone solid
node inside the designated liquid stretch stopped ARTEMIS in FRONT2, and a basin
meshed over water nobody had sounded carried a bed the survey never gave it.
"""

from __future__ import annotations

import numpy as np
import pytest


# -- the structure: ONE number ------------------------------------------------ #

def test_the_footprint_and_the_solid_faces_are_cut_at_the_same_width():
    """The mesher removes water inside HALF the declared width of the centreline,
    and the deck calls solid exactly what stands on what it removed. Two numbers
    here would let the deck stamp a face on water the cut left behind."""
    from trid3nt_server.tools import TOOL_REGISTRY

    door = TOOL_REGISTRY["artemis_harbor_agitation"].fn.workflow.plan_decl
    barrier = next(step for step in door.domain if step.label == "barrier")
    # A ref refuses to compare itself at plan-construction time, which is what
    # keeps a description from being read as a value; the NAME is the statement.
    assert barrier.kwargs["width_m"].name == "barrier_width_m"
    assert door.settle.kwargs["structure_width_m"].name == "barrier_width_m"


def test_a_boundary_node_is_on_the_structure_when_it_stands_on_the_punched_outline():
    """The water inside the footprint was removed, so a boundary node can only be
    within the half-width if it stands on the cut. A band wider than the cut
    reaches into open water and calls it solid."""
    from trid3nt_server.workflows.telemac.authoring.assembler import _nodes_near

    # one 200 m centreline segment, cut 20 m wide: the outline runs at 10 m.
    segments = [[0.0, 0.0, 200.0, 0.0]]
    points = np.array([[100.0, 10.0],     # on the outline
                       [100.0, -9.5],     # on the outline, the other face
                       [100.0, 26.0],     # open water an element edge away
                       [400.0, 0.0]])     # past the end of the structure
    assert _nodes_near(segments, points, [0, 1, 2, 3], 10.0) == [0, 1]
    # the band the arm used to run under - 1.5 element edges at a 25 m mesh -
    # swallows the open-water node, which is the FRONT2 refusal.
    assert 2 in _nodes_near(segments, points, [0, 1, 2, 3], 37.5)


# -- the basin: clipped to what was measured ---------------------------------- #

def _bed_raster(tmp_path, west_only: bool):
    """A 1-degree bed whose WEST half is sounded and whose east half is nodata."""
    import rasterio
    from rasterio.transform import from_origin

    values = np.full((20, 20), -5.0, dtype="float32")
    values[:, 10:] = np.nan if west_only else -5.0
    path = tmp_path / "bed.tif"
    with rasterio.open(
            path, "w", driver="GTiff", height=20, width=20, count=1,
            dtype="float32", crs="EPSG:4326", nodata=np.nan,
            transform=from_origin(-1.0, 1.0, 0.1, 0.1)) as dst:
        dst.write(values, 1)
    return str(path)


def _water(minx, maxx):
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[
            [minx, -0.5], [maxx, -0.5], [maxx, 0.5], [minx, 0.5],
            [minx, -0.5]]]}}]}


def test_the_basin_is_the_water_the_survey_actually_sounded(tmp_path):
    """Half the mapped water is nodata on the bed raster, and the domain that
    leaves is the sounded half - measured off the raster's MASK, so a cell
    sounded at zero and a cell nobody visited are told apart."""
    from trid3nt_server.workflows.telemac.templates.stratified_flow.measured_bed import (
        _clip,
    )

    clipped = _clip(_water(-1.0, 1.0), _bed_raster(tmp_path, west_only=True))
    assert clipped["mapped_area_km2"] == pytest.approx(
        2.0 * clipped["area_km2"], rel=0.02)
    assert "the survey did not sound" in clipped["note"]
    bounds = clipped["domain"]["features"][0]["geometry"]["coordinates"][0]
    assert max(lon for lon, _lat in bounds) == pytest.approx(0.0, abs=1e-9)


def test_water_the_survey_never_reached_refuses_rather_than_meshing(tmp_path):
    from trid3nt_server.workflows.telemac.helpers.errors import OpenWaterError
    from trid3nt_server.workflows.telemac.templates.stratified_flow.measured_bed import (
        _clip,
    )

    with pytest.raises(OpenWaterError) as excinfo:
        _clip(_water(2.0, 3.0), _bed_raster(tmp_path, west_only=False))
    assert excinfo.value.error_code == "TELEMAC3D_BED_DOES_NOT_REACH"


def test_the_basin_reads_the_same_survey_it_was_clipped_to(tmp_path):
    """One fetch, two readers: a clip against one survey and a paint from another
    would put nodes where the clip said nothing was measured."""
    from trid3nt_server.workflows.telemac.templates.stratified_flow.stratified_flow import (
        DATA,
        MESH,
    )
    from trid3nt_server.tools import TOOL_REGISTRY

    door = TOOL_REGISTRY["telemac3d_stratified_flow"].fn.workflow.plan_decl
    basin = next(step for step in door.domain if step.label == "basin")
    set_bed = next(op for op in MESH.ops if op.fn == "set_bed")
    assert repr(basin.kwargs["bed"]) == repr(DATA.bed) == "DataRef('bed')"
    assert repr(set_bed.kwargs["source"]) == repr(DATA.bed)
