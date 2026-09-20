"""``fetch_vertical_datum_offset``: the request VDatum needs, and the offset read off it.

The service couples each vertical frame to a horizontal frame and a geoid model,
and answers a mismatched pairing with a plausible wrong number, so the pairing is
pinned rather than searched. Covered with that: the offset read off a real answer,
the declared frame set matching the pinned table, the sentinel an uncovered point
comes back as, VDatum's own error envelope arriving under HTTP 200, an
unreportable uncertainty, a frame the table does not serve, and the region the
point itself chooses - which the service will not look up and answers wrongly
under the wrong grid."""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError, RouterInputError
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.ocean.fetch_vertical_datum_offset import hooks as vd

_GAUGE = [-122.6691667, 45.5175]
#: The Scripps nearshore water off La Jolla, CA, and the Duck FRF pier, NC: one
#: point in the west-coast grids and one in the contiguous grids.
_LA_JOLLA = [-117.25714, 32.86689]
_DUCK = [-75.7467, 36.1833]


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_vertical_datum_offset"]


def _answered(**fields) -> list[bytes]:
    body = {"t_x": "-122.6703716765", "t_y": "45.517337103", "t_z": "1.057",
            "uncertainty": "0.053"}
    body.update(fields)
    return [json.dumps(body).encode("utf-8")]


def _params(**over) -> dict:
    asked = {"point": _GAUGE, "from_frame": "ngvd29", "to_frame": "navd88"}
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


def test_a_tidal_river_stands_under_its_coastal_region_and_not_the_inland_grid(spec):
    # The contiguous grids carry no tidal surface, and VDatum refuses the
    # conversion there rather than answering short.
    plan, = vd.build_request(spec, _params())
    assert plan.params["region"] == "westcoast"


def test_an_inland_point_stands_under_the_contiguous_grid(spec):
    plan, = vd.build_request(spec, _params(point=[-104.9903, 39.7392]))
    assert plan.params["region"] == "contiguous"


def test_a_point_no_region_stands_over_refuses_by_name(spec):
    with pytest.raises(RouterInputError) as caught:
        vd.build_request(spec, _params(point=[-150.0, 30.0]))
    assert caught.value.error_code == "VDATUM_INPUT_INVALID"
    assert "no NOAA VDatum region stands over" in str(caught.value)


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


def test_a_west_coast_tidal_frame_is_sent_under_the_frame_that_region_demands(spec):
    """VDatum publishes its west-coast tidal grids on IGS14 and refuses the
    conversion outright under the frame every other region serves them on: its
    own words are "For West Coast Region, Target Horizontal Frame should be
    IGS14 for Tidal". The tidal side of the pair carries that frame and the
    other side keeps its own."""
    asked = _params(point=_LA_JOLLA, from_frame="mllw", to_frame="navd88")
    plan, = vd.build_request(spec, asked)
    assert plan.params["region"] == "westcoast"
    assert (plan.params["s_v_frame"], plan.params["s_h_frame"]) == ("MLLW", "IGS14")
    assert (plan.params["t_v_frame"], plan.params["t_h_frame"]) == ("NAVD88",
                                                                   "NAD83_2011")


def test_the_demanded_frame_follows_the_tidal_side_and_not_the_target_slot(spec):
    """The demand is the TIDAL frame's, so it moves with that frame rather than
    sitting on whichever side the conversion was asked in."""
    asked = _params(point=_LA_JOLLA, from_frame="navd88", to_frame="mhw")
    plan, = vd.build_request(spec, asked)
    assert plan.params["s_h_frame"] == "NAD83_2011"
    assert plan.params["t_h_frame"] == "IGS14"


def test_a_tidal_frame_outside_that_region_keeps_the_pairing_it_is_served_under(spec):
    """No other region demands it, and sending IGS14 where the grids are
    published on NAD83_2011 would be the same mismatch the other way."""
    plan, = vd.build_request(spec, _params(point=_DUCK, from_frame="mllw",
                                           to_frame="navd88"))
    assert plan.params["region"] == "contiguous"
    assert plan.params["s_h_frame"] == plan.params["t_h_frame"] == "NAD83_2011"


def test_the_west_coast_tidal_offset_is_read_off_the_answer_that_pairing_gets(spec):
    """The recorded answer to the request above: a conversion the service
    performs rather than the 412 the other pairing is refused with."""
    body = {"region": "WESTCOAST", "s_h_frame": "IGS14", "s_v_frame": "MLLW",
            "t_h_frame": "NAD83_2011", "t_v_frame": "NAVD88",
            "t_x": "-117.2571307911", "t_y": "32.866890859", "t_z": "-0.086",
            "uncertainty": "0.091"}
    asked = _params(point=_LA_JOLLA, from_frame="mllw", to_frame="navd88")
    read = vd.record(spec, asked, [json.dumps(body).encode("utf-8")])
    assert read["offset_m"] == -0.086
    assert read["uncertainty_m"] == 0.091
    assert read["region"] == "westcoast"


def test_the_refusal_the_wrong_west_coast_pairing_gets_is_read_as_the_input_error(spec):
    """The service answers it with HTTP 200 and an error envelope, and the
    message a reader is handed is the service's own."""
    body = {"errorCode": 412,
            "message": "For West Coast Region, Target Horizontal Frame should "
                       "be IGS14 for Tidal"}
    asked = _params(point=_LA_JOLLA, from_frame="mllw", to_frame="navd88")
    with pytest.raises(RouterInputError) as raised:
        vd.record(spec, asked, [json.dumps(body).encode("utf-8")])
    assert "IGS14 for Tidal" in str(raised.value)
