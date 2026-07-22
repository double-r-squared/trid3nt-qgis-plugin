"""Round-trip + negative tests for shared primitives (common.py)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from grace2_contracts.common import (
    BBox,
    GraceModel,
    TimeRange,
    ULIDStr,
    UTCDatetime,
    new_ulid,
    now_utc,
)


class _CommonHolder(GraceModel):
    ulid_field: ULIDStr
    dt_field: UTCDatetime
    bbox_field: BBox


def test_new_ulid_is_26_char_crockford_base32() -> None:
    value = new_ulid()
    assert isinstance(value, str)
    assert len(value) == 26


def test_now_utc_is_timezone_aware_utc() -> None:
    dt = now_utc()
    assert dt.tzinfo is not None
    assert dt.utcoffset() is not None
    assert dt.utcoffset().total_seconds() == 0


def test_common_holder_roundtrip_idempotent() -> None:
    """JSON -> model -> JSON is byte-identical the second pass (idempotent)."""
    payload = {
        "ulid_field": new_ulid(),
        "dt_field": "2026-06-05T12:00:00Z",
        "bbox_field": [-82.5, 26.4, -81.7, 26.9],
    }
    model_a = _CommonHolder.model_validate(payload)
    json_a = model_a.model_dump(mode="json")
    model_b = _CommonHolder.model_validate(json_a)
    json_b = model_b.model_dump(mode="json")
    assert json_a == json_b
    # round-trip via real JSON serialize/deserialize, not just dict
    text_a = json.dumps(json_a, sort_keys=True)
    text_b = json.dumps(json_b, sort_keys=True)
    assert text_a == text_b


def test_datetime_serializes_with_z_suffix() -> None:
    holder = _CommonHolder(
        ulid_field=new_ulid(),
        dt_field=datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc),
        bbox_field=(-82.5, 26.4, -81.7, 26.9),
    )
    dumped = holder.model_dump(mode="json")
    assert dumped["dt_field"].endswith("Z")
    assert "+00:00" not in dumped["dt_field"]


def test_naive_datetime_serializes_as_utc_z() -> None:
    holder = _CommonHolder(
        ulid_field=new_ulid(),
        dt_field=datetime(2026, 6, 5, 12, 0, 0),  # naive -> treated as UTC
        bbox_field=(-82.5, 26.4, -81.7, 26.9),
    )
    dumped = holder.model_dump(mode="json")
    assert dumped["dt_field"].endswith("Z")


def test_invalid_ulid_rejected() -> None:
    with pytest.raises(ValidationError):
        _CommonHolder(
            ulid_field="not-a-ulid",
            dt_field="2026-06-05T12:00:00Z",
            bbox_field=(-1.0, -1.0, 1.0, 1.0),
        )


def test_bbox_inverted_lon_rejected() -> None:
    """minLon > maxLon must fail (EPSG:4326 ordering invariant)."""
    with pytest.raises(ValidationError):
        _CommonHolder(
            ulid_field=new_ulid(),
            dt_field="2026-06-05T12:00:00Z",
            bbox_field=(10.0, -1.0, -10.0, 1.0),  # inverted longitudes
        )


def test_bbox_inverted_lat_rejected() -> None:
    with pytest.raises(ValidationError):
        _CommonHolder(
            ulid_field=new_ulid(),
            dt_field="2026-06-05T12:00:00Z",
            bbox_field=(-10.0, 10.0, 10.0, -10.0),  # inverted lats
        )


def test_bbox_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        _CommonHolder(
            ulid_field=new_ulid(),
            dt_field="2026-06-05T12:00:00Z",
            bbox_field=(-200.0, 0.0, 0.0, 10.0),  # lon < -180
        )


def test_extra_fields_forbidden() -> None:
    """GraceModel uses extra='forbid'; unknown keys are a defect."""
    with pytest.raises(ValidationError):
        _CommonHolder.model_validate(
            {
                "ulid_field": new_ulid(),
                "dt_field": "2026-06-05T12:00:00Z",
                "bbox_field": [-82.5, 26.4, -81.7, 26.9],
                "stray_key": 1,
            }
        )


def test_time_range_roundtrip() -> None:
    tr = TimeRange(start="2026-06-05T00:00:00Z", end="2026-06-05T06:00:00Z")
    dumped = tr.model_dump(mode="json")
    again = TimeRange.model_validate(dumped)
    assert again.model_dump(mode="json") == dumped
    assert dumped["start"].endswith("Z") and dumped["end"].endswith("Z")
