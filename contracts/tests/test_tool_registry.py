"""Tests for ``AtomicToolMetadata`` (FR-DC-2, FR-CE-8, FR-AS-3).

job-0030-schema-20260606 (sprint-06 / M4 pre-flight). Verifies:
- All four TTL classes are accepted on a cacheable tool with a source_class.
- The ``live-no-cache`` class round-trips on an uncacheable tool (FR-DC-6).
- The cross-field ``model_validator`` rejects the two inconsistent combos.
- ``source_class`` is required when ``cacheable=True``.
- JSON serialize → deserialize → re-serialize is idempotent.
- ``extra="forbid"`` is inherited via ``GraceModel``.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from grace2_contracts.tool_registry import (
    TTL_CLASSES,
    AtomicToolMetadata,
)


# --- TTL class coverage --- #


@pytest.mark.parametrize(
    ("ttl_class", "cacheable", "source_class"),
    [
        ("static-30d", True, "dem"),
        ("semi-static-7d", True, "buildings"),
        ("dynamic-1h", True, "nwis_iv"),
        ("live-no-cache", False, None),
    ],
)
def test_atomic_tool_metadata_accepts_all_four_ttl_classes(
    ttl_class: str, cacheable: bool, source_class: str | None
) -> None:
    """FR-DC-2: each of the four TTL classes is a legal registration."""
    meta = AtomicToolMetadata(
        name=f"fetch_{ttl_class.replace('-', '_')}",
        ttl_class=ttl_class,  # type: ignore[arg-type]
        cacheable=cacheable,
        source_class=source_class,
    )
    assert meta.ttl_class == ttl_class
    assert meta.cacheable is cacheable
    assert meta.source_class == source_class


def test_ttl_classes_tuple_matches_literal_members() -> None:
    """The tuple form (used by the agent registry's known-class assertions)
    matches the four-member ``Literal``."""
    assert TTL_CLASSES == (
        "static-30d",
        "semi-static-7d",
        "dynamic-1h",
        "live-no-cache",
    )


# --- Cross-field validator (FR-DC-6 consistency rule) --- #


def test_atomic_tool_metadata_rejects_cacheable_with_live_no_cache() -> None:
    """cacheable=True + ttl_class='live-no-cache' is inconsistent (FR-DC-6)."""
    with pytest.raises(ValidationError) as exc_info:
        AtomicToolMetadata(
            name="fetch_x",
            ttl_class="live-no-cache",
            cacheable=True,
            source_class="x",
        )
    assert "live-no-cache" in str(exc_info.value)


def test_atomic_tool_metadata_rejects_uncacheable_with_static_class() -> None:
    """cacheable=False + ttl_class='static-30d' is inconsistent (FR-DC-6)."""
    with pytest.raises(ValidationError) as exc_info:
        AtomicToolMetadata(
            name="request_spatial_input",
            ttl_class="static-30d",
            cacheable=False,
        )
    assert "live-no-cache" in str(exc_info.value)


def test_atomic_tool_metadata_rejects_cacheable_without_source_class() -> None:
    """cacheable=True requires a non-empty ``source_class`` (FR-DC-1 bucket path)."""
    with pytest.raises(ValidationError) as exc_info:
        AtomicToolMetadata(
            name="fetch_dem",
            ttl_class="static-30d",
            cacheable=True,
            # source_class omitted
        )
    assert "source_class" in str(exc_info.value)

    # Empty string also rejected
    with pytest.raises(ValidationError):
        AtomicToolMetadata(
            name="fetch_dem",
            ttl_class="static-30d",
            cacheable=True,
            source_class="",
        )


def test_atomic_tool_metadata_uncacheable_omits_source_class() -> None:
    """FR-DC-6 uncacheable tool: source_class MAY be None / omitted."""
    meta = AtomicToolMetadata(
        name="request_spatial_input",
        ttl_class="live-no-cache",
        cacheable=False,
    )
    assert meta.source_class is None


# --- Defaults --- #


def test_atomic_tool_metadata_defaults_cacheable_true() -> None:
    """cacheable defaults to True because the cacheable case is the common case."""
    meta = AtomicToolMetadata(
        name="fetch_dem",
        ttl_class="static-30d",
        source_class="dem",
    )
    assert meta.cacheable is True


# --- Round-trip --- #


def test_atomic_tool_metadata_json_roundtrip_idempotent() -> None:
    """Round-trip through real JSON serialize/deserialize is idempotent."""
    meta = AtomicToolMetadata(
        name="fetch_buildings",
        ttl_class="static-30d",
        source_class="buildings",
        cacheable=True,
    )
    dumped_a = meta.model_dump(mode="json")
    text_a = json.dumps(dumped_a, sort_keys=True)
    meta_b = AtomicToolMetadata.model_validate(json.loads(text_a))
    dumped_b = meta_b.model_dump(mode="json")
    text_b = json.dumps(dumped_b, sort_keys=True)
    assert text_a == text_b


# --- extra=forbid inheritance --- #


def test_atomic_tool_metadata_forbids_extra_fields() -> None:
    """GraceModel sets ``extra='forbid'``; unknown fields are rejected."""
    with pytest.raises(ValidationError):
        AtomicToolMetadata.model_validate(
            {
                "name": "fetch_dem",
                "ttl_class": "static-30d",
                "source_class": "dem",
                "cacheable": True,
                "cost_usd": 0.01,  # invariant 9: no cost theater
            }
        )


def test_atomic_tool_metadata_rejects_unknown_ttl_class() -> None:
    """ttl_class is a closed 4-member Literal (FR-DC-2 binding registry)."""
    with pytest.raises(ValidationError):
        AtomicToolMetadata.model_validate(
            {
                "name": "fetch_dem",
                "ttl_class": "static-90d",  # not one of the four
                "source_class": "dem",
                "cacheable": True,
            }
        )


# ============================================================================ #
# Wave 1.5 additions (job-0114-schema-20260608):
#   supports_global_query + payload_mb_estimator_name
# ============================================================================ #


def test_atomic_tool_metadata_supports_global_query_defaults_false() -> None:
    """Default supports_global_query is False (safer — tools opt in).

    Backward-compatibility check: existing call sites that never mention
    the flag must keep their pre-Wave-1.5 behaviour, which is "bbox is
    required by default."
    """
    meta = AtomicToolMetadata(
        name="fetch_dem",
        ttl_class="static-30d",
        source_class="dem",
    )
    assert meta.supports_global_query is False


def test_atomic_tool_metadata_payload_mb_estimator_name_defaults_none() -> None:
    """Default payload_mb_estimator_name is None (no Wave-2 chat-warning gate).

    Tools that don't yet declare an estimator pass through the Wave-2
    chat-warning system unaffected.
    """
    meta = AtomicToolMetadata(
        name="fetch_dem",
        ttl_class="static-30d",
        source_class="dem",
    )
    assert meta.payload_mb_estimator_name is None


def test_atomic_tool_metadata_accepts_supports_global_query_true() -> None:
    """A small-CONUS-payload tool opts in by passing supports_global_query=True."""
    meta = AtomicToolMetadata(
        name="fetch_nws_alerts_conus",
        ttl_class="dynamic-1h",
        source_class="nws_alerts",
        cacheable=True,
        supports_global_query=True,
    )
    assert meta.supports_global_query is True


def test_atomic_tool_metadata_accepts_payload_mb_estimator_name() -> None:
    """Tools wire a callable name reference for the Wave-2 chat-warning gate."""
    meta = AtomicToolMetadata(
        name="fetch_goes_satellite",
        ttl_class="dynamic-1h",
        source_class="goes",
        cacheable=True,
        payload_mb_estimator_name="estimate_payload_mb",
    )
    assert meta.payload_mb_estimator_name == "estimate_payload_mb"


def test_atomic_tool_metadata_both_wave15_fields_set() -> None:
    """The two Wave-1.5 fields compose independently (no cross-validator)."""
    meta = AtomicToolMetadata(
        name="fetch_mrms_qpe",
        ttl_class="dynamic-1h",
        source_class="mrms",
        cacheable=True,
        supports_global_query=True,
        payload_mb_estimator_name="estimate_payload_mb",
    )
    assert meta.supports_global_query is True
    assert meta.payload_mb_estimator_name == "estimate_payload_mb"


def test_atomic_tool_metadata_wave15_fields_roundtrip_through_json() -> None:
    """The Wave-1.5 fields round-trip through serialize/deserialize idempotently."""
    meta = AtomicToolMetadata(
        name="fetch_mrms_qpe",
        ttl_class="dynamic-1h",
        source_class="mrms",
        cacheable=True,
        supports_global_query=True,
        payload_mb_estimator_name="estimate_payload_mb",
    )
    dumped_a = meta.model_dump(mode="json")
    assert dumped_a["supports_global_query"] is True
    assert dumped_a["payload_mb_estimator_name"] == "estimate_payload_mb"

    text_a = json.dumps(dumped_a, sort_keys=True)
    meta_b = AtomicToolMetadata.model_validate(json.loads(text_a))
    dumped_b = meta_b.model_dump(mode="json")
    text_b = json.dumps(dumped_b, sort_keys=True)
    assert text_a == text_b
    assert meta_b.supports_global_query is True
    assert meta_b.payload_mb_estimator_name == "estimate_payload_mb"


def test_atomic_tool_metadata_supports_global_query_rejects_non_bool() -> None:
    """supports_global_query is typed bool — non-coercible values are rejected.

    Pydantic v2 will coerce common bool-like strings ("true"/"false"/"yes"/
    "no"/"1"/"0") and integers (0/1), which is fine for forward compat with
    JSON wire forms. But truly non-bool values (lists, dicts, arbitrary
    strings) must fail — we want misregistrations to fail fast.
    """
    with pytest.raises(ValidationError):
        AtomicToolMetadata.model_validate(
            {
                "name": "fetch_x",
                "ttl_class": "static-30d",
                "source_class": "x",
                "cacheable": True,
                "supports_global_query": ["true"],  # not a bool, not coercible
            }
        )

    with pytest.raises(ValidationError):
        AtomicToolMetadata.model_validate(
            {
                "name": "fetch_x",
                "ttl_class": "static-30d",
                "source_class": "x",
                "cacheable": True,
                "supports_global_query": "maybe",  # not a recognized bool string
            }
        )


def test_atomic_tool_metadata_payload_estimator_name_rejects_non_str() -> None:
    """payload_mb_estimator_name accepts only str | None — not a float or callable."""
    with pytest.raises(ValidationError):
        AtomicToolMetadata.model_validate(
            {
                "name": "fetch_x",
                "ttl_class": "static-30d",
                "source_class": "x",
                "cacheable": True,
                "payload_mb_estimator_name": 25,  # not a str
            }
        )


def test_atomic_tool_metadata_wave15_does_not_break_cacheable_validator() -> None:
    """Adding the new fields must not weaken the FR-DC-6 cross-field rule."""
    # cacheable=True + live-no-cache + supports_global_query=True is still
    # rejected — the cross-field rule still fires.
    with pytest.raises(ValidationError):
        AtomicToolMetadata(
            name="fetch_x",
            ttl_class="live-no-cache",
            cacheable=True,
            source_class="x",
            supports_global_query=True,
        )


# ============================================================================ #
# Geographic-correctness / semantic policy check
#
# This isn't a tool emitting geometry, but the kickoff codified lesson 1
# applies in spirit: when a tool advertises supports_global_query=False, it
# is the catalog's declaration that "the LLM must not call with bbox=None".
# The schema's job is to make that contract well-typed and discoverable.
# Verify the field is reachable on a snapshot of the catalog surface (the
# model_dump JSON that the agent serializes for the LLM).
# ============================================================================ #


def test_atomic_tool_metadata_advertises_global_policy_to_llm_catalog() -> None:
    """The catalog surface (model_dump JSON) must expose the global-policy hint
    so the LLM can compose workflows that respect each tool's bbox policy."""
    # Tool that requires bbox: supports_global_query=False (default).
    meta_required = AtomicToolMetadata(
        name="fetch_goes_satellite",
        ttl_class="dynamic-1h",
        source_class="goes",
    )
    dumped_required = meta_required.model_dump(mode="json")
    assert "supports_global_query" in dumped_required
    assert dumped_required["supports_global_query"] is False

    # Tool that allows bbox=None: supports_global_query=True.
    meta_global_ok = AtomicToolMetadata(
        name="fetch_nws_alerts_conus",
        ttl_class="dynamic-1h",
        source_class="nws_alerts",
        supports_global_query=True,
    )
    dumped_global_ok = meta_global_ok.model_dump(mode="json")
    assert dumped_global_ok["supports_global_query"] is True
