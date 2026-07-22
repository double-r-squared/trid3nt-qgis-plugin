#!/usr/bin/env python
"""Audit all tools in TOOL_REGISTRY for Gemini/Vertex OpenAPI schema compliance.

B11 — Gemini FunctionDeclaration parameter schema compliance audit (Wave 4.10).

Gemini's FunctionDeclaration parameter schema is a strict subset of OpenAPI 3.0.
Patterns that cause a request-level 400 INVALID_ARGUMENT from Vertex AI:

  * Properties with no ``type`` field (e.g. ``object | None``) — Vertex rejects
    the entire tool catalog with 400 INVALID_ARGUMENT.

  * ``anyOf`` / ``oneOf`` / ``allOf`` / ``$ref`` in the generated schema — Vertex
    rejects the declaration.

  * Underscore-prefixed parameters that survived ``_strip_private_params`` —
    ``object | None`` produces a typeless schema that triggers the 400 (job-0163).

The ``build_tool_declarations`` function in adapter.py now uses
``_normalize_callable_for_gemini`` to pre-process each tool's callable before
passing it to ``from_callable_with_api_option``.  This audit checks:

  1. ``from_callable_with_api_option`` must NOT raise for the NORMALIZED callable
     of any registered tool (B11 invariant — all tools produce machine-readable
     parameter schemas, not docstring-only fallbacks, except zero-parameter tools).
  2. No ``anyOf`` / ``oneOf`` / ``allOf`` / ``$ref`` in any generated schema.
  3. Every property in every tool's parameters schema has an explicit ``type`` field.
  4. No underscore-prefixed parameters leak past ``_strip_private_params``.

Usage::

    # From the services/agent/ directory:
    .venv/bin/python tests/audit_gemini_schema_compliance.py

    # Or via pytest:
    .venv/bin/pytest tests/audit_gemini_schema_compliance.py -v

Exit code 0 = all tools pass.  Non-zero = violations found (details printed).
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path setup — allow running from the repo root or services/agent/.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from google.genai import types as genai_types  # noqa: E402

from grace2_agent.adapter import (  # noqa: E402
    _normalize_callable_for_gemini,
    _strip_private_params,
    build_tool_declarations,
)
from grace2_agent.tools import TOOL_REGISTRY  # noqa: E402,F401 — populated on import


# ---------------------------------------------------------------------------
# Violation class
# ---------------------------------------------------------------------------

class SchemaViolation:
    """One schema compliance finding for a single tool."""

    def __init__(self, tool_name: str, violation_type: str, detail: str) -> None:
        self.tool_name = tool_name
        self.violation_type = violation_type
        self.detail = detail

    def __repr__(self) -> str:
        return (
            f"SchemaViolation(tool={self.tool_name!r}, "
            f"type={self.violation_type!r}, detail={self.detail!r})"
        )

    def __str__(self) -> str:
        return f"  [{self.violation_type}] {self.tool_name}: {self.detail}"


# ---------------------------------------------------------------------------
# Core checkers
# ---------------------------------------------------------------------------

_FORBIDDEN_KEYWORDS = ("anyOf", "oneOf", "allOf", "$ref")


def _check_schema_dict_recursive(
    tool_name: str,
    schema: dict[str, Any],
    path: str,
) -> list[SchemaViolation]:
    """Recursively walk a schema dict and flag forbidden keywords / missing types."""
    violations: list[SchemaViolation] = []

    for keyword in _FORBIDDEN_KEYWORDS:
        if keyword in schema:
            violations.append(SchemaViolation(
                tool_name,
                f"FORBIDDEN_{keyword.upper()}",
                f"{path}: contains '{keyword}'",
            ))

    # Check properties for missing type or forbidden keywords
    props = schema.get("properties")
    if isinstance(props, dict):
        for prop_name, prop_schema in props.items():
            if not isinstance(prop_schema, dict):
                continue
            prop_path = f"{path}.{prop_name}"

            # Missing type on a property is a Vertex 400 trigger.
            if "type" not in prop_schema and "anyOf" not in prop_schema:
                violations.append(SchemaViolation(
                    tool_name,
                    "MISSING_TYPE",
                    f"{prop_path}: property has no 'type' field",
                ))

            # Recurse into the property schema
            violations.extend(
                _check_schema_dict_recursive(tool_name, prop_schema, prop_path)
            )

    # Recurse into array item schema
    items = schema.get("items")
    if isinstance(items, dict):
        violations.extend(
            _check_schema_dict_recursive(tool_name, items, f"{path}[items]")
        )

    return violations


def _check_declaration_schema(
    tool_name: str,
    decl: genai_types.FunctionDeclaration,
) -> list[SchemaViolation]:
    """Inspect a FunctionDeclaration for OpenAPI compliance violations."""
    violations: list[SchemaViolation] = []

    if decl.parameters is None:
        # No schema — acceptable for zero-parameter tools (e.g. list_categories).
        # Not flagged as a violation; checked separately in the tool_has_schema test.
        return violations

    # Serialize via pydantic to a plain dict and walk it.
    try:
        schema_dict: dict[str, Any] = decl.parameters.model_dump(exclude_none=True)
    except Exception as exc:  # noqa: BLE001
        violations.append(SchemaViolation(
            tool_name,
            "SERIALIZATION_ERROR",
            f"could not serialize parameters schema: {exc}",
        ))
        return violations

    violations.extend(_check_schema_dict_recursive(tool_name, schema_dict, "schema"))

    # Belt-and-suspenders: stringify search for forbidden keywords anywhere.
    schema_str = json.dumps(schema_dict)
    for keyword in _FORBIDDEN_KEYWORDS:
        if keyword in schema_str:
            already_caught = any(
                v.violation_type == f"FORBIDDEN_{keyword.upper()}"
                for v in violations
            )
            if not already_caught:
                violations.append(SchemaViolation(
                    tool_name,
                    f"FORBIDDEN_{keyword.upper()}_RAW",
                    f"found '{keyword}' in serialized schema (not caught by recursive walk)",
                ))

    # Check for underscore-prefixed properties that leaked past _strip_private_params.
    props = decl.parameters.properties or {}
    leaked = [p for p in props if p.startswith("_")]
    if leaked:
        violations.append(SchemaViolation(
            tool_name,
            "PRIVATE_PARAM_LEAKED",
            f"underscore-prefixed params still in schema: {leaked}",
        ))

    return violations


# ---------------------------------------------------------------------------
# Primary audit function
# ---------------------------------------------------------------------------

def audit_all_tools() -> tuple[list[str], list[SchemaViolation], list[str]]:
    """Audit every tool in TOOL_REGISTRY via the normalized callable path.

    Tests the same path that ``build_tool_declarations`` uses: each tool's
    callable is first passed through ``_normalize_callable_for_gemini`` (which
    resolves forward-reference annotations and replaces Gemini-incompatible types)
    before being handed to ``from_callable_with_api_option``.

    Returns:
        (passing_tools, violations, zero_param_tools)
        - ``passing_tools``: names of tools that pass all checks.
        - ``violations``: list of ``SchemaViolation`` objects.
        - ``zero_param_tools``: tools with no parameters (docstring-only
          fallback is expected / correct; not a violation).
    """
    passing: list[str] = []
    all_violations: list[SchemaViolation] = []
    zero_param: list[str] = []

    for name, entry in sorted(TOOL_REGISTRY.items()):
        tool_violations: list[SchemaViolation] = []

        # 1. Normalise then attempt from_callable.
        normalised = _normalize_callable_for_gemini(entry.fn)
        try:
            decl = genai_types.FunctionDeclaration.from_callable_with_api_option(
                callable=normalised,
                api_option="VERTEX_AI",
            )
            decl = _strip_private_params(decl)
        except Exception as exc:  # noqa: BLE001
            tool_violations.append(SchemaViolation(
                name,
                "FROM_CALLABLE_FAILED",
                f"from_callable raised after normalisation: {str(exc)[:200]}",
            ))
            # Build a fallback declaration so we can still check schema-level issues.
            doc = inspect.getdoc(entry.fn) or f"Tool: {name}"
            decl = genai_types.FunctionDeclaration(
                name=name,
                description=doc[:1000],
            )

        # 2. Check for zero-parameter tools (expected no-schema case).
        if decl.parameters is None and not tool_violations:
            zero_param.append(name)
            passing.append(name)
            continue

        # 3. Inspect schema for forbidden patterns.
        schema_violations = _check_declaration_schema(name, decl)
        tool_violations.extend(schema_violations)

        if tool_violations:
            all_violations.extend(tool_violations)
        else:
            passing.append(name)

    return passing, all_violations, zero_param


# ---------------------------------------------------------------------------
# Report formatter
# ---------------------------------------------------------------------------

def print_report(
    passing: list[str],
    violations: list[SchemaViolation],
    zero_param: list[str],
) -> None:
    """Print a human-readable compliance report to stdout."""
    total = len(TOOL_REGISTRY)
    n_pass = len(passing)
    n_fail = len({v.tool_name for v in violations})

    print("=" * 72)
    print("GRACE-2 Gemini Schema Compliance Audit (B11 / Wave 4.10)")
    print("=" * 72)
    print(f"Total tools in registry:                 {total}")
    print(f"  Fully compliant (machine-readable schema): "
          f"{n_pass - len(zero_param)}")
    print(f"  Zero-parameter tools (expected no schema): {len(zero_param)}")
    print(f"  Tools with violations:                  {n_fail}")
    print()

    if not violations:
        print("PASS — no violations found.")
        if zero_param:
            print(f"Note: {zero_param} has no parameters (expected).")
        return

    # Group by violation type
    by_type: dict[str, list[SchemaViolation]] = {}
    for v in violations:
        by_type.setdefault(v.violation_type, []).append(v)

    for vtype, vlist in sorted(by_type.items()):
        print(f"--- {vtype} ({len(vlist)} occurrences) ---")
        for v in vlist:
            print(f"  {v.tool_name}: {v.detail[:120]}")
        print()

    print(f"RESULT: {len(violations)} violation(s) across {n_fail} tool(s).")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """Run audit; return 0 on clean pass, 1 on violations."""
    passing, violations, zero_param = audit_all_tools()
    print_report(passing, violations, zero_param)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
