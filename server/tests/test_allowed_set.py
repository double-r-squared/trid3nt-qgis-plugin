"""Tests for ``AllowedToolSet`` composition (Wave 4.10 job-B5).

The allowed set drives the post-hoc validator (``validate_function_call``).
It is monotonically growing within a session:

- Hot set is always present (8 tools).
- Opening a category via ``list_tools_in_category`` adds every member tool
  of that category for the rest of the session.
- Dispatching a tool adds that tool (sticky-after-dispatch).
- Explicit ``add_tools`` pre-warms additional tools.
"""

from __future__ import annotations

import pytest

from trid3nt_server.categories import (
    HOT_SET_TOOLS,
    AllowedToolSet,
    tools_for_category,
)


@pytest.fixture(scope="module", autouse=True)
def _populate_registry() -> None:
    """Ensure the full tool registry is loaded so category-member queries work."""
    from trid3nt_server.main import _import_tools_registry

    _import_tools_registry()


# ---------------------------------------------------------------------------
# Hot-set baseline
# ---------------------------------------------------------------------------


def test_fresh_allowed_set_equals_hot_set() -> None:
    """A new session starts at the hot set baseline."""
    allowed = AllowedToolSet()
    assert allowed.as_frozenset() == HOT_SET_TOOLS


def test_hot_set_tools_pass_validation() -> None:
    """Every hot-set tool must validate against a fresh AllowedToolSet."""
    from trid3nt_server.categories import validate_function_call

    allowed = AllowedToolSet()
    for name in HOT_SET_TOOLS:
        # Should not raise.
        validate_function_call(name, allowed)


# ---------------------------------------------------------------------------
# Sticky-after-dispatch
# ---------------------------------------------------------------------------


def test_record_dispatch_widens_allowed_set() -> None:
    """A dispatched tool stays available even though it isn't in the hot set."""
    allowed = AllowedToolSet()
    assert "compute_hillshade" not in allowed.as_frozenset()

    allowed.record_dispatch("compute_hillshade")
    assert "compute_hillshade" in allowed.as_frozenset()


def test_dispatch_is_cumulative() -> None:
    """Successive dispatches accumulate; nothing leaves."""
    allowed = AllowedToolSet()
    allowed.record_dispatch("compute_hillshade")
    allowed.record_dispatch("fetch_landcover")
    allowed.record_dispatch("compute_zonal_statistics")
    snapshot = allowed.as_frozenset()
    assert {"compute_hillshade", "fetch_landcover", "compute_zonal_statistics"}.issubset(
        snapshot
    )
    # Hot set still present.
    assert HOT_SET_TOOLS.issubset(snapshot)


# ---------------------------------------------------------------------------
# Sticky-after-list (sticky-after-category-open)
# ---------------------------------------------------------------------------


def test_open_category_widens_allowed_set_to_full_members() -> None:
    """Opening a category opens up every member tool for the rest of the session."""
    allowed = AllowedToolSet()
    # Conservation category includes GBIF, iNat, WDPA, eBird, IUCN, Movebank.
    assert "fetch_gbif_occurrences" not in allowed.as_frozenset()
    assert "fetch_wdpa_protected_areas" not in allowed.as_frozenset()

    allowed.open_category("conservation_ecology")
    snapshot = allowed.as_frozenset()

    expected_members = set(tools_for_category("conservation_ecology"))
    assert expected_members.issubset(snapshot)


def test_open_category_does_not_pollute_other_categories() -> None:
    """Opening one category does not open another."""
    allowed = AllowedToolSet()
    allowed.open_category("conservation_ecology")
    snapshot = allowed.as_frozenset()
    # A tool from a different category that isn't in the hot set should stay out.
    assert "fetch_hrrr_forecast" not in snapshot
    assert "fetch_landfire_fuels" not in snapshot


def test_multiple_categories_open_simultaneously() -> None:
    """Two opens result in both categories' members being available."""
    allowed = AllowedToolSet()
    allowed.open_category("conservation_ecology")
    allowed.open_category("fire")
    snapshot = allowed.as_frozenset()
    assert "fetch_wdpa_protected_areas" in snapshot  # conservation
    assert "fetch_nifc_fire_perimeters" in snapshot  # fire


# ---------------------------------------------------------------------------
# Explicit add_tools
# ---------------------------------------------------------------------------


def test_explicit_add_tools() -> None:
    """``add_tools`` pre-warms specific names without opening a whole category."""
    allowed = AllowedToolSet()
    allowed.add_tools(["compute_slope"])
    assert "compute_slope" in allowed.as_frozenset()


# ---------------------------------------------------------------------------
# Cross-listed tools surface in BOTH categories when either is opened
# ---------------------------------------------------------------------------


def test_secondary_category_open_surfaces_cross_listed_tool() -> None:
    """A tool listed primary in hazard_modeling but cross-listed under
    damage_assessment becomes available when EITHER category is opened."""
    a1 = AllowedToolSet()
    a1.open_category("damage_assessment")
    assert "run_pelicun_damage_assessment" in a1.as_frozenset()

    a2 = AllowedToolSet()
    a2.open_category("hazard_modeling")
    assert "run_pelicun_damage_assessment" in a2.as_frozenset()
