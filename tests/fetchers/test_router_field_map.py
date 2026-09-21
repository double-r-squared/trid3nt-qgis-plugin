"""Offline tests for the declarative field map, with no live calls.

Covers the blocks no hook stands behind any more: the request template and its
relative window, the body walk and response cap, both paging styles with their caps,
the status-shaped honest empty, and the keyed detail join with its clip."""

from __future__ import annotations

import json

import pytest

from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_server.tools.fetchers._router import field_map as F
from trid3nt_server.tools.fetchers._router import router as R
from trid3nt_server.tools.fetchers._router.errors import (RouterEmptyError,
                                                          RouterInputError,
                                                          RouterUpstreamError)
from trid3nt_server.tools.fetchers._router.executors import http_json as HJ
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers._router.transport import TransportError

_SPECS = compose_specs_from_tree()


def _spec(name: str) -> SourceSpec:
    return _SPECS[name]


def _with_ingest(name: str, **blocks) -> SourceSpec:
    spec = _spec(name)
    return spec.model_copy(update={"ingest": {**(spec.ingest or {}), **blocks}})


def test_read_path_walks_dicts_and_list_indices():
    obj = {"a": {"b": [{"c": 1}, {"c": 2}]}}
    assert F.read_path(obj, "a.b.1.c") == 2
    assert F.read_path(obj, "a.b.9.c") is None
    assert F.read_path(obj, "a.missing.c") is None


def test_template_drops_the_key_whose_param_is_unset():
    assert F._render("{bbox[0]}", {"bbox": [1.5, 2, 3, 4]}) == "1.5"
    assert F._render("{bbox[0]}", {"bbox": None}) is None
    assert F._render("literal", {}) == "literal"


def test_paginated_offset_stops_on_a_short_page():
    """A full page continues at the next offset; a short page ends the walk."""
    spec = _spec("fetch_openfema_disasters")
    params = R.validate_params(spec, {"state_code": "RI"})
    pages = [
        json.dumps({"DisasterDeclarationsSummaries": [{"fipsStateCode": "44"}] * 1000}).encode(),
        json.dumps({"DisasterDeclarationsSummaries": [{"fipsStateCode": "44"}]}).encode(),
    ]
    seen: list[str] = []

    def fake(_spec, plan):
        seen.append(plan.params["$skip"])
        return pages[len(seen) - 1]

    orig, HJ._get = HJ._get, fake
    try:
        bodies = HJ._fetch_paginated(spec, params, spec.ingest["pagination"])
    finally:
        HJ._get = orig
    assert seen == ["0", "1000"] and len(bodies) == 2


def test_paginated_offset_stops_at_the_row_cap():
    spec = _spec("fetch_openfema_disasters")
    params = R.validate_params(spec, {"state_code": "RI"})
    full = json.dumps({"DisasterDeclarationsSummaries": [{"fipsStateCode": "44"}] * 1000}).encode()
    calls = []

    def fake(_spec, plan):
        calls.append(plan.params["$skip"])
        return full

    orig, HJ._get = HJ._get, fake
    try:
        bodies = HJ._fetch_paginated(spec, params, spec.ingest["pagination"])
    finally:
        HJ._get = orig
    assert len(bodies) == 12                       # 12 x 1000 reaches the 12000 row cap


def test_paginated_page_style_walks_the_reported_count():
    """The page style reads its page count off body 1 and stops there."""
    spec = _spec("fetch_tsunami_events")
    params = R.validate_params(spec, {"bbox": [135, 30, 150, 45], "observation_type": "runups"})
    seen: list[int] = []

    def fake(_spec, plan):
        seen.append(len(seen) + 1)
        return json.dumps({"totalPages": 3, "totalItems": 5, "items": []}).encode()

    orig, HJ._get = HJ._get, fake
    try:
        bodies = HJ._fetch_paginated(spec, params, spec.ingest["pagination"])
    finally:
        HJ._get = orig
    assert len(bodies) == 3


def test_paginated_page_style_refuses_past_the_cap():
    spec = _spec("fetch_tsunami_events")
    params = R.validate_params(spec, {"bbox": [135, 30, 150, 45], "observation_type": "runups"})

    def fake(_spec, plan):
        return json.dumps({"totalPages": 900, "totalItems": 90000, "items": []}).encode()

    orig, HJ._get = HJ._get, fake
    try:
        with pytest.raises(RouterInputError) as ei:
            HJ._fetch_paginated(spec, params, spec.ingest["pagination"])
    finally:
        HJ._get = orig
    assert ei.value.error_code == "TSUNAMI_EVENTS_RESULT_TOO_LARGE"


def test_declared_empty_turns_a_status_into_the_honest_zero():
    """A 404 over the declared body substring IS this source's zero, not a failure."""
    spec = _with_ingest("fetch_usgs_earthquakes", empty={
        "message": "no events in scope", "code": "NO_EVENTS",
        "on_status": [404], "body_contains": "no sites found"})

    def fake(plan):
        raise TransportError("404", status=404, body="the service says no sites found here")

    orig, HJ._get_raw = HJ._get_raw, fake
    try:
        with pytest.raises(RouterEmptyError) as ee:
            HJ._get(spec, F.declared_plans(spec, R.validate_params(spec, {}))[0])
    finally:
        HJ._get_raw = orig
    assert ee.value.error_code == "USGS_EARTHQUAKES_NO_EVENTS"


def test_a_status_the_declaration_does_not_name_stays_upstream():
    spec = _with_ingest("fetch_usgs_earthquakes", empty={
        "message": "no events in scope", "on_status": [404], "body_contains": "no sites found"})

    def fake(plan):
        raise TransportError("500", status=500, body="boom")

    orig, HJ._get_raw = HJ._get_raw, fake
    try:
        with pytest.raises(RouterUpstreamError):
            HJ._get(spec, F.declared_plans(spec, R.validate_params(spec, {}))[0])
    finally:
        HJ._get_raw = orig


class _Result:
    def __init__(self, body):
        self.body = body


def _counties(*rows):
    return _Result(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"GEOID": geoid, "NAME": name},
         "geometry": {"type": "Polygon", "coordinates": [ring]}}
        for geoid, name, ring in rows]}).encode())


_RING_A = [[-71.4, 41.6], [-71.3, 41.6], [-71.3, 41.7], [-71.4, 41.7], [-71.4, 41.6]]
_RING_B = [[-71.6, 41.8], [-71.5, 41.8], [-71.5, 41.9], [-71.6, 41.9], [-71.6, 41.8]]


def _aggregate(fips):
    return {"type": "Feature", "geometry": None,
            "properties": {"county_fips": fips, "state_fips": fips[:2], "county_name": None}}


def test_enrich_plans_are_one_request_per_distinct_key():
    spec = _spec("fetch_openfema_disasters")
    params = R.validate_params(spec, {"state_code": "RI"})
    plans = F.enrich_plans(spec, params, [_aggregate("44001"), _aggregate("44003"),
                                          _aggregate("25001")])
    assert [key for key, _ in plans] == ["44", "25"]
    assert plans[0][1].params["where"] == "STATE='44'"


def test_enrich_join_lifts_geometry_and_the_named_columns():
    spec = _spec("fetch_openfema_disasters")
    params = R.validate_params(spec, {"state_code": "RI"})
    out = F.enrich_merge(spec, params, [_aggregate("44001")],
                         {"44": _counties(("44001", "Bristol", _RING_A))})
    assert out[0]["properties"]["county_name"] == "Bristol"
    assert out[0]["geometry"]["type"] == "Polygon"


def test_enrich_drops_the_unmatched_and_clips_to_the_bbox():
    spec = _spec("fetch_openfema_disasters")
    params = R.validate_params(spec, {"bbox": [-71.45, 41.55, -71.28, 41.72]})
    detail = {"44": _counties(("44001", "Bristol", _RING_A), ("44003", "Kent", _RING_B))}
    out = F.enrich_merge(spec, params, [_aggregate("44001"), _aggregate("44003"),
                                        _aggregate("44007")], detail)
    assert [f["properties"]["county_fips"] for f in out] == ["44001"]


def test_enrich_joining_nothing_raises_the_declared_empty():
    spec = _spec("fetch_openfema_disasters")
    params = R.validate_params(spec, {"state_code": "RI"})
    with pytest.raises(RouterEmptyError) as ee:
        F.enrich_merge(spec, params, [_aggregate("44001")], {"44": _counties()})
    assert ee.value.error_code == "OPENFEMA_NO_DECLARATIONS"
