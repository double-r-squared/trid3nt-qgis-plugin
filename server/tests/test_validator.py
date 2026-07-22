"""Tests for ``validate_function_call`` (Wave 4.10 job-B5 + job-0270).

The post-hoc validator is the Wave 4.10 CachedContent Option A enforcement
point. Every Gemini ``function_call`` is validated against the current
turn's ``AllowedToolSet`` BEFORE dispatch:

- In-set call → returns ``None`` (pass).
- Out-of-set call for a REAL registered tool → auto-widens the allowed set
  with that name and returns ``None`` (job-0270 — Gemini saw the full
  catalog via CachedContent; a registry-valid call is correct routing, not
  a hallucination. Live evidence: rejecting first calls to
  ``compute_colored_relief`` / ``compute_hillshade`` / ``publish_layer``
  burned 2-4 detour iterations per turn).
- Out-of-set call for a name that exists NOWHERE in the registry → raises
  ``OutOfAllowedSetError`` with ``error_code='OUT_OF_ALLOWED_SET'`` and
  ``retryable=False`` — the hallucination guard, unweakened.

The error is then routed through ``summarize_tool_result(error=...)`` in
``adapter.py`` so Gemini sees a structured envelope and can retry on its
next turn (typically by calling ``list_tools_in_category`` /
``discover_dataset`` to find a real tool).
"""

from __future__ import annotations

import logging

import pytest

from grace2_agent.categories import (
    HOT_SET_TOOLS,
    AllowedToolSet,
    OutOfAllowedSetError,
    validate_function_call,
)


@pytest.fixture(scope="module", autouse=True)
def _populate_registry() -> None:
    """Ensure the full registry is loaded."""
    from grace2_agent.main import _import_tools_registry

    _import_tools_registry()


# ---------------------------------------------------------------------------
# In-set passes
# ---------------------------------------------------------------------------


def test_hot_set_call_passes() -> None:
    """A function_call for a hot-set tool returns None (no raise)."""
    allowed = AllowedToolSet()
    # geocode_location is in the hot set.
    assert validate_function_call("geocode_location", allowed) is None


def test_dispatched_tool_passes() -> None:
    """A tool that was dispatched earlier in the session passes on later turns."""
    allowed = AllowedToolSet()
    allowed.record_dispatch("compute_hillshade")
    assert validate_function_call("compute_hillshade", allowed) is None


def test_category_opened_tool_passes() -> None:
    """A tool whose category was opened earlier passes on later turns."""
    allowed = AllowedToolSet()
    allowed.open_category("fire")
    # FIRMS is in the fire category.
    assert validate_function_call("fetch_firms_active_fire", allowed) is None


# ---------------------------------------------------------------------------
# job-0270: real-registry-but-not-allowed tools auto-widen (no raise)
# ---------------------------------------------------------------------------


def test_real_registry_tool_outside_hot_set_auto_widens() -> None:
    """A registered tool outside the allowed set no longer raises — the
    validator auto-widens the set and the dispatch proceeds. This is the
    exact live failure: Gemini called compute_colored_relief FIRST (correct
    routing) and the validator bounced it, burning detour iterations."""
    # publish_layer moved INTO the hot set (tool-retrieval STEP 0, 2026-06-23), so
    # it can no longer demonstrate the auto-widen path; compute_contours is a
    # registry-valid tool still outside the hot set.
    for name in ("compute_colored_relief", "compute_hillshade", "compute_contours"):
        allowed = AllowedToolSet()
        assert name not in allowed.as_frozenset(), (
            f"{name} unexpectedly in the hot set — test premise broken"
        )
        # Must NOT raise.
        assert validate_function_call(name, allowed) is None
        # The widened set now contains the tool.
        assert name in allowed.as_frozenset()


def test_auto_widen_persists_for_the_session() -> None:
    """The widened set is sticky — later turns re-validate without consulting
    the registry path again (monotonic growth, same as category open)."""
    allowed = AllowedToolSet()
    validate_function_call("compute_colored_relief", allowed)
    # Subsequent validations of the same tool pass via the snapshot.
    assert validate_function_call("compute_colored_relief", allowed) is None
    assert "compute_colored_relief" in allowed.as_frozenset()
    # Monotonic growth preserved: hot set + meta-tools still present.
    assert HOT_SET_TOOLS.issubset(allowed.as_frozenset())


def test_auto_widen_is_cumulative_across_tools() -> None:
    """Multiple auto-widens accumulate; nothing leaves the set."""
    allowed = AllowedToolSet()
    validate_function_call("compute_colored_relief", allowed)
    validate_function_call("compute_hillshade", allowed)
    validate_function_call("compute_contours", allowed)  # publish_layer is now hot-set
    snapshot = allowed.as_frozenset()
    assert {
        "compute_colored_relief",
        "compute_hillshade",
        "compute_contours",
    }.issubset(snapshot)
    assert HOT_SET_TOOLS.issubset(snapshot)


def test_auto_widen_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """The auto-widen path logs at WARNING so telemetry / hot-set tuning can
    see which real tools keep landing outside the hot set."""
    allowed = AllowedToolSet()
    with caplog.at_level(logging.WARNING, logger="grace2_agent.categories"):
        validate_function_call("compute_colored_relief", allowed)
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "allowed-set auto-widen tool=compute_colored_relief" in m
        and "outside hot set" in m
        for m in messages
    ), f"expected auto-widen WARNING, got: {messages}"


def test_auto_widen_uses_explicit_tools_growth_path() -> None:
    """The widening reuses the existing explicit-tools mechanism (the same
    one ``add_tools`` pre-warm uses) — no new state shape."""
    allowed = AllowedToolSet()
    validate_function_call("compute_aspect", allowed)
    assert "compute_aspect" in allowed.explicit_tools


# ---------------------------------------------------------------------------
# Hallucination guard: non-registry names still raise
# ---------------------------------------------------------------------------


def test_nonexistent_tool_raises_typed_exception() -> None:
    """A function_call for a name that exists nowhere in the registry raises
    ``OutOfAllowedSetError`` with the documented error_code + retryable
    semantics. Wave 4.9 ``summarize_tool_result`` reads these attrs to build
    the structured error envelope Gemini retries against."""
    allowed = AllowedToolSet()
    with pytest.raises(OutOfAllowedSetError) as exc:
        validate_function_call("compute_terrain_relief_v2", allowed)

    assert exc.value.error_code == "OUT_OF_ALLOWED_SET"
    assert exc.value.retryable is False
    assert exc.value.tool_name == "compute_terrain_relief_v2"
    # Error message includes the corrective hint pointing to
    # list_tools_in_category, per the kickoff message text.
    assert "list_tools_in_category" in str(exc.value)


def test_typo_or_invented_tool_name_raises() -> None:
    """Names that don't exist anywhere raise the out-of-set error.

    The ToolNotFoundError path runs further downstream (inside
    ``_invoke_tool_via_emitter``); this validator is intentionally a
    pre-check, so an unknown name lands as OUT_OF_ALLOWED_SET first."""
    allowed = AllowedToolSet()
    with pytest.raises(OutOfAllowedSetError) as exc:
        validate_function_call("invented_tool_that_does_not_exist", allowed)
    assert exc.value.error_code == "OUT_OF_ALLOWED_SET"


def test_nonexistent_tool_does_not_widen_the_set() -> None:
    """The hallucination guard must not pollute the allowed set — a rejected
    name stays out (it could never dispatch anyway, but the set should stay
    clean for observability)."""
    allowed = AllowedToolSet()
    before = allowed.as_frozenset()
    with pytest.raises(OutOfAllowedSetError):
        validate_function_call("fetch_unicorn_density", allowed)
    assert allowed.as_frozenset() == before


def test_error_carries_hot_set_hint() -> None:
    """The exception's hint surfaces the hot set so Gemini sees a corrective
    set of available tools in the function_response."""
    allowed = AllowedToolSet()
    with pytest.raises(OutOfAllowedSetError) as exc:
        validate_function_call("fetch_imaginary_dataset", allowed)
    # Hot-set hint should be present and include the meta-tools.
    assert exc.value.hot_set_hint
    assert "list_categories" in exc.value.hot_set_hint
    assert "list_tools_in_category" in exc.value.hot_set_hint


# ---------------------------------------------------------------------------
# Validator integrates with sticky updates
# ---------------------------------------------------------------------------


def test_category_open_still_widens_independently_of_auto_widen() -> None:
    """The sticky-after-list path is unchanged: opening a category makes its
    members pass via the snapshot (no registry consult / WARNING needed)."""
    allowed = AllowedToolSet()
    allowed.open_category("news_events")
    assert "fetch_storm_events_db" in allowed.as_frozenset()
    assert validate_function_call("fetch_storm_events_db", allowed) is None


def test_record_dispatch_still_widens_independently_of_auto_widen() -> None:
    """The sticky-after-dispatch path is unchanged."""
    allowed = AllowedToolSet()
    allowed.record_dispatch("compute_slope")
    assert validate_function_call("compute_slope", allowed) is None
    assert "compute_slope" in allowed.as_frozenset()
