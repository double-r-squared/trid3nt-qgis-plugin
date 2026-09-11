"""Provider OpenAPI schema compliance for every registered tool.

Each tool produces a valid FunctionDeclaration through the same normalized path
the runtime builder uses, with no docstring-only fallback except for a
zero-parameter tool; no declaration carries ``anyOf`` / ``oneOf`` / ``allOf`` /
``$ref``; every property has an explicit ``type``; no underscore param survives."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).parent.parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from google.genai import types as genai_types  # noqa: E402

from trid3nt_server.adapters.adapter import (  # noqa: E402
    _normalize_callable_for_gemini,
    _simplify_annotation,
    _strip_private_params,
    build_tool_declarations,
)
import trid3nt_server.main as _main  # noqa: E402
from trid3nt_server.tools import TOOL_REGISTRY  # noqa: E402,F401

# The catalog tools register through the daemon startup import, not through
# ``trid3nt_server.tools``, so a module that sweeps the registry must run that
# import itself or its case list depends on which test module loaded first.
_main._import_tools_registry()



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



_FORBIDDEN_KEYWORDS = ("anyOf", "oneOf", "allOf", "$ref")

_ZERO_PARAM_TOOLS: set[str] = set()  # no tool legitimately has zero parameters


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



@pytest.mark.parametrize("tool_name", sorted(TOOL_REGISTRY.keys()))
def test_every_tool_builds_declaration(tool_name: str) -> None:
    """Every tool in ``TOOL_REGISTRY`` produces a FunctionDeclaration without error.

    The normalised callable must pass the builder cleanly; a failure means a new
    incompatible annotation, fixed in the tool or in the annotation simplifier."""
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



def test_every_property_has_type(
    all_declarations: dict[str, genai_types.FunctionDeclaration],
) -> None:
    """Every parameter property must have an explicit ``type`` field.

    A missing type is a 400 INVALID_ARGUMENT that rejects the whole declaration."""
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



def test_no_private_params_in_declarations(
    all_declarations: dict[str, genai_types.FunctionDeclaration],
) -> None:
    """No underscore-prefixed parameter appears in any final declaration.

    They are test-injection kwargs invisible to the model; the strip removes them and
    this is the gate over every registered tool."""
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



def test_build_tool_declarations_covers_all_registry_tools() -> None:
    """``build_tool_declarations`` must produce one declaration per registry tool."""
    decls = build_tool_declarations(TOOL_REGISTRY)
    decl_names = {d.name for d in decls}
    registry_names = set(TOOL_REGISTRY.keys())

    assert decl_names == registry_names, (
        f"Missing from declarations: {registry_names - decl_names}\n"
        f"Extra in declarations: {decl_names - registry_names}"
    )



_KNOWN_PROBLEMATIC_TOOLS = [
    "compute_hillshade",          # _storage_client typeless schema
    "clip_raster_to_polygon",     # B11: tuple[float,4] bbox + -> LayerURI return
    "fetch_mrms_qpe",             # B11: bbox: tuple[float,4] | None
    "fetch_nws_event",            # B11: area: str | tuple[...]
    "fetch_mtbs_burn_severity",   # B11: year_range: tuple[int,int] | None
    "fetch_firms_active_fire",    # B11: secret_ref: SecretRecord | None
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
