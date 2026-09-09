"""The User account record - one document per account.

A forward-looking stub carrying only the fields the persistence layer demands;
growth is additive and ``extra="forbid"`` catches drift at the next
``model_validate``. No quota, usage or spend field ever lands here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import (
    GraceModel,
    ULIDStr,
    UTCDatetime,
)

__all__ = [
    "User",
]


class User(GraceModel):
    """An authenticated user - ``user_id`` is the key every per-user join uses.
    ``prefs`` is an OPEN dict with no per-key validation: unknown keys on a
    persisted row load back unchanged."""

    schema_version: Literal["v1"] = "v1"

    user_id: ULIDStr
    email: str | None = Field(default=None, max_length=320)  # RFC 5321 limit
    display_name: str | None = Field(default=None, max_length=200)
    created_at: UTCDatetime
    # False soft-deactivates: dispatch is refused on every Case this user owns.
    is_active: bool = True
    prefs: dict = Field(default_factory=dict)
    # An anonymous (no-IdP) User carries no credential; the single-user build
    # provisions its one fixed user this way.
    is_anonymous: bool = False
