"""Tests for ImpactEnvelope and OccupancyClassImpact (SRS Appendix B.6c.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from grace2_contracts.common import new_ulid
from grace2_contracts.impact_envelope import (
    ImpactEnvelope,
    OccupancyClassImpact,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_VALID_PELICUN_RUN_ID = new_ulid()

_VALID_BY_OCCUPANCY = {
    "RES1": OccupancyClassImpact(
        n_structures=612,
        n_damaged=318,
        n_destroyed=32,
        expected_loss_usd=20_140_000.0,
        loss_percentile_95_usd=35_600_000.0,
        population=9840,
        population_displaced=4210,
    ),
    "COM1": OccupancyClassImpact(
        n_structures=143,
        n_damaged=87,
        n_destroyed=10,
        expected_loss_usd=7_400_000.0,
        loss_percentile_95_usd=13_200_000.0,
        population=820,
        population_displaced=560,
    ),
}

_VALID_DAMAGE_STATE_DISTRIBUTION = {
    "DS0_none": 415,
    "DS1_slight": 183,
    "DS2_moderate": 142,
    "DS3_extensive": 63,
    "DS4_complete": 44,
}


def _representative_payload(**overrides) -> dict:
    """Return a valid ImpactEnvelope kwargs dict, with optional overrides."""
    base = dict(
        n_structures_total=847,
        n_structures_damaged=432,
        n_structures_destroyed=44,
        damage_state_distribution=_VALID_DAMAGE_STATE_DISTRIBUTION.copy(),
        total_replacement_value_usd=211_750_000.0,
        damaged_replacement_value_usd=108_000_000.0,
        expected_loss_usd=29_245_000.0,
        loss_percentile_95_usd=51_840_000.0,
        population_total=11_200,
        population_displaced=4_980,
        population_at_high_risk=1_870,
        impact_area_km2=8.4,
        bbox=(-82.10, 26.40, -81.60, 26.90),
        by_occupancy_class=_VALID_BY_OCCUPANCY,
        pelicun_run_id=_VALID_PELICUN_RUN_ID,
        damage_layer_uri="gs://trid3nt-cache-fixture/pelicun_damage/01KTJX71-hash.fgb",
        structure_inventory_source="USACE_NSI",
        flood_layer_uri="gs://legacy-cloud-runs/01KTJX71NKGDMXB9TN0DV75JWK/flood_depth_peak.tif",
        fragility_set="hazus_flood_v6",
        realization_count=100,
        generated_at="2026-06-09T14:35:22Z",
    )
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Round-trip: representative payload
# --------------------------------------------------------------------------- #


def test_representative_payload_accepts():
    """A fully-populated representative payload parses without error."""
    envelope = ImpactEnvelope(**_representative_payload())
    assert envelope.schema_version == "v1"
    assert envelope.n_structures_total == 847
    assert envelope.n_structures_damaged == 432
    assert envelope.n_structures_destroyed == 44
    assert envelope.damage_state_distribution["DS0_none"] == 415
    assert envelope.damage_state_distribution["DS4_complete"] == 44
    assert envelope.expected_loss_usd == pytest.approx(29_245_000.0)
    assert envelope.loss_percentile_95_usd == pytest.approx(51_840_000.0)
    assert envelope.population_total == 11_200
    assert envelope.population_displaced == 4_980
    assert envelope.population_at_high_risk == 1_870
    assert envelope.impact_area_km2 == pytest.approx(8.4)
    assert envelope.bbox == (-82.10, 26.40, -81.60, 26.90)
    assert "RES1" in envelope.by_occupancy_class
    assert envelope.by_occupancy_class["RES1"].n_structures == 612
    assert envelope.structure_inventory_source == "USACE_NSI"
    assert envelope.fragility_set == "hazus_flood_v6"
    assert envelope.realization_count == 100


def test_round_trip_json():
    """model_dump(mode='json') → model_validate round-trip preserves all fields."""
    original = ImpactEnvelope(**_representative_payload())
    dumped = original.model_dump(mode="json")
    restored = ImpactEnvelope.model_validate(dumped)
    assert restored.pelicun_run_id == original.pelicun_run_id
    assert restored.n_structures_total == original.n_structures_total
    assert restored.expected_loss_usd == pytest.approx(original.expected_loss_usd)
    assert restored.generated_at == original.generated_at


def test_generated_at_serializes_with_z():
    """generated_at serializes to ISO-8601 with a Z suffix (UTCDatetime convention)."""
    envelope = ImpactEnvelope(**_representative_payload())
    dumped = envelope.model_dump(mode="json")
    assert dumped["generated_at"].endswith("Z"), (
        f"generated_at should end with 'Z', got: {dumped['generated_at']!r}"
    )


def test_population_fields_none_for_ms_buildings():
    """population_total/displaced/at_high_risk can be None (MS_BUILDINGS source)."""
    envelope = ImpactEnvelope(
        **_representative_payload(
            structure_inventory_source="MS_BUILDINGS",
            population_total=None,
            population_displaced=None,
            population_at_high_risk=None,
            by_occupancy_class={
                "RES1": OccupancyClassImpact(
                    n_structures=612,
                    n_damaged=318,
                    n_destroyed=32,
                    expected_loss_usd=20_140_000.0,
                    loss_percentile_95_usd=35_600_000.0,
                    population=None,
                    population_displaced=None,
                ),
            },
        )
    )
    assert envelope.population_total is None
    assert envelope.population_displaced is None
    assert envelope.population_at_high_risk is None
    assert envelope.by_occupancy_class["RES1"].population is None


# --------------------------------------------------------------------------- #
# Missing required fields
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "missing_field",
    [
        "n_structures_total",
        "n_structures_damaged",
        "n_structures_destroyed",
        "damage_state_distribution",
        "total_replacement_value_usd",
        "damaged_replacement_value_usd",
        "expected_loss_usd",
        "loss_percentile_95_usd",
        "impact_area_km2",
        "bbox",
        "by_occupancy_class",
        "pelicun_run_id",
        "damage_layer_uri",
        "structure_inventory_source",
        "flood_layer_uri",
        "fragility_set",
        "realization_count",
        "generated_at",
    ],
)
def test_missing_required_field_raises(missing_field: str):
    """Omitting any required field raises a ValidationError."""
    payload = _representative_payload()
    del payload[missing_field]
    with pytest.raises(ValidationError):
        ImpactEnvelope(**payload)


# --------------------------------------------------------------------------- #
# Negative counts / negative losses
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("n_structures_total", -1),
        ("n_structures_damaged", -5),
        ("n_structures_destroyed", -1),
        ("total_replacement_value_usd", -0.01),
        ("damaged_replacement_value_usd", -100.0),
        ("expected_loss_usd", -1.0),
        ("loss_percentile_95_usd", -1.0),
        ("impact_area_km2", -0.001),
        ("population_total", -1),
        ("population_displaced", -1),
        ("population_at_high_risk", -1),
        ("realization_count", 0),
        ("realization_count", -10),
    ],
)
def test_negative_count_or_loss_raises(field: str, bad_value):
    """Negative counts, negative losses, and zero realization_count are rejected."""
    with pytest.raises(ValidationError):
        ImpactEnvelope(**_representative_payload(**{field: bad_value}))


# --------------------------------------------------------------------------- #
# OccupancyClassImpact negative values
# --------------------------------------------------------------------------- #


def test_occupancy_class_negative_n_structures_raises():
    """Negative n_structures in OccupancyClassImpact is rejected."""
    with pytest.raises(ValidationError):
        OccupancyClassImpact(
            n_structures=-1,
            n_damaged=0,
            n_destroyed=0,
            expected_loss_usd=0.0,
            loss_percentile_95_usd=0.0,
        )


def test_occupancy_class_negative_loss_raises():
    """Negative expected_loss_usd in OccupancyClassImpact is rejected."""
    with pytest.raises(ValidationError):
        OccupancyClassImpact(
            n_structures=10,
            n_damaged=5,
            n_destroyed=1,
            expected_loss_usd=-500.0,
            loss_percentile_95_usd=1000.0,
        )


def test_occupancy_class_negative_population_raises():
    """Negative population in OccupancyClassImpact is rejected."""
    with pytest.raises(ValidationError):
        OccupancyClassImpact(
            n_structures=10,
            n_damaged=5,
            n_destroyed=1,
            expected_loss_usd=500.0,
            loss_percentile_95_usd=1000.0,
            population=-1,
        )


# --------------------------------------------------------------------------- #
# Invalid provenance strings
# --------------------------------------------------------------------------- #


def test_empty_damage_layer_uri_raises():
    """An empty damage_layer_uri string is rejected (min_length=1)."""
    with pytest.raises(ValidationError):
        ImpactEnvelope(**_representative_payload(damage_layer_uri=""))


def test_empty_flood_layer_uri_raises():
    """An empty flood_layer_uri string is rejected (min_length=1)."""
    with pytest.raises(ValidationError):
        ImpactEnvelope(**_representative_payload(flood_layer_uri=""))


def test_invalid_structure_inventory_source_raises():
    """An unrecognized structure_inventory_source Literal value is rejected."""
    with pytest.raises(ValidationError):
        ImpactEnvelope(**_representative_payload(structure_inventory_source="GOOGLE_MAPS"))


def test_invalid_pelicun_run_id_raises():
    """A malformed ULID string for pelicun_run_id is rejected."""
    with pytest.raises(ValidationError):
        ImpactEnvelope(**_representative_payload(pelicun_run_id="not-a-ulid"))


# --------------------------------------------------------------------------- #
# bbox validation
# --------------------------------------------------------------------------- #


def test_bbox_inverted_lon_raises():
    """A bbox where minLon > maxLon is rejected by the BBox validator."""
    with pytest.raises(ValidationError):
        ImpactEnvelope(**_representative_payload(bbox=(-81.0, 26.40, -82.0, 26.90)))


def test_bbox_inverted_lat_raises():
    """A bbox where minLat > maxLat is rejected by the BBox validator."""
    with pytest.raises(ValidationError):
        ImpactEnvelope(**_representative_payload(bbox=(-82.10, 27.0, -81.60, 26.40)))


def test_bbox_out_of_range_lon_raises():
    """A bbox with lon outside [-180, 180] is rejected."""
    with pytest.raises(ValidationError):
        ImpactEnvelope(**_representative_payload(bbox=(-200.0, 26.40, -81.60, 26.90)))


# --------------------------------------------------------------------------- #
# extra="forbid" via GraceModel
# --------------------------------------------------------------------------- #


def test_extra_field_raises():
    """GraceModel.extra='forbid': an unknown field name is rejected."""
    with pytest.raises(ValidationError):
        ImpactEnvelope(**_representative_payload(unknown_field_xyz="oops"))


# --------------------------------------------------------------------------- #
# Minimal zero-damage case
# --------------------------------------------------------------------------- #


def test_zero_damage_all_ds0():
    """All structures in DS0 (no damage) is a valid degenerate case."""
    envelope = ImpactEnvelope(
        **_representative_payload(
            n_structures_damaged=0,
            n_structures_destroyed=0,
            damage_state_distribution={
                "DS0_none": 847,
                "DS1_slight": 0,
                "DS2_moderate": 0,
                "DS3_extensive": 0,
                "DS4_complete": 0,
            },
            expected_loss_usd=0.0,
            loss_percentile_95_usd=0.0,
            damaged_replacement_value_usd=0.0,
            population_displaced=0,
            population_at_high_risk=0,
            impact_area_km2=0.0,
            by_occupancy_class={
                "RES1": OccupancyClassImpact(
                    n_structures=847,
                    n_damaged=0,
                    n_destroyed=0,
                    expected_loss_usd=0.0,
                    loss_percentile_95_usd=0.0,
                    population=11_200,
                    population_displaced=0,
                ),
            },
        )
    )
    assert envelope.n_structures_damaged == 0
    assert envelope.expected_loss_usd == 0.0
    assert envelope.impact_area_km2 == 0.0
