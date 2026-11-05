"""The earthquake-epicentre row: the ComCat event record read into its preferred origin.

Offline. The record is an excerpt of USGS ComCat's event GeoJSON for Kamchatka 2025 (us6000qw60) with the products
dropped, the point and properties verbatim. Pinned: the location is the record's own point, the depth in km, the time
ISO UTC from epoch milliseconds; a record with no usable point and an unstated event refuse by name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trid3nt_server.tools.fetchers._router.registration import get_spec
from trid3nt_server.tools.fetchers.hazard.fetch_earthquake_epicentre import hooks

_EVENT = Path(__file__).parents[1] / "fixtures" / "geoclaw" / "earthquake_epicentre" / "kamchatka_2025_event.json"


def test_the_epicentre_is_the_records_preferred_origin():
    rec = hooks.record(get_spec("fetch_earthquake_epicentre"), {"event_id": "us6000qw60"}, [_EVENT.read_bytes()])

    assert (rec["longitude"], rec["latitude"], rec["depth_km"]) == (160.2395, 52.4948, 35.0)
    assert rec["time"] == "2025-07-29T23:24:52.483000Z"
    assert (rec["event_id"], rec["magnitude"], rec["magnitude_type"], rec["status"]) == ("us6000qw60", 8.8, "mww", "reviewed")


def test_a_record_without_a_point_and_an_unstated_event_refuse_by_name():
    spec = get_spec("fetch_earthquake_epicentre")
    body = json.loads(_EVENT.read_text())
    body["geometry"]["coordinates"] = [160.2395, None, 35]

    with pytest.raises(Exception, match="states no usable preferred origin"):
        hooks.record(spec, {"event_id": "e"}, [json.dumps(body).encode()])
    with pytest.raises(Exception, match="names? the ComCat event id|states no event"):
        hooks.build_request(spec, {"event_id": " "})
