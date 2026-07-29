"""Promotion parity tests (data-router fold, phase-2 wave 1).

Migrated from the deleted twin test files (test_fetch_gridmet /
_hifld_critical_infrastructure / _noaa_coops_tides / _esri_landcover_10m /
_census_acs). Those files unit-tested the twins' INTERNAL helpers
(``_plan_tile_grid``, ``_VARIABLES``, ``_fetch_coops_tides_bytes``,
``_resolve_variable`` ...) which no longer exist -- deleted per the migration
rule. The CONTRACT-level behavior that survives the fold (each twin name is a
registered tool with the twin's signature / docstring / typed errors) is
re-expressed HERE against the promoted router surface. Deeper behavior parity
(values, layer output, caveats, every error path) is covered twin-vs-router by
``experiments/fetcher_fold_replication`` (5/5 edge matrix) and the router unit
suites (``test_router_engine`` / ``test_router_executors`` / ``test_router_spec_loader``).
"""

from __future__ import annotations

import pytest

from trid3nt_server.agent.tools import TOOL_REGISTRY
from trid3nt_server.agent.adapters.adapter import build_tool_declarations
from trid3nt_server.agent.tools.fetchers._router.errors import (
    RouterInputError,
    bbox_error_suffix,
)
from trid3nt_server.agent.tools.fetchers._router.spec import compose_specs_from_tree

# The 5 promoted pilots: name -> (source_class, expected declaration inputSchema).
# The schema map is the ground truth captured from the hand-written twins BEFORE
# deletion (properties + required set) -- the fold must reproduce it byte-for-byte.
PILOTS = {
    "fetch_gridmet": {
        "source_class": "gridmet",
        "properties": ["bbox", "end_date", "start_date", "variable"],
        "required": ["bbox", "end_date", "start_date", "variable"],
    },
    "fetch_hifld_critical_infrastructure": {
        "source_class": "hifld_critical_infrastructure",
        "properties": ["bbox", "facility_type"],
        "required": ["bbox", "facility_type"],
    },
    "fetch_noaa_coops_tides": {
        "source_class": "noaa_coops_tides",
        "properties": ["bbox", "end_date", "product", "start_date"],
        "required": ["bbox", "end_date", "start_date"],
    },
    "fetch_esri_landcover_10m": {
        "source_class": "esri_landcover_10m",
        "properties": ["bbox", "year"],
        "required": ["bbox"],
    },
    "fetch_census_acs": {
        "source_class": "census_acs",
        "properties": ["bbox", "variable", "year"],
        "required": ["bbox"],
    },
}

_SPECS = compose_specs_from_tree()


@pytest.mark.parametrize("name", sorted(PILOTS))
def test_pilot_registered_as_general_tool(name: str) -> None:
    """The twin name resolves to a promoted spec-driven tool in the default pool."""
    entry = TOOL_REGISTRY.get(name)
    assert entry is not None, f"{name} not registered (promotion did not fire)"
    # tier=general -> in every default-pool producer (none filter it out).
    assert getattr(entry.metadata, "tier", "general") == "general"
    assert entry.metadata.source_class == PILOTS[name]["source_class"]
    assert entry.metadata.cacheable is True
    # The callable resolves through the router engine's synthetic module (so the
    # payload-warning seam finds the synthesized estimate_payload_mb).
    assert entry.module.endswith(f"_router._promoted.{name}")
    import importlib

    mod = importlib.import_module(entry.module)
    assert callable(getattr(mod, "estimate_payload_mb"))


@pytest.mark.parametrize("name", sorted(PILOTS))
def test_pilot_declaration_schema_matches_twin(name: str) -> None:
    """FunctionDeclaration inputSchema (properties + required) == the twin's."""
    entry = TOOL_REGISTRY[name]
    decls = build_tool_declarations({name: entry})
    assert len(decls) == 1
    d = decls[0]
    assert d.name == name
    assert d.parameters is not None
    props = sorted((d.parameters.properties or {}).keys())
    required = sorted(d.parameters.required or [])
    assert props == PILOTS[name]["properties"], f"{name} properties drifted"
    assert required == PILOTS[name]["required"], f"{name} required set drifted"
    # A non-trivial description is carried (the twin docstring, indistinguishable).
    assert d.description and len(d.description) > 200


@pytest.mark.parametrize("name", sorted(PILOTS))
def test_pilot_docstring_is_twin_verbatim(name: str) -> None:
    """The promoted tool carries the spec docstring (== twin, drives the index)."""
    entry = TOOL_REGISTRY[name]
    spec = _SPECS[name]
    assert spec.docstring, f"{name} source.yaml lost its docstring"
    # The promoted callable's __doc__ IS the spec docstring verbatim (the sole
    # source of the FunctionDeclaration description + the retrieval-index document).
    assert entry.fn.__doc__ == spec.docstring


@pytest.mark.parametrize("name", sorted(PILOTS))
def test_pilot_degenerate_bbox_raises_twin_typed_error(name: str) -> None:
    """A degenerate bbox raises the twin-identical typed input error, pre-network.

    Validation runs before any endpoint call (offline), and the router stamps the
    twin's exact A.6 error_code (prefix from spec.error_code_prefix, suffix from
    the bbox param / spec-level input suffix)."""
    entry = TOOL_REGISTRY[name]
    spec = _SPECS[name]
    expected_code = f"{spec.error_code_prefix}_{bbox_error_suffix(spec)}"
    # min_lon == max_lon -> degenerate (rejected by _validate_bbox before network).
    with pytest.raises(RouterInputError) as excinfo:
        entry.fn(bbox=(-100.0, 40.0, -100.0, 41.0))
    assert excinfo.value.error_code == expected_code
    assert excinfo.value.retryable is False
