"""The tool catalog the model reasons over, built from the registry.

Every registered tool produces one declaration whose schema is a typed object:
no ``anyOf`` / ``oneOf`` / ``allOf`` / ``$ref``, an explicit ``type`` on every
property, and no underscore-prefixed injection kwarg. The annotation cases the
registry actually carries - tuples, optionals, unions, custom classes - each
resolve to a single JSON Schema node.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from trid3nt_server.adapters.adapter import (
    _parameter_schema,
    _tool_schema,
    build_tool_declarations,
)
import trid3nt_server.main as _main
from trid3nt_server.tools import TOOL_REGISTRY  # noqa: F401 -- populated on import

# The catalog tools register through the daemon startup import, not through
# ``trid3nt_server.tools``, so a module that sweeps the registry must run that
# import itself or its case list depends on which test module loaded first.
_main._import_tools_registry()

_FORBIDDEN_KEYWORDS = ("anyOf", "oneOf", "allOf", "$ref")


@pytest.fixture(scope="module")
def all_declarations() -> dict[str, Any]:
    """Every declaration, by name, built through the runtime path."""
    return {d.name: d for d in build_tool_declarations(TOOL_REGISTRY)}


def _walk(schema: dict[str, Any], path: str) -> list[str]:
    """Violations found anywhere under one schema node."""
    found = [f"{path}: forbidden {k!r}" for k in _FORBIDDEN_KEYWORDS if k in schema]
    for name, sub in (schema.get("properties") or {}).items():
        if not isinstance(sub, dict):
            continue
        if "type" not in sub:
            found.append(f"{path}.{name}: no 'type'")
        found.extend(_walk(sub, f"{path}.{name}"))
    items = schema.get("items")
    if isinstance(items, dict):
        found.extend(_walk(items, f"{path}[items]"))
    return found


def test_one_declaration_per_registered_tool(all_declarations) -> None:
    """The catalog covers the registry exactly - no tool missing, none invented."""
    assert set(all_declarations) == set(TOOL_REGISTRY)


def test_every_schema_is_a_typed_object(all_declarations) -> None:
    """Each schema is an object whose every property carries an explicit type
    and no union or reference keyword: a provider rejects the whole catalog
    over one malformed declaration."""
    violations: list[str] = []
    for name, decl in sorted(all_declarations.items()):
        if decl.schema.get("type") != "object":
            violations.append(f"{name}: schema type is {decl.schema.get('type')!r}")
        violations.extend(_walk(decl.schema, name))
        for keyword in _FORBIDDEN_KEYWORDS:
            if keyword in json.dumps(decl.schema):
                violations.append(f"{name}: serialized schema carries {keyword!r}")
    assert violations == []


def test_no_private_parameter_reaches_the_model(all_declarations) -> None:
    """Underscore-prefixed injection kwargs are invisible to the model."""
    leaked = {
        name: [p for p in decl.schema.get("properties", {}) if p.startswith("_")]
        for name, decl in all_declarations.items()
    }
    assert {n: p for n, p in leaked.items() if p} == {}


def test_description_is_the_whole_docstring(all_declarations) -> None:
    """The docstring is the sole tool-selection signal and crosses uncapped."""
    import inspect

    name = "geocode_location"
    assert all_declarations[name].description == inspect.getdoc(TOOL_REGISTRY[name].fn)


def _example(
    a: int,
    b: str,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
) -> dict:
    """Example tool with two underscore-prefixed injection kwargs.

    Neither ``_storage_client`` nor ``_bucket`` may reach the generated schema."""
    return {"a": a, "b": b}


def test_private_kwargs_never_enter_a_built_schema() -> None:
    """The private convention is applied where the schema is built."""
    schema = _tool_schema(_example)
    assert set(schema["properties"]) == {"a", "b"}
    assert schema["required"] == ["a", "b"]


@pytest.mark.parametrize(
    "annotation, default, node, required",
    [
        (str, None, {"type": "string"}, True),
        (int | None, None, {"type": "integer"}, False),
        (bool, False, {"type": "boolean"}, False),
        (float, None, {"type": "number"}, True),
        (dict | None, None, {"type": "object", "properties": {}}, False),
        (list[str] | None, None, {"type": "array", "items": {"type": "string"}}, False),
        (
            tuple[int, int] | None,
            None,
            {"type": "array", "items": {"type": "integer"}},
            False,
        ),
        (list[int], None, {"type": "array", "items": {"type": "integer"}}, True),
        (float | list[float] | None, [1.0], {"type": "array", "items": {"type": "number"}}, False),
        (list | str | None, None, {"type": "string"}, False),
    ],
)
def test_annotation_resolves_to_one_node(annotation, default, node, required) -> None:
    """Each annotation shape the registry carries resolves to a single node."""
    import inspect

    got_node, got_required = _parameter_schema(
        annotation, inspect.Parameter.empty if default is None else default
    )
    assert got_node == node
    assert got_required is required


def test_a_coordinate_union_is_offered_as_the_named_spelling() -> None:
    """A union spanning a coordinate tuple and a name offers the name, so the
    model asks for a place rather than inventing coordinates."""
    node, required = _parameter_schema(
        tuple[float, float, float, float] | list[float] | str | None, None
    )
    assert node == {"type": "string"}
    assert required is True


def test_a_custom_class_crosses_as_text_and_is_never_required() -> None:
    """A pydantic model or a client object reaches a tool only serialized."""
    from trid3nt_contracts.execution import ExecutionHandle

    assert _parameter_schema(ExecutionHandle, None) == ({"type": "string"}, False)
