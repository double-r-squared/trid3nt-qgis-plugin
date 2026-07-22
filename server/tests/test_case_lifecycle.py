"""Unit tests for ``trid3nt_server.case_lifecycle`` (job-0121).

Coverage:
- ``test_case_qgs_uri_default_bucket`` — pure function builds the URI.
- ``test_case_qgs_uri_env_override`` — env var overrides bucket.
- ``test_ensure_case_qgs_lazy_init_copies_template`` — first publish copies
  the template ``.qgs`` to the case-scoped path and persists the URI.
- ``test_ensure_case_qgs_second_call_short_circuits`` — second publish
  uses the persisted URI; does NOT re-copy.
- ``test_ensure_case_qgs_returns_persisted_uri_on_no_copy`` — short-circuit
  returns the durable URI directly.
- ``test_ensure_case_qgs_raises_when_persistence_unbound`` — None
  Persistence raises ``CaseLifecycleError(PERSISTENCE_UNBOUND)``.
- ``test_ensure_case_qgs_raises_when_case_missing`` — missing Case raises
  ``CaseLifecycleError(CASE_NOT_FOUND)``.
- ``test_ensure_case_qgs_raises_when_copy_unbound`` — no GCS copier raises
  ``CaseLifecycleError(GCS_COPY_UNBOUND)``.
- ``test_ensure_case_qgs_wraps_copy_failure`` — copier exception wrapped as
  ``GCS_COPY_FAILED``.
- ``test_ensure_case_qgs_async_copier_awaited`` — async copier coroutine is
  awaited.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest

from trid3nt_server.case_lifecycle import (
    DEFAULT_CASE_QGS_BUCKET,
    CaseLifecycleError,
    case_qgs_uri,
    ensure_case_qgs,
    set_gcs_copy,
)
from trid3nt_server.persistence import Persistence
from trid3nt_contracts.case import CaseSummary
from trid3nt_contracts.common import new_ulid

# Reuse the MockMCPClient from the persistence tests (the mock is generic).
from .test_persistence import MockMCPClient, _fresh_case_summary


# --------------------------------------------------------------------------- #
# Pure-function tests
# --------------------------------------------------------------------------- #


def test_case_qgs_uri_default_bucket() -> None:
    """``case_qgs_uri`` builds ``gs://<bucket>/<case_id>.qgs`` with the default."""
    cid = new_ulid()
    uri = case_qgs_uri(cid)
    assert uri == f"gs://{DEFAULT_CASE_QGS_BUCKET}/{cid}.qgs"


def test_case_qgs_uri_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``TRID3NT_CASE_QGS_BUCKET`` env var overrides the bucket."""
    monkeypatch.setenv("TRID3NT_CASE_QGS_BUCKET", "alt-bucket")
    cid = new_ulid()
    uri = case_qgs_uri(cid)
    assert uri == f"gs://alt-bucket/{cid}.qgs"


# --------------------------------------------------------------------------- #
# Mock GCS copier — records every (template, target) tuple it sees.
# --------------------------------------------------------------------------- #


class _MockCopier:
    """In-memory GCS copy stand-in.

    Records every ``(template, target)`` tuple so tests can assert the
    lazy-init was actually invoked (not just short-circuited).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, template_uri: str, target_uri: str) -> str:
        self.calls.append((template_uri, target_uri))
        return target_uri


@pytest.fixture()
def _bind_copier():
    """Bind a fresh ``_MockCopier`` for the test; clear on teardown."""
    copier = _MockCopier()
    set_gcs_copy(copier)
    try:
        yield copier
    finally:
        set_gcs_copy(None)


# --------------------------------------------------------------------------- #
# Lazy-init path
# --------------------------------------------------------------------------- #


def test_ensure_case_qgs_lazy_init_copies_template(_bind_copier: _MockCopier) -> None:
    """First publish: copies template -> case-scoped path; persists the URI."""
    mock = MockMCPClient()
    persistence = Persistence(mock)
    case = _fresh_case_summary()
    assert case.qgs_project_uri is None
    asyncio.run(persistence.upsert_case(case))

    result = asyncio.run(ensure_case_qgs(persistence, case.case_id))
    expected = case_qgs_uri(case.case_id)
    assert result == expected

    # Copy was invoked exactly once
    assert len(_bind_copier.calls) == 1
    template_uri, target_uri = _bind_copier.calls[0]
    # The copier receives the canonical template (the live s3 default).
    assert template_uri == "s3://trid3nt-qgs/sample.qgs"
    assert target_uri == expected

    # Persistence shows the URI now stored
    updated = asyncio.run(persistence.get_case(case.case_id))
    assert updated is not None
    assert updated.qgs_project_uri == expected


def test_ensure_case_qgs_second_call_short_circuits(_bind_copier: _MockCopier) -> None:
    """Second publish: returns persisted URI; does NOT re-copy."""
    mock = MockMCPClient()
    persistence = Persistence(mock)
    case = _fresh_case_summary()
    asyncio.run(persistence.upsert_case(case))

    first = asyncio.run(ensure_case_qgs(persistence, case.case_id))
    second = asyncio.run(ensure_case_qgs(persistence, case.case_id))

    assert first == second
    # Only the FIRST call invoked the copier
    assert len(_bind_copier.calls) == 1


def test_ensure_case_qgs_returns_persisted_uri_on_no_copy() -> None:
    """If a Case already has ``qgs_project_uri`` set, we return it directly."""
    mock = MockMCPClient()
    persistence = Persistence(mock)
    case = _fresh_case_summary()
    pre_set = "s3://trid3nt-qgs/already-init.qgs"
    case = case.model_copy(update={"qgs_project_uri": pre_set})
    asyncio.run(persistence.upsert_case(case))

    # No copier bound; should still succeed because we short-circuit.
    set_gcs_copy(None)
    try:
        result = asyncio.run(ensure_case_qgs(persistence, case.case_id))
        assert result == pre_set
    finally:
        set_gcs_copy(None)


# --------------------------------------------------------------------------- #
# Error paths
# --------------------------------------------------------------------------- #


def test_ensure_case_qgs_raises_when_persistence_unbound() -> None:
    """Passing ``None`` for persistence -> PERSISTENCE_UNBOUND."""
    with pytest.raises(CaseLifecycleError) as ei:
        asyncio.run(ensure_case_qgs(None, new_ulid()))
    assert ei.value.error_code == "PERSISTENCE_UNBOUND"


def test_ensure_case_qgs_raises_when_case_missing(_bind_copier: _MockCopier) -> None:
    """Missing Case -> CASE_NOT_FOUND."""
    mock = MockMCPClient()
    persistence = Persistence(mock)
    with pytest.raises(CaseLifecycleError) as ei:
        asyncio.run(ensure_case_qgs(persistence, new_ulid()))
    assert ei.value.error_code == "CASE_NOT_FOUND"


def test_ensure_case_qgs_raises_when_copy_unbound() -> None:
    """No GCS copier bound -> GCS_COPY_UNBOUND."""
    mock = MockMCPClient()
    persistence = Persistence(mock)
    case = _fresh_case_summary()
    asyncio.run(persistence.upsert_case(case))
    set_gcs_copy(None)
    with pytest.raises(CaseLifecycleError) as ei:
        asyncio.run(ensure_case_qgs(persistence, case.case_id))
    assert ei.value.error_code == "GCS_COPY_UNBOUND"


def test_ensure_case_qgs_wraps_copy_failure() -> None:
    """Underlying copier exception -> GCS_COPY_FAILED with original chained."""

    def _boom(template_uri: str, target_uri: str) -> str:
        raise RuntimeError("403 Forbidden: pretend SA can't write to bucket")

    set_gcs_copy(_boom)
    try:
        mock = MockMCPClient()
        persistence = Persistence(mock)
        case = _fresh_case_summary()
        asyncio.run(persistence.upsert_case(case))
        with pytest.raises(CaseLifecycleError) as ei:
            asyncio.run(ensure_case_qgs(persistence, case.case_id))
        assert ei.value.error_code == "GCS_COPY_FAILED"
        # Original exception is in the cause chain
        assert "403 Forbidden" in str(ei.value.__cause__)
    finally:
        set_gcs_copy(None)


def test_ensure_case_qgs_async_copier_awaited() -> None:
    """An async copier coroutine is awaited and its result is persisted."""

    async def _async_copier(template_uri: str, target_uri: str) -> str:
        await asyncio.sleep(0)
        return target_uri

    set_gcs_copy(_async_copier)
    try:
        mock = MockMCPClient()
        persistence = Persistence(mock)
        case = _fresh_case_summary()
        asyncio.run(persistence.upsert_case(case))

        result = asyncio.run(ensure_case_qgs(persistence, case.case_id))
        assert result == case_qgs_uri(case.case_id)

        updated = asyncio.run(persistence.get_case(case.case_id))
        assert updated is not None
        assert updated.qgs_project_uri == result
    finally:
        set_gcs_copy(None)
