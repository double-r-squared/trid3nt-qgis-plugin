"""The AOI acquisition: an explicit extent verbatim, the box around a Point, a
geocoded place around one. Offline: the geocoder is stood in for."""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.inputs import Point
from trid3nt_server.workflows.inputs.aoi import acquire_aoi


@pytest.mark.asyncio
async def test_a_point_is_boxed_half_deg_either_side_and_the_place_only_names_it(
        monkeypatch):
    from trid3nt_server.workflows.inputs import aoi as aoi_mod

    async def _never(_name):
        raise AssertionError("a Point decides the box; the place is not geocoded")

    monkeypatch.setattr(aoi_mod, "geocode_place", _never)
    out = await acquire_aoi(location="Otto, North Carolina", bbox=None,
                            around=Point(-83.4, 35.05, "outlet"), half_deg=0.1,
                            default_name="watershed")
    assert out["bbox"] == pytest.approx((-83.5, 34.95, -83.3, 35.15))
    assert (out["lon"], out["lat"]) == (-83.4, 35.05)
    assert out["name"] == "Otto, North Carolina"


@pytest.mark.asyncio
async def test_an_explicit_extent_still_wins_over_the_point():
    out = await acquire_aoi(location=None, bbox=(-84.0, 35.0, -83.9, 35.1),
                            around=Point(-83.4, 35.05), half_deg=0.1,
                            default_name="watershed")
    assert out["bbox"] == (-84.0, 35.0, -83.9, 35.1)
    assert out["name"] == "watershed" and out["slug"] == "watershed"


@pytest.mark.asyncio
async def test_a_point_at_the_worlds_edge_is_clamped_to_it():
    out = await acquire_aoi(location=None, bbox=None, around=Point(-179.95, 89.98),
                            half_deg=0.1)
    assert out["bbox"] == pytest.approx((-180.0, 89.88, -179.85, 90.0))
