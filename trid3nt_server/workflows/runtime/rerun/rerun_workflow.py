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
    "same run but rougher bed", "try k1 = 0.5", "double the discharge", "run it
    again at 20 m"; also the repair path for a FAILED run, with the value that
    failed corrected.

    **What it does:** takes the parent run's own resolved sheet, seats your
    overrides on it through the USER door, re-derives what depends on them, and
    runs the same template again. Work the overrides do not reach is REUSED - the
    parent's identical objects. The parent run is untouched.

    **When NOT to use:** a different question or place (call the template itself);
    a first run of anything (no parent to derive from).

    **Parameters:** ``run_id`` (str) the run to derive from; ``overrides`` (dict)
    ``{param: new_value}`` for that run's template - any declared param, including
    ones it does not offer on its own schema, naming a value being the sanctioned
    way a fixed quantity moves.

    **Returns:** whatever the parent's template returns - the same layer type, the
    same answer fields - for the CHILD run, whose provenance names the parent and
    the overrides. Its own run id can be rerun again.

    **Raises:** ``RERUN_PARENT_UNKNOWN`` (no such derivable run);
    ``RERUN_OVERRIDES_EMPTY`` / ``RERUN_OVERRIDE_INERT`` (nothing would move);
    ``RERUN_OVERRIDE_UNKNOWN`` (a name the template does not declare, listed
    against the ones it does); ``RERUN_TEMPLATE_CHANGED`` (the template's params
    have changed since that run); ``COUPLED_VALIDITY_REFUSED`` (the override
    leaves two values that only mean something together meaning nothing); plus
    whatever the template itself raises on the re-run.
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
