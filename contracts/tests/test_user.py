"""Round-trip and invariant tests for the ``User`` schema.

The JSON round trip is byte-identical; only ``user_id`` and ``created_at`` are
required and the rest default; extra fields are refused, so a new field is
explicit; a syntactically invalid ULID is refused; no cost, spend or quota field
can be added; datetimes serialize with the ``Z`` suffix."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.user import User


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _fresh_user(
    *,
    email: str | None = "natealmanza3@gmail.com",
    display_name: str | None = "Nate Almanza",
) -> User:
    return User(
        user_id=new_ulid(),
        email=email,
        display_name=display_name,
        created_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        is_active=True,
        prefs={"theme": "dark", "map_basemap": "esri"},
    )


# --------------------------------------------------------------------------- #
# 1. JSON round-trip
# --------------------------------------------------------------------------- #


def test_user_roundtrip_idempotent() -> None:
    """JSON serialize -> deserialize -> re-serialize is byte-identical."""
    u = _fresh_user()
    a = u.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = User.model_validate(json.loads(text_a)).model_dump(mode="json")
    text_b = json.dumps(b, sort_keys=True)
    assert text_a == text_b, "non-idempotent round-trip"

    # Wire-shape sanity
    assert a["schema_version"] == "v1"
    assert a["is_active"] is True
    assert a["created_at"].endswith("Z")
    assert a["prefs"] == {"theme": "dark", "map_basemap": "esri"}


# --------------------------------------------------------------------------- #
# 2. Minimal construction
# --------------------------------------------------------------------------- #


def test_user_defaults_minimal_construction() -> None:
    """Only ``user_id`` and ``created_at`` are required; everything else defaults.

    The surface stays permissive so the persistence layer can write a partial record."""
    u = User(
        user_id=new_ulid(),
        created_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert not hasattr(u, "firebase_uid")
    assert u.email is None
    assert u.display_name is None
    assert u.is_active is True
    assert u.prefs == {}


# --------------------------------------------------------------------------- #
# 3. extra="forbid" — silent drift caught
# --------------------------------------------------------------------------- #


def test_user_rejects_extra_fields() -> None:
    """``extra='forbid'`` rejects unknown fields — the Auth track adds
    fields explicitly, not by accident.
    """
    base = _fresh_user().model_dump(mode="json")
    for forbidden in ("roles", "claims", "tenant_id", "subscription_tier"):
        bad = {**base, forbidden: "x"}
        with pytest.raises(ValidationError, match="(?i)extra"):
            User.model_validate(bad)


# --------------------------------------------------------------------------- #
# 4. ULID validation
# --------------------------------------------------------------------------- #


def test_user_invalid_ulid_rejected() -> None:
    """``user_id`` must be a syntactically valid ULID."""
    with pytest.raises(ValidationError):
        User(
            user_id="not-a-ulid",
            created_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        )
    with pytest.raises(ValidationError):
        User(
            user_id="",
            created_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        )


# --------------------------------------------------------------------------- #
# 5. Invariant 9: no cost / quota / spend fields
# --------------------------------------------------------------------------- #


def test_user_no_cost_or_quota_fields_invariant9() -> None:
    """Invariant 9 negative control: cost / quota / spend fields rejected."""
    base = _fresh_user().model_dump(mode="json")
    for forbidden in (
        "cost_usd",
        "estimated_cost",
        "monthly_spend",
        "quota_remaining",
        "subscription_cost",
    ):
        bad = {**base, forbidden: 0.0}
        with pytest.raises(ValidationError, match="(?i)extra"):
            User.model_validate(bad)


# --------------------------------------------------------------------------- #
# 6. UTC ``Z`` suffix discipline
# --------------------------------------------------------------------------- #


def test_user_datetime_serializes_with_z_suffix() -> None:
    """Every datetime field serializes to ISO-8601 with a ``Z`` suffix.

    Mirrors the cross-cutting UTCDatetime serializer convention; a regression
    that drops the ``Z`` would break the wire shape uniformly.
    """
    u = _fresh_user()
    a = u.model_dump(mode="json")
    assert a["created_at"].endswith("Z")
    assert "+00:00" not in a["created_at"]
