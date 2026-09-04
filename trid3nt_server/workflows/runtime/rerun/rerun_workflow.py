"""``rerun_workflow`` - the model- and user-facing door onto the rerun primitive.

The mechanism is ``derive.py``; this file is the SURFACE - one tool, one
docstring, one registration. The same call is what ``!run rerun_workflow(...)``
and a harness reach, so there is one implementation of "ask that question again
with this moved" rather than one per caller.
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

    **When to use:** whenever the next question is the LAST question with
    something moved. "same run but rougher bed", "try k1 = 0.5 instead",
    "double the discharge and compare", "that failed on the mesh size, run it
    again at 20 m". Also the repair path for a FAILED run: take the value that
    failed from its error and re-run the same question with it corrected.

    **What it does:** loads the parent run's own resolved sheet, seats your
    named overrides on it through the USER door, re-derives every value that
    depends on them (a value the user pinned keeps its precedence), and runs the
    same template again. Work the overrides do not reach is REUSED from the
    parent - the same terrain, the same river geometry, the same forcing series,
    the identical objects - so only authoring, solving and post-processing
    repeat. The parent run is untouched and stays exactly as it was.

    **When NOT to use:** a different question or a different place (call the
    template itself); a first run of anything (there is no parent to derive
    from).

    **Parameters:**
    - ``run_id`` (str): the run to derive from. Every completed run reports one;
      it is the id on the layer and in the run journal.
    - ``overrides`` (dict): ``{param_name: new_value}`` for the template that
      produced the parent - the SAME names that template takes. Any declared
      param may be named here, including ones the template does not offer on its
      own schema: naming a value explicitly IS the sanctioned way a fixed
      quantity moves for calibration. Nothing is absorbed silently - an
      undeclared name refuses, and so does an override that already equals the
      parent's value.

    **Returns:** whatever the parent's template returns - the same layer type,
    the same answer fields - for the CHILD run, whose provenance names the parent
    and the overrides. Its own run id can be rerun again, which is what a
    calibration loop walks.

    **Raises:** ``RERUN_PARENT_UNKNOWN`` (no such derivable run - it never
    solved, or its record expired); ``RERUN_OVERRIDES_EMPTY`` /
    ``RERUN_OVERRIDE_INERT`` (nothing would move); ``RERUN_OVERRIDE_UNKNOWN``
    (a name the template does not declare, listed against the ones it does);
    ``RERUN_TEMPLATE_CHANGED`` (the template's params have changed since that
    run, so its sheet is not one this build can run);
    ``COUPLED_VALIDITY_REFUSED`` (the override leaves two values that only mean
    something together in a combination that means nothing - e.g. a friction law
    changed without the coefficient whose meaning it inverts); plus whatever the
    template itself raises on the re-run.
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
