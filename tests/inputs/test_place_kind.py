"""THE PLACE'S KIND: which water a seed stands on, read off what the seed came with.

Offline. The kind is a fact of the PLACE - a river, a reservoir, a coast - so it
is never stated by the question asked there. A geocoder publishes its own word
for what it resolved; that word rides on the point and is turned here into the
word the hydrography sources publish the feature under. A seed nothing said a
type for is unclassified, and the match then ranks every row of the class.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from trid3nt_server.inputs.domain import place_kind
from trid3nt_server.inputs.point import Point, point


def _ingested(value):
    return asyncio.run(point(value, label="seed"))


def test_the_geocoder_s_word_becomes_the_word_a_source_publishes() -> None:
    assert place_kind(Point(-123.2, 45.5, "Hagg Lake", "reservoir")) == "waterbody"
    assert place_kind(Point(-90.2, 38.6, "Missouri River", "river")) == "flowline"
    assert place_kind(Point(-70.0, 41.6, "Nauset Beach", "beach")) == "coastline"


def test_a_place_that_is_not_water_says_nothing() -> None:
    """A city is a place the hydrography rows say nothing about, and nothing is
    the honest answer: a guess would fill the domain with the wrong shape under
    the right name."""
    assert place_kind(Point(-82.6, 27.9, "Tampa", "city")) == ""
    assert place_kind(Point(-82.6, 27.9)) == ""
    assert place_kind(None) == ""


def test_the_seed_carries_the_kind_through_the_point_ingestion() -> None:
    """What the geocoder answered with reaches the classifier as it came: the
    ingestion reads the type off the answer rather than asking for it."""
    seed = _ingested({"name": "Hagg Lake", "longitude": -123.2,
                      "latitude": 45.49, "place_class": "natural",
                      "place_type": "water"})
    assert (seed.name, seed.kind) == ("Hagg Lake", "water")
    assert place_kind(seed) == "waterbody"


def test_a_pick_states_no_kind() -> None:
    seed = _ingested({"coordinates": [-123.2, 45.49]})
    assert seed.kind == ""
    assert place_kind(seed) == ""


def test_the_geocoder_s_own_two_words_are_published_as_they_came(monkeypatch) -> None:
    """The service states what the place is on the SAME answer that resolved
    it, so the words are carried off that answer and never asked for again."""
    from trid3nt_server.tools.fetchers.socioeconomic.geocode_location import (
        geocode_location as module)

    raw = {"display_name": "Henry Hagg Lake, Oregon",
           "boundingbox": ["45.45", "45.52", "-123.25", "-123.18"],
           "class": "natural", "type": "water"}
    calls = []

    def _one_call(query, **_kwargs):
        calls.append(query)
        return SimpleNamespace(latitude=45.49, longitude=-123.2, raw=raw)

    monkeypatch.setattr(module, "_client",
                        lambda: SimpleNamespace(geocode=_one_call))
    payload = json.loads(module._fetch_nominatim_geocode_bytes("Hagg Lake"))
    assert (payload["place_class"], payload["place_type"]) == ("natural", "water")
    assert len(calls) == 1
    assert place_kind(_ingested(payload)) == "waterbody"
