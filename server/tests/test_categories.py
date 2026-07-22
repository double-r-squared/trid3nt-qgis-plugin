"""Tests for the 12-category registry + meta-tools (Wave 4.10 job-B5).

Coverage:
- ``CATEGORIES`` enumerates exactly the 12 specified ids.
- Every primary-mapped tool name appears in ``TOOL_REGISTRY`` once the full
  startup import path has run.
- Every registered tool has exactly one primary category (after eager init).
- ``list_categories`` returns 12 entries with the spec shape.
- ``list_tools_in_category`` returns members of one category with
  description snippets; unknown category raises ``UnknownCategoryError``.
- The two meta-tools register at import time so they appear in
  ``TOOL_REGISTRY``.
- Cross-listed tools (Pelicun, USACE NSI, NWS event ingest) appear in BOTH
  their primary and their secondary categories.
"""

from __future__ import annotations

import pytest

from trid3nt_server import categories
from trid3nt_server.categories import (
    CATEGORIES,
    HOT_SET_TOOLS,
    PRIMARY_CATEGORY,
    SECONDARY_CATEGORIES,
    UnknownCategoryError,
    list_categories,
    list_tools_in_category,
    tools_for_category,
)


def _ensure_full_registry() -> set[str]:
    """Run the full startup import path so workflow/solver/catalog tools
    appear in ``TOOL_REGISTRY``. Returns the set of registered names."""
    from trid3nt_server.main import _import_tools_registry
    from trid3nt_server import tools

    _import_tools_registry()
    return set(tools.TOOL_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Category-registry shape
# ---------------------------------------------------------------------------


def test_twelve_categories_registered() -> None:
    """The spec calls for exactly 12 categories."""
    assert len(CATEGORIES) == 12
    ids = [c.id for c in CATEGORIES]
    assert len(set(ids)) == 12, "category ids must be unique"


def test_categories_have_required_fields() -> None:
    """Each category exposes id, name, description."""
    for cat in CATEGORIES:
        assert cat.id and isinstance(cat.id, str)
        assert cat.name and isinstance(cat.name, str)
        assert cat.description and isinstance(cat.description, str)
        assert len(cat.description) <= 400, (
            f"category {cat.id} description too long ({len(cat.description)} chars); "
            "keep it crisp so the LLM-facing manifest stays cheap"
        )


def test_expected_category_ids_present() -> None:
    """The 12 ids must match the kickoff list verbatim."""
    expected = {
        "hazard_modeling",
        "weather_atmosphere",
        "hydrology",
        "terrain_elevation",
        "land_cover_development",
        "conservation_ecology",
        "fire",
        "coastal",
        "damage_assessment",
        "flood_infrastructure",
        "geographic_primitives",
        "news_events",
    }
    actual = {c.id for c in CATEGORIES}
    assert actual == expected


# ---------------------------------------------------------------------------
# Primary-category coverage (every registered tool has exactly one primary)
# ---------------------------------------------------------------------------


def test_every_registered_tool_has_a_primary_category() -> None:
    """All 76 startup-registered tools land in PRIMARY_CATEGORY.

    The two meta-tools (``list_categories`` + ``list_tools_in_category``) are
    implicit-hot-set and intentionally not in PRIMARY_CATEGORY; the test
    excludes them.
    """
    registered = _ensure_full_registry()
    mapped = set(PRIMARY_CATEGORY.keys()) | {"list_categories", "list_tools_in_category"}
    missing = registered - mapped
    assert missing == set(), (
        f"the following registered tools have no primary category: {sorted(missing)}"
    )


def test_no_primary_category_entry_points_to_missing_tool() -> None:
    """Every name in PRIMARY_CATEGORY must be a real registered tool."""
    registered = _ensure_full_registry()
    extras = set(PRIMARY_CATEGORY.keys()) - registered
    assert extras == set(), (
        f"PRIMARY_CATEGORY references non-registered tools: {sorted(extras)}"
    )


def test_every_primary_category_id_is_a_real_category() -> None:
    """Every value in PRIMARY_CATEGORY must be one of the 12 ids."""
    valid = {c.id for c in CATEGORIES}
    for tool_name, cat_id in PRIMARY_CATEGORY.items():
        assert cat_id in valid, (
            f"tool {tool_name} maps to category {cat_id} which is not registered"
        )


def test_every_secondary_category_id_is_a_real_category() -> None:
    """Every secondary category id must be a real category id too."""
    valid = {c.id for c in CATEGORIES}
    for tool_name, secondaries in SECONDARY_CATEGORIES.items():
        for s in secondaries:
            assert s in valid, (
                f"tool {tool_name} declares secondary category {s} which is not registered"
            )


def test_cross_listing_known_tools_appear_in_both_categories() -> None:
    """Pelicun and USACE NSI cross-list into damage_assessment."""
    _ensure_full_registry()
    damage_members = set(tools_for_category("damage_assessment"))
    assert {
        "run_pelicun_damage_assessment",
        "run_pelicun_with_buildings",
        "fetch_usace_nsi",
    }.issubset(damage_members)
    # Their primary categories still claim them too.
    hazard_members = set(tools_for_category("hazard_modeling"))
    assert "run_pelicun_damage_assessment" in hazard_members
    landuse_members = set(tools_for_category("land_cover_development"))
    assert "fetch_usace_nsi" in landuse_members


# ---------------------------------------------------------------------------
# list_categories meta-tool
# ---------------------------------------------------------------------------


def test_list_categories_returns_twelve_shaped_entries() -> None:
    """``list_categories()`` returns the spec-shape ``{categories: [...]}``."""
    result = list_categories()
    assert "categories" in result
    cats = result["categories"]
    assert len(cats) == 12
    for entry in cats:
        assert set(entry.keys()) == {"id", "name", "description"}
        assert isinstance(entry["id"], str)
        assert isinstance(entry["name"], str)
        assert isinstance(entry["description"], str)


def test_list_categories_is_registered_in_tool_registry() -> None:
    """Meta-tool registration fires at import time."""
    from trid3nt_server import tools

    assert "list_categories" in tools.TOOL_REGISTRY
    entry = tools.TOOL_REGISTRY["list_categories"]
    assert entry.metadata.supports_global_query is True
    assert entry.metadata.read_only_hint is True


# ---------------------------------------------------------------------------
# list_tools_in_category meta-tool
# ---------------------------------------------------------------------------


def test_list_tools_in_category_known_category_returns_members() -> None:
    """A valid category returns its sorted members with description snippets."""
    _ensure_full_registry()
    result = list_tools_in_category("conservation_ecology")
    assert result["category_id"] == "conservation_ecology"
    names = [t["name"] for t in result["tools"]]
    # spot-check a few expected members
    for expected in (
        "fetch_gbif_occurrences",
        "fetch_wdpa_protected_areas",
        "fetch_iucn_red_list_range",
    ):
        assert expected in names, (
            f"{expected} missing from conservation_ecology members: {names}"
        )
    # The shape carries description snippets.
    for entry in result["tools"]:
        assert "name" in entry
        assert "description_snippet" in entry
        assert isinstance(entry["description_snippet"], str)


def test_list_tools_in_category_unknown_category_raises() -> None:
    """An unknown id raises ``UnknownCategoryError`` (typed)."""
    with pytest.raises(UnknownCategoryError) as exc:
        list_tools_in_category("not_a_real_category_id")
    assert exc.value.error_code == "UNKNOWN_CATEGORY"
    assert exc.value.retryable is False
    # The hint includes the valid ids.
    assert "hazard_modeling" in exc.value.valid_ids


def test_list_tools_in_category_is_registered() -> None:
    """The second meta-tool registers too."""
    from trid3nt_server import tools

    assert "list_tools_in_category" in tools.TOOL_REGISTRY


# ---------------------------------------------------------------------------
# Hot-set composition
# ---------------------------------------------------------------------------


def test_hot_set_has_seventeen_tools() -> None:
    """Hot set: the original 8 (Wave 4.10 kickoff) + code_exec_request
    (job-0247 — cross-cutting capability, see OQ-0247-CODE-EXEC-NOT-IN-HOT-SET)
    + fetch_nws_event (job-0261 — validator rejected Gemini's correct
    state-scoped NWS call; the CONUS fallback spilled alerts nationwide)
    + compute_layer_bounds (NATE 2026-06-17 — fit/zoom/resize-to-encompass-all
    must be reachable without a category-open round-trip, else the agent falls
    back to the Python sandbox for bbox math, same failure mode as job-0247)
    + request_spatial_input (FR-AS-10 / FR-WC-16 — pause-the-turn user-draw
    action invoked at any point; same hot-set rationale as code_exec_request /
    compute_layer_bounds, else the urban-flood draw flow stalls on the post-hoc
    allowed-set validator)
    + the tool-retrieval STEP 0 floor (NATE 2026-06-23): publish_layer (survives
    today ONLY via validate_function_call's auto-widen — a latent gap once the
    catalog is trimmed) + the core analysis surface compute_zonal_statistics /
    generate_histogram / generate_time_series / spatial_query (the DuckDB
    spatial-query fold's SQL surface, holding the floor slot the folded
    summarize_layer_statistics held)."""
    assert len(HOT_SET_TOOLS) == 17


def test_hot_set_contains_required_anchors() -> None:
    """The hot set must include the meta-tools + discover_dataset so the LLM
    can always reach more tools when the initial set isn't enough, plus
    code_exec_request (job-0247: the validator rejected Gemini's CORRECT
    first-turn call and the agent narrated a false 'cannot run Python'),
    fetch_nws_event (job-0261: same first-turn-rejection failure mode —
    Gemini fell back to the unscoped CONUS sweep for 'weather alerts in
    texas' and rendered alerts in surrounding states), and
    compute_layer_bounds (NATE 2026-06-17: fit/zoom/resize-the-view is a
    cross-cutting action invoked at any point; keeping it always-reachable
    stops the agent from reaching for the Python sandbox for bbox math), and
    request_spatial_input (FR-AS-10 / FR-WC-16: the pause-the-turn user-draw
    action for AOIs + flood walls / flap gates — same always-reachable
    rationale, so the urban-flood draw flow does not stall on the validator)."""
    required = {
        "list_categories",
        "list_tools_in_category",
        "discover_dataset",
        "geocode_location",
        "fetch_dem",
        "fetch_nws_alerts_conus",
        "fetch_nws_event",
        "run_model_flood_scenario",
        "run_model_flood_habitat_scenario",
        "code_exec_request",
        "compute_layer_bounds",
        "request_spatial_input",
        # tool-retrieval STEP 0 floor (NATE 2026-06-23; spatial_query holds
        # the slot summarize_layer_statistics held before the Phase-B fold).
        "publish_layer",
        "compute_zonal_statistics",
        "generate_histogram",
        "generate_time_series",
        "spatial_query",
    }
    assert required == HOT_SET_TOOLS


def test_hot_set_dispatch_of_state_scoped_nws_call_is_allowed() -> None:
    """job-0261 regression: validate_function_call('fetch_nws_event') must
    pass on a FRESH session (no categories opened, nothing dispatched) —
    exactly the live-demo first turn that previously raised
    OutOfAllowedSetError and pushed Gemini to the unscoped CONUS sweep."""
    from trid3nt_server.categories import AllowedToolSet, validate_function_call

    fresh = AllowedToolSet()
    # Must not raise.
    validate_function_call("fetch_nws_event", fresh)
    validate_function_call("fetch_nws_alerts_conus", fresh)


def test_all_hot_set_tools_are_registered() -> None:
    """The hot set must reference real registered tools."""
    registered = _ensure_full_registry()
    missing = HOT_SET_TOOLS - registered
    assert missing == set(), (
        f"hot-set tools are not registered: {sorted(missing)}"
    )
