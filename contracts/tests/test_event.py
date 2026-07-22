"""Round-trip + negative tests for EventMetadata + ClaimSet (Appendix C)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.event import (
    AdminUnit,
    ClaimSet,
    EventLocation,
    EventMetadata,
    EventProvenance,
    HurricaneIntensity,
    IntensityIndicators,
    NumericClaim,
    RainfallIntensity,
    RiverFloodIntensity,
)


def _hurricane_event() -> EventMetadata:
    article_id = new_ulid()
    return EventMetadata(
        event_id=new_ulid(),
        event_type="hurricane",
        confidence=0.92,
        canonical_name="Hurricane Ian",
        canonical_id="AL092022",
        location=EventLocation(
            bbox=(-82.6, 26.4, -81.7, 27.0),
            place_name="Fort Myers, FL",
            admin_unit=AdminUnit(country="US", region="FL", locality="Fort Myers"),
            geocoded=True,
            granularity="city",
            precision_class="bbox_sufficient",
        ),
        time_range={"start": "2022-09-28T00:00:00Z", "end": "2022-09-30T00:00:00Z"},
        time_classification="past",
        intensity=IntensityIndicators(
            hurricane=HurricaneIntensity(
                saffir_simpson=ClaimSet(
                    claims=[
                        NumericClaim(
                            value=4.0,
                            unit="category",
                            source_type="agency",
                            source_id="nhc-tcr-al092022",
                            source_url="https://www.nhc.noaa.gov/tcr/AL092022_Ian.pdf",
                            reporting_time="2022-12-15T00:00:00Z",
                        )
                    ],
                    consensus_value=4.0,
                    consensus_unit="category",
                    consensus_method="single_source",
                    consensus_confidence="high",
                ),
                max_winds_kt=ClaimSet(
                    claims=[
                        NumericClaim(
                            value=130.0,
                            unit="kt",
                            source_type="agency",
                            source_id="nhc-tcr-al092022",
                            source_url="https://www.nhc.noaa.gov/tcr/AL092022_Ian.pdf",
                            reporting_time="2022-12-15T00:00:00Z",
                        )
                    ],
                    consensus_value=130.0,
                    consensus_unit="kt",
                    consensus_method="single_source",
                    consensus_confidence="high",
                ),
                landfall_location="Cayo Costa, FL",
            )
        ),
        provenance=EventProvenance(
            article_ids=[article_id],
            primary_article_id=article_id,
        ),
        embedding=None,
        embedding_model="text-embedding-005",
        extracted_at="2026-06-05T12:00:00Z",
        extractor_version="hep-extractor-v0.1.0",
    )


def test_hurricane_event_roundtrip_idempotent() -> None:
    ev = _hurricane_event()
    dumped_a = ev.model_dump(mode="json")
    text_a = json.dumps(dumped_a, sort_keys=True)
    ev_b = EventMetadata.model_validate(json.loads(text_a))
    dumped_b = ev_b.model_dump(mode="json")
    text_b = json.dumps(dumped_b, sort_keys=True)
    assert text_a == text_b


def test_intensity_bare_float_rejected_decision_m() -> None:
    """Decision M: every numeric intensity quantity is a ClaimSet, never a bare number.

    Smuggling a bare float into a ClaimSet slot must fail validation.
    """
    with pytest.raises(ValidationError):
        HurricaneIntensity.model_validate(
            {
                "saffir_simpson": 4.0,  # bare float where a ClaimSet belongs
            }
        )


def test_intensity_bare_int_rejected_decision_m() -> None:
    with pytest.raises(ValidationError):
        RainfallIntensity.model_validate(
            {
                "total_inches": 12,  # bare number, not a ClaimSet
            }
        )


def test_non_numeric_intensity_field_stays_scalar() -> None:
    """Scalar non-numeric fields (landfall_location, river_name, gauge_id) are
    allowed to be plain strings — they are not claim-quantities."""
    hi = HurricaneIntensity(landfall_location="Cayo Costa, FL")
    assert hi.landfall_location == "Cayo Costa, FL"


def test_event_location_requires_bbox_or_place_name() -> None:
    """At least one of bbox or place_name is required (Appendix C)."""
    with pytest.raises(ValidationError):
        EventLocation()


def test_event_location_with_bbox_only_ok() -> None:
    loc = EventLocation(bbox=(-82.6, 26.4, -81.7, 27.0))
    assert loc.bbox is not None


def test_event_location_with_place_name_only_ok() -> None:
    loc = EventLocation(place_name="Springfield")
    assert loc.place_name == "Springfield"


def test_wrong_intensity_payload_for_event_type_rejected() -> None:
    """A hurricane event with a river_flood intensity payload must fail."""
    base = _hurricane_event().model_dump(mode="json")
    # Replace hurricane intensity with a river_flood intensity payload
    base["intensity"] = {
        "river_flood": {
            "river_name": "Caloosahatchee River",
            "peak_stage_ft": {
                "claims": [
                    {
                        "value": 18.0,
                        "unit": "ft",
                        "source_type": "agency",
                        "source_id": "x",
                        "source_url": "https://example.com",
                        "reporting_time": "2026-06-05T12:00:00Z",
                    }
                ],
                "consensus_value": 18.0,
                "consensus_unit": "ft",
                "consensus_method": "single_source",
                "consensus_confidence": "high",
            },
        }
    }
    with pytest.raises(ValidationError):
        EventMetadata.model_validate(base)


def test_event_with_empty_intensity_allowed() -> None:
    """Appendix C.4 + the dispatcher: zero-intensity payloads are valid (the
    extractor may find no quantitative claims), as long as no *wrong* payload is
    populated."""
    base = _hurricane_event().model_dump(mode="json")
    base["intensity"] = {}  # extractor found no quantitative claims yet
    ev = EventMetadata.model_validate(base)
    assert ev.intensity.hurricane is None


def test_intense_rainfall_maps_to_rainfall_payload() -> None:
    """The dispatcher maps event_type=intense_rainfall to the rainfall payload."""
    article_id = new_ulid()
    ev = EventMetadata(
        event_id=new_ulid(),
        event_type="intense_rainfall",
        confidence=0.8,
        location=EventLocation(place_name="Topeka, KS"),
        time_range={"start": "2026-06-05T00:00:00Z", "end": "2026-06-05T06:00:00Z"},
        time_classification="past",
        intensity=IntensityIndicators(
            rainfall=RainfallIntensity(
                total_inches=ClaimSet(
                    claims=[
                        NumericClaim(
                            value=8.0,
                            unit="inches",
                            source_type="agency",
                            source_id="nws-spc",
                            source_url="https://www.spc.noaa.gov/exper/lsr/",
                            reporting_time="2026-06-05T07:00:00Z",
                        )
                    ],
                    consensus_value=8.0,
                    consensus_unit="inches",
                    consensus_method="single_source",
                    consensus_confidence="high",
                )
            )
        ),
        provenance=EventProvenance(article_ids=[article_id], primary_article_id=article_id),
        extracted_at="2026-06-05T07:30:00Z",
        extractor_version="hep-extractor-v0.1.0",
    )
    dumped = ev.model_dump(mode="json")
    again = EventMetadata.model_validate(dumped)
    assert again.intensity.rainfall is not None


def test_river_flood_event_with_river_flood_intensity() -> None:
    article_id = new_ulid()
    ev = EventMetadata(
        event_id=new_ulid(),
        event_type="river_flood",
        confidence=0.85,
        location=EventLocation(place_name="Davenport, IA"),
        time_range={"start": "2026-04-01T00:00:00Z", "end": "2026-04-05T00:00:00Z"},
        time_classification="past",
        intensity=IntensityIndicators(
            river_flood=RiverFloodIntensity(
                river_name="Mississippi River",
                peak_stage_ft=ClaimSet(
                    claims=[
                        NumericClaim(
                            value=22.4,
                            unit="ft",
                            source_type="agency",
                            source_id="usgs-05420500",
                            source_url="https://waterdata.usgs.gov/nwis/uv?05420500",
                            reporting_time="2026-04-03T18:00:00Z",
                        )
                    ],
                    consensus_value=22.4,
                    consensus_unit="ft",
                    consensus_method="single_source",
                    consensus_confidence="high",
                ),
                gauge_id="05420500",
            )
        ),
        provenance=EventProvenance(article_ids=[article_id], primary_article_id=article_id),
        extracted_at="2026-04-04T00:00:00Z",
        extractor_version="hep-extractor-v0.1.0",
    )
    ev.model_dump(mode="json")  # smoke


def test_numeric_claim_source_type_closed_enum() -> None:
    """Source-type tier is data-driven (closed Literal), never LLM-judged."""
    with pytest.raises(ValidationError):
        NumericClaim(
            value=1.0,
            unit="x",
            source_type="my_own_tier",  # not in the closed Literal
            source_id="x",
            source_url="https://example.com",
            reporting_time="2026-06-05T12:00:00Z",
        )


def test_claim_set_consensus_value_round_trips() -> None:
    cs = ClaimSet(
        claims=[
            NumericClaim(
                value=4.0,
                unit="category",
                source_type="agency",
                source_id="a",
                source_url="https://example.com",
                reporting_time="2026-06-05T12:00:00Z",
            ),
            NumericClaim(
                value=4.0,
                unit="category",
                source_type="major_news",
                source_id="b",
                source_url="https://example.com",
                reporting_time="2026-06-05T13:00:00Z",
            ),
        ],
        consensus_value=4.0,
        consensus_unit="category",
        consensus_method="authority_weighted",
        consensus_confidence="high",
    )
    dumped = cs.model_dump(mode="json")
    again = ClaimSet.model_validate(dumped)
    assert again.consensus_value == 4.0
    assert again.consensus_method == "authority_weighted"
