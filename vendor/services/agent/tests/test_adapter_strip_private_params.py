"""Unit tests for ``adapter._strip_private_params`` / ``build_tool_declarations``.

job-0163 surfaced a Vertex Gemini 400 INVALID_ARGUMENT that blocked the entire
tool catalog: a tool with an underscore-prefixed test-injection kwarg
(``_storage_client: object | None = None``) generated a FunctionDeclaration
property with no schema ``type`` field, which Vertex rejects. The fix strips
every underscore-prefixed property (and matching ``required`` entries) from
the generated schema before it reaches Gemini.

These tests cover:
1. The bug case — ``_storage_client: object | None`` produces a typeless schema
   that without the fix would trip Vertex; after the fix the property is gone.
2. The general filter — every underscore-prefixed property is removed even
   when its type is well-formed (``_bucket: str | None``).
3. The required list — underscore-prefixed entries are removed from
   ``required`` (defensive; underscore params have defaults and never end up
   required in practice).
4. The registry path — ``build_tool_declarations`` produces a declaration
   for ``compute_zonal_statistics`` with NO underscore properties left.
"""

from __future__ import annotations

import pytest

from grace2_agent.adapter import _strip_private_params, build_tool_declarations
from grace2_agent.tools import TOOL_REGISTRY  # noqa: F401 — populated on import
from google.genai import types as genai_types


def _example_with_private_kwargs(
    a: int,
    b: str,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
) -> dict:
    """Example tool.

    Params:
        a: required int
        b: required str
        _storage_client: test injection (must not be in schema)
        _bucket: test injection (must not be in schema)

    Returns:
        a dict
    """
    return {"a": a, "b": b}


def test_strip_removes_typeless_object_param() -> None:
    """``_storage_client: object | None`` generates a typeless Schema; the
    fix removes it so Vertex doesn't reject the catalog with INVALID_ARGUMENT."""
    decl = genai_types.FunctionDeclaration.from_callable_with_api_option(
        callable=_example_with_private_kwargs,
        api_option="VERTEX_AI",
    )
    props = decl.parameters.properties
    assert "_storage_client" in props, (
        "precondition: the generator should emit _storage_client (showing the bug exists)"
    )
    assert props["_storage_client"].type is None, (
        "precondition: the typeless Schema reproduces the Vertex 400 (no `type` field)"
    )

    cleaned = _strip_private_params(decl)
    assert "_storage_client" not in cleaned.parameters.properties
    assert "_bucket" not in cleaned.parameters.properties
    assert "a" in cleaned.parameters.properties
    assert "b" in cleaned.parameters.properties


def test_strip_preserves_public_required_list() -> None:
    """Underscore params are never required (defaults to None) but defensively
    strip them from ``required`` if present."""
    decl = genai_types.FunctionDeclaration.from_callable_with_api_option(
        callable=_example_with_private_kwargs,
        api_option="VERTEX_AI",
    )
    cleaned = _strip_private_params(decl)
    assert cleaned.parameters.required == ["a", "b"] or set(
        cleaned.parameters.required or []
    ) == {"a", "b"}
    assert all(not r.startswith("_") for r in (cleaned.parameters.required or []))


def test_strip_noop_when_no_underscore_params() -> None:
    """A declaration with no underscore params survives unchanged."""

    def public_only(x: int, y: str) -> dict:
        """Example.

        Params:
            x: int
            y: str
        """
        return {"x": x, "y": y}

    decl = genai_types.FunctionDeclaration.from_callable_with_api_option(
        callable=public_only, api_option="VERTEX_AI"
    )
    cleaned = _strip_private_params(decl)
    assert set(cleaned.parameters.properties.keys()) == {"x", "y"}
    assert cleaned.parameters.required == ["x", "y"] or set(
        cleaned.parameters.required or []
    ) == {"x", "y"}


def test_strip_noop_when_no_parameters() -> None:
    """A declaration with no parameters (e.g. docstring-only fallback) survives."""
    decl = genai_types.FunctionDeclaration(name="example", description="noop")
    cleaned = _strip_private_params(decl)
    assert cleaned == decl


def test_build_tool_declarations_drops_storage_client_for_zonal_statistics() -> None:
    """The full builder path produces a clean schema for the registry tool
    that triggered the original 400.

    This is the regression gate for the live-fire bug — if anyone re-adds an
    underscore property to a registered tool without going through the
    stripping path, this test fails before Gemini does.
    """
    from grace2_agent.tools import TOOL_REGISTRY

    decls = build_tool_declarations(TOOL_REGISTRY)
    by_name = {d.name: d for d in decls}

    # Tools known to expose _storage_client / _bucket in their public signature.
    sensitive = [
        "compute_zonal_statistics",
        "compute_hillshade",
        "compute_slope",
        "compute_aspect",
        "compute_impervious_surface",
        "extract_landcover_class",
        "clip_raster_to_bbox",
        "clip_raster_to_polygon",
        "clip_vector_to_polygon",
    ]
    for tname in sensitive:
        if tname not in by_name:
            continue  # registry shape varied across waves; tolerate absence
        decl = by_name[tname]
        if decl.parameters is None or decl.parameters.properties is None:
            # fallback declaration — no parameters to leak; that's fine
            continue
        leaked = [p for p in decl.parameters.properties if p.startswith("_")]
        assert leaked == [], (
            f"tool {tname!r} still exposes underscore-prefixed params to Gemini: {leaked}"
        )
