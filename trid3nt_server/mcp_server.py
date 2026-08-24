"""MCP stdio server: the tool registry as an MCP tool surface.

A second face on the same framework the WebSocket daemon drives. It starts the
registry the way the daemon does, advertises every registered tool with the
name, docstring and input schema the LLM adapters already synthesize, and
dispatches a call straight to the registry function.

Run it as ``python -m trid3nt_server.mcp_server`` (or the ``trid3nt-mcp``
console script) and register it in an MCP client with ``.mcp.json.example``.

V1 boundaries, deliberate:

  * stdio transport only.
  * Gates run in AUTO mode -- there is no duplex confirmation channel here, so
    an input gate proceeds with labeled defaults and a payload over the hard
    cap is REFUSED with a typed error rather than waiting for an approval that
    can never arrive.
  * Results are the summarized envelope (layer URIs, metrics, provenance,
    typed error codes) -- never megabytes of raw geometry.
  * No streamed emission, no chart cards, no WS session state. A tool that
    would publish a layer to a live map returns the layer URI instead.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
from typing import Any

logger = logging.getLogger("trid3nt_server.mcp_server")

#: Tool-description budget, matching the adapters' cap: 1000 chars captures the
#: routing block plus "Do NOT" plus "Params:" from a well-documented tool.
_DESCRIPTION_CHAR_BUDGET = 1000

#: JSON Schema type -> the Python annotation used on the synthesized wrapper
#: signature. The advertised schema is the registry's own; these annotations
#: only drive the SDK's argument validation.
_JSON_TYPE_TO_PY: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


# --------------------------------------------------------------------------- #
# Registry -> MCP tool definitions
# --------------------------------------------------------------------------- #


def load_registry() -> dict[str, Any]:
    """Populate and return ``TOOL_REGISTRY``, with the daemon's startup bindings.

    Uses the daemon's own registration import, so the spec-driven and
    late-bound tools (fetchers, QGIS discovery, solver, workflows) register
    here exactly as they do at daemon startup -- importing
    ``trid3nt_server.tools`` alone would silently under-register. The
    qgis_process submitter and the file-backed persistence singleton are bound
    for the same reason: without them the QGIS and Case-writing tools would be
    advertised but fail on every call.
    """
    from trid3nt_server.main import (
        _bind_worker_submitter,
        _import_tools_registry,
        _maybe_bind_dev_persistence,
    )

    count = _import_tools_registry()
    for bind in (_bind_worker_submitter, _maybe_bind_dev_persistence):
        try:
            bind()
        except Exception:  # noqa: BLE001 -- a missing substrate is not fatal
            logger.warning("mcp: %s failed", bind.__name__, exc_info=True)

    from trid3nt_server.tools import TOOL_REGISTRY

    logger.info("mcp: registry loaded with %d tools", count)
    return TOOL_REGISTRY


def tool_input_schema(declaration: Any) -> dict[str, Any]:
    """JSON Schema for one tool, from the shared genai declaration.

    Reuses the adapters' synthesis chain end to end -- private test-injection
    kwargs are already stripped and Gemini-hostile annotations already
    normalized by ``build_tool_declarations`` -- so the schema an MCP client
    sees is the schema the model sees on every other provider.
    """
    from .adapters.bedrock_adapter import _genai_schema_to_json_schema

    dumped = declaration.model_dump(mode="json", exclude_none=True)
    params = dumped.get("parameters")
    schema = (
        _genai_schema_to_json_schema(params)
        if params
        else {"type": "object", "properties": {}}
    )
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": {}}
    return schema


def _signature_from_schema(schema: dict[str, Any]) -> inspect.Signature:
    """Build a keyword-only signature mirroring ``schema``.

    The SDK derives its argument model from the wrapper's signature, so the
    wrapper has to carry one; ``**kwargs`` alone makes every call fail
    validation. Required properties become required keyword parameters,
    optional ones default to None.
    """
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    params: list[inspect.Parameter] = []
    for name, spec in properties.items():
        if not name.isidentifier():
            continue
        annotation = _JSON_TYPE_TO_PY.get(
            (spec or {}).get("type") if isinstance(spec, dict) else None, Any
        )
        if name in required:
            params.append(
                inspect.Parameter(
                    name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation
                )
            )
        else:
            params.append(
                inspect.Parameter(
                    name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=annotation | None if annotation is not Any else Any,
                    default=None,
                )
            )
    # Required first: Signature rejects a defaulted parameter before a bare one.
    params.sort(key=lambda p: p.default is not inspect.Parameter.empty)
    return inspect.Signature(params)


# --------------------------------------------------------------------------- #
# Payload discipline (AUTO mode -- no confirmation channel exists here)
# --------------------------------------------------------------------------- #


async def payload_verdict(tool_name: str, params: dict[str, Any]) -> tuple[str, float | None]:
    """Return ``(verdict, estimated_mb)`` for a call about to be dispatched.

    Verdicts: ``"ok"`` (under the warning threshold or no estimator declared),
    ``"warn"`` (over the warning threshold -- the call proceeds and the
    envelope carries an honest label), ``"refuse"`` (over the hard cap -- with
    no channel to ask for confirmation, proceeding would be the dishonest
    option). Reuses the declared-gate estimator seam; no new gate machinery.
    """
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.gates.cards.payload_warning import (
        _get_hard_cap_mb,
        _get_warning_threshold_mb,
        _resolve_payload_estimator,
    )

    entry = TOOL_REGISTRY.get(tool_name)
    estimator_name = getattr(getattr(entry, "metadata", None), "payload_mb_estimator_name", None)
    if not estimator_name:
        return "ok", None
    estimator = _resolve_payload_estimator(tool_name, estimator_name)
    if estimator is None:
        return "ok", None
    try:
        estimated = float(await asyncio.to_thread(estimator, **params))
    except Exception:  # noqa: BLE001 -- an estimator must never block a call
        logger.exception("mcp: payload estimate failed tool=%s", tool_name)
        return "ok", None
    if estimated > _get_hard_cap_mb():
        return "refuse", estimated
    if estimated > _get_warning_threshold_mb():
        return "warn", estimated
    return "ok", estimated


def _hard_cap_refusal(tool_name: str, estimated_mb: float) -> dict[str, Any]:
    """The typed refusal for a call whose payload exceeds the hard cap."""
    from trid3nt_server.gates.cards.payload_warning import _get_hard_cap_mb

    cap = _get_hard_cap_mb()
    message = (
        f"Estimated payload {estimated_mb:.1f} MB exceeds the {cap:.0f} MB hard "
        "cap. This MCP surface runs headless, so there is no way to confirm an "
        "oversized transfer: narrow the area of interest, coarsen the "
        "resolution, or shorten the time window and call again."
    )
    return {
        "tool": tool_name,
        "status": "error",
        "error_code": "PAYLOAD_HARD_CAP",
        "message": message,
        "error": message,
        "retryable": True,
        "estimated_mb": round(estimated_mb, 3),
        "hard_cap_mb": cap,
    }


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


async def dispatch_tool(tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run one registry tool and return its summarized envelope.

    A sync tool runs on a worker thread: this event loop is the only thing
    serving the stdio session, and a multi-minute fetch on it would stall the
    transport. The result goes through the same summarizer the model sees on
    every provider, so a LayerURI, its metrics and its provenance survive while
    raw geometry never reaches the wire.
    """
    from trid3nt_server.adapters.adapter import summarize_tool_result
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get(tool_name)
    if entry is None:
        return {
            "tool": tool_name,
            "status": "error",
            "error_code": "TOOL_NOT_FOUND",
            "message": f"No tool named {tool_name!r} is registered.",
            "error": f"No tool named {tool_name!r} is registered.",
            "retryable": False,
        }

    call_params = {k: v for k, v in params.items() if v is not None}
    verdict, estimated_mb = await payload_verdict(tool_name, call_params)
    if verdict == "refuse":
        assert estimated_mb is not None
        logger.warning(
            "mcp: refusing %s - estimated %.1f MB over hard cap", tool_name, estimated_mb
        )
        return _hard_cap_refusal(tool_name, estimated_mb)

    try:
        if asyncio.iscoroutinefunction(entry.fn):
            result = await entry.fn(**call_params)
        else:
            result = await asyncio.to_thread(entry.fn, **call_params)
    except Exception as exc:  # noqa: BLE001 -- typed envelope, never a traceback
        logger.exception("mcp: tool %s failed", tool_name)
        return summarize_tool_result(tool_name, None, error=exc)

    envelope = summarize_tool_result(tool_name, result)
    if verdict == "warn" and estimated_mb is not None:
        envelope["payload_warning_mb"] = round(estimated_mb, 3)
    return envelope


# --------------------------------------------------------------------------- #
# Server construction
# --------------------------------------------------------------------------- #


def _make_wrapper(tool_name: str, schema: dict[str, Any], description: str) -> Any:
    """An async callable carrying ``schema``'s signature, dispatching by name."""

    async def _wrapper(**kwargs: Any) -> dict[str, Any]:
        return await dispatch_tool(tool_name, kwargs)

    _wrapper.__name__ = tool_name
    _wrapper.__doc__ = description
    _wrapper.__signature__ = _signature_from_schema(schema)
    return _wrapper


def build_server(registry: dict[str, Any] | None = None) -> Any:
    """Build the MCP server with one tool per registry entry.

    ``MCPServer`` is the SDK's high-level server (the class FastMCP became in
    ``mcp`` 2.x). Each tool is registered from a synthesized wrapper so the SDK
    can validate arguments, then its advertised schema and description are
    replaced with the registry's own -- keeping parameter descriptions and enum
    values that a signature cannot carry.
    """
    from mcp.server import MCPServer
    from mcp.server.mcpserver.tools.base import Tool

    from .adapters.adapter import build_tool_declarations

    tool_registry = registry if registry is not None else load_registry()
    declarations = {d.name: d for d in build_tool_declarations(tool_registry)}

    tools: list[Any] = []
    for name in sorted(tool_registry):
        declaration = declarations.get(name)
        if declaration is None:
            logger.warning("mcp: no declaration synthesized for %s - skipped", name)
            continue
        schema = tool_input_schema(declaration)
        dumped = declaration.model_dump(mode="json", exclude_none=True)
        description = (dumped.get("description") or name)[:_DESCRIPTION_CHAR_BUDGET]

        try:
            tool = Tool.from_function(_make_wrapper(name, schema, description), name=name)
        except Exception:  # noqa: BLE001 -- one bad tool must not sink the server
            logger.exception("mcp: could not register %s", name)
            continue
        tools.append(
            tool.model_copy(update={"parameters": schema, "description": description})
        )

    logger.info("mcp: advertising %d tools", len(tools))
    return MCPServer(
        name="trid3nt",
        instructions=(
            "TRID3NT geospatial tools: fetch real data, run physics engines, and "
            "publish layers. Results are envelopes carrying layer URIs, metrics "
            "and provenance -- narrate from those values, never invent them."
        ),
        tools=tools,
    )


def main() -> None:
    """Entry point: build the server and serve MCP over stdio.

    Logging goes to stderr -- stdout is the JSON-RPC transport, and anything
    else written there corrupts the session.
    """
    logging.basicConfig(
        level=os.environ.get("TRID3NT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()


__all__ = [
    "build_server",
    "dispatch_tool",
    "load_registry",
    "main",
    "payload_verdict",
    "tool_input_schema",
]
