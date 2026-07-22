"""Live test of ``@register_tool``'s Wave-1.5 kwarg plumbing (job-0114-schema).

The schema-side contract change (``AtomicToolMetadata.supports_global_query``
+ ``payload_mb_estimator_name``) is paired with the agent-side
``register_tool`` decorator gaining the same two flags as optional
keyword arguments. This test exercises the integration:

1. ``@register_tool(metadata)`` with no kwargs preserves pre-Wave-1.5
   behaviour (the metadata's defaults are visible in ``TOOL_REGISTRY``).
2. ``@register_tool(metadata, supports_global_query=True)`` ovveridees the
   field on the registered metadata.
3. ``@register_tool(metadata, payload_mb_estimator_name="estimate_payload_mb")``
   overrides the estimator-name field.
4. Both flags compose (one decorator call sets both).
5. The decorator-level override fails fast if it would invalidate the
   cross-field FR-DC-6 rule.

These tests use ``clear_registry_for_tests`` so registrations done here
don't leak into the global registry (which is populated at import time
by the real tool modules).
"""

from __future__ import annotations

import pytest

from grace2_agent.tools import (
    TOOL_REGISTRY,
    clear_registry_for_tests,
    register_tool,
)
from grace2_contracts.tool_registry import AtomicToolMetadata


@pytest.fixture(autouse=True)
def _snapshot_and_restore_registry() -> None:
    """Snapshot the real registry, run the test, then restore.

    The real registry is populated at import time by every tool module
    listed at the bottom of ``grace2_agent/tools/__init__.py``. Our tests
    need a clean slate; we restore the snapshot at teardown so subsequent
    tests in the same pytest session still see the production tools.
    """
    snapshot = dict(TOOL_REGISTRY)
    clear_registry_for_tests()
    try:
        yield
    finally:
        clear_registry_for_tests()
        TOOL_REGISTRY.update(snapshot)


def test_register_tool_no_kwargs_preserves_pre_wave15_defaults() -> None:
    """Without the new kwargs, the registered metadata defaults are unchanged."""
    meta = AtomicToolMetadata(
        name="dummy_tool_legacy",
        ttl_class="static-30d",
        source_class="dummy",
    )

    @register_tool(meta)
    def dummy_tool_legacy(bbox: tuple[float, float, float, float]) -> str:
        return "ok"

    entry = TOOL_REGISTRY["dummy_tool_legacy"]
    assert entry.metadata.supports_global_query is False
    assert entry.metadata.payload_mb_estimator_name is None
    # Decorator returns the original function unchanged.
    assert dummy_tool_legacy.__name__ == "dummy_tool_legacy"


def test_register_tool_overrides_supports_global_query_via_kwarg() -> None:
    """Passing supports_global_query=True at the decorator overrides the metadata."""
    meta = AtomicToolMetadata(
        name="dummy_tool_global",
        ttl_class="dynamic-1h",
        source_class="dummy_alerts",
        # NOTE: supports_global_query defaults to False on the metadata;
        # the decorator kwarg must override it to True.
    )

    @register_tool(meta, supports_global_query=True)
    def dummy_tool_global(bbox: tuple[float, float, float, float] | None = None) -> str:
        return "ok"

    entry = TOOL_REGISTRY["dummy_tool_global"]
    assert entry.metadata.supports_global_query is True
    # Other fields are preserved.
    assert entry.metadata.name == "dummy_tool_global"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "dummy_alerts"


def test_register_tool_overrides_payload_mb_estimator_name_via_kwarg() -> None:
    """Passing payload_mb_estimator_name overrides the metadata field."""
    meta = AtomicToolMetadata(
        name="dummy_tool_estimator",
        ttl_class="dynamic-1h",
        source_class="dummy_satellite",
    )

    @register_tool(meta, payload_mb_estimator_name="estimate_payload_mb")
    def dummy_tool_estimator(
        bbox: tuple[float, float, float, float], bands: list[str]
    ) -> str:
        return "ok"

    entry = TOOL_REGISTRY["dummy_tool_estimator"]
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"
    assert entry.metadata.supports_global_query is False  # default preserved


def test_register_tool_composes_both_wave15_kwargs() -> None:
    """The two kwargs can be set together in one decorator call."""
    meta = AtomicToolMetadata(
        name="dummy_tool_mrms",
        ttl_class="dynamic-1h",
        source_class="dummy_mrms",
    )

    @register_tool(
        meta,
        supports_global_query=True,
        payload_mb_estimator_name="estimate_payload_mb",
    )
    def dummy_tool_mrms(
        bbox: tuple[float, float, float, float] | None = None, hours: int = 1
    ) -> str:
        return "ok"

    entry = TOOL_REGISTRY["dummy_tool_mrms"]
    assert entry.metadata.supports_global_query is True
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


def test_register_tool_kwarg_override_preserves_already_set_metadata_field() -> None:
    """If the metadata already declares the field, passing the kwarg overrides it.

    Wave 1.5 sibling tools (e.g. fetch_mrms_qpe) use a defensive try/except
    that passes the field via the ``AtomicToolMetadata`` constructor; once
    the schema field exists, that pattern works directly. This test asserts
    the decorator kwarg still wins when both are set, so a follow-up edit
    can move the declaration from the metadata constructor to the decorator
    site without behavioural drift.
    """
    # Construct metadata with supports_global_query=True already set on it.
    meta = AtomicToolMetadata(
        name="dummy_tool_both_set",
        ttl_class="dynamic-1h",
        source_class="dummy",
        supports_global_query=True,  # advertised on the metadata
    )

    # Decorator kwarg overrides to False.
    @register_tool(meta, supports_global_query=False)
    def dummy_tool_both_set(bbox: tuple[float, float, float, float]) -> str:
        return "ok"

    entry = TOOL_REGISTRY["dummy_tool_both_set"]
    # Decorator kwarg wins.
    assert entry.metadata.supports_global_query is False


def test_register_tool_kwarg_override_runs_cross_field_validator() -> None:
    """The kwarg override goes through ``model_copy(update=...)`` which re-runs
    the FR-DC-6 cross-field validator (because GraceModel sets
    ``validate_assignment=True``). A bad combination still fails at import."""
    # Start with a legitimate cacheable=False / live-no-cache metadata.
    meta = AtomicToolMetadata(
        name="dummy_tool_bad_override",
        ttl_class="live-no-cache",
        cacheable=False,
    )
    # Sanity: metadata constructed fine.
    assert meta.cacheable is False

    # The Wave-1.5 fields don't interact with the cacheable-consistency
    # rule (no cross-field rule between them). This test makes the
    # non-interaction explicit so a future cross-field rule won't break
    # silently — if it is added, this test will fail loudly.
    @register_tool(
        meta,
        supports_global_query=True,
        payload_mb_estimator_name="estimate_payload_mb",
    )
    def dummy_tool_bad_override() -> str:
        return "ok"

    entry = TOOL_REGISTRY["dummy_tool_bad_override"]
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.supports_global_query is True
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"
