"""The moment-tensor row: the ComCat event record read into every published solution.

Offline. The records are the moment-tensor products USGS published for Ecuador 2016 (W-phase and its centroid
solution) and Kamchatka 2025, verbatim. Pinned: both nodal planes, the scalar moment in N m and the centroid come from
the product's own properties; the centroid is the derived (centroid-type) origin and never the hypocentre; a product
without a nodal plane or a scalar moment, an event without a tensor and an unstated event refuse by name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trid3nt_server.tools.fetchers._router.registration import get_spec
from trid3nt_server.tools.fetchers.hazard.fetch_moment_tensor import hooks

_FIX = Path(__file__).parents[1] / "fixtures" / "geoclaw" / "moment_tensor"


def _record(name: str, event: str = "e") -> dict:
    return hooks.record(get_spec("fetch_moment_tensor"), {"event_id": event}, [(_FIX / f"{name}_event.json").read_bytes()])


def test_every_solution_is_returned_with_both_planes_the_moment_and_the_derived_centroid():
    rec = _record("ecuador_2016")
    by_code = {s["code"]: s for s in rec["solutions"]}

    assert set(by_code) == {"us_20005j32_mww", "us_20005j32_mwc"}
    mww = by_code["us_20005j32_mww"]
    assert mww["planes"] == [{"strike": 183.0, "dip": 75.0, "rake": 83.0}, {"strike": 26.0, "dip": 16.0, "rake": 113.0}]
    assert mww["scalar_moment_nm"] == 7.05e20 and mww["centroid"] == {"longitude": -79.97, "latitude": -0.25, "depth_km": 21.5}
    assert (mww["source"], mww["magnitude"], mww["magnitude_type"]) == ("us", 7.8, "Mww")


def test_the_centroid_is_the_derived_origin_never_the_hypocentre():
    kam = _record("kamchatka_2025")["solutions"][0]

    assert kam["centroid"] == {"longitude": 159.5904, "latitude": 52.2496, "depth_km": 21.5}
    assert kam["scalar_moment_nm"] == 2.22e22 and kam["duration_s"] == 225.0


def test_a_product_without_a_derived_origin_carries_no_centroid_and_one_without_a_moment_refuses():
    body = json.loads((_FIX / "kamchatka_2025_event.json").read_text())
    props = body["properties"]["products"]["moment-tensor"][0]["properties"]
    for key in ("derived-latitude", "derived-longitude", "derived-depth"):
        del props[key]
    spec = get_spec("fetch_moment_tensor")

    assert hooks.record(spec, {"event_id": "e"}, [json.dumps(body).encode()])["solutions"][0]["centroid"] is None
    del props["scalar-moment"]
    with pytest.raises(Exception, match="states no usable scalar-moment"):
        hooks.record(spec, {"event_id": "e"}, [json.dumps(body).encode()])
    del props["nodal-plane-2-dip"]
    props["scalar-moment"] = "1e20"
    with pytest.raises(Exception, match="nodal-plane-2-dip"):
        hooks.record(spec, {"event_id": "e"}, [json.dumps(body).encode()])


def test_an_event_without_a_tensor_and_an_unstated_event_refuse_naming_the_event():
    spec = get_spec("fetch_moment_tensor")
    none = json.dumps({"properties": {"products": {"origin": [{}]}}}).encode()

    with pytest.raises(Exception, match="event 'us7000none' holds no moment-tensor product"):
        hooks.record(spec, {"event_id": "us7000none"}, [none])
    with pytest.raises(Exception, match="states no event"):
        hooks.build_request(spec, {"event_id": " "})
    assert hooks.build_request(spec, {"event_id": "us20005j32"})[0].params == {"eventid": "us20005j32", "format": "geojson"}
