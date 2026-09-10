"""``rerun_workflow`` - the model- and user-facing door onto the rerun primitive.

One tool, one docstring, one registration: the same call is what an explicit
invocation and a harness both reach.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool

from .derive import rerun

__all__ = ["rerun_workflow"]


async def rerun_workflow(run_id: str, overrides: dict[str, Any] | None = None,
                         **_extra_ignored: Any) -> Any:
    """Re-run a PAST run with named values changed - what-if, retry, recalibrate.

    **When to use:** the next question is the last one with something moved -
    "same run but rougher bed", "try k1 = 0.5"; also the repair path for a FAILED
    run, with the failing value corrected.

    **What it does:** seats your overrides on the parent's resolved sheet through
    the USER door, re-derives what depends on them and runs the template again.
    Work the overrides do not reach is REUSED; the parent is untouched.

    **When NOT to use:** a different question or place - call the template.

    **Parameters:** ``run_id``; ``overrides`` ``{param: value}`` - any declared
    param, including ones its own schema does not offer, naming a value being the
    sanctioned way a fixed quantity moves.

    **Returns:** what the parent's template returns, for the CHILD run, whose
    provenance names the parent and the overrides.

    **Raises:** typed refusals for an unknown parent, an override that moves or
    names nothing, and a changed template.
    """
    return await rerun(run_id, overrides or {})


rerun_workflow = register_tool(
    AtomicToolMetadata(
        name="rerun_workflow",
        ttl_class="live-no-cache",
        source_class="workflow_dispatch",
        cacheable=False,
    ),
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)(rerun_workflow)
