"""Unit tests for the router's ``ingest.nearest`` post-fetch pick.

Covered: no point asked for leaving the features untouched, the nearest of
several winning, a required property skipping the rows that report nothing, the
measured distance riding on the kept row, an empty candidate set refusing typed,
a malformed point refusing typed, and a spec that declares the block without
naming its parameter refusing at the call."""

from __future__ import annotations

import pytest

from trid3nt_server.tools.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
)
from trid3nt_server.tools.fetchers._router.transforms import nearest


class _Spec:
    error_code_prefix = "TEST"
    input_error_suffix = "INPUT_ERROR"
    empty_error_suffix = "NO_SITES"


def _site(site_id: str, lon: float, lat: float, value: float | None = 1.0) -> dict:
    return {"type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"site_id": site_id, "value": value}}


_CFG = {"to": "near", "require": "value"}
_SITES = [_site("far", -123.20, 45.50), _site("near", -122.68, 45.51),
          _site("mid", -122.90, 45.50)]


def test_no_point_leaves_the_fetch_untouched() -> None:
    assert nearest.apply(_SITES, _Spec(), {}, _CFG) == _SITES


def test_the_nearest_site_wins_and_carries_its_distance() -> None:
    kept = nearest.apply(_SITES, _Spec(), {"near": [-122.67, 45.51]}, _CFG)
    assert len(kept) == 1
    assert kept[0]["properties"]["site_id"] == "near"
    assert kept[0]["properties"]["distance_km"] == pytest.approx(0.78, abs=0.05)


def test_a_site_reporting_nothing_is_not_a_candidate() -> None:
    sites = [_site("near", -122.68, 45.51, value=None), _site("mid", -122.90, 45.50)]
    kept = nearest.apply(sites, _Spec(), {"near": [-122.67, 45.51]}, _CFG)
    assert kept[0]["properties"]["site_id"] == "mid"


def test_nothing_that_reports_refuses_honest_empty() -> None:
    sites = [_site("near", -122.68, 45.51, value=None)]
    with pytest.raises(RouterEmptyError) as caught:
        nearest.apply(sites, _Spec(), {"near": [-122.67, 45.51]}, _CFG)
    assert caught.value.error_code == "TEST_NO_SITES"


def test_a_feature_with_no_position_is_not_a_candidate() -> None:
    sites = [{"type": "Feature", "geometry": None, "properties": {"value": 1.0}}]
    with pytest.raises(RouterEmptyError):
        nearest.apply(sites, _Spec(), {"near": [-122.67, 45.51]}, _CFG)


def test_a_malformed_point_refuses_typed() -> None:
    with pytest.raises(RouterInputError):
        nearest.apply(_SITES, _Spec(), {"near": "portland"}, _CFG)


def test_a_block_that_names_no_parameter_refuses_typed() -> None:
    with pytest.raises(RouterInputError):
        nearest.apply(_SITES, _Spec(), {"near": [-122.0, 45.0]}, {})


def test_a_line_feature_ranks_on_its_first_vertex() -> None:
    line = {"type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": [[-122.68, 45.51], [-122.60, 45.51]]},
            "properties": {"value": 3.0}}
    kept = nearest.apply([line, _site("far", -123.2, 45.5)], _Spec(),
                         {"near": [-122.67, 45.51]}, _CFG)
    assert kept[0]["properties"]["value"] == 3.0
