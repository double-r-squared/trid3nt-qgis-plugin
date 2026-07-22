"""End-to-end test of the post-hoc routing recovery loop (Wave 4.10 job-B5,
revised by job-0270).

Simulates the Wave 4.10 CachedContent Option A path. Since job-0270 there
are two distinct flows:

A. REAL registered tool outside the allowed set (the common case — Gemini
   saw the full catalog via CachedContent and routed correctly):
   ``validate_function_call`` AUTO-WIDENS the allowed set and the dispatch
   proceeds on the FIRST call. No error envelope, no detour turns.

B. Name that exists nowhere in the registry (hallucination guard):
   1. ``validate_function_call`` raises ``OutOfAllowedSetError``.
   2. ``summarize_tool_result(error=...)`` (adapter.py) renders the typed
      exception as the canonical Wave 4.9 structured envelope:
      ``{"status": "error", "error_code": "OUT_OF_ALLOWED_SET",
         "retryable": False, "message": ..., ...}``.
   3. Gemini reads the envelope, discovers a REAL tool (e.g. via
      ``list_tools_in_category``), and re-emits a valid function_call.

Both flows are exercised end-to-end here without spinning up a real Gemini.
"""

from __future__ import annotations

import pytest

from grace2_agent.adapter import summarize_tool_result
from grace2_agent.categories import (
    AllowedToolSet,
    OutOfAllowedSetError,
    list_tools_in_category,
    validate_function_call,
)


@pytest.fixture(scope="module", autouse=True)
def _populate_registry() -> None:
    from grace2_agent.main import _import_tools_registry

    _import_tools_registry()


def _simulate_validate_and_summarize(
    tool_name: str, allowed: AllowedToolSet
) -> dict:
    """Mirror the server.py dispatch loop's validate → summarize path.

    Returns the function_response payload Gemini would see on its next turn.
    """
    try:
        validate_function_call(tool_name, allowed)
    except OutOfAllowedSetError as exc:
        return summarize_tool_result(tool_name, None, error=exc)
    # No error path in this scaffolding — would normally call the tool here.
    return summarize_tool_result(tool_name, {"status": "ok"}, error=None)


# ---------------------------------------------------------------------------
# Flow A (job-0270): real tools dispatch on the FIRST call — no detour
# ---------------------------------------------------------------------------


def test_real_tool_outside_hot_set_dispatches_first_call() -> None:
    """The live failure this guards: Gemini called compute_colored_relief /
    publish_layer correctly on the first turn and the validator bounced it,
    burning 2-4 detour iterations guessing category names. Now: auto-widen,
    dispatch proceeds, Gemini sees an ok envelope immediately."""
    allowed = AllowedToolSet()
    for name in ("compute_colored_relief", "publish_layer", "fetch_storm_events_db"):
        envelope = _simulate_validate_and_summarize(name, allowed)
        assert envelope["status"] == "ok", (
            f"{name}: expected first-call dispatch, got {envelope}"
        )
        assert envelope.get("error_code") != "OUT_OF_ALLOWED_SET"
        # The widened set is sticky for the session.
        assert name in allowed.as_frozenset()


# ---------------------------------------------------------------------------
# Flow B: hallucinated names still bounce with the structured envelope
# ---------------------------------------------------------------------------


def test_hallucinated_name_yields_structured_error_envelope() -> None:
    """A name that exists nowhere in the registry surfaces the Wave 4.9
    envelope shape so Gemini can distinguish 'tool failed' from 'tool was
    never in the catalog'."""
    allowed = AllowedToolSet()
    envelope = _simulate_validate_and_summarize(
        "fetch_storm_reports_database", allowed  # plausible but not a tool
    )
    assert envelope["tool"] == "fetch_storm_reports_database"
    assert envelope["status"] == "error"
    assert envelope["error_code"] == "OUT_OF_ALLOWED_SET"
    assert envelope["retryable"] is False
    assert "list_tools_in_category" in envelope["message"]


def test_agent_recovers_from_hallucination_via_list_tools_in_category() -> None:
    """Full recovery loop for the guard path: hallucinated name → error
    envelope → list_tools_in_category surfaces the REAL names → re-issue
    with a valid name → validates → would dispatch."""
    allowed = AllowedToolSet()

    # --- Turn 1: Gemini invents a name. Bounced by the guard. ---
    envelope_1 = _simulate_validate_and_summarize(
        "fetch_storm_reports_database", allowed
    )
    assert envelope_1["error_code"] == "OUT_OF_ALLOWED_SET"

    # --- Turn 2: Gemini reads the envelope and opens the right category to
    # see the real member names. The server.py loop calls open_category
    # after a successful list_tools_in_category dispatch; replicated here. ---
    categorisation = list_tools_in_category("news_events")
    allowed.open_category(categorisation["category_id"])
    member_names = {t["name"] for t in categorisation["tools"]}
    assert "fetch_storm_events_db" in member_names

    # --- Turn 3: Gemini re-issues with the REAL name. Now passes. ---
    envelope_3 = _simulate_validate_and_summarize(
        "fetch_storm_events_db", allowed
    )
    assert envelope_3["status"] == "ok"


def test_agent_recovery_widens_to_every_member_of_the_opened_category() -> None:
    """After opening one category, multiple tools from it are reachable on
    the same turn (the LLM may want to fan out parallel function_calls)."""
    allowed = AllowedToolSet()
    # Open the conservation_ecology category.
    res = list_tools_in_category("conservation_ecology")
    allowed.open_category(res["category_id"])

    for name in ("fetch_gbif_occurrences", "fetch_wdpa_protected_areas",
                 "fetch_iucn_red_list_range"):
        assert validate_function_call(name, allowed) is None


def test_unknown_category_routes_to_typed_envelope() -> None:
    """If Gemini guesses a wrong category_id for list_tools_in_category, the
    UnknownCategoryError propagates as a structured envelope so Gemini can
    retry with a valid id (after re-reading list_categories)."""
    # Simulate: Gemini called list_tools_in_category with a typo.
    from grace2_agent.categories import (
        UnknownCategoryError,
        list_tools_in_category as _impl,
    )

    try:
        _impl("conservation")  # typo: should be "conservation_ecology"
    except UnknownCategoryError as exc:
        envelope = summarize_tool_result("list_tools_in_category", None, error=exc)
        assert envelope["error_code"] == "UNKNOWN_CATEGORY"
        assert envelope["retryable"] is False
    else:  # pragma: no cover — test logic guard
        pytest.fail("expected UnknownCategoryError for a bad category id")


def test_dispatched_tool_stays_allowed_on_subsequent_turns() -> None:
    """Sticky-after-dispatch — once a tool has been called this session it
    stays in the allowed set for later turns (monotonic growth)."""
    allowed = AllowedToolSet()
    # job-0270: the first call auto-widens (real registry tool)...
    assert validate_function_call("compute_zonal_statistics", allowed) is None
    # ...and the server records the dispatch on success.
    allowed.record_dispatch("compute_zonal_statistics")
    # Subsequent turns keep passing; the set never shrinks.
    assert validate_function_call("compute_zonal_statistics", allowed) is None
    assert "compute_zonal_statistics" in allowed.as_frozenset()
