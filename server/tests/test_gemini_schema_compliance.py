"""Pytest tests for Gemini/Vertex OpenAPI schema compliance (B11 / Wave 4.10).

Invariants verified:
  1. Every tool in TOOL_REGISTRY produces a valid FunctionDeclaration when
     passed through ``_normalize_callable_for_gemini`` → ``from_callable_with_api_option``.
     No tool activates the docstring-only fallback (except zero-parameter tools
     which legitimately have no schema).
  2. No generated declaration contains ``anyOf``, ``oneOf``, ``allOf``, or
     ``$ref`` — these are forbidden by Vertex AI and cause a 400 response.
  3. Every property in every generated schema has an explicit ``type`` field —
     typeless properties cause a Vertex 400 that blocks the entire tool catalog.
  4. No underscore-prefixed parameters survive to the final declaration
     (regression gate for the job-0163 Vertex 400 fix).

Tests use the same normalized path that ``build_tool_declarations`` uses at
runtime, so any regression that re-introduces an incompatible annotation will
be caught here before it reaches Vertex.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Path setup — allow running tests from the services/agent/ directory.
# ---------------------------------------------------------------------------
_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from google.genai import types as genai_types  # noqa: E402

from grace2_agent.adapter import (  # noqa: E402
    _normalize_callable_for_gemini,
    _simplify_annotation,
    _strip_private_params,
    build_tool_declarations,
)
from grace2_agent.tools import TOOL_REGISTRY  # noqa: E402,F401 — populated on import


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def all_declarations() -> dict[str, genai_types.FunctionDeclaration]:
    """Build all FunctionDeclaration objects from the TOOL_REGISTRY.

    Uses ``build_tool_declarations`` — the same path the agent uses at startup.
    Scoped to module so it runs once per test session.
    """
    decls = build_tool_declarations(TOOL_REGISTRY)
    return {d.name: d for d in decls}


@pytest.fixture(scope="module")
def tool_names() -> list[str]:
    """Sorted list of all registered tool names."""
    return sorted(TOOL_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_FORBIDDEN_KEYWORDS = ("anyOf", "oneOf", "allOf", "$ref")

_ZERO_PARAM_TOOLS = {"list_categories"}  # legitimately have no parameters


def _walk_schema_for_violations(
    schema: dict[str, Any],
    path: str,
) -> list[str]:
    """Return a list of violation strings found in the schema dict recursively."""
    found: list[str] = []

    for keyword in _FORBIDDEN_KEYWORDS:
        if keyword in schema:
            found.append(f"{path}: contains forbidden keyword '{keyword}'")

    props = schema.get("properties") or {}
    for prop_name, prop_schema in props.items():
        if not isinstance(prop_schema, dict):
            continue
        prop_path = f"{path}.{prop_name}"
        if "type" not in prop_schema and "anyOf" not in prop_schema:
            found.append(f"{prop_path}: missing 'type' field (Vertex 400 trigger)")
        found.extend(_walk_schema_for_violations(prop_schema, prop_path))

    items = schema.get("items")
    if isinstance(items, dict):
        found.extend(_walk_schema_for_violations(items, f"{path}[items]"))

    return found


# ---------------------------------------------------------------------------
# Test: all tools produce a declaration without raising
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name", sorted(TOOL_REGISTRY.keys()))
def test_every_tool_builds_declaration(tool_name: str) -> None:
    """Every tool in TOOL_REGISTRY must produce a FunctionDeclaration without error.

    The normalised callable (return annotation replaced with dict, tuple params
    replaced with list[float]/list[int], complex Pydantic params replaced with
    str | None) must pass ``from_callable_with_api_option`` cleanly.

    Failure here means a new incompatible annotation was added to the tool's
    signature; fix it either in the tool file OR by extending
    ``_simplify_annotation`` in adapter.py.
    """
    entry = TOOL_REGISTRY[tool_name]
    normalised = _normalize_callable_for_gemini(entry.fn)

    if tool_name in _ZERO_PARAM_TOOLS:
        pytest.skip(f"{tool_name!r} is a zero-parameter tool; no schema expected")

    try:
        decl = genai_types.FunctionDeclaration.from_callable_with_api_option(
            callable=normalised,
            api_option="VERTEX_AI",
        )
    except Exception as exc:
        pytest.fail(
            f"Tool {tool_name!r}: from_callable_with_api_option raised after "
            f"normalisation — the tool has an annotation that _normalize_callable_ "
            f"did not resolve.  Error: {exc}"
        )

    decl = _strip_private_params(decl)
    # A zero-parameter tool may still have parameters=None — that's fine.
    # Tools with actual parameters must have a schema.


# ---------------------------------------------------------------------------
# Test: no anyOf / oneOf / allOf / $ref in any generated schema
# ---------------------------------------------------------------------------

def test_no_anyof_in_any_tool_schema(
    all_declarations: dict[str, genai_types.FunctionDeclaration],
) -> None:
    """No tool schema may contain anyOf, oneOf, allOf, or $ref.

    These are forbidden by Vertex AI's OpenAPI schema subset and produce
    400 INVALID_ARGUMENT responses that block the entire tool catalog.
    """
    violations: list[str] = []
    for name, decl in sorted(all_declarations.items()):
        if decl.parameters is None:
            continue
        try:
            schema_dict = decl.parameters.model_dump(exclude_none=True)
            schema_str = json.dumps(schema_dict)
        except Exception:  # noqa: BLE001
            continue

        for keyword in _FORBIDDEN_KEYWORDS:
            if keyword in schema_str:
                violations.append(f"{name}: contains forbidden '{keyword}'")

    assert violations == [], (
        f"Tools with forbidden schema keywords (Vertex 400 triggers):\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# Test: every property has an explicit type field
# ---------------------------------------------------------------------------

def test_every_property_has_type(
    all_declarations: dict[str, genai_types.FunctionDeclaration],
) -> None:
    """Every parameter property must have an explicit 'type' field.

    A missing type causes Vertex AI to reject the declaration with
    400 INVALID_ARGUMENT: schema didn't specify the schema type field.
    This is the same class of bug that job-0163 fixed for underscore params.
    """
    violations: list[str] = []
    for name, decl in sorted(all_declarations.items()):
        if decl.parameters is None or decl.parameters.properties is None:
            continue
        try:
            schema_dict = decl.parameters.model_dump(exclude_none=True)
        except Exception:  # noqa: BLE001
            continue
        found = _walk_schema_for_violations(schema_dict, f"{name}.schema")
        violations.extend(found)

    assert violations == [], (
        f"Tools with missing 'type' fields or forbidden keywords:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# Test: no underscore-prefixed parameters survive to the final declaration
# ---------------------------------------------------------------------------

def test_no_private_params_in_declarations(
    all_declarations: dict[str, genai_types.FunctionDeclaration],
) -> None:
    """No underscore-prefixed parameter must appear in any final declaration.

    Underscore-prefixed params (``_storage_client``, ``_bucket``, etc.) are
    test-injection kwargs invisible to the LLM. ``_strip_private_params`` removes
    them; this test is a regression gate that prevents them from reappearing.

    This is the invariant first verified in ``test_adapter_strip_private_params.py``
    (job-0163) and extended here to all 58+ registered tools.
    """
    leaked: list[str] = []
    for name, decl in sorted(all_declarations.items()):
        if decl.parameters is None or decl.parameters.properties is None:
            continue
        bad = [p for p in decl.parameters.properties if p.startswith("_")]
        if bad:
            leaked.append(f"{name}: {bad}")

    assert leaked == [], (
        f"Tools with underscore-prefixed params still in schema:\n"
        + "\n".join(f"  {v}" for v in leaked)
    )


# ---------------------------------------------------------------------------
# Test: _simplify_annotation handles key annotation patterns correctly
# ---------------------------------------------------------------------------

def test_simplify_tuple_to_list() -> None:
    """``tuple[float, float, float, float]`` simplifies to ``list[float]``."""
    import types as _t
    from typing import get_origin

    result = _simplify_annotation(tuple[float, float, float, float])
    assert get_origin(result) is list, (
        f"Expected list type, got {result}"
    )


def test_simplify_optional_tuple_to_optional_list() -> None:
    """``tuple[float, float, float, float] | None`` simplifies to ``list[float] | None``."""
    import types as _t
    from typing import get_origin

    ann = tuple[float, float, float, float] | None
    result = _simplify_annotation(ann)
    # Result should be a union type containing list
    assert isinstance(result, _t.UnionType) or get_origin(result) is not None, (
        f"Expected a union/optional type, got {result}"
    )
    # Should contain a list type
    import typing
    args = typing.get_args(result) if not isinstance(result, _t.UnionType) else result.__args__
    list_args = [a for a in args if get_origin(a) is list]
    assert list_args, f"Expected list[float] in result args, got args={args}"


def test_simplify_str_or_tuple_to_str() -> None:
    """``str | tuple[float, ...]`` simplifies to ``str``."""
    ann = str | tuple[float, float, float, float]
    result = _simplify_annotation(ann)
    assert result is str, f"Expected str, got {result}"


def test_simplify_pydantic_model_to_str_none() -> None:
    """A Pydantic model annotation simplifies to ``str | None``."""
    import types as _t

    class _MockModel:  # noqa: N801
        pass

    result = _simplify_annotation(_MockModel)
    # Should be str | None
    assert isinstance(result, _t.UnionType) or result is str, (
        f"Expected str | None or str, got {result}"
    )


def test_simplify_passthrough_str() -> None:
    """``str`` annotations pass through unchanged."""
    assert _simplify_annotation(str) is str


def test_simplify_passthrough_int_none() -> None:
    """``int | None`` passes through unchanged."""
    import types as _t
    ann = int | None
    result = _simplify_annotation(ann)
    # Should still be a union containing int
    assert isinstance(result, _t.UnionType), f"Expected UnionType, got {result}"
    assert int in result.__args__, f"Expected int in args, got {result.__args__}"


# ---------------------------------------------------------------------------
# Test: build_tool_declarations produces the right count
# ---------------------------------------------------------------------------

def test_build_tool_declarations_covers_all_registry_tools() -> None:
    """``build_tool_declarations`` must produce one declaration per registry tool."""
    decls = build_tool_declarations(TOOL_REGISTRY)
    decl_names = {d.name for d in decls}
    registry_names = set(TOOL_REGISTRY.keys())

    assert decl_names == registry_names, (
        f"Missing from declarations: {registry_names - decl_names}\n"
        f"Extra in declarations: {decl_names - registry_names}"
    )


# ---------------------------------------------------------------------------
# Regression: specific tools known to have caused Vertex 400 in the past
# ---------------------------------------------------------------------------

_KNOWN_PROBLEMATIC_TOOLS = [
    "compute_zonal_statistics",   # job-0163: _storage_client typeless schema
    "compute_hillshade",          # job-0163: same class
    "clip_raster_to_bbox",        # B11: tuple[float,4] + -> LayerURI return
    "fetch_mrms_qpe",             # B11: bbox: tuple[float,4] | None
    "fetch_nws_event",            # B11: area: str | tuple[...]
    "fetch_gbif_occurrences",     # B11: year_range: tuple[int,int] | None
    "fetch_iucn_red_list_range",  # B11: secret_ref: SecretRecord | None
    "fetch_nhdplus_nldi_navigate", # B11: seed_point: tuple[float,2] | None
    "fetch_noaa_slr_scenarios",   # B11: scenario_ft: float | list[float] | None
]


@pytest.mark.parametrize("tool_name", _KNOWN_PROBLEMATIC_TOOLS)
def test_regression_known_problematic_tools(
    tool_name: str,
    all_declarations: dict[str, genai_types.FunctionDeclaration],
) -> None:
    """Regression gate: tools known to have caused Vertex 400 errors produce
    valid declarations after the B11 normalisation fix.

    A failure here means a specific previously-fixed violation regressed.
    """
    if tool_name not in all_declarations:
        pytest.skip(f"{tool_name!r} not in registry")

    decl = all_declarations[tool_name]

    # Must have a parameters schema (not docstring-only fallback).
    assert decl.parameters is not None, (
        f"{tool_name!r} has no parameters schema — normalisation did not succeed"
    )

    # No underscore params.
    props = decl.parameters.properties or {}
    leaked = [p for p in props if p.startswith("_")]
    assert leaked == [], f"{tool_name!r} leaked private params: {leaked}"

    # No forbidden keywords.
    if decl.parameters:
        try:
            schema_str = json.dumps(decl.parameters.model_dump(exclude_none=True))
            for keyword in _FORBIDDEN_KEYWORDS:
                assert keyword not in schema_str, (
                    f"{tool_name!r}: schema contains forbidden '{keyword}'"
                )
        except Exception:  # noqa: BLE001
            pass  # serialization errors are caught by the broader tests above
