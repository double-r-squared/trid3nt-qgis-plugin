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
    """The water inside the footprint was removed, so a boundary node is on the cut when
    it stands at the half-width, to the precision a relaxation places a node on a
    locked outline. An equality at exactly 10.000 m would halve one population."""
    from trid3nt_server.workflows.telemac.authoring.assembler import _nodes_near

    # one 200 m centreline segment, cut 20 m wide on a 5 m mesh: the outline runs
    # at 10 m and a node sits on it to within an edge.
    band = 20.0 / 2.0 + 5.0
    segments = [[0.0, 0.0, 200.0, 0.0]]
    points = np.array([[100.0, 10.4],     # on the outline, an edge's slack out
                       [100.0, -9.6],     # on it, the other face
                       [100.0, 40.0],     # open water well off the cut
                       [400.0, 0.0]])     # past the end of the structure
    assert _nodes_near(segments, points, [0, 1, 2, 3], band) == [0, 1]
    assert _nodes_near(segments, points, [0, 1, 2, 3], 10.0) == [1]


def test_a_lone_node_between_two_of_another_kind_is_not_a_face():
    """front2.f refuses "a solid point between two liquid points" and the reverse
    by name, so the walk settles them: a role is a RUN, and a single node whose
    two neighbours agree with each other is theirs."""
    from trid3nt_server.workflows.telemac.authoring.assembler import _settled_walk

    walk = [10, 11, 12, 13, 14, 15, 16, 17]
    structure, liquid = _settled_walk(
        walk, structure={10, 11, 13, 14}, liquid={12, 15, 16, 17})
    assert structure == [10, 11, 12, 13, 14]      # the hole at 12 closes
    assert liquid == [15, 16, 17]
    # and a lone structure node inside a liquid run goes the other way.
    structure, liquid = _settled_walk(
        walk, structure={13}, liquid={10, 11, 12, 14, 15, 16, 17})
    assert structure == []


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
    from trid3nt_server.workflows.telemac.errors import TelemacError
    from trid3nt_server.workflows.telemac.templates.stratified_flow.measured_bed import (
        _clip,
    )

    with pytest.raises(TelemacError) as excinfo:
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


# -- the basin: the level it opens at ----------------------------------------- #

def _gauges(tmp_path, rows):
    """A gauge layer as the CO-OPS fetch returns one: points carrying a series."""
    import geopandas as gpd
    from shapely.geometry import Point

    path = tmp_path / "gauges.fgb"
    gpd.GeoDataFrame(
        [{"station_id": sid, "station_name": name,
          "time_series_csv": series} for sid, name, _lon, _lat, series in rows],
        geometry=[Point(lon, lat) for _sid, _name, lon, lat, _series in rows],
        crs=4326).to_file(path, driver="FlatGeobuf")
    return str(path)


_SERIES = "2026-09-05T23:48Z,0.346000\n2026-09-05T23:54Z,0.291000"


def test_the_level_is_the_nearest_gauges_last_reading(tmp_path):
    """Two gauges on one lake: the one the AOI is at answers, and the level is
    the last sample it published in the window rather than a mean of it."""
    from trid3nt_server.workflows.telemac.templates.stratified_flow.lake_level import (
        _observed,
    )

    reading = _observed(
        _gauges(tmp_path, [("9099018", "Marquette C.G.", -87.378, 46.545, _SERIES),
                           ("9099044", "Ontonagon", -89.324, 46.874,
                            "2026-09-05T23:54Z,9.900000")]),
        "fetch_greatlakes_water_level", "fetch_greatlakes_bathymetry",
        {"lon": -87.380, "lat": 46.539})
    assert reading["elevation_m"] == pytest.approx(0.291)
    assert reading["station_id"] == "9099018"
    assert "offset between them is 0.000 m" in reading["note"]


def test_a_level_and_a_bed_on_two_datums_refuse_by_name(tmp_path):
    """The stated datum is the whole arithmetic: a gauge on one zero and a bed on
    another are not two numbers on one axis, and adding them is the refusal."""
    from trid3nt_server.workflows.telemac.errors import TelemacError
    from trid3nt_server.workflows.telemac.templates.stratified_flow.lake_level import (
        _observed,
    )

    with pytest.raises(TelemacError) as excinfo:
        _observed(_gauges(tmp_path, [("9099018", "Marquette C.G.", -87.378,
                                      46.545, _SERIES)]),
                  "fetch_greatlakes_water_level", "fetch_topobathy",
                  {"lon": -87.380, "lat": 46.539})
    assert excinfo.value.error_code == "TELEMAC3D_DATUMS_DIFFER"
    assert "NAVD88" in str(excinfo.value)


def test_water_no_gauge_watches_refuses_rather_than_opening_at_the_datum(tmp_path):
    """A lake nobody measures the level of does not get zero: zero IS the chart
    datum, which is the dry rim this row exists to give water."""
    from trid3nt_server.workflows.telemac.errors import TelemacError
    from trid3nt_server.workflows.telemac.templates.stratified_flow.lake_level import (
        _observed,
    )

    with pytest.raises(TelemacError) as excinfo:
        _observed(_gauges(tmp_path, []), "fetch_greatlakes_water_level",
                  "fetch_greatlakes_bathymetry", {"lon": -87.38, "lat": 46.54})
    assert excinfo.value.error_code == "TELEMAC3D_LAKE_IS_UNGAUGED"


def test_the_thermocline_is_stated_below_the_water_top_not_the_datum():
    """The hook has only the node's elevation, so a free surface the run opened
    above the datum has to enter the depth the thermocline is placed at."""
    from trid3nt_server.workflows.telemac.modules.telemac3d import _condi_thermocline

    assert "DPTH=0.2910D0-Z(I3)" in _condi_thermocline(
        8.0, 18.0, 6.0, 1.5, 0.291)
