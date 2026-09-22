"""``fetch_nhd_water_surface``: a seed and a radius in, the mapped banks out.

Offline. The one step the grammar cannot say is the envelope, so that is what is
covered here - the box the seed and the radius make, narrowed by latitude so it
is square on the ground - beside the filter that keeps the water and leaves the
structures on it, and the class door the domain slot reaches this row through.
"""

from __future__ import annotations

import pytest

from trid3nt_server.tools.fetchers._router.errors import RouterInputError
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.hydrology.fetch_nhd_water_surface import (
    hooks as ws)

#: The Willamette at Portland - a river wide enough for NHD to map its surface.
_SEED = [-122.6735, 45.5175]


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_nhd_water_surface"]


def _params(**over):
    return {"seed_point": list(_SEED), "radius_km": 3.0, **over}


def test_the_row_publishes_the_water_surface_and_nothing_else(spec):
    row = next(c for c in spec.coverage if c.data_class == "hydrography")
    assert set(row.vocabulary) == {"water surface"}
    assert row.ask == {"radius_km": "need:span_km"}
    assert spec.output.layer_type == "vector"


def test_only_the_water_is_asked_for_never_the_structures_on_it(spec):
    """NHDArea maps dams, locks and bridges beside the water. A domain is the
    water, named by the service's own FType codes."""
    assert spec.endpoints["data"].query["where"] == "FType IN (460, 537)"


def test_a_malformed_seed_refuses_before_the_network(spec):
    with pytest.raises(RouterInputError) as excinfo:
        ws.build_request(spec, _params(seed_point="the river"))
    assert excinfo.value.error_code == "NHD_WATER_SURFACE_INPUT_INVALID"


def test_the_envelope_is_the_seed_grown_by_the_radius(spec):
    plan = ws.build_request(spec, _params())[0]
    west, south, east, north = (float(v) for v in
                                plan.params["geometry"].split(","))
    assert north - south == pytest.approx(2.0 * 3.0 / 111.0, rel=1e-6)
    # A degree of longitude is shorter than a degree of latitude away from the
    # equator, so the box is WIDER in degrees to be square on the ground.
    assert (east - west) > (north - south)
    assert (west + east) / 2.0 == pytest.approx(_SEED[0])


def test_a_wider_radius_asks_a_wider_box(spec):
    near = ws.build_request(spec, _params(radius_km=1.0))[0]
    far = ws.build_request(spec, _params(radius_km=20.0))[0]
    assert float(far.params["geometry"].split(",")[3]) \
        > float(near.params["geometry"].split(",")[3])


def test_the_water_surface_is_found_through_its_class(class_routes_to_the_match):
    """A covered fetcher carries no corpus: its class is the door."""
    class_routes_to_the_match("hydrography", "fetch_nhd_water_surface")
