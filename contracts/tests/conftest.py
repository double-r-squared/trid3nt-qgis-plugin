"""Shared pytest fixtures for the GRACE-2 contracts test suite.

The fixtures are intentionally minimal: they build realistic instances of each
top-level contract model that test modules round-trip through JSON. The single
source of authority for shapes is the SRS v0.3 Appendices A-D — these fixtures
are the smallest-possible "real" examples consistent with those appendices.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from grace2_contracts.common import new_ulid


@pytest.fixture()
def session_id() -> str:
    return new_ulid()


@pytest.fixture()
def now_z() -> datetime:
    return datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
