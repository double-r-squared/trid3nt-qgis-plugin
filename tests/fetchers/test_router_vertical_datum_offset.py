"""``fetch_vertical_datum_offset``: the request VDatum needs, and the offset read off it.

The service couples each vertical frame to a horizontal frame and a geoid model,
and answers a mismatched pairing with a plausible wrong number, so the pairing is
pinned rather than searched. Covered with that: the offset read off a real answer,
the declared frame set matching the pinned table, the sentinel an uncovered point
comes back as, VDatum's own error envelope arriving under HTTP 200, an
unreportable uncertainty, and a frame the table does not serve."""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError, RouterInputError
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.ocean.fetch_vertical_datum_offset import hooks as vd

_GAUGE = [-122.6691667, 45.5175]


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_vertical_datum_offset"]


def _answered(**fields) -> list[bytes]:
    body = {"t_x": "-122.6703716765", "t_y": "45.517337103", "t_z": "1.057",
            "uncertainty": "0.053"}
    body.update(fields)
    return [json.dumps(body).encode("utf-8")]


def _params(**over) -> dict:
    asked = {"point": _GAUGE, "from_frame": "ngvd29", "to_frame": "navd88",
             "region": "westcoast"}
    asked.update(over)
    return asked


def test_the_declared_frames_are_the_ones_the_table_serves(spec):
    # Two homes for the frame set would let a declared frame reach the service
    # with no pairing and come back as an ellipsoid height.
    declared = set(spec.params["from_frame"].values)
    assert declared == {name.lower() for name in vd._SERVED_AS}
    assert set(spec.params["to_frame"].values) == declared


def test_the_request_sends_each_frame_under_its_own_pairing(spec):
    plan, = vd.build_request(spec, _params())
    assert plan.params["s_v_frame"] == "NGVD29"
    assert plan.params["s_h_frame"] == "NAD27"
    assert plan.params["t_v_frame"] == "NAVD88"
    assert plan.params["t_h_frame"] == "NAD83_2011"
    # A zero height in means the height that comes back IS the offset.
    assert plan.params["s_z"] == "0.0"
    assert plan.params["s_v_unit"] == plan.params["t_v_unit"] == "m"
    assert plan.params["region"] == "westcoast"


def test_egm2008_is_sent_under_the_ellipsoid_and_its_own_geoid(spec):
    plan, = vd.build_request(spec, _params(from_frame="navd88", to_frame="egm2008"))
    assert plan.params["t_h_frame"] == "WGS84_G1674"
    assert plan.params["t_v_geoid"] == "egm2008"
    assert plan.params["s_v_geoid"] == "geoid18"


def test_the_converted_height_is_the_offset(spec):
    found = vd.record(spec, _params(), _answered())
    assert found["offset_m"] == 1.057
    assert found["uncertainty_m"] == 0.053
    assert found["from_frame"] == "NGVD29" and found["to_frame"] == "NAVD88"
    assert found["lon"] == _GAUGE[0] and found["region"] == "westcoast"
    assert found["source"] == "NOAA VDatum"


def test_an_unreportable_uncertainty_is_none_and_not_zero(spec):
    found = vd.record(spec, _params(), _answered(uncertainty="NaN"))
    assert found["uncertainty_m"] is None


def test_an_uncovered_point_refuses_rather_than_reading_the_sentinel(spec):
    with pytest.raises(RouterEmptyError) as caught:
        vd.record(spec, _params(), _answered(t_z="-999999"))
    assert caught.value.error_code == "VDATUM_NO_COVERAGE"
    assert "westcoast" in str(caught.value)


def test_the_service_s_own_refusal_arrives_under_http_200(spec):
    body = [json.dumps({"errorCode": 412, "message": "Uncaught error"}).encode()]
    with pytest.raises(RouterInputError) as caught:
        vd.record(spec, _params(), body)
    assert caught.value.error_code == "VDATUM_INPUT_INVALID"
    assert "Uncaught error" in str(caught.value)


def test_a_project_datum_is_not_a_frame_this_fetch_serves(spec):
    with pytest.raises(RouterInputError) as caught:
        vd.build_request(spec, _params(from_frame="CRD"))
    assert caught.value.error_code == "VDATUM_INPUT_INVALID"
    assert "project datum" in str(caught.value)
