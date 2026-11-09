"""A domain the run's own search found is cut to the box it was searched in; one handed in is never cut.

Offline; the polygons are authored here.
"""

from __future__ import annotations

import pytest

from trid3nt_server.inputs.domain import domain
from trid3nt_server.inputs.user_input import EmptyResultError

#: A long river band, 1 degree east-west, the way a national river-area polygon overruns a reach.
_RIVER = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {
    "type": "Polygon", "coordinates": [[[-123.0, 45.50], [-122.0, 45.50], [-122.0, 45.52],
                                        [-123.0, 45.52], [-123.0, 45.50]]]}}]}
_WINDOW = [-122.6, 45.45, -122.5, 45.55]


def test_a_searched_polygon_is_cut_to_its_window():
    dom = domain(_RIVER, window=_WINDOW)
    west, south, east, north = dom.bbox
    assert west >= _WINDOW[0] - 1e-9 and east <= _WINDOW[2] + 1e-9
    assert (south, north) == pytest.approx((45.50, 45.52))


def test_a_handed_in_polygon_is_never_cut():
    assert domain(_RIVER).bbox[0] == pytest.approx(-123.0)


def test_a_search_with_no_water_inside_its_window_held_nothing():
    with pytest.raises(EmptyResultError):
        domain(_RIVER, window=[-121.0, 40.0, -120.9, 40.1])
