"""Mesh coverage of a reach: the measure, its terminal zero, its journalled line.

Offline: a two-triangle mesh on disk and an inline GeoJSON centreline, no network.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from trid3nt_server.workflows.lib import journal
from trid3nt_server.workflows.telemac.steps.errors import ReachMeshUncovered
from trid3nt_server.workflows.telemac.steps.reach import measure_mesh_coverage

#: A UTM zone and an origin inside it, so the metres the mesh is written in and
#: the lon/lat the centreline is written in describe the same ground.
_EPSG = 32611
_X0, _Y0 = 300_000.0, 4_600_000.0
_SIDE_M = 100.0


def _lonlat(x: float, y: float) -> tuple[float, float]:
    from pyproj import Transformer

    lon, lat = Transformer.from_crs(_EPSG, 4326, always_xy=True).transform(x, y)
    return float(lon), float(lat)


def _mesh(tmp_path) -> dict:
    """A square of two triangles, as the accepted mesh's ``.2dm`` display face."""
    corners = [(_X0, _Y0), (_X0 + _SIDE_M, _Y0),
               (_X0 + _SIDE_M, _Y0 + _SIDE_M), (_X0, _Y0 + _SIDE_M)]
    rows = [f"ND {i + 1} {x:.3f} {y:.3f} 0.0" for i, (x, y) in enumerate(corners)]
    rows += ["E3T 1 1 2 3 1", "E3T 2 1 3 4 1"]
    path = tmp_path / "mesh.2dm"
    path.write_text("MESH2D\n" + "\n".join(rows) + "\n")
    return {"display_uri": str(path), "artifact": SimpleNamespace(utm_epsg=_EPSG)}


def _centerline(start: tuple[float, float], end: tuple[float, float]) -> str:
    return json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {},
                      "geometry": {"type": "LineString",
                                   "coordinates": [list(_lonlat(*start)),
                                                   list(_lonlat(*end))]}}],
    })


@pytest.mark.asyncio
async def test_a_partly_meshed_reach_proceeds_with_the_measured_percent(tmp_path):
    """Above zero is a HEURISTIC, not a gate: the run proceeds and the measured
    percent is journalled so the user decides whether to re-run finer."""
    mid_y = _Y0 + 0.5 * _SIDE_M
    line = _centerline((_X0 + 0.5 * _SIDE_M, mid_y),
                       (_X0 + 1.5 * _SIDE_M, mid_y))          # half outside
    mesh = _mesh(tmp_path)

    token = journal.bind_notes()
    try:
        out = await measure_mesh_coverage(mesh=mesh, centerline=line)
        notes = journal.drain_notes(token)
    except BaseException:
        journal.drain_notes(token)
        raise

    assert out is mesh                       # a pass-through, measured in the chain
    note = " ".join(notes)
    assert "mesh coverage" in note and "50." in note
    assert "mesh_resolution_m" in note       # what the user can do about it


@pytest.mark.asyncio
async def test_a_reach_the_mesh_holds_none_of_refuses_terminally(tmp_path):
    """Zero coverage is the one terminal outcome: none of this river is in the
    domain, so the solve would answer about a different reach."""
    outside_y = _Y0 + 10.0 * _SIDE_M
    line = _centerline((_X0, outside_y), (_X0 + _SIDE_M, outside_y))

    with pytest.raises(ReachMeshUncovered) as exc:
        await measure_mesh_coverage(mesh=_mesh(tmp_path), centerline=line)
    assert exc.value.error_code == "REACH_MESH_UNCOVERED"
    assert getattr(exc.value, "retryable", False) is False
    assert "finer mesh_resolution_m" in str(exc.value)


@pytest.mark.asyncio
async def test_a_fully_meshed_reach_measures_one_hundred_percent(tmp_path):
    line = _centerline((_X0 + 0.25 * _SIDE_M, _Y0 + 0.5 * _SIDE_M),
                       (_X0 + 0.75 * _SIDE_M, _Y0 + 0.5 * _SIDE_M))
    token = journal.bind_notes()
    await measure_mesh_coverage(mesh=_mesh(tmp_path), centerline=line)
    assert "100.0%" in " ".join(journal.drain_notes(token))


@pytest.mark.asyncio
async def test_a_mesh_with_no_projected_zone_cannot_be_measured(tmp_path):
    """The measure is metres-on-the-mesh's-own-zone; without one it refuses rather
    than reading a coverage that depends on latitude."""
    from trid3nt_server.workflows.telemac.steps.errors import TelemacDyeScenarioError

    mesh = _mesh(tmp_path) | {"artifact": SimpleNamespace(utm_epsg=None)}
    with pytest.raises(TelemacDyeScenarioError, match="cannot be measured"):
        await measure_mesh_coverage(
            mesh=mesh, centerline=_centerline((_X0, _Y0), (_X0 + _SIDE_M, _Y0)))
