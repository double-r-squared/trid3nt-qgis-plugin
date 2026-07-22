"""Unit tests for the atomic-tool registry (job-0032, FR-AS-3, FR-CE-8).

Coverage:
- ``@register_tool`` happy path: populates ``TOOL_REGISTRY``, returns fn
  unchanged.
- Duplicate-name registration raises ``ToolRegistrationError``.
- ``get_registered_tools`` returns a sorted snapshot.
- The eager passthroughs import populates ``qgis_process``.
- ``register_tool`` rejects non-``AtomicToolMetadata`` arguments.
"""

from __future__ import annotations

import pytest
from grace2_contracts.tool_registry import AtomicToolMetadata

from grace2_agent import tools as agent_tools
from grace2_agent.tools import (
    RegisteredTool,
    ToolRegistrationError,
    get_registered_tools,
    register_tool,
)


def test_register_tool_decorator_populates_registry(empty_registry):
    """Decorating a function registers it and returns the original fn."""

    md = AtomicToolMetadata(
        name="fetch_demo",
        ttl_class="static-30d",
        source_class="demo",
        cacheable=True,
    )

    @register_tool(md)
    def fetch_demo(x: int) -> int:
        return x * 2

    assert "fetch_demo" in empty_registry
    entry = empty_registry["fetch_demo"]
    assert isinstance(entry, RegisteredTool)
    assert entry.metadata is md
    assert entry.fn is fetch_demo
    # Returned fn is callable directly (decorator does not wrap).
    assert fetch_demo(21) == 42
    assert entry.module == fetch_demo.__module__


def test_register_tool_duplicate_name_fails_fast(empty_registry):
    """A second registration under the same name raises at import time."""
    md = AtomicToolMetadata(
        name="dupe",
        ttl_class="dynamic-1h",
        source_class="dupe",
        cacheable=True,
    )

    @register_tool(md)
    def first() -> None:
        return None

    md2 = AtomicToolMetadata(
        name="dupe",
        ttl_class="dynamic-1h",
        source_class="dupe",
        cacheable=True,
    )
    with pytest.raises(ToolRegistrationError) as exc:

        @register_tool(md2)
        def second() -> None:  # pragma: no cover — must not register
            return None

    assert "dupe" in str(exc.value)
    assert "FR-CE-8" in str(exc.value)


def test_register_tool_rejects_non_metadata_argument():
    """Passing a dict / string / random object raises TypeError."""
    with pytest.raises(TypeError, match="AtomicToolMetadata"):
        register_tool({"name": "bad"})  # type: ignore[arg-type]


def test_get_registered_tools_returns_sorted_snapshot(empty_registry):
    """Snapshot is sorted by name for deterministic startup logs / diffs."""

    @register_tool(
        AtomicToolMetadata(
            name="b_tool", ttl_class="static-30d", source_class="b", cacheable=True
        )
    )
    def b_tool() -> None:
        return None

    @register_tool(
        AtomicToolMetadata(
            name="a_tool", ttl_class="static-30d", source_class="a", cacheable=True
        )
    )
    def a_tool() -> None:
        return None

    snapshot = get_registered_tools()
    assert [t.metadata.name for t in snapshot] == ["a_tool", "b_tool"]


def test_passthroughs_eager_import_registers_qgis():
    """Importing ``grace2_agent.tools`` populates the ``qgis_process`` pass-through.

    This is the acceptance-criterion test: the running agent registers it
    with ADK on startup because its module-level ``@register_tool`` call
    fires when ``grace2_agent.tools`` is imported. We exercise that by
    reading the live ``TOOL_REGISTRY`` after the package import.

    The dead ``mongo_query`` stub (MongoDB Atlas torn down) was removed; it
    must NOT be in the registry.
    """
    # No fixture: we deliberately use the live registry populated by import.
    assert "qgis_process" in agent_tools.TOOL_REGISTRY
    assert "mongo_query" not in agent_tools.TOOL_REGISTRY

    qp = agent_tools.TOOL_REGISTRY["qgis_process"]
    assert qp.metadata.ttl_class == "live-no-cache"
    assert qp.metadata.cacheable is False
    assert qp.metadata.source_class is None


def test_misconfigured_metadata_fails_at_construction():
    """FR-CE-8 fail-fast: cacheable=True + ttl_class='live-no-cache' rejects.

    The cross-field validator on ``AtomicToolMetadata`` runs at pydantic
    construction time, so a misconfigured ``@register_tool`` call dies
    before the decorator factory even sees it.
    """
    with pytest.raises(Exception):
        AtomicToolMetadata(
            name="bad",
            ttl_class="live-no-cache",
            source_class="bad",
            cacheable=True,
        )


def test_global_query_scope_audit():
    """NATE-requested ``supports_global_query`` scope audit (this job).

    Asserts the authoritative live ``TOOL_REGISTRY`` (populated by the eager
    package import) carries exactly the intended set of global-capable tools.
    Each tool in ``EXPECTED_GLOBAL_CAPABLE`` is one whose natural use is a
    no-bbox / nationwide query with a bounded upstream payload; everything
    else is bbox-required (a wrong ``True`` risks an absurd global download,
    so the default is the conservative ``False``).

    Two tools flipped to True in this audit:
    - ``fetch_nws_alerts_conus`` — the unscoped ``/alerts/active`` CONUS sweep
      (~200KB) is its primary use; resolves OQ-0105-GLOBAL-QUERY-FIELD.
    - ``fetch_nexrad_reflectivity`` — returns only a CONUS-wide WMS service URL
      (~0.1MB, no pixel transfer); the intent had been parked in dead code
      (``_INTENDED_METADATA_EXTENSIONS``) and never reached the live metadata.
      Resolves OQ-0102-METADATA-FIELDS for the global-query flag.
    """
    registry = agent_tools.TOOL_REGISTRY

    # Tools that legitimately run global/CONUS-wide with no bbox.
    expected_global_capable = {
        "fetch_nws_alerts_conus",       # /alerts/active CONUS sweep (this job)
        "fetch_era5_reanalysis",        # ERA5 is a global reanalysis grid
        "fetch_mrms_qpe",               # CONUS radar QPE mosaic
        "fetch_nexrad_reflectivity",    # WMS service URL; bbox=None => CONUS
        "fetch_nifc_fire_perimeters",   # active national fire perimeters
        "fetch_usace_dams",             # NID CONUS sweep (ArcGIS query)
        "fetch_usace_levees",           # NLD CONUS sweep (ArcGIS query)
        "fetch_iucn_red_list_range",    # queried by species name, not bbox
        "fetch_usgs_earthquakes",       # FDSN is global; "recent major quakes worldwide" is bounded (limit=20000, <=366d window)
        "fetch_usgs_volcano_alerts",    # HANS alert list is ~70 volcanoes, tiny/bounded
        "fetch_chirps_precipitation",   # quasi-global 0.05deg rainfall grid (~14MB), bounded like ERA5
        "fetch_fault_sources",          # GEM Global Active Faults is one bounded worldwide GeoJSON, bbox-filtered to AOI; no-bbox returns the global fault set (task #199)
        "fetch_storm_events_db",        # national NCEI severe-weather DB; bbox/state-less = legit CONUS-year sweep
        "fetch_tsunami_events",         # NCEI global historical tsunami DB is a bounded event list
        "list_categories",              # meta-tool, no spatial input
        "list_tools_in_category",       # meta-tool, no spatial input
    }

    # The set of tools actually flagged True must match exactly — this both
    # confirms every intended flip landed and guards against an accidental
    # future flip of a bbox-required tool.
    actual_global_capable = {
        name
        for name, entry in registry.items()
        if entry.metadata.supports_global_query
    }
    assert actual_global_capable == expected_global_capable, (
        "supports_global_query=True set drifted from the audited set; "
        f"unexpected={actual_global_capable - expected_global_capable}, "
        f"missing={expected_global_capable - actual_global_capable}"
    )

    # Explicitly assert the headline flip.
    assert registry["fetch_nws_alerts_conus"].metadata.supports_global_query is True

    # Representative bbox-required tools must remain False (a global query on
    # any of these would be an absurd / unbounded download).
    for bbox_required in (
        "fetch_dem",
        "fetch_buildings",
        "fetch_firms_active_fire",   # FIRMS AREA endpoint rejects a global bbox
        "fetch_hrsl_population",
        "fetch_gbif_occurrences",
        "clip_raster_to_bbox",
        "publish_layer",
    ):
        assert bbox_required in registry, f"{bbox_required} not registered"
        assert (
            registry[bbox_required].metadata.supports_global_query is False
        ), f"{bbox_required} must require a bbox (supports_global_query=False)"
