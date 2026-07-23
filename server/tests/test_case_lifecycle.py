"""Unit tests for ``trid3nt_server.case_lifecycle`` (job-0121).

Coverage:
- ``test_ensure_case_qgs_returns_persisted_uri`` — a Case with
  ``qgs_project_uri`` already set short-circuits and returns it directly.
- ``test_ensure_case_qgs_raises_when_persistence_unbound`` — None
  Persistence raises ``CaseLifecycleError(PERSISTENCE_UNBOUND)``.
- ``test_ensure_case_qgs_raises_when_case_missing`` — missing Case raises
  ``CaseLifecycleError(CASE_NOT_FOUND)``.
- ``test_ensure_case_qgs_raises_when_uri_unset`` — a Case with no
  ``qgs_project_uri`` raises ``CaseLifecycleError(PER_CASE_QGS_UNAVAILABLE)``
  (per-Case ``.qgs`` provisioning is not implemented on the local build; the
  GCS-copy DI seam that used to mint one was removed outright — it was never
  bound in production, so this fail-fast always fired anyway).
"""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.case_lifecycle import CaseLifecycleError, ensure_case_qgs
from trid3nt_server.persistence import Persistence
from trid3nt_contracts.common import new_ulid

# Reuse the MockMCPClient from the persistence tests (the mock is generic).
from .test_persistence import MockMCPClient, _fresh_case_summary


def test_ensure_case_qgs_returns_persisted_uri() -> None:
    """A Case with ``qgs_project_uri`` already set: return it directly."""
    mock = MockMCPClient()
    persistence = Persistence(mock)
    case = _fresh_case_summary()
    pre_set = "s3://trid3nt-qgs/already-init.qgs"
    case = case.model_copy(update={"qgs_project_uri": pre_set})
    asyncio.run(persistence.upsert_case(case))

    result = asyncio.run(ensure_case_qgs(persistence, case.case_id))
    assert result == pre_set


def test_ensure_case_qgs_raises_when_persistence_unbound() -> None:
    """Passing ``None`` for persistence -> PERSISTENCE_UNBOUND."""
    with pytest.raises(CaseLifecycleError) as ei:
        asyncio.run(ensure_case_qgs(None, new_ulid()))
    assert ei.value.error_code == "PERSISTENCE_UNBOUND"


def test_ensure_case_qgs_raises_when_case_missing() -> None:
    """Missing Case -> CASE_NOT_FOUND."""
    mock = MockMCPClient()
    persistence = Persistence(mock)
    with pytest.raises(CaseLifecycleError) as ei:
        asyncio.run(ensure_case_qgs(persistence, new_ulid()))
    assert ei.value.error_code == "CASE_NOT_FOUND"


def test_ensure_case_qgs_raises_when_uri_unset() -> None:
    """A Case with no qgs_project_uri -> PER_CASE_QGS_UNAVAILABLE.

    Honest fail-fast: no GCS-copy seam exists to provision one. The
    ``server._invoke_tool_via_emitter`` call site catches this and falls
    back to the single-tenant default ``.qgs``.
    """
    mock = MockMCPClient()
    persistence = Persistence(mock)
    case = _fresh_case_summary()
    assert case.qgs_project_uri is None
    asyncio.run(persistence.upsert_case(case))

    with pytest.raises(CaseLifecycleError) as ei:
        asyncio.run(ensure_case_qgs(persistence, case.case_id))
    assert ei.value.error_code == "PER_CASE_QGS_UNAVAILABLE"
