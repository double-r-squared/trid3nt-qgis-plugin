"""``fetch_watershed``: the DEM over a buffer window and the D8 trace, as one artifact.

The window is built in metres and held to the cell budget before any network call, the
elevation grid comes from the DEM fetcher that owns it, and the basin and the outlet
run it drains through come back as two rows. Covered with that, the refusals: a
malformed pour point, a window over the budget, a DEM the sibling could not serve, and
a catchment that ran off the edge of its window."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trid3nt_server.tools.fetchers._fetch_common import FetchError
from trid3nt_server.tools.fetchers._router.errors import RouterInputError, RouterUpstreamError
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.hydrology.fetch_watershed import hooks as ws

#: The gauged Coweeta Creek headwater near Otto, NC - the rain-on-grid canary's own
#: pour point, so the offline shapes here stand where the live proof stands.
_POUR = [-83.40402, 35.05746]


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_watershed"]


def _params(**over):
    return {"pour_point": list(_POUR), "buffer_km": 10.0, "resolution_m": 30,
            "dem_source": "auto", **over}


def _square(lon: float, lat: float, half: float = 0.01) -> dict:
    return {"type": "Polygon", "coordinates": [[
        [lon - half, lat - half], [lon + half, lat - half],
        [lon + half, lat + half], [lon - half, lat + half],
        [lon - half, lat - half]]]}


def _stub_basin(notes, *, truncated=False, snapped=(-83.40125, 35.059044)):
    """What the delineation hands back: the geometry, its measures and its notes."""
    return lambda *a, **kw: (_square(*_POUR), {
        "area_km2": 30.4788, "cell_count": 39835, "truncated": truncated,
        "pour_point_lon": _POUR[0], "pour_point_lat": _POUR[1],
        "snapped_lon": snapped[0], "snapped_lat": snapped[1]}, notes)


def test_the_spec_delegates_the_grid_and_declares_the_two_rows(spec):
    assert spec.hooks.delegate == "watershed.read"
    assert spec.hooks.delegate_validate == "watershed.validate"
    assert spec.output.layer_type == "vector"
    # ``part`` says which row it is and ``type`` is what the outlet run carries.
    assert {"part", "type", "dem_uri"} <= set(spec.ingest["properties"])


def test_a_malformed_pour_point_refuses_before_the_network(spec):
    with pytest.raises(RouterInputError) as excinfo:
        ws.validate(spec, _params(pour_point="the creek"))
    assert excinfo.value.error_code == "WATERSHED_INPUT_INVALID"


def test_a_pour_point_off_the_planet_refuses_by_name(spec):
    with pytest.raises(RouterInputError):
        ws.validate(spec, _params(pour_point=[-200.0, 35.0]))


def test_a_window_over_the_cell_budget_refuses_naming_both_levers(spec):
    with pytest.raises(RouterInputError) as excinfo:
        ws.validate(spec, _params(buffer_km=50.0, resolution_m=10))
    message = str(excinfo.value)
    assert "resolution_m=" in message and "buffer_km" in message


def test_a_window_inside_the_budget_passes(spec):
    assert ws.validate(spec, _params()) is None


def test_the_window_is_the_same_width_in_metres_on_both_axes():
    from pyproj import Geod

    west, south, east, north = ws._window(*_POUR, 10.0)
    geod = Geod(ellps="WGS84")
    mid_lat = 0.5 * (south + north)
    across = geod.inv(west, mid_lat, east, mid_lat)[2]
    down = geod.inv(_POUR[0], south, _POUR[0], north)[2]
    assert across == pytest.approx(20_000.0, rel=1e-3)
    assert down == pytest.approx(20_000.0, rel=1e-3)
    assert west < _POUR[0] < east and south < _POUR[1] < north


def test_the_outlet_run_is_two_points_a_few_cells_apart():
    from pyproj import Geod

    ring = _square(*_POUR)
    # A snapped outlet ON the eastern edge of the traced outline.
    run = ws._outlet_run(ring, (_POUR[0] + 0.01, _POUR[1]), 30)
    assert run["type"] == "LineString"
    (x0, y0), (x1, y1) = run["coordinates"]
    length = Geod(ellps="WGS84").inv(x0, y0, x1, y1)[2]
    assert length == pytest.approx(2.0 * ws._OUTLET_HALF_CELLS * 30.0, rel=0.02)


def test_a_basin_that_ran_off_the_window_refuses_rather_than_returning_a_cut(
        spec, monkeypatch):
    from trid3nt_server.tools import TOOL_REGISTRY

    monkeypatch.setattr(ws, "_basin", _stub_basin(["traced it"], truncated=True))
    monkeypatch.setitem(TOOL_REGISTRY, "fetch_dem", SimpleNamespace(
        fn=lambda **kw: SimpleNamespace(uri="s3://bucket/dem.tif")))
    with pytest.raises(RouterInputError) as excinfo:
        ws.read(spec, _params(), timeout_s=300.0)
    assert excinfo.value.error_code == "WATERSHED_TRUNCATED"
    assert "buffer_km" in str(excinfo.value)


def test_a_dem_the_sibling_could_not_serve_is_quoted_verbatim(spec, monkeypatch):
    from trid3nt_server.tools import TOOL_REGISTRY

    class _Outage(FetchError):
        error_code = "DEM_FALLBACK_GATE"
        retryable = True

    def _raise(**kw):
        raise _Outage("3DEP is down; retry with source=copernicus")

    monkeypatch.setitem(TOOL_REGISTRY, "fetch_dem", SimpleNamespace(fn=_raise))
    with pytest.raises(RouterUpstreamError) as excinfo:
        ws._dem(spec, (-83.5, 35.0, -83.3, 35.1), 30, "auto")
    message = str(excinfo.value)
    assert "DEM_FALLBACK_GATE" in message and "3DEP is down" in message
    # The retry the DEM names is stated in THIS tool's vocabulary.
    assert "dem_source" in message


def test_a_dem_refusal_that_is_not_retryable_stays_an_input_refusal(spec, monkeypatch):
    from trid3nt_server.tools import TOOL_REGISTRY

    class _Bad(FetchError):
        error_code = "DEM_OUT_OF_COVERAGE"
        retryable = False

    def _raise(**kw):
        raise _Bad("3DEP is US-only")

    monkeypatch.setitem(TOOL_REGISTRY, "fetch_dem", SimpleNamespace(fn=_raise))
    with pytest.raises(RouterInputError):
        ws._dem(spec, (2.3, 48.8, 2.4, 48.9), 30, "auto")


def test_the_two_rows_are_the_basin_and_the_outlet_it_drains_through(
        spec, monkeypatch):
    from trid3nt_server.tools import TOOL_REGISTRY

    monkeypatch.setattr(ws, "_basin", _stub_basin(["traced it"]))
    monkeypatch.setitem(TOOL_REGISTRY, "fetch_dem", SimpleNamespace(
        fn=lambda **kw: SimpleNamespace(uri="s3://bucket/dem.tif")))

    rows = ws.read(spec, _params(), timeout_s=300.0)
    by_part = {row["properties"]["part"]: row for row in rows}
    assert sorted(by_part) == ["basin", "outlet"]
    assert by_part["basin"]["geometry"]["type"] == "Polygon"
    assert by_part["basin"]["properties"]["type"] is None
    # A catchment outlet prescribes a stage-discharge relation, not a stated level,
    # and it is spelled with the slot's own constant so a rename cannot drop the run.
    from trid3nt_server.inputs.boundary import RATING, RUN_TYPES

    assert by_part["outlet"]["properties"]["type"] == RATING
    assert RATING in RUN_TYPES
    assert len(by_part["outlet"]["geometry"]["coordinates"]) == 2
    for row in rows:
        properties = row["properties"]
        # The grid the trace ran in is the grid the bed is read from afterwards.
        assert properties["dem_uri"] == "s3://bucket/dem.tif"
        assert properties["area_km2"] == pytest.approx(30.4788)
        assert properties["dem_resolution_m"] == 30
        assert properties["notes"] == "traced it"


def test_the_watershed_is_found_through_its_class(class_routes_to_the_match):
    """A covered fetcher carries no corpus: its class is the door."""
    class_routes_to_the_match("hydrography", "fetch_watershed")
